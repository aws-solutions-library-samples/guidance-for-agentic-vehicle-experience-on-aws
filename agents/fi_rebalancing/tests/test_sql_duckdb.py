"""DuckDB-based behavioural SQL tests for fi_rebalancing Athena SQL files.

Spec: G4-D1. Tests assert exact row selection per snapshot/run/RO image business
rules and busy-window edge cases.

Every fixture table is created with producer_columns.create_producer_table so a
query naming a column absent from the real producer fails here, exactly as it
would in Athena (architect check, 2026-10-10).

Shims Athena/Presto → DuckDB:
  date_diff('unit', ...) → datediff('unit', ...)  (underscore vs none)

All other syntax (INTERVAL, CAST AS DATE/TIMESTAMP, ROW_NUMBER, CTE) is
compatible natively.
"""
from __future__ import annotations

import re
from pathlib import Path

import duckdb
import pytest

from agents.fi_rebalancing.tests.producer_columns import create_producer_table

_SQL_DIR = Path(__file__).parent.parent / "sql"
_STAGE = "test"
_FLEET = "demo-rental-carshare"

# ---------------------------------------------------------------------------
# Athena → DuckDB shim
# ---------------------------------------------------------------------------


def _shim(sql: str, *, stage: str = _STAGE, fleet_id: str = _FLEET,
          today: str = "", now: str = "") -> str:
    sql = sql.replace("${stage}", stage)
    sql = sql.replace("${fleet_id}", fleet_id)
    sql = sql.replace("${today}", today)
    sql = sql.replace("${now}", now)
    # date_diff('unit', a, b) → datediff('unit', a, b)
    sql = re.sub(r"\bdate_diff\s*\(", "datediff(", sql, flags=re.IGNORECASE)
    return sql


def _run(conn: duckdb.DuckDBPyConnection, sql: str) -> list[dict]:
    """Run SQL; convert date/datetime to ISO strings to match Athena string output."""
    import datetime as _dt
    result = conn.execute(sql)
    cols = [d[0] for d in result.description]
    rows = []
    for row in result.fetchall():
        d = {}
        for col, val in zip(cols, row):
            if isinstance(val, _dt.datetime):
                d[col] = val.strftime("%Y-%m-%d %H:%M:%S")
            elif isinstance(val, _dt.date):
                d[col] = val.isoformat()
            else:
                d[col] = val
        rows.append(d)
    return rows


# ---------------------------------------------------------------------------
# Fixtures: gaps (two snapshots)
# ---------------------------------------------------------------------------

def _gaps_conn() -> duckdb.DuckDBPyConnection:
    conn = duckdb.connect(":memory:")
    create_producer_table(conn, "supply_demand_gaps_snapshot")
    conn.execute("""
        INSERT INTO adp_test_fleet_rebalance.supply_demand_gaps_snapshot
          (location_id, horizon_days, forecast_hours, supply_vehicles, supply_hours,
           forecast_utilization_pct, status, vehicles_short, vehicles_spare,
           data_as_of, snapshot_date, materialised_on)
        VALUES
          -- snapshot 2026-10-05 (older) horizon=7
          ('loc-a', 7, 80.0, 3.0, 24.0, 0.85, 'deficit', 2, 0,
           DATE '2026-10-05', DATE '2026-10-05', DATE '2026-10-05'),
          ('loc-b', 7, 40.0, 5.0, 40.0, 0.40, 'surplus', 0, 2,
           DATE '2026-10-05', DATE '2026-10-05', DATE '2026-10-05'),
          -- snapshot 2026-10-06 (newest) horizon=7  <- must be selected
          ('loc-a', 7, 90.0, 2.0, 16.0, 0.95, 'deficit', 3, 0,
           DATE '2026-10-06', DATE '2026-10-06', DATE '2026-10-06'),
          ('loc-b', 7, 45.0, 6.0, 48.0, 0.38, 'surplus', 0, 3,
           DATE '2026-10-06', DATE '2026-10-06', DATE '2026-10-06'),
          -- snapshot 2026-10-06 horizon=14 (different horizon, must NOT be selected)
          ('loc-a', 14, 200.0, 2.0, 16.0, 0.99, 'deficit', 5, 0,
           DATE '2026-10-06', DATE '2026-10-06', DATE '2026-10-06')
    """)
    return conn


