"""Fix Group G3-1 (Group 3 review cycle 1, agent area): tests that pin each fix.

Spec: .kiro/specs/2026-10-02-fi-cost-stage-b/ (decisions.md 2026-10-05 "Group 3 review
cycle 1", items 1-4 and 7).  Each test names the mutant it catches.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from agents.fi_cost.tests.test_guardrail import (
    _EVIDENCE,
    _guardrail_none,
    _valid_converse_reply,
)

_GOOD_REPLY = _valid_converse_reply()["output"]["message"]["content"][0]["toolUse"]["input"]


def _written(write_mock: MagicMock) -> dict:
    assert write_mock.call_count == 1, f"expected one write, got {write_mock.call_count}"
    args, kwargs = write_mock.call_args
    return kwargs.get("finding", args[0] if args else None)


def _sources(finding: dict) -> list:
    return [e.get("value") for e in finding.get("evidence", []) if e.get("kind") == "cost.diagnosis_source"]


# ---- item 1: the model cannot write into the Finding ------------------------------------

def test_reply_with_extra_key_is_rejected():
    """A1: a reply carrying an 'evidence' list (or any key outside the six) fails validation."""
    from agents.fi_cost.validate import validate_tool_input

    ok, msg = validate_tool_input({**_GOOD_REPLY, "evidence": [{"kind": "x", "vin": "OTHER"}]}, _EVIDENCE)
    assert ok is False and "extra keys" in msg


def test_model_may_not_claim_low_confidence():
    """A2: 'low' is the rules fallback's; a model reply saying 'low' fails validation, so it
    can never be recorded as diagnosis_source 'rules'."""
    from agents.fi_cost.validate import validate_tool_input

    ok, _ = validate_tool_input({**_GOOD_REPLY, "confidence": "low"}, _EVIDENCE)
    assert ok is False
    ok2, _ = validate_tool_input({**_GOOD_REPLY, "confidence": "certain"}, _EVIDENCE)
    assert ok2 is False


@pytest.mark.parametrize("field,limit", [("headline", 120), ("detail", 600)])
def test_narration_length_limits(field, limit):
    from agents.fi_cost.validate import validate_tool_input

    assert validate_tool_input({**_GOOD_REPLY, field: "x" * limit}, _EVIDENCE)[0] is True
    assert validate_tool_input({**_GOOD_REPLY, field: "x" * (limit + 1)}, _EVIDENCE)[0] is False


def test_run_cost_diagnosis_never_copies_model_evidence():
    """Even called directly with a reply that carries evidence, the Finding's evidence comes
    only from the bundle: no entry names another VIN."""
    from agents.fi_cost.diagnose import run_cost_diagnosis

    reply = {**_GOOD_REPLY, "evidence": [{"kind": "cost.cost_per_mile", "value": 9.99, "vin": "MRDOTHERVIN000001"}]}
    finding = run_cost_diagnosis(_EVIDENCE, reply, diagnosis_source="model")
    assert {e.get("vin") for e in finding["evidence"]} == {_EVIDENCE["vin"]}
    assert 9.99 not in [e.get("value") for e in finding["evidence"]]


# ---- item 3: no_action writes no recommended_action -------------------------------------

def test_no_action_has_no_recommended_action():
    """A3: an unclear / no_action diagnosis is informational."""
    from agents.fi_cost.diagnose import run_cost_diagnosis

    reply = {**_GOOD_REPLY, "cause": "unclear", "action": "no_action", "evidence_refs": []}
    finding = run_cost_diagnosis(_EVIDENCE, reply, diagnosis_source="model")
    assert "recommended_action" not in finding


# ---- item 4: a Bedrock error is a failed attempt, never an abort -------------------------

def test_converse_errors_fall_back_to_rules():
    """A4: converse raising on both attempts → one retry, then a rules Finding is written."""
    from agents.fi_cost.agent import process_vehicle

    stub = MagicMock()
    stub.converse.side_effect = RuntimeError("ThrottlingException")
    stub.apply_guardrail.return_value = _guardrail_none()
    write = MagicMock()
    stats = process_vehicle(_EVIDENCE, None, stub, write)
    assert stub.converse.call_count == 2
    finding = _written(write)
    assert _sources(finding) == ["rules"]
    assert stats["written"] == 1


def test_guardrail_error_falls_back_to_rules():
    from agents.fi_cost.agent import process_vehicle

    stub = MagicMock()
    stub.converse.return_value = _valid_converse_reply()
    stub.apply_guardrail.side_effect = RuntimeError("ServiceUnavailable")
    write = MagicMock()
    process_vehicle(_EVIDENCE, None, stub, write)
    assert stub.converse.call_count == 2
    assert _sources(_written(write)) == ["rules"]


# ---- item 7 (tokens and cost) -------------------------------------------------------------

def test_tokens_of_failed_attempts_are_counted_and_priced():
    """A5: two replies that fail validation (80 in / 40 out each) then the rules fallback:
    tokens_used = 240 and cost_usd = (160 × 3 + 80 × 15) / 1e6."""
    from agents.fi_cost.agent import process_vehicle

    bad = _valid_converse_reply()
    bad["output"]["message"]["content"][0]["toolUse"]["input"]["cause"] = "not_a_cause"
    stub = MagicMock()
    stub.converse.return_value = bad
    stub.apply_guardrail.return_value = _guardrail_none()
    write = MagicMock()
    stats = process_vehicle(_EVIDENCE, None, stub, write)
    finding = _written(write)
    assert _sources(finding) == ["rules"]
    assert stats["tokens_used"] == 240
    assert finding["tokens_used"] == 240
    assert finding["cost_usd"] == pytest.approx((160 * 3 + 80 * 15) / 1_000_000)


def test_model_path_records_model_source_and_tokens():
    from agents.fi_cost.agent import process_vehicle

    stub = MagicMock()
    stub.converse.return_value = _valid_converse_reply()
    stub.apply_guardrail.return_value = _guardrail_none()
    write = MagicMock()
    stats = process_vehicle(_EVIDENCE, None, stub, write)
    finding = _written(write)
    assert _sources(finding) == ["model"]
    assert stats["tokens_used"] == 120
    assert finding["cost_usd"] == pytest.approx((80 * 3 + 40 * 15) / 1_000_000)


# ---- repeat_repair needs maintenance above the cohort (warning W1) -----------------------

def test_repeat_repair_needs_maintenance_above_cohort():
    """A6: two repeats but maintenance below the cohort → repeat_repair is inconsistent."""
    from agents.fi_cost.validate import validate_tool_input

    ev = {**_EVIDENCE, "maintenance.max_repeat_count": 4, "delta.maintenance_usd": -10.0}
    reply = {**_GOOD_REPLY, "cause": "repeat_repair", "action": "reassign_or_remarket",
             "evidence_refs": ["maintenance.max_repeat_count"]}
    assert validate_tool_input(reply, ev)[0] is False
    ev_ok = {**ev, "delta.maintenance_usd": 48.0}
    assert validate_tool_input(reply, ev_ok)[0] is True
