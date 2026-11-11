"""fi_quality Athena-side seeder for FiLoopWorld.

The fi_quality investigation agent reads from Athena, not from DDB.
``adp_writer.py``'s ``seed_pattern`` seeds the ADP digital-thread writer's DDB
tables; this module seeds the FakeAthena DuckDB tables the fi_quality agent needs
to run ``build_investigation_bundle`` successfully.

Minimum tables required (from agents/fi_quality/readers.py):

  adp_{stage}_engineering.fault_patterns
      SELECT ... WHERE pattern_group_id = '<pattern_id>'
      (note: fi_quality uses pattern_group_id, ADP writer uses pattern_id)

  adp_{stage}_engineering.vehicle_build_components
      (the investigate call needs build facts; create empty table)

  adp_{stage}_vehicle_identity.vehicle_identity
      (for model/year/plant; create minimal rows for the affected VINs)

  adp_{stage}_engineering.engineering_failure_modes
  adp_{stage}_engineering.engineering_remedy_options
      (joined query; must have at least one row for the failure_mode_id)

  All other tables (controls, co-occurring codes, OTA campaigns, service records)
  may be empty — the agent handles no rows gracefully (empty evidence list).

Facts cited (file:line in this worktree):
  - agents/fi_quality/readers.py:177  _PATTERN_SQL  WHERE pattern_group_id
  - agents/fi_quality/readers.py:200  affected_vins_sample_csv
  - agents/fi_quality/readers.py:259  _FAILURE_MODES_SQL columns
  - agents/fi_quality/job.py:682      read_pattern_group call
  - agents/fi_quality/job.py:803      read_failure_modes call
"""
from __future__ import annotations

import datetime
from typing import Any


# ── DuckDB type maps ──────────────────────────────────────────────────────────

_FAULT_PATTERNS_DDL = """\
CREATE TABLE IF NOT EXISTS adp_{stage}_engineering.fault_patterns (
    pattern_id VARCHAR,
    code_family VARCHAR,
    cohort_dimension VARCHAR,
    cohort_value VARCHAR,
    candidate_rank INTEGER,
    pattern_group_id VARCHAR,
    vehicles_affected INTEGER,
    vehicles_in_service INTEGER,
    rate DOUBLE,
    rate_rest DOUBLE,
    rate_ratio DOUBLE,
    p_value DOUBLE,
    first_detected_month VARCHAR,
    trend VARCHAR,
    method VARCHAR,
    affected_vins_sample VARCHAR[],
    computed_at TIMESTAMP,
    parameters_version VARCHAR
)
"""

_VEHICLE_BUILD_COMPONENTS_DDL = """\
CREATE TABLE IF NOT EXISTS adp_{stage}_engineering.vehicle_build_components (
    vin VARCHAR,
    component_id VARCHAR,
    supplier_id VARCHAR,
    supplier_lot VARCHAR,
    component_category VARCHAR,
    installed_date DATE,
    part_number VARCHAR
)
"""

_VEHICLE_IDENTITY_DDL = """\
CREATE TABLE IF NOT EXISTS adp_{stage}_vehicle_identity.vehicle_identity (
    vin VARCHAR,
    model VARCHAR,
    model_year INTEGER,
    manufacture_date DATE,
    assembly_plant VARCHAR
)
"""

_FAILURE_MODES_DDL = """\
CREATE TABLE IF NOT EXISTS adp_{stage}_engineering.engineering_failure_modes (
    failure_mode_id VARCHAR,
    component_id VARCHAR,
    mechanism VARCHAR,
    symptom_codes VARCHAR[],
    cause_dimension VARCHAR,
    do_not_drive BOOLEAN,
    park_outside BOOLEAN
)
"""

