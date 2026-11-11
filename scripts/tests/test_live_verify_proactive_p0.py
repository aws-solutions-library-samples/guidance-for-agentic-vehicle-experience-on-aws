"""Tests for scripts/live-verify-proactive-p0.py.

Covers (no real AWS; conftest guard blocks boto3 transport):
  1. Subprocess/importlib import test — the module loads without error.
  2. Refusal guards:
       - LIVE_TESTS unset → exit 2
       - --stage prod → SystemExit (non-zero)
       - --vehicle-id VEH-MRDN-0015 → SystemExit (non-zero)
       - --vin MRDN0000000000015 → SystemExit (non-zero)
       - no --vehicle-id → argparse SystemExit(2)
  3. validate_run_id: valid and invalid run ids.
  4. save_manifest / _manifest_path use the right 0700 dir and 0600 file.
  5. _voice_cleanup on exception: Cognito delete and DTC delete are both called
     even when one raises (cleanup must not short-circuit).
  6. Cleanup on SIGTERM: the voice run's ``finally`` block calls _voice_cleanup
     (structural test — _voice_cleanup is monkeypatched to confirm it is wired
     into the ``finally``, not the ``try`` body).
"""
from __future__ import annotations

import importlib.util
import json
import os
import signal
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError as _ClientError
from botocore.exceptions import NoCredentialsError as _NoCredentialsError

_SCRIPT = Path(__file__).resolve().parents[1] / "live-verify-proactive-p0.py"

# Expose the catalog module (already on sys.path via _SCRIPT's repo insert).
import sys as _sys
_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in _sys.path:
    _sys.path.insert(0, str(_REPO))
from agents.supervisor import dtc_response_catalog as cat  # noqa: E402


# ---------------------------------------------------------------------------
# Module import (the "subprocess runpy import test")
# ---------------------------------------------------------------------------

