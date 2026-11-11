"""Tests for agents/fi_sim_operator/run.py (task 11.3a).

Fakes only; no real DynamoDB or AWS. Tests cover:
  - dry_run: no DDB writes                 (mutant L3)
  - approve: reserve_cap before write       (mutant L1)
  - approve: booking after grounding result (mutant L2)
  - person_first: conditional put blocked   (mutant L4)
  - fix (f): both pair Actions pre-validated before any write
  - fix (e): hold_vehicle_ids resolved in handler (tested in test_handler.py)

Mutant guards (in this file):
  L1: reserve after writing → cap exceeded silently
  L2: booking before grounding result → booking written for unfired grounding
  L3: dry_run writes → side effects in read-only mode
  L4: overwrite person's decision → concurrent approve race
"""
from __future__ import annotations

import hashlib
import uuid
from typing import Any, Optional
from unittest.mock import MagicMock, patch

import pytest

from agents.fi_sim_operator.run import (
    MODE_APPROVE,
    MODE_DRY_RUN,
    MODE_OBSERVE,
    FleetRunResult,
    UnitResult,
    _derive_action_id,
    _build_action,
    _execute_unit,
    run_fleet,
)
from agents.fi_sim_operator.plan import WorkUnit
from agents.fi_sim_operator.select import CLASS_FH_BOOK, CLASS_FH_GROUND_BOOK
from agents.executors.base import ExecuteResult, ActionStatus

_VIN = "MRDTEST0000000001"
_FLEET = "demo-lastmile-delivery"
_TABLE = "vsa-test-avx-sim-operator"
_ACTIONS = "vsa-test-avx-actions"
_TODAY = "2026-11-06"
_RUN_AT = "2026-11-06T07:10:00Z"
_CAP = 20


def _fid() -> str:
    return f"FIND-{uuid.uuid4()}"


def _fh_book_finding(fid: str = None) -> dict:
    fid = fid or _fid()
    return {
        "finding_id": fid,
        "agent_id": "agent-fi-fleet-health",
        "scope": {"vin": _VIN},
        "fleet_id": _FLEET,
        "severity": "attention",
        "computed_at": "2026-11-05T05:00:00Z",
        "expires_at": "2026-11-09T07:10:00Z",
        "recommended_action": {
            "action_kind": "book_fleet_service",
            "target_system": "dms",
            "parameters": {
                "vehicle_vin": _VIN,
                "needs": [{"need_id": "fault:P0001", "labor_op": "LBR-001"}],
                "dealer_id": "DLR-001",
                "urgency": "this_month",
            },
        },
        "evidence": [],
    }


def _fh_ground_finding(fid: str = None) -> dict:
    fid = fid or _fid()
    return {
        "finding_id": fid,
        "agent_id": "agent-fi-fleet-health",
        "scope": {"vin": _VIN},
        "fleet_id": _FLEET,
        "severity": "p0",
        "computed_at": "2026-11-05T05:00:00Z",
        "expires_at": "2026-11-09T07:10:00Z",
        "recommended_action": {
            "action_kind": "ground_vehicle",
            "target_system": "cms",
            "parameters": {"vehicle_vin": _VIN, "fault_codes": ["P0001"]},
        },
        "disclosure_txt": "Stop-driving P0001",
        "evidence": [],
    }


# ── Fake DDB with write log ───────────────────────────────────────────────────

