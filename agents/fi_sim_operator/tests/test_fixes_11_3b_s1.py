"""11.3b items 1, 2, 3 (run.py side), 5 (run.py side), 8 (plan side) and 9.

Architect's read of the 11.3 first pass (4a056b00). Fakes only; no AWS.
Mutants (records/11/mutants-11.3b-s1.md):
  B1  put ``pending_execution`` back into the made-grounding statuses
  B2  observe reserves cap and runs units
  B3  an unknown mode writes (the unit runs as approve)
  B5  one unit's exception stops the fleet
  B8  the pair-booking de-dup in build_plan dropped
  B9  the audit ``by`` built from ``result.status``
"""
from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import patch

import pytest

from agents.executors.base import ActionStatus, ExecuteResult
from agents.fi_sim_operator.plan import SKIP_PAIRED, WorkUnit, build_plan
from agents.fi_sim_operator.run import (
    MODE_APPROVE,
    MODE_OBSERVE,
    _derive_action_id,
    _execute_unit,
    run_fleet,
)
from agents.fi_sim_operator.select import (
    CLASS_FH_BOOK,
    CLASS_FH_GROUND_BOOK,
    CLASS_RECALL_BOOK,
    CLASS_RECALL_GROUND_BOOK,
    ClassifiedFinding,
)

_VIN = "MRDTEST0000000001"
_FLEET = "demo-lastmile-delivery"
_LEDGER = "vsa-test-avx-sim-operator"
_ACTIONS = "vsa-test-avx-actions"
_TODAY = "2026-11-06"
_RUN_AT = "2026-11-06T07:10:00Z"
_OLD = "2026-11-05T07:00:00Z"   # first seen > 20 h before _RUN_AT
_CAP = 20


def _fid() -> str:
    return f"FIND-{uuid.uuid4()}"


def _ground(fid: str) -> dict:
    return {
        "finding_id": fid, "agent_id": "agent-fi-fleet-health", "scope": {"vin": _VIN},
        "fleet_id": _FLEET, "severity": "p0", "computed_at": "2026-11-06T05:40:00Z",
        "expires_at": "2026-11-09T07:10:00Z", "disclosure_txt": "Stop driving",
        "recommended_action": {"action_kind": "ground_vehicle", "target_system": "cms",
                               "parameters": {"vehicle_vin": _VIN, "fault_codes": ["P0001"]}},
        "evidence": [],
    }


def _book(fid: str) -> dict:
    return {
        "finding_id": fid, "agent_id": "agent-fi-fleet-health", "scope": {"vin": _VIN},
        "fleet_id": _FLEET, "severity": "p0", "computed_at": "2026-11-06T05:40:00Z",
        "expires_at": "2026-11-09T07:10:00Z", "disclosure_txt": "Stop driving",
        "recommended_action": {"action_kind": "book_fleet_service", "target_system": "dms",
                               "parameters": {"vehicle_vin": _VIN, "dealer_id": "dealer-austin",
                                              "urgency": "this_week",
                                              "needs": [{"need_id": "fault:P0001",
                                                         "labor_op": "LBR-001"}]}},
        "evidence": [],
    }


