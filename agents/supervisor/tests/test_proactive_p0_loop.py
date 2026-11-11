"""F1.3b: loop invariant (C3) and gate-driven suppression through the real path (W1).

C3 — "Never twice for one fault": runs the real _run_proactive_p0_watcher for 3+ ticks
     with the same C1234 fault (fixed firstSeenAt) returning P0 every tick.  Asserts
     exactly one classification event, one IMMEDIATE note, one platform.speak, triage
     called once.

     Mutant A: watcher.mark_alerted(fault_keys) → pass must fail this test.

W1 — Gate-driven suppression through the real path: after _show_proactive_p0_alert arms
     the gate, feed a model text segment that leads with the directive through the real
     _make_output_callable → no platform.speak; one that does not lead → one
     platform.speak.  Cancel event never set by hand.

     Mutant B: _cancel_ev.set() in _end_hazard_gate → pass must fail the leading test.

Item 3 — finally pops _proactive_p0_cancel only when it is the same event object:
     two overlapping alert calls; the first fallback's finally must not clear the second
     call's cancel event.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import agents.supervisor.bidi_app as bidi
from agents.supervisor.proactive_p0 import (
    ProactiveP0Watcher,
)
from agents.supervisor.dtc_response_catalog import p0_directive

# ---------------------------------------------------------------------------
# Shared test fixtures
# ---------------------------------------------------------------------------

BRAKE_FAULT = {
    "code": "C1234",
    "severity": "CRITICAL",
    "template_id": "brake_emergency",
    "firstSeenAt": 1_000_000,
}
FIRE_FAULT = {
    "code": "B0001",
    "severity": "CRITICAL",
    "template_id": "exit_vehicle",
    "firstSeenAt": 2_000_000,
}
BRAKE_DIRECTIVE = p0_directive([{"template_id": "brake_emergency"}], None)

_TRIAGE_P0 = {
    "classification": "P0",
    "sessionId": "s-loop",
    "decidedAt": "2026-11-05T10:00:00Z",
    "latencyMs": 120,
    "triageDecisionId": "t-loop",
}


class _Queue:
    """Injection-queue stub with all methods _make_output_callable calls."""

    def __init__(self) -> None:
        self.submitted: list = []

    async def submit(self, req) -> None:
        self.submitted.append(req)

    def clear_deferred_block(self) -> None:
        pass

    def cancel_deferred(self) -> None:
        pass

    def mark_response_complete(self) -> None:
        pass

    def mark_response_start(self) -> None:
        pass


def _ctx(**kw) -> SimpleNamespace:
    base = dict(
        session_id="s-loop",
        vin="VIN-LOOP",
        vehicle_id="VEH-LOOP",
        tenant_id="meridian",
        driver_id="DRV-LOOP",
        driver_first_name=None,
        triage_classification=None,
        triage_decision_id=None,
        triage_active_dtcs=[],
        p0_directive_for_model=None,
        open_reason=None,
        driver_confirmed_priority=None,
        driver_confirmation_pending=False,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def _state(**extra) -> dict:
    iq = _Queue()
    st: dict = {
        # proactive P0
        "platform_note_tag": "test1234",
        "event_count": 0,
        "type_counts": {},
        "surfaced_classification": None,
        "last_classification": None,
        "active_dtcs_for_prompt": [BRAKE_FAULT],
        "hazard_gate": None,
        "p0_directive": None,
        "p0_directive_template": None,
        "p0_directive_offered": {},
        "injection_queue": iq,
        "vehicle_ctx": {},
        "outbound_audio_chunks": 0,
        # _make_output_callable extras
        "tool_use_names": {},
        "escalation_tool_in_flight_at": None,
        "auto_triage_fired": True,
        "awaiting_confirmation_category": None,
        "_narration_user_turn": 0,
        "_narration_turn_text": "",
        "_narration_corrected_turn": None,
        "primed": True,
    }
    st.update(extra)
    return st


def _sent_msgs(ws) -> list[dict]:
    return [c.args[0] for c in ws.send_json.call_args_list]


def _drain_pending() -> asyncio.coroutine:
    """Cancel and await every task that is not the current one."""
    async def _do() -> None:
        pending = [
            t for t in asyncio.all_tasks()
            if not t.done() and t is not asyncio.current_task()
        ]
        for t in pending:
            t.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
    return _do()


# ---------------------------------------------------------------------------
# Part 1: loop test — C3 / Mutant A
# ---------------------------------------------------------------------------

class TestLoopAlertOnce:
    """C3: the watcher marks faults alerted so the same fault never alerts twice.

    Mutant A: ``watcher.mark_alerted(fault_keys)`` → ``pass`` breaks this invariant;
    every tick sends a new classification event.
    """

    def test_three_ticks_same_fault_alerts_exactly_once(self) -> None:
        """Run _run_proactive_p0_watcher for 3+ ticks, same C1234/P0 fault each time.

        Asserts:
        - exactly 1 'classification' event sent to the websocket
        - exactly 1 IMMEDIATE note on the injection queue
        - exactly 1 'platform.speak' event sent
        - call_triage_endpoint called exactly 1 time
        """
        ws = AsyncMock()
        ctx = _ctx()
        # Empty baseline so BRAKE_FAULT is new on every tick (without mark_alerted).
        st = _state(active_dtcs_for_prompt=[])
        watcher = ProactiveP0Watcher()
        cfg = MagicMock()
        cfg.api_base = "https://test.example.com"
        triage_mock = MagicMock(return_value=_TRIAGE_P0)

        _POLL = 0.01   # 10 ms between ticks
        _FB = 0.05     # 50 ms fallback

        async def _inner() -> None:
            with (
                patch.object(bidi, "PROACTIVE_P0_POLL_SECONDS", _POLL),
                patch.object(bidi, "PROACTIVE_P0_FALLBACK_SECONDS", _FB),
                patch.object(bidi, "_fetch_active_dtcs", return_value=[BRAKE_FAULT]),
                patch.object(bidi, "call_triage_endpoint", triage_mock),
            ):
                task = asyncio.create_task(
                    bidi._run_proactive_p0_watcher(
                        watcher,
                        websocket=ws,
                        ctx=ctx,
                        state=st,
                        config=cfg,
                    )
                )
                # 3+ ticks (3 × 10 ms) + fallback (50 ms) + buffer (50 ms)
                await asyncio.sleep(3 * _POLL + _FB + 0.05)
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                await _drain_pending()

        asyncio.run(_inner())

        msgs = _sent_msgs(ws)
        classification_events = [m for m in msgs if m.get("type") == "classification"]
        speak_events = [m for m in msgs if m.get("type") == "platform.speak"]
        iq = st["injection_queue"]
        immediate_notes = [
            r for r in iq.submitted
            if r.priority == bidi.InjectionPriority.IMMEDIATE
        ]

        assert len(classification_events) == 1, (
            f"Expected exactly 1 classification event, got {len(classification_events)}. "
            "Mutant A (mark_alerted → pass) allows re-alerting every tick."
        )
        assert len(immediate_notes) == 1, (
            f"Expected exactly 1 IMMEDIATE note, got {len(immediate_notes)}."
        )
        assert len(speak_events) == 1, (
            f"Expected exactly 1 platform.speak, got {len(speak_events)}."
        )
        assert triage_mock.call_count == 1, (
            f"Expected triage called 1 time, got {triage_mock.call_count}. "
            "Mutant A allows a triage call per tick."
        )


# ---------------------------------------------------------------------------
# Part 2: gate-driven suppression through the real path — W1 / Mutant B
# ---------------------------------------------------------------------------

class TestGateSuppressesRealPath:
    """W1: The fallback is suppressed when the model leads with the directive.

    The test runs through the real _make_output_callable / _hazard_gate_feed /
    _end_hazard_gate path. The cancel event is never set by hand.

    Mutant B: ``_cancel_ev.set()`` in ``_end_hazard_gate`` → ``pass``. Even when the
    model leads with the directive the cancel event is never set, so the fallback fires
    and sends platform.speak. The first test (leading) fails; the second (not leading)
    is unaffected.
    """

    def _run(self, model_text: str) -> list[dict]:
        """Arm the proactive P0 gate, feed one model text segment, wait for the
        fallback window to close, return all messages sent to the websocket."""
        ws = AsyncMock()
        ctx = _ctx()
        st = _state()
        _FB = 0.05

        async def _go() -> None:
            loop = asyncio.get_event_loop()
            with patch.object(bidi, "PROACTIVE_P0_FALLBACK_SECONDS", _FB):
                # Build the state: arm the gate and create the fallback task.
                await bidi._show_proactive_p0_alert(
                    ws, ctx, st, _TRIAGE_P0, [BRAKE_FAULT], loop
                )
                # Feed the model text segment through the real output callable.
                out = bidi._make_output_callable(
                    ws, ctx, st,
                    SimpleNamespace(),                      # config stub
                    SimpleNamespace(send=AsyncMock()),      # agent stub
                )
                # Speculative text only (no is_final=True) to avoid the narration
                # severity task, which would need a richer ctx.
                await out({
                    "type": "bidi_transcript_stream",
                    "role": "assistant",
                    "text": model_text,
                    "is_final": False,
                })
                # Wait for the fallback window to expire (or be cancelled).
                await asyncio.sleep(_FB + 0.08)
            await _drain_pending()

        asyncio.run(_go())
        return _sent_msgs(ws)

    def test_model_leading_with_directive_suppresses_fallback(self) -> None:
        """A model segment that starts with the directive → gate opens →
        _end_hazard_gate sets the cancel event → no platform.speak.

        Mutant B: _cancel_ev.set() → pass: the cancel event is never set, fallback
        fires regardless, and this test fails (speak count = 1 ≠ 0).
        """
        msgs = self._run(BRAKE_DIRECTIVE)
        speak = [m for m in msgs if m.get("type") == "platform.speak"]
        assert len(speak) == 0, (
            f"Expected no platform.speak when model leads with the directive; "
            f"got {speak}. "
            "Mutant B (_cancel_ev.set() → pass) causes the fallback to fire."
        )

    def test_model_not_leading_with_directive_fires_fallback(self) -> None:
        """A model segment that does NOT start with the directive → gate stays
        armed → fallback timeout fires → exactly one platform.speak."""
        doubt = "I cannot confirm this issue at this moment."
        msgs = self._run(doubt)
        speak = [m for m in msgs if m.get("type") == "platform.speak"]
        assert len(speak) == 1, (
            f"Expected exactly 1 platform.speak when model does not lead with "
            f"the directive; got {speak}."
        )


# ---------------------------------------------------------------------------
# Part 3: finally pops _proactive_p0_cancel only when it is the same event
# ---------------------------------------------------------------------------

class TestOverlappingAlerts:
    """The `_fallback` finally block uses `is ev` so a second alert's cancel event
    is not cleared by the first alert's fallback task completing."""

    def test_first_fallback_finally_does_not_clear_second_cancel_event(self) -> None:
        """After the first _show_proactive_p0_alert creates T1 with ev1, a second
        alert replaces state["_proactive_p0_cancel"] with ev2.  T1's finally must
        leave ev2 untouched.

        With the old code ``state.pop("_proactive_p0_cancel", None)`` T1's finally
        unconditionally removes ev2 and this assertion fails.
        """
        ws = AsyncMock()
        ctx = _ctx()
        st = _state()
        _FB = 0.05

        async def _go() -> None:
            loop = asyncio.get_event_loop()
            with patch.object(bidi, "PROACTIVE_P0_FALLBACK_SECONDS", _FB):
                # First alert: creates ev1 in state and starts task T1.
                await bidi._show_proactive_p0_alert(
                    ws, ctx, st, _TRIAGE_P0, [BRAKE_FAULT], loop
                )
                ev1 = st["_proactive_p0_cancel"]
                assert ev1 is not None

                # Simulate a second, overlapping alert by replacing the event
                # (exactly what a second _show_proactive_p0_alert call does).
                ev2 = asyncio.Event()
                st["_proactive_p0_cancel"] = ev2
                assert ev1 is not ev2

                # Let T1 time out and run its finally block.
                await asyncio.sleep(_FB + 0.06)

                # With the fix: T1's finally checks `is ev1` → ev2 is not ev1
                # → no pop → ev2 survives.
                assert st.get("_proactive_p0_cancel") is ev2, (
                    "T1's finally must not pop ev2 (the second alert's cancel event). "
                    "Old code: state.pop(...) removes ev2 unconditionally."
                )
            await _drain_pending()

        asyncio.run(_go())
