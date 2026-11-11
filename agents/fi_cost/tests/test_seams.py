"""
Item 2 of task 1.4 — deterministic seams (Tier classification item 4).

Spec: .kiro/specs/2026-10-02-fi-cost-stage-b/spec.md § Tier classification item 4, D7, D8

The model's output schema has no numeric field, so ``estimated_value_usd`` can only
come from the D7 formula applied to evidence values.  ``severity`` comes from the ADP
``vehicle_cost_baseline`` row (decisions item 2); the agent copies it — no band table in
AVX.  VIN comes from the selection query.

Each test feeds ``run_cost_diagnosis()`` with a mock Bedrock response that carries:
  - a DIFFERENT VIN than the evidence VIN (under the real ``vin`` key);
  - a DIFFERENT severity than the evidence carries (under the real ``severity`` key);
  - a number under the real ``estimated_value_usd`` key.

Then asserts:
  - the written Finding's VIN equals the evidence VIN;
  - the written severity equals the evidence severity (copied from ADP, not from model);
  - ``estimated_value_usd`` equals the D7 formula result, not any number from the model.

Fix Group 1 changes (review cycle 1, C1 / W1a / W3):
  C1 — adversarial reply now uses real field names (``vin``, ``severity``,
        ``estimated_value_usd``), not ``_injected_*`` names an implementation would never read.
        The no-numeric-field test handles ``{"toolSpec": …}`` wrapper and list type forms.
  W1a — ``test_severity_routine_below_1_5`` now uses a valid cause/action pair and
         includes the evidence keys needed by the chosen action.  ``severity`` is in the
         evidence bundle so the seam test works without a band-table call.
  W3  — ``test_severity_band_at_exactly_1_5`` added (boundary, >=).

All tests are ``xfail(strict=True)`` — Group 3 implements the modules.
"""
from __future__ import annotations

import pytest

# ---------------------------------------------------------------------------
# Shared evidence bundle and model-reply fixtures
# ---------------------------------------------------------------------------

_EVIDENCE_VIN = "MRD-TEST-VIN-001"
_OTHER_VIN = "MRD-WRONG-VIN-999"

# ratio_cohort = 1.6 → ADP would emit severity "soon" (decisions item 2, D8: >=1.5)
_RATIO_COHORT = 1.6
_EXPECTED_SEVERITY = "soon"

# D7 formula inputs for shift_to_depot_off_peak
_EVIDENCE_BUNDLE = {
    "vin": _EVIDENCE_VIN,
    "fleet_id": "fleet-meridian-01",
    "year_month": "2026-08",
    "cost.month": "2026-08",
    "cost.cost_per_mile": 0.45,
    "cost.own_baseline": 0.30,
    "cost.cohort_baseline": 0.28,
    "cost.method": "own 6-month mean and same-month fleet-model median; outlier when both ratios >= 1.30",
    "ratio_cohort": _RATIO_COHORT,
    "ratio_own": 1.5,
    # ADP-provided severity (decisions item 2): agent copies this, no band table in AVX
    "severity": _EXPECTED_SEVERITY,
    "cost.energy_public": 320.0,
    "cost.energy_depot": 80.0,
    "energy.public_and_peak_kwh": 400.0,
    "energy.mean_public_peak_price_per_kwh": 0.38,
    "energy.depot_off_peak_price_per_kwh": 0.09,
    # Cause-consistency keys for validator — decisions item 1 charging_rate rule
    # W1 (cycle 2): added mean_price keys so charging_rate validation per item 1 passes
    "energy.mean_price_per_kwh": 0.35,           # vehicle mean > cohort 0.15
    "energy.cohort_mean_price_per_kwh": 0.15,
    "energy.public_share": 0.75,
    "energy.cohort_public_share": 0.30,
    "energy.peak_share": 0.40,                  # depot kWh at peak > cohort 0.15
    "energy.cohort_peak_share": 0.15,
    "energy.dc_fast_share": 0.60,               # public kWh at DC fast > cohort 0.25
    "energy.cohort_dc_fast_share": 0.25,
    "cost.maintenance": 200.0,
    "cost.lease": 600.0,
    "cost.insurance": 150.0,
    "cost.miles": 800.0,
    "cost.fleet_median_miles": 1000.0,
}

# Formula result for shift_to_depot_off_peak (D7): 400 * (0.38 - 0.09) = 116.0
_EXPECTED_VALUE_USD = 116.0

