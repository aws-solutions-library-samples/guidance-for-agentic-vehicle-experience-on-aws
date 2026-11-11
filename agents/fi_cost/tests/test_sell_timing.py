"""
Item 5 of task 1.4 — sell timing golden vectors (D10).

Spec: .kiro/specs/2026-10-02-fi-cost-stage-b/spec.md § D10

D10: the crossover function is ported to agents/fi_cost/ as a pure function and
both repos run the same golden vectors (a vendored JSON fixture, byte-pinned in
both, as DMS and CMS pin their shared renderer).

This test:
  1. Pins the sha256 of fixtures/crossover_golden.json (generated from CMS
     services/fleet_intelligence/lifecycle.py via task 1.4 item 5 script).
     If the fixture drifts, the test fails before anything else runs.
  2. For each vector, calls agents/fi_cost/sell_timing.crossover_month() with
     the fixture's inputs and asserts the output equals the fixture's expected.
  3. Asserts that a vehicle with crossover_month within SELL_TIMING_HORIZON_MONTHS
     triggers a sell_timing Finding, and one beyond does not.

Bedrock is not called (D10: no model call for sell timing in this stage per U3).

All tests are xfail(strict=True).  Group 3 makes them pass.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

_FIXTURE_PATH = Path(__file__).parent / "fixtures" / "crossover_golden.json"

# Pinned sha256 of the crossover_golden.json generated from CMS lifecycle.py.
# If you regenerate the file, update this constant and record the mutation in decisions.md.
_PINNED_SHA256 = "ef28de7aa759706ece8f83a9904abb95d747d0e501ca43e836f3c4f16e6b68f0"


# ---------------------------------------------------------------------------
# Pin the fixture itself
# ---------------------------------------------------------------------------


def test_crossover_golden_fixture_sha256():
    """Fixture must not drift without an explicit re-pin (D10: one arithmetic).

    This is NOT xfail — if the fixture changes without a re-pin, something went
    wrong and we need to know immediately, before any other test runs.
    """
    text = _FIXTURE_PATH.read_bytes()
    actual = hashlib.sha256(text).hexdigest()
    assert actual == _PINNED_SHA256, (
        f"crossover_golden.json has drifted from the pinned sha256.\n"
        f"  pinned : {_PINNED_SHA256}\n"
        f"  actual : {actual}\n"
        "Re-generate from CMS lifecycle.py, update _PINNED_SHA256, and record "
        "the mutation in decisions.md."
    )


_golden = json.loads(_FIXTURE_PATH.read_text())
_HORIZON = _golden["_horizon_months"]  # 3


# ---------------------------------------------------------------------------
# Golden vector tests — each vector through agents/fi_cost/sell_timing
# ---------------------------------------------------------------------------


def test_sell_timing_no_crossover_vector():
    """Vector 'no_crossover': flat maintenance → crossover_month is None."""
    from agents.fi_cost.sell_timing import compute_crossover_month  # noqa: F401

    vec = next(v for v in _golden["vectors"] if v["name"] == "no_crossover")
    inp = vec["input"]
    result = compute_crossover_month(
        purchase_price=inp["vehicle_params"]["purchasePrice"],
        maintenance_series=inp["maintenance_series"],
        horizon_months=inp["horizon_months"],
    )
    assert result["crossover_month"] == vec["expected"]["crossover_month"]
    assert result["r_squared_meaningful"] == vec["expected"]["r_squared_meaningful"]
    assert result["series_length"] == vec["expected"]["series_length"]
    assert result["maintenance_fit"]["slope"] == pytest.approx(
        vec["expected"]["maintenance_fit"]["slope"], abs=1e-9
    )
    assert result["maintenance_fit"]["intercept"] == pytest.approx(
        vec["expected"]["maintenance_fit"]["intercept"], abs=1e-9
    )


def test_sell_timing_crossover_now_vector():
    """Vector 'crossover_now': maintenance crossing depreciation within horizon."""
    from agents.fi_cost.sell_timing import compute_crossover_month  # noqa: F401

    vec = next(v for v in _golden["vectors"] if v["name"] == "crossover_now")
    inp = vec["input"]
    result = compute_crossover_month(
        purchase_price=inp["vehicle_params"]["purchasePrice"],
        maintenance_series=inp["maintenance_series"],
        horizon_months=inp["horizon_months"],
    )
    assert result["crossover_month"] == vec["expected"]["crossover_month"], (
        f"Expected crossover_month={vec['expected']['crossover_month']!r}, "
        f"got {result['crossover_month']!r}"
    )
    assert result["maintenance_fit"]["slope"] == pytest.approx(
        vec["expected"]["maintenance_fit"]["slope"], abs=1e-9
    )


def test_sell_timing_beyond_horizon_vector():
    """Vector 'beyond_horizon': slow rise → no crossover at the 3-month horizon."""
    from agents.fi_cost.sell_timing import compute_crossover_month  # noqa: F401

    vec = next(v for v in _golden["vectors"] if v["name"] == "beyond_horizon")
    inp = vec["input"]
    result = compute_crossover_month(
        purchase_price=inp["vehicle_params"]["purchasePrice"],
        maintenance_series=inp["maintenance_series"],
        horizon_months=inp["horizon_months"],
    )
    assert result["crossover_month"] is None, (
        "Slowly rising maintenance should not cross depreciation within 3 months"
    )


# ---------------------------------------------------------------------------
# Finding trigger: crossover within horizon → triggers fleet.cost.sell_timing
# ---------------------------------------------------------------------------


def test_crossover_within_horizon_triggers_finding():
    """A vehicle with crossover_month within SELL_TIMING_HORIZON_MONTHS gets a Finding."""
    from agents.fi_cost.sell_timing import should_write_sell_timing_finding  # noqa: F401
    from agents.fi_cost.sell_timing import SELL_TIMING_HORIZON_MONTHS  # noqa: F401

    assert SELL_TIMING_HORIZON_MONTHS == _HORIZON, (
        f"SELL_TIMING_HORIZON_MONTHS must equal the fixture's _horizon_months ({_HORIZON})"
    )

    vec = next(v for v in _golden["vectors"] if v["name"] == "crossover_now")
    result = should_write_sell_timing_finding(
        crossover_month=vec["expected"]["crossover_month"],
        series_end_month="2026-09",
        horizon_months=SELL_TIMING_HORIZON_MONTHS,
        already_crossed=False,
    )
    assert result is True


def test_no_crossover_does_not_trigger_finding():
    """A vehicle with no crossover in the horizon must not get a sell_timing Finding."""
    from agents.fi_cost.sell_timing import should_write_sell_timing_finding  # noqa: F401
    from agents.fi_cost.sell_timing import SELL_TIMING_HORIZON_MONTHS  # noqa: F401

    result = should_write_sell_timing_finding(
        crossover_month=None,
        series_end_month="2026-09",
        horizon_months=SELL_TIMING_HORIZON_MONTHS,
        already_crossed=False,
    )
    assert result is False


# ---------------------------------------------------------------------------
# Horizon boundary tests (W3): exactly at horizon → writes; one month beyond → doesn't
# ---------------------------------------------------------------------------


def test_crossover_at_exactly_horizon_triggers_finding():
    """Crossover exactly at horizon_months (month +3) triggers a Finding (D10: 'within 3 months').

    W3 fix: no vector had a non-null crossover beyond the horizon, so
    ``should_write_sell_timing_finding`` could ignore ``horizon_months`` and always return
    True.  Passing crossover at exactly +3 months and at +4 months pins the boundary.
    """
    from agents.fi_cost.sell_timing import should_write_sell_timing_finding  # noqa: F401
    from agents.fi_cost.sell_timing import SELL_TIMING_HORIZON_MONTHS  # noqa: F401

    # series_end_month="2026-09", horizon=3 → "2026-12" is exactly 3 months ahead
    result = should_write_sell_timing_finding(
        crossover_month="2026-12",
        series_end_month="2026-09",
        horizon_months=SELL_TIMING_HORIZON_MONTHS,
        already_crossed=False,
    )
    assert result is True, (
        "Crossover at exactly horizon_months (3 months, '2026-12') must trigger a Finding "
        "(D10: 'within 3 months' is inclusive of the boundary)"
    )


def test_crossover_one_month_beyond_horizon_does_not_trigger():
    """Crossover one month beyond horizon (month +4) does NOT trigger a Finding (D10).

    W3 fix: complements the boundary test above.  Without this test, an implementation
    that always returns True for any non-None crossover_month would pass.
    """
    from agents.fi_cost.sell_timing import should_write_sell_timing_finding  # noqa: F401
    from agents.fi_cost.sell_timing import SELL_TIMING_HORIZON_MONTHS  # noqa: F401

    # series_end_month="2026-09", horizon=3 → "2027-01" is 4 months ahead (beyond)
    result = should_write_sell_timing_finding(
        crossover_month="2027-01",
        series_end_month="2026-09",
        horizon_months=SELL_TIMING_HORIZON_MONTHS,
        already_crossed=False,
    )
    assert result is False, (
        "Crossover one month beyond horizon (4 months, '2027-01') must NOT trigger a Finding"
    )


# ---------------------------------------------------------------------------
# Option (b), user 2026-10-07: only crossovers still ahead get a Finding
# ---------------------------------------------------------------------------

# Exact straight lines against 36,000 / 120 = 300.0 a month, so every comparison is exact.
_PRICE_300 = 36000.0
_STILL_AHEAD = {          # fitted value at the data month (index 2) is below 300
    1: [270.0, 280.0, 290.0],   # 290 now, 300 at k = 1
    2: [260.0, 270.0, 280.0],   # 280 now, 300 at k = 2
    3: [250.0, 260.0, 270.0],   # 270 now, 300 at k = 3
}
_ALREADY_OVER = [300.0, 310.0, 320.0]   # 320 now; _crossover_offset still reports k = 1
_EXACTLY_AT = [300.0, 300.0, 300.0]     # 300 now: equality counts as already crossed
_JUST_BELOW = [299.0, 299.0, 299.0]     # 299 now: not crossed (review cycle 1 S4)
_MONTHS = ["2026-07", "2026-08", "2026-09"]


def _rows(vin: str, costs: list[float]) -> list[dict]:
    return [{"vin": vin, "fleet_id": "f1", "year_month": m, "maintenance_cost": str(c),
             "purchase_price_usd": str(_PRICE_300)} for m, c in zip(_MONTHS, costs)]


def _series(costs: list[float]) -> list[dict]:
    return [{"yearMonth": m, "maintenanceCost": c} for m, c in zip(_MONTHS, costs)]


@pytest.mark.parametrize("crossover_month", ["2026-10", "2026-11", "2026-12"])
def test_already_crossed_never_triggers_a_finding(crossover_month):
    """A vehicle already past the crossover gets no Finding at k = 1, 2 or 3."""
    from agents.fi_cost.sell_timing import SELL_TIMING_HORIZON_MONTHS, should_write_sell_timing_finding

    assert should_write_sell_timing_finding(
        crossover_month, "2026-09", SELL_TIMING_HORIZON_MONTHS, already_crossed=True,
    ) is False


@pytest.mark.parametrize("bad", [None, 0, 1, "false", "no"])
def test_already_crossed_fails_closed_on_anything_but_false(bad):
    """Only the literal ``False`` lets a Finding through (fail closed)."""
    from agents.fi_cost.sell_timing import SELL_TIMING_HORIZON_MONTHS, should_write_sell_timing_finding

    assert should_write_sell_timing_finding(
        "2026-10", "2026-09", SELL_TIMING_HORIZON_MONTHS, already_crossed=bad,
    ) is False


def test_already_crossed_is_a_required_keyword():
    """A caller cannot skip the check by leaving the argument out or passing it by position."""
    from agents.fi_cost.sell_timing import should_write_sell_timing_finding

    with pytest.raises(TypeError):
        should_write_sell_timing_finding("2026-10", "2026-09", 3)  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        should_write_sell_timing_finding("2026-10", "2026-09", 3, False)  # type: ignore[misc]


@pytest.mark.parametrize("costs, expected", [
    (_STILL_AHEAD[1], False), (_STILL_AHEAD[2], False), (_STILL_AHEAD[3], False),
    (_ALREADY_OVER, True), (_EXACTLY_AT, True), (_JUST_BELOW, False),
])
def test_crossed_by_data_month(costs, expected):
    """k = 0 with the same ``>=`` as ``_crossover_offset``; equality counts as crossed."""
    from agents.fi_cost.sell_timing import compute_crossover_month, crossed_by_data_month

    assert crossed_by_data_month(compute_crossover_month(_PRICE_300, _series(costs), 3)) is expected


def test_the_crossover_arithmetic_still_reports_already_crossed_vehicles():
    """Option (b) leaves the ported arithmetic alone (D10, identical to CMS lifecycle.py):
    it still reports k = 1 for these; only the write decision drops them."""
    from agents.fi_cost.sell_timing import compute_crossover_month

    for costs in (_ALREADY_OVER, _EXACTLY_AT):
        assert compute_crossover_month(_PRICE_300, _series(costs), 3)["crossover_month"] == "2026-10"


@pytest.mark.parametrize("months_ahead", [1, 2, 3])
def test_plan_writes_a_crossover_still_ahead(months_ahead):
    from agents.fi_cost.sell_timing import plan_sell_timing

    vin = "MRDST000000000010"
    candidates, rejected = plan_sell_timing(_rows(vin, _STILL_AHEAD[months_ahead]), year_month="2026-09")
    assert rejected == 0
    assert [(c["vin"], c["crossover_month"]) for c in candidates] == [
        (vin, ["2026-10", "2026-11", "2026-12"][months_ahead - 1])]


@pytest.mark.parametrize("costs", [_ALREADY_OVER, _EXACTLY_AT], ids=["over", "exactly_at"])
def test_plan_skips_a_vehicle_already_past_the_crossover(costs):
    """463 → 126 on 2026-09 staging data (decisions.md 2026-10-07)."""
    from agents.fi_cost.sell_timing import plan_sell_timing

    rows = _rows("MRDST000000000011", costs) + _rows("MRDST000000000012", _STILL_AHEAD[1])
    candidates, rejected = plan_sell_timing(rows, year_month="2026-09")
    assert rejected == 0
    assert [c["vin"] for c in candidates] == ["MRDST000000000012"]


# ---------------------------------------------------------------------------
# Sell-timing action is reassign_or_remarket (D10)
# ---------------------------------------------------------------------------


def test_sell_timing_action_is_reassign_or_remarket():
    """The sell_timing Finding's recommended_action.cost_action must be reassign_or_remarket."""
    from agents.fi_cost.sell_timing import build_sell_timing_finding  # noqa: F401

    vec = next(v for v in _golden["vectors"] if v["name"] == "crossover_now")
    finding = build_sell_timing_finding(
        vin="MRD-TEST-VIN-003",
        fleet_id="fleet-meridian-01",
        crossover_month=vec["expected"]["crossover_month"],
        purchase_price=vec["input"]["vehicle_params"]["purchasePrice"],
        monthly_depreciation=vec["expected"]["depreciation_params"]["monthly_depreciation"],
        year_month="2026-09",
    )
    rec = finding.get("recommended_action", {})
    assert rec.get("parameters", {}).get("cost_action") == "reassign_or_remarket", (
        "D10: sell_timing Finding's cost_action must always be 'reassign_or_remarket'"
    )
    assert finding.get("finding_kind") == "fleet.cost.sell_timing"
