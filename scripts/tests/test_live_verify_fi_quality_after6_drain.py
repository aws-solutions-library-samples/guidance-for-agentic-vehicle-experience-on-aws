"""Pre-check drain wait for the after-6 write items (issue 2026-10-28-fi-quality-after6-queue-precheck-race).

The 2026-10-28 live run SKIPped item 4 because its pre-check read the investigations queue as
in-flight=1: item 2's last message was still being processed (the job marks the row
``completed`` before the Lambda returns and the event source deletes the message), and SQS's
approximate counters can lag about a minute. ``_after6_pre_checks(..., wait_for_drain=True)``
waits a bounded time for the queue to read 0/0 on every read across a settle window, refuses
at once on a non-empty DLQ or a non-Enabled event source, and re-reads the event source after
the queue settles. Without ``wait_for_drain`` it behaves as before (one read, refuse).

Fake clock: ``lv.time`` is replaced with a namespace whose ``sleep`` advances ``monotonic``;
a read budget stops a mutant that loses its bounds from hanging the suite.

Run:
    PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest \
        scripts/tests/test_live_verify_fi_quality_after6_drain.py -q
"""
from __future__ import annotations

import importlib.util
import types
from pathlib import Path
from typing import Any, Callable

import pytest

_HERE = Path(__file__).resolve().parent
_SCRIPT = _HERE.parent / "live-verify-fi-quality.py"
_spec = importlib.util.spec_from_file_location("live_verify_fi_quality_drain", _SCRIPT)
assert _spec is not None
lv = importlib.util.module_from_spec(_spec)  # type: ignore[arg-type]
_spec.loader.exec_module(lv)  # type: ignore[union-attr]

_QUEUE = "vsa-staging-avx-investigations"
_READ_BUDGET = 200  # reads before the fake raises (a mutant without bounds must not hang)


