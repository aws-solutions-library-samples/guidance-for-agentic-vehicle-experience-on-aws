"""Reader-contract tests for fi_rebalancing.sources (F1 fixes).

Each test:
1. Creates a DuckDB table from producer_columns.create_producer_table (exactly the
   producer's columns, so a SQL that names a non-existent column fails here).
2. Inserts representative rows including NULL for every nullable column F1 names.
3. Runs the real SQL file through DuckDB to produce native Python typed rows.
4. Converts each cell to the string Athena returns for its declared type:
      DOUBLE  → "12.0"   BIGINT → "3"    DATE → "2026-10-12"
      TIMESTAMP → "2026-10-12 08:00:00.000"   array → "[a, b]"   NULL → None
5. Feeds those rows through the real reader by patching run_query to return them.
6. Asserts that every parsed value matches expectations — type AND value.

Catches:
  C1  announced_on required in read_events
  C2a supply_vehicles "12.0" parsed by _fi
  C2b forecast_utilization_pct nullable
  C2c ratio_hours / predicted_on_rent_hours_gain / actual_on_rent_hours_gain nullable
  C3  timestamps with .fff / .ffffff (fromisoformat)
  W1  affected_location_ids parsed from "[loc-a, loc-b]" to a list
  W2  service_month partition filter (via SQL test)
  W7a snapshot_date returned by read_vehicle_days
  W7b materialised_on returned by read_outcomes
  W8  read_open_items returns all live items, not just p0/attention (see test_sources.py)

Mutant coverage:
  M1 (re-anchored): _fi → _i — caught by test_read_gaps_athena_supply_vehicles_double
  M7 (re-anchored): set(affected_location_ids) → set() — caught by
      test_event_scoped_by_location_id_not_metro
"""
from __future__ import annotations

import datetime as _dt
import math
from decimal import Decimal
from pathlib import Path
from typing import Any
from unittest.mock import patch

import duckdb
import pytest

from agents.fi_rebalancing.sources import (
    SourceReadError,
    read_busy_windows,
    read_events,
    read_forecast_days,
    read_gaps,
    read_open_ros,
    read_outcomes,
    read_request_lengths,
    read_service_soon,
    read_vehicle_days,
    read_vehicle_utilization,
)
from agents.fi_rebalancing.tests.producer_columns import create_producer_table

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_SQL_DIR = Path(__file__).parent.parent / "sql"
_STAGE = "test"
_FLEET = "demo-rental-carshare"
_TODAY = "2026-10-06"
_NOW = "2026-10-06 12:00:00"
_ATHENA_MODULE = "agents.fi_rebalancing.sources.run_query"


# ---------------------------------------------------------------------------
# DuckDB helpers
# ---------------------------------------------------------------------------

def _shim(sql: str, *, stage: str = _STAGE, fleet_id: str = _FLEET,
          today: str = "", now: str = "") -> str:
    import re
    sql = sql.replace("${stage}", stage).replace("${fleet_id}", fleet_id)
    sql = sql.replace("${today}", today).replace("${now}", now)
    sql = re.sub(r"\bdate_diff\s*\(", "datediff(", sql, flags=re.IGNORECASE)
    return sql


def _run(conn: duckdb.DuckDBPyConnection, sql: str) -> list[dict]:
    """Run SQL and return native Python-typed rows."""
    result = conn.execute(sql)
    cols = [d[0] for d in result.description]
    return [dict(zip(cols, row)) for row in result.fetchall()]


def _athena_double(f: float) -> str:
    """DOUBLE as Athena renders it (Java ``Double.toString``).

    Checked against live Athena (cvx-staging-analytics, us-east-1, query
    f54fee95-54c0-4cf5-89b9-9dffce20dca2, 2026-10-12): NaN -> "NaN", infinity() -> "Infinity",
    -infinity() -> "-Infinity", 12000000 -> "1.2E7", 0.0001 -> "1.0E-4", 12 -> "12.0".
    """
    if math.isnan(f):
        return "NaN"
    if math.isinf(f):
        return "Infinity" if f > 0 else "-Infinity"
    if f == 0 or 1e-3 <= abs(f) < 1e7:
        return f"{f:.1f}" if f == int(f) else repr(f)
    sign, digits, exp = Decimal(repr(f)).normalize().as_tuple()
    e = len(digits) - 1 + exp
    mant = f"{digits[0]}." + ("".join(str(d) for d in digits[1:]) or "0")
    return ("-" if sign else "") + mant + f"E{e}"


def _to_athena(val: Any, typ: str) -> str | None:
    """Convert a DuckDB native value to the string Athena returns for ``typ``."""
    if val is None:
        return None
    if typ == "DOUBLE":
        return _athena_double(float(val))
    if typ == "BIGINT" or typ == "INTEGER":
        return str(int(val))
    if typ == "DATE":
        if isinstance(val, _dt.date):
            return val.isoformat()
        return str(val)
    if typ == "TIMESTAMP":
        # Athena engine v3 renders TIMESTAMP at millisecond precision.
        if isinstance(val, _dt.datetime):
            ms = val.microsecond // 1000
            return val.strftime("%Y-%m-%d %H:%M:%S.") + f"{ms:03d}"
        return str(val)
    if typ.endswith("[]"):
        # VARCHAR[] → "[a, b]" (Athena array string form)
        if isinstance(val, list):
            if not val:
                return "[]"
            return "[" + ", ".join(str(v) for v in val) + "]"
        return str(val)
    # VARCHAR
    return str(val)


