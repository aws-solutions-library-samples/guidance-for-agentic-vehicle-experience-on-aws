"""Tests for :mod:`agents.fi_rebalancing.llm` (spec D8 steps 6-7, task 5.1).

Covers:

* **M2 (mutation)** — the tool schema is strict (``additionalProperties: false``)
  and has exactly the field set spec D8 step 6 lists. Adding a numeric field
  (``count``, ``money``, ``severity``, ``confidence``) is caught by
  :func:`test_tool_schema_has_no_numeric_or_severity_fields` and
  :func:`test_tool_schema_additional_properties_is_false_everywhere`.
* The Converse flow calls through :func:`run_with_validator_fallback` with
  ``max_retries=1`` (two total model attempts, then rules).
* On ``GUARDRAIL_INTERVENED`` the ``policies_blocked`` log line is emitted
  with ``[{policy, name}]`` entries only, never the reply text (the
  2026-10-10 guardrail-blocks-replies lesson).
* The system prompt names the rooftop/dealer-id rule and the "no city/address"
  rule verbatim (regression guard on the guardrail lesson).

The tests use a tiny fake Bedrock client so no AWS is touched.
"""
from __future__ import annotations

import json
import logging
from typing import Any

import pytest

from agents.fi_rebalancing import llm as llm_mod
from agents.fi_rebalancing.llm import (
    ModelStats,
    TOOL_NAME,
    _REBALANCE_PLAN_SYSTEM,
    plan_rebalance,
    rebalance_plan_tool_config,
)


# ---------------------------------------------------------------------------
# Bundle factory and fake Converse client
# ---------------------------------------------------------------------------
_METRO = "seattle"
_LOC_A = "loc-airport-seattle"
_LOC_B = "loc-downtown-seattle"
_WEEK = "2026-W42"


def _bundle(**over) -> dict:
    base = {
        "candidates": [
            {
                "candidate_id": "cand-1",
                "from_location_id": _LOC_B,
                "to_location_id": _LOC_A,
                "count": 2,
                "eligible_vins": ["VIN-SEA-001", "VIN-SEA-002", "VIN-SEA-003"],
            },
        ],
        "gap_ids": [f"{_LOC_A}/{_WEEK}"],
        "gap_lookup": {f"{_LOC_A}/{_WEEK}": (_LOC_A, _WEEK)},
        "sources_by_id": {
            _LOC_B: {"supply_vehicles": 10, "op_hours": 70.0, "forecast_hours": 400.0},
        },
        "events": [],
        "evidence_kinds": [
            "rebalance.forecast", "rebalance.supply", "rebalance.transfer",
            "rebalance.revenue", "rebalance.vehicle", "rebalance.recent_outcomes",
            "rebalance.events", "rebalance.diagnosis_source", "rebalance.hold",
        ],
        "item_context": {"metro": _METRO},
    }
    base.update(over)
    return base


def _tool_response(moves: list[dict], holds: list[dict],
                   usage: dict | None = None) -> dict:
    """A Converse-shaped response carrying a toolUse block for TOOL_NAME."""
    return {
        "output": {
            "message": {
                "content": [
                    {
                        "toolUse": {
                            "name": TOOL_NAME,
                            "input": {"moves": moves, "holds": holds},
                        }
                    }
                ]
            }
        },
        "usage": usage or {"inputTokens": 100, "outputTokens": 50},
    }


class _FakeBedrock:
    """Minimal Bedrock Runtime client stub: records converse/apply_guardrail calls."""

    def __init__(
        self,
        converse_outputs: list[dict],
        guardrail_outputs: list[dict] | None = None,
    ) -> None:
        self._converse_outputs = list(converse_outputs)
        self._guardrail_outputs = list(guardrail_outputs or [])
        self.converse_calls: list[dict] = []
        self.guardrail_calls: list[dict] = []

    def converse(self, **kw):
        self.converse_calls.append(kw)
        if not self._converse_outputs:
            raise RuntimeError("fake Bedrock: no more converse responses queued")
        return self._converse_outputs.pop(0)

    def apply_guardrail(self, **kw):
        self.guardrail_calls.append(kw)
        if not self._guardrail_outputs:
            return {"action": "NONE"}
        return self._guardrail_outputs.pop(0)


