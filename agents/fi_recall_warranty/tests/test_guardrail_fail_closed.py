"""Tests — guardrail fail-closed on unknown action (T1.3 item 8, fi_recall_warranty).

Both ``plan_booking`` and ``draft_claim`` in llm.py block when action != "NONE".
Counters keep counting GUARDRAIL_INTERVENED only.
"""
from __future__ import annotations

import pytest

from agents.fi_recall_warranty.llm import ModelStats, plan_booking, draft_claim


# ---------------------------------------------------------------------------
# Shared fake Bedrock client
# ---------------------------------------------------------------------------

def _valid_recall_resp(detail: str = "Clean recall detail.") -> dict:
    return {
        "output": {"message": {"content": [{"toolUse": {
            "toolUseId": "tid-rw-fc",
            "name": "record_recall_plan",
            "input": {
                "dealer_id": "dealer-test-001",
                "combine_with": [],
                "urgency": "this_month",
                "detail": detail,
                "evidence_refs": ["recall.campaign"],
                "confidence": "high",
            },
        }}]}},
        "usage": {"inputTokens": 50, "outputTokens": 25},
    }


def _valid_warranty_resp(detail: str = "Clean warranty detail.") -> dict:
    return {
        "output": {"message": {"content": [{"toolUse": {
            "toolUseId": "tid-rw-fc-w",
            "name": "record_claim_draft",
            "input": {
                "narrative": "Complaint: fault. Cause: defect. Correction: replace part.",
                "proceed": "file",
                "detail": detail,
                "evidence_refs": ["warranty.ro"],
                "confidence": "high",
            },
        }}]}},
        "usage": {"inputTokens": 50, "outputTokens": 25},
    }


class _FakeBedrock:
    def __init__(self, resp_fn, guardrail_action: str) -> None:
        self._resp_fn = resp_fn
        self._action = guardrail_action
        self.converse_calls = 0

    def converse(self, **kwargs) -> dict:
        self.converse_calls += 1
        return self._resp_fn()

    def apply_guardrail(self, **kwargs) -> dict:
        return {"action": self._action}


_PII_TEXT = "RW-PII-DETAIL-SECRET-TOKEN jane.doe@example.com 555-0199"

_BOOKING_KWARGS = {
    "item_context": {"item_id": "item-test-001"},
    "candidate_dealer_ids": ["dealer-test-001"],
    "bundle_ids": [],
    "severity": "soon",
    "recall_class": "non_safety",
    "evidence_kinds": ["recall.campaign"],
    "rules_fn": lambda _exc=None: {"dealer_id": "dealer-test-001", "diagnosis_source": "rules"},
    "model_id": "MID",
    "guardrail_id": "GID",
    "guardrail_version": "DRAFT",
}

_CLAIM_KWARGS = {
    "item_context": {"item_id": "item-claim-001"},
    "evidence_kinds": ["warranty.ro"],
    "rules_fn": lambda _exc=None: {"narrative": "Fallback.", "diagnosis_source": "rules"},
    "model_id": "MID",
    "guardrail_id": "GID",
    "guardrail_version": "DRAFT",
}


# ---------------------------------------------------------------------------
# plan_booking
# ---------------------------------------------------------------------------

def test_unknown_action_blocks_plan_booking():
    """Unknown guardrail action must block plan_booking (falls back to rules)."""
    bedrock = _FakeBedrock(
        lambda: _valid_recall_resp(_PII_TEXT), "GUARDRAIL_UNKNOWN"
    )
    stats = ModelStats()
    result = plan_booking(
        bedrock_client=bedrock, stats=stats, **_BOOKING_KWARGS
    )
    assert result.get("diagnosis_source") == "rules", (
        f"Unknown action must fall back to rules; got {result.get('diagnosis_source')!r}"
    )


def test_empty_string_action_blocks_plan_booking():
    """Empty string action must block (not pass through) in plan_booking."""
    bedrock = _FakeBedrock(lambda: _valid_recall_resp(), "")
    stats = ModelStats()
    result = plan_booking(
        bedrock_client=bedrock, stats=stats, **_BOOKING_KWARGS
    )
    assert result.get("diagnosis_source") == "rules"


def test_unknown_action_counter_not_incremented_plan_booking():
    """Unknown action blocks but must NOT increment guardrail_interventions in plan_booking.

    Only GUARDRAIL_INTERVENED increments the counter.
    """
    bedrock = _FakeBedrock(lambda: _valid_recall_resp(_PII_TEXT), "GUARDRAIL_UNKNOWN")
    stats = ModelStats()
    result = plan_booking(bedrock_client=bedrock, stats=stats, **_BOOKING_KWARGS)
    assert result.get("diagnosis_source") == "rules", "Unknown action must block"
    assert stats.guardrail_interventions == 0, (
        "Unknown action must not increment guardrail_interventions "
        f"(only GUARDRAIL_INTERVENED does); got {stats.guardrail_interventions}"
    )


# ---------------------------------------------------------------------------
# draft_claim
# ---------------------------------------------------------------------------

def test_unknown_action_blocks_draft_claim():
    """Unknown guardrail action must block draft_claim (falls back to rules)."""
    bedrock = _FakeBedrock(
        lambda: _valid_warranty_resp(_PII_TEXT), "GUARDRAIL_UNKNOWN"
    )
    stats = ModelStats()
    result = draft_claim(
        bedrock_client=bedrock, stats=stats, **_CLAIM_KWARGS
    )
    assert result.get("diagnosis_source") == "rules", (
        f"Unknown action must fall back to rules; got {result.get('diagnosis_source')!r}"
    )


def test_empty_string_action_blocks_draft_claim():
    """Empty string action must block draft_claim."""
    bedrock = _FakeBedrock(lambda: _valid_warranty_resp(), "")
    stats = ModelStats()
    result = draft_claim(
        bedrock_client=bedrock, stats=stats, **_CLAIM_KWARGS
    )
    assert result.get("diagnosis_source") == "rules"


def test_unknown_action_counter_not_incremented_draft_claim():
    """Unknown action must NOT increment guardrail_interventions in draft_claim."""
    bedrock = _FakeBedrock(lambda: _valid_warranty_resp(_PII_TEXT), "GUARDRAIL_UNKNOWN")
    stats = ModelStats()
    result = draft_claim(bedrock_client=bedrock, stats=stats, **_CLAIM_KWARGS)
    assert result.get("diagnosis_source") == "rules", "Unknown action must block"
    assert stats.guardrail_interventions == 0, (
        f"Unknown action must not increment counter; got {stats.guardrail_interventions}"
    )
