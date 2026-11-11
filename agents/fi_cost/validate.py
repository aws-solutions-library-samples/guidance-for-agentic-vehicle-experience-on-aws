"""
Validator for the record_diagnosis tool input — D6 step 5.

Spec: .kiro/specs/2026-10-02-fi-cost-stage-b/spec.md § D6 step 5, § D9
Decisions: decisions.md 2026-10-04 "Architect calls from Group 1 review cycle 1"
           item 1 (cause → action table and consistency checks).
           decisions.md 2026-10-05 item 6 (energy-only outlier: cause restricted to
           charging_rate, charging_mix, efficiency, unclear).

``validate_tool_input`` runs five checks in order:
  1. ``cause`` is in VALID_CAUSES.
  2. For energy-only outliers (``cost.flag == "energy"``), cause is in
     _ENERGY_ONLY_CAUSES.  Mutation target: allowing ``repeat_repair`` here fails
     ``test_energy_only_cause_restriction`` (new test in
     test_energy_cause_restriction.py).
  3. ``action`` is in VALID_ACTIONS.
  4. (cause, action) pair is in the allowed table (decisions item 1).
  5. Every ``evidence_refs`` key exists in the evidence bundle.
  6. Numbers in evidence are consistent with the claimed cause (decisions item 1).

Returns ``(True, "")`` on success or ``(False, error_message)`` on the first failure.
The caller retries once on failure, then falls back (agent.py).
"""

from __future__ import annotations

from typing import Any


# ---------------------------------------------------------------------------
# Public constants
# ---------------------------------------------------------------------------

#: Closed set of valid cause values (D6 step 4).
VALID_CAUSES: frozenset[str] = frozenset({
    "charging_rate",
    "charging_mix",
    "repeat_repair",
    "low_utilisation",
    "efficiency",
    "unclear",
})

#: Closed set of valid action values (D9).
VALID_ACTIONS: frozenset[str] = frozenset({
    "shift_to_depot_off_peak",
    "schedule_inspection",
    "reassign_or_remarket",
    "driver_coaching",
    "no_action",
})

# Causes allowed for energy-only outliers (cost.flag == "energy").
# Decisions 2026-10-05 item 6: an energy-only outlier may only claim these causes.
# Mutation target: allowing "repeat_repair" here fails test_energy_only_cause_restriction.
_ENERGY_ONLY_CAUSES: frozenset[str] = frozenset({
    "charging_rate",
    "charging_mix",
    "efficiency",
    "unclear",
})