def _rows_to_athena(rows: list[dict],
                    type_map: dict[str, str]) -> list[dict[str, str | None]]:
    """Convert native DuckDB row dicts to Athena string-form dicts."""
    return [
        {col: _to_athena(val, type_map.get(col, "VARCHAR")) for col, val in row.items()}
        for row in rows
    ]


# ---------------------------------------------------------------------------
# C2a / C2b  read_gaps: supply_vehicles DOUBLE "12.0" and nullable forecast_utilization_pct
# ---------------------------------------------------------------------------

_GAPS_TYPE = {
    "location_id": "VARCHAR", "forecast_hours": "DOUBLE",
    "supply_vehicles": "DOUBLE",           # producer declares DOUBLE → "12.0"
    "forecast_utilization_pct": "DOUBLE",  # nullable: zero-supply case → NULL
    "status": "VARCHAR", "vehicles_short": "BIGINT",
    "vehicles_spare": "BIGINT", "data_as_of": "DATE",
}


def _gaps_conn_for_contract() -> duckdb.DuckDBPyConnection:
    conn = duckdb.connect(":memory:")
    create_producer_table(conn, "supply_demand_gaps_snapshot")
    conn.execute("""
        INSERT INTO adp_test_fleet_rebalance.supply_demand_gaps_snapshot
          (location_id, horizon_days, forecast_hours, supply_vehicles, supply_hours,
           forecast_utilization_pct, status, vehicles_short, vehicles_spare,
           data_as_of, snapshot_date, materialised_on)
        VALUES
          -- Normal deficit row: supply_vehicles is a whole-number DOUBLE ("12.0")
          ('loc-a', 7, 90.0, 12.0, 16.0, 0.95, 'deficit', 3, 0,
           DATE '2026-10-06', DATE '2026-10-06', DATE '2026-10-06'),
          -- Zero-supply row: supply_hours=0 → forecast_utilization_pct is NULL
          ('loc-b', 7, 45.0, 0.0, 0.0, NULL, 'deficit', 2, 0,
           DATE '2026-10-06', DATE '2026-10-06', DATE '2026-10-06')
    """)
    return conn


def test_read_gaps_athena_supply_vehicles_double():
    """C2a: supply_vehicles arrives as '12.0' (DOUBLE) and must parse to int 12."""
    conn = _gaps_conn_for_contract()
    sql = _shim((_SQL_DIR / "select_gaps.sql").read_text())
    raw = _run(conn, sql)
    athena_rows = _rows_to_athena(raw, _GAPS_TYPE)
    loc_a = next(r for r in athena_rows if r["location_id"] == "loc-a")
    assert loc_a["supply_vehicles"] == "12.0", "DuckDB→Athena must produce '12.0'"

    with patch(_ATHENA_MODULE, return_value=athena_rows):
        result = read_gaps(stage=_STAGE, fleet_id=_FLEET,
                           athena_client=None, workgroup="", results_s3_path="")

    loc_a_r = next(r for r in result if r["location_id"] == "loc-a")
    assert loc_a_r["supply_vehicles"] == 12
    assert isinstance(loc_a_r["supply_vehicles"], int)


def test_read_gaps_forecast_utilization_pct_nullable():
    """C2b: forecast_utilization_pct is NULL for zero-supply rows; must parse to None."""
    conn = _gaps_conn_for_contract()
    sql = _shim((_SQL_DIR / "select_gaps.sql").read_text())
    raw = _run(conn, sql)
    athena_rows = _rows_to_athena(raw, _GAPS_TYPE)
    loc_b = next(r for r in athena_rows if r["location_id"] == "loc-b")
    assert loc_b["forecast_utilization_pct"] is None, "DuckDB→Athena NULL must be None"

    with patch(_ATHENA_MODULE, return_value=athena_rows):
        result = read_gaps(stage=_STAGE, fleet_id=_FLEET,
                           athena_client=None, workgroup="", results_s3_path="")

    loc_b_r = next(r for r in result if r["location_id"] == "loc-b")
    assert loc_b_r["forecast_utilization_pct"] is None


# ---------------------------------------------------------------------------
# C2c / W7b  read_outcomes: nullable ratio_hours, gains, and materialised_on returned
# ---------------------------------------------------------------------------

_OUTCOMES_TYPE = {
    "to_location_id": "VARCHAR", "effective_date": "DATE",
    "ratio_hours": "DOUBLE",                     # nullable: NULLIF(predicted, 0)
    "predicted_on_rent_hours_gain": "DOUBLE",     # nullable
    "actual_on_rent_hours_gain": "DOUBLE",        # nullable
    "materialised_on": "DATE",                    # W7b: now selected
}


