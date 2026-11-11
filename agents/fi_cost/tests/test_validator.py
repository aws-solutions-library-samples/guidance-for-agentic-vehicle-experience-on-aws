"""
Item 3 of task 1.4 — validator and rules fallback (D6 steps 5 and fallback path).

Spec: .kiro/specs/2026-10-02-fi-cost-stage-b/spec.md § D6 steps 4-5

Validator checks:
  - unknown ``cause`` is rejected;
  - unknown ``action`` is rejected;
  - an ``evidence_refs`` key not in the bundle is rejected;
  - a cause the numbers contradict is rejected
    (``charging_rate`` requires public/peak share above cohort — if that is not
    true the validator must reject it).

Fallback behaviour (after one retry):
  - ``diagnosis_source: rules`` in an evidence entry (kind == "cost.diagnosis_source");
  - ``confidence: low``;
  - cause = the category with the largest dollar delta (delta.*_usd);
  - the Finding is still written (a run never depends on the model).

Retry contract (C3):
  - ``process_vehicle`` retries exactly once on a failed validation, then falls back;
  - assert exactly 2 Bedrock calls when both fail;
  - assert exactly 2 Bedrock calls when second succeeds (valid-on-retry path).

Fix Group 1 changes (review cycle 1, C3 / W1b):
  C3 — added ``test_invalid_twice_yields_fallback_after_exactly_two_calls`` and
        ``test_valid_on_retry_uses_model_diagnosis``, each driving ``process_vehicle``
        with a stub and asserting exact call counts.
  W1b — fallback tests now check ``kind == "cost.diagnosis_source"`` per D8 (was
          ``"diagnosis_source"``); cause assertion fixed to ``"repeat_repair"`` or
          ``"unclear"`` (``"schedule_inspection"`` is an action, not a cause).

Fix Group 3 changes (review cycle 3, W1 / W2):
  W1 — added one test per missing fallback branch: energy → charging_mix, energy →
       efficiency, maintenance count=2 → exactly repeat_repair, maintenance count=1
       → unclear, lease miles≥median → unclear.  Fixed test_fallback_cause_is_
       largest_delta_category to assert == "repeat_repair" (not just in the set).
       Fixed test_invalid_twice to assert params["cause"] == "repeat_repair".
  W2 — added delta.*_usd (dollar delta) and cost.energy to _VALID_EVIDENCE and to
       all fallback test overrides; added test_schedule_inspection_uses_dollar_delta
       and test_driver_coaching_uses_cost_energy.  ADP view keeps per-mile deltas;
       AVX evidence step computes dollar deltas (decisions cycle 3 item 1).

All tests are xfail(strict=True).  Group 3 makes them pass.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

_VALID_EVIDENCE = {
    "vin": "MRD-TEST-VIN-002",
    "fleet_id": "fleet-meridian-02",
    "year_month": "2026-08",
    "cost.month": "2026-08",
    "ratio_cohort": 1.7,
    "ratio_own": 1.4,
    # ADP-provided severity (decisions item 2)
    "severity": "soon",
    "energy.public_and_peak_kwh": 400.0,
    "energy.mean_public_peak_price_per_kwh": 0.38,
    "energy.depot_off_peak_price_per_kwh": 0.09,
    # Cause-consistency keys — decisions item 1 charging_rate rule:
    #   mean price per kWh above cohort AND (peak share or DC-fast share above cohort)
    "energy.mean_price_per_kwh": 0.35,          # vehicle mean > cohort 0.15
    "energy.cohort_mean_price_per_kwh": 0.15,
    "energy.public_share": 0.75,                # above cohort → charging_mix is consistent
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
    "cost.cohort_baseline": 0.28,
    # Monthly energy cost in USD (ADP view's energy_cost column; decisions cycle 3 item 1)
    "cost.energy": 280.0,
    # Fallback energy sub-cause keys
    "energy.vehicle_kwh_per_100mi": 35.0,
    "energy.cohort_kwh_per_100mi": 25.0,
    # Per-mile deltas from ADP view (delta_<cat>_cohort, $/mi; decisions cycle 3 item 1).
    # The fallback and D7 formulas read delta.*_usd (dollar deltas), not these.
    "delta.maintenance": 0.06,        # 0.06 $/mi × 800 miles = 48.0 USD
    "delta.energy": 0.375,            # 0.375 $/mi × 800 miles = 300.0 USD
    "delta.lease": 0.025,             # 0.025 $/mi × 800 miles = 20.0 USD
    "delta.insurance": 0.0125,        # 0.0125 $/mi × 800 miles = 10.0 USD
    # Dollar deltas (AVX evidence step computes: delta_<cat>_cohort × cost.miles;
    # decisions cycle 3 item 1; fallback category-selection uses these)
    "delta.maintenance_usd": 48.0,    # 0.06 × 800
    "delta.energy_usd": 300.0,        # 0.375 × 800
    "delta.lease_usd": 20.0,          # 0.025 × 800
    "delta.insurance_usd": 10.0,      # 0.0125 × 800
    # Repeat-repair count (for maintenance fallback)
    "maintenance.max_repeat_count": 2,
}


# ---------------------------------------------------------------------------
# Bedrock stubs for C3 tests
# ---------------------------------------------------------------------------


def _make_always_invalid_stub(call_tracker: list) -> MagicMock:
    """Stub that always returns an invalid reply (unknown cause)."""

    def _converse(**kwargs):
        call_tracker.append(kwargs)
        return {
            "output": {"message": {"content": [{"toolUse": {
                "toolUseId": "t-bad",
                "name": "record_diagnosis",
                "input": {
                    "cause": "bad_cause_that_validator_rejects",
                    "action": "shift_to_depot_off_peak",
                    "evidence_refs": [],
                    "headline": "Bad",
                    "detail": "Bad detail.",
                    "confidence": "high",
                },
            }}]}},
            "usage": {"inputTokens": 10, "outputTokens": 5},
        }

    stub = MagicMock()
    stub.converse = _converse
    stub.apply_guardrail.return_value = {"action": "NONE"}
    return stub


def _make_valid_on_retry_stub(call_tracker: list) -> MagicMock:
    """Stub: call 1 → invalid (bad cause); call 2 → valid charging_rate reply."""

    def _converse(**kwargs):
        call_num = len(call_tracker)
        call_tracker.append(kwargs)
        if call_num == 0:
            # First call: invalid
            return {
                "output": {"message": {"content": [{"toolUse": {
                    "toolUseId": "t-bad",
                    "name": "record_diagnosis",
                    "input": {
                        "cause": "bad_cause_that_validator_rejects",
                        "action": "shift_to_depot_off_peak",
                        "evidence_refs": [],
                        "headline": "Bad",
                        "detail": "Bad detail.",
                        "confidence": "high",
                    },
                }}]}},
                "usage": {"inputTokens": 10, "outputTokens": 5},
            }
        else:
            # Second call (retry): valid
            return {
                "output": {"message": {"content": [{"toolUse": {
                    "toolUseId": "t-good",
                    "name": "record_diagnosis",
                    "input": {
                        "cause": "charging_rate",
                        "action": "shift_to_depot_off_peak",
                        "evidence_refs": ["energy.public_and_peak_kwh",
                                          "energy.mean_public_peak_price_per_kwh"],
                        "headline": "High public charging",
                        "detail": "Shift to depot off-peak.",
                        "confidence": "high",
                    },
                }}]}},
                "usage": {"inputTokens": 80, "outputTokens": 40},
            }

    stub = MagicMock()
    stub.converse = _converse
    stub.apply_guardrail.return_value = {"action": "NONE"}
    return stub


# ---------------------------------------------------------------------------
# Unknown cause
# ---------------------------------------------------------------------------


def test_unknown_cause_is_rejected():
    """A ``cause`` value not in the closed list is rejected by the validator."""
    from agents.fi_cost.validate import validate_tool_input  # noqa: F401

    reply = {
        "cause": "bad_cause_value",
        "action": "shift_to_depot_off_peak",
        "evidence_refs": ["energy.public_and_peak_kwh"],
        "headline": "Something",
        "detail": "Something longer.",
        "confidence": "high",
    }
    is_valid, error = validate_tool_input(reply, _VALID_EVIDENCE)
    assert not is_valid
    assert "cause" in error.lower()


# ---------------------------------------------------------------------------
# Unknown action
# ---------------------------------------------------------------------------


def test_unknown_action_is_rejected():
    """An ``action`` value not in the closed list is rejected."""
    from agents.fi_cost.validate import validate_tool_input  # noqa: F401

    reply = {
        "cause": "charging_rate",
        "action": "invent_new_action",
        "evidence_refs": ["energy.public_and_peak_kwh"],
        "headline": "Something",
        "detail": "Something longer.",
        "confidence": "high",
    }
    is_valid, error = validate_tool_input(reply, _VALID_EVIDENCE)
    assert not is_valid
    assert "action" in error.lower()


# ---------------------------------------------------------------------------
# evidence_refs key not in bundle
# ---------------------------------------------------------------------------


def test_evidence_ref_not_in_bundle_is_rejected():
    """An evidence_refs entry that does not exist in the bundle is rejected."""
    from agents.fi_cost.validate import validate_tool_input  # noqa: F401

    reply = {
        "cause": "charging_rate",
        "action": "shift_to_depot_off_peak",
        "evidence_refs": ["energy.public_and_peak_kwh", "nonexistent.key"],
        "headline": "Something",
        "detail": "Something longer.",
        "confidence": "high",
    }
    is_valid, error = validate_tool_input(reply, _VALID_EVIDENCE)
    assert not is_valid
    assert "evidence_ref" in error.lower() or "nonexistent" in error.lower()


# ---------------------------------------------------------------------------
# Cause-consistency: charging_rate and charging_mix — decisions item 1
# charging_rate: mean price per kWh above cohort AND (depot peak share OR public
#   DC-fast share above cohort). charging_mix: public share above cohort.
# Public share is charging_mix's rule, not charging_rate's (cycle 2 W1): the first
# two tests fail a validator that conflates them, in either direction.
# ---------------------------------------------------------------------------

_CHARGING_RATE_REPLY = {
    "cause": "charging_rate",
    "action": "shift_to_depot_off_peak",
    "evidence_refs": ["energy.mean_price_per_kwh", "energy.dc_fast_share"],
    "headline": "Paying more per kWh than the fleet",
    "detail": "More DC fast and peak-rate charging than similar vehicles.",
    "confidence": "high",
}


def test_charging_rate_rejected_when_neither_peak_nor_dc_fast_share_above_cohort():
    """Price above cohort and public share above cohort, but peak and DC-fast shares
    at or below cohort: charging_rate is contradicted. A validator that checks public
    share for charging_rate accepts this and fails the test."""
    from agents.fi_cost.validate import validate_tool_input  # noqa: F401

    evidence = dict(
        _VALID_EVIDENCE,
        **{
            "energy.peak_share": 0.10, "energy.cohort_peak_share": 0.15,
            "energy.dc_fast_share": 0.25, "energy.cohort_dc_fast_share": 0.25,
            "energy.public_share": 0.75, "energy.cohort_public_share": 0.30,
        },
    )
    is_valid, _ = validate_tool_input(_CHARGING_RATE_REPLY, evidence)
    assert not is_valid


def test_charging_rate_accepted_on_dc_fast_share_with_public_share_below_cohort():
    """Price and DC-fast share above cohort, public share below it: charging_rate is
    supported. A validator that requires public share for charging_rate rejects it."""
    from agents.fi_cost.validate import validate_tool_input  # noqa: F401

    evidence = dict(
        _VALID_EVIDENCE,
        **{
            "energy.peak_share": 0.10, "energy.cohort_peak_share": 0.15,
            "energy.dc_fast_share": 0.60, "energy.cohort_dc_fast_share": 0.25,
            "energy.public_share": 0.20, "energy.cohort_public_share": 0.30,
        },
    )
    is_valid, error = validate_tool_input(_CHARGING_RATE_REPLY, evidence)
    assert is_valid, error


def test_charging_rate_accepted_on_peak_share_alone():
    """Price and depot peak share above cohort, DC-fast share not: still supported."""
    from agents.fi_cost.validate import validate_tool_input  # noqa: F401

    evidence = dict(
        _VALID_EVIDENCE,
        **{
            "energy.peak_share": 0.40, "energy.cohort_peak_share": 0.15,
            "energy.dc_fast_share": 0.20, "energy.cohort_dc_fast_share": 0.25,
        },
    )
    is_valid, error = validate_tool_input(_CHARGING_RATE_REPLY, evidence)
    assert is_valid, error


def test_charging_mix_rejected_when_public_share_not_above_cohort():
    """charging_mix needs public share above cohort; price and DC-fast don't count."""
    from agents.fi_cost.validate import validate_tool_input  # noqa: F401

    evidence = dict(
        _VALID_EVIDENCE,
        **{"energy.public_share": 0.30, "energy.cohort_public_share": 0.30},
    )
    reply = dict(_CHARGING_RATE_REPLY, cause="charging_mix",
                 evidence_refs=["energy.public_share"])
    is_valid, _ = validate_tool_input(reply, evidence)
    assert not is_valid


