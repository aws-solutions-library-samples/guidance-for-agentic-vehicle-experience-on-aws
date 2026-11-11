"""Model wrapper for fi_rebalancing (spec D8 steps 6 and 7, task 5.1).

One Bedrock Converse call per metro, through Stage B's wrapper pattern
(Claude Sonnet 4.6, the us-west-2 inference profile the Tier 2 role holds).
The model MUST call the ``record_rebalance_plan`` tool; the tool schema is
strict (``additionalProperties: false``) and allows only the fields D8 step 6
lists.  No numeric, headline, severity or confidence field is present — those
come from :mod:`agents.fi_rebalancing.formulas` and the candidate-bundle,
never from the model (spec D9).

The wrapper is copied, not imported, from
``agents/fi_recall_warranty/llm.py`` and
``agents/fi_cost/agent.py`` (decisions call 46 — each stage evolves its
own validator).  The guardrail-block log tag ``policies_blocked`` keeps the
same shape as the sister stages so a reader of the operator surface can
compare runs across all three.

The guardrail reference is **Stage B's guardrail** (``FI_COST_GUARDRAIL_ID`` /
``FI_COST_GUARDRAIL_VERSION``) per spec D8 step 7's parenthetical — the
rebalance detail text has the same PII shape as the cost detail text (short
operator-facing prose, no customer identifiers).  Creating a dedicated
rebalance guardrail is left out of scope here; it would add an extra Bedrock
resource for no added coverage.

The system prompt locks the known failure mode recorded in AVX issue
``2026-10-10-cost-guardrail-blocks-replies`` (and its 2026-10-10 recall-plan
follow-on): the model MUST reference rooftops/dealers by their exact ``loc-``
or ``dealer-`` id and never translate those ids into city names or add any
address, phone or email.  Readable names come from the screen.
"""
from __future__ import annotations

import dataclasses
import json
import logging
from typing import Any, Callable, Optional

from agents.fi_rebalancing.runner import run_with_validator_fallback
from agents.fi_rebalancing.validate import (
    ValidationError,
    validate_rebalance_plan,
)

log = logging.getLogger(__name__)

# Sonnet 4.6 list price per million tokens (same as fi_cost / fi_recall_warranty).
_INPUT_PRICE_PER_MILLION: float = 3.0
_OUTPUT_PRICE_PER_MILLION: float = 15.0

# Tool name and its allowed fields.  Public so tests can pin the schema shape
# without re-deriving it (mutation M2: adding a numeric field is caught).
TOOL_NAME: str = "record_rebalance_plan"

# D8 step 6 allowed enums (duplicated here from validate.py so the schema the
# model sees stays in lock-step with what the validator enforces).  A change
# must land in both places.
_TIMING_ENUM: tuple[str, ...] = ("today", "within_3_days")
_HOLD_REASON_ENUM: tuple[str, ...] = (
    "temporary_event", "returns_expected", "not_worth_cost", "unclear",
)


# ---------------------------------------------------------------------------
# ModelStats
# ---------------------------------------------------------------------------

@dataclasses.dataclass
class ModelStats:
    """Cumulative Bedrock usage across a rebalance run.

    Shape mirrors ``agents.fi_recall_warranty.llm.ModelStats``.  ``model_calls``
    counts every Converse request (including retries), so the ``cost_usd``
    estimate is comparable across stages.
    """

    model_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    guardrail_interventions: int = 0
    rules_fallbacks: int = 0

    def cost_usd(self) -> float:
        """Estimated cost at Sonnet 4.6 list prices."""
        return round(
            self.input_tokens * _INPUT_PRICE_PER_MILLION / 1_000_000
            + self.output_tokens * _OUTPUT_PRICE_PER_MILLION / 1_000_000,
            6,
        )


# ---------------------------------------------------------------------------
# Tool config (strict schema — D8 step 6)
# ---------------------------------------------------------------------------

