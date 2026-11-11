"""Review g4 cycle 1 W1/W2/W4: every investigation reply rule, the clock before call 2, and
the retry hint after a guardrail block."""
from __future__ import annotations

import copy

import pytest

from agents.fi_quality import llm as qllm
from agents.fi_quality.tests.test_model_path import (
    _BUNDLE, _FakeBedrock, _cfg, _investigate, _opts, _root)
from agents.fi_quality.validator import InvestigationValidator, ValidationError

_KINDS = qllm.EVIDENCE_KINDS


@pytest.mark.parametrize("field,value", [
    ("reasoning", "r" * 1201),
    ("reasoning", "ok\x00"),
    ("evidence_refs", ["quality.options"]),
    ("confidence", "sure"),
])
def test_root_cause_rule_rejects_one_bad_field(field, value):
    with pytest.raises(ValidationError):
        InvestigationValidator(_BUNDLE).validate_root_cause_input(
            {**_root(), field: value}, evidence_kinds=_KINDS)


def test_root_cause_reasoning_of_exactly_1200_passes():
    InvestigationValidator(_BUNDLE).validate_root_cause_input(
        {**_root(), "reasoning": "r" * 1200}, evidence_kinds=_KINDS)


@pytest.mark.parametrize("options", [
    [{"remedy_option_id": "opt-001", "tradeoffs": "t" * 601, "recommended": True},
     {"remedy_option_id": "opt-002", "tradeoffs": "t", "recommended": False}],
    [{"remedy_option_id": "opt-001", "tradeoffs": "t", "recommended": True},
     {"remedy_option_id": "opt-001", "tradeoffs": "t", "recommended": False}],
    # Exactly one real True, so only the strict-bool rule can reject the string "false"
    # (review mutant R10 survived the earlier "true"/False pair through the count check).
    [{"remedy_option_id": "opt-001", "tradeoffs": "t", "recommended": True},
     {"remedy_option_id": "opt-002", "tradeoffs": "t", "recommended": "false"}],
    [{"remedy_option_id": "opt-001", "tradeoffs": "t\x1b", "recommended": True},
     {"remedy_option_id": "opt-002", "tradeoffs": "t", "recommended": False}],
])
def test_design_option_rule_rejects_one_bad_option(options):
    with pytest.raises(ValidationError):
        InvestigationValidator(_BUNDLE).validate_design_options("fm-001", options)


def test_clock_is_checked_again_before_the_design_options_call():
    """Mutation: drop the clock check in converse_tool → a second call, fails."""
    ticks = iter([200.0, 200.0] + [10.0] * 10)
    fake = _FakeBedrock([_root(), _opts(("opt-002", True), ("opt-001", False))])
    out = _investigate(fake, remaining_seconds=lambda: next(ticks))
    assert len(fake.converse_calls) == 1
    assert out["diagnosis_source"] == "rules" and out["rules_reason"] == "fallback"


def test_retry_after_a_guardrail_block_asks_for_engineering_analysis():
    fake = _FakeBedrock([_root(), _root(), _opts(("opt-002", True), ("opt-001", False))],
                        guard=["GUARDRAIL_INTERVENED"])
    qllm.investigate(copy.deepcopy(_BUNDLE), bedrock_client=fake, config=_cfg())
    retry = fake.converse_calls[1]["messages"][0]["content"][0]["text"]
    assert retry.startswith("Previous attempt failed: the content guardrail blocked the text; "
                            + qllm.BLOCKED_HINT + ".")
    assert "booking" not in retry.split("<data>")[0]



# ============================================================================
# Review g4 cycle 2: exact values not place names (W1/S1), which policy blocked (W2), the
# literal retry hint (W3), the root cause's 10-ref cap (W4), the whole text reaches the
# guardrail (W6), the job forwards its clock (W7)
# ============================================================================

import logging  # noqa: E402

from agents.fi_quality.tests.test_model_path import _run_job  # noqa: E402

_ANCHOR = ("Refer to plants, supplier lots, candidates and campaigns only by their exact values "
           "from the data; do not expand a code into a city or place name, and do not name or "
           "judge any person or add any address, phone number or email.")