def _load():
    spec = importlib.util.spec_from_file_location("_lv_pp0_under_test", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


# Load once at test-collection time — any import-level error fails here.
lv = _load()


def test_script_imports_cleanly() -> None:
    """The script module is already loaded by _load() above; this confirms it."""
    assert lv.STAGE == "staging"
    assert lv.REGION == "us-west-2"
    assert "VEH-MRDN-0015" in lv.BANNED_VEHICLE_IDS
    assert "MRDN0000000000015" in lv.BANNED_VINS


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def _no_aws(monkeypatch):
    """Block boto3 / asyncio.run before any gate passes."""
    boom = MagicMock(side_effect=AssertionError("AWS client built before gates passed"))
    monkeypatch.setattr(lv, "boto3", boom, raising=False)
    monkeypatch.setattr(lv.asyncio, "run",
                        MagicMock(side_effect=AssertionError("asyncio.run called before gates")))


# ---------------------------------------------------------------------------
# 2. Refusal guards
# ---------------------------------------------------------------------------

def test_refused_without_live_tests(monkeypatch, _no_aws) -> None:
    monkeypatch.delenv("LIVE_TESTS", raising=False)
    assert lv.main(["voice", "--vehicle-id", "VEH-TEST-001"]) == 2


def test_refused_stage_prod(monkeypatch, _no_aws) -> None:
    monkeypatch.setenv("LIVE_TESTS", "1")
    with pytest.raises(SystemExit) as exc:
        lv.main(["voice", "--vehicle-id", "VEH-TEST-001", "--stage", "prod"])
    assert exc.value.code != 0


def test_refused_banned_vehicle_id(monkeypatch, _no_aws) -> None:
    monkeypatch.setenv("LIVE_TESTS", "1")
    with pytest.raises(SystemExit) as exc:
        lv.main(["voice", "--vehicle-id", "VEH-MRDN-0015"])
    assert exc.value.code != 0


def test_refused_banned_vin(monkeypatch, _no_aws) -> None:
    monkeypatch.setenv("LIVE_TESTS", "1")
    with pytest.raises(SystemExit) as exc:
        lv.main(["voice", "--vehicle-id", "VEH-TEST-001", "--vin", "MRDN0000000000015"])
    assert exc.value.code != 0


def test_refused_missing_vehicle_id(monkeypatch, _no_aws) -> None:
    """argparse exits with code 2 when required --vehicle-id is absent."""
    monkeypatch.setenv("LIVE_TESTS", "1")
    with pytest.raises(SystemExit) as exc:
        lv.main(["voice"])
    assert exc.value.code == 2


def test_push_stub_refuses_no_live_tests(monkeypatch, _no_aws) -> None:
    monkeypatch.delenv("LIVE_TESTS", raising=False)
    assert lv.main(["push", "--vehicle-id", "VEH-TEST-001",
                    "--device-token", "tok123"]) == 2


def test_push_no_key_exits_2_with_no_writes(monkeypatch) -> None:
    """When describe_secret raises ResourceNotFoundException push_run returns 2 and makes no writes."""
    from botocore.exceptions import ClientError as BotoClientError
    monkeypatch.setenv("LIVE_TESTS", "1")
    write_calls: list[str] = []

    def _fake_sm():
        c = MagicMock()
        c.describe_secret.side_effect = BotoClientError(
            {"Error": {"Code": "ResourceNotFoundException", "Message": "not found"}},
            "DescribeSecret",
        )
        return c

    def _fake_ddb_resource(*_a, **_kw):
        r = MagicMock()
        # Any write attempt on a Table should be recorded.
        t = MagicMock()
        t.put_item.side_effect = lambda **_: write_calls.append("put_item")
        t.delete_item.side_effect = lambda **_: write_calls.append("delete_item")
        t.update_item.side_effect = lambda **_: write_calls.append("update_item")
        r.Table.return_value = t
        return r

    fake_boto3 = MagicMock()
    fake_boto3.client.side_effect = lambda svc, **_: _fake_sm()
    fake_boto3.resource.side_effect = _fake_ddb_resource
    monkeypatch.setattr(lv, "boto3", fake_boto3)

    import argparse
    args = argparse.Namespace(
        stage="staging", vehicle_id="VEH-TEST-001",
        device_token="fake-token", vin=None, run_id=None,
    )
    result = lv.push_run(args)
    assert result == 2, "Should exit 2 when secret is absent"
    assert write_calls == [], f"No writes allowed; got {write_calls}"


def test_push_lambda_no_platform_arn_exits_2(monkeypatch) -> None:
    """When the Lambda has empty AVX_APNS_PLATFORM_APP_ARN, exit 2 with no writes."""
    monkeypatch.setenv("LIVE_TESTS", "1")
    write_calls: list[str] = []

    def _make_client(svc, **_kw):
        c = MagicMock()
        if svc == "secretsmanager":
            c.describe_secret.return_value = {"ARN": "arn:...", "Name": "cvx/staging/apns-auth-key"}
        elif svc == "lambda":
            c.get_function_configuration.return_value = {
                "Environment": {"Variables": {"AVX_APNS_PLATFORM_APP_ARN": ""}}
            }
        else:
            # Any write on any other client fails the test.
            c.put_item = MagicMock(side_effect=lambda **_: write_calls.append("put_item"))
        return c

    fake_boto3 = MagicMock()
    fake_boto3.client.side_effect = _make_client
    monkeypatch.setattr(lv, "boto3", fake_boto3)

    import argparse
    args = argparse.Namespace(
        stage="staging", vehicle_id="VEH-TEST-001",
        device_token="fake-token", vin=None, run_id=None,
    )
    result = lv.push_run(args)
    assert result == 2, "Should exit 2 when platform ARN is empty"
    assert write_calls == []


def test_push_never_calls_get_secret_value(monkeypatch) -> None:
    """push_run MUST use only describe_secret — never get_secret_value.

    Mutation M1: replace describe_secret(…) with get_secret_value(…).
    Without the precondition check raising an exception, the run would
    proceed past preconditions.  The Lambda client is mocked to also raise
    (simulates no-key scenario), so push_run returns 2 after both precondition
    checks, and the assertion on getsecret_calls catches M1 directly.
    """
    from botocore.exceptions import ClientError as BotoClientError
    monkeypatch.setenv("LIVE_TESTS", "1")
    getsecret_calls: list[str] = []

    def _make_client(svc, **_kw):
        c = MagicMock()
        if svc == "secretsmanager":
            # Nominal path: describe_secret raises ResourceNotFoundException → exit 2 early.
            # M1 path: get_secret_value is called (doesn't raise here);
            # the lambda check below then raises so push_run still returns 2.
            c.describe_secret.side_effect = BotoClientError(
                {"Error": {"Code": "ResourceNotFoundException", "Message": "nf"}},
                "DescribeSecret",
            )
            c.get_secret_value.side_effect = (
                lambda **_: getsecret_calls.append("get_secret_value")
            )
        elif svc == "lambda":
            # ResourceNotFound so M1 still returns 2 here and the
            # getsecret_calls assertion, not an incidental error, catches it.
            c.get_function_configuration.side_effect = BotoClientError(
                {"Error": {"Code": "ResourceNotFoundException", "Message": "nf"}},
                "GetFunctionConfiguration",
            )
        return c

    fake_boto3 = MagicMock()
    fake_boto3.client.side_effect = _make_client
    monkeypatch.setattr(lv, "boto3", fake_boto3)

    import argparse
    args = argparse.Namespace(
        stage="staging", vehicle_id="VEH-TEST-001",
        device_token="fake-token", vin=None, run_id=None,
    )
    result = lv.push_run(args)
    # Both nominal (describe_secret raises) and M1 (get_secret_value, then
    # lambda raises) return 2 — but only M1 populates getsecret_calls.
    assert result == 2
    assert getsecret_calls == [], (
        "get_secret_value must NEVER be called from push_run; "
        "only describe_secret is allowed for the key-presence check"
    )


def test_push_exact_directive_comparison(monkeypatch) -> None:
    """_detail_is_exact_directive requires strict equality, not substring containment.

    Mutation M3: replace ``== expected`` with ``expected in detail`` in
    _detail_is_exact_directive.  A superset detail (exact + suffix) passes ``in``
    but fails ``==``, so the second assert catches M3.
    """
    expected = cat.p0_directive(cat.faults_for_codes(["C1234"]), None)

    # Exact match must pass.
    assert lv._detail_is_exact_directive({"detail": expected}, expected) is True

    # Superset: contains expected as a prefix but is strictly longer.
    superset = expected + " (extra text appended)"
    assert expected in superset, "sanity: superset contains expected"
    assert superset != expected, "sanity: superset != expected"
    assert lv._detail_is_exact_directive({"detail": superset}, expected) is False, (
        "Superset must fail; would pass if M3 changes == to 'in'"
    )

    # Near-match (last char changed) must also fail.
    near = expected[:-1] + ("X" if expected[-1] != "X" else "Y")
    assert lv._detail_is_exact_directive({"detail": near}, expected) is False

    # None/absent detail must fail.
    assert lv._detail_is_exact_directive({}, expected) is False


def test_push_refused_banned_vehicle_id_push(monkeypatch, _no_aws) -> None:
    """push subcommand refuses VEH-MRDN-0015."""
    monkeypatch.setenv("LIVE_TESTS", "1")
    with pytest.raises(SystemExit) as exc:
        lv.main(["push", "--vehicle-id", "VEH-MRDN-0015",
                 "--device-token", "tok"])
    assert exc.value.code != 0


def test_push_refused_banned_vin_push(monkeypatch, _no_aws) -> None:
    """push subcommand refuses the field-video demo VIN."""
    monkeypatch.setenv("LIVE_TESTS", "1")
    with pytest.raises(SystemExit) as exc:
        lv.main(["push", "--vehicle-id", "VEH-TEST-001",
                 "--vin", "MRDN0000000000015",
                 "--device-token", "tok"])
    assert exc.value.code != 0


# ---------------------------------------------------------------------------
# _push_cleanup: no short-circuit, correct deletes, manifest-keyed
# ---------------------------------------------------------------------------

def _make_sns() -> MagicMock:
    return MagicMock()


def _make_ddb_table(*, fail: bool = False) -> MagicMock:
    t = MagicMock()
    if fail:
        t.delete_item.side_effect = RuntimeError("DDB throttled")
    return t


def test_push_cleanup_deletes_all_five_items() -> None:
    """All five cleanup targets are deleted when everything succeeds.

    The query-based device cleanup (W4) finds the endpoint_arn and device_token
    from the devices_table query rather than from manifest parameters.
    Mutation M-W4q: remove the devices_table.query call (replace with no-op).
    With M-W4q, sns.delete_endpoint and devices.delete_item are never called,
    so both assertions fail.
    """
    idp = _make_idp()
    drivers = _make_ddb_table()
    dtc = _make_ddb_table()
    devices = _make_ddb_table()
    devices.query.return_value = {
        "Items": [{"endpoint_arn": "arn:aws:sns:us-west-2:123:endpoint/APNS/app/id",
                   "device_token": "dev-tok"}]
    }
    sns = _make_sns()
    lv._push_cleanup(
        idp=idp, pool="pool-1", user_email="u@example.com",
        drivers_table=drivers, driver_id="DRV-PUSH-001",
        dtc_table=dtc, vehicle_id="VEH-T01",
        dtc_ts_ms=1_700_000_000_000, dtc_source="src-push",
        sns_client=sns,
        devices_table=devices, owner_sub="sub-abc",
    )
    idp.admin_delete_user.assert_called_once()
    drivers.delete_item.assert_called_once()
    dtc.delete_item.assert_called_once()
    devices.delete_item.assert_called_once()
    sns.delete_endpoint.assert_called_once_with(
        EndpointArn="arn:aws:sns:us-west-2:123:endpoint/APNS/app/id"
    )


def test_push_cleanup_skips_endpoint_when_none() -> None:
    """SNS delete_endpoint is not called when the query row has no endpoint_arn."""
    sns = _make_sns()
    devices = _make_ddb_table()
    devices.query.return_value = {
        "Items": [{"device_token": "tok"}]  # no endpoint_arn key
    }
    lv._push_cleanup(
        idp=_make_idp(), pool="p", user_email=None,
        drivers_table=_make_ddb_table(), driver_id=None,
        dtc_table=_make_ddb_table(), vehicle_id="V", dtc_ts_ms=None,
        dtc_source="s", sns_client=sns,
        devices_table=devices, owner_sub="sub-x",
    )
    sns.delete_endpoint.assert_not_called()


def test_push_cleanup_skips_device_row_when_no_sub() -> None:
    """devices.query is not called when owner_sub is None."""
    devices = _make_ddb_table()
    lv._push_cleanup(
        idp=_make_idp(), pool="p", user_email=None,
        drivers_table=_make_ddb_table(), driver_id=None,
        dtc_table=_make_ddb_table(), vehicle_id="V", dtc_ts_ms=None,
        dtc_source="s", sns_client=_make_sns(),
        devices_table=devices, owner_sub=None,
    )
    devices.query.assert_not_called()
    devices.delete_item.assert_not_called()


def test_push_cleanup_no_short_circuit_on_sns_failure(capsys) -> None:
    """SNS failure must not skip the device row or Cognito delete."""
    sns = _make_sns()
    sns.delete_endpoint.side_effect = RuntimeError("SNS down")
    devices = _make_ddb_table()
    devices.query.return_value = {
        "Items": [{"endpoint_arn": "arn:aws:sns:us-west-2:123:endpoint/APNS/app/id",
                   "device_token": "tok"}]
    }
    idp = _make_idp()
    lv._push_cleanup(
        idp=idp, pool="pool-1", user_email="u@example.com",
        drivers_table=_make_ddb_table(), driver_id=None,
        dtc_table=_make_ddb_table(), vehicle_id="V", dtc_ts_ms=None,
        dtc_source="s",
        sns_client=sns,
        devices_table=devices, owner_sub="sub-x",
    )
    devices.delete_item.assert_called_once()
    idp.admin_delete_user.assert_called_once()
    assert "SNS down" in capsys.readouterr().err


def test_push_cleanup_only_deletes_manifest_keyed_endpoint(capsys) -> None:
    """Only the endpoint ARN found in the device row query is deleted.

    Mutation M-W4e: use a hard-coded ARN instead of the one from the query row.
    The assert_called_once_with then fails because the wrong ARN was used.
    """
    sns = _make_sns()
    sentinel = "arn:aws:sns:us-west-2:999:endpoint/APNS_SANDBOX/app/MANIFEST-ID"
    devices = _make_ddb_table()
    devices.query.return_value = {
        "Items": [{"endpoint_arn": sentinel, "device_token": "tok"}]
    }
    lv._push_cleanup(
        idp=_make_idp(), pool="p", user_email=None,
        drivers_table=_make_ddb_table(), driver_id=None,
        dtc_table=_make_ddb_table(), vehicle_id="V", dtc_ts_ms=None,
        dtc_source="s", sns_client=sns,
        devices_table=devices, owner_sub="sub-sentinel",
    )
    sns.delete_endpoint.assert_called_once_with(EndpointArn=sentinel)


# ---------------------------------------------------------------------------
# 3. validate_run_id
# ---------------------------------------------------------------------------

def test_valid_run_id_accepted() -> None:
    rid = "lv-pp0-20261106T120000Z-a1b2c3"
    assert lv.validate_run_id(rid) == rid


def test_invalid_run_id_raises() -> None:
    with pytest.raises(SystemExit):
        lv.validate_run_id("bad-id")


def test_new_run_id_passes_validation() -> None:
    rid = lv.new_run_id()
    assert lv.validate_run_id(rid) == rid


def test_run_id_different_on_each_call() -> None:
    assert lv.new_run_id() != lv.new_run_id()


# ---------------------------------------------------------------------------
# 4. save_manifest + _manifest_path
# ---------------------------------------------------------------------------

def test_save_manifest_creates_0600_file(tmp_path) -> None:
    m = {"run_id": "lv-pp0-20261106T120000Z-aabbcc", "stage": "staging"}
    path = lv.save_manifest(m, base=tmp_path)
    assert path.exists()
    assert oct(path.stat().st_mode & 0o777) == oct(0o600)
    assert json.loads(path.read_text())["run_id"] == m["run_id"]


def test_save_manifest_creates_0700_dir(tmp_path) -> None:
    m = {"run_id": "lv-pp0-20261106T120000Z-ddeeff", "stage": "staging"}
    parent = tmp_path / "sub"
    lv.save_manifest(m, base=parent)
    assert oct(parent.stat().st_mode & 0o777) == oct(0o700)


def test_manifest_path_uses_run_id(tmp_path) -> None:
    rid = "lv-pp0-20261106T130000Z-112233"
    path = lv._manifest_path(rid, tmp_path)
    assert path.name == f"{rid}.json"


# ---------------------------------------------------------------------------
# 5. _voice_cleanup: no short-circuit on exception
# ---------------------------------------------------------------------------

def _make_idp(*, fail_delete: bool = False) -> MagicMock:
    idp = MagicMock()
    if fail_delete:
        idp.admin_delete_user.side_effect = RuntimeError("IDP down")
    return idp


def _make_table(*, fail_delete: bool = False) -> MagicMock:
    t = MagicMock()
    if fail_delete:
        t.delete_item.side_effect = RuntimeError("DDB throttled")
    return t


def test_cleanup_calls_both_deletes_on_success() -> None:
    idp = _make_idp()
    drivers = _make_table()
    dtc = _make_table()
    lv._voice_cleanup(
        idp=idp, pool="pool-1", user_email="u@example.com",
        drivers_table=drivers, driver_id="DRV-LV-001",
        dtc_table=dtc, vehicle_id="VEH-T01",
        dtc_ts_ms=1_700_000_000_000, dtc_source="src-abc",
    )
    idp.admin_delete_user.assert_called_once()
    drivers.delete_item.assert_called_once()
    dtc.delete_item.assert_called_once()


def test_cleanup_still_deletes_dtc_when_cognito_raises(capsys) -> None:
    """Cognito failure must not skip DTC cleanup."""
    idp = _make_idp(fail_delete=True)
    dtc = _make_table()
    lv._voice_cleanup(
        idp=idp, pool="pool-1", user_email="u@example.com",
        drivers_table=_make_table(), driver_id=None,
        dtc_table=dtc, vehicle_id="VEH-T01",
        dtc_ts_ms=1_700_000_000_001, dtc_source="src-xyz",
    )
    dtc.delete_item.assert_called_once()
    assert "u@example.com" in capsys.readouterr().err


def test_cleanup_still_deletes_user_when_dtc_raises(capsys) -> None:
    """DTC failure must not skip Cognito cleanup."""
    idp = _make_idp()
    dtc = _make_table(fail_delete=True)
    lv._voice_cleanup(
        idp=idp, pool="pool-1", user_email="u@example.com",
        drivers_table=_make_table(), driver_id=None,
        dtc_table=dtc, vehicle_id="VEH-T01",
        dtc_ts_ms=1_700_000_000_002, dtc_source="src-lmn",
    )
    idp.admin_delete_user.assert_called_once()


def test_cleanup_skips_dtc_when_ts_is_none() -> None:
    """DTC row was never written (e.g., exception before seed) — skip delete."""
    dtc = _make_table()
    lv._voice_cleanup(
        idp=_make_idp(), pool="pool-1", user_email=None,
        drivers_table=_make_table(), driver_id=None,
        dtc_table=dtc, vehicle_id="VEH-T01",
        dtc_ts_ms=None, dtc_source="src",
    )
    dtc.delete_item.assert_not_called()


def test_cleanup_skips_cognito_when_user_is_none() -> None:
    idp = _make_idp()
    lv._voice_cleanup(
        idp=idp, pool="pool-1", user_email=None,
        drivers_table=_make_table(), driver_id=None,
        dtc_table=_make_table(), vehicle_id="VEH-T01",
        dtc_ts_ms=None, dtc_source="src",
    )
    idp.admin_delete_user.assert_not_called()


def test_cleanup_prints_manual_command_on_dtc_delete_failure(capsys) -> None:
    dtc = _make_table(fail_delete=True)
    lv._voice_cleanup(
        idp=_make_idp(), pool="p", user_email=None,
        drivers_table=_make_table(), driver_id=None,
        dtc_table=dtc, vehicle_id="VEH-MANUAL", dtc_ts_ms=9999, dtc_source="s",
    )
    err = capsys.readouterr().err
    assert "VEH-MANUAL" in err
    assert "delete-item" in err


# ---------------------------------------------------------------------------
# 6. Cleanup wired to finally — SIGTERM resilience (structural test)
# ---------------------------------------------------------------------------

def test_cleanup_called_from_finally_on_inner_exception(monkeypatch) -> None:
    """_voice_cleanup must be called even when the inner session code raises.

    This is the structural analog of the SIGTERM test: if ``_voice_cleanup``
    were in the ``try`` body (not the ``finally``), an exception in the session
    body would skip it.  We simulate that by making the first AWS call raise
    and asserting cleanup still runs.

    Mutant tried: move ``_voice_cleanup(...)`` from the ``finally`` block to
    after the ``async with`` (try body).  This test then FAILS because the
    exception from ``admin_get_user`` propagates before cleanup.
    """
    import asyncio as _asyncio

    monkeypatch.setenv("LIVE_TESTS", "1")

    # Track whether _voice_cleanup was invoked.
    cleanup_calls: list[dict] = []

    def _fake_cleanup(**kw) -> None:
        cleanup_calls.append(kw)

    monkeypatch.setattr(lv, "_voice_cleanup", _fake_cleanup)

    # boto3 raises on the very first client call (get_parameter for the pool id)
    # so the inner try raises before cognito_created is ever True.
    class _BoomClient:
        def get_parameter(self, **_kw):
            raise RuntimeError("simulated SSM failure")

        def __getattr__(self, name):
            return MagicMock(side_effect=RuntimeError(f"unexpected {name}"))

    def _fake_client(service: str, **_kw):
        return _BoomClient()

    fake_boto3 = MagicMock()
    fake_boto3.client.side_effect = _fake_client
    fake_boto3.resource.return_value = MagicMock()
    monkeypatch.setattr(lv, "boto3", fake_boto3)

    import argparse
    args = argparse.Namespace(
        stage="staging", vehicle_id="VEH-TEST-999", vin="ZZZ00000000000001",
        run_id=None,
    )

    with pytest.raises(RuntimeError, match="simulated SSM failure"):
        _asyncio.run(lv._voice_run(args))

    # The finally block must have fired and called _voice_cleanup.
    assert len(cleanup_calls) == 1, (
        "_voice_cleanup must be in the finally block, not the try body"
    )


# ===========================================================================
# F2.3-F2.5 W2 + W3 + W4 + suggestion tests  (no AWS; stubbed)
# ===========================================================================

# ---------------------------------------------------------------------------
# W2 – every cleanup ConditionExpression names attributes via
#       ExpressionAttributeNames (#src alias; 'source' is a reserved word)
# ---------------------------------------------------------------------------

def _capture_delete_kwargs(table: MagicMock) -> list[dict]:
    """Return the kwargs captured from every delete_item call on *table*."""
    calls: list[dict] = []
    table.delete_item.side_effect = lambda **kw: calls.append(kw)
    return calls


def test_voice_cleanup_driver_condition_uses_expression_attribute_names() -> None:
    """_voice_cleanup driver-row delete must use ExpressionAttributeNames for 'source'.

    Mutation M-W2v: remove ExpressionAttributeNames from the drivers call.
    With that mutation the ConditionExpression contains 'source' bare, which
    DynamoDB rejects as a reserved word.  This test catches that by asserting
    the alias is present and 'source' is absent unescaped in the expression.
    """
    driver_calls: list[dict] = []
    drivers = MagicMock()
    drivers.delete_item.side_effect = lambda **kw: driver_calls.append(kw)

    lv._voice_cleanup(
        idp=_make_idp(), pool="p", user_email=None,
        drivers_table=drivers, driver_id="DRV-W2-001",
        dtc_table=_make_table(), vehicle_id="VEH-T01",
        dtc_ts_ms=None, dtc_source="src-w2",
    )
    assert driver_calls, "drivers.delete_item should have been called"
    kw = driver_calls[0]
    assert "ExpressionAttributeNames" in kw, (
        "ExpressionAttributeNames must be present for the driver-row delete; "
        "'source' is a DynamoDB reserved word"
    )
    alias = "#src"
    assert alias in kw["ExpressionAttributeNames"], (
        f"ExpressionAttributeNames must map {alias!r} to 'source'"
    )
    cond = kw.get("ConditionExpression", "")
    assert "source" not in cond.replace(alias, ""), (
        "bare 'source' must not appear in ConditionExpression; escape it via #src"
    )
    assert alias in cond, (
        f"alias {alias!r} must appear in ConditionExpression"
    )


def test_push_cleanup_driver_condition_uses_expression_attribute_names() -> None:
    """_push_cleanup driver-row delete must use ExpressionAttributeNames.

    Mutation M-W2p: remove ExpressionAttributeNames from _push_cleanup.
    """
    driver_calls: list[dict] = []
    drivers = MagicMock()
    drivers.delete_item.side_effect = lambda **kw: driver_calls.append(kw)

    lv._push_cleanup(
        idp=_make_idp(), pool="p", user_email=None,
        drivers_table=drivers, driver_id="DRV-PUSH-W2",
        dtc_table=_make_ddb_table(), vehicle_id="VEH-T01",
        dtc_ts_ms=None, dtc_source="src-w2-push",
        sns_client=_make_sns(),
        devices_table=_make_ddb_table(), owner_sub=None,
    )
    assert driver_calls, "drivers.delete_item should have been called"
    kw = driver_calls[0]
    assert "ExpressionAttributeNames" in kw, (
        "ExpressionAttributeNames must be present for _push_cleanup driver-row delete"
    )
    alias = "#src"
    cond = kw.get("ConditionExpression", "")
    assert alias in cond
    assert "source" not in cond.replace(alias, ""), (
        "bare 'source' must not appear in ConditionExpression"
    )


def test_voice_cleanup_driver_failure_prints_manual_command(capsys) -> None:
    """On driver-row delete failure _voice_cleanup prints a manual delete command.

    Mutation M-W2v-cmd: remove the manual-command print from the except block.
    The assert then fails because the table name or driverId is absent from stderr.
    """
    drivers = _make_table(fail_delete=True)
    lv._voice_cleanup(
        idp=_make_idp(), pool="p", user_email=None,
        drivers_table=drivers, driver_id="DRV-MANUAL-VOICE",
        dtc_table=_make_table(), vehicle_id="VEH-T01",
        dtc_ts_ms=None, dtc_source="src-cmd",
    )
    err = capsys.readouterr().err
    assert "DRV-MANUAL-VOICE" in err, "driver id must appear in manual command output"
    assert "delete-item" in err, "manual aws dynamodb delete-item must be printed"
    assert "storage-drivers" in err, "table name must appear in manual command"


def test_push_cleanup_driver_failure_prints_manual_command(capsys) -> None:
    """On driver-row delete failure _push_cleanup prints a manual delete command."""
    drivers = _make_ddb_table(fail=True)
    lv._push_cleanup(
        idp=_make_idp(), pool="p", user_email=None,
        drivers_table=drivers, driver_id="DRV-MANUAL-PUSH",
        dtc_table=_make_ddb_table(), vehicle_id="VEH-T01",
        dtc_ts_ms=None, dtc_source="src-push-cmd",
        sns_client=_make_sns(),
        devices_table=_make_ddb_table(), owner_sub=None,
    )
    err = capsys.readouterr().err
    assert "DRV-MANUAL-PUSH" in err
    assert "delete-item" in err
    assert "storage-drivers" in err


# ---------------------------------------------------------------------------
# W3 – _guard_active_critical_dtc
# ---------------------------------------------------------------------------

def _make_dtc_table_with_rows(rows: list[dict]) -> MagicMock:
    t = MagicMock()
    t.query.return_value = {"Items": rows}
    return t


def test_guard_active_critical_dtc_refuses_active_critical() -> None:
    """A car with an ACTIVE CRITICAL row is refused before any write.

    Mutation M-W3c: change ``rows`` check to ``not rows`` (never refuse).
    The test then fails because SystemExit is never raised.
    """
    dtc = _make_dtc_table_with_rows([
        {"vehicleId": "V", "code": "P0300", "severity": "CRITICAL", "status": "ACTIVE"},
    ])
    with pytest.raises(SystemExit) as exc:
        lv._guard_active_critical_dtc("VEH-T01", dtc)
    assert exc.value.code != 0


def test_guard_active_critical_dtc_refuses_active_high() -> None:
    """A car with an ACTIVE HIGH row is also refused.

    Mutation M-W3h: remove HIGH from the FilterExpression.
    """
    dtc = _make_dtc_table_with_rows([
        {"vehicleId": "V", "code": "P0200", "severity": "HIGH", "status": "ACTIVE"},
    ])
    with pytest.raises(SystemExit) as exc:
        lv._guard_active_critical_dtc("VEH-T01", dtc)
    assert exc.value.code != 0


def test_guard_active_critical_dtc_passes_clean_car() -> None:
    """A car with no ACTIVE CRITICAL/HIGH rows passes the guard without raising."""
    dtc = _make_dtc_table_with_rows([])
    # Must not raise.
    lv._guard_active_critical_dtc("VEH-T01", dtc)


# ---------------------------------------------------------------------------
# W3 – _check_residual_findings
# ---------------------------------------------------------------------------

def test_check_residual_findings_reports_when_found(capsys) -> None:
    """Residual Findings with computed_at after started_ms are reported to stderr
    and keyed into the manifest.

    Stub data shaped like the real writer: agent-2-vehicle-health Findings carry
    ``computed_at`` (ISO-8601 UTC), not ``created_ms``.

    Mutation M-W3r: remove the manifest update line.
    The second assert then fails.
    """
    # 1_700_000_000_000 ms = 2023-11-14T22:13:20Z.  Use a computed_at 1 s later.
    findings_table = MagicMock()
    findings_table.query.return_value = {
        "Items": [
            {
                "finding_id": "f-001",
                "severity": "p0",
                "agent_id": "agent-2-vehicle-health",
                "computed_at": "2023-11-14T22:13:21+00:00",
            },
        ]
    }
    manifest: dict = {"run_id": "lv-pp0-20261106T120000Z-aabbcc"}
    lv._check_residual_findings(findings_table, "VIN-001", 1_700_000_000_000, manifest)
    err = capsys.readouterr().err
    assert "f-001" in err
    assert "residual_finding_ids" in manifest, (
        "manifest must record residual finding IDs"
    )
    assert "f-001" in manifest["residual_finding_ids"]


def test_check_residual_findings_silent_when_none(capsys) -> None:
    """No output and no manifest key when there are no residual Findings."""
    findings_table = MagicMock()
    findings_table.query.return_value = {"Items": []}
    manifest: dict = {}
    lv._check_residual_findings(findings_table, "VIN-001", 1_700_000_000_000, manifest)
    assert capsys.readouterr().err == ""
    assert "residual_finding_ids" not in manifest


# ---------------------------------------------------------------------------
# W4 – _apply_open_reason_header
# ---------------------------------------------------------------------------

def test_apply_open_reason_header_sets_header_for_p0_push() -> None:
    """When open_reason is 'p0_push' the OpenReason header is added to headers.

    Mutation M-W4h: change ``== "p0_push"`` to ``is not None``.
    test_apply_open_reason_header_not_set_for_other catches that mutation
    (any non-p0_push value still sets the header).
    """
    h: dict[str, str] = {}
    lv._apply_open_reason_header(h, "p0_push")
    assert lv._OPEN_REASON_HEADER in h, (
        f"header {lv._OPEN_REASON_HEADER!r} must be set when open_reason='p0_push'"
    )
    assert h[lv._OPEN_REASON_HEADER] == "p0_push"


def test_apply_open_reason_header_not_set_for_none() -> None:
    """No header is added when open_reason is None.

    Mutation M-W4n: change ``if open_reason == "p0_push":`` to ``if True:``.
    This test catches that mutation (header is unexpectedly set).
    """
    h: dict[str, str] = {}
    lv._apply_open_reason_header(h, None)
    assert lv._OPEN_REASON_HEADER not in h, (
        "OpenReason header must not be set when open_reason is None"
    )


def test_apply_open_reason_header_not_set_for_other_value() -> None:
    """No header is added when open_reason is an unrecognised value.

    Mutation M-W4o: change ``== "p0_push"`` to truthy check ``if open_reason:``.
    This test catches that mutation.
    """
    h: dict[str, str] = {}
    lv._apply_open_reason_header(h, "unknown_reason")
    assert lv._OPEN_REASON_HEADER not in h


def test_open_reason_p0_push_arg_accepted(monkeypatch, _no_aws) -> None:
    """``--open-reason p0_push`` is a valid CLI argument for the voice subcommand.

    If argparse exits with code 2 (unknown argument), this test fails.
    If the arg is valid it propagates past argparse to the guards/asyncio gate.
    """
    monkeypatch.setenv("LIVE_TESTS", "1")
    # asyncio.run is blocked by _no_aws; we expect its AssertionError to
    # propagate (proving we got past argparse without code-2).
    with pytest.raises(AssertionError, match="asyncio.run called before gates"):
        lv.main(["voice", "--vehicle-id", "VEH-TEST-001", "--open-reason", "p0_push"])


# ---------------------------------------------------------------------------
# Suggestion – _try_decode_frame (skip non-JSON frames)
# ---------------------------------------------------------------------------

def test_try_decode_frame_returns_none_for_non_json() -> None:
    """Non-JSON frames return None so _recv can skip them.

    Mutation M-nj: change ``return None`` to ``return {}``.
    ``_recv`` would then treat the non-JSON frame as a valid empty event
    (appended to events and evaluated as not P0).  This test catches that.
    """
    assert lv._try_decode_frame("not-json-content") is None
    assert lv._try_decode_frame("") is None
    assert lv._try_decode_frame("PING") is None
    assert lv._try_decode_frame("<<binary-frame>>") is None


def test_try_decode_frame_returns_dict_for_valid_json() -> None:
    """Valid JSON frames are decoded and returned as dicts."""
    result = lv._try_decode_frame('{"type": "classification", "value": "P0"}')
    assert result == {"type": "classification", "value": "P0"}

    result2 = lv._try_decode_frame('{"type": "transcript", "isFinal": true}')
    assert result2 == {"type": "transcript", "isFinal": True}


# ---------------------------------------------------------------------------
# Suggestion – _evaluate_p0_log_lines (assert alerted / table-unavailable)
# ---------------------------------------------------------------------------

def test_evaluate_alerted_check_true_when_log_line_present() -> None:
    """'proactive-p0: alerted logged' is True when the log line is present.

    Mutation M-al: change ``"proactive-p0: alerted" in ln`` to ``not in ln``.
    This test catches that mutation.
    """
    p0_lines = [
        "2026-11-06T12:00:01Z proactive-p0: alerted codes=['C1234'] session=s1",
    ]
    checks = lv._evaluate_p0_log_lines(p0_lines, p0_lines, None)
    assert checks["proactive-p0: alerted logged"] is True


def test_evaluate_alerted_check_false_when_absent() -> None:
    """'proactive-p0: alerted logged' is False when the log line is absent."""
    checks = lv._evaluate_p0_log_lines([], [], None)
    assert checks["proactive-p0: alerted logged"] is False


def test_evaluate_table_unavailable_absent_when_no_error_line() -> None:
    """'severity table unavailable absent' is True when the error line is absent.

    Mutation M-tu: change ``not any(...)`` to ``any(...)``.
    This test catches that mutation (True becomes False with no matching lines).
    """
    checks = lv._evaluate_p0_log_lines([], [], None)
    assert checks["severity table unavailable absent"] is True


def test_evaluate_table_unavailable_absent_false_when_error_line_present() -> None:
    """'severity table unavailable absent' is False when the error line is present."""
    p0_lines = [
        "2026-11-06T12:00:01Z proactive-p0: severity table unavailable; "
        "using classifier only",
    ]
    checks = lv._evaluate_p0_log_lines(p0_lines, p0_lines, None)
    assert checks["severity table unavailable absent"] is False


def test_evaluate_open_reason_check_added_and_true_for_p0_push() -> None:
    """'bidi: openReason=p0_push logged' is True when present and open_reason=p0_push.

    Mutation M-or: change ``"openReason=p0_push" in ln`` to ``False``.
    This test catches that mutation.
    """
    log_lines = [
        "2026-11-06T12:00:01Z bidi: openReason=p0_push session=sess-1",
    ]
    checks = lv._evaluate_p0_log_lines([], log_lines, "p0_push")
    assert "bidi: openReason=p0_push logged" in checks
    assert checks["bidi: openReason=p0_push logged"] is True


def test_evaluate_open_reason_check_false_when_log_line_absent() -> None:
    """'bidi: openReason=p0_push logged' is False when absent for p0_push."""
    checks = lv._evaluate_p0_log_lines([], [], "p0_push")
    assert checks["bidi: openReason=p0_push logged"] is False


def test_evaluate_open_reason_check_not_added_when_open_reason_none() -> None:
    """'bidi: openReason=p0_push logged' check is not added when open_reason is None.

    Mutation M-or-scope: always add the openReason check regardless of open_reason.
    The caller then gets an unexpected FAIL for a normal voice run.
    This test catches that mutation.
    """
    log_lines = [
        "2026-11-06T12:00:01Z bidi: openReason=p0_push session=sess-1",
    ]
    checks = lv._evaluate_p0_log_lines([], log_lines, None)
    assert "bidi: openReason=p0_push logged" not in checks, (
        "the openReason check must only be added when open_reason='p0_push'"
    )


# ===========================================================================
# New tests for cycle-3 Warnings + Suggestions (W1, W2, W3, W4, S1, S3)
# ===========================================================================

# ---------------------------------------------------------------------------
# W1 — _evaluate_p0_log_lines: alerted (no table) must NOT pass alerted check
# ---------------------------------------------------------------------------

def test_evaluate_alerted_check_false_for_no_table_variant() -> None:
    """'proactive-p0: alerted logged' is False when the line is the no-table fallback.

    The no-table alert line is:
        ``proactive-p0: alerted (no table) codes=['C1234'] session=...``
    It does NOT contain ``alerted codes=``, so the W1 fix (match ``alerted codes=``)
    correctly returns False for this line, distinguishing it from a real table-backed
    alert.

    Mutation M-W1: change ``"proactive-p0: alerted codes=" in ln`` back to
    ``"proactive-p0: alerted" in ln``.  With that mutation the no-table line
    also matches and this test fails (True, not False).
    """
    no_table_line = (
        "2026-11-06T12:00:01Z proactive-p0: alerted (no table) "
        "codes=['C1234'] session=sess-1"
    )
    checks = lv._evaluate_p0_log_lines([no_table_line], [no_table_line], None)
    assert checks["proactive-p0: alerted logged"] is False, (
        "alerted (no table) must NOT pass the alerted check; "
        "only 'alerted codes=' (table-backed) is a valid alert"
    )


def test_evaluate_alerted_check_true_for_real_table_alert() -> None:
    """'proactive-p0: alerted logged' is True for the table-backed alert line.

    The real table-backed alert line is:
        ``proactive-p0: alerted codes=['C1234'] session=...``
    This contains ``alerted codes=`` and must pass.
    """
    real_alert = (
        "2026-11-06T12:00:01Z proactive-p0: alerted codes=['C1234'] "
        "session=sess-1"
    )
    checks = lv._evaluate_p0_log_lines([real_alert], [real_alert], None)
    assert checks["proactive-p0: alerted logged"] is True


# ---------------------------------------------------------------------------
# W2 — _computed_at_ge helper
# ---------------------------------------------------------------------------

def test_computed_at_ge_returns_true_when_after() -> None:
    """Returns True when computed_at is strictly after since.

    Mutation M-W2ge: change ``dt >= since`` to ``dt <= since``.
    An after-value would then return False and this test fails.
    """
    from datetime import datetime, timezone
    since = datetime(2023, 11, 14, 22, 13, 20, tzinfo=timezone.utc)
    assert lv._computed_at_ge("2023-11-14T22:13:21+00:00", since) is True


def test_computed_at_ge_returns_true_when_equal() -> None:
    """Returns True when computed_at exactly equals since (boundary case)."""
    from datetime import datetime, timezone
    since = datetime(2023, 11, 14, 22, 13, 20, tzinfo=timezone.utc)
    assert lv._computed_at_ge("2023-11-14T22:13:20+00:00", since) is True


def test_computed_at_ge_returns_false_when_before() -> None:
    """Returns False when computed_at is before since."""
    from datetime import datetime, timezone
    since = datetime(2023, 11, 14, 22, 13, 20, tzinfo=timezone.utc)
    assert lv._computed_at_ge("2023-11-14T22:13:19+00:00", since) is False


def test_computed_at_ge_returns_false_for_empty() -> None:
    """Returns False for absent/empty computed_at."""
    from datetime import datetime, timezone
    since = datetime(2023, 11, 14, 22, 13, 20, tzinfo=timezone.utc)
    assert lv._computed_at_ge("", since) is False
    assert lv._computed_at_ge(None, since) is False  # type: ignore[arg-type]


def test_computed_at_ge_returns_false_for_unparseable() -> None:
    """Returns False for a value that is not valid ISO-8601."""
    from datetime import datetime, timezone
    since = datetime(2023, 11, 14, tzinfo=timezone.utc)
    assert lv._computed_at_ge("not-a-date", since) is False


def test_check_residual_findings_ignores_stale_items(capsys) -> None:
    """Findings with computed_at BEFORE started_ms are silently ignored.

    Mutation M-W2stale: remove the _computed_at_ge client-side filter
    (keep all items from the DDB result).  The stale item would then be
    reported and this test fails on the 'no err output' assertion.
    """
    findings_table = MagicMock()
    # computed_at is 1 second BEFORE started_ms (2023-11-14T22:13:20Z)
    findings_table.query.return_value = {
        "Items": [
            {
                "finding_id": "f-stale",
                "severity": "p0",
                "agent_id": "agent-2-vehicle-health",
                "computed_at": "2023-11-14T22:13:19+00:00",
            },
        ]
    }
    manifest: dict = {"run_id": "lv-pp0-20261106T120000Z-aabbcc"}
    lv._check_residual_findings(findings_table, "VIN-001", 1_700_000_000_000, manifest)
    assert capsys.readouterr().err == "", "stale finding must not be reported"
    assert "residual_finding_ids" not in manifest


def test_check_residual_findings_query_has_no_timestamp_filter() -> None:
    """DDB query does NOT include a timestamp FilterExpression (comparison done client-side).

    Mutation M-W2flt: add ``created_ms >= :t`` back to the DDB filter.
    The DDB query returns nothing (created_ms key absent), and the test
    fails because no item is reported.  This test verifies the query
    keys are limited to vin + agent_id.
    """
    findings_table = MagicMock()
    findings_table.query.return_value = {
        "Items": [
            {
                "finding_id": "f-002",
                "severity": "p0",
                "agent_id": "agent-2-vehicle-health",
                "computed_at": "2023-11-14T22:13:21+00:00",
            },
        ]
    }
    manifest: dict = {"run_id": "lv-pp0-20261106T120000Z-aabbcc"}
    lv._check_residual_findings(findings_table, "VIN-001", 1_700_000_000_000, manifest)
    call_kwargs = findings_table.query.call_args[1]
    expr_vals = call_kwargs.get("ExpressionAttributeValues", {})
    # No timestamp value in the DDB query
    assert not any(
        isinstance(v, int) and v > 1_000_000_000 for v in expr_vals.values()
    ), "DDB query must not include a numeric timestamp filter; compare client-side"


# ---------------------------------------------------------------------------
# W3 — _guard_no_assigned_drivers
# ---------------------------------------------------------------------------

def test_guard_no_assigned_drivers_refuses_existing_driver() -> None:
    """Refuses if any driver row has the car as assignedVehicleId.

    Mutation M-W3n: change ``if conflicting:`` to ``if not conflicting:``.
    With this mutation a clean car (no drivers) causes refusal and a car
    with a driver passes — this test fails because SystemExit is not raised.
    """
    drivers = MagicMock()
    drivers.scan.return_value = {
        "Items": [
            {"driverId": "DRV-001",
             "assignedVehicleId": "VEH-T01",
             "cognitoEmail": "d@example.com"},
        ],
    }
    with pytest.raises(SystemExit) as exc:
        lv._guard_no_assigned_drivers("VEH-T01", drivers)
    assert exc.value.code != 0


def test_guard_no_assigned_drivers_passes_clean_car() -> None:
    """Passes without raising when no driver row names the car."""
    drivers = MagicMock()
    drivers.scan.return_value = {"Items": []}
    lv._guard_no_assigned_drivers("VEH-T01", drivers)  # must not raise


def test_guard_no_assigned_drivers_paginates() -> None:
    """Pages through scan results until LastEvaluatedKey is absent.

    Mutation M-W3pg: remove the pagination loop (never follow LastEvaluatedKey).
    With this mutation, only the first page is checked and the conflict on
    page 2 is missed.  This test verifies scan is called twice.
    """
    drivers = MagicMock()
    # Page 1: empty, but has more pages.
    # Page 2: empty, no more pages.
    drivers.scan.side_effect = [
        {"Items": [], "LastEvaluatedKey": {"driverId": {"S": "x"}}},
        {"Items": []},
    ]
    lv._guard_no_assigned_drivers("VEH-T01", drivers)
    assert drivers.scan.call_count == 2, (
        "scan must be called at least twice when LastEvaluatedKey is present"
    )


def test_guard_no_assigned_drivers_pagination_catches_conflict_on_page2() -> None:
    """A conflict on the second scan page is detected and causes refusal."""
    drivers = MagicMock()
    drivers.scan.side_effect = [
        {"Items": [], "LastEvaluatedKey": {"driverId": {"S": "x"}}},
        {"Items": [{"driverId": "DRV-PAGE2",
                    "assignedVehicleId": "VEH-T01",
                    "cognitoEmail": "late@example.com"}]},
    ]
    with pytest.raises(SystemExit) as exc:
        lv._guard_no_assigned_drivers("VEH-T01", drivers)
    assert exc.value.code != 0


# ---------------------------------------------------------------------------
# W4 — _push_cleanup queries owner devices; prints manual commands on failure
# ---------------------------------------------------------------------------

def test_push_cleanup_queries_owner_devices_when_sub_known() -> None:
    """When owner_sub is known, devices_table.query is called with owner_id filter.

    Mutation M-W4q: replace the devices_table.query call with no-op.
    With M-W4q, devices.query is never called and the assertion fails.
    """
    devices = _make_ddb_table()
    devices.query.return_value = {"Items": []}
    lv._push_cleanup(
        idp=_make_idp(), pool="p", user_email=None,
        drivers_table=_make_ddb_table(), driver_id=None,
        dtc_table=_make_ddb_table(), vehicle_id="V", dtc_ts_ms=None,
        dtc_source="s", sns_client=_make_sns(),
        devices_table=devices, owner_sub="sub-known",
    )
    devices.query.assert_called_once()
    call_kwargs = devices.query.call_args[1]
    assert ":o" in call_kwargs.get("ExpressionAttributeValues", {}), (
        "query must filter by owner_id"
    )
    assert call_kwargs["ExpressionAttributeValues"][":o"] == "sub-known"


def test_push_cleanup_endpoint_found_via_query_when_registration_partly_failed(capsys) -> None:
    """With owner_sub known, the device-row query finds the endpoint and cleanup
    deletes it (the row exists although push_run never saw a 2xx, e.g. a lost
    HTTP response).

    This test calls _push_cleanup directly. The push_run wiring (owner_sub
    passed on every path) and the CustomUserData sweep for the no-row case are
    pinned by the push_run tests at the end of this file (Fix Group 4:
    Morphwire, Msweep).
    """
    orphan_arn = "arn:aws:sns:us-west-2:123:endpoint/APNS/app/ORPHAN"
    devices = _make_ddb_table()
    devices.query.return_value = {
        "Items": [{"endpoint_arn": orphan_arn, "device_token": "tok-orphan"}]
    }
    sns = _make_sns()
    lv._push_cleanup(
        idp=_make_idp(), pool="p", user_email=None,
        drivers_table=_make_ddb_table(), driver_id=None,
        dtc_table=_make_ddb_table(), vehicle_id="V", dtc_ts_ms=None,
        dtc_source="s", sns_client=sns,
        devices_table=devices, owner_sub="sub-partial",
    )
    sns.delete_endpoint.assert_called_once_with(EndpointArn=orphan_arn)
    devices.delete_item.assert_called_once()


def test_push_cleanup_prints_manual_commands_on_device_delete_failure(capsys) -> None:
    """On device row and SNS delete failure, manual AWS CLI commands are printed.

    Mutation M-W4cmd: remove the manual-command print in the except blocks.
    The assert on 'delete-endpoint' in err fails.
    """
    sns = _make_sns()
    sns.delete_endpoint.side_effect = RuntimeError("SNS throttled")
    devices = _make_ddb_table()
    devices.query.return_value = {
        "Items": [{"endpoint_arn": "arn:aws:sns:us-west-2:1:endpoint/APNS/a/x",
                   "device_token": "tok-fail"}]
    }
    devices.delete_item.side_effect = RuntimeError("DDB throttled")

    lv._push_cleanup(
        idp=_make_idp(), pool="p", user_email=None,
        drivers_table=_make_ddb_table(), driver_id=None,
        dtc_table=_make_ddb_table(), vehicle_id="V", dtc_ts_ms=None,
        dtc_source="s", sns_client=sns,
        devices_table=devices, owner_sub="sub-fail",
    )
    err = capsys.readouterr().err
    assert "delete-endpoint" in err, "aws sns delete-endpoint must be in manual command output"
    assert "delete-item" in err, "aws dynamodb delete-item must be in manual command output"
    assert "notification-devices" in err, "table name must appear in manual command"


# ---------------------------------------------------------------------------
# S1 — DTC deletes in both cleanups use ExpressionAttributeNames
# ---------------------------------------------------------------------------

def test_voice_cleanup_dtc_delete_uses_expression_attribute_names() -> None:
    """_voice_cleanup DTC-row delete must use ExpressionAttributeNames for 'source'.

    Mutation MDTCv: remove ExpressionAttributeNames from the DTC delete call in
    _voice_cleanup.  DynamoDB would reject the bare 'source' as a reserved word.
    This test catches MDTCv by asserting the alias is present.
    """
    dtc_calls: list[dict] = []
    dtc = _make_table()
    dtc.delete_item.side_effect = lambda **kw: dtc_calls.append(kw)

    lv._voice_cleanup(
        idp=_make_idp(), pool="p", user_email=None,
        drivers_table=_make_table(), driver_id=None,
        dtc_table=dtc, vehicle_id="VEH-S1",
        dtc_ts_ms=1_700_000_000_001, dtc_source="src-s1",
    )
    assert dtc_calls, "dtc.delete_item should have been called"
    kw = dtc_calls[0]
    assert "ExpressionAttributeNames" in kw, (
        "ExpressionAttributeNames must be present for the DTC-row delete in _voice_cleanup"
    )
    alias = "#src"
    assert alias in kw["ExpressionAttributeNames"], (
        f"ExpressionAttributeNames must map {alias!r} → 'source'"
    )
    cond = kw.get("ConditionExpression", "")
    assert alias in cond, f"alias {alias!r} must appear in ConditionExpression"
    assert "source" not in cond.replace(alias, ""), (
        "bare 'source' must not appear in ConditionExpression"
    )


def test_push_cleanup_dtc_delete_uses_expression_attribute_names() -> None:
    """_push_cleanup DTC-row delete must use ExpressionAttributeNames for 'source'.

    Mutation MDTCp: remove ExpressionAttributeNames from the DTC delete call in
    _push_cleanup.  This test catches MDTCp.
    """
    dtc_calls: list[dict] = []
    dtc = _make_ddb_table()
    dtc.delete_item.side_effect = lambda **kw: dtc_calls.append(kw)

    lv._push_cleanup(
        idp=_make_idp(), pool="p", user_email=None,
        drivers_table=_make_ddb_table(), driver_id=None,
        dtc_table=dtc, vehicle_id="VEH-S1P",
        dtc_ts_ms=1_700_000_000_002, dtc_source="src-s1-push",
        sns_client=_make_sns(),
        devices_table=_make_ddb_table(), owner_sub=None,
    )
    assert dtc_calls, "dtc.delete_item should have been called"
    kw = dtc_calls[0]
    assert "ExpressionAttributeNames" in kw, (
        "ExpressionAttributeNames must be present for the DTC-row delete in _push_cleanup"
    )
    alias = "#src"
    cond = kw.get("ConditionExpression", "")
    assert alias in cond
    assert "source" not in cond.replace(alias, ""), (
        "bare 'source' must not appear in ConditionExpression"
    )


# ---------------------------------------------------------------------------
# S3 — push precondition: only ResourceNotFoundException → exit 2
# ---------------------------------------------------------------------------

def test_push_precondition_resource_not_found_exits_2(monkeypatch) -> None:
    """ResourceNotFoundException from describe_secret exits 2 with no writes.

    Mutation M-S3: remove the ClientError.Code check; catch all ClientErrors.
    Both ResourceNotFoundException AND ThrottlingException would return 2.
    test_push_precondition_non_resource_not_found_propagates catches M-S3
    by asserting that ThrottlingException DOES propagate.
    """
    from botocore.exceptions import ClientError as BotoClientError
    monkeypatch.setenv("LIVE_TESTS", "1")
    write_calls: list[str] = []

    def _make_client(svc, **_kw):
        c = MagicMock()
        if svc == "secretsmanager":
            c.describe_secret.side_effect = BotoClientError(
                {"Error": {"Code": "ResourceNotFoundException",
                           "Message": "Secret not found"}},
                "DescribeSecret",
            )
        t = MagicMock()
        t.put_item.side_effect = lambda **_: write_calls.append("put")
        t.delete_item.side_effect = lambda **_: write_calls.append("del")
        c.Table = MagicMock(return_value=t)
        return c

    fake_boto3 = MagicMock()
    fake_boto3.client.side_effect = _make_client
    monkeypatch.setattr(lv, "boto3", fake_boto3)

    import argparse
    args = argparse.Namespace(
        stage="staging", vehicle_id="VEH-TEST-001",
        device_token="tok", vin=None, run_id=None,
    )
    result = lv.push_run(args)
    assert result == 2
    assert write_calls == [], "no writes on ResourceNotFoundException"


def test_push_precondition_non_resource_not_found_propagates(monkeypatch) -> None:
    """Non-ResourceNotFoundException errors from describe_secret re-raise (exit 1 path).

    Mutation M-S3: catch all ClientErrors as exit-2. With M-S3 the ThrottlingException
    is swallowed (returns 2) instead of propagating, so pytest.raises finds nothing and
    the test fails.
    """
    from botocore.exceptions import ClientError as BotoClientError
    monkeypatch.setenv("LIVE_TESTS", "1")

    def _make_client(svc, **_kw):
        c = MagicMock()
        if svc == "secretsmanager":
            c.describe_secret.side_effect = BotoClientError(
                {"Error": {"Code": "ThrottlingException",
                           "Message": "Rate exceeded"}},
                "DescribeSecret",
            )
        return c

    fake_boto3 = MagicMock()
    fake_boto3.client.side_effect = _make_client
    monkeypatch.setattr(lv, "boto3", fake_boto3)

    import argparse
    args = argparse.Namespace(
        stage="staging", vehicle_id="VEH-TEST-001",
        device_token="tok", vin=None, run_id=None,
    )
    with pytest.raises(BotoClientError) as exc_info:
        lv.push_run(args)
    assert exc_info.value.response["Error"]["Code"] == "ThrottlingException"


def _precondition_args():
    import argparse
    return argparse.Namespace(
        stage="staging", vehicle_id="VEH-TEST-001",
        device_token="tok", vin=None, run_id=None,
    )


@pytest.mark.parametrize("exc", [
    ConnectionError("network failure"),
    _NoCredentialsError(),
], ids=["ConnectionError", "NoCredentialsError"])
def test_push_precondition_non_boto_exception_propagates(monkeypatch, exc) -> None:
    """Only ResourceNotFoundException means "not exercised" (review cycle 4 S1).

    A network error or missing credentials from describe_secret propagates, so
    the run exits 1 instead of reporting a missing APNs key it never confirmed.
    Mutant MS1a (restore ``except Exception: return 2``) returns 2 → fails.
    """
    monkeypatch.setenv("LIVE_TESTS", "1")

    def _make_client(svc, **_kw):
        c = MagicMock()
        if svc == "secretsmanager":
            c.describe_secret.side_effect = exc
        return c

    fake_boto3 = MagicMock()
    fake_boto3.client.side_effect = _make_client
    monkeypatch.setattr(lv, "boto3", fake_boto3)

    with pytest.raises(type(exc)):
        lv.push_run(_precondition_args())


def test_push_precondition_register_device_function_missing_exits_2(monkeypatch) -> None:
    """GetFunctionConfiguration ResourceNotFoundException → exit 2, no writes."""
    from botocore.exceptions import ClientError as BotoClientError
    monkeypatch.setenv("LIVE_TESTS", "1")
    built: list[str] = []

    def _make_client(svc, **_kw):
        built.append(svc)
        c = MagicMock()
        if svc == "lambda":
            c.get_function_configuration.side_effect = BotoClientError(
                {"Error": {"Code": "ResourceNotFoundException", "Message": "nf"}},
                "GetFunctionConfiguration",
            )
        return c

    fake_boto3 = MagicMock()
    fake_boto3.client.side_effect = _make_client
    monkeypatch.setattr(lv, "boto3", fake_boto3)

    assert lv.push_run(_precondition_args()) == 2
    assert built == ["secretsmanager", "lambda"], built
    fake_boto3.resource.assert_not_called()


@pytest.mark.parametrize("exc", [
    _ClientError(
        {"Error": {"Code": "AccessDeniedException", "Message": "denied"}},
        "GetFunctionConfiguration"),
    ConnectionError("network failure"),
], ids=["AccessDenied", "ConnectionError"])
def test_push_precondition_register_device_other_errors_propagate(monkeypatch, exc) -> None:
    """Any GetFunctionConfiguration error but ResourceNotFound propagates (exit 1).

    Mutant MS1b (restore ``except Exception: return 2`` there) → returns 2 → fails.
    """
    monkeypatch.setenv("LIVE_TESTS", "1")

    def _make_client(svc, **_kw):
        c = MagicMock()
        if svc == "lambda":
            c.get_function_configuration.side_effect = exc
        return c

    fake_boto3 = MagicMock()
    fake_boto3.client.side_effect = _make_client
    monkeypatch.setattr(lv, "boto3", fake_boto3)

    with pytest.raises(type(exc)):
        lv.push_run(_precondition_args())


# ===========================================================================
# F3.3 (prompt 312, option B): _voice_run call-site tests for cycle 4 W2's
# voice half.  The helpers are pinned above; these pin the CALLS that put
# them into effect.  Both run the real _voice_run against an in-process fake
# boto3 world and stop before any network: test A at the guard, test B at
# ws_connect (stubbed to raise, so no WebSocket is ever opened).
#
# Mutants (Mvoicewire's halves, applied in git-archive copies):
#   MVW-guard: ``_guard_no_assigned_drivers(vehicle_id, drivers_table)`` in
#              _voice_run → ``pass``  → test A fails (admin_create_user reached).
#   MVW-save:  the ``save_manifest(manifest)`` after _check_residual_findings
#              in _voice_run's finally removed → test B fails (file lacks ids).
# ===========================================================================

_VOICE_VEH = "VEH-TEST-VW1"
_VOICE_VIN = "ZZZVW000000000001"


class _AdminCreateReached(RuntimeError):
    """Raised by the fake admin_create_user: the run got past every guard."""


def _voice_world(monkeypatch, tmp_path, *, drivers_items, findings_items):
    """Install a fake boto3 world for _voice_run; return the recorder dict.

    Nothing here reaches AWS: boto3 is replaced, the AgentCore client module is
    replaced, and ``ws_connect`` raises, so the run stops right after
    ``started_ms`` is set.
    """
    monkeypatch.setenv("LIVE_TESTS", "1")
    monkeypatch.setattr(lv, "MANIFEST_DIR", tmp_path / "manifests")

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".bedrock_agentcore.yaml").write_text(
        "agents:\n  vsa_supervisor_bidi_staging:\n    bedrock_agentcore:\n"
        "      agent_arn: arn:aws:bedrock-agentcore:us-west-2:123456789012:"
        "runtime/vsa_supervisor_bidi_staging-TEST\n")
    monkeypatch.setattr(lv, "_REPO", repo)

    rec: dict = {"writes": []}

    idp = MagicMock(name="idp")
    idp.list_user_pool_clients.return_value = {"UserPoolClients": [{"ClientId": "c1"}]}
    idp.describe_user_pool_client.return_value = {
        "UserPoolClient": {"ExplicitAuthFlows": ["ALLOW_USER_PASSWORD_AUTH"]}}
    idp.admin_get_user.return_value = {
        "UserAttributes": [{"Name": "custom:tenantId", "Value": "meridian"}]}
    idp.admin_list_groups_for_user.return_value = {"Groups": [{"GroupName": "vehicle-owner"}]}
    idp.initiate_auth.return_value = {"AuthenticationResult": {"IdToken": "tok"}}
    rec["idp"] = idp

    ssm = MagicMock(name="ssm")
    ssm.get_parameter.return_value = {"Parameter": {"Value": "<user-pool-id>"}}
    cfn = MagicMock(name="cfn")
    cfn.describe_stacks.return_value = {"Stacks": [{"Outputs": [
        {"OutputKey": "RestApiUrl", "OutputValue": "https://api.invalid/staging/"}]}]}

    def _client(service, **_kw):
        return {"cognito-idp": idp, "ssm": ssm, "cloudformation": cfn}[service]

    tables: dict[str, MagicMock] = {}

    def _table(name):
        if name not in tables:
            t = MagicMock(name=name)
            t.put_item.side_effect = lambda **kw: rec["writes"].append((name, "put", kw))
            t.delete_item.side_effect = lambda **kw: rec["writes"].append((name, "delete", kw))
            if name.endswith("-storage-dtc-history"):
                t.query.return_value = {"Items": []}
            elif name.endswith("-storage-drivers"):
                t.scan.return_value = {"Items": list(drivers_items)}
            elif name.endswith("-avx-findings"):
                t.query.return_value = {"Items": list(findings_items)}
            tables[name] = t
        return tables[name]

    ddb = MagicMock(name="ddb")
    ddb.Table.side_effect = _table
    fake_boto3 = MagicMock(name="boto3")
    fake_boto3.client.side_effect = _client
    fake_boto3.resource.return_value = ddb
    monkeypatch.setattr(lv, "boto3", fake_boto3)
    rec["tables"] = tables

    fake_rt = types.ModuleType("bedrock_agentcore.runtime")

    class _FakeRuntimeClient:
        def __init__(self, region=None):
            pass

        def generate_ws_connection(self, runtime_arn, session_id):
            rec["ws_session_id"] = session_id
            return "wss://runtime.invalid/ws", {}

    fake_rt.AgentCoreRuntimeClient = _FakeRuntimeClient
    monkeypatch.setitem(sys.modules, "bedrock_agentcore.runtime", fake_rt)

    def _ws_connect(*_a, **_kw):
        raise RuntimeError("ws_connect stubbed: no network in tests")

    monkeypatch.setattr(lv, "ws_connect", _ws_connect)
    return rec


