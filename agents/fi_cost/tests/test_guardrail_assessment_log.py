"""Guardrail-assessment logging: records which policy fired without leaking reply text.

Decisions 2026-10-10 PO follow-on. Scope:

- ``_summarise_blocked_policies`` extracts (policy, name) pairs from an
  ApplyGuardrail response and NEVER carries detected-text fields (PII ``match``,
  ``customWords.match``).
- ``_apply_guardrail`` emits one WARNING log line on GUARDRAIL_INTERVENED that
  carries the summarised policies and NO reply text.
- The mutant we must catch: if someone edits the log statement to include the
  detected string or the headline/detail, the test fails.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from agents.fi_cost.agent import _apply_guardrail, _summarise_blocked_policies


_HEAD_SECRET = "DRIVER-JANE-DOE-HEADLINE-SECRET-TOKEN"
_DET_SECRET = "DRIVER-JANE-DOE-DETAIL-SECRET-TOKEN AT 555-SECRET-NUMBER"


def _stub_resp(assessments: list[dict[str, Any]]) -> dict[str, Any]:
    return {"action": "GUARDRAIL_INTERVENED", "assessments": assessments}


class _FakeBedrock:
    """Returns a canned ApplyGuardrail response; records the texts it saw.

    The texts argument captures what the agent passed as headline/detail; the
    tests use the ``seen_texts`` list to confirm the agent did pass the
    secrets to Bedrock (sanity check) and that the LOG RECORDS below still
    do not contain those secrets.
    """

    def __init__(self, canned_resp: dict[str, Any]) -> None:
        self.canned_resp = canned_resp
        self.seen_texts: list[str] = []

    def apply_guardrail(self, **kwargs: Any) -> dict[str, Any]:
        for item in kwargs.get("content", []):
            self.seen_texts.append(item["text"]["text"])
        return self.canned_resp


# ---------- _summarise_blocked_policies -------------------------------------


def test_summariser_lists_topic_and_pii_and_content_by_name_only():
    resp = _stub_resp([
        {
            "topicPolicy": {
                "topics": [
                    {"name": "advice-outside-vehicle-cost", "type": "DENY", "action": "BLOCKED"},
                    {"name": "blame-of-a-person",         "type": "DENY", "action": "NONE"},
                ],
            },
            "sensitiveInformationPolicy": {
                "piiEntities": [
                    {"type": "NAME",  "action": "BLOCKED", "match": "Jane Doe"},
                    {"type": "PHONE", "action": "BLOCKED", "match": "555-123-4567"},
                    {"type": "EMAIL", "action": "NONE"},
                ],
            },
            "contentPolicy": {
                "filters": [
                    {"type": "MISCONDUCT", "action": "BLOCKED", "confidence": "MEDIUM"},
                ],
            },
        },
    ])
    trips = _summarise_blocked_policies(resp)
    assert trips == [
        {"policy": "topic",   "name": "advice-outside-vehicle-cost"},
        {"policy": "pii",     "name": "NAME"},
        {"policy": "pii",     "name": "PHONE"},
        {"policy": "content", "name": "MISCONDUCT"},
    ]


def test_summariser_omits_pii_match_values_so_detected_text_is_not_recorded():
    # The response carries a leaky `match` for both PII entities; the summariser
    # output must not contain either value anywhere.
    resp = _stub_resp([
        {
            "sensitiveInformationPolicy": {
                "piiEntities": [
                    {"type": "NAME",    "action": "BLOCKED", "match": "Jane Doe"},
                    {"type": "ADDRESS", "action": "BLOCKED", "match": "742 Evergreen Terrace"},
                ],
            },
        },
    ])
    trips = _summarise_blocked_policies(resp)
    serialised = json.dumps(trips)
    assert "Jane Doe" not in serialised
    assert "Evergreen" not in serialised
    assert "742" not in serialised
    assert all(set(t.keys()) == {"policy", "name"} for t in trips)


def test_summariser_omits_custom_word_match_values():
    # If a hypothetical word policy ever ships, the matched word must not leak.
    resp = _stub_resp([
        {
            "wordPolicy": {
                "customWords": [
                    {"match": "SOME-COMPETITOR-NAME", "action": "BLOCKED"},
                ],
            },
        },
    ])
    trips = _summarise_blocked_policies(resp)
    assert trips == [{"policy": "word", "name": "custom"}]
    assert "SOME-COMPETITOR" not in json.dumps(trips)


def test_summariser_returns_empty_list_on_action_none():
    # action=NONE: every topic/pii/content entry is action=NONE and the
    # summariser returns [] — no spurious log entry.
    resp = {
        "action": "NONE",
        "assessments": [
            {
                "topicPolicy": {"topics": [{"name": "advice-outside-vehicle-cost", "action": "NONE"}]},
                "sensitiveInformationPolicy": {"piiEntities": [{"type": "NAME", "action": "NONE"}]},
            },
        ],
    }
    assert _summarise_blocked_policies(resp) == []


# ---------- _apply_guardrail logging ---------------------------------------


def test_apply_guardrail_warning_log_contains_policy_names_but_not_reply_text(
    caplog, monkeypatch
):
    monkeypatch.setenv("FI_COST_GUARDRAIL_ID", "test-guardrail-id")
    monkeypatch.setenv("FI_COST_GUARDRAIL_VERSION", "DRAFT")
    bedrock = _FakeBedrock(_stub_resp([
        {
            "topicPolicy": {"topics": [{"name": "advice-outside-vehicle-cost",
                                        "type": "DENY", "action": "BLOCKED"}]},
            "sensitiveInformationPolicy": {
                "piiEntities": [{"type": "NAME", "action": "BLOCKED", "match": "Jane Doe"}],
            },
        },
    ]))
    caplog.set_level(logging.WARNING, logger="agents.fi_cost.agent")
    blocked = _apply_guardrail(bedrock, _HEAD_SECRET, _DET_SECRET)
    assert blocked is True
    # Sanity: the fake bedrock did see the secrets (agent did pass them through).
    assert _HEAD_SECRET in bedrock.seen_texts
    assert _DET_SECRET in bedrock.seen_texts
    # Exactly one WARNING line emitted.
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    msg = warnings[0].getMessage()
    # Policy identifiers present.
    assert "advice-outside-vehicle-cost" in msg
    assert '"policy": "pii"' in msg or '"policy":"pii"' in msg
    assert '"name": "NAME"' in msg or '"name":"NAME"' in msg
    # Reply text absent — the two secrets must not appear in ANY log record.
    for rec in caplog.records:
        rendered = rec.getMessage()
        assert _HEAD_SECRET not in rendered, (
            "Headline text leaked into log: fix _apply_guardrail — do NOT pass "
            "headline/detail to log.* when guardrail intervenes."
        )
        assert _DET_SECRET not in rendered, (
            "Detail text leaked into log: fix _apply_guardrail — do NOT pass "
            "headline/detail to log.* when guardrail intervenes."
        )
        # The leaked-via-PII-match case: Jane Doe / 555-SECRET-NUMBER appeared
        # in the fake response's `match` fields. Confirm neither reached the log.
        assert "Jane Doe" not in rendered
        assert "SECRET-NUMBER" not in rendered


def test_apply_guardrail_no_warning_on_action_none(caplog, monkeypatch):
    monkeypatch.setenv("FI_COST_GUARDRAIL_ID", "test-guardrail-id")
    monkeypatch.setenv("FI_COST_GUARDRAIL_VERSION", "DRAFT")
    bedrock = _FakeBedrock({"action": "NONE", "assessments": []})
    caplog.set_level(logging.WARNING, logger="agents.fi_cost.agent")
    blocked = _apply_guardrail(bedrock, "headline that passes", "detail that passes")
    assert blocked is False
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings == []


def test_apply_guardrail_log_is_grepable_for_the_tag_the_run_report_expects(
    caplog, monkeypatch
):
    # A stable tag the operator can grep for across the Lambda log group.
    monkeypatch.setenv("FI_COST_GUARDRAIL_ID", "test-guardrail-id")
    monkeypatch.setenv("FI_COST_GUARDRAIL_VERSION", "DRAFT")
    bedrock = _FakeBedrock(_stub_resp([
        {"topicPolicy": {"topics": [{"name": "advice-outside-vehicle-cost",
                                     "type": "DENY", "action": "BLOCKED"}]}},
    ]))
    caplog.set_level(logging.WARNING, logger="agents.fi_cost.agent")
    _apply_guardrail(bedrock, "x", "y")
    msgs = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("fi_cost.agent" in m and "guardrail INTERVENED" in m
               and "policies_blocked=" in m for m in msgs)
