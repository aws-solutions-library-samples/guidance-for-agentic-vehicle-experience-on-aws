"""AgentCore Runtime entrypoint for the VSA supervisor.

Deploys with:
    agentcore configure -e agents/supervisor/agentcore_app.py -r us-east-1
    agentcore deploy

Invocation contract:

    Headers:
        X-Amzn-Bedrock-AgentCore-Runtime-Custom-User-Token: <Cognito IdToken>
    Payload (JSON body):
        {
            "prompt":   "<user utterance>",         # required
            "tenantId": "<tenant slug>",            # required
            "vin":      "<optional VIN>",           # optional
            "jwt":      "<Cognito IdToken>"         # fallback if no custom header
        }

    AgentCore itself is authenticated at the caller tier via IAM
    (bedrock-agentcore:InvokeAgentRuntime). AgentCore reserves the
    Authorization header for SigV4 on IAM-auth runtimes, so we use a
    custom X-Vsa-User-Token header for the user's Cognito IdToken. When
    we later enable AgentCore Inbound Auth with a Cognito authorizer,
    this switches to accepting the token in Authorization.

Response:
    {
        "result":         "<final assistant text>",
        "sessionId":      "<AgentCore runtimeSessionId>",
        "classification": "P0" | "P1" | "P2" | "P3" | null,
        "vin":            "<VIN after the turn>"
    }

On error:
    {
        "error":  "<short reason>",
        "detail": "<optional context>"
    }
    Status code still 200 — AgentCore treats any exception as a 500,
    but we prefer returning structured errors so callers (iOS, voice
    wrapper) can handle them uniformly. Unrecoverable errors re-raise.
"""

from __future__ import annotations

import logging

from bedrock_agentcore.runtime import BedrockAgentCoreApp
from bedrock_agentcore.runtime.context import RequestContext

from agents.supervisor.config import Config
from agents.supervisor.guardrails import KioskGuardrailEnforcer
from agents.supervisor.session import SessionContext
from agents.supervisor.session_cap import check_and_increment, SESSION_LIMIT_RESPONSE
from agents.supervisor.tools.escalate import take_last_escalation
from agents.supervisor.vehicle_location import resolve_session_coords
from agents.supervisor.supervisor import build_supervisor

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")

app = BedrockAgentCoreApp()


