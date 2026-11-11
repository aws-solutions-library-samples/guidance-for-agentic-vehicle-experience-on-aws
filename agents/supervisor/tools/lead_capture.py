"""lead_capture tool — identity-promotion boundary write for the Discover phase.

Writes a STAR-JSON envelope to ``vsa-acquire-leads`` (a dedicated DDB
table) when the customer provides their contact information during a
Discover session. This is the explicit identity-promotion boundary write
per IG framing — the customer deliberately saves a configuration or
requests a callback.

Idempotency: ``contactHash = SHA256(tenantId + normalised_email +
normalised_phone)`` is used to look up any existing lead for this contact
within the same tenant. If found, the existing ``leadId`` is returned and
``associatedSessions`` is updated with the current session; no duplicate
row is created.

**Consent enforcement:** ``contact_consent`` inside ``consent_flags`` MUST
be ``True``. If it is missing, ``False``, or any other falsy value, the tool
returns a validation error without writing to DDB.

When ``acquire_leads_table`` is not configured the tool returns an explicit
``{available: false}`` response.

# schema: docs/tech.md § "CVX Discover — STAR envelope: discover.lead_capture"
"""

from __future__ import annotations

import hashlib
import logging
import re
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Optional

import boto3
from botocore.exceptions import BotoCoreError, ClientError
from strands import tool

from agents.supervisor.config import Config
from agents.supervisor.session import SessionContext

log = logging.getLogger(__name__)

_ddb_resource: Any = None
_ddb_client: Any = None


def _get_ddb(region: str = "us-west-2"):
    global _ddb_resource
    if _ddb_resource is None:
        _ddb_resource = boto3.resource("dynamodb", region_name=region)
    return _ddb_resource


def _get_ddb_client(region: str = "us-west-2"):
    global _ddb_client
    if _ddb_client is None:
        _ddb_client = boto3.client("dynamodb", region_name=region)
    return _ddb_client


def _normalise_email(email: str) -> str:
    """Normalise email to lowercase and strip surrounding whitespace."""
    return email.lower().strip()


_E164_RE = re.compile(r"^\+[1-9]\d{6,14}$")


def _normalise_phone(phone: str) -> str:
    """Normalise phone: strip spaces/dashes, return E.164 or empty string."""
    if not phone:
        return ""
    stripped = re.sub(r"[\s\-\(\)]", "", phone.strip())
    if _E164_RE.match(stripped):
        return stripped
    # Return as-is (lenient for mock CRM).
    return stripped


