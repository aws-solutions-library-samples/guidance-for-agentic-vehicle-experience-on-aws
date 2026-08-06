"""Canonical-pattern Nova Sonic + Strands BidiAgent voice runtime.

Spike branch: trying the AWS-canonical pattern (~150 lines in the
upstream sample) layered with our actual tools and tenant context.
Strips out everything we added that doesn't appear in
aws-samples/sample-nova-sonic-websocket-agentcore or the AWS blog
"Scalable voice agent design with Amazon Nova Sonic".

What this DOESN'T have (compare to bidi_app.py):
  * No AgentInjectionQueue / IMMEDIATE / DEFERRED.
  * No auto-triage parallel classifier — triage is a normal @tool.
  * No synthetic transcript injection.
  * No turn watchdog, echo gate watchdog, speakingIdleTimeout.
  * No `_auto_book_on_affirmation` server-side fallback.
  * No multi-thousand-character "CRITICAL — YOU MUST" prompt rules.
  * No retry / reconnect logic on the agent side.

What this DOES have (kept from bidi_app.py):
  * Same WebSocket wire format with iOS — `audio.chunk` /
    `text.input` in, `audio.chunk` / `transcript` / `tool.call` /
    `tool.result` out — so iOS rebuild isn't required.
  * Same tenant/vehicle/driver context resolution — tools need it.
  * Same tool factories — find_service_center, book, escalate,
    knowledge, parts_lookup, vehicle_identity, retrieve.

The shape of the WebSocket handler matches the canonical sample:
build a BidiAgent, call `agent.run(inputs=[...], outputs=[...])`
with thin adapter callables that translate between iOS' wire
format and Strands' bidi event format. That's it.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from typing import Any

from bedrock_agentcore.runtime import BedrockAgentCoreApp
from bedrock_agentcore.runtime.context import RequestContext
from strands.experimental.bidi.agent import BidiAgent
from strands.experimental.bidi.models.nova_sonic import (
    BidiNovaSonicModel,
    NOVA_SONIC_V2_MODEL_ID,
)

from agents.supervisor.config import Config
from agents.supervisor.session import SessionContext
from agents.supervisor.tools import (
    make_book_tool,
    make_diy_repair_advisor_tool,
    make_escalate_tool,
    make_knowledge_tool,
    make_parts_lookup_tool,
    make_retrieve_tool,
    make_service_network_tool,
    make_vehicle_identity_tool,
)

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logging.getLogger("strands.experimental.bidi").setLevel(logging.INFO)
logging.getLogger("strands.experimental.bidi.models.nova_sonic").setLevel(logging.INFO)

app = BedrockAgentCoreApp()

# Wire-protocol constants. Match the existing iOS client.
INPUT_SAMPLE_RATE = 16000
OUTPUT_SAMPLE_RATE = 24000
DEFAULT_VOICE = "matthew"

# Headers iOS sends with the connect (same names as bidi_app.py so the
# iOS side doesn't change).
USER_TOKEN_HEADER = "x-amzn-bedrock-agentcore-runtime-custom-user-token"
TENANT_QUERY_PARAM = "x-amzn-bedrock-agentcore-runtime-custom-tenant-id"
VIN_QUERY_PARAM = "x-amzn-bedrock-agentcore-runtime-custom-vin"
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
LATITUDE_QUERY_PARAMS = (
    "x-amzn-bedrock-agentcore-runtime-custom-latitude",
)
LONGITUDE_QUERY_PARAMS = (
    "x-amzn-bedrock-agentcore-runtime-custom-longitude",
)


# ---------------------------------------------------------------------------
# System prompt — short by design.
# ---------------------------------------------------------------------------
#
# The AWS canonical sample uses ~1 sentence. The "scalable voice agent"
# blog uses 3-line prompts per sub-agent. Long prompts with multiple
# "CRITICAL — YOU MUST" sections compete with each other and confuse
# Nova. Vehicle context is interpolated at session start.
SYSTEM_PROMPT = """\
You are a fleet support voice assistant for {tenant_display}. You are on \
a live call with {driver_phrase}driving {vehicle_phrase}. Keep replies short \
— one sentence, natural for voice. \
\
You ALREADY KNOW the driver's vehicle. NEVER ask for the VIN, license \
plate, year, make, model, or any vehicle identifier. NEVER ask the \
driver to repeat their name. Their vehicle and identity are in this \
prompt above and the tools have them via session context — you do not \
need them as user input. \
\
You can call these tools when appropriate: \
- find_service_center: locate a nearby shop for a vehicle issue. \
- book: schedule an appointment after the driver confirms a center. \
- escalate_to_human: hand off to a human agent for emergencies. \
- diy_repair_advisor: when the driver asks if they can fix something \
themselves, wants step-by-step guidance, or asks how to handle a \
warning that might be a quick DIY (e.g. "Can I top this off myself?", \
"Walk me through it", "Is this something I can handle?"). This tool \
returns a `reference_material` field with owner-manual content. \
\
HARD LIMIT: your response after this tool MUST be 2 sentences MAXIMUM. \
That is a strict cap, not an aspiration. Pick the SINGLE most useful \
fact (e.g. the recommended pressure for this specific vehicle) plus \
ONE short safety/next-step note. NEVER list the full step-by-step. \
NEVER read multiple paragraphs. NEVER recite the whole manual. If you \
catch yourself starting a third sentence, stop. End with "Anything \
else I can help with?". \
\
SHORTER VOICE WHEN INFO IS ON SCREEN: If the tool result contains a \
`chat_message` field, the detailed data is being shown on the \
driver's screen as a card. Your voice response in that case must be \
ONE short sentence pointing at the card, like "I've put the pressures \
on your screen — front and rear are 35 PSI." Do not read the table \
aloud. Do not list each row. \
\
ALWAYS OFFER SERVICE AS AN ALTERNATIVE: After any DIY explanation, \
finish by offering service as a fallback. Use a phrase like "If \
you'd rather have it checked at a shop, just let me know" or "Want \
me to book a service appointment instead?". This goes BEFORE the \
"Anything else I can help with?" closer. The driver may decide \
they don't want to DIY after hearing about it. \
- knowledge / parts_lookup / vehicle_identity / retrieve: answer factual questions. \
\
When a tool returns a result that includes a `narration` field, speak \
the narration field VERBATIM as your next reply. Do not paraphrase, do \
not add. The narration field already contains the right phrasing for \
voice. \
\
ROUTING — DIY OR SERVICE: \
\
STEP 1 — Check the driver's first message for explicit intent words. \
If you hear ANY of these DIY signals, you MUST call diy_repair_advisor \
(NOT find_service_center): "myself", "by myself", "do it myself", \
"fix it myself", "inflate it myself", "top it off myself", "walk me \
through", "show me how", "do it on my own", "handle it myself", \
"can I just". \
If you hear ANY of these SERVICE signals, you MUST call \
find_service_center: "book service", "find a shop", "schedule \
appointment", "find a service center", "take it in". \
\
STEP 2 — If neither set of signals is present and the issue is \
quick-fix-eligible (low tire pressure, washer fluid, cabin filter, \
fuse, low DEF), briefly offer BOTH paths in one short sentence: \
"I can book service for you, or walk you through doing it yourself \
— which sounds better?" Wait for the driver's answer, then go back \
to STEP 1 with their reply. \
\
STEP 3 — For non-quick-fix issues (engine codes, brake faults, ABS, \
airbag, oil pressure warnings, anything red on the dash), go straight \
to find_service_center. Those need a professional, no DIY. \
\
{active_dtcs_block}\
\
When the driver mentions a check engine light or a vehicle symptom, \
explain the active fault codes from the KNOWN ACTIVE FAULTS section \
above in PLAIN LANGUAGE — never read the raw code aloud. For example, \
"Your turbocharger has low boost pressure, which is why the truck \
feels sluggish" — NOT "code P0299". After the explanation, call \
find_service_center directly and speak its `narration` field verbatim. \
\
After the driver affirms ("yes", "sure", "go ahead", "please book \
that"), call `book` directly with the booking details — do NOT \
narrate any preamble like "let me check that" or "one moment". Just \
call the tool, then speak its `narration` field. Never end your turn \
silent."""


