"""POST /acquire/leads  — create a lead record
GET  /acquire/leads/{leadId} — read back a lead record

Lead capture is feature-flagged OFF by default (per spec § Constraints:
"No PII by default — ships feature-flagged OFF"). When the flag is off,
POST returns 404 rather than 500.

On a show floor, lead data is real attendee contact info with consent
and retention obligations, so the flag must be explicitly enabled by an
operator before the endpoint becomes live.

Tenant clamp:
  POST — body.tenantId must match claim tenantId.
  GET  — the stored lead's tenantId must match claim tenantId.

Env vars:
    VSA_ACQUIRE_LEADS_TABLE            (default: vsa-dev-acquire-leads)
    VSA_ACQUIRE_LEAD_CAPTURE_ENABLED   (default: false / absent)
    AWS_REGION                         (default: us-east-1)
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

import boto3

log = logging.getLogger()
log.setLevel(logging.INFO)

_LEADS_TABLE = os.environ.get("VSA_ACQUIRE_LEADS_TABLE", "vsa-dev-acquire-leads")

_ddb = None


def _get_ddb():
    global _ddb
    if _ddb is None:
        _ddb = boto3.resource("dynamodb", region_name=os.environ.get("AWS_REGION", "us-east-1"))
    return _ddb


def _lead_capture_enabled() -> bool:
    """Read the feature-flag from the environment at call time (not at import)."""
    val = os.environ.get("VSA_ACQUIRE_LEAD_CAPTURE_ENABLED", "false").strip().lower()
    return val in ("1", "true", "yes", "on")


class _DecimalEncoder(json.JSONEncoder):
    def default(self, o):
        if isinstance(o, Decimal):
            return int(o) if o % 1 == 0 else float(o)
        return super().default(o)


def _to_jsonable(obj: Any) -> Any:
    if isinstance(obj, Decimal):
        return int(obj) if obj % 1 == 0 else float(obj)
    if isinstance(obj, dict):
        return {k: _to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_to_jsonable(v) for v in obj]
    return obj


def _resp(status: int, body: dict) -> dict:
    return {
        "statusCode": status,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*",
        },
        "body": json.dumps(body, cls=_DecimalEncoder),
    }


def _make_lead_id(tenant_id: str, session_id: str, captured_at: datetime) -> str:
    """Generate a deterministic leadId from (tenantId, sessionId, second)."""
    ts = captured_at.strftime("%Y%m%d")
    seed = f"{tenant_id}|{session_id}|{captured_at.strftime('%Y%m%dT%H%M%S')}"
    hex_suffix = hashlib.sha256(seed.encode()).hexdigest()[:8]
    return f"LEAD-{ts}-{hex_suffix}"


# Fields returned to the client for a lead read-back
_CLIENT_FIELDS = frozenset({
    "leadId",
    "tenantId",
    "sessionId",
    "capturedAt",
    "contactInfo",
    "modelId",
    "discoverSessionId",
})


def _public_lead(item: dict) -> dict:
    """Return only client-visible fields from a raw DDB lead item."""
    raw = _to_jsonable(item)
    return {k: raw[k] for k in _CLIENT_FIELDS if k in raw}


def _handle_post(event: dict, claims: dict) -> dict:
    """Handle POST /acquire/leads — create a lead."""
    # ── Feature flag ──────────────────────────────────────────────────────
    if not _lead_capture_enabled():
        return _resp(404, {"error": "Lead capture is not enabled"})

    # ── Parse request body ────────────────────────────────────────────────
    try:
        body = json.loads(event.get("body") or "{}")
    except (json.JSONDecodeError, ValueError):
        return _resp(400, {"error": "Body must be valid JSON"})

    # ── Validate required fields ──────────────────────────────────────────
    tenant_id_body = body.get("tenantId")
    if not tenant_id_body:
        return _resp(400, {"error": "Missing required field: tenantId"})

    session_id = body.get("sessionId") or ""

    # ── Tenant clamp ──────────────────────────────────────────────────────
    claim_tenant = claims.get("custom:tenantId") or claims.get("tenantId")
    if not claim_tenant or tenant_id_body != claim_tenant:
        return _resp(403, {"error": "Tenant mismatch — access denied"})

    # ── Write lead ────────────────────────────────────────────────────────
    now = datetime.now(timezone.utc)
    lead_id = _make_lead_id(claim_tenant, session_id, now)
    captured_at_iso = now.isoformat()

    item: dict[str, Any] = {
        "leadId": lead_id,
        "tenantId": claim_tenant,
        "capturedAt": captured_at_iso,
    }
    if session_id:
        item["sessionId"] = session_id
    if body.get("contactInfo"):
        item["contactInfo"] = body["contactInfo"]
    if body.get("modelId"):
        item["modelId"] = body["modelId"]
    if body.get("discoverSessionId"):
        item["discoverSessionId"] = body["discoverSessionId"]

    try:
        _get_ddb().Table(_LEADS_TABLE).put_item(Item=item)
        log.info("api-acquire-leads: created leadId=%s tenantId=%s", lead_id, claim_tenant)
    except Exception as exc:  # noqa: BLE001
        log.exception("api-acquire-leads: DDB PutItem failed for leadId=%s: %s", lead_id, exc)
        return _resp(500, {"error": "Failed to persist lead"})

    return _resp(201, {"leadId": lead_id, "capturedAt": captured_at_iso})


def _handle_get(event: dict, claims: dict) -> dict:
    """Handle GET /acquire/leads/{leadId} — read back a lead."""
    # ── Extract path param ────────────────────────────────────────────────
    path_params = event.get("pathParameters") or {}
    lead_id = path_params.get("leadId") if path_params else None
    if not lead_id:
        return _resp(400, {"error": "Missing required path parameter: leadId"})

    # ── Fetch lead ────────────────────────────────────────────────────────
    try:
        table = _get_ddb().Table(_LEADS_TABLE)
        result = table.get_item(Key={"leadId": lead_id})
    except Exception as exc:  # noqa: BLE001
        log.exception("api-acquire-leads: DDB GetItem failed for leadId=%s: %s", lead_id, exc)
        return _resp(500, {"error": "Failed to retrieve lead"})

    item = result.get("Item")
    if not item:
        return _resp(404, {"error": f"Lead '{lead_id}' not found"})

    # ── Tenant clamp ──────────────────────────────────────────────────────
    claim_tenant = claims.get("custom:tenantId") or claims.get("tenantId")
    lead_tenant = item.get("tenantId")
    if not claim_tenant or lead_tenant != claim_tenant:
        return _resp(403, {"error": "Tenant mismatch — access denied"})

    return _resp(200, _public_lead(item))


def handler(event: dict[str, Any], _context: Any) -> dict[str, Any]:
    """Lambda entry point — dispatches to POST or GET based on httpMethod."""
    claims = (
        (event.get("requestContext") or {})
        .get("authorizer", {})
        .get("claims") or {}
    )

    method = (event.get("httpMethod") or "GET").upper()
    if method == "POST":
        return _handle_post(event, claims)
    if method == "GET":
        return _handle_get(event, claims)

    return _resp(405, {"error": f"Method not allowed: {method}"})
