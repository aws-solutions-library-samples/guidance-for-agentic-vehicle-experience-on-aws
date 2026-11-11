"""
Rules-based fallback for the fi_cost Tier 2 component — D6 step 5 fallback path.

Spec: .kiro/specs/2026-10-02-fi-cost-stage-b/spec.md § D6 step 5, § D9
Decisions: decisions.md 2026-10-04 "Architect calls from Group 1 review cycle 1"
           item 1 (cause→action table; fallback cause-selection algorithm).
           decisions.md 2026-10-05 item 6 (energy-only outlier: fallback picks
           among charging_rate, charging_mix, efficiency, unclear).

``rules_fallback`` is called after validation fails twice (once original + one retry)
or after Bedrock Guardrails blocks the model's output twice.  It never depends on
the model being available: a run always produces a Finding.

Fallback cause-selection algorithm (decisions item 1):
  1. Identify the category with the largest positive dollar delta
     (delta.energy_usd, delta.maintenance_usd, delta.lease_usd, delta.insurance_usd).
  2. For energy: pick the energy sub-cause with the highest relative signal
     (each signal = vehicle_metric / cohort_metric − 1):
       charging_rate: energy.mean_price_per_kwh / energy.cohort_mean_price_per_kwh − 1
       charging_mix:  energy.public_share / energy.cohort_public_share − 1
       efficiency:    energy.vehicle_kwh_per_100mi / energy.cohort_kwh_per_100mi − 1
     The highest positive signal wins; if all are ≤ 0, the sub-cause is unclear.
  3. For maintenance: repeat_repair if maintenance.max_repeat_count >= 2, else unclear.
  4. For lease/insurance: low_utilisation if cost.miles < cost.fleet_median_miles,
     else unclear.
  5. No positive delta → unclear.

Energy-only outliers (cost.flag == "energy", decisions 2026-10-05 item 6):
  Regardless of which dollar delta is largest, the fallback restricts the cause to
  the energy sub-cause (or unclear).  Mutation target: allowing repeat_repair for
  energy-only outliers fails test_energy_only_fallback_uses_energy_cause.

The returned dict has the same shape as a model reply so that ``run_cost_diagnosis``
can process it without branching:
  {
    "cause": str,
    "action": str,
    "evidence_refs": [],
    "headline": str,
    "detail": str,
    "confidence": "low",
    "evidence": [{"kind": "cost.diagnosis_source", "value": "rules"}],
  }
"""

from __future__ import annotations

import logging
from typing import Any

from agents.fi_cost.validate import _CAUSE_ACTIONS, _ENERGY_ONLY_CAUSES  # noqa: F401

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Fixed narration (no model; brief operator text)
# ---------------------------------------------------------------------------

_FALLBACK_HEADLINE = "Automated cost review: manual follow-up recommended"
_FALLBACK_DETAIL = (
    "The automated diagnosis could not determine a specific cause with confidence. "
    "Please review the cost breakdown and evidence for this vehicle."
)

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def rules_fallback(evidence: dict[str, Any]) -> dict[str, Any]:
    """Deterministic fallback when validation fails after one retry (D6 step 5).

    Parameters
    ----------
    evidence:
        Flat evidence dict for the vehicle.

    Returns
    -------
    dict
        A partial reply dict (same shape as a model reply, not a Finding).
        ``run_cost_diagnosis`` converts it to a Finding with
        ``recommended_action.parameters.cause`` set from this dict's ``cause`` field.

    Rules (decisions item 1):
      - cause = category with the largest positive ``delta.*_usd`` value;
        energy ties broken by whichever of charging_rate/charging_mix/efficiency
        has the largest relative signal; no positive delta → "unclear".
      - action = first allowed action for that cause (per the cause→action table).
      - confidence = "low".
      - evidence entry: {kind: "cost.diagnosis_source", value: "rules"}.
    """
    is_energy_only = evidence.get("cost.flag") == "energy"

    if is_energy_only:
        # Energy-only outlier: always pick from energy sub-causes regardless of
        # which dollar delta is largest.  Decisions 2026-10-05 item 6.
        # Mutation target: allowing non-energy causes here fails
        # test_energy_only_fallback_uses_energy_cause.
        cause = _energy_sub_cause(evidence)
    else:
        # General case: pick by largest positive dollar delta category.
        cause = _select_cause_by_delta(evidence)

    action = _first_action_for(cause)

    # C2: if the best action requires a depot off-peak price but none exists for this
    # vehicle or its cohort, fall back to no_action (informational Finding per FIX1_CALLS).
    # This prevents a TypeError when _inputs_for tries float(None).
    if action == "shift_to_depot_off_peak":
        depot_price = evidence.get("energy.depot_off_peak_price_per_kwh")
        if depot_price is None:
            log.debug(
                "fi_cost.fallback: no depot off-peak price for vin=%s; using no_action",
                evidence.get("vin", "unknown"),
            )
            action = "no_action"

    return {
        "cause": cause,
        "action": action,
        "evidence_refs": [],
        "headline": _FALLBACK_HEADLINE,
        "detail": _FALLBACK_DETAIL,
        "confidence": "low",
        "evidence": [{"kind": "cost.diagnosis_source", "value": "rules"}],
    }


