"""Pure formula functions for FI fleet-rebalancing (spec G4-D4, G4-D6, G4-D9).

All functions are side-effect-free; no AWS calls.
"""
from __future__ import annotations

import math

from agents.fi_rebalancing import policy as rebal_policy

# ---------------------------------------------------------------------------
# Defaults (spec G4-D4)
# ---------------------------------------------------------------------------

DEFAULTS: dict = {
    "road_factor": 1.35,
    "urban_mph": 22,
    "handling_hours": 0.25,
    "driver_usd_per_hour": 30,
    "vehicle_usd_per_mile": 0.15,
    "RATES": {
        "airport": 3.50,
        "downtown": 3.00,
        "carshare_hub": 9.00,
    },
}

# Earth radius in miles (haversine)
_EARTH_RADIUS_MI = 3958.8


# ---------------------------------------------------------------------------
# Distance helper
# ---------------------------------------------------------------------------


def great_circle_miles(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Haversine great-circle distance in miles."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
    return 2 * _EARTH_RADIUS_MI * math.asin(math.sqrt(a))


# ---------------------------------------------------------------------------
# Transfer cost (spec G4-D4, contract § 7)
# ---------------------------------------------------------------------------


def compute_transfer_cost(
    *,
    distance_miles: float,
    urban_mph: float = DEFAULTS["urban_mph"],
    handling_hours: float = DEFAULTS["handling_hours"],
    driver_usd_per_hour: float = DEFAULTS["driver_usd_per_hour"],
    vehicle_usd_per_mile: float = DEFAULTS["vehicle_usd_per_mile"],
) -> float:
    """Transfer cost = (2×d/urban_mph + handling_h)×driver_$/h + d×veh_$/mi."""
    drive_hours = 2 * distance_miles / urban_mph
    labour = (drive_hours + handling_hours) * driver_usd_per_hour
    mileage = distance_miles * vehicle_usd_per_mile
    return labour + mileage


# ---------------------------------------------------------------------------
# Revenue gain per day (spec G4-D4, contract § 7)
# ---------------------------------------------------------------------------


def _unserved(forecast_hours: float, supply_vehicles: float, op_hours: float, max_util: float) -> float:
    return max(0.0, forecast_hours - supply_vehicles * op_hours * max_util)


def compute_revenue_gain_per_day(
    *,
    forecast_unserved_hours: float,
    moved_vehicles: int,
    vehicle_op_hours: float,
    max_utilization: float | None = None,
    usd_per_on_rent_hour: float,
) -> float:
    """Gain/day = min(forecast_unserved, moved_cap) × rate (contract § 7).

    *max_utilization* defaults to the run's policy value (U9; ``policy.current()``).
    """
    if max_utilization is None:
        max_utilization = rebal_policy.current()["max_utilization"]
    moved_cap = moved_vehicles * vehicle_op_hours * max_utilization
    hours_served = min(forecast_unserved_hours, moved_cap)
    return hours_served * usd_per_on_rent_hour


# ---------------------------------------------------------------------------
# Source loss per day (spec G4-D9)
# ---------------------------------------------------------------------------


def compute_source_loss_per_day(
    *,
    source_forecast_hours: float,
    source_supply_vehicles: int,
    moved_vehicles: int,
    vehicle_op_hours: float,
    max_utilization: float | None = None,
    usd_per_on_rent_hour: float,
) -> float:
    """Loss/day = [unserved(F, S−m) − unserved(F, S)] × rate (spec D9).

    *max_utilization* defaults to the run's policy value (U9; ``policy.current()``).
    """
    if max_utilization is None:
        max_utilization = rebal_policy.current()["max_utilization"]
    loss_before = _unserved(source_forecast_hours, source_supply_vehicles, vehicle_op_hours, max_utilization)
    loss_after = _unserved(source_forecast_hours, source_supply_vehicles - moved_vehicles, vehicle_op_hours, max_utilization)
    return max(0.0, loss_after - loss_before) * usd_per_on_rent_hour


# ---------------------------------------------------------------------------
# Revenue impact, net benefit, threshold (contract § 7)
# ---------------------------------------------------------------------------


def compute_revenue_impact(
    *,
    revenue_gain_per_day: float,
    source_loss_per_day: float,
    horizon_days: int,
) -> float:
    """Impact = (gain/day − loss/day) × horizon_days."""
    return (revenue_gain_per_day - source_loss_per_day) * horizon_days


def compute_net_benefit(*, revenue_impact: float, transfer_cost: float) -> float:
    """Net = revenue_impact − transfer_cost."""
    return revenue_impact - transfer_cost


def passes_net_benefit_threshold(*, net_benefit: float, min_net_benefit_usd: float) -> bool:
    """True when net_benefit >= min_net_benefit_usd (spec D8 step 2)."""
    return net_benefit >= min_net_benefit_usd


# ---------------------------------------------------------------------------
# Confidence band (spec G4-D6, contract § 7)
# ---------------------------------------------------------------------------


def compute_confidence_band(
    *,
    forecast_mape: float | None,
    recent_outcomes_ratio: float | None,
) -> str:
    """MAPE → initial band; downgrade one level if recent_outcomes_ratio < 0.5."""
    if forecast_mape is None or math.isnan(forecast_mape):
        band = "low"
    elif forecast_mape <= 0.15:
        band = "high"
    elif forecast_mape <= 0.30:
        band = "medium"
    else:
        band = "low"

    if recent_outcomes_ratio is not None and recent_outcomes_ratio < 0.5:
        if band == "high":
            band = "medium"
        elif band == "medium":
            band = "low"
        # low stays low

    return band


# ---------------------------------------------------------------------------
# Severity band (spec G4-D6)
# ---------------------------------------------------------------------------


def compute_severity_band(*, turned_away_share: float) -> str:
    """Severity from turned-away share; never returns 'p0' (spec D10)."""
    if turned_away_share >= 0.30:
        return "attention"
    if turned_away_share >= 0.15:
        return "soon"
    return "routine"
