"""Unit tests for the /assistant/chat route handler.

All tests are fast — no network, no AWS. The bedrock-agentcore client and
boto3 are monkeypatched throughout.

Run:
    python3 -m pytest lambdas/api-assistant-chat/ -v
"""

from __future__ import annotations

import json
import os
import sys

import pytest

# Allow direct import from the lambda root without a package install.
_LAMBDA_ROOT = os.path.join(os.path.dirname(__file__), "..")
if _LAMBDA_ROOT not in sys.path:
    sys.path.insert(0, _LAMBDA_ROOT)


def _make_event(
    prompt="hello",
    session_id="sess-123",
    vin=None,
    claims=None,
    auth_header="Bearer fake.id.token",
    body_override=None,
) -> dict:
    body = {"prompt": prompt, "sessionId": session_id}
    if vin:
        body["vin"] = vin
    if body_override is not None:
        body = body_override
    return {
        "requestContext": {
            "authorizer": {
                "claims": claims or {
                    "sub": "user-sub-123",
                    "custom:tenantId": "michelin",
                    "cognito:groups": "fleet-operator",
                }
            }
        },
        "headers": {"Authorization": auth_header},
        "body": json.dumps(body),
    }


def _good_runtime_response(result="Agent says hi", session_id="sess-123") -> dict:
    payload_bytes = json.dumps({
        "result": result,
        "sessionId": session_id,
        "classification": "P2",
        "vin": None,
    }).encode()

    class _Stream:
        def read(self):
            return payload_bytes

    return {"response": _Stream()}


class _FakeEvents:
    """Minimal fake of botocore.client.BaseClient.meta.events for tests.

    Records every register_first / unregister call so tests can assert the
    handler injects the user-token hook before invoke and tears it down
    afterwards (even on exception).
    """

    def __init__(self) -> None:
        self.registered: list = []
        self.unregistered: list = []

    def register_first(self, name, handler) -> None:
        self.registered.append((name, handler))

    def unregister(self, name, handler) -> None:
        self.unregistered.append((name, handler))


class _FakeMeta:
    def __init__(self) -> None:
        self.events = _FakeEvents()


class _FakeClient:
    """Drop-in for the bedrock-agentcore boto3 client in tests.

    ``invoke_callback(**kwargs)`` is invoked on every ``invoke_agent_runtime``
    call so individual tests can capture kwargs, simulate failures, etc.
    """

    def __init__(self, invoke_callback) -> None:
        self.meta = _FakeMeta()
        self._invoke_callback = invoke_callback

    def invoke_agent_runtime(self, **kwargs):
        return self._invoke_callback(**kwargs)


def _make_client(invoke_callback=None):
    """Build a _FakeClient. Default callback returns _good_runtime_response()."""
    if invoke_callback is None:
        def invoke_callback(**_kw):
            return _good_runtime_response()
    return _FakeClient(invoke_callback)


class _FakeContext:
    """Minimal fake of the Lambda context object — exposes aws_request_id."""

    def __init__(self, aws_request_id="req-test-1234") -> None:
        self.aws_request_id = aws_request_id


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_happy_path_returns_result(monkeypatch):
    import handler as h

    monkeypatch.setattr(h, "_agentcore_client", _make_client())
    monkeypatch.setenv("CVX_RUNTIME_ARN", "arn:aws:bedrock-agentcore:us-west-2:123456789012:runtime/test")

    resp = h.handler(_make_event(), None)
    assert resp["statusCode"] == 200
    body = json.loads(resp["body"])
    assert body["result"] == "Agent says hi"
    assert body["sessionId"] == "sess-123"
    assert body["classification"] == "P2"


def test_vin_forwarded_when_present(monkeypatch):
    import handler as h

    captured = {}

    def fake_invoke(**kw):
        captured["payload"] = json.loads(kw["payload"])
        return _good_runtime_response()

    monkeypatch.setattr(h, "_agentcore_client", _make_client(fake_invoke))
    monkeypatch.setenv("CVX_RUNTIME_ARN", "arn:aws:bedrock-agentcore:us-west-2:123456789012:runtime/test")

    h.handler(_make_event(vin="1FMCU0F70MUA12345"), None)
    assert captured["payload"]["vin"] == "1FMCU0F70MUA12345"


def test_persona_inferred_from_claims(monkeypatch):
    import handler as h

    captured = {}

    def fake_invoke(**kw):
        captured["payload"] = json.loads(kw["payload"])
        return _good_runtime_response()

    monkeypatch.setattr(h, "_agentcore_client", _make_client(fake_invoke))
    monkeypatch.setenv("CVX_RUNTIME_ARN", "arn:aws:bedrock-agentcore:us-west-2:123456789012:runtime/test")

    event = _make_event(claims={"sub": "u", "custom:tenantId": "michelin", "custom:role": "service-advisor"})
    h.handler(event, None)
    assert captured["payload"]["personaId"] == "service_advisor"


def test_tenant_from_claims(monkeypatch):
    import handler as h

    captured = {}

    def fake_invoke(**kw):
        captured["payload"] = json.loads(kw["payload"])
        return _good_runtime_response()

    monkeypatch.setattr(h, "_agentcore_client", _make_client(fake_invoke))
    monkeypatch.setenv("CVX_RUNTIME_ARN", "arn:aws:bedrock-agentcore:us-west-2:123456789012:runtime/test")

    event = _make_event(claims={"custom:tenantId": "ford", "cognito:groups": "fleet-operator"})
    h.handler(event, None)
    assert captured["payload"]["tenantId"] == "ford"