class WritingFakeDdb:
    """Records all writes; supports conditional put to simulate person-first."""

    def __init__(self, *, person_already_decided: bool = False) -> None:
        self._store: dict = {}
        self.put_calls: list[str] = []
        self.update_calls: list[str] = []
        self.reserve_calls: list[tuple] = []
        self._person_already_decided = person_already_decided

    def put_item(self, TableName, Item, ConditionExpression=None, **kwargs):
        from botocore.exceptions import ClientError
        if "pk" in Item:
            sk = Item.get("sk", {}).get("S", "")
            self.reserve_calls.append((TableName, sk))
        else:
            aid = Item.get("action_id", {}).get("S", "")
            self.put_calls.append(aid)
            if self._person_already_decided and ConditionExpression == "attribute_not_exists(action_id)":
                error_response = {"Error": {"Code": "ConditionalCheckFailedException", "Message": ""}}
                raise ClientError(error_response, "PutItem")
        key = tuple(sorted((k, v.get("S") or v.get("N") or "") for k, v in Item.items()
                    if k in ("pk", "sk", "action_id")))
        self._store[str(key)] = Item
        return {}

    def update_item(self, TableName, Key, UpdateExpression, **kwargs):
        values = kwargs.get("ExpressionAttributeValues", {})
        if "count" in str(UpdateExpression) or ":n" in values:
            # CAP update
            pk = Key.get("pk", {}).get("S", "")
            sk = Key.get("sk", {}).get("S", "")
            n = int(values.get(":n", {}).get("N", "1"))
            cap = int(values.get(":cap", {}).get("N", str(_CAP)))
            existing = int(self._store.get(str((pk, sk, "count")), {}).get("count", {}).get("N", "0") or "0")
            if existing + n > cap:
                from botocore.exceptions import ClientError
                error_response = {"Error": {"Code": "ConditionalCheckFailedException", "Message": ""}}
                raise ClientError(error_response, "UpdateItem")
            self._store[str((pk, sk, "count"))] = {"count": {"N": str(existing + n)}}
            return {}
        else:
            aid = Key.get("action_id", {}).get("S", "")
            self.update_calls.append(aid)
            return {}

    def query(self, TableName, **kwargs):
        return {"Items": []}

    def get_item(self, TableName, Key, **kwargs):
        return {}


# ── Fake registry.dispatch ────────────────────────────────────────────────────

def _make_dispatch(status: str = "in_progress", target_ref: str = "REF-001"):
    """Return a fake dispatch that always succeeds."""
    def _dispatch(action, claims):
        return ExecuteResult.success(target_ref=target_ref, status=ActionStatus.IN_PROGRESS)
    return _dispatch


def _make_failed_dispatch():
    """Return a dispatch that returns terminal error."""
    def _dispatch(action, claims):
        return ExecuteResult.terminal_error(last_error_class="FakeError")
    return _dispatch


# ── _derive_action_id ──────────────────────────────────────────────────────────

class TestDeriveActionId:
    def test_deterministic(self):
        """Same finding_id → same action_id."""
        fid = "FIND-test-001"
        assert _derive_action_id(fid) == _derive_action_id(fid)

    def test_format(self):
        """action_id has AVX-ACT-xxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx format."""
        aid = _derive_action_id("FIND-test-001")
        assert aid.startswith("AVX-ACT-")
        parts = aid[8:].split("-")
        assert len(parts) == 5

    def test_one_decision_per_finding(self):
        """Two findings with different ids get different action_ids."""
        aid1 = _derive_action_id("FIND-001")
        aid2 = _derive_action_id("FIND-002")
        assert aid1 != aid2

    def test_matches_auto_approver_formula(self):
        """Uses the same sha256 formula as the auto-approver."""
        fid = "FIND-auto-approver-test-0001"
        raw = hashlib.sha256(f"{fid}|fi-decision".encode()).hexdigest()[:32]
        expected = (
            "AVX-ACT-"
            + raw[0:8] + "-" + raw[8:12] + "-" + raw[12:16]
            + "-" + raw[16:20] + "-" + raw[20:32]
        )
        assert _derive_action_id(fid) == expected


# ── _build_action ──────────────────────────────────────────────────────────────

class TestBuildAction:
    def test_build_fh_book_action(self):
        """book_fleet_service finding builds a valid Action."""
        f = _fh_book_finding()
        fid = f["finding_id"]
        action_id = _derive_action_id(fid)
        action, err = _build_action(f, action_id, _RUN_AT)
        assert err is None, f"Expected no error, got: {err}"
        assert action is not None
        assert action["action_kind"] == "book_fleet_service"
        assert action["decided_by"]["kind"] == "simulated_fleet_operator"
        assert action["decided_by"]["id"] == "simulated-fleet-operator"
        assert action["decision"] == "approved"
        assert action["status"] == "pending_execution"

    def test_build_ground_action_has_fault_codes(self):
        """ground_vehicle finding: parameters include fault_codes from the Finding."""
        f = _fh_ground_finding()
        fid = f["finding_id"]
        action_id = _derive_action_id(fid)
        action, err = _build_action(f, action_id, _RUN_AT)
        assert err is None
        assert action["parameters"].get("fault_codes") == ["P0001"]

    def test_build_action_returns_error_for_missing_needs(self):
        """A book_fleet_service finding with missing 'needs' → server_set error."""
        f = _fh_book_finding()
        f["recommended_action"]["parameters"].pop("needs", None)
        fid = f["finding_id"]
        action_id = _derive_action_id(fid)
        action, err = _build_action(f, action_id, _RUN_AT)
        assert err is not None
        assert action is None