def _voice_args():
    import argparse
    return argparse.Namespace(
        stage="staging", vehicle_id=_VOICE_VEH, vin=_VOICE_VIN,
        run_id=None, open_reason=None,
    )


def test_voice_run_drivers_guard_refuses_before_any_write(monkeypatch, tmp_path) -> None:
    """_voice_run refuses a car a drivers row already names, before admin_create_user.

    Zero writes: no Cognito user, no driver row, no DTC row.  Mutant MVW-guard
    (the call at the guard site → ``pass``) reaches admin_create_user, whose
    fake raises _AdminCreateReached instead of SystemExit, so this test fails.
    """
    import asyncio as _asyncio

    rec = _voice_world(
        monkeypatch, tmp_path,
        drivers_items=[{"driverId": "DRV-REAL", "assignedVehicleId": _VOICE_VEH,
                        "cognitoEmail": "real.driver@example.com"}],
        findings_items=[],
    )
    rec["idp"].admin_create_user.side_effect = _AdminCreateReached("guard bypassed")

    with pytest.raises(SystemExit) as exc:
        _asyncio.run(lv._voice_run(_voice_args()))

    assert exc.value.code != 0
    rec["idp"].admin_create_user.assert_not_called()
    rec["idp"].admin_set_user_password.assert_not_called()
    assert rec["writes"] == [], f"refusal must leave no writes, got {rec['writes']}"
    drivers = rec["tables"]["cms-staging-storage-drivers"]
    scan_kw = drivers.scan.call_args.kwargs
    assert scan_kw["ExpressionAttributeValues"] == {":v": _VOICE_VEH}


