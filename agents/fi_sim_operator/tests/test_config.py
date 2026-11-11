"""Tests for agents/fi_sim_operator/config.py (task 11.2a).

Red tests first (written before implementation).  Run with the task runner:

    AWS_DEFAULT_REGION=us-west-2
    AWS_ACCESS_KEY_ID=testing
    AWS_SECRET_ACCESS_KEY=testing
    AWS_ENDPOINT_URL=http://127.0.0.1:1
    nice -n 19 .venv/bin/python -m pytest -p no:cacheprovider
        agents/fi_sim_operator/tests -q
"""
from __future__ import annotations

import pytest

from agents.fi_sim_operator.config import (
    ConfigError,
    OperatorConfig,
    parse_config,
    _MAX_CAP,
)
from agents.executors.simulated_operator import DEMO_FLEET_IDS

# ── minimal valid config ──────────────────────────────────────────────────────
_VALID = {
    "fleets": {
        "demo-lastmile-delivery": {"enabled": True},
        "demo-field-service": {"enabled": True},
        "demo-city-public": {"enabled": True},
        "demo-rental-carshare": {"enabled": True},
    },
    "daily_cap": 20,
    "hold_vins": ["MRDN0000000000015"],
    "hold_vehicle_ids": ["VEH-MRDN-0015"],
}


class TestParseConfigValid:
    """11.2a: valid configurations produce an OperatorConfig."""

    def test_all_four_demo_fleets_enabled(self):
        cfg = parse_config(_VALID)
        assert isinstance(cfg, OperatorConfig)
        assert cfg.enabled_fleets == frozenset(DEMO_FLEET_IDS)

    def test_daily_cap_is_int(self):
        cfg = parse_config(_VALID)
        assert isinstance(cfg, OperatorConfig)
        assert isinstance(cfg.daily_cap, int)

    def test_daily_cap_stored(self):
        raw = {**_VALID, "daily_cap": 15}
        cfg = parse_config(raw)
        assert isinstance(cfg, OperatorConfig)
        assert cfg.daily_cap == 15

    def test_daily_cap_clamped_to_20(self):
        """daily_cap 25 is clamped to 20."""
        raw = {**_VALID, "daily_cap": 25}
        cfg = parse_config(raw)
        assert isinstance(cfg, OperatorConfig)
        assert cfg.daily_cap == _MAX_CAP

    def test_daily_cap_zero_is_valid(self):
        raw = {**_VALID, "daily_cap": 0}
        cfg = parse_config(raw)
        assert isinstance(cfg, OperatorConfig)
        assert cfg.daily_cap == 0

    def test_hold_vins_in_frozenset(self):
        cfg = parse_config(_VALID)
        assert isinstance(cfg, OperatorConfig)
        assert "MRDN0000000000015" in cfg.hold_vins

    def test_hold_vehicle_ids_in_frozenset(self):
        cfg = parse_config(_VALID)
        assert isinstance(cfg, OperatorConfig)
        assert "VEH-MRDN-0015" in cfg.hold_vehicle_ids

    def test_non_demo_fleet_reported_not_run(self):
        """A fleet_id not in DEMO_FLEET_IDS → non_demo_ids, not enabled."""
        raw = {
            **_VALID,
            "fleets": {**_VALID["fleets"], "live-carrier-fleet-xyz": {"enabled": True}},
        }
        cfg = parse_config(raw)
        assert isinstance(cfg, OperatorConfig)
        assert "live-carrier-fleet-xyz" in cfg.non_demo_ids
        assert "live-carrier-fleet-xyz" not in cfg.enabled_fleets

    def test_fleet_enabled_false_not_run(self):
        """enabled: False → fleet not in enabled_fleets."""
        raw = {
            **_VALID,
            "fleets": {
                "demo-lastmile-delivery": {"enabled": True},
                "demo-field-service": {"enabled": False},
                "demo-city-public": {"enabled": True},
                "demo-rental-carshare": {"enabled": True},
            },
        }
        cfg = parse_config(raw)
        assert isinstance(cfg, OperatorConfig)
        assert "demo-field-service" not in cfg.enabled_fleets
        assert "demo-lastmile-delivery" in cfg.enabled_fleets

    def test_fleet_enabled_string_true_is_off(self):
        """enabled: 'true' (string) is NOT the boolean True → fleet off.

        Mutant guard: S3 (non-demo fleet run because parameter enables it).
        Any truthy non-bool must not enable a fleet.
        """
        raw = {
            **_VALID,
            "fleets": {
                "demo-lastmile-delivery": {"enabled": "true"},  # string, not bool
                "demo-field-service": {"enabled": True},
                "demo-city-public": {"enabled": True},
                "demo-rental-carshare": {"enabled": True},
            },
        }
        cfg = parse_config(raw)
        assert isinstance(cfg, OperatorConfig)
        assert "demo-lastmile-delivery" not in cfg.enabled_fleets

    def test_fleet_enabled_int_one_is_off(self):
        """enabled: 1 (int) is NOT the boolean True → fleet off."""
        raw = {
            **_VALID,
            "fleets": {"demo-lastmile-delivery": {"enabled": 1}},
        }
        cfg = parse_config(raw)
        # demo-lastmile-delivery is not in enabled_fleets
        assert isinstance(cfg, OperatorConfig)
        assert "demo-lastmile-delivery" not in cfg.enabled_fleets

    def test_hold_lists_absent_ok(self):
        """hold_vins and hold_vehicle_ids are optional (default empty)."""
        raw = {"fleets": _VALID["fleets"], "daily_cap": 5}
        cfg = parse_config(raw)
        assert isinstance(cfg, OperatorConfig)
        assert cfg.hold_vins == frozenset()
        assert cfg.hold_vehicle_ids == frozenset()

    def test_single_fleet_enabled(self):
        raw = {
            "fleets": {"demo-lastmile-delivery": {"enabled": True}},
            "daily_cap": 10,
        }
        cfg = parse_config(raw)
        assert isinstance(cfg, OperatorConfig)
        assert cfg.enabled_fleets == frozenset({"demo-lastmile-delivery"})


