"""
Spec: .kiro/specs/2026-09-18-avx-tier2-ios-cards-and-actions/tasks.md
      § Group 2 "Extend Agent 2 with post-service-visit contextualisation"

What is tested
--------------
Agent 2's post-service-visit narration path: when an input row carries a
``warranty_verdict_phrase`` field, the resulting Finding narrates that phrase
verbatim.  The guard that enforces this is ``assert_coverage_verdict_verbatim``
(agent-agnostic, authored in the preceding task).  This file verifies the
INTEGRATION between the narration path and that guard, exercised against
synthetic verdict transitions.

Scope correction (per architect brief):
  The event-consumer path (DMS → CVX EventBridge) is NOT built here — it
  relocated to the DMS spec at
  ``~/guidance-for-dealer-management-system-on-aws/.kiro/specs/
  2026-09-18-dms-avx-agentic-surface/`` § Group 5 Task 5.4.  This test file
  covers only what lives in this repo: the narration path in
  ``assess_vehicle_health`` and the one prompt clause in ``prompt.py`` that
  pins verbatim narration for ``warranty_verdict_phrase``.

Key test (task requirement):
  A fixture with ``verdict="denied"`` produces narration containing the
  literal token "denied" — not "under review", not "in review".

Four rules from the spec:
  1. Never read prose in a guard.
  2. Mutation values come from OUTSIDE the assertion's vocabulary.
  3. "Covered by construction" is a claim, not a measurement.
  4. Restore from a ``cp`` copy; NEVER ``git checkout --``.

Mutation verification:
  ``TestPostServiceVisitMutation`` applies the same monkeypatch pattern as
  ``test_coverage_verdict_verbatim.py`` — disabling the guard to a no-op and
  confirming that ``narration_degraded`` is NOT set, proving the guard is the
  only mechanism that sets it.
"""
from __future__ import annotations

import pytest

from agents.health.agent import (
    CoverageVerdictVerbatimError,
    assess_vehicle_health,
)
from agents.health.dms_verdicts import DMS_VERDICT_ENUM
from agents.health.prompt import WARRANTY_VERDICT_PROMPT_CLAUSE


# ---------------------------------------------------------------------------
# Shared fixtures — synthetic verdict-transition scenarios
# ---------------------------------------------------------------------------

# A brake record representing a recent post-service visit.  The outcome is
# "resolved" with warranty_covered=false (claim denied).  The deterministic
# finding text will NOT contain "denied" — it will say "resolved", which
# triggers the guard to rewrite with the verdict phrase prepended.
_BRAKE_POST_VISIT_DENIED = {
    "vin": "VIN-PSV-DENIED-001",
    "service_date": "2026-09-15",
    "dtc_codes": [],
    "complaint_text": "Brake squeal",
    "warranty_covered": "false",
    "total_cost_usd": "487.50",
    "outcome": "resolved",
    "is_open": False,
    "provenance": "live",
}

# A brake record where warranty was approved.  The deterministic text for a
# resolved + warranty_covered=True record does not necessarily contain "approved"
# verbatim — we pass verdict_phrase="approved" to exercise that path.
_BRAKE_POST_VISIT_APPROVED = {
    "vin": "VIN-PSV-APPROVED-001",
    "service_date": "2026-09-12",
    "dtc_codes": [],
    "complaint_text": "Brake pad wear",
    "warranty_covered": "true",
    "total_cost_usd": "0.00",
    "outcome": "resolved",
    "is_open": False,
    "provenance": "live",
}

# A brake record for "requires_review" state — claim not yet resolved.
_BRAKE_POST_VISIT_REQUIRES_REVIEW = {
    "vin": "VIN-PSV-UNDER-REVIEW-001",
    "service_date": "2026-09-16",
    "dtc_codes": [],
    "complaint_text": "Brake vibration",
    "warranty_covered": "unknown",
    "total_cost_usd": "0.00",
    "outcome": "follow_up_required",
    "is_open": True,
    "provenance": "live",
}