def test_charging_mix_accepted_when_public_share_above_cohort():
    from agents.fi_cost.validate import validate_tool_input  # noqa: F401

    reply = dict(_CHARGING_RATE_REPLY, cause="charging_mix",
                 evidence_refs=["energy.public_share"])
    is_valid, error = validate_tool_input(reply, _VALID_EVIDENCE)
    assert is_valid, error


def test_charging_rate_cause_rejected_when_mean_price_not_above_cohort():
    """charging_rate is rejected when mean price per kWh <= cohort mean price.

    Decisions item 1: charging_rate requires mean price per kWh above cohort AND
    public/peak (DC-fast) share above cohort.  If mean price is not above cohort,
    reject the cause even if share is above cohort.
    W1 fix: this tests the first (mean price) condition, which was missing.
    """
    from agents.fi_cost.validate import validate_tool_input  # noqa: F401

    # mean_price = 0.10 is BELOW cohort = 0.15 → charging_rate contradicted
    # (public_share is above cohort: 0.75 > 0.30)
    evidence_low_price = dict(
        _VALID_EVIDENCE,
        **{
            "energy.mean_price_per_kwh": 0.10,
            "energy.cohort_mean_price_per_kwh": 0.15,
        },
    )
    reply = {
        "cause": "charging_rate",
        "action": "shift_to_depot_off_peak",
        "evidence_refs": ["energy.public_and_peak_kwh"],
        "headline": "High public charging cost",
        "detail": "Rate above cohort.",
        "confidence": "high",
    }
    is_valid, error = validate_tool_input(reply, evidence_low_price)
    assert not is_valid, (
        "charging_rate cause must be rejected when mean price per kWh <= cohort mean price "
        "(decisions item 1 first condition: mean price above cohort)"
    )


