"""Dismiss write order: no `dismissed` Action without its feedback row.

Issue: issues/2026-11-08-avx-dismiss-write-ordering/. The dismiss route used to
commit `dismissed` first, then read the parent Finding and write the feedback
row, swallowing a failed write. A failure after the commit left a dismissed
Action the broker's demotion check never sees, and the iOS client never retries
a dismissed card.

Property pinned here, on moto DynamoDB with the REAL handler, the REAL
`_lib.avx_scope._load_finding_by_id` and the REAL `_write_feedback_row`:

  * a failed Finding read or a failed feedback write returns 503 and leaves the
    stored Action exactly as it was (status and audit trail);
  * a failed status write returns 503 (the feedback row it already wrote stays,
    as the owner's dismissal);
  * a dismiss retried after that leaves ONE feedback row, not two;
  * a status write that loses a race to another writer answers 200 with the
    Action's current status and KEEPS the feedback row (the owner did tap
    dismiss on a card of that kind; decision recorded in the issue summary);
  * the happy path stores `dismissed` and one feedback row with the Finding's kind.

Failures are real service errors, not stubs: the Finding read fails because the
findings table doesn't exist in moto (`ResourceNotFoundException` from the real
`get_item`), and the feedback write likewise. Only the status-write failure is
injected, because the same table must serve the Action read first.

Mutations (2026-11-08, prompt 362; see the issue's summary.md):
  M1 commit `dismissed` before the read and the write (the old order) -> caught
  M2 swallow the feedback-write failure and carry on                  -> caught
  M3 swallow the Finding-read failure (kind "unknown")                -> caught
  M4 feedback_id back to a random uuid4                               -> caught
  M5 let a status-write exception escape the handler                  -> caught
  M6 answer 200 instead of 503 on a feedback-write failure            -> caught
  M7 log the exception type name instead of the AWS error code         -> caught
  R6 feedback-write log line uses the type name (reviewer, cycle 2)    -> caught
  R7 status-write log line uses the type name (reviewer, cycle 2)      -> caught
  R8 status-write failure logged at WARNING (reviewer, cycle 2)        -> caught
  R10 feedback row keyed to the caller, not the owner (security S1)    -> caught
  R1 a lost race answers 503 instead of 200 (reviewer, cycle 1)        -> caught
  R2 a lost race deletes the feedback row it wrote (reviewer, cycle 1) -> caught

The handler is loaded by FILE PATH under a unique module name, as
agents/supervisor/lambdas/tests/test_avx_feedback_loop_integration.py does, so
it can't collide with another Lambda's top-level ``handler`` module.
"""
from __future__ import annotations

import importlib.util
import json
import logging
import os
import sys
from typing import Any
from unittest.mock import patch

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

_HERE = os.path.dirname(os.path.abspath(__file__))
_HANDLER = os.path.join(_HERE, "..", "handler.py")


def _load_handler():
    spec = importlib.util.spec_from_file_location("avx_actions_handler_dismiss_order", _HANDLER)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


h = _load_handler()
avx_scope = sys.modules["_lib.avx_scope"]

_OWNER = "5f0e1d2c-3b4a-4968-8776-655443322110"
_ACTION_ID = "AVX-ACT-aabbccdd-0000-4000-8000-000000000001"
_FINDING_ID = "FIND-aaaaaaaa-0000-4000-8000-000000000001"
_KIND = "health.brakes"


def _create(ddb, name: str, pk: str, sk: str | None = None) -> None:
    key = [{"AttributeName": pk, "KeyType": "HASH"}]
    attrs = [{"AttributeName": pk, "AttributeType": "S"}]
    if sk:
        key.append({"AttributeName": sk, "KeyType": "RANGE"})
        attrs.append({"AttributeName": sk, "AttributeType": "S"})
    ddb.create_table(TableName=name, KeySchema=key, AttributeDefinitions=attrs,
                     BillingMode="PAY_PER_REQUEST")


def _action(**over: Any) -> dict[str, Any]:
    item = {
        "action_id": _ACTION_ID,
        "finding_id": _FINDING_ID,
        "created_by": f"owner-{_OWNER}",
        "created_at": "2026-11-08T12:00:00Z",
        "updated_at": "2026-11-08T12:00:00Z",
        "target_system": "dms",
        "status": "pending_execution",
        "action_kind": "book_combined_visit",
        "parameters": {},
        "evidence": [],
        "audit_trail": [],
    }
    item.update(over)
    return item


