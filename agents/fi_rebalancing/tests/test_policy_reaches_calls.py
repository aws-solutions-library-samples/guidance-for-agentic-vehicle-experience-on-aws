"""Each policy key reaches the call that applies it (review g6fix cycle 1 W1).

The suite's fixtures were built on the config file's values, so a swap between two keys
(``target_utilization`` for ``max_utilization`` as the model path's source cap; ``deficit_above``
for ``target_utilization`` in the gap items' outlook) changed no checked outcome. Here a policy
whose five values differ from the file's and from each other is installed, and spies record what
reaches ``plan_rebalance``, every ``location_outlook`` call site, ``build_candidates`` and
``rules_fallback``. Reviewer mutants R4 and R5 fail here.
"""
from __future__ import annotations

import sys

from agents.fi_rebalancing import agent as agent_mod
from agents.fi_rebalancing import candidates as cands_mod
from agents.fi_rebalancing import policy
from agents.fi_rebalancing.tests.policy_file import policy_from_config_file
from agents.fi_rebalancing.tests.test_agent import _capture_fn, _install_patches, _make_run_kwargs

_DISTINCT = {"target_utilization": 0.6, "deficit_above": 0.7, "max_utilization": 0.8,
             "min_net_benefit_usd": 1.5, "max_vehicles_per_move": 3}


def test_distinct_policy_differs_from_the_file_and_within_itself():
    file_values = policy_from_config_file()
    assert all(_DISTINCT[k] != file_values[k] for k in _DISTINCT)
    assert len(set(_DISTINCT.values())) == len(_DISTINCT)


def _spies(monkeypatch):
    seen = {"outlook": [], "build": [], "fallback": [], "plan": []}
    real_outlook, real_build = cands_mod.location_outlook, cands_mod.build_candidates
    real_fallback = agent_mod.rules_fallback

    def outlook(*a, **kw):
        seen["outlook"].append((sys._getframe(1).f_code.co_name, kw["deficit_above"], kw["max_utilization"]))
        return real_outlook(*a, **kw)

    def build(**kw):
        seen["build"].append(dict(kw["config"]))
        return real_build(**kw)

    def fallback(**kw):
        seen["fallback"].append(kw.get("min_net_benefit_usd"))
        return real_fallback(**kw)

    def plan(**kw):
        seen["plan"].append(kw.get("source_util_cap"))
        return {"moves": [], "holds": [], "diagnosis_source": "rules"}

    monkeypatch.setattr(cands_mod, "location_outlook", outlook)
    monkeypatch.setattr(cands_mod, "build_candidates", build)
    monkeypatch.setattr(agent_mod, "rules_fallback", fallback)
    monkeypatch.setattr(agent_mod, "plan_rebalance", plan)
    return seen


def _run(monkeypatch, policy_values, **kw):
    _install_patches(monkeypatch)
    seen = _spies(monkeypatch)
    write_fn, _ = _capture_fn()
    refresh_fn, _ = _capture_fn()
    with policy.active(policy_values):
        agent_mod.run_rebalance(**_make_run_kwargs(write_fn=write_fn, refresh_fn=refresh_fn, **kw))
    return seen


def test_rules_path_gets_every_policy_value(monkeypatch):
    seen = _run(monkeypatch, _DISTINCT)
    assert seen["build"], "build_candidates never ran"
    for cfg in seen["build"]:
        for key, value in _DISTINCT.items():
            assert cfg[key] == value, key
    assert seen["fallback"] and set(seen["fallback"]) == {1.5}
    assert seen["outlook"]
    assert {(d, m) for _, d, m in seen["outlook"]} == {(0.7, 0.8)}


def test_gap_items_outlook_gets_deficit_above_and_max_utilization(monkeypatch):
    """A minimum net benefit no candidate meets leaves the deficit uncovered, so its gap item's
    outlook (``_outlook``) runs; R4 swapped its ``deficit_above`` for ``target_utilization``."""
    seen = _run(monkeypatch, {**_DISTINCT, "min_net_benefit_usd": 900_000})
    gap_calls = [(d, m) for caller, d, m in seen["outlook"] if caller == "_outlook"]
    assert gap_calls, f"gap-item outlook never ran; callers {sorted({c for c, _, _ in seen['outlook']})}"
    assert set(gap_calls) == {(0.7, 0.8)}


def test_model_path_gets_the_target_as_the_source_cap(monkeypatch):
    """R5 passed ``max_utilization`` as the model path's source cap."""
    seen = _run(monkeypatch, _DISTINCT, bedrock_client=object(), model_id="MID",
                guardrail_id="GID", guardrail_version="DRAFT")
    assert seen["plan"] == [0.6]
    model_site = [(d, m) for caller, d, m in seen["outlook"] if caller == "_plan_moves"]
    assert model_site and set(model_site) == {(0.7, 0.8)}



# ---------------------------------------------------------------------------
# Review g6fix cycle 2 W2: the model path refuses a move whose recomputed net benefit
# (money_for for the VINs the model chose) is under the policy's minimum. Reviewer
# mutant O2 (`if False:` on that check) passed the whole suite before this.
# ---------------------------------------------------------------------------
def _model_run_with_net(monkeypatch, net_benefit: float):
    from agents.fi_rebalancing.tests.test_agent_model_path import _FakeBedrock, _tool_response

    _install_patches(monkeypatch)
    real_money_for = agent_mod.money_for

    def money_for(cand, count):
        return {**real_money_for(cand, count), "net_benefit_usd": net_benefit}

    monkeypatch.setattr(agent_mod, "money_for", money_for)
    write_fn, written = _capture_fn()
    refresh_fn, _ = _capture_fn()
    bedrock = _FakeBedrock(responses=[_tool_response(
        moves=[{"candidate_id": "cand-loc-def-loc-sur", "vehicle_vins": ["VINELIG001"],
                "timing": "today", "detail": "Move one vehicle to loc-def.",
                "evidence_refs": ["rebalance.forecast"]}],
        holds=[])])
    with policy.active({**policy_from_config_file(), "min_net_benefit_usd": 40}):
        stats = agent_mod.run_rebalance(**_make_run_kwargs(
            write_fn=write_fn, refresh_fn=refresh_fn, bedrock_client=bedrock,
            model_id="MID", guardrail_id="GID", guardrail_version="DRAFT"))
    moves = [f for f in written if f["finding_kind"] == "fleet.rebalance.move"]
    return stats, moves


def test_model_move_under_the_minimum_after_recompute_is_refused(monkeypatch):
    stats, moves = _model_run_with_net(monkeypatch, 39.99)
    assert stats["model_calls"] >= 1
    assert moves == []
    assert stats["items_refused"] >= 1


def test_model_move_at_the_minimum_after_recompute_is_written(monkeypatch):
    """Control: the same move at exactly the minimum is written."""
    stats, moves = _model_run_with_net(monkeypatch, 40.0)
    assert stats["model_calls"] >= 1
    assert len(moves) == 1
    assert moves[0]["recommended_action"]["estimated_value_usd"] == 40.0
