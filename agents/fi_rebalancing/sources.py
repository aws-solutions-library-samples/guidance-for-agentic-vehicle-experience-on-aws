"""Data readers for fi-rebalancing (spec G4-D1).

All Athena reads go through adp_source.run_query (injected client, no direct boto3).
DynamoDB reads use the low-level client with explicit LastEvaluatedKey paging.
Malformed rows raise SourceReadError; they are never silently skipped.
"""
from __future__ import annotations

import logging
import math
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from botocore.exceptions import BotoCoreError, ClientError

from agents.tier2.adp_source import run_query

log = logging.getLogger(__name__)

_SQL_DIR = Path(__file__).parent / "sql"
_RE_STAGE    = re.compile(r"^[a-z][a-z0-9-]{0,15}$")
_RE_FLEET    = re.compile(r"^[a-z0-9-]{1,64}$")
_RE_NOW_Z    = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")
_RE_DATE     = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_RE_NOW      = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")


class SourceReadError(RuntimeError):
    """A data-source read failed in a way that aborts the run."""


# ---------------------------------------------------------------------------
# SQL loading + placeholder validation
# ---------------------------------------------------------------------------

def _load(name: str, *, stage: str, fleet_id: str = "", today: str = "", now: str = "") -> str:
    if not _RE_STAGE.match(stage):
        raise ValueError(f"stage {stage!r} fails ^[a-z][a-z0-9-]{{0,15}}$")
    if fleet_id and not _RE_FLEET.match(fleet_id):
        raise ValueError(f"fleet_id {fleet_id!r} fails ^[a-z0-9-]{{1,64}}$")
    if today and not _RE_DATE.match(today):
        raise ValueError(f"today {today!r} must be YYYY-MM-DD")
    if now and not _RE_NOW.match(now):
        raise ValueError(f"now {now!r} must be YYYY-MM-DD HH:MM:SS")
    sql = (_SQL_DIR / name).read_text()
    return (
        sql.replace("${stage}", stage)
           .replace("${fleet_id}", fleet_id)
           .replace("${today}", today)
           .replace("${now}", now)
    )


# ---------------------------------------------------------------------------
# Row-level parsers (raise SourceReadError on bad data, never skip)
# ---------------------------------------------------------------------------

def _f(row: dict, key: str) -> float:
    v = row.get(key)
    if v is None:
        raise SourceReadError(f"NULL for required float column {key!r}")
    try:
        return float(v)
    except (ValueError, TypeError):
        raise SourceReadError(f"bad float {key}={v!r}")


def _i(row: dict, key: str) -> int:
    v = row.get(key)
    if v is None:
        raise SourceReadError(f"NULL for required int column {key!r}")
    try:
        return int(v)
    except (ValueError, TypeError):
        raise SourceReadError(f"bad int {key}={v!r}")


def _fi(row: dict, key: str) -> int:
    """Parse a required DOUBLE column whose value is an integer ('12.0' → 12).

    Athena returns DOUBLE columns as decimal strings like '12.0'. ``_i`` rejects
    those; this helper accepts them while still raising SourceReadError for
    non-integral floats (e.g. '3.7') so schema violations are caught (C2a).
    """
    v = row.get(key)
    if v is None:
        raise SourceReadError(f"NULL for required int column {key!r}")
    try:
        f = float(v)
        if not math.isfinite(f):
            raise SourceReadError(f"non-finite value for int column {key}={v!r}")
        i = int(f)
        if f != i:
            raise SourceReadError(f"non-integral value for int column {key}={v!r}")
    except (ValueError, TypeError):
        raise SourceReadError(f"bad int {key}={v!r}")
    return i


def _s(row: dict, key: str) -> str:
    v = row.get(key)
    if v is None:
        raise SourceReadError(f"NULL for required string column {key!r}")
    return str(v)


def _s_opt(row: dict, key: str) -> str | None:
    v = row.get(key)
    return str(v) if v is not None else None


