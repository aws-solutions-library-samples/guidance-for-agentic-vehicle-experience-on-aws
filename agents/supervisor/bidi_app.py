"""AgentCore WebSocket entrypoint for the voice-capable VSA supervisor.

Phase 3 step 3. Pairs with agentcore_app.py (HTTP text-mode runtime):
this entrypoint speaks WebSocket and runs a Strands BidiAgent with
Nova Sonic v2 for bidirectional audio.

B1b REFACTOR NOTE (2026-04-29): the event loop was previously two
manual asyncio tasks calling agent.send()/agent.receive(). That didn't
produce any output events from Nova Sonic v2 even though audio was
ingested cleanly (150 speech tokens per clip, 0 output tokens). The
AWS reference sample at awslabs/agentcore-samples 06-bi-directional-
streaming/02-strands-ws uses BidiAgent.run(inputs, outputs) instead —
Strands' built-in async pump that matches the library's internal
assumptions. This file now mirrors that pattern while keeping the
AgentCore @app.websocket shell and the session handshake intact.

Deploy with a SEPARATE AgentCore runtime (vsa_supervisor_bidi), leaving
the existing text runtime intact. See agents/supervisor/README.md.

Invocation contract (client -> server):

    Connection URL:
        wss://bedrock-agentcore.us-east-1.amazonaws.com/runtimes/<arn>/ws
        ?X-Amzn-Bedrock-AgentCore-Runtime-Custom-Tenant-Id=michelin
        &X-Amzn-Bedrock-AgentCore-Runtime-Custom-Vin=1FMCU0F70MUA12345
        &X-Amzn-Bedrock-AgentCore-Runtime-Session-Id=<sid>

    Custom header (preferred for JWT):
        X-Amzn-Bedrock-AgentCore-Runtime-Custom-User-Token: <Cognito IdToken>

    First WS message after accept() (fallback if query params absent):
        {"type": "session.start", "tenantId": "...", "vin": "..."}

    Subsequent client messages:
        {"type": "audio.chunk", "data": "<base64 PCM>", "sampleRate": 16000}
        {"type": "text.input", "text": "..."}
        {"type": "session.end"}

Server -> client:
        {"type": "audio.chunk", "data": "<base64 PCM>", "sampleRate": 24000}
        {"type": "transcript", "role": "user|assistant", "text": "...", "isFinal": bool}
        {"type": "tool.call", "name": "...", "input": {...}}
        {"type": "classification", "value": "P0|P1|P2|P3"}
        {"type": "interruption"}
        {"type": "escalation", "status": "initiated|failed", "severity": "P0",
         "contactId": "...", "participantId": "...", "participantToken": "...",
         "connectionExpiry": "<iso8601>", "rsaDispatched": bool,
         "rsaActionId": "<uuid|null>"}
        {"type": "session.ended", "reason": "..."}
        {"type": "error", "message": "..."}

Close codes:
    1000 normal, 1008 policy (validation), 1011 server error.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
import uuid
from typing import Any

from bedrock_agentcore.runtime import BedrockAgentCoreApp
from bedrock_agentcore.runtime.context import RequestContext
from strands.experimental.bidi.agent import BidiAgent
from strands.experimental.bidi.models import nova_sonic as _nova_sonic_mod
from strands.experimental.bidi.models.nova_sonic import (
    BidiNovaSonicModel,
    NOVA_SONIC_V2_MODEL_ID,
)

from agents.supervisor.config import Config, build_persona_loader_config
from agents.supervisor.session import SessionContext
from agents.supervisor.supervisor import prompt_hash, build_system_prompt
from agents.supervisor.voice_prompt import VOICE_SYSTEM_PROMPT_TEMPLATE, render_voice_prompt
from agents.supervisor.dtc_response_catalog import lookup_dtc_response as _dtc_lookup_response
from agents.supervisor.triage_keywords import (
    EmergencyCategory,
    match_emergency_keywords,
    parse_confirmation_response,
)
from agents.supervisor.tools import (
    PERSONA_TOOL_FACTORIES,
    make_book_tool,
    make_escalate_tool,
    make_knowledge_tool,
    make_parts_lookup_tool,
    make_retrieve_tool,
    make_service_network_tool,
    make_triage_tool,
    make_vehicle_identity_tool,
)
from agents.supervisor.tools.triage import call_triage_endpoint
from agents.supervisor.tools.book import call_book_endpoint
from agents.supervisor.tools.escalate import call_escalate_endpoint, EscalateError
from agents.supervisor import persona_loader as _persona_loader


# ---------------------------------------------------------------------------
# Monkey-patch: backport strands-agents PR #2188 to the installed 1.37.0 wheel.
# ---------------------------------------------------------------------------
#
# PR #2188 (merged 2026-04-24 on strands-agents/sdk-python `main`) fixes a
# bug where BidiNovaSonicModel hardcoded `"interactive": True` on the
# `contentStart` event for the system prompt and for historical messages.
# Nova Sonic v2 interprets `interactive: True` on TEXT content as a *live*
# cross-modal text input during a voice session — which makes it wait for
# user action instead of generating. Symptom: audio ingested cleanly,
# `inputTokens` climbs, `outputTokens` stays at 0 forever.
#
# The fix is not yet on PyPI (latest release is 1.37.0, the buggy one).
# Pulling strands-agents from git tip works locally but AgentCore's uv-based
# CodeBuild cross-compile refuses source distributions, so we can't pin the
# git SHA in requirements.txt for the deploy. Instead we patch the two
# affected methods here, at the top of the entrypoint, before BidiAgent
# is instantiated. Safe to remove once a release post-1.37.0 ships with #2188.
#
# The patch is idempotent — if the installed code already has the fixed
# signature (post-release), we no-op.
def _apply_nova_sonic_pr2188_patch() -> None:
    import inspect
    sig = inspect.signature(_nova_sonic_mod.BidiNovaSonicModel._get_text_content_start_event)
    if "interactive" in sig.parameters:
        # Installed Strands already has the fix.
        return

    import json  # local import — keep top of file tidy
    import uuid

    def _fixed_text_content_start_event(
        self: BidiNovaSonicModel,
        content_name: str,
        role: str = "USER",
        interactive: bool = True,
    ) -> str:
        return json.dumps(
            {
                "event": {
                    "contentStart": {
                        "promptName": self._connection_id,
                        "contentName": content_name,
                        "type": "TEXT",
                        "role": role,
                        "interactive": interactive,
                        "textInputConfiguration": _nova_sonic_mod.NOVA_TEXT_CONFIG,
                    }
                }
            }
        )

    def _fixed_system_prompt_events(
        self: BidiNovaSonicModel, system_prompt: str | None
    ) -> list[str]:
        content_name = str(uuid.uuid4())
        return [
            self._get_text_content_start_event(content_name, "SYSTEM", interactive=False),
            self._get_text_input_event(content_name, system_prompt or ""),
            self._get_content_end_event(content_name),
        ]

    def _fixed_message_history_events(
        self: BidiNovaSonicModel, messages
    ) -> list[str]:
        # Minimal port of #2188's history-path change: non-interactive flag
        # on each message's contentStart. Skips the 50KB/200KB truncation
        # and user-first enforcement because our bidi_app never passes
        # `messages=` (BidiAgent starts with empty history). If we ever
        # do, those safeguards become necessary — bump the Strands pin
        # to pick them up via PR #2188 directly.
        events: list[str] = []
        for message in messages:
            role = message["role"].upper()
            content_blocks = message.get("content", [])
            text_parts = [b["text"] for b in content_blocks if "text" in b]
            if not text_parts:
                continue
            combined_text = "\n".join(text_parts)
            content_name = str(uuid.uuid4())
            events.extend(
                [
                    self._get_text_content_start_event(content_name, role, interactive=False),
                    self._get_text_input_event(content_name, combined_text),
                    self._get_content_end_event(content_name),
                ]
            )
        return events

    _nova_sonic_mod.BidiNovaSonicModel._get_text_content_start_event = _fixed_text_content_start_event
    _nova_sonic_mod.BidiNovaSonicModel._get_system_prompt_events = _fixed_system_prompt_events
    _nova_sonic_mod.BidiNovaSonicModel._get_message_history_events = _fixed_message_history_events


_apply_nova_sonic_pr2188_patch()

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
# Turn up Strands bidi logging so we can see the Nova Sonic event protocol
# in CloudWatch. This is how we diagnose "audio went in but nothing came
# out" — debug-level logs reveal contentStart / contentEnd / audioInput frames.
logging.getLogger("strands.experimental.bidi").setLevel(logging.DEBUG)
logging.getLogger("strands.experimental.bidi.models.nova_sonic").setLevel(logging.DEBUG)

app = BedrockAgentCoreApp()


# Header / query param names we accept for threading user context.
USER_TOKEN_HEADER = "x-amzn-bedrock-agentcore-runtime-custom-user-token"
TENANT_QUERY_PARAM = "x-amzn-bedrock-agentcore-runtime-custom-tenant-id"
VIN_QUERY_PARAM = "x-amzn-bedrock-agentcore-runtime-custom-vin"
PERSONA_ID_HEADER = "x-amzn-bedrock-agentcore-runtime-custom-persona-id"
CUSTOMER_ID_HEADER = "x-amzn-bedrock-agentcore-runtime-custom-customer-id"
# Optional CMS-side identifiers. Both are side-channel to the VIN so the
# backend can write to cms-prod-storage-service-history with the right
# primary key (vehicleId) and link bookings to a specific driver record
# without a VIN→vehicleId / Cognito-email→driver lookup. The iOS client
# populates these from VSAConfig.demoVehicleId/demoDriverId.
#
# We accept both casings because AgentCore's header normalization between
# the camelCase wire header (VehicleId) and the lowercased delivered key
# is ambiguous — we've observed `vehicleid`, `vehicle-id`, and
# `vehicle_id` in different runtime contexts. Belt and suspenders.
VEHICLE_ID_QUERY_PARAMS = (
    "x-amzn-bedrock-agentcore-runtime-custom-vehicleid",
    "x-amzn-bedrock-agentcore-runtime-custom-vehicle-id",
    "x-amzn-bedrock-agentcore-runtime-custom-vehicle_id",
)
DRIVER_ID_QUERY_PARAMS = (
    "x-amzn-bedrock-agentcore-runtime-custom-driverid",
    "x-amzn-bedrock-agentcore-runtime-custom-driver-id",
    "x-amzn-bedrock-agentcore-runtime-custom-driver_id",
)
# Optional fresh telemetry coordinates pushed by the iOS client at
# connect time (the same Redis-backed coords the iOS Vehicle tab map
# uses, cached on session.liveState). When these are present we skip
# the backend's `/vehicles/{id}/live-state` HTTP roundtrip during
# session setup — saves ~150-300ms of latency before Nova starts
# listening. Casing is normalized the same way as the other
# camelCase headers (Latitude / Longitude on the wire).
LATITUDE_QUERY_PARAMS = (
    "x-amzn-bedrock-agentcore-runtime-custom-latitude",
)
LONGITUDE_QUERY_PARAMS = (
    "x-amzn-bedrock-agentcore-runtime-custom-longitude",
)

# Nova Sonic audio config. Matches common mobile mic rates.
INPUT_SAMPLE_RATE = 16000
OUTPUT_SAMPLE_RATE = 24000

# Default voice. Per VSA_TENANT_CONFIG.md Cascadia example: "matthew".
# Parameterized here so a future per-session override is a single change.
DEFAULT_VOICE = "matthew"


# ---------------------------------------------------------------------------
# Affirmation detection
# ---------------------------------------------------------------------------
#
# Used by the auto-book branch in `_run_session_loop` to decide whether the
# user's transcript is a yes-style confirmation in response to a service-
# center offer. The original implementation used a small set of literal
# strings ("yes", "yes.", "yeah", ...) — but Nova's STT routinely emits
# transcripts like "yes. yes." (when the demo audio plays a "yes" clip
# and the speaker echo is picked up as a second utterance) or
# "yes please" with extra punctuation. Those near-misses fell through to
# the auto-triage branch, which injected a 305-char system note and
# blocked Nova's book call for ~1m 47s in observed sessions
# (2026-05-20 13:00:32 → 13:02:19 was traced to exactly this).
#
# This more forgiving check tokenises the cleaned text and accepts
# anything composed entirely of single-word affirmative tokens, plus
# a small list of multi-word phrases. False positives don't matter here
# because the only thing this gates is "skip auto-triage and let Nova's
# prompt rule fire book directly" — Nova still has to confirm the
# booking on its own.
_AFFIRMATIVE_WORDS: frozenset[str] = frozenset({
    "yes", "yeah", "yep", "yup", "sure", "please", "ok", "okay",
})
_AFFIRMATIVE_PHRASES: frozenset[str] = frozenset({
    "go ahead", "do it", "book it", "schedule it", "yes please",
    "lets do it", "let us do it", "sounds good", "sure thing",
})


def _is_affirmation(text: str) -> bool:
    """True if `text` reads as a plain yes-style confirmation.

    Examples that match:
      "yes", "Yes.", "yes please", "yes. yes.", "Yeah sure", "go ahead",
      "Okay, do it"
    Examples that don't:
      "no thanks", "yes but tomorrow", "yes I have a question",
      anything > 30 chars (probably a real follow-up sentence).
    """
    if not text:
        return False
    if len(text) > 30:
        # Long utterances are real follow-ups, not pure confirmations.
        return False
    # Strip punctuation, collapse whitespace, lowercase.
    cleaned = re.sub(r"[^\w\s]", " ", text.lower())
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if not cleaned:
        return False
    if cleaned in _AFFIRMATIVE_PHRASES:
        return True
    return all(word in _AFFIRMATIVE_WORDS for word in cleaned.split())

# Priming text sent as a text.input to Nova Sonic v2 at session start, BEFORE
# any audio flows through. Without this priming, Nova Sonic v2 ingests audio
# (token counts climb as expected) but never triggers inference — outputTokens
# stays at 0 indefinitely. This is a Nova Sonic v2 interaction-pattern
# requirement observed in an internal Slack thread and confirmed by the
# smoke-test in scripts/smoke-test-voice.py with --prompt-first.
#
# The priming message is CRITICAL but we want Nova Sonic's response to be
# as short as possible, because user audio ingestion is blocked until the
# priming turn completes. Long priming replies = driver has to wait.
#
# Strategy: explicitly instruct Nova Sonic to reply with a single short word
# like "ready" and then stop. Nova Sonic v2 is compliant enough with
# explicit response-format instructions (unlike its weakness with negative
# constraints).
#
# The client-side VoiceSessionViewModel suppresses display/audio of the
# priming response, so the user never hears it — but they do feel the
# ingestion delay.
PRIMING_TEXT = ""  # Empty by design: Nova Sonic v2 accepts raw user audio directly, no session warm-up turn needed. Historic non-empty values produced duplicate greeting turns.

# How long after `bidi_response_start` we wait before assuming the
# assistant's turn ended, when Nova Sonic v2 failed to emit a proper
# is_final=true assistant transcript. The watchdog force-clears the
# injection queue's turn_in_flight flag so DEFERRED P2/P3 triage
# guidance can dispatch. Set just above a typical Nova Sonic reply
# length (~4-5s of audio) with a little headroom so normal turns
# clear via the real is_final path and only stuck turns hit the
# watchdog. Paired with AgentInjectionQueue._MAX_DEFER_SECONDS (8s),
# this means a DEFERRED item flushes within ~8s even if Nova Sonic
# never signals turn completion.
TURN_WATCHDOG_SECONDS = 3.0

# Sentinel used by the input callable to signal end-of-stream to
# BidiAgent.run's inputs_task. Since BidiAgent.run loops forever
# consuming from input(), we need a way to bail out cleanly when the
# client sends session.end or the socket disconnects.
class _SessionEnd(Exception):
    """Raised inside the input callable to terminate BidiAgent.run."""


@app.websocket
async def websocket_handler(websocket, context: RequestContext) -> None:
    """Voice session entrypoint.

    Flow:
      1. Accept WS and resolve tenantId / vin / jwt from query params or
         a session.start handshake.
      2. Build BidiNovaSonicModel + BidiAgent with our three tools.
      3. Hand the WS over to BidiAgent.run with an input callable that
         reads client messages and an output callable that writes agent
         events to the client. BidiAgent.run drives the event pump.
      4. Emit classification mirror events when ctx.triage_classification
         changes (piggy-backed on the output callable — it's the only
         thing running on every agent event).
    """
    await websocket.accept()
    session_id = context.session_id or _fallback_session_id()
    log.info("bidi: accept session=%s", session_id)

    try:
        ctx = await _establish_session(websocket, context, session_id)
    except InvocationError as e:
        log.warning("bidi: session rejected session=%s reason=%s", session_id, e)
        await _send_error(websocket, str(e))
        await _safe_close(websocket, code=1008, reason=str(e)[:100])
        return

    config = Config.from_env()
    try:
        model = BidiNovaSonicModel(
            model_id=NOVA_SONIC_V2_MODEL_ID,
            provider_config={
                "audio": {
                    "input_rate": INPUT_SAMPLE_RATE,
                    "output_rate": OUTPUT_SAMPLE_RATE,
                    "voice": DEFAULT_VOICE,
                },
                "inference": {
                    # max_tokens back to 4096: capping at 120 caused Nova
                    # Sonic to truncate its priming turn mid-sentence and
                    # restart it in a loop, which prevented the session
                    # from ever transitioning to a user-speakable state.
                    # Enforce brevity via prompt discipline instead. If
                    # prompt discipline proves insufficient, consider
                    # post-processing the assistant text stream to cut
                    # at a sentence boundary past N words.
                    "max_tokens": config.supervisor_max_tokens,
                    "temperature": 0.7,
                },
                # v2-only: enable server-side VAD so Nova Sonic auto-detects
                # end-of-turn from silence. Nova Sonic handles VAD natively —
                # no explicit contentEnd required mid-session.
                #
                # `endpointingSensitivity` was MEDIUM (1.75s pause) — Nova
                # Sonic was interrupting drivers who paused mid-thought
                # (e.g., "my temperature gauge... spiked to the red" with
                # a breath in the middle). Switched to LOW (~2.0s pause)
                # on 2026-05-07 per the Nova Sonic team's tuning guidance
                # for thoughtful / problem-solving conversations — fleet
                # triage fits that category better than command-and-control.
                # Cost: marginal latency increase on end-of-turn detection.
                "turn_detection": {
                    "endpointingSensitivity": "LOW",
                },
            },
            client_config={"region": config.nova_sonic_region},
        )
    except Exception as e:  # noqa: BLE001
        log.exception("bidi: failed to initialize Nova Sonic v2 model")
        await _send_error(websocket, f"Nova Sonic init failed: {e}")
        await _safe_close(websocket, code=1011, reason="model-init-failed")
        return

    # Best-effort CMS lookup so the voice prompt can refer to the driver
    # by name and the vehicle by make/model instead of speaking the VIN.
    # All failures swallowed — prompt still works with just VIN.
    vehicle_ctx = _fetch_vehicle_context(ctx.vehicle_id) if ctx.vehicle_id else {}
    # Overlay fresh live-state coordinates from Redis on top of the
    # DDB-backed lat/lng. The DDB row can lag by days when the vehicle
    # is actively connecting, so the driver sees one location on the
    # iOS map (Redis-backed) while Nova computes find_service_center
    # distance from a stale DDB row — making the same service center
    # appear to be hundreds of miles closer or farther than it really
    # is. Matching the iOS Vehicle tab's source-of-truth keeps voice
    # and visual consistent.
    #
    # Two paths:
    #   1. Client pushed Latitude / Longitude headers (the iOS app reads
    #      them off session.liveState before connecting). Use those
    #      directly — same data the iOS map shows, zero extra HTTP.
    #   2. Client didn't supply coords (older builds, JSON fallback path,
    #      session.liveState wasn't loaded yet). Fall back to the
    #      live-state HTTP endpoint which does the same Redis-first
    #      lookup server-side.
    if ctx.client_latitude is not None and ctx.client_longitude is not None:
        vehicle_ctx["latitude"] = ctx.client_latitude
        vehicle_ctx["longitude"] = ctx.client_longitude
        log.info(
            "bidi: using client-supplied location lat=%s lng=%s",
            ctx.client_latitude, ctx.client_longitude,
        )
    elif ctx.vehicle_id:
        live_loc = _fetch_live_vehicle_location(
            ctx.vehicle_id, ctx.jwt, config.api_base
        )
        if live_loc.get("latitude") is not None:
            vehicle_ctx["latitude"] = live_loc["latitude"]
        if live_loc.get("longitude") is not None:
            vehicle_ctx["longitude"] = live_loc["longitude"]
    # Mirror the resolved coords back to ctx so tools (find_service_center
    # in particular) read them via the closure instead of relying on
    # Nova to forward lat/lng correctly. Observed Nova omitting the
    # coords entirely on some calls, which produced "0 nearby" replies
    # against centers that were obviously close to the driver.
    if vehicle_ctx.get("latitude") is not None:
        ctx.client_latitude = vehicle_ctx["latitude"]
    if vehicle_ctx.get("longitude") is not None:
        ctx.client_longitude = vehicle_ctx["longitude"]
    driver_ctx = _fetch_driver_context(ctx.driver_id) if ctx.driver_id else {}
    # Resolve the tenant's persona segment (fleet / oem / rental). Drives
    # the find_service_center filter so OEM-branded apps return only
    # brand-matching dealers, while fleet apps see the broader mix.
    ctx.tenant_segment = _fetch_tenant_segment(ctx.tenant_id)
    # Pre-load CRITICAL/HIGH active DTCs and embed them in the prompt
    # so Nova Sonic can produce a criticality-aware FIRST response
    # (e.g. "Samantha, this is critical — pull over now") without
    # waiting for the classifier-then-inject roundtrip (~30s latency
    # observed on 2026-05-08). Classifier still runs + escalates; the
    # DTC list just primes Nova's initial narration.
    active_dtcs = _fetch_active_dtcs(ctx.vehicle_id) if ctx.vehicle_id else []
    log.info(
        "bidi: context resolved vehicle=%s %s %s driver=%s %s active_dtcs=%d "
        "location=(%s,%s) segment=%s",
        vehicle_ctx.get("year"), vehicle_ctx.get("make"), vehicle_ctx.get("model"),
        driver_ctx.get("firstName"), driver_ctx.get("lastName"),
        len(active_dtcs),
        vehicle_ctx.get("latitude"), vehicle_ctx.get("longitude"),
        ctx.tenant_segment,
    )

    system_prompt = render_voice_prompt(
        # Hardcoded for the demo; production would read the tenant's
        # `ui.display_name` (or similar) from vsa-prod-tenant-config keyed
        # by ctx.tenant_id. Not plumbed through yet — affects voice prompt
        # wording only, so low-risk to hardcode for now.
        tenant_display="Cascadia Fleet Services",
        tenant_id=ctx.tenant_id,
        session_id=ctx.session_id,
        vin=ctx.vin,
        vehicle_make=vehicle_ctx.get("make"),
        vehicle_model=vehicle_ctx.get("model"),
        vehicle_year=vehicle_ctx.get("year"),
        driver_first_name=driver_ctx.get("firstName"),
        driver_last_name=driver_ctx.get("lastName"),
        active_dtcs=active_dtcs,
        vehicle_latitude=vehicle_ctx.get("latitude"),
        vehicle_longitude=vehicle_ctx.get("longitude"),
        tenant_segment=ctx.tenant_segment,
    )

    # Stash driver display name on ctx for escalation handover
    ctx.driver_first_name = driver_ctx.get("firstName")
    ctx.driver_display_name = " ".join(
        filter(None, [driver_ctx.get("firstName"), driver_ctx.get("lastName")])
    ) or None

    # --- Persona-loader: populate AgentCore Memory before agent.run() ---
    # Runs once per session-init; failure is non-fatal (spec § "Failure mode").
    _loader_start_ms = int(__import__("time").time() * 1000)
    try:
        loader_config = build_persona_loader_config()
        _snapshot = await _persona_loader.load_persona(
            persona_id=ctx.persona_id,
            vehicle_id=ctx.vin,
            customer_id=ctx.customer_id,
            config=loader_config,
        )
        _elapsed_ms = int(__import__("time").time() * 1000) - _loader_start_ms
        log.info(
            "persona-loader: loaded %d products in %dms session=%s persona=%s",
            len(_snapshot.data_sources), _elapsed_ms, ctx.session_id, ctx.persona_id,
        )
    except Exception:
        log.warning(
            "persona-loader failed — continuing with degraded session session=%s",
            ctx.session_id, exc_info=True,
        )

    # Build persona-specific tool list from PERSONA_TOOL_FACTORIES.
    # Fall back to a minimal set if the persona_id is unknown.
    _tool_factories = PERSONA_TOOL_FACTORIES.get(ctx.persona_id, [
        make_triage_tool, make_retrieve_tool, make_book_tool,
        make_escalate_tool, make_service_network_tool,
    ])
    _persona_tools = [f(ctx, config) if f.__code__.co_varnames[:2] == ("session", "config") or "config" in f.__code__.co_varnames else f(ctx) for f in _tool_factories]

    # Persona-aware system prompt from supervisor.build_system_prompt.
    # Falls back to the voice-adapted render_voice_prompt for voice personas
    # (fleet_driver / consumer / retail_advisor) which have a rich voice
    # prompt; build_system_prompt returns a text-mode prompt for new personas.
    try:
        _persona_system_prompt = build_system_prompt(ctx.persona_id, ctx)
    except Exception:
        _persona_system_prompt = system_prompt  # fallback to voice prompt

    # Inject the server-resolved vehicle identity into the active prompt.
    # build_system_prompt's persona body only carries the VIN (not make/
    # model/year), so when lookup_vehicle_identity returns "not loaded"
    # Nova fabricates a vehicle (observed: "2020 Ford Transit Van" for a
    # 2024 F-250). vehicle_ctx is already resolved above; pin it here.
    _veh_phrase = " ".join(
        str(x) for x in (vehicle_ctx.get("year"), vehicle_ctx.get("make"), vehicle_ctx.get("model")) if x
    )
    if _veh_phrase:
        _persona_system_prompt += (
            f"\n\nThe driver's vehicle is a {_veh_phrase} (VIN {ctx.vin}). "
            "Use these EXACT vehicle details in every response. NEVER guess, "
            "change, or invent the year, make, or model — even if a tool returns "
            "no vehicle details, rely on the vehicle stated here."
        )

    agent = BidiAgent(
        model=model,
        tools=_persona_tools,
        # The voice-adapted prompt from render_voice_prompt() — this is the
        # one that contains the "temperature gauge spiked" one-shot and the
        # vehicle-scope guardrail that keeps Nova Sonic from drifting into
        # medical/general-knowledge responses. Previously temporarily set
        # to None for a diagnostic ("is our prompt the blocker?") and never
        # restored, which is why the assistant was answering off-topic when
        # drivers described vehicle symptoms. Restored 2026-05-07.
        system_prompt=_persona_system_prompt,
        name="vsa-supervisor-bidi",
    )

    log.info(
        "bidi: start session=%s tenant=%s vin=%s vehicle_id=%s driver_id=%s model=%s prompt_hash=%s persona=%s",
        ctx.session_id,
        ctx.tenant_id,
        ctx.vin,
        ctx.vehicle_id,
        ctx.driver_id,
        NOVA_SONIC_V2_MODEL_ID,
        prompt_hash(_persona_system_prompt),
        ctx.persona_id,
    )

    # Pre-warm the classifier so the driver's first utterance doesn't
    # pay the Lambda cold-start penalty (~2s observed 2026-05-08).
    # Cold-starts happen whenever the Lambda hasn't been hit in ~10
    # minutes. We fire a benign classification call as a background
    # task; by the time the driver finishes speaking (~5s later) the
    # Lambda is warm and the real classifier call returns in ~120ms.
    # Throws away the result — we just want the container alive.
    asyncio.create_task(
        _warm_classifier(ctx, config),
        name=f"classifier-prewarm[{ctx.session_id}]",
    )

    # Per-session counters + classification mirror state. Captured by
    # the callables below via closure.
    state = {
        "chunk_count": 0,
        "total_audio_bytes": 0,
        "event_count": 0,
        "type_counts": {},
        "last_classification": None,
        # Snapshot of active CRITICAL/HIGH DTCs captured at session
        # start + embedded in the voice prompt (see `active_dtcs` above
        # in `_fetch_active_dtcs`). The auto-triage code path reads
        # this to decide whether Nova Sonic was already primed to
        # respond with the P0 safety script on its own, which is the
        # signal to skip the otherwise-~30s text injection.
        "active_dtcs_for_prompt": active_dtcs,
        # Set to True after the first call to input_callable returns the
        # priming text.input. See PRIMING_TEXT for why this exists.
        "primed": False,
        # Set to True after we've auto-triaged on the first final user
        # transcript. One-shot per session — Nova Sonic v2 doesn't
        # reliably invoke registered tools (Strands #2021), so we call
        # triage() directly when we hear the user's final utterance.
        "auto_triage_fired": False,
        # Driver-confirmed escalation state.
        # Populated when the classifier returned a non-urgent tier but
        # the driver's utterance matched an emergency keyword (see
        # triage_keywords.match_emergency_keywords). While these are set,
        # the next final user transcript is interpreted as a yes/no/
        # unclear answer to the confirmation question — not as a new
        # triage-triggering utterance. Cleared once the confirmation is
        # resolved (confirm, stand-down, or unclear-escalate).
        "awaiting_confirmation_category": None,   # EmergencyCategory or None
        "awaiting_confirmation_keyword": None,    # str: original utterance that triggered
        "classifier_tier_before_confirmation": None,  # str: "P2" | "P3" (what the classifier said)
        "service_center_offered": None,  # dict: last find_service_center result (for booking on "yes")
        "vehicle_ctx": vehicle_ctx,  # make, latitude, longitude for server-side service center lookup
    }

    input_callable = _make_input_callable(websocket, state, ctx, config)
    output_callable = _make_output_callable(websocket, ctx, state, config, agent)
    # Stash the output callable on state so input handlers can synthesize
    # transcript events when needed. Specifically: text.input messages
    # don't go through Nova's STT, so they don't naturally produce a
    # `bidi_transcript_stream role=user is_final=true` event. Without
    # such an event, auto-triage and auto-book branches in
    # output_callable never see the user's input as a "user turn",
    # which means text-mode driver utterances skip the classifier and
    # the auto-book server-side helper. Re-emitting a synthetic
    # transcript via this stashed callable closes that gap so text
    # and voice paths behave identically downstream. Demo flows
    # (DTC ⓘ button + "yes" demo clip) rely on this for the auto-book
    # path to fire reliably even when Nova's STT silently drops or
    # delays the audio transcript.
    state["output_callable"] = output_callable

    # Turn-aware injection queue. Prevents server-originated agent.send()
    # calls (e.g. triage auto-injection) from interrupting Nova Sonic
    # mid-response for non-urgent classifications. See the queue class
    # docstring for the full rationale.
    injection_queue = AgentInjectionQueue(agent, session_id=ctx.session_id)
    state["injection_queue"] = injection_queue
    injection_queue.start()

    # Guardrail observer (dry-run). Watches Nova Sonic's streaming
    # assistant transcript and logs ApplyGuardrail verdicts at sentence
    # boundaries. Takes no corrective action — we just want data on
    # which of our current drifts the policy would catch, and where it
    # might false-positive, before wiring enforcement. See GuardrailChecker
    # docstring for design notes.
    guardrail_checker = GuardrailChecker(session_id=ctx.session_id)
    state["guardrail_checker"] = guardrail_checker

    try:
        # BidiAgent.run drives start() -> pumps inputs via input_callable
        # into agent.send() and pumps agent.receive() events into every
        # output callable, then calls stop() on exit. We wrap with our
        # own try/except so we can report clean errors to the client.
        await agent.run(inputs=[input_callable], outputs=[output_callable])
    except _SessionEnd:
        # Expected path: client sent session.end or disconnected.
        log.info(
            "bidi: session ended cleanly session=%s chunks=%d events=%d type_counts=%s",
            ctx.session_id, state["chunk_count"], state["event_count"], state["type_counts"],
        )
    except Exception as e:  # noqa: BLE001
        log.exception("bidi: session error session=%s", ctx.session_id)
        await _send_error(websocket, f"server-error: {type(e).__name__}")
        await _safe_close(websocket, code=1011, reason="server-error")
        return
    finally:
        # Drain the queue on every exit path (clean exit, _SessionEnd, or
        # unexpected exception). stop() is idempotent so `return` from the
        # error branch above still triggers this block before the function
        # actually returns.
        await injection_queue.stop()
        # Drain any in-flight guardrail tasks. Also idempotent and
        # exception-swallowing so teardown can't be blocked by a stuck
        # ApplyGuardrail call.
        await guardrail_checker.aclose()

    await _safe_send_json(websocket, {"type": "session.ended", "reason": "client-closed"})
    await _safe_close(websocket, code=1000, reason="done")


# ---------------------------------------------------------------------------
# Input / output callables — these are what BidiAgent.run pumps
# ---------------------------------------------------------------------------


def _make_input_callable(websocket, state: dict, ctx: SessionContext, config: Config):
    """Build the input callable passed to BidiAgent.run(inputs=[...]).

    Strands' BidiAgent.run calls this repeatedly; each call should return
    either a BidiInputEvent subclass OR a dict with a "type" field that
    Strands recognizes (bidi_text_input, bidi_audio_input, bidi_image_input).
    The sample passes dicts through directly — simpler and it's what
    Strands.BidiAgent.send reconstructs internally.

    We filter session.end (raises _SessionEnd to break the pump) and
    translate our wire-protocol audio.chunk / text.input into Strands
    events. Any other message types are logged and skipped.
    """
    async def input_callable() -> dict:
        # One-shot priming: Nova Sonic v2 needs a text.input before audio
        # will trigger generation. This fires exactly once on the first
        # call, before we start reading from the WebSocket. See PRIMING_TEXT.
        # If PRIMING_TEXT is empty, skip priming entirely (for diagnostics).
        if not state["primed"]:
            state["primed"] = True
            # Deliberate emptiness check, not a string-constant-as-boolean
            # defect: an empty PRIMING_TEXT is the supported way to disable
            # priming for diagnostics, and the else branch below logs exactly
            # that. See issues/2026-08-03-cvx-probe-critical-triage/report.md.
            # nosemgrep: no-strings-as-booleans
            if PRIMING_TEXT:
                log.info("input: sending priming text.input (%d chars)", len(PRIMING_TEXT))
                return {
                    "type": "bidi_text_input",
                    "text": PRIMING_TEXT,
                    "role": "user",
                }
            else:
                log.info("input: priming disabled (PRIMING_TEXT is empty)")

        while True:
            msg = await websocket.receive_json()
            if not isinstance(msg, dict):
                log.debug("input: ignoring non-dict message type=%s", type(msg))
                continue

            msg_type = msg.get("type")

            if msg_type == "audio.chunk":
                audio_b64 = msg.get("data")
                if not audio_b64:
                    continue

                # Server-side echo gate: drop audio while the assistant is
                # outputting audio. The flag is set in the output handler
                # when bidi_audio_stream events flow, cleared when the
                # assistant turn ends (is_final transcript or interruption).
                if state.get("assistant_speaking"):
                    continue

                sample_rate = int(msg.get("sampleRate") or INPUT_SAMPLE_RATE)

                state["chunk_count"] += 1
                state["total_audio_bytes"] += (len(audio_b64) * 3) // 4
                if state["chunk_count"] <= 3 or state["chunk_count"] % 10 == 0:
                    log.info(
                        "input: audio.chunk #%d sr=%d (total raw ~%d bytes)",
                        state["chunk_count"], sample_rate, state["total_audio_bytes"],
                    )

                # Return a dict in the shape Strands reconstructs from.
                return {
                    "type": "bidi_audio_input",
                    "audio": audio_b64,
                    "format": "pcm",
                    "sample_rate": sample_rate,
                    "channels": 1,
                }

            if msg_type == "text.input":
                text = msg.get("text", "")
                if not text:
                    continue
                log.info("input: text %r", text[:100])
                # NOTE 2026-05-20: previously we mirrored text.input into
                # the OUTPUT path as a synthetic user-final transcript
                # so auto-triage would fire on text-mode input. That
                # caused auto-triage to dispatch a 451-char DEFERRED
                # injection in parallel with Nova's own text processing.
                # Two text inputs in rapid succession seems to put Nova
                # Sonic v2 into a degraded state where it produces zero
                # output. Removed for now — auto-triage stays
                # audio-only. Nova still sees the user's text via
                # bidi_text_input below and processes it natively.
                # If we want auto-triage on text inputs later, we'll
                # need to make sure the injection doesn't race against
                # Nova's native text response.
                #
                # SERVER-SIDE BOOK FALLBACK (added 2026-05-21 16:35
                # EDT): when text.input is an affirmation AND a
                # service center has been offered in this session,
                # schedule a 5s fallback that calls _auto_book_on_
                # affirmation server-side if Nova hasn't called
                # `book` itself by then. Why this is needed:
                #
                #   - The "yes-fleet" demo clip's audio is short
                #     (just "Yes."). Nova's STT often doesn't
                #     produce a final user transcript for it,
                #     which means the OUTPUT-side auto-book branch
                #     (gated on `event.role == "user"` and
                #     `is_final == true`) never fires.
                #   - Nova receives the parallel text.input
                #     "Yes." but treats it as another user turn
                #     overlapping the still-open audio user
                #     turn from the demo clip — net result is
                #     Nova accepting the tokens but never
                #     starting an assistant content stream.
                #   - With the server-side book IMMEDIATE
                #     injection that fires after `book` returns
                #     restored, all we need is for `book` to
                #     actually run. _auto_book_on_affirmation
                #     calls call_book_endpoint directly,
                #     bypassing Nova's tool-call path, then
                #     enqueues "Tell the driver: Done — you're
                #     booked..." via the IMMEDIATE injection
                #     queue. Nova reliably narrates IMMEDIATE
                #     injections (proven by the
                #     find_service_center IMMEDIATE narration
                #     working in the same session).
                #
                # We schedule with a 5s delay so Nova has a chance
                # to call `book` naturally first; if Nova does
                # call book, the tool_result handler clears
                # `service_center_offered` from state, and the
                # fallback below sees the cleared sentinel and
                # bails — preventing duplicate bookings.
                if (state.get("service_center_offered") is not None
                        and _is_affirmation(text)):
                    existing = state.get("auto_book_fallback_task")
                    if existing is not None and not existing.done():
                        existing.cancel()

                    async def _book_fallback(captured_text: str = text):
                        try:
                            await asyncio.sleep(5.0)
                            # Re-check sentinel: if Nova called book
                            # naturally, the tool_result handler
                            # already popped it. Skip the fallback.
                            if state.get("service_center_offered") is None:
                                log.info(
                                    "auto-book-fallback: skipped — Nova "
                                    "already called book session=%s",
                                    ctx.session_id,
                                )
                                return
                            log.info(
                                "auto-book-fallback: Nova didn't call "
                                "book within 5s of affirmation %r — "
                                "running server-side book session=%s",
                                captured_text[:40], ctx.session_id,
                            )
                            await _auto_book_on_affirmation(
                                websocket=websocket,
                                ctx=ctx,
                                config=config,
                                state=state,
                                user_text=captured_text,
                            )
                        except asyncio.CancelledError:
                            pass
                        except Exception as e:  # noqa: BLE001
                            log.exception(
                                "auto-book-fallback: failed session=%s: %s",
                                ctx.session_id, e,
                            )

                    state["auto_book_fallback_task"] = asyncio.create_task(
                        _book_fallback(),
                        name=f"auto-book-fallback[{ctx.session_id}]",
                    )

                # Forward the text input to Nova so Nova naturally
                # processes the user's turn and fires the appropriate
                # tool calls per prompt rules. We previously
                # suppressed forwarding when the input was a "yes"
                # affirmation after a service-center offer (so a
                # parallel server-side _auto_book_on_affirmation
                # could own the booking). That worked but produced a
                # silent confirmation: server-side wrote the
                # transcript card, but Nova never narrated "Done"
                # because there was no user turn to respond to.
                # Letting Nova see the text means Nova fires book
                # itself and speaks the confirmation naturally.
                # The synthetic transcript above still triggers
                # auto-triage so book's `triage_classification`
                # precondition is satisfied.
                return {
                    "type": "bidi_text_input",
                    "text": text,
                    "role": "user",
                }

            if msg_type == "session.end":
                log.info(
                    "input: client requested session.end (sent %d chunks, %d bytes)",
                    state["chunk_count"], state["total_audio_bytes"],
                )
                raise _SessionEnd()

            log.debug("input: ignoring type=%r", msg_type)

    return input_callable


def _make_output_callable(
    websocket,
    ctx: SessionContext,
    state: dict,
    config: Config,
    agent: BidiAgent,
):
    """Build the output callable passed to BidiAgent.run(outputs=[...]).

    Called once per event emitted by the agent. Projects each event into
    the wire format defined in the module docstring, logs interesting
    ones, and mirrors the ctx.triage_classification state change as
    a separate {"type":"classification",...} message.
    """
    async def output_callable(event: Any) -> None:
        state["event_count"] += 1
        event_type = event.get("type") if isinstance(event, dict) else None
        state["type_counts"][str(event_type)] = (
            state["type_counts"].get(str(event_type), 0) + 1
        )

        # Log first occurrence of each event type, plus every 20th event
        # overall, so we can see what's flowing without drowning in audio.
        if state["type_counts"][str(event_type)] == 1 or state["event_count"] % 20 == 0:
            preview = ""
            if isinstance(event, dict):
                non_audio = {
                    k: (v if k not in ("audio", "data") else f"<{len(v)} chars>")
                    for k, v in event.items()
                    if k != "delta"
                }
                preview = json.dumps(non_audio, default=str)[:200]
            log.info(
                "output: event #%d type=%r count=%d preview=%s",
                state["event_count"], event_type,
                state["type_counts"][str(event_type)], preview,
            )

        if event_type == "bidi_audio_stream":
            state["last_audio_output_at"] = time.time()
            state["assistant_speaking"] = True
            # Reset the turn watchdog on each audio chunk so DEFERRED
            # injections only fire 3s after Nova STOPS speaking.
            iq = state.get("injection_queue")
            if iq is not None:
                prev_wd = state.pop("turn_watchdog", None)
                if prev_wd is not None and not prev_wd.done():
                    prev_wd.cancel()

                async def _turn_watchdog_from_audio(iq_ref):
                    try:
                        await asyncio.sleep(TURN_WATCHDOG_SECONDS)
                        iq_ref.mark_response_complete()
                        log.info(
                            "turn-watchdog: force-cleared turn_in_flight "
                            "session=%s after %.1fs (no is_final from Nova)",
                            ctx.session_id, TURN_WATCHDOG_SECONDS,
                        )
                    except asyncio.CancelledError:
                        pass

                state["turn_watchdog"] = asyncio.create_task(_turn_watchdog_from_audio(iq))
            # Reset the echo-gate watchdog: clear assistant_speaking 1.5s
            # after the last audio chunk (speaker echo decays by then).
            prev_eg = state.pop("echo_gate_watchdog", None)
            if prev_eg is not None and not prev_eg.done():
                prev_eg.cancel()

            async def _echo_gate_clear():
                try:
                    await asyncio.sleep(1.5)
                    state["assistant_speaking"] = False
                except asyncio.CancelledError:
                    pass

            state["echo_gate_watchdog"] = asyncio.create_task(_echo_gate_clear())
            await _safe_send_json(
                websocket,
                {
                    "type": "audio.chunk",
                    "data": event.get("audio"),
                    "sampleRate": event.get("sample_rate"),
                    "format": event.get("format", "pcm"),
                },
            )
        elif event_type == "bidi_transcript_stream":
            await _safe_send_json(
                websocket,
                {
                    "type": "transcript",
                    "role": event.get("role"),
                    "text": event.get("text") or event.get("current_transcript", ""),
                    "isFinal": bool(event.get("is_final")),
                },
            )

            # Feed assistant transcript deltas into the guardrail checker.
            # The checker buffers internally and only calls ApplyGuardrail
            # at sentence boundaries, so this is cheap on the hot path.
            # Dry-run: verdicts are logged only, never acted on.
            if event.get("role") == "assistant":
                gc = state.get("guardrail_checker")
                if gc is not None:
                    delta = event.get("text") or ""
                    gc.feed_delta(delta)
                    if bool(event.get("is_final")):
                        gc.mark_turn_end()

            # Cancel any pending DEFERRED auto-triage injection as soon
            # as Nova produces her first assistant text chunk this turn.
            # The DEFERRED nudge is a fallback for the case where Nova
            # goes silent — if she's actually responding (text chunk
            # arrived), the nudge would land as a duplicate response.
            #
            # Why first chunk and not is_final=true: bidi_interruption
            # often fires BEFORE Nova's is_final transcript arrives, and
            # interruption triggers mark_response_complete which dispatches
            # the DEFERRED. By cancelling on the first chunk we beat the
            # interruption-driven dispatch. cancel_deferred() also sets
            # _block_deferred=True so any auto-triage task still racing
            # to submit gets dropped at submit() time too.
            #
            # Skip the priming-text chunks: when we kick off priming, Nova
            # echoes our own text back as an assistant transcript, and
            # we don't want THAT echo to burn the cancel.
            if (event.get("role") == "assistant"
                    and (event.get("text") or "").strip()
                    and not state.get("assistant_started_speaking")
                    and state.get("auto_triage_fired")):
                state["assistant_started_speaking"] = True
                iq = state.get("injection_queue")
                if iq is not None:
                    iq.cancel_deferred()

            # Mark the assistant turn as complete so any DEFERRED injections
            # (P2/P3 triage guidance) can flush. User-role finals and
            # non-final chunks don't change turn state.
            if event.get("role") == "assistant" and bool(event.get("is_final")):
                state["assistant_speaking"] = False
                # Reset the per-turn first-chunk flag so the cancel above
                # re-arms for the next turn.
                state["assistant_started_speaking"] = False
                iq = state.get("injection_queue")
                if iq is not None:
                    iq.mark_response_complete()
                # Real turn-end arrived — cancel the watchdog so it
                # doesn't log a spurious "force-cleared" message.
                wd = state.pop("turn_watchdog", None)
                if wd is not None and not wd.done():
                    wd.cancel()

            # Auto-triage hook: Nova Sonic v2 doesn't reliably invoke our
            # registered triage tool (Strands #2021). Work around it by
            # firing the triage call server-side when the user's final
            # transcript arrives, then inject the classification back into
            # the conversation as an authoritative note the model can
            # narrate from. One-shot per session to avoid re-triage loops.
            #
            # Filter out the priming text: Nova Sonic echoes our
            # text.input-fed priming back as a user-role transcript, and we
            # don't want that to burn our one-shot slot before the real
            # user has spoken. Comparing against PRIMING_TEXT exactly is
            # sufficient — the backend control the string so there's no
            # ambiguity.
            #
            # DRIVER-CONFIRMED ESCALATION GUARD: if a prior turn asked
            # the driver a yes/no emergency-confirmation question (see
            # triage_keywords + the divert inside _auto_triage_and_inject),
            # this user-final transcript is the answer. Intercept it
            # BEFORE any other logic so we don't re-run auto-triage,
            # re-fire the divert, or treat the answer as a new symptom.
            user_text = event.get("text") or event.get("current_transcript") or ""
            # New user final turn = fresh slate for the injection queue's
            # deferred-block flag (set when Nova calls tools earlier this
            # session). Lets DEFERRED submits work normally on this turn
            # unless Nova calls tools again, in which case cancel_deferred
            # will re-engage the block.
            if (
                event.get("role") == "user"
                and bool(event.get("is_final"))
                and user_text.strip() != PRIMING_TEXT.strip()
            ):
                iq = state.get("injection_queue")
                if iq is not None:
                    iq.clear_deferred_block()
            if (
                event.get("role") == "user"
                and bool(event.get("is_final"))
                and state.get("awaiting_confirmation_category") is not None
                and user_text.strip()
                and user_text.strip() != PRIMING_TEXT.strip()
            ):
                iq = state.get("injection_queue")
                asyncio.create_task(
                    _handle_confirmation_response(
                        websocket, ctx, config, user_text, iq, state
                    ),
                    name=f"confirm-response[{ctx.session_id}]",
                )
                # Don't fall through to the auto-triage block — the
                # confirmation handler owns this turn.
            elif (
                event.get("role") == "user"
                and bool(event.get("is_final"))
                and state.get("service_center_offered") is not None
                and _is_affirmation(user_text)
            ):
                # User confirmed booking. We rely on Nova's prompt rule
                # ("When the driver affirms after a service-center
                # offer, call book NOW") to fire the book tool. Nova
                # narrates the confirmation TTS naturally afterwards.
                #
                # 2026-05-20: previously we also dispatched a
                # server-side _auto_book_on_affirmation helper here.
                # Two problems:
                #   1. Duplicate book calls when both Nova and the
                #      helper fired (two distinct VSA-XXXX tickets
                #      for one "yes").
                #   2. When we suppressed forwarding to Nova to fix
                #      the duplicate, Nova had no user turn to
                #      respond to — server-side wrote the
                #      confirmation transcript, but Nova never
                #      narrated "Done" audibly. The IMMEDIATE
                #      injection ("Tell the driver: Done...") was
                #      either swallowed mid-flush or made Nova wait
                #      for non-existent user audio.
                # Letting Nova process the affirmation natively
                # restores the audible confirmation. We still pop
                # the sentinel here so a stray later "yes" doesn't
                # try to retrigger.
                state.pop("service_center_offered", None)
                log.info(
                    "auto-book: user confirmed (text=%r) — Nova handles "
                    "book + TTS natively per prompt rule", user_text[:60]
                )
            elif (
                not state["auto_triage_fired"]
                and event.get("role") == "user"
                and bool(event.get("is_final"))
                and ctx.vin
                and user_text.strip() != PRIMING_TEXT.strip()
            ):
                state["auto_triage_fired"] = True
                user_utterance = user_text
                log.info(
                    "auto-triage: firing for vin=%s on user transcript %r",
                    ctx.vin, user_utterance[:80],
                )
                # The final agent.send() inside _auto_triage_and_inject
                # now routes through the injection queue (see Task 4
                # changelog and AgentInjectionQueue docstring). Priority
                # is derived from the classifier result: P0/P1 are
                # IMMEDIATE, P2/P3 DEFERRED. The queue lives on state
                # and was started at session open.
                iq = state.get("injection_queue")
                asyncio.create_task(
                    _auto_triage_and_inject(
                        websocket, agent, ctx, config, user_utterance, iq, state
                    ),
                    name=f"auto-triage[{ctx.session_id}]",
                )
        elif event_type == "tool_use_stream":
            # Nova is actively calling a tool — cancel any pending DEFERRED
            # injection since Nova is handling the conversation itself.
            iq = state.get("injection_queue")
            if iq is not None:
                iq.cancel_deferred()
            # Clear echo gate only if audio hasn't played recently —
            # if Nova was just speaking, echo may still be in the mic.
            last_audio = state.get("last_audio_output_at", 0)
            if (time.time() - last_audio) > 1.5:
                state["assistant_speaking"] = False
            current_tool = event.get("current_tool_use") or {}
            tool_name = current_tool.get("name")
            tool_input = current_tool.get("input")
            tool_use_id = current_tool.get("toolUseId")
            # Track toolUseId→name so we can identify the tool in tool_result
            if tool_use_id and tool_name:
                state.setdefault("tool_use_names", {})[tool_use_id] = tool_name
            await _safe_send_json(
                websocket,
                {
                    "type": "tool.call",
                    "name": tool_name,
                    "input": tool_input,
                },
            )

        elif event_type == "tool_result":
            # Clear echo gate only if audio hasn't played recently.
            last_audio = state.get("last_audio_output_at", 0)
            if (time.time() - last_audio) > 1.5:
                state["assistant_speaking"] = False
            tool_result = event.get("tool_result") or {}
            tool_use_id = tool_result.get("toolUseId", "")
            tool_name = state.get("tool_use_names", {}).get(tool_use_id, "unknown")
            # Extract the JSON payload from the ToolResult content list
            result_data = {}
            for content_block in tool_result.get("content", []):
                if "json" in content_block:
                    result_data = content_block["json"]
                    break
                if "text" in content_block:
                    raw_text = content_block["text"]
                    try:
                        result_data = json.loads(raw_text)
                    except (json.JSONDecodeError, TypeError):
                        try:
                            import ast, re
                            # Strip Decimal('...') wrappers from Python repr
                            cleaned = re.sub(r"Decimal\('([^']+)'\)", r"\1", raw_text)
                            result_data = ast.literal_eval(cleaned)
                        except Exception:
                            result_data = {"result": raw_text}
                    break
            await _safe_send_json(
                websocket,
                {"type": "tool.result", "name": tool_name, "output": result_data},
            )
            # Emit escalation wire event so iOS Connect chat client connects
            if tool_name == "escalate_to_human" and result_data.get("contactId"):
                await _safe_send_json(
                    websocket,
                    {
                        "type": "escalation",
                        "status": "initiated",
                        "severity": result_data.get("severity", "P3"),
                        "contactId": result_data.get("contactId"),
                        "participantId": result_data.get("participantId"),
                        "participantToken": result_data.get("participantToken"),
                        "connectionExpiry": result_data.get("connectionExpiry"),
                        "rsaDispatched": result_data.get("rsaDispatched", False),
                        "rsaActionId": result_data.get("rsaActionId"),
                    },
                )
                log.info(
                    "escalation wire event sent: contact=%s severity=%s",
                    result_data.get("contactId"), result_data.get("severity"),
                )
            # Track when a service center was offered so we can auto-book on "yes"
            elif tool_name == "find_service_center" and result_data.get("found"):
                centers = result_data.get("centers", [])
                if centers:
                    state["service_center_offered"] = centers[0]
                    # Also stash on ctx so the book tool can use the
                    # actual offered center's name/address in its
                    # serviceCenter payload (instead of the fixture
                    # default in book.py's _build_payload).
                    ctx.last_offered_service_center = centers[0]
                    c = centers[0]
                    dist = c.get("distanceMiles")
                    dist_phrase = f" That's about {dist} miles from you." if dist else ""
                    narration = (
                        f"I found {c.get('name')} at {c.get('address')}.{dist_phrase}"
                        f" Would you like me to book that for you?"
                    )
                    log.info(
                        "find_service_center returned center=%s — restoring "
                        "IMMEDIATE narration injection",
                        c.get("name"),
                    )
                    # Restored 2026-05-21 16:33 EDT: yesterday's
                    # removal of this IMMEDIATE injection was
                    # premature. Without it, Nova frequently goes
                    # silent after find_service_center even though
                    # the prompt rules tell her to narrate. The
                    # injection was originally added because Nova
                    # Sonic v2 has a known weakness narrating after
                    # tool_use — the prompt isn't enough on its
                    # own. Tracked across multiple sessions today:
                    # post-removal, "yes"-affirmation flow times
                    # out at 55s; with the injection (per the
                    # 2026-05-20 16:32 / 16:47 sessions) the flow
                    # ran end-to-end. The double-narration bug we
                    # were trying to avoid is the lesser evil.
                    iq = state.get("injection_queue")
                    if iq is not None:
                        await iq.submit(
                            InjectionRequest(
                                payload=f"Tell the driver: {narration}",
                                priority=InjectionPriority.IMMEDIATE,
                                reason="service-center-found",
                            )
                        )
            # Booking succeeded — drop the offered-center sentinel so a
            # later plain "yes" (e.g. confirming an escalation prompt)
            # can't trigger another auto-book on the previously-offered
            # center, drowning out whatever Nova was actually trying to
            # do (e.g. acknowledge an escalation).
            elif tool_name == "book" and result_data.get("requestNumber"):
                # Capture the offered center BEFORE clearing it — the
                # injection below uses its name + slot for narration.
                offered_center = ctx.last_offered_service_center or {}
                state.pop("service_center_offered", None)
                ctx.last_offered_service_center = None
                log.info(
                    "book tool succeeded (request=%s) — cleared service_center_offered; "
                    "restoring IMMEDIATE confirmation injection",
                    result_data.get("requestNumber"),
                )
                # Restored 2026-05-21 16:33 EDT: same rationale as
                # the find_service_center IMMEDIATE injection above.
                # Nova Sonic v2 doesn't reliably narrate after the
                # book tool returns either; the IMMEDIATE injection
                # tells Nova exactly what to say.
                book_iq = state.get("injection_queue")
                if book_iq is not None:
                    center_name = offered_center.get("name") or "the service center"
                    slots = offered_center.get("nextAvailableSlots") or []
                    chosen_slot = slots[0] if slots else None
                    confirmation_payload = (
                        f"Tell the driver: Done — you're booked at "
                        f"{center_name}"
                        + (f" on {chosen_slot}." if chosen_slot else ".")
                    )
                    await book_iq.submit(
                        InjectionRequest(
                            payload=confirmation_payload,
                            priority=InjectionPriority.IMMEDIATE,
                            reason="book-confirmation",
                        )
                    )
            # Send silent audio nudge to wake Nova after tool result.
            # Without this, Nova can take 20-30s to generate speech.
            import base64 as _b64
            _silence = _b64.b64encode(b"\x00" * 1600).decode()
            try:
                await agent.send({
                    "type": "bidi_audio_input",
                    "audio": _silence,
                    "format": "pcm",
                    "sample_rate": 16000,
                    "channels": 1,
                })
            except Exception:
                pass
        elif event_type == "bidi_interruption":
            # Don't clear assistant_speaking immediately — the speaker
            # echo lingers for ~1s after interruption. Schedule a delayed
            # clear so echo doesn't flood back in.
            async def _delayed_unmute():
                await asyncio.sleep(1.0)
                state["assistant_speaking"] = False
            asyncio.create_task(_delayed_unmute())
            await _safe_send_json(websocket, {"type": "interruption"})
            # Treat interruption as turn-complete for queue purposes —
            # whether it was the driver barging in or our own IMMEDIATE
            # injection firing, either way the previous assistant turn
            # is over and any pending DEFERRED requests should no longer
            # wait on it.
            iq = state.get("injection_queue")
            if iq is not None:
                iq.mark_response_complete()
            # Cancel the turn watchdog too — interruption is a clean
            # turn-end from the queue's perspective.
            wd = state.pop("turn_watchdog", None)
            if wd is not None and not wd.done():
                wd.cancel()
            # Flush any buffered assistant text through the guardrail so
            # we capture what the model had said up to the interruption.
            gc = state.get("guardrail_checker")
            if gc is not None:
                gc.mark_turn_end()
        elif event_type == "bidi_response_start":
            # Nova Sonic just began an assistant turn. Tell the queue so
            # DEFERRED injections hold until we see the matching
            # is_final=true transcript (or a bidi_interruption).
            iq = state.get("injection_queue")
            if iq is not None:
                iq.mark_response_start()
                # Watchdog: Nova Sonic v2 doesn't reliably emit the
                # is_final=true assistant transcript that would normally
                # clear the turn. Observed 2026-05-07: contentEnd with
                # stopReason=END_TURN fires but the bidi framework drops
                # it (event_not_converted). Without this, DEFERRED
                # P2/P3 triage guidance sits in the queue indefinitely
                # and drivers hear "I'm checking" with no follow-up.
                # Fire-and-forget timer that force-clears turn_in_flight
                # after TURN_WATCHDOG_SECONDS; the real is_final path
                # cancels it if it arrives first.
                prev = state.pop("turn_watchdog", None)
                if prev is not None and not prev.done():
                    prev.cancel()

                async def _turn_watchdog(iq_ref):
                    try:
                        await asyncio.sleep(TURN_WATCHDOG_SECONDS)
                        iq_ref.mark_response_complete()
                        log.info(
                            "turn-watchdog: force-cleared turn_in_flight "
                            "session=%s after %.1fs (no is_final from Nova)",
                            ctx.session_id, TURN_WATCHDOG_SECONDS,
                        )
                    except asyncio.CancelledError:
                        pass

                state["turn_watchdog"] = asyncio.create_task(_turn_watchdog(iq))
        elif event_type == "bidi_connection_close":
            log.info("output: model closed connection type_counts=%s", state["type_counts"])
            # BidiAgent.run will terminate naturally after this.
        elif event_type == "bidi_error":
            err_payload = {"type": "error", "message": str(event.get("message") or event)}
            log.error("output: bidi_error %s", err_payload)
            await _safe_send_json(websocket, err_payload)
        else:
            # Emit a debug event to the client so unmapped event types
            # are visible without requiring CloudWatch access. For
            # bidi_usage specifically, pass through token counts so we
            # can see if Nova Sonic is producing any output at all.
            if isinstance(event, dict) and event_type:
                debug_payload: dict[str, Any] = {"type": "debug", "event_type": event_type}
                if event_type == "bidi_usage":
                    debug_payload.update({
                        "inputTokens": event.get("inputTokens"),
                        "outputTokens": event.get("outputTokens"),
                        "totalTokens": event.get("totalTokens"),
                    })
                await _safe_send_json(websocket, debug_payload)

        # Mirror classification whenever triage tool updates ctx.
        if ctx.triage_classification and ctx.triage_classification != state["last_classification"]:
            state["last_classification"] = ctx.triage_classification
            await _safe_send_json(
                websocket,
                {"type": "classification", "value": ctx.triage_classification},
            )

    return output_callable


# ---------------------------------------------------------------------------
# Session establishment — extract tenant / JWT / VIN from context + first msg
# ---------------------------------------------------------------------------


class InvocationError(ValueError):
    """Caller-side invocation contract violation."""


async def _establish_session(
    websocket,
    context: RequestContext,
    session_id: str,
) -> SessionContext:
    """Pull tenantId / VIN / JWT from query params or a session.start JSON msg.

    Precedence:
      1. Custom headers / query params (AgentCore normalizes both to headers).
      2. First WS message if it's {"type": "session.start", ...}.

    Returns a SessionContext. Raises InvocationError if any required field
    cannot be resolved.
    """
    headers = _normalize_headers(context.request_headers or {})
    # Log custom-prefixed header keys we received (values omitted from logs
    # for JWT safety; we log only the presence of each key name) so we can
    # diagnose AgentCore's casing behavior on VehicleId / DriverId.
    custom_keys = sorted(
        k for k in headers.keys()
        if k.startswith("x-amzn-bedrock-agentcore-runtime-custom-")
    )
    log.info("bidi: received custom header keys: %s", custom_keys)

    jwt = headers.get(USER_TOKEN_HEADER)
    tenant_id = headers.get(TENANT_QUERY_PARAM)
    vin = headers.get(VIN_QUERY_PARAM)
    persona_id = headers.get(PERSONA_ID_HEADER, "fleet_driver")
    customer_id = headers.get(CUSTOMER_ID_HEADER) or None
    vehicle_id = next(
        (headers[k] for k in VEHICLE_ID_QUERY_PARAMS if k in headers), None
    )
    driver_id = next(
        (headers[k] for k in DRIVER_ID_QUERY_PARAMS if k in headers), None
    )
    # Optional fresh coords pushed by the client. Tolerant of malformed
    # values: if the header is present but unparseable we treat it as
    # absent and fall back to the live-state HTTP lookup, rather than
    # failing the session.
    def _parse_coord(raw: Any) -> float | None:
        if raw is None or raw == "":
            return None
        try:
            value = float(raw)
        except (TypeError, ValueError):
            return None
        # Reject exactly (0, 0) — that's Null Island in the Atlantic
        # off Ghana, which no real connected vehicle is going to be.
        # Older iOS builds emitted "latitude": 0 / "longitude": 0 in
        # session.start when liveState wasn't loaded yet, and the
        # backend treated that as a valid location, sending
        # find_service_center off to find centers "nearby" 0°,0° —
        # which deterministically returned Tukwila first because it
        # was the first row in the DDB scan order. Treating 0 as
        # absent forces the live-state HTTP fallback, which knows
        # the vehicle's actual coords. Fixed 2026-05-19.
        if value == 0.0:
            return None
        return value
    client_latitude = _parse_coord(
        next((headers[k] for k in LATITUDE_QUERY_PARAMS if k in headers), None)
    )
    client_longitude = _parse_coord(
        next((headers[k] for k in LONGITUDE_QUERY_PARAMS if k in headers), None)
    )

    if not tenant_id or not vin:
        # Fallback: accept a session.start JSON message before audio starts.
        # Bound by 10s to avoid hanging on silent clients.
        try:
            start_msg = await asyncio.wait_for(
                websocket.receive_json(), timeout=10.0
            )
        except asyncio.TimeoutError:
            raise InvocationError(
                "Timed out waiting for session.start. Provide tenantId/vin "
                "via query params or send {\"type\":\"session.start\",...} first."
            ) from None
        if not isinstance(start_msg, dict) or start_msg.get("type") != "session.start":
            raise InvocationError(
                "First message must be {\"type\":\"session.start\", \"tenantId\":..., \"vin\":...}"
            )
        tenant_id = tenant_id or start_msg.get("tenantId")
        vin = vin or start_msg.get("vin")
        jwt = jwt or start_msg.get("jwt")
        # vehicleId / driverId are optional — don't fail if missing, just
        # leave SessionContext's fields as None. book() handles that path
        # by skipping the CMS write with a warning.
        vehicle_id = vehicle_id or start_msg.get("vehicleId")
        driver_id = driver_id or start_msg.get("driverId")
        # Lat/lng are optional best-effort coords for the live-state
        # bypass — pull them from the JSON fallback path too so the
        # local backend (which doesn't get AgentCore custom headers)
        # can still benefit.
        if client_latitude is None:
            client_latitude = _parse_coord(start_msg.get("latitude"))
        if client_longitude is None:
            client_longitude = _parse_coord(start_msg.get("longitude"))

    if not tenant_id:
        raise InvocationError("Missing tenantId (query param or session.start)")
    if not vin:
        raise InvocationError("Missing vin (query param or session.start)")
    if not jwt:
        raise InvocationError(
            "Missing user token. Supply X-Amzn-Bedrock-AgentCore-Runtime-Custom-User-Token "
            "header or include jwt in session.start."
        )

    jwt = jwt.strip()
    if not jwt:
        raise InvocationError("User token is empty")

    return SessionContext(
        tenant_id=tenant_id,
        session_id=session_id,
        jwt=jwt,
        persona_id=persona_id,
        vin=vin,
        vehicle_id=vehicle_id,
        driver_id=driver_id,
        customer_id=customer_id,
        client_latitude=client_latitude,
        client_longitude=client_longitude,
    )


def _normalize_headers(headers: dict[str, str]) -> dict[str, str]:
    """Lowercase header keys so we don't care about client casing."""
    return {k.lower(): v for k, v in headers.items()}


