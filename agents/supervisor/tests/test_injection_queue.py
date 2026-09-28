"""Unit tests for AgentInjectionQueue.

The queue is the mechanism that defers P2/P3 triage guidance injection
until Nova Sonic's current assistant turn completes — or, failing
that, flushes on a timeout because Nova Sonic v2 often never emits
the assistant-role is_final=true signal that would normally drive
mark_response_complete().

These tests exercise:
  - IMMEDIATE flushes regardless of turn state
  - DEFERRED waits for mark_response_complete() and then flushes after
    the post-turn grace window
  - DEFERRED flushes after _MAX_DEFER_SECONDS even if the turn never
    completes (the headline bug fix being validated)
  - Grace-window race: a new turn starting during the grace window
    re-defers the pending injection
  - stop() drops pending items idempotently and is safe under races

Uses a fake BidiAgent that records send() calls — no real Nova Sonic,
no real network. Time-sensitive tests override the class-level
_POST_TURN_GRACE_MS and _MAX_DEFER_SECONDS constants to short values
so the suite runs fast.

Run:
    pytest agents/supervisor/tests/test_injection_queue.py -v
"""

from __future__ import annotations

import asyncio
import time

import pytest

from agents.supervisor.bidi_app import (
    AgentInjectionQueue,
    InjectionPriority,
    InjectionRequest,
)


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class FakeAgent:
    """Records every payload sent through agent.send() for later assertions.

    Deliberately thin: the queue only calls `await agent.send(payload)`,
    so that's all we need to stand in for. Using a real Mock with
    AsyncMock pulls in extra machinery that obscures what the queue
    actually does under test.
    """

    def __init__(self) -> None:
        self.calls: list[str] = []
        # Optional: raise on next send() to exercise the error-swallow path.
        self.raise_next: Exception | None = None

    async def send(self, payload: str) -> None:
        if self.raise_next is not None:
            exc, self.raise_next = self.raise_next, None
            raise exc
        self.calls.append(payload)


def _req(reason: str, priority: str = InjectionPriority.DEFERRED) -> InjectionRequest:
    """Build a small InjectionRequest for tests. Payload encodes the
    reason so failed assertions are self-describing."""
    return InjectionRequest(
        payload=f"payload-for-{reason}",
        priority=priority,
        reason=reason,
    )


@pytest.fixture
def fast_queue_constants(monkeypatch):
    """Shrink queue timing constants so tests complete in <1s instead of
    waiting the production 8-second deadline. The ratio between grace
    window and max-defer is preserved so race conditions between them
    remain testable.

    Production defaults:
      _POST_TURN_GRACE_MS = 300   (0.3s)
      _MAX_DEFER_SECONDS  = 8.0
    Test overrides:
      _POST_TURN_GRACE_MS = 50    (0.05s)
      _MAX_DEFER_SECONDS  = 0.4
    """
    monkeypatch.setattr(AgentInjectionQueue, "_POST_TURN_GRACE_MS", 50)
    monkeypatch.setattr(AgentInjectionQueue, "_MAX_DEFER_SECONDS", 0.4)


@pytest.fixture
async def queue(fast_queue_constants):
    """Standard queue wired to a FakeAgent, started, and cleaned up
    on teardown. Yields (queue, agent) so tests can assert on the
    agent's recorded calls."""
    agent = FakeAgent()
    q = AgentInjectionQueue(agent, session_id="test-session")
    q.start()
    try:
        yield q, agent
    finally:
        await q.stop()


# ---------------------------------------------------------------------------
# IMMEDIATE path
# ---------------------------------------------------------------------------


async def test_immediate_flushes_without_turn(queue):
    """IMMEDIATE requests dispatch right away when no turn is in flight."""
    q, agent = queue
    await q.submit(_req("immediate-a", InjectionPriority.IMMEDIATE))
    # Give the dispatcher a tick to drain.
    await asyncio.sleep(0.05)
    assert agent.calls == ["payload-for-immediate-a"]


