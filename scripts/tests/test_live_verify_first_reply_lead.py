"""Stub tests for scripts/live-verify-first-reply-lead.py.

Pins the Critical fixes from review cycles 1 and 2
(issues/2026-10-30-safe-to-drive-narration/review-live-check-cycle1.md):

  1. Helpers (_delete_session_bookings, _run_cleanup): correctness of the cleanup path.
  2. run() wiring: an error after the bidi session still reaches _run_cleanup via the
     finally block, deleting any booking row AND deleting the throwaway user.
     Mutant kill: changing 'finally:' to 'except ZeroDivisionError:' makes this test fail.

No AWS calls; all DDB and IDP interactions are mocked.
"""
from __future__ import annotations

import importlib.util
import json
import sys
import time
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest

_SCRIPT = Path(__file__).resolve().parents[1] / "live-verify-first-reply-lead.py"


def _load():
    spec = importlib.util.spec_from_file_location("_lv_frl_under_test", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


lv = _load()


# ---------------------------------------------------------------------------
# _delete_session_bookings
# ---------------------------------------------------------------------------


def _table_with_rows(*rows):
    """A fake DDB Table whose query() returns the given row dicts (single page)."""
    table = MagicMock()
    table.query.return_value = {"Items": list(rows)}
    return table


def test_delete_session_bookings_calls_delete_item_for_each_row() -> None:
    row1 = {"vehicleId": "VEH-1", "serviceDate": "2026-11-02T12:00:00",
            "sessionId": "s1", "requestNumber": "R1"}
    row2 = {"vehicleId": "VEH-1", "serviceDate": "2026-11-02T12:01:00",
            "sessionId": "s1", "requestNumber": "R2"}
    table = _table_with_rows(row1, row2)

    lv._delete_session_bookings(table, "VEH-1", "s1", "2026-11-02T11:00:00")

    assert table.delete_item.call_count == 2
    keys = {c.kwargs["Key"]["serviceDate"] for c in table.delete_item.call_args_list}
    assert keys == {"2026-11-02T12:00:00", "2026-11-02T12:01:00"}


def test_delete_session_bookings_prints_message_when_no_rows(capsys) -> None:
    table = _table_with_rows()
    lv._delete_session_bookings(table, "VEH-1", "s1", "2026-11-02T11:00:00")
    table.delete_item.assert_not_called()
    assert "no booking rows" in capsys.readouterr().out


def test_delete_session_bookings_paginates_on_last_evaluated_key() -> None:
    table = MagicMock()
    table.query.side_effect = [
        {
            "Items": [{"vehicleId": "V", "serviceDate": "T1", "sessionId": "s", "requestNumber": "R1"}],
            "LastEvaluatedKey": {"vehicleId": "V", "serviceDate": "T1"},
        },
        {
            "Items": [{"vehicleId": "V", "serviceDate": "T2", "sessionId": "s", "requestNumber": "R2"}],
        },
    ]
    lv._delete_session_bookings(table, "V", "s", "T0")
    assert table.delete_item.call_count == 2
    assert table.query.call_count == 2
    _, second_kw = table.query.call_args_list[1]
    assert second_kw.get("ExclusiveStartKey") or table.query.call_args_list[1].kwargs.get("ExclusiveStartKey")


def test_query_session_bookings_returns_only_matching_rows() -> None:
    row = {"vehicleId": "VEH-2", "serviceDate": "2026-11-02T10:00:00", "sessionId": "s2"}
    table = _table_with_rows(row)
    rows = lv._query_session_bookings(table, "VEH-2", "s2", "2026-11-02T09:00:00")
    assert rows == [row]


# ---------------------------------------------------------------------------
# _run_cleanup  (both steps run even when one fails)
# ---------------------------------------------------------------------------


def _now_ms() -> int:
    return int(time.time() * 1000) - 60_000


def test_run_cleanup_calls_delete_booking_and_delete_user() -> None:
    table = _table_with_rows(
        {"vehicleId": "VEH-1", "serviceDate": "2026-11-02T12:00:00",
         "sessionId": "sess-1", "requestNumber": "R1"},
    )
    idp = MagicMock()

    lv._run_cleanup(
        idp=idp, pool="pool-1", user="user@example.com", created=True,
        history_table=table, vid="VEH-1", session_id="sess-1",
        start_ms=_now_ms(), stage="staging",
    )

    table.delete_item.assert_called_once()
    idp.admin_delete_user.assert_called_once_with(
        UserPoolId="pool-1", Username="user@example.com",
    )


def test_run_cleanup_skips_booking_cleanup_when_session_id_not_set() -> None:
    table = MagicMock()
    idp = MagicMock()

    lv._run_cleanup(
        idp=idp, pool="pool-1", user="user@example.com", created=True,
        history_table=table, vid="VEH-1", session_id=None,
        start_ms=_now_ms(), stage="staging",
    )

    table.query.assert_not_called()
    idp.admin_delete_user.assert_called_once()


def test_run_cleanup_skips_booking_cleanup_when_start_ms_not_set() -> None:
    table = MagicMock()
    idp = MagicMock()

    lv._run_cleanup(
        idp=idp, pool="pool-1", user="user@example.com", created=True,
        history_table=table, vid="VEH-1", session_id="sess-1",
        start_ms=None, stage="staging",
    )

    table.query.assert_not_called()
    idp.admin_delete_user.assert_called_once()


def test_error_after_session_still_deletes_booking_and_user(capsys) -> None:
    """Booking deletion raises → the user is still deleted."""
    table = MagicMock()
    table.query.return_value = {
        "Items": [{"vehicleId": "VEH-1", "serviceDate": "2026-11-02T12:00:00",
                   "sessionId": "s1", "requestNumber": "R99"}]
    }
    table.delete_item.side_effect = RuntimeError("simulated DDB error after session")
    idp = MagicMock()

    lv._run_cleanup(
        idp=idp, pool="pool-1", user="user@example.com", created=True,
        history_table=table, vid="VEH-1", session_id="s1",
        start_ms=_now_ms(), stage="staging",
    )

    table.delete_item.assert_called_once()
    idp.admin_delete_user.assert_called_once_with(
        UserPoolId="pool-1", Username="user@example.com",
    )
    err = capsys.readouterr().err
    assert "VEH-1" in err
    assert "s1" in err


def test_user_deletion_failure_prints_manual_command(capsys) -> None:
    table = _table_with_rows()
    idp = MagicMock()
    idp.admin_delete_user.side_effect = RuntimeError("IDP unavailable")

    lv._run_cleanup(
        idp=idp, pool="pool-99", user="orphan@example.com", created=True,
        history_table=table, vid=None, session_id=None,
        start_ms=None, stage="staging",
    )

    err = capsys.readouterr().err
    assert "orphan@example.com" in err
    assert "admin-delete-user" in err
    assert "pool-99" in err


# ---------------------------------------------------------------------------
# run() wiring: cleanup called from finally even when filter_log_events raises
# ---------------------------------------------------------------------------


def test_run_cleanup_wired_to_finally_on_filter_log_error(monkeypatch) -> None:
    """run() must call _run_cleanup from its finally block when filter_log_events raises.

    Mutant tried: 'finally:' changed to 'except ZeroDivisionError:' — when
    filter_log_events raises RuntimeError, the except clause does not match, the
    exception propagates without calling _run_cleanup, and admin_delete_user is
    never called.  This test fails on that mutant.
    """
    import asyncio as _asyncio

    # Patch asyncio.sleep to return after one event-loop tick (no real delay).
    _orig_sleep = _asyncio.sleep

    async def _fast_sleep(delay=0, *a, **kw):  # noqa: ANN001
        await _orig_sleep(0)

    monkeypatch.setattr(lv.asyncio, "sleep", _fast_sleep)

    # ── fake websocket ───────────────────────────────────────────────────────
    class _FakeWS:
        _n: int = 0

        async def send(self, data: str) -> None:  # noqa: ANN001
            pass

        async def recv(self) -> str:
            self._n += 1
            if self._n == 1:
                # One tool.call so found.wait() resolves immediately.
                return json.dumps({"type": "tool.call", "name": "find_service_center"})
            raise _asyncio.TimeoutError()

    class _FakeCM:
        async def __aenter__(self):
            return _FakeWS()

        async def __aexit__(self, *a):
            return False

    # ── fake boto3 ───────────────────────────────────────────────────────────
    history_table = MagicMock()
    # Return one row so delete_item is called (proves both cleanup steps fire).
    history_table.query.return_value = {
        "Items": [{"vehicleId": "VEH-T01", "serviceDate": "2026-11-02T12:00:00",
                   "sessionId": "any", "requestNumber": "R99"}]
    }

    idp_mock = MagicMock()
    idp_mock.get_paginator.return_value.paginate.return_value = [
        {"UserPoolClients": [{"ClientId": "cid-001"}]}
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
    ssm_mock.get_parameter.return_value = {"Parameter": {"Value": "pool-id-test"}}

    cf_mock = MagicMock()
    cf_mock.describe_stacks.return_value = {"Stacks": [{"Outputs": [
        {"OutputKey": "RestApiUrl", "OutputValue": "https://api.example.com"}
    ]}]}

    ctl_mock = MagicMock()
    ctl_mock.list_agent_runtimes.return_value = {"agentRuntimes": [{
        "agentRuntimeName": "vsa_supervisor_bidi_staging",
        "agentRuntimeArn": "arn:aws:bedrock-agentcore:us-west-2:123456789012:runtime/r1",
        "agentRuntimeId": "rid-001",
        "agentRuntimeVersion": "1.0.0",
        "lastUpdatedAt": "2026-11-02",
    }]}

    logs_mock = MagicMock()
    logs_mock.filter_log_events.side_effect = RuntimeError(
        "simulated log error after session")

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

    # Skip real triage/vehicle-selection stack.
    fake_lead = MagicMock(code="P0A00", verdict="limited", text="test lead")
    monkeypatch.setattr(lv, "pick_vehicle",
                        lambda *a, **kw: ("VIN-T01", "VEH-T01", "P1", fake_lead))

    # ── fake websockets module ────────────────────────────────────────────────
    fake_ws_mod = types.ModuleType("websockets")
    fake_ws_mod.ConnectionClosed = ConnectionError
    fake_ws_asyncio_client = types.ModuleType("websockets.asyncio.client")
    fake_ws_asyncio_client.connect = lambda *a, **kw: _FakeCM()
    fake_ws_asyncio = types.ModuleType("websockets.asyncio")
    fake_ws_asyncio.client = fake_ws_asyncio_client
    fake_ws_mod.asyncio = fake_ws_asyncio

    # ── fake bedrock_agentcore.runtime ────────────────────────────────────────
    fake_agentcore_runtime = types.ModuleType("bedrock_agentcore.runtime")
    fake_arc = MagicMock()
    fake_arc.return_value.generate_ws_connection.return_value = (
        "wss://fake.url", {"Authorization": "SigV4-fake"}
    )
    fake_agentcore_runtime.AgentCoreRuntimeClient = fake_arc

    # Keep real boto3.dynamodb.conditions (pure Python, no network calls).
    import boto3.dynamodb.conditions as _real_ddb_cond

    monkeypatch.setitem(sys.modules, "boto3", fake_boto3)
    monkeypatch.setitem(sys.modules, "websockets", fake_ws_mod)
    monkeypatch.setitem(sys.modules, "websockets.asyncio", fake_ws_asyncio)
    monkeypatch.setitem(sys.modules, "websockets.asyncio.client", fake_ws_asyncio_client)
    monkeypatch.setitem(sys.modules, "bedrock_agentcore.runtime", fake_agentcore_runtime)
    monkeypatch.setitem(sys.modules, "boto3.dynamodb.conditions", _real_ddb_cond)

    import argparse
    args = argparse.Namespace(
        stage="staging", region="us-west-2",
        max_tries=5, timeout=45.0, after=0.0,
    )

    with pytest.raises(RuntimeError, match="simulated log error after session"):
        _asyncio.run(lv.run(args))

    # Both cleanup steps must have run despite the exception.
    history_table.query.assert_called()     # booking cleanup was attempted
    history_table.delete_item.assert_called()  # the one booking row was deleted
    idp_mock.admin_delete_user.assert_called_once()  # throwaway user deleted


# ---------------------------------------------------------------------------
# Gate tests (no AWS)
# ---------------------------------------------------------------------------


@pytest.fixture()
def _no_boto3(monkeypatch):
    fake = MagicMock(side_effect=AssertionError("boto3 used before all gates passed"))
    monkeypatch.setattr(lv, "boto3", fake, raising=False)
    monkeypatch.setattr(lv.asyncio, "run",
                        MagicMock(side_effect=AssertionError("asyncio.run called")))


def test_refused_without_live_tests(monkeypatch, _no_boto3) -> None:
    monkeypatch.delenv("LIVE_TESTS", raising=False)
    assert lv.main(["--allow-writes"]) == 1


def test_refused_without_allow_writes(monkeypatch, _no_boto3) -> None:
    monkeypatch.setenv("LIVE_TESTS", "1")
    assert lv.main([]) == 1


def test_region_choice_is_pinned_to_us_west_2(monkeypatch) -> None:
    monkeypatch.setenv("LIVE_TESTS", "1")
    with pytest.raises(SystemExit) as exc:
        lv.main(["--allow-writes", "--region", "eu-west-1"])
    assert exc.value.code != 0
