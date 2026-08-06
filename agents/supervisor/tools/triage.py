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
    ctx.vin = vin

    return TriageResult(
        classification=body["classification"],
        sessionId=body["sessionId"],
        decidedAt=body["decidedAt"],
        latencyMs=body["latencyMs"],
        triageDecisionId=body["sessionId"],
    ).model_dump()


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
            decidedAt, latencyMs, and triageDecisionId.
        """
        payload: dict[str, Any] = {
            "vin": vin,
            "sessionId": ctx.session_id,
            "tenantId": ctx.tenant_id,
        }
        if extra is not None:
            payload["extra"] = extra

        # Delegate to the standalone helper so @tool and direct calls
        # go through the same code path.
        return call_triage_endpoint(ctx, config, vin, extra)

    return triage


class TriageError(RuntimeError):
    """Triage endpoint returned a non-2xx response."""


class TriageAuthError(TriageError):
    """Triage endpoint rejected auth — most likely an expired JWT."""
