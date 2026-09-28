"""escalate tools — wraps the deployed POST /escalate endpoint.

This module provides two @tool-decorated entry points:

  escalate_to_human  — safety escalation; severity read from
      ctx.triage_classification (never an LLM parameter). Fails closed
      when no triage verdict is set.

  escalate_for_service — service escalation; structurally carries no
      severity, so the roadside-dispatch path cannot be reached by a
      non-mechanical request (e.g. a purchase enquiry).

Same factory pattern as `triage.py` and `book.py` — tenant, JWT, session
id, vin, vehicleId, driverId come from the SessionContext closure and are
not LLM-visible. The LLM only chooses reason/summary.

There is also a `call_escalate_endpoint(ctx, config, ...)` helper that
bypasses the @tool wrappers. `bidi_app.py` uses it to auto-escalate on
P0 without going through Nova Sonic (which doesn't reliably invoke
registered tools — Strands #2021), exactly as the triage/book path
already does.

Return shape: the full /escalate response plus a narrative-friendly
`summary` string for the model to use in its spoken reply. The raw
`contactId`, `participantId`, `participantToken`, `connectionExpiry`, and
`rsaDispatched` fields stay top-level so bidi_app.py can pluck them for
the `escalation` side-channel event sent to iOS.

Design note: the `escalation.autoRequestRoadsideOnP0` and
`escalation.autoOpenHumanChatOnP0` tenant flags (both default false)
control auto-dispatch and auto-open-chat at the Lambda and bidi_app.py
layer respectively. Per spec D3, roadside requires a separate user-confirmed
tap — the @tool path never passes dispatch_roadside=True automatically.
"""

from __future__ import annotations

import logging
import threading
from typing import Any

import requests
from pydantic import BaseModel, Field
from strands import tool

from agents.supervisor.config import Config
from agents.supervisor.session import SessionContext

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Text-runtime side channel.
#
# The bidi/voice path emits the participant credentials to iOS as a WebSocket
# `escalation` event. The text path (`/assistant/chat`) has no side channel —
# it returns a single string — so a text-mode caller had no way to actually
# JOIN the Connect chat the tool had just created. The contact was real but
# unreachable from the client.
#
# Keyed by session id, NOT a bare global and NOT a ContextVar:
#   - A bare global would leak one caller's participant token into another
#     caller's response when AgentCore serves concurrent invocations in one
#     container. That is a credential disclosure, not merely a correctness bug.
#   - A ContextVar looks right but is unreliable here: Strands may execute a
#     tool in a worker thread, and a value set in that thread's context does
#     not propagate back to the entrypoint's context, so the payload would
#     silently vanish.
# A session-keyed map is correct under both conditions.
_ESCALATIONS: dict[str, dict[str, Any]] = {}
_ESCALATIONS_LOCK = threading.Lock()
_ESCALATIONS_MAX = 256


def _record_escalation(session_id: str, payload: dict[str, Any]) -> None:
    """Stash handoff credentials for this session's current turn."""
    if not session_id:
        return
    with _ESCALATIONS_LOCK:
        # Bound the map so a long-lived container cannot grow it without limit
        # if some caller never reads its entry back.
        if len(_ESCALATIONS) >= _ESCALATIONS_MAX:
            _ESCALATIONS.clear()
        _ESCALATIONS[session_id] = payload


def take_last_escalation(session_id: str) -> dict[str, Any] | None:
    """Pop the escalation created during this session's current turn, if any.

    Read-and-clear, so a later turn on the same session cannot re-emit stale
    credentials to the client.
    """
    if not session_id:
        return None
    with _ESCALATIONS_LOCK:
        return _ESCALATIONS.pop(session_id, None)


class EscalateResult(BaseModel):
    """Parsed /escalate response plus a narration-friendly summary."""

    contactId: str = Field(..., description="Amazon Connect contact id")
    participantId: str = Field(
        ..., description="Amazon Connect participant id for the driver side"
    )
    participantToken: str = Field(
        ..., description="JWT used to join the Connect chat WebSocket"
    )
    connectionExpiry: str = Field(
        ..., description="ISO-8601 expiry of participantToken (~15 min)"
    )
    severity: str = Field(..., description="Echoed severity: P0|P1|P2|P3")
    rsaDispatched: bool = Field(
        False,
        description=(
            "True iff a roadside request was RECORDED (status REQUESTED) in the "
            "RSA dispatch table. Recorded is not dispatched; the dispatch that "
            "may follow is simulated."
        ),
    )
    rsaActionId: str | None = Field(
        None, description="RSA dispatchId (rsa-<uuid>) when rsaDispatched=True"
    )
    summary: str = Field(
        ...,
        description=(
            "Short narrative for the model to speak to the driver. "
            "Uses the driver's displayName and severity where known."
        ),
    )


