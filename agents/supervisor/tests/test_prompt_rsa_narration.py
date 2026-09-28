"""Task 4.2 of spec `2026-09-23-cvx-rsa-stub-dispatch`: the narration boundary
for roadside dispatch status is test-enforced.

Phase A's escalation rule (rule 9 of `_HARD_RULES`, rule 10 of
`SYSTEM_PROMPT_TEMPLATE`) forbids describing a roadside dispatch as arranged,
under way, or completed. Phase B makes a dispatch lifecycle real (simulated
provider), so the rule gains one clause:

- the model MAY state a dispatch status and ETA a tool returned, as returned;
- it MUST NOT invent or estimate an ETA, upgrade a REQUESTED request to
  dispatched / en route / arrived, or present a simulated dispatch as real;
- `rsaDispatched: true` means "recorded", not "sent".

Why a separate file: `test_prompt_content.py` is being edited by the
concurrent escalation session (its Fix Group 6), so this task does not write
to it. The extractor below reproduces
`TestEscalationIsRecommendedNotAsserted._rule_body` exactly (same anchor, same
terminators) and adds a second, narrower extraction for the new clause.

Scoping: every property assertion runs against the extracted clause, which
sits inside the extracted rule body. An unscoped substring check would pass
with the clause deleted from one template, because the other template still
carries it; `test_property_phrases_are_unique_in_each_prompt` pins that each
phrase occurs exactly once per prompt, so scoping is what the assertions rely
on.

Mutations (run on a copy; recorded in the spec's decisions.md):
  M1 delete the clause from `_HARD_RULES` only        → property tests FAIL for _HARD_RULES
  M2 delete the clause from `SYSTEM_PROMPT_TEMPLATE` only → property tests FAIL for the template
  M3 keep the clause anchor, gut the rest, both templates identically
     → property tests FAIL; drift and vacuity guards still PASS
  M6 add permission to say the truck is dispatched / "help is on its way" (both) → exact pin FAILS
  M7 add "treat an omitted `simulated` as real" (both)                          → exact pin FAILS
"""

from __future__ import annotations

import pytest

from agents.supervisor.supervisor import SYSTEM_PROMPT_TEMPLATE, _HARD_RULES

_RULE_ANCHOR = "Escalation is a recommendation, never an assertion"
_CLAUSE_ANCHOR = "A recorded roadside request is not a dispatch"
# The clause ends where the pre-existing P0 sentence begins.
_CLAUSE_END = "On P0 the pull-over instruction"

_PROMPTS = [("_HARD_RULES", _HARD_RULES), ("SYSTEM_PROMPT_TEMPLATE", SYSTEM_PROMPT_TEMPLATE)]
_IDS = [n for n, _ in _PROMPTS]

# Each Accept 1 property, as the phrase that expresses it. Flattened whitespace.
_PROPERTIES = {
    "may narrate a provider-returned status and ETA, as returned":
        "you MAY state that status and its ETA exactly as the tool gave them, and nothing beyond them",
    "must not invent an ETA":
        "You MUST NOT invent or estimate an ETA",
    "must not upgrade REQUESTED":
        "describe a REQUESTED request as dispatched, en route, or arrived",
    # Fails closed (review Cycle 1 W1): absent or null `simulated` reads as
    # simulated. The earlier "marks simulated" wording failed open.
    "simulated unless the tool says otherwise":
        "Treat every dispatch as simulated unless the tool returned `simulated: false`",
    "must say it is simulated":
        "and say that it is simulated",
    "rsaDispatched means recorded, not sent":
        "`rsaDispatched: true` means the request was recorded (status REQUESTED), not that a truck was sent",
    "the permission is an exception to (b), not a replacement":
        "The one exception to (b) is a roadside dispatch status a tool returned",
}


# The whole clause, pinned (review Cycle 1 W2). Presence tests alone admitted
# additive mutations that contradict the clause (M6: permission to say "help is
# on its way"; M7: "treat an omitted `simulated` as real"). Any wording change
# must update this string in the same commit, which puts it in front of review.
_EXPECTED_CLAUSE = (
    "A recorded roadside request is not a dispatch: `rsaDispatched: true` means the request "
    "was recorded (status REQUESTED), not that a truck was sent. The one exception to (b) is a "
    "roadside dispatch status a tool returned: you MAY state that status and its ETA exactly as "
    "the tool gave them, and nothing beyond them. You MUST NOT invent or estimate an ETA, or "
    "describe a REQUESTED request as dispatched, en route, or arrived. Treat every dispatch as "
    "simulated unless the tool returned `simulated: false`, and say that it is simulated."
)


def _flat(text: str) -> str:
    return " ".join(text.split())


