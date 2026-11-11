"""DuckDB snapshot-filter tests for the 9 stage-D AVX snapshot reads (G3F6F1.3).

Every read in scope filters rows to the newest snapshot date via::

    snapshot_date = (SELECT MAX(snapshot_date) FROM <same-table>)

or, for battery / brake / tire selection SQL::

    mv.snapshot_date = (SELECT MAX(snapshot_date) FROM <managed_vehicles>)

For each of the 9 reads this file verifies three things:

  1. **newest_only** – two-snapshot fixture; only newest-snapshot rows come back
     (exact row set checked via VINs + key discriminating columns).
  2. **absent_today** – only the old snapshot is present; the read still works
     and returns the old rows (newest available = old).
  3. **mutant_le_fails** – ``snapshot_date = (SELECT MAX`` replaced with
     ``snapshot_date <= (SELECT MAX``; the returned row set differs from the
     exact-newest assertion, proving the ``=`` filter is load-bearing.

No Athena-only syntax was found across the 9 reads:
  - ``starts_with()`` is supported by DuckDB 1.x.
  - ``CAST(x AS VARCHAR)`` is standard SQL.
  - ``INNER JOIN … ON … AND (uncorrelated subquery)`` is standard SQL.
  - ``needs_replacement = true`` uses the DuckDB boolean literal.
No harness rewrites required.

Verified with DuckDB 1.5.5 (the venv-pinned version).
"""
from __future__ import annotations

import re

import duckdb
import pytest

from agents.fi_fleet_health.selection import (
    _DAILY_USAGE_SQL,
    _FAULT_SQL,
    _MANAGED_VINS_SQL,
    _PM_SQL,
)
from agents.fi_quality.readers import (
    _CO_OCCURRING_SQL,
    _CONTROLS_SQL,
    _safe_vin_list,
)
from agents.health.adp_battery_source import AdpBatterySource
from agents.health.adp_brake_source import AdpBrakeSource
from agents.health.adp_tire_source import AdpTireSource

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

STAGE = "staging"
DATE_OLD = "2024-01-01"   # older snapshot
DATE_NEW = "2024-01-02"   # newer snapshot → MAX in the two-snapshot fixture

# VIN_A: present in OLD (fleet_A) and NEW (fleet_B); fleet changes between dates.
# VIN_B: present in OLD only; absent from the new snapshot.
# 17 chars, all in [A-HJ-NPR-Z0-9] (VIN alphabet, no I/O/Q).
VIN_A = "MRD1AAAAAAA111111"
VIN_B = "MRD2BBBBBBB222222"

# ---------------------------------------------------------------------------
# Mutant helper: replace each "snapshot_date = (" with "snapshot_date <= ("
# ---------------------------------------------------------------------------

_EQ_RE = re.compile(r"snapshot_date\s*=\s*\(")


def _mutant(sql: str) -> str:
    """Return *sql* with every snapshot-date equality changed to <=."""
    return _EQ_RE.sub("snapshot_date <= (", sql)


# ---------------------------------------------------------------------------
# DuckDB in-memory fixture
# ---------------------------------------------------------------------------

def _q(con: duckdb.DuckDBPyConnection, sql: str) -> list[tuple]:
    """Execute *sql* and return all rows as a list of tuples."""
    return con.execute(sql).fetchall()


def _cols(con: duckdb.DuckDBPyConnection, sql: str) -> tuple[list[str], list[tuple]]:
    """Return (column_names, rows) for *sql*."""
    result = con.execute(sql)
    cols = [d[0] for d in result.description]
    rows = result.fetchall()
    return cols, rows


