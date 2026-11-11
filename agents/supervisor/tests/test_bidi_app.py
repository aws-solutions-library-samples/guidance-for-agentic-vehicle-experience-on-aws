"""Unit tests for the bidi (voice) entrypoint.

Tests the auth + session extraction logic without starting a real
BidiAgent, Bedrock stream, or WebSocket server. The entrypoint's
`websocket_handler` is hard to test in isolation because it expects
a Starlette-style WebSocket object; for validation paths we test
the internal helper `_establish_session` directly, which takes the
same RequestContext + websocket-like object.

Run:
    pytest agents/supervisor/tests/test_bidi_app.py -v

Needs pytest-asyncio (agents/supervisor/requirements-dev.txt). Until 2026-10-01 it was
not installed, so 13 of these 14 tests were skipped and the file exited 0
(issues/2026-10-01-async-tests-inert-and-triage-tests-not-in-ci/).

Mutations verified 2026-10-01 against bidi_app.py (each fails at least one test here,
or in test_bidi_app_jwt_verify.py for B10):
  B1  Default a missing token to a placeholder and proceed.
  B2  Delete the empty-token check.
  B3  Delete the missing-tenant check.
  B4  Delete the missing-VIN check.
  B5  Drop the `type == "session.start"` check.           (survived the old file)
  B6  Stop converting the silent-client timeout to a rejection.
  B7  websocket_handler carries on after a rejection.     (survived the old file)
  B8  websocket_handler closes a rejection with 1000.     (survived the old file)
  B9  websocket_handler does not send the error message.  (survived the old file)
  B10 `enforce` proceeds on a failed verification.
  B11 _establish_session loads the persona.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest
# Hard import, not importorskip: without the plugin these async tests are skipped and
# the file exits 0 (issues/2026-10-01-async-tests-inert-and-triage-tests-not-in-ci/).
import pytest_asyncio.plugin
from bedrock_agentcore.runtime.context import RequestContext

from agents.supervisor.bidi_app import (
    InvocationError,
    _establish_session,
    _normalize_headers,
)
from agents.supervisor.jwt_verify import ENV_VERIFY_MODE, MODE_OBSERVE


@pytest.fixture(autouse=True)
def _observe_mode(monkeypatch):
    """Run in `observe` so the tests do not depend on the caller's shell.

    These tests cover header parsing and the rejection checks that run before
    signature verification. The fake tokens here would fail verification, so under
    `enforce` the happy paths would be rejected for that reason instead. The modes
    themselves are covered by test_bidi_app_jwt_verify.py.
    """
    monkeypatch.setenv(ENV_VERIFY_MODE, MODE_OBSERVE)


@pytest.fixture(autouse=True)
def _require_asyncio_plugin(request):
    """Error every test, rather than skip it, when pytest-asyncio is installed but
    not loaded (`-p no:asyncio`, PYTEST_DISABLE_PLUGIN_AUTOLOAD=1). Fixture setup runs
    before pytest skips an unplugged async test, so this turns the skip into an error.
    """
    # is_registered, not hasplugin("asyncio"): the plugin can be loaded under another
    # name (`-p pytest_asyncio.plugin` with autoload off). Review cycle 3, S2.
    assert request.config.pluginmanager.is_registered(pytest_asyncio.plugin), (
        "pytest-asyncio is not loaded; these async tests would be skipped"
    )


def _ctx(session_id: str = "vsa-sess-test", headers: dict | None = None) -> RequestContext:
    return RequestContext(
        session_id=session_id,
        request_headers=headers or {},
        request=None,
    )


def _fake_websocket(first_message: dict | None = None) -> AsyncMock:
    ws = AsyncMock()
    if first_message is not None:
        ws.receive_json = AsyncMock(return_value=first_message)
    else:
        # Simulate a client that never sends anything.
        async def hang() -> dict:
            await asyncio.sleep(3600)
            return {}

        ws.receive_json = AsyncMock(side_effect=hang)
    return ws


# ---------------------------------------------------------------------------
# Query-param / header path (happy)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_session_from_headers_happy_path():
    ctx = _ctx(
        headers={
            "X-Amzn-Bedrock-AgentCore-Runtime-Custom-User-Token": "jwt.abc",
            "X-Amzn-Bedrock-AgentCore-Runtime-Custom-Tenant-Id": "meridian-fleet",
            "X-Amzn-Bedrock-AgentCore-Runtime-Custom-Vin": "1FMCU0F70MUA12345",
        }
    )
    # No WS receive_json call expected because all fields come from headers.
    ws = AsyncMock()
    ws.receive_json = AsyncMock(side_effect=AssertionError("should not be called"))

    session = await _establish_session(ws, ctx, "vsa-sess-test")

    assert session.tenant_id == "meridian-fleet"
    assert session.vin == "1FMCU0F70MUA12345"
    assert session.jwt == "jwt.abc"
    assert session.session_id == "vsa-sess-test"
    ws.receive_json.assert_not_awaited()


# ---------------------------------------------------------------------------
# Fallback: session.start message
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_session_start_fallback_accepted():
    ctx = _ctx(
        headers={
            "X-Amzn-Bedrock-AgentCore-Runtime-Custom-User-Token": "jwt.abc",
        }
    )
    ws = _fake_websocket(
        {
            "type": "session.start",
            "tenantId": "meridian-fleet",
            "vin": "1FMCU0F70MUA12345",
        }
    )

    session = await _establish_session(ws, ctx, "sid")
    assert session.tenant_id == "meridian-fleet"
    assert session.vin == "1FMCU0F70MUA12345"
    assert session.jwt == "jwt.abc"


@pytest.mark.asyncio
async def test_session_start_can_supply_jwt_too():
    ctx = _ctx(headers={})
    ws = _fake_websocket(
        {
            "type": "session.start",
            "tenantId": "meridian-fleet",
            "vin": "1FMCU0F70MUA12345",
            "jwt": "from.payload.token",
        }
    )

    session = await _establish_session(ws, ctx, "sid")
    assert session.jwt == "from.payload.token"


# ---------------------------------------------------------------------------
# Rejections
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rejects_wrong_session_start_type():
    """A first message that is not session.start is rejected, even when it carries
    every field a session.start would.

    The message includes tenantId, vin and jwt so that the type check is the only
    thing that can reject it. The earlier version sent only `type` and `data` and
    matched "session.start", which the later "Missing tenantId (query param or
    session.start)" error also contains, so deleting the type check left it green.
    """
    ctx = _ctx(headers={})
    ws = _fake_websocket(
        {
            "type": "audio.chunk",
            "data": "AAAA",
            "tenantId": "meridian-fleet",
            "vin": "1FMCU0F70MUA12345",
            "jwt": "from.payload.token",
        }
    )

    with pytest.raises(InvocationError, match="First message must be"):
        await _establish_session(ws, ctx, "sid")


@pytest.mark.asyncio
async def test_rejects_missing_tenant_after_fallback():
    ctx = _ctx(
        headers={
            "X-Amzn-Bedrock-AgentCore-Runtime-Custom-User-Token": "jwt.abc",
        }
    )
    ws = _fake_websocket(
        {
            "type": "session.start",
            "vin": "1FMCU0F70MUA12345",
            # tenantId missing
        }
    )
    with pytest.raises(InvocationError, match="tenantId"):
        await _establish_session(ws, ctx, "sid")


@pytest.mark.asyncio
async def test_rejects_missing_vin_after_fallback():
    ctx = _ctx(
        headers={
            "X-Amzn-Bedrock-AgentCore-Runtime-Custom-User-Token": "jwt.abc",
        }
    )
    ws = _fake_websocket({"type": "session.start", "tenantId": "meridian-fleet"})
    with pytest.raises(InvocationError, match="vin"):
        await _establish_session(ws, ctx, "sid")


@pytest.mark.asyncio
async def test_rejects_missing_user_token():
    ctx = _ctx(
        headers={
            "X-Amzn-Bedrock-AgentCore-Runtime-Custom-Tenant-Id": "meridian-fleet",
            "X-Amzn-Bedrock-AgentCore-Runtime-Custom-Vin": "1FMCU0F70MUA12345",
        }
    )
    ws = AsyncMock()
    ws.receive_json = AsyncMock(side_effect=AssertionError("should not be called"))

    with pytest.raises(InvocationError, match="user token"):
        await _establish_session(ws, ctx, "sid")


@pytest.mark.asyncio
async def test_rejects_empty_user_token():
    ctx = _ctx(
        headers={
            "X-Amzn-Bedrock-AgentCore-Runtime-Custom-User-Token": "   ",
            "X-Amzn-Bedrock-AgentCore-Runtime-Custom-Tenant-Id": "meridian-fleet",
            "X-Amzn-Bedrock-AgentCore-Runtime-Custom-Vin": "1FMCU0F70MUA12345",
        }
    )
    ws = AsyncMock()
    with pytest.raises(InvocationError, match="empty"):
        await _establish_session(ws, ctx, "sid")


@pytest.mark.asyncio
async def test_timeout_on_silent_client():
    """If no query params AND no session.start arrives within 10s,
    reject. Test uses a very short monkey-patched timeout to keep
    the test runtime reasonable."""
    import agents.supervisor.bidi_app as mod

    ctx = _ctx(headers={})
    ws = _fake_websocket()  # hangs forever

    # Monkey-patch asyncio.wait_for to use a short timeout for this test
    # without changing the production code path.
    original_wait_for = asyncio.wait_for

    async def fast_wait_for(awaitable, timeout):
        return await original_wait_for(awaitable, 0.05)

    mod.asyncio.wait_for = fast_wait_for
    try:
        with pytest.raises(InvocationError, match="Timed out"):
            await _establish_session(ws, ctx, "sid")
    finally:
        mod.asyncio.wait_for = original_wait_for


# ---------------------------------------------------------------------------
# Handler-level rejection: a rejected session stops before any model work
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("verify_mode", ["off", "observe", "enforce"])
async def test_handler_closes_rejected_session_before_any_model_work(monkeypatch, verify_mode):
    """websocket_handler must close a rejected session with 1008 and return.

    Parametrized over VSA_JWT_VERIFY_MODE (security review cycle 1, S4): a missing
    token is rejected before verification runs, so the outcome must not depend on
    the mode, including `off`.

    The _establish_session tests above prove the rejection is raised. This proves
    the handler acts on it: if the handler logged the error and carried on, every
    rejection test above would still pass while the session went on to build the
    model and read the driver's vehicle data. Added 2026-10-01 after a mutation
    that dropped the handler's `return` survived the rest of this file.
    """
    import agents.supervisor.bidi_app as mod

    monkeypatch.setenv(ENV_VERIFY_MODE, verify_mode)

    def _reached(name):
        def _fail(*_args, **_kwargs):
            raise AssertionError(f"{name} reached after a rejected session")
        return _fail

    monkeypatch.setattr(mod.Config, "from_env", _reached("Config.from_env"))
    monkeypatch.setattr(mod, "BidiNovaSonicModel", _reached("BidiNovaSonicModel"))
    monkeypatch.setattr(mod, "_fetch_vehicle_context", _reached("_fetch_vehicle_context"))
    monkeypatch.setattr(mod, "_fetch_tenant_config_item", _reached("_fetch_tenant_config_item"))

    ctx = _ctx(
        headers={
            # No user token.
            "X-Amzn-Bedrock-AgentCore-Runtime-Custom-Tenant-Id": "meridian-fleet",
            "X-Amzn-Bedrock-AgentCore-Runtime-Custom-Vin": "1FMCU0F70MUA12345",
            "X-Amzn-Bedrock-AgentCore-Runtime-Custom-VehicleId": "MRDN0000000000015",
        }
    )
    ws = AsyncMock()
    ws.receive_json = AsyncMock(side_effect=AssertionError("should not be called"))

    await mod.websocket_handler(ws, ctx)

    ws.accept.assert_awaited_once()
    ws.close.assert_awaited_once()
    assert ws.close.await_args.kwargs["code"] == 1008
    sent = [c.args[0] for c in ws.send_json.await_args_list]
    assert len(sent) == 1, sent
    assert sent[0]["type"] == "error"
    assert "user token" in sent[0]["message"].lower()


# ---------------------------------------------------------------------------
# Header case normalization
# ---------------------------------------------------------------------------


def test_header_normalization():
    h = _normalize_headers({"Foo-Bar": "baz", "HELLO": "world"})
    assert h == {"foo-bar": "baz", "hello": "world"}


@pytest.mark.asyncio
async def test_mixed_case_headers_accepted():
    """AgentCore normalizes header casing, but test both paths to be safe."""
    ctx = _ctx(
        headers={
            "x-amzn-bedrock-agentcore-runtime-custom-user-token": "jwt.abc",
            "X-AMZN-BEDROCK-AGENTCORE-RUNTIME-CUSTOM-TENANT-ID": "meridian-fleet",
            "X-Amzn-Bedrock-AgentCore-Runtime-Custom-Vin": "1FMCU0F70MUA12345",
        }
    )
    ws = AsyncMock()
    session = await _establish_session(ws, ctx, "sid")
    assert session.tenant_id == "meridian-fleet"
    assert session.vin == "1FMCU0F70MUA12345"
    assert session.jwt == "jwt.abc"


# ---------------------------------------------------------------------------
# Persona-loader integration (Group 4.C)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_session_init_extracts_persona_and_customer_id():
    """_establish_session reads persona-id and customer-id headers."""
    ctx = _ctx(
        headers={
            "X-Amzn-Bedrock-AgentCore-Runtime-Custom-User-Token": "jwt.tok",
            "X-Amzn-Bedrock-AgentCore-Runtime-Custom-Tenant-Id": "meridian-fleet",
            "X-Amzn-Bedrock-AgentCore-Runtime-Custom-Vin": "1FMCU0F70MUA12345",
            "X-Amzn-Bedrock-AgentCore-Runtime-Custom-Persona-Id": "vehicle_owner",
            "X-Amzn-Bedrock-AgentCore-Runtime-Custom-Customer-Id": "cust-007",
        }
    )
    ws = AsyncMock()
    ws.receive_json = AsyncMock(side_effect=AssertionError("should not be called"))

    session = await _establish_session(ws, ctx, "vsa-sess-test")

    assert session.persona_id == "vehicle_owner"
    assert session.customer_id == "cust-007"


@pytest.mark.asyncio
async def test_session_init_defaults_persona_when_header_absent():
    """persona_id defaults to fleet_driver when header is absent."""
    ctx = _ctx(
        headers={
            "X-Amzn-Bedrock-AgentCore-Runtime-Custom-User-Token": "jwt.tok",
            "X-Amzn-Bedrock-AgentCore-Runtime-Custom-Tenant-Id": "meridian-fleet",
            "X-Amzn-Bedrock-AgentCore-Runtime-Custom-Vin": "1FMCU0F70MUA12345",
        }
    )
    ws = AsyncMock()
    ws.receive_json = AsyncMock(side_effect=AssertionError("should not be called"))

    session = await _establish_session(ws, ctx, "vsa-sess-test")

    assert session.persona_id == "fleet_driver"
    assert session.customer_id is None


@pytest.mark.asyncio
async def test_establish_session_does_not_load_persona():
    """_establish_session only parses and authenticates; it must not load the persona.

    The persona snapshot is loaded later, in websocket_handler, after the session has
    been accepted. Loading it here would read the customer's data before the
    rejection checks below have run.

    Renamed 2026-10-01 from test_session_init_calls_persona_loader. Its docstring
    claimed "load_persona is called once with header-extracted args" but the body
    never asserted on the mock, and the test had never executed (pytest-asyncio was
    not installed). See issues/2026-10-01-async-tests-inert-and-triage-tests-not-in-ci/.
    """
    from unittest.mock import patch, MagicMock

    ctx = _ctx(
        headers={
            "X-Amzn-Bedrock-AgentCore-Runtime-Custom-User-Token": "jwt.tok",
            "X-Amzn-Bedrock-AgentCore-Runtime-Custom-Tenant-Id": "meridian-fleet",
            "X-Amzn-Bedrock-AgentCore-Runtime-Custom-Vin": "1FMCU0F70MUA12345",
            "X-Amzn-Bedrock-AgentCore-Runtime-Custom-Persona-Id": "fleet_driver",
            "X-Amzn-Bedrock-AgentCore-Runtime-Custom-Customer-Id": "cust-42",
        }
    )
    ws = AsyncMock()
    ws.receive_json = AsyncMock(side_effect=AssertionError("should not be called"))

    with patch("agents.supervisor.bidi_app._persona_loader.load_persona", new_callable=AsyncMock) as mock_load, \
         patch("agents.supervisor.bidi_app.build_persona_loader_config") as mock_cfg:
        mock_cfg.return_value = MagicMock()

        session = await _establish_session(ws, ctx, "vsa-sess-test")

    mock_load.assert_not_awaited()
    mock_cfg.assert_not_called()
    assert session.persona_id == "fleet_driver"
    assert session.customer_id == "cust-42"
    assert session.vin == "1FMCU0F70MUA12345"
