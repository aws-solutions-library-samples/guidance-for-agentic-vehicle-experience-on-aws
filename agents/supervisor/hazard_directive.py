"""Directive first: when a driver reports fire, smoke or brake loss, the platform's
safety directive is the first thing the driver gets, and nothing doubts the report.

Issue: issues/2026-10-06-fire-report-doubted-before-directive/ (backlog row "Fire report
doubted"). On 2026-10-05 a driver said "my engine is on fire"; the classifier said P3, the
platform injected the confirmation question ("Do you see flames or heavy smoke… yes or
no?"), and the assistant's first words were "I don't have live sensor data to confirm
flames or heavy smoke from your vehicle at this moment." The directive came only after the
driver said "yes".

Why code and not a prompt rule
------------------------------
A denylist of doubting phrases plus a "directive present" check both pass a reply that
doubts first and gives the directive later, and the model can doubt in words no list
names. The property is ORDER: nothing the model says reaches the driver before the
directive. This module holds the pieces both runtimes use to enforce that:

- ``reported_hazard``: does the driver's text REPORT fire/smoke or brake loss?
- ``leads_with``: does a model text begin with the directive (word for word, ignoring
  punctuation, case, "911" vs "nine-one-one" and an optional leading first name)?
- ``DirectiveFirstGate``: the voice gate. From the report on, model audio and
  transcript segments are held until the model's planned speech (Nova Sonic's
  speculative text, which arrives before the segment's audio) is known to begin with
  the directive; a segment that does not is dropped, never forwarded.
- ``lead_reply_with_directive``: the text rule. The reply begins with the catalog
  directive; the model's words follow only if they led with it.

What it cannot guarantee
------------------------
Model words AFTER a directive-led segment are not checked (the platform note tells the
model not to question the report). The gate holds audio before it is sent; it cannot
recall audio already played. If the model never leads with the directive, the driver
gets the on-screen directive and no model speech until the gate closes (driver's next
turn or ``GATE_LIMIT_SECONDS``).
"""

from __future__ import annotations

import re
from typing import Any, Iterable, Optional

from agents.supervisor.dtc_response_catalog import (
    P0_TEMPLATE_PRECEDENCE,
    RESPONSE_TEMPLATES,
    p0_directive,
    safe_first_name,
)
from agents.supervisor.triage_keywords import EMERGENCY_CATEGORIES, EmergencyCategory

# The categories whose report is never questioned: the user named fire, smoke and
# brake loss (2026-10-05). Crash, steering and visibility keep the confirmation
# question for now (their phrase lists are looser, e.g. "i hit"); see the issue.
DIRECTIVE_FIRST_CATEGORIES: frozenset[str] = frozenset({"fire_or_smoke", "brake_failure"})

# How long the voice gate waits for a model segment that leads with the directive.
GATE_LIMIT_SECONDS = 30.0

# Most events the gate holds while a segment's speculative text is a strict prefix
# of the directive (about 25-50 s of Nova audio). Past it the held segment is dropped.
GATE_MAX_HELD_EVENTS = 1200

# Words the platform tells the model about each category. They must not themselves
# read as a driver's report, or an echo of the platform note could re-trigger.
CATEGORY_WORDS: dict[str, str] = {
    "fire_or_smoke": "fire or smoke",
    "brake_failure": "brake loss",
    "crash": "a collision",
    "steering_failure": "steering loss",
    "visibility_loss": "lost visibility",
}

_BY_NAME = {c.name: c for c in EMERGENCY_CATEGORIES}