def test_voice_run_manifest_on_disk_holds_residual_finding_ids(monkeypatch, tmp_path) -> None:
    """After the run's finally, the manifest FILE carries residual_finding_ids.

    The run passes every guard, creates the (fake) user and driver row, sets
    started_ms, then ws_connect raises.  The finally block cleans up and the
    residual report finds one Finding computed after the run start (writer
    format, ``Z`` suffix).  Mutant MVW-save (the save after the report
    removed) leaves the file at its pre-report state, so this test fails.
    """
    import asyncio as _asyncio
    from datetime import datetime, timezone

    from datetime import timedelta
    # 30 s ahead of now, so it is after the run start whatever the test speed.
    fresh = (datetime.now(timezone.utc) + timedelta(seconds=30)).strftime(
        "%Y-%m-%dT%H:%M:%S.%fZ")
    rec = _voice_world(
        monkeypatch, tmp_path,
        drivers_items=[],
        findings_items=[
            {"finding_id": "F-FRESH-1", "severity": "p0", "computed_at": fresh,
             "agent_id": "agent-2-vehicle-health", "vin": _VOICE_VIN},
            {"finding_id": "F-STALE-1", "severity": "soon",
             "computed_at": "2026-01-01T00:00:00Z",
             "agent_id": "agent-2-vehicle-health", "vin": _VOICE_VIN},
        ],
    )

    with pytest.raises(RuntimeError, match="ws_connect stubbed"):
        _asyncio.run(lv._voice_run(_voice_args()))

    files = sorted((tmp_path / "manifests").glob("*.json"))
    assert len(files) == 1, files
    on_disk = json.loads(files[0].read_text())
    assert on_disk.get("session_id") == rec["ws_session_id"]
    assert on_disk.get("residual_finding_ids") == ["F-FRESH-1"], on_disk
    # The run did write, and cleanup removed what it wrote.
    kinds = [(n, k) for n, k, _ in rec["writes"]]
    assert ("cms-staging-storage-drivers", "put") in kinds
    assert ("cms-staging-storage-drivers", "delete") in kinds
    rec["idp"].admin_delete_user.assert_called_once()


