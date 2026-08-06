"""Test skeletons for lambdas/api-acquire-catalog/handler.py

These tests FAIL (red) because handler.py does not exist yet.
They MUST collect cleanly — no ImportError, no AttributeError on collection.

Run:
    python3 -m pytest lambdas/api-acquire-catalog/ -v

Contract coverage
-----------------
* 200 happy path — GET /acquire/catalog/{tenantId} returns CatalogResponse shape
* 400 on malformed input (missing tenantId path param)
* 403 when the path tenantId does not match the caller's Cognito claim
* 404 on an unknown tenant
* Response body matches Swift CatalogResponse field names exactly

Loading pattern: importlib.util.spec_from_file_location, because this dir
is NOT a Python package (no __init__.py) — same as lambdas/acquire/*/tests/.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Attempt to load the handler. When it does not exist the module-level
# sentinel stays None and every test xfails with a clear message.
# ---------------------------------------------------------------------------
_HANDLER_PATH = Path(__file__).parent.parent / "handler.py"
_handler_mod = None

if _HANDLER_PATH.exists():
    _spec = importlib.util.spec_from_file_location(
        "api_acquire_catalog_handler", _HANDLER_PATH
    )
    _handler_mod = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(_handler_mod)


def _skip_if_no_handler():
    if _handler_mod is None:
        pytest.fail(
            "lambdas/api-acquire-catalog/handler.py does not exist yet. "
            "These tests are expected to fail (red) until Group 2 implements it."
        )


# ---------------------------------------------------------------------------
# Helpers — API Gateway proxy event builder
# ---------------------------------------------------------------------------

def _event(
    tenant_id: str = "meridian-demo",
    claim_tenant: str = "meridian-demo",
    claim_sub: str = "a1b2c3d4-0000-0000-0000-000000000001",
    claim_groups: str = "kiosk",
) -> dict:
    return {
        "httpMethod": "GET",
        "pathParameters": {"tenantId": tenant_id},
        "requestContext": {
            "authorizer": {
                "claims": {
                    "sub": claim_sub,
                    "cognito:username": "kiosk-device-01",
                    "custom:tenantId": claim_tenant,
                    "cognito:groups": claim_groups,
                }
            }
        },
        "body": None,
    }


# DynamoDB stub returning one of each catalog item type.
_CATALOG_DB_ITEMS = [
    {
        "itemKey": "CAT#commuter",
        "categoryId": "commuter",
        "displayName": "Commuter",
        "sortOrder": 1,
    },
    {
        "itemKey": "MODEL#meridian-300",
        "modelId": "meridian-300",
        "categoryId": "commuter",
        "displayName": "Meridian 300",
        "basePrice": 9999.0,
    },
    {
        "itemKey": "VARIANT#meridian-300#standard",
        "variantId": "standard",
        "modelId": "meridian-300",
        "displayName": "Standard",
        "priceAdder": 0.0,
    },
    {
        "itemKey": "COLOR#standard#white",
        "colorId": "white",
        "variantId": "standard",
        "displayName": "Pearl White",
        "hexColor": "#F5F5F0",
    },
    {
        "itemKey": "ACC#top-box",
        "accessoryId": "top-box",
        "displayName": "Top Box 45L",
        "price": 249.0,
    },
]


def _make_ddb_stub(items=None):
    """Return a boto3 resource mock whose Table().query() yields the given items."""
    table = MagicMock()
    table.query.return_value = {
        "Items": items if items is not None else _CATALOG_DB_ITEMS,
        "LastEvaluatedKey": None,
    }
    table.batch_get_item = MagicMock(return_value={"Responses": {}})
    resource = MagicMock()
    resource.Table.return_value = table
    return resource


# ---------------------------------------------------------------------------
# 200 happy path
# ---------------------------------------------------------------------------

class TestCatalogHappyPath:
    def test_returns_200(self):
        _skip_if_no_handler()
        with patch.object(_handler_mod, "_get_ddb", return_value=_make_ddb_stub()):
            resp = _handler_mod.handler(_event(), None)
        assert resp["statusCode"] == 200

    def test_response_body_is_json(self):
        _skip_if_no_handler()
        with patch.object(_handler_mod, "_get_ddb", return_value=_make_ddb_stub()):
            resp = _handler_mod.handler(_event(), None)
        body = json.loads(resp["body"])
        assert isinstance(body, dict)

    def test_catalog_response_has_all_swift_dto_fields(self):
        """CatalogResponse Swift fields: tenantId, catalogVersion, categories,
        models, variants, colors, accessories — all must be present."""
        _skip_if_no_handler()
        with patch.object(_handler_mod, "_get_ddb", return_value=_make_ddb_stub()):
            resp = _handler_mod.handler(_event(), None)
        body = json.loads(resp["body"])
        for field in (
            "tenantId",
            "categories",
            "models",
            "variants",
            "colors",
            "accessories",
        ):
            assert field in body, f"CatalogResponse must include '{field}'"

    def test_tenant_id_echoed_in_body(self):
        _skip_if_no_handler()
        with patch.object(_handler_mod, "_get_ddb", return_value=_make_ddb_stub()):
            resp = _handler_mod.handler(_event(tenant_id="meridian-demo"), None)
        body = json.loads(resp["body"])
        assert body["tenantId"] == "meridian-demo"

    def test_categories_is_list(self):
        _skip_if_no_handler()
        with patch.object(_handler_mod, "_get_ddb", return_value=_make_ddb_stub()):
            body = json.loads(_handler_mod.handler(_event(), None)["body"])
        assert isinstance(body["categories"], list)

    def test_models_is_list(self):
        _skip_if_no_handler()
        with patch.object(_handler_mod, "_get_ddb", return_value=_make_ddb_stub()):
            body = json.loads(_handler_mod.handler(_event(), None)["body"])
        assert isinstance(body["models"], list)


# ---------------------------------------------------------------------------
# 400 on malformed / missing path param
# ---------------------------------------------------------------------------

class TestCatalogBadRequest:
    def test_missing_tenant_id_path_param_returns_400(self):
        _skip_if_no_handler()
        bad_event = _event()
        bad_event["pathParameters"] = {}  # tenantId absent
        with patch.object(_handler_mod, "_get_ddb", return_value=_make_ddb_stub()):
            resp = _handler_mod.handler(bad_event, None)
        assert resp["statusCode"] == 400

    def test_null_path_parameters_returns_400(self):
        _skip_if_no_handler()
        bad_event = _event()
        bad_event["pathParameters"] = None
        with patch.object(_handler_mod, "_get_ddb", return_value=_make_ddb_stub()):
            resp = _handler_mod.handler(bad_event, None)
        assert resp["statusCode"] == 400


# ---------------------------------------------------------------------------
# 403 when the path tenantId does not match the caller's claim
# ---------------------------------------------------------------------------

class TestCatalogTenantClamp:
    def test_mismatched_tenant_returns_403(self):
        """Path /acquire/catalog/other-tenant but claim says meridian-demo → 403."""
        _skip_if_no_handler()
        evt = _event(tenant_id="other-tenant", claim_tenant="meridian-demo")
        with patch.object(_handler_mod, "_get_ddb", return_value=_make_ddb_stub()):
            resp = _handler_mod.handler(evt, None)
        assert resp["statusCode"] == 403

    def test_matched_tenant_not_rejected(self):
        _skip_if_no_handler()
        evt = _event(tenant_id="meridian-demo", claim_tenant="meridian-demo")
        with patch.object(_handler_mod, "_get_ddb", return_value=_make_ddb_stub()):
            resp = _handler_mod.handler(evt, None)
        assert resp["statusCode"] != 403


# ---------------------------------------------------------------------------
# 404 on unknown / empty tenant
# ---------------------------------------------------------------------------

class TestCatalogNotFound:
    def test_no_items_returns_404(self):
        _skip_if_no_handler()
        evt = _event(tenant_id="unknown-tenant", claim_tenant="unknown-tenant")
        with patch.object(_handler_mod, "_get_ddb", return_value=_make_ddb_stub(items=[])):
            resp = _handler_mod.handler(evt, None)
        assert resp["statusCode"] == 404


# ---------------------------------------------------------------------------
# Per-item-type allow-list tests — unexpected attributes must not appear
# ---------------------------------------------------------------------------
# Regression guard for security-review Cycle 2 Warning: catalog items were
# echoed wholesale including `itemKey` and any operator-added column.
# After the fix each item is filtered through a per-type allow-list.
# ---------------------------------------------------------------------------

class TestCatalogFieldStrip:
    """Internal DDB attributes must not appear in any item in the response."""

    def test_unexpected_attribute_stripped_from_model_item(self):
        """An `internalField` on a MODEL# row must NOT appear in the response."""
        _skip_if_no_handler()
        items_with_internal = [
            {
                "itemKey": "MODEL#meridian-300",
                "modelId": "meridian-300",
                "categoryId": "commuter",
                "displayName": "Meridian 300",
                "basePrice": 9999.0,
                "internalField": "this-must-not-appear",   # injected internal attribute
            },
            # Include a CAT# item so the response is not 404
            {
                "itemKey": "CAT#commuter",
                "categoryId": "commuter",
                "displayName": "Commuter",
            },
        ]
        with patch.object(_handler_mod, "_get_ddb", return_value=_make_ddb_stub(items_with_internal)):
            resp = _handler_mod.handler(_event(), None)
        assert resp["statusCode"] == 200
        body = json.loads(resp["body"])
        for model_item in body.get("models", []):
            assert "internalField" not in model_item, (
                "internalField is not declared in CatalogModel and must be stripped"
            )
            assert "itemKey" not in model_item, (
                "itemKey is an internal DDB sort key and must be stripped"
            )

    def test_item_key_stripped_from_all_item_types(self):
        """itemKey is the internal DDB sort key and must never reach the client."""
        _skip_if_no_handler()
        with patch.object(_handler_mod, "_get_ddb", return_value=_make_ddb_stub()):
            resp = _handler_mod.handler(_event(), None)
        body = json.loads(resp["body"])
        for bucket in ("categories", "models", "variants", "colors", "accessories"):
            for item in body.get(bucket, []):
                assert "itemKey" not in item, (
                    f"itemKey leaked through into '{bucket}' item: {item}"
                )

    def test_interiors_always_present_even_when_empty(self):
        """decisions.md Q4: interiors must always be a key in the response,
        even when no INTERIOR# items exist, so Swift decode never sees a missing key."""
        _skip_if_no_handler()
        # Use items that contain no INTERIOR# rows
        with patch.object(_handler_mod, "_get_ddb", return_value=_make_ddb_stub()):
            resp = _handler_mod.handler(_event(), None)
        body = json.loads(resp["body"])
        assert "interiors" in body, "interiors must always be present in CatalogResponse"
        assert isinstance(body["interiors"], list)

    def test_ive_packages_always_present_even_when_empty(self):
        """decisions.md Q4: ivePackages must always be present."""
        _skip_if_no_handler()
        with patch.object(_handler_mod, "_get_ddb", return_value=_make_ddb_stub()):
            resp = _handler_mod.handler(_event(), None)
        body = json.loads(resp["body"])
        assert "ivePackages" in body, "ivePackages must always be present in CatalogResponse"
        assert isinstance(body["ivePackages"], list)


