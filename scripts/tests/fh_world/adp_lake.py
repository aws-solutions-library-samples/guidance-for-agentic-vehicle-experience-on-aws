"""adp_lake.py — Install ADP-derived Athena tables into fi_rw_world's FakeAthena.

Uses ADP's real view_runner.py to run vehicle_fault_status.sql and
pm_vehicle_status.sql over seeded DataFrames, then snapshots the results as
*_daily tables.  Also installs fault_codes, code_labor_op_map, managed_vehicles,
snapshot_runs, vehicle_utilisation_daily, and the Agent-2 sources
(energy_usage, tire_health, service_records).

The adp_root must point at the ADP checkout that has fleet_fault_events/
(resolved like test_labor_op_vocabulary.py's _adp_root: AVX_ADP_REPO env or
~/adp-wt-fi-health-d-g6).

No AWS calls; all data stays in DuckDB :memory: and FakeAthena's DuckDB.
"""
from __future__ import annotations

import datetime as _dt
import io
import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pandas as pd

if TYPE_CHECKING:
    from scripts.tests.fi_rw_world.athena import FakeAthena

from scripts.tests.fh_world.seed import (
    CODE_ATTN, CODE_P0, FLEET_A, FLEET_B,
    PM_CODE_BRAKE, PM_CODE_HV, PM_CODE_TIRE, STAGE, TODAY,
    VIN_ATTN_B, VIN_BATTERY_A, VIN_BRAKE_B, VIN_FLEET,
    VIN_P0_A, VIN_PM_CURRENT, VIN_PM_DMS, VIN_PM_DUE, VIN_PM_HV,
    VIN_PM_MILES, VIN_PM_OVERDUE, VIN_PM_SCOPED, VIN_PM_SR,
    VIN_TIRE_A, VIN_TIRE_B,
    VEHICLE_IDS,
)

# ---------------------------------------------------------------------------
# ADP repo resolution (mirrors test_labor_op_vocabulary.py._adp_root)
# ---------------------------------------------------------------------------

def _adp_root() -> Path:
    roots = ([Path(os.environ["AVX_ADP_REPO"])]
             if os.environ.get("AVX_ADP_REPO") else [])
    roots.append(Path.home() / "adp-wt-fi-health-d-g6")
    roots.append(Path.home() / "automotive-data-platform-on-aws")
    found = next((r for r in roots if (r / "platform-foundation").is_dir()), None)
    if found is None:
        raise RuntimeError(
            f"No ADP checkout found at {[str(r) for r in roots]}. "
            "Set AVX_ADP_REPO or clone to ~/adp-wt-fi-health-d-g6."
        )
    return found


def _adp_pkg_path(adp_root: Path) -> Path:
    return adp_root / "platform-foundation/source/data-products/fleet_fault_events"


def _ensure_adp_importable(adp_root: Path) -> None:
    pkg = str(_adp_pkg_path(adp_root))
    if pkg not in sys.path:
        sys.path.insert(0, pkg)


# ---------------------------------------------------------------------------
# Internal: fleet_fault_events raw DataFrames (minimal seeded data)
# ---------------------------------------------------------------------------

def _fleet_ops_faults_df() -> pd.DataFrame:
    """Minimal fleet_ops.faults data: one p0 open, one attention open."""
    return pd.DataFrame([
        {
            "vin": VIN_P0_A, "code": CODE_P0, "fleet_id": FLEET_A,
            "raised_at": _dt.date(2026, 10, 1), "last_seen_at": _dt.date(2026, 10, 1),
            "cleared_at": None, "clear_reason": None, "occurrence_count": 1,
            "odometer_mi": 18000, "source": "generated", "provenance": "generated",
        },
        {
            "vin": VIN_ATTN_B, "code": CODE_ATTN, "fleet_id": FLEET_B,
            "raised_at": _dt.date(2026, 9, 15), "last_seen_at": _dt.date(2026, 9, 15),
            "cleared_at": None, "clear_reason": None, "occurrence_count": 2,
            "odometer_mi": 12000, "source": "generated", "provenance": "generated",
        },
    ])


def _fault_codes_df(adp_root: Path) -> pd.DataFrame:
    """Load ADP's real fault_codes catalog."""
    _ensure_adp_importable(adp_root)
    from fleet_fault_events.fault_codes import load_catalog  # type: ignore[import]
    load_catalog.cache_clear()  # type: ignore[attr-defined]
    return load_catalog()


def _vin_cms_fleet_df() -> pd.DataFrame:
    return pd.DataFrame([
        {"vin": v, "fleet_id": f, "tier": "managed"}
        for v, f in VIN_FLEET.items()
    ])


def _repair_orders_df() -> pd.DataFrame:
    """Empty repair-orders (no faults repaired in this seed)."""
    return pd.DataFrame(columns=[
        "ro_id", "vehicle_vin", "_seq_no", "status", "updated_at", "lines",
        "record_kind", "opened_at", "created_at",
    ])


def _code_labor_op_map(adp_root: Path) -> dict[str, list[str]]:
    """Load ADP's real code→labor_op map from code_labor_op_map.json.

    JSON shape: {fault_codes: {code: [lop,...]}, pm_tasks: {code: [lop,...]}, ...}
    """
    import json
    fpath = _adp_pkg_path(adp_root) / "code_labor_op_map.json"
    raw = json.loads(fpath.read_text())
    result: dict[str, list[str]] = {}
    for section_key in ("fault_codes", "pm_tasks", "pm_task_codes"):
        section = raw.get(section_key, {})
        for code, lops in section.items():
            if isinstance(lops, list):
                result[code] = lops
    return result


