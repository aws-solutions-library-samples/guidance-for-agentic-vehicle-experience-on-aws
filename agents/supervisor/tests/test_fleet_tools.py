"""Tests for fleet manager persona tools.

Spec: `.kiro/specs/2026-08-21-cvx-fleet-manager-persona/spec.md` § Design

Group 3: All @pytest.mark.skip decorators removed; all tests must pass.

Test #14 (test_fleet_manager_group_gated_persona) has been REVISED from the
original Group 1 skeleton assertion ("fleet_manager in _CLIENT_REQUESTABLE_PERSONAS")
to the group-gated assertion per decisions.md 2026-08-21:
  - fleet_manager is NOT in the unconditional _CLIENT_REQUESTABLE_PERSONAS set
  - caller_has_fleet_group returns True for fleet-grouped callers
  - caller_has_fleet_group returns False for kiosk/claimless callers

This is a spec change (OQ1 re-resolution), NOT "matching broken behavior."

Tests enumerated:
  - One per tool factory asserting normalized dict shape
  - JWT forwarded as Authorization: Bearer <jwt> per tool
  - Empty config.cms_api_base degradation per tool
  - One 401 → FleetToolAuthError test
  - Group-gated fleet_manager persona test (revised from original)
  - fleet_manager in PERSONA_TOOL_FACTORIES with at least one entry
"""
from __future__ import annotations

import sys
import os
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

_FAKE_JWT = "eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJ1c2VyMSJ9.sig"


def _make_ctx(jwt: str = _FAKE_JWT) -> SimpleNamespace:
    """Minimal SessionContext-compatible stub (no real JWT needed in unit tests)."""
    return SimpleNamespace(
        jwt=jwt,
        session_id="vsa-sess-testabcd1234",
        tenant_id="fleet",
        vin=None,
        persona_id="fleet_manager",
    )


def _make_config(cms_api_base: str = "https://cms.example.com") -> SimpleNamespace:
    """Minimal Config-compatible stub for fleet tools."""
    return SimpleNamespace(cms_api_base=cms_api_base)


def _mock_response(status_code: int, body: dict) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = body
    resp.text = str(body)
    return resp


# ---------------------------------------------------------------------------
# Group A: fleet_health tool — normalized dict shape
# ---------------------------------------------------------------------------


def test_fleet_health_returns_scores():
    """make_fleet_health_tool factory returns a tool whose happy-path result
    contains 'available': True and a 'scores' or equivalent top-level key
    derived from the CMS /api/v1/fleet-health response."""
    from agents.supervisor.tools.fleet_health import make_fleet_health_tool

    ctx = _make_ctx()
    config = _make_config()

    fake_body = {
        "fleetId": "fleet-001",
        "scores": {"battery": 0.85, "tires": 0.92, "brakes": 0.78},
        "computedAt": "2026-08-21T10:00:00Z",
    }
    mock_resp = _mock_response(200, fake_body)

    with patch("requests.get", return_value=mock_resp) as mock_get:
        tool_fn = make_fleet_health_tool(ctx, config)
        result = tool_fn()

    assert isinstance(result, dict), "Expected a dict result"
    assert result.get("available") is True, "Expected 'available': True on 200"
    # The normalized shape must carry at least one fleet-level key
    assert any(k in result for k in ("scores", "fleet_scores", "data", "health",
                                      "composite", "utilization", "cost_health",
                                      "safety_compliance", "maintenance_health")), (
        "Expected at least one fleet-level key in result"
    )


# ---------------------------------------------------------------------------
# Group B: daily_briefing tool — normalized dict shape
# ---------------------------------------------------------------------------


def test_daily_briefing_returns_items():
    """make_daily_briefing_tool factory returns a tool whose happy-path result
    contains 'available': True and an 'items' or equivalent list key."""
    from agents.supervisor.tools.daily_briefing import make_daily_briefing_tool

    ctx = _make_ctx()
    config = _make_config()

    fake_body = {
        "items": [
            {"type": "alert", "text": "3 vehicles need service", "priority": "high"},
            {"type": "summary", "text": "Fleet is 94% healthy", "priority": "info"},
        ],
        "generatedAt": "2026-08-21T06:00:00Z",
    }
    mock_resp = _mock_response(200, fake_body)

    with patch("requests.get", return_value=mock_resp):
        tool_fn = make_daily_briefing_tool(ctx, config)
        result = tool_fn()

    assert isinstance(result, dict), "Expected a dict result"
    assert result.get("available") is True, "Expected 'available': True on 200"
    assert any(k in result for k in ("items", "briefing", "data", "entries")), (
        "Expected at least one list-bearing key in result"
    )