def rebalance_plan_tool_config(metro_bundle: dict) -> dict[str, Any]:
    """Converse ``toolConfig`` for :data:`TOOL_NAME`.

    The schema is strict: ``additionalProperties`` is ``False`` on every object
    (mutation M2: flipping to ``True`` or adding a numeric field like ``count``
    is caught by :func:`tests.test_llm.test_tool_config_rejects_extra_fields`).
    Each candidate's ``vehicle_vins`` enum is scoped to that candidate's
    eligible list — the schema itself forbids a VIN outside the list, before
    the validator even runs (mutation M1 is still caught by the validator
    though, because the tool schema is advisory to the model and the model
    could in theory pass a VIN the schema says is disallowed).
    """
    candidate_ids = [c["candidate_id"] for c in metro_bundle["candidates"]]
    gap_ids = list(metro_bundle["gap_ids"])

    # Model-visible: constrain vehicle_vins to the union of all candidates'
    # eligible lists.  The validator does the per-candidate scoping; the schema
    # here is a readability aid, not a load-bearing control.
    eligible_union: list[str] = sorted({
        v for c in metro_bundle["candidates"] for v in c["eligible_vins"]
    })
    gap_id_enum = sorted(gap_ids)
    cand_id_enum = sorted(candidate_ids)
    evidence_kinds = sorted(metro_bundle["evidence_kinds"])

    move_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "candidate_id": {"type": "string", "enum": cand_id_enum},
            "vehicle_vins": {
                "type": "array",
                "items": {"type": "string", "enum": eligible_union},
                "minItems": 1,
            },
            "timing": {"type": "string", "enum": list(_TIMING_ENUM)},
            "detail": {"type": "string", "minLength": 1, "maxLength": 600},
            "evidence_refs": {
                "type": "array",
                "items": {"type": "string", "enum": evidence_kinds},
            },
        },
        "required": [
            "candidate_id", "vehicle_vins", "timing", "detail", "evidence_refs",
        ],
        "additionalProperties": False,
    }

    hold_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "gap_id": {"type": "string", "enum": gap_id_enum},
            "reason": {"type": "string", "enum": list(_HOLD_REASON_ENUM)},
            "detail": {"type": "string", "minLength": 1, "maxLength": 300},
        },
        "required": ["gap_id", "reason", "detail"],
        "additionalProperties": False,
    }

    return {
        "tools": [
            {
                "toolSpec": {
                    "name": TOOL_NAME,
                    "description": (
                        "Record the chosen rebalancing moves and holds for this metro. "
                        "Call the tool exactly once per invocation."
                    ),
                    "inputSchema": {
                        "json": {
                            "type": "object",
                            "properties": {
                                "moves": {"type": "array", "items": move_schema},
                                "holds": {"type": "array", "items": hold_schema},
                            },
                            "required": ["moves", "holds"],
                            "additionalProperties": False,
                        }
                    },
                }
            }
        ],
        "toolChoice": {"tool": {"name": TOOL_NAME}},
    }


# ---------------------------------------------------------------------------
# System prompt (spec D8 step 6; guardrail-lesson rules)
# ---------------------------------------------------------------------------

_REBALANCE_PLAN_SYSTEM: str = (
    "You are recommending vehicle rebalancing moves for a rental fleet within "
    "one metro. Call the record_rebalance_plan tool exactly once. "
    "Rules you must follow: "
    "every candidate_id must be in the context's bundle of candidates; "
    "every vehicle_vins entry must be in that candidate's eligible_vins; "
    "pick between 1 and the candidate's count vehicles for that move; "
    "every gap_id must be in the context's gap_ids; "
    "every evidence_refs entry must be in the context's evidence_kinds; "
    "timing must be today or within_3_days; "
    "move detail is 1 to 600 characters; hold detail is 1 to 300 characters; "
    "a hold with reason temporary_event must point at a gap in a week when "
    "an event is announced in the context. "
    "Do not include any numeric field (count, money, hours), headline, "
    "severity, or confidence in the tool input — those are computed from the "
    "candidate bundle, not from you. "
    # Guardrail lesson, AVX issue 2026-10-10-cost-guardrail-blocks-replies:
    # location names translated from `loc-x` to a city string hit the Bedrock
    # Guardrails PII ADDRESS detector; `loc-x` and `dealer-y` (hyphenated) do
    # not. Readable names come from the screen; the model only has to reason
    # about ids.
    "Reference each rooftop, location or dealer only by its exact id value "
    "(for example loc-airport-seattle or dealer-atlanta). Do not translate "
    "ids to city names or add any location, address, phone number, or email."
)


# ---------------------------------------------------------------------------
# Internal helpers (copied from fi_recall_warranty.llm — decisions call 46)
# ---------------------------------------------------------------------------