class _Ddb:
    """Records every call; CAP updates and Action puts always succeed."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def put_item(self, **kw: Any) -> dict:
        self.calls.append(("put_item", kw))
        return {}

    def update_item(self, **kw: Any) -> dict:
        self.calls.append(("update_item", kw))
        return {}

    def writes(self) -> list[tuple[str, dict]]:
        return [c for c in self.calls if c[0] in ("put_item", "update_item")]

    def action_puts(self) -> list[str]:
        return [c[1]["Item"]["action_id"]["S"] for c in self.calls
                if c[0] == "put_item" and "action_id" in c[1].get("Item", {})]


class _Result:
    """An executor result whose status the test chooses."""

    def __init__(self, status: str) -> None:
        self.status = status
        self.target_ref = None

    def audit_entry(self, *, by: str, at: str) -> dict:
        return {"at": at, "by": by, "to_status": self.status}


def _pair_unit(gid: str, bid: str) -> WorkUnit:
    return WorkUnit(findings=(_ground(gid), _book(bid)), class_name=CLASS_FH_GROUND_BOOK,
                    first_seen_at=_OLD, decision_count=2)


def _single_unit(fid: str) -> WorkUnit:
    f = _book(fid)
    f["severity"] = "attention"
    return WorkUnit(findings=(f,), class_name=CLASS_FH_BOOK, first_seen_at=_OLD, decision_count=1)


# ── item 1: a booking follows only a MADE grounding ─────────────────────────────

class TestBookingOnlyAfterMadeGrounding:
    @pytest.mark.parametrize("status", ["pending_execution", "declined", "failed"])
    def test_no_booking_after_unmade_grounding(self, status):
        gid, bid = _fid(), _fid()
        ddb = _Ddb()
        with patch("agents.executors.registry.dispatch", return_value=_Result(status)):
            ur = _execute_unit(_pair_unit(gid, bid), MODE_APPROVE, ddb, _ACTIONS, _LEDGER,
                               _FLEET, _TODAY, _RUN_AT, _CAP)
        assert ddb.action_puts() == [_derive_action_id(gid)]
        assert ur.booking_status is None
        assert ur.skip_reason == "grounding_not_made"

    @pytest.mark.parametrize("status", ["in_progress", "executed", "completed"])
    def test_booking_after_made_grounding(self, status):
        gid, bid = _fid(), _fid()
        ddb = _Ddb()
        with patch("agents.executors.registry.dispatch", return_value=_Result(status)):
            ur = _execute_unit(_pair_unit(gid, bid), MODE_APPROVE, ddb, _ACTIONS, _LEDGER,
                               _FLEET, _TODAY, _RUN_AT, _CAP)
        assert ddb.action_puts() == [_derive_action_id(gid), _derive_action_id(bid)]
        assert ur.skip_reason is None


# ── items 2 and 3: observe and unknown modes write nothing ─────────────────────

class TestObserveAndUnknownModes:
    def test_observe_runs_no_units_and_reserves_no_cap(self):
        ddb = _Ddb()
        dispatched: list = []
        with patch("agents.executors.registry.dispatch",
                   side_effect=lambda a, c: dispatched.append(a) or _Result("in_progress")):
            res = run_fleet(_FLEET, [_single_unit(_fid()), _pair_unit(_fid(), _fid())], {"held": 2},
                            MODE_OBSERVE, ddb, _ACTIONS, _LEDGER, _TODAY, _RUN_AT, _CAP)
        assert ddb.writes() == []
        assert dispatched == []
        assert res.unit_results == []
        assert res.skip_counts == {"held": 2}

    @pytest.mark.parametrize("mode", ["observe", "aprove", "APPROVE", "", "dry-run"])
    def test_execute_unit_refuses_every_mode_but_approve_and_dry_run(self, mode):
        ddb = _Ddb()
        with patch("agents.executors.registry.dispatch", return_value=_Result("in_progress")):
            with pytest.raises(ValueError):
                _execute_unit(_single_unit(_fid()), mode, ddb, _ACTIONS, _LEDGER,
                              _FLEET, _TODAY, _RUN_AT, _CAP)
        assert ddb.writes() == []

    def test_unknown_mode_in_run_fleet_writes_nothing_and_counts_errors(self):
        ddb = _Ddb()
        res = run_fleet(_FLEET, [_single_unit(_fid())], {}, "aprove", ddb,
                        _ACTIONS, _LEDGER, _TODAY, _RUN_AT, _CAP)
        assert ddb.writes() == []
        assert res.units_error == 1


# ── item 5: one unit's error doesn't stop the fleet ────────────────────────────

class TestPerUnitErrors:
    def test_error_counted_and_next_unit_runs(self):
        bad, good = _fid(), _fid()
        ddb = _Ddb()

        def _dispatch(action, claims):
            if action["finding_id"] == bad:
                raise RuntimeError("boom")
            return ExecuteResult.success(target_ref="RO-1", status=ActionStatus.IN_PROGRESS)

        with patch("agents.executors.registry.dispatch", side_effect=_dispatch):
            res = run_fleet(_FLEET, [_single_unit(bad), _single_unit(good)], {}, MODE_APPROVE,
                            ddb, _ACTIONS, _LEDGER, _TODAY, _RUN_AT, _CAP)
        assert res.units_error == 1
        assert res.units_approved == 1
        assert [u.outcome for u in res.unit_results] == ["error", "approved"]
        assert res.unit_results[0].error == "RuntimeError"
        assert _derive_action_id(good) in ddb.action_puts()


# ── item 8: a pair's booking never runs as its own unit ────────────────────────

def _cf(f: dict, cls: str) -> ClassifiedFinding:
    return ClassifiedFinding(finding=f, class_name=cls, skip_reason=None)


class TestPairBookingDeDup:
    def test_standalone_booking_that_a_pair_books_is_dropped(self):
        gid, bid = _fid(), _fid()
        g, b = _ground(gid), _book(bid)
        plan = build_plan(
            classified=[_cf(g, CLASS_RECALL_GROUND_BOOK), _cf(b, CLASS_RECALL_BOOK)],
            paired_bookings={gid: (b, None)},
            first_seen={gid: _OLD, bid: _OLD},
            run_at=_RUN_AT, remaining_cap=_CAP,
        )
        assert [[f["finding_id"] for f in u.findings] for u in plan.units] == [[gid, bid]]
        assert plan.skip_counts.get(SKIP_PAIRED) == 1

    def test_two_pairs_naming_one_booking_run_once(self):
        g1, g2, bid = _fid(), _fid(), _fid()
        b = _book(bid)
        plan = build_plan(
            classified=[_cf(_ground(g1), CLASS_FH_GROUND_BOOK), _cf(_ground(g2), CLASS_FH_GROUND_BOOK)],
            paired_bookings={g1: (b, None), g2: (b, None)},
            first_seen={g1: _OLD, g2: _OLD, bid: _OLD},
            run_at=_RUN_AT, remaining_cap=_CAP,
        )
        assert [u.findings[1]["finding_id"] for u in plan.units] == [bid]
        assert plan.skip_counts.get(SKIP_PAIRED) == 1

    def test_unrelated_standalone_booking_still_runs(self):
        gid, bid, other = _fid(), _fid(), _fid()
        plan = build_plan(
            classified=[_cf(_ground(gid), CLASS_RECALL_GROUND_BOOK), _cf(_book(other), CLASS_RECALL_BOOK)],
            paired_bookings={gid: (_book(bid), None)},
            first_seen={gid: _OLD, other: _OLD, bid: _OLD},
            run_at=_RUN_AT, remaining_cap=_CAP,
        )
        assert sorted(len(u.findings) for u in plan.units) == [1, 2]
        assert SKIP_PAIRED not in plan.skip_counts


# ── item 9: the audit entry names the Action's target system ───────────────────

class TestAuditByTargetSystem:
    def test_audit_by_is_executor_of_each_actions_target(self):
        gid, bid = _fid(), _fid()
        ddb = _Ddb()
        with patch("agents.executors.registry.dispatch", return_value=_Result("in_progress")):
            _execute_unit(_pair_unit(gid, bid), MODE_APPROVE, ddb, _ACTIONS, _LEDGER,
                          _FLEET, _TODAY, _RUN_AT, _CAP)
        by = {}
        for name, kw in ddb.calls:
            if name == "update_item" and kw.get("TableName") == _ACTIONS:
                entry = kw["ExpressionAttributeValues"][":entry"]["L"][0]["M"]
                by[kw["Key"]["action_id"]["S"]] = entry["by"]["S"]
        assert by == {_derive_action_id(gid): "executor-cms", _derive_action_id(bid): "executor-dms"}
