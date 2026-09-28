"""Task 3.2 — the driver's tap on the escalation-recommendation affordance.

Spec `2026-09-23-cvx-escalation-recommend-not-act`, D3: the human chat and the
roadside request are **user-confirmed, one tap each**. iOS sends
``{"type": "escalation.request", "intent": "human"|"roadside"}`` over the bidi
WebSocket and the server decides everything else.

WHY THE TAP GOES OVER THE WEBSOCKET, NOT `POST /escalate`
---------------------------------------------------------
Task 3.2's Accept 1 asks for a tap that reaches `/escalate` "with no
client-supplied severity". `/escalate` rejects a body without `severity`
(`lambdas/api-escalate/handler.py`: *"severity must be one of P0, P1, P2, P3"*),
and the Lambda holds no session state to derive one from. So a direct POST from
iOS could only work by the phone asserting the level — the exact hole this spec
closes, moved from the LLM to the client. The only party that can supply a
severity without anyone asserting it is this server, which holds the verdict it
surfaced. See `decisions.md` § "Task 3.2: the tap goes over the session".

WHAT IS PINNED
--------------
1. Severity sent to /escalate == the verdict the server SURFACED, for two
   distinct verdicts, and a client-named `severity` is never read.
2. Fail closed: unknown intent, no classifier verdict, a surfaced P2/P3, and
   roadside on anything but P0 each refuse with a `failed` escalation event and
   make no /escalate call.
3. `human` sends an explicit `dispatch_roadside=False`; `roadside` sends `True`.
4. A request in flight makes a second tap a no-op (no second Connect contact).
5. Every classification emit site records `surfaced_classification` (AST).
6. The auto path still omits `dispatch_roadside` (review C1's property; its
   guard moved here because the call now goes through a parameter).
7. The reasoning-drawer mirror reports what was sent, not a hardcoded truck
   (review C2W6 / security W3).
"""

from __future__ import annotations

import ast
import asyncio
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

import agents.supervisor.bidi_app as bidi
from agents.supervisor.config import Config
from agents.supervisor.session import SessionContext

_BIDI_APP_PATH = Path(__file__).resolve().parent.parent / "bidi_app.py"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def config() -> Config:
    return Config.from_env()


@pytest.fixture
def ctx(config: Config) -> SessionContext:
    c = SessionContext(
        tenant_id=config.tenant_id,
        session_id="vsa-esc-req-test-001",
        jwt="fake-jwt-not-used",
        persona_id="fleet_driver",
        vin="1HGBH41JXMN000046",
        vehicle_id="VEH-0025",
    )
    # A classifier verdict exists on the session (set by triage.py in real
    # runs). The SURFACED tier is set per test on `state`.
    c.triage_classification = "P3"
    return c


def _state(surfaced: str | None) -> dict:
    return {
        "surfaced_classification": surfaced,
        "escalation_request_task": None,
        "active_dtcs_for_prompt": [],
    }


def _fake_escalate_response(severity: str, rsa: bool = False) -> dict:
    return {
        "contactId": "cnt-abc-123",
        "participantId": "par-abc-123",
        "participantToken": "tok-abc-123",
        "connectionExpiry": "2099-01-01T00:00:00Z",
        "severity": severity,
        "rsaDispatched": rsa,
        "rsaActionId": "rsa-1" if rsa else None,
        "summary": "connected",
    }


def _sent(ws: AsyncMock) -> list[dict]:
    return [c.args[0] for c in ws.send_json.call_args_list]


def _run_request(ctx, config, state, msg):
    """Invoke the handler and drain any background task it starts.

    Returns (captured call_escalate_endpoint kwargs, messages sent to client).
    """
    ws = AsyncMock()
    captured: list[dict] = []

    def _spy(*_args, **kwargs):
        captured.append(kwargs)
        return _fake_escalate_response(
            kwargs["severity"], rsa=bool(kwargs.get("dispatch_roadside"))
        )

    async def _go():
        with patch.object(bidi, "call_escalate_endpoint", side_effect=_spy):
            task = await bidi._handle_escalation_request(
                websocket=ws, ctx=ctx, config=config, state=state, msg=msg,
            )
            if task is not None:
                await task
        return task

    asyncio.run(_go())
    return captured, _sent(ws)


# ---------------------------------------------------------------------------
# 1. Severity is the surfaced verdict, never the client's
# ---------------------------------------------------------------------------


class TestSeverityIsServerAuthored:
    def test_p0_surfaced_sends_p0(self, ctx, config):
        calls, _ = _run_request(ctx, config, _state("P0"), {"type": "escalation.request", "intent": "human"})
        assert len(calls) == 1
        assert calls[0]["severity"] == "P0"

    def test_p1_surfaced_sends_p1(self, ctx, config):
        """Distinct verdict, so a hardcoded "P0" cannot pass both tests."""
        calls, _ = _run_request(ctx, config, _state("P1"), {"type": "escalation.request", "intent": "human"})
        assert len(calls) == 1
        assert calls[0]["severity"] == "P1"

    def test_client_named_severity_is_ignored(self, ctx, config):
        """A client asserting P0 while the server surfaced P1 gets P1.

        Mutation: read `msg.get("severity")` in the handler → this test FAILS.
        """
        msg = {
            "type": "escalation.request",
            "intent": "human",
            "severity": "P0",
            "dispatchRoadside": True,
        }
        calls, _ = _run_request(ctx, config, _state("P1"), msg)
        assert len(calls) == 1
        assert calls[0]["severity"] == "P1"
        assert calls[0]["dispatch_roadside"] is False

    def test_driver_confirmed_tier_is_honoured(self, ctx, config):
        """ctx still holds the classifier's P3; the server surfaced P0 after
        the driver confirmed. The tap escalates at the surfaced tier, which is
        the verdict the card was rendered for."""
        ctx.triage_classification = "P3"
        calls, _ = _run_request(ctx, config, _state("P0"), {"type": "escalation.request", "intent": "human"})
        assert calls[0]["severity"] == "P0"

    def test_handler_never_reads_severity_from_the_message(self):
        """Structural: the handler contains no `"severity"` / `"dispatchRoadside"`
        key literal at all, so it cannot read one from the client's message."""
        tree = ast.parse(_BIDI_APP_PATH.read_text(encoding="utf-8"))
        fn = next(
            n for n in ast.walk(tree)
            if isinstance(n, ast.AsyncFunctionDef) and n.name == "_handle_escalation_request"
        )
        offending = [
            node.value for node in ast.walk(fn)
            if isinstance(node, ast.Constant) and node.value in ("severity", "dispatchRoadside")
        ]
        assert not offending, (
            f"_handle_escalation_request references {offending!r}; the tap must "
            "never read a client-supplied severity or dispatch flag"
        )


# ---------------------------------------------------------------------------
# 2. Fail closed
# ---------------------------------------------------------------------------


class TestFailClosed:
    @pytest.mark.parametrize("intent", [None, "", "chat", "HUMAN", "roadside ", 1])
    def test_unknown_intent_refused(self, ctx, config, intent):
        calls, sent = _run_request(ctx, config, _state("P0"), {"type": "escalation.request", "intent": intent})
        assert calls == []
        assert sent and sent[-1]["type"] == "escalation" and sent[-1]["status"] == "failed"

    def test_no_classifier_verdict_refused(self, ctx, config):
        """Spec D2: no classifier run, no severity-bearing escalation — even if
        state somehow carries a surfaced tier.

        Mutation: delete the `ctx.triage_classification is None` check → FAILS.
        """
        ctx.triage_classification = None
        calls, sent = _run_request(ctx, config, _state("P0"), {"type": "escalation.request", "intent": "human"})
        assert calls == []
        assert sent[-1]["status"] == "failed"

    @pytest.mark.parametrize("tier", [None, "P2", "P3", "", "p0"])
    def test_non_recommended_tier_refused(self, ctx, config, tier):
        """Mutation: widen ESCALATION_RECOMMENDED_TIERS or drop the check → FAILS."""
        calls, sent = _run_request(ctx, config, _state(tier), {"type": "escalation.request", "intent": "human"})
        assert calls == []
        assert sent[-1]["status"] == "failed"

    def test_roadside_on_p1_refused(self, ctx, config):
        """Mutation: remove the `roadside and tier != "P0"` check → FAILS."""
        calls, sent = _run_request(ctx, config, _state("P1"), {"type": "escalation.request", "intent": "roadside"})
        assert calls == []
        assert sent[-1]["status"] == "failed"

    def test_refusals_carry_no_credentials(self, ctx, config):
        ctx.triage_classification = None
        _, sent = _run_request(ctx, config, _state("P0"), {"type": "escalation.request", "intent": "human"})
        assert "participantToken" not in sent[-1]
        assert "contactId" not in sent[-1]


# ---------------------------------------------------------------------------
# 3. Roadside is its own tap
# ---------------------------------------------------------------------------


