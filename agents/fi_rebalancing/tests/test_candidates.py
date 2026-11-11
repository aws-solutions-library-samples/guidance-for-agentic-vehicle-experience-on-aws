"""Tests for candidates.py and fallback.py (spec G4-D3..D5, task 4.1c)."""
from __future__ import annotations

import pytest

from agents.fi_rebalancing.candidates import build_candidates, money_for
from agents.fi_rebalancing.fallback import rules_fallback

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------
_TODAY = "2026-10-10"
_RUN = "2026-10-10"
# effective window: 2026-10-11 .. 2026-10-17
_EFF = [f"2026-10-{d:02d}" for d in range(11, 18)]
# horizon = same when run_date == today
_HOR = _EFF


def _loc(lid, metro="sea", loc_type="airport", lat=47.4, lon=-122.3, parking=20, op="8-18"):
    return {"locationId": lid, "name": lid, "metro": metro, "locationType": loc_type,
            "latitude": lat, "longitude": lon, "parkingSpaces": parking,
            "operatingHours": op, "op_hours": 10.0}


def _gap(lid, status="deficit", short=2, spare=0, supply=5, fc=50.0):
    return {"location_id": lid, "status": status, "vehicles_short": short,
            "vehicles_spare": spare, "supply_vehicles": supply,
            "forecast_hours": fc, "forecast_utilization_pct": 0.92, "data_as_of": _TODAY}


def _fc_rows(loc_id, dates=None, hours=55.0, mape=0.10):
    dates = dates or _EFF
    return [{"location_id": loc_id, "forecast_date": d, "forecast_hours": hours,
             "method": "ml", "mape_28d": mape, "run_date": _RUN, "data_as_of": _TODAY}
            for d in dates]


def _elig(vins):
    return [{"vin": v, "days_idle_last_7": 3} for v in vins]


# The eight tests whose candidates the policy file's $100 floor drops (net between $50 and $100;
# min_net_benefit_usd was $50 until 2026-10-16) set their own floor, so the floor doesn't decide
# selection, count or confidence. The floor itself is tested by TestMinNetBenefit and
# test_policy.test_config_file_holds_the_approved_values.
_NO_FLOOR = {"min_net_benefit_usd": 0.0}


def _base_args(deficit_id="loc-sea-airport", surplus_id="loc-sea-dt",
               short=2, spare=4, assigned=None, elig_vins=None, cfg=None,
               extra_gaps=None, extra_locs=None, extra_fc=None):
    locs = [_loc(deficit_id, metro="sea", loc_type="airport", lat=47.4, lon=-122.3),
            _loc(surplus_id, metro="sea", loc_type="downtown", lat=47.6, lon=-122.3)]
    if extra_locs:
        locs.extend(extra_locs)
    gaps = [_gap(deficit_id, short=short, supply=5),
            _gap(surplus_id, status="surplus", spare=spare, supply=10)]
    if extra_gaps:
        gaps.extend(extra_gaps)
    elig_vins = elig_vins or [f"SEAVIN{i:03d}" for i in range(1, 9)]
    assigned_rows = assigned or []
    forecast = _fc_rows(deficit_id) + _fc_rows(surplus_id)
    if extra_fc:
        forecast.extend(extra_fc)
    return dict(gaps=gaps, locations=locs, forecast=forecast, outcomes=[],
                assigned=assigned_rows,
                eligible_by_location={surplus_id: _elig(elig_vins)},
                request_hours=[], config=cfg or {}, today=_TODAY)


# ---------------------------------------------------------------------------
# build_candidates tests
# ---------------------------------------------------------------------------

class TestMetroBoundary:
    def test_nearer_surplus_in_other_metro_not_used(self):
        """A surplus location in a different metro must never be a candidate source."""
        deficit = _loc("loc-sea-airport", metro="sea", lat=47.4, lon=-122.3)
        near_other = _loc("loc-pdx-dt", metro="pdx", lat=47.5, lon=-122.3)  # nearer but wrong metro
        far_same = _loc("loc-sea-hub", metro="sea", lat=47.8, lon=-122.4)
        gaps = [_gap("loc-sea-airport", short=1, supply=5),
                _gap("loc-pdx-dt", status="surplus", spare=3, supply=8),
                _gap("loc-sea-hub", status="surplus", spare=3, supply=8)]
        fc = _fc_rows("loc-sea-airport") + _fc_rows("loc-pdx-dt") + _fc_rows("loc-sea-hub")
        result = build_candidates(
            gaps=gaps, locations=[deficit, near_other, far_same], forecast=fc,
            outcomes=[], assigned=[],
            eligible_by_location={"loc-pdx-dt": _elig(["VIN-PDX-001", "VIN-PDX-002"]),
                                   "loc-sea-hub": _elig(["VIN-SEA-H01", "VIN-SEA-H02"])},
            request_hours=[], config=_NO_FLOOR, today=_TODAY)
        src_ids = {c["from_location_id"] for c in result["candidates"]}
        assert "loc-pdx-dt" not in src_ids, "Cross-metro surplus must never be a source"
        assert src_ids == {"loc-sea-hub"}, f"the same-metro surplus must be the source, got {src_ids}"