# ---------------------------------------------------------------------------
# What counts as the driver REPORTING fire, smoke or brake loss
# ---------------------------------------------------------------------------
#
# Not the confirmation matcher's phrase list (triage_keywords): those phrases were
# written to trigger a yes/no question on the first turn, and some are loose ("can't
# stop" matches "I can't stop by the dealer until Friday"; "smoke coming from" matches
# "the car in front has smoke coming from its tailpipe"). Here a report triggers the
# directive, a P0 ticket and the decision row /escalate honours, on any turn, so a
# phrase must say the hazard is in THIS vehicle (review cycle 2 W1), and the natural
# ways drivers say it must be covered ("my car's on fire", "I smell smoke", "my brakes
# went out"; review cycle 2 W2). Someone else's vehicle, something heard about, a past
# event, "smoke" as a verb and "brakes" after a preposition are not reports (review
# cycle 3 W1). Exclusions apply to the match's own clause only. A loose match the
# patterns below reject still reaches the confirmation question on the first turn, as
# before the fix (pinned by a test).
#
# What comes AFTER the hazard word: a denylist of idioms, not an allowlist of
# continuations (review cycle 4 C1). Cycle 3 anchored the loose words ("burning",
# "went out", "failing") to the end of their clause or to a list of twelve next-words,
# which killed the false positives and took a dozen genuine reports with them: "my
# brakes went out going down the hill" matched nothing at all. The two sets are not
# symmetric — the ways a driver narrates an emergency are open-ended, while the
# ordinary uses of these words are a small closed set of idioms ("burning oil",
# "failed inspection", "out of warranty", "quit squeaking"). So the idioms are named
# and everything else is a report. The exception is a transitive hazard word, where
# the continuation IS the meaning: "won't stop beeping" is about the beeping, so
# "stop" keeps its end-of-clause anchor.
#
# Speech-to-text shape (review cycle 4 C2): the transcript may carry no punctuation,
# no apostrophes and doubled words. Every contraction and possessive below spells the
# apostrophe optional, and `_normalise` collapses a doubled word, so "my cars on fire"
# and "my car's on fire" are the same report. Nova's punctuation of a panicking driver
# must not decide whether the directive is shown.

_OWN = r"(?:my|the|our|this)"
_VEH = (
    r"(?:car|truck|vehicle|van|minivan|suv|ev|bus|rig|cab|tractor|trailer|jeep|pickup|sedan"
    r"|coupe|hatchback|wagon|rv|camper|motorhome)"
)
_PART = (
    r"(?:hood|bonnet|engine\s+compartment|engine|motor|dash|dashboard|vents?|cabin|interior"
    r"|steering(?:\s+(?:column|wheel))?|glove\s*box|wheel\s+wells?|wheels?|tires?|tyres?|brakes?"
    r"|battery(?:\s+pack)?|undercarriage|floor(?:boards?)?|seats?|back\s+seat|trunk|charging\s+port"
    r"|front\s+end|back\s+end|rear)"
)
# The apostrophe is optional everywhere it appears below: speech-to-text drops it
# ("my cars on fire", "my brakes dont work"). Cycle 4 C2 — it used to be required
# here and optional in "can['’]?t", which decided reports by punctuation.
_AP = r"['’]?"
_BE = rf"(?:\s*{_AP}s|\s+is|\s+are|\s+was|\s+were|\s+has|\s+have)?"
_ADV = r"(?:\s+(?:still|now|literally|really|actually|completely|just|totally|all))*"
# End of the clause. Kept only where the hazard word is transitive, so a continuation
# changes what the sentence is about ("won't stop beeping", "I can't stop thinking").
_END = r"(?=[\s!.?]*$)"
# For "won't stop" / "can't stop the car": a place after "stop" is where the driver
# means to stop ("isn't stopping at the charger"), so only the end of the clause or
# "at all" counts.
_END_STOP = r"(?=[\s!.?]*$|\s+(?:at\s+all|again|now|anymore)\b)"
# Idiom denylists: what an ORDINARY sentence puts after the hazard word. Everything
# else is narration and counts as a report (cycle 4 C1).
_NOT_INSPECTION = r"(?!\s+(?:the\s+|an?\s+|its\s+|my\s+)?(?:inspection|test|check|emissions|smog))"
# "will the brakes fail if I keep driving" asks about the future, not now.
_NOT_CONDITIONAL = r"(?!\s+(?:if|when|unless|whenever)\b)"
# "my brakes are out of alignment", "out of warranty".
_NOT_OF = r"(?!\s+of\b)"
# "my brakes quit squeaking after the service".
_NOT_NOISE = r"(?!\s+(?:squeak|squeal|grind|mak|stick|rub|vibrat|shak|smok|work)\w*\b)"
# "my engine is burning oil", "burning through gas", "burning hot", "burning up inside".
_NOT_BURN_OBJ = r"(?!\s+(?:oil|gas|gasoline|fuel|through|hot|hotter|rubber|smell|clean|up\s+inside))"
# "my car is smoking hot in this sun".
_NOT_SMOKE_IDIOM = r"(?!\s+(?:hot|deal|section))"
# "brakes" as the object of a preposition is not the subject of the sentence ("the
# grinding in the brakes is gone now").
_NOT_OBJECT = r"(?<!\bin\s)(?<!\bof\s)(?<!\bfrom\s)(?<!\bin\sthe\s)(?<!\bin\smy\s)(?<!\bof\sthe\s)(?<!\bof\smy\s)(?<!\bfrom\sthe\s)(?<!\bfrom\smy\s)"
# "smoke" as a verb ("can I smoke in the car") is not smoke.
_NOT_VERB = r"(?<!\bi\s)(?<!\bwe\s)(?<!\byou\s)(?<!\bto\s)(?<!\bcan\s)(?<!\bmay\s)(?<!\bcan\si\s)(?<!\bcan\swe\s)(?<!\bmay\si\s)"