class TestRoadsideIsSeparate:
    def test_human_sends_explicit_false(self, ctx, config):
        """Explicit False, not omitted: an omitted flag would let a tenant's
        `autoRequestRoadsideOnP0: true` order a truck for a driver who only
        asked for a person.

        Mutation: pass `dispatch_roadside=None` for the human intent → FAILS.
        """
        calls, _ = _run_request(ctx, config, _state("P0"), {"type": "escalation.request", "intent": "human"})
        assert calls[0]["dispatch_roadside"] is False

    def test_roadside_on_p0_sends_true(self, ctx, config):
        calls, sent = _run_request(ctx, config, _state("P0"), {"type": "escalation.request", "intent": "roadside"})
        assert len(calls) == 1
        assert calls[0]["dispatch_roadside"] is True
        assert calls[0]["severity"] == "P0"
        initiated = [m for m in sent if m.get("type") == "escalation"]
        assert initiated and initiated[0]["status"] == "initiated"
        assert initiated[0]["rsaDispatched"] is True


# ---------------------------------------------------------------------------
# 4. Double-tap
# ---------------------------------------------------------------------------


class TestInFlightGuard:
    def test_second_tap_while_in_flight_is_a_noop(self, ctx, config):
        """Mutation: delete the in-flight check → two contacts → FAILS."""
        state = _state("P0")
        ws = AsyncMock()
        calls: list[dict] = []
        release = asyncio.Event()

        async def _go():
            loop = asyncio.get_running_loop()

            def _slow_spy(*_a, **kw):
                calls.append(kw)
                # Block the executor thread until the second tap has been handled.
                fut = asyncio.run_coroutine_threadsafe(release.wait(), loop)
                fut.result(timeout=5)
                return _fake_escalate_response(kw["severity"])

            with patch.object(bidi, "call_escalate_endpoint", side_effect=_slow_spy):
                msg = {"type": "escalation.request", "intent": "human"}
                first = await bidi._handle_escalation_request(
                    websocket=ws, ctx=ctx, config=config, state=state, msg=msg,
                )
                await asyncio.sleep(0.05)
                second = await bidi._handle_escalation_request(
                    websocket=ws, ctx=ctx, config=config, state=state, msg=msg,
                )
                release.set()
                await first
                return first, second

        first, second = asyncio.run(_go())
        assert first is not None
        assert second is None
        assert len(calls) == 1

    def test_finished_request_does_not_block_retry(self, ctx, config):
        state = _state("P0")
        msg = {"type": "escalation.request", "intent": "human"}
        calls1, _ = _run_request(ctx, config, state, msg)
        calls2, _ = _run_request(ctx, config, state, msg)
        assert len(calls1) == 1 and len(calls2) == 1


# ---------------------------------------------------------------------------
# 5. Every emit site records what it surfaced
# ---------------------------------------------------------------------------


def _is_classification_dict(node: ast.AST) -> bool:
    if not isinstance(node, ast.Dict):
        return False
    for k, v in zip(node.keys, node.values):
        if (
            isinstance(k, ast.Constant) and k.value == "type"
            and isinstance(v, ast.Constant) and v.value == "classification"
        ):
            return True
    return False


def _assigns_surfaced(fn: ast.AST) -> bool:
    for node in ast.walk(fn):
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if (
                    isinstance(t, ast.Subscript)
                    and isinstance(t.slice, ast.Constant)
                    and t.slice.value == "surfaced_classification"
                ):
                    return True
    return False


class TestSurfacedClassificationIsRecordedAtEveryEmitSite:
    def _emit_site_functions(self) -> list[ast.AST]:
        tree = ast.parse(_BIDI_APP_PATH.read_text(encoding="utf-8"))
        fns = []
        for fn in ast.walk(tree):
            if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                # Only direct bodies: nested functions are checked on their own.
                own = [
                    n for n in ast.walk(fn)
                    if _is_classification_dict(n)
                ]
                if own:
                    fns.append((fn, len(own)))
        # Keep the innermost function per site (nested defs repeat in outer walks).
        innermost = []
        for fn, _count in fns:
            nested = [
                g for g, _ in fns
                if g is not fn and any(n is g for n in ast.walk(fn))
            ]
            if not nested:
                innermost.append(fn)
        return innermost

    def test_anti_vacuity_floor(self):
        """Three emit sites exist today; the walk must find at least that many
        functions holding them, or every assertion below passes over nothing."""
        tree = ast.parse(_BIDI_APP_PATH.read_text(encoding="utf-8"))
        sites = [n for n in ast.walk(tree) if _is_classification_dict(n)]
        assert len(sites) >= 3

    def test_every_emitting_function_records_the_surfaced_value(self):
        """Mutation: delete any one `state["surfaced_classification"] = ...`
        next to an emit site → the tap for that source is refused → FAILS here
        structurally, before a driver ever finds out."""
        fns = self._emit_site_functions()
        assert len(fns) >= 2, "expected the output callable and the confirmation handler"
        missing = [getattr(f, "name", "?") for f in fns if not _assigns_surfaced(f)]
        assert not missing, f"emit sites that do not record surfaced_classification: {missing}"

    def test_assignment_count_matches_emit_sites(self):
        """One assignment per emit site — catches deleting one of the two
        emits that share `_handle_confirmation_response`."""
        src = _BIDI_APP_PATH.read_text(encoding="utf-8")
        tree = ast.parse(src)
        sites = sum(1 for n in ast.walk(tree) if _is_classification_dict(n))
        assigns = 0
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                for t in node.targets:
                    if (
                        isinstance(t, ast.Subscript)
                        and isinstance(t.slice, ast.Constant)
                        and t.slice.value == "surfaced_classification"
                    ):
                        assigns += 1
        assert assigns == sites, f"{sites} emit sites but {assigns} surfaced_classification writes"


# ---------------------------------------------------------------------------
# 6. The auto path still leaves roadside to the tenant flag (review C1)
# ---------------------------------------------------------------------------


class TestAutoPathOmitsDispatchRoadside:
    def test_auto_caller_passes_no_dispatch_roadside(self):
        """C1's guard used to spy on the call inside `_run_escalate_in_background`.
        The value now arrives as a parameter, so the property lives at the auto
        caller: its `_run_escalate_in_background(...)` call must not set it.

        Mutation: add `dispatch_roadside=True` to the call in
        `_auto_triage_and_inject` → FAILS.
        """
        tree = ast.parse(_BIDI_APP_PATH.read_text(encoding="utf-8"))
        fn = next(
            n for n in ast.walk(tree)
            if isinstance(n, ast.AsyncFunctionDef) and n.name == "_auto_triage_and_inject"
        )
        calls = [
            n for n in ast.walk(fn)
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Name)
            and n.func.id == "_run_escalate_in_background"
        ]
        assert len(calls) == 1, "anti-vacuity: the auto caller must be found"
        kws = {k.arg for k in calls[0].keywords}
        assert "dispatch_roadside" not in kws

    def test_default_is_none(self):
        import inspect

        sig = inspect.signature(bidi._run_escalate_in_background)
        assert sig.parameters["dispatch_roadside"].default is None


# ---------------------------------------------------------------------------
# 7. The drawer mirror tells the truth
# ---------------------------------------------------------------------------


class TestDrawerMirror:
    def _mirror_input(self, ctx, config, **kwargs) -> dict:
        ws = AsyncMock()

        def _spy(*_a, **kw):
            return _fake_escalate_response(kw["severity"], rsa=bool(kw.get("dispatch_roadside")))

        async def _go():
            with patch.object(bidi, "call_escalate_endpoint", side_effect=_spy):
                await bidi._run_escalate_in_background(
                    websocket=ws, ctx=ctx, config=config,
                    user_utterance="engine overheating", booking_result=None,
                    **kwargs,
                )

        asyncio.run(_go())
        call = next(m for m in _sent(ws) if m.get("type") == "tool.call")
        return call["input"]

    def test_auto_path_does_not_claim_a_truck(self, ctx, config):
        """Mutation: restore the hardcoded `"dispatch_roadside": True` → FAILS."""
        inp = self._mirror_input(ctx, config, classification="P0")
        assert "dispatch_roadside" not in inp
        assert inp["severity"] == "P0"

    def test_tap_path_reports_the_explicit_value(self, ctx, config):
        inp = self._mirror_input(ctx, config, classification="P1", dispatch_roadside=False, trigger="driver-tap")
        assert inp["dispatch_roadside"] is False
        assert inp["severity"] == "P1"
        assert inp["trigger"] == "driver-tap"


# ---------------------------------------------------------------------------
# Input-loop wiring
# ---------------------------------------------------------------------------


class TestInputLoopRoutesTheMessage:
    def test_input_callable_dispatches_escalation_request(self):
        """Structural: the WebSocket reader routes `escalation.request` to the
        handler rather than logging and dropping it."""
        src = _BIDI_APP_PATH.read_text(encoding="utf-8")
        tree = ast.parse(src)
        fn = next(
            n for n in ast.walk(tree)
            if isinstance(n, ast.FunctionDef) and n.name == "_make_input_callable"
        )
        routed = any(
            isinstance(n, ast.Compare)
            and any(isinstance(c, ast.Constant) and c.value == "escalation.request" for c in n.comparators)
            for n in ast.walk(fn)
        )
        called = any(
            isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "_handle_escalation_request"
            for n in ast.walk(fn)
        )
        assert routed and called



# ---------------------------------------------------------------------------
# Review C2W2: the P0 auto-book must not record roadside as customer-accepted
# ---------------------------------------------------------------------------


