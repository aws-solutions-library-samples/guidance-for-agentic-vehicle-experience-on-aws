"""Tests for `_compute_health_score` — the vehicle health score.

WHY THIS EXISTS
---------------
This function is documented as "the single source of truth for both iOS and CMS
UI", and iOS `TradeInScore` consumes its output, so it also moves an indicative
trade-in valuation. It had **no tests at all** before 2026-08-21.

That mattered immediately: the battery state-of-health factor added that day is
the first change to the formula since it moved server-side, and it deliberately
retires the docstring's old promise that "the visible score doesn't change for
any vehicle". A formula that feeds a money-adjacent number should not be edited
without a characterisation suite, so the pre-existing branches are pinned here
too, not just the new one.

Loading pattern: importlib.util.spec_from_file_location (no __init__.py), which
matches the sibling lambda test suites.
"""

from __future__ import annotations

import importlib.util
import sys
from decimal import Decimal
from pathlib import Path

import pytest

_HANDLER_PATH = Path(__file__).parent.parent / "handler.py"
_spec = importlib.util.spec_from_file_location("vehicle_context_handler", _HANDLER_PATH)
handler = importlib.util.module_from_spec(_spec)
sys.modules["vehicle_context_handler"] = handler
_spec.loader.exec_module(handler)


def score(**kwargs):
    """`_compute_health_score` with healthy defaults, overridden per test."""
    params = {
        "active_dtcs": [],
        "scheduled_service_first_row": None,
        "live_connection_status": "connected",
    }
    params.update(kwargs)
    return handler._compute_health_score(**params)


def reasons(result):
    return [d["reason"] for d in result["deductions"]]


# ---------------------------------------------------------------------------
# Characterisation of the pre-existing formula
# ---------------------------------------------------------------------------

def test_healthy_vehicle_scores_100():
    r = score()
    assert r["score"] == 100
    assert r["deductions"] == []
    assert r["computedAt"].endswith("Z")


@pytest.mark.parametrize("severity,amount", [
    ("CRITICAL", 30), ("HIGH", 15), ("MEDIUM", 8),
    ("MODERATE", 8),   # normalised to MEDIUM
    ("LOW", 4), ("", 4), ("WEIRD", 4),   # default branch
])
def test_dtc_severity_weights(severity, amount):
    r = score(active_dtcs=[{"code": "P0299", "severity": severity}])
    assert r["score"] == 100 - amount


def test_severity_is_case_insensitive():
    assert score(active_dtcs=[{"code": "P0420", "severity": "critical"}])["score"] == 70


def test_disconnected_deducts_five():
    r = score(live_connection_status="disconnected")
    assert r["score"] == 95
    assert "Vehicle disconnected" in reasons(r)


def test_score_clamps_at_zero():
    dtcs = [{"code": f"P{i:04d}", "severity": "CRITICAL"} for i in range(10)]
    assert score(active_dtcs=dtcs)["score"] == 0


# ---------------------------------------------------------------------------
# Battery state of health (added 2026-08-21)
# ---------------------------------------------------------------------------

def test_degraded_bev_pack_is_deducted():
    r = score(battery_soh=74, fuel_type="electric")
    assert r["score"] == 90
    assert "Battery health 74%" in reasons(r)


def test_reason_carries_the_measured_figure_not_just_the_threshold():
    """The drill-down shows why without a second round trip."""
    assert "Battery health 61%" in reasons(score(battery_soh=61, fuel_type="bev"))


def test_pack_at_threshold_is_not_deducted():
    """80 is the warn point, not a failure — `< 80`, not `<= 80`."""
    r = score(battery_soh=80, fuel_type="electric")
    assert r["score"] == 100
    assert r["deductions"] == []


def test_healthy_pack_is_not_deducted():
    assert score(battery_soh=94, fuel_type="electric")["score"] == 100


def test_absent_soh_is_not_treated_as_degraded():
    """Absent is not degraded. A vehicle whose telemetry has not reported must
    not be penalised, and must not be silently treated as healthy either — it
    simply contributes nothing."""
    assert score(battery_soh=None, fuel_type="electric")["score"] == 100


def test_boto3_decimal_is_accepted():
    """DynamoDB numbers arrive as `Decimal`, which is what production passes."""
    r = score(battery_soh=Decimal("74"), fuel_type="electric")
    assert r["score"] == 90
    assert "Battery health 74%" in reasons(r)


def test_non_numeric_soh_is_skipped_rather_than_raising():
    assert score(battery_soh="n/a", fuel_type="electric")["score"] == 100


@pytest.mark.parametrize("fuel_type", ["bev", "electric", "ev", "ELECTRIC", " Electric "])
def test_bev_synonyms_all_score(fuel_type):
    """Mirrors iOS `isElectric(_:)`, which accepts these three lowercased."""
    assert score(battery_soh=74, fuel_type=fuel_type)["score"] == 90


@pytest.mark.parametrize("fuel_type", ["gasoline", "diesel", "petrol", None, ""])
def test_non_ev_is_never_scored_on_pack_health(fuel_type):
    """An ICE row's battery fields describe a 12V starter battery. Scoring that
    as state of health would flag healthy combustion vehicles — the same class of
    error as the "Engine 0°F on a BEV" defect, in the other direction."""
    assert score(battery_soh=40, fuel_type=fuel_type)["score"] == 100


@pytest.mark.parametrize("fuel_type", ["hybrid", "phev", "mhev"])
def test_hybrids_are_excluded(fuel_type):
    """Deliberate: a hybrid's pack has different warranty and replacement
    semantics, and iOS does not render the battery card for it either."""
    assert score(battery_soh=74, fuel_type=fuel_type)["score"] == 100


def test_battery_stacks_with_other_deductions():
    r = score(
        active_dtcs=[{"code": "P0299", "severity": "HIGH"}],
        live_connection_status="disconnected",
        battery_soh=74,
        fuel_type="electric",
    )
    assert r["score"] == 100 - 15 - 5 - 10
    assert set(reasons(r)) == {"DTC P0299 HIGH", "Vehicle disconnected", "Battery health 74%"}


def test_threshold_and_deduction_are_named_constants():
    """Pins the iOS coupling: `VehicleTabView` warns at `batterySoh < 80`. If
    this constant moves, that warn threshold moves in the same change."""
    assert handler._BATTERY_SOH_THRESHOLD == 80.0
    assert handler._BATTERY_SOH_DEDUCTION == 10


# ---------------------------------------------------------------------------
# The demo vehicle
# ---------------------------------------------------------------------------

def test_demo_trailwind_score_is_unchanged_by_the_battery_factor():
    """VEH-MRDN-0015 (2023 Meridian Trailwind, BEV) carries `batterySoh: 94`, so
    the new factor must not move the demo's visible score or its trade-in
    valuation. Pinned because this is the vehicle every demo shows."""
    without = score(fuel_type="electric")
    with_soh = score(battery_soh=Decimal("94"), fuel_type="electric")
    assert with_soh["score"] == without["score"] == 100
    assert with_soh["deductions"] == []


def test_is_bev_matches_ios_predicate():
    for truthy in ("bev", "electric", "ev", "BEV", "Electric"):
        assert handler._is_bev(truthy) is True
    for falsy in ("gasoline", "diesel", "hybrid", "phev", "", None, "   "):
        assert handler._is_bev(falsy) is False
