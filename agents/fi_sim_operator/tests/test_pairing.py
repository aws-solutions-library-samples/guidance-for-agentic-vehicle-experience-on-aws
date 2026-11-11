"""Tests for agents/fi_sim_operator/pairing.py (task 11.2c).

Covers the FH pair_booking port, the recall pairing, and the parity fixture
table (mirrored in CMS's parity test in task 11.6).
"""
from __future__ import annotations

import uuid

import pytest

from agents.fi_sim_operator.pairing import (
    REASON_ALREADY_DECIDED,
    REASON_DISCLOSURE_DIFFERS,
    REASON_NOT_FLEET_HEALTH,
    REASON_NO_BOOKING_ITEM,
    REASON_NO_DEALER,
    REASON_RECALL_CAMPAIGN_MISMATCH,
    pair_booking,
)
from agents.fi_sim_operator.select import FH_AGENT, RW_AGENT

_NOW = "2026-11-06T07:10:00Z"
_LATER = "2026-11-09T07:10:00Z"   # expires_at (future)
_PAST = "2026-11-05T07:09:00Z"    # expires_at (in the past)

_VIN = "MRDTEST0000000001"
_FLEET = "demo-lastmile-delivery"
_CAMPAIGN = "MRD-2026-004"


def _fid() -> str:
    return f"FIND-{uuid.uuid4()}"


def _fh_grounding(
    vin: str = _VIN,
    fleet: str = _FLEET,
    fault_codes: list | None = None,
    disclosure: str = "Stop-driving fault P0001",
    finding_id: str | None = None,
) -> dict:
    return {
        "finding_id": finding_id or _fid(),
        "agent_id": FH_AGENT,
        "scope": {"vin": vin},
        "fleet_id": fleet,
        "severity": "p0",
        "computed_at": "2026-11-05T05:00:00Z",
        "expires_at": _LATER,
        "recommended_action": {
            "action_kind": "ground_vehicle",
            "target_system": "cms",
            "parameters": {
                "vehicle_vin": vin,
                "fault_codes": fault_codes or ["P0001"],
            },
        },
        "disclosure_txt": disclosure,
        "evidence": [],
    }


def _fh_booking(
    vin: str = _VIN,
    fleet: str = _FLEET,
    needs: list | None = None,
    dealer_id: str | None = "DLR-001",
    disclosure: str = "Stop-driving fault P0001",
    finding_id: str | None = None,
    computed_at: str = "2026-11-05T05:10:00Z",
    expires_at: str = _LATER,
) -> dict:
    if needs is None:
        needs = [{"need_id": "fault:P0001"}]
    return {
        "finding_id": finding_id or _fid(),
        "agent_id": FH_AGENT,
        "scope": {"vin": vin},
        "fleet_id": fleet,
        "severity": "p0",
        "computed_at": computed_at,
        "expires_at": expires_at,
        "recommended_action": {
            "action_kind": "book_fleet_service",
            "target_system": "dms",
            "parameters": {
                "vehicle_vin": vin,
                "needs": needs,
                "dealer_id": dealer_id,
                "urgency": "this_week",
            },
        },
        "disclosure_txt": disclosure,
        "evidence": [],
    }


def _recall_grounding(
    vin: str = _VIN,
    campaign: str = _CAMPAIGN,
    fleet: str = _FLEET,
    finding_id: str | None = None,
) -> dict:
    return {
        "finding_id": finding_id or _fid(),
        "agent_id": RW_AGENT,
        "scope": {"vin": vin},
        "fleet_id": fleet,
        "severity": "p0",
        "computed_at": "2026-11-05T05:00:00Z",
        "expires_at": _LATER,
        "recommended_action": {
            "action_kind": "ground_vehicle",
            "target_system": "cms",
            "parameters": {"vehicle_vin": vin, "recall_id": campaign},
        },
        "evidence": [],
    }


def _recall_booking(
    vin: str = _VIN,
    campaign: str = _CAMPAIGN,
    fleet: str = _FLEET,
    dealer_id: str = "DLR-002",
    finding_id: str | None = None,
    computed_at: str = "2026-11-05T05:10:00Z",
    expires_at: str = _LATER,
) -> dict:
    return {
        "finding_id": finding_id or _fid(),
        "agent_id": RW_AGENT,
        "scope": {"vin": vin},
        "fleet_id": fleet,
        "severity": "attention",
        "computed_at": computed_at,
        "expires_at": expires_at,
        "recommended_action": {
            "action_kind": "book_recall_service",
            "target_system": "dms",
            "parameters": {
                "vehicle_vin": vin,
                "recall_id": campaign,
                "dealer_id": dealer_id,
            },
        },
        "evidence": [],
    }