def call_escalate_endpoint(
    ctx: SessionContext,
    config: Config,
    *,
    severity: str,
    reason: str,
    summary: str | None = None,
    dispatch_roadside: bool | None = None,
    display_name: str | None = None,
    escalation_type: str = "safety",
    triage_session_id: str | None = None,
) -> dict[str, Any]:
    """Call the deployed /escalate endpoint directly, bypassing the @tool wrapper.

    Parallel to `triage.call_triage_endpoint` and `book.call_book_endpoint`.
    Exists so `bidi_app.py` can invoke escalation on P0 without going
    through the model's tool-call path, which Nova Sonic v2 does not
    reliably trigger (Strands sdk-python#2021).

    Args:
        ctx: SessionContext providing tenant, jwt, session id, vin,
            vehicle id, driver id.
        config: Config providing api_base.
        severity: "P0"|"P1"|"P2"|"P3" (required by /escalate; must be
            derived from ctx.triage_classification at the LLM boundary).
        reason: Short human-readable reason — flows to the agent's
            incoming-chat context. Typically the driver's reported
            symptom phrase (e.g. "engine overheating").
        summary: Optional longer summary of the voice conversation. If
            provided, the /escalate Lambda surfaces it on the agent side.
        dispatch_roadside: Override the Lambda's default. When None the
            Lambda applies its own tenant-config default. The safety
            path (escalation_type="safety") may set this; the service
            path always forces it False.
        display_name: Optional driver display name to show the agent.
            When None and ctx has driver_id set, the /escalate Lambda
            can resolve the name from its own CMS lookup — but passing
            it through saves that round-trip.
        escalation_type: "safety" (default, back-compat) or "service".
            The "service" type carries no P-level semantics — the Lambda
            will force dispatch_roadside=False regardless of the request
            body, so a service call can never queue a roadside truck.
        triage_session_id: Override for the `triageSessionId` sent. Defaults
            to ctx.triage_decision_id (the classifier's decision row). The
            Lambda honours a roadside dispatch only when this names a recent
            platform-recorded P0 row for the same tenant and VIN (security W1),
            so a driver-confirmed P0 must name the confirmation audit row
            instead — the classifier's row for that session is not P0.

    Returns:
        A dict matching EscalateResult, ready to send to the iOS client
        via the `escalation` wire-event. Raises EscalateError on non-2xx
        or EscalateAuthError specifically on 401.
    """
    if severity not in ("P0", "P1", "P2", "P3"):
        raise EscalateError(
            f"Invalid severity {severity!r}; must be P0|P1|P2|P3"
        )

    payload: dict[str, Any] = {
        "severity": severity,
        "vin": ctx.vin,
        "vehicleId": ctx.vehicle_id,
        "reason": reason,
        "voiceSessionId": ctx.session_id,
        "escalationType": escalation_type,
    }
    if summary is not None:
        payload["summary"] = summary
    if dispatch_roadside is not None:
        payload["dispatchRoadside"] = dispatch_roadside
    decision_id = triage_session_id or ctx.triage_decision_id
    if decision_id:
        # The Lambda verifies a roadside dispatch against this row
        # (vsa-{stage}-triage-decisions) — see its _verified_p0_decision.
        payload["triageSessionId"] = decision_id
    if ctx.driver_id:
        payload["driverId"] = ctx.driver_id
    if display_name:
        payload["displayName"] = display_name

    # /escalate requires the VIN and vehicleId. Both must be non-empty.
    if not payload["vin"]:
        raise EscalateError(
            "Cannot escalate: ctx.vin is not set. The VIN is required for "
            "/escalate. Provide it at session start."
        )
    if not payload["vehicleId"]:
        raise EscalateError(
            "Cannot escalate: ctx.vehicle_id is not set. The vehicleId is "
            "required for /escalate. Provide it at session start."
        )

    url = f"{config.api_base}/escalate"
    # Log the payload minus any PII-ish fields. severity/vin/vehicleId are
    # fine to log; reason/summary we just truncate.
    safe_preview = {
        k: (v if k not in ("reason", "summary") else f"<{len(str(v))} chars>")
        for k, v in payload.items()
    }
    log.info("escalate direct: POST %s payload=%s", url, safe_preview)

    try:
        resp = requests.post(
            url,
            json=payload,
            headers={
                "Authorization": f"Bearer {ctx.jwt}",
                "Content-Type": "application/json",
            },
            timeout=15,
        )
    except requests.RequestException as e:
        raise EscalateError(f"Network error calling /escalate: {e}") from e

    if resp.status_code == 401:
        raise EscalateAuthError(
            "Escalate endpoint returned 401. JWT may have expired."
        )
    if resp.status_code >= 400:
        raise EscalateError(
            f"Escalate endpoint returned {resp.status_code}: {resp.text[:300]}"
        )

    body = resp.json()

    # Build a narration-friendly summary the model can speak. Keeps the
    # raw severity + name + rsa fields at top level so bidi_app.py can
    # still reach them when forwarding the wire event to iOS.
    narration = _build_narration(
        severity=body.get("severity", severity),
        rsa_dispatched=bool(body.get("rsaDispatched")),
        display_name=display_name,
    )

    result = EscalateResult(
        contactId=body["contactId"],
        participantId=body["participantId"],
        participantToken=body["participantToken"],
        connectionExpiry=body["connectionExpiry"],
        severity=body.get("severity", severity),
        rsaDispatched=bool(body.get("rsaDispatched")),
        rsaActionId=body.get("rsaActionId"),
        summary=narration,
    ).model_dump()

    log.info(
        "escalate direct: contact=%s severity=%s rsaDispatched=%s",
        result["contactId"],
        result["severity"],
        result["rsaDispatched"],
    )

    # Record for the text-runtime side channel (see _ESCALATIONS above).
    # Only the fields iOS needs to join the chat, so nothing extra rides along.
    _record_escalation(ctx.session_id, {
        "contactId": result["contactId"],
        "participantId": result["participantId"],
        "participantToken": result["participantToken"],
        "connectionExpiry": result["connectionExpiry"],
        "severity": result["severity"],
    })
    return result