def test_gaps_newest_snapshot_only():
    conn = _gaps_conn()
    sql = _shim((_SQL_DIR / "select_gaps.sql").read_text())
    rows = _run(conn, sql)
    dates = {r["data_as_of"] for r in rows}
    assert dates == {"2026-10-06"}, f"expected only 2026-10-06 rows, got {dates}"
    assert len(rows) == 2  # loc-a and loc-b from newest snapshot


def test_gaps_horizon_7_only():
    conn = _gaps_conn()
    sql = _shim((_SQL_DIR / "select_gaps.sql").read_text())
    rows = _run(conn, sql)
    loc_ids = {r["location_id"] for r in rows}
    # horizon=14 loc-a must not appear (only horizon=7)
    assert loc_ids == {"loc-a", "loc-b"}


def test_gaps_correct_values():
    conn = _gaps_conn()
    sql = _shim((_SQL_DIR / "select_gaps.sql").read_text())
    rows = _run(conn, sql)
    loc_a = next(r for r in rows if r["location_id"] == "loc-a")
    assert loc_a["vehicles_short"] == 3  # from newest snapshot
    assert loc_a["forecast_hours"] == pytest.approx(90.0)
    assert loc_a["status"] == "deficit"


# ---------------------------------------------------------------------------
# Fixtures: forecast_days (two runs)
# ---------------------------------------------------------------------------

def _forecast_conn() -> duckdb.DuckDBPyConnection:
    conn = duckdb.connect(":memory:")
    create_producer_table(conn, "fleet_bookings_forecast")
    conn.execute("""
        INSERT INTO adp_test_fleet_rebalance.fleet_bookings_forecast
          (location_id, forecast_date, run_date, forecast_hours, method,
           baseline_hours, pickup_hours, event_uplift_factor, mape_28d, data_as_of)
        VALUES
          -- run 2026-10-05 (older) — days +1..+9 = 2026-10-06..2026-10-14
          ('loc-a', DATE '2026-10-06', DATE '2026-10-05', 70.0, 'ml', 0.0, 0.0, 1.0, 0.12,
           DATE '2026-10-05'),
          ('loc-a', DATE '2026-10-07', DATE '2026-10-05', 75.0, 'ml', 0.0, 0.0, 1.0, 0.12,
           DATE '2026-10-05'),
          -- run 2026-10-06 (latest) — days +1..+9 = 2026-10-07..2026-10-15  <- must be selected
          ('loc-a', DATE '2026-10-07', DATE '2026-10-06', 80.0, 'ml', 0.0, 0.0, 1.0, 0.10,
           DATE '2026-10-06'),
          ('loc-a', DATE '2026-10-08', DATE '2026-10-06', 82.0, 'ml', 0.0, 0.0, 1.0, 0.10,
           DATE '2026-10-06'),
          ('loc-a', DATE '2026-10-15', DATE '2026-10-06', 85.0, 'ml', 0.0, 0.0, 1.0, 0.10,
           DATE '2026-10-06'),
          -- run_date itself (2026-10-06) must NOT appear (forecast_date > run_date)
          ('loc-a', DATE '2026-10-06', DATE '2026-10-06', 50.0, 'ml', 0.0, 0.0, 1.0, 0.10,
           DATE '2026-10-06'),
          -- day +10 beyond latest run (2026-10-16) must NOT appear
          ('loc-a', DATE '2026-10-16', DATE '2026-10-06', 88.0, 'ml', 0.0, 0.0, 1.0, 0.10,
           DATE '2026-10-06')
    """)
    return conn


def test_forecast_latest_run_only():
    conn = _forecast_conn()
    sql = _shim((_SQL_DIR / "select_forecast_days.sql").read_text())
    rows = _run(conn, sql)
    run_dates = {r["run_date"] for r in rows}
    assert run_dates == {"2026-10-06"}, f"expected only latest run, got {run_dates}"


def test_forecast_excludes_run_date_itself():
    conn = _forecast_conn()
    sql = _shim((_SQL_DIR / "select_forecast_days.sql").read_text())
    rows = _run(conn, sql)
    forecast_dates = {r["forecast_date"] for r in rows}
    assert "2026-10-06" not in forecast_dates, "run_date itself must be excluded (forecast_date > run_date)"


