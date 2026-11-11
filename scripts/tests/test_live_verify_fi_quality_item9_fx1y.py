"""Lane-Y fix-round-1 tests for --part item9: W1/W5/W6/W7/S4/S7/S8/C1a/C1b/W8.

Findings addressed (all cleanup-side or main()-dispatch):
  W1 (cleanup): re-read/re-delete loop when invoke_sent but not invoke_answered.
  W5: try/finally around cleanup hold so rows survive signal re-raise.
  W6: GetQueueUrl failure no longer skips row step or residue.
  W7: alarm-history wait when DLQ sent but alarm not seen; alarm_after_delete fake.
  S4: CMV/DeleteMessage errors recorded; visibility restored to 0 on delete fail.
  S7: --part item9 --run-id <existing> refused before any AWS call.
  S8: non-SystemExit from _run_item9 records item9-exception FAIL row.
  C1a: one test per write (Invoke, SendMessage) where window closes just before it.
  C1b: router hook on each write asserting manifest flags already on disk.
  W8: foreign-visibility assert; row-delete FAIL; residue FAILs; step-raise no-skip.

Run:
    nohup .venv/bin/python -m pytest \\
        scripts/tests/test_live_verify_fi_quality_item9_fx1y.py -v --no-header \\
        > /tmp/2026-10-05-fi-fleet-health-stage-d-65fx1y.log 2>&1 &

Requires:
    ~/automotive-data-platform-on-aws (FiLoopWorld)
    ~/connected-mobility-guidance-on-aws (FiLoopWorld)
"""
from __future__ import annotations

import datetime as dt
import json
import os
import signal
import uuid
from pathlib import Path
from typing import Any, Iterator

import pytest

# ---------------------------------------------------------------------------
# Re-use helpers and fixtures from the base item9 test module
# ---------------------------------------------------------------------------
from scripts.tests.test_live_verify_fi_quality_item9 import (  # noqa: E402
    _REPOS_PRESENT,
    _SKIP_REASON,
    _RUN_ID,
    _RUN_SUFFIX,
    _PATTERN_ID,
    _DLQ_BODY,
    _INVESTIGATIONS_TABLE,
    _INVESTIGATIONS_DLQ,
    _DLQ_ALARM_NAME,
    _call_main,
    _argv,
    _dlq_depth,
    _inv_row,
    _open_world,
    _scan_investigations_for_pattern,
    _router_writes,
    lv,
    STAGE,
    REGION,
    ACCOUNT,
)


# ---------------------------------------------------------------------------
# Autouse fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _restore_signals() -> Iterator[None]:
    old_int = signal.getsignal(signal.SIGINT)
    old_term = signal.getsignal(signal.SIGTERM)
    old_hup = signal.getsignal(signal.SIGHUP)
    yield
    signal.signal(signal.SIGINT, old_int)
    signal.signal(signal.SIGTERM, old_term)
    signal.signal(signal.SIGHUP, old_hup)