class TestNearestFirstAndCap:
    def test_nearest_first_order_and_three_candidate_cap(self):
        """Candidates are in ascending road-distance order; at most 3 per deficit."""
        deficit = _loc("loc-sea-airport", metro="sea", lat=47.4, lon=-122.3)
        s1 = _loc("loc-sea-s1", metro="sea", lat=47.41, lon=-122.3)   # nearest
        s2 = _loc("loc-sea-s2", metro="sea", lat=47.5, lon=-122.3)
        s3 = _loc("loc-sea-s3", metro="sea", lat=47.6, lon=-122.3)
        s4 = _loc("loc-sea-s4", metro="sea", lat=47.9, lon=-122.3)    # 4th — must be dropped
        gaps = [_gap("loc-sea-airport", short=1, supply=5),
                *[_gap(l, status="surplus", spare=3, supply=8)
                  for l in ("loc-sea-s1","loc-sea-s2","loc-sea-s3","loc-sea-s4")]]
        fc = sum((_fc_rows(l) for l in
                  ("loc-sea-airport","loc-sea-s1","loc-sea-s2","loc-sea-s3","loc-sea-s4")), [])
        eb = {l: _elig([f"VIN-{l[-2:]}-{i}" for i in range(1, 3)])
              for l in ("loc-sea-s1","loc-sea-s2","loc-sea-s3","loc-sea-s4")}
        result = build_candidates(gaps=gaps, locations=[deficit, s1, s2, s3, s4],
                                  forecast=fc, outcomes=[], assigned=[], eligible_by_location=eb,
                                  request_hours=[], config={}, today=_TODAY)
        cands = result["candidates"]
        assert len(cands) <= 3, "At most 3 candidates per deficit"
        assert "loc-sea-s4" not in {c["from_location_id"] for c in cands}
        dists = [c["distance_miles"] for c in cands]
        assert dists == sorted(dists), "Candidates must be in nearest-first order"


class TestCountBinding:
    def _build(self, short, spare, elig_count, max_veh=10):
        # Use negative min_net to isolate count-binding logic from net-benefit filtering
        args = _base_args(short=short, spare=spare,
                          cfg={"max_vehicles_per_move": max_veh, "min_net_benefit_usd": -999999.0},
                          elig_vins=[f"VIN-{i:03d}" for i in range(1, elig_count + 1)])
        return build_candidates(**args)

    def test_short_is_binding(self):
        result = self._build(short=2, spare=10, elig_count=20)
        assert result["candidates"][0]["count"] == 2

    def test_spare_is_binding(self):
        result = self._build(short=10, spare=3, elig_count=20)
        assert result["candidates"][0]["count"] == 3

    def test_max_is_binding(self):
        result = self._build(short=10, spare=10, elig_count=20, max_veh=2)
        assert result["candidates"][0]["count"] == 2


class TestParkingCut:
    def test_parking_cut_reduces_count(self):
        """Count is cut when assigned + count would exceed parking spaces."""
        # parking=20, assigned=19 → only 1 slot left
        assigned_rows = [{"vin": f"V{i}", "location_id": "loc-sea-airport"} for i in range(19)]
        args = _base_args(short=4, spare=4, assigned=assigned_rows)
        args["locations"][0] = _loc("loc-sea-airport", parking=20)
        result = build_candidates(**args)
        assert result["candidates"][0]["count"] == 1

    def test_parking_full_drops_candidate(self):
        """A deficit with no parking space produces no candidate."""
        assigned_rows = [{"vin": f"V{i}", "location_id": "loc-sea-airport"} for i in range(20)]
        args = _base_args(short=2, spare=4, assigned=assigned_rows)
        args["locations"][0] = _loc("loc-sea-airport", parking=20)
        result = build_candidates(**args)
        assert len(result["candidates"]) == 0
        assert "loc-sea-airport" in result["uncovered_deficits"]


class TestEligibleFewerThanCount:
    def test_count_cut_to_eligible_count(self):
        """If fewer eligible VINs than count, count is cut to eligible count."""
        args = _base_args(short=5, spare=5, elig_vins=["VIN-A01", "VIN-A02"], cfg=_NO_FLOOR)
        cands = build_candidates(**args)["candidates"]
        assert cands, "the two eligible VINs must still make a candidate"
        assert cands[0]["count"] == 2


class TestMinNetBenefit:
    def test_candidate_dropped_when_net_below_min(self):
        # Use a very high min so any realistic scenario fails
        args = _base_args(cfg={"min_net_benefit_usd": 999999.0})
        result = build_candidates(**args)
        assert len(result["candidates"]) == 0

    def test_candidate_kept_when_net_above_min(self):
        args = _base_args(cfg={"min_net_benefit_usd": 0.0})
        result = build_candidates(**args)
        assert len(result["candidates"]) >= 1


