"""Test skeletons for anonymous-kiosk tool allow-list + session-manager wiring.

Spec: .kiro/specs/2026-07-31-zone2-order-backend-and-kiosk/ § D3 + Group 3 tasks

Verifies:
  (d) A kiosk session's constructed agent exposes EXACTLY the ten allow-listed
      tools and NONE of the other sixteen.
  (e) A kiosk session receives None from session_manager_or_none().

These tests are RED until Group 3 implements the kiosk allow-list in
supervisor.py / session.py. They must fail with AssertionError, not
ImportError or AttributeError.

The allow-list is asserted BY TOOL NAME (not count) so that adding a new
tool to the consumer persona or to the codebase fails this test rather
than silently widening the anonymous-kiosk attack surface. That failure
is the point of the test.

Allow-list (verbatim from spec D3):
    catalog_query, catalog_compare, variant_compare, configurator_recommend,
    offers_lookup, manufacturing_explain, delivery_status, retrieve,
    knowledge, kb_filter

Run: pytest agents/supervisor/tests/test_kiosk_gating.py -v
"""

from __future__ import annotations

import sys
import os
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Kiosk tool allow-list — verbatim from spec D3.
# Assert by name so adding a tool later fails this test.
# ---------------------------------------------------------------------------

KIOSK_ALLOW_LIST: frozenset[str] = frozenset(
    {
        "catalog_query",
        "catalog_compare",
        "variant_compare",
        "configurator_recommend",
        "offers_lookup",
        "manufacturing_explain",
        "delivery_status",
        "retrieve",
        "knowledge",
        "kb_filter",
    }
)

# ---------------------------------------------------------------------------
# Session-module paths for patching lazy SDK imports
# ---------------------------------------------------------------------------

_MANAGER_PATH = (
    "bedrock_agentcore.memory.integrations.strands.session_manager"
    ".AgentCoreMemorySessionManager"
)
_CONFIG_PATH = (
    "bedrock_agentcore.memory.integrations.strands.config"
    ".AgentCoreMemoryConfig"
)


# ---------------------------------------------------------------------------
# Helper factories
# ---------------------------------------------------------------------------


def _make_kiosk_ctx(
    *,
    tenant_id: str = "meridian-demo",
    session_id: str = "vsa-sess-kiosk-001abcdef123",
    jwt: str = "kiosk-device-cognito-token",  # kiosk has a valid JWT (D2)
    persona_id: str = "consumer",
) -> SimpleNamespace:
    """Minimal SessionContext-shaped namespace representing a kiosk session.

    A kiosk session has a valid JWT (the device IS authenticated, D2) but
    the Cognito group is 'kiosk', which signals that this is an anonymous
    visitor session rather than an owner-scoped signed-in session.

    Group 3 will add an ``is_kiosk: bool`` field (or equivalent) to
    SessionContext so build_supervisor and session_manager_or_none can
    route correctly. Using SimpleNamespace here keeps the skeleton
    importable even before that field exists on the real dataclass.
    """
    return SimpleNamespace(
        tenant_id=tenant_id,
        session_id=session_id,
        jwt=jwt,
        persona_id=persona_id,
        is_kiosk=True,          # new field — Group 3 adds this to SessionContext
        vin=None,
        vehicle_id=None,
        driver_id=None,
        customer_id=None,
        tenant_display="Meridian",
        triage_decision_id=None,
        triage_classification=None,
        last_kb_citations=None,
        last_offered_service_center=None,
        tenant_segment="oem",
    )


def _make_signed_in_ctx(
    *,
    tenant_id: str = "meridian-demo",
    session_id: str = "vsa-sess-owner-001abcdef123",
    jwt: str = "owner-cognito-token",
    persona_id: str = "consumer",
) -> SimpleNamespace:
    """A signed-in (non-kiosk) consumer session for contrast tests."""
    return SimpleNamespace(
        tenant_id=tenant_id,
        session_id=session_id,
        jwt=jwt,
        persona_id=persona_id,
        is_kiosk=False,
        vin=None,
        vehicle_id=None,
        driver_id=None,
        customer_id=None,
        tenant_display="Meridian",
        triage_decision_id=None,
        triage_classification=None,
        last_kb_citations=None,
        last_offered_service_center=None,
        tenant_segment="oem",
    )


