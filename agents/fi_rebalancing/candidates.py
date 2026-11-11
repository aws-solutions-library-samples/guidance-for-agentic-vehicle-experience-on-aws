"""candidates.py — build transfer candidates for fi-rebalancing (spec G4-D3, G4-D4, G4-D6)."""
from __future__ import annotations

import math
from collections import defaultdict
from datetime import date, timedelta
from typing import Any

from agents.fi_rebalancing import policy as rebal_policy
from agents.fi_rebalancing.formulas import (
    DEFAULTS,
    compute_confidence_band,
    compute_net_benefit,
    compute_revenue_gain_per_day,
    compute_revenue_impact,
    compute_severity_band,
    compute_source_loss_per_day,
    compute_transfer_cost,
    great_circle_miles,
)

# The U9 thresholds (max vehicles per move, min net benefit, deficit_above, max_utilization)
# come from agents.fi_rebalancing.policy, loaded from SSM per run; none is written here.

_VALID_TYPES: set[str] = {"airport", "downtown", "carshare_hub"}
_RATES: dict[str, float] = DEFAULTS["RATES"]


def horizon_days(run_date: str) -> list[str]:
    """Forecast dates run_date+1 .. run_date+7 as ISO strings."""
    d = date.fromisoformat(run_date)
    return [(d + timedelta(days=i)).isoformat() for i in range(1, 8)]


def effective_window(today: str) -> list[str]:
    """Effective dates today+1 .. today+7 as ISO strings (the money window)."""
    d = date.fromisoformat(today)
    return [(d + timedelta(days=i)).isoformat() for i in range(1, 8)]


def turned_away_share(
    forecast_rows: list[dict],
    location_id: str,
    supply_vehicles: int,
    op_hours: float,
    max_util: float,
    hdays: list[str],
) -> float:
    """Σ unserved / Σ forecast over horizon; 0 when Σ F = 0."""
    fc = {r["forecast_date"]: r for r in forecast_rows if r["location_id"] == location_id}
    total_f = total_u = 0.0
    for day in hdays:
        row = fc.get(day)
        if row is None:
            continue
        f = float(row["forecast_hours"])
        total_f += f
        total_u += max(0.0, f - supply_vehicles * op_hours * max_util)
    return total_u / total_f if total_f > 0 else 0.0


def first_deficit_day(
    hdays: list[str],
    forecast_rows: list[dict],
    location_id: str,
    supply_vehicles: int,
    op_hours: float,
    deficit_above: float,
    max_util: float,
) -> str:
    """Earliest horizon day where F/(S×op) > deficit_above or (S=0 and F>0); else hdays[0]."""
    fc = {r["forecast_date"]: r for r in forecast_rows if r["location_id"] == location_id}
    for day in hdays:
        row = fc.get(day)
        if row is None:
            continue
        f = float(row["forecast_hours"])
        cap = supply_vehicles * op_hours
        if supply_vehicles == 0:
            if f > 0:
                return day
        elif cap > 0 and f / cap > deficit_above:
            return day
    return hdays[0] if hdays else ""


def money_for(candidate: dict, count: int) -> dict:
    """Recompute all money figures for the given count from per_day inputs.

    Raises ValueError when per_day is absent or any required field is missing.
    """
    per_day = candidate.get("per_day")
    if not per_day:
        raise ValueError("candidate missing per_day inputs")
    rate = candidate.get("rate")
    if rate is None:
        raise ValueError("candidate missing rate")
    source_rate = candidate.get("source_rate")
    if source_rate is None:
        raise ValueError("candidate missing source_rate")
    tc_per_veh = candidate.get("transfer_cost_per_vehicle")
    if tc_per_veh is None:
        raise ValueError("candidate missing transfer_cost_per_vehicle")
    max_util = candidate.get("max_utilization")
    if max_util is None:
        max_util = rebal_policy.current()["max_utilization"]
    max_util = float(max_util)
    mean_req = candidate.get("mean_request_hours")

    transfer = tc_per_veh * count
    total_gain = total_loss = total_unserved_gain = 0.0

    required = ("forecast_hours", "supply_vehicles", "op_hours",
                "source_forecast_hours", "source_supply_vehicles", "source_op_hours")
    for d in per_day:
        for key in required:
            if key not in d:
                raise ValueError(f"per_day row missing field {key!r}")
        f, s, op = float(d["forecast_hours"]), float(d["supply_vehicles"]), float(d["op_hours"])
        sf, ss, sop = (float(d["source_forecast_hours"]), float(d["source_supply_vehicles"]),
                       float(d["source_op_hours"]))
        unserved = max(0.0, f - s * op * max_util)
        # Gain in dollars uses the canonical formula (test_core_pure + shared fixture pin this).
        gain_d = compute_revenue_gain_per_day(
            forecast_unserved_hours=unserved,
            moved_vehicles=count,
            vehicle_op_hours=op,
            max_utilization=max_util,
            usd_per_on_rent_hour=rate,
        )
        total_gain += gain_d
        # Gain in hours for turnaways: hours = gain_$ / rate (rate > 0 by construction).
        total_unserved_gain += gain_d / rate
        total_loss += compute_source_loss_per_day(
            source_forecast_hours=sf, source_supply_vehicles=ss,
            moved_vehicles=count, vehicle_op_hours=sop,
            max_utilization=max_util, usd_per_on_rent_hour=source_rate,
        )

    impact = compute_revenue_impact(
        revenue_gain_per_day=total_gain,
        source_loss_per_day=total_loss,
        horizon_days=1,
    )
    net = compute_net_benefit(revenue_impact=impact, transfer_cost=transfer)
    turnaways = math.floor(total_unserved_gain / mean_req) if mean_req and mean_req > 0 else 0
    return {
        "transfer_cost_usd": round(transfer, 2),
        "revenue_gain_usd": round(total_gain, 2),
        "source_loss_usd": round(total_loss, 2),
        "revenue_impact_usd": round(impact, 2),
        "net_benefit_usd": round(net, 2),
        "predicted_on_rent_hours_gain": round(total_unserved_gain, 4),
        "predicted_turnaways_avoided": turnaways,
    }



