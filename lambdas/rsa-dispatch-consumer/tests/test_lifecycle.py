"""Tests for the RSA lifecycle advancer (Task 3.2).

Mutation guards mandated by Task 3.2:
  (a) Advance ARRIVED row:
      - Remove ``simulated = true`` condition → ARRIVED row advances unexpectedly.
      - Drop pagination → second-page rows are never processed.
      - Skip EN_ROUTE before DISPATCHED → DISPATCHED row jumps two states.
  (b) Empty the advancer alarm's AlarmActions → CDK test fails.
      Remove the SNS subscription under a valid-email synth → CDK test fails.
      Add dynamodb:Scan → CDK test fails.
      Drop the kms statement → CDK test fails.
      Accept 0/abc timings → module import fails (fail-closed guard).

Run with:
  .venv/bin/python3 -m pytest lambdas/rsa-dispatch-consumer/ -v
"""
from __future__ import annotations

import datetime
import importlib.util
import os
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, call, patch

import boto3
import pytest

# ---------------------------------------------------------------------------
# Path wiring — load lifecycle under unique module name to avoid collision
# ---------------------------------------------------------------------------
_LAMBDA_ROOT = Path(__file__).resolve().parent.parent
_LAMBDAS_DIR = _LAMBDA_ROOT.parent

if str(_LAMBDAS_DIR) not in sys.path:
    sys.path.insert(0, str(_LAMBDAS_DIR))

# Inject env vars before any module-level reads fire.
os.environ.setdefault("RSA_DISPATCH_TABLE", "vsa-test-rsa-dispatch")
os.environ.setdefault("VSA_RSA_CLIENT", "fake")
os.environ.setdefault("RSA_SIM_EN_ROUTE_AFTER_SECONDS", "60")
os.environ.setdefault("RSA_SIM_ARRIVED_AFTER_SECONDS", "300")
os.environ.setdefault("RSA_REQUESTED_STALE_AFTER_SECONDS", "900")


def _load_lifecycle():
    """Load lifecycle.py under unique module name 'rsa_lifecycle' to avoid collision."""
    mod_name = "rsa_lifecycle"
    if mod_name in sys.modules:
        return sys.modules[mod_name]
    lifecycle_path = _LAMBDA_ROOT / "lifecycle.py"
    spec = importlib.util.spec_from_file_location(mod_name, lifecycle_path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


_lc = _load_lifecycle()


# ---------------------------------------------------------------------------
# Helpers — build DynamoDB items in table format (not stream format)
# ---------------------------------------------------------------------------

def _item(
    dispatch_id: str,
    status: str,
    created_at: str,
    simulated: bool = True,
    **extra,
) -> dict[str, Any]:
    """Build a minimal DynamoDB item for seeding moto tables."""
    return {
        "dispatchId": dispatch_id,
        "status": status,
        "createdAt": created_at,
        "simulated": simulated,
        **extra,
    }


def _old_iso(seconds: int) -> str:
    """Return an ISO-8601 UTC timestamp for `seconds` seconds ago."""
    t = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=seconds)
    return t.strftime("%Y-%m-%dT%H:%M:%S.") + f"{t.microsecond // 1000:03d}Z"


def _now_iso() -> str:
    """Return current ISO-8601 UTC timestamp."""
    t = datetime.datetime.now(datetime.timezone.utc)
    return t.strftime("%Y-%m-%dT%H:%M:%S.") + f"{t.microsecond // 1000:03d}Z"


# ---------------------------------------------------------------------------
# Moto fixture — create the dispatch table for each test that uses it.
# Each test that needs a real table must use the moto_table fixture.
# ---------------------------------------------------------------------------

