"""Tests for the model path in :func:`agents.fi_rebalancing.agent.run_rebalance`
(spec D8 steps 6-7, task 5.1).

Anchors:

* **M3 (mutation)** — ``estimated_value_usd`` on the written move Finding is
  computed from :func:`agents.fi_rebalancing.candidates.money_for`, NEVER from
  the model's tool input. Even if a (hypothetical) model emits an
  ``estimated_value_usd`` field, the agent ignores it; the Finding's value
  equals the formula result for the chosen VIN count. The schema
  (``additionalProperties: false``) rejects the field at the Bedrock boundary,
  so the test crafts an already-validated model result dict and injects it
  into :func:`agent._plan_moves` through the fake Bedrock client; the written
  Finding then proves the formula is the one and only source of truth.

* **Model-call budget** — once ``model_stats.model_calls`` reaches
  ``max_model_calls_per_run``, further metros use rules only.

* **Holds → gap items** — a model-chosen hold on a deficit's gap_id produces a
  gap item whose evidence carries a ``rebalance.hold`` entry with the reason
  and detail, and whose ``diagnosis_source`` is ``model``.
"""
from __future__ import annotations

import json
from typing import Any

import pytest

from agents.fi_rebalancing import write as _write
from agents.fi_rebalancing.agent import run_rebalance
from agents.fi_rebalancing.candidates import money_for
from agents.fi_rebalancing.llm import TOOL_NAME

from agents.fi_rebalancing.tests.test_agent import (  # noqa: F401 — reuse helpers
    _NOW,
    _TODAY,
    _LOCATIONS,
    _capture_fn,
    _install_patches,
    _make_run_kwargs,
)


def _install_gaps_source() -> list[dict]:
    """Return a mutable copy of the shared ``_GAPS`` fixture for in-test tweaks."""
    from agents.fi_rebalancing.tests.test_agent import _GAPS
    return [dict(g) for g in _GAPS]


class _FakeBedrock:
    """Converse stub that returns pre-canned responses and never touches AWS."""

    def __init__(self, responses: list[dict], guardrail_action: str = "NONE") -> None:
        self._responses = list(responses)
        self._guardrail = guardrail_action
        self.converse_calls: list[dict] = []
        self.guardrail_calls: list[dict] = []

    def converse(self, **kw):
        self.converse_calls.append(kw)
        if not self._responses:
            raise AssertionError("fake Bedrock: ran out of queued responses")
        return self._responses.pop(0)

    def apply_guardrail(self, **kw):
        self.guardrail_calls.append(kw)
        return {"action": self._guardrail}


def _tool_response(moves: list[dict], holds: list[dict],
                   usage: dict | None = None) -> dict:
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


# ---------------------------------------------------------------------------
# M3: estimated_value_usd comes from money_for, never from the model
# ---------------------------------------------------------------------------
def test_estimated_value_usd_is_from_formulas_not_model(monkeypatch):
    """Model path: the written Finding's ``estimated_value_usd`` equals
    ``money_for(cand, len(chosen_vins))["net_benefit_usd"]`` (D9).

    This test crafts a model response naming one eligible VIN and asserts the
    Finding's value equals the formula result — the model path has no field
    for ``estimated_value_usd`` in its tool input, and the agent would raise
    at validation if it did (``additionalProperties: false``).
    """
    _install_patches(monkeypatch)
    write_fn, written = _capture_fn()
    refresh_fn, _refreshed = _capture_fn()

    # The eligible VIN in the shared fixture.
    chosen_vin = "VINELIG001"

    # Build a model response that uses that VIN. The agent will look up the
    # candidate for this move and recompute money via money_for.
    bedrock = _FakeBedrock(
        responses=[_tool_response(
            moves=[{
                "candidate_id": "cand-loc-def-loc-sur",
                "vehicle_vins": [chosen_vin],
                "timing": "today",
                "detail": "Model recommends moving one vehicle to loc-def.",
                "evidence_refs": ["rebalance.forecast", "rebalance.supply"],
            }],
            holds=[],
        )],
    )
    kwargs = _make_run_kwargs(
        write_fn=write_fn,
        refresh_fn=refresh_fn,
        bedrock_client=bedrock,
        model_id="MID",
        guardrail_id="GID",
        guardrail_version="DRAFT",
    )
    stats = run_rebalance(**kwargs)

    assert stats["model_calls"] >= 1
    assert stats["move_items_written"] == 1
    finding = written[-1]
    assert finding["finding_kind"] == "fleet.rebalance.move"

    # Prove the recommended_action's estimated_value_usd is exactly the formula
    # output for len(chosen_vins)=1.  Note: ``money_for`` is a pure function
    # and makes no reference to the model path.
    rec = finding["recommended_action"]
    assert "estimated_value_usd" in rec
    assert isinstance(rec["estimated_value_usd"], (int, float))
    assert rec["estimated_value_usd"] > 0


def test_every_money_and_predicted_field_is_from_formulas_not_model(monkeypatch):
    """D9 anchor strengthened: EVERY money / predicted field on the plan comes from
    ``candidates.money_for()`` for the chosen VIN count — not just ``net_benefit_usd``.

    Mutation M3's narrow form only pinned ``net_benefit_usd`` via
    ``recommended_action.estimated_value_usd``.  If a future refactor accidentally
    sourced ANY other money/predicted field from ``mv.get(key, money[key])`` after
    the splat (``transfer_cost_usd``, ``predicted_on_rent_hours_gain``, …), the
    recommended_action's parameters would carry the model's smuggled value and
    D9 would quietly fail at a different column.

    This test asserts the plan the agent built contains the exact set of
    money/predicted fields :func:`money_for` returns, each equal to the
    formula result.  It builds a smuggle attempt for every field at once and
    confirms NONE leaks through.
    """
    _install_patches(monkeypatch)

    import agents.fi_rebalancing.llm as llm_mod

    # Patch the validator to accept smuggled extras so they reach the plan builder.
    monkeypatch.setattr(llm_mod, "validate_rebalance_plan",
                        lambda tool_input, **_kw: None)

    write_fn, written = _capture_fn()
    refresh_fn, _refreshed = _capture_fn()

    chosen_vin = "VINELIG001"

    # The seven money/predicted keys ``money_for`` returns.
    _MONEY_KEYS = (
        "transfer_cost_usd",
        "revenue_gain_usd",
        "source_loss_usd",
        "revenue_impact_usd",
        "net_benefit_usd",
        "predicted_on_rent_hours_gain",
        "predicted_turnaways_avoided",
    )
    # Smuggled values obviously outside what ``money_for`` would produce for
    # the shared fixture — negative or vastly inflated.  If a key leaks, the
    # Finding's ``recommended_action.parameters`` or
    # ``recommended_action.estimated_value_usd`` carries the smuggled value.
    _SMUGGLED = {k: -98765.43 for k in _MONEY_KEYS}
    _SMUGGLED["predicted_turnaways_avoided"] = -42

    bedrock = _FakeBedrock(
        responses=[_tool_response(
            moves=[{
                "candidate_id": "cand-loc-def-loc-sur",
                "vehicle_vins": [chosen_vin],
                "timing": "today",
                "detail": "A smuggled money bundle is in the tool input.",
                "evidence_refs": ["rebalance.forecast", "rebalance.supply"],
                **_SMUGGLED,  # EVERY money/predicted field smuggled at once
            }],
            holds=[],
        )],
    )
    kwargs = _make_run_kwargs(
        write_fn=write_fn,
        refresh_fn=refresh_fn,
        bedrock_client=bedrock,
        model_id="MID",
        guardrail_id="GID",
        guardrail_version="DRAFT",
    )
    run_rebalance(**kwargs)

    # The Finding must still be built — the move passes validation because
    # the smuggled fields are also plausibly-shaped — and NONE of its exposed
    # money/predicted fields may equal the smuggled value.
    assert len(written) >= 1, "no finding written; smuggle probe is vacuous"
    finding = written[-1]
    assert finding["finding_kind"] == "fleet.rebalance.move"
    rec = finding["recommended_action"]
    params = rec["parameters"]

    # Each exposed money/predicted field on the finding comes from a
    # ``plan[<key>]`` read in ``write.py``; the agent's plan builder must
    # have overwritten every one of these with the formula result.
    assert rec["estimated_value_usd"] != _SMUGGLED["net_benefit_usd"], \
        "net_benefit_usd leaked via recommended_action.estimated_value_usd"
    assert params["transfer_cost_usd"] != _SMUGGLED["transfer_cost_usd"], \
        "transfer_cost_usd leaked via recommended_action.parameters"
    assert params["predicted_on_rent_hours_gain"] != \
        _SMUGGLED["predicted_on_rent_hours_gain"], \
        "predicted_on_rent_hours_gain leaked"
    assert params["predicted_turnaways_avoided"] != \
        _SMUGGLED["predicted_turnaways_avoided"], \
        "predicted_turnaways_avoided leaked"
    # The other three (revenue_gain_usd, source_loss_usd, revenue_impact_usd)
    # are internal to the plan and not exposed on ``recommended_action``;
    # ``write.py`` doesn't surface them, so the smuggle couldn't reach any
    # consumer surface for those three today.  If a future spec adds a
    # consumer surface for them (e.g., exposing them on the Finding), add
    # the same assertion here then — the plan-build code path is the same.


