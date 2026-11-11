"""AdpFaults — fleet-health views on the world's FakeAthena DuckDB.

Loads vehicle_fault_status.sql, fault_fleet_summary.sql, and pm_vehicle_status.sql
from ADP 895f7b61 via git archive (no working-tree fallback), and
pm_operator_completions_view.sql from the optional cms_ingest subtree via git show.
Applies view_runner.py Trino→DuckDB rules plus two extra rules documented below.

SOURCES:
  fleet_ops.faults       TABLE (not the coverage-union view); world seeds via add_fault().
  fleet_ops_faults.sql   NOT installed: coverage-union not needed; add_fault() seeds directly.
  vehicle_identity       Minimal stub (vin, model, model_year) for _SQL_FAULTS LEFT JOIN.
  cms_ingest.pm_plans    TABLE; world populates via seed_pm_plan_to_adp().
  cms_ingest.vehicles    TABLE; world populates via _load_cms_vehicles().
  fleet_ops.daily_usage  TABLE (empty stub; odo proxy for mileage-basis plans).
  service_records        TABLE (empty stub; service_records completion source).

Extra Trino→DuckDB rules (beyond view_runner.py; cited in _RULES order):
  array(json) → JSON[]
    Trino CAST(... AS array(json)) used in plan_powertrains / plan_models CTEs of
    pm_vehicle_status (pm_vehicle_status.sql dialect notes §4) and the completions
    UNNEST in pm_operator_completions_view.sql.  DuckDB equivalent: JSON[].
  json_parse(p.completions) → p.completions
    pm_operator_completions_view.sql wraps the VARCHAR completions column in
    json_parse (Trino's JSON type constructor); DuckDB stores it as VARCHAR and
    CAST(... AS JSON[]) handles the type coercion without json_parse.
"""
from __future__ import annotations

import io, json, os, subprocess, tarfile
import datetime as _dt
from pathlib import Path
from typing import Any

import yaml  # noqa: F401 – present in the world's venv

ADP_REPO = Path(os.environ.get("FI_ADP_REPO",
                               str(Path.home() / "automotive-data-platform-on-aws")))
ADP_COMMIT = "895f7b61"
_PRODUCT = "platform-foundation/source/data-products/fleet_fault_events"
_PM_OPT = "platform-foundation/source/optional/cms_ingest"

# Trino→DuckDB rules from view_runner.py at ADP 895f7b61 (verbatim subset used here),
# plus two world-local extra rules documented in module docstring.
_RULES: list[tuple[str, str]] = [
    # Extra: pm_operator_completions_view.sql completions unnest
    ("json_parse(p.completions)", "p.completions"),
    # Extra: plan_powertrains/plan_models CTEs and completions CAST in pm_vehicle_status
    ("array(json)", "JSON[]"),
    ("json_parse(sub.lines)", "sub.lines"),
    ("array(row(labor_op varchar))", "STRUCT(labor_op VARCHAR)[]"),
    ("json_extract_scalar(", "json_extract_string("),
    ("from_iso8601_timestamp(sub.updated_at)", "CAST(sub.updated_at AS TIMESTAMPTZ)"),
    ("from_iso8601_timestamp(sub.run_start_ts)", "CAST(sub.run_start_ts AS TIMESTAMPTZ)"),
    # World-local: DMS lines rows in the world have extra fields beyond labor_op.
    # STRUCT(labor_op VARCHAR)[] cast fails on those; use list_apply+json_extract_string instead.
    ("CAST(sub.lines AS STRUCT(labor_op VARCHAR)[])",
     "list_apply(CAST(sub.lines AS JSON[]), x -> {'labor_op': json_extract_string(x, '$.labor_op')})"),
]


def _xfrm(sql: str, stage: str) -> str:
    sql = sql.replace("${stage}", stage)
    for t, r in _RULES:
        sql = sql.replace(t, r)
    return sql