@pytest.fixture(autouse=True)
def _manifest_dir_guard(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("FI_QUALITY_MANIFEST_DIR", str(tmp_path))


# ---------------------------------------------------------------------------
# Helper: write a cleanup-only manifest
# ---------------------------------------------------------------------------


def _write_manifest(tmp_path: Path, extra: dict | None = None) -> None:
    """Write a minimal item9 manifest for _RUN_ID into tmp_path."""
    m: dict = {
        "run_id": _RUN_ID,
        "stage": STAGE,
        "part": "item9",
        "item9_row_put_sent": False,
        "item9_invoke_sent": False,
        "item9_invoke_answered": True,
        "item9_dlq_sent": False,
        "item9_investigation_id": None,
        "item9_pattern_id": _PATTERN_ID,
        "item9_dlq_body": None,
        "item9_dlq_send_at": None,
        "item9_dlq_message_id": None,
    }
    if extra:
        m.update(extra)
    mp = tmp_path / _RUN_ID
    mp.mkdir(parents=True, exist_ok=True)
    (mp / "manifest.json").write_text(json.dumps(m))


def _cleanup_argv() -> list[str]:
    return ["--stage", STAGE, "--cleanup-only", "--run-id", _RUN_ID, "--allow-writes"]


# ===========================================================================
# W6: GetQueueUrl failure does not skip row step
# ===========================================================================


@pytest.mark.skipif(not _REPOS_PRESENT, reason=_SKIP_REASON)
def test_w6_getqueueurl_fail_row_still_deleted(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """GetQueueUrl raises in DLQ step → DLQ FAIL; row step still deletes the row.

    Before fix: GetQueueUrl was outside any try, so an exception escaped
    _item9_cleanup entirely and the row delete never ran.
    """
    monkeypatch.setenv("LIVE_TESTS", "1")

    inv_id = str(uuid.uuid4())

    with _open_world() as w:
        w.table(_INVESTIGATIONS_TABLE).put_item(Item={
            "investigation_id": inv_id,
            "pattern_id": _PATTERN_ID,
            "status": "failed",
            "requested_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "requested_by": f"LIVETEST-{_RUN_ID}",
            "livetest_run_id": _RUN_ID,
        })

        _write_manifest(tmp_path, extra={
            "item9_row_put_sent": True,
            "item9_invoke_sent": True,
            "item9_invoke_answered": True,
            "item9_investigation_id": inv_id,
            "item9_dlq_sent": True,
            "item9_dlq_body": _DLQ_BODY,
            "item9_dlq_send_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "item9_alarm_seen_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        })

        # Make GetQueueUrl fail for the DLQ
        from scripts.tests.fi_loop_world.world import INVESTIGATIONS_DLQ  # noqa: PLC0415

        def _fail_gqu(p: dict[str, Any]) -> None:
            from scripts.tests.fi_rw_world.awsfake import AwsError  # noqa: PLC0415
            if p.get("QueueName") == INVESTIGATIONS_DLQ:
                raise AwsError("QueueDoesNotExist", "no DLQ for test", status=400)
            return None

        w.router.add("sqs", "GetQueueUrl", _fail_gqu)

        rc, rows = _call_main(monkeypatch, _cleanup_argv())
        row_after = _inv_row(w, inv_id)

    # Row step must still have run and deleted the row
    assert row_after == {}, "row must be deleted even when GetQueueUrl fails for DLQ"
    # DLQ step FAIL
    assert rows.get("item9-cleanup-dlq") == "FAIL", (
        f"item9-cleanup-dlq should be FAIL (GetQueueUrl error); rows={rows}"
    )
    # Foreign step FAIL (not reached — both DLQ rows set to FAIL in the except)
    assert rows.get("item9-cleanup-dlq-foreign") == "FAIL", (
        f"item9-cleanup-dlq-foreign should also be FAIL (not reached); rows={rows}"
    )
    assert rc == 1, f"expected rc=1; got rc={rc}"


@pytest.mark.skipif(not _REPOS_PRESENT, reason=_SKIP_REASON)
def test_w6_getqueueurl_fail_residue_still_runs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """GetQueueUrl raises only in residue step → residue FAIL; row step ran first.

    The row step uses no DLQ URL, so it must complete before residue fails.
    """
    monkeypatch.setenv("LIVE_TESTS", "1")

    inv_id = str(uuid.uuid4())
    _gqu_calls = [0]

    with _open_world() as w:
        w.table(_INVESTIGATIONS_TABLE).put_item(Item={
            "investigation_id": inv_id,
            "pattern_id": _PATTERN_ID,
            "status": "failed",
            "requested_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "requested_by": f"LIVETEST-{_RUN_ID}",
            "livetest_run_id": _RUN_ID,
        })

        _write_manifest(tmp_path, extra={
            "item9_row_put_sent": True,
            "item9_invoke_sent": True,
            "item9_invoke_answered": True,
            "item9_investigation_id": inv_id,
            "item9_dlq_sent": False,   # DLQ step skipped (nothing sent)
        })

        # Fail GetQueueUrl on first call (the residue step)
        from scripts.tests.fi_loop_world.world import INVESTIGATIONS_DLQ  # noqa: PLC0415

        def _fail_gqu_once(p: dict[str, Any]) -> None:
            from scripts.tests.fi_rw_world.awsfake import AwsError  # noqa: PLC0415
            _gqu_calls[0] += 1
            if _gqu_calls[0] >= 1 and p.get("QueueName") == INVESTIGATIONS_DLQ:
                raise AwsError("QueueDoesNotExist", "residue test", status=400)
            return None

        w.router.add("sqs", "GetQueueUrl", _fail_gqu_once)

        rc, rows = _call_main(monkeypatch, _cleanup_argv())
        row_after = _inv_row(w, inv_id)

    # Row step ran (DLQ was not sent so DLQ step says "nothing sent")
    assert row_after == {}, "row must be deleted before residue runs"
    assert rows.get("item9-cleanup-row") == "PASS", (
        f"row step should PASS; rows={rows}"
    )
    # Residue FAIL (GetQueueUrl failed)
    assert rows.get("item9-residue") == "FAIL", (
        f"residue should FAIL when GetQueueUrl fails; rows={rows}"
    )


# ===========================================================================
# W1 (cleanup): invoke_sent and not invoke_answered → re-read/re-delete loop
# ===========================================================================


@pytest.mark.skipif(not _REPOS_PRESENT, reason=_SKIP_REASON)
def test_w1_invoke_no_answer_row_recreated_deleted(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Row recreated without pattern_id (Lambda _set_failed) → W1 loop deletes it.

    Scenario (cleanup-only):
    - Manifest: invoke_sent=True, invoke_answered=False, invoke_started_at=now
    - DDB: row with investigation_id but no pattern_id (simulating _set_failed)
    - Expected: W1 loop deletes the recreated row; cleanup-row PASS, residue PASS.
    """
    monkeypatch.setenv("LIVE_TESTS", "1")

    inv_id = str(uuid.uuid4())

    with _open_world() as w:
        # W11: take invoke_started_at inside the world — world clock is 12:00Z.
        invoke_started_at = dt.datetime.now(dt.timezone.utc).isoformat()
        w.dlq_alarm.never_fires = True

        # "Recreated" row: no pattern_id (as Lambda's _set_failed would write)
        w.table(_INVESTIGATIONS_TABLE).put_item(Item={
            "investigation_id": inv_id,
            "status": "failed",
            "error_message": "PatternNotFound",
        })

        _write_manifest(tmp_path, extra={
            "item9_row_put_sent": True,
            "item9_invoke_sent": True,
            "item9_invoke_answered": False,    # ← triggers W1 loop
            "item9_invoke_started_at": invoke_started_at,
            "item9_investigation_id": inv_id,
            "item9_dlq_sent": False,
        })

        rc, rows = _call_main(monkeypatch, _cleanup_argv())
        row_after = _inv_row(w, inv_id)

    assert row_after == {}, "W1 loop must delete the recreated row (no pattern_id)"
    assert rows.get("item9-cleanup-row") == "PASS", (
        f"cleanup-row PASS after W1 re-delete; rows={rows}"
    )
    assert rc == 0, f"expected rc=0; got rc={rc}. rows={rows}"


@pytest.mark.skipif(not _REPOS_PRESENT, reason=_SKIP_REASON)
def test_w1_late_handler_write_full_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Full run with late_handler_write: invoke raises → link1-invoke FAIL, cleanup runs.

    FiLoopWorld(late_handler_write=True) makes the invoke raise EndpointConnectionError.
    The exception is caught inside _run_item9 as item9-link1-invoke: FAIL.
    The W1 re-read loop fires (invoke_sent=True, invoke_answered not set → False)
    but exits immediately since the initial delete succeeded (row has our pattern_id).
    cleanup-row: PASS; rc=1 from the FAIL rows.
    """
    monkeypatch.setenv("LIVE_TESTS", "1")

    from scripts.tests.fi_loop_world.world import FiLoopWorld  # noqa: PLC0415

    with FiLoopWorld(auto_pump=False, daily_cap=0, late_handler_write=True,
                     restore_live_tests=True) as w:
        w.dlq_alarm.never_fires = True

        rc, rows = _call_main(monkeypatch, _argv())
        rows_after = _scan_investigations_for_pattern(w, _PATTERN_ID)

    # Invoke raised → link1-invoke FAIL (caught inside _run_item9)
    assert rows.get("item9-link1-invoke") == "FAIL", (
        f"item9-link1-invoke should FAIL when invoke raises; rows={rows}"
    )
    # Cleanup ran and deleted the row (W1 loop fires but row was already deleted)
    assert rows_after == [], "cleanup must delete the row even after invoke error"
    assert rows.get("item9-cleanup-row") == "PASS", (
        f"cleanup-row should PASS (W1 loop exits immediately; row deleted normally); rows={rows}"
    )
    assert rc == 1, f"expected rc=1 from FAIL rows; got rc={rc}"


# ===========================================================================
# W5: try/finally ensures rows survive SIGINT re-raise during cleanup
# ===========================================================================


@pytest.mark.skipif(not _REPOS_PRESENT, reason=_SKIP_REASON)
def test_w7_alarm_after_delete_residue_passes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """ALARM fires after DLQ message deleted → W7 waits; sees ALARM then OK → PASS.

    alarm_after_delete=True means the alarm fires even after depth→0.
    The W7 poll in residue finds the ALARM StateUpdate, then the normal 15-min
    OK wait sees the alarm return to OK.
    """
    monkeypatch.setenv("LIVE_TESTS", "1")

    inv_id = str(uuid.uuid4())

    with _open_world() as w:
        # W11: take send_at inside the world — world clock is 12:00Z.
        send_at = dt.datetime.now(dt.timezone.utc)
        w.dlq_alarm.alarm_after_delete = True   # fires after depth → 0
        w.dlq_alarm.alarm_delay_s = 60.0        # fires 60 s after send
        w.dlq_alarm.recover_delay_s = 60.0      # recovers 60 s after depth→0

        _write_manifest(tmp_path, extra={
            "item9_row_put_sent": True,
            "item9_invoke_sent": True,
            "item9_invoke_answered": True,
            "item9_investigation_id": inv_id,
            "item9_dlq_sent": True,
            "item9_dlq_body": _DLQ_BODY,
            "item9_dlq_send_at": send_at.isoformat(),
            # alarm_seen_at NOT set → W7 mode
        })

        w.table(_INVESTIGATIONS_TABLE).put_item(Item={
            "investigation_id": inv_id,
            "pattern_id": _PATTERN_ID,
            "status": "failed",
            "requested_at": send_at.isoformat(),
            "requested_by": f"LIVETEST-{_RUN_ID}",
            "livetest_run_id": _RUN_ID,
        })
        sqs = w.internal("sqs")
        dlq_url = sqs.get_queue_url(QueueName=_INVESTIGATIONS_DLQ)["QueueUrl"]
        sqs.send_message(QueueUrl=dlq_url, MessageBody=_DLQ_BODY)

        rc, rows = _call_main(monkeypatch, _cleanup_argv())
        row_after = _inv_row(w, inv_id)
        dlq_vis = _dlq_depth(w)

    assert rc == 0, f"expected rc=0 (alarm after delete still PASS); rows={rows}"
    assert rows.get("item9-residue") == "PASS", (
        f"residue should PASS (ALARM followed by OK); rows={rows}"
    )
    assert row_after == {}, "row must be deleted"
    assert dlq_vis == 0, "DLQ must be empty"


@pytest.mark.skipif(not _REPOS_PRESENT, reason=_SKIP_REASON)
def test_w7_no_alarm_within_25min_residue_passes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """DLQ sent, alarm never fires within 25 min → residue PASS (alarm never fired).

    When alarm_seen_at is not in the manifest and never_fires=True, the W7 poll
    exhausts without finding an ALARM StateUpdate.  The residue reports
    'alarm never fired' as a PASS.
    """
    monkeypatch.setenv("LIVE_TESTS", "1")

    inv_id = str(uuid.uuid4())

    with _open_world() as w:
        # W11: take send_at inside the world — world clock is 12:00Z.
        send_at = dt.datetime.now(dt.timezone.utc)
        w.dlq_alarm.never_fires = True

        _write_manifest(tmp_path, extra={
            "item9_row_put_sent": True,
            "item9_invoke_sent": True,
            "item9_invoke_answered": True,
            "item9_investigation_id": inv_id,
            "item9_dlq_sent": True,
            "item9_dlq_body": _DLQ_BODY,
            "item9_dlq_send_at": send_at.isoformat(),
            # alarm_seen_at NOT set → W7 mode; alarm will never fire
        })

        w.table(_INVESTIGATIONS_TABLE).put_item(Item={
            "investigation_id": inv_id,
            "pattern_id": _PATTERN_ID,
            "status": "failed",
            "requested_at": send_at.isoformat(),
            "requested_by": f"LIVETEST-{_RUN_ID}",
            "livetest_run_id": _RUN_ID,
        })
        sqs = w.internal("sqs")
        dlq_url = sqs.get_queue_url(QueueName=_INVESTIGATIONS_DLQ)["QueueUrl"]
        sqs.send_message(QueueUrl=dlq_url, MessageBody=_DLQ_BODY)

        rc, rows = _call_main(monkeypatch, _cleanup_argv())

    assert rc == 0, f"expected rc=0 when alarm never fired; rows={rows}"
    assert rows.get("item9-residue") == "PASS", (
        f"residue should PASS saying alarm never fired; rows={rows}"
    )


# ===========================================================================
# S4: DeleteMessage errors recorded; visibility=0 on delete failure
# ===========================================================================


@pytest.mark.skipif(not _REPOS_PRESENT, reason=_SKIP_REASON)
def test_s4_deletemessage_fail_restores_visibility(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """DeleteMessage fails → FAIL recorded; ChangeMessageVisibility(0) called.

    Before fix: delete errors were swallowed (except: pass), visibility not restored.
    After fix: error in FAIL text; CMV(VisibilityTimeout=0) called for our message.
    """
    monkeypatch.setenv("LIVE_TESTS", "1")

    send_at = dt.datetime.now(dt.timezone.utc)
    _cmv_zero_calls: list[dict] = []

    with _open_world() as w:
        sqs = w.internal("sqs")
        dlq_url = sqs.get_queue_url(QueueName=_INVESTIGATIONS_DLQ)["QueueUrl"]
        sqs.send_message(QueueUrl=dlq_url, MessageBody=_DLQ_BODY)

        _write_manifest(tmp_path, extra={
            "item9_dlq_sent": True,
            "item9_dlq_body": _DLQ_BODY,
            "item9_dlq_send_at": send_at.isoformat(),
            "item9_alarm_seen_at": send_at.isoformat(),
        })

        def _fail_delete(p: dict[str, Any]) -> None:
            from scripts.tests.fi_rw_world.awsfake import AwsError  # noqa: PLC0415
            raise AwsError("ReceiptHandleIsInvalid", "handle expired", status=400)

        def _capture_cmv(p: dict[str, Any]) -> None:
            if int(p.get("VisibilityTimeout", -1)) == 0:
                _cmv_zero_calls.append(dict(p))
            return None

        w.router.add("sqs", "DeleteMessage", _fail_delete)
        w.router.add("sqs", "ChangeMessageVisibility", _capture_cmv)

        rc, rows = _call_main(monkeypatch, _cleanup_argv())

    assert rows.get("item9-cleanup-dlq") == "FAIL", (
        f"expected FAIL on delete error; rows={rows}"
    )
    assert _cmv_zero_calls, (
        "ChangeMessageVisibility(VisibilityTimeout=0) must be called when delete fails"
    )


@pytest.mark.skipif(not _REPOS_PRESENT, reason=_SKIP_REASON)
def test_s4_cmv_error_recorded_for_foreign_message(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """CMV error for a foreign message → error text in cleanup-dlq-foreign FAIL.

    Before fix: CMV errors for foreign messages were silently swallowed.
    """
    monkeypatch.setenv("LIVE_TESTS", "1")

    send_at = dt.datetime.now(dt.timezone.utc)

    with _open_world() as w:
        sqs = w.internal("sqs")
        dlq_url = sqs.get_queue_url(QueueName=_INVESTIGATIONS_DLQ)["QueueUrl"]
        sqs.send_message(QueueUrl=dlq_url, MessageBody=_DLQ_BODY)
        sqs.send_message(QueueUrl=dlq_url, MessageBody="foreign-s4-test")

        _write_manifest(tmp_path, extra={
            "item9_dlq_sent": True,
            "item9_dlq_body": _DLQ_BODY,
            "item9_dlq_send_at": send_at.isoformat(),
            "item9_alarm_seen_at": send_at.isoformat(),
        })

        def _fail_cmv(p: dict[str, Any]) -> None:
            from scripts.tests.fi_rw_world.awsfake import AwsError  # noqa: PLC0415
            raise AwsError("InvalidParameterValue", "CMV fail test", status=400)

        w.router.add("sqs", "ChangeMessageVisibility", _fail_cmv)

        rc, rows = _call_main(monkeypatch, _cleanup_argv())

    assert rows.get("item9-cleanup-dlq-foreign") == "FAIL", (
        f"expected item9-cleanup-dlq-foreign=FAIL (foreign + CMV error); rows={rows}"
    )


# ===========================================================================
# S7: existing manifest refused with no AWS call
# ===========================================================================


@pytest.mark.skipif(not _REPOS_PRESENT, reason=_SKIP_REASON)
def test_s7_existing_manifest_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """--part item9 --run-id <existing> → rc=1, no AWS calls.

    Before S7 fix: the manifest was overwritten immediately, so --cleanup-only
    would read "nothing sent" while the original rows/messages remained.
    """
    monkeypatch.setenv("LIVE_TESTS", "1")

    with _open_world() as w:
        # Create the manifest first
        _write_manifest(tmp_path)

        w.invocations_before_main = len(w.router.all_calls)
        rc, rows = _call_main(monkeypatch, _argv())
        calls_after = w.router.all_calls[w.invocations_before_main:]

    assert rc == 1, f"expected rc=1 (manifest exists); got rc={rc}"
    assert rows == {}, f"no rows should be produced; got {rows}"
    assert calls_after == [], (
        f"no AWS calls should be made when manifest exists; got {calls_after}"
    )


# ===========================================================================
# S8: non-SystemExit exception from _run_item9 → item9-exception FAIL row
# ===========================================================================


@pytest.mark.skipif(not _REPOS_PRESENT, reason=_SKIP_REASON)
def test_s8_exception_records_fail_row_cleanup_runs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Non-SystemExit from _run_item9 → item9-exception FAIL; cleanup still runs.

    The patched _run_item9 sets row_put_sent=True (writes PutItem to DDB) then
    raises RuntimeError.  S8 catches it, records FAIL row.  Cleanup runs in
    finally and deletes the row.
    """
    monkeypatch.setenv("LIVE_TESTS", "1")

    _inv_id_holder: list[str] = []

    with _open_world() as w:
        w.dlq_alarm.alarm_delay_s = 30.0

        def _raising_run(res: Any, **kwargs: Any) -> None:
            inv_id = str(uuid.uuid4())
            _inv_id_holder.append(inv_id)
            kwargs["manifest"]["item9_row_put_sent"] = True
            kwargs["manifest"]["item9_invoke_sent"] = True
            kwargs["manifest"]["item9_invoke_answered"] = True
            kwargs["manifest"]["item9_investigation_id"] = inv_id
            kwargs["manifest"]["item9_pattern_id"] = _PATTERN_ID
            lv._save_manifest(
                kwargs["run_id"], kwargs["manifest"], kwargs.get("manifest_dir")
            )
            w.table(_INVESTIGATIONS_TABLE).put_item(Item={
                "investigation_id": inv_id,
                "pattern_id": _PATTERN_ID,
                "status": "queued",
                "requested_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                "requested_by": "LIVETEST",
                "livetest_run_id": kwargs["run_id"],
            })
            raise RuntimeError("S8 test: simulated non-SystemExit from _run_item9")

        monkeypatch.setattr(lv, "_run_item9", _raising_run)

        rc, rows = _call_main(monkeypatch, _argv())
        row_after = _inv_row(w, _inv_id_holder[0]) if _inv_id_holder else {"present": True}

    assert rows.get("item9-exception") == "FAIL", (
        f"item9-exception=FAIL expected from RuntimeError; rows={rows}"
    )
    assert "item9-cleanup-row" in rows, (
        f"cleanup must run even when _run_item9 raises; rows={rows}"
    )
    assert row_after == {}, "cleanup must delete the row even after exception"
    assert rc == 1, f"expected rc=1; got rc={rc}"


# ===========================================================================
# C1a: window closes just before Invoke (write 2) or SendMessage (write 3)
# ===========================================================================


@pytest.mark.skipif(not _REPOS_PRESENT, reason=_SKIP_REASON)
def test_c1a_window_closes_before_invoke(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Window closes just before Invoke (call 6) → SKIP item9-invoke-window.

    PutItem (calls 4-5) already happened; Invoke must not fire.  Cleanup still
    runs and deletes the row.

    Call sequence in _run_item9:
      1: Guard 1  2: Guard 2  3: Guard 3  4: Guard 4/PutItem  5: requested_at
      6: Invoke guard  7: invoke_start  8: invoke_end  9: send guard
    """
    monkeypatch.setenv("LIVE_TESTS", "1")

    _T0 = dt.datetime(2026, 10, 29, 10, 0, 0, tzinfo=dt.timezone.utc)
    _BLOCKED = dt.datetime(2026, 10, 29, 5, 20, 0, tzinfo=dt.timezone.utc)
    _n = [0]

    def _utc_blocked_at_6() -> dt.datetime:
        _n[0] += 1
        return _BLOCKED if _n[0] >= 6 else _T0

    monkeypatch.setattr(lv, "_utc_now", _utc_blocked_at_6)

    with _open_world() as w:
        w.invocations_before_main = len(w.router.all_calls)
        rc, rows = _call_main(monkeypatch, _argv())
        writes = _router_writes(w)
        rows_after = _scan_investigations_for_pattern(w, _PATTERN_ID)

    invokes = [c for c in writes if c[0] == "lambda"]
    assert invokes == [], f"Invoke must not happen when window closed at call 6; got {invokes}"
    sends = [c for c in writes if c[0] == "sqs"]
    assert sends == [], f"SendMessage must not happen; got {sends}"
    skip_rows = [l for l, s in rows.items() if s == "SKIP"]
    assert skip_rows, f"expected a SKIP row; rows={rows}"
    # PutItem happened → cleanup deletes the row
    assert rows_after == [], (
        f"cleanup must delete the row even when invoke was skipped; found {rows_after}"
    )
    # invoke_started_at not set (Invoke skipped) → W1 loop does not trigger → rc==2
    assert rc == 2, (
        f"rc must be 2 (SKIP, no FAIL); w1-trigger-invoke-sent would trigger W1 without "
        f"invoke_started_at causing a failure; got rc={rc}"
    )
    assert rows.get("item9-cleanup-row") == "PASS", (
        f"cleanup-row must be PASS (row was deleted in cleanup); rows={rows}"
    )


@pytest.mark.skipif(not _REPOS_PRESENT, reason=_SKIP_REASON)
def test_c1a_window_closes_before_sendmessage(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Window closes just before SendMessage (call 9) → SKIP item9-send-window.

    PutItem (call 4-5) and Invoke (calls 6-8) happened; DLQ message must not be sent.
    Cleanup still runs and deletes the investigation row.
    """
    monkeypatch.setenv("LIVE_TESTS", "1")

    _T0 = dt.datetime(2026, 10, 29, 10, 0, 0, tzinfo=dt.timezone.utc)
    _BLOCKED = dt.datetime(2026, 10, 29, 5, 20, 0, tzinfo=dt.timezone.utc)
    _n = [0]

    def _utc_blocked_at_9() -> dt.datetime:
        _n[0] += 1
        return _BLOCKED if _n[0] >= 9 else _T0

    monkeypatch.setattr(lv, "_utc_now", _utc_blocked_at_9)

    with _open_world() as w:
        # S13 fix: plant a run record at T0 so the link-1 run-record check PASSes.
        # The real Lambda handler writes at system time (outside the T0 window); this
        # planted record at T0 ensures S13 does not return early, so the code reaches
        # call 9 (now_send) where the window closes and SKIP item9-send-window fires.
        import boto3 as _boto3_c1a  # noqa: PLC0415
        _ddb_c1a = _boto3_c1a.resource("dynamodb", region_name=REGION)
        _runs_c1a = _ddb_c1a.Table("vsa-staging-avx-agent-runs")
        _planted_at_c1a = _T0.replace(tzinfo=None).isoformat()
        _runs_c1a.put_item(Item={
            "agent_component": "agent-fi-quality#investigation",
            "started_at": _planted_at_c1a,
            "status": "aborted",
            "livetest_run_id": "c1a-planted",
        })
        w.invocations_before_main = len(w.router.all_calls)
        rc, rows = _call_main(monkeypatch, _argv())
        writes = _router_writes(w)
        rows_after = _scan_investigations_for_pattern(w, _PATTERN_ID)

    sends = [c for c in writes if c[0] == "sqs"]
    assert sends == [], f"SendMessage must not happen when window closed at call 9; got {sends}"
    invokes = [c for c in writes if c[0] == "lambda"]
    assert invokes, f"Invoke should have fired before window closed; invokes={invokes}"
    skip_rows = [l for l, s in rows.items() if s == "SKIP"]
    assert skip_rows, f"expected a SKIP row for send-window; rows={rows}"
    assert rows_after == [], (
        f"cleanup must delete the row even when send was skipped; found {rows_after}"
    )


# ===========================================================================
# C1b: manifest flags already on disk at the time of each write
# ===========================================================================


@pytest.mark.skipif(not _REPOS_PRESENT, reason=_SKIP_REASON)
def test_c1b_manifest_row_put_sent_at_putitem(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """At PutItem, manifest has item9_row_put_sent=True and investigation_id on disk.

    The script saves the manifest BEFORE PutItem; this router hook loads the file
    and asserts those fields are already present.
    """
    monkeypatch.setenv("LIVE_TESTS", "1")
    _errs: list[str] = []

    with _open_world() as w:
        w.dlq_alarm.alarm_delay_s = 30.0

        def _check_at_putitem(p: dict[str, Any]) -> None:
            if p.get("TableName") != _INVESTIGATIONS_TABLE:
                return None
            mdir = os.environ.get("FI_QUALITY_MANIFEST_DIR")
            mfile = Path(mdir) / _RUN_ID / "manifest.json"  # type: ignore[arg-type]
            if not mfile.exists():
                _errs.append("manifest file missing at PutItem")
                return None
            m = json.loads(mfile.read_text())
            if not m.get("item9_row_put_sent"):
                _errs.append(f"item9_row_put_sent not True at PutItem: {m!r}")
            if not m.get("item9_investigation_id"):
                _errs.append(f"item9_investigation_id missing at PutItem: {m!r}")
            return None

        w.router.add("dynamodb", "PutItem", _check_at_putitem)

        rc, rows = _call_main(monkeypatch, _argv())

    assert not _errs, "C1b PutItem: " + "; ".join(_errs)
    assert rc == 0, f"expected rc=0; got rc={rc}"


@pytest.mark.skipif(not _REPOS_PRESENT, reason=_SKIP_REASON)
def test_c1b_invoke_sent_at_invoke(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """At Invoke, manifest has item9_invoke_sent=True and message_id on disk."""
    monkeypatch.setenv("LIVE_TESTS", "1")
    _errs: list[str] = []

    with _open_world() as w:
        w.dlq_alarm.alarm_delay_s = 30.0

        # Chain with existing FakeLambda.invoke so the handler still runs
        _orig_invoke = w.router._handlers.get(("lambda", "Invoke"))

        def _check_at_invoke(p: dict[str, Any]) -> Any:
            from scripts.tests.fi_rw_world import world as _rw  # noqa: PLC0415
            fn = (p.get("FunctionName") or "").split(":function:")[-1].split(":")[0]
            if fn == _rw.HEALTH_FN:
                mdir = os.environ.get("FI_QUALITY_MANIFEST_DIR")
                mfile = Path(mdir) / _RUN_ID / "manifest.json"  # type: ignore[arg-type]
                if not mfile.exists():
                    _errs.append("manifest file missing at Invoke")
                else:
                    m = json.loads(mfile.read_text())
                    if not m.get("item9_invoke_sent"):
                        _errs.append(f"item9_invoke_sent not True at Invoke: {m!r}")
                    if not m.get("item9_message_id"):
                        _errs.append(f"item9_message_id missing at Invoke: {m!r}")
                    # S12: invoke_started_at must be saved before Invoke returns so
                    # a kill -9 mid-invoke leaves a --cleanup-only with the W1 trigger.
                    if not m.get("item9_invoke_started_at"):
                        _errs.append(f"item9_invoke_started_at missing at Invoke: {m!r}")
            # Chain to original FakeLambda handler
            return _orig_invoke(p) if _orig_invoke is not None else None

        w.router.add("lambda", "Invoke", _check_at_invoke)

        rc, rows = _call_main(monkeypatch, _argv())

    assert not _errs, "C1b Invoke: " + "; ".join(_errs)
    assert rc == 0, f"expected rc=0; got rc={rc}"


@pytest.mark.skipif(not _REPOS_PRESENT, reason=_SKIP_REASON)
def test_c1b_dlq_sent_at_sendmessage(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """At SendMessage, manifest has item9_dlq_sent=True and dlq_body on disk."""
    monkeypatch.setenv("LIVE_TESTS", "1")
    _errs: list[str] = []

    with _open_world() as w:
        w.dlq_alarm.alarm_delay_s = 30.0

        # Chain with existing _sqs_send_dlq_intercept so DLQ depth tracking still works
        _orig_send = w.router._handlers.get(("sqs", "SendMessage"))

        def _check_at_send(p: dict[str, Any]) -> Any:
            from scripts.tests.fi_loop_world.world import INVESTIGATIONS_DLQ  # noqa: PLC0415
            if INVESTIGATIONS_DLQ in (p.get("QueueUrl") or ""):
                mdir = os.environ.get("FI_QUALITY_MANIFEST_DIR")
                mfile = Path(mdir) / _RUN_ID / "manifest.json"  # type: ignore[arg-type]
                if not mfile.exists():
                    _errs.append("manifest file missing at SendMessage")
                else:
                    m = json.loads(mfile.read_text())
                    if not m.get("item9_dlq_sent"):
                        _errs.append(f"item9_dlq_sent not True at SendMessage: {m!r}")
                    if not m.get("item9_dlq_body"):
                        _errs.append(f"item9_dlq_body missing at SendMessage: {m!r}")
                    # S12: dlq_send_at must be saved so W7 can compute the deadline.
                    if not m.get("item9_dlq_send_at"):
                        _errs.append(f"item9_dlq_send_at missing at SendMessage: {m!r}")
            # Chain to original intercept so DLQ depth tracking still fires
            return _orig_send(p) if _orig_send is not None else None

        w.router.add("sqs", "SendMessage", _check_at_send)

        rc, rows = _call_main(monkeypatch, _argv())

    assert not _errs, "C1b SendMessage: " + "; ".join(_errs)
    assert rc == 0, f"expected rc=0; got rc={rc}"


# ===========================================================================
# W8 cleanup: visibility assert, condition FAIL, residue FAILs, step-no-skip
# ===========================================================================


@pytest.mark.skipif(not _REPOS_PRESENT, reason=_SKIP_REASON)
def test_w8_foreign_message_cmv_zero_called(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Foreign DLQ message's ChangeMessageVisibility(VisibilityTimeout=0) is called.

    The docstring says foreign message visibility is restored; this asserts it.
    """
    monkeypatch.setenv("LIVE_TESTS", "1")

    send_at = dt.datetime.now(dt.timezone.utc)
    _cmv_zero = [False]

    with _open_world() as w:
        sqs = w.internal("sqs")
        dlq_url = sqs.get_queue_url(QueueName=_INVESTIGATIONS_DLQ)["QueueUrl"]
        sqs.send_message(QueueUrl=dlq_url, MessageBody=_DLQ_BODY)
        sqs.send_message(QueueUrl=dlq_url, MessageBody="foreign-w8")

        def _track_cmv(p: dict[str, Any]) -> None:
            if int(p.get("VisibilityTimeout", -1)) == 0:
                _cmv_zero[0] = True
            return None

        w.router.add("sqs", "ChangeMessageVisibility", _track_cmv)

        _write_manifest(tmp_path, extra={
            "item9_dlq_sent": True,
            "item9_dlq_body": _DLQ_BODY,
            "item9_dlq_send_at": send_at.isoformat(),
            "item9_alarm_seen_at": send_at.isoformat(),
        })

        rc, rows = _call_main(monkeypatch, _cleanup_argv())

    assert _cmv_zero[0], (
        "ChangeMessageVisibility(VisibilityTimeout=0) must be called for foreign message"
    )
    assert rows.get("item9-cleanup-dlq-foreign") == "FAIL", (
        f"expected foreign FAIL; rows={rows}"
    )


@pytest.mark.skipif(not _REPOS_PRESENT, reason=_SKIP_REASON)
def test_w8_row_delete_condition_fail_row_present(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """ConditionalCheckFailed with row present (wrong pattern_id) → cleanup-row FAIL.

    invoke_answered=True so W1 re-read loop does not run.
    """
    monkeypatch.setenv("LIVE_TESTS", "1")

    inv_id = str(uuid.uuid4())

    with _open_world() as w:
        # Row exists with a DIFFERENT pattern_id
        w.table(_INVESTIGATIONS_TABLE).put_item(Item={
            "investigation_id": inv_id,
            "pattern_id": "LIVETEST-9-OTHER",   # ← mismatch
            "status": "failed",
            "requested_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "requested_by": "LIVETEST",
            "livetest_run_id": _RUN_ID,
        })

        _write_manifest(tmp_path, extra={
            "item9_row_put_sent": True,
            "item9_invoke_sent": True,
            "item9_invoke_answered": True,  # ← no W1 loop
            "item9_investigation_id": inv_id,
            "item9_dlq_sent": False,
        })

        rc, rows = _call_main(monkeypatch, _cleanup_argv())

    assert rows.get("item9-cleanup-row") == "FAIL", (
        f"expected FAIL (conditional check failed, row present); rows={rows}"
    )
    assert rc == 1, f"expected rc=1; got rc={rc}"


@pytest.mark.skipif(not _REPOS_PRESENT, reason=_SKIP_REASON)
def test_w8_residue_row_present_fail(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Residue finds investigation row still present → item9-residue FAIL."""
    monkeypatch.setenv("LIVE_TESTS", "1")

    inv_id = str(uuid.uuid4())
    send_at = dt.datetime.now(dt.timezone.utc)

    with _open_world() as w:
        w.dlq_alarm.alarm_delay_s = 30.0

        # Make DeleteItem fail so the row stays
        def _fail_delete_item(p: dict[str, Any]) -> None:
            if p.get("TableName") == _INVESTIGATIONS_TABLE:
                from scripts.tests.fi_rw_world.awsfake import AwsError  # noqa: PLC0415
                raise AwsError("InternalServerError", "forced fail", status=500)
            return None

        w.router.add("dynamodb", "DeleteItem", _fail_delete_item)

        w.table(_INVESTIGATIONS_TABLE).put_item(Item={
            "investigation_id": inv_id,
            # S21 (res-row-true): no pattern_id — residue GetItem finds this row (row_absent_c=False)
            # even though the pattern_id Scan won't find it.  The mutant (always row_absent_c=True)
            # misses the GetItem path and residue PASSes → this test FAILS → CAUGHT.
            "status": "failed",
            "requested_at": send_at.isoformat(),
            "requested_by": "LIVETEST",
            "livetest_run_id": _RUN_ID,
        })
        sqs = w.internal("sqs")
        dlq_url = sqs.get_queue_url(QueueName=_INVESTIGATIONS_DLQ)["QueueUrl"]
        sqs.send_message(QueueUrl=dlq_url, MessageBody=_DLQ_BODY)

        _write_manifest(tmp_path, extra={
            "item9_row_put_sent": True,
            "item9_invoke_sent": True,
            "item9_invoke_answered": True,
            "item9_investigation_id": inv_id,
            "item9_dlq_sent": True,
            "item9_dlq_body": _DLQ_BODY,
            "item9_dlq_send_at": send_at.isoformat(),
            "item9_alarm_seen_at": send_at.isoformat(),
        })

        rc, rows = _call_main(monkeypatch, _cleanup_argv())

    assert rows.get("item9-residue") == "FAIL", (
        f"residue should FAIL when row still present; rows={rows}"
    )
    assert rc == 1, f"expected rc=1; got rc={rc}"


@pytest.mark.skipif(not _REPOS_PRESENT, reason=_SKIP_REASON)
def test_w8_residue_dlq_nonzero_fail(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Residue sees DLQ not 0/0/0 → item9-residue FAIL.

    DeleteMessage is overridden to return empty (not actually delete), so the
    DLQ confirmation poll will eventually see vis>0.  The residue step then
    finds DLQ non-zero.
    """
    monkeypatch.setenv("LIVE_TESTS", "1")

    send_at = dt.datetime.now(dt.timezone.utc)

    with _open_world() as w:
        w.dlq_alarm.alarm_delay_s = 30.0

        # DeleteMessage returns empty (moto ignores non-existent handle silently
        # so we need to make it look like the delete succeeded but the message stays)
        # Approach: prevent the world's DLQ-depth tracker from decrementing,
        # so the DLQ confirmation poll always sees vis=1.
        # We do this by overriding GetQueueAttributes for the DLQ to always return 1.
        _attr_calls = [0]

        def _fake_attrs(p: dict[str, Any]) -> dict[str, Any] | None:
            from scripts.tests.fi_loop_world.world import INVESTIGATIONS_DLQ  # noqa: PLC0415
            url = p.get("QueueUrl") or ""
            if INVESTIGATIONS_DLQ not in url:
                return None
            _attr_calls[0] += 1
            # Return non-empty until the 10th call (residue check gets it after that)
            if _attr_calls[0] <= 8:
                return {"Attributes": {
                    "ApproximateNumberOfMessages": "1",
                    "ApproximateNumberOfMessagesNotVisible": "0",
                    "ApproximateNumberOfMessagesDelayed": "0",
                }}
            # On 9th+ call: return 1 for residue FAIL (visible=1)
            return {"Attributes": {
                "ApproximateNumberOfMessages": "1",
                "ApproximateNumberOfMessagesNotVisible": "0",
                "ApproximateNumberOfMessagesDelayed": "0",
            }}

        w.router.add("sqs", "GetQueueAttributes", _fake_attrs)

        sqs = w.internal("sqs")
        dlq_url = sqs.get_queue_url(QueueName=_INVESTIGATIONS_DLQ)["QueueUrl"]
        sqs.send_message(QueueUrl=dlq_url, MessageBody=_DLQ_BODY)

        _write_manifest(tmp_path, extra={
            "item9_dlq_sent": True,
            "item9_dlq_body": _DLQ_BODY,
            "item9_dlq_send_at": send_at.isoformat(),
            "item9_alarm_seen_at": send_at.isoformat(),
        })

        rc, rows = _call_main(monkeypatch, _cleanup_argv())

    assert rows.get("item9-residue") == "FAIL", (
        f"residue should FAIL when DLQ not 0/0/0; rows={rows}"
    )
    assert rc == 1, f"expected rc=1; got rc={rc}"


@pytest.mark.skipif(not _REPOS_PRESENT, reason=_SKIP_REASON)
def test_w8_cleanup_step_raise_does_not_skip_next(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Exception in DLQ step does not skip row step or residue.

    GetQueueUrl raises for the DLQ → DLQ steps FAIL.  Row step and residue
    must still run (each step has its own try/except).
    """
    monkeypatch.setenv("LIVE_TESTS", "1")

    inv_id = str(uuid.uuid4())

    with _open_world() as w:
        w.dlq_alarm.never_fires = True

        w.table(_INVESTIGATIONS_TABLE).put_item(Item={
            "investigation_id": inv_id,
            "pattern_id": _PATTERN_ID,
            "status": "failed",
            "requested_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "requested_by": "LIVETEST",
            "livetest_run_id": _RUN_ID,
        })

        _write_manifest(tmp_path, extra={
            "item9_row_put_sent": True,
            "item9_invoke_sent": True,
            "item9_invoke_answered": True,
            "item9_investigation_id": inv_id,
            "item9_dlq_sent": True,
            "item9_dlq_body": _DLQ_BODY,
            "item9_dlq_send_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "item9_alarm_seen_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        })

        from scripts.tests.fi_loop_world.world import INVESTIGATIONS_DLQ  # noqa: PLC0415

        def _fail_gqu_dlq(p: dict[str, Any]) -> None:
            from scripts.tests.fi_rw_world.awsfake import AwsError  # noqa: PLC0415
            if p.get("QueueName") == INVESTIGATIONS_DLQ:
                raise AwsError("QueueDoesNotExist", "DLQ gone", status=400)
            return None

        w.router.add("sqs", "GetQueueUrl", _fail_gqu_dlq)

        rc, rows = _call_main(monkeypatch, _cleanup_argv())
        row_after = _inv_row(w, inv_id)

    # DLQ step FAIL
    assert rows.get("item9-cleanup-dlq") == "FAIL", (
        f"DLQ step should FAIL; rows={rows}"
    )
    # Row step ran (row deleted)
    assert row_after == {}, "row step must run even when DLQ step fails"
    assert "item9-cleanup-row" in rows, "item9-cleanup-row must be in rows"
    # Residue ran
    assert "item9-residue" in rows, "item9-residue must be in rows"
