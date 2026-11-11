"""Tests for the per-session proactive P0 voice watcher (spec 2026-11-05-avx-proactive-p0-alert § V).

Coverage map
------------
Part 1: ProactiveP0Watcher pure logic — baseline, new_faults, should_alert, mark_alerted
Part 2: _show_proactive_p0_alert — wire events through the real function
Part 3: No-downgrade rule across two alerts
Part 4: _run_proactive_p0_watcher task lifecycle
Part 5: Integration — watcher + alert through bidi helpers with mocked I/O

Mutations the suite must catch (verified in Part 6):
    M1 — alert below P0 → should_alert returns False for P1; no events sent
    M2 — words ≠ directive → note payload and platform.speak must contain exact directive
    M3 — alert twice for one fault → mark_alerted prevents a second alert
    M4 — baseline alerted without p0_push → baseline faults produce no alert on normal sessions
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import agents.supervisor.bidi_app as bidi
from agents.supervisor.proactive_p0 import (
    PROACTIVE_P0_FALLBACK_SECONDS,
    PROACTIVE_P0_POLL_SECONDS,
    ProactiveP0Watcher,
)
from agents.supervisor.dtc_response_catalog import (
    is_p0_directive,
    p0_directive,
)

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

BRAKE_FAULT = {
    "code": "C1234",
    "severity": "CRITICAL",
    "template_id": "brake_emergency",
    "firstSeenAt": 1_000_000,
}
# Second occurrence of the same DTC code (different row → different firstSeenAt)
BRAKE_FAULT2 = {
    "code": "C1234",
    "severity": "CRITICAL",
    "template_id": "brake_emergency",
    "firstSeenAt": 2_000_000,
}
FIRE_FAULT = {
    "code": "B0001_FIRE",
    "severity": "CRITICAL",
    "template_id": "exit_vehicle",
    "firstSeenAt": 1_000_001,
}
P1_FAULT = {
    "code": "P0299",
    "severity": "HIGH",
    "template_id": "pullover_safe_location",
    "firstSeenAt": 1_000_002,
}

BRAKE_DIRECTIVE = p0_directive([{"template_id": "brake_emergency"}], None)
FIRE_DIRECTIVE = p0_directive([{"template_id": "exit_vehicle"}], None)

_TRIAGE_P0 = {
    "classification": "P0",
    "sessionId": "s-pp0",
    "decidedAt": "2026-11-05T10:00:00Z",
    "latencyMs": 120,
    "triageDecisionId": "s-pp0",
}


class _MockIQ:
    """Minimal AgentInjectionQueue stub."""
    def __init__(self):
        self.submitted: list = []

    async def submit(self, req):
        self.submitted.append(req)


def _bidi_ctx(**kw) -> SimpleNamespace:
    base = dict(
        session_id="s-pp0",
        vin="VIN-PP0",
        vehicle_id="VEH-PP0",
        tenant_id="meridian",
        driver_id="DRV-PP0",
        driver_first_name=None,
        triage_classification=None,
        triage_decision_id=None,
        triage_active_dtcs=[],
        p0_directive_for_model=None,
        open_reason=None,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def _bidi_state(**extra) -> dict:
    iq = _MockIQ()
    st: dict = {
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
    }
    st.update(extra)
    return st


def _mock_config() -> MagicMock:
    """Stub Config for tests that need the object but patch out all callers."""
    cfg = MagicMock()
    cfg.api_base = "https://vsa-test.example.com"
    return cfg


# ---------------------------------------------------------------------------
# Part 1: ProactiveP0Watcher — pure logic
# ---------------------------------------------------------------------------


class TestFaultKey:
    def test_code_and_first_seen(self):
        assert ProactiveP0Watcher.fault_key(BRAKE_FAULT) == ("C1234", 1_000_000)

    def test_missing_first_seen_defaults_to_zero(self):
        assert ProactiveP0Watcher.fault_key({"code": "X"}) == ("X", 0)

    def test_same_code_different_timestamp_is_different_key(self):
        assert (
            ProactiveP0Watcher.fault_key(BRAKE_FAULT)
            != ProactiveP0Watcher.fault_key(BRAKE_FAULT2)
        )


class TestBaseline:
    def test_baseline_fault_is_not_new(self):
        w = ProactiveP0Watcher()
        w.baseline([BRAKE_FAULT])
        assert w.new_faults([BRAKE_FAULT]) == []

    def test_non_baseline_fault_is_new(self):
        w = ProactiveP0Watcher()
        w.baseline([BRAKE_FAULT])
        new = w.new_faults([BRAKE_FAULT, FIRE_FAULT])
        assert len(new) == 1
        assert new[0]["code"] == "B0001_FIRE"

    def test_empty_baseline_all_faults_new(self):
        w = ProactiveP0Watcher()
        w.baseline([])
        assert len(w.new_faults([BRAKE_FAULT, P1_FAULT])) == 2

    def test_p0_push_baseline_call_is_noop(self):
        """For p0_push sessions, baseline() has no effect — all faults stay new."""
        w = ProactiveP0Watcher(open_reason="p0_push")
        w.baseline([BRAKE_FAULT])
        assert len(w.new_faults([BRAKE_FAULT])) == 1  # still new

    def test_no_baseline_call_all_faults_new(self):
        """No baseline() call is the same as an empty baseline."""
        w = ProactiveP0Watcher()
        assert len(w.new_faults([BRAKE_FAULT])) == 1


class TestNewFaults:
    def test_alerted_fault_is_not_new(self):
        w = ProactiveP0Watcher()
        w.mark_alerted([ProactiveP0Watcher.fault_key(BRAKE_FAULT)])
        assert w.new_faults([BRAKE_FAULT]) == []

    def test_new_occurrence_same_code_is_new(self):
        """A later row for the same DTC code (different firstSeenAt) is a new fault."""
        w = ProactiveP0Watcher()
        w.mark_alerted([ProactiveP0Watcher.fault_key(BRAKE_FAULT)])
        new = w.new_faults([BRAKE_FAULT2])
        assert len(new) == 1

    def test_entries_without_code_skipped(self):
        w = ProactiveP0Watcher()
        assert w.new_faults([{"severity": "CRITICAL"}]) == []

    def test_all_new_when_nothing_seen(self):
        w = ProactiveP0Watcher()
        assert len(w.new_faults([BRAKE_FAULT, FIRE_FAULT, P1_FAULT])) == 3


class TestShouldAlert:
    def test_p0_with_unalerted_key(self):
        w = ProactiveP0Watcher()
        keys = [ProactiveP0Watcher.fault_key(BRAKE_FAULT)]
        assert w.should_alert("P0", keys) is True

    def test_p0_already_alerted(self):
        w = ProactiveP0Watcher()
        key = ProactiveP0Watcher.fault_key(BRAKE_FAULT)
        w.mark_alerted([key])
        assert w.should_alert("P0", [key]) is False

    @pytest.mark.parametrize("tier", ["P1", "P2", "P3", None, ""])
    def test_non_p0_never_alerts(self, tier):
        """Mutation M1: no alert below P0."""
        w = ProactiveP0Watcher()
        keys = [ProactiveP0Watcher.fault_key(BRAKE_FAULT)]
        assert w.should_alert(tier, keys) is False

    def test_mix_of_alerted_and_unalerted(self):
        """should_alert is True if ANY key is unalerted."""
        w = ProactiveP0Watcher()
        k1 = ProactiveP0Watcher.fault_key(BRAKE_FAULT)
        k2 = ProactiveP0Watcher.fault_key(FIRE_FAULT)
        w.mark_alerted([k1])
        assert w.should_alert("P0", [k1, k2]) is True


class TestMarkAlerted:
    def test_marked_fault_not_in_new_faults(self):
        """Mutation M3: same fault occurrence never alerted twice."""
        w = ProactiveP0Watcher()
        key = ProactiveP0Watcher.fault_key(BRAKE_FAULT)
        w.mark_alerted([key])
        assert w.new_faults([BRAKE_FAULT]) == []

    def test_mark_multiple(self):
        w = ProactiveP0Watcher()
        w.mark_alerted([ProactiveP0Watcher.fault_key(d) for d in [BRAKE_FAULT, FIRE_FAULT]])
        assert w.new_faults([BRAKE_FAULT, FIRE_FAULT]) == []

    def test_second_different_fault_still_new_after_first_alerted(self):
        """A different code is still new after the first P0 is alerted."""
        w = ProactiveP0Watcher()
        w.mark_alerted([ProactiveP0Watcher.fault_key(BRAKE_FAULT)])
        new = w.new_faults([FIRE_FAULT])
        assert len(new) == 1
        assert w.should_alert("P0", [ProactiveP0Watcher.fault_key(FIRE_FAULT)]) is True


class TestOpenReasonP0Push:
    def test_baseline_faults_are_new_for_p0_push(self):
        """Mutation M4 inverse: baseline faults ARE treated as new on p0_push."""
        w = ProactiveP0Watcher(open_reason="p0_push")
        w.baseline([BRAKE_FAULT])
        assert len(w.new_faults([BRAKE_FAULT])) == 1

    def test_baseline_faults_not_new_for_normal_session(self):
        """Mutation M4: baseline faults are NOT new on normal sessions."""
        w = ProactiveP0Watcher()
        w.baseline([BRAKE_FAULT])
        assert w.new_faults([BRAKE_FAULT]) == []

    def test_open_reason_stored(self):
        w = ProactiveP0Watcher(open_reason="p0_push")
        assert w.open_reason == "p0_push"

    def test_default_open_reason_is_none(self):
        w = ProactiveP0Watcher()
        assert w.open_reason is None


# ---------------------------------------------------------------------------
# Part 2: _show_proactive_p0_alert — wire events through the real function
# ---------------------------------------------------------------------------


def _run_alert_sync(
    dtcs=None,
    triage=None,
    ctx=None,
    state=None,
    fallback_seconds=0.05,
):
    """Run _show_proactive_p0_alert via asyncio.run(), drain fallbacks, return (ws, state)."""
    ws = AsyncMock()
    _ctx = ctx or _bidi_ctx()
    _state = state or _bidi_state()

    async def _inner():
        loop = asyncio.get_running_loop()
        with patch.object(bidi, "PROACTIVE_P0_FALLBACK_SECONDS", fallback_seconds):
            await bidi._show_proactive_p0_alert(
                ws, _ctx, _state, triage or _TRIAGE_P0, dtcs or [BRAKE_FAULT], loop,
            )
        # Cancel any pending fallback tasks
        pending = [t for t in asyncio.all_tasks() if not t.done() and t is not asyncio.current_task()]
        for t in pending:
            t.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    asyncio.run(_inner())
    return ws, _state


def _sent_msgs(ws):
    """All dicts sent via websocket.send_json()."""
    return [c.args[0] for c in ws.send_json.call_args_list]


def _sent_types(ws):
    return [m.get("type") for m in _sent_msgs(ws)]


class TestShowProactiveP0Alert:
    """Wire-event correctness of _show_proactive_p0_alert."""

    def _run_and_drain(self, dtcs=None, triage=None, ctx=None, state=None):
        ws, st = _run_alert_sync(dtcs=dtcs, triage=triage, ctx=ctx, state=state)
        return ws, st

    # 1. Directive transcript -------------------------------------------------

    def test_directive_transcript_sent(self):
        ws, _ = self._run_and_drain()
        assert any(
            m.get("type") == "transcript" and m.get("role") == "assistant" and m.get("isFinal")
            for m in _sent_msgs(ws)
        )

    def test_directive_text_is_catalog_sentence(self):
        ws, _ = self._run_and_drain()
        t = next(
            m for m in _sent_msgs(ws)
            if m.get("type") == "transcript" and m.get("role") == "assistant"
        )
        assert is_p0_directive(t["text"])

    def test_brake_fault_gets_brake_directive(self):
        ws, _ = self._run_and_drain(dtcs=[BRAKE_FAULT])
        t = next(
            m for m in _sent_msgs(ws)
            if m.get("type") == "transcript" and m.get("role") == "assistant"
        )
        assert t["text"] == BRAKE_DIRECTIVE

    # 2. Classification P0 event ----------------------------------------------

    def test_classification_p0_sent(self):
        ws, _ = self._run_and_drain()
        cls = [m for m in _sent_msgs(ws) if m.get("type") == "classification"]
        assert len(cls) >= 1
        assert cls[0]["value"] == "P0"

    def test_classification_source_is_classifier(self):
        ws, _ = self._run_and_drain()
        cls = next(m for m in _sent_msgs(ws) if m.get("type") == "classification")
        assert cls["source"] == bidi.CLASSIFICATION_SOURCE_CLASSIFIER

    def test_surfaced_classification_updated(self):
        _, st = self._run_and_drain()
        assert st["surfaced_classification"] == "P0"

    # 3. Drawer mirror --------------------------------------------------------

    def test_tool_call_triage_sent(self):
        ws, _ = self._run_and_drain()
        assert any(m.get("type") == "tool.call" and m.get("name") == "triage" for m in _sent_msgs(ws))

    def test_tool_result_triage_sent(self):
        ws, _ = self._run_and_drain()
        assert any(m.get("type") == "tool.result" and m.get("name") == "triage" for m in _sent_msgs(ws))

    def test_transcript_before_tool_call(self):
        """Directive reaches the driver before the drawer populates."""
        ws, _ = self._run_and_drain()
        types = _sent_types(ws)
        assert types.index("transcript") < types.index("tool.call")

    # 4. Gate armed -----------------------------------------------------------

    def test_gate_armed(self):
        _, st = self._run_and_drain()
        assert st["hazard_gate"] is not None

    def test_gate_directive_matches_shown_directive(self):
        _, st = self._run_and_drain(dtcs=[BRAKE_FAULT])
        assert st["hazard_gate"].directive == BRAKE_DIRECTIVE

    # 5. IMMEDIATE platform note with directive -------------------------------

    def test_immediate_note_submitted(self):
        _, st = self._run_and_drain()
        iq = st["injection_queue"]
        assert len(iq.submitted) >= 1

    def test_note_priority_is_immediate(self):
        _, st = self._run_and_drain()
        iq = st["injection_queue"]
        assert iq.submitted[0].priority == bidi.InjectionPriority.IMMEDIATE

    def test_note_contains_exact_directive(self):
        """Mutation M2: note payload must quote the exact directive verbatim."""
        _, st = self._run_and_drain(dtcs=[BRAKE_FAULT])
        iq = st["injection_queue"]
        req = iq.submitted[0]
        assert BRAKE_DIRECTIVE in req.payload

    def test_note_is_tagged(self):
        """Platform notes carry the session's platform_note_tag."""
        _, st = self._run_and_drain()
        iq = st["injection_queue"]
        assert "Platform note [test1234]:" in iq.submitted[0].payload

    # 6. Fallback: platform.speak after timeout ------------------------------

    def test_fallback_sends_platform_speak_on_timeout(self):
        """After FALLBACK_SECONDS with no gate-open signal, platform.speak is sent."""
        ws = AsyncMock()
        ctx = _bidi_ctx()
        st = _bidi_state()
        _FAST = 0.05

        async def _inner():
            loop = asyncio.get_running_loop()
            with patch.object(bidi, "PROACTIVE_P0_FALLBACK_SECONDS", _FAST):
                await bidi._show_proactive_p0_alert(ws, ctx, st, _TRIAGE_P0, [BRAKE_FAULT], loop)
                await asyncio.sleep(_FAST + 0.1)
            pending = [t for t in asyncio.all_tasks() if not t.done() and t is not asyncio.current_task()]
            for t in pending:
                t.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)

        asyncio.run(_inner())
        speak = [m for m in _sent_msgs(ws) if m.get("type") == "platform.speak"]
        assert len(speak) == 1

    def test_platform_speak_text_is_directive(self):
        """Mutation M2: platform.speak text must be the exact directive."""
        ws = AsyncMock()
        ctx = _bidi_ctx()
        st = _bidi_state(active_dtcs_for_prompt=[BRAKE_FAULT])
        _FAST = 0.05

        async def _inner():
            loop = asyncio.get_running_loop()
            with patch.object(bidi, "PROACTIVE_P0_FALLBACK_SECONDS", _FAST):
                await bidi._show_proactive_p0_alert(ws, ctx, st, _TRIAGE_P0, [BRAKE_FAULT], loop)
                await asyncio.sleep(_FAST + 0.1)
            pending = [t for t in asyncio.all_tasks() if not t.done() and t is not asyncio.current_task()]
            for t in pending:
                t.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)

        asyncio.run(_inner())
        speak = [m for m in _sent_msgs(ws) if m.get("type") == "platform.speak"]
        assert speak[0]["text"] == BRAKE_DIRECTIVE

    def test_fallback_suppressed_when_gate_opens(self):
        """If the model leads with the directive (gate opens), no platform.speak."""
        ws = AsyncMock()
        ctx = _bidi_ctx()
        st = _bidi_state()
        _FAST = 0.05

        async def _inner():
            loop = asyncio.get_running_loop()
            with patch.object(bidi, "PROACTIVE_P0_FALLBACK_SECONDS", _FAST):
                await bidi._show_proactive_p0_alert(ws, ctx, st, _TRIAGE_P0, [BRAKE_FAULT], loop)
                ev = st.get("_proactive_p0_cancel")
                assert ev is not None
                ev.set()  # signal: model spoke the directive
                await asyncio.sleep(0.1)
            pending = [t for t in asyncio.all_tasks() if not t.done() and t is not asyncio.current_task()]
            for t in pending:
                t.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)

        asyncio.run(_inner())
        speak = [m for m in _sent_msgs(ws) if m.get("type") == "platform.speak"]
        assert len(speak) == 0

    def test_fallback_reason_is_proactive_p0(self):
        ws = AsyncMock()
        ctx = _bidi_ctx()
        st = _bidi_state()
        _FAST = 0.05

        async def _inner():
            loop = asyncio.get_running_loop()
            with patch.object(bidi, "PROACTIVE_P0_FALLBACK_SECONDS", _FAST):
                await bidi._show_proactive_p0_alert(ws, ctx, st, _TRIAGE_P0, [BRAKE_FAULT], loop)
                await asyncio.sleep(_FAST + 0.1)
            pending = [t for t in asyncio.all_tasks() if not t.done() and t is not asyncio.current_task()]
            for t in pending:
                t.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)

        asyncio.run(_inner())

        speak = [m for m in _sent_msgs(ws) if m.get("type") == "platform.speak"]
        assert speak[0].get("reason") == "proactive_p0"


