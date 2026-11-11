"""Bedrock Converse fake for the fi_quality investigation agent.

The fi_quality agent makes two forced-tool Converse calls
(``agents/fi_quality/llm.py``):

  Call 1: tool ``record_root_cause``
    Required fields: candidate_id (from enum), failure_mode_id (from enum),
    evidence_refs (array, items from EVIDENCE_KINDS), reasoning (1–1200 chars),
    confidence (from VALID_CONFIDENCE).

  Call 2: tool ``record_design_options``
    Required fields: options (array of {remedy_option_id, tradeoffs, recommended}).
    Exactly one option must be recommended=true.

This fake reads ``p["toolConfig"]["toolChoice"]["tool"]["name"]`` to decide which
call it is on, then fills the required fields from the tool schema's ``enum``
constraints.  That way the real validator (``InvestigationValidator``) accepts the
output, and ``run_investigation`` completes its full code path — including the
``write_finding`` call — without touching real Bedrock.

Install by registering with the world's ``Router``:
    router.add("bedrock-runtime", "Converse", FiQualityBedrock().converse)

The fake does NOT implement the guardrail path (``ApplyGuardrail``); the existing
``_apply_guardrail`` in ``fi_rw_world.World`` handles that correctly for all agents.
"""
from __future__ import annotations

import uuid
from typing import Any


class FiQualityBedrock:
    """Stateless fake for the fi_quality agent's forced-tool Converse calls.

    ``calls``: list of tool names called (in order); useful for test assertions.
    """

    def __init__(self) -> None:
        self.calls: list[str] = []

    def converse(self, p: dict[str, Any]) -> dict[str, Any]:
        """Fake Converse that follows the fi_quality forced-tool contract."""
        tool_name = (
            p.get("toolConfig", {}).get("toolChoice", {}).get("tool", {}).get("name", "")
        )
        self.calls.append(tool_name)

        if tool_name == "record_root_cause":
            return self._root_cause_response(p)
        if tool_name == "record_design_options":
            return self._design_options_response(p)
        # Fall back: return a minimal tool-use response for any other forced tool.
        return self._generic_response(tool_name)

    # ------------------------------------------------------------------ helpers

    def _root_cause_response(self, p: dict[str, Any]) -> dict[str, Any]:
        """Return a ``record_root_cause`` tool-use response.

        Picks the first candidate_id and failure_mode_id from the schema enums.
        evidence_refs picks the first available kind.  Confidence = "medium".
        """
        tools = p.get("toolConfig", {}).get("tools", [{}])
        schema = tools[0].get("toolSpec", {}).get("inputSchema", {}).get("json", {})
        props = schema.get("properties", {})

        candidates = props.get("candidate_id", {}).get("enum", ["cand-01"])
        failure_modes = props.get("failure_mode_id", {}).get("enum", ["fm-001"])
        ev_kinds = props.get("evidence_refs", {}).get("items", {}).get("enum",
                                                                        ["quality.pattern"])
        confidences = props.get("confidence", {}).get("enum", ["medium"])

        # Exclude "unknown" from failure_mode selection so the second model call runs.
        real_modes = [m for m in failure_modes if m != "unknown"]
        fm = real_modes[0] if real_modes else failure_modes[0]

        inp = {
            "candidate_id": candidates[0],
            "failure_mode_id": fm,
            "evidence_refs": [ev_kinds[0]],
            "reasoning": (
                "Complaint: fault reported. "
                "Cause: component failure consistent with the pattern evidence. "
                "Correction: component replacement recommended."
            ),
            "confidence": confidences[0] if confidences else "medium",
        }
        return self._tool_use_response("record_root_cause", inp)

    def _design_options_response(self, p: dict[str, Any]) -> dict[str, Any]:
        """Return a ``record_design_options`` tool-use response.

        Picks the first two option ids from ``allowed_option_ids``.  The first is
        recommended; the second is not.  Falls back to one option if fewer than two
        are available.
        """
        tools = p.get("toolConfig", {}).get("tools", [{}])
        schema = tools[0].get("toolSpec", {}).get("inputSchema", {}).get("json", {})
        props = schema.get("properties", {})
        items_schema = props.get("options", {}).get("items", {})
        allowed = items_schema.get("properties", {}).get(
            "remedy_option_id", {}).get("enum", ["opt-01"])

        options = [
            {
                "remedy_option_id": allowed[0],
                "tradeoffs": "Lowest cost option with standard lead time.",
                "recommended": True,
            },
        ]
        if len(allowed) >= 2:
            options.append({
                "remedy_option_id": allowed[1],
                "tradeoffs": "Alternative approach with longer lead time.",
                "recommended": False,
            })

        return self._tool_use_response("record_design_options", {"options": options})

    def _generic_response(self, tool_name: str) -> dict[str, Any]:
        return self._tool_use_response(tool_name, {})

    @staticmethod
    def _tool_use_response(tool_name: str, inp: dict[str, Any]) -> dict[str, Any]:
        return {
            "output": {"message": {"role": "assistant", "content": [
                {"toolUse": {
                    "toolUseId": uuid.uuid4().hex,
                    "name": tool_name,
                    "input": inp,
                }}
            ]}},
            "stopReason": "tool_use",
            "usage": {"inputTokens": 80, "outputTokens": 60, "totalTokens": 140},
            "metrics": {"latencyMs": 3},
        }