class TestForecastDaysMissing:
    def test_missing_effective_window_day_drops_candidate(self):
        """Candidate is dropped when any effective-window day has no forecast row."""
        # Only provide 6 of 7 effective-window days for the surplus location
        partial_fc = _fc_rows("loc-sea-dt", dates=_EFF[:-1])  # missing last day
        args = _base_args(extra_fc=partial_fc)
        # Replace the default forecast to have only partial surplus data
        args["forecast"] = _fc_rows("loc-sea-airport") + partial_fc
        result = build_candidates(**args)
        assert len(result["candidates"]) == 0
        assert result["counts"]["forecast_days_missing"] >= 1


class TestDepotType:
    def test_depot_deficit_produces_no_candidate(self):
        """A deficit location with locationType='depot' has no candidate."""
        depot = _loc("loc-sea-depot", metro="sea", loc_type="depot")
        surplus = _loc("loc-sea-dt", metro="sea", loc_type="downtown", lat=47.6, lon=-122.3)
        gaps = [_gap("loc-sea-depot", short=2, supply=3),
                _gap("loc-sea-dt", status="surplus", spare=5, supply=10)]
        fc = _fc_rows("loc-sea-depot") + _fc_rows("loc-sea-dt")
        result = build_candidates(gaps=gaps, locations=[depot, surplus], forecast=fc,
                                  outcomes=[], assigned=[],
                                  eligible_by_location={"loc-sea-dt": _elig(["VIN-D01","VIN-D02"])},
                                  request_hours=[], config={}, today=_TODAY)
        assert len(result["candidates"]) == 0
        assert "loc-sea-depot" in result["uncovered_deficits"]


# ---------------------------------------------------------------------------
# fallback tests
# ---------------------------------------------------------------------------

class TestFallbackSharedSource:
    def _shared_source_candidates(self):
        vins = [f"VIN-SEA-{i:03d}" for i in range(1, 7)]
        return [
            {"candidate_id": "cand-loc-a-loc-sea-hub", "from_location_id": "loc-sea-hub",
             "to_location_id": "loc-a", "eligible_vins": vins[:4], "net_benefit_usd": 200.0,
             "count": 2, "vehicles_spare": 3},
            {"candidate_id": "cand-loc-b-loc-sea-hub", "from_location_id": "loc-sea-hub",
             "to_location_id": "loc-b", "eligible_vins": vins[2:6], "net_benefit_usd": 100.0,
             "count": 2, "vehicles_spare": 3},
        ]

    def test_two_deficits_never_exceed_spare(self):
        """Total vehicles taken from one source never exceeds its vehicles_spare."""
        cands = self._shared_source_candidates()
        moves = rules_fallback(candidates=cands, model_call=lambda: None)
        taken = sum(len(m["vehicle_vins"]) for m in moves
                    if m["from_location_id"] == "loc-sea-hub")
        assert taken <= 3, "Total vehicles moved from a source must not exceed its spare"

    def test_vins_never_reused_across_moves(self):
        """A VIN assigned to one move must not appear in any other move."""
        cands = self._shared_source_candidates()
        moves = rules_fallback(candidates=cands, model_call=lambda: None)
        all_vins = [v for m in moves for v in m["vehicle_vins"]]
        assert len(all_vins) == len(set(all_vins)), "VINs must be unique across all moves"


class TestFallbackRecompute:
    def _cand_with_per_day(self, count=2):
        per_day = [{"date": d, "forecast_hours": 55.0, "supply_vehicles": 4,
                    "op_hours": 10.0, "source_forecast_hours": 40.0,
                    "source_supply_vehicles": 8, "source_op_hours": 10.0}
                   for d in _EFF]
        return {"candidate_id": "cand-loc-sea-airport-loc-sea-dt",
                "from_location_id": "loc-sea-dt", "to_location_id": "loc-sea-airport",
                "eligible_vins": [f"VIN-R{i}" for i in range(1, count + 2)],
                "net_benefit_usd": 300.0, "count": count, "vehicles_spare": count,
                "per_day": per_day, "rate": 3.50, "source_rate": 3.00,
                "transfer_cost_per_vehicle": 30.0,
                "max_utilization": 0.95, "mean_request_hours": 4.0}

    def test_recompute_on_smaller_count_changes_money(self):
        """When count is reduced, money_for with the new count changes net_benefit_usd."""
        cand = self._cand_with_per_day(count=2)
        m2 = money_for(cand, 2)
        m1 = money_for(cand, 1)
        assert m2["net_benefit_usd"] != m1["net_benefit_usd"], \
            "Recompute with smaller count must produce different net_benefit_usd"

    def test_candidate_missing_per_day_dropped_when_recompute_needed(self):
        """A candidate without per_day is dropped when count is scaled down."""
        # 2 eligible VINs but count=3 → actual_count=2 < 3 → recompute needed → no per_day → drop
        cand = {"candidate_id": "cand-loc-sea-airport-loc-sea-dt",
                "from_location_id": "loc-sea-dt", "to_location_id": "loc-sea-airport",
                "eligible_vins": ["VIN-X01", "VIN-X02"],
                "net_benefit_usd": 200.0, "count": 3, "vehicles_spare": 3}
        moves = rules_fallback(candidates=[cand], model_call=lambda: None)
        assert len(moves) == 0, "Candidate missing per_day must be dropped when recompute needed"



