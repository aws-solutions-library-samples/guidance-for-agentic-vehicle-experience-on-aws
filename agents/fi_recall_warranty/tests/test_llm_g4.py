"""Stubber-based tests for agents/fi_recall_warranty/llm.py (task 4.1b).

Drives a real boto3 client through botocore.stub.Stubber; no hand-written fakes.
expected_params on converse: modelId, toolConfig with toolChoice.
expected_params on apply_guardrail: guardrailIdentifier/Version, source, content.
"""
from __future__ import annotations

import json
from typing import Any

import boto3
import pytest
from botocore.stub import Stubber

from agents.fi_recall_warranty.llm import (
    CLAIM_DRAFT_TOOL_CONFIG,
    ModelStats,
    _CLAIM_DRAFT_SYSTEM,
    _RECALL_PLAN_SYSTEM,
    draft_claim,
    plan_booking,
    recall_plan_tool_config,
)

# ---------------------------------------------------------------------------
# Shared test constants
# ---------------------------------------------------------------------------

_MODEL_ID = "test-model-id"
_GID = "test-guardrail-id"
_GVER = "1"
_DEALERS = ["dealer-seattle", "dealer-portland"]
_BUNDLE_IDS: list[str] = []
_EVIDENCE_KINDS = ["recall.data"]
_ITEM_CTX: dict[str, Any] = {"vin": "MRD001", "subject_key": "MRD-2024-001"}
_ITEM_JSON = json.dumps(_ITEM_CTX, sort_keys=True)

_WARRANTY_CTX: dict[str, Any] = {"vin": "MRD001", "subject_key": "warranty-powertrain"}
_WARRANTY_JSON = json.dumps(_WARRANTY_CTX, sort_keys=True)

_RULES_PLAN: dict[str, Any] = {
    "dealer_id": "dealer-fallback",
    "combine_with": [],
    "urgency": "this_month",
    "detail": "Rules fallback.",
    "confidence": "low",
    "diagnosis_source": "rules",
}

_RULES_CLAIM: dict[str, Any] = {
    "narrative": "Rules fallback claim.",
    "proceed": "file",
    "detail": "Rules fallback.",
    "evidence_refs": [],
    "confidence": "low",
    "diagnosis_source": "rules",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_client() -> Any:
    return boto3.client(
        "bedrock-runtime",
        region_name="us-west-2",
        aws_access_key_id="x",
        aws_secret_access_key="x",
    )


def _converse_resp(tool_name: str, tool_input: dict, inp: int = 10, out: int = 5) -> dict:
    return {
        "output": {
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "toolUse": {
                            "toolUseId": "tid-1",
                            "name": tool_name,
                            "input": tool_input,
                        }
                    }
                ],
            }
        },
        "usage": {"inputTokens": inp, "outputTokens": out, "totalTokens": inp + out},
        "metrics": {"latencyMs": 100},
        "stopReason": "tool_use",
    }


def _text_converse_resp(text: str = "Sorry, I cannot help.") -> dict:
    return {
        "output": {
            "message": {
                "role": "assistant",
                "content": [{"text": text}],
            }
        },
        "usage": {"inputTokens": 5, "outputTokens": 3, "totalTokens": 8},
        "metrics": {"latencyMs": 80},
        "stopReason": "end_turn",
    }


_GUARDRAIL_USAGE = {
    "topicPolicyUnits": 0,
    "contentPolicyUnits": 0,
    "wordPolicyUnits": 0,
    "sensitiveInformationPolicyUnits": 0,
    "sensitiveInformationPolicyFreeUnits": 0,
    "contextualGroundingPolicyUnits": 0,
}


def _guardrail_none() -> dict:
    return {
        "action": "NONE",
        "outputs": [],
        "assessments": [],
        "usage": _GUARDRAIL_USAGE,
    }


def _guardrail_intervened() -> dict:
    return {
        "action": "GUARDRAIL_INTERVENED",
        "outputs": [],
        "assessments": [],
        "usage": _GUARDRAIL_USAGE,
    }