def _pm_plans_df() -> pd.DataFrame:
    """PM plans covering all item-4 cases:
      PLN-001 TIRE_ROTATION  calendar overdue   VIN_PM_OVERDUE  (operator completion 2026-03-01)
      PLN-002 BRAKE_INSPECTION calendar due     VIN_PM_DUE      (operator completion 2026-04-27)
      PLN-003 TIRE_ROTATION  calendar no_history VIN_PM_CURRENT (no completions)
      PLN-004 TIRE_ROTATION  calendar current   VIN_PM_SR       (completion from service_records)
      PLN-005 BRAKE_INSPECTION calendar current VIN_PM_DMS      (completion from DMS line)
      PLN-006 TIRE_ROTATION  mileage due        VIN_PM_MILES    (operator completion 2026-08-15)
      PLN-007 HV_BATTERY_CHECK calendar no_history VIN_PM_HV   (fault DMS op -- ignored)
    """
    import json

    def _scope() -> str:
        return json.dumps({"powertrains": None, "models": None,
                           "modelYearMin": None, "modelYearMax": None})

    def _comp(vin: str, completed_on: str, recorded_at: str = "2026-09-01T00:00:00+00:00") -> str:
        """DDB JSON list wrapping one completion (matches pm_operator_completions_view.sql)."""
        return json.dumps([{"M": {
            "vehicleId": {"S": vin},
            "vin": {"S": vin},
            "completedOn": {"S": completed_on},
            "operatorSub": {"S": "seed"},
            "source": {"S": "operator"},
            "recordedAt": {"S": recorded_at},
        }}])

    def _scoped_applies_to(powertrains: list, models: list,
                           year_min: int, year_max: int) -> str:
        """DDB JSON format for pm_vehicle_status.sql plan_candidates CTE."""
        return json.dumps({
            "powertrains": {"L": [{"S": pt} for pt in powertrains]},
            "models": {"L": [{"S": m} for m in models]},
            "modelYearMin": {"N": str(year_min)},
            "modelYearMax": {"N": str(year_max)},
        })

    def _row(plan_id: str, fleet_id: str, vin: str, task_code: str,
             basis: str, interval: int, start: str, completions: str) -> dict:
        return {
            "fleet_id": fleet_id, "plan_id": plan_id,
            "task_code": task_code, "basis": basis, "interval_value": interval,
            "applies_to": _scope(),
            "vehicle_id": VEHICLE_IDS.get(vin, vin),
            "completions": completions,
            "created_by_sub": "seed", "updated_at": "2026-10-01T00:00:00+00:00",
            "start_date": start, "provenance": "seed",
            "event_time": pd.Timestamp("2026-10-01T00:00:00+00:00"),
            "_change_kind": "INSERT", "_seq_no": "1", "raw_image": None,
        }

    def _scoped_row(plan_id: str, fleet_id: str, applies_to_json: str,
                    task_code: str, basis: str, interval: int, start: str,
                    completions: str) -> dict:
        """Scoped plan row: vehicle_id is NULL (None); applies_to holds DDB scope.

        None → NaN in pandas; _install_pm_plans_raw._vid converts NaN→SQL NULL
        so FakeAthena also sees IS NULL = True for the scoped-plan branch.
        ADP's run_pm_vehicle_status inserts None→SQL NULL directly into DuckDB.
        """
        return {
            "fleet_id": fleet_id, "plan_id": plan_id,
            "task_code": task_code, "basis": basis, "interval_value": interval,
            "applies_to": applies_to_json,
            "vehicle_id": None,
            "completions": completions,
            "created_by_sub": "seed", "updated_at": "2026-10-01T00:00:00+00:00",
            "start_date": start, "provenance": "seed",
            "event_time": pd.Timestamp("2026-10-01T00:00:00+00:00"),
            "_change_kind": "INSERT", "_seq_no": "1", "raw_image": None,
        }

    rows = [
        # overdue: last completion 2026-03-01, 180d -> next_due 2026-08-28 < TODAY
        _row("PLN-001", FLEET_A, VIN_PM_OVERDUE, PM_CODE_TIRE,
             "calendar", 180, "2026-03-01", _comp(VIN_PM_OVERDUE, "2026-03-01")),
        # due: last completion 2026-04-27, 180d -> next_due 2026-10-24 (within 30d)
        _row("PLN-002", FLEET_A, VIN_PM_DUE, PM_CODE_BRAKE,
             "calendar", 180, "2026-04-27", _comp(VIN_PM_DUE, "2026-04-27")),
        # no_history: no completions (VIN_PM_CURRENT)
        _row("PLN-003", FLEET_B, VIN_PM_CURRENT, PM_CODE_TIRE,
             "calendar", 180, "2026-07-10", "[]"),
        # current via service_records: no plan completions (source comes from sr table)
        _row("PLN-004", FLEET_A, VIN_PM_SR, PM_CODE_TIRE,
             "calendar", 180, "2026-09-01", "[]"),
        # current via DMS line: no plan completions (source comes from repair_orders)
        _row("PLN-005", FLEET_B, VIN_PM_DMS, PM_CODE_BRAKE,
             "calendar", 180, "2026-09-15", "[]"),
        # mileage due: operator completion 2026-08-15 at 9999 miles; total 14499 miles
        _row("PLN-006", FLEET_A, VIN_PM_MILES, PM_CODE_TIRE,
             "mileage", 5000, "2026-08-15", _comp(VIN_PM_MILES, "2026-08-15")),
        # HV_BATTERY_CHECK no_history: DMS has LAB-HVISO-01 (fault op) -- not a PM completion
        _row("PLN-007", FLEET_B, VIN_PM_HV, PM_CODE_HV,
             "calendar", 365, "2026-01-01", "[]"),
        # W3 scoped: EV/Azimuth/2023 fleet-A scope; completion 2026-04-22 + 180d = TODAY.
        # next_due == data_as_of → "due" (overdue boundary is strict <; due is <=).
        # Covers VIN_PM_SCOPED (no single-vehicle plan) and also VIN_PM_SR (has PLN-004 →
        # single-vehicle wins by precedence: is_single_vehicle DESC, plan_id ASC).
        _scoped_row("PLN-008", FLEET_A,
                    _scoped_applies_to(["ev"], ["Azimuth"], 2023, 2023),
                    PM_CODE_TIRE, "calendar", 180, "2026-04-22",
                    _comp(VIN_PM_SCOPED, "2026-04-22")),
    ]
    return pd.DataFrame(rows)