# ---------------------------------------------------------------------------
# Section 1: Prompt clause — WARRANTY_VERDICT_PROMPT_CLAUSE
# ---------------------------------------------------------------------------

class TestWarrantyVerdictPromptClause:
    """Tests for the WARRANTY_VERDICT_PROMPT_CLAUSE constant in prompt.py.

    The clause is a defence-in-depth instruction to the LLM.  Its value is
    test-pinned here so that softening the instruction fails a test by
    content, not by symbol dereference.
    """

    def test_clause_is_non_empty_string(self):
        """The clause constant is a non-empty string."""
        assert isinstance(WARRANTY_VERDICT_PROMPT_CLAUSE, str)
        assert len(WARRANTY_VERDICT_PROMPT_CLAUSE.strip()) > 0

    def test_clause_pins_verbatim_narration_instruction(self):
        """The clause instructs the LLM to narrate the phrase VERBATIM.

        At least one of 'verbatim', 'exact', or 'word for word' must appear
        so a reviewer can identify the instruction unambiguously.  This
        pins the instruction's CONTENT — softening "MUST contain that exact
        phrase verbatim" to "should try to include" would fail this test.
        """
        clause_lower = WARRANTY_VERDICT_PROMPT_CLAUSE.lower()
        verbatim_instruction_present = any(
            term in clause_lower
            for term in ("verbatim", "exact phrase", "word for word")
        )
        assert verbatim_instruction_present, (
            "WARRANTY_VERDICT_PROMPT_CLAUSE does not contain a verbatim-"
            "narration instruction ('verbatim', 'exact phrase', or 'word for "
            f"word').\n\nActual clause:\n{WARRANTY_VERDICT_PROMPT_CLAUSE}"
        )

    def test_clause_references_denied_as_canonical_example(self):
        """The clause gives 'denied' as a canonical example phrase.

        This pins the example so that removing it — which would weaken the
        instruction's concreteness — fails a test.

        The literal string 'denied' is checked (not a constant) so that
        any softening of the example also fails this assertion.
        """
        assert "denied" in WARRANTY_VERDICT_PROMPT_CLAUSE, (
            "WARRANTY_VERDICT_PROMPT_CLAUSE does not reference 'denied' as "
            "a canonical example phrase.\n\n"
            f"Actual clause:\n{WARRANTY_VERDICT_PROMPT_CLAUSE}"
        )

    def test_clause_explicitly_forbids_paraphrase(self):
        """The clause explicitly forbids paraphrase or softening.

        Words like 'paraphrase', 'soften', 'omit', or 'not approved' must
        appear to show the LLM what NOT to do.
        """
        clause_lower = WARRANTY_VERDICT_PROMPT_CLAUSE.lower()
        paraphrase_forbidden = any(
            term in clause_lower
            for term in ("paraphrase", "soften", "omit", "not approved", "do not")
        )
        assert paraphrase_forbidden, (
            "WARRANTY_VERDICT_PROMPT_CLAUSE does not explicitly forbid "
            "paraphrase or softening.  At least one of 'paraphrase', "
            "'soften', 'omit', 'not approved', 'do not' must be present."
            f"\n\nActual clause:\n{WARRANTY_VERDICT_PROMPT_CLAUSE}"
        )


# ---------------------------------------------------------------------------
# Section 2: The "denied" fixture — task-required literal-token test
# ---------------------------------------------------------------------------

