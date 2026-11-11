"""
Item 1 of task 1.4 — D7 saving formulas.

Spec: .kiro/specs/2026-10-02-fi-cost-stage-b/spec.md § D7

Each test loads a case from fixtures/savings_golden.json (also used by CMS
task 6.4) and asserts:

  - estimated_value_usd matches the formula's result from the evidence values;
  - estimated_value_basis matches the template for that action.

The golden fixture is the single source of truth shared with CMS's display
tests, so both repos agree on rounding.

All tests are ``xfail(strict=True)`` because ``agents.fi_cost.savings`` does
not exist yet; Group 3 makes them pass.  They must fail with ImportError or
NotImplementedError, never with SyntaxError or a test-logic error.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

FIXTURE = Path(__file__).parent / "fixtures" / "savings_golden.json"
_golden = json.loads(FIXTURE.read_text())


# ---------------------------------------------------------------------------
# shift_to_depot_off_peak
# ---------------------------------------------------------------------------


def test_shift_to_depot_off_peak_typical():
    """D7: public_and_peak_kwh x (mean_price - depot_off_peak_price) — typical case."""
    from agents.fi_cost.savings import estimate_saving  # noqa: F401

    case = _golden["formulas"]["shift_to_depot_off_peak"]["cases"][0]
    result = estimate_saving("shift_to_depot_off_peak", case["input"])
    assert result["estimated_value_usd"] == pytest.approx(
        case["expected"]["estimated_value_usd"], rel=1e-4
    ), "estimated_value_usd must equal the formula result, not a model output"
    assert result["estimated_value_basis"] == case["expected"]["estimated_value_basis"]


def test_shift_to_depot_off_peak_zero_kwh():
    """D7: zero public/peak kWh → saving is zero."""
    from agents.fi_cost.savings import estimate_saving  # noqa: F401

    case = _golden["formulas"]["shift_to_depot_off_peak"]["cases"][1]
    result = estimate_saving("shift_to_depot_off_peak", case["input"])
    assert result["estimated_value_usd"] == pytest.approx(0.0, abs=0.01)


# ---------------------------------------------------------------------------
# schedule_inspection
# ---------------------------------------------------------------------------


def test_schedule_inspection_excess():
    """D7: maintenance excess above cohort baseline."""
    from agents.fi_cost.savings import estimate_saving  # noqa: F401

    case = _golden["formulas"]["schedule_inspection"]["cases"][0]
    result = estimate_saving("schedule_inspection", case["input"])
    assert result["estimated_value_usd"] == pytest.approx(
        case["expected"]["estimated_value_usd"], rel=1e-4
    )
    assert result["estimated_value_basis"] == case["expected"]["estimated_value_basis"]


def test_schedule_inspection_rounding():
    """D7: fractional inputs round consistently (shared with CMS display tests)."""
    from agents.fi_cost.savings import estimate_saving  # noqa: F401

    case = _golden["formulas"]["schedule_inspection"]["cases"][1]
    result = estimate_saving("schedule_inspection", case["input"])
    assert result["estimated_value_usd"] == pytest.approx(
        case["expected"]["estimated_value_usd"], abs=0.01
    ), "CMS display tests pin this rounding; both must agree"


# ---------------------------------------------------------------------------
# reassign_or_remarket
# ---------------------------------------------------------------------------


def test_reassign_or_remarket_low_utilisation():
    """D7: (lease + insurance) x (1 - miles / fleet_median_miles)."""
    from agents.fi_cost.savings import estimate_saving  # noqa: F401

    case = _golden["formulas"]["reassign_or_remarket"]["cases"][0]
    result = estimate_saving("reassign_or_remarket", case["input"])
    assert result["estimated_value_usd"] == pytest.approx(
        case["expected"]["estimated_value_usd"], rel=1e-4
    )
    assert result["estimated_value_basis"] == case["expected"]["estimated_value_basis"]


def test_reassign_or_remarket_floor_at_zero():
    """D7: vehicle above fleet median → saving is floored at 0, not negative."""
    from agents.fi_cost.savings import estimate_saving  # noqa: F401

    case = _golden["formulas"]["reassign_or_remarket"]["cases"][1]
    result = estimate_saving("reassign_or_remarket", case["input"])
    assert result["estimated_value_usd"] == pytest.approx(0.0, abs=0.01), (
        "Saving must be >= 0; a vehicle used above the fleet median produces no saving"
    )


# ---------------------------------------------------------------------------
# driver_coaching
# ---------------------------------------------------------------------------


def test_driver_coaching_inefficient():
    """D7: energy_cost x (1 - cohort_kwh/vehicle_kwh)."""
    from agents.fi_cost.savings import estimate_saving  # noqa: F401

    case = _golden["formulas"]["driver_coaching"]["cases"][0]
    result = estimate_saving("driver_coaching", case["input"])
    assert result["estimated_value_usd"] == pytest.approx(
        case["expected"]["estimated_value_usd"], rel=1e-4
    )
    assert result["estimated_value_basis"] == case["expected"]["estimated_value_basis"]


def test_driver_coaching_at_fleet_efficiency_zero():
    """D7: vehicle at fleet efficiency → saving is zero."""
    from agents.fi_cost.savings import estimate_saving  # noqa: F401

    case = _golden["formulas"]["driver_coaching"]["cases"][1]
    result = estimate_saving("driver_coaching", case["input"])
    assert result["estimated_value_usd"] == pytest.approx(0.0, abs=0.01)


# ---------------------------------------------------------------------------
# no_action produces no recommended_action
# ---------------------------------------------------------------------------


def test_no_action_returns_none():
    """D7: no_action → no recommended_action (estimate_saving returns None)."""
    from agents.fi_cost.savings import estimate_saving  # noqa: F401

    result = estimate_saving(
        "no_action",
        {"vehicle_energy_cost": 500.0, "month": "2026-08"},
    )
    assert result is None, "no_action must produce no estimated saving"
