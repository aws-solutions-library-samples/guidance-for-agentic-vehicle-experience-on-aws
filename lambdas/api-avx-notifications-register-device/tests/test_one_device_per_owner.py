"""One device per owner (D-SCOPE-1) — F32.1-alt, against moto's DynamoDB.

Spec: .kiro/specs/2026-09-18-avx-tier2-ios-cards-and-actions/tasks-g7-g9-fragment.md F32.1-alt
Directive: decisions-scope-1to1-2026-09-24.md (commit b44e814)

The broker's consent and push reads use ``Limit=1`` on this table. That is
correct only while every owner has at most one row, and this handler is what
keeps it true. So these tests use moto: a MagicMock table cannot show that a
second page of rows was left behind, or that a failed delete left two rows.

Mutation results: .kiro/specs/2026-09-18-avx-tier2-ios-cards-and-actions/mutations-g7-g9.md
"""
from __future__ import annotations

import json
import os
import sys
from unittest.mock import MagicMock

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

_LAMBDA_ROOT = os.path.join(os.path.dirname(__file__), "..")
_LAMBDAS_ROOT = os.path.dirname(_LAMBDA_ROOT)
for _p in (_LAMBDA_ROOT, _LAMBDAS_ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import handler as h  # noqa: E402

_OWNER = "1c2d3e4f-5a6b-4c7d-8e9f-0a1b2c3d4e5f"
_CONSENT = {"safety": True, "coverage": False, "service": True}


@pytest.fixture()
def devices(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-west-2")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    with mock_aws():
        ddb = boto3.resource("dynamodb", region_name="us-west-2")
        ddb.create_table(
            TableName=h._DEVICES_TABLE,
            KeySchema=[
                {"AttributeName": "owner_id", "KeyType": "HASH"},
                {"AttributeName": "device_token", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "owner_id", "AttributeType": "S"},
                {"AttributeName": "device_token", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        h._ddb = ddb
        yield ddb
        h._ddb = None


def _register(token: str, owner: str = _OWNER) -> dict:
    event = {
        "httpMethod": "POST",
        "resource": "/notifications/register-device",
        "requestContext": {"authorizer": {"claims": {"sub": owner, "cognito:groups": "consumer"}}},
        "body": json.dumps({
            "device_token": token,
            "per_category_consent": _CONSENT,
            "platform": "apns-staging",
        }),
    }
    return h.handler(event, None)


def _seed(ddb, token: str, owner: str = _OWNER) -> None:
    ddb.Table(h._DEVICES_TABLE).put_item(Item={
        "owner_id": owner, "device_token": token,
        "per_category_consent": {"safety": False, "coverage": False, "service": False},
        "platform": "apns-staging",
    })


def _tokens(ddb, owner: str = _OWNER) -> list[str]:
    items = ddb.Table(h._DEVICES_TABLE).scan()["Items"]
    return sorted(i["device_token"] for i in items if i["owner_id"] == owner)


def _wrap_table(ddb, **overrides):
    """Return a Table() replacement that overrides some methods on the devices table."""
    real = ddb.Table

    def table(name):
        t = real(name)
        if name != h._DEVICES_TABLE:
            return t
        wrapper = MagicMock(wraps=t)
        for attr, fn in overrides.items():
            getattr(wrapper, attr).side_effect = fn(t)
        return wrapper
    return table


def test_a_first_registration_writes_one_row(devices):
    assert _register("tok-new")["statusCode"] == 201
    assert _tokens(devices) == ["tok-new"]


def test_b_new_device_replaces_the_old_row(devices):
    _seed(devices, "tok-old")
    assert _register("tok-new")["statusCode"] == 201
    assert _tokens(devices) == ["tok-new"]
    row = devices.Table(h._DEVICES_TABLE).get_item(
        Key={"owner_id": _OWNER, "device_token": "tok-new"})["Item"]
    assert row["per_category_consent"] == _CONSENT


def test_c_same_device_reregistering_is_an_in_place_upsert(devices):
    _seed(devices, "tok-same")
    deletes: list[dict] = []
    devices.Table = _wrap_table(devices, delete_item=lambda t: (
        lambda **kw: deletes.append(kw) or t.delete_item(**kw)))

    assert _register("tok-same")["statusCode"] == 201
    assert deletes == [], "re-registering the same device must not issue a DeleteItem"
    assert _tokens(devices) == ["tok-same"]
    row = devices.Table(h._DEVICES_TABLE).get_item(
        Key={"owner_id": _OWNER, "device_token": "tok-same"})["Item"]
    assert row["per_category_consent"] == _CONSENT


def test_d_delete_failure_returns_500_and_writes_nothing(devices):
    _seed(devices, "tok-old")

    def failing_delete(_t):
        def fn(**_kw):
            raise ClientError({"Error": {"Code": "InternalServerError", "Message": "x"}}, "DeleteItem")
        return fn
    devices.Table = _wrap_table(devices, delete_item=failing_delete)

    resp = _register("tok-new")

    assert resp["statusCode"] == 500
    assert "tok-new" not in json.dumps(resp), "never echo the device token"
    assert _tokens(devices) == ["tok-old"], "no PutItem after a failed delete: no two-row state"


def test_e_prior_rows_across_two_query_pages_are_all_deleted(devices):
    for i in range(3):
        _seed(devices, f"tok-old-{i}")

    def paged_query(t):
        # Force one row per page so LastEvaluatedKey paging is exercised with
        # DynamoDB's real semantics rather than a hand-built page sequence.
        # Merged rather than passed twice, so a handler that sets its own Limit
        # is measured on behaviour, not failed on a duplicate keyword.
        return lambda **kw: t.query(**{"Limit": 1, **kw})
    devices.Table = _wrap_table(devices, query=paged_query)

    assert _register("tok-new")["statusCode"] == 201
    assert _tokens(devices) == ["tok-new"]


def test_other_owners_rows_are_untouched(devices):
    other = "9f8e7d6c-5b4a-4392-8171-605f4e3d2c1b"
    _seed(devices, "tok-other", owner=other)
    _seed(devices, "tok-old")

    assert _register("tok-new")["statusCode"] == 201
    assert _tokens(devices, owner=other) == ["tok-other"]
    assert _tokens(devices) == ["tok-new"]



def test_f_query_failure_returns_500_and_writes_nothing(devices):
    """Review Cycle 5 W3: a Query outage must not fall through to the upsert.

    If it did, the old row would survive beside the new one: the two-row state
    that makes the broker's Limit=1 read pick a device by sort-key order.
    """
    _seed(devices, "tok-old")

    def failing_query(_t):
        def fn(**_kw):
            raise ClientError({"Error": {"Code": "InternalServerError", "Message": "x"}}, "Query")
        return fn
    devices.Table = _wrap_table(devices, query=failing_query)

    resp = _register("tok-new")

    assert resp["statusCode"] == 500
    assert _tokens(devices) == ["tok-old"]