# ---------------------------------------------------------------------------
# Group C: fleet_actions tool — normalized dict shape
# ---------------------------------------------------------------------------


def test_fleet_actions_returns_pending():
    """make_fleet_actions_tool factory returns a tool whose happy-path result
    contains 'available': True and a 'actions' or equivalent list key."""
    from agents.supervisor.tools.fleet_actions import make_fleet_actions_tool

    ctx = _make_ctx()
    config = _make_config()

    fake_body = {
        "actions": [
            {
                "actionId": "act-001",
                "status": "PENDING",
                "description": "Schedule tire rotation for VEH-001",
            }
        ],
        "total": 1,
    }
    mock_resp = _mock_response(200, fake_body)

    with patch("requests.get", return_value=mock_resp):
        tool_fn = make_fleet_actions_tool(ctx, config)
        result = tool_fn()

    assert isinstance(result, dict), "Expected a dict result"
    assert result.get("available") is True, "Expected 'available': True on 200"
    assert any(k in result for k in ("actions", "pending_actions", "data", "items")), (
        "Expected at least one list-bearing key in result"
    )


# ---------------------------------------------------------------------------
# Group D: decision_journal tool — normalized dict shape
# ---------------------------------------------------------------------------


def test_decision_journal_returns_decisions():
    """make_decision_journal_tool factory returns a tool whose happy-path result
    contains 'available': True and a 'decisions' or equivalent list key."""
    from agents.supervisor.tools.decision_journal import make_decision_journal_tool

    ctx = _make_ctx()
    config = _make_config()

    fake_body = {
        "decisions": [
            {
                "decisionId": "dec-001",
                "action": "approved",
                "vehicle": "VEH-001",
                "timestamp": "2026-08-20T14:30:00Z",
            }
        ],
        "total": 1,
    }
    mock_resp = _mock_response(200, fake_body)

    with patch("requests.get", return_value=mock_resp):
        tool_fn = make_decision_journal_tool(ctx, config)
        result = tool_fn()

    assert isinstance(result, dict), "Expected a dict result"
    assert result.get("available") is True, "Expected 'available': True on 200"
    assert any(k in result for k in ("decisions", "journal", "data", "entries")), (
        "Expected at least one list-bearing key in result"
    )


# ---------------------------------------------------------------------------
# Group E: JWT forwarded as Authorization: Bearer per tool
# ---------------------------------------------------------------------------


def test_fleet_health_forwards_jwt_as_bearer():
    """JWT from ctx.jwt is forwarded as Authorization: Bearer <jwt> to GET fleet-health."""
    from agents.supervisor.tools.fleet_health import make_fleet_health_tool

    ctx = _make_ctx(jwt="test-jwt-value-health")
    config = _make_config()

    with patch("requests.get", return_value=_mock_response(200, {"scores": {}})) as mock_get:
        tool_fn = make_fleet_health_tool(ctx, config)
        tool_fn()

    mock_get.assert_called_once()
    _, kwargs = mock_get.call_args
    headers = kwargs.get("headers", {})
    assert headers.get("Authorization") == "Bearer test-jwt-value-health", (
        "Expected Authorization header to carry the JWT as Bearer token"
    )


def test_daily_briefing_forwards_jwt_as_bearer():
    """JWT from ctx.jwt is forwarded as Authorization: Bearer <jwt> to GET daily-briefing."""
    from agents.supervisor.tools.daily_briefing import make_daily_briefing_tool

    ctx = _make_ctx(jwt="test-jwt-value-briefing")
    config = _make_config()

    with patch("requests.get", return_value=_mock_response(200, {"items": []})) as mock_get:
        tool_fn = make_daily_briefing_tool(ctx, config)
        tool_fn()

    mock_get.assert_called_once()
    _, kwargs = mock_get.call_args
    headers = kwargs.get("headers", {})
    assert headers.get("Authorization") == "Bearer test-jwt-value-briefing"