def _make_conn(*, two_snapshots: bool = True) -> duckdb.DuckDBPyConnection:
    """Return a fresh in-memory DuckDB connection populated with test fixtures.

    All six schemas used by the 9 reads are created.  When *two_snapshots* is
    True (default), both DATE_OLD and DATE_NEW rows are inserted into every
    snapshot table.  When False, only DATE_OLD rows are inserted (the
    'absent-today' scenario: MAX(snapshot_date) = DATE_OLD).
    """
    con = duckdb.connect(":memory:")

    fh = f"adp_{STAGE}_fleet_health"
    fr = f"adp_{STAGE}_fleet_recall"
    vi = f"adp_{STAGE}_vehicle_identity"
    eu = f"adp_{STAGE}_energy_usage"
    sr = f"adp_{STAGE}_service_records"
    th = f"adp_{STAGE}_tire_health"

    for schema in (fh, fr, vi, eu, sr, th):
        con.execute(f"CREATE SCHEMA {schema}")

    # -- vehicle_fault_status_daily (reads 1, 6) ----------------------------
    con.execute(f"""CREATE TABLE {fh}.vehicle_fault_status_daily (
        vin VARCHAR, code VARCHAR, fleet_id VARCHAR, severity_band VARCHAR,
        last_seen DATE, status VARCHAR, snapshot_date DATE)""")
    # OLD: VIN_A/P0001 in fleet_A; VIN_A/C0002 (co-occurring, non-P0001); VIN_B/P0002
    con.execute(f"INSERT INTO {fh}.vehicle_fault_status_daily VALUES "
                f"('{VIN_A}','P0001','fleet_A','soon','{DATE_OLD}','open','{DATE_OLD}'),"
                f"('{VIN_A}','C0002','fleet_A','soon','{DATE_OLD}','open','{DATE_OLD}'),"
                f"('{VIN_B}','P0002','fleet_A','p0',  '{DATE_OLD}','open','{DATE_OLD}')")
    if two_snapshots:
        # NEW: VIN_A/P0001 fleet_B; VIN_A/C0001 (co-occurring, non-P0001); VIN_B absent
        con.execute(f"INSERT INTO {fh}.vehicle_fault_status_daily VALUES "
                    f"('{VIN_A}','P0001','fleet_B','soon','{DATE_NEW}','open','{DATE_NEW}'),"
                    f"('{VIN_A}','C0001','fleet_B','soon','{DATE_NEW}','open','{DATE_NEW}')")

    con.execute(f"CREATE TABLE {fh}.fault_codes (code VARCHAR, directive_text VARCHAR)")
    con.execute(f"INSERT INTO {fh}.fault_codes VALUES "
                f"('P0001','Directive A'),('P0002','Directive B'),"
                f"('C0001','Directive C'),('C0002','Directive D')")

    # -- pm_vehicle_status_daily (read 2) -----------------------------------
    con.execute(f"""CREATE TABLE {fh}.pm_vehicle_status_daily (
        vin VARCHAR, fleet_id VARCHAR, task_code VARCHAR, status VARCHAR, snapshot_date DATE)""")
    con.execute(f"INSERT INTO {fh}.pm_vehicle_status_daily VALUES "
                f"('{VIN_A}','fleet_A','OIL_CHANGE','overdue','{DATE_OLD}'),"
                f"('{VIN_B}','fleet_A','TIRE_ROTATE','due',   '{DATE_OLD}')")
    if two_snapshots:
        con.execute(f"INSERT INTO {fh}.pm_vehicle_status_daily VALUES "
                    f"('{VIN_A}','fleet_B','OIL_CHANGE','overdue','{DATE_NEW}')")

    # -- vehicle_utilisation_daily (read 4) ---------------------------------
    con.execute(f"""CREATE TABLE {fh}.vehicle_utilisation_daily (
        vin VARCHAR, avg_miles_30d DOUBLE, snapshot_date DATE)""")
    con.execute(f"INSERT INTO {fh}.vehicle_utilisation_daily VALUES "
                f"('{VIN_A}',100.0,'{DATE_OLD}'),('{VIN_B}',150.0,'{DATE_OLD}')")
    if two_snapshots:
        con.execute(f"INSERT INTO {fh}.vehicle_utilisation_daily VALUES "
                    f"('{VIN_A}',200.0,'{DATE_NEW}')")

    # -- managed_vehicles (reads 3, 5, 7, 8, 9) ----------------------------
    con.execute(f"""CREATE TABLE {fr}.managed_vehicles (
        vin VARCHAR, fleet_id VARCHAR, snapshot_date DATE)""")
    con.execute(f"INSERT INTO {fr}.managed_vehicles VALUES "
                f"('{VIN_A}','fleet_A',    '{DATE_OLD}'),"
                f"('{VIN_B}','fleet_B_old','{DATE_OLD}')")
    if two_snapshots:
        # NEW: only VIN_A (VIN_B dropped from managed pool)
        con.execute(f"INSERT INTO {fr}.managed_vehicles VALUES "
                    f"('{VIN_A}','fleet_B','{DATE_NEW}')")

    # -- vehicle_identity (read 5: controls SQL) ----------------------------
    con.execute(f"""CREATE TABLE {vi}.vehicle_identity (
        vin VARCHAR, model VARCHAR, model_year INTEGER,
        manufacture_date DATE, assembly_plant VARCHAR)""")
    con.execute(f"INSERT INTO {vi}.vehicle_identity VALUES "
                f"('{VIN_A}','ModelX',2020,'2020-06-01','PlantA'),"
                f"('{VIN_B}','ModelX',2020,'2020-06-01','PlantA')")

    # -- energy_usage (read 7: battery managed VIN selection) ---------------
    con.execute(f"""CREATE TABLE {eu}.energy_usage (
        vin VARCHAR, state_of_health_pct DOUBLE, battery_age_days INTEGER)""")
    con.execute(f"INSERT INTO {eu}.energy_usage VALUES "
                f"('{VIN_A}',92.0,365),('{VIN_B}',88.0,400)")

    # -- service_records (read 8: brake managed VIN selection) --------------
    con.execute(f"""CREATE TABLE {sr}.service_records (
        vin VARCHAR, service_type VARCHAR, outcome VARCHAR)""")
    con.execute(f"INSERT INTO {sr}.service_records VALUES "
                f"('{VIN_A}','brake_service','follow_up_required'),"
                f"('{VIN_B}','brake_service','parts_pending')")

    # -- tire_health (read 9: tire managed VIN selection) -------------------
    con.execute(f"""CREATE TABLE {th}.tire_health (
        vin VARCHAR, tire_position VARCHAR, needs_replacement BOOLEAN)""")
    con.execute(f"INSERT INTO {th}.tire_health VALUES "
                f"('{VIN_A}','FL',true),('{VIN_B}','FL',true)")

    return con


