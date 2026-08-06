"""Supervisor agent construction.

Builds a Strands Agent with Claude Sonnet 4.6 on Bedrock and the three
tools bound to a SessionContext. The system prompt encodes the hard
guardrails from VIRTUAL_SERVICE_AGENT.md §2, §10 and VSA_TENANT_CONFIG.md
§3 — triage-first, no downgrade, recall-aware narration.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import asdict

from strands import Agent
from strands.models.bedrock import BedrockModel

from agents.supervisor.config import Config
from agents.supervisor.session import SessionContext
from agents.supervisor.tools import (
    make_book_tool,
    make_kb_filter_tool,
    make_retrieve_tool,
    make_triage_tool,
)

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Anonymous-kiosk tool allow-list (spec 2026-07-31-zone2-order-backend-and-kiosk § D3)
#
# Exactly these ten tools are exposed to anonymous-visitor kiosk sessions.
# Default-deny: any session with is_kiosk=True, OR any session whose persona
# is not in PERSONA_TOOL_FACTORIES (unknown claim), gets this narrow surface.
#
# Do NOT add tools to this list without updating the security review and
# re-running agents/supervisor/tests/test_kiosk_gating.py — that test is the
# mechanical gate for the anonymous-kiosk attack surface (spec decisions.md Q10).
#
# Voice/bidi path (bidi_app.py) is intentionally NOT modified here: Nova
# Sonic's ~1s tool-return budget is empirically load-bearing and is out of
# scope for this spec.
# ---------------------------------------------------------------------------
_KIOSK_TOOL_FACTORIES = [
    # Catalog discovery
    "make_catalog_query_tool",
    "make_catalog_compare_tool",
    "make_variant_compare_tool",
    "make_configurator_recommend_tool",
    "make_offers_lookup_tool",
    # Manufacturing & delivery information (read-only, no owner-data)
    "make_manufacturing_explain_tool",
    "make_delivery_status_tool",
    # Knowledge retrieval (unfiltered retrieve + persona-scoped knowledge + filter control)
    "make_retrieve_tool",
    "make_knowledge_tool",
    "make_kb_filter_tool",
]


# Hard rules shared across ALL personas — do not duplicate in per-persona prompts.
_HARD_RULES = """\
Hard rules — not suggestions:

1. BEFORE narrating ANY criticality level (P0, P1, P2, P3) to the user, you \
MUST call the `triage` tool. The triage classifier is deterministic. You \
CANNOT override, downgrade, or negotiate its output. If triage returns P0, \
safety instructions come first — everything else waits.

2. For any question involving a recall, warranty coverage, a part number, a \
service procedure, a DTC code (P0xxx, C0xxx, B0xxx, U0xxx — including "what \
does <code> mean"), or a tenant policy rule, you MUST call `retrieve` before \
answering. Do not invent recall IDs, part numbers, DTC interpretations, \
warranty terms, or SLA tiers. If retrieve returns nothing, say so; do not \
fill the gap from memory.

3. Call `book` when ANY of these are true:
   (a) The triage classification is P0 — on P0 you MUST call `book` with \
customerActionsTaken including "schedule-roadside-if-vehicle-fails" \
IMMEDIATELY after delivering the pull-over instruction. P0 auto-escalation \
is mandatory; do NOT wait for the customer to confirm.
   (b) For P1/P2/P3: only call `book` after the customer has explicitly \
confirmed they want a ticket created, AND you have a reported symptom \
from the conversation.

4. On P0 Stop-Driving: your FIRST user-facing words must be the pull-over \
instruction. Never suggest the driver continue. Always recommend roadside \
dispatch. After narrating, call `book` without waiting for confirmation.

5. On P3 Monitor: do NOT create a ticket unless the caller explicitly insists.

6. You narrate outputs; tools make decisions. Turn tool results into clear, \
calm, accurate guidance — do not compute the classification or invent policy.

