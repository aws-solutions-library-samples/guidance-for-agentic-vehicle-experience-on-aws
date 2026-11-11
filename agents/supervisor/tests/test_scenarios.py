"""End-to-end scenario tests against Claude Sonnet 4.6.

Runs the three demo scenarios (P3 tire, P1 coolant, P0 brake) through
the full supervisor agent and asserts:

  1. The expected tool sequence happened, with required args.
  2. No forbidden tools were called.
  3. The final user-facing text contains phrases consistent with the
     classification the deterministic triage classifier actually returned.

Assertions are classification-aware: whatever P-level triage returns,
the supervisor's narration and booking behavior must match. This
decouples scenario tests from the classifier's internal state, which
is owned by a different Phase.

These tests are SLOW (~15s each) and cost Bedrock tokens. Opt-in:

    VSA_TEST_E2E=1 pytest agents/supervisor/tests/test_scenarios.py -v

Requires:
  - AWS credentials for us-east-1, Bedrock access to
    us.anthropic.claude-sonnet-4-6 inference profile.
  - Cached Cognito token (auto-fetched on first run).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from strands.hooks.events import BeforeToolCallEvent

from agents.supervisor.config import Config, get_cached_id_token
from agents.supervisor.session import SessionContext
from agents.supervisor.supervisor import build_supervisor


pytestmark = pytest.mark.skipif(
    not os.environ.get("VSA_TEST_E2E"),
    reason="Set VSA_TEST_E2E=1 to run end-to-end scenario tests",
)


SCENARIOS_DIR = Path(__file__).parent / "scenarios"


# ---------------------------------------------------------------------------
# Tool-call recorder using Strands' documented hook API
# ---------------------------------------------------------------------------


@dataclass
class _ToolCall:
    name: str
    args: dict[str, Any] = field(default_factory=dict)


class _ToolRecorder:
    """Records every tool invocation via BeforeToolCallEvent."""

    def __init__(self) -> None:
        self.calls: list[_ToolCall] = []

    def on_before_tool(self, event: BeforeToolCallEvent) -> None:
        tu = event.tool_use
        self.calls.append(_ToolCall(name=tu["name"], args=dict(tu.get("input") or {})))

    def names(self) -> list[str]:
        return [c.name for c in self.calls]

    def first_call(self, name: str) -> _ToolCall | None:
        return next((c for c in self.calls if c.name == name), None)


def _load_scenarios() -> list[dict]:
    return [json.loads(p.read_text()) for p in sorted(SCENARIOS_DIR.glob("*.json"))]


def _extract_text(result) -> str:
    """Pull the final text response out of an AgentResult."""
    msg = getattr(result, "message", None)
    if msg is None:
        return str(result)
    content = msg.get("content") if isinstance(msg, dict) else getattr(msg, "content", None)
    if not content:
        return str(msg)
    parts: list[str] = []
    for block in content:
        if isinstance(block, dict) and "text" in block:
            parts.append(block["text"])
    return "\n".join(parts) if parts else str(msg)


def _collect_all_assistant_text(agent) -> str:
    """Concatenate every assistant-role text block in agent.messages.

    This captures narration delivered BEFORE a tool call, which is where
    the P0 pull-over instruction usually lands. _extract_text on the
    AgentResult only returns the final message, which may be a ticket
    confirmation rather than the safety directive.
    """
    parts: list[str] = []
    for msg in agent.messages or []:
        if not isinstance(msg, dict):
            continue
        if msg.get("role") != "assistant":
            continue
        for block in msg.get("content") or []:
            if isinstance(block, dict) and "text" in block:
                parts.append(block["text"])
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Classification-aware assertions
# ---------------------------------------------------------------------------

# Behavior profile per classification. These come from VIRTUAL_SERVICE_AGENT.md §2
# and VSA_TENANT_CONFIG.md §3, and from supervisor.py system prompt rules.
#
# For each P-level we assert:
#   - must_contain_any: phrases that must appear somewhere in final narration
#   - must_not_contain: phrases that must never appear (classification leaks,
#     softening language)
#   - book_required: whether book MUST be called before session ends
#   - book_actions_required: if book is called, at least one of these actions
#     must be in customerActionsTaken

_CLASSIFICATION_PROFILES: dict[str, dict[str, Any]] = {
    "P0": {
        "must_contain_any": ["pull over", "pull-over", "pull off", "stop driving",
                             "stop the vehicle", "roadside", "stop immediately"],
        # Softening phrases that would undermine a P0 call. We do NOT forbid
        # mentions of "P1/P2/P3" — Claude legitimately references the tier
        # system when explaining the classification. We also do NOT forbid
        # "at reduced speed" as a substring — it often appears in a
        # prohibitive sense ("do not continue even at reduced speed").
        "must_not_contain": ["should be fine", "don't worry", "no action needed",
                             "within safe range", "you can continue"],
        "book_required": True,
        # Roadside is NOT a customer action on P0: nobody asked the driver
        # (review C2W2, spec 2026-09-23-cvx-escalation-recommend-not-act).
        "book_actions_required": ["create-ticket"],
    },
    "P1": {
        "must_contain_any": ["P1", "service", "48 hour", "48 hr", "soon"],
        "must_not_contain": ["no action needed", "pull over right now",
                             "stop driving now"],
        "book_required": False,  # Only if customer confirms
        "book_actions_required": [],
    },
    "P2": {
        "must_contain_any": ["service", "schedule", "week"],
        "must_not_contain": ["pull over", "stop driving"],
        "book_required": False,
        "book_actions_required": [],
    },
    "P3": {
        "must_contain_any": ["monitor", "no action", "within", "tolerance", "P3",
                             "keep an eye"],
        "must_not_contain": ["pull over", "stop driving", "48 hours"],
        "book_required": False,
        "book_actions_required": [],
    },
}


def _assert_triage_first(recorder: _ToolRecorder) -> None:
    assert recorder.calls, "Expected at least one tool call; got none"
    assert recorder.calls[0].name == "triage", (
        f"First tool call must be triage. Actual sequence: {recorder.names()}"
    )


def _assert_narration_matches_classification(classification: str, text: str) -> None:
    profile = _CLASSIFICATION_PROFILES.get(classification)
    assert profile, f"Unknown classification {classification!r} from triage"

    lower = text.lower()

    must_any = profile["must_contain_any"]
    assert any(n.lower() in lower for n in must_any), (
        f"[{classification}] Expected any of {must_any} in narration. Got:\n{text}"
    )

    for forbidden in profile["must_not_contain"]:
        assert forbidden.lower() not in lower, (
            f"[{classification}] Forbidden phrase {forbidden!r} in narration:\n{text}"
        )


def _assert_book_consistency(
    classification: str,
    recorder: _ToolRecorder,
    scenario: dict,
) -> None:
    """If book was called, its inputs must match the classification.
    If P0, book must have been called (roadside dispatch is mandatory).
    For other levels, book depends on whether the scenario had the customer
    agree to a ticket — honoring the 'book only after confirmation' rule.
    """
    profile = _CLASSIFICATION_PROFILES[classification]
    book_calls = [c for c in recorder.calls if c.name == "book"]

    if profile["book_required"]:
        assert book_calls, (
            f"[{classification}] Expected book() to be called (required for this "
            f"classification). Actual sequence: {recorder.names()}"
        )

    # If the scenario explicitly expects a book call, enforce it.
    if scenario.get("expect_book_call"):
        assert book_calls, (
            f"Scenario '{scenario['name']}' expects book to be called. "
            f"Actual sequence: {recorder.names()}"
        )

    # If book was called, check actions align with classification.
    for call in book_calls:
        actions = call.args.get("customerActionsTaken") or []
        required = profile["book_actions_required"]
        if required:
            overlap = set(actions) & set(required)
            assert overlap, (
                f"[{classification}] book called with actions {actions} but "
                f"classification requires at least one of {required}"
            )
        # The VIN must match the session VIN.
        assert call.args.get("vin") == scenario["vin"], (
            f"book called with vin={call.args.get('vin')!r}, expected {scenario['vin']!r}"
        )


def _assert_forbidden_tools(scenario: dict, recorder: _ToolRecorder) -> None:
    forbidden = set(scenario.get("forbidden_tools", []))
    fired = set(recorder.names())
    leaked = forbidden & fired
    # For forbidden_tools we only enforce when classification matches.
    # E.g. "forbidden: [book]" in the P3 scenario only applies if triage
    # actually returned P3. Caller passes a filtered scenario.
    assert not leaked, (
        f"Forbidden tools fired: {leaked}. Actual sequence: {list(fired)}"
    )


# ---------------------------------------------------------------------------
# The test
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("scenario", _load_scenarios(), ids=lambda s: s["name"])
def test_scenario(scenario: dict, request) -> None:
    config = Config.from_env()
    jwt = get_cached_id_token(config)
    vin = scenario.get("vin", config.default_vin)
    ctx = SessionContext.new(tenant_id=config.tenant_id, jwt=jwt, persona_id="fleet_driver", vin=vin)

    agent = build_supervisor(ctx, config)

    recorder = _ToolRecorder()
    agent.add_hook(recorder.on_before_tool, BeforeToolCallEvent)

    final_text = ""
    for user_turn in scenario["user_turns"]:
        result = agent(user_turn)
        final_text = _extract_text(result)
    # Collect ALL assistant text across the entire session from
    # agent.messages. Pre-tool narration (like the pull-over instruction
    # on P0) is often delivered BEFORE a tool call and would be lost if
    # we only checked the final AgentResult. Walking agent.messages
    # captures every assistant text block, whether it came before a
    # tool call, between tool calls, or as the final response.
    all_text = _collect_all_assistant_text(agent)

    # Core assertions (classification-independent).
    _assert_triage_first(recorder)

    triage_call = recorder.first_call("triage")
    assert triage_call and triage_call.args.get("vin") == vin, (
        f"triage called with vin={triage_call.args.get('vin') if triage_call else None!r}, "
        f"expected {vin!r}"
    )

    classification = ctx.triage_classification
    assert classification in _CLASSIFICATION_PROFILES, (
        f"Triage returned unknown classification {classification!r}"
    )

    # Log what actually happened so failures are debuggable.
    print(f"\n=== {scenario['name']} ===")
    print(f"Triage returned: {classification}")
    print(f"Tool sequence:   {recorder.names()}")
    print(f"Final narration: {final_text[:300]}...")

    # Behavioral assertions gated on what triage actually said.
    _assert_narration_matches_classification(classification, all_text)
    _assert_book_consistency(classification, recorder, scenario)

    # Scenario-level forbidden tools still apply only when relevant.
    # A scenario can scope forbidden_tools to a classification via
    # "forbidden_tools_if": {"P3": ["book"]}.
    conditional_forbidden = scenario.get("forbidden_tools_if", {}).get(classification, [])
    fired = set(recorder.names())
    leaked = set(conditional_forbidden) & fired
    assert not leaked, (
        f"[{classification}] Forbidden tools fired: {leaked}. "
        f"Sequence: {list(fired)}"
    )