# ---------------------------------------------------------------------------
# Voice-prompt enrichment: CMS lookups for vehicle make/model + driver name
# ---------------------------------------------------------------------------
#
# These run once at session start to pull make/model/year/driver-name into
# the voice system prompt so Nova Sonic can refer to "your 2022 Chevrolet
# Equinox" and greet the driver by name, instead of speaking the VIN. Both
# are best-effort: on failure we return an empty dict and the prompt still
# renders with just the VIN.
#
# IAM for these reads lives on the supervisor runtime execution role; see
# scripts/attach-agentcore-permissions.py CmsRead statement.

import boto3 as _boto3  # lazy-ish; module-level import is fine for Lambda/AgentCore runtime

_cms_ddb_resource: Any = None


def _get_cms_ddb():
    """Lazy DDB resource so imports don't require AWS creds (unit tests)."""
    global _cms_ddb_resource
    if _cms_ddb_resource is None:
        _cms_ddb_resource = _boto3.resource(
            "dynamodb",
            region_name=os.environ.get("AWS_REGION", "us-east-1"),
        )
    return _cms_ddb_resource


def _fetch_vehicle_context(vehicle_id: str) -> dict[str, Any]:
    """Return a dict with make/model/year for the given vehicle, or {} on any
    failure. Never raises — voice session must not break if CMS is unreachable."""
    if not vehicle_id:
        return {}
    try:
        table_name = os.environ.get("CMS_VEHICLES_TABLE", f"cms-{os.environ.get('CMS_STAGE', 'dev')}-storage-vehicles")
        resp = _get_cms_ddb().Table(table_name).get_item(Key={"vehicleId": vehicle_id})
        item = resp.get("Item") or {}
    except Exception as e:  # noqa: BLE001
        log.warning("bidi: vehicle context lookup failed for %s: %s", vehicle_id, e)
        return {}
    # year is Decimal on DDB — cast to int for easier template use.
    year = item.get("year")
    try:
        year_int = int(year) if year is not None else None
    except (TypeError, ValueError):
        year_int = None
    return {
        "make": item.get("make"),
        "model": item.get("model"),
        "year": year_int,
        "vin": item.get("vin"),
        "latitude": float(item["lastLatitude"]) if item.get("lastLatitude") else None,
        "longitude": float(item["lastLongitude"]) if item.get("lastLongitude") else None,
    }