# ---------------------------------------------------------------------------
# W2a: Disallowed cause/action pair is rejected
# ---------------------------------------------------------------------------


def test_disallowed_action_for_cause_is_rejected():
    """A cause/action pair not in the allowed table is rejected.

    Decisions item 1: charging_rate allows {shift_to_depot_off_peak, no_action}.
    reassign_or_remarket is only allowed for repeat_repair and low_utilisation.
    W2a fix: a validator without the pair-check passes this; a correct one fails it.
    """
    from agents.fi_cost.validate import validate_tool_input  # noqa: F401

    reply = {
        "cause": "charging_rate",
        "action": "reassign_or_remarket",   # not in charging_rate's allowed set
        "evidence_refs": ["energy.public_and_peak_kwh"],
        "headline": "Something",
        "detail": "Something longer.",
        "confidence": "high",
    }
    is_valid, error = validate_tool_input(reply, _VALID_EVIDENCE)
    assert not is_valid, (
        "reassign_or_remarket is not an allowed action for charging_rate "
        "(decisions item 1); the pair-check must reject it"
    )
    assert "action" in error.lower() or "allow" in error.lower() or "cause" in error.lower()


# ---------------------------------------------------------------------------
# W2b: Fallback rules — one test per fallback path, with cause and action asserted
# ---------------------------------------------------------------------------


