"""Unit tests for the AgentCore entrypoint.

These tests invoke the @app.entrypoint function directly with a mocked
RequestContext. No AgentCore server, no Bedrock, no network by default.

Tests cover:
  - Header validation (missing, malformed, case variants)
  - Payload validation (missing prompt, missing tenantId, wrong types)
  - session_id fallback when AgentCore did not provide one
  - A full happy path that hits Claude + /triage — gated behind
    VSA_TEST_E2E=1 because it costs tokens and network.

Run:
    pytest agents/supervisor/tests/test_agentcore_app.py -v
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock

import pytest
from bedrock_agentcore.runtime.context import RequestContext

from agents.supervisor.agentcore_app import invoke
from agents.supervisor.config import Config, get_cached_id_token


def _ctx(session_id: str | None = "vsa-sess-test", headers: dict | None = None) -> RequestContext:
    return RequestContext(
        session_id=session_id,
        request_headers=headers or {},
        request=None,
    )


# ---------------------------------------------------------------------------
# Validation tests — fast, no network
# ---------------------------------------------------------------------------


def test_rejects_missing_authorization_header():
    result = invoke(
        payload={"prompt": "hi", "tenantId": "michelin"},
        context=_ctx(headers={}),
    )
    assert "error" in result
    assert "token" in result["error"].lower() or "X-Vsa-User-Token" in result["error"]


def test_payload_jwt_fallback_accepted(monkeypatch):
    """When no user-token header is present, fall back to payload.jwt.
    This path is used by `agentcore invoke` (CLI) which does not pass
    custom headers."""
    import agents.supervisor.agentcore_app as mod

    fake_agent = MagicMock(return_value=MagicMock(message={"content": [{"text": "ok"}]}))
    fake_ctx_state = {}

    def fake_build_supervisor(ctx, config):
        fake_ctx_state["ctx"] = ctx
        return fake_agent

    monkeypatch.setattr(mod, "build_supervisor", fake_build_supervisor)

    result = invoke(
        payload={"prompt": "hi", "tenantId": "michelin", "jwt": "payload.jwt.token"},
        context=_ctx(headers={}),
    )
    assert "error" not in result
    assert fake_ctx_state["ctx"].jwt == "payload.jwt.token"


def test_rejects_empty_user_token():
    result = invoke(
        payload={"prompt": "hi", "tenantId": "michelin", "jwt": "   "},
        context=_ctx(headers={"X-Amzn-Bedrock-AgentCore-Runtime-Custom-User-Token": "   "}),
    )
    assert "error" in result
    assert "empty" in result["error"].lower()


def test_rejects_missing_prompt():
    result = invoke(
        payload={"tenantId": "michelin", "jwt": "fake.jwt.token"},
        context=_ctx(headers={}),
    )
    assert "error" in result
    assert "prompt" in result["error"]


def test_rejects_missing_tenant_id():
    result = invoke(
        payload={"prompt": "hi", "jwt": "fake.jwt.token"},
        context=_ctx(headers={}),
    )
    assert "error" in result
    assert "tenantId" in result["error"]


def test_lowercase_authorization_header_accepted(monkeypatch):
    """Header extraction should work with both canonical and lowercase
    variants of X-Vsa-User-Token."""
    import agents.supervisor.agentcore_app as mod

    fake_agent = MagicMock()
    fake_agent.return_value = MagicMock(
        message={"content": [{"text": "ok"}]},
    )
    fake_ctx_state = {}

    def fake_build_supervisor(ctx, config):
        fake_ctx_state["ctx"] = ctx
        return fake_agent

    monkeypatch.setattr(mod, "build_supervisor", fake_build_supervisor)

    result = invoke(
        payload={"prompt": "hi", "tenantId": "michelin", "vin": "1FMCU0F70MUA12345"},
        context=_ctx(headers={"x-amzn-bedrock-agentcore-runtime-custom-user-token": "fake.jwt.token"}),
    )
    assert "error" not in result
    assert result["result"] == "ok"
    assert result["sessionId"] == "vsa-sess-test"
    assert fake_ctx_state["ctx"].jwt == "fake.jwt.token"
    assert fake_ctx_state["ctx"].tenant_id == "michelin"
    assert fake_ctx_state["ctx"].vin == "1FMCU0F70MUA12345"


def test_session_id_fallback_when_agentcore_did_not_provide(monkeypatch):
    import agents.supervisor.agentcore_app as mod

    fake_agent = MagicMock(return_value=MagicMock(message={"content": [{"text": "ok"}]}))
    fake_ctx_state = {}

    def fake_build_supervisor(ctx, config):
        fake_ctx_state["ctx"] = ctx
        return fake_agent

    monkeypatch.setattr(mod, "build_supervisor", fake_build_supervisor)

    result = invoke(
        payload={"prompt": "hi", "tenantId": "michelin", "jwt": "t"},
        context=_ctx(session_id=None, headers={}),
    )
    assert "error" not in result
    assert result["sessionId"].startswith("vsa-sess-local-")
    assert fake_ctx_state["ctx"].session_id.startswith("vsa-sess-local-")


# ---------------------------------------------------------------------------
# End-to-end happy path — hits Bedrock + /triage. Opt-in.
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not os.environ.get("VSA_TEST_E2E"),
    reason="Set VSA_TEST_E2E=1 to run end-to-end entrypoint test",
)
def test_full_invoke_live():
    config = Config.from_env()
    jwt = get_cached_id_token(config)

    result = invoke(
        payload={
            "prompt": "Temperature gauge just spiked.",
            "tenantId": config.tenant_id,
            "vin": config.default_vin,
        },
        context=_ctx(headers={"X-Amzn-Bedrock-AgentCore-Runtime-Custom-User-Token": jwt}),
    )

    assert "error" not in result, f"Unexpected error: {result}"
    assert result["sessionId"]
    assert result["classification"] in {"P0", "P1", "P2", "P3"}
    assert result["vin"] == config.default_vin
    assert result["result"], "Expected non-empty narration"
    print(f"\nClassification: {result['classification']}")
    print(f"Narration: {result['result'][:200]}...")


# ---------------------------------------------------------------------------
# personaId threading tests (Group 1 skeleton + Group 2 implementation)
# ---------------------------------------------------------------------------


def _fake_build(monkeypatch):
    """Patch build_supervisor; return (captured_ctx_holder, mock_agent)."""
    import agents.supervisor.agentcore_app as mod

    fake_agent = MagicMock(return_value=MagicMock(message={"content": [{"text": "ok"}]}))
    holder = {}

    def fake_build_supervisor(ctx, config):
        holder["ctx"] = ctx
        return fake_agent

    monkeypatch.setattr(mod, "build_supervisor", fake_build_supervisor)
    return holder, fake_agent


def _hdr(token: str = "fake.jwt.token") -> dict:
    return {"X-Amzn-Bedrock-AgentCore-Runtime-Custom-User-Token": token}


def test_valid_persona_id_threads_into_session_context(monkeypatch):
    """A recognised personaId is forwarded to SessionContext.persona_id."""
    holder, _ = _fake_build(monkeypatch)
    result = invoke(
        payload={"prompt": "hi", "tenantId": "michelin", "personaId": "service_advisor"},
        context=_ctx(headers=_hdr()),
    )
    assert "error" not in result
    assert holder["ctx"].persona_id == "service_advisor"


def test_invalid_persona_id_defaults_to_consumer(monkeypatch):
    """An unrecognised personaId falls back to consumer (not fleet_driver).

    Fix Group 2 Task 2b: _DEFAULT_PERSONA changed from 'fleet_driver' to
    'consumer' so a malformed/missing/unknown personaId in the runtime payload
    cannot land on the owner-scoped fleet_driver surface.

    The old test name ('defaults_to_fleet_driver') encoded the security defect.
    """
    holder, _ = _fake_build(monkeypatch)
    result = invoke(
        payload={"prompt": "hi", "tenantId": "michelin", "personaId": "super_admin"},
        context=_ctx(headers=_hdr()),
    )
    assert "error" not in result
    assert holder["ctx"].persona_id == "consumer", (
        f"Unrecognised personaId must fall back to 'consumer' (narrow surface), "
        f"not 'fleet_driver' (owner-scoped). Got: {holder['ctx'].persona_id!r}"
    )


def test_omitted_persona_id_defaults_to_consumer(monkeypatch):
    """When personaId is absent the entrypoint falls back to consumer.

    Fix Group 2 Task 2b: any caller — including Ops tooling, test harnesses,
    or Lambdas — that omits personaId now receives the narrow consumer surface
    rather than the owner-scoped fleet_driver surface.
    """
    holder, _ = _fake_build(monkeypatch)
    result = invoke(
        payload={"prompt": "hi", "tenantId": "michelin"},
        context=_ctx(headers=_hdr()),
    )
    assert "error" not in result
    assert holder["ctx"].persona_id == "consumer", (
        f"Absent personaId must fall back to 'consumer', not 'fleet_driver'. "
        f"Got: {holder['ctx'].persona_id!r}"
    )


def test_all_valid_personas_accepted(monkeypatch):
    """Every slug in PERSONA_PRODUCT_BINDINGS is a valid personaId."""
    from agents.supervisor.persona_definitions import PERSONA_PRODUCT_BINDINGS

    for persona in PERSONA_PRODUCT_BINDINGS:
        holder, _ = _fake_build(monkeypatch)
        result = invoke(
            payload={"prompt": "hi", "tenantId": "michelin", "personaId": persona},
            context=_ctx(headers=_hdr()),
        )
        assert "error" not in result, f"unexpected error for persona={persona!r}: {result}"
        assert holder["ctx"].persona_id == persona, f"persona mismatch for {persona!r}"


def test_empty_persona_id_defaults_to_consumer(monkeypatch):
    """An empty-string personaId is treated as omitted → falls back to consumer.

    Fix Group 2 Task 2b: empty personaId must not escalate to fleet_driver.
    """
    holder, _ = _fake_build(monkeypatch)
    result = invoke(
        payload={"prompt": "hi", "tenantId": "michelin", "personaId": ""},
        context=_ctx(headers=_hdr()),
    )
    assert "error" not in result
    assert holder["ctx"].persona_id == "consumer", (
        f"Empty personaId must fall back to 'consumer'. Got: {holder['ctx'].persona_id!r}"
    )


def test_response_contract_unchanged(monkeypatch):
    """personaId does not alter the existing response shape."""
    holder, _ = _fake_build(monkeypatch)
    result = invoke(
        payload={"prompt": "hi", "tenantId": "michelin", "personaId": "consumer"},
        context=_ctx(headers=_hdr()),
    )
    assert set(result.keys()) == {"result", "sessionId", "classification", "vin"}


def test_iskiosk_true_in_payload_sets_is_kiosk_on_context(monkeypatch):
    """isKiosk=True in the runtime payload sets is_kiosk=True on SessionContext.

    Fix Group 2 Task 1c: agentcore_app.invoke reads payload['isKiosk'] and
    passes it to the SessionContext constructor. This is the production plumbing
    fix that makes the tool allow-list gate actually fire.

    Before the fix: is_kiosk was not a dataclass field, so even if set, getattr
    returned False. After: is_kiosk is a real field and this test verifies it
    is correctly set by the agentcore_app entrypoint.
    """
    holder, _ = _fake_build(monkeypatch)
    result = invoke(
        payload={
            "prompt": "show me the catalog",
            "tenantId": "meridian",
            "personaId": "consumer",
            "isKiosk": True,
        },
        context=_ctx(headers=_hdr()),
    )
    assert "error" not in result
    assert holder["ctx"].is_kiosk is True, (
        f"isKiosk=True in payload must set SessionContext.is_kiosk=True. "
        f"Got: {holder['ctx'].is_kiosk!r}. "
        "This is the Fix Group 2 Task 1c plumbing fix."
    )


def test_iskiosk_false_in_payload_leaves_context_non_kiosk(monkeypatch):
    """isKiosk absent/False in payload leaves is_kiosk=False on SessionContext."""
    holder, _ = _fake_build(monkeypatch)
    result = invoke(
        payload={"prompt": "hi", "tenantId": "michelin", "personaId": "fleet_driver"},
        context=_ctx(headers=_hdr()),
    )
    assert "error" not in result
    assert holder["ctx"].is_kiosk is False, (
        f"No isKiosk in payload must leave SessionContext.is_kiosk=False. "
        f"Got: {holder['ctx'].is_kiosk!r}."
    )


def test_iskiosk_not_set_defaults_to_false(monkeypatch):
    """isKiosk not present in payload → is_kiosk=False (existing CMS callers unaffected)."""
    holder, _ = _fake_build(monkeypatch)
    result = invoke(
        payload={"prompt": "hi", "tenantId": "michelin"},
        context=_ctx(headers=_hdr()),
    )
    assert "error" not in result
    assert holder["ctx"].is_kiosk is False


# ---------------------------------------------------------------------------
# Per-session turn cap — agentcore_app.py path (decisions.md Q9)
# ---------------------------------------------------------------------------
#
# Mirrors the handler.py tests but exercises the agentcore_app invoke() path.
# Tests cover: under-cap, at-cap, over-cap (graceful message), fail-open on
# store error, independent session counters.
# ---------------------------------------------------------------------------


def _invoke_with_cap(monkeypatch, session_id: str = "ac-cap-sess-001", limit: int = 3):
    """Helper: set a turn limit and invoke() once, return the result dict."""
    import agents.supervisor.agentcore_app as mod
    import agents.supervisor.session_cap as sc

    monkeypatch.setenv("VSA_SESSION_TURN_LIMIT", str(limit))
    fake_agent = MagicMock(return_value=MagicMock(message={"content": [{"text": "agent reply"}]}))

    def fake_build(ctx, config):
        return fake_agent

    monkeypatch.setattr(mod, "build_supervisor", fake_build)

    return invoke(
        payload={"prompt": "tell me about Meridian", "tenantId": "meridian", "jwt": "fake.jwt"},
        context=_ctx(session_id=session_id, headers={}),
    )


def test_agentcore_under_cap_returns_agent_reply(monkeypatch):
    """Turns within the cap return the agent's reply."""
    import agents.supervisor.session_cap as sc
    sc.clear_all()

    result = _invoke_with_cap(monkeypatch, limit=5)
    assert "error" not in result
    assert result.get("result") == "agent reply"
    sc.clear_all()


