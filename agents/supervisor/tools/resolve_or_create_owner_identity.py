"""resolve_or_create_owner_identity tool — look up or create an owner identity.

V1 mock — real impl provisions a Cognito user in the tenant pool with
``confirm_sign_up`` + user attributes, or looks up an existing user by
email. This version uses a deterministic UUID-v5 based on the tenant and
email address so the same caller always receives the same ``ownerSub``.

The ``_seen`` module-level set tracks which subs have been returned in
the current process lifetime, so ``isNewIdentity`` is True the first time
a (tenantId, email) pair is resolved and False on subsequent calls. This
state is intentionally reset on process restart — production Cognito
persistence is a separate concern addressed in the real implementation.

Note: V1 mock — real impl provisions Cognito user in tenant pool with
confirm_sign_up + user attributes.

Additive extension (spec 2026-08-04-cvx-oem-discover-order-meridian, merged from predecessor 2026-07-28, Group 2 Task 6):
When an optional ``leadId`` is supplied, the tool reads the lead record from
``vsa-acquire-leads`` (via ``config.acquire_leads_table``) and hydrates
``email`` and ``phone`` from the stored ``contactInfo`` before proceeding with
normal identity resolution.  When ``leadId`` is omitted, behaviour is
unchanged (backward compatible).
"""

from __future__ import annotations

import logging
import uuid
from typing import Any, Optional

import boto3
from botocore.exceptions import BotoCoreError, ClientError
from strands import tool

from agents.supervisor.config import Config
from agents.supervisor.session import SessionContext

log = logging.getLogger(__name__)

# UUID namespace for deterministic sub generation.
# Using the OID namespace so the generated UUID is scoped to our
# application context without a custom UUID namespace registration.
_NAMESPACE_OID = uuid.UUID("6ba7b812-9dad-11d1-80b4-00c04fd430c8")

# Module-level "seen" set — tracks (tenantId, email.lower()) pairs that have
# been returned this process. Enables isNewIdentity = True on first call.
# Intentionally not persisted — reset on process restart (V1 mock behaviour).
_seen: set[str] = set()

_ddb = None


def _get_ddb():
    global _ddb
    if _ddb is None:
        _ddb = boto3.resource("dynamodb")
    return _ddb


def make_resolve_or_create_owner_identity_tool(ctx: SessionContext, config: Config):
    """Build the resolve_or_create_owner_identity tool bound to a session context."""

    @tool
    def resolve_or_create_owner_identity(
        email: str,
        firstName: str,
        lastName: str,
        phone: Optional[str] = None,
        leadId: Optional[str] = None,
    ) -> dict[str, Any]:
        """Resolve an existing owner identity or create a new one for the buyer.

        Use this tool during the reservation flow to associate the order with
        a verifiable customer identity. Returns a stable sub (unique identifier)
        for the owner that links the order to their account.

        This tool does NOT perform authentication — it creates or finds an
        identity record for the order association only. The customer must
        separately authenticate to access their order history.

        When a ``leadId`` from the Discover phase is supplied, contact
        information (email, phone) is hydrated from the stored lead record in
        ``vsa-acquire-leads``, skipping the need for the buyer to re-enter
        contact details during the Order phase.

        Args:
            email: The buyer's email address. Used as the identity key.
                Normalized to lower-case before lookup.  May be overridden
                by the lead record when ``leadId`` is provided.
            firstName: Buyer's first name.
            lastName: Buyer's last name.
            phone: Optional phone number for contact record.  May be
                overridden by the lead record when ``leadId`` is provided.
            leadId: Optional lead identifier from the Discover phase.  When
                supplied, the tool fetches the lead record and hydrates
                ``email`` and ``phone`` from it.  Existing behaviour is
                preserved when omitted.

        Returns:
            Dict with ``ownerSub`` (stable unique identifier), ``isNewIdentity``
            (True if this email has not been resolved before in this session),
            ``email``, ``firstName``, ``lastName``.
        """
        resolved_email = email
        resolved_phone = phone

        # Additive lead-record hydration path (spec 2026-08-04-cvx-oem-discover-order-meridian, merged from predecessor 2026-07-28).
        # When a leadId is supplied and the leads table is configured, fetch the
        # lead record and override email/phone from the stored contactInfo.
        leads_table_name = getattr(config, "acquire_leads_table", "")
        if leadId and leads_table_name:
            try:
                response = _get_ddb().Table(leads_table_name).get_item(
                    Key={"leadId": leadId}
                )
                lead_item = response.get("Item")
                if lead_item:
                    contact_info = lead_item.get("contactInfo") or {}
                    hydrated_email = contact_info.get("emailNormalized", "")
                    hydrated_phone = contact_info.get("phoneE164", "")
                    if hydrated_email:
                        resolved_email = hydrated_email
                    if hydrated_phone:
                        resolved_phone = hydrated_phone
                    log.info(
                        "resolve_or_create_owner_identity: hydrated contact from "
                        "leadId=%s tenant=%s",
                        leadId,
                        ctx.tenant_id,
                    )
                else:
                    log.warning(
                        "resolve_or_create_owner_identity: leadId=%s not found in "
                        "table=%s — using caller-supplied email",
                        leadId,
                        leads_table_name,
                    )
            except (BotoCoreError, ClientError) as exc:
                # Non-fatal: log and fall back to caller-supplied contact info.
                log.warning(
                    "resolve_or_create_owner_identity: could not fetch leadId=%s: %s "
                    "— using caller-supplied email",
                    leadId,
                    exc,
                )

        normalized_email = resolved_email.strip().lower()
        seen_key = f"{ctx.tenant_id}\x00{normalized_email}"

        # Deterministic ownerSub — same tenant+email always maps to the same sub
        owner_sub = str(uuid.uuid5(_NAMESPACE_OID, seen_key))

        is_new = seen_key not in _seen
        _seen.add(seen_key)

        log.info(
            "resolve_or_create_owner_identity: tenant=%s email=%s sub=%s is_new=%s",
            ctx.tenant_id,
            normalized_email,
            owner_sub,
            is_new,
        )

        return {
            "ownerSub": owner_sub,
            "isNewIdentity": is_new,
            "email": normalized_email,
            "firstName": firstName,
            "lastName": lastName,
        }

    return resolve_or_create_owner_identity