_BLOCKED = {"action": "GUARDRAIL_INTERVENED", "assessments": [{
    "sensitiveInformationPolicy": {"piiEntities": [
        {"type": "ADDRESS", "match": "Reno", "action": "BLOCKED"}]},
    "topicPolicy": {"topics": [{"name": "blame-of-a-person", "type": "DENY",
                                "action": "BLOCKED"}]},
}]}
_POLICIES = ('[{"name": "blame-of-a-person", "policy": "topic"}, '
             '{"name": "ADDRESS", "policy": "pii"}]')


def test_both_calls_tell_the_model_to_use_exact_values():
    fake = _FakeBedrock([_root(), _opts(("opt-002", True), ("opt-001", False))])
    _investigate(fake)
    assert len(fake.converse_calls) == 2
    for call in fake.converse_calls:
        assert _ANCHOR in call["system"][0]["text"]


def test_a_block_logs_each_call_site_and_policies_never_the_text(caplog):
    reno = _root(reasoning="The Reno plant staff caused it.")
    opts = _opts(("opt-002", True), ("opt-001", False))
    fake = _FakeBedrock([reno, _root(), opts, opts],
                        guard=[_BLOCKED, "NONE", _BLOCKED, "NONE", "NONE"])
    with caplog.at_level(logging.DEBUG):
        out = _investigate(fake)
    assert out["diagnosis_source"] == "model"
    warnings = [r.getMessage() for r in caplog.records
                if r.name == "agents.fi_fleet_health.llm" and r.levelno == logging.WARNING]
    assert warnings == [
        f"fi_quality.root_cause: guardrail INTERVENED; policies_blocked={_POLICIES}",
        f"fi_quality.design_options: guardrail INTERVENED; policies_blocked={_POLICIES}",
    ]
    assert "Reno" not in "\n".join(r.getMessage() for r in caplog.records)


def test_retry_after_a_block_asks_for_exact_values_not_places():
    fake = _FakeBedrock([_root(), _root(), _opts(("opt-002", True), ("opt-001", False))],
                        guard=["GUARDRAIL_INTERVENED"])
    qllm.investigate(copy.deepcopy(_BUNDLE), bedrock_client=fake, config=_cfg())
    retry = fake.converse_calls[1]["messages"][0]["content"][0]["text"]
    assert retry.startswith(
        "Previous attempt failed: the content guardrail blocked the text; rewrite it as plain "
        "engineering analysis of the evidence that refers to plants, lots and campaigns only "
        "by their exact values, with no place names, addresses, people's names or judgement "
        "of any person.\n\n<data>")


def test_root_cause_evidence_refs_are_capped_at_ten():
    v = InvestigationValidator(_BUNDLE)
    v.validate_root_cause_input({**_root(), "evidence_refs": ["quality.candidates"] * 10},
                                evidence_kinds=_KINDS)
    with pytest.raises(ValidationError):
        v.validate_root_cause_input({**_root(), "evidence_refs": ["quality.candidates"] * 11},
                                    evidence_kinds=_KINDS)


def test_guardrail_sees_the_whole_reasoning_and_tradeoffs():
    reasoning = ("Lot L of supplier B explains the excess over plant CGA. " * 25)[:1200]
    tradeoffs = ("Field fix is faster; the redesign removes the cause. " * 15)[:600]
    assert (len(reasoning), len(tradeoffs)) == (1200, 600)
    opts = {"options": [
        {"remedy_option_id": "opt-002", "tradeoffs": tradeoffs, "recommended": True},
        {"remedy_option_id": "opt-001", "tradeoffs": "Short.", "recommended": False}]}
    fake = _FakeBedrock([_root(reasoning=reasoning), opts])
    out = _investigate(fake)
    assert out["diagnosis_source"] == "model"
    texts = [[c["text"]["text"] for c in g["content"]] for g in fake.guard_calls]
    assert texts == [[reasoning], [tradeoffs, "Short."]]


def test_the_job_passes_its_clock_to_the_model_calls(monkeypatch):
    """A job clock below the reserve: no Converse call, the rules result (no_budget)."""
    fake = _FakeBedrock([_root(), _opts(("opt-002", True), ("opt-001", False))])
    usage: dict = {}
    (finding,), row = _run_job(monkeypatch, fake, usage_out=usage,
                               remaining_seconds=lambda: 10.0)
    assert fake.converse_calls == []
    assert usage["rules_reason"] == "no_budget" and usage["diagnosis_source"] == "rules"
    assert row["status"]["S"] == "completed"