def _fetch_live_vehicle_location(
    vehicle_id: str,
    jwt: str,
    api_base: str,
) -> dict[str, Any]:
    """Return {latitude, longitude} from the live-state endpoint.

    The CMS-side `/vehicles/{id}/live-state` Lambda reads Redis first
    (fresh telemetry — same data the iOS Vehicle tab map shows and the
    CMS UI shows) with a DynamoDB fallback. The DDB row's
    lastLatitude/lastLongitude can lag actual telemetry by days when
    the vehicle is actively connecting, which previously caused Nova's
    find_service_center distance to be computed against a stale point
    while the driver was looking at the *current* map position in the
    app. Matching that endpoint's resolution order keeps the voice
    and visual surfaces consistent.

    Returns {} on any failure — voice session must not break if the
    live-state endpoint is unreachable; the caller falls back to the
    DDB-backed lat/lng from `_fetch_vehicle_context`.
    """
    if not (vehicle_id and jwt and api_base):
        return {}
    import requests
    try:
        resp = requests.get(
            f"{api_base}/vehicles/{vehicle_id}/live-state",
            headers={"Authorization": f"Bearer {jwt}"},
            timeout=5,
        )
        if resp.status_code >= 400:
            log.warning(
                "bidi: live-state returned %d for %s",
                resp.status_code, vehicle_id,
            )
            return {}
        data = resp.json() or {}
        return {
            "latitude": data.get("latitude"),
            "longitude": data.get("longitude"),
        }
    except Exception as e:  # noqa: BLE001
        log.warning(
            "bidi: live-state lookup failed for %s: %s", vehicle_id, e
        )
        return {}


