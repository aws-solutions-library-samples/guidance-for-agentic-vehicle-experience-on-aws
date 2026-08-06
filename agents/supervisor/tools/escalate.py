"""escalate tool — wraps the deployed POST /escalate endpoint.

This tool hands the driver off to a human agent via Amazon Connect. The
deployed `/escalate` Lambda handles the Connect-side work (StartChatContact,
CreateParticipantConnection context), writes a PENDING row to the
roadside-dispatch queue for P0, and returns the participantToken the iOS
client needs to join the chat WebSocket directly.

Same factory pattern as `triage.py` and `book.py` — tenant, JWT, session
id, vin, vehicleId, driverId come from the SessionContext closure and are
not LLM-visible. The LLM only chooses severity/reason/summary/dispatch.

There is also a `call_escalate_endpoint(ctx, config, ...)` helper that
bypasses the @tool wrapper. `bidi_app.py` uses it to auto-escalate on P0
without going through Nova Sonic (which doesn't reliably invoke registered
tools — Strands #2021), exactly as the triage/book path already does.

Return shape: the full /escalate response plus a narrative-friendly
`summary` string for the model to use in its spoken reply. The raw
`contactId`, `participantId`, `participantToken`, `connectionExpiry`, and
`rsaDispatched` fields stay top-level so bidi_app.py can pluck them for
the `escalation` side-channel event sent to iOS.
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
        False, description="True iff roadside assistance was auto-queued"
    )
    rsaActionId: str | None = Field(
        None, description="Action-queue row id when rsaDispatched=True"
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
        severity: "P0"|"P1"|"P2"|"P3" (required by /escalate).
        reason: Short human-readable reason — flows to the agent's
            incoming-chat context. Typically the driver's reported
            symptom phrase (e.g. "engine overheating").
        summary: Optional longer summary of the voice conversation. If
            provided, the /escalate Lambda surfaces it on the agent side.
        dispatch_roadside: Override the Lambda's default. If None, the
            Lambda applies its own default (true for P0, false otherwise).
        display_name: Optional driver display name to show the agent.
            When None and ctx has driver_id set, the /escalate Lambda
            can resolve the name from its own CMS lookup — but passing
            it through saves that round-trip.

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
    }
    if summary is not None:
        payload["summary"] = summary
    if dispatch_roadside is not None:
        payload["dispatchRoadside"] = dispatch_roadside
    if ctx.triage_decision_id:
        # Let the Lambda enrich from vsa-prod-triage-decisions.
        payload["triageSessionId"] = ctx.triage_decision_id
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
    """Build the escalate tool bound to a session context.

    The returned function is decorated with @tool so Strands can discover
    it. Only the four narrative args (`severity`, `reason`, `summary`,
    `dispatch_roadside`) are LLM-visible — tenant, jwt, vin, vehicleId,
    driverId, and sessionId are closure-captured from ctx/config.

    Note: Nova Sonic v2 doesn't reliably invoke registered tools (see
    Strands #2021), so in the bidi voice path `bidi_app.py` calls
    `call_escalate_endpoint` directly. This @tool wrapper is what the
    text-mode supervisor (`supervisor.py` / `agentcore_app.py`) uses.
    """

    @tool
    def escalate_to_human(
        severity: str,
        reason: str,
        summary: str = "",
        dispatch_roadside: bool = True,
    ) -> dict[str, Any]:
        """Hand the driver off to a human fleet-support agent via Amazon
        Connect chat. Use this when:

          - The triage classifier returned P0 (always auto-call, do NOT
            ask the driver; P0 is a safety-critical handoff that must
            not wait for confirmation).
          - The triage classifier returned P1 and the driver explicitly
            confirmed they want to be connected (yes / please / connect
            me / etc.).
          - The driver directly asks to speak to a human.

        This tool creates an Amazon Connect chat contact, returns the
        participant credentials the iOS client uses to join the chat,
        and (for P0 by default) queues roadside assistance dispatch.

        Args:
            severity: The triage classification driving the escalation.
                Must be one of "P0", "P1", "P2", "P3". Use the value
                returned by the most recent triage call; do not
                downgrade it.
            reason: One short phrase describing why we're escalating,
                in the driver's own words when possible (e.g. "engine
                overheating", "ABS warning light and brake pedal feels
                soft"). Flows to the agent's incoming-chat view.
            summary: Optional 1-3 sentence summary of the voice
                conversation so far. Helps the agent pick up context
                without replaying the audio. Defaults to empty.
            dispatch_roadside: Whether to dispatch roadside assistance
                in addition to the human handoff. Only honored when
                severity == "P0"; ignored otherwise. Defaults to True
                so P0 escalations both connect-and-dispatch.

        Returns:
            A dict with:
              contactId: Connect contact id for the handoff.
              participantId, participantToken, connectionExpiry: creds
                for the iOS client to join the chat WebSocket.
              severity: echoed back (authoritative from server).
              rsaDispatched: bool — true iff roadside was queued.
              rsaActionId: action-queue row id if rsaDispatched.
              summary: short narrative you can use in your spoken reply
                (e.g. "I've connected you to Kevin in fleet support...").
        """
        return call_escalate_endpoint(
            ctx,
            config,
            severity=severity,
            reason=reason,
            summary=summary or None,
            dispatch_roadside=dispatch_roadside,
            # The @tool path has no way to know displayName; the caller
            # can plumb it through ctx if needed. Leave it to the
            # /escalate Lambda's CMS lookup.
            display_name=None,
        )

    return escalate_to_human


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
            base += " Roadside assistance is also on its way."
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