# ── Dry run: no writes (mutant L3) ────────────────────────────────────────────

class TestDryRunNoWrites:
    """L3: dry_run must never call put_item or update_item on action/ledger tables.

    Mutation guard (mutant = dry_run writes): if dry_run wrote Actions, a
    scheduled Lambda invoked with mode=dry_run would permanently record decisions
    and consume cap — defeating the purpose of a preview mode.
    """

    def test_dry_run_returns_would_approve(self):
        """dry_run outcome is 'would_approve', no writes."""
        ddb = WritingFakeDdb()
        fid = _fid()
        f = _fh_book_finding(fid)
        unit = WorkUnit(
            findings=(f,), class_name=CLASS_FH_BOOK,
            first_seen_at="2026-11-05T07:10:00Z", decision_count=1,
        )
        with patch("agents.fi_sim_operator.run.reserve_cap") as mock_reserve, \
             patch("agents.fi_sim_operator.run.record_first_seen") as mock_seen, \
             patch("agents.fi_sim_operator.run._write_action") as mock_write:
            mock_reserve.return_value = True
            mock_seen.return_value = True
            mock_write.return_value = "written"
            ur = _execute_unit(
                unit, MODE_DRY_RUN, ddb, _ACTIONS, _TABLE, _FLEET, _TODAY, _RUN_AT, _CAP,
            )
        assert ur.outcome == "would_approve", (
            "dry_run must return 'would_approve'. "
            "Mutant L3: if dry_run calls _write_action, Actions are written."
        )
        mock_write.assert_not_called()
        mock_seen.assert_not_called()

    def test_dry_run_does_not_reserve_cap(self):
        """dry_run must not reserve cap (reads only)."""
        ddb = WritingFakeDdb()
        fid = _fid()
        f = _fh_book_finding(fid)
        unit = WorkUnit(
            findings=(f,), class_name=CLASS_FH_BOOK,
            first_seen_at="2026-11-05T07:10:00Z", decision_count=1,
        )
        with patch("agents.fi_sim_operator.run.reserve_cap") as mock_reserve:
            mock_reserve.return_value = True
            ur = _execute_unit(
                unit, MODE_DRY_RUN, ddb, _ACTIONS, _TABLE, _FLEET, _TODAY, _RUN_AT, _CAP,
            )
        mock_reserve.assert_not_called()


# ── Reserve cap before writing (mutant L1) ────────────────────────────────────

class TestReserveCapBeforeWrite:
    """L1: reserve_cap is called BEFORE any Action write.

    Mutation guard (mutant = reserve after writing):
      If reserve_cap is called after _write_action, the Action is written before
      the cap is checked. A concurrent run on the same fleet could see 0 cap used,
      both write their Actions, then both try to reserve — one succeeds, the other
      fails, but BOTH Actions were already written (cap exceeded by 1).
    """

    def test_cap_reserved_before_action_written(self):
        """reserve_cap is called; on success, _write_action is called next."""
        call_order: list[str] = []
        fid = _fid()
        f = _fh_book_finding(fid)
        unit = WorkUnit(
            findings=(f,), class_name=CLASS_FH_BOOK,
            first_seen_at="2026-11-05T07:10:00Z", decision_count=1,
        )

        def fake_reserve(*args, **kwargs):
            call_order.append("reserve")
            return True

        def fake_write(*args, **kwargs):
            call_order.append("write")
            return "written"

        def fake_dispatch(action, claims):
            call_order.append("dispatch")
            return ExecuteResult.success(target_ref="REF", status=ActionStatus.IN_PROGRESS)

        ddb = WritingFakeDdb()
        with patch("agents.fi_sim_operator.run.reserve_cap", fake_reserve), \
             patch("agents.fi_sim_operator.run.record_first_seen", return_value=True), \
             patch("agents.fi_sim_operator.run._write_action", fake_write), \
             patch("agents.fi_sim_operator.run._record_dispatch_result", return_value="in_progress"), \
             patch("agents.executors.registry.dispatch", fake_dispatch):
            _execute_unit(
                unit, MODE_APPROVE, ddb, _ACTIONS, _TABLE, _FLEET, _TODAY, _RUN_AT, _CAP,
            )

        assert call_order[0] == "reserve", (
            "reserve_cap must be called BEFORE _write_action. "
            "Mutant L1: 'write' before 'reserve' allows cap to be exceeded."
        )
        reserve_idx = call_order.index("reserve")
        write_idx = call_order.index("write")
        assert reserve_idx < write_idx, (
            f"reserve ({reserve_idx}) must precede write ({write_idx}). "
            "Call order was: " + str(call_order)
        )

    def test_cap_exceeded_returns_skip(self):
        """When reserve_cap returns False, the unit is skipped."""
        fid = _fid()
        f = _fh_book_finding(fid)
        unit = WorkUnit(
            findings=(f,), class_name=CLASS_FH_BOOK,
            first_seen_at="2026-11-05T07:10:00Z", decision_count=1,
        )
        ddb = WritingFakeDdb()
        with patch("agents.fi_sim_operator.run.reserve_cap", return_value=False), \
             patch("agents.fi_sim_operator.run._write_action") as mock_write:
            ur = _execute_unit(
                unit, MODE_APPROVE, ddb, _ACTIONS, _TABLE, _FLEET, _TODAY, _RUN_AT, _CAP,
            )
        assert ur.outcome == "skipped"
        mock_write.assert_not_called()