def _outcomes_conn_for_contract() -> duckdb.DuckDBPyConnection:
    conn = duckdb.connect(":memory:")
    create_producer_table(conn, "move_outcomes_snapshot")
    conn.execute("""
        INSERT INTO adp_test_fleet_rebalance.move_outcomes_snapshot
          (move_id, fleet_id, from_location_id, to_location_id, vins, effective_date,
           review_available_on, predicted_on_rent_hours_gain,
           predicted_turnaways_avoided, actual_on_rent_hours_gain,
           actual_turnaways_at_destination, source_actual_turnaways,
           ratio_hours, method, snapshot_date, materialised_on)
        VALUES
          -- Normal row with all DOUBLE values present
          ('move-1', 'demo-rental-carshare', 'loc-a', 'loc-b', ['VIN-1'],
           DATE '2026-10-01', DATE '2026-10-08',
           12.0, 2, 11.0, 0, 0, 1.1, 'rules',
           DATE '2026-10-06', DATE '2026-10-06'),
          -- Row with NULL ratio_hours (predicted was 0) and NULL gains
          ('move-2', 'demo-rental-carshare', 'loc-a', 'loc-c', ['VIN-2'],
           DATE '2026-10-02', DATE '2026-10-09',
           NULL, 0, NULL, 0, 0, NULL, 'rules',
           DATE '2026-10-06', DATE '2026-10-06')
    """)
    return conn


def test_read_outcomes_null_ratio_hours():
    """C2c: ratio_hours NULL must parse to None (was _f which raised)."""
    conn = _outcomes_conn_for_contract()
    sql = _shim((_SQL_DIR / "select_outcomes.sql").read_text())
    raw = _run(conn, sql)
    athena_rows = _rows_to_athena(raw, _OUTCOMES_TYPE)
    null_row = next(r for r in athena_rows if r["to_location_id"] == "loc-c")
    assert null_row["ratio_hours"] is None

    with patch(_ATHENA_MODULE, return_value=athena_rows):
        result = read_outcomes(stage=_STAGE, fleet_id=_FLEET,
                               athena_client=None, workgroup="", results_s3_path="")

    loc_c = next(r for r in result if r["to_location_id"] == "loc-c")
    assert loc_c["ratio_hours"] is None
    assert loc_c["predicted_on_rent_hours_gain"] is None
    assert loc_c["actual_on_rent_hours_gain"] is None


def test_read_outcomes_materialised_on_returned():
    """W7b: materialised_on must be present in each outcome row."""
    conn = _outcomes_conn_for_contract()
    sql = _shim((_SQL_DIR / "select_outcomes.sql").read_text())
    raw = _run(conn, sql)
    athena_rows = _rows_to_athena(raw, _OUTCOMES_TYPE)

    with patch(_ATHENA_MODULE, return_value=athena_rows):
        result = read_outcomes(stage=_STAGE, fleet_id=_FLEET,
                               athena_client=None, workgroup="", results_s3_path="")

    for r in result:
        assert "materialised_on" in r, "materialised_on must be in every outcome row"
        assert r["materialised_on"] == "2026-10-06"


# ---------------------------------------------------------------------------
# C3  read_busy_windows: TIMESTAMP with fractional seconds (.fff / .ffffff)
# ---------------------------------------------------------------------------

_BUSY_TYPE = {
    "vin": "VARCHAR", "location_id": "VARCHAR",
    "start_at": "TIMESTAMP", "end_at": "TIMESTAMP",
}


def _busy_conn_for_contract() -> duckdb.DuckDBPyConnection:
    conn = duckdb.connect(":memory:")
    create_producer_table(conn, "booking_requests")
    conn.execute("""
        INSERT INTO adp_test_fleet_rebalance.booking_requests
          (fleet_id, vin, location_id, outcome, start_at, end_at)
        VALUES
          -- On rent right now: start before now, end after now
          ('demo-rental-carshare', 'VIN-1', 'loc-a', 'fulfilled',
           TIMESTAMP '2026-10-06 10:00:00', TIMESTAMP '2026-10-06 14:00:00')
    """)
    return conn


def test_read_busy_windows_millisecond_timestamps():
    """C3: timestamps with .000 suffix (Athena engine v3 millisecond precision)."""
    conn = _busy_conn_for_contract()
    sql = _shim((_SQL_DIR / "select_busy_windows.sql").read_text(), now=_NOW)
    raw = _run(conn, sql)
    athena_rows = _rows_to_athena(raw, _BUSY_TYPE)
    # Verify the conversion produces ".000" suffix
    assert athena_rows[0]["start_at"] == "2026-10-06 10:00:00.000", (
        f"DuckDB→Athena TIMESTAMP must have .000 suffix, got {athena_rows[0]['start_at']!r}"
    )

    with patch(_ATHENA_MODULE, return_value=athena_rows):
        result = read_busy_windows(stage=_STAGE, fleet_id=_FLEET, now=_NOW,
                                   athena_client=None, workgroup="", results_s3_path="")

    assert len(result) == 1
    assert result[0]["vin"] == "VIN-1"
    assert isinstance(result[0]["start_at"], _dt.datetime)
    assert result[0]["start_at"] == _dt.datetime(2026, 10, 6, 10, 0, 0)


def test_read_busy_windows_microsecond_timestamps():
    """C3: timestamps with .ffffff suffix must also parse correctly."""
    # Simulate Athena returning microsecond precision
    athena_rows = [
        {"vin": "VIN-2", "location_id": "loc-b",
         "start_at": "2026-10-06 08:00:00.000000",
         "end_at": "2026-10-06 20:00:00.000000"},
    ]
    with patch(_ATHENA_MODULE, return_value=athena_rows):
        result = read_busy_windows(stage=_STAGE, fleet_id=_FLEET, now=_NOW,
                                   athena_client=None, workgroup="", results_s3_path="")

    assert result[0]["start_at"] == _dt.datetime(2026, 10, 6, 8, 0, 0)