# ---------------------------------------------------------------------------
# Architect check (2026-10-10): up to 3 sources each offer a destination its whole
# shortfall, so the rules plan must cap the destination's total by its shortfall and
# its free parking. Without the cap a 2-short airport received 6 vehicles.
# ---------------------------------------------------------------------------

def _offer(src: str, vins: list[str], *, net: float, short: int = 2, parking: int = 10,
           spare: int = 5, count: int = 2) -> dict:
    return {
        "candidate_id": f"cand-loc-sea-airport-{src}",
        "from_location_id": src,
        "to_location_id": "loc-sea-airport",
        "count": count,
        "eligible_vins": vins,
        "vehicles_short": short,
        "vehicles_spare": spare,
        "parking_room": parking,
        "net_benefit_usd": net,
        "transfer_cost_usd": 80.0,
        "revenue_gain_usd": 300.0,
        "source_loss_usd": 0.0,
        "revenue_impact_usd": 300.0,
        "predicted_on_rent_hours_gain": 60.0,
        "predicted_turnaways_avoided": 3,
    }


def test_fallback_caps_destination_by_its_shortfall():
    from agents.fi_rebalancing.fallback import rules_fallback

    offers = [
        _offer("loc-sea-hub-1", ["MRDHUB1VIN000001A", "MRDHUB1VIN000002A"], net=300.0),
        _offer("loc-sea-hub-2", ["MRDHUB2VIN000001A", "MRDHUB2VIN000002A"], net=250.0),
        _offer("loc-sea-hub-3", ["MRDHUB3VIN000001A", "MRDHUB3VIN000002A"], net=200.0),
    ]
    moves = rules_fallback(candidates=offers, model_call=None)
    sent = sum(len(m["vehicle_vins"]) for m in moves)
    assert sent == 2, f"a 2-short destination must get 2 vehicles in total, got {sent}"
    assert [m["from_location_id"] for m in moves] == ["loc-sea-hub-1"]


def test_fallback_caps_destination_by_free_parking():
    from agents.fi_rebalancing.fallback import rules_fallback

    offers = [
        _offer("loc-sea-hub-1", ["MRDHUB1VIN000001A", "MRDHUB1VIN000002A"], net=300.0,
               short=4, parking=1, count=2),
    ]
    offers[0]["per_day"] = None  # a recompute is needed for 1 of 2; no inputs -> dropped
    assert rules_fallback(candidates=offers, model_call=None) == []


@pytest.mark.parametrize("missing", ["count", "vehicles_spare", "vehicles_short", "parking_room"])
def test_fallback_drops_a_candidate_missing_a_cap(missing):
    from agents.fi_rebalancing.fallback import rules_fallback

    offer = _offer("loc-sea-hub-1", ["MRDHUB1VIN000001A", "MRDHUB1VIN000002A"], net=300.0)
    del offer[missing]
    assert rules_fallback(candidates=[offer], model_call=None) == []



# ---------------------------------------------------------------------------
# Architect check (2026-10-10, mutants C3, C7, C9, C10, F3, F4).
# ---------------------------------------------------------------------------

def test_four_surplus_sources_give_the_nearest_three():
    hubs = [_loc(f"loc-sea-hub-{i}", metro="sea", loc_type="carshare_hub",
                 lat=47.4 + 0.05 * i, lon=-122.3) for i in range(1, 5)]
    args = _base_args(short=2, spare=0, cfg=_NO_FLOOR,
                      extra_locs=hubs,
                      extra_gaps=[_gap(h["locationId"], status="surplus", spare=3, supply=10)
                                  for h in hubs],
                      extra_fc=[r for h in hubs for r in _fc_rows(h["locationId"], hours=20.0)])
    args["gaps"] = [g for g in args["gaps"] if g["location_id"] != "loc-sea-dt"]
    args["eligible_by_location"] = {
        h["locationId"]: _elig([f"MRDHUB{i}VIN00000{j}" for j in range(1, 5)])
        for i, h in enumerate(hubs, start=1)}
    sources = [c["from_location_id"] for c in build_candidates(**args)["candidates"]]
    assert sources == ["loc-sea-hub-1", "loc-sea-hub-2", "loc-sea-hub-3"]


def test_candidate_lists_twice_its_count():
    vins = [f"MRDSEAVIN0000000{i}" for i in range(1, 6)]
    cands = build_candidates(**_base_args(short=2, spare=4, elig_vins=vins,
                                          cfg=_NO_FLOOR))["candidates"]
    assert cands and cands[0]["count"] == 2
    assert cands[0]["eligible_vins"] == vins[:4]


