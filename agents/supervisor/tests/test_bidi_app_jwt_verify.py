"""JWT verification wiring at the bidi session boundary (Fix Group 18 T18.6).

Covers the *wiring* — that `_establish_session` calls verification and that each
`VSA_JWT_VERIFY_MODE` does what it claims. The verification logic itself is covered by
`test_jwt_verify.py` against real RSA keys; here `verify_id_token` is substituted so a
mode's behaviour can be asserted independently of crypto.

Same two departures from `test_bidi_app.py` as `test_bidi_app_avx_preseed.py`, for the
same reasons:

1. **Synchronous, driving `_establish_session` through `asyncio.run`.** `pytest.ini` sets
   `asyncio_mode = auto` but `pytest-asyncio` is NOT installed, so every
   `@pytest.mark.asyncio` test in `test_bidi_app.py` is SKIPPED (13 of 14), and no CI job
   runs `agents/supervisor/tests/` at all. An async test added here would execute nowhere
   — which is the definition of a vacuous test.
2. **Its own file**, so it does not collide with a concurrent session holding
   neighbouring files under `agents/supervisor/`.

The load-bearing property is the *asymmetry*: `observe` must PROCEED on a failure and
`enforce` must REJECT on the same failure. A test that only checks "verification was
called" would pass against a build where both modes proceed — which is the bug that
matters, since it is the silent one.
"""

from __future__ import annotations

import asyncio
import logging
from unittest.mock import AsyncMock

import pytest
from bedrock_agentcore.runtime.context import RequestContext

from agents.supervisor import bidi_app
from agents.supervisor.bidi_app import InvocationError, _establish_session
from agents.supervisor.jwt_verify import ENV_VERIFY_MODE, VerifyOutcome

TOKEN = "header.payload.signature"

BASE_HEADERS = {
    "X-Amzn-Bedrock-AgentCore-Runtime-Custom-User-Token": TOKEN,
    "X-Amzn-Bedrock-AgentCore-Runtime-Custom-Tenant-Id": "meridian-fleet",
    "X-Amzn-Bedrock-AgentCore-Runtime-Custom-Vin": "1FMCU0F70MUA12345",
}

FAILURE = VerifyOutcome(False, "bad_signature")
SUCCESS = VerifyOutcome(True, "verified", sub="sub-aaaa-1111", claims={"sub": "sub-aaaa-1111"})


def _establish():
    ctx = RequestContext(session_id="sid", request_headers=dict(BASE_HEADERS), request=None)
    ws = AsyncMock()
    ws.receive_json = AsyncMock(
        side_effect=AssertionError("receive_json should not be called")
    )
    return asyncio.run(_establish_session(ws, ctx, "sid"))


@pytest.fixture
def spy(monkeypatch):
    """Substitute `verify_id_token` and record the tokens it was handed."""

    calls: list[object] = []
    outcome = {"value": FAILURE}

    def fake(token, **kwargs):
        calls.append(token)
        return outcome["value"]

    monkeypatch.setattr(bidi_app, "verify_id_token", fake)
    return type("Spy", (), {"calls": calls, "outcome": outcome})()


# ─── off ──────────────────────────────────────────────────────────────────────


def test_off_mode_does_not_verify_at_all(monkeypatch, spy):
    """`off` is for local development: no verification call, no rejection."""
    monkeypatch.setenv(ENV_VERIFY_MODE, "off")

    session = _establish()

    assert spy.calls == [], (
        "off mode must not call verify_id_token — it exists so a developer without "
        "network access to JWKS can run the runtime at all."
    )
    assert session.jwt == TOKEN


# ─── observe: verify, log, PROCEED ────────────────────────────────────────────


def test_observe_mode_proceeds_despite_failed_verification(monkeypatch, spy, caplog):
    """The whole point of observe: produce the signal without breaking sessions."""
    monkeypatch.setenv(ENV_VERIFY_MODE, "observe")
    spy.outcome["value"] = FAILURE

    with caplog.at_level(logging.WARNING, logger=bidi_app.log.name):
        session = _establish()

    assert session.jwt == TOKEN, "observe mode must not reject the session"
    assert spy.calls == [TOKEN], "observe mode must still perform the verification"
    assert any(
        "jwt verification FAILED" in r.message and "bad_signature" in r.getMessage()
        for r in caplog.records
    ), (
        "observe mode must log the failure reason — that log line IS the evidence the "
        "enforce decision rests on. Silently proceeding would make observe worthless."
    )