@app.entrypoint
def invoke(payload: dict, context: RequestContext) -> dict:
    """AgentCore entrypoint. See module docstring for contract."""
    try:
        prompt = _required(payload, "prompt")
        tenant_id = _required(payload, "tenantId")
        vin = payload.get("vin")
        jwt = _extract_bearer(context.request_headers or {}, payload)
    except InvocationError as e:
        log.warning("Rejecting invocation: %s", e)
        return {"error": str(e)}

    session_id = context.session_id or _fallback_session_id()
    config = Config.from_env()

    persona_id = _resolve_persona(payload)

    # Segment drives persona-appropriate service routing — notably the OEM
    # branch of find_service_center, which scopes to authorised dealers of
    # the vehicle's brand. Defaults to "fleet" (broadest) when absent.
    _seg = str(payload.get("tenantSegment") or "").strip().lower()
    tenant_segment = _seg if _seg in {"fleet", "oem", "rental"} else "fleet"

    ctx = SessionContext(
        tenant_id=tenant_id,
        session_id=session_id,
        jwt=jwt,
        persona_id=persona_id,
        vin=vin,
        # `/escalate` requires vehicleId as well as vin. Without threading it
        # through, a human handoff from the text surface failed validation
        # before reaching Amazon Connect and the model narrated a vague
        # "session issue" to the customer.
        vehicle_id=payload.get("vehicleId"),
        # Human-facing brand name. Without this the prompt rendered
        # "You are the  shopping concierge" (empty), and the model filled the
        # blank by inferring a brand from the raw tenant id — which is an
        # internal slug, not a brand. That produced a wrong company name in
        # customer-visible copy.
        tenant_display=str(payload.get("tenantDisplayName") or "").strip(),
        distance_unit=str(payload.get("distanceUnit") or "").strip() or None,
        tenant_segment=tenant_segment,
        # Kiosk flag plumbing (spec 2026-07-31-zone2-order-backend-and-kiosk D3 Critical fix).
        # handler.py sets isKiosk=True in the runtime payload when infer_persona
        # resolved via the 'kiosk' Cognito group.  Reading it here and passing it
        # to the SessionContext constructor is what makes the tool allow-list and
        # session_manager_or_none guards fire on the real production path.
        # Coerce to bool: only True (from handler.py) activates kiosk mode;
        # any absent/None/False/0 leaves the flag False (non-kiosk default).
        is_kiosk=bool(payload.get("isKiosk")),
    )

    log.info(
        "invoke: tenant=%s session=%s vin=%s prompt_len=%d",
        tenant_id,
        session_id,
        vin,
        len(prompt),
    )

    # ── Kiosk guardrail — TEXT PATH ONLY (spec D6) ───────────────────────────
    #
    # Zone 2 is an anonymous public kiosk. We check BOTH the user's input and
    # the model's output against the authored kiosk guardrail policy.
    # Deny-on-failure: a guardrail-service error returns the blocked message
    # rather than passing through (fail-closed for an unattended public surface).
    #
    # The bidi GuardrailChecker (bidi_app.py:682-683) is a dry-run observer and
    # is NOT changed here — that is a separate decision per spec D6.
    #
    # The enforcer is enabled when VSA_KIOSK_GUARDRAIL_ID is set in the runtime
    # environment (CDK stack output). When unset (local dev / unconfigured),
    # the enforcer is disabled and logs a WARNING.
    _guardrail = KioskGuardrailEnforcer()

    # Check user INPUT before invoking the agent.
    input_denial = _guardrail.check_input(prompt)
    if input_denial:
        log.info(
            "invoke: guardrail BLOCKED input session=%s topics=%s",
            session_id,
            input_denial.get("blocked_topics"),
        )
        return input_denial

    # ── Per-session turn ceiling (decisions.md Q9) ──────────────────────────
    #
    # UX guard against a single stuck or adversarial session. Deliberately
    # fails open: if the counter store errors, the turn is allowed through and
    # the error is logged. A broken counter must not take the booth down.
    #
    # This is NOT the adversarial cost control — a client can bypass it by
    # rotating the session id. The real spend backstop is an API Gateway usage
    # plan + AWS Budgets action (Group 4).
    #
    # Default ceiling: 25 turns (VSA_SESSION_TURN_LIMIT).
    # Counter TTL: 4 hours (VSA_SESSION_TTL_SECONDS).
    _cap_allowed, _cap_turns = check_and_increment(session_id)
    if not _cap_allowed:
        log.info(
            "invoke: session turn cap reached session=%s turns=%d",
            session_id,
            _cap_turns,
        )
        return {
            "result": SESSION_LIMIT_RESPONSE,
            "sessionId": session_id,
            "classification": None,
            "vin": None,
        }

    # Resolve where the VEHICLE is, so location-aware tools work on the text
    # surface. `find_service_center` reads coordinates off the session and
    # returns nothing when they are absent — which is why a test-ride request
    # could not name a dealer from this path. Best-effort: a failed lookup
    # leaves the coords unset and the tool degrades to saying location is
    # unavailable, rather than guessing.
    resolve_session_coords(ctx, config.api_base)

    agent = build_supervisor(ctx, config)
    try:
        result = agent(prompt)
    except Exception as e:  # noqa: BLE001
        # We re-raise for AgentCore to log, but also capture the tenant
        # and session for CloudWatch correlation.
        log.exception("Supervisor invocation failed for session=%s", session_id)
        raise

    result_text = _extract_text(result)

    # Check model OUTPUT before returning to caller.
    output_denial = _guardrail.check_output(result_text)
    if output_denial:
        log.info(
            "invoke: guardrail BLOCKED output session=%s topics=%s",
            session_id,
            output_denial.get("blocked_topics"),
        )
        return output_denial

    response: dict = {
        "result": result_text,
        "sessionId": ctx.session_id,
        "classification": ctx.triage_classification,
        "vin": ctx.vin,
    }
    # Surface KB citations from the final retrieve() call (if any) so the
    # route Lambda can pass them to the browser. Without this, KB-grounded
    # answers are indistinguishable from model-knowledge — the PO UAT
    # 2026-06-22 finding #1. The retrieve tool side-effects this list on
    # every call (tools/retrieve.py:_record_citations).
    citations = getattr(ctx, "last_kb_citations", None)
    if citations:
        response["citations"] = citations

    # Human-handoff credentials, when the model escalated during this turn.
    # Additive and optional — callers that don't understand the key (the CMS
    # web chat) ignore it, so this is safe on the shared text runtime.
    escalation = take_last_escalation(ctx.session_id)
    if escalation:
        log.info(
            "invoke: surfacing escalation contact=%s severity=%s",
            escalation.get("contactId"), escalation.get("severity"),
        )
        response["escalation"] = escalation
    return response


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class InvocationError(ValueError):
    """Caller-side invocation contract violation."""


