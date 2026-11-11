"""Driver-coaching narration vs the shared guardrail's ``personal-advice`` topic.

Issue ``issues/2026-10-10-cost-guardrail-blocks-replies/`` (prompt 157, 2026-10-28;
evidence in ``2026-10-28-p157-evidence/``). Since 10-22, 216 of 228 cost-agent
interventions were ``personal-advice`` blocks on energy-only ``efficiency`` replies
whose action was worded "driver coaching". Rewording only the action as a fleet action
on the vehicle cleared 18 of 18 blocked replies on the live guardrail. User decision
2026-10-28 (option 1): fix the agent's wording, not the guardrail.

Pinned here:
  - the playbook's Narration section carries the driver_coaching wording rule;
  - a retry after a guardrail block names every blocked policy; a ``personal-advice``
    block adds the fleet-action instruction, plus the coaching wording for a
    ``driver_coaching`` reply;
  - the INTERVENED log line carries ``vin_hash``, ``cause`` and ``action`` (enum values
    only, never reply text or the VIN);
  - the 28 reproduced cars are a fixture, and the live probe gates their new-prompt
    replies as pass cases.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import logging
import pathlib
import re
from typing import Any

import pytest

from agents.fi_cost import agent
from agents.fi_cost.diagnose import PLAYBOOK_TEXT

_HERE = pathlib.Path(__file__).parent
FIXTURE = _HERE / "fixtures" / "coaching_replies_2026-10-28.json"
PROBE = _HERE.parent / "scripts" / "live_guardrail_probe.py"

_VIN = "MRDAZ01S1SC02CEDB"
_VIN_HASH = hashlib.sha256(_VIN.encode("utf-8")).hexdigest()[:8]
_FLEET_ACTION = "enrol this vehicle in the fleet's eco-driving programme"


def _narration_section() -> str:
    m = re.search(r"^## Narration\n(.*?)(?=^## |\Z)", PLAYBOOK_TEXT, re.S | re.M)
    assert m, "playbook has no '## Narration' section"
    return m.group(1)


def _intervened(assessments: list[dict[str, Any]]) -> dict[str, Any]:
    return {"action": "GUARDRAIL_INTERVENED", "assessments": assessments}


_PERSONAL_ADVICE = _intervened([
    {"topicPolicy": {"topics": [{"name": "personal-advice", "type": "DENY",
                                 "action": "BLOCKED", "detected": True}]}},
])
_PII_NAME = _intervened([
    {"sensitiveInformationPolicy": {"piiEntities": [
        {"type": "NAME", "action": "BLOCKED", "match": "Jane Doe"}]}},
])
_PASS = {"action": "NONE", "assessments": []}


class _SeqBedrock:
    """ApplyGuardrail stub returning canned responses in order."""

    def __init__(self, responses: list[dict[str, Any]]) -> None:
        self.responses = list(responses)

    def apply_guardrail(self, **kwargs: Any) -> dict[str, Any]:
        return self.responses.pop(0)


def _tool_reply(headline: str = "H", detail: str = "D",
                action: str = "driver_coaching") -> dict[str, Any]:
    return {"output": {"message": {"content": [{"toolUse": {
        "name": "record_diagnosis",
        "input": {"cause": "efficiency", "action": action,
                  "headline": headline, "detail": detail},
    }}]}}}


@pytest.fixture
def retry_harness(monkeypatch):
    """Patch Converse and validation; record the error_context of every attempt."""
    monkeypatch.setenv("FI_COST_GUARDRAIL_ID", "test-guardrail-id")
    monkeypatch.setenv("FI_COST_GUARDRAIL_VERSION", "DRAFT")
    contexts: list[str] = []

    def fake_converse(evidence, bedrock_client, error_context):
        contexts.append(error_context)
        return _tool_reply("REPLY-HEADLINE-SECRET", "REPLY-DETAIL-SECRET"), 10, 5

    monkeypatch.setattr(agent, "_converse", fake_converse)
    monkeypatch.setattr(agent, "validate_tool_input", lambda ti, ev: (True, ""))
    return contexts


# ---------- playbook rule ----------------------------------------------------


def test_playbook_narration_words_driver_coaching_as_a_fleet_action():
    section = _narration_section()
    assert "`driver_coaching`" in section
    assert _FLEET_ACTION in section


def test_playbook_narration_forbids_the_blocked_phrase():
    assert re.search(r'Do not write "driver coaching"', _narration_section())


def test_playbook_narration_forbids_addressing_or_describing_a_driver():
    assert re.search(r"do not address a driver or describe a driver's", _narration_section())


# ---------- retry hint -------------------------------------------------------


def test_retry_after_personal_advice_names_the_topic_and_the_fleet_action(retry_harness):
    bedrock = _SeqBedrock([_PERSONAL_ADVICE, _PASS])
    _ti, _i, _o, source = agent._diagnose_with_retry({"vin": _VIN}, bedrock)
    assert source == "model"
    assert retry_harness[0] == ""
    hint = retry_harness[1]
    assert "topic personal-advice" in hint
    assert _FLEET_ACTION in hint
    assert "REPLY-" not in hint


def test_retry_after_a_pii_block_names_the_policy_only(retry_harness):
    bedrock = _SeqBedrock([_PII_NAME, _PASS])
    agent._diagnose_with_retry({"vin": _VIN}, bedrock)
    hint = retry_harness[1]
    assert "pii NAME" in hint
    assert "Jane Doe" not in hint
    assert _FLEET_ACTION not in hint
    assert "personal-advice" not in hint


def test_retry_after_blame_of_a_person_names_that_topic_not_personal_advice(retry_harness):
    blame = _intervened([{"topicPolicy": {"topics": [
        {"name": "blame-of-a-person", "type": "DENY", "action": "BLOCKED"}]}}])
    agent._diagnose_with_retry({"vin": _VIN}, _SeqBedrock([blame, _PASS]))
    hint = retry_harness[1]
    assert "topic blame-of-a-person" in hint
    assert "personal-advice" not in hint
    assert _FLEET_ACTION not in hint


def test_retry_after_personal_advice_plus_pii_names_both(retry_harness):
    mixed = _intervened([{
        "topicPolicy": {"topics": [{"name": "personal-advice", "type": "DENY",
                                    "action": "BLOCKED"}]},
        "sensitiveInformationPolicy": {"piiEntities": [
            {"type": "NAME", "action": "BLOCKED", "match": "Jane Doe"}]},
    }])
    agent._diagnose_with_retry({"vin": _VIN}, _SeqBedrock([mixed, _PASS]))
    hint = retry_harness[1]
    assert "topic personal-advice" in hint
    assert "pii NAME" in hint
    assert _FLEET_ACTION in hint
    assert "Jane Doe" not in hint


def test_personal_advice_hint_gives_the_coaching_wording_only_for_driver_coaching():
    trips = [{"policy": "topic", "name": "personal-advice"}]
    coaching = agent._guardrail_retry_hint(trips, "driver_coaching")
    other = agent._guardrail_retry_hint(trips, "schedule_inspection")
    for hint in (coaching, other):
        assert "topic personal-advice" in hint
        assert "fleet action on this vehicle" in hint
        assert "do not address a driver or describe a driver's behaviour" in hint
    assert _FLEET_ACTION in coaching
    assert _FLEET_ACTION not in other
    assert "driver_coaching" not in other


def test_retry_hint_uses_the_replys_own_action(monkeypatch):
    monkeypatch.setenv("FI_COST_GUARDRAIL_ID", "test-guardrail-id")
    contexts: list[str] = []

    def fake_converse(evidence, bedrock_client, error_context):
        contexts.append(error_context)
        return _tool_reply(action="schedule_inspection"), 10, 5

    monkeypatch.setattr(agent, "_converse", fake_converse)
    monkeypatch.setattr(agent, "validate_tool_input", lambda ti, ev: (True, ""))
    agent._diagnose_with_retry({"vin": _VIN}, _SeqBedrock([_PERSONAL_ADVICE, _PASS]))
    hint = contexts[1]
    assert "fleet action on this vehicle" in hint
    assert _FLEET_ACTION not in hint
    assert "driver_coaching" not in hint


def test_retry_hint_topic_name_matches_the_stack():
    stack = (_HERE.parents[2] / "infrastructure" / "lib" / "vsa-tier2-stack.ts").read_text()
    assert re.search(r"name:\s*'personal-advice'", stack), (
        "the retry hint keys on the topic name 'personal-advice'; rename both together")


def test_two_personal_advice_blocks_still_fall_back_to_rules(retry_harness):
    bedrock = _SeqBedrock([_PERSONAL_ADVICE, _PERSONAL_ADVICE])
    _ti, _i, _o, source = agent._diagnose_with_retry({"vin": _VIN}, bedrock)
    assert source == "rules"
    assert len(retry_harness) == 2


# ---------- INTERVENED log line ---------------------------------------------


def _warnings(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records
            if r.levelno == logging.WARNING and "policies_blocked=" in r.getMessage()]


def test_intervened_line_carries_vin_hash_cause_and_action(caplog, monkeypatch):
    monkeypatch.setenv("FI_COST_GUARDRAIL_ID", "test-guardrail-id")
    caplog.set_level(logging.WARNING, logger="agents.fi_cost.agent")
    agent._apply_guardrail(_SeqBedrock([_PERSONAL_ADVICE]), "H", "D",
                           vin=_VIN, cause="efficiency", action="driver_coaching")
    [msg] = _warnings(caplog)
    assert f"vin_hash={_VIN_HASH}" in msg
    assert "cause=efficiency" in msg
    assert "action=driver_coaching" in msg
    assert _VIN not in msg


def test_intervened_line_never_logs_a_cause_or_action_outside_the_enums(caplog, monkeypatch):
    monkeypatch.setenv("FI_COST_GUARDRAIL_ID", "test-guardrail-id")
    caplog.set_level(logging.WARNING, logger="agents.fi_cost.agent")
    agent._apply_guardrail(_SeqBedrock([_PERSONAL_ADVICE]), "H", "D", vin=_VIN,
                           cause="Jane Doe CAUSE-SECRET", action="call 555 ACTION-SECRET")
    [msg] = _warnings(caplog)
    assert "SECRET" not in msg
    assert "cause=? " in msg or msg.endswith("cause=?")
    assert "action=?" in msg


def test_diagnose_passes_vin_cause_and_action_to_the_intervened_line(retry_harness, caplog):
    caplog.set_level(logging.WARNING, logger="agents.fi_cost.agent")
    agent._diagnose_with_retry({"vin": _VIN}, _SeqBedrock([_PERSONAL_ADVICE, _PASS]))
    [msg] = _warnings(caplog)
    assert f"vin_hash={_VIN_HASH}" in msg
    assert "cause=efficiency action=driver_coaching" in msg
    assert "REPLY-" not in msg


# ---------- the 28 reproduced cars ------------------------------------------


def _fixture() -> dict[str, Any]:
    return json.loads(FIXTURE.read_text())


def test_fixture_holds_the_28_reproduced_cars_with_live_matched_inputs():
    cars = _fixture()["cars"]
    assert len(cars) == 28
    assert len({c["vin"] for c in cars}) == 28
    assert all(c["inputs_hash_matched_live"] for c in cars)
    assert sum(c["old_prompt"]["guardrail_action"] == "GUARDRAIL_INTERVENED" for c in cars) == 18


def test_fixture_old_prompt_replies_carry_the_blocked_phrase():
    for car in _fixture()["cars"]:
        old = car["old_prompt"]
        assert "driver coaching" in (old["headline"] + " " + old["detail"]).lower()


def test_fixture_new_prompt_replies_follow_the_playbook_rule():
    for car in _fixture()["cars"]:
        new = car["new_prompt"]
        text = (new["headline"] + " " + new["detail"]).lower()
        assert "coaching" not in text, car["vin"]
        assert len(new["headline"]) <= 120 and len(new["detail"]) <= 600, car["vin"]
        assert new["guardrail_action"] == "NONE", car["vin"]
        if new["action"] == "driver_coaching":
            assert "eco-driving programme" in text, car["vin"]


def _probe_module():
    spec = importlib.util.spec_from_file_location("live_guardrail_probe", PROBE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_probe_gates_every_new_prompt_reply_as_a_pass_case():
    probes = {name: (expected, h, d) for expected, name, h, d in _probe_module().PROBES}
    for i, car in enumerate(_fixture()["cars"], start=1):
        name = f"coaching_{i:02d}"
        assert name in probes, name
        expected, headline, detail = probes[name]
        assert expected == "pass"
        assert (headline, detail) == (car["new_prompt"]["headline"], car["new_prompt"]["detail"])
        assert not any(car["vin"][-6:] in n for n in probes), car["vin"]


def test_probe_keeps_the_original_ten_cases():
    probes = {name: expected for expected, name, _h, _d in _probe_module().PROBES}
    original = {"charging_rate": "pass", "charging_mix": "pass", "repeat_repair": "pass",
                "low_utilisation": "pass", "efficiency": "pass", "unclear": "pass",
                "vin_mention": "pass", "real_pii_name": "block", "blame_of_person": "block",
                "off_topic_advice": "block"}
    assert {k: probes.get(k) for k in original} == original
    assert len(probes) == len(original) + len(_fixture()["cars"])