_BURNING = rf"burning{_NOT_BURN_OBJ}(?:\s+up)?(?!\s+inside)"
_SMOKING = rf"smoking{_NOT_SMOKE_IDIOM}"
_HAZARD_STATE = (
    rf"(?:on\s+fire|in\s+flames|caught\s+(?:on\s+)?fire|catching\s+(?:on\s+)?fire"
    rf"|{_BURNING}|{_SMOKING}|(?:filling\s+(?:up\s+)?with|full\s+of)\s+smoke)"
)

_FIRE = [
    rf"(?:^|\b{_OWN}\s+){_VEH}{_BE}{_ADV}\s+{_HAZARD_STATE}",
    rf"(?:^|\b{_OWN}\s+){_PART}{_BE}{_ADV}\s+{_HAZARD_STATE}",
    rf"\bit(?:{_AP}s|\s+is|\s+was){_ADV}\s+(?:on\s+fire|in\s+flames)\b",
    r"\bit\s+(?:just\s+)?(?:caught|is\s+catching)\s+(?:on\s+)?fire\b",
    rf"\b(?:i\s+(?:can\s+)?see|there(?:{_AP}s|\s+is|\s+are)|seeing)\s+(?:some\s+|big\s+|huge\s+)?flames"
    rf"(?:{_END}|\s+(?:coming|shooting|under|from|in|inside|out)\b)",
    rf"\bflames?\s+(?:are\s+|is\s+)?(?:coming\s+|shooting\s+)?(?:out\s+of|from\s+under|from|under|in)\s+(?:(?:the|my|our)\s+)?(?:{_VEH}|{_PART})\b",
    rf"\b(?:a\s+)?fire\s+(?:in|under|inside|from|at|on)\s+(?:(?:the|my|our)\s+)?(?:{_VEH}|{_PART})\b",
    rf"\b(?:{_VEH}|engine|battery|electrical|wheel|tire)\s+fire{_END}",
    rf"{_NOT_VERB}\bsmoke\b(?:\s+(?:is|was|keeps|are))?(?:\s+(?:coming|pouring|billowing|rising|blowing|leaking|filling|getting))?(?:\s+(?:out|up|in))?"
    rf"\s+(?:of|from\s+under|from|under|in|inside|into|through)\s+(?:(?:the|my|our)\s+)?(?:{_VEH}|{_PART})\b",
    rf"\bsmoke\s+(?:is\s+|was\s+)?(?:filling|pouring\s+into|coming\s+into|getting\s+into)\s+(?:up\s+)?(?:the|my|our)\s+(?:{_VEH}|{_PART})\b",
    r"\b(?:i|we)\s+(?:can\s+)?smell\s+(?:some\s+|a\s+lot\s+of\s+)?(?:smoke|burning|fire|something\s+burning)\b",
    rf"\b(?:i|we)\s+(?:can\s+)?see\s+(?:some\s+|a\s+lot\s+of\s+)?(?:smoke|fire)(?:{_END}|\s+(?:coming|pouring|from|under|in|inside)\b)",
    rf"\b(?:burning\s+smell|smells?\s+like\s+(?:something\s+)?(?:smoke|burning|fire)|something(?:{_AP}s|\s+is)\s+burning|smell\s+of\s+(?:smoke|burning))\b",
    rf"\bthere(?:{_AP}s|\s+is)\s+(?:a\s+lot\s+of\s+|some\s+|so\s+much\s+)?smoke\b",
    r"\bsmoke\s+everywhere\b",
    r"^\s*(?:fire|flames|smoke)[\s!.]*$",
    rf"\bsmoke\b(?:\s+(?:is|was|keeps))?\s+(?:coming|pouring|billowing)(?:\s+out)?{_END}",
    rf"\bthere(?:{_AP}s|\s+is)\s+a\s+fire{_END}",
]
_NEG_AUX = (
    rf"(?:do\s+not|don{_AP}t|does\s+not|doesn{_AP}t|aren{_AP}t|are\s+not|isn{_AP}t|is\s+not"
    rf"|won{_AP}t|will\s+not)"
)
_BRAKE = [
    rf"{_NOT_OBJECT}\bbrakes?(?:\s+pedal)?(?:\s+(?:have|has|are|is|just|keep|kept|totally|completely|suddenly))*\s+"
    rf"(?:failed\b{_NOT_INSPECTION}{_NOT_CONDITIONAL}|failing\b{_NOT_INSPECTION}{_NOT_CONDITIONAL}"
    rf"|fail\b{_NOT_INSPECTION}{_NOT_CONDITIONAL}"
    rf"|went\s+out\b{_NOT_OF}|out\b{_NOT_OF}|stopped\s+working\b|quit\b(?:\s+working\b)?{_NOT_NOISE}"
    rf"|gave\s+out\b{_NOT_OF}|gone\b|not\s+working\b"
    rf"|{_NEG_AUX}\s+(?:work(?:ing)?|respond(?:ing)?|do(?:ing)?\s+anything"
    rf"|stop(?:ping)?(?:\s+(?:the|my|our)\s+{_VEH})?{_END_STOP}))",
    r"\b(?:lost|losing)\s+(?:my\s+|the\s+|all\s+)?brakes?\b(?!\s+(?:fluid|pads?|dust|lights?|rotors?|lines?))",
    r"\bno\s+brakes\b",
    rf"\b(?:do\s+not|don{_AP}t|does\s+not|doesn{_AP}t|didn{_AP}t|did\s+not)\s+have\s+"
    rf"(?:any\s+|no\s+|working\s+|my\s+|the\s+)?brakes\b",
    r"\bbrake\s+failure\b(?!\s+(?:warning|light|indicator|message|recall|alert))",
    r"\bbrake\s+pedal(?:\s+(?:goes|went|is\s+going|sank|sinks|drops?|dropped))?\s+(?:all\s+the\s+way\s+)?(?:to|into)\s+the\s+floor\b",
    r"\bpedal\s+(?:goes|went|is\s+going|sank|sinks|drops?|dropped)\s+(?:all\s+the\s+way\s+)?(?:to|into)\s+the\s+floor\b",
    r"\b(?:pressing|pushing|stepping\s+on|standing\s+on)\s+(?:the\s+|my\s+)?brakes?(?:\s+pedal)?\s+"
    r"(?:does|did|is\s+doing)\s+(?:nothing|not(?:hing)?\s+do\s+anything)\b",
    rf"\b(?:can{_AP}t|cannot|can\s+not|couldn{_AP}t)\s+stop\s+(?:(?:the|my|our)\s+)?{_VEH}{_END_STOP}",
    rf"\b{_OWN}\s+{_VEH}\s+(?:won{_AP}t|will\s+not|isn{_AP}t|is\s+not|doesn{_AP}t|does\s+not)\s+stop(?:ping)?{_END_STOP}",
    rf"\b(?:can{_AP}t|cannot|can\s+not)\s+brake\b",
    rf"\b(?:can{_AP}t|cannot|can\s+not)\s+(?:stop|slow\s+down){_END}",
]