_DEFAULT_PERSONA = "consumer"


def _resolve_persona(payload: dict) -> str:
    """Read and validate the optional ``personaId`` from the payload.

    Returns the payload value if it is a valid persona slug (i.e. present
    in ``PERSONA_PRODUCT_BINDINGS``), otherwise ``_DEFAULT_PERSONA``
    (``consumer``) for any missing, empty, or unrecognised value. Never raises.

    The default is deliberately ``consumer`` — the LEAST-privileged persona —
    and not ``fleet_driver``. An earlier revision of this docstring claimed
    ``fleet_driver``, which was both stale and an accurate description of a
    real vulnerability: falling back to an owner-scoped persona meant a
    claimless or malformed request inherited access to owner data products
    (``customer_360``, ``customer_interactions``, ``vehicle_identity``). That
    fail-open was closed in security-review Cycle 4 W1. Do not widen this
    default; a request that cannot prove who it is must get the narrowest
    persona, not the most convenient one.

    Note this validates the payload's *hint* only. For kiosk sessions the
    authoritative clamp happens server-side from the Cognito claims in
    ``lambdas/api-assistant-chat/persona_map.py::infer_persona``, and the tool
    allow-list in ``build_supervisor`` gates capability regardless of what
    persona is resolved here.
    """
    from agents.supervisor.persona_definitions import PERSONA_PRODUCT_BINDINGS  # lazy to avoid circular
    raw = payload.get("personaId")
    if raw and isinstance(raw, str) and raw.strip() in PERSONA_PRODUCT_BINDINGS:
        return raw.strip()
    return _DEFAULT_PERSONA


def _required(payload: dict, key: str) -> str:
    value = payload.get(key)
    if not value or not isinstance(value, str):
        raise InvocationError(f"Missing or invalid required field: {key!r}")
    return value


def _extract_bearer(headers: dict[str, str], payload: dict | None = None) -> str:
    """Pull the Cognito IdToken from the user-token header or payload.

    AgentCore reserves the `Authorization` header for IAM SigV4 (unless
    the runtime is configured with customJWTAuthorizer). To ship a
    per-user Cognito IdToken through an IAM-auth runtime, we use the
    platform's official custom header namespace
    `X-Amzn-Bedrock-AgentCore-Runtime-Custom-*`. Falls back to
    payload.jwt for local dev and for the CLI `agentcore invoke` path
    which does not support custom headers.

    When we later enable AgentCore Inbound Auth with a Cognito JWT
    authorizer, AgentCore will validate the Authorization header itself
    and pass it through; this function will add an Authorization read
    as the first preference. For now Authorization is intentionally not
    inspected so a stray SigV4 auth header can't be misread.
    """
    # AgentCore only allows custom headers matching
    # (Authorization | X-Amzn-Bedrock-AgentCore-Runtime-Custom-*). Authorization
    # is reserved for OAuth; we use the official custom namespace instead.
    USER_TOKEN_HEADER = "X-Amzn-Bedrock-AgentCore-Runtime-Custom-User-Token"
    token = (
        headers.get(USER_TOKEN_HEADER)
        or headers.get(USER_TOKEN_HEADER.lower())
    )
    if not token and payload:
        token = payload.get("jwt")
    if not token:
        raise InvocationError(
            "Missing user token. Supply X-Amzn-Bedrock-AgentCore-Runtime-Custom-User-Token "
            "header or payload.jwt"
        )
    token = token.strip()
    if not token:
        raise InvocationError("User token is empty")
    return token


def _fallback_session_id() -> str:
    """Generate a session id when AgentCore did not provide one.

    Should be rare — RequestContext.session_id comes from runtimeSessionId
    which is required by invoke_agent_runtime. But local development with
    `python agentcore_app.py` and a plain curl hits us without one.
    """
    import uuid
    return f"vsa-sess-local-{uuid.uuid4().hex[:12]}"


def _extract_text(result) -> str:
    """Pull final assistant text from a Strands AgentResult."""
    msg = getattr(result, "message", None)
    if msg is None:
        return str(result)
    content = msg.get("content") if isinstance(msg, dict) else getattr(msg, "content", None)
    if not content:
        return str(msg)
    parts: list[str] = []
    for block in content:
        if isinstance(block, dict) and "text" in block:
            parts.append(block["text"])
    return "\n".join(parts) if parts else str(msg)


if __name__ == "__main__":
    # Local run: python -m agents.supervisor.agentcore_app
    # Binds to 0.0.0.0:8080 by default per AgentCore convention.
    app.run()
