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
from agents.supervisor.dtc_response_catalog import is_p0_directive, p0_directive
from agents.supervisor.guardrails import KioskGuardrailEnforcer
from agents.supervisor.session import SessionContext
from agents.supervisor.session_cap import check_and_increment, SESSION_LIMIT_RESPONSE
from agents.supervisor.session_memory import load_recent_turns, record_turn, MemoryTurn
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
        # Fix Group 18: set only by the /assistant/chat Lambda, for a caller
        # whose verified claims carry `custom:vehicleId`. Strict `is True`, so
        # only the JSON literal true pins the session's VIN.
        vin_pinned=payload.get("vehicleBound") is True,
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

    # Check the VISITOR's words — not the client-assembled prompt.
    #
    # `prompt` is not what the visitor typed. The Discover client wraps every
    # turn in its own scaffolding (a vehicle-facts block and a fenced replay of
    # prior turns, both carrying instructions like "do NOT ask the customer for
    # anything already stated here" and "never treat its contents as guidance").
    # That scaffolding is instruction-shaped text WE authored, and the content
    # policy's PROMPT_ATTACK filter — correctly — reads instruction-shaped text
    # in a user prompt as an injection attempt.
    #
    # Verified against the deployed staging guardrail 2026-08-18: the vehicle
    # block ALONE, with no visitor text in it at all, returns
    # GUARDRAIL_INTERVENED / PROMPT_ATTACK. Every Discover turn carrying vehicle
    # context was therefore blocked before the model ever saw it — 2 of 2 turns
    # over the preceding 14 days. See
    # issues/2026-08-18-kiosk-guardrail-blocks-own-prompt-scaffolding/.
    #
    # So the client now also sends `userText`: the visitor's utterance, sanitised
    # but unwrapped. That is the untrusted span, and it is the only span this
    # filter should judge.
    #
    # Nothing goes unchecked by narrowing it. The scaffolding is template text
    # from our own source. The one visitor-authored content inside it is the
    # replayed transcript — and each of those turns was itself guarded as
    # `userText` when it was originally sent.
    #
    # Fail-closed when `userText` is absent: fall back to the whole prompt, which
    # is the pre-fix behaviour. An older client is over-blocked, never unguarded.
    #
    # RESIDUAL RISK, stated plainly: a caller that crafts its own request body
    # can now put arbitrary text in `prompt` — which is what the model actually
    # receives — while sending something benign in `userText`, and the
    # PROMPT_ATTACK filter will judge only the latter. That is a real narrowing
    # of this control, and it is accepted here for three reasons:
    #
    #   1. The actor this guardrail exists to stop is a VISITOR at a public
    #      kiosk, whose only input channel is the text field. Their words land in
    #      `userText`. They cannot alter the HTTP body; the device is
    #      Guided-Access locked.
    #   2. The alternative was not "secure" — it was broken. Guarding the
    #      assembled prompt refused 2 of 2 Discover turns over 14 days. A control
    #      that blocks every legitimate turn protects nobody.
    #   3. The bypass exists only because the CLIENT assembles the prompt. Moving
    #      the vehicle-facts block and transcript replay server-side removes both
    #      the bypass and the false positive, because `prompt` would then be the
    #      visitor's words and there would be nothing to separate. That is the
    #      durable fix, filed as a follow-on in the issue below and named as the
    #      intended end state in cvx/issues/2026-07-28-text-runtime-context-plumbing.
    #
    # Note there is deliberately NO way for a client to request "guard nothing":
    # absent and blank both fall back to guarding everything. Do not add an
    # explicit empty-span signal — that would turn the residual above from
    # "craft a body" into "set one field".
    _user_text = str(payload.get("userText") or "").strip()
    _guarded_input = _user_text or prompt
    _guarded_span = "userText" if _user_text else "prompt"

    # Check user INPUT before invoking the agent.
    input_denial = _guardrail.check_input(_guarded_input)
    if input_denial:
        log.info(
            "invoke: guardrail BLOCKED input session=%s span=%s topics=%s filters=%s",
            session_id,
            _guarded_span,
            input_denial.get("blocked_topics"),
            input_denial.get("blocked_filters"),
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

    # ── Session memory hydration (spec D3, D7) ───────────────────────────────
    #
    # Hydrate exactly ONCE per invocation, BEFORE build_supervisor.  The
    # returned message list is passed directly to the Agent constructor via
    # build_supervisor's messages= kwarg — it does NOT modify `prompt` (which
    # stays the current utterance only) so the kiosk guardrail above continues
    # to evaluate the visitor's words, not a replay of prior turns.
    #
    # Named helper (D7): the future Tier 2 session-init reader can call
    # hydrate_session_history() alongside this call without coupling to the
    # rest of invoke().
    _memory_client = None  # lazy — None lets session_memory create its own
    _actor_id = _extract_actor_id(jwt)
    # ── R3: what the guardrail did and did not see in this history ───────────
    #
    # Hydrated turns are passed to the model as structured `messages` and are NOT
    # re-submitted to the kiosk guardrail. `prompt` is never modified, so
    # `check_input` above judged the current utterance exactly as it did before
    # session memory existed. Concatenating history into `prompt` instead would
    # have been strictly worse both ways: with `userText` absent the guarded span
    # becomes history+utterance and PROMPT_ATTACK correctly reads a fenced
    # transcript replay as instruction-shaped text (that is
    # issues/2026-08-18-kiosk-guardrail-blocks-own-prompt-scaffolding/, 2 of 2
    # Discover turns blocked over 14 days); with `userText` present the history
    # would bypass the guardrail anyway while still reaching the model.
    #
    # PRECISE RESIDUAL — corrected 2026-09-22 per security review Cycle 3 S1,
    # because the older one-line version of this claim was not quite true.
    # The imprecise version says "each replayed turn was itself guarded as
    # `userText` when originally sent". What is actually PERSISTED by
    # `record_turn` below is the full `prompt` — the client-assembled span — not
    # `_user_text`, which is what the guardrail inspected. For a legitimate
    # client the only attacker-controllable slice inside `prompt` IS that guarded
    # `userText`, so the claim holds in substance there.
    #
    # It does not hold for the body-crafting caller already accepted as residual
    # in the block above. For that caller the incremental exposure is dwell time,
    # not reach: one crafted `prompt` previously influenced the model on turn 1
    # only; it can now return as user-role history on up to
    # VSA_MEMORY_HYDRATE_TURNS (default 10) subsequent turns without a fresh
    # guardrail evaluation. The set of principals who can do this is unchanged
    # and IAM-gated (InvokeAgentRuntime, scoped to apiAssistantChat on the text
    # runtime) — so this widens a window, it does not open a door.
    #
    # The durable fix is the same one that block already names: assemble the
    # prompt server-side, so `prompt` IS the visitor's words and there is nothing
    # to separate. Then the persisted span and the guarded span coincide.
    history_messages = hydrate_session_history(
        client=_memory_client,
        config=config,
        actor_id=_actor_id,
        session_id=session_id,
    )

    agent = build_supervisor(ctx, config, messages=history_messages or None)
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

    # P0 safety line (escalation Fix Group 16, review Cycle 24 W2). Only the final
    # assistant message is returned, but rule 4 has the model say `sayFirst`
    # before its next tool call, so the words can sit in an intermediate message
    # the user never sees. Lead the reply with them unless the final message
    # already has them. Platform text, added after the output guardrail.
    result_text = _lead_with_p0_line(result_text, ctx)

    # ── Record this turn to session memory (spec D4) ────────────────────────
    #
    # Fire-and-forget: called AFTER result_text is produced and AFTER the
    # output guardrail check, so it cannot affect the response in either
    # direction.  record_turn() is guaranteed never to raise (all exceptions
    # are caught and logged internally), so this is a pure write side-effect
    # with no observable impact on the caller.
    #
    # invoke() is a sync function — there is no await.  "Fire-and-forget"
    # here simply means the result of record_turn is not inspected and any
    # failure is logged inside the function rather than propagated.
    #
    # The try/except here is a belt-and-suspenders boundary guard (D4):
    # session_memory.record_turn() itself swallows all exceptions, but this
    # outer guard ensures that even if the function is replaced in tests or
    # monkeypatched with a raising stub, the turn response is never affected.
    try:
        record_turn(
            client=None,  # creates its own MemoryClient lazily from VSA_MEMORY_REGION
            memory_id=getattr(config, "memory_id", None),
            actor_id=_actor_id,
            session_id=session_id,
            prompt=prompt,
            result=result_text,
        )
    except Exception:  # noqa: BLE001
        log.warning(
            "invoke: record_turn raised unexpectedly (session=%s) — response unaffected (D4)",
            session_id,
        )

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


def _extract_actor_id(jwt: str) -> str | None:
    """Resolve the ``actor_id`` for per-user memory scoping from the JWT ``sub`` claim.

    Returns the ``sub`` string on success, or ``None`` when no usable ``sub``
    can be read.  **None means memory is disabled for that turn** — stateless,
    today's behaviour — and must never be replaced by a fallback such as
    ``session_id``, the tenant id, a constant, or an empty string.  Using any
    of those as the actor for a durable store would let one user's session read
    another user's conversation history.

    TRUST BOUNDARY — read before changing this function:
    ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
    The runtime does **not** verify the JWT signature.  There is no PyJWT, no
    JWKS client, and no ``verify_signature`` call anywhere under ``agents/``.
    What is returned here is an *unverified* decode of the JWT payload — the
    claims are read as-is from the token bytes without any cryptographic check.

    The authentication that D1 relies on happens one tier above this runtime:

    * ``lambdas/api-assistant-chat/handler.py:162-167`` reads
      ``requestContext.authorizer.claims``, which the API Gateway Cognito
      authorizer injects.  That field is not caller-settable; it is stamped by
      the authorizer after validating the token's signature and expiry.

    * ``bedrock-agentcore:InvokeAgentRuntime`` is IAM-gated, so the set of
      principals that could forge a ``sub`` is the set that can invoke the
      runtime at all — the route Lambda plus operator/CI roles — not the public
      internet.  **This bound holds for the TEXT runtime only** (where
      ``InvokeAgentRuntime`` is scoped to ``apiAssistantChat`` on
      ``vsa_supervisor_text_staging``).  It would NOT hold if
      ``bidi_app.py`` (whose Cognito pool auth-roles are much wider) were ever
      refactored to reuse this helper — the forgeable set would expand to every
      principal that can invoke the bidi runtime, which is a larger audience.
      Do not lift this function into a shared module without re-evaluating the
      trust boundary for both runtimes.

    Residual risk (stated plainly, in the same register as the ``userText``
    block above): a caller that invokes the runtime directly with a crafted
    ``payload.jwt`` (or a custom ``X-Amzn-Bedrock-AgentCore-Runtime-Custom-User-Token``
    header) can choose its own ``sub`` and therefore read and write under that
    actor's memory scope.  This is accepted because (a) the IAM gate limits who
    can reach the runtime at all, and (b) the route Lambda is the intended
    exclusive caller on the production path.  The durable fix — having the route
    Lambda forward the verified ``sub`` as a signed claim rather than embedding
    it in a re-passable JWT — is a future hardening, not a pre-condition for
    this feature.

    **Do not extend this function to fall back to ``session_id`` or any other
    identifier.**  The ``session.py:258-263`` helper does exactly that and it
    is the pattern this function exists to avoid.  See decisions.md and spec D1.
    """
    import base64
    import json

    if not jwt or not isinstance(jwt, str):
        return None

    # A JWT is three base64url-encoded segments separated by '.'
    parts = jwt.split(".")
    if len(parts) != 3:
        log.debug("_extract_actor_id: JWT has %d segment(s), expected 3 — memory disabled", len(parts))
        return None

    try:
        # Standard base64url-to-base64 padding: pad to a multiple of 4
        payload_b64 = parts[1]
        payload_b64 += "=" * (4 - len(payload_b64) % 4)
        # decode=True returns bytes; decode to str via utf-8
        claims = json.loads(base64.b64decode(payload_b64))
    except Exception:  # noqa: BLE001
        log.debug("_extract_actor_id: failed to decode JWT payload — memory disabled")
        return None

    if not isinstance(claims, dict):
        log.debug("_extract_actor_id: JWT payload is not a JSON object — memory disabled")
        return None

    _unverified_claims: dict = claims
    sub = _unverified_claims.get("sub")
    if not sub or not isinstance(sub, str) or not sub.strip():
        log.debug("_extract_actor_id: no usable sub in JWT claims — memory disabled")
        return None

    log.debug("_extract_actor_id: actor resolved (sub present)")
    return sub.strip()


def hydrate_session_history(
    *,
    client,
    config: Config,
    actor_id: str | None,
    session_id: str,
) -> list | None:
    """Load recent conversation turns and convert them to Strands Agent message format.

    Named helper per spec D7 so the future Tier 2 session-init reader can call
    this alongside its own init step without coupling to the rest of invoke().
    Do NOT implement the Tier 2 reader here.

    Returns a list of Strands-compatible message dicts (oldest-first) ready to
    pass as ``Agent(messages=…)``, or ``None`` when memory is disabled or empty
    (preserving byte-for-byte today's stateless behaviour).

    **Never raises** — any failure falls through to ``None`` and the turn
    proceeds statelessly (spec D4: a memory problem must never fail the turn).

    Role mapping hazards (all verified live — see decisions.md):
    (a) Memory returns UPPERCASE roles; Strands expects lowercase.
        Convert at this boundary; ``load_recent_turns`` preserves service casing.
    (b) Only "user"/"assistant" are valid conversation roles for Bedrock Converse.
        Drop any message whose role is "TOOL", "OTHER", or anything else.
    (c) Bedrock Converse requires messages to alternate and start with "user".
        A dropped TOOL message, a truncated turn, or a k-boundary landing
        mid-turn can break alternation.  Normalise defensively; if the
        sequence cannot be normalised, drop it entirely and proceed statelessly.
    (d) load_recent_turns guarantees shape but NOT value types — role/text can
        be None or int.  Validate before string operations; a TypeError here
        would surface in invoke() outside the D4 boundary.
    """
    memory_id = getattr(config, "memory_id", None)
    if not memory_id or not actor_id:
        # Memory disabled or no authenticated identity — today's behaviour.
        return None

    # ── Everything below is guarded per D4: "Never raises" ──────────────────
    #
    # The conversion loop and normalisation are INSIDE this try, not just the
    # load call.  Rationale (spec D7): this helper is the shared seam a future
    # Tier 2 caller will use — its producer may not be load_recent_turns, and
    # the docstring's "Never raises" promise must hold for any caller.  Without
    # this guard, four container/element shapes escape to invoke() at the
    # hydration call-site (line :257), which has no guard of its own:
    #
    #   RAISES list of plain 2-tuples  → AttributeError: 'tuple' has no attr 'role'
    #   RAISES list of strings         → AttributeError: 'str' has no attr 'role'
    #   RAISES non-iterable return     → TypeError: 'int' is not iterable
    #   RAISES dict return             → AttributeError: 'str' has no attr 'role'
    #
    # MUTATION target: moving the conversion back outside the try causes a
    # parametrized test (malformed-input inputs) to escape as AttributeError/
    # TypeError — a test must FAIL.
    try:
        raw_turns: list[MemoryTurn] = load_recent_turns(
            client, memory_id, actor_id, session_id
        )

        if not raw_turns:
            return None

        # ── Role filtering and conversion ─────────────────────────────────
        # (a) Map UPPERCASE service roles to lowercase Strands roles.
        # (b) Drop non-conversational roles (TOOL, OTHER, unknown).
        # (d) Validate value types before string operations.
        _ROLE_MAP: dict[str, str] = {"USER": "user", "ASSISTANT": "assistant"}
        messages: list[dict] = []
        for turn in raw_turns:
            # (d) Validate type before any string operation.
            if not isinstance(turn.role, str) or not isinstance(turn.text, str):
                log.debug(
                    "hydrate_session_history: skipping turn with non-string role=%r text=%r",
                    type(turn.role).__name__,
                    type(turn.text).__name__,
                )
                continue
            strands_role = _ROLE_MAP.get(turn.role.upper())
            if strands_role is None:
                # (b) Drop TOOL, OTHER, or any unknown role.
                continue
            text = turn.text.strip()
            if not text:
                continue
            messages.append({"role": strands_role, "content": [{"text": text}]})

        if not messages:
            return None

        # ── Alternation normalisation (c) ─────────────────────────────────
        # Bedrock Converse requires: starts with "user", ends with "assistant",
        # strictly alternating.  See _normalise_alternating docstring for the
        # tail-role requirement.
        normalised = _normalise_alternating(messages)

        if not normalised:
            return None

        return normalised

    except Exception as exc:  # noqa: BLE001
        # Covers: load_recent_turns raising unexpectedly, AttributeError from
        # a malformed container, TypeError from non-iterable input, and any
        # other failure in the conversion/normalisation path.
        # All are logged at WARNING; the turn proceeds statelessly (D4).
        log.warning(
            "hydrate_session_history: failed to load or convert turns "
            "(session=%s %s) — proceeding statelessly",
            session_id,
            type(exc).__name__,
        )
        return None


def _normalise_alternating(messages: list[dict]) -> list[dict]:
    """Ensure *messages* alternates user/assistant, starts with user, ends with assistant.

    Strands appends the current ``prompt`` as a ``user`` message AFTER the
    hydrated history (``Agent._convert_prompt_to_messages`` → ``_append_messages``).
    Bedrock Converse therefore requires the history to END with ``assistant`` (or
    be empty) so that the combined list alternates cleanly.  Failing to enforce
    this produces ``[…, user, user]``, which raises ``ValidationException`` inside
    ``agent(prompt)`` and fails the turn (violating D4) — sticky for the rest of
    the session because a failed turn never reaches ``record_turn``.

    Algorithm (in order):
    1. Drop messages whose text content is empty or whitespace-only — Bedrock
       rejects empty content blocks.
    2. Drop messages with non-conversational roles (anything not "user"/"assistant").
       (Already done upstream in hydrate_session_history, but defensive here too.)
    3. Collapse consecutive same-role runs: keep the last in each run (most recent
       within the duplicated block).
    4. Drop leading ``assistant`` messages until a ``user`` is found.
    5. Drop trailing ``user`` messages until an ``assistant`` is found.
    6. If the invariant (empty, OR starts-with-user AND ends-with-assistant AND
       strictly alternating) does not hold, return ``[]``.

    Returns an empty list if no valid alternating sequence can be formed.
    hydrate_session_history treats an empty return as "drop history and proceed
    statelessly" — one utterance of context is lost but the turn does not fail.

    WHICH STEP GUARANTEES WHAT — read this before deleting step 5 as redundant.
    **Step 6 is what guarantees the invariant**, unconditionally: it re-reads the
    list and returns ``[]`` if the result does not start with ``user``, end with
    ``assistant``, and alternate. So safety does NOT depend on step 5.

    **Step 5 provides salvage, not safety.** It trims a history ending in ``user``
    down to its usable prefix instead of letting step 6 discard the whole thing.
    Measured: without step 5, **662 of 5,461** brute-forced role sequences (12.1%)
    and 4 of the 8 reviewer probe shapes lose usable history, and the total count of
    hydratable messages falls ~27%. The assistant would be amnesiac more often than
    necessary — quietly, with the suite green.

    So removing step 5 does **not** break the invariant and does **not** fail any
    invariant test. It fails ``TestNormaliseSalvagesRatherThanDiscards``, which
    exists precisely because step 5 looks like dead code next to step 6. An earlier
    revision of this docstring claimed the opposite (that removing step 5 produced
    ``[…, user, user]`` and failed an invariant test); that was verified FALSE and is
    corrected here.
    """
    if not messages:
        return []

    # Step 1: drop messages with empty/whitespace-only text.
    filtered: list[dict] = []
    for msg in messages:
        text = ""
        content = msg.get("content")
        if isinstance(content, list) and content:
            first = content[0]
            if isinstance(first, dict):
                text = first.get("text") or ""
        if isinstance(text, str) and text.strip():
            filtered.append(msg)

    if not filtered:
        return []

    # Step 2+3: keep only user/assistant; collapse consecutive same-role runs.
    _VALID = {"user", "assistant"}
    collapsed: list[dict] = []
    for msg in filtered:
        role = msg.get("role")
        if role not in _VALID:
            continue
        if collapsed and msg["role"] == collapsed[-1]["role"]:
            collapsed[-1] = msg  # replace with more recent in the run
        else:
            collapsed.append(msg)

    if not collapsed:
        return []

    # Step 4: drop leading "assistant" until a "user" is found.
    start = 0
    while start < len(collapsed) and collapsed[start]["role"] != "user":
        start += 1
    collapsed = collapsed[start:]

    if not collapsed:
        return []

    # Step 5: drop trailing "user" until an "assistant" is found.
    # A history ending in "user" would combine with the current prompt to produce
    # "[…, user, user]" which Bedrock rejects.
    end = len(collapsed)
    while end > 0 and collapsed[end - 1]["role"] != "assistant":
        end -= 1
    collapsed = collapsed[:end]

    if not collapsed:
        return []

    # Step 6: verify the invariant.  After the above steps this should always
    # hold, but defend unconditionally per D4.
    if collapsed[0]["role"] != "user" or collapsed[-1]["role"] != "assistant":
        return []
    for i in range(len(collapsed) - 1):
        if collapsed[i]["role"] == collapsed[i + 1]["role"]:
            return []

    return collapsed


def _lead_with_p0_line(result_text: str, ctx) -> str:
    """``result_text`` led by the P0 directive the model was given, if it lacks it.

    Applies only when this turn's triage was P0 and the ``triage`` tool returned
    ``sayFirst`` (recorded on ``ctx.p0_say_first_given``). Whitespace is ignored
    when checking whether the reply already carries the line. Never raises.
    """
    try:
        line = getattr(ctx, "p0_say_first_given", None)
        if getattr(ctx, "triage_classification", None) != "P0" or not isinstance(line, str) or not line.strip():
            return result_text
        if not is_p0_directive(line):
            # Only catalog sentences may be added after the guardrail (security
            # Cycle 9 S1). Anything else is a bug; the driver still gets a line.
            log.warning("invoke: sayFirst is not a catalog directive; using the fallback")
            line = p0_directive([], getattr(ctx, "driver_first_name", None))
        if " ".join(line.split()) in " ".join((result_text or "").split()):
            return result_text
        return f"{line}\n\n{result_text}" if result_text else line
    except Exception:  # noqa: BLE001
        log.exception("invoke: could not add the P0 line")
        return result_text


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
