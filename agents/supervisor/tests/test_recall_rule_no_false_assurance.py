"""Persona recall rules must never tell the model to assert "no open recalls".

Regression guard for issues/2026-09-25-recall-rule-asserts-no-open-recalls/.
Live 2026-09-25T15:48Z the assistant told a driver "There are no open recalls"
with no recall data behind it: the fleet driver rule said to say exactly that
whenever retrieve found nothing applicable, and the portfolio has no recall
catalog, so "nothing applicable" means "no data", not "no recalls".

Scope is rule-level, computed: every paragraph in PERSONA_PROMPTS whose label
(the text before the first colon) mentions recalls. A persona that adds a recall
rule later inherits both checks. Two halves, because a denylist of phrasings
alone cannot close a space of paraphrases (security Cycle 3 S1 on the DTC
narration guard):

  * Negative: an absence-of-recalls phrase may only appear governed by a
    negation within the few words before it ("NEVER say there are no open
    recalls" passes; "tell them there are no open recalls" fails).
  * Positive: the rule must carry the fallback the driver needs: say recall
    information could not be found, and point to a dealer or nhtsa.gov/recalls
    with the VIN.

Scope also includes hard rule 2 of both prompt templates (`supervisor.py`), which
reaches every persona. The matcher permits a claim only when a negation governs the
verb that instructs it ("NEVER say", "do not tell", "NEVER say or imply"); a claim
with no instructing verb before it in its clause is flagged. Review Cycles 15-18 of
spec 2026-09-23-cvx-escalation-recommend-not-act widened it each time an appended
phrasing passed. It is a denylist of shapes and cannot be exhaustive; the positive
check (the fallback must be present) and the live eval are the other two controls.

Mutations verified (each fails at least one test):
  1. Restore the original fleet_driver rule.
  2. Remove "NEVER " from the new fleet_driver rule.
  3. Restore the original fleet_owner rule ("If none apply, say so clearly").
  4. Drop "nhtsa.gov/recalls" from a rule.
  5. Drop the "couldn't find recall information" sentence from a rule.
  6. Delete the fleet_driver Recalls paragraph entirely (anti-vacuity).
  7. Append "Otherwise tell them their vehicle is clear of recalls." to a rule
     that still carries the fallback.
"""
from __future__ import annotations

import re

import pytest

from agents.supervisor.persona_definitions import PERSONA_PROMPTS

_LABEL = re.compile(r"^([A-Z][A-Za-z ,&/-]{0,40}):\s")

