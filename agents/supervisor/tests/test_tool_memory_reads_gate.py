"""Tests for the ``tool_memory_reads`` gate in the 8 AgentCore Memory tool files.

Task T2.5 — spec `.kiro/specs/2026-09-22-cvx-text-session-memory/`.

When ``config.tool_memory_reads`` is False, each tool MUST:
  - Return its existing degraded payload (same dict the ``except`` branch returns).
  - NOT call ``boto3.client`` at all — proven by counting client constructions and
    asserting the count is zero when the flag is off.

What actually makes these tests non-vacuous (measured, not assumed):
  The load-bearing element is the **assertion**, not the patch target. A
  **return-value-only** test is vacuous here, because each tool's pre-existing broad
  ``except`` returns the *identical* degraded dict whether or not the gate exists —
  so an ungated tool is indistinguishable from a gated one by return value alone.
  Asserting on **whether the client was constructed** is what detects a missing gate.

  Measured against the real tool (gate stripped vs present, both patch targets):

      patch target   assertion      gate absent  ->  result
      global         return_only                     PASS   <-- VACUOUS
      global         call_counter                    FAIL   (detects it)
      per-module     return_only                     PASS   <-- VACUOUS
      per-module     call_counter                    FAIL   (detects it)

  So the call-counter detects the missing gate with *either* patch target, and
  return-value-only is vacuous with *either*. An earlier revision of this docstring —
  and of the spec's T2.5 note and commit fa1422d — claimed the per-module patch target
  was what conferred non-vacuity. That was wrong, and wrong in the direction that
  matters: a future author would have taken away "pick the right patch target" instead
  of "assert whether the client was constructed."

  The per-module target (``agents.supervisor.tools.<name>.boto3``) is still the better
  choice and is kept: each tool does ``import boto3`` at module top, so replacing the
  name in the module's own namespace is the more direct intercept and does not depend
  on attribute lookup order. But it is a robustness preference, not the reason the
  tests work.

Mutation test (mandatory, per task entry):
  - Gate removed, flag OFF, boto3 patched to raise: the affected tool's case FAILS
    with "boto3.client WAS called" → the assertion tracks the gate.
  - Gate present, flag ON, boto3 patched to raise: that same case fails → confirms
    the assertion measures the gate rather than the ``except`` swallow.
  - Architect re-verified per-tool: stripping the gate from ``vehicle_identity`` alone
    failed exactly ``test_gate_off_returns_degraded_payload_without_boto3[vehicle_identity]``
    and nothing else, so coverage is per-tool rather than incidental.

All outcomes were observed, not asserted.
"""

from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any, Iterator
from unittest.mock import MagicMock, patch

import pytest

from agents.supervisor.tools.charging_history import make_charging_history_tool
from agents.supervisor.tools.customer_profile import make_customer_profile_tool
from agents.supervisor.tools.energy_summary import make_energy_summary_tool
from agents.supervisor.tools.interaction_history import make_interaction_history_tool
from agents.supervisor.tools.ota_history import make_ota_history_tool
from agents.supervisor.tools.service_history import make_service_history_tool
from agents.supervisor.tools.telemetry_summary import make_telemetry_summary_tool
from agents.supervisor.tools.vehicle_identity import make_vehicle_identity_tool

# ---------------------------------------------------------------------------
# Expected degraded payloads — byte-identical to each tool's ``except`` branch.
# ---------------------------------------------------------------------------

_PYSPARK_GATING_RESPONSE: dict[str, Any] = {
    "available": False,
    "reason": "Pending PySpark via Glue spec ship",
    "tracking_row": "PySpark via Glue",
}

_DEGRADED: dict[str, dict[str, Any]] = {
    "energy_summary":      _PYSPARK_GATING_RESPONSE,
    "service_history":     {"available": False, "reason": "service_records not yet loaded"},
    "interaction_history": {"available": False, "reason": "customer_interactions not yet loaded"},
    "telemetry_summary":   _PYSPARK_GATING_RESPONSE,
    "vehicle_identity":    {"available": False, "reason": "vehicle_identity not yet loaded"},
    "charging_history":    {"available": False, "reason": "charging_sessions not yet loaded"},
    "customer_profile":    {"available": False, "reason": "customer_360 not yet loaded"},
    "ota_history":         {"available": False, "reason": "ota_campaigns not yet loaded"},
}

# Map each tool name to its module path (for per-module boto3 patching).
_MODULE_PATH: dict[str, str] = {
    "energy_summary":      "agents.supervisor.tools.energy_summary.boto3",
    "service_history":     "agents.supervisor.tools.service_history.boto3",
    "interaction_history": "agents.supervisor.tools.interaction_history.boto3",
    "telemetry_summary":   "agents.supervisor.tools.telemetry_summary.boto3",
    "vehicle_identity":    "agents.supervisor.tools.vehicle_identity.boto3",
    "charging_history":    "agents.supervisor.tools.charging_history.boto3",
    "customer_profile":    "agents.supervisor.tools.customer_profile.boto3",
    "ota_history":         "agents.supervisor.tools.ota_history.boto3",
}


# ---------------------------------------------------------------------------
# Session / config stubs
# ---------------------------------------------------------------------------

def _session(*, vin: str = "VIN123", tenant_id: str = "t1",
             persona_id: str = "p1", customer_id: str = "C001") -> Any:
    return SimpleNamespace(
        vin=vin,
        tenant_id=tenant_id,
        persona_id=persona_id,
        customer_id=customer_id,
    )


def _config(tool_memory_reads: bool, memory_id: str = "mem-id") -> Any:
    return SimpleNamespace(
        memory_id=memory_id,
        memory_region="us-west-2",
        tool_memory_reads=tool_memory_reads,
    )


