"""Behavioural tests for escalation tool and bidi caller contract.

Covers the properties review C1 and C2 found unguarded:

  C1: bidi_app.py's call_escalate_endpoint invocation must NOT pass
      dispatch_roadside=True, so the Lambda's tenant-config flag governs.

  C2: The severity *binding* — severity forwarded to call_escalate_endpoint
      equals ctx.triage_classification, not a hardcoded value.
      The fail-closed raise when ctx.triage_classification is None is
      asserted by invoking the tool, not by reading its source.

These are *behavioural* tests: they invoke the tools or mock the call at the
network boundary to observe what arguments flow through. They deliberately
complement (and do NOT duplicate) the AST-structural guards in
test_triage_classification_provenance.py (T1.4), which cover signature shape.

Run: .venv/bin/python3 -m pytest agents/supervisor/tests/test_escalate_behaviour.py -v
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch, call as mock_call

import pytest

from agents.supervisor.config import Config
from agents.supervisor.session import SessionContext
from agents.supervisor.tools.escalate import (
    EscalateError,
    make_escalate_tool,
    make_service_escalate_tool,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def config() -> Config:
    return Config.from_env()


@pytest.fixture
def ctx(config: Config) -> SessionContext:
    """Minimal context — vin and vehicle_id are required by /escalate."""
    return SessionContext(
        tenant_id=config.tenant_id,
        session_id="vsa-behav-test-001",
        jwt="fake-jwt-not-used",
        persona_id="fleet_driver",
        vin="1HGBH41JXMN000046",
        vehicle_id="VEH-0025",
    )


def _fake_escalate_response(severity: str = "P0") -> dict:
    """Minimal /escalate response body matching EscalateResult fields."""
    return {
        "contactId": "cnt-abc-123",
        "participantId": "par-abc-123",
        "participantToken": "tok-abc-123",
        "connectionExpiry": "2099-01-01T00:00:00Z",
        "severity": severity,
        "rsaDispatched": False,
        "rsaActionId": None,
    }


# ---------------------------------------------------------------------------
# Accept 1: fail-closed raise when triage_classification is None
# ---------------------------------------------------------------------------


class TestEscalateFailClosed:
    def test_raises_when_triage_classification_is_none(
        self, ctx: SessionContext, config: Config
    ):
        """Invoking escalate_to_human WITHOUT a prior triage verdict MUST raise.

        This is Accept 1 of F1.2. The tool is called directly — not inferred
        from source substrings — so demoting the raise to log.warning or
        moving it into an unreachable branch is caught here, not just in the
        AST guard.

        Mutation: raise EscalateError → log.warning → this test MUST fail.
        """
        assert ctx.triage_classification is None, (
            "fixture precondition: no triage has run on this context"
        )
        escalate = make_escalate_tool(ctx, config)
        with pytest.raises(EscalateError, match="triage_classification is not set"):
            escalate(reason="engine knocking")

    def test_does_not_mutate_classification_when_refusing(
        self, ctx: SessionContext, config: Config
    ):
        """The refusal must not leave a phantom classification behind.

        Mirrors the book-tool precautionary check — if we set the field on
        our way out, a later call in the same session would find it populated
        and proceed, defeating the fail-closed guard.
        """
        escalate = make_escalate_tool(ctx, config)
        # Anchored (review Cycle 2 C2W5): an unanchored raises() was also
        # satisfied by an EscalateError from further down the call — e.g. a
        # network failure — so it could not tell the guard firing from the
        # guard being gone. Transport pinned so a broken guard cannot make a
        # real outbound request either.
        with patch("requests.post", side_effect=AssertionError("guard bypassed: HTTP attempted")), \
                pytest.raises(EscalateError, match="triage_classification is not set"):
            escalate(reason="engine knocking")
        assert ctx.triage_classification is None, (
            "escalate_to_human must not fabricate a classification on refusal"
        )


# ---------------------------------------------------------------------------
# Accept 2: severity binding — forwarded value equals triage_classification
# ---------------------------------------------------------------------------


class TestSeverityBinding:
    """The severity forwarded to call_escalate_endpoint equals
    ctx.triage_classification.

    Asserted for two distinct verdicts (P0 and P2) so a hardcode of either
    value cannot pass both assertions.

    Mutation: severity=ctx.triage_classification → severity="P0" →
    the P2 assertion MUST fail.
    """

    def _call_with_classification(
        self, ctx: SessionContext, config: Config, verdict: str
    ) -> dict:
        """Set classification, call the tool, return the captured payload."""
        ctx.triage_classification = verdict
        captured_payloads: list[dict] = []

        def _fake_post(url, json, headers, timeout):
            captured_payloads.append(json)
            resp = MagicMock()
            resp.status_code = 200
            resp.json.return_value = _fake_escalate_response(severity=verdict)
            return resp

        escalate = make_escalate_tool(ctx, config)
        with patch("requests.post", side_effect=_fake_post):
            escalate(reason="brake fade at 70 mph")

        assert len(captured_payloads) == 1, (
            "Expected exactly one /escalate POST"
        )
        return captured_payloads[0]

    def test_severity_matches_p0_classification(
        self, ctx: SessionContext, config: Config
    ):
        payload = self._call_with_classification(ctx, config, "P0")
        assert payload["severity"] == "P0", (
            f"Expected severity 'P0' from ctx.triage_classification but got "
            f"{payload['severity']!r}"
        )

    def test_severity_matches_p2_classification(
        self, ctx: SessionContext, config: Config
    ):
        """P2 assertion prevents a 'P0' hardcode from satisfying this class."""
        payload = self._call_with_classification(ctx, config, "P2")
        assert payload["severity"] == "P2", (
            f"Expected severity 'P2' from ctx.triage_classification but got "
            f"{payload['severity']!r}"
        )

    def test_classification_is_the_only_severity_source(
        self, ctx: SessionContext, config: Config
    ):
        """P0 and P2 from the same ctx confirm the value flows through, not
        from a hardcode — two calls from a single context object are
        sufficient because ctx.triage_classification is mutated between calls.
        """
        captured: list[str] = []

        def _fake_post(url, json, headers, timeout):
            captured.append(json["severity"])
            resp = MagicMock()
            resp.status_code = 200
            resp.json.return_value = _fake_escalate_response(severity=json["severity"])
            return resp

        escalate = make_escalate_tool(ctx, config)
        for verdict in ("P0", "P2"):
            ctx.triage_classification = verdict
            with patch("requests.post", side_effect=_fake_post):
                escalate(reason="check engine light")

        assert captured == ["P0", "P2"], (
            f"Expected severity to track ctx.triage_classification across calls; "
            f"got {captured}"
        )


# ---------------------------------------------------------------------------
# Accept 3: bidi caller payload — dispatchRoadside absent when no explicit arg
# ---------------------------------------------------------------------------


class TestBidiCallerPayload:
    """The payload call_escalate_endpoint builds must NOT include
    dispatchRoadside when dispatch_roadside is None (the default).

    This is the seam C1 identified: neither the supervisor tests nor the
    Lambda tests previously asserted what the bidi caller actually sends.
    The Lambda's precedence rule is:
      service → False
      elif "dispatchRoadside" in body → body value
      else → tenant flag
    So if dispatchRoadside is present in the body (even False), the tenant
    flag is bypassed. Omitting it entirely lets the flag govern.

    Mutation: re-add dispatch_roadside=True at bidi_app.py:3938 →
    this test MUST fail (the body will contain dispatchRoadside=True,
    bypassing the tenant flag's false default).
    """

    def test_no_dispatch_roadside_in_body_when_arg_omitted(
        self, ctx: SessionContext, config: Config
    ):
        """call_escalate_endpoint with dispatch_roadside=None must NOT send
        dispatchRoadside in the POST body.
        """
        from agents.supervisor.tools.escalate import call_escalate_endpoint

        ctx.triage_classification = "P0"
        captured_payloads: list[dict] = []

        def _fake_post(url, json, headers, timeout):
            captured_payloads.append(json)
            resp = MagicMock()
            resp.status_code = 200
            resp.json.return_value = _fake_escalate_response()
            return resp

        with patch("requests.post", side_effect=_fake_post):
            call_escalate_endpoint(
                ctx,
                config,
                severity="P0",
                reason="P0 auto-escalation",
                summary="auto-escalation summary",
                # dispatch_roadside intentionally omitted — mirrors the
                # bidi_app.py caller after F1.1 removes the True pin.
                display_name=None,
            )

        assert len(captured_payloads) == 1
        payload = captured_payloads[0]
        assert "dispatchRoadside" not in payload, (
            "dispatchRoadside must be absent from the POST body when "
            "dispatch_roadside=None so the Lambda's tenant flag governs. "
            f"Got payload keys: {list(payload.keys())}"
        )

    def test_dispatch_roadside_true_bypasses_tenant_flag(
        self, ctx: SessionContext, config: Config
    ):
        """Confirm the mechanism: when dispatch_roadside=True is passed
        explicitly, dispatchRoadside DOES appear in the body — which is
        exactly what bidi_app.py:3938's old pin caused.

        This test is the positive control for the mutation. After F1.1,
        bidi_app.py omits the arg, so the Lambda's tenant flag governs.
        If the pin were re-added (mutation), the bidi caller would send
        dispatchRoadside=True, bypassing the flag — and
        test_bidi_caller_omits_dispatch_roadside_in_payload below would fail.
        """
        from agents.supervisor.tools.escalate import call_escalate_endpoint

        ctx.triage_classification = "P0"
        captured_payloads: list[dict] = []

        def _fake_post(url, json, headers, timeout):
            captured_payloads.append(json)
            resp = MagicMock()
            resp.status_code = 200
            resp.json.return_value = _fake_escalate_response()
            return resp

        with patch("requests.post", side_effect=_fake_post):
            call_escalate_endpoint(
                ctx,
                config,
                severity="P0",
                reason="P0 auto-escalation",
                dispatch_roadside=True,  # explicit True — the old pin's effect
                display_name=None,
            )

        assert len(captured_payloads) == 1
        payload = captured_payloads[0]
        assert payload.get("dispatchRoadside") is True, (
            "When dispatch_roadside=True is passed, the body MUST contain "
            "dispatchRoadside=True — this is how the old pin bypassed the flag."
        )

    def test_bidi_caller_omits_dispatch_roadside_in_payload(
        self, ctx: SessionContext, config: Config
    ):
        """Spy on call_escalate_endpoint at the bidi_app boundary to verify
        the auto-escalation caller does NOT pass dispatch_roadside=True.

        This is the seam-crossing test C1 asked for: the bidi caller is
        inspected, not inferred from escalate.py alone.

        Mutation: re-add dispatch_roadside=True at bidi_app.py:3938 →
        the captured kwargs will contain dispatch_roadside=True → FAILS.
        """
        import asyncio
        from unittest.mock import AsyncMock, patch as async_patch
        import agents.supervisor.bidi_app as bidi_module

        ctx.triage_classification = "P0"
        captured_kwargs: list[dict] = []

        def _spy_call_escalate_endpoint(*args, **kwargs):
            captured_kwargs.append(kwargs)
            return _fake_escalate_response()

        # We need to call _run_escalate_in_background; it needs a websocket mock.
        mock_ws = AsyncMock()
        mock_ws.send = AsyncMock()

        with patch.object(
            bidi_module, "call_escalate_endpoint", side_effect=_spy_call_escalate_endpoint
        ):
            asyncio.run(
                bidi_module._run_escalate_in_background(
                    ctx=ctx,
                    config=config,
                    websocket=mock_ws,
                    classification="P0",
                    user_utterance="engine overheating",
                    booking_result=None,
                    chat_diagnosis=None,
                    display_name=None,
                )
            )

        assert len(captured_kwargs) >= 1, (
            "Expected _run_escalate_in_background to call call_escalate_endpoint"
        )
        kw = captured_kwargs[0]
        assert "dispatch_roadside" not in kw or kw.get("dispatch_roadside") is None, (
            f"bidi auto-escalation must NOT pass dispatch_roadside=True. "
            f"Got dispatch_roadside={kw.get('dispatch_roadside')!r}. "
            f"If this fails on the mutation, the fix is working correctly."
        )

    def test_narration_omits_roadside_sentence_when_rsa_not_dispatched(
        self, ctx: SessionContext, config: Config
    ):
        """_build_narration is conditional on rsa_dispatched=True.

        Verifying this here — as F1.1 Accept 3 requests — means the fix
        self-corrects once dispatch_roadside is no longer pinned True.
        """
        from agents.supervisor.tools.escalate import _build_narration

        narration_no_rsa = _build_narration(
            severity="P0", rsa_dispatched=False, display_name=None
        )
        narration_with_rsa = _build_narration(
            severity="P0", rsa_dispatched=True, display_name=None
        )

        assert "roadside" not in narration_no_rsa.lower(), (
            "Narration must NOT mention roadside when rsaDispatched=False. "
            f"Got: {narration_no_rsa!r}"
        )
        assert "roadside" in narration_with_rsa.lower(), (
            "Narration MUST mention roadside when rsaDispatched=True. "
            f"Got: {narration_with_rsa!r}"
        )


# ---------------------------------------------------------------------------
# Accept 4: escalate_for_service never carries a caller-supplied severity
# ---------------------------------------------------------------------------


class TestServiceEscalationContract:
    """escalate_for_service must:
      1. Send escalationType="service" in the POST body.
      2. Never send a caller-supplied severity (no LLM argument; the service
         path structurally cannot carry one — spec D1).
    """

    def test_service_escalation_sends_escalation_type(
        self, ctx: SessionContext, config: Config
    ):
        captured_payloads: list[dict] = []

        def _fake_post(url, json, headers, timeout):
            captured_payloads.append(json)
            resp = MagicMock()
            resp.status_code = 200
            resp.json.return_value = _fake_escalate_response(severity="P3")
            return resp

        escalate_for_service = make_service_escalate_tool(ctx, config)
        with patch("requests.post", side_effect=_fake_post):
            escalate_for_service(reason="billing question")

        assert len(captured_payloads) == 1
        payload = captured_payloads[0]
        assert payload.get("escalationType") == "service", (
            f"escalate_for_service must send escalationType='service'; "
            f"got {payload.get('escalationType')!r}"
        )

    def test_service_escalation_signature_has_no_severity_param(
        self, ctx: SessionContext, config: Config
    ):
        """The @tool function exposed to the LLM must not accept severity.

        This is the structural half of the contract (the AST guard in
        test_triage_classification_provenance.py is the authoritative check;
        this confirms the same property behaviourally by inspecting the
        live callable's signature at runtime).
        """
        import inspect

        escalate_for_service = make_service_escalate_tool(ctx, config)
        sig = inspect.signature(escalate_for_service)
        assert "severity" not in sig.parameters, (
            f"escalate_for_service must not expose a 'severity' parameter "
            f"to the LLM. Found parameters: {list(sig.parameters)}"
        )