def _default_move() -> dict:
    return {
        "candidate_id": "cand-1",
        "vehicle_vins": ["VIN-SEA-001"],
        "timing": "today",
        "detail": "Move one vehicle to cover the morning deficit.",
        "evidence_refs": ["rebalance.forecast", "rebalance.supply"],
    }


# ---------------------------------------------------------------------------
# Tool schema shape — mutation M2 anchors
# ---------------------------------------------------------------------------
def test_tool_schema_additional_properties_is_false_everywhere():
    """M2 anchor part 1: ``additionalProperties: false`` on every object.

    Flipping any one to ``True`` admits numeric or confidence fields the model
    might emit, breaking spec D8 step 6.
    """
    cfg = rebalance_plan_tool_config(_bundle())
    root = cfg["tools"][0]["toolSpec"]["inputSchema"]["json"]
    assert root["additionalProperties"] is False, root
    move_schema = root["properties"]["moves"]["items"]
    hold_schema = root["properties"]["holds"]["items"]
    assert move_schema["additionalProperties"] is False, move_schema
    assert hold_schema["additionalProperties"] is False, hold_schema


def test_tool_schema_has_no_numeric_or_severity_fields():
    """M2 anchor part 2: the move/hold schemas have exactly D8 step 6's fields.

    Adding ``count``, ``severity``, ``headline``, ``confidence`` or any money
    key (``money``, ``net_benefit_usd``, ``transfer_cost_usd``, etc.) breaks
    this equality.  Those come from :mod:`agents.fi_rebalancing.formulas`,
    never from the model (spec D9).
    """
    cfg = rebalance_plan_tool_config(_bundle())
    move_schema = cfg["tools"][0]["toolSpec"]["inputSchema"]["json"][
        "properties"]["moves"]["items"]
    hold_schema = cfg["tools"][0]["toolSpec"]["inputSchema"]["json"][
        "properties"]["holds"]["items"]

    assert set(move_schema["properties"].keys()) == {
        "candidate_id", "vehicle_vins", "timing", "detail", "evidence_refs",
    }, move_schema["properties"].keys()
    assert set(move_schema["required"]) == {
        "candidate_id", "vehicle_vins", "timing", "detail", "evidence_refs",
    }
    assert set(hold_schema["properties"].keys()) == {
        "gap_id", "reason", "detail",
    }, hold_schema["properties"].keys()
    assert set(hold_schema["required"]) == {"gap_id", "reason", "detail"}

    # Explicit sanity: none of these forbidden keys is anywhere in the schema.
    schema_str = json.dumps(cfg, sort_keys=True)
    for forbidden in (
        '"count"', '"severity"', '"headline"', '"confidence"',
        '"money"', '"net_benefit_usd"', '"transfer_cost_usd"',
        '"estimated_value_usd"', '"predicted_on_rent_hours_gain"',
    ):
        assert forbidden not in schema_str, (forbidden, schema_str[:200])


def test_tool_choice_pins_the_tool_name():
    cfg = rebalance_plan_tool_config(_bundle())
    assert cfg["toolChoice"] == {"tool": {"name": TOOL_NAME}}


def test_candidate_id_enum_is_only_the_bundle_ids():
    cfg = rebalance_plan_tool_config(_bundle())
    move_schema = cfg["tools"][0]["toolSpec"]["inputSchema"]["json"][
        "properties"]["moves"]["items"]
    cand_id_enum = move_schema["properties"]["candidate_id"]["enum"]
    assert cand_id_enum == ["cand-1"], cand_id_enum