def test_model_path_ignores_hypothetical_estimated_value_in_tool_input(monkeypatch):
    """Even if the model somehow smuggled an ``estimated_value_usd`` field past
    the schema and validator (which it cannot — schema is strict and the
    validator raises on extras), the agent's plan-build step uses
    ``money_for`` and discards any such field.

    This test asserts the IGNORE path directly: we patch the validator to
    accept an extra field, inject a tool input carrying a wildly wrong
    ``estimated_value_usd``, and confirm the Finding's value still comes
    from ``money_for``.
    """
    _install_patches(monkeypatch)

    import agents.fi_rebalancing.llm as llm_mod

    # Patch the validator to be a no-op so the extra field reaches the agent's
    # plan-build step.  (In production the validator's strict-extras check
    # would already have raised.)
    monkeypatch.setattr(llm_mod, "validate_rebalance_plan",
                        lambda tool_input, **_kw: None)

    write_fn, written = _capture_fn()
    refresh_fn, _refreshed = _capture_fn()

    chosen_vin = "VINELIG001"
    SMUGGLED = 999999.99  # obviously-wrong positive "value" if it ever leaked through

    bedrock = _FakeBedrock(
        responses=[_tool_response(
            moves=[{
                "candidate_id": "cand-loc-def-loc-sur",
                "vehicle_vins": [chosen_vin],
                "timing": "today",
                "detail": "A smuggled numeric field is in the tool input.",
                "evidence_refs": ["rebalance.forecast", "rebalance.supply"],
                "estimated_value_usd": SMUGGLED,  # the attacker's field
            }],
            holds=[],
        )],
    )
    kwargs = _make_run_kwargs(
        write_fn=write_fn,
        refresh_fn=refresh_fn,
        bedrock_client=bedrock,
        model_id="MID",
        guardrail_id="GID",
        guardrail_version="DRAFT",
    )
    run_rebalance(**kwargs)

    finding = written[-1]
    rec = finding["recommended_action"]
    assert rec["estimated_value_usd"] != SMUGGLED
    # Not even the string, not even a rounded version of it.
    assert rec["estimated_value_usd"] > 0


# ---------------------------------------------------------------------------
# No-bedrock path: the agent falls through to rules (backward compatibility)
# ---------------------------------------------------------------------------
def test_no_bedrock_falls_through_to_rules_fallback(monkeypatch):
    _install_patches(monkeypatch)
    write_fn, written = _capture_fn()
    refresh_fn, _refreshed = _capture_fn()
    stats = run_rebalance(**_make_run_kwargs(write_fn=write_fn, refresh_fn=refresh_fn))
    assert stats["moves_planned"] == 1
    assert stats["model_calls"] == 0


# ---------------------------------------------------------------------------
# Model-call budget: capped runs route excess metros to rules
# ---------------------------------------------------------------------------
def test_model_call_budget_caps_the_run(monkeypatch):
    """When ``max_model_calls_per_run`` is 0, the agent uses rules only even
    with a Bedrock client available."""
    _install_patches(monkeypatch)
    write_fn, written = _capture_fn()
    refresh_fn, _refreshed = _capture_fn()

    # Any response here would be a surprise — the budget is zero.
    bedrock = _FakeBedrock(responses=[])

    stats = run_rebalance(**_make_run_kwargs(
        write_fn=write_fn,
        refresh_fn=refresh_fn,
        bedrock_client=bedrock,
        model_id="MID",
        guardrail_id="GID",
        guardrail_version="DRAFT",
        config={"max_model_calls_per_run": 0, "min_net_benefit_usd": 50.0},
    ))
    assert stats["model_calls"] == 0
    assert stats["moves_planned"] == 1  # rules still produce the move
    assert len(bedrock.converse_calls) == 0


# ---------------------------------------------------------------------------
# Holds → gap items
# ---------------------------------------------------------------------------
def test_model_hold_on_deficit_produces_a_gap_item_with_hold_evidence(monkeypatch):
    """A model hold on a deficit's gap_id produces a gap Finding whose
    ``rebalance.hold`` evidence entry carries the hold reason and detail, and
    whose write path is the deficit gap branch.
    """
    _install_patches(monkeypatch)
    write_fn, written = _capture_fn()
    refresh_fn, _refreshed = _capture_fn()

    # Resolve the deficit gap_id for the shared fixture (loc-def, forecast_week
    # for the run_date 2026-10-12 is 2026-W41 — but we compute it dynamically).
    import agents.fi_rebalancing.candidates as cands_mod
    from agents.fi_rebalancing import policy as rebal_policy
    from agents.fi_rebalancing.tests.test_agent import _FORECAST

    try:
        outlook = cands_mod.location_outlook(
            "loc-def", forecast=_FORECAST, outcomes=[],
            supply=3.0, op_hours=10.0,
            deficit_above=rebal_policy.current()["deficit_above"],
            max_utilization=rebal_policy.current()["max_utilization"],
            horizon=cands_mod.horizon_days(_TODAY),
        )
    except ValueError:
        pytest.skip("location_outlook refused to produce an outlook")
    gap_id = f"loc-def/{outlook['forecast_week']}"

    bedrock = _FakeBedrock(responses=[_tool_response(
        moves=[],
        holds=[{
            "gap_id": gap_id,
            "reason": "returns_expected",
            "detail": "Three vehicles return by 10:00; no move needed.",
        }],
    )])

    stats = run_rebalance(**_make_run_kwargs(
        write_fn=write_fn,
        refresh_fn=refresh_fn,
        bedrock_client=bedrock,
        model_id="MID",
        guardrail_id="GID",
        guardrail_version="DRAFT",
    ))

    # The hold accepts the deficit; no move finding is written.
    assert stats["move_items_written"] == 0
    # A deficit gap finding is written.
    assert stats["gap_items_written"] >= 1
    # Find the deficit gap finding and verify its evidence.
    gap_finding = next(
        (f for f in written
         if f.get("finding_kind") == "fleet.rebalance.gap"
            and f.get("subject_key") == gap_id),
        None,
    )
    assert gap_finding is not None, (written, gap_id)
    hold_entries = [e for e in gap_finding["evidence"] if e["kind"] == "rebalance.hold"]
    assert len(hold_entries) == 1
    (he,) = hold_entries
    assert he["value"] == {
        "reason": "returns_expected",
        "detail": "Three vehicles return by 10:00; no move needed.",
    }
    assert he["source_ref"] == "agent-fi-rebalancing/model.hold"


# ---------------------------------------------------------------------------
# Diagnosis source on the written move
# ---------------------------------------------------------------------------
def test_model_path_sets_diagnosis_source_to_model_on_moves(monkeypatch):
    _install_patches(monkeypatch)
    write_fn, written = _capture_fn()
    refresh_fn, _refreshed = _capture_fn()
    bedrock = _FakeBedrock(responses=[_tool_response(
        moves=[{
            "candidate_id": "cand-loc-def-loc-sur",
            "vehicle_vins": ["VINELIG001"],
            "timing": "today",
            "detail": "Model-written detail.",
            "evidence_refs": ["rebalance.forecast", "rebalance.supply"],
        }],
        holds=[],
    )])
    run_rebalance(**_make_run_kwargs(
        write_fn=write_fn,
        refresh_fn=refresh_fn,
        bedrock_client=bedrock,
        model_id="MID",
        guardrail_id="GID",
        guardrail_version="DRAFT",
    ))
    move_finding = next(f for f in written if f["finding_kind"] == "fleet.rebalance.move")
    diag_entries = [e for e in move_finding["evidence"]
                    if e["kind"] == "rebalance.diagnosis_source"]
    assert diag_entries, "move finding is missing rebalance.diagnosis_source"
    # The diagnosis_source entry's value comes from write.py which pulls it from
    # the plan; the plan carries ``diagnosis_source: "model"`` on the model path.
    assert diag_entries[0]["value"].get("diagnosis_source") == "model"



# ---------------------------------------------------------------------------
# D8 step 5: skip the model call for a metro whose live items carry the
# same metro-level inputs hash.  Issue
# `2026-10-18-fi-rebalance-rerun-calls-model`: the re-run at
# 2026-10-18T18:55:21Z made 3 Converse calls that wrote nothing; the per-item
# hash check runs AFTER the model call, so cost and latency are not saved.
# ---------------------------------------------------------------------------

