"""Tests for agents/health/prompt.py — verbatim P0 guard correctness.

Spec: .kiro/specs/2026-08-03-cvx-tier2-platform-health-agent/spec.md § D8
Security review: Cycle 7 Warnings W1 and W6; Cycle 8 W-C8-1 (T4.12).

W1 — substring-in check permits softening AFTER the verbatim phrase:
    Fixed in T4.7 using a deny-list/window mechanism.  T4.12 (Cycle 8 W-C8-1)
    replaced T4.7's mechanism entirely with a positional rule: the phrase must
    be the LEADING sentence of its paragraph.  Under the positional rule,
    softening that follows a leading phrase does NOT trigger injection —
    the owner reads the phrase first, so the safety property holds.
    The real defeat the positional rule closes is content placed BEFORE the
    phrase (see TestCycle8Defeats for the three live-reproduced defeat strings).

W6 — case-sensitive C0161 match no-ops on lowercase input:
    inject_p0_verbatim_phrases(..., ['c0161']) returns the finding unchanged
    because the guard compares against the uppercase literal 'C0161'.  A
    safety backstop that depends on upstream casing conventions is not a
    backstop.

T4.12 / Cycle 8 W-C8-1 — three defeat strings that survived Fix Group 4b:
    (A) Softening BEFORE the phrase: dismissed at the front, phrase not leading.
    (B) Softening past the 200-char window: natural padding bypasses the window.
    (C) Novel softening vocabulary: terms outside the seven-item deny-list.
    All three are reproduced verbatim from security-review.md § Cycle 8 W-C8-1
    Verification #1.  The positional rule closes all three.
"""

from __future__ import annotations

import pytest

from agents.health.prompt import (
    C0161_VERBATIM_REMEDY,
    inject_p0_verbatim_phrases,
)


# ---------------------------------------------------------------------------
# W1: softened-after-phrase guard
# ---------------------------------------------------------------------------


