"""Lane-X fix-round-1 tests for ``--part item9`` (fix round 1, lane X).

C1c  link-1 verdicts that must FAIL when the property breaks.
W3   pending-only subscription → SKIP; first AlarmAction is wrong topic → SKIP.
W4   investigations queue not idle → SKIP item9-pre-inv-queue-idle.
W8   (run side of _run_item9): client config pin, DLQ pre-check edge cases,
     alarm ActionsEnabled=False, redrive-wrong-DLQ, second-non-Enabled ESM,
     link-3 pre-send re-reads, Action before StateUpdate, StateUpdate not ALARM.

New test file (lane X only); companion: test_live_verify_fi_quality_item9_fx1y.py
(cleanup-side tests authored by lane Y).

Run:
    nohup .venv/bin/python -m pytest \\
        scripts/tests/test_live_verify_fi_quality_item9_fx1x.py -v --no-header \\
        > /tmp/2026-10-05-fi-fleet-health-stage-d-65fx1x.log 2>&1 &
"""
from __future__ import annotations

import datetime as dt
import importlib.util
import io
import json
import os
import signal
import socket
import uuid
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
    "test_live_verify_fi_quality_item9_fx1x requires "
    "~/automotive-data-platform-on-aws and ~/connected-mobility-guidance-on-aws"
)

# ---------------------------------------------------------------------------
# Load script module
# ---------------------------------------------------------------------------

_spec = importlib.util.spec_from_file_location("lv_item9_fx1x_mod", _SCRIPT)
lv = importlib.util.module_from_spec(_spec)  # type: ignore[arg-type]
_spec.loader.exec_module(_mod := lv)  # type: ignore[union-attr]
import sys as _sys_fx1x
_sys_fx1x.modules["lv_item9_fx1x_mod"] = lv

# ---------------------------------------------------------------------------
# Constants (mirrored from test_live_verify_fi_quality_item9.py)
# ---------------------------------------------------------------------------

STAGE = "staging"
REGION = "us-west-2"
ACCOUNT = "123456789012"

_RUN_ID = "fi-quality-20261030T100000-fx1x0001"
_RUN_SUFFIX = _RUN_ID.split("-")[-1]          # "fx1x0001"
_PATTERN_ID = f"LIVETEST-9-{_RUN_SUFFIX}"

_INVESTIGATIONS_TABLE = "vsa-staging-avx-investigations"
_INVESTIGATIONS_DLQ   = "vsa-staging-avx-investigations-dlq"
_INVESTIGATIONS_QUEUE = "vsa-staging-avx-investigations"
_DLQ_ALARM_NAME       = f"vsa-staging-avx-investigations-dlq-depth-{REGION}"
_AGENT_RUNS_TABLE     = "vsa-staging-avx-agent-runs"

# ---------------------------------------------------------------------------
# Autouse fixtures (same as the base item9 test file)
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


# ===========================================================================
# C1c — link-1 verdicts FAIL when they should
# ===========================================================================