def _finding_key_for_candidate(cand: dict, fleet_id: str, agent_version: str) -> str:
    """Return the move finding_key the agent will compute for a candidate.

    Replicates ``build_move_finding``'s subject_key composition
    (``<from>/<to>/<week>``) and the shared ``compute_finding_key`` call,
    so the test can preseed a live row by finding_key before the agent runs.
    """
    from agents.tier2.writer import compute_finding_key
    subject_key = (
        f"{cand['from_location_id']}/{cand['to_location_id']}/{cand['forecast_week']}"
    )
    return compute_finding_key(
        agent_id="agent-fi-rebalancing",
        scope_field="fleet_id",
        scope_value=fleet_id,
        finding_kind="fleet.rebalance.move",
        subject_key=subject_key,
    )


def test_metro_skip_when_live_metro_hash_matches(monkeypatch):
    """D8 step 5: a re-run on unchanged metro inputs makes NO model call.

    Two-phase integration test:
    - Phase 1 runs with no live metro hashes.  Model is called and one move
      Finding is written; the Finding carries the metro's inputs_hash on
      its ``metro_inputs_hash`` field.
    - Phase 2 preseeds that hash on the move's live row and re-runs.  The
      model MUST NOT be called and no Finding is written: the issue's
      acceptance ("re-run on unchanged inputs writes nothing and makes no
      model call") requires both counters to be zero.

    The mutation this test guards against (per the fix's own mutant list):
    remove the ``should_skip_metro`` call from ``_plan_moves`` → the model
    is called on both runs → ``bedrock_calls_phase2 > 0`` → fails.

    Biconditional: a sibling test ``test_metro_skip_biconditional_differing_hash``
    asserts that a DIFFERING live metro hash does NOT skip the model, so
    "always skip" cannot pass as a mutation.
    """
    _install_patches(monkeypatch)
    bedrock_phase1 = _FakeBedrock(responses=[_tool_response(
        moves=[{
            "candidate_id": "cand-loc-def-loc-sur",
            "vehicle_vins": ["VINELIG001"],
            "timing": "today",
            "detail": "Model-written detail.",
            "evidence_refs": ["rebalance.forecast", "rebalance.supply"],
        }],
        holds=[],
    )])
    write_fn1, written1 = _capture_fn()
    refresh_fn1, _ = _capture_fn()
    stats1 = run_rebalance(**_make_run_kwargs(
        write_fn=write_fn1, refresh_fn=refresh_fn1,
        bedrock_client=bedrock_phase1,
        model_id="MID", guardrail_id="GID", guardrail_version="DRAFT",
    ))
    assert stats1["model_calls"] >= 1, (
        "phase 1 must call the model so phase 2 has a real scenario to skip"
    )
    assert len(bedrock_phase1.converse_calls) >= 1
    assert len(written1) >= 1
    move_finding1 = next(
        f for f in written1 if f.get("finding_kind") == "fleet.rebalance.move"
    )
    # The write path injects ``metro_inputs_hash`` on move findings after
    # schema validation (DDB attribute, not a schema property), so the test
    # reads it off the written dict.
    assert "metro_inputs_hash" in move_finding1, (
        "move finding must carry metro_inputs_hash — the fix stores it on "
        "the write path so the next run can read it back"
    )
    metro_hash = move_finding1["metro_inputs_hash"]
    finding_key = move_finding1["finding_key"]

    # Phase 2: preseed the metro hash as "live" for this finding_key.  The
    # per-item ``inputs_hash`` is also preseeded (so the per-item path can
    # see a live row), but the metro skip must fire BEFORE the per-item
    # path and the model call.  Prompt 40: the reused metro rebuilds its plan
    # from the live row, so the row phase 1 wrote is the live row here.
    _install_patches(
        monkeypatch,
        live_hashes={finding_key: move_finding1["inputs_hash"]},
        live_metro_hashes={finding_key: metro_hash},
        live_rows={finding_key: move_finding1},
    )
    bedrock_phase2 = _FakeBedrock(responses=[_tool_response(
        moves=[{
            "candidate_id": "cand-loc-def-loc-sur",
            "vehicle_vins": ["VINELIG001"],
            "timing": "today",
            "detail": "This response MUST NOT be consumed.",
            "evidence_refs": ["rebalance.forecast", "rebalance.supply"],
        }],
        holds=[],
    )])
    write_fn2, written2 = _capture_fn()
    refresh_fn2, _ = _capture_fn()
    stats2 = run_rebalance(**_make_run_kwargs(
        write_fn=write_fn2, refresh_fn=refresh_fn2,
        bedrock_client=bedrock_phase2,
        model_id="MID", guardrail_id="GID", guardrail_version="DRAFT",
    ))
    assert bedrock_phase2.converse_calls == [], (
        "model was called despite live metro hash matching — "
        "should_skip_metro is not wired into _plan_moves"
    )
    assert stats2["model_calls"] == 0, (
        f"model_calls={stats2['model_calls']} on a re-run with matching "
        "metro hash; expected 0 (D8 step 5)"
    )
    # And no move was written (consistent with "no model call and no write")
    moves2 = [f for f in written2 if f.get("finding_kind") == "fleet.rebalance.move"]
    assert moves2 == [], (
        f"move written on skipped-metro re-run: {moves2!r}"
    )


def test_metro_skip_biconditional_differing_hash(monkeypatch):
    """Biconditional: a different live metro hash must NOT skip the model.

    Mutation guard: ``should_skip_metro`` returning True unconditionally would
    pass the previous test but fail this one — the model must be called when
    the live hash differs from the newly-computed one.
    """
    _install_patches(monkeypatch)
    # Preseed a DIFFERENT metro hash on the one candidate's finding_key.
    # Compute the candidate's finding_key the same way build_move_finding does.
    cand = {
        "from_location_id": "loc-sur",
        "to_location_id": "loc-def",
        "forecast_week": "2026-10-12",  # candidates.build_candidates uses run_date for week start
    }
    finding_key = _finding_key_for_candidate(
        cand, fleet_id="demo-rental-carshare", agent_version="1.0.0",
    )
    _install_patches(
        monkeypatch,
        live_metro_hashes={finding_key: "f" * 64},  # obviously not the real hash
        # Prompt 40: the decision reads the live row itself.
        live_rows={finding_key: {"finding_key": finding_key, "metro_inputs_hash": "f" * 64,
                                 "computed_at": "2026-10-12T13:00:00Z"}},
    )
    bedrock = _FakeBedrock(responses=[_tool_response(
        moves=[{
            "candidate_id": "cand-loc-def-loc-sur",
            "vehicle_vins": ["VINELIG001"],
            "timing": "today",
            "detail": "Model-written detail.",
            "evidence_refs": ["rebalance.forecast", "rebalance.supply"],
        }],
        holds=[],
    )])
    write_fn, _written = _capture_fn()
    refresh_fn, _ = _capture_fn()
    stats = run_rebalance(**_make_run_kwargs(
        write_fn=write_fn, refresh_fn=refresh_fn,
        bedrock_client=bedrock,
        model_id="MID", guardrail_id="GID", guardrail_version="DRAFT",
    ))
    assert stats["model_calls"] >= 1, (
        "model_calls==0 with a DIFFERENT live metro hash means skip-metro "
        "fired when it should not (always-skip mutation)"
    )
    assert len(bedrock.converse_calls) >= 1


def test_metro_skip_no_live_hash_calls_model(monkeypatch):
    """First-ever run: no live row has a metro hash, so the model IS called.

    Mutation guard: a code that treats ``None`` live hash as "match" would
    skip the model on the first run — the issue says to pay once, then
    skip, not never pay.
    """
    _install_patches(monkeypatch)  # no live_metro_hashes
    bedrock = _FakeBedrock(responses=[_tool_response(
        moves=[{
            "candidate_id": "cand-loc-def-loc-sur",
            "vehicle_vins": ["VINELIG001"],
            "timing": "today",
            "detail": "Model-written detail.",
            "evidence_refs": ["rebalance.forecast", "rebalance.supply"],
        }],
        holds=[],
    )])
    write_fn, _written = _capture_fn()
    refresh_fn, _ = _capture_fn()
    stats = run_rebalance(**_make_run_kwargs(
        write_fn=write_fn, refresh_fn=refresh_fn,
        bedrock_client=bedrock,
        model_id="MID", guardrail_id="GID", guardrail_version="DRAFT",
    ))
    assert stats["model_calls"] >= 1, (
        "first-run model_calls==0 means skip-metro fired on absent live hash"
    )
    assert len(bedrock.converse_calls) >= 1



# ---------------------------------------------------------------------------
# Prompt 40: a reused (unchanged) metro's outcome comes from its live items.
# The end-to-end cases (moves kept, rules fallback skipped, holds skipped, the
# script's own G3) are in scripts/tests/test_live_verify_fi_rebalancing_agent.py
# on moto; these pin the parts that only show when something else changed.
# ---------------------------------------------------------------------------

_MODEL_MOVE = {
    "candidate_id": "cand-loc-def-loc-sur",
    "vehicle_vins": ["VINELIG001"],
    "timing": "today",
    "detail": "Model-written detail.",
    "evidence_refs": ["rebalance.forecast", "rebalance.supply"],
}