# ---------------------------------------------------------------------------
# SQL builders for reads that require parameter substitution or class methods
# ---------------------------------------------------------------------------

def _fault_sql() -> str:
    return _FAULT_SQL.replace("${stage}", STAGE)

def _pm_sql() -> str:
    return _PM_SQL.replace("${stage}", STAGE)

def _managed_vins_sql() -> str:
    return _MANAGED_VINS_SQL.replace("${stage}", STAGE)

def _daily_usage_sql() -> str:
    return _DAILY_USAGE_SQL.replace("${stage}", STAGE)

def _controls_sql() -> str:
    return (
        _CONTROLS_SQL
        .replace("${stage}", STAGE)
        .replace("${model}", "ModelX")
        .replace("${model_year}", "2020")
        .replace("${excluded_vins}", "'__no_exclusion__'")
        .replace("${cap}", "100")
    )

def _co_occurring_sql() -> str:
    vin_literal = _safe_vin_list([VIN_A])
    return (
        _CO_OCCURRING_SQL
        .replace("${stage}", STAGE)
        .replace("${vins}", vin_literal)
        .replace("${code_family}", "P0001")
    )

def _battery_managed_sql() -> str:
    src = AdpBatterySource(
        STAGE, athena_client=None, workgroup="test", results_s3_path="s3://test"
    )
    return src._build_select_managed_vins_sql(cursor=None, limit=100)