def test_forecast_excludes_day_10_plus():
    conn = _forecast_conn()
    sql = _shim((_SQL_DIR / "select_forecast_days.sql").read_text())
    rows = _run(conn, sql)
    forecast_dates = sorted(r["forecast_date"] for r in rows)
    assert "2026-10-16" not in forecast_dates, "+10 day must be excluded (run_date+9 ceiling)"
    # day 9 (2026-10-15) IS included
    assert "2026-10-15" in forecast_dates


# ---------------------------------------------------------------------------
# Fixtures: vehicle_days (latest snapshot)
# ---------------------------------------------------------------------------

def _vd_conn() -> duckdb.DuckDBPyConnection:
    conn = duckdb.connect(":memory:")
    create_producer_table(conn, "vehicle_days")
    conn.execute("""
        INSERT INTO adp_test_fleet_rebalance.vehicle_days
          (vin, fleet_id, location_id, snapshot_date, op_open_h, available_hours)
        VALUES
          -- older snapshot 2026-10-05: VIN-1 and VIN-2
          ('VIN-1', 'demo-rental-carshare', 'loc-a', DATE '2026-10-05', 8, 8.0),
          ('VIN-2', 'demo-rental-carshare', 'loc-b', DATE '2026-10-05', 8, 8.0),
          -- newest snapshot 2026-10-06: VIN-1 and VIN-3  <- must be selected
          ('VIN-1', 'demo-rental-carshare', 'loc-a', DATE '2026-10-06', 8, 8.0),
          ('VIN-3', 'demo-rental-carshare', 'loc-c', DATE '2026-10-06', 8, 6.0),
          -- different fleet, newest date: must NOT appear
          ('VIN-X', 'other-fleet', 'loc-x', DATE '2026-10-07', 8, 8.0)
    """)
    return conn


def test_vehicle_days_latest_snapshot_only():
    conn = _vd_conn()
    sql = _shim((_SQL_DIR / "select_vehicle_days.sql").read_text())
    rows = _run(conn, sql)
    vins = {r["vin"] for r in rows}
    assert vins == {"VIN-1", "VIN-3"}, f"expected only latest snapshot VINs, got {vins}"
    assert len(rows) == 2


def test_vehicle_days_correct_values():
    conn = _vd_conn()
    sql = _shim((_SQL_DIR / "select_vehicle_days.sql").read_text())
    rows = _run(conn, sql)
    vin3 = next(r for r in rows if r["vin"] == "VIN-3")
    assert vin3["location_id"] == "loc-c"
    assert vin3["available_hours"] == pytest.approx(6.0)
    # W7a: snapshot_date is now selected
    assert vin3["snapshot_date"] == "2026-10-06"


# ---------------------------------------------------------------------------
# Fixtures: vehicle_utilization (7-day window edges)
# ---------------------------------------------------------------------------

def _util_conn() -> duckdb.DuckDBPyConnection:
    conn = duckdb.connect(":memory:")
    create_producer_table(conn, "vehicle_days")
    create_producer_table(conn, "vehicle_utilization_daily")
    # anchor: latest vehicle_days snapshot_date = 2026-10-06
    conn.execute("""
        INSERT INTO adp_test_fleet_rebalance.vehicle_days
          (vin, fleet_id, location_id, snapshot_date, op_open_h, available_hours)
        VALUES ('VIN-1', 'demo-rental-carshare', 'loc-a', DATE '2026-10-06', 8, 8.0)
    """)
    # utilization rows spanning the boundary
    conn.execute("""
        INSERT INTO adp_test_fleet_rebalance.vehicle_utilization_daily
          (vin, location_id, snapshot_date, on_rent_hours, available_hours, utilization_pct)
        VALUES
          -- exactly 7 days before latest (2026-09-29 = 2026-10-06 - 7): EXCLUDED (strict >)
          ('VIN-1', 'loc-a', DATE '2026-09-29', 4.0, 8.0, 0.5),
          -- 6 days before latest (2026-09-30): INCLUDED
          ('VIN-1', 'loc-a', DATE '2026-09-30', 4.0, 8.0, 0.5),
          -- day before latest (2026-10-05): INCLUDED
          ('VIN-1', 'loc-a', DATE '2026-10-05', 6.0, 8.0, 0.75),
          -- latest day itself (2026-10-06): INCLUDED (<= latest)
          ('VIN-1', 'loc-a', DATE '2026-10-06', 8.0, 8.0, 1.0),
          -- one day after latest (2026-10-07): EXCLUDED
          ('VIN-1', 'loc-a', DATE '2026-10-07', 8.0, 8.0, 1.0)
    """)
    return conn