# ---------------------------------------------------------------------------
# Internal: cause selection
# ---------------------------------------------------------------------------


def _select_cause_by_delta(evidence: dict[str, Any]) -> str:
    """Pick cause by the largest positive dollar delta category.

    Returns one of: "charging_rate", "charging_mix", "efficiency",
    "repeat_repair", "low_utilisation", "unclear".
    """
    energy_usd = _float(evidence.get("delta.energy_usd", 0.0))
    maint_usd = _float(evidence.get("delta.maintenance_usd", 0.0))
    lease_usd = _float(evidence.get("delta.lease_usd", 0.0))
    insurance_usd = _float(evidence.get("delta.insurance_usd", 0.0))

    # Among positive deltas find the maximum category
    positive_deltas = {
        "energy": energy_usd if energy_usd > 0 else None,
        "maintenance": maint_usd if maint_usd > 0 else None,
        "lease": lease_usd if lease_usd > 0 else None,
        "insurance": insurance_usd if insurance_usd > 0 else None,
    }
    # Combine lease and insurance into one "fixed_costs" bucket
    fixed_cost = max(
        lease_usd if lease_usd > 0 else 0.0,
        insurance_usd if insurance_usd > 0 else 0.0,
    )

    candidates = {
        "energy": energy_usd if energy_usd > 0 else 0.0,
        "maintenance": maint_usd if maint_usd > 0 else 0.0,
        "fixed_costs": fixed_cost,
    }

    if all(v <= 0.0 for v in [energy_usd, maint_usd, lease_usd, insurance_usd]):
        return "unclear"

    # Determine the category with the largest positive delta
    # (lease and insurance are lumped as fixed_costs)
    best_cat = max(candidates, key=lambda k: candidates[k])

    if best_cat == "energy":
        return _energy_sub_cause(evidence)

    if best_cat == "maintenance":
        count = int(evidence.get("maintenance.max_repeat_count", 0))
        if count >= 2:
            return "repeat_repair"
        return "unclear"

    # fixed_costs (lease or insurance)
    miles = _float(evidence.get("cost.miles", 0.0))
    fleet_median = _float(evidence.get("cost.fleet_median_miles", 0.0))
    if miles < fleet_median:
        return "low_utilisation"
    return "unclear"


def _energy_sub_cause(evidence: dict[str, Any]) -> str:
    """Pick the energy sub-cause with the highest relative signal above the cohort.

    Relative signals:
      charging_rate: mean_price / cohort_mean_price − 1
      charging_mix:  public_share / cohort_public_share − 1
      efficiency:    vehicle_kwh / cohort_kwh − 1

    The sub-cause with the highest positive signal wins.
    If all are <= 0, returns "unclear".

    This function is also used directly for energy-only outliers (regardless of
    which dollar delta is largest), per decisions 2026-10-05 item 6.
    """
    def _relative_signal(vehicle: float, cohort: float) -> float:
        if cohort <= 0:
            return 0.0
        return vehicle / cohort - 1.0

    price_signal = _relative_signal(
        _float(evidence.get("energy.mean_price_per_kwh", 0.0)),
        _float(evidence.get("energy.cohort_mean_price_per_kwh", 1.0)),
    )
    public_signal = _relative_signal(
        _float(evidence.get("energy.public_share", 0.0)),
        _float(evidence.get("energy.cohort_public_share", 1.0)),
    )
    kwh_signal = _relative_signal(
        _float(evidence.get("energy.vehicle_kwh_per_100mi", 0.0)),
        _float(evidence.get("energy.cohort_kwh_per_100mi", 1.0)),
    )

    sub_signals = {
        "charging_rate": price_signal,
        "charging_mix": public_signal,
        "efficiency": kwh_signal,
    }

    best = max(sub_signals, key=lambda k: sub_signals[k])
    if sub_signals[best] <= 0.0:
        return "unclear"
    return best


def _first_action_for(cause: str) -> str:
    """Return the first (rules-fallback preferred) action for a cause."""
    actions = _CAUSE_ACTIONS.get(cause, ("no_action",))
    return actions[0]


def _float(value: Any) -> float:
    if value is None:
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0
