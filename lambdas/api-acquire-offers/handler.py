"""GET /acquire/offers/{modelId}

Returns promotional offers for a given model.

The handler reads OFFER# rows from the catalog table for the caller's
tenant and the requested modelId. An empty offer list returns 200 with
`{"offers": []}` — this is correct v1 behaviour and prevents the Swift
client from throwing AcquireError.endpointUnavailable on a missing
body field.

The effective tenantId is always clamped to the caller's Cognito claim;
there is no caller-supplied tenantId on this route.

Env vars:
    VSA_ACQUIRE_CATALOG_TABLE  (default: vsa-dev-acquire-catalog)
    AWS_REGION                 (default: us-east-1)
"""
from __future__ import annotations

import json
import logging
import os
from decimal import Decimal
from typing import Any

import boto3
from boto3.dynamodb.conditions import Key

log = logging.getLogger()
log.setLevel(logging.INFO)

_CATALOG_TABLE = os.environ.get("VSA_ACQUIRE_CATALOG_TABLE", "vsa-dev-acquire-catalog")

# Pagination safety cap — stop following LastEvaluatedKey after this many items.
# Same defect class as cvx/issues/2026-07-29-assistant-chat-504-response-length/.
# Default is well above any realistic offer count; configurable for tenants with
# unusually large offer catalogs.
_OFFERS_PAGE_CAP = int(os.environ.get("VSA_ACQUIRE_OFFERS_PAGE_CAP", "500"))

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
            "Access-Control-Allow-Origin": "*",
        },
        "body": json.dumps(body, cls=_DecimalEncoder),
    }


def _query_offers(table, tenant_id: str, model_id: str) -> list[dict]:
    """Query OFFER#<modelId># items for the given tenant, handling pagination.

    Stops following LastEvaluatedKey once the total item count exceeds
    _OFFERS_PAGE_CAP.  Logs loudly if the cap is hit.
    """
    prefix = f"OFFER#{model_id}#"
    items: list[dict] = []
    kwargs: dict[str, Any] = {
        "KeyConditionExpression": (
            Key("tenantId").eq(tenant_id) & Key("itemKey").begins_with(prefix)
        ),
    }
    while True:
        result = table.query(**kwargs)
        items.extend(result.get("Items", []))
        if len(items) >= _OFFERS_PAGE_CAP:
            log.warning(
                "api-acquire-offers: pagination cap reached for tenantId=%s modelId=%s "
                "(cap=%d, items_so_far=%d) — stopping pagination",
                tenant_id, model_id, _OFFERS_PAGE_CAP, len(items),
            )
            break
        last = result.get("LastEvaluatedKey")
        if not last:
            break
        kwargs["ExclusiveStartKey"] = last
    return items


def handler(event: dict[str, Any], _context: Any) -> dict[str, Any]:
    """Lambda entry point for GET /acquire/offers/{modelId}."""
    # ── 1. Extract and validate path param ────────────────────────────────
    path_params = event.get("pathParameters") or {}
    model_id = path_params.get("modelId") if path_params else None
    if not model_id:
        return _resp(400, {"error": "Missing required path parameter: modelId"})

    # ── 2. Resolve tenantId from claim (no caller-supplied tenantId here) ─
    claims = (
        (event.get("requestContext") or {})
        .get("authorizer", {})
        .get("claims") or {}
    )
    claim_tenant = claims.get("custom:tenantId") or claims.get("tenantId")
    if not claim_tenant:
        return _resp(403, {"error": "Missing tenantId claim — access denied"})

    # ── 3. Query OFFER# rows for this tenant and modelId ──────────────────
    try:
        table = _get_ddb().Table(_CATALOG_TABLE)
        raw_offers = _query_offers(table, claim_tenant, model_id)
    except Exception as exc:  # noqa: BLE001
        log.exception(
            "api-acquire-offers: DDB query failed for tenantId=%s modelId=%s: %s",
            claim_tenant, model_id, exc,
        )
        return _resp(500, {"error": "Failed to load offers"})

    # ── 4. Return the offers array (empty is a valid v1 response) ─────────
    offers = [_to_jsonable(item) for item in raw_offers]
    return _resp(200, {"modelId": model_id, "tenantId": claim_tenant, "offers": offers})