def _f_opt(row: dict, key: str) -> float | None:
    v = row.get(key)
    if v is None:
        return None
    try:
        return float(v)
    except (ValueError, TypeError):
        raise SourceReadError(f"bad optional float {key}={v!r}")


# ---------------------------------------------------------------------------
# Athena reads
# ---------------------------------------------------------------------------

def _aq(name: str, *, stage: str, fleet_id: str = "", today: str = "", now: str = "",
        athena_client: Any, workgroup: str, results_s3_path: str) -> list[dict]:
    """Load SQL, substitute placeholders, run query."""
    sql = _load(name, stage=stage, fleet_id=fleet_id, today=today, now=now)
    return run_query(sql, athena_client=athena_client, workgroup=workgroup,
                     results_s3_path=results_s3_path)


def read_gaps(*, stage: str, fleet_id: str, athena_client: Any,
              workgroup: str, results_s3_path: str) -> list[dict]:
    rows = _aq("select_gaps.sql", stage=stage, fleet_id=fleet_id,
               athena_client=athena_client, workgroup=workgroup, results_s3_path=results_s3_path)
    return [{"location_id": _s(r, "location_id"), "forecast_hours": _f(r, "forecast_hours"),
             "supply_vehicles": _fi(r, "supply_vehicles"),
             "forecast_utilization_pct": _f_opt(r, "forecast_utilization_pct"),
             "status": _s(r, "status"), "vehicles_short": _i(r, "vehicles_short"),
             "vehicles_spare": _i(r, "vehicles_spare"), "data_as_of": _s(r, "data_as_of")}
            for r in rows]


def read_forecast_days(*, stage: str, fleet_id: str, athena_client: Any,
                       workgroup: str, results_s3_path: str) -> list[dict]:
    rows = _aq("select_forecast_days.sql", stage=stage, fleet_id=fleet_id,
               athena_client=athena_client, workgroup=workgroup, results_s3_path=results_s3_path)
    return [{"location_id": _s(r, "location_id"), "forecast_date": _s(r, "forecast_date"),
             "forecast_hours": _f(r, "forecast_hours"), "method": _s(r, "method"),
             "mape_28d": _f_opt(r, "mape_28d"), "run_date": _s(r, "run_date"),
             "data_as_of": _s(r, "data_as_of")} for r in rows]


def read_outcomes(*, stage: str, fleet_id: str, athena_client: Any,
                  workgroup: str, results_s3_path: str) -> list[dict]:
    rows = _aq("select_outcomes.sql", stage=stage, fleet_id=fleet_id,
               athena_client=athena_client, workgroup=workgroup, results_s3_path=results_s3_path)
    return [{"to_location_id": _s(r, "to_location_id"), "effective_date": _s(r, "effective_date"),
             "ratio_hours": _f_opt(r, "ratio_hours"),
             "predicted_on_rent_hours_gain": _f_opt(r, "predicted_on_rent_hours_gain"),
             "actual_on_rent_hours_gain": _f_opt(r, "actual_on_rent_hours_gain"),
             "materialised_on": _s(r, "materialised_on")} for r in rows]


def read_vehicle_days(*, stage: str, fleet_id: str, athena_client: Any,
                      workgroup: str, results_s3_path: str) -> list[dict]:
    rows = _aq("select_vehicle_days.sql", stage=stage, fleet_id=fleet_id,
               athena_client=athena_client, workgroup=workgroup, results_s3_path=results_s3_path)
    return [{"vin": _s(r, "vin"), "location_id": _s(r, "location_id"),
             "available_hours": _f(r, "available_hours"),
             "snapshot_date": _s(r, "snapshot_date")} for r in rows]


def read_vehicle_utilization(*, stage: str, fleet_id: str, athena_client: Any,
                              workgroup: str, results_s3_path: str) -> list[dict]:
    rows = _aq("select_vehicle_utilization.sql", stage=stage, fleet_id=fleet_id,
               athena_client=athena_client, workgroup=workgroup, results_s3_path=results_s3_path)
    return [{"vin": _s(r, "vin"), "snapshot_date": _s(r, "snapshot_date"),
             "available_hours": _f(r, "available_hours"),
             "on_rent_hours": _f(r, "on_rent_hours")} for r in rows]