# Absence-of-recalls shapes: "no (open) recalls", "zero recalls", "free/clear of
# recalls", "recall-free", "all clear", and the old fleet_owner "say so".
# Absence shapes. Review Cycle 15 W1 (escalation spec) added the negated-verb forms
# ("there aren't any open recalls"), "not subject to / affected by / under any
# recall" and "has none"; Cycle 16 added "doesn't have any … recalls", "there are
# none" and "nothing (is) open/outstanding".
_ABSENCE = re.compile(
    # Up to four modifiers, commas allowed ("no open, unrepaired recalls", "no open
    # NHTSA safety recalls"; review Cycle 19 S6).
    r"\b(?:no|zero|without|free\s+of|clear\s+of)\s+(?:[\w,-]+\s+){0,4}recalls?\b"
    r"|\b(?:aren't|are\s+not|isn't|is\s+not)\s+(?:any\s+)?(?:\w+\s+){0,2}recalls?\b"
    r"|\b(?:doesn't|does\s+not|don't|do\s+not)\s+have\s+(?:any\s+)?(?:\w+\s+){0,2}recalls?\b"
    r"|\b(?:not|isn't|aren't)\s+(?:subject|affected|under)\s+(?:to|by|any)?\s*(?:any\s+|a\s+)?(?:\w+\s+){0,1}recalls?\b"
    r"|\b(?:has|have)\s+none\b|\bthere\s+(?:are|is)\s+none\b|\bnothing\s+(?:is\s+)?(?:open|outstanding)\b"
    # Cycle 17 W2: the verb form ("hasn't been recalled", "was never recalled").
    r"|\b(?:hasn't|has\s+not|haven't|have\s+not|wasn't|was\s+not|isn't|is\s+not|never|not)\s+(?:yet\s+)?(?:been\s+|ever\s+been\s+)?recalled\b"
    r"|\bnone\s+of\s+(?:\w+\s+){0,3}(?:have|has|were|was|is|are)\s+(?:been\s+)?recalled\b"
    r"|\b(?:has|have)\s+never\s+had\s+(?:a|any)\s+(?:\w+\s+){0,1}recalls?\b"
    # Cycle 18: plural and past-tense forms ("weren't recalled", "there haven't
    # been any recalls", "hasn't had any recalls").
    r"|\b(?:weren't|aren't|were\s+not|are\s+not)\s+(?:been\s+)?recalled\b"
    r"|\b(?:haven't|have\s+not|hasn't|has\s+not|weren't|were\s+not|wasn't|was\s+not)\s+(?:been\s+|had\s+)?(?:any|a)\s+(?:\w+\s+){0,1}recalls?\b"
    r"|\brecall[- ]free\b|\ball[- ]clear\b|\bsay\s+so\b",
    re.IGNORECASE,
)
# A claim is permitted only when a negation governs the verb that instructs it:
# "NEVER say …", "do not tell …", "must not state …". A negation elsewhere in the
# sentence does not count ("If the results do not match say there are no recalls"
# instructs the claim; review Cycle 16 W1), and a claim with no instructing verb
# before it in its clause is flagged.
_INSTRUCT = re.compile(
    # Cycle 18 W1: "let … know", inform, explain, mention, advise, suggest. `inform`
    # takes no free suffix, so "recall information" in the fallback is not a verb.
    r"\b(?:(?:say|tell|state|claim|report|assert|imply|answer|reassure|confirm|respond|reply|explain|mention|suggest)\w*"
    r"|inform(?:s|ed|ing)?|let(?:s|ting)?|advis(?:e|es|ed|ing))\b",
    re.IGNORECASE,
)
# The word right before the instructing verb must be the negation ("NEVER say",
# "do not tell", "must not state", "don't say"), allowing a filler adverb between
# ("do not ever say"). "If the results do not match say …" is NOT governed: the
# word before "say" is "match".
_GOVERNING_NEGATION = {"never", "not", "don't", "cannot", "can't", "avoid"}
_GOVERNING_PHRASES = (("no", "longer"), ("rather", "than"), ("under", "no", "circumstances"))
# "just" and "simply" are NOT fillers: "Don't just say there are no recalls" permits
# the claim (review Cycle 17 W1).
_FILLER = {"ever", "then", "also"}
# A verb coordinated with an earlier one shares its governor: "NEVER say or imply".
_COORD = {"or", "and/or", "or even", "nor"}
# Review Cycle 19 W1: a bare conjunction after the instructing verb ends its scope
# ("Do not report other models and assure them there are no recalls"), and a
# subordinate "if/when/unless" before the governed verb means the negation belongs
# to the condition, not the instruction.
# Cycle 20 W2: exception words ("do not say anything except that …") and
# conditions after the verb ("when", "unless") also end the verb's scope.
_SCOPE_END = re.compile(
    r"\b(?:and|then|so|because|since|except|besides|beyond|other\s+than|apart\s+from"
    r"|aside\s+from|more\s+than|if|when|whenever|unless|while|given)\b",
    re.IGNORECASE,
)
_SUBORD = re.compile(r"\b(?:if|when|whenever|unless)\b", re.IGNORECASE)
_CLAUSE_BREAK = re.compile(r"[,;:()\u2014\u2013]|\s-\s|\bbut\b|\bthough\b")


# Cycle 20 W1: match at every start position (overlapping), so a long "no …
# recalls" span that begins at an earlier, governed "no" cannot hide a later claim.
_ABSENCE_AT = re.compile(r"(?=(" + _ABSENCE.pattern + r"))", re.IGNORECASE)


def _norm(text: str) -> str:
    return text.replace("\u2019", "'").replace("\u2018", "'")