def test_fallback_energy_largest_delta_charging_rate_when_mean_price_wins():
    """Fallback: energy dollar delta largest + mean price per kWh above cohort → charging_rate.

    Decisions item 1 fallback energy rule: energy → whichever of
    {charging_rate, charging_mix, efficiency} signal is furthest above cohort.
    Mean price wins (0.45/0.10 − 1 = 3.5 > 0.31/0.30 − 1 = 0.033 > 25/25 − 1 = 0.0).
    Action must be the first allowed for charging_rate: shift_to_depot_off_peak.
    W2b fix: energy fallback path was not tested.
    Fix Group 3 W2: uses delta.energy_usd (dollar delta) for category selection.
    """
    from agents.fi_cost.fallback import rules_fallback  # noqa: F401

    # delta.energy_usd = 500.0 is the largest category dollar delta; energy sub-cause signals:
    # charging_rate: mean_price 0.45/0.10 - 1 = 3.5 ← wins
    # charging_mix: public_share 0.31/0.30 - 1 = 0.033
    # efficiency: vehicle_kwh 25/25 - 1 = 0.0
    evidence = dict(
        _VALID_EVIDENCE,
        **{
            "delta.energy_usd": 500.0,          # largest category
            "delta.maintenance_usd": 50.0,
            "delta.lease_usd": 20.0,
            "delta.insurance_usd": 10.0,
            "energy.mean_price_per_kwh": 0.45,       # 3.5 × above cohort
            "energy.cohort_mean_price_per_kwh": 0.10,
            "energy.public_share": 0.31,             # barely above cohort (0.033)
            "energy.cohort_public_share": 0.30,
            "energy.vehicle_kwh_per_100mi": 25.0,    # at cohort (0.0)
            "energy.cohort_kwh_per_100mi": 25.0,
        },
    )
    result = rules_fallback(evidence)
    assert result["cause"] == "charging_rate", (
        "With mean_price/cohort_mean_price ratio the highest energy signal, "
        "fallback must select cause='charging_rate'"
    )
    assert result["action"] == "shift_to_depot_off_peak", (
        "First allowed action for charging_rate is 'shift_to_depot_off_peak' "
        "(decisions item 1)"
    )


def test_fallback_lease_insurance_largest_gives_low_utilisation_when_miles_below_median():
    """Fallback: lease/insurance dollar delta largest + miles < fleet_median → low_utilisation.

    Decisions item 1 fallback rule: lease/insurance → low_utilisation if miles below
    fleet median, else unclear.  Action = reassign_or_remarket (first allowed).
    W2b fix: lease/insurance path was not tested.
    Fix Group 3 W2: uses delta.lease_usd (dollar delta) for category selection.
    """
    from agents.fi_cost.fallback import rules_fallback  # noqa: F401

    evidence = dict(
        _VALID_EVIDENCE,
        **{
            "delta.energy_usd": 20.0,
            "delta.maintenance_usd": 30.0,
            "delta.lease_usd": 500.0,         # largest
            "delta.insurance_usd": 100.0,
            "cost.miles": 300.0,               # below fleet_median 1000.0
            "cost.fleet_median_miles": 1000.0,
        },
    )
    result = rules_fallback(evidence)
    assert result["cause"] == "low_utilisation", (
        "With lease dollar delta largest and miles < fleet median, "
        "fallback must select cause='low_utilisation'"
    )
    assert result["action"] == "reassign_or_remarket", (
        "First allowed action for low_utilisation is 'reassign_or_remarket' "
        "(decisions item 1)"
    )


def test_fallback_no_positive_delta_gives_unclear_no_action():
    """Fallback: no positive dollar delta → unclear, action=no_action.

    Decisions item 1 fallback rule: no positive delta → unclear.
    W2b fix: unclear path was not tested.
    Fix Group 3 W2: uses delta.*_usd (dollar deltas) for category selection.
    """
    from agents.fi_cost.fallback import rules_fallback  # noqa: F401

    evidence = dict(
        _VALID_EVIDENCE,
        **{
            "delta.energy_usd": -10.0,      # all negative: no category is above cohort
            "delta.maintenance_usd": -5.0,
            "delta.lease_usd": -2.0,
            "delta.insurance_usd": -1.0,
        },
    )
    result = rules_fallback(evidence)
    assert result["cause"] == "unclear", (
        "With no positive dollar delta, fallback must select cause='unclear'"
    )
    assert result["action"] == "no_action", (
        "The only allowed action for 'unclear' is 'no_action' (decisions item 1)"
    )