# The hazard is somewhere else, or not real. Checked only in the clause of the match,
# and not when the match itself names the driver's own vehicle ("another car hit me
# and now my car is on fire" is a report).
# The hazard is somewhere else, or not real. Checked only in the clause of the match,
# and not when the clause up to the match names the driver's own vehicle ("another car
# hit me and now my car is on fire" is a report).
_ELSEWHERE = (
    rf"\b(?:{_VEH}s?\s+(?:in\s+front|ahead|next\s+to|beside|behind|across)|in\s+front\s+of\s+(?:me|us)"
    rf"|another\s+{_VEH}|(?:the\s+)?other\s+{_VEH}s?|that\s+{_VEH}|those\s+{_VEH}s|its\s+(?:tailpipe|exhaust|hood|engine)"
    r"|(?:wild|forest|brush|house|grass|camp)\s*fires?|fireplaces?|bonfires?|buildings?|houses?|barbecues?|bbqs?"
    r"|grills?|cigarettes?|cigars?|vap(?:e|es|ing)|recalls?|over\s+there|ahead|outside"
    r"|on\s+the\s+(?:hill|hills|hillside|horizon|news|tv)"
    # someone else's vehicle, something heard about, or a past event
    rf"|(?:friend|neighbou?r|brother|sister|mom|dad|mother|father|wife|husband|son|daughter|cousin|coworker|boss){_AP}s"
    r"|(?:read|heard)\s+(?:about|of)|last\s+(?:week|month|year|time)|yesterday|years?\s+ago)\b"
)
# "what do I do if…" asks about a hazard that is not happening. It only cancels a
# report it GOVERNS: within `_HYPO_GAP_WORDS` words of the hazard phrase. A hedge with
# its own clause in between does not ("i dont know if i should get out the engine is on
# fire" is a report — cycle 4 W2, and unpunctuated is the shape voice actually sends).
_HYPOTHETICAL = (
    r"\b(?:if|in\s+case|suppose|supposing|whenever|hypothetically"
    r"|what\s+(?:happens|does\s+it\s+mean|do\s+i\s+do|should\s+i\s+do|to\s+do)\s+(?:when|in\s+an?|during|for))\b"
)
_HYPO_GAP_WORDS = 2