# A model reply that tries to inject a wrong VIN, wrong severity, and a number.
# These are placed under the REAL field names so that an M1 mutant reading
# model_reply.get("vin", ...), model_reply.get("severity"), or
# model_reply.get("estimated_value_usd", ...) would pick up the wrong values.
_MODEL_REPLY_WITH_WRONG_VIN_AND_NUMBER = {
    "cause": "charging_rate",
    "action": "shift_to_depot_off_peak",
    "evidence_refs": ["energy.public_and_peak_kwh", "energy.mean_public_peak_price_per_kwh"],
    "headline": f"Vehicle {_OTHER_VIN} costs $9999 too much on public DC charging",
    "detail": "Switching to depot off-peak saves $9999 per month.",
    "confidence": "high",
    # Adversarial values under real field names (M1 mutation target):
    "vin": _OTHER_VIN,                      # wrong VIN — must be ignored
    "severity": "attention",               # wrong severity — must be ignored (evidence has "soon")
    "estimated_value_usd": 9999.0,         # wrong value — must be ignored (formula gives 116.0)
}


# ---------------------------------------------------------------------------
# VIN seam: the written Finding's VIN comes from evidence, not the model reply
# ---------------------------------------------------------------------------


def test_finding_vin_comes_from_evidence_not_model():
    """Whatever the model names, the Finding's VIN equals the evidence VIN."""
    from agents.fi_cost.diagnose import run_cost_diagnosis  # noqa: F401

    finding = run_cost_diagnosis(
        evidence=_EVIDENCE_BUNDLE,
        model_reply=_MODEL_REPLY_WITH_WRONG_VIN_AND_NUMBER,
    )
    assert finding["scope"]["vin"] == _EVIDENCE_VIN, (
        "VIN must come from the evidence bundle; model reply naming another VIN must be ignored"
    )
    assert finding["scope"]["vin"] != _OTHER_VIN


# ---------------------------------------------------------------------------
# Severity seam: from ADP evidence row, not model reply (decisions item 2)
# ---------------------------------------------------------------------------


def test_severity_comes_from_evidence_not_model():
    """Severity is the ADP-provided evidence['severity']; model reply must not override it.

    Decisions item 2: 'The agent copies the selected row's severity. AVX keeps no band table.'
    The evidence bundle carries severity='soon' (ratio_cohort=1.6).
    The adversarial reply carries severity='attention' (wrong).
    The Finding must have 'soon'.
    """
    from agents.fi_cost.diagnose import run_cost_diagnosis  # noqa: F401

    finding = run_cost_diagnosis(
        evidence=_EVIDENCE_BUNDLE,
        model_reply=_MODEL_REPLY_WITH_WRONG_VIN_AND_NUMBER,
    )
    assert finding["severity"] == _EXPECTED_SEVERITY, (
        f"evidence['severity']='soon'; adversarial reply has 'attention'; finding must be 'soon'. "
        f"The agent copies severity from the ADP evidence row (decisions item 2)."
    )


# ---------------------------------------------------------------------------
# Value seam: estimated_value_usd from D7 formula, never a model number
# ---------------------------------------------------------------------------


def test_estimated_value_comes_from_formula_not_model():
    """estimated_value_usd equals the D7 formula result whatever the model returns."""
    from agents.fi_cost.diagnose import run_cost_diagnosis  # noqa: F401

    finding = run_cost_diagnosis(
        evidence=_EVIDENCE_BUNDLE,
        model_reply=_MODEL_REPLY_WITH_WRONG_VIN_AND_NUMBER,
    )
    rec = finding.get("recommended_action", {})
    assert rec.get("estimated_value_usd") == pytest.approx(_EXPECTED_VALUE_USD, rel=1e-4), (
        "estimated_value_usd must equal the formula result (400 * (0.38 - 0.09) = 116.0); "
        "model injected 9999.0 — formula must win"
    )


# ---------------------------------------------------------------------------
# recommended_action.parameters.vehicle_vin comes from evidence (D9)
# ---------------------------------------------------------------------------


def test_recommended_action_vin_comes_from_evidence():
    """recommended_action.parameters.vehicle_vin must equal evidence['vin'] (D9).

    D9: AVX sets vehicle_vin from scope (evidence VIN).  The adversarial reply
    carries vin=_OTHER_VIN; that must not reach recommended_action.parameters.
    """
    from agents.fi_cost.diagnose import run_cost_diagnosis  # noqa: F401

    finding = run_cost_diagnosis(
        evidence=_EVIDENCE_BUNDLE,
        model_reply=_MODEL_REPLY_WITH_WRONG_VIN_AND_NUMBER,
    )
    params = finding.get("recommended_action", {}).get("parameters", {})
    assert params.get("vehicle_vin") == _EVIDENCE_VIN, (
        "recommended_action.parameters.vehicle_vin must come from evidence['vin'], "
        "not from the model reply"
    )