def test_gain_is_capped_by_the_moved_vehicles_hours():
    per_day = [{"date": d, "forecast_hours": 500.0, "supply_vehicles": 1, "op_hours": 10.0,
                "source_forecast_hours": 0.0, "source_supply_vehicles": 10,
                "source_op_hours": 10.0} for d in _EFF]
    cand = {"per_day": per_day, "rate": 3.5, "source_rate": 3.0,
            "transfer_cost_per_vehicle": 40.0, "max_utilization": 0.95,
            "mean_request_hours": 9.5}
    money = money_for(cand, 2)
    # unserved = 500 - 1*10*0.95 = 490.5 h/day, far above 2 x 10 x 0.95 = 19 h/day.
    assert money["predicted_on_rent_hours_gain"] == pytest.approx(19.0 * 7)
    assert money["revenue_gain_usd"] == pytest.approx(19.0 * 7 * 3.5)
    assert money["predicted_turnaways_avoided"] == 14  # floor(133 / 9.5)


def test_null_outcome_ratio_is_ignored_in_the_downgrade():
    args = _base_args(cfg=_NO_FLOOR)
    args["outcomes"] = [
        {"to_location_id": "loc-sea-airport", "effective_date": "2026-10-01", "ratio_hours": None},
        {"to_location_id": "loc-sea-airport", "effective_date": "2026-09-25", "ratio_hours": 0.3},
    ]
    cands = build_candidates(**args)["candidates"]
    # MAPE 0.10 -> high; the one real ratio (0.3 < 0.5) lowers it to medium.
    assert cands and cands[0]["confidence"] == "medium"


def _two_destinations(spare: int, vins_a: list[str], vins_b: list[str]) -> list[dict]:
    base = dict(from_location_id="loc-sea-dt", vehicles_spare=spare, vehicles_short=2,
                parking_room=10, count=2, transfer_cost_usd=80.0, revenue_gain_usd=300.0,
                source_loss_usd=0.0, revenue_impact_usd=300.0,
                predicted_on_rent_hours_gain=60.0, predicted_turnaways_avoided=3)
    return [
        {**base, "candidate_id": "cand-loc-sea-airport-loc-sea-dt",
         "to_location_id": "loc-sea-airport", "eligible_vins": vins_a, "net_benefit_usd": 300.0},
        {**base, "candidate_id": "cand-loc-sea-hub-1-loc-sea-dt",
         "to_location_id": "loc-sea-hub-1", "eligible_vins": vins_b, "net_benefit_usd": 250.0},
    ]


def test_shared_source_is_never_drawn_past_its_spare():
    vins = ["MRDDTVIN000000001", "MRDDTVIN000000002", "MRDDTVIN000000003", "MRDDTVIN000000004"]
    moves = rules_fallback(candidates=_two_destinations(2, vins, vins[2:] + vins[:2]),
                           model_call=None)
    assert sum(len(m["vehicle_vins"]) for m in moves) == 2


def test_shared_source_never_sends_one_vin_twice():
    vins = ["MRDDTVIN000000001", "MRDDTVIN000000002", "MRDDTVIN000000003", "MRDDTVIN000000004"]
    moves = rules_fallback(candidates=_two_destinations(4, vins, vins), model_call=None)
    sent = [v for m in moves for v in m["vehicle_vins"]]
    assert len(moves) == 2 and len(sent) == len(set(sent)) == 4



# ---------------------------------------------------------------------------
# F2 fixes: W3, W4, W5, W12
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# W3: money_for uses compute_revenue_gain_per_day + formulas.py chain; source_rate required
# ---------------------------------------------------------------------------

def _per_day_rows():
    """Seven per-day rows, each with non-zero unserved demand."""
    return [
        {
            "date": d, "forecast_hours": 60.0, "supply_vehicles": 3, "op_hours": 10.0,
            "source_forecast_hours": 30.0, "source_supply_vehicles": 8, "source_op_hours": 10.0,
        }
        for d in _EFF
    ]


def test_money_for_source_rate_required():
    """money_for raises ValueError when source_rate is absent (M4 re-anchor).

    Mutation M4 re-anchor: drop the ValueError check → money_for returns a value
    (using the destination rate for source loss), so no raise → test fails.
    """
    cand = {
        "per_day": _per_day_rows(),
        "rate": 3.50,
        # source_rate intentionally absent
        "transfer_cost_per_vehicle": 30.0,
        "max_utilization": 0.95,
    }
    with pytest.raises(ValueError, match="source_rate"):
        money_for(cand, 2)


