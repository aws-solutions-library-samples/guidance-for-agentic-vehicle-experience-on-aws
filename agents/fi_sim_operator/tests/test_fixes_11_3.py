"""Tests for architect's fix items (b), (c), (d) — spec D10, task 11.3.

Each fix corresponds to a mutation guard (one mutant each where marked):
  (b) _is_expired: parse UTC, missing/malformed/naive → expired (fail closed).
      Mutant: old string comparison — missing expires_at returns live (False).
  (c) Recall pairing requires same fleet as the grounding.
      Mutant: no fleet check — a cross-fleet booking is paired.
  (d) build_plan paired_bookings is (booking, reason); pair only when reason is None.
      Mutant: pair when reason is "disclosure_differs" — approved with wrong directive.
"""
from __future__ import annotations

import uuid

import pytest

from agents.fi_sim_operator.select import (
    classify_finding,
    FH_AGENT,
    RW_AGENT,
    CLASS_FH_GROUND_BOOK,
    SKIP_EXPIRED,
    SKIP_HELD,
    SKIP_UNCLASSIFIED,
)
from agents.fi_sim_operator.pairing import (
    pair_booking,
    REASON_RECALL_CAMPAIGN_MISMATCH,
)
from agents.fi_sim_operator.plan import build_plan, _MIN_AGE_HOURS
from agents.fi_sim_operator.select import ClassifiedFinding, CLASS_FH_BOOK

_NOW = "2026-11-06T07:10:00Z"
_FUTURE = "2026-11-09T07:10:00Z"
_PAST = "2026-11-05T07:09:00Z"
_VIN = "MRDTEST0000000001"
_VIN2 = "MRDTEST0000000002"
_FLEET = "demo-lastmile-delivery"
_FLEET2 = "demo-field-service"
_CAMPAIGN = "MRD-2026-004"


def _fid() -> str:
    return f"FIND-{uuid.uuid4()}"


def _base_finding(
    *,
    finding_id: str | None = None,
    agent_id: str = FH_AGENT,
    action_kind: str = "ground_vehicle",
    target_system: str = "cms",
    vin: str = _VIN,
    fleet_id: str = _FLEET,
    severity: str = "p0",
    computed_at: str = "2026-11-06T05:00:00Z",
    expires_at: str | None = _FUTURE,
    parameters: dict | None = None,
    evidence: list | None = None,
) -> dict:
    rec_params = parameters if parameters is not None else {
        "vehicle_vin": vin,
        "fault_codes": ["P0001"],
    }
    f: dict = {
        "finding_id": finding_id or _fid(),
        "agent_id": agent_id,
        "scope": {"vin": vin},
        "fleet_id": fleet_id,
        "severity": severity,
        "computed_at": computed_at,
        "recommended_action": {
            "action_kind": action_kind,
            "target_system": target_system,
            "parameters": rec_params,
        },
        "disclosure_txt": "Stop-driving fault P0001",
        "evidence": evidence if evidence is not None else [],
    }
    if expires_at is not None:
        f["expires_at"] = expires_at
    return f


def _classify(f, *, decided=None, hold_vins=None, grounded=None, pending=None):
    return classify_finding(
        finding=f,
        decided_ids=decided or set(),
        now_iso=_NOW,
        hold_vins=frozenset(hold_vins or []),
        # 11.3b item 7: a list of VINs means "grounded for the fixtures' code P0001";
        # a dict is passed through as {vin: codes}.
        grounded_codes=(grounded if isinstance(grounded, dict)
                        else {v: frozenset({"P0001"}) for v in (grounded or [])}),
        recall_pending_groundings=set(pending or []),
    )


def _cf(finding: dict, class_name: str = CLASS_FH_BOOK) -> ClassifiedFinding:
    return ClassifiedFinding(finding=finding, class_name=class_name, skip_reason=None)