# ---------------------------------------------------------------------------
# System prompt — guardrail lesson pins
# ---------------------------------------------------------------------------
def test_system_prompt_requires_id_only_references():
    """Guardrail lesson (AVX issue 2026-10-10): the model MUST reference
    rooftops/dealers by their exact id and must NEVER translate to a city name
    or add an address/phone/email. Deleting any clause of the lesson fails
    this test."""
    assert "exact id value" in _REBALANCE_PLAN_SYSTEM
    assert "loc-airport-seattle" in _REBALANCE_PLAN_SYSTEM
    assert "dealer-atlanta" in _REBALANCE_PLAN_SYSTEM
    assert "city names" in _REBALANCE_PLAN_SYSTEM
    assert "address" in _REBALANCE_PLAN_SYSTEM
    assert "phone number" in _REBALANCE_PLAN_SYSTEM
    assert "email" in _REBALANCE_PLAN_SYSTEM


def test_system_prompt_forbids_numeric_fields():
    assert "numeric field" in _REBALANCE_PLAN_SYSTEM
    assert "headline" in _REBALANCE_PLAN_SYSTEM
    assert "severity" in _REBALANCE_PLAN_SYSTEM


# ---------------------------------------------------------------------------
# plan_rebalance happy path
# ---------------------------------------------------------------------------
def test_plan_rebalance_returns_model_moves_on_first_attempt():
    bedrock = _FakeBedrock(
        converse_outputs=[_tool_response(moves=[_default_move()], holds=[])],
    )
    stats = ModelStats()
    result = plan_rebalance(
        bedrock_client=bedrock,
        metro_bundle=_bundle(),
        used_vins_global=set(),
        rules_fn=lambda _exc=None: {"moves": [], "holds": []},
        stats=stats,
        model_id="us.anthropic.claude-sonnet-4-6",
        guardrail_id="GID",
        guardrail_version="DRAFT",
    )
    assert result["diagnosis_source"] == "model"
    assert len(result["moves"]) == 1
    assert len(result["holds"]) == 0
    assert stats.model_calls == 1
    assert stats.input_tokens == 100
    assert stats.output_tokens == 50
    assert stats.rules_fallbacks == 0


# ---------------------------------------------------------------------------
# Validation failure → one retry → rules
# ---------------------------------------------------------------------------
def test_validation_fails_once_then_succeeds_counts_two_calls():
    bedrock = _FakeBedrock(
        converse_outputs=[
            # First response: bad VIN → ValidationError
            _tool_response(moves=[{**_default_move(), "vehicle_vins": ["VIN-X-999"]}],
                           holds=[]),
            # Retry: good VIN
            _tool_response(moves=[_default_move()], holds=[]),
        ],
    )
    stats = ModelStats()
    rules_called = [False]

    def _rules(_exc):
        rules_called[0] = True
        return {"moves": [], "holds": []}

    result = plan_rebalance(
        bedrock_client=bedrock,
        metro_bundle=_bundle(),
        used_vins_global=set(),
        rules_fn=_rules,
        stats=stats,
        model_id="MID",
        guardrail_id="GID",
        guardrail_version="DRAFT",
    )
    assert result["diagnosis_source"] == "model"
    assert stats.model_calls == 2  # one for the bad attempt, one for the retry
    assert rules_called[0] is False


def test_two_validation_failures_fall_through_to_rules():
    bedrock = _FakeBedrock(
        converse_outputs=[
            _tool_response(moves=[{**_default_move(), "vehicle_vins": ["VIN-X-999"]}],
                           holds=[]),
            _tool_response(moves=[{**_default_move(), "vehicle_vins": ["VIN-X-999"]}],
                           holds=[]),
        ],
    )
    stats = ModelStats()

    def _rules(_exc):
        return {"moves": [], "holds": []}

    result = plan_rebalance(
        bedrock_client=bedrock,
        metro_bundle=_bundle(),
        used_vins_global=set(),
        rules_fn=_rules,
        stats=stats,
        model_id="MID",
        guardrail_id="GID",
        guardrail_version="DRAFT",
    )
    assert result["diagnosis_source"] == "rules"
    assert stats.model_calls == 2
    assert stats.rules_fallbacks == 1


