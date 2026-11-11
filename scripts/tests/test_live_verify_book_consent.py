"""Tests for scripts/live-verify-book-consent.py.

Covers:
  1. Guard suite — gates refuse before any AWS client.
  2. _delete_session_bookings / _query_session_bookings helpers.
  3. _run_cleanup — booking deletion then user deletion; booking failure does not skip user
     cleanup (mirrors the Critical from review C1).
  4. evaluate() — pure verdict function, one test per case listed in review C1-C3.
     Mutant kill: both PASS conditions were set to 'if True:' → only the evaluate() tests
     catch this because they call the actual function.
  5. run() wiring — drives run() with stubs, raises filter_log_events, asserts both
     delete_item and admin_delete_user ran (via the finally block).
     Mutant kill: changing 'finally:' to 'except ZeroDivisionError:' → admin_delete_user
     is never called → test fails.
  6. _polly_pcm_frames — shape and padding.

No AWS calls; all DDB / IDP / Polly interactions are mocked.
"""
from __future__ import annotations

import importlib.util
import json
import sys
import time
import types
from io import BytesIO
from pathlib import Path
from unittest.mock import MagicMock

import pytest

_SCRIPT = Path(__file__).resolve().parents[1] / "live-verify-book-consent.py"


def _load():
    spec = importlib.util.spec_from_file_location("_lv_bc_under_test", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


lv = _load()


# ──────────────────────────────────────────────────────────────────────────────
# Guards (no AWS)
# ──────────────────────────────────────────────────────────────────────────────


@pytest.fixture()
def _no_aws(monkeypatch):
    boom = MagicMock(side_effect=AssertionError("AWS client built before gates passed"))
    monkeypatch.setattr(lv, "boto3", boom, raising=False)
    monkeypatch.setattr(lv.asyncio, "run", MagicMock(side_effect=AssertionError("asyncio.run")))


def test_refused_without_live_tests(monkeypatch, _no_aws) -> None:
    monkeypatch.delenv("LIVE_TESTS", raising=False)
    assert lv.main(["--allow-writes", "--scenario", "unasked"]) == 1


def test_refused_without_allow_writes(monkeypatch, _no_aws) -> None:
    monkeypatch.setenv("LIVE_TESTS", "1")
    assert lv.main(["--scenario", "unasked"]) == 1


@pytest.mark.parametrize("scenario", ["unasked", "typed-yes", "spoken-yes"])
def test_scenario_arg_accepted(monkeypatch, scenario: str) -> None:
    """All three scenario choices are recognised — argparse must not raise SystemExit(2)."""
    monkeypatch.setenv("LIVE_TESTS", "1")
    # We expect exit 1 from the live-write guard (go env not set).
    # We must NOT get SystemExit(2) from argparse (invalid choice).
    monkeypatch.setattr(lv._lwg, "writes_sanctioned", lambda *a, **kw: False)
    rc = lv.main(["--allow-writes", "--scenario", scenario])
    assert rc == 1


def test_live_go_refused_without_go_env(monkeypatch, _no_aws) -> None:
    """When LIVE_TESTS=1 but go env is unset, the live-write guard blocks.

    Monkeypatches live_write_guard.aws_block_active to False so the conftest
    AWS block (which would otherwise bypass the go-env check) does not interfere.
    """
    monkeypatch.setenv("LIVE_TESTS", "1")
    monkeypatch.delenv("BOOK_CONSENT_LIVE_GO", raising=False)
    # Without this, conftest's AWS block makes aws_block_active() True, which
    # bypasses the go-env requirement (writes_sanctioned returns True).
    monkeypatch.setattr(lv._lwg, "aws_block_active", lambda: False)
    rc = lv.main(["--allow-writes", "--scenario", "unasked"])
    assert rc == 1


def test_invalid_scenario_is_rejected(monkeypatch, _no_aws) -> None:
    monkeypatch.setenv("LIVE_TESTS", "1")
    with pytest.raises(SystemExit) as exc:
        lv.main(["--allow-writes", "--scenario", "none"])
    assert exc.value.code != 0


def test_region_is_pinned_to_us_west_2(monkeypatch) -> None:
    monkeypatch.setenv("LIVE_TESTS", "1")
    with pytest.raises(SystemExit) as exc:
        lv.main(["--allow-writes", "--scenario", "unasked", "--region", "ap-northeast-1"])
    assert exc.value.code != 0


def test_stage_is_pinned_to_staging(monkeypatch) -> None:
    monkeypatch.setenv("LIVE_TESTS", "1")
    with pytest.raises(SystemExit) as exc:
        lv.main(["--allow-writes", "--scenario", "unasked", "--stage", "prod"])
    assert exc.value.code != 0


# ──────────────────────────────────────────────────────────────────────────────
# _query_session_bookings / _delete_session_bookings
# ──────────────────────────────────────────────────────────────────────────────


def _table_with(*rows):
    t = MagicMock()
    t.query.return_value = {"Items": list(rows)}
    return t


def test_query_session_bookings_returns_all_rows_from_single_page() -> None:
    row = {"vehicleId": "V1", "serviceDate": "2026-11-02T12:00:00", "sessionId": "s1"}
    table = _table_with(row)
    result = lv._query_session_bookings(table, "V1", "s1", "2026-11-02T11:00:00")
    assert result == [row]


def test_query_session_bookings_paginates() -> None:
    t = MagicMock()
    t.query.side_effect = [
        {"Items": [{"vehicleId": "V", "serviceDate": "T1", "sessionId": "s"}],
         "LastEvaluatedKey": {"vehicleId": "V", "serviceDate": "T1"}},
        {"Items": [{"vehicleId": "V", "serviceDate": "T2", "sessionId": "s"}]},
    ]
    rows = lv._query_session_bookings(t, "V", "s", "T0")
    assert len(rows) == 2
    assert t.query.call_count == 2


def test_delete_session_bookings_calls_delete_per_row() -> None:
    rows = [
        {"vehicleId": "V", "serviceDate": "T1", "sessionId": "s", "requestNumber": "R1"},
        {"vehicleId": "V", "serviceDate": "T2", "sessionId": "s", "requestNumber": "R2"},
    ]
    table = _table_with(*rows)
    lv._delete_session_bookings(table, "V", "s", "T0")
    assert table.delete_item.call_count == 2


def test_delete_session_bookings_prints_no_rows_when_empty(capsys) -> None:
    table = _table_with()
    lv._delete_session_bookings(table, "V", "s", "T0")
    table.delete_item.assert_not_called()
    assert "no booking rows" in capsys.readouterr().out


# ──────────────────────────────────────────────────────────────────────────────
# _run_cleanup
# ──────────────────────────────────────────────────────────────────────────────


def _now_ms():
    return int(time.time() * 1000) - 60_000


def test_run_cleanup_deletes_booking_and_user() -> None:
    table = _table_with(
        {"vehicleId": "V", "serviceDate": "2026-11-02T12:00:00",
         "sessionId": "sess", "requestNumber": "R1"},
    )
    idp = MagicMock()
    lv._run_cleanup(idp=idp, pool="pool-1", user="u@example.com", created=True,
                    history_table=table, vid="V", session_id="sess",
                    start_ms=_now_ms(), stage="staging")
    table.delete_item.assert_called_once()
    idp.admin_delete_user.assert_called_once_with(UserPoolId="pool-1", Username="u@example.com")


def test_run_cleanup_still_deletes_user_when_booking_cleanup_raises(capsys) -> None:
    table = MagicMock()
    table.query.side_effect = Exception("DDB throttled")
    idp = MagicMock()
    lv._run_cleanup(idp=idp, pool="pool-1", user="u@example.com", created=True,
                    history_table=table, vid="V", session_id="sess",
                    start_ms=_now_ms(), stage="staging")
    idp.admin_delete_user.assert_called_once()
    err = capsys.readouterr().err
    assert "V" in err and "sess" in err


def test_run_cleanup_skips_booking_when_session_id_none() -> None:
    table = MagicMock()
    idp = MagicMock()
    lv._run_cleanup(idp=idp, pool="p", user="u@example.com", created=True,
                    history_table=table, vid="V", session_id=None,
                    start_ms=_now_ms(), stage="staging")
    table.query.assert_not_called()
    idp.admin_delete_user.assert_called_once()


def test_run_cleanup_skips_booking_when_start_ms_none() -> None:
    table = MagicMock()
    idp = MagicMock()
    lv._run_cleanup(idp=idp, pool="p", user="u@example.com", created=True,
                    history_table=table, vid="V", session_id="sess",
                    start_ms=None, stage="staging")
    table.query.assert_not_called()
    idp.admin_delete_user.assert_called_once()


def test_run_cleanup_prints_manual_command_on_user_delete_failure(capsys) -> None:
    table = _table_with()
    idp = MagicMock()
    idp.admin_delete_user.side_effect = RuntimeError("IDP unavailable")
    lv._run_cleanup(idp=idp, pool="pool-99", user="orphan@example.com", created=True,
                    history_table=table, vid=None, session_id=None,
                    start_ms=None, stage="staging")
    err = capsys.readouterr().err
    assert "orphan@example.com" in err
    assert "admin-delete-user" in err


# ──────────────────────────────────────────────────────────────────────────────
# evaluate() — pure verdict function, one test per review case
# ──────────────────────────────────────────────────────────────────────────────

# Helper: empty-ish defaults for parameters not under test.
_NO_ROWS: list = []
_NO_REFUSAL: list = []
_NO_FALLBACK: list = []
_NO_RESULTS: list = []
_YES_ISO = "2026-11-02T15:08:30+00:00"
_ROW_BEFORE = {"vehicleId": "V", "serviceDate": "2026-11-02T15:07:00.000000+00:00",
               "sessionId": "s", "requestNumber": "R-BEFORE"}
_ROW_AFTER = {"vehicleId": "V", "serviceDate": "2026-11-02T15:09:00.000000+00:00",
              "sessionId": "s", "requestNumber": "R-AFTER"}


def _ev(scenario="unasked", tools=None, rows=None, refusal=None, post_yes=None,
        fallback=None, yes_iso=None, book_results=None):
    return lv.evaluate(
        scenario=scenario,
        tools=tools or [],
        booking_rows=rows if rows is not None else [],
        session_refusal_lines=refusal or [],
        post_yes_refusal_lines=post_yes or [],
        fallback_lines=fallback or [],
        yes_sent_iso=yes_iso,
        book_results_after_yes=book_results or [],
    )


# ── unasked ───────────────────────────────────────────────────────────────────

def test_evaluate_unasked_no_book_call_is_inconclusive() -> None:
    """unasked: model never called book → 2 (consent gate not exercised)."""
    code, msg = _ev("unasked", tools=[])
    assert code == 2
    assert "never called book" in msg


def test_evaluate_unasked_book_plus_refusal_no_row_is_pass() -> None:
    """unasked: book called + refusal logged + 0 rows → 0 (PASS)."""
    code, msg = _ev("unasked", tools=["book"],
                    refusal=["book: consent refused session=s1"])
    assert code == 0
    assert "PASS" in msg


def test_evaluate_unasked_any_row_is_fail() -> None:
    """unasked: any booking row → 1 (FAIL: booking without consent)."""
    code, msg = _ev("unasked", tools=["book"],
                    refusal=["book: consent refused session=s1"],
                    rows=[_ROW_AFTER])
    assert code == 1
    assert "FAIL" in msg


def test_evaluate_unasked_book_called_no_refusal_no_row_is_inconclusive() -> None:
    """unasked: book called but no refusal and no row → 2 (other refusal path)."""
    code, msg = _ev("unasked", tools=["book"])
    assert code == 2
    assert "INCONCLUSIVE" in msg


# ── yes scenarios ─────────────────────────────────────────────────────────────

def test_evaluate_yes_fallback_lines_is_inconclusive() -> None:
    """typed-yes: auto-book-fallback fired → 2 (gated path not exercised)."""
    code, msg = _ev("typed-yes", rows=[_ROW_AFTER], yes_iso=_YES_ISO,
                    book_results=["R-AFTER"],
                    fallback=["auto-book-fallback: Nova didn't call book session=s"])
    assert code == 2
    assert "INCONCLUSIVE" in msg


def test_evaluate_yes_post_yes_refusal_is_fail() -> None:
    """typed-yes: consent refused AFTER yes → 1 (yes was not consumed by the gate)."""
    code, msg = _ev("typed-yes", rows=[_ROW_AFTER], yes_iso=_YES_ISO,
                    book_results=["R-AFTER"],
                    post_yes=["book: consent refused session=s"])
    assert code == 1
    assert "FAIL" in msg


def test_evaluate_yes_no_rows_is_fail() -> None:
    """typed-yes: said yes but no booking row → 1."""
    code, msg = _ev("typed-yes", rows=[], yes_iso=_YES_ISO)
    assert code == 1
    assert "FAIL" in msg


def test_evaluate_yes_two_rows_is_fail() -> None:
    """typed-yes: two rows → 1 (expected exactly one)."""
    code, msg = _ev("typed-yes", rows=[_ROW_AFTER, _ROW_BEFORE], yes_iso=_YES_ISO,
                    book_results=["R-AFTER"])
    assert code == 1
    assert "FAIL" in msg


def test_evaluate_yes_pre_yes_row_is_fail() -> None:
    """Yes scenarios: row.serviceDate before yes_sent_iso → 1 (pre-yes booking, defect)."""
    code, msg = _ev("typed-yes", rows=[_ROW_BEFORE], yes_iso=_YES_ISO,
                    book_results=["R-BEFORE"])
    assert code == 1
    assert "pre-dates yes" in msg or "FAIL" in msg


def test_evaluate_yes_one_gated_row_after_yes_is_pass() -> None:
    """typed-yes: one row after yes with matching requestNumber → 0 (PASS)."""
    code, msg = _ev("typed-yes", rows=[_ROW_AFTER], yes_iso=_YES_ISO,
                    book_results=["R-AFTER"])
    assert code == 0
    assert "PASS" in msg


def test_evaluate_spoken_yes_same_logic_as_typed_yes() -> None:
    """spoken-yes shares the same verdict logic as typed-yes."""
    code, msg = _ev("spoken-yes", rows=[_ROW_AFTER], yes_iso=_YES_ISO,
                    book_results=["R-AFTER"])
    assert code == 0
    assert "PASS" in msg


def test_evaluate_yes_untracked_booking_is_fail() -> None:
    """typed-yes: row requestNumber not in book_results_after_yes → 1."""
    code, msg = _ev("typed-yes", rows=[_ROW_AFTER], yes_iso=_YES_ISO,
                    book_results=["DIFFERENT-R"])
    assert code == 1
    assert "FAIL" in msg


# ──────────────────────────────────────────────────────────────────────────────
# run() wiring: cleanup called from finally even when filter_log_events raises
# ──────────────────────────────────────────────────────────────────────────────


def test_run_cleanup_wired_to_finally_on_filter_log_error(monkeypatch) -> None:
    """run() must call _run_cleanup from its finally block when filter_log_events raises.

    Uses --scenario unasked (simplest: no offer/yes flow needed).

    Mutant tried: 'finally:' changed to 'except ZeroDivisionError:' — RuntimeError from
    filter_log_events is not caught, exception propagates without calling _run_cleanup,
    and admin_delete_user is never called.  This test fails on that mutant.
    """
    import asyncio as _asyncio

    _orig_sleep = _asyncio.sleep

    async def _fast_sleep(delay=0, *a, **kw):  # noqa: ANN001
        await _orig_sleep(0)

    monkeypatch.setattr(lv.asyncio, "sleep", _fast_sleep)

    # ── fake websocket (unasked: no offer event needed) ───────────────────────
    class _FakeWS:
        async def send(self, data: str) -> None:  # noqa: ANN001
            pass

        async def recv(self) -> str:
            raise _asyncio.TimeoutError()

    class _FakeCM:
        async def __aenter__(self):
            return _FakeWS()

        async def __aexit__(self, *a):
            return False

    # ── fake boto3 ───────────────────────────────────────────────────────────
    history_table = MagicMock()
    history_table.query.return_value = {
        "Items": [{"vehicleId": "VEH-T01", "serviceDate": "2026-11-02T12:00:00",
                   "sessionId": "any", "requestNumber": "R88"}]
    }

    idp_mock = MagicMock()
    idp_mock.get_paginator.return_value.paginate.return_value = [
        {"UserPoolClients": [{"ClientId": "cid"}]}
    ]
    idp_mock.describe_user_pool_client.return_value = {
        "UserPoolClient": {"ExplicitAuthFlows": ["ALLOW_USER_PASSWORD_AUTH"]}
    }
    idp_mock.admin_get_user.return_value = {
        "UserAttributes": [{"Name": "custom:tenantId", "Value": "meridian-fleet"}]
    }
    idp_mock.admin_list_groups_for_user.return_value = {"Groups": []}
    idp_mock.initiate_auth.return_value = {"AuthenticationResult": {"IdToken": "tok"}}

    ssm_mock = MagicMock()
    ssm_mock.get_parameter.return_value = {"Parameter": {"Value": "pool-id"}}

    cf_mock = MagicMock()
    cf_mock.describe_stacks.return_value = {"Stacks": [{"Outputs": [
        {"OutputKey": "RestApiUrl", "OutputValue": "https://api.example.com"}
    ]}]}

    ctl_mock = MagicMock()
    ctl_mock.list_agent_runtimes.return_value = {"agentRuntimes": [{
        "agentRuntimeName": "vsa_supervisor_bidi_staging",
        "agentRuntimeArn": "arn:aws:x", "agentRuntimeId": "rid",
        "agentRuntimeVersion": "1.0", "lastUpdatedAt": "2026-11-02",
    }]}

    logs_mock = MagicMock()
    logs_mock.filter_log_events.side_effect = RuntimeError("simulated log error")

    def _fake_client(service: str, **_):
        return {
            "cognito-idp": idp_mock,
            "ssm": ssm_mock,
            "cloudformation": cf_mock,
            "bedrock-agentcore-control": ctl_mock,
            "logs": logs_mock,
        }.get(service, MagicMock())

    ddb_resource = MagicMock()
    ddb_resource.Table.return_value = history_table

    fake_boto3 = MagicMock()
    fake_boto3.client.side_effect = _fake_client
    fake_boto3.resource.return_value = ddb_resource

    monkeypatch.setattr(lv, "pick_vehicle",
                        lambda *a, **kw: ("VIN-T01", "VEH-T01", "P1", None))

    fake_ws_mod = types.ModuleType("websockets")
    fake_ws_mod.ConnectionClosed = ConnectionError
    fake_ws_asyncio_client = types.ModuleType("websockets.asyncio.client")
    fake_ws_asyncio_client.connect = lambda *a, **kw: _FakeCM()
    fake_ws_asyncio = types.ModuleType("websockets.asyncio")
    fake_ws_asyncio.client = fake_ws_asyncio_client
    fake_ws_mod.asyncio = fake_ws_asyncio

    fake_agentcore_runtime = types.ModuleType("bedrock_agentcore.runtime")
    fake_arc = MagicMock()
    fake_arc.return_value.generate_ws_connection.return_value = (
        "wss://fake.url", {"Authorization": "SigV4"}
    )
    fake_agentcore_runtime.AgentCoreRuntimeClient = fake_arc

    import boto3.dynamodb.conditions as _real_ddb_cond

    monkeypatch.setitem(sys.modules, "boto3", fake_boto3)
    monkeypatch.setitem(sys.modules, "websockets", fake_ws_mod)
    monkeypatch.setitem(sys.modules, "websockets.asyncio", fake_ws_asyncio)
    monkeypatch.setitem(sys.modules, "websockets.asyncio.client", fake_ws_asyncio_client)
    monkeypatch.setitem(sys.modules, "bedrock_agentcore.runtime", fake_agentcore_runtime)
    monkeypatch.setitem(sys.modules, "boto3.dynamodb.conditions", _real_ddb_cond)

    import argparse
    args = argparse.Namespace(
        stage="staging", region="us-west-2", scenario="unasked",
        max_tries=5, timeout=45.0, after=0.0,
    )

    with pytest.raises(RuntimeError, match="simulated log error"):
        _asyncio.run(lv.run(args))

    history_table.query.assert_called()
    history_table.delete_item.assert_called()
    idp_mock.admin_delete_user.assert_called_once()


# ──────────────────────────────────────────────────────────────────────────────
# _polly_pcm_frames
# ──────────────────────────────────────────────────────────────────────────────


def _fake_polly(pcm_bytes: bytes) -> MagicMock:
    polly = MagicMock()
    polly.synthesize_speech.return_value = {"AudioStream": BytesIO(pcm_bytes)}
    return polly


def test_polly_frames_are_base64_encoded_200ms_chunks() -> None:
    import base64 as _b64
    pcm = b"\x01" * (lv._FRAME_BYTES * 3)
    frames = lv._polly_pcm_frames(_fake_polly(pcm), "hello", "us-west-2")
    assert len(frames) == 3
    for f in frames:
        assert len(_b64.b64decode(f)) == lv._FRAME_BYTES


def test_polly_last_frame_is_zero_padded() -> None:
    import base64 as _b64
    pcm = b"\xff" * (lv._FRAME_BYTES + lv._FRAME_BYTES // 2)
    frames = lv._polly_pcm_frames(_fake_polly(pcm), "hi", "us-west-2")
    assert len(frames) == 2
    last = _b64.b64decode(frames[-1])
    assert last[lv._FRAME_BYTES // 2:] == b"\x00" * (lv._FRAME_BYTES - lv._FRAME_BYTES // 2)


def test_polly_calls_synthesize_with_correct_params() -> None:
    polly = _fake_polly(b"\x00" * lv._FRAME_BYTES)
    lv._polly_pcm_frames(polly, "test text", "us-west-2")
    call_kw = polly.synthesize_speech.call_args.kwargs
    assert call_kw["OutputFormat"] == "pcm"
    assert call_kw["SampleRate"] == "16000"
    assert call_kw["VoiceId"] == "Joanna"
    assert call_kw["Engine"] == "neural"
    assert call_kw["Text"] == "test text"
