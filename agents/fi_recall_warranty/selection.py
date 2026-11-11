"""Selection layer for the FI recall and warranty Tier 2 agent (spec D8, task 3.1E).

Public surface
--------------
select_unremedied_recalls  — Athena query returning unremedied recall rows + refused count.
select_parts_stock         — Athena query returning rooftop stock for all part numbers.
select_warranty_candidates — Athena query returning unclaimed warranty candidate rows.
lookup_vehicle_cms_ids     — DDB: VIN → (vehicleId, fleetId, locationId) or None.
lookup_fleet_locations     — DDB: fleetId → {locationId: (lat, lon)}.
check_grounded             — DDB: vehicleId → (is_grounded, error_count).

All Athena functions accept a ``_run_query`` keyword argument so tests can inject a
fake without monkeypatching module globals.  Default is ``agents.tier2.adp_source.run_query``.

Database/stage names are validated with ``re.fullmatch`` before SQL interpolation.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, Optional

from botocore.exceptions import BotoCoreError, ClientError

from agents.tier2.adp_source import run_query as _default_run_query

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Stage validation
# ---------------------------------------------------------------------------
_STAGE_RE = re.compile(r"^(staging|prod)$")


def _validate_stage(stage: str) -> str:
    if not _STAGE_RE.fullmatch(stage):
        raise ValueError(f"Invalid stage {stage!r}; expected 'staging' or 'prod'")
    return stage


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class RegistryReadError(Exception):
    """Raised by lookup_vehicle_cms_ids when a botocore error prevents the read.

    The loop treats this as 'no vehicleId, no origin' (same as an unknown VIN)
    and counts it in ``stats['grounding_read_errors']`` (G3-17, D17).
    """


# ---------------------------------------------------------------------------
# SQL templates — loaded once at import time
# ---------------------------------------------------------------------------
_SQL_DIR = Path(__file__).parent / "sql"

_RECALLS_SQL: str = (_SQL_DIR / "select_recalls.sql").read_text()
_PARTS_SQL: str = (_SQL_DIR / "select_parts_stock.sql").read_text()
_WARRANTY_SQL: str = (_SQL_DIR / "select_warranty_candidates.sql").read_text()
_TERMS_SQL: str = (_SQL_DIR / "select_warranty_terms.sql").read_text()


# ---------------------------------------------------------------------------
# Helpers: parse Athena-serialised types
# ---------------------------------------------------------------------------

def _parse_do_not_drive(raw: str | None) -> bool:
    """Parse Athena's string representation of a boolean column.

    Athena returns boolean columns as the string ``"true"`` or ``"false"``.
    ``"false"`` is truthy in Python, so string → bool must be explicit (G3-9).

    Raises ValueError for any value other than exactly ``"true"`` or ``"false"``.
    """
    if raw == "true":
        return True
    if raw == "false":
        return False
    raise ValueError(f"Unexpected do_not_drive value {raw!r}; expected 'true' or 'false'")


def _parse_part_numbers(raw: str | None) -> list[str]:
    """Parse Athena's ``array<string>`` serialisation: ``[part1, part2]``."""
    if not raw:
        return []
    raw = raw.strip()
    if raw in ("[]", "", "null", "NULL"):
        return []
    if raw.startswith("[") and raw.endswith("]"):
        raw = raw[1:-1]
    return [item.strip().strip("'\"") for item in raw.split(",") if item.strip()]



# ---------------------------------------------------------------------------
# Athena: unremedied recall rows
# ---------------------------------------------------------------------------

