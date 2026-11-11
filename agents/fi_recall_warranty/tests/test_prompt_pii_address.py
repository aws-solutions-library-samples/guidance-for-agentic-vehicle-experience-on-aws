"""Prompt directive that keeps the model from writing city-shaped text.

Context (issues/2026-10-10-cost-guardrail-blocks-replies/summary.md § "Follow-on
fix"): the 2026-10-10T13:08:07Z stage C recall run had 36 of 95 model replies
blocked by the FiCost Guardrail. Live reproduction identified the dominant
cause as the PII ADDRESS detector tripping on city-name tokens in the detail
(`Atlanta`, `Austin`, `Boston`) — the model was translating `dealer-atlanta`
style ids into their English city form. Fix: add an explicit directive to
both system prompts to reference dealers / vendors only by their id and never
add location, address, phone number, or email text.

This test file pins the directive on both prompts; a mutant that removes it
fails these tests. Confirmed in a second live reproduction run against DRAFT
guardrail `8wge4n0s9zzm`: 0 of 15 replies tripped after the fix. See
`issues/2026-10-10-cost-guardrail-blocks-replies/live-rw-reproduce.json`.
"""

from __future__ import annotations

from agents.fi_recall_warranty.llm import (
    _CLAIM_DRAFT_SYSTEM,
    _RECALL_PLAN_SYSTEM,
)


def _lower(s: str) -> str:
    return s.lower()


# ────────────────────────────────────────────────────────────────────────────
# _RECALL_PLAN_SYSTEM — plan_booking
# ────────────────────────────────────────────────────────────────────────────


def test_recall_plan_system_names_the_dealer_id_anchor():
    # The anchor the fix depends on: tell the model to reference each dealer
    # only by its exact dealer_id value. Removing this phrase lets the model
    # fall back to natural-English rephrasing — "the Atlanta location" — and
    # the guardrail trips on ADDRESS.
    assert "dealer_id" in _RECALL_PLAN_SYSTEM
    low = _lower(_RECALL_PLAN_SYSTEM)
    assert ("exact dealer_id" in low) or ("dealer_id value" in low), (
        "plan_booking prompt must tell the model to reference each dealer by "
        "its exact dealer_id value — removing this directive brought back "
        "the 13:08Z ADDRESS block rate."
    )


def test_recall_plan_system_forbids_city_names_and_addresses():
    low = _lower(_RECALL_PLAN_SYSTEM)
    # Must explicitly forbid city-shaped output. Any mutant that drops the
    # 'city names' phrase re-opens the ADDRESS trip path.
    assert "city name" in low or "city names" in low, (
        "plan_booking prompt must forbid translating dealer_ids into city "
        "names — the chief source of the 36/95 ADDRESS block rate on "
        "2026-10-10T13:08Z."
    )


def test_recall_plan_system_forbids_address_phone_email_text():
    low = _lower(_RECALL_PLAN_SYSTEM)
    for forbidden in ("address", "phone", "email"):
        assert forbidden in low, (
            f"plan_booking prompt must forbid '{forbidden}' content in the "
            f"detail text (prevents sibling PII entity trips)."
        )


def test_recall_plan_system_keeps_the_existing_do_not_describe_rules():
    # Mutant guard: the prompt-anchor fix must not accidentally delete the
    # safety-risk / coverage / verdicts guard that the recall notice path
    # relies on (operator sees the recall notice verbatim).
    low = _lower(_RECALL_PLAN_SYSTEM)
    assert "safety risk" in low
    assert "coverage" in low
    assert "verdicts" in low
    assert "operator sees the recall" in low


# ────────────────────────────────────────────────────────────────────────────
# _CLAIM_DRAFT_SYSTEM — draft_claim
# ────────────────────────────────────────────────────────────────────────────


def test_claim_draft_system_mirrors_the_dealer_id_anchor():
    low = _lower(_CLAIM_DRAFT_SYSTEM)
    assert "dealer" in low or "vendor" in low, (
        "claim-draft prompt must anchor id references so warranty narration "
        "cannot translate ids to city names in the same way the recall path did."
    )
    assert "id value" in low or "exact id" in low or "exact dealer_id" in low


def test_claim_draft_system_forbids_city_names_and_contact_text():
    low = _lower(_CLAIM_DRAFT_SYSTEM)
    assert "city name" in low or "city names" in low
    for forbidden in ("address", "phone", "email"):
        assert forbidden in low, (
            f"claim-draft prompt must forbid '{forbidden}' content in the "
            f"narrative / detail text."
        )


def test_claim_draft_system_keeps_the_dms_verdict_guard():
    # Mutant guard: do not delete the DMS verdict / coverage guard that
    # keeps the claim narrative inside AVX's scope.
    low = _lower(_CLAIM_DRAFT_SYSTEM)
    assert "dms" in low
    assert "verdict" in low
    assert "coverage" in low or "amounts" in low
