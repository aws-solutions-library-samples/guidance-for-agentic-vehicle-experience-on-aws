"""Production readers for the FI fleet-health Tier 2 component (spec D8 step 1 / task 3.2c).

Reads from four ADP Athena tables and two DynamoDB indexes.  SQL and column
names are owned here; Athena mechanics are delegated to
``agents.tier2.adp_source.run_query``.

Public API
----------
select_fault_rows(stage, *, athena_client, workgroup, results_s3_path)
    Open faults at band ``soon``+ from ``vehicle_fault_status`` joined to
    ``fault_codes``; paged internally by ``run_query`` via ``NextToken``.

select_pm_rows(stage, *, athena_client, workgroup, results_s3_path)
    PM tasks with status ``overdue`` or ``due`` from ``pm_vehicle_status``.

select_managed_vins(stage, *, athena_client, workgroup, results_s3_path)
    All managed VINs and their fleet_id from ``fleet_recall.managed_vehicles``
    (newest snapshot; no tier filter — the table holds managed VINs only).

select_daily_usage(stage, *, athena_client, workgroup, results_s3_path)
    30-day average miles per VIN from
    ``fleet_health.vehicle_utilisation_daily`` (newest snapshot).

select_agent2_findings(fleet_ids, *, ddb_client, findings_table, now)
    Live Agent 2 vehicle-health findings (severity soon or attention, never p0)
    via ``by_fleet`` GSI, paging ``LastEvaluatedKey``.  Returns per-VIN list.

recall_visit_fn(vin, *, ddb_client, findings_table, now)
    Live Stage C recall booking Finding for a VIN via ``by_vin`` GSI.
    Returns ``{finding_id, recall_id, rooftop_dealer_id}`` or ``None``.

build_vehicle_rows(*, fault_rows, pm_rows, managed_vins, usage_rows, agent2_rows)
    Assembler.  Builds ``vehicle_rows`` for ``run_health_loop`` (union: every
    VIN with at least one need) and counters dict.

Re-exports (Stage C readers, reused verbatim)
---------------------------------------------
lookup_vehicle_cms_ids, lookup_fleet_locations, select_parts_stock
    From ``agents.fi_recall_warranty.selection``.

Backward compatibility
----------------------
select_vehicles_with_needs is kept (repointed to select_fault_rows rows).

Mutation guards (documented on the guarded tests in test_readers.py)
--------------------------------------------------------------------
- Stopping after the first Athena page → SQL text tests (row count short).
- Dropping ``tier = 'managed'`` from managed-VIN query → wrong VINs.
- Reading Agent 2 findings without the severity filter → routine findings appear.
- Dropping the live (expiry) filter on recall_visit_fn → expired visit returned.
"""
from __future__ import annotations

import datetime
import logging
from typing import Any, Callable, Optional

from boto3.dynamodb.types import TypeDeserializer as _TypeDeserializer

from agents.tier2.adp_source import run_query as _default_run_query

# ---------------------------------------------------------------------------
# Re-exports: Stage C CMS / DMS readers (spec D8 step 4 / task 3.2c item 4)
# ---------------------------------------------------------------------------
from agents.fi_recall_warranty.selection import (  # noqa: F401
    lookup_fleet_locations,
    lookup_vehicle_cms_ids,
    select_parts_stock,
)

log = logging.getLogger(__name__)

_DESERIALIZER = _TypeDeserializer()

# ---------------------------------------------------------------------------
# ADP Athena SQL templates (stage substituted via .replace("${stage}", stage))
# ---------------------------------------------------------------------------

#: Open faults (soon, attention, p0) joined to fault_codes for band + directive.
#: Database: adp_${stage}_fleet_health (vehicle_fault_status_daily, fault_codes).
#: Reads the newest daily snapshot only (WHERE snapshot_date = MAX(snapshot_date)).
#:
#: W4 NOTE: `last_seen_at` here is `vfs.last_seen = MAX(raised_at)` from ADP's
#: daily snapshot of `vehicle_fault_status`.  The return-to-service check's true
#: anchor is `max(last_seen_at)` across per-occurrence last-sighting dates.  These
#: agree for single-occurrence events.  Once stage 2's CMS `lastSeenAt` rows flow
#: in, the ADP view should expose its computed `anchor` column.  Until then this
#: field is the best available approximation; it is documented in contract §1.6
#: and in `docs/fi-fleet-health-contract.md` § 4.
#:
#: Mutation guard: removing severity_band IN (...) returns routine faults that
#: must not reach the loop.
_FAULT_SQL = """\
SELECT
    vfs.vin,
    vfs.code,
    vfs.fleet_id,
    vfs.severity_band               AS band,
    CAST(vfs.last_seen AS VARCHAR)  AS last_seen_at,
    fc.directive_text,
    vfs.source                      AS fault_source
FROM adp_${stage}_fleet_health.vehicle_fault_status_daily   vfs
JOIN adp_${stage}_fleet_health.fault_codes                   fc
  ON vfs.code = fc.code
WHERE vfs.snapshot_date = (
    SELECT MAX(snapshot_date)
    FROM adp_${stage}_fleet_health.vehicle_fault_status_daily
)
  AND vfs.status = 'open'
  AND vfs.severity_band IN ('p0', 'attention', 'soon')
"""