def select_unremedied_recalls(
    stage: str,
    *,
    athena_client: Any,
    workgroup: str,
    results_s3_path: str,
    _run_query: Any = _default_run_query,
) -> tuple[list[dict], int]:
    """Query recall_vehicle_status for unremedied rows of received campaigns.

    Returns ``(rows, refused_count)`` where *refused_count* is the number of rows
    dropped because ``do_not_drive`` was not exactly ``"true"`` or ``"false"``
    (G3-9: a string ``"false"`` is truthy in Python, so rows with non-bool values
    are refused rather than silently misclassified).

    Each row dict is the raw Athena row plus ``do_not_drive`` as a real ``bool``
    and ``remedy_part_numbers`` as a ``list[str]``.
    """
    _validate_stage(stage)
    sql = _RECALLS_SQL.replace("${stage}", stage)
    raw_rows = _run_query(
        sql,
        athena_client=athena_client,
        workgroup=workgroup,
        results_s3_path=results_s3_path,
    )

    accepted: list[dict] = []
    refused = 0
    for row in raw_rows:
        try:
            dnd = _parse_do_not_drive(row.get("do_not_drive"))
        except ValueError:
            log.warning(
                "selection: refused row campaign_id=%s vin=<redacted> — bad do_not_drive=%r",
                row.get("campaign_id"), row.get("do_not_drive"),
            )
            refused += 1
            continue
        out = dict(row)
        out["do_not_drive"] = dnd
        out["remedy_part_numbers"] = _parse_part_numbers(row.get("remedy_part_numbers"))
        # FI stage D 9.3: an issued recall's lots (array<string>, "[a, b]"; NULL elsewhere).
        out["component_lots"] = _parse_part_numbers(row.get("component_lots"))
        accepted.append(out)

    log.debug(
        "selection: recalls query stage=%s accepted=%d refused=%d",
        stage, len(accepted), refused,
    )
    return accepted, refused



# ---------------------------------------------------------------------------
# Athena: DMS parts stock + rooftop coordinates
# ---------------------------------------------------------------------------

def select_parts_stock(
    stage: str,
    *,
    athena_client: Any,
    workgroup: str,
    results_s3_path: str,
    _run_query: Any = _default_run_query,
) -> list[dict]:
    """Query dms_${stage} for parts stock and rooftop coordinates.

    Returns one row per ``(dealer_id, part_number)`` with ``qty_on_hand``,
    ``latitude``, and ``longitude`` as strings (Athena raw output).

    The caller filters by the specific part numbers from the recall campaigns.
    """
    _validate_stage(stage)
    sql = _PARTS_SQL.replace("${stage}", stage)
    raw_rows = _run_query(
        sql,
        athena_client=athena_client,
        workgroup=workgroup,
        results_s3_path=results_s3_path,
    )
    log.debug("selection: parts stock rows=%d", len(raw_rows))
    return raw_rows


# ---------------------------------------------------------------------------
# Athena: unclaimed warranty candidates
# ---------------------------------------------------------------------------

def select_warranty_candidates(
    stage: str,
    *,
    athena_client: Any,
    workgroup: str,
    results_s3_path: str,
    _run_query: Any = _default_run_query,
) -> list[dict]:
    """Query dms_${stage}.warranty_candidates for unclaimed approved rows (D8 step 1).

    SQL filters: claimed = FALSE, verdict_status IN ('approved', 'requires_review'),
    and filing_deadline > current_date.  ``line_no`` is CAST to VARCHAR in SQL (G3-6).
    All other fields are returned as strings or None per adp_source semantics.
    """
    _validate_stage(stage)
    sql = _WARRANTY_SQL.replace("${stage}", stage)
    raw_rows = _run_query(
        sql,
        athena_client=athena_client,
        workgroup=workgroup,
        results_s3_path=results_s3_path,
    )
    log.debug("selection: warranty candidates rows=%d", len(raw_rows))
    return raw_rows


# ---------------------------------------------------------------------------
# Athena: DMS warranty_terms (lake) — current terms by coverage_group
# ---------------------------------------------------------------------------

def _semver(v: Any) -> Optional[tuple]:
    """Numeric semver tuple, or None when ``v`` is not three integer parts (§ 7.2 S5)."""
    parts = str(v or "").split(".")
    if len(parts) != 3 or not all(p.isdigit() for p in parts):
        return None
    return tuple(int(p) for p in parts)