# ---------------------------------------------------------------------------
# Factory + invocation helpers
# ---------------------------------------------------------------------------

def _make_tool(name: str, session: Any, config: Any) -> Any:
    makers = {
        "energy_summary":      make_energy_summary_tool,
        "service_history":     make_service_history_tool,
        "interaction_history": make_interaction_history_tool,
        "telemetry_summary":   make_telemetry_summary_tool,
        "vehicle_identity":    make_vehicle_identity_tool,
        "charging_history":    make_charging_history_tool,
        "customer_profile":    make_customer_profile_tool,
        "ota_history":         make_ota_history_tool,
    }
    return makers[name](session, config)


def _call_tool(name: str, tool_fn: Any) -> Any:
    """Invoke the tool function with the correct signature for each tool."""
    raw = tool_fn.func if hasattr(tool_fn, "func") else tool_fn
    if name == "vehicle_identity":
        return raw(vin="VIN123")
    return raw()


@contextmanager
def _raising_boto3(tool_name: str) -> Iterator[list]:
    """Context manager: patches *the tool module's own boto3 reference* to raise.

    Returns a list that accumulates True for each boto3.client call attempted.

    The returned counter is the point: callers MUST assert on it. Asserting only on
    the tool's return value is vacuous, because the pre-existing broad ``except``
    returns the same degraded dict whether or not the gate exists. See the module
    docstring for the measured 4-way table. The per-module patch target is the more
    direct intercept, but it is not what makes these tests detect a missing gate.
    """
    client_called: list[bool] = []

    def _raiser(*args: Any, **kwargs: Any) -> Any:
        client_called.append(True)
        raise RuntimeError("boto3.client must not be called when gate is active")

    mock_boto3 = MagicMock()
    mock_boto3.client.side_effect = _raiser

    with patch(_MODULE_PATH[tool_name], mock_boto3):
        yield client_called


# ---------------------------------------------------------------------------
# Parametrized tests
# ---------------------------------------------------------------------------

TOOL_NAMES = list(_DEGRADED.keys())


@pytest.mark.parametrize("tool_name", TOOL_NAMES)
def test_gate_off_returns_degraded_payload_without_boto3(tool_name: str) -> None:
    """With tool_memory_reads=False, the tool returns the degraded dict.

    boto3 (in the tool's own module namespace) is patched so every client
    construction is counted. The assertion below is on that COUNT, not on the return
    value — the flag-off path must genuinely bypass the client, and a return-value
    check cannot tell bypassed-by-the-gate from reached-and-swallowed-by-except.

    Mutation outcome (observed):
      - Gate REMOVED from service_history.py: this test FAILED with
        "boto3.client WAS called with tool_memory_reads=False; the gate is not
        working" — the count assertion caught it, exactly as intended.
      - Architect re-verified per-tool: stripping the gate from vehicle_identity
        alone failed only this test's [vehicle_identity] case.
    """
    session = _session()
    config = _config(tool_memory_reads=False)
    tool_fn = _make_tool(tool_name, session, config)

    with _raising_boto3(tool_name) as client_called:
        result = _call_tool(tool_name, tool_fn)

    assert not client_called, (
        f"{tool_name}: boto3.client WAS called with tool_memory_reads=False; "
        "the gate is not working"
    )
    assert result == _DEGRADED[tool_name], (
        f"{tool_name}: expected {_DEGRADED[tool_name]!r}, got {result!r}"
    )


@pytest.mark.parametrize("tool_name", TOOL_NAMES)
def test_gate_on_calls_boto3(tool_name: str) -> None:
    """With tool_memory_reads=True, the tool reaches boto3.client.

    boto3 (in the tool's own module namespace) is patched to raise; the tool's
    broad ``except`` catches it and returns the degraded payload.  The key assertion
    is that boto3.client IS called, which means this test would FAIL if the gate
    were accidentally active.

    This is the second direction of the mutation: confirming that the flag-on path
    reaches the network layer.

    Mutation outcome recorded in handback:
      - With flag=True: client_called=True — boto3.client was reached.
        Same test for flag=False: client_called=False — gate is the measured property.
    """
    session = _session()
    config = _config(tool_memory_reads=True)
    tool_fn = _make_tool(tool_name, session, config)

    with _raising_boto3(tool_name) as client_called:
        result = _call_tool(tool_name, tool_fn)

    assert client_called, (
        f"{tool_name}: boto3.client was NOT called with tool_memory_reads=True; "
        "the gate appears active when it should not be"
    )
    # The except branch catches the error and returns the degraded payload.
    assert result == _DEGRADED[tool_name], (
        f"{tool_name}: expected degraded payload from except branch, got {result!r}"
    )


@pytest.mark.parametrize("tool_name", TOOL_NAMES)
def test_gate_defaults_to_enabled_when_field_absent(tool_name: str) -> None:
    """getattr default=True: a config without tool_memory_reads calls boto3.

    This guards the spec constraint: missing field → reads enabled, never disabled.
    A PersonaLoaderConfig on the voice path does not carry tool_memory_reads.
    """
    session = _session()
    # Config with NO tool_memory_reads attribute at all.
    config = SimpleNamespace(memory_id="mem-id", memory_region="us-west-2")
    tool_fn = _make_tool(tool_name, session, config)

    with _raising_boto3(tool_name) as client_called:
        _call_tool(tool_name, tool_fn)

    assert client_called, (
        f"{tool_name}: boto3.client was NOT called with no tool_memory_reads attribute; "
        "default must be True (reads enabled)"
    )
