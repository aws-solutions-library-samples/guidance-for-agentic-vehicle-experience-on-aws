"""Test skeletons for lambdas/api-acquire-offers/handler.py

These tests FAIL (red) because handler.py does not exist yet.
They MUST collect cleanly — no ImportError, no AttributeError on collection.

Run:
    python3 -m pytest lambdas/api-acquire-offers/ -v

Contract coverage
-----------------
* 200 happy path — GET /acquire/offers/{modelId} returns offer list (may be empty)
* 400 on malformed input (missing modelId path param)
* 403 when the effective tenantId from the claim is absent or mismatched
* 404 when no OFFER# rows exist for the modelId (empty is 404 or empty list — both valid)
* Response body structure consistent with spec (array of offers)

Loading pattern: importlib.util.spec_from_file_location (no __init__.py).
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Attempt to load the handler.
# ---------------------------------------------------------------------------
_HANDLER_PATH = Path(__file__).parent.parent / "handler.py"
_handler_mod = None

if _HANDLER_PATH.exists():
    _spec = importlib.util.spec_from_file_location(
        "api_acquire_offers_handler", _HANDLER_PATH
    )
    _handler_mod = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(_handler_mod)


def _skip_if_no_handler():
    if _handler_mod is None:
        pytest.fail(
            "lambdas/api-acquire-offers/handler.py does not exist yet. "
            "These tests are expected to fail (red) until Group 2 implements it."
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _event(
    model_id: str = "meridian-300",
    claim_tenant: str = "meridian-demo",
    claim_sub: str = "a1b2c3d4-0000-0000-0000-000000000001",
    claim_groups: str = "kiosk",
) -> dict:
    return {
        "httpMethod": "GET",
        "pathParameters": {"modelId": model_id},
        "requestContext": {
            "authorizer": {
                "claims": {
                    "sub": claim_sub,
                    "custom:tenantId": claim_tenant,
                    "cognito:groups": claim_groups,
                }
            }
        },
        "queryStringParameters": None,
        "body": None,
    }


_OFFER_ITEMS = [
    {
        "itemKey": "OFFER#meridian-300#spring2026",
        "modelId": "meridian-300",
        "tenantId": "meridian-demo",
        "offerId": "spring2026",
        "displayName": "Spring 2026 Launch Special",
        "discountAmount": 500.0,
        "expiresAt": "2026-09-30T00:00:00+00:00",
    },
]


def _make_ddb_with_offers(items=None):
    table = MagicMock()
    table.query.return_value = {
        "Items": items if items is not None else _OFFER_ITEMS,
        "LastEvaluatedKey": None,
    }
    resource = MagicMock()
    resource.Table.return_value = table
    return resource


# ---------------------------------------------------------------------------
# 200 happy path
# ---------------------------------------------------------------------------

class TestOffersHappyPath:
    def test_returns_200(self):
        _skip_if_no_handler()
        with patch.object(_handler_mod, "_get_ddb", return_value=_make_ddb_with_offers()):
            resp = _handler_mod.handler(_event(), None)
        assert resp["statusCode"] == 200

    def test_response_body_is_json(self):
        _skip_if_no_handler()
        with patch.object(_handler_mod, "_get_ddb", return_value=_make_ddb_with_offers()):
            resp = _handler_mod.handler(_event(), None)
        body = json.loads(resp["body"])
        assert isinstance(body, dict)

    def test_response_contains_offers_list(self):
        _skip_if_no_handler()
        with patch.object(_handler_mod, "_get_ddb", return_value=_make_ddb_with_offers()):
            body = json.loads(_handler_mod.handler(_event(), None)["body"])
        # The response must carry an "offers" key (even if empty in v1)
        assert "offers" in body, "Response must include an 'offers' array"
        assert isinstance(body["offers"], list)

    def test_empty_offers_returns_200_not_404(self):
        """v1 ships with zero offers — handler must return 200 with an empty list,
        not a 404, so the Swift client does not throw AcquireError.endpointUnavailable."""
        _skip_if_no_handler()
        with patch.object(_handler_mod, "_get_ddb", return_value=_make_ddb_with_offers(items=[])):
            resp = _handler_mod.handler(_event(), None)
        assert resp["statusCode"] == 200
        body = json.loads(resp["body"])
        assert body.get("offers") == []


# ---------------------------------------------------------------------------
# 400 on malformed / missing path param
# ---------------------------------------------------------------------------

class TestOffersBadRequest:
    def test_missing_model_id_returns_400(self):
        _skip_if_no_handler()
        bad_event = _event()
        bad_event["pathParameters"] = {}
        with patch.object(_handler_mod, "_get_ddb", return_value=_make_ddb_with_offers()):
            resp = _handler_mod.handler(bad_event, None)
        assert resp["statusCode"] == 400

    def test_null_path_parameters_returns_400(self):
        _skip_if_no_handler()
        bad_event = _event()
        bad_event["pathParameters"] = None
        with patch.object(_handler_mod, "_get_ddb", return_value=_make_ddb_with_offers()):
            resp = _handler_mod.handler(bad_event, None)
        assert resp["statusCode"] == 400


# ---------------------------------------------------------------------------
# 403 when the claim does not carry a tenantId
# ---------------------------------------------------------------------------

class TestOffersTenantClamp:
    def test_no_tenant_claim_returns_403(self):
        """A claim without custom:tenantId cannot resolve an authorized tenant → 403."""
        _skip_if_no_handler()
        evt = _event()
        # Remove tenantId from claims
        del evt["requestContext"]["authorizer"]["claims"]["custom:tenantId"]
        with patch.object(_handler_mod, "_get_ddb", return_value=_make_ddb_with_offers()):
            resp = _handler_mod.handler(evt, None)
        assert resp["statusCode"] == 403


# ---------------------------------------------------------------------------
# Pagination cap tests — handler must not follow LastEvaluatedKey without bound
# ---------------------------------------------------------------------------

class TestOffersPaginationCap:
    """Handler must stop paginating when the item cap is reached."""

    def test_cap_is_honoured_when_ddb_returns_multiple_pages(self):
        """Stub DDB to return a page that exceeds the cap.  Handler must stop
        after that page and not request further pages."""
        _skip_if_no_handler()

        import unittest.mock as mock

        page1_items = [
            {
                "itemKey": f"OFFER#meridian-300#offer-{i}",
                "offerId": f"offer-{i}",
                "displayName": f"Offer {i}",
            }
            for i in range(300)
        ]
        page2_items = [
            {
                "itemKey": f"OFFER#meridian-300#extra-{i}",
                "offerId": f"extra-{i}",
                "displayName": f"Extra {i}",
            }
            for i in range(300)
        ]

        call_count = {"n": 0}

        def _paginated_query(**kwargs):
            call_count["n"] += 1
            if call_count["n"] == 1:
                return {"Items": page1_items, "LastEvaluatedKey": {"tenantId": "meridian-demo", "itemKey": "marker"}}
            return {"Items": page2_items, "LastEvaluatedKey": None}

        table = mock.MagicMock()
        table.query.side_effect = _paginated_query
        resource = mock.MagicMock()
        resource.Table.return_value = table

        with mock.patch.dict("os.environ", {"VSA_ACQUIRE_OFFERS_PAGE_CAP": "200"}):
            import importlib.util
            spec = importlib.util.spec_from_file_location(
                "api_acquire_offers_handler_cap",
                _HANDLER_PATH,
            )
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            with mock.patch.object(mod, "_get_ddb", return_value=resource):
                resp = mod.handler(_event(), None)

        assert table.query.call_count == 1, (
            f"Handler should have stopped at page 1 (cap=200 < 300 items), "
            f"but made {table.query.call_count} query call(s)"
        )
        assert resp["statusCode"] == 200
        body = json.loads(resp["body"])
        assert "offers" in body