class _Clock:
    def __init__(self, *, frozen: bool = False) -> None:
        self.t = 0.0
        self.sleeps: list[float] = []
        self.frozen = frozen

    def monotonic(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.sleeps.append(s)
        if not self.frozen:
            self.t += s


def _install_clock(monkeypatch: pytest.MonkeyPatch, clock: _Clock) -> None:
    monkeypatch.setattr(lv, "time", types.SimpleNamespace(monotonic=clock.monotonic, sleep=clock.sleep))


class _Lam:
    """ESM states returned per list call, in order; the last one repeats."""

    def __init__(self, states: list[str]) -> None:
        self.states = states
        self.calls = 0

    def list_event_source_mappings(self, **_kw: Any) -> dict:
        st = self.states[min(self.calls, len(self.states) - 1)]
        self.calls += 1
        if st == "<none>":
            return {"EventSourceMappings": []}
        return {"EventSourceMappings": [{
            "UUID": "uuid-1",
            "EventSourceArn": f"arn:aws:sqs:us-west-2:123456789012:{_QUEUE}",
            "State": st,
        }]}


class _Sqs:
    """``queue(t)`` / ``dlq(t)`` give (visible, in_flight) at fake time t."""

    def __init__(self, clock: _Clock, queue: Callable[[float], tuple[int, int]],
                 dlq: Callable[[float], tuple[int, int]] = lambda _t: (0, 0)) -> None:
        self.clock = clock
        self.queue = queue
        self.dlq = dlq
        self.queue_reads: list[float] = []
        self.dlq_reads: list[float] = []

    def get_queue_url(self, QueueName: str, **_kw: Any) -> dict:
        return {"QueueUrl": f"https://sqs.us-west-2.amazonaws.com/123456789012/{QueueName}"}

    def get_queue_attributes(self, QueueUrl: str, **_kw: Any) -> dict:
        if len(self.queue_reads) + len(self.dlq_reads) > _READ_BUDGET:
            raise RuntimeError("read budget exceeded: the drain wait is unbounded")
        if QueueUrl.endswith("-dlq"):
            self.dlq_reads.append(self.clock.t)
            v, i = self.dlq(self.clock.t)
        else:
            self.queue_reads.append(self.clock.t)
            v, i = self.queue(self.clock.t)
        return {"Attributes": {"ApproximateNumberOfMessages": str(v),
                               "ApproximateNumberOfMessagesNotVisible": str(i)}}


def _call(sqs: _Sqs, lam: _Lam, **kw: Any) -> str | None:
    return lv._after6_pre_checks(sqs, lam, "staging", **kw)


# ---------------------------------------------------------------------------
# The drain wait
# ---------------------------------------------------------------------------

def test_constants_bound_the_wait() -> None:
    assert lv._PRECHECK_SETTLE_S == 60
    assert lv._PRECHECK_DRAIN_INTERVAL_S == 10
    assert lv._PRECHECK_DRAIN_MAX_S == 240


def test_drain_waits_for_inflight_message_then_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    """In flight for 30 s (the 2026-10-28 case), then empty: passes after a full settle window."""
    clock = _Clock()
    _install_clock(monkeypatch, clock)
    sqs = _Sqs(clock, queue=lambda t: (0, 1) if t < 30 else (0, 0))
    assert _call(sqs, _Lam(["Enabled"]), wait_for_drain=True) is None
    first_zero = min(t for t in sqs.queue_reads if t >= 30)
    assert clock.t - first_zero >= lv._PRECHECK_SETTLE_S
    assert clock.t <= lv._PRECHECK_DRAIN_MAX_S


def test_drain_never_clears_refuses_after_the_bound(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = _Clock()
    _install_clock(monkeypatch, clock)
    sqs = _Sqs(clock, queue=lambda _t: (0, 1))
    reason = _call(sqs, _Lam(["Enabled"]), wait_for_drain=True)
    assert reason is not None
    assert "queue not empty before write" in reason
    assert "in-flight=1" in reason
    assert "waited" in reason
    assert lv._PRECHECK_DRAIN_MAX_S <= clock.t <= lv._PRECHECK_DRAIN_MAX_S + lv._PRECHECK_DRAIN_INTERVAL_S


def test_drain_is_bounded_when_the_clock_is_frozen(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = _Clock(frozen=True)
    _install_clock(monkeypatch, clock)
    sqs = _Sqs(clock, queue=lambda _t: (1, 0))
    reason = _call(sqs, _Lam(["Enabled"]), wait_for_drain=True)
    assert reason is not None and "queue not empty before write" in reason
    assert len(sqs.queue_reads) <= 2 * (lv._PRECHECK_DRAIN_MAX_S // lv._PRECHECK_DRAIN_INTERVAL_S) + 3


def test_drain_zero_streak_resets_on_a_nonzero_read(monkeypatch: pytest.MonkeyPatch) -> None:
    """0/0 for 50 s, one in-flight read at 60 s, then 0/0: the settle window restarts at 70 s."""
    clock = _Clock()
    _install_clock(monkeypatch, clock)
    sqs = _Sqs(clock, queue=lambda t: (0, 1) if 60 <= t < 70 else (0, 0))
    assert _call(sqs, _Lam(["Enabled"]), wait_for_drain=True) is None
    assert clock.t >= 70 + lv._PRECHECK_SETTLE_S


def test_drain_single_zero_read_does_not_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    """Counters can lag: one 0/0 read is not enough in wait mode."""
    clock = _Clock()
    _install_clock(monkeypatch, clock)
    sqs = _Sqs(clock, queue=lambda _t: (0, 0))
    assert _call(sqs, _Lam(["Enabled"]), wait_for_drain=True) is None
    assert clock.t >= lv._PRECHECK_SETTLE_S
    assert len(sqs.queue_reads) >= 2


def test_drain_zero_streak_too_short_at_the_bound_refuses(monkeypatch: pytest.MonkeyPatch) -> None:
    """In flight until 200 s: 0/0 only for the last 40 s of the 240 s bound, so refuse."""
    clock = _Clock()
    _install_clock(monkeypatch, clock)
    sqs = _Sqs(clock, queue=lambda t: (0, 1) if t < 200 else (0, 0))
    reason = _call(sqs, _Lam(["Enabled"]), wait_for_drain=True)
    assert reason is not None
    assert "settle" in reason


@pytest.mark.parametrize("dlq_depth", [(1, 0), (0, 1)])
def test_drain_dlq_nonempty_refuses_without_waiting(
    dlq_depth: tuple[int, int], monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Nothing drains the DLQ, so a message there (visible or in flight) refuses at once."""
    clock = _Clock()
    _install_clock(monkeypatch, clock)
    sqs = _Sqs(clock, queue=lambda _t: (0, 1), dlq=lambda _t: dlq_depth)
    reason = _call(sqs, _Lam(["Enabled"]), wait_for_drain=True)
    assert reason is not None and "DLQ not empty before write" in reason
    assert clock.sleeps == []


def test_drain_dlq_message_arriving_mid_wait_refuses(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = _Clock()
    _install_clock(monkeypatch, clock)
    sqs = _Sqs(clock, queue=lambda t: (0, 1) if t < 100 else (0, 0),
               dlq=lambda t: (1, 0) if t >= 20 else (0, 0))
    reason = _call(sqs, _Lam(["Enabled"]), wait_for_drain=True)
    assert reason is not None and "DLQ not empty before write" in reason
    assert clock.t <= 30


def test_drain_esm_disabled_refuses_before_any_queue_read(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = _Clock()
    _install_clock(monkeypatch, clock)
    sqs = _Sqs(clock, queue=lambda _t: (0, 1))
    reason = _call(sqs, _Lam(["Disabled"]), wait_for_drain=True)
    assert reason is not None and "not Enabled" in reason
    assert clock.sleeps == [] and sqs.queue_reads == []


def test_drain_esm_rechecked_after_the_queue_settles(monkeypatch: pytest.MonkeyPatch) -> None:
    """Enabled at the start, Disabled by the time the queue settles: refuse."""
    clock = _Clock()
    _install_clock(monkeypatch, clock)
    sqs = _Sqs(clock, queue=lambda _t: (0, 0))
    lam = _Lam(["Enabled", "Disabled"])
    reason = _call(sqs, lam, wait_for_drain=True)
    assert reason is not None and "not Enabled" in reason
    assert lam.calls >= 2


def test_drain_dlq_message_while_queue_empty_refuses(monkeypatch: pytest.MonkeyPatch) -> None:
    """Review cycle 1 W1: queue 0/0 throughout (what a redrive leaves), DLQ message on the read
    that would pass: refuse."""
    clock = _Clock()
    _install_clock(monkeypatch, clock)
    sqs = _Sqs(clock, queue=lambda _t: (0, 0), dlq=lambda t: (1, 0) if t >= lv._PRECHECK_SETTLE_S else (0, 0))
    reason = _call(sqs, _Lam(["Enabled"]), wait_for_drain=True)
    assert reason is not None and "DLQ not empty before write" in reason


def test_drain_sleeps_use_the_interval(monkeypatch: pytest.MonkeyPatch) -> None:
    """Review cycle 1 S1 (R5): every sleep is the 10 s interval, so the settle window spans 7 reads."""
    clock = _Clock()
    _install_clock(monkeypatch, clock)
    sqs = _Sqs(clock, queue=lambda t: (0, 1) if t < 30 else (0, 0))
    assert _call(sqs, _Lam(["Enabled"]), wait_for_drain=True) is None
    assert clock.sleeps and set(clock.sleeps) == {lv._PRECHECK_DRAIN_INTERVAL_S}
    zero_reads = [t for t in sqs.queue_reads if t >= 30]
    assert len(zero_reads) == lv._PRECHECK_SETTLE_S // lv._PRECHECK_DRAIN_INTERVAL_S + 1


def test_drain_read_error_is_not_a_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    """Review cycle 1 S1 (R4): a failed attribute read raises (the item FAILs), never counts as empty."""
    clock = _Clock()
    _install_clock(monkeypatch, clock)

    class _Boom(_Sqs):
        def get_queue_attributes(self, QueueUrl: str, **_kw: Any) -> dict:  # noqa: N803
            raise RuntimeError("throttled")

    with pytest.raises(RuntimeError, match="throttled"):
        _call(_Boom(clock, queue=lambda _t: (0, 0)), _Lam(["Enabled"]), wait_for_drain=True)


# ---------------------------------------------------------------------------
# Event source guard, both modes (review cycle 1 W2)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("wait", [False, True])
def test_no_mapping_refuses_before_any_queue_read(wait: bool, monkeypatch: pytest.MonkeyPatch) -> None:
    clock = _Clock()
    _install_clock(monkeypatch, clock)
    sqs = _Sqs(clock, queue=lambda _t: (0, 0))
    reason = _call(sqs, _Lam(["<none>"]), wait_for_drain=wait)
    assert reason is not None and "no SQS event source mapping" in reason
    assert sqs.queue_reads == [] and sqs.dlq_reads == [] and clock.sleeps == []


@pytest.mark.parametrize("state", ["Disabling", "Enabling", "Updating", "Creating", "Disabled"])
@pytest.mark.parametrize("wait", [False, True])
def test_non_enabled_state_refuses(state: str, wait: bool, monkeypatch: pytest.MonkeyPatch) -> None:
    clock = _Clock()
    _install_clock(monkeypatch, clock)
    sqs = _Sqs(clock, queue=lambda _t: (0, 0))
    reason = _call(sqs, _Lam([state]), wait_for_drain=wait)
    assert reason is not None and "not Enabled" in reason and state in reason
    assert sqs.queue_reads == []


@pytest.mark.parametrize("later", ["Disabling", "Updating", "<none>"])
def test_esm_change_during_the_settle_refuses(later: str, monkeypatch: pytest.MonkeyPatch) -> None:
    clock = _Clock()
    _install_clock(monkeypatch, clock)
    sqs = _Sqs(clock, queue=lambda _t: (0, 0))
    reason = _call(sqs, _Lam(["Enabled", later]), wait_for_drain=True)
    assert reason is not None
    assert ("not Enabled" in reason) if later != "<none>" else ("no SQS event source mapping" in reason)
    assert clock.t >= lv._PRECHECK_SETTLE_S


@pytest.mark.parametrize("wait", [False, True])
def test_one_enabled_and_one_disabled_mapping_refuses(wait: bool, monkeypatch: pytest.MonkeyPatch) -> None:
    """Review cycle 2 S2 (R15): every mapping on the queue must be Enabled, not just one."""
    clock = _Clock()
    _install_clock(monkeypatch, clock)

    class _TwoLam:
        def list_event_source_mappings(self, **_kw: Any) -> dict:
            arn = f"arn:aws:sqs:us-west-2:123456789012:{_QUEUE}"
            return {"EventSourceMappings": [{"UUID": "uuid-1", "EventSourceArn": arn, "State": "Enabled"},
                                            {"UUID": "uuid-2", "EventSourceArn": arn, "State": "Disabled"}]}

    sqs = _Sqs(clock, queue=lambda _t: (0, 0))
    reason = _call(sqs, _TwoLam(), wait_for_drain=wait)
    assert reason is not None and "not Enabled" in reason
    assert sqs.queue_reads == []


# ---------------------------------------------------------------------------
# Default (no wait) is unchanged
# ---------------------------------------------------------------------------

def test_default_refuses_on_inflight_without_waiting(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = _Clock()
    _install_clock(monkeypatch, clock)
    sqs = _Sqs(clock, queue=lambda _t: (0, 1))
    reason = _call(sqs, _Lam(["Enabled"]))
    assert reason is not None and "queue not empty before write: visible=0 in-flight=1" in reason
    assert clock.sleeps == []


def test_default_passes_on_one_empty_read(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = _Clock()
    _install_clock(monkeypatch, clock)
    sqs = _Sqs(clock, queue=lambda _t: (0, 0))
    assert _call(sqs, _Lam(["Enabled"])) is None
    assert clock.sleeps == [] and len(sqs.queue_reads) == 1


# ---------------------------------------------------------------------------
# The write items ask for the wait
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("item", ["2", "4", "5"])
def test_write_items_call_pre_checks_with_wait_for_drain(
    item: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "x")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "x")
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    monkeypatch.delenv("AWS_SESSION_TOKEN", raising=False)
    calls: list[dict] = []

    def _rec(*_a: Any, **kw: Any) -> str:
        calls.append(kw)
        return "stop-here"

    monkeypatch.setattr(lv, "_after6_write_window_guard", lambda *_a, **_k: None)
    monkeypatch.setattr(lv, "_after6_pre_checks", _rec)
    manifest = {"item4_thread_versions": [{"thread_id": "THR-x"}]}
    res = lv.Results()
    fn = {"2": lv._item_2, "4": lv._item_4, "5": lv._item_5}[item]
    fn(res, stage="staging", run_id="fi-quality-20261028T000000-00000000", manifest=manifest,
       manifest_dir=None, allow_writes=True, answer_key="/nonexistent.parquet")
    assert calls == [{"wait_for_drain": True}]
    assert any(st == "SKIP" and "stop-here" in why for _lbl, st, why in res.rows)