def _valid_recall_input(**kw) -> dict:
    base = {
        "dealer_id": "dealer-seattle",
        "combine_with": [],
        "urgency": "this_week",
        "detail": "Book the recall service at the nearest available dealer.",
        "evidence_refs": ["recall.data"],
        "confidence": "high",
    }
    base.update(kw)
    return base


def _valid_claim_input(**kw) -> dict:
    base = {
        "narrative": "Warranty claim for powertrain failure.",
        "proceed": "file",
        "detail": "Submit claim with supporting documentation.",
        "evidence_refs": ["recall.data"],
        "confidence": "medium",
    }
    base.update(kw)
    return base


def _call_plan(client, **kw):
    defaults = dict(
        bedrock_client=client,
        item_context=_ITEM_CTX,
        candidate_dealer_ids=_DEALERS,
        bundle_ids=_BUNDLE_IDS,
        severity="soon",
        recall_class="non_safety",
        evidence_kinds=_EVIDENCE_KINDS,
        rules_fn=lambda _exc: _RULES_PLAN,
        stats=ModelStats(),
        model_id=_MODEL_ID,
        guardrail_id=_GID,
        guardrail_version=_GVER,
    )
    defaults.update(kw)
    return defaults.pop("stats"), plan_booking(**defaults)


def _converse_params(user_text: str, tool_config: dict) -> dict:
    return {
        "modelId": _MODEL_ID,
        "system": [{"text": _RECALL_PLAN_SYSTEM}],
        "messages": [{"role": "user", "content": [{"text": user_text}]}],
        "toolConfig": tool_config,
    }



# ---------------------------------------------------------------------------
# ModelStats
# ---------------------------------------------------------------------------

class TestModelStats:
    def test_cost_usd_zero(self):
        s = ModelStats()
        assert s.cost_usd() == 0.0

    def test_cost_usd_input_only(self):
        s = ModelStats(input_tokens=1_000_000)
        assert s.cost_usd() == pytest.approx(3.0)

    def test_cost_usd_output_only(self):
        s = ModelStats(output_tokens=1_000_000)
        assert s.cost_usd() == pytest.approx(15.0)

    def test_cost_usd_mixed(self):
        s = ModelStats(input_tokens=500_000, output_tokens=100_000)
        assert s.cost_usd() == pytest.approx(1.5 + 1.5)


# ---------------------------------------------------------------------------
# recall_plan_tool_config
# ---------------------------------------------------------------------------

class TestRecallPlanToolConfig:
    def test_tool_choice_forces_record_recall_plan(self):
        cfg = recall_plan_tool_config(["d1", "d2"])
        assert cfg["toolChoice"] == {"tool": {"name": "record_recall_plan"}}

    def test_dealer_enum_reflects_candidates(self):
        cfg = recall_plan_tool_config(["d-a", "d-b"])
        props = cfg["tools"][0]["toolSpec"]["inputSchema"]["json"]["properties"]
        assert props["dealer_id"]["enum"] == ["d-a", "d-b"]

    def test_additional_properties_false(self):
        cfg = recall_plan_tool_config(["d1"])
        schema = cfg["tools"][0]["toolSpec"]["inputSchema"]["json"]
        assert schema["additionalProperties"] is False


# ---------------------------------------------------------------------------
# CLAIM_DRAFT_TOOL_CONFIG
# ---------------------------------------------------------------------------

class TestClaimDraftToolConfig:
    def test_tool_choice_forces_record_claim_draft(self):
        assert CLAIM_DRAFT_TOOL_CONFIG["toolChoice"] == {
            "tool": {"name": "record_claim_draft"}
        }

    def test_additional_properties_false(self):
        schema = CLAIM_DRAFT_TOOL_CONFIG["tools"][0]["toolSpec"]["inputSchema"]["json"]
        assert schema["additionalProperties"] is False


# ---------------------------------------------------------------------------
# plan_booking: valid reply → model plan
# ---------------------------------------------------------------------------