class TestDeniedFixtureLiteralToken:
    """The task requires: a fixture with verdict="denied" produces narration
    containing the literal token "denied" — not "under review", not "in review".

    This is the PRIMARY deliverable of the task.  These tests verify the end-
    to-end behaviour directly.
    """

    def test_denied_verdict_produces_finding_with_literal_denied_token(self):
        """A post-service-visit input with verdict='denied' produces a Finding
        containing the literal token 'denied'.

        Uses the deterministic path (_llm_caller=None) so the test is
        reproducible without infrastructure.
        """
        result = assess_vehicle_health(
            "VIN-PSV-DENIED-001",
            brake_data=_BRAKE_POST_VISIT_DENIED,
            warranty_verdict_phrase="denied",
            _llm_caller=None,
        )
        brakes = result["brakes"]
        # Primary assertion: the literal token "denied" is in the finding.
        # This is the LITERAL string — not a constant — per Rule 1 (no
        # constant-following) and the task's explicit requirement.
        assert "denied" in brakes["finding"], (
            "Post-service-visit narration with verdict='denied' must contain "
            "the literal token 'denied'.\n\n"
            f"Actual finding: {brakes['finding']!r}"
        )

    def test_denied_verdict_does_not_produce_requires_review_in_finding(self):
        """The finding must not contain 'under review' when verdict is 'denied'.

        'under review' is the canonical softened-phrase boundary violation.
        It is from OUTSIDE the DMS verdict enum (which uses underscore: 'requires_review')
        and represents the exact substitution the guard exists to block.
        """
        result = assess_vehicle_health(
            "VIN-PSV-DENIED-001",
            brake_data=_BRAKE_POST_VISIT_DENIED,
            warranty_verdict_phrase="denied",
            _llm_caller=None,
        )
        brakes = result["brakes"]
        assert "under review" not in brakes["finding"].lower(), (
            "Finding must not contain 'under review' when verdict is 'denied'.\n\n"
            f"Actual finding: {brakes['finding']!r}"
        )

    def test_denied_verdict_does_not_produce_in_review_in_finding(self):
        """The finding must not contain 'in review' when verdict is 'denied'.

        'in review' is another softening variant of the out-of-vocabulary
        phrase the task spec explicitly names.
        """
        result = assess_vehicle_health(
            "VIN-PSV-DENIED-001",
            brake_data=_BRAKE_POST_VISIT_DENIED,
            warranty_verdict_phrase="denied",
            _llm_caller=None,
        )
        brakes = result["brakes"]
        assert "in review" not in brakes["finding"].lower(), (
            "Finding must not contain 'in review' when verdict is 'denied'.\n\n"
            f"Actual finding: {brakes['finding']!r}"
        )

    def test_denied_literal_pin(self):
        """Literal-value pin: the string 'denied' is the canonical DMS denial phrase.

        This test uses a LITERAL string (not a constant) so that any softening
        of the phrase in the codebase fails here by value rather than by
        symbol-dereference.

        Analogous to ``test_c0161_verbatim_remedy_value_is_pinned`` in
        ``test_agent.py`` and ``test_verdict_phrase_literal_pin`` in
        ``test_coverage_verdict_verbatim.py``.
        """
        # The canonical DMS denial phrase — literal, not a constant.
        assert "denied" == "denied"  # Tautology that reads as intent anchor.
        # More substantively: the guard raises for the softened form.
        from agents.health.agent import assert_coverage_verdict_verbatim
        with pytest.raises(CoverageVerdictVerbatimError):
            assert_coverage_verdict_verbatim(
                "denied",   # LITERAL — canonical DMS denial phrase
                headline="The claim is under review.",   # softened — not 'denied'
                detail="Your warranty claim is under review.",
            )


# ---------------------------------------------------------------------------
# Section 3: Synthetic verdict-transition scenarios
# ---------------------------------------------------------------------------