def _cms_vehicles_df() -> pd.DataFrame:
    """One CMS vehicle row per PM VIN (exact cms_ingest.vehicles shape)."""
    rows = []
    pm_vins = [
        (VIN_PM_OVERDUE, FLEET_A), (VIN_PM_DUE, FLEET_A),
        (VIN_PM_CURRENT, FLEET_B), (VIN_PM_SR, FLEET_A),
        (VIN_PM_DMS, FLEET_B), (VIN_PM_MILES, FLEET_A),
        (VIN_PM_HV, FLEET_B), (VIN_PM_SCOPED, FLEET_A),
    ]
    for v, fleet in pm_vins:
        rows.append({
            "vehicle_id": VEHICLE_IDS.get(v, v), "vin": v, "fleet_id": fleet,
            "make": "Meridian", "model": "Azimuth", "model_year": 2023,
            "fuel_type": "ev", "vehicle_type": "car",
            "odometer": 15000.0, "purchase_date": "2023-01-01",
            "purchase_price": 45000.0, "producer": "fleet",
            "status": "active", "location_id": None, "location_assigned_at": None,
            "provenance": "seed",
            "event_time": pd.Timestamp("2026-10-01T00:00:00+00:00"),
            "_change_kind": "INSERT", "_seq_no": "1", "raw_image": None,
        })
    return pd.DataFrame(rows)


def _service_records_df() -> pd.DataFrame:
    """service_records rows for PM and Agent-2 use:
      VIN_BRAKE_B: brake_service open (Agent-2 source)
      VIN_PM_SR:   tire_service completed 2026-09-01 (item-4 PM completion source)
    """
    return pd.DataFrame([
        {
            "service_id": "SVC-001", "vin": VIN_BRAKE_B,
            "service_date": _dt.date(2026, 10, 5),
            "service_type": "brake_service",
            "dtc_codes": [],
            "outcome": "parts_pending",
            "linked_campaign_id": None,
        },
        {
            "service_id": "SVC-004", "vin": VIN_PM_SR,
            "service_date": _dt.date(2026, 9, 1),
            "service_type": "tire_service",
            "dtc_codes": [],
            "outcome": "completed",
            "linked_campaign_id": None,
        },
    ])


def _repair_orders_df() -> pd.DataFrame:
    """DMS repair orders for PM completions (DMS lake shape; lines as JSON VARCHAR).
      VIN_PM_DMS: BRAKE_INSPECTION completion via LAB-BRK-01 (Invoiced 2026-09-15)
      VIN_PM_HV:  HV_BATTERY_CHECK fault repair via LAB-HVISO-01 -- must NOT count as PM
    """
    import json

    def _ro(ro_id: str, vin: str, labor_op: str, status: str, updated_at: str) -> dict:
        return {
            "ro_id": ro_id, "vehicle_vin": vin,
            "_seq_no": "1", "status": status,
            "updated_at": updated_at,
            # lines must contain ONLY labor_op; DuckDB CAST to STRUCT(labor_op VARCHAR)[]
            # fails if extra fields are present (view_runner Rule 2 translation).
            "lines": json.dumps([{"labor_op": labor_op}]),
            "record_kind": "repair_order",
            "opened_at": updated_at, "created_at": updated_at,
        }

    return pd.DataFrame([
        _ro("RO-010", VIN_PM_DMS, "LAB-BRK-01", "Invoiced", "2026-09-15T00:00:00+00:00"),
        _ro("RO-012", VIN_PM_HV,  "LAB-HVISO-01", "Invoiced", "2026-10-01T00:00:00+00:00"),
    ])


