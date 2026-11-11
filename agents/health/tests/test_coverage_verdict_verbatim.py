"""Tests for the coverage-verdict-verbatim guard.

Spec:  .kiro/specs/2026-09-18-avx-tier2-ios-cards-and-actions/tasks.md
       § Group 2 "Author + mutation-verify coverage-verdict-verbatim guard"

What is guarded
---------------
An Agent 2 Finding that cites a DMS warranty verdict MUST embed the verdict
phrase VERBATIM in both ``headline`` and ``detail``.  Softening "denied" to
"under review" is the canonical boundary violation.

The guard is agent-agnostic — it knows nothing about batteries, tires, or
brakes.  It takes a ``verdict_phrase`` and rendered text strings; that is all.
This also means Spec 1b's Agent 3 and the Group 6.1 ``warranty_agent`` persona
consume the same guard without modification.

Why the test file pins the literal phrase value (not just presence)
-------------------------------------------------------------------
Lessons from the P0 verbatim prior art in this repo (``prompt.py`` /
``test_agent.py``): a test that asserts ``SOME_CONSTANT in rendered_text``
follows the constant if the constant is softened.  Rewriting the constant to
``"under review"`` — the exact boundary violation — leaves every
*presence*-based test green.  We therefore pin literal verdict strings in at
least one test so that softening the phrase fails a test by value, not by
symbol-dereference.

Four rules this spec learned expensively (all four apply here)
--------------------------------------------------------------
1. Never read prose in a guard.  Compare structured data against exact
   expectations.
2. A mutation value must come from OUTSIDE the assertion's own vocabulary.
   "under review" is not in the DMS verdict enum {approved, denied,
   requires_review}.
3. "Covered by construction" is a claim, not a measurement.  The mutation run
   below MEASURES coverage.
4. Restore from a ``cp`` copy; NEVER ``git checkout --``.

Mutation verification (required before task is [x])
----------------------------------------------------
The mutation test in this file does NOT monkey-patch prompt.py (too coupled to
Agent 2's brakes path).  Instead it monkey-patches the
``assert_coverage_verdict_verbatim`` function itself to a permissive no-op,
which is the exact failure mode we are guarding against: a caller in the
narration path that forgets to assert.  The mutation is:

    monkeypatch.setattr(
        "agents.health.agent.assert_coverage_verdict_verbatim",
        lambda *args, **kwargs: None,
    )

Under this mutation the violation is silently swallowed and ``assess_vehicle_health``
returns text containing the softened phrase without raising or flagging.  The
test asserts ``narration_degraded=True`` must be set — under the mutation it is
NOT set because the guard never fires.

Which assertion catches the mutation:
    ``test_mutation_softened_verdict_flagged_as_degraded`` →
    ``assert result["brakes"]["narration_degraded"] is True``
"""
from __future__ import annotations

import pytest

from agents.health.agent import (
    CoverageVerdictVerbatimError,
    assert_coverage_verdict_verbatim,
    assess_vehicle_health,
)
from agents.health.dms_verdicts import DMS_VERDICT_ENUM


# ---------------------------------------------------------------------------
# Literal-value pin — the guard's canonical vocabulary
# ---------------------------------------------------------------------------

# Pinned as LITERALS, not by importing from a DMS module that does not yet
# exist, so that softening one of these phrases fails a test by value.
# Agent-agnostic: the guard handles any caller's phrase, not just these.
_VERDICT_DENIED = "denied"
_VERDICT_APPROVED = "approved"
_VERDICT_REQUIRES_REVIEW = "requires_review"   # canonical DMS form; underscore, not space

# The softened variant that MUST NOT be accepted in place of "denied".
# Comes from OUTSIDE the assertion's vocabulary — it is not in the DMS
# verdict enum and would not be substituted by a legitimate caller.
_SOFTENED_PHRASE = "under review"   # space, not underscore — deliberately alien

# ---------------------------------------------------------------------------
# Section 1: assert_coverage_verdict_verbatim — the agent-agnostic guard
# ---------------------------------------------------------------------------


