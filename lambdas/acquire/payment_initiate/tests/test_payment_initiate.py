"""Tests for lambdas/acquire/payment_initiate/handler.py

Run:
    python3 -m pytest lambdas/acquire/payment_initiate/ -v
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError

# ---------------------------------------------------------------------------
# Load handler by file path to avoid module-name collisions with sibling Lambdas.
# ---------------------------------------------------------------------------
_HANDLER_PATH = Path(__file__).parent.parent / "handler.py"
_spec = importlib.util.spec_from_file_location("payment_initiate_handler", _HANDLER_PATH)
pi = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pi)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _event(body: dict) -> dict:
    return {"body": json.dumps(body)}


_BASE = {
    "tenantId": "genericmoto-demo",
    "orderId": "ORD-20260721-abc123",
    "amount": 200.0,
    "currencyCode": "USD",
    "method": "card",
    "partnerId": "meridian-financial",
}


def _mock_ddb_table_ok():
    table = MagicMock()
    table.update_item.return_value = {}
    resource = MagicMock()
    resource.Table.return_value = table
    return resource


def _client_error(code: str = "ConditionalCheckFailedException") -> ClientError:
    return ClientError(
        {"Error": {"Code": code, "Message": "msg"}},
        "UpdateItem",
    )


# ---------------------------------------------------------------------------
# Pure helper tests
# ---------------------------------------------------------------------------

class TestMakeTxnRef:
    def test_determinism_same_inputs(self):
        a = pi._make_txn_ref("t1", "ord-1", 200.0)
        b = pi._make_txn_ref("t1", "ord-1", 200.0)
        assert a == b

    def test_different_order_different_ref(self):
        a = pi._make_txn_ref("t1", "ord-1", 200.0)
        b = pi._make_txn_ref("t1", "ord-2", 200.0)
        assert a != b

    def test_different_tenant_different_ref(self):
        a = pi._make_txn_ref("t1", "ord-1", 200.0)
        b = pi._make_txn_ref("t2", "ord-1", 200.0)
        assert a != b

    def test_different_amount_different_ref(self):
        a = pi._make_txn_ref("t1", "ord-1", 200.0)
        b = pi._make_txn_ref("t1", "ord-1", 300.0)
        assert a != b

    def test_ref_starts_with_txn_prefix(self):
        ref = pi._make_txn_ref("t1", "ord-1", 200.0)
        assert ref.startswith("TXN-")


# ---------------------------------------------------------------------------
# Handler tests
# ---------------------------------------------------------------------------

class TestHandler:
    def test_happy_path_captured(self):
        with patch.object(pi, "_get_ddb", return_value=_mock_ddb_table_ok()):
            resp = pi.handler(_event(_BASE), None)
        assert resp["statusCode"] == 200
        body = json.loads(resp["body"])
        assert body["status"] == "captured"
        assert body["transactionRef"].startswith("TXN-")
        assert "capturedAt" in body
        assert "receipt" in body

    def test_missing_required_field(self):
        bad = {k: v for k, v in _BASE.items() if k != "orderId"}
        resp = pi.handler(_event(bad), None)
        assert resp["statusCode"] == 400
        assert "orderId" in json.loads(resp["body"])["error"]

    def test_invalid_method_rejected(self):
        bad = {**_BASE, "method": "crypto"}
        resp = pi.handler(_event(bad), None)
        assert resp["statusCode"] == 400

    def test_zero_amount_rejected(self):
        bad = {**_BASE, "amount": 0}
        resp = pi.handler(_event(bad), None)
        assert resp["statusCode"] == 400

    def test_negative_amount_rejected(self):
        bad = {**_BASE, "amount": -50}
        resp = pi.handler(_event(bad), None)
        assert resp["statusCode"] == 400

    def test_determinism_same_txn_ref(self):
        """Same request body → same transactionRef across two invocations."""
        with patch.object(pi, "_get_ddb", return_value=_mock_ddb_table_ok()):
            r1 = json.loads(pi.handler(_event(_BASE), None)["body"])
            r2 = json.loads(pi.handler(_event(_BASE), None)["body"])
        assert r1["transactionRef"] == r2["transactionRef"]

    def test_order_not_found_is_not_an_error(self):
        """ConditionalCheckFailed (order not yet created) → still 200."""
        table = MagicMock()
        table.update_item.side_effect = _client_error("ConditionalCheckFailedException")
        resource = MagicMock()
        resource.Table.return_value = table
        with patch.object(pi, "_get_ddb", return_value=resource):
            resp = pi.handler(_event(_BASE), None)
        assert resp["statusCode"] == 200

    def test_ddb_fatal_error_returns_500(self):
        """Any non-conditional DDB error → 500."""
        table = MagicMock()
        table.update_item.side_effect = _client_error("ProvisionedThroughputExceededException")
        resource = MagicMock()
        resource.Table.return_value = table
        with patch.object(pi, "_get_ddb", return_value=resource):
            resp = pi.handler(_event(_BASE), None)
        assert resp["statusCode"] == 500

    def test_invalid_json_body(self):
        resp = pi.handler({"body": "not-json"}, None)
        assert resp["statusCode"] == 400

    def test_provider_name_in_response(self):
        with patch.object(pi, "_get_ddb", return_value=_mock_ddb_table_ok()):
            body = json.loads(pi.handler(_event(_BASE), None)["body"])
        assert "providerName" in body
        assert len(body["providerName"]) > 0