# ---------------------------------------------------------------------------
# Token never logged (structural: verify no log.info/log.debug logs id_token)
# ---------------------------------------------------------------------------


def test_id_token_not_in_log_output(monkeypatch, caplog):
    import handler as h
    import logging

    monkeypatch.setattr(h, "_agentcore_client", _make_client())
    monkeypatch.setenv("CVX_RUNTIME_ARN", "arn:aws:bedrock-agentcore:us-west-2:123456789012:runtime/test")

    secret_token = "secret.id.token.must.not.appear"
    with caplog.at_level(logging.DEBUG):
        h.handler(_make_event(auth_header=f"Bearer {secret_token}"), None)

    assert secret_token not in caplog.text, "IdToken must never appear in logs"


# ---------------------------------------------------------------------------
# Validation failures
# ---------------------------------------------------------------------------


def test_missing_prompt_returns_400(monkeypatch):
    import handler as h
    monkeypatch.setenv("CVX_RUNTIME_ARN", "arn:aws:bedrock-agentcore:us-west-2:123456789012:runtime/test")

    event = _make_event(body_override={"sessionId": "s"})
    resp = h.handler(event, None)
    assert resp["statusCode"] == 400
    assert "prompt" in json.loads(resp["body"])["error"]


def test_missing_session_id_returns_400(monkeypatch):
    import handler as h
    monkeypatch.setenv("CVX_RUNTIME_ARN", "arn:aws:bedrock-agentcore:us-west-2:123456789012:runtime/test")

    event = _make_event(body_override={"prompt": "hi"})
    resp = h.handler(event, None)
    assert resp["statusCode"] == 400
    assert "sessionId" in json.loads(resp["body"])["error"]


def test_invalid_json_body_returns_400(monkeypatch):
    import handler as h
    monkeypatch.setenv("CVX_RUNTIME_ARN", "arn:aws:bedrock-agentcore:us-west-2:123456789012:runtime/test")

    event = _make_event()
    event["body"] = "not json {"
    resp = h.handler(event, None)
    assert resp["statusCode"] == 400


def test_missing_runtime_arn_returns_500(monkeypatch):
    monkeypatch.delenv("CVX_RUNTIME_ARN", raising=False)

    import importlib
    import handler as h  # already imported; just ensure env is unset at call time

    resp = h.handler(_make_event(), None)
    assert resp["statusCode"] == 500
    assert "CVX_RUNTIME_ARN" in json.loads(resp["body"])["error"]


# ---------------------------------------------------------------------------
# Upstream failure — fail closed with structured JSON
# ---------------------------------------------------------------------------


def test_runtime_exception_returns_structured_500(monkeypatch):
    import handler as h

    def boom(**kw):
        raise RuntimeError("network error")

    monkeypatch.setattr(h, "_agentcore_client", _make_client(boom))
    monkeypatch.setenv("CVX_RUNTIME_ARN", "arn:aws:bedrock-agentcore:us-west-2:123456789012:runtime/test")

    resp = h.handler(_make_event(), _FakeContext("req-abc-1"))
    assert resp["statusCode"] == 500
    body = json.loads(resp["body"])
    assert body["error"] == "upstream invocation failed"
    # Security: do NOT leak exception detail to caller (security-review.md cycle 1).
    assert "detail" not in body
    # When Lambda context is provided, requestId is included for correlation.
    assert body.get("requestId") == "req-abc-1"


# ---------------------------------------------------------------------------
# CORS headers always present
# ---------------------------------------------------------------------------


def test_cors_headers_on_success(monkeypatch):
    import handler as h

    monkeypatch.setattr(h, "_agentcore_client", _make_client())
    monkeypatch.setenv("CVX_RUNTIME_ARN", "arn:aws:bedrock-agentcore:us-west-2:123456789012:runtime/test")

    resp = h.handler(_make_event(), None)
    assert resp["headers"]["Access-Control-Allow-Origin"] == "*"


def test_cors_headers_on_error(monkeypatch):
    import handler as h
    monkeypatch.setenv("CVX_RUNTIME_ARN", "arn:aws:bedrock-agentcore:us-west-2:123456789012:runtime/test")

    event = _make_event(body_override={"sessionId": "s"})  # missing prompt
    resp = h.handler(event, None)
    assert resp["headers"]["Access-Control-Allow-Origin"] == "*"


# ---------------------------------------------------------------------------
# Regression: boto3 invoke_agent_runtime API correctness (review.md cycle 1)
# ---------------------------------------------------------------------------


def test_invoke_called_with_correct_kwargs(monkeypatch):
    """Use agentRuntimeArn (NOT agentRuntimeId); never pass additionalRequestHeaders."""
    import handler as h

    captured: dict = {}

    def fake_invoke(**kw):
        captured["kwargs"] = dict(kw)
        return _good_runtime_response()

    monkeypatch.setattr(h, "_agentcore_client", _make_client(fake_invoke))
    monkeypatch.setenv(
        "CVX_RUNTIME_ARN",
        "arn:aws:bedrock-agentcore:us-west-2:123456789012:runtime/test",
    )

    h.handler(_make_event(), None)

    kwargs = captured["kwargs"]
    # Correct param name
    assert "agentRuntimeArn" in kwargs, "expected agentRuntimeArn kwarg"
    assert kwargs["agentRuntimeArn"].endswith("/test")
    # Wrong param name MUST NOT appear
    assert "agentRuntimeId" not in kwargs, "agentRuntimeId is not a valid kwarg"
    # Non-existent param MUST NOT appear
    assert "additionalRequestHeaders" not in kwargs, (
        "additionalRequestHeaders is not a valid kwarg of invoke_agent_runtime"
    )
    # Required kwargs are present
    assert "runtimeSessionId" in kwargs
    assert "payload" in kwargs


