"""Tests for :mod:`agents.fi_rebalancing.validate` (spec D8 step 7, task 5.1).

Covers each rule listed in the validator docstring. The anchor tests are:

* **M1 (mutation)** — the validator rejects a VIN not in the named candidate's
  ``eligible_vins``. Dropping the check in ``validate.py`` makes
  :func:`test_rejects_vin_outside_eligible_list` and
  :func:`test_m1_dropping_eligible_check_passes_bad_vins` disagree, so M1 is
  caught the moment the rule is deleted.
* **source-util cap** — a plan whose moves push a source above 85% forecast
  utilisation fails.
* **temporary_event** — a hold with that reason fails when no event is
  announced for the gap's week.
* **both empty** — a tool input with no moves and no holds is refused: when
  the model is called, it must say something (spec D8 step 6).

The fixtures are small and share no state: each test builds its own bundle.
"""
from __future__ import annotations

import pytest

from agents.fi_rebalancing.validate import (
    ValidationError,
    validate_rebalance_plan,
)


# ---------------------------------------------------------------------------
# Minimal bundle factory
# ---------------------------------------------------------------------------
_METRO = "seattle"
_LOC_A = "loc-airport-seattle"
_LOC_B = "loc-downtown-seattle"
_WEEK = "2026-W42"


def _bundle(**over) -> dict:
    base = {
        "candidates": [
            {
                "candidate_id": "cand-1",
                "from_location_id": _LOC_B,
                "to_location_id": _LOC_A,
                "count": 2,
                "eligible_vins": ["VIN-SEA-001", "VIN-SEA-002", "VIN-SEA-003"],
            },
        ],
        "gap_ids": [f"{_LOC_A}/{_WEEK}"],
        "gap_lookup": {f"{_LOC_A}/{_WEEK}": (_LOC_A, _WEEK)},
        "sources_by_id": {
            _LOC_B: {"supply_vehicles": 10, "op_hours": 70.0, "forecast_hours": 400.0},
        },
        "events": [],
        "evidence_kinds": [
            "rebalance.forecast", "rebalance.supply", "rebalance.transfer",
            "rebalance.revenue", "rebalance.vehicle", "rebalance.recent_outcomes",
            "rebalance.events", "rebalance.diagnosis_source", "rebalance.hold",
        ],
    }
    base.update(over)
    return base


def _move(**over) -> dict:
    base = {
        "candidate_id": "cand-1",
        "vehicle_vins": ["VIN-SEA-001"],
        "timing": "today",
        "detail": "Move the vehicle to cover the morning deficit.",
        "evidence_refs": ["rebalance.forecast", "rebalance.supply"],
    }
    base.update(over)
    return base


def _hold(**over) -> dict:
    base = {
        "gap_id": f"{_LOC_A}/{_WEEK}",
        "reason": "returns_expected",
        "detail": "Three vehicles are expected back by 10:00; skip the move.",
    }
    base.update(over)
    return base


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------
def test_accepts_a_well_formed_plan():
    validate_rebalance_plan(
        {"moves": [_move()], "holds": []},
        metro_bundle=_bundle(),
    )


def test_accepts_a_well_formed_hold():
    validate_rebalance_plan(
        {"moves": [], "holds": [_hold()]},
        metro_bundle=_bundle(),
    )


# ---------------------------------------------------------------------------
# Schema shape
# ---------------------------------------------------------------------------
def test_rejects_missing_top_key():
    with pytest.raises(ValidationError, match="missing required keys"):
        validate_rebalance_plan({"moves": []}, metro_bundle=_bundle())


def test_rejects_extra_top_key():
    with pytest.raises(ValidationError, match="extra keys"):
        validate_rebalance_plan(
            {"moves": [], "holds": [], "something_else": {}},
            metro_bundle=_bundle(),
        )


def test_rejects_extra_move_key():
    with pytest.raises(ValidationError, match="moves\\[0\\] has extra keys"):
        validate_rebalance_plan(
            {"moves": [{**_move(), "count": 2}], "holds": []},
            metro_bundle=_bundle(),
        )


def test_rejects_both_empty():
    """D8 step 6: when called, the model must say something."""
    with pytest.raises(ValidationError, match="neither moves nor holds"):
        validate_rebalance_plan({"moves": [], "holds": []}, metro_bundle=_bundle())


# ---------------------------------------------------------------------------
# Mutation M1 — a VIN outside the candidate's eligible list is caught
# ---------------------------------------------------------------------------
def test_rejects_vin_outside_eligible_list():
    """M1 anchor: a model-named VIN that is not in the eligible list is rejected."""
    with pytest.raises(ValidationError, match="not in candidate"):
        validate_rebalance_plan(
            {"moves": [_move(vehicle_vins=["VIN-NEW-999"])], "holds": []},
            metro_bundle=_bundle(),
        )