def test_vehicle_utilization_7day_window_edges():
    conn = _util_conn()
    sql = _shim((_SQL_DIR / "select_vehicle_utilization.sql").read_text())
    rows = _run(conn, sql)
    dates = {r["snapshot_date"] for r in rows}
    assert "2026-09-29" not in dates, "exact 7-day boundary must be excluded (strict >)"
    assert "2026-09-30" in dates, "first day inside window must be included"
    assert "2026-10-05" in dates, "day before latest must be included"
    assert "2026-10-06" in dates, "latest day must be included (<= boundary)"
    assert "2026-10-07" not in dates, "day after latest must be excluded"
    assert len(rows) == 3  # 2026-09-30, 2026-10-05, 2026-10-06


# ---------------------------------------------------------------------------
# Fixtures: open_ros (two images per ro_id)
# ---------------------------------------------------------------------------

def _ros_conn() -> duckdb.DuckDBPyConnection:
    conn = duckdb.connect(":memory:")
    create_producer_table(conn, "repair_orders")
    conn.execute("""
        INSERT INTO dms_test.repair_orders (ro_id, vehicle_vin, status, _seq_no)
        VALUES
          -- _seq_no as DMS writes it: a string zero-padded to 40 digits, so 9 sorts before 10.
          -- ro-001: older image closed (Ready), newer image open (InProgress) -> must appear
          ('ro-001', 'VIN-A', 'Ready',         lpad('9', 40, '0')),
          ('ro-001', 'VIN-A', 'InProgress',    lpad('10', 40, '0')),
          -- ro-002: older image open, newer image closed (Delivered) -> must NOT appear
          ('ro-002', 'VIN-B', 'InProgress',    lpad('9', 40, '0')),
          ('ro-002', 'VIN-B', 'Delivered',     lpad('10', 40, '0')),
          -- ro-003: only one image, open -> must appear
          ('ro-003', 'VIN-C', 'AwaitingParts', lpad('1', 40, '0')),
          -- ro-004: only one image, closed (Invoiced) -> must NOT appear
          ('ro-004', 'VIN-D', 'Invoiced',      lpad('1', 40, '0')),
          -- ro-005: open, then DMS's administrative Closed -> must NOT appear (ADP FI sweep
          -- 2026-10-26, L96: the car is out of the shop, as ADP's supply_demand_gaps counts it)
          ('ro-005', 'VIN-E', 'InProgress',    lpad('9', 40, '0')),
          ('ro-005', 'VIN-E', 'Closed',        lpad('10', 40, '0')),
          -- ro-006: one Ready image -> must NOT appear
          ('ro-006', 'VIN-F', 'Ready',         lpad('1', 40, '0'))
    """)
    return conn


def test_ros_latest_image_by_seq_no():
    conn = _ros_conn()
    sql = _shim((_SQL_DIR / "select_open_ros.sql").read_text())
    rows = _run(conn, sql)
    ro_ids = {r["ro_id"] for r in rows}
    assert "ro-001" in ro_ids, "ro-001 latest image (In Progress) must appear"
    assert "ro-002" not in ro_ids, "ro-002 latest image (Delivered) must be excluded"
    assert "ro-003" in ro_ids, "ro-003 single open image must appear"
    assert "ro-004" not in ro_ids, "ro-004 Invoiced must be excluded"
    assert "ro-005" not in ro_ids, "ro-005 latest image Closed must be excluded"
    assert "ro-006" not in ro_ids, "ro-006 Ready must be excluded"
    assert ro_ids == {"ro-001", "ro-003"} and len(rows) == 2, ro_ids


