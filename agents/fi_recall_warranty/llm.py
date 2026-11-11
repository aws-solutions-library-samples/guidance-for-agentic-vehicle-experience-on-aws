"""LLM helpers for fi_recall_warranty: tool configs, stats, booking/claim functions (task 4.1b).

Copied from agents/fi_cost/agent.py (decisions.md call 46: Stage B wrapper copied, not imported).
Bedrock Converse + ApplyGuardrail through run_with_validator_fallback (two total attempts, then
rules fallback; decisions.md calls 47–49).
"""
from __future__ import annotations

import dataclasses
import json
import logging
from typing import Any, Callable, Optional

from agents.fi_recall_warranty.runner import run_with_validator_fallback
from agents.fi_recall_warranty.validator import (
    ValidationError,
    validate_claim_draft,
    validate_recall_plan,
)

log = logging.getLogger(__name__)

_INPUT_PRICE_PER_MILLION: float = 3.0   # USD per 1M input tokens (Sonnet 4.6)
_OUTPUT_PRICE_PER_MILLION: float = 15.0  # USD per 1M output tokens (Sonnet 4.6)

# ---------------------------------------------------------------------------
# Tool configs (decisions.md call 48)
# ---------------------------------------------------------------------------

def recall_plan_tool_config(candidate_dealer_ids: list[str]) -> dict[str, Any]:
    """Converse toolConfig for record_recall_plan; dealer_id enum = candidate_dealer_ids."""
    return {
        "tools": [
            {
                "toolSpec": {
                    "name": "record_recall_plan",
                    "description": "Record a recall service booking plan.",
                    "inputSchema": {
                        "json": {
                            "type": "object",
                            "properties": {
                                "dealer_id": {
                                    "type": "string",
                                    "enum": list(candidate_dealer_ids),
                                },
                                "combine_with": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                },
                                "urgency": {
                                    "type": "string",
                                    "enum": ["this_week", "this_month", "next_visit"],
                                },
                                "detail": {
                                    "type": "string",
                                    "minLength": 1,
                                    "maxLength": 600,
                                },
                                "evidence_refs": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                },
                                "confidence": {
                                    "type": "string",
                                    "enum": ["high", "medium", "low"],
                                },
                            },
                            "required": [
                                "dealer_id",
                                "combine_with",
                                "urgency",
                                "detail",
                                "evidence_refs",
                                "confidence",
                            ],
                            "additionalProperties": False,
                        }
                    },
                }
            }
        ],
        "toolChoice": {"tool": {"name": "record_recall_plan"}},
    }



CLAIM_DRAFT_TOOL_CONFIG: dict[str, Any] = {
    "tools": [
        {
            "toolSpec": {
                "name": "record_claim_draft",
                "description": "Record a warranty claim draft.",
                "inputSchema": {
                    "json": {
                        "type": "object",
                        "properties": {
                            "narrative": {
                                "type": "string",
                                "minLength": 1,
                                "maxLength": 1000,
                            },
                            "proceed": {
                                "type": "string",
                                "enum": ["file", "insufficient_evidence"],
                            },
                            "detail": {
                                "type": "string",
                                "minLength": 1,
                                "maxLength": 600,
                            },
                            "evidence_refs": {
                                "type": "array",
                                "items": {"type": "string"},
                            },
                            "confidence": {
                                "type": "string",
                                "enum": ["high", "medium", "low"],
                            },
                        },
                        "required": [
                            "narrative",
                            "proceed",
                            "detail",
                            "evidence_refs",
                            "confidence",
                        ],
                        "additionalProperties": False,
                    }
                },
            }
        }
    ],
    "toolChoice": {"tool": {"name": "record_claim_draft"}},
}

# ---------------------------------------------------------------------------
# ModelStats
# ---------------------------------------------------------------------------

@dataclasses.dataclass
class ModelStats:
    """Cumulative Bedrock usage across a run (decisions.md call 46)."""

    model_calls: int = 0           # Converse requests, including retries
    input_tokens: int = 0
    output_tokens: int = 0
    guardrail_interventions: int = 0  # GUARDRAIL_INTERVENED responses only
    rules_fallbacks: int = 0

    def cost_usd(self) -> float:
        """Estimated cost at Sonnet 4.6 list prices ($3 / $15 per million tokens)."""
        return round(
            self.input_tokens * _INPUT_PRICE_PER_MILLION / 1_000_000
            + self.output_tokens * _OUTPUT_PRICE_PER_MILLION / 1_000_000,
            6,
        )


