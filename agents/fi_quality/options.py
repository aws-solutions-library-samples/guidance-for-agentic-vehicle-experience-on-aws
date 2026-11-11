"""Option evidence builder for the FI quality investigation agent — Stage D task 3.3.

Spec D10 / contract § 2.6 / ADP interface § 4 (engineering_remedy_options):
**All numeric fields** (parts cost, labor hours, lead time, total cost) come
from ADP corpus tables.  The model tool schema has no numeric field; a numeric
value the model might suggest via any channel is ignored.

Public API
----------
build_option_evidence(corpus_option, total_vehicles,
                      *, model_cost_suggestion=None) -> dict
    Build one option evidence entry with numbers exclusively from corpus.

build_options_evidence(failure_mode, total_vehicles,
                       *, model_options=None) -> list[dict]
    Build the full quality.options evidence list, merging model selection
    (which options to include, recommended flag, tradeoffs text) with corpus
    numbers.
"""
from __future__ import annotations

from typing import Any, Optional


def build_option_evidence(
    *,
    corpus_option: dict[str, Any],
    total_vehicles: int,
) -> dict[str, Any]:
    """Build one option evidence entry from ADP corpus data.

    Mutation guard (test_option_numbers_from_corpus_not_model):
        Using a model-supplied number for ``parts_cost_per_vehicle`` causes
        the corpus-equality assertion to fail.

    Parameters
    ----------
    corpus_option:
        Row from ``engineering_remedy_options`` (or the nested ``remedy_options``
        list in the bundle's failure-mode dict).  Column names follow ADP
        interface § 4 (``parts_cost_per_vehicle_usd`` alias resolved to
        ``parts_cost_per_vehicle`` for the bundle representation).
    total_vehicles:
        Total affected vehicles from the fault pattern.

    Returns
    -------
    dict
        Evidence value dict with numeric fields exclusively from corpus:
        ``remedy_option_id``, ``remedy_type``, ``parts_cost_per_vehicle``,
        ``labor_hours_per_vehicle``, ``total_vehicles``, ``total_cost``,
        ``lead_time_weeks``, ``over_the_air``.
    """
    # All numeric fields from corpus — never from any model-supplied value.
    parts_cost = _as_float(
        corpus_option.get("parts_cost_per_vehicle")
        or corpus_option.get("parts_cost_per_vehicle_usd")
        or 0.0
    )
    labor_hours = _as_float(
        corpus_option.get("labor_hours_per_vehicle") or 0.0
    )
    lead_time = corpus_option.get("lead_time_weeks")  # int or None
    over_the_air = bool(corpus_option.get("over_the_air", False))
    remedy_type = corpus_option.get("remedy_type", "")
    remedy_option_id = corpus_option.get("remedy_option_id", "")

    # total_cost: parts cost per vehicle × total vehicles (labour billed per RO).
    # Mutation guard: wiring to a model value changes parts_cost, which changes
    # total_cost, which the assertion detects.
    total_cost = parts_cost * total_vehicles

    return {
        "remedy_option_id": remedy_option_id,
        "remedy_type": remedy_type,
        "parts_cost_per_vehicle": parts_cost,
        "labor_hours_per_vehicle": labor_hours,
        "total_vehicles": total_vehicles,
        "total_cost": total_cost,
        "lead_time_weeks": lead_time,
        "over_the_air": over_the_air,
    }


def build_options_evidence(
    *,
    failure_mode: dict[str, Any],
    total_vehicles: int,
    model_options: Optional[list[dict[str, Any]]] = None,
) -> list[dict[str, Any]]:
    """Build the full quality.options evidence value.

    Numeric fields always come from corpus.  Model output provides only:
    - which options to include (2–4 options from the bundle's failure mode)
    - which one to mark recommended (``recommended: true``)
    - tradeoffs text per option (at most 600 chars)

    When ``model_options`` is ``None`` or empty (fallback path), all corpus
    options are returned with the first one recommended.

    Parameters
    ----------
    failure_mode:
        Failure mode dict from the investigation bundle, with nested
        ``remedy_options`` list.
    total_vehicles:
        Total affected vehicles.
    model_options:
        Optional model-provided list of
        ``{remedy_option_id, tradeoffs, recommended}``.
        Any numeric field the model might include in tradeoffs text does not
        reach the numeric columns (those come from corpus via corpus_option).

    Returns
    -------
    list[dict]
        quality.options evidence value list.  Each entry carries the fields
        from ``build_option_evidence`` plus ``recommended`` (bool) and
        ``tradeoffs`` (str).
    """
    corpus_options: list[dict[str, Any]] = failure_mode.get("remedy_options") or []
    corpus_by_id: dict[str, dict] = {
        o.get("remedy_option_id", ""): o for o in corpus_options
    }

    if not model_options:
        # Fallback / rules path: include all corpus options; first is recommended.
        # W3 fix: if there are no corpus options (unknown failure mode, no catalog
        # match), add a corpus-free "monitor" option so the Finding always has at
        # least one option and option_id is never null.
        if not corpus_options:
            monitor_ev = {
                "remedy_option_id": "monitor",
                "remedy_type": "monitor",
                "parts_cost_per_vehicle": 0.0,
                "labor_hours_per_vehicle": 0.0,
                "total_vehicles": total_vehicles,
                "total_cost": 0.0,
                "lead_time_weeks": None,
                "over_the_air": False,
                "recommended": True,
                "tradeoffs": "Continue monitoring for further occurrences.",
            }
            return [monitor_ev]
        results = []
        for i, opt in enumerate(corpus_options):
            ev = build_option_evidence(
                corpus_option=opt,
                total_vehicles=total_vehicles,
            )
            ev["recommended"] = (i == 0)
            ev["tradeoffs"] = _tradeoffs_text(opt)
            results.append(ev)
        return results

    # Model path: merge model selection with corpus numbers.
    # Task 4.2: ``monitor`` is a valid model option even when the catalog has no row
    # for it (spec D10 step 3, "or monitor"); it carries no corpus numbers.
    results = []
    for model_opt in model_options:
        oid = model_opt.get("remedy_option_id", "")
        corpus_opt = corpus_by_id.get(oid)
        if corpus_opt is None and oid == "monitor":
            results.append({
                "remedy_option_id": "monitor",
                "remedy_type": "monitor",
                "parts_cost_per_vehicle": 0.0,
                "labor_hours_per_vehicle": 0.0,
                "total_vehicles": total_vehicles,
                "total_cost": 0.0,
                "lead_time_weeks": None,
                "over_the_air": False,
                "recommended": model_opt.get("recommended") is True,
                "tradeoffs": (model_opt.get("tradeoffs") or "")[:600],
            })
            continue
        if corpus_opt is None:
            continue
        ev = build_option_evidence(
            corpus_option=corpus_opt,
            total_vehicles=total_vehicles,
        )
        ev["recommended"] = bool(model_opt.get("recommended", False))
        # Tradeoffs text: model provides per-option analysis (at most 600 chars).
        raw_tradeoffs = (
            model_opt.get("tradeoffs")
            or _tradeoffs_text(corpus_opt)
        )
        ev["tradeoffs"] = raw_tradeoffs[:600] if raw_tradeoffs else ""
        results.append(ev)
    return results


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _as_float(value: Any) -> float:
    """Safely convert a corpus numeric value to float."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _tradeoffs_text(corpus_opt: dict[str, Any]) -> str:
    """Extract tradeoffs text from a corpus option row."""
    raw = (
        corpus_opt.get("trade_off_notes")
        or corpus_opt.get("tradeoffs")
        or ""
    )
    return raw[:600] if raw else ""
