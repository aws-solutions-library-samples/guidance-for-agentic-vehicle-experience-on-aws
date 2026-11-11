"""Tests for agents/fi_sim_operator/plan.py (task 11.2d).

Covers work units, eligibility (20 h rule), ordering, cap enforcement, and
mutants S4 (cap off by one), S5 (pair split at cap), S6 (too_new dropped).
"""
from __future__ import annotations

import uuid

import pytest

from agents.fi_sim_operator.plan import (
    Plan,
    WorkUnit,
    build_plan,
    _MIN_AGE_HOURS,
)
from agents.fi_sim_operator.select import (
    CLASS_FH_BOOK,
    CLASS_FH_GROUND_BOOK,
    CLASS_RECALL_GROUND_BOOK,
    CLASS_WARRANTY_FILE,
    ClassifiedFinding,
    SKIP_CAP_REACHED,
    SKIP_TOO_NEW,
)

_NOW = "2026-11-06T07:10:00Z"
_VIN = "MRDTEST0000000001"
_VIN2 = "MRDTEST0000000002"
_FLEET = "demo-lastmile-delivery"


def _fid() -> str:
    return f"FIND-{uuid.uuid4()}"


def _finding(
    *,
    finding_id: str | None = None,
    vin: str = _VIN,
    severity: str = "attention",
    action_kind: str = "book_fleet_service",
    target_system: str = "dms",
    computed_at: str = "2026-11-05T05:00:00Z",
    expires_at: str = "2026-11-09T07:10:00Z",
) -> dict:
    return {
        "finding_id": finding_id or _fid(),
        "agent_id": "agent-fi-fleet-health",
        "scope": {"vin": vin},
        "fleet_id": _FLEET,
        "severity": severity,
        "computed_at": computed_at,
        "expires_at": expires_at,
        "recommended_action": {
            "action_kind": action_kind,
            "target_system": target_system,
            "parameters": {"vehicle_vin": vin},
        },
        "evidence": [],
    }


def _cf(finding: dict, class_name: str = CLASS_FH_BOOK) -> ClassifiedFinding:
    return ClassifiedFinding(finding=finding, class_name=class_name, skip_reason=None)


def _cf_skip(finding: dict, reason: str) -> ClassifiedFinding:
    return ClassifiedFinding(finding=finding, class_name=None, skip_reason=reason)