def _render_system_prompt(
    *,
    tenant_display: str,
    driver_first: str | None,
    vehicle_year: int | str | None,
    vehicle_make: str | None,
    vehicle_model: str | None,
    active_dtcs: list[dict[str, Any]] | None,
) -> str:
    driver_phrase = f"{driver_first}, " if driver_first else ""
    vehicle_phrase = " ".join(
        str(p) for p in (vehicle_year, vehicle_make, vehicle_model) if p
    ) or "a fleet vehicle"
    if active_dtcs:
        codes = ", ".join(
            f"{d.get('code')} ({(d.get('severity') or '').upper()}, {d.get('description', '')})"
            for d in active_dtcs[:5]
            if d.get("code")
        )
        active_dtcs_block = f"Known active fault codes on this vehicle: {codes}.\n\n"
    else:
        active_dtcs_block = ""
    return SYSTEM_PROMPT.format(
        tenant_display=tenant_display or "the fleet",
        driver_phrase=driver_phrase,
        vehicle_phrase=vehicle_phrase,
        active_dtcs_block=active_dtcs_block,
    )


# ---------------------------------------------------------------------------
# WebSocket handler
# ---------------------------------------------------------------------------


class _SessionEnd(Exception):
    """Raised by the input adapter when the client cleanly ends the session."""


