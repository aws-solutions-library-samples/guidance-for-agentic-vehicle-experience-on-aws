"""Lane-Q fix-round-2 tests for --part item9: C1a/W5, C1b/W7, W1, W9, S12,
S14, S16(S4), W8-unk, W8-write-filter, W8-g7-exact.

Findings addressed (cleanup-side, _item9_cleanup, main()-dispatch, item4):
  C1a W5:  SIGINT must arrive inside the cleanup hold; sig-hold-removed caught.
  C1b W7:  alarm_after_delete fake fixed; w7-branch-off / w7-alarm-found-pass caught.
  W1:      late_handler_write now plants recreated row after initial delete.
  W9:      pattern_id Scan in own try; Scan denial no longer skips alarm checks.
  S12:     missing/unparseable dlq_send_at defaults to 25-min wait from now.
  S14:     describe_alarms after W7 "never fired" closes fail-open gap.
  S16/S4:  CMV error text and our message past batch 1; s4-* mutants caught.
  W8 unk:  HeadObject found (200) and 403 both FAIL; unk-no-lake-true caught.
  W8:      broader write filter; g7 write-window-shut asserts rc==2 exactly.

Run:
    nohup .venv/bin/python -m pytest \\
        scripts/tests/test_live_verify_fi_quality_item9_fx2q.py -v --no-header \\
        > /tmp/2026-10-05-fi-fleet-health-stage-d-65fx2q.log 2>&1 &

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
from decimal import Decimal
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

# Also import s2b helpers for item-4 unk tests
from scripts.tests.test_live_verify_fi_quality_after6_s2b import (  # noqa: E402
    _REPOS_PRESENT as _S2B_REPOS_PRESENT,
    _SKIP_REASON as _S2B_SKIP_REASON,
    _open_world as _s2b_open_world,
    _seed_item4_livetest_patterns,
    _fresh_manifest45,
    _build_item4_finding,
    _write_avx_finding,
    _patch_guards,
    _VALID_RUN_ID as _S2B_RUN_ID,
    lv as lv_s2b,
    STAGE as S2B_STAGE,
    ADP_REGION,
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
# Shared helpers
# ---------------------------------------------------------------------------


def _write_manifest(tmp_path: Path, extra: dict | None = None) -> None:
    """Write a minimal item9 cleanup-only manifest for _RUN_ID into tmp_path."""
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


def _router_nonread_ops(w: Any) -> list[tuple[str, str, dict]]:
    """All non-read AWS calls the SCRIPT sent (excluding handler and world spans).

    The world wraps the health Lambda invoke with _span/_handler_start and
    _handler_end sentinels in router.all_calls.  Calls between those sentinels
    are the handler's own operations (Athena, CloudWatch PutMetricData, DDB
    UpdateItem, ADP writer Invoke, etc.) and are excluded from the write filter.
    Similarly, DlqDepthAlarm.create / pump / set_already_alarm bracket their
    CloudWatch calls with _span/_world_start and _world_end sentinels.

    Read-only operations excluded: Get*, List*, Describe*, Scan, ReceiveMessage,
    Converse, AssumeRole, GetCallerIdentity.
    """
    _READ_OPS = frozenset({
        "GetItem", "GetQueueUrl", "GetQueueAttributes", "DescribeAlarms",
        "DescribeAlarmHistory", "ListEventSourceMappings", "GetFunctionConfiguration",
        "GetSchedule", "ListSchedules", "GetCallerIdentity", "ListSubscriptionsByTopic",
        "AssumeRole", "Converse", "Query", "BatchGetItem",
        "Scan", "ReceiveMessage",
    })
    start = getattr(w, "invocations_before_main", 0)
    result: list[tuple[str, str, dict]] = []
    inside_handler = False
    inside_world = False
    for svc, op, params in w.router.all_calls[start:]:
        if svc == "_span":
            if op == "_handler_start":
                inside_handler = True
            elif op == "_handler_end":
                inside_handler = False
            elif op == "_world_start":
                inside_world = True
            elif op == "_world_end":
                inside_world = False
            continue
        if inside_handler or inside_world:
            continue  # exclude calls made by the Lambda handler or world infra
        if op in _READ_OPS:
            continue
        result.append((svc, op, params))
    return result


# ===========================================================================
# C1b W7: alarm_after_delete now works — w7-branch-off / w7-alarm-found-pass
# ===========================================================================


@pytest.mark.skipif(not _REPOS_PRESENT, reason=_SKIP_REASON)
def test_c1b_w7_alarm_fires_after_delete_and_recovers(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """alarm_after_delete=True → ALARM fires after delete → W7 waits for OK → residue PASS.

    W7 pin: mutant w7-deadline-zero (deadline=0 cuts the wait) causes the W7 loop to
    exit before the alarm fires (alarm_delay_s=600 >> loop iterations × 30 s).
    S20 pin: fake-alarm-after-delete-off (alarm_after_delete=False) means the alarm
    never fires → W7 loop exits without seeing ALARM → residue FAIL.

    Uses alarm_delay_s=600 (> 180 s DLQ-confirm bound so the alarm fires AFTER the
    script's DLQ confirmation window), recover_delay_s=60.  send_at is taken inside
    the world so it reflects the world clock, not wall-clock time.
    """
    monkeypatch.setenv("LIVE_TESTS", "1")

    inv_id = str(uuid.uuid4())

    with _open_world() as w:
        # W11: take send_at inside the world — dt.datetime.now() here is the world clock.
        send_at = dt.datetime.now(dt.timezone.utc)

        # Alarm fires AFTER the delete (delayed CloudWatch metric).
        # alarm_delay_s=600: beyond the 180 s DLQ-confirm bound so mutant w7-deadline-zero
        # (deadline=0) exits the W7 loop before elapsed≥600; real code waits long enough.
        w.dlq_alarm.alarm_after_delete = True
        w.dlq_alarm.alarm_delay_s = 600.0
        w.dlq_alarm.recover_delay_s = 60.0

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
        # S20: read history directly (DescribeAlarmHistory would call _auto_transition
        # again and could add another spurious state update).
        history_states = [
            json.loads(h["HistoryData"])["newState"]["stateValue"]
            for h in w.dlq_alarm.history
            if h.get("HistoryItemType") == "StateUpdate"
        ]

    assert rc == 0, f"expected rc=0 (alarm_after_delete PASS); rows={rows}"
    assert rows.get("item9-residue") == "PASS", (
        f"residue should PASS; rows={rows}"
    )
    # S20: alarm fires exactly once — exactly one ALARM then one OK.
    assert history_states == ["ALARM", "OK"], (
        f"alarm_after_delete must fire exactly once then recover; states={history_states}"
    )


@pytest.mark.skipif(not _REPOS_PRESENT, reason=_SKIP_REASON)
def test_c1b_w7_ok_poll_runs_after_alarm_recovers(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """alarm_after_delete: W7 finds the ALARM then waits for OK → residue PASS.

    Pin: mutant w7-deadline-zero (deadline=0) cuts the OK-wait loop short before the
    alarm recovers (recover_delay_s=60 >> one loop iteration × 30 s on deadline=0).

    Uses alarm_delay_s=600 (beyond 180 s DLQ-confirm bound) and recover_delay_s=60.
    send_at inside the with so it reflects the world clock (W11).
    """
    monkeypatch.setenv("LIVE_TESTS", "1")

    inv_id = str(uuid.uuid4())

    with _open_world() as w:
        # W11: take send_at inside the world.
        send_at = dt.datetime.now(dt.timezone.utc)

        w.dlq_alarm.alarm_after_delete = True
        w.dlq_alarm.alarm_delay_s = 600.0
        w.dlq_alarm.recover_delay_s = 60.0

        _write_manifest(tmp_path, extra={
            "item9_row_put_sent": True,
            "item9_invoke_sent": True,
            "item9_invoke_answered": True,
            "item9_investigation_id": inv_id,
            "item9_dlq_sent": True,
            "item9_dlq_body": _DLQ_BODY,
            "item9_dlq_send_at": send_at.isoformat(),
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
        # Read history directly — DescribeAlarmHistory calls _auto_transition again.
        history_states = [
            json.loads(h["HistoryData"])["newState"]["stateValue"]
            for h in w.dlq_alarm.history
            if h.get("HistoryItemType") == "StateUpdate"
        ]

    # ALARM was found by W7, then recovered to OK, so residue PASSes
    assert rows.get("item9-residue") == "PASS", (
        f"residue should PASS (alarm_after_delete fires and recovers); rows={rows}"
    )
    assert rc == 0, f"expected rc=0; rows={rows}"
    assert history_states == ["ALARM", "OK"], (
        f"alarm must fire exactly once then recover; states={history_states}"
    )


# ===========================================================================
# C1a W5: SIGINT inside cleanup hold — sig-hold-removed must fail
# ===========================================================================


@pytest.mark.skipif(not _REPOS_PRESENT, reason=_SKIP_REASON)
def test_c1a_sigint_inside_cleanup_hold_rows_recorded(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """SIGINT on the 4th describe_alarm_history (first inside cleanup) → residue row still recorded.

    Mutant sig-hold-removed replaces:
        with _defer_signals_for_restore():
            _item9_cleanup(_i9_rows, ...)
    with:
        with contextlib.nullcontext():
            _item9_cleanup(_i9_rows, ...)

    This mutant only applies to the RUN path (--part item9), not --cleanup-only.
    So this test uses a full run (_argv()) and signals on the first CLEANUP DAH call.

    Call-count for describe_alarm_history in a full happy-path run:
      #1  Link 3 StateUpdate poll, iteration 1 (alarm not yet fired)
      #2  Link 3 StateUpdate poll, iteration 2 (ALARM found; alarm_delay_s=30)
      #3  Link 3 Action poll (action found)
      #4  Cleanup residue OK poll: describe_alarm_history(MaxRecords=1) ← signal here

    With _defer_signals_for_restore (normal code):
      - SIGINT on call 4 is captured by _record, no exception
      - Cleanup continues to completion: residue PASS
      - Hold exits, deferred SIGINT re-delivered → SystemExit(143)
      - Finally appends all rows including residue

    With nullcontext (sig-hold-removed):
      - SIGINT on call 4 → _exit_once is called → raises SystemExit(143) synchronously
      - Cleanup interrupted before residue row is appended
      - Finally appends partial _i9_rows (no residue)

    Assertion: 'item9-residue' must be in rows (residue appended with hold)
    """
    monkeypatch.setenv("LIVE_TESTS", "1")

    _dah_count = [0]
    _sigint_sent = [False]

    with _open_world() as w:
        w.dlq_alarm.alarm_delay_s = 30.0   # alarm fires after call 1 miss + sleep(30)
        w.dlq_alarm.recover_delay_s = 30.0  # alarm recovers quickly

        _orig_dah = w.dlq_alarm.describe_alarm_history

        def _dah_call_handler_on_4th(p: dict[str, Any]) -> dict[str, Any]:
            """Call SIGINT handler directly on the 4th DAH call (first in cleanup).

            With _defer_signals_for_restore: handler is _record, no exception.
            With nullcontext (mutant): handler is _exit_once, raises SystemExit(143).
            """
            _dah_count[0] += 1
            if _dah_count[0] == 4 and not _sigint_sent[0]:
                _sigint_sent[0] = True
                import signal as _sig  # noqa: PLC0415
                handler = _sig.getsignal(_sig.SIGINT)
                if callable(handler):
                    handler(_sig.SIGINT, None)
            return _orig_dah(p)

        w.dlq_alarm.describe_alarm_history = _dah_call_handler_on_4th

        rc, rows = _call_main(monkeypatch, _argv())

    # S18: assert signal was actually sent (test doesn't pass vacuously if DAH=3)
    assert _sigint_sent[0], "test never sent SIGINT; check DAH call count / alarm_delay_s"
    # S18: SIGINT re-delivered after hold → SystemExit(143) → rc=143
    assert rc == 143, f"expected rc=143 (deferred SIGINT exited process); got rc={rc}"
    # W5: the residue row must be present (hold deferred signal until cleanup finished)
    assert "item9-residue" in rows, (
        f"C1a/W5: item9-residue must be in rows even after deferred SIGINT on call 4; rows={rows}"
    )
    # Cleanup row (step 3) must also be present
    assert "item9-cleanup-row" in rows, (
        f"C1a/W5: item9-cleanup-row must be recorded; rows={rows}"
    )


# ===========================================================================
# W1: "both orders" — Order 2 (late handler write after initial delete)
# ===========================================================================


@pytest.mark.skipif(not _REPOS_PRESENT, reason=_SKIP_REASON)
def test_w1_late_handler_write_row_cleaned_up(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Full run with late_handler_write: W1 loop catches handler's late UpdateItem.

    The fixed late_handler_write plants a recreated row (no pattern_id) on the
    DeleteItem for the investigation key.  The W1 loop re-deletes it via the
    post-deadline delete conditioned on attribute_not_exists(pattern_id).
    Final: cleanup-row PASS, residue PASS, row absent.

    Two DeleteItem calls on the investigation key expected:
      1. Initial conditional delete (pattern_id present) → succeeds, triggers plant
      2. W1 post-deadline delete (attribute_not_exists(pattern_id)) → removes recreated row

    Mutants caught: w1-loop-stops-after-delete (loop exits after first delete → row
    stays → residue FAIL), fake-late-plant-off (plant never fires → row absent before
    W1 loop → cleanup-row PASS but second DeleteItem is missing).
    """
    monkeypatch.setenv("LIVE_TESTS", "1")

    from scripts.tests.fi_loop_world.world import FiLoopWorld  # noqa: PLC0415

    with FiLoopWorld(auto_pump=False, daily_cap=0, late_handler_write=True,
                     restore_live_tests=True) as w:
        w.dlq_alarm.never_fires = True

        rc, rows = _call_main(monkeypatch, _argv())
        rows_after = _scan_investigations_for_pattern(w, _PATTERN_ID)
        # Count DeleteItem calls on the investigations table (any investigation key).
        # Two are expected: initial conditional delete + W1 post-deadline delete.
        delete_calls = [
            c for c in w.router.all_calls
            if c[0] == "dynamodb" and c[1] == "DeleteItem"
            and c[2].get("TableName") == _INVESTIGATIONS_TABLE
        ]

    # Invoke FAIL (late_handler_write raises)
    assert rows.get("item9-link1-invoke") == "FAIL", (
        f"item9-link1-invoke should FAIL (invoke raised); rows={rows}"
    )
    # W1 loop ran and deleted the late-written row
    assert rows_after == [], (
        "W1 loop must have deleted the handler's late-written row; "
        f"found {rows_after}"
    )
    assert rows.get("item9-cleanup-row") == "PASS", (
        f"cleanup-row should PASS after W1 re-delete; rows={rows}"
    )
    # S21: residue must PASS (no row with pattern_id=LIVETEST-9-* left)
    assert rows.get("item9-residue") == "PASS", (
        f"residue should PASS after W1 cleans up the recreated row; rows={rows}"
    )
    # Two DeleteItem calls: initial (pattern_id match) + W1 re-delete (no pattern_id)
    assert len(delete_calls) == 2, (
        f"expected exactly 2 DeleteItem calls on investigation key; got {len(delete_calls)}: "
        f"{delete_calls}"
    )
    assert rc == 1, f"expected rc=1 (FAIL rows from invoke); got rc={rc}"


@pytest.mark.skipif(not _REPOS_PRESENT, reason=_SKIP_REASON)
def test_w1_cleanup_only_recreated_row_deleted(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """--cleanup-only with invoke_started_at 2 h ago and recreated row (no pattern_id) → PASS.

    Simulates the case where:
    - A previous run invoked the Lambda (invoke_sent=True, invoke_started_at set)
    - The handler ran _set_failed and left a row without pattern_id
    - A --cleanup-only run finds the row, triggers the W1 loop, deletes it

    Pin (w1-trigger-invoke-sent): if trigger is invoke_sent (always True when sent)
    instead of invoke_started_at_s, the W1 loop enters even without an
    invoke_started_at — the deadline calculation then raises or uses a fallback,
    changing the cleanup result.
    Pin (w1-post-deadline-delete-off): if the post-deadline delete is removed,
    the recreated row (no pattern_id) stays → cleanup-row FAIL.
    """
    monkeypatch.setenv("LIVE_TESTS", "1")

    inv_id = str(uuid.uuid4())

    with _open_world() as w:
        # invoke_started_at = world now − 2 h: already past the W1 deadline
        invoke_started_at = (
            dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=2)
        ).isoformat()
        w.dlq_alarm.never_fires = True

        # Plant a recreated row without pattern_id (simulates _set_failed landed)
        w.table(_INVESTIGATIONS_TABLE).put_item(Item={
            "investigation_id": inv_id,
            "status": "failed",
            "error_message": "PatternNotFound",
            "requested_by": f"LIVETEST-{_RUN_ID}",
            "livetest_run_id": _RUN_ID,
        })

        _write_manifest(tmp_path, extra={
            "item9_row_put_sent": True,
            "item9_invoke_sent": True,
            "item9_invoke_answered": False,   # W1 trigger condition
            "item9_invoke_started_at": invoke_started_at,
            "item9_investigation_id": inv_id,
            "item9_dlq_sent": False,
        })

        rc, rows = _call_main(monkeypatch, _cleanup_argv())
        row_after = _inv_row(w, inv_id)

    assert row_after == {}, (
        f"W1 must delete the recreated row (no pattern_id); found {row_after}"
    )
    assert rows.get("item9-cleanup-row") == "PASS", (
        f"cleanup-row should PASS after W1 removes recreated row; rows={rows}"
    )
    assert rc == 0, f"expected rc=0; got rc={rc}"





@pytest.mark.skipif(not _REPOS_PRESENT, reason=_SKIP_REASON)
def test_w9_scan_failure_alarm_checks_still_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """DynamoDB Scan permission denied → alarm checks still run; residue FAILs (W10).

    W9: Scan is in its own try after all alarm checks; a denial does not skip
    the alarm waits.
    W10: A failed Scan is fail-closed: row_absent_c is set to False so the
    residue FAILs rather than silently passing without checking the fourth
    contract condition. DescribeAlarms must precede the Scan in router.all_calls.

    Pin: after the fix, reversing the except to ``pass`` (keeping row_absent_c
    True on error) gives residue PASS instead of FAIL — caught.
    """
    monkeypatch.setenv("LIVE_TESTS", "1")

    inv_id = str(uuid.uuid4())

    _scan_calls = [0]

    with _open_world() as w:
        w.dlq_alarm.alarm_delay_s = 30.0

        from scripts.tests.fi_rw_world.awsfake import AwsError  # noqa: PLC0415

        def _fail_scan(p: dict[str, Any]) -> None:
            _scan_calls[0] += 1
            raise AwsError(
                "AccessDeniedException",
                "User is not authorized to perform: dynamodb:Scan",
                status=400,
            )

        w.router.add("dynamodb", "Scan", _fail_scan)

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

        w.table(_INVESTIGATIONS_TABLE).put_item(Item={
            "investigation_id": inv_id,
            "pattern_id": _PATTERN_ID,
            "status": "failed",
            "requested_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "requested_by": f"LIVETEST-{_RUN_ID}",
            "livetest_run_id": _RUN_ID,
        })
        sqs = w.internal("sqs")
        dlq_url = sqs.get_queue_url(QueueName=_INVESTIGATIONS_DLQ)["QueueUrl"]
        sqs.send_message(QueueUrl=dlq_url, MessageBody=_DLQ_BODY)

        w.invocations_before_main = len(w.router.all_calls)
        rc, rows = _call_main(monkeypatch, _cleanup_argv())
        all_ops = [(svc, op) for svc, op, _ in w.router.all_calls[w.invocations_before_main:]]

    # W10: Scan failure is fail-closed → residue FAIL, not PASS
    assert rows.get("item9-residue") == "FAIL", (
        f"W10: residue must FAIL when Scan raises (fail-closed); rows={rows}"
    )
    # Scan was called (W9 puts Scan in own try; it fires after alarm checks)
    assert _scan_calls[0] >= 1, "Scan must have been attempted"
    # DescribeAlarms must precede Scan (alarm checks run before the Scan)
    da_pos = [i for i, (s, o) in enumerate(all_ops)
              if s == "cloudwatch" and o == "DescribeAlarms"]
    scan_pos = [i for i, (s, o) in enumerate(all_ops)
                if s == "dynamodb" and o == "Scan"]
    assert da_pos, f"W10: DescribeAlarms must have been called; all_ops={all_ops}"
    assert min(da_pos) < min(scan_pos), (
        f"W10: DescribeAlarms must precede Scan; "
        f"da_pos={min(da_pos)}, scan_pos={min(scan_pos)}"
    )


# ===========================================================================
# S15: DLQ read error in residue — alarm waits still run, error in detail
# ===========================================================================


@pytest.mark.skipif(not _REPOS_PRESENT, reason=_SKIP_REASON)
def test_s15_dlq_read_error_alarm_waits_still_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """GetQueueUrl fails in residue step → alarm waits still run; FAIL with error text.

    S15 fix wraps the DLQ read in its own try and stores the exception text.
    The alarm waits are not gated on the DLQ read, so DescribeAlarms must still
    be called.  The FAIL detail must include the DLQ read error text (not "0/0/0").

    Mutant ``s15-dlq-read-escapes`` re-raises the GetQueueUrl exception from inside
    the residue try, which would skip the alarm waits entirely — the DescribeAlarms
    assertion catches it.
    """
    monkeypatch.setenv("LIVE_TESTS", "1")

    inv_id = str(uuid.uuid4())

    with _open_world() as w:
        w.dlq_alarm.alarm_delay_s = 30.0

        from scripts.tests.fi_loop_world.world import INVESTIGATIONS_DLQ  # noqa: PLC0415
        from scripts.tests.fi_rw_world.awsfake import AwsError  # noqa: PLC0415

        def _fail_gqu(p: dict[str, Any]) -> None:
            if p.get("QueueName") == INVESTIGATIONS_DLQ:
                raise AwsError("QueueDoesNotExist", "S15 test: no DLQ", status=400)
            return None

        w.router.add("sqs", "GetQueueUrl", _fail_gqu)

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

        w.invocations_before_main = len(w.router.all_calls)
        rc, rows = _call_main(monkeypatch, _cleanup_argv())
        all_ops = [(svc, op) for svc, op, _ in w.router.all_calls[w.invocations_before_main:]]

    # Residue must FAIL (DLQ read error → dlq_r_ok=False)
    assert rows.get("item9-residue") == "FAIL", (
        f"S15: residue must FAIL when GetQueueUrl raises; rows={rows}"
    )
    # DescribeAlarms must have been called (alarm OK poll still ran after DLQ error)
    da_calls = [op for svc, op in all_ops if svc == "cloudwatch" and op == "DescribeAlarms"]
    assert da_calls, (
        f"S15: DescribeAlarms must still be called after DLQ read error; "
        f"all_ops={[(s, o) for s, o in all_ops if s == 'cloudwatch']}"
    )


# ===========================================================================
# S12: missing dlq_send_at triggers W7 wait (fail-closed)
# ===========================================================================


@pytest.mark.skipif(not _REPOS_PRESENT, reason=_SKIP_REASON)
def test_s12_missing_dlq_send_at_w7_fires(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """dlq_sent=True but dlq_send_at_s missing → W7 wait fires from 'now'.

    Before S12 fix: _w7_dlq_send_at stayed None and the W7 wait was silently
    skipped.  After fix: defaults to dt.datetime.now() so the W7 wait always
    runs when dlq_sent=True and alarm_seen_at is not set.
    """
    monkeypatch.setenv("LIVE_TESTS", "1")

    inv_id = str(uuid.uuid4())
    _dah_calls: list[str] = []

    with _open_world() as w:
        w.dlq_alarm.never_fires = True  # alarm never fires

        _orig_dah = w.dlq_alarm.describe_alarm_history

        def _record_dah(p: dict[str, Any]) -> dict[str, Any]:
            _dah_calls.append(p.get("HistoryItemType", "?"))
            return _orig_dah(p)

        w.dlq_alarm.describe_alarm_history = _record_dah

        _write_manifest(tmp_path, extra={
            "item9_row_put_sent": True,
            "item9_invoke_sent": True,
            "item9_invoke_answered": True,
            "item9_investigation_id": inv_id,
            "item9_dlq_sent": True,
            "item9_dlq_body": _DLQ_BODY,
            # dlq_send_at intentionally omitted (S12 scenario)
            # alarm_seen_at NOT set → W7 mode
        })

        w.table(_INVESTIGATIONS_TABLE).put_item(Item={
            "investigation_id": inv_id,
            "pattern_id": _PATTERN_ID,
            "status": "failed",
            "requested_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "requested_by": f"LIVETEST-{_RUN_ID}",
            "livetest_run_id": _RUN_ID,
        })

        rc, rows = _call_main(monkeypatch, _cleanup_argv())

    # W7 wait must have fired (describe_alarm_history called with StateUpdate type)
    w7_calls = [c for c in _dah_calls if c == "StateUpdate"]
    assert w7_calls, (
        f"S12: W7 poll (StateUpdate DAH calls) must fire even when "
        f"dlq_send_at_s is missing; DAH calls={_dah_calls}"
    )
    # residue emitted
    assert "item9-residue" in rows, f"residue row must be present; rows={rows}"


# ===========================================================================
# S14: describe_alarms after W7 "never fired" — fail-closed
# ===========================================================================


@pytest.mark.skipif(not _REPOS_PRESENT, reason=_SKIP_REASON)
def test_s14_alarm_not_ok_after_w7_never_fired_residue_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """W7 polls exhaust without finding ALARM; describe_alarms finds ALARM → residue FAIL.

    Before S14 fix: after "never fired" the code immediately set alarm_r_ok=True
    without verifying the alarm's current state.  An alarm that fired AFTER the
    W7 poll window would be missed.

    After S14 fix: one describe_alarms call after the "never fired" path; if
    alarm state is not OK, falls through to the 15-min OK poll, which times out
    (never_recovers=True), leaving alarm_r_ok=False → residue FAIL.
    """
    monkeypatch.setenv("LIVE_TESTS", "1")

    inv_id = str(uuid.uuid4())

    with _open_world() as w:
        # W11: take send_at inside the world — world clock is 12:00Z.
        send_at = dt.datetime.now(dt.timezone.utc)
        # never_fires=True: W7 polls run but find no StateUpdate ALARM
        w.dlq_alarm.never_fires = True
        w.dlq_alarm.never_recovers = True

        # Pre-set alarm to ALARM state without adding StateUpdate history.
        # set_already_alarm() would add a StateUpdate at send_at (world clock),
        # which is inside the W7 poll window (send_at − 60 s … send_at + 25 min),
        # so W7 finds it immediately and sets _w7_alarm_found=True, bypassing S14.
        # Instead, update moto state + world internal flag only, leaving history empty
        # so W7 exhausts without finding a StateUpdate, then S14 reads ALARM via
        # describe_alarms and falls through to the 15-min OK poll.
        _cw_internal = w.internal("cloudwatch")
        _cw_internal.set_alarm_state(
            AlarmName=w.dlq_alarm.name,
            StateValue="ALARM",
            StateReason="test: pre-ALARM for S14 without history",
        )
        w.dlq_alarm.state = "ALARM"

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

    # S14: alarm still ALARM after W7 "never fired" → residue FAIL
    assert rows.get("item9-residue") == "FAIL", (
        f"S14: residue should FAIL when alarm is not OK after W7 never-fired; rows={rows}"
    )
    assert rc == 1, f"expected rc=1; got rc={rc}"


# ===========================================================================
# S16 (S4 part): CMV error text and our message past first receive batch
# ===========================================================================

@pytest.mark.skipif(not _REPOS_PRESENT, reason=_SKIP_REASON)
def test_s4_cmv_error_text_in_detail_string(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """CMV error text appears in the cleanup-dlq-foreign FAIL detail string.

    Directly calls _item9_cleanup to access the raw detail tuple and verify
    the CMV error text is in the detail, catching s4-cmv-err-dropped.
    """
    monkeypatch.setenv("LIVE_TESTS", "1")

    inv_id = str(uuid.uuid4())

    with _open_world() as w:
        # W11: take send_at inside the world — world clock is 12:00Z.
        send_at = dt.datetime.now(dt.timezone.utc)
        sqs = w.internal("sqs")
        dlq_url = sqs.get_queue_url(QueueName=_INVESTIGATIONS_DLQ)["QueueUrl"]
        sqs.send_message(QueueUrl=dlq_url, MessageBody=_DLQ_BODY)
        sqs.send_message(QueueUrl=dlq_url, MessageBody="foreign-cmv-detail-test")

        from scripts.tests.fi_rw_world.awsfake import AwsError  # noqa: PLC0415

        def _fail_cmv_detail(p: dict[str, Any]) -> None:
            raise AwsError("InvalidParameterValue", "CMVDetailCheck", status=400)

        w.router.add("sqs", "ChangeMessageVisibility", _fail_cmv_detail)

        manifest = {
            "run_id": _RUN_ID,
            "stage": STAGE,
            "part": "item9",
            "item9_row_put_sent": False,
            "item9_invoke_sent": True,
            "item9_invoke_answered": True,
            "item9_investigation_id": inv_id,
            "item9_dlq_sent": True,
            "item9_dlq_body": _DLQ_BODY,
            "item9_dlq_send_at": send_at.isoformat(),
            "item9_alarm_seen_at": send_at.isoformat(),
            "item9_pattern_id": _PATTERN_ID,
        }
        raw_rows: list[tuple[str, str, str]] = []
        lv._item9_cleanup(
            raw_rows,
            manifest=manifest,
            manifest_dir=None,
            stage=STAGE,
        )

    fail_row = next(
        (r for r in raw_rows
         if r[0] == "item9-cleanup-dlq-foreign" and r[1] == "FAIL"),
        None,
    )
    assert fail_row is not None, (
        f"item9-cleanup-dlq-foreign FAIL expected; rows={raw_rows}"
    )
    assert "CMVDetailCheck" in fail_row[2], (
        f"S16/S4: CMV error text must appear in FAIL detail; got {fail_row[2]!r}"
    )


@pytest.mark.skipif(not _REPOS_PRESENT, reason=_SKIP_REASON)
def test_s4_our_message_in_second_batch_found_and_deleted(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Our message is only in the second batch of 10 → still found and deleted.

    Pin: mutant s4-single-batch (for _ in range(1): instead of range(3)) receives
    only the first 10 messages; our message in batch 2 is never received → not
    deleted → DLQ stays non-empty → cleanup-dlq FAIL.
    """
    monkeypatch.setenv("LIVE_TESTS", "1")

    with _open_world() as w:
        # W11: take send_at inside the world — world clock is 12:00Z.
        send_at = dt.datetime.now(dt.timezone.utc)
        sqs = w.internal("sqs")
        dlq_url = sqs.get_queue_url(QueueName=_INVESTIGATIONS_DLQ)["QueueUrl"]

        # Send 10 foreign messages first, then our message
        for i in range(10):
            sqs.send_message(QueueUrl=dlq_url, MessageBody=f"foreign-batch1-{i}")
        sqs.send_message(QueueUrl=dlq_url, MessageBody=_DLQ_BODY)

        _write_manifest(tmp_path, extra={
            "item9_dlq_sent": True,
            "item9_dlq_body": _DLQ_BODY,
            "item9_dlq_send_at": send_at.isoformat(),
            "item9_alarm_seen_at": send_at.isoformat(),
        })

        rc, rows = _call_main(monkeypatch, _cleanup_argv())
        dlq_after = _dlq_depth(w)

    # With s4-single-batch (mutant), our message in position 11 is never received.
    # DLQ after would have 11 messages (10 foreign + 1 ours still there).
    # With 3 batches (fix), our message IS found in batch 2 and deleted.
    # DLQ after has 10 messages (10 foreign restored from VT=900, our message gone).
    assert dlq_after == 10, (
        f"S16/S4: after deleting our message, DLQ should have 10 foreign messages "
        f"(our message in batch 2 was found and deleted); got {dlq_after}"
    )
    # Also assert DLQ step FAILed (foreign messages still in queue → not empty after delete)
    assert rows.get("item9-cleanup-dlq") == "FAIL", (
        f"S16/S4: cleanup-dlq FAILs because 10 foreign messages remain; rows={rows}"
    )


# ===========================================================================
# W8: item 4 unk — HeadObject found (200) and 403 both FAIL
# ===========================================================================


@pytest.mark.skipif(
    not (_REPOS_PRESENT and _S2B_REPOS_PRESENT),
    reason=_SKIP_REASON,
)
def test_w8_unk_lake_object_found_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """HeadObject returns 200 (object found) → item-4-unk-no-lake-object FAIL.

    Pin: mutant unk-no-lake-true changes the check to True, making any response
    PASS.  This test ensures that when the object IS found (no exception), the
    check FAILs.
    """
    _patch_guards(monkeypatch)
    run_id = _S2B_RUN_ID

    with _s2b_open_world() as w:
        specs = _seed_item4_livetest_patterns(w, run_id)
        unk_spec = next(s for s in specs if s["suffix"] == "unk")
        finding = _build_item4_finding(
            pattern_id=unk_spec["pattern_id"],
            fm_id=unk_spec["fm_id"],
            option_id=unk_spec["option_id"],
            remedy_type=None,
            vins=unk_spec["vins"],
            run_id=run_id,
        )
        _write_avx_finding(w, finding)

        # Intercept S3 HeadObject to return "found" (no exception → object exists)
        def _head_object_found(p: dict[str, Any]) -> dict:
            # Return a minimal HeadObject success response
            return {"ContentLength": 123, "ContentType": "application/json"}

        w.router.add("s3", "HeadObject", _head_object_found)

        manifest = _fresh_manifest45(run_id)
        manifest["item4_patterns"].append({
            "pattern_id": unk_spec["pattern_id"],
            "fm_id": unk_spec["fm_id"],
            "vins": unk_spec["vins"],
            "sha256": unk_spec["sha256"],
            "livetest_run_id": run_id,
        })
        manifest["item4_findings"].append({
            "finding_id": finding["finding_id"],
            "finding_kind": finding.get("finding_kind"),
            "pattern_id": unk_spec["pattern_id"],
        })

        res = lv_s2b.Results()
        lv_s2b._item_4(
            res,
            stage=S2B_STAGE,
            run_id=run_id,
            manifest=manifest,
            manifest_dir=str(tmp_path),
            allow_writes=True,
        )

    fail_labels = {r[0] for r in res.rows if r[1] == "FAIL"}
    assert "item-4-unk-no-lake-object" in fail_labels, (
        f"W8/unk: item-4-unk-no-lake-object should FAIL when object exists; rows={res.rows}"
    )


@pytest.mark.skipif(
    not (_REPOS_PRESENT and _S2B_REPOS_PRESENT),
    reason=_SKIP_REASON,
)
def test_w8_unk_lake_403_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """HeadObject returns 403 → item-4-unk-no-lake-object FAIL (not PASS).

    Pin: mutant unk-no-lake-true changes '_code in ("404", "NoSuchKey")' to True,
    making a 403 error PASS incorrectly.  403 means permission denied — we cannot
    confirm the object is absent — so the check must FAIL.
    """
    _patch_guards(monkeypatch)
    run_id = _S2B_RUN_ID

    with _s2b_open_world() as w:
        specs = _seed_item4_livetest_patterns(w, run_id)
        unk_spec = next(s for s in specs if s["suffix"] == "unk")
        finding = _build_item4_finding(
            pattern_id=unk_spec["pattern_id"],
            fm_id=unk_spec["fm_id"],
            option_id=unk_spec["option_id"],
            remedy_type=None,
            vins=unk_spec["vins"],
            run_id=run_id,
        )
        _write_avx_finding(w, finding)

        # Simulate HeadObject returning 403 (AccessDenied)
        from scripts.tests.fi_rw_world.awsfake import AwsError  # noqa: PLC0415

        def _fail_head_403(p: dict[str, Any]) -> None:
            raise AwsError("403", "Access Denied", status=403)

        w.router.add("s3", "HeadObject", _fail_head_403)

        manifest = _fresh_manifest45(run_id)
        manifest["item4_patterns"].append({
            "pattern_id": unk_spec["pattern_id"],
            "fm_id": unk_spec["fm_id"],
            "vins": unk_spec["vins"],
            "sha256": unk_spec["sha256"],
            "livetest_run_id": run_id,
        })
        manifest["item4_findings"].append({
            "finding_id": finding["finding_id"],
            "finding_kind": finding.get("finding_kind"),
            "pattern_id": unk_spec["pattern_id"],
        })

        res = lv_s2b.Results()
        lv_s2b._item_4(
            res,
            stage=S2B_STAGE,
            run_id=run_id,
            manifest=manifest,
            manifest_dir=str(tmp_path),
            allow_writes=True,
        )

    fail_labels = {r[0] for r in res.rows if r[1] == "FAIL"}
    assert "item-4-unk-no-lake-object" in fail_labels, (
        f"W8/unk: 403 from HeadObject must FAIL (not PASS); rows={res.rows}"
    )


# ===========================================================================
# W8: write filter — happy path has exactly the expected writes
# ===========================================================================


@pytest.mark.skipif(not _REPOS_PRESENT, reason=_SKIP_REASON)
def test_w8_write_filter_happy_path_no_unexpected_writes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Happy path: _router_nonread_ops counts exactly the expected script writes.

    Writes expected (5 total for the happy path including cleanup):
      1. dynamodb PutItem (investigation row — link 1)
      2. lambda   Invoke  (health agent — link 1)
      3. sqs      SendMessage (DLQ — link 3)
      4. dynamodb DeleteItem (cleanup row — cleanup step 3)
      5. sqs      DeleteMessage (DLQ message — cleanup step 1)

    """
    monkeypatch.setenv("LIVE_TESTS", "1")

    with _open_world() as w:
        w.dlq_alarm.alarm_delay_s = 30.0
        w.invocations_before_main = len(w.router.all_calls)
        rc, rows = _call_main(monkeypatch, _argv())
        all_writes = _router_nonread_ops(w)

    # Categorise
    ddb_writes = [(s, o, p) for s, o, p in all_writes if s == "dynamodb"]
    lam_writes = [(s, o, p) for s, o, p in all_writes if s == "lambda"]
    sqs_writes = [(s, o, p) for s, o, p in all_writes if s == "sqs"]
    other_writes = [(s, o, p) for s, o, p in all_writes
                    if s not in ("dynamodb", "lambda", "sqs")]

    assert rc == 0, f"happy path should be rc=0; got rc={rc}. rows={rows}"

    # Assert the exact sorted (service, op) list — no SNS, no unexpected writes.
    # Mutants wf-sns-publish-hidden (adds sns.publish) and
    # wf-setalarmstate-hidden (adds cw.set_alarm_state) are both caught because
    # the list would gain an extra element.
    write_ops = sorted((s, o) for s, o, _ in all_writes)
    assert write_ops == [
        ("dynamodb", "DeleteItem"),
        ("dynamodb", "PutItem"),
        ("lambda", "Invoke"),
        ("sqs", "DeleteMessage"),
        ("sqs", "SendMessage"),
    ], f"W8: unexpected write ops; got {write_ops}"

