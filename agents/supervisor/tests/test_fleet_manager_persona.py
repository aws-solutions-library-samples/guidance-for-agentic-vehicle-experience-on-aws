"""Structural unit test — fleet_manager persona wiring.

Pure structural checks (no network, no Claude, no AWS). Lives in its own file
rather than test_scenarios.py because that module is entirely E2E-gated
(module-level ``pytestmark = skipif(not VSA_TEST_E2E)``); a lightweight
always-run test does not belong under that guard. See decisions.md 2026-08-21.
"""
from __future__ import annotations

import os
import sys


def test_fleet_manager_persona_requestable() -> None:
    """Verify fleet_manager persona is wired correctly across all registry dicts."""
    # Make the lambda root importable so we can pull handler + persona_map.
    _lambda_root = os.path.join(
        os.path.dirname(__file__), "..", "..", "..", "lambdas", "api-assistant-chat"
    )
    if _lambda_root not in sys.path:
        sys.path.insert(0, _lambda_root)

    import handler as h
    from persona_map import caller_has_fleet_group

    from agents.supervisor.tools import PERSONA_TOOL_FACTORIES
    from agents.supervisor.persona_definitions import (
        PERSONA_PROMPTS,
        PERSONA_PRODUCT_BINDINGS,
    )

    # 1. fleet_manager must NOT be in the unconditional client-requestable set.
    #    It is group-gated via caller_has_fleet_group instead.
    assert "fleet_manager" not in h._CLIENT_REQUESTABLE_PERSONAS, (
        "fleet_manager must not appear in _CLIENT_REQUESTABLE_PERSONAS — "
        "it is group-gated (fleet-viewer / fleet-operator / platform-admin), "
        "not unconditionally client-requestable."
    )
    # caller_has_fleet_group must return True for a fleet-grouped caller …
    assert caller_has_fleet_group({"cognito:groups": "fleet-viewer"}) is True
    assert caller_has_fleet_group({"cognito:groups": "fleet-operator"}) is True
    assert caller_has_fleet_group({"cognito:groups": "platform-admin"}) is True
    # … and False for a kiosk or claimless caller.
    assert caller_has_fleet_group({"cognito:groups": "kiosk"}) is False
    assert caller_has_fleet_group({}) is False

    # 2. fleet_manager must be registered in PERSONA_TOOL_FACTORIES.
    assert "fleet_manager" in PERSONA_TOOL_FACTORIES, (
        "fleet_manager is missing from PERSONA_TOOL_FACTORIES"
    )

    # 3. The factory list must include the four fleet-specific tools.
    factory_names = {f.__name__ for f in PERSONA_TOOL_FACTORIES["fleet_manager"]}
    for expected in (
        "make_fleet_health_tool",
        "make_daily_briefing_tool",
        "make_fleet_actions_tool",
        "make_decision_journal_tool",
    ):
        assert expected in factory_names, (
            f"PERSONA_TOOL_FACTORIES['fleet_manager'] is missing {expected!r}. "
            f"Present: {sorted(factory_names)}"
        )

    # 4. fleet_manager must have an entry in PERSONA_PROMPTS.
    assert "fleet_manager" in PERSONA_PROMPTS, (
        "fleet_manager is missing from PERSONA_PROMPTS"
    )

    # 5. fleet_manager has no per-VIN ADP products at v1 — binding must be [].
    assert PERSONA_PRODUCT_BINDINGS.get("fleet_manager") == [], (
        f"Expected PERSONA_PRODUCT_BINDINGS['fleet_manager'] == [] "
        f"but got {PERSONA_PRODUCT_BINDINGS.get('fleet_manager')!r}"
    )