# ---------------------------------------------------------------------------
# C1 / W1  read_events: announced_on required; affected_location_ids parsed
# ---------------------------------------------------------------------------

_EVENTS_TYPE = {
    "event_id": "VARCHAR", "metro": "VARCHAR",
    "affected_location_ids": "VARCHAR[]",
    "starts_on": "DATE", "ends_on": "DATE",
    "category": "VARCHAR", "announced_on": "DATE", "uplift_band": "VARCHAR",
}


def _events_conn_for_contract() -> duckdb.DuckDBPyConnection:
    conn = duckdb.connect(":memory:")
    create_producer_table(conn, "metro_events")
    conn.execute("""
        INSERT INTO adp_test_fleet_rebalance.metro_events
          (event_id, metro, affected_location_ids, starts_on, ends_on,
           category, announced_on, uplift_band)
        VALUES
          -- Multi-location event in window
          ('ev-1', 'seattle', ['loc-a', 'loc-b'],
           DATE '2026-10-06', DATE '2026-10-08', 'concert',
           DATE '2026-10-05', 'high'),
          -- Single-location event
          ('ev-2', 'portland', ['loc-c'],
           DATE '2026-10-07', DATE '2026-10-09', 'festival',
           DATE '2026-10-04', 'medium')
    """)
    return conn


def test_read_events_announced_on_returned():
    """C1: announced_on must be present in each returned event row."""
    conn = _events_conn_for_contract()
    sql = _shim((_SQL_DIR / "select_events.sql").read_text(), today=_TODAY)
    raw = _run(conn, sql)
    athena_rows = _rows_to_athena(raw, _EVENTS_TYPE)

    with patch(_ATHENA_MODULE, return_value=athena_rows):
        result = read_events(stage=_STAGE, today=_TODAY,
                             athena_client=None, workgroup="", results_s3_path="")

    for r in result:
        assert "announced_on" in r, "announced_on must be in every event row"
    ev1 = next(r for r in result if r["event_id"] == "ev-1")
    assert ev1["announced_on"] == "2026-10-05"


def test_read_events_affected_location_ids_parsed():
    """W1: affected_location_ids arrives as '[loc-a, loc-b]' and must become a list."""
    conn = _events_conn_for_contract()
    sql = _shim((_SQL_DIR / "select_events.sql").read_text(), today=_TODAY)
    raw = _run(conn, sql)
    athena_rows = _rows_to_athena(raw, _EVENTS_TYPE)

    # Verify the DuckDB→Athena conversion produces the array string form
    ev1_athena = next(r for r in athena_rows if r["event_id"] == "ev-1")
    assert ev1_athena["affected_location_ids"] == "[loc-a, loc-b]", (
        f"DuckDB→Athena array must be '[loc-a, loc-b]', got {ev1_athena['affected_location_ids']!r}"
    )

    with patch(_ATHENA_MODULE, return_value=athena_rows):
        result = read_events(stage=_STAGE, today=_TODAY,
                             athena_client=None, workgroup="", results_s3_path="")

    ev1 = next(r for r in result if r["event_id"] == "ev-1")
    assert isinstance(ev1["affected_location_ids"], list), (
        "affected_location_ids must be a Python list after parsing"
    )
    assert ev1["affected_location_ids"] == ["loc-a", "loc-b"]

    ev2 = next(r for r in result if r["event_id"] == "ev-2")
    assert ev2["affected_location_ids"] == ["loc-c"]


def test_read_events_empty_affected_location_ids():
    """S-b5: Athena's empty array '[]' parses to an empty Python list."""
    conn = _events_conn_for_contract()
    # Insert a third event with no affected locations (empty array)
    conn.execute("""
        INSERT INTO adp_test_fleet_rebalance.metro_events
          (event_id, metro, affected_location_ids, starts_on, ends_on,
           category, announced_on, uplift_band)
        VALUES
          ('ev-empty', 'seattle', [],
           DATE '2026-10-07', DATE '2026-10-09', 'roadwork',
           DATE '2026-10-06', 'low')
    """)
    sql = _shim((_SQL_DIR / "select_events.sql").read_text(), today=_TODAY)
    raw = _run(conn, sql)
    athena_rows = _rows_to_athena(raw, _EVENTS_TYPE)

    # Verify the empty array arrives as '[]' from the Athena conversion
    ev_empty_athena = next(r for r in athena_rows if r["event_id"] == "ev-empty")
    assert ev_empty_athena["affected_location_ids"] == "[]", (
        f"DuckDB→Athena empty array must be '[]', got {ev_empty_athena['affected_location_ids']!r}"
    )

    with patch(_ATHENA_MODULE, return_value=athena_rows):
        result = read_events(stage=_STAGE, today=_TODAY,
                             athena_client=None, workgroup="", results_s3_path="")

    ev_empty = next(r for r in result if r["event_id"] == "ev-empty")
    assert ev_empty["affected_location_ids"] == [], (
        "empty affected_location_ids '[]' must parse to an empty Python list"
    )