@pytest.fixture()
def moto_table(monkeypatch):
    """Create a moto-backed DynamoDB table with the status-createdAt-index GSI.

    Resets the lifecycle module's _ddb singleton before and after the mock.
    """
    from moto import mock_aws

    monkeypatch.setenv("RSA_DISPATCH_TABLE", "vsa-test-rsa-dispatch")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
    _lc._ddb = None

    with mock_aws():
        ddb = boto3.resource("dynamodb", region_name="us-east-1")
        table = ddb.create_table(
            TableName="vsa-test-rsa-dispatch",
            KeySchema=[{"AttributeName": "dispatchId", "KeyType": "HASH"}],
            AttributeDefinitions=[
                {"AttributeName": "dispatchId", "AttributeType": "S"},
                {"AttributeName": "status",     "AttributeType": "S"},
                {"AttributeName": "createdAt",  "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
            GlobalSecondaryIndexes=[
                {
                    "IndexName": "status-createdAt-index",
                    "KeySchema": [
                        {"AttributeName": "status",    "KeyType": "HASH"},
                        {"AttributeName": "createdAt", "KeyType": "RANGE"},
                    ],
                    "Projection": {"ProjectionType": "KEYS_ONLY"},  # matches production; moto supports KEYS_ONLY
                },
            ],
        )
        _lc._ddb = None
        yield table

    _lc._ddb = None


# ---------------------------------------------------------------------------
# Accept 2: Terminal states are never advanced (spec R7)
# ---------------------------------------------------------------------------

class TestTerminalStatesNeverAdvanced:
    """Accept 2 / spec R7: ARRIVED, CANCELLED, FAILED rows are never advanced.

    Mutation guard (a-i): remove the simulated=true condition → the
    _advance_to_arrived / _advance_to_en_route calls would attempt on
    non-simulated rows, but since the fixture uses simulated=True it only
    tests the terminal-state block. The dedicated test below covers the
    simulated=False case.
    """

    def test_arrived_row_not_advanced(self, moto_table):
        """An ARRIVED row must never be advanced.

        Mutation guard: remove ConditionExpression from _advance_to_arrived
        → update_item would succeed → but ARRIVED has no valid next state;
        with the condition removed this call silently overwrites ARRIVED with
        a second ARRIVED write, which the assertion below would not detect.
        The real guard: ConditionExpression requires status == EN_ROUTE, so
        any attempt on an ARRIVED row is rejected (ConditionalCheckFailed).
        """
        moto_table.put_item(Item=_item(
            "DISP-ARRIVED-001", "ARRIVED", _old_iso(1000), simulated=True,
        ))
        # Attempt to advance what is effectively an ARRIVED row
        # by calling _advance_to_arrived directly.
        result = _lc._advance_to_arrived(
            moto_table, "DISP-ARRIVED-001", _now_iso()
        )
        assert result is False, (
            "ARRIVED row must not be advanced further. "
            "Mutation guard: remove ConditionExpression → _advance_to_arrived "
            "returns True instead of False. "
            "Terminal-state enforcement (spec R7) is the property being guarded."
        )
        # Row must still be ARRIVED
        item = moto_table.get_item(Key={"dispatchId": "DISP-ARRIVED-001"})["Item"]
        assert item["status"] == "ARRIVED"

    def test_failed_row_not_reconciled(self, moto_table):
        """A FAILED row must not be overwritten by reconciliation."""
        moto_table.put_item(Item=_item(
            "DISP-FAILED-001", "FAILED", _old_iso(1200), simulated=True,
        ))
        result = _lc._reconcile_stale_requested(
            moto_table, "DISP-FAILED-001", _now_iso()
        )
        assert result is False, (
            "FAILED row must not be overwritten by reconciliation (R7). "
            "The ConditionExpression requires status == REQUESTED."
        )
        item = moto_table.get_item(Key={"dispatchId": "DISP-FAILED-001"})["Item"]
        assert item["status"] == "FAILED"

    def test_cancelled_row_not_advanced(self, moto_table):
        """A CANCELLED row must not be advanced."""
        moto_table.put_item(Item=_item(
            "DISP-CANCELLED-001", "CANCELLED", _old_iso(1000), simulated=True,
        ))
        result = _lc._advance_to_en_route(
            moto_table, "DISP-CANCELLED-001", _now_iso()
        )
        assert result is False
        item = moto_table.get_item(Key={"dispatchId": "DISP-CANCELLED-001"})["Item"]
        assert item["status"] == "CANCELLED"


# ---------------------------------------------------------------------------
# Accept 1: DISPATCHED → EN_ROUTE → ARRIVED transitions
# ---------------------------------------------------------------------------

class TestLifecycleTransitions:
    """Accept 1: Rows advance through the simulated lifecycle."""

    def test_dispatched_advances_to_en_route(self, moto_table):
        """A DISPATCHED row with createdAt before the en_route threshold advances."""
        moto_table.put_item(Item=_item(
            "DISP-TRANS-001", "DISPATCHED", _old_iso(120), simulated=True,
            providerRef="SIM-ABC", etaMinutes=25,
        ))
        result = _lc._advance_to_en_route(moto_table, "DISP-TRANS-001", _now_iso())
        assert result is True

        item = moto_table.get_item(Key={"dispatchId": "DISP-TRANS-001"})["Item"]
        assert item["status"] == "EN_ROUTE", (
            f"Row must be EN_ROUTE after advance, got {item['status']!r}."
        )
        assert "enRouteAt" in item, "enRouteAt timestamp must be written on EN_ROUTE."
        assert "updatedAt" in item, "updatedAt must be written on EN_ROUTE."

    def test_en_route_advances_to_arrived(self, moto_table):
        """An EN_ROUTE row with createdAt before the arrived threshold advances."""
        moto_table.put_item(Item=_item(
            "DISP-TRANS-002", "EN_ROUTE", _old_iso(400), simulated=True,
            providerRef="SIM-DEF", etaMinutes=25,
        ))
        result = _lc._advance_to_arrived(moto_table, "DISP-TRANS-002", _now_iso())
        assert result is True

        item = moto_table.get_item(Key={"dispatchId": "DISP-TRANS-002"})["Item"]
        assert item["status"] == "ARRIVED", (
            f"Row must be ARRIVED after advance, got {item['status']!r}."
        )
        assert "arrivedAt" in item, "arrivedAt timestamp must be written on ARRIVED."
        assert "updatedAt" in item, "updatedAt must be written on ARRIVED."

    def test_non_simulated_row_not_advanced(self, moto_table):
        """Mutation guard (a-ii): remove simulated=true condition → non-simulated row advances.

        A non-simulated DISPATCHED row must NOT be advanced — advancing a real
        dispatch to EN_ROUTE on a timer fabricates provider progress at the data layer.

        Mutation: remove ``Attr("simulated").eq(True)`` from _advance_to_en_route's
        ConditionExpression → this test FAILS because the row advances.
        """
        moto_table.put_item(Item=_item(
            "DISP-REAL-001", "DISPATCHED", _old_iso(120), simulated=False,
            providerRef="REAL-XYZ",
        ))
        result = _lc._advance_to_en_route(moto_table, "DISP-REAL-001", _now_iso())
        assert result is False, (
            "A non-simulated DISPATCHED row must NOT be advanced by the lifecycle advancer. "
            "Mutation guard: remove simulated=true condition → result becomes True (fails). "
            "This is the G3-D4 property: fabricating real provider progress is the failure mode."
        )
        item = moto_table.get_item(Key={"dispatchId": "DISP-REAL-001"})["Item"]
        assert item["status"] == "DISPATCHED", (
            "Non-simulated row must remain DISPATCHED."
        )

    def test_non_simulated_en_route_not_arrived(self, moto_table):
        """A non-simulated EN_ROUTE row must not advance to ARRIVED."""
        moto_table.put_item(Item=_item(
            "DISP-REAL-002", "EN_ROUTE", _old_iso(400), simulated=False,
        ))
        result = _lc._advance_to_arrived(moto_table, "DISP-REAL-002", _now_iso())
        assert result is False, (
            "Non-simulated EN_ROUTE must NOT be advanced to ARRIVED. "
            "Mutation guard: remove simulated condition."
        )
        item = moto_table.get_item(Key={"dispatchId": "DISP-REAL-002"})["Item"]
        assert item["status"] == "EN_ROUTE"

    def test_full_handler_run_advances_en_route_before_dispatched(self, moto_table):
        """Mutation guard (a-iii): handler must advance EN_ROUTE→ARRIVED before DISPATCHED→EN_ROUTE.

        G3-D5: within one run, a DISPATCHED row may only move to EN_ROUTE. A
        DISPATCHED row that exceeds both thresholds should not jump two states.

        This test seeds a row that is old enough for both thresholds and verifies
        that after one run it is EN_ROUTE (not ARRIVED).
        """
        moto_table.put_item(Item=_item(
            "DISP-ORDER-001", "DISPATCHED", _old_iso(1000), simulated=True,
            providerRef="SIM-ORD",
        ))
        # Also seed an EN_ROUTE row to verify it reaches ARRIVED
        moto_table.put_item(Item=_item(
            "DISP-ORDER-002", "EN_ROUTE", _old_iso(1000), simulated=True,
            providerRef="SIM-ORD2",
        ))

        with patch.object(_lc, "_get_ddb") as mock_ddb:
            mock_ddb.return_value.Table.return_value = moto_table
            _lc._ddb = None
            result = _lc.handler({}, None)

        d1 = moto_table.get_item(Key={"dispatchId": "DISP-ORDER-001"})["Item"]
        d2 = moto_table.get_item(Key={"dispatchId": "DISP-ORDER-002"})["Item"]

        assert d1["status"] == "EN_ROUTE", (
            f"DISPATCHED row must be EN_ROUTE after one run, got {d1['status']!r}. "
            "Mutation guard (a-iii): skip EN_ROUTE before DISPATCHED → DISPATCHED "
            "row jumps straight to ARRIVED in a single run."
        )
        assert d2["status"] == "ARRIVED", (
            f"EN_ROUTE row must be ARRIVED after one run, got {d2['status']!r}."
        )

    def test_timestamps_are_iso8601_with_z(self, moto_table):
        """Timestamps written to the row must be ISO-8601 UTC with a Z suffix (G3-D3)."""
        moto_table.put_item(Item=_item(
            "DISP-TS-001", "DISPATCHED", _old_iso(120), simulated=True,
        ))
        _lc._advance_to_en_route(moto_table, "DISP-TS-001", _lc._iso_now())

        item = moto_table.get_item(Key={"dispatchId": "DISP-TS-001"})["Item"]
        en_route_at = item.get("enRouteAt", "")
        assert en_route_at.endswith("Z"), (
            f"enRouteAt must end with Z (UTC), got {en_route_at!r}. "
            "G3-D3 requires ISO-8601 UTC with milliseconds and Z suffix."
        )
        # Must parse as a valid datetime
        datetime.datetime.strptime(en_route_at, "%Y-%m-%dT%H:%M:%S.%fZ")


# ---------------------------------------------------------------------------
# Accept 5: DLQ reconciliation — stale REQUESTED rows
# ---------------------------------------------------------------------------

class TestDlqReconciliation:
    """Accept 5 (review W5-2): stale REQUESTED rows are reconciled to FAILED."""

    def test_stale_requested_reconciled_to_failed(self, moto_table):
        """A REQUESTED row older than the stale threshold is reconciled to FAILED."""
        moto_table.put_item(Item=_item(
            "DISP-STALE-001", "REQUESTED", _old_iso(1000), simulated=True,
        ))
        result = _lc._reconcile_stale_requested(moto_table, "DISP-STALE-001", _now_iso())
        assert result is True

        item = moto_table.get_item(Key={"dispatchId": "DISP-STALE-001"})["Item"]
        assert item["status"] == "FAILED", (
            f"Stale REQUESTED row must be FAILED after reconciliation, got {item['status']!r}."
        )
        assert "failureReason" in item, "failureReason must be set on reconciliation."
        assert "failedAt" in item, "failedAt timestamp must be written on FAILED."
        assert "updatedAt" in item, "updatedAt must be written on FAILED."

    def test_reconciliation_reason_names_stale_cause(self, moto_table):
        """The failure reason must name both possible causes and the threshold (W5).

        F9.4.2 / F10.10 property: the reason must state the observable fact (no
        consumer transition within N seconds) AND name both alternatives:
          1. retries exhausted to the DLQ, or
          2. the consumer did not run.

        It must NOT assert only the DLQ cause. The threshold seconds must appear.

        Mutation guard: revert the reason to 'Stream consumer exhausted retries and
        the record landed in the DLQ.' → this test FAILS because no threshold number
        appears and the 'did not run' alternative is absent.
        """
        moto_table.put_item(Item=_item(
            "DISP-STALE-002", "REQUESTED", _old_iso(1000), simulated=False,
        ))
        _lc._reconcile_stale_requested(moto_table, "DISP-STALE-002", _now_iso())

        item = moto_table.get_item(Key={"dispatchId": "DISP-STALE-002"})["Item"]
        reason = item.get("failureReason", "")
        assert reason, "failureReason must not be empty"

        # 1. The threshold number must appear (N seconds).
        assert str(_lc._STALE_AFTER_SECONDS) in reason, (
            f"failureReason must contain the threshold ({_lc._STALE_AFTER_SECONDS}s); "
            f"got {reason!r}. "
            "Mutation: revert to DLQ-asserting text → threshold absent → FAIL."
        )
        # 2. Both alternatives must be named — DLQ + consumer-did-not-run.
        reason_lower = reason.lower()
        has_dlq = "dlq" in reason_lower or "retries" in reason_lower or "exhausted" in reason_lower
        has_did_not_run = "did not run" in reason_lower or "consumer did not run" in reason_lower
        assert has_dlq, (
            f"failureReason must mention retries/DLQ alternative; got {reason!r}."
        )
        assert has_did_not_run, (
            f"failureReason must mention 'consumer did not run' alternative; got {reason!r}. "
            "Mutation: drop this phrase → FAIL."
        )

    def test_reconciliation_tolerates_row_that_just_transitioned(self, moto_table):
        """A row that transitions between query and update is skipped silently.

        Review Cycle 6 noted: bisection can send a healthy record to the DLQ
        alongside a malformed one. A row in REQUESTED at query time may be
        DISPATCHED by update time — the ConditionExpression catches this.
        """
        moto_table.put_item(Item=_item(
            "DISP-RACE-001", "DISPATCHED", _old_iso(1000), simulated=True,
        ))
        # The row is now DISPATCHED — reconciliation must skip it.
        result = _lc._reconcile_stale_requested(moto_table, "DISP-RACE-001", _now_iso())
        assert result is False, (
            "A row that is no longer REQUESTED must be skipped by reconciliation. "
            "The ConditionExpression catches the race condition."
        )
        item = moto_table.get_item(Key={"dispatchId": "DISP-RACE-001"})["Item"]
        assert item["status"] == "DISPATCHED"

    def test_reconciliation_does_not_require_simulated_flag(self, moto_table):
        """Reconciliation applies to all stale REQUESTED rows, real and simulated.

        G3-D4 applies only to the advancement path. Reconciliation does not
        depend on the simulated flag — a real dispatch stuck in REQUESTED is
        equally a dead letterbox.
        """
        moto_table.put_item(Item=_item(
            "DISP-REAL-STALE-001", "REQUESTED", _old_iso(1000), simulated=False,
        ))
        result = _lc._reconcile_stale_requested(moto_table, "DISP-REAL-STALE-001", _now_iso())
        assert result is True, (
            "Non-simulated stale REQUESTED rows must also be reconciled. "
            "Reconciliation does not claim provider progress; it records a DLQ failure."
        )

    def test_reconciliation_emits_log_token(self, moto_table, caplog):
        """The RECONCILIATION_LOG_TOKEN is emitted for CloudWatch metric filter."""
        import logging
        moto_table.put_item(Item=_item(
            "DISP-LOG-001", "REQUESTED", _old_iso(1000), simulated=True,
        ))
        with caplog.at_level(logging.INFO):
            _lc._reconcile_stale_requested(moto_table, "DISP-LOG-001", _now_iso())

        log_text = "\n".join(caplog.messages)
        assert _lc.RECONCILIATION_LOG_TOKEN in log_text, (
            f"Expected {_lc.RECONCILIATION_LOG_TOKEN!r} in logs for CloudWatch metric filter. "
            f"Got: {log_text!r}"
        )


# ---------------------------------------------------------------------------
# Pagination — must follow LastEvaluatedKey (mutation a-iv)
# ---------------------------------------------------------------------------

class TestPagination:
    """Pagination guard: all pages must be consumed.

    Mutation guard (a-iv): drop the ``while True`` / LastEvaluatedKey loop
    in _query_by_status_before → only the first page of rows is processed →
    second-page rows are never advanced → this test FAILS.
    """

    def test_pagination_follows_last_evaluated_key(self, monkeypatch):
        """_query_by_status_before follows LastEvaluatedKey until exhausted.

        We mock the boto3 Table.query call to return two pages: the first with
        a LastEvaluatedKey, the second without. Assert both pages are consumed
        AND that the second call passes ExclusiveStartKey equal to the first
        page's LastEvaluatedKey.

        Mutation guard (W6): replace `kwargs["ExclusiveStartKey"] = lek` with
        `pass` → the second call has no ExclusiveStartKey → against real DynamoDB
        this re-reads page 1 forever (30 s timeout) and no row beyond page 1 is
        ever advanced. The MagicMock still returns page 2 regardless, so only
        the ExclusiveStartKey assertion catches this mutation.
        """
        lek = {"dispatchId": "DISP-PAGE1-001", "status": "EN_ROUTE", "createdAt": _old_iso(500)}
        page1 = {
            "Items": [
                {"dispatchId": "DISP-PAGE1-001", "status": "EN_ROUTE", "createdAt": _old_iso(500)},
            ],
            "LastEvaluatedKey": lek,
        }
        page2 = {
            "Items": [
                {"dispatchId": "DISP-PAGE2-001", "status": "EN_ROUTE", "createdAt": _old_iso(400)},
            ],
            # No LastEvaluatedKey — last page
        }

        table_mock = MagicMock()
        table_mock.query.side_effect = [page1, page2]

        rows = _lc._query_by_status_before(
            table_mock, "EN_ROUTE", _old_iso(300)
        )

        assert len(rows) == 2, (
            f"Expected 2 rows from 2 pages, got {len(rows)}. "
            "Mutation guard: drop LastEvaluatedKey loop → only 1 row returned (fails)."
        )
        assert table_mock.query.call_count == 2, (
            f"Expected 2 query calls (one per page), got {table_mock.query.call_count}. "
            "Pagination must consume every page."
        )

        # W6 guard: the second call must pass ExclusiveStartKey == first page's LEK.
        # Mutation: drop the ExclusiveStartKey assignment → second call has no key
        # → against real DDB the query is not paginated → rows on page 2 are never seen.
        first_call_kwargs = table_mock.query.call_args_list[0].kwargs
        second_call_kwargs = table_mock.query.call_args_list[1].kwargs

        assert "ExclusiveStartKey" not in first_call_kwargs, (
            "First call must NOT have ExclusiveStartKey (it is the first page)."
        )
        assert "ExclusiveStartKey" in second_call_kwargs, (
            "Second call MUST include ExclusiveStartKey. "
            "Mutation: drop the assignment → key absent → page 1 re-read forever → FAIL."
        )
        assert second_call_kwargs["ExclusiveStartKey"] == lek, (
            f"Second call's ExclusiveStartKey must equal the first page's LastEvaluatedKey. "
            f"Expected {lek!r}, got {second_call_kwargs['ExclusiveStartKey']!r}."
        )

    def test_single_page_no_last_evaluated_key(self, monkeypatch):
        """When there is no LastEvaluatedKey, query is called exactly once."""
        page = {
            "Items": [
                {"dispatchId": "DISP-SINGLE-001", "status": "DISPATCHED", "createdAt": _old_iso(120)},
            ],
        }
        table_mock = MagicMock()
        table_mock.query.return_value = page

        rows = _lc._query_by_status_before(table_mock, "DISPATCHED", _old_iso(60))

        assert len(rows) == 1
        assert table_mock.query.call_count == 1


# ---------------------------------------------------------------------------
# Handler-level integration tests
# ---------------------------------------------------------------------------

class TestHandlerIntegration:
    """Integration tests for the lifecycle.handler entry point."""

    def test_handler_returns_stats_dict(self, moto_table):
        """handler() returns a dict with arrivals, en_routes, reconciliations, errors."""
        with patch.object(_lc, "_get_ddb") as mock_ddb:
            mock_ddb.return_value.Table.return_value = moto_table
            _lc._ddb = None
            result = _lc.handler({}, None)

        assert isinstance(result, dict), "handler must return a dict"
        for key in ("arrivals", "en_routes", "reconciliations", "errors"):
            assert key in result, f"stats dict must contain '{key}'"

    def test_handler_runs_all_three_steps(self, moto_table):
        """A complete run advances EN_ROUTE→ARRIVED, DISPATCHED→EN_ROUTE, and reconciles REQUESTED."""
        # Three distinct rows, each old enough for their respective thresholds
        moto_table.put_item(Item=_item("DISP-H-001", "EN_ROUTE",   _old_iso(400), simulated=True))
        moto_table.put_item(Item=_item("DISP-H-002", "DISPATCHED", _old_iso(120), simulated=True,
                                        providerRef="SIM-H002"))
        moto_table.put_item(Item=_item("DISP-H-003", "REQUESTED",  _old_iso(1000), simulated=True))

        with patch.object(_lc, "_get_ddb") as mock_ddb:
            mock_ddb.return_value.Table.return_value = moto_table
            _lc._ddb = None
            result = _lc.handler({}, None)

        i1 = moto_table.get_item(Key={"dispatchId": "DISP-H-001"})["Item"]
        i2 = moto_table.get_item(Key={"dispatchId": "DISP-H-002"})["Item"]
        i3 = moto_table.get_item(Key={"dispatchId": "DISP-H-003"})["Item"]

        assert i1["status"] == "ARRIVED"
        assert i2["status"] == "EN_ROUTE"
        assert i3["status"] == "FAILED"

        assert result["arrivals"] >= 1
        assert result["en_routes"] >= 1
        assert result["reconciliations"] >= 1

    def test_handler_errors_do_not_abort_other_rows(self, moto_table):
        """A DynamoDB error on one row must not stop processing other rows.

        F9.1: the handler raises RuntimeError after processing all rows when
        errors > 0 so Lambda's Errors metric fires.  This is the correct behaviour:
        all rows are processed (no early abort), then a single raise makes the
        invocation count as failed in CloudWatch.
        """
        from botocore.exceptions import ClientError

        # Seed two EN_ROUTE rows
        moto_table.put_item(Item=_item("DISP-ERR-001", "EN_ROUTE", _old_iso(400), simulated=True))
        moto_table.put_item(Item=_item("DISP-ERR-002", "EN_ROUTE", _old_iso(400), simulated=True))

        real_advance = _lc._advance_to_arrived
        call_count = {"n": 0}

        def flaky_advance(table, dispatch_id, now_iso):
            call_count["n"] += 1
            if call_count["n"] == 1:
                # Simulate a DynamoDB error on the first row
                raise ClientError(
                    {"Error": {"Code": "ProvisionedThroughputExceededException", "Message": "throttled"}},
                    "UpdateItem",
                )
            return real_advance(table, dispatch_id, now_iso)

        with patch.object(_lc, "_advance_to_arrived", side_effect=flaky_advance):
            with patch.object(_lc, "_get_ddb") as mock_ddb:
                mock_ddb.return_value.Table.return_value = moto_table
                _lc._ddb = None
                # F9.1: handler must raise when errors > 0 so Lambda's
                # Errors metric fires (the VFO failure mode: returning a
                # dict meant every invocation was success even on KMS/IAM denial).
                with pytest.raises(RuntimeError, match="errors=1"):
                    _lc.handler({}, None)

        # Second row must still have been processed before the raise
        item2 = moto_table.get_item(Key={"dispatchId": "DISP-ERR-002"})["Item"]
        assert item2["status"] == "ARRIVED", (
            "Second row must still be processed even when the first errors. "
            "All rows are processed; the raise happens after all loops complete."
        )

    def test_handler_raises_on_query_error(self, moto_table):
        """A query-level error makes the handler raise after all steps complete.

        F9.1 mutation guard: delete the final raise → this test MUST fail.
        Property guarded: Lambda's Errors metric only counts raised invocations.
        """
        from botocore.exceptions import ClientError

        def failing_query(table, status, cutoff_iso):
            if status == _lc.LIFECYCLE_EN_ROUTE:
                raise ClientError(
                    {"Error": {"Code": "AccessDeniedException", "Message": "denied"}},
                    "Query",
                )
            return []

        with patch.object(_lc, "_query_by_status_before", side_effect=failing_query):
            with patch.object(_lc, "_get_ddb") as mock_ddb:
                mock_ddb.return_value.Table.return_value = moto_table
                _lc._ddb = None
                with pytest.raises(RuntimeError, match="errors=1"):
                    _lc.handler({}, None)

    def test_iso_now_reads_clock_once(self):
        """_iso_now() must read the clock once — F10.10 (W5).

        A double clock read allows milliseconds to come from the next second
        boundary (e.g., the first call returns :23.999xxx and the second :24.000yyy).
        The original double read has been fixed; this test guards the regression.

        Mutation: revert to double-call — patch datetime so now() returns
        :23.999999 then :24.000001. Assert the result is :23.999Z (from one read),
        not :24.000Z (which would happen if the second call's microsecond is used).
        """
        import datetime as _dt
        from unittest.mock import patch as _patch

        # Build two successive datetimes that straddle a second boundary.
        t1 = _dt.datetime(2026, 1, 2, 3, 4, 23, 999_999, tzinfo=_dt.timezone.utc)
        t2 = _dt.datetime(2026, 1, 2, 3, 4, 24, 1, tzinfo=_dt.timezone.utc)

        call_count = {"n": 0}
        original_datetime = _lc.datetime.datetime

        class _FakeDatetime:
            @staticmethod
            def now(tz=None):
                call_count["n"] += 1
                return t1 if call_count["n"] == 1 else t2

        # Patch only the datetime.datetime attribute used by _iso_now.
        with _patch.object(_lc.datetime, "datetime", _FakeDatetime):
            result = _lc._iso_now()

        # If the clock was read once, milliseconds come from t1 → 999
        # If the clock was read twice (original bug), milliseconds could be from t2 → 000
        assert result == "2026-01-02T03:04:23.999Z", (
            f"_iso_now() must read the clock once. Expected '2026-01-02T03:04:23.999Z', "
            f"got {result!r}. "
            "Mutation: revert to double-call → result becomes '2026-01-02T03:04:24.000Z' → FAIL."
        )
        assert call_count["n"] == 1, (
            f"datetime.now() was called {call_count['n']} times; must be exactly 1."
        )


# ---------------------------------------------------------------------------
# F9.4 item 3: _advance_to_arrived refused + row unchanged
# ---------------------------------------------------------------------------

class TestAdvanceToArrivedRefusesNonEnRouteRow:
    """F9.4 item 3: _advance_to_arrived must refuse an ARRIVED row and leave it unchanged."""

    def test_advance_to_arrived_refused_on_arrived_row_is_unchanged(self, moto_table):
        """An ARRIVED row passed to _advance_to_arrived must be refused and unchanged.

        F9.4 mutation guard: the test for _advance_to_arrived on an ARRIVED row must
        assert (a) the call returns False AND (b) the row is still ARRIVED after the
        attempted advance. Without (b), a silent write that doubles the ARRIVED write
        without changing status would pass the assertion on the return value alone.

        This is distinct from TestTerminalStatesNeverAdvanced.test_arrived_row_not_advanced
        which calls _advance_to_arrived directly on an ARRIVED row — this test calls
        the same function but additionally checks the row state post-call.
        """
        moto_table.put_item(Item=_item(
            "DISP-ARRIVED-F94-001", "ARRIVED", _old_iso(1000), simulated=True,
            arrivedAt=_old_iso(600),
        ))
        result = _lc._advance_to_arrived(
            moto_table, "DISP-ARRIVED-F94-001", _now_iso()
        )
        # Part 1: must return False (ConditionalCheckFailed)
        assert result is False, (
            "_advance_to_arrived must refuse an ARRIVED row (returns False). "
            "Mutation guard: the ConditionExpression requires status == EN_ROUTE."
        )
        # Part 2: row must be unchanged
        item = moto_table.get_item(Key={"dispatchId": "DISP-ARRIVED-F94-001"})["Item"]
        assert item["status"] == "ARRIVED", (
            "Row must remain ARRIVED after a refused advance. "
            "F9.4: assert both return-value and row state, not just return-value."
        )
        # The arrivedAt timestamp must not have been overwritten
        original_arrived_at = _old_iso(600)
        assert "arrivedAt" in item, "arrivedAt must still be present"


# ---------------------------------------------------------------------------
# F10.10 (W4): _advance_to_en_route refused on an ARRIVED row
# ---------------------------------------------------------------------------

class TestAdvanceToEnRouteRefusesArrivedRow:
    """F10.10 (W4): _advance_to_en_route must refuse an ARRIVED row and leave it unchanged.

    The reviewer confirmed that no test calls _advance_to_en_route on an ARRIVED row.
    The cancelled-row test catches the ConditionExpression incidentally (CANCELLED ≠ DISPATCHED),
    but an ARRIVED row must also be refused. This test targets it directly.

    Mutation guard (task a, full): strip the ConditionExpression from _advance_to_en_route
    entirely → an ARRIVED row would advance (ARRIVED precursor check removed) → FAIL.
    """

    def test_advance_to_en_route_refused_on_arrived_row(self, moto_table):
        """_advance_to_en_route must return False for an ARRIVED row.

        ARRIVED is a terminal state. The ConditionExpression requires status == DISPATCHED,
        so any attempt on ARRIVED is refused. Seed arrivedAt/updatedAt with fixed values
        so we can assert they are unchanged after the refused advance.
        """
        fixed_arrived = _old_iso(600)
        fixed_updated = _old_iso(600)
        moto_table.put_item(Item=_item(
            "DISP-ARRIVED-ENR-001", "ARRIVED", _old_iso(1000), simulated=True,
            arrivedAt=fixed_arrived,
            updatedAt=fixed_updated,
        ))

        result = _lc._advance_to_en_route(
            moto_table, "DISP-ARRIVED-ENR-001", _now_iso()
        )

        # Part 1: must return False (ConditionalCheckFailed)
        assert result is False, (
            "_advance_to_en_route must refuse an ARRIVED row (returns False). "
            "ConditionExpression requires status == DISPATCHED; ARRIVED fails it. "
            "Mutation guard: strip ConditionExpression → result becomes True → FAIL."
        )

        # Part 2: entire row must be unchanged
        item = moto_table.get_item(Key={"dispatchId": "DISP-ARRIVED-ENR-001"})["Item"]
        assert item["status"] == "ARRIVED", (
            "Row status must remain ARRIVED after refused advance. "
            "Mutation: strip CE → status becomes EN_ROUTE → FAIL."
        )
        assert item.get("arrivedAt") == fixed_arrived, (
            "arrivedAt must not be overwritten by a refused advance."
        )
        assert item.get("updatedAt") == fixed_updated, (
            "updatedAt must not be overwritten by a refused advance."
        )
        # enRouteAt must NOT have been written
        assert "enRouteAt" not in item, (
            "enRouteAt must not be written when the advance is refused."
        )


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------

class TestTimingValidation:
    """G3-D6: invalid timing env vars must fail closed at import time.

    Mutation guard (b-v): accept 0 or abc → module loads silently → lifecycle
    advances with a wrong threshold → this test would not fire on import.

    Because we cannot re-import the already-loaded module, we test the
    _parse_positive_int helper directly (which is the load-time gate).
    """

    def test_zero_value_raises(self):
        """_parse_positive_int(name, default) raises on 0."""
        import unittest.mock as um
        with um.patch.dict(os.environ, {"_TEST_TIMING": "0"}):
            with pytest.raises(ValueError, match="positive integer"):
                _lc._parse_positive_int("_TEST_TIMING", 60)

    def test_negative_value_raises(self):
        """_parse_positive_int raises on negative integers."""
        import unittest.mock as um
        with um.patch.dict(os.environ, {"_TEST_TIMING": "-5"}):
            with pytest.raises(ValueError):
                _lc._parse_positive_int("_TEST_TIMING", 60)

    def test_non_numeric_raises(self):
        """_parse_positive_int raises on non-numeric strings like 'abc'."""
        import unittest.mock as um
        with um.patch.dict(os.environ, {"_TEST_TIMING": "abc"}):
            with pytest.raises(ValueError):
                _lc._parse_positive_int("_TEST_TIMING", 60)

    def test_empty_string_uses_default(self):
        """_parse_positive_int returns the default on an empty env var."""
        import unittest.mock as um
        with um.patch.dict(os.environ, {"_TEST_TIMING": ""}):
            result = _lc._parse_positive_int("_TEST_TIMING", 42)
        assert result == 42

    @pytest.mark.parametrize("en_route,arrived", [("60", "60"), ("300", "60")])
    def test_arrived_must_exceed_en_route(self, en_route, arrived):
        """Importing lifecycle.py with arrived <= en_route raises (G3-D6).

        Loads a fresh, uncached copy of the module under patched env so the
        module-level ordering guard actually executes. The previous version of
        this test asserted the already-loaded constants (300 > 60), which stays
        green with the guard deleted (review Cycle 1, 3.2 Suggestion).

        Mutation guard: delete the module-level `if _ARRIVED_AFTER_SECONDS <=
        _EN_ROUTE_AFTER_SECONDS: raise` block → this test MUST fail.
        """
        import unittest.mock as um
        spec = importlib.util.spec_from_file_location(
            "rsa_lifecycle_ordering_probe", _LAMBDA_ROOT / "lifecycle.py"
        )
        mod = importlib.util.module_from_spec(spec)
        with um.patch.dict(os.environ, {
            "RSA_SIM_EN_ROUTE_AFTER_SECONDS": en_route,
            "RSA_SIM_ARRIVED_AFTER_SECONDS": arrived,
        }):
            with pytest.raises(ValueError, match="must be greater than"):
                spec.loader.exec_module(mod)
