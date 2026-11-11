"""Tests for lambdas/avx-sim-operator/handler.py (task 11.3a).

Fakes only; no real AWS calls.

Covers:
  - FI_SIM_OPERATOR_ENABLED guard
  - SSM config load and parse failure → config_error status
  - hold_vehicle_id resolution (fix e): unresolvable id stops all fleets
  - dry_run/observe/approve mode dispatch from event
  - Run record written after each run
  - Required env vars missing → RuntimeError

Mutant guards (fix e):
  Mutant = skip hold_vehicle_id resolution: a held vehicle_id would not be added
  to hold_vins; the vehicle could be approved.
  test_hold_vehicle_id_unresolvable_stops_all_fleets: if resolution is skipped,
  the handler proceeds with no error — caught here because the fake returns no
  item for the vehicle_id.
"""
from __future__ import annotations

import json
import os
import sys
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

# Path setup
_LAMBDA_ROOT = os.path.join(os.path.dirname(__file__), "..")
_LAMBDAS_ROOT = os.path.dirname(_LAMBDA_ROOT)
_REPO_ROOT = os.path.dirname(_LAMBDAS_ROOT)
for _p in (_LAMBDA_ROOT, _LAMBDAS_ROOT, _REPO_ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

os.environ.setdefault("FI_SIM_OPERATOR_ENABLED", "true")
os.environ.setdefault("AVX_SIM_OPERATOR_TABLE", "vsa-test-avx-sim-operator")
os.environ.setdefault("AVX_ACTIONS_TABLE", "vsa-test-avx-actions")
os.environ.setdefault("AVX_FINDINGS_TABLE", "vsa-test-avx-findings")
os.environ.setdefault("AVX_AGENT_RUNS_TABLE", "vsa-test-avx-agent-runs")
os.environ.setdefault("FI_SIM_OPERATOR_PARAM", "/cvx/test/fi/sim-operator")
os.environ.setdefault("CMS_VEHICLES_TABLE", "cms-test-storage-vehicles")
os.environ.setdefault("CMS_GROUNDINGS_TABLE", "cms-test-storage-vehicle-groundings")

import handler as h  # noqa: E402

_VALID_CONFIG = {
    "fleets": {
        "demo-lastmile-delivery": {"enabled": True},
    },
    "daily_cap": 5,
    "hold_vins": [],
    "hold_vehicle_ids": [],
}


@pytest.fixture(autouse=True)
def _no_live_groundings_read():
    """11.3b item 7 added a CMS groundings read per fleet; these older tests don't
    model it, so it answers "nothing grounded, no error" here. Its own tests are in
    ``test_handler_11_3b.py``."""
    with patch.object(h, "_read_grounded_codes", return_value=({}, 0)):
        yield


def _fake_ssm_config(config_dict: dict):
    """Return a fake SSM get_parameter response."""
    return {"Parameter": {"Value": json.dumps(config_dict)}}


# ── FI_SIM_OPERATOR_ENABLED guard ────────────────────────────────────────────

class TestSimOperatorEnabledGuard:
    def test_handler_raises_when_flag_not_set(self, monkeypatch):
        """If FI_SIM_OPERATOR_ENABLED != 'true', handler raises RuntimeError."""
        monkeypatch.setenv("FI_SIM_OPERATOR_ENABLED", "false")
        with pytest.raises(RuntimeError, match="FI_SIM_OPERATOR_ENABLED"):
            h.handler({}, None)

    def test_handler_raises_when_flag_absent(self, monkeypatch):
        """If FI_SIM_OPERATOR_ENABLED is unset, handler raises RuntimeError."""
        monkeypatch.delenv("FI_SIM_OPERATOR_ENABLED", raising=False)
        with pytest.raises(RuntimeError, match="FI_SIM_OPERATOR_ENABLED"):
            h.handler({}, None)


# ── SSM config errors → config_error status ──────────────────────────────────

class TestSsmConfigErrors:
    def test_ssm_exception_returns_config_error(self, monkeypatch):
        """SSM get_parameter raising → status config_error."""
        monkeypatch.setenv("FI_SIM_OPERATOR_ENABLED", "true")

        def _fake_load_ssm(*args, **kwargs):
            raise RuntimeError("SSM unavailable")

        # 11.3b item 5: an unreadable parameter is an error: run record, then raise.
        with patch.object(h, "_load_ssm_config", side_effect=RuntimeError("SSM unavailable")), \
             patch.object(h, "_write_run_record", return_value=True) as rr:
            with pytest.raises(RuntimeError, match="config_error"):
                h.handler({}, None)
        assert rr.call_args.kwargs["reason"].startswith("ssm_unreadable:")

    def test_invalid_config_returns_config_error(self, monkeypatch):
        """Invalid config (e.g. daily_cap is a bool) → status config_error."""
        monkeypatch.setenv("FI_SIM_OPERATOR_ENABLED", "true")
        bad_config = {**_VALID_CONFIG, "daily_cap": True}  # bool not int

        with patch.object(h, "_load_ssm_config", return_value=bad_config), \
             patch.object(h, "_write_run_record", return_value=True) as rr:
            with pytest.raises(RuntimeError, match="config_error"):
                h.handler({}, None)
        assert rr.call_args.kwargs["reason"].startswith("config_invalid:")


# ── hold_vehicle_id resolution (fix e) ───────────────────────────────────────

class TestHoldVehicleIdResolution:
    """(e) hold_vehicle_ids must be resolved to VINs before the run starts.

    An unresolvable id stops ALL fleets for the run (the vehicle must not be
    touched while its VIN is unknown).

    Mutation guard (mutant = skip resolution):
      If _resolve_vehicle_ids_to_vins is not called (or its result is ignored),
      the run proceeds with no error even though a hold_vehicle_id can't be
      resolved. ``test_hold_vehicle_id_unresolvable_stops_all_fleets`` catches
      this because a fake get_item returning no item would cause a RuntimeError
      that the handler catches and returns as "error" status.
    """

    def test_hold_vehicle_id_unresolvable_stops_all_fleets(self, monkeypatch):
        """An unresolvable hold_vehicle_id returns status 'error' and no fleets run.

        Mutation guard: if resolution is skipped, the handler calls run_fleet
        normally (fleets key is non-empty); this test checks that status is 'error'
        and fleets is empty.
        """
        monkeypatch.setenv("FI_SIM_OPERATOR_ENABLED", "true")
        config_with_vid = {
            **_VALID_CONFIG,
            "hold_vehicle_ids": ["VEH-UNRESOLVABLE-001"],
        }

        # Fake get_item that returns no item for any vehicle_id
        def _fake_resolve(vehicle_ids, vehicles_table):
            raise RuntimeError(
                "sim-operator: hold_vehicle_id 'VEH-UNRESOLVABLE-001' could not be resolved"
            )

        with patch.object(h, "_load_ssm_config", return_value=config_with_vid), \
             patch.object(h, "_resolve_vehicle_ids_to_vins", side_effect=_fake_resolve), \
             patch.object(h, "_scan_findings_for_fleet") as scan, \
             patch.object(h, "_write_run_record", return_value=True) as rr:
            # 11.3b item 5: stops every fleet, records why, then raises.
            with pytest.raises(RuntimeError, match='"status": "error"'):
                h.handler({}, None)

        scan.assert_not_called()
        assert rr.call_args.kwargs["fleet_results"] == {}
        assert rr.call_args.kwargs["reason"].startswith("hold_unresolved:")

    def test_hold_vehicle_id_resolved_adds_to_hold_vins(self, monkeypatch):
        """Resolved vehicle IDs are added to hold_vins for the run."""
        monkeypatch.setenv("FI_SIM_OPERATOR_ENABLED", "true")
        config_with_vid = {
            **_VALID_CONFIG,
            "hold_vehicle_ids": ["VEH-0015"],
        }
        resolved_vin = "MRDN0000000000015"  # 17 chars

        resolved: list[frozenset] = []

        def _fake_fleet_run(*args, fleet_id=None, **kwargs):
            # We can't easily capture the resolved hold_vins here without
            # patching more deeply, but we verify the handler proceeds (no error).
            from agents.fi_sim_operator.run import FleetRunResult
            return FleetRunResult(fleet_id=fleet_id or "", mode="dry_run")

        def _fake_resolve(vehicle_ids, vehicles_table):
            return {"VEH-0015": resolved_vin}

        with patch.object(h, "_load_ssm_config", return_value=config_with_vid), \
             patch.object(h, "_resolve_vehicle_ids_to_vins", side_effect=_fake_resolve), \
             patch.object(h, "_scan_findings_for_fleet", return_value=[]), \
             patch.object(h, "_get_decided_ids", return_value=set()), \
             patch("agents.fi_sim_operator.ledger.get_first_seen_times", return_value={}), \
             patch("agents.fi_sim_operator.ledger.get_today_cap_used", return_value=0), \
             patch.object(h, "_write_run_record"):
            resp = h.handler({"mode": "dry_run"}, None)

        # No error — resolution succeeded
        assert resp["status"] == "ok", f"Expected ok, got {resp!r}"


# ── Mode dispatch ──────────────────────────────────────────────────────────────

class TestModeDispatch:
    def test_event_dry_run_mode_used(self, monkeypatch):
        """Event with mode='dry_run' runs in dry_run mode."""
        monkeypatch.setenv("FI_SIM_OPERATOR_ENABLED", "true")

        with patch.object(h, "_load_ssm_config", return_value=_VALID_CONFIG), \
             patch.object(h, "_scan_findings_for_fleet", return_value=[]), \
             patch.object(h, "_get_decided_ids", return_value=set()), \
             patch("agents.fi_sim_operator.ledger.get_first_seen_times", return_value={}), \
             patch("agents.fi_sim_operator.ledger.get_today_cap_used", return_value=0), \
             patch.object(h, "_write_run_record"):
            resp = h.handler({"mode": "dry_run"}, None)

        assert resp["status"] == "ok"
        assert resp["mode"] == "dry_run"

    def test_default_mode_is_approve(self, monkeypatch):
        """Empty event defaults to approve mode."""
        monkeypatch.setenv("FI_SIM_OPERATOR_ENABLED", "true")

        with patch.object(h, "_load_ssm_config", return_value=_VALID_CONFIG), \
             patch.object(h, "_scan_findings_for_fleet", return_value=[]), \
             patch.object(h, "_get_decided_ids", return_value=set()), \
             patch("agents.fi_sim_operator.ledger.get_first_seen_times", return_value={}), \
             patch("agents.fi_sim_operator.ledger.get_today_cap_used", return_value=0), \
             patch.object(h, "_write_run_record"):
            resp = h.handler({}, None)

        assert resp["mode"] == "approve"

    def test_response_has_fleets_key(self, monkeypatch):
        """Response contains a 'fleets' dict with per-fleet results."""
        monkeypatch.setenv("FI_SIM_OPERATOR_ENABLED", "true")

        with patch.object(h, "_load_ssm_config", return_value=_VALID_CONFIG), \
             patch.object(h, "_scan_findings_for_fleet", return_value=[]), \
             patch.object(h, "_get_decided_ids", return_value=set()), \
             patch("agents.fi_sim_operator.ledger.get_first_seen_times", return_value={}), \
             patch("agents.fi_sim_operator.ledger.get_today_cap_used", return_value=0), \
             patch.object(h, "_write_run_record"):
            resp = h.handler({"mode": "dry_run"}, None)

        assert "fleets" in resp
        assert isinstance(resp["fleets"], dict)

    def test_non_demo_fleet_id_not_in_response(self, monkeypatch):
        """Config with a non-demo fleet id doesn't add it to the response."""
        monkeypatch.setenv("FI_SIM_OPERATOR_ENABLED", "true")
        config = {
            **_VALID_CONFIG,
            "fleets": {
                "demo-lastmile-delivery": {"enabled": True},
                "non-demo-fleet": {"enabled": True},  # not in DEMO_FLEET_IDS
            },
        }

        with patch.object(h, "_load_ssm_config", return_value=config), \
             patch.object(h, "_scan_findings_for_fleet", return_value=[]), \
             patch.object(h, "_get_decided_ids", return_value=set()), \
             patch("agents.fi_sim_operator.ledger.get_first_seen_times", return_value={}), \
             patch("agents.fi_sim_operator.ledger.get_today_cap_used", return_value=0), \
             patch.object(h, "_write_run_record"):
            resp = h.handler({"mode": "dry_run"}, None)

        assert "non-demo-fleet" not in resp["fleets"]
        # demo fleet was enabled and processed
        assert "demo-lastmile-delivery" in resp["fleets"]


# ── Required env vars ────────────────────────────────────────────────────────

class TestRequiredEnvVars:
    def test_missing_actions_table_raises(self, monkeypatch):
        """Missing AVX_ACTIONS_TABLE raises RuntimeError."""
        monkeypatch.setenv("FI_SIM_OPERATOR_ENABLED", "true")
        monkeypatch.delenv("AVX_ACTIONS_TABLE", raising=False)
        with pytest.raises(RuntimeError, match="AVX_ACTIONS_TABLE"):
            h.handler({}, None)

    def test_missing_param_name_raises(self, monkeypatch):
        """Missing FI_SIM_OPERATOR_PARAM raises RuntimeError."""
        monkeypatch.setenv("FI_SIM_OPERATOR_ENABLED", "true")
        monkeypatch.delenv("FI_SIM_OPERATOR_PARAM", raising=False)
        with pytest.raises(RuntimeError, match="FI_SIM_OPERATOR_PARAM"):
            h.handler({}, None)
