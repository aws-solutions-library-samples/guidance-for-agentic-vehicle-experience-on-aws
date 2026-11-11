"""Narration severity: detect when the assistant's words contradict the classifier.

Used by both runtimes (bidi ``_make_output_callable`` and text ``agentcore_app.invoke``)
to check each assistant turn against the triage classifier's verdict, and to supply
the platform's correction line when they disagree.

Issue: issues/2026-10-05-narrated-severity-not-bound-to-classifier/

Why code and not a prompt rule
------------------------------
``supervisor.py`` ``_HARD_RULES`` already tell the model to narrate only the
classifier's verdict. ``agentic-tiers.md``: "a boundary that exists only in a prompt
will be crossed", and on 2026-09-25 it was: the classifier returned P3 and the
assistant told the driver "this is a P0-level critical fault… stop driving
immediately". This module is the executable check; its tests are the enforcement.

What it can and cannot guarantee
--------------------------------
In voice, audio already spoken cannot be recalled. What this guarantees is that a
contradiction is detected on every turn, the platform shows the classifier's
verdict on screen (never weaker than a stop instruction), the model is told to say
the correction, and one log line records it.

Claim classes
-------------
``stop``    the driver should stop driving now (P0 level)
``urgent``  service soon, still drivable (P1 level)
``routine`` no urgency, fine to keep driving (P2/P3 level)
``not_stop`` a stop phrase negated ("this isn't an emergency"): rules out P0 only

Negation ("not", "never", "no", "...n't", up to three words and an article before the phrase, not across a
conjunction such as "and" or "but", and not
across punctuation) flips a phrase: "it isn't safe to drive" is ``stop``; "it's not urgent" is
``routine``; "this is not an emergency" is ``not_stop``.
"""

from __future__ import annotations

import re
from typing import Iterable, Optional

# ---------------------------------------------------------------------------
# Phrase patterns (regex fragments, case-insensitive, word-bounded)
# ---------------------------------------------------------------------------

_SEP = r"(?:\s+|-)"  # "monitor only" and "monitor-only"

# A pattern is a regex fragment, or a (plain, negated) pair when the affirmative
# form needs an exclusion its negated form must not inherit: "it's critical to keep
# the tires inflated" is advice (no claim), but "it's not critical to stop" on a P0
# is a downplay (review cycle 2).
_STOP_PATTERNS: tuple = (
    (r"critical(?!" + _SEP + r"to\b)", r"critical"),
    # "emergency brake/flashers/..." names equipment.
    r"emergency(?!" + _SEP + r"(?:brake|brakes|flashers|lights|services|contact|kit)\b)",
    r"p0",
    r"stop" + _SEP + r"driving",
    r"do" + _SEP + r"not" + _SEP + r"drive",
    r"don['’]t" + _SEP + r"drive",
    r"unsafe" + _SEP + r"to" + _SEP + r"drive",
    r"pull" + _SEP + r"over" + _SEP + r"(?:right" + _SEP + r")?(?:now|immediately|right" + _SEP + r"away)",
)

_URGENT_PATTERNS: tuple[str, ...] = (
    r"urgent(?:ly)?",
    r"as" + _SEP + r"soon" + _SEP + r"as" + _SEP + r"possible",
    r"within" + _SEP + r"48" + _SEP + r"hours",
    r"p1",
)

_ROUTINE_PATTERNS: tuple[str, ...] = (
    r"(?:safe|fine|okay|ok)" + _SEP + r"to" + _SEP + r"(?:keep" + _SEP + r"|continue" + _SEP + r")?driv(?:e|ing)",
    r"keep" + _SEP + r"driving",
    r"continue" + _SEP + r"driving",
    r"no" + _SEP + r"rush",
    r"(?:it|this|that)" + _SEP + r"can" + _SEP + r"wait",
    r"monitor" + _SEP + r"only",
    r"nothing" + _SEP + r"to" + _SEP + r"worry" + _SEP + r"about",
    r"p2",
    r"p3",
)