def test_ros_closed_statuses_excluded():
    conn = _ros_conn()
    sql = _shim((_SQL_DIR / "select_open_ros.sql").read_text())
    rows = _run(conn, sql)
    for r in rows:
        assert r["status"] not in ("Ready", "Delivered", "Invoiced", "Closed"), (
            f"closed status {r['status']} leaked through for ro_id={r['ro_id']}"
        )


# ---------------------------------------------------------------------------
# Fixtures: outcomes (newest materialised_on)
# ---------------------------------------------------------------------------

def _outcomes_conn() -> duckdb.DuckDBPyConnection:
    conn = duckdb.connect(":memory:")
    create_producer_table(conn, "move_outcomes_snapshot")
    conn.execute("""
        INSERT INTO adp_test_fleet_rebalance.move_outcomes_snapshot
          (move_id, fleet_id, from_location_id, to_location_id, vins, effective_date,
           review_available_on, predicted_on_rent_hours_gain, predicted_turnaways_avoided,
           actual_on_rent_hours_gain, actual_turnaways_at_destination,
           source_actual_turnaways, ratio_hours, method, snapshot_date, materialised_on)
        VALUES
          -- older materialised_on 2026-10-05
          ('move-1', 'demo-rental-carshare', 'loc-a', 'loc-b', ['VIN-1'],
           DATE '2026-10-01', DATE '2026-10-08',
           10.0, 2, 9.0, 0, 0, 0.9, 'rules',
           DATE '2026-10-05', DATE '2026-10-05'),
          -- newest materialised_on 2026-10-06  <- must be selected
          ('move-2', 'demo-rental-carshare', 'loc-a', 'loc-c', ['VIN-2'],
           DATE '2026-10-02', DATE '2026-10-09',
           12.0, 3, 11.0, 0, 0, 1.1, 'rules',
           DATE '2026-10-06', DATE '2026-10-06'),
          -- different fleet with even newer date: must NOT be selected
          ('move-3', 'other-fleet', 'loc-x', 'loc-y', ['VIN-X'],
           DATE '2026-10-02', DATE '2026-10-09',
           15.0, 4, 14.0, 0, 0, 1.0, 'rules',
           DATE '2026-10-07', DATE '2026-10-07')
    """)
    return conn


def test_outcomes_newest_materialised_on():
    conn = _outcomes_conn()
    sql = _shim((_SQL_DIR / "select_outcomes.sql").read_text())
    rows = _run(conn, sql)
    assert len(rows) == 1, f"expected 1 row from newest materialised_on, got {len(rows)}"
    assert rows[0]["to_location_id"] == "loc-c"
    assert rows[0]["ratio_hours"] == pytest.approx(1.1)
    assert rows[0]["predicted_on_rent_hours_gain"] == pytest.approx(12.0)
    # W7b: materialised_on is now selected
    assert rows[0]["materialised_on"] == "2026-10-06"


# ---------------------------------------------------------------------------
# Fixtures: events (window overlap and announced_on filter)
# ---------------------------------------------------------------------------

_EVENTS_TODAY = "2026-10-06"


def _events_conn() -> duckdb.DuckDBPyConnection:
    conn = duckdb.connect(":memory:")
    create_producer_table(conn, "metro_events")
    conn.execute("""
        INSERT INTO adp_test_fleet_rebalance.metro_events
          (event_id, metro, affected_location_ids, starts_on, ends_on,
           category, announced_on, uplift_band)
        VALUES
          -- valid in-window event, announced before today -> INCLUDED
          ('ev-1', 'seattle', ['loc-a'], DATE '2026-10-06', DATE '2026-10-08',
           'concert', DATE '2026-10-05', 'high'),
          -- event ended before today -> EXCLUDED (ends_on < today)
          ('ev-2', 'seattle', ['loc-a'], DATE '2026-10-01', DATE '2026-10-05',
           'conference', DATE '2026-10-01', 'medium'),
          -- starts after today+9 -> EXCLUDED
          ('ev-3', 'seattle', ['loc-a'], DATE '2026-10-16', DATE '2026-10-20',
           'festival', DATE '2026-10-04', 'low'),
          -- announced_on > today -> EXCLUDED
          ('ev-4', 'seattle', ['loc-a'], DATE '2026-10-07', DATE '2026-10-09',
           'show', DATE '2026-10-07', 'medium'),
          -- starts_on = today+9 (boundary <= today+9) -> INCLUDED
          ('ev-5', 'portland', ['loc-b'], DATE '2026-10-15', DATE '2026-10-17',
           'expo', DATE '2026-10-04', 'low'),
          -- ends_on = today (boundary >= today) -> INCLUDED
          ('ev-6', 'seattle', ['loc-c'], DATE '2026-10-04', DATE '2026-10-06',
           'meet', DATE '2026-10-05', 'medium')
    """)
    return conn