def _make_config(
    *,
    model_id: str = "us.anthropic.claude-sonnet-4-6-20250514-v1:0",
    region: str = "us-west-2",
    memory_id: str = "mem-test-meridian-001",
) -> SimpleNamespace:
    """Minimal config stub exposing only the fields build_supervisor reads."""
    return SimpleNamespace(
        supervisor_model_id=model_id,
        aws_region=region,
        supervisor_max_tokens=4096,
        memory_id=memory_id,
        memory_region=region,
        acquire_catalog_table="vsa-staging-acquire-catalog",
        acquire_orders_table="vsa-staging-acquire-orders",
        acquire_leads_table="vsa-staging-acquire-leads",
        adp_kb_id="test-adp-kb-id",
        adp_region=region,
        discover_guardrails_identifier="discover-offers-guardrails-v1",
        acquire_kb_source_category="vehicle_manufacturing",
    )


def _build_supervisor_with_mock_model(ctx, config):
    """Call build_supervisor with BedrockModel patched so no AWS calls are made."""
    mock_model = MagicMock()
    with patch(
        "agents.supervisor.supervisor.BedrockModel", return_value=mock_model
    ):
        from agents.supervisor.supervisor import build_supervisor

        agent = build_supervisor(ctx, config)
    return agent


# ---------------------------------------------------------------------------
# Tool allow-list tests (spec D3)
# ---------------------------------------------------------------------------


class TestKioskToolAllowList:
    """build_supervisor enforces the ten-tool allow-list for kiosk sessions."""

    def test_kiosk_agent_has_exactly_the_ten_allow_listed_tools(self):
        """A kiosk session's agent exposes exactly the D3 allow-list — no more, no less.

        Asserted BY TOOL NAME. Adding a tool to the consumer persona or to the
        codebase will fail this test, which is intentional: the allow-list is the
        security boundary for the anonymous-kiosk surface.

        FAILS RED until Group 3 wires the kiosk allow-list into build_supervisor.
        """
        ctx = _make_kiosk_ctx()
        config = _make_config()

        agent = _build_supervisor_with_mock_model(ctx, config)

        actual_tools: set[str] = set(agent.tool_names)

        # Assert exact allow-list — no extras, no omissions.
        assert actual_tools == KIOSK_ALLOW_LIST, (
            f"\nKiosk tool surface diverges from spec D3 allow-list."
            f"\nExpected (10):  {sorted(KIOSK_ALLOW_LIST)}"
            f"\nActual   ({len(actual_tools)}): {sorted(actual_tools)}"
            f"\nExtra (must remove): {sorted(actual_tools - KIOSK_ALLOW_LIST)}"
            f"\nMissing (must add):  {sorted(KIOSK_ALLOW_LIST - actual_tools)}"
        )

    def test_kiosk_agent_does_not_expose_book(self):
        """'book' is owner-scoped and must not appear in a kiosk session."""
        ctx = _make_kiosk_ctx()
        config = _make_config()

        agent = _build_supervisor_with_mock_model(ctx, config)

        assert "book" not in agent.tool_names, (
            "'book' must not be exposed to kiosk sessions — it is an owner-scoped tool."
        )

    def test_kiosk_agent_does_not_expose_triage(self):
        """'triage' (fleet diagnostic) must not appear in a kiosk session."""
        ctx = _make_kiosk_ctx()
        config = _make_config()

        agent = _build_supervisor_with_mock_model(ctx, config)

        assert "triage" not in agent.tool_names, (
            "'triage' must not be exposed to kiosk sessions."
        )

    def test_kiosk_agent_does_not_expose_escalate(self):
        """'escalate' must not appear in a kiosk session."""
        ctx = _make_kiosk_ctx()
        config = _make_config()

        agent = _build_supervisor_with_mock_model(ctx, config)

        assert "escalate" not in agent.tool_names, (
            "'escalate' must not be exposed to kiosk sessions."
        )

    def test_kiosk_agent_does_not_expose_customer_360_or_owner_data_products(self):
        """Owner-scoped data-product tools must not appear in a kiosk session.

        These are correct for a signed-in consumer but wrong for an anonymous
        visitor at a conference kiosk (spec D3 rationale).
        """
        ctx = _make_kiosk_ctx()
        config = _make_config()

        agent = _build_supervisor_with_mock_model(ctx, config)
        actual_tools = set(agent.tool_names)

        owner_scoped_tools = {
            "vehicle_identity",
            "charging_history",
            "customer_profile",
            "interaction_history",
            "ota_history",
            "energy_summary",
            "service_network",
            "resolve_or_create_owner_identity",
            "lead_capture",
        }
        exposed_owner_tools = owner_scoped_tools & actual_tools
        assert not exposed_owner_tools, (
            f"Owner-scoped tools must not be exposed to kiosk sessions; "
            f"found: {sorted(exposed_owner_tools)}"
        )

    def test_kiosk_agent_exposes_all_ten_allow_listed_tools(self):
        """Every tool on the allow-list must be present (completeness guard).

        Paired with the upper-bound test above to give a clear signal on
        which direction the failure is in.
        """
        ctx = _make_kiosk_ctx()
        config = _make_config()

        agent = _build_supervisor_with_mock_model(ctx, config)
        actual_tools = set(agent.tool_names)

        missing = KIOSK_ALLOW_LIST - actual_tools
        assert not missing, (
            f"Allow-listed tools missing from kiosk agent: {sorted(missing)}"
        )

    def test_signed_in_consumer_gets_more_than_ten_tools(self):
        """A signed-in consumer gets the full consumer tool set (>10 tools).

        Confirms that the allow-list only narrows kiosk sessions, not all
        consumer sessions. If this fails, the kiosk gating is too broad.
        """
        ctx = _make_signed_in_ctx(persona_id="consumer")
        config = _make_config()

        agent = _build_supervisor_with_mock_model(ctx, config)

        assert len(agent.tool_names) > 10, (
            f"Signed-in consumer should have more than 10 tools; "
            f"got {len(agent.tool_names)}: {sorted(agent.tool_names)}"
        )

    def test_allow_list_is_exactly_ten_tools(self):
        """Sanity-check: the allow-list constant itself has exactly 10 entries.

        If this test fails, the test file itself has a bug — not the implementation.
        """
        assert len(KIOSK_ALLOW_LIST) == 10, (
            f"KIOSK_ALLOW_LIST must have exactly 10 entries; "
            f"has {len(KIOSK_ALLOW_LIST)}: {sorted(KIOSK_ALLOW_LIST)}"
        )


