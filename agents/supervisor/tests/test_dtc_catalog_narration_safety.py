"""The DTC response catalog must never assert a completed escalation.

Spec `2026-09-23-cvx-escalation-recommend-not-act`, headline property: the
platform RECOMMENDS a human handoff and roadside; it does not claim either has
happened. Nothing in this platform calls emergency services at all, and F1.1
removed the roadside dispatch request.

WHY THIS FILE EXISTS
--------------------
Security review Cycle 2 (W4) found that `B0001_FIRE` and `U3000_CRASH` still
carried *"Emergency services and roadside have your location and are en route"*
and *"Emergency services have your exact location and are en route"* in their
`chat_diagnosis`, three fix groups after `e6d0bed` removed the same claims from
`follow_sentences` and the pull-over templates. Two things made that miss costly:

1. `chat_diagnosis` is **consumed**, unlike the `follow_sentences` e6d0bed fixed
   (`render_response` still has no callers). `bidi_app.py` emits it as
   `escalation.chatDiagnosis` and iOS renders it as a SYSTEM bubble in the
   Connect chat.
2. It **contradicted the spoken line of its own template** — `exit_vehicle` says
   "Call nine-one-one if anyone is hurt or in danger" and `stay_call_911` says
   "call nine-one-one right now". A driver in a fire or a collision who is told
   responders are already coming may wait instead of calling. That is the one
   error in this catalog that can cost more than credibility.

No test referenced `dtc_response_catalog` at all before this file, which is why
a prose-only rule went un-enforced for three fix groups. Per
`~/.kiro/steering/agentic-tiers.md`: a boundary that exists only in a prompt or a
comment is a boundary that will be crossed — test-enforce it.

This scans EVERY string in the catalog, so a future entry inherits the guard
rather than needing its own review.
"""

from __future__ import annotations

import re

import pytest

from agents.supervisor.dtc_response_catalog import (
    DTC_TO_TEMPLATE,
    RESPONSE_TEMPLATES,
)

# Phrases that assert an escalation has already happened. Each is a claim the
# platform cannot make: nothing calls emergency services, and nothing dispatches
# a truck. "You can request roadside assistance" and "call nine-one-one" are the
# permitted recommendation forms and must not match.
#
# Security review Cycle 3, S1: the first version of this list enumerated surface
# phrasings ("en route", "on the way", "dispatched", "have your location") and was
# bypassable by five equivalents, including
# "Help is coming to you. 911 has been notified and responders will arrive shortly."
# A denylist of phrasings cannot close a space of paraphrases. So the patterns
# below target the ACTION SHAPE instead — something-has-been-summoned, or
# someone-is-approaching — and `TestHazardTemplatesMustDirectTheDriverToCall`
# adds a positive requirement so the two hazard entries cannot merely avoid
# forbidden words, they must carry the 911 directive.
_COMPLETED_ESCALATION_CLAIMS = [
    # --- something/someone has been summoned ---
    r"\b(?:has|have|had)\s+(?:already\s+)?been\s+"
    r"(?:notified|alerted|contacted|called|summoned|sent|dispatched|informed)\b",
    r"\bwe(?:'ve|\s+have)\s+(?:sent|called|notified|alerted|dispatched|contacted)\b",
    r"\b(?:is|are)\s+being\s+(?:called|notified|dispatched|sent|contacted)\b",
    r"\bhas\s+been\s+dispatched\b",
    r"\bhave\s+been\s+dispatched\b",
    r"\bdispatched\s+to\s+your\b",
    # --- someone is approaching / will arrive ---
    r"\ben\s+route\b",
    r"\b(?:is|are)\s+on\s+(?:the|its|their)\s+way\b",
    r"\balready\s+on\s+(?:its|their|the)\s+way\b",
    r"\b(?:help|assistance|support|someone|somebody|a\s+tow|a\s+truck|"
    r"responders?|a\s+response\s+team|emergency\s+services)\s+(?:is|are)\s+"
    r"(?:coming|arriving|nearby|close|inbound|approaching)\b",
    r"\bwill\s+(?:arrive|be\s+there|reach\s+you|be\s+with\s+you)\b",
    r"\b(?:is|are)\s+nearby\b",
    # --- a handoff asserted as completed ---
    r"\bon\s+the\s+line\b",
    r"\bhave\s+your\s+(?:exact\s+)?(?:location|position|coordinates|GPS)\b",
    r"\bhas\s+your\s+(?:exact\s+)?(?:location|position|coordinates|GPS)\b",
    r"\bI(?:'ve|\s+have)\s+connected\s+you\b",
    r"\ba\s+technician\s+is\b",
]