_REMEDY_OPTIONS_DDL = """\
CREATE TABLE IF NOT EXISTS adp_{stage}_engineering.engineering_remedy_options (
    failure_mode_id VARCHAR,
    remedy_option_id VARCHAR,
    remedy_type VARCHAR,
    parts_cost_per_vehicle_usd DOUBLE,
    labor_hours_per_vehicle DOUBLE,
    lead_time_weeks DOUBLE,
    over_the_air BOOLEAN,
    trade_off_notes VARCHAR
)
"""

# Empty tables — queries return no rows, agent handles gracefully.
_EMPTY_TABLES_DDL = [
    "CREATE SCHEMA IF NOT EXISTS adp_{stage}_engineering",
    "CREATE SCHEMA IF NOT EXISTS adp_{stage}_vehicle_identity",
    "CREATE SCHEMA IF NOT EXISTS adp_{stage}_ota_campaigns",
    "CREATE SCHEMA IF NOT EXISTS adp_{stage}_service_records",
    "CREATE SCHEMA IF NOT EXISTS adp_{stage}_fleet_recall",
    "CREATE SCHEMA IF NOT EXISTS adp_{stage}_fleet_health",
    """\
CREATE TABLE IF NOT EXISTS adp_{stage}_ota_campaigns.ota_campaigns (
    campaign_id VARCHAR, campaign_name VARCHAR, status VARCHAR, release_version VARCHAR
)""",
    """\
CREATE TABLE IF NOT EXISTS adp_{stage}_ota_campaigns.ota_campaign_events (
    campaign_id VARCHAR, vin VARCHAR
)""",
    """\
CREATE TABLE IF NOT EXISTS adp_{stage}_service_records.service_records (
    vin VARCHAR, service_date DATE, service_type VARCHAR,
    dtc_codes VARCHAR[], total_cost_usd DOUBLE, outcome VARCHAR,
    linked_campaign_id VARCHAR
)""",
    """\
CREATE TABLE IF NOT EXISTS adp_{stage}_fleet_health.vehicle_fault_status_daily (
    vin VARCHAR, code VARCHAR, status VARCHAR, snapshot_date DATE
)""",
    # snapshot_runs is queried by snapshot_freshness.py before any snapshot table read.
    # One row per table_key; the fi_quality agent checks freshness for
    # 'vehicle_fault_status_daily'.  We seed one row so the freshness check passes.
    """\
CREATE TABLE IF NOT EXISTS adp_{stage}_fleet_health.snapshot_runs (
    table_key VARCHAR, snapshot_date DATE, row_count INTEGER
)""",
]


