"""Recall eval cases must reject "no open recalls" answers and accept the honest fallback.

Regression guard for issues/2026-09-25-recall-rule-asserts-no-open-recalls/. The
platform has no recall catalog, so an answer saying a vehicle has no recalls is never
grounded. `conversation/vehicle_owner/recall_check.yaml` accepted "no open recalls"
and failed "recall information unavailable": it scored the false assurance as correct
and the honest answer as wrong.

The check is behavioural. Each case's `response` block is applied to fixed answers the
way both runners apply it (`tier2_conversation.py` (b)/(c), `tier3_e2e.py`): pass iff
the text contains at least one `must_contain_one_of` phrase and none of
`must_not_contain`, case-insensitive substring. `_passes` copies that check because
neither runner exposes it as a function; keep the two in step (review Cycle 22 S).

  * Every absence answer must fail: one probe per phrase in `NO_RECALLS_PHRASES`,
    each also naming a dealer and NHTSA so the accepted list alone cannot reject it.
  * Every non-answer must fail, so an accepted list cannot be widened to "anything
    that mentions recalls".
  * Every fallback answer must pass, including dealer-only and NHTSA-only forms,
    since the prompts say "a dealer or nhtsa.gov/recalls".

`NO_RECALLS_PHRASES` is a fixed list and cannot cover every paraphrase ("nothing
outstanding on recalls; check with your dealer" passes). It pins that no listed
phrasing can be dropped from any case, except "has not been recalled", which the
shorter "not been recalled" already covers.

Scope is computed: every case under evals/cases whose utterance mentions recalls.
Cases about one named recall are exempt from the fallback check only.

Known gap (review Cycle 21 S1): "no recall" also fails an honest answer phrased
"no recall information on file". The prompts prescribe "couldn't find recall
information", which none of the phrases touch.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

_CASES = Path(__file__).resolve().parents[1] / "cases"

# Cases about one named recall ("what is recall 24V-567 about?"). The answer there
# describes that recall, so the couldn't-find fallback does not apply to them.
_SPECIFIC_RECALL_CASES = {
    "knowledge/recall-24v-567-001.yaml",
}
_RECALL_ID = re.compile(r"\b\d{2}[A-Z]-\d{3}\b")

# Each is a way of saying the vehicle has no recalls. Straight and curly apostrophes
# are listed separately because the match is a plain substring.
NO_RECALLS_PHRASES = (
    "no open recall", "no recall", "no safety recall", "no outstanding recall",
    "no active recall", "zero open recall", "no open safety recall",
    "aren't any", "aren’t any", "doesn't have any", "doesn’t have any",
    "not subject to any recall", "isn't subject to any recall", "isn’t subject to any recall",
    "not subject to a recall", "isn't subject to a recall", "isn’t subject to a recall",
    "not affected by any recall", "isn't affected by any recall", "isn’t affected by any recall",
    "not affected by any open recall",
    "clear of recall", "recall-free", "all clear",
    "hasn't been recalled", "hasn’t been recalled", "has not been recalled",
    "never been recalled", "never recalled", "not been recalled", "not recalled",
    "isn't recalled", "isn’t recalled", "wasn't recalled", "wasn’t recalled",
)

# One probe per phrase. Each names a dealer and NHTSA, so only the rejected list
# can make the case fail it.
ABSENCE_ANSWERS = tuple(
    f"Your vehicle: {phrase}. A dealer or NHTSA can confirm." for phrase in NO_RECALLS_PHRASES
) + (
    "There are no open recalls for your vehicle.",
    "There aren't any open recalls on your vehicle, but a dealer can double-check.",
    "Your vehicle hasn't been recalled. Your dealer can confirm.",
)

# Answers that mention recalls but answer nothing. An accepted list widened to a
# bare "recall" (or emptied) passes these.
NON_ANSWERS = (
    "I'm not sure about recalls on your vehicle.",
    "Recalls are issued by manufacturers.",
)

# The fallback the prompts prescribe, in the wordings a model is likely to use.
FALLBACK_ANSWERS = (
    "I couldn't find recall information for your vehicle. Please check with a "
    "dealer or at nhtsa.gov/recalls using your VIN.",
    "I could not find recall information for your 2025 Meridian Azimuth. A dealer "
    "can check it, or you can look up your VIN at nhtsa.gov/recalls.",
    "I couldn’t find recall information for this vehicle, so I can't confirm its "
    "recall status. Check with your dealer or on the NHTSA site with the VIN.",
    "Recall information for your vehicle is unavailable to me. Please check with a "
    "dealer or at nhtsa.gov/recalls with your VIN.",
    # The wording the original vehicle_owner case rejected.
    "Sorry, recall information unavailable for this VIN right now. Please check "
    "with a dealer or at nhtsa.gov/recalls.",
    "I don't have any recall information for your vehicle, so please check with a "
    "dealer or at nhtsa.gov/recalls with your VIN.",
    "I don't have recall information for this vehicle. Your dealer or "
    "nhtsa.gov/recalls can check it with your VIN.",
    "I wasn't able to find recall information for your vehicle. A dealer can check "
    "it with your VIN, or you can use nhtsa.gov/recalls.",
    "I'm unable to find recall information for this VIN. Please ask your dealer or "
    "check nhtsa.gov/recalls.",
    # Dealer only, and NHTSA only: the prompts say "or".
    "I couldn't find recall information for your vehicle. Your dealer can look it "
    "up with your VIN.",
    "I couldn't find recall information for your vehicle. You can look up your VIN "
    "on the NHTSA recalls site.",
)


def _all_recall_cases() -> list[tuple[str, dict]]:
    out = []
    for path in sorted(_CASES.rglob("*.yaml")):
        case = yaml.safe_load(path.read_text()) or {}
        utterance = ((case.get("input") or {}).get("utterance") or "").lower()
        if "recall" in utterance:
            out.append((str(path.relative_to(_CASES)), case))
    return out


CASES = _all_recall_cases()
_IDS = [name for name, _ in CASES]
STATUS_CASES = [(n, c) for n, c in CASES if n not in _SPECIFIC_RECALL_CASES]
_STATUS_IDS = [name for name, _ in STATUS_CASES]


def _passes(case: dict, text: str) -> bool:
    """The runners' response check (tier2_conversation.py (b)/(c), tier3_e2e.py)."""
    resp = ((case.get("expected") or {}).get("response")) or {}
    accepted = resp.get("must_contain_one_of") or []
    rejected = resp.get("must_not_contain") or []
    low = text.lower()
    contains_one = not accepted or any(p.lower() in low for p in accepted)
    contains_none = all(p.lower() not in low for p in rejected)
    return contains_one and contains_none


