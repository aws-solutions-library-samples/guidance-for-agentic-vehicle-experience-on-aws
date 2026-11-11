"""
Energy-only cause restriction tests (task 3.2 — 2026-10-05 note).

Spec: .kiro/specs/2026-10-02-fi-cost-stage-b/spec.md § D6 step 5
Decisions: decisions.md 2026-10-05 item 6 (energy-only outlier: validator allows
           only charging_rate, charging_mix, efficiency, unclear; the rules fallback
           picks among those).

Tests:
  V1 — validator rejects repeat_repair for an energy-only outlier (mutation target:
       allow repeat_repair → test fails).
  V2 — validator rejects low_utilisation for an energy-only outlier.
  V3 — validator accepts charging_rate for an energy-only outlier.
  V4 — validator accepts efficiency for an energy-only outlier.
  V5 — validator accepts unclear for an energy-only outlier.
  F1 — fallback picks energy sub-cause (not repeat_repair) for energy-only outlier,
       even when maintenance delta is the largest (mutation target: allow repeat_repair
       → test fails).

Mutation target for V1 (per task 3.2 constraints):
  M_allow_repeat_repair: remove the energy-only restriction from validate_tool_input
  → V1 passes (accepts repeat_repair) instead of failing → this test fails.
Verified: see hand-back.
"""
from __future__ import annotations

import pytest

# ---------------------------------------------------------------------------
# Shared evidence — energy-only outlier (cost.flag == "energy")
# Charging-rate numbers: mean price above cohort, dc_fast_share above cohort.
# Maintenance max_repeat_count = 2 to make repeat_repair consistent if the
# energy-only restriction were removed (mutation-test anchor).
# ---------------------------------------------------------------------------

_ENERGY_ONLY_EVIDENCE = {
    "vin": "MRD-EN-RESTR-001",
    "fleet_id": "fleet-meridian-energy",
    "year_month": "2026-09",
    "cost.month": "2026-09",
    # Energy-only outlier: is_energy_outlier=True, is_outlier=False
    "cost.flag": "energy",
    "ratio_cohort": 1.12,      # below total-cost threshold (1.30); energy rule fired
    "ratio_own": 1.08,
    "severity": "routine",
    # Charging-rate evidence (for accept tests)
    "energy.mean_price_per_kwh": 0.35,
    "energy.cohort_mean_price_per_kwh": 0.15,   # price above cohort
    "energy.public_share": 0.20,
    "energy.cohort_public_share": 0.30,          # public share BELOW cohort
    "energy.peak_share": 0.40,
    "energy.cohort_peak_share": 0.15,            # peak share above cohort
    "energy.dc_fast_share": 0.60,
    "energy.cohort_dc_fast_share": 0.25,         # dc_fast above cohort
    # Efficiency evidence
    "energy.vehicle_kwh_per_100mi": 35.0,
    "energy.cohort_kwh_per_100mi": 25.0,         # vehicle kWh above cohort
    # charging_mix: below cohort (to keep charging_rate tests clean)
    "energy.cohort_public_share": 0.30,
    # Maintenance
    "cost.maintenance": 200.0,
    "maintenance.max_repeat_count": 2,           # ≥2 → repeat_repair valid by count,
                                                  # but energy-only restriction forbids it
    "delta.maintenance_usd": 48.0,
    # Utilisation (miles below median → low_utilisation consistent, but forbidden)
    "cost.miles": 300.0,
    "cost.fleet_median_miles": 1000.0,
    # Other required evidence
    "cost.lease": 600.0,
    "cost.insurance": 150.0,
    "cost.fleet_median_miles": 1000.0,
    "cost.energy": 280.0,
    "cost.cohort_baseline": 0.28,
    "energy.public_and_peak_kwh": 400.0,
    "energy.mean_public_peak_price_per_kwh": 0.38,
    "energy.depot_off_peak_price_per_kwh": 0.09,
    "delta.energy": 0.375,
    "delta.energy_usd": 300.0,          # energy delta is largest
    "delta.lease": 0.025,
    "delta.lease_usd": 20.0,
    "delta.insurance": 0.0125,
    "delta.insurance_usd": 10.0,
    "delta.maintenance": 0.06,
}

# ---------------------------------------------------------------------------
# V1 — validator rejects repeat_repair for energy-only outlier
#
# Mutation target: remove the energy-only restriction from validate_tool_input
# (the check `if evidence.get("cost.flag") == "energy" and cause not in
# _ENERGY_ONLY_CAUSES`).  Without the restriction, repeat_repair is accepted
# (max_repeat_count=2 and maintenance above cohort).  This test then fails
# because assert not is_valid would fail (is_valid would be True).
# ---------------------------------------------------------------------------


