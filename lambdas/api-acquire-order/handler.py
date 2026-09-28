"""GET /acquire/orders/{orderId}

Returns a single order record for a verified caller.

Auth requirements (decisions.md Q8 and Q5):
  1. Tenant clamp: order.tenantId must match the caller's claim tenantId.
  2. Owner match: order.ownerSub must match the caller's claim sub.
     Tenant-match alone is NOT sufficient — every kiosk caller on the
     show floor shares the same tenant, so only the ownerSub distinguishes
     one visitor's order from another's.

Wire-name mapping (decisions.md Q5):
  DDB attribute  → Response wire name
  stage          → currentStage
  stageHistory   → stages
  Both Swift fields are non-optional; a wrong name decodes as nil and
  breaks the Beat 5 order tracker.

Only the fields the Swift AcquireOrder DTO declares are returned.
Internal DDB attributes (tenantOwnerKey, ownerSub, etc.) are never echoed.

Env vars:
    VSA_ACQUIRE_ORDERS_TABLE  (default: vsa-dev-acquire-orders)
    AWS_REGION                (default: us-east-1)
"""
from __future__ import annotations

import json
import logging
import os
from decimal import Decimal
from typing import Any

import boto3

log = logging.getLogger()
log.setLevel(logging.INFO)

_ORDERS_TABLE = os.environ.get("VSA_ACQUIRE_ORDERS_TABLE", "vsa-dev-acquire-orders")

_ddb = None


def _get_ddb():
    global _ddb
    if _ddb is None:
        _ddb = boto3.resource("dynamodb", region_name=os.environ.get("AWS_REGION", "us-east-1"))
    return _ddb


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
            "Access-Control-Allow-Origin": os.environ.get("ALLOWED_ORIGIN", "*"),
        },
        "body": json.dumps(body, cls=_DecimalEncoder),
    }


# Fields the Swift AcquireOrder DTO declares (decisions.md Q5).
# This frozenset is the single source of truth for what leaves the handler.
# Internal-only DDB attributes (tenantOwnerKey, ownerSub, stage, stageHistory,
# configuration, correlation, financing, nextTransitionAt, orderPlacedAt,
# payment, plantId) must never appear in the response.
#
# Note: the Q5 renames (stage→currentStage, stageHistory→stages) are handled
# explicitly in _build_response_body() before the pass-through loop.
_CLIENT_FIELDS = frozenset({
    "orderId",
    "tenantId",
    "currentStage",      # remapped from DDB `stage` in _build_response_body
    "stages",            # remapped from DDB `stageHistory` in _build_response_body
    "modelId",
    "variantId",
    "colorId",
    "selectedAccessoryIds",
    "reservationDepositRef",
    "dealerRef",
    "vinAssigned",
    "createdAt",
    "updatedAt",
})


def _build_response_body(item: dict) -> dict:
    """Map a raw DDB order item to the AcquireOrder wire shape.

    decisions.md Q5:
        DDB `stage`        → wire `currentStage`
        DDB `stageHistory` → wire `stages`

    Only fields in _CLIENT_FIELDS are returned.  This is the single allow-list
    that prevents internal DDB attributes (configuration, correlation, financing,
    nextTransitionAt, orderPlacedAt, payment, plantId, etc.) from leaking to
    the client.
    """
    raw = _to_jsonable(item)

    body: dict[str, Any] = {}

    # Q5 renames: applied before the pass-through loop so the renamed keys
    # end up in the response under their wire names.
    if "stage" in raw:
        body["currentStage"] = raw["stage"]
    body["stages"] = raw.get("stageHistory", [])

    # Pass-through every field that is in _CLIENT_FIELDS, excluding the two
    # renamed keys (already handled above) and any internal-only attribute.
    _RENAME_SOURCES = {"stage", "stageHistory"}
    for field, value in raw.items():
        if field in _CLIENT_FIELDS and field not in _RENAME_SOURCES and field not in ("currentStage", "stages"):
            body[field] = value

    return body


def handler(event: dict[str, Any], _context: Any) -> dict[str, Any]:
    """Lambda entry point for GET /acquire/orders/{orderId}."""
    # ── 1. Extract and validate path param ────────────────────────────────
    path_params = event.get("pathParameters") or {}
    order_id = path_params.get("orderId") if path_params else None
    if not order_id:
        return _resp(400, {"error": "Missing required path parameter: orderId"})

    # ── 2. Extract caller claims ───────────────────────────────────────────
    claims = (
        (event.get("requestContext") or {})
        .get("authorizer", {})
        .get("claims") or {}
    )
    claim_tenant = claims.get("custom:tenantId") or claims.get("tenantId")
    claim_sub = claims.get("sub")

    # ── 3. Fetch the order ─────────────────────────────────────────────────
    try:
        table = _get_ddb().Table(_ORDERS_TABLE)
        result = table.get_item(Key={"orderId": order_id})
    except Exception as exc:  # noqa: BLE001
        log.exception("api-acquire-order: DDB GetItem failed for orderId=%s: %s", order_id, exc)
        return _resp(500, {"error": "Failed to retrieve order"})

    item = result.get("Item")
    if not item:
        return _resp(404, {"error": f"Order '{order_id}' not found"})

    # ── 4. Tenant clamp (Q8 first layer) ──────────────────────────────────
    order_tenant = item.get("tenantId")
    if not claim_tenant or order_tenant != claim_tenant:
        return _resp(403, {"error": "Tenant mismatch — access denied"})

    # ── 5. Owner match (Q8 second layer, mandatory per decisions.md Q8) ───
    # Every kiosk visitor on the show floor shares a tenant, so tenant-match
    # alone does NOT prevent one visitor reading another's order.
    # If the order carries an ownerSub, the caller MUST supply a matching sub.
    # Denying on missing claim_sub closes the bypass path where a service token
    # or misconfigured authorizer omits sub and the triple-and guard is skipped.
    # Orders without ownerSub (legacy / non-kiosk) remain readable — they have
    # no isolation requirement.
    order_owner_sub = item.get("ownerSub")
    if order_owner_sub:
        if not claim_sub or order_owner_sub != claim_sub:
            log.warning(
                "api-acquire-order: owner-match denied for orderId=%s "
                "claim_sub=%s order_ownerSub=%s",
                order_id, claim_sub, order_owner_sub,
            )
            return _resp(403, {"error": "Order does not belong to this caller"})

    # ── 6. Build and return the response ──────────────────────────────────
    response_body = _build_response_body(item)
    return _resp(200, response_body)