# ---------------------------------------------------------------------------
# M7 (re-anchored)  event_scoped_by_location_id_not_metro
# Catches: evidence.py `& set(e.get("affected_location_ids") or [])` → `& set()`
# ---------------------------------------------------------------------------

def test_event_scoped_by_location_id_not_metro():
    """M7: an event naming the from_location must appear in evidence even if its
    metro differs from the candidate's metro.  Removing the location-ID intersection
    (M7) would drop this event.
    """
    from agents.fi_rebalancing.evidence import move_evidence

    # Event in a different metro that explicitly names the from_location
    event_cross_metro = {
        "event_id": "ev-cross",
        "metro": "portland",           # different from candidate metro "seattle"
        "affected_location_ids": ["loc-from"],  # parsed list from W1 fix
        "category": "festival",
        "uplift_band": "high",
        "starts_on": "2026-10-07",
        "ends_on": "2026-10-09",
        "announced_on": "2026-10-06",
    }

    plan = {
        "candidate_id": "cand-from-to",
        "from_location_id": "loc-from",
        "to_location_id": "loc-to",
        "vehicle_vins": ["MRDVEH00000000021"],
        "timing": "today",
        "detail": "test",
        "diagnosis_source": "rules",
        "transfer_cost_usd": 0.0,
        "revenue_gain_usd": 0.0,
        "source_loss_usd": 0.0,
        "revenue_impact_usd": 0.0,
        "net_benefit_usd": 0.0,
        "predicted_on_rent_hours_gain": 0.0,
        "predicted_turnaways_avoided": 0,
    }
    candidate = {
        "candidate_id": "cand-from-to",
        "from_location_id": "loc-from",
        "to_location_id": "loc-to",
        "metro": "seattle",   # different from event metro "portland"
        "count": 1,
        "eligible_vins": ["MRDVEH00000000021"],
        "eligible_facts": {
            "MRDVEH00000000021": {
                "days_idle_last_7": 3,
                "days_to_next_service": float("inf"),
                "hours_to_next_booking": float("inf"),
                "open_lower_severity_items": [],
            }
        },
        "vehicles_short": 1,
        "vehicles_spare": 2,
        "parking_room": 5,
        "distance_miles": 10.0,
        "transfer_cost_per_vehicle": 40.0,
        "per_day": [],
        "effective_date": "2026-10-07",
        "first_deficit_day": "2026-10-07",
        "forecast_week": "2026-W41",
        "severity": "soon",
        "confidence": "medium",
        "max_utilization": 0.95,
        "rate": 3.50,
        "source_rate": 3.00,
        "mean_request_hours": 8.0,
        "net_benefit_usd": 0.0,
        "transfer_cost_usd": 0.0,
        "revenue_gain_usd": 0.0,
        "source_loss_usd": 0.0,
        "revenue_impact_usd": 0.0,
        "predicted_on_rent_hours_gain": 0.0,
        "predicted_turnaways_avoided": 0,
        "gaps_data_as_of": "2026-10-06",
    }
    from_location = {
        "locationId": "loc-from", "name": "From", "locationType": "downtown",
        "metro": "seattle", "latitude": 47.60, "longitude": -122.33,
        "parkingSpaces": 10, "operatingHours": "08-20", "op_hours": 12.0,
    }
    to_location = {
        "locationId": "loc-to", "name": "To", "locationType": "airport",
        "metro": "seattle", "latitude": 47.45, "longitude": -122.30,
        "parkingSpaces": 20, "operatingHours": "06-22", "op_hours": 16.0,
    }

    evidence = move_evidence(
        plan, candidate,
        computed_at="2026-10-06T06:00:00Z",
        from_location=from_location,
        to_location=to_location,
        outcomes=[],
        events=[event_cross_metro],
    )

    events_entry = next(e for e in evidence if e["kind"] == "rebalance.events")
    event_ids = {e["event_id"] for e in events_entry["value"]}
    assert "ev-cross" in event_ids, (
        "Event naming from_location_id must appear in evidence regardless of metro "
        "(M7 catch: removing & set(affected_location_ids) would drop it)"
    )


# ---------------------------------------------------------------------------
# W7a  read_vehicle_days: snapshot_date returned
# ---------------------------------------------------------------------------

_VD_TYPE = {
    "vin": "VARCHAR", "location_id": "VARCHAR",
    "available_hours": "DOUBLE", "snapshot_date": "DATE",
}


def _vd_conn_for_contract() -> duckdb.DuckDBPyConnection:
    conn = duckdb.connect(":memory:")
    create_producer_table(conn, "vehicle_days")
    conn.execute("""
        INSERT INTO adp_test_fleet_rebalance.vehicle_days
          (vin, fleet_id, location_id, snapshot_date, op_open_h, available_hours)
        VALUES
          ('VIN-1', 'demo-rental-carshare', 'loc-a', DATE '2026-10-06', 8, 8.0),
          ('VIN-2', 'demo-rental-carshare', 'loc-b', DATE '2026-10-06', 8, 6.0)
    """)
    return conn


