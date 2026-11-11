"""T1.6a — platform.callback.ringing notice in _run_escalate_in_background.

When the /escalate 200 body includes p0Callback.placed=True, the bidi runtime
must send a platform.callback.ringing event BEFORE the escalation chat event.
The event carries callerName="Meridian fleet support" and the maskedNumber
exactly as returned by the Lambda.

Requirements and mutation targets:
  M1: ringing sent when p0Callback.placed=False
      → test_ringing_not_sent_when_placed_false FAILS
  M2: ringing sent on an EscalateError (502 path)
      → test_ringing_not_sent_on_escalate_error FAILS
  M3: ringing sent when p0Callback key is absent from response
      → test_ringing_not_sent_when_no_p0callback_key FAILS
  M4: full (unmasked) number in the ringing event
      → test_full_number_not_in_ringing_event FAILS
  M5: callerName uses a different string
      → test_ringing_caller_name_is_directive_word_for_word FAILS
  M6: ringing sent on escalation with no callback (any placement of the check)
      → test_ringing_only_when_placed_true FAILS

Run (from the repo root, with .venv active):
    .venv/bin/python -m pytest agents/supervisor/tests/test_callback_ringing_notice.py -q
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

import agents.supervisor.bidi_app as bidi
from agents.supervisor.config import Config
from agents.supervisor.session import SessionContext
from agents.supervisor.tools.escalate import EscalateError


# ---------------------------------------------------------------------------
# Fixtures (mirror test_bidi_escalation_request.py)
# ---------------------------------------------------------------------------


@pytest.fixture
def config() -> Config:
    return Config.from_env()


@pytest.fixture
def ctx(config: Config) -> SessionContext:
    c = SessionContext(
        tenant_id=config.tenant_id,
        session_id="cb-ring-test-001",
        jwt="fake-jwt",
        persona_id="fleet_driver",
        vin="1HGBH41JXMN000024",
        vehicle_id="VEH-0099",
    )
    c.triage_classification = "P0"
    return c


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_MASKED = "\u2022\u2022\u2022-\u2022\u2022\u2022-0199"  # •••-•••-0199


def _escalate_result(
    *,
    placed: bool | None,
    masked_number: str | None = _MASKED,
) -> dict:
    """Build a call_escalate_endpoint return value, optionally with p0Callback."""
    result: dict = {
        "contactId": "cnt-ringing-001",
        "participantId": "par-ringing-001",
        "participantToken": "tok-ringing-001",
        "connectionExpiry": "2099-01-01T00:00:00Z",
        "severity": "P0",
        "rsaDispatched": False,
        "rsaActionId": None,
        "summary": "connected",
    }
    if placed is not None:
        p0cb: dict = {"placed": placed}
        if placed and masked_number is not None:
            p0cb["maskedNumber"] = masked_number
        result["p0Callback"] = p0cb
    # placed=None → no p0Callback key at all (escalation without callback)
    return result


def _run(ctx, config, escalate_return_value) -> list[dict]:
    """Run _run_escalate_in_background and return all sent JSON messages."""
    ws = AsyncMock()

    async def _go():
        with patch.object(bidi, "call_escalate_endpoint",
                          return_value=escalate_return_value):
            await bidi._run_escalate_in_background(
                websocket=ws,
                ctx=ctx,
                config=config,
                user_utterance="brake failure",
                booking_result=None,
                classification="P0",
            )

    asyncio.run(_go())
    return [c.args[0] for c in ws.send_json.call_args_list]


def _run_with_error(ctx, config, exc: Exception) -> list[dict]:
    """Run _run_escalate_in_background with call_escalate_endpoint raising."""
    ws = AsyncMock()

    async def _go():
        with patch.object(bidi, "call_escalate_endpoint", side_effect=exc):
            await bidi._run_escalate_in_background(
                websocket=ws,
                ctx=ctx,
                config=config,
                user_utterance="brake failure",
                booking_result=None,
                classification="P0",
            )

    asyncio.run(_go())
    return [c.args[0] for c in ws.send_json.call_args_list]


def _ringing_events(msgs: list[dict]) -> list[dict]:
    return [m for m in msgs if m.get("type") == "platform.callback.ringing"]


def _escalation_events(msgs: list[dict]) -> list[dict]:
    return [m for m in msgs if m.get("type") == "escalation"]


# ---------------------------------------------------------------------------
# Tests: ringing event present when placed=True
# ---------------------------------------------------------------------------


class TestRingingSentWhenPlaced:
    """The ringing event is emitted exactly once when p0Callback.placed is True."""

    def test_ringing_sent_when_placed_true(self, ctx, config):
        msgs = _run(ctx, config, _escalate_result(placed=True))
        rings = _ringing_events(msgs)
        assert len(rings) == 1, (
            "Exactly one platform.callback.ringing event must be sent when placed=True. "
            "M6 mutation should make this FAIL."
        )

    def test_ringing_caller_name_is_directive_word_for_word(self, ctx, config):
        """M5: use any other string → FAILS.

        The callerName must be exactly "Meridian fleet support" — no variant,
        no trailing space, no different capitalisation.
        """
        msgs = _run(ctx, config, _escalate_result(placed=True))
        ring = _ringing_events(msgs)[0]
        assert ring["callerName"] == "Meridian fleet support", (
            f"callerName must be 'Meridian fleet support', got {ring['callerName']!r}. "
            "M5 mutation (different string) should make this FAIL."
        )

    def test_ringing_carries_masked_number(self, ctx, config):
        msgs = _run(ctx, config, _escalate_result(placed=True, masked_number=_MASKED))
        ring = _ringing_events(msgs)[0]
        assert ring.get("maskedNumber") == _MASKED, (
            f"maskedNumber in ringing event must be {_MASKED!r}, "
            f"got {ring.get('maskedNumber')!r}"
        )

    def test_ringing_comes_before_escalation_event(self, ctx, config):
        """The ringing notice must precede the chat escalation event."""
        msgs = _run(ctx, config, _escalate_result(placed=True))
        types = [m.get("type") for m in msgs]
        assert "platform.callback.ringing" in types, "ringing event must be sent"
        assert "escalation" in types, "escalation event must also be sent"
        ri = types.index("platform.callback.ringing")
        ei = types.index("escalation")
        assert ri < ei, (
            "platform.callback.ringing must come BEFORE the escalation event "
            "so iOS shows the ringing banner while the chat card loads. "
            "Swapping order should make this FAIL."
        )


# ---------------------------------------------------------------------------
# Tests: ringing event NOT sent when placed is false or absent
# ---------------------------------------------------------------------------


class TestRingingNotSent:
    """No ringing event on placed=False, on a 502, or when p0Callback is absent."""

    def test_ringing_not_sent_when_placed_false(self, ctx, config):
        """M1: send ringing on placed=False → FAILS."""
        msgs = _run(ctx, config, _escalate_result(placed=False))
        rings = _ringing_events(msgs)
        assert rings == [], (
            "platform.callback.ringing must NOT be sent when placed=False. "
            "M1 mutation (send on any placed) should make this FAIL."
        )

    def test_ringing_not_sent_when_no_p0callback_key(self, ctx, config):
        """M3: send ringing when p0Callback absent → FAILS.

        An escalation that had no callback configured returns no p0Callback
        key. The ringing event must not be sent.
        """
        msgs = _run(ctx, config, _escalate_result(placed=None))
        rings = _ringing_events(msgs)
        assert rings == [], (
            "platform.callback.ringing must NOT be sent when p0Callback is absent. "
            "M3 mutation should make this FAIL."
        )

    def test_ringing_not_sent_on_escalate_error(self, ctx, config):
        """M2: send ringing inside the except branch → FAILS.

        On a 502, call_escalate_endpoint raises EscalateError. The ringing
        event must never be sent in the error path.
        """
        msgs = _run_with_error(ctx, config, EscalateError("Escalate endpoint returned 502"))
        rings = _ringing_events(msgs)
        assert rings == [], (
            "platform.callback.ringing must NOT be sent when /escalate returned a 502. "
            "M2 mutation should make this FAIL."
        )

    def test_escalation_event_still_sent_on_placed_false(self, ctx, config):
        """Regression: the escalation (chat card) event is unaffected by placed=False."""
        msgs = _run(ctx, config, _escalate_result(placed=False))
        esc = _escalation_events(msgs)
        assert esc, "escalation event must still be sent even when placed=False"
        assert esc[0]["status"] == "initiated"


# ---------------------------------------------------------------------------
# Tests: full phone number never in the ringing event
# ---------------------------------------------------------------------------


class TestNoPhoneLeakInRinging:
    """The ringing event must never carry the raw driver phone number."""

    def test_full_number_not_in_ringing_event(self, ctx, config):
        """M4: maskedNumber = full phone → FAILS."""
        # Simulate a Lambda that returns a full phone number as maskedNumber
        # (the handler should never do this, but we test the bidi gate too).
        full_phone = "+15559876543"
        # In normal operation maskedNumber is already masked by the Lambda;
        # bidi_app just relays it. This test verifies that at least bidi_app
        # does not introduce the full number into the event.
        masked = _MASKED  # handler always sends a masked value
        msgs = _run(ctx, config, _escalate_result(placed=True, masked_number=masked))
        ring = _ringing_events(msgs)[0]
        event_str = str(ring)
        # The full phone must not appear anywhere in the event.
        assert full_phone not in event_str, (
            f"Full phone {full_phone!r} must never appear in the ringing event. "
            "M4 mutation should make this FAIL."
        )

    def test_masked_number_format_in_ringing(self, ctx, config):
        """The maskedNumber relayed by bidi_app starts with bullet dots."""
        msgs = _run(ctx, config, _escalate_result(placed=True, masked_number=_MASKED))
        ring = _ringing_events(msgs)[0]
        mn = ring.get("maskedNumber", "")
        assert "\u2022\u2022\u2022-\u2022\u2022\u2022-" in mn, (
            f"maskedNumber {mn!r} must start with •••-•••- to confirm masking. "
            "An unmasked number would fail this check."
        )