class TestParseConfigErrors:
    """11.2a: malformed input returns ConfigError, never raises."""

    def test_not_a_dict_returns_error(self):
        assert isinstance(parse_config("not a dict"), ConfigError)

    def test_missing_fleets_returns_error(self):
        assert isinstance(parse_config({"daily_cap": 10}), ConfigError)

    def test_fleets_not_dict_returns_error(self):
        assert isinstance(parse_config({"fleets": [], "daily_cap": 10}), ConfigError)

    def test_missing_daily_cap_returns_error(self):
        assert isinstance(parse_config({"fleets": _VALID["fleets"]}), ConfigError)

    def test_daily_cap_bool_returns_error(self):
        """bool is a subclass of int in Python; reject it explicitly.

        Mutant guard: a naive ``isinstance(cap, int)`` check would admit True.
        """
        raw = {**_VALID, "daily_cap": True}
        result = parse_config(raw)
        assert isinstance(result, ConfigError)

    def test_daily_cap_false_returns_error(self):
        raw = {**_VALID, "daily_cap": False}
        result = parse_config(raw)
        assert isinstance(result, ConfigError)

    def test_daily_cap_string_returns_error(self):
        raw = {**_VALID, "daily_cap": "20"}
        assert isinstance(parse_config(raw), ConfigError)

    def test_daily_cap_float_returns_error(self):
        raw = {**_VALID, "daily_cap": 1.5}
        assert isinstance(parse_config(raw), ConfigError)

    def test_daily_cap_none_returns_error(self):
        raw = {**_VALID, "daily_cap": None}
        assert isinstance(parse_config(raw), ConfigError)

    def test_hold_vins_not_list_returns_error(self):
        raw = {**_VALID, "hold_vins": "MRDN0000000000015"}
        assert isinstance(parse_config(raw), ConfigError)

    def test_hold_vins_non_string_entry_returns_error(self):
        raw = {**_VALID, "hold_vins": [123]}
        assert isinstance(parse_config(raw), ConfigError)

    def test_hold_vehicle_ids_not_list_returns_error(self):
        raw = {**_VALID, "hold_vehicle_ids": "VEH-MRDN-0015"}
        assert isinstance(parse_config(raw), ConfigError)

    def test_fleet_entry_not_dict_returns_error(self):
        raw = {
            **_VALID,
            "fleets": {"demo-lastmile-delivery": "enabled"},
        }
        assert isinstance(parse_config(raw), ConfigError)