def test_model_without_tool_call_is_a_validation_error():
    bedrock = _FakeBedrock(
        converse_outputs=[
            {
                "output": {"message": {"content": [{"text": "I'm not going to call the tool."}]}},
                "usage": {"inputTokens": 10, "outputTokens": 5},
            },
            # Retry still refuses
            {
                "output": {"message": {"content": [{"text": "Still not calling it."}]}},
                "usage": {"inputTokens": 10, "outputTokens": 5},
            },
        ],
    )
    stats = ModelStats()
    result = plan_rebalance(
        bedrock_client=bedrock,
        metro_bundle=_bundle(),
        used_vins_global=set(),
        rules_fn=lambda _exc=None: {"moves": [], "holds": []},
        stats=stats,
        model_id="MID",
        guardrail_id="GID",
        guardrail_version="DRAFT",
    )
    assert result["diagnosis_source"] == "rules"
    assert stats.model_calls == 2


# ---------------------------------------------------------------------------
# Guardrail
# ---------------------------------------------------------------------------
def test_guardrail_intervention_counts_and_falls_back_without_leaking_text(caplog):
    """The guardrail block logs ``policies_blocked`` with policy/name only; the
    reply detail text ``POLLUTED REPLY`` NEVER appears in the log output."""
    polluted = "POLLUTED REPLY WITH A PHONE 555-1234"
    bedrock = _FakeBedrock(
        converse_outputs=[
            _tool_response(moves=[{**_default_move(), "detail": polluted}], holds=[]),
            _tool_response(moves=[{**_default_move(), "detail": polluted}], holds=[]),
        ],
        guardrail_outputs=[
            {
                "action": "GUARDRAIL_INTERVENED",
                "assessments": [{"sensitiveInformationPolicy": {"piiEntities": [
                    {"type": "PHONE", "action": "BLOCKED"},
                ]}}],
            },
            {
                "action": "GUARDRAIL_INTERVENED",
                "assessments": [{"sensitiveInformationPolicy": {"piiEntities": [
                    {"type": "PHONE", "action": "BLOCKED"},
                ]}}],
            },
        ],
    )
    stats = ModelStats()

    caplog.set_level(logging.WARNING, logger="agents.fi_rebalancing.llm")
    result = plan_rebalance(
        bedrock_client=bedrock,
        metro_bundle=_bundle(),
        used_vins_global=set(),
        rules_fn=lambda _exc=None: {"moves": [], "holds": []},
        stats=stats,
        model_id="MID",
        guardrail_id="GID",
        guardrail_version="DRAFT",
    )
    assert result["diagnosis_source"] == "rules"
    assert stats.guardrail_interventions == 2
    assert stats.model_calls == 2

    # The policy identifier is logged; the reply text is NOT.
    log_text = "\n".join(r.getMessage() for r in caplog.records)
    assert "policies_blocked" in log_text
    assert '"PHONE"' in log_text
    assert polluted not in log_text  # no leaked reply text
    assert "555-1234" not in log_text


def test_apply_guardrail_runs_on_both_move_and_hold_detail():
    # The hold is on a second deficit (loc-hub-seattle): a hold on the move's own
    # destination is refused by the validator since 2026-10-14 (MH1).
    loc_c = "loc-hub-seattle"
    bundle = _bundle(
        gap_ids=[f"{_LOC_A}/{_WEEK}", f"{loc_c}/{_WEEK}"],
        gap_lookup={f"{_LOC_A}/{_WEEK}": (_LOC_A, _WEEK), f"{loc_c}/{_WEEK}": (loc_c, _WEEK)},
    )
    bedrock = _FakeBedrock(
        converse_outputs=[
            _tool_response(
                moves=[_default_move()],
                holds=[{
                    "gap_id": f"{loc_c}/{_WEEK}",
                    "reason": "returns_expected",
                    "detail": "Vehicles come back by 10:00.",
                }],
            ),
        ],
    )
    stats = ModelStats()
    plan_rebalance(
        bedrock_client=bedrock,
        metro_bundle=bundle,
        used_vins_global=set(),
        rules_fn=lambda _exc=None: {"moves": [], "holds": []},
        stats=stats,
        model_id="MID",
        guardrail_id="GID",
        guardrail_version="DRAFT",
    )
    assert len(bedrock.guardrail_calls) == 1
    (call,) = bedrock.guardrail_calls
    # Two text items: one move detail, one hold detail.
    assert len(call["content"]) == 2


