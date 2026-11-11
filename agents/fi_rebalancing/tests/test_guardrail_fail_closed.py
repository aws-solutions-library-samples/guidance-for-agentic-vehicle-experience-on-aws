"""Tests — guardrail fail-closed on unknown action (T1.3 item 8, fi_rebalancing).

``_apply_guardrail`` blocks when action is not exactly ``"NONE"``.
Any action value other than ``"NONE"`` (including ``None``, ``""``,
``"GUARDRAIL_UNKNOWN"`` or a missing key) blocks the text.  ``GUARDRAIL_INTERVENED``
additionally increments ``stats.guardrail_interventions``; unknown-action blocks
do NOT increment the counter (counters count GUARDRAIL_INTERVENED only).
"""
from __future__ import annotations

import json

import pytest

from agents.fi_rebalancing.llm import ModelStats, plan_rebalance


# ---------------------------------------------------------------------------
# Shared bundle (passes validate_rebalance_plan with default policy from conftest)
# ---------------------------------------------------------------------------
_LOC_A = "loc-a"
_LOC_B = "loc-b"
_WEEK = "2026-W42"

_BUNDLE = {
    "candidates": [{
        "candidate_id": "cand-1",
        "from_location_id": _LOC_B,
        "to_location_id": _LOC_A,
        "count": 2,
        "eligible_vins": ["VIN-FC-001", "VIN-FC-002"],
    }],
    "gap_ids": [f"{_LOC_A}/{_WEEK}"],
    "gap_lookup": {f"{_LOC_A}/{_WEEK}": (_LOC_A, _WEEK)},
    "sources_by_id": {
        # supply=10, op=70h: util after 1 removed ≈ 0.64 (well under 0.85 default cap)
        _LOC_B: {"supply_vehicles": 10, "op_hours": 70.0, "forecast_hours": 400.0},
    },
    "events": [],
    "evidence_kinds": ["rebalance.forecast"],
    "item_context": {"metro": "test-metro"},
}

_PII_TEXT = "SECRET-DETAIL-MUST-NOT-REACH-FINDING"


def _valid_tool_resp(detail: str = "Clean move detail for test.") -> dict:
    return {
        "output": {"message": {"content": [{"toolUse": {
            "toolUseId": "tid-fc",
            "name": "record_rebalance_plan",
            "input": {
                "moves": [{
                    "candidate_id": "cand-1",
                    "vehicle_vins": ["VIN-FC-001"],
                    "timing": "today",
                    "detail": detail,
                    "evidence_refs": ["rebalance.forecast"],
                }],
                "holds": [],
            },
        }}]}},
        "usage": {"inputTokens": 10, "outputTokens": 5},
    }


class _FakeBedrock:
    def __init__(self, guardrail_action: str) -> None:
        self._action = guardrail_action
        self.converse_calls: int = 0

    def converse(self, **kwargs) -> dict:
        self.converse_calls += 1
        return _valid_tool_resp(_PII_TEXT)

    def apply_guardrail(self, **kwargs) -> dict:
        return {"action": self._action}


def _run_with_action(action: str) -> tuple[dict, ModelStats]:
    bedrock = _FakeBedrock(action)
    stats = ModelStats()
    result = plan_rebalance(
        bedrock_client=bedrock,
        metro_bundle=_BUNDLE,
        used_vins_global=set(),
        rules_fn=lambda _exc=None: {"moves": [], "holds": []},
        stats=stats,
        model_id="MID",
        guardrail_id="GID",
        guardrail_version="DRAFT",
    )
    return result, stats


def test_unknown_action_blocks_model_text_rebalancing():
    """Unknown guardrail action must block; plan falls back to rules."""
    result, stats = _run_with_action("GUARDRAIL_UNKNOWN")
    assert result["diagnosis_source"] == "rules", (
        f"Unknown guardrail action must fall back to rules; "
        f"got diagnosis_source={result['diagnosis_source']!r}"
    )
    assert _PII_TEXT not in json.dumps(result), (
        "Blocked model text must not appear in the result"
    )


def test_missing_action_key_blocks_rebalancing():
    """No 'action' key → resp.get('action') is None → must block."""
    class _NoBedrock:
        converse_calls = 0
        def converse(self, **kw):
            self.converse_calls += 1
            return _valid_tool_resp()
        def apply_guardrail(self, **kw):
            return {}  # no 'action' key → None

    bedrock = _NoBedrock()
    stats = ModelStats()
    result = plan_rebalance(
        bedrock_client=bedrock,
        metro_bundle=_BUNDLE,
        used_vins_global=set(),
        rules_fn=lambda _exc=None: {"moves": [], "holds": []},
        stats=stats,
        model_id="MID",
        guardrail_id="GID",
        guardrail_version="DRAFT",
    )
    assert result["diagnosis_source"] == "rules"


def test_empty_string_action_blocks_rebalancing():
    """Empty string action must block (not pass through)."""
    result, stats = _run_with_action("")
    assert result["diagnosis_source"] == "rules"


def test_unknown_action_does_not_increment_intervention_counter():
    """Unknown action blocks but must NOT increment guardrail_interventions.

    Only GUARDRAIL_INTERVENED increments the counter; unknown-action blocks do not.
    """
    result, stats = _run_with_action("GUARDRAIL_UNKNOWN")
    assert result["diagnosis_source"] == "rules", (
        "Unknown action must fall back to rules (text blocked)"
    )
    assert stats.guardrail_interventions == 0, (
        "Unknown action must NOT increment guardrail_interventions "
        f"(only GUARDRAIL_INTERVENED does); got {stats.guardrail_interventions}"
    )