def _fetch_tenant_segment(tenant_id: str) -> str:
    """Resolve the tenant's persona segment from vsa-prod-tenant-config.

    Returns one of "fleet" / "oem" / "rental". Falls back to "fleet"
    on any failure (missing table, missing pointer, wrong stage) so
    voice sessions still work when tenant config is unreachable —
    "fleet" gives the broadest find_service_center behavior.

    Follows the standard pointer-to-version pattern used elsewhere
    in the codebase (api-escalate, api-tenant-config):
        (tenantId, "current") → activeVersion → (tenantId, version) → segment.
    """
    if not tenant_id:
        return "fleet"
    try:
        table_name = os.environ.get(
            "VSA_TENANT_CONFIG_TABLE",
            f"vsa-{os.environ.get('VSA_STAGE', 'prod')}-tenant-config",
        )
        table = _get_cms_ddb().Table(table_name)
        pointer = table.get_item(
            Key={"tenantId": tenant_id, "version": "current"}
        ).get("Item") or {}
        active_version = pointer.get("activeVersion")
        if not active_version:
            log.warning(
                "tenant-segment: no activeVersion pointer for %s — defaulting to fleet",
                tenant_id,
            )
            return "fleet"
        item = table.get_item(
            Key={"tenantId": tenant_id, "version": active_version}
        ).get("Item") or {}
        seg = (item.get("segment") or "fleet").strip().lower()
        if seg not in ("fleet", "oem", "rental"):
            log.warning(
                "tenant-segment: unknown segment %r for %s — defaulting to fleet",
                seg, tenant_id,
            )
            return "fleet"
        return seg
    except Exception as e:  # noqa: BLE001
        log.warning("tenant-segment: lookup failed for %s: %s", tenant_id, e)
        return "fleet"


