"""triage tool — wraps the deployed POST /triage endpoint.

The classifier is the deterministic source of truth for P0–P3. The LLM
narrates the result; it cannot downgrade a P0 (see VIRTUAL_SERVICE_AGENT.md
§2 and §10, and VSA_TENANT_CONFIG.md §3 triage.policyOverrides.
downgradeForbidden).

This tool calls the live deployed API. JWT and tenantId come from the
SessionContext, not from the LLM — the LLM only chooses which VIN to
classify.

Return shape matches VSA_API_CONTRACT.md §2 plus a triageDecisionId
field that the book tool later uses to correlate the booking.
"""

from __future__ import annotations

import logging
from typing import Any

import requests
from pydantic import BaseModel, Field
from strands import tool

from agents.supervisor.config import Config
from agents.supervisor.dtc_response_catalog import faults_for_codes, p0_directive
from agents.supervisor.session import SessionContext

log = logging.getLogger(__name__)


class TriageResult(BaseModel):
    """Output of the triage tool — matches POST /triage response plus correlation id."""

    classification: str = Field(..., description="One of P0, P1, P2, P3")
    sessionId: str = Field(..., description="Session id echoed by the classifier")
    decidedAt: str = Field(..., description="ISO 8601 UTC timestamp of the decision")
    latencyMs: int = Field(..., description="End-to-end classifier latency")
    triageDecisionId: str = Field(
        ..., description="Stable id for this decision; used to correlate bookings"
    )


