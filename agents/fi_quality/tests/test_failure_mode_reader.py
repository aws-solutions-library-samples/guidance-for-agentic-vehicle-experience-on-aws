"""Failure-mode reader: symptom-only matching and stable order (decisions.md "Group 3 review
cycle 3" answer 7; architect check of G3F3.2).

A plant, build-window or model pattern has no ``supplier_lot`` candidate, so no candidate names a
component.  ``5b3b038`` passed an empty component list and the reader returned ``[]``, so such a
pattern could never reach a failure mode: every planted plant/build-window cause would have been
investigated as ``unknown``.  The reader now reads every failure mode and matches on symptom codes.
Without ``ORDER BY`` Athena returns rows in any order, and the rules fallback recommends the first
option in "catalog order", so the order is pinned too.
"""
from __future__ import annotations

from unittest.mock import patch

import agents.fi_quality.readers as readers

_STAGE = "staging"


def _row(fm_id: str, component: str, symptoms: str, option_id: str) -> dict:
    return {
        "failure_mode_id": fm_id,
        "component_id": component,
        "mechanism": "isolation_degradation",
        "symptom_codes_csv": symptoms,
        "cause_dimension": "assembly_plant",
        "do_not_drive": "false",
        "park_outside": "false",
        "remedy_option_id": option_id,
        "remedy_type": "hardware_recall",
        "parts_cost_per_vehicle": "2400.00",
        "labor_hours_per_vehicle": "8.00",
        "lead_time_weeks": "16",
        "over_the_air": "false",
        "trade_off_notes": "",
    }


def _read(component_ids, rows):
    seen: list[str] = []

    def _fake_run_query(sql, **kw):
        seen.append(sql)
        return rows

    with patch.object(readers, "run_query", side_effect=_fake_run_query):
        modes = readers.read_failure_modes(
            "P0AA", component_ids, _STAGE,
            athena_client=object(), workgroup="wg", results_s3_path="s3://b/p/",
        )
    return modes, seen


def test_no_candidate_components_still_matches_by_symptom_codes():
    rows = [
        _row("fm-hv-iso", "hv_battery_module", "P0AA6,P0AA7", "opt-1"),
        _row("fm-brake", "brake_caliper", "C0035", "opt-9"),
    ]
    modes, seen = _read([], rows)

    ids = [m["failure_mode_id"] for m in modes]
    assert ids == ["fm-hv-iso"], (
        "A pattern with no supplier_lot candidate must still find the failure mode whose "
        f"symptom codes match its code family; got {ids!r}. Returning [] for an empty "
        "component list sends every plant/build-window pattern to 'unknown'."
    )
    assert len(seen) == 1 and "WHERE fm.component_id" not in seen[0], (
        "With no candidate components the query must not filter by component."
    )


def test_candidate_components_filter_the_query():
    _, seen = _read(["brake_caliper"], [])
    assert len(seen) == 1
    assert "WHERE fm.component_id IN ('brake_caliper')" in seen[0]


def test_query_orders_rows_for_a_stable_catalog_order():
    _, seen_empty = _read([], [])
    _, seen_comp = _read(["brake_caliper"], [])
    for sql in (seen_empty[0], seen_comp[0]):
        assert "ORDER BY fm.failure_mode_id, ro.remedy_option_id" in sql
        assert "adp_staging_engineering.engineering_failure_modes" in sql
        assert "adp_staging_engineering.engineering_remedy_options" in sql