class TestAutoBookRecordsOnlyWhatIsTrue:
    """`customerActionsTaken` is documented (tools/book.py) as what the customer
    agreed to. The P0 auto-book asks the driver nothing, so it may record only
    `create-ticket`. It used to hardcode `schedule-roadside-if-vehicle-fails`
    too, writing a false consent claim into a P0 ticket an advisor reads."""

    def test_constant_is_create_ticket_only(self):
        assert bidi.P0_AUTO_BOOK_ACTION == "create-ticket"

    def test_roadside_action_literal_is_absent_from_the_module(self):
        """Mutation: restore "schedule-roadside-if-vehicle-fails" in the
        auto-book call or its drawer mirror → FAILS."""
        tree = ast.parse(_BIDI_APP_PATH.read_text(encoding="utf-8"))
        hits = [
            n.lineno for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and n.value == "schedule-roadside-if-vehicle-fails"
        ]
        assert not hits, f"roadside recorded as a customer action at lines {hits}"

    def test_auto_book_call_uses_the_constant(self):
        tree = ast.parse(_BIDI_APP_PATH.read_text(encoding="utf-8"))
        fn = next(
            n for n in ast.walk(tree)
            if isinstance(n, ast.AsyncFunctionDef) and n.name == "_auto_triage_and_inject"
        )
        calls = [
            n for n in ast.walk(fn)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
            and n.func.id == "call_book_endpoint"
        ]
        assert calls, "anti-vacuity: the P0 auto-book call must be found"
        for call in calls:
            kw = {k.arg: k.value for k in call.keywords}
            value = kw.get("customer_actions_taken")
            assert isinstance(value, ast.List) and len(value.elts) == 1
            assert isinstance(value.elts[0], ast.Name) and value.elts[0].id == "P0_AUTO_BOOK_ACTION"



# ---------------------------------------------------------------------------
# Security W1: the tap names the decision row /escalate will verify
# ---------------------------------------------------------------------------


class TestTapNamesTheVerifiableDecision:
    """/escalate now honours a roadside dispatch only when `triageSessionId`
    names a recent platform-recorded P0 row for the same tenant and VIN. The
    tap must therefore name the row that actually records the surfaced P0."""

    def test_classifier_verdict_uses_the_ctx_decision(self, ctx, config):
        ctx.triage_classification = "P0"
        calls, _ = _run_request(ctx, config, _state("P0"), {"type": "escalation.request", "intent": "roadside"})
        assert calls[0]["triage_session_id"] is None  # → ctx.triage_decision_id downstream

    def test_driver_confirmed_verdict_names_the_audit_row(self, ctx, config):
        """ctx holds the classifier's P3; the surfaced P0 came from the driver.
        Mutation: always pass None → FAILS (every driver-confirmed roadside tap
        would be refused by /escalate)."""
        ctx.triage_classification = "P3"
        calls, _ = _run_request(ctx, config, _state("P0"), {"type": "escalation.request", "intent": "roadside"})
        assert calls[0]["triage_session_id"] == bidi._driver_confirmation_decision_id(ctx)
        assert calls[0]["triage_session_id"] == f"{ctx.session_id}-driver-confirmed"

    def test_writer_and_reader_share_one_key_function(self):
        """The audit writer must key its row with the same helper the tap uses."""
        tree = ast.parse(_BIDI_APP_PATH.read_text(encoding="utf-8"))
        writer = next(
            n for n in ast.walk(tree)
            if isinstance(n, ast.AsyncFunctionDef) and n.name == "_write_driver_confirmation_audit"
        )
        assert any(
            isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
            and n.func.id == "_driver_confirmation_decision_id"
            for n in ast.walk(writer)
        )

    def test_payload_carries_the_override(self, ctx, config):
        """call_escalate_endpoint sends the override, not ctx's id."""
        from agents.supervisor.tools.escalate import call_escalate_endpoint

        ctx.triage_decision_id = "classifier-row"
        sent: list[dict] = []

        def _fake_post(url, json, headers, timeout):
            sent.append(json)
            resp = AsyncMock()
            resp.status_code = 200
            resp.json = lambda: _fake_escalate_response("P0")
            return resp

        with patch("requests.post", side_effect=_fake_post):
            call_escalate_endpoint(ctx, config, severity="P0", reason="r", triage_session_id="audit-row")
            call_escalate_endpoint(ctx, config, severity="P0", reason="r")
        assert sent[0]["triageSessionId"] == "audit-row"
        assert sent[1]["triageSessionId"] == "classifier-row"


class TestTriageDecisionsTableIsStageDerived:
    def test_explicit_env_wins(self, monkeypatch):
        monkeypatch.setenv("VSA_TRIAGE_DECISIONS_TABLE", "explicit-table")
        assert bidi._triage_decisions_table_name() == "explicit-table"

    def test_stage_derived_default(self, monkeypatch):
        """Mutation: restore the hardcoded "vsa-prod-triage-decisions" default →
        FAILS. Staging wrote its audit rows to prod, then to nothing."""
        monkeypatch.delenv("VSA_TRIAGE_DECISIONS_TABLE", raising=False)
        monkeypatch.setenv("VSA_STAGE", "staging")
        assert bidi._triage_decisions_table_name() == "vsa-staging-triage-decisions"



# ---------------------------------------------------------------------------
# Review Cycle 3: double-contact guard covers every escalation path
# ---------------------------------------------------------------------------


class TestNoSecondContactAcrossPaths:
    def test_tap_during_auto_escalation_is_a_noop(self, ctx, config):
        """Mutation: stop recording the auto task in state → the auto path no
        longer blocks a tap (caught by the AST test below and this one)."""
        state = _state("P0")

        async def _go():
            pending = asyncio.get_running_loop().create_future()
            auto = asyncio.ensure_future(pending)  # never completes during the tap
            state["escalation_request_task"] = auto
            with patch.object(bidi, "call_escalate_endpoint") as spy:
                task = await bidi._handle_escalation_request(
                    websocket=AsyncMock(), ctx=ctx, config=config, state=state,
                    msg={"type": "escalation.request", "intent": "human"},
                )
            auto.cancel()
            return task, spy

        task, spy = asyncio.run(_go())
        assert task is None
        spy.assert_not_called()

    def test_auto_path_records_its_task_in_the_guard_slot(self):
        tree = ast.parse(_BIDI_APP_PATH.read_text(encoding="utf-8"))
        fn = next(
            n for n in ast.walk(tree)
            if isinstance(n, ast.AsyncFunctionDef) and n.name == "_auto_triage_and_inject"
        )
        writes = [
            n for n in ast.walk(fn)
            if isinstance(n, ast.Assign)
            and any(
                isinstance(t, ast.Subscript) and isinstance(t.slice, ast.Constant)
                and t.slice.value == "escalation_request_task" for t in n.targets
            )
            and isinstance(n.value, ast.Name) and n.value.id == "escalate_task"
        ]
        assert writes, "auto path must record escalate_task in state['escalation_request_task']"

    def test_tap_while_model_tool_in_flight_is_a_noop(self, ctx, config):
        import time as _time

        state = _state("P0")
        state["escalation_tool_in_flight_at"] = _time.monotonic()
        calls, sent = _run_request(ctx, config, state, {"type": "escalation.request", "intent": "human"})
        assert calls == []
        assert sent == []  # no 'failed' event: that would tear down the chat being opened

    def test_stale_model_tool_flag_does_not_block_forever(self, ctx, config):
        import time as _time

        state = _state("P0")
        state["escalation_tool_in_flight_at"] = _time.monotonic() - bidi.ESCALATION_TOOL_IN_FLIGHT_S - 1
        calls, _ = _run_request(ctx, config, state, {"type": "escalation.request", "intent": "human"})
        assert len(calls) == 1

    @pytest.mark.parametrize("intent", [["human"], {"a": 1}])
    def test_unhashable_intent_is_refused_not_raised(self, ctx, config, intent):
        """`[] in frozenset` raises TypeError, which would end the voice session."""
        calls, sent = _run_request(ctx, config, _state("P0"), {"type": "escalation.request", "intent": intent})
        assert calls == []
        assert sent and sent[-1]["status"] == "failed"


# ---------------------------------------------------------------------------
# Review Cycle 3: the P0 script promises a live agent only when one is coming
# ---------------------------------------------------------------------------


class TestP0ScriptDoesNotPromiseAnUnopenedChat:
    def test_default_is_the_recommendation_form(self):
        g = bidi._classification_guidance("P0")
        assert bidi.P0_SENTENCE_2_RECOMMEND in g
        assert bidi.P0_SENTENCE_2_CHAT_OPENING not in g

    def test_chat_opening_form_only_when_asked(self):
        g = bidi._classification_guidance("P0", chat_opening=True)
        assert bidi.P0_SENTENCE_2_CHAT_OPENING in g

    def test_pull_over_sentence_is_identical_in_both_forms(self):
        """Sentence (1) is the protected safety seam; only (2) may vary."""
        for g in (bidi._classification_guidance("P0"), bidi._classification_guidance("P0", chat_opening=True)):
            assert "(1) tell the driver to pull over safely and turn on their hazard lights" in g

    @pytest.mark.parametrize("fn_name,expected", [
        ("_auto_triage_and_inject", "auto_open_chat"),
        ("_handle_confirmation_response", False),
    ])
    def test_callers_pass_the_true_condition(self, fn_name, expected):
        """Mutation: pass `chat_opening=True` from the driver-confirmed branch, or
        drop the argument on the auto path → FAILS."""
        tree = ast.parse(_BIDI_APP_PATH.read_text(encoding="utf-8"))
        fn = next(
            n for n in ast.walk(tree)
            if isinstance(n, ast.AsyncFunctionDef) and n.name == fn_name
        )
        p0_calls = []
        for n in ast.walk(fn):
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "_classification_guidance":
                kw = {k.arg: k.value for k in n.keywords}
                if "chat_opening" in kw:
                    p0_calls.append(kw["chat_opening"])
        assert p0_calls, f"{fn_name} must pass chat_opening explicitly"
        v = p0_calls[0]
        if expected is False:
            assert isinstance(v, ast.Constant) and v.value is False
        else:
            assert isinstance(v, ast.Name) and v.id == expected



