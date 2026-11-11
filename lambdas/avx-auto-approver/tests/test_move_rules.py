"""The evaluator approves moves only within a rule's limit and the bounds (task 8.3, U6).

Spec group8-notes G8-D7. Runs the real handler on moto tables (actions, rules, agent runs),
so the one-decision conditional put and the rules Query behave as DynamoDB does. Patched
seams: ``_load_bounds`` (the SSM read) and, except in the end-to-end test, the dispatch
(recorded, answers executed).

Every "not approved" case asserts no Action was written and nothing was dispatched, and
that the batch reports no failure (the item simply stays in the operator's queue).
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
from decimal import Decimal
from unittest.mock import MagicMock, patch

import boto3
import pytest
from moto import mock_aws

_HERE = os.path.dirname(os.path.abspath(__file__))
_LAMBDA_ROOT = os.path.dirname(_HERE)
_LAMBDAS_ROOT = os.path.dirname(_LAMBDA_ROOT)
_REPO_ROOT = os.path.dirname(_LAMBDAS_ROOT)
for _p in (_LAMBDAS_ROOT, _REPO_ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

_FLEET = "demo-rental-carshare"
_FINDING = "FIND-e8a00000-0000-4000-8000-000000000001"
_T = {"actions": "vsa-test-avx-actions", "rules": "vsa-test-avx-approval-rules",
      "runs": "vsa-test-avx-agent-runs"}
_BOUNDS = {"max_limit_usd": 500.0, "max_vehicles_per_auto_move": 5}
_VINS = ["1HGBH41JXMN109186", "2T1BURHE0JC043821"]
_INTERNAL_BASE = "https://yourapi000.execute-api.us-west-2.amazonaws.com/prod"


def _load_handler():
    spec = importlib.util.spec_from_file_location(
        "auto_approver_handler_g8", os.path.join(_LAMBDA_ROOT, "handler.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _typed(value):
    from boto3.dynamodb.types import TypeSerializer
    return TypeSerializer().serialize(value)


def _params(**changes) -> dict:
    p = {
        "fleet_id": _FLEET, "from_location_id": "ORD-AIRPORT", "to_location_id": "ORD-DOWNTOWN",
        "vehicle_vins": list(_VINS), "forecast_week": "2026-W43",
        "transfer_cost_usd": Decimal("168.12"), "predicted_on_rent_hours_gain": Decimal("48.5"),
        "predicted_turnaways_avoided": 6,
    }
    p.update(changes)
    return {k: v for k, v in p.items() if v is not _DROP}


_DROP = object()


def _record(params=None, *, kind="move_vehicles", target="cms", fleet=_FLEET, seq="seq-1"):
    image = {
        "finding_id": _FINDING, "agent_id": "agent-fi-rebalancing", "fleet_id": fleet,
        "finding_kind": "rebalancing.move", "scope": {"fleet_id": fleet},
        "recommended_action": {"action_kind": kind, "target_system": target,
                               "parameters": params if params is not None else _params()},
        "evidence": [{"kind": "rebalance.vehicle", "vin": _VINS[0]}],
        "expires_at": "2099-12-31T00:00:00Z", "computed_at": "2026-10-20T06:00:00Z",
    }
    return {"eventName": "INSERT",
            "dynamodb": {"SequenceNumber": seq,
                         "NewImage": {k: _typed(v) for k, v in image.items()}}}


class _World:
    def __init__(self, mod, ddb):
        self.mod, self.ddb = mod, ddb
        self.bounds = dict(_BOUNDS)
        self.dispatched: list = []

    def rule(self, rule_id="small-moves", **item):
        row = {"fleet_id": _FLEET, "rule_id": rule_id, "action_kind": "move_vehicles",
               "enabled": True, "limit": Decimal("200"), "created_by": "op",
               "updated_at": "2026-10-19T10:00:00Z"}
        row.update(item)
        row = {k: v for k, v in row.items() if v is not _DROP}
        self.ddb.Table(_T["rules"]).put_item(Item=row)

    def _bounds(self):
        if self.bounds is None:
            from agents.executors.policy import BoundsUnavailable
            raise BoundsUnavailable("injected")
        return dict(self.bounds)

    def run(self, *records, real_dispatch=False):
        from agents.executors.base import ActionStatus, ExecuteResult

        def _fake_dispatch(action, claims, **kw):
            self.dispatched.append((action, claims))
            return ExecuteResult.success(target_ref=action["action_id"], status=ActionStatus.EXECUTED)

        with patch.object(self.mod, "_get_ddb", return_value=self.ddb), \
             patch.object(self.mod, "_load_bounds", self._bounds):
            if real_dispatch:
                return self.mod.handler({"Records": list(records)}, None)
            with patch("agents.executors.registry.dispatch", side_effect=_fake_dispatch):
                return self.mod.handler({"Records": list(records)}, None)

    def actions(self) -> list:
        return self.ddb.Table(_T["actions"]).scan(ConsistentRead=True)["Items"]

    def assert_not_approved(self, result) -> None:
        assert result["batchItemFailures"] == []
        assert self.actions() == []
        assert self.dispatched == []


@pytest.fixture
def w(monkeypatch):
    monkeypatch.setenv("AVX_ACTIONS_TABLE", _T["actions"])
    monkeypatch.setenv("AVX_RULES_TABLE", _T["rules"])
    monkeypatch.setenv("AVX_AGENT_RUNS_TABLE", _T["runs"])
    monkeypatch.setenv("FI_AUTO_MOVES_ENABLED", "true")
    with mock_aws():
        ddb = boto3.resource("dynamodb", region_name="us-west-2")
        for name, keys in ((_T["actions"], ["action_id"]),
                           (_T["rules"], ["fleet_id", "rule_id"]),
                           (_T["runs"], ["agent_component", "started_at"])):
            ddb.create_table(
                TableName=name,
                KeySchema=[{"AttributeName": k, "KeyType": "HASH" if i == 0 else "RANGE"}
                           for i, k in enumerate(keys)],
                AttributeDefinitions=[{"AttributeName": k, "AttributeType": "S"} for k in keys],
                BillingMode="PAY_PER_REQUEST")
        world = _World(_load_handler(), ddb)
        world.rule()
        yield world


# --- the control: a move within an enabled rule and the bounds is approved ----------

def test_a_move_within_the_rule_and_the_bounds_is_approved_by_the_rule(w):
    result = w.run(_record())
    assert result["batchItemFailures"] == []
    (action,) = w.actions()
    assert action["decision"] == "auto_approved"
    assert action["decided_by"] == {"kind": "rule", "id": f"{_FLEET}/small-moves"}
    assert action["action_kind"] == "move_vehicles"
    assert action["target_system"] == "cms"
    assert action["parameters"]["vehicle_vins"] == _VINS
    assert action["parameters"]["transfer_cost_usd"] == Decimal("168.12")
    assert action["fleet_id"] == _FLEET
    (dispatched, claims), = w.dispatched
    assert dispatched["action_id"] == action["action_id"]
    assert claims == {}


def test_a_replayed_record_decides_once(w):
    w.run(_record(seq="s1"))
    w.run(_record(seq="s2"))
    assert len(w.actions()) == 1
    assert len(w.dispatched) == 1


def test_cost_exactly_at_the_limit_and_count_exactly_at_the_bound_are_approved(w):
    w.rule(limit=Decimal("168.12"))
    w.bounds["max_vehicles_per_auto_move"] = 2
    w.run(_record())
    assert len(w.actions()) == 1


def test_the_first_covering_rule_by_rule_id_decides(w):
    w.rule("a-too-small", limit=Decimal("100"))
    w.rule("b-covers", limit=Decimal("300"))
    w.rule("c-covers", limit=Decimal("400"))
    w.ddb.Table(_T["rules"]).delete_item(Key={"fleet_id": _FLEET, "rule_id": "small-moves"})
    w.run(_record())
    (action,) = w.actions()
    assert action["decided_by"]["id"] == f"{_FLEET}/b-covers"


# --- the switch ----------------------------------------------------------------------

@pytest.mark.parametrize("value", [None, "", "false", "True", "1"])
def test_moves_are_never_approved_unless_switched_on(w, monkeypatch, value):
    if value is None:
        monkeypatch.delenv("FI_AUTO_MOVES_ENABLED")
    else:
        monkeypatch.setenv("FI_AUTO_MOVES_ENABLED", value)
    w.assert_not_approved(w.run(_record()))


# --- what the rule must allow --------------------------------------------------------

def test_a_limit_under_the_cost_is_not_approved(w):
    w.rule(limit=Decimal("168.11"))
    w.assert_not_approved(w.run(_record()))


@pytest.mark.parametrize("limit", [_DROP, True, "500", Decimal("-1"), Decimal("600")])
def test_a_missing_bad_or_over_bound_limit_is_not_approved(w, limit):
    w.rule(limit=limit)
    w.assert_not_approved(w.run(_record()))


@pytest.mark.parametrize("param", [None, "predicted_on_rent_hours_gain"])
def test_a_limited_kind_must_limit_the_transfer_cost(w, monkeypatch, param):
    """The evaluator compares the rule's limit with transfer_cost_usd, so a policy row that
    names no limit parameter, or another one, approves nothing (G8-D7)."""
    import dataclasses
    from agents.executors import policy
    row = policy.ACTION_KINDS["move_vehicles"]
    monkeypatch.setitem(policy.ACTION_KINDS, "move_vehicles",
                        dataclasses.replace(row, rule_limit_param=param))
    w.rule()
    w.assert_not_approved(w.run(_record()))


@pytest.mark.parametrize("rule_id", ["small moves", "a/b", "x" * 129])
def test_a_rule_whose_id_cms_refuses_is_not_approved(w, rule_id):
    """CMS's internal route requires decided_by.id's rule part to match ^[^/\\s]{1,128}$."""
    w.ddb.Table(_T["rules"]).delete_item(Key={"fleet_id": _FLEET, "rule_id": "small-moves"})
    w.rule(rule_id=rule_id)
    w.assert_not_approved(w.run(_record()))


def test_the_rules_are_read_strongly_consistent(w):
    """Review c1: a rule disabled a moment ago must be seen (CMS re-reads it consistently)."""
    seen = []
    real = w.ddb.Table

    def _spy(name):
        table = real(name)
        if name == _T["rules"]:
            orig = table.query

            def query(**kw):
                seen.append(kw)
                return orig(**kw)
            table.query = query
        return table
    with patch.object(w.ddb, "Table", side_effect=_spy):
        w.run(_record())
    assert seen and all(kw.get("ConsistentRead") is True for kw in seen), seen


def test_the_bounds_are_not_read_for_a_fleet_with_no_move_rule(w):
    w.ddb.Table(_T["rules"]).delete_item(Key={"fleet_id": _FLEET, "rule_id": "small-moves"})
    w.rule(rule_id="ack", action_kind="acknowledge_only")
    calls = []
    real = w._bounds
    w._bounds = lambda: (calls.append(1), real())[1]
    w.assert_not_approved(w.run(_record()))
    assert calls == []


def test_the_evaluator_reads_the_bounds_the_env_names(monkeypatch):
    mod = _load_handler()
    seen = []

    class _Ssm:
        def get_parameter(self, Name):
            seen.append(Name)
            return {"Parameter": {"Value": '{"max_limit_usd": 300, "max_vehicles_per_auto_move": 3}'}}
    monkeypatch.setenv("AVX_AUTO_APPROVAL_BOUNDS_PARAM", "/vsa/staging/avx/fi-rebalancing/auto-approval-bounds")
    monkeypatch.setattr(mod, "_ssm", _Ssm())
    assert mod._load_bounds() == {"max_limit_usd": 300.0, "max_vehicles_per_auto_move": 3}
    assert seen == ["/vsa/staging/avx/fi-rebalancing/auto-approval-bounds"]
    monkeypatch.delenv("AVX_AUTO_APPROVAL_BOUNDS_PARAM")
    from agents.executors.policy import BoundsUnavailable
    with pytest.raises(BoundsUnavailable):
        mod._load_bounds()


def test_a_disabled_rule_is_not_approved(w):
    w.rule(enabled=False)
    w.assert_not_approved(w.run(_record()))


def test_another_fleets_rule_is_not_approved(w):
    w.ddb.Table(_T["rules"]).delete_item(Key={"fleet_id": _FLEET, "rule_id": "small-moves"})
    w.ddb.Table(_T["rules"]).put_item(Item={
        "fleet_id": "demo-lastmile-delivery", "rule_id": "small-moves",
        "action_kind": "move_vehicles", "enabled": True, "limit": Decimal("500")})
    w.assert_not_approved(w.run(_record()))


# --- what the move must be -----------------------------------------------------------

@pytest.mark.parametrize("params", [
    _params(vehicle_vins=[]),
    _params(vehicle_vins=[_VINS[0], _VINS[0]]),
    _params(vehicle_vins=[_VINS[0], ""]),
    _params(vehicle_vins=_DROP),
    _params(fleet_id="demo-lastmile-delivery"),
    _params(fleet_id=_DROP),
    _params(transfer_cost_usd=_DROP),
    _params(transfer_cost_usd=Decimal("-1")),
    _params(transfer_cost_usd="168.12"),
    _params(transfer_cost_usd=Decimal("500.01")),
    _params(from_location_id=""),
    _params(to_location_id=_DROP),
    # CMS's internal route refuses these too (review c1): VIN format and the move's figures.
    _params(vehicle_vins=[_VINS[0], "not-a-vin"]),
    _params(vehicle_vins=[_VINS[0], " " + _VINS[1]]),
    _params(vehicle_vins=[_VINS[0], _VINS[1].lower()]),
    _params(forecast_week=_DROP),
    _params(forecast_week=""),
    _params(predicted_on_rent_hours_gain=_DROP),
    _params(predicted_turnaways_avoided=None),
])
def test_a_move_a_rule_cant_cover_is_not_approved(w, params):
    w.rule(limit=Decimal("500"))
    w.assert_not_approved(w.run(_record(params)))


def test_more_vehicles_than_the_bound_is_not_approved(w):
    w.bounds["max_vehicles_per_auto_move"] = 1
    w.assert_not_approved(w.run(_record()))


def test_unreadable_bounds_approve_nothing(w):
    w.bounds = None
    w.assert_not_approved(w.run(_record()))


def test_a_recommended_target_other_than_cms_is_not_approved(w):
    w.assert_not_approved(w.run(_record(target="none")))


def test_acknowledge_only_is_unchanged_by_the_switch(w, monkeypatch):
    monkeypatch.delenv("FI_AUTO_MOVES_ENABLED")
    w.ddb.Table(_T["rules"]).put_item(Item={
        "fleet_id": _FLEET, "rule_id": "ack", "action_kind": "acknowledge_only",
        "enabled": True})
    w.run(_record({}, kind="acknowledge_only", target="none"))
    (action,) = w.actions()
    assert action["action_kind"] == "acknowledge_only"


# --- end to end: the real dispatch signs the request to CMS's internal route -----------

def test_the_decision_is_sent_signed_to_cms_and_recorded_executed(w, monkeypatch):
    from collections import namedtuple

    from agents.executors import registry
    from agents.executors.base import RetryPolicy
    from agents.executors.cms import CmsExecutor

    creds = namedtuple("C", "access_key secret_key token")("TESTEXAMPLEACCESSKEY", "s", "t")
    monkeypatch.setenv("VSA_CMS_FI_INTERNAL_API_ENDPOINT", _INTERNAL_BASE)
    executor = CmsExecutor(sleep_fn=lambda _: None, credentials_fn=lambda: creds,
                           retry_policy=RetryPolicy(retry_count=1, backoff_base_ms=0, backoff_cap_ms=0))
    ok = MagicMock(status_code=200)
    with patch.dict(registry.EXECUTORS, {"cms": executor}), \
         patch("agents.executors.cms.requests.post", return_value=ok) as post:
        result = w.run(_record(), real_dispatch=True)
    assert result["batchItemFailures"] == []
    (call,) = post.call_args_list
    assert call.args == (f"{_INTERNAL_BASE}/api/v1/fleet-intelligence/internal/moves",)
    assert call.kwargs["headers"]["Authorization"].startswith("AWS4-HMAC-SHA256 ")
    assert json.loads(call.kwargs["data"])["actionId"] == w.actions()[0]["action_id"]
    (action,) = w.actions()
    assert action["status"] == "executed"