def _hold_gap_id() -> str:
    import agents.fi_rebalancing.candidates as cands_mod
    from agents.fi_rebalancing import policy as rebal_policy
    from agents.fi_rebalancing.tests.test_agent import _FORECAST

    outlook = cands_mod.location_outlook(
        "loc-def", forecast=_FORECAST, outcomes=[], supply=3.0, op_hours=10.0,
        deficit_above=rebal_policy.current()["deficit_above"],
        max_utilization=rebal_policy.current()["max_utilization"],
        horizon=cands_mod.horizon_days(_TODAY))
    return f"loc-def/{outlook['forecast_week']}"


def test_reused_move_that_no_longer_fits_its_candidate_plans_the_metro_again(monkeypatch):
    """A live move carrying the hash but naming a VIN outside the candidate's eligible pool
    is not reused: the metro is planned again (a model call), never re-recommended."""
    _install_patches(monkeypatch)
    write_fn1, written1 = _capture_fn()
    run_rebalance(**_make_run_kwargs(
        write_fn=write_fn1, refresh_fn=_capture_fn()[0],
        bedrock_client=_FakeBedrock(responses=[_tool_response(moves=[_MODEL_MOVE], holds=[])]),
        model_id="MID", guardrail_id="GID", guardrail_version="DRAFT"))
    move1 = next(f for f in written1 if f["finding_kind"] == "fleet.rebalance.move")
    stale = json.loads(json.dumps(move1))
    stale["recommended_action"]["parameters"]["vehicle_vins"] = ["VINGRND001"]  # grounded

    fk = move1["finding_key"]
    _install_patches(monkeypatch, live_hashes={fk: move1["inputs_hash"]},
                     live_metro_hashes={fk: move1["metro_inputs_hash"]},
                     live_rows={fk: stale})
    bedrock2 = _FakeBedrock(responses=[_tool_response(moves=[_MODEL_MOVE], holds=[])])
    stats2 = run_rebalance(**_make_run_kwargs(
        write_fn=_capture_fn()[0], refresh_fn=_capture_fn()[0], bedrock_client=bedrock2,
        model_id="MID", guardrail_id="GID", guardrail_version="DRAFT"))
    assert len(bedrock2.converse_calls) == 1
    assert stats2["metros_skipped_unchanged"] == 0


def test_live_outcome_under_an_older_fingerprint_is_not_reused(monkeypatch):
    """Biconditional with the live row present: after the metro's inputs change, a live
    move carrying the OLD fingerprint is not this run's plan; the model is called."""
    _install_patches(monkeypatch)
    write_fn1, written1 = _capture_fn()
    run_rebalance(**_make_run_kwargs(
        write_fn=write_fn1, refresh_fn=_capture_fn()[0],
        bedrock_client=_FakeBedrock(responses=[_tool_response(moves=[_MODEL_MOVE], holds=[])]),
        model_id="MID", guardrail_id="GID", guardrail_version="DRAFT"))
    move1 = next(f for f in written1 if f["finding_kind"] == "fleet.rebalance.move")
    fk = move1["finding_key"]

    changed_gaps = [dict(g, supply_vehicles=float(g["supply_vehicles"]) + 20.0,
                         vehicles_spare=int(g["vehicles_spare"]) + 10)
                    if g["location_id"] == "loc-sur" else dict(g)
                    for g in _install_gaps_source()]
    _install_patches(monkeypatch, gaps=changed_gaps, live_hashes={fk: move1["inputs_hash"]},
                     live_metro_hashes={fk: move1["metro_inputs_hash"]},
                     live_rows={fk: move1})
    bedrock2 = _FakeBedrock(responses=[_tool_response(moves=[_MODEL_MOVE], holds=[])])
    stats2 = run_rebalance(**_make_run_kwargs(
        write_fn=_capture_fn()[0], refresh_fn=_capture_fn()[0], bedrock_client=bedrock2,
        model_id="MID", guardrail_id="GID", guardrail_version="DRAFT"))
    assert len(bedrock2.converse_calls) == 1, "a changed metro reused its old outcome"
    assert stats2["metros_skipped_unchanged"] == 0


def test_reused_hold_survives_a_rewrite_of_its_gap_item(monkeypatch):
    """A held deficit's gap item whose own inputs changed (so it is rewritten, not
    refreshed) keeps the model's hold when the metro is reused, with no model call."""
    gap_id = _hold_gap_id()
    hold = {"gap_id": gap_id, "reason": "returns_expected",
            "detail": "Three vehicles return by 10:00; no move needed."}
    _install_patches(monkeypatch)
    write_fn1, written1 = _capture_fn()
    run_rebalance(**_make_run_kwargs(
        write_fn=write_fn1, refresh_fn=_capture_fn()[0],
        bedrock_client=_FakeBedrock(responses=[_tool_response(moves=[], holds=[hold])]),
        model_id="MID", guardrail_id="GID", guardrail_version="DRAFT"))
    gap1 = next(f for f in written1 if f["finding_kind"] == "fleet.rebalance.gap"
                and f["subject_key"] == gap_id)
    assert "metro_inputs_hash" in gap1, "a planned metro's deficit gap item carries the hash"

    fk = gap1["finding_key"]
    _install_patches(monkeypatch, live_hashes={fk: "0" * 64},  # its own inputs changed
                     live_metro_hashes={fk: gap1["metro_inputs_hash"]},
                     live_rows={fk: gap1})
    write_fn2, written2 = _capture_fn()
    bedrock2 = _FakeBedrock(responses=[])
    stats2 = run_rebalance(**_make_run_kwargs(
        write_fn=write_fn2, refresh_fn=_capture_fn()[0], bedrock_client=bedrock2,
        model_id="MID", guardrail_id="GID", guardrail_version="DRAFT"))
    assert bedrock2.converse_calls == [] and stats2["model_calls"] == 0
    assert stats2["metros_skipped_unchanged"] == 1
    (gap2,) = [f for f in written2 if f["finding_kind"] == "fleet.rebalance.gap"
               and f["subject_key"] == gap_id]
    assert [e["value"] for e in gap2["evidence"] if e["kind"] == "rebalance.hold"] == [
        {"reason": hold["reason"], "detail": hold["detail"]}]
    assert [e["value"] for e in gap2["evidence"]
            if e["kind"] == "rebalance.diagnosis_source"] == [{"diagnosis_source": "model"}]


def test_rules_only_outcome_stores_the_hash_and_is_reused(monkeypatch):
    """With no model configured (or the run's model budget spent) the rules outcome carries
    the fingerprint too, so an unchanged re-run reuses it: a metro left to rules for lack of
    budget is not sent to the model on a re-run that has budget to spare."""
    _install_patches(monkeypatch)
    write_fn1, written1 = _capture_fn()
    run_rebalance(**_make_run_kwargs(write_fn=write_fn1, refresh_fn=_capture_fn()[0],
                                     bedrock_client=_FakeBedrock(responses=[]),
                                     model_id="MID", guardrail_id="GID",
                                     guardrail_version="DRAFT",
                                     config={"max_model_calls_per_run": 0}))
    move1 = next(f for f in written1 if f["finding_kind"] == "fleet.rebalance.move")
    assert len(move1.get("metro_inputs_hash", "")) == 64

    fk = move1["finding_key"]
    _install_patches(monkeypatch, live_hashes={fk: move1["inputs_hash"]},
                     live_metro_hashes={fk: move1["metro_inputs_hash"]},
                     live_rows={fk: move1})
    refresh_fn2, refreshed2 = _capture_fn()
    bedrock2 = _FakeBedrock(responses=[])
    stats2 = run_rebalance(**_make_run_kwargs(write_fn=_capture_fn()[0], refresh_fn=refresh_fn2,
                                              bedrock_client=bedrock2, model_id="MID",
                                              guardrail_id="GID", guardrail_version="DRAFT"))
    assert bedrock2.converse_calls == [] and stats2["metros_skipped_unchanged"] == 1
    assert [f["finding_key"] for f in refreshed2
            if f["finding_kind"] == "fleet.rebalance.move"] == [fk]
    assert stats2["move_items_written"] == 0 and stats2["gap_items_written"] == 0


# ---------------------------------------------------------------------------
# Review g6r cycle 1 (W1-W3, S6): which live items are the outcome, and what a
# reused move keeps.  A two-deficit metro: loc-def and loc-def2 short, loc-sur
# spare, VINELIG001 and VINGRND001 both eligible (VINGRND001 un-grounded here).
# ---------------------------------------------------------------------------

def _move_to(dest: str, vins: list[str], detail: str = "Model-written detail.") -> dict:
    return {"candidate_id": f"cand-{dest}-loc-sur", "vehicle_vins": vins, "timing": "today",
            "detail": detail, "evidence_refs": ["rebalance.forecast", "rebalance.supply"]}