# Up to three words between the negation and the phrase, plus an optional
# article, so "didn't flag it as an emergency" is negated but "no warning lights
# mean it's safe to drive" is not. "no doubt" is an intensifier, not a negation.
_NEG = (
    r"(?:\b(?:not|never)\b|\bno\b(?!\s+doubt)|n['’]t\b)"
    r"(?:\s+(?!(?:and|but|or|so|because|if|then|yet)\b)[a-z'’]+){0,3}"
    r"(?:\s+(?:a|an|the))?\s+"
)


def _compile(patterns: Iterable) -> list[tuple[re.Pattern[str], re.Pattern[str]]]:
    out = []
    for p in patterns:
        plain_p, neg_p = p if isinstance(p, tuple) else (p, p)
        plain = re.compile(r"(?<![a-z0-9])" + plain_p + r"(?![a-z0-9])", re.IGNORECASE)
        negated = re.compile(_NEG + r"(" + neg_p + r")(?![a-z0-9])", re.IGNORECASE)
        out.append((plain, negated))
    return out


_STOP = _compile(_STOP_PATTERNS)
_URGENT = _compile(_URGENT_PATTERNS)
_ROUTINE = _compile(_ROUTINE_PATTERNS)

# ---------------------------------------------------------------------------
# Correction copy
# ---------------------------------------------------------------------------
#
# The P0 "under" correction is the catalog P0 directive itself (approved copy).
# The two lines below were approved by the user as written on 2026-10-05.

# P1 "under": the model downplayed a P1.
P1_UNDER_COPY = "Your vehicle's systems rate this as urgent: it needs service within 48 hours."

# "over": the model claimed more urgency than the classifier found. It must never
# say the vehicle is safe to drive: a correction never weakens a stop instruction
# into reassurance (test-enforced).
OVER_COPY = (
    "Your vehicle's systems have not flagged this as critical. If anything about "
    "the vehicle feels unsafe right now, tell me what you're seeing."
)

CORRECTION_LINES: tuple[str, ...] = (P1_UNDER_COPY, OVER_COPY)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def _classes_in(text: str, compiled, plain_class: str, negated_class: str, claims: set[str]) -> None:
    for plain, negated in compiled:
        neg_spans = {m.span(1) for m in negated.finditer(text)}
        if neg_spans:
            claims.add(negated_class)
        for m in plain.finditer(text):
            if m.span() not in neg_spans:
                claims.add(plain_class)


def severity_claims(text: str) -> set[str]:
    """The severity claim classes ``text`` makes: ``stop``, ``urgent``, ``routine``,
    and ``not_stop`` (a stop phrase negated: "not an emergency", "not critical").

    Empty when the text makes no claim. Never raises.
    """
    if not isinstance(text, str) or not text.strip():
        return set()
    claims: set[str] = set()
    # A negated stop phrase ("this isn't an emergency") rules out P0 only. It is
    # consistent with P1, so it is its own class rather than "routine".
    _classes_in(text, _STOP, "stop", "not_stop", claims)
    _classes_in(text, _ROUTINE, "routine", "stop", claims)
    _classes_in(text, _URGENT, "urgent", "routine", claims)
    return claims


def strip_platform_lines(text: str, lines: Iterable[Optional[str]]) -> str:
    """``text`` with every occurrence of the given platform lines removed.

    The model is told to repeat platform lines word for word (the P0 directive, a
    correction). Those words are the platform's, not the model's claim, and a
    correction must not trigger a further correction. Whitespace-insensitive.
    """
    if not isinstance(text, str):
        return ""
    out = " ".join(text.split())
    for line in lines:
        if isinstance(line, str) and line.strip():
            out = out.replace(" ".join(line.split()), " ")
    return out


def narration_conflict(claims: set[str], classification: Optional[str]) -> Optional[str]:
    """``"under"``, ``"over"`` or ``None`` for this turn's claims against the verdict.

    - ``under``: P0 with a ``routine`` or ``not_stop`` claim; P1 with a ``routine`` claim.
    - ``over``: P1 with a ``stop`` claim; P2 or P3 with a ``stop`` or ``urgent`` claim.
    - ``None``: no verdict yet (the triage-error rule governs that), no claim, or a match.
    """
    if not classification or not claims:
        return None
    if classification == "P0" and ({"routine", "not_stop"} & claims):
        return "under"
    if classification == "P1" and "routine" in claims:
        return "under"
    if classification == "P1" and "stop" in claims:
        return "over"
    if classification in ("P2", "P3") and ({"stop", "urgent"} & claims):
        return "over"
    return None