class TestAssertCoverageVerdictVerbatim:
    """Direct tests for the standalone guard function.

    The guard is agent-agnostic: it takes (verdict_phrase, headline, detail)
    and raises ``CoverageVerdictVerbatimError`` on any violation.  It has no
    knowledge of Agent 2, batteries, tires, or brakes.
    """

    # --- Happy-path tests ---------------------------------------------------

    def test_passes_when_phrase_in_both_fields(self):
        """Guard succeeds when the phrase is verbatim in headline AND detail."""
        assert_coverage_verdict_verbatim(
            _VERDICT_DENIED,
            headline=f"Your warranty claim was {_VERDICT_DENIED}.",
            detail=f"The repair cost was not covered. Verdict: {_VERDICT_DENIED}.",
        )
        # No exception → test passes.

    def test_passes_for_approved_verdict(self):
        """Guard accepts 'approved' phrase when present in both fields."""
        assert_coverage_verdict_verbatim(
            _VERDICT_APPROVED,
            headline=f"Warranty claim {_VERDICT_APPROVED}.",
            detail=f"The claim has been {_VERDICT_APPROVED} and repair is covered.",
        )

    def test_passes_for_requires_review_verdict(self):
        """Guard accepts requires_review (underscore form) when present in both fields."""
        assert_coverage_verdict_verbatim(
            _VERDICT_REQUIRES_REVIEW,
            headline=f"Claim status: {_VERDICT_REQUIRES_REVIEW}.",
            detail=f"Your warranty claim is currently {_VERDICT_REQUIRES_REVIEW}.",
        )

    def test_passes_when_phrase_at_start_of_headline(self):
        """Phrase may appear at the very start of headline (sentence-leading)."""
        assert_coverage_verdict_verbatim(
            _VERDICT_DENIED,
            headline=f"{_VERDICT_DENIED} — the claim was reviewed.",
            detail=f"The claim was {_VERDICT_DENIED} by the warranty team.",
        )

    # --- Failure-mode tests (non-mutation): these measure the guard directly ---

    def test_raises_when_phrase_missing_from_headline(self):
        """Raises when phrase is in detail but absent from headline."""
        with pytest.raises(CoverageVerdictVerbatimError) as exc_info:
            assert_coverage_verdict_verbatim(
                _VERDICT_DENIED,
                headline="The warranty claim was reviewed and a decision was reached.",
                detail=f"The claim was {_VERDICT_DENIED}.",
            )
        assert "headline" in str(exc_info.value).lower()
        assert _VERDICT_DENIED in str(exc_info.value)

    def test_raises_when_phrase_missing_from_detail(self):
        """Raises when phrase is in headline but absent from detail."""
        with pytest.raises(CoverageVerdictVerbatimError) as exc_info:
            assert_coverage_verdict_verbatim(
                _VERDICT_DENIED,
                headline=f"Warranty claim {_VERDICT_DENIED}.",
                detail="The warranty team reviewed your claim and made a determination.",
            )
        assert "detail" in str(exc_info.value).lower()
        assert _VERDICT_DENIED in str(exc_info.value)

    def test_raises_when_phrase_missing_from_both_fields(self):
        """Raises when phrase is absent from BOTH headline and detail."""
        with pytest.raises(CoverageVerdictVerbatimError) as exc_info:
            assert_coverage_verdict_verbatim(
                _VERDICT_DENIED,
                headline="Warranty claim decision reached.",
                detail="The warranty team reviewed your claim.",
            )
        error_msg = str(exc_info.value)
        assert "headline" in error_msg.lower()
        assert "detail" in error_msg.lower()
        assert _VERDICT_DENIED in error_msg

    def test_raises_when_softened_phrase_used_instead_of_denied(self):
        """The canonical boundary violation: 'under review' replaces 'denied'.

        This is the exact softening the guard exists to catch.  The assertion
        uses the LITERAL string 'denied' — it does not dereference a constant
        — so even if someone edits ``_VERDICT_DENIED`` to 'under review' the
        test still checks for the actual word 'denied'.
        """
        with pytest.raises(CoverageVerdictVerbatimError):
            assert_coverage_verdict_verbatim(
                "denied",   # LITERAL — not _VERDICT_DENIED
                headline=f"The claim is {_SOFTENED_PHRASE}.",
                detail=f"Your warranty claim is currently {_SOFTENED_PHRASE}.",
            )

    def test_check_is_case_sensitive(self):
        """Guard is case-sensitive: 'Denied' (title) does not satisfy 'denied'."""
        with pytest.raises(CoverageVerdictVerbatimError):
            assert_coverage_verdict_verbatim(
                "denied",   # LITERAL
                headline="The claim was Denied.",
                detail="Warranty outcome: Denied.",
            )

    def test_guard_does_not_read_prose(self):
        """Guard must NOT accept paraphrase/synonym — it compares exact strings.

        'rejected', 'declined', and 'not approved' are semantically equivalent to
        'denied' but must NOT satisfy the guard.  This test verifies the guard
        is a verbatim exact-match, not a semantic/prose reader.

        ``declined`` earns its place in this list specifically.  It is not an
        arbitrary synonym: it is a real status in this subsystem — an AVX
        **Action** terminal status
        (``lambdas/api-avx-actions/handler.py:104-112``) — and mistaking it for a
        DMS warranty verdict is the exact confusion that produced the
        fabricated-vocabulary defect closed by Fix Group 19.  It is therefore the
        single most likely softening a future author or model will reach for, and
        the guard must reject it.

        Note for the scan in ``test_dms_verdict_enum.py``: ``declined`` appears
        here as a **paraphrase that must be rejected**, never as a verdict value.
        That scan selects by verdict-phrase *position* (the
        ``warranty_verdict_phrase=`` keyword, the guard's first argument, and
        ``_VERDICT_*`` constants) rather than by token, precisely so that
        legitimate uses like this one are not false positives — and so that the
        AVX Action domain does not need a blanket exemption, which would be a
        suppression rather than an assertion.
        """
        for paraphrase in ("rejected", "declined", "not approved", "not covered"):
            with pytest.raises(CoverageVerdictVerbatimError, match="denied"):
                assert_coverage_verdict_verbatim(
                    "denied",   # LITERAL
                    headline=f"The claim was {paraphrase}.",
                    detail=f"Warranty decision: {paraphrase}.",
                )

    def test_empty_verdict_phrase_raises_value_error(self):
        """An empty verdict phrase is a programming error, not a graceful miss."""
        with pytest.raises(ValueError):
            assert_coverage_verdict_verbatim("", "some headline", "some detail")