def make_escalate_tool(ctx: SessionContext, config: Config):
    """Build the safety-escalation tool bound to a session context.

    Returns ONE `@tool`-decorated function, `escalate_to_human`. The
    service-escalation counterpart has its own factory,
    `make_service_escalate_tool` — see the note below on why they are not
    combined. Tenant, jwt, vin, vehicleId, driverId, and sessionId are
    closure-captured from ctx/config and are not LLM-visible.

    **One factory returns exactly one tool.** Every other module in this
    package holds that ratio (`dms_copilot.py` is 3 factories / 3 tools,
    `dms_warranty.py` 2 / 2), and two things depend on it:

      1. `supervisor.py`'s registration builds a FLAT list —
         ``[f(ctx, config) for f in factories]`` (:381-384). A factory
         returning a tuple puts a tuple where Strands expects a tool, and
         the surrounding try/except ("never fail a session because a
         persona is misconfigured", :385) SWALLOWS the resulting error and
         silently falls back to the legacy path. The tools go missing with
         a green test suite. Observed 2026-09-23 during this spec's
         Group 1.
      2. `scripts/generate_agent_inventory.py` counts factory names in
         ``__all__`` and renders them as "N registered tools"
         (:417, :493). A factory returning two tools makes that count
         understate reality — 39 vs 40 table rows, observed the same day.
         Honest agent/tool counts are an explicit portfolio constraint.

    Note: Nova Sonic v2 doesn't reliably invoke registered tools (see
    Strands #2021), so in the bidi voice path `bidi_app.py` calls
    `call_escalate_endpoint` directly. These @tool wrappers are what
    the text-mode supervisor (`supervisor.py` / `agentcore_app.py`) uses.
    """

    @tool
    def escalate_to_human(
        reason: str,
        summary: str = "",
    ) -> dict[str, Any]:
        """Hand the driver off to a human fleet-support agent via Amazon
        Connect chat when there is a safety or mechanical concern.

        Use this when:
          - The triage classifier returned P0 or P1 and the driver
            confirmed they want to be connected.
          - The driver directly asks to speak to a human about a
            vehicle fault, warning, or safety concern.

        Severity is read automatically from the session's triage result.
        Do NOT invent a severity — if no triage has run this call will
        fail closed.

        This tool creates an Amazon Connect chat contact, returns the
        participant credentials the iOS client uses to join the chat.
        Roadside assistance requires a separate confirmed tap.

        Args:
            reason: One short phrase describing why we're escalating,
                in the driver's own words when possible (e.g. "engine
                overheating", "ABS warning light and brake pedal feels
                soft"). Flows to the agent's incoming-chat view.
            summary: Optional 1-3 sentence summary of the voice
                conversation so far. Helps the agent pick up context
                without replaying the audio. Defaults to empty.

        Returns:
            A dict with:
              contactId: Connect contact id for the handoff.
              participantId, participantToken, connectionExpiry: creds
                for the iOS client to join the chat WebSocket.
              severity: echoed back (authoritative from server).
              rsaDispatched: bool — always false; roadside is a
                separate confirmed tap.
              summary: short narrative you can use in your spoken reply
                (e.g. "I've connected you to Kevin in fleet support...").
        """
        # Read severity from session context — the triage classifier is
        # the only authoritative source. Do not default.
        if ctx.triage_classification is None:
            raise EscalateError(
                "Cannot escalate to human: ctx.triage_classification is not set. "
                "A triage call must complete before escalation. "
                "Run the triage tool first, or use escalate_for_service if the "
                "driver simply wants a human without a vehicle verdict."
            )
        return call_escalate_endpoint(
            ctx,
            config,
            severity=ctx.triage_classification,
            reason=reason,
            summary=summary or None,
            # Roadside dispatch is a separate user-confirmed tap; never
            # auto-dispatch from the LLM-facing safety path.
            dispatch_roadside=False,
            display_name=None,
        )

    return escalate_to_human