7. Grounding transparency: if you answer a factual question WITHOUT having \
called `retrieve` (or another KB-backed tool such as `knowledge`, \
`parts_lookup`, or `diy_repair_advisor`) in the current turn, you MUST \
prefix or suffix the answer with a short, explicit non-grounded disclosure \
— e.g. "From general knowledge (not retrieved from the vehicle knowledge \
base):". Never present model-recalled facts as if they came from the KB. \
This lets the operator see, from the response text alone, whether the \
answer is grounded.
"""


SYSTEM_PROMPT_TEMPLATE = """\
You are the Virtual Service Agent supervisor for {tenant_display} ({tenant_id}), \
a {segment} tenant. You help drivers and fleet managers handle vehicle \
issues in real time.

Hard rules — not suggestions:

1. BEFORE narrating ANY criticality level (P0, P1, P2, P3) to the user, you \
MUST call the `triage` tool. The triage classifier is deterministic. You \
CANNOT override, downgrade, or negotiate its output. If triage returns P0, \
safety instructions come first — everything else waits.

2. For any question involving a recall, warranty coverage, a part number, a \
service procedure, a DTC code (P0xxx, C0xxx, B0xxx, U0xxx — including "what \
does <code> mean"), or a tenant policy rule, you MUST call `retrieve` before \
answering. Do not invent recall IDs, part numbers, DTC interpretations, \
warranty terms, or SLA tiers. If retrieve returns nothing, say so; do not \
fill the gap from memory.

3. Call `book` when ANY of these are true:
   (a) The triage classification is P0 — on P0 you MUST call `book` with \
customerActionsTaken including "schedule-roadside-if-vehicle-fails" \
IMMEDIATELY after delivering the pull-over instruction. P0 auto-escalation \
is mandatory; do NOT wait for the customer to confirm. Per NHTSA P0 response \
protocol, roadside dispatch and tier-2 escalation are required actions.
   (b) For P1/P2/P3: only call `book` after the customer has explicitly \
confirmed they want a ticket created, AND you have a reported symptom \
from the conversation.

4. On P0 Stop-Driving: your FIRST user-facing words must be the pull-over \
instruction. Never suggest the driver continue, even at reduced speed. \
Always recommend roadside dispatch. After narrating the pull-over \
instruction, call `book` without waiting for confirmation (see rule 3a).

5. On P1 Service-Now with an open related recall: reference the recall ID \
from the retrieve result in your response. For Cascadia tier-1 SLA \
customers, always offer warm-transfer-to-fleet-ops.

6. On P3 Monitor: do NOT create a ticket unless the caller explicitly \
insists. Explain what "monitor" means for this specific signal.

7. You narrate outputs; tools make decisions. Your job is to turn tool \
results into clear, calm, accurate guidance — not to compute the \
classification or invent policy.

8. Grounding transparency: if you answer a factual question WITHOUT having \
called `retrieve` in the current turn, you MUST prefix or suffix the answer \
with a short, explicit non-grounded disclosure — e.g. "From general \
knowledge (not retrieved from the vehicle knowledge base):". Never present \
model-recalled facts as if they came from the KB.

Session context:
  - tenantId: {tenant_id}
  - sessionId: {session_id}
  - Known VIN: {vin}