#: PM tasks that need attention (overdue or due).
#: Database: adp_${stage}_fleet_health (pm_vehicle_status_daily).
#: Reads the newest daily snapshot only.
_PM_SQL = """\
SELECT
    vin,
    fleet_id,
    task_code,
    status
FROM adp_${stage}_fleet_health.pm_vehicle_status_daily
WHERE snapshot_date = (
    SELECT MAX(snapshot_date)
    FROM adp_${stage}_fleet_health.pm_vehicle_status_daily
)
  AND status IN ('overdue', 'due')
"""

#: Managed VINs and their fleet membership.
#: Database: adp_${stage}_fleet_recall (managed_vehicles).
#: No tier filter — the table holds managed VINs only.
#: Reads the newest daily snapshot: WHERE snapshot_date = MAX(snapshot_date).
#:
#: Mutation guard: the SQL text test pins the table name and the snapshot filter.
_MANAGED_VINS_SQL = """\
SELECT
    vin,
    fleet_id
FROM adp_${stage}_fleet_recall.managed_vehicles
WHERE snapshot_date = (
    SELECT MAX(snapshot_date)
    FROM adp_${stage}_fleet_recall.managed_vehicles
)
"""

#: 30-day average miles per VIN.
#: Database: adp_${stage}_fleet_health (vehicle_utilisation_daily).
#: Reads the newest daily snapshot only.
_DAILY_USAGE_SQL = """\
SELECT
    vin,
    avg_miles_30d
FROM adp_${stage}_fleet_health.vehicle_utilisation_daily
WHERE snapshot_date = (
    SELECT MAX(snapshot_date)
    FROM adp_${stage}_fleet_health.vehicle_utilisation_daily
)
"""

# ---------------------------------------------------------------------------
# DynamoDB constants
# ---------------------------------------------------------------------------

_AGENT2_AGENT_ID = "agent-2-vehicle-health"
_RECALL_AGENT_ID = "agent-fi-recall-warranty"
_RECALL_FINDING_KIND = "fleet.recall.vehicle"
_RECALL_ACTION_KIND = "book_recall_service"

#: Severity bands that qualify for Agent 2 inclusion: soon and attention.
#:
#: ``p0`` is left out on purpose (user, prompt 299, option B; spec
#: 2026-11-05-avx-proactive-p0-alert, decisions "Option B"). An Agent 2 ``p0``
#: Finding is the diagnostics component's stop-driving Finding, whose text is the
#: P0 directive for the driver's phone. FI fleet health sees stop-driving faults
#: only through the ADP fault path (``select_fault_rows``, band ``p0``), so the
#: same fault never becomes a second FI booking need.
_AGENT2_SEVERITY_BANDS = frozenset({"attention", "soon"})


# ---------------------------------------------------------------------------
# Internal: stage validation (mirrors Stage C pattern)
# ---------------------------------------------------------------------------

def _validate_stage(stage: str) -> None:
    if stage not in ("staging", "prod"):
        raise ValueError(f"stage must be 'staging' or 'prod', got {stage!r}")


# ---------------------------------------------------------------------------
# Athena readers
# ---------------------------------------------------------------------------

def select_fault_rows(
    stage: str,
    *,
    athena_client: Any,
    workgroup: str,
    results_s3_path: str,
    _run_query: Callable = _default_run_query,
) -> list[dict]:
    """Return open faults (band soon/attention/p0) for managed vehicles.

    Columns: vin, code, fleet_id, band, last_seen_at, directive_text.
    Paging via NextToken is handled inside run_query.

    Mutation guard (stop after first page): the paging test stubs run_query to
    return rows simulating two pages; if the caller stops at page 1, fewer rows
    are returned and the assertion fails.
    """
    _validate_stage(stage)
    sql = _FAULT_SQL.replace("${stage}", stage)
    rows = _run_query(
        sql,
        athena_client=athena_client,
        workgroup=workgroup,
        results_s3_path=results_s3_path,
    )
    log.debug("select_fault_rows: stage=%s rows=%d", stage, len(rows))
    return rows


