"""fleet_recommendation: a fleet-health stop-driving item's directive is narrated verbatim.

Issue: ``issues/2026-10-27-stop-driving-verbatim-in-fleet-chat/report.md``.
Same contract as recall items (stage C D18, ``test_fleet_recommendation_safety_text.py``):

- Field: a ``fleet.health.service`` Finding with ``severity == "p0"`` (booking ``service/<n>``
  and grounding ``ground/<n>`` items alike, contract ``docs/fi-fleet-health-contract.md``
  § 1.5) returns top-level ``safety_text`` equal to its ``disclosure_txt`` byte for byte, on
  the ``available: true`` path only, never when the text is empty or not a string.
- Never for a non-stop-driving item: fleet-health items below p0, and p0 items of other
  kinds, get no ``safety_text`` even when they carry ``disclosure_txt``.
- Instruction: the model-visible tool description and ``FLEET_MANAGER_PROMPT`` tell the
  assistant to read ``safety_text`` for stop-driving faults word for word, first.

The narration itself is checked against the deployed text runtime by
``scripts/live-verify-stop-driving-verbatim.py`` with the eval case
``evals/cases/fi/fleet-health-stop-driving-text-001.yaml``.
"""

from __future__ import annotations

import re
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import agents.supervisor.tools.fleet_recommendation as fr_mod
from agents.supervisor.persona_definitions import FLEET_MANAGER_PROMPT
from agents.supervisor.tools.fleet_recommendation import make_fleet_recommendation_tool
from agents.supervisor.tests.test_fleet_recommendation import (  # noqa: E402
    _ddb_stubs,
    _make_action,
    _make_finding,
    _make_jwt,
)

_HEALTH_KIND = "fleet.health.service"
_FINDING_ID = "FIND-health01-1234-5678-abcd-ef1234567890"

#: The directive every live p0 fleet-health Finding carried on 2026-10-27 (read-only scan of
#: vsa-staging-avx-findings: 1,708 items, one distinct value).
_LIVE_DIRECTIVE = (
    "Stop driving safely now. Do not restart the vehicle. "
    "Contact Meridian Roadside Assistance."
)

#: A directive that a strip, whitespace collapse or re-sentencing would change: a double
#: space, an inner newline, a trailing space.
_SHAPED_DIRECTIVE = (
    "Stop driving immediately.  Do not restart the vehicle.\n"
    "Contact your dealer for towing to the nearest service centre. "
)


def _health_finding(
    *,
    severity: str | None = "p0",
    subject_key: str = "service/1",
    disclosure: object = _SHAPED_DIRECTIVE,
    finding_kind: str = _HEALTH_KIND,
) -> dict:
    """A fleet-health Finding in the shape the writer stores (write.build_finding)."""
    f = _make_finding(finding_id=_FINDING_ID)
    f["agent_id"] = "agent-fi-fleet-health"
    f["finding_kind"] = finding_kind
    f["subject_key"] = subject_key
    f["headline"] = "Stop-driving fault P0AA6: book service at dealer-denver"
    f["detail"] = ""
    if severity is None:
        f.pop("severity", None)
    else:
        f["severity"] = severity
    if disclosure is not None:
        f["disclosure_txt"] = disclosure
    if subject_key.startswith("ground/"):
        f["recommended_action"] = {
            "action_kind": "ground_vehicle",
            "target_system": "cms",
            "parameters": {"fault_codes": ["P0AA6"]},
        }
    else:
        f["recommended_action"] = {
            "action_kind": "book_fleet_service",
            "target_system": "dms",
            "parameters": {"dealer_id": "dealer-denver", "urgency": "this_week"},
        }
    return f


def _call(
    finding: dict | None,
    *,
    fleet_ids: tuple[str, ...] = ("FLEET-001",),
    finding_error: Exception | None = None,
    action_error: Exception | None = None,
) -> dict:
    jwt_str, env = _make_jwt(
        groups=["fleet-operator"], fleet_ids=list(fleet_ids), trust_fleet_ids=True
    )
    resource = _ddb_stubs(
        finding,
        _make_action(finding_id=_FINDING_ID),
        finding_error=finding_error,
        action_error=action_error,
    )
    with patch.dict("os.environ", env), patch.object(
        fr_mod.boto3, "resource", return_value=resource
    ):
        tool = make_fleet_recommendation_tool(SimpleNamespace(jwt=jwt_str), SimpleNamespace())
        return tool(_FINDING_ID)