If the caller has not given a VIN and you do not have one in context, \
ask for it before calling `triage`. Do not guess a VIN."""


# The one persona that MUST keep the pre-2026-07-28 text-runtime behavior
# (hardcoded [triage, retrieve, book] + SYSTEM_PROMPT_TEMPLATE). This is the
# persona `infer_persona` yields for CMS web-chat callers, so pinning it here
# is what makes the Option-B persona wiring a zero-regression change. See
# spec 2026-08-04-cvx-oem-discover-order-meridian decisions.md (merged from predecessor 2026-07-28).
_LEGACY_TEXT_PERSONA = "fleet_driver"


def build_system_prompt(persona_id: str, session: SessionContext) -> str:
    """Select and render a system prompt for the given persona.

    Tries ``PERSONA_PROMPTS[persona_id]`` from ``persona_definitions`` (Group 5.A).
    Falls back to ``SYSTEM_PROMPT_TEMPLATE`` if the symbol is not yet present.
    Hard rules are always prepended via ``_HARD_RULES``.

    Never raises on unknown persona_id — falls back with a WARNING log.

    Accepts both ``SessionContext`` dataclass instances and ``SimpleNamespace``
    objects (the latter are used in unit tests and by the kiosk path).
    """
    # Produce a dict from the session regardless of whether it's a dataclass
    # or a SimpleNamespace / arbitrary object.  ``dataclasses.asdict`` requires
    # a dataclass instance and raises ``TypeError`` on anything else.
    import dataclasses as _dc
    if _dc.is_dataclass(session) and not isinstance(session, type):
        session_dict = _dc.asdict(session)
    else:
        session_dict = vars(session) if hasattr(session, "__dict__") else {}

    try:
        from agents.supervisor.persona_definitions import PERSONA_PROMPTS  # type: ignore[attr-defined]
        template = PERSONA_PROMPTS.get(persona_id)
        if template is None:
            log.warning(
                "build_system_prompt: unknown persona_id=%r, falling back to default",
                persona_id,
            )
            template = PERSONA_PROMPTS.get("fleet_driver", SYSTEM_PROMPT_TEMPLATE)
        persona_body = template.format(**session_dict)
    except (ImportError, AttributeError):
        # Group 5.A has not shipped PERSONA_PROMPTS yet — use legacy template.
        persona_body = SYSTEM_PROMPT_TEMPLATE.format(
            tenant_display="Fleet Services",
            tenant_id=session_dict.get("tenant_id", ""),
            segment=session_dict.get("tenant_segment", "fleet"),
            session_id=session_dict.get("session_id", ""),
            vin=session_dict.get("vin") or "(not yet provided)",
        )

    return _HARD_RULES + "\n" + persona_body


def build_supervisor(
    ctx: SessionContext,
    config: Config,
    *,
    tenant_display: str = "Cascadia Fleet Services",
    tenant_segment: str = "fleet",
) -> Agent:
    """Construct a Strands Agent ready to serve one session.

    The agent is stateless between sessions — caller creates a new
    SessionContext and calls build_supervisor again for each new
    conversation. This mirrors how AgentCore will work in production.

    Args:
        ctx: Session context (tenant, session id, JWT, VIN).
        config: Static configuration.
        tenant_display: Human-facing tenant name for the prompt. Read
            from tenant config in production; hard-coded here.
        tenant_segment: One of fleet | oem | rental.

    Returns:
        A Strands Agent with triage, retrieve, and book tools registered
        and Claude Sonnet 4.6 as the model.
    """
    model = BedrockModel(
        model_id=config.supervisor_model_id,
        region_name=config.aws_region,
        max_tokens=config.supervisor_max_tokens,
    )

    triage = make_triage_tool(ctx, config)
    retrieve = make_retrieve_tool(ctx, config)
    book = make_book_tool(ctx, config)

    # ------------------------------------------------------------------
    # Anonymous-kiosk path (spec 2026-07-31-zone2-order-backend-and-kiosk § D3).
    #
    # DEFAULT-DENY: a kiosk session (is_kiosk=True) gets ONLY the ten
    # allow-listed tools. An unknown/unrecognised persona also falls here
    # because PERSONA_TOOL_FACTORIES won't have a matching entry, and the
    # wider consumer surface must never be silently granted to an anonymous
    # visitor (decisions.md Q10).
    #
    # The kiosk check must come BEFORE the persona-aware block so that a
    # kiosk device presenting a valid JWT and persona_id="consumer" still
    # gets the narrow surface rather than the full consumer surface.
    #
    # Voice/bidi path (bidi_app.py) is NOT touched here — Nova Sonic's
    # ~1s tool-return budget is empirically load-bearing and is out of scope.
    # ------------------------------------------------------------------
    is_kiosk: bool = bool(getattr(ctx, "is_kiosk", False))

    if is_kiosk:
        from agents.supervisor.tools import (  # noqa: PLC0415
            PERSONA_TOOL_FACTORIES as _PTF,
            make_catalog_compare_tool,
            make_catalog_query_tool,
            make_configurator_recommend_tool,
            make_delivery_status_tool,
            make_knowledge_tool,
            make_manufacturing_explain_tool,
            make_offers_lookup_tool,
            make_variant_compare_tool,
        )

        kiosk_factories = [
            make_catalog_query_tool,
            make_catalog_compare_tool,
            make_variant_compare_tool,
            make_configurator_recommend_tool,
            make_offers_lookup_tool,
            make_manufacturing_explain_tool,
            make_delivery_status_tool,
            make_retrieve_tool,
            make_knowledge_tool,
            make_kb_filter_tool,
        ]
        tools = [
            f(ctx, config) if "config" in f.__code__.co_varnames else f(ctx)
            for f in kiosk_factories
        ]
        system_prompt = build_system_prompt("consumer", ctx)
        log.info(
            "build_supervisor: kiosk path tenant=%s session=%s tools=%d",
            ctx.tenant_id,
            ctx.session_id,
            len(tools),
        )

    # ------------------------------------------------------------------
    # Persona-aware tool + prompt selection (spec
    # 2026-08-04-cvx-oem-discover-order-meridian, Option B).
    #
    # Historically this function hardcoded `[triage, retrieve, book]` and
    # the fleet-oriented SYSTEM_PROMPT_TEMPLATE, ignoring persona entirely.
    # `PERSONA_TOOL_FACTORIES` / `build_system_prompt` were consumed ONLY by
    # bidi_app.py (the voice runtime), so the text runtime could never serve
    # a non-fleet persona — the Discover tools were registered into a
    # registry this path never read.
    #
    # This is deliberately GATED: `fleet_driver` (and any absent/unknown
    # persona) takes the byte-identical legacy path, so the CMS web-chat
    # surface on both staging and prod is unchanged. Only an explicitly
    # recognised non-fleet persona gets the registry-driven treatment.
    # Verified by re-running scripts/smoke-test-assistant-chat.py, which
    # exercises the fleet_driver path.
    # ------------------------------------------------------------------
    else:
        persona_id = (getattr(ctx, "persona_id", "") or "").strip()
        legacy_tools = [triage, retrieve, book]
        use_persona_path = bool(persona_id) and persona_id != _LEGACY_TEXT_PERSONA

        if use_persona_path:
            try:
                from agents.supervisor.tools import PERSONA_TOOL_FACTORIES

                factories = PERSONA_TOOL_FACTORIES.get(persona_id)
                if not factories:
                    raise KeyError(f"no tool factories for persona {persona_id!r}")

                # Tool factories are inconsistent in arity — some take
                # (session, config), some take (session) only. Mirror the
                # defensive dispatch already used in bidi_app.py rather than
                # inventing a second convention.
                tools = [
                    f(ctx, config) if "config" in f.__code__.co_varnames else f(ctx)
                    for f in factories
                ]
                system_prompt = build_system_prompt(persona_id, ctx)
                log.info(
                    "build_supervisor: persona-aware path persona=%s tools=%d",
                    persona_id,
                    len(tools),
                )
            except Exception:
                # Never fail a session because a persona is misconfigured —
                # degrade to the proven legacy path and make it loud.
                log.warning(
                    "build_supervisor: persona-aware build failed for persona=%r "
                    "— falling back to legacy fleet tools/prompt",
                    persona_id,
                    exc_info=True,
                )
                tools = legacy_tools
                system_prompt = SYSTEM_PROMPT_TEMPLATE.format(
                    tenant_display=tenant_display,
                    tenant_id=ctx.tenant_id,
                    segment=tenant_segment,
                    session_id=ctx.session_id,
                    vin=ctx.vin or "(not yet provided)",
                )
        else:
            tools = legacy_tools
            system_prompt = SYSTEM_PROMPT_TEMPLATE.format(
                tenant_display=tenant_display,
                tenant_id=ctx.tenant_id,
                segment=tenant_segment,
                session_id=ctx.session_id,
                vin=ctx.vin or "(not yet provided)",
            )

    log.info(
        "build_supervisor: tenant=%s session=%s model=%s prompt_hash=%s",
        ctx.tenant_id,
        ctx.session_id,
        config.supervisor_model_id,
        prompt_hash(system_prompt),
    )

    return Agent(
        model=model,
        tools=tools,
        system_prompt=system_prompt,
        name="vsa-supervisor",
        description=(
            f"VSA supervisor for {tenant_display}. Handles customer-facing "
            "service interactions with deterministic triage and STAR booking."
        ),
    )


def prompt_hash(prompt: str) -> str:
    """SHA256 hash of a prompt — matches the audit field in DESIGN docs."""
    return hashlib.sha256(prompt.encode()).hexdigest()[:16]