def test_valid_reply_passes_validation():
    """A well-formed reply with consistent cause passes the validator."""
    from agents.fi_cost.validate import validate_tool_input  # noqa: F401

    reply = {
        "cause": "charging_rate",
        "action": "shift_to_depot_off_peak",
        "evidence_refs": ["energy.public_and_peak_kwh", "energy.mean_public_peak_price_per_kwh"],
        "headline": "High public charging share",
        "detail": "Vehicle uses 75% public DC fast vs 30% cohort; shift to depot off-peak.",
        "confidence": "high",
    }
    is_valid, _error = validate_tool_input(reply, _VALID_EVIDENCE)
    assert is_valid


# ---------------------------------------------------------------------------
# Fallback after one retry: diagnosis_source=rules, confidence=low
# W1b fix: kind == "cost.diagnosis_source" per D8 (was "diagnosis_source")
# W1b fix: cause assertion uses valid cause names only ("repeat_repair" or "unclear")
# ---------------------------------------------------------------------------


def test_fallback_after_retry_sets_diagnosis_source_rules():
    """After one retry still fails, the rules fallback sets cost.diagnosis_source='rules'.

    W1b fix: evidence entry kind is 'cost.diagnosis_source' per D8.
    Fix Group 3 W2: uses delta.maintenance_usd (dollar delta) for category selection.
    """
    from agents.fi_cost.fallback import rules_fallback  # noqa: F401

    # Evidence where maintenance dollar delta is the largest
    evidence = dict(
        _VALID_EVIDENCE,
        **{
            "delta.maintenance_usd": 600.0,
            "delta.energy_usd": 100.0,
            "delta.lease_usd": 50.0,
            "delta.insurance_usd": 30.0,
        },
    )
    result = rules_fallback(evidence)
    evidence_entries = result.get("evidence", [])
    # W1b fix: kind is "cost.diagnosis_source" per D8, not bare "diagnosis_source"
    sources = [e.get("value") for e in evidence_entries if e.get("kind") == "cost.diagnosis_source"]
    assert "rules" in sources, (
        "rules fallback must include an evidence entry with "
        "kind='cost.diagnosis_source' and value='rules' (D8)"
    )


def test_fallback_confidence_is_low():
    """The rules fallback always produces confidence='low'.

    Fix Group 3 W2: uses delta.maintenance_usd (dollar delta) for category selection.
    """
    from agents.fi_cost.fallback import rules_fallback  # noqa: F401

    evidence = dict(
        _VALID_EVIDENCE,
        **{
            "delta.maintenance_usd": 600.0,
            "delta.energy_usd": 100.0,
            "delta.lease_usd": 50.0,
            "delta.insurance_usd": 30.0,
        },
    )
    result = rules_fallback(evidence)
    assert result["confidence"] == "low", (
        "rules fallback must always produce confidence='low' per D6"
    )


def test_fallback_cause_is_largest_delta_category():
    """Fallback picks the category with the largest dollar delta as the cause.

    W1b fix: cause must be 'repeat_repair' when maintenance is largest AND
    maintenance.max_repeat_count >= 2 (decisions item 1: maintenance →
    repeat_repair if a service type repeats, else unclear).
    'schedule_inspection' is an action, not a cause.
    Fix Group 3 W1: changed assertion from 'in ("repeat_repair", "unclear")' to
    '== "repeat_repair"' when max_repeat_count=2.  C3-6 (always "unclear") would
    have passed the old assertion; it must fail this one.
    Fix Group 3 W2: uses delta.*_usd (dollar deltas) for category selection.
    """
    from agents.fi_cost.fallback import rules_fallback  # noqa: F401

    evidence = dict(
        _VALID_EVIDENCE,
        **{
            "delta.maintenance_usd": 600.0,   # largest
            "delta.energy_usd": 100.0,
            "delta.lease_usd": 50.0,
            "delta.insurance_usd": 30.0,
            "maintenance.max_repeat_count": 2,  # >= 2 → exactly repeat_repair
        },
    )
    result = rules_fallback(evidence)
    # maintenance is largest dollar delta, max_repeat_count=2 → exactly repeat_repair
    # Fix Group 3 W1: "unclear" is NOT acceptable here; C3-6 returns it and must fail.
    assert result["cause"] == "repeat_repair", (
        "When maintenance dollar delta is the largest and max_repeat_count=2, "
        "the fallback cause must be exactly 'repeat_repair' (decisions item 1). "
        "C3-6 (always 'unclear') fails this assertion."
    )

# ---------------------------------------------------------------------------
# W1 (cycle 3) — One failing case per missing fallback branch
# C3-5 (always charging_rate for energy) must fail the charging_mix and
# efficiency tests.  C3-6 (always unclear for maintenance) must fail the
# repeat_repair test.  A miles-ignoring lease fallback must fail the
# lease-at-median test.
# ---------------------------------------------------------------------------


