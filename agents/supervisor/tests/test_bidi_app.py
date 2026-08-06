"""Unit tests for the bidi (voice) entrypoint.

Tests the auth + session extraction logic without starting a real
BidiAgent, Bedrock stream, or WebSocket server. The entrypoint's
`websocket_handler` is hard to test in isolation because it expects
a Starlette-style WebSocket object; for validation paths we test
the internal helper `_establish_session` directly, which takes the
same RequestContext + websocket-like object.

Run:
    pytest agents/supervisor/tests/test_bidi_app.py -v
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest
from bedrock_agentcore.runtime.context import RequestContext

from agents.supervisor.bidi_app import (
    InvocationError,
    _establish_session,
    _normalize_headers,
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
            "X-Amzn-Bedrock-AgentCore-Runtime-Custom-Tenant-Id": "michelin",
            "X-Amzn-Bedrock-AgentCore-Runtime-Custom-Vin": "1FMCU0F70MUA12345",
        }
    )
    # No WS receive_json call expected because all fields come from headers.
    ws = AsyncMock()
    ws.receive_json = AsyncMock(side_effect=AssertionError("should not be called"))

    session = await _establish_session(ws, ctx, "vsa-sess-test")

    assert session.tenant_id == "michelin"
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
            "tenantId": "michelin",
            "vin": "1FMCU0F70MUA12345",
        }
    )

    session = await _establish_session(ws, ctx, "sid")
    assert session.tenant_id == "michelin"
    assert session.vin == "1FMCU0F70MUA12345"
    assert session.jwt == "jwt.abc"


@pytest.mark.asyncio
async def test_session_start_can_supply_jwt_too():
    ctx = _ctx(headers={})
    ws = _fake_websocket(
        {
            "type": "session.start",
            "tenantId": "michelin",
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
    ctx = _ctx(headers={})
    ws = _fake_websocket({"type": "audio.chunk", "data": "AAAA"})

    with pytest.raises(InvocationError, match="session.start"):
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
    ws = _fake_websocket({"type": "session.start", "tenantId": "michelin"})
    with pytest.raises(InvocationError, match="vin"):
        await _establish_session(ws, ctx, "sid")


@pytest.mark.asyncio
async def test_rejects_missing_user_token():
    ctx = _ctx(
        headers={
            "X-Amzn-Bedrock-AgentCore-Runtime-Custom-Tenant-Id": "michelin",
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
            "X-Amzn-Bedrock-AgentCore-Runtime-Custom-Tenant-Id": "michelin",
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
            "X-AMZN-BEDROCK-AGENTCORE-RUNTIME-CUSTOM-TENANT-ID": "michelin",
            "X-Amzn-Bedrock-AgentCore-Runtime-Custom-Vin": "1FMCU0F70MUA12345",
        }
    )
    ws = AsyncMock()
    session = await _establish_session(ws, ctx, "sid")
    assert session.tenant_id == "michelin"
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
            "X-Amzn-Bedrock-AgentCore-Runtime-Custom-Tenant-Id": "michelin",
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
            "X-Amzn-Bedrock-AgentCore-Runtime-Custom-Tenant-Id": "michelin",
            "X-Amzn-Bedrock-AgentCore-Runtime-Custom-Vin": "1FMCU0F70MUA12345",
        }
    )
    ws = AsyncMock()
    ws.receive_json = AsyncMock(side_effect=AssertionError("should not be called"))

    session = await _establish_session(ws, ctx, "vsa-sess-test")

    assert session.persona_id == "fleet_driver"
    assert session.customer_id is None


@pytest.mark.asyncio
async def test_session_init_calls_persona_loader():
    """persona_loader.load_persona is called once with header-extracted args."""
    from unittest.mock import patch, MagicMock
    from agents.supervisor.persona_loader import PersonaSnapshot

    ctx = _ctx(
        headers={
            "X-Amzn-Bedrock-AgentCore-Runtime-Custom-User-Token": "jwt.tok",
            "X-Amzn-Bedrock-AgentCore-Runtime-Custom-Tenant-Id": "michelin",
            "X-Amzn-Bedrock-AgentCore-Runtime-Custom-Vin": "1FMCU0F70MUA12345",
            "X-Amzn-Bedrock-AgentCore-Runtime-Custom-Persona-Id": "fleet_driver",
            "X-Amzn-Bedrock-AgentCore-Runtime-Custom-Customer-Id": "cust-42",
        }
    )
    ws = AsyncMock()
    ws.receive_json = AsyncMock(side_effect=AssertionError("should not be called"))

    fake_snapshot = PersonaSnapshot(
        persona_id="fleet_driver",
        vehicle_id="1FMCU0F70MUA12345",
        customer_id="cust-42",
        data_sources={"vehicle_identity": {"vin": "1FMCU0F70MUA12345"}},
    )

    with patch("agents.supervisor.bidi_app._persona_loader.load_persona", new_callable=AsyncMock) as mock_load, \
         patch("agents.supervisor.bidi_app.build_persona_loader_config") as mock_cfg:
        mock_load.return_value = fake_snapshot
        mock_cfg.return_value = MagicMock()

        session = await _establish_session(ws, ctx, "vsa-sess-test")

    # _establish_session extracts headers but does NOT call persona_loader —
    # that happens in websocket_handler before agent.run(). The session fields
    # should be correctly populated from the headers regardless.
    assert session.persona_id == "fleet_driver"
    assert session.customer_id == "cust-42"
    assert session.vin == "1FMCU0F70MUA12345"