def test_agentcore_over_cap_returns_graceful_message(monkeypatch):
    """Turn N+1 returns the start-new-session message with no error field."""
    import agents.supervisor.agentcore_app as mod
    import agents.supervisor.session_cap as sc

    sc.clear_all()
    monkeypatch.setenv("VSA_SESSION_TURN_LIMIT", "2")

    fake_agent = MagicMock(return_value=MagicMock(message={"content": [{"text": "agent reply"}]}))

    def fake_build(ctx, config):
        return fake_agent

    monkeypatch.setattr(mod, "build_supervisor", fake_build)

    sid = "ac-over-cap-001"

    # Exhaust cap
    for _ in range(2):
        invoke(
            payload={"prompt": "hi", "tenantId": "meridian", "jwt": "t"},
            context=_ctx(session_id=sid, headers={}),
        )

    # Over-cap turn
    result = invoke(
        payload={"prompt": "hi", "tenantId": "meridian", "jwt": "t"},
        context=_ctx(session_id=sid, headers={}),
    )

    assert "error" not in result, "cap exceeded must NOT produce an error key"
    assert "start a new session" in result.get("result", "").lower(), (
        "cap exceeded must return the graceful start-new-session message"
    )
    assert result.get("result") != "agent reply"
    sc.clear_all()


def test_agentcore_cap_fail_open_on_store_error(monkeypatch):
    """If the counter store raises, the request is allowed through (fail-open)."""
    import agents.supervisor.agentcore_app as mod
    import agents.supervisor.session_cap as sc

    sc.clear_all()

    def exploding_inner(session_id):
        raise RuntimeError("simulated counter store failure")

    monkeypatch.setattr(sc, "_check_and_increment_inner", exploding_inner)

    fake_agent = MagicMock(return_value=MagicMock(message={"content": [{"text": "agent reply"}]}))

    def fake_build(ctx, config):
        return fake_agent

    monkeypatch.setattr(mod, "build_supervisor", fake_build)

    result = invoke(
        payload={"prompt": "hi", "tenantId": "meridian", "jwt": "t"},
        context=_ctx(session_id="fail-open-sess", headers={}),
    )

    # Fail-open: agent reply must be returned despite the counter error
    assert "error" not in result
    assert result.get("result") == "agent reply", (
        "fail-open: agent reply must be returned when counter store errors"
    )
    sc.clear_all()