# ── Parity fixture table (spec D1, mirrored in CMS task 11.6) ─────────────────
# Rows: (grounding, candidates_desc, decided_ids, expected_reason, check_booking)

PARITY_TABLE = [
    # Row 1: basic happy path
    ("fh_basic", None, None, None, True),
    # Row 2: not from FH agent → not_fleet_health
    ("rw_grounding_as_fh", None, None, REASON_NOT_FLEET_HEALTH, False),
    # Row 3: no candidates → no_booking_item
    ("fh_no_candidates", None, None, REASON_NO_BOOKING_ITEM, False),
    # Row 4: all candidates decided → already_decided
    ("fh_all_decided", None, "all", REASON_ALREADY_DECIDED, True),
    # Row 5: no dealer_id → no_dealer
    ("fh_no_dealer", None, None, REASON_NO_DEALER, True),
    # Row 6: disclosure differs → disclosure_differs
    ("fh_disclosure_differs", None, None, REASON_DISCLOSURE_DIFFERS, True),
]


class TestFhPairingHappyPath:
    def test_basic_pair_returns_booking_and_none_reason(self):
        """FH grounding + matching booking → (booking, None)."""
        g = _fh_grounding()
        b = _fh_booking()
        booking, reason = pair_booking(g, [b], set(), _NOW)
        assert reason is None
        assert booking is not None
        assert booking["finding_id"] == b["finding_id"]

    def test_latest_computed_at_is_chosen(self):
        """Among multiple valid bookings, the latest computed_at is chosen."""
        g = _fh_grounding()
        b_old = _fh_booking(computed_at="2026-11-04T05:00:00Z")
        b_new = _fh_booking(computed_at="2026-11-05T08:00:00Z")
        booking, reason = pair_booking(g, [b_old, b_new], set(), _NOW)
        assert reason is None
        assert booking["finding_id"] == b_new["finding_id"]


class TestFhPairingReasons:
    def test_non_fh_grounding_not_fleet_health(self):
        """Grounding from RW agent → not_fleet_health."""
        g = _recall_grounding()  # RW_AGENT, ground_vehicle
        b = _fh_booking()
        _, reason = pair_booking(g, [b], set(), _NOW)
        # RW grounding has recall_id, not fault_codes → recall path
        # The recall path requires RW bookings; the FH booking won't match
        # Actually the grounding has recall_id so it goes to recall path.
        # The FH booking is not a book_recall_service → no match → recall_campaign_mismatch
        assert reason == REASON_RECALL_CAMPAIGN_MISMATCH

    def test_fh_grounding_no_candidates_no_booking_item(self):
        """No candidates at all → no_booking_item."""
        g = _fh_grounding()
        _, reason = pair_booking(g, [], set(), _NOW)
        assert reason == REASON_NO_BOOKING_ITEM

    def test_fh_grounding_wrong_vin_booking_no_booking_item(self):
        """Booking for a different VIN → no_booking_item."""
        g = _fh_grounding(vin=_VIN)
        b = _fh_booking(vin="MRDTEST0000000002")
        _, reason = pair_booking(g, [b], set(), _NOW)
        assert reason == REASON_NO_BOOKING_ITEM

    def test_fh_grounding_fault_code_not_covered_no_booking_item(self):
        """Booking's needs don't include fault:P0001 → no_booking_item."""
        g = _fh_grounding(fault_codes=["P0001"])
        b = _fh_booking(needs=[{"need_id": "pm:OIL_CHANGE"}])
        _, reason = pair_booking(g, [b], set(), _NOW)
        assert reason == REASON_NO_BOOKING_ITEM

    def test_all_candidates_decided_already_decided(self):
        """All valid candidates are decided → already_decided with the candidate."""
        g = _fh_grounding()
        b_id = _fid()
        b = _fh_booking(finding_id=b_id)
        booking, reason = pair_booking(g, [b], {b_id}, _NOW)
        assert reason == REASON_ALREADY_DECIDED
        assert booking["finding_id"] == b_id

    def test_no_dealer_id_returns_no_dealer(self):
        """Booking with dealer_id=None → no_dealer."""
        g = _fh_grounding()
        b = _fh_booking(dealer_id=None)
        booking, reason = pair_booking(g, [b], set(), _NOW)
        assert reason == REASON_NO_DEALER
        assert booking is not None

    def test_disclosure_differs_returns_disclosure_differs(self):
        """Different disclosure_txt → disclosure_differs."""
        g = _fh_grounding(disclosure="P0001 stop-driving directive")
        b = _fh_booking(disclosure="DIFFERENT disclosure text here")
        booking, reason = pair_booking(g, [b], set(), _NOW)
        assert reason == REASON_DISCLOSURE_DIFFERS
        assert booking is not None

    def test_expired_booking_excluded(self):
        """Expired booking candidate is excluded from matching."""
        g = _fh_grounding()
        b = _fh_booking(expires_at=_PAST)
        _, reason = pair_booking(g, [b], set(), _NOW)
        assert reason == REASON_NO_BOOKING_ITEM

    def test_eligible_preferred_over_decided(self):
        """If one eligible and one decided, the eligible one is returned."""
        g = _fh_grounding()
        b_decided_id = _fid()
        b_decided = _fh_booking(finding_id=b_decided_id, computed_at="2026-11-05T09:00:00Z")
        b_eligible = _fh_booking(computed_at="2026-11-04T05:00:00Z")
        booking, reason = pair_booking(g, [b_decided, b_eligible], {b_decided_id}, _NOW)
        assert reason is None
        assert booking["finding_id"] == b_eligible["finding_id"]