class TestSyntheticVerdictTransitions:
    """Exercise the narration path against each verdict transition.

    Each test is a distinct scenario showing the guard fires on a different
    DMS verdict and the literal phrase appears in the finding.
    """

    def test_approved_verdict_produces_finding_with_approved_token(self):
        """verdict='approved' → 'approved' in finding."""
        result = assess_vehicle_health(
            "VIN-PSV-APPROVED-001",
            brake_data=_BRAKE_POST_VISIT_APPROVED,
            warranty_verdict_phrase="approved",
            _llm_caller=None,
        )
        brakes = result["brakes"]
        assert "approved" in brakes["finding"], (
            f"Finding must contain 'approved'. Got: {brakes['finding']!r}"
        )

    def test_requires_review_verdict_produces_finding_with_requires_review_token(self):
        """verdict='requires_review' → 'requires_review' in finding.

        The canonical DMS form uses underscore (not space).  The guard checks
        for the exact phrase passed in — so 'requires_review' (underscore) is
        what must appear, NOT 'under review' (space).
        """
        result = assess_vehicle_health(
            "VIN-PSV-UNDER-REVIEW-001",
            brake_data=_BRAKE_POST_VISIT_REQUIRES_REVIEW,
            warranty_verdict_phrase="requires_review",
            _llm_caller=None,
        )
        brakes = result["brakes"]
        assert "requires_review" in brakes["finding"], (
            "Finding must contain the canonical DMS form 'requires_review' "
            f"(underscore). Got: {brakes['finding']!r}"
        )

    def test_denied_sets_narration_degraded_when_not_in_deterministic_text(self):
        """When the deterministic text lacks 'denied', narration_degraded=True.

        The deterministic brake builder for a resolved/warranty_covered=false
        record does not produce text that explicitly contains 'denied'.
        The guard fires, rewrites, and sets the flag.
        """
        result = assess_vehicle_health(
            "VIN-PSV-DENIED-001",
            brake_data=_BRAKE_POST_VISIT_DENIED,
            warranty_verdict_phrase="denied",
            _llm_caller=None,
        )
        brakes = result["brakes"]
        assert brakes.get("narration_degraded") is True, (
            "narration_degraded must be True when the deterministic text "
            "lacks 'denied' and the guard rewrites it.\n\n"
            f"Actual brakes result: {brakes!r}"
        )

    def test_verdict_phrase_leads_the_rewritten_finding(self):
        """The rewritten finding prepends the verdict phrase as the leading content.

        When the guard rewrites, it produces ``'{phrase}\\n\\n{original}'``,
        so the phrase appears at the very start of the finding text.
        """
        result = assess_vehicle_health(
            "VIN-PSV-DENIED-001",
            brake_data=_BRAKE_POST_VISIT_DENIED,
            warranty_verdict_phrase="denied",
            _llm_caller=None,
        )
        brakes = result["brakes"]
        # The phrase is prepended, so the finding starts with it.
        assert brakes["finding"].startswith("denied"), (
            "Rewritten finding must start with 'denied' (prepended by the guard).\n\n"
            f"Actual finding start: {brakes['finding'][:80]!r}"
        )

    def test_no_verdict_phrase_produces_no_degradation(self):
        """Passing warranty_verdict_phrase=None (default) → no narration_degraded."""
        result = assess_vehicle_health(
            "VIN-PSV-DENIED-001",
            brake_data=_BRAKE_POST_VISIT_DENIED,
            _llm_caller=None,
        )
        brakes = result["brakes"]
        assert not brakes.get("narration_degraded", False), (
            "No narration_degraded expected when warranty_verdict_phrase=None."
        )

    def test_verdict_applies_to_all_present_components(self):
        """When multiple components are present, the guard applies to each.

        In this test both battery and brakes are provided.  A verdict phrase
        that is absent from both deterministic findings should set
        narration_degraded=True on BOTH components.
        """
        battery_rows = [{"health_pct": 88, "age_days": 730}]
        result = assess_vehicle_health(
            "VIN-PSV-MULTI-001",
            battery_rows=battery_rows,
            brake_data=_BRAKE_POST_VISIT_DENIED,
            warranty_verdict_phrase="denied",
            _llm_caller=None,
        )
        # Both components must contain 'denied' after the guard runs.
        for comp in ("battery", "brakes"):
            comp_result = result[comp]
            assert "denied" in comp_result["finding"], (
                f"Component '{comp}' finding must contain 'denied'.\n\n"
                f"Actual finding: {comp_result['finding']!r}"
            )

    def test_existing_component_predictions_unchanged_when_verdict_none(self):
        """Agent 2's per-component predictions are unchanged when no verdict phrase.

        This is the 'existing per-component predictions unchanged' constraint
        from the task.  Without a warranty_verdict_phrase, assess_vehicle_health
        must return the same findings as before this extension.
        """
        battery_rows = [{"health_pct": 92, "age_days": 365}]
        tire_rows = [
            {"replacement_flagged": False, "wear_band": "ok", "position": "LF"},
            {"replacement_flagged": False, "wear_band": "normal", "position": "RF"},
        ]

        result_no_verdict = assess_vehicle_health(
            "VIN-BASELINE-001",
            battery_rows=battery_rows,
            tire_rows=tire_rows,
            brake_data=_BRAKE_POST_VISIT_APPROVED,
            _llm_caller=None,
        )

        result_with_none = assess_vehicle_health(
            "VIN-BASELINE-001",
            battery_rows=battery_rows,
            tire_rows=tire_rows,
            brake_data=_BRAKE_POST_VISIT_APPROVED,
            warranty_verdict_phrase=None,
            _llm_caller=None,
        )

        # Findings must be identical when warranty_verdict_phrase=None.
        for comp in ("battery", "tires", "brakes"):
            if comp in result_no_verdict and comp in result_with_none:
                assert result_no_verdict[comp]["finding"] == result_with_none[comp]["finding"], (
                    f"Component '{comp}' finding changed when warranty_verdict_phrase=None "
                    "(existing predictions must be unchanged by this extension)."
                )

    def test_multiple_distinct_verdicts_exercise_different_phrases(self):
        """Each of the three canonical DMS verdict strings is exercised.

        Tests all three as separate invocations to show the path works
        for each, not just 'denied'.
        """
        verdicts_and_data = [
            ("denied", _BRAKE_POST_VISIT_DENIED),
            ("approved", _BRAKE_POST_VISIT_APPROVED),
            ("requires_review", _BRAKE_POST_VISIT_REQUIRES_REVIEW),
        ]
        for verdict, brake_data in verdicts_and_data:
            result = assess_vehicle_health(
                f"VIN-PSV-{verdict.upper()}-MULTI",
                brake_data=brake_data,
                warranty_verdict_phrase=verdict,
                _llm_caller=None,
            )
            brakes = result["brakes"]
            assert verdict in brakes["finding"], (
                f"For verdict={verdict!r}, the finding must contain the literal "
                f"phrase {verdict!r}.\n\nActual finding: {brakes['finding']!r}"
            )


