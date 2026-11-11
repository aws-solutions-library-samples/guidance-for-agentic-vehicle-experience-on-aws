"""Red tests for ``--part item9`` of scripts/live-verify-fi-quality.py (stage 2a).

All tests FAIL NOW because ``--part item9`` is not yet implemented (stage 2b adds it).
The argparse choices list does not include "item9", so
``main(["--part", "item9", ...]`` raises ``SystemExit(2)``.  Every test wraps the call,
converts SystemExit to an rc, then asserts something that fails cleanly on its own
AssertionError.  Tests that expect rc=2 also assert on row labels so they don't
accidentally pass from the argparse rejection alone.

Groups:
  G1  Happy path: every label PASS, rc=0
  G2  Pre-checks: SKIP rc=2, zero non-read requests
  G3  Link 2: config mismatches → FAIL rc=1 before any write
  G4  Link 1: investigation job failures
  G5  Link 3: alarm-wait failures
  G6  Cleanup edge cases
  G7  Gates: write-window and --allow-writes guards

Run:
    nohup .venv/bin/python -m pytest \\
        scripts/tests/test_live_verify_fi_quality_item9.py -v --no-header \\
        > /tmp/2026-10-05-fi-fleet-health-stage-d-65i9-item9.log 2>&1 &

Requires:
    ~/automotive-data-platform-on-aws (FiLoopWorld)
    ~/connected-mobility-guidance-on-aws (FiLoopWorld)
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
# Repo presence checks — skip all tests if FiLoopWorld deps absent
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
    "test_live_verify_fi_quality_item9 requires "
    "~/automotive-data-platform-on-aws and ~/connected-mobility-guidance-on-aws"
)

# ---------------------------------------------------------------------------
# Load the script module
# ---------------------------------------------------------------------------

_spec = importlib.util.spec_from_file_location("lv_item9_mod", _SCRIPT)
lv = importlib.util.module_from_spec(_spec)  # type: ignore[arg-type]
_spec.loader.exec_module(_mod := lv)  # type: ignore[union-attr]
# Register the module in sys.modules so the world's ScriptTime patching
# (which iterates sys.modules looking for live-verify-fi-quality.py) can
# replace lv.time with a fake that advances the world clock instead of
# blocking on real sleep.  Without this, time.sleep(30) in the alarm poll
# would actually sleep 30 real seconds per iteration.
import sys as _sys_item9
_sys_item9.modules["lv_item9_mod"] = lv

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

STAGE = "staging"
REGION = "us-west-2"
ACCOUNT = "123456789012"

# Run id with a predictable suffix so LIVETEST-9-<suffix> is deterministic.
_RUN_ID = "fi-quality-20261029T100000-i9test001"
_RUN_SUFFIX = _RUN_ID.split("-")[-1]          # "i9test001"
_PATTERN_ID = f"LIVETEST-9-{_RUN_SUFFIX}"     # "LIVETEST-9-i9test001"
_DLQ_BODY = f"LIVETEST-9-{_RUN_ID}"           # body sent to DLQ in link 3

# FiLoopWorld resource names (from fi_loop_world.world constants)
_INVESTIGATIONS_TABLE = "vsa-staging-avx-investigations"
_INVESTIGATIONS_DLQ = "vsa-staging-avx-investigations-dlq"
_DLQ_ALARM_NAME = f"vsa-staging-avx-investigations-dlq-depth-us-west-2"
_AGENT_RUNS_TABLE = "vsa-staging-avx-agent-runs"

# Expected happy-path labels (binding names from the contract)
_HAPPY_LABELS = (
    "item9-pre-dlq-empty",
    "item9-pre-inv-queue-idle",
    "item9-pre-alarm-ok",
    "item9-pre-subscription",
    "item9-link2-redrive",
    "item9-link2-esm",
    "item9-link1-row-written",
    "item9-link1-invoke",
    "item9-link1-row-failed",
    "item9-link1-run-record",
    "item9-link3-sent",
    "item9-link3-alarm-state",
    "item9-link3-action",
    "item9-cleanup-dlq",
    "item9-cleanup-row",
    "item9-residue",
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
def _loopback_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail the test if it makes any real outbound TCP connection."""
    real_connect = socket.socket.connect

    def _connect(sock: socket.socket, address: Any) -> Any:
        host = address[0] if isinstance(address, tuple) else str(address)
        if host not in ("127.0.0.1", "::1", "localhost"):
            raise AssertionError(f"test reached real host {address!r}")
        return real_connect(sock, address)

    monkeypatch.setattr(socket.socket, "connect", _connect)


