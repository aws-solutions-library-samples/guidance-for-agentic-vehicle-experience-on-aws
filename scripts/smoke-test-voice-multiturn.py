#!/usr/bin/env python3
"""Multi-turn smoke test for the deployed Nova Sonic v2 + Strands runtime.

Reproduces the iOS demo flow without needing the simulator:

  1. Connect to vsa_supervisor_bidi via SigV4 + JWT WebSocket.
  2. Send session.start JSON as the iOS fallback path does.
  3. Stream tire-pressure-low-fleet.wav (~2.8s) + 2s silence.
  4. Wait for:
       - tool.call find_service_center
       - tool.result with narration
       - assistant audio chunks ("I found ... Want me to book it?")
  5. Stream yes-fleet.wav (~1s) + 2s silence.
  6. Wait for:
       - tool.call book
       - tool.result with narration ("Done — you're booked at ...")
       - assistant audio chunks  <-- THIS is where Nova goes silent
  7. Print a verdict line and exit:
       - find_service_center: PASS|SILENT
       - book:                PASS|SILENT
  8. Save full response audio to /tmp/vsa-multiturn-response-<seq>.wav.

Run multiple times to estimate Nova post-tool TTS reliability.

Usage:
    python scripts/smoke-test-voice-multiturn.py
    python scripts/smoke-test-voice-multiturn.py --runs 10
    python scripts/smoke-test-voice-multiturn.py --persona oem
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

from agents.supervisor.config import (  # noqa: E402
    Config,
    clear_cached_tokens,
    get_cached_id_token,
)

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
)
log = logging.getLogger("multiturn")

# Sibling-repo path to the CMS iOS demo clips. Override via CMS_REPO_PATH env
# var when the CMS repo is checked out somewhere other than a sibling of this
# repo (default assumes `~/connected-mobility-guidance-on-aws` next to this
# one, mirroring the AWS Solutions Library house layout).
_CMS_REPO = Path(
    os.environ.get(
        "CMS_REPO_PATH",
        Path.home() / "connected-mobility-guidance-on-aws",
    )
)
WAV_DIR = _CMS_REPO / "clients/ios/VSACompanion/DemoClips"
AGENT_NAME = "vsa_supervisor_bidi"
INPUT_RATE = 16000
OUTPUT_RATE = 24000
CHUNK_MS = 200
TURN_SILENCE_MS = 2000  # silence to flush Nova VAD end-of-turn
POST_BOOK_TIMEOUT_S = 25.0  # how long to wait for book TTS before declaring SILENT
POST_FIND_TIMEOUT_S = 25.0


def load_agent_arn() -> str:
    cfg = yaml.safe_load((REPO_ROOT / ".bedrock_agentcore.yaml").read_text())
    return cfg["agents"][AGENT_NAME]["bedrock_agentcore"]["agent_arn"]


def read_wav(path: Path) -> bytes:
    with wave.open(str(path), "rb") as w:
        assert w.getnchannels() == 1
        assert w.getsampwidth() == 2
        assert w.getframerate() == INPUT_RATE
        return w.readframes(w.getnframes())


def chunks_of(data: bytes, n: int):
    for i in range(0, len(data), n):
        yield data[i : i + n]


def write_wav(path: Path, pcm: bytes, rate: int = OUTPUT_RATE) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)


class TurnRecorder:
    """Stateful event tracker for a single multi-turn session."""

    def __init__(self) -> None:
        self.events: list[dict] = []
        self.find_service_center_called = False
        self.find_service_center_result: dict | None = None
        self.book_called = False
        self.book_result: dict | None = None
        self.assistant_audio_after_find = bytearray()
        self.assistant_audio_after_book = bytearray()
        self.assistant_transcripts_after_find: list[str] = []
        self.assistant_transcripts_after_book: list[str] = []
        self.errors: list[str] = []

        # phase: 'pre-find' | 'post-find' | 'post-book'
        self.phase = "pre-find"

        # full response audio
        self.all_audio = bytearray()

    def on_event(self, evt: dict) -> None:
        self.events.append(evt)
        t = evt.get("type")

        if t == "tool.call":
            name = evt.get("name")
            if name == "find_service_center":
                self.find_service_center_called = True
                log.info("  ▸ tool.call find_service_center")
            elif name == "book":
                self.book_called = True
                log.info("  ▸ tool.call book")
        elif t == "tool.result":
            name = evt.get("name")
            output = evt.get("output") or {}
            if name == "find_service_center":
                self.find_service_center_result = output
                self.phase = "post-find"
                log.info(
                    "  ▸ tool.result find_service_center "
                    "(narration: %r)",
                    (output.get("narration") or "")[:60],
                )
            elif name == "book":
                self.book_result = output
                self.phase = "post-book"
                log.info(
                    "  ▸ tool.result book (narration: %r)",
                    (output.get("narration") or "")[:60],
                )
        elif t == "audio.chunk":
            data = base64.b64decode(evt.get("data") or "")
            self.all_audio.extend(data)
            if self.phase == "post-find":
                self.assistant_audio_after_find.extend(data)
            elif self.phase == "post-book":
                self.assistant_audio_after_book.extend(data)
        elif t == "transcript":
            role = evt.get("role")
            text = (evt.get("text") or "").strip()
            is_final = evt.get("isFinal")
            if role == "assistant" and is_final and text:
                if self.phase == "post-find":
                    self.assistant_transcripts_after_find.append(text)
                elif self.phase == "post-book":
                    self.assistant_transcripts_after_book.append(text)
                log.info(
                    "  ▸ transcript[asst, final, phase=%s]: %r",
                    self.phase, text[:80],
                )
        elif t == "error":
            self.errors.append(evt.get("message", ""))
            log.error("  ▸ server error: %s", evt.get("message"))


async def stream_clip(
    ws, pcm: bytes,
    ios_exact: bool = False,
    short_trail: bool = False,
    no_sleep_after_last: bool = False,
    fast_pacing: bool = False,
) -> None:
    """Stream PCM data + trailing silence so Nova VAD detects end-of-turn.

    Variants for bisecting the iOS bug:
      ios_exact      = all three of the below combined (legacy)
      short_trail    = 4 trailing silence chunks (~800ms) vs 10 (~2000ms)
      no_sleep_after_last = 1ms gap from last audio to first silence (vs 200ms)
      fast_pacing    = 180ms inter-chunk pacing (vs 200ms)
    """
    if ios_exact:
        short_trail = True
        no_sleep_after_last = True
        fast_pacing = True

    chunk_bytes = (INPUT_RATE * 2 * CHUNK_MS) // 1000
    pacing_ms = (CHUNK_MS * 9) // 10 if fast_pacing else CHUNK_MS
    chunks_list = list(chunks_of(pcm, chunk_bytes))
    for i, chunk in enumerate(chunks_list):
        await ws.send(json.dumps({
            "type": "audio.chunk",
            "data": base64.b64encode(chunk).decode("ascii"),
            "sampleRate": INPUT_RATE,
        }))
        is_last = i == len(chunks_list) - 1
        if is_last and no_sleep_after_last:
            pass  # iOS quirk: no sleep after final audio chunk
        else:
            await asyncio.sleep(pacing_ms / 1000.0)
    silence_chunk = b"\x00" * chunk_bytes
    n_silence = int(os.environ.get("BISECT_SILENCE_N", "4")) if short_trail else (TURN_SILENCE_MS // CHUNK_MS)
    for _ in range(n_silence):
        await ws.send(json.dumps({
            "type": "audio.chunk",
            "data": base64.b64encode(silence_chunk).decode("ascii"),
            "sampleRate": INPUT_RATE,
        }))
        await asyncio.sleep(pacing_ms / 1000.0)


async def send_silence(ws, ms: int) -> None:
    """Send a single silent audio chunk of `ms` milliseconds. Mimics iOS
    wake-up chunk + keep-alive pings."""
    n_bytes = (INPUT_RATE * 2 * ms) // 1000
    silence = b"\x00" * n_bytes
    await ws.send(json.dumps({
        "type": "audio.chunk",
        "data": base64.b64encode(silence).decode("ascii"),
        "sampleRate": INPUT_RATE,
    }))


async def wait_for(
    rec: TurnRecorder,
    predicate,
    timeout_s: float,
    label: str,
) -> bool:
    """Poll until predicate(rec) is True or timeout. Receiver is in the background."""
    start = time.time()
    while time.time() - start < timeout_s:
        if predicate(rec):
            return True
        await asyncio.sleep(0.1)
    log.warning("  ▸ TIMEOUT waiting for %s (%.1fs)", label, timeout_s)
    return False


async def run_session(
    *,
    persona: str,
    seq: int,
    cfg: Config,
    jwt: str,
    agent_arn: str,
    output_dir: Path,
    ios_ramp: bool = False,
    ios_pacing: bool = False,
) -> dict:
    """Run one multi-turn session and return a verdict dict."""
    tire_wav = WAV_DIR / f"tire-pressure-low-{persona}.wav"
    yes_wav = WAV_DIR / f"yes-{persona}.wav"
    if not tire_wav.exists() or not yes_wav.exists():
        raise FileNotFoundError(f"Missing demo clips for persona={persona}")
    tire_pcm = read_wav(tire_wav)
    yes_pcm = read_wav(yes_wav)

    session_id = f"vsa-multi-{seq:02d}-{uuid.uuid4().hex[:8]}"
    log.info("=== Run #%d  session=%s persona=%s ===", seq, session_id, persona)

    from bedrock_agentcore.runtime import AgentCoreRuntimeClient
    rt = AgentCoreRuntimeClient(region=cfg.aws_region)
    ws_url, signed = rt.generate_ws_connection(
        runtime_arn=agent_arn, session_id=session_id,
    )
    signed["X-Amzn-Bedrock-AgentCore-Runtime-Custom-User-Token"] = jwt
    signed["X-Amzn-Bedrock-AgentCore-Runtime-Custom-Tenant-Id"] = cfg.tenant_id
    signed["X-Amzn-Bedrock-AgentCore-Runtime-Custom-Vin"] = cfg.default_vin
    signed["X-Amzn-Bedrock-AgentCore-Runtime-Custom-VehicleId"] = cfg.default_vehicle_id
    signed["X-Amzn-Bedrock-AgentCore-Runtime-Custom-DriverId"] = cfg.default_driver_id

    rec = TurnRecorder()
    verdict = {
        "seq": seq,
        "session_id": session_id,
        "persona": persona,
        "find_called": False,
        "find_audio_bytes": 0,
        "find_verdict": "ERROR",
        "book_called": False,
        "book_audio_bytes": 0,
        "book_verdict": "ERROR",
        "errors": [],
    }

    try:
        async with websockets.connect(
            ws_url, additional_headers=signed, max_size=2**24, open_timeout=10
        ) as ws:
            # 1) session.start (iOS-style identity fallback)
            await ws.send(json.dumps({
                "type": "session.start",
                "tenantId": cfg.tenant_id,
                "vin": cfg.default_vin,
                "jwt": jwt,
                "vehicleId": cfg.default_vehicle_id,
                "driverId": cfg.default_driver_id,
            }))

            # background receiver
            stop = asyncio.Event()

            async def receiver():
                while not stop.is_set():
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=1.0)
                    except asyncio.TimeoutError:
                        continue
                    except websockets.exceptions.ConnectionClosed:
                        return
                    try:
                        evt = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    rec.on_event(evt)

            recv_task = asyncio.create_task(receiver())

            # iOS-style session-start ramp (replicates VoiceSessionViewModel):
            #   1. 200ms wake-up silent chunk immediately after session.start
            #   2. Wait ~5s, with keep-alive 50ms silent chunks at 5s intervals
            if ios_ramp:
                log.info("iOS-ramp: 200ms wake-up + 3 keep-alives")
                await send_silence(ws, 200)
                await asyncio.sleep(5.0)
                await send_silence(ws, 50)
                await asyncio.sleep(5.0)
                await send_silence(ws, 50)
                await asyncio.sleep(5.0)
                await send_silence(ws, 50)

            # 2) Turn 1: tire-pressure clip
            log.info("Turn 1: streaming tire-pressure-low-%s.wav (ios_pacing=%s)",
                     persona, ios_pacing)
            # BISECT: test ONE iOS-exact behavior at a time via env vars
            short_trail = os.environ.get("BISECT_SHORT_TRAIL") == "1"
            no_sleep_after = os.environ.get("BISECT_NO_SLEEP_AFTER") == "1"
            fast_pacing = os.environ.get("BISECT_FAST_PACING") == "1"
            log.info("  bisect flags: short_trail=%s no_sleep_after=%s fast_pacing=%s",
                     short_trail, no_sleep_after, fast_pacing)
            await stream_clip(
                ws, tire_pcm,
                ios_exact=ios_pacing,
                short_trail=short_trail,
                no_sleep_after_last=no_sleep_after,
                fast_pacing=fast_pacing,
            )

            # Wait for find_service_center tool.result
            await wait_for(
                rec,
                lambda r: r.find_service_center_result is not None,
                POST_FIND_TIMEOUT_S,
                "find_service_center tool.result",
            )
            verdict["find_called"] = rec.find_service_center_called

            # Wait extra 8s after tool.result for assistant audio to finish.
            await asyncio.sleep(8.0)
            verdict["find_audio_bytes"] = len(rec.assistant_audio_after_find)
            verdict["find_verdict"] = (
                "PASS" if len(rec.assistant_audio_after_find) > 5000
                else "SILENT" if rec.find_service_center_called
                else "NO-CALL"
            )

            # 3) Turn 2: yes clip (only proceed if find succeeded)
            if not rec.find_service_center_called:
                log.warning("  ▸ skipping turn 2 — find_service_center never fired")
            else:
                log.info("Turn 2: streaming yes-%s.wav", persona)
                short_trail = os.environ.get("BISECT_SHORT_TRAIL") == "1"
                no_sleep_after = os.environ.get("BISECT_NO_SLEEP_AFTER") == "1"
                fast_pacing = os.environ.get("BISECT_FAST_PACING") == "1"
                await stream_clip(
                    ws, yes_pcm,
                    ios_exact=ios_pacing,
                    short_trail=short_trail,
                    no_sleep_after_last=no_sleep_after,
                    fast_pacing=fast_pacing,
                )

                await wait_for(
                    rec,
                    lambda r: r.book_result is not None,
                    POST_BOOK_TIMEOUT_S,
                    "book tool.result",
                )
                verdict["book_called"] = rec.book_called

                # Wait extra 10s after book tool.result for assistant audio.
                # This is THE window where Nova goes silent post-tool.
                await asyncio.sleep(10.0)
                verdict["book_audio_bytes"] = len(rec.assistant_audio_after_book)
                verdict["book_verdict"] = (
                    "PASS" if len(rec.assistant_audio_after_book) > 5000
                    else "SILENT" if rec.book_called
                    else "NO-CALL"
                )

            # 4) clean shutdown
            stop.set()
            try:
                await ws.send(json.dumps({"type": "session.end"}))
            except Exception:
                pass
            try:
                await asyncio.wait_for(recv_task, timeout=2.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                recv_task.cancel()

    except websockets.exceptions.InvalidStatus as e:
        log.error("WS handshake failed: %s", e.response.status_code)
        verdict["errors"].append(f"handshake-{e.response.status_code}")
    except Exception as e:  # noqa: BLE001
        log.exception("Run failed: %s", e)
        verdict["errors"].append(str(e))

    if rec.errors:
        verdict["errors"].extend(rec.errors)

    if rec.all_audio:
        out = output_dir / f"vsa-multiturn-{seq:02d}-response.wav"
        write_wav(out, bytes(rec.all_audio))
        verdict["audio_path"] = str(out)

    return verdict


async def main_async(args) -> int:
    cfg = Config.from_env()
    if args.force_reauth:
        clear_cached_tokens()
    jwt = get_cached_id_token(cfg)
    log.info("JWT len=%d tenant=%s vin=%s", len(jwt), cfg.tenant_id, cfg.default_vin)
    agent_arn = load_agent_arn()
    log.info("Agent ARN: %s", agent_arn)

    out_dir = Path("/tmp/vsa-multiturn")
    out_dir.mkdir(parents=True, exist_ok=True)

    verdicts: list[dict] = []
    for seq in range(1, args.runs + 1):
        v = await run_session(
            persona=args.persona,
            seq=seq,
            cfg=cfg,
            jwt=jwt,
            agent_arn=agent_arn,
            output_dir=out_dir,
            ios_ramp=args.ios_style or args.ios_ramp,
            ios_pacing=args.ios_style or args.ios_pacing,
        )
        verdicts.append(v)
        log.info(
            "  RESULT #%d find=%s (%dB) book=%s (%dB) errors=%s",
            seq,
            v["find_verdict"], v["find_audio_bytes"],
            v["book_verdict"], v["book_audio_bytes"],
            v["errors"],
        )
        # small breath between runs
        if seq < args.runs:
            await asyncio.sleep(3.0)

    # Summary
    log.info("=" * 60)
    log.info("Summary over %d runs (persona=%s)", args.runs, args.persona)
    find_pass = sum(1 for v in verdicts if v["find_verdict"] == "PASS")
    find_silent = sum(1 for v in verdicts if v["find_verdict"] == "SILENT")
    find_nocall = sum(1 for v in verdicts if v["find_verdict"] == "NO-CALL")
    book_pass = sum(1 for v in verdicts if v["book_verdict"] == "PASS")
    book_silent = sum(1 for v in verdicts if v["book_verdict"] == "SILENT")
    book_nocall = sum(1 for v in verdicts if v["book_verdict"] == "NO-CALL")
    log.info(
        "  find_service_center: PASS=%d SILENT=%d NO-CALL=%d",
        find_pass, find_silent, find_nocall,
    )
    log.info(
        "  book:                PASS=%d SILENT=%d NO-CALL=%d",
        book_pass, book_silent, book_nocall,
    )

    summary_path = out_dir / "summary.json"
    summary_path.write_text(json.dumps(verdicts, indent=2, default=str))
    log.info("Verdicts saved to %s", summary_path)
    return 0


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--persona", default="fleet", choices=["fleet", "oem", "rental"])
    p.add_argument("--runs", type=int, default=1)
    p.add_argument("--force-reauth", action="store_true")
    p.add_argument("--ios-style", action="store_true",
                   help="Replicate iOS session-start ramp + iOS-exact streaming")
    p.add_argument("--ios-pacing", action="store_true",
                   help="Only iOS-exact stream pacing (90 pct, no sleep after last audio, 4 trailing silence)")
    p.add_argument("--ios-ramp", action="store_true",
                   help="Only iOS session-start ramp (wake-up + 3 keep-alives)")
    args = p.parse_args()
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    sys.exit(main())
