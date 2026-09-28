"""Fix Group 32 + Group 9 tests for agents/supervisor/lambdas/avx_broker.py.

Spec: .kiro/specs/2026-09-18-avx-tier2-ios-cards-and-actions/
      tasks-g7-g9-fragment.md (F32.2, F32.4, F32.5, F9.1, F9.2, F9.3)
      decisions-g7-g9-fragment.md, decisions-scope-1to1-2026-09-24.md

Most tests here run against moto's DynamoDB rather than MagicMock, because the
defects this group closes (a duplicate push after a suppressed conditional
write, a TTL-expired row still demoting, rate budget consumed by a skip) are
about what DynamoDB actually returns. A MagicMock table answers whatever the
test tells it to, so it cannot fail the way the service fails.

SNS stays a MagicMock: it is the output side, and the assertions are about
whether and how often ``publish`` is called.

Mutation results are recorded in
.kiro/specs/2026-09-18-avx-tier2-ios-cards-and-actions/mutations-g7-g9.md
"""
from __future__ import annotations

import ast
import os
import sys
import time
from typing import Any
from unittest.mock import MagicMock, patch

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

_LAMBDAS_DIR = os.path.join(os.path.dirname(__file__), "..")
if _LAMBDAS_DIR not in sys.path:
    sys.path.insert(0, _LAMBDAS_DIR)

import avx_broker as broker  # noqa: E402

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", ".."))
_BROKER_SRC = os.path.join(_REPO_ROOT, "agents", "supervisor", "lambdas", "avx_broker.py")
_WRITER_SRC = os.path.join(_REPO_ROOT, "agents", "tier2", "writer.py")

_OWNER = "0a1b2c3d-4e5f-6789-abcd-ef0123456789"
_ENDPOINT = "arn:aws:sns:us-west-2:123456789012:endpoint/APNS_SANDBOX/avx/0000"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _reset_broker_singletons():
    broker._policy = None
    broker._ddb = None
    broker._ssm = None
    broker._sns = None
    yield
    broker._policy = None
    broker._ddb = None
    broker._ssm = None
    broker._sns = None