def make_service_escalate_tool(ctx: SessionContext, config: Config):
    """Build the service-escalation tool bound to a session context.

    Returns ONE `@tool`-decorated function, `escalate_for_service`, for the
    "user asked for a human, no vehicle verdict" path (spec D1).

    Separate from `make_escalate_tool` on purpose — see that factory's
    docstring for the two mechanisms that depend on one-factory-one-tool.
    Keeping them distinct also means a caller registering only the safety
    tool cannot accidentally hand the LLM the service tool, or vice versa.
    """

    @tool
    def escalate_for_service(
        reason: str,
        summary: str = "",
    ) -> dict[str, Any]:
        """Connect the user to a human agent for a service enquiry —
        sales, billing, general help — where there is no vehicle fault
        or triage verdict.

        Use this when:
          - The user asks to speak to a person for any non-safety reason
            (e.g. "I want to talk to someone", "can I speak to an agent",
            purchase or pricing questions, account help).
          - There is no active triage classification.

        This path structurally cannot carry a severity or dispatch
        roadside. Calling it will never queue a roadside assistance
        truck, regardless of any other request field.

        Args:
            reason: Short phrase describing what the user wants help with
                (e.g. "motorcycle purchase enquiry", "billing question").
            summary: Optional conversation summary for the agent.

        Returns:
            A dict with contactId, participantId, participantToken,
            connectionExpiry for the iOS client to join the chat.
            rsaDispatched is always False.
        """
        return call_escalate_endpoint(
            ctx,
            config,
            # D1 says the service path "structurally cannot carry a
            # severity". It does not fully hold on the wire yet, and this
            # is the honest version rather than the aspirational one.
            #
            # `/escalate` validates severity ∈ {P0..P3} for every request
            # (handler.py:320-322) and packs it into the Amazon Connect
            # contact attributes, which the deployed contact flow reads at
            # `$.Attributes.severity` for priority routing
            # (handler.py:205-210). Dropping or renaming it is not
            # verifiable from this repo — the flow lives in Connect — so
            # sending a non-P sentinel risks breaking live routing for a
            # vocabulary win.
            #
            # What IS enforced, server-side and test-pinned: escalation_type
            #="service" forces dispatch_roadside=False and skips the P0
            # callback regardless of this value, so the roadside-truck-for-a
            # -shopper path is closed. The Lambda also stamps
            # `escalationType` into the contact attributes so the tier-2
            # agent and the CMS EscalationContextPanel can tell a sales
            # enquiry from a P3 fault.
            #
            # Remaining gap, recorded in decisions.md: the wire still says
            # P3 for a non-safety enquiry. Closing it needs the Connect
            # contact flow inspected first.
            severity="P3",
            reason=reason,
            summary=summary or None,
            dispatch_roadside=False,
            display_name=None,
            escalation_type="service",
        )

    return escalate_for_service


# ---------------------------------------------------------------------------
# Narration helper
# ---------------------------------------------------------------------------


def _build_narration(
    *,
    severity: str,
    rsa_dispatched: bool,
    display_name: str | None,
) -> str:
    """Build a short spoken-friendly summary for the model to use.

    The model is free to rephrase, but having a canonical baseline
    string means the narration is consistent even when the LLM's own
    phrasing drifts. Keep it short — this will be spoken out loud.
    """
    who = "you"  # model will render from the driver's POV
    agent_name = "the next available fleet support agent"

    if severity == "P0":
        base = (
            f"I'm connecting {who} to {agent_name} right now — hang on "
            f"the line and they'll pick up in a moment."
        )
        if rsa_dispatched:
            # A REQUEST was recorded; nothing guarantees a truck is coming.
            # RSA Phase B dispatch is simulated and can still fail on location.
            # Was "Roadside assistance is also on its way." — review C2W4.
            base += " A roadside assistance request has also been recorded."
        return base

    if severity == "P1":
        return (
            f"I'm connecting {who} to {agent_name}. They'll be with {who} "
            f"shortly."
        )

    # P2/P3 — escalation was probably driver-initiated. Keep it neutral.
    return (
        f"I'm connecting {who} to {agent_name}. They'll be with {who} shortly."
    )


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class EscalateError(RuntimeError):
    """Escalate endpoint returned a non-2xx response or was called with
    invalid preconditions."""


class EscalateAuthError(EscalateError):
    """Escalate endpoint rejected auth — most likely an expired JWT."""
