"""API: GET /tenants/{tenantId}/config

Returns the active tenant config, stripped of internal infrastructure fields
the client has no business seeing (ARNs, queue IDs, secrets references).
"""
from __future__ import annotations

import json
import os
from decimal import Decimal
from typing import Any

import boto3

_ddb = boto3.resource("dynamodb", region_name=os.environ.get("AWS_REGION", "us-east-1"))
_table = _ddb.Table(os.environ["TENANT_CONFIG_TABLE"])

# Fields safe to expose to the companion app. Everything else is internal.
_PUBLIC_FIELDS = {
    "tenantId", "version", "status", "displayName", "segment",
    "branding", "voice", "features",
    # `acquire` added 2026-07-29. The iOS client has carried
    # `TenantConfig.acquire: AcquireConfig?` since the Acquire spec
    # (2026-08-04-cvx-oem-discover-order-meridian) but this allowlist was never
    # widened to match, so the field was ALWAYS absent from the response and
    # every tenant-configurable Acquire/Discover value — currency symbol,
    # category label, Discover greetings and primed prompts, plants, delivery
    # cadence, upgrade offers — silently fell back to hardcoded Swift
    # defaults. Silent because an absent optional decodes fine.
    #
    # Safe to expose: the handler already enforces
    # `path_tenant == caller_tenant`, so this is the caller's own tenant
    # config, and the block holds presentation and catalog-shaping data that
    # the client must render anyway. Do NOT put credentials, private lender
    # keys, or internal-only pricing logic under `acquire` — this field is
    # now client-visible.
    "acquire",
    # `assistantTitle` added 2026-07-29. Optional override for the Assistant
    # surface title; absent means the client uses its segment default. Presentation
    # only — no authorisation significance.
    "assistantTitle",
    # "km" | "mi" for distance presentation. Presentation only.
    "distanceUnit",
}


def _to_jsonable(obj: Any) -> Any:
    if isinstance(obj, Decimal):
        return float(obj) if obj % 1 else int(obj)
    if isinstance(obj, dict):
        return {k: _to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_to_jsonable(v) for v in obj]
    return obj


def _response(status: int, body: dict[str, Any]) -> dict[str, Any]:
    return {
        "statusCode": status,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*",
        },
        "body": json.dumps(body),
    }


def handler(event: dict[str, Any], _context: Any) -> dict[str, Any]:
    claims = (event.get("requestContext") or {}).get("authorizer", {}).get("claims") or {}
    caller_tenant = claims.get("custom:tenantId") or claims.get("tenantId")
    path_tenant = (event.get("pathParameters") or {}).get("tenantId")

    if not caller_tenant:
        return _response(401, {"error": "missing tenantId claim"})
    if path_tenant != caller_tenant:
        return _response(403, {"error": "tenant mismatch"})

    pointer = _table.get_item(Key={"tenantId": caller_tenant, "version": "current"}).get("Item")
    if not pointer:
        return _response(404, {"error": "tenant not found"})
    item = _table.get_item(
        Key={"tenantId": caller_tenant, "version": pointer["activeVersion"]}
    ).get("Item")
    if not item:
        return _response(404, {"error": "active config missing"})

    public = {k: _to_jsonable(v) for k, v in item.items() if k in _PUBLIC_FIELDS}
    return _response(200, public)
