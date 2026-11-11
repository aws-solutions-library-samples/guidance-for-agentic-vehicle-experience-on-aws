"""
Diagnosis building for the fi_cost Tier 2 component — D6 step 4 and step 6.

Spec: .kiro/specs/2026-10-02-fi-cost-stage-b/spec.md § D6 steps 4-6, § D7, § D8,
      § D9, § Tier classification item 4
Decisions: decisions.md 2026-10-04 "Architect calls from Group 1 review cycle 1"
           item 2 (severity has one source: ADP; AVX keeps no band table).
           decisions.md 2026-10-04 cycle 3 item 1 (dollar deltas in evidence).
           decisions.md 2026-10-04 (schedule_inspection uses delta.maintenance_usd).

This module contains two exports:

``RECORD_DIAGNOSIS_TOOL_SCHEMA``
    The Bedrock Converse toolSpec for ``record_diagnosis``.  Has exactly D6's six
    fields (cause, action, evidence_refs, headline, detail, confidence) and NO
    numeric/integer field anywhere (spec Tier item 4: the model cannot inject
    ``estimated_value_usd``).

``run_cost_diagnosis(evidence, model_reply) -> dict``
    Builds a Finding dict from the already-validated model reply and evidence.
    Deterministic seams (spec Tier item 4):
      - ``scope.vin`` ← ``evidence["vin"]`` (never model_reply)
      - ``severity`` ← ``evidence["severity"]`` (ADP-provided; no band table in AVX;
        decisions item 2)
      - ``recommended_action.estimated_value_usd`` ← ``estimate_saving(action, ...)``
        (never model_reply; the schema enforces no numeric field)
      - ``recommended_action.parameters.vehicle_vin`` ← ``evidence["vin"]`` (D9)
"""

from __future__ import annotations

import datetime
import re
from pathlib import Path
from typing import Any

from agents.fi_cost.savings import estimate_saving

# ---------------------------------------------------------------------------
# Playbook version → agent_version
# ---------------------------------------------------------------------------

_PLAYBOOK_PATH: Path = Path(__file__).parent / "playbook.md"

#: Full playbook text, loaded once at module import.  Used in the system prompt.
PLAYBOOK_TEXT: str = _PLAYBOOK_PATH.read_text()

_VER_RE = re.compile(r"##\s+version:\s+(\S+)")
_VER_MATCH = _VER_RE.search(PLAYBOOK_TEXT)
PLAYBOOK_VERSION: str = _VER_MATCH.group(1) if _VER_MATCH else "unknown"

#: Used as the Finding's ``agent_version`` field (spec Tier item 3).
#: Must be a semver string matching ``^[0-9]+\.[0-9]+\.[0-9]+$`` (frozen schema).
#: Equal to PLAYBOOK_VERSION so a playbook change bumps agent_version.
AGENT_VERSION: str = PLAYBOOK_VERSION

# ---------------------------------------------------------------------------
# Tool schema — RECORD_DIAGNOSIS_TOOL_SCHEMA
# ---------------------------------------------------------------------------