def test_read_vehicle_days_snapshot_date_returned():
    """W7a: snapshot_date must be present in each vehicle_days row."""
    conn = _vd_conn_for_contract()
    sql = _shim((_SQL_DIR / "select_vehicle_days.sql").read_text())
    raw = _run(conn, sql)
    athena_rows = _rows_to_athena(raw, _VD_TYPE)

    with patch(_ATHENA_MODULE, return_value=athena_rows):
        result = read_vehicle_days(stage=_STAGE, fleet_id=_FLEET,
                                   athena_client=None, workgroup="", results_s3_path="")

    for r in result:
        assert "snapshot_date" in r, "snapshot_date must be in every vehicle_days row"
        assert r["snapshot_date"] == "2026-10-06"


# ---------------------------------------------------------------------------
# W2  service_soon: service_month partition filter excludes wrong-month rows
# (SQL-level; already tested in test_sql_duckdb.py via test_service_soon_partition_filter)
# Reader-level: verify read_service_soon parses vin and service_date correctly.
# ---------------------------------------------------------------------------

_SERVICE_TYPE = {
    "vin": "VARCHAR", "service_date": "DATE", "service_month": "DATE",
}


def _service_conn_for_contract() -> duckdb.DuckDBPyConnection:
    conn = duckdb.connect(":memory:")
    create_producer_table(conn, "service_records")
    conn.execute("""
        INSERT INTO adp_test_service_records.service_records
          (service_id, service_date, vin, service_type, outcome, service_month)
        VALUES
          ('sr-1', DATE '2026-10-07', 'VIN-A', 'tires', 'scheduled', DATE '2026-10-01'),
          -- W2: same service_date but wrong month → excluded by SQL
          ('sr-2', DATE '2026-10-07', 'VIN-B', 'oil',   'scheduled', DATE '2026-09-01')
    """)
    return conn


def test_read_service_soon_partition_filter_excludes_wrong_month():
    """W2: service_month filter excludes rows with a mismatched partition month."""
    conn = _service_conn_for_contract()
    sql = _shim((_SQL_DIR / "select_service_soon.sql").read_text(), today=_TODAY)
    raw = _run(conn, sql)
    # Only VIN-A (correct month) should survive the SQL
    assert len(raw) == 1, f"partition filter must exclude the wrong-month row, got {raw}"
    assert raw[0]["vin"] == "VIN-A"

    athena_rows = _rows_to_athena(raw, _SERVICE_TYPE)
    with patch(_ATHENA_MODULE, return_value=athena_rows):
        result = read_service_soon(stage=_STAGE, today=_TODAY,
                                   athena_client=None, workgroup="", results_s3_path="")

    vins = {r["vin"] for r in result}
    assert "VIN-A" in vins
    assert "VIN-B" not in vins, "W2 partition filter must exclude wrong-month row"
    assert result[0]["service_date"] == "2026-10-07"



# ---------------------------------------------------------------------------
# C-p2-1 (reader half)  read_open_ros: DMS-shaped rows; only open RO's vehicle_vin
# Catches: MB-7 — _s(r, "vehicle_vin") → _s(r, "ro_id") would return "RO-OPEN" not "VIN-OPEN"
# ---------------------------------------------------------------------------

_OPEN_ROS_TYPE = {
    "ro_id": "VARCHAR",
    "vehicle_vin": "VARCHAR",
    "status": "VARCHAR",
}


def _open_ros_conn_for_contract() -> duckdb.DuckDBPyConnection:
    conn = duckdb.connect(":memory:")
    create_producer_table(conn, "repair_orders")
    conn.execute("""
        INSERT INTO dms_test.repair_orders
          (ro_id, vehicle_vin, status, _seq_no)
        VALUES
          -- As the DMS lake holds it: all strings, _seq_no zero-padded to 40 digits, DMS
          -- statuses (Draft / InProgress / AwaitingParts open; Ready / Delivered / Invoiced / Closed
          -- closed).
          ('RO-OPEN', 'VIN-OPEN', 'Draft', lpad('9', 40, '0')),
          ('RO-OPEN', 'VIN-OPEN', 'InProgress', lpad('10', 40, '0')),
          -- One closed RO (status in the exclusion list)
          ('RO-CLOSED', 'VIN-CLOSED', 'Delivered', lpad('1', 40, '0'))
    """)
    return conn


def test_read_open_ros_vehicle_vin_from_open_ro_only():
    """C-p2-1 (reader): DMS-shaped row through real read_open_ros; only open RO's vehicle_vin returned.

    Catches MB-7: _s(r, "vehicle_vin") → _s(r, "ro_id") would give "RO-OPEN" instead of "VIN-OPEN".
    """
    conn = _open_ros_conn_for_contract()
    sql = _shim((_SQL_DIR / "select_open_ros.sql").read_text())
    raw = _run(conn, sql)
    athena_rows = _rows_to_athena(raw, _OPEN_ROS_TYPE)

    # SQL must keep exactly one row: the open RO's latest image
    assert len(athena_rows) == 1, (
        f"Expected 1 open RO after SQL filter, got {len(athena_rows)}: {athena_rows}"
    )
    assert athena_rows[0]["vehicle_vin"] == "VIN-OPEN", (
        "DuckDB→Athena vehicle_vin must be 'VIN-OPEN'"
    )

    with patch(_ATHENA_MODULE, return_value=athena_rows):
        result = read_open_ros(stage=_STAGE, athena_client=None, workgroup="", results_s3_path="")

    assert len(result) == 1
    assert result[0]["vehicle_vin"] == "VIN-OPEN", (
        "vehicle_vin must map from vehicle_vin column, not ro_id (MB-7 catch)"
    )
    assert result[0]["ro_id"] == "RO-OPEN"