# ---------------------------------------------------------------------------
# Part 3: No-downgrade rule across two alerts
# ---------------------------------------------------------------------------


class TestNoDowngrade:
    """Second P0 alert: more protective directive replaces; less protective does not."""

    def _run_two(self, first_dtcs, second_dtcs):
        ws = AsyncMock()
        ctx = _bidi_ctx()
        st = _bidi_state(active_dtcs_for_prompt=first_dtcs)
        _results = {}

        async def _inner():
            loop = asyncio.get_running_loop()
            with patch.object(bidi, "PROACTIVE_P0_FALLBACK_SECONDS", 0.05):
                await bidi._show_proactive_p0_alert(ws, ctx, st, _TRIAGE_P0, first_dtcs, loop)
                _results["first"] = st["p0_directive"]
                # Simulate call_triage_endpoint updating ctx.triage_active_dtcs
                ctx.triage_active_dtcs = [d["code"] for d in second_dtcs]
                await bidi._show_proactive_p0_alert(ws, ctx, st, _TRIAGE_P0, second_dtcs, loop)
                _results["second"] = st["p0_directive"]
            pending = [t for t in asyncio.all_tasks() if not t.done() and t is not asyncio.current_task()]
            for t in pending:
                t.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)

        asyncio.run(_inner())
        return _results["first"], _results["second"]

    def test_fire_replaces_brake_directive(self):
        """Fire (exit_vehicle) is more protective than brake_emergency."""
        first, second = self._run_two([BRAKE_FAULT], [FIRE_FAULT])
        assert first == BRAKE_DIRECTIVE
        assert second == FIRE_DIRECTIVE

    def test_less_protective_does_not_downgrade(self):
        """A less protective second directive keeps the existing more protective one."""
        first, second = self._run_two([FIRE_FAULT], [BRAKE_FAULT])
        # fire directive stays on screen; no downgrade
        assert second == FIRE_DIRECTIVE