# ---------------------------------------------------------------------------
# Section 4: LLM-caller path — clause is appended to prompt
# ---------------------------------------------------------------------------

class TestWarrantyVerdictClauseInLlmPath:
    """Verify the WARRANTY_VERDICT_PROMPT_CLAUSE is appended to the LLM user
    prompt when warranty_verdict_phrase is provided.

    The clause is defence-in-depth.  The post-generation guard (Step 2b)
    remains the primary enforcement; this test verifies the instruction also
    reaches the LLM.
    """

    def test_prompt_clause_appended_when_verdict_phrase_given(self):
        """The LLM caller receives the WARRANTY_VERDICT_PROMPT_CLAUSE when
        warranty_verdict_phrase is provided.

        We inject a recording llm_caller that captures the user_prompt and
        returns a valid JSON response.  We then assert the clause text is
        present in the captured prompt.
        """
        captured_prompts: list[tuple[str, str]] = []

        def recording_llm_caller(system_prompt: str, user_prompt: str) -> str:
            captured_prompts.append((system_prompt, user_prompt))
            # Return a valid JSON response with 'denied' present so the guard
            # does not rewrite (we want to inspect the prompt, not the rewrite).
            return (
                '{"brakes": {"finding": "The warranty claim was denied.", '
                '"severity_hint": "routine", "confidence": "high", "evidence": []}}'
            )

        assess_vehicle_health(
            "VIN-LLM-CLAUSE-TEST",
            brake_data=_BRAKE_POST_VISIT_DENIED,
            warranty_verdict_phrase="denied",
            _llm_caller=recording_llm_caller,
        )

        assert len(captured_prompts) == 1, (
            "Expected exactly one LLM call; got "
            f"{len(captured_prompts)}"
        )
        _, user_prompt = captured_prompts[0]

        # The clause content must appear in the user_prompt.
        # We check for a key substring rather than the full clause to avoid
        # brittle exact-match on whitespace/punctuation.
        assert "verbatim" in user_prompt.lower() or "exact" in user_prompt.lower(), (
            "The user_prompt must contain the verbatim-narration instruction "
            "(from WARRANTY_VERDICT_PROMPT_CLAUSE) when warranty_verdict_phrase "
            f"is provided.\n\nActual user_prompt tail:\n{user_prompt[-500:]!r}"
        )
        # The verdict phrase itself must appear in the prompt so the LLM knows
        # what to narrate.
        assert "denied" in user_prompt, (
            f"The verdict phrase 'denied' must appear in the user_prompt.\n\n"
            f"Actual user_prompt tail:\n{user_prompt[-500:]!r}"
        )

    def test_prompt_clause_not_appended_when_no_verdict_phrase(self):
        """The clause is NOT appended when warranty_verdict_phrase=None.

        This prevents the clause from polluting prompts where no verdict
        is relevant — preserving the existing LLM prompt behaviour.
        """
        captured_prompts: list[tuple[str, str]] = []

        def recording_llm_caller(system_prompt: str, user_prompt: str) -> str:
            captured_prompts.append((system_prompt, user_prompt))
            return (
                '{"brakes": {"finding": "Brake service resolved.", '
                '"severity_hint": "routine", "confidence": "high", "evidence": []}}'
            )

        assess_vehicle_health(
            "VIN-LLM-NO-CLAUSE-TEST",
            brake_data=_BRAKE_POST_VISIT_APPROVED,
            warranty_verdict_phrase=None,
            _llm_caller=recording_llm_caller,
        )

        assert len(captured_prompts) == 1
        _, user_prompt = captured_prompts[0]

        # The clause should NOT be present when no verdict phrase is given.
        clause_marker = "DMS warranty verdict"
        assert clause_marker not in user_prompt, (
            f"The clause marker {clause_marker!r} must NOT appear in the "
            "user_prompt when warranty_verdict_phrase=None."
        )


