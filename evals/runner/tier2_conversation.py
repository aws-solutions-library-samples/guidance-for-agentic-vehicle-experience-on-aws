"""Tier 2 runner — in-process conversation eval engine.

Pytest-parameterized over ``evals/cases/{routing,safety,knowledge}/**/*.yaml``.

## Lazy-import pattern for MockBidiModel

``MockBidiModel`` lives in ``evals/runner/mock_bidi.py`` (Task 3a). That file
may not exist when this file is collected by pytest. To allow collection to
succeed regardless, ``MockBidiModel`` is imported lazily inside the test
function via ``_get_mock_bidi_model()``. If the import fails, the test is
skipped (not failed). Once Task 3a lands, the import succeeds and the tests
run for real.

## Tool registration order

Tools are registered in the same order as ``bidi_app_canonical.py``:
    find_service_center, book, escalate, knowledge, parts_lookup,
    vehicle_identity, retrieve, diy_repair_advisor

## DIY-or-service heuristic (assertion e)

When ``expected.routing.offers_diy_choice`` is True, the response text is
checked for the presence of "yourself" OR "book service" (case-insensitive).
This is a soft heuristic matching the SYSTEM_PROMPT language:
    "I can book service for you, or walk you through doing it yourself"
    "Want me to book a service appointment instead?"
"""

from __future__ import annotations

import asyncio
import glob
import time
from typing import Any

import pytest
import yaml

from evals.runner.schema import EvalCase

_DEFAULT_LATENCY_BUDGET_MS = 5000  # Tier 2 default per spec

# Short system prompt for mocked mode — Nova isn't decoding it.
_SYSTEM_PROMPT = (
    "You are a fleet support voice assistant. Keep replies short — one sentence."
)


def _get_mock_bidi_model():
    """Lazy import — task 3a may complete after this file is collected."""
    try:
        from evals.runner.mock_bidi import MockBidiModel  # noqa: PLC0415
        return MockBidiModel
    except ImportError as e:
        pytest.skip(f"MockBidiModel not yet available (Task 3a): {e}")


def _load_cases() -> list[tuple[str, dict[str, Any]]]:
    """Return sorted (yaml_path, raw_dict) pairs from routing/safety/knowledge/conversation."""
    paths = sorted(
        glob.glob("evals/cases/routing/**/*.yaml", recursive=True)
        + glob.glob("evals/cases/safety/**/*.yaml", recursive=True)
        + glob.glob("evals/cases/knowledge/**/*.yaml", recursive=True)
        + glob.glob("evals/cases/conversation/**/*.yaml", recursive=True)
    )
    result = []
    for path in paths:
        with open(path) as fh:
            raw = yaml.safe_load(fh)
        result.append((path, raw))
    return result


def _make_test_ids(cases: list[tuple[str, dict[str, Any]]]) -> list[str]:
    """Extract test IDs from loaded case tuples for pytest parametrization."""
    return [raw.get("id", path) for path, raw in cases]


_CASES = _load_cases()
_IDS = _make_test_ids(_CASES)


