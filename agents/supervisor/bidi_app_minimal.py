"""MINIMAL bidi entrypoint for Nova Sonic v2 isolation test.

Phase 3 step 3 B-path plan, step 3: strip the full entrypoint down to
absolute minimum to determine whether Nova Sonic v2 produces any output
at all when we remove our custom tools, system prompt, and session
plumbing. This is the bisection test: if Nova Sonic emits output here,
the blocker is in our tools or system prompt (and we can bisect
further); if it doesn't, the problem is upstream of our payload
entirely.

NOT intended for production or demo use. This entrypoint accepts audio
from any authenticated client and returns Nova Sonic output raw, with
no tenant isolation, no guardrails, no auth tokens checked. After the
diagnostic is complete, the runtime MUST be switched back to bidi_app.py.

Kept deliberately small and comment-heavy so the diff against bidi_app.py
is obvious:
  - No SessionContext.
  - No tools list.
  - No system_prompt.
  - No monkey-patch (we want to see if Nova Sonic v2 works with default
    Strands behavior on the buggy 1.37.0 — which includes the known
    `interactive: True` issue but also every other config Strands emits).
  - No custom handshake (headers/query params/session.start). Accepts
    audio immediately after the WS opens.
  - Output events forwarded to client as {"type":"debug","event_type":...}
    with token counts passed through for bidi_usage.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import Any

from bedrock_agentcore.runtime import BedrockAgentCoreApp
from bedrock_agentcore.runtime.context import RequestContext
from strands.experimental.bidi.agent import BidiAgent
from strands.experimental.bidi.models.nova_sonic import (
    BidiNovaSonicModel,
    NOVA_SONIC_V2_MODEL_ID,
)

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logging.getLogger("strands.experimental.bidi").setLevel(logging.DEBUG)
logging.getLogger("strands.experimental.bidi.models.nova_sonic").setLevel(logging.DEBUG)

app = BedrockAgentCoreApp()

INPUT_SAMPLE_RATE = 16000
OUTPUT_SAMPLE_RATE = 24000


class _SessionEnd(Exception):
    pass


@app.websocket
async def websocket_handler(websocket, context: RequestContext) -> None:
    await websocket.accept()
    session_id = context.session_id or f"vsa-minimal-{uuid.uuid4().hex[:12]}"
    log.info("minimal: accept session=%s", session_id)

    # Bare-minimum Nova Sonic v2. No provider_config overrides except the
    # sample rates (in case default differs). Default turn detection, default
    # inference. If this produces output, we know something we customized in
    # bidi_app.py is the blocker.
    try:
        model = BidiNovaSonicModel(
            model_id=NOVA_SONIC_V2_MODEL_ID,
            provider_config={
                "audio": {
                    "input_rate": INPUT_SAMPLE_RATE,
                    "output_rate": OUTPUT_SAMPLE_RATE,
                },
            },
            client_config={"region": "us-east-1"},
        )
    except Exception as e:  # noqa: BLE001
        log.exception("minimal: model init failed")
        await _safe_send_json(websocket, {"type": "error", "message": f"init: {e}"})
        await _safe_close(websocket, code=1011, reason="model-init-failed")
        return

    # BidiAgent with nothing attached: no tools, no system_prompt, no messages.
    agent = BidiAgent(model=model, name="minimal")

    state = {"chunks": 0, "bytes": 0, "events": 0, "types": {}}

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
                state["chunks"] += 1
                state["bytes"] += (len(audio_b64) * 3) // 4
                if state["chunks"] <= 3 or state["chunks"] % 10 == 0:
                    log.info("minimal: audio.chunk #%d sr=%d (bytes ~%d)",
                             state["chunks"], sr, state["bytes"])
                return {
                    "type": "bidi_audio_input",
                    "audio": audio_b64,
                    "format": "pcm",
                    "sample_rate": sr,
                    "channels": 1,
                }
            if t == "text.input":
                text = msg.get("text", "")
                if text:
                    log.info("minimal: text %r", text[:100])
                    return {"type": "bidi_text_input", "text": text, "role": "user"}
            if t == "session.end":
                log.info("minimal: session.end (chunks=%d bytes=%d)",
                         state["chunks"], state["bytes"])
                raise _SessionEnd()
            # Silently accept session.start (we don't need it but don't want
            # to reject clients that send it).

    async def output_callable(event: Any) -> None:
        state["events"] += 1
        et = event.get("type") if isinstance(event, dict) else None
        state["types"][str(et)] = state["types"].get(str(et), 0) + 1

        if state["types"][str(et)] == 1 or state["events"] % 20 == 0:
            preview = ""
            if isinstance(event, dict):
                non_audio = {k: (v if k not in ("audio", "data") else f"<{len(v)} chars>")
                             for k, v in event.items() if k != "delta"}
                preview = json.dumps(non_audio, default=str)[:200]
            log.info("minimal: event #%d type=%r count=%d preview=%s",
                     state["events"], et, state["types"][str(et)], preview)

        # Forward absolutely everything to the client so the smoke test
        # can see exactly what Nova Sonic produced. Strip audio data
        # (too big for logs) but keep the event_type shape.
        if isinstance(event, dict) and et:
            payload = {"type": "debug", "event_type": et}
            if et == "bidi_usage":
                payload.update({
                    "inputTokens": event.get("inputTokens"),
                    "outputTokens": event.get("outputTokens"),
                    "totalTokens": event.get("totalTokens"),
                })
            elif et == "bidi_audio_stream":
                # Emit as-is in the audio.chunk wire format so we can tell
                # if Nova Sonic actually generated audio.
                payload = {
                    "type": "audio.chunk",
                    "data": event.get("audio"),
                    "sampleRate": event.get("sample_rate"),
                }
            elif et == "bidi_transcript_stream":
                payload = {
                    "type": "transcript",
                    "role": event.get("role"),
                    "text": event.get("text") or event.get("current_transcript", ""),
                    "isFinal": bool(event.get("is_final")),
                }
            await _safe_send_json(websocket, payload)

    try:
        await agent.run(inputs=[input_callable], outputs=[output_callable])
    except _SessionEnd:
        log.info("minimal: session ended (events=%d types=%s)",
                 state["events"], state["types"])
    except Exception as e:  # noqa: BLE001
        log.exception("minimal: session error")
        await _safe_send_json(websocket, {"type": "error", "message": f"server: {e}"})
        await _safe_close(websocket, code=1011, reason="server-error")
        return

    await _safe_send_json(websocket, {"type": "session.ended", "reason": "closed"})
    await _safe_close(websocket, code=1000, reason="done")


async def _safe_send_json(websocket, payload: dict[str, Any]) -> None:
    try:
        await websocket.send_json(payload)
    except Exception as e:  # noqa: BLE001
        log.debug("minimal: send failed: %s", e)


async def _safe_close(websocket, code: int = 1000, reason: str = "") -> None:
    try:
        await websocket.close(code=code, reason=reason)
    except Exception as e:  # noqa: BLE001
        log.debug("minimal: close failed: %s", e)


if __name__ == "__main__":
    app.run(log_level="info")