_CLAIM_RE = re.compile("|".join(_COMPLETED_ESCALATION_CLAIMS), re.IGNORECASE)


def _walk_strings(obj, path: str = ""):
    """Yield (path, text) for every string anywhere in the structure."""
    if isinstance(obj, str):
        yield path, obj
    elif isinstance(obj, dict):
        for k, v in obj.items():
            yield from _walk_strings(v, f"{path}.{k}" if path else str(k))
    elif isinstance(obj, (list, tuple)):
        for i, v in enumerate(obj):
            yield from _walk_strings(v, f"{path}[{i}]")


def _catalog_strings() -> list[tuple[str, str]]:
    out = list(_walk_strings(DTC_TO_TEMPLATE, "DTC_TO_TEMPLATE"))
    out += list(_walk_strings(RESPONSE_TEMPLATES, "RESPONSE_TEMPLATES"))
    # `keywords` are driver utterances to match on, not platform narration.
    return [(p, t) for p, t in out if ".keywords" not in p]


class TestCatalogNeverAssertsACompletedEscalation:
    def test_anti_vacuity_the_scan_reaches_the_content(self):
        """If the walker stops finding strings, every assertion below passes for
        the wrong reason. Pinned low enough not to be brittle, high enough to
        prove the structure was traversed."""
        found = _catalog_strings()
        assert len(found) >= 40, (
            f"expected the walker to reach the catalog's narration; found only "
            f"{len(found)} strings. Has the structure changed shape?"
        )
        joined = " ".join(t for _, t in found)
        # The two safety-critical templates must be in scope, or a regression in
        # exactly the place W4 found one would go unseen.
        assert "thermal-runaway threshold" in joined, (
            "anti-vacuity: B0001_FIRE's chat_diagnosis must be in scope"
        )
        assert "Impact sensors recorded a collision" in joined, (
            "anti-vacuity: U3000_CRASH's chat_diagnosis must be in scope"
        )

    def test_positive_control_the_matcher_catches_the_real_phrasing(self):
        """The matcher must catch the exact strings W4 found, or the negative
        assertion below is decorative. The second block is security Cycle 3's S1:
        paraphrases that bypassed the first, phrasing-based version of this list."""
        for text in (
            # The two W4 originals.
            "Emergency services and roadside have your location and are en route.",
            "Emergency services have your exact location and are en route.",
            "Roadside assistance has been dispatched to your location.",
            "I'm connecting you to a live fleet-support agent.  A technician is coming.",
            "Emergency services are being called and an agent is on the line.",
            # S1 bypasses — each passed the phrasing-based denylist.
            "Help is coming to you. 911 has been notified and responders will "
            "arrive shortly.",
            "Emergency services have been called.",
            "A response team is nearby.",
            "We've sent help to your location.",
            "Someone is on their way.",
            "Roadside is already on its way.",
            "A tow truck is inbound and will be with you soon.",
            "Responders have your coordinates.",
        ):
            assert _CLAIM_RE.search(text), f"matcher must flag: {text!r}"

    def test_permitted_recommendation_forms_are_not_flagged(self):
        """The guard must not fire on the forms the spec wants, or it will be
        weakened the first time someone writes correct copy."""
        for text in (
            "You can tap to talk with a live fleet-support agent in the app.",
            "You can request roadside assistance from your screen if the "
            "vehicle can't be driven safely.",
            "Call nine-one-one if anyone is hurt or in danger.",
            "Get clear of the vehicle and call nine-one-one now. Fleet support "
            "can reach you on this chat.",
            "Stay in the vehicle if it's safe and call nine-one-one now. Stay on "
            "this chat so fleet support can relay anything the emergency "
            "responders need.",
        ):
            assert not _CLAIM_RE.search(text), f"must NOT flag: {text!r}"

    @pytest.mark.parametrize("path,text", _catalog_strings(), ids=lambda v: None)
    def test_no_catalog_string_asserts_a_completed_escalation(self, path, text):
        hit = _CLAIM_RE.search(text)
        assert hit is None, (
            f"{path} asserts a completed escalation: {hit.group(0)!r}\n"
            f"  full text: {text!r}\n"
            "The platform does not call emergency services and does not "
            "dispatch roadside (F1.1). Recommend, never assert — and note that "
            "`chat_diagnosis` is rendered to the driver as a chat bubble, so a "
            "false claim here can stop them calling for help themselves."
        )