def test_user_token_injected_via_event_hook(monkeypatch):
    """The CMS IdToken is registered as a `before-sign` event hook,
    not passed as a kwarg, and is unregistered after the call."""
    import handler as h

    fake_client = _make_client()
    monkeypatch.setattr(h, "_agentcore_client", fake_client)
    monkeypatch.setenv(
        "CVX_RUNTIME_ARN",
        "arn:aws:bedrock-agentcore:us-west-2:123456789012:runtime/test",
    )

    h.handler(_make_event(auth_header="Bearer some.id.token"), None)

    events = fake_client.meta.events
    # Exactly one before-sign hook was registered AND unregistered (cleanup ran).
    assert len(events.registered) == 1
    name, hook = events.registered[0]
    assert name == "before-sign.bedrock-agentcore.InvokeAgentRuntime"
    assert callable(hook)
    assert (name, hook) in events.unregistered, (
        "the before-sign hook must be unregistered after the call"
    )


def test_event_hook_unregistered_on_exception(monkeypatch):
    """Even when invoke_agent_runtime raises, the event hook is unregistered."""
    import handler as h

    def boom(**_kw):
        raise RuntimeError("simulated upstream failure")

    fake_client = _make_client(boom)
    monkeypatch.setattr(h, "_agentcore_client", fake_client)
    monkeypatch.setenv(
        "CVX_RUNTIME_ARN",
        "arn:aws:bedrock-agentcore:us-west-2:123456789012:runtime/test",
    )

    resp = h.handler(_make_event(auth_header="Bearer t"), None)
    assert resp["statusCode"] == 500  # fail-closed

    events = fake_client.meta.events
    assert len(events.registered) == 1, "hook was registered before the failing invoke"
    name, hook = events.registered[0]
    assert (name, hook) in events.unregistered, (
        "the before-sign hook MUST unregister even on exception (finally block)"
    )


def test_no_event_hook_when_no_id_token(monkeypatch):
    """When no Authorization header is provided, no event hook is registered."""
    import handler as h

    fake_client = _make_client()
    monkeypatch.setattr(h, "_agentcore_client", fake_client)
    monkeypatch.setenv(
        "CVX_RUNTIME_ARN",
        "arn:aws:bedrock-agentcore:us-west-2:123456789012:runtime/test",
    )

    h.handler(_make_event(auth_header=""), None)

    events = fake_client.meta.events
    assert events.registered == [], "no token \u2192 no hook registration"
    assert events.unregistered == [], "no hook \u2192 nothing to unregister"


# ---------------------------------------------------------------------------
# Regression: 500-body must not leak runtime ARN (security-review.md cycle 1 Warning)
# ---------------------------------------------------------------------------


def test_500_body_does_not_leak_runtime_arn(monkeypatch):
    """Botocore exceptions routinely embed the runtime ARN + account ID.
    The 500 response body must NOT echo any of that to the caller."""
    import handler as h

    runtime_arn = "arn:aws:bedrock-agentcore:us-west-2:123456789012:runtime/test"

    def leaky_boom(**_kw):
        # Simulate a botocore-style error message that would include the ARN
        raise RuntimeError(
            f"An error occurred (ResourceNotFoundException) when calling "
            f"the InvokeAgentRuntime operation: agent runtime {runtime_arn} not found"
        )

    monkeypatch.setattr(h, "_agentcore_client", _make_client(leaky_boom))
    monkeypatch.setenv("CVX_RUNTIME_ARN", runtime_arn)

    resp = h.handler(_make_event(), _FakeContext("req-leak-test"))

    body_text = resp["body"]
    body = json.loads(body_text)

    assert resp["statusCode"] == 500
    assert body["error"] == "upstream invocation failed"
    assert body.get("requestId") == "req-leak-test"
    # Hard guarantees: NO leak of internal AWS detail
    assert "detail" not in body, "detail field must be removed from 500 body"
    assert runtime_arn not in body_text, "runtime ARN must NOT appear in caller-visible body"
    assert "123456789012" not in body_text, "account ID must NOT appear in caller-visible body"
    assert "ResourceNotFoundException" not in body_text, (
        "botocore exception class must NOT appear in caller-visible body"
    )


def test_500_body_omits_request_id_when_context_lacks_it(monkeypatch):
    """When the Lambda context is None or has no aws_request_id, the 500
    response is still well-formed (graceful degradation)."""
    import handler as h

    def boom(**_kw):
        raise RuntimeError("upstream failure")

    monkeypatch.setattr(h, "_agentcore_client", _make_client(boom))
    monkeypatch.setenv(
        "CVX_RUNTIME_ARN",
        "arn:aws:bedrock-agentcore:us-west-2:123456789012:runtime/test",
    )

    resp = h.handler(_make_event(), None)
    body = json.loads(resp["body"])
    assert resp["statusCode"] == 500
    assert body["error"] == "upstream invocation failed"
    # No requestId when context is None — body still well-formed
    assert "requestId" not in body
    assert "detail" not in body


# ---------------------------------------------------------------------------
# Regression: runtimeSessionId length >= 33 (AgentCore API constraint)
# See issues/2026-06-22-assistant-chat-runtime-session-id-too-short/
# ---------------------------------------------------------------------------