def read_busy_windows(*, stage: str, fleet_id: str, now: str, athena_client: Any,
                      workgroup: str, results_s3_path: str) -> list[dict]:
    rows = _aq("select_busy_windows.sql", stage=stage, fleet_id=fleet_id, now=now,
               athena_client=athena_client, workgroup=workgroup, results_s3_path=results_s3_path)
    result = []
    for r in rows:
        vin = _s(r, "vin")
        location_id = _s(r, "location_id")
        start_str = _s(r, "start_at")
        end_str = _s(r, "end_at")
        try:
            # Athena engine v3 returns TIMESTAMP as YYYY-MM-DD HH:MM:SS[.fff[fff]].
            # datetime.fromisoformat handles the optional fractional seconds (3.11+).
            start_at = datetime.fromisoformat(start_str.replace(" ", "T"))
        except ValueError:
            raise SourceReadError(f"malformed start_at: {start_str!r}")
        try:
            end_at = datetime.fromisoformat(end_str.replace(" ", "T"))
        except ValueError:
            raise SourceReadError(f"malformed end_at: {end_str!r}")
        # Strip any timezone info — busy windows are compared to naive UTC now_dt.
        if start_at.tzinfo is not None:
            start_at = start_at.astimezone(timezone.utc).replace(tzinfo=None)
        if end_at.tzinfo is not None:
            end_at = end_at.astimezone(timezone.utc).replace(tzinfo=None)
        result.append({"vin": vin, "location_id": location_id,
                        "start_at": start_at, "end_at": end_at})
    return result


def read_request_lengths(*, stage: str, fleet_id: str, now: str, athena_client: Any,
                         workgroup: str, results_s3_path: str) -> list[dict]:
    rows = _aq("select_request_lengths.sql", stage=stage, fleet_id=fleet_id, now=now,
               athena_client=athena_client, workgroup=workgroup, results_s3_path=results_s3_path)
    return [{"location_id": _s(r, "location_id"),
             "mean_request_hours": _f(r, "mean_request_hours")} for r in rows]


def _parse_array(v: object) -> list[str]:
    """Parse Athena's array string form ``'[a, b]'`` to a Python list.

    ``run_query`` returns every cell as a string; an ``array<string>`` column
    arrives as ``'[loc-a, loc-b]'``.  An already-list value passes through
    unchanged (unit-test convenience).  NULL / empty → [].
    """
    if v is None:
        return []
    if isinstance(v, list):
        return [str(item) for item in v]
    s = str(v).strip()
    if s in ("[]", ""):
        return []
    # Strip leading '[' and trailing ']', then split on ', '
    inner = s.strip("[]")
    return [item.strip() for item in inner.split(", ") if item.strip()]


def read_events(*, stage: str, today: str, athena_client: Any,
                workgroup: str, results_s3_path: str) -> list[dict]:
    rows = _aq("select_events.sql", stage=stage, today=today,
               athena_client=athena_client, workgroup=workgroup, results_s3_path=results_s3_path)
    return [{"event_id": _s(r, "event_id"), "metro": _s(r, "metro"),
             "affected_location_ids": _parse_array(r.get("affected_location_ids")),
             "category": _s(r, "category"), "uplift_band": _s(r, "uplift_band"),
             "starts_on": _s(r, "starts_on"), "ends_on": _s(r, "ends_on"),
             "announced_on": _s(r, "announced_on")} for r in rows]


def read_open_ros(*, stage: str, athena_client: Any,
                  workgroup: str, results_s3_path: str) -> list[dict]:
    rows = _aq("select_open_ros.sql", stage=stage,
               athena_client=athena_client, workgroup=workgroup, results_s3_path=results_s3_path)
    return [{"ro_id": _s(r, "ro_id"), "vehicle_vin": _s(r, "vehicle_vin"),
             "status": _s(r, "status")} for r in rows]