def pick_current_terms(rows: list[dict]) -> dict[str, dict]:
    """Newest ``terms_version`` by SEMVER (never string order), keyed by coverage_group.

    ``rows`` are lake rows already filtered to ``effective_from <= current_date``. Values
    arrive as strings (adp_source semantics); a row whose version or numbers do not parse
    is dropped. Returns ``{}`` when nothing usable remains.
    """
    usable = []
    for r in rows:
        ver = _semver(r.get("terms_version"))
        try:
            parsed = {
                "terms_version": str(r["terms_version"]),
                "coverage_group": str(r["coverage_group"]),
                "effective_from": str(r.get("effective_from") or ""),
                "term_years": int(float(r["term_years"])),
                "term_miles": int(float(r["term_miles"])),
                "labor_rate_usd": float(r["labor_rate_usd"]),
            }
        except (KeyError, TypeError, ValueError):
            continue
        if ver is not None and parsed["coverage_group"]:
            usable.append((ver, parsed))
    if not usable:
        return {}
    best = max(v for v, _ in usable)
    return {p["coverage_group"]: p for v, p in usable if v == best}


def select_warranty_terms(
    stage: str,
    *,
    athena_client: Any,
    workgroup: str,
    results_s3_path: str,
    _run_query: Any = _default_run_query,
) -> dict[str, dict]:
    """Return the current DMS warranty terms keyed by coverage_group (DMS § 7.1/§ 7.2).

    Reads the lake table ``dms_${stage}.warranty_terms`` (the same Athena path as the
    warranty candidates), rows in effect today, then :func:`pick_current_terms`.
    """
    _validate_stage(stage)
    sql = _TERMS_SQL.replace("${stage}", stage)
    rows = _run_query(sql, athena_client=athena_client, workgroup=workgroup,
                      results_s3_path=results_s3_path)
    terms = pick_current_terms(rows)
    log.debug("selection: warranty terms rows=%d groups=%d", len(rows), len(terms))
    return terms


# ---------------------------------------------------------------------------
# DynamoDB: VIN → CMS vehicle ids
# ---------------------------------------------------------------------------

# Attribute names on cms-{stage}-storage-vehicles (per owner_resolver.py / G3-10).
_VIN_INDEX = "vin-index"
_VEHICLE_ID_ATTR = "vehicleId"
_FLEET_ID_ATTR = "fleetId"
_LOCATION_ID_ATTR = "locationId"


def lookup_vehicle_cms_ids(
    vin: str,
    *,
    ddb_client: Any,
    vehicles_table: str,
) -> Optional[tuple[str, str, Optional[str]]]:
    """Resolve VIN → (vehicleId, fleetId, locationId) from the CMS vehicles table.

    Returns ``None`` when:
    - zero vehicles match the VIN (not in registry);
    - two or more vehicles match (ambiguous — registry corruption; G3-5);
    - ``fleetId`` is absent from the base row.

    ``locationId`` may be None; callers must handle "no assigned location" gracefully
    (G3-10: no assigned location → candidates unranked, distance_miles null).

    Raises :class:`RegistryReadError` on ``ClientError`` or ``BotoCoreError``
    (G3-17, D17: the loop treats this as "no vehicleId, no origin" and counts
    it in ``stats['grounding_read_errors']`` without aborting the run).
    """
    try:
        resp = ddb_client.query(
            TableName=vehicles_table,
            IndexName=_VIN_INDEX,
            KeyConditionExpression="#v = :vin",
            ExpressionAttributeNames={"#v": "vin"},
            ExpressionAttributeValues={":vin": {"S": vin}},
            Limit=2,
        )
        items = resp.get("Items") or []
        if len(items) != 1:
            log.debug(
                "selection: vin-index returned %d items for vin=<redacted> (expected 1) → None",
                len(items),
            )
            return None

        vehicle_id = (items[0].get(_VEHICLE_ID_ATTR) or {}).get("S")
        if not vehicle_id:
            return None

        get_resp = ddb_client.get_item(
            TableName=vehicles_table,
            Key={_VEHICLE_ID_ATTR: {"S": vehicle_id}},
            ProjectionExpression="#fl, #li",
            ExpressionAttributeNames={"#fl": _FLEET_ID_ATTR, "#li": _LOCATION_ID_ATTR},
        )
        item = get_resp.get("Item") or {}
        fleet_id = (item.get(_FLEET_ID_ATTR) or {}).get("S")
        if not fleet_id:
            return None
        location_id: Optional[str] = (item.get(_LOCATION_ID_ATTR) or {}).get("S") or None
        return vehicle_id, fleet_id, location_id
    except (ClientError, BotoCoreError) as exc:
        log.warning(
            "selection: registry read failed for vin=<redacted> — treating as no ids: %s",
            exc,
        )
        raise RegistryReadError(str(exc)) from exc