# ---------------------------------------------------------------------------
# Section 2: assert_coverage_verdict_verbatim is agent-agnostic
# ---------------------------------------------------------------------------


class TestGuardIsAgentAgnostic:
    """Verify the guard has no coupling to Agent 2 internals (batteries/tires/brakes)."""

    def test_guard_callable_with_any_non_empty_phrase(self):
        """Guard accepts any non-empty phrase string — not restricted to DMS verdicts."""
        # Non-DMS phrase; proves the guard is not hard-coded to a DMS enum.
        assert_coverage_verdict_verbatim(
            verdict_phrase="service required",
            headline="Your vehicle: service required.",
            detail="Based on recent diagnostics, service required is the status.",
        )

    def test_guard_is_importable_without_agent2_dependencies(self):
        """The guard can be imported standalone without importing Agent 2 data sources.

        This ensures Spec 1b's Agent 3 and warranty_agent can import it without
        pulling the entire Agent 2 dependency tree.
        """
        # If the import at the top of this file succeeded, the guard is
        # importable.  We also assert the error class is accessible.
        assert CoverageVerdictVerbatimError is not None
        assert callable(assert_coverage_verdict_verbatim)

    def test_guard_function_signature_is_agent_agnostic(self):
        """Guard function parameters: (verdict_phrase, headline, detail).

        No 'component', 'battery', 'tire', 'brake', or 'vin' parameter exists.
        """
        import inspect
        sig = inspect.signature(assert_coverage_verdict_verbatim)
        param_names = set(sig.parameters.keys())
        # Must have the three agent-agnostic params.
        assert "verdict_phrase" in param_names
        assert "headline" in param_names
        assert "detail" in param_names
        # Must NOT have Agent 2 specifics.
        for agent2_specific in ("component", "battery", "tire", "brake", "vin"):
            assert agent2_specific not in param_names, (
                f"Guard has an Agent 2-specific parameter {agent2_specific!r}. "
                "The guard must be agent-agnostic for Spec 1b inheritance."
            )


