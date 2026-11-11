"""Inputs-hash computation for FI rebalancing (spec G4-D8).

Pure function; no AWS calls.
"""
from __future__ import annotations

import hashlib
import json
import math


def _finite(v: object) -> object:
    """Return *v* if it is a finite number; return None for NaN or ±Inf.

    DynamoDB's TypeSerializer raises TypeError on non-finite floats (C4).
    Confidence still reads the raw mape_28d value before evidence is built.
    """
    if isinstance(v, float) and not math.isfinite(v):
        return None
    return v


def _canonicalise(obj: object) -> object:
    """Floor *_shortfall keys to int; sort *_vins list values; recurse."""
    if isinstance(obj, dict):
        result = {}
        for k, v in obj.items():
            if k.endswith("_shortfall") and isinstance(v, (int, float)):
                result[k] = int(math.floor(v))
            elif k.endswith("_vins") and isinstance(v, list):
                result[k] = sorted(_canonicalise(item) for item in v)
            else:
                result[k] = _canonicalise(v)
        return result
    if isinstance(obj, list):
        return [_canonicalise(item) for item in obj]
    return obj


def compute_inputs_hash(summary: dict) -> str:
    """SHA-256 of canonical JSON of *summary* (spec G4-D8)."""
    canonical = _canonicalise(summary)
    payload = json.dumps(canonical, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()



# ---------------------------------------------------------------------------
# Evidence builders (G4-D7)
# ---------------------------------------------------------------------------

_SOURCE = "agent-fi-rebalancing"


def _entry(
    kind: str,
    *,
    value: object,
    computed_at: str,
    source_ref: str,
    vin: str,
) -> dict:
    """Return a schema-valid evidence entry."""
    return {
        "kind": kind,
        "source": _SOURCE,
        "source_ref": source_ref,
        "value": value,
        "computed_at": computed_at,
        "provenance": "live",
        "vin": vin,
    }


def move_evidence(
    plan: dict,
    candidate: dict,
    *,
    computed_at: str,
    from_location: dict,
    to_location: dict,
    outcomes: list,
    events: list,
    vehicle_days: list | None = None,
    today: str = "",
) -> list:
    """Build evidence entries for a fleet.rebalance.move finding (G4-D7).

    Location-level entries carry the first VIN from plan['vehicle_vins'].
    Per-vehicle entries carry the vehicle's own VIN.

    W7: every source_ref names the table and its run/snapshot date.
    S4: next_service_in_days carries the window label ``next_service_in_days_next_3d``
        (the reader only reads today..today+3; ``None`` means nothing in that window,
        not "nothing ever scheduled").

    Parameters
    ----------
    vehicle_days:
        Rows returned by ``read_vehicle_days``; each has a ``snapshot_date`` field.
        Used to date the ``vehicle_days`` source_ref.  Pass ``None`` or ``[]`` to
        fall back to an undated ref.
    today:
        The run's logical date (``today_str`` in the agent), used to date the
        ``metro_events`` and ``fleet-locations`` source_refs.
    """
    vins: list[str] = plan.get("vehicle_vins") or []
    if not vins:
        raise ValueError("plan missing vehicle_vins")
    first_vin = vins[0]
    entries: list[dict] = []

    # rebalance.forecast — per effective/horizon day
    for d in candidate.get("per_day", []):
        entries.append(_entry(
            "rebalance.forecast",
            value={
                "date": d.get("date"),
                "forecast_hours": _finite(d.get("forecast_hours")),
                "supply_vehicles": _finite(d.get("supply_vehicles")),
                "op_hours": _finite(d.get("op_hours")),
                "method": d.get("method"),
                "mape_28d": _finite(d.get("mape_28d")),
                "run_date": d.get("run_date"),
                "data_as_of": d.get("data_as_of"),
            },
            computed_at=computed_at,
            source_ref=(f"adp.fleet_bookings_forecast run_date={d.get('run_date')} "
                        f"forecast_date={d.get('date')}"),
            vin=first_vin,
        ))

    # rebalance.supply — source and destination supply snapshot
    entries.append(_entry(
        "rebalance.supply",
        value={
            "from_location_id": plan["from_location_id"],
            "to_location_id": plan["to_location_id"],
            "vehicles_short": candidate.get("vehicles_short"),
            "vehicles_spare": candidate.get("vehicles_spare"),
            "eligible_vins": candidate.get("eligible_vins", []),
        },
        computed_at=computed_at,
        source_ref=f"adp.supply_demand_gaps_snapshot data_as_of={candidate.get('gaps_data_as_of')}",
        vin=first_vin,
    ))

    # rebalance.events — metro events in the forecast window
    entries.append(_entry(
        "rebalance.events",
        # Only events in this move's metro or naming one of its two locations.
        value=[
            {k: v for k, v in e.items() if k in (
                "event_id", "metro", "category", "uplift_band", "starts_on", "ends_on",
                "affected_location_ids",
            )}
            for e in events
            if e.get("metro") == candidate.get("metro")
            or {plan["from_location_id"], plan["to_location_id"]}
            & set(e.get("affected_location_ids") or [])
        ],
        computed_at=computed_at,
        source_ref=f"adp.metro_events data_as_of={today}" if today else "adp.metro_events",
        vin=first_vin,
    ))

    # rebalance.transfer — distance and cost
    entries.append(_entry(
        "rebalance.transfer",
        value={
            "distance_miles": candidate.get("distance_miles"),
            "road_factor": 1.35,
            "transfer_cost_per_vehicle": candidate.get("transfer_cost_per_vehicle"),
            "transfer_cost_usd": plan.get("transfer_cost_usd"),
            "vehicle_count": len(vins),
        },
        computed_at=computed_at,
        source_ref=f"cms.fleet-locations data_as_of={today}" if today else "cms.fleet-locations",
        vin=first_vin,
    ))

    # rebalance.revenue — financial impact
    _forecast_run_date = (candidate.get("per_day") or [{}])[0].get("run_date", "")
    entries.append(_entry(
        "rebalance.revenue",
        value={
            "revenue_gain_usd": plan.get("revenue_gain_usd"),
            "source_loss_usd": plan.get("source_loss_usd"),
            "revenue_impact_usd": plan.get("revenue_impact_usd"),
            "net_benefit_usd": plan.get("net_benefit_usd"),
            "predicted_on_rent_hours_gain": plan.get("predicted_on_rent_hours_gain"),
            "predicted_turnaways_avoided": plan.get("predicted_turnaways_avoided"),
        },
        computed_at=computed_at,
        source_ref=(f"adp.fleet_bookings_forecast run_date={_forecast_run_date}"
                    if _forecast_run_date else "adp.fleet_bookings_forecast"),
        vin=first_vin,
    ))

    # rebalance.vehicle — one per VIN
    facts = candidate.get("eligible_facts") or {}
    # W7: pick the snapshot_date from the vehicle_days row for the first VIN (or any VIN).
    _vd_by_vin: dict[str, dict] = {r["vin"]: r for r in (vehicle_days or [])}
    _vd_snap = (_vd_by_vin.get(vins[0]) or {}).get("snapshot_date", "") if vins else ""
    for vin in vins:
        if vin not in facts:
            # A VIN the eligibility pass never saw cannot be recommended.
            raise ValueError("plan names a vehicle with no eligibility facts")
        fact = facts[vin]
        entries.append(_entry(
            "rebalance.vehicle",
            value={
                "vin": vin,
                "days_idle_last_7": fact.get("days_idle_last_7"),
                # S4: label the window so None means "nothing in the next 3 days",
                # not "nothing ever scheduled" (the reader only reads today..today+3).
                "next_service_in_days_next_3d": (
                    None if fact.get("days_to_next_service") in (None, float("inf"))
                    else fact["days_to_next_service"]
                ),
                "open_lower_severity_items": fact.get("open_lower_severity_items") or [],
                "battery_health_band": None,  # not read in Group 4 (no source granted)
            },
            computed_at=computed_at,
            source_ref=(f"adp.vehicle_days snapshot_date={_vd_snap}"
                        if _vd_snap else "adp.vehicle_days"),
            vin=vin,
        ))

    # rebalance.recent_outcomes — last 3 moves into destination
    dest_id = plan.get("to_location_id", "")
    dest_outcomes = sorted(
        (o for o in outcomes if o.get("to_location_id") == dest_id),
        key=lambda o: o.get("effective_date", ""),
        reverse=True,
    )[:3]
    # W7: use materialised_on from the most recent outcome row, if present.
    _mat_on = dest_outcomes[0].get("materialised_on", "") if dest_outcomes else ""
    entries.append(_entry(
        "rebalance.recent_outcomes",
        value=[
            {k: v for k, v in o.items() if k in (
                "to_location_id", "effective_date", "ratio_hours",
                "predicted_on_rent_hours_gain", "actual_on_rent_hours_gain"
            )}
            for o in dest_outcomes
        ],
        computed_at=computed_at,
        source_ref=(f"adp.move_outcomes_snapshot materialised_on={_mat_on}"
                    if _mat_on else "adp.move_outcomes_snapshot"),
        vin=first_vin,
    ))

    # rebalance.diagnosis_source
    entries.append(_entry(
        "rebalance.diagnosis_source",
        value={"diagnosis_source": plan.get("diagnosis_source", "rules")},
        computed_at=computed_at,
        source_ref="agent-fi-rebalancing/run",
        vin=first_vin,
    ))

    return entries


def gap_evidence(
    gap_item: dict,
    *,
    computed_at: str,
    location: dict,
    assigned_vins: list,
) -> list:
    """Build evidence entries for a fleet.rebalance.gap finding (G4-D7).

    Uses the first assigned VIN (sorted) for all entries.
    Raises ValueError when no VINs are assigned at the location.
    """
    sorted_vins = sorted(assigned_vins) if assigned_vins else []
    if not sorted_vins:
        raise ValueError(
            f"gap location {gap_item.get('location_id')!r} has no assigned vehicle "
            "— cannot write gap Finding (G4-D7: gap_items_without_vehicle)"
        )
    first_vin = sorted_vins[0]
    entries: list[dict] = []

    # rebalance.supply
    entries.append(_entry(
        "rebalance.supply",
        value={
            "location_id": gap_item.get("location_id"),
            "vehicles_short": gap_item.get("vehicles_short"),
            "vehicles_spare": gap_item.get("vehicles_spare"),
            "assigned_vins": sorted_vins,
        },
        computed_at=computed_at,
        source_ref=f"adp.supply_demand_gaps_snapshot data_as_of={gap_item.get('gaps_data_as_of')}",
        vin=first_vin,
    ))

    # rebalance.forecast — per day if available
    for d in gap_item.get("per_day", []):
        entries.append(_entry(
            "rebalance.forecast",
            value={
                "date": d.get("date"),
                "forecast_hours": _finite(d.get("forecast_hours")),
                "supply_vehicles": _finite(d.get("supply_vehicles")),
                "op_hours": _finite(d.get("op_hours")),
                "method": d.get("method"),
                "mape_28d": _finite(d.get("mape_28d")),
                "run_date": d.get("run_date"),
                "data_as_of": d.get("data_as_of"),
            },
            computed_at=computed_at,
            source_ref=(f"adp.fleet_bookings_forecast run_date={d.get('run_date')} "
                        f"forecast_date={d.get('date')}"),
            vin=first_vin,
        ))

    # rebalance.diagnosis_source
    entries.append(_entry(
        "rebalance.diagnosis_source",
        value={"diagnosis_source": gap_item.get("diagnosis_source", "rules")},
        computed_at=computed_at,
        source_ref="agent-fi-rebalancing/run",
        vin=first_vin,
    ))

    return entries



def hold_evidence_entry(
    hold: dict,
    *,
    computed_at: str,
    vin: str,
) -> dict:
    """One ``rebalance.hold`` evidence entry (spec D10) for a model-chosen hold.

    The entry carries the hold ``reason`` (one of ``temporary_event``,
    ``returns_expected``, ``not_worth_cost``, ``unclear``) and the model's
    short free-text ``detail``.  No numeric field is included — a hold is
    informational, and the ``recommended_action`` on the containing gap item
    stays ``None`` (D10: gap items have no ``recommended_action``).

    The ``source_ref`` names the model agent run (not an Athena table), so a
    reader can tell at a glance that this entry came from the model path.
    """
    return _entry(
        "rebalance.hold",
        value={
            "reason": hold.get("reason"),
            "detail": hold.get("detail"),
        },
        computed_at=computed_at,
        source_ref="agent-fi-rebalancing/model.hold",
        vin=vin,
    )
