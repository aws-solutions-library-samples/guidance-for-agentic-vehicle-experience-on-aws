"""Card text: the one rule for a Finding's headline (spec 2026-09-25-avx-own-vehicle-findings T4.3).

The headline is written by ``runner._build_artifact`` and checked by the coverage-verdict
guard in ``agents.health.agent.assess_vehicle_health``. Both call :func:`derive_headline`, so
the guard checks exactly the headline that reaches the Finding.

Rule:

1. Empty text: a generic headline.
2. Longer than 120 characters: the leading complete sentences that fit within 120 characters,
   or — when no full sentence fits — a word-boundary truncation so the headline never ends
   mid-word. Safety verbatim phrases (C0161_VERBATIM_REMEDY, two sentences: "…detected. Pull
   over immediately.") must not be split: when the text carries such a phrase the leading
   sentence that contains it is kept whole even when it exceeds 120 characters, because a
   headline that drops "Pull over immediately" softens the directive.
3. Up to 120 characters and more than one sentence: the first sentence. This is the case
   the demo card showed (routine brakes: "…completed successfully … No open issues.").
   When the caller names phrases that must stay in the headline (the warranty verdict)
   and the first sentence drops one, the first sentence carrying all of them is used.
   When that sentence is the bare verdict token alone (e.g. "denied" with no surrounding
   prose), it is joined to the first substantive sentence so the headline makes sense in a
   push notification: "denied: Brake service from 2025-01-01 is awaiting parts."
4. Otherwise the text itself. A single short sentence is already its own summary; the iOS
   card does not render a detail that repeats the headline, and cutting or re-punctuating
   the sentence to force a difference would only lose meaning.

The agent computes the headline with its verdict phrase and passes it to the runner as
``agent_fields["headline"]``; the runner derives one only when none is supplied.
"""
from __future__ import annotations

import re

_MAX_HEADLINE_CHARS = 120

#: A sentence ends at ". ", "! ", "? " or a line break.
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+|\n+")


def _safety_phrases() -> tuple[str, ...]:
    from agents.health.prompt import C0161_VERBATIM_REMEDY

    return (C0161_VERBATIM_REMEDY,)


def _truncate_at_boundary(text: str, limit: int) -> str:
    """Return up to *limit* characters of *text*, truncated at a word boundary.

    Tries leading complete sentences first: the longest run of complete sentences
    that fits within *limit* characters. Falls back to the last space before *limit*
    so the result never ends mid-word. If the text contains no whitespace before
    *limit*, returns the raw slice.
    """
    if len(text) <= limit:
        return text
    # Longest leading run of whole sentences, cut from the original text so the
    # separators stay as written.
    best = ""
    for m in _SENTENCE_END.finditer(text):
        prefix = text[:m.start()].rstrip()
        if len(prefix) > limit:
            break
        best = prefix
    if best:
        return best
    # Fallback: word boundary.
    truncated = text[:limit]
    last_space = truncated.rfind(" ")
    if last_space > 0:
        return truncated[:last_space]
    return truncated


def derive_headline(finding_text: str, required_phrases: tuple[str, ...] = ()) -> str:
    """Return the Finding headline for *finding_text* (see the module docstring).

    *required_phrases* are phrases that must stay in the headline when they are in the
    text (the DMS warranty verdict). When the first sentence drops one, the first
    sentence that carries all of them is used instead, or the whole text if none does.
    When the only sentence containing the required phrase is the bare phrase itself
    (e.g. the verdict token appears as the first paragraph of the rewritten text),
    it is joined with the next substantive sentence so the headline is meaningful.
    """
    text = (finding_text or "").strip()
    if not text:
        return "Vehicle health assessment."
    if len(text) > _MAX_HEADLINE_CHARS:
        # Safety verbatim phrases must never be split: keep them whole even if > 120.
        safety = _safety_phrases()
        for phrase in safety:
            if phrase not in text:
                continue
            # Find the position of the phrase in the text and take everything up to
            # and including the phrase end.  This preserves the phrase verbatim
            # regardless of sentence-splitting behaviour.
            phrase_end = text.index(phrase) + len(phrase)
            return text[:phrase_end]
        return _truncate_at_boundary(text, _MAX_HEADLINE_CHARS)
    if any(phrase in text for phrase in _safety_phrases()):
        return text
    needed = tuple(p for p in required_phrases if p and p in text)
    sentences = [s.strip() for s in _SENTENCE_END.split(text) if s.strip()]
    for i, sentence in enumerate(sentences):
        if all(p in sentence for p in needed):
            # W1 fix: if this sentence is the bare required phrase (e.g. just "denied"),
            # join it with the next substantive sentence so the headline has meaning.
            if needed and sentence.strip().rstrip(".") == needed[0].strip().rstrip("."):
                next_sentences = [s for s in sentences[i + 1 :] if s.strip()]
                if next_sentences:
                    return f"{sentence}: {next_sentences[0]}"
            return sentence
    return text