# ── Booking after grounding result (mutant L2) ────────────────────────────────

class TestBookingAfterGrounding:
    """L2: booking Action written ONLY AFTER grounding result is recorded.

    Mutation guard (mutant = booking before grounding result):
      If booking is written before the grounding dispatch completes, a booking
      could be written for a grounding that failed to dispatch — approving a
      service appointment without the safety grounding being in flight.
    """

    def test_booking_written_after_grounding_dispatch(self):
        """For a pair unit, grounding is dispatched before booking is written."""
        call_order: list[str] = []
        gid = _fid()
        bid = _fid()
        g = _fh_ground_finding(gid)
        b = _fh_book_finding(bid)
        unit = WorkUnit(
            findings=(g, b), class_name=CLASS_FH_GROUND_BOOK,
            first_seen_at="2026-11-05T07:10:00Z", decision_count=2,
        )

        def fake_write(ddb_client, actions_table, action):
            call_order.append(f"write_{action['action_id']}")
            return "written"

        def fake_record_dispatch(ddb_client, actions_table, action_id, result, **_kw):  # 11.3b item 9 adds target_system
            call_order.append(f"record_{action_id}")
            return "in_progress"

        ddb = WritingFakeDdb()
        with patch("agents.fi_sim_operator.run.reserve_cap", return_value=True), \
             patch("agents.fi_sim_operator.run.record_first_seen", return_value=True), \
             patch("agents.fi_sim_operator.run._write_action", fake_write), \
             patch("agents.fi_sim_operator.run._record_dispatch_result", fake_record_dispatch), \
             patch("agents.executors.registry.dispatch",
                   return_value=ExecuteResult.success(target_ref="REF", status=ActionStatus.IN_PROGRESS)):
            ur = _execute_unit(
                unit, MODE_APPROVE, ddb, _ACTIONS, _TABLE, _FLEET, _TODAY, _RUN_AT, _CAP,
            )

        grnd_action_id = _derive_action_id(gid)
        book_action_id = _derive_action_id(bid)
        grnd_write_idx = next(i for i, s in enumerate(call_order)
                              if s == f"write_{grnd_action_id}")
        grnd_record_idx = next(i for i, s in enumerate(call_order)
                               if s == f"record_{grnd_action_id}")
        book_write_idx = next(i for i, s in enumerate(call_order)
                              if s == f"write_{book_action_id}")
        assert grnd_write_idx < grnd_record_idx < book_write_idx, (
            f"Order must be: write_grnd ({grnd_write_idx}) → "
            f"record_grnd ({grnd_record_idx}) → write_book ({book_write_idx}). "
            "Mutant L2: booking before grounding result."
        )
        assert ur.outcome == "approved"
        assert ur.grounding_status == "in_progress"

    def test_booking_skipped_when_grounding_not_attempted(self):
        """If grounding dispatch returns an unexpected status, booking is skipped."""
        gid = _fid()
        bid = _fid()
        g = _fh_ground_finding(gid)
        b = _fh_book_finding(bid)
        unit = WorkUnit(
            findings=(g, b), class_name=CLASS_FH_GROUND_BOOK,
            first_seen_at="2026-11-05T07:10:00Z", decision_count=2,
        )
        ddb = WritingFakeDdb()
        with patch("agents.fi_sim_operator.run.reserve_cap", return_value=True), \
             patch("agents.fi_sim_operator.run.record_first_seen", return_value=True), \
             patch("agents.fi_sim_operator.run._write_action", return_value="written"), \
             patch("agents.fi_sim_operator.run._record_dispatch_result",
                   return_value="declined"), \
             patch("agents.executors.registry.dispatch",
                   return_value=ExecuteResult.refused()):
            ur = _execute_unit(
                unit, MODE_APPROVE, ddb, _ACTIONS, _TABLE, _FLEET, _TODAY, _RUN_AT, _CAP,
            )
        # Grounding declined → booking not written
        assert ur.booking_status is None or ur.booking_status == "declined"