# ---------------------------------------------------------------------------
# session_manager_or_none tests for kiosk sessions (spec D3 / Risk R2)
# ---------------------------------------------------------------------------


class TestKioskSessionManagerNone:
    """session_manager_or_none returns None for kiosk sessions (spec Risk R2).

    Kiosk sessions are anonymous-visitor sessions: no long-term memory contact,
    no previous visitor's context leaking. A kiosk caller HAS a valid JWT
    (the device is authenticated, D2) but IS flagged as kiosk — the test
    verifies that the kiosk flag, not jwt emptiness, controls the None return.

    FAILS RED until Group 3 wires is_kiosk into session_manager_or_none().
    """

    def _call_with_mocks(self, ctx, config):
        """Call session_manager_or_none with both SDK classes patched at source."""
        mock_manager_instance = MagicMock()
        mock_manager_cls = MagicMock(return_value=mock_manager_instance)
        mock_config_cls = MagicMock()

        with patch(_MANAGER_PATH, mock_manager_cls), patch(_CONFIG_PATH, mock_config_cls):
            from agents.supervisor.session import session_manager_or_none

            result = session_manager_or_none(ctx, config)

        return result, mock_config_cls, mock_manager_cls, mock_manager_instance

    def test_kiosk_session_returns_none_despite_valid_jwt(self):
        """Kiosk sessions return None even when JWT is present.

        This distinguishes the kiosk case from the anonymous-jwt case already
        tested in test_session.py. The kiosk device IS authenticated; the None
        return is driven by is_kiosk, not by jwt emptiness.

        FAILS RED: current session_manager_or_none checks jwt only, not is_kiosk.
        The kiosk JWT is non-empty, so the current code would return a manager.
        """
        ctx = _make_kiosk_ctx(jwt="kiosk-device-valid-cognito-token")
        config = _make_config()

        result, _, mock_manager_cls, _ = self._call_with_mocks(ctx, config)

        assert result is None, (
            "Kiosk session must return None from session_manager_or_none() "
            "even though a valid JWT is present. The kiosk flag, not jwt "
            "emptiness, is the guard for anonymous-visitor LTM protection."
        )
        mock_manager_cls.assert_not_called()

    def test_kiosk_session_does_not_instantiate_memory_sdk(self):
        """AgentCore Memory SDK classes are NOT instantiated for kiosk sessions.

        Kiosk visitors are anonymous: touching LTM would merge their context
        with the previous visitor's session (spec Risk R2).
        """
        ctx = _make_kiosk_ctx(jwt="kiosk-device-valid-cognito-token")
        config = _make_config()

        _, mock_config_cls, mock_manager_cls, _ = self._call_with_mocks(ctx, config)

        assert mock_config_cls.call_count == 0, (
            "AgentCoreMemoryConfig must not be instantiated for kiosk sessions."
        )
        assert mock_manager_cls.call_count == 0, (
            "AgentCoreMemorySessionManager must not be instantiated for kiosk sessions."
        )

    def test_signed_in_consumer_gets_memory_manager(self):
        """A signed-in consumer (non-kiosk) DOES receive a memory manager.

        Confirms that the None return is specific to kiosk sessions, not to
        all consumer sessions. Memory is desirable for returning signed-in customers.
        """
        ctx = _make_signed_in_ctx(jwt="owner-valid-cognito-token")
        config = _make_config(memory_id="mem-test-meridian-001")

        result, _, mock_manager_cls, mock_manager_instance = self._call_with_mocks(
            ctx, config
        )

        assert result is mock_manager_instance, (
            "Signed-in consumer (is_kiosk=False) should receive an "
            "AgentCoreMemorySessionManager, not None."
        )
        mock_manager_cls.assert_called_once()

    def test_kiosk_flag_takes_priority_over_memory_id_present(self):
        """is_kiosk=True returns None even when memory_id is configured.

        Ensures the guard is the kiosk flag, not the absence of memory_id.
        """
        ctx = _make_kiosk_ctx(jwt="kiosk-device-valid-cognito-token")
        config = _make_config(memory_id="mem-test-meridian-001")  # configured

        result, _, mock_manager_cls, _ = self._call_with_mocks(ctx, config)

        assert result is None, (
            "is_kiosk=True must return None even when memory_id is configured."
        )
        mock_manager_cls.assert_not_called()