# Four guards used to live here, added across security review cycles 1 and 2 to stop
# relayed, quoted, attributed and possibility wording ("the dashboard says my car is on
# fire", "did you say my car is on fire", "my car is on fire according to the app", "can
# my brakes fail", "if im driving and the engine is on fire what do i do"). They are
# GONE, on the user's decision of 2026-10-06, and the reason is worth keeping:
#
# Every one of them existed to stop a false trigger from BOOKING — a P0 ticket, the
# verdict="reported" decision row `/escalate` verifies, the session P0 pin. Each
# narrowing then cost genuine reports, silently: cycle 4 found 26 dropped, and cycle 2
# of the security review found the verb-keyed quotation guard swallowing "i can tell my
# car is on fire". Six cycles swung between the two failures because one detector was
# deciding two things whose costs differ by three orders of magnitude.
#
# The actions are now gated elsewhere (`bidi_app._driver_reported_hazard_followup`:
# the classifier's own P0, or the driver's yes). So a false trigger here costs one
# unneeded safety sentence and a held model turn — and recall is what matters. What
# remains are the guards that still earn their place even at that price: a denial is
# not a report (showing an emergency directive to a driver who just said "there's no
# smoke" is actively confusing), a hypothetical that directly governs the phrase, the
# clause being about another vehicle or somewhere else, and the ordinary-maintenance
# idioms ("burning oil", "failed inspection", "won't stop beeping") — those are
# everyday vehicle questions, and answering one with an emergency directive while
# holding back the answer is a cost the driver notices.
_CLAUSE_SPLIT = r",|;|:|\s[—–-]\s|\s+(?:and|but|so|then|because|while)\s+"

_REPORTS = [(re.compile(p), "fire_or_smoke") for p in _FIRE] + [(re.compile(p), "brake_failure") for p in _BRAKE]
_ELSEWHERE_RE = re.compile(_ELSEWHERE)
_HYPOTHETICAL_RE = re.compile(_HYPOTHETICAL)
_CLAUSE_SPLIT_RE = re.compile(_CLAUSE_SPLIT)
# The driver's OWN vehicle, which outranks an exclusion noun in the same clause. Not
# "my friend's brakes" or "my neighbour's car" — those are the exclusion. Searched over
# the clause up to the end of the match, not over the match text, because a brake match
# starts at "brakes" and so never contains "my" (cycle 4 C1: that is why "my brakes went
# out on the hill" was discarded as being about a hill).
_OWNED = re.compile(
    r"\b(?:my|our)\b(?!\s+(?:friend|neighbou?r|brother|sister|mom|dad|mother|father|wife"
    rf"|husband|son|daughter|cousin|coworker|boss){_AP}s?\b)",
)