# ── Person first: conditional put blocked (mutant L4) ────────────────────────

class TestPersonFirstL4:
    """L4: a person's prior decision must not be overwritten.

    The conditional put uses ``attribute_not_exists(action_id)``; on
    ConditionalCheckFailedException the outcome is 'person_first' (not an error).

    Mutation guard (mutant = overwrite person's decision):
      Without the conditional put, the sim-operator would silently replace a
      person's approve/decline with its own, stealing their decision.
    """

    def test_person_already_decided_returns_person_first(self):
        """When conditional put fails, outcome is 'person_first', not 'approved'.

        Mutant L4: without ``attribute_not_exists``, the put always succeeds and
        the person's decision is overwritten.
        """
        fid = _fid()
        f = _fh_book_finding(fid)
        unit = WorkUnit(
            findings=(f,), class_name=CLASS_FH_BOOK,
            first_seen_at="2026-11-05T07:10:00Z", decision_count=1,
        )
        ddb = WritingFakeDdb(person_already_decided=True)
        with patch("agents.fi_sim_operator.run.reserve_cap", return_value=True), \
             patch("agents.fi_sim_operator.run.record_first_seen", return_value=False):
            ur = _execute_unit(
                unit, MODE_APPROVE, ddb, _ACTIONS, _TABLE, _FLEET, _TODAY, _RUN_AT, _CAP,
            )
        assert ur.outcome == "person_first", (
            "When a person already decided this Finding, outcome must be 'person_first'. "
            "Mutant L4: without attribute_not_exists, the sim-operator would overwrite "
            "the person's decision."
        )


# ── Fix (f): both pair Actions pre-validated before any write ─────────────────

class TestPairPreValidationFixF:
    """(f) Both Actions in a pair pass server_set rule before either is written.

    If the booking has a missing server_set field (e.g. 'needs' in the
    recommendation), neither the grounding nor the booking is written.
    This prevents a grounding from being written for a booking that would fail.
    """

    def test_booking_server_set_error_skips_whole_pair(self):
        """A booking finding with missing 'needs' causes the pair to be skipped."""
        gid = _fid()
        bid = _fid()
        g = _fh_ground_finding(gid)
        # Booking with missing needs (server_set error)
        b_invalid = {
            "finding_id": bid,
            "agent_id": "agent-fi-fleet-health",
            "scope": {"vin": _VIN},
            "fleet_id": _FLEET,
            "severity": "attention",
            "computed_at": "2026-11-05T05:10:00Z",
            "expires_at": "2026-11-09T07:10:00Z",
            "recommended_action": {
                "action_kind": "book_fleet_service",
                "target_system": "dms",
                "parameters": {
                    "vehicle_vin": _VIN,
                    # 'needs' is missing — server_set error
                    "dealer_id": "DLR-001",
                    "urgency": "this_month",
                },
            },
            "evidence": [],
        }
        unit = WorkUnit(
            findings=(g, b_invalid), class_name=CLASS_FH_GROUND_BOOK,
            first_seen_at="2026-11-05T07:10:00Z", decision_count=2,
        )
        ddb = WritingFakeDdb()
        with patch("agents.fi_sim_operator.run.reserve_cap") as mock_reserve, \
             patch("agents.fi_sim_operator.run._write_action") as mock_write:
            mock_reserve.return_value = True
            ur = _execute_unit(
                unit, MODE_APPROVE, ddb, _ACTIONS, _TABLE, _FLEET, _TODAY, _RUN_AT, _CAP,
            )
        assert ur.outcome == "skipped", (
            "When the booking fails server_set validation, the whole pair must be skipped. "
            "Fix (f): no grounding should be written for a booking that would fail."
        )
        mock_write.assert_not_called()

    def test_grounding_server_set_error_skips_unit(self):
        """A grounding with a server_set error (e.g. fault_codes missing) is skipped."""
        gid = _fid()
        # Grounding with missing fault_codes and no recall_id (server_set error)
        g_invalid = {
            "finding_id": gid,
            "agent_id": "agent-fi-fleet-health",
            "scope": {"vin": _VIN},
            "fleet_id": _FLEET,
            "severity": "p0",
            "computed_at": "2026-11-05T05:00:00Z",
            "expires_at": "2026-11-09T07:10:00Z",
            "recommended_action": {
                "action_kind": "ground_vehicle",
                "target_system": "cms",
                "parameters": {
                    "vehicle_vin": _VIN,
                    # Neither fault_codes nor recall_id — server_set will error
                },
            },
            "evidence": [],
        }
        unit = WorkUnit(
            findings=(g_invalid,), class_name=CLASS_FH_BOOK,
            first_seen_at="2026-11-05T07:10:00Z", decision_count=1,
        )
        ddb = WritingFakeDdb()
        with patch("agents.fi_sim_operator.run._write_action") as mock_write:
            ur = _execute_unit(
                unit, MODE_APPROVE, ddb, _ACTIONS, _TABLE, _FLEET, _TODAY, _RUN_AT, _CAP,
            )
        assert ur.outcome == "skipped"
        mock_write.assert_not_called()


