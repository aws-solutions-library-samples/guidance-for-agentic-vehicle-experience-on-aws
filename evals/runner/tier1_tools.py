"""Tier 1 runner — tool unit eval engine.

Pytest-parameterized over ``evals/cases/tools/**/*.yaml``.

## YAML convention for Tier 1 cases

Each YAML file must be a valid ``EvalCase`` (tier=1) plus a top-level
``tool`` field naming the tool to invoke (e.g. ``tool: triage``).

    tool: triage          # <-- top-level; NOT part of EvalCase schema
    id: triage-happy-001
    description: "..."
    tier: 1
    persona: fleet-driver
    vehicle_id: VEH-0025
    driver_id: drv-samantha-carter
    input:
      type: text
      utterance: "Check my tire pressure"
    expected:
      tool_output:
        classification: P2
    latency_budget_ms: 1000

The ``tool`` field is ignored by Pydantic (EvalCase.model_config is empty,
so extra fields are silently discarded). We read it directly from the raw
YAML dict before validation.

## Tool invocation convention

The tool function is resolved via the ``tool_factory_kit`` fixture from
``evals/conftest.py`` (primary path). Falls back to inspect-based factory
invocation when the fixture is unavailable.

If ``case.input.input_args`` is set, those kwargs are passed directly to
the tool. Otherwise, ``case.input.utterance`` is passed as the first
parameter.

## Error case convention

If ``expected.tool_output`` contains an ``error`` key, the runner expects
the tool to raise an exception whose class name matches ``error``. The
optional ``message_contains`` key asserts a substring of the exception
message.
"""

from __future__ import annotations

import glob
import importlib
import inspect
import time
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
import yaml

from evals.runner.mocks import MemoryStub, seed_persona_memory
from evals.runner.schema import EvalCase

# ---------------------------------------------------------------------------
# Default Memory stub fixture data (enables existing 3-persona Tier 1 cases)
# ---------------------------------------------------------------------------

_FIXTURE_VIN = "1FTRX18W6XKB12345"
_FIXTURE_CUST = "drv-samantha-carter"
_FIXTURE_MEM_ID = "eval-stub-mem-001"

_DEFAULT_FIXTURES: dict[str, Any] = {
    "vehicle_identity": {
        "vin": _FIXTURE_VIN,
        "make": "Ford",
        "model": "F-150",
        "year": 2022,
        "trim": "XLT",
        "engine": "3.5L EcoBoost V6",
        "transmission": "10-speed automatic",
        "fuelType": "gasoline",
        "maintenanceGroup": "ford-f150-2022",
        "recallEligibility": True,
        "found": True,
    },
    "service_records": [
        {"service_date": "2024-01-15", "mileage": 32000, "work": "Oil change"},
        {"service_date": "2023-07-01", "mileage": 28000, "work": "Brake inspection"},
    ],
    "charging_sessions": [
        {"session_date": "2024-06-01", "kwh": 45.2, "location": "Home"},
    ],
    "customer_360": {
        "customer_id": _FIXTURE_CUST,
        "name": "Samantha Carter",
        "loyalty_tier": "gold",
        "vehicles_owned": 1,
    },
    "customer_interactions": [
        {"interaction_date": "2024-05-10", "channel": "phone", "resolution": "resolved"},
    ],
    "ota_campaigns": [
        {"campaign_name": "FW-2024-001", "dispatch_date": "2024-03-15", "status": "installed"},
    ],
}


def _make_memory_stub_with_defaults(
    persona_id: str = "fleet_driver",
    vehicle_id: str = _FIXTURE_VIN,
    customer_id: str = _FIXTURE_CUST,
    tenant_id: str = "meridian-fleet",
) -> MemoryStub:
    """Return a MemoryStub pre-seeded with default fixture data."""
    stub = MemoryStub()
    seed_persona_memory(
        stub=stub,
        memory_id=_FIXTURE_MEM_ID,
        persona_id=persona_id,
        vehicle_id=vehicle_id,
        customer_id=customer_id,
        fixtures=_DEFAULT_FIXTURES,
        tenant_id=tenant_id,
    )
    return stub


def _make_tier1_config(
    memory_stub: MemoryStub | None = None,
    vin: str = _FIXTURE_VIN,
) -> Any:
    """Return a minimal config-like object for Memory-backed tool factories."""
    from types import SimpleNamespace
    return SimpleNamespace(
        memory_id=_FIXTURE_MEM_ID,
        memory_region="us-west-2",
        adp_kb_id="eval-stub-kb",
        adp_region="us-east-1",
        kb_fixtures_dir=Path("corpora"),
    )