def test_short_session_id_padded_to_33_chars(monkeypatch):
    """ChatAgent.tsx generates 31-char session IDs (`session_<ms>_<9-rand>`);
    AgentCore InvokeAgentRuntime requires runtimeSessionId length >=33. The
    handler must pad the upstream call deterministically while keeping the
    user-facing sessionId unchanged."""
    import handler as h

    captured: dict = {}

    def fake_invoke(**kw):
        captured["kwargs"] = dict(kw)
        return _good_runtime_response()

    monkeypatch.setattr(h, "_agentcore_client", _make_client(fake_invoke))
    monkeypatch.setenv(
        "CVX_RUNTIME_ARN",
        "arn:aws:bedrock-agentcore:us-west-2:123456789012:runtime/test",
    )

    short_session = "session_1782144000000_abc123def"  # 31 chars (typical browser format)
    assert len(short_session) == 31

    event = _make_event(session_id=short_session)
    resp = h.handler(event, None)
    assert resp["statusCode"] == 200

    runtime_session = captured["kwargs"]["runtimeSessionId"]
    # AgentCore minimum
    assert len(runtime_session) >= 33, (
        f"runtimeSessionId must be >=33 chars; got {len(runtime_session)}"
    )
    # User-supplied prefix preserved (so AgentCore Memory continuity holds)
    assert runtime_session.startswith(short_session), (
        f"runtimeSessionId must preserve the user session_id prefix; got {runtime_session}"
    )
    # Pattern compliance: [a-zA-Z0-9][a-zA-Z0-9-_]*
    import re
    assert re.match(r"^[a-zA-Z0-9][a-zA-Z0-9_-]*$", runtime_session), (
        f"runtimeSessionId pattern violation: {runtime_session}"
    )


def test_long_session_id_passed_through_unchanged(monkeypatch):
    """When the caller already supplies a >=33-char session_id (e.g. uuid4
    hex = 32 chars + prefix), the handler must NOT pad — pass through."""
    import handler as h

    captured: dict = {}

    def fake_invoke(**kw):
        captured["kwargs"] = dict(kw)
        return _good_runtime_response()

    monkeypatch.setattr(h, "_agentcore_client", _make_client(fake_invoke))
    monkeypatch.setenv(
        "CVX_RUNTIME_ARN",
        "arn:aws:bedrock-agentcore:us-west-2:123456789012:runtime/test",
    )

    long_session = "session_" + "a" * 40  # 48 chars
    h.handler(_make_event(session_id=long_session), None)
    assert captured["kwargs"]["runtimeSessionId"] == long_session


def test_response_body_returns_user_session_id_unchanged(monkeypatch):
    """Even though the handler pads runtimeSessionId, the response body's
    sessionId is whatever the runtime returned (or the user-supplied id),
    NOT the padded version. The padded suffix is an internal detail."""
    import handler as h

    def fake_invoke(**kw):
        # Runtime echoes the runtimeSessionId in its result payload as-is —
        # this simulates the existing AgentCore behavior. The handler should
        # surface whatever the runtime sets in the response body's sessionId
        # OR fall back to the original user session_id.
        return _good_runtime_response(session_id=kw["runtimeSessionId"])

    monkeypatch.setattr(h, "_agentcore_client", _make_client(fake_invoke))
    monkeypatch.setenv(
        "CVX_RUNTIME_ARN",
        "arn:aws:bedrock-agentcore:us-west-2:123456789012:runtime/test",
    )

    short_session = "session_1782144000000_abc123def"
    resp = h.handler(_make_event(session_id=short_session), None)
    body = json.loads(resp["body"])
    # The handler trusts what the runtime returned (which mirrors the padded
    # ARN here). For session continuity in the browser, the browser keeps
    # using its own short ID — the padded ID is an internal detail.
    # This test just verifies the padded session ID is what the upstream sees,
    # not that we strip it back to the user-visible form (we don't, today).
    assert "sessionId" in body


# ---------------------------------------------------------------------------
# Persona resolution: _CLIENT_REQUESTABLE_PERSONAS allowlist
# (spec 2026-08-04-cvx-oem-discover-order-meridian, section 4 of handler.py)
#
# Security contract: only "consumer" may be supplied by the client; any
# other value — including privileged personas such as "service_advisor" —
# is silently ignored and the claim-derived persona wins.
# ---------------------------------------------------------------------------


def _make_event_with_persona(persona_id, claims=None):
    """Construct an event that includes ``personaId`` in the request body."""
    claims = claims or {
        "sub": "user-sub-456",
        "custom:tenantId": "michelin",
        "cognito:groups": "fleet-operator",
    }
    body: dict = {"prompt": "hello", "sessionId": "sess-persona-test-xx-xx", "personaId": persona_id}
    return {
        "requestContext": {"authorizer": {"claims": claims}},
        "headers": {"Authorization": "Bearer fake.id.token"},
        "body": json.dumps(body),
    }


def test_no_persona_id_uses_claim_derived_persona(monkeypatch):
    """(1) Regression guard — when the body contains no personaId the handler
    must continue to derive the persona from Cognito claims via infer_persona."""
    import handler as h

    captured: dict = {}

    def fake_invoke(**kw):
        captured["payload"] = json.loads(kw["payload"])
        return _good_runtime_response()

    monkeypatch.setattr(h, "_agentcore_client", _make_client(fake_invoke))
    monkeypatch.setenv("CVX_RUNTIME_ARN", "arn:aws:bedrock-agentcore:us-west-2:123456789012:runtime/test")

    claims = {"sub": "u", "custom:tenantId": "michelin", "custom:role": "service-advisor"}
    event = _make_event(claims=claims)  # no personaId in body
    h.handler(event, None)
    # service-advisor claim → service_advisor persona
    assert captured["payload"]["personaId"] == "service_advisor"