# ── run_fleet aggregation ──────────────────────────────────────────────────────

class TestRunFleet:
    def test_run_fleet_dry_run_returns_would_approve(self):
        """run_fleet in dry_run mode returns would_approve counts."""
        fid = _fid()
        f = _fh_book_finding(fid)
        unit = WorkUnit(
            findings=(f,), class_name=CLASS_FH_BOOK,
            first_seen_at="2026-11-05T07:10:00Z", decision_count=1,
        )
        ddb = WritingFakeDdb()
        result = run_fleet(
            fleet_id=_FLEET,
            plan_units=[unit],
            plan_skip_counts={},
            mode=MODE_DRY_RUN,
            ddb_client=ddb,
            actions_table=_ACTIONS,
            ledger_table=_TABLE,
            today=_TODAY,
            run_at=_RUN_AT,
            daily_cap=_CAP,
        )
        assert result.units_would_approve == 1
        assert result.units_approved == 0
        assert result.decisions_written == 0

    def test_run_fleet_approve_counts_decisions(self):
        """run_fleet approve mode: decisions_written matches approved units."""
        fid = _fid()
        f = _fh_book_finding(fid)
        unit = WorkUnit(
            findings=(f,), class_name=CLASS_FH_BOOK,
            first_seen_at="2026-11-05T07:10:00Z", decision_count=1,
        )
        ddb = WritingFakeDdb()
        with patch("agents.fi_sim_operator.run.reserve_cap", return_value=True), \
             patch("agents.fi_sim_operator.run.record_first_seen", return_value=True), \
             patch("agents.fi_sim_operator.run._write_action", return_value="written"), \
             patch("agents.fi_sim_operator.run._record_dispatch_result", return_value="in_progress"), \
             patch("agents.executors.registry.dispatch",
                   return_value=ExecuteResult.success(target_ref="REF", status=ActionStatus.IN_PROGRESS)):
            result = run_fleet(
                fleet_id=_FLEET,
                plan_units=[unit],
                plan_skip_counts={},
                mode=MODE_APPROVE,
                ddb_client=ddb,
                actions_table=_ACTIONS,
                ledger_table=_TABLE,
                today=_TODAY,
                run_at=_RUN_AT,
                daily_cap=_CAP,
            )
        assert result.units_approved == 1
        assert result.decisions_written == 1

    def test_run_fleet_skip_counts_propagated(self):
        """Pre-plan skip counts are included in the fleet result."""
        ddb = WritingFakeDdb()
        result = run_fleet(
            fleet_id=_FLEET,
            plan_units=[],
            plan_skip_counts={"stale": 5, "expired": 3},
            mode=MODE_DRY_RUN,
            ddb_client=ddb,
            actions_table=_ACTIONS,
            ledger_table=_TABLE,
            today=_TODAY,
            run_at=_RUN_AT,
            daily_cap=_CAP,
        )
        assert result.skip_counts.get("stale") == 5
        assert result.skip_counts.get("expired") == 3