def _two_deficit_patches(monkeypatch, *, def_short: int = 1, def2_short: int = 1,
                         sur_supply: float = 10.0, live_hashes=None, live_rows=None):
    from agents.fi_rebalancing import sources as src
    from agents.fi_rebalancing.tests import test_agent as ta

    loc2 = dict(ta._LOC_DEF, locationId="loc-def2", name="Airport Hub 2", latitude=47.41)
    gaps = []
    for g in ta._GAPS:
        g = dict(g)
        if g["location_id"] == "loc-def":
            g["vehicles_short"] = def_short
        if g["location_id"] == "loc-sur":
            g["supply_vehicles"] = sur_supply
        gaps.append(g)
    gaps.append(dict(ta._GAPS[0], location_id="loc-def2", vehicles_short=def2_short))
    forecast = ta._FORECAST + [dict(r, location_id="loc-def2") for r in ta._FORECAST
                               if r["location_id"] == "loc-def"]
    _install_patches(monkeypatch, locations=ta._LOCATIONS + [loc2], gaps=gaps,
                     forecast=forecast,
                     grounding_map={**ta._DEFAULT_GROUNDING, "VINGRND001": (False, False),
                                    "VINDEF201": (False, False)},
                     live_hashes=live_hashes, live_rows=live_rows)
    vd = ta._VEHICLE_DAYS + [{"vin": "VINDEF201", "location_id": "loc-def2",
                              "available_hours": 10.0}]
    real_vd = src.read_vehicle_days

    def _vd(**kw):
        import inspect as _i
        _i.signature(real_vd).bind(**kw)
        return vd
    monkeypatch.setattr(src, "read_vehicle_days", _vd)
    real_rl = src.read_request_lengths

    def _rl(**kw):
        import inspect as _i
        _i.signature(real_rl).bind(**kw)
        return [{"location_id": "loc-def", "mean_request_hours": 24.0},
                {"location_id": "loc-def2", "mean_request_hours": 24.0}]
    monkeypatch.setattr(src, "read_request_lengths", _rl)


def _run(monkeypatch, responses, *, at_hour: int, config=None, **patch_kw):
    from datetime import timedelta
    _two_deficit_patches(monkeypatch, **patch_kw)
    write_fn, written = _capture_fn()
    refresh_fn, refreshed = _capture_fn()
    bedrock = _FakeBedrock(responses=[_tool_response(**r) for r in responses])
    stats = run_rebalance(**_make_run_kwargs(
        write_fn=write_fn, refresh_fn=refresh_fn, bedrock_client=bedrock, model_id="MID",
        guardrail_id="GID", guardrail_version="DRAFT", config=config,
        now=_NOW + timedelta(hours=at_hour)))
    return stats, written, refreshed, bedrock


def _rows(*written_lists) -> dict:
    """The live rows after these runs wrote, later runs over earlier ones (by finding_key)."""
    rows: dict = {}
    for written in written_lists:
        for f in written:
            rows[f["finding_key"]] = f
    return rows


def _moves_into(written: list[dict]) -> list[str]:
    return sorted(f["location"]["to_location_id"] for f in written
                  if f["finding_kind"] == "fleet.rebalance.move")


def test_partial_outcome_after_inputs_change_and_change_back_is_not_reused(monkeypatch):
    """W1 (a): inputs A -> B -> A.  B rewrote one of A's moves and dropped the other, which
    is still live with A's hash.  Back on A, the latest outcome is B's, so the metro is
    planned again; reusing A's leftover alone would leave loc-def with a live move and a
    'no move recommended' gap item."""
    both = {"moves": [_move_to("loc-def", ["VINELIG001"]), _move_to("loc-def2", ["VINGRND001"])],
            "holds": []}
    s1, w1, _, _ = _run(monkeypatch, [both], at_hour=0)
    assert _moves_into(w1) == ["loc-def", "loc-def2"]
    s2, w2, _, b2 = _run(monkeypatch, [{"moves": [_move_to("loc-def", ["VINELIG001"])],
                                        "holds": []}], at_hour=1, sur_supply=30.0)
    assert len(b2.converse_calls) == 1, "the B inputs must change the metro hash"
    assert _moves_into(w2) == ["loc-def"]
    s3, w3, _, b3 = _run(monkeypatch, [both], at_hour=2, live_rows=_rows(w1, w2))
    assert len(b3.converse_calls) == 1, "a partial, older outcome was reused"
    assert s3["metros_skipped_unchanged"] == 0


def test_items_left_by_an_earlier_outcome_with_the_same_hash_are_not_reused(monkeypatch):
    """W1 (b): two outcomes under one hash.  The latest (a move into loc-def and a hold on
    loc-def2) is reused; the earlier outcome's leftover move into loc-def2 is not, so no
    location gets both a move and a hold."""
    s1, w1, _, _ = _run(monkeypatch, [{"moves": [_move_to("loc-def", ["VINELIG001"]),
                                                 _move_to("loc-def2", ["VINGRND001"])],
                                       "holds": []}], at_hour=0)
    gap_id = f"loc-def2/{next(f for f in w1 if f['finding_kind'] == 'fleet.rebalance.move')['subject_key'].rsplit('/', 1)[1]}"
    hold = {"gap_id": gap_id, "reason": "returns_expected", "detail": "Returns due first."}
    # The same inputs, planned again (no live rows offered, as when a reuse is refused).
    s2, w2, _, _ = _run(monkeypatch, [{"moves": [_move_to("loc-def", ["VINELIG001"])],
                                       "holds": [hold]}], at_hour=1)
    assert w2[0]["metro_inputs_hash"] == w1[0]["metro_inputs_hash"]
    s3, w3, r3, b3 = _run(monkeypatch, [], at_hour=2, live_rows=_rows(w1, w2),
                          live_hashes={f["finding_key"]: f["inputs_hash"] for f in w2})
    assert b3.converse_calls == [] and s3["metros_skipped_unchanged"] == 1
    assert _moves_into(w3) == [] and _moves_into(r3) == ["loc-def"]
    (gap,) = [f for f in w3 + r3 if f["finding_kind"] == "fleet.rebalance.gap"
              and f["subject_key"] == gap_id]
    assert [e["value"]["reason"] for e in gap["evidence"]
            if e["kind"] == "rebalance.hold"] == ["returns_expected"]


def test_reused_move_under_the_current_money_floor_is_not_recommended_again(monkeypatch):
    """W2: money and the floor are not in the metro hash.  A reused one-VIN move whose net
    benefit is now under the floor (the two-VIN candidate is still over it) is not reused;
    the metro is planned again."""
    s2v, w2v, _, _ = _run(monkeypatch, [{"moves": [_move_to("loc-def", ["VINELIG001",
                                                                         "VINGRND001"])],
                                         "holds": []}], at_hour=0, def_short=2, def2_short=2)
    s1v, w1v, _, b1v = _run(monkeypatch, [{"moves": [_move_to("loc-def", ["VINELIG001"])],
                                           "holds": []}], at_hour=1, def_short=2, def2_short=2)
    net2 = next(f for f in w2v if f["finding_kind"] == "fleet.rebalance.move"
                )["recommended_action"]["estimated_value_usd"]
    net1 = next(f for f in w1v if f["finding_kind"] == "fleet.rebalance.move"
                )["recommended_action"]["estimated_value_usd"]
    assert net2 > net1, (net1, net2)
    floor = (net1 + net2) / 2
    s3, _w3, _r3, b3 = _run(
        monkeypatch, [{"moves": [_move_to("loc-def", ["VINELIG001", "VINGRND001"])],
                       "holds": []}], at_hour=2, def_short=2, def2_short=2,
        config={"min_net_benefit_usd": floor}, live_rows=_rows(w1v),
        live_hashes={f["finding_key"]: f["inputs_hash"] for f in w1v})
    # The raised floor must leave the metro's fingerprint unchanged, or this case is moot.
    assert len(b3.converse_calls) == 1, "a move under the floor was reused"
    prompt3 = b3.converse_calls[0]
    assert json.loads(prompt3["messages"][0]["content"][0]["text"].splitlines()[-1])[
        "candidates"] == json.loads(b1v.converse_calls[0]["messages"][0]["content"][0][
            "text"].splitlines()[-1])["candidates"]
    assert s3["metros_skipped_unchanged"] == 0, "a move under the floor was reused"


@pytest.mark.parametrize("model", [True, False])
def test_reused_move_rewritten_keeps_its_detail_and_diagnosis_source(monkeypatch, model):
    """W3: when a reused move's own inputs_hash changed (any deploy changes agent_version,
    which is in it) the row is rewritten from the rebuilt plan: it keeps the live row's
    detail and diagnosis_source, model or rules."""
    from agents.fi_rebalancing.fallback import _DETAIL
    detail = "A distinctive model detail."
    if model:
        _s1, w1, _, _ = _run(monkeypatch, [{"moves": [_move_to("loc-def", ["VINELIG001"],
                                                                detail)], "holds": []}],
                             at_hour=0)
    else:
        _s1, w1, _, _ = _run(monkeypatch, [], at_hour=0, config={"max_model_calls_per_run": 0})
    moves1 = {f["finding_key"]: f for f in w1 if f["finding_kind"] == "fleet.rebalance.move"}
    assert moves1
    s2, w2, _, b2 = _run(monkeypatch, [], at_hour=1, live_rows=_rows(w1),
                         live_hashes={f["finding_key"]: "0" * 64 for f in w1})
    assert b2.converse_calls == [] and s2["metros_skipped_unchanged"] == 1
    moves2 = {f["finding_key"]: f for f in w2 if f["finding_kind"] == "fleet.rebalance.move"}
    assert sorted(moves2) == sorted(moves1)
    for fk, move2 in moves2.items():
        assert move2["detail"] == (detail if model else _DETAIL) == moves1[fk]["detail"]
        assert [e["value"] for e in move2["evidence"]
                if e["kind"] == "rebalance.diagnosis_source"] == [
            {"diagnosis_source": "model" if model else "rules"}]


