"""Run-loop mechanics for the FI fleet-health agent (spec D8, task 3.2).

Reuses the skip-predicate pattern from Stage C (agents/fi_recall_warranty/runner.py).
The skip rule and cap are deterministic; the model-call path is task 4.1.

Public API
----------
same_hash_model_written(existing_hash, inputs_hash, diagnosis_source) -> bool
    Re-exported from Stage C's runner (single definition rule, spec D8 step 4).
    True only when hash matches AND diagnosis_source==model.

apply_health_cap(items, cap=60) -> tuple[list, list]
    Split items into (within_cap, over_cap), highest severity first.

write_over_cap_items(over_cap_items) -> list
    Write over-cap items on the rules path (diagnosis_source=rules).

HealthRunner
    Thin orchestrator enforcing the skip rule per spec D8 step 4.

run_with_validator_fallback(*, model_fn, rules_fn, max_retries=1, severity=None, candidates=None)
    One model attempt plus one retry, then the rules fallback (spec D8 step 7, task 4.1).
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Iterable, Optional

# Re-export Stage C's single predicate definition (defect fix: was a copy).
# Both loops use one definition — any drift between copies would be a bug.
from agents.fi_recall_warranty.runner import same_hash_model_written  # noqa: F401

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Severity ordering (p0 best)
# ---------------------------------------------------------------------------
_SEVERITY_ORDER: dict[str, int] = {
    "p0": 0,
    "attention": 1,
    "soon": 2,
    "routine": 3,
    "informational": 4,
}

# Default per-run cap for model calls (spec D8)
HEALTH_CAP: int = 60


# ---------------------------------------------------------------------------
# apply_health_cap
# ---------------------------------------------------------------------------

def apply_health_cap(
    items: Iterable[dict],
    cap: int = HEALTH_CAP,
) -> tuple[list, list]:
    """Split items into (within_cap, over_cap) by severity, highest first.

    D8 caps MODEL CALLS, not writes: over-cap items are still written on the
    rules path via write_over_cap_items.  Highest-severity items survive the cap.

    Returns
    -------
    (within_cap, over_cap)
        ``within_cap``  : the first ``cap`` items sorted by severity.
        ``over_cap``    : the remaining items (in input order).

    Mutation guard (test_apply_cap_highest_severity_survives):
        Taking items in input order instead of severity order → p0 items lost.
    """
    all_items = list(items)
    sorted_items = sorted(
        all_items,
        key=lambda x: _SEVERITY_ORDER.get(x.get("severity", ""), 99),
    )
    within = sorted_items[:cap]
    # over_cap preserves original order for determinism
    within_set = set(id(i) for i in within)
    over = [i for i in all_items if id(i) not in within_set]
    return within, over


# ---------------------------------------------------------------------------
# write_over_cap_items
# ---------------------------------------------------------------------------

def write_over_cap_items(over_cap_items: list) -> list:
    """Write over-cap items on the rules path (diagnosis_source=rules).

    D8: the cap is on model calls, not writes.  Every over-cap item is still
    written, with ``diagnosis_source`` forced to ``"rules"``.

    Mutation guard (test_over_cap_p0_vehicle_gets_rules_path_item):
        Returning [] drops items — test len(written)==5 fails.
        Leaving diagnosis_source as "model" — assertion fails.
    """
    result = []
    for item in over_cap_items:
        written = dict(item)
        written["diagnosis_source"] = "rules"
        result.append(written)
    return result


# ---------------------------------------------------------------------------
# HealthRunner
# ---------------------------------------------------------------------------

class HealthRunner:
    """Thin orchestrator enforcing the skip rule per spec D8 step 4.

    Skip rule:
        same inputs_hash AND diagnosis_source=model  → no model call, no write.
        same inputs_hash AND diagnosis_source=rules  → eligible; call model, write.
        different hash (new or changed inputs)        → call model, write.
    """

    def __init__(
        self,
        write_fn: Callable[[dict], Any],
        model_fn: Callable[[str, dict], dict],
    ) -> None:
        self._write = write_fn
        self._model = model_fn

    def maybe_write(
        self,
        *,
        vin: str,
        finding_hash: str,
        diagnosis_source: str,
        existing_hash: Optional[str],
        severity: str,
    ) -> Optional[dict]:
        """Apply the skip rule; call model and write when eligible.

        Mutation guard (test_same_hash_model_written_skipped):
            Calling the model when same_hash_model_written is True fails.
        Mutation guard (test_same_hash_rules_written_model_called):
            NOT calling the model for a rules-written same-hash item fails.
        """
        if same_hash_model_written(existing_hash, finding_hash, diagnosis_source):
            # Spec D8 step 4: same hash + model-written → skip entirely.
            return None

        # Same hash but rules-written, or new/changed hash → call model and write.
        ctx = {"finding_hash": finding_hash, "severity": severity}
        result = self._model(vin, ctx)
        self._write(result)
        return result


# ---------------------------------------------------------------------------
# run_with_validator_fallback — task 4.1 (spec D8 step 7)
# ---------------------------------------------------------------------------

#: Fixed fallback detail (spec D8 step 7, "fixed detail").
RULES_FALLBACK_DETAIL: str = (
    "Service plan from deterministic rules: nearest candidate selected, "
    "no needs deferred (model output failed validation after retry)."
)
#: Fixed fallback detail when the fallback keeps the recall visit's rooftop (spec D8,
#: "with a recall visit"); the loop puts "Book with recall <id> at <rooftop>." first.
RULES_FALLBACK_DETAIL_RECALL: str = (
    "Service plan from deterministic rules: booked with the recall visit's rooftop, "
    "no needs deferred (model output failed validation after retry)."
)


class ModelBudgetStop(Exception):
    """Raised by a model attempt when the run has no call, token or time budget left.

    :func:`run_with_validator_fallback` stops retrying at once and falls back; it never
    turns this into another model call.
    """


def _distance(candidate: dict) -> float:
    for key in ("distance_miles", "distance_km"):
        value = candidate.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
    return float("inf")


def choose_fallback_dealer(candidates: Optional[list[dict]]) -> Optional[str]:
    """The rules fallback's rooftop: the recall visit's rooftop, else the nearest candidate.

    "Nearest" keeps ``rank_rooftops``'s order — in stock first, then distance, then
    ``dealer_id`` — so on the real path the fallback picks the same rooftop as the
    Group 3 rules item (recall rooftop, else ``rooftops[0]``).  Candidates without an
    ``in_stock`` flag all share one tier, so the nearest by distance wins.
    """
    if not candidates:
        return None

    def _key(c: dict) -> tuple:
        recall_tier = 0 if c.get("recall_visit") is True else 1
        stocked = c.get("in_stock", c.get("parts_in_stock"))
        stock_tier = 0 if stocked is True else 1
        return (recall_tier, stock_tier, _distance(c), str(c.get("dealer_id") or ""))

    return sorted(candidates, key=_key)[0].get("dealer_id")


def run_with_validator_fallback(
    *,
    model_fn: Callable[[], dict],
    rules_fn: Callable[[], dict],
    max_retries: int = 1,
    severity: Optional[str] = None,
    candidates: Optional[list[dict]] = None,
) -> dict:
    """Run ``model_fn`` (which validates its own reply) with retries, then the rules fallback.

    ``model_fn`` returns a validated plan or raises.  Any exception is a failed attempt
    (spec D8 step 7: one retry); :class:`ModelBudgetStop` ends the attempts at once.  A
    model plan comes back with ``diagnosis_source: model``.

    The fallback starts from ``rules_fn()`` and then forces what spec D8 step 7 fixes,
    whatever ``rules_fn`` returned: urgency from the band (when ``severity`` is given),
    the rooftop from :func:`choose_fallback_dealer` (when ``candidates`` is given),
    nothing deferred, the fixed detail, no evidence references, ``confidence: low`` and
    ``diagnosis_source: rules``.
    """
    from agents.fi_fleet_health.validator import band_urgency

    if isinstance(max_retries, bool) or not isinstance(max_retries, int) or max_retries < 0:
        raise ValueError("max_retries must be a non-negative integer")

    for _attempt in range(1 + max_retries):
        try:
            plan = model_fn()
        except ModelBudgetStop:
            break
        except Exception as exc:  # noqa: BLE001 — every model failure is a failed attempt
            log.info("fleet_health: model attempt failed (%s)", type(exc).__name__)
            continue
        return {**plan, "diagnosis_source": "model"}

    plan = dict(rules_fn())
    if severity is not None:
        plan["urgency"] = band_urgency(severity)
    recall_rooftop = False
    if candidates is not None:
        plan["dealer_id"] = choose_fallback_dealer(candidates)
        recall_rooftop = any(
            c.get("recall_visit") is True and c.get("dealer_id") == plan["dealer_id"]
            for c in candidates
        )
    plan["defer_need_ids"] = []
    plan["detail"] = RULES_FALLBACK_DETAIL_RECALL if recall_rooftop else RULES_FALLBACK_DETAIL
    plan["evidence_refs"] = []
    plan["confidence"] = "low"
    plan["diagnosis_source"] = "rules"
    return plan