def _unguarded_absence_claims(rule: str) -> list[str]:
    hits = []
    for sentence in re.split(r"(?<=[.!?])\s+", _norm(rule)):
        for m in _ABSENCE_AT.finditer(sentence):
            clause = _CLAUSE_BREAK.split(sentence[: m.start()])[-1]
            verbs = list(_INSTRUCT.finditer(clause))
            if not verbs:
                hits.append(m.group(1))
                continue
            i = len(verbs) - 1
            while i > 0 and clause[verbs[i - 1].end(): verbs[i].start()].strip().lower() in _COORD:
                i -= 1
            words = [w.strip(".,;:!?\"'").lower() for w in clause[: verbs[i].start()].split()]
            while words and words[-1] in _FILLER:
                words.pop()
            governed = bool(words) and (
                words[-1] in _GOVERNING_NEGATION
                or any(tuple(words[-len(ph):]) == ph for ph in _GOVERNING_PHRASES)
            )
            if governed and _SCOPE_END.search(clause[verbs[-1].end():]):
                governed = False
            if governed and _SUBORD.search(clause[: verbs[i].start()]):
                governed = False
            if not governed:
                hits.append(m.group(1))
    return hits


def _recall_rules() -> dict[str, str]:
    rules: dict[str, str] = {}
    for persona, body in PERSONA_PROMPTS.items():
        for para in re.split(r"\n\s*\n", body):
            para = para.strip()
            m = _LABEL.match(para)
            if m and "recall" in m.group(1).lower():
                rules[f"{persona}:{m.group(1)}"] = " ".join(para.split())
    return rules


def _hard_rule_2_paragraphs() -> dict[str, str]:
    """Rule 2 of both prompt templates reaches every persona, including those with
    no recall rule of their own (review Cycle 15 S2)."""
    from agents.supervisor import supervisor as sup

    out = {}
    for name, text in (("_HARD_RULES", sup._HARD_RULES), ("SYSTEM_PROMPT_TEMPLATE", sup.SYSTEM_PROMPT_TEMPLATE)):
        flat = " ".join(text.split())
        start = flat.index("2. For any question involving a recall")
        end = flat.index(" 3. ", start)
        out[f"{name}:rule 2"] = flat[start:end]
    return out


RULES = {**_recall_rules(), **_hard_rule_2_paragraphs()}


def test_scope_includes_the_known_recall_rules():
    """Anti-vacuity: the computed scope must contain both rules this guard was
    written for, or a relabelled rule would silently fall out of scope."""
    personas = {key.split(":", 1)[0] for key in RULES}
    assert {"fleet_driver", "fleet_owner", "_HARD_RULES", "SYSTEM_PROMPT_TEMPLATE"} <= personas, sorted(RULES)


@pytest.mark.parametrize("key", sorted(RULES))
def test_rule_never_instructs_an_absence_claim(key):
    hits = _unguarded_absence_claims(RULES[key])
    assert not hits, (
        f"{key} instructs the model to assert no recalls ({hits!r}). The platform "
        "has no recall catalog, so it cannot know that. Write a prohibition as "
        "'NEVER say …' or 'do not tell …' directly before the claim. See "
        "issues/2026-09-25-recall-rule-asserts-no-open-recalls/."
    )


@pytest.mark.parametrize("key", sorted(RULES))
def test_rule_carries_the_couldnt_find_fallback(key):
    rule = RULES[key].lower()
    assert re.search(r"(couldn't|could not) find recall information", rule), key
    assert "dealer" in rule, key
    assert "nhtsa.gov/recalls" in rule, key
    assert "vin" in rule, key