#: The Bedrock Converse toolSpec for ``record_diagnosis``.
#:
#: Input schema has exactly D6's six fields and NO numeric/integer field anywhere
#: (spec Tier classification item 4: "The model's output schema has no numeric field,
#: so it cannot supply the figure [estimated_value_usd]").
#:
#: Tests verify:
#:   - Exactly {"cause", "action", "evidence_refs", "headline", "detail", "confidence"}
#:   - _has_numeric(input_schema) is False (including list-type, anyOf/oneOf, items)
RECORD_DIAGNOSIS_TOOL_SCHEMA: dict[str, Any] = {
    "toolSpec": {
        "name": "record_diagnosis",
        "description": (
            "Record the diagnosis for a fleet vehicle cost anomaly. "
            "Identify the primary cause, the recommended action, which evidence "
            "keys you relied on, and write a concise operator-facing explanation."
        ),
        "inputSchema": {
            "json": {
                "type": "object",
                "properties": {
                    "cause": {
                        "type": "string",
                        "enum": [
                            "charging_rate",
                            "charging_mix",
                            "repeat_repair",
                            "low_utilisation",
                            "efficiency",
                            "unclear",
                        ],
                        "description": (
                            "Primary cause of the cost anomaly. "
                            "Must be supported by the numbers in the evidence. "
                            "For energy-only anomalies (cost.flag='energy') use only: "
                            "charging_rate, charging_mix, efficiency, or unclear."
                        ),
                    },
                    "action": {
                        "type": "string",
                        "enum": [
                            "shift_to_depot_off_peak",
                            "schedule_inspection",
                            "reassign_or_remarket",
                            "driver_coaching",
                            "no_action",
                        ],
                        "description": (
                            "Recommended action. Must be in the allowed set for the "
                            "chosen cause (see playbook cause→action table)."
                        ),
                    },
                    "evidence_refs": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Evidence-bundle keys you relied on. "
                            "Each key must exist in the evidence bundle."
                        ),
                    },
                    "headline": {
                        "type": "string",
                        "description": (
                            "Operator-facing headline: ≤120 characters. "
                            "No PII (names, phone numbers, email addresses)."
                        ),
                        "maxLength": 120,
                    },
                    "detail": {
                        "type": "string",
                        "description": (
                            "Operator-facing explanation: ≤600 characters. "
                            "Name the category, the comparison value, and the "
                            "recommended action. No PII."
                        ),
                        "maxLength": 600,
                    },
                    "confidence": {
                        "type": "string",
                        "enum": ["low", "medium", "high"],
                        "description": (
                            "Confidence level. Use 'high' when one clear cause explains "
                            ">80 % of the excess; 'medium' when most evidence agrees but "
                            "one signal is mixed; do not use 'low' (reserved for the "
                            "rules fallback)."
                        ),
                    },
                },
                "required": [
                    "cause",
                    "action",
                    "evidence_refs",
                    "headline",
                    "detail",
                    "confidence",
                ],
            }
        },
    }
}

# ---------------------------------------------------------------------------
# toolConfig for the Converse call (module-level to avoid rebuilding per call)
# ---------------------------------------------------------------------------