def test_money_for_source_loss_uses_source_rate_not_destination_rate():
    """source_loss_usd is priced at source_rate (3.00), not destination rate (3.50).

    With a source location over-supplied, moving 2 vehicles causes zero source loss
    (the source stays below capacity). Set source just at capacity so moving 1 vehicle
    creates a measurable loss, then assert the exact value at source_rate != dest_rate.

    Mutation M4 re-anchor: `source_rate = candidate.get("source_rate")` →
    `source_rate = rate` (uses dest rate 3.50 instead of 3.00) → source_loss_usd
    differs → assertion fails.
    """
    # Source: supply=1, op=10, max_util=0.95 → cap=9.5 h/day.
    # Source forecast=10 h/day → 0 loss before move (cap > F? no: F=10 > 9.5 → unserved=0.5)
    # Wait: loss = max(0, unserved(S-moved) - unserved(S))
    # unserved(S=1) = max(0, 10 - 1*10*0.95) = max(0, 10-9.5) = 0.5
    # unserved(S=0) = max(0, 10 - 0) = 10
    # loss_per_day = (10 - 0.5) * source_rate = 9.5 * source_rate
    # With source_rate=3.00: 9.5*3.00 = 28.50 per day × 7 days = 199.50
    # With source_rate=3.50: 9.5*3.50 = 33.25 per day × 7 days = 232.75
    per_day = [
        {
            "date": d, "forecast_hours": 40.0, "supply_vehicles": 5, "op_hours": 10.0,
            "source_forecast_hours": 10.0, "source_supply_vehicles": 1, "source_op_hours": 10.0,
        }
        for d in _EFF
    ]
    cand = {
        "per_day": per_day,
        "rate": 3.50,
        "source_rate": 3.00,
        "transfer_cost_per_vehicle": 0.0,
        "max_utilization": 0.95,
    }
    money = money_for(cand, 1)
    # source_loss = 9.5 * 3.00 * 7 = 199.50
    assert money["source_loss_usd"] == pytest.approx(199.50, rel=1e-4), (
        f"source_loss_usd should be 199.50 (source_rate=3.00); got {money['source_loss_usd']}. "
        "Mutation M4: source_rate=rate (3.50) → 232.75 → fails."
    )


def test_money_for_net_uses_compute_revenue_impact_with_horizon_days_1():
    """money_for computes net via compute_revenue_impact(horizon_days=1).

    If horizon_days=0 were used instead, impact=0 and net=-transfer<0.
    With positive revenue and small transfer, the correct net is positive.

    Mutation F2-W3b: horizon_days=0 → impact=0 → net<0 → net_benefit_usd<0
    → assertion fails.
    """
    per_day = [
        {
            "date": d, "forecast_hours": 500.0, "supply_vehicles": 1, "op_hours": 10.0,
            "source_forecast_hours": 0.0, "source_supply_vehicles": 20, "source_op_hours": 10.0,
        }
        for d in _EFF
    ]
    cand = {
        "per_day": per_day,
        "rate": 3.50,
        "source_rate": 3.00,
        "transfer_cost_per_vehicle": 1.0,   # small transfer → net positive
        "max_utilization": 0.95,
    }
    money = money_for(cand, 2)
    assert money["net_benefit_usd"] > 0, (
        f"net_benefit_usd must be positive with large demand; got {money['net_benefit_usd']}. "
        "Mutation F2-W3b: horizon_days=0 → impact=0 → net=-1 < 0 → fails."
    )


def test_money_for_gain_uses_compute_revenue_gain_per_day():
    """gain_d = compute_revenue_gain_per_day(..., max_utilization=max_util, ...).

    Set forecast huge so unserved >> moved capacity. Then:
      moved_cap = count × op × max_util = 2 × 10 × 0.95 = 19 h/day.
      revenue_gain/day = 19 × rate.
      total over 7 days = 19 × 3.50 × 7 = 465.50.

    Mutation M5 re-anchor: pass max_utilization=1.0 instead of 0.95 →
      moved_cap = 2 × 10 × 1.0 = 20 h/day → gain = 20 × 3.50 × 7 = 490.00 → fails.
    """
    per_day = [
        {
            "date": d, "forecast_hours": 500.0, "supply_vehicles": 1, "op_hours": 10.0,
            "source_forecast_hours": 5.0, "source_supply_vehicles": 20, "source_op_hours": 10.0,
        }
        for d in _EFF
    ]
    cand = {
        "per_day": per_day,
        "rate": 3.50,
        "source_rate": 3.00,
        "transfer_cost_per_vehicle": 0.0,
        "max_utilization": 0.95,
    }
    money = money_for(cand, 2)
    assert money["revenue_gain_usd"] == pytest.approx(19.0 * 3.50 * 7, rel=1e-4), (
        f"revenue_gain_usd should be {19.0*3.50*7}; got {money['revenue_gain_usd']}. "
        "Mutation M5: max_utilization=1.0 → 20×3.5×7=490.0 → fails."
    )


# ---------------------------------------------------------------------------
# W4: rules_fallback drops candidate with a missing money field; configured minimum passes through
# ---------------------------------------------------------------------------