def test_scope_includes_the_known_recall_cases():
    """Anti-vacuity: a moved or renamed case must not fall out of scope silently."""
    assert {
        "conversation/vehicle_owner/recall_check.yaml",
        "e2e/recall-lookup-text-001.yaml",
        "routing/recall-lookup-001.yaml",
    } <= set(_STATUS_IDS), _STATUS_IDS


@pytest.mark.parametrize("name", sorted(_SPECIFIC_RECALL_CASES))
def test_exemptions_ask_about_one_named_recall(name):
    """An exemption must stay a question about one named recall, not a recall-status one."""
    path = _CASES / name
    assert path.is_file(), name
    utterance = ((yaml.safe_load(path.read_text()) or {}).get("input") or {}).get("utterance") or ""
    assert _RECALL_ID.search(utterance.upper()), (name, utterance)


@pytest.mark.parametrize("name,case", CASES, ids=_IDS)
def test_no_absence_claim_is_listed_as_accepted(name, case):
    """On top of the behavioural checks: an accepted phrase that is itself a
    no-recalls claim records the wrong answer as a right one, one deleted
    rejection away from passing."""
    resp = ((case.get("expected") or {}).get("response")) or {}
    bad = [p for p in resp.get("must_contain_one_of") or []
           if any(phrase in p.lower() for phrase in NO_RECALLS_PHRASES)]
    assert not bad, f"{name} lists a no-recalls claim as an accepted answer: {bad!r}"


@pytest.mark.parametrize("answer", ABSENCE_ANSWERS)
@pytest.mark.parametrize("name,case", CASES, ids=_IDS)
def test_an_absence_claim_fails_the_case(name, case, answer):
    assert not _passes(case, answer), f"{name} scores a no-recalls answer as correct: {answer!r}"


@pytest.mark.parametrize("answer", NON_ANSWERS)
@pytest.mark.parametrize("name,case", CASES, ids=_IDS)
def test_a_non_answer_fails_the_case(name, case, answer):
    assert not _passes(case, answer), f"{name} scores a non-answer as correct: {answer!r}"


@pytest.mark.parametrize("answer", FALLBACK_ANSWERS)
@pytest.mark.parametrize("name,case", STATUS_CASES, ids=_STATUS_IDS)
def test_the_honest_fallback_passes_the_case(name, case, answer):
    assert _passes(case, answer), f"{name} fails the couldn't-find fallback: {answer!r}"