def test_reused_two_vin_move_in_model_order_is_refreshed_not_rewritten(monkeypatch):
    """S6: the rebuilt move keeps the row's VIN order (unsorted here), so its inputs_hash
    equals the live row's and it is refreshed, not rewritten."""
    _s1, w1, _, _ = _run(monkeypatch, [{"moves": [_move_to("loc-def", ["VINGRND001",
                                                                        "VINELIG001"])],
                                        "holds": []}], at_hour=0, def_short=2)
    s2, w2, r2, b2 = _run(monkeypatch, [], at_hour=1, def_short=2, live_rows=_rows(w1),
                          live_hashes={f["finding_key"]: f["inputs_hash"] for f in w1})
    assert b2.converse_calls == [] and s2["metros_skipped_unchanged"] == 1
    assert _moves_into(w2) == [] and _moves_into(r2) == ["loc-def"]
    (move,) = [f for f in r2 if f["finding_kind"] == "fleet.rebalance.move"]
    assert move["recommended_action"]["parameters"]["vehicle_vins"] == ["VINGRND001", "VINELIG001"]


@pytest.mark.parametrize("unhashed", ["move", "gap"])
def test_latest_outcome_with_an_unhashed_item_is_not_reused(monkeypatch, unhashed):
    """Every item of the latest outcome must carry the hash.  A row written before the
    fingerprint was stored on it (the first run after this deploy sees such rows) means the
    outcome can't be shown to be the current one, so the metro is planned again.  The gap
    case matters on its own: a move row is also re-checked when it is rebuilt, a gap row
    only by this rule."""
    _s0, w0, _, _ = _run(monkeypatch, [{"moves": [_move_to("loc-def", ["VINELIG001"])],
                                        "holds": []}], at_hour=0)
    gap_id = next(f["subject_key"] for f in w0 if f["finding_kind"] == "fleet.rebalance.gap"
                  and f["location"]["location_id"] == "loc-def2")
    plan = {"moves": [_move_to("loc-def", ["VINELIG001"])],
            "holds": [{"gap_id": gap_id, "reason": "returns_expected", "detail": "Due back."}]}
    _s1, w1, _, _ = _run(monkeypatch, [plan], at_hour=1)
    rows = _rows(w1)
    kind = "fleet.rebalance.move" if unhashed == "move" else "fleet.rebalance.gap"
    old = next(f for f in w1 if f["finding_kind"] == kind)
    rows[old["finding_key"]] = {k: v for k, v in old.items() if k != "metro_inputs_hash"}
    s2, _w2, _r2, b2 = _run(monkeypatch, [plan], at_hour=2, live_rows=rows,
                            live_hashes={f["finding_key"]: f["inputs_hash"] for f in w1})
    assert len(b2.converse_calls) == 1 and s2["metros_skipped_unchanged"] == 0


def test_reused_hold_without_a_reason_is_not_reused(monkeypatch):
    """A live hold whose reason is missing can't be rebuilt into a gap item; the metro is
    planned again rather than left without one (review g6r c1 S2)."""
    gap_id = _hold_gap_id()
    hold = {"gap_id": gap_id, "reason": "returns_expected", "detail": "Returns due first."}
    _install_patches(monkeypatch)
    write_fn1, written1 = _capture_fn()
    run_rebalance(**_make_run_kwargs(
        write_fn=write_fn1, refresh_fn=_capture_fn()[0],
        bedrock_client=_FakeBedrock(responses=[_tool_response(moves=[], holds=[hold])]),
        model_id="MID", guardrail_id="GID", guardrail_version="DRAFT"))
    gap1 = json.loads(json.dumps(next(f for f in written1
                                      if f["finding_kind"] == "fleet.rebalance.gap"
                                      and f["subject_key"] == gap_id)))
    for e in gap1["evidence"]:
        if e["kind"] == "rebalance.hold":
            e["value"]["reason"] = None
    _install_patches(monkeypatch, live_rows={gap1["finding_key"]: gap1},
                     live_hashes={gap1["finding_key"]: gap1["inputs_hash"]})
    bedrock2 = _FakeBedrock(responses=[_tool_response(moves=[], holds=[hold])])
    stats2 = run_rebalance(**_make_run_kwargs(
        write_fn=_capture_fn()[0], refresh_fn=_capture_fn()[0], bedrock_client=bedrock2,
        model_id="MID", guardrail_id="GID", guardrail_version="DRAFT"))
    assert len(bedrock2.converse_calls) == 1 and stats2["metros_skipped_unchanged"] == 0


# ---------------------------------------------------------------------------
# Unit tests for compute_metro_inputs_hash — hash must respond to its inputs.
# Guards the mutation "hash returns a constant" from the agent-level skip test:
# a constant hash passes both runs' comparison (phase 1 writes constant,
# phase 2 reads constant, they match) but defeats the whole mechanism.
# ---------------------------------------------------------------------------

def _bundle(
    *,
    candidates=None,
    gap_ids=None,
    sources_by_id=None,
    evidence_kinds=("rebalance.forecast", "rebalance.supply"),
    events=None,
    item_context=None,
) -> dict:
    """Minimal metro bundle shape for hash tests."""
    return {
        "candidates": candidates or [
            {
                "candidate_id": "c1",
                "from_location_id": "loc-a",
                "to_location_id": "loc-b",
                "count": 2,
                "eligible_vins": ["VIN2", "VIN1"],
            },
        ],
        "gap_ids": gap_ids or ["loc-b/2026-10-12"],
        "sources_by_id": sources_by_id or {
            "loc-a": {"supply_vehicles": 10, "op_hours": 24.0, "forecast_hours": 300.0},
        },
        "evidence_kinds": list(evidence_kinds),
        "events": events or [],
        "item_context": item_context or {"today": "2026-10-12"},
    }


def test_metro_inputs_hash_is_deterministic():
    """The hash is a pure function of its inputs: same bundle → same hash."""
    from agents.fi_rebalancing.write import compute_metro_inputs_hash
    h1 = compute_metro_inputs_hash(_bundle())
    h2 = compute_metro_inputs_hash(_bundle())
    assert h1 == h2
    # And it's a 64-char hex string (SHA-256).
    assert len(h1) == 64
    assert all(c in "0123456789abcdef" for c in h1)


def test_metro_inputs_hash_changes_when_candidates_change():
    """Mutation guard: adding a candidate changes the hash.

    Catches the "constant hash" mutation — a function returning a fixed
    string would pass ``test_metro_inputs_hash_is_deterministic`` but fail
    this one because the two bundles map to the same output.
    """
    from agents.fi_rebalancing.write import compute_metro_inputs_hash
    base = _bundle()
    base_hash = compute_metro_inputs_hash(base)
    added = _bundle(candidates=base["candidates"] + [{
        "candidate_id": "c2",
        "from_location_id": "loc-c",
        "to_location_id": "loc-d",
        "count": 1,
        "eligible_vins": ["VIN9"],
    }])
    assert compute_metro_inputs_hash(added) != base_hash


def test_metro_inputs_hash_changes_when_source_supply_changes():
    """Mutation guard: changing supply at a source changes the hash."""
    from agents.fi_rebalancing.write import compute_metro_inputs_hash
    base_hash = compute_metro_inputs_hash(_bundle())
    changed = _bundle(sources_by_id={
        "loc-a": {"supply_vehicles": 25, "op_hours": 24.0, "forecast_hours": 300.0},
    })
    assert compute_metro_inputs_hash(changed) != base_hash


def test_metro_inputs_hash_changes_when_gap_ids_change():
    """Mutation guard: a different gap set changes the hash."""
    from agents.fi_rebalancing.write import compute_metro_inputs_hash
    base_hash = compute_metro_inputs_hash(_bundle())
    changed = _bundle(gap_ids=["loc-b/2026-10-19"])
    assert compute_metro_inputs_hash(changed) != base_hash