def _rule_body(prompt: str) -> str:
    """Same extraction as TestEscalationIsRecommendedNotAsserted._rule_body."""
    assert _RULE_ANCHOR in prompt, f"escalation rule anchor {_RULE_ANCHOR!r} not found"
    rest = prompt[prompt.index(_RULE_ANCHOR):]
    for terminator in ("\n\nSession context:", '\n"""'):
        if terminator in rest:
            rest = rest[: rest.index(terminator)]
    return rest.strip()


def _clause(prompt: str) -> str:
    """The Task 4.2 clause, extracted from within the rule body only."""
    body = _rule_body(prompt)
    assert _CLAUSE_ANCHOR in body, (
        f"Task 4.2 clause anchor {_CLAUSE_ANCHOR!r} not found in the escalation rule "
        "body — the clause was deleted, reworded, or moved out of the rule, and every "
        "assertion scoped to it would otherwise pass vacuously"
    )
    rest = body[body.index(_CLAUSE_ANCHOR):]
    assert _CLAUSE_END in rest, f"clause terminator {_CLAUSE_END!r} not found after the clause"
    return _flat(rest[: rest.index(_CLAUSE_END)])


@pytest.mark.parametrize("name,prompt", _PROMPTS, ids=_IDS)
@pytest.mark.parametrize("prop", sorted(_PROPERTIES))
def test_clause_expresses_each_property(name, prompt, prop):
    phrase = _PROPERTIES[prop]
    assert phrase in _clause(prompt), (
        f"{name}: the roadside-status clause no longer expresses '{prop}'. "
        f"Missing: {phrase!r}. Spec Task 4.2 Accept 1."
    )


@pytest.mark.parametrize("name,prompt", _PROMPTS, ids=_IDS)
def test_clause_sits_inside_the_escalation_rule_before_the_p0_sentence(name, prompt):
    body = _flat(_rule_body(prompt))
    assert body.index(_CLAUSE_ANCHOR) < body.index(_CLAUSE_END), (
        f"{name}: the clause must precede the P0 pull-over sentence inside rule 9/10"
    )


@pytest.mark.parametrize("name,prompt", _PROMPTS, ids=_IDS)
def test_clause_is_exactly_the_reviewed_text(name, prompt):
    assert _clause(prompt) == _EXPECTED_CLAUSE, (
        f"{name}: the roadside-status clause changed. If intended, update "
        f"_EXPECTED_CLAUSE in the same commit.\n  got:      {_clause(prompt)!r}\n"
        f"  expected: {_EXPECTED_CLAUSE!r}"
    )


def test_clause_is_byte_identical_across_templates():
    """Accept 2. The whole-rule byte-identity guard in test_prompt_content.py
    also covers this; this one names the clause so a drift message points here."""
    hard, sys_ = _clause(_HARD_RULES), _clause(SYSTEM_PROMPT_TEMPLATE)
    assert hard == sys_, (
        "the roadside-status clause differs between _HARD_RULES and "
        f"SYSTEM_PROMPT_TEMPLATE:\n  hard: {hard!r}\n  sys:  {sys_!r}"
    )


@pytest.mark.parametrize("name,prompt", _PROMPTS, ids=_IDS)
def test_clause_extraction_is_not_vacuous(name, prompt):
    clause = _clause(prompt)
    body = _flat(_rule_body(prompt))
    assert clause.startswith(_CLAUSE_ANCHOR), f"{name}: extraction did not start at the clause anchor"
    assert 0 < len(clause) < len(body), f"{name}: clause extraction is empty or the whole rule"


@pytest.mark.parametrize("name,prompt", _PROMPTS, ids=_IDS)
def test_property_phrases_are_unique_in_each_prompt(name, prompt):
    """Each phrase appears exactly once in each prompt, so a phrase found in the
    clause cannot have come from another rule, and scoping is load-bearing."""
    flat = _flat(prompt)
    for prop, phrase in _PROPERTIES.items():
        assert flat.count(phrase) == 1, f"{name}: {prop!r} phrase occurs {flat.count(phrase)} times"


@pytest.mark.parametrize("name,prompt", _PROMPTS, ids=_IDS)
def test_phase_a_prohibition_survives_the_exception(name, prompt):
    """The new permission must not have displaced Phase A's (b) or its example
    denials; the clause is an exception to them, not a rewrite."""
    body = _flat(_rule_body(prompt))
    for phrase in (
        "as arranged, under way, or completed",
        "never \"I've connected you\", \"a technician is on the way\", or \"help is already on its way\"",
        "requested a handoff, not delivered one",
    ):
        assert phrase in body, f"{name}: Phase A phrase lost: {phrase!r}"