async def test_immediate_flushes_during_turn(queue):
    """IMMEDIATE requests dispatch even while a turn is in flight.
    This is the P0/P1 safety-override path: safety-critical narration
    must preempt in-flight assistant speech."""
    q, agent = queue
    q.mark_response_start()  # turn is speaking
    await q.submit(_req("p0-urgent", InjectionPriority.IMMEDIATE))
    await asyncio.sleep(0.05)
    assert agent.calls == ["payload-for-p0-urgent"]


# ---------------------------------------------------------------------------
# DEFERRED path — turn-complete trigger
# ---------------------------------------------------------------------------


async def test_deferred_waits_for_turn_complete(queue):
    """DEFERRED holds the payload while the turn is in flight and flushes
    once mark_response_complete() fires (plus the small grace window)."""
    q, agent = queue
    q.mark_response_start()
    await q.submit(_req("p3-guidance"))

    # While the turn is in flight, the request must not have flushed.
    # Wait longer than the grace window to rule out accidental early flush.
    await asyncio.sleep(0.1)
    assert agent.calls == [], "DEFERRED flushed while turn was still in flight"

    # Now signal turn-complete. The dispatcher should wait the grace
    # window (~50ms in tests) and then dispatch.
    q.mark_response_complete()
    await asyncio.sleep(0.15)
    assert agent.calls == ["payload-for-p3-guidance"]


async def test_deferred_flushes_immediately_when_idle(queue):
    """If no turn is in flight when a DEFERRED request is submitted, it
    still respects the grace window but then flushes promptly — no
    need to wait for a mark_response_complete() that already happened."""
    q, agent = queue
    # No turn marked as started.
    await q.submit(_req("p3-idle"))
    # Grace window is 50ms in tests. Wait a bit longer.
    await asyncio.sleep(0.15)
    assert agent.calls == ["payload-for-p3-idle"]


# ---------------------------------------------------------------------------
# DEFERRED path — timeout trigger (the headline bug fix)
# ---------------------------------------------------------------------------


async def test_deferred_flushes_on_timeout_when_turn_never_completes(queue):
    """This is the bug this whole change exists to fix.

    Nova Sonic v2 often finishes speaking without emitting the
    assistant-role is_final=true transcript that would normally drive
    mark_response_complete(). Before this fix, such a turn would leave
    DEFERRED injections stuck in the queue forever until session
    teardown. The timeout guarantees the guidance eventually plays.
    """
    q, agent = queue
    q.mark_response_start()
    await q.submit(_req("p3-stuck"))

    # Never call mark_response_complete(). Wait longer than the test
    # override of _MAX_DEFER_SECONDS (0.4s).
    await asyncio.sleep(0.6)

    assert agent.calls == ["payload-for-p3-stuck"], (
        "DEFERRED should flush via timeout when turn never completes"
    )


async def test_deferred_timeout_honors_per_item_deadline(queue):
    """Each DEFERRED item has its own deadline from its submission time,
    not shared with others. A burst of submissions during a long turn
    shouldn't make later items inherit the earlier items' remaining
    budget — they should each get their own full _MAX_DEFER_SECONDS."""
    q, agent = queue
    q.mark_response_start()

    # Submit three items with staggered timing. Each one should flush
    # ~0.4s after its own submission time.
    await q.submit(_req("item-1"))
    t1 = time.monotonic()

    await asyncio.sleep(0.15)
    await q.submit(_req("item-2"))

    await asyncio.sleep(0.15)
    await q.submit(_req("item-3"))

    # Wait enough for all three to time out. Each has 0.4s from its
    # own submission; the dispatcher processes them serially so the
    # wall-clock total is ~3 * 0.4 = 1.2s in the worst case. Give
    # some slack.
    await asyncio.sleep(1.4)

    # All three should have flushed, in order.
    assert agent.calls == [
        "payload-for-item-1",
        "payload-for-item-2",
        "payload-for-item-3",
    ]


# ---------------------------------------------------------------------------
# Grace-window race
# ---------------------------------------------------------------------------