def _old_enough(hours_ago: float = 22.0) -> str:
    """Return a first_seen_at that is hours_ago before _NOW."""
    from datetime import datetime, timezone, timedelta
    dt = datetime.fromisoformat(_NOW.replace("Z", "+00:00"))
    return (dt - timedelta(hours=hours_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _too_new(hours_ago: float = 5.0) -> str:
    """Return a first_seen_at that is hours_ago < 20 before _NOW."""
    from datetime import datetime, timezone, timedelta
    dt = datetime.fromisoformat(_NOW.replace("Z", "+00:00"))
    return (dt - timedelta(hours=hours_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


class TestEligibility:
    def test_eligible_when_first_seen_20h_or_more_ago(self):
        """first_seen_at >= 20h before run → eligible (one unit in plan)."""
        fid = _fid()
        f = _finding(finding_id=fid)
        plan = build_plan(
            classified=[_cf(f)],
            paired_bookings={},
            first_seen={fid: _old_enough(22)},
            run_at=_NOW,
            remaining_cap=5,
        )
        assert len(plan.units) == 1

    def test_ineligible_when_first_seen_less_than_20h_ago(self):
        """first_seen_at < 20h before run → too_new (mutant guard S6).

        If the 20 h gate were dropped, findings from today's scan would be
        approved on the same run they were first seen.
        """
        fid = _fid()
        f = _finding(finding_id=fid)
        plan = build_plan(
            classified=[_cf(f)],
            paired_bookings={},
            first_seen={fid: _too_new(5)},
            run_at=_NOW,
            remaining_cap=5,
        )
        assert len(plan.units) == 0
        assert plan.skip_counts.get(SKIP_TOO_NEW, 0) >= 1

    def test_absent_from_ledger_is_too_new(self):
        """Finding not in first_seen → treated as first-seen now → too_new."""
        fid = _fid()
        f = _finding(finding_id=fid)
        plan = build_plan(
            classified=[_cf(f)],
            paired_bookings={},
            first_seen={},  # empty
            run_at=_NOW,
            remaining_cap=5,
        )
        assert len(plan.units) == 0
        assert plan.skip_counts.get(SKIP_TOO_NEW, 0) >= 1

    def test_exactly_20h_boundary_is_eligible(self):
        """Exactly 20h before run is eligible (inclusive boundary)."""
        fid = _fid()
        f = _finding(finding_id=fid)
        plan = build_plan(
            classified=[_cf(f)],
            paired_bookings={},
            first_seen={fid: _old_enough(20)},
            run_at=_NOW,
            remaining_cap=5,
        )
        assert len(plan.units) == 1


class TestOrdering:
    def test_oldest_first_seen_comes_first(self):
        """Oldest first_seen_at → first in plan."""
        fid_old = _fid()
        fid_new = _fid()
        f_old = _finding(finding_id=fid_old)
        f_new = _finding(finding_id=fid_new)
        plan = build_plan(
            classified=[_cf(f_new), _cf(f_old)],
            paired_bookings={},
            first_seen={
                fid_old: _old_enough(30),
                fid_new: _old_enough(22),
            },
            run_at=_NOW,
            remaining_cap=5,
        )
        assert len(plan.units) == 2
        assert plan.units[0].findings[0]["finding_id"] == fid_old

    def test_p0_before_non_p0_same_first_seen(self):
        """Within the same first_seen bucket, p0 precedes non-p0."""
        fid_p0 = _fid()
        fid_att = _fid()
        f_p0 = _finding(finding_id=fid_p0, severity="p0")
        f_att = _finding(finding_id=fid_att, severity="attention")
        same_time = _old_enough(22)
        plan = build_plan(
            classified=[_cf(f_att), _cf(f_p0)],
            paired_bookings={},
            first_seen={fid_p0: same_time, fid_att: same_time},
            run_at=_NOW,
            remaining_cap=5,
        )
        assert len(plan.units) == 2
        assert plan.units[0].findings[0]["finding_id"] == fid_p0

    def test_finding_id_tiebreaker(self):
        """Same first_seen and same severity → alphabetically earlier finding_id first."""
        fid_a = "FIND-00000000-0000-0000-0000-000000000001"
        fid_b = "FIND-00000000-0000-0000-0000-000000000002"
        f_a = _finding(finding_id=fid_a)
        f_b = _finding(finding_id=fid_b)
        same_time = _old_enough(22)
        plan = build_plan(
            classified=[_cf(f_b), _cf(f_a)],
            paired_bookings={},
            first_seen={fid_a: same_time, fid_b: same_time},
            run_at=_NOW,
            remaining_cap=5,
        )
        assert len(plan.units) == 2
        assert plan.units[0].findings[0]["finding_id"] == fid_a


class TestCapEnforcement:
    def test_cap_zero_produces_empty_plan(self):
        """remaining_cap=0 → no units (all cap_reached)."""
        fid = _fid()
        f = _finding(finding_id=fid)
        plan = build_plan(
            classified=[_cf(f)],
            paired_bookings={},
            first_seen={fid: _old_enough(22)},
            run_at=_NOW,
            remaining_cap=0,
        )
        assert len(plan.units) == 0
        assert plan.skip_counts.get(SKIP_CAP_REACHED, 0) >= 1

    def test_cap_one_produces_one_unit(self):
        """remaining_cap=1 with one eligible standalone → 1 unit."""
        fid = _fid()
        f = _finding(finding_id=fid)
        plan = build_plan(
            classified=[_cf(f)],
            paired_bookings={},
            first_seen={fid: _old_enough(22)},
            run_at=_NOW,
            remaining_cap=1,
        )
        assert len(plan.units) == 1
        assert plan.total_decisions() == 1

    def test_cap_exact_boundary(self):
        """remaining_cap=N admits exactly N decisions (mutant guard S4: off by one)."""
        fids = [_fid() for _ in range(5)]
        fs = [_finding(finding_id=fid) for fid in fids]
        plan = build_plan(
            classified=[_cf(f) for f in fs],
            paired_bookings={},
            first_seen={fid: _old_enough(22) for fid in fids},
            run_at=_NOW,
            remaining_cap=3,
        )
        # Exactly 3 decisions admitted, 2 skipped as cap_reached.
        assert plan.total_decisions() == 3
        assert plan.skip_counts.get(SKIP_CAP_REACHED, 0) == 2

    def test_cap_never_exceeded(self):
        """total_decisions never exceeds remaining_cap regardless of input size."""
        fids = [_fid() for _ in range(20)]
        fs = [_finding(finding_id=fid) for fid in fids]
        for cap in range(0, 21):
            plan = build_plan(
                classified=[_cf(f) for f in fs],
                paired_bookings={},
                first_seen={fid: _old_enough(22) for fid in fids},
                run_at=_NOW,
                remaining_cap=cap,
            )
            assert plan.total_decisions() <= cap


class TestPairCapEnforcement:
    """Cap enforcement must NEVER split a pair (mutant guard S5)."""

    def _grounding(self, fid: str = None) -> dict:
        return {
            "finding_id": fid or _fid(),
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
            "evidence": [],
        }

    def _booking(self) -> dict:
        return {
            "finding_id": _fid(),
            "agent_id": "agent-fi-fleet-health",
            "scope": {"vin": _VIN},
            "fleet_id": _FLEET,
            "severity": "p0",
            "computed_at": "2026-11-05T05:10:00Z",
            "expires_at": "2026-11-09T07:10:00Z",
            "recommended_action": {
                "action_kind": "book_fleet_service",
                "target_system": "dms",
                "parameters": {
                    "vehicle_vin": _VIN,
                    "needs": [{"need_id": "fault:P0001"}],
                    "dealer_id": "DLR-001",
                    "urgency": "this_week",
                },
            },
            "evidence": [],
        }

    def test_pair_needs_2_slots_cap_1_not_started(self):
        """A pair counts as 2 decisions; with cap=1, the pair is skipped entirely
        (never split into ground-only).

        Mutant guard S5: splitting the pair would approve the grounding without
        the booking, leaving a grounded car with no service appointment.
        """
        gid = _fid()
        g = self._grounding(fid=gid)
        b = self._booking()
        plan = build_plan(
            classified=[_cf(g, CLASS_FH_GROUND_BOOK)],
            paired_bookings={gid: (b, None)},
            first_seen={gid: _old_enough(22), b["finding_id"]: _old_enough(22)},
            run_at=_NOW,
            remaining_cap=1,
        )
        assert len(plan.units) == 0
        assert plan.skip_counts.get(SKIP_CAP_REACHED, 0) >= 1

    def test_pair_needs_2_slots_cap_2_is_one_unit(self):
        """With cap=2 a pair fits as one unit of 2 decisions."""
        gid = _fid()
        g = self._grounding(fid=gid)
        b = self._booking()
        plan = build_plan(
            classified=[_cf(g, CLASS_FH_GROUND_BOOK)],
            paired_bookings={gid: (b, None)},
            first_seen={gid: _old_enough(22), b["finding_id"]: _old_enough(22)},
            run_at=_NOW,
            remaining_cap=2,
        )
        assert len(plan.units) == 1
        assert plan.units[0].decision_count == 2
        assert len(plan.units[0].findings) == 2

    def test_pair_unit_contains_both_findings(self):
        """A pair unit's findings tuple is (grounding, booking)."""
        gid = _fid()
        g = self._grounding(fid=gid)
        b = self._booking()
        plan = build_plan(
            classified=[_cf(g, CLASS_FH_GROUND_BOOK)],
            paired_bookings={gid: (b, None)},
            first_seen={gid: _old_enough(22), b["finding_id"]: _old_enough(22)},
            run_at=_NOW,
            remaining_cap=5,
        )
        assert len(plan.units) == 1
        grnd, book = plan.units[0].findings
        assert grnd["finding_id"] == gid
        assert book["finding_id"] == b["finding_id"]

    def test_pair_cap_after_standalone_units(self):
        """A standalone unit uses 1 slot; subsequent pair is skipped when only 1 remains."""
        standalone_fid = _fid()
        s = _finding(finding_id=standalone_fid)
        gid = _fid()
        g = self._grounding(fid=gid)
        b = self._booking()
        # cap=2: standalone takes 1, pair needs 2 → pair skipped
        plan = build_plan(
            classified=[_cf(s, CLASS_FH_BOOK), _cf(g, CLASS_FH_GROUND_BOOK)],
            paired_bookings={gid: (b, None)},
            first_seen={
                standalone_fid: _old_enough(25),  # older → comes first
                gid: _old_enough(22),
                b["finding_id"]: _old_enough(22),
            },
            run_at=_NOW,
            remaining_cap=2,
        )
        assert len(plan.units) == 1
        assert plan.units[0].findings[0]["finding_id"] == standalone_fid
        assert plan.skip_counts.get(SKIP_CAP_REACHED, 0) >= 1


class TestSkipCountTally:
    def test_pre_classified_skips_tallied(self):
        """Skips from classify_finding (pre-plan) are counted in plan.skip_counts."""
        f1 = _finding()
        f2 = _finding()
        plan = build_plan(
            classified=[
                _cf_skip(f1, "expired"),
                _cf_skip(f2, "decided"),
            ],
            paired_bookings={},
            first_seen={},
            run_at=_NOW,
            remaining_cap=5,
        )
        assert plan.skip_counts.get("expired", 0) == 1
        assert plan.skip_counts.get("decided", 0) == 1
        assert len(plan.units) == 0