def select_pm_rows(
    stage: str,
    *,
    athena_client: Any,
    workgroup: str,
    results_s3_path: str,
    _run_query: Callable = _default_run_query,
) -> list[dict]:
    """Return PM tasks with status overdue or due for managed vehicles.

    Columns: vin, fleet_id, task_code, status.
    """
    _validate_stage(stage)
    sql = _PM_SQL.replace("${stage}", stage)
    rows = _run_query(
        sql,
        athena_client=athena_client,
        workgroup=workgroup,
        results_s3_path=results_s3_path,
    )
    log.debug("select_pm_rows: stage=%s rows=%d", stage, len(rows))
    return rows


def select_managed_vins(
    stage: str,
    *,
    athena_client: Any,
    workgroup: str,
    results_s3_path: str,
    _run_query: Callable = _default_run_query,
) -> list[dict]:
    """Return managed VINs and their fleet_id from fleet_recall.managed_vehicles.

    Reads the newest daily snapshot (WHERE snapshot_date = MAX(snapshot_date)).
    No tier filter — the table holds managed VINs only.

    Columns: vin, fleet_id.

    Mutation guard (table name): the SQL text test pins the table name;
    reverting to the old CMS replica view causes the test to fail.
    """
    _validate_stage(stage)
    sql = _MANAGED_VINS_SQL.replace("${stage}", stage)
    rows = _run_query(
        sql,
        athena_client=athena_client,
        workgroup=workgroup,
        results_s3_path=results_s3_path,
    )
    log.debug("select_managed_vins: stage=%s rows=%d", stage, len(rows))
    return rows


def select_daily_usage(
    stage: str,
    *,
    athena_client: Any,
    workgroup: str,
    results_s3_path: str,
    _run_query: Callable = _default_run_query,
) -> list[dict]:
    """Return 30-day average miles per VIN from vehicle_utilisation_daily.

    Reads the newest daily snapshot (WHERE snapshot_date = MAX(snapshot_date)).
    Columns: vin, avg_miles_30d (DOUBLE from Athena, returned as string).
    """
    _validate_stage(stage)
    sql = _DAILY_USAGE_SQL.replace("${stage}", stage)
    rows = _run_query(
        sql,
        athena_client=athena_client,
        workgroup=workgroup,
        results_s3_path=results_s3_path,
    )
    log.debug("select_daily_usage: stage=%s rows=%d", stage, len(rows))
    return rows


# ---------------------------------------------------------------------------
# DynamoDB: Agent 2 vehicle-health findings via by_fleet index
# ---------------------------------------------------------------------------

