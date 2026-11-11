"""Lane-P fix-round-2 tests for ``--part item9`` (fix round 2, lane P).

Covers the run side (``_run_item9``) and pre-checks:

C1c/W2  Action poll requires >= 2 reads when action_lag_s=45 (mutant: w2-action-single-read).
C1d     Run-record window: real record outside the -10 s lower bound (mutant:
        l1-run-record-window-wide) and two real records in the window (mutant:
        w4-ambiguous-off).
W9      Operator topic not found → SKIP item9-pre-subscription, no non-read call.
S13     Any link 1 FAIL → link 3 skipped (no SendMessage); cleanup still runs.
S16/S5  StatusCode != 200 → FAIL item9-link1-invoke (mutant: s5-statuscode-true);
        ESM ARN is a substring match but not exact → FAIL item9-link2-esm (mutant:
        s5-esm-substring); missing maxReceiveCount → FAIL item9-link2-redrive.
S17     ListSubscriptionsByTopic answers confirmed only for self.topic_arn (mutant:
        w3-topic-from-actions: topic taken from AlarmActions[0] which is a wrong topic
        placed first; world returns empty for it → SKIP item9-pre-subscription).

Run:
    nohup .venv/bin/python -m pytest \\
        scripts/tests/test_live_verify_fi_quality_item9_fx2p.py -v --no-header \\
        > /tmp/2026-10-05-fi-fleet-health-stage-d-65fx2p.log 2>&1 &

Requires:
    ~/automotive-data-platform-on-aws (FiLoopWorld)
    ~/connected-mobility-guidance-on-aws (FiLoopWorld)
"""
from __future__ import annotations

import datetime as dt
import importlib.util
import json
import os
import signal
import socket
from pathlib import Path
from typing import Any, Iterator

import pytest

# ---------------------------------------------------------------------------
# Repo presence checks
# ---------------------------------------------------------------------------

_HERE = Path(__file__).resolve().parent
_SCRIPT = _HERE.parent / "live-verify-fi-quality.py"

_ADP_REPO = Path(os.environ.get(
    "FI_LOOP_WORLD_ADP_REPO",
    str(Path.home() / "automotive-data-platform-on-aws"),
))
_CMS_REPO = Path(os.environ.get(
    "FI_LOOP_WORLD_CMS_REPO",
    str(Path.home() / "connected-mobility-guidance-on-aws"),
))
_ADP_HANDLER = (
    _ADP_REPO
    / "platform-foundation" / "source" / "lambda"
    / "digital_thread_writer" / "handler.py"
)
_CMS_INDEX = _CMS_REPO / "services" / "fleet_intelligence" / "index.py"

_REPOS_PRESENT = _ADP_HANDLER.exists() and _CMS_INDEX.exists()
_SKIP_REASON = (
    "test_live_verify_fi_quality_item9_fx2p requires "
    "~/automotive-data-platform-on-aws and ~/connected-mobility-guidance-on-aws"
)

# ---------------------------------------------------------------------------
# Load script module
# ---------------------------------------------------------------------------

_spec = importlib.util.spec_from_file_location("lv_item9_fx2p_mod", _SCRIPT)
lv = importlib.util.module_from_spec(_spec)  # type: ignore[arg-type]
_spec.loader.exec_module(_mod := lv)  # type: ignore[union-attr]
import sys as _sys_fx2p
_sys_fx2p.modules["lv_item9_fx2p_mod"] = lv

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

STAGE = "staging"
REGION = "us-west-2"
ACCOUNT = "123456789012"

_RUN_ID = "fi-quality-20261030T100000-fx2p0001"
_RUN_SUFFIX = _RUN_ID.split("-")[-1]          # "fx2p0001"
_PATTERN_ID = f"LIVETEST-9-{_RUN_SUFFIX}"

_INVESTIGATIONS_TABLE = "vsa-staging-avx-investigations"
_INVESTIGATIONS_DLQ   = "vsa-staging-avx-investigations-dlq"
_INVESTIGATIONS_QUEUE = "vsa-staging-avx-investigations"
_DLQ_ALARM_NAME       = f"vsa-staging-avx-investigations-dlq-depth-{REGION}"
_AGENT_RUNS_TABLE     = "vsa-staging-avx-agent-runs"

