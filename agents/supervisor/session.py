"""SessionContext — tenantId, sessionId, JWT, and VIN threaded through tools.

Why this exists: tool functions are called by Strands with args from the
LLM. We deliberately don't expose tenantId, JWT, or sessionId as LLM-
controllable arguments — the LLM should not be able to forge those.
Instead they live in a context object bound via closure when the tool is
registered.

Usage pattern (see supervisor.py):

    ctx = SessionContext(persona_id=..., tenant_id=..., session_id=..., jwt=..., vin=...)
    triage_tool = make_triage_tool(ctx, config)
    retrieve_tool = make_retrieve_tool(ctx, config)
    book_tool = make_book_tool(ctx, config)
    agent = Agent(tools=[triage_tool, retrieve_tool, book_tool], ...)

Each tool factory closes over the context and returns a fresh @tool-
decorated function whose signature is exactly what the LLM sees.

Note: `persona_id` is REQUIRED (no default). This surfaces caller bugs
early per spec § "Module layout" row for session.py.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass
class SessionContext:
    """Per-session state threaded through tool calls.

    Fields:
        tenant_id: Slug from tenant config (e.g. "michelin"). Used for
            auth and for correlating messages.
        session_id: Stable ID for this supervisor session. Generated once
            at session start and passed into every tool call so downstream
            artifacts can be correlated.
        jwt: Cognito IdToken for the tenant user. Used by the triage tool
            to authenticate against POST /triage. In AgentCore deployment
            this will come from the session auth context; locally it's
            cached from Cognito USER_PASSWORD_AUTH.
        vin: The VIN under discussion. May be None at session start and
            populated once the supervisor resolves it from the caller.
        vehicle_id: The CMS-side vehicle primary key (e.g. "VEH-0025") for
            the same vehicle as ``vin``. Optional side-channel to avoid a
            runtime VIN→vehicleId lookup when the caller already knows it
            (the iOS client reads it from ``VSAConfig.demoVehicleId`` and
            passes it at session start). Used by ``book()`` as the HASH
            key for ``cms-prod-storage-service-history`` writes.
        driver_id: The CMS-side driver primary key (e.g. "DRV-0055") for
            the human operating this vehicle. Same rationale as
            ``vehicle_id`` — provided by the iOS client so ``book()``
            doesn't need a Cognito-email → CMS-driver lookup. Multi-
            driver / real-auth binding is a v2 feature.
        triage_decision_id: Most recent triage classification's ID.
            Populated by the triage tool after a successful call. Used
            by the book tool to correlate the booking to its triage.
        triage_classification: The P0/P1/P2/P3 result from the most
            recent triage call. Used by the book tool to set priority.
        last_offered_service_center: The service center most recently
            returned by find_service_center and offered to the driver
            (the first/closest entry of the centers list). Populated
            by bidi_app.py when find_service_center's tool_result is
            processed. Used by book() to fill serviceCenter in the
            STAR payload with the actual offered center instead of a
            hardcoded fixture default. Cleared after a successful
            book() so a stale center can't leak into a later booking.
        client_latitude / client_longitude: Optional fresh coordinates
            pushed by the iOS client at WebSocket connect time, sourced
            from the same Redis-backed live-state endpoint the Vehicle
            tab map uses. When present we skip the backend's
            `/vehicles/{id}/live-state` HTTP call during session setup
            (saves ~150-300ms before Nova starts listening). When
            absent — older clients, local backend tests, or clients
            that haven't loaded liveState yet — the backend falls back
            to its own live-state lookup. Either way, find_service_center
            ends up with the same fresh coordinates the driver sees on
            their map.

    The last four fields are mutable so tools can update them mid-session.
    """

    tenant_id: str
    session_id: str
    jwt: str
    persona_id: str
    vin: str | None = None
    vehicle_id: str | None = None
    driver_id: str | None = None
    customer_id: str | None = None
    tenant_display: str = ""
    triage_decision_id: str | None = field(default=None)
    triage_classification: str | None = field(default=None)
    # Most-recent retrieve() tool result. Each retrieve() call replaces this
    # list (last call wins); local-fallback hits are included with their
    # relative path as `source` (no `s3://` prefix). Read by `agentcore_app.py`
    # after `agent(prompt)` returns and surfaced as `citations` in the runtime
    # response so the route Lambda can pass them to the browser. Without this,
    # KB-grounding is invisible from the response payload — answers are
    # indistinguishable from model knowledge (PO UAT 2026-06-22 finding #1).
    last_kb_citations: list[dict] | None = field(default=None)
    last_offered_service_center: dict | None = field(default=None)
    vehicle_make: str | None = field(default=None)
    distance_unit: str | None = field(default=None)
    client_latitude: float | None = field(default=None)
    client_longitude: float | None = field(default=None)
    # Tenant persona — drives which kind of service center
    # find_service_center returns. "fleet" (default) returns a mix of
    # dealers / independents / fleet-service / quick-service. "oem"
    # restricts results to brand-matching dealerships only (OEM
    # branded support apps want their drivers steered to the
    # dealer network). "rental" prefers national chains. Resolved
    # at session setup from vsa-prod-tenant-config; falls back to
    # "fleet" on any failure since that's the broadest behavior.
    tenant_segment: str = "fleet"
    # Anonymous-kiosk gate (spec 2026-07-31-zone2-order-backend-and-kiosk D3).
    # True when the Cognito group claim is "kiosk" — i.e. the session belongs
    # to a conference kiosk device, NOT a signed-in fleet driver or consumer.
    # Controls:
    #   1. build_supervisor: restricts agent to the 10-tool kiosk allow-list.
    #   2. session_manager_or_none: returns None (no LTM contact for anonymous
    #      visitors; prevents cross-visitor memory bleed, spec Risk R2).
    # Defaults to False so existing signed-in sessions are unaffected.
    is_kiosk: bool = False

    @classmethod
    def new(
        cls,
        tenant_id: str,
        jwt: str,
        persona_id: str = "fleet_driver",
        vin: str | None = None,
        vehicle_id: str | None = None,
        driver_id: str | None = None,
        customer_id: str | None = None,
        tenant_display: str = "",
    ) -> "SessionContext":
        """Create a new session with a generated sessionId.

        `persona_id` defaults to 'fleet_driver' so existing call sites that
        don't supply it remain on the same persona they targeted before this
        initiative. New call sites should always pass `persona_id` explicitly.
        `tenant_segment` is resolved from PERSONA_TENANT_DEFAULTS if not
        otherwise provided.
        """
        from agents.supervisor.persona_definitions import PERSONA_TENANT_DEFAULTS  # avoid circular at module level
        return cls(
            tenant_id=tenant_id,
            session_id=f"vsa-sess-{uuid.uuid4().hex[:12]}",
            jwt=jwt,
            persona_id=persona_id,
            vin=vin,
            vehicle_id=vehicle_id,
            driver_id=driver_id,
            customer_id=customer_id,
            tenant_display=tenant_display,
            tenant_segment=PERSONA_TENANT_DEFAULTS.get(persona_id, "fleet"),
        )


def utc_now_iso() -> str:
    """UTC ISO 8601 with milliseconds — matches API response format."""
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def session_manager_or_none(ctx: "SessionContext", config: object):
    """Return an AgentCoreMemorySessionManager iff the caller is signed in.

    Anonymous Discover sessions MUST NOT touch AgentCore Memory LTM (spec
    Risk R2: anonymous session leaks into LTM). Call this helper wherever
    a tool or agent turn would otherwise unconditionally wire LTM hydration.

    Signed-in detection: ``ctx.jwt`` is non-empty.  A missing, empty, or
    whitespace-only JWT means the session is anonymous and this function
    returns ``None`` without importing or touching the AgentCore Memory SDK.

    When signed in, the actor identity is resolved as follows:
        1. ``ctx.customer_id`` — preferred; stable across sessions for a
           known owner.
        2. ``ctx.session_id``  — fallback for sessions where customer_id
           is not yet resolved (e.g. mid-Discover before lead_capture runs).
    This matches the pattern in ``persona_loader.py`` where the actor
    namespace is built from ``(tenant_id, persona_id)`` — here we scope it
    to the individual caller identity.

    Args:
        ctx:    The current SessionContext.
        config: Any config object that exposes ``memory_id: str`` and an
                optional ``memory_region: str`` (defaults to ``"us-west-2"``
                if absent). Accepts the production ``Config`` dataclass or
                a test ``SimpleNamespace``/``MagicMock``.

    Returns:
        An ``AgentCoreMemorySessionManager`` context manager if signed in,
        or ``None`` for anonymous sessions.

    Usage (tool factory)::

        sm = session_manager_or_none(ctx, config)
        if sm is not None:
            with sm:
                result = agent(prompt, session_manager=sm)
        else:
            result = agent(prompt)

    Note: The caller is responsible for using the returned object as a
    context manager (``with sm:``) to ensure Memory flush on exit.
    """
    # Kiosk guard — anonymous-visitor sessions MUST NOT touch AgentCore Memory
    # LTM even though a kiosk device has a valid JWT (spec D2 + decisions.md Q10).
    #
    # A kiosk device IS authenticated (it has its own Cognito sub per spec D2),
    # so JWT-presence alone is NOT a sufficient discriminator here.  The
    # ``is_kiosk`` flag, set by ``build_supervisor`` (and the handler) when the
    # Cognito group claim is ``kiosk``, is the correct gate.
    #
    # Hazard if this check is absent: Groups 2+4 deployed without Group 3
    # would attach real long-term memory to kiosk sessions, blending one
    # visitor's context into the next.  The ``make preflight`` target in the
    # Makefile is the mechanical gate for this ordering requirement (decisions.md Q10).
    is_kiosk: bool = bool(getattr(ctx, "is_kiosk", False))
    if is_kiosk:
        return None

    # Anonymous guard — no LTM contact for unsigned sessions (spec R2).
    jwt: str = getattr(ctx, "jwt", "") or ""
    if not jwt.strip():
        return None

    memory_id: str = getattr(config, "memory_id", "") or ""
    if not memory_id:
        # Memory not configured for this deployment — safe degradation.
        return None

    memory_region: str = getattr(config, "memory_region", "us-west-2") or "us-west-2"

    # Prefer stable customer_id; fall back to session_id so the memory
    # namespace at least correlates this session's turns together.
    actor_id: str = (
        getattr(ctx, "customer_id", None)
        or getattr(ctx, "session_id", "")
        or ctx.session_id
    )
    if not actor_id:
        # Cannot build a valid actor_id — degrade gracefully.
        return None

    from bedrock_agentcore.memory.integrations.strands.config import AgentCoreMemoryConfig  # noqa: PLC0415
    from bedrock_agentcore.memory.integrations.strands.session_manager import AgentCoreMemorySessionManager  # noqa: PLC0415

    memory_config = AgentCoreMemoryConfig(
        memory_id=memory_id,
        session_id=ctx.session_id,
        actor_id=actor_id,
    )
    return AgentCoreMemorySessionManager(memory_config, region_name=memory_region)