# ---------------------------------------------------------------------------
# Model-output schema has no numeric field (M6 mutation target)
# ---------------------------------------------------------------------------


def test_record_diagnosis_tool_schema_has_no_numeric_field():
    """The record_diagnosis tool schema must contain no numeric (number/integer) field.

    Spec D6 step 4: 'The model's output schema has no numeric field, so it cannot
    supply the figure.'

    Handles both the bare {'name': ..., 'inputSchema': ...} shape and the
    Bedrock Converse toolConfig wrapper shape {'toolSpec': {'name': ..., 'inputSchema': ...}}.

    Also recurses through list-type forms (['number','null']), 'items', and
    'anyOf'/'oneOf'/'allOf' combinators (M6 mutation targets).

    Also asserts the property set is exactly D6's six fields.
    """
    from agents.fi_cost.diagnose import RECORD_DIAGNOSIS_TOOL_SCHEMA  # noqa: F401

    # Handle {"toolSpec": {...}} wrapper or bare {"name": ..., "inputSchema": ...}
    schema_root = RECORD_DIAGNOSIS_TOOL_SCHEMA
    if "toolSpec" in schema_root:
        schema_root = schema_root["toolSpec"]

    input_schema = schema_root.get("inputSchema", {}).get("json", {})

    # Assert exactly D6's six fields
    expected_fields = {
        "cause", "action", "evidence_refs", "headline", "detail", "confidence"
    }
    actual_fields = set(input_schema.get("properties", {}).keys())
    assert actual_fields == expected_fields, (
        f"record_diagnosis schema must have exactly D6's six fields {expected_fields}; "
        f"got {actual_fields}"
    )

    def _has_numeric(schema: dict) -> bool:
        """Recursively check for any number/integer field, including list forms."""
        t = schema.get("type")
        if isinstance(t, list):
            if any(v in ("number", "integer") for v in t):
                return True
        elif t in ("number", "integer"):
            return True
        for prop in schema.get("properties", {}).values():
            if _has_numeric(prop):
                return True
        for sub in (
            schema.get("anyOf", [])
            + schema.get("oneOf", [])
            + schema.get("allOf", [])
        ):
            if _has_numeric(sub):
                return True
        items = schema.get("items")
        if isinstance(items, dict) and _has_numeric(items):
            return True
        return False

    assert not _has_numeric(input_schema), (
        "record_diagnosis tool schema must have no numeric field; "
        "a numeric field lets the model supply estimated_value_usd, breaking the seam"
    )


# ---------------------------------------------------------------------------
# Severity: attention band (ratio_cohort >= 2.0)
# ---------------------------------------------------------------------------


def test_severity_attention_at_2x_ratio():
    """ADP emits severity='attention' for ratio_cohort >= 2.0 (D8); agent must copy it."""
    from agents.fi_cost.diagnose import run_cost_diagnosis  # noqa: F401

    # ADP would emit severity="attention" for ratio_cohort=2.0
    evidence_high = dict(
        _EVIDENCE_BUNDLE,
        ratio_cohort=2.0,
        severity="attention",   # from ADP (decisions item 2)
        vin=_EVIDENCE_VIN,
    )
    finding = run_cost_diagnosis(
        evidence=evidence_high,
        model_reply={
            "cause": "charging_rate",
            "action": "shift_to_depot_off_peak",
            "evidence_refs": ["energy.public_and_peak_kwh"],
            "headline": "High cost",
            "detail": "Costs too much.",
            "confidence": "high",
            # Adversarial: model tries to override with "soon"
            "severity": "soon",
        },
    )
    assert finding["severity"] == "attention"


# ---------------------------------------------------------------------------
# Severity: routine band (ratio_cohort < 1.5)
# Fix Group 1 / W1a: use a valid cause/action pair; evidence has severity key
# ---------------------------------------------------------------------------