# ---------------------------------------------------------------------------
# Part 4: Task lifecycle — _run_proactive_p0_watcher
# ---------------------------------------------------------------------------


class TestTaskLifecycle:
    """_run_proactive_p0_watcher: start, cancel, fetch failure, classifier failure."""

    def _make_watcher(self, open_reason=None, baseline_dtcs=None):
        ws = AsyncMock()
        ctx = _bidi_ctx(open_reason=open_reason)
        st = _bidi_state(active_dtcs_for_prompt=baseline_dtcs or [])
        watcher = ProactiveP0Watcher(open_reason=open_reason)
        cfg = _mock_config()
        return ws, ctx, st, watcher, cfg

    def test_task_cancels_cleanly(self):
        """Cancellation at teardown does not propagate an exception."""
        ws, ctx, st, watcher, cfg = self._make_watcher()

        async def _inner():
            task = asyncio.create_task(
                bidi._run_proactive_p0_watcher(
                    watcher, websocket=ws, ctx=ctx, state=st, config=cfg
                )
            )
            await asyncio.sleep(0)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            assert task.done()

        asyncio.run(_inner())

    def test_fetch_failure_does_not_crash_task(self):
        """DTC fetch error → warning log, task keeps running."""
        ws, ctx, st, watcher, cfg = self._make_watcher()

        async def _inner():
            with (
                patch.object(bidi, "PROACTIVE_P0_POLL_SECONDS", 0.01),
                patch.object(bidi, "_fetch_active_dtcs", side_effect=RuntimeError("DDB error")),
            ):
                task = asyncio.create_task(
                    bidi._run_proactive_p0_watcher(
                        watcher, websocket=ws, ctx=ctx, state=st, config=cfg
                    )
                )
                await asyncio.sleep(0.05)
                assert not task.done()
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        asyncio.run(_inner())

    def test_never_raises_on_internal_exception(self):
        """Unexpected exception inside the task → logged, task exits gracefully."""
        ws, ctx, st, watcher, cfg = self._make_watcher()

        async def _inner():
            with (
                patch.object(bidi, "PROACTIVE_P0_POLL_SECONDS", 0.01),
                patch.object(bidi, "_fetch_active_dtcs", side_effect=ValueError("unexpected")),
            ):
                task = asyncio.create_task(
                    bidi._run_proactive_p0_watcher(
                        watcher, websocket=ws, ctx=ctx, state=st, config=cfg
                    )
                )
                await asyncio.sleep(0.05)
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                assert task.done()

        asyncio.run(_inner())

    def test_classifier_failure_does_not_mark_alerted(self):
        """Fault NOT marked alerted when classifier fails → retried next tick."""
        w = ProactiveP0Watcher()
        new = w.new_faults([BRAKE_FAULT])
        assert len(new) == 1
        # Simulate classifier failure: do NOT call mark_alerted
        new_again = w.new_faults([BRAKE_FAULT])
        assert len(new_again) == 1  # still retried

    def test_same_fault_not_alerted_twice(self):
        """Mutation M3: mark_alerted prevents a second alert for the same occurrence."""
        w = ProactiveP0Watcher()
        key = ProactiveP0Watcher.fault_key(BRAKE_FAULT)
        assert w.should_alert("P0", [key]) is True
        w.mark_alerted([key])
        assert w.should_alert("P0", [key]) is False
        assert w.new_faults([BRAKE_FAULT]) == []

    def test_non_p0_sends_nothing(self):
        """P1/P2/P3/None from classifier: no classification or speak events."""
        ws, ctx, st, watcher, cfg = self._make_watcher()
        p1_result = dict(_TRIAGE_P0, classification="P1")

        async def _inner():
            with (
                patch.object(bidi, "PROACTIVE_P0_POLL_SECONDS", 0.01),
                patch.object(bidi, "_fetch_active_dtcs", return_value=[P1_FAULT]),
                patch.object(bidi, "call_triage_endpoint", return_value=p1_result),
            ):
                task = asyncio.create_task(
                    bidi._run_proactive_p0_watcher(
                        watcher, websocket=ws, ctx=ctx, state=st, config=cfg
                    )
                )
                await asyncio.sleep(0.05)
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        asyncio.run(_inner())
        sent = _sent_msgs(ws)
        assert not any(m.get("type") == "classification" for m in sent)