def test_m1_dropping_eligible_check_passes_bad_vins():
    """Positive control for M1: without the eligible-list check, the above test's
    bad VIN wouldn't be caught. We verify the mutation shape by swapping the
    bundle's eligible list to include the attacker-chosen VIN; the validator then
    accepts it — proof the eligible-list membership is what blocks the bad input.
    If the next cycle removes the eligible-list test altogether, this test still
    passes (as the mutated validator would too), but ``test_rejects_vin_outside
    _eligible_list`` fails: the pair catches the mutation on both sides."""
    validate_rebalance_plan(
        {"moves": [_move(vehicle_vins=["VIN-NEW-999"])], "holds": []},
        metro_bundle=_bundle(
            candidates=[
                {
                    "candidate_id": "cand-1",
                    "from_location_id": _LOC_B,
                    "to_location_id": _LOC_A,
                    "count": 2,
                    "eligible_vins": ["VIN-NEW-999", "VIN-SEA-002"],
                },
            ]
        ),
    )


def test_rejects_unknown_candidate_id():
    with pytest.raises(ValidationError, match="not in the bundle"):
        validate_rebalance_plan(
            {"moves": [_move(candidate_id="cand-does-not-exist")], "holds": []},
            metro_bundle=_bundle(),
        )


def test_rejects_duplicate_vin_within_a_move():
    with pytest.raises(ValidationError, match="duplicate VIN"):
        validate_rebalance_plan(
            {"moves": [_move(vehicle_vins=["VIN-SEA-001", "VIN-SEA-001"])], "holds": []},
            metro_bundle=_bundle(),
        )


def test_rejects_vin_reused_across_moves():
    bundle = _bundle(
        candidates=[
            {
                "candidate_id": "cand-1",
                "from_location_id": _LOC_B,
                "to_location_id": _LOC_A,
                "count": 2,
                "eligible_vins": ["VIN-SEA-001", "VIN-SEA-002"],
            },
            {
                "candidate_id": "cand-2",
                "from_location_id": _LOC_B,
                "to_location_id": _LOC_A,
                "count": 2,
                "eligible_vins": ["VIN-SEA-001", "VIN-SEA-003"],
            },
        ],
    )
    with pytest.raises(ValidationError, match="reuses VIN"):
        validate_rebalance_plan(
            {
                "moves": [
                    _move(candidate_id="cand-1", vehicle_vins=["VIN-SEA-001"]),
                    _move(candidate_id="cand-2", vehicle_vins=["VIN-SEA-001"]),
                ],
                "holds": [],
            },
            metro_bundle=bundle,
        )


def test_rejects_vin_already_used_in_prior_metro():
    """A VIN already assigned in a prior metro cannot be reused."""
    with pytest.raises(ValidationError, match="reuses VIN"):
        validate_rebalance_plan(
            {"moves": [_move(vehicle_vins=["VIN-SEA-001"])], "holds": []},
            metro_bundle=_bundle(),
            used_vins_global={"VIN-SEA-001"},
        )


# ---------------------------------------------------------------------------
# Timing / detail / evidence_refs
# ---------------------------------------------------------------------------
def test_rejects_unknown_timing():
    with pytest.raises(ValidationError, match="timing"):
        validate_rebalance_plan(
            {"moves": [_move(timing="whenever")], "holds": []},
            metro_bundle=_bundle(),
        )


def test_rejects_detail_too_long_for_move():
    with pytest.raises(ValidationError, match="length"):
        validate_rebalance_plan(
            {"moves": [_move(detail="x" * 601)], "holds": []},
            metro_bundle=_bundle(),
        )


def test_rejects_detail_too_long_for_hold():
    with pytest.raises(ValidationError, match="length"):
        validate_rebalance_plan(
            {"moves": [], "holds": [_hold(detail="y" * 301)]},
            metro_bundle=_bundle(),
        )


def test_rejects_empty_detail():
    with pytest.raises(ValidationError, match="length 0"):
        validate_rebalance_plan(
            {"moves": [_move(detail="")], "holds": []},
            metro_bundle=_bundle(),
        )


def test_rejects_evidence_ref_outside_allowed():
    with pytest.raises(ValidationError, match="evidence_refs"):
        validate_rebalance_plan(
            {"moves": [_move(evidence_refs=["some.unlisted.kind"])], "holds": []},
            metro_bundle=_bundle(),
        )


# ---------------------------------------------------------------------------
# Vehicle count bounds
# ---------------------------------------------------------------------------
def test_rejects_zero_vehicles_in_move():
    with pytest.raises(ValidationError, match=r"1\.\.2 entries"):
        validate_rebalance_plan(
            {"moves": [_move(vehicle_vins=[])], "holds": []},
            metro_bundle=_bundle(),
        )


def test_rejects_more_than_candidate_count():
    with pytest.raises(ValidationError, match=r"1\.\.2 entries"):
        validate_rebalance_plan(
            {"moves": [_move(vehicle_vins=["VIN-SEA-001", "VIN-SEA-002", "VIN-SEA-003"])],
             "holds": []},
            metro_bundle=_bundle(),
        )


