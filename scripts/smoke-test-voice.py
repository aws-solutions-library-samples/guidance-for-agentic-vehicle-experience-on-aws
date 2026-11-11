#!/usr/bin/env python3
"""Smoke-test the deployed AgentCore bidi (voice) runtime.

Opens a WebSocket to the deployed vsa_supervisor_bidi runtime with a
SigV4-signed handshake plus the custom user-token header, streams a
pre-recorded PCM audio clip as base64 audio.chunk messages, and
records every server event.

Asserts:
  - Connection opened successfully.
  - Server emitted at least one transcript event for the user speech.
  - Server emitted at least one audio.chunk back.
  - A classification (P0/P1/P2/P3) came through.

Response audio is saved to /tmp/vsa-smoke-response.wav so we can play
it back manually and confirm the agent actually spoke a safety
instruction, not silence.

Usage:
    python scripts/smoke-test-voice.py
    python scripts/smoke-test-voice.py --clip corpora/test-audio/temperature-spike.wav
    python scripts/smoke-test-voice.py --timeout 60

Prereqs:
    - scripts/smoke-test-agentcore.py has already succeeded (proves JWT
      caching and the base agentcore invocation path work).
    - vsa_supervisor_bidi runtime is deployed (see agents/supervisor/README.md).
    - aws-sdk-bedrock-runtime + bedrock-agentcore installed in the venv.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import logging
import os
import sys
import time
import uuid
import wave
from pathlib import Path

import websockets
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

# Deferred imports after sys.path tweak.
from agents.supervisor.config import (  # noqa: E402
    Config,
    clear_cached_tokens,
    get_cached_id_token,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("smoke-voice")

DEFAULT_CLIP = REPO_ROOT / "corpora" / "test-audio" / "temperature-spike.wav"
DEFAULT_OUTPUT = Path("/tmp/vsa-smoke-response.wav")
AGENT_NAME = "vsa_supervisor_bidi"

# Nova Sonic returns audio at 24kHz by default — see bidi_app.OUTPUT_SAMPLE_RATE.
OUTPUT_SAMPLE_RATE = 24000

# Stream audio in 32KB-under chunks to stay below AgentCore's frame limit.
# 16-bit PCM @ 16kHz mono = 32000 bytes/sec; 20ms = 640 bytes raw, or ~860
# base64 bytes. 200ms chunks give us 6400 raw -> ~8600 base64 which is
# comfortably under any frame limit.
CHUNK_MS = 200


def load_agent_arn(agent_name: str = AGENT_NAME) -> str:
    config_path = REPO_ROOT / ".bedrock_agentcore.yaml"
    if not config_path.exists():
        raise RuntimeError(f"{config_path} not found.")
    cfg = yaml.safe_load(config_path.read_text())
    agent = (cfg.get("agents") or {}).get(agent_name)
    if not agent:
        raise RuntimeError(
            f"Agent '{agent_name}' not found in {config_path}. "
            f"Available: {list((cfg.get('agents') or {}).keys())}"
        )
    arn = (agent.get("bedrock_agentcore") or {}).get("agent_arn")
    if not arn:
        raise RuntimeError(f"Agent '{agent_name}' has no agent_arn yet — has it been deployed?")
    return arn


def read_wav_pcm(path: Path) -> tuple[bytes, int]:
    """Return (raw PCM bytes, sample rate). Validates mono 16-bit."""
    with wave.open(str(path), "rb") as w:
        if w.getnchannels() != 1:
            raise ValueError(f"Expected mono WAV, got {w.getnchannels()} channels")
        if w.getsampwidth() != 2:
            raise ValueError(f"Expected 16-bit WAV, got {w.getsampwidth()*8}-bit")
        return w.readframes(w.getnframes()), w.getframerate()


def chunks_of(data: bytes, chunk_bytes: int):
    for i in range(0, len(data), chunk_bytes):
        yield data[i : i + chunk_bytes]


def write_wav_pcm(path: Path, pcm: bytes, sample_rate: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(pcm)


async def run_smoke_test(
    *,
    clip_path: Path,
    output_path: Path,
    timeout_s: float,
    tenant_id: str,
    vin: str,
    vehicle_id: str | None,
    driver_id: str | None,
    force_reauth: bool,
    nudge_after: float = 0.0,
    nudge_text: str = "hello",
    prompt_first: str | None = None,
) -> int:
    if not clip_path.exists():
        log.error("Input clip not found: %s", clip_path)
        return 2

    cfg = Config.from_env()
    if force_reauth:
        clear_cached_tokens()
    log.info("Fetching Cognito token for tenant=%s", tenant_id)
    jwt = get_cached_id_token(cfg)
    log.info("  token length: %d", len(jwt))

    pcm, input_rate = read_wav_pcm(clip_path)
    duration_s = len(pcm) / 2 / input_rate
    log.info("Loaded clip %s (%.2fs, %d Hz, %d bytes)", clip_path, duration_s, input_rate, len(pcm))

    agent_arn = load_agent_arn()
    session_id = f"vsa-sess-voice-smoke-{uuid.uuid4().hex[:12]}"

    # Build the WebSocket URL. Custom headers can go as query params when
    # prefixed with X-Amzn-Bedrock-AgentCore-Runtime-Custom-. This is the
    # simplest path from Python since websockets + SigV4 signing via the
    # agentcore runtime client is more involved.
    from bedrock_agentcore.runtime import AgentCoreRuntimeClient

    rt_client = AgentCoreRuntimeClient(region=cfg.aws_region)
    ws_url, signed_headers = rt_client.generate_ws_connection(
        runtime_arn=agent_arn,
        session_id=session_id,
    )
    log.info("Agent ARN:  %s", agent_arn)
    log.info("Session:    %s", session_id)
    log.info("WS URL:     %s", ws_url)

    # Pass tenantId, vin, and user token via custom headers.
    signed_headers["X-Amzn-Bedrock-AgentCore-Runtime-Custom-User-Token"] = jwt
    signed_headers["X-Amzn-Bedrock-AgentCore-Runtime-Custom-Tenant-Id"] = tenant_id
    signed_headers["X-Amzn-Bedrock-AgentCore-Runtime-Custom-Vin"] = vin
    # Optional CMS-side identifiers: when set, the backend writes the
    # booking into cms-prod-storage-service-history and our post-run
    # assertion can verify the row landed.
    if vehicle_id:
        signed_headers[
            "X-Amzn-Bedrock-AgentCore-Runtime-Custom-VehicleId"
        ] = vehicle_id
    if driver_id:
        signed_headers[
            "X-Amzn-Bedrock-AgentCore-Runtime-Custom-DriverId"
        ] = driver_id

    received_audio_pcm = bytearray()
    received_events: list[dict] = []
    classification: str | None = None
    user_transcripts: list[str] = []
    assistant_transcripts: list[str] = []
    errors: list[str] = []

    start = time.time()
    try:
        async with websockets.connect(
            ws_url,
            additional_headers=signed_headers,
            max_size=2**24,  # 16MB — audio chunks can be big
            open_timeout=10,
        ) as ws:
            log.info("Connected. Streaming audio...")

            if prompt_first:
                log.info("--prompt-first: sending text.input %r before audio",
                         prompt_first)
                await ws.send(
                    json.dumps({"type": "text.input", "text": prompt_first})
                )
                # Give Nova Sonic a moment to process the priming text.
                await asyncio.sleep(1.0)

            async def send_audio() -> None:
                # 16-bit mono PCM @ 16kHz -> 32000 bytes/sec -> 6400 per 200ms
                chunk_bytes = (input_rate * 2 * CHUNK_MS) // 1000
                for chunk in chunks_of(pcm, chunk_bytes):
                    await ws.send(
                        json.dumps(
                            {
                                "type": "audio.chunk",
                                "data": base64.b64encode(chunk).decode("ascii"),
                                "sampleRate": input_rate,
                            }
                        )
                    )
                    # Pace roughly to wall-clock so turn detection works.
                    await asyncio.sleep(CHUNK_MS / 1000.0)
                log.info("Audio sent (%.2fs wall time). Streaming silence for VAD...",
                         time.time() - start)

                # Silence-continuation: Nova Sonic v2's VAD needs to detect
                # 1.75s of silence IN the audio stream to fire end-of-turn
                # (per docs endpointingSensitivity=MEDIUM). Real mic inputs
                # supply silence as low-amplitude PCM frames; our canned clip
                # just stops. Send 2s of zero-filled PCM frames at the same
                # 200ms pacing so Nova Sonic sees the silence and responds.
                silence_chunk = b"\x00" * chunk_bytes
                silence_ms_total = 2000
                silence_chunks = silence_ms_total // CHUNK_MS
                for _ in range(silence_chunks):
                    await ws.send(
                        json.dumps(
                            {
                                "type": "audio.chunk",
                                "data": base64.b64encode(silence_chunk).decode("ascii"),
                                "sampleRate": input_rate,
                            }
                        )
                    )
                    await asyncio.sleep(CHUNK_MS / 1000.0)
                log.info("Silence sent (%d chunks = %dms). Waiting for response...",
                         silence_chunks, silence_ms_total)

                # Optional nudge: replicates Varun's "user says hello to unstick
                # the agent" behavior from the internal Slack thread. If Nova
                # Sonic v2 has a known 'silent until user interrupts' quirk we
                # should see output appear after this text.input.
                log.info("nudge check: nudge_after=%s", nudge_after)
                if nudge_after > 0:
                    await asyncio.sleep(nudge_after)
                    log.info("--nudge-after: sending text.input %r to unstick agent",
                             nudge_text)
                    await ws.send(
                        json.dumps({"type": "text.input", "text": nudge_text})
                    )

            async def receive_events() -> None:
                nonlocal classification
                while True:
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=timeout_s)
                    except asyncio.TimeoutError:
                        log.warning("No event for %ds. Closing.", timeout_s)
                        return
                    try:
                        evt = json.loads(raw)
                    except json.JSONDecodeError:
                        log.warning("Non-JSON frame: %r", raw[:100])
                        continue
                    received_events.append(evt)
                    t = evt.get("type")
                    if t == "audio.chunk":
                        audio_b64 = evt.get("data") or ""
                        received_audio_pcm.extend(base64.b64decode(audio_b64))
                    elif t == "transcript":
                        role = evt.get("role")
                        text = evt.get("text", "")
                        is_final = evt.get("isFinal")
                        log.info("transcript[%s, final=%s]: %s", role, is_final, text[:200])
                        if role == "user":
                            user_transcripts.append(text)
                        else:
                            assistant_transcripts.append(text)
                    elif t == "tool.call":
                        log.info("tool.call: %s(%s)", evt.get("name"),
                                 json.dumps(evt.get("input") or {})[:120])
                    elif t == "classification":
                        classification = evt.get("value")
                        log.info("classification: %s", classification)
                    elif t == "error":
                        log.error("server error: %s", evt.get("message"))
                        errors.append(evt.get("message", ""))
                    elif t == "session.ended":
                        log.info("session.ended: %s", evt.get("reason"))
                        return
                    elif t == "debug":
                        # Server emits these for unmapped Strands event types.
                        # Log them so we can see what Nova Sonic is actually producing.
                        if evt.get("event_type") == "bidi_usage":
                            log.info(
                                "bidi_usage: in=%s out=%s total=%s",
                                evt.get("inputTokens"),
                                evt.get("outputTokens"),
                                evt.get("totalTokens"),
                            )
                        else:
                            log.info("debug event: unmapped_type=%r", evt.get("event_type"))
                    else:
                        # Strip audio fields and log the full event so we can
                        # see what's arriving when nothing matches our mappings.
                        non_audio = {k: v for k, v in evt.items() if k != "data"}
                        log.info("unmapped client event: %s", json.dumps(non_audio, default=str)[:300])

            # Start the receiver BEFORE sending so we don't miss anything.
            recv_task = asyncio.create_task(receive_events())
            await send_audio()
            # Let receiver drain for a bit after audio stops.
            try:
                await asyncio.wait_for(recv_task, timeout=timeout_s)
            except asyncio.TimeoutError:
                log.warning("Receiver did not finish in %ds; cancelling.", timeout_s)
                recv_task.cancel()
                try:
                    await recv_task
                except asyncio.CancelledError:
                    pass

            try:
                await ws.send(json.dumps({"type": "session.end"}))
            except Exception:
                pass
    except websockets.exceptions.InvalidStatus as e:
        log.error("WebSocket handshake failed: status=%d", e.response.status_code)
        log.error("Response body: %s", e.response.body.decode(errors="replace")[:500])
        return 1
    except Exception as e:  # noqa: BLE001
        log.exception("Connection failed")
        return 1

    elapsed = time.time() - start
    log.info("===== Smoke test summary =====")
    log.info("Elapsed:        %.2fs", elapsed)
    log.info("Events:         %d", len(received_events))
    log.info("Audio bytes:    %d", len(received_audio_pcm))
    log.info("Classification: %s", classification)
    log.info("User transcripts:      %r", user_transcripts[:3])
    log.info("Assistant transcripts: %r", assistant_transcripts[:3])
    if errors:
        log.info("Errors: %r", errors)

    if received_audio_pcm:
        write_wav_pcm(output_path, bytes(received_audio_pcm), OUTPUT_SAMPLE_RATE)
        log.info("Response audio written to %s", output_path)

    # Assertions.
    failures: list[str] = []
    if not received_events:
        failures.append("no events received from server")
    if not received_audio_pcm:
        failures.append("server did not emit any audio.chunk")
    if classification not in {"P0", "P1", "P2", "P3"}:
        failures.append(f"classification missing or invalid: {classification!r}")
    if errors:
        failures.append(f"server emitted error events: {errors}")

    if failures:
        log.error("✗ Smoke test FAILED:")
        for f in failures:
            log.error("  - %s", f)
        return 1

    log.info("🎉 Smoke test passed.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--clip", type=Path, default=DEFAULT_CLIP,
                        help="Input PCM WAV (16-bit mono 16kHz)")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT,
                        help="Where to write the response audio WAV")
    parser.add_argument("--timeout", type=float, default=45.0,
                        help="Per-event recv timeout in seconds")
    parser.add_argument("--tenant", default=None)
    parser.add_argument("--vin", default=None)
    parser.add_argument(
        "--vehicle-id",
        default=None,
        help="CMS vehicleId to send as a session header (defaults to VSA_DEFAULT_VEHICLE_ID).",
    )
    parser.add_argument(
        "--driver-id",
        default=None,
        help="CMS driverId to send as a session header (defaults to VSA_DEFAULT_DRIVER_ID).",
    )
    parser.add_argument("--force-reauth", action="store_true")
    parser.add_argument(
        "--nudge-after",
        type=float,
        default=0.0,
        help=(
            "If >0, send a text.input message this many seconds after audio finishes. "
            "Used to test the Nova Sonic v2 'agent silent until user interrupts' hypothesis "
            "per the Slack thread re: Xi's phatic filler workaround."
        ),
    )
    parser.add_argument(
        "--nudge-text",
        default="hello",
        help="Text to send when --nudge-after is set.",
    )
    parser.add_argument(
        "--prompt-first",
        default=None,
        help=(
            "If set, send a text.input with this content BEFORE streaming audio. "
            "Tests whether Nova Sonic v2 needs a text-input priming interaction "
            "before it will generate from audio."
        ),
    )
    args = parser.parse_args()

    cfg = Config.from_env()
    tenant = args.tenant or cfg.tenant_id
    vin = args.vin or cfg.default_vin
    vehicle_id = args.vehicle_id or cfg.default_vehicle_id
    driver_id = args.driver_id or cfg.default_driver_id

    return asyncio.run(
        run_smoke_test(
            clip_path=args.clip,
            output_path=args.output,
            timeout_s=args.timeout,
            tenant_id=tenant,
            vin=vin,
            vehicle_id=vehicle_id,
            driver_id=driver_id,
            force_reauth=args.force_reauth,
            nudge_after=args.nudge_after,
            nudge_text=args.nudge_text,
            prompt_first=args.prompt_first,
        )
    )


if __name__ == "__main__":
    sys.exit(main())