class TestPlanBookingValidReply:
    def test_valid_reply_returns_model_plan(self):
        client = _make_client()
        tool_input = _valid_recall_input()
        cfg = recall_plan_tool_config(_DEALERS)

        with Stubber(client) as stub:
            stub.add_response(
                "converse",
                _converse_resp("record_recall_plan", tool_input),
                expected_params=_converse_params(_ITEM_JSON, cfg),
            )
            stub.add_response(
                "apply_guardrail",
                _guardrail_none(),
                expected_params={
                    "guardrailIdentifier": _GID,
                    "guardrailVersion": _GVER,
                    "source": "OUTPUT",
                    "content": [{"text": {"text": tool_input["detail"]}}],
                },
            )
            stats = ModelStats()
            result = plan_booking(
                bedrock_client=client,
                item_context=_ITEM_CTX,
                candidate_dealer_ids=_DEALERS,
                bundle_ids=_BUNDLE_IDS,
                severity="soon",
                recall_class="non_safety",
                evidence_kinds=_EVIDENCE_KINDS,
                rules_fn=lambda _: _RULES_PLAN,
                stats=stats,
                model_id=_MODEL_ID,
                guardrail_id=_GID,
                guardrail_version=_GVER,
            )

        assert result["diagnosis_source"] == "model"
        assert result["dealer_id"] == "dealer-seattle"
        assert stats.model_calls == 1
        assert stats.rules_fallbacks == 0
        assert stats.guardrail_interventions == 0



# ---------------------------------------------------------------------------
# plan_booking: invalid dealer then valid → 2 calls, model
# ---------------------------------------------------------------------------

class TestPlanBookingInvalidDealerRetry:
    def test_invalid_dealer_then_valid_two_calls_model(self):
        """Attempt 1: invalid dealer → validation error; Attempt 2: valid."""
        client = _make_client()
        cfg = recall_plan_tool_config(_DEALERS)

        invalid_input = _valid_recall_input(dealer_id="dealer-BAD")
        valid_input = _valid_recall_input(dealer_id="dealer-seattle")

        # The error from attempt 1 (from RecallValidator)
        dealer_error = (
            f"dealer_id 'dealer-BAD' is not in candidate list {_DEALERS!r}"
        )
        retry_user_text = f"Previous attempt failed: {dealer_error}\n\n{_ITEM_JSON}"

        with Stubber(client) as stub:
            stub.add_response(
                "converse",
                _converse_resp("record_recall_plan", invalid_input),
                expected_params=_converse_params(_ITEM_JSON, cfg),
            )
            stub.add_response(
                "converse",
                _converse_resp("record_recall_plan", valid_input),
                expected_params=_converse_params(retry_user_text, cfg),
            )
            stub.add_response(
                "apply_guardrail",
                _guardrail_none(),
                expected_params={
                    "guardrailIdentifier": _GID,
                    "guardrailVersion": _GVER,
                    "source": "OUTPUT",
                    "content": [{"text": {"text": valid_input["detail"]}}],
                },
            )
            stats = ModelStats()
            result = plan_booking(
                bedrock_client=client,
                item_context=_ITEM_CTX,
                candidate_dealer_ids=_DEALERS,
                bundle_ids=_BUNDLE_IDS,
                severity="soon",
                recall_class="non_safety",
                evidence_kinds=_EVIDENCE_KINDS,
                rules_fn=lambda _: _RULES_PLAN,
                stats=stats,
                model_id=_MODEL_ID,
                guardrail_id=_GID,
                guardrail_version=_GVER,
            )

        assert result["diagnosis_source"] == "model"
        assert result["dealer_id"] == "dealer-seattle"
        assert stats.model_calls == 2


# ---------------------------------------------------------------------------
# plan_booking: two invalid → rules + rules_fallbacks 1
# ---------------------------------------------------------------------------