# A denial is a negation that governs the hazard phrase: right before it ("no smoke
# coming from the hood"), or through a perception, possession or speech word ("I don't
# see flames", "I don't have any smoke coming from…", "I didn't say the car is on
# fire", "there's no sign of smoke…"). Other negations do not deny the hazard ("I can't
# believe my car is on fire", "I don't know why there's smoke coming from the hood"),
# and "no" after an interjection or another "no" is an exclamation ("oh no the engine
# is on fire", "no no no the car is on fire"). A negation INSIDE a phrase ("brakes
# aren't working", "no brakes") is part of the report. When unsure, it is a report:
# showing the directive is the protective error.
_GOVERNING = (
    r"(?:see|seeing|seen|saw|smell|smelling|smelled|smelt|notice|noticed|noticing|have|has|had"
    r"|got|getting|any|more|visible|real|actual|sign|signs|of|say|said|mean|meant|think|thought"
    r"|feel|feeling|hear|heard)"
)
_NEGATION_BEFORE = re.compile(
    r"(?:\b(?:not|never|without)\b|(?<!\boh )(?<!\buh )(?<!\bah )(?<!\bno )\bno\b|n['’]?t\b)"
    rf"(?:(?:\s+{_GOVERNING}){{1,3}}(?:\s+(?:a|an|the|there['’]?s|there\s+is|there\s+are))?)?\s*$",
)

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?;])\s+|\n+")
_WORD_RE = re.compile(r"[a-z0-9]+")
# A doubled word is a speech-to-text artefact ("my my car is on fire", "my car is is on
# fire"), not meaning (cycle 4 W1). "no" is excluded: a repeated "no" is an exclamation
# and is deliberately a report, not a denial ("no no smoke coming from the hood").
_DOUBLED = re.compile(r"\b(?!no\b)(\w+(?:'\w+)?)\s+\1\b")


def _normalise(text: str) -> str:
    out = " ".join(text.lower().replace("’", "'").split())
    for _ in range(3):  # "my my my car" collapses in passes
        new = _DOUBLED.sub(r"\1", out)
        if new == out:
            break
        out = new
    return out


def _sentence_reports(sentence: str) -> list[str]:
    """Category names this sentence reports. Judged clause by clause: a match counts
    unless, in its own clause, a negation governs it, a hypothetical directly governs
    it, or (when the clause up to the match does not name the driver's own vehicle)
    the clause is about something else.

    Tuned for RECALL (user decision, 2026-10-06): this decides the WORDS, where a
    false trigger costs one unneeded safety sentence. The actions are gated on the
    classifier's P0 or the driver's yes, in `bidi_app`."""
    found: list[str] = []
    for clause in _CLAUSE_SPLIT_RE.split(sentence):
        clause = clause.strip()
        if not clause:
            continue
        for pattern, name in _REPORTS:
            if name in found:
                continue
            for m in pattern.finditer(clause):
                before = clause[: m.start()]
                # A match that names the driver's own vehicle is its own clause, not
                # the object of an upstream perception verb: "i cant see my car is on
                # fire" reports, while "i cant see any smoke" denies (cycle 4 W1).
                own = _OWNED.match(m.group())
                if not own and _NEGATION_BEFORE.search(before):
                    continue
                if _governing_hypothetical(before):
                    continue
                if not _OWNED.search(clause[: m.end()]) and _ELSEWHERE_RE.search(clause):
                    continue
                found.append(name)
                break
    return found


def _governing_hypothetical(before: str) -> bool:
    """True when a hypothetical frame ends within ``_HYPO_GAP_WORDS`` words of the
    hazard phrase, so it is that phrase the driver is supposing about."""
    last = None
    for m in _HYPOTHETICAL_RE.finditer(before):
        last = m
    if last is None:
        return False
    return len(_WORD_RE.findall(before[last.end():])) <= _HYPO_GAP_WORDS


def reported_hazard(text: Any) -> Optional[EmergencyCategory]:
    """The fire/smoke or brake-loss category ``text`` REPORTS, else ``None``.

    Sentence by sentence. Questions count ("is there smoke coming from the hood?"):
    the driver is asking about something they see. When both are reported, the one
    whose directive is more protective wins (fire). Never raises.
    """
    try:
        if not isinstance(text, str) or not text.strip():
            return None
        names: set[str] = set()
        for sentence in _SENTENCE_SPLIT.split(_normalise(text)):
            if sentence:
                names.update(_sentence_reports(sentence))
        if not names:
            return None
        cats = [_BY_NAME[n] for n in names]
        return min(cats, key=lambda c: P0_TEMPLATE_PRECEDENCE.index(c.p0_template))
    except Exception:  # noqa: BLE001
        return None


# Number words, so "911" and "nine-one-one", "50" and "fifty" compare equal.
_NUMBER_WORDS: dict[str, tuple[str, ...]] = {
    "911": ("nine", "one", "one"),
    "50": ("fifty",),
}
_WORD = re.compile(r"[a-z0-9]+(?:['’][a-z]+)?")


def words(text: Any) -> list[str]:
    """Lower-case words of ``text`` with punctuation removed and numbers spelled out."""
    if not isinstance(text, str):
        return []
    out: list[str] = []
    for tok in _WORD.findall(text.lower().replace("’", "'")):
        out.extend(_NUMBER_WORDS.get(tok, (tok,)))
    return out