def test_fallback_energy_charging_mix_when_public_share_wins():
    """Fallback: energy dollar delta largest + public share relative signal highest → charging_mix.

    Cycle 3 W1: energy's charging_mix outcome was not tested, so C3-5
    (always charging_rate) survived.  The three energy signals are:
      charging_rate: mean_price / cohort_mean_price − 1
      charging_mix:  public_share / cohort_public_share − 1
      efficiency:    vehicle_kwh / cohort_kwh − 1
    Here public_share wins.  Action must be first for charging_mix:
    shift_to_depot_off_peak.
    """
    from agents.fi_cost.fallback import rules_fallback  # noqa: F401

    evidence = dict(
        _VALID_EVIDENCE,
        **{
            "delta.energy_usd": 500.0,        # largest category
            "delta.maintenance_usd": 30.0,
            "delta.lease_usd": 20.0,
            "delta.insurance_usd": 10.0,
            # Public share relative signal = 0.75/0.30 − 1 = 1.50 (highest)
            "energy.mean_price_per_kwh": 0.20,        # price: 0.20/0.15 − 1 = 0.33
            "energy.cohort_mean_price_per_kwh": 0.15,
            "energy.public_share": 0.75,              # public: 0.75/0.30 − 1 = 1.50 ← wins
            "energy.cohort_public_share": 0.30,
            "energy.vehicle_kwh_per_100mi": 28.0,     # efficiency: 28/25 − 1 = 0.12
            "energy.cohort_kwh_per_100mi": 25.0,
        },
    )
    result = rules_fallback(evidence)
    assert result["cause"] == "charging_mix", (
        "With public_share relative signal highest (1.50 > 0.33 price, > 0.12 efficiency), "
        "fallback must select cause='charging_mix' (decisions item 1 energy sub-cause rule). "
        "C3-5 (always 'charging_rate') fails this assertion."
    )
    assert result["action"] == "shift_to_depot_off_peak", (
        "First allowed action for charging_mix is 'shift_to_depot_off_peak' (decisions item 1)"
    )


def test_fallback_energy_efficiency_when_kwh_per_100mi_wins():
    """Fallback: energy dollar delta largest + kWh/100mi relative signal highest → efficiency.

    Cycle 3 W1: energy's efficiency outcome was not tested, so C3-5
    (always charging_rate) survived.  Action must be first for efficiency:
    driver_coaching.
    """
    from agents.fi_cost.fallback import rules_fallback  # noqa: F401

    evidence = dict(
        _VALID_EVIDENCE,
        **{
            "delta.energy_usd": 500.0,        # largest category
            "delta.maintenance_usd": 30.0,
            "delta.lease_usd": 20.0,
            "delta.insurance_usd": 10.0,
            # Efficiency relative signal = 40/25 − 1 = 0.60 (highest)
            "energy.mean_price_per_kwh": 0.16,        # price: 0.16/0.15 − 1 = 0.067
            "energy.cohort_mean_price_per_kwh": 0.15,
            "energy.public_share": 0.31,              # public: 0.31/0.30 − 1 = 0.033
            "energy.cohort_public_share": 0.30,
            "energy.vehicle_kwh_per_100mi": 40.0,     # efficiency: 40/25 − 1 = 0.60 ← wins
            "energy.cohort_kwh_per_100mi": 25.0,
        },
    )
    result = rules_fallback(evidence)
    assert result["cause"] == "efficiency", (
        "With kWh/100mi relative signal highest (0.60 > 0.067 price, > 0.033 public), "
        "fallback must select cause='efficiency' (decisions item 1 energy sub-cause rule). "
        "C3-5 (always 'charging_rate') fails this assertion."
    )
    assert result["action"] == "driver_coaching", (
        "First allowed action for efficiency is 'driver_coaching' (decisions item 1)"
    )


def test_fallback_maintenance_repeat_repair_when_count_ge_2():
    """Fallback: maintenance largest dollar delta + max_repeat_count >= 2 → exactly repeat_repair.

    Cycle 3 W1: the only maintenance test asserted cause in {repeat_repair, unclear},
    so C3-6 (always unclear) survived.  This test asserts exactly repeat_repair.
    """
    from agents.fi_cost.fallback import rules_fallback  # noqa: F401

    evidence = dict(
        _VALID_EVIDENCE,
        **{
            "delta.energy_usd": 30.0,
            "delta.maintenance_usd": 600.0,   # largest
            "delta.lease_usd": 20.0,
            "delta.insurance_usd": 10.0,
            "maintenance.max_repeat_count": 2,  # >= 2 → exactly repeat_repair
        },
    )
    result = rules_fallback(evidence)
    assert result["cause"] == "repeat_repair", (
        "maintenance largest dollar delta + max_repeat_count=2 must give "
        "cause='repeat_repair' (decisions item 1: 'maintenance → repeat_repair "
        "if a service type repeats').  C3-6 (always 'unclear') fails this assertion."
    )
    assert result["action"] == "schedule_inspection", (
        "First allowed action for repeat_repair is 'schedule_inspection' (decisions item 1)"
    )


