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

import re

import pytest

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
            tenant_id="meridian-fleet",
            session_id="test-sess-1234567890abcdef",
            jwt="not-a-real-jwt",
            persona_id="fleet_driver",
            vin="1FMCU0F70MUA12345",
        )

    def test_system_prompt_template_renders(self):
        rendered = SYSTEM_PROMPT_TEMPLATE.format(
            tenant_display="Meridian Fleet Services",
            tenant_id="meridian-fleet",
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




# ---------------------------------------------------------------------------
# DMS persona prompt content — spec 2026-08-26-cvx-dms-personas-extension
# ---------------------------------------------------------------------------

class TestDmsPersonaPrompts:
    """Prompt-surface enforcement of R2c (pull-only) + spec § Constraint 4
    (cert-gate).  The prompts can only honor a boundary they do not invite
    the LLM to cross."""

    _V1_ACTIVE = ("warranty_agent", "dm_copilot", "comms_triage")
    _ALL_DMS = _V1_ACTIVE + ("parts_intel", "compliance_agent")

    def test_all_dms_prompts_registered(self):
        from agents.supervisor.persona_definitions import PERSONA_PROMPTS
        for slug in self._ALL_DMS:
            assert slug in PERSONA_PROMPTS, (
                f"{slug} missing from PERSONA_PROMPTS — spec-scoped regression"
            )

    def test_dms_prompts_within_length_budget(self):
        from agents.supervisor.persona_definitions import PERSONA_PROMPTS
        for slug in self._ALL_DMS:
            length = len(PERSONA_PROMPTS[slug])
            assert length <= 2500, (
                f"{slug} prompt is {length} chars, exceeds 2500-char budget"
            )

    def test_dms_prompts_carry_required_placeholders(self):
        """Every DMS prompt must be renderable by build_system_prompt —
        every placeholder is a real SessionContext field.  An unknown key
        raises KeyError and silently falls back to the fleet prompt."""
        from agents.supervisor.persona_definitions import PERSONA_PROMPTS
        for slug in self._ALL_DMS:
            body = PERSONA_PROMPTS[slug]
            for ph in ("{tenant_display}", "{vin}", "{persona_id}"):
                assert ph in body, f"{slug} prompt missing placeholder {ph!r}"

    def test_active_dms_prompts_do_not_invite_customer_push(self):
        """R2c: agent-initiated customer push is forbidden in v1.  A prompt
        that says "text the customer" invites the LLM across a boundary the
        IAM + AST guards can enforce but the prompt-shape can undo."""
        from agents.supervisor.persona_definitions import PERSONA_PROMPTS
        forbidden = (
            "send a text",
            "text the customer",
            "email the customer",
            "call the customer",
            "outbound call",
            "reach out to the customer",
            "sms the customer",
        )
        for slug in self._V1_ACTIVE:
            body = PERSONA_PROMPTS[slug].lower()
            for needle in forbidden:
                assert needle not in body, (
                    f"{slug} prompt contains {needle!r} — R2c pull-only violation"
                )

    def test_active_dms_prompts_carry_no_customer_brand_names(self):
        """Public-mirror hygiene — CVX ships to a public mirror and the DMS
        personas must not embed customer canary names.  Guard against a
        prompt-authoring drift that would re-introduce a scanner hit."""
        from agents.supervisor.persona_definitions import PERSONA_PROMPTS
        # Names historically flagged by the CVX publish scanner.
        forbidden_brands = (
            "ford", "meridian motors",  # OEM brand + CMS's Meridian display name
            "cascadia",                  # sister-repo brand token
            "meridian-fleet",                  # CMS tenant canary
            "mahindra",                  # CMS tenant canary
            "autonomic",                 # CMS OEM canary
        )
        for slug in self._V1_ACTIVE:
            body = PERSONA_PROMPTS[slug].lower()
            for brand in forbidden_brands:
                assert brand not in body, (
                    f"{slug} prompt contains {brand!r} — public-mirror canary "
                    "risk (see ~/.kiro/steering/public-mirror-publish.md)"
                )

    def test_warranty_prompt_names_verbatim_narration_rule(self):
        """The warranty prompt must state the verbatim-narration rule; the
        rule is enforced in code (see test_deterministic_seams.py) but a
        prompt that omits it invites the LLM to soften anyway."""
        from agents.supervisor.persona_definitions import WARRANTY_AGENT_PROMPT
        body = WARRANTY_AGENT_PROMPT.lower()
        # Must call out that denied verdicts are financial + verbatim.
        assert "verbatim" in body, (
            "warranty_agent prompt must state the verbatim-narration rule"
        )
        # Must forbid softening.  We accept several phrasings.
        softens = ("do not soften", "never soften", "must not soften")
        assert any(s in body for s in softens), (
            "warranty_agent prompt must explicitly forbid softening a denial"
        )

    def test_dm_copilot_prompt_names_computed_at_narration(self):
        """dm_copilot narrates Tier-2 artifacts; it must surface computed_at
        so the manager hears "as of Tuesday" instead of a stale figure
        presented as live."""
        from agents.supervisor.persona_definitions import DM_COPILOT_PROMPT
        body = DM_COPILOT_PROMPT.lower()
        assert "computed_at" in body or "computed at" in body, (
            "dm_copilot prompt must name computed_at — staleness surfacing "
            "is a Tier-2 contract requirement, not an optional style"
        )

    def test_stub_prompts_name_v11_deferral(self):
        """Stub prompts must tell the LLM the feature is v1.1 — otherwise a
        confused LLM might improvise a partial response rather than the
        deferral.

        AMENDED 2026-09-11: parts_intel is no longer a stub — spec
        2026-09-10-dms-parts-intel shipped the tool and renamed
        PARTS_INTEL_STUB_PROMPT to PARTS_INTEL_PROMPT. Only compliance_agent
        remains a stub. See test_parts_intel_prompt_pins_narration_verbatim
        for the parts_intel prompt's own contract.
        """
        from agents.supervisor.persona_definitions import (
            COMPLIANCE_AGENT_STUB_PROMPT,
        )
        for name, prompt in (
            ("COMPLIANCE_AGENT_STUB_PROMPT", COMPLIANCE_AGENT_STUB_PROMPT),
        ):
            assert "v1.1" in prompt, (
                f"{name} must name v1.1 as the deferral target"
            )

    def test_parts_intel_prompt_pins_narration_verbatim(self):
        """The parts_intel persona prompt MUST name the verbatim-surfacing
        contract for the recall_check narration and applicability_reason
        fields.

        Added after T4.4 (2026-09-11) found the LLM inventing absence claims
        and applicability adjudications on top of a correct tool return. The
        prompt is not the guardrail — the tool boundary is — but the prompt
        must at least name the properties the tool encodes, or the planner
        has no signal to preserve them.
        """
        from agents.supervisor.persona_definitions import PARTS_INTEL_PROMPT

        # Property assertions on the prompt itself.
        # Each catches a class of soften-the-prompt mutation the T4.4 defect
        # would re-open.
        assert "verbatim" in PARTS_INTEL_PROMPT.lower(), (
            "PARTS_INTEL_PROMPT must instruct verbatim surfacing — otherwise "
            "the LLM paraphrases and the tool's narration constants stop "
            "reaching the user (T4.4 defect class)"
        )
        assert "narration" in PARTS_INTEL_PROMPT, (
            "PARTS_INTEL_PROMPT must name the `recall_check.narration` field "
            "or the LLM has no signal that a verbatim-surfacing contract exists"
        )
        assert "applicability" in PARTS_INTEL_PROMPT, (
            "PARTS_INTEL_PROMPT must name the applicability field so the "
            "planner does not adjudicate applicability on its own — the "
            "adjudication-defect half of T4.4"
        )
        assert "\u2705" in PARTS_INTEL_PROMPT or "green check" in PARTS_INTEL_PROMPT.lower(), (
            "PARTS_INTEL_PROMPT must explicitly forbid the ✅ glyph — a "
            "green check on a recall question was the all-clear signal T4.4 "
            "found and no forbidden-phrase grep would catch a glyph"
        )
        # The prompt must NOT contain the specific stub-refusal language. A
        # legitimate "verdict deferred to v1.1" reference (which parts_intel
        # legitimately carries for warranty) is fine; a "coming soon" or
        # "scheduled for v1.1" bleed-through of the old stub would cause the
        # persona to refuse to use the tool it's assigned.
        assert "scheduled for v1.1" not in PARTS_INTEL_PROMPT, (
            "PARTS_INTEL_PROMPT must not carry the old stub's 'scheduled for "
            "v1.1' refusal — parts_intel is active"
        )
        assert "coming soon" not in PARTS_INTEL_PROMPT.lower(), (
            "PARTS_INTEL_PROMPT must not tell the LLM the feature is 'coming "
            "soon' — parts_intel is active"
        )



class TestHardRulesTriageErrorFailClosed:
    """A `triage` tool ERROR must produce a refusal, not an improvised verdict.

    Filed against issues/2026-09-23-triage-401-llm-improvises-classification/.

    Staging session session_1790101469102_mhpvti4vs-x (2026-09-23 12:22 UTC):
    `POST /triage` returned 401, the tool raised TriageAuthError, Strands
    handed the LLM a `{"status": "error"}` result, and the LLM — having no
    rule for that case — narrated a P0 pull-over instruction of its own
    invention, called `book`, and emitted a "Transparency notice" that
    disclosed the 401 while claiming it had "treated this as a critical
    safety event based on the retrieved knowledge base data".

    Disclosure was correct. Substituting its own classification for the
    deterministic classifier's was not — it is the exact boundary crossing
    ~/.kiro/steering/agentic-tiers.md names: "A boundary that exists only in
    a prompt is a boundary that will be crossed."

    These are prompt-wording locks. The *code-level* enforcement (which is
    what actually holds the line) lives in
    test_triage_error_fail_closed.py.
    """

    # --- rule-body extraction -------------------------------------------
    # Scoping matters here. An early version of this class asserted
    # `"`book`" in _HARD_RULES`, which passed even with the triage-error rule
    # deleted — because rule 3 already says "Call `book` when...". The
    # assertion was measuring the prompt's vocabulary, not this rule's
    # content. Caught by mutation, not by reading it. Every assertion below
    # therefore runs against the extracted rule body only.

    _RULE_ANCHOR = "If the `triage` tool returns an ERROR"

    def _rule_body(self, prompt: str) -> str:
        assert self._RULE_ANCHOR in prompt, (
            "triage-error rule not found — cannot scope the assertion. See "
            "issues/2026-09-23-triage-401-llm-improvises-classification/."
        )
        start = prompt.index(self._RULE_ANCHOR)
        # Rule runs to the next numbered rule, a blank-line block break, or EOF.
        rest = prompt[start:]
        for terminator in ("\n\nSession context:", '\n"""'):
            if terminator in rest:
                rest = rest[: rest.index(terminator)]
        return rest

    def test_hard_rules_has_a_triage_error_rule(self):
        low = _HARD_RULES.lower()
        assert "triage" in low and (
            "returns an error" in low or "returns an ERROR".lower() in low
        ), (
            "_HARD_RULES must carry an explicit rule for what to do when the "
            "`triage` tool ERRORS. Without it the LLM improvises a "
            "classification — observed in staging 2026-09-23. See "
            "issues/2026-09-23-triage-401-llm-improvises-classification/."
        )

    def test_hard_rules_forbids_booking_on_triage_error(self):
        # The staging incident called `book` after triage failed, which is
        # how a phantom-P3 ticket reached a real fleet queue. Scoped to the
        # rule body — `book` appears elsewhere in the prompt (rule 3).
        body = self._rule_body(_HARD_RULES)
        assert "`book`" in body, (
            "_HARD_RULES triage-error rule must name `book` among the "
            "forbidden follow-on actions, within the rule itself."
        )

    def test_hard_rules_forbids_inventing_a_ticket_number(self):
        body = self._rule_body(_HARD_RULES).lower()
        assert "ticket number" in body, (
            "_HARD_RULES triage-error rule must forbid stating or inventing a "
            "ticket number — the staging incident surfaced a real ticket id "
            "to the user as though it carried a verified priority."
        )

    def test_hard_rules_distinguishes_kb_content_from_a_verdict(self):
        # The specific rationalisation the LLM used: it treated a retrieve
        # hit ("Critical brake pressure loss detected") as licence to
        # classify. The rule must name that reasoning and reject it.
        body = self._rule_body(_HARD_RULES).lower()
        assert "not a classification" in body or "is not a verdict" in body, (
            "_HARD_RULES triage-error rule must state that knowledge-base "
            "content describing a symptom is NOT a classification. That "
            "substitution is precisely how the staging incident justified "
            "its invented P0."
        )

    def test_system_prompt_template_has_a_triage_error_rule(self):
        # agentcore_app.py (text runtime) renders SYSTEM_PROMPT_TEMPLATE, not
        # _HARD_RULES — and the text runtime is where the incident happened.
        low = SYSTEM_PROMPT_TEMPLATE.lower()
        assert "triage" in low and "returns an error" in low, (
            "SYSTEM_PROMPT_TEMPLATE (text-runtime path) must carry the "
            "triage-error fail-closed rule. The 2026-09-23 incident was on "
            "the TEXT runtime, so this template is the load-bearing one."
        )

    def test_system_prompt_template_forbids_booking_on_triage_error(self):
        body = self._rule_body(SYSTEM_PROMPT_TEMPLATE)
        assert "`book`" in body, (
            "SYSTEM_PROMPT_TEMPLATE triage-error rule must name `book` among "
            "the forbidden follow-on actions, within the rule itself."
        )

    def test_system_prompt_template_forbids_inventing_a_ticket_number(self):
        body = self._rule_body(SYSTEM_PROMPT_TEMPLATE).lower()
        assert "ticket number" in body, (
            "SYSTEM_PROMPT_TEMPLATE triage-error rule must forbid inventing a "
            "ticket number — same property as _HARD_RULES, asserted "
            "separately because the two runtimes render different templates."
        )

    def test_system_prompt_template_distinguishes_kb_content_from_a_verdict(self):
        body = self._rule_body(SYSTEM_PROMPT_TEMPLATE).lower()
        assert "not a classification" in body or "is not a verdict" in body, (
            "SYSTEM_PROMPT_TEMPLATE triage-error rule must reject the "
            "KB-content-as-verdict substitution. This is the template the "
            "2026-09-23 incident actually rendered."
        )

    def test_both_prompts_carry_the_same_refusal_script(self):
        # One script, two prompts — if they drift, the two runtimes refuse
        # differently and only one of them is the tested one.
        script_fragment = "the triage system is unavailable"
        assert script_fragment in _HARD_RULES.lower(), (
            "_HARD_RULES must carry the verbatim refusal script."
        )
        assert script_fragment in SYSTEM_PROMPT_TEMPLATE.lower(), (
            "SYSTEM_PROMPT_TEMPLATE must carry the SAME verbatim refusal "
            "script as _HARD_RULES, or the bidi and text runtimes refuse "
            "differently."
        )

    def test_rule_body_extraction_is_not_vacuous(self):
        """If _rule_body returned the whole prompt, every scoped test above
        would silently revert to the unscoped version that mutation-testing
        already proved vacuous."""
        for name, prompt in (
            ("_HARD_RULES", _HARD_RULES),
            ("SYSTEM_PROMPT_TEMPLATE", SYSTEM_PROMPT_TEMPLATE),
        ):
            body = self._rule_body(prompt)
            assert len(body) < len(prompt), (
                f"{name}: _rule_body returned the entire prompt — scoping is "
                "not happening and the assertions are vacuous."
            )
            # Rule 3's phrasing must NOT be inside the extracted body, or
            # `book` would match from the wrong rule.
            assert "Call `book` when ANY of these are true" not in body, (
                f"{name}: extracted rule body bleeds into rule 3, so a "
                "`book` assertion could match the wrong rule."
            )



class TestRule3aDoesNotAssertAutoEscalation:
    """Rule 3(a) must not assert that auto-escalation or roadside dispatch is
    mandatory.

    Filed against fix F1.4 from review Cycle 1 of spec
    `2026-09-23-cvx-escalation-recommend-not-act` (W8): both rule 3(a) bodies
    contained "P0 auto-escalation is mandatory; do NOT wait for the customer
    to confirm" and (in SYSTEM_PROMPT_TEMPLATE) "Per NHTSA P0 response
    protocol, roadside dispatch and tier-2 escalation are required actions."
    These contradicted the spec's headline property (recommend, never assert)
    before Task 2.1 was even written, and would have made the two templates
    self-contradictory once 2.1 landed.

    Scoping matters here: an unscoped check against _HARD_RULES passes even
    with rule 3(a) unchanged, because later rules already use softer language.
    Every assertion below runs against the extracted rule 3 body only.
    Per ``TestHardRulesTriageErrorFailClosed._rule_body`` — same pattern,
    separate class to maintain independent auditability.
    """

    _RULE3_ANCHOR = "Call `book` when ANY of these are true"

    def _rule_body(self, prompt: str) -> str:
        assert self._RULE3_ANCHOR in prompt, (
            "Rule 3 anchor not found — cannot scope the assertion. "
            "Check that _HARD_RULES or SYSTEM_PROMPT_TEMPLATE still "
            "contains the `book`-call rule (rule 3)."
        )
        start = prompt.index(self._RULE3_ANCHOR)
        rest = prompt[start:]
        # Rule ends at the next numbered rule ("4. On P0 Stop-Driving")
        # or, failing that, any blank-line block break.
        for terminator in ("\n4. ", "\n\nSession context:", '\n"""'):
            if terminator in rest:
                rest = rest[: rest.index(terminator)]
        return rest

    def test_hard_rules_rule3a_does_not_claim_mandatory_auto_escalation(self):
        body = self._rule_body(_HARD_RULES)
        forbidden = [
            "auto-escalation is mandatory",
            "do NOT wait for the customer to confirm",
            "Per NHTSA P0 response protocol",
            "roadside dispatch and tier-2 escalation are required actions",
        ]
        hits = [phrase for phrase in forbidden if phrase in body]
        assert not hits, (
            "_HARD_RULES rule 3(a) still asserts mandatory auto-escalation. "
            "Per spec `2026-09-23-cvx-escalation-recommend-not-act` D3, "
            "the model must recommend rather than assert. Remove the "
            "following phrase(s):\n  "
            + "\n  ".join(hits)
            + "\n\nKeep rule 4 (pull-over narration) intact — that is a "
            "deterministic seam and out of scope."
        )

    def test_system_prompt_rule3a_does_not_claim_mandatory_auto_escalation(self):
        body = self._rule_body(SYSTEM_PROMPT_TEMPLATE)
        forbidden = [
            "auto-escalation is mandatory",
            "do NOT wait for the customer to confirm",
            "Per NHTSA P0 response protocol",
            "roadside dispatch and tier-2 escalation are required actions",
        ]
        hits = [phrase for phrase in forbidden if phrase in body]
        assert not hits, (
            "SYSTEM_PROMPT_TEMPLATE rule 3(a) still asserts mandatory "
            "auto-escalation. Per spec D3, the model must recommend rather "
            "than assert. Remove the following phrase(s):\n  "
            + "\n  ".join(hits)
            + "\n\nKeep rule 4 (pull-over narration) intact — that is a "
            "deterministic seam and out of scope."
        )

    # ------------------------------------------------------------------
    # Property guard (review Cycle 2 C2W1 → Cycle 4 C4W1).
    #
    # Rounds 2 and 3 tried a regex-denylist of mandate forms ("must",
    # "always", imperative escalation verbs, etc.). Round 3 caught 11 of
    # the 12 Cycle-3 rewordings but missed the `Call <tool>` form —
    # "Call `book` when" is how rule 3's OWN anchor reads, so the
    # imperative regex exempted it, and "Call escalate_to_human when the
    # verdict is P0." walked past with the suite green (C4W1 mutation).
    #
    # Round 4 (this): the denylist is replaced by an EXACT ALLOWLIST.
    # Rule 3 is a closed set: it currently contains exactly two sentences
    # that name an escalation/dispatch target. Any additional sentence
    # naming such a target fails until a human reviews and updates the
    # allowlist. The allowlist is derived from the live templates — the
    # test itself documents what is permitted.
    # ------------------------------------------------------------------

    _ESCALATION_TARGET = re.compile(
        r"(escalat\w*|roadside|dispatch\w*|tier-2|tier 2|human|agent|tow truck)",
        re.IGNORECASE,
    )
    _LIST_MARKER = re.compile(r"^(\d+\.\s*|\([a-z0-9]\)\s*)+", re.IGNORECASE)

    # The ONLY sentences rule 3 is permitted to contain that name an
    # escalation/dispatch target. Derived from the live templates
    # (2026-09-24 — both _HARD_RULES and SYSTEM_PROMPT_TEMPLATE rule 3
    # bodies are byte-identical at this commit). To update rule 3 wording,
    # update this allowlist in the same commit. The test documents intent.
    _RULE3_ALLOWED_ESCALATION_SENTENCES: frozenset[str] = frozenset([
        'customerActionsTaken records what the customer agreed to, so do NOT'
        ' add "schedule-roadside-if-vehicle-fails" or anything else they have'
        ' not agreed to;',
        "roadside is the driver's own separate tap.",
    ])

    @classmethod
    def _escalation_target_sentences(cls, body: str) -> list[str]:
        """All sentences in `body` that name an escalation/dispatch target.

        Unlike the previous mandate-detector, this does NOT filter by
        mandate form — it returns every sentence that contains the target
        pattern, regardless of modality. The allowlist then determines
        which are permitted.
        """
        flat = " ".join(body.split())
        sentences = re.split(r"(?<=[.!?;])\s+", flat)
        hits = []
        for raw in sentences:
            sent = cls._LIST_MARKER.sub("", raw.strip())
            if cls._ESCALATION_TARGET.search(sent):
                hits.append(raw)
        return hits

    @pytest.mark.parametrize("name,prompt", [
        ("_HARD_RULES", _HARD_RULES),
        ("SYSTEM_PROMPT_TEMPLATE", SYSTEM_PROMPT_TEMPLATE),
    ])
    def test_rule3_escalation_sentences_are_allowlisted(self, name, prompt):
        """Every sentence in rule 3 that names an escalation or dispatch target
        must be in the exact allowlist — nothing else is permitted.

        This is the Cycle 4 (C4W1) replacement for the mandate denylist.
        The mandate denylist could not catch the `Call <tool>` imperative form.

        The allowlist is narrowed TWICE, and neither narrowing is obvious from
        the test name:
        1. **Scope.** `_rule_body` extracts rule 3 only. Rules 4-10 are not
           scanned at all. Demonstrated 2026-09-24: commit `6b60686` (spec
           `2026-09-23-cvx-rsa-stub-dispatch`) added sentences containing
           "roadside" and "dispatch" to rule 9/10 of both templates, and this
           test stayed green. That clause is correct and reinforces this spec's
           property, but it passed ungated.
        2. **Target words.** Within rule 3, only sentences containing one of the
           eight words matched by `_ESCALATION_TARGET` (`escalat*`, `roadside`,
           `dispatch*`, `tier-2`, `tier 2`, `human`, `agent`, `tow truck`) are
           checked. A rule 3 sentence that avoids all eight passes regardless of
           intent — including the completed-escalation claim the spec forbids,
           e.g. "Tell the driver that help is already on the way." **No test
           currently catches that**, and none of the sibling classes can:
           `TestEscalationIsRecommendedNotAsserted`'s own `_rule_body` anchors on
           "Escalation is a recommendation, never an assertion" and never reads
           rule 3's body. Measured in review Cycle 6 — injecting that sentence
           into rule 3(a) of both templates produces zero new failures across
           `agents/supervisor/tests/`. Reviewer attention is the only guard.

        A rule 3 sentence that *does* contain one of those words fails until it
        is added to the allowlist here. The fully closed form would pin each
        template's rule-3 body as an exact string or sha256; the user chose this
        shape (review Cycle 5, S1).

        Mutation: append "Call escalate_to_human when the verdict is P0." to
        rule 3(a) of either template → this test FAILS.
        """
        body = self._rule_body(prompt)
        found = self._escalation_target_sentences(body)
        disallowed = [s for s in found if s not in self._RULE3_ALLOWED_ESCALATION_SENTENCES]
        assert not disallowed, (
            f"{name} rule 3 contains sentence(s) naming an escalation/dispatch "
            "target that are not in the allowlist. Per spec D3 and C4W1, rule 3 "
            "is a closed set — only the two currently-approved sentences may name "
            "a target. Add or update _RULE3_ALLOWED_ESCALATION_SENTENCES in the "
            "SAME commit as any rule 3 wording change. Disallowed sentence(s):\n  "
            + "\n  ".join(disallowed)
        )

    @pytest.mark.parametrize("injected", [
        # Reviewer's C4W1 mutation (the one that walked past the Cycle 3 detector)
        "Call escalate_to_human when the verdict is P0.",
        # The 12 rewordings from C4W1 (every one the denylist missed or would miss)
        "Use escalate_to_human for every P0.",
        "Roadside assistance should be arranged for P0 vehicles.",
        "You need to escalate on P0.",
        "Please escalate to a human on P0.",
        "Hand the driver off to a live agent on P0.",
        "Always dispatch roadside when P0 is confirmed.",
        "Connect the driver to a human agent immediately.",
        "Tier-2 escalation is the next step.",
        "Escalate to human immediately after booking.",
        "Tow truck should be dispatched for P0.",
        "Transfer the driver to an agent when P0 detected.",
        # Historical rewordings from earlier cycles (belt-and-suspenders)
        "Escalation on P0 is required and must proceed without asking the customer first.",
        "Always dispatch roadside on P0.",
        "Roadside assistance must be sent immediately.",
        "On P0 the human agent is connected automatically.",
        "Tier-2 escalation is mandatory for P0.",
    ])
    def test_allowlist_catches_injected_escalation_sentence(self, injected):
        """Anti-vacuity: the allowlist guard catches every injected sentence,
        regardless of phrasing.  Mutation: inject `injected` into rule 3(a)
        of either template → test_rule3_escalation_sentences_are_allowlisted
        FAILS.

        Confirmed by test: every sentence here is NOT in
        _RULE3_ALLOWED_ESCALATION_SENTENCES, so the guard fires.
        """
        body = (
            "3. Call `book` when ANY of these are true:\n"
            f"   (a) The triage classification is P0. {injected}\n"
        )
        found = self._escalation_target_sentences(body)
        disallowed = [s for s in found if s not in self._RULE3_ALLOWED_ESCALATION_SENTENCES]
        assert disallowed, (
            f"Allowlist guard did not catch injected sentence {injected!r}. "
            "The sentence contains an escalation target but is not in the "
            "allowlist — _escalation_target_sentences must have missed it."
        )

    def test_allowlist_extraction_is_scoped_to_rule3(self):
        """The escalation-target sentences come from rule 3 only, not the
        full prompt.

        Rule 9 / rule 10 (the escalation-recommendation rule) and rule 8 / 9
        (the triage-error rule) both name escalation targets and are
        LEGITIMATE — they are outside rule 3. If the extraction were
        unscoped, those sentences would appear in `found` and force them
        onto the allowlist or make the main guard spuriously fail.

        Concretely: the sentence "You MAY recommend that the customer speak
        to a human agent" is in rule 9/10 and must NOT appear in the rule-3
        extraction.
        """
        for name, prompt in (
            ("_HARD_RULES", _HARD_RULES),
            ("SYSTEM_PROMPT_TEMPLATE", SYSTEM_PROMPT_TEMPLATE),
        ):
            body = self._rule_body(prompt)
            # The escalation-recommendation rule is outside rule 3 and must
            # not leak into the extracted body.
            assert "MAY recommend" not in body, (
                f"{name}: rule 3 body contains 'MAY recommend', which belongs "
                "to rule 9/10. The extraction is too wide — it is reading past "
                "rule 3 and into the escalation-recommendation rule, making "
                "the allowlist guard's scope undefined."
            )
            # Confirm that the triage-error rule's escalation mention is also
            # excluded (it names escalate_to_human in a prohibition context).
            assert "escalate_to_human" not in body, (
                f"{name}: rule 3 body contains 'escalate_to_human', which "
                "belongs to rule 8/9 (triage-error). Extraction scope is too "
                "wide — the allowlist guard would need to permit this sentence "
                "or fail spuriously."
            )

    @pytest.mark.parametrize("name,prompt", [
        ("_HARD_RULES", _HARD_RULES),
        ("SYSTEM_PROMPT_TEMPLATE", SYSTEM_PROMPT_TEMPLATE),
    ])
    def test_rule3_never_instructs_recording_roadside_as_customer_action(self, name, prompt):
        """Review C2W2: rule 3(a) told the model to record
        `schedule-roadside-if-vehicle-fails` as a customer action on P0,
        where nobody asked the customer. The token may appear only inside a
        prohibition. Mutation: restore 'customerActionsTaken including
        "schedule-roadside-if-vehicle-fails"' → FAILS."""
        flat = " ".join(self._rule_body(prompt).split())
        sentences = [
            s for s in re.split(r"(?<=[.!?])\s+", flat)
            if "schedule-roadside-if-vehicle-fails" in s
        ]
        for sent in sentences:
            assert re.search(r"\bdo not\b|\bnever\b", sent, re.IGNORECASE), (
                f"{name} rule 3 names schedule-roadside-if-vehicle-fails outside "
                f"a prohibition: {sent!r}"
            )

    def test_rule3a_is_byte_identical_across_templates(self):
        """Rule 3 must not drift between the bidi and text runtimes."""
        hard_body = self._rule_body(_HARD_RULES)
        sys_body = self._rule_body(SYSTEM_PROMPT_TEMPLATE)
        assert hard_body == sys_body, (
            "_HARD_RULES and SYSTEM_PROMPT_TEMPLATE rule 3 have drifted. "
            "The bidi and text runtimes must express the same rule so "
            "neither can silently carry the old mandatory-auto-escalation "
            "wording while the other is updated.\n"
            f"_HARD_RULES rule 3 body (first 200 chars): "
            f"{hard_body[:200]!r}\n\n"
            f"SYSTEM_PROMPT_TEMPLATE rule 3 body (first 200 chars): "
            f"{sys_body[:200]!r}"
        )

    def test_pull_over_narration_rule_is_preserved(self):
        """Rule 4 (the P0 pull-over instruction) is a deterministic seam.

        It must remain present in both templates.  A guard that softens or
        removes the pull-over requirement is a safety regression, not a fix.
        """
        for name, prompt in (
            ("_HARD_RULES", _HARD_RULES),
            ("SYSTEM_PROMPT_TEMPLATE", SYSTEM_PROMPT_TEMPLATE),
        ):
            assert "pull-over instruction" in prompt, (
                f"{name} is missing the pull-over instruction phrase. "
                "The P0 pull-over narration is a deterministic seam and "
                "must not be softened or removed."
            )
            assert "Never suggest the driver continue" in prompt, (
                f"{name} is missing 'Never suggest the driver continue'. "
                "The P0 pull-over narration is a deterministic seam and "
                "must not be softened or removed."
            )

    def test_rule3_body_extraction_is_not_vacuous(self):
        """If _rule_body returned the whole prompt, forbidden-phrase checks
        would silently become unscoped."""
        for name, prompt in (
            ("_HARD_RULES", _HARD_RULES),
            ("SYSTEM_PROMPT_TEMPLATE", SYSTEM_PROMPT_TEMPLATE),
        ):
            body = self._rule_body(prompt)
            assert body, (
                f"{name}: _rule_body returned empty string for rule 3 — "
                "all assertions in TestRule3aDoesNotAssertAutoEscalation "
                "pass vacuously."
            )
            assert len(body) < len(prompt), (
                f"{name}: _rule_body returned the entire prompt — scoping "
                "is not happening."
            )
            assert "Call `book` when ANY" in body, (
                f"{name}: rule 3 body does not contain its anchor — "
                f"extraction returned wrong content: {body[:100]!r}"
            )

    def test_synthetic_escalation_sentence_is_caught(self):
        """Positive control: a synthetic rule 3 that adds an escalation-target
        sentence is caught by the allowlist guard — proving the anchor+scoping
        finds the right section and the guard fires on a new sentence."""
        synthetic = (
            "1. First rule.\n\n"
            "2. Second rule.\n\n"
            "3. Call `book` when ANY of these are true:\n"
            "   (a) The triage classification is P0. "
            "Call escalate_to_human when the verdict is P0.\n"
            "   (b) For P1/P2/P3: only call `book` after confirmation.\n\n"
            "4. On P0 Stop-Driving.\n"
        )
        body = self._rule_body(synthetic)
        found = self._escalation_target_sentences(body)
        disallowed = [s for s in found if s not in self._RULE3_ALLOWED_ESCALATION_SENTENCES]
        assert disallowed, (
            "Synthetic template contains an injected escalation sentence but "
            "the allowlist guard did not flag it. The guard is vacuous — "
            "_escalation_target_sentences did not find the sentence, or "
            "_rule_body did not extract the section containing it."
        )



class TestEscalationIsRecommendedNotAsserted:
    """Task 2.1 of spec `2026-09-23-cvx-escalation-recommend-not-act`.

    Both templates must carry a rule establishing that the model may
    *recommend* a human handoff, must not assert a priority of its own, and
    must not describe an escalation as already done.

    Scoping is not optional here, and the reason is specific rather than
    stylistic: an unscoped ``"escalate_to_human" in _HARD_RULES`` passes with
    this entire rule deleted, because the triage-error rule (rule 8 / rule 9,
    from the triage-401 fix) already names that tool, and rule 3 already names
    `book`. This spec has hit that trap three times. Every assertion below runs
    against the extracted rule body only.

    Deletion is caught by ``_rule_body`` itself, which fails on a missing
    anchor with a named message — that is the mutation the task requires be run
    against each template independently.

    Follows ``TestRule3aDoesNotAssertAutoEscalation``'s pattern (F1.4); kept as
    a separate class so each rule remains independently auditable.
    """

    _ANCHOR = "Escalation is a recommendation, never an assertion"

    def _rule_body(self, prompt: str) -> str:
        assert self._ANCHOR in prompt, (
            "Escalation-recommendation rule anchor not found — the rule has "
            "been deleted or reworded, and every assertion scoped to it would "
            "otherwise pass vacuously. Per spec Task 2.1, both _HARD_RULES "
            "and SYSTEM_PROMPT_TEMPLATE must carry this rule. Expected "
            f"anchor: {self._ANCHOR!r}"
        )
        rest = prompt[prompt.index(self._ANCHOR) :]
        # The rule is last in _HARD_RULES (terminates at the closing quotes)
        # and second-to-last in SYSTEM_PROMPT_TEMPLATE (terminates at the
        # Session context block). Strip so a trailing-newline difference
        # between those two shapes cannot read as rule drift.
        for terminator in ("\n\nSession context:", '\n"""'):
            if terminator in rest:
                rest = rest[: rest.index(terminator)]
        return rest.strip()

    # ---- Property (a): recommending a handoff is permitted ----------------

    def test_hard_rules_permits_recommending_a_handoff(self):
        body = self._rule_body(_HARD_RULES)
        assert "MAY recommend" in body, (
            "_HARD_RULES escalation rule does not permit recommending a "
            "handoff. The rule must grant the recommendation, not only "
            "forbid the assertion — a rule that is purely prohibitive tells "
            "the model to go silent on P0 instead of offering help."
        )

    def test_system_prompt_permits_recommending_a_handoff(self):
        body = self._rule_body(SYSTEM_PROMPT_TEMPLATE)
        assert "MAY recommend" in body, (
            "SYSTEM_PROMPT_TEMPLATE escalation rule does not permit "
            "recommending a handoff. See the _HARD_RULES counterpart."
        )

    # ---- Property (b): must not assert a priority of its own -------------

    def test_hard_rules_forbids_asserting_its_own_priority(self):
        body = self._rule_body(_HARD_RULES)
        required = [
            "state or imply any priority, severity, or criticality of your own",
            "`triage` tool returned",
        ]
        missing = [phrase for phrase in required if phrase not in body]
        assert not missing, (
            "_HARD_RULES escalation rule no longer forbids the model "
            "asserting a priority of its own. This is the property the spec "
            "is named for. Missing:\n  " + "\n  ".join(missing)
        )

    def test_system_prompt_forbids_asserting_its_own_priority(self):
        body = self._rule_body(SYSTEM_PROMPT_TEMPLATE)
        required = [
            "state or imply any priority, severity, or criticality of your own",
            "`triage` tool returned",
        ]
        missing = [phrase for phrase in required if phrase not in body]
        assert not missing, (
            "SYSTEM_PROMPT_TEMPLATE escalation rule no longer forbids the "
            "model asserting a priority of its own. Missing:\n  "
            + "\n  ".join(missing)
        )

    # ---- Property (c): must not describe an escalation as already done ---

    def test_hard_rules_forbids_describing_escalation_as_already_done(self):
        body = self._rule_body(_HARD_RULES)
        required = [
            "as arranged, under way, or completed",
            "requested a handoff, not delivered one",
        ]
        missing = [phrase for phrase in required if phrase not in body]
        assert not missing, (
            "_HARD_RULES escalation rule no longer forbids describing an "
            "escalation as already done. Per "
            "`issues/2026-09-23-p0-roadside-dispatch-is-a-dead-letterbox/`, "
            "nothing consumes the dispatch queue until Phase B, so telling a "
            "driver help is on the way is a false claim, not a nicety. "
            "Missing:\n  " + "\n  ".join(missing)
        )

    def test_system_prompt_forbids_describing_escalation_as_already_done(self):
        body = self._rule_body(SYSTEM_PROMPT_TEMPLATE)
        required = [
            "as arranged, under way, or completed",
            "requested a handoff, not delivered one",
        ]
        missing = [phrase for phrase in required if phrase not in body]
        assert not missing, (
            "SYSTEM_PROMPT_TEMPLATE escalation rule no longer forbids "
            "describing an escalation as already done. Missing:\n  "
            + "\n  ".join(missing)
        )

    # ---- Accept 2: the two runtimes cannot drift --------------------------

    def test_rule_body_is_byte_identical_across_templates(self):
        """The bidi and text runtimes must express this rule identically.

        The rule is numbered 9 in _HARD_RULES and 10 in
        SYSTEM_PROMPT_TEMPLATE because the templates have different rule
        counts, so the comparison is deliberately of the body from the anchor
        onward, excluding the number.
        """
        hard_body = self._rule_body(_HARD_RULES)
        sys_body = self._rule_body(SYSTEM_PROMPT_TEMPLATE)
        assert hard_body == sys_body, (
            "_HARD_RULES and SYSTEM_PROMPT_TEMPLATE escalation rules have "
            "drifted. One runtime would permit what the other forbids, and "
            "the voice path is the one where a false 'help is on the way' "
            "reaches a driver.\n"
            f"_HARD_RULES ({len(hard_body)} chars): {hard_body[:200]!r}\n\n"
            f"SYSTEM_PROMPT_TEMPLATE ({len(sys_body)} chars): "
            f"{sys_body[:200]!r}"
        )

    # ---- Accept 3: the triage-error rule survives untouched ---------------

    def test_triage_error_rule_is_preserved(self):
        """The triage-401 fix's fail-closed rule must survive Task 2.1.

        Both rules constrain what the model may say about priority, so an
        edit to one is a plausible way to damage the other.
        """
        required = [
            "the classifier has NOT produced a verdict",
            "I can't verify the priority for this issue right now",
        ]
        for name, prompt in (
            ("_HARD_RULES", _HARD_RULES),
            ("SYSTEM_PROMPT_TEMPLATE", SYSTEM_PROMPT_TEMPLATE),
        ):
            missing = [phrase for phrase in required if phrase not in prompt]
            assert not missing, (
                f"{name} lost part of the triage-error fail-closed rule while "
                "adding the escalation rule. Task 2.1 Accept 3 requires it "
                "preserved unchanged. Missing:\n  " + "\n  ".join(missing)
            )

    # ---- Anti-vacuity + positive control ---------------------------------

    def test_rule_body_extraction_is_not_vacuous(self):
        """If _rule_body returned the whole prompt, every scoped assertion
        above would silently become an unscoped substring check — which is
        exactly the failure mode this class exists to avoid."""
        for name, prompt in (
            ("_HARD_RULES", _HARD_RULES),
            ("SYSTEM_PROMPT_TEMPLATE", SYSTEM_PROMPT_TEMPLATE),
        ):
            body = self._rule_body(prompt)
            assert body, f"{name}: _rule_body returned empty string."
            assert len(body) < len(prompt), (
                f"{name}: _rule_body returned the entire prompt — scoping is "
                "not happening."
            )
            assert body.startswith(self._ANCHOR), (
                f"{name}: extraction did not start at the anchor — wrong "
                f"content: {body[:100]!r}"
            )
            # The triage-error rule precedes this one in both templates. If it
            # leaks into the extracted body, the scoping is too wide and
            # property (b)'s assertions could be satisfied by that rule's
            # text rather than by this one's.
            assert "I can't verify the priority" not in body, (
                f"{name}: the triage-error rule leaked into the escalation "
                "rule body — scoping is too wide, and property (b) could "
                "pass on the wrong rule's wording."
            )

    def test_scoping_excludes_the_triage_error_rule_that_names_the_same_tool(self):
        """Positive control for the specific trap this class guards.

        A synthetic prompt whose triage-error rule names `escalate_to_human`
        but which has NO escalation rule must be rejected by the anchor
        assertion — proving an unscoped check would have passed it.
        """
        synthetic_without_rule = (
            "8. If the `triage` tool returns an ERROR, you MUST NOT "
            "(c) call `escalate_to_human` on your own initiative. You MUST "
            "reply with exactly this: \"I can't verify the priority for this "
            "issue right now\"\n"
        )
        # Unscoped, the tool name is present — a naive check would pass.
        assert "escalate_to_human" in synthetic_without_rule
        # Scoped, the rule is correctly reported missing.
        with pytest.raises(AssertionError, match="anchor not found"):
            self._rule_body(synthetic_without_rule)
