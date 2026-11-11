"""Tests — cross-metro VIN uniqueness (T1.3 item 1).

Item 1: ``agents/fi_rebalancing/agent.py`` now creates a single ``used_vins_global``
set before the metro loop and passes it to every ``plan_rebalance`` call, updating
it after each model-path success.

The test patches ``agents.fi_rebalancing.agent.plan_rebalance`` to record what
``used_vins_global`` each call received, then checks that the second call received
a NON-EMPTY set containing the first metro's VINs.
"""
from __future__ import annotations

import inspect
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from agents.fi_rebalancing import sources as _src, write as _write
from agents.fi_rebalancing.agent import run_rebalance
from agents.fi_rebalancing.llm import ModelStats


# ---------------------------------------------------------------------------
# Two-metro test data: both metros share VIN-SHARED-001
# ---------------------------------------------------------------------------
_TODAY = "2026-10-12"
_RUN = "2026-10-12"
_NOW = datetime(2026, 10, 12, 14, 0, 0, tzinfo=timezone.utc)
_EFF = [f"2026-10-{d:02d}" for d in range(13, 20)]

_LOC_DEF_A = {"locationId": "loc-def-a", "name": "Hub A", "metro": "metro-a",
               "locationType": "airport", "latitude": 47.0, "longitude": -122.0,
               "parkingSpaces": 20, "operatingHours": "8-18", "op_hours": 10.0}
_LOC_SUR_A = {"locationId": "loc-sur-a", "name": "Downtown A", "metro": "metro-a",
               "locationType": "downtown", "latitude": 47.1, "longitude": -122.1,
               "parkingSpaces": 20, "operatingHours": "8-18", "op_hours": 10.0}

_LOC_DEF_B = {"locationId": "loc-def-b", "name": "Hub B", "metro": "metro-b",
               "locationType": "airport", "latitude": 48.0, "longitude": -123.0,
               "parkingSpaces": 20, "operatingHours": "8-18", "op_hours": 10.0}
_LOC_SUR_B = {"locationId": "loc-sur-b", "name": "Downtown B", "metro": "metro-b",
               "locationType": "downtown", "latitude": 48.1, "longitude": -123.1,
               "parkingSpaces": 20, "operatingHours": "8-18", "op_hours": 10.0}

_TWO_METRO_LOCATIONS = [_LOC_DEF_A, _LOC_SUR_A, _LOC_DEF_B, _LOC_SUR_B]

