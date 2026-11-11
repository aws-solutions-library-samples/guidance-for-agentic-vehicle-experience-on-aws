"""Guardrail-assessment logging for fi_recall_warranty (plan_booking + draft_claim).

Shape mirrors ``agents/fi_cost/tests/test_guardrail_assessment_log.py``.
Decisions 2026-10-10 PO follow-on: the shared FiCost guardrail blocked 36 of 95
recall-plan replies in the 13:13Z stage C run and nothing in the agent logs
told us which policy fired. This test file pins the fix on both call sites.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from agents.fi_recall_warranty.llm import (
    _apply_guardrail,
    _summarise_blocked_policies,
)


_NARR_SECRET = "DRIVER-JANE-DOE-NARRATIVE-SECRET-TOKEN"
_DET_SECRET = "DRIVER-JANE-DOE-DETAIL-SECRET-TOKEN AT 555-SECRET-NUMBER"


class _FakeBedrock:
    def __init__(self, canned_resp: dict[str, Any]) -> None:
        self.canned_resp = canned_resp
        self.seen_texts: list[str] = []

    def apply_guardrail(self, **kwargs: Any) -> dict[str, Any]:
        for item in kwargs.get("content", []):
            self.seen_texts.append(item["text"]["text"])
        return self.canned_resp


def _stub_resp(assessments: list[dict[str, Any]]) -> dict[str, Any]:
    return {"action": "GUARDRAIL_INTERVENED", "assessments": assessments}


def test_summariser_mirrors_fi_cost_shape():
    resp = _stub_resp([
        {
            "topicPolicy": {"topics": [{"name": "blame-of-a-person",
                                        "type": "DENY", "action": "BLOCKED"}]},
            "sensitiveInformationPolicy": {
                "piiEntities": [{"type": "NAME",  "action": "BLOCKED", "match": "Jane Doe"},
                                {"type": "PHONE", "action": "BLOCKED", "match": "555-1212"}],
            },
        },
    ])
    trips = _summarise_blocked_policies(resp)
    assert trips == [
        {"policy": "topic", "name": "blame-of-a-person"},
        {"policy": "pii",   "name": "NAME"},
        {"policy": "pii",   "name": "PHONE"},
    ]
    assert "Jane Doe" not in json.dumps(trips)


def test_apply_guardrail_tags_the_plan_booking_call_site_and_hides_narrative_text(
    caplog,
):
    bedrock = _FakeBedrock(_stub_resp([
        {"topicPolicy": {"topics": [{"name": "advice-outside-vehicle-cost",
                                     "type": "DENY", "action": "BLOCKED"}]}},
    ]))
    caplog.set_level(logging.WARNING, logger="agents.fi_recall_warranty.llm")
    blocked = _apply_guardrail(
        bedrock, "gid", "DRAFT", [_NARR_SECRET, _DET_SECRET],
        component="fi_recall_warranty.plan_booking",
    )
    assert blocked is True
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    msg = warnings[0].getMessage()
    assert "fi_recall_warranty.plan_booking" in msg
    assert "advice-outside-vehicle-cost" in msg
    for rec in caplog.records:
        rendered = rec.getMessage()
        assert _NARR_SECRET not in rendered
        assert _DET_SECRET not in rendered
        assert "SECRET-NUMBER" not in rendered


def test_apply_guardrail_tags_the_draft_claim_call_site_distinctly(caplog):
    # Same helper, different component tag — a 36-of-95 run breakdown should
    # read naturally per call site in CloudWatch Logs Insights.
    bedrock = _FakeBedrock(_stub_resp([
        {"topicPolicy": {"topics": [{"name": "blame-of-a-person",
                                     "type": "DENY", "action": "BLOCKED"}]}},
    ]))
    caplog.set_level(logging.WARNING, logger="agents.fi_recall_warranty.llm")
    blocked = _apply_guardrail(
        bedrock, "gid", "DRAFT", ["n", "d"],
        component="fi_recall_warranty.draft_claim",
    )
    assert blocked is True
    msgs = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("fi_recall_warranty.draft_claim" in m for m in msgs)
    assert any("fi_recall_warranty.plan_booking" not in m for m in msgs)


def test_apply_guardrail_no_warning_on_action_none(caplog):
    bedrock = _FakeBedrock({"action": "NONE", "assessments": []})
    caplog.set_level(logging.WARNING, logger="agents.fi_recall_warranty.llm")
    blocked = _apply_guardrail(bedrock, "gid", "DRAFT", ["ok"],
                               component="fi_recall_warranty.plan_booking")
    assert blocked is False
    assert [r for r in caplog.records if r.levelno == logging.WARNING] == []