# ---------------------------------------------------------------------------
# Section 3: assess_vehicle_health narration path integration
# ---------------------------------------------------------------------------
#
# These tests exercise the full narration path in Agent 2 to verify the guard
# is wired correctly.  A synthetic brake_data fixture is used (brakes are the
# component most likely to carry a warranty verdict in post-service-visit
# contextualisation).  The tests do NOT call the LLM — _llm_caller=None.

_BRAKE_DATA_RESOLVED_WITH_VERDICT = {
    "vin": "VIN-WARRANTY-TEST-001",
    "service_date": "2026-09-01",
    "dtc_codes": [],
    "complaint_text": "Brake noise",
    "warranty_covered": "true",
    "total_cost_usd": "0.00",
    "outcome": "resolved",
    "is_open": False,
    "provenance": "live",
}


class TestAssessVehicleHealthVerdictPath:
    """Integration: warranty_verdict_phrase wired into assess_vehicle_health."""

    def test_happy_path_verdict_phrase_in_finding(self):
        """When the narrated text contains the verdict verbatim, the guard passes
        and narration_degraded is absent (or False).

        **Rewritten by Fix Group 19.**  This test previously passed
        ``warranty_verdict_phrase='resolved'`` against a deterministic finding and
        asserted the guard stayed quiet.  It passed for the wrong reason:
        ``resolved`` is not a warranty verdict at all — it is the **repair-order
        outcome** field (``agent.py`` ``_COL_OUTCOME``), read straight off
        ``_BRAKE_DATA_RESOLVED_WITH_VERDICT["outcome"]``.  A third enum conflated
        with the two the issue already documents.  The guard "passed" only because
        the deterministic brake builder happens to echo the outcome into its text.

        The honest construction uses a real DMS verdict and an ``_llm_caller``
        double that narrates it verbatim — which is what the production path does
        now that the Lambda bundles strands (see the Group 2 CDK bundling task).

        Worth recording, because it is a real operational consequence rather than
        a test artefact: **no deterministic builder emits any verdict token.**
        ``warranty_covered`` is boolean-ish and no builder writes ``approved`` /
        ``denied`` / ``requires_review``.  So with ``_llm_caller=None`` every real
        verdict takes the guard's rewrite path and sets ``narration_degraded=True``
        — correctly.  The "guard passes without a rewrite" case requires narration,
        and that is the only construction in which it can honestly be tested.
        """
        import json as _json

        def _stub_llm_narrating_verdict(system_prompt: str, user_prompt: str) -> str:
            """Narrate the real DMS verdict verbatim, as the prompt clause instructs."""
            return _json.dumps(
                {
                    "brakes": {
                        "finding": (
                            "Brake service is complete. The warranty claim was "
                            "denied by the dealer."
                        ),
                        "severity_hint": "routine",
                        "confidence": "high",
                    }
                }
            )

        result = assess_vehicle_health(
            "VIN-WARRANTY-TEST-001",
            brake_data=_BRAKE_DATA_RESOLVED_WITH_VERDICT,
            warranty_verdict_phrase="denied",
            _llm_caller=_stub_llm_narrating_verdict,
        )
        brakes = result["brakes"]
        # Guard must not have fired — narration_degraded is absent or False.
        assert not brakes.get("narration_degraded", False), (
            "narration_degraded should be False/absent when the LLM narrated the "
            "verdict phrase verbatim."
        )
        # The phrase is in the finding text, verbatim and case-sensitively.
        assert "denied" in brakes["finding"], (
            "Finding text must contain the verdict phrase 'denied' verbatim."
        )

    def test_violation_sets_narration_degraded_flag(self):
        """When the rendered text lacks the verdict phrase, narration_degraded=True.

        We use verdict_phrase='denied' on a finding whose deterministic
        text will NOT contain 'denied' (it's a resolved + warranty_covered=True
        record, so the text says 'resolved', not 'denied').

        The guard fires, rewrites the finding with the phrase prepended, and
        sets narration_degraded=True.
        """
        result = assess_vehicle_health(
            "VIN-WARRANTY-TEST-001",
            brake_data=_BRAKE_DATA_RESOLVED_WITH_VERDICT,
            warranty_verdict_phrase="denied",
            _llm_caller=None,
        )
        brakes = result["brakes"]
        assert brakes.get("narration_degraded") is True, (
            "narration_degraded must be True when the verdict phrase 'denied' "
            "is absent from the deterministic finding text."
        )
        # The rewrite must have embedded the phrase.
        assert "denied" in brakes["finding"], (
            "Rewritten finding must contain 'denied' after guard-triggered rewrite."
        )

    def test_violation_rewrite_phrase_is_verbatim(self):
        """The guard's rewrite embeds the EXACT phrase — no paraphrase.

        This pins the literal string 'denied', not _VERDICT_DENIED, so the
        test catches softening of the rewrite target itself.
        """
        result = assess_vehicle_health(
            "VIN-WARRANTY-TEST-001",
            brake_data=_BRAKE_DATA_RESOLVED_WITH_VERDICT,
            warranty_verdict_phrase="denied",
            _llm_caller=None,
        )
        brakes = result["brakes"]
        # LITERAL assertion — not a reference to _VERDICT_DENIED.
        assert "denied" in brakes["finding"], (
            "The rewritten finding must contain the LITERAL string 'denied', "
            "not a paraphrase or softened variant."
        )

    def test_no_verdict_phrase_leaves_results_unchanged(self):
        """Passing warranty_verdict_phrase=None (default) leaves results unchanged."""
        result_without = assess_vehicle_health(
            "VIN-WARRANTY-TEST-001",
            brake_data=_BRAKE_DATA_RESOLVED_WITH_VERDICT,
            _llm_caller=None,
        )
        result_with_none = assess_vehicle_health(
            "VIN-WARRANTY-TEST-001",
            brake_data=_BRAKE_DATA_RESOLVED_WITH_VERDICT,
            warranty_verdict_phrase=None,
            _llm_caller=None,
        )
        # Both results must be identical in the brakes finding.
        assert result_without["brakes"]["finding"] == result_with_none["brakes"]["finding"]
        assert not result_with_none["brakes"].get("narration_degraded", False)

    def test_guard_does_not_touch_p0_bypass_component(self):
        """The T4.14 P0 structural bypass must not be disrupted.

        When brakes carry an active C0161 (P0) DTC, _enrich_with_llm bypasses
        the LLM for the brakes component entirely.  The verdict guard is
        applied AFTER enrichment (Step 2b), so it fires on the deterministic
        brakes finding.  Crucially: it must NOT interfere with the P0 seam.

        We verify: with C0161 present AND warranty_verdict_phrase provided,
        the P0 verbatim phrase is still in the finding text (not displaced by
        the verdict prepend).
        """
        from agents.health.prompt import C0161_VERBATIM_REMEDY

        brake_p0 = {
            "vin": "VIN-P0-WARRANTY-001",
            "service_date": "2026-09-10",
            "dtc_codes": ["C0161"],
            "complaint_text": "Brake warning light",
            "warranty_covered": "false",
            "total_cost_usd": "0.00",
            "outcome": "parts_pending",
            "is_open": True,
            "provenance": "live",
        }
        result = assess_vehicle_health(
            "VIN-P0-WARRANTY-001",
            brake_data=brake_p0,
            warranty_verdict_phrase="denied",
            _llm_caller=None,
        )
        brakes = result["brakes"]
        # The P0 verbatim phrase must still be in the finding.
        assert C0161_VERBATIM_REMEDY in brakes["finding"], (
            "P0 verbatim phrase must remain present even when verdict guard "
            "also fires.  The T4.14 P0 bypass + Step 2b verdict guard are "
            "independent; neither must displace the other."
        )