# ── W3: accurate decisions_written and grounding_not_made counter ─────────────

class TestW3RunFleetCounts:
    """W3: run_fleet counts Actions actually written, not unit.decision_count.

    Reviewer probe P2: CMS refuses the grounding (status 'declined', not in
    _GROUNDING_MADE_STATUSES). Outcome is 'approved' with skip_reason
    'grounding_not_made'. decisions_written must be 1, not 2; grounding_not_made
    must be 1.

    Mutant K3: if grounding_not_made is counted as 2 (unit.decision_count for a
    pair), test_grounding_not_made_counts_one_decision catches it.
    """

    def _pair_unit(self):
        gid = _fid()
        bid = _fid()
        g = _fh_ground_finding(gid)
        b = _fh_book_finding(bid)
        return WorkUnit(
            findings=(g, b), class_name=CLASS_FH_GROUND_BOOK,
            first_seen_at="2026-11-05T07:10:00Z", decision_count=2,
        ), _derive_action_id(gid), _derive_action_id(bid)

    def test_grounding_not_made_counts_one_decision(self):
        """K3 guard: grounding refused → decisions_written=1, grounding_not_made=1."""
        unit, grnd_aid, book_aid = self._pair_unit()
        ddb = WritingFakeDdb()
        with patch("agents.fi_sim_operator.run.reserve_cap", return_value=True), \
             patch("agents.fi_sim_operator.run.record_first_seen", return_value=True), \
             patch("agents.fi_sim_operator.run._write_action", return_value="written"), \
             patch("agents.fi_sim_operator.run._record_dispatch_result",
                   return_value="declined"):
            result = run_fleet(
                fleet_id=_FLEET,
                plan_units=[unit],
                plan_skip_counts={},
                mode=MODE_APPROVE,
                ddb_client=ddb,
                actions_table=_ACTIONS,
                ledger_table=_TABLE,
                today=_TODAY,
                run_at=_RUN_AT,
                daily_cap=_CAP,
            )
        assert result.decisions_written == 1, (
            "K3: grounding_not_made must count 1 written Action, not 2. "
            f"Got decisions_written={result.decisions_written}"
        )
        assert result.grounding_not_made == 1, (
            f"grounding_not_made counter must be 1; got {result.grounding_not_made}"
        )
        assert result.units_approved == 1
        # executor_declined is incremented from grounding_status
        assert result.executor_declined == 1

    def test_booking_person_first_counts_one_decision(self):
        """Grounding written + dispatched, booking person-first: 1 written, not 2."""
        unit, grnd_aid, book_aid = self._pair_unit()
        ddb = WritingFakeDdb()

        call_count = {"n": 0}

        def fake_write(client, table, action):
            call_count["n"] += 1
            # First call (grounding): written; second call (booking): person_first
            return "written" if call_count["n"] == 1 else "person_first"

        with patch("agents.fi_sim_operator.run.reserve_cap", return_value=True), \
             patch("agents.fi_sim_operator.run.record_first_seen", return_value=True), \
             patch("agents.fi_sim_operator.run._write_action", fake_write), \
             patch("agents.fi_sim_operator.run._record_dispatch_result",
                   return_value="in_progress"), \
             patch("agents.executors.registry.dispatch",
                   return_value=ExecuteResult.success(target_ref="REF", status=ActionStatus.IN_PROGRESS)):
            result = run_fleet(
                fleet_id=_FLEET,
                plan_units=[unit],
                plan_skip_counts={},
                mode=MODE_APPROVE,
                ddb_client=ddb,
                actions_table=_ACTIONS,
                ledger_table=_TABLE,
                today=_TODAY,
                run_at=_RUN_AT,
                daily_cap=_CAP,
            )
        assert result.decisions_written == 1
        assert result.booking_person_first == 1
        assert result.grounding_not_made == 0

    def test_fully_approved_pair_counts_two_decisions(self):
        """Both grounding and booking written: decisions_written=2."""
        unit, grnd_aid, book_aid = self._pair_unit()
        ddb = WritingFakeDdb()
        with patch("agents.fi_sim_operator.run.reserve_cap", return_value=True), \
             patch("agents.fi_sim_operator.run.record_first_seen", return_value=True), \
             patch("agents.fi_sim_operator.run._write_action", return_value="written"), \
             patch("agents.fi_sim_operator.run._record_dispatch_result",
                   return_value="in_progress"), \
             patch("agents.executors.registry.dispatch",
                   return_value=ExecuteResult.success(target_ref="REF", status=ActionStatus.IN_PROGRESS)):
            result = run_fleet(
                fleet_id=_FLEET,
                plan_units=[unit],
                plan_skip_counts={},
                mode=MODE_APPROVE,
                ddb_client=ddb,
                actions_table=_ACTIONS,
                ledger_table=_TABLE,
                today=_TODAY,
                run_at=_RUN_AT,
                daily_cap=_CAP,
            )
        assert result.decisions_written == 2
        assert result.grounding_not_made == 0
        assert result.booking_person_first == 0

    def test_singleton_approved_counts_one_decision(self):
        """Singleton (fh_book) approved: decisions_written=1."""
        fid = _fid()
        f = _fh_book_finding(fid)
        unit = WorkUnit(
            findings=(f,), class_name=CLASS_FH_BOOK,
            first_seen_at="2026-11-05T07:10:00Z", decision_count=1,
        )
        ddb = WritingFakeDdb()
        with patch("agents.fi_sim_operator.run.reserve_cap", return_value=True), \
             patch("agents.fi_sim_operator.run.record_first_seen", return_value=True), \
             patch("agents.fi_sim_operator.run._write_action", return_value="written"), \
             patch("agents.fi_sim_operator.run._record_dispatch_result",
                   return_value="executed"), \
             patch("agents.executors.registry.dispatch",
                   return_value=ExecuteResult.success(target_ref="REF", status=ActionStatus.IN_PROGRESS)):
            result = run_fleet(
                fleet_id=_FLEET,
                plan_units=[unit],
                plan_skip_counts={},
                mode=MODE_APPROVE,
                ddb_client=ddb,
                actions_table=_ACTIONS,
                ledger_table=_TABLE,
                today=_TODAY,
                run_at=_RUN_AT,
                daily_cap=_CAP,
            )
        assert result.decisions_written == 1
        assert result.executor_executed == 1