def directive_core(directive: str) -> list[str]:
    """The directive's words without a leading "{name}, "."""
    for template in RESPONSE_TEMPLATES.values():
        first = template["first_sentence"]
        if directive == first[:1].upper() + first[1:] or directive.endswith(", " + first):
            return words(first)
    return words(directive)


def _match(got: list[str], core: list[str]) -> str:
    if len(got) >= len(core):
        return "match" if got[: len(core)] == core else "no"
    return "prefix" if got == core[: len(got)] else "no"


def leads_with(text: Any, directive: str, name: Optional[str] = None) -> str:
    """``"match"`` if ``text`` begins with the directive's words, ``"prefix"`` if
    ``text`` is so far only the start of them, ``"no"`` otherwise.

    An optional leading first name (the plain name the directive is personalised with,
    or none) is allowed before the directive's words; nothing else is.
    """
    return _leads(words(text), directive_core(directive), words(safe_first_name(name)))


def _leads(got: list[str], core: list[str], name_words: list[str]) -> str:
    results = [_match(got, core)]
    if name_words:
        n = len(name_words)
        if got[:n] == name_words:
            results.append(_match(got[n:], core))
        elif got == name_words[: len(got)]:
            results.append("prefix")
    for r in ("match", "prefix"):
        if r in results:
            return r
    return "no"


def _split_after_directive(text: str, core: list[str], name_words: list[str]) -> Optional[str]:
    """The part of ``text`` after its leading directive words, or ``None`` if it does
    not lead with them."""
    tokens: list[tuple[str, int]] = []  # (word, end offset in text)
    for m in _WORD.finditer(text.lower().replace("’", "'")):
        for w in _NUMBER_WORDS.get(m.group(), (m.group(),)):
            tokens.append((w, m.end()))
    got = [w for w, _ in tokens]
    for skip in ([len(name_words)] if name_words and got[: len(name_words)] == name_words else []) + [0]:
        if got[skip: skip + len(core)] == core and len(got) >= skip + len(core):
            end = tokens[skip + len(core) - 1][1]
            return text[end:].lstrip(" \t\r\n.,;:!?—–-")
    return None


def lead_reply_with_directive(reply: Any, directive: str, name: Optional[str] = None) -> str:
    """The text runtime's reply for a turn that reports a hazard.

    Starts with ``directive`` exactly as the catalog writes it. The model's words follow
    only if its reply led with the directive (they replace nothing before it, because
    there was nothing); otherwise the reply is the directive alone. Never raises.
    """
    try:
        if not isinstance(reply, str) or not reply.strip():
            return directive
        rest = _split_after_directive(
            reply, directive_core(directive), words(safe_first_name(name)),
        )
        if rest is None:
            return directive
        return f"{directive}\n\n{rest}" if rest else directive
    except Exception:  # noqa: BLE001
        return directive


def hazard_directive(
    category: EmergencyCategory,
    first_name: Optional[str] = None,
    other_templates: Iterable[Optional[str]] = (),
) -> str:
    """The catalog directive for a reported hazard: the most protective of the
    category's template and ``other_templates`` (e.g. the P0 line the faults call for)."""
    faults = [{"template_id": category.p0_template}]
    faults.extend({"template_id": t} for t in other_templates if t in RESPONSE_TEMPLATES)
    return p0_directive(faults, first_name)


def template_of(directive: Any) -> Optional[str]:
    """The template id whose first sentence ``directive`` is, or ``None``."""
    if not isinstance(directive, str):
        return None
    for template_id, template in RESPONSE_TEMPLATES.items():
        first = template["first_sentence"]
        if directive == first[:1].upper() + first[1:] or directive.endswith(", " + first):
            return template_id
    return None