def read_service_soon(*, stage: str, today: str, athena_client: Any,
                      workgroup: str, results_s3_path: str) -> list[dict]:
    rows = _aq("select_service_soon.sql", stage=stage, today=today,
               athena_client=athena_client, workgroup=workgroup, results_s3_path=results_s3_path)
    return [{"vin": _s(r, "vin"), "service_date": _s(r, "service_date")} for r in rows]


# ---------------------------------------------------------------------------
# DynamoDB reads
# ---------------------------------------------------------------------------

_RE_OP_HOURS = re.compile(r"^(\d+)-(\d+)$")


def read_locations(fleet_id: str, *, ddb_client: Any, table: str
                   ) -> tuple[list[dict], list[str], set[str]]:
    """Query fleet-locations table; raises SourceReadError on any failure.

    Returns ``(complete_locations, incomplete_location_ids, incomplete_metros)``.
    A location with a missing or malformed metro, locationType, latitude,
    longitude, parkingSpaces, or operatingHours (format 'HH-HH',
    0 <= open < close <= 24) is added to ``incomplete_location_ids`` and
    excluded from the returned list.  Each complete location also carries
    ``op_hours = close - open`` as a float.  ``incomplete_metros`` is the set
    of metro names that have at least one incomplete location; callers use it
    to skip surplus gaps in metros where demand data may be unreliable.
    """
    items: list = []
    kwargs: dict = {
        "TableName": table,
        "KeyConditionExpression": "#f = :fid",
        "ExpressionAttributeNames": {"#f": "fleetId"},
        "ExpressionAttributeValues": {":fid": {"S": fleet_id}},
    }
    try:
        while True:
            resp = ddb_client.query(**kwargs)
            items.extend(resp.get("Items") or [])
            lek = resp.get("LastEvaluatedKey")
            if not lek:
                break
            kwargs["ExclusiveStartKey"] = lek
    except (ClientError, BotoCoreError, Exception) as exc:
        raise SourceReadError(f"fleet-locations read failed: {exc}") from exc

    complete: list[dict] = []
    incomplete: list[str] = []
    incomplete_metros: set[str] = set()

    for item in items:
        loc_id = (item.get("locationId") or {}).get("S")
        if not loc_id:
            continue

        metro = (item.get("metro") or {}).get("S")
        if not metro:
            log.warning("sources: location %s incomplete: missing metro", loc_id)
            incomplete.append(loc_id)
            continue
        # metro is known at this point — record it in incomplete_metros for any
        # subsequent parse failure so S3 can skip surplus gaps in this metro.

        location_type = (item.get("locationType") or {}).get("S")
        if not location_type:
            log.warning("sources: location %s incomplete: missing locationType", loc_id)
            incomplete.append(loc_id)
            incomplete_metros.add(metro)
            continue

        lat_str = (item.get("latitude") or {}).get("N")
        if lat_str is None:
            log.warning("sources: location %s incomplete: missing latitude", loc_id)
            incomplete.append(loc_id)
            incomplete_metros.add(metro)
            continue
        try:
            lat = float(lat_str)
        except (ValueError, TypeError):
            log.warning("sources: location %s incomplete: malformed latitude %r", loc_id, lat_str)
            incomplete.append(loc_id)
            incomplete_metros.add(metro)
            continue

        lon_str = (item.get("longitude") or {}).get("N")
        if lon_str is None:
            log.warning("sources: location %s incomplete: missing longitude", loc_id)
            incomplete.append(loc_id)
            incomplete_metros.add(metro)
            continue
        try:
            lon = float(lon_str)
        except (ValueError, TypeError):
            log.warning("sources: location %s incomplete: malformed longitude %r", loc_id, lon_str)
            incomplete.append(loc_id)
            incomplete_metros.add(metro)
            continue

        parking_str = (item.get("parkingSpaces") or {}).get("N")
        if parking_str is None:
            log.warning("sources: location %s incomplete: missing parkingSpaces", loc_id)
            incomplete.append(loc_id)
            incomplete_metros.add(metro)
            continue
        try:
            parking = int(parking_str)
        except (ValueError, TypeError):
            log.warning("sources: location %s incomplete: malformed parkingSpaces %r", loc_id, parking_str)
            incomplete.append(loc_id)
            incomplete_metros.add(metro)
            continue

        op_hours_str = (item.get("operatingHours") or {}).get("S")
        if not op_hours_str:
            log.warning("sources: location %s incomplete: missing operatingHours", loc_id)
            incomplete.append(loc_id)
            incomplete_metros.add(metro)
            continue
        m = _RE_OP_HOURS.match(op_hours_str)
        if not m:
            log.warning("sources: location %s incomplete: malformed operatingHours %r", loc_id, op_hours_str)
            incomplete.append(loc_id)
            incomplete_metros.add(metro)
            continue
        open_h, close_h = int(m.group(1)), int(m.group(2))
        if not (0 <= open_h < close_h <= 24):
            log.warning("sources: location %s incomplete: invalid operatingHours range %r", loc_id, op_hours_str)
            incomplete.append(loc_id)
            incomplete_metros.add(metro)
            continue

        complete.append({
            "locationId": loc_id,
            "name": (item.get("name") or {}).get("S") or loc_id,
            "metro": metro,
            "locationType": location_type,
            "latitude": lat,
            "longitude": lon,
            "parkingSpaces": parking,
            "operatingHours": op_hours_str,
            "op_hours": float(close_h - open_h),
        })

    return complete, incomplete, incomplete_metros