def _brake_managed_sql() -> str:
    src = AdpBrakeSource(
        STAGE, athena_client=None, workgroup="test", results_s3_path="s3://test"
    )
    return src._build_select_managed_vins_sql(cursor=None, limit=100)

def _tire_managed_sql() -> str:
    src = AdpTireSource(
        STAGE, athena_client=None, workgroup="test", results_s3_path="s3://test"
    )
    return src._build_select_managed_vins_sql(cursor=None, limit=100)


# ===========================================================================
# Read 1 — _FAULT_SQL (vehicle_fault_status_daily)
# ===========================================================================

def test_read1_fault_sql_newest_only() -> None:
    """Only newest-snapshot fault rows for VIN_A; VIN_B (old-only) absent."""
    con = _make_conn()
    rows = _q(con, _fault_sql())
    vins = {r[0] for r in rows}
    fleets = {r[2] for r in rows}
    assert vins == {VIN_A}, f"expected only {VIN_A}, got {vins}"
    assert VIN_B not in vins
    assert fleets == {"fleet_B"}, f"expected fleet_B (new), got {fleets}"


def test_read1_fault_sql_absent_today() -> None:
    """With only old snapshot, returns old rows for both VIN_A and VIN_B."""
    con = _make_conn(two_snapshots=False)
    rows = _q(con, _fault_sql())
    vins = {r[0] for r in rows}
    assert vins == {VIN_A, VIN_B}, f"expected both VINs from old snapshot, got {vins}"


def test_read1_fault_sql_mutant_le_fails() -> None:
    """Mutant `= MAX` → `<= MAX` returns rows from both snapshots (more rows)."""
    con = _make_conn()
    normal_rows = _q(con, _fault_sql())
    mutant_rows = _q(con, _mutant(_fault_sql()))
    assert len(mutant_rows) > len(normal_rows), (
        f"mutant should return more rows: normal={len(normal_rows)}, "
        f"mutant={len(mutant_rows)}"
    )
    # VIN_B must appear in the mutant (old snapshot)
    assert any(r[0] == VIN_B for r in mutant_rows)


# ===========================================================================
# Read 2 — _PM_SQL (pm_vehicle_status_daily)
# ===========================================================================

def test_read2_pm_sql_newest_only() -> None:
    """Only newest-snapshot PM row for VIN_A; VIN_B absent."""
    con = _make_conn()
    rows = _q(con, _pm_sql())
    vins = {r[0] for r in rows}
    fleets = {r[1] for r in rows}
    assert vins == {VIN_A}
    assert VIN_B not in vins
    assert fleets == {"fleet_B"}


def test_read2_pm_sql_absent_today() -> None:
    """Old-only snapshot returns both VINs."""
    con = _make_conn(two_snapshots=False)
    vins = {r[0] for r in _q(con, _pm_sql())}
    assert vins == {VIN_A, VIN_B}


def test_read2_pm_sql_mutant_le_fails() -> None:
    """Mutant returns rows from both snapshots (VIN_A twice, VIN_B once)."""
    con = _make_conn()
    normal = _q(con, _pm_sql())
    mutant = _q(con, _mutant(_pm_sql()))
    assert len(mutant) > len(normal)
    assert any(r[0] == VIN_B for r in mutant)


# ===========================================================================
# Read 3 — _MANAGED_VINS_SQL (managed_vehicles)
# ===========================================================================

def test_read3_managed_vins_newest_only() -> None:
    """Only the newest-snapshot VIN_A/fleet_B row; VIN_B absent."""
    con = _make_conn()
    rows = _q(con, _managed_vins_sql())
    vins = {r[0] for r in rows}
    fleets = {r[1] for r in rows}
    assert rows == [(VIN_A, "fleet_B")], f"exact row set wrong: {rows}"
    _ = vins, fleets  # suppress unused warnings


