"""MockBidiModel — deterministic drop-in for BidiNovaSonicModel.

Replays a scripted event sequence (tool calls + response text) without
calling real Nova Sonic. Used by Tier 2 conversation evals to test the
BidiAgent loop (event flow, tool dispatch, asyncio.create_task plumbing).

Verified against strands-agents==1.37.0 BidiModel interface (docs/tech.md).
BidiModel is @runtime_checkable Protocol — requires config attr + async start/stop.
"""
from __future__ import annotations

import asyncio
import uuid
from typing import Any, AsyncGenerator

from strands.experimental.bidi import (
    BidiConnectionCloseEvent,
    BidiConnectionStartEvent,
    BidiResponseCompleteEvent,
    BidiTranscriptStreamEvent,
)
from strands.types._events import ToolResultEvent, ToolUseStreamEvent


class MockBidiModel:
    """Deterministic BidiModel that replays a scripted event sequence.

    Implements the BidiModel Protocol interface (start/stop/send/receive + config)
    so BidiAgent accepts it via isinstance(model, BidiModel).

    The scripted sequence is:
      1. Emit BidiConnectionStartEvent
      2. For each scripted tool call: emit ToolUseStreamEvent, wait for ToolResultEvent
      3. Emit BidiTranscriptStreamEvent (final) with scripted_response_text
      4. Emit BidiResponseCompleteEvent
      5. Emit BidiConnectionCloseEvent (terminates the agent loop)

    Attributes:
        captured_events: All events emitted by this model (dicts).
        captured_sends: All inputs received via send() (dicts).
    """

    # Required by BidiModel Protocol for isinstance() check
    config: dict[str, Any] = {}

    def __init__(
        self,
        *,
        scripted_tool_calls: list[dict],
        scripted_response_text: str,
        model_id: str = "mock-bidi-v1",
    ) -> None:
        self.scripted_tool_calls = scripted_tool_calls
        self.scripted_response_text = scripted_response_text
        self.model_id = model_id
        self.config = {}

        self.captured_events: list[dict] = []
        self.captured_sends: list[dict] = []

        # Queue receives ToolResultEvent objects from send(); receive() waits on it.
        self._tool_result_queue: asyncio.Queue = asyncio.Queue()
        self._connection_id = f"mock-conn-{uuid.uuid4().hex[:8]}"

    async def start(
        self,
        system_prompt: str | None = None,
        tools: list[Any] | None = None,
        messages: list[Any] | None = None,
        **kwargs: Any,
    ) -> None:
        """Called by BidiAgent loop before receive() is consumed."""
        # Reset queue in case of restart
        self._tool_result_queue = asyncio.Queue()

    async def stop(self) -> None:
        """Called by BidiAgent loop on shutdown."""

    async def send(self, content: Any) -> None:
        """Receive input from the agent (user text, audio, or tool results).

        Tool results are enqueued so receive() can unblock and continue.
        All inputs are captured in captured_sends.
        """
        if isinstance(content, dict):
            self.captured_sends.append(dict(content))
        else:
            self.captured_sends.append({"raw": repr(content)})

        if isinstance(content, ToolResultEvent):
            self._tool_result_queue.put_nowait(content)

    async def receive(self) -> AsyncGenerator[Any, None]:
        """Async generator that yields the scripted event sequence.

        The agent loop reads from this generator. For each scripted tool call,
        we yield a ToolUseStreamEvent then wait for the agent to call send()
        with the corresponding ToolResultEvent before proceeding.
        """
        # 1. Connection start — required so the agent loop initialises properly.
        conn_start = BidiConnectionStartEvent(
            connection_id=self._connection_id,
            model=self.model_id,
        )
        self.captured_events.append(dict(conn_start))
        yield conn_start

        # 2. Scripted tool calls — one ToolUseStreamEvent per entry.
        for tool_spec in self.scripted_tool_calls:
            tool_use_id = f"mock-tool-{uuid.uuid4().hex[:8]}"
            tool_use = {
                "toolUseId": tool_use_id,
                "name": tool_spec["name"],
                "input": tool_spec.get("args") or tool_spec.get("args_contain") or {},
            }
            event = ToolUseStreamEvent(
                delta={"toolUse": tool_use},
                current_tool_use=dict(tool_use),
            )
            self.captured_events.append(dict(event))
            yield event

            # Wait for the agent to execute the tool and call send(ToolResultEvent).
            await self._tool_result_queue.get()

        # 3. Text response — emit as a final BidiTranscriptStreamEvent.
        #    Narration timing: text BEFORE response_end (spec constraint).
        transcript = BidiTranscriptStreamEvent(
            delta={"text": self.scripted_response_text},
            text=self.scripted_response_text,
            role="assistant",
            is_final=True,
            current_transcript=self.scripted_response_text,
        )
        self.captured_events.append(dict(transcript))
        yield transcript

        # 4. Response complete.
        response_id = f"mock-resp-{uuid.uuid4().hex[:8]}"
        resp_complete = BidiResponseCompleteEvent(
            response_id=response_id,
            stop_reason="complete",
        )
        self.captured_events.append(dict(resp_complete))
        yield resp_complete

        # 5. Connection close — signals the agent loop to stop iterating.
        conn_close = BidiConnectionCloseEvent(
            connection_id=self._connection_id,
            reason="user_request",
        )
        self.captured_events.append(dict(conn_close))
        yield conn_close
