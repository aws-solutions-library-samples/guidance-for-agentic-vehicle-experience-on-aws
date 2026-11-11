"""A Finding that worsens to p0 is pushed exactly once; a lost push is counted.

Issues: ``issues/2026-10-23-tier2-writer-frozen-severity`` (design § W1, B1, V1) and
``issues/2026-10-23-lost-p0-push-not-alarmed`` (§ L1).

The end-to-end tests drive the REAL Tier 2 path for a health Finding
(``runner._build_artifact`` -> ``finding_transform.health_artifact_to_finding`` ->
``writer.write_finding``) into a moto findings table, read the persisted item back
as a stream NEW_IMAGE, and hand it to the REAL broker ``handler``. That is the only
way to see the two halves agree: the writer must record the escalation, and the
broker must push it once. SNS stays a MagicMock (the output side).

Mutants run against this file: ``issues/2026-10-23-tier2-writer-frozen-severity/mutants.md``.
"""
from __future__ import annotations

import json
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
_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import avx_broker as broker  # noqa: E402
from agents.health.prompt import C0161_VERBATIM_REMEDY, inject_p0_verbatim_phrases  # noqa: E402
from agents.tier2 import runner as tier2_runner  # noqa: E402
from agents.tier2.finding_transform import health_artifact_to_finding  # noqa: E402
from agents.tier2.owner_resolver import StandingFacts  # noqa: E402
from agents.tier2.writer import write_finding  # noqa: E402

_OWNER = "0a1b2c3d-4e5f-6789-abcd-ef0123456789"
_ENDPOINT = "arn:aws:sns:us-west-2:123456789012:endpoint/APNS_SANDBOX/avx/0000"
_VIN = "1HGCM82633A123456"
_FINDINGS_TABLE = "vsa-test-avx-findings"
_STANDING = StandingFacts(owner_id=_OWNER, fleet_id=None, vehicle_id=None)


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