# ===========================================================================
# Fix Group 4 (prompt 317): push_run call-site tests for review cycle 4 W1 and
# W2's push half.  The helpers are pinned above; these run the real push_run
# against an in-process fake AWS world (boto3, requests and time replaced), so
# each guard, the early stop and the cleanup wiring is tested where it is
# called.  Nothing here reaches AWS or the network, and no test really sleeps.
#
# Mutants (decisions.md, Fix Group 4, F4.2; applied in git-archive copies):
#   Mpushguard-vin / -dtc / -drv: the resolved-VIN, CRITICAL/HIGH and
#       assigned-driver guard calls in push_run → ``pass``
#       → the matching test_push_run_guard_refuses_with_zero_writes case fails.
#   Mnoarn:     ``if not endpoint_arn:`` → ``if False:``
#       → test_push_run_missing_endpoint_arn_returns_1_and_never_seeds fails.
#   Morphwire:  owner_sub taken only after register-device answers 2xx
#       → test_push_run_register_500_still_deletes_the_runs_endpoint fails.
#   Msweep:     the CustomUserData sweep call removed from _push_cleanup
#       → same test fails.
#   Msweepany:  the sweep matches any endpoint (CustomUserData check dropped)
#       → same test fails (the other owner's endpoint is deleted).
#   Mpushfresh: the push wait's computed_at filter → ``if True``
#       → test_push_run_skips_stale_finding_and_passes_on_fresh_one fails.
# ===========================================================================