def location_outlook(location_id: str, *, forecast: list[dict], outcomes: list[dict],
                     supply: float, op_hours: float, deficit_above: float,
                     max_utilization: float, horizon: list[str]) -> dict:
    """First deficit day, ISO week, severity and confidence for one location (G4-D6).

    The single definition used by move candidates and by gap items, so the two can't
    disagree. ``outcomes`` are the location's own rows as a destination.
    """
    ta = turned_away_share(forecast, location_id, supply, op_hours, max_utilization, horizon)
    fdd = first_deficit_day(horizon, forecast, location_id, supply, op_hours,
                            deficit_above, max_utilization)
    loc_fc = [r for r in forecast if r["location_id"] == location_id]
    # Require all 7 horizon rows so move and gap items have a complete picture (W5).
    loc_fc_dates = {r["forecast_date"] for r in loc_fc}
    missing = [d for d in horizon if d not in loc_fc_dates]
    if missing:
        raise ValueError(
            f"location {location_id!r} missing forecast days: {missing}"
        )
    latest_run = max((r["run_date"] for r in loc_fc), default=None)
    mapes = [r["mape_28d"] for r in loc_fc
             if r.get("run_date") == latest_run and r.get("mape_28d") is not None]
    mape = (sum(mapes) / len(mapes)) if mapes else None
    recent = sorted(outcomes, key=lambda o: o["effective_date"], reverse=True)[:3]
    # A move with no prediction has a null ratio; it says nothing about delivery.
    rs = sorted(o["ratio_hours"] for o in recent if o.get("ratio_hours") is not None)
    n = len(rs)
    ratio_med = None if n == 0 else rs[n // 2] if n % 2 else (rs[n // 2 - 1] + rs[n // 2]) / 2.0
    return {
        "first_deficit_day": fdd,
        "forecast_week": date.fromisoformat(fdd).strftime("%G-W%V"),
        "turned_away_share": ta,
        "severity": compute_severity_band(turned_away_share=ta),
        "confidence": compute_confidence_band(forecast_mape=mape, recent_outcomes_ratio=ratio_med),
    }

def build_candidates(
    *,
    gaps: list[dict],
    locations: list[dict],
    forecast: list[dict],
    outcomes: list[dict],
    assigned: list[dict],
    eligible_by_location: dict[str, list[dict]],
    request_hours: list[dict],
    config: dict,
    today: str,
) -> dict:
    """Build move candidates for all deficit locations.

    Returns ``{candidates, uncovered_deficits, counts}``.
    """
    cfg = {**rebal_policy.current(), **config}
    max_veh: int = int(cfg["max_vehicles_per_move"])
    min_net: float = float(cfg["min_net_benefit_usd"])
    d_above: float = float(cfg["deficit_above"])
    max_util: float = float(cfg["max_utilization"])

    loc_by_id: dict[str, dict] = {loc["locationId"]: loc for loc in locations}
    assigned_count: dict[str, int] = {}
    for row in assigned:
        lid = row["location_id"]
        assigned_count[lid] = assigned_count.get(lid, 0) + 1
    req_by_loc: dict[str, float] = {r["location_id"]: r["mean_request_hours"] for r in request_hours}
    outcomes_by_dest: dict[str, list[dict]] = defaultdict(list)
    for o in outcomes:
        outcomes_by_dest[o["to_location_id"]].append(o)

    eff_win = effective_window(today)
    run_date = max((r["run_date"] for r in forecast), default=today)
    hdays = horizon_days(run_date)
    fc_idx: dict[tuple, dict] = {(r["location_id"], r["forecast_date"]): r for r in forecast}

    candidates: list[dict] = []
    uncovered: list[str] = []
    counts: dict[str, int] = {
        "candidates_built": 0, "forecast_days_missing": 0,
        "net_below_min": 0, "parking_full": 0, "no_surplus_in_metro": 0,
    }

    for gap in (g for g in gaps if g["status"] == "deficit"):
        lid = gap["location_id"]
        loc = loc_by_id.get(lid)
        if loc is None or loc["locationType"] not in _VALID_TYPES:
            uncovered.append(lid); continue
        rate = _RATES[loc["locationType"]]
        metro, op_l = loc["metro"], loc["op_hours"]
        parking, assigned_l = loc["parkingSpaces"], assigned_count.get(lid, 0)
        short, supply_l = gap["vehicles_short"], gap["supply_vehicles"]

        surplus = [g for g in gaps if g["status"] == "surplus" and g.get("vehicles_spare", 0) > 0
                   and loc_by_id.get(g["location_id"], {}).get("metro") == metro]
        if not surplus:
            counts["no_surplus_in_metro"] = counts.get("no_surplus_in_metro", 0) + 1
            uncovered.append(lid); continue

        def _dist(sg: dict) -> float:
            si = loc_by_id.get(sg["location_id"])
            if not si:
                return float("inf")
            return great_circle_miles(loc["latitude"], loc["longitude"],
                                      si["latitude"], si["longitude"]) * 1.35

        # horizon stats: one definition for move and gap items (location_outlook)
        try:
            outlook = location_outlook(lid, forecast=forecast, outcomes=outcomes_by_dest[lid],
                                       supply=supply_l, op_hours=op_l, deficit_above=d_above,
                                       max_utilization=max_util, horizon=hdays)
        except ValueError:
            counts["forecast_days_missing"] += 1
            uncovered.append(lid)
            continue
        fdd, fdd_iso = outlook["first_deficit_day"], outlook["forecast_week"]
        severity, confidence = outlook["severity"], outlook["confidence"]

        built = 0
        for sg in sorted(surplus, key=_dist)[:3]:
            sid = sg["location_id"]
            src = loc_by_id.get(sid)
            if src is None or src["locationType"] not in _VALID_TYPES:
                continue
            src_rate = _RATES[src["locationType"]]
            spare, supply_s, op_s = sg["vehicles_spare"], sg["supply_vehicles"], src["op_hours"]
            count = min(short, spare, max_veh)
            avail_p = parking - assigned_l
            if avail_p <= 0:
                counts["parking_full"] += 1; continue
            count = min(count, avail_p)
            if count <= 0:
                counts["parking_full"] += 1; continue
            elig = eligible_by_location.get(sid, [])[:2 * count]
            if len(elig) < count:
                count = len(elig)
            if count <= 0:
                continue
            dist = _dist(sg)
            tc_pv = compute_transfer_cost(distance_miles=dist)
            per_day: list[dict] = []
            miss = False
            for eff_d in eff_win:
                fl = fc_idx.get((lid, eff_d))
                fs = fc_idx.get((sid, eff_d))
                if fl is None or fs is None:
                    miss = True; break
                per_day.append({"date": eff_d,
                    "forecast_hours": fl["forecast_hours"], "supply_vehicles": supply_l, "op_hours": op_l,
                    # Tier 2 artifact contract: every forecast number names its method and
                    # the data's own date, so a stale forecast is narrated as stale.
                    "method": fl.get("method"), "mape_28d": fl.get("mape_28d"),
                    "run_date": fl.get("run_date"), "data_as_of": fl.get("data_as_of"),
                    "source_forecast_hours": fs["forecast_hours"], "source_supply_vehicles": supply_s,
                    "source_op_hours": op_s})
            if miss:
                counts["forecast_days_missing"] += 1; continue
            cand: dict[str, Any] = {
                "candidate_id": f"cand-{lid}-{sid}",
                "from_location_id": sid, "to_location_id": lid,
                "metro": metro, "count": count,
                "eligible_vins": [v["vin"] for v in elig],
                # D10 rebalance.vehicle evidence: the facts eligibility read, per VIN.
                "eligible_facts": {v["vin"]: {
                    "days_idle_last_7": v.get("days_idle_last_7"),
                    "days_to_next_service": v.get("days_to_next_service"),
                    "hours_to_next_booking": v.get("hours_to_next_booking"),
                    "open_lower_severity_items": [
                        i.get("severity") for i in (v.get("open_fi_items") or [])],
                } for v in elig},
                "vehicles_short": short, "vehicles_spare": spare, "parking_room": avail_p,
                "gaps_data_as_of": gap.get("data_as_of"),
                "distance_miles": round(dist, 2),
                "transfer_cost_per_vehicle": round(tc_pv, 2),
                "per_day": per_day, "effective_date": eff_win[0],
                "first_deficit_day": fdd, "forecast_week": fdd_iso,
                "severity": severity, "confidence": confidence,
                "max_utilization": max_util, "rate": rate, "source_rate": src_rate,
                "mean_request_hours": req_by_loc.get(lid),
            }
            try:
                money = money_for(cand, count)
            except ValueError:
                counts["forecast_days_missing"] += 1; continue
            if money["net_benefit_usd"] < min_net:
                counts["net_below_min"] += 1; continue
            cand.update(money)
            candidates.append(cand)
            counts["candidates_built"] += 1
            built += 1

        if not built:
            uncovered.append(lid)

    return {"candidates": candidates, "uncovered_deficits": uncovered, "counts": counts}