def select_agent2_findings(
    fleet_ids: list[str],
    *,
    ddb_client: Any,
    findings_table: str,
    now: datetime.datetime,
) -> tuple[dict[str, list[dict]], int]:
    """Return live Agent 2 vehicle-health findings keyed by VIN.

    Queries the ``by_fleet`` GSI for each fleet_id, filters for:
    - agent_id = 'agent-2-vehicle-health'
    - severity IN ('attention', 'soon')
    - expires_at > now (live only)

    Every returned item is checked against ``_AGENT2_SEVERITY_BANDS`` again, so a
    ``p0`` Finding (the diagnostics component's stop-driving Finding) never
    becomes an FI booking need even if the query returns it (option B, spec
    2026-11-05-avx-proactive-p0-alert). FI learns of stop-driving faults only
    from the ADP fault path.

    Pages each fleet query via ``LastEvaluatedKey`` to exhaustion.

    Mutation guard (no severity filter): routine-severity findings would appear
    in vehicle_rows.agent2_findings, inflating band scores.  The test
    ``test_agent2_severity_filter_excludes_routine`` fails when the filter is
    dropped.  Mutation guards (p0 let through, in the query or in the item
    check): ``test_agent2_p0_diagnostics_excluded.py``.

    Parameters
    ----------
    fleet_ids:
        List of fleet identifiers to query.  May be empty (returns {}).
    ddb_client:
        boto3 DynamoDB client (typed-format responses).
    findings_table:
        DynamoDB table name for the Findings store.
    now:
        Current UTC datetime; findings with expires_at <= now are excluded.

    Returns
    -------
    tuple[dict[vin, list[{severity, finding_id}]], int]
        (result, agent2_read_errors) — result is one entry per qualifying VIN;
        agent2_read_errors counts the number of fleet DDB queries that failed.
        W10: narrowed catch (ClientError/BotoCoreError) so programming errors
        propagate; each failing fleet is counted, not the whole batch.
    """
    from botocore.exceptions import BotoCoreError, ClientError

    now_iso = _to_iso(now)
    result: dict[str, list[dict]] = {}
    agent2_read_errors: int = 0

    for fleet_id in fleet_ids:
        kwargs: dict[str, Any] = {
            "TableName": findings_table,
            "IndexName": "by_fleet",
            "KeyConditionExpression": "fleet_id = :fid",
            "FilterExpression": (
                "agent_id = :aid"
                " AND severity IN (:s_att, :s_soon)"
                " AND expires_at > :now"
            ),
            "ExpressionAttributeValues": {
                ":fid": {"S": fleet_id},
                ":aid": {"S": _AGENT2_AGENT_ID},
                ":s_att": {"S": "attention"},
                ":s_soon": {"S": "soon"},
                ":now": {"S": now_iso},
            },
        }
        # W10-C: per-fleet exception handling so a single failing fleet does not
        #         abort the whole run.
        #         Mutation: remove try/except → fleet_2 error aborts fleet_1 data.
        try:
            while True:
                resp = ddb_client.query(**kwargs)
                for raw in resp.get("Items", []):
                    item = {k: _DESERIALIZER.deserialize(v) for k, v in raw.items()}
                    vin = item.get("vin")
                    if not vin:
                        continue
                    # Option B: only soon/attention, never p0 (see _AGENT2_SEVERITY_BANDS).
                    if item.get("severity") not in _AGENT2_SEVERITY_BANDS:
                        continue
                    result.setdefault(vin, []).append({
                        "finding_id": item.get("finding_id", ""),
                        "severity": item.get("severity", ""),
                        # W1: carry finding_kind so map_need_to_labor_op can key the
                        #     config by the real component (e.g. "health.battery").
                        "finding_kind": item.get("finding_kind", ""),
                    })
                last = resp.get("LastEvaluatedKey")
                if not last:
                    break
                kwargs["ExclusiveStartKey"] = last
        except (ClientError, BotoCoreError) as exc:
            # W10: narrow catch — programming errors propagate; only DDB/botocore
            # errors are swallowed per-fleet.  Each failing fleet is counted.
            # Mutation guard: revert to Exception → test_agent2_read_errors_counted fails.
            agent2_read_errors += 1
            log.warning(
                "select_agent2_findings: fleet %s DDB query failed (%s) — skipping: %s",
                fleet_id, type(exc).__name__, exc,
            )

    return result, agent2_read_errors


# ---------------------------------------------------------------------------
# DynamoDB: Stage C recall booking item via by_vin index
# ---------------------------------------------------------------------------

def recall_visit_fn(
    vin: str,
    *,
    ddb_client: Any,
    findings_table: str,
    now: datetime.datetime,
) -> Optional[dict]:
    """Return the live Stage C recall booking item for a VIN, or None.

    Queries the ``by_vin`` GSI for:
    - agent_id = 'agent-fi-recall-warranty'
    - finding_kind = 'fleet.recall.vehicle'
    - recommended_action.action_kind = 'book_recall_service'
    - expires_at > now  (live only)

    Pages via ``LastEvaluatedKey``.  Returns the first qualifying item as::

        {
            "finding_id": str,
            "recall_id": str,
            "rooftop_dealer_id": str | None,
        }

    Mutation guard (drop live filter): an expired recall booking is returned
    to the loop and the default dealer is set to the recall's dealer instead
    of the nearest stock dealer.  The test
    ``test_recall_visit_fn_excludes_expired`` injects an expired finding and
    asserts None is returned.
    """
    now_iso = _to_iso(now)
    kwargs: dict[str, Any] = {
        "TableName": findings_table,
        "IndexName": "by_vin",
        "KeyConditionExpression": "#v = :vin",
        "FilterExpression": (
            "agent_id = :aid"
            " AND finding_kind = :fk"
            " AND expires_at > :now"
        ),
        "ExpressionAttributeNames": {"#v": "vin"},
        "ExpressionAttributeValues": {
            ":vin": {"S": vin},
            ":aid": {"S": _RECALL_AGENT_ID},
            ":fk": {"S": _RECALL_FINDING_KIND},
            ":now": {"S": now_iso},
        },
    }
    while True:
        resp = ddb_client.query(**kwargs)
        for raw in resp.get("Items", []):
            item = {k: _DESERIALIZER.deserialize(v) for k, v in raw.items()}
            ra = item.get("recommended_action") or {}
            if ra.get("action_kind") != _RECALL_ACTION_KIND:
                continue
            params = ra.get("parameters") or {}
            recall_id = params.get("recall_id") or item.get("recall_id") or ""
            rooftop = params.get("dealer_id")
            return {
                # W3d: contract §1.6 names this field 'recall_item_finding_id'
                "recall_item_finding_id": item.get("finding_id", ""),
                "recall_id": recall_id,
                "rooftop_dealer_id": rooftop,
            }
        last = resp.get("LastEvaluatedKey")
        if not last:
            break
        kwargs["ExclusiveStartKey"] = last

    return None