# ---------------------------------------------------------------------------
# Review Cycle 4 C4W2: escalation_tool_in_flight_at is written by the
# output callable, not hand-assigned (C4W2b)
# ---------------------------------------------------------------------------


def _minimal_state() -> dict:
    """Minimum state dict for _make_output_callable to run tool_use/result."""
    return {
        "event_count": 0,
        "type_counts": {},
        "escalation_tool_in_flight_at": None,
        "tool_use_names": {},
        # Optional keys referenced via .get() — missing is fine:
        # last_audio_output_at, assistant_speaking, injection_queue, ...
    }


class TestEscalationToolInFlightFlagWriter:
    """The output callable is the only producer of escalation_tool_in_flight_at.

    Mutations *"never set the flag"* and *"never clear the flag"* leave the
    supervisor suite green when the tests hand-assign the timestamp, because
    the producer (the output callable) is never exercised. These tests drive
    a tool_use_stream + tool_result pair through the real output callable and
    assert the flag changes, so both mutations become visible (C4W2b).
    """

    def _run_output_events(self, events: list[dict], state: dict | None = None) -> dict:
        """Drive a list of events through _make_output_callable; return final state."""
        ws = AsyncMock()
        if state is None:
            state = _minimal_state()
        # Config/agent/ctx are passed through but only read by code paths not
        # triggered here (bidi audio, triage injection). Use nulls.
        ctx_stub = SimpleNamespace(
            session_id="test-in-flight",
            triage_classification=None,
            vin="VIN-TEST",
        )
        config_stub = SimpleNamespace()
        agent_stub = SimpleNamespace()

        async def _go():
            output_callable = bidi._make_output_callable(
                ws, ctx_stub, state, config_stub, agent_stub
            )
            for event in events:
                await output_callable(event)

        asyncio.run(_go())
        return state

    # --- set side ---

    def test_tool_use_stream_for_escalate_to_human_sets_the_flag(self):
        """Mutation: delete `state["escalation_tool_in_flight_at"] = time.monotonic()`
        in the tool_use_stream branch → this test FAILS.
        """
        state = self._run_output_events([
            {
                "type": "tool_use_stream",
                "current_tool_use": {
                    "name": "escalate_to_human",
                    "toolUseId": "tu-001",
                    "input": {"reason": "driver requested"},
                },
            }
        ])
        assert state["escalation_tool_in_flight_at"] is not None
        assert isinstance(state["escalation_tool_in_flight_at"], float)

    def test_tool_use_stream_for_other_tool_does_not_set_the_flag(self):
        """Only escalate_to_human writes the flag; a different tool must not."""
        state = self._run_output_events([
            {
                "type": "tool_use_stream",
                "current_tool_use": {
                    "name": "book",
                    "toolUseId": "tu-002",
                    "input": {},
                },
            }
        ])
        assert state["escalation_tool_in_flight_at"] is None

    # --- clear side ---

    def test_tool_result_for_escalate_to_human_clears_the_flag(self):
        """Mutation: delete `state["escalation_tool_in_flight_at"] = None`
        in the tool_result branch → this test FAILS.
        """
        import time as _time
        state = _minimal_state()
        # Pre-set to simulate a set that was produced by a previous event.
        state["escalation_tool_in_flight_at"] = _time.monotonic()
        # Also prime the name-lookup so the tool_result branch resolves
        # the tool name correctly.
        state["tool_use_names"] = {"tu-001": "escalate_to_human"}
        state = self._run_output_events(
            [
                {
                    "type": "tool_result",
                    "tool_result": {
                        "toolUseId": "tu-001",
                        "content": [{"json": {"contactId": "cnt-001"}}],
                    },
                }
            ],
            state=state,
        )
        assert state["escalation_tool_in_flight_at"] is None

    # --- round-trip: set then clear ---

    def test_set_then_clear_via_output_callable(self):
        """Drive the full tool_use_stream → tool_result sequence and assert
        the flag is set between the two events and cleared after.

        Mutation "never set the flag": flag is still None after tool_use → FAIL.
        Mutation "never clear the flag": flag is not None after tool_result → FAIL.
        """
        import time as _time
        ws = AsyncMock()
        state = _minimal_state()
        ctx_stub = SimpleNamespace(
            session_id="test-roundtrip",
            triage_classification=None,
            vin="VIN-RT",
        )

        flag_after_use: list[float | None] = []
        flag_after_result: list[float | None] = []

        async def _go():
            output_callable = bidi._make_output_callable(
                ws, ctx_stub, state, SimpleNamespace(), SimpleNamespace()
            )
            use_event = {
                "type": "tool_use_stream",
                "current_tool_use": {
                    "name": "escalate_to_human",
                    "toolUseId": "tu-rt-1",
                    "input": {"reason": "test"},
                },
            }
            result_event = {
                "type": "tool_result",
                "tool_result": {
                    "toolUseId": "tu-rt-1",
                    "content": [{"json": {"contactId": "cnt-rt-1"}}],
                },
            }
            await output_callable(use_event)
            flag_after_use.append(state["escalation_tool_in_flight_at"])
            await output_callable(result_event)
            flag_after_result.append(state["escalation_tool_in_flight_at"])

        asyncio.run(_go())

        assert flag_after_use[0] is not None, (
            "escalation_tool_in_flight_at should be set after tool_use_stream"
        )
        assert flag_after_result[0] is None, (
            "escalation_tool_in_flight_at should be cleared after tool_result"
        )


# ---------------------------------------------------------------------------
# Review Cycle 4 C4W2a: sentinel reserves the slot before the auto-book call
# ---------------------------------------------------------------------------


class TestAutoEscalationSlotReservation:
    """The auto path must reserve the escalation slot right after triage returns
    and before the async book call, so a tap arriving in that window replaces
    the sentinel and the auto path cancels its own task (the tap wins).

    Design (decisions.md § "F5.2 design"): the tap wins the race because its
    intent may be ``roadside``, which the auto path never requests. Refusing it
    would silently drop a roadside request the driver made. Exactly one
    ``/escalate`` call results either way.

    Mutation "no reservation before auto-book": the slot stays None until after
    book returns, so a tap in the P0 book-call window passes every check and
    both paths call /escalate.
    Mutation "auto path overwrites the tap task": the auto path assigns
    escalate_task unconditionally, so both connect contacts are opened.
    """

    def test_sentinel_is_placed_before_the_book_call(self):
        """AST: the assignment to _ESCALATION_SLOT_RESERVED must appear in
        _auto_triage_and_inject BEFORE the call_book_endpoint call.

        Mutation "no reservation before auto-book": remove the sentinel
        assignment → this test FAILS.
        """
        src = _BIDI_APP_PATH.read_text(encoding="utf-8")
        tree = ast.parse(src)
        fn = next(
            n for n in ast.walk(tree)
            if isinstance(n, ast.AsyncFunctionDef) and n.name == "_auto_triage_and_inject"
        )

        # Find the line number of the sentinel assignment.
        sentinel_line = None
        for node in ast.walk(fn):
            if (
                isinstance(node, ast.Assign)
                and any(
                    isinstance(t, ast.Subscript)
                    and isinstance(t.slice, ast.Constant)
                    and t.slice.value == "escalation_request_task"
                    for t in node.targets
                )
                and isinstance(node.value, ast.Attribute)
                and node.value.attr == "_ESCALATION_SLOT_RESERVED"
            ):
                sentinel_line = node.lineno
                break
            # Also accept the form: Name id="_ESCALATION_SLOT_RESERVED"
            if (
                isinstance(node, ast.Assign)
                and any(
                    isinstance(t, ast.Subscript)
                    and isinstance(t.slice, ast.Constant)
                    and t.slice.value == "escalation_request_task"
                    for t in node.targets
                )
                and isinstance(node.value, ast.Name)
                and node.value.id == "_ESCALATION_SLOT_RESERVED"
            ):
                sentinel_line = node.lineno
                break

        assert sentinel_line is not None, (
            "_auto_triage_and_inject must assign _ESCALATION_SLOT_RESERVED to "
            "state['escalation_request_task'] before the book call"
        )

        # Find the line number of the call_book_endpoint call.
        book_line = None
        for node in ast.walk(fn):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "call_book_endpoint"
            ):
                book_line = node.lineno
                break

        assert book_line is not None, "anti-vacuity: call_book_endpoint must be found"
        assert sentinel_line < book_line, (
            f"sentinel assignment (line {sentinel_line}) must precede "
            f"call_book_endpoint (line {book_line})"
        )

    def test_auto_path_skips_when_tap_task_is_in_slot(self):
        """AST: the auto path must check whether a tap has replaced the sentinel
        before assigning escalate_task, and skip (cancel) if so.

        Mutation "auto path overwrites the tap task": remove the sentinel check
        and assign escalate_task unconditionally → this test FAILS.
        """
        src = _BIDI_APP_PATH.read_text(encoding="utf-8")
        tree = ast.parse(src)
        fn = next(
            n for n in ast.walk(tree)
            if isinstance(n, ast.AsyncFunctionDef) and n.name == "_auto_triage_and_inject"
        )

        # The guard must read state["escalation_request_task"] and compare it
        # to _ESCALATION_SLOT_RESERVED before the unconditional assignment.
        # We check that there is an `if` whose test references both
        # "escalation_request_task" (from state) and "_ESCALATION_SLOT_RESERVED".
        guard_found = False
        for node in ast.walk(fn):
            if not isinstance(node, ast.If):
                continue
            test_src = ast.unparse(node.test)
            if "escalation_request_task" in test_src and "_ESCALATION_SLOT_RESERVED" in test_src:
                # The if-body must contain the unconditional task assignment.
                body_assigns = [
                    n for n in ast.walk(node)
                    if isinstance(n, ast.Assign)
                    and any(
                        isinstance(t, ast.Subscript)
                        and isinstance(t.slice, ast.Constant)
                        and t.slice.value == "escalation_request_task"
                        for t in n.targets
                    )
                    and isinstance(n.value, ast.Name)
                    and n.value.id == "escalate_task"
                ]
                if body_assigns:
                    guard_found = True
                    break

        assert guard_found, (
            "_auto_triage_and_inject must check _ESCALATION_SLOT_RESERVED before "
            "assigning escalate_task to state['escalation_request_task']; "
            "the auto path must skip when a tap has already claimed the slot"
        )