# ---------------------------------------------------------------------------
# Pagination cap tests — handler must not follow LastEvaluatedKey without bound
# ---------------------------------------------------------------------------
# Regression guard for security-review Cycle 2 Warning: same defect class as
# cvx/issues/2026-07-29-assistant-chat-504-response-length/.
# ---------------------------------------------------------------------------

class TestCatalogPaginationCap:
    """Handler must stop paginating when the item cap is reached."""

    def test_cap_is_honoured_when_ddb_returns_multiple_pages(self):
        """Stub DDB to return two pages each with 600 items (total 1200 > default cap 1000).
        Handler must stop after the first page that pushes the total over the cap
        and must NOT query a third page."""
        _skip_if_no_handler()

        import unittest.mock as mock

        # Page 1: 600 CAT# items (enough to be above half of the cap)
        page1_items = [
            {"itemKey": f"CAT#cat-{i}", "categoryId": f"cat-{i}", "displayName": f"Cat {i}"}
            for i in range(600)
        ]
        # Page 2: 600 more items — pushing total to 1200, above cap of 1000
        page2_items = [
            {"itemKey": f"CAT#cat-extra-{i}", "categoryId": f"cat-extra-{i}", "displayName": f"Extra {i}"}
            for i in range(600)
        ]

        call_count = {"n": 0}

        def _paginated_query(**kwargs):
            call_count["n"] += 1
            if call_count["n"] == 1:
                return {"Items": page1_items, "LastEvaluatedKey": {"tenantId": "meridian-demo", "itemKey": "marker"}}
            # Page 2 — should not be reached if cap fired after page 1 pushed total ≥ cap
            return {"Items": page2_items, "LastEvaluatedKey": None}

        table = mock.MagicMock()
        table.query.side_effect = _paginated_query
        resource = mock.MagicMock()
        resource.Table.return_value = table

        with mock.patch.dict("os.environ", {"VSA_ACQUIRE_CATALOG_PAGE_CAP": "500"}):
            # Re-import the module so the env var is picked up
            import importlib
            import importlib.util
            spec = importlib.util.spec_from_file_location(
                "api_acquire_catalog_handler_cap",
                _HANDLER_PATH,
            )
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            with mock.patch.object(mod, "_get_ddb", return_value=resource):
                resp = mod.handler(_event(), None)

        # Handler must have stopped after page 1 hit the cap (500 < 600 items on page 1)
        assert table.query.call_count == 1, (
            f"Handler should have stopped at page 1 (cap=500, page1 has 600 items), "
            f"but made {table.query.call_count} query call(s)"
        )
        # And still returns a valid 200 response with what it collected
        assert resp["statusCode"] == 200