def _daily_usage_df() -> pd.DataFrame:
    """daily_usage rows for all managed VINs.

    Generic 30 miles/day for most VINs (calendar plans ignore odometer).
    VIN_PM_MILES (mileage plan): two rows chosen so:
      - sum before completion (2026-08-15): 9999 miles  -> last_odo = 9999
      - total sum: 14499 miles -> cur_odo = 14499
      - ndm = 9999 + 5000 = 14999; miles_left = 500 -> "due"
      Integer values avoid DuckDB-vs-Trino CAST discrepancy in the world.
    """
    # Seed a full year (365 rows) for every non-mileage VIN so the daily_usage
    # table exceeds 1,000 rows when all VINs are queried together (W8 / WM6).
    # VIN_PM_MILES rows are appended last; with 12 * 365 = 4,380 preceding rows
    # they land on page 5, beyond page 1's 999-row limit.  WM6 (_athena_run_paged
    # breaks after page 1) therefore never sees VIN_PM_MILES, yields odo_total=0,
    # and the mileage plan flips from "due" to "current" → 4-view-match FAILS.
    rows = []
    _year_start = _dt.date(2025, 10, 20)   # TODAY - 364 d; 365 rows/VIN
    for v in VIN_FLEET:
        if v != VIN_PM_MILES:
            for n in range(365):
                rows.append({"vin": v,
                             "usage_date": _year_start + _dt.timedelta(days=n),
                             "miles_driven": 30.0})
    # Mileage-specific rows for VIN_PM_MILES
    # Fractional miles exercise _cast_bigint at e2e (DOUBLE→BIGINT path).
    # Fractional part < 0.5: DuckDB truncates and _cast_bigint agree (see DESIGN.md § cast).
    # sum_before_completion=9999.3 → CAST→9999; total=14499.3 → CAST→14499.
    # ndm=9999+5000=14999; cur_odo=14499; miles_left=500 → "due".
    rows.append({"vin": VIN_PM_MILES, "usage_date": _dt.date(2026, 8, 15),
                 "miles_driven": 9999.3})   # before completion; frac < 0.5 → CAST 9999
    rows.append({"vin": VIN_PM_MILES, "usage_date": _dt.date(2026, 10, 1),
                 "miles_driven": 4500.0})   # after completion; total = 14499.3
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# Internal: register snapshot_runs marker row in FakeAthena
# ---------------------------------------------------------------------------

def _snapshot_runs_rows(today: _dt.date) -> list[dict]:
    keys = [
        "vehicle_fault_status_daily",
        "pm_vehicle_status_daily",
        "vehicle_utilisation_daily",
    ]
    return [
        {"table_key": k, "snapshot_date": today,
         "row_count": 1, "parameters_version": "1.0.0"}
        for k in keys
    ]


# ---------------------------------------------------------------------------
# run_vehicle_fault_status via ADP view_runner
# ---------------------------------------------------------------------------

def _build_vfs_df(adp_root: Path, today: _dt.date) -> pd.DataFrame:
    """Run ADP's vehicle_fault_status view over seeded data; return DataFrame."""
    _ensure_adp_importable(adp_root)
    from fleet_fault_events.view_runner import run_vehicle_fault_status  # type: ignore
    faults_df  = _fleet_ops_faults_df()
    fc_df      = _fault_codes_df(adp_root)
    vcf_df     = _vin_cms_fleet_df()
    ro_df      = _repair_orders_df()
    clom       = _code_labor_op_map(adp_root)
    sr_df      = _service_records_df()
    return run_vehicle_fault_status(
        fleet_ops_faults=faults_df,
        fault_codes=fc_df,
        vin_cms_fleet=vcf_df,
        repair_orders=ro_df,
        code_labor_op_map=clom,
        data_as_of=today,
        service_records=sr_df,
    )


def _build_pm_df(adp_root: Path, today: _dt.date) -> pd.DataFrame:
    """Run ADP's pm_vehicle_status view over seeded data; return DataFrame."""
    _ensure_adp_importable(adp_root)
    from fleet_fault_events.view_runner import run_pm_vehicle_status  # type: ignore
    clom = _code_labor_op_map(adp_root)
    return run_pm_vehicle_status(
        pm_plans=_pm_plans_df(),
        cms_vehicles=_cms_vehicles_df(),
        service_records=_service_records_df(),
        repair_orders=_repair_orders_df(),
        vin_cms_fleet=_vin_cms_fleet_df(),
        daily_usage=_daily_usage_df(),
        code_labor_op_map=clom,
        data_as_of=today,
    )


# ---------------------------------------------------------------------------
# Internal: FakeAthena table registrations
# ---------------------------------------------------------------------------

def _install_vehicle_fault_status(
    athena: "FakeAthena", vfs_df: pd.DataFrame, today: _dt.date
) -> None:
    snap = vfs_df.copy()
    snap["snapshot_date"] = today
    cols = [
        ("vin", "string"), ("code", "string"), ("fleet_id", "string"),
        ("first_seen", "date"), ("last_seen", "date"), ("occurrences", "bigint"),
        ("severity_band", "string"), ("system", "string"), ("component_id", "string"),
        ("status", "string"), ("repair_ref", "string"), ("repaired_on", "date"),
        ("catalog_version", "string"), ("data_as_of", "date"),
        ("snapshot_date", "date"),
    ]
    rows_out = []
    for _, row in snap.iterrows():
        d: dict[str, Any] = {}
        for col, _ in cols:
            v = row.get(col)
            if hasattr(v, "item"):
                v = v.item()
            if hasattr(v, "date") and not isinstance(v, _dt.date):
                v = v.date()
            d[col] = v
        rows_out.append(d)
    athena.create_table(
        f"adp_{STAGE}_fleet_health.vehicle_fault_status_daily", cols, rows_out
    )