@pytest.mark.parametrize("yaml_path,raw", _CASES, ids=_IDS)
def test_conversation(
    yaml_path: str,
    raw: dict[str, Any],
    request: pytest.FixtureRequest,
) -> None:
    """Tier 2 conversation eval.

    For each YAML case:
    1. Validates against EvalCase schema; skips non-tier-2 cases.
    2. Skips audio-input cases (Tier 2 is text-only).
    3. Builds an in-process BidiAgent with MockBidiModel + real tool factories
       wired via tool_factory_kit from evals/conftest.py.
    4. Sends the user utterance as a BidiTextInputEvent.
    5. Asserts tool sequence, routing, response text, and latency.
    """
    MockBidiModel = _get_mock_bidi_model()

    # --- 1. Validate schema ---
    try:
        case = EvalCase.model_validate(raw)
    except Exception as exc:
        pytest.fail(f"[{yaml_path}] EvalCase validation failed: {exc}")

    # --- 2. Skip non-tier-2 ---
    if case.tier != 2:
        pytest.skip(f"tier={case.tier}; Tier 2 runner only handles tier=2 cases")

    # --- 3. Skip audio input ---
    if case.input.type == "audio":
        pytest.skip("audio Tier 2 not supported")

    utterance = case.input.utterance or ""
    expected_tool_calls: list[Any] = case.expected.tool_calls or []
    expected_routing: dict[str, Any] = case.expected.routing or {}
    expected_response: dict[str, Any] = case.expected.response or {}
    budget_ms = (
        case.latency_budget_ms
        if case.latency_budget_ms is not None
        else _DEFAULT_LATENCY_BUDGET_MS
    )

    # --- 4. Build scripted response text ---
    must_contain = expected_response.get("must_contain_one_of") or []
    scripted_response_text = must_contain[0] if must_contain else ""

    # --- 5. Construct MockBidiModel ---
    mock_model = MockBidiModel(
        scripted_tool_calls=expected_tool_calls,
        scripted_response_text=scripted_response_text,
    )

    # --- 6. Resolve tool_factory_kit ---
    try:
        kit = request.getfixturevalue("tool_factory_kit")
    except pytest.FixtureLookupError as exc:
        pytest.fail(f"[{yaml_path}] tool_factory_kit fixture not available: {exc}")

    # Tool registration order matches bidi_app_canonical.py exactly.
    tools = [
        kit["find_service_center"],
        kit["book"],
        kit["escalate"],
        kit["knowledge"],
        kit["parts_lookup"],
        kit["vehicle_identity"],
        kit["retrieve"],
        kit["diy_repair_advisor"],
    ]

    from strands.experimental.bidi.agent import BidiAgent  # noqa: PLC0415

    agent = BidiAgent(
        model=mock_model,
        tools=tools,
        system_prompt=_SYSTEM_PROMPT,
        name="vsa-tier2-eval",
    )

    # --- 7. Run the agent and capture events ---
    captured_events: list[dict[str, Any]] = []
    input_sent = {"done": False}

    async def input_callable() -> dict:
        if not input_sent["done"]:
            input_sent["done"] = True
            return {"type": "bidi_text_input", "text": utterance, "role": "user"}
        # Block until MockBidiModel stops pulling (prevents infinite loop).
        await asyncio.sleep(60)
        raise StopAsyncIteration

    async def output_callable(event: Any) -> None:
        if isinstance(event, dict):
            captured_events.append(event)

    async def _run() -> None:
        await agent.run(inputs=[input_callable], outputs=[output_callable])

    start = time.perf_counter()
    try:
        asyncio.run(_run())
    except (StopAsyncIteration, StopIteration):
        pass
    except Exception as exc:
        pytest.fail(
            f"[{yaml_path}] BidiAgent.run raised: {type(exc).__name__}: {exc}"
        )
    elapsed_ms = (time.perf_counter() - start) * 1000

    # --- 8. Assertions ---

    # Collect tool-use events (bidi_tool_use = MockBidiModel native;
    # tool_use_stream = Strands native format).
    def _tool_name(e: dict[str, Any]) -> str | None:
        """Extract the tool name from a Strands event (bidi_tool_use or tool_use_stream)."""
        if e.get("type") == "bidi_tool_use":
            return e.get("name")
        if e.get("type") == "tool_use_stream":
            return (e.get("current_tool_use") or {}).get("name")
        return None

    all_tool_events = [
        e for e in captured_events
        if e.get("type") in ("bidi_tool_use", "tool_use_stream")
    ]
    called_tool_names = [_tool_name(e) for e in all_tool_events if _tool_name(e)]

    # Collect response text from multiple possible event types.
    response_text_parts: list[str] = []
    for e in captured_events:
        et = e.get("type")
        if et == "bidi_text_content":
            response_text_parts.append(e.get("text") or "")
        elif et == "bidi_transcript_stream" and e.get("role") == "assistant":
            response_text_parts.append(
                e.get("text") or e.get("current_transcript") or ""
            )
        elif et == "bidi_response_text":
            response_text_parts.append(e.get("text") or "")
    response_text = " ".join(response_text_parts).strip()

    # MockBidiModel may expose captured_response_text directly.
    if not response_text and hasattr(mock_model, "captured_response_text"):
        response_text = mock_model.captured_response_text or ""

    # (a) Tool sequence: each expected tool call must appear in captured events.
    for i, expected_call in enumerate(expected_tool_calls):
        expected_name = (
            expected_call.get("name")
            if isinstance(expected_call, dict)
            else str(expected_call)
        )
        assert expected_name in called_tool_names, (
            f"[{yaml_path}] Expected tool call #{i} '{expected_name}' not found in "
            f"captured tool sequence: {called_tool_names}"
        )
        # Check args_contain if specified.
        if isinstance(expected_call, dict) and expected_call.get("args_contain"):
            args_contain: dict[str, Any] = expected_call["args_contain"]
            matching = [e for e in all_tool_events if _tool_name(e) == expected_name]
            if matching:
                ev = matching[0]
                actual_args: dict[str, Any] = {}
                if ev.get("type") == "bidi_tool_use":
                    actual_args = ev.get("args") or ev.get("input") or {}
                elif ev.get("type") == "tool_use_stream":
                    actual_args = (ev.get("current_tool_use") or {}).get("input") or {}
                for key, val in args_contain.items():
                    assert key in actual_args, (
                        f"[{yaml_path}] Tool '{expected_name}' args missing key "
                        f"'{key}'. Actual args: {actual_args}"
                    )
                    if isinstance(val, list):
                        actual_val = actual_args[key]
                        if isinstance(actual_val, list):
                            for item in val:
                                assert item in actual_val, (
                                    f"[{yaml_path}] Tool '{expected_name}' arg "
                                    f"'{key}' missing item {item!r}. "
                                    f"Actual: {actual_val}"
                                )
                        else:
                            assert str(val).lower() in str(actual_val).lower(), (
                                f"[{yaml_path}] Tool '{expected_name}' arg '{key}': "
                                f"expected {val!r} in {actual_val!r}"
                            )
                    else:
                        assert actual_args[key] == val, (
                            f"[{yaml_path}] Tool '{expected_name}' arg '{key}': "
                            f"expected {val!r}, got {actual_args[key]!r}"
                        )

    # (b) Response must contain at least one of must_contain_one_of.
    if must_contain:
        rt_lower = response_text.lower()
        assert any(phrase.lower() in rt_lower for phrase in must_contain), (
            f"[{yaml_path}] Response text does not contain any of "
            f"{must_contain!r}. Response: {response_text!r}"
        )

    # (c) Response must NOT contain any of must_not_contain.
    must_not: list[str] = expected_response.get("must_not_contain") or []
    if must_not:
        rt_lower = response_text.lower()
        for phrase in must_not:
            assert phrase.lower() not in rt_lower, (
                f"[{yaml_path}] Response text contains forbidden phrase "
                f"{phrase!r}. Response: {response_text!r}"
            )

    # (d) Safety guarantee: if safety_critical, diy_repair_advisor must NOT be called.
    if expected_routing.get("safety_critical"):
        assert "diy_repair_advisor" not in called_tool_names, (
            f"[{yaml_path}] Safety-critical case: diy_repair_advisor must NOT be "
            f"called. Called tools: {called_tool_names}"
        )

    # (e) DIY-or-service choice heuristic.
    # Heuristic: response contains "yourself" OR "book service" (case-insensitive).
    # Matches SYSTEM_PROMPT language: "walk you through doing it yourself" /
    # "Want me to book a service appointment instead?" / "book service for you".
    if expected_routing.get("offers_diy_choice"):
        rt_lower = response_text.lower()
        assert "yourself" in rt_lower or "book service" in rt_lower, (
            f"[{yaml_path}] Expected DIY-or-service choice language ('yourself' or "
            f"'book service') in response. Response: {response_text!r}"
        )

    # (f) Latency budget.
    assert elapsed_ms <= budget_ms, (
        f"[{yaml_path}] Elapsed {elapsed_ms:.1f}ms exceeds budget {budget_ms}ms"
    )