def test_fallback_maintenance_unclear_when_count_lt_2():
    """Fallback: maintenance largest dollar delta + max_repeat_count < 2 → unclear, no_action.

    Cycle 3 W1: complement of the count >= 2 test.
    """
    from agents.fi_cost.fallback import rules_fallback  # noqa: F401

    evidence = dict(
        _VALID_EVIDENCE,
        **{
            "delta.energy_usd": 30.0,
            "delta.maintenance_usd": 600.0,   # largest
            "delta.lease_usd": 20.0,
            "delta.insurance_usd": 10.0,
            "maintenance.max_repeat_count": 1,  # < 2 → unclear
        },
    )
    result = rules_fallback(evidence)
    assert result["cause"] == "unclear", (
        "maintenance largest dollar delta + max_repeat_count=1 must give "
        "cause='unclear' (decisions item 1: 'maintenance → repeat_repair if "
        "a service type repeats, else unclear')"
    )
    assert result["action"] == "no_action", (
        "The only allowed action for 'unclear' is 'no_action' (decisions item 1)"
    )


def test_fallback_lease_unclear_when_miles_at_or_above_median():
    """Fallback: lease largest dollar delta + miles >= fleet_median → unclear.

    Cycle 3 W1: the only lease test used miles < median, so a fallback that
    ignores miles (always low_utilisation) survived.  This test asserts that
    miles at or above the fleet median gives unclear, not low_utilisation.
    """
    from agents.fi_cost.fallback import rules_fallback  # noqa: F401

    evidence = dict(
        _VALID_EVIDENCE,
        **{
            "delta.energy_usd": 20.0,
            "delta.maintenance_usd": 30.0,
            "delta.lease_usd": 500.0,         # largest
            "delta.insurance_usd": 100.0,
            "cost.miles": 1000.0,              # at fleet median → NOT below → unclear
            "cost.fleet_median_miles": 1000.0,
        },
    )
    result = rules_fallback(evidence)
    assert result["cause"] == "unclear", (
        "lease largest dollar delta + miles >= fleet_median must give cause='unclear' "
        "(decisions item 1: 'lease/insurance → low_utilisation if miles < median, "
        "else unclear').  A miles-ignoring fallback (always low_utilisation) fails this."
    )
    assert result["action"] == "no_action", (
        "The only allowed action for 'unclear' is 'no_action' (decisions item 1)"
    )


# ---------------------------------------------------------------------------
# W2 (cycle 3) — Dollar deltas and cost.energy catalogue mapping
# ---------------------------------------------------------------------------


def test_schedule_inspection_uses_dollar_delta_not_per_mile():
    """schedule_inspection saving uses delta.maintenance_usd ($), not delta.maintenance ($/mi).

    Cycle 3 W2: ADP emits delta_<cat>_cohort in $/mi (F2.2 notes item 1).
    AVX's evidence step computes delta.*_usd = delta_cohort × miles
    (decisions cycle 3 item 1).  D7's formulas read delta.*_usd.

    This test sets:
      delta.maintenance = 0.06 $/mi  (per-mile)
      delta.maintenance_usd = 48.0   (= 0.06 × 800; dollar)
    Correct formula: cohort_baseline = 200.0 − 48.0 = 152.0, saving = 48.0
    Wrong formula ($/mi):  cohort_baseline = 200.0 − 0.06 = 199.94, saving = 0.06
    """
    from agents.fi_cost.diagnose import run_cost_diagnosis  # noqa: F401

    evidence = dict(
        _VALID_EVIDENCE,
        **{
            "delta.maintenance": 0.06,        # $/mi from ADP view
            "delta.maintenance_usd": 48.0,    # dollar: 0.06 × 800
            "cost.maintenance": 200.0,
            "cost.miles": 800.0,
        },
    )
    reply = {
        "cause": "repeat_repair",
        "action": "schedule_inspection",
        "evidence_refs": [],
        "headline": "Maintenance above cohort",
        "detail": "More maintenance events than similar vehicles.",
        "confidence": "medium",
    }
    finding = run_cost_diagnosis(evidence, reply)
    saving = finding.get("recommended_action", {}).get("estimated_value_usd", 0.0)
    assert saving > 40.0, (
        f"schedule_inspection saving should use delta.maintenance_usd=48.0 → saving≈48.0; "
        f"got {saving}.  A formula using $/mi delta.maintenance=0.06 gives saving≈0.06."
    )


def test_driver_coaching_uses_cost_energy_not_energy_public_plus_depot():
    """driver_coaching formula reads cost.energy (monthly USD), not cost.energy_public + cost.energy_depot.

    Cycle 3 W2: decisions cycle 3 item 1 — cost.energy is the view's energy_cost
    column (monthly USD).  cost.energy_public and cost.energy_depot are not in the
    evidence catalogue.  An implementation reading them raises KeyError when absent.
    """
    from agents.fi_cost.diagnose import run_cost_diagnosis  # noqa: F401

    # Provide cost.energy; deliberately exclude cost.energy_public and cost.energy_depot
    evidence = {k: v for k, v in _VALID_EVIDENCE.items()
                if k not in ("cost.energy_public", "cost.energy_depot")}
    evidence["cost.energy"] = 280.0   # present; must be the formula's input
    reply = {
        "cause": "efficiency",
        "action": "driver_coaching",
        "evidence_refs": [],
        "headline": "Efficiency below cohort",
        "detail": "kWh per 100 mi above the fleet-model cohort.",
        "confidence": "medium",
    }
    finding = run_cost_diagnosis(evidence, reply)
    # Must not raise KeyError; recommended_action must carry estimated_value_usd
    assert "estimated_value_usd" in finding.get("recommended_action", {}), (
        "driver_coaching must succeed when cost.energy is present and "
        "cost.energy_public / cost.energy_depot are absent "
        "(decisions cycle 3 item 1: formula reads cost.energy, not _public + _depot)"
    )