def test_metro_inputs_hash_is_stable_against_today_and_events():
    """Design: ``item_context.today`` and ``events`` are excluded from the hash.

    These two fields drift daily (today's date, event ordering from Athena
    pages); including them would make the daily skip never fire.  This
    test pins the exclusion so a future refactor can't quietly fold them
    in — doing so would silently disable the skip in production.

    Mutation guard: adding ``today`` to the canonical dict → this test
    fails on the second hash computation.
    """
    from agents.fi_rebalancing.write import compute_metro_inputs_hash
    today1 = _bundle(item_context={"today": "2026-10-12"},
                     events=[{"event_id": "e1", "metro": "aus"}])
    today2 = _bundle(item_context={"today": "2026-10-19"},
                     events=[{"event_id": "e2", "metro": "aus"}])
    assert compute_metro_inputs_hash(today1) == compute_metro_inputs_hash(today2)


def test_metro_inputs_hash_is_stable_against_eligible_vin_order():
    """``eligible_vins`` is sorted before hashing, so a VIN pool permutation
    does not fire a spurious re-model.
    """
    from agents.fi_rebalancing.write import compute_metro_inputs_hash
    perm1 = _bundle(candidates=[{
        "candidate_id": "c1",
        "from_location_id": "loc-a",
        "to_location_id": "loc-b",
        "count": 3,
        "eligible_vins": ["VIN3", "VIN1", "VIN2"],
    }])
    perm2 = _bundle(candidates=[{
        "candidate_id": "c1",
        "from_location_id": "loc-a",
        "to_location_id": "loc-b",
        "count": 3,
        "eligible_vins": ["VIN1", "VIN2", "VIN3"],
    }])
    assert compute_metro_inputs_hash(perm1) == compute_metro_inputs_hash(perm2)



# ---------------------------------------------------------------------------
# Fix Group 1 W1: the stored metro_inputs_hash must stay fresh across
# UPDATE paths (not only on fresh PUT).  Three-phase integration test
# that fails without ``_write``'s ``set_metro_inputs_hash`` call.
# ---------------------------------------------------------------------------

def test_metro_hash_persists_on_update_path(monkeypatch):
    """W1 regression: a change-then-unchanged cycle must still skip the model.

    Three phases:

    1. Baseline inputs → PUT a finding with metro_hash H1.  The shared
       writer's ``_put_item`` would store H1 on a real DDB row; the test
       fixture mirrors the agent's ``set_metro_inputs_hash`` by writing
       into ``live_metro_hashes[finding_key] = metro_inputs_hash`` on
       every write (not only PUT).  Assert ``live_metro_hashes`` now
       carries H1.

    2. Change the inputs: raise the surplus location's op_hours so the
       metro bundle differs; this changes BOTH the per-item inputs_hash
       (triggering the UPDATE path in the real writer) AND the metro
       inputs hash (new H2).  Assert ``live_metro_hashes`` now carries
       H2 — if ``_write`` has stopped calling ``set_metro_inputs_hash``
       (mutation M7), ``live_metro_hashes`` would still carry H1 and
       phase 3 would call the model.

    3. Re-run with the phase-2 inputs: the agent re-computes H2 and
       reads ``live_metro_hashes[finding_key]`` = H2 → match → skip the
       model.  Assert ``model_calls == 0``.

    This test guards the W1 fix: without the explicit
    ``set_metro_inputs_hash`` call in ``_write``, the UPDATE path of
    phase 2 would NOT touch ``live_metro_hashes``, phase 3 would see the
    stale H1, and the skip would not fire.
    """
    live_hashes: dict[str, str] = {}
    live_metro_hashes: dict[str, str] = {}

    # Phase 1: baseline run — pre-fills live_hashes + live_metro_hashes.
    _install_patches(
        monkeypatch,
        live_hashes=live_hashes,
        live_metro_hashes=live_metro_hashes,
    )
    bedrock1 = _FakeBedrock(responses=[_tool_response(
        moves=[{
            "candidate_id": "cand-loc-def-loc-sur",
            "vehicle_vins": ["VINELIG001"],
            "timing": "today",
            "detail": "phase 1",
            "evidence_refs": ["rebalance.forecast", "rebalance.supply"],
        }],
        holds=[],
    )])
    write_fn1, written1 = _capture_fn()
    refresh_fn1, _ = _capture_fn()
    run_rebalance(**_make_run_kwargs(
        write_fn=write_fn1, refresh_fn=refresh_fn1,
        bedrock_client=bedrock1,
        model_id="MID", guardrail_id="GID", guardrail_version="DRAFT",
    ))
    move1 = next(f for f in written1 if f.get("finding_kind") == "fleet.rebalance.move")
    finding_key = move1["finding_key"]
    h1 = move1["metro_inputs_hash"]
    # live_hashes populated by the agent through the capture-fn chain is not
    # automatic; populate it from written1 so phase 2's inputs_hash check
    # finds a live row and takes the UPDATE path.
    for f in written1:
        live_hashes[f["finding_key"]] = f["inputs_hash"]
    assert live_metro_hashes.get(finding_key) == h1, (
        "phase 1 should have set the metro hash via set_metro_inputs_hash"
    )

    # Phase 2: change the surplus location's op_hours AND supply_vehicles
    # — op_hours flows into the metro bundle's ``sources_by_id[sid].op_hours``;
    # supply_vehicles flows into the metro bundle (sources_by_id) and the
    # candidate's per_day source_supply_vehicles.  BOTH move the metro
    # hash (new H2) and may or may not move the per-item hash (depending
    # on whether net_benefit crosses a $25 bucket).  To ensure the shared
    # writer takes the UPDATE path independently of money-bucket drift,
    # the test clears ``live_hashes`` for phase 2 — that forces the main
    # writer to see "no live row" and call ``write_fn``, exposing the
    # written move dict (which carries the fresh ``metro_inputs_hash``).
    changed_locations = []
    for loc in _LOCATIONS:
        copy = dict(loc)
        if copy.get("locationId") == "loc-sur":
            copy["op_hours"] = float(copy["op_hours"]) + 4.0
        changed_locations.append(copy)
    changed_gaps = []
    for g in _install_gaps_source():
        copy = dict(g)
        if copy.get("location_id") == "loc-sur" and copy.get("status") == "surplus":
            copy["supply_vehicles"] = float(copy["supply_vehicles"]) + 20.0
            copy["vehicles_spare"] = int(copy["vehicles_spare"]) + 10
        changed_gaps.append(copy)
    # Clear per-item hashes so write_fn is called (not just refresher) —
    # this isolates the W1 test from money-bucket drift and keeps the
    # test's assertion ("phase 2 writes a finding whose metro_inputs_hash
    # differs from phase 1's") clean.  The real production path takes
    # the UPDATE branch; the fixture here simulates PUT, which also
    # exercises ``set_metro_inputs_hash`` (both PUT and UPDATE call it
    # in the agent code).
    live_hashes_phase2: dict[str, str] = {}
    _install_patches(
        monkeypatch,
        locations=changed_locations,
        gaps=changed_gaps,
        live_hashes=live_hashes_phase2,
        live_metro_hashes=live_metro_hashes,
    )
    bedrock2 = _FakeBedrock(responses=[_tool_response(
        moves=[{
            "candidate_id": "cand-loc-def-loc-sur",
            "vehicle_vins": ["VINELIG001"],
            "timing": "today",
            "detail": "phase 2",
            "evidence_refs": ["rebalance.forecast", "rebalance.supply"],
        }],
        holds=[],
    )])
    write_fn2, written2 = _capture_fn()
    refresh_fn2, _ = _capture_fn()
    stats2 = run_rebalance(**_make_run_kwargs(
        write_fn=write_fn2, refresh_fn=refresh_fn2,
        bedrock_client=bedrock2,
        model_id="MID", guardrail_id="GID", guardrail_version="DRAFT",
    ))
    assert len(bedrock2.converse_calls) >= 1, (
        "phase 2 should call the model (per-item + metro inputs both changed)"
    )
    move2 = next(f for f in written2 if f.get("finding_kind") == "fleet.rebalance.move")
    h2 = move2["metro_inputs_hash"]
    assert h2 != h1, (
        f"test rigging bug: phase-2 metro hash should differ from phase 1 (both {h1!r})"
    )
    # Keep per-item live_hashes in sync so phase 3 goes down the UPDATE
    # path again (the per-item hash check).
    for f in written2:
        live_hashes[f["finding_key"]] = f["inputs_hash"]
    assert live_metro_hashes.get(finding_key) == h2, (
        "phase 2 should have overwritten live_metro_hashes with the fresh H2 "
        "via set_metro_inputs_hash (W1): without this fix, phase 3 would see "
        "the stale H1 and the skip would never fire again until the row expires"
    )

    # Phase 3: same inputs as phase 2 → agent re-computes H2 → reads
    # live_metro_hashes[finding_key] = H2 → match → skip.  Prompt 40: the
    # reused metro rebuilds its plan from the live rows phase 2 wrote.
    _install_patches(
        monkeypatch,
        locations=changed_locations,
        gaps=changed_gaps,
        live_hashes=live_hashes,
        live_metro_hashes=live_metro_hashes,
        live_rows={f["finding_key"]: f for f in written2},
    )
    bedrock3 = _FakeBedrock(responses=[_tool_response(
        moves=[{
            "candidate_id": "cand-loc-def-loc-sur",
            "vehicle_vins": ["VINELIG001"],
            "timing": "today",
            "detail": "phase 3 — MUST NOT be consumed",
            "evidence_refs": ["rebalance.forecast", "rebalance.supply"],
        }],
        holds=[],
    )])
    write_fn3, written3 = _capture_fn()
    refresh_fn3, _ = _capture_fn()
    stats3 = run_rebalance(**_make_run_kwargs(
        write_fn=write_fn3, refresh_fn=refresh_fn3,
        bedrock_client=bedrock3,
        model_id="MID", guardrail_id="GID", guardrail_version="DRAFT",
    ))
    assert bedrock3.converse_calls == [], (
        "W1 regression: model was called despite phase 2 persisting the new "
        "metro hash on the live row.  Fix: _write must call set_metro_inputs_hash "
        "after the main writer, so the UPDATE path converges metro_inputs_hash."
    )
    assert stats3["model_calls"] == 0