def test_read3_managed_vins_absent_today() -> None:
    """Old-only snapshot returns VIN_A/fleet_A and VIN_B/fleet_B_old."""
    con = _make_conn(two_snapshots=False)
    rows = sorted(_q(con, _managed_vins_sql()))
    assert {r[0] for r in rows} == {VIN_A, VIN_B}


def test_read3_managed_vins_mutant_le_fails() -> None:
    """Mutant returns 3 rows (VIN_A old, VIN_A new, VIN_B old); normal returns 1."""
    con = _make_conn()
    normal = _q(con, _managed_vins_sql())
    mutant = _q(con, _mutant(_managed_vins_sql()))
    assert len(mutant) > len(normal), (
        f"mutant={len(mutant)} rows, normal={len(normal)}"
    )
    assert any(r[0] == VIN_B for r in mutant)


# ===========================================================================
# Read 4 — _DAILY_USAGE_SQL (vehicle_utilisation_daily)
# ===========================================================================

def test_read4_daily_usage_newest_only() -> None:
    """Only VIN_A with avg_miles_30d=200.0 from the new snapshot."""
    con = _make_conn()
    rows = _q(con, _daily_usage_sql())
    assert len(rows) == 1
    assert rows[0][0] == VIN_A
    assert abs(float(rows[0][1]) - 200.0) < 0.001


def test_read4_daily_usage_absent_today() -> None:
    """Old-only snapshot returns both VINs (100.0 and 150.0)."""
    con = _make_conn(two_snapshots=False)
    vins = {r[0] for r in _q(con, _daily_usage_sql())}
    assert vins == {VIN_A, VIN_B}


def test_read4_daily_usage_mutant_le_fails() -> None:
    """Mutant returns VIN_A twice (avg 100.0 and 200.0) plus VIN_B."""
    con = _make_conn()
    normal = _q(con, _daily_usage_sql())
    mutant = _q(con, _mutant(_daily_usage_sql()))
    assert len(mutant) > len(normal)
    assert any(r[0] == VIN_B for r in mutant)


# ===========================================================================
# Read 5 — _CONTROLS_SQL (managed_vehicles subquery)
# ===========================================================================

def test_read5_controls_sql_newest_only() -> None:
    """Only VIN_A qualifies as a control (VIN_B not in newest managed snapshot)."""
    con = _make_conn()
    rows = _q(con, _controls_sql())
    vins = {r[0] for r in rows}
    assert vins == {VIN_A}, f"expected {{VIN_A}} controls, got {vins}"
    assert VIN_B not in vins


def test_read5_controls_sql_absent_today() -> None:
    """Old-only snapshot: both VIN_A and VIN_B are controls."""
    con = _make_conn(two_snapshots=False)
    vins = {r[0] for r in _q(con, _controls_sql())}
    assert vins == {VIN_A, VIN_B}


def test_read5_controls_sql_mutant_le_fails() -> None:
    """Mutant includes VIN_B (only in old managed snapshot); normal excludes it."""
    con = _make_conn()
    normal_vins = {r[0] for r in _q(con, _controls_sql())}
    mutant_vins = {r[0] for r in _q(con, _mutant(_controls_sql()))}
    assert VIN_B not in normal_vins
    assert VIN_B in mutant_vins, f"mutant must include VIN_B; got {mutant_vins}"


# ===========================================================================
# Read 6 — _CO_OCCURRING_SQL (vehicle_fault_status_daily)
# ===========================================================================

def test_read6_co_occurring_newest_only() -> None:
    """Only C0001 (new-snapshot code) returned; C0002 (old-only) absent."""
    con = _make_conn()
    rows = _q(con, _co_occurring_sql())
    codes = {r[0] for r in rows}
    assert codes == {"C0001"}, f"expected only C0001 (new snapshot), got {codes}"
    assert "C0002" not in codes


def test_read6_co_occurring_absent_today() -> None:
    """Old-only snapshot returns C0002 (VIN_A's old co-occurring code)."""
    con = _make_conn(two_snapshots=False)
    rows = _q(con, _co_occurring_sql())
    codes = {r[0] for r in rows}
    assert codes == {"C0002"}