# ---------------------------------------------------------------------------
# Internal helpers (copied from fi_cost.agent — decisions.md call 46)
# ---------------------------------------------------------------------------

def _extract_tool_input(response: dict[str, Any], tool_name: str) -> Optional[dict[str, Any]]:
    """Extract the named tool's input from a Converse response, or None."""
    content = (
        response.get("output", {})
        .get("message", {})
        .get("content", [])
    )
    for item in content:
        tool_use = item.get("toolUse")
        if tool_use and tool_use.get("name") == tool_name:
            return tool_use.get("input", {})
    return None


def _summarise_blocked_policies(resp: dict[str, Any]) -> list[dict[str, str]]:
    """Return blocked-policy identifiers from an ApplyGuardrail response.

    Shape mirrors ``agents.fi_cost.agent._summarise_blocked_policies`` — copied
    not imported per decisions.md call 46 (Stage B wrapper copied, not imported).

    Each entry carries ``policy`` and ``name`` only — the configured identifier
    of the blocked item (topic name, PII entity type, content filter type).
    Never the detected text: PII ``match`` and ``wordPolicy.customWords.match``
    can carry a slice of the reply (e.g. ``Jane Doe``) and this agent log is a
    public operational surface, so they are deliberately omitted. If a reviewer
    needs the detected string they can call ApplyGuardrail themselves.
    """
    trips: list[dict[str, str]] = []
    for a in resp.get("assessments", []) or []:
        for t in (a.get("topicPolicy") or {}).get("topics") or []:
            if t.get("action") == "BLOCKED":
                trips.append({"policy": "topic", "name": t.get("name", "")})
        for p in (a.get("sensitiveInformationPolicy") or {}).get("piiEntities") or []:
            if p.get("action") == "BLOCKED":
                trips.append({"policy": "pii", "name": p.get("type", "")})
        for r in (a.get("sensitiveInformationPolicy") or {}).get("regexes") or []:
            if r.get("action") == "BLOCKED":
                trips.append({"policy": "regex", "name": r.get("name", "")})
        for c in (a.get("contentPolicy") or {}).get("filters") or []:
            if c.get("action") == "BLOCKED":
                trips.append({"policy": "content", "name": c.get("type", "")})
        for w in (a.get("wordPolicy") or {}).get("customWords") or []:
            if w.get("action") == "BLOCKED":
                trips.append({"policy": "word", "name": "custom"})
        for w in (a.get("wordPolicy") or {}).get("managedWordLists") or []:
            if w.get("action") == "BLOCKED":
                trips.append({"policy": "managed_word", "name": w.get("type", "")})
    return trips


def _apply_guardrail(
    bedrock_client: Any,
    guardrail_id: str,
    guardrail_version: str,
    texts: list[str],
    *,
    component: str = "fi_recall_warranty",
    stats: Optional["ModelStats"] = None,
) -> bool:
    """Apply the guardrail to texts.  Returns True when action is not exactly "NONE".

    Fail-closed: any action other than ``"NONE"`` (including ``None``, ``""``,
    ``"GUARDRAIL_UNKNOWN"`` or a missing key) blocks the text.
    When the guardrail returns ``GUARDRAIL_INTERVENED``, the summarised assessment
    is logged (policy type + name only, never the reply text) and
    ``stats.guardrail_interventions`` is incremented when *stats* is provided.
    Unknown-action blocks do NOT increment the counter. The ``component`` kwarg
    lets both callers tag their log line with the exact call site.
    Decisions 2026-10-10 PO note."""
    resp = bedrock_client.apply_guardrail(
        guardrailIdentifier=guardrail_id,
        guardrailVersion=guardrail_version,
        source="OUTPUT",
        content=[{"text": {"text": t}} for t in texts],
    )
    action = resp.get("action")
    if action == "GUARDRAIL_INTERVENED":
        trips = _summarise_blocked_policies(resp)
        log.warning(
            "%s: guardrail INTERVENED; policies_blocked=%s",
            component,
            json.dumps(trips, sort_keys=True),
        )
        if stats is not None:
            stats.guardrail_interventions += 1
    # Fail-closed: block unless action is exactly "NONE". Unknown/missing
    # values are blocked (T1.3 item 8).
    return action != "NONE"


