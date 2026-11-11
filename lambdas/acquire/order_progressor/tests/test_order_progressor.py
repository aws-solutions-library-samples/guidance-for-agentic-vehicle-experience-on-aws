"""Tests for lambdas/acquire/order_progressor/handler.py

Run:
    python3 -m pytest lambdas/acquire/order_progressor/ -v
"""
from __future__ import annotations

import importlib.util
import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError

# ---------------------------------------------------------------------------
# Load handler by file path to avoid module-name collisions with sibling Lambdas.
# ---------------------------------------------------------------------------
_HANDLER_PATH = Path(__file__).parent.parent / "handler.py"
_spec = importlib.util.spec_from_file_location("order_progressor_handler", _HANDLER_PATH)
op = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(op)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _client_error(code: str = "ConditionalCheckFailedException") -> ClientError:
    return ClientError(
        {"Error": {"Code": code, "Message": "msg"}},
        "UpdateItem",
    )


def _order(
    order_id: str = "ORD-20260721-abc123",
    stage: str = "orderPlaced",
    next_at: str | None = None,
    tenant_id: str = "genericmoto-demo",
) -> dict:
    o: dict = {
        "orderId": order_id,
        "tenantId": tenant_id,
        "stage": stage,
        "stageHistory": [{"stage": stage, "enteredAt": "2026-07-21T15:00:00+00:00"}],
    }
    if next_at is not None:
        o["nextTransitionAt"] = next_at
    return o


_PAST = "2026-07-21T10:00:00+00:00"
_NOW = datetime(2026, 7, 21, 15, 0, 0, tzinfo=timezone.utc)


def _make_mock_resource(query_items=None, update_raises=None):
    table = MagicMock()
    query_items = query_items or []
    table.query.return_value = {"Items": query_items, "LastEvaluatedKey": None}
    table.get_item.return_value = {"Item": {}}

    if update_raises is not None:
        table.update_item.side_effect = update_raises
    else:
        table.update_item.return_value = {}

    resource = MagicMock()
    resource.Table.return_value = table
    return resource, table


# ---------------------------------------------------------------------------
# Unit tests for _next_stage()
# ---------------------------------------------------------------------------

class TestNextStage:
    def test_first_stage(self):
        assert op._next_stage("orderPlaced") == "paymentReceived"

    def test_penultimate_stage(self):
        assert op._next_stage("atDealer") == "readyForDelivery"

    def test_terminal_stage_returns_none(self):
        assert op._next_stage("readyForDelivery") is None

    def test_unknown_stage_returns_none(self):
        assert op._next_stage("unknownStage") is None

    def test_all_transitions_covered(self):
        for stage in op.STAGES[:-1]:
            assert op._next_stage(stage) is not None


# ---------------------------------------------------------------------------
# handler() integration tests
# ---------------------------------------------------------------------------