import base64 as _b64
import time as _real_time
from datetime import datetime as _dt, timedelta as _td, timezone as _tz

_PUSH_VEH = "VEH-TEST-PW1"
_PUSH_VIN = "ZZZPW000000000001"
_PUSH_TOKEN = "a1b2c3d4" * 8
_PUSH_SUB = "11111111-aaaa-4bbb-8ccc-222222222222"
_PUSH_APP_ARN = ("arn:aws:sns:us-west-2:123456789012:"
                 "app/APNS_SANDBOX/vsa-staging-avx-apns-sandbox")
_PUSH_EP_ARN = ("arn:aws:sns:us-west-2:123456789012:"
                "endpoint/APNS_SANDBOX/vsa-staging-avx-apns-sandbox/"
                "0a0a0a0a-1b1b-4c2c-8d3d-4e4e4e4e4e4e")
_PUSH_OTHER_EP_ARN = ("arn:aws:sns:us-west-2:123456789012:"
                      "endpoint/APNS_SANDBOX/vsa-staging-avx-apns-sandbox/"
                      "9f9f9f9f-8e8e-4d7d-8c6c-5b5b5b5b5b5b")


def _fake_jwt(sub: str) -> str:
    def enc(d: dict) -> str:
        return _b64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")
    return f"{enc({'alg': 'none'})}.{enc({'sub': sub})}.sig"