# ---------------------------------------------------------------------------
# Integration test — real SessionContext, not SimpleNamespace
# (Fix Group 2 Critical: the test that would have caught the Group 3 defect)
# ---------------------------------------------------------------------------


class TestKioskGatingWithRealSessionContext:
    """build_supervisor enforces the allow-list when given a REAL SessionContext.

    The Group 3 defect shipped through two green gates because the existing
    tests use SimpleNamespace(is_kiosk=True) — an object that has the attribute.
    The real SessionContext did NOT have ``is_kiosk`` as a dataclass field, so
    ``getattr(ctx, "is_kiosk", False)`` always returned False on the production
    path.

    Fix Group 2 adds ``is_kiosk: bool = False`` to SessionContext, plumbs the
    flag through ``handler.py`` → runtime payload → ``agentcore_app.py`` →
    ``SessionContext`` constructor, and this test class confirms the full chain
    works end-to-end with a real dataclass instance.

    Run: pytest agents/supervisor/tests/test_kiosk_gating.py::TestKioskGatingWithRealSessionContext -v
    """

    @staticmethod
    def _make_real_kiosk_ctx() -> object:
        """Construct a real SessionContext with is_kiosk=True, as agentcore_app does."""
        from agents.supervisor.session import SessionContext
        return SessionContext(
            tenant_id="meridian",
            session_id="vsa-sess-integration-kiosk-abc123",
            jwt="kiosk-device-cognito-token",
            persona_id="consumer",
            is_kiosk=True,  # set by agentcore_app after reading payload["isKiosk"]
        )

    @staticmethod
    def _make_real_non_kiosk_ctx() -> object:
        """Construct a real SessionContext with is_kiosk=False (default)."""
        from agents.supervisor.session import SessionContext
        return SessionContext(
            tenant_id="meridian",
            session_id="vsa-sess-integration-signed-in-abc123",
            jwt="owner-cognito-token",
            persona_id="consumer",
            # is_kiosk defaults to False
        )

    def test_real_sessioncontext_kiosk_has_exactly_ten_tools(self):
        """build_supervisor returns 10 tools (not 20) for a REAL kiosk SessionContext.

        This is the test that would have caught the Group 3 defect on the
        first review cycle. It fails BEFORE the fix (tool count is 20) and
        passes AFTER the fix (tool count is 10).

        The before/after tool counts are logged to stdout for the review record.
        """
        import dataclasses
        from agents.supervisor.session import SessionContext

        ctx = self._make_real_kiosk_ctx()
        config = _make_config()

        # Confirm is_kiosk is now a real field
        field_names = {f.name for f in dataclasses.fields(SessionContext)}
        assert "is_kiosk" in field_names, (
            "is_kiosk must be a real dataclass field on SessionContext. "
            "Fix Group 2 adds it so the production path can set it."
        )
        assert ctx.is_kiosk is True, "ctx.is_kiosk must be True for a kiosk session"

        agent = _build_supervisor_with_mock_model(ctx, config)
        actual_tools: set[str] = set(agent.tool_names)

        print(f"\n[integration] AFTER fix — real SessionContext kiosk tool count: {len(actual_tools)}")
        print(f"[integration] Tools: {sorted(actual_tools)}")

        assert actual_tools == KIOSK_ALLOW_LIST, (
            f"\nReal-SessionContext kiosk agent diverges from D3 allow-list.\n"
            f"Expected (10):  {sorted(KIOSK_ALLOW_LIST)}\n"
            f"Actual   ({len(actual_tools)}): {sorted(actual_tools)}\n"
            f"Extra (must remove): {sorted(actual_tools - KIOSK_ALLOW_LIST)}\n"
            f"Missing (must add):  {sorted(KIOSK_ALLOW_LIST - actual_tools)}"
        )

    def test_real_sessioncontext_non_kiosk_consumer_gets_full_surface(self):
        """A real SessionContext with is_kiosk=False gives the full consumer surface.

        Confirms the kiosk narrowing is specific to is_kiosk=True, not to all
        consumer sessions. Signed-in consumers should keep their full tool set.
        """
        ctx = self._make_real_non_kiosk_ctx()
        config = _make_config()

        agent = _build_supervisor_with_mock_model(ctx, config)
        actual_tools: set[str] = set(agent.tool_names)

        print(f"\n[integration] real SessionContext non-kiosk consumer tool count: {len(actual_tools)}")

        assert len(actual_tools) > 10, (
            f"Non-kiosk consumer (is_kiosk=False) should have more than 10 tools; "
            f"got {len(actual_tools)}: {sorted(actual_tools)}"
        )

    def test_is_kiosk_field_exists_on_sessioncontext(self):
        """SessionContext dataclass has is_kiosk: bool = False as a real field.

        Structural assertion: before this fix, is_kiosk was not in the dataclass
        fields, which caused getattr(ctx, 'is_kiosk', False) to return False on
        every production SessionContext regardless of how it was constructed.
        """
        import dataclasses
        from agents.supervisor.session import SessionContext

        field_names = {f.name for f in dataclasses.fields(SessionContext)}
        assert "is_kiosk" in field_names, (
            "is_kiosk must be a dataclass field on SessionContext so the "
            "production path can set it via the constructor."
        )

        # Default must be False — non-kiosk sessions are unaffected
        ctx_default = SessionContext(
            tenant_id="t", session_id="s", jwt="j", persona_id="fleet_driver"
        )
        assert ctx_default.is_kiosk is False, (
            "Default value of is_kiosk must be False so existing sessions "
            "are not accidentally treated as kiosk."
        )

    def test_real_sessioncontext_kiosk_session_manager_returns_none(self):
        """session_manager_or_none returns None for a real kiosk SessionContext."""
        from unittest.mock import patch, MagicMock
        from agents.supervisor.session import session_manager_or_none

        ctx = self._make_real_kiosk_ctx()
        config = _make_config()

        mock_manager_instance = MagicMock()
        mock_manager_cls = MagicMock(return_value=mock_manager_instance)
        mock_config_cls = MagicMock()

        with patch(_MANAGER_PATH, mock_manager_cls), patch(_CONFIG_PATH, mock_config_cls):
            result = session_manager_or_none(ctx, config)

        assert result is None, (
            "Real kiosk SessionContext (is_kiosk=True) must return None from "
            "session_manager_or_none() — no LTM contact for anonymous visitors."
        )
        mock_manager_cls.assert_not_called()