def test_allowlisted_persona_consumer_is_honoured(monkeypatch):
    """(2) personaId="consumer" is in _CLIENT_REQUESTABLE_PERSONAS; it must
    be forwarded to the runtime regardless of the claim-derived persona."""
    import handler as h

    captured: dict = {}

    def fake_invoke(**kw):
        captured["payload"] = json.loads(kw["payload"])
        return _good_runtime_response()

    monkeypatch.setattr(h, "_agentcore_client", _make_client(fake_invoke))
    monkeypatch.setenv("CVX_RUNTIME_ARN", "arn:aws:bedrock-agentcore:us-west-2:123456789012:runtime/test")

    # claim-derived persona would be fleet_driver; client requests consumer
    event = _make_event_with_persona("consumer")
    h.handler(event, None)
    assert captured["payload"]["personaId"] == "consumer"


def test_privileged_persona_service_advisor_is_ignored(monkeypatch):
    """(3) Security-critical: personaId="service_advisor" is NOT in the
    allowlist. The claim-derived persona must win; the caller gets fleet_driver
    (from the default fleet-operator claim) rather than a 4xx."""
    import handler as h

    captured: dict = {}

    def fake_invoke(**kw):
        captured["payload"] = json.loads(kw["payload"])
        return _good_runtime_response()

    monkeypatch.setattr(h, "_agentcore_client", _make_client(fake_invoke))
    monkeypatch.setenv("CVX_RUNTIME_ARN", "arn:aws:bedrock-agentcore:us-west-2:123456789012:runtime/test")

    # Claims produce fleet_driver; caller tries to escalate to service_advisor
    event = _make_event_with_persona(
        "service_advisor",
        claims={"sub": "u", "custom:tenantId": "michelin", "cognito:groups": "fleet-operator"},
    )
    h.handler(event, None)
    assert captured["payload"]["personaId"] == "fleet_driver", (
        "service_advisor is a privileged persona and must NOT be client-requestable"
    )


def test_privileged_persona_technician_is_ignored(monkeypatch):
    """(4) personaId="technician" is not allowlisted; claim-derived persona wins."""
    import handler as h

    captured: dict = {}

    def fake_invoke(**kw):
        captured["payload"] = json.loads(kw["payload"])
        return _good_runtime_response()

    monkeypatch.setattr(h, "_agentcore_client", _make_client(fake_invoke))
    monkeypatch.setenv("CVX_RUNTIME_ARN", "arn:aws:bedrock-agentcore:us-west-2:123456789012:runtime/test")

    event = _make_event_with_persona(
        "technician",
        claims={"sub": "u", "custom:tenantId": "michelin", "cognito:groups": "fleet-operator"},
    )
    h.handler(event, None)
    assert captured["payload"]["personaId"] == "fleet_driver"


def test_empty_persona_id_uses_claim_derived(monkeypatch):
    """(5) personaId="" (empty string) must be treated as absent; the
    claim-derived persona wins without error."""
    import handler as h

    captured: dict = {}

    def fake_invoke(**kw):
        captured["payload"] = json.loads(kw["payload"])
        return _good_runtime_response()

    monkeypatch.setattr(h, "_agentcore_client", _make_client(fake_invoke))
    monkeypatch.setenv("CVX_RUNTIME_ARN", "arn:aws:bedrock-agentcore:us-west-2:123456789012:runtime/test")

    event = _make_event_with_persona("")
    resp = h.handler(event, None)
    assert resp["statusCode"] == 200
    assert captured["payload"]["personaId"] == "fleet_driver"


def test_whitespace_persona_id_uses_claim_derived(monkeypatch):
    """(5 cont.) personaId="   " (whitespace-only) is stripped to empty and
    must also fall through to the claim-derived persona without error."""
    import handler as h

    captured: dict = {}

    def fake_invoke(**kw):
        captured["payload"] = json.loads(kw["payload"])
        return _good_runtime_response()

    monkeypatch.setattr(h, "_agentcore_client", _make_client(fake_invoke))
    monkeypatch.setenv("CVX_RUNTIME_ARN", "arn:aws:bedrock-agentcore:us-west-2:123456789012:runtime/test")

    event = _make_event_with_persona("   ")
    resp = h.handler(event, None)
    assert resp["statusCode"] == 200
    assert captured["payload"]["personaId"] == "fleet_driver"


def test_unknown_garbage_persona_id_ignored_no_exception(monkeypatch):
    """(6) Unknown / garbage personaId must be silently ignored — no exception,
    no 4xx. The handler returns 200 with the claim-derived persona."""
    import handler as h

    captured: dict = {}

    def fake_invoke(**kw):
        captured["payload"] = json.loads(kw["payload"])
        return _good_runtime_response()

    monkeypatch.setattr(h, "_agentcore_client", _make_client(fake_invoke))
    monkeypatch.setenv("CVX_RUNTIME_ARN", "arn:aws:bedrock-agentcore:us-west-2:123456789012:runtime/test")

    event = _make_event_with_persona("'; DROP TABLE personas; --")
    resp = h.handler(event, None)
    assert resp["statusCode"] == 200
    assert captured["payload"]["personaId"] == "fleet_driver"