class TestHandler:
    # 1. Happy path — non-terminal order advances to next stage.
    def test_advances_non_terminal_order(self):
        order = _order(stage="orderPlaced", next_at=_PAST)
        resource, table = _make_mock_resource(query_items=[order])
        with patch.object(op, "_get_ddb", return_value=resource), \
             patch.object(op, "datetime") as mock_dt:
            mock_dt.now.return_value = _NOW
            result = op.handler({"tenantIds": ["genericmoto-demo"]}, None)
        assert result["advanced"] == 1
        assert result["skipped"] == 0
        assert result["errors"] == []
        table.update_item.assert_called_once()
        call_kwargs = table.update_item.call_args.kwargs
        assert ":nextStage" in call_kwargs["ExpressionAttributeValues"]
        assert call_kwargs["ExpressionAttributeValues"][":nextStage"] == "paymentReceived"

    # 2. Idempotent no-op — ConditionalCheckFailed means order already advanced.
    def test_concurrent_advance_is_noop(self):
        order = _order(stage="orderPlaced", next_at=_PAST)
        resource, table = _make_mock_resource(
            query_items=[order],
            update_raises=_client_error("ConditionalCheckFailedException"),
        )
        with patch.object(op, "_get_ddb", return_value=resource), \
             patch.object(op, "datetime") as mock_dt:
            mock_dt.now.return_value = _NOW
            result = op.handler({"tenantIds": ["genericmoto-demo"]}, None)
        assert result["advanced"] == 0
        assert result["skipped"] == 1
        assert result["errors"] == []

    # 3. Terminal order skipped (readyForDelivery has no nextTransitionAt).
    def test_terminal_order_skipped(self):
        order = _order(stage="readyForDelivery", next_at=None)
        resource, table = _make_mock_resource(query_items=[order])
        with patch.object(op, "_get_ddb", return_value=resource), \
             patch.object(op, "datetime") as mock_dt:
            mock_dt.now.return_value = _NOW
            result = op.handler({"tenantIds": ["genericmoto-demo"]}, None)
        assert result["advanced"] == 0
        assert result["skipped"] == 1
        table.update_item.assert_not_called()

    # 4. Dwell-time not yet elapsed → GSI returns empty.
    def test_not_yet_due_not_returned(self):
        resource, table = _make_mock_resource(query_items=[])
        with patch.object(op, "_get_ddb", return_value=resource), \
             patch.object(op, "datetime") as mock_dt:
            mock_dt.now.return_value = _NOW
            result = op.handler({"tenantIds": ["genericmoto-demo"]}, None)
        assert result["advanced"] == 0
        assert result["skipped"] == 0
        assert result["errors"] == []

    # 5. Empty scan — no active orders.
    def test_empty_scan_returns_zeros(self):
        resource, table = _make_mock_resource(query_items=[])
        with patch.object(op, "_get_ddb", return_value=resource), \
             patch.object(op, "datetime") as mock_dt:
            mock_dt.now.return_value = _NOW
            result = op.handler({"tenantIds": ["genericmoto-demo"]}, None)
        assert result == {"advanced": 0, "skipped": 0, "errors": []}

    # 6. ClientError on UpdateItem surfaces in errors[] without crashing.
    def test_update_fatal_error_captured_in_errors(self):
        order = _order(stage="orderPlaced", next_at=_PAST)
        resource, table = _make_mock_resource(
            query_items=[order],
            update_raises=_client_error("ProvisionedThroughputExceededException"),
        )
        with patch.object(op, "_get_ddb", return_value=resource), \
             patch.object(op, "datetime") as mock_dt:
            mock_dt.now.return_value = _NOW
            result = op.handler({"tenantIds": ["genericmoto-demo"]}, None)
        assert result["advanced"] == 0
        assert result["skipped"] == 0
        assert len(result["errors"]) == 1
        assert result["errors"][0]["orderId"] == "ORD-20260721-abc123"

    # 7. No tenants configured → early exit with zeros.
    def test_no_tenants_returns_zeros(self):
        resource, _ = _make_mock_resource()
        with patch.object(op, "_get_ddb", return_value=resource), \
             patch.object(op, "_ACTIVE_TENANTS_ENV", ""):
            result = op.handler({}, None)
        assert result == {"advanced": 0, "skipped": 0, "errors": []}

    # 8. Terminal transition removes nextTransitionAt (REMOVE in UpdateExpression).
    def test_entering_terminal_removes_next_transition_at(self):
        order = _order(stage="atDealer", next_at=_PAST)
        captured: list[dict] = []

        table = MagicMock()
        table.query.return_value = {"Items": [order], "LastEvaluatedKey": None}
        table.get_item.return_value = {"Item": {}}

        def capture_update(**kwargs):
            captured.append(kwargs)
            return {}

        table.update_item.side_effect = capture_update
        resource = MagicMock()
        resource.Table.return_value = table

        with patch.object(op, "_get_ddb", return_value=resource), \
             patch.object(op, "datetime") as mock_dt:
            mock_dt.now.return_value = _NOW
            result = op.handler({"tenantIds": ["genericmoto-demo"]}, None)

        assert result["advanced"] == 1
        assert len(captured) == 1
        assert "REMOVE" in captured[0]["UpdateExpression"]
        assert "nextTransitionAt" in captured[0]["UpdateExpression"]


# ---------------------------------------------------------------------------
# Single-order invocation mode (Step Functions path)
# ---------------------------------------------------------------------------

