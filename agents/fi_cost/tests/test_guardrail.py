"""
Security Fix Group 1 — AVX W1 (decisions security review cycle 1, item 1):
ApplyGuardrail on headline and detail of every model reply.

Decisions item 1:
  - apply_guardrail runs on headline and detail of every model reply.
  - GUARDRAIL_INTERVENED on either field is handled like a rejected reply:
    one retry, then the rules fallback with fixed narration.
  - Model text that the guardrail blocked is never written.

Three red tests (Group 3 makes them pass):
  T1 — apply_guardrail is called with source='OUTPUT' and both headline/detail
       texts appear in the content (catches the "skip the call" mutation: the
       mutation leaves guardrail_spy.called=False, failing the first assertion).
  T2 — GUARDRAIL_INTERVENED on the first attempt triggers exactly one retry:
       converse is called exactly twice when the first reply is intercepted
       and the second is clean; apply_guardrail is also called twice.
  T3 — Intervention on both attempts gives the rules fallback with fixed narration;
       the blocked PII text appears nowhere in the written Finding (catches the
       "write blocked text anyway" mutation: the finding would contain _HEADLINE_PII
       or _DETAIL_PII, failing the absence assertions).

Mutations verified in the control (/tmp/2026-10-02-fi-cost-stage-b-avx-sf1/):
  M_skip_guardrail — delete _apply_guardrail call from process_vehicle:
    T1 fails (guardrail_spy.called=False), T2 fails (converse called once),
    T3 fails (finding contains PII).
  M_write_blocked — call apply_guardrail but use the blocked model reply anyway:
    T2 fails (converse called once — first attempt always succeeds),
    T3 fails (finding contains PII).

All tests are xfail(strict=True) — Group 3 makes them pass.
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

# ---------------------------------------------------------------------------
# Evidence shared by all guardrail tests
# Matches the shape used in test_validator.py (_VALID_EVIDENCE):
# charging_rate is consistent (mean_price > cohort, peak_share > cohort_peak_share)
# and delta.energy_usd is the largest dollar delta (for the fallback coverage path).
# ---------------------------------------------------------------------------

_EVIDENCE = {
    "vin": "MRD-TEST-VIN-GRD-001",
    "fleet_id": "fleet-meridian-grd",
    "year_month": "2026-09",
    "cost.month": "2026-09",
    "ratio_cohort": 1.7,
    "ratio_own": 1.4,
    "severity": "soon",
    "energy.public_and_peak_kwh": 400.0,
    "energy.mean_public_peak_price_per_kwh": 0.38,
    "energy.depot_off_peak_price_per_kwh": 0.09,
    "energy.mean_price_per_kwh": 0.35,
    "energy.cohort_mean_price_per_kwh": 0.15,
    "energy.public_share": 0.75,
    "energy.cohort_public_share": 0.30,
    "energy.peak_share": 0.40,
    "energy.cohort_peak_share": 0.15,
    "energy.dc_fast_share": 0.60,
    "energy.cohort_dc_fast_share": 0.25,
    "energy.vehicle_kwh_per_100mi": 35.0,
    "energy.cohort_kwh_per_100mi": 25.0,
    "cost.maintenance": 200.0,
    "cost.lease": 600.0,
    "cost.insurance": 150.0,
    "cost.miles": 800.0,
    "cost.fleet_median_miles": 1000.0,
    "cost.cohort_baseline": 0.28,
    "cost.energy": 280.0,
    "delta.maintenance": 0.06,
    "delta.energy": 0.375,
    "delta.lease": 0.025,
    "delta.insurance": 0.0125,
    "delta.maintenance_usd": 48.0,
    "delta.energy_usd": 300.0,    # largest → fallback picks energy sub-cause
    "delta.lease_usd": 20.0,
    "delta.insurance_usd": 10.0,
    "maintenance.max_repeat_count": 0,
}

# PII-shaped headline/detail: must NEVER appear in the written Finding when blocked.
# Synthetic values only; not a real person or phone number.
_HEADLINE_PII = "Driver Jane Doe +1-555-0199 at risk: high charging costs"
_DETAIL_PII = (
    "Phone: 555-0199. Email: jane.doe@meridian.example. "
    "Energy cost 30% above cohort median."
)

# Clean (non-PII) texts used for the "clean second attempt" in T2.
_CLEAN_HEADLINE = "High charging costs detected at public DC-fast stations"
_CLEAN_DETAIL = "Vehicle charges at public stations above fleet baseline; shift to depot off-peak."


# ---------------------------------------------------------------------------
# Stubs
# ---------------------------------------------------------------------------


def _valid_converse_reply(headline: str = _CLEAN_HEADLINE, detail: str = _CLEAN_DETAIL) -> dict:
    """A Bedrock converse response with the given headline/detail (passes validation)."""
    return {
        "output": {"message": {"content": [{"toolUse": {
            "toolUseId": "t-grd-test",
            "name": "record_diagnosis",
            "input": {
                "cause": "charging_rate",
                "action": "shift_to_depot_off_peak",
                "evidence_refs": ["energy.public_and_peak_kwh"],
                "headline": headline,
                "detail": detail,
                "confidence": "high",
            },
        }}]}},
        "usage": {"inputTokens": 80, "outputTokens": 40},
    }


def _guardrail_none() -> dict:
    """apply_guardrail response when no intervention."""
    return {"action": "NONE", "outputs": [{"text": "Passed.", "type": "TEXT"}]}


def _guardrail_intervened() -> dict:
    """apply_guardrail response when the guardrail blocks the output."""
    return {
        "action": "GUARDRAIL_INTERVENED",
        "outputs": [{"text": "I cannot provide that information.", "type": "TEXT"}],
    }


def _collect_texts_from_guardrail_calls(spy) -> list[str]:
    """Extract every text string passed to apply_guardrail content items."""
    texts: list[str] = []
    for c in spy.call_args_list:
        for item in c.kwargs.get("content", []):
            t = item.get("text", {})
            if isinstance(t, dict):
                texts.append(t.get("text", ""))
            elif isinstance(t, str):
                texts.append(t)
    return texts


# ---------------------------------------------------------------------------
# T1: apply_guardrail is called with source='OUTPUT' and both texts in content
#
# Mutation M_skip_guardrail (delete _apply_guardrail call): guardrail_spy.called
# is False → "apply_guardrail must be called" assertion fails.
# ---------------------------------------------------------------------------


def test_apply_guardrail_receives_headline_and_detail_texts():
    """apply_guardrail is called with source='OUTPUT'; content includes both
    headline and detail texts of the model reply.

    Spec D6 step 5 and decisions security review cycle 1 item 1:
    the guardrail runs on both narration fields before write_finding().
    The mutation that deletes the apply_guardrail call leaves
    guardrail_spy.called=False and fails this test.
    """
    from agents.fi_cost.agent import process_vehicle

    guardrail_spy = MagicMock(return_value=_guardrail_none())
    stub = MagicMock()
    stub.converse.return_value = _valid_converse_reply(_HEADLINE_PII, _DETAIL_PII)
    stub.apply_guardrail = guardrail_spy

    process_vehicle(_EVIDENCE, None, stub, MagicMock())

    assert guardrail_spy.called, (
        "apply_guardrail must be called on every model reply; "
        "M_skip_guardrail (delete the call) is caught here"
    )

    sources = [c.kwargs.get("source") for c in guardrail_spy.call_args_list]
    assert "OUTPUT" in sources, (
        "apply_guardrail must use source='OUTPUT' for model output (spec D6 step 5)"
    )

    texts = _collect_texts_from_guardrail_calls(guardrail_spy)
    assert _HEADLINE_PII in texts, (
        f"headline text must be in apply_guardrail content; texts seen: {texts}"
    )
    assert _DETAIL_PII in texts, (
        f"detail text must be in apply_guardrail content; texts seen: {texts}"
    )


# ---------------------------------------------------------------------------
# T2: GUARDRAIL_INTERVENED on first reply triggers exactly one retry
#
# Mutation M_skip_guardrail: the first attempt always succeeds (no guardrail
# check) → converse called once → call_count==1 != 2 fails.
# Mutation M_write_blocked: first attempt always succeeds → converse called
# once → same failure.
# ---------------------------------------------------------------------------


def test_guardrail_intervention_on_first_reply_triggers_exactly_one_retry():
    """GUARDRAIL_INTERVENED on the first attempt → converse called exactly twice
    (one retry); the second attempt clears the guardrail and the finding is written.

    Decisions item 1: 'GUARDRAIL_INTERVENED on either is handled like a rejected
    reply: one retry'. The existing retry counter covers validation failures;
    this test verifies guardrail intervention also consumes a retry slot.
    """
    from agents.fi_cost.agent import process_vehicle

    # First apply_guardrail call → INTERVENED; second → NONE (retry succeeds)
    guardrail_spy = MagicMock(
        side_effect=[_guardrail_intervened(), _guardrail_none()]
    )
    stub = MagicMock()
    stub.converse.return_value = _valid_converse_reply(_CLEAN_HEADLINE, _CLEAN_DETAIL)
    stub.apply_guardrail = guardrail_spy

    write_mock = MagicMock()
    process_vehicle(_EVIDENCE, None, stub, write_mock)

    assert stub.converse.call_count == 2, (
        f"converse must be called exactly twice (original + 1 retry after INTERVENED); "
        f"got {stub.converse.call_count}. "
        f"M_skip_guardrail (skip the check) makes the first attempt succeed immediately "
        f"→ converse called once → this assertion fails."
    )
    assert guardrail_spy.call_count == 2, (
        f"apply_guardrail must be called once per converse call (2 total); "
        f"got {guardrail_spy.call_count}"
    )
    assert write_mock.called, (
        "write_finding must be called after the second (clean) attempt succeeds"
    )


# ---------------------------------------------------------------------------
# T3: both attempts blocked → rules fallback; blocked text not in finding
#
# Mutation M_skip_guardrail: model reply used directly → finding contains PII
# → _HEADLINE_PII/DETAIL_PII absence assertions fail.
# Mutation M_write_blocked: blocked model reply written anyway → same failure.
# ---------------------------------------------------------------------------


def test_guardrail_intervention_on_both_attempts_gives_rules_fallback_without_blocked_text():
    """Intervention on both attempts → rules fallback with fixed narration; the
    blocked PII text appears nowhere in the written Finding.

    Decisions item 1: 'one retry, then the rules fallback with fixed narration.
    Model text that the guardrail blocked is never written.'

    The mutation that writes the blocked model reply directly causes the finding
    to contain _HEADLINE_PII or _DETAIL_PII, failing the absence assertions.
    """
    from agents.fi_cost.agent import process_vehicle

    # Both attempts blocked
    guardrail_spy = MagicMock(return_value=_guardrail_intervened())
    stub = MagicMock()
    stub.converse.return_value = _valid_converse_reply(_HEADLINE_PII, _DETAIL_PII)
    stub.apply_guardrail = guardrail_spy

    write_mock = MagicMock()
    process_vehicle(_EVIDENCE, None, stub, write_mock)

    assert write_mock.called, (
        "write_finding must still be called after guardrail blocks both attempts "
        "(the rules fallback writes a Finding regardless)"
    )

    finding = write_mock.call_args[0][0]
    finding_str = json.dumps(finding)

    assert _HEADLINE_PII not in finding_str, (
        "Blocked headline text must not appear in the written Finding; "
        "M_write_blocked (write model reply directly) is caught here"
    )
    assert _DETAIL_PII not in finding_str, (
        "Blocked detail text must not appear in the written Finding"
    )

    # Rules fallback markers: confidence='low' and/or cost.diagnosis_source='rules'
    has_low_confidence = finding.get("confidence") == "low"
    has_rules_source = any(
        e.get("kind") == "cost.diagnosis_source" and e.get("value") == "rules"
        for e in finding.get("evidence", [])
    )
    assert has_low_confidence or has_rules_source, (
        "After guardrail blocks both attempts, the rules fallback must be used "
        "(confidence='low' and/or cost.diagnosis_source='rules' in evidence); "
        "got confidence={!r}, evidence={!r}".format(
            finding.get("confidence"), finding.get("evidence")
        )
    )