def test_events_window_and_announced_on():
    conn = _events_conn()
    sql = _shim((_SQL_DIR / "select_events.sql").read_text(), today=_EVENTS_TODAY)
    rows = _run(conn, sql)
    ids = {r["event_id"] for r in rows}
    assert "ev-1" in ids, "valid in-window event must be included"
    assert "ev-2" not in ids, "event ending before today must be excluded"
    assert "ev-3" not in ids, "event starting after today+9 must be excluded"
    assert "ev-4" not in ids, "event announced in future must be excluded"
    assert "ev-5" in ids, "starts_on = today+9 boundary must be included"
    assert "ev-6" in ids, "ends_on = today boundary must be included (>= today)"
    assert len(rows) == 3, f"expected 3 rows (ev-1, ev-5, ev-6), got {ids}"


def test_events_correct_columns():
    """Verify the select returns exactly the expected columns (including 'announced_on')."""
    conn = _events_conn()
    sql = _shim((_SQL_DIR / "select_events.sql").read_text(), today=_EVENTS_TODAY)
    rows = _run(conn, sql)
    assert len(rows) > 0
    expected_cols = {"event_id", "metro", "affected_location_ids",
                     "category", "uplift_band", "starts_on", "ends_on", "announced_on"}
    assert set(rows[0].keys()) == expected_cols, (
        f"unexpected columns: {set(rows[0].keys())}"
    )


# ---------------------------------------------------------------------------
# Fixtures: service_soon (window edges)
# ---------------------------------------------------------------------------

_SERVICE_TODAY = "2026-10-06"


def _service_conn() -> duckdb.DuckDBPyConnection:
    conn = duckdb.connect(":memory:")
    create_producer_table(conn, "service_records")
    conn.execute("""
        INSERT INTO adp_test_service_records.service_records
          (service_id, service_date, vin, service_type, outcome, service_month)
        VALUES
          -- one day before today (2026-10-05) -> EXCLUDED (< today)
          ('sr-1', DATE '2026-10-05', 'VIN-A', 'oil',        'completed', DATE '2026-10-01'),
          -- today (2026-10-06) -> INCLUDED (boundary >= today)
          ('sr-2', DATE '2026-10-06', 'VIN-B', 'tires',      'scheduled', DATE '2026-10-01'),
          -- today+2 (2026-10-08) -> INCLUDED
          ('sr-3', DATE '2026-10-08', 'VIN-C', 'brakes',     'scheduled', DATE '2026-10-01'),
          -- today+3 (2026-10-09) -> INCLUDED (boundary <= today+3)
          ('sr-4', DATE '2026-10-09', 'VIN-D', 'inspection', 'scheduled', DATE '2026-10-01'),
          -- today+4 (2026-10-10) -> EXCLUDED (> today+3)
          ('sr-5', DATE '2026-10-10', 'VIN-E', 'battery',    'scheduled', DATE '2026-10-01'),
          -- wrong service_month (partition mismatch) -> EXCLUDED by W2 filter
          ('sr-6', DATE '2026-10-08', 'VIN-F', 'tires',      'scheduled', DATE '2026-09-01')
    """)
    return conn


def test_service_soon_window_edges():
    conn = _service_conn()
    sql = _shim((_SQL_DIR / "select_service_soon.sql").read_text(), today=_SERVICE_TODAY)
    rows = _run(conn, sql)
    vins = {r["vin"] for r in rows}
    assert "VIN-A" not in vins, "service one day before today must be excluded"
    assert "VIN-B" in vins, "service on today must be included (>= boundary)"
    assert "VIN-C" in vins, "service today+2 must be included"
    assert "VIN-D" in vins, "service on today+3 must be included (<= boundary)"
    assert "VIN-E" not in vins, "service today+4 must be excluded"
    assert len(rows) == 3, f"expected 3 rows (VIN-B, VIN-C, VIN-D), got {vins}"


