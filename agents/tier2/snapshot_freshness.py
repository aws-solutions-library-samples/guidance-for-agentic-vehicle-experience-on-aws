"""snapshot_freshness.py — staleness bound on stage D snapshot reads."""
from __future__ import annotations

import datetime
from dataclasses import dataclass
from typing import Any, Callable

from agents.tier2.adp_source import run_query

# Keys backed by ADP snapshot_runs markers (have row_count + marker date).
ADP_KEYS = frozenset({
    "vehicle_fault_status_daily",
    "pm_vehicle_status_daily",
    "vehicle_utilisation_daily",
})

SNAPSHOT_TABLES: dict[str, str] = {
    "vehicle_fault_status_daily": "adp_${stage}_fleet_health.vehicle_fault_status_daily",
    "pm_vehicle_status_daily":    "adp_${stage}_fleet_health.pm_vehicle_status_daily",
    "vehicle_utilisation_daily":  "adp_${stage}_fleet_health.vehicle_utilisation_daily",
    "managed_vehicles":           "adp_${stage}_fleet_recall.managed_vehicles",
}

MAX_SNAPSHOT_AGE_DAYS = 1
_VALID_STAGES = frozenset({"staging", "prod"})


@dataclass(frozen=True)
class SnapshotState:
    date: datetime.date | None        # marker date (ADP) or MAX(snapshot_date) (managed_vehicles)
    row_count: int | None             # None for managed_vehicles; 0 means empty run
    table_date: datetime.date | None  # MAX(snapshot_date) of the actual daily table


class StaleSnapshotError(RuntimeError):
    def __init__(self, reason: str, dates: dict) -> None:
        super().__init__(reason)
        self.reason = reason
        self.dates = dates


def read_snapshot_dates(
    stage: str,
    keys: list[str],
    *,
    athena_client: Any,
    workgroup: str,
    results_s3_path: str,
    _run_query: Callable[..., list[dict]] = run_query,
) -> dict[str, SnapshotState]:
    """One UNION ALL query — SnapshotState per requested key.

    ADP keys: marker date + row_count from snapshot_runs (latest row); table_date
    from MAX(snapshot_date) of the daily table.  managed_vehicles: date = table_date
    = MAX(snapshot_date), row_count = None.  A key absent from the result raises.
    """
    if stage not in _VALID_STAGES:
        raise ValueError(f"stage must be one of {sorted(_VALID_STAGES)}, got {stage!r}")
    runs = f"adp_{stage}_fleet_health.snapshot_runs"
    parts: list[str] = []
    for k in keys:
        tbl = SNAPSHOT_TABLES[k].replace("${stage}", stage)
        if k in ADP_KEYS:
            parts.append(
                f"SELECT '{k}' AS table_key,"
                f" CAST(sr.snapshot_date AS VARCHAR) AS marker_date,"
                f" CAST(sr.row_count AS VARCHAR) AS row_count,"
                f" CAST(t.td AS VARCHAR) AS table_date"
                f" FROM (SELECT snapshot_date,row_count FROM {runs}"
                f"  WHERE table_key='{k}' ORDER BY snapshot_date DESC LIMIT 1) sr"
                f" CROSS JOIN (SELECT MAX(snapshot_date) AS td FROM {tbl}) t"
            )
        else:
            parts.append(
                f"SELECT '{k}' AS table_key,"
                f" CAST(MAX(snapshot_date) AS VARCHAR) AS marker_date,"
                f" CAST(NULL AS VARCHAR) AS row_count,"
                f" CAST(MAX(snapshot_date) AS VARCHAR) AS table_date"
                f" FROM {tbl}"
            )
    rows = _run_query(
        " UNION ALL ".join(parts),
        athena_client=athena_client,
        workgroup=workgroup,
        results_s3_path=results_s3_path,
    )

    def _d(s: str | None) -> datetime.date | None:
        return datetime.date.fromisoformat(s) if s else None

    result: dict[str, SnapshotState] = {}
    for row in rows:
        rc = row.get("row_count")
        result[row["table_key"]] = SnapshotState(
            date=_d(row.get("marker_date")),
            row_count=int(rc) if rc is not None else None,
            table_date=_d(row.get("table_date")),
        )
    missing = [k for k in keys if k not in result]
    if missing:
        raise StaleSnapshotError(f"missing keys: {missing}", dates=result)
    return result


def require_fresh(
    states: dict[str, SnapshotState],
    *,
    today: datetime.date,
    max_age_days: int = MAX_SNAPSHOT_AGE_DAYS,
    same_date: bool = True,
) -> datetime.date:
    """Validate states and return the snapshot date, or raise StaleSnapshotError.

    Refuses when any key's date is None (no marker / empty), stale, future, or
    (same_date=True) mixed.  For ADP keys also refuses when marker and table
    disagree: row_count>0 with table_date != date, or row_count==0 with
    table_date after date.
    """
    cutoff = today - datetime.timedelta(days=max_age_days)
    for key, s in states.items():
        if s.date is None:
            label = "no marker" if key in ADP_KEYS else "empty"
            raise StaleSnapshotError(f"{label} snapshot for {key!r}", dates=states)
    for key, s in states.items():
        if s.date > today:
            raise StaleSnapshotError(f"future snapshot {s.date} for {key!r}", dates=states)
    for key, s in states.items():
        if s.date < cutoff:
            raise StaleSnapshotError(
                f"stale snapshot for {key!r}: {s.date} older than cutoff {cutoff}",
                dates=states,
            )
    if same_date:
        unique = {s.date for s in states.values()}
        if len(unique) > 1:
            raise StaleSnapshotError(f"mixed snapshot dates: {unique}", dates=states)
    for key, s in states.items():
        if key not in ADP_KEYS:
            continue
        if s.row_count is not None and s.row_count > 0 and s.table_date != s.date:
            raise StaleSnapshotError(
                f"table and marker disagree for {key!r}: "
                f"table_date={s.table_date}, date={s.date}",
                dates=states,
            )
        if s.row_count == 0 and s.table_date is not None and s.table_date > s.date:
            raise StaleSnapshotError(
                f"table and marker disagree for {key!r}: "
                f"table_date={s.table_date} after date={s.date}",
                dates=states,
            )
    if same_date:
        return next(iter(states.values())).date
    return min(s.date for s in states.values())


def empty_keys(states: dict[str, SnapshotState]) -> frozenset[str]:
    """ADP keys whose marker says row_count == 0 (the ADP job ran but found nothing today)."""
    return frozenset(k for k, s in states.items() if k in ADP_KEYS and s.row_count == 0)
