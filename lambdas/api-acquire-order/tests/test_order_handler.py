"""Test skeletons for lambdas/api-acquire-order/handler.py

These tests FAIL (red) because handler.py does not exist yet.
They MUST collect cleanly — no ImportError, no AttributeError on collection.

Run:
    python3 -m pytest lambdas/api-acquire-order/ -v

Contract coverage
-----------------
* 200 happy path — GET /acquire/orders/{orderId} returns AcquireOrder shape
* 400 on malformed input (missing orderId path param)
* 403 when the order's tenantId does not match the caller's Cognito claim
* 404 on a missing / unknown orderId
* Response body matches Swift AcquireOrder field names exactly

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
        "api_acquire_order_handler", _HANDLER_PATH
    )
    _handler_mod = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(_handler_mod)


def _skip_if_no_handler():
    if _handler_mod is None:
        pytest.fail(
            "lambdas/api-acquire-order/handler.py does not exist yet. "
            "These tests are expected to fail (red) until Group 2 implements it."
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _event(
    order_id: str = "ORD-20260731-a1b2c3",
    claim_tenant: str = "meridian-demo",
    claim_sub: str = "a1b2c3d4-0000-0000-0000-000000000001",
    claim_groups: str = "kiosk",
) -> dict:
    return {
        "httpMethod": "GET",
        "pathParameters": {"orderId": order_id},
        "requestContext": {
            "authorizer": {
                "claims": {
                    "sub": claim_sub,
                    "custom:tenantId": claim_tenant,
                    "cognito:groups": claim_groups,
                }
            }
        },
        "body": None,
    }


# Minimal DDB order item for the correct tenant.
# ownerSub is included so the happy-path suite exercises the owner-match guard
# (the claim_sub in _event() defaults to the same value).
_ORDER_ITEM = {
    "orderId": "ORD-20260731-a1b2c3",
    "tenantId": "meridian-demo",
    "ownerSub": "a1b2c3d4-0000-0000-0000-000000000001",   # matches _event() default claim_sub
    "stage": "orderPlaced",
    "stageHistory": [
        {"stage": "orderPlaced", "enteredAt": "2026-07-31T11:00:00+00:00"}
    ],
    "nextTransitionAt": "2026-07-31T11:00:12+00:00",
    "configuration": {
        "modelId": "meridian-300",
        "variantId": "standard",
        "colorId": "white",
    },
}


def _make_ddb_found(item=None):
    table = MagicMock()
    table.get_item.return_value = {"Item": item if item is not None else _ORDER_ITEM}
    resource = MagicMock()
    resource.Table.return_value = table
    return resource


def _make_ddb_not_found():
    table = MagicMock()
    table.get_item.return_value = {}  # No "Item" key → not found
    resource = MagicMock()
    resource.Table.return_value = table
    return resource


# ---------------------------------------------------------------------------
# 200 happy path
# ---------------------------------------------------------------------------

class TestOrderHappyPath:
    def test_returns_200(self):
        _skip_if_no_handler()
        with patch.object(_handler_mod, "_get_ddb", return_value=_make_ddb_found()):
            resp = _handler_mod.handler(_event(), None)
        assert resp["statusCode"] == 200

    def test_response_body_is_json(self):
        _skip_if_no_handler()
        with patch.object(_handler_mod, "_get_ddb", return_value=_make_ddb_found()):
            resp = _handler_mod.handler(_event(), None)
        body = json.loads(resp["body"])
        assert isinstance(body, dict)

    def test_order_response_has_all_swift_dto_fields(self):
        """AcquireOrder Swift fields: orderId, tenantId, currentStage, stages.
        Optional fields (modelId, variantId, colorId, selectedAccessoryIds,
        reservationDepositRef, dealerRef, vinAssigned, createdAt, updatedAt)
        may be absent but orderId/tenantId/currentStage/stages must be present.
        """
        _skip_if_no_handler()
        with patch.object(_handler_mod, "_get_ddb", return_value=_make_ddb_found()):
            resp = _handler_mod.handler(_event(), None)
        body = json.loads(resp["body"])
        for required_field in ("orderId", "tenantId", "currentStage", "stages"):
            assert required_field in body, (
                f"AcquireOrder response must include '{required_field}'"
            )

    def test_current_stage_matches_ddb_stage(self):
        _skip_if_no_handler()
        with patch.object(_handler_mod, "_get_ddb", return_value=_make_ddb_found()):
            body = json.loads(_handler_mod.handler(_event(), None)["body"])
        assert body["currentStage"] == "orderPlaced"

    def test_stages_is_list(self):
        _skip_if_no_handler()
        with patch.object(_handler_mod, "_get_ddb", return_value=_make_ddb_found()):
            body = json.loads(_handler_mod.handler(_event(), None)["body"])
        assert isinstance(body["stages"], list)

    def test_does_not_echo_raw_ddb_fields(self):
        """Handler must not leak internal fields (e.g. tenantOwnerKey) to the client."""
        _skip_if_no_handler()
        internal_item = {
            **_ORDER_ITEM,
            "tenantOwnerKey": "meridian-demo#a1b2c3d4",
            "ownerSub": "a1b2c3d4-0000-0000-0000-000000000001",
        }
        with patch.object(_handler_mod, "_get_ddb", return_value=_make_ddb_found(internal_item)):
            body = json.loads(_handler_mod.handler(_event(), None)["body"])
        assert "tenantOwnerKey" not in body, (
            "tenantOwnerKey is an internal DDB key and must not be in the response"
        )
        assert "ownerSub" not in body, (
            "ownerSub is PII and must not be in the response"
        )


# ---------------------------------------------------------------------------
# 400 on malformed / missing path param
# ---------------------------------------------------------------------------

class TestOrderBadRequest:
    def test_missing_order_id_path_param_returns_400(self):
        _skip_if_no_handler()
        bad_event = _event()
        bad_event["pathParameters"] = {}
        with patch.object(_handler_mod, "_get_ddb", return_value=_make_ddb_found()):
            resp = _handler_mod.handler(bad_event, None)
        assert resp["statusCode"] == 400

    def test_null_path_parameters_returns_400(self):
        _skip_if_no_handler()
        bad_event = _event()
        bad_event["pathParameters"] = None
        with patch.object(_handler_mod, "_get_ddb", return_value=_make_ddb_found()):
            resp = _handler_mod.handler(bad_event, None)
        assert resp["statusCode"] == 400


# ---------------------------------------------------------------------------
# 403 when the order's tenantId does not match the caller's claim
# ---------------------------------------------------------------------------

class TestOrderTenantClamp:
    def test_order_belonging_to_other_tenant_returns_403(self):
        """Order has tenantId=other-tenant, caller claims meridian-demo → 403."""
        _skip_if_no_handler()
        other_item = {**_ORDER_ITEM, "tenantId": "other-tenant"}
        with patch.object(_handler_mod, "_get_ddb", return_value=_make_ddb_found(other_item)):
            resp = _handler_mod.handler(_event(claim_tenant="meridian-demo"), None)
        assert resp["statusCode"] == 403

    def test_own_order_not_rejected(self):
        _skip_if_no_handler()
        with patch.object(_handler_mod, "_get_ddb", return_value=_make_ddb_found()):
            resp = _handler_mod.handler(_event(claim_tenant="meridian-demo"), None)
        assert resp["statusCode"] != 403


# ---------------------------------------------------------------------------
# 404 on missing order
# ---------------------------------------------------------------------------

class TestOrderNotFound:
    def test_unknown_order_id_returns_404(self):
        _skip_if_no_handler()
        with patch.object(_handler_mod, "_get_ddb", return_value=_make_ddb_not_found()):
            resp = _handler_mod.handler(_event(), None)
        assert resp["statusCode"] == 404


# ---------------------------------------------------------------------------
# Field-strip tests — response must contain only Swift-declared fields
# ---------------------------------------------------------------------------
# Regression guard for security-review Cycle 2 Warning: seven fields
# (configuration, correlation, financing, nextTransitionAt, orderPlacedAt,
# payment, plantId) were previously echoed through to the client.
# ---------------------------------------------------------------------------

# The exact set of keys the Swift AcquireOrder struct declares.
_SWIFT_ACQUIRE_ORDER_FIELDS = frozenset({
    "orderId",
    "tenantId",
    "currentStage",
    "stages",
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


def _fully_populated_ddb_item():
    """DDB order item containing every internal attribute that must not reach the client."""
    return {
        "orderId": "ORD-20260731-a1b2c3",
        "tenantId": "meridian-demo",
        "ownerSub": "a1b2c3d4-0000-0000-0000-000000000001",
        "stage": "orderPlaced",
        "stageHistory": [{"stage": "orderPlaced", "enteredAt": "2026-07-31T11:00:00+00:00"}],
        "modelId": "meridian-300",
        "variantId": "standard",
        "colorId": "white",
        "selectedAccessoryIds": [],
        "dealerRef": "DLR-001",
        # Seven fields that MUST be stripped
        "configuration": {"modelId": "meridian-300"},
        "correlation": {"discoverSessionId": "sess-abc"},
        "financing": {"approvedAmount": 12000, "apr": 5.9},
        "nextTransitionAt": "2026-07-31T11:00:12+00:00",
        "orderPlacedAt": "2026-07-31T11:00:00+00:00",
        "payment": {"status": "pending", "transactionRef": "TXN-001"},
        "plantId": "plt-001",
        # Internal DDB key
        "tenantOwnerKey": "meridian-demo#a1b2c3d4",
    }


class TestOrderFieldStrip:
    """Response body must contain only Swift-declared AcquireOrder fields."""

    def test_response_key_set_is_subset_of_swift_declared(self):
        """No key outside _SWIFT_ACQUIRE_ORDER_FIELDS must appear in the response."""
        _skip_if_no_handler()
        with patch.object(_handler_mod, "_get_ddb", return_value=_make_ddb_found(_fully_populated_ddb_item())):
            resp = _handler_mod.handler(_event(), None)
        body = json.loads(resp["body"])
        unexpected = set(body.keys()) - _SWIFT_ACQUIRE_ORDER_FIELDS
        assert not unexpected, (
            f"Response contained fields not declared in Swift AcquireOrder: {unexpected}"
        )

    def test_seven_leaking_fields_are_not_in_response(self):
        """Each of the seven previously-leaking fields must be absent."""
        _skip_if_no_handler()
        with patch.object(_handler_mod, "_get_ddb", return_value=_make_ddb_found(_fully_populated_ddb_item())):
            resp = _handler_mod.handler(_event(), None)
        body = json.loads(resp["body"])
        for field in ("configuration", "correlation", "financing",
                      "nextTransitionAt", "orderPlacedAt", "payment", "plantId"):
            assert field not in body, (
                f"Field '{field}' is not in Swift AcquireOrder and must be stripped"
            )

    def test_financing_and_payment_not_echoed_even_when_populated(self):
        """Defense-in-depth: financing and payment carry real financial data."""
        _skip_if_no_handler()
        item = {
            **_ORDER_ITEM,
            "financing": {"approvedAmount": 9999, "apr": 4.9, "monthly": 199},
            "payment": {"status": "captured", "transactionRef": "TXN-999"},
        }
        with patch.object(_handler_mod, "_get_ddb", return_value=_make_ddb_found(item)):
            resp = _handler_mod.handler(_event(), None)
        body = json.loads(resp["body"])
        assert "financing" not in body, "financing must not reach the wire"
        assert "payment" not in body, "payment must not reach the wire"


# ---------------------------------------------------------------------------
# Q8 owner-match regression tests — bypass paths that MUST be denied
# ---------------------------------------------------------------------------
# Regression guard for the fail-open bypass caught in security-review Cycle 2.
# The original triple-and condition `if order_owner_sub and claim_sub and
# order_owner_sub != claim_sub` skips the guard entirely when either
# order_owner_sub OR claim_sub is falsy, granting the request silently.
#
# Both tests must FAIL against the pre-fix code (which has the triple-and)
# and PASS after the fix (which denies whenever ownerSub is present but the
# caller cannot prove a matching sub).
# ---------------------------------------------------------------------------

class TestOwnerMatchBypass:
    """Regression tests for Q8 owner-match bypass — security-review Cycle 2 Critical."""

    def test_order_with_ownersub_and_mismatched_claim_sub_returns_403(self):
        """Bypass path A: order carries ownerSub, but caller presents a different sub.
        The original triple-and guard fires on this case already — but included
        here to anchor the test suite alongside the two bypass tests and ensure
        the fix does not accidentally break the primary denial path.
        """
        _skip_if_no_handler()
        item_with_owner = {
            **_ORDER_ITEM,
            "ownerSub": "device-A",
        }
        evt = _event(claim_sub="attacker-B", claim_tenant="meridian-demo")
        with patch.object(_handler_mod, "_get_ddb", return_value=_make_ddb_found(item_with_owner)):
            resp = _handler_mod.handler(evt, evt)
        assert resp["statusCode"] == 403, (
            "An order with ownerSub='device-A' presented to a caller with "
            "claim_sub='attacker-B' must be denied with 403"
        )

    def test_order_with_ownersub_and_no_claim_sub_returns_403(self):
        """Bypass path B: order carries ownerSub, but the token has no sub claim.
        The pre-fix triple-and `if order_owner_sub and claim_sub and ...`
        evaluates to False when claim_sub is None/empty, so the guard is
        SKIPPED and the handler returns 200.  After the fix the handler must
        return 403.
        """
        _skip_if_no_handler()
        item_with_owner = {
            **_ORDER_ITEM,
            "ownerSub": "device-A",
        }
        # Build an event whose claims dict omits 'sub' entirely
        evt = {
            "httpMethod": "GET",
            "pathParameters": {"orderId": "ORD-20260731-a1b2c3"},
            "requestContext": {
                "authorizer": {
                    "claims": {
                        # 'sub' intentionally absent — simulates a service token
                        # or a misconfigured custom authorizer
                        "custom:tenantId": "meridian-demo",
                        "cognito:groups": "kiosk",
                    }
                }
            },
            "body": None,
        }
        with patch.object(_handler_mod, "_get_ddb", return_value=_make_ddb_found(item_with_owner)):
            resp = _handler_mod.handler(evt, evt)
        assert resp["statusCode"] == 403, (
            "An order with ownerSub='device-A' presented by a token with no "
            "sub claim must be denied with 403, not granted silently"
        )
