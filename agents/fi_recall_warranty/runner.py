"""Run-loop mechanics for the FI recall and warranty Tier 2 agent (spec D8, task 3.1D).

Public surface:
  sort_by_severity   — order items p0 > attention > soon > routine (task 4.1 model-call selector).
  apply_cap          — per-kind MODEL-CALL cap (task 4.1; D8 caps model calls, not writes).
  build_rules_plan   — candidate 0 (in-stock first), band urgency, confidence=low, no combines.
  skip_unchanged     — THE skip rule both loops use: same hash + model-written → skip.
  FiRwRunner         — task 4.1's model-path wrapper; decides through the same rule.

The rules path writes every item and never calls sort_by_severity / apply_cap. Athena paging
lives in agents.tier2.adp_source.run_query (follows NextToken).

``run_with_validator_fallback`` is task 4.1 — not implemented here.
"""
from __future__ import annotations

from typing import Any, Callable, Iterable, Optional

from agents.fi_recall_warranty.band import BandResult

# ---------------------------------------------------------------------------
# Module constants — overridable by argument
# ---------------------------------------------------------------------------
RECALL_CAP: int = 60
WARRANTY_CAP: int = 30

_SEVERITY_ORDER: dict[str, int] = {
    "p0": 0,
    "attention": 1,
    "soon": 2,
    "routine": 3,
}

_RECALL_KIND = "fleet.recall.vehicle"
_WARRANTY_KIND = "fleet.warranty.claim_candidate"


# ---------------------------------------------------------------------------
# sort_by_severity
# ---------------------------------------------------------------------------
def sort_by_severity(items: list[dict]) -> list[dict]:
    """Return a new list sorted p0 → attention → soon → routine.

    Unknown severities sort last (stable within unknowns).
    """
    return sorted(items, key=lambda x: _SEVERITY_ORDER.get(x.get("severity", ""), 99))


# ---------------------------------------------------------------------------
# apply_cap
# ---------------------------------------------------------------------------
def apply_cap(
    items: Iterable[dict],
    recall_cap: int = RECALL_CAP,
    warranty_cap: int = WARRANTY_CAP,
) -> list[dict]:
    """Task 4.1's model-call selector: which items get a model call this run (spec D8).

    D8 caps MODEL CALLS, not writes: items past the cap are still written on the rules
    path. The rules path never calls this. Sort by severity within each finding kind,
    then enforce per-kind caps.

    Highest-severity items survive the cap; lower-severity items are dropped.
    Items of other finding_kind values are passed through unchanged (no cap).
    """
    recalls = sorted(
        [i for i in items if i.get("finding_kind") == _RECALL_KIND],
        key=lambda x: _SEVERITY_ORDER.get(x.get("severity", ""), 99),
    )
    warranties = sorted(
        [i for i in items if i.get("finding_kind") == _WARRANTY_KIND],
        key=lambda x: _SEVERITY_ORDER.get(x.get("severity", ""), 99),
    )
    return recalls[:recall_cap] + warranties[:warranty_cap]


# ---------------------------------------------------------------------------
# build_rules_plan
# ---------------------------------------------------------------------------
def build_rules_plan(
    candidates: list[dict],
    band_result: BandResult,
) -> dict:
    """Return the rules-fallback booking plan (spec D8 step 6, contract § 1.6).

    Takes candidate 0 (already ranked: in-stock first, then distance, then
    dealer_id).  When candidate 0 is out of stock, the detail names the dealer
    and says parts are not in stock there (D9).
    """
    best = candidates[0]
    in_stock = best.get("in_stock", True)
    detail = (
        "Book the recall service at the nearest available dealer."
        if in_stock
        else (
            f"Parts for this recall are not in stock at {best['dealer_id']}; "
            "book when restocked."
        )
    )
    return {
        "dealer_id": best["dealer_id"],
        "combine_with": [],
        "urgency": band_result.urgency,
        "detail": detail,
        "confidence": "low",
        "diagnosis_source": "rules",
    }


# ---------------------------------------------------------------------------
# skip rule — one definition (spec D8 step 4, G3-11)
# ---------------------------------------------------------------------------
def same_hash_model_written(
    existing_hash: Optional[str], inputs_hash: str, diagnosis_source: Optional[str],
) -> bool:
    """True only when the live item has the SAME inputs_hash AND was model-written."""
    return (existing_hash is not None and existing_hash == inputs_hash
            and diagnosis_source == "model")


def _live_diagnosis_source(live: dict, source_kind: str) -> Optional[str]:
    for entry in live.get("evidence") or []:
        if entry.get("kind") == source_kind:          # schema form {kind, value}
            return entry.get("value")
        if source_kind in entry:                      # domain-key form
            return entry[source_kind]
    return None


def skip_unchanged(live: Optional[dict], inputs_hash: str, *, source_kind: str) -> bool:
    """The skip rule both daily loops apply to the live Finding for a subject_key.

    ``source_kind`` is ``recall.diagnosis_source`` or ``warranty.diagnosis_source``.
    A rules-written item with the same hash stays eligible (task 4.1 upgrades it).
    """
    if live is None:
        return False
    return same_hash_model_written(live.get("inputs_hash"), inputs_hash,
                                   _live_diagnosis_source(live, source_kind))


# ---------------------------------------------------------------------------
# run_with_validator_fallback — task 4.1
# ---------------------------------------------------------------------------
def run_with_validator_fallback(
    *,
    model_fn: Callable[..., Any],
    rules_fn: Callable[[Exception], Any],
    max_retries: int = 1,
) -> Any:
    """Call model_fn up to (max_retries + 1) times, then fall back to rules_fn.

    First call: model_fn(prev_error=None).
    Each retry:  model_fn(prev_error=<last exception>).
    If all model attempts raise, rules_fn(last_exception) is returned.

    decisions.md call 49: "one retry" → max_retries=1 → two total model calls.
    """
    last_exc: Exception | None = None
    for attempt in range(max_retries + 1):
        try:
            return model_fn(prev_error=last_exc)
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
    return rules_fn(last_exc)