def seed_fi_quality_tables(
    athena: Any,
    *,
    stage: str = "staging",
    pattern_id: str,
    vin_list: list[str],
    failure_mode_id: str,
    cohort_dimension: str = "supplier_lot",
    cohort_value: str = "SUP-A/LOT-001",
    code_family: str = "C1A45",
    option_id: str = "opt-recall-001",
) -> None:
    """Seed the fi_quality agent's Athena tables in *athena*'s DuckDB.

    Idempotent: uses ``CREATE TABLE IF NOT EXISTS``; re-seeding with the same
    ``pattern_id`` inserts duplicate rows (acceptable in a single-test world).

    :param athena: a ``FakeAthena`` instance.
    :param stage: ADP stage name.
    :param pattern_id: the pattern group id the investigation will look up.
    :param vin_list: affected VINs (max 50 per _VIN_QUERY_CAP).
    :param failure_mode_id: the failure mode to return.
    :param cohort_dimension: 'supplier_lot' etc.
    :param cohort_value: e.g. 'SUP-A/LOT-001'.
    :param code_family: DTC code family prefix, e.g. 'C1A45'.
    :param option_id: remedy option id for the recall.
    """
    con = athena.con

    # Create all schemas and empty tables first.
    for ddl in _EMPTY_TABLES_DDL:
        con.execute(ddl.format(stage=stage))

    today = datetime.date.today()

    # fault_patterns — one row per affected VIN sample, keyed by pattern_group_id.
    # candidate_rank=1 means it's the top candidate.
    con.execute(_FAULT_PATTERNS_DDL.format(stage=stage))
    vin_sample = vin_list[:10]  # up to 10 VINs in the sample
    con.execute(
        f"INSERT INTO adp_{stage}_engineering.fault_patterns VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            pattern_id, code_family, cohort_dimension, cohort_value,
            1, pattern_id,  # candidate_rank=1, pattern_group_id=pattern_id
            len(vin_list), len(vin_list) + 100,  # vehicles_affected, vehicles_in_service
            0.05, 0.005,  # rate, rate_rest
            10.0, 0.0001,  # rate_ratio, p_value
            "2026-01", "rising", "cohort",
            vin_sample,  # affected_vins_sample (VARCHAR[])
            datetime.datetime(2026, 10, 1, 5, 0, 0),  # computed_at
            "1.0",  # parameters_version
        ],
    )

    # Seed snapshot_runs (snapshot_freshness.py checks freshness before reading fleet_health tables).
    # One row per ADP key; the agent checks 'vehicle_fault_status_daily'.
    con.execute(
        f"INSERT INTO adp_{stage}_fleet_health.snapshot_runs "
        f"(table_key, snapshot_date, row_count) VALUES "
        f"('vehicle_fault_status_daily', ?, 0)",
        [today],
    )

    # vehicle_build_components — one row per VIN with the cohort lot.
    con.execute(_VEHICLE_BUILD_COMPONENTS_DDL.format(stage=stage))
    supplier_id, _, lot = cohort_value.partition("/")
    for v in vin_list:
        con.execute(
            f"INSERT INTO adp_{stage}_engineering.vehicle_build_components "
            f"(vin, component_id, supplier_id, supplier_lot, component_category, installed_date, part_number) "
            f"VALUES (?, ?, ?, ?, ?, ?, ?)",
            [v, "suspension_bushing", supplier_id or cohort_value, lot or cohort_value,
             "suspension", datetime.date(2023, 3, 1), "MRD-P-SUS"],
        )

    # managed_vehicles — one row per VIN (C10 fix: _pick_pattern computes managed×lot
    # per candidate via Athena; needs at least one managed VIN in the table).
    for v in vin_list:
        con.execute(
            f"INSERT INTO adp_{stage}_fleet_recall.managed_vehicles (vin, fleet_id, snapshot_date) "
            f"VALUES (?, ?, ?)",
            [v, "fleet-test-001", today],
        )

    # vehicle_identity — minimal row per VIN.
    con.execute(_VEHICLE_IDENTITY_DDL.format(stage=stage))
    for v in vin_list:
        con.execute(
            f"INSERT INTO adp_{stage}_vehicle_identity.vehicle_identity VALUES (?, ?, ?, ?, ?)",
            [v, "Azimuth", 2023, datetime.date(2023, 2, 1), "CGA"],
        )

    # engineering_failure_modes + remedy_options — one mode, one recall option.
    con.execute(_FAILURE_MODES_DDL.format(stage=stage))
    con.execute(
        f"INSERT INTO adp_{stage}_engineering.engineering_failure_modes VALUES (?, ?, ?, ?, ?, ?, ?)",
        [failure_mode_id, "suspension_bushing", "fatigue crack",
         [code_family + "0", code_family + "1"],  # symptom_codes: real-shaped 5-char codes
         cohort_dimension, False, False],
    )
    con.execute(_REMEDY_OPTIONS_DDL.format(stage=stage))
    con.execute(
        f"INSERT INTO adp_{stage}_engineering.engineering_remedy_options VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [failure_mode_id, option_id, "hardware_recall",
         120.0, 2.0, 8.0, False, "Replace suspension bushing"],
    )
    # Also seed a second non-recall option for the design-options step.
    con.execute(
        f"INSERT INTO adp_{stage}_engineering.engineering_remedy_options VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [failure_mode_id, "opt-monitor-001", "monitor",
         0.0, 0.5, 0.0, True, "Continue monitoring"],
    )