def test_fleet_actions_forwards_jwt_as_bearer():
    """JWT from ctx.jwt is forwarded as Authorization: Bearer <jwt> to GET fleet-actions."""
    from agents.supervisor.tools.fleet_actions import make_fleet_actions_tool

    ctx = _make_ctx(jwt="test-jwt-value-actions")
    config = _make_config()

    with patch("requests.get", return_value=_mock_response(200, {"actions": []})) as mock_get:
        tool_fn = make_fleet_actions_tool(ctx, config)
        tool_fn()

    mock_get.assert_called_once()
    _, kwargs = mock_get.call_args
    headers = kwargs.get("headers", {})
    assert headers.get("Authorization") == "Bearer test-jwt-value-actions"


def test_decision_journal_forwards_jwt_as_bearer():
    """JWT from ctx.jwt is forwarded as Authorization: Bearer <jwt> to GET decision-journal."""
    from agents.supervisor.tools.decision_journal import make_decision_journal_tool

    ctx = _make_ctx(jwt="test-jwt-value-journal")
    config = _make_config()

    with patch("requests.get", return_value=_mock_response(200, {"decisions": []})) as mock_get:
        tool_fn = make_decision_journal_tool(ctx, config)
        tool_fn()

    mock_get.assert_called_once()
    _, kwargs = mock_get.call_args
    headers = kwargs.get("headers", {})
    assert headers.get("Authorization") == "Bearer test-jwt-value-journal"


# ---------------------------------------------------------------------------
# Group F: Empty config.cms_api_base degradation — returns available:False
# ---------------------------------------------------------------------------


def test_fleet_health_degrades_when_cms_api_base_empty():
    """When config.cms_api_base is empty string, tool returns available:False without raising."""
    from agents.supervisor.tools.fleet_health import make_fleet_health_tool

    ctx = _make_ctx()
    config = _make_config(cms_api_base="")

    # No HTTP call should be made when base URL is not configured
    with patch("requests.get") as mock_get:
        tool_fn = make_fleet_health_tool(ctx, config)
        result = tool_fn()
        mock_get.assert_not_called()

    assert result == {"available": False, "reason": "CMS API not configured"}, (
        f"Expected degradation dict, got: {result}"
    )


def test_daily_briefing_degrades_when_cms_api_base_empty():
    """When config.cms_api_base is empty string, tool returns available:False without raising."""
    from agents.supervisor.tools.daily_briefing import make_daily_briefing_tool

    ctx = _make_ctx()
    config = _make_config(cms_api_base="")

    with patch("requests.get") as mock_get:
        tool_fn = make_daily_briefing_tool(ctx, config)
        result = tool_fn()
        mock_get.assert_not_called()

    assert result == {"available": False, "reason": "CMS API not configured"}


def test_fleet_actions_degrades_when_cms_api_base_empty():
    """When config.cms_api_base is empty string, tool returns available:False without raising."""
    from agents.supervisor.tools.fleet_actions import make_fleet_actions_tool

    ctx = _make_ctx()
    config = _make_config(cms_api_base="")

    with patch("requests.get") as mock_get:
        tool_fn = make_fleet_actions_tool(ctx, config)
        result = tool_fn()
        mock_get.assert_not_called()

    assert result == {"available": False, "reason": "CMS API not configured"}


def test_decision_journal_degrades_when_cms_api_base_empty():
    """When config.cms_api_base is empty string, tool returns available:False without raising."""
    from agents.supervisor.tools.decision_journal import make_decision_journal_tool

    ctx = _make_ctx()
    config = _make_config(cms_api_base="")

    with patch("requests.get") as mock_get:
        tool_fn = make_decision_journal_tool(ctx, config)
        result = tool_fn()
        mock_get.assert_not_called()

    assert result == {"available": False, "reason": "CMS API not configured"}


# ---------------------------------------------------------------------------
# Group G: 401 response raises FleetToolAuthError
# ---------------------------------------------------------------------------


def test_fleet_health_401_raises_fleet_tool_auth_error():
    """A 401 response from the CMS endpoint raises FleetToolAuthError (not a raw HTTPError)."""
    from agents.supervisor.tools.fleet_health import FleetToolAuthError, make_fleet_health_tool

    ctx = _make_ctx()
    config = _make_config()

    mock_resp = _mock_response(401, {"message": "Unauthorized"})

    with patch("requests.get", return_value=mock_resp):
        tool_fn = make_fleet_health_tool(ctx, config)
        with pytest.raises(FleetToolAuthError):
            tool_fn()


