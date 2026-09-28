#!/usr/bin/env python3
"""Quick tester for the diy_repair_advisor sub-agent.

Sends a text.input asking DIY guidance, captures tool calls and the
narration Nova speaks back. Lets us iterate on the sub-agent prompt
and corpus without the iOS round-trip.

Usage:
    python scripts/test-diy-advisor.py
    python scripts/test-diy-advisor.py --query "Can I top off the tire myself?"
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import sys
import time
import uuid
from pathlib import Path

import websockets
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from agents.supervisor.config import (  # noqa: E402
    Config, clear_cached_tokens, get_cached_id_token,
)


def load_agent_arn() -> str:
    cfg = yaml.safe_load((REPO_ROOT / ".bedrock_agentcore.yaml").read_text())
    return cfg["agents"]["vsa_supervisor_bidi"]["bedrock_agentcore"]["agent_arn"]


async def run(query: str, timeout_s: float = 30.0):
    cfg = Config.from_env()
    jwt = get_cached_id_token(cfg)
    arn = load_agent_arn()
    session_id = f"vsa-diy-test-{uuid.uuid4().hex[:8]}"
    print(f"session={session_id} query={query!r}")

    from bedrock_agentcore.runtime import AgentCoreRuntimeClient
    rt = AgentCoreRuntimeClient(region=cfg.aws_region)
    ws_url, signed = rt.generate_ws_connection(runtime_arn=arn, session_id=session_id)
    signed["X-Amzn-Bedrock-AgentCore-Runtime-Custom-User-Token"] = jwt
    signed["X-Amzn-Bedrock-AgentCore-Runtime-Custom-Tenant-Id"] = cfg.tenant_id
    signed["X-Amzn-Bedrock-AgentCore-Runtime-Custom-Vin"] = cfg.default_vin
    signed["X-Amzn-Bedrock-AgentCore-Runtime-Custom-VehicleId"] = cfg.default_vehicle_id
    signed["X-Amzn-Bedrock-AgentCore-Runtime-Custom-DriverId"] = cfg.default_driver_id

    tool_calls: list[tuple[str, dict]] = []
    tool_results: list[tuple[str, dict]] = []
    assistant_text: list[str] = []
    audio_bytes = 0

    async with websockets.connect(
        ws_url, additional_headers=signed, max_size=2**24, open_timeout=10
    ) as ws:
        await ws.send(json.dumps({
            "type": "session.start",
            "tenantId": cfg.tenant_id, "vin": cfg.default_vin, "jwt": jwt,
            "vehicleId": cfg.default_vehicle_id, "driverId": cfg.default_driver_id,
        }))

        # Wake-up silence — Nova Sonic v2 needs a real audio "turn"
        # to register before it'll TTS anything. The original 200ms
        # wake-up chunk used by iOS works for in-stream Nova responses
        # (e.g. the (i) button's natural explanation) but NOT for our
        # post-tool narration-injection path. Send 10 chunks (~2000ms)
        # of silence to give Nova VAD a full end-of-turn signal —
        # same threshold the trailing-silence-after-demo-audio fix
        # established empirically.
        chunk_bytes = 16000 * 2 * 200 // 1000  # 200ms 16kHz mono PCM
        wake = b"\x00" * chunk_bytes
        for _ in range(10):
            await ws.send(json.dumps({
                "type": "audio.chunk",
                "data": base64.b64encode(wake).decode("ascii"),
                "sampleRate": 16000,
            }))
            await asyncio.sleep(0.2)

        # Send the user's question as text input.
        await ws.send(json.dumps({"type": "text.input", "text": query}))
        print(f"[{time.strftime('%H:%M:%S')}] sent text.input")

        deadline = time.time() + timeout_s
        while time.time() < deadline:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            except websockets.exceptions.ConnectionClosed:
                break
            try:
                evt = json.loads(raw)
            except json.JSONDecodeError:
                continue
            t = evt.get("type")
            if t == "audio.chunk":
                audio_bytes += len(base64.b64decode(evt.get("data") or ""))
            elif t == "transcript":
                if evt.get("role") == "assistant" and evt.get("isFinal"):
                    text = (evt.get("text") or "").strip()
                    if text:
                        assistant_text.append(text)
                        print(f"[{time.strftime('%H:%M:%S')}] asst: {text!r}")
            elif t == "tool.call":
                tool_calls.append((evt.get("name"), evt.get("input") or {}))
                print(f"[{time.strftime('%H:%M:%S')}] tool.call {evt.get('name')} {evt.get('input')}")
            elif t == "tool.result":
                tool_results.append((evt.get("name"), evt.get("output") or {}))
                narr = (evt.get("output") or {}).get("narration") or ""
                print(f"[{time.strftime('%H:%M:%S')}] tool.result {evt.get('name')} narration={narr[:120]!r}")
            elif t == "session.ended":
                break

        try:
            await ws.send(json.dumps({"type": "session.end"}))
        except Exception:
            pass

    print()
    print("=== Summary ===")
    print(f"  tool calls:      {[n for n, _ in tool_calls]}")
    print(f"  tool results:    {[n for n, _ in tool_results]}")
    print(f"  assistant text:  {assistant_text}")
    print(f"  audio bytes:     {audio_bytes}")
    if tool_results:
        for n, out in tool_results:
            if out.get("narration"):
                print(f"  {n} narration: {out['narration']}")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--query", default="Can I just top off the tire myself instead?")
    p.add_argument("--timeout", type=float, default=30.0)
    args = p.parse_args()
    asyncio.run(run(args.query, args.timeout))


if __name__ == "__main__":
    main()
