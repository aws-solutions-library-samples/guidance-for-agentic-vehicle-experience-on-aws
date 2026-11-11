"""Ranked candidate builder for the FI quality investigation agent — Stage D task 3.3.

Converts raw ``fault_patterns`` rows (which represent a pattern group) into the
ranked candidates list that the investigation bundle carries and the model
evaluates.

Public API
----------
build_candidates_from_pattern_rows(rows) -> list[dict]
    Build the sorted ranked-candidate list from a fault_patterns group.
"""
from __future__ import annotations

from typing import Any


def build_candidates_from_pattern_rows(
    rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Build a sorted ranked-candidate list from ``fault_patterns`` rows.

    Each row in the group (all sharing the same ``pattern_group_id``) becomes
    one candidate entry.  Rows are sorted by ``candidate_rank`` (ascending)
    so that the top-ranked candidate (rank=1) is first — matching the fallback
    selector's expectation.

    The ``candidate_id`` field is the row's own ``pattern_id`` (spec D10:
    each candidate in the group is identified by its ``pattern_id``).

    Parameters
    ----------
    rows:
        List of ``fault_patterns`` rows belonging to one pattern group.
        Each row must carry at least ``pattern_id``, ``cohort_dimension``,
        ``cohort_value``, ``candidate_rank``, ``rate_ratio``, ``p_value``.

    Returns
    -------
    list[dict]
        Ranked candidates, each with:
        ``candidate_id``, ``dimension``, ``value``, ``ratio``,
        ``p_value``, ``rank``.
        Sorted by rank ascending.
    """
    candidates = []
    for row in rows:
        candidates.append({
            "candidate_id": row.get("pattern_id", ""),
            "dimension":    row.get("cohort_dimension", ""),
            "value":        row.get("cohort_value", ""),
            "ratio":        float(row.get("rate_ratio", 0.0)),
            "p_value":      float(row.get("p_value", 1.0)),
            "rank":         int(row.get("candidate_rank", 1)),
        })
    return sorted(candidates, key=lambda c: c["rank"])
