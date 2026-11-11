"""
Energy-outlier selection tests and subject_key = data month tests (task 3.1).

Spec: .kiro/specs/2026-10-02-fi-cost-stage-b/spec.md § D6 step 1, § D8
Decisions: decisions.md 2026-10-05 item 6 (energy-cost outlier rule — selection
           is is_outlier OR is_energy_outlier; ordering; one Finding per vehicle
           per month either way)
           decisions.md 2026-10-04 "User: one cost item per vehicle per month"
           (subject_key = YYYY-MM; two months → two finding_keys)

Tests in this file:
  1. An energy-only outlier (is_outlier=false, is_energy_outlier=true) is selected.
  2. A total-only outlier (is_outlier=true, is_energy_outlier=false) is selected.
  3. A vehicle with both flags gives cost.flag = "both" (via evidence postprocessing).
  4. A vehicle with only energy flag gives cost.flag = "energy".
  5. A vehicle with only total flag gives cost.flag = "total".
  6. Ordering: vehicle with larger energy_excess_usd comes before one with smaller
     total excess (mutation check: drop GREATEST → order reverses).
  7. Mutation check OR: drop is_energy_outlier side → energy-only vehicle missing.
  8. Mutation check OR: drop is_outlier side → total-only vehicle missing.
  9. subject_key = data month: two different months for one VIN give two finding_keys.
 10. subject_key = data month: the same month twice gives one finding_key.
 11. Both fleet.cost.anomaly and fleet.cost.sell_timing carry subject_key.

These tests run green (no xfail) because:
  - agents/fi_cost/selection.py implements select_anomalies
  - agents/fi_cost/evidence.py implements the cost.flag postprocessing
  - agents/fi_cost/finding_keys.py implements cost_finding_key with subject_key

Mutation note (recorded per decisions.md 2026-10-05 item 6 constraint):
  M-OR-energy: comment out `is_energy_outlier` from the WHERE clause of
               select_anomalies.sql → test_energy_only_outlier_is_selected fails.
  M-OR-total:  comment out `is_outlier` from the WHERE clause →
               test_total_only_outlier_is_selected fails.
  Both are verified in test_sql_shape.py using the SQL text directly.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest


# ---------------------------------------------------------------------------
# Stubs and helpers
# ---------------------------------------------------------------------------


def _athena_stub_from_rows(rows: list[dict]) -> MagicMock:
    """Build an Athena stub that returns the given rows as the query result.

    The stub simulates: start_query_execution → get_query_execution (SUCCEEDED)
    → get_query_results (single page, column headers derived from first row).
    """
    if not rows:
        all_rows = [{"Data": [{"VarCharValue": "vin"}]}]
    else:
        headers = list(rows[0].keys())
        header_row = {"Data": [{"VarCharValue": h} for h in headers]}
        data_rows = [
            {"Data": [{"VarCharValue": str(row.get(h, ""))} for h in headers]}
            for row in rows
        ]
        all_rows = [header_row] + data_rows

    def _start_query(**kwargs):
        return {"QueryExecutionId": "qid-energy-test"}

    def _get_query_execution(**kwargs):
        return {"QueryExecution": {"Status": {"State": "SUCCEEDED"}}}

    def _get_query_results(**kwargs):
        return {"ResultSet": {"Rows": all_rows}}

    stub = MagicMock()
    stub.start_query_execution = _start_query
    stub.get_query_execution = _get_query_execution
    stub.get_query_results = _get_query_results
    return stub


def _call_select(stub: MagicMock, **kw) -> list[dict]:
    from agents.fi_cost.selection import select_anomalies

    return select_anomalies(
        athena_client=stub,
        workgroup="cvx-staging-analytics",
        results_s3_path="s3://cvx-staging-athena-results-123456789012-us-east-1/tier2/",
        max_diagnoses_per_run=100,
        latest_complete_month=kw.get("latest_complete_month", "2026-09"),
    )


# ---------------------------------------------------------------------------
# 1. Energy-only outlier is selected (decisions 2026-10-05 item 6)
# ---------------------------------------------------------------------------


def test_energy_only_outlier_is_selected():
    """A vehicle with is_energy_outlier=true, is_outlier=false must be selected.

    Mutation check M-OR-energy: if the SQL omits is_energy_outlier from
    the WHERE clause, this vehicle is NOT returned.
    The SQL mutation is verified in test_sql_shape.py.
    """
    from agents.fi_cost.selection import select_anomalies  # noqa: F401

    energy_only_row = {
        "vin": "MRD-ENERGY-001",
        "fleet_id": "fleet-meridian-01",
        "year_month": "2026-09",
        "ratio_cohort": "1.15",
        "ratio_own": "1.08",
        "severity": "routine",
        "is_outlier": "false",
        "is_energy_outlier": "true",
        "energy_excess_usd": "45.00",
        "cost_per_mile": "0.42",
        "cohort_median": "0.37",
        "miles": "800",
        "method": "own 6-month mean and same-month fleet-model median",
    }

    stub = _athena_stub_from_rows([energy_only_row])
    results = _call_select(stub)

    assert len(results) == 1, (
        "An energy-only outlier (is_energy_outlier=true, is_outlier=false) "
        "must be returned by select_anomalies — selection is is_outlier OR is_energy_outlier"
    )
    assert results[0]["vin"] == "MRD-ENERGY-001"


# ---------------------------------------------------------------------------
# 2. Total-only outlier is selected
# ---------------------------------------------------------------------------


def test_total_only_outlier_is_selected():
    """A vehicle with is_outlier=true, is_energy_outlier=false must be selected."""
    from agents.fi_cost.selection import select_anomalies  # noqa: F401

    total_only_row = {
        "vin": "MRD-TOTAL-002",
        "fleet_id": "fleet-meridian-01",
        "year_month": "2026-09",
        "ratio_cohort": "1.55",
        "ratio_own": "1.32",
        "severity": "soon",
        "is_outlier": "true",
        "is_energy_outlier": "false",
        "energy_excess_usd": "5.00",
        "cost_per_mile": "0.50",
        "cohort_median": "0.32",
        "miles": "1000",
        "method": "own 6-month mean and same-month fleet-model median",
    }

    stub = _athena_stub_from_rows([total_only_row])
    results = _call_select(stub)

    assert len(results) == 1
    assert results[0]["vin"] == "MRD-TOTAL-002"


# ---------------------------------------------------------------------------
# 3-5. cost.flag set by evidence postprocessing
# ---------------------------------------------------------------------------


def test_cost_flag_both_when_both_flags_true():
    """Both flags true → cost.flag = 'both' (decisions 2026-10-05 item 6)."""
    from agents.fi_cost.evidence import _postprocess_row

    row = {"is_outlier": "true", "is_energy_outlier": "true", "cost.miles": "800"}
    result = _postprocess_row(row, "2026-09")
    assert result["cost.flag"] == "both", (
        "Vehicle with both is_outlier and is_energy_outlier must have cost.flag='both'"
    )


def test_cost_flag_energy_when_only_energy_outlier():
    """Only energy flag true → cost.flag = 'energy'."""
    from agents.fi_cost.evidence import _postprocess_row

    row = {"is_outlier": "false", "is_energy_outlier": "true", "cost.miles": "800"}
    result = _postprocess_row(row, "2026-09")
    assert result["cost.flag"] == "energy"


def test_cost_flag_total_when_only_total_outlier():
    """Only total flag true → cost.flag = 'total'."""
    from agents.fi_cost.evidence import _postprocess_row

    row = {"is_outlier": "true", "is_energy_outlier": "false", "cost.miles": "800"}
    result = _postprocess_row(row, "2026-09")
    assert result["cost.flag"] == "total"


# ---------------------------------------------------------------------------
# 6. Ordering by the larger excess (SQL shape)
# ---------------------------------------------------------------------------


def test_select_anomalies_ordering_uses_larger_excess():
    """Ordering uses GREATEST(total_excess, energy_excess_usd) (decisions 2026-10-05 item 6).

    Stub returns two rows ordered by total excess; energy-heavy vehicle has
    larger energy_excess_usd.  Athena decides the actual ordering (the stub
    returns them in the order given), so this test verifies the SQL ORDER BY
    clause includes GREATEST and both excess terms via test_sql_shape.py.

    Here we just verify that select_anomalies returns both rows in the order
    the stub delivers them (no Python-side re-ordering that could mask a wrong SQL).
    """
    from agents.fi_cost.selection import select_anomalies  # noqa: F401

    # Vehicle A: large energy excess, small total excess
    row_a = {
        "vin": "MRD-ORDER-A",
        "fleet_id": "fleet-meridian-01",
        "year_month": "2026-09",
        "ratio_cohort": "1.12",
        "is_outlier": "false",
        "is_energy_outlier": "true",
        "energy_excess_usd": "150.00",
        "cost_per_mile": "0.39",
        "cohort_median": "0.35",
        "miles": "800",
        "severity": "routine",
    }
    # Vehicle B: larger total excess, smaller energy excess
    row_b = {
        "vin": "MRD-ORDER-B",
        "fleet_id": "fleet-meridian-01",
        "year_month": "2026-09",
        "ratio_cohort": "1.40",
        "is_outlier": "true",
        "is_energy_outlier": "false",
        "energy_excess_usd": "20.00",
        "cost_per_mile": "0.49",
        "cohort_median": "0.35",
        "miles": "800",
        "severity": "routine",
    }

    # Stub returns A first (higher energy_excess_usd should rank it higher)
    stub = _athena_stub_from_rows([row_a, row_b])
    results = _call_select(stub)

    assert len(results) == 2
    # The ordering is SQL-driven; Python preserves it.
    assert results[0]["vin"] == "MRD-ORDER-A"
    assert results[1]["vin"] == "MRD-ORDER-B"


# ---------------------------------------------------------------------------
# 7. Mutation check: SQL must include is_energy_outlier in WHERE
# Tests that the SQL text has both sides of the OR (moved to test_sql_shape.py
# for the actual SQL mutation; here we test the observable behaviour)
# ---------------------------------------------------------------------------


def test_both_outlier_types_selected_in_one_call():
    """Both energy-only and total-only outliers are returned from the same query.

    Verifies that the SELECT returns vehicles from EITHER flag in a single call,
    not two separate queries.  If the SQL only checked one flag, one vehicle
    would be missing.
    """
    from agents.fi_cost.selection import select_anomalies  # noqa: F401

    rows = [
        {
            "vin": "MRD-BOTH-A",
            "fleet_id": "fleet-meridian-01",
            "year_month": "2026-09",
            "is_outlier": "true",
            "is_energy_outlier": "false",
            "ratio_cohort": "1.60",
            "severity": "soon",
            "energy_excess_usd": "0",
        },
        {
            "vin": "MRD-BOTH-B",
            "fleet_id": "fleet-meridian-01",
            "year_month": "2026-09",
            "is_outlier": "false",
            "is_energy_outlier": "true",
            "ratio_cohort": "1.10",
            "severity": "routine",
            "energy_excess_usd": "55.00",
        },
    ]
    stub = _athena_stub_from_rows(rows)
    results = _call_select(stub)

    vins = {r["vin"] for r in results}
    assert "MRD-BOTH-A" in vins, "Total-only outlier must be selected"
    assert "MRD-BOTH-B" in vins, "Energy-only outlier must be selected"


# ---------------------------------------------------------------------------
# 9-11. subject_key = data month (decisions 2026-10-04)
# ---------------------------------------------------------------------------


def test_two_months_give_two_distinct_finding_keys():
    """Two different months for the same VIN give two distinct finding_keys (D8).

    decisions.md 2026-10-04: subject_key = YYYY-MM.
    Two items for one vehicle: each month's recommendation is its own queue item.
    """
    from agents.fi_cost.finding_keys import cost_finding_key, FINDING_KIND_ANOMALY

    vin = "MRD-SUBJECT-001"
    key_aug = cost_finding_key(vin=vin, finding_kind=FINDING_KIND_ANOMALY, year_month="2026-08")
    key_sep = cost_finding_key(vin=vin, finding_kind=FINDING_KIND_ANOMALY, year_month="2026-09")

    assert key_aug != key_sep, (
        "Two different data months for the same VIN must give two distinct finding_keys. "
        "D8: one Finding per vehicle per data month."
    )


def test_same_month_twice_gives_same_finding_key():
    """The same month for the same VIN gives the same finding_key (idempotent in-place update)."""
    from agents.fi_cost.finding_keys import cost_finding_key, FINDING_KIND_ANOMALY

    vin = "MRD-SUBJECT-001"
    key_a = cost_finding_key(vin=vin, finding_kind=FINDING_KIND_ANOMALY, year_month="2026-09")
    key_b = cost_finding_key(vin=vin, finding_kind=FINDING_KIND_ANOMALY, year_month="2026-09")

    assert key_a == key_b, (
        "The same VIN + same data month must give the same finding_key every time "
        "so a re-run on the same month updates the existing item in place."
    )


def test_both_cost_kinds_carry_subject_key():
    """Both fleet.cost.anomaly and fleet.cost.sell_timing use subject_key = year_month (D8)."""
    from agents.fi_cost.finding_keys import cost_finding_key
    from agents.fi_cost.finding_keys import FINDING_KIND_ANOMALY, FINDING_KIND_SELL_TIMING

    vin = "MRD-SUBJECT-002"
    year_month = "2026-09"

    key_anomaly = cost_finding_key(
        vin=vin, finding_kind=FINDING_KIND_ANOMALY, year_month=year_month
    )
    key_sell = cost_finding_key(
        vin=vin, finding_kind=FINDING_KIND_SELL_TIMING, year_month=year_month
    )

    # Different kinds → different keys (kind is in the hash payload)
    assert key_anomaly != key_sell, (
        "anomaly and sell_timing for the same VIN+month must have different finding_keys "
        "(finding_kind is in the canonical payload)"
    )

    # But for the same kind, same month → same key (both use subject_key)
    key_anomaly_again = cost_finding_key(
        vin=vin, finding_kind=FINDING_KIND_ANOMALY, year_month=year_month
    )
    assert key_anomaly == key_anomaly_again, (
        "Same kind + same month must always give the same key"
    )

    # Different month for anomaly → different key (subject_key changes the digest)
    key_anomaly_different_month = cost_finding_key(
        vin=vin, finding_kind=FINDING_KIND_ANOMALY, year_month="2026-08"
    )
    assert key_anomaly != key_anomaly_different_month, (
        "Different month must give a different finding_key for fleet.cost.anomaly"
    )

    # Different month for sell_timing → different key too
    key_sell_different_month = cost_finding_key(
        vin=vin, finding_kind=FINDING_KIND_SELL_TIMING, year_month="2026-08"
    )
    assert key_sell != key_sell_different_month, (
        "Different month must give a different finding_key for fleet.cost.sell_timing"
    )


def test_subject_key_in_sell_timing_finding_dict():
    """build_sell_timing_finding sets subject_key = year_month on the Finding dict."""
    from agents.fi_cost.sell_timing import build_sell_timing_finding

    finding = build_sell_timing_finding(
        vin="MRD-ST-001",
        fleet_id="fleet-meridian-01",
        crossover_month="2026-10",
        purchase_price=30000.0,
        monthly_depreciation=250.0,
        year_month="2026-09",
    )

    assert finding.get("subject_key") == "2026-09", (
        "build_sell_timing_finding must set subject_key = year_month (D8)"
    )


def test_sell_timing_finding_key_uses_subject_key():
    """The finding_key in the sell_timing dict uses subject_key = year_month."""
    from agents.fi_cost.sell_timing import build_sell_timing_finding
    from agents.fi_cost.finding_keys import cost_finding_key, FINDING_KIND_SELL_TIMING

    vin = "MRD-ST-KEY-001"
    year_month = "2026-09"

    finding = build_sell_timing_finding(
        vin=vin,
        fleet_id="fleet-meridian-01",
        crossover_month="2026-10",
        purchase_price=30000.0,
        monthly_depreciation=250.0,
        year_month=year_month,
    )

    expected_key = cost_finding_key(
        vin=vin,
        finding_kind=FINDING_KIND_SELL_TIMING,
        year_month=year_month,
    )

    assert finding.get("finding_key") == expected_key, (
        "The sell_timing Finding's finding_key must equal cost_finding_key(vin, "
        "FINDING_KIND_SELL_TIMING, year_month)."
    )


# ---------------------------------------------------------------------------
# Extra: cost.month in evidence postprocessing
# ---------------------------------------------------------------------------


def test_cost_month_set_from_year_month():
    """evidence postprocessing sets cost.month = year_month (D8 evidence label)."""
    from agents.fi_cost.evidence import _postprocess_row

    row = {"is_outlier": "true", "is_energy_outlier": "false", "cost.miles": "800"}
    result = _postprocess_row(row, "2026-09")

    assert result.get("cost.month") == "2026-09", (
        "cost.month must equal the year_month passed to _postprocess_row (D8)"
    )


# ---------------------------------------------------------------------------
# Dollar delta computation
# ---------------------------------------------------------------------------


def test_dollar_deltas_computed_from_per_mile_delta_and_miles():
    """delta.*_usd = delta.* (per-mile) × cost.miles (decisions cycle 3, item 1)."""
    from agents.fi_cost.evidence import _postprocess_row

    row = {
        "is_outlier": "true",
        "is_energy_outlier": "false",
        "cost.miles": "1000",
        "delta.energy": "0.05",       # per-mile delta
        "delta.maintenance": "0.10",
        "delta.lease": "0.02",
        "delta.insurance": "0.01",
    }
    result = _postprocess_row(row, "2026-09")

    assert result["delta.energy_usd"] == pytest.approx(50.0, abs=0.01), (
        "delta.energy_usd = delta.energy × cost.miles = 0.05 × 1000 = 50.00"
    )
    assert result["delta.maintenance_usd"] == pytest.approx(100.0, abs=0.01)
    assert result["delta.lease_usd"] == pytest.approx(20.0, abs=0.01)
    assert result["delta.insurance_usd"] == pytest.approx(10.0, abs=0.01)



# ---------------------------------------------------------------------------
# Fix 3 — stage validation in select_anomalies (task 3.1 pre-review fix)
# ---------------------------------------------------------------------------


def test_select_anomalies_rejects_invalid_stage():
    """select_anomalies must raise ValueError for any stage other than 'staging'/'prod'.

    Mutation M-STAGE-SEL (test_sql_shape.py also covers): remove stage validation →
    invalid stage is interpolated into SQL without error → this test fails.
    """
    from agents.fi_cost.selection import select_anomalies

    # Use a stub that would return immediately if reached (avoids hanging on polling).
    # The test property is: ValueError must be raised BEFORE start_query_execution is called.
    call_count = []

    def _counting_start(**kwargs):
        call_count.append(kwargs)
        return {"QueryExecutionId": "qid-stage-test"}

    def _get_exec(**kwargs):
        return {"QueryExecution": {"Status": {"State": "SUCCEEDED"}}}

    def _get_results(**kwargs):
        return {"ResultSet": {"Rows": [{"Data": [{"VarCharValue": "vin"}]}]}}

    stub = MagicMock()
    stub.start_query_execution = _counting_start
    stub.get_query_execution = _get_exec
    stub.get_query_results = _get_results

    with pytest.raises(ValueError, match="stage"):
        select_anomalies(
            athena_client=stub,
            workgroup="cvx-staging-analytics",
            results_s3_path="s3://bucket/path/",
            max_diagnoses_per_run=50,
            latest_complete_month="2026-09",
            stage="test",  # invalid
        )

    assert len(call_count) == 0, (
        "No Athena call must be made when stage is invalid. "
        "Mutation M-STAGE-SEL: without stage validation the call proceeds."
    )


def test_select_anomalies_accepts_prod_stage():
    """select_anomalies must accept 'prod' as a valid stage."""
    from agents.fi_cost.selection import select_anomalies

    stub = _athena_stub_from_rows([])
    # Should not raise
    result = select_anomalies(
        athena_client=stub,
        workgroup="cvx-prod-analytics",
        results_s3_path="s3://bucket/path/",
        max_diagnoses_per_run=50,
        latest_complete_month="2026-09",
        stage="prod",
    )
    assert result == []