#: Full toolConfig dict passed to bedrock_runtime.converse().
TOOL_CONFIG: dict[str, Any] = {
    "tools": [RECORD_DIAGNOSIS_TOOL_SCHEMA],
    "toolChoice": {"tool": {"name": "record_diagnosis"}},
}

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def run_cost_diagnosis(
    evidence: dict[str, Any],
    model_reply: dict[str, Any],
    *,
    diagnosis_source: str | None = None,
) -> dict[str, Any]:
    """Build a Finding dict from a validated model reply and evidence.

    This function implements the deterministic seams in D6 step 6 and the Tier
    classification (spec § Tier item 4).  It receives an *already-validated*
    model reply (either from Bedrock or from ``rules_fallback``); it does NOT
    call the validator.

    Deterministic seams:
      - ``finding["scope"]["vin"]`` ← ``evidence["vin"]``
        (adversarial model reply with a different ``vin`` is ignored)
      - ``finding["severity"]`` ← ``evidence["severity"]``
        (ADP-provided; agent copies it — no band table in AVX; decisions item 2)
      - ``finding["recommended_action"]["estimated_value_usd"]``
        ← ``estimate_saving(action, _inputs_for(action, evidence))``
        (formula result, never any value from model_reply)
      - ``finding["recommended_action"]["parameters"]["vehicle_vin"]``
        ← ``evidence["vin"]``
        (D9: AVX sets it from scope)

    Parameters
    ----------
    evidence:
        Flat evidence dict for this vehicle (from ``gather_evidence``).
    model_reply:
        Validated ``input`` dict from the Bedrock ``toolUse`` block, or the
        dict returned by ``rules_fallback``.

    Returns
    -------
    dict
        A Finding dict ready for ``write_finding()``.  ``process_vehicle``
        adds ``inputs_hash`` before the call.
    """
    vin: str = evidence["vin"]
    year_month: str = evidence.get("year_month", evidence.get("cost.month", ""))
    severity: str = str(evidence.get("severity", "routine"))

    cause: str = model_reply["cause"]
    action: str = model_reply["action"]
    confidence: str = model_reply.get("confidence", "medium")
    headline: str = model_reply.get("headline", "")
    detail: str = model_reply.get("detail", "")

    # diagnosis_source from the code path: "low" confidence means rules fallback ran;
    # the validator blocks "low" from the model, so "low" iff rules_fallback returned this reply.
    # The code path decides model vs rules (process_vehicle passes it); a direct call
    # without it infers from confidence, which only the rules fallback may set to "low"
    # (validate_tool_input rejects "low" from the model).
    if diagnosis_source is None:
        diagnosis_source = "rules" if confidence == "low" else "model"
    if diagnosis_source not in ("model", "rules"):
        raise ValueError(f"diagnosis_source must be 'model' or 'rules', got {diagnosis_source!r}")

    # Compute estimated saving from evidence (deterministic seam — never model value).
    saving_result = estimate_saving(action, _inputs_for(action, evidence))

    # Build recommended_action — omitted entirely when action is "no_action" (D7, D6, C3 fix).
    recommended_action: dict[str, Any] | None = None
    if action != "no_action":
        parameters: dict[str, Any] = {
            "cost_action": action,
            "vehicle_vin": vin,          # always from evidence (D9)
            "month": year_month,
            "cause": cause,              # model's cause flows here (validated already)
        }
        recommended_action = {
            "action_kind": "apply_cost_action",
            "target_system": "cms",
            "parameters": parameters,
        }
        if saving_result is not None:
            recommended_action["estimated_value_usd"] = saving_result["estimated_value_usd"]
            recommended_action["estimated_value_basis"] = saving_result["estimated_value_basis"]

    # Timestamp for all evidence entries (frozen schema requires computed_at on each entry).
    now_utc: str = (
        datetime.datetime.now(datetime.timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )
    # Opaque reference to the ADP evidence batch for this vehicle/month.
    adp_ref: str = f"adp-evidence/{year_month}"

    def _adp_entry(kind: str, value: Any) -> dict[str, Any]:
        """Build a schema-valid evidence entry sourced from ADP (C1/W6)."""
        return {
            "kind": kind,
            "value": value,
            "vin": vin,
            "provenance": "live",
            "source": "adp",
            "source_ref": adp_ref,
            "computed_at": now_utc,
        }

    # Build evidence list — only from the evidence bundle, never from model_reply (C1).
    # D8: cost.diagnosis_source first, then all D8 keys present in the bundle.
    evidence_entries: list[dict[str, Any]] = [
        {
            "kind": "cost.diagnosis_source",
            "value": diagnosis_source,
            "vin": vin,
            "provenance": "live",
            "source": "agent-fi-cost",
            "source_ref": "local-decision",
            "computed_at": now_utc,
        }
    ]

    # D8 base keys (always included when present in evidence bundle).
    _D8_BASE_KEYS = (
        "cost.month", "cost.cost_per_mile", "cost.own_baseline",
        "cost.cohort_baseline", "cost.method", "cost.flag",
        "delta.energy_usd", "delta.maintenance_usd",
        "delta.lease_usd", "delta.insurance_usd",
    )
    for key in _D8_BASE_KEYS:
        if key in evidence:
            evidence_entries.append(_adp_entry(key, evidence[key]))

    # D8 energy-outlier keys (included when cost.flag is "energy" or "both").
    cost_flag = evidence.get("cost.flag", "total")
    if cost_flag in ("energy", "both"):
        for key in (
            "cost.energy_ratio_own", "cost.energy_ratio_cohort",
            "cost.energy_excess_usd", "cost.energy_method",
        ):
            if key in evidence:
                evidence_entries.append(_adp_entry(key, evidence[key]))

    # Add the evidence keys the model cited (evidence_refs), if not already included (W6).
    included_kinds = {e["kind"] for e in evidence_entries}
    for ref_key in model_reply.get("evidence_refs", []):
        if ref_key not in included_kinds and ref_key in evidence:
            evidence_entries.append(_adp_entry(ref_key, evidence[ref_key]))
            included_kinds.add(ref_key)

    finding: dict[str, Any] = {
        "agent_id": "agent-fi-cost",
        "finding_kind": "fleet.cost.anomaly",
        "scope": {"vin": vin},
        "subject_key": year_month,
        "severity": severity,          # from ADP, never recomputed here (decisions item 2)
        "confidence": confidence,
        "evidence": evidence_entries,
        "agent_version": AGENT_VERSION,
        "headline": headline,
        "detail": detail,
    }
    if recommended_action is not None:
        finding["recommended_action"] = recommended_action
    return finding


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _inputs_for(action: str, evidence: dict[str, Any]) -> dict[str, Any]:
    """Map evidence keys to the input dict expected by ``estimate_saving``.

    Mapping (interface.md "Evidence keys by D7 formula" and decisions cycle 3 item 1):

    shift_to_depot_off_peak:
        energy.public_and_peak_kwh  → public_and_peak_kwh
        energy.mean_public_peak_price_per_kwh → mean_public_peak_price_per_kwh
        energy.depot_off_peak_price_per_kwh   → depot_off_peak_price_per_kwh
        cost.month                  → month

    schedule_inspection:
        cost.maintenance            → vehicle_maintenance_cost
        cost.maintenance − delta.maintenance_usd → cohort_baseline_maintenance_cost
        cost.month                  → month
        (decisions cycle 3 item 1: delta.maintenance_usd is the dollar delta,
        not the per-mile delta.maintenance)

    reassign_or_remarket:
        cost.lease          → lease_cost
        cost.insurance      → insurance_cost
        cost.miles          → vehicle_miles
        cost.fleet_median_miles → fleet_median_miles
        cost.month          → month

    driver_coaching:
        cost.energy         → vehicle_energy_cost
        (decisions cycle 3 item 1: cost.energy is the view's energy_cost column,
        monthly USD; NOT cost.energy_public + cost.energy_depot)
        energy.cohort_kwh_per_100mi → cohort_kwh_per_100mi
        energy.vehicle_kwh_per_100mi → vehicle_kwh_per_100mi
        cost.month          → month
    """
    month = str(evidence.get("cost.month", evidence.get("year_month", "")))

    if action == "shift_to_depot_off_peak":
        return {
            "public_and_peak_kwh": evidence["energy.public_and_peak_kwh"],
            "mean_public_peak_price_per_kwh": evidence[
                "energy.mean_public_peak_price_per_kwh"
            ],
            "depot_off_peak_price_per_kwh": evidence[
                "energy.depot_off_peak_price_per_kwh"
            ],
            "month": month,
        }

    if action == "schedule_inspection":
        vehicle_maint = float(evidence["cost.maintenance"])
        delta_maint_usd = float(evidence["delta.maintenance_usd"])
        # cohort_baseline = vehicle - dollar_delta (decisions cycle 3 item 1)
        cohort_baseline = vehicle_maint - delta_maint_usd
        return {
            "vehicle_maintenance_cost": vehicle_maint,
            "cohort_baseline_maintenance_cost": cohort_baseline,
            "month": month,
        }

    if action == "reassign_or_remarket":
        return {
            "lease_cost": evidence["cost.lease"],
            "insurance_cost": evidence["cost.insurance"],
            "vehicle_miles": evidence["cost.miles"],
            "fleet_median_miles": evidence["cost.fleet_median_miles"],
            "month": month,
        }

    if action == "driver_coaching":
        # decisions cycle 3 item 1: formula reads cost.energy (monthly USD),
        # NOT cost.energy_public + cost.energy_depot.
        return {
            "vehicle_energy_cost": evidence["cost.energy"],
            "cohort_kwh_per_100mi": evidence["energy.cohort_kwh_per_100mi"],
            "vehicle_kwh_per_100mi": evidence["energy.vehicle_kwh_per_100mi"],
            "month": month,
        }

    # no_action or unknown → no inputs needed
    return {}