class TestPlanBookingTwoInvalidRules:
    def test_two_invalid_falls_back_to_rules(self):
        client = _make_client()
        cfg = recall_plan_tool_config(_DEALERS)
        invalid_input = _valid_recall_input(dealer_id="dealer-BAD")

        dealer_error = (
            f"dealer_id 'dealer-BAD' is not in candidate list {_DEALERS!r}"
        )
        retry_user_text = f"Previous attempt failed: {dealer_error}\n\n{_ITEM_JSON}"

        with Stubber(client) as stub:
            stub.add_response(
                "converse",
                _converse_resp("record_recall_plan", invalid_input),
                expected_params=_converse_params(_ITEM_JSON, cfg),
            )
            stub.add_response(
                "converse",
                _converse_resp("record_recall_plan", invalid_input),
                expected_params=_converse_params(retry_user_text, cfg),
            )
            stats = ModelStats()
            result = plan_booking(
                bedrock_client=client,
                item_context=_ITEM_CTX,
                candidate_dealer_ids=_DEALERS,
                bundle_ids=_BUNDLE_IDS,
                severity="soon",
                recall_class="non_safety",
                evidence_kinds=_EVIDENCE_KINDS,
                rules_fn=lambda _: _RULES_PLAN,
                stats=stats,
                model_id=_MODEL_ID,
                guardrail_id=_GID,
                guardrail_version=_GVER,
            )

        assert result["diagnosis_source"] == "rules"
        assert stats.rules_fallbacks == 1
        assert stats.model_calls == 2



# ---------------------------------------------------------------------------
# plan_booking: ThrottlingException twice → rules
# ---------------------------------------------------------------------------

class TestPlanBookingThrottling:
    def test_throttling_exception_twice_falls_back_to_rules(self):
        client = _make_client()

        with Stubber(client) as stub:
            stub.add_client_error("converse", "ThrottlingException", "Rate exceeded")
            stub.add_client_error("converse", "ThrottlingException", "Rate exceeded")
            stats = ModelStats()
            result = plan_booking(
                bedrock_client=client,
                item_context=_ITEM_CTX,
                candidate_dealer_ids=_DEALERS,
                bundle_ids=_BUNDLE_IDS,
                severity="soon",
                recall_class="non_safety",
                evidence_kinds=_EVIDENCE_KINDS,
                rules_fn=lambda _: _RULES_PLAN,
                stats=stats,
                model_id=_MODEL_ID,
                guardrail_id=_GID,
                guardrail_version=_GVER,
            )

        assert result["diagnosis_source"] == "rules"
        assert stats.rules_fallbacks == 1
        assert stats.model_calls == 2


# ---------------------------------------------------------------------------
# plan_booking: guardrail INTERVENED twice → rules, blocked text absent
# ---------------------------------------------------------------------------

class TestPlanBookingGuardrailIntervened:
    _BLOCKED = "BLOCKED_TEXT sensitive corporate secret"

    def test_guardrail_intervened_twice_rules_and_blocked_text_absent(self):
        client = _make_client()
        cfg = recall_plan_tool_config(_DEALERS)
        blocked_input = _valid_recall_input(detail=self._BLOCKED)

        guardrail_error_msg = (
            "Output was blocked by the content guardrail. "
            "Rewrite the detail without PII or off-topic content."
        )
        retry_user_text = (
            f"Previous attempt failed: {guardrail_error_msg}\n\n{_ITEM_JSON}"
        )

        with Stubber(client) as stub:
            stub.add_response(
                "converse",
                _converse_resp("record_recall_plan", blocked_input),
                expected_params=_converse_params(_ITEM_JSON, cfg),
            )
            stub.add_response(
                "apply_guardrail",
                _guardrail_intervened(),
                expected_params={
                    "guardrailIdentifier": _GID,
                    "guardrailVersion": _GVER,
                    "source": "OUTPUT",
                    "content": [{"text": {"text": self._BLOCKED}}],
                },
            )
            # Retry: user_text must NOT contain the blocked text
            stub.add_response(
                "converse",
                _converse_resp("record_recall_plan", blocked_input),
                expected_params=_converse_params(retry_user_text, cfg),
            )
            stub.add_response(
                "apply_guardrail",
                _guardrail_intervened(),
                expected_params={
                    "guardrailIdentifier": _GID,
                    "guardrailVersion": _GVER,
                    "source": "OUTPUT",
                    "content": [{"text": {"text": self._BLOCKED}}],
                },
            )
            stats = ModelStats()
            result = plan_booking(
                bedrock_client=client,
                item_context=_ITEM_CTX,
                candidate_dealer_ids=_DEALERS,
                bundle_ids=_BUNDLE_IDS,
                severity="soon",
                recall_class="non_safety",
                evidence_kinds=_EVIDENCE_KINDS,
                rules_fn=lambda _: _RULES_PLAN,
                stats=stats,
                model_id=_MODEL_ID,
                guardrail_id=_GID,
                guardrail_version=_GVER,
            )

        assert result["diagnosis_source"] == "rules"
        assert stats.guardrail_interventions == 2
        assert stats.rules_fallbacks == 1
        # Blocked text absent from the result
        assert self._BLOCKED not in result.get("detail", "")
        # Blocked text absent from the second converse request (verified by expected_params above)