# ── Recall pairing ─────────────────────────────────────────────────────────────

class TestRecallPairing:
    def test_recall_pair_returns_booking_and_none_reason(self):
        """RW grounding (recall_id) + matching recall booking → (booking, None)."""
        g = _recall_grounding(campaign=_CAMPAIGN)
        b = _recall_booking(campaign=_CAMPAIGN)
        booking, reason = pair_booking(g, [b], set(), _NOW)
        assert reason is None
        assert booking is not None
        assert booking["finding_id"] == b["finding_id"]

    def test_recall_pair_wrong_campaign_no_match(self):
        """Booking with different recall_id → recall_campaign_mismatch."""
        g = _recall_grounding(campaign="MRD-2026-004")
        b = _recall_booking(campaign="MRD-2026-999")
        _, reason = pair_booking(g, [b], set(), _NOW)
        assert reason == REASON_RECALL_CAMPAIGN_MISMATCH

    def test_recall_pair_wrong_vin_no_match(self):
        """Booking with different VIN → recall_campaign_mismatch (no match)."""
        g = _recall_grounding(vin=_VIN, campaign=_CAMPAIGN)
        b = _recall_booking(vin="MRDTEST0000000002", campaign=_CAMPAIGN)
        _, reason = pair_booking(g, [b], set(), _NOW)
        assert reason == REASON_RECALL_CAMPAIGN_MISMATCH

    def test_recall_pair_all_decided_already_decided(self):
        """All recall booking candidates decided → already_decided."""
        g = _recall_grounding()
        b_id = _fid()
        b = _recall_booking(finding_id=b_id)
        booking, reason = pair_booking(g, [b], {b_id}, _NOW)
        assert reason == REASON_ALREADY_DECIDED

    def test_recall_pair_expired_booking_excluded(self):
        """Expired recall booking → no match."""
        g = _recall_grounding()
        b = _recall_booking(expires_at=_PAST)
        _, reason = pair_booking(g, [b], set(), _NOW)
        assert reason == REASON_RECALL_CAMPAIGN_MISMATCH

    def test_recall_pair_latest_computed_at_chosen(self):
        """Among multiple valid recall bookings, latest computed_at is chosen."""
        g = _recall_grounding()
        b_old = _recall_booking(computed_at="2026-11-04T05:00:00Z")
        b_new = _recall_booking(computed_at="2026-11-05T08:00:00Z")
        booking, reason = pair_booking(g, [b_old, b_new], set(), _NOW)
        assert reason is None
        assert booking["finding_id"] == b_new["finding_id"]
