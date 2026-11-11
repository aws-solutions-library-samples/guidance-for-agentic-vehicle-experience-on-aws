"""The columns each table the rebalancing agent reads really has, copied from its producer.

Every DuckDB fixture table in this package is created from these lists and nothing else, so a
query that names a column the producer does not write fails here, as it would in Athena.
Fixtures built from the reader's own assumptions passed a `fleet_id` filter on three tables
that have no such column (architect check, 2026-10-10, Group 4 part B).

Sources (ADP main 690ccdbb unless named):
- supply_demand_gaps_snapshot, move_outcomes_snapshot: platform-foundation/stacks/optional/
  fleet_rebalance_stack.py MATERIALISE_SDG_CREATE_SQL, MATERIALISE_MO_CREATE_SQL.
- fleet_bookings_forecast, booking_requests, vehicle_days, metro_events: platform-foundation/
  source/data-products/fleet_bookings/schema.yaml.
- vehicle_utilization_daily: fleet_bookings/views/vehicle_utilization_daily.sql (final SELECT).
- service_records: source/data-products/service_records/schema.yaml (subset: every column the
  agent may read, plus the key).
- dms repair_orders: the four columns ADP's supply_demand_gaps.sql and Stage C read
  (ro_id, vehicle_vin, status, _seq_no); a subset of the DMS lake table.
"""
from __future__ import annotations

PRODUCER_COLUMNS: dict[str, list[tuple[str, str]]] = {
    "supply_demand_gaps_snapshot": [
        ("location_id", "VARCHAR"), ("horizon_days", "INTEGER"), ("forecast_hours", "DOUBLE"),
        ("supply_vehicles", "DOUBLE"), ("supply_hours", "DOUBLE"),
        ("forecast_utilization_pct", "DOUBLE"), ("status", "VARCHAR"),
        ("vehicles_short", "BIGINT"), ("vehicles_spare", "BIGINT"), ("data_as_of", "DATE"),
        ("snapshot_date", "DATE"), ("materialised_on", "DATE"),
    ],
    "move_outcomes_snapshot": [
        ("move_id", "VARCHAR"), ("fleet_id", "VARCHAR"), ("from_location_id", "VARCHAR"),
        ("to_location_id", "VARCHAR"), ("vins", "VARCHAR[]"), ("effective_date", "DATE"),
        ("review_available_on", "DATE"), ("predicted_on_rent_hours_gain", "DOUBLE"),
        ("predicted_turnaways_avoided", "DOUBLE"), ("actual_on_rent_hours_gain", "DOUBLE"),
        ("actual_turnaways_at_destination", "BIGINT"), ("source_actual_turnaways", "BIGINT"),
        ("ratio_hours", "DOUBLE"), ("method", "VARCHAR"), ("snapshot_date", "DATE"),
        ("materialised_on", "DATE"),
    ],
    "fleet_bookings_forecast": [
        ("location_id", "VARCHAR"), ("forecast_date", "DATE"), ("run_date", "DATE"),
        ("forecast_hours", "DOUBLE"), ("method", "VARCHAR"), ("baseline_hours", "DOUBLE"),
        ("pickup_hours", "DOUBLE"), ("event_uplift_factor", "DOUBLE"), ("mape_28d", "DOUBLE"),
        ("data_as_of", "DATE"),
    ],
    "booking_requests": [
        ("request_id", "VARCHAR"), ("fleet_id", "VARCHAR"), ("location_id", "VARCHAR"),
        ("requested_at", "TIMESTAMP"), ("start_at", "TIMESTAMP"), ("end_at", "TIMESTAMP"),
        ("booking_type", "VARCHAR"), ("lead_time_days", "INTEGER"), ("outcome", "VARCHAR"),
        ("vin", "VARCHAR"), ("event_id", "VARCHAR"), ("generator_version", "VARCHAR"),
        ("dimension_version", "VARCHAR"), ("provenance", "VARCHAR"),
    ],
    "vehicle_days": [
        ("vin", "VARCHAR"), ("fleet_id", "VARCHAR"), ("location_id", "VARCHAR"),
        ("snapshot_date", "DATE"), ("op_open_h", "INTEGER"), ("available_hours", "DOUBLE"),
    ],
    "metro_events": [
        ("event_id", "VARCHAR"), ("metro", "VARCHAR"), ("affected_location_ids", "VARCHAR[]"),
        ("starts_on", "DATE"), ("ends_on", "DATE"), ("category", "VARCHAR"),
        ("announced_on", "DATE"), ("uplift_band", "VARCHAR"),
    ],
    "vehicle_utilization_daily": [
        ("vin", "VARCHAR"), ("location_id", "VARCHAR"), ("snapshot_date", "DATE"),
        ("on_rent_hours", "DOUBLE"), ("available_hours", "DOUBLE"), ("utilization_pct", "DOUBLE"),
    ],
    "service_records": [
        ("service_id", "VARCHAR"), ("service_date", "DATE"), ("vin", "VARCHAR"),
        ("service_type", "VARCHAR"), ("outcome", "VARCHAR"),
        ("service_month", "DATE"),   # partition key (W2: partition filter added to select_service_soon.sql)
    ],
    "repair_orders": [
        ("ro_id", "VARCHAR"), ("vehicle_vin", "VARCHAR"), ("status", "VARCHAR"),
        # DMS lake: every column string (Iceberg); _seq_no is the DynamoDB stream sequence
        # number zero-padded to 40 digits (DMS deployment/glue/ddb_to_iceberg_merge.py; live
        # dms_staging.repair_orders checked 2026-10-12).
        ("_seq_no", "VARCHAR"),
    ],
}

#: Database (schema) each table lives in, with ``test`` as the stage.
DATABASE: dict[str, str] = {
    "service_records": "adp_test_service_records",
    "repair_orders": "dms_test",
}
DEFAULT_DATABASE = "adp_test_fleet_rebalance"


def create_producer_table(conn, table: str) -> str:
    """Create ``table`` in DuckDB with exactly its producer's columns; return its qualified name."""
    db = DATABASE.get(table, DEFAULT_DATABASE)
    conn.execute(f"CREATE SCHEMA IF NOT EXISTS {db}")
    cols = ", ".join(f"{name} {typ}" for name, typ in PRODUCER_COLUMNS[table])
    conn.execute(f"CREATE TABLE {db}.{table} ({cols})")
    return f"{db}.{table}"