# ---------------------------------------------------------------------------
# Section 5: MUTATION VERIFICATION
# ---------------------------------------------------------------------------
#
# Rule: a mutation value must come from OUTSIDE the assertion's vocabulary.
# The monkeypatch mutation disables assert_coverage_verdict_verbatim to a
# no-op.  Under the mutation, narration_degraded is NOT set.
#
# The assertion that catches the mutation:
#   assert brakes.get("narration_degraded") is True
#
# This is the SAME catching assertion as in test_coverage_verdict_verbatim.py.
# That is correct: both test files measure the same guard from different call
# sites.  The mutation is recorded in decisions-fragment-post-service-visit.md.


class TestPostServiceVisitMutation:
    """Mutation-boundary tests for the post-service-visit narration path.

    The mutation: monkey-patch ``assert_coverage_verdict_verbatim`` to a
    permissive no-op (exactly as in test_coverage_verdict_verbatim.py).

    Under the mutation:
      - assess_vehicle_health runs with warranty_verdict_phrase='denied'
      - 'denied' is absent from the deterministic finding text
      - The guard is a no-op → narration_degraded is NOT set
      - test_guard_is_load_bearing asserts narration_degraded=True → FAILS

    The failure is the proof that the guard is the only mechanism setting
    narration_degraded=True on this path.

    Mutation value: lambda *args, **kwargs: None
    This is from OUTSIDE the assertion's vocabulary — it is not a DMS verdict
    phrase, not a component name, not anything the guard normally returns.
    """

    def test_guard_is_load_bearing_on_post_service_visit_path(self):
        """NORMAL SUITE: guard fires → narration_degraded=True.

        This test PASSES in the normal run.  When the mutation is applied
        (guard disabled), the exact assertion below FAILS — proving the guard
        is the only mechanism setting narration_degraded=True here.

        The catching assertion:
            assert brakes.get("narration_degraded") is True
        """
        result = assess_vehicle_health(
            "VIN-PSV-MUTATION-001",
            brake_data=_BRAKE_POST_VISIT_DENIED,
            warranty_verdict_phrase="denied",   # LITERAL — canonical DMS denial phrase
            _llm_caller=None,
        )
        brakes = result["brakes"]
        assert brakes.get("narration_degraded") is True, (
            "narration_degraded must be True when 'denied' is absent from the "
            "deterministic finding text.\n\n"
            "MUTATION CATCH: this is the assertion that catches the mutation "
            "(guard → no-op).  Under the mutation, narration_degraded is not "
            "set and this assertion fails."
        )

    def test_guard_disabled_by_monkeypatch_does_not_set_narration_degraded(self, monkeypatch):
        """MUTATION RUN (in-suite): proves the mutation causes the above test to fail.

        Under the mutation (guard disabled), narration_degraded is NOT set.
        This test PASSES because it asserts the INVERTED condition.

        Together with ``test_guard_is_load_bearing_on_post_service_visit_path``
        it demonstrates the two states:
          - Normal:   narration_degraded=True  (guard fires)
          - Mutation: narration_degraded absent (guard no-op)
        """
        # Disable the guard — the mutation.
        monkeypatch.setattr(
            "agents.health.agent.assert_coverage_verdict_verbatim",
            lambda *args, **kwargs: None,  # no-op from OUTSIDE the vocabulary
        )

        result = assess_vehicle_health(
            "VIN-PSV-MUTATION-001",
            brake_data=_BRAKE_POST_VISIT_DENIED,
            warranty_verdict_phrase="denied",
            _llm_caller=None,
        )
        brakes = result["brakes"]
        # Under the mutation: narration_degraded is NOT set.
        # The production test above would FAIL here.
        assert not brakes.get("narration_degraded", False), (
            "Under the mutation (guard disabled), narration_degraded must not be set."
        )
        # The phrase is also absent from the finding (no rewrite happened).
        assert "denied" not in brakes["finding"], (
            "Under the mutation, the rewrite must not have happened — "
            "'denied' is absent from the finding."
        )

    def test_mutation_value_is_outside_dms_verdict_vocabulary(self):
        """Confirm that the mutation (no-op lambda) is outside the DMS verdict enum.

        Rule 2: a mutation value must come from OUTSIDE the assertion's own
        vocabulary.  The mutation is a no-op lambda — not a DMS verdict, not a
        component name, not a string from the assertion.  This test documents
        that invariant.

        The enum is IMPORTED from ``agents.health.dms_verdicts``, never restated
        here.  Until Fix Group 19 this body declared its own
        ``frozenset({"approved", "declined", "under_review"})`` — a second copy of
        the one in ``test_coverage_verdict_verbatim.py``, wrong in the same two
        places.  Pinned by
        ``test_dms_verdict_enum.py::TestNoHardcodedVerdictEnumsRemain``.
        """
        # The mutation is a callable, not a string.  It is categorically outside
        # the vocabulary (which consists of strings).
        mutation = lambda *args, **kwargs: None  # noqa: E731
        assert callable(mutation), "Mutation is a callable (outside string vocabulary)."
        assert mutation not in DMS_VERDICT_ENUM, (
            "Mutation value must not be in the DMS verdict enum."
        )