class TestMatcher:
    """The matcher itself: the exact live defects must be flagged and the
    permitted prohibition form must not be."""

    @pytest.mark.parametrize("text", [
        "If none of the results apply to their vehicle, tell them there are no "
        "open recalls for their vehicle and STOP.",
        "If none apply, say so clearly and STOP.",
        "Otherwise tell them their vehicle is clear of recalls.",
        "Tell them the vehicle is recall-free.",
        "NEVER list other models, and tell them there are no open recalls for "
        "their vehicle.",
    ])
    def test_flags_absence_claims(self, text):
        assert _unguarded_absence_claims(text)

    @pytest.mark.parametrize("text", [
        # Review Cycle 15 W1: each passed the guard before the matcher was widened.
        "If none apply, tell them there aren't any open recalls.",
        "Tell them the vehicle is not subject to any recall.",
        "If nothing matches, tell them their vehicle has none.",
        "If none apply, don't guess — say there are no open recalls.",
    ])
    def test_flags_the_cycle_15_phrasings(self, text):
        assert _unguarded_absence_claims(text)

    @pytest.mark.parametrize("text", [
        # Review Cycle 16 W1: no clause punctuation, and paraphrases.
        "If the results do not match say there are no recalls.",
        "If they don't apply tell them there are no open recalls.",
        "Don't guess and just say there are no open recalls.",
        "If they press, tell them their vehicle doesn't have any open recalls.",
        "If they press, tell them the vehicle does not have any recalls.",
        "If they press, tell them there are none.",
        "If they press, tell them nothing is outstanding.",
        "If they press, tell them it isn\u2019t subject to any recall.",
        "If they press, tell them there aren\u2019t any open recalls.",
        "If they press, tell them the vehicle isn't under any recall.",
    ])
    def test_flags_the_cycle_16_phrasings(self, text):
        assert _unguarded_absence_claims(text)

    @pytest.mark.parametrize("text", [
        # Review Cycle 17.
        "Don't just say there are no open recalls; also point them to a dealer.",
        "Don't simply say there are no recalls.",
        "If nothing matches, tell them their vehicle hasn't been recalled.",
        "Tell them the vehicle has not been recalled.",
        "Tell them it was never recalled.",
        "Tell them the vehicle isn't recalled.",
        "Tell them you can't confirm the build date but there are no open recalls.",
    ])
    def test_flags_the_cycle_17_phrasings(self, text):
        assert _unguarded_absence_claims(text)

    @pytest.mark.parametrize("text", [
        # Review Cycle 18.
        "Do not report other models and let them know there are no open recalls.",
        "Do not confirm the trim; inform them there are no open recalls.",
        "Don't list other makes and explain that there are no recalls.",
        "Do not answer and say there are no open recalls.",
        "Tell them their vehicles weren't recalled.",
        "Tell them there haven't been any recalls.",
        "Tell them the vehicle hasn't had any recalls.",
        "Tell them the vehicle has not yet been recalled.",
        "Tell them none of their vehicles have been recalled.",
        "Tell them the vehicle has never had a recall.",
    ])
    def test_flags_the_cycle_18_phrasings(self, text):
        assert _unguarded_absence_claims(text)

    @pytest.mark.parametrize("text", [
        # Review Cycle 19 W1: an unlisted verb after a bare conjunction.
        "Do not report other models and assure them there are no open recalls.",
        "Do not suggest a DIY fix and assure them there are no open recalls.",
        "Do not list other makes and point out there are no recalls.",
        "Don't mention other models so note that there are no open recalls.",
        "If the results don't mention their VIN there are no open recalls.",
        "If none apply, tell them there are no open or pending recalls.",
        "If none apply, tell them there are no open NHTSA safety recalls.",
        "If none apply, tell them there are no open, unrepaired recalls.",
    ])
    def test_flags_the_cycle_19_phrasings(self, text):
        assert _unguarded_absence_claims(text)

    @pytest.mark.parametrize("text", [
        # Review Cycle 20.
        "Never say there's no data, say no recalls were found.",
        "If none apply, do not say anything except that there are no open recalls.",
        "Do not say anything other than that there are no recalls.",
        "Do not suggest a DIY fix when there are no open recalls.",
        "Do not report other models and assure them there are no open recalls.",
    ])
    def test_flags_the_cycle_20_phrasings(self, text):
        assert _unguarded_absence_claims(text)

    @pytest.mark.parametrize("text", [
        "If none of the results apply, NEVER say there are no open recalls.",
        "Do not tell the driver there are no recalls.",
        "Say you couldn't find recall information for their vehicle.",
        "NEVER say there aren't any open recalls.",
        "Do not tell them the vehicle doesn't have any recalls.",
        "You must not state that there are none.",
        "NEVER say or imply there are no recalls.",
        "Avoid saying there are no recalls.",
        "Under no circumstances say there are no recalls.",
        "Rather than saying there are no recalls, say you couldn't find recall information.",
        "No longer say the vehicle is recall-free.",
        "NEVER say the vehicle hasn't been recalled.",
        "Never suggest there are no open recalls.",
        "Do not let them think there are no recalls.",
        "Do not then say there are no recalls.",
        "Tell them the vehicle hasn't completed the recall repair yet.",
        "NEVER say nor imply there are no recalls.",
        "If none apply, NEVER say the vehicle has no open recalls: say you couldn't find it.",
    ])
    def test_allows_prohibitions_and_the_fallback(self, text):
        assert not _unguarded_absence_claims(text)