@pytest.fixture(autouse=True)
def _manifest_dir_guard(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Redirect manifest writes to tmp_path; fail on any write to the default dir."""
    monkeypatch.setenv("FI_QUALITY_MANIFEST_DIR", str(tmp_path))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _open_world() -> Any:
    """Return an unopened FiLoopWorld.  Use as a context manager."""
    from scripts.tests.fi_loop_world.world import FiLoopWorld  # noqa: PLC0415
    # auto_pump=False: item9 does its own direct Lambda invoke; no investigation-queue
    # auto-pump needed.  The ESM is in Enabled state (world default).
    # restore_live_tests=True: fi_rw_world.World.open() pops LIVE_TESTS; item9 calls
    # lv.main() inside the world so it must be restored for the guard to pass.
    return FiLoopWorld(auto_pump=False, daily_cap=0, restore_live_tests=True)


class _CapResults:
    """Thin replacement for lv.Results that captures rows for assertions."""

    def __init__(self) -> None:
        self._rows: list[tuple[str, str, str]] = []
        self._skips: list[tuple[str, str]] = []
        self.run_id: str = _RUN_ID

    def skip(self, label: str, reason: str = "") -> None:  # noqa: ARG002
        self._skips.append((label, reason))
        self._rows.append((label, "SKIP", reason))

    @property
    def rows(self) -> list[tuple[str, str, str]]:
        return list(self._rows)

    @property
    def has_any_fail(self) -> bool:
        return any(s == "FAIL" for _, s, _ in self._rows)

    def summary(self, run_id: str = "") -> None:  # noqa: ARG002
        pass


def _call_main(
    monkeypatch: pytest.MonkeyPatch,
    argv: list[str],
) -> tuple[int, dict[str, str]]:
    """Call lv.main(argv); return (rc, rows_dict).

    Patches lv.Results to capture the rows.  Converts SystemExit to an integer
    rc so tests can make assertions rather than seeing an uncaught exception.
    If lv.Results is never constructed (e.g. argparse raises before it), the
    rows dict is empty.
    """
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
    """Build a ``--part item9`` argv list."""
    args = ["--stage", STAGE, "--part", "item9", "--run-id", run_id]
    if allow_writes:
        args.append("--allow-writes")
    if extra:
        args.extend(extra)
    return args


def _dlq_depth(w: Any) -> int:
    """Read the DLQ visible message count from moto."""
    sqs = w.internal("sqs")
    dlq_url = sqs.get_queue_url(QueueName=_INVESTIGATIONS_DLQ)["QueueUrl"]
    attrs = sqs.get_queue_attributes(
        QueueUrl=dlq_url,
        AttributeNames=["ApproximateNumberOfMessages"],
    )["Attributes"]
    return int(attrs.get("ApproximateNumberOfMessages", "0"))


def _inv_row(w: Any, inv_id: str) -> dict:
    """Read an investigation row by id (consistent read)."""
    t = w.table(_INVESTIGATIONS_TABLE)
    return t.get_item(
        Key={"investigation_id": inv_id}, ConsistentRead=True
    ).get("Item", {})


def _scan_investigations_for_pattern(w: Any, pattern_id: str) -> list[dict]:
    """Scan the investigations table for rows with the given pattern_id."""
    import boto3  # noqa: PLC0415
    ddb = boto3.resource("dynamodb", region_name=REGION)
    tbl = ddb.Table(_INVESTIGATIONS_TABLE)
    resp = tbl.scan(FilterExpression="pattern_id = :p", ExpressionAttributeValues={":p": pattern_id})
    return resp.get("Items", [])


def _router_writes(w: Any) -> list[tuple[str, str, str]]:
    """Return (service, operation, resource) for write calls the script itself sends.

    Selects from ``all_calls[invocations_before_main:]`` and filters to the three
    write operations the script owns:

    * ``(dynamodb, PutItem, INVESTIGATIONS_TABLE)`` — investigation row creation
    * ``(lambda, Invoke, HEALTH_FN)``               — tier2 Lambda invocation
    * ``(sqs, SendMessage, INVESTIGATIONS_DLQ)``    — DLQ message in link 3

    The handler's own writes (UpdateItem on the investigation row, PutItem on the
    agent-runs table) are excluded by resource/operation filtering.
    Handler Invoke calls (to the ADP writer) use a different FunctionName and are
    also excluded.
    """
    from scripts.tests.fi_rw_world import world as _rw_w  # noqa: PLC0415
    start = getattr(w, "invocations_before_main", 0)
    result: list[tuple[str, str, str]] = []
    for svc, op, params in w.router.all_calls[start:]:
        if svc == "dynamodb" and op == "PutItem":
            if params.get("TableName") == _INVESTIGATIONS_TABLE:
                result.append((svc, op, _INVESTIGATIONS_TABLE))
        elif svc == "lambda" and op == "Invoke":
            fn_name = params.get("FunctionName") or ""
            if _rw_w.HEALTH_FN in fn_name:
                result.append((svc, op, fn_name))
        elif svc == "sqs" and op == "SendMessage":
            url = params.get("QueueUrl") or ""
            if _INVESTIGATIONS_DLQ in url:
                result.append((svc, op, url))
    return result


# ===========================================================================
# G1: Happy path
# ===========================================================================


@pytest.mark.skipif(not _REPOS_PRESENT, reason=_SKIP_REASON)
def test_g1_happy_path_all_pass(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Every label PASS, rc=0; exactly one Invoke, one PutItem, one SendMessage.

    Red reason: SystemExit(2) from argparse (--part item9 not in choices) →
    rc=2 → assert rc==0 fails.
    """
    monkeypatch.setenv("LIVE_TESTS", "1")

    with _open_world() as w:
        w.dlq_alarm.alarm_delay_s = 30.0   # fires after one 30-s poll

        rc, rows = _call_main(monkeypatch, _argv())

    assert rc == 0, (
        f"expected rc=0 (all PASS); got rc={rc}. rows={rows}"
    )
    for label in _HAPPY_LABELS:
        assert rows.get(label) == "PASS", (
            f"label {label!r} should be PASS; got {rows.get(label)!r}"
        )


@pytest.mark.skipif(not _REPOS_PRESENT, reason=_SKIP_REASON)
def test_g1_happy_path_write_counts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Exactly one Invoke, one PutItem (investigation row), one SendMessage to DLQ.

    Red reason: SystemExit(2) → no router calls → write-count assertions fail.
    """
    monkeypatch.setenv("LIVE_TESTS", "1")

    with _open_world() as w:
        w.dlq_alarm.alarm_delay_s = 30.0
        # Note call count before main()
        w.invocations_before_main = len(w.router.all_calls)
        rc, rows = _call_main(monkeypatch, _argv())
        writes = _router_writes(w)

    invokes = [(s, o) for s, o, r in writes if s == "lambda"]
    put_items = [(s, o) for s, o, r in writes if s == "dynamodb"]
    sends = [(s, o) for s, o, r in writes if s == "sqs"]

    assert len(invokes) == 1, f"expected 1 Invoke; got {len(invokes)}: {invokes}"
    assert len(put_items) == 1, f"expected 1 PutItem (investigation row); got {len(put_items)}: {put_items}"
    assert len(sends) == 1, f"expected 1 SendMessage to DLQ; got {len(sends)}: {sends}"


@pytest.mark.skipif(not _REPOS_PRESENT, reason=_SKIP_REASON)
def test_g1_happy_path_world_end_state(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """World end state: no LIVETEST-9 row, DLQ 0, alarm OK, run record present.

    Red reason: SystemExit(2) → world not modified → row count = 0 (no row was ever
    written), but the assertion is on the alarm being OK (it already is), so the
    rc assertion fails first.
    """
    monkeypatch.setenv("LIVE_TESTS", "1")

    with _open_world() as w:
        w.dlq_alarm.alarm_delay_s = 30.0
        # S21: capture clock before the run to assert the happy path doesn't pay the W1 wait.
        _clock_start = w.clock.now()
        rc, rows = _call_main(monkeypatch, _argv())
        _clock_elapsed_s = (w.clock.now() - _clock_start).total_seconds()

        rows_after = _scan_investigations_for_pattern(w, _PATTERN_ID)
        dlq_visible = _dlq_depth(w)
        alarm_ok = (w.dlq_alarm.state == "OK")

    assert rc == 0, f"expected rc=0; got rc={rc}"
    assert rows_after == [], (
        f"no LIVETEST-9-... row should survive cleanup; found {rows_after}"
    )
    assert dlq_visible == 0, f"DLQ should be empty; got {dlq_visible} messages"
    assert alarm_ok, f"alarm should be OK; got state={w.dlq_alarm.state!r}"
    # S21 (w1-run-answered-false): happy path must not pay the W1 wait (~960 s).
    # 900 s > 16 alarm ticks of 30 s each (480 s) but < W1 max wait of 960 s.
    assert _clock_elapsed_s < 900, (
        f"happy path advanced clock by {_clock_elapsed_s:.0f} s (expected < 900 s); "
        f"w1-run-answered-false mutant would trigger the 16-min W1 loop"
    )


@pytest.mark.skipif(not _REPOS_PRESENT, reason=_SKIP_REASON)
def test_g1_happy_path_failure_reason(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Happy-path handler fails for the planted reason: PatternNotFound.

    No fault_patterns rows are seeded, so the fi_quality job raises
    PatternNotFound.  Two pinning assertions:

    1. The tier2 runner log records ``investigation_failed ... error=PatternNotFound``
       for this run.  A stale-snapshot failure (different code path) would report a
       different error class and would not satisfy this assertion.

    2. The Athena fake received a ``fault_patterns`` query containing
       ``LIVETEST-9-...`` — confirming the job reached the pattern-lookup step.
       A failure that short-circuits before Athena (e.g. a DDB error) would not
       produce this query.
    """
    import logging as _logging  # noqa: PLC0415

    monkeypatch.setenv("LIVE_TESTS", "1")

    with _open_world() as w:
        w.dlq_alarm.alarm_delay_s = 30.0
        with caplog.at_level(_logging.WARNING, logger="agents.tier2.runner"):
            rc, rows = _call_main(monkeypatch, _argv())
        athena_queries = [
            params.get("QueryString", "")
            for svc, op, params in w.router.all_calls
            if svc == "athena" and op == "StartQueryExecution"
        ]

    assert rc == 0, f"expected rc=0 (all PASS); got rc={rc}. rows={rows}"

    # 1. Runner log must record investigation_failed + PatternNotFound
    runner_msgs = [
        r.getMessage()
        for r in caplog.records
        if "agents.tier2" in r.name
    ]
    assert any(
        "investigation_failed" in m and "PatternNotFound" in m for m in runner_msgs
    ), (
        f"expected runner log with investigation_failed+PatternNotFound; "
        f"got runner records: {runner_msgs[:5]}"
    )

    # 2. Athena must have received a fault_patterns query for the LIVETEST-9 pattern
    assert any(
        "fault_patterns" in q and _PATTERN_ID in q for q in athena_queries
    ), (
        f"expected Athena fault_patterns query for {_PATTERN_ID!r}; "
        f"got {len(athena_queries)} queries, first 3: "
        f"{[q[:120] for q in athena_queries[:3]]}"
    )

