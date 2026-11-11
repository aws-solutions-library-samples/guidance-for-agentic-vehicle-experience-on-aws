"""Review g4 cycle 1 W1/W2: every server-side reply rule, and the clock before a retry."""
from __future__ import annotations

import pytest

from agents.fi_fleet_health import llm
from agents.fi_fleet_health.tests.test_model_path import (
    _FakeBedrock, _config, _evidence, _row, _run, _soon, _due_pm)
from agents.fi_fleet_health.validator import ServicePlanValidator, ValidationError

_GOOD = {"dealer_id": "d1", "urgency": "this_month", "defer_need_ids": [],
         "detail": "Book it.", "evidence_refs": ["health.need"], "confidence": "medium"}


def _v():
    return ServicePlanValidator(candidate_dealer_ids=["d1"], severity="soon",
                                non_deferrable_need_ids=["fault:A"],
                                deferrable_need_ids=["pm:B", "pm:C"])


def test_the_good_reply_passes():
    assert _v().validate_tool_input(dict(_GOOD), evidence_kinds=["health.need"]) == _GOOD


@pytest.mark.parametrize("field,value", [
    ("detail", "x" * 601),
    ("detail", "Book it.\x07"),
    ("detail", "   "),
    ("evidence_refs", ["health.position"]),
    ("evidence_refs", ["health.need"] * 11),
    ("confidence", "certain"),
    ("defer_need_ids", ["pm:B", "pm:B"]),
    ("defer_need_ids", ["pm:Z"]),
    ("urgency", "someday"),
])
def test_each_rule_rejects_one_bad_field(field, value):
    with pytest.raises(ValidationError):
        _v().validate_tool_input({**_GOOD, field: value}, evidence_kinds=["health.need"])


def test_detail_of_exactly_600_characters_passes():
    _v().validate_tool_input({**_GOOD, "detail": "x" * 600}, evidence_kinds=["health.need"])


def test_a_bad_field_through_the_loop_falls_back_after_two_calls():
    fake = _FakeBedrock(replies=[
        lambda req: {**_FakeBedrock.valid_plan(req), "detail": "y" * 601}] * 2)
    _, writes = _run([_row(faults=[_soon()], pm=[_due_pm()])], bedrock=fake, config=_config())
    (finding, _, _), = writes.service()
    assert _evidence(finding, "health.diagnosis_source") == ["rules"]
    assert len(fake.converse_calls) == 2


def test_clock_is_checked_again_before_the_retry():
    """The first call starts with time left; the retry finds too little and never starts.
    Mutation: drop the clock check in converse_tool → a second call, fails."""
    ticks = iter([200.0, 200.0] + [100.0] * 10)
    fake = _FakeBedrock(replies=[lambda req: {**_FakeBedrock.valid_plan(req),
                                              "dealer_id": "nope"}])
    stats, writes = _run([_row(faults=[_soon()])], bedrock=fake, config=_config(),
                         remaining=lambda: next(ticks))
    assert len(fake.converse_calls) == 1
    (finding, _, _), = writes.service()
    assert _evidence(finding, "health.diagnosis_source") == ["rules"]
    assert llm.MODEL_CALL_RESERVE_S == 150.0



# ============================================================================
# Review g4 cycle 2: dealer ids not city names (W1), which policy blocked (W2), the literal
# retry hint (W3), the whole detail reaches the guardrail (W5)
# ============================================================================

import logging  # noqa: E402

_DIRECTIVE = ("Name each dealer only by its exact dealer_id value (for example "
              "dealer-seattle); do not turn a dealer_id into a city or place name, and do not "
              "add any address, phone number, email or person's name.")

#: A block on a city name (the 2026-10-10 recall-run failure), a topic, a custom word, and an
#: entity the guardrail only anonymised (not blocked, so not listed).
_BLOCKED = {"action": "GUARDRAIL_INTERVENED", "assessments": [{
    "topicPolicy": {"topics": [{"name": "advice-outside-vehicle-cost", "type": "DENY",
                                "action": "BLOCKED"}]},
    "sensitiveInformationPolicy": {"piiEntities": [
        {"type": "ADDRESS", "match": "Seattle", "action": "BLOCKED"},
        {"type": "NAME", "match": "Jane Doe", "action": "ANONYMIZED"}]},
    "wordPolicy": {"customWords": [{"match": "Jane", "action": "BLOCKED"}]},
}]}
_BLOCKED_LINE = ('fi_fleet_health.plan_service: guardrail INTERVENED; policies_blocked='
                 '[{"name": "advice-outside-vehicle-cost", "policy": "topic"}, '
                 '{"name": "ADDRESS", "policy": "pii"}, {"name": "custom", "policy": "word"}]')


def _llm_warnings(caplog):
    return [r.getMessage() for r in caplog.records
            if r.name == "agents.fi_fleet_health.llm" and r.levelno == logging.WARNING]


def test_every_call_tells_the_model_to_use_exact_dealer_ids():
    fake = _FakeBedrock()
    _run([_row(faults=[_soon()])], bedrock=fake, config=_config())
    (call,) = fake.converse_calls
    assert _DIRECTIVE in call["system"][0]["text"]


def test_a_block_logs_the_call_site_and_policies_never_the_text(caplog):
    detail = "Book at the Seattle rooftop and ask Jane Doe for the keys."
    fake = _FakeBedrock(replies=[lambda req: {**_FakeBedrock.valid_plan(req),
                                              "detail": detail}] * 2,
                        guard=[_BLOCKED, _BLOCKED])
    with caplog.at_level(logging.DEBUG):
        _run([_row(faults=[_soon()])], bedrock=fake, config=_config())
    assert _llm_warnings(caplog) == [_BLOCKED_LINE, _BLOCKED_LINE]
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert "Seattle" not in logged and "Jane" not in logged


@pytest.mark.parametrize("verdict,line", [
    (RuntimeError("throttled"), "fi_fleet_health.plan_service: guardrail check failed "
                                "(RuntimeError)"),
    ({"action": "SOMETHING_NEW"}, "fi_fleet_health.plan_service: guardrail check returned "
                                  "no verdict"),
])
def test_a_failed_guardrail_check_logs_the_call_site(caplog, verdict, line):
    fake = _FakeBedrock(guard=[verdict, verdict])
    with caplog.at_level(logging.WARNING):
        _run([_row(faults=[_soon()])], bedrock=fake, config=_config())
    assert _llm_warnings(caplog) == [line, line]


def test_retry_after_a_block_asks_for_dealer_ids_not_places():
    fake = _FakeBedrock(guard=["GUARDRAIL_INTERVENED"])
    _run([_row(faults=[_soon()])], bedrock=fake, config=_config())
    retry = fake.converse_calls[1]["messages"][0]["content"][0]["text"]
    assert retry.startswith(
        "Previous attempt failed: the content guardrail blocked the text; rewrite it as plain "
        "booking logistics that names each dealer only by its exact dealer_id, with no city or "
        "place names, addresses, phone numbers, emails or people's names.\n\n<data>")


def test_guardrail_sees_the_whole_600_character_detail():
    detail = ("Book the first open slot at the chosen rooftop next week. " * 11)[:600]
    assert len(detail) == 600
    fake = _FakeBedrock(replies=[lambda req: {**_FakeBedrock.valid_plan(req),
                                              "detail": detail}])
    _, writes = _run([_row(faults=[_soon()])], bedrock=fake, config=_config())
    (g,) = fake.guard_calls
    assert g["content"] == [{"text": {"text": detail}}]
    (finding, _, _), = writes.service()
    assert finding["detail"] == detail