def read_grounding(vin: str, *, ddb_client: Any, vehicles_table: str,
                   groundings_table: str) -> tuple[bool | None, bool]:
    """(is_grounded, read_error). Returns (None, True) on any failure."""
    try:
        resp = ddb_client.query(
            TableName=vehicles_table,
            IndexName="vin-index",
            KeyConditionExpression="#v = :vin",
            ExpressionAttributeNames={"#v": "vin"},
            ExpressionAttributeValues={":vin": {"S": vin}},
            Limit=2,
        )
        items = resp.get("Items") or []
        if len(items) != 1:
            return None, True
        vehicle_id = (items[0].get("vehicleId") or {}).get("S")
        if not vehicle_id:
            return None, True
    except Exception as exc:
        log.warning("sources: vin-index failed vin=<redacted>: %s", exc)
        return None, True

    try:
        grounding_items: list = []
        kwargs: dict = {
            "TableName": groundings_table,
            "KeyConditionExpression": "#v = :vid",
            "ExpressionAttributeNames": {"#v": "vehicleId"},
            "ExpressionAttributeValues": {
                ":vid": {"S": vehicle_id},
                ":null": {"S": "NULL"},
            },
            "FilterExpression": (
                "attribute_not_exists(returnedAt) OR attribute_type(returnedAt, :null)"
            ),
        }
        while True:
            resp = ddb_client.query(**kwargs)
            grounding_items.extend(resp.get("Items") or [])
            lek = resp.get("LastEvaluatedKey")
            if not lek:
                break
            kwargs["ExclusiveStartKey"] = lek
        return len(grounding_items) > 0, False
    except Exception as exc:
        log.warning("sources: groundings read failed vehicleId=<redacted>: %s", exc)
        return None, True