def _fetch_driver_context(driver_id: str) -> dict[str, Any]:
    """Return a dict with firstName/lastName/email for the given driver, or {}
    on any failure."""
    if not driver_id:
        return {}
    try:
        table_name = os.environ.get("CMS_DRIVERS_TABLE", f"cms-{os.environ.get('CMS_STAGE', 'dev')}-storage-drivers")
        resp = _get_cms_ddb().Table(table_name).get_item(Key={"driverId": driver_id})
        item = resp.get("Item") or {}
    except Exception as e:  # noqa: BLE001
        log.warning("bidi: driver context lookup failed for %s: %s", driver_id, e)
        return {}
    return {
        "firstName": item.get("firstName"),
        "lastName": item.get("lastName"),
        "email": item.get("email"),
    }


def _fetch_active_dtcs(vehicle_id: str) -> list[dict[str, Any]]:
    """Return a list of this vehicle's currently-active CRITICAL and HIGH
    DTCs, or [] on any failure.

    Used at session start to embed into the voice prompt so Nova Sonic can
    deliver a criticality-aware FIRST response (e.g., "Samantha, this is
    critical — pull over now") instead of a generic "I understand, let me
    check" ack followed by a 30-second-delayed classifier injection.
    Added 2026-05-08 to resolve the long-latency P0 demo gap.

    Scope: only CRITICAL and HIGH. LOW/MEDIUM aren't "pull over now"
    triggers; leaving them out keeps the prompt tight. The classifier
    still runs on every utterance and is the deterministic source of
    truth for P0/P1/P2/P3 — this DTC list just primes Nova Sonic so the
    first audio response is already on-script for the critical case.

    Mirrors the DTC retrieval logic in the triage classifier's
    `cms_client_real.get_active_dtcs` but inlined here so the supervisor
    doesn't need to import the classifier package. Dedup by code,
    keeping the newest row per code. Capped at 5 entries so the prompt
    doesn't bloat.
    """
    if not vehicle_id:
        return []
    try:
        from boto3.dynamodb.conditions import Key
        import time as _time
        table_name = os.environ.get("CMS_DTC_HISTORY_TABLE", f"cms-{os.environ.get('CMS_STAGE', 'dev')}-storage-dtc-history")
        # Status is the source of truth for "is this DTC active on the
        # vehicle right now?" — not row age. Originally we used a 30-day
        # lookback to trim the query, but that filtered out long-running
        # ACTIVE DTCs (observed 2026-05-08: P0A80 on VEH-0047, still
        # ACTIVE but row was 276 days old, silently dropped). Now: query
        # the full recent-first window (effectively all history for the
        # vehicle, 10-year cap to avoid a runaway scan in some future
        # dataset), and rely on the status/clearedDate filter below to
        # drop rows that aren't currently active.
        cutoff_ms = int(_time.time() * 1000) - 87600 * 3600 * 1000  # ~10 years
        resp = _get_cms_ddb().Table(table_name).query(
            KeyConditionExpression=(
                Key("vehicleId").eq(vehicle_id) & Key("timestamp").gte(cutoff_ms)
            ),
            ScanIndexForward=False,  # newest first
            Limit=100,
        )
    except Exception as e:  # noqa: BLE001
        log.warning("bidi: active-DTC lookup failed for %s: %s", vehicle_id, e)
        return []

    seen_codes: set[str] = set()
    out: list[dict[str, Any]] = []
    for item in resp.get("Items", []):
        status = (item.get("status") or "").upper()
        if status in ("CLEARED", "RESOLVED"):
            continue
        code = item.get("code")
        if not code or code in seen_codes:
            continue
        sev_raw = (item.get("severity") or "").upper()
        # Normalise numeric severity (4=CRITICAL, 3=HIGH, 2=MED, 1=LOW)
        # to string, matching the iOS severity helper.
        sev = {"4": "CRITICAL", "3": "HIGH", "2": "MEDIUM", "1": "LOW"}.get(sev_raw, sev_raw)
        if sev not in ("CRITICAL", "HIGH"):
            continue
        seen_codes.add(code)
        entry = {
            "code": code,
            "severity": sev,
            "description": (item.get("description") or "").strip()[:160],
            "system": item.get("system"),
        }
        # Enrich with response-template + symptom keywords from the
        # catalog so the voice prompt can drive a per-DTC critical
        # response (pull-over vs exit-vehicle vs stay-call-911, etc.)
        # Added 2026-05-08 — see agents/supervisor/dtc_response_catalog.py
        # for the mapping rationale and extension path.
        response = _dtc_lookup_response(code)
        if response:
            entry["template_id"] = response["template_id"]
            entry["keywords"] = response["keywords"]
            entry["diagnosis"] = response["diagnosis"]
            entry["chat_diagnosis"] = response["chat_diagnosis"]
        out.append(entry)
        if len(out) >= 5:
            break
    return out


def _fallback_session_id() -> str:
    return f"vsa-sess-bidi-{uuid.uuid4().hex[:12]}"


# ---------------------------------------------------------------------------
# WebSocket helpers (tolerant — the connection may already be closed)
# ---------------------------------------------------------------------------


async def _safe_send_json(websocket, payload: dict[str, Any]) -> None:
    try:
        await websocket.send_json(payload)
    except Exception as e:  # noqa: BLE001
        log.debug("bidi: send_json failed: %s", e)


async def _send_error(websocket, message: str) -> None:
    await _safe_send_json(websocket, {"type": "error", "message": message})


async def _safe_close(websocket, code: int = 1000, reason: str = "") -> None:
    try:
        await websocket.close(code=code, reason=reason)
    except Exception as e:  # noqa: BLE001
        log.debug("bidi: close failed: %s", e)


# ---------------------------------------------------------------------------
# Agent injection queue — turn-aware dispatch of server-side agent.send() calls
# ---------------------------------------------------------------------------
# Nova Sonic v2 treats a mid-response agent.send() as user barge-in and fires
# stopReason=INTERRUPTED, cutting off the assistant's speech. For safety-
# critical narration (P0/P1) interruption is semantically correct: the
# pull-over instruction must preempt any other response. For non-urgent
# narration (P2/P3) interruption is harmful — the driver hears the first
# 10-15 words of a helpful reply, then silence.
#
# This queue fixes that by tracking whether an assistant turn is currently
# in flight and dispatching submissions based on priority:
#   - IMMEDIATE priority flushes right away (used for P0/P1)
#   - DEFERRED priority waits for the current assistant turn to complete
#     (plus a small grace window so Nova Sonic's internal state settles),
#     then flushes (used for P2/P3)
#
# Only the triage auto-injection uses this queue today. Other injection
# sites (the book/escalate narrative strings) piggyback inside the triage
# payload, so they inherit the same timing. Future callers that want
# server-side agent.send() should go through this queue too.
# ---------------------------------------------------------------------------


class InjectionPriority:
    """Priority tiers for agent injection requests.

    IMMEDIATE: flush to the agent right away, even if mid-response. Use for
      safety-critical narration (P0 pull-over) and urgent handoffs (P1)
      where interrupting the model's current speech is the right behavior.
    DEFERRED: wait for the current assistant turn to complete before
      flushing. Use for non-urgent guidance (P2/P3) where the model should
      be allowed to finish its current response uninterrupted.
    """

    IMMEDIATE = "immediate"
    DEFERRED = "deferred"


class InjectionRequest:
    """A single agent.send() call waiting to be dispatched.

    Attributes:
        payload: The text to send to the agent (becomes the user-role
            turn from Nova Sonic's perspective).
        priority: One of InjectionPriority.{IMMEDIATE,DEFERRED}.
        reason: Short tag used in log output so we can correlate queue
            activity with triggers (e.g. "triage-P3", "triage-P0+book").
    """

    __slots__ = ("payload", "priority", "reason")

    def __init__(self, payload: str, priority: str, reason: str) -> None:
        self.payload = payload
        self.priority = priority
        self.reason = reason