# ── S4: failed _record_dispatch_result counts in units_error ──────────────────

class TestS4RecordDispatchError:
    """S4: a failed DDB status update in _record_dispatch_result → units_error.

    Previously _record_dispatch_result swallowed the ClientError with log.warning.
    Now it re-raises; _execute_unit does not catch it; run_fleet's except clause
    catches it and creates outcome='error', so units_error is incremented.
    """

    def test_failed_dispatch_record_counts_units_error(self):
        """When _record_dispatch_result raises, units_error is incremented."""
        from botocore.exceptions import ClientError

        fid = _fid()
        f = _fh_book_finding(fid)
        unit = WorkUnit(
            findings=(f,), class_name=CLASS_FH_BOOK,
            first_seen_at="2026-11-05T07:10:00Z", decision_count=1,
        )
        ddb = WritingFakeDdb()

        def raise_client_error(*args, **kwargs):
            error_response = {"Error": {"Code": "ProvisionedThroughputExceededException",
                                        "Message": "Rate exceeded"}}
            raise ClientError(error_response, "UpdateItem")

        with patch("agents.fi_sim_operator.run.reserve_cap", return_value=True), \
             patch("agents.fi_sim_operator.run.record_first_seen", return_value=True), \
             patch("agents.fi_sim_operator.run._write_action", return_value="written"), \
             patch("agents.fi_sim_operator.run._record_dispatch_result",
                   raise_client_error), \
             patch("agents.executors.registry.dispatch",
                   return_value=ExecuteResult.success(target_ref="REF", status=ActionStatus.IN_PROGRESS)):
            result = run_fleet(
                fleet_id=_FLEET,
                plan_units=[unit],
                plan_skip_counts={},
                mode=MODE_APPROVE,
                ddb_client=ddb,
                actions_table=_ACTIONS,
                ledger_table=_TABLE,
                today=_TODAY,
                run_at=_RUN_AT,
                daily_cap=_CAP,
            )
        assert result.units_error == 1, (
            "S4: a failed _record_dispatch_result must count in units_error so "
            f"the Errors alarm fires; got units_error={result.units_error}"
        )
        assert result.units_approved == 0