# ---------------------------------------------------------------------------
# Assembler
# ---------------------------------------------------------------------------

def build_vehicle_rows(
    *,
    fault_rows: list[dict],
    pm_rows: list[dict],
    managed_vins: list[dict],
    usage_rows: list[dict],
    agent2_rows: dict[str, list[dict]],
) -> tuple[list[dict], dict]:
    """Assemble ``vehicle_rows`` for ``run_health_loop`` from pre-fetched reader data.

    Takes the raw row lists from the four Athena readers and the Agent 2 DDB
    reader.  Returns ``(vehicle_rows, counters)``.

    ``vehicle_rows`` is the union of all VINs with at least one need:
      - an open fault at band soon/attention/p0, OR
      - a PM task with status overdue or due, OR
      - a live Agent 2 finding with severity soon/attention (never p0, option B).

    Each vehicle_row carries::

        vin             (str)
        fleet_id        (str | None — from managed_vins or fault/pm row)
        open_faults     (list[{code, band, last_seen_at, fault_source}])
        pm_tasks        (list[{task_code, status}])
        agent2_findings (list[{finding_id, severity}])
        utilisation     ({avg_miles_30d: float} | None)
        catalog_entries ({code: {code, band, directive}} — for disclosure_txt)

    Fields ``vehicle_id`` and ``position`` are **not** set here; the runner
    (task 3.8) adds them via ``lookup_vehicle_cms_ids`` and
    ``lookup_fleet_locations``.

    Counters
    --------
    ``{managed_vin_count, fault_vin_count, pm_vin_count, agent2_vin_count,
       vehicle_rows_count}``
    """
    # --- Index managed VINs ---------------------------------------------------
    fleet_by_vin: dict[str, Optional[str]] = {}
    for row in managed_vins:
        vin = row.get("vin")
        if vin:
            fleet_by_vin[vin] = row.get("fleet_id")

    # --- Index fault rows by VIN ----------------------------------------------
    fault_by_vin: dict[str, list[dict]] = {}
    catalog_by_vin: dict[str, dict[str, dict]] = {}
    for row in fault_rows:
        vin = row.get("vin")
        if not vin:
            continue
        fault_by_vin.setdefault(vin, []).append({
            "code": row.get("code", ""),
            "band": row.get("band", ""),
            "last_seen_at": row.get("last_seen_at") or "",
            # C1 fix: carry fault_source so the health loop's fault need evidence
            # reflects the ADP source column ('generated'|'cms'|None for pre-column rows).
            "fault_source": row.get("fault_source"),
        })
        code = row.get("code", "")
        if code:
            catalog_by_vin.setdefault(vin, {})[code] = {
                "code": code,
                "band": row.get("band", ""),
                "directive": row.get("directive_text"),  # None for non-p0
            }

    # --- Index PM rows by VIN -------------------------------------------------
    pm_by_vin: dict[str, list[dict]] = {}
    for row in pm_rows:
        vin = row.get("vin")
        if not vin:
            continue
        pm_by_vin.setdefault(vin, []).append({
            "task_code": row.get("task_code", ""),
            "status": row.get("status", ""),
        })
        # Carry fleet_id from PM rows for VINs not in managed_vins (safety)
        if vin not in fleet_by_vin:
            fleet_by_vin[vin] = row.get("fleet_id")

    # --- Index daily usage by VIN ---------------------------------------------
    usage_by_vin: dict[str, Optional[float]] = {}
    for row in usage_rows:
        vin = row.get("vin")
        if not vin:
            continue
        raw = row.get("avg_miles_30d")
        try:
            usage_by_vin[vin] = float(raw) if raw is not None else None
        except (TypeError, ValueError):
            usage_by_vin[vin] = None

    # --- Union of all VINs with needs -----------------------------------------
    need_vins: set[str] = (
        set(fault_by_vin) | set(pm_by_vin) | set(agent2_rows)
    )

    vehicle_rows: list[dict] = []
    for vin in sorted(need_vins):          # sorted for determinism
        fleet_id = fleet_by_vin.get(vin)   # None if VIN not in managed_vins
        faults = fault_by_vin.get(vin) or []
        pm_tasks = pm_by_vin.get(vin) or []
        a2 = agent2_rows.get(vin) or []
        avg_miles = usage_by_vin.get(vin)
        # W3b: contract §1.6 names this field 'daily_average_miles_30d'.
        #      The SQL alias stays 'avg_miles_30d' (Athena column name);
        #      we rename at assembly time.
        utilisation: Optional[dict] = (
            {"daily_average_miles_30d": avg_miles} if avg_miles is not None else None
        )
        catalog = catalog_by_vin.get(vin) or {}

        vehicle_rows.append({
            "vin": vin,
            "fleet_id": fleet_id,
            "open_faults": faults,
            "pm_tasks": pm_tasks,
            "agent2_findings": a2,
            "utilisation": utilisation,
            "catalog_entries": catalog,
        })

    counters: dict[str, int] = {
        "managed_vin_count": len(fleet_by_vin),
        "fault_vin_count": len(fault_by_vin),
        "pm_vin_count": len(pm_by_vin),
        "agent2_vin_count": len(agent2_rows),
        "vehicle_rows_count": len(vehicle_rows),
    }

    log.debug(
        "build_vehicle_rows: managed=%d fault_vins=%d pm_vins=%d a2_vins=%d rows=%d",
        counters["managed_vin_count"],
        counters["fault_vin_count"],
        counters["pm_vin_count"],
        counters["agent2_vin_count"],
        counters["vehicle_rows_count"],
    )
    return vehicle_rows, counters


