"""The limited kinds' bounds: config entry, parser and limit check (task 8.3, G8-D5).

One parameter, read at run time by the rules API, the auto-approver and CMS's internal
route. Every read or parse failure is ``BoundsUnavailable`` (the callers fail closed), and
the config file's staging default is exactly U6's approved values.
"""
from __future__ import annotations

import json
import pathlib
from decimal import Decimal

import pytest

from agents.executors import policy

_CONFIG = (pathlib.Path(__file__).resolve().parents[3]
           / "deployment" / "config" / "avx-policy-defaults.json")


def _raises_unavailable(fn, *args):
    with pytest.raises(Exception) as ei:
        fn(*args)
    assert type(ei.value) is policy.BoundsUnavailable, repr(ei.value)


def test_the_config_entry_is_u6s_bounds_and_parses():
    params = json.loads(_CONFIG.read_text())["parameters"]
    (entry,) = [p for p in params if p["_key_template"] == policy.AUTO_APPROVAL_BOUNDS_PARAM]
    assert entry["staging_default"] == {"max_limit_usd": 500, "max_vehicles_per_auto_move": 5}
    assert policy.parse_auto_approval_bounds(json.dumps(entry["staging_default"])) == {
        "max_limit_usd": 500.0, "max_vehicles_per_auto_move": 5}


def test_the_rebalancing_policy_entry_keeps_exactly_its_own_keys():
    """The bounds didn't join the agent's policy parameter (its loader wants exact keys)."""
    params = json.loads(_CONFIG.read_text())["parameters"]
    (entry,) = [p for p in params
                if p["_key_template"] == "/vsa/{stage}/avx/fi-rebalancing/policy"]
    assert set(entry["staging_default"]) == {
        "target_utilization", "deficit_above", "max_utilization",
        "min_net_benefit_usd", "max_vehicles_per_move"}


@pytest.mark.parametrize("raw", [
    "not json", "[]", "null",
    {"max_limit_usd": 500},
    {"max_limit_usd": 500, "max_vehicles_per_auto_move": 5, "extra": 1},
    {"max_limit_usd": True, "max_vehicles_per_auto_move": 5},
    {"max_limit_usd": "500", "max_vehicles_per_auto_move": 5},
    {"max_limit_usd": 0, "max_vehicles_per_auto_move": 5},
    {"max_limit_usd": 100001, "max_vehicles_per_auto_move": 5},
    {"max_limit_usd": float("inf"), "max_vehicles_per_auto_move": 5},
    {"max_limit_usd": 500, "max_vehicles_per_auto_move": 5.0},
    {"max_limit_usd": 500, "max_vehicles_per_auto_move": True},
    {"max_limit_usd": 500, "max_vehicles_per_auto_move": 0},
    {"max_limit_usd": 500, "max_vehicles_per_auto_move": 51},
    '{"max_limit_usd": 1' + "0" * 400 + ', "max_vehicles_per_auto_move": 5}',
])
def test_malformed_bounds_are_unavailable(raw):
    _raises_unavailable(policy.parse_auto_approval_bounds, raw)


def test_the_boundaries_parse():
    assert policy.parse_auto_approval_bounds(
        {"max_limit_usd": 100000, "max_vehicles_per_auto_move": 50}) == {
        "max_limit_usd": 100000.0, "max_vehicles_per_auto_move": 50}
    assert policy.parse_auto_approval_bounds(
        {"max_limit_usd": 0.01, "max_vehicles_per_auto_move": 1})["max_limit_usd"] == 0.01


class _Ssm:
    def __init__(self, value=None, error=None):
        self.value, self.error, self.names = value, error, []

    def get_parameter(self, Name):
        self.names.append(Name)
        if self.error:
            raise self.error
        return {"Parameter": {"Value": self.value}}


def test_the_parameter_is_read_by_the_name_given():
    ssm = _Ssm('{"max_limit_usd": 300, "max_vehicles_per_auto_move": 3}')
    name = "/vsa/staging/avx/fi-rebalancing/auto-approval-bounds"
    assert policy.load_auto_approval_bounds(ssm, name) == {
        "max_limit_usd": 300.0, "max_vehicles_per_auto_move": 3}
    assert ssm.names == [name]


def test_a_read_failure_is_unavailable():
    _raises_unavailable(policy.load_auto_approval_bounds, _Ssm(error=RuntimeError("AccessDenied")),
                        "/vsa/staging/avx/fi-rebalancing/auto-approval-bounds")


@pytest.mark.parametrize("name", ["", None, "vsa/x", policy.AUTO_APPROVAL_BOUNDS_PARAM])
def test_an_unset_relative_or_unsubstituted_name_is_unavailable_before_any_read(name):
    ssm = _Ssm("{}")
    _raises_unavailable(policy.load_auto_approval_bounds, ssm, name)
    assert ssm.names == []


@pytest.mark.parametrize("limit,ok", [
    (0, True), (500, True), (Decimal("499.99"), True), (500.0, True),
    (500.01, False), (-0.01, False), (True, False), ("100", False), (None, False),
    (float("nan"), False),
])
def test_rule_limit_error(limit, ok):
    bounds = {"max_limit_usd": 500.0, "max_vehicles_per_auto_move": 5}
    assert (policy.rule_limit_error(limit, bounds) is None) is ok


@pytest.mark.parametrize("value,expected", [
    (0, 0.0), (500, 500.0), (Decimal("168.12"), 168.12), (1.5, 1.5),
    (float("inf"), None), (float("-inf"), None), (float("nan"), None), (Decimal("Infinity"), None),
    (True, None), (False, None), ("100", None), (None, None), ([1], None),
    (10 ** 400, None),
])
def test_finite_number(value, expected):
    """Every bounds and limit check reads numbers through this: finite numbers only, never a
    bool or a string (a direct pin; downstream range checks also refuse inf and nan)."""
    assert policy.finite_number(value) == expected