def test_fallback_drops_candidate_with_missing_money_field():
    """rules_fallback drops a count-unchanged candidate whose net_benefit is set but
    another money key is absent.

    The current rules-plan path copies money fields directly from the candidate.
    A candidate built without revenue_gain_usd used to default to 0.0; after W4
    the entire candidate is dropped.

    Mutation M6 re-anchor: `min_net = 0.0` → candidate passes the minimum check
    (0 < min_net=50 is no longer the gate; the drop is by key-presence now) →
    this particular test would still drop on missing key, so M6 is tested separately.
    """
    cand_missing_field = {
        "candidate_id": "cand-drop-test",
        "from_location_id": "loc-sea-dt",
        "to_location_id": "loc-sea-airport",
        "eligible_vins": ["VIN001", "VIN002"],
        "count": 2,
        "vehicles_spare": 3,
        "vehicles_short": 2,
        "parking_room": 5,
        "net_benefit_usd": 150.0,
        # revenue_gain_usd intentionally absent
        "transfer_cost_usd": 30.0,
        "source_loss_usd": 0.0,
        "revenue_impact_usd": 200.0,
        "predicted_on_rent_hours_gain": 60.0,
        "predicted_turnaways_avoided": 5,
    }
    moves = rules_fallback(candidates=[cand_missing_field], model_call=None)
    assert moves == [], (
        "Candidate missing a money field must be dropped. "
        "Mutation: keep the .get(..., 0.0) default → move written → fails."
    )


def test_fallback_min_net_benefit_usd_gates_plan():
    """rules_fallback with min_net_benefit_usd=100 drops a candidate whose recomputed net is 7.25.

    count=2 but only 1 VIN available → recompute with money_for(count=1).
    With count=1: unserved = 10 - 1*10*0.95 = 0.5 h/day; source far over-supplied (loss≈0).
      gain_per_day = min(0.5, 9.5)*3.50 = 1.75; total_gain = 12.25;
      transfer = 5.0; net = 12.25 − 5.0 = 7.25.
    7.25 < 100 → dropped (correct behavior).

    Mutation M6: min_net = 0.0 → net=7.25 > 0 → move written → assert fails → CAUGHT.
    """
    per_day = [
        {
            "date": d,
            "forecast_hours": 10.0,
            "supply_vehicles": 1,
            "op_hours": 10.0,
            "source_forecast_hours": 5.0,
            "source_supply_vehicles": 20,
            "source_op_hours": 10.0,
        }
        for d in _EFF
    ]
    cand = {
        "candidate_id": "cand-net-low",
        "from_location_id": "loc-sea-dt",
        "to_location_id": "loc-sea-airport",
        "eligible_vins": ["VIN001"],  # only 1 VIN; count=2 → actual_count=1 → recompute
        "count": 2,
        "vehicles_spare": 2,
        "vehicles_short": 2,
        "parking_room": 5,
        "per_day": per_day,
        "rate": 3.50,
        "source_rate": 3.50,
        "transfer_cost_per_vehicle": 5.0,
        "max_utilization": 0.95,
        "mean_request_hours": 4.0,
        # money for count=2 path (not reached — count is scaled to 1)
        "net_benefit_usd": 15.0,
        "transfer_cost_usd": 10.0,
        "revenue_gain_usd": 25.0,
        "source_loss_usd": 0.0,
        "revenue_impact_usd": 25.0,
        "predicted_on_rent_hours_gain": 7.0,
        "predicted_turnaways_avoided": 0,
    }
    moves = rules_fallback(candidates=[cand], model_call=None, min_net_benefit_usd=100.0)
    assert moves == [], (
        "Move with recomputed net=7.25 below min_net_benefit_usd=100 must be dropped. "
        "Mutation M6: min_net=0.0 → net=7.25 > 0 → written → fails."
    )


# ---------------------------------------------------------------------------
# W5: location_outlook requires all 7 horizon rows; empty forecast aborts
# ---------------------------------------------------------------------------

def test_location_outlook_raises_on_missing_horizon_row():
    """location_outlook raises ValueError when the location has < 7 horizon rows.

    Mutation: remove the missing-days check → no raise → pytest.raises fails.
    """
    from agents.fi_rebalancing.candidates import location_outlook  # noqa: PLC0415

    horizon = _EFF  # 7 days
    # Only 6 forecast rows for the location (missing the last day)
    fc = [
        {"location_id": "loc-x", "forecast_date": d, "forecast_hours": 50.0,
         "method": "ml", "mape_28d": 0.10, "run_date": _RUN, "data_as_of": _TODAY}
        for d in horizon[:6]
    ]
    with pytest.raises(ValueError, match="missing forecast"):
        location_outlook(
            "loc-x",
            forecast=fc,
            outcomes=[],
            supply=5.0,
            op_hours=10.0,
            deficit_above=0.90,
            max_utilization=0.95,
            horizon=horizon,
        )