def read_open_items(vin: str, *, ddb_client: Any, findings_table: str,
                    now_iso: str) -> tuple[list | None, bool]:
    """(live_items, read_error). All live items (expires_at > now), any severity.

    The caller (eligibility) is responsible for disqualifying ``p0``/``attention``
    items; the rest are kept as evidence (G4-D2 W8 fix: filtering by severity in
    DynamoDB made ``open_lower_severity_items`` always empty).

    ``now_iso`` must be in the findings table's own format (``YYYY-MM-DDTHH:MM:SSZ``, as
    ``agents.tier2.writer`` writes ``expires_at``): the filter compares strings, and a
    space-separated now would rank every row expiring earlier the same day as live.
    """
    if not _RE_NOW_Z.fullmatch(now_iso or ""):
        raise ValueError("now_iso must be YYYY-MM-DDTHH:MM:SSZ")
    try:
        items: list = []
        kwargs: dict = {
            "TableName": findings_table,
            "IndexName": "by_vin",
            "KeyConditionExpression": "#v = :vin",
            "ExpressionAttributeNames": {"#v": "vin"},
            "ExpressionAttributeValues": {
                ":vin": {"S": vin},
                ":now": {"S": now_iso},
            },
            "FilterExpression": "expires_at > :now",
        }
        while True:
            resp = ddb_client.query(**kwargs)
            items.extend(resp.get("Items") or [])
            lek = resp.get("LastEvaluatedKey")
            if not lek:
                break
            kwargs["ExclusiveStartKey"] = lek
        return [
            {"severity": (it.get("severity") or {}).get("S") or "",
             "finding_id": (it.get("finding_id") or {}).get("S") or ""}
            for it in items
        ], False
    except Exception as exc:
        log.warning("sources: open_items read failed vin=<redacted>: %s", exc)
        return None, True


# ---------------------------------------------------------------------------
# Vehicle-facts assembler (spec G4-D2)
# ---------------------------------------------------------------------------

def build_vehicle_facts(
    vin: str,
    *,
    location_id: str,
    vehicle_days_rows: list[dict],
    utilization_rows: list[dict],
    busy_rows: list[dict],
    open_ros_rows: list[dict],
    service_rows: list[dict],
    now_dt: datetime,
    today_str: str,
) -> dict:
    """Assemble per-vehicle facts needed by eligibility.filter_eligible."""
    # normalise now_dt to naive UTC (busy_rows start_at/end_at are naive UTC datetimes)
    if now_dt.tzinfo is not None:
        now_dt = now_dt.astimezone(timezone.utc).replace(tzinfo=None)

    # in_service: latest vehicle_days row for this vin
    vd = next((r for r in vehicle_days_rows if r["vin"] == vin), None)
    in_service = bool(vd and vd["available_hours"] > 0)

    # days_idle_last_7
    util_vin = [r for r in utilization_rows if r["vin"] == vin]
    days_idle = sum(
        1 for r in util_vin if r["available_hours"] > 0 and r["on_rent_hours"] == 0
    )

    # days_to_next_service: 0.0 for open RO, days to earliest service_date, else inf
    has_open_ro = any(r["vehicle_vin"] == vin for r in open_ros_rows)
    if has_open_ro:
        days_to_next_service: float = 0.0
    else:
        svc_dates = [r["service_date"] for r in service_rows if r["vin"] == vin]
        if svc_dates:
            earliest = min(svc_dates)
            from datetime import date
            svc_date = date.fromisoformat(earliest)
            today = date.fromisoformat(today_str)
            days_to_next_service = float((svc_date - today).days)
        else:
            days_to_next_service = float("inf")

    # hours_to_next_booking — busy_rows carry naive UTC datetimes (start_at, end_at)
    vin_busy = [r for r in busy_rows if r["vin"] == vin]
    hours_to_next_booking: float
    if not vin_busy:
        hours_to_next_booking = float("inf")
    else:
        on_rent_now = any(
            r["start_at"] <= now_dt < r["end_at"]
            for r in vin_busy
        )
        if on_rent_now:
            hours_to_next_booking = 0.0
        else:
            future = [r for r in vin_busy if r["start_at"] > now_dt]
            if future:
                next_start = min(r["start_at"] for r in future)
                delta = next_start - now_dt
                hours_to_next_booking = delta.total_seconds() / 3600.0
            else:
                hours_to_next_booking = float("inf")

    return {
        "vin": vin,
        "location_id": location_id,
        "in_service": in_service,
        "days_idle_last_7": days_idle,
        "days_to_next_service": days_to_next_service,
        "hours_to_next_booking": hours_to_next_booking,
    }
