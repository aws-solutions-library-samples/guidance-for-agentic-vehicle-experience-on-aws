"""GET /acquire/catalog/{tenantId}

Returns the full tenant catalog in a single response: categories, models,
variants, colors, accessories, interiors, and IVE packages.

Strategy:
1. Verify that the path tenantId matches the caller's Cognito claim.
2. Query the catalog table with the tenantId partition key to retrieve all
   items. (For production scale, this would use the by-category GSI +
   BatchGetItem for models/variants/colors; for kiosk-scale catalogs a
   full-partition query is sufficient and avoids N+1 round trips.)
3. Additionally query for INTERIOR# and IVE# items to populate the two new
   top-level arrays introduced in decisions.md Q4.
4. Sort items into their respective arrays and return the CatalogResponse
   shape the Swift client expects.

decisions.md Q4: catalog response gains `interiors` and `ivePackages` as
new top-level arrays.

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

# Pagination safety cap — stop following LastEvaluatedKey after accumulating
# this many items.  Set well above Meridian's ~98 items so the cap never fires
# on the demo tenant; it protects against runaway multi-tenant queries.
# Configurable via VSA_ACQUIRE_CATALOG_PAGE_CAP so operators can tune without
# a code change.
_CATALOG_PAGE_CAP = int(os.environ.get("VSA_ACQUIRE_CATALOG_PAGE_CAP", "1000"))

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
    """Recursively convert Decimal → int/float for JSON serialisation."""
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


def _query_all_items(table, tenant_id: str) -> list[dict]:
    """Query all catalog items for the given tenant, handling pagination.

    Stops following LastEvaluatedKey once the total item count exceeds
    _CATALOG_PAGE_CAP.  Logs loudly if the cap is hit — this is a latent-
    tenant guard, not a demo constraint, and should never fire for Meridian.
    """
    items: list[dict] = []
    kwargs: dict[str, Any] = {
        "KeyConditionExpression": Key("tenantId").eq(tenant_id),
    }
    while True:
        result = table.query(**kwargs)
        items.extend(result.get("Items", []))
        if len(items) >= _CATALOG_PAGE_CAP:
            log.warning(
                "api-acquire-catalog: pagination cap reached for tenantId=%s "
                "(cap=%d, items_so_far=%d) — stopping pagination",
                tenant_id, _CATALOG_PAGE_CAP, len(items),
            )
            break
        last = result.get("LastEvaluatedKey")
        if not last:
            break
        kwargs["ExclusiveStartKey"] = last
    return items


def _classify(items: list[dict]) -> dict[str, list[dict]]:
    """Sort raw DDB items into the CatalogResponse bucket lists.

    Each item is filtered through an explicit allow-list matching its
    corresponding Swift Catalog* struct.  Internal DDB attributes (itemKey,
    tenantId at the item level, any operator-added column) are stripped.
    """
    # Per-item-type allow-lists (Swift struct field names only).
    # Sourced from AcquireCatalogClient.swift and acquire-catalog-schema.md.
    _CAT_FIELDS = frozenset({
        "categoryId", "displayName", "symbolName", "sortOrder",
    })
    _MODEL_FIELDS = frozenset({
        "modelId", "categoryId", "displayName", "basePrice",
        "imageKey", "specBadges", "sortOrder",
    })
    _VARIANT_FIELDS = frozenset({
        "variantId", "modelId", "displayName", "priceAdder", "sortOrder",
    })
    _COLOR_FIELDS = frozenset({
        "colorId", "variantId", "displayName", "hexColor", "imageKey", "priceAdder",
    })
    _ACC_FIELDS = frozenset({
        "accessoryId", "displayName", "price", "categoryId", "imageKey", "sortOrder",
    })
    # Interior and IVE schemas from acquire-catalog-schema.md (Zone 2 addition).
    # Swift structs are carry-forward for Zone 2 iPad target (decisions.md Q4);
    # these allow-lists match the doc-defined field set to avoid pre-emptive leakage.
    _INTERIOR_FIELDS = frozenset({
        "modelId", "interiorId", "displayName", "description",
        "materialLabel", "colorLabel", "hexSwatch", "imageKey",
        "priceDelta", "displayOrder",
    })
    _IVE_FIELDS = frozenset({
        "packageId", "displayName", "description",
        "features", "priceDelta", "displayOrder",
    })

    categories: list[dict] = []
    models: list[dict] = []
    variants: list[dict] = []
    colors: list[dict] = []
    accessories: list[dict] = []
    interiors: list[dict] = []
    ive_packages: list[dict] = []

    for raw in items:
        item = _to_jsonable(raw)
        key: str = item.get("itemKey", "")
        if key.startswith("CAT#"):
            categories.append({k: v for k, v in item.items() if k in _CAT_FIELDS})
        elif key.startswith("MODEL#"):
            models.append({k: v for k, v in item.items() if k in _MODEL_FIELDS})
        elif key.startswith("VAR#"):
            variants.append({k: v for k, v in item.items() if k in _VARIANT_FIELDS})
        elif key.startswith("COLOR#"):
            colors.append({k: v for k, v in item.items() if k in _COLOR_FIELDS})
        elif key.startswith("ACC#") and not key.startswith("ACC-COMPAT#"):
            accessories.append({k: v for k, v in item.items() if k in _ACC_FIELDS})
        elif key.startswith("INTERIOR#"):
            interiors.append({k: v for k, v in item.items() if k in _INTERIOR_FIELDS})
        elif key.startswith("IVE#"):
            ive_packages.append({k: v for k, v in item.items() if k in _IVE_FIELDS})
        # Skip META#, ACC-COMPAT#, OFFER# etc.

    return {
        "categories": categories,
        "models": models,
        "variants": variants,
        "colors": colors,
        "accessories": accessories,
        "interiors": interiors,
        "ivePackages": ive_packages,
    }


def handler(event: dict[str, Any], _context: Any) -> dict[str, Any]:
    """Lambda entry point for GET /acquire/catalog/{tenantId}."""
    # ── 1. Extract and validate path param ────────────────────────────────
    path_params = event.get("pathParameters") or {}
    tenant_id = path_params.get("tenantId") if path_params else None
    if not tenant_id:
        return _resp(400, {"error": "Missing required path parameter: tenantId"})

    # ── 2. Extract claim and enforce tenant clamp ──────────────────────────
    claims = (
        (event.get("requestContext") or {})
        .get("authorizer", {})
        .get("claims") or {}
    )
    claim_tenant = claims.get("custom:tenantId") or claims.get("tenantId")
    if not claim_tenant or tenant_id != claim_tenant:
        return _resp(403, {"error": "Tenant mismatch — access denied"})

    # ── 3. Query all catalog items for this tenant ─────────────────────────
    try:
        table = _get_ddb().Table(_CATALOG_TABLE)
        all_items = _query_all_items(table, tenant_id)
    except Exception as exc:  # noqa: BLE001
        log.exception("api-acquire-catalog: DDB query failed for tenantId=%s: %s", tenant_id, exc)
        return _resp(500, {"error": "Failed to load catalog"})

    if not all_items:
        return _resp(404, {"error": f"No catalog found for tenant '{tenant_id}'"})

    # ── 4. Classify items and return CatalogResponse ───────────────────────
    classified = _classify(all_items)

    body = {
        "tenantId": tenant_id,
        **classified,
    }
    return _resp(200, body)