# ---------------------------------------------------------------------------
# Autouse fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _restore_signals() -> Iterator[None]:
    old_int  = signal.getsignal(signal.SIGINT)
    old_term = signal.getsignal(signal.SIGTERM)
    old_hup  = signal.getsignal(signal.SIGHUP)
    yield
    signal.signal(signal.SIGINT,  old_int)
    signal.signal(signal.SIGTERM, old_term)
    signal.signal(signal.SIGHUP,  old_hup)


@pytest.fixture(autouse=True)
def _loopback_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    real_connect = socket.socket.connect

    def _connect(sock: socket.socket, address: Any) -> Any:
        host = address[0] if isinstance(address, tuple) else str(address)
        if host not in ("127.0.0.1", "::1", "localhost"):
            raise AssertionError(f"test reached real host {address!r}")
        return real_connect(sock, address)

    monkeypatch.setattr(socket.socket, "connect", _connect)


@pytest.fixture(autouse=True)
def _manifest_dir_guard(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("FI_QUALITY_MANIFEST_DIR", str(tmp_path))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _open_world() -> Any:
    from scripts.tests.fi_loop_world.world import FiLoopWorld  # noqa: PLC0415
    return FiLoopWorld(auto_pump=False, daily_cap=0, restore_live_tests=True)


class _CapResults:
    def __init__(self) -> None:
        self._rows: list[tuple[str, str, str]] = []
        self.run_id: str = _RUN_ID

    def skip(self, label: str, reason: str = "") -> None:
        self._rows.append((label, "SKIP", reason))

    @property
    def rows(self) -> list[tuple[str, str, str]]:
        return list(self._rows)

    @property
    def has_any_fail(self) -> bool:
        return any(s == "FAIL" for _, s, _ in self._rows)

    def summary(self, run_id: str = "") -> None:
        pass


def _call_main(
    monkeypatch: pytest.MonkeyPatch,
    argv: list[str],
) -> tuple[int, dict[str, str]]:
    captured: list[_CapResults] = []
    orig_results = lv.Results

    class _Cap(orig_results):  # type: ignore[misc]
        def __init__(self) -> None:
            super().__init__()
            captured.append(self)  # type: ignore[arg-type]

    monkeypatch.setattr(lv, "Results", _Cap)
    try:
        rc = lv.main(argv)
    except SystemExit as exc:
        rc = exc.code if isinstance(exc.code, int) else 2
    finally:
        monkeypatch.setattr(lv, "Results", orig_results)

    rows = (
        {label: status for label, status, _ in captured[0].rows}
        if captured
        else {}
    )
    return rc, rows


def _argv(
    *,
    allow_writes: bool = True,
    run_id: str = _RUN_ID,
    extra: list[str] | None = None,
) -> list[str]:
    args = ["--stage", STAGE, "--part", "item9", "--run-id", run_id]
    if allow_writes:
        args.append("--allow-writes")
    if extra:
        args.extend(extra)
    return args


def _scan_investigations_for_pattern(w: Any, pattern_id: str) -> list[dict]:
    import boto3  # noqa: PLC0415
    ddb = boto3.resource("dynamodb", region_name=REGION)
    tbl = ddb.Table(_INVESTIGATIONS_TABLE)
    resp = tbl.scan(
        FilterExpression="pattern_id = :p",
        ExpressionAttributeValues={":p": pattern_id},
    )
    return resp.get("Items", [])


def _sns_create_calls(w: Any) -> list[tuple]:
    """Return all CreateTopic calls the SCRIPT made (post-main only)."""
    start = getattr(w, "invocations_before_main", 0)
    return [
        (svc, op, params)
        for svc, op, params in w.router.all_calls[start:]
        if svc == "sns" and op == "CreateTopic"
    ]


def _non_read_calls(w: Any) -> list[tuple]:
    """Return non-read-only AWS calls the script made (post-main start).

    Reads: GetQueueAttributes, GetQueueUrl, DescribeAlarms, DescribeAlarmHistory,
    ListEventSourceMappings, GetFunctionConfiguration, GetCallerIdentity,
    ListSchedules, GetSchedule, ListSubscriptionsByTopic.
    Anything else is a write (or an ambiguous op we treat as write for this test).
    """
    _READ_OPS = frozenset({
        "GetQueueAttributes",
        "GetQueueUrl",
        "DescribeAlarms",
        "DescribeAlarmHistory",
        "ListEventSourceMappings",
        "GetFunctionConfiguration",
        "GetCallerIdentity",
        "ListSchedules",
        "GetSchedule",
        "ListSubscriptionsByTopic",
        "GetItem",
        "Query",
        "Scan",
        "ReceiveMessage",
        "HeadObject",
        "GetObject",
        "ListScheduleGroups",
    })
    start = getattr(w, "invocations_before_main", 0)
    return [
        (svc, op)
        for svc, op, _ in w.router.all_calls[start:]
        if op not in _READ_OPS
    ]


# ===========================================================================
# C1c / W2 — Action poll requires >= 2 reads when action_lag_s > 0
# ===========================================================================


@pytest.mark.skipif(not _REPOS_PRESENT, reason=_SKIP_REASON)
def test_c1c_w2_action_lag_requires_poll(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """action_lag_s=45 hides the Action on the first read; poll must continue.

    With action_lag_s=45 the DlqDepthAlarm stamps the Action item 45 s after the
    StateUpdate.  The script calls describe_alarm_history(HistoryItemType=Action)
    every 30 s (virtual clock, no real sleep).  On the first read (0 s after
    StateUpdate) the Action is in the future and hidden.  After the second sleep(30)
    the world clock is 60 s past the StateUpdate, which is 15 s after the Action
    stamp, so the Action becomes visible.

    Mutant ``w2-action-single-read`` reads Action history exactly once and returns;
    with that mutant the Action is still hidden on the first read and the check FAILs.
    """
    monkeypatch.setenv("LIVE_TESTS", "1")

    with _open_world() as w:
        w.dlq_alarm.alarm_delay_s = 30.0
        w.dlq_alarm.action_lag_s  = 45.0  # Action visible only on 2nd+ poll read

        rc, rows = _call_main(monkeypatch, _argv())

    assert rc == 0, (
        f"expected rc=0 (all PASS) with action_lag_s=45; got rc={rc}. rows={rows}"
    )
    assert rows.get("item9-link3-action") == "PASS", (
        f"expected item9-link3-action=PASS (poll found action after lag); rows={rows}"
    )


# ===========================================================================
# C1d — real records inside / outside the invoke window
# ===========================================================================


@pytest.mark.skipif(not _REPOS_PRESENT, reason=_SKIP_REASON)
def test_c1d_run_record_outside_window(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A real run record just before the window lower bound does not match.

    Pre-plant a record at worldclock.now() − 15 s (just before the -10 s lower
    bound).  The Lambda handler runs normally and writes its own record at
    approximately worldclock.now() (inside the window).  The correct BETWEEN query
    [invoke_start − 10 s, invoke_end + 10 s] returns exactly the handler's record
    → PASS.

    Mutant ``l1-run-record-window-wide`` widens the lower bound to 400 days, which
    includes the pre-planted record → 2 results → FAIL ambiguous.
    """
    monkeypatch.setenv("LIVE_TESTS", "1")

    with _open_world() as w:
        w.dlq_alarm.alarm_delay_s = 30.0

        # Pre-plant a stale record 15 s before the expected invoke time (outside the
        # ±10 s window that the script uses for the run-record BETWEEN query).
        import boto3 as _boto3  # noqa: PLC0415
        _ddb = _boto3.resource("dynamodb", region_name=REGION)
        _runs = _ddb.Table(_AGENT_RUNS_TABLE)
        _T = w.clock.now()
        _outside_ts = (
            (_T - dt.timedelta(seconds=15)).replace(tzinfo=None)
            .isoformat(timespec="milliseconds")
        )
        _runs.put_item(Item={
            "agent_component": "agent-fi-quality#investigation",
            "started_at": _outside_ts,
            "status": "aborted",
            "livetest_run_id": "planted-outside-window",
        })

        rc, rows = _call_main(monkeypatch, _argv())

    # Correct code: BETWEEN [invoke_start - 10 s, invoke_end + 10 s] returns only
    # the handler's record (planted at T_now, inside window) → PASS.
    assert rows.get("item9-link1-run-record") == "PASS", (
        f"expected item9-link1-run-record=PASS (outside record not in window); "
        f"rows={rows}"
    )


@pytest.mark.skipif(not _REPOS_PRESENT, reason=_SKIP_REASON)
def test_c1d_run_record_two_in_window(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Two run records in the invoke window → FAIL item9-link1-run-record (ambiguous).

    Pre-plant one record 5 s before worldclock.now() (inside the ±10 s window).
    The Lambda handler runs normally and writes a second record at approximately
    worldclock.now().  The script finds two records in the window and FAILs with
    the 'ambiguous' message.

    Mutant ``w4-ambiguous-off`` picks rr_items[0] and checks its status instead
    of failing on count > 1; with that mutant the check PASSes when the first
    record has status != 'completed'.
    """
    monkeypatch.setenv("LIVE_TESTS", "1")

    with _open_world() as w:
        w.dlq_alarm.alarm_delay_s = 30.0

        # Pre-plant a record 5 s before the expected invoke time (inside the window).
        import boto3 as _boto3  # noqa: PLC0415
        _ddb = _boto3.resource("dynamodb", region_name=REGION)
        _runs = _ddb.Table(_AGENT_RUNS_TABLE)
        _T = w.clock.now()
        _inside_ts = (
            (_T - dt.timedelta(seconds=5)).replace(tzinfo=None)
            .isoformat(timespec="milliseconds")
        )
        _runs.put_item(Item={
            "agent_component": "agent-fi-quality#investigation",
            "started_at": _inside_ts,
            "status": "aborted",
            "livetest_run_id": "planted-inside-window",
        })

        rc, rows = _call_main(monkeypatch, _argv())

    # Correct code: two records → FAIL (ambiguous; contract §link1).
    assert rows.get("item9-link1-run-record") == "FAIL", (
        f"expected item9-link1-run-record=FAIL (ambiguous: 2 records in window); "
        f"rows={rows}"
    )


# ===========================================================================
# W9 — topic not found → SKIP, no non-read calls
# ===========================================================================


@pytest.mark.skipif(not _REPOS_PRESENT, reason=_SKIP_REASON)
def test_w9_topic_not_found_skip(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Operator topic does not exist → SKIP item9-pre-subscription, no non-read calls.

    Override ListSubscriptionsByTopic for the operator topic to raise a ClientError
    with Code=NotFound (simulating the topic ARN not existing in the account).
    The W9 fix builds the ARN from STS and uses ListSubscriptionsByTopic as the
    existence check; NotFound → SKIP, not an exception propagating out.

    Assert that no write call was made (CreateTopic replaced by ARN-building from STS,
    which is a read-only GetCallerIdentity call; ListSubscriptionsByTopic is read-only).
    """
    monkeypatch.setenv("LIVE_TESTS", "1")

    from botocore.exceptions import ClientError as _CE  # noqa: PLC0415

    with _open_world() as w:
        w.invocations_before_main = len(w.router.all_calls)

        # Override ListSubscriptionsByTopic for the operator topic to raise NotFound.
        # (The S17 fix already restricts the world's override to self.topic_arn; we
        # further force a ClientError so the script's try/except path is exercised.)
        _topic_to_block = w.topic_arn  # operator topic ARN

        def _raise_not_found(p: dict) -> Any:
            if p.get("TopicArn") == _topic_to_block:
                raise _CE(
                    error_response={
                        "Error": {
                            "Code": "NotFound",
                            "Message": "Topic does not exist",
                        }
                    },
                    operation_name="ListSubscriptionsByTopic",
                )
            return {"Subscriptions": []}

        w.router.add("sns", "ListSubscriptionsByTopic", _raise_not_found)

        rc, rows = _call_main(monkeypatch, _argv())
        non_reads = _non_read_calls(w)

    assert rc == 2, (
        f"expected rc=2 (SKIP) when topic not found; got rc={rc}. rows={rows}"
    )
    assert rows.get("item9-pre-subscription") == "SKIP", (
        f"expected item9-pre-subscription=SKIP (topic NotFound); rows={rows}"
    )
    # No write (or mutating) call must have been made — CreateTopic is gone (W9).
    assert non_reads == [], (
        f"expected zero non-read calls on SKIP (topic not found); got {non_reads}"
    )
    # Explicitly confirm CreateTopic was NOT called.
    create_topic_calls = _sns_create_calls(w)
    assert create_topic_calls == [], (
        f"CreateTopic must not be called (W9 fix); got {create_topic_calls}"
    )


# ===========================================================================
# S13 — link 1 FAIL → link 3 skipped, cleanup runs
# ===========================================================================


@pytest.mark.skipif(not _REPOS_PRESENT, reason=_SKIP_REASON)
def test_s13_link1_fail_skips_link3(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A link 1 FAIL → no SendMessage to DLQ; cleanup still runs.

    Override the investigation row GetItem (post-invoke) to return status='queued'
    so item9-link1-row-failed FAILs.  The S13 fix returns early before link 3,
    so no SendMessage is sent.  Cleanup runs (item9_row_put_sent was set) and
    removes the row.
    """
    monkeypatch.setenv("LIVE_TESTS", "1")

    with _open_world() as w:
        w.dlq_alarm.alarm_delay_s = 30.0

        # Capture the investigation_id as the PutItem happens.
        _inv_id_holder: list[str] = [""]

        def _capture_put(p: dict) -> None:
            if p.get("TableName") == _INVESTIGATIONS_TABLE:
                item = p.get("Item") or {}
                _inv_id_holder[0] = item.get("investigation_id", "")
            return None

        w.router.add("dynamodb", "PutItem", _capture_put)

        # Force the row check (GetItem post-invoke) to return status='queued'.
        def _return_queued(p: dict) -> dict | None:
            if (
                p.get("TableName") == _INVESTIGATIONS_TABLE
                and (p.get("Key") or {}).get("investigation_id") == _inv_id_holder[0]
                and _inv_id_holder[0]
            ):
                return {"Item": {
                    "investigation_id": _inv_id_holder[0],
                    "status": "queued",
                    "pattern_id": _PATTERN_ID,
                }}
            return None

        w.router.add("dynamodb", "GetItem", _return_queued)

        rc, rows = _call_main(monkeypatch, _argv())
        rows_after = _scan_investigations_for_pattern(w, _PATTERN_ID)

    assert rc == 1, f"expected rc=1 (FAIL); got rc={rc}"
    assert rows.get("item9-link1-row-failed") == "FAIL", (
        f"expected item9-link1-row-failed=FAIL (row still queued); rows={rows}"
    )
    # S13: link 3 must NOT have run (no SendMessage).
    assert "item9-link3-sent" not in rows, (
        f"expected no link3 rows after link1 FAIL (S13); rows={rows}"
    )
    # Cleanup must still run.
    assert rows_after == [], (
        f"cleanup must remove row even when link 1 FAILs; found {rows_after}"
    )


# ===========================================================================
# S16 / S5 — StatusCode, ESM ARN exactness, missing maxReceiveCount
# ===========================================================================


def test_s16_statuscode_202_fails_invoke(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Invoke answers StatusCode 202 → item9-link1-invoke FAIL (S16 behavioural).

    The router-based fake can return any StatusCode dict; overriding with 202
    (async-invoke code) directly exercises the script's ``status_ok`` check
    rather than testing via source inspection.  The payload is the correct
    batchItemFailures shape so that ``payload_ok`` is True — only the StatusCode
    check causes the FAIL.

    Mutant ``s5-statuscode-unused`` drops the ``status_ok`` term from the combined
    ``status_ok and payload_ok`` check, making it ``payload_ok`` alone.  With a
    correct payload and StatusCode 202, the mutant gives item9-link1-invoke PASS
    instead of FAIL — caught.
    """
    import io  # noqa: PLC0415

    monkeypatch.setenv("LIVE_TESTS", "1")

    with _open_world() as w:
        w.dlq_alarm.alarm_delay_s = 30.0

        def _invoke_202(p: dict) -> dict:
            """Return StatusCode 202 with the expected batchItemFailures payload."""
            import json as _json  # noqa: PLC0415
            # Extract the message_id the script put in the SQS event payload.
            raw = p.get("Payload", b"{}")
            sqs_event = _json.loads(raw if isinstance(raw, (bytes, str)) else raw.read())
            msg_id = (sqs_event.get("Records") or [{}])[0].get("messageId", "")
            payload_bytes = _json.dumps(
                {"batchItemFailures": [{"itemIdentifier": msg_id}]}
            ).encode()
            return {"StatusCode": 202, "Payload": io.BytesIO(payload_bytes)}

        w.router.add("lambda", "Invoke", _invoke_202)

        rc, rows = _call_main(monkeypatch, _argv())

    assert rows.get("item9-link1-invoke") == "FAIL", (
        f"S16: StatusCode 202 must FAIL item9-link1-invoke; rows={rows}"
    )


@pytest.mark.skipif(not _REPOS_PRESENT, reason=_SKIP_REASON)
def test_s16_s5_esm_arn_substring(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """ESM function ARN only contains the function name as a substring → FAIL link2-esm.

    The correct ``_fn_matches`` check requires the FunctionArn to equal the
    function name exactly OR to end with ``:function:{tier2_fn}``.  If the ARN
    contains the name but not at the expected position (e.g. a name prefix was
    added), the check must FAIL.

    Mutant ``s5-esm-substring`` uses ``tier2_fn in fn_arn`` (substring), which
    would pass on the wrong ARN used here.
    """
    monkeypatch.setenv("LIVE_TESTS", "1")

    from scripts.tests.fi_rw_world import world as _rw_w  # noqa: PLC0415
    from scripts.tests.fi_loop_world.world import (  # noqa: PLC0415
        INVESTIGATIONS_ESM_UUID,
        INVESTIGATIONS_QUEUE,
    )

    # Build an ARN where the tier2 function name is a suffix of a longer name.
    # ":function:prefix-<tier2_fn>" contains tier2_fn but does NOT end with
    # ":function:<tier2_fn>", so the exact-match check must FAIL.
    tier2_fn = lv._TIER2_LAMBDA.format(stage=STAGE)
    wrong_fn_arn = (
        f"arn:aws:lambda:{REGION}:{ACCOUNT}:function:prefix-{tier2_fn}"
    )

    with _open_world() as w:
        import boto3 as _boto3  # noqa: PLC0415
        lam = _boto3.client("lambda", region_name=REGION)
        # Remove the existing valid ESM and add a new one with the wrong ARN.
        # moto: delete by UUID then re-create with wrong function.
        try:
            lam.delete_event_source_mapping(UUID=INVESTIGATIONS_ESM_UUID)
        except Exception:  # noqa: BLE001
            pass

        # Create the wrong-ARN ESM by overriding ListEventSourceMappings.
        def _wrong_esm_list(p: dict) -> dict | None:
            fn = p.get("FunctionName", "")
            arn = p.get("EventSourceArn", "")
            if fn or arn:  # intercept any ESM list for this function/queue
                return {
                    "EventSourceMappings": [{
                        "UUID": "wrong-esm-uuid",
                        "State": "Enabled",
                        "FunctionArn": wrong_fn_arn,
                        "EventSourceArn": (
                            f"arn:aws:sqs:{REGION}:{ACCOUNT}:{INVESTIGATIONS_QUEUE}"
                        ),
                        "FunctionResponseTypes": ["ReportBatchItemFailures"],
                    }]
                }
            return None

        w.router.add("lambda", "ListEventSourceMappings", _wrong_esm_list)

        rc, rows = _call_main(monkeypatch, _argv())

    assert rc == 1, (
        f"expected rc=1 (FAIL item9-link2-esm for substring ARN); "
        f"got rc={rc}. rows={rows}"
    )
    assert rows.get("item9-link2-esm") == "FAIL", (
        f"expected item9-link2-esm=FAIL (ARN is substring, not exact); rows={rows}"
    )


@pytest.mark.skipif(not _REPOS_PRESENT, reason=_SKIP_REASON)
def test_s16_s5_missing_maxreceivecount(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Redrive policy has no maxReceiveCount key → FAIL item9-link2-redrive, rc=1.

    When maxReceiveCount is absent, ``redrive.get('maxReceiveCount')`` returns None.
    The S5 check ``isinstance(mrc_val, int) and ...`` is False → FAIL.
    This pins the "missing key" branch of the maxReceiveCount check.
    """
    monkeypatch.setenv("LIVE_TESTS", "1")

    with _open_world() as w:
        import boto3 as _boto3  # noqa: PLC0415
        import json as _json  # noqa: PLC0415
        _sqs_w = _boto3.client("sqs", region_name=REGION)
        _inv_queue_url_w = _sqs_w.get_queue_url(
            QueueName=_INVESTIGATIONS_QUEUE
        )["QueueUrl"]
        _dlq_queue_url_w = _sqs_w.get_queue_url(
            QueueName=_INVESTIGATIONS_DLQ
        )["QueueUrl"]
        _dlq_arn_w = _sqs_w.get_queue_attributes(
            QueueUrl=_dlq_queue_url_w, AttributeNames=["QueueArn"]
        )["Attributes"]["QueueArn"]

        # Override GetQueueAttributes for the investigations queue to return a
        # redrive policy that has deadLetterTargetArn but no maxReceiveCount key.
        def _no_max_rc(p: dict) -> dict | None:
            url = p.get("QueueUrl", "")
            if (
                _INVESTIGATIONS_QUEUE in url
                and _INVESTIGATIONS_DLQ not in url
                and "RedrivePolicy" in p.get("AttributeNames", [])
            ):
                # No maxReceiveCount in the policy object.
                policy = _json.dumps({"deadLetterTargetArn": _dlq_arn_w})
                return {"Attributes": {
                    "RedrivePolicy": policy,
                    "ApproximateNumberOfMessages": "0",
                    "ApproximateNumberOfMessagesNotVisible": "0",
                    "ApproximateNumberOfMessagesDelayed": "0",
                }}
            return None

        w.router.add("sqs", "GetQueueAttributes", _no_max_rc)

        rc, rows = _call_main(monkeypatch, _argv())

    assert rc == 1, (
        f"expected rc=1 (FAIL on missing maxReceiveCount); got rc={rc}. rows={rows}"
    )
    assert rows.get("item9-link2-redrive") == "FAIL", (
        f"expected item9-link2-redrive=FAIL (no maxReceiveCount); rows={rows}"
    )


# ===========================================================================
# S17 — ListSubscriptionsByTopic tied to operator topic (mutant: w3-topic-from-actions)
# ===========================================================================


@pytest.mark.skipif(not _REPOS_PRESENT, reason=_SKIP_REASON)
def test_s17_subscription_check_tied_to_operator_topic(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Operator topic is second in AlarmActions; script must still resolve via STS.

    Add a different (wrong) topic as the FIRST entry in AlarmActions and the real
    operator topic as the SECOND.  The correct W9 code builds the operator topic ARN
    from STS (always the right ARN regardless of order), finds it in AlarmActions,
    and retrieves the confirmed subscription from the operator topic → PASS.

    Mutant ``w3-topic-from-actions`` takes AlarmActions[0] (the wrong topic) and calls
    ListSubscriptionsByTopic on it.  After the S17 fix the world only answers
    'confirmed' for self.topic_arn, so the wrong topic returns empty → SKIP
    item9-pre-subscription.
    """
    monkeypatch.setenv("LIVE_TESTS", "1")

    with _open_world() as w:
        w.dlq_alarm.alarm_delay_s = 30.0

        import boto3 as _boto3  # noqa: PLC0415
        _sns_w = _boto3.client("sns", region_name=REGION)
        _cw_w = _boto3.client("cloudwatch", region_name=REGION)

        # Create a second "wrong" topic and make it AlarmActions[0].
        wrong_topic_arn = _sns_w.create_topic(
            Name="vsa-staging-wrong-second-topic"
        )["TopicArn"]

        # Recreate the alarm with wrong_topic first, correct operator topic second.
        _cw_w.put_metric_alarm(
            AlarmName=_DLQ_ALARM_NAME,
            MetricName="ApproximateNumberOfMessagesVisible",
            Namespace="AWS/SQS",
            Statistic="Maximum",
            Period=300,
            EvaluationPeriods=1,
            Threshold=1.0,
            ComparisonOperator="GreaterThanOrEqualToThreshold",
            TreatMissingData="notBreaching",
            ActionsEnabled=True,
            AlarmActions=[wrong_topic_arn, w.topic_arn],  # wrong first, correct second
        )

        rc, rows = _call_main(monkeypatch, _argv())

    # Correct code (STS-based ARN): finds correct operator topic in AlarmActions[1],
    # calls ListSubscriptionsByTopic on it, gets the confirmed subscription → PASS.
    assert rows.get("item9-pre-subscription") == "PASS", (
        f"expected item9-pre-subscription=PASS (operator topic second in AlarmActions); "
        f"rows={rows}"
    )