def _squash(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


# ===========================================================================
# Field: stop-driving items carry safety_text, byte for byte
# ===========================================================================


class TestStopDrivingItemsCarrySafetyText:
    @pytest.mark.parametrize("subject_key", ["service/1", "ground/1"])
    def test_p0_item_safety_text_is_the_directive_byte_for_byte(self, subject_key: str) -> None:
        result = _call(_health_finding(subject_key=subject_key))
        assert result["available"] is True
        assert result.get("safety_text") == _SHAPED_DIRECTIVE, (
            "a p0 fleet-health item (booking or grounding) must return its disclosure_txt as "
            "top-level safety_text, unchanged: no strip, no whitespace collapse, no rewording"
        )

    def test_live_directive_comes_back_unchanged(self) -> None:
        result = _call(_health_finding(disclosure=_LIVE_DIRECTIVE))
        assert result["safety_text"] == _LIVE_DIRECTIVE

    def test_safety_text_is_top_level_and_the_plain_field_is_unchanged(self) -> None:
        result = _call(_health_finding())
        assert "safety_text" not in result["finding"]
        assert result["finding"]["disclosure_txt"] == _SHAPED_DIRECTIVE


# ===========================================================================
# Never for a non-stop-driving item
# ===========================================================================


class TestNoSafetyTextForNonStopDrivingItems:
    @pytest.mark.parametrize("severity", ["attention", "soon", "routine", "P0", "p0 "])
    def test_fleet_health_item_below_p0_gets_none(self, severity: str) -> None:
        """Contract § 1.5: items below p0 carry no directive. One that does is not narrated
        as a stop-driving order. Near-miss spellings of p0 don't count either."""
        result = _call(_health_finding(severity=severity))
        assert result["available"] is True
        assert "safety_text" not in result, f"severity={severity!r} must not get safety_text"

    def test_fleet_health_item_without_severity_gets_none(self) -> None:
        result = _call(_health_finding(severity=None))
        assert result["available"] is True
        assert "safety_text" not in result

    @pytest.mark.parametrize(
        "kind",
        ["fleet.cost.anomaly", "fleet.cost.sell_timing", "fleet.warranty.claim_candidate",
         "fleet.rebalance.move", "fleet.rebalance.gap", "quality.issue",
         "quality.investigation", "fleet.health.alert", "fleet.health.service.x"],
    )
    def test_p0_item_of_another_kind_gets_none(self, kind: str) -> None:
        result = _call(_health_finding(finding_kind=kind))
        assert result["available"] is True
        assert "safety_text" not in result, f"{kind} p0 must not get safety_text"

    @pytest.mark.parametrize("disclosure", ["", None, ["Stop driving."], 7])
    def test_p0_item_without_usable_text_gets_none(self, disclosure: object) -> None:
        result = _call(_health_finding(disclosure=disclosure))
        assert result["available"] is True
        assert "safety_text" not in result


class TestNoSafetyTextWhenUnavailable:
    def test_absent_when_fleet_standing_fails(self) -> None:
        result = _call(_health_finding(), fleet_ids=("FLEET-999",))
        assert result["available"] is False
        assert "safety_text" not in result

    def test_absent_when_finding_read_fails(self) -> None:
        result = _call(None, finding_error=RuntimeError("DDB unavailable"))
        assert result["available"] is False
        assert "safety_text" not in result

    def test_absent_when_action_read_fails(self) -> None:
        result = _call(_health_finding(), action_error=RuntimeError("DDB unavailable"))
        assert result["available"] is False
        assert "safety_text" not in result


class TestRecallItemsUnchanged:
    def test_recall_p0_still_gets_safety_text(self) -> None:
        result = _call(_health_finding(finding_kind="fleet.recall.vehicle"))
        assert result["safety_text"] == _SHAPED_DIRECTIVE

    def test_recall_below_p0_still_gets_safety_text(self) -> None:
        """D18 keys recall items on kind alone; this fix doesn't narrow that."""
        result = _call(_health_finding(finding_kind="fleet.recall.vehicle", severity="soon"))
        assert result["safety_text"] == _SHAPED_DIRECTIVE


# ===========================================================================
# Instruction: what the model is told
# ===========================================================================


def _description() -> str:
    tool = make_fleet_recommendation_tool(SimpleNamespace(jwt=""), SimpleNamespace())
    return tool.tool_spec["description"]


class TestToolDescriptionInstruction:
    def test_description_tells_the_model_to_read_stop_driving_safety_text_word_for_word(
        self,
    ) -> None:
        """The paragraph that introduces safety_text must name stop-driving faults and say
        'word for word'. Mutation: drop or narrow the instruction back to recalls only."""
        paragraphs = [p for p in _description().split("\n\n") if "safety_text" in p]
        assert paragraphs, "the tool description must mention safety_text"
        para = _squash(" ".join(paragraphs)).lower()
        assert "stop-driving" in para, para
        assert "recall" in para, para
        assert "word for word" in para, para
        assert "before anything else" in para, para

    def test_description_names_fleet_health_items(self) -> None:
        first = _squash(_description().split("\n\n")[0]).lower()
        assert "fleet health" in first, first


class TestFleetManagerPromptInstruction:
    def test_tool_line_says_safety_text_covers_stop_driving_faults(self) -> None:
        prompt = _squash(FLEET_MANAGER_PROMPT)
        m = re.search(r"- `fleet_recommendation` — (.+?)(?= - `|Supporting tools:)", prompt)
        assert m, "FLEET_MANAGER_PROMPT must list fleet_recommendation"
        line = m.group(1).lower()
        assert "safety_text" in line and "stop-driving" in line and "recall" in line, line

    def test_rule_four_routes_fleet_health_recommendations_to_the_tool(self) -> None:
        prompt = _squash(FLEET_MANAGER_PROMPT)
        m = re.search(r" 4\. (.+?) 5\. ", prompt)
        assert m, "FLEET_MANAGER_PROMPT must keep its numbered rule 4"
        rule = m.group(1).lower()
        assert "fleet_recommendation" in rule
        assert "fleet health" in rule, rule

    def test_rule_five_reads_safety_text_verbatim_first(self) -> None:
        """The narration rule itself, word for word (whitespace aside). Mutation: drop it or
        soften any clause."""
        rule = (
            "5. When `fleet_recommendation` returns `safety_text`, read it to the user "
            "exactly as given, word for word, before anything else. Never shorten, "
            "summarise, soften or reorder it."
        )
        assert rule in _squash(FLEET_MANAGER_PROMPT)