def test_read6_co_occurring_mutant_le_fails() -> None:
    """Mutant returns both C0001 (new) and C0002 (old); normal returns only C0001."""
    con = _make_conn()
    normal_codes = {r[0] for r in _q(con, _co_occurring_sql())}
    mutant_codes = {r[0] for r in _q(con, _mutant(_co_occurring_sql()))}
    assert "C0001" in normal_codes
    assert "C0002" not in normal_codes
    assert "C0001" in mutant_codes
    assert "C0002" in mutant_codes, "mutant must include C0002 from old snapshot"


# ===========================================================================
# Read 7 — AdpBatterySource._build_select_managed_vins_sql
# ===========================================================================

def test_read7_battery_managed_newest_only() -> None:
    """Only VIN_A in newest managed snapshot; VIN_B (old-only) absent."""
    con = _make_conn()
    vins = {r[0] for r in _q(con, _battery_managed_sql())}
    assert vins == {VIN_A}, f"expected only VIN_A, got {vins}"
    assert VIN_B not in vins


def test_read7_battery_managed_absent_today() -> None:
    """Old-only managed snapshot returns both VINs."""
    con = _make_conn(two_snapshots=False)
    vins = {r[0] for r in _q(con, _battery_managed_sql())}
    assert vins == {VIN_A, VIN_B}


def test_read7_battery_managed_mutant_le_fails() -> None:
    """Mutant returns VIN_B (from old managed snapshot); normal does not."""
    con = _make_conn()
    normal = {r[0] for r in _q(con, _battery_managed_sql())}
    mutant = {r[0] for r in _q(con, _mutant(_battery_managed_sql()))}
    assert VIN_B not in normal
    assert VIN_B in mutant, f"mutant must include VIN_B; got {mutant}"


# ===========================================================================
# Read 8 — AdpBrakeSource._build_select_managed_vins_sql
# ===========================================================================

def test_read8_brake_managed_newest_only() -> None:
    """Only VIN_A from the newest managed snapshot (open brake jobs)."""
    con = _make_conn()
    vins = {r[0] for r in _q(con, _brake_managed_sql())}
    assert vins == {VIN_A}
    assert VIN_B not in vins


def test_read8_brake_managed_absent_today() -> None:
    """Old-only managed snapshot returns both VINs."""
    con = _make_conn(two_snapshots=False)
    vins = {r[0] for r in _q(con, _brake_managed_sql())}
    assert vins == {VIN_A, VIN_B}


def test_read8_brake_managed_mutant_le_fails() -> None:
    """Mutant includes VIN_B from old managed snapshot."""
    con = _make_conn()
    normal = {r[0] for r in _q(con, _brake_managed_sql())}
    mutant = {r[0] for r in _q(con, _mutant(_brake_managed_sql()))}
    assert VIN_B not in normal
    assert VIN_B in mutant


# ===========================================================================
# Read 9 — AdpTireSource._build_select_managed_vins_sql
# ===========================================================================

def test_read9_tire_managed_newest_only() -> None:
    """Only VIN_A from the newest managed snapshot (needs_replacement=True)."""
    con = _make_conn()
    vins = {r[0] for r in _q(con, _tire_managed_sql())}
    assert vins == {VIN_A}
    assert VIN_B not in vins


def test_read9_tire_managed_absent_today() -> None:
    """Old-only managed snapshot returns both VINs."""
    con = _make_conn(two_snapshots=False)
    vins = {r[0] for r in _q(con, _tire_managed_sql())}
    assert vins == {VIN_A, VIN_B}


def test_read9_tire_managed_mutant_le_fails() -> None:
    """Mutant includes VIN_B from old managed snapshot."""
    con = _make_conn()
    normal = {r[0] for r in _q(con, _tire_managed_sql())}
    mutant = {r[0] for r in _q(con, _mutant(_tire_managed_sql()))}
    assert VIN_B not in normal
    assert VIN_B in mutant