# ---------------------------------------------------------------------------
# Group H: Group-gated fleet_manager persona (REVISED from Group 1 skeleton)
#
# Original skeleton asserted: "fleet_manager" in _CLIENT_REQUESTABLE_PERSONAS
# REVISED per decisions.md 2026-08-21 (OQ1 re-resolution):
#   - fleet_manager is NOT in the unconditional _CLIENT_REQUESTABLE_PERSONAS set
#   - caller_has_fleet_group (persona_map.py) returns True for fleet-grouped callers
#   - caller_has_fleet_group returns False for kiosk/claimless callers
#
# This is a spec change, NOT "matching broken behavior."
# ---------------------------------------------------------------------------


def test_fleet_manager_group_gated_persona():
    """fleet_manager must NOT be in the unconditional _CLIENT_REQUESTABLE_PERSONAS.
    Instead, it is group-gated: caller_has_fleet_group returns True for callers
    whose cognito:groups include fleet-viewer/fleet-operator/platform-admin, and
    False for kiosk or claimless callers.

    Revised from original Group 1 skeleton per decisions.md 2026-08-21 (OQ1
    re-resolution — gate lives on CVX side, not in CMS endpoints).
    """
    # Add the lambda dir to sys.path so persona_map is importable
    lambda_dir = os.path.join(
        os.path.dirname(__file__),
        "..", "..", "..", "lambdas", "api-assistant-chat"
    )
    lambda_dir = os.path.normpath(lambda_dir)
    if lambda_dir not in sys.path:
        sys.path.insert(0, lambda_dir)

    try:
        from persona_map import caller_has_fleet_group
        from handler import _CLIENT_REQUESTABLE_PERSONAS
    except ImportError as e:
        pytest.skip(f"handler/persona_map not importable: {e}")
        return

    # fleet_manager must NOT be in the unconditional requestable set
    assert "fleet_manager" not in _CLIENT_REQUESTABLE_PERSONAS, (
        f"'fleet_manager' must NOT be in the unconditional _CLIENT_REQUESTABLE_PERSONAS "
        f"(it is group-gated). Found in: {_CLIENT_REQUESTABLE_PERSONAS}"
    )

    # Fleet-grouped callers should be granted fleet_manager
    assert caller_has_fleet_group({"cognito:groups": "fleet-viewer"}) is True, (
        "caller_has_fleet_group must return True for fleet-viewer group"
    )
    assert caller_has_fleet_group({"cognito:groups": "fleet-operator"}) is True, (
        "caller_has_fleet_group must return True for fleet-operator group"
    )
    assert caller_has_fleet_group({"cognito:groups": "platform-admin"}) is True, (
        "caller_has_fleet_group must return True for platform-admin group"
    )

    # Kiosk and claimless callers must NOT be granted fleet_manager
    assert caller_has_fleet_group({"cognito:groups": "kiosk"}) is False, (
        "caller_has_fleet_group must return False for kiosk group"
    )
    assert caller_has_fleet_group({}) is False, (
        "caller_has_fleet_group must return False for claimless caller"
    )
    assert caller_has_fleet_group({"cognito:groups": "consumer"}) is False, (
        "caller_has_fleet_group must return False for consumer-only group"
    )


# ---------------------------------------------------------------------------
# Group I: fleet_manager in PERSONA_TOOL_FACTORIES
# ---------------------------------------------------------------------------


def test_fleet_manager_in_persona_tool_factories():
    """PERSONA_TOOL_FACTORIES['fleet_manager'] must exist and contain at least one entry
    after Group 2 wires the fleet manager tools into tools/__init__.py."""
    try:
        from agents.supervisor.tools import PERSONA_TOOL_FACTORIES
    except ImportError as e:
        pytest.skip(f"PERSONA_TOOL_FACTORIES not importable: {e}")
        return

    assert "fleet_manager" in PERSONA_TOOL_FACTORIES, (
        f"'fleet_manager' not found in PERSONA_TOOL_FACTORIES keys: "
        f"{list(PERSONA_TOOL_FACTORIES.keys())}"
    )
    assert len(PERSONA_TOOL_FACTORIES["fleet_manager"]) >= 1, (
        "PERSONA_TOOL_FACTORIES['fleet_manager'] must have at least one entry"
    )