@pytest.fixture()
def world(monkeypatch):
    """moto DynamoDB; tables created per test so each can leave one out."""
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-west-2")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    assert avx_scope._FINDINGS_TABLE == h._FINDINGS_TABLE
    with mock_aws():
        ddb = boto3.resource("dynamodb", region_name="us-west-2")
        h._ddb = ddb
        avx_scope._ddb = ddb

        def make(findings: bool = True, feedback: bool = True, finding_item: bool = True):
            _create(ddb, h._ACTIONS_TABLE, "action_id")
            ddb.Table(h._ACTIONS_TABLE).put_item(Item=_action())
            if findings:
                _create(ddb, h._FINDINGS_TABLE, "finding_id")
                if finding_item:
                    ddb.Table(h._FINDINGS_TABLE).put_item(
                        Item={"finding_id": _FINDING_ID, "finding_kind": _KIND})
            if feedback:
                _create(ddb, h._FEEDBACK_TABLE, "owner_id", "feedback_id")
            return ddb

        yield make
        h._ddb = None
        avx_scope._ddb = None


def _dismiss() -> dict[str, Any]:
    return h._dismiss_action(_ACTION_ID, {"sub": _OWNER})


def _stored_action(ddb) -> dict[str, Any]:
    return ddb.Table(h._ACTIONS_TABLE).get_item(Key={"action_id": _ACTION_ID})["Item"]


def _feedback_rows(ddb) -> list[dict[str, Any]]:
    return ddb.Table(h._FEEDBACK_TABLE).scan()["Items"]


def _assert_untouched(ddb) -> None:
    stored = _stored_action(ddb)
    assert stored["status"] == "pending_execution", stored
    assert stored["audit_trail"] == [], stored
    assert stored["updated_at"] == "2026-11-08T12:00:00Z", stored


def _assert_503(resp: dict[str, Any]) -> None:
    assert resp["statusCode"] == 503, resp
    body = json.loads(resp["body"])
    assert body["error"] == "dismiss_unavailable", body
    # The caller learns to retry, not which table or exception failed.
    assert "ResourceNotFound" not in resp["body"] and "Throttl" not in resp["body"], body


class TestFailuresBeforeTheCommitLeaveTheActionAsItWas:

    def test_finding_read_failure_returns_503_and_writes_nothing(self, world, caplog):
        ddb = world(findings=False)  # real get_item -> ResourceNotFoundException
        with caplog.at_level(logging.ERROR):
            resp = _dismiss()
        _assert_503(resp)
        _assert_untouched(ddb)
        assert _feedback_rows(ddb) == []
        assert any(r.levelno == logging.ERROR and "finding read failed" in r.getMessage()
                   for r in caplog.records), [r.getMessage() for r in caplog.records]

    def test_the_aws_error_code_is_logged_not_just_clienterror(self, world, caplog):
        """Throttling, AccessDenied and a missing table must be told apart in the log.
        An unmodelled error (ThrottlingException on GetItem) arrives as a bare
        ClientError, so the type name alone would say only "ClientError"."""
        ddb = world()
        err = ClientError({"Error": {"Code": "ThrottlingException", "Message": "Rate exceeded"}},
                          "GetItem")
        with caplog.at_level(logging.ERROR), \
                patch.object(h, "_load_finding_by_id", side_effect=err):
            resp = _dismiss()
        _assert_503(resp)
        _assert_untouched(ddb)
        assert any(r.levelno == logging.ERROR and "ThrottlingException" in r.getMessage()
                   for r in caplog.records), [r.getMessage() for r in caplog.records]

    def test_feedback_write_failure_returns_503_and_leaves_the_action(self, world, caplog):
        ddb = world(feedback=False)  # real put_item -> ResourceNotFoundException
        with caplog.at_level(logging.ERROR):
            resp = _dismiss()
        _assert_503(resp)
        _assert_untouched(ddb)
        # Not swallowed: logged at ERROR, not WARNING.
        assert any(r.levelno == logging.ERROR and "feedback write failed" in r.getMessage()
                   for r in caplog.records), [r.getMessage() for r in caplog.records]


class TestFeedbackWriteErrorCode:

    def test_feedback_write_failure_logs_the_aws_error_code_at_error(self, world, caplog):
        ddb = world()
        err = ClientError({"Error": {"Code": "ThrottlingException", "Message": "Rate exceeded"}},
                          "PutItem")
        with caplog.at_level(logging.WARNING), \
                patch.object(h, "_write_feedback_row", side_effect=err):
            resp = _dismiss()
        _assert_503(resp)
        _assert_untouched(ddb)
        assert any(r.levelno == logging.ERROR and "feedback write failed" in r.getMessage()
                   and "ThrottlingException" in r.getMessage()
                   for r in caplog.records), [(r.levelname, r.getMessage()) for r in caplog.records]