def test_observe_mode_is_the_default_when_unset(monkeypatch, spy, caplog):
    """An unset mode must verify-and-proceed, never silently skip."""
    monkeypatch.delenv(ENV_VERIFY_MODE, raising=False)
    spy.outcome["value"] = FAILURE

    with caplog.at_level(logging.WARNING, logger=bidi_app.log.name):
        session = _establish()

    assert spy.calls == [TOKEN], (
        "with no mode configured the default must be observe (verify + proceed), not "
        "off — a missing env var must not disable a security control."
    )
    assert session.jwt == TOKEN


def test_unrecognised_mode_falls_back_to_observe_not_off(monkeypatch, spy):
    """A typo must degrade to observe, never to off."""
    monkeypatch.setenv(ENV_VERIFY_MODE, "enfroce")  # deliberate typo
    spy.outcome["value"] = FAILURE

    session = _establish()

    assert spy.calls == [TOKEN], (
        "a typo'd mode must still verify. Falling back to off would mean a one-character "
        "mistake silently disables verification."
    )
    assert session.jwt == TOKEN, "fallback is observe, so the session proceeds"


# ─── enforce: verify, log, REJECT ─────────────────────────────────────────────


def test_enforce_mode_rejects_failed_verification(monkeypatch, spy, caplog):
    monkeypatch.setenv(ENV_VERIFY_MODE, "enforce")
    spy.outcome["value"] = FAILURE

    with caplog.at_level(logging.WARNING, logger=bidi_app.log.name):
        with pytest.raises(InvocationError) as excinfo:
            _establish()

    assert spy.calls == [TOKEN]
    assert "bad_signature" in str(excinfo.value), (
        "the rejection must name the reason, or an operator cannot tell an expired "
        "token from a forged one from a JWKS outage."
    )
    assert any("jwt REJECTED" in r.getMessage() for r in caplog.records)


def test_enforce_mode_allows_verified_token(monkeypatch, spy):
    monkeypatch.setenv(ENV_VERIFY_MODE, "enforce")
    spy.outcome["value"] = SUCCESS

    session = _establish()

    assert session.jwt == TOKEN, "a verified token must establish the session normally"


@pytest.mark.parametrize(
    "reason",
    ["expired", "bad_signature", "untrusted_issuer", "jwks_unavailable",
     "crypto_unavailable", "pyjwt_unavailable", "wrong_token_use", "missing_token"],
)
def test_enforce_rejects_every_failure_reason(monkeypatch, spy, reason):
    """No failure class may be treated as a pass.

    `jwks_unavailable`, `crypto_unavailable` and `pyjwt_unavailable` are the ones worth
    being explicit about: they are infrastructure failures, not authentication failures,
    and the tempting "fail open so an outage does not take voice down" is exactly the
    fail-open default this spec's security review flagged elsewhere.
    """
    monkeypatch.setenv(ENV_VERIFY_MODE, "enforce")
    spy.outcome["value"] = VerifyOutcome(False, reason)

    with pytest.raises(InvocationError) as excinfo:
        _establish()

    assert reason in str(excinfo.value)


# ─── log hygiene ──────────────────────────────────────────────────────────────


def test_no_mode_logs_the_token_or_claims(monkeypatch, spy, caplog):
    """A JWT in a log line is a credential in CloudWatch.

    `session_memory.py` states this discipline explicitly; asserted here because the
    verification path is the one place that handles a raw token by design.
    """
    for mode, expect_raise in (("observe", False), ("enforce", True)):
        caplog.clear()
        monkeypatch.setenv(ENV_VERIFY_MODE, mode)
        spy.outcome["value"] = VerifyOutcome(
            False, "bad_signature", sub="sub-secret", claims={"sub": "sub-secret"}
        )
        with caplog.at_level(logging.DEBUG, logger=bidi_app.log.name):
            if expect_raise:
                with pytest.raises(InvocationError):
                    _establish()
            else:
                _establish()

        blob = " ".join(r.getMessage() for r in caplog.records)
        assert TOKEN not in blob, f"mode={mode} leaked the raw token into logs"
        assert "sub-secret" not in blob, f"mode={mode} leaked a claim value into logs"