# ---------------------------------------------------------------------------
# C3 — Retry driven through process_vehicle with exact call-count assertions
# ---------------------------------------------------------------------------


def test_invalid_twice_yields_fallback_after_exactly_two_calls():
    """process_vehicle retries exactly once; after two invalid replies → rules fallback.

    C3 fix: the 'at most one retry' bound is enforced.  M5 allows 4 retries (5 Bedrock
    calls per vehicle); a bound of 0 retries also survived the old suite.  This test
    asserts len(calls) == 2 exactly.

    Fix Group 3 W1: assert params["cause"] == "repeat_repair" (maintenance largest,
    max_repeat_count=2) instead of the old != "bad_cause..." check.  The old check
    passed even when the fallback used any valid cause.  C3-6 (always "unclear")
    returns cause="unclear" here and must fail.

    Asserts:
      - exactly 2 Bedrock calls (one attempt + one retry);
      - exactly 1 write;
      - confidence == 'low';
      - evidence entry kind='cost.diagnosis_source', value='rules';
      - recommended_action.parameters.cause == 'repeat_repair'.
    """
    from agents.fi_cost.agent import process_vehicle  # noqa: F401

    calls: list = []
    writes: list = []
    bedrock_stub = _make_always_invalid_stub(calls)

    evidence = dict(
        _VALID_EVIDENCE,
        **{
            "delta.maintenance_usd": 600.0,
            "delta.energy_usd": 100.0,
            "delta.lease_usd": 50.0,
            "delta.insurance_usd": 30.0,
            "maintenance.max_repeat_count": 2,  # fallback → exactly repeat_repair
        },
    )

    process_vehicle(
        evidence=evidence,
        live_finding=None,
        bedrock_client=bedrock_stub,
        write_finding=lambda f, **kw: writes.append(f),
    )

    assert len(calls) == 2, (
        f"process_vehicle must make exactly 2 Bedrock calls (attempt + one retry); "
        f"got {len(calls)}.  "
        f"M5 (retry bound 1→4) would produce 5 calls and fail here."
    )
    assert len(writes) == 1, "Exactly one Finding must be written (rules fallback)"

    finding = writes[0]
    assert finding.get("confidence") == "low", (
        "Rules fallback must set confidence='low'"
    )

    evidence_entries = finding.get("evidence", [])
    diag_sources = [
        e.get("value")
        for e in evidence_entries
        if e.get("kind") == "cost.diagnosis_source"
    ]
    assert "rules" in diag_sources, (
        "Rules fallback must include evidence entry kind='cost.diagnosis_source', value='rules'"
    )

    # Fix Group 3 W1: assert params["cause"] == "repeat_repair" (maintenance largest,
    # max_repeat_count=2).  The old assertion (!= "bad_cause...") passed even when the
    # fallback used an arbitrary cause.  C3-6 returns "unclear" here and must fail.
    params = finding.get("recommended_action", {}).get("parameters", {})
    assert params.get("cause") == "repeat_repair", (
        "With maintenance dollar delta largest and max_repeat_count=2, the rules "
        "fallback must set cause='repeat_repair' in recommended_action.parameters.cause "
        "(decisions item 1).  C3-6 (always 'unclear') fails this assertion."
    )


def test_valid_on_retry_uses_model_diagnosis():
    """process_vehicle accepts a valid reply on the second attempt.

    C3 fix: call 1 fails validation, call 2 succeeds.  Assert exactly 2 calls
    and that the result is the model's diagnosis (not the rules fallback).
    """
    from agents.fi_cost.agent import process_vehicle  # noqa: F401

    calls: list = []
    writes: list = []
    bedrock_stub = _make_valid_on_retry_stub(calls)

    process_vehicle(
        evidence=_VALID_EVIDENCE,
        live_finding=None,
        bedrock_client=bedrock_stub,
        write_finding=lambda f, **kw: writes.append(f),
    )

    assert len(calls) == 2, (
        f"process_vehicle must make exactly 2 Bedrock calls (failed attempt + retry); "
        f"got {len(calls)}"
    )
    assert len(writes) == 1, "Exactly one Finding must be written"

    finding = writes[0]
    # Should NOT be the rules fallback
    assert finding.get("confidence") != "low", (
        "Valid-on-retry path must not produce confidence='low' (that is rules-fallback only)"
    )
    evidence_entries = finding.get("evidence", [])
    diag_sources = [
        e.get("value")
        for e in evidence_entries
        if e.get("kind") == "cost.diagnosis_source"
    ]
    assert "rules" not in diag_sources, (
        "Valid-on-retry path must not include a 'cost.diagnosis_source: rules' entry"
    )
    # The model's valid cause must be present in recommended_action.parameters
    params = finding.get("recommended_action", {}).get("parameters", {})
    assert params.get("cause") == "charging_rate", (
        "The valid retry's cause='charging_rate' must reach "
        "recommended_action.parameters.cause in the Finding (D9)"
    )
