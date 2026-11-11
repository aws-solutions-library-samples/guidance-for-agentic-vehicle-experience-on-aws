"""Tests for scripts/live-verify-p0-escalation.py (gates, guard, manifest, cleanup, residue).

The live check itself runs only against staging after the combined tier2 deploy; these
tests pin what must hold before anyone runs it: it refuses without its flags, it can only
touch its own rows, it records what it writes before writing, and it cleans up and checks
for residue on every exit.
"""
from __future__ import annotations

import importlib.util
import io
import json
import os
import signal
import stat
import subprocess
import sys
import zipfile
from pathlib import Path
from unittest.mock import MagicMock

import boto3
import pytest
from moto import mock_aws

_REPO = Path(__file__).resolve().parents[2]
_SCRIPT = _REPO / "scripts" / "live-verify-p0-escalation.py"


def _load():
    spec = importlib.util.spec_from_file_location("live_verify_p0_escalation", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


lv = _load()

_OWNER = "0a1b2c3d-4e5f-6789-abcd-ef0123456789"


def _manifest(**over):
    m = {"run_id": "p0esc-20261024T120000Z-abc123", "stage": "staging", "owner": _OWNER,
         "vin": "ZZZ" + "A" * 14, "device_token": "livetest-p0esc-20261024T120000Z-abc123",
         "finding_ids": [], "action_ids": [], "device_written": False}
    m.update(over)
    return m


# ---------------------------------------------------------------------------
# Gates
# ---------------------------------------------------------------------------

def test_refuses_without_live_tests_and_builds_no_client(monkeypatch):
    monkeypatch.delenv("LIVE_TESTS", raising=False)
    monkeypatch.setattr(lv, "guarded_clients", MagicMock(side_effect=AssertionError("built")))
    assert lv.main(["--allow-writes", "--allow-page"]) == 2


@pytest.mark.parametrize("argv,rc", [
    (["--stage", "prod", "--allow-writes", "--allow-page"], 1),
    (["--allow-page"], 2),
    (["--allow-writes"], 2),
])
def test_refuses_other_stages_and_missing_flags_before_any_client(monkeypatch, argv, rc):
    monkeypatch.setenv("LIVE_TESTS", "1")
    monkeypatch.setattr(lv, "guarded_clients", MagicMock(side_effect=AssertionError("built")))
    assert lv.main(argv) == rc


def test_generated_run_id_passes_its_own_gate_and_bad_ids_are_refused():
    assert lv.validate_run_id(lv.new_run_id())
    for bad in ("x", "p0esc-20261024T120000Z-ABC123", "p0esc-20261024T120000Z-abc123/../",
                "p0esc-20261024T120000Z-abc12"):
        with pytest.raises(SystemExit):
            lv.validate_run_id(bad)


def test_cleanup_only_needs_a_valid_run_id(monkeypatch, tmp_path):
    monkeypatch.setenv("LIVE_TESTS", "1")
    assert lv.main(["--allow-writes", "--cleanup-only"], base=tmp_path) == 1
    with pytest.raises(SystemExit):
        lv.main(["--allow-writes", "--cleanup-only", "--run-id", "bad id"], base=tmp_path)


def test_new_vin_is_a_zzz_test_vin():
    import re
    for _ in range(50):
        assert re.match(r"^ZZZ[A-HJ-NPR-Z0-9]{14}$", lv.new_vin())


# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------

def test_manifest_is_0600_in_a_0700_dir_and_round_trips(tmp_path):
    m = _manifest(finding_ids=["FIND-" + "0" * 8 + "-0000-0000-0000-" + "0" * 12])
    path = lv.save_manifest(m, tmp_path / "m")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert lv.load_manifest(m["run_id"], tmp_path / "m") == m


@pytest.mark.parametrize("over", [
    {"stage": "prod"}, {"owner": "CUST-0000AAAA"}, {"vin": "1HGCM82633A123456"},
    {"finding_ids": ["../../etc"]},
])
def test_manifest_with_a_foreign_shape_is_refused(tmp_path, over):
    m = _manifest()
    lv.save_manifest(m, tmp_path)
    path = lv._manifest_path(m["run_id"], tmp_path)
    data = json.loads(path.read_text())
    data.update(over)
    path.write_text(json.dumps(data))
    with pytest.raises(SystemExit):
        lv.load_manifest(m["run_id"], tmp_path)


# ---------------------------------------------------------------------------
# Guard: real clients, a before-send sentinel, nothing reaches the network
# ---------------------------------------------------------------------------

class _Sent(Exception):
    pass


def _guarded(manifest, read_only=False):
    session = boto3.session.Session(region_name="us-west-2", aws_access_key_id="testing",
                                    aws_secret_access_key="testing")
    clients = lv.guarded_clients(manifest, session_factory=lambda: session,
                                 read_only=read_only)

    def _sentinel(**_):
        raise _Sent()

    for client in clients.values():  # a client copies the session's emitter at creation
        client.meta.events.register("before-send", _sentinel)
    return clients


def _raised(call):
    try:
        call()
    except Exception as exc:  # noqa: BLE001
        return exc
    return None


_FID = "FIND-11111111-2222-3333-4444-555555555555"
_AID = "AVX-ACT-11111111-2222-3333-4444-555555555555"


def test_guard_admits_this_runs_requests():
    m = _manifest(finding_ids=[_FID], action_ids=[_AID])
    c = _guarded(m)
    d = c["dynamodb"]
    allowed = [
        lambda: d.put_item(TableName=lv.FINDINGS_TABLE, Item={
            "finding_id": {"S": _FID}, "scope": {"M": {"vin": {"S": m["vin"]}}}}),
        lambda: d.update_item(TableName=lv.FINDINGS_TABLE, Key={"finding_id": {"S": _FID}},
                              UpdateExpression="SET a = :a",
                              ExpressionAttributeValues={":a": {"S": "x"}}),
        lambda: d.delete_item(TableName=lv.ACTIONS_TABLE, Key={"action_id": {"S": _AID}}),
        lambda: d.put_item(TableName=lv.DEVICES_TABLE, Item={
            "owner_id": {"S": _OWNER}, "device_token": {"S": "t"}}),
        lambda: d.update_item(TableName=lv.RATE_TABLE, Key={
            "owner_id": {"S": _OWNER}, "window_key": {"S": f"p0:{_FID}"}},
            UpdateExpression="SET a = :a", ExpressionAttributeValues={":a": {"S": "x"}}),
        lambda: d.query(TableName=lv.RATE_TABLE, KeyConditionExpression="owner_id = :o",
                        ExpressionAttributeValues={":o": {"S": _OWNER}}),
        lambda: d.get_item(TableName=lv.FINDINGS_TABLE, Key={"finding_id": {"S": _FID}}),
        lambda: d.get_item(TableName=lv.ACTIONS_TABLE, Key={"action_id": {"S": _AID}}),
        lambda: d.get_item(TableName=lv.RATE_TABLE, Key={
            "owner_id": {"S": _OWNER}, "window_key": {"S": f"p0:{_FID}"}}),
        lambda: c["ssm"].get_parameter(Name=f"{lv.SSM_PREFIX}rate-limit"),
        lambda: c["logs"].filter_log_events(logGroupName=lv.BROKER_LOG_GROUP),
        lambda: c["lambda"].get_function(FunctionName=lv.BROKER_FN),
    ]
    for call in allowed:
        exc = _raised(call)
        assert type(exc) is _Sent, exc


def test_guard_refuses_everything_outside_the_run():
    m = _manifest(finding_ids=[_FID], action_ids=[_AID])
    c = _guarded(m)
    d = c["dynamodb"]
    other = "99999999-8888-7777-6666-555555555555"
    refused = [
        lambda: d.put_item(TableName=lv.FINDINGS_TABLE, Item={
            "finding_id": {"S": _FID}, "scope": {"M": {"vin": {"S": "1HGCM82633A123456"}}}}),
        lambda: d.delete_item(TableName=lv.FINDINGS_TABLE,
                              Key={"finding_id": {"S": "FIND-other"}}),
        lambda: d.put_item(TableName=lv.ACTIONS_TABLE, Item={"action_id": {"S": _AID}}),
        lambda: d.delete_item(TableName=lv.ACTIONS_TABLE, Key={"action_id": {"S": "AVX-ACT-x"}}),
        lambda: d.delete_item(TableName=lv.DEVICES_TABLE, Key={
            "owner_id": {"S": other}, "device_token": {"S": "t"}}),
        lambda: d.update_item(TableName=lv.RATE_TABLE, Key={
            "owner_id": {"S": other}, "window_key": {"S": "day:x"}},
            UpdateExpression="SET a = :a", ExpressionAttributeValues={":a": {"S": "x"}}),
        lambda: d.query(TableName=lv.DEVICES_TABLE, KeyConditionExpression="owner_id = :o",
                        ExpressionAttributeValues={":o": {"S": other}}),
        lambda: d.scan(TableName=lv.FINDINGS_TABLE),
        lambda: d.scan(TableName=lv.ACTIONS_TABLE),  # write runs never scan (sec S3)
        # point reads outside this run (security review cycle 1 S2)
        lambda: d.get_item(TableName=lv.FINDINGS_TABLE, Key={"finding_id": {"S": "FIND-other"}}),
        lambda: d.get_item(TableName=lv.ACTIONS_TABLE, Key={"action_id": {"S": "AVX-ACT-x"}}),
        lambda: d.get_item(TableName=lv.RATE_TABLE, Key={
            "owner_id": {"S": other}, "window_key": {"S": "day:x"}}),
        lambda: d.get_item(TableName=lv.DEVICES_TABLE, Key={
            "owner_id": {"S": other}, "device_token": {"S": "t"}}),
        lambda: d.put_item(TableName="vsa-staging-other", Item={"k": {"S": "v"}}),
        lambda: c["ssm"].get_parameter(Name="/cvx/staging/secret"),
        lambda: c["logs"].filter_log_events(logGroupName="/aws/lambda/other"),
        lambda: c["lambda"].get_function(FunctionName="other-fn"),
        lambda: c["cloudwatch"].set_alarm_state(AlarmName=lv.ALARM_NAME, StateValue="OK",
                                                StateReason="x"),
    ]
    for call in refused:
        exc = _raised(call)
        assert type(exc) is lv.GuardViolation, exc


# ---------------------------------------------------------------------------
# Preconditions
# ---------------------------------------------------------------------------

def _zip(files: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, body in files.items():
            zf.writestr(name, body)
    return buf.getvalue()


def _pre_clients(alarm_over=None, subs=None, rate=None, broker_src=None, writer_src=None):
    acct = "123456789012"
    alarm = {"Namespace": "AVX/Broker", "MetricName": "PushNotDelivered",
             "Dimensions": [{"Name": "Stage", "Value": "staging"},
                            {"Name": "Severity", "Value": "p0"}],
             "ActionsEnabled": True, "AlarmActions": [lv.topic_arn(acct)],
             "TreatMissingData": "notBreaching", "StateValue": "OK"}
    alarm.update(alarm_over or {})
    c = {k: MagicMock() for k in ("dynamodb", "lambda", "cloudwatch", "sns", "ssm", "logs", "sts")}
    c["lambda"].get_function.side_effect = lambda FunctionName: {
        "Code": {"Location": f"https://example.invalid/{FunctionName}"}}
    c["cloudwatch"].describe_alarms.return_value = {"MetricAlarms": [alarm]}
    c["sns"].list_subscriptions_by_topic.return_value = {"Subscriptions": subs if subs is not None
                                                         else [{"SubscriptionArn":
                                                                lv.topic_arn(acct) + ":abc"}]}
    params = {f"{lv.SSM_PREFIX}rate-limit": json.dumps(rate or {"day": 10, "hour": 3}),
              f"{lv.SSM_PREFIX}cardinality-cap": "15"}
    c["ssm"].get_parameter.side_effect = lambda Name: {"Parameter": {"Value": params[Name]}}
    bsrc = broker_src if broker_src is not None else "\n".join(lv.BROKER_MARKERS)
    wsrc = writer_src if writer_src is not None else "\n".join(lv.WRITER_MARKERS)

    def fetch(url):
        if url.endswith(lv.BROKER_FN):
            return _zip({"agents/supervisor/lambdas/avx_broker.py": bsrc})
        return _zip({"agents/tier2/writer.py": wsrc})
    return c, acct, fetch


def test_preconditions_pass_on_the_expected_world():
    c, acct, fetch = _pre_clients()
    res = lv.Results()
    assert lv.check_preconditions(c, res, acct, fetch) is True
    assert all(s == "PASS" for _, s, _ in res.rows)


@pytest.mark.parametrize("kw", [
    {"broker_src": "old broker"},
    # names present, property lines missing (review cycle 1 S3)
    {"broker_src": 'def _push_p0_once\nPushNotDelivered\n_P0_LEDGER_PREFIX = "p0:"'},
    {"writer_src": "old writer"},
    {"writer_src": 'update_expr += ", #severity = :severity"'},
    {"alarm_over": {"AlarmActions": []}},
    {"alarm_over": {"Dimensions": [{"Name": "Stage", "Value": "staging"},
                                   {"Name": "Severity", "Value": "soon"}]}},
    {"alarm_over": {"ActionsEnabled": False}},
    {"alarm_over": {"StateValue": "ALARM"}},
    {"subs": [{"SubscriptionArn": "PendingConfirmation"}]},
    {"rate": {"day": 10, "hour": 1}},
])
def test_each_precondition_can_fail(kw):
    c, acct, fetch = _pre_clients(**kw)
    res = lv.Results()
    assert lv.check_preconditions(c, res, acct, fetch) is False


# ---------------------------------------------------------------------------
# main: precondition failure writes nothing; every exit cleans up and checks residue
# ---------------------------------------------------------------------------

def _tables(client):
    client.create_table(
        TableName=lv.FINDINGS_TABLE,
        KeySchema=[{"AttributeName": "finding_id", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "finding_id", "AttributeType": "S"},
                              {"AttributeName": "finding_key", "AttributeType": "S"}],
        GlobalSecondaryIndexes=[{"IndexName": "by_finding_key",
                                 "KeySchema": [{"AttributeName": "finding_key", "KeyType": "HASH"}],
                                 "Projection": {"ProjectionType": "ALL"}}],
        BillingMode="PAY_PER_REQUEST")
    client.create_table(TableName=lv.ACTIONS_TABLE,
                        KeySchema=[{"AttributeName": "action_id", "KeyType": "HASH"}],
                        AttributeDefinitions=[{"AttributeName": "action_id", "AttributeType": "S"},
                                              {"AttributeName": "created_by", "AttributeType": "S"}],
                        GlobalSecondaryIndexes=[{
                            "IndexName": "by_created_by",
                            "KeySchema": [{"AttributeName": "created_by", "KeyType": "HASH"}],
                            "Projection": {"ProjectionType": "ALL"}}],
                        BillingMode="PAY_PER_REQUEST")
    for name, sk in ((lv.RATE_TABLE, "window_key"), (lv.DEVICES_TABLE, "device_token")):
        client.create_table(
            TableName=name,
            KeySchema=[{"AttributeName": "owner_id", "KeyType": "HASH"},
                       {"AttributeName": sk, "KeyType": "RANGE"}],
            AttributeDefinitions=[{"AttributeName": "owner_id", "AttributeType": "S"},
                                  {"AttributeName": sk, "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST")


@pytest.fixture()
def moto_ddb(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-west-2")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    with mock_aws():
        client = boto3.client("dynamodb", region_name="us-west-2")
        _tables(client)
        yield client


def _world(moto_ddb, **kw):
    c, acct, fetch = _pre_clients(**kw)
    c["dynamodb"] = moto_ddb
    c["sts"].get_caller_identity.return_value = {"Account": acct}
    return c, fetch


def _count(client, table):
    return client.scan(TableName=table)["Count"]


def test_precondition_failure_writes_nothing(monkeypatch, tmp_path, moto_ddb):
    monkeypatch.setenv("LIVE_TESTS", "1")
    c, fetch = _world(moto_ddb, broker_src="old")
    rc = lv.main(["--allow-writes", "--allow-page"], clients=c, base=tmp_path, fetch=fetch,
                 sleep=lambda s: None)
    assert rc == 1
    for t in (lv.FINDINGS_TABLE, lv.DEVICES_TABLE, lv.RATE_TABLE):
        assert _count(moto_ddb, t) == 0
    assert not list(tmp_path.glob("*.json")), "no manifest before preconditions pass"


def test_a_check_failure_still_cleans_up_and_checks_residue(monkeypatch, tmp_path, moto_ddb, capsys):
    """No deployed broker in the fake world, so no card appears: the run FAILs at that
    check, and the device row and the Finding it wrote are deleted, then confirmed gone."""
    monkeypatch.setenv("LIVE_TESTS", "1")
    monkeypatch.setattr(lv, "_poll", lambda fn, *a, **k: fn())
    c, fetch = _world(moto_ddb)
    rc = lv.main(["--allow-writes", "--allow-page", "--no-wait-alarm-ok"], clients=c,
                 base=tmp_path, fetch=fetch, sleep=lambda s: None)
    out = capsys.readouterr().out
    assert rc == 1
    assert "[FAIL] the soon Finding is carded" in out
    assert "[PASS] no rows of this run remain" in out
    assert "P0_ESCALATION_LIVE_GO=staging LIVE_TESTS=1" in out, (
        "cleanup-if-interrupted line must contain P0_ESCALATION_LIVE_GO=staging before LIVE_TESTS=1; "
        f"got (first 1000 chars): {out[:1000]!r}"
    )
    for t in (lv.FINDINGS_TABLE, lv.DEVICES_TABLE, lv.RATE_TABLE):
        assert _count(moto_ddb, t) == 0
    (manifest_path,) = list(tmp_path.glob("*.json"))
    m = json.loads(manifest_path.read_text())
    assert m["device_written"] is True and len(m["finding_ids"]) == 1


def test_an_exception_after_the_first_write_cleans_up(monkeypatch, tmp_path, moto_ddb, capsys):
    monkeypatch.setenv("LIVE_TESTS", "1")
    monkeypatch.setattr(lv, "build_finding", MagicMock(side_effect=RuntimeError("boom")))
    c, fetch = _world(moto_ddb)
    rc = lv.main(["--allow-writes", "--allow-page", "--no-wait-alarm-ok"], clients=c,
                 base=tmp_path, fetch=fetch, sleep=lambda s: None)
    out = capsys.readouterr().out
    assert rc == 1
    assert "[FAIL] run: stopped by RuntimeError" in out
    assert _count(moto_ddb, lv.DEVICES_TABLE) == 0
    assert "[PASS] no rows of this run remain" in out


def test_systemexit_after_the_first_write_still_cleans_up(monkeypatch, tmp_path, moto_ddb):
    monkeypatch.setenv("LIVE_TESTS", "1")
    monkeypatch.setattr(lv, "build_finding", MagicMock(side_effect=SystemExit(143)))
    c, fetch = _world(moto_ddb)
    with pytest.raises(SystemExit):
        lv.main(["--allow-writes", "--allow-page", "--no-wait-alarm-ok"], clients=c,
                base=tmp_path, fetch=fetch, sleep=lambda s: None)
    assert _count(moto_ddb, lv.DEVICES_TABLE) == 0


def test_a_delete_that_answers_success_but_leaves_the_row_fails_residue(monkeypatch, tmp_path,
                                                                        moto_ddb, capsys):
    monkeypatch.setenv("LIVE_TESTS", "1")
    monkeypatch.setattr(lv, "_poll", lambda fn, *a, **k: fn())
    c, fetch = _world(moto_ddb)
    real = moto_ddb

    class _NoDelete:
        def __getattr__(self, name):
            return getattr(real, name)

        def delete_item(self, **kw):
            return {}

    c["dynamodb"] = _NoDelete()
    rc = lv.main(["--allow-writes", "--allow-page", "--no-wait-alarm-ok"], clients=c,
                 base=tmp_path, fetch=fetch, sleep=lambda s: None)
    out = capsys.readouterr().out
    assert rc == 1
    assert "[FAIL] no rows of this run remain: left behind:" in out
    assert "device row livetest-" in out


def test_cleanup_only_deletes_a_crashed_runs_rows(monkeypatch, tmp_path, moto_ddb, capsys):
    monkeypatch.setenv("LIVE_TESTS", "1")
    m = _manifest(device_written=True)
    lv.save_manifest(m, tmp_path)
    moto_ddb.put_item(TableName=lv.DEVICES_TABLE, Item={
        "owner_id": {"S": _OWNER}, "device_token": {"S": m["device_token"]}})
    moto_ddb.put_item(TableName=lv.RATE_TABLE, Item={
        "owner_id": {"S": _OWNER}, "window_key": {"S": "p0:FIND-x"}})
    c = {"dynamodb": moto_ddb}
    rc = lv.main(["--allow-writes", "--cleanup-only", "--run-id", m["run_id"]], clients=c,
                 base=tmp_path, sleep=lambda s: None)
    assert rc == 0
    assert _count(moto_ddb, lv.DEVICES_TABLE) == 0 and _count(moto_ddb, lv.RATE_TABLE) == 0


def test_cleanup_never_deletes_another_owners_rate_rows(moto_ddb):
    other = "99999999-8888-7777-6666-555555555555"
    moto_ddb.put_item(TableName=lv.RATE_TABLE, Item={
        "owner_id": {"S": other}, "window_key": {"S": "day:2026-10-24"}})
    lv.cleanup({"dynamodb": moto_ddb}, _manifest(), lv.Results())
    assert _count(moto_ddb, lv.RATE_TABLE) == 1


# ---------------------------------------------------------------------------
# Signals and running as a file
# ---------------------------------------------------------------------------

def test_signal_during_cleanup_is_held_until_it_finishes():
    signal.signal(signal.SIGTERM, signal.default_int_handler)
    done = []
    with pytest.raises(SystemExit) as ei:
        with lv._signals_held():
            os.kill(os.getpid(), signal.SIGTERM)
            done.append("after-signal")
    assert done == ["after-signal"] and ei.value.code == 143


def test_runs_as_a_file_with_lazy_imports_resolvable(tmp_path):
    code = (
        "import runpy, sys; runpy.run_path(%r, run_name='probe'); "
        "import agents.tier2.runner, agents.tier2.finding_transform, agents.tier2.writer, "
        "agents.tier2.owner_resolver, agents.health.prompt" % str(_SCRIPT)
    )
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    proc = subprocess.run([sys.executable, "-I", "-B", "-c", code], cwd=tmp_path, env=env,
                          capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr[-2000:]


# ---------------------------------------------------------------------------
# A fake "deployed" broker: the REAL broker runs on every Findings write the
# script makes, so run_checks is driven past its first check (review cycle 1 S4)
# ---------------------------------------------------------------------------

import logging  # noqa: E402
import time  # noqa: E402
from datetime import datetime, timezone  # noqa: E402

from botocore.exceptions import ClientError  # noqa: E402


def _broker_module():
    lam = str(_REPO / "agents" / "supervisor" / "lambdas")
    if lam not in sys.path:
        sys.path.insert(0, lam)
    import avx_broker  # noqa: PLC0415
    return avx_broker


class _DeployedWorld:
    """moto tables + the real broker on the Findings stream + fake logs/metrics/alarm."""

    def __init__(self, moto_client, monkeypatch, publish_ok=False, broker_patch=None,
                 alarm_fires=True, old_alarm_actions=()):
        self.client = moto_client
        moto_client.create_table(
            TableName="vsa-staging-avx-feedback",
            KeySchema=[{"AttributeName": "owner_id", "KeyType": "HASH"},
                       {"AttributeName": "feedback_id", "KeyType": "RANGE"}],
            AttributeDefinitions=[{"AttributeName": "owner_id", "AttributeType": "S"},
                                  {"AttributeName": "feedback_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST")
        self.broker = b = _broker_module()
        for attr, name in (("_FINDINGS_TABLE", lv.FINDINGS_TABLE),
                           ("_ACTIONS_TABLE", lv.ACTIONS_TABLE),
                           ("_RATE_LIMITS_TABLE", lv.RATE_TABLE),
                           ("_DEVICES_TABLE", lv.DEVICES_TABLE),
                           ("_FEEDBACK_TABLE", "vsa-staging-avx-feedback")):
            monkeypatch.setattr(b, attr, name)
        monkeypatch.setattr(b, "_STAGE", "staging")
        monkeypatch.setattr(b, "_ddb", boto3.resource("dynamodb", region_name="us-west-2"))
        sns = MagicMock()
        if not publish_ok:
            sns.publish.side_effect = ClientError(
                {"Error": {"Code": "NotFound", "Message": "Endpoint does not exist"}}, "Publish")
        monkeypatch.setattr(b, "_sns", sns)
        monkeypatch.setattr(b, "_policy", {"apns_threshold": "p0",
                                           "rate_limit": {"day": 10, "hour": 3},
                                           "cardinality_cap": 15})
        for name, fn in (broker_patch or {}).items():
            monkeypatch.setattr(b, name, fn)
        self.sns = sns
        self.lines: list[tuple[int, str]] = []
        self.p0_metric = 0
        self.p0_metric_at: list[float] = []   # epoch seconds of each p0 EMF line
        self.alarm_fires = alarm_fires
        #: Action history from before this run (review cycle 2 S3): epoch seconds.
        self.old_alarm_actions = list(old_alarm_actions)
        handler = logging.Handler()
        handler.emit = lambda rec: self.lines.append((int(time.time() * 1000), rec.getMessage()))
        logging.getLogger(b.__name__).addHandler(handler)
        self._handler = handler
        real_emit = b._emit_push_not_delivered

        def emit(severity, reason, finding_id):
            if severity == "p0":
                self.p0_metric += 1
                self.p0_metric_at.append(time.time())
            return real_emit(severity, reason, finding_id)
        monkeypatch.setattr(b, "_emit_push_not_delivered", emit)
        self._seq = 0

    def close(self):
        logging.getLogger(self.broker.__name__).removeHandler(self._handler)

    # the DynamoDB client the script uses: every Findings write reaches the broker
    def ddb(self):
        world = self

        class _Ddb:
            def __getattr__(self, name):
                return getattr(world.client, name)

            def put_item(self, **kw):
                out = world.client.put_item(**kw)
                if kw["TableName"] == lv.FINDINGS_TABLE:
                    world._deliver(kw["Item"]["finding_id"]["S"], "INSERT")
                return out

            def update_item(self, **kw):
                out = world.client.update_item(**kw)
                if kw["TableName"] == lv.FINDINGS_TABLE:
                    world._deliver(kw["Key"]["finding_id"]["S"], "MODIFY")
                return out
        return _Ddb()

    def _deliver(self, fid, event):
        self._seq += 1
        image = self.client.get_item(TableName=lv.FINDINGS_TABLE,
                                     Key={"finding_id": {"S": fid}})["Item"]
        self.broker.handler({"Records": [{"eventName": event, "dynamodb": {
            "SequenceNumber": str(self._seq), "NewImage": image}}]}, None)

    def clients(self, acct="123456789012"):
        c, _, fetch = _pre_clients()
        c["dynamodb"] = self.ddb()
        c["sts"].get_caller_identity.return_value = {"Account": acct}

        def filter_log_events(**kw):
            needle = kw["filterPattern"].strip('"')
            return {"events": [{"message": m} for t, m in self.lines
                               if t >= kw["startTime"] and needle in m]}
        c["logs"].filter_log_events.side_effect = filter_log_events
        # The metric and alarm fakes honour the script's own arguments (review cycle 2
        # S3), so its Severity=p0 selection and its "this run's action" filter are tested.
        def get_metric_statistics(**kw):
            dims = {d["Name"]: d["Value"] for d in kw["Dimensions"]}
            if (kw["Namespace"], kw["MetricName"]) != ("AVX/Broker", "PushNotDelivered") \
                    or dims != {"Stage": "staging", "Severity": "p0"}:
                return {"Datapoints": []}
            lo, hi = kw["StartTime"].timestamp(), kw["EndTime"].timestamp()
            n = sum(1 for t in self.p0_metric_at if lo <= t <= hi)
            return {"Datapoints": [{"Sum": float(n)}] if n else []}

        def describe_alarm_history(**kw):
            if kw.get("AlarmName") != lv.ALARM_NAME or kw.get("HistoryItemType") != "Action":
                return {"AlarmHistoryItems": []}
            stamps = list(self.old_alarm_actions)
            if self.alarm_fires and self.p0_metric_at:
                stamps.append(self.p0_metric_at[0])
            items = [{"Timestamp": datetime.fromtimestamp(t, timezone.utc),
                      "HistoryData": json.dumps({"actionState": "Succeeded"})}
                     for t in sorted(stamps, reverse=True)]
            return {"AlarmHistoryItems": items[:kw.get("MaxRecords", 100)]}
        c["cloudwatch"].get_metric_statistics.side_effect = get_metric_statistics
        c["cloudwatch"].describe_alarm_history.side_effect = describe_alarm_history
        return c, fetch


def _run(world, tmp_path, capsys):
    c, fetch = world.clients()
    rc = lv.main(["--allow-writes", "--allow-page"], clients=c, base=tmp_path, fetch=fetch,
                 sleep=lambda s: None)
    return rc, capsys.readouterr().out


@pytest.fixture()
def fast_poll(monkeypatch):
    monkeypatch.setattr(lv, "_poll", lambda fn, *a, **k: fn())


def test_full_run_against_the_real_broker_passes(monkeypatch, tmp_path, moto_ddb, capsys,
                                                 fast_poll):
    monkeypatch.setenv("LIVE_TESTS", "1")
    world = _DeployedWorld(moto_ddb, monkeypatch)
    try:
        rc, out = _run(world, tmp_path, capsys)
    finally:
        world.close()
    assert rc == 0, out
    for label in ("the soon Finding is carded by the deployed broker",
                  "the p0 write updates the same Finding in place",
                  "the stored Finding says p0 with the C0161 directive verbatim",
                  "the deployed broker attempted the escalation push and recorded it failed",
                  "the broker logged the lost p0 push", "the EMF metric has a p0 datapoint",
                  "the alarm fired and its SNS action succeeded",
                  "a repeat p0 write after a delivered push is already_carded",
                  "a repeat p0 write after a delivered push makes no push attempt",
                  "no rows of this run remain"):
        assert f"[PASS] {label}" in out, label
    assert "[FAIL]" not in out
    assert world.sns.publish.call_count == 1, "one escalation attempt, no repeat"


def test_an_alarm_action_from_before_this_run_does_not_pass(monkeypatch, tmp_path, moto_ddb,
                                                            capsys, fast_poll):
    """Review cycle 2 S3: only an action stamped after this run's escalation counts."""
    monkeypatch.setenv("LIVE_TESTS", "1")
    world = _DeployedWorld(moto_ddb, monkeypatch, alarm_fires=False,
                           old_alarm_actions=[time.time() - 3600])
    try:
        rc, out = _run(world, tmp_path, capsys)
    finally:
        world.close()
    assert rc != 0
    assert "[FAIL] the alarm fired and its SNS action succeeded" in out
    assert "[PASS] the EMF metric has a p0 datapoint" in out


def test_a_broker_that_pushes_again_fails_the_repeat_check(monkeypatch, tmp_path, moto_ddb,
                                                           capsys, fast_poll):
    """A deployed broker that ignores a published ledger (mutant R1's shape) must FAIL."""
    monkeypatch.setenv("LIVE_TESTS", "1")
    world = _DeployedWorld(moto_ddb, monkeypatch,
                           broker_patch={"_p0_ledger_state": lambda owner, fid: None})
    try:
        rc, out = _run(world, tmp_path, capsys)
    finally:
        world.close()
    assert rc == 1
    assert "[FAIL] a repeat p0 write after a delivered push makes no push attempt" in out


def test_a_broker_that_skips_and_still_pushes_fails_the_no_attempt_check(
        monkeypatch, tmp_path, moto_ddb, capsys, fast_poll):
    """Review cycle 2 RV-6: the absence check must FAIL on its own, even when the
    already_carded line is also there (here: the repeat write is delivered twice, the
    first time to a broker that ignores the published ledger)."""
    monkeypatch.setenv("LIVE_TESTS", "1")
    world = _DeployedWorld(moto_ddb, monkeypatch)
    real_state = world.broker._p0_ledger_state
    seen_published = {"n": 0}

    def flaky_state(owner, fid):
        state = real_state(owner, fid)
        if state == "published":
            seen_published["n"] += 1
            if seen_published["n"] == 1:
                return None  # the first delivery of the repeat write ignores it
        return state
    monkeypatch.setattr(world.broker, "_p0_ledger_state", flaky_state)
    real_deliver = world._deliver

    def deliver_twice_after_marking(fid, event):
        real_deliver(fid, event)
        if event == "MODIFY" and seen_published["n"]:
            real_deliver(fid, event)
    monkeypatch.setattr(world, "_deliver", deliver_twice_after_marking)
    try:
        rc, out = _run(world, tmp_path, capsys)
    finally:
        world.close()
    assert rc == 1
    assert "[PASS] a repeat p0 write after a delivered push is already_carded" in out
    assert "[FAIL] a repeat p0 write after a delivered push makes no push attempt" in out


def test_a_published_escalation_fails_the_failed_ledger_check(monkeypatch, tmp_path, moto_ddb,
                                                              capsys, fast_poll):
    """The check expects the bogus endpoint's failure; any other ledger state must FAIL."""
    monkeypatch.setenv("LIVE_TESTS", "1")
    world = _DeployedWorld(moto_ddb, monkeypatch, publish_ok=True)
    try:
        rc, out = _run(world, tmp_path, capsys)
    finally:
        world.close()
    assert rc == 1
    assert "[FAIL] the deployed broker attempted the escalation push and recorded it failed" in out


def test_a_leftover_card_fails_residue(monkeypatch, tmp_path, moto_ddb, capsys, fast_poll):
    monkeypatch.setenv("LIVE_TESTS", "1")
    world = _DeployedWorld(moto_ddb, monkeypatch)
    c, fetch = world.clients()
    real_ddb = c["dynamodb"]

    class _KeepCards:
        def __getattr__(self, name):
            return getattr(real_ddb, name)

        def delete_item(self, **kw):
            if kw["TableName"] == lv.ACTIONS_TABLE:
                return {}
            return real_ddb.delete_item(**kw)

    c["dynamodb"] = _KeepCards()
    try:
        rc = lv.main(["--allow-writes", "--allow-page"], clients=c, base=tmp_path, fetch=fetch,
                     sleep=lambda s: None)
    finally:
        world.close()
    out = capsys.readouterr().out
    assert rc == 1
    assert "[FAIL] no rows of this run remain: left behind: action AVX-ACT-" in out


def test_cleanup_removes_the_device_row_before_anything_else(moto_ddb):
    """S2: a delivery still in the stream must meet consent_off, not a live device."""
    order = []
    m = _manifest(device_written=True, finding_ids=[_FID], action_ids=[_AID])

    class _Rec:
        def __getattr__(self, name):
            return getattr(moto_ddb, name)

        def delete_item(self, **kw):
            order.append(kw["TableName"])
            return moto_ddb.delete_item(**kw)
    lv.cleanup({"dynamodb": _Rec()}, m, lv.Results(),
               sleep=lambda s: order.append(("sleep", s)))
    # Review cycle 2 S5: the drain sits between the device row and everything else.
    assert order[:2] == [lv.DEVICES_TABLE, ("sleep", lv.CLEANUP_DRAIN_SECONDS)]
    assert lv.FINDINGS_TABLE in order[2:] and lv.ACTIONS_TABLE in order[2:]


def test_cleanup_drain_outlasts_the_broker_timeout():
    """Review cycle 2 S5: a delivery that read consent just before the device row went
    can run for the broker's whole timeout; the drain must outlast it."""
    import re
    stack = (_REPO / "infrastructure" / "lib" / "vsa-tier2-stack.ts").read_text()
    start = stack.index("const avxBrokerFn = ")
    m = re.search(r"timeout:\s*cdk\.Duration\.seconds\((\d+)\)", stack[start:])
    assert m, "broker timeout not found"
    assert lv.CLEANUP_DRAIN_SECONDS >= int(m.group(1)) + 5


def test_deployed_code_markers_occur_in_this_checkout():
    """Review cycle 2 S4: the fakes build the deployed bundle FROM the markers, so a
    marker that drifted from the source passes every other test and then fails the
    real precondition on a correct deploy."""
    broker_src = (_REPO / "agents" / "supervisor" / "lambdas" / "avx_broker.py").read_text()
    writer_src = (_REPO / "agents" / "tier2" / "writer.py").read_text()
    assert [m for m in lv.BROKER_MARKERS if m not in broker_src] == []
    assert [m for m in lv.WRITER_MARKERS if m not in writer_src] == []


# ---------------------------------------------------------------------------
# Pre-deploy count (review cycle 1 W2)
# ---------------------------------------------------------------------------

def _card(moto_ddb, fid, owner=_OWNER, target="ios-owner"):
    moto_ddb.put_item(TableName=lv.ACTIONS_TABLE, Item={
        "action_id": {"S": f"AVX-ACT-{fid}"}, "finding_id": {"S": fid},
        "created_by": {"S": f"owner-{owner}"}, "target_system": {"S": target}})


def _finding_row(moto_ddb, fid, severity, expires="2099-01-01T00:00:00Z", headline="x"):
    moto_ddb.put_item(TableName=lv.FINDINGS_TABLE, Item={
        "finding_id": {"S": fid}, "severity": {"S": severity}, "expires_at": {"S": expires},
        "headline": {"S": headline}})


def _check_row(res, label):
    (row,) = [r for r in res.rows if r[0] == label]
    return row


def test_pre_deploy_count_lists_only_live_p0_cards_without_a_ledger_row(moto_ddb):
    _finding_row(moto_ddb, "F-live-p0", "p0"); _card(moto_ddb, "F-live-p0")
    _finding_row(moto_ddb, "F-soon", "soon"); _card(moto_ddb, "F-soon")
    _finding_row(moto_ddb, "F-expired", "p0", expires="2020-01-01T00:00:00Z")
    _card(moto_ddb, "F-expired")
    _finding_row(moto_ddb, "F-ledgered", "p0"); _card(moto_ddb, "F-ledgered")
    moto_ddb.put_item(TableName=lv.RATE_TABLE, Item={
        "owner_id": {"S": _OWNER}, "window_key": {"S": "p0:F-ledgered"}, "state": {"S": "published"}})
    _finding_row(moto_ddb, "F-not-ios", "p0"); _card(moto_ddb, "F-not-ios", target="dms")
    res = lv.Results()
    assert lv.pre_deploy_count({"dynamodb": moto_ddb}, res) == ["F-live-p0"]
    row = _check_row(res, "no live p0 card would push again at the transition")
    assert row[1] == "FAIL" and "F-live-p0" in row[2]


def test_pre_deploy_count_follows_every_scan_page(moto_ddb):
    """Review cycle 2 S6a: a one-item page forces the LastEvaluatedKey loop."""
    fids = [f"F-page-{i}" for i in range(4)]
    for fid in fids:
        _finding_row(moto_ddb, fid, "p0"); _card(moto_ddb, fid)
    pages = []

    class _Paged:
        def __getattr__(self, name):
            return getattr(moto_ddb, name)

        def scan(self, **kw):
            pages.append(kw.get("ExclusiveStartKey"))
            return moto_ddb.scan(Limit=1, **kw)
    found = lv.pre_deploy_count({"dynamodb": _Paged()}, lv.Results())
    assert sorted(found) == fids
    assert len(pages) >= len(fids), "one item per page: every page was read"


def test_pre_deploy_main_exits_1_when_a_card_would_push(monkeypatch, moto_ddb, capsys):
    """Review cycle 2 S6b: the exit code is what gates the hand-back's deploy."""
    monkeypatch.setenv("LIVE_TESTS", "1")
    _finding_row(moto_ddb, "F-live-p0", "p0"); _card(moto_ddb, "F-live-p0")
    assert lv.main(["--pre-deploy"], clients={"dynamodb": moto_ddb}) == 1
    assert "[FAIL] no live p0 card would push again at the transition" in capsys.readouterr().out


def test_pre_deploy_notes_cards_that_escalate_at_the_transition(moto_ddb):
    """Review cycle 2 S7: a Finding stored soon whose text already carries the C0161
    directive pushes once after the deploy; the count names those for the operator."""
    from agents.health.prompt import C0161_VERBATIM_REMEDY
    _finding_row(moto_ddb, "F-frozen", "soon", headline=f"{C0161_VERBATIM_REMEDY}\n\nBrakes.")
    _card(moto_ddb, "F-frozen")
    _finding_row(moto_ddb, "F-plain", "soon"); _card(moto_ddb, "F-plain")
    _finding_row(moto_ddb, "F-old", "soon", expires="2020-01-01T00:00:00Z",
                 headline=C0161_VERBATIM_REMEDY)
    _card(moto_ddb, "F-old")
    res = lv.Results()
    assert lv.pre_deploy_count({"dynamodb": moto_ddb}, res) == []
    (note,) = [r for r in res.rows if r[1] == "NOTE"]
    assert note[2].startswith("1;") and "F-frozen" in note[2] and "F-plain" not in note[2]
    assert "F-old" not in note[2]


def test_read_only_guard_refuses_every_write():
    """Review cycle 2 S6c: the pre-deploy manifest is {owner: None, vin: None}, so a
    Findings PutItem without scope.vin matches ``None == None``; only the flag stops it."""
    c = _guarded({"owner": None, "vin": None}, read_only=True)
    for call in (
        lambda: c["dynamodb"].put_item(TableName=lv.FINDINGS_TABLE,
                                       Item={"finding_id": {"S": "F-x"}}),
        lambda: c["dynamodb"].update_item(TableName=lv.RATE_TABLE,
                                          Key={"owner_id": {"S": "o"}, "window_key": {"S": "k"}}),
        lambda: c["dynamodb"].delete_item(TableName=lv.ACTIONS_TABLE,
                                          Key={"action_id": {"S": "a"}}),
    ):
        exc = _raised(call)
        assert type(exc) is lv.GuardViolation and "read-only" in str(exc), exc
    assert type(_raised(lambda: c["dynamodb"].scan(TableName=lv.ACTIONS_TABLE))) is _Sent
    # Without the flag the same put reaches the wire (the hole the flag closes).
    open_c = _guarded({"owner": None, "vin": None})
    assert type(_raised(lambda: open_c["dynamodb"].put_item(
        TableName=lv.FINDINGS_TABLE, Item={"finding_id": {"S": "F-x"}}))) is _Sent


def test_pre_deploy_main_builds_read_only_clients(monkeypatch):
    monkeypatch.setenv("LIVE_TESTS", "1")
    seen = {}

    def fake(manifest, session_factory=None, read_only=False):
        seen["read_only"] = read_only
        raise RuntimeError("stop")
    monkeypatch.setattr(lv, "guarded_clients", fake)
    with pytest.raises(RuntimeError):
        lv.main(["--pre-deploy"])
    assert seen == {"read_only": True}


def test_pre_deploy_mode_is_read_only_and_needs_no_write_flag(monkeypatch, moto_ddb, capsys):
    monkeypatch.setenv("LIVE_TESTS", "1")
    _finding_row(moto_ddb, "F-soon", "soon"); _card(moto_ddb, "F-soon")
    calls = []

    class _Rec:
        def __getattr__(self, name):
            calls.append(name)
            return getattr(moto_ddb, name)
    rc = lv.main(["--pre-deploy"], clients={"dynamodb": _Rec()})
    assert rc == 0
    assert set(calls) <= {"scan", "get_item"}, calls
    assert "[PASS] no live p0 card would push again at the transition" in capsys.readouterr().out


def test_guard_scans_only_the_actions_table_and_only_read_only():
    c = _guarded({"owner": None, "vin": None}, read_only=True)
    assert type(_raised(lambda: c["dynamodb"].scan(TableName=lv.ACTIONS_TABLE))) is _Sent
    # the read-only count reads other owners' rows by design
    assert type(_raised(lambda: c["dynamodb"].get_item(
        TableName=lv.FINDINGS_TABLE, Key={"finding_id": {"S": "FIND-any"}}))) is _Sent
    exc = _raised(lambda: c["dynamodb"].scan(TableName=lv.RATE_TABLE))
    assert type(exc) is lv.GuardViolation and "only the Actions table" in str(exc)
    w = _guarded(_manifest())
    exc = _raised(lambda: w["dynamodb"].scan(TableName=lv.ACTIONS_TABLE))
    assert type(exc) is lv.GuardViolation and "read-only" in str(exc)


def test_the_full_run_passes_its_own_guard(monkeypatch, tmp_path, moto_ddb, capsys, fast_poll):
    """Security review cycle 1 S2/S3: every DynamoDB request the real run makes goes
    through make_guard (registered on the client the broker does not use), so a guard
    tightened past what the run needs fails here rather than on staging."""
    monkeypatch.setenv("LIVE_TESTS", "1")
    world = _DeployedWorld(moto_ddb, monkeypatch)
    c, fetch = world.clients()
    seen = []

    def guarded(manifest, session_factory=None, read_only=False):
        guard = lv.make_guard(manifest, read_only)

        def recording(model, params, **kw):
            seen.append(model.name)
            return guard(model=model, params=params, **kw)
        moto_ddb.meta.events.register("before-parameter-build.dynamodb", recording)
        return c
    monkeypatch.setattr(lv, "guarded_clients", guarded)
    try:
        rc = lv.main(["--allow-writes", "--allow-page"], base=tmp_path, fetch=fetch,
                     sleep=lambda s: None)
    finally:
        world.close()  # the fixture's client, and so the guard, end with the test
    out = capsys.readouterr().out
    assert rc == 0, out
    assert "GuardViolation" not in out
    assert {"PutItem", "UpdateItem", "GetItem", "DeleteItem", "Query"} <= set(seen), seen