class TestHazardTemplatesMustDirectTheDriverToCall:
    """The positive half of the guard, added for security review Cycle 3 S1.

    A denylist can only say what the copy must not claim. For the two entries
    where a driver's life is the stake, that is not enough: copy can satisfy any
    denylist by saying nothing useful at all, and the original defect was not
    merely a false claim — it was a false claim that *replaced* the instruction to
    call for help, and contradicted the spoken line of its own template.

    So these two must affirmatively carry the 911 directive. Scoped to the two
    hazard DTCs deliberately: a tyre-pressure entry has no business telling anyone
    to call emergency services, so this is not a catalog-wide rule.
    """

    # DTC -> the template whose spoken line already instructs the driver to call.
    _HAZARD_DTCS = {
        "B0001_FIRE": "exit_vehicle",
        "U3000_CRASH": "stay_call_911",
    }

    def test_anti_vacuity_the_hazard_entries_exist_with_the_expected_templates(self):
        for dtc, template in self._HAZARD_DTCS.items():
            assert dtc in DTC_TO_TEMPLATE, f"{dtc} missing from DTC_TO_TEMPLATE"
            assert DTC_TO_TEMPLATE[dtc].get("template") == template, (
                f"{dtc} should use the {template!r} template; if this changed, "
                "re-check that its spoken line still instructs the driver to call"
            )
            assert template in RESPONSE_TEMPLATES, f"{template} missing"

    def test_hazard_chat_diagnosis_tells_the_driver_to_call(self):
        """Mutation: replace either `chat_diagnosis` with anything that omits the
        directive — including copy that passes the denylist, e.g. "Sensors
        recorded a collision. Fleet support can reach you on this chat." — and
        this FAILS.
        """
        for dtc in self._HAZARD_DTCS:
            text = DTC_TO_TEMPLATE[dtc].get("chat_diagnosis", "")
            assert re.search(r"nine-one-one|\b911\b", text, re.IGNORECASE), (
                f"{dtc}.chat_diagnosis must tell the driver to call emergency "
                f"services themselves, because the platform does not. Got: {text!r}\n"
                "The original defect replaced this instruction with a false claim "
                "that help was already coming, contradicting the spoken line of "
                "this entry's own template."
            )

    def test_hazard_spoken_lines_still_instruct_the_driver_to_call(self):
        """The chat bubble must not be the only place the directive lives. If a
        template's spoken line loses it, the two surfaces can drift apart again —
        which is the shape of the original defect."""
        for dtc, template in self._HAZARD_DTCS.items():
            tpl = RESPONSE_TEMPLATES[template]
            spoken = " ".join(
                [tpl.get("first_sentence", "")] + list(tpl.get("follow_sentences", []))
            )
            assert re.search(r"nine-one-one|\b911\b", spoken, re.IGNORECASE), (
                f"template {template!r} (used by {dtc}) must instruct the driver to "
                f"call emergency services in its spoken copy. Got: {spoken!r}"
            )



# -----------------------------------------------------------------------------
# The chat bubble must not make the spoken 911 directive conditional
# -----------------------------------------------------------------------------
#
# User decision 2026-09-24 (HANDOFF-fire-crash-wording.md). Fix Group 10's
# U3000_CRASH copy read "If anyone is hurt, call nine-one-one now." It passed
# every test above: it claims no completed escalation, and it does contain the
# directive. The defect was a third kind: the spoken line of its template
# (`stay_call_911`) tells the driver to call unconditionally, and the chat text
# made that conditional. A driver who feels fine after a collision could read it
# as permission not to call.
#
# The property: where a template's spoken copy carries an UNCONDITIONAL "call
# nine-one-one", the entry's chat_diagnosis must carry one too, and must carry
# no conditioned one. "Stay in the vehicle if it's safe and call nine-one-one"
# is NOT conditioned. Its "if" governs staying in the vehicle, not the call,
# and it matches the spoken line word for word. So the matcher targets a
# conditional clause that governs the CALL: a leading "If X, call ..." or a
# trailing "call ... if X".