def _install_pm_vehicle_status(
    athena: "FakeAthena", pm_df: pd.DataFrame, today: _dt.date
) -> None:
    """Install pm_vehicle_status_daily AND pm_vehicle_status (live view) tables.

    Both are derived from the same pm_df; _daily adds snapshot_date.
    Columns include last_done_date and last_done_source (present in ADP view output
    and queried by item 4's SELECT on both tables).
    """
    snap = pm_df.copy()
    snap["snapshot_date"] = today
    # Full set of columns that item 4 queries from pm_vehicle_status
    live_cols = [
        ("vin", "string"), ("fleet_id", "string"), ("task_code", "string"),
        ("basis", "string"), ("interval_value", "int"),
        ("last_done_date", "date"), ("last_done_source", "string"),
        ("next_due_date", "date"), ("next_due_miles", "bigint"),
        ("status", "string"), ("data_as_of", "date"),
    ]
    daily_cols = live_cols + [("snapshot_date", "date")]

    def _to_rows(df: pd.DataFrame, cols: list[tuple[str, str]]) -> list[dict]:
        out: list[dict[str, Any]] = []
        for _, row in df.iterrows():
            d: dict[str, Any] = {}
            for col, _ in cols:
                v = row.get(col)
                if hasattr(v, "item"):
                    v = v.item()
                if hasattr(v, "date") and not isinstance(v, _dt.date):
                    try:
                        v = v.date()
                    except Exception:
                        pass
                # NaT / NaN -> None
                try:
                    import pandas as _pd
                    if _pd.isna(v):
                        v = None
                except (TypeError, ValueError):
                    pass
                d[col] = v
            out.append(d)
        return out

    athena.create_table(
        f"adp_{STAGE}_fleet_health.pm_vehicle_status", live_cols, _to_rows(pm_df, live_cols)
    )
    athena.create_table(
        f"adp_{STAGE}_fleet_health.pm_vehicle_status_daily", daily_cols, _to_rows(snap, daily_cols)
    )


def _install_view_tables_only(
    athena: "FakeAthena",
    vfs_df: pd.DataFrame,
    pm_df: pd.DataFrame,
    today: _dt.date,
) -> None:
    """Install only the live VIEW tables (not *_daily snapshots) from fresh DataFrames.

    Called by FhWorld._refresh_lake to mirror ADP's view-at-query-time semantics.
    vehicle_fault_status and pm_vehicle_status are both views in live ADP;
    only the *_daily tables are historical snapshots.  This helper refreshes
    the view tables without touching the dailies installed at open().
    """
    # vehicle_fault_status view (no snapshot_date)
    vfs_view_cols = [
        ("vin", "string"), ("code", "string"), ("fleet_id", "string"),
        ("first_seen", "date"), ("last_seen", "date"), ("occurrences", "bigint"),
        ("severity_band", "string"), ("system", "string"), ("component_id", "string"),
        ("status", "string"), ("repair_ref", "string"), ("repaired_on", "date"),
        ("catalog_version", "string"), ("data_as_of", "date"),
    ]

    def _row_for(df: pd.DataFrame, col_spec: list[tuple[str, str]]) -> list[dict]:
        out: list[dict] = []
        for _, row in df.iterrows():
            d: dict[str, Any] = {}
            for col, _ in col_spec:
                v = row.get(col)
                if hasattr(v, "item"):
                    v = v.item()
                if hasattr(v, "date") and not isinstance(v, _dt.date):
                    try:
                        v = v.date()
                    except Exception:  # noqa: BLE001
                        pass
                try:
                    import pandas as _pd2
                    if _pd2.isna(v):
                        v = None
                except (TypeError, ValueError):
                    pass
                d[col] = v
            out.append(d)
        return out

    athena.create_table(
        f"adp_{STAGE}_fleet_health.vehicle_fault_status",
        vfs_view_cols, _row_for(vfs_df, vfs_view_cols),
    )
    # pm_vehicle_status view (no snapshot_date)
    pm_view_cols = [
        ("vin", "string"), ("fleet_id", "string"), ("task_code", "string"),
        ("basis", "string"), ("interval_value", "int"),
        ("last_done_date", "date"), ("last_done_source", "string"),
        ("next_due_date", "date"), ("next_due_miles", "bigint"),
        ("status", "string"), ("data_as_of", "date"),
    ]
    athena.create_table(
        f"adp_{STAGE}_fleet_health.pm_vehicle_status",
        pm_view_cols, _row_for(pm_df, pm_view_cols),
    )


def _install_fault_codes(athena: "FakeAthena", fc_df: pd.DataFrame) -> None:
    cols = [
        ("code", "string"), ("description", "string"), ("system", "string"),
        ("component_id", "string"), ("band", "string"),
        ("directive_text", "string"), ("catalog_version", "string"),
    ]
    rows = fc_df[["code", "description", "system", "component_id",
                  "band", "directive_text", "catalog_version"]].to_dict("records")
    athena.create_table(f"adp_{STAGE}_fleet_health.fault_codes", cols, rows)


