#!/usr/bin/env python3
"""One-off test: stream an arbitrary WAV as a single user turn.

Useful for testing the diy_repair_advisor and other sub-agents
with custom Polly-generated phrases without rebuilding iOS.
Reuses the multiturn harness's session.start, audio chunking,
and event capture.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
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

from agents.supervisor.config import Config, get_cached_id_token  # noqa: E402

# websockets 13.x: the legacy `websockets.connect` takes `extra_headers`, while the
# new asyncio client takes `additional_headers`. Passing the new name to the legacy
# client forwards it to asyncio and raises
# `create_connection() got an unexpected keyword argument 'additional_headers'`.
# Import the asyncio client explicitly so the kwarg matches, falling back to the
# legacy spelling on older installs. websockets 14 removes the legacy client, so this
# also stops the harness breaking on the next upgrade.
try:  # websockets >= 13.0
    from websockets.asyncio.client import connect as _ws_connect

    def ws_connect(url, headers, **kw):
        return _ws_connect(url, additional_headers=headers, **kw)
except ImportError:  # pragma: no cover — websockets < 13
    def ws_connect(url, headers, **kw):
        return websockets.connect(url, extra_headers=headers, **kw)


INPUT_RATE = 16000
CHUNK_MS = 200
TURN_SILENCE_CHUNKS = 10  # 2000ms — Nova v2 minimum


def load_agent_arn() -> str:
    """Resolve the bidi runtime ARN for the CURRENT stage.

    Was hardcoded to `agents["vsa_supervisor_bidi"]`, which is the us-east-1 runtime
    — so running this harness under `with-stage-env.sh staging` still exercised a
    different runtime in a different region, and nothing appeared in the staging log
    group. That cost a debugging session on 2026-08-21: the test passed, produced a
    real transcript and real audio, and the instrumentation it was meant to validate
    saw none of it.

    Prefers the stage-suffixed entry, falls back to the unsuffixed one, and PRINTS
    which it chose — a harness that quietly targets the wrong environment is worse
    than one that fails.
    """
    cfg = yaml.safe_load((REPO_ROOT / ".bedrock_agentcore.yaml").read_text())
    stage = os.environ.get("VSA_STAGE", "").strip()
    candidates = [f"vsa_supervisor_bidi_{stage}"] if stage else []
    candidates.append("vsa_supervisor_bidi")
    for name in candidates:
        entry = cfg["agents"].get(name)
        if entry:
            arn = entry["bedrock_agentcore"]["agent_arn"]
            region = arn.split(":")[3]
            print(f"  runtime: {name} ({region})")
            return arn
    raise SystemExit(f"no bidi runtime in .bedrock_agentcore.yaml among {candidates}")

def read_wav(path: Path) -> bytes:
    with wave.open(str(path), "rb") as w:
        assert w.getnchannels() == 1
        assert w.getsampwidth() == 2
        assert w.getframerate() == INPUT_RATE, f"want {INPUT_RATE}Hz got {w.getframerate()}"
        return w.readframes(w.getnframes())


async def stream_clip(ws, pcm: bytes):
    chunk_bytes = (INPUT_RATE * 2 * CHUNK_MS) // 1000
    for i in range(0, len(pcm), chunk_bytes):
        chunk = pcm[i:i + chunk_bytes]
        await ws.send(json.dumps({
            "type": "audio.chunk",
            "data": base64.b64encode(chunk).decode("ascii"),
            "sampleRate": INPUT_RATE,
        }))
        await asyncio.sleep(CHUNK_MS / 1000.0)
    silence = b"\x00" * chunk_bytes
    for _ in range(TURN_SILENCE_CHUNKS):
        await ws.send(json.dumps({
            "type": "audio.chunk",
            "data": base64.b64encode(silence).decode("ascii"),
            "sampleRate": INPUT_RATE,
        }))
        await asyncio.sleep(CHUNK_MS / 1000.0)


async def run(wav_path: Path, timeout_s: float = 30.0):
    cfg = Config.from_env()
    jwt = get_cached_id_token(cfg)
    arn = load_agent_arn()
    sid = f"vsa-wav-test-{uuid.uuid4().hex[:8]}"
    print(f"session={sid} wav={wav_path}")
    pcm = read_wav(wav_path)
    print(f"  pcm: {len(pcm)} bytes (~{len(pcm)/2/INPUT_RATE:.2f}s)")

    from bedrock_agentcore.runtime import AgentCoreRuntimeClient
    rt = AgentCoreRuntimeClient(region=cfg.aws_region)
    ws_url, signed = rt.generate_ws_connection(runtime_arn=arn, session_id=sid)
    signed["X-Amzn-Bedrock-AgentCore-Runtime-Custom-User-Token"] = jwt
    signed["X-Amzn-Bedrock-AgentCore-Runtime-Custom-Tenant-Id"] = cfg.tenant_id
    signed["X-Amzn-Bedrock-AgentCore-Runtime-Custom-Vin"] = cfg.default_vin
    signed["X-Amzn-Bedrock-AgentCore-Runtime-Custom-VehicleId"] = cfg.default_vehicle_id
    signed["X-Amzn-Bedrock-AgentCore-Runtime-Custom-DriverId"] = cfg.default_driver_id

    tool_calls = []
    tool_results = []
    asst_text = []
    audio_bytes = 0
    user_text = []

    async with ws_connect(
        ws_url, signed, max_size=2**24, open_timeout=10
    ) as ws:
        await ws.send(json.dumps({
            "type": "session.start",
            "tenantId": cfg.tenant_id, "vin": cfg.default_vin, "jwt": jwt,
            "vehicleId": cfg.default_vehicle_id, "driverId": cfg.default_driver_id,
        }))

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
                t = evt.get("type")
                if t == "audio.chunk":
                    nonlocal audio_bytes
                    audio_bytes += len(base64.b64decode(evt.get("data") or ""))
                elif t == "transcript":
                    role = evt.get("role")
                    text = (evt.get("text") or "").strip()
                    if evt.get("isFinal") and text:
                        if role == "user":
                            user_text.append(text)
                            print(f"[{time.strftime('%H:%M:%S')}] user: {text!r}")
                        else:
                            asst_text.append(text)
                            print(f"[{time.strftime('%H:%M:%S')}] asst: {text!r}")
                elif t == "tool.call":
                    tool_calls.append(evt.get("name"))
                    print(f"[{time.strftime('%H:%M:%S')}] tool.call {evt.get('name')} {evt.get('input')}")
                elif t == "tool.result":
                    tool_results.append(evt.get("name"))
                    # Tool outputs are not uniformly dicts: `retrieve` returns a
                    # LIST of KB hits, and `(list).get("narration")` raised
                    # AttributeError, killing the harness mid-session on the P0420
                    # clip — a crash in the test, reported as if the agent had failed.
                    out = evt.get("output")
                    if isinstance(out, dict):
                        narr = out.get("narration") or ""
                    elif isinstance(out, list):
                        narr = f"<{len(out)} item(s)>"
                    else:
                        narr = "" if out is None else str(out)[:120]
                    print(f"[{time.strftime('%H:%M:%S')}] tool.result {evt.get('name')} narration={narr[:120]!r}")

        recv_task = asyncio.create_task(receiver())

        print(f"[{time.strftime('%H:%M:%S')}] streaming WAV...")
        await stream_clip(ws, pcm)
        print(f"[{time.strftime('%H:%M:%S')}] WAV streamed; waiting for response...")

        await asyncio.sleep(timeout_s)

        stop.set()
        try:
            await ws.send(json.dumps({"type": "session.end"}))
        except Exception:
            pass
        try:
            await asyncio.wait_for(recv_task, timeout=2.0)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            recv_task.cancel()

    print()
    print("=== Summary ===")
    print(f"  user transcript: {user_text}")
    print(f"  tool calls:      {tool_calls}")
    print(f"  tool results:    {tool_results}")
    print(f"  asst text:       {asst_text}")
    print(f"  asst audio:      {audio_bytes} bytes (~{audio_bytes/2/24000:.2f}s)")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--wav", default="/tmp/diy-tire-test.wav")
    p.add_argument("--timeout", type=float, default=20.0)
    args = p.parse_args()
    asyncio.run(run(Path(args.wav), args.timeout))


if __name__ == "__main__":
    main()