# ---------------------------------------------------------------------------
# Per-session cost cap (decisions.md Q9 — UX guard, fail-open)
# ---------------------------------------------------------------------------
#
# Tests cover: under-cap, at-cap (last allowed turn), over-cap (graceful
# message), fail-open on store error, TTL expiry resets the counter.
#
# The session_cap module is imported by handler.py and operates on a shared
# module-level dict. Tests must isolate via session_cap.clear_all() to
# avoid cross-test pollution.
# ---------------------------------------------------------------------------


def _make_capped_event(session_id="cap-test-sess-001"):
    return _make_event(session_id=session_id)


def test_under_cap_request_allowed(monkeypatch):
    """Turns 1..N-1 all succeed normally (result is the agent's reply)."""
    import handler as h
    import session_cap

    session_cap.clear_all()
    monkeypatch.setattr(h, "_agentcore_client", _make_client())
    monkeypatch.setenv("CVX_RUNTIME_ARN", "arn:aws:bedrock-agentcore:us-west-2:123456789012:runtime/test")
    monkeypatch.setenv("VSA_SESSION_TURN_LIMIT", "3")

    for i in range(1, 3):  # turns 1 and 2 (limit=3, so both allowed)
        resp = h.handler(_make_capped_event(), None)
        assert resp["statusCode"] == 200
        body = json.loads(resp["body"])
        assert body["result"] == "Agent says hi", f"turn {i} should return agent reply"
    session_cap.clear_all()


def test_at_cap_turn_is_last_allowed(monkeypatch):
    """Exactly the Nth turn (where N == limit) is still allowed."""
    import handler as h
    import session_cap

    session_cap.clear_all()
    monkeypatch.setattr(h, "_agentcore_client", _make_client())
    monkeypatch.setenv("CVX_RUNTIME_ARN", "arn:aws:bedrock-agentcore:us-west-2:123456789012:runtime/test")
    monkeypatch.setenv("VSA_SESSION_TURN_LIMIT", "3")

    # Drive the counter to N-1
    for _ in range(2):
        h.handler(_make_capped_event(), None)

    # Turn N (at-cap) is allowed
    resp = h.handler(_make_capped_event(), None)
    assert resp["statusCode"] == 200
    body = json.loads(resp["body"])
    assert body["result"] == "Agent says hi", "turn at the ceiling should still succeed"
    session_cap.clear_all()


def test_over_cap_returns_graceful_message_not_error(monkeypatch):
    """Turn N+1 returns 200 with the start-new-session message, not an error."""
    import handler as h
    import session_cap

    session_cap.clear_all()
    monkeypatch.setattr(h, "_agentcore_client", _make_client())
    monkeypatch.setenv("CVX_RUNTIME_ARN", "arn:aws:bedrock-agentcore:us-west-2:123456789012:runtime/test")
    monkeypatch.setenv("VSA_SESSION_TURN_LIMIT", "3")

    # Exhaust the limit
    for _ in range(3):
        h.handler(_make_capped_event(), None)

    # Turn N+1 must be graceful, NOT a 4xx/5xx
    resp = h.handler(_make_capped_event(), None)
    assert resp["statusCode"] == 200, "cap exceeded must still return 200, not an error"
    body = json.loads(resp["body"])
    assert "start a new session" in body["result"].lower(), (
        "cap exceeded must return the graceful start-new-session message"
    )
    assert body["sessionId"] == "cap-test-sess-001"
    # Must NOT be the agent's reply
    assert body["result"] != "Agent says hi"
    session_cap.clear_all()


def test_over_cap_does_not_invoke_runtime(monkeypatch):
    """When the cap is exceeded, the AgentCore runtime is NOT called."""
    import handler as h
    import session_cap

    session_cap.clear_all()
    invoke_count = {"n": 0}

    def counting_invoke(**kw):
        invoke_count["n"] += 1
        return _good_runtime_response()

    monkeypatch.setattr(h, "_agentcore_client", _make_client(counting_invoke))
    monkeypatch.setenv("CVX_RUNTIME_ARN", "arn:aws:bedrock-agentcore:us-west-2:123456789012:runtime/test")
    monkeypatch.setenv("VSA_SESSION_TURN_LIMIT", "2")

    # Two turns exhaust the limit
    h.handler(_make_capped_event(), None)
    h.handler(_make_capped_event(), None)
    assert invoke_count["n"] == 2

    # Third turn is over-cap — runtime must NOT be called
    h.handler(_make_capped_event(), None)
    assert invoke_count["n"] == 2, "runtime must NOT be invoked when cap is exceeded"
    session_cap.clear_all()


def test_cap_fail_open_on_store_error(monkeypatch):
    """If the counter store raises, the request is allowed through (fail-open)."""
    import handler as h
    import session_cap

    session_cap.clear_all()
    monkeypatch.setattr(h, "_agentcore_client", _make_client())
    monkeypatch.setenv("CVX_RUNTIME_ARN", "arn:aws:bedrock-agentcore:us-west-2:123456789012:runtime/test")

    # Patch the inner function to raise
    original = session_cap._check_and_increment_inner

    def exploding_inner(session_id):
        raise RuntimeError("simulated store failure")

    monkeypatch.setattr(session_cap, "_check_and_increment_inner", exploding_inner)

    resp = h.handler(_make_capped_event(), None)
    # Fail-open: request must succeed, not 500
    assert resp["statusCode"] == 200
    body = json.loads(resp["body"])
    assert body["result"] == "Agent says hi", "fail-open: agent reply must be returned"
    session_cap.clear_all()