def call_triage_endpoint(
    ctx: SessionContext,
    config: Config,
    vin: str,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Call the deployed /triage endpoint directly, bypassing the @tool wrapper.

    This exists so bidi_app.py can call triage() without going through
    Strands' tool-registry/LLM-invocation path, which Nova Sonic v2 does
    not reliably trigger (upstream issue strands-agents/sdk-python#2021).

    Behavior is identical to the @tool-decorated triage function: hits the
    live endpoint, updates ctx.triage_classification + ctx.triage_decision_id
    + ctx.vin, returns the parsed response dict matching TriageResult.
    """
    payload: dict[str, Any] = {
        "vin": vin,
        "sessionId": ctx.session_id,
        "tenantId": ctx.tenant_id,
    }
    if extra is not None:
        payload["extra"] = extra

    url = f"{config.api_base}/triage"
    log.info("triage direct: POST %s vin=%s tenant=%s", url, vin, ctx.tenant_id)
    resp = requests.post(
        url,
        json=payload,
        headers={
            "Authorization": f"Bearer {ctx.jwt}",
            "Content-Type": "application/json",
        },
        timeout=10,
    )

    if resp.status_code == 401:
        raise TriageAuthError(
            "Triage endpoint returned 401. JWT may have expired."
        )
    if resp.status_code >= 400:
        raise TriageError(
            f"Triage endpoint returned {resp.status_code}: {resp.text[:200]}"
        )

    body = resp.json()
    ctx.triage_decision_id = body.get("sessionId")
    ctx.triage_classification = body.get("classification")
    # The codes the classifier used (Fix Group 14). Kept on ctx, not added to
    # the returned TriageResult, so the model's tool result is unchanged.
    ctx.triage_active_dtcs = [c for c in (body.get("activeDtcs") or []) if isinstance(c, str)]
    ctx.vin = vin

    return TriageResult(
        classification=body["classification"],
        sessionId=body["sessionId"],
        decidedAt=body["decidedAt"],
        latencyMs=body["latencyMs"],
        triageDecisionId=body["sessionId"],
    ).model_dump()


# Instruction returned beside `sayFirst` (escalation Fix Group 16). The model got a P0
# from its own triage call, made 14 more tool calls and never spoke the pull-over
# (iOS run 2026-09-26 14:27Z); the prompt rule alone did not hold.
SAY_FIRST_INSTRUCTION = (
    "Say sayFirst to the driver now, word for word, before you call any other tool."
)

# Personas who are not in the vehicle (review Cycle 24 S3). The directive speaks to
# the driver ("pump the brakes…"), so these get the classification without it, as
# before Fix Group 16. A denylist on purpose: a new or unknown persona gets the line.
NOT_IN_VEHICLE_PERSONAS = frozenset({
    "retail_advisor", "service_advisor", "technician", "fleet_manager", "fleet_owner",
})


def p0_say_first(ctx: SessionContext) -> str:
    """The P0 safety directive for the model to speak, as shown on screen.

    In the bidi runtime ``ctx.p0_directive_for_model`` returns the line the platform
    will show (same selection, upgrades included; the screen keeps a more protective
    line if one is already there). Elsewhere the directive is
    picked from the codes the classifier used. Never raises and never returns empty:
    any failure yields the conservative stop-engine directive, since an empty
    ``sayFirst`` on a P0 would leave the model with nothing to say first.
    """
    try:
        hook = getattr(ctx, "p0_directive_for_model", None)
        if hook is not None:
            text = hook()
        else:
            faults = faults_for_codes(getattr(ctx, "triage_active_dtcs", None))
            text = p0_directive(faults, getattr(ctx, "driver_first_name", None))
        if isinstance(text, str) and text.strip():
            return text
    except Exception:  # noqa: BLE001
        log.exception("triage: P0 sayFirst selection failed, using the fallback")
    return p0_directive([], getattr(ctx, "driver_first_name", None))


# Fix Group 18: what the model is told when a pinned session's triage call named
# another vehicle, or when the pinned vehicle's VIN is unknown.
PINNED_VIN_NOTE = (
    "This chat covers only the signed-in driver's assigned vehicle, so that "
    "vehicle was classified. The VIN you passed was not used."
)
# Facts only: the system prompt's triage-error rule governs the reply (review
# Cycle 3 W1), so this text must not give the driver instructions of its own.
PINNED_VIN_UNKNOWN = (
    "This chat covers only the signed-in driver's assigned vehicle, and its VIN "
    "could not be read, so it was not classified. Do not ask the driver for a VIN."
)


def make_triage_tool(ctx: SessionContext, config: Config):
    """Build the triage tool bound to a session context.

    The returned function is decorated with @tool so Strands can discover
    it. Only `vin` and optional `extra` are LLM-visible — tenant, session,
    and auth are closure-captured from ctx/config.
    """

    @tool
    def triage(vin: str, extra: dict | None = None) -> dict[str, Any]:
        """Classify the current vehicle into P0, P1, P2, or P3 using the
        deterministic VSA triage classifier. ALWAYS call this before
        narrating any criticality level to the user. The classifier is the
        source of truth; you cannot override or downgrade its output.

        Args:
            vin: The vehicle identification number to classify. Must be
                the vehicle currently under discussion.
            extra: Optional additional context to pass to tenant rule
                expressions (for example, {"fleet_sla": "tier-1"} for
                Cascadia tier-1 customers). Omit if you don't know.

        Returns:
            A dict with keys classification (P0|P1|P2|P3), sessionId,
            decidedAt, latencyMs, and triageDecisionId. On P0 it also has
            sayFirst (the safety instruction to say to the driver first, word
            for word) and nextStep.
        """
        payload: dict[str, Any] = {
            "vin": vin,
            "sessionId": ctx.session_id,
            "tenantId": ctx.tenant_id,
        }
        if extra is not None:
            payload["extra"] = extra

        # Fix Group 18: a session pinned to the caller's claim vehicle always
        # classifies that vehicle. Checked here, at the model boundary, and not
        # in call_triage_endpoint, whose direct callers pass trusted VINs.
        pinned_note: str | None = None
        if getattr(ctx, "vin_pinned", False):
            pinned = (ctx.vin or "").strip()
            if not pinned:
                raise TriageError(PINNED_VIN_UNKNOWN)
            if str(vin or "").strip().upper() != pinned.upper():
                log.warning("triage: session is pinned to its claim vehicle; the model's VIN was not used")
                pinned_note = PINNED_VIN_NOTE
            vin = pinned

        # Delegate to the standalone helper so @tool and direct calls
        # go through the same code path.
        result = call_triage_endpoint(ctx, config, vin, extra)
        if pinned_note:
            result["vinNote"] = pinned_note
        # Only the model-facing path gets the words: the direct callers in
        # bidi_app give the model the directive in their own note.
        if (
            result.get("classification") == "P0"
            and getattr(ctx, "persona_id", None) not in NOT_IN_VEHICLE_PERSONAS
        ):
            result["sayFirst"] = p0_say_first(ctx)
            result["nextStep"] = SAY_FIRST_INSTRUCTION
            ctx.p0_say_first_given = result["sayFirst"]
        return result

    return triage


class TriageError(RuntimeError):
    """Triage endpoint returned a non-2xx response."""


class TriageAuthError(TriageError):
    """Triage endpoint rejected auth — most likely an expired JWT."""