class TestSingleOrderMode:
    """handler({"orderId": "..."}, ...) exercises the single-order path."""

    # 1. Happy path — order exists and is due, advances to next stage.
    def test_single_order_advances(self):
        order = _order(stage="orderPlaced", next_at=_PAST)
        # get_item returns the order; update_item succeeds.
        table = MagicMock()
        table.get_item.return_value = {"Item": order}
        table.update_item.return_value = {}
        resource = MagicMock()
        resource.Table.return_value = table

        with patch.object(op, "_get_ddb", return_value=resource), \
             patch.object(op, "datetime") as mock_dt:
            mock_dt.now.return_value = _NOW
            result = op.handler({"orderId": "ORD-20260721-abc123"}, None)

        assert result == {"advanced": 1, "skipped": 0, "errors": []}
        table.update_item.assert_called_once()
        call_kw = table.update_item.call_args.kwargs
        assert call_kw["ExpressionAttributeValues"][":nextStage"] == "paymentReceived"
        # GSI query must NOT be called in single-order mode.
        table.query.assert_not_called()

    # 2. Idempotent retry — ConditionalCheckFailed means already advanced; returns skipped.
    def test_single_order_idempotent_retry(self):
        order = _order(stage="orderPlaced", next_at=_PAST)
        table = MagicMock()
        table.get_item.return_value = {"Item": order}
        table.update_item.side_effect = _client_error("ConditionalCheckFailedException")
        resource = MagicMock()
        resource.Table.return_value = table

        with patch.object(op, "_get_ddb", return_value=resource), \
             patch.object(op, "datetime") as mock_dt:
            mock_dt.now.return_value = _NOW
            result = op.handler({"orderId": "ORD-20260721-abc123"}, None)

        assert result == {"advanced": 0, "skipped": 1, "errors": []}

    # 3. Order not found — GetItem returns no Item; treated as skipped (safe to retry).
    def test_single_order_not_found_returns_skipped(self):
        table = MagicMock()
        table.get_item.return_value = {}  # no "Item" key
        resource = MagicMock()
        resource.Table.return_value = table

        with patch.object(op, "_get_ddb", return_value=resource), \
             patch.object(op, "datetime") as mock_dt:
            mock_dt.now.return_value = _NOW
            result = op.handler({"orderId": "ORD-MISSING"}, None)

        assert result == {"advanced": 0, "skipped": 1, "errors": []}
        table.update_item.assert_not_called()
        table.query.assert_not_called()

    # 4. Terminal order skipped — readyForDelivery has no nextTransitionAt.
    def test_single_order_terminal_skipped(self):
        order = _order(stage="readyForDelivery", next_at=None)
        table = MagicMock()
        table.get_item.return_value = {"Item": order}
        table.update_item.return_value = {}
        resource = MagicMock()
        resource.Table.return_value = table

        with patch.object(op, "_get_ddb", return_value=resource), \
             patch.object(op, "datetime") as mock_dt:
            mock_dt.now.return_value = _NOW
            result = op.handler({"orderId": "ORD-20260721-abc123"}, None)

        assert result == {"advanced": 0, "skipped": 1, "errors": []}
        table.update_item.assert_not_called()

    # 5. Terminal transition via single-order — REMOVE nextTransitionAt in UpdateExpression.
    def test_single_order_terminal_transition_removes_next_transition_at(self):
        order = _order(stage="atDealer", next_at=_PAST)
        captured: list[dict] = []

        def capture_update(**kwargs):
            captured.append(kwargs)
            return {}

        table = MagicMock()
        table.get_item.return_value = {"Item": order}
        table.update_item.side_effect = capture_update
        resource = MagicMock()
        resource.Table.return_value = table

        with patch.object(op, "_get_ddb", return_value=resource), \
             patch.object(op, "datetime") as mock_dt:
            mock_dt.now.return_value = _NOW
            result = op.handler({"orderId": "ORD-20260721-abc123"}, None)

        assert result["advanced"] == 1
        assert len(captured) == 1
        assert "REMOVE" in captured[0]["UpdateExpression"]
        assert "nextTransitionAt" in captured[0]["UpdateExpression"]

    # 6. UpdateItem fatal error in single-order mode surfaces in errors[].
    def test_single_order_update_error_captured(self):
        order = _order(stage="orderPlaced", next_at=_PAST)
        table = MagicMock()
        table.get_item.return_value = {"Item": order}
        table.update_item.side_effect = _client_error("ProvisionedThroughputExceededException")
        resource = MagicMock()
        resource.Table.return_value = table

        with patch.object(op, "_get_ddb", return_value=resource), \
             patch.object(op, "datetime") as mock_dt:
            mock_dt.now.return_value = _NOW
            result = op.handler({"orderId": "ORD-20260721-abc123"}, None)

        assert result["advanced"] == 0
        assert result["skipped"] == 0
        assert len(result["errors"]) == 1
        assert result["errors"][0]["orderId"] == "ORD-20260721-abc123"

    # 7. Tenant-scan path unchanged — orderId absent, tenantIds present → uses GSI.
    def test_tenant_scan_path_unchanged_when_no_order_id(self):
        order = _order(stage="orderPlaced", next_at=_PAST)
        resource, table = _make_mock_resource(query_items=[order])

        with patch.object(op, "_get_ddb", return_value=resource), \
             patch.object(op, "datetime") as mock_dt:
            mock_dt.now.return_value = _NOW
            result = op.handler({"tenantIds": ["genericmoto-demo"]}, None)

        assert result["advanced"] == 1
        # GSI query was used (not get_item for a single order).
        table.query.assert_called()

    # 8. Single-order mode does NOT call GSI query even when tenantIds also present.
    def test_single_order_ignores_tenant_ids(self):
        order = _order(stage="orderPlaced", next_at=_PAST)
        table = MagicMock()
        table.get_item.return_value = {"Item": order}
        table.update_item.return_value = {}
        resource = MagicMock()
        resource.Table.return_value = table

        with patch.object(op, "_get_ddb", return_value=resource), \
             patch.object(op, "datetime") as mock_dt:
            mock_dt.now.return_value = _NOW
            # Both orderId and tenantIds present — orderId wins.
            result = op.handler(
                {"orderId": "ORD-20260721-abc123", "tenantIds": ["genericmoto-demo"]},
                None,
            )

        assert result["advanced"] == 1
        table.query.assert_not_called()
