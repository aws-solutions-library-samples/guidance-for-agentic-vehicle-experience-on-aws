"""API: GET /rsa/dispatch/{dispatchId}

Returns a tenant-scoped status projection of a single RSA dispatch row.

Path parameter:
    dispatchId   — the PK of the row in vsa-{stage}-rsa-dispatch.
                   Must match /^rsa-[0-9a-f-]{36}$/ (basic format guard).

Response 200:
    {
      "dispatchId":   "rsa-<uuid4>",
      "status":       "REQUESTED | DISPATCHED | EN_ROUTE | ARRIVED | FAILED | CANCELLED",
      "etaMinutes":   <int | null>,
      "providerRef":  "<str | null>",
      "simulated":    <bool | null>,
      "serviceType":  "<str | null>",
      "createdAt":    "<ISO-8601Z>",
      "dispatchedAt": "<ISO-8601Z | null>",
      "enRouteAt":    "<ISO-8601Z | null>",
      "arrivedAt":    "<ISO-8601Z | null>",
      "failedAt":     "<ISO-8601Z | null>",
      "updatedAt":    "<ISO-8601Z | null>",
      "asOf":         "<ISO-8601Z>"
    }

Response 400: malformed dispatchId.
Response 401: missing tenantId claim.
Response 404: row absent, row has empty tenantId, or tenantId mismatch.

Design notes
------------
- Tenant isolation: read the caller's JWT `custom:tenantId` claim (same
  extraction as api-escalate/handler.py) and compare to the row's `tenantId`.
  Not-found and mismatch return the same 404 body so a caller cannot enumerate
  another tenant's dispatch IDs (G3-D9).

- Fail closed on empty tenantId: an empty string must never equal an empty
  string — both sides must be non-empty before comparing (G3-D9).

- Allowlisted projection: only the fields enumerated in G3-D8 are returned.
  In particular, `vin`, `lat`, `lon`, `callbackContact`, `driverId`,
  `driverName`, `vehicleId`, `connectContactId`, `summary`, `reason`,
  `triageSessionId`, `voiceSessionId`, `tenantId`, and `failureReason` are
  never returned. `failureReason` contains internal diagnostics that may still
  carry exception text (per the two latent Suggestions carried forward from
  Group 2).

- `simulated` field (G3-D7): returned as the row's boolean attribute when
  present. When the attribute is absent (a REQUESTED row, or a row that
  failed before any provider call), falls back to `true` if VSA_RSA_CLIENT
  is "fake" or unset, `null` otherwise. Prevents presenting a simulated
  dispatch as real, which is the failure this spec exists to prevent.

- `asOf`: the server wall-clock at the moment of the DDB read, ISO-8601 UTC
  with millisecond precision and a Z suffix (same format as other timestamps
  per G3-D3), so a client can judge staleness.

- KMS: the table uses SSE-KMS. This Lambda's role is granted kms:Decrypt +
  kms:DescribeKey on the CMS CMK, conditioned on kms:ViaService (G3-D10).
"""
from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

import boto3
from botocore.exceptions import BotoCoreError, ClientError

log = logging.getLogger()
log.setLevel(logging.INFO)

# ---------------------------------------------------------------------------
# Environment variables — bound at module load so a missing var fails loudly
# at cold-start rather than silently at the first request.
# ---------------------------------------------------------------------------
_RSA_DISPATCH_TABLE = os.environ["RSA_DISPATCH_TABLE"]
_VSA_RSA_CLIENT = os.environ.get("VSA_RSA_CLIENT", "fake")

# Regex for a basic dispatch-ID format check.  The PK format is
# `rsa-{uuid4}` (api-escalate/handler.py _queue_rsa_action).
_DISPATCH_ID_RE = re.compile(r"^rsa-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")

# Allowlisted attribute names returned to the caller (G3-D8).
_ALLOWED_ATTRS = frozenset({
    "dispatchId",
    "status",
    "etaMinutes",
    "providerRef",
    "simulated",
    "serviceType",
    "createdAt",
    "dispatchedAt",
    "enRouteAt",
    "arrivedAt",
    "failedAt",
    "updatedAt",
})

# ---------------------------------------------------------------------------
# DynamoDB client factory (patched in tests)
# ---------------------------------------------------------------------------
_ddb_client: Any = None


def _get_ddb() -> Any:
    global _ddb_client
    if _ddb_client is None:
        _ddb_client = boto3.resource("dynamodb")
    return _ddb_client


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    """Current UTC time as ISO-8601 with ms precision and Z suffix (G3-D3)."""
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _response(status_code: int, body: dict) -> dict:
    """Build an API Gateway response with CORS headers (same as /escalate's _response)."""
    return {
        "statusCode": status_code,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": os.environ.get("ALLOWED_ORIGIN", "*"),
            "Access-Control-Allow-Headers": "Content-Type,Authorization",
        },
        "body": json.dumps(body),
    }


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------


def handler(event: dict[str, Any], _context: Any) -> dict[str, Any]:
    # ---- Auth: pull tenantId from JWT claims (same extraction as /escalate) ----
    claims = (event.get("requestContext") or {}).get("authorizer", {}).get("claims") or {}
    caller_tenant = claims.get("custom:tenantId") or claims.get("tenantId")
    if not caller_tenant:
        return _response(401, {"error": "missing tenantId claim"})

    # ---- Path parameter: dispatchId ----
    path_params = event.get("pathParameters") or {}
    dispatch_id = (path_params.get("dispatchId") or "").strip()
    if not dispatch_id or not _DISPATCH_ID_RE.match(dispatch_id):
        return _response(400, {"error": "invalid or missing dispatchId"})

    # ---- DynamoDB GetItem ----
    as_of = _now_iso()
    try:
        table = _get_ddb().Table(_RSA_DISPATCH_TABLE)
        result = table.get_item(Key={"dispatchId": dispatch_id})
    except (BotoCoreError, ClientError) as exc:
        log.error("DynamoDB GetItem failed for dispatchId=%s: %s", dispatch_id, exc)
        return _response(503, {"error": "service temporarily unavailable"})

    item = result.get("Item")

    # ---- Tenant check (G3-D9): absent, empty, or mismatched → same 404 ----
    # An empty string must never equal an empty string; both sides must be
    # non-empty before comparing.
    row_tenant = item.get("tenantId") if item else None
    if not item or not row_tenant or row_tenant != caller_tenant:
        return _response(404, {"error": "dispatch not found"})

    # ---- Build projection (G3-D8) ----
    # Only allowlisted attributes are copied; never return tenantId, vin,
    # lat, lon, failureReason, or any other internal field.
    projection: dict[str, Any] = {}
    for key in _ALLOWED_ATTRS:
        if key in item:
            val = item[key]
            # DynamoDB Decimal → int for etaMinutes; avoid silently truncating
            # other Decimal values (e.g. Decimal("NaN") → json.dumps failure).
            if key == "etaMinutes" and isinstance(val, Decimal):
                val = int(val)
            projection[key] = val

    # ---- simulated fallback (G3-D7) ----
    # Row attribute present → use it.
    # Absent (REQUESTED row or pre-provider failure) → true when client is
    # "fake"/unset, null otherwise.  Leans toward "simulated" because
    # presenting a simulated dispatch as real is the failure this spec prevents.
    if "simulated" not in projection:
        projection["simulated"] = True if _VSA_RSA_CLIENT in ("fake", "") else None

    # ---- asOf: server read time so a client can judge staleness ----
    projection["asOf"] = as_of

    return _response(200, projection)
