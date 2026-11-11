"""
AVX Notifications: Register Device Lambda (G4.T4)

Route: POST /notifications/register-device

Request body:
  {
    "device_token": "<APNs device token>",
    "per_category_consent": {"safety": bool, "coverage": bool, "service": bool},
    "platform": "apns-staging" | "apns-prod"
  }

Behavior:
  1. Extract owner_id from claims.sub (NEVER from body or path)
  2. Delete every other row for this owner (1:1 device, see below)
  3. Upsert a row in vsa-{stage}-notification-devices:
       PK: owner_id
       SK: device_token (SHA-256-prefixed log-safe hash in logs only; actual value stored)
  4. Call sns.create_platform_endpoint() with the platform-application ARN from
     env AVX_APNS_PLATFORM_APP_ARN. If the env var is empty/unset, skip SNS and
     record endpoint_arn=None (with a Warning log).
  5. Store the returned endpoint ARN in the DynamoDB row.
  6. Return {success: true}

One device per owner (D-SCOPE-1, decisions-scope-1to1-2026-09-24.md):
  An owner has AT MOST ONE row. Registering a new device deletes the owner's
  other rows first, so the last device registered is the one whose consent
  governs and whose endpoint the broker pushes to. This deliberately breaks the
  "N devices per owner" reading the (owner_id, device_token) key allows, and
  it is what makes the broker's Limit=1 device reads correct by construction.
  A Query or DeleteItem failure returns 500 before any write, which is stricter
  than the SNS-failure path (201 with endpoint_arn=None): a leftover old row is
  a disclosure hole, a missing endpoint only means no push.

  Residual race: two registrations for the same owner from two devices in the
  same moment can each find nothing to delete and both write, leaving two
  rows. The next registration from either device heals it. Closing it fully
  needs an owner-only key, a schema migration D-SCOPE-1 rejected. The old
  device's SNS endpoint is not deleted (no sns:DeleteEndpoint grant); nothing
  reads it once its row is gone.

Security:
  - Device tokens are secrets by APNs convention. NEVER log the token value.
    Log a SHA-256 hex prefix (first 16 chars) only for traceability.
  - NEVER echo the device_token in any response field.

Environment variables:
  AVX_STAGE                        — stage name (default "dev")
  AVX_NOTIFICATION_DEVICES_TABLE   — DynamoDB table name
  AVX_APNS_PLATFORM_APP_ARN        — SNS platform-application ARN for APNs
                                     (empty → skip SNS, log warning)
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import sys
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

import boto3
from boto3.dynamodb.conditions import Key

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------
_LAMBDA_DIR = os.path.dirname(os.path.abspath(__file__))
_LAMBDAS_DIR = os.path.dirname(_LAMBDA_DIR)
if _LAMBDAS_DIR not in sys.path:
    sys.path.insert(0, _LAMBDAS_DIR)

log = logging.getLogger(__name__)
log.setLevel(logging.INFO)

_STAGE = os.environ.get("AVX_STAGE", "dev")
_DEVICES_TABLE = os.environ.get(
    "AVX_NOTIFICATION_DEVICES_TABLE", f"vsa-{_STAGE}-notification-devices"
)
_APNS_PLATFORM_APP_ARN = os.environ.get("AVX_APNS_PLATFORM_APP_ARN", "")

_ddb = None
_sns = None


def _get_ddb():
    global _ddb
    if _ddb is None:
        _ddb = boto3.resource("dynamodb")
    return _ddb


def _get_sns():
    global _sns
    if _sns is None:
        _sns = boto3.client("sns")
    return _sns


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class _DecimalEncoder(json.JSONEncoder):
    def default(self, o):
        if isinstance(o, Decimal):
            return int(o) if o % 1 == 0 else float(o)
        return super().default(o)


def _respond(status: int, body: dict) -> dict[str, Any]:
    return {
        "statusCode": status,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": os.environ.get("ALLOWED_ORIGIN", "*"),
            "Access-Control-Allow-Headers": "Content-Type,Authorization",
        },
        "body": json.dumps(body, cls=_DecimalEncoder),
    }


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _token_log_prefix(device_token: str) -> str:
    """Return a non-reversible 16-char prefix safe for logging."""
    return hashlib.sha256(device_token.encode()).hexdigest()[:16]


def _get_claims(event: dict) -> dict | None:
    try:
        claims = (event.get("requestContext", {})
                       .get("authorizer", {})
                       .get("claims"))
        if claims and claims.get("sub"):
            return claims
        return None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# SNS endpoint registration
# ---------------------------------------------------------------------------

def _create_platform_endpoint(device_token: str, owner_id: str) -> str | None:
    """Call SNS CreatePlatformEndpoint. Returns endpoint ARN or None on failure."""
    if not _APNS_PLATFORM_APP_ARN:
        log.warning(
            "AVX_APNS_PLATFORM_APP_ARN is not set; "
            "skipping SNS endpoint creation for token_prefix=%s",
            _token_log_prefix(device_token),
        )
        return None

    try:
        resp = _get_sns().create_platform_endpoint(
            PlatformApplicationArn=_APNS_PLATFORM_APP_ARN,
            Token=device_token,
            CustomUserData=owner_id,  # link endpoint to owner for future re-key
        )
        arn = resp.get("EndpointArn")
        log.info(
            "SNS endpoint created/retrieved for token_prefix=%s owner_id=%s",
            _token_log_prefix(device_token),
            owner_id,
        )
        return arn
    except Exception as exc:
        log.error(
            "SNS create_platform_endpoint failed for token_prefix=%s: %s",
            _token_log_prefix(device_token),
            exc,
        )
        return None


# ---------------------------------------------------------------------------
# 1:1 device enforcement (D-SCOPE-1)
# ---------------------------------------------------------------------------

def _delete_other_devices(owner_id: str, device_token: str) -> int:
    """Delete every row for ``owner_id`` whose ``device_token`` differs.

    Pages ``LastEvaluatedKey`` to exhaustion with no ``Limit``, so a second
    page of rows is never left behind. The row for ``device_token`` itself is
    kept: re-registering the same device stays an in-place ``PutItem``.

    Returns the number of rows deleted. Any error propagates; the caller fails
    the request before writing, so no partial state is created.

    The table is resolved here (not passed in) so the IAM call-site guard in
    infrastructure/test/vsa-api-stack-avx.test.ts can attribute the Query and
    DeleteItem to the devices table.
    """
    table = _get_ddb().Table(_DEVICES_TABLE)
    kwargs: dict[str, Any] = {
        "KeyConditionExpression": Key("owner_id").eq(owner_id),
        "ProjectionExpression": "owner_id, device_token",
    }
    deleted = 0
    while True:
        resp = table.query(**kwargs)
        for row in resp.get("Items", []):
            if row.get("device_token") == device_token:
                continue
            table.delete_item(Key={"owner_id": owner_id, "device_token": row["device_token"]})
            deleted += 1
        last_key = resp.get("LastEvaluatedKey")
        if not last_key:
            return deleted
        kwargs["ExclusiveStartKey"] = last_key


# ---------------------------------------------------------------------------
# Route handler
# ---------------------------------------------------------------------------

def handler(event: dict, context: Any) -> dict[str, Any]:
    claims = _get_claims(event)
    if claims is None:
        return _respond(401, {"error": "missing or invalid JWT claims"})

    try:
        body = json.loads(event.get("body") or "{}")
    except json.JSONDecodeError:
        return _respond(400, {"error": "invalid JSON body"})

    # Validate required fields
    device_token = body.get("device_token")
    if not device_token:
        return _respond(400, {"error": "device_token required"})

    per_category_consent = body.get("per_category_consent")
    if not isinstance(per_category_consent, dict):
        return _respond(400, {"error": "per_category_consent must be an object"})

    # Validate consent structure
    # W7: enforce additionalProperties:false — only copy declared keys
    _VALID_CONSENT_KEYS = {"safety", "coverage", "service"}
    extra_keys = set(per_category_consent.keys()) - _VALID_CONSENT_KEYS
    if extra_keys:
        return _respond(400, {
            "error": f"per_category_consent contains unknown keys: {sorted(extra_keys)}. "
                     "Allowed: safety, coverage, service."
        })
    for cat in ("safety", "coverage", "service"):
        if not isinstance(per_category_consent.get(cat), bool):
            return _respond(400, {
                "error": f"per_category_consent.{cat} must be a boolean"
            })

    # W7: enforce platform enum
    _VALID_PLATFORMS = {"apns-staging", "apns-prod"}

    platform = body.get("platform", "")
    if not platform:
        return _respond(400, {"error": "platform required"})
    if platform not in _VALID_PLATFORMS:
        return _respond(400, {
            "error": f"platform must be one of: {sorted(_VALID_PLATFORMS)}. Got: '{platform}'"
        })

    # Sanitize: only store the three declared consent keys (additionalProperties:false)
    per_category_consent = {k: per_category_consent[k] for k in _VALID_CONSENT_KEYS}

    # Owner identity from claims (NEVER from body)
    owner_id = claims.get("sub")
    if not owner_id:
        return _respond(401, {"error": "sub claim required"})

    # Log safe hash prefix only — NEVER log the token value
    token_prefix = _token_log_prefix(device_token)
    log.info(
        "register-device: owner_id=%s platform=%s token_prefix=%s",
        owner_id, platform, token_prefix,
    )

    # 1:1 device (D-SCOPE-1): remove the owner's other device rows BEFORE the
    # SNS call and the upsert, so a failure here leaves no orphan endpoint and
    # no state in which the old row and the new row both exist.
    try:
        deleted = _delete_other_devices(owner_id, device_token)
    except Exception as exc:
        log.error(
            "register-device: could not clear prior devices owner_id=%s token_prefix=%s: %s",
            owner_id, token_prefix, type(exc).__name__,
        )
        return _respond(500, {"error": "failed to register device"})
    if deleted:
        log.info(
            "register-device: replaced %d prior device row(s) owner_id=%s",
            deleted, owner_id,
        )

    # Create/retrieve SNS platform endpoint
    endpoint_arn = _create_platform_endpoint(device_token, owner_id)

    # Upsert the device row in DynamoDB
    now = _now_iso()
    table = _get_ddb().Table(_DEVICES_TABLE)
    item: dict[str, Any] = {
        "owner_id": owner_id,          # PK
        "device_token": device_token,  # SK — stored for endpoint management
        "per_category_consent": per_category_consent,
        "platform": platform,
        "endpoint_arn": endpoint_arn,  # None if SNS skipped
        "registered_at": now,
        "updated_at": now,
    }

    try:
        table.put_item(Item=item)
    except Exception as exc:
        log.error(
            "DynamoDB put_item failed for owner_id=%s token_prefix=%s: %s",
            owner_id, token_prefix, exc,
        )
        return _respond(500, {"error": "failed to register device"})

    # Return only the success indicator — NEVER echo device_token
    return _respond(201, {"success": True})