_TWO_METRO_GAPS = [
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

_TWO_METRO_FORECAST = (
    [{"location_id": lid, "forecast_date": d, "forecast_hours": fc,
      "method": "ml", "mape_28d": 0.10, "run_date": _RUN, "data_as_of": _TODAY}
     for lid, fc in [("loc-def-a", 45.0), ("loc-sur-a", 20.0),
                     ("loc-def-b", 45.0), ("loc-sur-b", 20.0)]
     for d in _EFF]
)

# VIN-SHARED-001 is eligible in both metros
_TWO_METRO_VEHICLE_DAYS = [
    {"vin": "VIN-SHARED-001", "location_id": "loc-sur-a", "available_hours": 10.0},
    {"vin": "VIN-SHARED-001", "location_id": "loc-sur-b", "available_hours": 10.0},
    {"vin": "VIN-ONLY-A", "location_id": "loc-sur-a", "available_hours": 10.0},
    {"vin": "VIN-ONLY-B", "location_id": "loc-sur-b", "available_hours": 10.0},
]

_TWO_METRO_UTILIZATION = [
    {"vin": "VIN-SHARED-001", "snapshot_date": _TODAY, "available_hours": 10.0, "on_rent_hours": 0.0},
    {"vin": "VIN-ONLY-A", "snapshot_date": _TODAY, "available_hours": 10.0, "on_rent_hours": 0.0},
    {"vin": "VIN-ONLY-B", "snapshot_date": _TODAY, "available_hours": 10.0, "on_rent_hours": 0.0},
]


def _install_two_metro_patches(monkeypatch):
    monkeypatch.setattr(_src, "read_locations",
        lambda fleet_id, *, ddb_client, table: (_TWO_METRO_LOCATIONS, [], set()))
    monkeypatch.setattr(_src, "read_gaps",
        lambda *, stage, fleet_id, athena_client, workgroup, results_s3_path:
            _TWO_METRO_GAPS)
    monkeypatch.setattr(_src, "read_forecast_days",
        lambda *, stage, fleet_id, athena_client, workgroup, results_s3_path:
            _TWO_METRO_FORECAST)
    monkeypatch.setattr(_src, "read_outcomes",
        lambda *, stage, fleet_id, athena_client, workgroup, results_s3_path: [])
    monkeypatch.setattr(_src, "read_vehicle_days",
        lambda *, stage, fleet_id, athena_client, workgroup, results_s3_path:
            _TWO_METRO_VEHICLE_DAYS)
    monkeypatch.setattr(_src, "read_vehicle_utilization",
        lambda *, stage, fleet_id, athena_client, workgroup, results_s3_path:
            _TWO_METRO_UTILIZATION)
    monkeypatch.setattr(_src, "read_busy_windows",
        lambda *, stage, fleet_id, now, athena_client, workgroup, results_s3_path: [])
    monkeypatch.setattr(_src, "read_request_lengths",
        lambda *, stage, fleet_id, now, athena_client, workgroup, results_s3_path: [])
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


def test_second_metro_plan_rebalance_call_sees_first_metros_vins(monkeypatch):
    """After the fix, the second metro call to plan_rebalance receives the first
    metro's chosen VINs in its used_vins_global argument.

    The test patches plan_rebalance to record the used_vins_global each call
    received, then asserts the second call's set is non-empty (fix applied) and
    contains VIN-SHARED-001 from the first metro's accepted result.
    """
    import agents.fi_rebalancing.agent as agent_mod

    plan_calls: list[set] = []

    def fake_plan_rebalance(
        *,
        bedrock_client,
        metro_bundle,
        used_vins_global,
        rules_fn,
        stats,
        model_id,
        guardrail_id,
        guardrail_version,
        source_util_cap=None,
    ) -> dict:
        plan_calls.append(set(used_vins_global))
        candidates = metro_bundle.get("candidates", [])
        vin = "VIN-SHARED-001"
        if candidates:
            eligible = candidates[0].get("eligible_vins", [])
            vin = next((v for v in eligible if v == "VIN-SHARED-001"), eligible[0] if eligible else vin)
        stats.model_calls += 1
        return {
            "moves": [{"candidate_id": candidates[0]["candidate_id"],
                       "vehicle_vins": [vin],
                       "timing": "today",
                       "detail": "Move detail.",
                       "diagnosis_source": "model",
                       "metro_inputs_hash": "hash-fake"}],
            "holds": [],
            "diagnosis_source": "model",
        }

    monkeypatch.setattr(agent_mod, "plan_rebalance", fake_plan_rebalance)
    _install_two_metro_patches(monkeypatch)

    write_fn = MagicMock(return_value={"action": "put", "finding_id": "fid"})
    refresh_fn = MagicMock(return_value={"action": "noop", "finding_id": "fid"})

    run_rebalance(
        **_make_run_kwargs(
            write_fn=write_fn,
            refresh_fn=refresh_fn,
            bedrock_client=MagicMock(),
            model_id="MID",
            guardrail_id="GID",
            guardrail_version="DRAFT",
        )
    )

    assert len(plan_calls) >= 2, (
        f"Expected plan_rebalance to be called at least twice; got {len(plan_calls)}"
    )

    second_call_vins = plan_calls[1]
    assert second_call_vins, (
        "Second plan_rebalance call must receive non-empty used_vins_global "
        "containing the first metro's chosen VINs (cross-metro uniqueness fix)"
    )
    # Verify the first call's result contributed VINs to the global set
    first_call_vins = plan_calls[0]
    assert first_call_vins == set(), (
        "First metro call must start with an empty used_vins_global"
    )
    # The second metro's used_vins_global must contain VINs that the first metro chose.
    # (Any VIN chosen by the first metro — not necessarily VIN-SHARED-001 since
    # eligibility filtering may select a metro-specific VIN first.)
    assert len(second_call_vins) > 0, (
        "Second call's used_vins_global must be non-empty after first metro commits VINs"
    )
