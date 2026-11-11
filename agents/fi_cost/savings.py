"""
D7 saving-formula implementations for the fi_cost Tier 2 component.

Spec: .kiro/specs/2026-10-02-fi-cost-stage-b/spec.md § D7

The estimated saving is always a formula result — NEVER a model output.
The Bedrock tool schema has no numeric field so the model cannot inject a value;
this module is the only source of ``estimated_value_usd``.  This is a
deterministic seam (spec § Tier classification item 4).

All formulas round to cents (2 decimal places) to match the golden fixture and
to agree with CMS task 6.4's display tests (both repos pin fixtures/savings_golden.json).

Mutation targets (task 3.1 constraints):
  - swap any two terms in a formula; its golden-vector test must fail.
"""

from __future__ import annotations

from typing import Any


# ---------------------------------------------------------------------------
# Basis string templates — must match fixtures/savings_golden.json exactly
# ---------------------------------------------------------------------------

_BASIS_SHIFT_TO_DEPOT: str = (
    "monthly kWh moved to depot off-peak x price difference, {month}"
)
_BASIS_SCHEDULE_INSPECTION: str = (
    "maintenance above fleet-model median, {month}"
)
_BASIS_REASSIGN_OR_REMARKET: str = (
    "fixed cost not covered by fleet-median use, {month}"
)
_BASIS_DRIVER_COACHING: str = (
    "energy above fleet-model efficiency, {month}"
)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def estimate_saving(
    action: str,
    inputs: dict[str, Any],
) -> dict[str, Any] | None:
    """Compute ``estimated_value_usd`` and ``estimated_value_basis`` for a D7 action.

    Returns ``{"estimated_value_usd": float, "estimated_value_basis": str}``
    or ``None`` when ``action == "no_action"``.

    Formulas (spec D7):

    =====================  ======================================================
    shift_to_depot_off_peak  public_and_peak_kwh × (mean_public_peak_price −
                             depot_off_peak_price)
    schedule_inspection      vehicle_maintenance_cost −
                             cohort_baseline_maintenance_cost
    reassign_or_remarket     max(0, (lease + insurance) ×
                             (1 − miles / fleet_median_miles))
    driver_coaching          energy_cost × (1 − cohort_kwh_per_100mi /
                             vehicle_kwh_per_100mi)
    =====================  ======================================================

    Parameters
    ----------
    action:
        One of the four D7 action kinds or ``"no_action"``.
    inputs:
        Action-specific evidence values (see interface.md "inputs keys by action").

    Returns
    -------
    dict or None
        ``{"estimated_value_usd": float, "estimated_value_basis": str}`` or
        ``None`` for ``no_action``.

    Raises
    ------
    KeyError
        When a required input key is missing.
    ValueError
        When ``action`` is not recognised.
    """
    if action == "no_action":
        return None

    if action == "shift_to_depot_off_peak":
        return _shift_to_depot_off_peak(inputs)
    if action == "schedule_inspection":
        return _schedule_inspection(inputs)
    if action == "reassign_or_remarket":
        return _reassign_or_remarket(inputs)
    if action == "driver_coaching":
        return _driver_coaching(inputs)

    raise ValueError(
        f"Unknown action: {action!r}. "
        "Must be one of: shift_to_depot_off_peak, schedule_inspection, "
        "reassign_or_remarket, driver_coaching, no_action"
    )


# ---------------------------------------------------------------------------
# Formula implementations
# ---------------------------------------------------------------------------


def _shift_to_depot_off_peak(inputs: dict[str, Any]) -> dict[str, Any]:
    """D7: public_and_peak_kwh × (mean_public_peak_price − depot_off_peak_price)."""
    kwh: float = float(inputs["public_and_peak_kwh"])
    mean_price: float = float(inputs["mean_public_peak_price_per_kwh"])
    depot_price: float = float(inputs["depot_off_peak_price_per_kwh"])
    month: str = str(inputs["month"])

    # Mutation target: swapping terms (e.g. kwh * depot_price or price*price)
    # makes the golden test fail.
    saving = round(kwh * (mean_price - depot_price), 2)
    return {
        "estimated_value_usd": saving,
        "estimated_value_basis": _BASIS_SHIFT_TO_DEPOT.format(month=month),
    }


def _schedule_inspection(inputs: dict[str, Any]) -> dict[str, Any]:
    """D7: vehicle_maintenance_cost − cohort_baseline_maintenance_cost."""
    vehicle_cost: float = float(inputs["vehicle_maintenance_cost"])
    cohort_cost: float = float(inputs["cohort_baseline_maintenance_cost"])
    month: str = str(inputs["month"])

    # Mutation target: swap operands (cohort - vehicle) → negative value → test fails.
    saving = round(vehicle_cost - cohort_cost, 2)
    return {
        "estimated_value_usd": saving,
        "estimated_value_basis": _BASIS_SCHEDULE_INSPECTION.format(month=month),
    }


def _reassign_or_remarket(inputs: dict[str, Any]) -> dict[str, Any]:
    """D7: max(0, (lease + insurance) × (1 − miles / fleet_median_miles))."""
    lease: float = float(inputs["lease_cost"])
    insurance: float = float(inputs["insurance_cost"])
    miles: float = float(inputs["vehicle_miles"])
    fleet_median: float = float(inputs["fleet_median_miles"])
    month: str = str(inputs["month"])

    # Mutation target: removing max(0, ...) or swapping lease/insurance → test fails.
    utilisation_factor = 1.0 - miles / fleet_median
    saving = max(0.0, round((lease + insurance) * utilisation_factor, 2))
    return {
        "estimated_value_usd": saving,
        "estimated_value_basis": _BASIS_REASSIGN_OR_REMARKET.format(month=month),
    }


def _driver_coaching(inputs: dict[str, Any]) -> dict[str, Any]:
    """D7: energy_cost × (1 − cohort_kwh_per_100mi / vehicle_kwh_per_100mi)."""
    energy_cost: float = float(inputs["vehicle_energy_cost"])
    cohort_kwh: float = float(inputs["cohort_kwh_per_100mi"])
    vehicle_kwh: float = float(inputs["vehicle_kwh_per_100mi"])
    month: str = str(inputs["month"])

    # Mutation target: swapping cohort/vehicle → inverted efficiency → test fails.
    efficiency_improvement = 1.0 - cohort_kwh / vehicle_kwh
    saving = round(energy_cost * efficiency_improvement, 2)
    return {
        "estimated_value_usd": saving,
        "estimated_value_basis": _BASIS_DRIVER_COACHING.format(month=month),
    }