def _install_code_labor_op_map(
    athena: "FakeAthena", clom: dict[str, list[str]]
) -> None:
    cols = [("code", "string"), ("labor_op", "string")]
    rows = [{"code": c, "labor_op": lop}
            for c, lops in clom.items() for lop in lops]
    athena.create_table(f"adp_{STAGE}_fleet_health.code_labor_op_map", cols, rows)


def _install_managed_vehicles(athena: "FakeAthena", today: _dt.date) -> None:
    cols = [("vin", "string"), ("fleet_id", "string"), ("snapshot_date", "date")]
    rows = [{"vin": v, "fleet_id": f, "snapshot_date": today}
            for v, f in VIN_FLEET.items()]
    athena.create_table(f"adp_{STAGE}_fleet_recall.managed_vehicles", cols, rows)


def _install_snapshot_runs(athena: "FakeAthena", today: _dt.date) -> None:
    cols = [
        ("table_key", "string"), ("snapshot_date", "date"),
        ("row_count", "bigint"), ("parameters_version", "string"),
    ]
    rows = _snapshot_runs_rows(today)
    athena.create_table(f"adp_{STAGE}_fleet_health.snapshot_runs", cols, rows)


def _install_vehicle_utilisation(athena: "FakeAthena", today: _dt.date) -> None:
    cols = [("vin", "string"), ("avg_miles_30d", "double"), ("snapshot_date", "date")]
    rows = [{"vin": v, "avg_miles_30d": 32.5, "snapshot_date": today}
            for v in VIN_FLEET]
    athena.create_table(
        f"adp_{STAGE}_fleet_health.vehicle_utilisation_daily", cols, rows
    )


def _install_energy_usage(athena: "FakeAthena", today: _dt.date) -> None:
    """Agent-2 battery source: energy_usage with VIN_BATTERY_A."""
    cols = [
        ("vin", "string"), ("state_of_health_pct", "double"),
        ("battery_age_days", "int"), ("usage_date", "date"),
    ]
    rows = [{"vin": VIN_BATTERY_A, "state_of_health_pct": 87.5,
             "battery_age_days": 730, "usage_date": today}]
    athena.create_table(f"adp_{STAGE}_energy_usage.energy_usage", cols, rows)


def _install_tire_health(athena: "FakeAthena", today: _dt.date) -> None:
    """Agent-2 tire source: tire_health with VIN_TIRE_A needs_replacement=true."""
    cols = [
        ("vin", "string"), ("tire_position", "string"),
        ("needs_replacement", "boolean"), ("wear_category", "string"),
        ("wear_rate_mm_per_1k_km", "double"), ("event_date", "date"),
    ]
    rows = [
        {"vin": VIN_TIRE_A, "tire_position": "FL", "needs_replacement": True,
         "wear_category": "worn", "wear_rate_mm_per_1k_km": 0.42, "event_date": today},
        # W5: second pool VIN so tires pool has 2 managed VINs.
        {"vin": VIN_TIRE_B, "tire_position": "FL", "needs_replacement": True,
         "wear_category": "worn", "wear_rate_mm_per_1k_km": 0.38, "event_date": today},
    ]
    athena.create_table(f"adp_{STAGE}_tire_health.tire_health", cols, rows)


def _install_service_records_agent2(athena: "FakeAthena", today: _dt.date) -> None:
    """service_records Athena table: Agent-2 brake source + PM tire completion.

    Columns match adp_brake_source._BATCH_SQL_TEMPLATE:
    vin, service_date, dtc_codes (array<string>), complaint_text,
    warranty_covered (boolean), total_cost_usd (double), outcome.
    Item 4 queries: vin, service_type, service_date.
    """
    cols = [
        ("vin", "string"), ("service_type", "string"),
        ("outcome", "string"), ("service_date", "date"),
        ("service_id", "string"), ("dtc_codes", "array<string>"),
        ("complaint_text", "string"), ("warranty_covered", "boolean"),
        ("total_cost_usd", "double"),
    ]
    rows = [
        {
            "vin": VIN_BRAKE_B, "service_type": "brake_service",
            "outcome": "parts_pending", "service_date": today,
            "service_id": "SVC-002", "dtc_codes": [],
            "complaint_text": "brakes", "warranty_covered": False,
            "total_cost_usd": 0.0,
        },
        # PM completion source for VIN_PM_SR (item 4: TIRE_ROTATION via tire_service)
        {
            "vin": VIN_PM_SR, "service_type": "tire_service",
            "outcome": "completed", "service_date": _dt.date(2026, 9, 1),
            "service_id": "SVC-004", "dtc_codes": [],
            "complaint_text": "tire rotation", "warranty_covered": False,
            "total_cost_usd": 0.0,
        },
    ]
    athena.create_table(f"adp_{STAGE}_service_records.service_records", cols, rows)


def _install_vehicle_identity(athena: "FakeAthena", today: _dt.date) -> None:
    """vehicle_identity table: vin -> model_year (LEFT JOINed by brakes source)."""
    cols = [("vin", "string"), ("model_year", "int")]
    rows = [{"vin": v, "model_year": 2023} for v in VIN_FLEET]
    athena.create_table(f"adp_{STAGE}_vehicle_identity.vehicle_identity", cols, rows)


# ---------------------------------------------------------------------------
# Item-4 specific Athena table installers (raw input tables for the oracle)
# ---------------------------------------------------------------------------

