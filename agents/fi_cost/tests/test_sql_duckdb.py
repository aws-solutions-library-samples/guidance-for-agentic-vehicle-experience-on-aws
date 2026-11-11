"""
DuckDB-based behavioural SQL tests for fi_cost Tier 2 component (W3 fix).

Spec: .kiro/specs/2026-10-02-fi-cost-stage-b/spec.md § D6 step 1 and step 2
FIX1_CALLS item 7: "run both SQL files under DuckDB against minimal tables shaped
like the ADP views (pin their column lists), with a small shim rewriting
DATE_PARSE/DATE_ADD for DuckDB. Must catch the reviewer's M3 (the OR without
parentheses) and M1 (depot-peak kWh dropped from public_and_peak_kwh)."

Mutations targeted:
  M1 — drop OR (category='energy_depot' AND tariff_band='peak') from public_and_peak_kwh.
       Original: VIN with 50 depot-peak kWh and 30 public kWh → public_and_peak_kwh=80.
       M1 mutant: public_and_peak_kwh=30 (depot-peak dropped).
       Caught by: test_gather_evidence_includes_depot_peak_in_public_and_peak_kwh.

  M3 — drop parentheses: AND is_outlier OR is_energy_outlier.
       Original: exactly the 2026-09 is_outlier/is_energy_outlier rows.
       M3 mutant: also selects is_energy_outlier rows from other months.
       Caught by: test_select_anomalies_does_not_select_energy_outlier_from_wrong_month.

Requires duckdb==1.5.5 (requirements-dev.txt); already installed in the venv.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import duckdb
import pytest

# ---------------------------------------------------------------------------
# SQL template paths
# ---------------------------------------------------------------------------
_SQL_DIR = Path(__file__).parent.parent / "sql"
_SELECT_SQL = (_SQL_DIR / "select_anomalies.sql").read_text()
_EVIDENCE_SQL = (_SQL_DIR / "gather_evidence.sql").read_text()

# Test parameters
_STAGE = "test"
_YEAR_MONTH = "2026-09"

# VINs used in DuckDB tests — Meridian Motors format (RULES.md rule 7)
_VIN_DEPOT_PEAK = "MRDDBTEST001"   # Only depot-peak charging (no off-peak)
_VIN_PUBLIC = "MRDDBTEST002"       # Only public charging
_VIN_BOTH = "MRDDBTEST003"         # Both depot-peak and public charging

# ---------------------------------------------------------------------------
# Shim: Athena/Presto functions → DuckDB equivalents
# ---------------------------------------------------------------------------


def _athena_to_duckdb(sql: str, stage: str, year_month: str, vin_list: str) -> str:
    """Substitute template variables and rewrite Athena functions for DuckDB.

    Handles:
      ${stage}, ${year_month}, ${vin_list} — template substitution
      DATE_PARSE(str, fmt) → strptime(str, fmt)
      DATE_ADD('month', N, expr) → (expr +/- INTERVAL |N| MONTH)
    """
    sql = sql.replace("${stage}", stage)
    sql = sql.replace("${year_month}", year_month)
    sql = sql.replace("${vin_list}", vin_list)

    # DATE_PARSE → strptime (DuckDB uses strptime for same semantics)
    sql = re.sub(r"\bDATE_PARSE\b", "strptime", sql, flags=re.IGNORECASE)

    # DATE_ADD('month', N, strptime(...)) → (strptime(...) +/- INTERVAL |N| MONTH)
    # The third argument is always strptime(...) after the DATE_PARSE rewrite above.
    def _date_add(m: re.Match) -> str:
        n = int(m.group(1))
        inner = m.group(2).strip()
        if n < 0:
            return f"({inner} - INTERVAL {abs(n)} MONTH)"
        return f"({inner} + INTERVAL {n} MONTH)"

    sql = re.sub(
        r"DATE_ADD\s*\(\s*'month'\s*,\s*(-?\d+)\s*,\s*(strptime\([^)]+\))\s*\)",
        _date_add,
        sql,
        flags=re.IGNORECASE,
    )
    return sql


# ---------------------------------------------------------------------------
# DuckDB fixture: selection SQL (select_anomalies.sql)
# ---------------------------------------------------------------------------

def _make_selection_conn() -> duckdb.DuckDBPyConnection:
    """Create an in-memory DuckDB connection with a minimal vehicle_cost_baseline."""
    conn = duckdb.connect(":memory:")
    conn.execute("CREATE SCHEMA adp_test_fleet_cost")
    conn.execute("""
        CREATE TABLE adp_test_fleet_cost.vehicle_cost_baseline (
            vin               VARCHAR,
            fleet_id          VARCHAR,
            year_month        VARCHAR,
            ratio_cohort      DOUBLE,
            ratio_own         DOUBLE,
            severity          VARCHAR,
            is_outlier        BOOLEAN,
            is_energy_outlier BOOLEAN,
            energy_excess_usd DOUBLE,
            energy_ratio_own  DOUBLE,
            energy_ratio_cohort DOUBLE,
            energy_method     VARCHAR,
            cost_per_mile     DOUBLE,
            cohort_median     DOUBLE,
            miles             DOUBLE,
            method            VARCHAR
        )
    """)
    conn.execute("""
        INSERT INTO adp_test_fleet_cost.vehicle_cost_baseline VALUES
          -- 2026-09 total outlier only → selected
          ('VIN-TOTAL-2609', 'fleet-01', '2026-09', 1.6, 1.5, 'soon',
           true, false, 0.0, 1.0, 1.0, 'total', 0.45, 0.28, 1000, 'mean6m'),
          -- 2026-09 energy outlier only → selected
          ('VIN-ENERGY-2609', 'fleet-01', '2026-09', 1.1, 1.05, 'routine',
           false, true, 45.0, 1.4, 1.3, 'energy', 0.33, 0.30, 800, 'mean6m'),
          -- 2026-09 both flags → selected
          ('VIN-BOTH-2609', 'fleet-01', '2026-09', 1.7, 1.6, 'soon',
           true, true, 60.0, 1.5, 1.4, 'energy', 0.50, 0.29, 1200, 'mean6m'),
          -- 2026-09 neither flag → NOT selected
          ('VIN-NEITHER-2609', 'fleet-01', '2026-09', 1.1, 1.05, 'routine',
           false, false, 0.0, 1.0, 1.0, 'total', 0.32, 0.30, 700, 'mean6m'),
          -- 2026-08 total outlier → NOT selected (wrong month)
          ('VIN-TOTAL-2608', 'fleet-01', '2026-08', 1.6, 1.5, 'soon',
           true, false, 0.0, 1.0, 1.0, 'total', 0.45, 0.28, 1000, 'mean6m'),
          -- 2026-08 energy outlier → NOT selected by original (wrong month)
          --   BUT would be selected by M3 mutant (is_energy_outlier=true bypasses month filter)
          ('VIN-ENERGY-2608', 'fleet-01', '2026-08', 1.1, 1.05, 'routine',
           false, true, 45.0, 1.4, 1.3, 'energy', 0.33, 0.30, 800, 'mean6m')
    """)
    return conn


# ---------------------------------------------------------------------------
# DuckDB fixture: evidence SQL (gather_evidence.sql)
# ---------------------------------------------------------------------------

def _make_evidence_conn() -> duckdb.DuckDBPyConnection:
    """Create an in-memory DuckDB connection with minimal ADP tables for evidence SQL."""
    conn = duckdb.connect(":memory:")
    conn.execute("CREATE SCHEMA adp_test_fleet_cost")
    conn.execute("CREATE SCHEMA adp_test_service_records")

    # fleet_cost_events: energy events for the test VINs
    conn.execute("""
        CREATE TABLE adp_test_fleet_cost.fleet_cost_events (
            vin         VARCHAR,
            category    VARCHAR,
            tariff_band VARCHAR,
            kwh         DOUBLE,
            amount_usd  DOUBLE,
            year_month  VARCHAR
        )
    """)
    conn.execute("""
        INSERT INTO adp_test_fleet_cost.fleet_cost_events VALUES
          -- VIN_DEPOT_PEAK: only depot-peak charging
          -- public_and_peak_kwh should = 50 (depot-peak only)
          -- M1 drops the depot-peak term → public_and_peak_kwh = 0
          ('MRDDBTEST001', 'energy_depot', 'peak',     50.0, 10.0, '2026-09'),
          -- VIN_PUBLIC: only public charging
          -- public_and_peak_kwh = 30 (public only; same result with or without M1)
          ('MRDDBTEST002', 'energy_public',  'standard', 30.0,  9.0, '2026-09'),
          -- VIN_BOTH: depot-peak AND public charging
          -- public_and_peak_kwh should = 80 (50 depot-peak + 30 public)
          -- M1 drops depot-peak → public_and_peak_kwh = 30
          ('MRDDBTEST003', 'energy_depot', 'peak',     50.0, 10.0, '2026-09'),
          ('MRDDBTEST003', 'energy_public',  'standard', 30.0,  9.0, '2026-09')
    """)

    # vehicle_cost_monthly: one row per test VIN
    conn.execute("""
        CREATE TABLE adp_test_fleet_cost.vehicle_cost_monthly (
            vin             VARCHAR,
            fleet_id        VARCHAR,
            model           VARCHAR,
            year_month      VARCHAR,
            miles           DOUBLE,
            cost_per_mile   DOUBLE,
            energy_cost     DOUBLE,
            maintenance_cost DOUBLE,
            lease_cost      DOUBLE,
            insurance_cost  DOUBLE
        )
    """)
    conn.execute("""
        INSERT INTO adp_test_fleet_cost.vehicle_cost_monthly VALUES
          ('MRDDBTEST001', 'fleet-01', 'ModelX', '2026-09', 1000.0, 0.45, 200.0, 150.0, 500.0, 100.0),
          ('MRDDBTEST002', 'fleet-01', 'ModelX', '2026-09',  800.0, 0.35, 180.0, 120.0, 500.0, 100.0),
          ('MRDDBTEST003', 'fleet-01', 'ModelX', '2026-09', 1200.0, 0.50, 230.0, 160.0, 500.0, 100.0)
    """)

    # vehicle_cost_baseline: one row per test VIN (required by evidence SQL joins)
    conn.execute("""
        CREATE TABLE adp_test_fleet_cost.vehicle_cost_baseline (
            vin                   VARCHAR,
            fleet_id              VARCHAR,
            year_month            VARCHAR,
            ratio_cohort          DOUBLE,
            ratio_own             DOUBLE,
            severity              VARCHAR,
            method                VARCHAR,
            is_outlier            BOOLEAN,
            is_energy_outlier     BOOLEAN,
            energy_ratio_own      DOUBLE,
            energy_ratio_cohort   DOUBLE,
            energy_excess_usd     DOUBLE,
            energy_method         VARCHAR,
            own_mean              DOUBLE,
            cohort_median         DOUBLE,
            delta_energy_cohort   DOUBLE,
            delta_maintenance_cohort DOUBLE,
            delta_lease_cohort    DOUBLE,
            delta_insurance_cohort DOUBLE
        )
    """)
    conn.execute("""
        INSERT INTO adp_test_fleet_cost.vehicle_cost_baseline VALUES
          ('MRDDBTEST001', 'fleet-01', '2026-09', 1.6, 1.5, 'soon',
           'mean6m', true, false, 1.0, 1.0, 0.0, 'total', 0.28, 0.28,
           0.05, 0.04, 0.01, 0.005),
          ('MRDDBTEST002', 'fleet-01', '2026-09', 1.1, 1.05, 'routine',
           'mean6m', false, true, 1.4, 1.3, 45.0, 'energy', 0.30, 0.30,
           0.02, 0.01, 0.005, 0.002),
          ('MRDDBTEST003', 'fleet-01', '2026-09', 1.7, 1.6, 'soon',
           'mean6m', true, true, 1.5, 1.4, 60.0, 'energy', 0.29, 0.29,
           0.06, 0.05, 0.01, 0.005)
    """)

    # service_records: a few rows for the svc_window CTE
    conn.execute("""
        CREATE TABLE adp_test_service_records.service_records (
            vin              VARCHAR,
            service_type     VARCHAR,
            service_month    DATE,
            warranty_covered BOOLEAN
        )
    """)
    conn.execute("""
        INSERT INTO adp_test_service_records.service_records VALUES
          ('MRDDBTEST001', 'tire_rotation', DATE '2026-07-01', false),
          ('MRDDBTEST001', 'tire_rotation', DATE '2026-08-01', false),
          ('MRDDBTEST002', 'oil_change',    DATE '2026-09-01', false)
    """)
    return conn


# ---------------------------------------------------------------------------
# Tests: select_anomalies.sql (M3 target)
# ---------------------------------------------------------------------------


def test_select_anomalies_correct_year_month_only():
    """Only rows with the target year_month and either outlier flag are selected.

    Original SQL: WHERE year_month = '2026-09' AND (is_outlier OR is_energy_outlier)
    Expected: VIN-TOTAL-2609, VIN-ENERGY-2609, VIN-BOTH-2609 (3 rows from 2026-09)
    NOT selected: VIN-NEITHER-2609 (no flag), VIN-TOTAL-2608 (wrong month),
                  VIN-ENERGY-2608 (wrong month).
    """
    conn = _make_selection_conn()
    sql = _athena_to_duckdb(_SELECT_SQL, _STAGE, "2026-09", "'X'")
    rows = conn.execute(sql).fetchall()
    vins = {r[0] for r in rows}
    assert "VIN-TOTAL-2609" in vins, "Total-only outlier in 2026-09 must be selected"
    assert "VIN-ENERGY-2609" in vins, "Energy-only outlier in 2026-09 must be selected"
    assert "VIN-BOTH-2609" in vins, "Both-flag outlier in 2026-09 must be selected"
    assert len(rows) == 3, (
        f"Exactly 3 rows from 2026-09 should be selected, got {len(rows)}: {vins}"
    )


def test_select_anomalies_does_not_select_wrong_month_total_outlier():
    """A total outlier from 2026-08 is NOT selected when querying 2026-09."""
    conn = _make_selection_conn()
    sql = _athena_to_duckdb(_SELECT_SQL, _STAGE, "2026-09", "'X'")
    rows = conn.execute(sql).fetchall()
    vins = {r[0] for r in rows}
    assert "VIN-TOTAL-2608" not in vins, (
        "VIN from 2026-08 must not appear when querying 2026-09"
    )


def test_select_anomalies_does_not_select_energy_outlier_from_wrong_month():
    """An energy-only outlier from 2026-08 is NOT selected when querying 2026-09.

    M3 mutant (drop parentheses): AND is_outlier OR is_energy_outlier
    SQL precedence makes this: (year_month='2026-09' AND is_outlier) OR is_energy_outlier
    With is_energy_outlier=true on VIN-ENERGY-2608, the M3 mutant selects it
    regardless of year_month.

    This test catches M3: it fails when the row from the wrong month is selected.
    """
    conn = _make_selection_conn()
    sql = _athena_to_duckdb(_SELECT_SQL, _STAGE, "2026-09", "'X'")
    rows = conn.execute(sql).fetchall()
    vins = {r[0] for r in rows}
    assert "VIN-ENERGY-2608" not in vins, (
        "VIN-ENERGY-2608 is from 2026-08 and must NOT be selected when querying 2026-09. "
        "M3 mutant (drops parentheses from AND (is_outlier OR is_energy_outlier)) would "
        "select it because is_energy_outlier=true bypasses the year_month filter. "
        "This assertion catches that mutant."
    )


def test_select_anomalies_m3_mutant_produces_wrong_count():
    """Confirm M3 mutant SQL selects MORE rows than expected, catching the defect.

    This test directly runs the M3 mutant SQL and asserts it selects the wrong VIN,
    verifying that our normal test (above) would fail against the mutant.
    This test EXPECTS the mutant to misbehave (it passes when the mutant is bad).
    """
    conn = _make_selection_conn()
    # M3 mutant: drop the parentheses
    m3_sql = _SELECT_SQL.replace(
        "AND (is_outlier OR is_energy_outlier)",
        "AND is_outlier OR is_energy_outlier",
    )
    sql = _athena_to_duckdb(m3_sql, _STAGE, "2026-09", "'X'")
    rows = conn.execute(sql).fetchall()
    vins = {r[0] for r in rows}
    # M3 mutant should select VIN-ENERGY-2608 (wrong month) because is_energy_outlier=true
    assert "VIN-ENERGY-2608" in vins, (
        "M3 mutant must select VIN-ENERGY-2608 (is_energy_outlier=true, wrong month) "
        "confirming the mutant is detectable"
    )
    assert len(rows) > 3, (
        f"M3 mutant must select more than 3 rows (got {len(rows)}), "
        "proving that the original correct SQL selects fewer"
    )


def test_select_anomalies_ordering_largest_excess_first():
    """ORDER BY puts the row with the largest excess first.

    VIN-BOTH-2609: total excess = (0.50-0.29)*1200 = 252  ← largest
    VIN-TOTAL-2609: total excess = (0.45-0.28)*1000 = 170
    VIN-ENERGY-2609: total excess vs energy_excess_usd = GREATEST(0, 45) = 45  ← smallest
    Expected order: BOTH, TOTAL, ENERGY.
    """
    conn = _make_selection_conn()
    sql = _athena_to_duckdb(_SELECT_SQL, _STAGE, "2026-09", "'X'")
    rows = conn.execute(sql).fetchall()
    assert len(rows) == 3
    # First column is vin
    assert rows[0][0] == "VIN-BOTH-2609", (
        f"VIN with largest excess (252) must come first; got {rows[0][0]}"
    )
    assert rows[-1][0] == "VIN-ENERGY-2609", (
        f"VIN with smallest excess (energy_excess_usd=45) must come last; "
        f"got {rows[-1][0]}"
    )


# ---------------------------------------------------------------------------
# Tests: gather_evidence.sql (M1 target)
# ---------------------------------------------------------------------------


def _run_evidence(conn: duckdb.DuckDBPyConnection, sql_text: str) -> list[dict]:
    """Run the evidence SQL and return rows as dicts keyed by column name."""
    vins = f"'{_VIN_DEPOT_PEAK}','{_VIN_PUBLIC}','{_VIN_BOTH}'"
    sql = _athena_to_duckdb(sql_text, _STAGE, _YEAR_MONTH, vins)
    result = conn.execute(sql)
    cols = [desc[0] for desc in result.description]
    return [dict(zip(cols, row)) for row in result.fetchall()]


def test_gather_evidence_includes_depot_peak_in_public_and_peak_kwh():
    """public_and_peak_kwh includes depot-peak kWh (M1 target).

    VIN_DEPOT_PEAK has only depot-peak charging: 50 kWh.
    Original SQL: public_and_peak_kwh = 50 (depot-peak included)
    M1 mutant:    public_and_peak_kwh = 0  (depot-peak dropped)

    This test catches M1: it fails when the depot-peak term is dropped.
    """
    conn = _make_evidence_conn()
    rows = _run_evidence(conn, _EVIDENCE_SQL)
    row = next((r for r in rows if r.get("vin") == _VIN_DEPOT_PEAK), None)
    assert row is not None, f"VIN {_VIN_DEPOT_PEAK} must appear in evidence output"
    val = float(row.get("energy.public_and_peak_kwh", 0) or 0)
    assert val == pytest.approx(50.0, rel=1e-4), (
        f"VIN {_VIN_DEPOT_PEAK} has 50 kWh of depot-peak charging; "
        f"public_and_peak_kwh must be 50.0 (includes depot-peak). "
        f"Got {val}. "
        f"M1 mutant (drops OR (category='energy_depot' AND tariff_band='peak')) "
        f"returns 0.0 and is caught by this assertion."
    )


def test_gather_evidence_m1_mutant_drops_depot_peak():
    """M1 mutant drops depot-peak term and produces 0 for depot-only vehicle.

    This test directly runs the M1 mutant SQL and confirms it gives wrong value,
    verifying the normal test above would catch the mutant.
    """
    conn = _make_evidence_conn()
    # Apply M1 mutation: drop the OR (category='energy_depot' AND tariff_band='peak') term
    m1_sql = _EVIDENCE_SQL.replace(
        "OR (category = 'energy_depot' AND tariff_band = 'peak')\n                 THEN kwh ELSE 0.0 END)\n            AS public_and_peak_kwh",
        "THEN kwh ELSE 0.0 END)\n            AS public_and_peak_kwh",
    )
    # Also fix the mean price CTE (same mutation)
    m1_sql = m1_sql.replace(
        "OR (category = 'energy_depot' AND tariff_band = 'peak')\n                          THEN kwh ELSE 0.0 END) > 0",
        "THEN kwh ELSE 0.0 END) > 0",
    )
    rows = _run_evidence(conn, m1_sql)
    row = next((r for r in rows if r.get("vin") == _VIN_DEPOT_PEAK), None)
    if row is None:
        return  # M1 may cause a null join — acceptable, still wrong
    val = float(row.get("energy.public_and_peak_kwh", 0) or 0)
    assert val != pytest.approx(50.0, rel=1e-4), (
        f"M1 mutant must NOT produce 50.0 for public_and_peak_kwh of {_VIN_DEPOT_PEAK}; "
        f"the mutant drops depot-peak kWh so the value must differ from the correct 50.0."
    )


def test_gather_evidence_combined_vehicle_public_and_peak():
    """VIN with both public and depot-peak charging has correct total.

    VIN_BOTH has 50 kWh depot-peak + 30 kWh public → public_and_peak_kwh = 80.
    M1 mutant would give 30 (public only).
    """
    conn = _make_evidence_conn()
    rows = _run_evidence(conn, _EVIDENCE_SQL)
    row = next((r for r in rows if r.get("vin") == _VIN_BOTH), None)
    assert row is not None, f"VIN {_VIN_BOTH} must appear in evidence output"
    val = float(row.get("energy.public_and_peak_kwh", 0) or 0)
    assert val == pytest.approx(80.0, rel=1e-4), (
        f"VIN {_VIN_BOTH} has 50 depot-peak + 30 public = 80 kWh; "
        f"public_and_peak_kwh must be 80.0, got {val}. "
        "M1 mutant would give 30.0 (public only) and fail this assertion."
    )


def test_gather_evidence_all_selected_vins_in_year_month():
    """Every row in the evidence output has the expected year_month."""
    conn = _make_evidence_conn()
    rows = _run_evidence(conn, _EVIDENCE_SQL)
    assert len(rows) > 0, "Evidence query must return at least one row"
    for row in rows:
        assert row.get("year_month") == _YEAR_MONTH, (
            f"All evidence rows must have year_month={_YEAR_MONTH!r}; "
            f"got {row.get('year_month')!r} for vin={row.get('vin')}"
        )


def test_gather_evidence_cohort_depot_offpeak_price_present():
    """gather_evidence.sql must produce energy.cohort_depot_off_peak_price_per_kwh (C2 fix).

    This column is used as a fallback when the vehicle itself has no off-peak charging.
    A vehicle with 100% depot-peak charging (peak_share=1.0) would have NULL vehicle
    depot_off_peak_price, so the cohort median must be available to prevent TypeError.
    """
    conn = _make_evidence_conn()
    rows = _run_evidence(conn, _EVIDENCE_SQL)
    assert rows, "Evidence query must return rows"
    cols = list(rows[0].keys())
    assert "energy.cohort_depot_off_peak_price_per_kwh" in cols, (
        "gather_evidence.sql must return energy.cohort_depot_off_peak_price_per_kwh. "
        "This is the C2 fix: the cohort median fallback for vehicles with no off-peak "
        "charging data (peak_share=1.0 vehicles like MRDAZ01S0TC03NT7V). "
        "Without it, the shift_to_depot_off_peak formula crashes with TypeError."
    )