# ---------------------------------------------------------------------------
# Backward-compatibility shim
# ---------------------------------------------------------------------------

def select_vehicles_with_needs(
    athena_fn: Optional[Callable] = None,
    sql: str = "",
    **kwargs: Any,
) -> list[str]:
    """Shim kept for callers that pre-date task 3.2c.

    The old ``vehicle_needs_view`` query is gone.  This function now delegates
    to ``select_managed_vins`` when an ``athena_fn`` stub is supplied (the
    paging test path), or returns ``[]`` with a warning otherwise.

    Parameters
    ----------
    athena_fn:
        Callable with the OLD callback signature
        ``(sql, **kwargs) -> {"ResultSet": {"Rows": [...]}, "NextToken": ...}``.
        When provided, pages through NextToken as before so the pre-existing
        test ``test_athena_selection_follows_next_token`` remains valid after
        the test is repointed to the new fault SQL.
    sql:
        Ignored — the caller's SQL is not used.  The fault SQL is substituted.
    **kwargs:
        Forwarded to each ``athena_fn`` call.

    Returns
    -------
    list[str]
        All VINs extracted from the paged responses.
    """
    if athena_fn is None:
        log.warning(
            "select_vehicles_with_needs: no athena_fn provided — returning []. "
            "Use select_fault_rows / select_managed_vins for production."
        )
        return []

    all_vins: list[str] = []
    page_kwargs: dict[str, Any] = dict(kwargs)

    # Use the fault SQL (repointed away from vehicle_needs_view).
    query_sql = _FAULT_SQL.replace("${stage}", kwargs.get("stage", "staging"))

    while True:
        page = athena_fn(query_sql, **page_kwargs)
        rows = (page.get("ResultSet") or {}).get("Rows") or []
        for row in rows:
            vin = row.get("vin")
            if vin:
                all_vins.append(vin)
        next_token = page.get("NextToken")
        if not next_token:
            break
        page_kwargs["NextToken"] = next_token

    log.debug("select_vehicles_with_needs (shim): returned %d VINs", len(all_vins))
    return all_vins


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _to_iso(dt: datetime.datetime) -> str:
    """Return the ISO-8601 UTC string for *dt* (ending in 'Z')."""
    return (
        dt.astimezone(datetime.timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )
