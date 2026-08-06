"""Unit tests for agents/supervisor/session.py — session_manager_or_none().

Spec: connected-mobility-guidance-on-aws/.kiro/specs/
      2026-08-04-cvx-oem-discover-order-meridian/ § Risk R2 (merged from predecessor 2026-07-28)

Covers:
  - Signed-in branch: returns AgentCoreMemorySessionManager when jwt is set
    and memory_id is configured.
  - Anonymous branch: returns None when jwt is absent/empty/whitespace.
  - Degraded branch: returns None when memory_id is absent/empty.
  - actor_id resolution: customer_id preferred, session_id fallback.

No AWS credentials or live infrastructure required — AgentCoreMemorySessionManager
constructor is patched at its source module path.

Run: pytest agents/supervisor/tests/test_session.py -v
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

# Source-module paths for lazy imports inside session_manager_or_none.
_MANAGER_PATH = "bedrock_agentcore.memory.integrations.strands.session_manager.AgentCoreMemorySessionManager"
_CONFIG_PATH = "bedrock_agentcore.memory.integrations.strands.config.AgentCoreMemoryConfig"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_ctx(
    jwt: str = "test-cognito-id-token",
    session_id: str = "vsa-sess-testabc123def",
    customer_id: str | None = None,
    tenant_id: str = "genericmoto-demo",
) -> SimpleNamespace:
    return SimpleNamespace(
        jwt=jwt,
        session_id=session_id,
        customer_id=customer_id,
        tenant_id=tenant_id,
        persona_id="consumer",
    )


def _make_config(
    memory_id: str = "mem-test-001-xyz",  # ≥12 chars to pass botocore validation if real
    memory_region: str = "us-west-2",
) -> SimpleNamespace:
    return SimpleNamespace(
        memory_id=memory_id,
        memory_region=memory_region,
    )


def _call_with_mocks(ctx, config):
    """Call session_manager_or_none with both SDK classes patched at source."""
    mock_manager_instance = MagicMock()
    mock_manager_cls = MagicMock(return_value=mock_manager_instance)
    mock_config_cls = MagicMock()

    with patch(_MANAGER_PATH, mock_manager_cls), patch(_CONFIG_PATH, mock_config_cls):
        from agents.supervisor.session import session_manager_or_none
        result = session_manager_or_none(ctx, config)

    return result, mock_config_cls, mock_manager_cls, mock_manager_instance


# ---------------------------------------------------------------------------
# Tests — signed-in branch
# ---------------------------------------------------------------------------


class TestSessionManagerOrNoneSignedIn:
    """session_manager_or_none returns a manager when the user is signed in."""

    def test_returns_manager_when_jwt_and_memory_id_set(self):
        """Signed-in user with memory_id → returns AgentCoreMemorySessionManager."""
        ctx = _make_ctx(jwt="valid-cognito-token", customer_id="CUST-001")
        config = _make_config(memory_id="mem-abc123-000000")

        result, _, mock_manager_cls, mock_manager_instance = _call_with_mocks(ctx, config)

        assert result is mock_manager_instance, (
            "Expected AgentCoreMemorySessionManager instance for signed-in session"
        )
        mock_manager_cls.assert_called_once()

    def test_uses_customer_id_as_actor_when_present(self):
        """customer_id is preferred over session_id as actor_id."""
        ctx = _make_ctx(jwt="valid-token", customer_id="CUST-789", session_id="sess-xyz-0000")
        config = _make_config(memory_id="mem-test-001-xyz")

        _, mock_config_cls, _, _ = _call_with_mocks(ctx, config)

        call_kwargs = mock_config_cls.call_args
        actor_passed = call_kwargs.kwargs.get("actor_id")
        assert actor_passed == "CUST-789", (
            f"customer_id should be used as actor_id, got {actor_passed!r}"
        )

    def test_falls_back_to_session_id_when_no_customer_id(self):
        """When customer_id is None, session_id is used as actor_id."""
        ctx = _make_ctx(jwt="valid-token", customer_id=None, session_id="sess-fallback-001-abc")
        config = _make_config(memory_id="mem-test-001-xyz")

        _, mock_config_cls, _, _ = _call_with_mocks(ctx, config)

        call_kwargs = mock_config_cls.call_args
        actor_passed = call_kwargs.kwargs.get("actor_id")
        assert actor_passed == "sess-fallback-001-abc", (
            f"session_id fallback expected as actor_id, got {actor_passed!r}"
        )

    def test_memory_region_passed_to_manager(self):
        """memory_region from config is forwarded to AgentCoreMemorySessionManager."""
        ctx = _make_ctx(jwt="valid-token")
        config = _make_config(memory_id="mem-test-001-xyz", memory_region="us-east-1")

        _, _, mock_manager_cls, _ = _call_with_mocks(ctx, config)

        call_kwargs = mock_manager_cls.call_args
        region_passed = call_kwargs.kwargs.get("region_name")
        assert region_passed == "us-east-1", (
            f"memory_region 'us-east-1' should be forwarded, got {region_passed!r}"
        )

    def test_session_id_passed_to_memory_config(self):
        """ctx.session_id is forwarded to AgentCoreMemoryConfig as session_id."""
        ctx = _make_ctx(jwt="valid-token", session_id="sess-specific-abc123", customer_id="CUST-X")
        config = _make_config(memory_id="mem-test-001-xyz")

        _, mock_config_cls, _, _ = _call_with_mocks(ctx, config)

        call_kwargs = mock_config_cls.call_args
        sid_passed = call_kwargs.kwargs.get("session_id")
        assert sid_passed == "sess-specific-abc123", (
            f"ctx.session_id should be forwarded to config, got {sid_passed!r}"
        )

    def test_memory_id_passed_to_memory_config(self):
        """config.memory_id is forwarded to AgentCoreMemoryConfig as memory_id."""
        ctx = _make_ctx(jwt="valid-token", customer_id="CUST-Y")
        config = _make_config(memory_id="mem-custom-id-12345")

        _, mock_config_cls, _, _ = _call_with_mocks(ctx, config)

        call_kwargs = mock_config_cls.call_args
        mid_passed = call_kwargs.kwargs.get("memory_id")
        assert mid_passed == "mem-custom-id-12345", (
            f"config.memory_id should be forwarded, got {mid_passed!r}"
        )


# ---------------------------------------------------------------------------
# Tests — anonymous branch (spec Risk R2)
# ---------------------------------------------------------------------------


class TestSessionManagerOrNoneAnonymous:
    """session_manager_or_none returns None for anonymous sessions (spec Risk R2)."""

    def test_returns_none_when_jwt_is_empty_string(self):
        """Empty jwt → anonymous session → None, no LTM contact."""
        ctx = _make_ctx(jwt="")
        config = _make_config(memory_id="mem-test-001-xyz")

        result, _, mock_manager_cls, _ = _call_with_mocks(ctx, config)

        assert result is None, "Anonymous session (empty jwt) must return None"
        mock_manager_cls.assert_not_called()

    def test_returns_none_when_jwt_is_whitespace(self):
        """Whitespace-only jwt is treated as absent → anonymous → None."""
        ctx = _make_ctx(jwt="   ")
        config = _make_config(memory_id="mem-test-001-xyz")

        result, _, mock_manager_cls, _ = _call_with_mocks(ctx, config)

        assert result is None, "Whitespace jwt must be treated as anonymous"
        mock_manager_cls.assert_not_called()

    def test_returns_none_when_jwt_attribute_missing(self):
        """Context object with no jwt attribute is treated as anonymous."""
        ctx = SimpleNamespace(
            session_id="sess-anon-001",
            customer_id=None,
            tenant_id="genericmoto-demo",
            # jwt intentionally absent
        )
        config = _make_config(memory_id="mem-test-001-xyz")

        result, _, mock_manager_cls, _ = _call_with_mocks(ctx, config)

        assert result is None, "Missing jwt attribute must be treated as anonymous"
        mock_manager_cls.assert_not_called()

    def test_sdk_never_called_for_anonymous_sessions(self):
        """AgentCore Memory SDK classes are NOT instantiated for anonymous sessions (R2 guard)."""
        ctx = _make_ctx(jwt="")
        config = _make_config(memory_id="mem-test-001-xyz")

        _, mock_config_cls, mock_manager_cls, _ = _call_with_mocks(ctx, config)

        assert mock_config_cls.call_count == 0, "AgentCoreMemoryConfig must not be instantiated for anon sessions"
        assert mock_manager_cls.call_count == 0, "AgentCoreMemorySessionManager must not be instantiated for anon sessions"


# ---------------------------------------------------------------------------
# Tests — degraded / misconfigured branch
# ---------------------------------------------------------------------------


class TestSessionManagerOrNoneDegraded:
    """session_manager_or_none degrades gracefully when memory is not configured."""

    def test_returns_none_when_memory_id_empty(self):
        """Signed-in but memory_id not configured → None (safe degradation)."""
        ctx = _make_ctx(jwt="valid-token", customer_id="CUST-001")
        config = _make_config(memory_id="")

        result, _, mock_manager_cls, _ = _call_with_mocks(ctx, config)

        assert result is None, "Empty memory_id should cause safe degradation to None"
        mock_manager_cls.assert_not_called()

    def test_returns_none_when_memory_id_absent(self):
        """Config with no memory_id attribute at all → None."""
        ctx = _make_ctx(jwt="valid-token")
        config = SimpleNamespace()  # no memory_id attribute

        result, _, mock_manager_cls, _ = _call_with_mocks(ctx, config)

        assert result is None
        mock_manager_cls.assert_not_called()

    def test_default_memory_region_when_absent(self):
        """Config without memory_region uses 'us-west-2' default."""
        ctx = _make_ctx(jwt="valid-token", customer_id="CUST-002")
        config = SimpleNamespace(memory_id="mem-test-001-xyz")  # no memory_region

        _, _, mock_manager_cls, _ = _call_with_mocks(ctx, config)

        call_kwargs = mock_manager_cls.call_args
        region_passed = call_kwargs.kwargs.get("region_name")
        assert region_passed == "us-west-2", (
            f"Default region 'us-west-2' expected when config has no memory_region, got {region_passed!r}"
        )