async def test_new_turn_during_grace_window_redefers(queue):
    """If a new assistant turn starts during the post-turn grace window,
    the dispatcher must re-defer the pending request rather than
    flushing into the middle of the new turn.

    The turn is cleared, a new turn starts inside the grace window,
    then that new turn completes. We expect exactly one final flush
    at the end — not an early one during the grace window.
    """
    q, agent = queue
    q.mark_response_start()
    await q.submit(_req("p3-racey"))

    # Let the dispatcher get to the waiting state.
    await asyncio.sleep(0.05)

    # Clear the turn, then immediately start a new one before the
    # grace window (50ms) elapses.
    q.mark_response_complete()
    await asyncio.sleep(0.02)  # well inside the 50ms grace window
    q.mark_response_start()

    # At this point the dispatcher should have seen the new turn and
    # re-entered the wait. The payload must not have flushed yet.
    await asyncio.sleep(0.08)  # past where the original grace window would have fired
    assert agent.calls == [], (
        "DEFERRED should NOT flush when a new turn starts during grace window"
    )

    # Now let the second turn complete cleanly. Payload should flush
    # after the normal grace period. (Per-item deadline is 0.4s from
    # submission; we're well within that.)
    q.mark_response_complete()
    await asyncio.sleep(0.15)
    assert agent.calls == ["payload-for-p3-racey"]


# ---------------------------------------------------------------------------
# Teardown / idempotency
# ---------------------------------------------------------------------------


async def test_stop_drops_pending_items(fast_queue_constants):
    """stop() should cancel the dispatcher and drop any pending items
    without raising. Items enqueued before stop() that haven't been
    dispatched yet are simply lost — acceptable because stop() runs
    at session teardown when the WebSocket is closing anyway.
    """
    agent = FakeAgent()
    q = AgentInjectionQueue(agent, session_id="stop-test")
    q.start()
    q.mark_response_start()  # force DEFERRED to wait
    await q.submit(_req("will-be-dropped"))

    # Stop before the timeout or turn-complete triggers a flush.
    await q.stop()

    # Nothing should have been sent.
    assert agent.calls == []


async def test_stop_is_idempotent(fast_queue_constants):
    """stop() must be safe to call multiple times (the websocket_handler's
    try/finally pattern can result in double-stops on error paths)."""
    agent = FakeAgent()
    q = AgentInjectionQueue(agent, session_id="idempotent-stop")
    q.start()
    await q.stop()
    await q.stop()  # must not raise
    # And start() after stop is a no-op — the task is None and _stopped
    # is True, so subsequent submits get the warn-and-drop path. Verify
    # that path too.
    await q.submit(_req("post-stop"))
    assert agent.calls == []


async def test_submit_after_stop_is_dropped(fast_queue_constants):
    """After stop(), further submit() calls must not crash and must
    not dispatch (the dispatcher task is gone)."""
    agent = FakeAgent()
    q = AgentInjectionQueue(agent, session_id="post-stop-submit")
    q.start()
    await q.stop()
    await q.submit(_req("too-late", InjectionPriority.IMMEDIATE))
    await asyncio.sleep(0.05)
    assert agent.calls == []


# ---------------------------------------------------------------------------
# Error isolation
# ---------------------------------------------------------------------------


async def test_send_exception_does_not_kill_dispatcher(queue):
    """If one injection's agent.send() raises, the dispatcher must catch
    the exception and keep processing subsequent items. Otherwise a
    single transient network glitch would silently kill triage guidance
    for the rest of the session."""
    q, agent = queue
    agent.raise_next = RuntimeError("simulated send failure")

    # First submission hits the raise.
    await q.submit(_req("bad-item", InjectionPriority.IMMEDIATE))
    # Second submission should still go through.
    await q.submit(_req("good-item", InjectionPriority.IMMEDIATE))

    await asyncio.sleep(0.1)

    # The bad item is lost; the good one arrives.
    assert agent.calls == ["payload-for-good-item"]