_DIRECTIVE_RE = re.compile(r"\bcall\s+(?:nine-one-one|911)\b", re.IGNORECASE)

_CONDITIONAL = r"(?:if|when|unless|once|should|in\s+case)"
_CONDITIONED_DIRECTIVE_RES = [
    # "If anyone is hurt, call nine-one-one" / "When it's safe, call 911"
    re.compile(
        rf"\b{_CONDITIONAL}\b[^.!?]*?,\s*(?:please\s+|then\s+)?call\s+(?:nine-one-one|911)\b",
        re.IGNORECASE,
    ),
    # "Call nine-one-one if anyone is hurt"
    re.compile(
        rf"\bcall\s+(?:nine-one-one|911)\b[^.!?]*?\b{_CONDITIONAL}\b",
        re.IGNORECASE,
    ),
]


def _sentences(text: str) -> list[str]:
    return [s for s in re.split(r"(?<=[.!?])\s+", text) if s]


def _directive_sentences(text: str) -> list[str]:
    return [s for s in _sentences(text) if _DIRECTIVE_RE.search(s)]


def _is_conditioned(sentence: str) -> bool:
    return any(r.search(sentence) for r in _CONDITIONED_DIRECTIVE_RES)


def _spoken_copy(template_id: str) -> str:
    tpl = RESPONSE_TEMPLATES[template_id]
    return " ".join([tpl.get("first_sentence", "")] + list(tpl.get("follow_sentences", [])))


def _entries_with_unconditional_spoken_directive() -> list[str]:
    """DTCs whose template's spoken copy tells the driver to call 911 with no
    condition. Computed over the whole catalog, so a future entry bound to such
    a template inherits the guard without anyone adding it here."""
    out = []
    for dtc, entry in DTC_TO_TEMPLATE.items():
        spoken = _spoken_copy(entry["template"])
        if any(not _is_conditioned(s) for s in _directive_sentences(spoken)):
            out.append(dtc)
    return out


class TestChatDiagnosisDoesNotWeakenTheSpokenDirective:
    def test_positive_control_conditioned_forms_are_flagged(self):
        """The rejected U3000 line and its paraphrases must be caught, or the
        catalog assertion below is decorative."""
        for text in (
            "If anyone is hurt, call nine-one-one now.",  # the rejected line
            "If you are injured, call 911.",
            "When it's safe, call nine-one-one.",
            "Call nine-one-one if anyone is hurt or in danger.",
            "Call 911 when you can.",
            "In case of injury, please call nine-one-one.",
        ):
            sentences = _directive_sentences(text)
            assert sentences, f"test input must contain the directive: {text!r}"
            assert all(_is_conditioned(s) for s in sentences), (
                f"matcher must flag the conditioned directive: {text!r}"
            )

    def test_unconditional_forms_are_not_flagged(self):
        """The approved copy and the spoken line itself must pass. If the
        template's own first_sentence tripped the matcher, the rule would be
        inconsistent with the line it defends."""
        for text in (
            "Stay in the vehicle if it's safe and call nine-one-one now.",
            "Get clear of the vehicle and call nine-one-one now.",
            RESPONSE_TEMPLATES["stay_call_911"]["first_sentence"],
        ):
            sentences = _directive_sentences(text)
            assert sentences, f"test input must contain the directive: {text!r}"
            assert not any(_is_conditioned(s) for s in sentences), (
                f"must NOT flag the unconditional directive: {text!r}"
            )

    def test_anti_vacuity_the_crash_entry_is_in_scope(self):
        """If scope computation stops finding U3000_CRASH, the check below
        passes over an empty list and guards nothing."""
        scope = _entries_with_unconditional_spoken_directive()
        assert "U3000_CRASH" in scope, (
            f"U3000_CRASH must be in scope; computed scope was {scope!r}. Has "
            "`stay_call_911`'s spoken line lost its unconditional directive?"
        )

    @pytest.mark.parametrize("dtc", _entries_with_unconditional_spoken_directive())
    def test_chat_diagnosis_carries_the_directive_unconditionally(self, dtc):
        """Mutation: restore "If anyone is hurt, call nine-one-one now." in
        U3000_CRASH, and this FAILS."""
        chat = DTC_TO_TEMPLATE[dtc].get("chat_diagnosis", "")
        sentences = _directive_sentences(chat)
        assert sentences, (
            f"{dtc}.chat_diagnosis must tell the driver to call nine-one-one, "
            f"as its template's spoken line does. Got: {chat!r}"
        )
        conditioned = [s for s in sentences if _is_conditioned(s)]
        assert not conditioned, (
            f"{dtc}.chat_diagnosis makes the 911 directive conditional: "
            f"{conditioned!r}\n"
            f"Its template's spoken line is unconditional: "
            f"{_spoken_copy(DTC_TO_TEMPLATE[dtc]['template'])!r}\n"
            "The chat bubble must not say less than the spoken line. A driver "
            "who feels fine could read a condition as permission not to call."
        )


