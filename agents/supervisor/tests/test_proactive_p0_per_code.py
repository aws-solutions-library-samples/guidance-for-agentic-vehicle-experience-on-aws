"""F1.4 (W2): per-code severity-table check in _run_proactive_p0_watcher.

Rule implemented in bidi_app._run_proactive_p0_watcher (spec
2026-11-05-avx-proactive-p0-alert, review Cycle 1 W2):

  * Alert only when the classifier returns P0 AND at least one new code is P0
    by the classifier's own severity table (first_reply_lead.implied_level).
  * After the classifier answers, mark new faults that are NOT P0 per code as
    seen so they are never sent to /triage again.
  * If the classifier says non-P0 while a new code IS P0 per table, leave the
    key unmarked and retry; after 3 non-P0 answers mark it seen with WARNING.
  * If the table is unavailable (empty mapping), fall back to the classifier
    verdict alone and log once "proactive-p0: severity table unavailable".
  * Classifier failure: unchanged — not marked, retried.

Tests run the real _run_proactive_p0_watcher for 3+ ticks.
_fetch_active_dtcs and call_triage_endpoint are patched.

Mutant verification (performed in /tmp/p0-f14/, each against a git archive
copy with only that mutant applied):

  MUT1 — per-code check removed (treat any P0 classifier verdict as alert).
           Caught by: test_p1_by_table_plus_p0_classifier_no_alert
  MUT2 — non-P0-by-table faults NOT marked seen.
           Caught by: test_non_p0_by_table_not_sent_to_triage_again
  MUT3 — severity table replaced by a hardcoded {"B1234": ...} mapping.
           Caught by: test_p0_by_table_new_c1234_alerts_once
  MUT4 — empty-table fallback removed (fall back to classifier check removed).
           Caught by: test_table_unavailable_alerts_on_classifier_p0
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import agents.supervisor.bidi_app as bidi
from agents.supervisor.proactive_p0 import ProactiveP0Watcher
from agents.supervisor.dtc_response_catalog import p0_directive

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

# C1234: critical + no → P0 by table (dtc_severity.yaml line 36)
C1234_FAULT = {
    "code": "C1234",
    "severity": "CRITICAL",
    "template_id": "brake_emergency",
    "firstSeenAt": 1_000_000,
}
# B0001: high + limited → P1 by table (dtc_severity.yaml: "Airbag / SRS fault")
# Used as a "new P1-by-table fault" fixture (the task description calls this "B1234").
P1_TABLE_FAULT = {
    "code": "B0001",
    "severity": "HIGH",
    "template_id": "airbag_warning",
    "firstSeenAt": 2_000_000,
}
# P0299: high + limited → P1 by table
P1_FAULT = {
    "code": "P0299",
    "severity": "HIGH",
    "template_id": "pullover_safe_location",
    "firstSeenAt": 3_000_000,
}

_TRIAGE_P0 = {
    "classification": "P0",
    "sessionId": "s-f14",
    "decidedAt": "2026-11-06T10:00:00Z",
    "latencyMs": 80,
    "triageDecisionId": "t-f14",
}
_TRIAGE_P1 = dict(_TRIAGE_P0, classification="P1")

_POLL = 0.01   # 10 ms between ticks in all tests
_FB = 0.05     # 50 ms fallback


class _Queue:
    """Minimal injection-queue stub."""

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
        session_id="s-f14",
        vin="VIN-F14",
        vehicle_id="VEH-F14",
        tenant_id="meridian",
        driver_id="DRV-F14",
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
    base: dict = {
        "platform_note_tag": "f14test",
        "event_count": 0,
        "type_counts": {},
        "surfaced_classification": None,
        "last_classification": None,
        "active_dtcs_for_prompt": [C1234_FAULT],  # baseline
        "hazard_gate": None,
        "p0_directive": None,
        "p0_directive_template": None,
        "p0_directive_offered": {},
        "injection_queue": iq,
        "vehicle_ctx": {},
        "outbound_audio_chunks": 0,
        "tool_use_names": {},
        "escalation_tool_in_flight_at": None,
        "auto_triage_fired": True,
        "awaiting_confirmation_category": None,
        "_narration_user_turn": 0,
        "_narration_turn_text": "",
        "_narration_corrected_turn": None,
        "primed": True,
    }
    base.update(extra)
    return base


def _run_watcher(
    *,
    fetch_dtcs: list,
    triage_result: dict,
    baseline_dtcs: list | None = None,
    run_ticks: int = 3,
    extra_wait_s: float = 0.05,
) -> tuple[list[dict], dict]:
    """Run the real _run_proactive_p0_watcher for `run_ticks` ticks and return
    (sent_msgs, state).  _fetch_active_dtcs and call_triage_endpoint are patched.
    """
    ws = AsyncMock()
    ctx = _ctx()
    st = _state(active_dtcs_for_prompt=baseline_dtcs if baseline_dtcs is not None else [C1234_FAULT])
    watcher = ProactiveP0Watcher()
    cfg = MagicMock()
    cfg.api_base = "https://test.example.com"

    triage_mock = MagicMock(return_value=triage_result)

    async def _inner() -> None:
        with (
            patch.object(bidi, "PROACTIVE_P0_POLL_SECONDS", _POLL),
            patch.object(bidi, "PROACTIVE_P0_FALLBACK_SECONDS", _FB),
            patch.object(bidi, "_fetch_active_dtcs", return_value=fetch_dtcs),
            patch.object(bidi, "call_triage_endpoint", triage_mock),
        ):
            task = asyncio.create_task(
                bidi._run_proactive_p0_watcher(
                    watcher, websocket=ws, ctx=ctx, state=st, config=cfg,
                )
            )
            await asyncio.sleep(run_ticks * _POLL + _FB + extra_wait_s)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            pending = [
                t for t in asyncio.all_tasks()
                if not t.done() and t is not asyncio.current_task()
            ]
            for t in pending:
                t.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)

    asyncio.run(_inner())
    return [c.args[0] for c in ws.send_json.call_args_list], st, triage_mock


# ---------------------------------------------------------------------------
# Test 1: Baseline C1234 + new B1234 (P1 by table) + classifier P0 → no alert
# ---------------------------------------------------------------------------

class TestBaselineP0PlusNewP1TableClassifierP0:
    """W2 case 1: vehicle has a standing P0 (C1234 in baseline) and a new HIGH
    fault (B0001, P1 by table) arrives.  The classifier grades the whole vehicle
    P0 (because of the standing C1234).  B0001 is P1 by the per-code table.

    Expected: no alert (B0001 is not P0 per code), triage called exactly once
    (B0001 marked seen after the first answer), no classification/note/speak.
    """

    def test_p1_by_table_plus_p0_classifier_no_alert(self) -> None:
        """MUT1 target: removing the per-code check would alert here."""
        msgs, st, triage_mock = _run_watcher(
            fetch_dtcs=[C1234_FAULT, P1_TABLE_FAULT],
            triage_result=_TRIAGE_P0,
            baseline_dtcs=[C1234_FAULT],  # C1234 is in baseline
        )
        classification_events = [m for m in msgs if m.get("type") == "classification"]
        speak_events = [m for m in msgs if m.get("type") == "platform.speak"]
        iq = st["injection_queue"]
        immediate_notes = [r for r in iq.submitted if r.priority == bidi.InjectionPriority.IMMEDIATE]

        assert len(classification_events) == 0, (
            f"Expected 0 classification events (B1234 is P1 by table, not P0); "
            f"got {len(classification_events)}. MUT1 (per-code check removed) allows alert."
        )
        assert len(speak_events) == 0
        assert len(immediate_notes) == 0
        assert triage_mock.call_count == 1, (
            f"Triage called {triage_mock.call_count} times; expected exactly 1 "
            "(B1234 should be marked seen after the first non-P0-by-table answer)."
        )

    def test_non_p0_by_table_not_sent_to_triage_again(self) -> None:
        """MUT2 target: if non-P0 faults are NOT marked seen they poll every tick."""
        msgs, st, triage_mock = _run_watcher(
            fetch_dtcs=[P1_TABLE_FAULT],    # P1 by table only, no baseline
            triage_result=_TRIAGE_P0,        # classifier says P0 (hypothetical)
            baseline_dtcs=[],
            run_ticks=5,
        )
        # B1234 is P1 by table → first tick marks it seen, subsequent ticks have
        # new_faults = [] → no more triage calls.
        assert triage_mock.call_count == 1, (
            f"Triage called {triage_mock.call_count} times; expected 1. "
            "MUT2 (non-P0 not marked seen) allows repeated triage calls."
        )
        assert not any(m.get("type") == "classification" for m in msgs)


# ---------------------------------------------------------------------------
# Test 2: New P1 fault (B1234) + classifier P1 → triage called once
# ---------------------------------------------------------------------------

class TestNewP1FaultClassifierP1:
    """New B0001 (P1 by table: high+limited), classifier returns P1.  Triage
    called once, B0001 marked seen, no alert.
    """

    def test_new_p1_fault_classifier_p1_no_alert(self) -> None:
        msgs, st, triage_mock = _run_watcher(
            fetch_dtcs=[P1_TABLE_FAULT],
            triage_result=_TRIAGE_P1,
            baseline_dtcs=[],
            run_ticks=4,
        )
        assert not any(m.get("type") == "classification" for m in msgs)
        assert not any(m.get("type") == "platform.speak" for m in msgs)
        # B1234 is P1 by table → marked seen after first classifier answer.
        assert triage_mock.call_count == 1, (
            f"Triage called {triage_mock.call_count} times; expected 1."
        )


# ---------------------------------------------------------------------------
# Test 3: New C1234 (P0 by table) + classifier P0 → alerts once
# ---------------------------------------------------------------------------

class TestNewC1234AlertsOnce:
    """New C1234 (P0 by table), classifier P0 → exactly one alert.
    MUT3 target: replacing the table with {"B1234": ...} would make C1234 look
    non-P0-by-table and suppress the alert.
    """

    def test_p0_by_table_new_c1234_alerts_once(self) -> None:
        """MUT3 target: a hardcoded table covering only B1234 misses C1234."""
        msgs, st, triage_mock = _run_watcher(
            fetch_dtcs=[C1234_FAULT],
            triage_result=_TRIAGE_P0,
            baseline_dtcs=[],    # C1234 not in baseline → it is new
            run_ticks=3,
        )
        classification_events = [m for m in msgs if m.get("type") == "classification"]
        speak_events = [m for m in msgs if m.get("type") == "platform.speak"]
        iq = st["injection_queue"]
        immediate_notes = [r for r in iq.submitted if r.priority == bidi.InjectionPriority.IMMEDIATE]

        assert len(classification_events) == 1, (
            f"Expected exactly 1 classification event for new C1234/P0; "
            f"got {len(classification_events)}. MUT3 (hardcoded table) suppresses the alert."
        )
        assert len(speak_events) == 1, (
            f"Expected 1 platform.speak; got {len(speak_events)}."
        )
        assert len(immediate_notes) == 1, (
            f"Expected 1 IMMEDIATE note; got {len(immediate_notes)}."
        )
        assert triage_mock.call_count == 1, (
            f"Expected triage called once; got {triage_mock.call_count}."
        )


# ---------------------------------------------------------------------------
# Test 4: Table unavailable → falls back to classifier P0 and alerts
# ---------------------------------------------------------------------------

class TestTableUnavailableFallback:
    """When load_severity_table returns {} (empty), the watcher falls back to
    the classifier verdict alone.  A classifier P0 triggers the alert.

    MUT4 target: if the empty-table fallback is removed, the alert is suppressed.
    """

    def test_table_unavailable_alerts_on_classifier_p0(self) -> None:
        """MUT4 target: removing the empty-table fallback silences the alert."""
        msgs, st, triage_mock = _run_watcher(
            fetch_dtcs=[C1234_FAULT],
            triage_result=_TRIAGE_P0,
            baseline_dtcs=[],
            run_ticks=3,
            # Patch _p0_load_severity_table to return {} inside the run helper.
        )
        # Without table patching, the real table is used and C1234 is P0 → alert.
        # Re-run with table patched to empty to test the fallback path.
        ws2 = AsyncMock()
        ctx2 = _ctx()
        st2 = _state(active_dtcs_for_prompt=[])
        watcher2 = ProactiveP0Watcher()
        cfg2 = MagicMock()
        cfg2.api_base = "https://test.example.com"
        triage_mock2 = MagicMock(return_value=_TRIAGE_P0)

        async def _inner() -> None:
            with (
                patch.object(bidi, "PROACTIVE_P0_POLL_SECONDS", _POLL),
                patch.object(bidi, "PROACTIVE_P0_FALLBACK_SECONDS", _FB),
                patch.object(bidi, "_fetch_active_dtcs", return_value=[C1234_FAULT]),
                patch.object(bidi, "call_triage_endpoint", triage_mock2),
                patch.object(bidi, "_p0_load_severity_table", return_value={}),
            ):
                task = asyncio.create_task(
                    bidi._run_proactive_p0_watcher(
                        watcher2, websocket=ws2, ctx=ctx2, state=st2, config=cfg2,
                    )
                )
                await asyncio.sleep(3 * _POLL + _FB + 0.05)
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                pending = [
                    t for t in asyncio.all_tasks()
                    if not t.done() and t is not asyncio.current_task()
                ]
                for t in pending:
                    t.cancel()
                if pending:
                    await asyncio.gather(*pending, return_exceptions=True)

        asyncio.run(_inner())
        sent2 = [c.args[0] for c in ws2.send_json.call_args_list]
        classification_events = [m for m in sent2 if m.get("type") == "classification"]
        speak_events = [m for m in sent2 if m.get("type") == "platform.speak"]

        assert len(classification_events) == 1, (
            f"Expected 1 classification event when table unavailable + classifier P0; "
            f"got {len(classification_events)}. "
            "MUT4 (fallback removed) suppresses the alert."
        )
        assert len(speak_events) == 1, (
            f"Expected 1 platform.speak in table-unavailable fallback; got {len(speak_events)}."
        )
        assert triage_mock2.call_count == 1


class TestTableUnavailableNoRepeatTriage:
    """With the table unreadable, a fault the classifier answered non-P0 is not
    re-sent to /triage every tick (architect follow-up to F1.4, review W2 part 2)."""

    def test_table_unavailable_non_p0_not_sent_to_triage_again(self) -> None:
        ws = AsyncMock()
        ctx = _ctx()
        st = _state(active_dtcs_for_prompt=[])
        watcher = ProactiveP0Watcher()
        cfg = MagicMock()
        cfg.api_base = "https://test.example.com"
        triage_mock = MagicMock(return_value=dict(_TRIAGE_P0, classification="P1"))

        async def _inner() -> None:
            with (
                patch.object(bidi, "PROACTIVE_P0_POLL_SECONDS", _POLL),
                patch.object(bidi, "PROACTIVE_P0_FALLBACK_SECONDS", _FB),
                patch.object(bidi, "_fetch_active_dtcs", return_value=[C1234_FAULT]),
                patch.object(bidi, "call_triage_endpoint", triage_mock),
                patch.object(bidi, "_p0_load_severity_table", return_value={}),
            ):
                task = asyncio.create_task(
                    bidi._run_proactive_p0_watcher(
                        watcher, websocket=ws, ctx=ctx, state=st, config=cfg,
                    )
                )
                await asyncio.sleep(4 * _POLL + 0.05)
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        asyncio.run(_inner())
        sent = [c.args[0] for c in ws.send_json.call_args_list]
        assert not any(m.get("type") == "classification" for m in sent)
        assert triage_mock.call_count == 1, (
            f"/triage called {triage_mock.call_count} times for one non-P0 fault"
        )


# ---------------------------------------------------------------------------
# Test 6 (review cycle 2 C1, Fix Group 2 F2.1): P0 by table, classifier says P1
# ---------------------------------------------------------------------------

class TestTableP0ClassifierP1GivesUpAfterThree:
    """The classifier is the only P0 authority, also on the per-code branch.

    A new C1234 (P0 by the severity table) whose classifier answer is P1 on every
    tick must never alert: no classification event, no IMMEDIATE note, no
    ``platform.speak``. It is retried, and after 3 non-P0 answers it is marked seen
    with one WARNING, so ``/triage`` is called exactly 3 times however many ticks run.

    The run waits on the number of DTC fetches (one per tick), not on wall-clock
    ticks, so a slow machine can't make it pass or fail by timing.

    Mutants (review cycle 2, each caught; decisions.md "F2.1"):
      MC  table branch ``if classification == "P0":`` -> ``if True:``  (speaks on a P1)
      MD  the 3-answer give-up never fires                           (/triage every tick)
      ME  give up on the first non-P0 answer                          (no retry)
    """

    _TICKS = 8

    def _run(self, caplog) -> tuple[list[dict], dict, MagicMock, int]:
        import logging
        import threading

        ws = AsyncMock()
        ctx = _ctx()
        st = _state(active_dtcs_for_prompt=[])  # C1234 not in the baseline: it is new
        watcher = ProactiveP0Watcher()
        cfg = MagicMock()
        cfg.api_base = "https://test.example.com"
        triage_mock = MagicMock(return_value=_TRIAGE_P1)
        fetches: list[int] = []
        lock = threading.Lock()

        def _fetch(*_a, **_k):
            with lock:
                fetches.append(1)
            return [C1234_FAULT]

        async def _inner() -> None:
            with (
                patch.object(bidi, "PROACTIVE_P0_POLL_SECONDS", _POLL),
                patch.object(bidi, "PROACTIVE_P0_FALLBACK_SECONDS", _FB),
                patch.object(bidi, "_fetch_active_dtcs", side_effect=_fetch),
                patch.object(bidi, "call_triage_endpoint", triage_mock),
            ):
                task = asyncio.create_task(
                    bidi._run_proactive_p0_watcher(
                        watcher, websocket=ws, ctx=ctx, state=st, config=cfg,
                    )
                )
                deadline = asyncio.get_running_loop().time() + 60.0
                while len(fetches) < self._TICKS + 1:
                    if asyncio.get_running_loop().time() > deadline:
                        break
                    await asyncio.sleep(_POLL)
                await asyncio.sleep(_FB + 0.05)  # let any fallback fire
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                pending = [
                    t for t in asyncio.all_tasks()
                    if not t.done() and t is not asyncio.current_task()
                ]
                for t in pending:
                    t.cancel()
                if pending:
                    await asyncio.gather(*pending, return_exceptions=True)

        with caplog.at_level(logging.WARNING):
            asyncio.run(_inner())
        return [c.args[0] for c in ws.send_json.call_args_list], st, triage_mock, len(fetches)

    def test_table_p0_classifier_p1_never_alerts_and_gives_up_after_three(self, caplog) -> None:
        msgs, st, triage_mock, n_fetches = self._run(caplog)

        assert n_fetches >= self._TICKS + 1, f"only {n_fetches} ticks ran; the test proves nothing"
        assert not [m for m in msgs if m.get("type") == "classification"], (
            "a P1 classifier answer must never produce a P0 classification (MC: table overrides classifier)"
        )
        assert not [m for m in msgs if m.get("type") == "platform.speak"]
        assert not [m for m in msgs if m.get("type") == "transcript"]
        iq = st["injection_queue"]
        assert not [r for r in iq.submitted if r.priority == bidi.InjectionPriority.IMMEDIATE]
        assert triage_mock.call_count == 3, (
            f"/triage called {triage_mock.call_count} times over {n_fetches} ticks; expected 3 "
            "(MD: never gives up -> every tick; ME: gives up after 1)"
        )
        warnings = [
            r for r in caplog.records
            if r.levelname == "WARNING" and "is P0 by table but classifier returned non-P0" in r.getMessage()
        ]
        assert len(warnings) == 1, f"expected one give-up WARNING, got {len(warnings)}"
        assert "code=C1234" in warnings[0].getMessage()