def correction_line(conflict: str, classification: str, p0_directive: str) -> str:
    """The platform's correction for a conflict.

    ``under`` on P0 is ``p0_directive`` (the catalog line, verbatim); ``under`` on P1
    is ``P1_UNDER_COPY``; ``over`` is ``OVER_COPY``.
    """
    if conflict == "under":
        return p0_directive if classification == "P0" else P1_UNDER_COPY
    return OVER_COPY


def check_turn(
    text: str,
    classification: Optional[str],
    platform_lines: Iterable[Optional[str]] = (),
) -> Optional[str]:
    """The conflict for one assistant turn, after removing platform lines."""
    cleaned = strip_platform_lines(text, list(platform_lines) + list(CORRECTION_LINES))
    return narration_conflict(severity_claims(cleaned), classification)


# Hazard words in the DRIVER's speech. Wider than the confirmation matcher
# (triage_keywords), which needs exact phrases ("smoke coming from"): "I smell
# smoke" or "the brakes stopped working" must also keep the model's stop
# instruction from being taken back (review cycle 2). Only REPORTS count:
# questions ("is this critical?", "what does P0 mean"), denials ("there's no
# smoke") and names of warning lamps or app events ("forward collision warning",
# "the app crashed") do not (review cycle 3 W1).
_HAZARD = (
    r"smoke|smoking|fire|flames?|burning"
    r"|accident|crash(?:ed)?(?!\s+(?:warning|alert|detection|sensor))"
    r"|collision(?!\s+(?:warning|alert|avoidance|system|mitigation))"
    r"|airbags?\s+(?:went\s+off|deployed|blew)"
    r"|brakes?\s+(?:fail\w*|gone|went|stopped|not\s+working|won['’]?t|don['’]?t|aren['’]?t)"
    r"|can['’]?t\s+(?:stop|steer|brake)|steering\s+(?:fail\w*|locked|gone|stopped|went)"
    r"|leak\w*\s+(?:fuel|gas)|smell\w*\s+(?:fuel|gas)"
)
_DRIVER_HAZARD = re.compile(r"(?<![a-z])(?:" + _HAZARD + r")(?![a-z])", re.IGNORECASE)
_DRIVER_HAZARD_NEGATED = re.compile(_NEG + r"((?:" + _HAZARD + r"))(?![a-z])", re.IGNORECASE)
# "app/phone/screen ... crashed" is software, not a vehicle crash.
_SOFTWARE_CRASH = re.compile(r"\b(?:app|application|phone|screen|software|system)\b[^.!?]{0,30}\bcrash", re.IGNORECASE)
_QUESTION_START = re.compile(
    r"^(?:is|are|was|were|what|what['’]s|whats|does|do|did|should|shall|can|could|will|would|"
    r"how|why|when|where|which|who|am)\b",
    re.IGNORECASE,
)


def _is_question(sentence: str) -> bool:
    s = sentence.strip()
    return s.endswith("?") or bool(_QUESTION_START.match(s))


def driver_reports_hazard(text: str) -> bool:
    """Whether the driver's words REPORT a hazard or make a stop-class claim.

    Sentence by sentence: questions are skipped (voice transcripts often have no
    "?", so a question word at the start counts too), negated hazards are
    skipped, and a software "crash" is not a vehicle crash.
    """
    if not isinstance(text, str) or not text.strip():
        return False
    for sentence in re.split(r"(?<=[.!?])\s+|\n+", text):
        s = sentence.strip()
        if not s or _is_question(s):
            continue
        negated = {m.span(1) for m in _DRIVER_HAZARD_NEGATED.finditer(s)}
        software = bool(_SOFTWARE_CRASH.search(s))
        for m in _DRIVER_HAZARD.finditer(s):
            if m.span() in negated:
                continue
            if software and m.group().lower().startswith("crash"):
                continue
            return True
        if "stop" in severity_claims(s):
            return True
    return False