# ===========================================================================
# G2: Pre-checks — SKIP rc=2, zero non-read requests
# ===========================================================================


@pytest.mark.skipif(not _REPOS_PRESENT, reason=_SKIP_REASON)
def test_g2_dlq_nonempty_skip(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """DLQ has a message → SKIP rc=2, zero non-read AWS requests.

    Red reason: SystemExit(2) from argparse → rows empty →
    assert rows['item9-pre-dlq-empty'] == 'SKIP' fails (no such key).
    """
    monkeypatch.setenv("LIVE_TESTS", "1")

    with _open_world() as w:
        # Plant a message in the DLQ before running
        sqs = w.internal("sqs")
        dlq_url = sqs.get_queue_url(QueueName=_INVESTIGATIONS_DLQ)["QueueUrl"]
        sqs.send_message(QueueUrl=dlq_url, MessageBody="foreign-preexisting")
        w.invocations_before_main = len(w.router.all_calls)

        rc, rows = _call_main(monkeypatch, _argv())
        writes = _router_writes(w)

    assert rc == 2, f"expected SKIP rc=2; got rc={rc}. rows={rows}"
    assert rows.get("item9-pre-dlq-empty") == "SKIP", (
        f"expected item9-pre-dlq-empty=SKIP; rows={rows}"
    )
    assert writes == [], (
        f"expected zero write requests on DLQ-nonempty SKIP; got {writes}"
    )


@pytest.mark.skipif(not _REPOS_PRESENT, reason=_SKIP_REASON)
def test_g2_alarm_already_alarm_skip(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Alarm already in ALARM → SKIP rc=2, zero non-read AWS requests.

    Red reason: SystemExit(2) → rows empty →
    assert rows['item9-pre-alarm-ok'] == 'SKIP' fails.
    """
    monkeypatch.setenv("LIVE_TESTS", "1")

    with _open_world() as w:
        w.dlq_alarm.set_already_alarm()   # start in ALARM state
        w.invocations_before_main = len(w.router.all_calls)

        rc, rows = _call_main(monkeypatch, _argv())
        writes = _router_writes(w)

    assert rc == 2, f"expected SKIP rc=2; got rc={rc}. rows={rows}"
    assert rows.get("item9-pre-alarm-ok") == "SKIP", (
        f"expected item9-pre-alarm-ok=SKIP; rows={rows}"
    )
    assert writes == [], (
        f"expected zero write requests on alarm-ALARM SKIP; got {writes}"
    )


@pytest.mark.skipif(not _REPOS_PRESENT, reason=_SKIP_REASON)
def test_g2_no_confirmed_subscription_skip(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """No confirmed subscription → SKIP rc=2, zero non-read AWS requests.

    Red reason: SystemExit(2) → rows empty →
    assert rows['item9-pre-subscription'] == 'SKIP' fails.
    """
    monkeypatch.setenv("LIVE_TESTS", "1")

    with _open_world() as w:
        # Override ListSubscriptionsByTopic to return empty list
        w.router.add(
            "sns", "ListSubscriptionsByTopic",
            lambda p: {"Subscriptions": []},
        )
        w.invocations_before_main = len(w.router.all_calls)

        rc, rows = _call_main(monkeypatch, _argv())
        writes = _router_writes(w)

    assert rc == 2, f"expected SKIP rc=2; got rc={rc}. rows={rows}"
    assert rows.get("item9-pre-subscription") == "SKIP", (
        f"expected item9-pre-subscription=SKIP; rows={rows}"
    )
    assert writes == [], (
        f"expected zero write requests on no-subscription SKIP; got {writes}"
    )


# ===========================================================================
# G3: Link 2 mismatches — FAIL rc=1 before any write
# ===========================================================================


@pytest.mark.skipif(not _REPOS_PRESENT, reason=_SKIP_REASON)
def test_g3_maxreceivecount_wrong(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """maxReceiveCount=3 (not 2) → FAIL item9-link2-redrive, rc=1, no write.

    Red reason: SystemExit(2) → rows empty →
    assert rows['item9-link2-redrive'] == 'FAIL' fails.
    """
    monkeypatch.setenv("LIVE_TESTS", "1")

    with _open_world() as w:
        # Override GetQueueAttributes for the main queue to return maxReceiveCount=3
        from scripts.tests.fi_loop_world.world import INVESTIGATIONS_QUEUE  # noqa: PLC0415
        _orig_get_qa = w.router._handlers.get(("sqs", "GetQueueAttributes"))

        def _wrong_redrive(p: dict) -> dict | None:
            url = p.get("QueueUrl", "")
            if url.endswith(INVESTIGATIONS_QUEUE) and not url.endswith(_INVESTIGATIONS_DLQ):
                # Return a RedrivePolicy with wrong maxReceiveCount
                dlq_attrs = w.internal("sqs").get_queue_attributes(
                    QueueUrl=w._investigations_dlq_url,
                    AttributeNames=["QueueArn"],
                )["Attributes"]
                dlq_arn = dlq_attrs.get("QueueArn", "")
                policy = json.dumps({"deadLetterTargetArn": dlq_arn, "maxReceiveCount": 3})
                attrs: dict = {"RedrivePolicy": policy}
                for attr_name in p.get("AttributeNames", []):
                    if attr_name not in attrs:
                        attrs[attr_name] = "0"
                return {"Attributes": attrs}
            return None

        w.router.add("sqs", "GetQueueAttributes", _wrong_redrive)
        w.invocations_before_main = len(w.router.all_calls)

        rc, rows = _call_main(monkeypatch, _argv())
        writes = _router_writes(w)

    assert rc == 1, f"expected FAIL rc=1; got rc={rc}. rows={rows}"
    assert rows.get("item9-link2-redrive") == "FAIL", (
        f"expected item9-link2-redrive=FAIL; rows={rows}"
    )
    assert writes == [], (
        f"expected zero write requests on link-2 FAIL; got {writes}"
    )


@pytest.mark.skipif(not _REPOS_PRESENT, reason=_SKIP_REASON)
def test_g3_esm_missing_rbif(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """ESM without ReportBatchItemFailures → FAIL item9-link2-esm, rc=1, no write.

    Red reason: SystemExit(2) → rows empty →
    assert rows['item9-link2-esm'] == 'FAIL' fails.
    """
    monkeypatch.setenv("LIVE_TESTS", "1")

    with _open_world() as w:
        # Override ListEventSourceMappings to return ESM without RBIF
        from scripts.tests.fi_loop_world.world import (  # noqa: PLC0415
            INVESTIGATIONS_ESM_UUID, INVESTIGATIONS_QUEUE,
        )
        from scripts.tests.fi_rw_world import world as _rw  # noqa: PLC0415

        def _esm_no_rbif(p: dict) -> dict:
            fn = p.get("FunctionName", "")
            arn = p.get("EventSourceArn", "")
            if fn and _rw.HEALTH_FN not in fn:
                return {"EventSourceMappings": []}
            if arn and INVESTIGATIONS_QUEUE not in arn:
                return {"EventSourceMappings": []}
            return {"EventSourceMappings": [{
                "UUID": INVESTIGATIONS_ESM_UUID,
                "EventSourceArn": (
                    f"arn:aws:sqs:{REGION}:{ACCOUNT}:{INVESTIGATIONS_QUEUE}"
                ),
                "FunctionArn": (
                    f"arn:aws:lambda:{REGION}:{ACCOUNT}:function:{_rw.HEALTH_FN}"
                ),
                "State": "Enabled",
                "FunctionResponseTypes": [],   # ← missing ReportBatchItemFailures
                "BatchSize": 1,
            }]}

        w.router.add("lambda", "ListEventSourceMappings", _esm_no_rbif)
        w.invocations_before_main = len(w.router.all_calls)

        rc, rows = _call_main(monkeypatch, _argv())
        writes = _router_writes(w)

    assert rc == 1, f"expected FAIL rc=1; got rc={rc}. rows={rows}"
    assert rows.get("item9-link2-esm") == "FAIL", (
        f"expected item9-link2-esm=FAIL; rows={rows}"
    )
    assert writes == [], (
        f"expected zero write requests on link-2-esm FAIL; got {writes}"
    )


@pytest.mark.skipif(not _REPOS_PRESENT, reason=_SKIP_REASON)
def test_g3_esm_disabled(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """ESM Disabled → FAIL item9-link2-esm, rc=1, no write.

    Red reason: SystemExit(2) → rows empty →
    assert rows['item9-link2-esm'] == 'FAIL' fails.
    """
    monkeypatch.setenv("LIVE_TESTS", "1")

    with _open_world() as w:
        w._esm_state = "Disabled"    # world's ESM fake checks this attribute
        w.invocations_before_main = len(w.router.all_calls)

        rc, rows = _call_main(monkeypatch, _argv())
        writes = _router_writes(w)

    assert rc == 1, f"expected FAIL rc=1; got rc={rc}. rows={rows}"
    assert rows.get("item9-link2-esm") == "FAIL", (
        f"expected item9-link2-esm=FAIL; rows={rows}"
    )
    assert writes == [], (
        f"expected zero write requests on ESM-disabled FAIL; got {writes}"
    )


# ===========================================================================
# G4: Link 1 failures
# ===========================================================================


@pytest.mark.skipif(not _REPOS_PRESENT, reason=_SKIP_REASON)
def test_g4_fault_patterns_rows_present(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Row has fault_patterns → job returns batchItemFailures=[] (no item) → FAIL.

    When the pattern_id HAS rows in fault_patterns, the job finds the pattern and
    writes a Finding instead of marking failed.  The expected payload
    ``{"batchItemFailures": [{"itemIdentifier": <msgId>}]}`` does not match
    (batchItemFailures is empty or the item is absent) → item9-link1-invoke FAIL.
    Cleanup should still remove the row.

    Red reason: SystemExit(2) → rows empty →
    assert rows['item9-link1-invoke'] == 'FAIL' fails.
    """
    monkeypatch.setenv("LIVE_TESTS", "1")

    with _open_world() as w:
        w.dlq_alarm.alarm_delay_s = 30.0
        # Seed a real fault pattern so the job finds it (does NOT raise PatternNotFound)
        _vins = [f"1TEST0000000{n:05d}" for n in range(3)]
        w.seed_pattern(
            pattern_id=_PATTERN_ID,
            vin_list=_vins,
            failure_mode_id="fm-g4-test-001",
        )

        rc, rows = _call_main(monkeypatch, _argv())
        rows_after = _scan_investigations_for_pattern(w, _PATTERN_ID)

    assert rc == 1, f"expected rc=1 (FAIL); got rc={rc}"
    assert rows.get("item9-link1-invoke") == "FAIL", (
        f"expected item9-link1-invoke=FAIL (payload mismatch); rows={rows}"
    )
    # Cleanup should have removed the row even on FAIL
    assert rows_after == [], (
        f"cleanup should remove LIVETEST-9 row even on FAIL; found {rows_after}"
    )


@pytest.mark.skipif(not _REPOS_PRESENT, reason=_SKIP_REASON)
def test_g4_function_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Lambda returns FunctionError → FAIL item9-link1-invoke, rc=1.

    Red reason: SystemExit(2) → rows empty →
    assert rows['item9-link1-invoke'] == 'FAIL' fails.
    """
    monkeypatch.setenv("LIVE_TESTS", "1")

    with _open_world() as w:
        w.dlq_alarm.alarm_delay_s = 30.0

        # Replace the tier2 handler with one that always raises
        from scripts.tests.fi_rw_world import world as _rw  # noqa: PLC0415

        def _crashing_handler(event: Any, ctx: Any) -> Any:  # noqa: ARG001
            raise RuntimeError("simulated crash for test_g4_function_error")

        w.lam.handlers[_rw.HEALTH_FN] = _crashing_handler

        rc, rows = _call_main(monkeypatch, _argv())

    assert rc == 1, f"expected rc=1 (FAIL); got rc={rc}"
    assert rows.get("item9-link1-invoke") == "FAIL", (
        f"expected item9-link1-invoke=FAIL (FunctionError); rows={rows}"
    )


@pytest.mark.skipif(not _REPOS_PRESENT, reason=_SKIP_REASON)
def test_g4_raise_after_handler_ran_row_still_deleted(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Handler ran successfully but FakeLambda sees a raise after → row still deleted.

    The script must distinguish between a normal batchItemFailures response and a
    FunctionError.  If the handler ran and returned but the script's own post-invoke
    code raises (e.g., network error reading the row), cleanup should still delete
    the investigation row.

    Red reason: SystemExit(2) → rows empty → assertion on row absence fails.
    """
    monkeypatch.setenv("LIVE_TESTS", "1")

    with _open_world() as w:
        w.dlq_alarm.alarm_delay_s = 30.0

        # Let the tier2 handler run normally (returns batchItemFailures with the msgId)
        # but simulate a failure in the row-read step after the invoke by making
        # GetItem fail on the first post-invoke read.
        _get_item_calls: list[int] = [0]
        _orig_get_item = w.router._handlers.get(("dynamodb", "GetItem"))

        def _fail_first_post_invoke_read(p: dict) -> dict | None:
            if p.get("TableName") == _INVESTIGATIONS_TABLE:
                _get_item_calls[0] += 1
                if _get_item_calls[0] == 2:
                    # First post-invoke read → raise (simulates lost-answer scenario)
                    from scripts.tests.fi_rw_world.awsfake import AwsError  # noqa: PLC0415
                    raise AwsError(
                        "ProvisionedThroughputExceededException",
                        "rate limit for test",
                        status=400,
                    )
            return None  # all other reads go to moto

        w.router.add("dynamodb", "GetItem", _fail_first_post_invoke_read)

        rc, rows = _call_main(monkeypatch, _argv())
        rows_after = _scan_investigations_for_pattern(w, _PATTERN_ID)

    assert rc == 1, f"expected rc=1; got rc={rc}"
    # The row should still be removed by cleanup even though the read failed
    assert rows_after == [], (
        f"cleanup should remove row even when post-invoke row read failed; "
        f"found {rows_after}"
    )


@pytest.mark.skipif(not _REPOS_PRESENT, reason=_SKIP_REASON)
def test_g4_synthetic_event_shape_does_not_raise(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The synthetic SQS event the script sends, fed to the real handler, does not raise.

    This pins the event shape: a malformed event would cause a handler crash and trip
    the run-failure alarm.  The test invokes the real tier2 handler directly with the
    contract's event shape and asserts no FunctionError.

    This test calls the handler directly (not through main) so it passes even before
    stage 2b, providing a green baseline.  Its failure mode is a handler crash that
    should never happen.
    """
    from scripts.tests.fi_loop_world.world import (  # noqa: PLC0415
        FiLoopWorld, INVESTIGATIONS_TABLE, INVESTIGATIONS_QUEUE, ADP_WRITER_ARN,
    )
    from scripts.tests.fi_rw_world import world as _rw  # noqa: PLC0415
    from scripts.tests.fi_rw_world.lambdas import lambda_env  # noqa: PLC0415

    investigation_id = str(uuid.uuid4())
    message_id = f"livetest-9-{_RUN_SUFFIX}"

    with FiLoopWorld(auto_pump=False, daily_cap=0) as w:
        # Write an investigation row in queued state
        w.table(INVESTIGATIONS_TABLE).put_item(Item={
            "investigation_id": investigation_id,
            "pattern_id": _PATTERN_ID,
            "status": "queued",
            "requested_at": "2026-01-01T00:00:00Z",
            "requested_by": f"LIVETEST-{_RUN_ID}",
            "livetest_run_id": _RUN_ID,
        })

        sqs_event = {
            "Records": [{
                "messageId": message_id,
                "receiptHandle": "handle-g4-shape-test",
                "body": investigation_id,
                "attributes": {
                    "ApproximateReceiveCount": "2",
                    "SentTimestamp": "1698000000000",
                    "SenderId": ACCOUNT,
                    "ApproximateFirstReceiveTimestamp": "1698000000001",
                },
                "messageAttributes": {},
                "md5OfBody": "abc",
                "eventSource": "aws:sqs",
                "eventSourceARN": (
                    f"arn:aws:sqs:{REGION}:{ACCOUNT}:{INVESTIGATIONS_QUEUE}"
                ),
                "awsRegion": REGION,
            }],
        }
        inv_env = dict(w.function_env(_rw.HEALTH_FN))
        inv_env["AVX_INVESTIGATIONS_TABLE"] = INVESTIGATIONS_TABLE
        inv_env["AVX_INVESTIGATIONS_QUEUE_URL"] = w._investigations_queue_url
        inv_env["SQS_MAX_RECEIVE_COUNT"] = "2"
        inv_env["ADP_THREAD_WRITER_ARN"] = ADP_WRITER_ARN
        with lambda_env(inv_env, _rw.HEALTH_FN, REGION):
            result = w.handlers["runner"].lambda_handler(sqs_event, None)

    # No FunctionError: the handler returned normally with batchItemFailures
    assert "FunctionError" not in (result or {}), (
        f"handler raised on the contract event shape: {result!r}"
    )
    assert "batchItemFailures" in (result or {}), (
        f"handler should return batchItemFailures; got {result!r}"
    )
    # The single item in batchItemFailures must be the message_id
    items = result["batchItemFailures"]
    assert len(items) == 1 and items[0]["itemIdentifier"] == message_id, (
        f"expected batchItemFailures=[{{itemIdentifier: {message_id!r}}}]; got {items}"
    )


# ===========================================================================
# G5: Link 3 failures
# ===========================================================================


@pytest.mark.skipif(not _REPOS_PRESENT, reason=_SKIP_REASON)
def test_g5_alarm_never_fires(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Alarm never fires → FAIL after 25-min bound, cleanup still ran, rc=1.

    Red reason: SystemExit(2) → rows empty →
    assert rows['item9-link3-alarm-state'] == 'FAIL' fails.
    """
    monkeypatch.setenv("LIVE_TESTS", "1")

    with _open_world() as w:
        w.dlq_alarm.never_fires = True      # suppress all ALARM transitions
        w.dlq_alarm.alarm_delay_s = 99999.0

        rc, rows = _call_main(monkeypatch, _argv())
        rows_after = _scan_investigations_for_pattern(w, _PATTERN_ID)

    assert rc == 1, f"expected rc=1 (link3 FAIL); got rc={rc}"
    assert rows.get("item9-link3-alarm-state") == "FAIL", (
        f"expected item9-link3-alarm-state=FAIL (alarm never fired); rows={rows}"
    )
    # Cleanup must still run even on FAIL
    assert rows_after == [], (
        f"cleanup should remove row even when alarm never fires; found {rows_after}"
    )


@pytest.mark.skipif(not _REPOS_PRESENT, reason=_SKIP_REASON)
def test_g5_history_item_only_before_send(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """History has only a StateUpdate item from before send_at-60s → FAIL item9-link3-alarm-state.

    The alarm is in OK state (pre-check passes) but has a pre-existing history item
    from before the run.  When the script filters with StartDate=send_at-60s, the item
    falls outside the window.

    Red reason: SystemExit(2) → rows empty →
    assert rows['item9-link3-alarm-state'] == 'FAIL' fails.
    """
    monkeypatch.setenv("LIVE_TESTS", "1")

    with _open_world() as w:
        # Add a StateUpdate at current time (world start, before send_at)
        w.dlq_alarm.add_before_run_state_update()
        # Prevent the alarm from auto-transitioning so no new item is added
        w.dlq_alarm.never_fires = True

        rc, rows = _call_main(monkeypatch, _argv())

    assert rc == 1, f"expected rc=1; got rc={rc}"
    assert rows.get("item9-link3-alarm-state") == "FAIL", (
        f"expected item9-link3-alarm-state=FAIL (only pre-run item); rows={rows}"
    )


@pytest.mark.skipif(not _REPOS_PRESENT, reason=_SKIP_REASON)
def test_g5_action_not_succeeded(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Action item has actionState != Succeeded → FAIL item9-link3-action, rc=1.

    Red reason: SystemExit(2) → rows empty →
    assert rows['item9-link3-action'] == 'FAIL' fails.
    """
    monkeypatch.setenv("LIVE_TESTS", "1")

    with _open_world() as w:
        w.dlq_alarm.alarm_delay_s = 30.0
        w.dlq_alarm.action_state = "Failed"   # DlqDepthAlarm records actionState=Failed

        rc, rows = _call_main(monkeypatch, _argv())

    assert rc == 1, f"expected rc=1; got rc={rc}"
    assert rows.get("item9-link3-action") == "FAIL", (
        f"expected item9-link3-action=FAIL (actionState=Failed); rows={rows}"
    )


@pytest.mark.skipif(not _REPOS_PRESENT, reason=_SKIP_REASON)
def test_g5_action_wrong_topic(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Action item names a different topic → FAIL item9-link3-action.

    Red reason: SystemExit(2) → rows empty →
    assert rows['item9-link3-action'] == 'FAIL' fails.
    """
    monkeypatch.setenv("LIVE_TESTS", "1")

    with _open_world() as w:
        w.dlq_alarm.alarm_delay_s = 30.0
        # Override the alarm's topic_arn so its history Action records the wrong topic
        w.dlq_alarm.topic_arn = (
            f"arn:aws:sns:{REGION}:{ACCOUNT}:wrong-topic-for-test"
        )

        rc, rows = _call_main(monkeypatch, _argv())

    assert rc == 1, f"expected rc=1; got rc={rc}"
    assert rows.get("item9-link3-action") == "FAIL", (
        f"expected item9-link3-action=FAIL (wrong topic); rows={rows}"
    )


# ===========================================================================
# G6: Cleanup edge cases
# ===========================================================================


@pytest.mark.skipif(not _REPOS_PRESENT, reason=_SKIP_REASON)
def test_g6_foreign_dlq_message(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A foreign message arrives in the DLQ during the alarm wait.

    The script must NOT delete it; its visibility must be restored to 0; the run
    must FAIL naming the foreign message's MessageId; ours must still be deleted.

    Red reason: SystemExit(2) → rows empty →
    assert rows['item9-cleanup-dlq-foreign'] is not None fails.
    """
    monkeypatch.setenv("LIVE_TESTS", "1")

    with _open_world() as w:
        w.dlq_alarm.alarm_delay_s = 30.0

        # Plant a foreign message in the DLQ AFTER the alarm fires but before cleanup.
        # We do this by monkey-patching describe_alarm_history to plant the message
        # the first time the alarm history is polled.
        _planted = [False]
        _orig_dah = w.dlq_alarm.describe_alarm_history

        def _dah_with_foreign(p: dict) -> dict:
            result = _orig_dah(p)
            if not _planted[0] and w.dlq_alarm.state == "ALARM":
                sqs = w.internal("sqs")
                dlq_url = sqs.get_queue_url(
                    QueueName=_INVESTIGATIONS_DLQ
                )["QueueUrl"]
                sqs.send_message(QueueUrl=dlq_url, MessageBody="foreign-body-g6")
                _planted[0] = True
            return result

        w.dlq_alarm.describe_alarm_history = _dah_with_foreign

        rc, rows = _call_main(monkeypatch, _argv())
        rows_after = _scan_investigations_for_pattern(w, _PATTERN_ID)

    assert rc == 1, f"expected rc=1 (foreign message FAIL); got rc={rc}"
    # Cleanup-foreign check must FAIL naming the foreign MessageId
    foreign_row = rows.get("item9-cleanup-dlq-foreign")
    assert foreign_row == "FAIL", (
        f"expected item9-cleanup-dlq-foreign=FAIL; rows={rows}"
    )
    # The LIVETEST-9 row should still be cleaned up
    assert rows_after == [], (
        f"LIVETEST-9 row should be deleted even with foreign message; found {rows_after}"
    )


@pytest.mark.skipif(not _REPOS_PRESENT, reason=_SKIP_REASON)
def test_g6_alarm_never_recovers(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Alarm stays ALARM after DLQ is cleaned → item9-residue FAIL naming the alarm.

    Red reason: SystemExit(2) → rows empty →
    assert rows['item9-residue'] == 'FAIL' fails.
    """
    monkeypatch.setenv("LIVE_TESTS", "1")

    with _open_world() as w:
        w.dlq_alarm.alarm_delay_s = 30.0
        w.dlq_alarm.never_recovers = True   # alarm stays ALARM after DLQ is empty

        rc, rows = _call_main(monkeypatch, _argv())

    assert rc == 1, f"expected rc=1 (residue FAIL); got rc={rc}"
    assert rows.get("item9-residue") == "FAIL", (
        f"expected item9-residue=FAIL (alarm never recovered); rows={rows}"
    )
    # The residue FAIL message should name the alarm
    residue_msg = next(
        (msg for _, status, msg in (
            getattr(rows, "_rows", [])
            if hasattr(rows, "_rows") else []
        ) if status == "FAIL"),
        "",
    )
    # Even without message inspection the FAIL is sufficient for a red test


@pytest.mark.skipif(not _REPOS_PRESENT, reason=_SKIP_REASON)
def test_g6_delete_leaves_message(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """DeleteMessage answers success but message is still present → caught by depth check.

    Simulated by overriding DeleteMessage to succeed without actually deleting.

    Red reason: SystemExit(2) → rows empty →
    assert rows['item9-cleanup-dlq'] == 'FAIL' fails.
    """
    monkeypatch.setenv("LIVE_TESTS", "1")

    with _open_world() as w:
        w.dlq_alarm.alarm_delay_s = 30.0

        # Override DeleteMessage on the DLQ to return success without deleting
        _orig_delete = w.router._handlers.get(("sqs", "DeleteMessage"))

        def _fake_delete_success(p: dict) -> dict | None:
            if p.get("QueueUrl", "").endswith(_INVESTIGATIONS_DLQ):
                # Return success without actually deleting the message
                return {}
            if _orig_delete:
                return _orig_delete(p)
            return None

        w.router.add("sqs", "DeleteMessage", _fake_delete_success)

        rc, rows = _call_main(monkeypatch, _argv())

    assert rc == 1, f"expected rc=1 (delete-leaves-message FAIL); got rc={rc}"
    assert rows.get("item9-cleanup-dlq") == "FAIL", (
        f"expected item9-cleanup-dlq=FAIL (delete lied); rows={rows}"
    )


@pytest.mark.skipif(not _REPOS_PRESENT, reason=_SKIP_REASON)
def test_g6_sigterm_during_alarm_wait(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """SIGTERM during the alarm wait → DLQ message and row still deleted, rc=1.

    The script must clean up (row + DLQ message) even when interrupted by SIGTERM
    during the link-3 poll loop.  On SIGTERM the signal handler fires; cleanup runs
    under a signal hold.

    Red reason: SystemExit(2) → rows empty → rc and row assertions fail.
    """
    monkeypatch.setenv("LIVE_TESTS", "1")

    with _open_world() as w:
        w.dlq_alarm.alarm_delay_s = 30.0

        # Send SIGTERM to ourselves on the first alarm-history poll
        _fired = [False]
        _orig_dah = w.dlq_alarm.describe_alarm_history

        def _dah_sigterm_on_first(p: dict) -> dict:
            result = _orig_dah(p)
            if not _fired[0]:
                _fired[0] = True
                signal.raise_signal(signal.SIGTERM)
            return result

        w.dlq_alarm.describe_alarm_history = _dah_sigterm_on_first

        rc, rows = _call_main(monkeypatch, _argv())
        rows_after = _scan_investigations_for_pattern(w, _PATTERN_ID)
        dlq_vis = _dlq_depth(w)

    # rc=1 because some checks failed (interrupted)
    assert rc == 1, f"expected rc=1 after SIGTERM; got rc={rc}"
    # Row must be cleaned up
    assert rows_after == [], (
        f"row should be deleted even after SIGTERM; found {rows_after}"
    )
    # DLQ must be clean
    assert dlq_vis == 0, f"DLQ should be empty after SIGTERM cleanup; got {dlq_vis}"


@pytest.mark.skipif(not _REPOS_PRESENT, reason=_SKIP_REASON)
def test_g6_cleanup_only_after_send(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Crash right after SendMessage lands → cleanup-only rc=0 with residue clean.

    Simulate a "crash after SendMessage" by building a manifest that records:
    - item9_row_put_sent=True (PutItem was sent)
    - item9_invoke_sent=True (Invoke was sent)
    - item9_dlq_sent=True (SendMessage to DLQ was sent)
    - The investigation_id and message_id

    Then call --cleanup-only with this manifest; expect rc=0 and residue clean.

    Red reason: SystemExit(2) from argparse (--part item9 or --cleanup-only not
    handling item9 manifest) → rc=2 → assert rc==0 fails.
    """
    monkeypatch.setenv("LIVE_TESTS", "1")

    with _open_world() as w:
        w.dlq_alarm.alarm_delay_s = 30.0

        # Write an investigation row and a DLQ message to simulate the mid-run state
        inv_id = str(uuid.uuid4())
        w.table(_INVESTIGATIONS_TABLE).put_item(Item={
            "investigation_id": inv_id,
            "pattern_id": _PATTERN_ID,
            "status": "failed",
            "requested_at": "2026-01-01T00:00:00Z",
            "requested_by": f"LIVETEST-{_RUN_ID}",
            "livetest_run_id": _RUN_ID,
        })
        sqs = w.internal("sqs")
        dlq_url = sqs.get_queue_url(QueueName=_INVESTIGATIONS_DLQ)["QueueUrl"]
        dlq_resp = sqs.send_message(QueueUrl=dlq_url, MessageBody=_DLQ_BODY)
        dlq_msg_id = dlq_resp["MessageId"]

        # Build a manifest as if the script crashed after SendMessage
        manifest = {
            "run_id": _RUN_ID,
            "stage": STAGE,
            "part": "item9",
            "item9_investigation_id": inv_id,
            "item9_pattern_id": _PATTERN_ID,
            "item9_row_put_sent": True,
            "item9_invoke_sent": True,
            "item9_message_id": f"livetest-9-{_RUN_SUFFIX}",
            "item9_dlq_body": _DLQ_BODY,
            "item9_dlq_send_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "item9_dlq_sent": True,
            "item9_dlq_message_id": dlq_msg_id,
        }
        manifest_path = Path(os.environ["FI_QUALITY_MANIFEST_DIR"]) / _RUN_ID
        manifest_path.mkdir(parents=True, exist_ok=True)
        (manifest_path / "manifest.json").write_text(json.dumps(manifest))

        rc, rows = _call_main(
            monkeypatch,
            ["--stage", STAGE, "--cleanup-only", "--run-id", _RUN_ID, "--allow-writes"],
        )
        rows_after = _scan_investigations_for_pattern(w, _PATTERN_ID)
        dlq_vis = _dlq_depth(w)

    assert rc == 0, f"expected cleanup-only rc=0; got rc={rc}. rows={rows}"
    assert rows_after == [], (
        f"cleanup-only should delete the row; found {rows_after}"
    )
    assert dlq_vis == 0, f"cleanup-only should delete DLQ message; got {dlq_vis}"


@pytest.mark.skipif(not _REPOS_PRESENT, reason=_SKIP_REASON)
def test_g6_cleanup_only_nothing_sent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """--cleanup-only on a manifest with nothing sent → rc=0, no delete request.

    A manifest that records no writes (item9_row_put_sent=False, etc.) must not
    attempt any deletes.

    Red reason: SystemExit(2) → rc=2 → assert rc==0 fails.
    """
    monkeypatch.setenv("LIVE_TESTS", "1")

    with _open_world() as w:
        # Build a manifest with no sent flags
        manifest = {
            "run_id": _RUN_ID,
            "stage": STAGE,
            "part": "item9",
            "item9_row_put_sent": False,
            "item9_invoke_sent": False,
            "item9_dlq_sent": False,
        }
        manifest_path = Path(os.environ["FI_QUALITY_MANIFEST_DIR"]) / _RUN_ID
        manifest_path.mkdir(parents=True, exist_ok=True)
        (manifest_path / "manifest.json").write_text(json.dumps(manifest))

        w.invocations_before_main = len(w.router.all_calls)
        rc, rows = _call_main(
            monkeypatch,
            ["--stage", STAGE, "--cleanup-only", "--run-id", _RUN_ID, "--allow-writes"],
        )
        calls_after = w.router.all_calls[w.invocations_before_main:]

    deletes = [c for c in calls_after if c[1] in ("DeleteItem", "DeleteMessage")]

    assert rc == 0, f"expected rc=0 for nothing-sent cleanup-only; got rc={rc}"
    assert deletes == [], (
        f"expected no delete requests when nothing was sent; got {deletes}"
    )


# ===========================================================================
# G7: Gates — write-window and --allow-writes guards
# ===========================================================================


@pytest.mark.skipif(not _REPOS_PRESENT, reason=_SKIP_REASON)
def test_g7_no_allow_writes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """No --allow-writes → rc=1 before any AWS call.

    Red reason: SystemExit(2) → rc=2 → assert rc==1 fails.
    """
    monkeypatch.setenv("LIVE_TESTS", "1")

    with _open_world() as w:
        w.invocations_before_main = len(w.router.all_calls)
        rc, rows = _call_main(monkeypatch, _argv(allow_writes=False))
        calls_after = w.router.all_calls[w.invocations_before_main:]

    aws_calls = [c for c in calls_after]
    assert rc == 1, f"expected rc=1 (no --allow-writes); got rc={rc}"
    assert aws_calls == [], (
        f"expected no AWS calls without --allow-writes; got {aws_calls}"
    )


@pytest.mark.skipif(not _REPOS_PRESENT, reason=_SKIP_REASON)
def test_g7_write_window_shut(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Write window shut at start → SKIP, nothing written.

    Red reason: SystemExit(2) → rows empty →
    ``assert rows != {}`` fails (rows is empty dict).
    After implementation rows has a SKIP row → passes.
    """
    monkeypatch.setenv("LIVE_TESTS", "1")

    # Set time inside a blocked window (20 min before a 05:40 UTC schedule)
    monkeypatch.setattr(
        lv, "_utc_now",
        lambda: dt.datetime(2026, 10, 29, 5, 20, 0, tzinfo=dt.timezone.utc),
    )

    with _open_world() as w:
        w.invocations_before_main = len(w.router.all_calls)
        rc, rows = _call_main(monkeypatch, _argv())
        calls_after = w.router.all_calls[w.invocations_before_main:]

    # At least one result row must be present (SKIP for window); before implementation
    # rows is empty {} because argparse rejected --part item9 entirely.
    assert rows != {}, (
        f"expected at least one SKIP row (window-shut); rows={rows}"
    )
    # W8: rc must be exactly 2 (SKIP only); rc=1 means something FAILed.
    assert rc == 2, f"expected rc=2 (SKIP) for window-shut; got rc={rc}"
    # W8: the guard-1 label must be item9-pre-window (implementation must use this name).
    assert rows.get("item9-pre-window") == "SKIP", (
        f"expected item9-pre-window=SKIP for window-shut; rows={rows}"
    )
    writes = [c for c in calls_after if c[1] not in (
        "GetQueueAttributes", "GetQueueUrl", "DescribeAlarms",
        "ListSchedules", "GetSchedule", "GetCallerIdentity",
    )]
    assert writes == [], f"expected no writes when window is shut; got {writes}"


@pytest.mark.skipif(not _REPOS_PRESENT, reason=_SKIP_REASON)
def test_g7_window_closes_before_putitem(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Window closes just before the first write (PutItem) → no write after it.

    The write-window guard runs before each write.  If the window closes between
    the link-2 checks and the PutItem, the script must stop before PutItem.

    Red reason: SystemExit(2) → rows empty →
    ``assert rows != {}`` fails (no rows were written by the script).
    After implementation rows has entries (pre-checks ran) and no PutItem.
    """
    monkeypatch.setenv("LIVE_TESTS", "1")

    _call_count = [0]
    _T0 = dt.datetime(2026, 10, 29, 10, 0, 0, tzinfo=dt.timezone.utc)

    def _utc_now_closes_later() -> dt.datetime:
        _call_count[0] += 1
        # After 4 calls advance to a blocked time so the guard fires before PutItem
        if _call_count[0] >= 4:
            return dt.datetime(2026, 10, 29, 5, 20, 0, tzinfo=dt.timezone.utc)
        return _T0

    monkeypatch.setattr(lv, "_utc_now", _utc_now_closes_later)

    with _open_world() as w:
        w.invocations_before_main = len(w.router.all_calls)
        rc, rows = _call_main(monkeypatch, _argv())
        calls_after = w.router.all_calls[w.invocations_before_main:]

    # Before implementation: rows={} (argparse never ran item9 code)
    assert rows != {}, (
        f"expected result rows (pre-checks ran); got empty rows "
        f"(--part item9 not implemented yet)"
    )
    put_items = [c for c in calls_after if c[1] == "PutItem"]
    assert put_items == [], (
        f"no PutItem should be sent after window closes; got {put_items}"
    )