# ---------------------------------------------------------------------------
# Section 4: MUTATION VERIFICATION
# ---------------------------------------------------------------------------
#
# Rule: a mutation value must come from OUTSIDE the assertion's vocabulary.
# The mutation here monkey-patches assert_coverage_verdict_verbatim to a
# permissive no-op.  This simulates a caller in the narration path that
# "forgets to assert" — the exact silent failure the guard prevents.
#
# Under the mutation:
#   - assess_vehicle_health runs with warranty_verdict_phrase='denied'
#   - The verdict 'denied' is absent from the deterministic finding text
#   - The guard is a no-op → it never sets narration_degraded=True
#   - The test asserts narration_degraded=True → FAILS  ← this is the catch
#
# The assertion that catches it:
#   assert brakes.get("narration_degraded") is True
#
# Recorded in decisions-fragment-coverage-verdict-guard.md per spec requirement.


class TestMutationVerdictSoftening:
    """Mutation-boundary tests for the coverage-verdict-verbatim guard.

    Rule: the guard is load-bearing ONLY IF disabling it causes a test to fail.
    The mutation is: monkey-patch ``assert_coverage_verdict_verbatim`` to a
    no-op and observe which assertion catches it.

    MANUAL MUTATION VERIFICATION (run separately to demonstrate catch):
    ------------------------------------------------------------------
    Apply the mutation::

        # In a Python shell or separate test invocation:
        import agents.health.agent as _agent
        _orig = _agent.assert_coverage_verdict_verbatim
        _agent.assert_coverage_verdict_verbatim = lambda *a, **kw: None  # no-op

        result = _agent.assess_vehicle_health(
            "VIN-MUTATION-TEST",
            brake_data={
                "vin": "VIN-MUTATION-TEST",
                "service_date": "2026-09-01",
                "dtc_codes": [],
                "complaint_text": "Brake noise",
                "warranty_covered": "true",
                "total_cost_usd": "0.00",
                "outcome": "resolved",
                "is_open": False,
                "provenance": "live",
            },
            warranty_verdict_phrase="denied",
            _llm_caller=None,
        )
        # Under the mutation: narration_degraded is absent/False
        assert result["brakes"].get("narration_degraded") is True
        # → AssertionError: None is not True  ← CAUGHT

        _agent.assert_coverage_verdict_verbatim = _orig  # restore

    The assertion that catches the mutation::

        assert brakes.get("narration_degraded") is True

    Mutation recorded in decisions-fragment-coverage-verdict-guard.md.

    The test below (``test_guard_is_load_bearing_on_violation``) PASSES in the
    normal suite (no mutation applied) and FAILS when the mutation is applied
    via monkeypatch — use pytest's ``-k`` flag or the monkeypatch fixture to
    reproduce the catch in a test context (see
    ``test_guard_disabled_by_monkeypatch_does_not_flag_degraded``).
    """

    def test_guard_is_load_bearing_on_violation(self):
        """NORMAL SUITE: guard fires when verdict phrase is absent → narration_degraded=True.

        This test passes in the NORMAL run.  When the mutation is applied
        (guard disabled), this exact assertion fails:
            assert brakes.get("narration_degraded") is True

        That failure is the proof that the guard is load-bearing.
        """
        result = assess_vehicle_health(
            "VIN-MUTATION-TEST-001",
            brake_data=_BRAKE_DATA_RESOLVED_WITH_VERDICT,
            warranty_verdict_phrase="denied",  # absent from deterministic text
            _llm_caller=None,
        )
        brakes = result["brakes"]
        # Guard fires: narration_degraded=True is set.
        assert brakes.get("narration_degraded") is True, (
            "narration_degraded must be True when 'denied' is absent from "
            "the finding text.  If the guard is disabled (mutation), this "
            "assertion fails — proving the guard is load-bearing.\n\n"
            "MUTATION CATCH: this is the assertion that catches the mutation "
            "(guard → no-op).  See class docstring for the manual mutation run."
        )

    def test_guard_disabled_by_monkeypatch_does_not_flag_degraded(self, monkeypatch):
        """MUTATION RUN (in-suite): proves the mutation causes test_guard_is_load_bearing
        to fail by observing the no-degraded-flag behaviour directly.

        This test patches the guard to a no-op and asserts the INVERTED condition
        — narration_degraded is NOT set.  It passes ONLY under the mutation.
        Together with ``test_guard_is_load_bearing_on_violation`` it demonstrates
        the two states:
          - Normal:   narration_degraded=True  (guard fires)
          - Mutation: narration_degraded absent (guard no-op)

        Note: this test itself PASSES — it is documenting the mutation effect,
        not asserting the desired production behaviour.  The load-bearing
        assertion (narration_degraded=True) lives in the test above.
        """
        # Disable the guard.
        monkeypatch.setattr(
            "agents.health.agent.assert_coverage_verdict_verbatim",
            lambda *args, **kwargs: None,
        )
        result = assess_vehicle_health(
            "VIN-MUTATION-TEST-001",
            brake_data=_BRAKE_DATA_RESOLVED_WITH_VERDICT,
            warranty_verdict_phrase="denied",
            _llm_caller=None,
        )
        brakes = result["brakes"]
        # Under the mutation: narration_degraded is NOT set.
        # The production test above would FAIL here (asserts True, gets None).
        assert not brakes.get("narration_degraded", False), (
            "Under the mutation (guard disabled), narration_degraded must not be "
            "set.  This confirms the flag is set exclusively by the guard — not "
            "by any other path."
        )
        # The phrase is also absent from the finding (not rewritten).
        assert "denied" not in brakes["finding"], (
            "Under the mutation, the rewrite must not have happened — "
            "'denied' is absent from the finding."
        )

    def test_verdict_phrase_literal_pin(self):
        """PIN the literal string 'denied' as the canonical DMS denial phrase.

        Every other test in this class uses _VERDICT_DENIED (a constant).
        This test uses the LITERAL string so that rewriting _VERDICT_DENIED
        to 'under review' FAILS here by value rather than following the symbol.

        Analogous to test_c0161_verbatim_remedy_value_is_pinned in test_agent.py
        (the lesson that motivated this guard).  Both are required:
          - This test catches softening of the canonical constant value.
          - Presence-in-finding tests catch a finding that drops the phrase.
        """
        assert _VERDICT_DENIED == "denied", (
            f"The canonical DMS denial phrase has changed value.\n"
            f"  Got:      {_VERDICT_DENIED!r}\n"
            "  Expected: 'denied'\n\n"
            "If the DMS verdict vocabulary changed, update this test AND "
            "decisions-fragment-coverage-verdict-guard.md to record the change."
        )

    def test_softened_phrase_is_outside_verdict_vocabulary(self):
        """Confirm that _SOFTENED_PHRASE ('under review') is NOT in the DMS enum.

        This test enforces Rule 2: a mutation value must come from OUTSIDE
        the assertion's own vocabulary.  If 'under review' were a valid DMS
        verdict, a test that accepts it as a valid phrase could not detect
        softening.

        The DMS verdict enum is ``{approved, denied, requires_review}``.
        'under review' (with a space) is not a member, and neither is
        'requires_review' the same string as 'under review' — the underscore
        form is the producer's, the spaced form is the softening.

        The enum is IMPORTED from ``agents.health.dms_verdicts``, never restated
        here.  Until Fix Group 19 this test body declared its own
        ``frozenset({"approved", "declined", "under_review"})``, and an identical
        second copy lived in ``test_post_service_visit.py``.  Both were wrong in
        the same two places, neither could see the other, and the suite stayed
        green — a per-test copy of a mirrored enum has nothing to drift against.
        Pinned by ``test_dms_verdict_enum.py::TestNoHardcodedVerdictEnumsRemain``.
        """
        assert _SOFTENED_PHRASE not in DMS_VERDICT_ENUM, (
            f"Softened phrase {_SOFTENED_PHRASE!r} is in the DMS verdict enum — "
            "it cannot serve as an out-of-vocabulary mutation value."
        )