def test_energy_only_outlier_rejects_repeat_repair():
    """Validator rejects repeat_repair for an energy-only outlier (cost.flag='energy').

    Decisions 2026-10-05 item 6: an energy-only outlier may only claim
    charging_rate, charging_mix, efficiency, or unclear.

    Mutation M_allow_repeat_repair (remove energy-only restriction):
      With max_repeat_count=2 and maintenance above cohort, repeat_repair is
      consistent by the normal cause check.  Without the restriction, the validator
      accepts it → is_valid=True → this assertion (assert not is_valid) fails.
    """
    from agents.fi_cost.validate import validate_tool_input

    reply = {
        "cause": "repeat_repair",
        "action": "schedule_inspection",   # allowed for repeat_repair
        "evidence_refs": [],
        "headline": "Maintenance above cohort",
        "detail": "Multiple repairs detected.",
        "confidence": "high",
    }
    is_valid, error = validate_tool_input(reply, _ENERGY_ONLY_EVIDENCE)
    assert not is_valid, (
        "repeat_repair must be rejected for an energy-only outlier (cost.flag='energy'). "
        "Decisions 2026-10-05 item 6: allowed causes are charging_rate, charging_mix, "
        "efficiency, unclear. "
        "Mutation M_allow_repeat_repair (remove the restriction) makes is_valid=True "
        "and fails this assertion."
    )
    assert "energy" in error.lower() or "cause" in error.lower(), (
        f"Error message should reference the energy-only restriction; got: {error!r}"
    )


# ---------------------------------------------------------------------------
# V2 — validator rejects low_utilisation for energy-only outlier
# ---------------------------------------------------------------------------


def test_energy_only_outlier_rejects_low_utilisation():
    """Validator rejects low_utilisation for an energy-only outlier (cost.flag='energy').

    miles=300 < fleet_median=1000 → low_utilisation is consistent, but the
    energy-only restriction forbids it.
    """
    from agents.fi_cost.validate import validate_tool_input

    reply = {
        "cause": "low_utilisation",
        "action": "reassign_or_remarket",
        "evidence_refs": [],
        "headline": "Low mileage vehicle",
        "detail": "Fixed costs spread over fewer miles than average.",
        "confidence": "medium",
    }
    is_valid, error = validate_tool_input(reply, _ENERGY_ONLY_EVIDENCE)
    assert not is_valid, (
        "low_utilisation must be rejected for an energy-only outlier; "
        "only charging_rate, charging_mix, efficiency, unclear are allowed."
    )


# ---------------------------------------------------------------------------
# V3 — validator accepts charging_rate for energy-only outlier
# ---------------------------------------------------------------------------


def test_energy_only_outlier_accepts_charging_rate():
    """Validator accepts charging_rate for an energy-only outlier when numbers support it."""
    from agents.fi_cost.validate import validate_tool_input

    reply = {
        "cause": "charging_rate",
        "action": "shift_to_depot_off_peak",
        "evidence_refs": ["energy.mean_price_per_kwh", "energy.dc_fast_share"],
        "headline": "High public DC-fast charging costs",
        "detail": "Charging above fleet mean price; shift to depot off-peak.",
        "confidence": "high",
    }
    is_valid, error = validate_tool_input(reply, _ENERGY_ONLY_EVIDENCE)
    assert is_valid, (
        f"charging_rate must be accepted for an energy-only outlier when numbers support "
        f"it (mean price above cohort, dc_fast_share above cohort); got error: {error!r}"
    )


# ---------------------------------------------------------------------------
# V4 — validator accepts efficiency for energy-only outlier
# ---------------------------------------------------------------------------


def test_energy_only_outlier_accepts_efficiency():
    """Validator accepts efficiency for an energy-only outlier when kWh/100mi above cohort."""
    from agents.fi_cost.validate import validate_tool_input

    reply = {
        "cause": "efficiency",
        "action": "driver_coaching",
        "evidence_refs": ["energy.vehicle_kwh_per_100mi"],
        "headline": "Above-average energy consumption",
        "detail": "Vehicle uses more kWh per mile than the fleet model median.",
        "confidence": "high",
    }
    is_valid, error = validate_tool_input(reply, _ENERGY_ONLY_EVIDENCE)
    assert is_valid, (
        f"efficiency must be accepted for an energy-only outlier when kWh/100mi > cohort; "
        f"got error: {error!r}"
    )