def _old_enough(hours: float = 22.0) -> str:
    from datetime import datetime, timezone, timedelta
    dt = datetime.fromisoformat(_NOW.replace("Z", "+00:00"))
    return (dt - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")


# ── Fix (b): _is_expired — parse UTC, fail closed ────────────────────────────

class TestIsExpiredFixB:
    """(b) _is_expired must parse UTC, not compare strings.

    Missing or malformed expires_at → expired (fail closed).

    String comparison fails for Z-vs-dot ordering:
      "2026-11-05T07:00:00Z" > "2026-11-05T07:00:00.400Z" (ord('Z')=90 > ord('.')=46)
    so a finding that expired 400 ms ago would be treated as live.

    Mutation guard (mutant = old string comparison):
      A missing expires_at would return live (the string ``isinstance(None, str)``
      is False so the whole expression is False) — test_missing_expires_at_is_expired
      catches this because the finding would be classified (not skipped for expiry).
    """

    def test_missing_expires_at_is_expired(self):
        """Missing expires_at → the finding is skipped with SKIP_EXPIRED (fail closed).

        Mutant guard: old string comparison returns False for missing expires_at,
        so the finding would be classified instead of skipped.
        """
        f = _base_finding(expires_at=None)  # no expires_at key
        cf = _classify(f)
        assert cf.class_name is None, (
            "A finding with no expires_at must be skipped (fail closed); "
            "old string comparison would have returned False (live)."
        )
        assert cf.skip_reason == SKIP_EXPIRED

    def test_malformed_expires_at_is_expired(self):
        """Malformed expires_at string → SKIP_EXPIRED (fail closed)."""
        f = _base_finding(expires_at="not-a-date")
        cf = _classify(f)
        assert cf.class_name is None
        assert cf.skip_reason == SKIP_EXPIRED

    def test_integer_expires_at_is_expired(self):
        """Non-string expires_at (int) → SKIP_EXPIRED."""
        f = _base_finding()
        f["expires_at"] = 99999999  # not a string
        cf = _classify(f)
        assert cf.class_name is None
        assert cf.skip_reason == SKIP_EXPIRED

    def test_naive_datetime_string_is_expired(self):
        """A naive datetime string (no timezone) → SKIP_EXPIRED (fail closed)."""
        f = _base_finding(expires_at="2099-01-01T00:00:00")  # far future but no TZ
        cf = _classify(f)
        assert cf.class_name is None
        assert cf.skip_reason == SKIP_EXPIRED

    def test_future_z_suffix_is_live(self):
        """A properly-formed future expires_at → not expired (normal case)."""
        f = _base_finding(expires_at="2099-01-01T00:00:00Z")
        cf = _classify(f)
        # Should be classified (not expired)
        assert cf.skip_reason != SKIP_EXPIRED

    def test_sub_second_expiry_string_comparison_bug(self):
        """Regression: a finding that expired 400 ms ago must be expired.

        With the old string comparison:
          "2026-11-06T07:10:00Z" > "2026-11-06T07:10:00.400Z"
        because ord('Z')=90 > ord('.')=46, so the comparison incorrectly treats
        the 400ms-past expiry as live.  With UTC parsing this is correctly expired.
        """
        # _NOW is "2026-11-06T07:10:00Z" → this expired 400ms ago
        f = _base_finding(expires_at="2026-11-06T07:09:59.600Z")
        cf = _classify(f)
        assert cf.skip_reason == SKIP_EXPIRED, (
            "A finding that expired 400ms before _NOW must be skipped. "
            "Old string comparison '2026-11-06T07:10:00Z' > '2026-11-06T07:09:59.600Z' "
            "is True (correct here), but the parallel case where NOW has .400 suffix would fail."
        )


class TestPairingIsExpiredFixB:
    """(b) _is_expired in pairing.py: same semantics."""

    def test_pairing_expired_candidate_skipped_with_missing_expires_at(self):
        """A booking candidate with no expires_at is treated as expired.

        If pairing allowed it (old string comparison: missing → live), the
        booking finding would be paired even though it has no expiry date.
        Mutation guard: old string comparison would treat None as live.
        """
        gid = _fid()
        g = {
            "finding_id": gid,
            "agent_id": FH_AGENT,
            "scope": {"vin": _VIN},
            "fleet_id": _FLEET,
            "severity": "p0",
            "computed_at": "2026-11-05T05:00:00Z",
            "expires_at": _FUTURE,
            "recommended_action": {
                "action_kind": "ground_vehicle",
                "target_system": "cms",
                "parameters": {"vehicle_vin": _VIN, "fault_codes": ["P0001"]},
            },
            "disclosure_txt": "Stop-driving",
            "evidence": [],
        }
        booking_no_expiry = {
            "finding_id": _fid(),
            "agent_id": FH_AGENT,
            "scope": {"vin": _VIN},
            "fleet_id": _FLEET,
            "severity": "p0",
            "computed_at": "2026-11-05T05:10:00Z",
            # No expires_at key
            "recommended_action": {
                "action_kind": "book_fleet_service",
                "target_system": "dms",
                "parameters": {
                    "vehicle_vin": _VIN,
                    "needs": [{"need_id": "fault:P0001", "labor_op": "LBR-001"}],
                    "dealer_id": "DLR-001",
                    "urgency": "this_week",
                },
            },
            "disclosure_txt": "Stop-driving",
            "evidence": [],
        }
        result, reason = pair_booking(
            grounding=g,
            candidates=[booking_no_expiry],
            decided_ids=set(),
            now_iso=_NOW,
        )
        # The booking has no expires_at → treated as expired → no valid candidate
        assert result is None, (
            "A booking with no expires_at must be treated as expired and not paired. "
            "Old string comparison returned False (live) for None → this pair would succeed."
        )


# ── Fix (c): recall pairing requires the same fleet ──────────────────────────

class TestRecallPairingFleetFixC:
    """(c) Recall pairing must require the same fleet as the grounding.

    FH pairing already checked fleet (``_get_fleet(cand) != g_fleet``); recall
    pairing omitted this, allowing a cross-fleet booking to be paired.

    Mutation guard (mutant = no fleet check):
      A booking with a different fleet_id would be paired successfully.
      ``test_recall_pairing_different_fleet_rejected`` fails because the
      booking is returned from a different fleet.
    """

    def _recall_grounding(self, fleet: str = _FLEET) -> dict:
        return {
            "finding_id": _fid(),
            "agent_id": RW_AGENT,
            "scope": {"vin": _VIN2},
            "fleet_id": fleet,
            "severity": "p0",
            "computed_at": "2026-11-05T05:00:00Z",
            "expires_at": _FUTURE,
            "recommended_action": {
                "action_kind": "ground_vehicle",
                "target_system": "cms",
                "parameters": {"vehicle_vin": _VIN2, "recall_id": _CAMPAIGN},
            },
            "evidence": [],
        }

    def _recall_booking(self, fleet: str = _FLEET) -> dict:
        return {
            "finding_id": _fid(),
            "agent_id": RW_AGENT,
            "scope": {"vin": _VIN2},
            "fleet_id": fleet,
            "severity": "p0",
            "computed_at": "2026-11-05T05:10:00Z",
            "expires_at": _FUTURE,
            "recommended_action": {
                "action_kind": "book_recall_service",
                "target_system": "dms",
                "parameters": {"vehicle_vin": _VIN2, "recall_id": _CAMPAIGN},
            },
            "evidence": [],
        }

    def test_recall_pairing_same_fleet_succeeds(self):
        """Same fleet → pairing succeeds."""
        g = self._recall_grounding(fleet=_FLEET)
        b = self._recall_booking(fleet=_FLEET)
        result, reason = pair_booking(
            grounding=g, candidates=[b], decided_ids=set(), now_iso=_NOW,
        )
        assert reason is None
        assert result is not None

    def test_recall_pairing_different_fleet_rejected(self):
        """Different fleet for booking → REASON_RECALL_CAMPAIGN_MISMATCH.

        Mutation guard: without the fleet check, pair_booking would return the
        booking successfully (same VIN + same campaign_id).
        """
        g = self._recall_grounding(fleet=_FLEET)
        b = self._recall_booking(fleet=_FLEET2)  # wrong fleet
        result, reason = pair_booking(
            grounding=g, candidates=[b], decided_ids=set(), now_iso=_NOW,
        )
        assert result is None, (
            "A booking from a different fleet must not be paired with the grounding. "
            "Without the fleet check, same VIN+campaign would pair successfully."
        )
        assert reason == REASON_RECALL_CAMPAIGN_MISMATCH

    def test_fh_pairing_still_checks_fleet(self):
        """FH pairing already checked fleet (regression guard: ensure it still does)."""
        gid = _fid()
        g = {
            "finding_id": gid,
            "agent_id": FH_AGENT,
            "scope": {"vin": _VIN},
            "fleet_id": _FLEET,
            "severity": "p0",
            "computed_at": "2026-11-05T05:00:00Z",
            "expires_at": _FUTURE,
            "recommended_action": {
                "action_kind": "ground_vehicle",
                "target_system": "cms",
                "parameters": {"vehicle_vin": _VIN, "fault_codes": ["P0001"]},
            },
            "disclosure_txt": "Stop-driving",
            "evidence": [],
        }
        b_wrong_fleet = {
            "finding_id": _fid(),
            "agent_id": FH_AGENT,
            "scope": {"vin": _VIN},
            "fleet_id": _FLEET2,  # wrong fleet
            "severity": "p0",
            "computed_at": "2026-11-05T05:10:00Z",
            "expires_at": _FUTURE,
            "recommended_action": {
                "action_kind": "book_fleet_service",
                "target_system": "dms",
                "parameters": {
                    "vehicle_vin": _VIN,
                    "needs": [{"need_id": "fault:P0001", "labor_op": "LBR"}],
                    "dealer_id": "DLR-001",
                    "urgency": "this_week",
                },
            },
            "disclosure_txt": "Stop-driving",
            "evidence": [],
        }
        from agents.fi_sim_operator.pairing import REASON_NO_BOOKING_ITEM
        result, reason = pair_booking(
            grounding=g, candidates=[b_wrong_fleet],
            decided_ids=set(), now_iso=_NOW,
        )
        assert result is None
        assert reason == REASON_NO_BOOKING_ITEM


# ── Fix (d): build_plan uses (booking, reason) tuples ────────────────────────

class TestBuildPlanPairingTupleFixD:
    """(d) paired_bookings values are (booking, reason) tuples.

    When ``reason`` is not None, pairing failed; count the reason as the skip,
    not the generic "pair_missing".

    Mutation guard (mutant = pair when reason is "disclosure_differs"):
      A pair with ``reason="disclosure_differs"`` would be approved, sending a
      booking whose directive differs from the grounding's.
    """

    def _grounding(self, fid: str) -> dict:
        return {
            "finding_id": fid,
            "agent_id": FH_AGENT,
            "scope": {"vin": _VIN},
            "fleet_id": _FLEET,
            "severity": "p0",
            "computed_at": "2026-11-05T05:00:00Z",
            "expires_at": _FUTURE,
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
            "agent_id": FH_AGENT,
            "scope": {"vin": _VIN},
            "fleet_id": _FLEET,
            "severity": "p0",
            "computed_at": "2026-11-05T05:10:00Z",
            "expires_at": _FUTURE,
            "recommended_action": {
                "action_kind": "book_fleet_service",
                "target_system": "dms",
                "parameters": {
                    "vehicle_vin": _VIN,
                    "needs": [{"need_id": "fault:P0001", "labor_op": "LBR"}],
                    "dealer_id": "DLR-001",
                    "urgency": "this_week",
                },
            },
            "evidence": [],
        }

    def test_plan_successful_pair_uses_booking(self):
        """reason=None → booking is used; unit has 2 findings."""
        gid = _fid()
        g = self._grounding(gid)
        b = self._booking()
        plan = build_plan(
            classified=[_cf(g, CLASS_FH_GROUND_BOOK)],
            paired_bookings={gid: (b, None)},  # success
            first_seen={gid: _old_enough(22), b["finding_id"]: _old_enough(22)},
            run_at=_NOW,
            remaining_cap=5,
        )
        assert len(plan.units) == 1
        assert plan.units[0].decision_count == 2

    def test_plan_disclosure_differs_is_skip(self):
        """reason='disclosure_differs' → skip counted as 'disclosure_differs'.

        Mutation guard (mutant = pair when reason is 'disclosure_differs'):
          Without this check, a booking whose directive mismatches the grounding
          would be approved — an unsafe pair.

        The old code would check ``if booking is None: skip`` — passing a booking
        with a non-None reason would have approved it since the booking dict is
        truthy. The new code checks ``pair_reason is not None`` first.
        """
        from agents.fi_sim_operator.pairing import REASON_DISCLOSURE_DIFFERS
        gid = _fid()
        g = self._grounding(gid)
        b = self._booking()
        plan = build_plan(
            classified=[_cf(g, CLASS_FH_GROUND_BOOK)],
            paired_bookings={gid: (b, REASON_DISCLOSURE_DIFFERS)},
            first_seen={gid: _old_enough(22)},
            run_at=_NOW,
            remaining_cap=5,
        )
        assert len(plan.units) == 0, (
            "disclosure_differs must skip the pair entirely — not approve it. "
            "Mutant: pairing on a booking with reason='disclosure_differs' would "
            "approve a pair where the disclosure directive differs from the grounding."
        )
        assert plan.skip_counts.get(REASON_DISCLOSURE_DIFFERS, 0) == 1

    def test_plan_no_dealer_is_skip(self):
        """reason='no_dealer' → skip counted as 'no_dealer'."""
        from agents.fi_sim_operator.pairing import REASON_NO_DEALER
        gid = _fid()
        g = self._grounding(gid)
        b = self._booking()
        plan = build_plan(
            classified=[_cf(g, CLASS_FH_GROUND_BOOK)],
            paired_bookings={gid: (b, REASON_NO_DEALER)},
            first_seen={gid: _old_enough(22)},
            run_at=_NOW,
            remaining_cap=5,
        )
        assert len(plan.units) == 0
        assert plan.skip_counts.get(REASON_NO_DEALER, 0) == 1

    def test_plan_none_booking_with_none_reason_is_pair_missing(self):
        """(None, None) should count as pair_missing (defensive)."""
        from agents.fi_sim_operator.select import SKIP_PAIR_MISSING
        gid = _fid()
        g = self._grounding(gid)
        plan = build_plan(
            classified=[_cf(g, CLASS_FH_GROUND_BOOK)],
            paired_bookings={gid: (None, None)},
            first_seen={gid: _old_enough(22)},
            run_at=_NOW,
            remaining_cap=5,
        )
        assert len(plan.units) == 0
        assert plan.skip_counts.get(SKIP_PAIR_MISSING, 0) == 1

    def test_plan_not_in_paired_bookings_is_pair_missing(self):
        """Grounding not in paired_bookings → pair_missing (no key at all)."""
        from agents.fi_sim_operator.select import SKIP_PAIR_MISSING
        gid = _fid()
        g = self._grounding(gid)
        plan = build_plan(
            classified=[_cf(g, CLASS_FH_GROUND_BOOK)],
            paired_bookings={},  # empty
            first_seen={gid: _old_enough(22)},
            run_at=_NOW,
            remaining_cap=5,
        )
        assert len(plan.units) == 0
        assert plan.skip_counts.get(SKIP_PAIR_MISSING, 0) == 1