class AgentInjectionQueue:
    """Turn-aware dispatcher for server-originated agent.send() calls.

    Lifecycle:
        queue = AgentInjectionQueue(agent, session_id=...)
        queue.start()                           # spawns the dispatcher task
        queue.mark_response_start()             # call on bidi_response_start
        queue.mark_response_complete()          # call on assistant is_final=true
        await queue.submit(InjectionRequest(...))  # enqueue from anywhere
        await queue.stop()                      # on session teardown

    Concurrency:
        All public methods are safe to call from any asyncio task. The
        internal dispatcher runs as a single task and owns the agent.send()
        call site, so we can't have two injections racing. The turn-state
        flag is an asyncio.Event (not a bool) so the dispatcher can park
        on it efficiently without polling.

    Why not just use asyncio.Queue?
        We need priority-aware ordering (IMMEDIATE jumps the queue) and
        turn-aware gating (DEFERRED waits for a signal). Neither is a
        natural fit for Queue. A small hand-rolled structure is clearer.
    """

    # Grace period after an assistant turn completes, before DEFERRED
    # injections flush. Nova Sonic sometimes emits a small tail of events
    # after is_final=true, and injecting too eagerly can still race the
    # tail and produce stopReason=INTERRUPTED. 300ms has been reliable in
    # manual testing; tune via env var if we see regressions.
    _POST_TURN_GRACE_MS = 300

    # Maximum time a DEFERRED request will sit in the queue waiting for
    # a turn-complete signal before it flushes anyway. Nova Sonic v2 is
    # unreliable about emitting the assistant-role is_final=true transcript
    # that drives mark_response_complete() — empirically, turns routinely
    # finish speaking without ever emitting that event, which would leave
    # DEFERRED guidance stuck in the queue until session teardown. The
    # timeout guarantees the guidance eventually plays even if the
    # turn-end signal never arrives. Tuned to be long enough that a
    # normal-length assistant response finishes first (so we get the
    # "wait for turn to end" benefit on well-behaved turns), but short
    # enough that drivers aren't left waiting noticeably on misbehaved
    # ones. See the bidi_app injection-queue rationale for full context.
    _MAX_DEFER_SECONDS = 8.0

    def __init__(self, agent: BidiAgent, *, session_id: str) -> None:
        self._agent = agent
        self._session_id = session_id
        # Set means "assistant turn is currently in flight." Cleared means
        # "idle — deferred injections may flush."
        self._turn_in_flight = asyncio.Event()
        # Pending DEFERRED requests, as (request, submitted_at_monotonic)
        # tuples in submission order. The monotonic timestamp lets the
        # dispatcher apply the per-item _MAX_DEFER_SECONDS deadline
        # independently of when the dispatcher happens to service the
        # request — otherwise a fast-queuing burst during a long turn
        # would make later items inherit earlier items' remaining budget.
        self._deferred: list[tuple[InjectionRequest, float]] = []
        # IMMEDIATE requests queued here; the dispatcher drains both.
        self._immediate: list[InjectionRequest] = []
        # When set, future DEFERRED submits are dropped. Set by
        # cancel_deferred() when Nova starts calling tools (she's
        # handling the conversation; backup auto-triage nudges should
        # not interfere). Cleared by clear_deferred_block() on the next
        # user turn so the queue is reusable across the session.
        self._block_deferred = False
        # Wakes the dispatcher whenever there's new work OR a turn ends.
        self._wake = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._stopped = False

    # ------------------------------------------------------------------ public

    def start(self) -> None:
        """Spawn the dispatcher task. Idempotent."""
        if self._task is not None:
            return
        self._task = asyncio.create_task(
            self._dispatch_loop(), name=f"injection-queue[{self._session_id}]"
        )
        log.info("injection-queue: started session=%s", self._session_id)

    async def stop(self) -> None:
        """Cancel the dispatcher task and drop any pending work. Idempotent."""
        self._stopped = True
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
        self._task = None
        dropped = len(self._immediate) + len(self._deferred)
        if dropped:
            log.info(
                "injection-queue: stopped session=%s dropped=%d",
                self._session_id, dropped,
            )

    def cancel_deferred(self) -> int:
        """Cancel pending DEFERRED injections AND block future ones.

        When Nova starts calling tools (tool_use_stream), she's handling
        the conversation herself. Any in-flight auto-triage task may
        still finish *after* this call and try to submit a DEFERRED
        nudge — that submission would race past the cancel and fire
        anyway as soon as the turn ends or an interruption arrives.

        Setting the block here drops both the current queue and any
        late-arriving DEFERRED submits until clear_deferred_block() is
        called on the next user turn.

        Returns the count of items cleared from the queue (does not
        include future drops).
        """
        count = len(self._deferred)
        if count:
            self._deferred.clear()
        self._block_deferred = True
        log.info(
            "injection-queue: cancelled %d deferred + blocked future deferred session=%s",
            count, self._session_id,
        )
        return count

    def clear_deferred_block(self) -> None:
        """Allow DEFERRED submits again. Called on next user final turn."""
        if self._block_deferred:
            self._block_deferred = False
            log.info(
                "injection-queue: deferred block cleared session=%s",
                self._session_id,
            )

    def mark_response_start(self) -> None:
        """Signal that Nova Sonic just began an assistant turn."""
        self._turn_in_flight.set()

    def mark_response_complete(self) -> None:
        """Signal that the current assistant turn has ended (is_final=true)."""
        self._turn_in_flight.clear()
        self._wake.set()

    async def submit(self, request: InjectionRequest) -> None:
        """Enqueue an injection. IMMEDIATE requests dispatch right away;
        DEFERRED requests wait until the current assistant turn ends.
        """
        if self._stopped:
            log.warning(
                "injection-queue: drop submit after stop session=%s reason=%s",
                self._session_id, request.reason,
            )
            return
        if request.priority == InjectionPriority.IMMEDIATE:
            self._immediate.append(request)
            log.info(
                "injection-queue: enqueue IMMEDIATE session=%s reason=%s "
                "payload_chars=%d turn_in_flight=%s",
                self._session_id, request.reason, len(request.payload),
                self._turn_in_flight.is_set(),
            )
        else:
            if self._block_deferred:
                # Nova called a tool earlier this turn; she's handling
                # it. Drop the late-arriving auto-triage nudge so it
                # doesn't fire as a duplicate response.
                log.info(
                    "injection-queue: drop DEFERRED (blocked, tools active) "
                    "session=%s reason=%s payload_chars=%d",
                    self._session_id, request.reason, len(request.payload),
                )
                return
            self._deferred.append((request, asyncio.get_event_loop().time()))
            log.info(
                "injection-queue: enqueue DEFERRED session=%s reason=%s "
                "payload_chars=%d turn_in_flight=%s max_defer_s=%.1f",
                self._session_id, request.reason, len(request.payload),
                self._turn_in_flight.is_set(), self._MAX_DEFER_SECONDS,
            )
        self._wake.set()

    # ----------------------------------------------------------------- internal

    async def _dispatch_loop(self) -> None:
        """Drain IMMEDIATE first, then DEFERRED. Runs until cancelled.

        DEFERRED handling is the interesting part. Each DEFERRED request
        was stamped with its submission time and has a _MAX_DEFER_SECONDS
        budget. For each request at the head of the queue, the dispatcher
        waits for EITHER (a) the turn-in-flight event to clear (Nova
        Sonic emitted is_final=true or an interruption), plus a short
        grace window, OR (b) the request's deadline to elapse, whichever
        comes first.

        The timeout path exists because Nova Sonic v2 frequently finishes
        speaking without emitting the assistant-role is_final=true
        transcript that would normally clear the turn. Without the
        timeout, DEFERRED guidance would sit in the queue until session
        teardown and the driver would never hear it.
        """
        try:
            while True:
                await self._wake.wait()
                self._wake.clear()

                # Drain all IMMEDIATE requests — these run regardless of turn state.
                while self._immediate:
                    req = self._immediate.pop(0)
                    await self._send(req, trigger="immediate")

                # Drain DEFERRED requests one at a time. Each one gets its
                # own turn-complete-or-timeout race.
                while self._deferred and not self._stopped:
                    req, submitted_at = self._deferred[0]
                    trigger = await self._wait_for_deferred_flush(submitted_at)
                    if trigger is None:
                        # _wait_for_deferred_flush returns None when it got
                        # cancelled or the queue stopped. Exit cleanly.
                        return
                    # Pop and send. Previously we did a defensive re-check
                    # (`self._deferred[0] is not (req, submitted_at)`) to
                    # guard against the head moving via stop(), but that
                    # check compared a fresh tuple literal with the stored
                    # tuple — always unequal by identity, so the dispatcher
                    # `continue`d forever and the injection never flushed.
                    # The only caller that mutates `_deferred` under us
                    # is stop() or cancel_deferred(). Guard against empty.
                    if not self._deferred:
                        break
                    self._deferred.pop(0)
                    await self._send(req, trigger=trigger)
        except asyncio.CancelledError:
            log.info("injection-queue: dispatch-loop cancelled session=%s", self._session_id)
            raise
        except Exception as e:  # noqa: BLE001
            log.exception(
                "injection-queue: dispatch-loop CRASHED session=%s: %s",
                self._session_id, e,
            )
            raise

    async def _wait_for_deferred_flush(self, submitted_at: float) -> str | None:
        """Wait until a DEFERRED request at the head of the queue is
        eligible to flush. Returns a short string describing which path
        triggered the flush ("turn-complete" or "timeout"), or None if
        the dispatcher is being cancelled/stopped.

        The race is: wait until turn-in-flight clears and the grace
        window elapses, OR until the request's individual deadline
        passes, whichever happens first.
        """
        loop = asyncio.get_event_loop()
        deadline = submitted_at + self._MAX_DEFER_SECONDS

        while True:
            now = loop.time()
            remaining = deadline - now
            if remaining <= 0:
                return "timeout"

            if self._turn_in_flight.is_set():
                # Turn currently speaking. Wait for it to clear OR for
                # our deadline, whichever comes first.
                clear_task = asyncio.create_task(
                    self._wait_for_turn_clear()
                )
                try:
                    done, _ = await asyncio.wait(
                        {clear_task},
                        timeout=remaining,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                finally:
                    if not clear_task.done():
                        clear_task.cancel()
                        try:
                            await clear_task
                        except (asyncio.CancelledError, Exception):  # noqa: BLE001
                            pass
                if not done:
                    # Deadline elapsed while the turn was still in flight.
                    return "timeout"
                # Turn cleared — fall through to grace-window handling.

            # Turn is idle. Wait the grace period, but respect the
            # deadline. If a new turn starts during the grace window,
            # we'll see turn_in_flight set again on the next loop iter
            # and re-enter the wait branch above.
            grace = self._POST_TURN_GRACE_MS / 1000
            now = loop.time()
            wait_for = min(grace, max(0.0, deadline - now))
            if wait_for > 0:
                await asyncio.sleep(wait_for)
            if self._turn_in_flight.is_set():
                # A new turn started during the grace window. Loop back
                # and wait again, subject to the same overall deadline.
                log.info(
                    "injection-queue: grace-window race session=%s "
                    "re-deferring head",
                    self._session_id,
                )
                continue
            # Past the grace window and no new turn. Good to flush.
            return "turn-complete" if loop.time() < deadline else "timeout"

    async def _wait_for_turn_clear(self) -> None:
        """Block until _turn_in_flight is cleared. Uses a tiny poll loop
        because asyncio.Event doesn't expose a "wait for cleared" primitive;
        we sleep briefly and check. The cost is negligible since this
        only runs while a DEFERRED item is head-of-queue."""
        while self._turn_in_flight.is_set():
            await asyncio.sleep(0.05)

    async def _send(self, req: InjectionRequest, *, trigger: str) -> None:
        """Actually call agent.send(). Catches exceptions so one bad
        injection doesn't kill the dispatcher for the rest of the session.

        `trigger` records which dispatch path fired this send —
        "immediate" for IMMEDIATE requests, "turn-complete" when a
        DEFERRED request flushed because Nova Sonic signalled turn end,
        or "timeout" when it flushed because the _MAX_DEFER_SECONDS
        budget ran out. Logged so CloudWatch can show whether the
        timeout path is being exercised (i.e. Nova Sonic is misbehaving)
        vs. the clean turn-complete path.
        """
        log.info(
            "injection-queue: dispatch %s session=%s reason=%s payload_chars=%d",
            trigger, self._session_id, req.reason, len(req.payload),
        )
        try:
            await self._agent.send(req.payload)
        except Exception as e:  # noqa: BLE001
            log.exception(
                "injection-queue: agent.send failed session=%s reason=%s: %s",
                self._session_id, req.reason, e,
            )


# ---------------------------------------------------------------------------
# Guardrail checker — dry-run observability for Nova Sonic content drift
# ---------------------------------------------------------------------------
# Wraps Bedrock's ApplyGuardrail API against a configured guardrail policy
# and runs it over Nova Sonic's streaming assistant transcript. In DRY-RUN
# mode (the only mode today) it never blocks or interrupts — it just logs
# intervention verdicts so we can see which drifts the guardrail catches
# and which of our current responses it would falsely flag.
#
# Buffering strategy: the Nova Sonic model emits assistant transcript in
# small deltas (a clause at a time). Calling ApplyGuardrail on every delta
# would be wasteful and noisy. Instead we accumulate deltas into a rolling
# buffer and submit when we hit a sentence boundary (`.` `!` `?`), a
# force-flush byte cap, or the end of the assistant turn. This gives us
# near-real-time verdicts without spamming Bedrock.
#
# Calls run in a fire-and-forget asyncio task so the main output pipeline
# doesn't block on guardrail latency (50-150ms per call). The checker
# owns its own tiny task set so teardown at session end cancels cleanly.
#
# Future non-dry-run mode: on `INTERVENED` verdict, enqueue an IMMEDIATE
# InjectionRequest into the AgentInjectionQueue so the agent is forced to
# course-correct. The helper below leaves that path commented/placeholder.
# ---------------------------------------------------------------------------


# Guardrail identity. Sourced from env (VSA_GUARDRAIL_ID / _VERSION) with
# fall-through defaults. The deployed guardrail `vsa-vehicle-scope`
# (o1skd021xxnm) exists in us-east-1 with only a DRAFT version published —
# requesting version "1" (the prior hardcoded value) made every
# ApplyGuardrail call fail with ValidationException "guardrail identifier or
# version ... does not exist". Default to DRAFT; override via env once a
# numbered version is published.
_VSA_GUARDRAIL_ID = os.environ.get("VSA_GUARDRAIL_ID", "o1skd021xxnm")
_VSA_GUARDRAIL_VERSION = os.environ.get("VSA_GUARDRAIL_VERSION", "DRAFT")

# Rolling assistant-transcript buffer flushes to ApplyGuardrail when any
# of these conditions hit:
#   1. the last char is a sentence terminator and the buffer is >= this size
#   2. the buffer exceeds _GUARDRAIL_FORCE_FLUSH_CHARS regardless of content
#   3. the assistant turn ends (is_final=true)
# The sentence-min keeps us from flushing "Yeah." as a separate check.
_GUARDRAIL_MIN_SENTENCE_CHARS = 12
_GUARDRAIL_FORCE_FLUSH_CHARS = 200

_bedrock_runtime_client: Any = None


def _get_bedrock_runtime():
    """Lazy boto3 client for bedrock-runtime.

    ApplyGuardrail lives on the `bedrock-runtime` service, not `bedrock`
    (which only has the control-plane APIs like CreateGuardrail). Lazy
    construction mirrors `_get_cms_ddb` — avoids requiring AWS creds at
    module import time, which matters for unit tests that don't exercise
    the guardrail path.
    """
    global _bedrock_runtime_client
    if _bedrock_runtime_client is None:
        _bedrock_runtime_client = _boto3.client(
            "bedrock-runtime",
            region_name=os.environ.get("AWS_REGION", "us-east-1"),
        )
    return _bedrock_runtime_client


class GuardrailChecker:
    """Buffers assistant transcript deltas and fires ApplyGuardrail at
    sentence boundaries. DRY-RUN: logs verdicts, never blocks.

    Lifecycle:
        checker = GuardrailChecker(session_id=...)
        checker.feed_delta(" Hello there.")   # accumulates + maybe flushes
        checker.mark_turn_end()               # force-flush remaining buffer
        await checker.aclose()                # wait for in-flight tasks

    Concurrency:
        feed_delta() is synchronous and safe to call from the output
        callable's hot path. Actual ApplyGuardrail calls fork into
        asyncio tasks so the delta pipeline is never blocked on Bedrock
        latency. aclose() awaits those tasks for clean teardown.
    """

    def __init__(
        self,
        *,
        session_id: str,
        guardrail_id: str = _VSA_GUARDRAIL_ID,
        guardrail_version: str = _VSA_GUARDRAIL_VERSION,
    ) -> None:
        self._session_id = session_id
        self._guardrail_id = guardrail_id
        self._guardrail_version = guardrail_version
        self._buffer: str = ""
        self._tasks: set[asyncio.Task] = set()
        # Count of flushes/verdicts so log lines include a turn index.
        self._flush_count: int = 0
        self._intervened_count: int = 0
        # Latched true after a fatal config error (e.g. the guardrail id or
        # version does not exist) so we stop retrying — and stop spamming the
        # logs — on every sentence for the rest of the session.
        self._disabled: bool = False

    # ------------------------------------------------------------------ public

    def feed_delta(self, text: str) -> None:
        """Accumulate a transcript delta. May schedule an ApplyGuardrail
        check as a side effect if a sentence boundary or force-flush
        threshold is hit."""
        if not text:
            return
        self._buffer += text
        if self._should_flush_on_boundary():
            self._schedule_flush(reason="sentence-boundary")
        elif len(self._buffer) >= _GUARDRAIL_FORCE_FLUSH_CHARS:
            self._schedule_flush(reason="force-flush")

    def mark_turn_end(self) -> None:
        """Force-flush whatever remains in the buffer. Call on
        bidi_transcript_stream with is_final=true and role=assistant, or
        on bidi_interruption so we capture the partial utterance too."""
        if self._buffer.strip():
            self._schedule_flush(reason="turn-end")

    async def aclose(self) -> None:
        """Wait for any in-flight guardrail tasks to finish. Idempotent.
        Called from session teardown so we don't leak pending coroutines."""
        if not self._tasks:
            return
        log.info(
            "guardrail: session=%s aclose waiting on %d in-flight checks",
            self._session_id, len(self._tasks),
        )
        # Snapshot tasks and await them. Swallow all exceptions so a
        # failed Bedrock call doesn't cascade into session teardown.
        pending = list(self._tasks)
        for task in pending:
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self._tasks.clear()
        log.info(
            "guardrail: session=%s closed flushes=%d interventions=%d",
            self._session_id, self._flush_count, self._intervened_count,
        )

    # ----------------------------------------------------------------- internal

    def _should_flush_on_boundary(self) -> bool:
        """True if the buffer ends in a sentence terminator and has
        enough content to be worth checking. We don't want to flush
        on every single "Yeah." or "OK." — only on meaningful sentences
        that might carry drift content."""
        stripped = self._buffer.rstrip()
        if not stripped:
            return False
        if len(stripped) < _GUARDRAIL_MIN_SENTENCE_CHARS:
            return False
        return stripped[-1] in ".!?"

    def _schedule_flush(self, *, reason: str) -> None:
        """Snapshot the current buffer, reset it, and fire the
        ApplyGuardrail call as a background task. Never blocks."""
        if self._disabled:
            self._buffer = ""
            return
        text = self._buffer
        self._buffer = ""
        if not text.strip():
            return
        self._flush_count += 1
        flush_idx = self._flush_count
        task = asyncio.create_task(
            self._run_apply_guardrail(text, reason=reason, flush_idx=flush_idx),
            name=f"guardrail[{self._session_id}][{flush_idx}]",
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _run_apply_guardrail(
        self, text: str, *, reason: str, flush_idx: int
    ) -> None:
        """Actually call ApplyGuardrail. In DRY-RUN we log the verdict
        and never take corrective action. Designed to be resilient:
        exceptions (credential issues, throttling, network) are logged
        but swallowed so a guardrail misbehavior doesn't damage the
        voice session."""
        loop = asyncio.get_event_loop()
        preview = text[:80].replace("\n", " ")
        try:
            # boto3 is synchronous; push it to the default executor so
            # we don't block the asyncio loop.
            resp = await loop.run_in_executor(
                None, self._invoke_apply_guardrail, text
            )
        except Exception as e:  # noqa: BLE001
            msg = str(e)
            if "ValidationException" in msg and "does not exist" in msg:
                # Misconfigured guardrail (id/version not found). Latch off so
                # we don't retry + log on every remaining sentence this session.
                self._disabled = True
                log.warning(
                    "guardrail: session=%s DISABLED — guardrail id=%s version=%s "
                    "not found; dry-run checks skipped. Set VSA_GUARDRAIL_ID / "
                    "VSA_GUARDRAIL_VERSION to a valid guardrail to re-enable.",
                    self._session_id, self._guardrail_id, self._guardrail_version,
                )
            else:
                log.exception(
                    "guardrail: session=%s flush=%d reason=%s ApplyGuardrail failed: %s",
                    self._session_id, flush_idx, reason, e,
                )
            return

        action = resp.get("action", "NONE")
        if action == "GUARDRAIL_INTERVENED":
            self._intervened_count += 1
            # Summarise which policy categories fired. ApplyGuardrail
            # returns an `assessments` array with per-policy details.
            categories = self._summarise_assessments(resp)
            log.warning(
                "guardrail: session=%s flush=%d reason=%s INTERVENED "
                "categories=%s preview=%r",
                self._session_id, flush_idx, reason, categories, preview,
            )
        else:
            log.info(
                "guardrail: session=%s flush=%d reason=%s action=%s preview=%r",
                self._session_id, flush_idx, reason, action, preview,
            )

    def _invoke_apply_guardrail(self, text: str) -> dict[str, Any]:
        """Synchronous ApplyGuardrail call. Split out so _run_apply_guardrail
        can hand it off to the executor without awkward lambda wrapping."""
        client = _get_bedrock_runtime()
        return client.apply_guardrail(
            guardrailIdentifier=self._guardrail_id,
            guardrailVersion=self._guardrail_version,
            # Source OUTPUT because we're evaluating what the assistant
            # would say to the driver. INPUT is for user-supplied text.
            source="OUTPUT",
            content=[{"text": {"text": text}}],
        )

    def _summarise_assessments(self, resp: dict[str, Any]) -> list[str]:
        """Extract a short human-readable list of which policies fired
        for logging. ApplyGuardrail's response shape is nested; we only
        care about the denied-topic names in DRY-RUN."""
        hits: list[str] = []
        for assessment in resp.get("assessments", []) or []:
            topic = assessment.get("topicPolicy") or {}
            for t in topic.get("topics", []) or []:
                if t.get("action") == "BLOCKED":
                    hits.append(t.get("name", "?"))
        # De-dup while preserving order.
        seen: set[str] = set()
        unique: list[str] = []
        for h in hits:
            if h not in seen:
                unique.append(h)
                seen.add(h)
        return unique


# ---------------------------------------------------------------------------
# Auto-triage: run the classifier server-side when the user finishes speaking,
# then inject the result back into the conversation so Nova Sonic narrates
# from real data. See Addendum 11 in PHASE3_STEP3_RESULTS.md for why.
# ---------------------------------------------------------------------------


async def _auto_book_on_affirmation(
    websocket,
    ctx: SessionContext,
    config: Config,
    state: dict[str, Any],
    user_text: str,
) -> None:
    """Server-side book call when user affirms after a service-center offer.

    Background — we previously relied on Nova Sonic to call the registered
    `book` tool when the driver said "yes" after a find_service_center
    offer. That works most of the time but fails in two reproducible
    ways with Nova Sonic v2:

    1. Nova narrates a stall ("I need to run a quick check first. One
       moment please.") instead of calling the tool. Observed
       2026-05-20 16:22:40.
    2. Nova would also try to book before triage ran — but the book
       tool requires `ctx.triage_classification` to be set, and for
       text-input flows (DTC ⓘ button → primed text) the auto-triage
       hook never fired because it gates on user-AUDIO transcripts.

    This helper bypasses Nova for the booking step entirely. When the
    auto-book branch in `_run_session_loop` detects an affirmation and
    a previously-offered center is on `state`, it dispatches this
    coroutine. We:

      1. Synchronously call triage if classification is missing (so
         book's precondition is satisfied even on text-only flows).
      2. Synchronously call book via call_book_endpoint.
      3. Mirror tool.call + tool.result events to iOS so the reasoning
         drawer shows the same affordances as a Nova-driven book.
      4. Inject a short narration to Nova so it SPEAKS the
         confirmation rather than us trying to interrupt with TTS
         ourselves. "Tell the driver: Done — you're booked at X at Y."

    Synchronously is the right call here because the user just said
    "yes" — a 200-300ms wait for the booking is fine, and lets us
    feed Nova the actual confirmation phrasing rather than a
    speculative one.
    """
    offered = state.get("service_center_offered")
    if not offered:
        log.warning(
            "auto-book: no service_center_offered in state (cleared by another handler?)"
        )
        return

    # Pop the sentinel up front so a duplicate "yes" can't double-book.
    state.pop("service_center_offered", None)

    # Cancel any in-flight Nova response — when the user said "yes",
    # Nova may have already started narrating a stall ("I need to run
    # a quick check"). Sending an interruption here lets our IMMEDIATE
    # narration injection take over without overlapping audio.
    iq = state.get("injection_queue")

    loop = asyncio.get_running_loop()

    # ------ ensure triage is satisfied ------
    if ctx.triage_classification is None or ctx.triage_decision_id is None:
        try:
            triage_result = await loop.run_in_executor(
                None, lambda: call_triage_endpoint(ctx, config, ctx.vin)
            )
            log.info(
                "auto-book: ran inline triage classification=%s decision=%s",
                triage_result.get("classification"),
                triage_result.get("triageDecisionId", "unknown"),
            )
            # Mirror to drawer so the user sees the triage step.
            await _safe_send_json(
                websocket,
                {"type": "tool.call", "name": "triage", "input": {"vin": ctx.vin}},
            )
            await _safe_send_json(
                websocket,
                {"type": "tool.result", "name": "triage", "output": triage_result},
            )
        except Exception as e:  # noqa: BLE001
            log.exception("auto-book: inline triage failed: %s", e)
            # Best-effort fallback — set P3 so book can proceed without
            # blocking the demo on a transient classifier hiccup.
            ctx.triage_classification = "P3"
            ctx.triage_decision_id = ctx.session_id

    # ------ call book ------
    center_name = offered.get("name", "the service center")
    center_addr = offered.get("address", "")
    slots = offered.get("nextAvailableSlots") or []
    chosen_slot = slots[0] if slots else "the next available slot"

    narrative = (
        f"Driver confirmed booking at {center_name} via voice "
        f"affirmation ('{user_text}'). Reported issue tied to "
        f"earlier triage in this session."
    )
    try:
        booking_result = await loop.run_in_executor(
            None,
            lambda: call_book_endpoint(
                ctx,
                config,
                ctx.vin,
                reported_symptom=user_text or "Driver confirmed booking",
                customer_actions_taken=["book-appointment"],
                narrative=narrative,
            ),
        )
        log.info(
            "auto-book: server-side book succeeded request=%s status=%s",
            booking_result.get("requestNumber"),
            booking_result.get("status"),
        )
    except Exception as e:  # noqa: BLE001
        log.exception("auto-book: server-side book failed: %s", e)
        # Tell Nova to apologize so the user isn't left in silence.
        if iq is not None:
            await iq.submit(
                InjectionRequest(
                    payload=(
                        "Tell the driver: I had trouble completing the "
                        "booking — please try again in a moment."
                    ),
                    priority=InjectionPriority.IMMEDIATE,
                    reason="auto-book-failed",
                )
            )
        return

    # ------ confirmation transcript first ------
    #
    # Send the assistant transcript BEFORE the tool.call/tool.result
    # mirrors so iOS renders things in the right order:
    #   1. "Done — you're booked at AutoNation Ford..." appears as
    #      the assistant's reply bubble.
    #   2. Then the booking confirmation card (rendered from the
    #      tool.result book event by VoiceSessionViewModel) appears
    #      below it.
    # If we sent the tool events first, iOS would render the
    # confirmation card BEFORE the "Done" bubble — the card would
    # arrive ahead of the explanation that justifies it.
    confirmation_text = (
        f"Done — you're booked at {center_name} on {chosen_slot}."
    )
    await _safe_send_json(
        websocket,
        {
            "type": "transcript",
            "role": "assistant",
            "text": confirmation_text,
            "isFinal": True,
            "_synthetic_from_auto_book": True,
        },
    )

    # ------ then mirror book to drawer + as a confirmation card ------
    await _safe_send_json(
        websocket,
        {
            "type": "tool.call",
            "name": "book",
            "input": {
                "vin": ctx.vin,
                "reportedSymptom": user_text,
                "customerActionsTaken": ["book-appointment"],
                "serviceCenter": center_name,
            },
        },
    )
    await _safe_send_json(
        websocket,
        {
            "type": "tool.result",
            "name": "book",
            "output": {
                "requestNumber": booking_result.get("requestNumber"),
                "status": booking_result.get("status"),
                "messageId": booking_result.get("messageId"),
                "serviceCenter": center_name,
                "scheduledFor": chosen_slot,
            },
        },
    )

    # ------ inject narration to Nova for TTS ------
    #
    # We tell Nova what to say so the user gets an audible
    # confirmation matching the on-screen text. Best-effort: when
    # Nova is mid-flush of a previous turn the IMMEDIATE injection
    # may be swallowed (observed ~1 minute delay before Nova's TTS
    # actually plays the confirmation). The synthetic transcript
    # above is the reliable visual path; this is the audio path.
    if iq is not None:
        confirmation = f"Tell the driver: {confirmation_text}"
        await iq.submit(
            InjectionRequest(
                payload=confirmation,
                priority=InjectionPriority.IMMEDIATE,
                reason="auto-book-confirmation",
            )
        )


async def _auto_triage_and_inject(
    websocket,
    agent: BidiAgent,
    ctx: SessionContext,
    config: Config,
    user_utterance: str,
    injection_queue: "AgentInjectionQueue",
    state: dict,
) -> None:
    """Run triage() server-side, mirror tool events to the reasoning drawer,
    auto-book on P0, and enqueue a combined system note for the agent.

    The final agent-facing injection goes through `injection_queue` rather
    than calling agent.send() directly so that P2/P3 guidance doesn't
    interrupt Nova Sonic mid-response. P0/P1 still flush immediately;
    the interruption is semantically correct for safety-critical and
    urgent-handoff narration. See AgentInjectionQueue for the full design.

    Driver-confirmed escalation (see triage_keywords.py): when the
    classifier returns P2 or P3 BUT the driver's utterance contained an
    emergency keyword (brake failure, steering failure, fire/smoke, etc.),
    this function diverges from the normal flow: instead of narrating
    the classifier's non-urgent guidance, it injects a yes/no
    confirmation question and records state so the next user transcript
    is interpreted as a confirmation response rather than a new triage
    trigger. `state["awaiting_confirmation_*"]` fields carry the pending
    context.

    Two things happen here that Nova Sonic v2 can't do itself reliably
    (Strands sdk-python#2021):

    1. Tool invocation. We call triage() directly and mirror the call as
       wire-protocol tool.call + tool.result events so the iOS reasoning
       drawer populates like it does in the text-mode path.
    2. P0 auto-escalation (design-doc rule 3a). When the classifier
       returns P0, we immediately auto-fire book() with the design-
       mandated actions (`create-ticket`, `schedule-roadside-if-vehicle-fails`)
       without waiting for customer confirmation. P0 is the only tier
       that auto-escalates; P1/P2/P3 still require customer confirmation
       which would come from a later conversational turn.

    The user_utterance is used as the reportedSymptom on P0 booking so
    the STAR envelope carries the driver's own words.
    """
    # ------ triage ------
    try:
        loop = asyncio.get_running_loop()
        triage_result = await loop.run_in_executor(
            None, lambda: call_triage_endpoint(ctx, config, ctx.vin)
        )
    except Exception as e:  # noqa: BLE001
        log.exception("auto-triage: classifier call failed: %s", e)
        return

    classification = triage_result.get("classification")
    log.info(
        "auto-triage: classifier returned %s (decision=%s, latency=%dms)",
        classification,
        triage_result.get("triageDecisionId", "unknown"),
        triage_result.get("latencyMs", -1),
    )

    # Mirror the triage call to the client so the reasoning drawer
    # displays it identically to a tool-call in the text-mode path.
    await _safe_send_json(
        websocket,
        {
            "type": "tool.call",
            "name": "triage",
            "input": {"vin": ctx.vin},
        },
    )
    await _safe_send_json(
        websocket,
        {
            "type": "tool.result",
            "name": "triage",
            "output": triage_result,
        },
    )

    # ------ auto-book on P0 (design rule 3a) ------
    booking_result: dict[str, Any] | None = None
    if classification == "P0":
        narrative = (
            f"P0 auto-escalation for VIN {ctx.vin}. Driver reported: "
            f"{user_utterance!r}. Roadside dispatch scheduled per NHTSA "
            f"P0 response protocol."
        )
        try:
            booking_result = await loop.run_in_executor(
                None,
                lambda: call_book_endpoint(
                    ctx,
                    config,
                    ctx.vin,
                    reported_symptom=user_utterance,
                    customer_actions_taken=[
                        "create-ticket",
                        "schedule-roadside-if-vehicle-fails",
                    ],
                    narrative=narrative,
                ),
            )
            log.info(
                "auto-book: P0 ticket created %s status=%s",
                booking_result.get("requestNumber"),
                booking_result.get("status"),
            )
            # Mirror book to the drawer too.
            await _safe_send_json(
                websocket,
                {
                    "type": "tool.call",
                    "name": "book",
                    "input": {
                        "vin": ctx.vin,
                        "reportedSymptom": user_utterance,
                        "customerActionsTaken": [
                            "create-ticket",
                            "schedule-roadside-if-vehicle-fails",
                        ],
                        "narrative": narrative,
                    },
                },
            )
            await _safe_send_json(
                websocket,
                {
                    "type": "tool.result",
                    "name": "book",
                    "output": {
                        # Send the small shape, not the full envelope. The
                        # full STAR envelope is audit-tier detail; the
                        # drawer only needs the ticket number and status.
                        "requestNumber": booking_result.get("requestNumber"),
                        "status": booking_result.get("status"),
                        "messageId": booking_result.get("messageId"),
                    },
                },
            )
        except Exception as e:  # noqa: BLE001
            log.exception("auto-book: P0 book call failed: %s", e)

    # ------ auto-escalate to a human agent on P0 ------
    #
    # Design rule: P0 is safety-critical, so we NEVER ask the driver to
    # confirm the handoff — we just do it, tell them, and Nova Sonic
    # narrates what's happening while the iOS client joins the Connect
    # chat in parallel. The `escalation` wire event carries the
    # participantToken iOS needs to join the chat WebSocket directly.
    #
    # Same Strands #2021 workaround as triage/book: we call the REST
    # endpoint directly from the server instead of going through a
    # registered tool, because Nova Sonic v2 doesn't reliably invoke
    # tools.
    #
    # ------ auto-escalate to a human agent on P0 ------
    #
    # Previously we awaited the /escalate call before injecting the P0
    # voice guidance. That added ~5s of silent waiting (Amazon Connect
    # StartChatContact is the long tail) between the driver finishing
    # their sentence and hearing "pull over + critical + connecting"
    # — observed 8.5s total end-to-end on 2026-05-08. Unacceptable for
    # a critical-safety-event reply.
    #
    # The escalate call doesn't need to be serialized with the voice
    # injection. The driver is told "connecting you to a live agent" as
    # part of the injected guidance REGARDLESS of whether Connect has
    # finished provisioning the chat yet — and iOS doesn't need the
    # escalation wire event to render the voice response. We fire
    # escalate as a background task so it runs in parallel with the
    # classifier injection → Nova Sonic → audio path. When it finishes
    # (typically ~3-5s later), the background task emits the
    # `escalation` wire event, and iOS opens the chat card at that
    # point. Net saving: ~5s from finish-of-user-utterance to start of
    # P0 voice reply.
    #
    # P1 and below are NOT auto-escalated here. Per voice_prompt.py
    # policy, P1 Nova Sonic offers the human handoff and waits for driver
    # confirmation — in that path the escalate_to_human tool gets invoked
    # via the text-mode supervisor flow (or will, once Nova Sonic v2
    # tool-calling becomes reliable). That's out of scope for the bidi
    # auto-injection path.
    # For the injection path below, treat P0 as "escalation is happening"
    # regardless of whether the actual Connect call has returned yet —
    # we're committed at classification time.
    escalation_result: dict[str, Any] | None = None
    escalate_task: asyncio.Task | None = None
    if classification == "P0":
        # Compose the chat-side diagnosis text that will ride along on
        # the escalation wire event so iOS can render a one-shot SYSTEM
        # bubble as soon as the Connect chat opens. Picks the first
        # catalog-mapped DTC from the primed active-DTC list; falls
        # back to empty string if no matched DTC (then iOS just shows
        # no diagnosis bubble, no harm done). Added 2026-05-11 — see
        # dtc_response_catalog.py for the per-DTC chat text rationale.
        chat_diagnosis = ""
        for _d in state.get("active_dtcs_for_prompt") or []:
            cd = (_d.get("chat_diagnosis") or "").strip()
            if cd:
                chat_diagnosis = cd
                break
        # Kick off the escalate call in the background. The task emits
        # wire events to iOS when it completes; we don't await.
        escalate_task = asyncio.create_task(
            _run_escalate_in_background(
                websocket=websocket,
                ctx=ctx,
                config=config,
                user_utterance=user_utterance,
                booking_result=booking_result,
                classification=classification,
                display_name=getattr(ctx, "driver_display_name", None),
                chat_diagnosis=chat_diagnosis,
            ),
            name=f"auto-escalate[{ctx.session_id}]",
        )

    # ------ inject combined system note to drive the voice response ------
    # Without priming, the only reliable way to keep the model on
    # vehicle-topic is to inject a system note for ALL classifications.
    # The note carries vehicle context and routing guidance. Previously
    # we skipped injection for P3 to avoid audio stutter, but that
    # allowed Nova Sonic to drift into generic "temperature could be
    # a device" hallucinations. Injection via agent.send interrupts
    # the model's in-flight response — which is exactly what we want
    # for P3 when the model has started to speculate about non-vehicle
    # causes.
    vehicle_ctx = state.get("vehicle_ctx") or {}
    vehicle_phrase = ""
    if vehicle_ctx.get("year") and vehicle_ctx.get("make") and vehicle_ctx.get("model"):
        vehicle_phrase = f" The driver's vehicle is a {vehicle_ctx['year']} {vehicle_ctx['make']} {vehicle_ctx['model']}."

    # For P2/P3: provide context, not scripted response. Let Nova decide
    # what to say and which tools to call.
    if classification in ("P2", "P3"):
        injection_parts = [
            f"Triage context:{vehicle_phrase} Classification: {classification} (not urgent, safe to drive)."
            f" Address the driver's specific concern about '{user_utterance}' first."
            f" Do NOT talk about unrelated vehicle faults unless the driver asks."
            f" If the concern is a quick-fix-eligible issue (low or under-inflated"
            f" tire pressure, washer fluid, wiper blades, cabin air filter, a blown"
            f" fuse, or low DEF) and the driver has NOT already asked for a shop or"
            f" appointment or to do it themselves, do NOT call find_service_center"
            f" yet and do NOT propose an appointment — offer BOTH paths in one short"
            f" sentence (\"I can book that at a shop, or walk you through doing it"
            f" yourself — which would you prefer?\") and wait for their choice."
            f" Otherwise, use find_service_center to look up a nearby location."
        ]
    else:
        # P0/P1: keep the directive injection (safety-critical)
        guidance = _classification_guidance(classification)
        injection_parts = [
            f"The vehicle's triage system assessed this situation.{vehicle_phrase} {guidance}"
        ]

    # No server-side service center or knowledge lookup for P2/P3 —
    # Nova calls find_service_center itself (visible in reasoning drawer).

    if booking_result is not None:
        ticket = booking_result.get("requestNumber", "unknown")
        injection_parts.append(
            f"A service ticket has been created and roadside dispatch has "
            f"been scheduled. Tell the driver help is on the way. "
            f"Do not read the ticket number ({ticket}) out loud."
        )
    if escalation_result is not None or classification == "P0":
        # On P0 we tell the driver the agent is being connected even if
        # the Connect StartChatContact hasn't returned yet — the
        # background escalate task is running in parallel and will
        # emit the wire event to iOS when it lands (~3-5s). Nova
        # Sonic's voice reply goes out immediately instead of waiting
        # on Connect. Driver experience: voice guidance lands ~3s
        # after user-final, chat card appears ~3-5s after that.
        rsa_phrase = " Roadside assistance has been dispatched to the driver's location."
        injection_parts.append(
            f"A human fleet-support agent is being connected to the driver "
            f"right now through the in-app chat. Tell the driver that a "
            f"support agent will be with them in a moment.{rsa_phrase} "
            f"Do not read contact or participant identifiers out loud."
        )
    injection = " ".join(injection_parts)
    log.info(
        "auto-triage: prepared %d-char injection for classification=%s",
        len(injection), classification,
    )

    # ------ driver-confirmed escalation divert ------
    # Before submitting the normal classifier-driven guidance, check if
    # the driver's utterance matched an emergency keyword category AND
    # the classifier's tier is non-urgent (P2/P3). If so, we have a
    # mismatch between what the driver said and what the sensors show,
    # and we can't just narrate P3 guidance or we'd under-triage a
    # potentially real emergency. Instead we ask a yes/no confirmation
    # question via IMMEDIATE injection (so it interrupts any in-flight
    # response) and record the pending-confirmation state. The next
    # final user transcript will be interpreted by the transcript
    # handler as yes/no/unclear and routed accordingly — see
    # _handle_confirmation_response.
    #
    # We only divert on P2/P3. If the classifier already returned P0/P1,
    # trust it: sensor-backed emergencies take precedence over the
    # ask-the-driver path, since we already have independent evidence.
    matched_category: EmergencyCategory | None = None
    if classification in ("P2", "P3"):
        matched_category = match_emergency_keywords(user_utterance)

    if matched_category is not None:
        # Suppress the normal classifier injection and substitute the
        # confirmation question.
        driver_first_name = getattr(ctx, "driver_first_name", None) or ""
        driver_name_prefix = (
            f"{driver_first_name}, " if driver_first_name else ""
        )
        confirmation_question = matched_category.confirmation_question.format(
            driver_name=driver_name_prefix
        )
        log.warning(
            "auto-triage: driver-confirmed-escalation divert session=%s "
            "classifier=%s matched_category=%s utterance=%r",
            ctx.session_id, classification, matched_category.name,
            user_utterance[:120],
        )
        # Record state so the next user transcript is parsed as a
        # confirmation response. The transcript handler in output_callable
        # is responsible for clearing these fields on resolution.
        state["awaiting_confirmation_category"] = matched_category
        state["awaiting_confirmation_keyword"] = user_utterance
        state["classifier_tier_before_confirmation"] = classification

        # IMMEDIATE priority so the question gets asked right away even
        # if Nova Sonic is in the middle of a non-urgent response. A
        # confirmation question interrupting a generic acknowledgment is
        # correct behavior — the driver needs to hear the question.
        await injection_queue.submit(
            InjectionRequest(
                payload=confirmation_question,
                priority=InjectionPriority.IMMEDIATE,
                reason=f"confirm-{matched_category.name}",
            )
        )
        # Mirror to the iOS reasoning drawer so it's visible that we
        # asked for confirmation rather than narrating the normal tier.
        await _safe_send_json(
            websocket,
            {
                "type": "tool.call",
                "name": "driver_confirmation",
                "input": {
                    "matchedCategory": matched_category.name,
                    "classifierTier": classification,
                    "question": confirmation_question,
                },
            },
        )
        return  # skip the normal submit below

    # ------ normal classifier-driven injection ------
    # Route through the injection queue. P0/P1 are safety-critical /
    # time-sensitive and their narration must preempt any other speech,
    # so they flush immediately (interrupting Nova Sonic mid-response is
    # the correct behavior). P2/P3 aren't urgent; defer them until the
    # current assistant turn finishes so the driver hears a complete
    # response instead of a mid-sentence cutoff.
    #
    # SKIP TEXT INJECTION ENTIRELY (2026-05-08).
    #
    # Nova Sonic v2 has a well-known timing quirk where text input sent
    # while its own audio stream is open gets queued and only processed
    # after an external trigger (keep-alive, timeout). We observed 30+
    # second silences between a P0 injection and Nova delivering the
    # guidance. The only reliable text-to-speech path we have is the
    # system prompt — which is loaded at session start and Nova reads
    # reliably.
    #
    # Text injection into Nova is unreliable (30s+ delays observed).
    # The prompt handles the voice narration. But we still need to run
    # the escalation/booking logic below for P0/P1.
    log.info(
        "auto-triage: prompt handles narration, continuing for escalation. "
        "session=%s classification=%s active_dtcs=%d",
        ctx.session_id, classification,
        len(state.get("active_dtcs_for_prompt") or []),
    )

    # P0/P1: IMMEDIATE injection (safety-critical, must override Nova).
    # P2/P3: DEFERRED injection only when there's actually new
    # information (booking confirmation or escalation in flight). For
    # plain P2/P3 cases — the prompt already handles narration; piling
    # an extra "Triage context: ..." text input on top of Nova when
    # it's already chewing on the user's audio + text is what was
    # producing the stuck state. Observed 2026-05-21 16:02 UTC: demo
    # clip "tire pressure is low" → audio + text hit Nova → Nova
    # silent → turn-watchdog cleared at 3s → DEFERRED triage payload
    # dispatched (320 chars) → Nova received tokens but never started
    # contentStart for an assistant response → 55s session timeout.
    # Without the DEFERRED payload, Nova has just two inputs (the
    # demo audio's STT'd transcript + the deterministic text.input)
    # to reconcile — same shape as session-start text.input which
    # works reliably.
    if classification in ("P0", "P1"):
        priority = InjectionPriority.IMMEDIATE
        should_enqueue = True
    elif booking_result is not None or escalation_result is not None:
        # P2/P3 path that DID produce server-side action — Nova needs
        # to be told the booking/escalation happened so it can narrate
        # the confirmation. Use DEFERRED so it only fires if Nova went
        # silent (otherwise the prompt rule fires and the injection
        # would just be a duplicate).
        priority = InjectionPriority.DEFERRED
        should_enqueue = True
    else:
        # P2/P3 with no server-side action — let the prompt drive the
        # response. No injection.
        log.info(
            "auto-triage: skipping DEFERRED injection for non-urgent classification=%s "
            "(no booking, no escalation) — prompt handles narration",
            classification,
        )
        return

    reason_parts = [f"triage-{classification or 'unclassified'}"]
    if booking_result is not None:
        reason_parts.append("book")
    if escalation_result is not None:
        reason_parts.append("escalate")
    reason = "+".join(reason_parts)

    await injection_queue.submit(
        InjectionRequest(payload=injection, priority=priority, reason=reason)
    )


async def _handle_confirmation_response(
    websocket,
    ctx: SessionContext,
    config: Config,
    user_utterance: str,
    injection_queue: "AgentInjectionQueue",
    state: dict,
) -> None:
    """Process the driver's yes/no/unclear answer to an emergency-
    confirmation question.

    Called when state["awaiting_confirmation_category"] is set and a
    new user-final transcript arrives. Three branches:

      yes     -> escalate to the category's target tier (P0 today for
                 all five categories). Write a driver-confirmed audit
                 record. Submit the P0 narration as an IMMEDIATE
                 injection. P0 auto-book / escalation hooks run here too
                 so Kevin-and-roadside get notified.

      no      -> stand down. Fall back to the classifier's original
                 tier (P2/P3) by submitting the normal guidance. Audit
                 records the false alarm so the keyword list can be
                 tuned.

      unclear -> escalate conservatively to P1. Per design discussion
                 2026-05-02, an ambiguous confirmation is safer to
                 escalate than to downgrade. Human fleet-support is
                 invoked (not roadside/P0 hazards), driver continues
                 driving, agent picks up the thread. Audit records the
                 unclear response verbatim.

    State is cleared at the top of every branch so a subsequent user
    turn is treated normally, not as another confirmation answer.

    Audit records are best-effort DynamoDB writes — we swallow write
    failures so a DDB hiccup can't block the safety-critical narration
    that follows. The record is extra visibility; the narration is the
    product.
    """
    category: EmergencyCategory = state["awaiting_confirmation_category"]
    original_keyword_utterance: str = state.get("awaiting_confirmation_keyword") or ""
    original_classification: str = (
        state.get("classifier_tier_before_confirmation") or "P3"
    )

    # Clear state IMMEDIATELY so any racing user transcript (unlikely in
    # practice — Nova Sonic serializes per-turn — but defensive) doesn't
    # double-fire this handler.
    state["awaiting_confirmation_category"] = None
    state["awaiting_confirmation_keyword"] = None
    state["classifier_tier_before_confirmation"] = None

    verdict = parse_confirmation_response(user_utterance)
    log.warning(
        "driver-confirm: session=%s category=%s verdict=%s "
        "original_tier=%s response=%r",
        ctx.session_id, category.name, verdict,
        original_classification, user_utterance[:120],
    )

    if verdict == "yes":
        final_tier = category.target_tier_on_confirm  # "P0" today for all
        guidance = _classification_guidance(final_tier)
        payload = (
            f"The driver confirmed: {category.rationale} "
            f"Treat this as priority {final_tier}. {guidance}"
        )
        await injection_queue.submit(
            InjectionRequest(
                payload=payload,
                priority=InjectionPriority.IMMEDIATE,
                reason=f"driver-confirmed-{category.name}-{final_tier}",
            )
        )
        # Mirror updated classification to iOS so the reasoning drawer
        # reflects the final tier (not the original P3) and shows the
        # driver-confirmed origin.
        await _safe_send_json(
            websocket,
            {
                "type": "classification",
                "value": final_tier,
                "source": "driver-confirmed",
                "category": category.name,
            },
        )

    elif verdict == "no":
        # False alarm. Fall back to the classifier's original tier by
        # submitting the normal guidance inline — we don't re-run the
        # classifier because its inputs (sensor data) haven't changed.
        final_tier = original_classification
        guidance = _classification_guidance(final_tier)
        payload = (
            f"The driver confirmed the situation is not an emergency. "
            f"Continue normally. {guidance}"
        )
        # DEFERRED is fine here — nothing urgent. If Nova Sonic is mid-
        # "that was a false alarm" response, let it finish first.
        await injection_queue.submit(
            InjectionRequest(
                payload=payload,
                priority=InjectionPriority.DEFERRED,
                reason=f"driver-declined-{category.name}-{final_tier}",
            )
        )

    else:  # unclear
        final_tier = "P1"
        guidance = _classification_guidance(final_tier)
        payload = (
            f"The driver's response was not a clear yes or no after a "
            f"safety confirmation. Treat this as priority P1 out of "
            f"caution. {guidance}"
        )
        await injection_queue.submit(
            InjectionRequest(
                payload=payload,
                priority=InjectionPriority.IMMEDIATE,
                reason=f"driver-unclear-{category.name}-P1",
            )
        )
        # Mirror the new P1 classification to iOS.
        await _safe_send_json(
            websocket,
            {
                "type": "classification",
                "value": final_tier,
                "source": "driver-unclear-default",
                "category": category.name,
            },
        )

    # ------ audit record ------
    # Best-effort DDB write. Never blocks the narration. Lives in the
    # existing triage-decisions table with a confirmationSource marker
    # so fleet-ops dashboards can filter driver-confirmed escalations
    # out of sensor-backed ones.
    try:
        await _write_driver_confirmation_audit(
            ctx=ctx,
            category=category,
            original_utterance=original_keyword_utterance,
            response_utterance=user_utterance,
            verdict=verdict,
            original_classification=original_classification,
            final_classification=final_tier,
        )
    except Exception as e:  # noqa: BLE001
        log.exception(
            "driver-confirm: audit write failed session=%s category=%s: %s",
            ctx.session_id, category.name, e,
        )


async def _write_driver_confirmation_audit(
    *,
    ctx: SessionContext,
    category: EmergencyCategory,
    original_utterance: str,
    response_utterance: str,
    verdict: str,
    original_classification: str,
    final_classification: str,
) -> None:
    """Write a DynamoDB row capturing the driver-confirmed escalation
    (or false alarm) for audit. Kept intentionally minimal — shape
    mirrors the existing triage-decisions table so the same log/
    dashboard infrastructure can see both sensor-backed and driver-
    confirmed decisions, distinguished by confirmationSource=driver.

    Table selection: writes to the existing VSA_TRIAGE_DECISIONS_TABLE
    (same one the triage-classifier Lambda writes to) so there's one
    canonical place to query all triage outcomes per vehicle. Requires
    no new table provisioning.

    Non-blocking: this runs in the same asyncio task as the confirmation
    handler, but the caller catches exceptions so a DDB failure doesn't
    prevent the driver from hearing the P0 narration.
    """
    loop = asyncio.get_running_loop()
    # Table name comes from env (matches the classifier Lambda's env var)
    # with a safe default of the known prod table — the AgentCore runtime
    # doesn't currently set environment variables at deploy time, so
    # defaulting to the known name is how the audit path actually works.
    # If the table name changes, update both this default AND the
    # classifier Lambda env var.
    table_name = os.environ.get(
        "VSA_TRIAGE_DECISIONS_TABLE", "vsa-prod-triage-decisions"
    )

    from datetime import datetime, timezone

    decided_at = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )
    # Use a derived sessionId so this record doesn't overwrite the
    # classifier's decision record (same ctx.session_id). Suffix marks
    # it as a driver-confirmed follow-up; queries can filter by
    # confirmationSource="driver" or by the suffix.
    audit_session_id = f"{ctx.session_id}-driver-confirmed"
    item = {
        "sessionId": audit_session_id,
        "parentSessionId": ctx.session_id,
        "tenantId": ctx.tenant_id,
        "vin": ctx.vin or "",
        "vehicleId": ctx.vehicle_id or "",
        "driverId": ctx.driver_id or "",
        "classification": final_classification,
        "classifierVersion": "driver-confirmed-1.0.0",
        "decidedAt": decided_at,
        "confirmationSource": "driver",
        "driverConfirmationCategory": category.name,
        "driverConfirmationVerdict": verdict,  # yes | no | unclear
        "originalClassification": original_classification,
        "originalUtterance": original_utterance[:1000],
        "responseUtterance": response_utterance[:1000],
        "sensorCorroboration": "none",
        "rationale": category.rationale,
    }

    def _put() -> None:
        client = _get_cms_ddb()
        table = client.Table(table_name)
        table.put_item(Item=item)

    await loop.run_in_executor(None, _put)
    log.info(
        "driver-confirm: audit written session=%s category=%s verdict=%s "
        "final_tier=%s table=%s",
        ctx.session_id, category.name, verdict, final_classification, table_name,
    )


def _classification_guidance(classification: str | None) -> str:
    """Return a short narration guidance string for each criticality tier."""
    if classification == "P0":
        return (
            "This is a critical safety situation. Speak three short sentences "
            "in order: (1) tell the driver to pull over safely and turn on "
            "their hazard lights, (2) tell them this is critical and you are "
            "connecting them to a live fleet-support agent right now through "
            "the in-app chat, (3) tell them roadside assistance has been "
            "dispatched. Do not say the code 'P0'; say 'critical' instead."
        )
    if classification == "P1":
        return (
            "This requires service within 48 hours. Tell the driver the "
            "priority level, offer to warm-transfer them to fleet ops for "
            "tier-1 SLA customers, and ask if they want to book now. Keep "
            "the response to three short sentences."
        )
    if classification == "P2":
        return (
            "This is not urgent and can wait for the next scheduled service. "
            "Acknowledge the symptom, reassure the driver it's safe to continue "
            "driving, and offer to schedule a convenient appointment. Do NOT say "
            "'P2' or any priority code out loud. Three short sentences."
        )
    if classification == "P3":
        return (
            "This is a monitor-only signal. Tell the driver what 'monitor' "
            "means for this symptom, no ticket needed unless they insist. "
            "Two short sentences."
        )
    return (
        "Acknowledge the driver's concern and ask one clarifying question. "
        "Keep it brief."
    )


async def _warm_classifier(ctx: SessionContext, config: Config) -> None:
    """Pre-warm the triage classifier Lambda container at session start.

    Hits the deployed /triage endpoint with the session's own VIN so the
    classifier's VPC lookups + VIN→vehicleId cache are populated before
    the driver's first utterance lands. Throws away the result; we only
    care that the Lambda container is hot. Failures are logged and
    swallowed — this is a best-effort optimization, not a correctness
    path.

    Intentionally does NOT use `call_triage_endpoint` even though that's
    a ready-made helper: it mutates `ctx.triage_classification`, which
    is observed by the bidi output loop and mirrored to the iOS client
    as a `{type: "classification", value: P0}` wire event. That fires
    the reasoning drawer before the driver has even spoken, surfacing
    a false "classification: P0" entry. We do a direct POST here so
    the ctx stays clean until the real auto-triage call.
    """
    if not getattr(ctx, "vin", None):
        return
    try:
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(
            None,
            lambda: _raw_triage_post(ctx, config),
        )
        log.info(
            "classifier-prewarm: done session=%s lat=%sms",
            ctx.session_id, result.get("latencyMs") if result else "n/a",
        )
    except Exception as e:  # noqa: BLE001
        log.warning(
            "classifier-prewarm: failed session=%s (%s) — proceeding anyway",
            ctx.session_id, type(e).__name__,
        )


def _raw_triage_post(ctx: SessionContext, config: Config) -> dict[str, Any] | None:
    """Fire a classifier call purely for warm-up side effects; do NOT
    touch ctx.triage_classification. Returns parsed JSON on success or
    None on failure — caller doesn't care either way."""
    import requests
    try:
        resp = requests.post(
            f"{config.api_base}/triage",
            json={
                "vin": ctx.vin,
                "sessionId": ctx.session_id,
                "tenantId": ctx.tenant_id,
            },
            headers={
                "Authorization": f"Bearer {ctx.jwt}",
                "Content-Type": "application/json",
            },
            timeout=10,
        )
        if resp.status_code >= 400:
            return None
        return resp.json()
    except Exception:  # noqa: BLE001
        return None


async def _run_escalate_in_background(
    *,
    websocket,
    ctx: SessionContext,
    config: Config,
    user_utterance: str,
    booking_result: dict[str, Any] | None,
    classification: str,
    display_name: str | None = None,
    chat_diagnosis: str = "",
) -> None:
    """Run /escalate on a background asyncio task and emit iOS wire events.

    Previously the main auto-triage coroutine awaited /escalate before
    submitting the voice-guidance injection to Nova Sonic, adding ~5s of
    silent waiting (Amazon Connect StartChatContact is the slow tail).
    By running this in the background, the voice path gets its injection
    as soon as classification + booking complete; iOS gets the chat card
    whenever Connect's call returns, ~3-5s later, without blocking audio.

    Errors are swallowed and surfaced to iOS via a `{status:"failed"}`
    escalation wire event so the UI can stop spinning on
    "connecting to agent…" — this is a fire-and-forget task; no caller
    will await the result.
    """
    loop = asyncio.get_running_loop()
    try:
        escalation_result = await loop.run_in_executor(
            None,
            lambda: call_escalate_endpoint(
                ctx,
                config,
                severity="P0",
                reason=user_utterance[:280] or "P0 auto-escalation",
                # Same short-summary contract as before so Kevin sees
                # context without replaying audio.
                summary=_build_escalation_summary(
                    classification=classification,
                    user_utterance=user_utterance,
                    booking_result=booking_result,
                ),
                # Explicit so a future Lambda default flip doesn't
                # silently disable roadside dispatch.
                dispatch_roadside=True,
                display_name=display_name,
            ),
        )
        log.info(
            "auto-escalate (bg): P0 contact=%s rsaDispatched=%s",
            escalation_result.get("contactId"),
            escalation_result.get("rsaDispatched"),
        )

        # Emit the escalation side-channel event so iOS can join the
        # Connect chat WebSocket. Flat payload keyed off by iOS decoder.
        log.info(
            "auto-escalate (bg): emitting escalation event contact=%s chatDiagnosis=%s",
            escalation_result.get("contactId"),
            ("(len=" + str(len(chat_diagnosis)) + ")") if chat_diagnosis else "(empty)",
        )
        await _safe_send_json(
            websocket,
            {
                "type": "escalation",
                "status": "initiated",
                "severity": escalation_result.get("severity", "P0"),
                "contactId": escalation_result.get("contactId"),
                "participantId": escalation_result.get("participantId"),
                "participantToken": escalation_result.get("participantToken"),
                "connectionExpiry": escalation_result.get("connectionExpiry"),
                "rsaDispatched": bool(escalation_result.get("rsaDispatched")),
                "rsaActionId": escalation_result.get("rsaActionId"),
                # Optional: plain-text diagnosis suitable for a SYSTEM
                # bubble in the Connect chat. iOS renders it as a local
                # message (NOT sent to Connect) so the driver sees the
                # fault context while waiting for the live agent.
                # Empty string when no active DTC is catalog-mapped.
                "chatDiagnosis": chat_diagnosis,
            },
        )

        # Also mirror the escalation as tool.call / tool.result for the
        # reasoning drawer. Token stripped from the drawer — sensitive.
        await _safe_send_json(
            websocket,
            {
                "type": "tool.call",
                "name": "escalate_to_human",
                "input": {
                    "severity": "P0",
                    "reason": user_utterance[:280],
                    "dispatch_roadside": True,
                },
            },
        )
        await _safe_send_json(
            websocket,
            {
                "type": "tool.result",
                "name": "escalate_to_human",
                "output": {
                    "contactId": escalation_result.get("contactId"),
                    "severity": escalation_result.get("severity"),
                    "rsaDispatched": escalation_result.get("rsaDispatched"),
                    "rsaActionId": escalation_result.get("rsaActionId"),
                    "summary": escalation_result.get("summary"),
                },
            },
        )
    except EscalateError as e:
        log.exception("auto-escalate (bg): P0 escalate call failed: %s", e)
        await _safe_send_json(
            websocket,
            {
                "type": "escalation",
                "status": "failed",
                "severity": "P0",
                "message": str(e)[:200],
            },
        )
    except Exception as e:  # noqa: BLE001
        log.exception("auto-escalate (bg): unexpected failure: %s", e)
        await _safe_send_json(
            websocket,
            {
                "type": "escalation",
                "status": "failed",
                "severity": "P0",
                "message": f"internal-error: {type(e).__name__}",
            },
        )


def _build_escalation_summary(
    *,
    classification: str | None,
    user_utterance: str,
    booking_result: dict[str, Any] | None,
) -> str:
    """Compose the `summary` string we send to /escalate.

    This string is what Kevin sees on his Connect agent view as the
    initial-message context for the chat — it has to be scannable in a
    couple of seconds. Keep it compact: priority line, driver-words
    symptom, and (for P0) the booking/ticket info.

    Capped below the /escalate Lambda's summary limits (~4KB) with
    plenty of headroom; realistic content is a few hundred bytes.
    """
    lines: list[str] = []
    lines.append(f"Triage: {classification or 'unknown'} (auto-classified)")
    if user_utterance:
        driver_quote = user_utterance[:400].strip()
        lines.append(f"Driver reported: \"{driver_quote}\"")
    if booking_result is not None:
        req = booking_result.get("requestNumber") or "unknown"
        status = booking_result.get("status") or "unknown"
        lines.append(f"Service ticket: {req} (status={status})")
    return "\n".join(lines)


if __name__ == "__main__":
    # Local run: python -m agents.supervisor.bidi_app
    # Binds WebSocket to ws://localhost:8080/ws per AgentCore convention.
    app.run(log_level="info")