# ---------------------------------------------------------------------------
# W-b1 (part 1)  read_forecast_days: mape_28d NULL and NaN
# Catches: MB-4 — _f_opt → _f would raise SourceReadError on NULL mape_28d
# ---------------------------------------------------------------------------

_FORECAST_TYPE = {
    "location_id": "VARCHAR",
    "forecast_date": "DATE",
    "forecast_hours": "DOUBLE",
    "method": "VARCHAR",
    "mape_28d": "DOUBLE",
    "run_date": "DATE",
    "data_as_of": "DATE",
}


def _forecast_conn_for_contract() -> duckdb.DuckDBPyConnection:
    conn = duckdb.connect(":memory:")
    create_producer_table(conn, "fleet_bookings_forecast")
    conn.execute("""
        INSERT INTO adp_test_fleet_rebalance.fleet_bookings_forecast
          (location_id, forecast_date, run_date, forecast_hours, method,
           baseline_hours, pickup_hours, event_uplift_factor, mape_28d, data_as_of)
        VALUES
          -- NULL mape_28d: ADP's "no 28-day backtest" form
          ('loc-a', DATE '2026-10-07', DATE '2026-10-06', 90.0, 'baseline',
           85.0, 5.0, 1.0, NULL, DATE '2026-10-06'),
          -- NaN mape_28d: an alternate non-finite sentinel
          ('loc-b', DATE '2026-10-07', DATE '2026-10-06', 60.0, 'baseline',
           55.0, 5.0, 1.0, CAST('NaN' AS DOUBLE), DATE '2026-10-06')
    """)
    return conn


def test_read_forecast_days_mape_28d_null_and_nan():
    """W-b1: NULL and NaN mape_28d must both pass through _f_opt without raising.

    Catches MB-4: _f_opt → _f would raise SourceReadError on NULL mape_28d,
    aborting every run for a location with no 28-day backtest.
    """
    conn = _forecast_conn_for_contract()
    sql = _shim((_SQL_DIR / "select_forecast_days.sql").read_text())
    raw = _run(conn, sql)
    athena_rows = _rows_to_athena(raw, _FORECAST_TYPE)

    null_row = next(r for r in athena_rows if r["location_id"] == "loc-a")
    assert null_row["mape_28d"] is None, (
        "NULL mape_28d must survive DuckDB→Athena as None"
    )
    nan_row = next(r for r in athena_rows if r["location_id"] == "loc-b")
    assert nan_row["mape_28d"] == "NaN", (
        f"NaN mape_28d must arrive as Athena's 'NaN', got {nan_row['mape_28d']!r}"
    )

    with patch(_ATHENA_MODULE, return_value=athena_rows):
        result = read_forecast_days(stage=_STAGE, fleet_id=_FLEET,
                                    athena_client=None, workgroup="", results_s3_path="")

    loc_a = next(r for r in result if r["location_id"] == "loc-a")
    assert loc_a["mape_28d"] is None, (
        "NULL mape_28d must parse to None, not raise (MB-4 catch)"
    )

    loc_b = next(r for r in result if r["location_id"] == "loc-b")
    assert loc_b["mape_28d"] is not None and math.isnan(loc_b["mape_28d"]), (
        "NaN mape_28d must parse to float nan (MB-4 catch: _f would raise on NULL)"
    )

    # W-c3-2 (G4-3): every other mapped field, exactly. The agent joins forecast rows to the
    # horizon on the forecast_date string, so a field mapped from the wrong column would make
    # every location fail the 7-row horizon check and the run write nothing.
    def _strip(r):
        return {k: v for k, v in r.items() if k != "mape_28d"}
    assert _strip(loc_a) == {
        "location_id": "loc-a", "forecast_date": "2026-10-07", "forecast_hours": 90.0,
        "method": "baseline", "run_date": "2026-10-06", "data_as_of": "2026-10-06",
    }, loc_a
    assert _strip(loc_b) == {
        "location_id": "loc-b", "forecast_date": "2026-10-07", "forecast_hours": 60.0,
        "method": "baseline", "run_date": "2026-10-06", "data_as_of": "2026-10-06",
    }, loc_b


def test_athena_double_helper_matches_live_athena():
    """The test helper renders DOUBLE exactly as live Athena did (query f54fee95, 2026-10-12)."""
    observed = {float("nan"): "NaN", float("inf"): "Infinity", float("-inf"): "-Infinity",
                12000000.0: "1.2E7", 0.0001: "1.0E-4", 12.0: "12.0", 12.75: "12.75"}
    for value, athena in observed.items():
        assert _to_athena(value, "DOUBLE") == athena, (value, _to_athena(value, "DOUBLE"))



# ---------------------------------------------------------------------------
# W-b1 (part 2)  read_vehicle_utilization: on_rent_hours "0.0"
# Catches: MB-5 — _f → _i would raise on "0.0" (int("0.0") fails)
# ---------------------------------------------------------------------------