def test_service_soon_partition_filter():
    """W2: a row with the correct service_date but a wrong service_month is excluded."""
    conn = _service_conn()
    sql = _shim((_SQL_DIR / "select_service_soon.sql").read_text(), today=_SERVICE_TODAY)
    rows = _run(conn, sql)
    vins = {r["vin"] for r in rows}
    assert "VIN-F" not in vins, "wrong service_month (2026-09-01) must be excluded by partition filter"


# ---------------------------------------------------------------------------
# Fixtures: busy_windows (edge cases)
# ---------------------------------------------------------------------------

_NOW_STR = "2026-10-06 12:00:00"


def _busy_conn() -> duckdb.DuckDBPyConnection:
    conn = duckdb.connect(":memory:")
    create_producer_table(conn, "booking_requests")
    conn.execute("""
        INSERT INTO adp_test_fleet_rebalance.booking_requests
          (fleet_id, vin, location_id, outcome, start_at, end_at)
        VALUES
          -- on rent now: start before now, end after now -> included
          ('demo-rental-carshare', 'VIN-1', 'loc-a', 'fulfilled',
           TIMESTAMP '2026-10-06 10:00:00', TIMESTAMP '2026-10-06 14:00:00'),
          -- future booking starts in 6h -> included
          ('demo-rental-carshare', 'VIN-2', 'loc-a', 'fulfilled',
           TIMESTAMP '2026-10-06 18:00:00', TIMESTAMP '2026-10-07 08:00:00'),
          -- start_at exactly = now+24h (not < now+24h) -> excluded
          ('demo-rental-carshare', 'VIN-3', 'loc-a', 'fulfilled',
           TIMESTAMP '2026-10-07 12:00:00', TIMESTAMP '2026-10-07 20:00:00'),
          -- end_at exactly = now (not > now) -> excluded
          ('demo-rental-carshare', 'VIN-4', 'loc-a', 'fulfilled',
           TIMESTAMP '2026-10-05 08:00:00', TIMESTAMP '2026-10-06 12:00:00'),
          -- start_at = now-35d (boundary, within 35-day prune window) -> included if end_at > now
          ('demo-rental-carshare', 'VIN-5', 'loc-a', 'fulfilled',
           TIMESTAMP '2026-09-01 12:00:00', TIMESTAMP '2026-10-07 00:00:00'),
          -- start_at just before now-35d (outside prune window) -> excluded
          ('demo-rental-carshare', 'VIN-6', 'loc-a', 'fulfilled',
           TIMESTAMP '2026-08-31 11:59:59', TIMESTAMP '2026-10-07 00:00:00'),
          -- outcome = 'cancelled' -> excluded
          ('demo-rental-carshare', 'VIN-7', 'loc-a', 'cancelled',
           TIMESTAMP '2026-10-06 09:00:00', TIMESTAMP '2026-10-06 15:00:00'),
          -- vin IS NULL -> excluded
          ('demo-rental-carshare', NULL, 'loc-a', 'fulfilled',
           TIMESTAMP '2026-10-06 09:00:00', TIMESTAMP '2026-10-06 15:00:00')
    """)
    return conn


def test_busy_on_rent_now_included():
    conn = _busy_conn()
    sql = _shim((_SQL_DIR / "select_busy_windows.sql").read_text(), now=_NOW_STR)
    rows = _run(conn, sql)
    vins = {r["vin"] for r in rows}
    assert "VIN-1" in vins, "on-rent-now row must be included"


def test_busy_future_within_24h_included():
    conn = _busy_conn()
    sql = _shim((_SQL_DIR / "select_busy_windows.sql").read_text(), now=_NOW_STR)
    rows = _run(conn, sql)
    vins = {r["vin"] for r in rows}
    assert "VIN-2" in vins, "booking starting in 6h must be included"


