"""Async WebSocket client for invoking a deployed AgentCore runtime.

Wire format matches the iOS client documented in bidi_app_canonical.py:
  Outbound: audio.chunk, text.input, tool.result, session.start, session.end
  Inbound:  audio.chunk, transcript, tool.call, tool.result,
            escalation, interruption, session.start, session.end

SigV4 auth via AgentCoreRuntimeClient.generate_ws_connection() (preferred)
or hand-rolled via requests_aws4auth (fallback).

AgentCore session timeout: 55s. Client enforces 50s safety margin.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import time
from typing import Any, AsyncGenerator, Callable, Awaitable

# Wire-protocol constants — must match bidi_app_canonical.py
INPUT_SAMPLE_RATE = 16000
OUTPUT_SAMPLE_RATE = 24000

# Client-side timeout (50s) — AgentCore hard limit is 55s
CLIENT_TIMEOUT_S = 50.0

# When wait_for_turn=True, defer session.end until tool.result arrives or
# this many seconds have elapsed since the first audio chunk was sent.
AUDIO_TURN_DEADLINE_S = 25.0

# Audio tail: 5×100 ms of zero-PCM appended after the WAV payload when
# wait_for_turn=True.  Defense-in-depth against Nova VAD missing the
# end-of-utterance due to per-deploy startup latency.
# 100 ms @ 16 kHz mono 16-bit = 1600 frames = 3200 bytes.
_AUDIO_TAIL = b"\x00" * 3200


def _build_runtime_arn(runtime_id: str, region: str) -> str:
    """Construct the AgentCore runtime ARN from runtime ID + region + account."""
    import boto3
    account = boto3.client("sts", region_name=region).get_caller_identity()["Account"]
    return f"arn:aws:bedrock-agentcore:{region}:{account}:runtime/{runtime_id}"


def _get_ws_connection(runtime_arn: str, region: str) -> tuple[str, dict[str, str]]:
    """Return (wss_url, headers) using AgentCoreRuntimeClient (preferred) or fallback."""
    try:
        from bedrock_agentcore.runtime import AgentCoreRuntimeClient
        client = AgentCoreRuntimeClient(region=region)
        return client.generate_ws_connection(runtime_arn=runtime_arn)
    except (ImportError, AttributeError):
        pass

    # Fallback: hand-roll SigV4 via requests_aws4auth + manual upgrade
    import boto3
    from requests_aws4auth import AWS4Auth
    import urllib.parse

    session = boto3.Session(region_name=region)
    creds = session.get_credentials().get_frozen_credentials()
    auth = AWS4Auth(
        creds.access_key,
        creds.secret_key,
        region,
        "bedrock-agentcore",
        session_token=creds.token,
    )
    host = f"bedrock-agentcore.{region}.amazonaws.com"
    path = f"/runtimes/{urllib.parse.quote(runtime_arn, safe='')}/ws"
    wss_url = f"wss://{host}{path}"

    # Sign a dummy GET request to extract the Authorization header
    import requests
    dummy = requests.Request("GET", f"https://{host}{path}")
    prepared = dummy.prepare()
    auth(prepared)
    headers = {k: v for k, v in prepared.headers.items()
               if k.lower() in ("authorization", "x-amz-date", "x-amz-security-token", "host")}
    return wss_url, headers


class AgentCoreWSClient:
    """Async WebSocket client for a deployed AgentCore runtime.

    Usage::

        client = AgentCoreWSClient()
        async for event in client.stream(text="Hello"):
            print(event)

    All sent and received events are captured in ``client.trace``.
    """

    def __init__(
        self,
        runtime_id: str | None = None,
        region: str | None = None,
    ) -> None:
        self.runtime_id = runtime_id or os.environ.get("RUNTIME_ID")
        self.region = region or os.environ.get("AWS_REGION", "us-east-1")
        self.trace: list[dict[str, Any]] = []

    def _record(self, direction: str, event: dict[str, Any]) -> None:
        """Record an incoming or outgoing WebSocket event with timestamp."""
        self.trace.append({"ts": time.monotonic(), "dir": direction, **event})

    def _make_text_event(self, text: str) -> dict[str, Any]:
        """Create a text.input event for the WebSocket protocol."""
        return {"type": "text.input", "text": text}

    def _make_audio_event(self, audio_bytes: bytes) -> dict[str, Any]:
        """Create an audio.chunk event (base64-encoded) for the WebSocket protocol."""
        return {
            "type": "audio.chunk",
            # Field MUST be "data" — bidi_app.py input_callable reads msg.get("data")
            # (wire protocol at bidi_app.py:35). Using "audio" silently drops every
            # chunk, so no speech reaches Nova Sonic and the turn never generates.
            "data": base64.b64encode(audio_bytes).decode(),
            "sampleRate": INPUT_SAMPLE_RATE,
        }

    def _make_session_start(self, **kwargs: Any) -> dict[str, Any]:
        """Create a session.start event for the WebSocket protocol."""
        return {"type": "session.start", **kwargs}

    def _make_session_end(self) -> dict[str, Any]:
        """Create a session.end event for the WebSocket protocol."""
        return {"type": "session.end"}

    async def stream(
        self,
        *,
        text: str | None = None,
        audio_bytes: bytes | None = None,
        session_meta: dict[str, Any] | None = None,
        extra_headers: dict[str, str] | None = None,
        wait_for_turn: bool = False,
        on_event: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    ) -> AsyncGenerator[dict[str, Any], None]:
        """Connect to AgentCore, send input, yield incoming events.

        Args:
            text: Text input to send (mutually exclusive with audio_bytes).
            audio_bytes: Raw LPCM audio at INPUT_SAMPLE_RATE to send.
            session_meta: Extra fields merged into the session.start event.
            extra_headers: Custom headers merged with SigV4 headers.
            wait_for_turn: When True and audio_bytes is set, defer session.end
                until a tool.result event is received or AUDIO_TURN_DEADLINE_S
                seconds have elapsed since the first chunk was sent.  Also
                appends a 500 ms zero-PCM tail before session.end for defense
                against Nova VAD missing the end-of-utterance.  Default
                behavior (False) is byte-for-byte unchanged.
            on_event: Optional async callback invoked for each incoming event.

        Yields:
            Parsed JSON events from the AgentCore runtime.
        """
        if not self.runtime_id:
            raise RuntimeError(
                "RUNTIME_ID env var is required for real AgentCore connections. "
                "Set RUNTIME_ID or use --dry-run."
            )

        import websockets

        runtime_arn = _build_runtime_arn(self.runtime_id, self.region)
        ws_url, headers = _get_ws_connection(runtime_arn, self.region)

        # Merge any caller-supplied custom headers (e.g. the
        # x-amzn-bedrock-agentcore-runtime-custom-persona-id / -vin / -customer-id
        # headers the runtime reads — see agents/supervisor/bidi_app.py:211-234).
        # SigV4 headers take precedence on key collision; the custom-* namespace
        # is disjoint so no real collision occurs. Additive — values are never
        # logged.
        if extra_headers:
            headers = {**extra_headers, **headers}

        # NOTE: top-level websockets.connect (v13.x) is the LEGACY API whose
        # header kwarg is `extra_headers` (the new websockets.asyncio.client API
        # uses `additional_headers`). Passing `additional_headers` here falls
        # through to asyncio's create_connection and raises
        # "unexpected keyword argument 'additional_headers'".
        async with websockets.connect(ws_url, extra_headers=headers) as ws:
            # Send session.start
            start_event = self._make_session_start(**(session_meta or {}))
            self._record("send", start_event)
            await ws.send(json.dumps(start_event))

            # Send the input
            if text is not None:
                evt = self._make_text_event(text)
                self._record("send", evt)
                await ws.send(json.dumps(evt))
            elif audio_bytes is not None:
                # Send in 100ms chunks (1600 samples @ 16kHz)
                chunk_size = INPUT_SAMPLE_RATE * 2 // 10  # 100ms of 16-bit PCM
                audio_start = asyncio.get_event_loop().time()
                for i in range(0, len(audio_bytes), chunk_size):
                    chunk = audio_bytes[i : i + chunk_size]
                    evt = self._make_audio_event(chunk)
                    self._record("send", evt)
                    await ws.send(json.dumps(evt))
                    await asyncio.sleep(0.1)

                if wait_for_turn:
                    # Append 500 ms zero-PCM tail (5×100 ms chunks) before
                    # session.end — defense-in-depth against Nova VAD missing
                    # the end-of-utterance due to per-deploy startup latency.
                    for _ in range(5):
                        evt = self._make_audio_event(_AUDIO_TAIL)
                        self._record("send", evt)
                        await ws.send(json.dumps(evt))
                        await asyncio.sleep(0.1)

            # When wait_for_turn=True on the audio path, drain inbound events
            # until we see a tool.result (the 7.B.4 signal) or the per-turn
            # deadline expires — THEN send session.end.
            # On any other path, send session.end immediately (existing behavior).
            turn_deadline = (
                asyncio.get_event_loop().time() + AUDIO_TURN_DEADLINE_S
                if wait_for_turn and audio_bytes is not None
                else None
            )

            # --- pre-session.end receive drain (audio wait_for_turn only) ---
            if turn_deadline is not None:
                overall_deadline = asyncio.get_event_loop().time() + CLIENT_TIMEOUT_S
                while True:
                    remaining = min(
                        turn_deadline - asyncio.get_event_loop().time(),
                        overall_deadline - asyncio.get_event_loop().time(),
                    )
                    if remaining <= 0:
                        break
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
                    except asyncio.TimeoutError:
                        break
                    except websockets.exceptions.ConnectionClosed:
                        break

                    try:
                        event = json.loads(raw)
                    except json.JSONDecodeError:
                        event = {"type": "raw", "data": raw}

                    self._record("recv", event)
                    if on_event:
                        await on_event(event)
                    yield event

                    if event.get("type") in ("session.end", "error"):
                        return  # terminal — skip session.end send + second drain
                    if (
                        event.get("type") == "tool.result"
                        and event.get("name") != "triage"
                    ):
                        # An AGENT (non-triage) tool.result means the turn
                        # produced its substantive result. We skip `triage`
                        # because the server-side auto-triage inject emits a
                        # triage tool.result early on fault-adjacent prompts —
                        # breaking on it would end the session before the
                        # agent's KB `retrieve` runs (decisions.md, the
                        # wait_for_turn race). Turn complete — send
                        # session.end, then drain.
                        break

            # Send session.end
            end_event = self._make_session_end()
            self._record("send", end_event)
            await ws.send(json.dumps(end_event))

            # Receive events until session ends or timeout
            deadline = asyncio.get_event_loop().time() + CLIENT_TIMEOUT_S
            while True:
                remaining = deadline - asyncio.get_event_loop().time()
                if remaining <= 0:
                    break
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
                except asyncio.TimeoutError:
                    break
                except websockets.exceptions.ConnectionClosed:
                    break

                try:
                    event = json.loads(raw)
                except json.JSONDecodeError:
                    event = {"type": "raw", "data": raw}

                self._record("recv", event)
                if on_event:
                    await on_event(event)
                yield event

                if event.get("type") in ("session.end", "error"):
                    break