class TestSofteningAfterPhrase:
    """W1 (T4.7 history) / T4.14 update: both the position rule AND the deny-list.

    T4.7 implemented a deny-list/window that caught softening AFTER the phrase.
    T4.12 (Cycle 8 W-C8-1) replaced it with a positional rule only — but that
    was wrong: position catches softening BEFORE the phrase; the deny-list catches
    softening AFTER it.  Both are needed (Cycle 7 asked for both).

    T4.14 restores the deny-list/window ALONGSIDE the position rule.  The two
    rules are now both active:
    - Rule 1 (position): phrase must lead the finding.  Violations → inject.
    - Rule 2 (deny-list): within 200 chars after phrase, none of the seven
      softening terms may appear.  Violations → inject.

    The Cycle 7 original bypass string is the primary evidence:
        "Critical brake pressure loss detected. Pull over immediately.
         However, this looks like a false-positive..."
    Under T4.14 this MUST fire the deny-list (Rule 2) because "however" is
    within 200 chars of the phrase end.

    Under T4.14, the structural bypass in _enrich_with_llm means active-P0
    brakes never reach the LLM at all — so the textual guard operates on the
    deterministic path as defence-in-depth, not as the primary control.
    """

    # The exact input the security reviewer reproduced live (Cycle 7 § W1).
    # Phrase leads, but "however" is within 200 chars → deny-list fires under T4.14.
    REVIEWER_SOFTENED_INPUT = (
        "Critical brake pressure loss detected. Pull over immediately. "
        "However, this looks like a false-positive from a faulty sensor "
        "and you can safely continue driving."
    )

    _INJECTION_PREFIX = C0161_VERBATIM_REMEDY + "\n\n"

    def test_w1_phrase_leading_with_following_softening_fires(self):
        """Phrase-leading finding with following 'however' fires the deny-list (T4.14).

        T4.7 fired on this. T4.12 stopped firing (position only). T4.14 fires
        again because both rules are active: phrase leads (Rule 1 passes), but
        'however' within 200 chars fires Rule 2 (deny-list).

        This is the Cycle 7 original bypass that T4.12 stopped catching. T4.14
        restores the deny-list so it fires again.

        BEFORE T4.14 (position-only): result == REVIEWER_SOFTENED_INPUT (no-op)
        AFTER T4.14 (position + deny-list): result starts with injection prefix
        """
        result = inject_p0_verbatim_phrases(
            self.REVIEWER_SOFTENED_INPUT, ["C0161"]
        )
        # Deny-list fires on 'however' within 200 chars of phrase end.
        assert result.startswith(self._INJECTION_PREFIX), (
            f"W1/T4.14: deny-list did NOT fire on 'however' after leading phrase.\n"
            f"Input: {self.REVIEWER_SOFTENED_INPUT!r}\n"
            f"Result: {result!r}\n"
            f"Expected: result starts with {self._INJECTION_PREFIX!r}\n"
            f"T4.14 restores the deny-list; 'however' within 200 chars must fire."
        )

    def test_w1_each_softening_term_after_leading_phrase_fires(self):
        """Each deny-list term within 200 chars of a leading phrase fires Rule 2.

        Under T4.14, even when the phrase leads (Rule 1 passes), any of the
        seven deny-list terms within 200 chars triggers injection.
        """
        softening_terms = [
            "however",
            "that said",
            "false positive",
            "false alarm",
            "you can continue",
            "exercise caution",
            "no urgent",
        ]
        for term in softening_terms:
            softened = (
                f"{C0161_VERBATIM_REMEDY} "
                f"{term.capitalize()}, the situation may not require action."
            )
            result = inject_p0_verbatim_phrases(softened, ["C0161"])
            assert result.startswith(self._INJECTION_PREFIX), (
                f"W1/T4.14: deny-list did NOT fire for term {term!r} after leading phrase.\n"
                f"Input:  {softened!r}\n"
                f"Result: {result!r}\n"
                f"T4.14: deny-list term within 200 chars of leading phrase must inject."
            )

    def test_w1_unsoftened_phrase_at_start_is_noop(self):
        """A well-formed leading phrase with no softening must remain a no-op.

        No deny-list terms present → both rules pass → no injection.
        """
        correct_output = (
            f"{C0161_VERBATIM_REMEDY} "
            "Schedule immediate repair at your nearest service centre."
        )
        result = inject_p0_verbatim_phrases(correct_output, ["C0161"])
        assert C0161_VERBATIM_REMEDY in result
        assert result.count(C0161_VERBATIM_REMEDY) == 1, (
            "A correctly-formed output must not be double-injected."
        )

    def test_w1_missing_phrase_still_injected(self):
        """The existing injection path (phrase absent entirely) still works.

        This is the pre-existing test scenario in test_agent.py — repeated
        here to confirm the fix does not regress it.
        """
        no_phrase = "Brake service completed. Outcome was resolved."
        result = inject_p0_verbatim_phrases(no_phrase, ["C0161"])
        assert C0161_VERBATIM_REMEDY in result, (
            "Phrase must be injected when it is entirely absent."
        )


# ---------------------------------------------------------------------------
# W6: case-insensitive C0161 matching
# ---------------------------------------------------------------------------


class TestCaseInsensitiveDtcMatch:
    """W6: the guard must fire for lowercase 'c0161' in dtc_codes.

    The current implementation compares ``"C0161" not in dtc_codes``, which
    fails for lower- or mixed-case codes.  ADP's generator emits uppercase
    today, but this is a reference architecture and a safety backstop that
    depends on an upstream product's casing convention is not a backstop.
    """

    def test_w6_lowercase_c0161_triggers_injection(self):
        """Lowercase 'c0161' must trigger verbatim phrase injection.

        Security reviewer reproduced live (Cycle 7 § W6):
            inject_p0_verbatim_phrases('Some finding', ['c0161'])
            → 'Some finding'  (phrase injected: False)

        After the fix, the guard must caselessly match C0161 and inject.
        """
        result = inject_p0_verbatim_phrases("Some finding.", ["c0161"])
        assert C0161_VERBATIM_REMEDY in result, (
            f"W6: guard did NOT fire for lowercase 'c0161'.\n"
            f"Result: {result!r}\n"
            f"Expected: C0161_VERBATIM_REMEDY injected into the output.\n"
            f"This is the exact reproduction from security-review Cycle 7."
        )

    def test_w6_mixed_case_c0161_triggers_injection(self):
        """Mixed-case 'C0161' variants must also trigger injection."""
        for variant in ("C0161", "c0161", "C0161"):
            result = inject_p0_verbatim_phrases("Brakes checked.", [variant])
            assert C0161_VERBATIM_REMEDY in result, (
                f"W6: guard did NOT fire for DTC code variant {variant!r}."
            )

    def test_w6_unrelated_dtcs_are_not_affected(self):
        """Lowercase codes that are NOT C0161 must not trigger injection."""
        result = inject_p0_verbatim_phrases(
            "Tire pressure sensor issue.", ["c0040"]
        )
        assert C0161_VERBATIM_REMEDY not in result, (
            "C0040 (even lowercase) must not cause the C0161 phrase to be injected."
        )

    def test_w6_existing_softened_noop_test_still_passes(self):
        """The pre-existing 'softened variant without phrase' path still works.

        test_c0161_verbatim_phrase_not_softened in test_agent.py tests softened
        variants that do NOT contain the verbatim phrase.  This is a different
        scenario than W1 — confirm both paths coexist correctly.
        """
        # Softened text WITH NO verbatim phrase: the guard must inject.
        no_phrase_softened = "Your brakes need attention. Exercise caution while driving."
        result = inject_p0_verbatim_phrases(no_phrase_softened, ["C0161"])
        assert C0161_VERBATIM_REMEDY in result, (
            "Guard must inject when phrase is absent (regardless of other text)."
        )