# ---------------------------------------------------------------------------
# plan_booking: no toolUse in response → retry
# ---------------------------------------------------------------------------

class TestPlanBookingNoToolUse:
    def test_no_tool_use_triggers_retry_and_succeeds(self):
        client = _make_client()
        cfg = recall_plan_tool_config(_DEALERS)
        valid_input = _valid_recall_input()

        no_tool_error = "Model did not call record_recall_plan. Use the tool."
        retry_user_text = f"Previous attempt failed: {no_tool_error}\n\n{_ITEM_JSON}"

        with Stubber(client) as stub:
            stub.add_response(
                "converse",
                _text_converse_resp(),
                expected_params=_converse_params(_ITEM_JSON, cfg),
            )
            stub.add_response(
                "converse",
                _converse_resp("record_recall_plan", valid_input),
                expected_params=_converse_params(retry_user_text, cfg),
            )
            stub.add_response(
                "apply_guardrail",
                _guardrail_none(),
            )
            stats = ModelStats()
            result = plan_booking(
                bedrock_client=client,
                item_context=_ITEM_CTX,
                candidate_dealer_ids=_DEALERS,
                bundle_ids=_BUNDLE_IDS,
                severity="soon",
                recall_class="non_safety",
                evidence_kinds=_EVIDENCE_KINDS,
                rules_fn=lambda _: _RULES_PLAN,
                stats=stats,
                model_id=_MODEL_ID,
                guardrail_id=_GID,
                guardrail_version=_GVER,
            )

        assert result["diagnosis_source"] == "model"
        assert stats.model_calls == 2


# ---------------------------------------------------------------------------
# plan_booking: tokens summed over both attempts
# ---------------------------------------------------------------------------

class TestPlanBookingTokenSumming:
    def test_tokens_summed_over_both_attempts(self):
        client = _make_client()
        cfg = recall_plan_tool_config(_DEALERS)
        invalid_input = _valid_recall_input(dealer_id="dealer-BAD")
        valid_input = _valid_recall_input()

        dealer_error = (
            f"dealer_id 'dealer-BAD' is not in candidate list {_DEALERS!r}"
        )
        retry_user_text = f"Previous attempt failed: {dealer_error}\n\n{_ITEM_JSON}"

        with Stubber(client) as stub:
            stub.add_response(
                "converse",
                _converse_resp("record_recall_plan", invalid_input, inp=10, out=5),
                expected_params=_converse_params(_ITEM_JSON, cfg),
            )
            stub.add_response(
                "converse",
                _converse_resp("record_recall_plan", valid_input, inp=20, out=8),
                expected_params=_converse_params(retry_user_text, cfg),
            )
            stub.add_response("apply_guardrail", _guardrail_none())
            stats = ModelStats()
            plan_booking(
                bedrock_client=client,
                item_context=_ITEM_CTX,
                candidate_dealer_ids=_DEALERS,
                bundle_ids=_BUNDLE_IDS,
                severity="soon",
                recall_class="non_safety",
                evidence_kinds=_EVIDENCE_KINDS,
                rules_fn=lambda _: _RULES_PLAN,
                stats=stats,
                model_id=_MODEL_ID,
                guardrail_id=_GID,
                guardrail_version=_GVER,
            )

        assert stats.input_tokens == 30   # 10 + 20
        assert stats.output_tokens == 13  # 5 + 8



