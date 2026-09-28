"""POST /assistant/chat

Proxies a user chat prompt to the CVX AgentCore text runtime (SigV4,
same-account/same-region) and returns the structured agent response.

Request (from browser, after API-GW Cognito authorizer validates):
    {
        "prompt":    "<user message>",   # required
        "sessionId": "<session uuid>",   # required
        "vin":       "<VIN>"             # optional
        "vehicleId": "<CMS vehicle id>"  # optional
        "userText":  "<raw visitor utterance>"  # optional
    }

A caller whose token carries `custom:vehicleId` (a driver bound to one vehicle):
its VIN is read from the CMS vehicles table, a body `vin` or `vehicleId` that
does not match is dropped, and the payload carries `vehicleBound: true` so the
runtime's `triage` tool classifies that VIN whatever VIN the model passes. See
`vehicle_scope.py` and spec 2026-09-23-cvx-escalation-recommend-not-act Fix
Group 18.

`userText` is the visitor's own words with no client-side context scaffolding
around them. When present it is forwarded to the runtime, which guards THAT
span rather than the assembled `prompt` — see
issues/2026-08-18-kiosk-guardrail-blocks-own-prompt-scaffolding/. Clients that
do not wrap their prompts have no reason to send it.

Auth context injected by API-GW Cognito authorizer (not caller-settable):
    requestContext.authorizer.claims:
        sub              — Cognito user sub
        custom:tenantId  — tenant slug (e.g. "meridian-fleet")
        cognito:groups   — comma-separated group list
        custom:role      — explicit role claim

Response:
    {
        "result":         "<assistant text>",
        "sessionId":      "<session uuid>",
        "classification": "P0" | "P1" | "P2" | "P3" | null,
                             # NOTE: carries NO `source` field. This runtime does
                             # NOT speak the bidi `classification` source protocol
                             # (bidi_app.py CLASSIFICATION_SOURCES, where an absent
                             # source means "unknown shape — fail closed"). Here
                             # absence is normal. Any client that renders a
                             # verdict-dependent affordance (e.g. the escalation-
                             # recommendation card) from THIS field must first get
                             # `source` added server-side; reusing the iOS bidi
                             # rule would read every verdict as unsubstantiated,
                             # and inverting it would render verdicts it cannot
                             # vouch for. Security review Cycle 1 W6, spec
                             # 2026-09-23-cvx-escalation-recommend-not-act T1.5c.
        "vin":            "<VIN>" | null,
        "citations":      [...]  # optional — when runtime returns them
    }

Error (fail-closed — never a raw 5xx with no body):
    {
        "error":  "<short reason>",
        "detail": "<optional context>"
    }
    HTTP 400 for caller errors, 500 for upstream failures.

Security:
    - The caller's CMS Cognito IdToken is forwarded to the runtime in the
      X-Amzn-Bedrock-AgentCore-Runtime-Custom-User-Token header only.
    - It is NEVER logged (R7 mitigation).
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

import boto3
from botocore.config import Config

import vehicle_scope
from persona_map import caller_has_fleet_group, infer_persona, is_kiosk_claim
from session_cap import check_and_increment, SESSION_LIMIT_RESPONSE

log = logging.getLogger()
log.setLevel(logging.INFO)

# Visitor-facing fallbacks. These exist so no code path can ever hand raw
# internal structure (a Python dict repr, a stack trace, a runtime envelope)
# to a public kiosk tablet. See
# issues/2026-08-02-kiosk-blocked-envelope-leaks-python-repr/.
GUARDRAIL_FALLBACK_MESSAGE = (
    "I'm not able to help with that request at this kiosk. "
    "Please ask me about Meridian vehicles, features, or the order process."
)
GENERIC_FALLBACK_MESSAGE = (
    "Sorry — I didn't catch that. Could you ask me again about the vehicles, "
    "features, or the order process?"
)

# NOTE: The old module-level `_CORS_HEADERS` constant was removed 2026-09-16 in
# favor of building the headers dict inside `_respond()`. The env-var read is
# now per-response, not per-module-load — see `_respond()` docstring for why.

# Personas a client may explicitly request via `body.personaId`.
#
# Persona selection is normally derived SERVER-SIDE from Cognito claims
# (`infer_persona`) precisely so a caller cannot hand-pick a privileged tool
# surface. The Discover / Acquire shopping flow (spec
# `2026-08-04-cvx-oem-discover-order-meridian`) is the single exception:
#
#   * It is a public-facing, catalog-browsing persona whose tool surface is
#     strictly non-privileged — catalog reads plus a consent-gated lead
#     write. It exposes no fleet, vehicle-control, or service-order surface.
#   * It is not derivable from claims: a shopper is frequently anonymous, and
#     a signed-in shopper carries no claim that distinguishes "I am shopping"
#     from "I am driving".
#
# Any value NOT in this allowlist is ignored and the claim-derived persona
# wins. Do not widen this set without a security review — adding a persona
# here lets any authenticated caller assume that persona's tool surface.
_CLIENT_REQUESTABLE_PERSONAS = frozenset({"consumer"})

# Personas a client may request, but only when the caller's Cognito groups
# include a fleet-authorized group (platform-admin, fleet-operator, or
# fleet-viewer). This is NOT in _CLIENT_REQUESTABLE_PERSONAS — the gate is
# enforced here on the CVX side because the CMS fleet endpoints do NOT enforce
# per-group authorization (Group 1 research: main_api/index.py routes
# 7348/7362/7600/7794 return HTTP 200 to any authenticated token without
# consulting is_admin/is_viewer/has_unscoped_access). An ungrouped caller who
# requests a persona from this set is silently ignored and falls back to their
# claim-derived persona — no fleet tools, no error.
_GROUP_GATED_REQUESTABLE_PERSONAS = frozenset({"fleet_manager"})

# Lazy boto3 client — reset to None in tests via monkeypatch
_agentcore_client = None

# Lazy DynamoDB resource for the claim-vehicle VIN lookup. Short timeouts and
# two attempts in total (at most ~4 s): the lookup sits in front of a model turn
# behind API Gateway's 29 s limit, and a failed lookup only means no VIN is
# sent. `total_max_attempts` counts the first try; `max_attempts` does not.
_ddb_resource = None
_DDB_CONFIG = Config(connect_timeout=1, read_timeout=1, retries={"total_max_attempts": 2})


def _get_ddb():
    global _ddb_resource
    if _ddb_resource is None:
        _ddb_resource = boto3.resource(
            "dynamodb",
            region_name=os.environ.get("AWS_REGION", "us-west-2"),
            config=_DDB_CONFIG,
        )
    return _ddb_resource


def _lookup_vin(vehicle_id: str) -> str | None:
    """Return the VIN the CMS vehicles table holds for `vehicle_id`, or None.

    Raises when the table is not configured or the read fails; the caller
    (`vehicle_scope.resolve`) treats that as "VIN unknown".
    """
    table_name = os.environ.get("CMS_VEHICLES_TABLE", "")
    if not table_name:
        raise RuntimeError("CMS_VEHICLES_TABLE not configured")
    item = (
        _get_ddb()
        .Table(table_name)
        .get_item(Key={"vehicleId": vehicle_id}, ProjectionExpression="vin")
        .get("Item")
        or {}
    )
    vin = str(item.get("vin") or "").strip()
    return vin or None


def _get_agentcore_client():
    global _agentcore_client
    if _agentcore_client is None:
        _agentcore_client = boto3.client(
            "bedrock-agentcore",
            region_name=os.environ.get("CVX_RUNTIME_REGION", "us-west-2"),
        )
    return _agentcore_client


def _respond(status: int, body: dict) -> dict[str, Any]:
    # Build headers at RESPONSE TIME (not module-load time) so the env-var-driven
    # `Access-Control-Allow-Origin` value reflects what the CDK stack set when
    # the Lambda cold-started, AND so tests that monkeypatch `ALLOWED_ORIGIN`
    # after importing this module (the standard pytest pattern) see their value
    # rather than the module-import-time snapshot. See
    # `issues/2026-09-16-cvx-cors-scoping/` for the design.
    return {
        "statusCode": status,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": os.environ.get("ALLOWED_ORIGIN", "*"),
            "Access-Control-Allow-Headers": "Content-Type,Authorization",
        },
        "body": json.dumps(body),
    }


def handler(event: dict[str, Any], _context: Any) -> dict[str, Any]:
    """Lambda entry point for POST /assistant/chat."""
    # Env vars read at call time so monkeypatch.setenv works in tests
    runtime_arn = os.environ.get("CVX_RUNTIME_ARN", "")
    # Default is `vehicle-owner`, not a company-named tenant (2026-08-21).
    #
    # This fallback fires for any JWT without a `custom:tenantId` claim — currently 8
    # accounts in the staging pool. It previously defaulted to `meridian-fleet`, so a
    # claimless caller was served a tenant named after a real company. `vehicle-owner`
    # is the de-branded oem-segment tenant ("Vehicle Owner Connect"), which is the right
    # generic destination for a caller whose tenant we cannot determine.
    #
    # NOTE this changes behaviour for claimless callers: they now get vehicle-owner's
    # branding, greeting and segment routing rather than the fleet tenant's. That is the
    # intended correction — falling back to a *specific customer's* tenant was never
    # right — but it is a behaviour change, not a cosmetic rename.
    default_tenant = os.environ.get("DEFAULT_TENANT_ID", "vehicle-owner")
    default_persona = os.environ.get("DEFAULT_PERSONA_ID", "fleet_driver")

    # --- 1. Extract claims from API-GW Cognito authorizer ---
    claims: dict = (
        (event.get("requestContext") or {})
        .get("authorizer", {})
        .get("claims") or {}
    )

    # --- 2. Resolve tenant ---
    tenant_id: str = (claims.get("custom:tenantId") or "").strip() or default_tenant

    # --- 3. Parse request body ---
    try:
        body: dict = json.loads(event.get("body") or "{}")
    except json.JSONDecodeError:
        return _respond(400, {"error": "invalid JSON body"})

    prompt: str = (body.get("prompt") or "").strip()
    if not prompt:
        return _respond(400, {"error": "prompt is required"})

    # The visitor's own utterance, unwrapped — the span the kiosk guardrail's
    # PROMPT_ATTACK filter should judge.
    #
    # Optional and additive. `prompt` remains what the model receives, and the
    # Discover client legitimately wraps it in context scaffolding (a
    # vehicle-facts block, a fenced replay of prior turns). That scaffolding is
    # instruction-shaped, so guarding the assembled prompt made PROMPT_ATTACK
    # fire on our own template text and blocked every Discover turn carrying
    # vehicle context. See
    # issues/2026-08-18-kiosk-guardrail-blocks-own-prompt-scaffolding/.
    #
    # `str(...)` before `.strip()` for the same reason as `personaId` below
    # (security review 2026-07-29 S2): a non-string value must fail into the
    # fallback, not raise an unhandled 5xx. Absent or empty → omitted from the
    # runtime payload, and the runtime falls back to guarding the whole prompt,
    # which is the pre-fix behaviour. Over-blocking, never unguarded.
    _user_text_raw = body.get("userText")
    user_text: str = (
        "" if _user_text_raw is None else str(_user_text_raw).strip()
    )

    session_id: str = (body.get("sessionId") or "").strip()
    if not session_id:
        return _respond(400, {"error": "sessionId is required"})

    # Client-sent vehicle. Used only for callers WITHOUT a `custom:vehicleId`
    # claim; a vehicle-bound caller's vehicle comes from the claim (step 5b).
    # `vehicleId` is the CMS vehicle key: `/escalate` requires BOTH vin and
    # vehicleId, so without forwarding it a human handoff from the text surface
    # failed validation before it ever reached Amazon Connect.
    body_vin = body.get("vin")
    body_vehicle_id = body.get("vehicleId")
    # Human-facing brand name from the client's tenant config. Cosmetic only —
    # it never affects authorisation — but without it the agent's prompt has an
    # empty brand slot and the model infers one from the tenant slug.
    tenant_display: str = (body.get("tenantDisplayName") or "").strip()[:120]
    # "km" | "mi". Presentation only.
    distance_unit: str = (body.get("distanceUnit") or "").strip().lower()[:8]

    # --- 4. Resolve persona: claim-derived by default, narrow client override ---
    #
    # The claim-derived persona is always computed. A client-supplied
    # `personaId` is honored ONLY when it appears in
    # `_CLIENT_REQUESTABLE_PERSONAS` (today: `consumer`, for the Discover /
    # Acquire shopping flow, which has no claim-derivable signal). Any other
    # requested value is ignored — it does NOT error, so a stale or hostile
    # client simply gets its claim-derived persona rather than a 4xx.
    # Client-supplied because the client already fetched tenant config; this
    # endpoint has no reason to re-read the tenant table on every turn.
    # Allowlisted values only — an unknown value falls back to "fleet",
    # which is the broadest and least surprising behaviour.
    _seg_raw = body.get("tenantSegment")
    _seg = "" if _seg_raw is None else str(_seg_raw).strip().lower()
    tenant_segment: str = _seg if _seg in {"fleet", "oem", "rental"} else "fleet"

    claim_persona: str = infer_persona(claims)
    # `str(...)` coercion before `.strip()`: security review 2026-07-29 S2 —
    # a non-string `personaId` (list, dict, bool, int) raised AttributeError
    # here and surfaced as an unhandled 5xx, which is a trivial availability
    # nuisance for any authenticated caller. Coercing makes a malformed value
    # simply fail the allowlist check and fall through to the claim-derived
    # persona, which is the correct outcome anyway.
    _requested_raw = body.get("personaId")
    requested_persona: str = (
        "" if _requested_raw is None else str(_requested_raw).strip()
    )
    persona_override_applied: bool
    if bool(requested_persona) and requested_persona in _CLIENT_REQUESTABLE_PERSONAS:
        # Unconditionally requestable — no group check required.
        persona_override_applied = True
    elif bool(requested_persona) and requested_persona in _GROUP_GATED_REQUESTABLE_PERSONAS:
        # Honored only for fleet-grouped callers. Kiosk/consumer callers carry
        # no fleet group, so they fall back to their claim-derived persona here.
        persona_override_applied = caller_has_fleet_group(claims)
    else:
        persona_override_applied = False
    persona_id: str = requested_persona if persona_override_applied else claim_persona

    # --- 5. Retrieve the caller's IdToken (NEVER log it) ---
    auth_header: str = (event.get("headers") or {}).get("Authorization") or ""
    if auth_header.lower().startswith("bearer "):
        id_token = auth_header[7:].strip()
    else:
        id_token = auth_header.strip()

    if not runtime_arn:
        return _respond(500, {"error": "CVX_RUNTIME_ARN not configured"})

    # --- 5a. Per-session turn cap ---
    #
    # UX guard against a single stuck or adversarial session. This is NOT the
    # adversarial cost control — a client can bypass it by rotating the session
    # id (decisions.md Q9). The real spend backstop is an API Gateway usage
    # plan plus an AWS Budgets action (Group 4).
    #
    # Fail-open: if the counter store errors, the request is allowed through
    # and the error is logged loudly. A broken counter must not take the booth
    # down mid-show.
    #
    # Default ceiling: 25 turns (VSA_SESSION_TURN_LIMIT env var).
    # Counter TTL: 4 hours (VSA_SESSION_TTL_SECONDS env var) — prevents a
    # counter from the morning setup run from blocking afternoon visitors.
    _cap_allowed, _cap_turns = check_and_increment(session_id)
    if not _cap_allowed:
        log.info(
            "chat: session turn cap reached session=%s turns=%d",
            session_id,
            _cap_turns,
        )
        return _respond(200, {
            "result": SESSION_LIMIT_RESPONSE,
            "sessionId": session_id,
        })

    # --- 5b. Vehicle for this turn ---
    #
    # A caller with a `custom:vehicleId` claim is pinned to that vehicle; its
    # VIN is read server-side and client-sent values that don't match are
    # dropped. Other callers keep the body values. See vehicle_scope.py.
    scope = vehicle_scope.resolve(claims, body_vin, body_vehicle_id, _lookup_vin)
    vin: str | None = scope.vin
    vehicle_id: str | None = scope.vehicle_id

    # --- 6. Build runtime payload ---
    #
    # The CMS IdToken reaches the runtime via TWO independent paths:
    #
    # (a) `payload.jwt` — primary path for HTTP `InvokeAgentRuntime`. The
    #     AgentCore HTTP unary API does NOT forward arbitrary custom request
    #     headers (the `X-Amzn-Bedrock-AgentCore-Runtime-Custom-*` namespace
    #     is documented for WebSocket runtimes only — see
    #     issues/2026-06-22-assistant-chat-no-text-runtime-container/).
    #     Putting the token in the payload is what `agentcore_app.py`'s
    #     `_extract_bearer` actually accepts on this path.
    #
    # (b) `X-Amzn-Bedrock-AgentCore-Runtime-Custom-User-Token` header via
    #     the `before-sign` event-hook below — defense-in-depth. If
    #     AgentCore ever begins forwarding custom headers on HTTP, the
    #     entrypoint will read it from there (it tries header first per
    #     `_extract_bearer`); today it's silently dropped.
    #
    # The IdToken is NEVER logged (R7 — handler.py log statement above
    # surfaces only sub/tenant/persona/session/vin/prompt_len).
    runtime_payload: dict = {
        "prompt": prompt,
        "tenantId": tenant_id,
        "personaId": persona_id,
        # Forward the tenant segment so the runtime does not default to
        # "fleet". Without it `find_service_center` skips the OEM branch
        # entirely — it filters on `type == "dealer"` AND brand only when
        # segment == "oem", so an OEM customer was being shown arbitrary
        # fleet service centres instead of authorised dealers.
        "tenantSegment": tenant_segment,
    }
    # Signal kiosk-mode to the runtime so it can set is_kiosk=True on the
    # SessionContext and activate the ten-tool allow-list (spec D3 Critical fix).
    # Only set when the kiosk Cognito GROUP was the reason for the consumer
    # persona — the _ANONYMOUS_PERSONA fallback for claimless input is also
    # "consumer" but is NOT a provisioned kiosk device.
    if is_kiosk_claim(claims):
        runtime_payload["isKiosk"] = True
    if user_text:
        runtime_payload["userText"] = user_text
    if vin:
        runtime_payload["vin"] = vin
    if vehicle_id:
        runtime_payload["vehicleId"] = vehicle_id
    if scope.bound:
        # Tells the runtime to keep `triage` on this VIN (session.vin_pinned).
        runtime_payload["vehicleBound"] = True
    if tenant_display:
        runtime_payload["tenantDisplayName"] = tenant_display
    if distance_unit in ("km", "mi"):
        runtime_payload["distanceUnit"] = distance_unit
    if id_token:
        runtime_payload["jwt"] = id_token  # primary path for HTTP runtimes

    # Log correlation context — token is intentionally NOT logged (R7)
    log.info(
        "chat: sub=%s tenant=%s persona=%s persona_src=%s session=%s vin=%s "
        "vehicle_src=%s body_vehicle_dropped=%s prompt_len=%d user_text_len=%d",
        claims.get("sub", ""),
        tenant_id,
        persona_id,
        "client-request" if persona_override_applied else "claims",
        session_id,
        vin,
        scope.source,
        scope.body_dropped,
        len(prompt),
        len(user_text),
    )

    # --- 7. Invoke the AgentCore runtime (SigV4, same-account) ---
    #
    # AgentCore custom headers must be injected via botocore's event system
    # before SigV4 signing — `invoke_agent_runtime` does NOT accept a
    # headers kwarg. Pattern mirrors scripts/smoke-test-agentcore.py:85-96.
    client = _get_agentcore_client()
    event_system = client.meta.events
    event_name = "before-sign.bedrock-agentcore.InvokeAgentRuntime"
    hook = None

    if id_token:
        # Closure captures id_token only for the duration of this request.
        # The token value itself is never logged (R7).
        def _inject_user_token_header(request, **_kwargs):
            request.headers.add_header(
                "X-Amzn-Bedrock-AgentCore-Runtime-Custom-User-Token",
                id_token,
            )

        hook = _inject_user_token_header
        event_system.register_first(event_name, hook)

    # AgentCore's `InvokeAgentRuntime` requires `runtimeSessionId` length >=33
    # and pattern [a-zA-Z0-9][a-zA-Z0-9-_]*. CMS UI's ChatAgent.tsx generates
    # 31-char IDs (`session_<ms>_<9-rand>`); CLI smoke tests use shorter ones.
    # Pad deterministically so (a) the user-facing session_id stays whatever
    # the caller chose (returned in the response body unchanged), and (b) the
    # same user session always maps to the same upstream runtimeSessionId for
    # AgentCore Memory continuity.
    # See issues/2026-06-22-assistant-chat-runtime-session-id-too-short/.
    _MIN_RUNTIME_SESSION_LEN = 33
    runtime_session_id = session_id
    if len(runtime_session_id) < _MIN_RUNTIME_SESSION_LEN:
        pad_len = _MIN_RUNTIME_SESSION_LEN - len(runtime_session_id)
        # Suffix is deterministic ('-' + 'x'*N) so the same user session_id
        # always hashes to the same runtime session, which is what AgentCore
        # Memory needs for continuity within a session window.
        runtime_session_id = f"{session_id}-{'x' * (pad_len - 1)}"

    try:
        resp = client.invoke_agent_runtime(
            agentRuntimeArn=runtime_arn,
            runtimeSessionId=runtime_session_id,
            payload=json.dumps(runtime_payload).encode(),
            qualifier="DEFAULT",
        )
    except Exception:  # noqa: BLE001
        # Server-side log captures the full traceback (incl. botocore
        # exception text). Do NOT leak the exception string to the caller:
        # botocore strings include the runtime ARN, account ID, AWS request
        # IDs, and parameter-shape detail useful for reconnaissance.
        log.exception("invoke_agent_runtime failed: session=%s", session_id)
        body: dict = {"error": "upstream invocation failed"}
        request_id = getattr(_context, "aws_request_id", None)
        if request_id:
            body["requestId"] = request_id
        return _respond(500, body)
    finally:
        # Always unregister, even on exception — prevents the closure (which
        # captures the IdToken) from outliving this request on the cached
        # module-level boto3 client.
        if hook is not None:
            event_system.unregister(event_name, hook)

    # --- 8. Parse runtime response ---
    try:
        raw = resp.get("response") or resp.get("body") or b""
        if hasattr(raw, "read"):
            raw = raw.read()
        if isinstance(raw, (bytes, bytearray)):
            raw = raw.decode("utf-8", errors="replace")
        agent_resp: dict = json.loads(raw) if raw else {}
    except Exception as exc:  # noqa: BLE001
        log.warning("Failed to parse runtime response: %s", exc)
        agent_resp = {}

    # A guardrail denial from the runtime is an envelope, NOT a result:
    #   {"error": "<visitor-safe message>", "blocked": True, "blocked_topics": [...]}
    # It carries no "result" key. The previous fallback (`str(agent_resp)`)
    # therefore serialised the whole dict with Python's repr and handed it to the
    # client as the assistant's reply, so a blocked kiosk visitor saw a literal
    #   {'error': "I'm not able to help...", 'blocked': True, 'blocked_topics': [...]}
    # rendered on the tablet. Observed live on staging 2026-08-02 during Group 5
    # negative-check 2; see issues/2026-08-02-kiosk-blocked-envelope-leaks-python-repr/.
    #
    # Surface the guardrail's own message, and flag `blocked` so a client can
    # style a refusal differently from a normal turn.
    if agent_resp.get("blocked"):
        result_text = agent_resp.get("error") or GUARDRAIL_FALLBACK_MESSAGE
        log.info(
            "chat: guardrail-blocked turn session=%s topics=%s filters=%s",
            session_id,
            agent_resp.get("blocked_topics"),
            agent_resp.get("blocked_filters"),
        )
    else:
        # Never `str(agent_resp)` — an unparseable or unexpected runtime payload
        # must not leak internal structure into visitor-visible text.
        result_text = agent_resp.get("result") or GENERIC_FALLBACK_MESSAGE
        if not agent_resp.get("result"):
            log.warning(
                "chat: runtime returned no 'result' key session=%s keys=%s",
                session_id,
                sorted(agent_resp.keys()),
            )

    response_body: dict = {
        "result": result_text,
        "sessionId": agent_resp.get("sessionId", session_id),
    }
    if agent_resp.get("blocked"):
        response_body["blocked"] = True
    if agent_resp.get("classification") is not None:
        response_body["classification"] = agent_resp["classification"]
    if agent_resp.get("vin") is not None:
        response_body["vin"] = agent_resp["vin"]
    if agent_resp.get("citations"):
        response_body["citations"] = agent_resp["citations"]
    # Human-handoff credentials, present only on the turn where the agent
    # escalated. Passed straight through so the client can join the Amazon
    # Connect chat; the token is short-lived and scoped to that one contact.
    if agent_resp.get("escalation"):
        esc = agent_resp["escalation"]
        response_body["escalation"] = esc
        # Log the contact id for support correlation, never the token.
        log.info(
            "chat: escalation contact=%s severity=%s",
            esc.get("contactId"), esc.get("severity"),
        )

    return _respond(200, response_body)