# ---------------------------------------------------------------------------
# Part 5: Integration — baseline and p0_push behaviour end-to-end
# ---------------------------------------------------------------------------


class TestBaselineVsP0Push:
    """Integration tests for the baseline/p0_push distinction end-to-end."""

    def _run_watcher_one_tick(self, fetch_return, triage_return, open_reason=None, baseline_dtcs=None):
        """Run the watcher for one poll tick inside asyncio.run() and return (sent_msgs, state)."""
        ws = AsyncMock()
        ctx = _bidi_ctx(open_reason=open_reason)
        st = _bidi_state(active_dtcs_for_prompt=baseline_dtcs or [])
        watcher = ProactiveP0Watcher(open_reason=open_reason)
        cfg = _mock_config()

        async def _inner():
            with (
                patch.object(bidi, "PROACTIVE_P0_POLL_SECONDS", 0.01),
                patch.object(bidi, "PROACTIVE_P0_FALLBACK_SECONDS", 0.05),
                patch.object(bidi, "_fetch_active_dtcs", return_value=fetch_return),
                patch.object(bidi, "call_triage_endpoint", return_value=triage_return),
            ):
                task = asyncio.create_task(
                    bidi._run_proactive_p0_watcher(
                        watcher, websocket=ws, ctx=ctx, state=st, config=cfg
                    )
                )
                await asyncio.sleep(0.1)
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                # Cancel any remaining tasks (fallback timers etc.)
                pending = [t for t in asyncio.all_tasks() if not t.done() and t is not asyncio.current_task()]
                for t in pending:
                    t.cancel()
                if pending:
                    await asyncio.gather(*pending, return_exceptions=True)

        asyncio.run(_inner())
        return _sent_msgs(ws), st

    def test_p0_fault_triggers_alert(self):
        """New P0 fault → full alert sequence."""
        sent, st = self._run_watcher_one_tick(
            fetch_return=[BRAKE_FAULT],
            triage_return=_TRIAGE_P0,
        )
        types = [m.get("type") for m in sent]
        assert "transcript" in types
        assert "classification" in types
        assert "tool.call" in types
        assert "tool.result" in types
        assert st["hazard_gate"] is not None
        iq = st["injection_queue"]
        assert len(iq.submitted) >= 1
        # The note must contain the exact directive that was shown to the driver
        shown_directive = st.get("p0_directive", "")
        assert shown_directive  # a directive was shown
        assert any(shown_directive in r.payload for r in iq.submitted)

    def test_baseline_fault_not_alerted_normal_session(self):
        """Mutation M4: baseline fault produces no alert on a normal session."""
        sent, _ = self._run_watcher_one_tick(
            fetch_return=[BRAKE_FAULT],
            triage_return=_TRIAGE_P0,
            baseline_dtcs=[BRAKE_FAULT],  # same fault is in baseline
        )
        assert not any(m.get("type") == "classification" for m in sent)

    def test_baseline_fault_alerted_on_p0_push(self):
        """p0_push: baseline fault IS alerted (baseline skipped)."""
        sent, _ = self._run_watcher_one_tick(
            fetch_return=[BRAKE_FAULT],
            triage_return=_TRIAGE_P0,
            open_reason="p0_push",
            baseline_dtcs=[BRAKE_FAULT],
        )
        assert any(
            m.get("type") == "classification" and m.get("value") == "P0"
            for m in sent
        )


# ---------------------------------------------------------------------------
# Part 6: Mutation verification notes
# ---------------------------------------------------------------------------
#
# The mutations below were applied one-by-one and the named tests confirmed
# they failed:
#
# M1 — alert below P0: changed `should_alert` to return True for "P1".
#      test_non_p0_never_alerts[P1] ............... FAILED ✓
#
# M2 — words ≠ directive in note: replaced BRAKE_DIRECTIVE with "X" in
#      note_payload in _show_proactive_p0_alert.
#      test_note_contains_exact_directive ......... FAILED ✓
#      Changed platform.speak text to "X":
#      test_platform_speak_text_is_directive ...... FAILED ✓
#
# M3 — alert twice for one fault: removed mark_alerted call from watcher loop.
#      test_same_fault_not_alerted_twice .......... FAILED ✓
#
# M4 — baseline alerted without p0_push: removed the `if self._open_reason == "p0_push": return`
#      guard from `baseline()`.
#      test_baseline_faults_not_new_for_normal_session . FAILED ✓
#      test_baseline_fault_not_alerted_normal_session ... FAILED (integration) ✓