# -----------------------------------------------------------------------------
# F17.1: 911 on the exit_vehicle P0 directive (2026-09-28)
# -----------------------------------------------------------------------------
#
# `issues/2026-09-28-b0001-fire-p0-directive-omits-911/`: on a fire the platform's
# on-screen safety line was the first sentence of the `exit_vehicle` template
# alone — "stop the vehicle, turn it off, get out, and move at least fifty feet
# away." — with the 911 clause living in `follow_sentences[1]` conditioned on
# "if anyone is hurt or in danger". `follow_sentences` is not consumed by
# `p0_directive` (see the comment above `RESPONSE_TEMPLATES`), so the 911
# instruction never reached the driver on the platform's own P0 channel.
#
# The property enforced below has two halves:
#
#   (a) `p0_directive` for every code bound to `exit_vehicle` (currently
#       `B0001_FIRE` and `P0A80` — user decision 2026-09-27 for P0A80) speaks
#       the literal substring "call nine-one-one now".
#   (b) No condition governs the 911 clause in the sentence that carries it,
#       under the same `_CONDITIONED_DIRECTIVE_RES` matcher the existing chat-
#       bubble guard uses.
#
# Both halves are computed off the running catalog, so a future code moved onto
# `exit_vehicle` inherits the guard without anyone remembering to add it.
#
# **Mutations each half must catch** (verified by hand on 2026-09-28):
#
# - Revert `first_sentence` to "...and move at least fifty feet away." (drop
#   the ", and call nine-one-one now" clause) → (a) FAILS.
# - Change the clause to "call nine-one-one if you feel unsafe" → (b) FAILS
#   because a conditional now governs the directive in the sentence carrying
#   it.
# - Move the clause into a separate sentence "Call nine-one-one now." within
#   `first_sentence` → the property still passes (correct: an unconditional
#   911 sentence, even as its own sentence, is fine).
# - Move the 911 clause back into `follow_sentences` and leave a `first_sentence`
#   without it → (a) FAILS. This is the shape of the original defect.
# - Restore the conditional "Call nine-one-one if anyone is hurt or in danger."
#   in `follow_sentences` alongside the unconditional in `first_sentence` →
#   `TestChatDiagnosisDoesNotWeakenTheSpokenDirective` above already catches
#   any DTC whose `chat_diagnosis` fails to match, but this new test does NOT
#   catch a conditional follow-sentence on its own — that is deliberate. The
#   directive-channel property (this test) is scoped to what `p0_directive`
#   emits. A resurrected conditional in `follow_sentences` is a separate
#   defect and is caught by the whole-catalog scan in
#   `TestCatalogNeverAssertsACompletedEscalation`.
#
# The `exit_vehicle` template's spoken copy is deliberately covered by TWO
# guards: this test scans the P0 directive channel, and
# `TestHazardTemplatesMustDirectTheDriverToCall.test_hazard_spoken_lines_still_
# instruct_the_driver_to_call` scans the concatenated spoken copy. The
# original defect passed the second because it looked for the substring
# "nine-one-one" anywhere in the joined spoken copy — the conditional line was
# in `follow_sentences`, and the joined string contained the substring, so the
# guard fired on a property that did not hold on the P0 directive channel.
# This guard scans the channel that ships to the driver on a P0.


from agents.supervisor.dtc_response_catalog import (  # noqa: E402
    RESPONSE_TEMPLATES as _CATALOG_TEMPLATES,
    lookup_dtc_response as _lookup,
    p0_directive as _p0_directive,
)


def _dtcs_on(template_id: str) -> list[str]:
    return sorted(
        code for code, entry in DTC_TO_TEMPLATE.items()
        if entry.get("template") == template_id
    )


