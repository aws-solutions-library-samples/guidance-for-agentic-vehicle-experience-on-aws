"""Group 9 integration test: dismissal -> feedback -> demotion, across two modules.

Spec: .kiro/specs/2026-09-18-avx-tier2-ios-cards-and-actions/tasks-g7-g9-fragment.md F9.4

Drives the REAL dismiss path in lambdas/api-avx-actions/handler.py
(``_dismiss_action`` -> ``_write_feedback_row``) and the REAL broker
(``_process_finding``) against one moto DynamoDB, so the feedback row the
broker reads is the one the dismiss handler actually wrote: same table, same
owner key, same ``finding_kind`` and ``ttl`` shape. A unit test that
hand-writes the feedback row cannot catch the two modules disagreeing.

The actions handler is loaded by FILE PATH under a unique module name. Every
AVX Lambda has a top-level ``handler.py``; importing it as ``handler`` would
collide in ``sys.modules`` with other suites and bind tests to the wrong module
(issues/2026-09-23-pytest-test-handler-module-collision).

Not a live test: moto is not DynamoDB Streams, and nothing here runs in AWS.
The live half is Group 10.
"""
from __future__ import annotations

import importlib.util
import os
import sys
from typing import Any
from unittest.mock import MagicMock

import boto3
import pytest
from moto import mock_aws

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.abspath(os.path.join(_HERE, "..", "..", "..", ".."))
_BROKER_DIR = os.path.join(_HERE, "..")
_ACTIONS_HANDLER = os.path.join(_REPO_ROOT, "lambdas", "api-avx-actions", "handler.py")

if _BROKER_DIR not in sys.path:
    sys.path.insert(0, _BROKER_DIR)

import avx_broker as broker  # noqa: E402

_OWNER = "5f0e1d2c-3b4a-4968-8776-655443322110"
_KIND = "health.brakes"


def _load_actions_handler():
    spec = importlib.util.spec_from_file_location(
        "avx_actions_handler_for_feedback_integration", _ACTIONS_HANDLER
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


actions = _load_actions_handler()
avx_scope = sys.modules["_lib.avx_scope"]


def _table(ddb, name: str, pk: str, sk: str | None = None, gsis: list[tuple[str, str]] = ()) -> None:
    attrs = {pk}
    key = [{"AttributeName": pk, "KeyType": "HASH"}]
    if sk:
        attrs.add(sk)
        key.append({"AttributeName": sk, "KeyType": "RANGE"})
    kwargs: dict[str, Any] = {}
    if gsis:
        kwargs["GlobalSecondaryIndexes"] = [{
            "IndexName": idx,
            "KeySchema": [{"AttributeName": attr, "KeyType": "HASH"}],
            "Projection": {"ProjectionType": "ALL"},
        } for idx, attr in gsis]
        attrs.update(attr for _, attr in gsis)
    ddb.create_table(
        TableName=name, KeySchema=key, BillingMode="PAY_PER_REQUEST",
        AttributeDefinitions=[{"AttributeName": a, "AttributeType": "S"} for a in sorted(attrs)],
        **kwargs,
    )


@pytest.fixture()
def loop(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-west-2")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")

    # The two modules must agree on every table they share, or the loop is
    # testing two different stores.
    assert actions._ACTIONS_TABLE == broker._ACTIONS_TABLE
    assert actions._FEEDBACK_TABLE == broker._FEEDBACK_TABLE
    assert avx_scope._FINDINGS_TABLE == actions._FINDINGS_TABLE

    with mock_aws():
        ddb = boto3.resource("dynamodb", region_name="us-west-2")
        _table(ddb, broker._ACTIONS_TABLE, "action_id", gsis=[("by_created_by", "created_by")])
        _table(ddb, broker._FEEDBACK_TABLE, "owner_id", "feedback_id")
        _table(ddb, broker._DEVICES_TABLE, "owner_id", "device_token")
        _table(ddb, broker._RATE_LIMITS_TABLE, "owner_id", "window_key")
        _table(ddb, avx_scope._FINDINGS_TABLE, "finding_id")
        ddb.Table(broker._DEVICES_TABLE).put_item(Item={
            "owner_id": _OWNER,
            "device_token": "tok-integration",
            "per_category_consent": {"safety": True, "coverage": True, "service": True},
            "endpoint_arn": "arn:aws:sns:us-west-2:123456789012:endpoint/APNS_SANDBOX/avx/1",
        })

        broker._ddb, broker._sns, broker._policy = ddb, MagicMock(), None
        actions._ddb = ddb
        avx_scope._ddb = ddb
        yield ddb, broker._sns
        broker._ddb = broker._sns = broker._policy = None
        actions._ddb = None
        avx_scope._ddb = None


def _finding(finding_id: str) -> dict[str, Any]:
    return {
        "finding_id": finding_id,
        "finding_kind": _KIND,
        "severity": "attention",
        "headline": "Brake pad wear",
        "scope": {"owner_id": _OWNER},
        "owner_id": _OWNER,
    }


_POLICY = {"apns_threshold": "attention", "rate_limit": {"day": 10, "hour": 3}, "cardinality_cap": 15}


def _card_for(ddb, finding: dict[str, Any]) -> str:
    """Broker creates the card for ``finding`` and returns its action_id."""
    ddb.Table(avx_scope._FINDINGS_TABLE).put_item(Item=finding)
    result = broker._process_finding(finding, _POLICY)
    assert result["action"] == "in_app_card_and_apns", result
    return result["action_id"]


def test_dismissal_demotes_the_next_finding_of_that_kind(loop):
    ddb, sns = loop
    action_a = _card_for(ddb, _finding("FIND-aaaaaaaa-0000-4000-8000-000000000001"))
    assert sns.publish.call_count == 1

    resp = actions._dismiss_action(action_a, {"sub": _OWNER})
    assert resp["statusCode"] == 200, resp

    feedback = ddb.Table(broker._FEEDBACK_TABLE).scan()["Items"]
    assert [(r["owner_id"], r["finding_kind"]) for r in feedback] == [(_OWNER, _KIND)], (
        "the dismiss handler must write the owner + kind the broker demotes on"
    )

    finding_b = _finding("FIND-bbbbbbbb-0000-4000-8000-000000000002")
    ddb.Table(avx_scope._FINDINGS_TABLE).put_item(Item=finding_b)
    result_b = broker._process_finding(finding_b, _POLICY)

    assert result_b["demotion_reason"] == "demoted"
    assert result_b["action"] == "in_app_card_only"
    assert sns.publish.call_count == 1, "the demoted Finding must not push"


def test_control_without_dismissal_the_next_finding_pushes(loop):
    ddb, sns = loop
    _card_for(ddb, _finding("FIND-aaaaaaaa-0000-4000-8000-000000000011"))

    finding_b = _finding("FIND-bbbbbbbb-0000-4000-8000-000000000012")
    ddb.Table(avx_scope._FINDINGS_TABLE).put_item(Item=finding_b)
    result_b = broker._process_finding(finding_b, _POLICY)

    assert result_b["demotion_reason"] == "no_prior_dismissal"
    assert result_b["action"] == "in_app_card_and_apns"
    assert sns.publish.call_count == 2