# ---------------------------------------------------------------------------
# System prompt sent to converse
# ---------------------------------------------------------------------------
def test_converse_call_sends_the_system_prompt_and_tool_config():
    bedrock = _FakeBedrock(
        converse_outputs=[_tool_response(moves=[_default_move()], holds=[])],
    )
    stats = ModelStats()
    plan_rebalance(
        bedrock_client=bedrock,
        metro_bundle=_bundle(),
        used_vins_global=set(),
        rules_fn=lambda _exc=None: {"moves": [], "holds": []},
        stats=stats,
        model_id="MID",
        guardrail_id="GID",
        guardrail_version="DRAFT",
    )
    (call,) = bedrock.converse_calls
    assert call["modelId"] == "MID"
    assert call["system"] == [{"text": _REBALANCE_PLAN_SYSTEM}]
    assert call["toolConfig"]["toolChoice"] == {"tool": {"name": TOOL_NAME}}


# ---------------------------------------------------------------------------
# ModelStats.cost_usd
# ---------------------------------------------------------------------------
def test_cost_usd_follows_sonnet_4_6_list_prices():
    s = ModelStats(input_tokens=1_000_000, output_tokens=200_000)
    # Expected: 1,000,000 * 3/1M + 200,000 * 15/1M = 3.0 + 3.0 = 6.0
    assert s.cost_usd() == pytest.approx(6.0, abs=1e-9)



# ---------------------------------------------------------------------------
# A move and a hold on the same deficit: the retry runs, then the rules
# (user's answer 2026-10-14; validator check, mutant MH1).
# ---------------------------------------------------------------------------
_SAME_LOC_HOLD = {"gap_id": f"{_LOC_A}/{_WEEK}", "reason": "returns_expected",
                  "detail": "Vehicles are expected back; no move needed."}


def test_move_and_hold_on_same_deficit_is_retried_then_accepted_without_the_hold():
    bedrock = _FakeBedrock(converse_outputs=[
        _tool_response(moves=[_default_move()], holds=[_SAME_LOC_HOLD]),
        _tool_response(moves=[_default_move()], holds=[]),
    ])
    stats = ModelStats()
    rules_called = []
    result = plan_rebalance(
        bedrock_client=bedrock, metro_bundle=_bundle(), used_vins_global=set(),
        rules_fn=lambda exc: rules_called.append(exc) or {"moves": [], "holds": []},
        stats=stats, model_id="MID", guardrail_id="GID", guardrail_version="DRAFT",
    )
    assert stats.model_calls == 2
    assert rules_called == []
    assert result["diagnosis_source"] == "model"
    assert result["holds"] == []
    # The retry carried the refusal to the model.
    retry_text = json.dumps(bedrock.converse_calls[1]["messages"])
    assert "both a move into and a hold on" in retry_text


def test_move_and_hold_on_same_deficit_twice_falls_to_rules():
    bedrock = _FakeBedrock(converse_outputs=[
        _tool_response(moves=[_default_move()], holds=[_SAME_LOC_HOLD]),
        _tool_response(moves=[_default_move()], holds=[_SAME_LOC_HOLD]),
    ])
    stats = ModelStats()
    rules_called = []
    result = plan_rebalance(
        bedrock_client=bedrock, metro_bundle=_bundle(), used_vins_global=set(),
        rules_fn=lambda exc: rules_called.append(exc) or {"moves": [], "holds": []},
        stats=stats, model_id="MID", guardrail_id="GID", guardrail_version="DRAFT",
    )
    assert stats.model_calls == 2
    assert len(rules_called) == 1
    assert "both a move into and a hold on" in str(rules_called[0])
    assert result["diagnosis_source"] == "rules"