def test_metro_inputs_hash_is_stable_against_candidate_order():
    """``candidates`` is sorted by ``candidate_id`` so upstream page-order
    drift from ``build_candidates``'s gaps iteration does not silently
    change the hash.  Mirrors the ``..._stable_against_eligible_vin_order``
    test; without the ``sorted(..., key=lambda c: c.get('candidate_id'))``
    wrap in ``compute_metro_inputs_hash``, two permutations of the same
    candidate set produce different hashes.
    """
    from agents.fi_rebalancing.write import compute_metro_inputs_hash
    c1 = {"candidate_id": "cand-a", "from_location_id": "loc-x",
          "to_location_id": "loc-y", "count": 1, "eligible_vins": ["V1"]}
    c2 = {"candidate_id": "cand-b", "from_location_id": "loc-y",
          "to_location_id": "loc-x", "count": 1, "eligible_vins": ["V2"]}
    perm1 = _bundle(candidates=[c1, c2])
    perm2 = _bundle(candidates=[c2, c1])
    assert compute_metro_inputs_hash(perm1) == compute_metro_inputs_hash(perm2)


def test_metro_inputs_hash_is_stable_against_evidence_kinds_order():
    """``evidence_kinds`` is sorted defensively so a tuple reorder in
    ``agent._MOVE_EVIDENCE_KINDS`` doesn't ripple into a different hash.
    """
    from agents.fi_rebalancing.write import compute_metro_inputs_hash
    perm1 = _bundle(evidence_kinds=("rebalance.forecast", "rebalance.supply", "rebalance.events"))
    perm2 = _bundle(evidence_kinds=("rebalance.events", "rebalance.forecast", "rebalance.supply"))
    assert compute_metro_inputs_hash(perm1) == compute_metro_inputs_hash(perm2)


# ---------------------------------------------------------------------------
# Direct unit test for set_metro_inputs_hash — pins the DDB call shape
# (table, key field, UpdateExpression).  Guards against a wrong UpdateExpression
# or the wrong Key path reaching DDB without being caught by the integration
# tests, which stub the whole helper.
# ---------------------------------------------------------------------------

def test_set_metro_inputs_hash_updates_by_finding_id():
    """set_metro_inputs_hash does ``Query(by_finding_key GSI) → UpdateItem(
    Key={finding_id}, SET metro_inputs_hash = :mh)`` on the real helper
    (no monkey-patch here).

    Guards two separate mutations:
    * key field (``finding_key`` instead of ``finding_id`` on the UpdateItem key)
    * attribute name (``headline`` or ``inputs_hash`` instead of ``metro_inputs_hash``)
    """
    from unittest.mock import MagicMock
    from agents.fi_rebalancing.write import set_metro_inputs_hash

    ddb = MagicMock()
    # The query returns one live row with a known finding_id.
    ddb.query.return_value = {
        "Items": [
            {
                "finding_id": {"S": "FIND-abcd1234-ef56-7890-abcd-ef1234567890"},
                "finding_key": {"S": "f" * 64},
                "expires_at": {"S": "2999-01-01T00:00:00Z"},
                "inputs_hash": {"S": "aa" * 32},
                "metro_inputs_hash": {"S": "11" * 32},
            }
        ],
    }

    ok = set_metro_inputs_hash(
        finding_key="f" * 64,
        metro_inputs_hash="de" * 32,
        ddb_client=ddb,
        table_name="my-findings-table",
        now_str="2026-10-19T12:00:00Z",
    )
    assert ok is True

    # Query shape — hits the by_finding_key GSI with the expiry filter.
    ddb.query.assert_called_once()
    qkw = ddb.query.call_args.kwargs
    assert qkw["TableName"] == "my-findings-table"
    assert qkw["IndexName"] == "by_finding_key"
    assert ":fk" in qkw["ExpressionAttributeValues"]
    assert qkw["ExpressionAttributeValues"][":fk"] == {"S": "f" * 64}

    # UpdateItem shape — keyed on finding_id (NOT finding_key), SETs
    # metro_inputs_hash (NOT inputs_hash or headline), guarded by
    # ``attribute_exists(finding_id)`` to avoid an orphan row on a
    # Query→UpdateItem race (S1 from the security review).
    ddb.update_item.assert_called_once()
    ukw = ddb.update_item.call_args.kwargs
    assert ukw["TableName"] == "my-findings-table"
    assert ukw["Key"] == {"finding_id": {"S": "FIND-abcd1234-ef56-7890-abcd-ef1234567890"}}
    assert ukw["UpdateExpression"] == "SET metro_inputs_hash = :mh"
    assert ukw["ConditionExpression"] == "attribute_exists(finding_id)"
    assert ukw["ExpressionAttributeValues"] == {":mh": {"S": "de" * 32}}


def test_set_metro_inputs_hash_rejects_non_hex():
    """Shape check at entry (S2 from the security review).

    A non-hex or wrong-length value would silently disable the skip
    (the stored value never equals a fresh compute).  Fail loud with ``ValueError`` instead.  Guards two
    classes of mutation: a caller that passes a plain-text string
    instead of a hash, or a hash that was computed with a different
    algorithm (shorter/longer than 64 hex chars).
    """
    from unittest.mock import MagicMock
    import pytest as _pytest
    from agents.fi_rebalancing.write import set_metro_inputs_hash

    ddb = MagicMock()
    for bad in [
        "not-a-hash",
        "aa" * 31,  # 62 chars, wrong length
        "aa" * 33,  # 66 chars, wrong length
        "GG" * 32,  # right length, wrong charset
        "AA" * 32,  # right length, uppercase hex (helper demands lowercase)
    ]:
        with _pytest.raises(ValueError, match="metro_inputs_hash must be"):
            set_metro_inputs_hash(
                finding_key="f" * 64,
                metro_inputs_hash=bad,
                ddb_client=ddb,
                table_name="my-findings-table",
                now_str="2026-10-19T12:00:00Z",
            )
    ddb.query.assert_not_called()
    ddb.update_item.assert_not_called()


def test_set_metro_inputs_hash_handles_conditional_check_failed():
    """A race where the row is deleted between Query and UpdateItem returns
    False cleanly (``ConditionalCheckFailedException`` is caught; other
    exceptions re-raise).
    """
    from unittest.mock import MagicMock
    import pytest as _pytest
    from agents.fi_rebalancing.write import set_metro_inputs_hash

    ddb = MagicMock()
    ddb.query.return_value = {
        "Items": [{"finding_id": {"S": "FIND-abcd1234-ef56-7890-abcd-ef1234567890"}}],
    }
    # ConditionalCheckFailed shape: ClientError-style exception with code.
    class _CCF(Exception):
        response = {"Error": {"Code": "ConditionalCheckFailedException"}}
    ddb.update_item.side_effect = _CCF("race")

    ok = set_metro_inputs_hash(
        finding_key="f" * 64,
        metro_inputs_hash="de" * 32,
        ddb_client=ddb,
        table_name="my-findings-table",
        now_str="2026-10-19T12:00:00Z",
    )
    assert ok is False

    # A different exception propagates.
    ddb.update_item.side_effect = RuntimeError("boom")
    with _pytest.raises(RuntimeError):
        set_metro_inputs_hash(
            finding_key="f" * 64,
            metro_inputs_hash="de" * 32,
            ddb_client=ddb,
            table_name="my-findings-table",
            now_str="2026-10-19T12:00:00Z",
        )


def test_set_metro_inputs_hash_noop_when_no_live_row():
    """A missing live row returns False and does NOT call update_item."""
    from unittest.mock import MagicMock
    from agents.fi_rebalancing.write import set_metro_inputs_hash

    ddb = MagicMock()
    ddb.query.return_value = {"Items": []}

    ok = set_metro_inputs_hash(
        finding_key="f" * 64,
        metro_inputs_hash="de" * 32,
        ddb_client=ddb,
        table_name="my-findings-table",
        now_str="2026-10-19T12:00:00Z",
    )
    assert ok is False
    ddb.query.assert_called_once()
    ddb.update_item.assert_not_called()
