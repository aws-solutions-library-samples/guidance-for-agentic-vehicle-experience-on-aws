"""Rules for move_vehicles: the first limited kind (stage E task 8.3, U6; group8-notes G8-D6).

Runs the real handler against a moto rules table (pk fleet_id, sk rule_id), so conditional
writes and Decimal storage behave as DynamoDB does. The bounds loader is the one seam
patched: ``_load_bounds`` returns the config file's shape or raises.

Every refusal asserts the code and that the stored rule is exactly what it was before.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
from decimal import Decimal

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

_TABLE = "vsa-staging-avx-approval-rules"
_FLEET = "demo-rental-carshare"
_RULE = "small-moves"
_RESOURCE = "/approval-rules/fleet/{fleet_id}/{rule_id}"
_BOUNDS = {"max_limit_usd": 500.0, "max_vehicles_per_auto_move": 5}


def _load_handler():
    spec = importlib.util.spec_from_file_location(
        "approval_rules_handler_g8", os.path.join(_LAMBDA_ROOT, "handler.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _World:
    def __init__(self, mod, ddb, monkeypatch):
        self.mod = mod
        self.table = ddb.Table(_TABLE)
        self.bounds = dict(_BOUNDS)
        monkeypatch.setattr(mod, "_RULES_TABLE", _TABLE)
        monkeypatch.setattr(mod, "_ddb", ddb)
        monkeypatch.setattr(mod, "_load_bounds", self._bounds)

    def _bounds(self):
        if self.bounds is None:
            from agents.executors.policy import BoundsUnavailable
            raise BoundsUnavailable("injected")
        return dict(self.bounds)

    def put(self, body, rule_id=_RULE):
        event = {
            "httpMethod": "PUT", "resource": _RESOURCE,
            "pathParameters": {"fleet_id": _FLEET, "rule_id": rule_id},
            "requestContext": {"authorizer": {"claims": {
                "sub": "op-sub-1", "cognito:groups": "fleet-operator",
                "custom:fleetIds": _FLEET}}},
            "body": json.dumps(body),
        }
        resp = self.mod.handler(event, None)
        resp["json"] = json.loads(resp["body"]) if resp["body"] else {}
        return resp

    def stored(self, rule_id=_RULE):
        return self.table.get_item(Key={"fleet_id": _FLEET, "rule_id": rule_id},
                                   ConsistentRead=True).get("Item")

    def seed(self, **item):
        row = {"fleet_id": _FLEET, "rule_id": _RULE, "created_by": "op-sub-0",
               "updated_at": "2026-10-19T10:00:00Z"}
        row.update(item)
        self.table.put_item(Item=row)
        return row


@pytest.fixture
def w(monkeypatch):
    monkeypatch.setenv("AVX_FLEET_IDS_CLAIM_TRUSTED", "true")
    with mock_aws():
        ddb = boto3.resource("dynamodb", region_name="us-west-2")
        ddb.create_table(
            TableName=_TABLE,
            KeySchema=[{"AttributeName": "fleet_id", "KeyType": "HASH"},
                       {"AttributeName": "rule_id", "KeyType": "RANGE"}],
            AttributeDefinitions=[{"AttributeName": "fleet_id", "AttributeType": "S"},
                                  {"AttributeName": "rule_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST")
        yield _World(_load_handler(), ddb, monkeypatch)


def _refused(w, resp, status, code, before):
    assert resp["statusCode"] == status, resp["json"]
    assert resp["json"]["code"] == code, resp["json"]
    assert w.stored() == before


# --- create ---------------------------------------------------------------

def test_a_move_rule_is_created_disabled_with_its_limit(w):
    resp = w.put({"action_kind": "move_vehicles", "limit": 200})
    assert resp["statusCode"] == 200, resp["json"]
    assert resp["json"]["enabled"] is False
    assert resp["json"]["limit"] == 200
    row = w.stored()
    assert row["enabled"] is False
    assert row["limit"] == Decimal("200")
    assert row["action_kind"] == "move_vehicles"


@pytest.mark.parametrize("limit", [0, 500, 499.99])
def test_a_limit_from_zero_to_the_bound_is_accepted(w, limit):
    assert w.put({"action_kind": "move_vehicles", "limit": limit})["statusCode"] == 200
    assert w.stored()["limit"] == Decimal(str(float(limit)))


def test_a_move_rule_without_a_limit_is_400(w):
    _refused(w, w.put({"action_kind": "move_vehicles"}), 400, "limit_required", None)


@pytest.mark.parametrize("limit,code", [
    (500.01, "invalid_limit"), (600, "invalid_limit"), (-1, "invalid_limit"),
    (True, "invalid_limit"), ("100", "invalid_limit"), ([100], "invalid_limit"),
    (10 ** 400, "invalid_limit"),
])
def test_a_bad_limit_is_400(w, limit, code):
    _refused(w, w.put({"action_kind": "move_vehicles", "limit": limit}), 400, code, None)


def test_unreadable_bounds_write_nothing(w):
    w.bounds = None
    _refused(w, w.put({"action_kind": "move_vehicles", "limit": 200}), 503,
             "auto_approval_bounds_unavailable", None)


def test_a_move_rule_created_enabled_is_refused(w):
    _refused(w, w.put({"action_kind": "move_vehicles", "limit": 200, "enabled": True}),
             400, "rules_are_created_disabled", None)


def test_a_create_with_a_limit_and_no_kind_is_told_the_kind_is_missing(w):
    _refused(w, w.put({"limit": 200}), 400, "action_kind_required", None)


def test_acknowledge_only_still_takes_no_limit(w):
    _refused(w, w.put({"action_kind": "acknowledge_only", "limit": 5}), 400,
             "limit_not_supported", None)
    # A limit that isn't a number is invalid whatever the kind (a bool is not a number).
    _refused(w, w.put({"action_kind": "acknowledge_only", "limit": True}), 400,
             "invalid_limit", None)
    assert w.put({"action_kind": "acknowledge_only"})["statusCode"] == 200
    assert "limit" not in w.stored()


# --- enable, disable, change the limit -------------------------------------

def test_enabling_keeps_the_limit(w):
    w.seed(action_kind="move_vehicles", enabled=False, limit=Decimal("200"))
    resp = w.put({"enabled": True})
    assert resp["statusCode"] == 200, resp["json"]
    assert w.stored()["enabled"] is True
    assert w.stored()["limit"] == Decimal("200")


def test_enabling_a_rule_whose_limit_is_now_over_the_bound_is_refused(w):
    before = w.seed(action_kind="move_vehicles", enabled=False, limit=Decimal("200"))
    w.bounds["max_limit_usd"] = 100.0
    _refused(w, w.put({"enabled": True}), 400, "invalid_limit", before)


def test_enabling_with_unreadable_bounds_is_refused(w):
    before = w.seed(action_kind="move_vehicles", enabled=False, limit=Decimal("200"))
    w.bounds = None
    _refused(w, w.put({"enabled": True}), 503, "auto_approval_bounds_unavailable", before)


def test_disabling_works_even_when_the_bounds_cant_be_read(w):
    w.seed(action_kind="move_vehicles", enabled=True, limit=Decimal("200"))
    w.bounds = None
    resp = w.put({"enabled": False})
    assert resp["statusCode"] == 200, resp["json"]
    assert w.stored()["enabled"] is False
    assert w.stored()["limit"] == Decimal("200")


def test_raising_the_limit_within_the_bound_keeps_the_rule_enabled(w):
    w.seed(action_kind="move_vehicles", enabled=True, limit=Decimal("200"))
    assert w.put({"limit": 450})["statusCode"] == 200
    assert w.stored()["limit"] == Decimal("450.0")
    assert w.stored()["enabled"] is True


def test_raising_the_limit_past_the_bound_is_refused(w):
    before = w.seed(action_kind="move_vehicles", enabled=True, limit=Decimal("200"))
    _refused(w, w.put({"limit": 501}), 400, "invalid_limit", before)


# --- a kind change is a new standing approval -------------------------------

def test_an_enabled_acknowledge_rule_changed_to_moves_is_stored_disabled(w):
    w.seed(action_kind="acknowledge_only", enabled=True)
    resp = w.put({"action_kind": "move_vehicles", "limit": 200})
    assert resp["statusCode"] == 200, resp["json"]
    row = w.stored()
    assert row["action_kind"] == "move_vehicles"
    assert row["enabled"] is False
    assert row["limit"] == Decimal("200")


def test_a_kind_change_cant_enable_in_the_same_request(w):
    before = w.seed(action_kind="acknowledge_only", enabled=True)
    _refused(w, w.put({"action_kind": "move_vehicles", "limit": 200, "enabled": True}),
             400, "rules_are_created_disabled", before)


def test_a_kind_change_to_moves_needs_its_own_limit(w):
    before = w.seed(action_kind="acknowledge_only", enabled=False)
    _refused(w, w.put({"action_kind": "move_vehicles"}), 400, "limit_required", before)


def test_a_move_rule_changed_to_acknowledge_drops_its_limit(w):
    w.seed(action_kind="move_vehicles", enabled=True, limit=Decimal("200"))
    assert w.put({"action_kind": "acknowledge_only"})["statusCode"] == 200
    row = w.stored()
    assert "limit" not in row
    assert row["enabled"] is False


def test_the_bounds_come_from_the_parameter_the_env_names(monkeypatch):
    mod = _load_handler()
    seen = []

    class _Ssm:
        def get_parameter(self, Name):
            seen.append(Name)
            return {"Parameter": {"Value": '{"max_limit_usd": 500, "max_vehicles_per_auto_move": 5}'}}

    monkeypatch.setattr(mod, "_ssm", _Ssm())
    monkeypatch.setenv("AVX_AUTO_APPROVAL_BOUNDS_PARAM",
                       "/vsa/staging/avx/fi-rebalancing/auto-approval-bounds")
    assert mod._load_bounds() == {"max_limit_usd": 500.0, "max_vehicles_per_auto_move": 5}
    assert seen == ["/vsa/staging/avx/fi-rebalancing/auto-approval-bounds"]
    monkeypatch.delenv("AVX_AUTO_APPROVAL_BOUNDS_PARAM")
    from agents.executors.policy import BoundsUnavailable
    with pytest.raises(Exception) as ei:
        mod._load_bounds()
    assert type(ei.value) is BoundsUnavailable
