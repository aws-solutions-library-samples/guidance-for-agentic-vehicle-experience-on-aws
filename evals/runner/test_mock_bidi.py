"""Tests for MockBidiModel.

Verifies that MockBidiModel correctly replays a scripted event sequence
through a real BidiAgent, exercising the agent loop without calling Nova.
"""
from __future__ import annotations

import asyncio
from typing import Any

import pytest
from strands import tool
from strands.experimental.bidi.agent import BidiAgent
from strands.experimental.bidi import BidiTextInputEvent
from strands.types._events import ToolUseStreamEvent

from evals.runner.mock_bidi import MockBidiModel


@pytest.mark.anyio
async def test_mock_bidi_one_tool_scripted_sequence():
    """MockBidiModel replays one scripted tool call and returns the scripted response."""

    # --- Stub tool — captures received kwargs ---
    received_kwargs: list[dict[str, Any]] = []

    @tool
    def triage(vin: str) -> dict:
        """Triage a vehicle fault code.

        Args:
            vin: Vehicle identification number.
        """
        received_kwargs.append({"vin": vin})
        return {"classification": "P2", "vin": vin}

    # --- Build mock model ---
    model = MockBidiModel(
        scripted_tool_calls=[{"name": "triage", "args": {"vin": "1FTRX18W6XKB12345"}}],
        scripted_response_text="That's a P2 alert.",
    )

    # --- Capture output events ---
    captured_output: list[dict] = []

    async def capture_callback(event: dict) -> None:
        captured_output.append(event)

    # --- Build agent and run ---
    agent = BidiAgent(model=model, tools=[triage])

    sent = {"done": False}

    async def input_callable() -> dict:
        if not sent["done"]:
            sent["done"] = True
            return BidiTextInputEvent(text="my engine light is on")
        await asyncio.Event().wait()  # type: ignore[attr-defined]

    await agent.run(inputs=[input_callable], outputs=[capture_callback])

    # --- Assertions ---

    # 1. model.captured_events contains a ToolUseStreamEvent for 'triage'
    tool_use_events = [
        e for e in model.captured_events
        if e.get("type") == "tool_use_stream"
        and e.get("current_tool_use", {}).get("name") == "triage"
    ]
    assert len(tool_use_events) == 1, (
        f"Expected 1 triage ToolUseStreamEvent, got {len(tool_use_events)}. "
        f"captured_events={model.captured_events}"
    )

    # 2. Captured output text matches the scripted response
    transcript_texts = [
        e.get("text", "") or e.get("current_transcript", "")
        for e in captured_output
        if e.get("type") == "bidi_transcript_stream"
    ]
    assert any("P2 alert" in t for t in transcript_texts), (
        f"Expected scripted response in transcript events. Got: {transcript_texts}"
    )

    # 3. model.captured_sends contains the user input
    assert any(
        s.get("type") == "bidi_text_input" for s in model.captured_sends
    ), f"Expected bidi_text_input in captured_sends. Got: {model.captured_sends}"

    # 4. Tool received the correct args (end-to-end args propagation)
    assert received_kwargs, "tool was never called — mock did not propagate tool_use to agent"
    assert received_kwargs[0]["vin"] == "1FTRX18W6XKB12345", (
        f"expected vin propagated to tool, got {received_kwargs[0]}"
    )


@pytest.mark.anyio
async def test_mock_bidi_args_contain_propagated():
    """MockBidiModel reads args_contain key and propagates args to the tool."""

    received_kwargs: list[dict[str, Any]] = []

    @tool
    def triage(vin: str) -> dict:
        """Triage a vehicle fault code.

        Args:
            vin: Vehicle identification number.
        """
        received_kwargs.append({"vin": vin})
        return {"classification": "P2", "vin": vin}

    model = MockBidiModel(
        scripted_tool_calls=[{"name": "triage", "args_contain": {"vin": "1FTRX18W6XKB12345"}}],
        scripted_response_text="P2 confirmed.",
    )

    agent = BidiAgent(model=model, tools=[triage])

    sent = {"done": False}

    async def input_callable() -> dict:
        if not sent["done"]:
            sent["done"] = True
            return BidiTextInputEvent(text="check my vehicle")
        await asyncio.Event().wait()  # type: ignore[attr-defined]

    await agent.run(inputs=[input_callable], outputs=[])

    assert received_kwargs, "tool was never called — args_contain not propagated"
    assert received_kwargs[0]["vin"] == "1FTRX18W6XKB12345", (
        f"expected vin='1FTRX18W6XKB12345' from args_contain, got {received_kwargs[0]}"
    )
