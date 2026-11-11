"""Tests for Fix Group 11-1: W1, W2, S1, S2, S8.

Mutation guards:
  P-a: planner skips booking staleness check  → test_stale_booking_no_pair
  P-b: planner skips booking first-seen check → test_booking_first_seen_too_new
  P-c: recall_book without campaign match     → test_recall_book_campaign_mismatch_no_classify
"""
from __future__ import annotations

import datetime
import uuid

import pytest

from agents.fi_sim_operator.config import ConfigError, parse_config
from agents.fi_sim_operator.plan import build_plan, SKIP_PAIRED
from agents.fi_sim_operator.select import (
    CLASS_FH_GROUND_BOOK,
    CLASS_RECALL_BOOK,
    SKIP_DECIDED,
    SKIP_EXPIRED,
    SKIP_NOT_SAFETY_RECALL,
    SKIP_STALE,
    SKIP_TOO_NEW,
    SKIP_UNCLASSIFIED,
    ClassifiedFinding,
    FH_AGENT,
    RW_AGENT,
    classify_finding,
)

_NOW = "2026-11-06T07:10:00Z"
_RECENT = "2026-11-06T05:00:00Z"       # 2 h 10 m ago — within 26 h
_30H_OLD = "2026-11-04T01:10:00Z"      # 30 h before _NOW — stale
_FUTURE = "2026-11-09T07:10:00Z"
_VIN = "MRDTEST0000000001"
_FLEET = "demo-lastmile-delivery"
_CAMPAIGN = "MRD-2026-004"


def _fid() -> str:
    return f"FIND-{uuid.uuid4()}"


def _grounding(fid: str, vin: str = _VIN, fleet: str = _FLEET) -> dict:
    return {
        "finding_id": fid,
        "agent_id": FH_AGENT,
        "scope": {"vin": vin},
        "fleet_id": fleet,
        "severity": "p0",
        "computed_at": _RECENT,
        "expires_at": _FUTURE,
        "recommended_action": {
            "action_kind": "ground_vehicle",
            "target_system": "cms",
            "parameters": {"vehicle_vin": vin, "fault_codes": ["P0001"]},
        },
        "evidence": [],
    }


def _booking(
    *,
    vin: str = _VIN,
    fleet: str = _FLEET,
    computed_at: str = _RECENT,
    expires_at: str = _FUTURE,
    target_system: str = "dms",
) -> dict:
    return {
        "finding_id": _fid(),
        "agent_id": FH_AGENT,
        "scope": {"vin": vin},
        "fleet_id": fleet,
        "severity": "p0",
        "computed_at": computed_at,
        "expires_at": expires_at,
        "recommended_action": {
            "action_kind": "book_fleet_service",
            "target_system": target_system,
            "parameters": {
                "vehicle_vin": vin,
                "needs": [{"need_id": "fault:P0001", "labor_op": "LBR-001"}],
                "dealer_id": "DLR-001",
                "urgency": "this_week",
            },
        },
        "evidence": [],
    }


def _cf(finding: dict, class_name: str = CLASS_FH_GROUND_BOOK) -> ClassifiedFinding:
    return ClassifiedFinding(finding=finding, class_name=class_name, skip_reason=None)