def test_busy_exactly_24h_ahead_excluded():
    conn = _busy_conn()
    sql = _shim((_SQL_DIR / "select_busy_windows.sql").read_text(), now=_NOW_STR)
    rows = _run(conn, sql)
    vins = {r["vin"] for r in rows}
    assert "VIN-3" not in vins, "start_at exactly = now+24h must be excluded (strict <)"


def test_busy_end_at_exactly_now_excluded():
    conn = _busy_conn()
    sql = _shim((_SQL_DIR / "select_busy_windows.sql").read_text(), now=_NOW_STR)
    rows = _run(conn, sql)
    vins = {r["vin"] for r in rows}
    assert "VIN-4" not in vins, "end_at exactly = now must be excluded (strict >)"


def test_busy_prune_boundary_included():
    conn = _busy_conn()
    sql = _shim((_SQL_DIR / "select_busy_windows.sql").read_text(), now=_NOW_STR)
    rows = _run(conn, sql)
    vins = {r["vin"] for r in rows}
    # VIN-5: start_at = 2026-09-01 = now - 35 days exactly; end_at > now -> included
    assert "VIN-5" in vins, "start_at at 35-day boundary must be included"


def test_busy_outside_prune_window_excluded():
    conn = _busy_conn()
    sql = _shim((_SQL_DIR / "select_busy_windows.sql").read_text(), now=_NOW_STR)
    rows = _run(conn, sql)
    vins = {r["vin"] for r in rows}
    assert "VIN-6" not in vins, "start_at before 35-day boundary must be excluded"


def test_busy_non_fulfilled_excluded():
    conn = _busy_conn()
    sql = _shim((_SQL_DIR / "select_busy_windows.sql").read_text(), now=_NOW_STR)
    rows = _run(conn, sql)
    vins = {r["vin"] for r in rows}
    assert "VIN-7" not in vins, "non-fulfilled outcome must be excluded"


def test_busy_null_vin_excluded():
    conn = _busy_conn()
    sql = _shim((_SQL_DIR / "select_busy_windows.sql").read_text(), now=_NOW_STR)
    rows = _run(conn, sql)
    for r in rows:
        assert r["vin"] is not None, "NULL vin must be excluded"


# ---------------------------------------------------------------------------
# request_lengths SQL: date_diff shim
# ---------------------------------------------------------------------------

def _req_lengths_conn() -> duckdb.DuckDBPyConnection:
    conn = duckdb.connect(":memory:")
    create_producer_table(conn, "booking_requests")
    conn.execute("""
        INSERT INTO adp_test_fleet_rebalance.booking_requests
          (fleet_id, location_id, vin, outcome, start_at, end_at)
        VALUES
          -- 2h booking within window -> contributes
          ('demo-rental-carshare', 'loc-a', 'VIN-A', 'fulfilled',
           TIMESTAMP '2026-10-03 10:00:00', TIMESTAMP '2026-10-03 12:00:00'),
          -- 4h booking within window -> contributes (mean = (2+4)/2 = 3h)
          ('demo-rental-carshare', 'loc-a', 'VIN-B', 'fulfilled',
           TIMESTAMP '2026-10-04 08:00:00', TIMESTAMP '2026-10-04 12:00:00'),
          -- pending -> excluded
          ('demo-rental-carshare', 'loc-a', 'VIN-C', 'pending',
           TIMESTAMP '2026-10-05 08:00:00', TIMESTAMP '2026-10-05 10:00:00'),
          -- start_at outside 28-day window (too old) -> excluded
          ('demo-rental-carshare', 'loc-a', 'VIN-D', 'fulfilled',
           TIMESTAMP '2026-08-01 08:00:00', TIMESTAMP '2026-08-01 10:00:00')
    """)
    return conn


def test_request_lengths_mean_and_excludes_pending():
    conn = _req_lengths_conn()
    sql = _shim((_SQL_DIR / "select_request_lengths.sql").read_text(), now=_NOW_STR)
    rows = _run(conn, sql)
    assert len(rows) == 1
    assert rows[0]["location_id"] == "loc-a"
    assert rows[0]["mean_request_hours"] == pytest.approx(3.0, rel=1e-4), (
        "mean of 2h and 4h bookings must be 3.0"
    )