# ---------------------------------------------------------------------------
# V5 — validator accepts unclear for energy-only outlier
# ---------------------------------------------------------------------------


def test_energy_only_outlier_accepts_unclear():
    """Validator accepts unclear/no_action for an energy-only outlier."""
    from agents.fi_cost.validate import validate_tool_input

    reply = {
        "cause": "unclear",
        "action": "no_action",
        "evidence_refs": [],
        "headline": "Energy cost elevated; cause unclear",
        "detail": "Review the evidence for this vehicle.",
        "confidence": "medium",
    }
    is_valid, error = validate_tool_input(reply, _ENERGY_ONLY_EVIDENCE)
    assert is_valid, (
        f"unclear/no_action must be accepted for an energy-only outlier; "
        f"got error: {error!r}"
    )


# ---------------------------------------------------------------------------
# F1 — fallback picks energy sub-cause for energy-only outlier
#
# Even when maintenance dollar delta is the largest, the fallback must
# pick from energy sub-causes because cost.flag == "energy".
#
# Mutation M_allow_repeat_repair in fallback: remove the energy-only restriction
# (the `if is_energy_only: ... return _energy_sub_cause(evidence)` branch).
# Without it, with delta.maintenance_usd=600 > delta.energy_usd=30 and
# max_repeat_count=2, the fallback would pick repeat_repair instead of an
# energy sub-cause → this assertion fails.
# ---------------------------------------------------------------------------


def test_energy_only_fallback_uses_energy_cause_regardless_of_maintenance_delta():
    """Fallback picks energy sub-cause for energy-only outlier even when maintenance delta is largest.

    Decisions 2026-10-05 item 6: "the rules fallback picks among those [energy causes]."
    Even when delta.maintenance_usd is the largest dollar delta, the fallback must
    ignore that and pick from charging_rate / charging_mix / efficiency / unclear.

    Mutation M_allow_repeat_repair in fallback (remove is_energy_only branch):
      With max_repeat_count=2 and maintenance the largest delta, the general algorithm
      picks repeat_repair.  This test asserts the cause is NOT repeat_repair →
      mutation M_allow_repeat_repair makes it repeat_repair → assertion fails.
    """
    from agents.fi_cost.fallback import rules_fallback

    # Evidence where maintenance delta dominates AND max_repeat_count=2,
    # but cost.flag="energy" → fallback must use energy sub-cause.
    evidence = dict(
        _ENERGY_ONLY_EVIDENCE,
        **{
            "cost.flag": "energy",
            "delta.energy_usd": 30.0,
            "delta.maintenance_usd": 600.0,   # larger than energy, but must be ignored
            "delta.lease_usd": 20.0,
            "delta.insurance_usd": 10.0,
            "maintenance.max_repeat_count": 2,
            # Energy sub-cause signals (efficiency wins: 40/25-1 = 0.60)
            "energy.mean_price_per_kwh": 0.16,           # price: 0.16/0.15-1 = 0.067
            "energy.cohort_mean_price_per_kwh": 0.15,
            "energy.public_share": 0.31,                 # public: 0.31/0.30-1 = 0.033
            "energy.cohort_public_share": 0.30,
            "energy.vehicle_kwh_per_100mi": 40.0,        # efficiency: 40/25-1 = 0.60
            "energy.cohort_kwh_per_100mi": 25.0,
        },
    )
    result = rules_fallback(evidence)

    assert result["cause"] in ("charging_rate", "charging_mix", "efficiency", "unclear"), (
        f"For energy-only outlier, fallback must pick from energy causes; "
        f"got cause={result['cause']!r}. "
        "Mutation M_allow_repeat_repair (remove energy-only branch) returns "
        "'repeat_repair' and fails this assertion."
    )
    assert result["cause"] != "repeat_repair", (
        "repeat_repair must not be chosen by the fallback for an energy-only outlier; "
        "M_allow_repeat_repair is caught here."
    )
    # With the efficiency signal highest (0.60), the fallback should pick efficiency.
    assert result["cause"] == "efficiency", (
        f"With kWh/100mi signal (0.60) highest among energy sub-causes, "
        f"fallback must pick efficiency; got {result['cause']!r}."
    )
    assert result["action"] == "driver_coaching", (
        "First allowed action for efficiency is driver_coaching."
    )