# ---------------------------------------------------------------------------
# Mutation coverage for the escalation race and double-contact guards.
#
# HOW TO READ THIS, and why it does not state counts. Earlier versions gave a
# per-mutation failure COUNT and were wrong four cycles running (C6W2, Cycle 7
# S5, C8W2) — not from carelessness but structurally: almost any new test that
# drives the auto path's happy path also fails mutations (b) and (e), so every
# addition silently invalidated numbers written one commit earlier. Counts are
# a property of the whole suite, not of the guard. So each row names the test
# that TARGETS the mutation, and rows where that test is the ONLY failure are
# marked [SOLE GUARD] — delete one of those and the property is unprotected.
# Other tests failing incidentally is expected and not recorded.
#
# Every row was MEASURED: the mutation applied to a /tmp copy with an
# assertion that its pattern matched exactly once, the suite run, failing test
# ids recorded, the file restored and sha256-verified. Regenerate the same way.
#
# --- Cycle 5 C5W1: the tap/auto race ---
#   (a) escalate_task.cancel() -> pass
#         test_tap_during_book_makes_exactly_one_escalate_call
#         test_tap_during_book_carries_the_tap_dispatch_flag
#       These two are the only guard on the C4W2 double contact. NOT the no-tap
#       probe: with no tap, cancel() is never reached.
#   (b) post-book `is _ESCALATION_SLOT_RESERVED` -> `is not`
#         test_no_tap_makes_exactly_one_escalate_call
#   (c) tap guard reverted to `existing is not None and not existing.done()`
#         test_tap_accepts_when_slot_holds_the_sentinel  (AttributeError)
#   (d) tap handler refuses when the slot holds the sentinel
#         test_tap_accepts_when_slot_holds_the_sentinel
#   (e) sentinel write put under `if False and ...`
#         test_no_tap_makes_exactly_one_escalate_call
#       NOT the tap-during-book probes: with no reservation the tap sees an
#       empty slot, the auto path still finds the tap's task afterwards and
#       stands down, so the count stays 1. Shape matters — `if False and ...`
#       leaves the assignment AST-visible; deleting the line outright also
#       fails test_sentinel_is_placed_before_the_book_call.
#   (f) the reservation made unconditional again (Fix Group 6's fix reverted)
#         test_tap_accepted_before_triage_returns_makes_exactly_one_call
#         [SOLE GUARD]
#
# --- Cycle 6 C6W1: stand down on a contact that actually opened ---
#   (h) the `elif state.get("escalation_contact_id")` branch removed
#         test_completed_successful_tap_stops_the_auto_path
#   (i) the `state["escalation_contact_id"] = contact_id` write removed
#         test_completed_successful_tap_stops_the_auto_path
#   (j) that contact id recorded unconditionally (on failure too)
#         test_escalation_without_a_contact_id_does_not_stop_the_auto_path
#         [SOLE GUARD]
#       A hard raise skips everything after the escalate call, so it cannot
#       tell a guarded write from an unguarded one; only a response that
#       returns while naming no contact exercises the guard. This mutation
#       survived the whole suite until that test was added.
#   (k) `state=state` dropped from the tap's _run_escalate_in_background call
#         test_completed_successful_tap_stops_the_auto_path
#
# --- Cycle 7 C7W1 and Cycle 8 C8W1 are annotated at
#     TestModelToolOpenedContactAlsoStopsTheAutoPath, with the same rules. ---
#
# 16/16 mutations caught as of this commit. The jest-side counterpart (a third
# CMS table added to CmsVehicleDriverRead) lives in
# infrastructure/test/vsa-api-stack-w1-dispatch-gate.test.ts.
# ---------------------------------------------------------------------------


def _auto_ctx(config: "Config") -> "SessionContext":
    """A SessionContext with auto_open_human_chat_on_p0 = True."""
    c = SessionContext(
        tenant_id=config.tenant_id,
        session_id="vsa-auto-race-test",
        jwt="fake-jwt",
        persona_id="fleet_driver",
        vin="AUTORACE0000000001",
        vehicle_id="VEH-AUTO-001",
    )
    c.triage_classification = None  # will be set by the mocked triage call
    c.auto_open_human_chat_on_p0 = True
    return c


def _auto_state() -> dict:
    """Minimal state dict accepted by _auto_triage_and_inject."""
    return {
        "surfaced_classification": None,
        "escalation_request_task": None,
        "active_dtcs_for_prompt": [],
        "event_count": 0,
        "type_counts": {},
        "escalation_tool_in_flight_at": None,
        "tool_use_names": {},
    }


def _fake_triage_response(classification: str = "P0") -> dict:
    return {
        "classification": classification,
        "triageDecisionId": "triage-test-001",
        "latencyMs": 10,
        "confidence": 0.95,
    }


def _fake_book_response() -> dict:
    return {
        "requestNumber": "REQ-TEST-001",
        "status": "created",
        "messageId": "msg-001",
    }


async def _run_auto_triage(
    ctx,
    config,
    state: dict,
    *,
    triage_response=None,
    book_gate: threading.Event | None = None,
    escalate_spy: list | None = None,
    escalate_side_effect=None,
):
    """Drive _auto_triage_and_inject with all three endpoints patched.

    book_gate: if given, the book call blocks until the event is set
               (uses threading.Event.wait() directly — safe in executor threads).
    escalate_spy: if given, captures kwargs from every call_escalate_endpoint call.
    escalate_side_effect: replaces the default escalate stub entirely — use to
               simulate a /escalate failure.

    Returns the injection-queue stub so callers can assert on the note that was
    submitted to Nova Sonic (the narration half of the auto path).
    """
    if triage_response is None:
        triage_response = _fake_triage_response()
    if escalate_spy is None:
        escalate_spy = []

    def _triage(*_a, **_kw):
        return triage_response

    def _book(*_a, **_kw):
        if book_gate is not None:
            # This runs in an executor thread — no event loop. Use the
            # threading.Event directly; the caller sets it when ready.
            book_gate.wait(timeout=5)
        return _fake_book_response()

    def _escalate(*_a, **kw):
        escalate_spy.append(kw)
        return _fake_escalate_response(kw["severity"])

    ws = AsyncMock()
    agent_stub = SimpleNamespace(send=AsyncMock())
    queue_stub = SimpleNamespace(
        submit=AsyncMock(),
        start=lambda: None,
        stop=AsyncMock(),
    )

    with (
        patch.object(bidi, "call_triage_endpoint", side_effect=_triage),
        patch.object(bidi, "call_book_endpoint", side_effect=_book),
        patch.object(
            bidi, "call_escalate_endpoint",
            side_effect=escalate_side_effect or _escalate,
        ),
    ):
        await bidi._auto_triage_and_inject(
            ws, agent_stub, ctx, config,
            "engine light on", queue_stub, state,
        )
        # Drain any background escalate task the function created. Stays INSIDE
        # the `with` so the endpoint patches are still active while it runs.
        task = state.get("escalation_request_task")
        if asyncio.isfuture(task) and not task.done():
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=2)
            except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
                pass
    return queue_stub