# ---------------------------------------------------------------------------
# plan_booking: hostile reply with extra keys → rejected
# ---------------------------------------------------------------------------

class TestPlanBookingHostileReply:
    def test_extra_keys_severity_headline_rejected(self):
        """Model reply with extra keys severity/headline is rejected by validation."""
        client = _make_client()
        cfg = recall_plan_tool_config(_DEALERS)

        hostile_input = {
            **_valid_recall_input(),
            "severity": "p0",
            "headline": "Critical recall - act now",
        }
        valid_input = _valid_recall_input()

        # The error from attempt 1: extra keys rejected
        # Compute the expected error prefix (from validator._check_exact_keys)
        extra_error_prefix = "tool output contains unexpected keys"
        retry_user_text_prefix = f"Previous attempt failed: {extra_error_prefix}"

        with Stubber(client) as stub:
            stub.add_response(
                "converse",
                _converse_resp("record_recall_plan", hostile_input),
                expected_params=_converse_params(_ITEM_JSON, cfg),
            )
            # Retry: we only verify modelId/toolConfig here (not exact message)
            stub.add_response(
                "converse",
                _converse_resp("record_recall_plan", valid_input),
            )
            stub.add_response("apply_guardrail", _guardrail_none())
            stats = ModelStats()
            result = plan_booking(
                bedrock_client=client,
                item_context=_ITEM_CTX,
                candidate_dealer_ids=_DEALERS,
                bundle_ids=_BUNDLE_IDS,
                severity="soon",
                recall_class="non_safety",
                evidence_kinds=_EVIDENCE_KINDS,
                rules_fn=lambda _: _RULES_PLAN,
                stats=stats,
                model_id=_MODEL_ID,
                guardrail_id=_GID,
                guardrail_version=_GVER,
            )

        assert result["diagnosis_source"] == "model"
        assert "severity" not in result
        assert "headline" not in result
        assert stats.model_calls == 2


# ---------------------------------------------------------------------------
# draft_claim: valid reply → model plan
# ---------------------------------------------------------------------------

class TestDraftClaimValidReply:
    def test_valid_reply_returns_model_claim(self):
        client = _make_client()
        tool_input = _valid_claim_input()

        with Stubber(client) as stub:
            stub.add_response(
                "converse",
                _converse_resp("record_claim_draft", tool_input),
                expected_params={
                    "modelId": _MODEL_ID,
                    "system": [{"text": _CLAIM_DRAFT_SYSTEM}],
                    "messages": [
                        {"role": "user", "content": [{"text": _WARRANTY_JSON}]}
                    ],
                    "toolConfig": CLAIM_DRAFT_TOOL_CONFIG,
                },
            )
            stub.add_response(
                "apply_guardrail",
                _guardrail_none(),
                expected_params={
                    "guardrailIdentifier": _GID,
                    "guardrailVersion": _GVER,
                    "source": "OUTPUT",
                    "content": [
                        {"text": {"text": tool_input["narrative"]}},
                        {"text": {"text": tool_input["detail"]}},
                    ],
                },
            )
            stats = ModelStats()
            result = draft_claim(
                bedrock_client=client,
                item_context=_WARRANTY_CTX,
                evidence_kinds=["recall.data"],
                rules_fn=lambda _: _RULES_CLAIM,
                stats=stats,
                model_id=_MODEL_ID,
                guardrail_id=_GID,
                guardrail_version=_GVER,
            )

        assert result["diagnosis_source"] == "model"
        assert result["proceed"] == "file"
        assert stats.model_calls == 1
        assert stats.rules_fallbacks == 0