def _create_tables(ddb_resource) -> None:
    """Broker tables plus the findings table, key schemas as in vsa-tier2-stack.ts."""
    ddb_resource.create_table(
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
        ddb_resource.create_table(
            TableName=name,
            KeySchema=[{"AttributeName": pk, "KeyType": "HASH"},
                       {"AttributeName": sk, "KeyType": "RANGE"}],
            AttributeDefinitions=[{"AttributeName": pk, "AttributeType": "S"},
                                  {"AttributeName": sk, "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
    ddb_resource.create_table(
        TableName=_FINDINGS_TABLE,
        KeySchema=[{"AttributeName": "finding_id", "KeyType": "HASH"}],
        AttributeDefinitions=[
            {"AttributeName": "finding_id", "AttributeType": "S"},
            {"AttributeName": "finding_key", "AttributeType": "S"},
        ],
        GlobalSecondaryIndexes=[{
            "IndexName": "by_finding_key",
            "KeySchema": [{"AttributeName": "finding_key", "KeyType": "HASH"}],
            "Projection": {"ProjectionType": "ALL"},
        }],
        BillingMode="PAY_PER_REQUEST",
    )


@pytest.fixture()
def world(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-west-2")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    with mock_aws():
        ddb = boto3.resource("dynamodb", region_name="us-west-2")
        _create_tables(ddb)
        broker._ddb = ddb
        sns = MagicMock()
        broker._sns = sns
        broker._policy = _policy()
        yield _World(ddb, boto3.client("dynamodb", region_name="us-west-2"), sns)


class _World:
    def __init__(self, ddb, client, sns):
        self.ddb = ddb
        self.client = client
        self.sns = sns
        self._seq = 0

    # -- the real Tier 2 write path ------------------------------------------
    def write(self, severity_hint: str, text: str, rows_tag: str) -> dict[str, Any]:
        """One health write through the production path; returns the writer result."""
        rows = [{"vin": _VIN, "brake_pad_mm": rows_tag}]
        agent_fields = {
            "finding": text,
            "severity_hint": severity_hint,
            "confidence": "high",
            "evidence": [{"source": "adp.brakes.pad", "value": {"tag": rows_tag},
                          "provenance": "live"}],
        }
        artifact, _ = tier2_runner._build_artifact(_VIN, "brakes", rows, agent_fields)
        finding = health_artifact_to_finding(artifact)
        return write_finding(finding, table_name=_FINDINGS_TABLE,
                             dynamodb_client=self.client, standing=_STANDING)

    def item(self, finding_id: str) -> dict[str, Any]:
        """The persisted item in DynamoDB-typed form (what the stream's NEW_IMAGE carries)."""
        return self.client.get_item(TableName=_FINDINGS_TABLE,
                                    Key={"finding_id": {"S": finding_id}})["Item"]

    def deliver(self, finding_id: str, event_name: str) -> dict[str, Any]:
        self._seq += 1
        record = {"eventName": event_name,
                  "dynamodb": {"SequenceNumber": str(self._seq),
                               "NewImage": self.item(finding_id)}}
        return broker.handler({"Records": [record]}, None)

    def ledger(self, finding_id: str) -> dict[str, Any] | None:
        return self.ddb.Table(broker._RATE_LIMITS_TABLE).get_item(
            Key={"owner_id": _OWNER, "window_key": f"p0:{finding_id}"}).get("Item")

    def counters(self) -> list[int]:
        rows = self.ddb.Table(broker._RATE_LIMITS_TABLE).scan()["Items"]
        return sorted(int(r["count"]) for r in rows
                      if str(r["window_key"]).startswith(("day:", "hour:")))

    def register_device(self, consent: dict[str, bool] | None = None,
                        endpoint: str | None = _ENDPOINT) -> None:
        item: dict[str, Any] = {
            "owner_id": _OWNER, "device_token": "tok-0001",
            "per_category_consent": consent or {"safety": True, "coverage": True,
                                                "service": True},
        }
        if endpoint:
            item["endpoint_arn"] = endpoint
        self.ddb.Table(broker._DEVICES_TABLE).put_item(Item=item)

    def card_for(self, finding_id: str) -> dict[str, Any] | None:
        action_id, _, _ = broker._card_identity(
            {"finding_id": finding_id, "finding_kind": "health.brakes"}, _OWNER)
        return self.ddb.Table(broker._ACTIONS_TABLE).get_item(
            Key={"action_id": action_id}).get("Item")


def _policy(threshold: str = "p0", day: int = 10, hour: int = 3) -> dict[str, Any]:
    return {"apns_threshold": threshold, "rate_limit": {"day": day, "hour": hour},
            "cardinality_cap": 15}


_SOON_TEXT = "Front brake pads are wearing faster than expected. Book a service soon."
_P0_TEXT = inject_p0_verbatim_phrases(
    "Brake pressure readings dropped sharply on the last two trips.", ["C0161"])


def _alerts(sns: MagicMock) -> list[str]:
    """The APNs alert text of every publish, parsed from the real message structure."""
    out = []
    for call in sns.publish.call_args_list:
        message = json.loads(call.kwargs["Message"])
        out.append(json.loads(message["APNS"])["aps"]["alert"])
    return out


def _emf_lines(capsys) -> list[dict[str, Any]]:
    lines = []
    for line in capsys.readouterr().out.splitlines():
        line = line.strip()
        if line.startswith("{") and '"_aws"' in line:
            lines.append(json.loads(line))
    return lines


def _client_error(code: str, op: str = "Publish") -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": "boom"}}, op)


# ===========================================================================
# 1. soon -> p0 pushes once, with the directive verbatim (design W1, B1, V1)
# ===========================================================================

class TestEscalationThroughTheRealWriter:
    def test_soon_then_p0_pushes_once_with_the_directive_verbatim(self, world):
        world.register_device()
        first = world.write("soon", _SOON_TEXT, "a")
        assert first["action"] == "put"
        fid = first["finding_id"]
        assert world.deliver(fid, "INSERT") == {"batchItemFailures": []}
        assert world.card_for(fid) is not None, "the soon Finding is carded"
        world.sns.publish.assert_not_called()  # below the p0 push threshold

        second = world.write("p0", _P0_TEXT, "b")
        assert second == {"action": "update", "finding_id": fid}
        stored = world.item(fid)
        assert stored["severity"] == {"S": "p0"}, "the writer must record the escalation"
        assert C0161_VERBATIM_REMEDY in stored["headline"]["S"]
        assert C0161_VERBATIM_REMEDY in stored["detail"]["S"]

        assert world.deliver(fid, "MODIFY") == {"batchItemFailures": []}
        assert world.sns.publish.call_count == 1, "soon -> p0 must push exactly once"
        (alert,) = _alerts(world.sns)
        assert alert == stored["headline"]["S"], "the push carries the card's headline"
        assert C0161_VERBATIM_REMEDY in alert, "the p0 directive must reach the push verbatim"
        assert world.ledger(fid)["state"] == "published"

    def test_repeat_writes_and_redeliveries_do_not_push_again(self, world):
        world.register_device()
        fid = world.write("soon", _SOON_TEXT, "a")["finding_id"]
        world.deliver(fid, "INSERT")
        world.write("p0", _P0_TEXT, "b")
        world.deliver(fid, "MODIFY")
        assert world.sns.publish.call_count == 1

        world.deliver(fid, "MODIFY")                   # stream redelivery
        world.write("p0", _P0_TEXT, "c")               # next night, still p0
        world.deliver(fid, "MODIFY")
        world.write("soon", _SOON_TEXT, "d")           # improves
        assert world.item(fid)["severity"] == {"S": "soon"}, "severity is recorded both ways"
        world.deliver(fid, "MODIFY")
        world.write("p0", _P0_TEXT, "e")               # worsens again
        world.deliver(fid, "MODIFY")

        assert world.sns.publish.call_count == 1, "one p0 push per Finding and owner"
        assert world.counters() == [2, 2], "the soon card and the one p0 push, nothing more"

    def test_first_written_p0_pushes_once_and_later_p0_writes_do_not(self, world):
        world.register_device()
        fid = world.write("p0", _P0_TEXT, "a")["finding_id"]
        world.deliver(fid, "INSERT")
        world.write("p0", _P0_TEXT, "b")
        world.deliver(fid, "MODIFY")
        assert world.sns.publish.call_count == 1
        assert world.ledger(fid)["state"] == "published"

    def test_dismissed_soon_card_does_not_stop_the_p0_push(self, world):
        world.register_device()
        fid = world.write("soon", _SOON_TEXT, "a")["finding_id"]
        world.deliver(fid, "INSERT")
        card = world.card_for(fid)
        world.ddb.Table(broker._ACTIONS_TABLE).update_item(
            Key={"action_id": card["action_id"]},
            UpdateExpression="SET #s = :d", ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":d": "dismissed"})

        world.write("p0", _P0_TEXT, "b")
        world.deliver(fid, "MODIFY")

        assert world.sns.publish.call_count == 1
        assert world.card_for(fid)["status"] == "dismissed", "the card is not resurrected"

    def test_soon_to_attention_stays_notify_once(self, world):
        """An escalation that stays below p0 does not push, even with the threshold at it."""
        world.register_device()
        broker._policy = _policy(threshold="attention")
        fid = world.write("soon", _SOON_TEXT, "a")["finding_id"]
        world.deliver(fid, "INSERT")
        world.write("attention", "Brake wear is now high. Book a service soon.", "b")
        assert world.item(fid)["severity"] == {"S": "attention"}
        world.deliver(fid, "MODIFY")
        world.sns.publish.assert_not_called()


# ===========================================================================
# 2. A lost p0 push is counted and retried (design B1, L1)
# ===========================================================================

class TestLostP0Push:
    def test_failed_p0_push_is_counted_then_retried_by_the_next_write(self, world, capsys):
        world.register_device()
        fid = world.write("soon", _SOON_TEXT, "a")["finding_id"]
        world.deliver(fid, "INSERT")
        capsys.readouterr()

        world.sns.publish.side_effect = _client_error("EndpointDisabled")
        world.write("p0", _P0_TEXT, "b")
        world.deliver(fid, "MODIFY")

        emf = _emf_lines(capsys)
        assert len(emf) == 1
        assert emf[0]["Severity"] == "p0" and emf[0]["reason"] == "publish_failed"
        assert emf[0]["PushNotDelivered"] == 1 and emf[0]["finding_id"] == fid
        assert world.ledger(fid)["state"] == "failed"

        world.sns.publish.side_effect = None
        world.write("p0", _P0_TEXT, "c")
        world.deliver(fid, "MODIFY")
        assert world.sns.publish.call_count == 2, "the failed push is retried once"
        assert world.ledger(fid)["state"] == "published"
        assert _emf_lines(capsys) == []

        world.write("p0", _P0_TEXT, "d")
        world.deliver(fid, "MODIFY")
        assert world.sns.publish.call_count == 2

    @pytest.mark.parametrize("endpoint,reason", [(None, "endpoint_missing")])
    def test_p0_push_without_an_endpoint_is_counted(self, world, capsys, endpoint, reason):
        world.register_device(endpoint=endpoint)
        fid = world.write("p0", _P0_TEXT, "a")["finding_id"]
        world.deliver(fid, "INSERT")
        emf = _emf_lines(capsys)
        assert [(e["Severity"], e["reason"]) for e in emf] == [("p0", reason)]
        assert world.ledger(fid)["state"] == "failed"

    def test_rate_limited_p0_escalation_is_counted_and_claims_nothing(self, world, capsys):
        world.register_device()
        fid = world.write("soon", _SOON_TEXT, "a")["finding_id"]
        world.deliver(fid, "INSERT")
        broker._policy = _policy(hour=1)  # the soon card's write spent this hour's unit
        capsys.readouterr()

        world.write("p0", _P0_TEXT, "b")
        world.deliver(fid, "MODIFY")

        world.sns.publish.assert_not_called()
        emf = _emf_lines(capsys)
        assert [(e["Severity"], e["reason"]) for e in emf] == [("p0", "rate_limit_exceeded")]
        assert world.ledger(fid) is None, "no claim, so the next write tries again"

        broker._policy = _policy()
        world.write("p0", _P0_TEXT, "c")
        world.deliver(fid, "MODIFY")
        assert world.sns.publish.call_count == 1

    def test_consent_off_p0_escalation_is_not_a_lost_push(self, world, capsys):
        world.register_device()
        fid = world.write("soon", _SOON_TEXT, "a")["finding_id"]
        world.deliver(fid, "INSERT")
        world.register_device(consent={"safety": False, "coverage": True, "service": True})
        counters_before = world.counters()
        capsys.readouterr()

        world.write("p0", _P0_TEXT, "b")
        world.deliver(fid, "MODIFY")

        world.sns.publish.assert_not_called()
        assert _emf_lines(capsys) == []
        assert world.ledger(fid) is None
        assert world.counters() == counters_before

    def test_non_p0_push_failure_is_counted_under_its_own_severity(self, world, capsys):
        world.register_device()
        broker._policy = _policy(threshold="soon")
        world.sns.publish.side_effect = _client_error("EndpointDisabled")
        fid = world.write("soon", _SOON_TEXT, "a")["finding_id"]
        world.deliver(fid, "INSERT")
        emf = _emf_lines(capsys)
        assert [(e["Severity"], e["reason"]) for e in emf] == [("soon", "publish_failed")]
        assert world.ledger(fid) is None, "only p0 pushes are tracked in the ledger"

    def test_cap_blocked_due_p0_is_counted_only_when_it_would_have_reached(self, world, capsys):
        broker._policy = {**_policy(), "cardinality_cap": 0}
        world.register_device()
        fid = world.write("p0", _P0_TEXT, "a")["finding_id"]
        world.deliver(fid, "INSERT")
        assert [(e["Severity"], e["reason"]) for e in _emf_lines(capsys)] == [
            ("p0", "cardinality_cap_exceeded")]

        world.register_device(consent={"safety": False, "coverage": True, "service": True})
        world.write("p0", _P0_TEXT, "b")
        world.deliver(fid, "MODIFY")
        assert _emf_lines(capsys) == [], "consent off is the owner's choice, not a lost push"

    def test_base_path_consent_off_is_not_counted(self, world, capsys):
        world.register_device(consent={"safety": False, "coverage": True, "service": True})
        fid = world.write("p0", _P0_TEXT, "a")["finding_id"]
        world.deliver(fid, "INSERT")
        assert _emf_lines(capsys) == []

    def test_cap_blocked_below_threshold_finding_is_not_counted(self, world, capsys):
        """Review cycle 1 RV-3: a Finding below the push threshold had no push due."""
        broker._policy = {**_policy(), "cardinality_cap": 0}
        world.register_device()
        fid = world.write("soon", _SOON_TEXT, "a")["finding_id"]
        world.deliver(fid, "INSERT")
        assert _emf_lines(capsys) == []


# ===========================================================================
# 3. Ledger claim states and failure handling (design B1)
# ===========================================================================

def _carded_p0(world) -> str:
    world.register_device()
    fid = world.write("soon", _SOON_TEXT, "a")["finding_id"]
    world.deliver(fid, "INSERT")
    world.write("p0", _P0_TEXT, "b")
    return fid


class TestLedgerClaim:
    @pytest.mark.parametrize("age", [0, 10, 30, 90, 600])
    def test_claimed_row_of_any_age_is_reclaimed_and_pushed(self, world, capsys, age):
        """Review cycle 2 W1/S1: a ``claimed`` row is a delivery that is not running.

        No two deliveries of one Finding run at once (one mapping, per-key order) and
        the broker is the only writer of ``claimed``/``failed``, so the row is either an
        earlier delivery that died between claim and outcome or this delivery's own
        write retried by botocore.
        Either way the push goes out (at-least-once), whatever the row's age. The
        cycle 1 design refused a row younger than 60 s and paged ``claim_held``
        instead of pushing.
        """
        fid = _carded_p0(world)
        before = world.counters()
        world.ddb.Table(broker._RATE_LIMITS_TABLE).put_item(Item={
            "owner_id": _OWNER, "window_key": f"p0:{fid}", "state": "claimed",
            "claimed_at": int(time.time()) - age, "ttl": int(time.time()) + 86400})
        capsys.readouterr()

        world.deliver(fid, "MODIFY")

        assert world.sns.publish.call_count == 1
        assert world.ledger(fid)["state"] == "published"
        assert _emf_lines(capsys) == [], "a delivered push is not counted as lost"
        assert world.counters() == [c + 1 for c in before], "the push spends one unit"

    def test_committed_card_write_retried_into_a_conflict_still_pushes_a_p0(self, world):
        """Review cycle 2 W1(c): the Actions PutItem commits, its reply is lost, and
        botocore's retry of the same conditional write fails ConditionalCheckFailed.

        The first-written p0 must still push exactly once, record ``published`` and
        spend one unit of each window.
        """
        world.register_device()
        fid = world.write("p0", _P0_TEXT, "a")["finding_id"]
        real_table = world.ddb.Table

        class _Ddb:
            def Table(self, name):  # noqa: N802
                table = real_table(name)
                if name != broker._ACTIONS_TABLE:
                    return table

                def put_item(**kw):
                    table.put_item(**kw)  # first attempt commits
                    raise _client_error("ConditionalCheckFailedException", "PutItem")

                proxy = MagicMock(wraps=table)
                proxy.put_item.side_effect = put_item
                return proxy

        broker._ddb = _Ddb()
        assert world.deliver(fid, "INSERT") == {"batchItemFailures": []}

        assert world.card_for(fid) is not None
        assert world.sns.publish.call_count == 1
        assert world.ledger(fid)["state"] == "published"
        assert world.counters() == [1, 1]

    def test_escalation_claim_refused_by_a_published_row_refunds(self, world):
        """The ledger read and the claim are two calls. If the row says ``published`` by
        the claim (mutant R8 at 81e2c581), the claim refuses, nothing is pushed, the
        result is ``duplicate_delivery`` and the rate unit goes back."""
        fid = _carded_p0(world)
        before = world.counters()
        world.ddb.Table(broker._RATE_LIMITS_TABLE).put_item(Item={
            "owner_id": _OWNER, "window_key": f"p0:{fid}", "state": "published",
            "claimed_at": int(time.time()), "ttl": int(time.time()) + 86400})
        finding = broker._deserialise_ddb_item(world.item(fid))
        with patch.object(broker, "_p0_ledger_state", return_value=None):
            result = broker._process_finding(finding, broker._policy)
        assert result["skip_reason"] == "duplicate_delivery"
        assert result["escalation"] is True
        world.sns.publish.assert_not_called()
        assert world.counters() == before, "the refused claim's rate unit is refunded"

    def test_committed_claim_retried_by_botocore_is_admitted(self, world):
        """The claim's own retry after its first attempt committed must not be refused."""
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)
        finding = {"finding_id": "FIND-retry", "expires_at": "2099-01-01T00:00:00Z"}
        assert broker._claim_p0_ledger(_OWNER, finding, now) is True
        assert broker._claim_p0_ledger(_OWNER, finding, now) is True

    def test_ledger_row_outlives_the_finding(self, world):
        fid = _carded_p0(world)
        world.deliver(fid, "MODIFY")
        expires = world.item(fid)["expires_at"]["S"]
        from datetime import datetime, timedelta
        expires_epoch = datetime.fromisoformat(expires.replace("Z", "+00:00")).timestamp()
        ttl = int(world.ledger(fid)["ttl"])
        assert ttl == int(expires_epoch + timedelta(days=30).total_seconds())

    def test_ledger_read_error_is_a_batch_failure_not_a_push(self, world):
        fid = _carded_p0(world)
        real_table = world.ddb.Table

        class _Ddb:
            def Table(self, name):  # noqa: N802
                table = real_table(name)
                if name != broker._RATE_LIMITS_TABLE:
                    return table
                proxy = MagicMock(wraps=table)
                proxy.get_item.side_effect = _client_error("InternalServerError", "GetItem")
                return proxy

        broker._ddb = _Ddb()
        result = world.deliver(fid, "MODIFY")
        assert result["batchItemFailures"], "an unreadable ledger is not an absent one"
        world.sns.publish.assert_not_called()

    def test_claim_error_on_escalation_raises_refunds_and_does_not_push(self, world):
        fid = _carded_p0(world)
        before = world.counters()
        real_table = world.ddb.Table

        class _Ddb:
            def Table(self, name):  # noqa: N802
                table = real_table(name)
                if name != broker._RATE_LIMITS_TABLE:
                    return table

                def update_item(**kw):
                    if str(kw["Key"]["window_key"]).startswith("p0:"):
                        raise _client_error("InternalServerError", "UpdateItem")
                    return table.update_item(**kw)

                proxy = MagicMock(wraps=table)
                proxy.update_item.side_effect = update_item
                return proxy

        broker._ddb = _Ddb()
        result = world.deliver(fid, "MODIFY")
        assert result["batchItemFailures"]
        world.sns.publish.assert_not_called()
        broker._ddb = world.ddb
        assert world.counters() == before, "the rate unit is refunded before the retry"

        world.deliver(fid, "MODIFY")  # the retry
        assert world.sns.publish.call_count == 1

    def test_base_path_p0_claim_error_raises_then_the_retry_pushes_once(self, world):
        world.register_device()
        fid = world.write("p0", _P0_TEXT, "a")["finding_id"]
        real_table = world.ddb.Table
        state = {"fail": True}

        class _Ddb:
            def Table(self, name):  # noqa: N802
                table = real_table(name)
                if name != broker._RATE_LIMITS_TABLE:
                    return table

                def update_item(**kw):
                    if state["fail"] and str(kw["Key"]["window_key"]).startswith("p0:"):
                        raise _client_error("InternalServerError", "UpdateItem")
                    return table.update_item(**kw)

                proxy = MagicMock(wraps=table)
                proxy.update_item.side_effect = update_item
                return proxy

        broker._ddb = _Ddb()
        assert world.deliver(fid, "INSERT")["batchItemFailures"]
        world.sns.publish.assert_not_called()
        assert world.card_for(fid) is not None, "the card was written before the claim"
        assert world.counters() == [0, 0], "the card's unit is refunded; the retry spends it"

        state["fail"] = False
        broker._ddb = world.ddb
        assert world.deliver(fid, "INSERT") == {"batchItemFailures": []}
        assert world.sns.publish.call_count == 1
        assert world.counters() == [1, 1]

        world.deliver(fid, "INSERT")
        assert world.sns.publish.call_count == 1


# ===========================================================================
# 3b. The two guards against a repeat push, each on its own (design B1)
# ===========================================================================
#
# "A repeat write does not push again" is held twice: the ledger READ stops a
# published Finding before any gate, and the ledger CLAIM refuses a published row.
# Breaking either alone leaves the behaviour intact, so each is pinned directly.

class TestRepeatGuardsSeparately:
    def test_published_ledger_stops_before_any_gate_or_claim(self, world):
        fid = _carded_p0(world)
        world.ddb.Table(broker._RATE_LIMITS_TABLE).put_item(Item={
            "owner_id": _OWNER, "window_key": f"p0:{fid}", "state": "published",
            "claimed_at": int(time.time()) - 86400, "ttl": int(time.time()) + 86400})
        finding = broker._deserialise_ddb_item(world.item(fid))
        with patch.object(broker, "_check_consent") as consent, \
                patch.object(broker, "_check_and_increment_rate") as rate, \
                patch.object(broker, "_claim_p0_ledger") as claim:
            result = broker._process_finding(finding, broker._policy)
        assert result["skip_reason"] == "already_carded"
        consent.assert_not_called()
        rate.assert_not_called()
        claim.assert_not_called()

    @pytest.mark.parametrize("row,admitted", [
        (None, True),
        ({"state": "failed", "claimed_at": 0}, True),
        ({"state": "claimed", "claimed_at": -600}, True),   # a delivery that died
        ({"state": "claimed", "claimed_at": 0}, True),      # fresh: this delivery's retry
        ({"state": "published", "claimed_at": -86400}, False),
        ({"state": "published"}, False),                    # no claimed_at
        # Malformed rows are admitted, matching _p0_ledger_state (review cycle 1 S6).
        ({"claimed_at": 0}, True),                          # no state
        ({"state": "claimed"}, True),                       # no claimed_at
        ({"state": "claimed", "claimed_at": "soon"}, True),  # non-numeric claimed_at
        ({"state": "weird", "claimed_at": 0}, True),        # unknown state
    ])
    def test_claim_refuses_only_a_published_row(self, world, row, admitted):
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)
        finding = {"finding_id": "FIND-claim", "expires_at": "2099-01-01T00:00:00Z"}
        if row is not None:
            item = {"owner_id": _OWNER, "window_key": "p0:FIND-claim", "ttl": 1}
            if "state" in row:
                item["state"] = row["state"]
            if "claimed_at" in row:
                ca = row["claimed_at"]
                item["claimed_at"] = int(now.timestamp()) + ca if isinstance(ca, int) else ca
            world.ddb.Table(broker._RATE_LIMITS_TABLE).put_item(Item=item)
        assert broker._claim_p0_ledger(_OWNER, finding, now) is admitted

    def test_claim_condition_is_pinned_whole(self):
        """moto evaluates ``#st <> :published`` as true on a missing ``state``, so the
        behaviour test above cannot see ``attribute_not_exists(#st)`` removed (mutant
        F-S6a). Real DynamoDB need not agree, so the expression is pinned as written.
        """
        assert broker._P0_CLAIM_CONDITION == (
            "attribute_not_exists(window_key) OR attribute_not_exists(#st) "
            "OR #st <> :published")

    @pytest.mark.parametrize("state,claimed_offset,expected", [
        ("published", -86400, "published"),
        ("failed", 0, "failed"),
        ("claimed", 0, "claimed"),
        ("claimed", -600, "claimed"),
        (7, 0, "failed"),                                   # non-string state
    ])
    def test_ledger_read_returns_the_stored_state(self, world, state, claimed_offset,
                                                  expected):
        world.ddb.Table(broker._RATE_LIMITS_TABLE).put_item(Item={
            "owner_id": _OWNER, "window_key": "p0:FIND-read", "state": state,
            "claimed_at": int(time.time()) + claimed_offset, "ttl": 1})
        assert broker._p0_ledger_state(_OWNER, "FIND-read") == expected
        assert broker._p0_ledger_state(_OWNER, "FIND-absent") is None


# ===========================================================================
# 4. The writer records severity changes even when the inputs did not change (W1)
# ===========================================================================

class TestWriterSeverityWithUnchangedInputs:
    def test_same_inputs_new_severity_updates_in_place(self, world):
        fid = world.write("soon", _SOON_TEXT, "a")["finding_id"]
        result = world.write("p0", _P0_TEXT, "a")  # same rows -> same inputs_hash
        assert result == {"action": "update", "finding_id": fid}
        stored = world.item(fid)
        assert stored["severity"] == {"S": "p0"}
        notes = [e["M"].get("note", {}).get("S") for e in stored["audit_trail"]["L"]]
        assert "severity soon -> p0" in notes

    def test_same_inputs_same_severity_is_still_a_noop(self, world):
        fid = world.write("soon", _SOON_TEXT, "a")["finding_id"]
        assert world.write("soon", _SOON_TEXT, "a") == {"action": "noop", "finding_id": fid}

    def test_refresh_on_noop_with_new_severity_updates_rather_than_refreshes(self, world):
        rows = [{"vin": _VIN, "brake_pad_mm": "a"}]
        written = []
        for hint in ("soon", "attention"):
            fields = {"finding": "Brake wear.", "severity_hint": hint, "confidence": "high",
                      "evidence": [{"source": "adp.brakes.pad", "value": {"tag": "a"},
                                    "provenance": "live"}]}
            artifact, _ = tier2_runner._build_artifact(_VIN, "brakes", rows, fields)
            written.append(write_finding(health_artifact_to_finding(artifact),
                                         table_name=_FINDINGS_TABLE,
                                         dynamodb_client=world.client,
                                         standing=_STANDING, refresh_on_noop=True))
        assert written[1]["action"] == "update"
        assert world.item(written[0]["finding_id"])["severity"] == {"S": "attention"}


# ===========================================================================
# 5. The EMF line is what the alarm reads (L1)
# ===========================================================================

def test_push_not_delivered_emf_shape(capsys):
    broker._emit_push_not_delivered("p0", "publish_failed", "FIND-x")
    (line,) = _emf_lines(capsys)
    (directive,) = line["_aws"]["CloudWatchMetrics"]
    assert directive["Namespace"] == "AVX/Broker"
    assert directive["Dimensions"] == [["Stage", "Severity"]]
    assert directive["Metrics"] == [{"Name": "PushNotDelivered", "Unit": "Count"}]
    assert line["Stage"] == broker._STAGE and line["Severity"] == "p0"
    assert line["PushNotDelivered"] == 1
    assert isinstance(line["_aws"]["Timestamp"], int)


def test_push_not_delivered_never_raises(capsys):
    with patch.object(broker.sys, "stdout") as out:
        out.write.side_effect = OSError("closed")
        broker._emit_push_not_delivered("p0", "publish_failed", "FIND-x")


def test_unknown_severity_is_bucketed_not_a_new_dimension(capsys):
    broker._emit_push_not_delivered("weird", "publish_failed", "FIND-x")
    (line,) = _emf_lines(capsys)
    assert line["Severity"] == "unknown"


# ===========================================================================
# 6. Review cycle 1 fixes (W1, W3, S7, S8, C1(a))
# ===========================================================================

class TestReviewCycle1:
    def test_a_same_input_write_does_not_retry_a_failed_p0(self, world, capsys):
        """W1, the documented limit: the retry needs a write that reaches the stream.

        A nightly run with unchanged inputs is a writer no-op, so a failed p0 stays
        failed until the Finding changes. This pins the behaviour the broker
        docstring, the issue report and the alarm description now state.
        """
        world.register_device()
        fid = world.write("soon", _SOON_TEXT, "a")["finding_id"]
        world.deliver(fid, "INSERT")
        world.sns.publish.side_effect = _client_error("EndpointDisabled")
        world.write("p0", _P0_TEXT, "b")
        world.deliver(fid, "MODIFY")
        assert world.ledger(fid)["state"] == "failed"
        # Backdate the live row so a writer that refreshed p0 rows on the no-op path
        # would have something to extend (review cycle 2 F-W1): the same-input write must
        # still be a no-op, i.e. no stream record and no retry.
        from datetime import datetime, timedelta, timezone
        soon = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(
            timespec="seconds").replace("+00:00", "Z")
        world.client.update_item(TableName=_FINDINGS_TABLE, Key={"finding_id": {"S": fid}},
                                 UpdateExpression="SET expires_at = :e",
                                 ExpressionAttributeValues={":e": {"S": soon}})

        assert world.write("p0", _P0_TEXT, "b") == {"action": "noop", "finding_id": fid}

    def test_base_path_rate_limited_p0_is_counted(self, world, capsys):
        """W3: a first-written p0 blocked by the rate limit pages the operator."""
        world.register_device()
        broker._policy = _policy(hour=1)
        other = world.write("soon", _SOON_TEXT, "a")["finding_id"]
        world.deliver(other, "INSERT")  # spends the hour's one unit
        capsys.readouterr()

        rows = [{"vin": "1HGCM82633A654321", "brake_pad_mm": "x"}]
        fields = {"finding": _P0_TEXT, "severity_hint": "p0", "confidence": "high",
                  "evidence": [{"source": "adp.brakes.pad", "value": {"tag": "x"},
                                "provenance": "live"}]}
        artifact, _ = tier2_runner._build_artifact("1HGCM82633A654321", "brakes", rows, fields)
        fid = write_finding(health_artifact_to_finding(artifact), table_name=_FINDINGS_TABLE,
                            dynamodb_client=world.client, standing=_STANDING)["finding_id"]
        world.deliver(fid, "INSERT")

        world.sns.publish.assert_not_called()
        assert [(e["Severity"], e["reason"]) for e in _emf_lines(capsys)] == [
            ("p0", "rate_limit_exceeded")]
        assert world.card_for(fid) is None

    def test_cap_path_consent_read_error_counts_the_p0(self, world, capsys):
        """W3: an unreadable consent row on the cap path counts the lost p0."""
        world.register_device()
        broker._policy = {**_policy(), "cardinality_cap": 0}
        fid = world.write("p0", _P0_TEXT, "a")["finding_id"]
        finding = broker._deserialise_ddb_item(world.item(fid))
        capsys.readouterr()

        with patch.object(broker, "_check_consent",
                          side_effect=broker.BrokerTransientError("consent read failed")):
            result = broker._process_finding(finding, broker._policy)

        assert result["skip_reason"] == "cardinality_cap_exceeded"
        assert [(e["Severity"], e["reason"]) for e in _emf_lines(capsys)] == [
            ("p0", "cardinality_cap_exceeded")]

    def test_base_path_refused_claim_reports_no_push(self, world):
        """S7: a p0 card written while a published ledger row exists pushes nothing."""
        world.register_device()
        fid = world.write("p0", _P0_TEXT, "a")["finding_id"]
        world.ddb.Table(broker._RATE_LIMITS_TABLE).put_item(Item={
            "owner_id": _OWNER, "window_key": f"p0:{fid}", "state": "published",
            "claimed_at": int(time.time()) - 86400, "ttl": int(time.time()) + 86400})
        finding = broker._deserialise_ddb_item(world.item(fid))

        result = broker._process_finding(finding, broker._policy)

        assert result["action"] == "in_app_card_only"
        assert result["push_outcome"] == "already_notified"
        world.sns.publish.assert_not_called()

    def test_rate_limit_counts_by_the_demoted_threshold(self, world, capsys):
        """S8: a soon push that demotion would have suppressed is not a lost push."""
        world.register_device()
        broker._policy = _policy(threshold="soon", hour=1)
        first = world.write("soon", _SOON_TEXT, "a")["finding_id"]
        world.deliver(first, "INSERT")  # spends the hour's unit (and pushes: threshold soon)
        world.ddb.Table(broker._FEEDBACK_TABLE).put_item(Item={
            "owner_id": _OWNER, "feedback_id": "fb-1", "finding_kind": "health.tires",
            "action_id": "AVX-ACT-00000000-0000-0000-0000-000000000000",
            "dismissed_at": "2026-09-01T00:00:00Z", "ttl": int(time.time()) + 86400})
        capsys.readouterr()

        def _tire(vin: str) -> str:
            rows = [{"vin": vin, "tread": "t"}]
            fields = {"finding": "Tire tread is low. Book a check soon.", "severity_hint": "soon",
                      "confidence": "high", "evidence": [{"source": "adp.tires", "value": {},
                                                          "provenance": "live"}]}
            artifact, _ = tier2_runner._build_artifact(vin, "tires", rows, fields)
            return write_finding(health_artifact_to_finding(artifact), table_name=_FINDINGS_TABLE,
                                 dynamodb_client=world.client, standing=_STANDING)["finding_id"]

        demoted = _tire("1HGCM82633A000001")  # health.tires has a dismissal: demoted
        world.deliver(demoted, "INSERT")
        assert _emf_lines(capsys) == [], "demoted below its push threshold: not lost"

        world.ddb.Table(broker._FEEDBACK_TABLE).delete_item(
            Key={"owner_id": _OWNER, "feedback_id": "fb-1"})
        not_demoted = _tire("1HGCM82633A000002")
        world.deliver(not_demoted, "INSERT")
        assert [(e["Severity"], e["reason"]) for e in _emf_lines(capsys)] == [
            ("soon", "rate_limit_exceeded")]


def test_broker_clients_are_bounded_inside_the_lambda_timeout(monkeypatch):
    """C1(a): no client call can outlive the broker's 30 s timeout on botocore defaults."""
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-west-2")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    broker._sns = None
    broker._ddb = None
    sns_cfg = broker._get_sns().meta.config
    ddb_cfg = broker._get_ddb().meta.client.meta.config
    for cfg, attempts in ((sns_cfg, 2), (ddb_cfg, 3)):
        assert cfg.connect_timeout == 3 and cfg.read_timeout == 5
        assert cfg.retries["total_max_attempts"] == attempts
        # Worst case per CALL stays well inside 30 s. Per invocation (up to 10
        # records) it need not; that case is a stream retry that reclaims, not a
        # lost push (review cycle 2 S2, the _SNS_CONFIG comment).
        assert attempts * (cfg.connect_timeout + cfg.read_timeout) <= 24


# ===========================================================================
# 7. Review cycle 3 fixes (S2, S3, S4)
# ===========================================================================

def _conflicting_card_writes(world):
    """A DynamoDB resource whose Actions PutItem commits, then raises ConditionalCheckFailed
    (botocore's retry of a committed conditional write, review cycle 2 W1)."""
    real_table = world.ddb.Table

    class _Ddb:
        def Table(self, name):  # noqa: N802
            table = real_table(name)
            if name != broker._ACTIONS_TABLE:
                return table

            def put_item(**kw):
                table.put_item(**kw)
                raise _client_error("ConditionalCheckFailedException", "PutItem")

            proxy = MagicMock(wraps=table)
            proxy.put_item.side_effect = put_item
            return proxy
    return _Ddb()


class TestReviewCycle3:
    def test_the_claim_sends_the_pinned_condition(self, world):
        """S2: the whole-expression pin reads the constant; this reads what the claim sends,
        so a call site that drops ``attribute_not_exists(#st)`` fails here (mutant RC3-2)."""
        from datetime import datetime, timezone
        real_table = world.ddb.Table
        sent = []

        class _Ddb:
            def Table(self, name):  # noqa: N802
                table = real_table(name)
                if name != broker._RATE_LIMITS_TABLE:
                    return table
                proxy = MagicMock(wraps=table)

                def update_item(**kw):
                    sent.append(kw)
                    return table.update_item(**kw)
                proxy.update_item.side_effect = update_item
                return proxy

        broker._ddb = _Ddb()
        finding = {"finding_id": "FIND-sent", "expires_at": "2099-01-01T00:00:00Z"}
        assert broker._claim_p0_ledger(_OWNER, finding, datetime.now(timezone.utc)) is True
        (kw,) = sent
        assert kw["ConditionExpression"] == broker._P0_CLAIM_CONDITION
        assert "attribute_not_exists(#st)" in kw["ConditionExpression"]
        assert kw["ExpressionAttributeValues"][":published"] == "published"

    def test_conflicted_p0_whose_push_fails_is_counted_and_keeps_its_unit(self, world, capsys):
        """S3: the W1 path with a failing publish (mutant RC3-3)."""
        world.register_device()
        fid = world.write("p0", _P0_TEXT, "a")["finding_id"]
        world.sns.publish.side_effect = _client_error("EndpointDisabled")
        broker._ddb = _conflicting_card_writes(world)
        capsys.readouterr()

        result = broker._process_finding(broker._deserialise_ddb_item(world.item(fid)),
                                         broker._policy)

        broker._ddb = world.ddb
        assert result["action"] == "in_app_card_and_apns"
        assert result["push_outcome"] == "publish_failed"
        assert [(e["Severity"], e["reason"]) for e in _emf_lines(capsys)] == [
            ("p0", "publish_failed")]
        assert world.counters() == [1, 1], "the attempted push keeps its unit"
        assert world.ledger(fid)["state"] == "failed"

    def test_below_p0_conflict_counts_a_due_push(self, world, capsys):
        """S4: below p0 a conflict is still notify-once, but a due push is not silent."""
        world.register_device()
        broker._policy = _policy(threshold="soon")
        fid = world.write("soon", _SOON_TEXT, "a")["finding_id"]
        broker._ddb = _conflicting_card_writes(world)
        capsys.readouterr()

        result = broker._process_finding(broker._deserialise_ddb_item(world.item(fid)),
                                         broker._policy)

        broker._ddb = world.ddb
        assert result["skip_reason"] == "duplicate_delivery"
        world.sns.publish.assert_not_called()
        assert [(e["Severity"], e["reason"]) for e in _emf_lines(capsys)] == [
            ("soon", "card_write_conflict")]
        assert world.counters() == [0, 0]

    def test_below_p0_conflict_with_no_due_push_is_not_counted(self, world, capsys):
        world.register_device()
        fid = world.write("soon", _SOON_TEXT, "a")["finding_id"]  # threshold stays p0
        broker._ddb = _conflicting_card_writes(world)
        capsys.readouterr()

        result = broker._process_finding(broker._deserialise_ddb_item(world.item(fid)),
                                         broker._policy)

        broker._ddb = world.ddb
        assert result["skip_reason"] == "duplicate_delivery"
        assert _emf_lines(capsys) == []


# ===========================================================================
# 8. Security review cycle 1 fixes (S1, SRC1-8)
# ===========================================================================

class TestSecurityCycle1:
    def test_a_failed_outcome_never_reverts_a_published_row(self, world):
        """S1: a failed delivery beside a successful one (the mapping-replacement window)
        must not reopen the Finding for another push."""
        world.ddb.Table(broker._RATE_LIMITS_TABLE).put_item(Item={
            "owner_id": _OWNER, "window_key": "p0:FIND-rev", "state": "published",
            "outcome": "published", "ttl": int(time.time()) + 86400})
        broker._record_p0_outcome(_OWNER, "FIND-rev", "publish_failed")
        row = world.ledger("FIND-rev")
        assert row["state"] == "published" and row["outcome"] == "published"

    @pytest.mark.parametrize("prior", [None, "claimed", "failed"])
    def test_a_failed_outcome_is_recorded_over_any_other_row(self, world, prior):
        if prior:
            world.ddb.Table(broker._RATE_LIMITS_TABLE).put_item(Item={
                "owner_id": _OWNER, "window_key": "p0:FIND-out", "state": prior,
                "ttl": int(time.time()) + 86400})
        broker._record_p0_outcome(_OWNER, "FIND-out", "publish_failed")
        assert world.ledger("FIND-out")["state"] == "failed"

    def test_a_published_outcome_is_recorded_over_a_published_row(self, world):
        world.ddb.Table(broker._RATE_LIMITS_TABLE).put_item(Item={
            "owner_id": _OWNER, "window_key": "p0:FIND-pp", "state": "published",
            "outcome": "x", "ttl": int(time.time()) + 86400})
        broker._record_p0_outcome(_OWNER, "FIND-pp", "published")
        assert world.ledger("FIND-pp")["outcome"] == "published"

    def test_the_ledger_read_is_strongly_consistent(self, world):
        """SRC1-8: moto does not model eventual consistency, so the kwarg is pinned at the
        call: a stream retry just after a publish must see ``published``."""
        real_table = world.ddb.Table
        sent = []

        class _Ddb:
            def Table(self, name):  # noqa: N802
                table = real_table(name)
                if name != broker._RATE_LIMITS_TABLE:
                    return table
                proxy = MagicMock(wraps=table)

                def get_item(**kw):
                    sent.append(kw)
                    return table.get_item(**kw)
                proxy.get_item.side_effect = get_item
                return proxy

        broker._ddb = _Ddb()
        assert broker._p0_ledger_state(_OWNER, "FIND-cr") is None
        (kw,) = sent
        assert kw["ConsistentRead"] is True