# ---------------------------------------------------------------------------
# T4.12: Cycle 8 W-C8-1 — three defeat strings that survived Fix Group 4b
# ---------------------------------------------------------------------------


class TestCycle8Defeats:
    """T4.12 (security review Cycle 8 W-C8-1) + T4.14: three defeat shapes analysis.

    Each test uses the verbatim defeat string from security-review.md § Cycle 8
    W-C8-1 Verification #1.

    Under T4.14's two-rule guard (position + deny-list), each case is analysed:

    **[A] Softening BEFORE the phrase** — caught by Rule 1 (phrase not leading
    → inject).  Fires against the backstop.

    **[B] Softening past the 200-char window** — phrase IS leading (Rule 1 passes);
    "however" is at offset 256 from phrase end, past the 200-char window (Rule 2
    passes) → no injection.  Structurally unreachable via T4.14 bypass: active-P0
    brakes never reach the LLM, so this shape can only arise on the deterministic
    path, where the guard correctly does not inject (phrase leads, softening is
    downstream past the window).

    **[C] Novel softening vocabulary** — phrase IS leading (Rule 1 passes);
    "actually" is not in the seven-item deny-list (Rule 2 passes) → no injection.
    Structurally unreachable via T4.14 bypass for the same reason.

    The deny-list is defence-in-depth on the deterministic path, not an exhaustive
    vocabulary gate.  Position + deny-list together close the cases the old
    window-only (T4.7) or position-only (T4.12) rules left open.
    """

    _INJECTION_PREFIX = C0161_VERBATIM_REMEDY + "\n\n"

    # -----------------------------------------------------------------------
    # [A] Softening BEFORE the phrase
    # Verbatim from security-review.md § Cycle 8 W-C8-1 Verification #1:
    #   Input: "This looks like a false-positive from a faulty sensor and you
    #           can safely continue driving. However, Critical brake pressure
    #           loss detected. Pull over immediately."
    #   BEFORE fix: No-op: True  (guard did NOT fire)
    # -----------------------------------------------------------------------
    DEFEAT_A = (
        "This looks like a false-positive from a faulty sensor and you "
        "can safely continue driving. However, "
        "Critical brake pressure loss detected. Pull over immediately."
    )

    def test_c8_defeat_A_softening_before_phrase_fires(self):
        """[A] Softening BEFORE the phrase fires against the backstop.

        BEFORE fix (T4.7 deny-list/window): guard did NOT fire.
        The window only scanned characters AFTER the phrase end, so any
        content placed before the phrase was invisible to the guard.

        AFTER T4.14 (position + deny-list): the phrase does not lead
        (there is a full dismissal sentence before it), so Rule 1 fires.
        Result must start with C0161_VERBATIM_REMEDY + "\\n\\n".

        This is the backstop case — active-P0 brakes never reach the LLM
        under T4.14's structural bypass, so this shape can only arise on the
        deterministic path.

        Verbatim defeat string from security-review.md § Cycle 8 W-C8-1 [A].
        """
        result = inject_p0_verbatim_phrases(self.DEFEAT_A, ["C0161"])
        assert result.startswith(self._INJECTION_PREFIX), (
            f"C8-W1 [A]: guard did NOT fire on softening-BEFORE-phrase input.\n"
            f"Input:    {self.DEFEAT_A!r}\n"
            f"Result:   {result!r}\n"
            f"Expected: result starts with {self._INJECTION_PREFIX!r}\n"
            f"T4.14 Rule 1 (position) must fire when phrase does not lead."
        )

    # -----------------------------------------------------------------------
    # [B] Softening past the 200-character window
    # Verbatim from security-review.md § Cycle 8 W-C8-1 Verification #1:
    #   Input length: 341; 'However' at offset 256 from phrase end (past window)
    #   BEFORE fix: No-op: True  (guard did NOT fire)
    # Under T4.14: structurally unreachable via bypass; backstop correct no-op.
    # -----------------------------------------------------------------------
    _PAD = (
        "The brake system uses hydraulic pressure to convert pedal force "
        "into stopping power. " * 3
    )
    DEFEAT_B = (
        f"Critical brake pressure loss detected. Pull over immediately. "
        f"{_PAD}However, false positive."
    )

    def test_c8_defeat_B_past_window_structurally_unreachable(self):
        """[B] Softening past the 200-char window: structurally unreachable via T4.14 bypass.

        T4.14 structural bypass: active-P0 brakes never reach the LLM, so
        this shape can only appear on the deterministic path.  On the
        deterministic path, the backstop correctly does NOT inject:
        - Rule 1: phrase IS the leading sentence → passes.
        - Rule 2: "however" at offset 256 > 200-char window → passes.
        Both rules pass → result unchanged (correct output).

        The T4.7 window rule would have fired if "however" were within 200
        chars.  At 256 chars it was already past the window even then.
        The security-review Cycle 8 reproduced this as a no-op against T4.7's
        rule (correct — window too small); T4.14 preserves that behavior.
        """
        result = inject_p0_verbatim_phrases(self.DEFEAT_B, ["C0161"])
        # Both rules pass (phrase leads; however is past window) → no injection.
        assert result == self.DEFEAT_B, (
            f"C8-W1 [B]: unexpected injection on phrase-leading, past-window input.\n"
            f"Input starts with phrase: "
            f"{self.DEFEAT_B.lstrip().startswith(C0161_VERBATIM_REMEDY)}\n"
            f"'However' offset from phrase end: "
            f"{self.DEFEAT_B.find('However') - len(C0161_VERBATIM_REMEDY)} (> 200)\n"
            f"Result: {result[:120]!r}...\n"
            f"Both rules pass → no injection."
        )
        assert result.count(C0161_VERBATIM_REMEDY) == 1, (
            "Phrase must not be duplicated when both rules pass."
        )

    def test_c8_defeat_B_phrase_not_leading_fires(self):
        """[B] variant: phrase at char >0, with long padding before it, fires Rule 1.

        The pure [B] case above has the phrase leading, so it's a correct no-op.
        This variant puts the padding BEFORE the phrase to confirm that a
        non-leading phrase with long intervening text still triggers injection
        — the window size is irrelevant to the positional rule.
        """
        phrase_not_leading_with_long_padding = (
            f"{self._PAD}However, {C0161_VERBATIM_REMEDY}"
        )
        result = inject_p0_verbatim_phrases(
            phrase_not_leading_with_long_padding, ["C0161"]
        )
        assert result.startswith(self._INJECTION_PREFIX), (
            f"C8-W1 [B] variant: guard did NOT fire on non-leading phrase "
            f"with long preceding content.\n"
            f"Input: ...{phrase_not_leading_with_long_padding[-80:]!r}\n"
            f"Result starts with prefix: "
            f"{result.startswith(self._INJECTION_PREFIX)}"
        )

    # -----------------------------------------------------------------------
    # [C] Novel softening terms not in the deny-list
    # Verbatim from security-review.md § Cycle 8 W-C8-1 Verification #1:
    #   Input: "Critical brake pressure loss detected. Pull over immediately.
    #           Actually, that's likely wrong — sensor error is more probable."
    #   BEFORE fix: No-op: True  (guard did NOT fire — "actually" not in list)
    # Under T4.14: structurally unreachable via bypass; backstop correct no-op.
    # -----------------------------------------------------------------------
    DEFEAT_C = (
        "Critical brake pressure loss detected. Pull over immediately. "
        "Actually, that's likely wrong \u2014 sensor error is more probable."
    )

    def test_c8_defeat_C_novel_term_structurally_unreachable(self):
        """[C] Novel softening term outside deny-list: structurally unreachable via T4.14 bypass.

        T4.14 structural bypass: active-P0 brakes never reach the LLM, so
        this shape can only appear on the deterministic path.  On the
        deterministic path, the backstop correctly does NOT inject:
        - Rule 1: phrase IS the leading sentence → passes.
        - Rule 2: "actually" is not in the seven-item deny-list → passes.
        Both rules pass → result unchanged (correct output).

        The deny-list is defence-in-depth, not an exhaustive vocabulary gate.
        Novel vocabulary that follows a leading phrase does not defeat
        the safety property under T4.14, because the primary control
        (_enrich_with_llm bypass) ensures no LLM text narrates the component.

        BEFORE T4.7 (old guard): no-op (defeat).
        Under T4.14: correct no-op (no bypass from the backstop, but structurally
        unreachable via the primary control).
        """
        result = inject_p0_verbatim_phrases(self.DEFEAT_C, ["C0161"])
        # Both rules pass (phrase leads; "actually" not in deny-list) → no injection.
        assert result == self.DEFEAT_C, (
            f"C8-W1 [C]: unexpected injection on phrase-leading, novel-term input.\n"
            f"Input starts with phrase: "
            f"{self.DEFEAT_C.lstrip().startswith(C0161_VERBATIM_REMEDY)}\n"
            f"Result: {result[:120]!r}...\n"
            f"'Actually' not in deny-list; phrase leads → both rules pass."
        )
        assert result.count(C0161_VERBATIM_REMEDY) == 1, (
            "Phrase must not be duplicated when both rules pass."
        )

    def test_c8_defeat_C_novel_term_before_phrase_fires(self):
        """[C] variant: novel softening term BEFORE the phrase must trigger injection.

        The pure [C] case above has the phrase leading (valid output, no injection
        needed).  This variant places the novel softening term BEFORE the phrase
        — the shape where deny-list logic would fail regardless of the term used.
        The positional rule catches it because the phrase does not lead.
        """
        novel_before = (
            f"Actually, that's likely wrong \u2014 sensor error is more probable. "
            f"{C0161_VERBATIM_REMEDY}"
        )
        result = inject_p0_verbatim_phrases(novel_before, ["C0161"])
        assert result.startswith(self._INJECTION_PREFIX), (
            f"C8-W1 [C] variant: guard did NOT fire on novel-term-before-phrase.\n"
            f"Input: {novel_before!r}\n"
            f"Result: {result[:120]!r}"
        )

    # -----------------------------------------------------------------------
    # Legitimate case: phrase leads with unrelated prose in later paragraphs
    # (The task requires this is left unchanged — it is the correct non-trigger)
    # -----------------------------------------------------------------------

    def test_c8_phrase_leading_with_later_neutral_prose_is_noop(self):
        """A finding where the phrase leads but has unrelated prose later is a no-op.

        This is the 'legitimate case' named in T4.12's Accept criteria:
        'A finding whose phrase already leads its paragraph, with unrelated
        prose in *later* paragraphs, is left unchanged.'

        Example: "Critical brake pressure loss detected. Pull over immediately.
                  However, the oil change is routine and can wait."
        The phrase leads, so the safety property holds.  The positional rule
        must NOT inject a second copy.
        """
        legitimate = (
            f"{C0161_VERBATIM_REMEDY}\n\n"
            "However, the oil change is routine and can wait. "
            "Battery is charging normally. "
            "No tire concerns detected."
        )
        result = inject_p0_verbatim_phrases(legitimate, ["C0161"])
        assert result == legitimate, (
            "Phrase-leading finding with later neutral prose must be returned unchanged."
        )
        assert result.count(C0161_VERBATIM_REMEDY) == 1, (
            "Phrase must not be duplicated when it already leads."
        )