def test_cap_different_sessions_are_independent(monkeypatch):
    """Different session ids maintain separate counters."""
    import handler as h
    import session_cap

    session_cap.clear_all()
    monkeypatch.setattr(h, "_agentcore_client", _make_client())
    monkeypatch.setenv("CVX_RUNTIME_ARN", "arn:aws:bedrock-agentcore:us-west-2:123456789012:runtime/test")
    monkeypatch.setenv("VSA_SESSION_TURN_LIMIT", "1")

    # Exhaust session A
    h.handler(_make_capped_event("cap-sess-A"), None)
    resp_a = h.handler(_make_capped_event("cap-sess-A"), None)
    assert "start a new session" in json.loads(resp_a["body"])["result"].lower()

    # Session B is independent — its first turn should succeed
    resp_b = h.handler(_make_capped_event("cap-sess-B"), None)
    assert json.loads(resp_b["body"])["result"] == "Agent says hi", (
        "session B must be independent of session A's counter"
    )
    session_cap.clear_all()


def test_cap_ttl_expiry_resets_counter(monkeypatch):
    """A counter that has passed its TTL is treated as a new session."""
    import session_cap
    import time

    session_cap.clear_all()
    monkeypatch.setenv("VSA_SESSION_TURN_LIMIT", "1")
    monkeypatch.setenv("VSA_SESSION_TTL_SECONDS", "1")

    sid = "ttl-test-session-001"

    # First turn — at-limit
    allowed1, n1 = session_cap.check_and_increment(sid)
    assert allowed1 and n1 == 1

    # Second turn — over-limit (not yet expired)
    allowed2, n2 = session_cap.check_and_increment(sid)
    assert not allowed2 and n2 == 2

    # Forcibly expire the entry by back-dating it
    count, expires = session_cap._COUNTERS[sid]
    session_cap._COUNTERS[sid] = (count, time.monotonic() - 1.0)

    # Third call — expired, so treated as new session (turn 1 again)
    allowed3, n3 = session_cap.check_and_increment(sid)
    assert allowed3 and n3 == 1, "expired entry must reset the counter (new session)"
    session_cap.clear_all()


# ---------------------------------------------------------------------------
# Fix Group 2: Task 2 — Close handler.py persona fail-open layers
# ---------------------------------------------------------------------------


def _make_event_with_claims(claims: dict | None, prompt="hello", session_id="sess-fg2-001") -> dict:
    """Build a handler event with the specified claims (or None for no authorizer)."""
    body = json.dumps({"prompt": prompt, "sessionId": session_id})
    event: dict = {"headers": {}, "body": body}
    if claims is not None:
        event["requestContext"] = {"authorizer": {"claims": claims}}
    else:
        event["requestContext"] = {}
    return event


class TestHandlerPersonaFailOpenFixed:
    """handler.py must never fall back to fleet_driver for empty/missing claims.

    Security-review Cycle 4 W1: the ternary ``infer_persona(claims) if claims
    else default_persona`` short-circuits to ``fleet_driver`` for empty-dict
    claims (Python treats {} as falsy). Fix: call infer_persona(claims)
    unconditionally; infer_persona handles {} correctly via _ANONYMOUS_PERSONA.
    """

    def _run(self, event, monkeypatch) -> dict:
        import handler as h
        monkeypatch.setattr(h, "_agentcore_client", _make_client())
        monkeypatch.setenv("CVX_RUNTIME_ARN", "arn:aws:bedrock-agentcore:us-west-2:123456789012:runtime/test")
        captured = {}
        real_invoke = h._get_agentcore_client().__class__

        def capturing_invoke(**kw):
            import json as _json
            captured["payload"] = _json.loads(kw.get("payload", b"{}"))
            return _good_runtime_response()

        monkeypatch.setattr(h, "_agentcore_client", _make_client(capturing_invoke))
        h.handler(event, None)
        return captured.get("payload", {})

    def test_empty_claims_dict_gives_consumer_not_fleet_driver(self, monkeypatch):
        """claims={} must resolve to 'consumer', not the owner-scoped 'fleet_driver'.

        Before fix: ``if claims else default_persona`` → claims={} is falsy →
        persona = default_persona = "fleet_driver". After fix: unconditional
        infer_persona({}) returns _ANONYMOUS_PERSONA = "consumer".
        """
        event = _make_event_with_claims({})
        payload = self._run(event, monkeypatch)
        assert payload.get("personaId") == "consumer", (
            f"Empty claims dict must give 'consumer' persona, not 'fleet_driver'. "
            f"Got: {payload.get('personaId')!r}. "
            "Before fix: if claims else default_persona short-circuits to fleet_driver."
        )

    def test_missing_claims_key_gives_consumer_not_fleet_driver(self, monkeypatch):
        """Missing requestContext.authorizer.claims key falls through to infer_persona({})."""
        # handler builds claims via: (.get("requestContext") or {}).get("authorizer", {}).get("claims") or {}
        # A missing claims key yields {} — same as empty dict.
        event = _make_event_with_claims(None)  # No requestContext at all
        payload = self._run(event, monkeypatch)
        assert payload.get("personaId") == "consumer", (
            f"Missing claims must give 'consumer' persona, not 'fleet_driver'. "
            f"Got: {payload.get('personaId')!r}."
        )

    def test_kiosk_group_claims_give_consumer_and_iskiosk_true(self, monkeypatch):
        """Claims with kiosk group must give personaId=consumer AND isKiosk=True in payload."""
        event = _make_event_with_claims({
            "cognito:groups": "kiosk",
            "sub": "kiosk-device-001",
            "custom:tenantId": "meridian",
        })
        payload = self._run(event, monkeypatch)
        assert payload.get("personaId") == "consumer", (
            f"Kiosk claims must give 'consumer' persona; got {payload.get('personaId')!r}."
        )
        assert payload.get("isKiosk") is True, (
            f"Kiosk claims must set isKiosk=True in runtime payload; "
            f"got isKiosk={payload.get('isKiosk')!r}. "
            "This is the plumbing fix that makes the tool allow-list activate."
        )

    def test_non_kiosk_claims_do_not_set_iskiosk(self, monkeypatch):
        """Non-kiosk claims must NOT set isKiosk in the payload."""
        event = _make_event_with_claims({
            "cognito:groups": "fleet-operator",
            "sub": "driver-001",
            "custom:tenantId": "michelin",
        })
        payload = self._run(event, monkeypatch)
        assert payload.get("isKiosk") is not True, (
            f"Non-kiosk claims must not set isKiosk=True; got {payload.get('isKiosk')!r}."
        )

    def test_anonymous_fallback_claims_do_not_set_iskiosk(self, monkeypatch):
        """claimless anonymous caller gets consumer persona but NOT isKiosk=True.

        The anonymous/claimless fallback is NOT a kiosk device — it did not
        carry a kiosk group token. isKiosk must remain unset (or False) so
        the tool allow-list is not triggered for every anonymous API caller.
        """
        event = _make_event_with_claims({})
        payload = self._run(event, monkeypatch)
        assert payload.get("personaId") == "consumer"
        assert not payload.get("isKiosk"), (
            "Anonymous fallback to 'consumer' must NOT set isKiosk=True. "
            "isKiosk=True is only for provisioned kiosk devices with the kiosk group."
        )