@app.websocket
async def websocket_handler(websocket, context: RequestContext) -> None:
    await websocket.accept()

    # Resolve session identity from custom headers. RequestContext exposes
    # them under `request_headers` (NOT `headers` — that attribute returns
    # an empty dict in the AgentCore Runtime context).
    raw_headers: dict[str, Any] = {}
    try:
        raw_headers = dict(getattr(context, "request_headers", {}) or {})
    except Exception:  # noqa: BLE001
        raw_headers = {}

    # Normalize header keys to lowercase since AgentCore's case
    # handling between client → runtime is inconsistent (we've
    # observed `VehicleId`, `vehicleid`, and `vehicle-id` in
    # different deploys).
    norm_headers = {str(k).lower(): v for k, v in raw_headers.items()}

    def _hdr(*names: str) -> str | None:
        for n in names:
            v = norm_headers.get(n.lower())
            if v:
                return str(v)
        return None

    session_id = context.session_id or f"vsa-canonical-{uuid.uuid4().hex[:12]}"
    tenant_id = _hdr(TENANT_QUERY_PARAM)
    vin = _hdr(VIN_QUERY_PARAM)
    vehicle_id = _hdr(*VEHICLE_ID_QUERY_PARAMS)
    driver_id = _hdr(*DRIVER_ID_QUERY_PARAMS)
    jwt = _hdr(USER_TOKEN_HEADER) or ""
    lat_str = _hdr(*LATITUDE_QUERY_PARAMS)
    lng_str = _hdr(*LONGITUDE_QUERY_PARAMS)

    custom_keys = sorted(
        k for k in norm_headers.keys()
        if k.startswith("x-amzn-bedrock-agentcore-runtime-custom-")
    )
    log.info("canonical: received custom header keys: %s", custom_keys)

    # session.start fallback: if any required field is missing from
    # headers, read the first WS message — iOS sends a session.start
    # frame as a redundant fallback path for exactly this reason
    # (it's a no-op when headers already resolved). Bound by 10s so
    # silent clients don't hang us.
    if not tenant_id or not vin or not jwt:
        try:
            start_msg = await asyncio.wait_for(
                websocket.receive_json(), timeout=10.0
            )
        except asyncio.TimeoutError:
            log.warning(
                "canonical: timed out waiting for session.start fallback "
                "(no headers either) — closing"
            )
            await _safe_send_json(websocket, {
                "type": "error",
                "message": "Missing session identity (tenantId/vin/jwt).",
            })
            await _safe_close(websocket, code=4400, reason="missing-session-identity")
            return
        if isinstance(start_msg, dict) and start_msg.get("type") == "session.start":
            tenant_id = tenant_id or start_msg.get("tenantId")
            vin = vin or start_msg.get("vin")
            jwt = jwt or (start_msg.get("jwt") or "")
            vehicle_id = vehicle_id or start_msg.get("vehicleId") or None
            driver_id = driver_id or start_msg.get("driverId") or None
            if lat_str is None and start_msg.get("latitude") is not None:
                lat_str = str(start_msg.get("latitude"))
            if lng_str is None and start_msg.get("longitude") is not None:
                lng_str = str(start_msg.get("longitude"))
            log.info(
                "canonical: resolved identity from session.start fallback "
                "tenant=%s vin=%s vehicle_id=%s driver_id=%s",
                tenant_id, vin, vehicle_id, driver_id,
            )
        else:
            log.warning(
                "canonical: first WS message was not session.start — closing"
            )
            await _safe_send_json(websocket, {
                "type": "error",
                "message": "First message must be session.start with tenantId/vin/jwt",
            })
            await _safe_close(websocket, code=4400, reason="bad-first-message")
            return

    if not tenant_id or not vin:
        log.warning("canonical: missing tenant_id (%r) or vin (%r) — closing", tenant_id, vin)
        await _safe_send_json(websocket, {
            "type": "error",
            "message": "Missing tenantId or vin.",
        })
        await _safe_close(websocket, code=4400, reason="missing-tenant-or-vin")
        return

    try:
        client_lat = float(lat_str) if lat_str else None
    except ValueError:
        client_lat = None
    try:
        client_lng = float(lng_str) if lng_str else None
    except ValueError:
        client_lng = None
    # Reject (0, 0) "Null Island" coordinates — iOS sometimes sends 0
    # before its location service is ready, and 0,0 produces nonsense
    # find_service_center results.
    if client_lat == 0.0:
        client_lat = None
    if client_lng == 0.0:
        client_lng = None

    log.info(
        "canonical: accept session=%s tenant=%s vin=%s vehicle_id=%s "
        "driver_id=%s client_lat=%s client_lng=%s",
        session_id, tenant_id, vin, vehicle_id, driver_id,
        client_lat, client_lng,
    )

    config = Config.from_env()

    # Fallback: if iOS didn't push fresh coords (liveState wasn't loaded
    # yet — common when the user opens the assistant drawer before
    # visiting the Vehicle tab), pull last known position from the
    # live-state endpoint. Same source-of-truth the iOS Vehicle map
    # uses, so voice and visual stay consistent. Best-effort — if the
    # endpoint is unreachable we just leave coords None and tools fall
    # back to scan-order results.
    if (client_lat is None or client_lng is None) and vehicle_id and jwt:
        try:
            from agents.supervisor.bidi_app import _fetch_live_vehicle_location
            live_loc = _fetch_live_vehicle_location(
                vehicle_id, jwt, config.api_base,
            )
            fallback_lat = live_loc.get("latitude")
            fallback_lng = live_loc.get("longitude")
            if fallback_lat is not None and fallback_lng is not None:
                client_lat = float(fallback_lat)
                client_lng = float(fallback_lng)
                log.info(
                    "canonical: location fallback from live-state "
                    "lat=%s lng=%s session=%s",
                    client_lat, client_lng, session_id,
                )
        except Exception as e:  # noqa: BLE001
            log.warning(
                "canonical: location fallback failed for vehicle_id=%s: %s",
                vehicle_id, e,
            )

    ctx = SessionContext(
        tenant_id=tenant_id,
        session_id=session_id,
        jwt=jwt,
        persona_id="fleet_driver",
        vin=vin,
        vehicle_id=vehicle_id,
        driver_id=driver_id,
        client_latitude=client_lat,
        client_longitude=client_lng,
    )

    # Resolve display context for the system prompt. We re-use the same
    # helpers bidi_app.py uses so behaviour matches one-for-one. Wrapped
    # in best-effort try/except — if any of these fail the agent still
    # works, the prompt just has less personalisation.
    vehicle_ctx: dict[str, Any] = {}
    driver_ctx: dict[str, Any] = {}
    active_dtcs: list[dict[str, Any]] = []
    tenant_display = tenant_id
    try:
        from agents.supervisor.bidi_app import (  # type: ignore
            _fetch_vehicle_context,
            _fetch_driver_context,
            _fetch_active_dtcs,
            _fetch_tenant_segment,
        )
        if vehicle_id:
            vehicle_ctx = _fetch_vehicle_context(vehicle_id) or {}
        if driver_id:
            driver_ctx = _fetch_driver_context(driver_id) or {}
        if vehicle_id:
            active_dtcs = _fetch_active_dtcs(vehicle_id) or []
        # Tenant segment is used by the find_service_center filter.
        ctx.tenant_segment = _fetch_tenant_segment(tenant_id)
    except Exception as e:  # noqa: BLE001
        log.warning("canonical: context resolution partial-fail: %s", e)

    # If iOS supplied fresh coords, mirror them into vehicle_ctx so tools
    # see one consistent location source.
    if client_lat is not None:
        vehicle_ctx["latitude"] = client_lat
    if client_lng is not None:
        vehicle_ctx["longitude"] = client_lng

    system_prompt = _render_system_prompt(
        tenant_display=tenant_display,
        driver_first=driver_ctx.get("firstName"),
        vehicle_year=vehicle_ctx.get("year"),
        vehicle_make=vehicle_ctx.get("make"),
        vehicle_model=vehicle_ctx.get("model"),
        active_dtcs=active_dtcs,
    )
    log.info(
        "canonical: prompt rendered chars=%d tenant=%s driver=%s vehicle=%s active_dtcs=%d",
        len(system_prompt), tenant_display,
        driver_ctx.get("firstName"),
        f"{vehicle_ctx.get('year')} {vehicle_ctx.get('make')} {vehicle_ctx.get('model')}".strip(),
        len(active_dtcs),
    )

    # Build the model with audio rates that match iOS.
    sonic_model = BidiNovaSonicModel(
        model_id=NOVA_SONIC_V2_MODEL_ID,
        provider_config={
            "audio": {
                "voice": DEFAULT_VOICE,
                "input_rate": INPUT_SAMPLE_RATE,
                "output_rate": OUTPUT_SAMPLE_RATE,
            },
        },
        client_config={"region": config.aws_region or "us-east-1"},
    )

    # Register tools — same factories as the full bidi_app.py.
    # Note: triage is intentionally NOT registered here. The
    # canonical pattern lets Nova call tools naturally, but Nova
    # was observed stalling at "I need to check severity first"
    # without actually calling triage when book required it.
    # book defaults to P3 internally if no triage was run, so
    # there's no need for a separate triage step in normal demo
    # flows. Safety-critical (P0/P1) paths use escalate_to_human
    # for emergencies.
    tools = [
        make_service_network_tool(ctx),
        make_book_tool(ctx, config),
        make_escalate_tool(ctx, config),
        make_knowledge_tool(),
        make_parts_lookup_tool(),
        make_vehicle_identity_tool(),
        make_retrieve_tool(ctx, config),
        make_diy_repair_advisor_tool(ctx),
    ]

    agent = BidiAgent(
        model=sonic_model,
        tools=tools,
        system_prompt=system_prompt,
        name="vsa-canonical",
    )

    # Adapter callables.
    #
    # Strands expects events in its native bidi_* format. iOS speaks
    # our wire format (audio.chunk / text.input / etc.). The adapters
    # below translate. They are deliberately stateless beyond a
    # session.end sentinel.
    chunk_count = {"n": 0}
    event_count = {"n": 0}

    # last_audio_at tracks the monotonic timestamp of the most recent
    # bidi_audio_stream event Nova produced. The narration watchdog
    # uses this to decide whether Nova actually verbalized the
    # injection or went silent post-tool. See `_send_narration_with_retry`.
    last_audio_at = {"t": 0.0}
    NARRATION_WATCHDOG_S = 4.0
    NARRATION_MAX_RETRIES = 2

    # Pre-warm gate is OPEN by default after pre-warm was removed
    # (see below). Kept as a vestigial flag so the audio-stream and
    # transcript-stream forwarders don't need conditional removal —
    # they'll always see prewarm_open=True. Left in place in case we
    # want to bring back a non-greeting probe later.
    prewarm_open = {"v": True}
    prewarm_done = {"v": True}
    PREWARM_TIMEOUT_S = 4.0

    async def input_callable() -> dict:
        while True:
            msg = await websocket.receive_json()
            if not isinstance(msg, dict):
                continue
            t = msg.get("type")
            if t == "audio.chunk":
                audio_b64 = msg.get("data")
                if not audio_b64:
                    continue
                sr = int(msg.get("sampleRate") or INPUT_SAMPLE_RATE)
                chunk_count["n"] += 1
                if chunk_count["n"] <= 3 or chunk_count["n"] % 20 == 0:
                    log.info(
                        "canonical: audio.chunk #%d sr=%d (~%d bytes)",
                        chunk_count["n"], sr, (len(audio_b64) * 3) // 4,
                    )
                return {
                    "type": "bidi_audio_input",
                    "audio": audio_b64,
                    "format": "pcm",
                    "sample_rate": sr,
                    "channels": 1,
                }
            if t == "text.input":
                text = msg.get("text", "")
                if not text:
                    continue
                log.info("canonical: text %r", text[:120])
                return {
                    "type": "bidi_text_input",
                    "text": text,
                    "role": "user",
                }
            if t == "session.end":
                log.info(
                    "canonical: session.end (chunks=%d events=%d)",
                    chunk_count["n"], event_count["n"],
                )
                raise _SessionEnd()
            # Silently ignore any other message types so the client can
            # send session.start / heartbeat / etc. without us caring.

    # Track which tool a given toolUseId is for so we can label
    # tool.result events on the wire (Strands gives us toolUseId, not
    # name, in tool_result).
    tool_use_names: dict[str, str] = {}

    async def output_callable(event: Any) -> None:
        event_count["n"] += 1
        if not isinstance(event, dict):
            return
        et = event.get("type")

        if et == "bidi_audio_stream":
            # Always record the timestamp — the pre-warm probe and
            # narration watchdog use this to detect Nova
            # responsiveness even when we're swallowing the audio.
            last_audio_at["t"] = asyncio.get_event_loop().time()
            # Pre-warm gate: swallow audio chunks until the probe
            # has verified Nova's TTS is alive, otherwise iOS hears
            # Nova's "Hi" greeting from the probe at session start.
            if not prewarm_open["v"]:
                return
            await _safe_send_json(websocket, {
                "type": "audio.chunk",
                "data": event.get("audio"),
                "sampleRate": event.get("sample_rate"),
                "format": event.get("format", "pcm"),
            })
        elif et == "bidi_transcript_stream":
            # Same pre-warm gate as audio: swallow Nova's transcript
            # of her probe reply so iOS doesn't display "Hi" in the
            # transcript pane at session start.
            if not prewarm_open["v"]:
                return
            await _safe_send_json(websocket, {
                "type": "transcript",
                "role": event.get("role"),
                "text": event.get("text") or event.get("current_transcript", ""),
                "isFinal": bool(event.get("is_final")),
            })
        elif et == "bidi_interruption":
            await _safe_send_json(websocket, {"type": "interruption"})
        elif et == "tool_use_stream":
            current = event.get("current_tool_use") or {}
            name = current.get("name")
            tool_use_id = current.get("toolUseId")
            if tool_use_id and name:
                tool_use_names[tool_use_id] = name
            log.info(
                "canonical: tool_use_stream name=%s input=%s",
                name, current.get("input"),
            )
            await _safe_send_json(websocket, {
                "type": "tool.call",
                "name": name,
                "input": current.get("input"),
            })
        elif et == "tool_result":
            tr = event.get("tool_result") or {}
            tool_use_id = tr.get("toolUseId", "")
            name = tool_use_names.get(tool_use_id, "unknown")
            # Strands wraps the tool's return in content[].json or .text.
            result_data: Any = {}
            for block in tr.get("content", []) or []:
                if "json" in block:
                    result_data = block["json"]
                    break
                if "text" in block:
                    raw = block["text"]
                    try:
                        result_data = json.loads(raw)
                    except (json.JSONDecodeError, TypeError):
                        try:
                            import ast, re
                            cleaned = re.sub(r"Decimal\('([^']+)'\)", r"\1", raw)
                            result_data = ast.literal_eval(cleaned)
                        except Exception:
                            result_data = {"result": raw}
                    break
            log.info(
                "canonical: tool_result name=%s status=%s",
                name, tr.get("status"),
            )
            await _safe_send_json(websocket, {
                "type": "tool.result",
                "name": name,
                "output": result_data,
            })
            # If a sub-agent attached a `chat_message` to its result,
            # forward it to iOS as a screen-side info bubble. Lets Nova
            # stay brief in voice while the driver still gets the
            # detailed reference data (e.g. tire pressure table) on
            # screen. See sub_agent.py SubAgentConfig.chat_message_fn.
            if isinstance(result_data, dict):
                chat_message = result_data.get("chat_message")
                if chat_message:
                    log.info(
                        "canonical: forwarding tool chat_message "
                        "name=%s chars=%d",
                        name, len(chat_message),
                    )
                    await _safe_send_json(websocket, {
                        "type": "info.message",
                        "source": name,
                        "markdown": chat_message,
                    })
            # Track the offered service center on ctx so the book
            # tool can include the center name + slot in its
            # `narration` field. find_service_center returns
            # {"found": ..., "centers": [...], "narration": "..."}
            # and we stash the FIRST center (the one the narration
            # references) for downstream use.
            if (name == "find_service_center"
                    and isinstance(result_data, dict)
                    and result_data.get("found")):
                centers = result_data.get("centers") or []
                if centers:
                    ctx.last_offered_service_center = centers[0]
            # On book success, clear the offered-center sentinel so
            # a stray later affirmation doesn't carry the previous
            # center into a duplicate booking.
            if (name == "book"
                    and isinstance(result_data, dict)
                    and result_data.get("requestNumber")):
                ctx.last_offered_service_center = None

            # POST-TOOL NARRATION INJECTION — kept after canonical
            # simplification because we proved it's actually doing
            # essential work, not just papering over our complexity.
            #
            # Confirmed by AWS Nova Sonic team (Slack thread shared
            # 2026-05-22): "Yes, this is a case of TTS hallucination
            # - the content is correct, but the generated speech is
            # not. We're working on it" (June/August release fix).
            # Their recommended workaround matches what we landed on:
            # cross-modal text input — send a text message
            # alongside the tool result to explicitly tell Nova
            # what to say.
            #
            # `narration` is the field find_service_center / book
            # already populate. We use `asyncio.create_task` so the
            # send runs OUTSIDE the output_callable's call stack —
            # calling `await agent.send()` inline from the
            # output_callable was observed to produce no visible
            # send (no Strands "sending text content" debug log,
            # Nova never got the injection, session timed out at 55s
            # with InternalErrorCode=532). Decoupling via task
            # mirrors the original bidi_app.py's InjectionQueue
            # dispatch_loop, which is what worked end-to-end at
            # 16:32 / 16:47 EDT yesterday.
            if isinstance(result_data, dict):
                narration = result_data.get("narration")
                if narration and name in (
                    "find_service_center", "book",
                ):
                    log.info(
                        "canonical: narration injection name=%s chars=%d preview=%r",
                        name, len(narration), narration[:80],
                    )
                    payload = f"Tell the driver: {narration}"

                    async def _send_narration_with_retry(
                        p: str = payload, tool_name: str = name,
                    ) -> None:
                        # Workaround for AWS-confirmed Nova Sonic v2 TTS
                        # hallucination: Nova receives the cross-modal
                        # text input but sometimes generates no audio
                        # output (we've observed it stay silent until
                        # Bedrock's 55s session-level timeout fires
                        # with InternalErrorCode=532). Retry the
                        # injection if no audio arrives within the
                        # watchdog window. Capped at NARRATION_MAX_RETRIES
                        # so a genuinely-broken session doesn't loop
                        # forever; total worst-case latency before iOS's
                        # own 12s silence watchdog kicks in is
                        # (1 + NARRATION_MAX_RETRIES) * NARRATION_WATCHDOG_S
                        # = 12s, which lines up.
                        for attempt in range(1 + NARRATION_MAX_RETRIES):
                            sent_at = asyncio.get_event_loop().time()
                            try:
                                await agent.send(p)
                                log.info(
                                    "canonical: narration injection sent "
                                    "name=%s attempt=%d/%d",
                                    tool_name, attempt + 1,
                                    1 + NARRATION_MAX_RETRIES,
                                )
                            except Exception as e:  # noqa: BLE001
                                log.exception(
                                    "canonical: narration injection failed "
                                    "name=%s attempt=%d: %s",
                                    tool_name, attempt + 1, e,
                                )
                                return
                            await asyncio.sleep(NARRATION_WATCHDOG_S)
                            # Did Nova produce audio AFTER we sent this
                            # injection? If yes, the injection landed and
                            # we're done. If no, retry (or give up).
                            if last_audio_at["t"] > sent_at:
                                if attempt > 0:
                                    log.info(
                                        "canonical: narration recovered "
                                        "name=%s after %d retries",
                                        tool_name, attempt,
                                    )
                                return
                            if attempt >= NARRATION_MAX_RETRIES:
                                log.warning(
                                    "canonical: narration injection "
                                    "EXHAUSTED name=%s — Nova silent "
                                    "for %.1fs across %d attempts",
                                    tool_name,
                                    (1 + NARRATION_MAX_RETRIES)
                                    * NARRATION_WATCHDOG_S,
                                    1 + NARRATION_MAX_RETRIES,
                                )
                                return
                            log.warning(
                                "canonical: narration injection SILENT "
                                "name=%s attempt=%d, retrying",
                                tool_name, attempt + 1,
                            )

                    asyncio.create_task(
                        _send_narration_with_retry(),
                        name=f"narration-injection-{name}",
                    )
            # If the book tool returned an escalation contact, mirror it
            # to iOS as an `escalation` event so the Connect chat client
            # picks it up. Same wire format as the full bidi_app.py.
            if name == "escalate_to_human" and isinstance(result_data, dict) and result_data.get("contactId"):
                await _safe_send_json(websocket, {
                    "type": "escalation",
                    "status": "initiated",
                    "severity": result_data.get("severity", "P3"),
                    "contactId": result_data.get("contactId"),
                    "participantId": result_data.get("participantId"),
                    "participantToken": result_data.get("participantToken"),
                    "connectionExpiry": result_data.get("connectionExpiry"),
                    "rsaDispatched": result_data.get("rsaDispatched", False),
                    "rsaActionId": result_data.get("rsaActionId"),
                })
        # Other event types (bidi_response_start, bidi_response_end,
        # bidi_usage, etc.) we don't forward — iOS doesn't render them.

    # NOTE: Pre-warm probe was removed 2026-05-22 after testing showed it
    # MADE THE BUG WORSE. The probe sent "Hi." which triggered Nova to
    # generate ~13s of "Hello Samantha..." TTS. Audio streamed but iOS
    # suppressed it (hasSeenFirstUserTurn=false). After 60-90s of user
    # idle, when the user finally spoke, Nova emitted a stale
    # `{"interrupted": true}` event for the long-completed greeting
    # turn — and the FIRST real tool call's narration died.
    #
    # Without pre-warm, Nova is silent at session start (no greeting,
    # no stale turn to interrupt later) and the first tool's narration
    # works reliably. Bug pattern reverts to "occasional book-narration
    # silence" which the narration retry catches.

    try:
        await agent.run(inputs=[input_callable], outputs=[output_callable])
    except _SessionEnd:
        log.info("canonical: session ended cleanly session=%s", session_id)
    except Exception as e:  # noqa: BLE001
        log.exception("canonical: session error session=%s: %s", session_id, e)
        await _safe_send_json(websocket, {"type": "error", "message": f"server: {e}"})

    await _safe_send_json(websocket, {"type": "session.ended", "reason": "closed"})
    await _safe_close(websocket)


async def _safe_send_json(websocket, payload: dict[str, Any]) -> None:
    try:
        await websocket.send_json(payload)
    except Exception as e:  # noqa: BLE001
        log.debug("canonical: send failed: %s", e)


async def _safe_close(websocket, code: int = 1000, reason: str = "") -> None:
    try:
        await websocket.close(code=code, reason=reason)
    except Exception as e:  # noqa: BLE001
        log.debug("canonical: close failed: %s", e)


if __name__ == "__main__":
    app.run(log_level="info")