def _make_tier1_session(
    persona_id: str = "fleet_driver",
    vin: str = _FIXTURE_VIN,
    customer_id: str = _FIXTURE_CUST,
    tenant_id: str = "meridian-fleet",
) -> Any:
    """Return a minimal session-like object for Memory-backed tool factories."""
    from types import SimpleNamespace
    return SimpleNamespace(
        persona_id=persona_id,
        vin=vin,
        customer_id=customer_id,
        tenant_id=tenant_id,
    )

# ---------------------------------------------------------------------------
# Tool name aliases: YAML ``tool:`` field → tool_factory_kit key
# ---------------------------------------------------------------------------

_TOOL_KIT_ALIASES: dict[str, str] = {
    "service_network": "find_service_center",
}

_DEFAULT_LATENCY_BUDGET_MS = 1000


def _is_subset(expected: object, actual: object) -> bool:
    """Recursive subset equality: every key/value in expected must appear in actual.

    For dicts: every key in expected must exist in actual with a matching value
    (recursively). Extra keys in actual are ignored.
    For lists: expected and actual must be equal (no subset semantics for lists).
    For scalars: must be equal.
    """
    if isinstance(expected, dict) and isinstance(actual, dict):
        return all(
            k in actual and _is_subset(v, actual[k])
            for k, v in expected.items()
        )
    return expected == actual


def _load_cases() -> list[tuple[str, dict[str, Any]]]:
    """Return sorted (yaml_path, raw_dict) pairs from evals/cases/tools/."""
    paths = sorted(glob.glob("evals/cases/tools/**/*.yaml", recursive=True))
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