def test_severity_routine_below_1_5():
    """ADP emits severity='routine' for ratio_cohort < 1.5 (D8); agent must copy it.

    W1a fix: action changed to 'no_action' (valid for charging_rate per decisions item 1
    cause→action table), which avoids the KeyError on missing kWh-per-100-mile keys.
    The 'driver_coaching' action is only valid for the 'efficiency' cause.
    """
    from agents.fi_cost.diagnose import run_cost_diagnosis  # noqa: F401

    # ADP would emit severity="routine" for ratio_cohort=1.35
    evidence_low = dict(
        _EVIDENCE_BUNDLE,
        ratio_cohort=1.35,
        severity="routine",     # from ADP (decisions item 2)
        vin=_EVIDENCE_VIN,
    )
    finding = run_cost_diagnosis(
        evidence=evidence_low,
        model_reply={
            "cause": "charging_rate",
            "action": "no_action",    # valid per decisions item 1: charging_rate → {shift_to_depot_off_peak, no_action}
            "evidence_refs": ["energy.public_and_peak_kwh"],
            "headline": "Slightly above baseline",
            "detail": "Minor cost gap.",
            "confidence": "medium",
            # Adversarial: model tries to override with "soon"
            "severity": "soon",
        },
    )
    assert finding["severity"] == "routine"


# ---------------------------------------------------------------------------
# Severity: boundary at exactly 1.5 (W3)
# ---------------------------------------------------------------------------


def test_severity_at_exactly_1_5_is_soon():
    """ADP emits severity='soon' at exactly ratio_cohort=1.5 (D8: >=1.5 → soon, not >1.5).

    W3 fix: the boundary was untested. A '> 1.5' implementation would emit 'routine'
    here; the test catches it because evidence['severity']='soon' must be preserved.
    """
    from agents.fi_cost.diagnose import run_cost_diagnosis  # noqa: F401

    # ADP emits "soon" at exactly 1.5 (>= 1.5, not > 1.5)
    evidence_at_boundary = dict(
        _EVIDENCE_BUNDLE,
        ratio_cohort=1.5,
        severity="soon",        # from ADP (decisions item 2)
        vin=_EVIDENCE_VIN,
    )
    finding = run_cost_diagnosis(
        evidence=evidence_at_boundary,
        model_reply={
            "cause": "charging_rate",
            "action": "no_action",
            "evidence_refs": ["energy.public_and_peak_kwh"],
            "headline": "At boundary",
            "detail": "ratio_cohort exactly 1.5.",
            "confidence": "medium",
            # Adversarial: model tries to downgrade to "routine"
            "severity": "routine",
        },
    )
    assert finding["severity"] == "soon", (
        "ADP emits severity='soon' at ratio_cohort=1.5 (>= 1.5); "
        "the agent must copy it from evidence, not let the model override it"
    )



# ---------------------------------------------------------------------------
# W3 (cycle 2): Severity from evidence when ADP parameters are non-default
# M1b target: severity recomputed from ratio_cohort with DEFAULT thresholds → wrong
# ---------------------------------------------------------------------------


def test_severity_copied_from_evidence_when_adp_parameters_nondefault():
    """Severity from evidence['severity'] even when it disagrees with the D8 default bands.

    M1b mutant: severity = ("attention" if ratio_cohort >= 2.0 else
                            "soon" if ratio_cohort >= 1.5 else "routine")
    With ratio_cohort=1.6 the default bands give 'soon'.
    ADP emits 'attention' when severity_attention_ratio has been lowered to 1.5.
    evidence['severity'] = 'attention'; agent must copy it, never recompute.

    W3 fix: the previous seam tests all used evidence['severity'] equal to what the
    default D8 bands would give, so M1b (which recomputes using default thresholds)
    produced the same result and was not caught.  This case explicitly disagrees.
    """
    from agents.fi_cost.diagnose import run_cost_diagnosis  # noqa: F401

    # ratio_cohort = 1.6: default D8 bands (attention>=2.0, soon>=1.5) give "soon".
    # ADP parameters changed (severity_attention_ratio = 1.5): ADP emits "attention".
    evidence_nondefault = dict(
        _EVIDENCE_BUNDLE,
        ratio_cohort=1.6,
        severity="attention",   # ADP emits this; default AVX bands would give "soon"
    )
    finding = run_cost_diagnosis(
        evidence=evidence_nondefault,
        model_reply={
            "cause": "charging_rate",
            "action": "no_action",
            "evidence_refs": ["energy.public_and_peak_kwh"],
            "headline": "Cost above baseline",
            "detail": "Vehicle uses public charging extensively.",
            "confidence": "medium",
            "severity": "soon",   # adversarial: model follows default bands
        },
    )
    assert finding["severity"] == "attention", (
        "ratio_cohort=1.6; ADP emits severity='attention' when "
        "severity_attention_ratio=1.5 (non-default). "
        "An AVX band table with default thresholds (attention>=2.0) gives 'soon' — wrong. "
        "M1b is caught here: agent must copy evidence['severity'], not recompute. "
        "(decisions item 2: AVX keeps no band table)"
    )