def test_build_candidates_counts_forecast_days_missing_on_incomplete_location():
    """A deficit location with < 7 horizon rows is counted as forecast_days_missing,
    not silently treated as routine.

    Mutation: remove the ValueError check in build_candidates → the location gets
    a default severity instead of being counted → assertion on counts fails.
    """
    # Only provide 6 of 7 horizon rows for the deficit location
    fc_deficit_partial = _fc_rows("loc-sea-airport", dates=_EFF[:6])
    fc_surplus = _fc_rows("loc-sea-dt")
    args = _base_args()
    args["forecast"] = fc_deficit_partial + fc_surplus
    result = build_candidates(**args)
    assert result["counts"]["forecast_days_missing"] >= 1, (
        "A deficit with < 7 horizon rows must be counted as forecast_days_missing. "
        "Mutation: drop the ValueError → counted as 0 → fails."
    )
    # No candidate should be built for the incomplete location
    assert len(result["candidates"]) == 0


# ---------------------------------------------------------------------------
# W12: median of three outcome ratios (not the minimum)
# ---------------------------------------------------------------------------

def test_outcome_ratio_median_not_minimum_with_three_ratios():
    """With ratios [0.3, 0.8, 0.9], median=0.8 ≥ 0.5: no downgrade.
    Minimum=0.3 < 0.5 would downgrade if used instead.

    Mutation M8: ratio_med = rs[0] (minimum, 0.3) → confidence downgraded →
    assertion fails.
    """
    args = _base_args(cfg=_NO_FLOOR)
    args["outcomes"] = [
        {"to_location_id": "loc-sea-airport", "effective_date": "2026-10-03", "ratio_hours": 0.3},
        {"to_location_id": "loc-sea-airport", "effective_date": "2026-10-05", "ratio_hours": 0.8},
        {"to_location_id": "loc-sea-airport", "effective_date": "2026-10-07", "ratio_hours": 0.9},
    ]
    cands = build_candidates(**args)["candidates"]
    # MAPE 0.10 → high; median 0.8 ≥ 0.5 → no downgrade → confidence stays high.
    assert cands and cands[0]["confidence"] == "high", (
        f"Expected confidence=high (median=0.8 ≥ 0.5); got {cands[0].get('confidence')!r}. "
        "Mutation M8: rs[0]=0.3 < 0.5 → medium → fails."
    )


def test_outcome_ratio_median_even_count():
    """Even count (2 ratios): median = mean of the two middle values.

    Two ratios [0.4, 0.7]: median = (0.4+0.7)/2 = 0.55 ≥ 0.5 → no downgrade.
    Minimum (0.4) would downgrade.

    Mutation M8: rs[0] = 0.4 < 0.5 → downgrade → confidence not 'high' → fails.
    """
    args = _base_args(cfg=_NO_FLOOR)
    args["outcomes"] = [
        {"to_location_id": "loc-sea-airport", "effective_date": "2026-10-05", "ratio_hours": 0.4},
        {"to_location_id": "loc-sea-airport", "effective_date": "2026-10-07", "ratio_hours": 0.7},
    ]
    cands = build_candidates(**args)["candidates"]
    assert cands and cands[0]["confidence"] == "high", (
        f"Expected confidence=high (mean-of-two=0.55 ≥ 0.5); got {cands[0].get('confidence')!r}. "
        "Mutation M8: min=0.4 < 0.5 → medium → fails."
    )


def test_outcome_ratio_window_keeps_only_last_3():
    """With 4 outcomes, only the 3 most recent are used; window direction changes the verdict.

    Ratios oldest→newest: 2026-10-03→0.1, 2026-10-05→0.2, 2026-10-07→0.6, 2026-10-09→0.7.

    Last-3 (newest): 0.2, 0.6, 0.7 → median=0.6 ≥ 0.5 → no downgrade → confidence stays high.
    All 4 without [:3] (R1): 0.1, 0.2, 0.6, 0.7 → median=(0.2+0.6)/2=0.4 < 0.5 → downgrade.
    Oldest 3 with sort direction flipped (R2): 0.1, 0.2, 0.6 → median=0.2 < 0.5 → downgrade.
    Both mutations R1 (window removed) and R2 (oldest 3 instead of newest 3) must be caught.
    """
    args = _base_args(cfg=_NO_FLOOR)
    args["outcomes"] = [
        {"to_location_id": "loc-sea-airport", "effective_date": "2026-10-03", "ratio_hours": 0.1},
        {"to_location_id": "loc-sea-airport", "effective_date": "2026-10-05", "ratio_hours": 0.2},
        {"to_location_id": "loc-sea-airport", "effective_date": "2026-10-07", "ratio_hours": 0.6},
        {"to_location_id": "loc-sea-airport", "effective_date": "2026-10-09", "ratio_hours": 0.7},
    ]
    cands = build_candidates(**args)["candidates"]
    # Last 3 newest: 0.2, 0.6, 0.7 → median=0.6 ≥ 0.5 → no downgrade.
    # Without [:3] all 4 median=0.4 < 0.5 → downgrade (catches R1).
    # Oldest 3 median=0.2 < 0.5 → downgrade (catches R2).
    assert cands and cands[0]["confidence"] == "high", (
        f"Expected confidence=high (last-3 newest median=0.6 ≥ 0.5); "
        f"got {cands[0].get('confidence')!r}. "
        "Mutation R1 (window removed) or R2 (oldest 3) would downgrade to medium."
    )