# ---------------------------------------------------------------------------
# Parameterized test
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("yaml_path,raw", _CASES, ids=_IDS)
def test_tool_unit(
    yaml_path: str,
    raw: dict[str, Any],
    request: pytest.FixtureRequest,
) -> None:
    """Tier 1 tool unit eval.

    For each YAML case:
    1. Validates against EvalCase schema.
    2. Skips non-tier-1 cases.
    3. Resolves the tool function via tool_factory_kit (primary) or factory
       inspect fallback.
    4. Invokes the tool with input_args or utterance, times it.
    5. For error cases (expected.tool_output has 'error' key): asserts the
       tool raises the expected exception.
    6. For normal cases: asserts output subset matches expected.tool_output.
    7. Asserts elapsed ms <= latency_budget_ms.
    """
    # --- 0. Skip if skip_reason is set ---
    skip_reason: str | None = raw.get("skip_reason")
    if skip_reason:
        pytest.skip(f"[{yaml_path}] {skip_reason}")

    # --- 1. Validate schema ---
    try:
        case = EvalCase.model_validate(raw)
    except Exception as exc:
        pytest.fail(f"[{yaml_path}] EvalCase validation failed: {exc}")

    # --- 2. Skip non-tier-1 ---
    if case.tier != 1:
        pytest.skip(f"tier={case.tier}; Tier 1 runner only handles tier=1 cases")

    # --- 3. Resolve tool name ---
    tool_name: str | None = raw.get("tool")
    if not tool_name:
        pytest.fail(
            f"[{yaml_path}] Cannot resolve tool name: add a top-level 'tool:' field"
        )

    # Map YAML tool name to kit key (e.g. service_network → find_service_center)
    kit_key = _TOOL_KIT_ALIASES.get(tool_name, tool_name)

    # --- 4. Resolve tool function ---
    tool_fn = None

    # Primary: use tool_factory_kit from conftest.py
    try:
        kit = request.getfixturevalue("tool_factory_kit")
        tool_fn = kit[kit_key]
    except (pytest.FixtureLookupError, KeyError):
        # Fallback: signature-based factory invocation with MemoryStub injection
        tools_mod = importlib.import_module("agents.supervisor.tools")
        factory_name = f"make_{tool_name}_tool"
        factory = getattr(tools_mod, factory_name, None)
        if factory is None:
            pytest.fail(
                f"[{yaml_path}] Tool factory '{factory_name}' not found in "
                "agents.supervisor.tools"
            )

        # Build Memory stub seeded with default fixture data.
        _memory_stub = _make_memory_stub_with_defaults(
            persona_id="fleet_driver",
            vehicle_id=_FIXTURE_VIN,
            customer_id=_FIXTURE_CUST,
            tenant_id="meridian-fleet",
        )
        _stub_client = _memory_stub.make_client()

        ctx = _make_tier1_session(
            persona_id="fleet_driver",
            vin=_FIXTURE_VIN,
            customer_id=_FIXTURE_CUST,
            tenant_id="meridian-fleet",
        )
        config = _make_tier1_config(memory_stub=_memory_stub)

        factory_sig = inspect.signature(factory)
        factory_params = list(factory_sig.parameters.keys())

        # Memory-backed tools need boto3.client patched with the stub.
        _memory_tools = {
            "vehicle_identity", "service_history", "charging_history",
            "customer_profile", "interaction_history", "ota_history",
            "telemetry_summary", "energy_summary",
        }
        _boto3_target = (
            f"agents.supervisor.tools.{tool_name}.boto3.client"
            if tool_name in _memory_tools
            else None
        )

        def _build():
            if len(factory_params) == 0:
                return factory()
            elif len(factory_params) == 1:
                return factory(ctx)
            else:
                return factory(ctx, config)

        if _boto3_target:
            with patch(_boto3_target, return_value=_stub_client):
                tool_fn = _build()
        else:
            tool_fn = _build()

    # --- 5. Build invocation kwargs ---
    if case.input.input_args is not None:
        invoke_kwargs = case.input.input_args
    else:
        sig = inspect.signature(tool_fn)
        params = [
            name for name, p in sig.parameters.items()
            if p.kind not in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD)
        ]
        if not params:
            pytest.fail(f"[{yaml_path}] Tool {tool_name!r} has no parameters")
        invoke_kwargs = {params[0]: case.input.utterance or ""}

    budget_ms = case.latency_budget_ms if case.latency_budget_ms is not None else _DEFAULT_LATENCY_BUDGET_MS
    expected_output: dict[str, Any] = case.expected.tool_output or {}

    # --- 6. Error case: expect an exception ---
    if "error" in expected_output:
        start = time.perf_counter()
        try:
            tool_fn(**invoke_kwargs)
            elapsed_ms = (time.perf_counter() - start) * 1000
            pytest.fail(
                f"[{yaml_path}] Expected {expected_output['error']} to be raised, "
                "but tool returned without error"
            )
        except Exception as exc:
            elapsed_ms = (time.perf_counter() - start) * 1000
            # Allow pytest.fail to propagate
            if isinstance(exc, pytest.fail.Exception):
                raise
            exc_type = type(exc).__name__
            expected_exc = expected_output["error"]
            assert exc_type == expected_exc, (
                f"[{yaml_path}] Expected exception {expected_exc!r}, got {exc_type!r}: {exc}"
            )
            if "message_contains" in expected_output:
                assert expected_output["message_contains"] in str(exc), (
                    f"[{yaml_path}] Exception message {str(exc)!r} does not contain "
                    f"{expected_output['message_contains']!r}"
                )
        assert elapsed_ms <= budget_ms, (
            f"[{yaml_path}] Tool {tool_name!r} took {elapsed_ms:.1f}ms, budget is {budget_ms}ms"
        )
        return

    # --- 7. Normal case: invoke and assert output subset ---
    start = time.perf_counter()
    try:
        actual_output = tool_fn(**invoke_kwargs)
    except Exception as exc:
        pytest.fail(f"[{yaml_path}] Tool {tool_name!r} raised: {exc}")
    elapsed_ms = (time.perf_counter() - start) * 1000

    if not isinstance(actual_output, dict):
        if hasattr(actual_output, "model_dump"):
            actual_output = actual_output.model_dump()
        else:
            actual_output = {"result": actual_output}

    for key, expected_val in expected_output.items():
        assert key in actual_output, (
            f"[{yaml_path}] Expected key {key!r} missing from tool output. "
            f"Actual keys: {list(actual_output.keys())}"
        )
        assert _is_subset(expected_val, actual_output[key]), (
            f"[{yaml_path}] Key {key!r}: expected subset {expected_val!r}, "
            f"got {actual_output[key]!r}"
        )

    # --- 8. Assert latency ---
    assert elapsed_ms <= budget_ms, (
        f"[{yaml_path}] Tool {tool_name!r} took {elapsed_ms:.1f}ms, budget is {budget_ms}ms"
    )