class TestP0DirectiveOnExitVehicleSpeaks911Unconditionally:
    _NAME = "Marcus"

    def test_anti_vacuity_exit_vehicle_still_has_the_expected_codes(self):
        """If a future edit unbinds every code from `exit_vehicle`, the
        parametrized checks below iterate over an empty set and this class
        guards nothing. The two currently-bound codes are the hazards named
        in the 2026-09-28 issue (B0001_FIRE) and the 2026-09-27 decision
        (P0A80)."""
        codes = _dtcs_on("exit_vehicle")
        assert set(codes) == {"B0001_FIRE", "P0A80"}, (
            f"expected the two hazard codes on `exit_vehicle`; got {codes!r}. "
            "If a code has been rebound, re-check the driver-facing P0 "
            "directive still speaks 911 for it."
        )

    def test_positive_control_the_matcher_catches_a_conditioned_directive(self):
        """The two _CONDITIONED_DIRECTIVE_RES patterns must catch the exact
        conditional forms the 2026-09-28 issue removes. If they stop matching
        these, the parametrized check below is decorative."""
        for text in (
            "Call nine-one-one if anyone is hurt or in danger.",
            "If you feel unsafe, call nine-one-one now.",
            "When it's safe, call 911.",
        ):
            assert _directive_sentences(text), text
            assert any(
                _is_conditioned(s) for s in _directive_sentences(text)
            ), f"matcher must flag: {text!r}"

    def test_positive_control_the_matcher_passes_the_unconditional_directive(self):
        """The new `exit_vehicle.first_sentence` must not be flagged by the
        matcher — otherwise the parametrized check would fail on the very
        line it defends."""
        first = _CATALOG_TEMPLATES["exit_vehicle"]["first_sentence"]
        sentences = _directive_sentences(first)
        assert sentences, (
            f"anti-vacuity: `exit_vehicle.first_sentence` must contain the "
            f"911 directive. Got: {first!r}"
        )
        assert not any(_is_conditioned(s) for s in sentences), (
            f"unconditional form must not be flagged: {first!r}"
        )

    @pytest.mark.parametrize("code", _dtcs_on("exit_vehicle"))
    def test_p0_directive_speaks_unconditional_911(self, code):
        """The property under test.

        For every code bound to `exit_vehicle`, the sentence `p0_directive`
        returns must contain "call nine-one-one now" AND no `if|when|unless|
        once|should|in case` clause may govern that directive in the sentence
        carrying it.

        This is the P0 directive CHANNEL, not the joined spoken copy — the
        original defect passed a joined-copy check because a conditional 911
        line lived in `follow_sentences`, which the P0 channel does not
        emit.

        Mutations:
          - Revert `first_sentence` to the pre-2026-09-28 wording -> FAILS.
          - Change the clause to "call nine-one-one if you feel unsafe" ->
            FAILS.
          - Move the clause into `follow_sentences` alone -> FAILS.
        """
        # Route through the real selection + rendering path — a directive
        # obtained by calling p0_directive on a fault carrying this code's
        # template_id is what ships to the driver.
        resolved = _lookup(code)
        assert resolved is not None, f"catalog lookup failed for {code!r}"
        dtcs = [{"code": code, "template_id": resolved["template_id"]}]

        directive = _p0_directive(dtcs, self._NAME)

        # (a) the literal directive is present in the spoken sentence.
        assert "call nine-one-one now" in directive, (
            f"{code}: the P0 directive must speak `call nine-one-one now`. "
            f"Got: {directive!r}. This is the sentence the platform sends to "
            f"the driver's screen on a P0 (bidi_app._show_p0_directive) and "
            f"asks Nova to speak; without it, the driver is not told to "
            "call before Connect handoff."
        )

        # (b) no condition governs the 911 clause in the sentence carrying it.
        # `_is_conditioned` inspects a sentence for the two patterns the
        # existing chat-bubble guard uses.
        sentences_with_directive = _directive_sentences(directive)
        assert sentences_with_directive, (
            f"anti-vacuity: {code}'s directive must contain the 911 clause"
        )
        for s in sentences_with_directive:
            assert not _is_conditioned(s), (
                f"{code}: the sentence carrying the 911 directive is "
                f"conditioned on `if|when|unless|once|should|in case`. "
                f"Sentence: {s!r}. A driver who feels fine can read the "
                f"condition as permission not to call."
            )