class DirectiveFirstGate:
    """Voice output gate armed when a driver reports a hazard.

    Feed it every assistant event in order: ``"text"`` (speculative transcript: the
    planned speech, which Nova Sonic sends before the segment's audio), ``"audio"``
    and ``"final"`` (the transcript of what was spoken, after the audio). ``feed``
    returns the events to deliver now, in order.

    A segment starts with a speculative text block (consecutive ``"text"`` events) and
    its audio covers the whole block, so the decision is made on the block's words
    from its first word:

    - A segment that begins with the directive opens the gate: its held events and
      everything after are delivered.
    - A segment that is so far only the start of the directive is held; more text in
      the block, or the next segment's text, continues it (a sentence can arrive in
      pieces).
    - Any other segment is dropped: its text, audio and final transcript. "Okay. This
      is an emergency…" is dropped too: nothing may come before the directive.
    - Audio or a final transcript with no speculative text since the report belongs to
      no segment the gate has seen and is dropped.

    The gate stops gating when it opens, on ``close`` (the driver's next turn), or once
    ``limit_s`` has passed, at the next segment boundary so a dropped segment is never
    cut in half. A pure state machine: no I/O, no clock of its own.
    """

    def __init__(
        self,
        directive: str,
        first_name: Optional[str] = None,
        *,
        started_at: float,
        limit_s: float = GATE_LIMIT_SECONDS,
        max_held: int = GATE_MAX_HELD_EVENTS,
    ) -> None:
        self.directive = directive
        self._core = directive_core(directive)
        self._name = words(safe_first_name(first_name))
        self.started_at = started_at
        self.limit_s = limit_s
        self.max_held = max_held
        self.opened = False
        self.closed_reason: Optional[str] = None
        self._mode = "idle"  # idle | hold | drop
        self._last_kind: Optional[str] = None
        self._candidate: list[str] = []
        self._held: list[tuple[str, Any]] = []
        self.dropped_segments = 0
        self.dropped_events = 0
        self.unattributed_events = 0

    @property
    def active(self) -> bool:
        return not self.opened and self.closed_reason is None

    def close(self, reason: str) -> None:
        """Stop gating without the model having led with the directive."""
        if self.active:
            self._discard_held()
            self.closed_reason = reason

    def retarget(self, directive: str) -> None:
        """The platform now shows a more protective directive: the model must lead with
        that one. A held start of the old one is dropped."""
        if not self.active:
            return
        self.directive = directive
        self._core = directive_core(directive)
        if self._mode == "hold":
            self._discard_held()
            self.dropped_segments += 1
            self._mode = "drop"
        self._candidate = []

    def interrupted(self) -> None:
        """The model was interrupted: a held segment will never be completed."""
        if not self.active:
            return
        if self._mode == "hold":
            self._discard_held()
            self.dropped_segments += 1
        self._mode = "idle"
        self._candidate = []
        self._last_kind = None

    def _discard_held(self) -> None:
        self.dropped_events += len(self._held)
        self._held = []

    def _drop(self) -> list:
        self.dropped_events += 1
        return []

    def _hold(self, item: tuple[str, Any]) -> list:
        self._held.append(item)
        if len(self._held) > self.max_held:
            self._discard_held()
            self.dropped_segments += 1
            self._mode = "drop"
            self._candidate = []
        return []

    def feed(self, kind: str, event: Any, now: float) -> list[tuple[str, Any]]:
        item = (kind, event)
        if not self.active:
            return [item]
        new_block = kind == "text" and self._last_kind != "text"
        self._last_kind = kind

        if now - self.started_at >= self.limit_s:
            if self._mode in ("drop", "hold") and not new_block:
                # Finish the segment in hand first, so it is never half delivered.
                if self._mode == "hold":
                    self._discard_held()
                    self.dropped_segments += 1
                    self._mode = "drop"
                return self._drop()
            self._discard_held()
            self.closed_reason = "timeout"
            return [item]

        if kind != "text":
            if self._mode == "hold":
                return self._hold(item)
            if self._mode == "idle":
                self.unattributed_events += 1
                return self._drop()
            if kind == "final":
                self._mode = "idle"  # the dropped segment is over
            return self._drop()

        got = words(event.get("text") if isinstance(event, dict) else "")
        if self._mode == "drop" and not new_block:
            return self._drop()  # more text of a rejected block
        if self._mode == "hold":
            state = _leads(self._candidate + got, self._core, self._name)
            if state == "match":
                return self._open(item)
            if state == "prefix":
                self._candidate += got
                return self._hold(item)
            # The held start did not become the directive.
            self._discard_held()
            self.dropped_segments += 1
            if not new_block:
                self._mode = "drop"
                self._candidate = []
                return self._drop()
        # A new segment, judged from its first word.
        state = _leads(got, self._core, self._name)
        if state == "match":
            return self._open(item)
        if state == "prefix":
            self._mode = "hold"
            self._candidate = got
            return self._hold(item)
        self._mode = "drop"
        self._candidate = []
        self.dropped_segments += 1
        return self._drop()

    def _open(self, item: tuple[str, Any]) -> list:
        out = self._held + [item]
        self._held = []
        self._candidate = []
        self.opened = True
        return out