def _install_pm_plans_raw(athena: "FakeAthena", pm_plans_df: pd.DataFrame) -> None:
    """adp_{stage}_cms_ingest.pm_plans - flat Athena table queried by item 4.

    Columns: plan_id, fleet_id, vehicle_id, applies_to, task_code, basis,
             interval_value, start_date  (all strings; Athena returns strings).
    """
    cols = [
        ("plan_id", "string"), ("fleet_id", "string"), ("vehicle_id", "string"),
        ("applies_to", "string"), ("task_code", "string"), ("basis", "string"),
        ("interval_value", "string"), ("start_date", "string"),
    ]
    import pandas as _pd
    def _vid(raw: Any) -> Any:
        """Return SQL NULL for empty/NaN vehicle_id (scoped plan); str otherwise."""
        if raw is None:
            return None
        try:
            if _pd.isna(raw):
                return None
        except (TypeError, ValueError):
            pass
        s = str(raw).strip()
        return None if s in ("", "nan", "None") else s

    rows = [
        {
            "plan_id": str(r.get("plan_id") or ""),
            "fleet_id": str(r.get("fleet_id") or ""),
            "vehicle_id": _vid(r.get("vehicle_id")),
            "applies_to": str(r.get("applies_to") or ""),
            "task_code": str(r.get("task_code") or ""),
            "basis": str(r.get("basis") or ""),
            "interval_value": str(r.get("interval_value") or ""),
            "start_date": str(r.get("start_date") or ""),
        }
        for _, r in pm_plans_df.iterrows()
    ]
    athena.create_table(f"adp_{STAGE}_cms_ingest.pm_plans", cols, rows)


def _install_cms_vehicles_raw(athena: "FakeAthena", cms_veh_df: pd.DataFrame) -> None:
    """adp_{stage}_cms_ingest.vehicles - item 4 queries vehicle_id, vin, model, model_year, fuel_type."""
    cols = [
        ("vehicle_id", "string"), ("vin", "string"), ("model", "string"),
        ("model_year", "string"), ("fuel_type", "string"),
    ]
    rows = [
        {
            "vehicle_id": str(r.get("vehicle_id") or ""),
            "vin": str(r.get("vin") or ""),
            "model": str(r.get("model") or ""),
            "model_year": str(r.get("model_year") or ""),
            "fuel_type": str(r.get("fuel_type") or ""),
        }
        for _, r in cms_veh_df.iterrows()
    ]
    athena.create_table(f"adp_{STAGE}_cms_ingest.vehicles", cols, rows)


def _install_pm_operator_completions(
    athena: "FakeAthena", pm_plans_df: pd.DataFrame
) -> None:
    """adp_{stage}_cms_ingest.pm_operator_completions extracted from plan.completions JSON.

    Also includes VIN_PM_MILES operator completion (from plan PLN-006).
    Columns: vin, task_code, completed_on.
    """
    import json
    cols = [("vin", "string"), ("task_code", "string"), ("completed_on", "string")]
    rows: list[dict[str, Any]] = []
    for _, plan in pm_plans_df.iterrows():
        raw = plan.get("completions") or "[]"
        try:
            comps = json.loads(raw) if isinstance(raw, str) else (raw or [])
        except Exception:  # noqa: BLE001
            comps = []
        task_code = str(plan.get("task_code") or "")
        for entry in comps:
            m = entry.get("M") if isinstance(entry, dict) else None
            if not m:
                continue
            vin_av = m.get("vin") or m.get("vehicleId") or {}
            completed_av = m.get("completedOn") or {}
            vin_val = vin_av.get("S") if isinstance(vin_av, dict) else None
            comp_val = completed_av.get("S") if isinstance(completed_av, dict) else None
            if vin_val and comp_val and task_code:
                rows.append({"vin": vin_val, "task_code": task_code,
                             "completed_on": comp_val[:10]})
    athena.create_table(f"adp_{STAGE}_cms_ingest.pm_operator_completions", cols, rows)


def _install_daily_usage(athena: "FakeAthena", du_df: pd.DataFrame) -> None:
    """adp_{stage}_fleet_ops.daily_usage - vin, usage_date, miles_driven."""
    cols = [("vin", "string"), ("usage_date", "date"), ("miles_driven", "double")]
    rows: list[dict[str, Any]] = []
    for _, r in du_df.iterrows():
        ud = r.get("usage_date")
        if hasattr(ud, "date"):
            ud = ud.date()
        rows.append({"vin": str(r.get("vin") or ""), "usage_date": ud,
                     "miles_driven": float(r.get("miles_driven") or 0)})
    athena.create_table(f"adp_{STAGE}_fleet_ops.daily_usage", cols, rows)


def _install_parameters(athena: "FakeAthena", today: _dt.date) -> None:
    """adp_{stage}_fleet_health.parameters - due_horizon_days, due_horizon_miles, data_as_of."""
    cols = [("due_horizon_days", "string"), ("due_horizon_miles", "string"),
            ("data_as_of", "string")]
    rows = [{"due_horizon_days": "30", "due_horizon_miles": "500",
             "data_as_of": today.isoformat()}]
    athena.create_table(f"adp_{STAGE}_fleet_health.parameters", cols, rows)