def _old_enough(hours_ago: float = 22.0) -> str:
    dt = datetime.datetime.fromisoformat(_NOW.replace("Z", "+00:00"))
    return (dt - datetime.timedelta(hours=hours_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _too_new_ts(hours_ago: float = 10.0) -> str:
    dt = datetime.datetime.fromisoformat(_NOW.replace("Z", "+00:00"))
    return (dt - datetime.timedelta(hours=hours_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


# ── W1: booking candidate filter in the planner ──────────────────────────────

class TestW1BookingCandidateFilter:
    """Planner validates the paired booking against the candidate filter (W1).

    P-a: removing the staleness check → stale booking passes → unit is planned.
    test_stale_booking_no_pair asserts len(plan.units)==0; mutant P-a makes it 1.

    P-b: removing the first-seen check → 10h-old booking passes → unit is planned.
    test_booking_first_seen_too_new asserts len(plan.units)==0; mutant P-b makes it 1.
    """

    def _plan_with_booking(self, b: dict, *, b_first_seen: str | None = None) -> object:
        gid = _fid()
        g = _grounding(gid)
        bid = b["finding_id"]
        first_seen = {gid: _old_enough(22.0)}
        if b_first_seen is not None:
            first_seen[bid] = b_first_seen
        return build_plan(
            classified=[_cf(g)],
            paired_bookings={gid: (b, None)},
            first_seen=first_seen,
            run_at=_NOW,
            remaining_cap=5,
        )

    def test_stale_booking_no_pair(self):
        """P1 probe: a booking with computed_at 30 h old → no unit (grounding skipped stale).

        Mutation guard P-a: removing the staleness check for the booking makes
        this test fail (unit would be planned).
        """
        b = _booking(computed_at=_30H_OLD)
        plan = self._plan_with_booking(b, b_first_seen=_old_enough(22.0))
        assert len(plan.units) == 0, (
            "A booking with computed_at 30 h old must be rejected (stale). "
            "Mutant P-a (no staleness check) would produce 1 unit."
        )
        assert plan.skip_counts.get(SKIP_STALE, 0) == 1

    def test_booking_target_system_cms_no_pair(self):
        """P1b probe: booking with target_system='cms' → no unit (grounding skipped)."""
        b = _booking(target_system="cms")
        plan = self._plan_with_booking(b, b_first_seen=_old_enough(22.0))
        assert len(plan.units) == 0, (
            "A booking whose target_system is 'cms' must not pair."
        )
        assert plan.skip_counts.get(SKIP_UNCLASSIFIED, 0) == 1

    def test_booking_first_seen_too_new(self):
        """Booking first seen 10 h ago → no unit (too_new).

        Mutation guard P-b: removing the booking first-seen check makes this
        test fail (unit would be planned).
        """
        b = _booking()
        plan = self._plan_with_booking(b, b_first_seen=_too_new_ts(10.0))
        assert len(plan.units) == 0, (
            "A booking first seen 10 h ago (< 20 h) must be rejected (too_new). "
            "Mutant P-b (no first-seen check) would produce 1 unit."
        )
        assert plan.skip_counts.get(SKIP_TOO_NEW, 0) >= 1

    def test_booking_absent_from_first_seen_too_new(self):
        """Booking not in first_seen at all → too_new."""
        b = _booking()
        # Do NOT add booking id to first_seen
        gid = _fid()
        g = _grounding(gid)
        plan = build_plan(
            classified=[_cf(g)],
            paired_bookings={gid: (b, None)},
            first_seen={gid: _old_enough(22.0)},  # booking absent
            run_at=_NOW,
            remaining_cap=5,
        )
        assert len(plan.units) == 0
        assert plan.skip_counts.get(SKIP_TOO_NEW, 0) >= 1

    def test_booking_expired_no_pair(self):
        """Expired booking → no unit."""
        b = _booking(expires_at="2026-11-05T07:09:00Z")  # before _NOW
        plan = self._plan_with_booking(b, b_first_seen=_old_enough(22.0))
        assert len(plan.units) == 0
        assert plan.skip_counts.get(SKIP_EXPIRED, 0) == 1

    def test_booking_decided_no_pair(self):
        """Booking in decided_ids → no unit."""
        b = _booking()
        gid = _fid()
        g = _grounding(gid)
        plan = build_plan(
            classified=[_cf(g)],
            paired_bookings={gid: (b, None)},
            first_seen={gid: _old_enough(22.0), b["finding_id"]: _old_enough(22.0)},
            run_at=_NOW,
            remaining_cap=5,
            decided_ids={b["finding_id"]},
        )
        assert len(plan.units) == 0
        assert plan.skip_counts.get(SKIP_DECIDED, 0) == 1

    def test_good_pair_still_planned(self):
        """A valid booking with good first_seen → unit planned."""
        b = _booking()
        plan = self._plan_with_booking(b, b_first_seen=_old_enough(22.0))
        assert len(plan.units) == 1
        assert plan.units[0].decision_count == 2


# ── W2: recall_book requires campaign_id == recall_id ────────────────────────

def _recall_finding(
    *,
    recall_id: str | None = _CAMPAIGN,
    campaign_evidence_id: str = _CAMPAIGN,
    computed_at: str = _RECENT,
    expires_at: str = _FUTURE,
    vin: str = _VIN,
    fleet: str = _FLEET,
) -> dict:
    ev = {
        "kind": "recall.campaign",
        "value": {
            "campaign_id": campaign_evidence_id,
            "recall_class": "safety",
            "do_not_drive": True,
        },
        "vin": vin,
        "provenance": "live",
    }
    params: dict = {"vehicle_vin": vin}
    if recall_id is not None:
        params["recall_id"] = recall_id
    return {
        "finding_id": _fid(),
        "agent_id": RW_AGENT,
        "scope": {"vin": vin},
        "fleet_id": fleet,
        "severity": "p0",
        "computed_at": computed_at,
        "expires_at": expires_at,
        "recommended_action": {
            "action_kind": "book_recall_service",
            "target_system": "dms",
            "parameters": params,
        },
        "evidence": [ev],
    }


def _classify_recall(f: dict, pending=None) -> ClassifiedFinding:
    return classify_finding(
        finding=f,
        decided_ids=set(),
        now_iso=_NOW,
        hold_vins=frozenset(),
        grounded_codes={},
        recall_pending_groundings=set(pending or []),
    )


class TestW2RecallBookCampaignMatch:
    """W2: recall_book requires evidence campaign_id == parameters recall_id.

    Mutation guard P-c: removing the campaign_id match check lets a booking
    with a mismatched campaign classify as recall_book.
    test_recall_book_campaign_mismatch_no_classify asserts skip_reason ==
    SKIP_NOT_SAFETY_RECALL; mutant P-c would set class_name = CLASS_RECALL_BOOK.
    """

    def test_matching_campaign_classifies_recall_book(self):
        """Same campaign_id in evidence and parameters → recall_book."""
        f = _recall_finding(recall_id=_CAMPAIGN, campaign_evidence_id=_CAMPAIGN)
        cf = _classify_recall(f)
        assert cf.class_name == CLASS_RECALL_BOOK

    def test_recall_book_campaign_mismatch_no_classify(self):
        """P3 probe: evidence campaign_id != parameters recall_id → SKIP_NOT_SAFETY_RECALL.

        Mutation guard P-c: removing the campaign_id match check makes
        test_matching_campaign_classifies_recall_book succeed (expected), but
        this test would fail — the mismatched booking would classify as
        recall_book instead of being skipped.
        """
        f = _recall_finding(recall_id="MRD-NONSAFETY-9", campaign_evidence_id=_CAMPAIGN)
        cf = _classify_recall(f)
        assert cf.class_name is None, (
            "A recall booking whose evidence campaign_id differs from parameters "
            "recall_id must not be classified. Mutant P-c (no campaign check) "
            "would return CLASS_RECALL_BOOK."
        )
        assert cf.skip_reason == SKIP_NOT_SAFETY_RECALL

    def test_no_recall_id_in_parameters_is_not_safety_recall(self):
        """Missing recall_id in parameters → SKIP_NOT_SAFETY_RECALL."""
        f = _recall_finding(recall_id=None, campaign_evidence_id=_CAMPAIGN)
        cf = _classify_recall(f)
        assert cf.class_name is None
        assert cf.skip_reason == SKIP_NOT_SAFETY_RECALL


# ── S1: _is_stale catches TypeError for naive datetimes ──────────────────────

class TestS1IsStaleTypeError:
    """S1: a naive computed_at raises TypeError on subtraction → treat as stale."""

    def _fh_ground(self, computed_at: str) -> dict:
        return {
            "finding_id": _fid(),
            "agent_id": FH_AGENT,
            "scope": {"vin": _VIN},
            "fleet_id": _FLEET,
            "severity": "p0",
            "computed_at": computed_at,
            "expires_at": _FUTURE,
            "recommended_action": {
                "action_kind": "ground_vehicle",
                "target_system": "cms",
                "parameters": {"vehicle_vin": _VIN, "fault_codes": ["P0001"]},
            },
            "evidence": [],
        }

    def _classify(self, f: dict) -> ClassifiedFinding:
        return classify_finding(
            finding=f,
            decided_ids=set(),
            now_iso=_NOW,
            hold_vins=frozenset(),
            grounded_codes={},
            recall_pending_groundings=set(),
        )

    def test_naive_computed_at_is_stale(self):
        """A naive computed_at (no tz) raises TypeError → treated as stale."""
        f = self._fh_ground(computed_at="2026-11-06T05:40:00")  # no 'Z'
        cf = self._classify(f)
        # Must be skipped; previously TypeError propagated and aborted the run.
        assert cf.class_name is None
        assert cf.skip_reason == SKIP_STALE

    def test_recent_aware_computed_at_not_stale(self):
        """Normal aware computed_at within 26 h → not stale."""
        f = self._fh_ground(computed_at=_RECENT)
        cf = self._classify(f)
        assert cf.skip_reason != SKIP_STALE


# ── S2: daily_cap below 0 is a ConfigError ───────────────────────────────────

_BASE_CFG = {
    "fleets": {"demo-lastmile-delivery": {"enabled": True}},
    "daily_cap": 5,
}


class TestS2DailyCapNegative:
    """S2: daily_cap below 0 must be rejected as ConfigError (D10a)."""

    def test_negative_cap_is_config_error(self):
        result = parse_config({**_BASE_CFG, "daily_cap": -3})
        assert isinstance(result, ConfigError), (
            "daily_cap=-3 must be rejected as ConfigError."
        )
        assert "0" in result.reason or "greater" in result.reason

    def test_minus_one_is_config_error(self):
        result = parse_config({**_BASE_CFG, "daily_cap": -1})
        assert isinstance(result, ConfigError)

    def test_zero_is_valid(self):
        """daily_cap=0 is valid (disables decisions for the day)."""
        from agents.fi_sim_operator.config import OperatorConfig
        result = parse_config({**_BASE_CFG, "daily_cap": 0})
        assert isinstance(result, OperatorConfig)
        assert result.daily_cap == 0

    def test_positive_cap_still_valid(self):
        from agents.fi_sim_operator.config import OperatorConfig
        result = parse_config({**_BASE_CFG, "daily_cap": 10})
        assert isinstance(result, OperatorConfig)


# ── S8: recall booking held back by pending grounding → "paired" ─────────────

class TestS8RecallBookPairedReason:
    """S8: a recall booking held back by a pending grounding counts as 'paired'."""

    def test_pending_grounding_gives_paired_not_unclassified(self):
        f = _recall_finding(recall_id=_CAMPAIGN, campaign_evidence_id=_CAMPAIGN)
        vin = f["scope"]["vin"]
        cf = _classify_recall(f, pending=[(vin, _CAMPAIGN)])
        assert cf.class_name is None
        assert cf.skip_reason == SKIP_PAIRED, (
            f"Expected SKIP_PAIRED ('paired'), got {cf.skip_reason!r}. "
            "Before S8, this counted as SKIP_UNCLASSIFIED."
        )

    def test_no_pending_grounding_classifies_normally(self):
        f = _recall_finding(recall_id=_CAMPAIGN, campaign_evidence_id=_CAMPAIGN)
        cf = _classify_recall(f, pending=[])
        assert cf.class_name == CLASS_RECALL_BOOK