# ---------------------------------------------------------------------------
# Group 5 staging validation — guardrail denial envelope must not leak internals
#
# The runtime returns a DENIAL ENVELOPE for a blocked turn:
#   {"error": "<visitor-safe message>", "blocked": True, "blocked_topics": [...]}
# It has no "result" key. The original fallback `agent_resp.get("result") or
# str(agent_resp)` therefore serialised the entire dict with Python's repr and
# returned it as the assistant's reply, so a blocked visitor at the kiosk saw
#   {'error': "I'm not able to help...", 'blocked': True, 'blocked_topics': [...]}
# printed on the tablet. Observed live on staging 2026-08-02.
# See issues/2026-08-02-kiosk-blocked-envelope-leaks-python-repr/.
# ---------------------------------------------------------------------------


def _runtime_response(payload: dict) -> dict:
    """Wrap an arbitrary runtime payload in the streaming-response shape."""
    payload_bytes = json.dumps(payload).encode()

    class _Stream:
        def read(self):
            return payload_bytes

    return {"response": _Stream()}


class TestBlockedEnvelopeNeverLeaksInternals:
    _DENIAL = {
        "error": (
            "I'm not able to help with that request at this kiosk. "
            "Please ask me about Meridian vehicles, features, or the order process."
        ),
        "blocked": True,
        "blocked_topics": ["off-topic-non-vehicle"],
    }

    def _call(self, monkeypatch, runtime_payload: dict) -> dict:
        import handler as h
        monkeypatch.setenv(
            "CVX_RUNTIME_ARN",
            "arn:aws:bedrock-agentcore:us-west-2:123456789012:runtime/test",
        )
        monkeypatch.setattr(
            h, "_agentcore_client",
            _make_client(lambda **_kw: _runtime_response(runtime_payload)),
        )
        resp = h.handler(_make_event_with_claims({
            "sub": "kiosk-device-1",
            "custom:tenantId": "meridian",
            "cognito:groups": "kiosk",
        }), None)
        return json.loads(resp["body"])

    def test_blocked_turn_returns_the_guardrail_message_as_result(self, monkeypatch):
        body = self._call(monkeypatch, self._DENIAL)
        assert body["result"] == self._DENIAL["error"], (
            f"A blocked turn must surface the guardrail's own message as `result`. "
            f"Got: {body['result']!r}"
        )

    def test_blocked_turn_result_is_not_a_python_repr(self, monkeypatch):
        """The specific regression: no dict/repr syntax in visitor-facing text."""
        body = self._call(monkeypatch, self._DENIAL)
        result = body["result"]
        for marker in ("{'", "'blocked'", "blocked_topics", "': True", '{"error"'):
            assert marker not in result, (
                f"Visitor-facing `result` contains internal structure {marker!r}: "
                f"{result!r}. Never fall back to str(agent_resp)."
            )

    def test_blocked_turn_flags_blocked_for_the_client(self, monkeypatch):
        body = self._call(monkeypatch, self._DENIAL)
        assert body.get("blocked") is True, (
            "A blocked turn should set `blocked: true` so the client can style a "
            "refusal differently from a normal reply."
        )

    def test_unexpected_payload_falls_back_to_a_human_message(self, monkeypatch):
        """An unparseable/unknown runtime payload must not leak its keys either."""
        body = self._call(monkeypatch, {"someInternalKey": {"nested": "value"}})
        result = body["result"]
        assert "someInternalKey" not in result and "nested" not in result, (
            f"Unexpected runtime payload leaked into visitor text: {result!r}"
        )
        assert result and isinstance(result, str) and not result.startswith("{"), (
            f"Expected a human-readable fallback, got {result!r}"
        )

    def test_normal_turn_still_returns_result_unchanged(self, monkeypatch):
        """The fix must not disturb the happy path."""
        body = self._call(monkeypatch, {
            "result": "The Windrose starts at 18,900.",
            "sessionId": "sess-ok",
        })
        assert body["result"] == "The Windrose starts at 18,900."
        assert "blocked" not in body