class AdpFaults:
    """Installs ADP fleet-health views; exposes add_fault / add_ro_image."""

    def __init__(self, con: Any, stage: str = "staging",
                 today: _dt.date | None = None) -> None:
        self._con, self._s = con, stage
        self._today = today or _dt.date.today()
        r = subprocess.run(
            ["git", "-C", str(ADP_REPO), "archive", "--format=tar",
             ADP_COMMIT, _PRODUCT + "/"],
            capture_output=True)
        if r.returncode:
            raise RuntimeError(
                f"git archive {ADP_COMMIT} failed: {r.stderr.decode()[:200]}")
        fs: dict[str, bytes] = {}
        with tarfile.open(fileobj=io.BytesIO(r.stdout)) as t:
            for m in t.getmembers():
                if m.isfile() and (f := t.extractfile(m)):
                    fs[m.name.split(_PRODUCT + "/", 1)[-1]] = f.read()
        self._fs = fs
        # pm_operator_completions_view.sql lives outside _PRODUCT; fetch via git show.
        r2 = subprocess.run(
            ["git", "-C", str(ADP_REPO), "show",
             f"{ADP_COMMIT}:{_PM_OPT}/pm_operator_completions_view.sql"],
            capture_output=True)
        if r2.returncode:
            raise RuntimeError(
                f"git show pm_operator_completions_view failed: {r2.stderr.decode()[:200]}")
        self._pm_op_sql = r2.stdout.decode()
        self._setup()

    def _setup(self) -> None:
        s, con = self._s, self._con
        con.execute("SET TimeZone='UTC'")
        for sch in [f"adp_{s}_fleet_health", f"adp_{s}_fleet_ops",
                    f"adp_{s}_cms_ingest", f"adp_{s}_vehicle_identity",
                    f"adp_{s}_service_records"]:
            con.execute(f"CREATE SCHEMA IF NOT EXISTS {sch}")
        # fleet_ops.faults as TABLE — seeded directly via add_fault()
        con.execute(
            f"CREATE TABLE IF NOT EXISTS adp_{s}_fleet_ops.faults "
            "(event_id VARCHAR, vin VARCHAR, code VARCHAR, raised_at DATE, "
            "last_seen_at DATE, cleared_at DATE, clear_reason VARCHAR, "
            "occurrence_count INTEGER, odometer_mi DOUBLE, source VARCHAR, provenance VARCHAR)")
        # fault_codes from yaml
        fc = yaml.safe_load(self._fs["fault_codes.yaml"])
        ver = fc["catalog_version"]
        con.execute(
            f"CREATE OR REPLACE TABLE adp_{s}_fleet_health.fault_codes "
            "(code VARCHAR, band VARCHAR, system VARCHAR, component_id VARCHAR, "
            "directive_text VARCHAR, catalog_version VARCHAR)")
        for c in fc["codes"]:
            con.execute(
                f"INSERT INTO adp_{s}_fleet_health.fault_codes VALUES (?,?,?,?,?,?)",
                [c["code"], c["band"], c.get("system", ""), c.get("component_id"),
                 c.get("directive_text", ""), ver])
        # code_labor_op_map (fault_codes + pm_tasks sections)
        clom = json.loads(self._fs["code_labor_op_map.json"])
        con.execute(
            f"CREATE OR REPLACE TABLE adp_{s}_fleet_health.code_labor_op_map "
            "(code VARCHAR, labor_op VARCHAR)")
        for sect in ("fault_codes", "pm_tasks"):
            for code, ops in (clom.get(sect) or {}).items():
                for op in (ops or []):
                    con.execute(
                        f"INSERT INTO adp_{s}_fleet_health.code_labor_op_map VALUES (?,?)",
                        [code, op])
        # parameters
        p = yaml.safe_load(self._fs["views/parameters.yaml"])
        con.execute(
            f"CREATE OR REPLACE TABLE adp_{s}_fleet_health.parameters "
            "(data_as_of DATE, parameters_version VARCHAR, due_horizon_days INTEGER, "
            "due_horizon_miles INTEGER, open_fault_lookback_days INTEGER)")
        con.execute(
            f"INSERT INTO adp_{s}_fleet_health.parameters VALUES (?,?,?,?,?)",
            [self._today, p["parameters_version"], p["due_horizon_days"],
             p["due_horizon_miles"], p["open_fault_lookback_days"]])
        # vin_cms_fleet and vehicle_identity — empty until load_* called
        con.execute(
            f"CREATE TABLE IF NOT EXISTS adp_{s}_cms_ingest.vin_cms_fleet "
            "(vin VARCHAR, tier VARCHAR, fleet_id VARCHAR)")
        con.execute(
            f"CREATE TABLE IF NOT EXISTS adp_{s}_vehicle_identity.vehicle_identity "
            "(vin VARCHAR, model VARCHAR, model_year INTEGER)")
        # cms_ingest.vehicles — CMS vehicle record (glue_merge_job.py _vehicles_projection
        #   iceberg_columns @ ADP 895f7b61): vehicle_id, vin, fleet_id, model, model_year,
        #   fuel_type; populated by world._load_cms_vehicles().
        con.execute(
            f"CREATE TABLE IF NOT EXISTS adp_{s}_cms_ingest.vehicles "
            "(vehicle_id VARCHAR, vin VARCHAR, fleet_id VARCHAR, model VARCHAR, "
            "model_year INTEGER, fuel_type VARCHAR)")
        # cms_ingest.pm_plans — PM plan definitions (glue_merge_job.py _pm_plans_projection
        #   iceberg_columns @ ADP 895f7b61): fleet_id(PK), plan_id(SK), task_code, basis,
        #   interval_value (DDB attr 'interval' N→INT), applies_to (DDB appliesTo.M raw JSON
        #   map string), vehicle_id (nullable), completions (DDB L raw JSON list string),
        #   start_date (DDB startDate.S); populated by world.seed_pm_plan_to_adp().
        con.execute(
            f"CREATE TABLE IF NOT EXISTS adp_{s}_cms_ingest.pm_plans "
            "(fleet_id VARCHAR, plan_id VARCHAR, task_code VARCHAR, basis VARCHAR, "
            "interval_value INTEGER, applies_to VARCHAR, vehicle_id VARCHAR, "
            "completions VARCHAR, start_date VARCHAR)")
        # fleet_ops.daily_usage — odo proxy (pm_vehicle_status mileage-basis; empty stub)
        con.execute(
            f"CREATE TABLE IF NOT EXISTS adp_{s}_fleet_ops.daily_usage "
            "(vin VARCHAR, miles_driven DOUBLE, usage_date DATE)")
        # service_records.service_records — completion source 1 (pm_vehicle_status; empty stub)
        con.execute(
            f"CREATE TABLE IF NOT EXISTS adp_{s}_service_records.service_records "
            "(vin VARCHAR, service_type VARCHAR, service_date DATE)")
        # pm_operator_completions view (pm_vehicle_status source 3)
        # platform-foundation/source/optional/cms_ingest/pm_operator_completions_view.sql
        # @ ADP 895f7b61 — SELECT only; wrap with CREATE OR REPLACE VIEW.
        con.execute(
            f"CREATE OR REPLACE VIEW adp_{s}_cms_ingest.pm_operator_completions AS "
            + _xfrm(self._pm_op_sql, s))
        # Install the two fault-health view SQL files then pm_vehicle_status
        for rel in ("views/vehicle_fault_status.sql", "views/fault_fleet_summary.sql",
                    "views/pm_vehicle_status.sql"):
            con.execute(_xfrm(self._fs[rel].decode(), s))

    def load_vin_cms_fleet(self, vin_fleet: dict[int, str], vin_fn: Any) -> None:
        s = self._s
        self._con.execute(f"DELETE FROM adp_{s}_cms_ingest.vin_cms_fleet")
        for i, fid in vin_fleet.items():
            self._con.execute(
                f"INSERT INTO adp_{s}_cms_ingest.vin_cms_fleet VALUES (?,?,?)",
                [vin_fn(i), "managed", fid])

    def load_vehicle_identity(self, vin_fleet: dict[int, str], vin_fn: Any) -> None:
        s = self._s
        self._con.execute(f"DELETE FROM adp_{s}_vehicle_identity.vehicle_identity")
        for i in vin_fleet:
            self._con.execute(
                f"INSERT INTO adp_{s}_vehicle_identity.vehicle_identity VALUES (?,?,?)",
                [vin_fn(i), "Azimuth", 2023])

    def add_fault(self, vin: str, code: str, raised_at: _dt.date,
                  last_seen_at: _dt.date, cleared_at: _dt.date | None = None,
                  occurrence_count: int = 1) -> None:
        """Append a row to fleet_ops.faults; cleared_at=None means open."""
        self._con.execute(
            f"INSERT INTO adp_{self._s}_fleet_ops.faults VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            [f"evt-{vin}-{code}", vin, code, raised_at, last_seen_at,
             cleared_at, None, occurrence_count, None, "generated", "world"])

    def add_ro_image(self, ro_id: str, vin: str, status: str, labor_op: str,
                     updated_at: str, seq_no: str = "1") -> None:
        """Append a DMS lake image row to dms_staging.repair_orders."""
        lines = json.dumps([{"labor_op": labor_op}])
        self._con.execute(
            "INSERT INTO dms_staging.repair_orders "
            "(vehicle_vin, ro_id, status, lines, updated_at, _seq_no) VALUES (?,?,?,?,?,?)",
            [vin, ro_id, status, lines, updated_at, seq_no])