@pytest.mark.skipif(not _REPOS_PRESENT, reason=_SKIP_REASON)
def test_c1c_row_not_failed_payload_matches(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Payload matches expected batchItemFailures but row status is 'queued' → FAIL row-failed.

    The invoke returns the correct payload (PatternNotFound → batchItemFailures=[msgId]),
    but the post-invoke consistent-read is overridden to return status='queued' (simulating
    a runner abort path that doesn't call _set_failed).  The script must FAIL
    item9-link1-row-failed, not silently PASS.
    """
    monkeypatch.setenv("LIVE_TESTS", "1")

    with _open_world() as w:
        w.dlq_alarm.alarm_delay_s = 30.0

        # Intercept PutItem to capture the investigation_id when the row is created.
        _inv_id_holder: list[str] = [""]

        def _capture_put(p: dict) -> None:
            if p.get("TableName") == _INVESTIGATIONS_TABLE:
                item = p.get("Item") or {}
                _inv_id_holder[0] = item.get("investigation_id", "")
            return None  # let moto handle the actual write

        w.router.add("dynamodb", "PutItem", _capture_put)

        # Override GetItem for the investigation row to return status='queued'.
        # Return DynamoDB wire-format types ({"S": "..."}) so boto3's Table
        # resource deserialises cleanly; without them the deserialization raises
        # AttributeError and the except path fires FAIL regardless of the
        # row_status comparison, letting the row_status mutant survive.
        def _return_queued(p: dict) -> dict | None:
            if (
                p.get("TableName") == _INVESTIGATIONS_TABLE
                and (p.get("Key") or {}).get("investigation_id") == _inv_id_holder[0]
                and _inv_id_holder[0]
            ):
                return {"Item": {
                    "investigation_id": _inv_id_holder[0],
                    "status": {"S": "queued"},
                    "pattern_id": {"S": _PATTERN_ID},
                }}
            return None  # fall through to moto for other tables/rows

        w.router.add("dynamodb", "GetItem", _return_queued)

        rc, rows = _call_main(monkeypatch, _argv())

    assert rc == 1, f"expected rc=1 (FAIL); got rc={rc}"
    assert rows.get("item9-link1-row-failed") == "FAIL", (
        f"expected item9-link1-row-failed=FAIL (row still queued); rows={rows}"
    )


@pytest.mark.skipif(not _REPOS_PRESENT, reason=_SKIP_REASON)
def test_c1c_run_record_absent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """No run record in the invoke window → FAIL item9-link1-run-record.

    Override the agent-runs query to return an empty Items list.  With the W4 fix
    (no fallback), 0 results → FAIL, not a widened query picking up a stale record.
    """
    monkeypatch.setenv("LIVE_TESTS", "1")

    with _open_world() as w:
        w.dlq_alarm.alarm_delay_s = 30.0

        def _empty_runs_query(p: dict) -> dict | None:
            if p.get("TableName") == _AGENT_RUNS_TABLE:
                return {"Items": [], "Count": 0, "ScannedCount": 0}
            return None

        w.router.add("dynamodb", "Query", _empty_runs_query)

        rc, rows = _call_main(monkeypatch, _argv())

    assert rc == 1, f"expected rc=1; got rc={rc}"
    assert rows.get("item9-link1-run-record") == "FAIL", (
        f"expected item9-link1-run-record=FAIL (absent); rows={rows}"
    )


@pytest.mark.skipif(not _REPOS_PRESENT, reason=_SKIP_REASON)
def test_c1c_run_record_completed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Run record has status='completed' → FAIL item9-link1-run-record.

    The contract requires status != 'completed' (the runner normally writes
    'aborted' or 'failed' for a PatternNotFound investigation).
    """
    monkeypatch.setenv("LIVE_TESTS", "1")

    with _open_world() as w:
        w.dlq_alarm.alarm_delay_s = 30.0
        _fake_started_at = dt.datetime.utcnow().replace(microsecond=0).isoformat()

        def _completed_run_record(p: dict) -> dict | None:
            if p.get("TableName") == _AGENT_RUNS_TABLE:
                # Use DynamoDB wire-format types ({"S": "..."}) so boto3's Table
                # resource deserialises cleanly; plain strings cause AttributeError
                # in the TypeDeserializer and the except path fires FAIL regardless
                # of the rr_status comparison, letting the rr_status mutant survive.
                return {"Items": [{
                    "agent_component": {"S": "agent-fi-quality#investigation"},
                    "started_at": {"S": _fake_started_at},
                    "status": {"S": "completed"},
                }], "Count": 1, "ScannedCount": 1}
            return None

        w.router.add("dynamodb", "Query", _completed_run_record)

        rc, rows = _call_main(monkeypatch, _argv())

    assert rc == 1, f"expected rc=1; got rc={rc}"
    assert rows.get("item9-link1-run-record") == "FAIL", (
        f"expected item9-link1-run-record=FAIL (status=completed); rows={rows}"
    )


@pytest.mark.skipif(not _REPOS_PRESENT, reason=_SKIP_REASON)
def test_c1c_run_record_outside_window(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The run record's started_at is outside [invoke_start - 10s, invoke_end + 10s].

    The window query returns empty (record is outside bounds).  With W4 (no fallback),
    this means FAIL item9-link1-run-record — not a stale record from a prior run.
    """
    monkeypatch.setenv("LIVE_TESTS", "1")

    with _open_world() as w:
        w.dlq_alarm.alarm_delay_s = 30.0

        # Return empty for the window query; a real query with BETWEEN bounds would
        # miss a record whose started_at is hours away.
        def _outside_window_query(p: dict) -> dict | None:
            if p.get("TableName") == _AGENT_RUNS_TABLE:
                # The script sends KeyConditionExpression with BETWEEN.
                # Return empty list — the record is not in the window.
                return {"Items": [], "Count": 0, "ScannedCount": 0}
            return None

        w.router.add("dynamodb", "Query", _outside_window_query)

        rc, rows = _call_main(monkeypatch, _argv())

    assert rc == 1, f"expected rc=1; got rc={rc}"
    assert rows.get("item9-link1-run-record") == "FAIL", (
        f"expected item9-link1-run-record=FAIL (outside window); rows={rows}"
    )


# ===========================================================================
# W3 — topic resolution and subscription checks
# ===========================================================================


@pytest.mark.skipif(not _REPOS_PRESENT, reason=_SKIP_REASON)
def test_w3_pending_only_subscription_skip(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """All subscriptions are PendingConfirmation → SKIP item9-pre-subscription, rc=2.

    After W3 the script filters out PendingConfirmation entries; a topic with only
    pending subscribers is treated as having no confirmed subscriber → SKIP.
    """
    monkeypatch.setenv("LIVE_TESTS", "1")

    with _open_world() as w:
        # Override ListSubscriptionsByTopic to return only pending subscriptions.
        # A last-write-wins add replaces the world's confirmed-subscription override.
        _pending_arn = "PendingConfirmation"
        _topic_arn = w.topic_arn
        w.router.add(
            "sns", "ListSubscriptionsByTopic",
            lambda p: {
                "Subscriptions": [{
                    "SubscriptionArn": _pending_arn,
                    "TopicArn": _topic_arn,
                    "Protocol": "email",
                    "Endpoint": "operator@example.invalid",
                    "Owner": ACCOUNT,
                }]
            },
        )

        rc, rows = _call_main(monkeypatch, _argv())

    assert rc == 2, f"expected SKIP rc=2; got rc={rc}. rows={rows}"
    assert rows.get("item9-pre-subscription") == "SKIP", (
        f"expected item9-pre-subscription=SKIP (pending-only); rows={rows}"
    )


@pytest.mark.skipif(not _REPOS_PRESENT, reason=_SKIP_REASON)
def test_w3_alarm_action_wrong_topic_skip(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """AlarmActions points to a different topic, not the operator topic → SKIP.

    After W3 the script resolves the operator topic by name and checks it appears
    in AlarmActions.  An alarm pointing at the wrong topic → SKIP item9-pre-alarm-ok.
    """
    monkeypatch.setenv("LIVE_TESTS", "1")

    with _open_world() as w:
        import boto3  # noqa: PLC0415
        # Create a different topic and point the alarm's AlarmActions at it.
        wrong_topic_arn = boto3.client(
            "sns", region_name=REGION
        ).create_topic(Name="vsa-staging-wrong-topic")["TopicArn"]
        cw = boto3.client("cloudwatch", region_name=REGION)
        cw.put_metric_alarm(
            AlarmName=_DLQ_ALARM_NAME,
            MetricName="ApproximateNumberOfMessagesVisible",
            Namespace="AWS/SQS",
            Statistic="Maximum",
            Period=300,
            EvaluationPeriods=1,
            Threshold=1.0,
            ComparisonOperator="GreaterThanOrEqualToThreshold",
            AlarmActions=[wrong_topic_arn],   # ← wrong topic
            ActionsEnabled=True,
        )

        rc, rows = _call_main(monkeypatch, _argv())

    assert rc == 2, f"expected SKIP rc=2; got rc={rc}. rows={rows}"
    assert rows.get("item9-pre-alarm-ok") == "SKIP", (
        f"expected item9-pre-alarm-ok=SKIP (wrong topic in AlarmActions); rows={rows}"
    )


# ===========================================================================
# W4 — investigations queue idle pre-check
# ===========================================================================


@pytest.mark.skipif(not _REPOS_PRESENT, reason=_SKIP_REASON)
def test_w4_inv_queue_not_idle_skip(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Investigations queue has an in-flight message → SKIP item9-pre-inv-queue-idle, rc=2.

    A concurrent investigation being processed from the queue would produce a second
    run record and make the window query ambiguous.  The pre-check gates on queue idle.
    """
    monkeypatch.setenv("LIVE_TESTS", "1")

    with _open_world() as w:
        # Inject a response for GetQueueAttributes on the investigations queue that
        # reports 1 in-flight message, simulating a concurrent investigation.
        _inv_queue_name = "vsa-staging-avx-investigations"

        def _fake_inv_queue_attrs(p: dict) -> dict | None:
            url = p.get("QueueUrl", "")
            # Match the investigations input queue (not the DLQ).
            if (
                _inv_queue_name in url
                and "dlq" not in url
            ):
                return {"Attributes": {
                    "ApproximateNumberOfMessages": "0",
                    "ApproximateNumberOfMessagesNotVisible": "1",  # in-flight
                    "ApproximateNumberOfMessagesDelayed": "0",
                }}
            return None

        w.router.add("sqs", "GetQueueAttributes", _fake_inv_queue_attrs)

        w.invocations_before_main = len(w.router.all_calls)
        rc, rows = _call_main(monkeypatch, _argv())
        writes = [
            (svc, op)
            for svc, op, params in w.router.all_calls[w.invocations_before_main:]
            if op in ("PutItem", "Invoke", "SendMessage")
        ]

    assert rc == 2, f"expected SKIP rc=2; got rc={rc}. rows={rows}"
    assert rows.get("item9-pre-inv-queue-idle") == "SKIP", (
        f"expected item9-pre-inv-queue-idle=SKIP (in-flight msg); rows={rows}"
    )
    assert writes == [], (
        f"expected zero write requests on pre-check SKIP; got {writes}"
    )


# ===========================================================================
# W8 (run side) — invoke client config, DLQ/alarm edge cases, link-2, link-3
# ===========================================================================


@pytest.mark.skipif(not _REPOS_PRESENT, reason=_SKIP_REASON)
def test_w8_invoke_client_config_pinned() -> None:
    """The invoke client is created with read_timeout > 900s, 1 attempt, tcp_keepalive.

    Inspects the _run_item9 source to ensure these safety-critical values are present.
    A change to the config (e.g., read_timeout=300) would let a slow Lambda call time
    out mid-invoke and leak the Lambda execution, the fix for W1.
    """
    import inspect  # noqa: PLC0415

    src = inspect.getsource(lv._run_item9)
    assert "read_timeout=901" in src, (
        "invoke client read_timeout must be > 900 s (Lambda timeout)"
    )
    assert '"total_max_attempts": 1' in src, (
        "invoke client must not retry (total_max_attempts=1)"
    )
    assert "tcp_keepalive=True" in src, (
        "invoke client must use tcp_keepalive=True"
    )


@pytest.mark.skipif(not _REPOS_PRESENT, reason=_SKIP_REASON)
def test_w8_dlq_precheck_inflight_only_skip(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """DLQ has only in-flight messages (NotVisible=1, Visible=0) → SKIP item9-pre-dlq-empty.

    The pre-check requires all three attributes to be 0.  A message invisible
    (being processed by another consumer) still blocks the run.
    """
    monkeypatch.setenv("LIVE_TESTS", "1")

    with _open_world() as w:
        import boto3  # noqa: PLC0415
        sqs = boto3.client("sqs", region_name=REGION)
        dlq_url = sqs.get_queue_url(QueueName=_INVESTIGATIONS_DLQ)["QueueUrl"]
        # Plant a message and receive it to put it in-flight.
        sqs.send_message(QueueUrl=dlq_url, MessageBody="inflight-for-precheck")
        sqs.receive_message(QueueUrl=dlq_url, VisibilityTimeout=900)
        # Now: Visible=0, NotVisible=1, Delayed=0.

        rc, rows = _call_main(monkeypatch, _argv())

    assert rc == 2, f"expected SKIP rc=2; got rc={rc}"
    assert rows.get("item9-pre-dlq-empty") == "SKIP", (
        f"expected item9-pre-dlq-empty=SKIP (in-flight msg); rows={rows}"
    )


@pytest.mark.skipif(not _REPOS_PRESENT, reason=_SKIP_REASON)
def test_w8_dlq_precheck_delayed_only_skip(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """DLQ has only delayed messages → SKIP item9-pre-dlq-empty.

    Override GetQueueAttributes to return Delayed=1 to simulate a delayed message
    (cannot be done with moto reliably without real-time delays).
    """
    monkeypatch.setenv("LIVE_TESTS", "1")

    with _open_world() as w:
        _dlq_name = _INVESTIGATIONS_DLQ

        def _fake_dlq_delayed(p: dict) -> dict | None:
            if _dlq_name in p.get("QueueUrl", "") and "dlq" in p.get("QueueUrl", ""):
                return {"Attributes": {
                    "ApproximateNumberOfMessages": "0",
                    "ApproximateNumberOfMessagesNotVisible": "0",
                    "ApproximateNumberOfMessagesDelayed": "1",  # delayed only
                    "QueueArn": (
                        f"arn:aws:sqs:{REGION}:{ACCOUNT}:{_dlq_name}"
                    ),
                }}
            return None

        w.router.add("sqs", "GetQueueAttributes", _fake_dlq_delayed)

        rc, rows = _call_main(monkeypatch, _argv())

    assert rc == 2, f"expected SKIP rc=2; got rc={rc}"
    assert rows.get("item9-pre-dlq-empty") == "SKIP", (
        f"expected item9-pre-dlq-empty=SKIP (delayed msg); rows={rows}"
    )


@pytest.mark.skipif(not _REPOS_PRESENT, reason=_SKIP_REASON)
def test_w8_alarm_actions_disabled_skip(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Alarm has ActionsEnabled=False → SKIP item9-pre-alarm-ok, rc=2.

    The script checks ActionsEnabled as a pre-condition; with it False the alarm
    would not notify even if it transitioned, so the run can't proceed.
    """
    monkeypatch.setenv("LIVE_TESTS", "1")

    with _open_world() as w:
        import boto3  # noqa: PLC0415
        cw = boto3.client("cloudwatch", region_name=REGION)
        # Disable alarm actions (keep state OK so state check passes).
        cw.disable_alarm_actions(AlarmNames=[_DLQ_ALARM_NAME])

        w.invocations_before_main = len(w.router.all_calls)
        rc, rows = _call_main(monkeypatch, _argv())
        writes = [
            op
            for _, op, _ in w.router.all_calls[w.invocations_before_main:]
            if op in ("PutItem", "Invoke", "SendMessage")
        ]

    assert rc == 2, f"expected SKIP rc=2; got rc={rc}. rows={rows}"
    assert rows.get("item9-pre-alarm-ok") == "SKIP", (
        f"expected item9-pre-alarm-ok=SKIP (ActionsEnabled=False); rows={rows}"
    )
    assert writes == [], (
        f"expected no writes on ActionsEnabled=False SKIP; got {writes}"
    )


@pytest.mark.skipif(not _REPOS_PRESENT, reason=_SKIP_REASON)
def test_w8_redrive_wrong_dlq_fail(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """RedrivePolicy points to a different DLQ → FAIL item9-link2-redrive, rc=1, no write.

    The dead-letter target ARN in the queue's redrive policy must equal the DLQ ARN.
    """
    monkeypatch.setenv("LIVE_TESTS", "1")

    with _open_world() as w:
        from scripts.tests.fi_loop_world.world import INVESTIGATIONS_QUEUE  # noqa: PLC0415

        def _wrong_dlq_redrive(p: dict) -> dict | None:
            url = p.get("QueueUrl", "")
            # Target the main investigations queue (not the DLQ).
            if (
                INVESTIGATIONS_QUEUE in url
                and "dlq" not in url
                and "ApproximateNumberOfMessages" not in str(p.get("AttributeNames", []))
            ):
                wrong_dlq_arn = (
                    f"arn:aws:sqs:{REGION}:{ACCOUNT}:vsa-staging-avx-some-other-dlq"
                )
                policy = json.dumps({
                    "deadLetterTargetArn": wrong_dlq_arn,
                    "maxReceiveCount": 2,
                })
                return {"Attributes": {"RedrivePolicy": policy}}
            return None

        w.router.add("sqs", "GetQueueAttributes", _wrong_dlq_redrive)
        w.invocations_before_main = len(w.router.all_calls)

        rc, rows = _call_main(monkeypatch, _argv())
        writes = [
            op
            for _, op, _ in w.router.all_calls[w.invocations_before_main:]
            if op in ("PutItem", "Invoke", "SendMessage")
        ]

    assert rc == 1, f"expected FAIL rc=1; got rc={rc}. rows={rows}"
    assert rows.get("item9-link2-redrive") == "FAIL", (
        f"expected item9-link2-redrive=FAIL (wrong DLQ ARN); rows={rows}"
    )
    assert writes == [], f"expected no writes on link-2 FAIL; got {writes}"


@pytest.mark.skipif(not _REPOS_PRESENT, reason=_SKIP_REASON)
def test_w8_second_non_enabled_esm_fail(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A second ESM on the queue that is not Enabled → FAIL item9-link2-esm, rc=1.

    test_g3_esm_disabled disables the ONLY mapping so valid_esm fails anyway.
    This test has one valid Enabled ESM (with RBIF) AND a second, non-Enabled one;
    the non-Enabled second mapping is the trigger for FAIL (any non-Enabled mapping
    is disallowed per the contract, even when a valid one also exists).
    """
    monkeypatch.setenv("LIVE_TESTS", "1")

    with _open_world() as w:
        from scripts.tests.fi_loop_world.world import (  # noqa: PLC0415
            INVESTIGATIONS_ESM_UUID, INVESTIGATIONS_QUEUE,
        )
        from scripts.tests.fi_rw_world import world as _rw  # noqa: PLC0415

        def _two_esms(p: dict) -> dict:
            fn = p.get("FunctionName", "")
            arn = p.get("EventSourceArn", "")
            if fn and _rw.HEALTH_FN not in fn:
                return {"EventSourceMappings": []}
            if arn and INVESTIGATIONS_QUEUE not in arn:
                return {"EventSourceMappings": []}
            inv_queue_arn = (
                f"arn:aws:sqs:{REGION}:{ACCOUNT}:{INVESTIGATIONS_QUEUE}"
            )
            tier2_fn_arn = (
                f"arn:aws:lambda:{REGION}:{ACCOUNT}:function:{_rw.HEALTH_FN}"
            )
            return {"EventSourceMappings": [
                {   # valid Enabled ESM with RBIF
                    "UUID": INVESTIGATIONS_ESM_UUID,
                    "EventSourceArn": inv_queue_arn,
                    "FunctionArn": tier2_fn_arn,
                    "State": "Enabled",
                    "FunctionResponseTypes": ["ReportBatchItemFailures"],
                    "BatchSize": 1,
                },
                {   # second mapping on same queue, not Enabled → should FAIL
                    "UUID": "fi-loop-world-esm-uuid-second-not-enabled",
                    "EventSourceArn": inv_queue_arn,
                    "FunctionArn": tier2_fn_arn,
                    "State": "Disabled",  # non-Enabled
                    "FunctionResponseTypes": ["ReportBatchItemFailures"],
                    "BatchSize": 1,
                },
            ]}

        w.router.add("lambda", "ListEventSourceMappings", _two_esms)
        w.invocations_before_main = len(w.router.all_calls)

        rc, rows = _call_main(monkeypatch, _argv())
        writes = [
            op
            for _, op, _ in w.router.all_calls[w.invocations_before_main:]
            if op in ("PutItem", "Invoke", "SendMessage")
        ]

    assert rc == 1, f"expected FAIL rc=1; got rc={rc}. rows={rows}"
    assert rows.get("item9-link2-esm") == "FAIL", (
        f"expected item9-link2-esm=FAIL (second non-Enabled mapping); rows={rows}"
    )
    assert writes == [], f"expected no writes on link-2-esm FAIL; got {writes}"


@pytest.mark.skipif(not _REPOS_PRESENT, reason=_SKIP_REASON)
def test_w8_link3_presend_dlq_nonempty_fail(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """DLQ becomes non-empty between link1 and link3's pre-send re-read → FAIL link3-sent.

    The script re-reads the DLQ before the SendMessage; if it's not 0/0/0 the send
    is aborted and item9-link3-sent FAILs.  Cleanup (row delete) must still run.
    """
    monkeypatch.setenv("LIVE_TESTS", "1")

    with _open_world() as w:
        w.dlq_alarm.alarm_delay_s = 30.0

        # Inject a message into the DLQ after the main pre-checks but before the
        # The DLQ is read twice with all three depth attrs:
        #   call 1 = pre-check 1 (must be 0/0/0 to pass)
        #   call 2 = link-3 pre-send re-read (we return non-empty here)
        _dlq_full_reads = [0]

        def _nonempty_on_second_read(p: dict) -> dict | None:
            url = p.get("QueueUrl", "")
            attrs = p.get("AttributeNames", [])
            if (
                _INVESTIGATIONS_DLQ in url
                and "ApproximateNumberOfMessagesDelayed" in attrs
            ):
                _dlq_full_reads[0] += 1
                if _dlq_full_reads[0] >= 2:
                    # Second call = link-3 pre-send re-read: return 1 visible message.
                    return {"Attributes": {
                        "ApproximateNumberOfMessages": "1",
                        "ApproximateNumberOfMessagesNotVisible": "0",
                        "ApproximateNumberOfMessagesDelayed": "0",
                        "QueueArn": (
                            f"arn:aws:sqs:{REGION}:{ACCOUNT}:{_INVESTIGATIONS_DLQ}"
                        ),
                    }}
            return None  # first call → moto returns 0/0/0, pre-check passes

        w.router.add("sqs", "GetQueueAttributes", _nonempty_on_second_read)

        rc, rows = _call_main(monkeypatch, _argv())
        rows_after = _scan_investigations_for_pattern(w, _PATTERN_ID)

    assert rc == 1, f"expected rc=1 (DLQ dirty before send); got rc={rc}"
    assert rows.get("item9-link3-sent") == "FAIL", (
        f"expected item9-link3-sent=FAIL (DLQ not 0/0/0 before send); rows={rows}"
    )
    # Row must be cleaned up even on FAIL.
    assert rows_after == [], (
        f"cleanup must delete row even when link3 FAILed; found {rows_after}"
    )


@pytest.mark.skipif(not _REPOS_PRESENT, reason=_SKIP_REASON)
def test_w8_link3_presend_alarm_not_ok_fail(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Alarm transitions to ALARM before the link-3 SendMessage re-read → FAIL link3-sent.

    The script re-reads the alarm state before the SendMessage.  If it's not OK the
    send is aborted.
    """
    monkeypatch.setenv("LIVE_TESTS", "1")

    with _open_world() as w:
        w.dlq_alarm.alarm_delay_s = 30.0

        # After the initial pre-checks pass (alarm OK), flip the alarm to ALARM
        # on the DescribeAlarms call that the link-3 re-read makes.
        _describe_calls = [0]

        def _alarm_goes_bad_on_second_describe(p: dict) -> dict | None:
            alarm_names = p.get("AlarmNames", [])
            if _INVESTIGATIONS_DLQ.replace("-dlq", "-depth") in " ".join(alarm_names).lower() \
                    or _DLQ_ALARM_NAME in alarm_names:
                _describe_calls[0] += 1
                if _describe_calls[0] >= 2:
                    # Second DescribeAlarms call (link-3 re-read): return ALARM.
                    return {"MetricAlarms": [{
                        "AlarmName": _DLQ_ALARM_NAME,
                        "StateValue": "ALARM",
                        "ActionsEnabled": True,
                        "AlarmActions": [w.topic_arn],
                    }]}
            return None

        w.router.add("cloudwatch", "DescribeAlarms", _alarm_goes_bad_on_second_describe)

        rc, rows = _call_main(monkeypatch, _argv())
        rows_after = _scan_investigations_for_pattern(w, _PATTERN_ID)

    assert rc == 1, f"expected rc=1 (alarm not OK before send); got rc={rc}"
    assert rows.get("item9-link3-sent") == "FAIL", (
        f"expected item9-link3-sent=FAIL (alarm not OK before send); rows={rows}"
    )
    assert rows_after == [], (
        f"cleanup must delete row even when link3 FAILed; found {rows_after}"
    )


@pytest.mark.skipif(not _REPOS_PRESENT, reason=_SKIP_REASON)
def test_w8_action_before_stateupdate_ignored(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An Action item timestamped before the StateUpdate is ignored → FAIL link3-action.

    The script filters out Action items with a_ts < state_update_ts.  If the only
    Action item in history is before the StateUpdate, item9-link3-action must FAIL.
    """
    monkeypatch.setenv("LIVE_TESTS", "1")

    with _open_world() as w:
        w.dlq_alarm.alarm_delay_s = 30.0

        # Suppress the normal Action item (which is at the same ts as StateUpdate).
        # Instead, inject a fake Action that is timestamped 120 s BEFORE the StateUpdate.
        # We do this by using action_lag_s = -120 effectively: override the alarm's
        # pump() to write the Action earlier.
        _orig_pump = w.dlq_alarm.pump

        def _pump_with_early_action(dlq_depth: int) -> None:
            import datetime as _dt  # noqa: PLC0415
            from scripts.tests.fi_loop_world.dlq_alarm import DlqDepthAlarm  # noqa: PLC0415
            new = "ALARM" if dlq_depth >= 1 else "OK"
            if new == w.dlq_alarm.state:
                return
            old, w.dlq_alarm.state = w.dlq_alarm.state, new
            cw_w = w.dlq_alarm.w.internal("cloudwatch")
            cw_w.set_alarm_state(
                AlarmName=w.dlq_alarm.name,
                StateValue=new,
                StateReason=f"world: depth={dlq_depth} → {new}",
            )
            ts = w.dlq_alarm.w.clock.now()
            # StateUpdate at ts (normal)
            w.dlq_alarm.history.append({
                "AlarmName": w.dlq_alarm.name,
                "AlarmType": "MetricAlarm",
                "Timestamp": ts,
                "HistoryItemType": "StateUpdate",
                "HistorySummary": f"Alarm updated from {old} to {new}",
                "HistoryData": json.dumps({
                    "oldState": {"stateValue": old},
                    "newState": {"stateValue": new},
                }),
            })
            # Action 1 s BEFORE StateUpdate (should be filtered out by a_ts < state_update_ts).
            # 120 s before was outside start_date (send_at − 60 s) so describe_alarm_history
            # never returned the item and the mutant was invisible; 1 s before sits at
            # ts − 1 s ≈ send_at + 29 s which is well inside the polling window.
            action_ts = ts - _dt.timedelta(seconds=1)
            if new == "ALARM" and w.dlq_alarm.topic_arn:
                w.dlq_alarm.history.append({
                    "AlarmName": w.dlq_alarm.name,
                    "AlarmType": "MetricAlarm",
                    "Timestamp": action_ts,
                    "HistoryItemType": "Action",
                    "HistorySummary": "action before stateupdate",
                    "HistoryData": json.dumps({
                        "actionState": "Succeeded",
                        "notificationResource": w.dlq_alarm.topic_arn,
                        "publishedMessage": "{}",
                        "error": None,
                    }),
                })

        w.dlq_alarm.pump = _pump_with_early_action

        rc, rows = _call_main(monkeypatch, _argv())

    assert rc == 1, f"expected rc=1; got rc={rc}"
    assert rows.get("item9-link3-action") == "FAIL", (
        f"expected item9-link3-action=FAIL (Action before StateUpdate); rows={rows}"
    )


@pytest.mark.skipif(not _REPOS_PRESENT, reason=_SKIP_REASON)
def test_w8_stateupdate_not_alarm(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A StateUpdate after the send whose new state is not ALARM → FAIL link3-alarm-state.

    The poll loop must only count a StateUpdate whose historyData.newState.stateValue
    is 'ALARM'.  A transition to 'INSUFFICIENT_DATA' or back to 'OK' does not count.
    """
    monkeypatch.setenv("LIVE_TESTS", "1")

    with _open_world() as w:
        # Suppress normal alarm firing (never_fires).
        w.dlq_alarm.never_fires = True

        # Inject a StateUpdate to INSUFFICIENT_DATA after the send.
        # Override describe_alarm_history to return it on the first poll after send.
        _poll_count = [0]
        _orig_dah = w.dlq_alarm.describe_alarm_history

        def _dah_with_non_alarm_update(p: dict) -> dict:
            result = _orig_dah(p)
            if p.get("HistoryItemType") == "StateUpdate":
                _poll_count[0] += 1
                if _poll_count[0] == 1:
                    # First StateUpdate poll: inject a non-ALARM StateUpdate.
                    import datetime as _dt  # noqa: PLC0415
                    fake_ts = w.clock.now()
                    non_alarm_item = {
                        "AlarmName": w.dlq_alarm.name,
                        "AlarmType": "MetricAlarm",
                        "Timestamp": fake_ts,
                        "HistoryItemType": "StateUpdate",
                        "HistorySummary": "Alarm updated from OK to INSUFFICIENT_DATA",
                        "HistoryData": json.dumps({
                            "oldState": {"stateValue": "OK"},
                            "newState": {"stateValue": "INSUFFICIENT_DATA"},
                        }),
                    }
                    items = result.get("AlarmHistoryItems", [])
                    return {"AlarmHistoryItems": items + [non_alarm_item]}
            return result

        w.dlq_alarm.describe_alarm_history = _dah_with_non_alarm_update

        rc, rows = _call_main(monkeypatch, _argv())

    assert rc == 1, f"expected rc=1; got rc={rc}"
    assert rows.get("item9-link3-alarm-state") == "FAIL", (
        f"expected item9-link3-alarm-state=FAIL (StateUpdate not ALARM); rows={rows}"
    )