def _extract_tool_input(response: dict[str, Any], tool_name: str) -> Optional[dict[str, Any]]:
    """Extract the named tool's input from a Converse response, or ``None``."""
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

    Each entry carries only ``policy`` and ``name`` — the configured identifier
    of the blocked item (topic name, PII entity type, content filter type).
    The detected text is deliberately omitted: a PII ``match`` or a
    ``customWords.match`` can carry a slice of the reply (for example a
    person's name), and this log line is the operator-facing surface.
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
    component: str = "fi_rebalancing",
    stats: Optional["ModelStats"] = None,
) -> bool:
    """Apply the guardrail to *texts*; return ``True`` when action is not exactly ``"NONE"``.

    Fail-closed: any action value other than ``"NONE"`` (including ``None``,
    ``""``, ``"GUARDRAIL_UNKNOWN"`` or a missing key) blocks the text.
    ``GUARDRAIL_INTERVENED`` additionally logs the assessed policies (type + name
    only, never the reply text) and increments ``stats.guardrail_interventions``
    when *stats* is provided.  Unknown-action blocks do NOT increment the counter
    so the counter stays a pure measure of Bedrock-confirmed interventions.
    The *component* kwarg tags each call site.
    """
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
    # action values are treated as blocks to prevent new Bedrock action
    # strings from silently passing through as clear text (T1.3 item 8).
    return action != "NONE"


# ---------------------------------------------------------------------------
# plan_rebalance — the per-metro entry point (D8 step 6)
# ---------------------------------------------------------------------------

def plan_rebalance(
    *,
    bedrock_client: Any,
    metro_bundle: dict,
    used_vins_global: set[str],
    rules_fn: Callable[[Exception], dict[str, Any]],
    stats: ModelStats,
    model_id: str,
    guardrail_id: str,
    guardrail_version: str,
    source_util_cap: float | None = None,
) -> dict[str, Any]:
    """Plan this metro's rebalancing via Bedrock Converse.

    Two total model attempts through :func:`run_with_validator_fallback` with
    the validation error carried into the retry; the guardrail runs on each
    move's and each hold's ``detail`` field (D8 step 7).  On every model path
    failure *rules_fn* is called with the last exception and its return value
    is used instead; the returned dict's ``diagnosis_source`` is then
    ``"rules"`` instead of ``"model"``.

    *metro_bundle* has the shape documented on
    :func:`agents.fi_rebalancing.validate.validate_rebalance_plan` with one
    additional field: ``item_context`` — the dict serialised and sent to the
    model as the user turn.  It is kept separate so the validator does not
    need to understand the context representation.

    Returns ``{"moves": [...], "holds": [...], "diagnosis_source": ...}``.
    """
    tool_config = rebalance_plan_tool_config(metro_bundle)
    item_json = json.dumps(
        metro_bundle["item_context"], default=str, sort_keys=True
    )
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
            system=[{"text": _REBALANCE_PLAN_SYSTEM}],
            messages=[{"role": "user", "content": [{"text": user_text}]}],
            toolConfig=tool_config,
        )

        usage = resp.get("usage", {})
        stats.input_tokens += usage.get("inputTokens", 0)
        stats.output_tokens += usage.get("outputTokens", 0)

        tool_input = _extract_tool_input(resp, TOOL_NAME)
        if tool_input is None:
            raise ValidationError(
                f"Model did not call {TOOL_NAME}. Use the tool."
            )

        validate_rebalance_plan(
            tool_input,
            metro_bundle=metro_bundle,
            used_vins_global=used_vins_global,
            source_util_cap=source_util_cap,
        )

        # The guardrail runs on every detail string, both for moves and for
        # holds.  Any intervention raises a ValidationError and triggers the
        # single retry (then the rules path).  No reply text ever reaches the
        # log — only the policy identifiers.
        detail_texts: list[str] = []
        for m in tool_input["moves"]:
            detail_texts.append(m["detail"])
        for h in tool_input["holds"]:
            detail_texts.append(h["detail"])
        if detail_texts:
            try:
                blocked = _apply_guardrail(
                    bedrock_client, guardrail_id, guardrail_version, detail_texts,
                    component="fi_rebalancing.plan_rebalance",
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
        return {
            "moves": list(tool_input["moves"]),
            "holds": list(tool_input["holds"]),
        }

    def _rules(exc: Exception) -> dict[str, Any]:
        stats.rules_fallbacks += 1
        return rules_fn(exc)

    result = run_with_validator_fallback(
        model_fn=_attempt, rules_fn=_rules, max_retries=1
    )
    if _from_model[0]:
        return {**result, "diagnosis_source": "model"}
    return {**result, "diagnosis_source": "rules"}
