"""Regression tests for supervisor prompt content — grounding-quality gate.

Filed against issues/2026-07-13-cvx-web-ui-uat-followups/ after the CVX web
UI PO UAT surfaced two failure modes tied to prompt wording:

  1. The retrieve-first hard rule listed "recall / warranty / part number /
     service procedure / policy" but not "DTC code" — leaving DTC-code
     interpretation questions (P0420, etc.) as a discretion-driven retrieve
     call. When retrieve was skipped, the response came from model priors
     with no citation trail.

  2. The prompt did not require an explicit non-KB-grounded disclosure when
     the LLM answered from model priors, so UAT could not distinguish
     "answered from KB (but no visible citation)" from "answered from model".

The fix adds "a DTC code" to rule 2's MUST-retrieve enumeration, and adds a
grounding-transparency rule that requires an explicit non-grounded prefix
or suffix whenever retrieve was NOT called.

These tests lock the wording so a future prompt refactor doesn't quietly
lose the guardrail. They are string-content tests only — no LLM inference,
no network calls.
"""

from __future__ import annotations

from agents.supervisor.supervisor import (
    SYSTEM_PROMPT_TEMPLATE,
    _HARD_RULES,
    build_system_prompt,
    prompt_hash,
)


class TestHardRulesRetrieveScope:
    """Rule 2 must explicitly compel `retrieve` for DTC codes."""

    def test_hard_rules_names_dtc_code(self):
        assert "DTC code" in _HARD_RULES, (
            "_HARD_RULES rule 2 must explicitly enumerate 'DTC code' among the "
            "MUST-retrieve topics — otherwise DTC-code questions (P0420 etc.) "
            "are discretion-driven and grounding becomes unreliable. See "
            "issues/2026-07-13-cvx-web-ui-uat-followups/."
        )

    def test_hard_rules_names_dtc_prefixes(self):
        # Spot-check the code families so a future rewrite can't drop them.
        for prefix in ("P0xxx", "C0xxx", "B0xxx", "U0xxx"):
            assert prefix in _HARD_RULES, (
                f"_HARD_RULES rule 2 must enumerate DTC prefix {prefix!r} to "
                "cover the OBD-II code families the KB indexes."
            )

    def test_system_prompt_template_names_dtc_code(self):
        # The text runtime (agentcore_app.py → build_supervisor) uses
        # SYSTEM_PROMPT_TEMPLATE directly, not _HARD_RULES. Both must carry
        # the DTC-code rule or the text path regresses to model priors.
        assert "DTC code" in SYSTEM_PROMPT_TEMPLATE, (
            "SYSTEM_PROMPT_TEMPLATE (text-runtime path) must also enumerate "
            "'DTC code' in rule 2 — same reason as _HARD_RULES."
        )


class TestHardRulesGroundingTransparency:
    """Un-grounded model-prior answers must self-disclose."""

    def test_hard_rules_requires_non_grounded_disclosure(self):
        assert "not retrieved" in _HARD_RULES.lower() or (
            "non-grounded" in _HARD_RULES.lower()
        ), (
            "_HARD_RULES must include a grounding-transparency rule that "
            "requires an explicit non-grounded disclosure when retrieve was "
            "NOT called. UAT could not distinguish KB-grounded from model-"
            "prior answers without this."
        )

    def test_hard_rules_mentions_kb_backed_tool_family(self):
        # The rule should acknowledge that knowledge/parts_lookup/
        # diy_repair_advisor also count as KB-backed — technicians and
        # service advisors use those instead of raw retrieve.
        for tool in ("retrieve", "knowledge", "parts_lookup", "diy_repair_advisor"):
            assert tool in _HARD_RULES, (
                f"_HARD_RULES grounding rule should name {tool!r} as a "
                "KB-backed tool so multi-persona wording stays inclusive."
            )

    def test_system_prompt_template_requires_non_grounded_disclosure(self):
        assert "not retrieved" in SYSTEM_PROMPT_TEMPLATE.lower() or (
            "non-grounded" in SYSTEM_PROMPT_TEMPLATE.lower()
        ), (
            "SYSTEM_PROMPT_TEMPLATE (text-runtime path) must carry the "
            "grounding-transparency rule too — the text runtime uses this "
            "template, not _HARD_RULES."
        )


class TestPromptRenderability:
    """Format-string safety — the rendered prompt must be a valid str with no
    unresolved {placeholders} for the fields we thread through."""

    def _mock_session(self):
        # Local minimal SessionContext-shaped object so this test doesn't
        # depend on session.py mutations.
        from agents.supervisor.session import SessionContext
        return SessionContext(
            tenant_id="michelin",
            session_id="test-sess-1234567890abcdef",
            jwt="not-a-real-jwt",
            persona_id="fleet_driver",
            vin="1FMCU0F70MUA12345",
        )

    def test_system_prompt_template_renders(self):
        rendered = SYSTEM_PROMPT_TEMPLATE.format(
            tenant_display="Michelin Fleet Services",
            tenant_id="michelin",
            segment="fleet",
            session_id="test-sess-1234567890abcdef",
            vin="1FMCU0F70MUA12345",
        )
        assert isinstance(rendered, str) and len(rendered) > 500
        # No stray unresolved placeholders
        assert "{" not in rendered.replace("{}", "")

    def test_build_system_prompt_fleet_driver_renders(self):
        rendered = build_system_prompt("fleet_driver", self._mock_session())
        assert isinstance(rendered, str) and len(rendered) > 500
        # Hard rules always prepended
        assert "Hard rules" in rendered
        # Persona-body substitutions worked
        assert "1FMCU0F70MUA12345" in rendered

    def test_prompt_hash_stable(self):
        h = prompt_hash("stable-input-42")
        assert isinstance(h, str) and len(h) == 16
        assert h == prompt_hash("stable-input-42")