# ---------------------------------------------------------------------------
# System prompts
# ---------------------------------------------------------------------------

_RECALL_PLAN_SYSTEM = (
    "You are recommending a recall service booking plan for a fleet vehicle. "
    "Call the record_recall_plan tool exactly once. "
    "Rules you must follow: "
    "dealer_id must be one of the candidates listed in the context; "
    "every combine_with id must be from the bundle listed in the context; "
    "urgency must be one of the allowed_urgencies for this item's band; "
    "every evidence_refs entry must be one of the evidence_kinds listed in the context; "
    "detail must be between 1 and 600 characters. "
    "Do not describe the safety risk, say whether the vehicle can be driven, "
    "or state coverage, verdicts, or amounts — the operator sees the recall "
    "notice verbatim. "
    # Added 2026-10-10 after stage C recall run 2026-10-10T13:08:07Z had 36 of
    # 95 model replies blocked by the guardrail's PII ADDRESS detector:
    # dealer_ids like `dealer-atlanta` were being translated to the city name
    # `Atlanta` in the detail prose, which Bedrock classifies as ADDRESS.
    # `dealer-atlanta` (hyphenated) does not trip; `Atlanta` (capitalised,
    # standalone) does. See
    # issues/2026-10-10-cost-guardrail-blocks-replies/summary.md § 'Follow-on
    # fix 2026-10-10 — recall plan replies blocked on PII ADDRESS'.
    "Reference each dealer only by its exact dealer_id value "
    "(for example dealer-atlanta). Do not translate dealer_ids to city names "
    "or add any location, address, phone number, or email."
)

_CLAIM_DRAFT_SYSTEM = (
    "You are evaluating a warranty claim for a fleet vehicle. "
    "Call the record_claim_draft tool exactly once. "
    "Rules you must follow: "
    "narrative must be between 1 and 1000 characters, in Complaint / Cause / Correction format; "
    "detail must be between 1 and 600 characters; "
    "every evidence_refs entry must be one of the evidence_kinds listed in the context. "
    "Do not describe coverage, verdicts, or amounts in narrative or detail — "
    "DMS's verdict stands; do not restate it. "
    # Added 2026-10-10 to mirror the recall-plan fix: a warranty narrative
    # that identifies a vendor or dealer by city name (e.g. 'Boston service
    # centre') trips the guardrail's PII ADDRESS detector in the same way the
    # recall path did. Keep narration anchored to the ids present in the
    # context. See issues/2026-10-10-cost-guardrail-blocks-replies/summary.md.
    "Reference each dealer or vendor only by its exact id value as given in "
    "the context. Do not add city names, addresses, phone numbers, or emails."
)

# ---------------------------------------------------------------------------
# plan_booking
# ---------------------------------------------------------------------------