class TestStatusWriteFailure:

    @staticmethod
    def _throttled(*_a, **_kw):
        raise ClientError({"Error": {"Code": "ThrottlingException", "Message": "slow down"}},
                          "UpdateItem")

    def test_status_write_failure_returns_503(self, world, caplog):
        ddb = world()
        with caplog.at_level(logging.WARNING), \
                patch.object(h, "_update_action_status", side_effect=self._throttled):
            resp = _dismiss()
        _assert_503(resp)
        # Logged at ERROR with the AWS error code (a bare ClientError's type name
        # would say only "ClientError").
        assert any(r.levelno == logging.ERROR and "status write failed" in r.getMessage()
                   and "ThrottlingException" in r.getMessage()
                   for r in caplog.records), [(r.levelname, r.getMessage()) for r in caplog.records]
        _assert_untouched(ddb)
        # The feedback row was written first and stays: it records the owner's dismissal.
        rows = _feedback_rows(ddb)
        assert [(r["owner_id"], r["finding_kind"]) for r in rows] == [(_OWNER, _KIND)], rows

    def test_retry_after_a_failed_status_write_leaves_one_feedback_row(self, world):
        ddb = world()
        with patch.object(h, "_update_action_status", side_effect=self._throttled):
            assert _dismiss()["statusCode"] == 503
        resp = _dismiss()
        assert resp["statusCode"] == 200, resp
        assert _stored_action(ddb)["status"] == "dismissed"
        rows = _feedback_rows(ddb)
        assert len(rows) == 1, rows
        assert rows[0]["feedback_id"] == f"dismiss-{_ACTION_ID}"


class TestLostRace:
    """Another writer (an owner PATCH, the executor) changes the status between
    the dismiss's read and its conditional update."""

    def test_lost_race_answers_200_with_the_current_action_and_keeps_the_row(self, world):
        ddb = world()
        # What the dismiss read: still pending. What is stored by the time it writes: executed.
        seen = _action()
        ddb.Table(h._ACTIONS_TABLE).put_item(Item=_action(status="executed"))
        real_get = h._get_action
        calls = []

        def stale_then_real(action_id):
            calls.append(action_id)
            return seen if len(calls) == 1 else real_get(action_id)

        with patch.object(h, "_get_action", side_effect=stale_then_real):
            resp = _dismiss()
        assert resp["statusCode"] == 200, resp
        assert json.loads(resp["body"])["status"] == "executed"
        stored = _stored_action(ddb)
        assert stored["status"] == "executed" and stored["audit_trail"] == [], stored
        rows = _feedback_rows(ddb)
        assert [(r["feedback_id"], r["finding_kind"]) for r in rows] == [
            (f"dismiss-{_ACTION_ID}", _KIND)], rows


class TestHappyPath:

    def test_dismiss_stores_dismissed_and_one_feedback_row(self, world):
        ddb = world()
        resp = _dismiss()
        assert resp["statusCode"] == 200, resp
        assert json.loads(resp["body"])["status"] == "dismissed"
        stored = _stored_action(ddb)
        assert stored["status"] == "dismissed"
        assert [e["to_status"] for e in stored["audit_trail"]] == ["dismissed"]
        rows = _feedback_rows(ddb)
        assert len(rows) == 1, rows
        row = rows[0]
        assert (row["owner_id"], row["finding_kind"], row["action_id"]) == (_OWNER, _KIND, _ACTION_ID)
        assert row["feedback_id"] == f"dismiss-{_ACTION_ID}"
        assert int(row["ttl"]) > 0

    def test_admin_dismiss_on_behalf_keys_the_row_to_the_owner(self, world):
        """W6 (2026-09-19): a platform-admin dismissing for the owner writes the
        demotion row under the OWNER's sub, not the admin's (security review p362 S1)."""
        ddb = world()
        admin = "9a8b7c6d-0000-4000-8000-0000000000ad"
        resp = h._dismiss_action(_ACTION_ID, {"sub": admin, "cognito:groups": "platform-admin"})
        assert resp["statusCode"] == 200, resp
        rows = _feedback_rows(ddb)
        assert [(r["owner_id"], r["action_id"]) for r in rows] == [(_OWNER, _ACTION_ID)], rows
        trail = _stored_action(ddb)["audit_trail"]
        assert [e["by"] for e in trail] == [f"owner-{admin}"], trail

    def test_missing_finding_still_dismisses_with_unknown_kind(self, world):
        """An absent Finding is not a read failure: same as before the change."""
        ddb = world(finding_item=False)
        resp = _dismiss()
        assert resp["statusCode"] == 200, resp
        assert _stored_action(ddb)["status"] == "dismissed"
        assert [r["finding_kind"] for r in _feedback_rows(ddb)] == ["unknown"]

    def test_already_dismissed_writes_nothing(self, world):
        ddb = world(feedback=False)  # any feedback write would raise
        ddb.Table(h._ACTIONS_TABLE).put_item(Item=_action(status="dismissed"))
        resp = _dismiss()
        assert resp["statusCode"] == 200, resp
        assert _stored_action(ddb)["status"] == "dismissed"