# ---------------------------------------------------------------------------
# DynamoDB: fleetId → location coordinates
# ---------------------------------------------------------------------------

def lookup_fleet_locations(
    fleet_id: str,
    *,
    ddb_client: Any,
    fleet_locations_table: str,
) -> dict[str, tuple[float, float]]:
    """Query cms-{stage}-storage-fleet-locations for a fleet's locations.

    Returns ``{locationId: (latitude, longitude)}`` as floats.
    Unknown or missing coordinate attributes are skipped.

    Per G3-10: pk=fleetId, sk=locationId, columns latitude/longitude.
    """
    try:
        resp = ddb_client.query(
            TableName=fleet_locations_table,
            KeyConditionExpression="#f = :fid",
            ExpressionAttributeNames={"#f": "fleetId"},
            ExpressionAttributeValues={":fid": {"S": fleet_id}},
            ProjectionExpression="locationId, latitude, longitude",
        )
        items = resp.get("Items") or []
        result: dict[str, tuple[float, float]] = {}
        for item in items:
            loc_id = (item.get("locationId") or {}).get("S")
            lat_node = item.get("latitude") or {}
            lon_node = item.get("longitude") or {}
            lat_raw = lat_node.get("N") or lat_node.get("S")
            lon_raw = lon_node.get("N") or lon_node.get("S")
            if loc_id and lat_raw is not None and lon_raw is not None:
                try:
                    result[loc_id] = (float(lat_raw), float(lon_raw))
                except (ValueError, TypeError):
                    log.debug("selection: skipping location %s — bad coordinates", loc_id)
        return result
    except (ClientError, BotoCoreError) as exc:
        # G3-17 / cycle 2 agent W1: a fleet-locations read error is a CMS read error on
        # the grounding leg; the loop keeps the vehicle id and ranks without an origin.
        log.warning("selection: fleet-locations read failed — no origin: %s", type(exc).__name__)
        raise RegistryReadError(type(exc).__name__) from exc


# ---------------------------------------------------------------------------
# DynamoDB: vehicleId → open grounding check
# ---------------------------------------------------------------------------

def check_grounded(
    vehicle_id: str,
    *,
    ddb_client: Any,
    groundings_table: str,
) -> tuple[bool, int]:
    """Check whether a vehicle has an open grounding in the CMS groundings table.

    An open grounding is any row with pk=vehicleId and no ``returnedAt`` attribute.
    Returns ``(is_grounded, error_count)`` where ``error_count`` is 1 on any read
    error, 0 on success.

    On error the safe direction is ``False`` (not grounded) so a grounding item is
    still written for the vehicle — CMS's POST is idempotent (D17).
    """
    try:
        resp = ddb_client.query(
            TableName=groundings_table,
            KeyConditionExpression="#v = :vid",
            ExpressionAttributeNames={"#v": "vehicleId"},
            ExpressionAttributeValues={
                ":vid": {"S": vehicle_id},
                ":null": {"S": "NULL"},
            },
            FilterExpression="attribute_not_exists(returnedAt) OR attribute_type(returnedAt, :null)",
        )
        items = resp.get("Items") or []
        is_open = len(items) > 0
        return is_open, 0
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "selection: grounding read failed for vehicleId=<redacted> — treating as not grounded: %s",
            exc,
        )
        return False, 1