_UTIL_TYPE = {
    "vin": "VARCHAR",
    "snapshot_date": "DATE",
    "available_hours": "DOUBLE",
    "on_rent_hours": "DOUBLE",
}


def _util_conn_for_contract() -> duckdb.DuckDBPyConnection:
    conn = duckdb.connect(":memory:")
    create_producer_table(conn, "vehicle_days")
    create_producer_table(conn, "vehicle_utilization_daily")
    # vehicle_days provides the latest snapshot_date for the CTE
    conn.execute("""
        INSERT INTO adp_test_fleet_rebalance.vehicle_days
          (vin, fleet_id, location_id, snapshot_date, op_open_h, available_hours)
        VALUES
          ('VIN-1', 'demo-rental-carshare', 'loc-a', DATE '2026-10-06', 8, 8.0)
    """)
    # on_rent_hours = 0 is an idle day; Athena returns DOUBLE "0.0"
    conn.execute("""
        INSERT INTO adp_test_fleet_rebalance.vehicle_utilization_daily
          (vin, location_id, snapshot_date, on_rent_hours, available_hours, utilization_pct)
        VALUES
          ('VIN-1', 'loc-a', DATE '2026-10-06', 0.0, 8.0, 0.0)
    """)
    return conn


def test_read_vehicle_utilization_on_rent_hours_zero_point_zero():
    """W-b1: on_rent_hours DOUBLE "0.0" must parse to float 0.0 via _f.

    Catches MB-5: _f → _i would raise SourceReadError because int("0.0") fails.
    """
    conn = _util_conn_for_contract()
    sql = _shim((_SQL_DIR / "select_vehicle_utilization.sql").read_text())
    raw = _run(conn, sql)
    athena_rows = _rows_to_athena(raw, _UTIL_TYPE)

    assert len(athena_rows) == 1
    assert athena_rows[0]["on_rent_hours"] == "0.0", (
        f"DuckDB→Athena on_rent_hours must be '0.0', got {athena_rows[0]['on_rent_hours']!r}"
    )

    with patch(_ATHENA_MODULE, return_value=athena_rows):
        result = read_vehicle_utilization(stage=_STAGE, fleet_id=_FLEET,
                                          athena_client=None, workgroup="", results_s3_path="")

    assert len(result) == 1
    assert result[0]["on_rent_hours"] == 0.0, (
        "on_rent_hours '0.0' must parse to float 0.0 (MB-5 catch: _i would raise)"
    )
    assert isinstance(result[0]["on_rent_hours"], float)



# ---------------------------------------------------------------------------
# W-b1 (part 3)  read_request_lengths: mean_request_hours, a non-integral DOUBLE string
# Catches: MB-6 — _f → _i would raise on a string like "12.5". Athena types
# AVG(bigint / 3600.0) as double, not DECIMAL (live check, query f54fee95, 2026-10-12: "12.75").
# ---------------------------------------------------------------------------

_REQUEST_TYPE = {
    "location_id": "VARCHAR",
    "mean_request_hours": "DOUBLE",
}


def _request_conn_for_contract() -> duckdb.DuckDBPyConnection:
    conn = duckdb.connect(":memory:")
    create_producer_table(conn, "booking_requests")
    # 12.5-hour request within the 28-day window; AVG → 12.5 DOUBLE
    conn.execute("""
        INSERT INTO adp_test_fleet_rebalance.booking_requests
          (request_id, fleet_id, location_id, requested_at,
           start_at, end_at, booking_type, lead_time_days, outcome,
           vin, event_id, generator_version, dimension_version, provenance)
        VALUES
          ('req-1', 'demo-rental-carshare', 'loc-a',
           TIMESTAMP '2026-10-05 09:00:00',
           TIMESTAMP '2026-10-05 10:00:00', TIMESTAMP '2026-10-05 22:30:00',
           'standard', 1, 'fulfilled',
           'VIN-1', NULL, 'v1', 'v1', 'sim')
    """)
    return conn


def test_read_request_lengths_decimal_string():
    """W-b1: mean_request_hours DOUBLE (e.g. '12.5') must parse to float via _f.

    Catches MB-6: _f → _i would raise SourceReadError because int('12.5') fails;
    Athena returns this AVG as a DOUBLE (live check above), e.g. "12.75".
    """
    conn = _request_conn_for_contract()
    sql = _shim((_SQL_DIR / "select_request_lengths.sql").read_text(), now=_NOW)
    raw = _run(conn, sql)
    athena_rows = _rows_to_athena(raw, _REQUEST_TYPE)

    assert len(athena_rows) == 1
    # Verify _to_athena produces a decimal (non-integer) string
    val_str = athena_rows[0]["mean_request_hours"]
    assert "." in val_str, (
        f"mean_request_hours must be a decimal string (e.g. '12.5'), got {val_str!r}"
    )

    with patch(_ATHENA_MODULE, return_value=athena_rows):
        result = read_request_lengths(stage=_STAGE, fleet_id=_FLEET, now=_NOW,
                                      athena_client=None, workgroup="", results_s3_path="")

    assert len(result) == 1
    assert result[0]["mean_request_hours"] == 12.5, (
        "a non-integral DOUBLE string must parse to float 12.5 (MB-6 catch: _i would raise)"
    )
    assert isinstance(result[0]["mean_request_hours"], float)