class TestAutoEscalationSlotBehavioural:
    """Behavioural probes that drive the real _auto_triage_and_inject and
    _handle_escalation_request; the five AST-surviving mutations each fail
    at least one of these tests (C5W1)."""

    def test_no_tap_makes_exactly_one_escalate_call(self, config):
        """No tap — auto path should make exactly one /escalate call.

        Catches mutation (b): `is _ESCALATION_SLOT_RESERVED` → `is not`
          The inverted guard makes the auto path cancel its own task → 0 calls → FAILS.
        Catches mutation (e): sentinel write made dead (`if False and …`)
          With no reservation the post-book check reads None, which is not the
          sentinel, so the auto path takes the else branch and cancels its own
          task → 0 calls → FAILS. Verified by mutation, not inferred.
        """
        state = _auto_state()
        ctx = _auto_ctx(config)
        spy: list[dict] = []

        asyncio.run(_run_auto_triage(ctx, config, state, escalate_spy=spy))

        assert len(spy) == 1, (
            f"expected exactly 1 /escalate call with no tap; got {len(spy)}"
        )

    def test_tap_during_book_makes_exactly_one_escalate_call(self, config):
        """A tap arriving while the book call is blocked must produce exactly
        one /escalate call total (the tap's), not two.

        Catches mutation (a): escalate_task.cancel() → pass
          Auto task is not cancelled; both paths reach /escalate → 2 calls → FAILS.

        Does NOT catch mutation (e) (sentinel write made dead): with no
        reservation the tap simply sees an empty slot, and the auto path still
        finds the tap's task rather than the sentinel afterwards, so it stands
        down and the count stays 1. `test_no_tap_makes_exactly_one_escalate_call`
        is what catches (e). Verified by mutation.
        """
        state = _auto_state()
        ctx = _auto_ctx(config)
        # surfaced_classification must be set so the tap's in-flight check works.
        # In real flow, auto-triage writes this after triage returns but before
        # the book call. We simulate that here: triage returns "P0" and the
        # auto path sets surfaced_classification before blocking on book.
        # We pre-populate it so _handle_escalation_request accepts the tap.
        state["surfaced_classification"] = "P0"
        ctx.triage_classification = "P0"
        all_spy: list[dict] = []
        book_gate = threading.Event()

        async def _go():
            loop = asyncio.get_running_loop()

            # Start auto-triage; the book call will block on book_gate.
            auto_task = loop.create_task(
                _run_auto_triage(
                    ctx, config, state,
                    book_gate=book_gate,
                    escalate_spy=all_spy,
                )
            )

            # Spin until the sentinel is written in the slot (book is in flight).
            for _ in range(100):
                await asyncio.sleep(0.02)
                slot = state.get("escalation_request_task")
                if slot is bidi._ESCALATION_SLOT_RESERVED or asyncio.isfuture(slot):
                    break

            # Tap arrives while book is blocking.
            tap_task = await bidi._handle_escalation_request(
                websocket=AsyncMock(), ctx=ctx, config=config, state=state,
                msg={"type": "escalation.request", "intent": "human"},
            )

            # Release the book call; auto-triage will proceed.
            book_gate.set()
            await auto_task

            if tap_task is not None and not tap_task.done():
                try:
                    await asyncio.wait_for(tap_task, timeout=2)
                except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
                    pass

        asyncio.run(_go())

        assert len(all_spy) == 1, (
            f"expected exactly 1 /escalate call with a tap during book; "
            f"got {len(all_spy)}"
        )

    def test_tap_during_book_carries_the_tap_dispatch_flag(self, config):
        """The single call must carry the tap's dispatch_roadside value, not
        the auto path's None — confirming the tap task won, not the auto task.

        Together with test_tap_during_book_makes_exactly_one_escalate_call this
        catches mutation (a) (cancel → pass).
        """
        state = _auto_state()
        ctx = _auto_ctx(config)
        state["surfaced_classification"] = "P0"
        ctx.triage_classification = "P0"
        all_spy: list[dict] = []
        book_gate = threading.Event()

        async def _go():
            loop = asyncio.get_running_loop()
            auto_task = loop.create_task(
                _run_auto_triage(
                    ctx, config, state,
                    book_gate=book_gate,
                    escalate_spy=all_spy,
                )
            )
            for _ in range(100):
                await asyncio.sleep(0.02)
                slot = state.get("escalation_request_task")
                if slot is bidi._ESCALATION_SLOT_RESERVED or asyncio.isfuture(slot):
                    break

            # Use the human intent (not roadside) so dispatch_roadside is False.
            # If the auto task won instead, dispatch_roadside would be None (omitted).
            tap_task = await bidi._handle_escalation_request(
                websocket=AsyncMock(), ctx=ctx, config=config, state=state,
                msg={"type": "escalation.request", "intent": "human"},
            )
            book_gate.set()
            await auto_task
            if tap_task is not None and not tap_task.done():
                try:
                    await asyncio.wait_for(tap_task, timeout=2)
                except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
                    pass

        asyncio.run(_go())

        assert len(all_spy) == 1
        call = all_spy[0]
        # The tap's human intent sends dispatch_roadside=False explicitly.
        # The auto path omits it (None). If this assertion fails, the auto task won.
        assert call.get("dispatch_roadside") is False, (
            "the single /escalate call should carry dispatch_roadside=False (the "
            "tap's human intent); if the auto task won, dispatch_roadside would "
            f"be None. Got: {call.get('dispatch_roadside')!r}"
        )

    def test_tap_accepts_when_slot_holds_the_sentinel(self, config):
        """_handle_escalation_request must accept a tap when the slot holds the
        sentinel (a plain object, not an asyncio.Future).

        Catches mutation (c): guard reverted to `existing is not None and not existing.done()`
          — the sentinel is not None and has no .done() → AttributeError, session ends.
        Catches mutation (d): tap handler refuses when slot holds the sentinel
          — zero calls returned; this test expects one → FAILS.
        """
        state = _auto_state()
        # Place the sentinel in the slot as the auto path would.
        state["escalation_request_task"] = bidi._ESCALATION_SLOT_RESERVED
        state["surfaced_classification"] = "P0"
        ctx = _auto_ctx(config)
        ctx.triage_classification = "P0"

        calls, sent = _run_request(
            ctx, config, state,
            {"type": "escalation.request", "intent": "human"},
        )

        assert len(calls) == 1, (
            "the tap must be accepted when the slot holds the sentinel; "
            f"got {len(calls)} /escalate calls"
        )
        # The tap replaces the sentinel with its own (now done) asyncio.Future.
        assert asyncio.isfuture(state.get("escalation_request_task")), (
            "the tap must replace the sentinel with its own asyncio.Future"
        )

    def test_tap_accepted_before_triage_returns_makes_exactly_one_call(self, config):
        """A tap accepted BEFORE auto-triage returns already owns the slot. The
        auto path must stand down rather than overwrite it.

        This is the reverse of the book-window race: there the tap arrives while
        the auto path holds the slot; here the tap holds it first. Before the
        Fix Group 6 fix the reservation was unconditional, so the sentinel
        replaced the tap's live task, the auto path then found its own sentinel
        after book, and both paths escalated.

        Mutation "reservation made unconditional again" (revert the
        `isinstance(_existing, asyncio.Future)` guard to a bare
        `state["escalation_request_task"] = _ESCALATION_SLOT_RESERVED`):
        2 /escalate calls -> this test FAILS.
        """
        state = _auto_state()
        state["surfaced_classification"] = "P0"
        ctx = _auto_ctx(config)
        ctx.triage_classification = "P0"

        all_spy: list[dict] = []
        tap_escalate_gate = threading.Event()

        def _blocking_escalate(*_a, **kw):
            all_spy.append(kw)
            # Hold the tap's call open so its task is still in flight while
            # auto-triage runs and tries to reserve the slot.
            tap_escalate_gate.wait(timeout=5)
            return _fake_escalate_response(kw["severity"])

        async def _go():
            with patch.object(bidi, "call_escalate_endpoint", side_effect=_blocking_escalate):
                tap_task = await bidi._handle_escalation_request(
                    websocket=AsyncMock(), ctx=ctx, config=config, state=state,
                    msg={"type": "escalation.request", "intent": "human"},
                )
                assert tap_task is not None, "anti-vacuity: the tap must be accepted"

                # Wait until the tap's /escalate call is actually in flight.
                for _ in range(100):
                    await asyncio.sleep(0.02)
                    if all_spy:
                        break
                assert all_spy, "anti-vacuity: the tap's /escalate call must start"
                assert not tap_task.done(), (
                    "anti-vacuity: the tap task must still be in flight when "
                    "auto-triage reserves the slot"
                )
                assert asyncio.isfuture(state.get("escalation_request_task")), (
                    "anti-vacuity: the tap's task must be in the slot"
                )

                # Auto-triage now runs with the tap's live task in the slot.
                await _run_auto_triage(ctx, config, state, escalate_spy=all_spy)

            tap_escalate_gate.set()
            await asyncio.wait_for(tap_task, timeout=5)

            # Give any background escalate task the auto path may have started
            # a chance to run, so a regression shows up as a second call.
            for _ in range(50):
                await asyncio.sleep(0.02)
                pending = [
                    t for t in asyncio.all_tasks()
                    if t is not asyncio.current_task() and not t.done()
                ]
                if not pending:
                    break

        asyncio.run(_go())

        assert len(all_spy) == 1, (
            "a tap accepted before triage returned must be the only /escalate "
            f"call; the auto path must stand down. Got {len(all_spy)} calls"
        )