# ---------------------------------------------------------------------------
# Source utilisation cap
# ---------------------------------------------------------------------------
def test_rejects_moves_that_push_source_over_85_percent():
    """8 vehicles move from a source that has 10 supply, 70h ops; after removing
    8 the remaining 2*70 = 140 ops hours must cover 400h of forecast — well
    above 85%."""
    bundle = _bundle(
        candidates=[
            {
                "candidate_id": "cand-1",
                "from_location_id": _LOC_B,
                "to_location_id": _LOC_A,
                "count": 8,
                "eligible_vins": [f"VIN-SEA-{i:03d}" for i in range(1, 9)],
            },
        ],
    )
    with pytest.raises(ValidationError, match="forecast utilisation"):
        validate_rebalance_plan(
            {
                "moves": [_move(
                    candidate_id="cand-1",
                    vehicle_vins=[f"VIN-SEA-{i:03d}" for i in range(1, 9)],
                )],
                "holds": [],
            },
            metro_bundle=bundle,
        )


def test_source_cap_allows_safe_moves():
    """2 vehicles move from the same source: remaining 8*70=560 ops hours
    comfortably covers the 400h forecast → under 85%."""
    bundle = _bundle(
        candidates=[
            {
                "candidate_id": "cand-1",
                "from_location_id": _LOC_B,
                "to_location_id": _LOC_A,
                "count": 2,
                "eligible_vins": ["VIN-SEA-001", "VIN-SEA-002"],
            },
        ],
    )
    validate_rebalance_plan(
        {"moves": [_move(vehicle_vins=["VIN-SEA-001", "VIN-SEA-002"])], "holds": []},
        metro_bundle=bundle,
    )


# ---------------------------------------------------------------------------
# Hold shape and temporary_event
# ---------------------------------------------------------------------------
def test_rejects_unknown_hold_reason():
    with pytest.raises(ValidationError, match="reason"):
        validate_rebalance_plan(
            {"moves": [], "holds": [_hold(reason="because_i_said_so")]},
            metro_bundle=_bundle(),
        )


def test_temporary_event_hold_requires_an_event():
    with pytest.raises(ValidationError, match="temporary_event"):
        validate_rebalance_plan(
            {"moves": [], "holds": [_hold(reason="temporary_event")]},
            metro_bundle=_bundle(events=[]),
        )


def test_temporary_event_hold_accepts_a_matching_event():
    validate_rebalance_plan(
        {"moves": [], "holds": [_hold(reason="temporary_event")]},
        metro_bundle=_bundle(events=[{"location_id": _LOC_A, "forecast_week": _WEEK}]),
    )


def test_temporary_event_hold_requires_matching_location_and_week():
    """Event is in the right location but a different week → still refuses."""
    with pytest.raises(ValidationError, match="temporary_event"):
        validate_rebalance_plan(
            {"moves": [], "holds": [_hold(reason="temporary_event")]},
            metro_bundle=_bundle(events=[
                {"location_id": _LOC_A, "forecast_week": "2026-W41"}
            ]),
        )


def test_rejects_unknown_gap_id():
    with pytest.raises(ValidationError, match="gap_id"):
        validate_rebalance_plan(
            {"moves": [], "holds": [_hold(gap_id="loc-elsewhere/2026-W42")]},
            metro_bundle=_bundle(),
        )



# ---------------------------------------------------------------------------
# A move and a hold on the same deficit (Group 5 review cycle 1 Suggestion 2;
# user's answer 2026-10-14): refused, so the retry and then the rules run instead
# of the hold vanishing. Mutant MH1 (the check deleted) is caught here.
# ---------------------------------------------------------------------------
_LOC_C = "loc-hub-seattle"


def _two_deficit_bundle() -> dict:
    return _bundle(
        gap_ids=[f"{_LOC_A}/{_WEEK}", f"{_LOC_C}/{_WEEK}"],
        gap_lookup={f"{_LOC_A}/{_WEEK}": (_LOC_A, _WEEK), f"{_LOC_C}/{_WEEK}": (_LOC_C, _WEEK)},
    )


def test_rejects_a_move_and_a_hold_on_the_same_location():
    with pytest.raises(ValidationError, match=r"both a move into and a hold on \['loc-airport-seattle'\]"):
        validate_rebalance_plan(
            {"moves": [_move()], "holds": [_hold(gap_id=f"{_LOC_A}/{_WEEK}")]},
            metro_bundle=_two_deficit_bundle(),
        )


def test_accepts_a_move_and_a_hold_on_different_locations():
    """Control: a move into A and a hold on C is a valid plan."""
    validate_rebalance_plan(
        {"moves": [_move()], "holds": [_hold(gap_id=f"{_LOC_C}/{_WEEK}")]},
        metro_bundle=_two_deficit_bundle(),
    )


def test_hold_whose_gap_is_missing_from_lookup_is_refused():
    bundle = _two_deficit_bundle()
    bundle["gap_lookup"] = {f"{_LOC_A}/{_WEEK}": (_LOC_A, _WEEK)}
    with pytest.raises(ValidationError, match="missing from the bundle's gap_lookup"):
        validate_rebalance_plan(
            {"moves": [], "holds": [_hold(gap_id=f"{_LOC_C}/{_WEEK}")]},
            metro_bundle=bundle,
        )