def plan_booking(
    *,
    bedrock_client: Any,
    item_context: dict[str, Any],
    candidate_dealer_ids: list[str],
    bundle_ids: list[str],
    severity: str,
    recall_class: Optional[str],
    evidence_kinds: list[str],
    rules_fn: Callable[[Exception], dict[str, Any]],
    stats: ModelStats,
    model_id: str,
    guardrail_id: str,
    guardrail_version: str,
) -> dict[str, Any]:
    """Plan a recall service booking via Bedrock Converse (decisions.md calls 47–50).

    Two total attempts through run_with_validator_fallback; guardrail on detail.
    Returns tool input + diagnosis_source='model', or rules_fn result with
    diagnosis_source='rules'.  Never raises on a model or guardrail error.
    """
    tool_config = recall_plan_tool_config(candidate_dealer_ids)
    item_json = json.dumps(item_context, default=str, sort_keys=True)
    _from_model = [False]

    def _attempt(prev_error: Optional[Exception]) -> dict[str, Any]:
        user_text = (
            f"Previous attempt failed: {prev_error}\n\n{item_json}"
            if prev_error is not None
            else item_json
        )
        stats.model_calls += 1
        resp = bedrock_client.converse(
                modelId=model_id,
                system=[{"text": _RECALL_PLAN_SYSTEM}],
                messages=[{"role": "user", "content": [{"text": user_text}]}],
                toolConfig=tool_config,
            )

        usage = resp.get("usage", {})
        stats.input_tokens += usage.get("inputTokens", 0)
        stats.output_tokens += usage.get("outputTokens", 0)

        tool_input = _extract_tool_input(resp, "record_recall_plan")
        if tool_input is None:
            raise ValidationError("Model did not call record_recall_plan. Use the tool.")

        validate_recall_plan(
            tool_input,
            candidate_dealer_ids=candidate_dealer_ids,
            bundle_ids=bundle_ids,
            severity=severity,
            recall_class=recall_class,
            evidence_kinds=evidence_kinds,
        )

        try:
            blocked = _apply_guardrail(
                bedrock_client, guardrail_id, guardrail_version,
                [tool_input.get("detail", "")],
                component="fi_recall_warranty.plan_booking",
                stats=stats,
            )
        except Exception as exc:
            raise ValidationError(
                f"Guardrail check failed: {type(exc).__name__}"
            ) from exc
        if blocked:
            raise ValidationError(
                "Output was blocked by the content guardrail. "
                "Rewrite the detail without PII or off-topic content."
            )

        _from_model[0] = True
        return tool_input

    def _rules(exc: Exception) -> dict[str, Any]:
        stats.rules_fallbacks += 1
        return rules_fn(exc)

    result = run_with_validator_fallback(
        model_fn=_attempt, rules_fn=_rules, max_retries=1
    )
    if _from_model[0]:
        return {**result, "diagnosis_source": "model"}
    return result



# ---------------------------------------------------------------------------
# draft_claim
# ---------------------------------------------------------------------------

def draft_claim(
    *,
    bedrock_client: Any,
    item_context: dict[str, Any],
    evidence_kinds: list[str],
    rules_fn: Callable[[Exception], dict[str, Any]],
    stats: ModelStats,
    model_id: str,
    guardrail_id: str,
    guardrail_version: str,
) -> dict[str, Any]:
    """Draft a warranty claim via Bedrock Converse (decisions.md calls 47–50).

    Two total attempts through run_with_validator_fallback; guardrail on
    narrative and detail (warranty).  Returns tool input + diagnosis_source='model',
    or rules_fn result.  Never raises on a model or guardrail error.
    """
    item_json = json.dumps(item_context, default=str, sort_keys=True)
    _from_model = [False]

    def _attempt(prev_error: Optional[Exception]) -> dict[str, Any]:
        user_text = (
            f"Previous attempt failed: {prev_error}\n\n{item_json}"
            if prev_error is not None
            else item_json
        )
        stats.model_calls += 1
        resp = bedrock_client.converse(
                modelId=model_id,
                system=[{"text": _CLAIM_DRAFT_SYSTEM}],
                messages=[{"role": "user", "content": [{"text": user_text}]}],
                toolConfig=CLAIM_DRAFT_TOOL_CONFIG,
            )

        usage = resp.get("usage", {})
        stats.input_tokens += usage.get("inputTokens", 0)
        stats.output_tokens += usage.get("outputTokens", 0)

        tool_input = _extract_tool_input(resp, "record_claim_draft")
        if tool_input is None:
            raise ValidationError("Model did not call record_claim_draft. Use the tool.")

        validate_claim_draft(tool_input, evidence_kinds=evidence_kinds)

        try:
            blocked = _apply_guardrail(
                bedrock_client, guardrail_id, guardrail_version,
                [
                    tool_input.get("narrative", ""),
                    tool_input.get("detail", ""),
                ],
                component="fi_recall_warranty.draft_claim",
                stats=stats,
            )
        except Exception as exc:
            raise ValidationError(
                f"Guardrail check failed: {type(exc).__name__}"
            ) from exc
        if blocked:
            raise ValidationError(
                "Output was blocked by the content guardrail. "
                "Rewrite the narrative and detail without PII or off-topic content."
            )

        _from_model[0] = True
        return tool_input

    def _rules(exc: Exception) -> dict[str, Any]:
        stats.rules_fallbacks += 1
        return rules_fn(exc)

    result = run_with_validator_fallback(
        model_fn=_attempt, rules_fn=_rules, max_retries=1
    )
    if _from_model[0]:
        return {**result, "diagnosis_source": "model"}
    return result