def _compute_contact_hash(tenant_id: str, email_norm: str, phone_norm: str) -> str:
    """SHA256 hex of tenantId + normalised_email + normalised_phone."""
    raw = f"{tenant_id}|{email_norm}|{phone_norm}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _generate_lead_id() -> str:
    """Generate a deterministic-random lead ID in the ORD-style pattern."""
    import secrets
    today = datetime.now(timezone.utc).strftime("%Y%m%d")
    suffix = secrets.token_hex(4).upper()
    return f"LEAD-{today}-{suffix}"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def make_lead_capture_tool(ctx: SessionContext, config: Config):
    """Build the lead_capture tool bound to a session context."""

    leads_table: str = getattr(config, "acquire_leads_table", "") or ""
    aws_region: str = getattr(config, "aws_region", "us-west-2") or "us-west-2"

    @tool
    def lead_capture(
        contact_info: dict,
        consent_flags: dict,
        saved_config: Optional[dict] = None,
    ) -> dict[str, Any]:
        """Capture the customer's contact information and save a Discover lead.

        Call this tool when the customer is ready to save their configuration,
        request a callback, or receive more information — and has provided
        explicit consent. This writes a lead record that carries through to
        the Order phase.

        IMPORTANT: Always present a consent checkbox to the customer before
        calling this tool. Do NOT call this tool unless ``contact_consent``
        is explicitly ``True`` in ``consent_flags``.

        Args:
            contact_info: Dict with at least one of ``email`` (str) or
                ``phone`` (str). Both may be provided.
            consent_flags: Dict with ``contact_consent`` (bool, required True)
                and optionally ``marketing_consent`` (bool, default False).
            saved_config: Optional dict with ``preferred_category`` (str) and
                ``preferred_model_id`` (str) to carry into the Order phase.

        Returns:
            Dict with ``leadId`` (str), ``wroteAt`` (ISO-8601 timestamp),
            and ``available`` (bool). Returns a validation error dict if
            consent is missing/false.
        """
        if not leads_table:
            return {"available": False, "reason": "leads table not configured"}

        # Consent enforcement — MUST NOT write without explicit contact_consent.
        #
        # Identity comparison against True, NOT truthiness. Security review
        # 2026-07-29 W1: `if not contact_consent` refused falsy values but
        # ACCEPTED any truthy non-True value — the string "false", 1, [False],
        # a dict, and so on. That contradicts both the spec ("MUST be True")
        # and this module's docstring, and it matters because the value arrives
        # from an LLM tool call whose arguments are ultimately influenced by
        # user text. Worse, the STAR envelope written below hard-codes
        # `consent.contactConsent: True`, so a lead captured on a truthy-ish
        # value would be recorded as if consent had been explicit.
        contact_consent = consent_flags.get("contact_consent") if consent_flags else None
        if contact_consent is not True:
            return {
                "available": False,
                "reason": (
                    "contact_consent is required and must be exactly True "
                    f"(received {type(contact_consent).__name__}). "
                    "Obtain explicit customer consent before capturing lead."
                ),
            }

        email_raw: str = contact_info.get("email", "") if contact_info else ""
        phone_raw: str = contact_info.get("phone", "") if contact_info else ""

        if not email_raw and not phone_raw:
            return {
                "available": False,
                "reason": "contact_info must include at least one of 'email' or 'phone'",
            }

        email_norm = _normalise_email(email_raw) if email_raw else ""
        phone_norm = _normalise_phone(phone_raw) if phone_raw else ""
        contact_hash = _compute_contact_hash(ctx.tenant_id, email_norm, phone_norm)
        now_iso = _utc_now_iso()
        session_id = ctx.session_id

        try:
            ddb = _get_ddb(aws_region)
            table = ddb.Table(leads_table)

            # Idempotency check — query by-tenant-and-contact GSI.
            try:
                gsi_resp = table.query(
                    IndexName="by-tenant-and-contact",
                    KeyConditionExpression=(
                        "tenantId = :tid AND contactHash = :ch"
                    ),
                    ExpressionAttributeValues={
                        ":tid": ctx.tenant_id,
                        ":ch": contact_hash,
                    },
                    ProjectionExpression="leadId",
                )
                existing_items = gsi_resp.get("Items", [])
            except (BotoCoreError, ClientError) as exc:
                log.warning("lead_capture: GSI query failed (%s)", exc)
                existing_items = []

            if existing_items:
                existing_lead_id = existing_items[0]["leadId"]
                # Update associatedSessions on the existing record.
                try:
                    table.update_item(
                        Key={"leadId": existing_lead_id},
                        UpdateExpression=(
                            "SET updatedAt = :ua, "
                            "associatedSessions = list_append("
                            "  if_not_exists(associatedSessions, :empty_list), "
                            "  :new_session"
                            ")"
                        ),
                        ExpressionAttributeValues={
                            ":ua": now_iso,
                            ":empty_list": [],
                            ":new_session": [session_id],
                        },
                    )
                except (BotoCoreError, ClientError) as exc:
                    log.warning(
                        "lead_capture: update of existing lead %s failed (%s)",
                        existing_lead_id,
                        exc,
                    )
                return {
                    "available": True,
                    "leadId": existing_lead_id,
                    "wroteAt": now_iso,
                    "idempotent": True,
                }

            # New lead — build the STAR envelope and write.
            lead_id = _generate_lead_id()
            marketing_consent: bool = bool(consent_flags.get("marketing_consent", False))

            preferred_category = ""
            preferred_model_id = ""
            if saved_config:
                preferred_category = str(saved_config.get("preferred_category") or "")
                preferred_model_id = str(saved_config.get("preferred_model_id") or "")

            # STAR envelope — schema: docs/tech.md §
            # "CVX Discover — STAR envelope: discover.lead_capture"
            envelope = {
                "envelope_type": "discover.lead_capture",
                "envelope_version": "v1",
                "tenantId": ctx.tenant_id,
                "leadId": lead_id,
                "sessionId": session_id,
                "contactInfo": {
                    "emailNormalized": email_norm,
                    "phoneE164": phone_norm,
                    "contactHash": contact_hash,
                },
                "consent": {
                    "contactConsent": True,
                    "contactConsentTimestamp": now_iso,
                    "marketingConsent": marketing_consent,
                },
                "savedConfig": {
                    "preferredCategory": preferred_category,
                    "preferredModelId": preferred_model_id,
                },
                "createdAt": now_iso,
                "sourcePath": "pathA",
            }

            item = {
                "leadId": lead_id,
                "tenantId": ctx.tenant_id,
                "sessionId": session_id,
                "contactHash": contact_hash,
                "contactInfo": {
                    "email": email_norm,
                    "phone": phone_norm,
                },
                "savedConfig": {
                    "preferredCategory": preferred_category,
                    "preferredModelId": preferred_model_id,
                },
                "envelope": envelope,
                "createdAt": now_iso,
                "updatedAt": now_iso,
                "associatedSessions": [session_id],
            }

            table.put_item(
                Item=item,
                ConditionExpression="attribute_not_exists(leadId)",
            )

        except (BotoCoreError, ClientError) as exc:
            log.warning("lead_capture: DDB write failed (%s)", exc)
            return {"available": False, "reason": f"lead capture failed: {exc}"}

        return {
            "available": True,
            "leadId": lead_id,
            "wroteAt": now_iso,
            "idempotent": False,
        }

    return lead_capture