# ---------------------------------------------------------------------------
# Review Cycle 6, C6W1: the auto path must stand down on a contact that was
# actually opened, not on the task slot — which cannot tell a finished-and-
# successful escalation from a finished-and-failed one.
# ---------------------------------------------------------------------------


class TestAutoPathStandsDownOnAnOpenContact:
    """`escalation_request_task` answers "is a request in flight?". The auto path
    needs a different question: "has a human contact already been opened?".

    Reusing the slot for both conflated them. A done Future meant the same thing
    whether the call opened a Connect contact or raised, so a tap that had
    already succeeded did not stop the auto path, and a second contact opened
    for one P0 (C6W1, reproduced at `af88f1f` as 2 calls / flags [False, None]).

    The fix records `state["escalation_contact_id"]` inside
    `_run_escalate_in_background`, on success only. Two properties follow, and
    both are pinned below because they pull in opposite directions:
      1. a SUCCESSFUL escalation stops the auto path (no duplicate contact);
      2. a FAILED one does not (no missed human on a P0).
    Only the auto path stands down. `_handle_escalation_request` still accepts a
    tap on a done Future, so the driver keeps a one-tap route to a human.
    """

    @staticmethod
    def _accept_tap(ctx, config, state, escalate):
        """Run a tap to completion with `escalate` as call_escalate_endpoint."""
        async def _go():
            with patch.object(bidi, "call_escalate_endpoint", side_effect=escalate):
                task = await bidi._handle_escalation_request(
                    websocket=AsyncMock(), ctx=ctx, config=config, state=state,
                    msg={"type": "escalation.request", "intent": "human"},
                )
                assert task is not None, "anti-vacuity: the tap must be accepted"
                await task
            return task
        return _go

    @staticmethod
    async def _drain():
        for _ in range(50):
            await asyncio.sleep(0.02)
            pending = [
                t for t in asyncio.all_tasks()
                if t is not asyncio.current_task() and not t.done()
            ]
            if not pending:
                break

    def test_completed_successful_tap_stops_the_auto_path(self, config):
        """C6W1. A tap whose /escalate SUCCEEDED must stop the auto path.

        Mutations that must fail this test:
          - the `elif state.get("escalation_contact_id")` branch removed
          - the `state["escalation_contact_id"] = contact_id` write removed
          - `state=state` dropped from the tap's _run_escalate_in_background call
        """
        state = _auto_state()
        state["surfaced_classification"] = "P0"
        ctx = _auto_ctx(config)
        ctx.triage_classification = "P0"
        all_spy: list[dict] = []

        def _escalate(*_a, **kw):
            all_spy.append(kw)
            return _fake_escalate_response(kw["severity"])

        async def _go():
            tap_task = await self._accept_tap(ctx, config, state, _escalate)()

            # Anti-vacuity: the exact state C6W1 depends on.
            assert tap_task.done(), "anti-vacuity: the tap task must be done"
            slot = state.get("escalation_request_task")
            assert asyncio.isfuture(slot) and slot.done(), (
                "anti-vacuity: the slot must hold a DONE Future — that is the "
                "state the in-flight check cannot act on"
            )
            assert len(all_spy) == 1, "anti-vacuity: the tap alone makes 1 call"
            assert state.get("escalation_contact_id"), (
                "anti-vacuity: a successful escalation must record the contact id"
            )

            await _run_auto_triage(ctx, config, state, escalate_spy=all_spy)
            await self._drain()

        asyncio.run(_go())

        assert len(all_spy) == 1, (
            "a successful tap must be the only /escalate call; the auto path "
            f"must stand down on the open contact. Got {len(all_spy)}: "
            f"{[c.get('dispatch_roadside') for c in all_spy]}"
        )

    def test_failed_tap_does_not_stop_the_auto_path(self, config):
        """The other direction, and the one that matters more. If the tap's
        /escalate FAILED, no contact was opened and the auto path must still
        escalate. A missed human on a P0 is worse than a duplicate one.

        Mutation: record `escalation_contact_id` unconditionally rather than
        only on success (e.g. drop the `if ... contact_id` guard, or set it
        before the call) → the auto path stands down after a failure and the
        driver gets nobody → this test FAILS.
        """
        state = _auto_state()
        state["surfaced_classification"] = "P0"
        ctx = _auto_ctx(config)
        ctx.triage_classification = "P0"
        auto_spy: list[dict] = []

        def _failing_escalate(*_a, **_kw):
            raise RuntimeError("Connect StartChatContact refused")

        async def _go():
            tap_task = await self._accept_tap(ctx, config, state, _failing_escalate)()

            # Anti-vacuity: the tap really did run and really did fail.
            assert tap_task.done(), "anti-vacuity: the tap task must be done"
            assert not state.get("escalation_contact_id"), (
                "anti-vacuity: a FAILED escalation must not record a contact id"
            )

            await _run_auto_triage(ctx, config, state, escalate_spy=auto_spy)
            await self._drain()

        asyncio.run(_go())

        assert len(auto_spy) == 1, (
            "after a FAILED tap the auto path must still escalate — the "
            "stand-down keys on a contact that opened, not on a finished "
            f"request. Got {len(auto_spy)} calls"
        )
        assert auto_spy[0].get("dispatch_roadside") is None, (
            "the surviving call must be the auto path's (which omits "
            "dispatch_roadside), not a replayed tap"
        )

    def test_escalation_without_a_contact_id_does_not_stop_the_auto_path(self, config):
        """The soft-failure case, and the only one that actually exercises the
        `and contact_id` guard.

        A raise never reaches the write at all — everything after the escalate
        call is skipped — so a hard failure cannot distinguish a guarded write
        from an unguarded one. Only a response that RETURNS while naming no
        contact does. Found by mutation: without this test, recording the id
        unconditionally (`contact_id or "unknown"`) survived the whole suite.

        Mutation: drop the `and contact_id` half of the guard, or record
        `contact_id or "unknown"` → the auto path stands down though no contact
        exists, and the driver gets nobody → this test FAILS.
        """
        state = _auto_state()
        state["surfaced_classification"] = "P0"
        ctx = _auto_ctx(config)
        ctx.triage_classification = "P0"
        auto_spy: list[dict] = []

        def _contactless_escalate(*_a, **kw):
            resp = _fake_escalate_response(kw["severity"])
            resp["contactId"] = None  # Connect accepted nothing
            return resp

        async def _go():
            tap_task = await self._accept_tap(ctx, config, state, _contactless_escalate)()
            assert tap_task.done(), "anti-vacuity: the tap task must be done"
            assert not state.get("escalation_contact_id"), (
                "anti-vacuity: a response naming no contact must not record one"
            )
            await _run_auto_triage(ctx, config, state, escalate_spy=auto_spy)
            await self._drain()

        asyncio.run(_go())

        assert len(auto_spy) == 1, (
            "a /escalate that returned no contactId did not open a contact, so "
            f"the auto path must still escalate. Got {len(auto_spy)} calls"
        )

    def test_stand_down_narrates_the_recommendation_not_a_connection(self, config):
        """Review Cycle 6, Suggestion 2. On the stand-down path `auto_open_chat`
        is False, so the injected note must carry the recommendation sentence and
        must NOT claim a live agent is being connected.

        Cycle 3 established that promising an unopened chat is the defect. The
        existing guards on this are source-text and AST checks; this asserts the
        note that actually reaches Nova Sonic.
        """
        state = _auto_state()
        state["surfaced_classification"] = "P0"
        ctx = _auto_ctx(config)
        ctx.triage_classification = "P0"
        all_spy: list[dict] = []

        def _escalate(*_a, **kw):
            all_spy.append(kw)
            return _fake_escalate_response(kw["severity"])

        async def _go():
            await self._accept_tap(ctx, config, state, _escalate)()
            assert state.get("escalation_contact_id"), "anti-vacuity: contact recorded"
            queue = await _run_auto_triage(ctx, config, state, escalate_spy=all_spy)
            await self._drain()
            return queue

        queue = asyncio.run(_go())

        submitted_payloads: list[str] = []
        for call in queue.submit.call_args_list:
            for a in list(call.args) + list(call.kwargs.values()):
                payload = getattr(a, "payload", None)
                submitted_payloads.append(payload if isinstance(payload, str) else str(a))
        submitted = "\n".join(submitted_payloads)
        assert submitted.strip(), "anti-vacuity: a note must have been submitted"
        # Anti-vacuity: we are reading InjectionRequest.payload, not its repr.
        assert "object at 0x" not in submitted, (
            "anti-vacuity: the payload was not extracted — this is reading an "
            "object repr, so the assertions below would be vacuous"
        )
        assert bidi.P0_SENTENCE_2_RECOMMEND in submitted, (
            "the stand-down path must narrate the recommendation form"
        )
        assert bidi.P0_SENTENCE_2_CHAT_OPENING not in submitted, (
            "the stand-down path must NOT tell the driver an agent is being "
            "connected right now — Cycle 3's defect"
        )