# Cause → allowed actions (first is the rules-fallback choice).
# Decisions 2026-10-04 "Architect calls from Group 1 review cycle 1" item 1.
_CAUSE_ACTIONS: dict[str, tuple[str, ...]] = {
    "charging_rate":   ("shift_to_depot_off_peak", "no_action"),
    "charging_mix":    ("shift_to_depot_off_peak", "no_action"),
    "repeat_repair":   ("schedule_inspection", "reassign_or_remarket", "no_action"),
    "low_utilisation": ("reassign_or_remarket", "no_action"),
    "efficiency":      ("driver_coaching", "schedule_inspection", "no_action"),
    "unclear":         ("no_action",),
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def validate_tool_input(
    reply: dict[str, Any],
    evidence: dict[str, Any],
) -> tuple[bool, str]:
    """Validate record_diagnosis tool input against the schema and evidence.

    Parameters
    ----------
    reply:
        The ``input`` dict extracted from the Bedrock Converse ``toolUse`` block.
    evidence:
        The flat evidence dict for this vehicle (one per VIN from
        ``gather_evidence``).

    Returns
    -------
    tuple[bool, str]
        ``(True, "")`` on success.
        ``(False, error_message)`` on the first check that fails.

    Checks (in order):
      1. ``cause`` in VALID_CAUSES.
      2. Energy-only restriction: if ``cost.flag == "energy"`` cause must be in
         _ENERGY_ONLY_CAUSES.
      3. ``action`` in VALID_ACTIONS.
      4. (cause, action) pair allowed by the cause → action table.
      5. Every ``evidence_refs`` key exists in the evidence bundle.
      6. Numbers are consistent with the cause (charging_rate, charging_mix,
         repeat_repair, low_utilisation, efficiency).
    """
    # 0 — JSON types first, so a malformed reply is a failed attempt, never a TypeError
    #     (review cycle 2 Critical): a dict; cause/action/headline/detail/confidence
    #     strings; evidence_refs a list of strings.
    if not isinstance(reply, dict):
        return False, f"reply must be an object, got {type(reply).__name__!r}"
    for _key in ("cause", "action", "headline", "detail", "confidence"):
        if _key in reply and not isinstance(reply[_key], str):
            return False, f"{_key} must be a string, got {type(reply[_key]).__name__!r}"
    _refs = reply.get("evidence_refs", [])
    if not isinstance(_refs, list) or not all(isinstance(r, str) for r in _refs):
        return False, "evidence_refs must be a list of strings"

    cause = reply.get("cause", "")
    action = reply.get("action", "")
    evidence_refs = reply.get("evidence_refs", [])

    # 0a — no extra keys beyond the six D6 fields (C1: model cannot inject fields)
    _ALLOWED_REPLY_KEYS: frozenset[str] = frozenset({
        "cause", "action", "evidence_refs", "headline", "detail", "confidence",
    })
    extra_keys = set(reply.keys()) - _ALLOWED_REPLY_KEYS
    if extra_keys:
        return False, (
            f"reply contains extra keys not in the record_diagnosis schema: "
            f"{sorted(extra_keys)!r}. Only {sorted(_ALLOWED_REPLY_KEYS)!r} are allowed."
        )

    # 0b — confidence must be "medium" or "high" ("low" is reserved for the rules fallback)
    confidence = reply.get("confidence", "")
    if confidence not in ("medium", "high"):
        return False, (
            f"confidence {confidence!r} is not allowed; the model must use 'medium' or 'high'. "
            f"'low' is reserved for the rules fallback (D6 step 5)."
        )

    # 0c/0d — headline and detail must be strings within length limits
    headline = reply.get("headline", "")
    if not isinstance(headline, str):
        return False, f"headline must be a string, got {type(headline).__name__!r}"
    if not headline.strip() or not str(reply.get("detail", "")).strip():
        return False, "headline and detail are required (the operator-facing narration)"
    if len(headline) > 120:
        return False, (
            f"headline is {len(headline)} characters; maximum is 120 (frozen schema cap)"
        )
    detail = reply.get("detail", "")
    if not isinstance(detail, str):
        return False, f"detail must be a string, got {type(detail).__name__!r}"
    if len(detail) > 600:
        return False, (
            f"detail is {len(detail)} characters; maximum is 600 (D6 step 4)"
        )

    # 1 — cause must be in the closed list
    if cause not in VALID_CAUSES:
        return False, (
            f"cause {cause!r} is not in the allowed set "
            f"{sorted(VALID_CAUSES)!r}"
        )

    # 2 — energy-only restriction (decisions 2026-10-05 item 6)
    if evidence.get("cost.flag") == "energy" and cause not in _ENERGY_ONLY_CAUSES:
        return False, (
            f"cause {cause!r} is not allowed for an energy-only outlier "
            f"(cost.flag='energy'); allowed: {sorted(_ENERGY_ONLY_CAUSES)!r}"
        )

    # 3 — action must be in the closed list
    if action not in VALID_ACTIONS:
        return False, (
            f"action {action!r} is not in the allowed set "
            f"{sorted(VALID_ACTIONS)!r}"
        )

    # 4 — (cause, action) pair must be in the table
    allowed_actions = _CAUSE_ACTIONS.get(cause, ())
    if action not in allowed_actions:
        return False, (
            f"action {action!r} is not allowed for cause {cause!r}; "
            f"allowed: {list(allowed_actions)!r} (decisions item 1)"
        )

    # 5 — every evidence_refs key must exist in the evidence bundle
    for ref in evidence_refs:
        if ref not in evidence:
            return False, (
                f"evidence_refs key {ref!r} does not exist in the evidence bundle"
            )

    # 6 — numbers must support the claimed cause (decisions item 1)
    ok, msg = _check_cause_consistency(cause, evidence)
    if not ok:
        return False, msg

    # 7 — action-level formula-input checks (C2/W1: prevent TypeError or negative savings)

    # shift_to_depot_off_peak requires a depot off-peak price (vehicle's or cohort's)
    # C2: if both are None, the formula crashes with TypeError.
    if action == "shift_to_depot_off_peak":
        depot_price = evidence.get("energy.depot_off_peak_price_per_kwh")
        if depot_price is None:
            return False, (
                "action 'shift_to_depot_off_peak' requires a non-null depot off-peak "
                "price (energy.depot_off_peak_price_per_kwh). No off-peak charging data "
                "exists for this vehicle or its cohort; choose a different action."
            )

    # schedule_inspection requires maintenance above the cohort (delta > 0) — W1.
    # A negative or zero delta means the vehicle's maintenance is at or below cohort:
    # estimated_value_usd would be <= 0, violating the schema's minimum: 0 constraint.
    if action == "schedule_inspection":
        delta_maint = _float_or_zero(evidence.get("delta.maintenance_usd"))
        if delta_maint <= 0:
            return False, (
                "action 'schedule_inspection' requires delta.maintenance_usd > 0 "
                f"(maintenance must be above the cohort median), but delta={delta_maint}. "
                "The formula would produce a non-positive estimated_value_usd (W1)."
            )

    return True, ""


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _check_cause_consistency(
    cause: str,
    evidence: dict[str, Any],
) -> tuple[bool, str]:
    """Return (True, "") when the evidence numbers support the claimed cause.

    Decisions 2026-10-04 item 1 defines which numbers must hold per cause:

    charging_rate:
        mean price per kWh above cohort AND
        (depot peak share OR public DC-fast share above cohort)

    charging_mix:
        public share of kWh above cohort

    repeat_repair:
        maintenance.max_repeat_count >= 2
        (maintenance above cohort is implicit in the outlier flag)

    low_utilisation:
        cost.miles < cost.fleet_median_miles

    efficiency:
        energy.vehicle_kwh_per_100mi > energy.cohort_kwh_per_100mi

    unclear:
        no consistency check (any evidence is acceptable)
    """
    if cause == "charging_rate":
        return _check_charging_rate(evidence)
    if cause == "charging_mix":
        return _check_charging_mix(evidence)
    if cause == "repeat_repair":
        return _check_repeat_repair(evidence)
    if cause == "low_utilisation":
        return _check_low_utilisation(evidence)
    if cause == "efficiency":
        return _check_efficiency(evidence)
    # unclear: no consistency check
    return True, ""


def _check_charging_rate(evidence: dict[str, Any]) -> tuple[bool, str]:
    """charging_rate: mean price above cohort AND (peak_share OR dc_fast_share above cohort).

    Decisions item 1: the first condition is mean price per kWh above the cohort's.
    The second condition is either depot peak share OR public DC-fast share above cohort.
    Note: public share (charging_mix's signal) is NOT part of charging_rate's check.
    """
    mean_price = _float_or_zero(evidence.get("energy.mean_price_per_kwh"))
    cohort_mean_price = _float_or_zero(evidence.get("energy.cohort_mean_price_per_kwh"))

    if mean_price <= cohort_mean_price:
        return False, (
            "cause 'charging_rate' requires energy.mean_price_per_kwh > "
            "energy.cohort_mean_price_per_kwh, but "
            f"vehicle={mean_price} <= cohort={cohort_mean_price}"
        )

    peak_share = _float_or_zero(evidence.get("energy.peak_share"))
    cohort_peak_share = _float_or_zero(evidence.get("energy.cohort_peak_share"))
    dc_fast_share = _float_or_zero(evidence.get("energy.dc_fast_share"))
    cohort_dc_fast_share = _float_or_zero(evidence.get("energy.cohort_dc_fast_share"))

    peak_above_cohort = peak_share > cohort_peak_share
    dc_fast_above_cohort = dc_fast_share > cohort_dc_fast_share

    if not (peak_above_cohort or dc_fast_above_cohort):
        return False, (
            "cause 'charging_rate' requires (energy.peak_share > cohort OR "
            "energy.dc_fast_share > cohort); "
            f"peak={peak_share} vs cohort={cohort_peak_share}, "
            f"dc_fast={dc_fast_share} vs cohort={cohort_dc_fast_share}"
        )

    return True, ""


def _check_charging_mix(evidence: dict[str, Any]) -> tuple[bool, str]:
    """charging_mix: public share of kWh above cohort."""
    public_share = _float_or_zero(evidence.get("energy.public_share"))
    cohort_public_share = _float_or_zero(evidence.get("energy.cohort_public_share"))

    if public_share <= cohort_public_share:
        return False, (
            "cause 'charging_mix' requires energy.public_share > "
            "energy.cohort_public_share, but "
            f"vehicle={public_share} <= cohort={cohort_public_share}"
        )
    return True, ""


def _check_repeat_repair(evidence: dict[str, Any]) -> tuple[bool, str]:
    """repeat_repair: maintenance.max_repeat_count >= 2 AND delta.maintenance_usd > 0.

    Decisions item 1 (W1 fix): cause repeat_repair requires both:
      1. At least two events of one service_type in the window (max_repeat_count >= 2)
      2. Maintenance above the cohort (delta.maintenance_usd > 0)

    A total outlier can be driven by lease, insurance or energy; maintenance being
    slightly above cohort with no repeating service does not support repeat_repair.
    """
    count = int(evidence.get("maintenance.max_repeat_count", 0))
    if count < 2:
        return False, (
            "cause 'repeat_repair' requires maintenance.max_repeat_count >= 2, "
            f"but count={count}"
        )
    delta_usd = _float_or_zero(evidence.get("delta.maintenance_usd"))
    if delta_usd <= 0:
        return False, (
            "cause 'repeat_repair' requires delta.maintenance_usd > 0 "
            "(maintenance above the cohort), "
            f"but delta.maintenance_usd={delta_usd} (W1)"
        )
    return True, ""


def _check_low_utilisation(evidence: dict[str, Any]) -> tuple[bool, str]:
    """low_utilisation: cost.miles < cost.fleet_median_miles."""
    miles = _float_or_zero(evidence.get("cost.miles"))
    fleet_median = _float_or_zero(evidence.get("cost.fleet_median_miles"))

    if miles >= fleet_median:
        return False, (
            "cause 'low_utilisation' requires cost.miles < cost.fleet_median_miles, "
            f"but miles={miles} >= fleet_median={fleet_median}"
        )
    return True, ""


def _check_efficiency(evidence: dict[str, Any]) -> tuple[bool, str]:
    """efficiency: vehicle kWh per 100 miles above cohort."""
    vehicle_kwh = _float_or_zero(evidence.get("energy.vehicle_kwh_per_100mi"))
    cohort_kwh = _float_or_zero(evidence.get("energy.cohort_kwh_per_100mi"))

    if vehicle_kwh <= cohort_kwh:
        return False, (
            "cause 'efficiency' requires energy.vehicle_kwh_per_100mi > "
            "energy.cohort_kwh_per_100mi, but "
            f"vehicle={vehicle_kwh} <= cohort={cohort_kwh}"
        )
    return True, ""


def _float_or_zero(value: Any) -> float:
    """Convert a value to float, returning 0.0 on failure."""
    if value is None:
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0