class _FakeTime:
    """``time`` stand-in: real wall clock, a monotonic clock that only sleep moves."""

    def __init__(self) -> None:
        self.mono = 1_000.0
        self.sleeps: list[float] = []

    def time(self) -> float:
        return _real_time.time()

    def monotonic(self) -> float:
        return self.mono

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.mono += seconds


def _push_world(monkeypatch, tmp_path, *, vehicles_vin=_PUSH_VIN, dtc_guard_items=(),
                drivers_items=(), reg_status=200, device_row=None,
                findings_by_poll=(), ledger_published_for=None,
                sns_pages=((),)):
    """Install a fake AWS world for push_run; return the recorder dict.

    ``findings_by_poll[i]`` is what the i-th Finding poll returns (the last one
    repeats).  ``sns_pages`` are the ListEndpointsByPlatformApplication pages.
    ``rec["writes"]`` holds every AWS write in order as (target, op, kwargs).
    """
    monkeypatch.setenv("LIVE_TESTS", "1")
    monkeypatch.setattr(lv, "MANIFEST_DIR", tmp_path / "manifests")
    rec: dict = {"writes": [], "posts": [], "finding_polls": 0,
                 "sns_list_calls": [], "device_reads": []}

    def w(target, op):
        return lambda **kw: rec["writes"].append((target, op, kw))

    sm = MagicMock(name="secretsmanager")
    sm.describe_secret.return_value = {"Name": "cvx/staging/apns-auth-key"}
    lam = MagicMock(name="lambda")
    lam.get_function_configuration.return_value = {
        "Environment": {"Variables": {"AVX_APNS_PLATFORM_APP_ARN": _PUSH_APP_ARN}}}

    sns = MagicMock(name="sns")
    pages = list(sns_pages)

    def _list(**kw):
        rec["sns_list_calls"].append(kw)
        i = len(rec["sns_list_calls"]) - 1
        out = {"Endpoints": list(pages[i])}
        if i + 1 < len(pages):
            out["NextToken"] = f"page-{i + 1}"
        return out

    sns.list_endpoints_by_platform_application.side_effect = _list
    sns.delete_endpoint.side_effect = w("sns", "delete_endpoint")
    rec["sns"] = sns

    idp = MagicMock(name="idp")
    idp.list_user_pool_clients.return_value = {"UserPoolClients": [{"ClientId": "c1"}]}
    idp.describe_user_pool_client.return_value = {
        "UserPoolClient": {"ExplicitAuthFlows": ["ALLOW_USER_PASSWORD_AUTH"]}}
    idp.admin_get_user.return_value = {
        "UserAttributes": [{"Name": "custom:tenantId", "Value": "meridian"}]}
    idp.admin_list_groups_for_user.return_value = {"Groups": [{"GroupName": "vehicle-owner"}]}
    idp.initiate_auth.return_value = {
        "AuthenticationResult": {"IdToken": _fake_jwt(_PUSH_SUB)}}
    idp.admin_create_user.side_effect = w("cognito", "admin_create_user")
    idp.admin_set_user_password.side_effect = w("cognito", "admin_set_user_password")
    idp.admin_add_user_to_group.side_effect = w("cognito", "admin_add_user_to_group")
    idp.admin_delete_user.side_effect = w("cognito", "admin_delete_user")
    rec["idp"] = idp

    ssm = MagicMock(name="ssm")
    ssm.get_parameter.return_value = {"Parameter": {"Value": "<user-pool-id>"}}
    cfn = MagicMock(name="cfn")
    cfn.describe_stacks.return_value = {"Stacks": [{"Outputs": [
        {"OutputKey": "RestApiUrl", "OutputValue": "https://api.invalid/staging/"}]}]}

    clients = {"secretsmanager": sm, "lambda": lam, "sns": sns, "cognito-idp": idp,
               "ssm": ssm, "cloudformation": cfn}

    tables: dict[str, MagicMock] = {}

    def _table(name):
        if name in tables:
            return tables[name]
        t = MagicMock(name=name)
        t.put_item.side_effect = w(name, "put_item")
        t.delete_item.side_effect = w(name, "delete_item")
        t.update_item.side_effect = w(name, "update_item")
        if name.endswith("-storage-vehicles"):
            t.get_item.return_value = {"Item": {"vehicleId": _PUSH_VEH, "vin": vehicles_vin}}
        elif name.endswith("-storage-dtc-history"):
            t.query.return_value = {"Items": list(dtc_guard_items)}
        elif name.endswith("-storage-drivers"):
            t.scan.return_value = {"Items": list(drivers_items)}
        elif name.endswith("-notification-devices"):
            def _get(**kw):
                rec["device_reads"].append(("get_item", kw))
                return {"Item": dict(device_row)} if device_row is not None else {}

            def _query(**kw):
                rec["device_reads"].append(("query", kw))
                return {"Items": [dict(device_row)] if device_row is not None else []}

            t.get_item.side_effect = _get
            t.query.side_effect = _query
        elif name.endswith("-avx-findings"):
            def _fq(**kw):
                i = rec["finding_polls"]
                rec["finding_polls"] += 1
                if not findings_by_poll:
                    return {"Items": []}
                return {"Items": list(findings_by_poll[min(i, len(findings_by_poll) - 1)])}

            t.query.side_effect = _fq
        elif name.endswith("-avx-rate-limits"):
            def _lg(**kw):
                key = kw["Key"]
                ok = (ledger_published_for is not None
                      and key == {"owner_id": _PUSH_SUB,
                                  "window_key": f"p0:{ledger_published_for}"})
                return {"Item": {"state": "published"}} if ok else {}

            t.get_item.side_effect = _lg
        tables[name] = t
        return t

    ddb = MagicMock(name="ddb")
    ddb.Table.side_effect = _table
    fake_boto3 = MagicMock(name="boto3")
    fake_boto3.client.side_effect = lambda svc, **_kw: clients[svc]
    fake_boto3.resource.return_value = ddb
    monkeypatch.setattr(lv, "boto3", fake_boto3)
    rec["tables"] = tables

    def _post(url, **kw):
        rec["posts"].append((url, kw))
        return types.SimpleNamespace(status_code=reg_status, text="{}")

    monkeypatch.setattr(lv, "_http", types.SimpleNamespace(post=_post))
    fake_time = _FakeTime()
    monkeypatch.setattr(lv, "time", fake_time)
    rec["time"] = fake_time
    return rec


