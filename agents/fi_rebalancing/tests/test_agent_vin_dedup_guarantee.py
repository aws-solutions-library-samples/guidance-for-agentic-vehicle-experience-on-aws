"""Tests — W2 fix: one VIN per run across every planning path (F1.3).

A VIN eligible in two metros must appear in at most one written move for the run,
regardless of which planning path each metro takes.

The ``eligible_vins`` injection strategy:
  ``build_candidates`` is called with the real data (so financial fields are
  computed correctly) and then each candidate's ``eligible_vins`` is replaced
  with ``["VIN-SHARED-001"]``.  This gives both metros a valid candidate whose
  only eligible VIN is the shared one.  The real ``rules_fallback`` and
  ``plan_rebalance``/validator are exercised without change.

Paths covered (four tests, each RED before the fix):
  - rules + rules (no model)
  - model + rules (metro-a model, metro-b budget-exhausted rules)
  - model + model (metro-a model success; metro-b model fails validator, rules)
  - reused + new  (metro-a outcome reused via mock; metro-b rules)
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

import agents.fi_rebalancing.agent as agent_mod
from agents.fi_rebalancing import candidates as cands_mod
from agents.fi_rebalancing import sources as _src
from agents.fi_rebalancing import write as _write
from agents.fi_rebalancing.agent import run_rebalance
from agents.fi_rebalancing.llm import TOOL_NAME

# ---------------------------------------------------------------------------
# Two-metro fixture: VIN-ONLY-A is at metro-a's surplus; VIN-ONLY-B at metro-b's.
# build_candidates produces one candidate per metro; the injection wrapper then
# replaces each candidate's eligible_vins with ["VIN-SHARED-001"].
# ---------------------------------------------------------------------------
_TODAY = "2026-10-12"
_RUN = "2026-10-12"
_EFF = [f"2026-10-{d:02d}" for d in range(13, 20)]
_NOW = datetime(2026, 10, 12, 14, 0, 0, tzinfo=timezone.utc)

_LOCS = [
    {"locationId": "loc-def-a", "name": "Hub A", "metro": "metro-a",
     "locationType": "airport", "latitude": 47.0, "longitude": -122.0,
     "parkingSpaces": 20, "operatingHours": "8-18", "op_hours": 10.0},
    {"locationId": "loc-sur-a", "name": "Downtown A", "metro": "metro-a",
     "locationType": "downtown", "latitude": 47.1, "longitude": -122.1,
     "parkingSpaces": 20, "operatingHours": "8-18", "op_hours": 10.0},
    {"locationId": "loc-def-b", "name": "Hub B", "metro": "metro-b",
     "locationType": "airport", "latitude": 48.0, "longitude": -123.0,
     "parkingSpaces": 20, "operatingHours": "8-18", "op_hours": 10.0},
    {"locationId": "loc-sur-b", "name": "Downtown B", "metro": "metro-b",
     "locationType": "downtown", "latitude": 48.1, "longitude": -123.1,
     "parkingSpaces": 20, "operatingHours": "8-18", "op_hours": 10.0},
]

_GAPS = [
    {"location_id": "loc-def-a", "status": "deficit", "vehicles_short": 1,
     "vehicles_spare": 0, "supply_vehicles": 3, "forecast_hours": 45.0,
     "forecast_utilization_pct": 0.95, "data_as_of": _TODAY},
    {"location_id": "loc-sur-a", "status": "surplus", "vehicles_short": 0,
     "vehicles_spare": 2, "supply_vehicles": 10, "forecast_hours": 20.0,
     "forecast_utilization_pct": 0.20, "data_as_of": _TODAY},
    {"location_id": "loc-def-b", "status": "deficit", "vehicles_short": 1,
     "vehicles_spare": 0, "supply_vehicles": 3, "forecast_hours": 45.0,
     "forecast_utilization_pct": 0.95, "data_as_of": _TODAY},
    {"location_id": "loc-sur-b", "status": "surplus", "vehicles_short": 0,
     "vehicles_spare": 2, "supply_vehicles": 10, "forecast_hours": 20.0,
     "forecast_utilization_pct": 0.20, "data_as_of": _TODAY},
]

_FORECAST = (
    [{"location_id": lid, "forecast_date": d, "forecast_hours": fc,
      "method": "ml", "mape_28d": 0.10, "run_date": _RUN, "data_as_of": _TODAY}
     for lid, fc in [("loc-def-a", 45.0), ("loc-sur-a", 20.0),
                     ("loc-def-b", 45.0), ("loc-sur-b", 20.0)]
     for d in _EFF]
)

# One metro-specific VIN per surplus location; both metros get a real candidate.
# The test wrapper replaces eligible_vins with ["VIN-SHARED-001"] before planning.
_VEHICLE_DAYS = [
    {"vin": "VIN-ONLY-A", "location_id": "loc-sur-a", "available_hours": 10.0},
    {"vin": "VIN-ONLY-B", "location_id": "loc-sur-b", "available_hours": 10.0},
]

_UTILIZATION = [
    {"vin": "VIN-ONLY-A", "snapshot_date": _TODAY, "available_hours": 10.0,
     "on_rent_hours": 0.0},
    {"vin": "VIN-ONLY-B", "snapshot_date": _TODAY, "available_hours": 10.0,
     "on_rent_hours": 0.0},
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _install_patches(monkeypatch):
    """Monkeypatch all sources and write helpers for two-metro fixture."""
    monkeypatch.setattr(_src, "read_locations",
        lambda fleet_id, *, ddb_client, table: (_LOCS, [], set()))
    monkeypatch.setattr(_src, "read_gaps",
        lambda *, stage, fleet_id, athena_client, workgroup, results_s3_path: _GAPS)
    monkeypatch.setattr(_src, "read_forecast_days",
        lambda *, stage, fleet_id, athena_client, workgroup, results_s3_path: _FORECAST)
    monkeypatch.setattr(_src, "read_outcomes",
        lambda *, stage, fleet_id, athena_client, workgroup, results_s3_path: [])
    monkeypatch.setattr(_src, "read_vehicle_days",
        lambda *, stage, fleet_id, athena_client, workgroup, results_s3_path:
            _VEHICLE_DAYS)
    monkeypatch.setattr(_src, "read_vehicle_utilization",
        lambda *, stage, fleet_id, athena_client, workgroup, results_s3_path:
            _UTILIZATION)
    monkeypatch.setattr(_src, "read_busy_windows",
        lambda *, stage, fleet_id, now, athena_client, workgroup, results_s3_path: [])
    monkeypatch.setattr(_src, "read_request_lengths",
        lambda *, stage, fleet_id, now, athena_client, workgroup, results_s3_path:
            [{"location_id": lid, "mean_request_hours": 24.0}
             for lid in ("loc-def-a", "loc-sur-a", "loc-def-b", "loc-sur-b")])
    monkeypatch.setattr(_src, "read_events",
        lambda *, stage, today, athena_client, workgroup, results_s3_path: [])
    monkeypatch.setattr(_src, "read_open_ros",
        lambda *, stage, athena_client, workgroup, results_s3_path: [])
    monkeypatch.setattr(_src, "read_service_soon",
        lambda *, stage, today, athena_client, workgroup, results_s3_path: [])
    monkeypatch.setattr(_src, "read_grounding",
        lambda vin, *, ddb_client, vehicles_table, groundings_table: (False, False))
    monkeypatch.setattr(_src, "read_open_items",
        lambda vin, *, ddb_client, findings_table, now_iso: ([], False))
    monkeypatch.setattr(_write, "read_live_hash",
        lambda fk, *, ddb_client, table_name, now_str: None)
    monkeypatch.setattr(_write, "read_live_item",
        lambda fk, *, ddb_client, table_name, now_str: None)
    monkeypatch.setattr(_write, "set_metro_inputs_hash",
        lambda *, finding_key, metro_inputs_hash, ddb_client, table_name, now_str: True)


def _inject_shared_vin(monkeypatch):
    """Wrap build_candidates to replace each candidate's eligible_vins with
    ["VIN-SHARED-001"] and provide matching eligible_facts.

    All financial fields come from the real computation.  eligible_facts is
    replaced too because move_evidence raises if a planned VIN is absent.
    """
    orig = cands_mod.build_candidates

    def _patched(**kw):
        result = orig(**kw)
        for cand in result["candidates"]:
            cand["eligible_vins"] = ["VIN-SHARED-001"]
            cand["eligible_facts"] = {
                "VIN-SHARED-001": {
                    "days_idle_last_7": 3,
                    "days_to_next_service": None,
                    "hours_to_next_booking": None,
                    "open_lower_severity_items": [],
                },
            }
        return result

    monkeypatch.setattr(cands_mod, "build_candidates", _patched)


def _make_run_kwargs(**overrides) -> dict:
    base = dict(
        stage="staging",
        athena_client=object(), workgroup="test-wg",
        results_s3_path="s3://test/results",
        ddb_client=object(), findings_table="findings-tbl",
        vehicles_table="vehicles-tbl", groundings_table="groundings-tbl",
        fleet_locations_table="locations-tbl",
        now=_NOW,
    )
    base.update(overrides)
    return base


def _capture():
    calls: list[dict] = []

    def fn(finding: dict) -> None:
        calls.append(finding)

    return fn, calls


def _move_vins_written(written: list[dict]) -> list[list[str]]:
    """vehicle_vins from every fleet.rebalance.move finding."""
    return [
        f["recommended_action"]["parameters"]["vehicle_vins"]
        for f in written
        if f.get("finding_kind") == "fleet.rebalance.move"
    ]


def _surviving_from_locs(written: list[dict]) -> list[str]:
    """from_location_id from every fleet.rebalance.move finding."""
    return [
        f["recommended_action"]["parameters"]["from_location_id"]
        for f in written
        if f.get("finding_kind") == "fleet.rebalance.move"
    ]


# ---------------------------------------------------------------------------
# Fake Bedrock for model-path tests
# ---------------------------------------------------------------------------

class _FakeBedrock:
    """Returns pre-queued converse responses; apply_guardrail always returns NONE."""

    def __init__(self, responses: list[dict]) -> None:
        self._responses = list(responses)

    def converse(self, **kw):
        if not self._responses:
            raise AssertionError("_FakeBedrock exhausted its queued responses")
        return self._responses.pop(0)

    def apply_guardrail(self, **kw):
        return {"action": "NONE"}


def _tool_response(candidate_id: str, vins: list[str]) -> dict:
    """Minimal valid converse response for the given candidate / VINs."""
    return {
        "output": {
            "message": {
                "content": [{
                    "toolUse": {
                        "name": TOOL_NAME,
                        "input": {
                            "moves": [{
                                "candidate_id": candidate_id,
                                "vehicle_vins": vins,
                                "timing": "today",
                                "detail": "Transfer vehicles to address fleet deficit.",
                                "evidence_refs": [
                                    "rebalance.forecast",
                                    "rebalance.supply",
                                ],
                            }],
                            "holds": [],
                        },
                    }
                }]
            }
        },
        "usage": {"inputTokens": 100, "outputTokens": 50},
    }


# ---------------------------------------------------------------------------
# Tests (RED before the post-loop dedup is added to _plan_moves)
# ---------------------------------------------------------------------------

def test_rules_rules_vin_at_most_once_per_run(monkeypatch):
    """rules + rules: both metros use the rules-only path.

    With both candidates' eligible_vins = ["VIN-SHARED-001"] and no model,
    each metro's rules_fallback independently selects VIN-SHARED-001.  Without
    the post-loop dedup both plans are written; after the fix only one is.
    """
    _install_patches(monkeypatch)
    _inject_shared_vin(monkeypatch)
    write_fn, written = _capture()

    stats = run_rebalance(**_make_run_kwargs(write_fn=write_fn, refresh_fn=_capture()[0]))

    counts = sum(
        1 for vins in _move_vins_written(written) if "VIN-SHARED-001" in vins
    )
    assert counts <= 1, (
        f"VIN-SHARED-001 in {counts} written moves; expected at most 1 "
        f"(plans_deduped not firing on rules+rules path). "
        f"Vins per plan: {_move_vins_written(written)}"
    )
    # The FIRST-processed metro (metro-a, alphabetically first) should win;
    # the later plan is dropped — not the earlier one (mutant 2 guard).
    surviving_srcs = _surviving_from_locs(written)
    assert surviving_srcs == ["loc-sur-a"], (
        f"Expected only metro-a's plan (from loc-sur-a) to survive; "
        f"got from_locations={surviving_srcs}"
    )
    assert stats["plans_deduped"] == 1, (
        f"Expected plans_deduped=1 (metro-b's plan dropped); got {stats['plans_deduped']}"
    )


def test_model_rules_budget_vin_at_most_once_per_run(monkeypatch):
    """model + rules: metro-a takes the model path; metro-b is budget-exhausted.

    max_model_calls_per_run=1 exhausts the budget after metro-a.  Metro-b
    falls to the rules path and selects VIN-SHARED-001 (rules does not consult
    used_vins_global).  The post-loop dedup must drop metro-b's plan.
    """
    _install_patches(monkeypatch)
    _inject_shared_vin(monkeypatch)
    write_fn, written = _capture()

    bedrock = _FakeBedrock([
        # metro-a (alphabetically first) succeeds with VIN-SHARED-001
        _tool_response("cand-loc-def-a-loc-sur-a", ["VIN-SHARED-001"]),
    ])
    stats = run_rebalance(**_make_run_kwargs(
        write_fn=write_fn, refresh_fn=_capture()[0],
        bedrock_client=bedrock,
        model_id="MID", guardrail_id="GID", guardrail_version="DRAFT",
        config={"max_model_calls_per_run": 1},
    ))

    counts = sum(
        1 for vins in _move_vins_written(written) if "VIN-SHARED-001" in vins
    )
    assert counts <= 1, (
        f"VIN-SHARED-001 in {counts} written moves; expected at most 1 "
        f"(plans_deduped not firing on model+rules path). "
        f"Vins per plan: {_move_vins_written(written)}"
    )
    surviving_srcs = _surviving_from_locs(written)
    assert surviving_srcs == ["loc-sur-a"], (
        f"Expected metro-a's plan (from loc-sur-a) to survive; "
        f"got from_locations={surviving_srcs}"
    )
    assert stats["plans_deduped"] == 1, (
        f"Expected plans_deduped=1 (metro-b's plan dropped); got {stats['plans_deduped']}"
    )


def test_model_model_vin_at_most_once_per_run(monkeypatch):
    """model + model: metro-a model succeeds; metro-b model fails validator
    (VIN already in used_vins_global) on both attempts, falls to rules.

    Rules for metro-b selects VIN-SHARED-001 without checking used_vins_global.
    The post-loop dedup must drop metro-b's rules-fallback plan.
    """
    _install_patches(monkeypatch)
    _inject_shared_vin(monkeypatch)
    write_fn, written = _capture()

    # metro-a: 1 successful call.
    # metro-b: 2 calls fail the validator (VIN in used_vins_global) → rules.
    bedrock = _FakeBedrock([
        _tool_response("cand-loc-def-a-loc-sur-a", ["VIN-SHARED-001"]),  # metro-a ok
        _tool_response("cand-loc-def-b-loc-sur-b", ["VIN-SHARED-001"]),  # metro-b try 1
        _tool_response("cand-loc-def-b-loc-sur-b", ["VIN-SHARED-001"]),  # metro-b retry
    ])
    stats = run_rebalance(**_make_run_kwargs(
        write_fn=write_fn, refresh_fn=_capture()[0],
        bedrock_client=bedrock,
        model_id="MID", guardrail_id="GID", guardrail_version="DRAFT",
        config={"max_model_calls_per_run": 10},
    ))

    counts = sum(
        1 for vins in _move_vins_written(written) if "VIN-SHARED-001" in vins
    )
    assert counts <= 1, (
        f"VIN-SHARED-001 in {counts} written moves; expected at most 1 "
        f"(plans_deduped not firing on model+model-→-rules path). "
        f"Vins per plan: {_move_vins_written(written)}"
    )
    surviving_srcs = _surviving_from_locs(written)
    assert surviving_srcs == ["loc-sur-a"], (
        f"Expected metro-a's model plan (from loc-sur-a) to survive; "
        f"got from_locations={surviving_srcs}"
    )
    assert stats["plans_deduped"] == 1, (
        f"Expected plans_deduped=1 (metro-b's plan dropped); got {stats['plans_deduped']}"
    )


def test_reused_new_vin_at_most_once_per_run(monkeypatch):
    """reused + new: metro-a's plan is returned by _reuse_live_outcome with
    VIN-SHARED-001 (simulating a plan stored in DDB from a previous run).
    Metro-b runs the real rules path and also selects VIN-SHARED-001.

    The post-loop dedup must drop metro-b's plan.
    """
    _install_patches(monkeypatch)
    _inject_shared_vin(monkeypatch)

    def _fake_reuse(*, metro, metro_cands, gap_ids, new_metro_hash, fleet_id,
                    ddb_client, findings_table, now_iso_z, min_net_benefit_usd):
        """Return a pre-built plan with VIN-SHARED-001 for metro-a, None for metro-b."""
        if metro != "metro-a":
            return None
        cand = next(
            (c for c in metro_cands
             if c.get("from_location_id") == "loc-sur-a"),
            None,
        )
        if cand is None:
            return None
        from agents.fi_rebalancing.candidates import money_for
        try:
            money = money_for(cand, 1)
        except ValueError:
            return None
        if money["net_benefit_usd"] < min_net_benefit_usd:
            return None
        plan = {
            "candidate_id": cand["candidate_id"],
            "from_location_id": cand["from_location_id"],
            "to_location_id": cand["to_location_id"],
            "vehicle_vins": ["VIN-SHARED-001"],
            "timing": "today",
            "detail": "Reused plan: transfer to address deficit.",
            "diagnosis_source": "rules",
            "metro_inputs_hash": new_metro_hash,
            **money,
        }
        return [plan], {}

    monkeypatch.setattr(agent_mod, "_reuse_live_outcome", _fake_reuse)

    write_fn, written = _capture()

    stats = run_rebalance(**_make_run_kwargs(write_fn=write_fn, refresh_fn=_capture()[0]))

    counts = sum(
        1 for vins in _move_vins_written(written) if "VIN-SHARED-001" in vins
    )
    assert counts <= 1, (
        f"VIN-SHARED-001 in {counts} written moves; expected at most 1 "
        f"(plans_deduped not firing on reused+new path). "
        f"Vins per plan: {_move_vins_written(written)}"
    )
    surviving_srcs = _surviving_from_locs(written)
    assert surviving_srcs == ["loc-sur-a"], (
        f"Expected metro-a's reused plan (from loc-sur-a) to survive; "
        f"got from_locations={surviving_srcs}"
    )
    assert stats["plans_deduped"] == 1, (
        f"Expected plans_deduped=1 (metro-b's plan dropped); got {stats['plans_deduped']}"
    )


# ---------------------------------------------------------------------------
# G2.1 S1: rules-path VINs are fed into used_vins_global for later model metros
# ---------------------------------------------------------------------------

_FACT_G21 = {"days_idle_last_7": 3, "days_to_next_service": None,
              "hours_to_next_booking": None, "open_lower_severity_items": []}


def _inject_two_metro_vins(monkeypatch):
    """metro-a: eligible_vins=["VIN-SHARED-001"]; metro-b: ["VIN-SHARED-001", "VIN-ONLY-B"].

    This gives metro-b a second VIN choice so the model can retry with it.
    """
    orig = cands_mod.build_candidates

    def _patched(**kw):
        result = orig(**kw)
        for cand in result["candidates"]:
            if cand["from_location_id"] == "loc-sur-a":
                vins = ["VIN-SHARED-001"]
            else:
                vins = ["VIN-SHARED-001", "VIN-ONLY-B"]
            cand["eligible_vins"] = vins
            cand["eligible_facts"] = {v: dict(_FACT_G21) for v in vins}
        return result

    monkeypatch.setattr(cands_mod, "build_candidates", _patched)


def test_model_fallback_rules_vins_fed_to_later_model(monkeypatch):
    """G2.1 S1 (RED): when metro-a's model fails twice and falls back to rules,
    metro-b's model validator must see VIN-SHARED-001 in used_vins_global.

    metro-a model attempts return "VIN-NOT-ELIGIBLE" (not in eligible_vins) →
    ValidationError both times → rules fallback → VIN-SHARED-001.
    With the fix, used_vins_global is updated after the rules fallback, so
    metro-b's first model proposal (VIN-SHARED-001) is rejected and the retry
    (VIN-ONLY-B) succeeds.  Without the fix, metro-b's first proposal passes
    validation, and the post-loop dedup drops metro-b's plan (plans_deduped=1).
    """
    _install_patches(monkeypatch)
    _inject_two_metro_vins(monkeypatch)
    write_fn, written = _capture()
    bedrock = _FakeBedrock([
        # metro-a: both attempts propose a VIN not in eligible_vins → ValidationError × 2 → rules
        _tool_response("cand-loc-def-a-loc-sur-a", ["VIN-NOT-ELIGIBLE"]),
        _tool_response("cand-loc-def-a-loc-sur-a", ["VIN-NOT-ELIGIBLE"]),
        # metro-b: first proposes VIN-SHARED-001 (rejected with fix); retry proposes VIN-ONLY-B
        _tool_response("cand-loc-def-b-loc-sur-b", ["VIN-SHARED-001"]),
        _tool_response("cand-loc-def-b-loc-sur-b", ["VIN-ONLY-B"]),
    ])
    stats = run_rebalance(**_make_run_kwargs(
        write_fn=write_fn, refresh_fn=_capture()[0],
        bedrock_client=bedrock,
        model_id="MID", guardrail_id="GID", guardrail_version="DRAFT",
        config={"max_model_calls_per_run": 10},
    ))
    vins = _move_vins_written(written)
    assert sorted(map(tuple, vins)) == [("VIN-ONLY-B",), ("VIN-SHARED-001",)], (
        f"Expected 2 moves (VIN-SHARED-001 from metro-a rules, VIN-ONLY-B from metro-b model); "
        f"got {vins}"
    )
    assert stats["plans_deduped"] == 0, (
        f"Expected plans_deduped=0 (model steering prevented the overlap); "
        f"got {stats['plans_deduped']}"
    )


# ---------------------------------------------------------------------------
# G2.1 S2: a metro whose plan is deduped is removed from metro_hashes
# ---------------------------------------------------------------------------


def test_deduped_metro_not_in_metro_hashes(monkeypatch):
    """G2.1 S2 (RED): when metro-b's plan is deduped, metro_hashes["metro-b"]
    is removed so the next run re-plans that metro.

    Verified via set_metro_inputs_hash call count: the deduped metro's deficit
    gap finding must NOT receive a non-None hash.
    Before fix: 2 calls (metro-a move + metro-b deficit).
    After fix: 1 call (metro-a move only — metro-b was removed from metro_hashes
    so metro_hashes.get("metro-b") returns None, and set_metro_inputs_hash is
    not called for it).

    vehicle_days is augmented with VINs at both deficit locations so that
    gap_evidence succeeds and gap items are actually written for both metros.
    """
    _install_patches(monkeypatch)
    _inject_shared_vin(monkeypatch)

    # Add assigned vehicles at deficit locations so gap_evidence succeeds.
    monkeypatch.setattr(
        _src, "read_vehicle_days",
        lambda *, stage, fleet_id, athena_client, workgroup, results_s3_path: (
            _VEHICLE_DAYS + [
                {"vin": "VIN-SHARED-001", "location_id": "loc-def-a",
                 "available_hours": 10.0},
                {"vin": "VIN-SHARED-001", "location_id": "loc-def-b",
                 "available_hours": 10.0},
            ]
        ),
    )

    hash_calls: list = []
    monkeypatch.setattr(
        _write, "set_metro_inputs_hash",
        lambda *, finding_key, metro_inputs_hash, ddb_client, table_name, now_str:
            hash_calls.append(metro_inputs_hash),
    )

    write_fn, written = _capture()
    stats = run_rebalance(**_make_run_kwargs(
        write_fn=write_fn, refresh_fn=_capture()[0],
    ))

    # Baseline: metro-b's plan is deduped
    assert stats["plans_deduped"] == 1, f"Expected 1 dedup; got {stats}"

    # After fix: only 1 non-None set_metro_inputs_hash call
    # (metro-a's move finding only — metro-b's deficit gap item gets
    # metro_inputs_hash=None because metro-b was removed from metro_hashes).
    # Before fix: 2 calls (metro-a move + metro-b deficit gap with hash_b).
    assert len(hash_calls) == 1, (
        f"Expected 1 set_metro_inputs_hash call (deduped metro removed from "
        f"metro_hashes so its gap item gets None); got {len(hash_calls)}: {hash_calls}"
    )


# ---------------------------------------------------------------------------
# H3.3 S4: reused-then-model pins the reused-site used_vins_global.update
# ---------------------------------------------------------------------------


def test_reused_model_vin_at_most_once_per_run_reused_site(monkeypatch):
    """reused-then-model: metro-a is REUSED with VIN-SHARED-001; metro-b runs
    the model path and first proposes VIN-SHARED-001 (rejected), then VIN-ONLY-B.

    Pins the reused-site ``used_vins_global.update`` (agent.py G2.1-S1 reused
    branch).  Mutant: removing only that update leaves VIN-SHARED-001 absent
    from used_vins_global when metro-b's model runs, so metro-b's first proposal
    passes validation, VIN-ONLY-B is never requested, the two-move assertion
    fails, and plans_deduped becomes 1 instead of 0.
    """
    _install_patches(monkeypatch)
    _inject_two_metro_vins(monkeypatch)

    def _fake_reuse(*, metro, metro_cands, gap_ids, new_metro_hash, fleet_id,
                    ddb_client, findings_table, now_iso_z, min_net_benefit_usd):
        if metro != "metro-a":
            return None
        cand = next(
            (c for c in metro_cands if c.get("from_location_id") == "loc-sur-a"),
            None,
        )
        if cand is None:
            return None
        from agents.fi_rebalancing.candidates import money_for
        try:
            money = money_for(cand, 1)
        except ValueError:
            return None
        if money["net_benefit_usd"] < min_net_benefit_usd:
            return None
        return [{
            "candidate_id": cand["candidate_id"],
            "from_location_id": cand["from_location_id"],
            "to_location_id": cand["to_location_id"],
            "vehicle_vins": ["VIN-SHARED-001"],
            "timing": "today",
            "detail": "Reused plan: transfer to address deficit.",
            "diagnosis_source": "rules",
            "metro_inputs_hash": new_metro_hash,
            **money,
        }], {}

    monkeypatch.setattr(agent_mod, "_reuse_live_outcome", _fake_reuse)

    write_fn, written = _capture()
    bedrock = _FakeBedrock([
        # metro-b: first proposal VIN-SHARED-001 is rejected (in used_vins_global
        # because reused metro-a added it via the reused-site update)
        _tool_response("cand-loc-def-b-loc-sur-b", ["VIN-SHARED-001"]),
        # metro-b: retry proposes VIN-ONLY-B (accepted)
        _tool_response("cand-loc-def-b-loc-sur-b", ["VIN-ONLY-B"]),
    ])
    stats = run_rebalance(**_make_run_kwargs(
        write_fn=write_fn, refresh_fn=_capture()[0],
        bedrock_client=bedrock,
        model_id="MID", guardrail_id="GID", guardrail_version="DRAFT",
        config={"max_model_calls_per_run": 10},
    ))
    vins = _move_vins_written(written)
    assert sorted(map(tuple, vins)) == [("VIN-ONLY-B",), ("VIN-SHARED-001",)], (
        f"Expected 2 moves (VIN-SHARED-001 from reused metro-a, VIN-ONLY-B from "
        f"metro-b model); got {vins}"
    )
    assert stats["plans_deduped"] == 0, (
        f"Expected plans_deduped=0 (steering prevented VIN overlap); "
        f"got {stats['plans_deduped']}"
    )


# ---------------------------------------------------------------------------
# H3.3 S5: fold test_probe_used_vins_steering.py — model-success path
# ---------------------------------------------------------------------------


def test_model_success_steering_keeps_second_metro_move(monkeypatch):
    """model-success + model: metro-a model succeeds with VIN-SHARED-001; metro-b
    model first proposes VIN-SHARED-001 (rejected via used_vins_global steering),
    then retries with VIN-ONLY-B (accepted).

    Pins the model-path ``used_vins_global.update(chosen_vins)`` (agent.py
    G2.1-S1 model branch).  Both metros write a move; plans_deduped is 0 because
    steering resolved the overlap before the post-loop dedup ran.
    """
    _install_patches(monkeypatch)
    _inject_two_metro_vins(monkeypatch)
    write_fn, written = _capture()
    bedrock = _FakeBedrock([
        _tool_response("cand-loc-def-a-loc-sur-a", ["VIN-SHARED-001"]),
        _tool_response("cand-loc-def-b-loc-sur-b", ["VIN-SHARED-001"]),
        _tool_response("cand-loc-def-b-loc-sur-b", ["VIN-ONLY-B"]),
    ])
    stats = run_rebalance(**_make_run_kwargs(
        write_fn=write_fn, refresh_fn=_capture()[0], bedrock_client=bedrock,
        model_id="MID", guardrail_id="GID", guardrail_version="DRAFT",
        config={"max_model_calls_per_run": 10}))
    vins = _move_vins_written(written)
    assert sorted(map(tuple, vins)) == [("VIN-ONLY-B",), ("VIN-SHARED-001",)], (
        f"Expected 2 moves (VIN-SHARED-001 metro-a, VIN-ONLY-B metro-b); got {vins}"
    )
    assert stats.get("plans_deduped") == 0, (
        f"Expected plans_deduped=0; got {stats.get('plans_deduped')}"
    )