def _insert_pm_repair_orders(
    athena: "FakeAthena", ro_df: pd.DataFrame
) -> None:
    """INSERT PM-specific repair order rows into the existing dms_{stage}.repair_orders table.

    Uses direct DuckDB INSERT to avoid replacing existing DMS service lane data.
    Only fills the columns item 4 queries (ro_id, vehicle_vin, status, lines, updated_at,
    _seq_no); all others default to NULL.
    No-op when the table does not exist (standalone adp_lake fixture).
    """
    if ro_df.empty:
        return
    s = STAGE
    # Guard: only insert if the table already exists (fi_rw_world context)
    try:
        athena.con.execute(f"SELECT 1 FROM dms_{s}.repair_orders LIMIT 0")
    except Exception:  # noqa: BLE001
        return
    for _, r in ro_df.iterrows():
        athena.con.execute(
            f"INSERT INTO dms_{s}.repair_orders "
            f"(ro_id, vehicle_vin, status, updated_at, \"_seq_no\", lines) "
            f"VALUES (?, ?, ?, ?, ?, ?)",
            [
                str(r.get("ro_id") or ""),
                str(r.get("vehicle_vin") or ""),
                str(r.get("status") or ""),
                str(r.get("updated_at") or ""),
                str(r.get("_seq_no") or ""),
                str(r.get("lines") or "[]"),
            ],
        )


# ---------------------------------------------------------------------------
# Config parquet bytes (returned for moto S3 upload by world.py)
# ---------------------------------------------------------------------------

def config_parquet_bytes(adp_root: Path, today: _dt.date) -> dict[str, bytes]:
    """Return {s3_key: bytes} for the two config Parquet objects.

    Keys:
      config/fleet_health/fault_codes/fault_codes.parquet
      config/fleet_health/code_labor_op_map/code_labor_op_map.parquet
    """
    _ensure_adp_importable(adp_root)
    from fleet_fault_events.fault_codes import fault_codes_table  # type: ignore
    import pyarrow.parquet as pq  # type: ignore

    # fault_codes
    fc_buf = io.BytesIO()
    pq.write_table(fault_codes_table(), fc_buf, compression="snappy")
    fc_bytes = fc_buf.getvalue()

    # code_labor_op_map (use daily.write_config_tables indirectly)
    import json
    fpath = (_adp_pkg_path(adp_root) / "code_labor_op_map.json")
    raw = json.loads(fpath.read_text())
    clom: dict[str, list[str]] = {}
    for section_key in ("fault_codes", "pm_tasks", "pm_task_codes"):
        section = raw.get(section_key, {})
        for code, lops in section.items():
            if isinstance(lops, list):
                clom[code] = lops

    import pyarrow as pa  # type: ignore
    rows = [(c, lop) for c, lops in clom.items() for lop in lops]
    clom_tbl = pa.table(
        {"code": pa.array([c for c, _ in rows], type=pa.string()),
         "labor_op": pa.array([lop for _, lop in rows], type=pa.string())}
    )
    clom_buf = io.BytesIO()
    pq.write_table(clom_tbl, clom_buf, compression="snappy")
    clom_bytes = clom_buf.getvalue()

    return {
        "config/fleet_health/fault_codes/fault_codes.parquet": fc_bytes,
        "config/fleet_health/code_labor_op_map/code_labor_op_map.parquet": clom_bytes,
    }


# ---------------------------------------------------------------------------
# Public: install_fleet_health_tables
# ---------------------------------------------------------------------------

def install_fleet_health_tables(
    athena: "FakeAthena",
    today: _dt.date = TODAY,
    adp_root: Path | None = None,
) -> None:
    """Install all ADP-derived Athena tables into *athena* for the given *today*.

    Calls ADP's real view_runner to derive vehicle_fault_status and
    pm_vehicle_status from seeded DataFrames, then registers results as
    *_daily snapshots together with all supporting tables.
    Also installs item-4 raw input tables (pm_plans, vehicles,
    pm_operator_completions, daily_usage, parameters) and inserts PM
    repair orders into the existing dms_{stage}.repair_orders table.
    """
    if adp_root is None:
        adp_root = _adp_root()

    fc_df       = _fault_codes_df(adp_root)
    clom        = _code_labor_op_map(adp_root)
    vfs_df      = _build_vfs_df(adp_root, today)
    pm_plans_df = _pm_plans_df()
    cms_veh_df  = _cms_vehicles_df()
    sr_df       = _service_records_df()
    ro_df       = _repair_orders_df()
    du_df       = _daily_usage_df()
    pm_df       = _build_pm_df(adp_root, today)

    _install_fault_codes(athena, fc_df)
    _install_code_labor_op_map(athena, clom)
    _install_managed_vehicles(athena, today)
    _install_snapshot_runs(athena, today)
    _install_vehicle_fault_status(athena, vfs_df, today)
    _install_pm_vehicle_status(athena, pm_df, today)
    _install_vehicle_utilisation(athena, today)
    _install_energy_usage(athena, today)
    _install_tire_health(athena, today)
    _install_service_records_agent2(athena, today)
    _install_vehicle_identity(athena, today)
    # Item-4 raw input tables
    _install_pm_plans_raw(athena, pm_plans_df)
    _install_cms_vehicles_raw(athena, cms_veh_df)
    _install_pm_operator_completions(athena, pm_plans_df)
    _install_daily_usage(athena, du_df)
    _install_parameters(athena, today)
    _insert_pm_repair_orders(athena, ro_df)