def _push_args(vin=None):
    import argparse
    return argparse.Namespace(stage="staging", vehicle_id=_PUSH_VEH, vin=vin,
                              device_token=_PUSH_TOKEN, run_id=None)


def _ops(rec, op):
    return [(t, kw) for t, o, kw in rec["writes"] if o == op]


def _device_row():
    return {"owner_id": _PUSH_SUB, "device_token": _PUSH_TOKEN,
            "endpoint_arn": _PUSH_EP_ARN}


def _finding(fid, computed_at, detail=None):
    return {"finding_id": fid, "severity": "p0", "computed_at": computed_at,
            "agent_id": "agent-2-vehicle-health", "vin": _PUSH_VIN,
            "detail": detail if detail is not None
            else cat.p0_directive(cat.faults_for_codes(["C1234"]), None)}


@pytest.mark.parametrize("case", ["resolved_vin", "standing_critical", "assigned_driver"])
def test_push_run_guard_refuses_with_zero_writes(monkeypatch, tmp_path, case) -> None:
    """Each push guard refuses before admin_create_user, with zero AWS writes.

    The fake admin_create_user raises _AdminCreateReached, so a bypassed guard
    (mutants Mpushguard-vin / -dtc / -drv) surfaces as that error, not SystemExit.
    """
    kw: dict = {}
    if case == "resolved_vin":
        kw["vehicles_vin"] = "MRDN0000000000015"  # resolved, not passed as --vin
    elif case == "standing_critical":
        kw["dtc_guard_items"] = [{"vehicleId": _PUSH_VEH, "code": "P0300",
                                  "status": "ACTIVE", "severity": "CRITICAL"}]
    else:
        kw["drivers_items"] = [{"driverId": "DRV-REAL", "assignedVehicleId": _PUSH_VEH,
                                "cognitoEmail": "real.driver@example.com"}]
    rec = _push_world(monkeypatch, tmp_path, **kw)
    rec["idp"].admin_create_user.side_effect = _AdminCreateReached("guard bypassed")

    with pytest.raises(SystemExit) as exc:
        lv.push_run(_push_args())

    assert exc.value.code != 0
    rec["idp"].admin_create_user.assert_not_called()
    assert rec["writes"] == [], f"refusal must leave no writes, got {rec['writes']}"
    assert rec["posts"] == [], "register-device must not be called"
    rec["sns"].list_endpoints_by_platform_application.assert_not_called()


def test_push_run_missing_endpoint_arn_returns_1_and_never_seeds(monkeypatch, tmp_path) -> None:
    """2xx with no endpoint_arn on the row → exit 1 before the DTC seed; cleanup runs.

    Mutant Mnoarn (``if not endpoint_arn`` → ``if False``) seeds the DTC row and
    waits for a Finding → the "no DTC put_item" assertion fails.
    """
    row = {"owner_id": _PUSH_SUB, "device_token": _PUSH_TOKEN}  # no endpoint_arn
    rec = _push_world(monkeypatch, tmp_path, reg_status=200, device_row=row)

    assert lv.push_run(_push_args()) == 1

    dtc_puts = [t for t, _ in _ops(rec, "put_item") if t.endswith("-storage-dtc-history")]
    assert dtc_puts == [], "no DTC row may be seeded without an endpoint"
    assert rec["finding_polls"] == 0
    assert rec["time"].sleeps == [], "no Finding wait without an endpoint"
    # Cleanup ran for what was created.
    deleted = {t for t, _ in _ops(rec, "delete_item")}
    assert "cms-staging-storage-drivers" in deleted
    assert "vsa-staging-notification-devices" in deleted
    assert _ops(rec, "admin_delete_user")
    # The readback that found no endpoint was strongly consistent.
    get_kw = [kw for op, kw in rec["device_reads"] if op == "get_item"]
    assert get_kw and all(kw.get("ConsistentRead") is True for kw in get_kw)


def test_push_run_register_500_still_deletes_the_runs_endpoint(monkeypatch, tmp_path) -> None:
    """register-device 500 after CreatePlatformEndpoint: no device row, endpoint left.

    Cleanup must still find the run's endpoint by CustomUserData == owner_sub on
    the platform application, delete it, and leave another owner's endpoint.
    Mutants Morphwire (owner_sub only after a 2xx), Msweep (no sweep) and
    Msweepany (CustomUserData not checked) each fail an assertion below.
    """
    rec = _push_world(
        monkeypatch, tmp_path, reg_status=500, device_row=None,
        sns_pages=(
            [{"EndpointArn": _PUSH_OTHER_EP_ARN,
              "Attributes": {"CustomUserData": "someone-else", "Enabled": "true"}}],
            [{"EndpointArn": _PUSH_EP_ARN,
              "Attributes": {"CustomUserData": _PUSH_SUB, "Enabled": "true"}}],
        ),
    )

    assert lv.push_run(_push_args()) == 1

    assert [kw["EndpointArn"] for _, kw in _ops(rec, "delete_endpoint")] == [_PUSH_EP_ARN]
    assert rec["sns_list_calls"] == [
        {"PlatformApplicationArn": _PUSH_APP_ARN},
        {"PlatformApplicationArn": _PUSH_APP_ARN, "NextToken": "page-1"},
    ]
    q_kw = [kw for op, kw in rec["device_reads"] if op == "query"]
    assert q_kw == [{"KeyConditionExpression": "owner_id = :o",
                     "ExpressionAttributeValues": {":o": _PUSH_SUB},
                     "ConsistentRead": True}]
    dtc_puts = [t for t, _ in _ops(rec, "put_item") if t.endswith("-storage-dtc-history")]
    assert dtc_puts == []
    assert _ops(rec, "admin_delete_user")


def test_push_run_skips_stale_finding_and_passes_on_fresh_one(monkeypatch, tmp_path) -> None:
    """A p0 Finding from before the run start is ignored; the fresh one is used.

    Polls 1-2 return only a stale Finding (same VIN, exact directive, writer's
    ``Z`` format); poll 3 adds a fresh one.  The ledger is ``published`` only for
    the fresh one.  Mutant Mpushfresh takes the stale one on poll 1 → the
    manifest's finding_id and the exit code assertions fail.
    """
    stale = _finding("F-STALE-1", "2026-01-01T00:00:00Z")
    fresh_at = (_dt.now(_tz.utc) + _td(seconds=30)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    fresh = _finding("F-FRESH-1", fresh_at)
    rec = _push_world(
        monkeypatch, tmp_path, reg_status=200, device_row=_device_row(),
        findings_by_poll=([stale], [stale], [stale, fresh]),
        ledger_published_for="F-FRESH-1",
        sns_pages=([{"EndpointArn": _PUSH_EP_ARN,
                     "Attributes": {"CustomUserData": _PUSH_SUB}}],),
    )

    assert lv.push_run(_push_args()) == 0

    files = sorted((tmp_path / "manifests").glob("*.json"))
    assert len(files) == 1, files
    on_disk = json.loads(files[0].read_text())
    assert on_disk["finding_id"] == "F-FRESH-1"
    assert on_disk["ledger_key"] == "p0:F-FRESH-1"
    assert rec["finding_polls"] == 3
    # One DTC seed, then everything the run created is deleted; the endpoint once,
    # although both the device row and the sweep name it.
    dtc_puts = [kw for t, kw in _ops(rec, "put_item") if t.endswith("-storage-dtc-history")]
    assert len(dtc_puts) == 1 and dtc_puts[0]["Item"]["code"] == "C1234"
    assert [kw["EndpointArn"] for _, kw in _ops(rec, "delete_endpoint")] == [_PUSH_EP_ARN]
    deleted = [t for t, _ in _ops(rec, "delete_item")]
    assert deleted.count("cms-staging-storage-dtc-history") == 1
    assert deleted.count("vsa-staging-notification-devices") == 1
    assert deleted.count("cms-staging-storage-drivers") == 1
    assert len(_ops(rec, "admin_delete_user")) == 1


def test_delete_owner_endpoints_list_failure_prints_commands(capsys) -> None:
    """A failed ListEndpointsByPlatformApplication prints the list and delete commands."""
    sns = MagicMock()
    sns.list_endpoints_by_platform_application.side_effect = RuntimeError("throttled")
    lv._delete_owner_endpoints(sns, _PUSH_APP_ARN, _PUSH_SUB, set())
    err = capsys.readouterr().err
    assert "list-endpoints-by-platform-application" in err
    assert f"--platform-application-arn {_PUSH_APP_ARN}" in err
    assert f"Attributes.CustomUserData=='{_PUSH_SUB}'" in err
    assert "aws sns delete-endpoint" in err
    sns.delete_endpoint.assert_not_called()


def test_delete_owner_endpoints_delete_failure_prints_command_and_continues(capsys) -> None:
    """One failed delete prints its command; the next matching endpoint is still deleted."""
    sns = MagicMock()
    sns.list_endpoints_by_platform_application.return_value = {"Endpoints": [
        {"EndpointArn": _PUSH_OTHER_EP_ARN, "Attributes": {"CustomUserData": _PUSH_SUB}},
        {"EndpointArn": _PUSH_EP_ARN, "Attributes": {"CustomUserData": _PUSH_SUB}},
    ]}
    calls: list[str] = []

    def _del(EndpointArn):
        calls.append(EndpointArn)
        if EndpointArn == _PUSH_OTHER_EP_ARN:
            raise RuntimeError("denied")

    sns.delete_endpoint.side_effect = _del
    lv._delete_owner_endpoints(sns, _PUSH_APP_ARN, _PUSH_SUB, set())
    assert calls == [_PUSH_OTHER_EP_ARN, _PUSH_EP_ARN]
    assert f"--endpoint-arn {_PUSH_OTHER_EP_ARN}" in capsys.readouterr().err


def test_delete_owner_endpoints_no_match_prints_count_and_recheck_command(capsys) -> None:
    """A sweep that matches nothing says so and prints the list command to re-check.

    No read-after-write guarantee is documented for ListEndpointsByPlatformApplication,
    so a register-device 500 whose endpoint the list missed must stay auditable
    (review cycle 5 suggestion).
    """
    sns = MagicMock()
    sns.list_endpoints_by_platform_application.return_value = {"Endpoints": [
        {"EndpointArn": _PUSH_OTHER_EP_ARN, "Attributes": {"CustomUserData": "someone-else"}},
    ]}
    lv._delete_owner_endpoints(sns, _PUSH_APP_ARN, _PUSH_SUB, set())
    out = capsys.readouterr().out
    assert f"endpoint sweep: 0 endpoint(s) with CustomUserData={_PUSH_SUB}" in out
    assert "to re-check by hand: aws sns list-endpoints-by-platform-application" in out
    assert f"Attributes.CustomUserData=='{_PUSH_SUB}'" in out
    sns.delete_endpoint.assert_not_called()