# ---------------------------------------------------------------------------
# Review Cycle 7 C7W1 and Cycle 8 C8W1: the model's own escalate_to_human tool is
# a THIRD way a contact gets opened, and the auto path could not see it.
#
# Same rules as the table above: each row names the test that TARGETS the
# mutation, [SOLE GUARD] marks rows where that test is the only failure, and no
# counts are stated. Measured on /tmp copies with patterns asserted and sha256
# restore.
#   (l) the `state["escalation_contact_id"] = result_data["contactId"]` write at
#       the tool-result site removed
#         test_contact_opened_by_the_model_tool_stops_the_auto_path  [SOLE GUARD]
#   (m) the `elif _model_escalation_tool_in_flight(state)` branch removed
#         test_model_tool_in_flight_stops_the_auto_path  [SOLE GUARD]
#   (n) `_model_escalation_tool_in_flight` always returns False
#         test_model_tool_in_flight_stops_the_auto_path
#         test_tap_while_model_tool_in_flight_is_a_noop  (pre-existing, tap side)
#   (o) the helper drops its staleness comparison, so any non-None flag blocks
#       forever — the over-fire direction
#         test_stale_model_tool_flag_does_not_block_the_auto_path
#         test_stale_model_tool_flag_does_not_block_forever  (pre-existing, tap side)
#   (p) the tool-result write hoisted out of the contactId branch and defaulted,
#       as a "make the key always exist" refactor would do (C8W1)
#         test_model_tool_result_without_a_contact_id_does_not_stop_the_auto_path
#         [SOLE GUARD]
#
# (n) and (o) are why the helper is shared rather than copied: breaking the
# window in either direction fails BOTH the auto-path and the tap-path tests, so
# half-guarding one path is unreachable.
# ---------------------------------------------------------------------------


class TestModelToolOpenedContactAlsoStopsTheAutoPath:
    """`escalate_to_human` is a tool the model can call itself. It goes straight
    to `call_escalate_endpoint` and never sees `state`, so a contact it opened
    was invisible to the auto path's stand-down (C7W1).

    Two windows, and the auto path was unguarded in both:
      - in flight: the tool result has not returned. The tap path has refused
        here since Cycle 4; the auto path never read the key.
      - returned: a real contactId came back and iOS joined that chat.
    """

    def test_model_tool_in_flight_stops_the_auto_path(self, config):
        """C7W1 case 4a. Mutations (m) and (n) must fail this."""
        import time as _time

        state = _auto_state()
        state["surfaced_classification"] = "P0"
        state["escalation_tool_in_flight_at"] = _time.monotonic()
        ctx = _auto_ctx(config)
        ctx.triage_classification = "P0"
        spy: list[dict] = []

        # Anti-vacuity: the shared helper must agree the tool is in flight, and
        # the tap path must refuse in this same window.
        assert bidi._model_escalation_tool_in_flight(state), (
            "anti-vacuity: the helper must report the model tool as in flight"
        )

        asyncio.run(_run_auto_triage(ctx, config, state, escalate_spy=spy))

        assert spy == [], (
            "the auto path must stand down while the model's own "
            f"escalate_to_human is in flight; got {len(spy)} /escalate calls"
        )

    def test_stale_model_tool_flag_does_not_block_the_auto_path(self, config):
        """The counterweight, mirroring the tap path's own stale-flag test. A
        flag older than the window must NOT suppress the auto escalation, or a
        tool result that never arrives silently costs the driver their human.

        Mutation: drop the staleness comparison so any non-None flag blocks
        → this test FAILS.
        """
        import time as _time

        state = _auto_state()
        state["surfaced_classification"] = "P0"
        state["escalation_tool_in_flight_at"] = (
            _time.monotonic() - bidi.ESCALATION_TOOL_IN_FLIGHT_S - 1
        )
        ctx = _auto_ctx(config)
        ctx.triage_classification = "P0"
        spy: list[dict] = []

        assert not bidi._model_escalation_tool_in_flight(state), (
            "anti-vacuity: a flag older than the window must not count as in flight"
        )

        asyncio.run(_run_auto_triage(ctx, config, state, escalate_spy=spy))

        assert len(spy) == 1, (
            "a stale model-tool flag must not block the auto escalation; got "
            f"{len(spy)} calls"
        )

    def test_contact_opened_by_the_model_tool_stops_the_auto_path(self, config):
        """C7W1 case 4b, driven through the real output callable so the tool
        result is parsed by the shipped code rather than simulated.

        Mutation (l): remove the `state["escalation_contact_id"]` write at the
        tool-result site → the auto path cannot see the model's contact and
        opens a second one → this test FAILS.
        """
        state = _auto_state()
        state["surfaced_classification"] = "P0"
        state["tool_use_names"] = {"tu-1": "escalate_to_human"}
        # The output callable mirrors classification changes and reads this key
        # with [], not .get() (bidi_app.py:1951). Real session state carries it;
        # _auto_state does not, because the auto-triage path never reads it.
        state["last_classification"] = "P0"
        ctx = _auto_ctx(config)
        ctx.triage_classification = "P0"
        spy: list[dict] = []

        async def _feed_tool_result():
            """Hand the REAL output callable an escalate_to_human tool result,
            using the same factory and event shape as
            TestEscalationToolInFlightFlagWriter, so the result is parsed by
            shipped code rather than simulated here."""
            output_callable = bidi._make_output_callable(
                AsyncMock(), ctx, state, SimpleNamespace(), SimpleNamespace()
            )
            await output_callable({
                "type": "tool_use_stream",
                "current_tool_use": {
                    "name": "escalate_to_human",
                    "toolUseId": "tu-1",
                    "input": {"reason": "brake fault"},
                },
            })
            await output_callable({
                "type": "tool_result",
                "tool_result": {
                    "toolUseId": "tu-1",
                    "content": [{"json": {
                        "contactId": "cnt-model-999",
                        "participantId": "par-1",
                        "participantToken": "tok-1",
                        "severity": "P0",
                    }}],
                },
            })

        async def _go():
            await _feed_tool_result()
            # Anti-vacuity: the model's contact must have been recorded.
            assert state.get("escalation_contact_id") == "cnt-model-999", (
                "anti-vacuity: the tool-result site must record the contact the "
                f"model opened; got {state.get('escalation_contact_id')!r}"
            )
            await _run_auto_triage(ctx, config, state, escalate_spy=spy)

        asyncio.run(_go())

        assert spy == [], (
            "the auto path must stand down on a contact the model's own tool "
            f"opened; got {len(spy)} /escalate calls"
        )

    def test_model_tool_result_without_a_contact_id_does_not_stop_the_auto_path(self, config):
        """C8W1. The counterweight for the tool-result writer — the sibling of
        `test_escalation_without_a_contact_id_does_not_stop_the_auto_path`, which
        does the same job for the `_run_escalate_in_background` writer.

        `escalate_to_human` can come back naming no contact: `/escalate` can
        return without one, and a raised tool (`EscalateError` when
        `ctx.triage_classification is None`, or an HTTP failure) yields a tool
        result whose content is error text. Both are failures of the model's
        escalation — exactly when the driver most needs the auto path to still
        fire. And because `escalation_contact_id` is never cleared, one false
        latch would suppress auto escalation for every later P0 in the session.

        Mutation: hoist the write out of the `and result_data.get("contactId")`
        branch into the block that runs unconditionally for this tool, with the
        defaulting a "make the key always exist" refactor produces ::

            if tool_name == "escalate_to_human":
                state["escalation_tool_in_flight_at"] = None
                state["escalation_contact_id"] = result_data.get("contactId") or "pending"

        → the auto path stands down though no contact exists → this test FAILS.
        Before it existed that mutation survived all 1255 tests.
        """
        state = _auto_state()
        state["surfaced_classification"] = "P0"
        state["tool_use_names"] = {"tu-2": "escalate_to_human"}
        state["last_classification"] = "P0"
        ctx = _auto_ctx(config)
        ctx.triage_classification = "P0"
        spy: list[dict] = []

        async def _go():
            output_callable = bidi._make_output_callable(
                AsyncMock(), ctx, state, SimpleNamespace(), SimpleNamespace()
            )
            await output_callable({
                "type": "tool_use_stream",
                "current_tool_use": {
                    "name": "escalate_to_human",
                    "toolUseId": "tu-2",
                    "input": {"reason": "brake fault"},
                },
            })
            # The tool came back, but named no contact — a failed escalation.
            await output_callable({
                "type": "tool_result",
                "tool_result": {
                    "toolUseId": "tu-2",
                    "content": [{"json": {"error": "EscalateError: no classification"}}],
                },
            })

            # Anti-vacuity: the tool really did report back (so the in-flight
            # flag is cleared and cannot be what lets the auto path through),
            # and nothing was latched.
            assert state.get("escalation_tool_in_flight_at") is None, (
                "anti-vacuity: the tool result must clear the in-flight flag, or "
                "this test would pass via the in-flight branch instead"
            )
            assert not state.get("escalation_contact_id"), (
                "anti-vacuity: a tool result naming no contact must not latch "
                f"one; got {state.get('escalation_contact_id')!r}"
            )

            await _run_auto_triage(ctx, config, state, escalate_spy=spy)

        asyncio.run(_go())

        assert len(spy) == 1, (
            "a model escalation that named no contact did not open one, so the "
            f"auto path must still escalate. Got {len(spy)} calls"
        )
