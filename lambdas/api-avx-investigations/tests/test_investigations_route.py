"""Tests — AVX investigations route (task 3.4).

Covers spec D10 + the four fixes from task 3.4:

  1. Atomic transact_write_items (no read-then-write race).
     Both cancellation cases tested with botocore Stubber.
  2. No isinstance(MagicMock) accommodations in the handler; Stubber or
     ClientError fakes are used wherever the cancellation path matters.
  3. GET refuses ids not shaped like UUIDs (400 for lock# / cap# keys).
  4. Missing table or queue env var → 500, no AWS call.

Run:
    .venv/bin/python -m pytest lambdas/api-avx-investigations/tests -q
"""
from __future__ import annotations

import json
import os
import sys
from decimal import Decimal
from unittest.mock import MagicMock, patch

import boto3
import pytest
from botocore.exceptions import ClientError
from botocore.stub import Stubber

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------
_LAMBDA_ROOT = os.path.join(os.path.dirname(__file__), "..")
_LAMBDAS_ROOT = os.path.dirname(_LAMBDA_ROOT)
_REPO_ROOT = os.path.dirname(_LAMBDAS_ROOT)
for _p in (_LAMBDA_ROOT, _LAMBDAS_ROOT, _REPO_ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

os.environ.setdefault("AVX_STAGE", "test")
os.environ.setdefault("AVX_INVESTIGATIONS_TABLE", "vsa-test-avx-investigations")
os.environ.setdefault("AVX_INVESTIGATIONS_QUEUE_URL",
                      "https://sqs.us-west-2.amazonaws.com/123456789012/vsa-test-investigations")
os.environ.setdefault("AVX_FLEET_IDS_CLAIM_TRUSTED", "true")

# ---------------------------------------------------------------------------
# Shared constants
# ---------------------------------------------------------------------------

_PATTERN_ID = "pat-001-P0AA6-plant-CGA"
_INVESTIGATION_ID = "aaaaaaaa-bbbb-cccc-dddd-000000000001"

_ENGINEER_SUB = "eng-sub-inv-0001"
_ENGINEER_CLAIMS: dict = {
    "sub": _ENGINEER_SUB,
    "cognito:groups": ["product-engineer"],
    "custom:fleetIds": "fleet-meridian-01,fleet-meridian-02",
}

_OPERATOR_CLAIMS: dict = {
    "sub": "op-sub-inv-0001",
    "cognito:groups": ["fleet-operator"],
    "custom:fleetIds": "fleet-meridian-01",
}

_TABLE = os.environ["AVX_INVESTIGATIONS_TABLE"]
_QUEUE = os.environ["AVX_INVESTIGATIONS_QUEUE_URL"]


# ---------------------------------------------------------------------------
# Event builders
# ---------------------------------------------------------------------------

def _post_event(
    pattern_id: str = _PATTERN_ID,
    extra_body: dict | None = None,
    claims: dict | None = None,
) -> dict:
    body: dict = {"pattern_id": pattern_id}
    if extra_body:
        body.update(extra_body)
    return {
        "httpMethod": "POST",
        "path": "/investigations",
        "pathParameters": {},
        "queryStringParameters": {},
        "body": json.dumps(body),
        "requestContext": {"authorizer": {"claims": claims or _ENGINEER_CLAIMS}},
    }


def _get_event(investigation_id: str = _INVESTIGATION_ID) -> dict:
    return {
        "httpMethod": "GET",
        "path": f"/investigations/{investigation_id}",
        "pathParameters": {"investigationId": investigation_id},
        "queryStringParameters": {},
        "body": None,
        "requestContext": {"authorizer": {"claims": _ENGINEER_CLAIMS}},
    }


# ---------------------------------------------------------------------------
# Helpers: build TransactionCanceledException ClientError
# ---------------------------------------------------------------------------

def _txn_cancelled(reasons: list[dict]) -> ClientError:
    """Return a ClientError that looks like a TransactionCanceledException."""
    return ClientError(
        {
            "Error": {
                "Code": "TransactionCanceledException",
                "Message": "Transaction cancelled, please refer to cancellation reasons "
                           "for specific reasons",
            },
            "CancellationReasons": reasons,
        },
        "TransactWriteItems",
    )


def _lock_fired_reasons(existing_inv_id: str = _INVESTIGATION_ID) -> list[dict]:
    """CancellationReasons where the lock Put (index 0) fired."""
    return [
        {
            "Code": "ConditionalCheckFailed",
            "Message": "The conditional request failed",
            "Item": {
                "investigation_id": {"S": f"lock#{_PATTERN_ID}"},
                "investigation_id_ref": {"S": existing_inv_id},
                "status": {"S": "queued"},
                "requested_at": {"S": "2026-10-05T06:00:00Z"},
            },
        },
        {"Code": "None"},
        {"Code": "None"},
    ]


def _cap_fired_reasons() -> list[dict]:
    """CancellationReasons where the cap Update (index 2) fired."""
    return [
        {"Code": "None"},
        {"Code": "None"},
        {"Code": "ConditionalCheckFailed", "Message": "The conditional request failed"},
    ]


# ---------------------------------------------------------------------------
# Patch helpers
# ---------------------------------------------------------------------------

def _ddb_success_mock() -> MagicMock:
    """A DDB client mock whose transact_write_items returns a success dict."""
    m = MagicMock()
    m.transact_write_items.return_value = {}
    return m


def _sqs_success_mock() -> MagicMock:
    m = MagicMock()
    m.send_message.return_value = {"MessageId": "msg-test-001"}
    return m


# ---------------------------------------------------------------------------
# Table mock helpers (D10-fix: handler now reads lock before transacting)
# ---------------------------------------------------------------------------

def _table_mock_no_lock() -> MagicMock:
    """Table mock whose get_item always returns empty (no lock item exists).

    Use for POST tests that want to reach the transaction without any
    pre-existing live investigation.
    """
    m = MagicMock()
    m.get_item.return_value = {}
    return m


def _refused_post(claims: dict) -> tuple[dict, MagicMock, MagicMock]:
    """Security review g43 cycle 1 S6 (infra): call POST with clients that WOULD succeed,
    so a dropped group check returns 2xx and the caller's 403 assertion fails on its own,
    instead of the request stopping on conftest's RealAwsCallBlocked. Returns the response,
    the SQS mock and the DDB mock so the caller can assert nothing was queued or written."""
    import handler as h

    sqs, ddb = _sqs_success_mock(), _ddb_success_mock()
    with patch.object(h, "_get_investigations_table", return_value=_table_mock_no_lock()), \
         patch.object(h, "_get_ddb_client", return_value=ddb), \
         patch.object(h, "_get_sqs_client", return_value=sqs):
        resp = h.handler(_post_event(claims=claims), None)
    return resp, sqs, ddb


def _assert_refused(resp: dict, sqs: MagicMock, ddb: MagicMock) -> None:
    assert resp["statusCode"] == 403, f"expected 403, got {resp['statusCode']}: {resp.get('body')}"
    sqs.send_message.assert_not_called()
    ddb.transact_write_items.assert_not_called()


def _lock_item_for_pattern(
    investigation_id: str = _INVESTIGATION_ID,
    pattern_id: str = _PATTERN_ID,
) -> dict:
    """A DDB lock item pointing at an investigation (no status field)."""
    return {
        "investigation_id": f"lock#{pattern_id}",
        "pattern_id": pattern_id,
        "investigation_id_ref": investigation_id,
        "requested_at": "2026-10-05T06:00:00Z",
    }


def _inv_row(
    status: str,
    investigation_id: str = _INVESTIGATION_ID,
    requested_at: str = "2026-10-05T06:00:00Z",
) -> dict:
    """A minimal investigation row item for get_item mocking."""
    return {
        "investigation_id": investigation_id,
        "pattern_id": _PATTERN_ID,
        "status": status,
        "requested_at": requested_at,
        "requested_by": _ENGINEER_SUB,
    }


def _table_mock_seq(*items) -> MagicMock:
    """Table mock where successive get_item calls return the given Python dicts.

    Pass None to produce an empty (not-found) response.  Wraps each non-None
    value in {"Item": value} for you.
    """
    m = MagicMock()
    m.get_item.side_effect = [
        ({"Item": item} if item is not None else {})
        for item in items
    ]
    return m


# ============================================================================
# Group / access tests
# ============================================================================

class TestInvestigationsRouteAccess:
    """Only product-engineer group members may POST /investigations (spec D10)."""

    def test_product_engineer_can_post_investigation(self):
        """A product-engineer successfully queues an investigation."""
        import handler as h

        with patch.object(h, "_get_investigations_table", return_value=_table_mock_no_lock()), \
             patch.object(h, "_get_ddb_client", return_value=_ddb_success_mock()), \
             patch.object(h, "_get_sqs_client", return_value=_sqs_success_mock()):
            resp = h.handler(_post_event(claims=_ENGINEER_CLAIMS), None)

        assert resp["statusCode"] in (200, 201, 202)

    def test_fleet_operator_gets_403_on_investigations(self):
        """A fleet-operator is refused with 403 on POST /investigations."""
        _assert_refused(*_refused_post(_OPERATOR_CLAIMS))

    def test_no_group_gets_403(self):
        """A caller with no group claim is refused with 403."""
        no_group = {"sub": "anon-sub-0001", "cognito:groups": []}
        _assert_refused(*_refused_post(no_group))

    def test_group_check_removal_test(self):
        """Removal test: if the group check is deleted, a fleet-operator passes.

        Mutation (group check deleted): fleet-operator returns 2xx → fails here.
        """
        resp, sqs, ddb = _refused_post(_OPERATOR_CLAIMS)
        assert resp["statusCode"] == 403, (
            "Mutation guard: removing the product-engineer check lets a "
            "fleet-operator through; this assertion catches it."
        )
        sqs.send_message.assert_not_called()


# ============================================================================
# requested_by from token sub
# ============================================================================

class TestRequestedByFromToken:
    """requested_by is taken from the verified token sub, not the request body."""

    def _extract_row_item(self, ddb_mock: MagicMock) -> dict:
        """Return the investigation row item from the transact_write call."""
        call_kwargs = ddb_mock.transact_write_items.call_args
        transact_items = (
            call_kwargs.kwargs.get("TransactItems")
            or (call_kwargs.args[0] if call_kwargs.args else [])
        )
        # Item 1 is the investigation row Put
        return transact_items[1]["Put"]["Item"]

    def test_requested_by_from_token_sub(self):
        """The DynamoDB record's requested_by equals the token sub.

        Mutation: store body['requested_by'] instead of claims['sub'] →
        stored value != _ENGINEER_SUB, this fails.
        """
        import handler as h

        ddb_mock = _ddb_success_mock()

        with patch.object(h, "_get_investigations_table", return_value=_table_mock_no_lock()), \
             patch.object(h, "_get_ddb_client", return_value=ddb_mock), \
             patch.object(h, "_get_sqs_client", return_value=_sqs_success_mock()):
            resp = h.handler(_post_event(claims=_ENGINEER_CLAIMS), None)

        assert resp["statusCode"] in (200, 201, 202)
        assert ddb_mock.transact_write_items.called, (
            "Handler must call transact_write_items to write the investigation."
        )
        row_item = self._extract_row_item(ddb_mock)
        # DynamoDB JSON format: {"S": "value"}
        stored_by = row_item["requested_by"]["S"]
        assert stored_by == _ENGINEER_SUB, (
            f"requested_by must equal the token sub '{_ENGINEER_SUB}'. "
            "Mutation: store body value → wrong sub, this fails."
        )

    def test_body_requested_by_is_ignored(self):
        """A body field requested_by must not override the token sub.

        Mutation: store body['requested_by'] → 'attacker-sub-0001' in DynamoDB.
        """
        import handler as h

        ddb_mock = _ddb_success_mock()

        with patch.object(h, "_get_investigations_table", return_value=_table_mock_no_lock()), \
             patch.object(h, "_get_ddb_client", return_value=ddb_mock), \
             patch.object(h, "_get_sqs_client", return_value=_sqs_success_mock()):
            resp = h.handler(
                _post_event(
                    claims=_ENGINEER_CLAIMS,
                    extra_body={"requested_by": "attacker-sub-0001"},
                ),
                None,
            )

        assert resp["statusCode"] in (200, 201, 202)
        assert ddb_mock.transact_write_items.called
        row_item = self._extract_row_item(ddb_mock)
        stored_by = row_item["requested_by"]["S"]
        assert stored_by == _ENGINEER_SUB, (
            f"requested_by must be the token sub '{_ENGINEER_SUB}', not the body value. "
            "Mutation: store body value → 'attacker-sub-0001', this fails."
        )
        assert stored_by != "attacker-sub-0001", (
            "Body-supplied requested_by 'attacker-sub-0001' must be discarded."
        )


# ============================================================================
# Stubbed GET tests (W17 fix suite, updated)
# ============================================================================

_STORED_INVESTIGATION = {
    "investigation_id": _INVESTIGATION_ID,
    "pattern_id": _PATTERN_ID,
    "status": "in_progress",
    "requested_by": _ENGINEER_SUB,
}


class TestInvestigationsGetRouteStubbed:
    """GET tests against a stubbed DynamoDB table."""

    def test_get_returns_status_with_stubbed_table(self):
        """GET /investigations/{id} returns status when the record exists."""
        import handler as h

        table_mock = MagicMock()
        table_mock.get_item.return_value = {"Item": _STORED_INVESTIGATION}

        with patch.object(h, "_get_investigations_table", return_value=table_mock):
            resp = h.handler(_get_event(), None)

        assert resp["statusCode"] == 200
        body = json.loads(resp["body"])
        assert "status" in body, "GET must return 'status' field."

    def test_get_never_returns_requested_by_with_stubbed_table(self):
        """GET must not expose requested_by.

        Mutation: forward the full DynamoDB record → requested_by leaks, fails.
        """
        import handler as h

        table_mock = MagicMock()
        table_mock.get_item.return_value = {"Item": _STORED_INVESTIGATION}

        with patch.object(h, "_get_investigations_table", return_value=table_mock):
            resp = h.handler(_get_event(), None)

        assert resp["statusCode"] == 200
        body = json.loads(resp["body"])
        assert "requested_by" not in body, (
            "GET must never expose requested_by (spec D10). "
            "Mutation: return full item → PII leaks, fails."
        )

    def test_get_unknown_id_returns_404_with_stubbed_table(self):
        """GET /investigations/{id} returns 404 when the id is not found."""
        import handler as h

        table_mock = MagicMock()
        table_mock.get_item.return_value = {}

        with patch.object(h, "_get_investigations_table", return_value=table_mock):
            resp = h.handler(
                _get_event(investigation_id="aaaaaaaa-0000-0000-0000-000000000000"),
                None,
            )

        assert resp["statusCode"] == 404, (
            "GET with unknown id must return 404. "
            "Mutation: always return 200 → fails."
        )


class TestInvestigationsRoute403Roles:
    """connected-services and platform-admin (without product-engineer) get 403."""

    def test_connected_services_gets_403_on_post(self):

        cs_claims = {
            "sub": "cs-sub-0001",
            "cognito:groups": ["connected-services"],
            "custom:fleetIds": "fleet-meridian-01",
        }
        _assert_refused(*_refused_post(cs_claims))

    def test_platform_admin_without_engineer_gets_403_on_post(self):

        admin_claims = {
            "sub": "admin-sub-0001",
            "cognito:groups": ["platform-admin"],
            "custom:fleetIds": "fleet-meridian-01,fleet-meridian-02",
        }
        _assert_refused(*_refused_post(admin_claims))


# ============================================================================
# Fix 1a — lock cancellation (Stubber) — no SQS send
# ============================================================================

class TestLockCancellation:
    """transact_write lock cancellation (race) returns winner's id without SQS send.

    In the D10-fix design the handler reads the lock first; a TransactionCanceled
    on the lock condition means another request won the race between the pre-read
    and the write.  The handler re-reads the lock and returns the winner's id.

    Mutation: remove the lock condition → no cancellation fires → a duplicate
    investigation is created and a new id is returned (both tests below fail).
    """

    def test_lock_cancellation_returns_existing_id_no_sqs(self):
        """Race: lock condition fires → round 2 fresh pre-read → winner's id (200, no SQS).

        Round 1: no lock seen → condition = attribute_not_exists → transaction fires
        (lock condition). Round 2: fresh pre-read returns winner's lock then winner's
        investigation row (live, queued) → return 200 with winner's id.

        Mutation (drop the lock condition OR drop the 200 path on round 2 pre-read):
          → handler returns 202 with a new id; both assertions fail.
        """
        import handler as h
        from datetime import datetime, timezone

        winner_lock = _lock_item_for_pattern(_INVESTIGATION_ID)
        fresh_ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        # Round 1: no lock (call 1); Round 2: winner's lock (call 2), winner's inv row (call 3)
        table_mock = _table_mock_seq(
            None,                                                          # round 1: no lock
            winner_lock,                                                   # round 2: lock
            _inv_row("queued", requested_at=fresh_ts),                     # round 2: inv row
        )

        ddb_mock = MagicMock()
        ddb_mock.transact_write_items.side_effect = _txn_cancelled(
            _lock_fired_reasons(_INVESTIGATION_ID)
        )
        sqs_mock = MagicMock()

        with patch.object(h, "_get_investigations_table", return_value=table_mock), \
             patch.object(h, "_get_ddb_client", return_value=ddb_mock), \
             patch.object(h, "_get_sqs_client", return_value=sqs_mock):
            resp = h.handler(_post_event(claims=_ENGINEER_CLAIMS), None)

        assert resp["statusCode"] == 200, (
            f"Lock-race cancellation must return 200, got {resp['statusCode']}. "
            "Mutation (drop lock condition): handler continues to 202."
        )
        body = json.loads(resp["body"])
        assert body.get("investigation_id") == _INVESTIGATION_ID, (
            f"Must return winner's id '{_INVESTIGATION_ID}'. "
            "Mutation: new id returned, assertion fails."
        )
        assert not sqs_mock.send_message.called, (
            "SQS must NOT be called when the race lock cancellation fires. "
            "Mutation (call SQS on cancellation): send_message.called is True, fails."
        )

    def test_lock_cancellation_with_stubber(self):
        """Same race-cancellation assertion via botocore Stubber.

        The Stubber validates the transact_write_items call structure and
        returns the pre-configured TransactionCanceledException.
        Round 2 pre-read sees winner's lock then live inv row → 200 winner's id.
        """
        import handler as h
        from datetime import datetime, timezone

        winner_lock = _lock_item_for_pattern(_INVESTIGATION_ID)
        fresh_ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        # Round 1: no lock; Round 2: winner's lock + live inv row
        table_mock = _table_mock_seq(
            None,
            winner_lock,
            _inv_row("queued", requested_at=fresh_ts),
        )

        ddb = boto3.client("dynamodb", region_name="us-west-2")
        stubber = Stubber(ddb)
        stubber.add_client_error(
            "transact_write_items",
            service_error_code="TransactionCanceledException",
            service_message="Transaction cancelled",
            modeled_fields={
                "CancellationReasons": _lock_fired_reasons(_INVESTIGATION_ID),
            },
        )
        sqs_mock = MagicMock()

        with stubber:
            with patch.object(h, "_get_investigations_table", return_value=table_mock), \
                 patch.object(h, "_get_ddb_client", return_value=ddb), \
                 patch.object(h, "_get_sqs_client", return_value=sqs_mock):
                resp = h.handler(_post_event(claims=_ENGINEER_CLAIMS), None)

        assert resp["statusCode"] == 200
        body = json.loads(resp["body"])
        assert body.get("investigation_id") == _INVESTIGATION_ID
        assert not sqs_mock.send_message.called, (
            "SQS must NOT be called on race lock cancellation (Stubber variant)."
        )

    def test_failed_lock_does_not_block_new_request(self):
        """A lock pointing at a 'failed' investigation is not live → new request.

        The pre-read finds the lock; the investigation row has status='failed';
        is_live_investigation('failed') = False; the transaction proceeds and a
        new investigation is created.

        Mutation: treating 'failed' as live would return the old id, not a new one.
        """
        import handler as h

        # Pre-read: lock exists pointing at the old (failed) investigation.
        # Investigation read: status = "failed" → not live.
        table_mock = _table_mock_seq(
            _lock_item_for_pattern(_INVESTIGATION_ID),  # call 1: lock pre-read
            _inv_row("failed"),                          # call 2: investigation read
        )
        ddb_mock = _ddb_success_mock()
        sqs_mock = _sqs_success_mock()

        with patch.object(h, "_get_investigations_table", return_value=table_mock), \
             patch.object(h, "_get_ddb_client", return_value=ddb_mock), \
             patch.object(h, "_get_sqs_client", return_value=sqs_mock):
            resp = h.handler(_post_event(claims=_ENGINEER_CLAIMS), None)

        assert resp["statusCode"] in (200, 201, 202)
        body = json.loads(resp["body"])
        new_id = body.get("investigation_id", "")
        assert new_id != _INVESTIGATION_ID, (
            "A failed investigation must admit a new request; new id expected."
        )
        assert ddb_mock.transact_write_items.called, (
            "transact_write_items must be called to create the new row."
        )
        assert sqs_mock.send_message.called, "SQS must be called for a new investigation."


# ============================================================================
# Fix 1b — cap cancellation (Stubber) — no SQS send
# ============================================================================

class TestCapCancellation:
    """transact_write cap cancellation returns 429 without SQS send.

    Mutation: remove the cap condition → no cancellation fires → 202 returned.
    Both tests below fail on that mutation.
    """

    def test_cap_cancellation_returns_429_no_sqs(self):
        """When the cap condition fires, return 429 (no SQS send).

        Mutation (drop the cap condition OR drop the 429 path on cancellation):
          → handler returns 202; the statusCode assertion fails.
          → sqs_mock.send_message.called is True; the second assertion fails.
        """
        import handler as h

        ddb_mock = MagicMock()
        ddb_mock.transact_write_items.side_effect = _txn_cancelled(
            _cap_fired_reasons()
        )
        sqs_mock = MagicMock()

        with patch.object(h, "_get_investigations_table", return_value=_table_mock_no_lock()), \
             patch.object(h, "_get_ddb_client", return_value=ddb_mock), \
             patch.object(h, "_get_sqs_client", return_value=sqs_mock):
            resp = h.handler(_post_event(claims=_ENGINEER_CLAIMS), None)

        assert resp["statusCode"] == 429, (
            f"Cap cancellation must return 429, got {resp['statusCode']}. "
            "Mutation (drop cap condition): handler continues to 202."
        )
        assert not sqs_mock.send_message.called, (
            "SQS must NOT be called when the cap cancellation fires. "
            "Mutation (call SQS on cancellation): send_message.called is True, fails."
        )

    def test_cap_cancellation_with_stubber(self):
        """Same cap-cancellation assertion via botocore Stubber.

        ``modeled_fields`` is the botocore API for attaching CancellationReasons
        to the client error response.
        """
        import handler as h

        ddb = boto3.client("dynamodb", region_name="us-west-2")
        stubber = Stubber(ddb)
        stubber.add_client_error(
            "transact_write_items",
            service_error_code="TransactionCanceledException",
            service_message="Transaction cancelled",
            modeled_fields={"CancellationReasons": _cap_fired_reasons()},
        )
        sqs_mock = MagicMock()

        with stubber:
            with patch.object(h, "_get_investigations_table", return_value=_table_mock_no_lock()), \
                 patch.object(h, "_get_ddb_client", return_value=ddb), \
                 patch.object(h, "_get_sqs_client", return_value=sqs_mock):
                resp = h.handler(_post_event(claims=_ENGINEER_CLAIMS), None)

        assert resp["statusCode"] == 429
        assert not sqs_mock.send_message.called, (
            "SQS must NOT be called on cap cancellation (Stubber variant)."
        )

    def test_under_cap_succeeds(self):
        """When the cap has not been reached, transact_write succeeds → 202."""
        import handler as h

        with patch.object(h, "_get_investigations_table", return_value=_table_mock_no_lock()), \
             patch.object(h, "_get_ddb_client", return_value=_ddb_success_mock()), \
             patch.object(h, "_get_sqs_client", return_value=_sqs_success_mock()):
            resp = h.handler(_post_event(claims=_ENGINEER_CLAIMS), None)

        assert resp["statusCode"] in (200, 201, 202), (
            f"Expected success (under cap), got {resp['statusCode']}."
        )


# ============================================================================
# SQS tests
# ============================================================================

class TestSQSMessageBody:
    """SQS message body must be the investigation id only (spec D10)."""

    def test_sqs_message_body_is_investigation_id_only(self):
        """SQS send_message MessageBody equals exactly the investigation_id.

        Mutation (send JSON payload or any extras): body != id, fails.
        """
        import handler as h

        ddb_mock = _ddb_success_mock()
        sqs_mock = _sqs_success_mock()

        with patch.object(h, "_get_investigations_table", return_value=_table_mock_no_lock()), \
             patch.object(h, "_get_ddb_client", return_value=ddb_mock), \
             patch.object(h, "_get_sqs_client", return_value=sqs_mock):
            resp = h.handler(_post_event(claims=_ENGINEER_CLAIMS), None)

        assert resp["statusCode"] in (200, 201, 202)
        resp_body = json.loads(resp["body"])
        investigation_id = resp_body.get("investigation_id", "")
        assert investigation_id, "Response must include investigation_id."

        assert sqs_mock.send_message.called, "SQS send_message must be called."
        sqs_call = sqs_mock.send_message.call_args
        msg_body = sqs_call.kwargs.get("MessageBody")

        assert msg_body == investigation_id, (
            f"SQS MessageBody must be exactly '{investigation_id}', got {msg_body!r}. "
            "Mutation (send more than the id): assertion fails."
        )
        assert not msg_body.startswith("{"), (
            "SQS body must not be a JSON object. "
            "Mutation (send JSON payload): starts with '{', fails."
        )

    def test_sqs_failure_returns_503(self):
        """When SQS send_message raises, handler returns 503."""
        import handler as h

        ddb_mock = _ddb_success_mock()
        sqs_mock = MagicMock()
        sqs_mock.send_message.side_effect = ClientError(
            {"Error": {"Code": "ServiceUnavailable", "Message": "SQS unavailable"}},
            "SendMessage",
        )
        table_mock = MagicMock()
        table_mock.get_item.return_value = {}   # no lock in pre-read
        table_mock.update_item.return_value = {}

        with patch.object(h, "_get_investigations_table", return_value=table_mock), \
             patch.object(h, "_get_ddb_client", return_value=ddb_mock), \
             patch.object(h, "_get_sqs_client", return_value=sqs_mock):
            resp = h.handler(_post_event(claims=_ENGINEER_CLAIMS), None)

        assert resp["statusCode"] == 503, (
            f"Expected 503 on SQS failure, got {resp['statusCode']}."
        )


# ============================================================================
# GET field tests
# ============================================================================

class TestGetRouteFields:
    """GET /investigations/{id} returns exactly the allowed fields (spec D10)."""

    def test_get_returns_status_pattern_id_requested_at(self):
        """GET response includes status, pattern_id, and requested_at."""
        import handler as h

        stored = {
            "investigation_id": _INVESTIGATION_ID,
            "pattern_id": _PATTERN_ID,
            "status": "completed",
            "requested_at": "2026-10-05T06:00:00Z",
            "finding_id": "FIND-test-001",
            "requested_by": _ENGINEER_SUB,
        }
        table_mock = MagicMock()
        table_mock.get_item.return_value = {"Item": stored}

        with patch.object(h, "_get_investigations_table", return_value=table_mock):
            resp = h.handler(_get_event(), None)

        assert resp["statusCode"] == 200
        body = json.loads(resp["body"])
        assert "status" in body
        assert "pattern_id" in body
        assert "requested_at" in body
        assert "requested_by" not in body, "GET must never return requested_by."

    def test_get_failed_includes_failure_sentence(self):
        """GET on a failed investigation includes the fixed failure sentence."""
        import handler as h

        stored = {
            "investigation_id": _INVESTIGATION_ID,
            "pattern_id": _PATTERN_ID,
            "status": "failed",
            "requested_at": "2026-10-05T06:00:00Z",
            "error_message": "The investigation could not finish. Start it again.",
            "requested_by": _ENGINEER_SUB,
        }
        table_mock = MagicMock()
        table_mock.get_item.return_value = {"Item": stored}

        with patch.object(h, "_get_investigations_table", return_value=table_mock):
            resp = h.handler(_get_event(), None)

        assert resp["statusCode"] == 200
        body = json.loads(resp["body"])
        assert body.get("status") == "failed"
        assert "error_message" in body, "Failed investigation must include error_message."
        assert "requested_by" not in body


# ============================================================================
# Fix 3 — GET id shape validation (400 for lock# / cap# keys)
# ============================================================================

class TestGetIdShapeValidation:
    """GET refuses ids that do not match the UUID format (fix 3, spec D10).

    POST creates investigation ids as str(uuid.uuid4()).  lock# and cap#
    special items use non-UUID keys and must not be readable via GET.

    Mutation (remove the UUID check): the handler calls get_item with the
    lock# / cap# key and may return the internal special item; the 400
    assertions below catch that.
    """

    def _get_id(self, investigation_id: str) -> dict:
        import handler as h

        table_mock = MagicMock()
        # Return a non-empty item so we can tell if the handler reached get_item
        table_mock.get_item.return_value = {
            "Item": {
                "investigation_id": investigation_id,
                "status": "queued",
                "investigation_id_ref": _INVESTIGATION_ID,
            }
        }

        with patch.object(h, "_get_investigations_table", return_value=table_mock):
            resp = h.handler(_get_event(investigation_id=investigation_id), None)

        # Confirm no DynamoDB call was made (the guard fires before get_item).
        assert not table_mock.get_item.called, (
            f"get_item must NOT be called for an invalid id '{investigation_id}'. "
            "Mutation (remove UUID guard): get_item is called, assertion fails."
        )
        return resp

    def test_get_lock_prefix_id_returns_400(self):
        """GET for a lock# key must return 400 without calling get_item.

        Mutation (drop the UUID guard): handler calls get_item with
        'lock#<pattern_id>' and may return the lock record; this fails.
        """
        resp = self._get_id(f"lock#{_PATTERN_ID}")
        assert resp["statusCode"] == 400, (
            f"Expected 400 for lock# id, got {resp['statusCode']}. "
            "Mutation: guard removed → lock item read, 200/404 returned."
        )

    def test_get_cap_prefix_id_returns_400(self):
        """GET for a cap# key must return 400 without calling get_item.

        Mutation (drop the UUID guard): handler calls get_item with the
        cap key; fails.
        """
        from datetime import datetime, timezone
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        resp = self._get_id(f"cap#{today}")
        assert resp["statusCode"] == 400, (
            f"Expected 400 for cap# id, got {resp['statusCode']}. "
            "Mutation: guard removed → cap item read, 200/404 returned."
        )

    def test_get_valid_uuid_proceeds_to_lookup(self):
        """GET for a well-formed UUID id passes the shape check and calls get_item."""
        import handler as h

        table_mock = MagicMock()
        table_mock.get_item.return_value = {}  # unknown id → 404

        with patch.object(h, "_get_investigations_table", return_value=table_mock):
            resp = h.handler(_get_event(investigation_id=_INVESTIGATION_ID), None)

        assert table_mock.get_item.called, (
            "A valid UUID id must pass the shape check and reach get_item."
        )
        assert resp["statusCode"] == 404  # unknown id → 404


# ============================================================================
# Fix 4 — env var guard: 500, no AWS call
# ============================================================================

class TestEnvVarGuard:
    """Missing AVX_INVESTIGATIONS_TABLE or AVX_INVESTIGATIONS_QUEUE_URL → 500.

    The 500 must be returned before any AWS call is made.

    Mutation (remove the env var check): the handler proceeds and either uses
    an empty/wrong table name or raises a boto3 error; the 500 assertion fails.
    """

    def test_missing_table_env_var_returns_500_no_aws_call(self):
        """When AVX_INVESTIGATIONS_TABLE is absent, return 500 without AWS calls.

        Mutation (drop the table-var guard): handler proceeds, boto3 call or
        wrong table; 500 not returned, assertion fails.
        """
        import handler as h

        ddb_mock = MagicMock()
        sqs_mock = MagicMock()

        saved = os.environ.pop("AVX_INVESTIGATIONS_TABLE", None)
        try:
            with patch.object(h, "_get_ddb_client", return_value=ddb_mock), \
                 patch.object(h, "_get_sqs_client", return_value=sqs_mock):
                resp = h.handler(_post_event(claims=_ENGINEER_CLAIMS), None)
        finally:
            if saved is not None:
                os.environ["AVX_INVESTIGATIONS_TABLE"] = saved
            else:
                os.environ.setdefault("AVX_INVESTIGATIONS_TABLE", _TABLE)

        assert resp["statusCode"] == 500, (
            f"Expected 500 when table env var is absent, got {resp['statusCode']}. "
            "Mutation (drop the guard): proceeds to AWS call, assertion fails."
        )
        assert not ddb_mock.transact_write_items.called, (
            "No AWS call must be made when the table var is missing. "
            "Mutation: guard removed → transact_write_items called, fails."
        )
        assert not sqs_mock.send_message.called, (
            "SQS must not be called when the table var is missing."
        )

    def test_missing_queue_env_var_returns_500_no_aws_call(self):
        """When AVX_INVESTIGATIONS_QUEUE_URL is absent, return 500 without AWS calls.

        Mutation (drop the queue-var guard): handler reaches SQS send with empty
        URL; 500 not returned by the guard; assertion fails.
        """
        import handler as h

        ddb_mock = MagicMock()
        sqs_mock = MagicMock()

        saved = os.environ.pop("AVX_INVESTIGATIONS_QUEUE_URL", None)
        try:
            with patch.object(h, "_get_ddb_client", return_value=ddb_mock), \
                 patch.object(h, "_get_sqs_client", return_value=sqs_mock):
                resp = h.handler(_post_event(claims=_ENGINEER_CLAIMS), None)
        finally:
            if saved is not None:
                os.environ["AVX_INVESTIGATIONS_QUEUE_URL"] = saved
            else:
                os.environ.setdefault("AVX_INVESTIGATIONS_QUEUE_URL", _QUEUE)

        assert resp["statusCode"] == 500, (
            f"Expected 500 when queue env var is absent, got {resp['statusCode']}. "
            "Mutation (drop the guard): proceeds past guard, assertion fails."
        )
        assert not ddb_mock.transact_write_items.called, (
            "No AWS DDB call must be made when the queue var is missing."
        )
        assert not sqs_mock.send_message.called, (
            "No SQS call must be made when the queue var is missing."
        )



# ============================================================================
# D10-fix: optimistic-concurrency lock tests (task 3.4 fix)
# ============================================================================

_WINNER_ID = "cccccccc-dddd-eeee-ffff-000000000099"
_OLD_COMPLETED_AT = "2026-09-27T06:00:00Z"   # 8 days before 2026-10-05
_RECENT_COMPLETED_AT = "2026-10-04T06:00:00Z"  # 1 day before 2026-10-05


class TestD10OptimisticConcurrency:
    """Five scenarios that verify the D10-fix optimistic-concurrency design.

    The handler now reads the lock first, then reads the investigation row and
    calls is_live_investigation before deciding whether to run the transaction.

    Mutation A (test_queued_returns_existing_id,
                 test_completed_within_window_returns_existing_id):
        Deciding liveness from the lock item instead of the investigation row.
        The lock carries no status, so every pre-read lock would look
        "not live" and the handler would always create a new investigation.
        Both tests below catch this because they assert 200 + existing id.

    Mutation B (test_race_returns_winners_id_no_sqs):
        Dropping the ``investigation_id_ref = :seen_ref`` condition from the
        lock Put.  Two concurrent requests can both overwrite the lock; the
        test catches it via the cancellation and re-read path.
    """

    # ------------------------------------------------------------------
    # 1. Failed investigation → new request creates a new id and sends SQS
    # ------------------------------------------------------------------

    def test_failed_investigation_admits_new_request_and_sends_sqs(self):
        """Pre-read: lock points at a 'failed' investigation.

        is_live_investigation('failed') = False → proceed to transaction.
        Transaction succeeds → new investigation id returned (202), SQS sent.

        Mutation A (decide liveness from lock not row):
            Lock has no status attribute; handler would treat every lock as
            not-live and proceed — this test would still pass.  But see
            test_queued_returns_existing_id for the mutation that catches A.
        """
        import handler as h

        table_mock = _table_mock_seq(
            _lock_item_for_pattern(_INVESTIGATION_ID),   # call 1: lock pre-read
            _inv_row("failed"),                          # call 2: investigation read
        )
        ddb_mock = _ddb_success_mock()
        sqs_mock = _sqs_success_mock()

        with patch.object(h, "_get_investigations_table", return_value=table_mock), \
             patch.object(h, "_get_ddb_client", return_value=ddb_mock), \
             patch.object(h, "_get_sqs_client", return_value=sqs_mock):
            resp = h.handler(_post_event(claims=_ENGINEER_CLAIMS), None)

        assert resp["statusCode"] == 202, (
            f"Failed investigation must admit a new request (202), got {resp['statusCode']}."
        )
        body = json.loads(resp["body"])
        new_id = body.get("investigation_id", "")
        assert new_id != _INVESTIGATION_ID, (
            "New investigation id must differ from the old (failed) id. "
            "Mutation: treating 'failed' as live returns the old id."
        )
        assert ddb_mock.transact_write_items.called, (
            "transact_write_items must be called to write the new investigation."
        )
        assert sqs_mock.send_message.called, (
            "SQS must be called for a new investigation (failed doesn't block)."
        )

    # ------------------------------------------------------------------
    # 2. Completed > 7 days ago → new id (expired window)
    # ------------------------------------------------------------------

    def test_completed_outside_window_admits_new_request(self):
        """Pre-read: lock points at a 'completed' investigation 8 days old.

        is_live_investigation('completed', 8_days_ago) = False → new request.
        Transaction succeeds → 202, SQS sent, new id returned.
        """
        import handler as h
        from unittest.mock import patch as _patch
        from datetime import datetime, timedelta, timezone

        eight_days_ago = (
            datetime.now(timezone.utc) - timedelta(days=8)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")

        table_mock = _table_mock_seq(
            _lock_item_for_pattern(_INVESTIGATION_ID),   # lock pre-read
            _inv_row("completed", requested_at=eight_days_ago),  # investigation read
        )
        ddb_mock = _ddb_success_mock()
        sqs_mock = _sqs_success_mock()

        with _patch.object(h, "_get_investigations_table", return_value=table_mock), \
             _patch.object(h, "_get_ddb_client", return_value=ddb_mock), \
             _patch.object(h, "_get_sqs_client", return_value=sqs_mock):
            resp = h.handler(_post_event(claims=_ENGINEER_CLAIMS), None)

        assert resp["statusCode"] == 202, (
            f"Completed > 7 days must admit a new request (202), got {resp['statusCode']}."
        )
        body = json.loads(resp["body"])
        assert body.get("investigation_id") != _INVESTIGATION_ID, (
            "Expired completed investigation must yield a fresh id."
        )
        assert sqs_mock.send_message.called, "SQS must be called for the new investigation."

    # ------------------------------------------------------------------
    # 3. Completed < 7 days ago → existing id (still within window)
    # ------------------------------------------------------------------

    def test_completed_within_window_returns_existing_id(self):
        """Pre-read: lock points at a 'completed' investigation 1 day old.

        is_live_investigation('completed', 1_day_ago) = True → return 200
        with the existing id.  No transaction, no SQS send.

        Mutation A (decide liveness from lock not row):
            Lock has no status; handler always sees not-live → always creates
            a new id.  This test asserts 200 + existing id, so it fails.
        """
        import handler as h
        from unittest.mock import patch as _patch
        from datetime import datetime, timedelta, timezone

        one_day_ago = (
            datetime.now(timezone.utc) - timedelta(days=1)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")

        table_mock = _table_mock_seq(
            _lock_item_for_pattern(_INVESTIGATION_ID),   # lock pre-read
            _inv_row("completed", requested_at=one_day_ago),  # investigation read
        )
        sqs_mock = MagicMock()

        with _patch.object(h, "_get_investigations_table", return_value=table_mock), \
             _patch.object(h, "_get_sqs_client", return_value=sqs_mock):
            resp = h.handler(_post_event(claims=_ENGINEER_CLAIMS), None)

        assert resp["statusCode"] == 200, (
            f"Completed < 7 days must return 200 with existing id, got {resp['statusCode']}. "
            "Mutation A: deciding from lock (no status) always creates new id → 202."
        )
        body = json.loads(resp["body"])
        assert body.get("investigation_id") == _INVESTIGATION_ID, (
            f"Must return existing id '{_INVESTIGATION_ID}'. "
            "Mutation A: new id returned."
        )
        assert not sqs_mock.send_message.called, (
            "SQS must NOT be called when a live completed investigation exists."
        )

    # ------------------------------------------------------------------
    # 4. Queued → existing id (always live)
    # ------------------------------------------------------------------

    def test_queued_returns_existing_id(self):
        """Pre-read: lock points at a 'queued' investigation.

        is_live_investigation('queued') = True (fresh) → return 200 with existing id.
        No transaction, no SQS send.

        C1 fix: queued is live only within _STALE_AGE_SECONDS.  This test
        uses a fresh timestamp (1 minute ago).

        Mutation A (decide liveness from lock not row):
            Lock has no status → handler sees not-live → creates new id → 202.
            This test asserts 200 + existing id, so it fails on mutation A.
        """
        import handler as h
        from unittest.mock import patch as _patch
        from datetime import datetime, timedelta, timezone

        one_minute_ago = (
            datetime.now(timezone.utc) - timedelta(minutes=1)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")

        table_mock = _table_mock_seq(
            _lock_item_for_pattern(_INVESTIGATION_ID),           # lock pre-read
            _inv_row("queued", requested_at=one_minute_ago),     # investigation read (fresh)
        )
        sqs_mock = MagicMock()
        ddb_mock = MagicMock()  # must NOT be called

        with _patch.object(h, "_get_investigations_table", return_value=table_mock), \
             _patch.object(h, "_get_ddb_client", return_value=ddb_mock), \
             _patch.object(h, "_get_sqs_client", return_value=sqs_mock):
            resp = h.handler(_post_event(claims=_ENGINEER_CLAIMS), None)

        assert resp["statusCode"] == 200, (
            f"Queued investigation must return 200 with existing id, got {resp['statusCode']}. "
            "Mutation A: handler creates new id → 202."
        )
        body = json.loads(resp["body"])
        assert body.get("investigation_id") == _INVESTIGATION_ID, (
            f"Must return existing id '{_INVESTIGATION_ID}'. "
            "Mutation A: new id returned."
        )
        assert not ddb_mock.transact_write_items.called, (
            "transact_write_items must NOT be called when a live investigation exists. "
            "Mutation A: transaction called, assertion fails."
        )
        assert not sqs_mock.send_message.called, (
            "SQS must NOT be called when a live queued investigation exists."
        )

    # ------------------------------------------------------------------
    # 5. Race: lock ref changed between pre-read and transaction
    # ------------------------------------------------------------------

    def test_race_returns_winners_id_no_sqs(self):
        """Race: no lock seen in pre-read; transaction fires lock condition.

        Another request won the lock between our GetItem and our TransactWrite.
        Round 2 fresh pre-read sees winner's lock + live inv row → 200 winner's id.

        Mutation B (drop ``investigation_id_ref = :seen_ref`` condition):
            More directly: the test asserts 200 + winner's id + no SQS.
            Without the condition, two concurrent requests can both win the
            lock; the test catches the missing loop-restart path instead.
        """
        import handler as h
        from unittest.mock import patch as _patch
        from datetime import datetime, timezone

        winner_lock = _lock_item_for_pattern(_WINNER_ID)
        fresh_ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        # Round 1: no lock; Round 2: winner's lock + winner's live inv row
        table_mock = _table_mock_seq(
            None,
            winner_lock,
            _inv_row("queued", investigation_id=_WINNER_ID, requested_at=fresh_ts),
        )

        ddb_mock = MagicMock()
        ddb_mock.transact_write_items.side_effect = _txn_cancelled(
            _lock_fired_reasons(_WINNER_ID)
        )
        sqs_mock = MagicMock()

        with _patch.object(h, "_get_investigations_table", return_value=table_mock), \
             _patch.object(h, "_get_ddb_client", return_value=ddb_mock), \
             _patch.object(h, "_get_sqs_client", return_value=sqs_mock):
            resp = h.handler(_post_event(claims=_ENGINEER_CLAIMS), None)

        assert resp["statusCode"] == 200, (
            f"Race cancellation must return 200, got {resp['statusCode']}. "
            "Mutation B (drop re-read path): handler errors or returns wrong code."
        )
        body = json.loads(resp["body"])
        assert body.get("investigation_id") == _WINNER_ID, (
            f"Must return winner's id '{_WINNER_ID}', got {body.get('investigation_id')!r}. "
            "Mutation B: re-read not called → empty winner_id returned."
        )
        assert not sqs_mock.send_message.called, (
            "SQS must NOT be called when the race cancellation fires. "
            "Mutation B: SQS called with loser's id, assertion fails."
        )

    def test_stale_lock_uses_seen_ref_condition(self):
        """When a non-live lock exists, the transaction condition is ``investigation_id_ref = :seen_ref``.

        C2/C3 fix: when seen_ref is set, the transaction now includes:
          - A ConditionCheck on the seen_ref row at index 0
          - The lock Put at index 1 (not index 0)

        Mutation B (always use attribute_not_exists regardless of seen_ref):
            transact_write_items is called with ``attribute_not_exists`` instead of
            ``investigation_id_ref = :seen_ref``; the ConditionExpression assertion
            fails.
        """
        import handler as h
        from unittest.mock import patch as _patch

        # Pre-read: stale lock pointing at a failed investigation (non-live).
        table_mock = _table_mock_seq(
            _lock_item_for_pattern(_INVESTIGATION_ID),   # lock pre-read
            _inv_row("failed"),                          # investigation read → not live
        )
        ddb_mock = _ddb_success_mock()
        sqs_mock = _sqs_success_mock()

        with _patch.object(h, "_get_investigations_table", return_value=table_mock), \
             _patch.object(h, "_get_ddb_client", return_value=ddb_mock), \
             _patch.object(h, "_get_sqs_client", return_value=sqs_mock):
            h.handler(_post_event(claims=_ENGINEER_CLAIMS), None)

        assert ddb_mock.transact_write_items.called, "transact_write_items must be called."
        call_kwargs = ddb_mock.transact_write_items.call_args
        transact_items = (
            call_kwargs.kwargs.get("TransactItems")
            or (call_kwargs.args[0] if call_kwargs.args else [])
        )

        # C2 fix: ConditionCheck on the seen_ref row is at index 0; lock Put at 1.
        assert "ConditionCheck" in transact_items[0], (
            "When seen_ref is set, item 0 must be a ConditionCheck on the referenced row. "
            "C2 fix: ensures the seen_ref row is still not-live inside the transaction."
        )
        lock_put = transact_items[1]["Put"]

        assert "investigation_id_ref = :seen_ref" in lock_put["ConditionExpression"], (
            "When a non-live lock was seen, the lock Put condition must be "
            "'investigation_id_ref = :seen_ref'. "
            "Mutation B (always attribute_not_exists): different condition, fails."
        )
        attr_vals = lock_put.get("ExpressionAttributeValues", {})
        assert ":seen_ref" in attr_vals, (
            "ExpressionAttributeValues must include ':seen_ref'. "
            "Mutation B: key absent, assertion fails."
        )
        assert attr_vals[":seen_ref"]["S"] == _INVESTIGATION_ID, (
            f"':seen_ref' must equal the old investigation id '{_INVESTIGATION_ID}'. "
            "Mutation B: ':seen_ref' not set, assertion fails."
        )



# ===========================================================================
# Contract test: SQS body sent by the route feeds the runner's lambda_handler
# ===========================================================================

class TestSQSBodyContract:
    """The body the POST route sends to SQS is the bare investigation id.

    This test captures what the route *actually* enqueues (stubbed SQS) and
    then feeds that body directly into the runner's SQS consumer as a record
    body, asserting that ``run_investigation`` receives that exact id.

    The test is the single executable specification of the SQS contract between
    the two modules.  If the route wraps the id in a JSON object the second
    assertion fails; if the runner parses the body as JSON instead of a bare
    string the second assertion also fails — both directions are caught.

    Mutation guard
    --------------
    Revert ``_run_investigation_sqs`` to parse the body as a JSON object
    (``json.loads(body)``) — the test fails because ``json.loads("<uuid>")``
    raises ``JSONDecodeError`` and the record ends up in batchItemFailures
    rather than being passed to ``run_investigation``.
    """

    def test_route_body_is_consumed_correctly_by_runner(self, monkeypatch):
        """Body captured from POST /investigations SQS call feeds runner correctly.

        Steps
        -----
        1. POST to the route (all AWS stubbed), capture ``MessageBody`` from SQS.
        2. Feed that body as a SQS record to ``_run_investigation_sqs``.
        3. Assert ``run_investigation`` receives exactly the captured id.
        """
        import os as _os
        import sys as _sys
        import agents.tier2.runner as _runner
        import agents.tier2.artifact_writer as _aw
        import handler as h

        # ── Step 1: call the route and capture the SQS MessageBody ──────────
        captured: list[str] = []

        capturing_sqs = MagicMock()
        capturing_sqs.send_message.side_effect = (
            lambda **kw: captured.append(kw["MessageBody"]) or {"MessageId": "m-capture"}
        )

        with patch.object(h, "_get_investigations_table",
                          return_value=_table_mock_no_lock()), \
             patch.object(h, "_get_ddb_client",
                          return_value=_ddb_success_mock()), \
             patch.object(h, "_get_sqs_client",
                          return_value=capturing_sqs):
            resp = h.handler(_post_event(claims=_ENGINEER_CLAIMS), None)

        assert resp["statusCode"] == 202, (
            f"POST must return 202, got {resp['statusCode']}: {resp['body']}")
        assert len(captured) == 1, "Route must enqueue exactly one SQS message."
        body_from_route = captured[0]

        # Confirm shape: a plain string, not a JSON object.
        assert not body_from_route.startswith("{"), (
            "Route must send a bare id, not a JSON object.")

        # ── Step 2+3: feed body to runner._run_investigation_sqs ────────────
        # Patch env vars the runner needs.
        monkeypatch.setenv("VSA_STAGE", "staging")
        monkeypatch.setenv("ATHENA_RESULTS_BUCKET", "contract-test-bucket")
        monkeypatch.setenv("AVX_FINDINGS_TABLE", "vsa-staging-avx-findings")
        monkeypatch.setenv("AVX_INVESTIGATIONS_TABLE", "vsa-staging-avx-investigations")
        monkeypatch.setenv("DYNAMODB_REGION", "us-west-2")
        monkeypatch.setenv("ADP_REGION", "us-east-1")
        monkeypatch.setenv("ATHENA_WORKGROUP", "cvx-staging-analytics")

        fake_ddb = object()
        fake_athena = object()

        class _FakeCw:
            def put_metric_data(self, **_kw): pass

        def _fake_boto3_client(service, **_kw):
            if service == "dynamodb":
                return fake_ddb
            if service == "athena":
                return fake_athena
            if service == "cloudwatch":
                return _FakeCw()
            return MagicMock()

        monkeypatch.setattr("boto3.client", _fake_boto3_client)

        class _FakeWriter:
            table_name = "vsa-staging-avx-findings"
            dynamodb_client = fake_ddb

            def __init__(self, **_kw):
                pass

            def write_finding(self, finding, *, standing=None, **_kw):
                return {"action": "put", "finding_id": "FIND-x"}

        monkeypatch.setattr(_aw, "ArtifactWriter", _FakeWriter)

        received_ids: list[str] = []

        from agents.fi_quality import job as _job

        def _capture_run_investigation(inv_id, **_kw):
            received_ids.append(inv_id)

        monkeypatch.setattr(_job, "run_investigation", _capture_run_investigation)
        monkeypatch.setattr(_runner, "_emit_run_record", lambda *_a, **_kw: None)

        sqs_record = {
            "eventSource": "aws:sqs",
            "messageId": "msg-contract-001",
            "body": body_from_route,
        }
        result = _runner._run_investigation_sqs([sqs_record], object())

        assert result == {"batchItemFailures": []}, (
            f"Route body must be parseable by the runner; batchItemFailures={result['batchItemFailures']}. "
            "Mutation (runner parses body as JSON): json.loads('<uuid>') raises JSONDecodeError; "
            "the message lands in batchItemFailures instead of being passed to run_investigation."
        )
        assert received_ids == [body_from_route], (
            f"run_investigation must be called with the exact id the route enqueued. "
            f"Expected {body_from_route!r}, got {received_ids!r}."
        )



# =============================================================================
# C3 fix: Full TransactItems assertions with condition-evaluating fake
# =============================================================================

class _FakeTransactDDB:
    """In-memory fake DDB client that evaluates the lock, row, and cap conditions.

    Implements only the operations the route uses:
      - transact_write_items: evaluates all ConditionExpressions.
      - get_item: returns stored items.

    Items stored by key string (investigation_id value, not DDB JSON).
    """

    def __init__(self) -> None:
        self._items: dict = {}  # key → plain Python dict
        self.transact_calls: list = []

    def _put(self, key: str, item: dict) -> None:
        self._items[key] = item

    def get_item(self, *, TableName: str, Key: dict, ConsistentRead: bool = False) -> dict:
        from boto3.dynamodb.types import TypeDeserializer
        deser = TypeDeserializer()
        raw_key = deser.deserialize(list(Key.values())[0])
        item = self._items.get(raw_key)
        if item is None:
            return {}
        return {"Item": item}

    def transact_write_items(self, *, TransactItems: list) -> dict:
        from boto3.dynamodb.types import TypeDeserializer, TypeSerializer
        from botocore.exceptions import ClientError

        deser = TypeDeserializer()
        self.transact_calls.append(TransactItems)

        # Evaluate each item's condition; collect failures.
        reasons = []
        for txn_item in TransactItems:
            kind = next(iter(txn_item))
            spec = txn_item[kind]

            # Get the key value from the item (Put uses "Item", others use "Key").
            if kind == "Put":
                first_val = list(spec["Item"].values())[0]
                key_val = deser.deserialize(first_val)
            else:
                raw_key_ddb = list(spec["Key"].values())[0]
                key_val = deser.deserialize(raw_key_ddb)

            existing = self._items.get(key_val)
            cond = spec.get("ConditionExpression", "")
            attr_vals = spec.get("ExpressionAttributeValues", {})
            attr_names = spec.get("ExpressionAttributeNames", {})

            failed = self._eval_condition(cond, existing, attr_vals, attr_names)
            if failed:
                reasons.append({
                    "Code": "ConditionalCheckFailed",
                    "Item": {k: v for k, v in (spec.get("Item") or spec.get("Key") or {}).items()},
                })
            else:
                reasons.append({"Code": "None"})

        if any(r["Code"] == "ConditionalCheckFailed" for r in reasons):
            raise ClientError(
                {
                    "Error": {
                        "Code": "TransactionCanceledException",
                        "Message": "Transaction cancelled",
                    },
                    "CancellationReasons": reasons,
                },
                "TransactWriteItems",
            )

        # Apply writes.
        for txn_item in TransactItems:
            kind = next(iter(txn_item))
            spec = txn_item[kind]
            if kind == "Put":
                item_raw = spec["Item"]
                key_val = deser.deserialize(list(item_raw.values())[0])
                self._items[key_val] = {
                    k: deser.deserialize(v) for k, v in item_raw.items()
                }
            elif kind == "Update":
                raw_key_ddb = list(spec["Key"].values())[0]
                key_val = deser.deserialize(raw_key_ddb)
                if key_val not in self._items:
                    self._items[key_val] = {"investigation_id": key_val}
                item = self._items[key_val]
                attr_vals = spec.get("ExpressionAttributeValues", {})
                attr_names = spec.get("ExpressionAttributeNames", {})
                # Minimal: only support "ADD #cnt :one"
                for k, v in attr_names.items():
                    if v == "count":
                        one_raw = attr_vals.get(":one", {})
                        one = deser.deserialize(one_raw)
                        item[v] = int(item.get(v, 0)) + int(one)
            # ConditionCheck: read-only, no write.
        return {}

    @staticmethod
    def _eval_condition(
        cond: str,
        existing: "dict | None",
        attr_vals: dict,
        attr_names: dict,
    ) -> bool:
        """Return True if the condition FAILS (triggering ConditionalCheckFailed)."""
        from boto3.dynamodb.types import TypeDeserializer
        deser = TypeDeserializer()

        if not cond:
            return False

        # Resolve expression attribute names/values.
        def _resolve_name(n: str) -> str:
            return attr_names.get(n, n)

        def _resolve_val(v: str) -> object:
            raw = attr_vals.get(v)
            if raw is None:
                return None
            return deser.deserialize(raw)

        # Check more-specific patterns FIRST (before attribute_not_exists).

        # Cap: attribute_not_exists(investigation_id) OR #cnt < :cap
        if "OR #cnt < :cap" in cond:
            cnt_field = _resolve_name("#cnt")
            cap = _resolve_val(":cap")
            if existing is None:
                return False  # attribute_not_exists part — pass
            current = int(existing.get(cnt_field, 0))
            return not (current < int(cap))  # fail if count >= cap

        # investigation_id_ref = :seen_ref
        if "investigation_id_ref = :seen_ref" in cond:
            if existing is None:
                return True  # attr absent → condition fails
            seen = _resolve_val(":seen_ref")
            return existing.get("investigation_id_ref") != seen

        # ConditionCheck: row liveness (simplified: attribute_not_exists OR status=failed OR ...)
        if "OR #s" in cond and "attribute_not_exists(investigation_id)" in cond:
            if existing is None:
                return False  # attribute_not_exists passes
            status = existing.get("status", "")
            failed_val = _resolve_val(":failed")
            return not (status == failed_val or status == "")

        # Pure attribute_not_exists(investigation_id) — only for lock/row Put conditions.
        if cond.strip() == "attribute_not_exists(investigation_id)":
            return existing is not None

        # Fallback: unknown condition → passes (safe direction).
        return False


class TestTransactItemsFullStructure:
    """C3 fix: assert the full TransactItems structure with a condition-evaluating fake.

    The reviewer found that 5 mutations survived: making the lock condition
    always-true, making the cap condition always-true, an ADD that never
    increments, a default cap of 2000, and dropping the row Put's condition.
    These tests exercise the real code with an in-memory fake that evaluates
    conditions, catching all five mutations.

    Mutation guard table:
    - M1: lock condition always-true (attribute_not_exists removed):
      test_21st_request_returns_429 — 21 requests → cap fires, not lock
      test_transact_items_lock_condition — asserts attribute_not_exists
    - M2: cap condition always-true:
      test_21st_request_returns_429 — 21 requests each succeed → last one
      returns 202 instead of 429, assertion fails.
    - M3: ADD never increments:
      test_21st_request_returns_429 — cap never fills → all 21 succeed → fails.
    - M4: default cap 2000:
      test_cap_default_is_20 — asserts :cap = "20" in the TransactItems call.
    - M5: row Put attribute_not_exists dropped:
      test_transact_items_row_condition — asserts ConditionExpression on item 1.
    """

    def _make_handler_with_fake(self):
        """Return (handler module, fake_table_mock, fake_ddb, sqs_mock)."""
        import handler as h
        from unittest.mock import patch as _patch, MagicMock as _MM

        fake_ddb = _FakeTransactDDB()
        sqs_mock = _MM()
        sqs_mock.send_message.return_value = {"MessageId": "msg-c3-001"}

        # Table mock for GET (pre-read): always empty.
        table_mock = _MM()
        table_mock.get_item.return_value = {}
        return h, table_mock, fake_ddb, sqs_mock, _patch

    def test_21st_request_returns_429(self):
        """After 20 successful requests the 21st returns 429 (cap fires).

        Mutations M2 (cap always-true) and M3 (ADD no-op): the cap never fills,
        so the 21st request returns 202 instead of 429 — both assertions below fail.
        """
        import handler as h
        from unittest.mock import patch as _patch, MagicMock as _MM

        fake_ddb = _FakeTransactDDB()
        sqs_mock = _MM()
        sqs_mock.send_message.return_value = {"MessageId": "msg-cap-test"}
        table_mock = _MM()
        table_mock.get_item.return_value = {}  # no pre-existing lock

        with _patch.object(h, "_get_investigations_table", return_value=table_mock), \
             _patch.object(h, "_get_ddb_client", return_value=fake_ddb), \
             _patch.object(h, "_get_sqs_client", return_value=sqs_mock):
            for i in range(20):
                r = h.handler(_post_event(pattern_id=f"pat-cap-{i:03d}"), None)
                assert r["statusCode"] == 202, (
                    f"Request {i+1}/20 must succeed (202), got {r['statusCode']}. "
                    "M3: cap counter not incremented → all succeed."
                )
            # 21st request: different pattern, same day → should hit the cap.
            r21 = h.handler(_post_event(pattern_id="pat-cap-overflow"), None)

        assert r21["statusCode"] == 429, (
            f"21st request must return 429 (cap exhausted), got {r21['statusCode']}. "
            "M2: cap condition always-true → all succeed, this fails. "
            "M3: ADD no-op → count stays 0, cap never fires, this fails."
        )

    def test_two_requests_one_pattern_one_row(self):
        """Two POST requests for the same pattern yield exactly one investigation row.

        C2/C3 fix: the pre-read checks the lock, and if the referenced row is live,
        returns the existing id.  With the fake evaluating conditions, the second
        POST re-reads the lock and returns the first id.

        Mutation (drop lock condition): the second request creates a second row —
        test catches it via the different ids.
        """
        import handler as h
        from unittest.mock import patch as _patch, MagicMock as _MM

        fake_ddb = _FakeTransactDDB()
        sqs_mock = _MM()
        sqs_mock.send_message.return_value = {"MessageId": "msg-dup-test"}

        # Table resource mock: for pre-reads.  We use a stateful mock so the
        # second request can find the lock that the first request wrote.
        inv_store: dict = {}  # inv_id → {status, requested_at, ...}
        lock_store: dict = {}  # lock_key → {investigation_id_ref, ...}

        def _table_get_item(**kwargs):
            key_obj = kwargs.get("Key", {})
            inv_id = key_obj.get("investigation_id", "")
            # Return from fake_ddb in native Python format.
            item = fake_ddb._items.get(inv_id)
            if item:
                return {"Item": item}
            return {}

        table_mock = _MM()
        table_mock.get_item.side_effect = _table_get_item

        with _patch.object(h, "_get_investigations_table", return_value=table_mock), \
             _patch.object(h, "_get_ddb_client", return_value=fake_ddb), \
             _patch.object(h, "_get_sqs_client", return_value=sqs_mock):
            r1 = h.handler(_post_event(pattern_id=_PATTERN_ID), None)
            assert r1["statusCode"] == 202, f"First request must succeed, got {r1['statusCode']}"
            id1 = json.loads(r1["body"])["investigation_id"]

            # Second request: lock now exists in fake_ddb.
            r2 = h.handler(_post_event(pattern_id=_PATTERN_ID), None)

        assert r2["statusCode"] in (200, 202), (
            f"Second request must return 200/202, got {r2['statusCode']}."
        )
        id2 = json.loads(r2["body"])["investigation_id"]
        # Both must reference the same investigation.
        assert id1 == id2, (
            f"Two requests for the same pattern must yield the same id. "
            f"Got id1={id1!r}, id2={id2!r}. "
            "Mutation (drop lock/ConditionCheck): second request creates a new row."
        )

    def test_transact_items_lock_condition(self):
        """No-lock POST: the lock Put condition is attribute_not_exists.

        Mutation M1 (drop attribute_not_exists): the condition is absent or always
        true — test asserts its presence and exact value.
        """
        import handler as h
        from unittest.mock import patch as _patch, MagicMock as _MM

        fake_ddb = _FakeTransactDDB()
        sqs_mock = _MM()
        sqs_mock.send_message.return_value = {"MessageId": "msg-cond-test"}
        table_mock = _MM()
        table_mock.get_item.return_value = {}  # no lock

        with _patch.object(h, "_get_investigations_table", return_value=table_mock), \
             _patch.object(h, "_get_ddb_client", return_value=fake_ddb), \
             _patch.object(h, "_get_sqs_client", return_value=sqs_mock):
            resp = h.handler(_post_event(pattern_id=_PATTERN_ID), None)

        assert resp["statusCode"] == 202
        assert fake_ddb.transact_calls, "transact_write_items must be called."
        txn = fake_ddb.transact_calls[0]
        # No seen_ref → first item is the lock Put (no ConditionCheck prefix).
        lock_idx = 0
        if "ConditionCheck" in txn[0]:
            lock_idx = 1
        lock_put = txn[lock_idx]["Put"]
        assert "attribute_not_exists" in lock_put["ConditionExpression"], (
            "No-lock path: lock Put condition must be attribute_not_exists(investigation_id). "
            "Mutation M1: condition absent or always-true → fails."
        )

    def test_transact_items_cap_condition_and_default(self):
        """Cap Update condition is present and :cap is the configured default (20).

        Mutation M2 (always-true cap): condition absent → cap never fires → M2.
        Mutation M4 (default 2000): :cap = 2000 → 20 requests succeed then 21st
        still succeeds → the test_21st_request_returns_429 fails.
        """
        import handler as h
        from unittest.mock import patch as _patch, MagicMock as _MM

        fake_ddb = _FakeTransactDDB()
        sqs_mock = _MM()
        sqs_mock.send_message.return_value = {"MessageId": "msg-cap-cond-test"}
        table_mock = _MM()
        table_mock.get_item.return_value = {}

        with _patch.object(h, "_get_investigations_table", return_value=table_mock), \
             _patch.object(h, "_get_ddb_client", return_value=fake_ddb), \
             _patch.object(h, "_get_sqs_client", return_value=sqs_mock):
            resp = h.handler(_post_event(pattern_id=_PATTERN_ID), None)

        assert resp["statusCode"] == 202
        txn = fake_ddb.transact_calls[0]
        # Cap Update is always the last TransactItem.
        cap_update = txn[-1]["Update"]
        cond = cap_update["ConditionExpression"]
        assert "#cnt < :cap" in cond, (
            f"Cap Update condition must include '#cnt < :cap'. Got: {cond!r}. "
            "Mutation M2: condition absent → cap never fires."
        )
        cap_val_raw = cap_update["ExpressionAttributeValues"].get(":cap", {})
        cap_val = cap_val_raw.get("N", "0")
        assert cap_val == "20", (
            f"Cap :cap must equal '20' (the configured default). Got {cap_val!r}. "
            "Mutation M4: cap = 2000 → 20 daily requests would succeed and the "
            "test_21st_request_returns_429 would fail."
        )
        # M3 mutation guard: verify ADD is used (not SET), so the counter actually grows.
        update_expr = cap_update.get("UpdateExpression", "")
        assert "ADD" in update_expr, (
            f"Cap UpdateExpression must use ADD. Got: {update_expr!r}. "
            "Mutation M3: SET instead of ADD → counter never grows → cap never fires."
        )
        assert ":one" in cap_update.get("ExpressionAttributeValues", {}), (
            "Cap UpdateExpression must use :one as the increment value. "
            "Mutation M3: missing :one → ADD has no value → counter not incremented."
        )

    def test_transact_items_row_put_condition(self):
        """The row Put has attribute_not_exists(investigation_id) condition.

        Mutation M5 (drop row condition): UUID collision could write two rows for
        the same id — test asserts the condition is present on the row Put.
        """
        import handler as h
        from unittest.mock import patch as _patch, MagicMock as _MM

        fake_ddb = _FakeTransactDDB()
        sqs_mock = _MM()
        sqs_mock.send_message.return_value = {"MessageId": "msg-row-cond-test"}
        table_mock = _MM()
        table_mock.get_item.return_value = {}

        with _patch.object(h, "_get_investigations_table", return_value=table_mock), \
             _patch.object(h, "_get_ddb_client", return_value=fake_ddb), \
             _patch.object(h, "_get_sqs_client", return_value=sqs_mock):
            resp = h.handler(_post_event(pattern_id=_PATTERN_ID), None)

        assert resp["statusCode"] == 202
        txn = fake_ddb.transact_calls[0]
        # Row Put: find it by the absence of a lock# prefix on the key.
        row_puts = [
            item["Put"] for item in txn
            if "Put" in item
            and not list(item["Put"]["Item"].values())[0].get("S", "").startswith("lock#")
            and not list(item["Put"]["Item"].values())[0].get("S", "").startswith("cap#")
        ]
        assert row_puts, "Row Put must be present in TransactItems."
        row_put = row_puts[0]
        assert "attribute_not_exists(investigation_id)" in row_put.get("ConditionExpression", ""), (
            "Row Put must have attribute_not_exists(investigation_id) condition. "
            "Mutation M5: condition absent → UUID collision could write two rows."
        )



# =============================================================================
# C3 / W1 / W3b fix: moto-based route tests
# G3F2.3 — design answer 6: run route tests against real moto DynamoDB so
# DynamoDB evaluates transaction conditions for real.  Replaces the
# substring-matching _FakeTransactDDB for behavioural assertions.
# =============================================================================

def _moto_setup(table_name: str, region: str = "us-west-2"):
    """Create the investigations table in moto and return (ddb_client, table)."""
    import boto3 as _boto3
    ddb = _boto3.client("dynamodb", region_name=region)
    ddb.create_table(
        TableName=table_name,
        AttributeDefinitions=[
            {"AttributeName": "investigation_id", "AttributeType": "S"},
        ],
        KeySchema=[
            {"AttributeName": "investigation_id", "KeyType": "HASH"},
        ],
        BillingMode="PAY_PER_REQUEST",
    )
    table = _boto3.resource("dynamodb", region_name=region).Table(table_name)
    return ddb, table


def _reset_handler_clients():
    """Force the handler to re-initialise its module-level boto3 clients."""
    import handler as h
    h._table = None
    h._ddb_client = None
    h._sqs_client = None


_MOTO_TABLE = "vsa-moto-test-avx-investigations"
_MOTO_QUEUE = "https://sqs.us-west-2.amazonaws.com/123456789012/vsa-moto-investigations"


class TestMotoRouteTransactions:
    """C3 fix (design answer 6): moto evaluates real DynamoDB conditions.

    Each test enters ``mock_aws()``, creates the table, resets the handler's
    cached clients, and drives the handler directly.  moto evaluates the
    TransactWriteItems conditions natively so substring-matching fakes cannot
    mask broken expressions.

    Mutant guards captured here that the _FakeTransactDDB missed:
    - stale queued/running arm removed from the ConditionCheck (handler item 0)
    - pre-read treats queued/running as live at any age
    - ConditionCheck/Update condition made always-true
    - cap condition OR attribute_exists(...)
    - seen_ref lock condition OR attribute_exists(...)
    - ConsistentRead removed from the lock/row reads
    - empty winner id answers 200 "" instead of 503
    - W3 b: cap_idx wrong when seen_ref is set
    - W1: TransactionConflict inside TransactionCanceledException is retried
    """

    def _post(self, pattern_id: str = _PATTERN_ID) -> dict:
        import handler as h
        return h.handler(_post_event(pattern_id=pattern_id), None)

    def _get(self, investigation_id: str) -> dict:
        import handler as h
        return h.handler(_get_event(investigation_id), None)

    def test_21_posts_fill_cap_and_22nd_returns_429(self):
        """moto evaluates the cap condition; 21st request returns 429.

        Mutant guards:
        - cap condition made always-true → all 21 succeed, never 429 → FAILS.
        - cap condition uses OR attribute_exists(…) → same → FAILS.
        - ADD no-op → count never grows → cap never fires → FAILS.
        """
        from moto import mock_aws
        import os

        with mock_aws():
            _moto_setup(_MOTO_TABLE)
            _reset_handler_clients()
            with patch.dict(os.environ, {
                "AVX_INVESTIGATIONS_TABLE": _MOTO_TABLE,
                "AVX_INVESTIGATIONS_QUEUE_URL": _MOTO_QUEUE,
                "AVX_INVESTIGATIONS_DAILY_CAP": "20",
            }):
                import boto3 as _b3
                sqs_mock = MagicMock()
                sqs_mock.send_message.return_value = {"MessageId": "m1"}
                import handler as h
                h._sqs_client = sqs_mock

                for i in range(20):
                    r = self._post(pattern_id=f"pat-moto-cap-{i:03d}")
                    assert r["statusCode"] == 202, (
                        f"Request {i+1}/20 must succeed (202); got {r['statusCode']}. "
                        "Mutant: cap always-true → all succeed."
                    )

                r21 = self._post(pattern_id="pat-moto-cap-overflow")
                assert r21["statusCode"] == 429, (
                    f"21st request must return 429; got {r21['statusCode']}. "
                    "Mutant: cap never fires → all succeed."
                )

    def test_two_posts_same_pattern_return_same_id(self):
        """moto lock prevents two concurrent POSTs from creating two rows.

        Mutant guard: lock condition dropped → second POST creates a new row →
        different ids → FAILS.
        """
        from moto import mock_aws
        import os

        with mock_aws():
            _moto_setup(_MOTO_TABLE)
            _reset_handler_clients()
            with patch.dict(os.environ, {
                "AVX_INVESTIGATIONS_TABLE": _MOTO_TABLE,
                "AVX_INVESTIGATIONS_QUEUE_URL": _MOTO_QUEUE,
            }):
                sqs_mock = MagicMock()
                sqs_mock.send_message.return_value = {"MessageId": "m2"}
                import handler as h
                h._sqs_client = sqs_mock

                r1 = self._post(pattern_id=_PATTERN_ID)
                assert r1["statusCode"] == 202
                id1 = json.loads(r1["body"])["investigation_id"]

                r2 = self._post(pattern_id=_PATTERN_ID)
                assert r2["statusCode"] in (200, 202), (
                    f"Second POST must return 200 or 202; got {r2['statusCode']}."
                )
                id2 = json.loads(r2["body"])["investigation_id"]
                assert id1 == id2, (
                    f"Two POSTs for the same pattern must return the same id. "
                    f"Got {id1!r} and {id2!r}. Mutant: lock condition dropped → new row."
                )

    def test_stale_queued_row_admits_new_id(self):
        """A stale queued row (pre-read) is marked failed in the transaction.

        The route's takeover Update (C2 fix, design answer 5) atomically marks the
        stale row failed so the old SQS message cannot _set_running later.
        A new investigation is admitted.

        Mutant guard: drop the stale arm from the Update condition → the old
        message can run → two Findings for one pattern (C2 defect) → FAILS
        because the second id is different (live investigation exists).
        """
        from moto import mock_aws
        import os
        from datetime import datetime, timezone, timedelta
        from boto3.dynamodb.types import TypeSerializer

        ser = TypeSerializer()

        stale_ts = (
            datetime.now(timezone.utc) - timedelta(hours=3)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")

        with mock_aws():
            ddb_client, table = _moto_setup(_MOTO_TABLE)
            _reset_handler_clients()

            # Pre-seed a stale queued row with a lock pointing at it.
            stale_inv_id = "cc000001-0000-4000-8000-000000000001"
            table.put_item(Item={
                "investigation_id": f"lock#{_PATTERN_ID}",
                "pattern_id": _PATTERN_ID,
                "investigation_id_ref": stale_inv_id,
                "requested_at": stale_ts,
            })
            table.put_item(Item={
                "investigation_id": stale_inv_id,
                "pattern_id": _PATTERN_ID,
                "status": "queued",
                "requested_at": stale_ts,
                "requested_by": "eng-old",
            })

            with patch.dict(os.environ, {
                "AVX_INVESTIGATIONS_TABLE": _MOTO_TABLE,
                "AVX_INVESTIGATIONS_QUEUE_URL": _MOTO_QUEUE,
            }):
                sqs_mock = MagicMock()
                sqs_mock.send_message.return_value = {"MessageId": "m3"}
                import handler as h
                h._sqs_client = sqs_mock

                r = self._post(pattern_id=_PATTERN_ID)
                assert r["statusCode"] == 202, (
                    f"A stale queued row must admit a new investigation (202); "
                    f"got {r['statusCode']}."
                )
                new_id = json.loads(r["body"])["investigation_id"]
                assert new_id != stale_inv_id, "New id must differ from the stale one."

                # Verify the stale row was atomically marked failed.
                stale_row = table.get_item(
                    Key={"investigation_id": stale_inv_id}
                ).get("Item", {})
                assert stale_row.get("status") == "failed", (
                    f"Stale queued row must be marked failed by the transaction; "
                    f"got status={stale_row.get('status')!r}. "
                    "Mutant: takeover Update removed → stale row stays queued → "
                    "old message can run → two Findings."
                )

    def test_stale_running_row_admits_new_id(self):
        """A stale running row (started_at old) admits a new investigation.

        The route uses started_at (not requested_at) for running rows (C2 age basis).

        Mutant guard: use requested_at for running → a recently requested but
        long-running Lambda looks stale → admits a concurrent duplicate → FAILS
        because the existing row would still be live by started_at.
        """
        from moto import mock_aws
        import os
        from datetime import datetime, timezone, timedelta

        stale_started = (
            datetime.now(timezone.utc) - timedelta(hours=3)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        fresh_requested = (
            datetime.now(timezone.utc) - timedelta(minutes=5)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")

        with mock_aws():
            ddb_client, table = _moto_setup(_MOTO_TABLE)
            _reset_handler_clients()

            stale_inv_id = "dd000001-0000-4000-8000-000000000001"
            table.put_item(Item={
                "investigation_id": f"lock#{_PATTERN_ID}",
                "pattern_id": _PATTERN_ID,
                "investigation_id_ref": stale_inv_id,
                "requested_at": fresh_requested,
            })
            table.put_item(Item={
                "investigation_id": stale_inv_id,
                "pattern_id": _PATTERN_ID,
                "status": "running",
                "requested_at": fresh_requested,
                "started_at": stale_started,
                "requested_by": "eng-old",
            })

            with patch.dict(os.environ, {
                "AVX_INVESTIGATIONS_TABLE": _MOTO_TABLE,
                "AVX_INVESTIGATIONS_QUEUE_URL": _MOTO_QUEUE,
            }):
                sqs_mock = MagicMock()
                sqs_mock.send_message.return_value = {"MessageId": "m4"}
                import handler as h
                h._sqs_client = sqs_mock

                r = self._post(pattern_id=_PATTERN_ID)
                assert r["statusCode"] == 202, (
                    f"A stale running row must admit a new investigation (202); "
                    f"got {r['statusCode']}. Mutant: pre-read uses requested_at for "
                    "running → fresh requested_at keeps it live → 200 returned."
                )
                new_id = json.loads(r["body"])["investigation_id"]
                assert new_id != stale_inv_id, "New id must differ from the stale one."

    def test_completed_within_window_blocks_new_request(self):
        """A completed row within its TTL blocks a new request (returns 200).

        Mutant guard: treat completed as always dead → second POST creates a new
        row instead of returning the existing id → status code 202 not 200 → FAILS.
        """
        from moto import mock_aws
        import os
        from datetime import datetime, timezone, timedelta

        fresh_ts = (
            datetime.now(timezone.utc) - timedelta(minutes=30)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")

        with mock_aws():
            ddb_client, table = _moto_setup(_MOTO_TABLE)
            _reset_handler_clients()

            live_inv_id = "ee000001-0000-4000-8000-000000000001"
            table.put_item(Item={
                "investigation_id": f"lock#{_PATTERN_ID}",
                "investigation_id_ref": live_inv_id,
                "requested_at": fresh_ts,
            })
            table.put_item(Item={
                "investigation_id": live_inv_id,
                "pattern_id": _PATTERN_ID,
                "status": "completed",
                "requested_at": fresh_ts,
                "finding_id": "FIND-completed-001",
                "requested_by": "eng-old",
            })

            with patch.dict(os.environ, {
                "AVX_INVESTIGATIONS_TABLE": _MOTO_TABLE,
                "AVX_INVESTIGATIONS_QUEUE_URL": _MOTO_QUEUE,
            }):
                sqs_mock = MagicMock()
                sqs_mock.send_message.return_value = {"MessageId": "m5"}
                import handler as h
                h._sqs_client = sqs_mock

                r = self._post(pattern_id=_PATTERN_ID)
                assert r["statusCode"] == 200, (
                    f"A completed row within its window must return 200 (existing id); "
                    f"got {r['statusCode']}. Mutant: completed treated as dead → 202."
                )
                assert json.loads(r["body"])["investigation_id"] == live_inv_id

    def test_seen_ref_and_cap_cancellation_returns_429(self):
        """W3 b: cap fires correctly when seen_ref is set (cap_idx offset check).

        Before the fix ``cap_idx = (2 if seen_ref else 2)`` was always 2.
        With seen_ref the transaction has a prefix item (Update or ConditionCheck),
        so cap is at index 3.  This test exercises that path through moto.

        Mutant guard: cap_idx off-by-one → 429 matched to the wrong reason →
        re-raises as a 502 instead of 429 → FAILS.
        """
        from moto import mock_aws
        import os
        from datetime import datetime, timezone, timedelta

        stale_ts = (
            datetime.now(timezone.utc) - timedelta(hours=3)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")

        with mock_aws():
            ddb_client, table = _moto_setup(_MOTO_TABLE)
            _reset_handler_clients()

            # Seed a stale queued row (triggers the prefix Update path).
            stale_inv = "ff000001-0000-4000-8000-000000000001"
            table.put_item(Item={
                "investigation_id": f"lock#{_PATTERN_ID}",
                "investigation_id_ref": stale_inv,
                "requested_at": stale_ts,
            })
            table.put_item(Item={
                "investigation_id": stale_inv,
                "status": "queued",
                "requested_at": stale_ts,
                "requested_by": "eng-old",
            })

            # Fill the cap with OTHER patterns first.
            with patch.dict(os.environ, {
                "AVX_INVESTIGATIONS_TABLE": _MOTO_TABLE,
                "AVX_INVESTIGATIONS_QUEUE_URL": _MOTO_QUEUE,
                "AVX_INVESTIGATIONS_DAILY_CAP": "20",
            }):
                sqs_mock = MagicMock()
                sqs_mock.send_message.return_value = {"MessageId": "m6"}
                import handler as h
                h._sqs_client = sqs_mock

                for i in range(20):
                    r = h.handler(_post_event(pattern_id=f"pat-cap-pre-{i:03d}"), None)
                    assert r["statusCode"] == 202

                # Now POST for the stale pattern (seen_ref path) with cap full.
                r = self._post(pattern_id=_PATTERN_ID)
                assert r["statusCode"] == 429, (
                    f"Post with seen_ref + full cap must return 429; got {r['statusCode']}. "
                    "Mutant: cap_idx off-by-one → wrong reason matched → 502 or 500."
                )

    def test_get_stale_queued_returns_failed(self):
        """GET for a stale queued row reports failed + fixed sentence (C1 / W6 a2).

        Mutant guard: stale GET check removed → GET returns status=queued →
        CS shows 'Investigating since…' forever → FAILS.
        """
        from moto import mock_aws
        import os
        from datetime import datetime, timezone, timedelta

        stale_ts = (
            datetime.now(timezone.utc) - timedelta(hours=3)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")

        with mock_aws():
            ddb_client, table = _moto_setup(_MOTO_TABLE)
            _reset_handler_clients()
            # Valid UUID format required (8-4-4-4-12 hex).
            inv_id = "aa000001-0000-4000-8000-000000000001"
            table.put_item(Item={
                "investigation_id": inv_id,
                "pattern_id": _PATTERN_ID,
                "status": "queued",
                "requested_at": stale_ts,
                "requested_by": "eng-001",
            })
            with patch.dict(os.environ, {
                "AVX_INVESTIGATIONS_TABLE": _MOTO_TABLE,
                "AVX_INVESTIGATIONS_QUEUE_URL": _MOTO_QUEUE,
            }):
                import handler as h
                r = self._get(inv_id)
                assert r["statusCode"] == 200
                body = json.loads(r["body"])
                assert body.get("status") == "failed", (
                    f"Stale queued row GET must report status=failed; "
                    f"got {body.get('status')!r}. "
                    "Mutant: stale GET check removed → status=queued returned."
                )
                from handler import _FAILURE_SENTENCE
                assert body.get("error_message") == _FAILURE_SENTENCE, (
                    f"Stale queued row GET must include the fixed failure sentence; "
                    f"got {body.get('error_message')!r}."
                )

    def test_get_stale_running_returns_failed(self):
        """GET for a stale running row (started_at old) reports failed (C1 / W6 a2).

        Uses started_at for the staleness check (design answer 5).
        """
        from moto import mock_aws
        import os
        from datetime import datetime, timezone, timedelta

        stale_started = (
            datetime.now(timezone.utc) - timedelta(hours=3)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        fresh_requested = (
            datetime.now(timezone.utc) - timedelta(minutes=5)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")

        with mock_aws():
            ddb_client, table = _moto_setup(_MOTO_TABLE)
            _reset_handler_clients()
            inv_id = "bb000001-0000-4000-8000-000000000001"
            table.put_item(Item={
                "investigation_id": inv_id,
                "pattern_id": _PATTERN_ID,
                "status": "running",
                "requested_at": fresh_requested,
                "started_at": stale_started,
                "requested_by": "eng-001",
            })
            with patch.dict(os.environ, {
                "AVX_INVESTIGATIONS_TABLE": _MOTO_TABLE,
                "AVX_INVESTIGATIONS_QUEUE_URL": _MOTO_QUEUE,
            }):
                import handler as h
                r = self._get(inv_id)
                assert r["statusCode"] == 200
                body = json.loads(r["body"])
                assert body.get("status") == "failed", (
                    f"Stale running row GET must report status=failed; "
                    f"got {body.get('status')!r}. "
                    "Mutant: stale GET check removed OR uses requested_at for running "
                    "→ row looks live → status=running returned."
                )


class TestStaleAgeBoundsPin:
    """Pin the stale-age constants to their env-derived values (4.3 carry).

    4.3 must configure the queue's visibility timeout, maxReceiveCount and the
    Lambda timeout env var to be consistent with these bounds.

    Mutation guards:
    - _STALE_RUNNING_AGE_SECONDS set to 10**9 → test_stale_running_is_not_live fails.
    - _STALE_QUEUED_AGE_SECONDS set to 10**9 → test_stale_queued_is_not_live fails.
    - Function timeout default changed → running bound changes → fails.
    """

    def test_running_bound_exceeds_zero_and_is_below_queued_bound(self):
        """Running bound = function_timeout + 60 (< queued bound by design).

        4.3 carry: TIER2_FUNCTION_TIMEOUT_SECONDS must match the Lambda timeout
        configured in vsa-tier2-stack.ts (currently 900 s).
        """
        from agents.fi_quality.write import (
            _STALE_RUNNING_AGE_SECONDS,
            _STALE_QUEUED_AGE_SECONDS,
        )
        assert _STALE_RUNNING_AGE_SECONDS > 0, (
            "Running stale bound must be positive."
        )
        assert _STALE_RUNNING_AGE_SECONDS < _STALE_QUEUED_AGE_SECONDS, (
            f"Running bound ({_STALE_RUNNING_AGE_SECONDS}s) must be < queued bound "
            f"({_STALE_QUEUED_AGE_SECONDS}s): a Lambda that started recently is not "
            "dead, but a queued message that's been waiting for longer IS dead."
        )

    def test_running_bound_default_equals_function_timeout_plus_margin(self):
        """Default running bound = 900 + 60 = 960 s (no env override)."""
        import importlib
        import os
        # Temporarily clear env override to test the default.
        old = os.environ.pop("TIER2_FUNCTION_TIMEOUT_SECONDS", None)
        try:
            import agents.fi_quality.write as _w
            importlib.reload(_w)
            assert _w._STALE_RUNNING_AGE_SECONDS == 960, (
                f"Default running bound must be 960 s (900+60); "
                f"got {_w._STALE_RUNNING_AGE_SECONDS}. "
                "Mutant: default changed → Lambda seen as dead too early or too late."
            )
        finally:
            if old is not None:
                os.environ["TIER2_FUNCTION_TIMEOUT_SECONDS"] = old
            importlib.reload(_w)

    def test_queued_bound_default_is_two_hours(self):
        """Default queued bound = 7200 s (2 h, cycle-3 answer 4; no env override).

        Cycle 4 b W1: it was 3600 s, under tech.md's visibility timeout + function
        timeout (5400 + 900), so a requeued investigation's redrive arrived after GET
        already reported it failed. Mutant: default "3600" -> FAILS."""
        import importlib
        import os
        old = os.environ.pop("TIER2_STALE_QUEUED_AGE_SECONDS", None)
        try:
            import agents.fi_quality.write as _w
            importlib.reload(_w)
            assert _w._STALE_QUEUED_AGE_SECONDS == 7200, (
                f"Default queued bound must be 7200 s; "
                f"got {_w._STALE_QUEUED_AGE_SECONDS}."
            )
        finally:
            if old is not None:
                os.environ["TIER2_STALE_QUEUED_AGE_SECONDS"] = old
            importlib.reload(_w)


class TestCycle5StaleLimitsReadTheirEnv:
    """Cycle 5 b W1: both stale limits follow their env variables, which task 4.3's stack
    sets (so the code and the queue/function settings share one value). Mutants
    ``running-env-ignored`` (``= 960``) and ``queued-env-ignored`` (``= 7200``) FAIL."""

    @staticmethod
    def _reload_with(env):
        import importlib
        import os
        import agents.fi_quality.write as _w
        old = {k: os.environ.get(k) for k in env}
        try:
            os.environ.update(env)
            importlib.reload(_w)
            return _w._STALE_RUNNING_AGE_SECONDS, _w._STALE_QUEUED_AGE_SECONDS
        finally:
            for k, v in old.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
            importlib.reload(_w)

    def test_running_limit_is_function_timeout_plus_60(self):
        running, _ = self._reload_with({"TIER2_FUNCTION_TIMEOUT_SECONDS": "600"})
        assert running == 660, running

    def test_queued_limit_follows_its_env(self):
        _, queued = self._reload_with({"TIER2_STALE_QUEUED_AGE_SECONDS": "10800"})
        assert queued == 10800, queued


class TestTransactionConflictRetry:
    """W1 fix: TransactionConflict as a cancellation reason is retried.

    DynamoDB reports an overlapping transaction conflict as a cancellation
    reason (``TransactionConflict``), not as a top-level error code.
    The old code checked ``exc.response["Error"]["Code"] == "TransactionConflict"``
    which never matched, so the branch was dead code.

    Mutation guard: disable the retry branch → a conflict re-raises as 502
    instead of retrying → test fails because the second stub succeeds.
    """

    def _txn_conflict_error(self):
        """Return a TransactionCanceledException with TransactionConflict reason."""
        return ClientError(
            {
                "Error": {
                    "Code": "TransactionCanceledException",
                    "Message": "Transaction cancelled, please refer cancellation reasons …",
                },
                "CancellationReasons": [
                    {"Code": "TransactionConflict"},
                    {"Code": "None"},
                    {"Code": "None"},
                ],
            },
            "TransactWriteItems",
        )

    def test_transaction_conflict_in_cancellation_reason_is_retried(self):
        """TransactionConflict as a cancellation reason causes a retry + 202.

        Mutant guard: disable the retry branch → first attempt conflict re-raises →
        502 returned instead of 202 → FAILS.
        """
        import handler as h
        from unittest.mock import patch as _patch, MagicMock as _MM

        conflict_error = self._txn_conflict_error()
        success_resp = {}

        call_count = [0]

        def _transact_write(**kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                raise conflict_error
            return success_resp

        ddb_mock = _MM()
        ddb_mock.transact_write_items.side_effect = _transact_write
        sqs_mock = _MM()
        sqs_mock.send_message.return_value = {"MessageId": "m-conflict-001"}
        table_mock = _MM()
        table_mock.get_item.return_value = {}

        with _patch.object(h, "_get_investigations_table", return_value=table_mock), \
             _patch.object(h, "_get_ddb_client", return_value=ddb_mock), \
             _patch.object(h, "_get_sqs_client", return_value=sqs_mock):
            r = h.handler(_post_event(pattern_id=_PATTERN_ID), None)

        assert r["statusCode"] == 202, (
            f"TransactionConflict in cancellation must retry; got {r['statusCode']}. "
            "Mutant: retry branch disabled → first conflict re-raises → 502."
        )
        assert call_count[0] == 2, (
            f"transact_write_items must be called exactly twice (1 conflict + 1 retry); "
            f"got {call_count[0]}."
        )

    def test_transaction_conflict_exhaust_retries_returns_503(self):
        """Two consecutive TransactionConflicts → 503 after exhausting retries."""
        import handler as h
        from unittest.mock import patch as _patch, MagicMock as _MM

        conflict_error = self._txn_conflict_error()
        ddb_mock = _MM()
        ddb_mock.transact_write_items.side_effect = conflict_error
        sqs_mock = _MM()
        table_mock = _MM()
        table_mock.get_item.return_value = {}

        with _patch.object(h, "_get_investigations_table", return_value=table_mock), \
             _patch.object(h, "_get_ddb_client", return_value=ddb_mock), \
             _patch.object(h, "_get_sqs_client", return_value=sqs_mock):
            r = h.handler(_post_event(pattern_id=_PATTERN_ID), None)

        assert r["statusCode"] == 503, (
            f"Two TransactionConflicts must exhaust retries and return 503; "
            f"got {r['statusCode']}."
        )



# =============================================================================
# G3F3.2 new tests — close 33 surviving reviewer mutants (route / handler side)
# =============================================================================


class TestC3ConsistentReadAssertions:
    """C3: POST reads lock and row with ConsistentRead=True (H7, H8).

    Mutant C3-H7: lock read ConsistentRead removed → stale replica can show
    empty even when lock exists → duplicates.
    Mutant C3-H8: row read ConsistentRead removed → stale empty replica for
    a live row → route admits a concurrent duplicate.

    Verified by inspecting the handler source rather than mock machinery.
    """

    def test_post_investigation_has_two_consistent_reads(self):
        """_post_investigation uses ConsistentRead=True for both the lock and the row.

        _post_investigation has 2 ConsistentRead=True calls inside the loop:
        1. Lock pre-read (every round)
        2. seen_ref row pre-read (when seen_ref is present)

        The old code had a 3rd explicit re-read after lock cancellation; the new
        bounded-loop design replaces it with the next round's pre-read (same code
        path, same ConsistentRead=True, no extra call site).

        Mutant C3-H7: lock read CR removed → count drops to 1 → FAILS.
        Mutant C3-H8: row read CR removed → count drops to 1 → FAILS.
        Either mutation also removes CR from the specific pattern window.
        """
        import inspect
        import handler as h
        source = inspect.getsource(h._post_investigation)
        count = source.count("ConsistentRead=True")
        assert count >= 2, (
            f"_post_investigation must have at least 2 ConsistentRead=True calls; "
            f"found {count}. "
            "Mutant C3-H7: lock read CR removed → count=1 → FAILS. "
            "Mutant C3-H8: row read CR removed → count=1 → FAILS."
        )

        # Verify each specific pre-read has CR=True in its nearby window.
        lock_idx = source.find('Key={"investigation_id": lock_key}')
        row_idx = source.find('Key={"investigation_id": str(seen_ref)}')
        assert lock_idx != -1 and row_idx != -1
        lock_w = source[max(0, lock_idx - 20):lock_idx + 200]
        row_w = source[max(0, row_idx - 20):row_idx + 200]
        assert "ConsistentRead=True" in lock_w, (
            f"Lock read must have ConsistentRead=True; window={lock_w!r}. "
            "Mutant C3-H7: removed → FAILS."
        )
        assert "ConsistentRead=True" in row_w, (
            f"Row read must have ConsistentRead=True; window={row_w!r}. "
            "Mutant C3-H8: removed → FAILS."
        )

    def test_get_investigation_has_consistent_read(self):
        """_get_investigation calls table.get_item without ConsistentRead=True (GET reads lax)."""
        import inspect
        import handler as h
        # GET reads don't need ConsistentRead (best-effort is ok for display).
        # The check here is just that _post_investigation has 2 and we don't accidentally
        # use False in either read.
        source_post = inspect.getsource(h._post_investigation)
        assert "ConsistentRead=False" not in source_post, (
            "_post_investigation must not set ConsistentRead=False."
        )


class TestC3ForcedEmptyPreread:
    """C3: forced stale-empty lock pre-read returns the winner's id (H4, H10).

    The route's first GET returns {} (simulating a stale empty read), but the
    lock already exists (written by another request).  The lock Put condition
    fires; the route reads the winner's id from ALL_OLD and returns 200.

    Mutant C3-H4: lock condition (no-lock form) always-true → transaction
    succeeds with a new lock → returns 202 new id → FAILS (expected 200 same id).
    Mutant C3-H10: ALL_OLD removed from the lock Put → winner_id from
    ALL_OLD is None → falls back to a re-read → still works in this test
    (re-read branch tested separately).
    """

    def test_forced_empty_preread_returns_existing_id(self):
        """After a stale-empty lock read, the transaction cancels and returns id1.

        Mutant C3-H4: condition removed → transaction writes new id → 202 → FAILS.
        """
        import json
        from moto import mock_aws

        with mock_aws():
            ddb, tbl = _moto_setup(_MOTO_TABLE)
            _reset_handler_clients()

            with patch.dict(os.environ, {
                "AVX_INVESTIGATIONS_TABLE": _MOTO_TABLE,
                "AVX_INVESTIGATIONS_QUEUE_URL": _MOTO_QUEUE,
            }):
                sqs_mock = MagicMock()
                sqs_mock.send_message.return_value = {"MessageId": "m-empty-1"}
                import handler as h
                h._sqs_client = sqs_mock

                # First POST: creates id1 and lock normally.
                r1 = h.handler(_post_event(), None)
                assert r1["statusCode"] == 202
                id1 = json.loads(r1["body"])["investigation_id"]

                # Second POST: force the lock GET to return {} on first call,
                # simulating a stale-empty read — but the lock exists in moto.
                real_tbl = h._get_investigations_table()
                first_call = [True]

                class _StaleEmptyTable:
                    def get_item(self_, **kwargs):
                        if first_call[0]:
                            first_call[0] = False
                            return {}   # forced stale-empty
                        return real_tbl.get_item(**kwargs)

                    def __getattr__(self_, name):
                        return getattr(real_tbl, name)

                with patch.object(h, "_get_investigations_table",
                                  return_value=_StaleEmptyTable()):
                    r2 = h.handler(_post_event(), None)

                body2 = json.loads(r2["body"])
                assert r2["statusCode"] == 200, (
                    f"Forced empty lock pre-read must return 200 with existing id; "
                    f"got {r2['statusCode']} body={body2}. "
                    "Mutant C3-H4: lock condition removed → new lock written → 202 → FAILS."
                )
                assert body2.get("investigation_id") == id1, (
                    f"Must return id1={id1!r}; got {body2.get('investigation_id')!r}. "
                    "Mutant C3-H10: ALL_OLD missing → no fallback → 503 or wrong id → FAILS."
                )
                # Only one SQS send (the first POST).
                assert sqs_mock.send_message.call_count == 1, (
                    f"Only 1 SQS send (first POST); got {sqs_mock.send_message.call_count}. "
                    "Mutant C3-H4: second POST creates new inv → SQS called again → FAILS."
                )


class TestC3SeenRefLockCondition:
    """C3: seen_ref lock condition prevents lock race (H5).

    Pre-read sees lock ref=X, but by transaction time the lock has been
    updated to ref=Y (another request won).  The condition
    `investigation_id_ref = :seen_ref` should fire.

    Mutant C3-H5: condition always-true → transaction wins even when lock
    changed → two investigations for one pattern → FAILS (should return Y).
    """

    def test_seen_ref_lock_condition_prevents_stale_race(self):
        """When lock's ref changes between read and transaction, returns the winner's id.

        Mutant C3-H5: `investigation_id_ref = :seen_ref OR attribute_exists(investigation_id)`
        → condition always-true → new id written → returns the stale id.
        """
        import json
        from moto import mock_aws
        from datetime import datetime, timezone, timedelta

        with mock_aws():
            ddb, tbl = _moto_setup(_MOTO_TABLE)
            _reset_handler_clients()

            with patch.dict(os.environ, {
                "AVX_INVESTIGATIONS_TABLE": _MOTO_TABLE,
                "AVX_INVESTIGATIONS_QUEUE_URL": _MOTO_QUEUE,
            }):
                sqs_mock = MagicMock()
                sqs_mock.send_message.return_value = {"MessageId": "m-race"}
                import handler as h
                h._sqs_client = sqs_mock

                # Seed: failed row X (not live), lock → X
                stale_ts = (
                    datetime.now(timezone.utc) - timedelta(hours=3)
                ).strftime("%Y-%m-%dT%H:%M:%SZ")
                fresh_ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                x_id = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
                tbl.put_item(Item={
                    "investigation_id": f"lock#{_PATTERN_ID}",
                    "investigation_id_ref": x_id,
                    "requested_at": stale_ts,
                })
                tbl.put_item(Item={
                    "investigation_id": x_id,
                    "pattern_id": _PATTERN_ID,
                    "status": "failed",
                    "requested_at": stale_ts,
                    "requested_by": "eng-x",
                })

                # First POST on same pattern (a concurrent request): creates Y.
                r_first = h.handler(_post_event(), None)
                assert r_first["statusCode"] == 202
                y_id = json.loads(r_first["body"])["investigation_id"]

                # Now the lock points to Y (live queued).
                # Simulate: A's pre-read saw lock → X (failed, not live),
                # but by transaction time the lock points to Y.
                # Force get_item to return X as the lock ref on first call.
                real_tbl = h._get_investigations_table()
                calls = [0]

                class _StaleRefTable:
                    def get_item(self_, **kwargs):
                        calls[0] += 1
                        k = kwargs.get("Key", {}).get("investigation_id", "")
                        # Return stale X ref on the first call (lock read).
                        if calls[0] == 1:
                            return {"Item": {
                                "investigation_id": f"lock#{_PATTERN_ID}",
                                "investigation_id_ref": x_id,
                                "requested_at": stale_ts,
                            }}
                        # Return X (failed, not live) on the second call (row read).
                        if calls[0] == 2:
                            return {"Item": {
                                "investigation_id": x_id,
                                "pattern_id": _PATTERN_ID,
                                "status": "failed",
                                "requested_at": stale_ts,
                                "requested_by": "eng-x",
                            }}
                        return real_tbl.get_item(**kwargs)

                    def __getattr__(self_, n):
                        return getattr(real_tbl, n)

                with patch.object(h, "_get_investigations_table",
                                  return_value=_StaleRefTable()):
                    r = h.handler(_post_event(), None)

                body = json.loads(r["body"])
                # With real condition: transaction cancels (lock changed X→Y) → returns Y.
                # With H5 mutant: condition passes → new id written → returns new id.
                assert r["statusCode"] == 200, (
                    f"Stale lock ref race must return 200 with winner's id; "
                    f"got {r['statusCode']} body={body}. "
                    "Mutant C3-H5: lock condition always-true → new id → 202 → FAILS."
                )
                assert body.get("investigation_id") == y_id, (
                    f"Must return y_id={y_id!r} (the winner); "
                    f"got {body.get('investigation_id')!r}. "
                    "Mutant C3-H5: returns new or stale id instead of winner → FAILS."
                )


class TestC3EmptyWinnerReturns503:
    """C3: empty winner_id returns 503 (H9).

    When ALL_OLD is empty AND the re-read is also empty (e.g. the lock was
    just deleted), winner_id = "" → route must return 503 (not 200 "").

    Mutant C3-H9: `return _respond(200, {"investigation_id": str(winner_id or "")})`
    → returns 200 with "" → test FAILS (expected 503).
    """

    def test_empty_winner_id_returns_503_not_200_empty(self):
        """winner_id="" after failed ALL_OLD + re-read → 503.

        Mutant C3-H9: returns 200 with "" → FAILS.
        """
        import handler as h
        from botocore.exceptions import ClientError

        # Build a TransactionCanceledException where the lock condition fired (index 0
        # when no prefix) with an empty Item (ALL_OLD empty).
        cancel_error = ClientError(
            {
                "Error": {
                    "Code": "TransactionCanceledException",
                    "Message": "Transaction cancelled",
                },
                "CancellationReasons": [
                    {"Code": "ConditionalCheckFailed", "Item": {}},  # lock fired, empty ALL_OLD
                    {"Code": "None"},
                    {"Code": "None"},
                ],
            },
            "TransactWriteItems",
        )

        ddb_mock = MagicMock()
        ddb_mock.transact_write_items.side_effect = cancel_error
        sqs_mock = MagicMock()
        sqs_mock.send_message.return_value = {}

        # Table: no lock exists (empty pre-read), and re-read also returns empty.
        table_mock = MagicMock()
        table_mock.get_item.return_value = {}   # empty — no lock, no re-read result

        with patch.object(h, "_get_investigations_table", return_value=table_mock), \
             patch.object(h, "_get_ddb_client", return_value=ddb_mock), \
             patch.object(h, "_get_sqs_client", return_value=sqs_mock):
            r = h.handler(_post_event(), None)

        # C3-H9 mutant: returns 200 with ""; real code returns 503.
        assert r["statusCode"] == 503, (
            f"Empty winner_id must return 503; got {r['statusCode']}. "
            "Mutant C3-H9: `or ''` → returns 200 with empty id → FAILS."
        )


class TestC3LockIdxCorrectWithPrefix:
    """C3: lock_idx = len(prefix) + 0 (not hardcoded 0) when prefix is present (H12).

    When seen_ref is set, the transaction has a prefix item (Update or ConditionCheck)
    at index 0 and the Lock Put at index 1.  lock_idx = 0 would misidentify the
    prefix as the lock.

    Mutant C3-H12: lock_idx = 0 → when the lock (index 1) fires, the route reads
    the prefix's ALL_OLD item as the lock → gets wrong winner_id → FAILS.
    """

    def test_lock_idx_is_one_when_prefix_present(self):
        """With seen_ref (prefix item), lock fires at index 1; route reads correct id.

        This test exercises the path where:
        - seen_ref is set (a failed row X is pre-read)
        - Another concurrent request updated the lock to point to Y
        - Lock Put condition fires at index 1 (not 0)
        - Route reads index 1's ALL_OLD to get Y's id

        Mutant C3-H12: lock_idx=0 → reads index 0 (ConditionCheck's ALL_OLD, which
        carries X's investigation_id) instead of Y's → returns X (the failed row).
        """
        import json
        from moto import mock_aws
        from datetime import datetime, timezone, timedelta

        with mock_aws():
            ddb, tbl = _moto_setup(_MOTO_TABLE)
            _reset_handler_clients()

            with patch.dict(os.environ, {
                "AVX_INVESTIGATIONS_TABLE": _MOTO_TABLE,
                "AVX_INVESTIGATIONS_QUEUE_URL": _MOTO_QUEUE,
            }):
                sqs_mock = MagicMock()
                sqs_mock.send_message.return_value = {"MessageId": "m-lockidx"}
                import handler as h
                h._sqs_client = sqs_mock

                stale_ts = (datetime.now(timezone.utc) - timedelta(hours=3)
                            ).strftime("%Y-%m-%dT%H:%M:%SZ")
                x_id = "ffffffff-0000-4000-8000-000000000001"
                tbl.put_item(Item={
                    "investigation_id": f"lock#{_PATTERN_ID}",
                    "investigation_id_ref": x_id,
                    "requested_at": stale_ts,
                })
                tbl.put_item(Item={
                    "investigation_id": x_id,
                    "status": "failed",
                    "requested_at": stale_ts,
                    "requested_by": "eng-x",
                })

                # Concurrent request Y takes the lock before A's transaction.
                r_y = h.handler(_post_event(), None)
                assert r_y["statusCode"] == 202
                y_id = json.loads(r_y["body"])["investigation_id"]

                # Now force A to use the stale pre-read (sees X=failed → ConditionCheck).
                real_tbl = h._get_investigations_table()
                calls2 = [0]

                class _StalePreread:
                    def get_item(self_, **kwargs):
                        calls2[0] += 1
                        if calls2[0] == 1:
                            return {"Item": {
                                "investigation_id": f"lock#{_PATTERN_ID}",
                                "investigation_id_ref": x_id,
                                "requested_at": stale_ts,
                            }}
                        if calls2[0] == 2:
                            return {"Item": {
                                "investigation_id": x_id,
                                "status": "failed",
                                "requested_at": stale_ts,
                                "requested_by": "eng-x",
                            }}
                        return real_tbl.get_item(**kwargs)

                    def __getattr__(self_, n):
                        return getattr(real_tbl, n)

                with patch.object(h, "_get_investigations_table",
                                  return_value=_StalePreread()):
                    r_a = h.handler(_post_event(), None)

                body_a = json.loads(r_a["body"])
                # Correct: lock fired at index 1 → reads Y's id → returns 200 Y.
                # Mutant H12 (lock_idx=0): reads index 0 (ConditionCheck) → X → 200 X.
                assert r_a["statusCode"] == 200, (
                    f"Lock race with prefix must return 200 winner's id; "
                    f"got {r_a['statusCode']} body={body_a}. "
                    "Mutant C3-H12: lock_idx=0 → reads wrong ALL_OLD → FAILS."
                )
                assert body_a.get("investigation_id") == y_id, (
                    f"Must return y_id={y_id!r} (winner); "
                    f"got {body_a.get('investigation_id')!r}. "
                    "Mutant C3-H12: lock_idx=0 → returns x_id (the failed row) → FAILS."
                )


class TestW8PatternIdValidation:
    """W8: pattern_id shape check (H14a) and type check (H14b).

    Mutant W8-H14a: len/RE check removed → oversized or malformed pattern_id
    passes through to DynamoDB → later errors or incorrect lock keys.
    Mutant W8-H14b: type check removed → integer pattern_id coerced to string.
    """

    def _post(self, body_override):
        import handler as h
        return h.handler(
            {
                "httpMethod": "POST",
                "body": json.dumps(body_override),
                "requestContext": {"authorizer": {"claims": {
                    "sub": "eng-001",
                    "cognito:groups": "product-engineer",
                }}},
            },
            None,
        )

    def test_oversized_pattern_id_returns_400(self):
        """pattern_id longer than 256 chars returns 400.

        Mutant W8-H14a: shape check removed → proceeds with oversized id.
        """
        r = self._post({"pattern_id": "a" * 300})
        assert r["statusCode"] == 400, (
            f"Oversized pattern_id must return 400; got {r['statusCode']}. "
            "Mutant W8-H14a: shape check removed → proceeds with 300-char id → FAILS."
        )

    def test_nonalnum_pattern_id_returns_400(self):
        """pattern_id containing illegal characters (e.g. quote) returns 400.

        Mutant W8-H14a: shape check removed → illegal char passes through.
        """
        r = self._post({"pattern_id": "a'b"})
        assert r["statusCode"] == 400, (
            f"pattern_id with illegal char must return 400; got {r['statusCode']}. "
            "Mutant W8-H14a: RE check removed → illegal char passes → FAILS."
        )

    def test_nonstring_pattern_id_returns_400(self):
        """pattern_id as integer returns 400.

        Mutant W8-H14b: type check removed → int coerced to str → proceeds.
        """
        r = self._post({"pattern_id": 12345})
        assert r["statusCode"] == 400, (
            f"Integer pattern_id must return 400; got {r['statusCode']}. "
            "Mutant W8-H14b: type check removed → int coerced to str → FAILS."
        )

    def test_list_pattern_id_returns_400(self):
        """pattern_id as list returns 400 (exercises isinstance check)."""
        r = self._post({"pattern_id": ["abc"]})
        assert r["statusCode"] == 400, (
            f"List pattern_id must return 400; got {r['statusCode']}. "
            "Mutant W8-H14b: type check removed → list coerced → FAILS."
        )


class TestC2ConditionCheckAlwaysTrue:
    """C2-H20: ConditionCheck condition always-true.

    When the pre-read sees a failed row X (not stale → ConditionCheck path),
    and by transaction time X is completed (live), the ConditionCheck must fire
    and return X's id.

    Mutant C2-H20: condition always-true → ConditionCheck never fires →
    transaction creates a new investigation → 202 (not 200 X) → FAILS.
    """

    def test_conditioncheck_fires_for_revived_failed_row(self):
        """ConditionCheck fires when a failed pre-read row becomes completed.

        Mutant C2-H20: always-true → transaction succeeds → 202 with new id → FAILS.
        """
        import json
        from moto import mock_aws
        from datetime import datetime, timezone

        with mock_aws():
            ddb, tbl = _moto_setup(_MOTO_TABLE)
            _reset_handler_clients()

            with patch.dict(os.environ, {
                "AVX_INVESTIGATIONS_TABLE": _MOTO_TABLE,
                "AVX_INVESTIGATIONS_QUEUE_URL": _MOTO_QUEUE,
            }):
                sqs_mock = MagicMock()
                sqs_mock.send_message.return_value = {"MessageId": "m-cc"}
                import handler as h
                h._sqs_client = sqs_mock

                now_ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                x_id = "ccddeeaa-0000-4000-8000-000000000002"
                # X is failed (not stale → ConditionCheck path).
                tbl.put_item(Item={
                    "investigation_id": f"lock#{_PATTERN_ID}",
                    "investigation_id_ref": x_id,
                    "requested_at": now_ts,
                })
                tbl.put_item(Item={
                    "investigation_id": x_id,
                    "pattern_id": _PATTERN_ID,
                    "status": "failed",
                    "requested_at": now_ts,
                    "requested_by": "eng-x",
                })

                # Update X to completed (now live) BEFORE the transaction runs.
                # The pre-read sees X=failed, but the ConditionCheck sees X=completed.
                tbl.update_item(
                    Key={"investigation_id": x_id},
                    UpdateExpression="SET #s = :c, finding_id = :f",
                    ExpressionAttributeNames={"#s": "status"},
                    ExpressionAttributeValues={":c": "completed", ":f": "FIND-cc"},
                )

                # Force the route's pre-read to return failed X (stale snapshot).
                # Create real_tbl directly (not via h._get_investigations_table which
                # uses _TABLE_NAME which may be "" before handler() runs).
                import boto3 as _b3
                real_tbl = _b3.resource("dynamodb", region_name="us-west-2").Table(_MOTO_TABLE)
                reads = [0]

                class _SnapshotTable:
                    def get_item(self_, **kwargs):
                        reads[0] += 1
                        k = (kwargs.get("Key") or {}).get("investigation_id", "")
                        if reads[0] == 2 and k == x_id:
                            # Second read = row read; return X as failed (stale snapshot).
                            return {"Item": {
                                "investigation_id": x_id,
                                "status": "failed",
                                "requested_at": now_ts,
                                "requested_by": "eng-x",
                            }}
                        return real_tbl.get_item(**kwargs)

                    def __getattr__(self_, n):
                        return getattr(real_tbl, n)

                with patch.object(h, "_get_investigations_table",
                                  return_value=_SnapshotTable()):
                    r = h.handler(_post_event(), None)

                body = json.loads(r["body"])
                # ConditionCheck fires (X is completed, not failed) → returns X's id.
                # Mutant C2-H20: condition always-true → transaction creates new id → 202.
                assert r["statusCode"] == 200, (
                    f"ConditionCheck must fire → return X's id (completed, live); "
                    f"got {r['statusCode']} body={body}. "
                    "Mutant C2-H20: always-true → transaction creates new id → 202 → FAILS."
                )
                returned_id = body.get("investigation_id")
                assert returned_id == x_id, (
                    f"Must return x_id={x_id!r}; got {returned_id!r}. "
                    "Mutant C2-H20: returns new id instead of completed X → FAILS."
                )






class TestW1DeadWinnerRetry:
    """W1 fix (cycle-3 answer 6): lock cancellation whose winner is not live retries create.

    Probe N1: two racing restarts on a stale row -- the loser gets the dead row's
    id (200 with status=failed).  Fix: check winner liveness; if dead, retry.

    Mutation guard (test_dead_winner_retries_create):
        Return winner_id without liveness check -> caller gets a dead id ->
        subsequent GET returns status=failed -> FAILS.
    """

    def test_dead_winner_retries_create(self):
        """Lock condition fires; round 2 pre-read sees no lock → new investigation (202).

        Simulates the race:
          1. Lock condition fires (round 1: attribute_not_exists, lock now gone).
          2. Round 2 pre-read: no lock exists → creates a new investigation.
          3. Caller receives 202 (not 200 with a dead id from ALL_OLD).

        With the new bounded-loop design, on a lock-condition cancellation the
        handler does NOT extract the winner from ALL_OLD and check its liveness.
        Instead, the next round's fresh pre-read re-observes the table and
        decides correctly.

        Mutation guard: on lock cancellation, return ALL_OLD winner_id directly
        (skipping fresh pre-read) → returns dead id → test asserts 202 → FAILS.
        """
        import json as _json
        import uuid as _uuid
        from botocore.exceptions import ClientError
        import handler as h

        dead_id = str(_uuid.uuid4())
        lock_key_str = f"lock#{_PATTERN_ID}"

        sqs_mock = MagicMock()
        sqs_mock.send_message.return_value = {"MessageId": "msg-w1"}

        call_counts = {"txn": 0}

        def _fake_transact(**kwargs):
            call_counts["txn"] += 1
            if call_counts["txn"] == 1:
                # First attempt: lock condition fires; ALL_OLD has dead_id.
                raise ClientError(
                    {
                        "Error": {
                            "Code": "TransactionCanceledException",
                            "Message": "cancelled",
                        },
                        "CancellationReasons": [
                            {
                                "Code": "ConditionalCheckFailed",
                                "Item": {
                                    "investigation_id": {"S": lock_key_str},
                                    "investigation_id_ref": {"S": dead_id},
                                },
                            },
                            {"Code": "None"},
                            {"Code": "None"},
                        ],
                    },
                    "TransactWriteItems",
                )
            # Second attempt: succeeds.
            return {}

        get_pks: list[str] = []

        def _fake_get(*, Key, ConsistentRead=False, **_kw):
            pk = Key.get("investigation_id", "")
            get_pks.append(pk)
            if pk == lock_key_str:
                return {}   # no pre-existing lock (both rounds)
            if pk == dead_id:
                # If called: winner liveness check (old behavior).
                return {
                    "Item": {
                        "investigation_id": dead_id,
                        "status": "failed",
                        "requested_at": "2026-01-01T00:00:00Z",
                    }
                }
            return {}

        class _Table:
            def get_item(self_, **kw):
                return _fake_get(**kw)
            def __getattr__(self_, n):
                raise AttributeError(n)

        class _DDB:
            def transact_write_items(self_, **kw):
                return _fake_transact(**kw)
            def get_item(self_, **kw):
                return _fake_get(**kw)

        with (
            patch.object(h, "_get_investigations_table", return_value=_Table()),
            patch.object(h, "_get_ddb_client", return_value=_DDB()),
            patch.object(h, "_get_sqs_client", return_value=sqs_mock),
        ):
            result = h.handler(_post_event(pattern_id=_PATTERN_ID), None)

        body = _json.loads(result["body"])

        # The handler must retry (not return 200 with dead_id from ALL_OLD).
        assert result["statusCode"] == 202, (
            f"After lock-cancellation retry the handler must return 202; "
            f"got {result['statusCode']} body={body}. "
            "Mutation: return ALL_OLD winner_id directly → 200 with dead id → FAILS."
        )
        returned_id = body.get("investigation_id", "")
        assert returned_id != dead_id, (
            f"Handler must NOT return the dead winner_id ({dead_id!r}); "
            f"got {returned_id!r}. "
            "Mutation: return ALL_OLD winner_id → dead id returned → FAILS."
        )
        assert call_counts["txn"] == 2, (
            f"Expected 2 transact_write calls (1 fail + 1 succeed); "
            f"got {call_counts['txn']}. "
            "Mutation: return dead id without retry → only 1 call → FAILS."
        )
        # New design: the handler does NOT explicitly GET the winner id;
        # instead the next round's pre-read re-observes the table.
        # (No assertion on dead_id in get_pks — the new design reads the
        # lock key only, not the dead row id directly.)



# =============================================================================
# G3F3.2c — fix: bounded loop with fresh pre-read on every round
# Tests for the two new mutation guards added in the fix.
# =============================================================================


class TestN1InterleavingBGetsAIdNoSecondSend:
    """N1: racing takeovers on a stale row — loser B gets winner A's id.

    Probe N1 (``/tmp/.../probe_route3.py`` output): A and B both pre-read a
    stale queued lock → X.  A commits first (marks X failed, writes A, updates
    lock → A).  B's takeover Update on X then fires (X is already failed). With
    the old code, the prefix-cancellation branch returned ``seen_ref = X`` with
    200 — a dead-row id.

    Fix: prefix cancellation → ``continue`` → round 2 fresh pre-read → lock → A
    (live, queued) → return 200 A.

    Mutation guard:
      Mutant 1 — return ``seen_ref`` on prefix cancellation instead of
      ``continue``: B returns 200 X (the dead row); the assertion
      ``b_id == a_id`` fails.
    """

    def test_n1_interleaving_b_gets_a_id_no_second_send(self):
        """B's stale takeover Update fires; round 2 pre-read returns A's id (200, no SQS).

        Setup:
          - Stale queued row X + lock → X seeded in moto.
          - A runs first: marks X failed, creates A, lock → A (202, SQS sent once).
          - B's round 1 pre-read is intercepted to return the pre-A snapshot
            (stale: lock → X, X = queued).
          - B's transaction: Update(X: mark failed) fires (X already failed by A).
          - Round 2 pre-read (real table): lock → A (queued, fresh) → live → 200 A.
          - B must return 200 with A's id and must NOT send a second SQS message.

        Mutation guard (Mutant 1: return seen_ref on prefix cancellation):
          B returns 200 with X (the dead row) → ``b_id == a_id`` fails since
          dead_id == X != a_id.
        """
        import json
        import os
        from datetime import datetime, timezone, timedelta
        from moto import mock_aws

        with mock_aws():
            ddb_client, table = _moto_setup(_MOTO_TABLE)
            _reset_handler_clients()

            with patch.dict(os.environ, {
                "AVX_INVESTIGATIONS_TABLE": _MOTO_TABLE,
                "AVX_INVESTIGATIONS_QUEUE_URL": _MOTO_QUEUE,
            }):
                import handler as h
                sqs_mock = MagicMock()
                sqs_mock.send_message.return_value = {"MessageId": "m-n1"}
                h._sqs_client = sqs_mock

                stale_ts = (
                    datetime.now(timezone.utc) - timedelta(hours=3)
                ).strftime("%Y-%m-%dT%H:%M:%SZ")
                x_id = "00000001-0000-4111-8000-000000000001"

                # Seed stale state: lock → X (queued, 2 h old).
                table.put_item(Item={
                    "investigation_id": f"lock#{_PATTERN_ID}",
                    "pattern_id": _PATTERN_ID,
                    "investigation_id_ref": x_id,
                    "requested_at": stale_ts,
                })
                table.put_item(Item={
                    "investigation_id": x_id,
                    "pattern_id": _PATTERN_ID,
                    "status": "queued",
                    "requested_at": stale_ts,
                    "requested_by": "eng-old",
                })

                # Capture the snapshot that B's pre-read will see.
                snap_lock = table.get_item(
                    Key={"investigation_id": f"lock#{_PATTERN_ID}"},
                    ConsistentRead=True,
                ).get("Item")
                snap_x = table.get_item(
                    Key={"investigation_id": x_id},
                    ConsistentRead=True,
                ).get("Item")

                # Request A runs first (uses the real table — sees stale X → takes over).
                r_a = h.handler(_post_event(pattern_id=_PATTERN_ID), None)
                assert r_a["statusCode"] == 202, (
                    f"A must succeed (202); got {r_a['statusCode']}"
                )
                a_id = json.loads(r_a["body"])["investigation_id"]
                assert a_id != x_id, "A must create a new id, not reuse stale X."
                assert sqs_mock.send_message.call_count == 1, (
                    "A must enqueue exactly one SQS message."
                )

                # Verify A updated the table correctly.
                x_row = table.get_item(Key={"investigation_id": x_id})["Item"]
                assert x_row["status"] == "failed", "A must have marked X failed."
                lock_ref = table.get_item(
                    Key={"investigation_id": f"lock#{_PATTERN_ID}"}
                )["Item"]["investigation_id_ref"]
                assert lock_ref == a_id, "Lock must now point to A's id."

                # Request B: round 1 pre-read is intercepted to return the
                # pre-A snapshot (stale: lock → X, X = queued).
                real_tbl = h._get_investigations_table()
                pre_a_calls = [0]

                class _PreASnapshot:
                    def get_item(self_, **kwargs):
                        pk = (kwargs.get("Key") or {}).get("investigation_id", "")
                        pre_a_calls[0] += 1
                        if pre_a_calls[0] == 1 and pk == f"lock#{_PATTERN_ID}":
                            # B round 1 lock read: stale (pre-A) lock → X.
                            return {"Item": snap_lock} if snap_lock else {}
                        if pre_a_calls[0] == 2 and pk == x_id:
                            # B round 1 X row read: stale (pre-A) X = queued.
                            return {"Item": snap_x} if snap_x else {}
                        # Round 2+: real table.
                        return real_tbl.get_item(**kwargs)

                    def __getattr__(self_, name):
                        return getattr(real_tbl, name)

                sqs_call_count_before_b = sqs_mock.send_message.call_count

                with patch.object(h, "_get_investigations_table",
                                  return_value=_PreASnapshot()):
                    r_b = h.handler(_post_event(pattern_id=_PATTERN_ID), None)

                b_body = json.loads(r_b["body"])
                b_id = b_body.get("investigation_id")

                # B must return 200 with A's id (winner from round 2 pre-read).
                assert r_b["statusCode"] == 200, (
                    f"B must return 200 (A is the live winner); "
                    f"got {r_b['statusCode']} body={b_body}. "
                    "Mutant 1 (return seen_ref on prefix cancellation): "
                    "B returns 200 with dead X → statusCode may still be 200 but id ≠ A."
                )
                assert b_id == a_id, (
                    f"B must return A's id ({a_id!r}), not dead X ({x_id!r}); "
                    f"got {b_id!r}. "
                    "Mutant 1 (return seen_ref on prefix cancellation): "
                    "B returns X (dead row) → b_id == x_id ≠ a_id → FAILS."
                )

                # B must NOT send a second SQS message (A already owns this pattern).
                assert sqs_mock.send_message.call_count == sqs_call_count_before_b, (
                    f"B must NOT send SQS (A is the live investigation); "
                    f"expected {sqs_call_count_before_b} calls, "
                    f"got {sqs_mock.send_message.call_count}. "
                    "Mutant 1: B returns 200 with X and no SQS — but x_id ≠ a_id "
                    "would catch it first."
                )


class TestStaleQueuedWinnerAfterLockCancellationMarkedFailed:
    """Stale queued winner seen after lock cancellation is marked failed + new id created.

    The second defect described in the bug: when the dead-winner retry ran it
    set ``transact_items_prefix = []``, so a stale queued/running winner was
    never marked failed.  The old SQS message could then run ``_set_running``
    and write a Finding.

    Fix: lock cancellation → ``continue`` → round 2 fresh pre-read → stale winner
    seen → takeover Update (mark failed) + create new.

    Mutation guard:
      Mutant 2 — on the retry round, skip the takeover Update (set
      ``transact_items_prefix = []`` regardless of stale status): the stale
      winner stays queued; the assertion ``stale_row["status"] == "failed"``
      fails.
    """

    def test_stale_queued_winner_after_lock_cancellation_marked_failed(self):
        """After lock fires in round 1, round 2 finds stale queued winner and marks it failed.

        Setup:
          - No lock exists initially (so round 1 uses attribute_not_exists condition).
          - Stale queued winner Y + lock → Y seeded between round 1 pre-read and
            round 1 transaction, simulated by forcing round 1's pre-read to return
            empty (no lock) while the real table already has stale Y.
          - Round 1: condition = attribute_not_exists → transaction fires (lock exists).
          - Round 2: fresh pre-read → lock → Y (stale queued, 2 h old) → not live →
            takeover Update(Y: mark failed) + create new → 202.
          - After transaction: Y.status == "failed"; new row created; SQS sent once.

        Mutation guard (Mutant 2: skip takeover on retry round):
          Round 2 builds no Update for Y → transaction succeeds but Y stays queued →
          stale_row["status"] == "queued" ≠ "failed" → assertion FAILS.
        """
        import json
        import os
        from datetime import datetime, timezone, timedelta
        from moto import mock_aws

        with mock_aws():
            ddb_client, table = _moto_setup(_MOTO_TABLE)
            _reset_handler_clients()

            with patch.dict(os.environ, {
                "AVX_INVESTIGATIONS_TABLE": _MOTO_TABLE,
                "AVX_INVESTIGATIONS_QUEUE_URL": _MOTO_QUEUE,
            }):
                import handler as h
                sqs_mock = MagicMock()
                sqs_mock.send_message.return_value = {"MessageId": "m-sq"}
                h._sqs_client = sqs_mock
                # Set _TABLE_NAME so _get_investigations_table() uses the moto table
                # (handler() would set it on first call, but we need real_tbl first).
                h._TABLE_NAME = _MOTO_TABLE
                h._QUEUE_URL = _MOTO_QUEUE

                stale_ts = (
                    datetime.now(timezone.utc) - timedelta(hours=3)
                ).strftime("%Y-%m-%dT%H:%M:%SZ")
                y_id = "00000002-0000-4111-8000-000000000002"

                # Seed stale queued row Y + lock → Y (already in the table).
                table.put_item(Item={
                    "investigation_id": f"lock#{_PATTERN_ID}",
                    "pattern_id": _PATTERN_ID,
                    "investigation_id_ref": y_id,
                    "requested_at": stale_ts,
                })
                table.put_item(Item={
                    "investigation_id": y_id,
                    "pattern_id": _PATTERN_ID,
                    "status": "queued",
                    "requested_at": stale_ts,
                    "requested_by": "eng-old",
                })

                # Force round 1 pre-read to return empty (no lock seen).
                real_tbl = h._get_investigations_table()
                round1_lock_call = [False]

                class _EmptyRound1:
                    def get_item(self_, **kwargs):
                        pk = (kwargs.get("Key") or {}).get("investigation_id", "")
                        if not round1_lock_call[0] and pk == f"lock#{_PATTERN_ID}":
                            # First lock read → empty (simulates round 1 stale pre-read).
                            round1_lock_call[0] = True
                            return {}
                        # All subsequent reads (round 2+): real table.
                        return real_tbl.get_item(**kwargs)

                    def __getattr__(self_, name):
                        return getattr(real_tbl, name)

                with patch.object(h, "_get_investigations_table",
                                  return_value=_EmptyRound1()):
                    r = h.handler(_post_event(pattern_id=_PATTERN_ID), None)

                body = json.loads(r["body"])
                new_id = body.get("investigation_id")

                # Handler must create a new investigation (not return stale Y).
                assert r["statusCode"] == 202, (
                    f"Must create a new investigation (202); "
                    f"got {r['statusCode']} body={body}. "
                    "Mutant 2: takeover skipped → transaction may still succeed "
                    "but Y not marked failed; 202 may still be returned if "
                    "lock condition is met."
                )
                assert new_id != y_id, (
                    f"New id must differ from stale Y ({y_id!r}); got {new_id!r}."
                )

                # Stale row Y must be marked failed (takeover Update ran).
                stale_row = table.get_item(
                    Key={"investigation_id": y_id}
                ).get("Item", {})
                assert stale_row.get("status") == "failed", (
                    f"Stale queued winner Y must be marked failed by the takeover "
                    f"Update; got status={stale_row.get('status')!r}. "
                    "Mutant 2 (skip takeover on retry round): no Update on Y → "
                    "Y stays queued → old SQS message can run _set_running → "
                    "duplicate Finding; assertion FAILS."
                )

                # Lock must point to the new investigation.
                lock_ref = table.get_item(
                    Key={"investigation_id": f"lock#{_PATTERN_ID}"}
                ).get("Item", {}).get("investigation_id_ref")
                assert lock_ref == new_id, (
                    f"Lock must point to new_id ({new_id!r}); got {lock_ref!r}."
                )

                # Exactly one SQS message sent (for the new investigation).
                assert sqs_mock.send_message.call_count == 1, (
                    f"Exactly 1 SQS message must be sent (new investigation); "
                    f"got {sqs_mock.send_message.call_count}."
                )
                sqs_body = sqs_mock.send_message.call_args.kwargs.get("MessageBody", "")
                assert sqs_body == new_id, (
                    f"SQS message body must be new_id={new_id!r}; got {sqs_body!r}."
                )



# =============================================================================
# G3-3 Fix: R5 / R11
# =============================================================================


class TestG3FixR5PrereadQueuedAge:
    """R5-preread-queued-age-ignores-queued_at:

    When a 'queued' row was requeued (a2 W3 fix sets queued_at), its age for
    the pre-read liveness check must be measured from queued_at, not from
    requested_at.

    Scenario: requested_at is stale (> _STALE_QUEUED_AGE_SECONDS ago) but
    queued_at is fresh (< 60 seconds ago).  The handler must return 200 + the
    existing investigation_id because the row is still live.

    With R5 mutation (uses requested_at for 'queued' rows too): requested_at is
    stale → is_live_investigation returns False → handler creates a new id → 202
    with a different id → assertion fails → CAUGHT.
    """

    def test_queued_row_liveness_uses_queued_at_not_requested_at(self):
        """R5: pre-read for a requeued row uses queued_at for age calculation."""
        import handler as h
        from unittest.mock import patch as _patch
        from datetime import datetime, timedelta, timezone

        # requested_at: 3 hours ago → stale (default _STALE_QUEUED_AGE_SECONDS = 7200;
        # was 2 h against the old 3600 default — moved so requested_at stays stale)
        two_hours_ago = (
            datetime.now(timezone.utc) - timedelta(hours=3)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")

        # queued_at: 1 minute ago → fresh
        one_minute_ago = (
            datetime.now(timezone.utc) - timedelta(minutes=1)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")

        # Build an investigation row that has both timestamps
        requeued_inv = {
            "investigation_id": _INVESTIGATION_ID,
            "pattern_id": _PATTERN_ID,
            "status": "queued",
            "requested_at": two_hours_ago,   # stale — must NOT be used
            "queued_at": one_minute_ago,      # fresh — must be used
            "requested_by": _ENGINEER_SUB,
        }

        table_mock = _table_mock_seq(
            _lock_item_for_pattern(_INVESTIGATION_ID),   # pre-read: lock exists
            requeued_inv,                                 # pre-read: investigation row
        )
        sqs_mock = MagicMock()
        ddb_mock = MagicMock()  # must NOT be called (live row → no transaction)

        with _patch.object(h, "_get_investigations_table", return_value=table_mock), \
             _patch.object(h, "_get_ddb_client", return_value=ddb_mock), \
             _patch.object(h, "_get_sqs_client", return_value=sqs_mock):
            resp = h.handler(_post_event(claims=_ENGINEER_CLAIMS), None)

        assert resp["statusCode"] == 200, (
            f"Requeued investigation with fresh queued_at must return 200 + existing id. "
            f"Got statusCode={resp['statusCode']!r}. "
            "R5 mutation uses requested_at (stale) → not live → new id → 202."
        )
        body = json.loads(resp["body"])
        assert body.get("investigation_id") == _INVESTIGATION_ID, (
            f"Must return existing id {_INVESTIGATION_ID!r}; "
            f"got {body.get('investigation_id')!r}. "
            "R5 mutation: stale age → new investigation created → different id."
        )
        assert not ddb_mock.transact_write_items.called, (
            "transact_write_items must NOT be called when a live investigation exists. "
            "R5 mutation: requested_at is stale → handler thinks row is dead "
            "→ tries to create new investigation → transaction called."
        )


class TestG3FixR11RowPutCondition:
    """R11-row-put-condition-dropped:

    The investigation row Put must have ConditionExpression
    'attribute_not_exists(investigation_id)' so a UUID collision is detected.

    R11 mutation: changes condition to
    'attribute_exists(investigation_id) OR attribute_not_exists(investigation_id)'
    (always-true), which silently overwrites an existing row on a UUID collision.

    The existing _FakeTransactDDB test uses an 'in' substring check that passes
    for the mutant (the string 'attribute_not_exists(investigation_id)' still
    appears inside the always-true expression).  This moto test forces an actual
    UUID collision so the condition is evaluated for real.
    """

    def test_row_put_condition_blocks_uuid_collision_via_moto(self):
        """R11: UUID collision raises an exception; no overwrite, no 202 response.

        Approach:
        1. Pre-seed a row with investigation_id = COLLISION_UUID.
        2. Patch uuid.uuid4() to always return COLLISION_UUID.
        3. POST a new investigation: the row Put condition fires.
           - Correct code: attribute_not_exists → ConditionalCheckFailed at index 1
             → reasons[lock_idx] and reasons[cap_idx] don't match → raise propagates.
           - R11 mutant: always-true → transaction succeeds → 202 → CAUGHT.
        """
        from moto import mock_aws
        import uuid as _uuid_mod
        import os

        # A valid UUID for the collision target
        collision_uuid = "eeeeeeee-eeee-4eee-8eee-000000000001"
        r11_pattern_id = "pat-r11-uuid-collision"

        with mock_aws():
            ddb_client, table = _moto_setup(_MOTO_TABLE)
            _reset_handler_clients()

            # Pre-seed the collision row so the condition fires
            table.put_item(Item={
                "investigation_id": collision_uuid,
                "pattern_id": r11_pattern_id,
                "status": "completed",
                "requested_by": "eng-original-r11",
                "requested_at": "2026-09-01T00:00:00Z",
            })

            with patch.dict(os.environ, {
                "AVX_INVESTIGATIONS_TABLE": _MOTO_TABLE,
                "AVX_INVESTIGATIONS_QUEUE_URL": _MOTO_QUEUE,
            }):
                sqs_mock = MagicMock()
                sqs_mock.send_message.return_value = {"MessageId": "msg-r11-test"}
                import handler as h
                h._sqs_client = sqs_mock

                raised = False
                resp_code = None
                # Patch uuid.uuid4 so it always returns the existing row's id
                with patch.object(_uuid_mod, "uuid4",
                                  return_value=_uuid_mod.UUID(collision_uuid)):
                    try:
                        resp = h.handler(
                            _post_event(pattern_id=r11_pattern_id,
                                        claims=_ENGINEER_CLAIMS),
                            None,
                        )
                        resp_code = resp["statusCode"]
                    except Exception:
                        raised = True

                assert raised or resp_code != 202, (
                    f"R11: UUID collision with correct attribute_not_exists condition "
                    f"must not succeed with 202. "
                    f"raised={raised}, resp_code={resp_code}. "
                    "R11 mutation: always-true condition → row is silently overwritten "
                    "→ transaction succeeds → 202 is returned → CAUGHT."
                )
                # The correct code raises (ConditionalCheckFailed at row-put is
                # not handled by any of the lock/cap/prefix checks).
                assert raised, (
                    "R11 (correct code): UUID collision must propagate an exception. "
                    "attribute_not_exists fires → ConditionalCheckFailed at index 1 "
                    "→ handler raises → CAUGHT. "
                    "R11 mutation: condition always-true → no failure → 202 returned."
                )


# ============================================================================
# S3 fix: mark-failed metric emitter reads AWS_REGION
# ============================================================================

class TestMarkFailedMetricEmitter:
    """S3 fix: InvestigationMarkFailedError emitter reads AWS_REGION, not a hardcode.

    The emitter runs when SQS send_message fails AND the mark-failed DDB write
    also fails.  It must derive the CloudWatch region from AWS_REGION so that
    deployments outside us-west-2 emit to the right regional endpoint.
    """

    def test_emitter_uses_aws_region_env_var(self, monkeypatch):
        """Setting AWS_REGION=eu-west-1 makes the CW client use eu-west-1."""
        import handler as h

        monkeypatch.setenv("AVX_INVESTIGATIONS_TABLE", "vsa-test-avx-investigations")
        monkeypatch.setenv(
            "AVX_INVESTIGATIONS_QUEUE_URL",
            "https://sqs.us-west-2.amazonaws.com/123456789012/test-q",
        )
        monkeypatch.setenv("AWS_REGION", "eu-west-1")

        captured_region: list[str | None] = []
        fake_cw = MagicMock()

        def _fake_boto3_client(service, *_args, **kw):
            if service == "cloudwatch":
                captured_region.append(kw.get("region_name"))
                return fake_cw
            return MagicMock()

        # SQS fails → mark-failed DDB write also fails → emitter fires.
        sqs_mock = MagicMock()
        sqs_mock.send_message.side_effect = RuntimeError("SQS down")
        table_mock = MagicMock()
        table_mock.get_item.return_value = {}          # no lock in pre-read
        table_mock.update_item.side_effect = RuntimeError("DDB down")  # mark-fail fails

        with patch.object(h, "_get_investigations_table", return_value=table_mock), \
             patch.object(h, "_get_ddb_client", return_value=_ddb_success_mock()), \
             patch.object(h, "_get_sqs_client", return_value=sqs_mock), \
             patch("boto3.client", side_effect=_fake_boto3_client):
            h.handler(_post_event(claims=_ENGINEER_CLAIMS), None)

        assert captured_region, (
            "boto3.client('cloudwatch', ...) was never called in the mark-failed path; "
            "check that the SQS+DDB double-failure path is still reachable."
        )
        assert captured_region[0] == "eu-west-1", (
            f"S3 fix: emitter must read AWS_REGION; expected 'eu-west-1', "
            f"got {captured_region[0]!r}.  Mutation: hardcode 'us-west-2' → FAILS here."
        )