def _create_broker_tables(ddb) -> None:
    """Tables with the key schemas and GSIs declared in vsa-tier2-stack.ts."""
    ddb.create_table(
        TableName=broker._ACTIONS_TABLE,
        KeySchema=[{"AttributeName": "action_id", "KeyType": "HASH"}],
        AttributeDefinitions=[
            {"AttributeName": "action_id", "AttributeType": "S"},
            {"AttributeName": "created_by", "AttributeType": "S"},
        ],
        GlobalSecondaryIndexes=[{
            "IndexName": "by_created_by",
            "KeySchema": [{"AttributeName": "created_by", "KeyType": "HASH"}],
            "Projection": {"ProjectionType": "ALL"},
        }],
        BillingMode="PAY_PER_REQUEST",
    )
    for name, pk, sk in (
        (broker._FEEDBACK_TABLE, "owner_id", "feedback_id"),
        (broker._DEVICES_TABLE, "owner_id", "device_token"),
        (broker._RATE_LIMITS_TABLE, "owner_id", "window_key"),
    ):
        ddb.create_table(
            TableName=name,
            KeySchema=[
                {"AttributeName": pk, "KeyType": "HASH"},
                {"AttributeName": sk, "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": pk, "AttributeType": "S"},
                {"AttributeName": sk, "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )


@pytest.fixture()
def aws(monkeypatch):
    """moto DynamoDB with the broker's tables, broker wired to it, SNS mocked."""
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-west-2")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    with mock_aws():
        ddb = boto3.resource("dynamodb", region_name="us-west-2")
        _create_broker_tables(ddb)
        broker._ddb = ddb
        sns = MagicMock()
        broker._sns = sns
        yield ddb, sns


def _policy(threshold: str = "p0") -> dict[str, Any]:
    return {
        "apns_threshold": threshold,
        "rate_limit": {"day": 10, "hour": 3},
        "cardinality_cap": 15,
    }


def _finding(severity: str = "p0", kind: str = "health.tires",
             finding_id: str = "FIND-00000000-0000-0000-0000-00000000aa01",
             owner: str | None = _OWNER, owner_scoped: bool = False) -> dict[str, Any]:
    f: dict[str, Any] = {
        "finding_id": finding_id,
        "finding_kind": kind,
        "severity": severity,
        "headline": "Test finding",
    }
    if owner_scoped:
        # Owner-scoped Finding carrying ONLY scope.owner_id (no top-level projection).
        f["scope"] = {"owner_id": owner}
    else:
        f["scope"] = {"vin": "1HGCM82633A123456"}
        if owner is not None:
            f["owner_id"] = owner
    return f


def _register_device(ddb, consent: dict[str, bool] | None = None,
                     endpoint: str | None = _ENDPOINT, owner: str = _OWNER) -> None:
    item: dict[str, Any] = {
        "owner_id": owner,
        "device_token": "tok-0001",
        "per_category_consent": consent or {"safety": True, "coverage": True, "service": True},
    }
    if endpoint:
        item["endpoint_arn"] = endpoint
    ddb.Table(broker._DEVICES_TABLE).put_item(Item=item)


def _dismissal(ddb, kind: str, ttl_offset_s: int, owner: str = _OWNER,
               feedback_id: str = "fb-0001") -> None:
    ddb.Table(broker._FEEDBACK_TABLE).put_item(Item={
        "owner_id": owner,
        "feedback_id": feedback_id,
        "finding_kind": kind,
        "action_id": "AVX-ACT-00000000-0000-0000-0000-000000000000",
        "dismissed_at": "2026-09-01T00:00:00Z",
        "ttl": int(time.time()) + ttl_offset_s,
    })


def _rate_rows(ddb) -> list[dict]:
    return ddb.Table(broker._RATE_LIMITS_TABLE).scan()["Items"]


def _client_error(code: str = "InternalServerError", op: str = "Query") -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": "boom"}}, op)


# ===========================================================================
# F32.2 — structural guard on Limit= (allowlist under D-SCOPE-1)
# ===========================================================================

# The two device-table reads are correct by construction under the 1:1 device
# model (D-SCOPE-1): register-device keeps at most one row per owner. Every
# other per-owner query in the broker must page, because a Limit on a query
# with a FilterExpression evaluates an arbitrary page (Cycle 3 C5).
_LIMIT_ALLOWLIST = frozenset({"_check_consent", "_send_apns_push"})


def _query_calls_by_function(src_path: str) -> list[tuple[str, bool]]:
    """Return ``(enclosing_function, passes_Limit)`` for every ``.query(`` call."""
    tree = ast.parse(open(src_path).read())
    found: list[tuple[str, bool]] = []
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for node in ast.walk(fn):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "query"):
                has_limit = any(kw.arg == "Limit" for kw in node.keywords)
                # A **kwargs dict could smuggle Limit past the keyword check.
                if any(kw.arg is None for kw in node.keywords):
                    has_limit = has_limit or "Limit" in ast.unparse(fn)
                found.append((fn.name, has_limit))
    # ast.walk visits a nested function's calls once per enclosing def, so a
    # nested call is checked under every enclosing name. That is conservative
    # (it can only add offenders), and no dedupe is attempted.
    return found


class TestLimitStructuralGuard:
    def test_no_limit_on_per_owner_queries_outside_allowlist(self):
        calls = _query_calls_by_function(_BROKER_SRC)
        offenders = sorted({fn for fn, lim in calls if lim and fn not in _LIMIT_ALLOWLIST})
        assert not offenders, (
            f"Limit= passed to .query() in {offenders}. Outside the D-SCOPE-1 "
            "allowlist every per-owner read must page on LastEvaluatedKey "
            "(G7 Cycle 3 C5 / Cycle 4 Critical)."
        )

    def test_allowlist_entries_are_still_real(self):
        """Anti-vacuity 1: a renamed or rewritten allowlisted site fails loudly."""
        calls = _query_calls_by_function(_BROKER_SRC)
        with_limit = {fn for fn, lim in calls if lim}
        stale = sorted(_LIMIT_ALLOWLIST - with_limit)
        assert not stale, (
            f"Allowlisted {stale} no longer contain a .query(Limit=...). Remove "
            "the entry (and revisit D-SCOPE-1) rather than leave a dead exemption."
        )

    def test_guard_checks_at_least_one_non_allowlisted_query(self):
        """Anti-vacuity 2: the guard must have something to bite on."""
        calls = _query_calls_by_function(_BROKER_SRC)
        checked = {fn for fn, _ in calls if fn not in _LIMIT_ALLOWLIST}
        assert checked, "No non-allowlisted .query() call found; the guard is vacuous."
        assert "_check_demotion" in checked and "_check_cardinality" in checked


# ===========================================================================
# F32.4 — notify once per card
# ===========================================================================

class TestNotifyOncePerCard:
    def test_second_delivery_does_not_push_or_consume_rate(self, aws):
        ddb, sns = aws
        _register_device(ddb)
        finding = _finding(severity="p0")

        first = broker._process_finding(finding, _policy())
        second = broker._process_finding(finding, _policy())  # stream retry / nightly MODIFY

        assert first["action"] == "in_app_card_and_apns"
        assert second["skip_reason"] == "already_carded"
        assert sns.publish.call_count == 1, "a redelivery must not push again"
        counts = sorted(int(r["count"]) for r in _rate_rows(ddb))
        assert counts == [1, 1], f"day+hour counters must be 1 each, got {counts}"

    def test_dismissed_card_is_not_resurrected(self, aws):
        ddb, sns = aws
        _register_device(ddb)
        finding = _finding(severity="p0")
        action_id, _, created_by = broker._card_identity(finding, _OWNER)
        ddb.Table(broker._ACTIONS_TABLE).put_item(Item={
            "action_id": action_id, "created_by": created_by, "status": "dismissed",
        })

        result = broker._process_finding(finding, _policy())

        assert result["skip_reason"] == "already_carded"
        sns.publish.assert_not_called()
        row = ddb.Table(broker._ACTIONS_TABLE).get_item(Key={"action_id": action_id})["Item"]
        assert row["status"] == "dismissed"
        assert _rate_rows(ddb) == []

    def test_consent_off_writes_no_rate_counter(self, aws):
        ddb, sns = aws
        _register_device(ddb, consent={"safety": False, "coverage": True, "service": True})

        result = broker._process_finding(_finding(kind="health.tires"), _policy())

        assert result["skip_reason"] == "consent_off"
        assert _rate_rows(ddb) == [], "a skip must never consume rate budget (W4-new)"
        sns.publish.assert_not_called()

    def test_lost_write_race_does_not_push(self, aws):
        """The existence read missed, then the conditional write found the row."""
        ddb, sns = aws
        _register_device(ddb)
        finding = _finding(severity="p0")
        action_id, _, created_by = broker._card_identity(finding, _OWNER)
        ddb.Table(broker._ACTIONS_TABLE).put_item(Item={
            "action_id": action_id, "created_by": created_by, "status": "pending_execution",
        })

        with patch.object(broker, "_card_exists", return_value=False):
            result = broker._process_finding(finding, _policy())

        assert result["skip_reason"] == "duplicate_delivery"
        sns.publish.assert_not_called()

    def test_writer_in_place_update_does_not_set_severity(self):
        """Tripwire for D-G7-2.

        already_carded skips every MODIFY of an open Finding. That loses nothing
        only because the D2 writer's in-place update never rewrites severity,
        so a MODIFY cannot carry an escalation. The day the writer starts
        SETting severity, this fails, and the broker must be widened on purpose
        to re-notify on escalation (it will need the stream's OLD image).
        """
        tree = ast.parse(open(_WRITER_SRC).read())
        literals: list[tuple[str, str]] = []
        for fn in ast.walk(tree):
            if not isinstance(fn, ast.FunctionDef):
                continue
            for node in ast.walk(fn):
                if (isinstance(node, ast.Assign)
                        and any(isinstance(t, ast.Name) and t.id == "update_expr" for t in node.targets)
                        and isinstance(node.value, ast.Constant)
                        and isinstance(node.value.value, str)):
                    literals.append((fn.name, node.value.value))
        in_place = [(fn, s) for fn, s in literals if "SET expires_at" in s]
        assert len(in_place) == 1, (
            f"expected exactly one in-place update_expr literal in writer.py, found {len(in_place)}; "
            "the tripwire cannot see the writer any more — re-point it"
        )
        fn_name, expr = in_place[0]
        fn_src = ast.get_source_segment(open(_WRITER_SRC).read(), next(
            n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == fn_name))
        assert "severity" not in expr and "severity" not in fn_src, (
            f"writer.py:{fn_name} now touches severity in the in-place update. "
            "avx_broker's already_carded skip will silently swallow severity "
            "escalations (e.g. soon -> p0). Widen the broker deliberately, then "
            "update this tripwire."
        )


# ===========================================================================
# F32.5 — batch item failures; errors are not policy outcomes
# ===========================================================================

def _stream_record(seq: str, finding_id: str) -> dict[str, Any]:
    return {
        "eventName": "INSERT",
        "dynamodb": {
            "SequenceNumber": seq,
            "NewImage": {
                "finding_id": {"S": finding_id},
                "finding_kind": {"S": "health.tires"},
                "severity": {"S": "p0"},
                "owner_id": {"S": _OWNER},
                "scope": {"M": {"vin": {"S": "1HGCM82633A123456"}}},
            },
        },
    }


class TestBatchItemFailures:
    def test_failure_on_record_two_reports_its_sequence_number_and_stops(self):
        records = [_stream_record(str(i), f"FIND-{i}") for i in (1, 2, 3)]
        calls: list[str] = []

        def fake_process(finding, policy):
            calls.append(finding["finding_id"])
            if finding["finding_id"] == "FIND-2":
                raise broker.BrokerTransientError("card write failed")
            return {"action": "in_app_card_only"}

        with (
            patch.object(broker, "_get_policy", return_value=_policy()),
            patch.object(broker, "_process_finding", side_effect=fake_process),
        ):
            resp = broker.handler({"Records": records}, None)

        assert resp == {"batchItemFailures": [{"itemIdentifier": "2"}]}
        assert calls == ["FIND-1", "FIND-2"], "records after the failure must not be processed"

    def test_full_success_returns_empty_failure_list(self):
        with (
            patch.object(broker, "_get_policy", return_value=_policy()),
            patch.object(broker, "_process_finding", return_value={"action": "skipped"}),
        ):
            resp = broker.handler({"Records": [_stream_record("9", "FIND-9")]}, None)
        assert resp == {"batchItemFailures": []}

    def test_card_write_error_becomes_batch_failure_end_to_end(self, aws):
        ddb, sns = aws
        _register_device(ddb)
        real_table = ddb.Table

        def table(name):
            t = real_table(name)
            if name == broker._ACTIONS_TABLE:
                wrapper = MagicMock(wraps=t)
                wrapper.put_item.side_effect = _client_error(op="PutItem")
                return wrapper
            return t

        with (
            patch.object(broker, "_get_policy", return_value=_policy()),
            patch.object(ddb, "Table", side_effect=table),
        ):
            resp = broker.handler({"Records": [_stream_record("42", "FIND-42")]}, None)

        assert resp == {"batchItemFailures": [{"itemIdentifier": "42"}]}
        sns.publish.assert_not_called()

    def test_device_read_error_is_transient_not_consent_off(self, aws):
        ddb, _ = aws
        real_table = ddb.Table

        def table(name):
            t = real_table(name)
            if name == broker._DEVICES_TABLE:
                wrapper = MagicMock(wraps=t)
                wrapper.query.side_effect = _client_error()
                return wrapper
            return t

        with patch.object(ddb, "Table", side_effect=table):
            with pytest.raises(broker.BrokerTransientError):
                broker._process_finding(_finding(), _policy())
        assert _rate_rows(ddb) == []

    def test_cardinality_read_error_is_transient_not_cap_exceeded(self, aws):
        ddb, _ = aws
        _register_device(ddb)
        real_table = ddb.Table

        def table(name):
            t = real_table(name)
            if name == broker._ACTIONS_TABLE:
                wrapper = MagicMock(wraps=t)
                wrapper.query.side_effect = _client_error()
                return wrapper
            return t

        with patch.object(ddb, "Table", side_effect=table):
            with pytest.raises(broker.BrokerTransientError):
                broker._process_finding(_finding(), _policy())

    def test_rate_write_error_is_transient_and_rolls_back_day(self, aws):
        ddb, _ = aws
        _register_device(ddb)
        real_table = ddb.Table

        def table(name):
            t = real_table(name)
            if name == broker._RATE_LIMITS_TABLE:
                wrapper = MagicMock(wraps=t)
                original = t.update_item

                def update_item(**kwargs):
                    if kwargs["Key"]["window_key"].startswith("hour:"):
                        raise _client_error(op="UpdateItem")
                    return original(**kwargs)
                wrapper.update_item.side_effect = update_item
                return wrapper
            return t

        with patch.object(ddb, "Table", side_effect=table):
            with pytest.raises(broker.BrokerTransientError):
                broker._process_finding(_finding(), _policy())
        day = [r for r in _rate_rows(ddb) if r["window_key"].startswith("day:")]
        assert [int(r["count"]) for r in day] == [0], "the day increment must be rolled back"

    def test_publish_failure_is_recorded_not_raised(self, aws):
        ddb, sns = aws
        _register_device(ddb)
        sns.publish.side_effect = _client_error(code="EndpointDisabled", op="Publish")

        result = broker._process_finding(_finding(severity="p0"), _policy())

        assert result["action"] == "in_app_card_and_apns"
        assert result["push_outcome"] == "publish_failed"

    def test_missing_endpoint_is_recorded(self, aws):
        ddb, sns = aws
        _register_device(ddb, endpoint=None)
        result = broker._process_finding(_finding(severity="p0"), _policy())
        assert result["push_outcome"] == "endpoint_missing"
        sns.publish.assert_not_called()


# ===========================================================================
# F9.1 / F9.2 — demotion reads the right owner and the real window
# ===========================================================================

class TestDemotionOwnerAndWindow:
    def test_owner_scoped_finding_without_top_level_owner_is_demoted(self, aws):
        ddb, _ = aws
        _dismissal(ddb, kind="health.tires", ttl_offset_s=86400)
        finding = _finding(kind="health.tires", owner_scoped=True)
        assert "owner_id" not in finding

        assert broker._check_demotion(finding) == (True, "demoted")

    def test_ttl_expired_row_does_not_demote(self, aws):
        """DynamoDB TTL deletion is asynchronous: an expired row can still be read."""
        ddb, _ = aws
        _dismissal(ddb, kind="health.tires", ttl_offset_s=-3600)

        assert broker._check_demotion(_finding(kind="health.tires")) == (False, "no_prior_dismissal")

    def test_live_row_of_another_kind_does_not_demote(self, aws):
        ddb, _ = aws
        _dismissal(ddb, kind="health.brakes", ttl_offset_s=86400)
        assert broker._check_demotion(_finding(kind="health.tires")) == (False, "no_prior_dismissal")


# ===========================================================================
# F9.3 — demotion shifts the APNs threshold one step, clamped at p0 (D-G9-1)
# ===========================================================================

class TestDemotionThresholdShift:
    @pytest.mark.parametrize("threshold,demoted,expected", [
        ("p0", False, "p0"),
        ("p0", True, "p0"),                  # clamp: nothing is above p0
        ("attention", True, "p0"),
        ("soon", True, "attention"),
        ("informational", True, "routine"),
        ("attention", False, "attention"),
    ])
    def test_effective_threshold(self, threshold, demoted, expected):
        assert broker._effective_apns_threshold(threshold, demoted) == expected

    @pytest.mark.parametrize("threshold,severity,demoted,pushes", [
        ("attention", "attention", True, False),   # shifted to p0 → in-app only
        ("attention", "p0", True, True),           # p0 still meets the shifted threshold
        ("attention", "attention", False, True),   # control: undemoted pushes
        ("p0", "p0", True, True),                  # staging default: demotion never silences p0
    ])
    def test_push_decision(self, aws, threshold, severity, demoted, pushes):
        ddb, sns = aws
        _register_device(ddb)
        reason = "demoted" if demoted else "no_prior_dismissal"
        with patch.object(broker, "_check_demotion", return_value=(demoted, reason)):
            result = broker._process_finding(_finding(severity=severity), _policy(threshold))

        assert (sns.publish.call_count == 1) is pushes, result
        assert result["action"] == ("in_app_card_and_apns" if pushes else "in_app_card_only")



# ===========================================================================
# Fix Group 33 — review Cycle 5 W1, W2, S4, S5
# ===========================================================================

def _failing_table(ddb, table_name: str, method: str, op: str):
    """Table() replacement whose ``method`` on ``table_name`` raises a ClientError."""
    real_table = ddb.Table

    def table(name):
        t = real_table(name)
        if name == table_name:
            wrapper = MagicMock(wraps=t)
            getattr(wrapper, method).side_effect = _client_error(op=op)
            return wrapper
        return t
    return table


class TestFixGroup33:
    def test_w1_failing_card_write_never_drains_budget_or_skips_to_rate_limit(self, aws):
        """Every retry of a failing card write must stay a batch failure.

        Before the refund, retry 4 at the staging rate-limit {"day":10,"hour":3}
        found the hour counter at 3, returned rate_limit_exceeded with an empty
        batchItemFailures, and the record was checkpointed past the DLQ.
        """
        ddb, sns = aws
        _register_device(ddb)
        record = _stream_record("77", "FIND-77")

        with (
            patch.object(broker, "_get_policy", return_value=_policy()),
            patch.object(ddb, "Table", side_effect=_failing_table(
                ddb, broker._ACTIONS_TABLE, "put_item", "PutItem")),
        ):
            responses = [broker.handler({"Records": [record]}, None) for _ in range(5)]

        assert responses == [{"batchItemFailures": [{"itemIdentifier": "77"}]}] * 5
        counts = sorted(int(r["count"]) for r in _rate_rows(ddb))
        assert counts == [0, 0], f"failed writes must refund both windows, got {counts}"
        sns.publish.assert_not_called()

    def test_w1_lost_race_refunds_budget(self, aws):
        ddb, _ = aws
        _register_device(ddb)
        finding = _finding(severity="p0")
        action_id, _, created_by = broker._card_identity(finding, _OWNER)
        ddb.Table(broker._ACTIONS_TABLE).put_item(Item={
            "action_id": action_id, "created_by": created_by, "status": "pending_execution",
        })
        with patch.object(broker, "_card_exists", return_value=False):
            result = broker._process_finding(finding, _policy())

        assert result["skip_reason"] == "duplicate_delivery"
        assert sorted(int(r["count"]) for r in _rate_rows(ddb)) == [0, 0]

    def test_w2_card_existence_read_error_is_transient(self, aws):
        ddb, sns = aws
        _register_device(ddb)
        with patch.object(ddb, "Table", side_effect=_failing_table(
                ddb, broker._ACTIONS_TABLE, "get_item", "GetItem")):
            with pytest.raises(broker.BrokerTransientError):
                broker._process_finding(_finding(severity="p0"), _policy())
        assert _rate_rows(ddb) == []
        sns.publish.assert_not_called()

    def test_s4_ddb_null_deserialises_to_none(self):
        """Pin: {"NULL": true} is None, not True (the value of the NULL marker)."""
        assert broker._deserialise_value({"NULL": True}) is None

    def test_s5_fractional_evidence_value_still_writes_a_card(self, aws):
        """A non-integer N must deserialise to Decimal; boto3 rejects float at put_item."""
        ddb, _ = aws
        _register_device(ddb)
        record = _stream_record("88", "FIND-88")
        record["dynamodb"]["NewImage"]["evidence"] = {"L": [{"M": {
            "kind": {"S": "tire_pressure"},
            "value": {"M": {"psi": {"N": "28.5"}}},
        }}]}

        with patch.object(broker, "_get_policy", return_value=_policy()):
            resp = broker.handler({"Records": [record]}, None)

        assert resp == {"batchItemFailures": []}
        cards = ddb.Table(broker._ACTIONS_TABLE).scan()["Items"]
        assert len(cards) == 1
        assert str(cards[0]["evidence"][0]["value"]["psi"]) == "28.5"



# ===========================================================================
# Fix Group 34 — review Cycle 6 W1, W2, S2, S3, S4
# ===========================================================================

from decimal import Decimal  # noqa: E402

from botocore.exceptions import ReadTimeoutError  # noqa: E402


def _timeout_table(ddb, table_name: str, method: str, when=None):
    """Table() replacement whose ``method`` raises botocore's ReadTimeoutError.

    ReadTimeoutError is a BotoCoreError, not a ClientError: the transport
    family that Cycle 6 W1 found escaping every ``except ClientError``.
    """
    real_table = ddb.Table

    def table(name):
        t = real_table(name)
        if name != table_name:
            return t
        wrapper = MagicMock(wraps=t)
        original = getattr(t, method)

        def fn(**kwargs):
            if when is None or when(kwargs):
                raise ReadTimeoutError(endpoint_url="https://dynamodb.us-west-2.amazonaws.com")
            return original(**kwargs)
        getattr(wrapper, method).side_effect = fn
        return wrapper
    return table


class TestFixGroup34:
    def test_w1_transport_error_on_card_write_refunds_and_stays_a_batch_failure(self, aws):
        ddb, sns = aws
        _register_device(ddb)
        record = _stream_record("91", "FIND-91")
        with (
            patch.object(broker, "_get_policy", return_value=_policy()),
            patch.object(ddb, "Table", side_effect=_timeout_table(ddb, broker._ACTIONS_TABLE, "put_item")),
        ):
            responses = [broker.handler({"Records": [record]}, None) for _ in range(5)]

        assert responses == [{"batchItemFailures": [{"itemIdentifier": "91"}]}] * 5
        assert sorted(int(r["count"]) for r in _rate_rows(ddb)) == [0, 0]
        sns.publish.assert_not_called()

    def test_w1_transport_error_on_hour_write_rolls_back_day(self, aws):
        ddb, _ = aws
        _register_device(ddb)
        is_hour = lambda kw: kw["Key"]["window_key"].startswith("hour:") and ":one" in kw.get(  # noqa: E731
            "ExpressionAttributeValues", {})
        with patch.object(ddb, "Table", side_effect=_timeout_table(
                ddb, broker._RATE_LIMITS_TABLE, "update_item", when=is_hour)):
            with pytest.raises(broker.BrokerTransientError):
                broker._process_finding(_finding(), _policy())
        day = [int(r["count"]) for r in _rate_rows(ddb) if r["window_key"].startswith("day:")]
        assert day == [0]

    def test_w1_transport_error_on_every_gate_read_is_transient(self, aws):
        ddb, _ = aws
        _register_device(ddb)
        for table_name, method in (
            (broker._ACTIONS_TABLE, "get_item"),   # already_carded
            (broker._ACTIONS_TABLE, "query"),      # cardinality
            (broker._DEVICES_TABLE, "query"),      # consent
        ):
            with patch.object(ddb, "Table", side_effect=_timeout_table(ddb, table_name, method)):
                with pytest.raises(broker.BrokerTransientError):
                    broker._process_finding(_finding(), _policy())
        assert _rate_rows(ddb) == []

    def test_w2_rate_limit_binds_at_the_hour_limit(self, aws):
        """Nothing asserted rate_limit_exceeded before; the refund made that matter."""
        ddb, sns = aws
        _register_device(ddb)
        results = [
            broker._process_finding(
                _finding(severity="p0", finding_id=f"FIND-00000000-0000-0000-0000-0000000000{i:02d}"),
                _policy(),  # {"day": 10, "hour": 3}
            )
            for i in range(6)
        ]
        assert [r["action"] for r in results[:3]] == ["in_app_card_and_apns"] * 3
        assert [r["skip_reason"] for r in results[3:]] == ["rate_limit_exceeded"] * 3
        assert sns.publish.call_count == 3
        counts = {r["window_key"].split(":")[0]: int(r["count"]) for r in _rate_rows(ddb)}
        assert counts == {"day": 3, "hour": 3}, "a refused hour must roll the day back"

    def test_w2_rate_limit_binds_at_the_day_limit(self, aws):
        ddb, _ = aws
        _register_device(ddb)
        policy = _policy()
        policy["rate_limit"] = {"day": 2, "hour": 10}
        results = [
            broker._process_finding(
                _finding(finding_id=f"FIND-00000000-0000-0000-0000-0000000001{i:02d}"), policy)
            for i in range(3)
        ]
        assert [r.get("skip_reason") for r in results] == [None, None, "rate_limit_exceeded"]

    def test_s2_refund_uses_the_increment_instant(self, aws):
        ddb, _ = aws
        _register_device(ddb)
        seen: dict[str, Any] = {}
        real_inc, real_refund = broker._check_and_increment_rate, broker._refund_rate

        def inc(owner, rate_limit, now=None):
            seen["inc"] = now
            return real_inc(owner, rate_limit, now=now)

        def refund(owner, now):
            seen["refund"] = now
            return real_refund(owner, now)

        with (
            patch.object(broker, "_check_and_increment_rate", side_effect=inc),
            patch.object(broker, "_refund_rate", side_effect=refund),
            patch.object(ddb, "Table", side_effect=_timeout_table(ddb, broker._ACTIONS_TABLE, "put_item")),
        ):
            with pytest.raises(broker.BrokerTransientError):
                broker._process_finding(_finding(), _policy())
        assert seen["inc"] is not None and seen["refund"] is seen["inc"]

    def test_s3_fractional_number_is_decimal(self):
        value = broker._deserialise_value({"N": "28.5"})
        assert isinstance(value, Decimal) and value == Decimal("28.5")

    def test_s4_undeserialisable_record_is_a_batch_failure_not_a_crash(self):
        """Decimal % 1 raises InvalidOperation past 28 integer digits.

        Deserialisation used to run outside the handler's try, so one such
        record raised out of the handler and failed the whole batch with no
        itemIdentifier. It now reports that record's SequenceNumber.
        """
        record = _stream_record("99", "FIND-99")
        record["dynamodb"]["NewImage"]["tokens_used"] = {"N": "1" * 40}
        with patch.object(broker, "_get_policy", return_value=_policy()):
            resp = broker.handler({"Records": [record]}, None)
        assert resp == {"batchItemFailures": [{"itemIdentifier": "99"}]}