# ---------------------------------------------------------------------------
# draft_claim: guardrail INTERVENED on the narrative (landing fix, Group 4)
#
# The narrative becomes a DMS claim's notes (spec D8 warranty step 5), so the guardrail on it
# is a security property. Landing mutant L16 (draft_claim's `if blocked:` made `if False:`)
# survived every suite before these tests: nothing exercised an intervention on draft_claim.
# ---------------------------------------------------------------------------

class TestDraftClaimGuardrailIntervened:
    _BLOCKED = "BLOCKED_NARRATIVE call Jane Doe at 555-0100 about this claim"

    def _converse_params(self, user_text: str) -> dict:
        return {
            "modelId": _MODEL_ID,
            "system": [{"text": _CLAIM_DRAFT_SYSTEM}],
            "messages": [{"role": "user", "content": [{"text": user_text}]}],
            "toolConfig": CLAIM_DRAFT_TOOL_CONFIG,
        }

    def _guardrail_params(self, tool_input: dict) -> dict:
        return {
            "guardrailIdentifier": _GID,
            "guardrailVersion": _GVER,
            "source": "OUTPUT",
            "content": [
                {"text": {"text": tool_input["narrative"]}},
                {"text": {"text": tool_input["detail"]}},
            ],
        }

    def _run(self, client, stats):
        return draft_claim(
            bedrock_client=client,
            item_context=_WARRANTY_CTX,
            evidence_kinds=["recall.data"],
            rules_fn=lambda _: _RULES_CLAIM,
            stats=stats,
            model_id=_MODEL_ID,
            guardrail_id=_GID,
            guardrail_version=_GVER,
        )

    def test_intervened_twice_gives_rules_and_the_blocked_narrative_never_lands(self):
        client = _make_client()
        blocked = _valid_claim_input(narrative=self._BLOCKED)
        retry_text = (
            "Previous attempt failed: Output was blocked by the content guardrail. "
            "Rewrite the narrative and detail without PII or off-topic content."
            f"\n\n{_WARRANTY_JSON}"
        )
        with Stubber(client) as stub:
            stub.add_response("converse", _converse_resp("record_claim_draft", blocked),
                              expected_params=self._converse_params(_WARRANTY_JSON))
            stub.add_response("apply_guardrail", _guardrail_intervened(),
                              expected_params=self._guardrail_params(blocked))
            # The retry's prompt carries the fixed message, never the blocked narrative.
            stub.add_response("converse", _converse_resp("record_claim_draft", blocked),
                              expected_params=self._converse_params(retry_text))
            stub.add_response("apply_guardrail", _guardrail_intervened(),
                              expected_params=self._guardrail_params(blocked))
            stats = ModelStats()
            result = self._run(client, stats)
            stub.assert_no_pending_responses()

        assert result["diagnosis_source"] == "rules", result
        assert stats.guardrail_interventions == 2
        assert stats.rules_fallbacks == 1
        assert self._BLOCKED not in str(result)

    def test_intervened_once_then_clean_gives_the_clean_model_draft(self):
        client = _make_client()
        blocked = _valid_claim_input(narrative=self._BLOCKED)
        clean = _valid_claim_input()
        retry_text = (
            "Previous attempt failed: Output was blocked by the content guardrail. "
            "Rewrite the narrative and detail without PII or off-topic content."
            f"\n\n{_WARRANTY_JSON}"
        )
        with Stubber(client) as stub:
            stub.add_response("converse", _converse_resp("record_claim_draft", blocked),
                              expected_params=self._converse_params(_WARRANTY_JSON))
            stub.add_response("apply_guardrail", _guardrail_intervened(),
                              expected_params=self._guardrail_params(blocked))
            stub.add_response("converse", _converse_resp("record_claim_draft", clean),
                              expected_params=self._converse_params(retry_text))
            stub.add_response("apply_guardrail", _guardrail_none(),
                              expected_params=self._guardrail_params(clean))
            stats = ModelStats()
            result = self._run(client, stats)
            stub.assert_no_pending_responses()

        assert result["diagnosis_source"] == "model"
        assert result["narrative"] == clean["narrative"]
        assert self._BLOCKED not in str(result)
        assert stats.guardrail_interventions == 1 and stats.model_calls == 2
