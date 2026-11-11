"""Athena readers for the FI quality investigation agent — Stage D task 3.3.

SQL templates and reader functions for the investigation bundle.  All SQL and
column names live in this module; ``agents.tier2.adp_source.run_query`` handles
Athena mechanics (start, poll, paginate via ``NextToken``).

Tables read (spec D10 step 1, contract § 2.6)
---------------------------------------------
``adp_{stage}_engineering.fault_patterns``
    Pattern group rows and ranked candidates.

``adp_{stage}_engineering.vehicle_build_components``
    Build facts (part number, supplier, lot) per (VIN, component).

``adp_{stage}_engineering.engineering_failure_modes``
    Failure modes whose symptom codes include the pattern's code family
    and whose component appears in the affected VINs' build facts.

``adp_{stage}_engineering.engineering_remedy_options``
    Remedy options (joined to failure modes in the same query).

``adp_{stage}_vehicle_identity.vehicle_identity``
    Model/year/plant for affected VINs; also used for matched controls.

``adp_{stage}_fleet_health.vehicle_fault_status_daily``
    Co-occurring codes on the affected VINs (newest daily snapshot).

``adp_{stage}_ota_campaigns.ota_campaigns``
    Software campaigns on the same vehicles.

``adp_{stage}_ota_campaigns.ota_campaign_events``
    Per-VIN campaign membership (joined to campaigns in one query).

``adp_{stage}_fleet_recall.recall_campaigns_received``
    Recall catalog rows applicable to the affected VINs.

``adp_{stage}_fleet_recall.recall_vehicle_status``
    Per-VIN recall status (joined to campaigns in one query).

``adp_{stage}_service_records.service_records``
    Service history and parts used for affected VINs.

Telemetry aggregates (``adp_{stage}_vehicle_telemetry_aggregated``)
--------------------------------------------------------------------
Not queried in this reader set.  ADP telemetry data ends 2026-03-31
(spec § "What exists today"), before the 18-month fault-event backfill
window (starting mid-2025 for a 2026-10 run).  A query would return
empty results for all managed VINs in the generated cohort.
``read_telemetry_aggregates`` is provided as a no-op stub; task 4.x can
replace it when production telemetry resumes.

VIN safety
----------
VINs from ADP query results are embedded in subsequent SQL via
``_safe_vin_list``.  Every VIN is validated against the canonical pattern
``^[A-HJ-NPR-Z0-9]{17}$`` before embedding.  An invalid VIN raises
``ValueError`` so it is never silently dropped.

Stage substitution
------------------
SQL templates use ``${stage}`` as a placeholder; callers supply the
concrete stage string (``staging`` / ``prod``).  Each reader calls
``_sql(template, stage)`` which replaces the placeholder.

Mutation guards (verified by tests):
  * Stopping after the first ``GetQueryResults`` page silently drops rows
    when the result set spans multiple pages (two-page paging tests).
  * An ``adp_{stage}_engineering.*`` table name typo is detected by the
    exact SQL-text tests.
"""
from __future__ import annotations

import logging
import re
from typing import Any

from agents.tier2.adp_source import run_query

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# VIN validation
# ---------------------------------------------------------------------------

_VIN_RE = re.compile(r"^[A-HJ-NPR-Z0-9]{17}$")

# ---------------------------------------------------------------------------
# Stage substitution helper
# ---------------------------------------------------------------------------


def _sql(template: str, stage: str) -> str:
    """Substitute ``${stage}`` in *template* with the concrete *stage* value."""
    return template.replace("${stage}", stage)


# ---------------------------------------------------------------------------
# Athena array parsing (array<string> → Python list)
# ---------------------------------------------------------------------------

def _parse_csv_array(raw: str | None) -> list[str]:
    """Parse Athena's ``array_join`` CSV or ``[...]`` array representation.

    Athena renders ``array<string>`` as ``[item1, item2, ...]`` in
    ``GetQueryResults``.  When the column is projected with
    ``array_join(col, ',')`` it arrives as a plain CSV string.  Both
    forms are handled here.

    Returns an empty list for NULL / empty values.
    """
    if not raw:
        return []
    raw = raw.strip()
    if raw in ("[]", "", "null", "NULL"):
        return []
    # Strip outer brackets if present (Athena array literal).
    if raw.startswith("[") and raw.endswith("]"):
        raw = raw[1:-1]
    return [item.strip().strip("'\"") for item in raw.split(",") if item.strip()]


def _safe_vin_list(vins: list[str]) -> str:
    """Return a SQL-safe ``'VIN1', 'VIN2', …`` literal for an IN clause.

    Raises ``ValueError`` if any VIN fails the canonical format check so
    that invalid values are never silently dropped or embedded in SQL.

    An empty list raises ``ValueError`` because ``IN ()`` is a syntax error.
    """
    if not vins:
        raise ValueError("VIN list must not be empty for an IN clause")
    validated: list[str] = []
    for vin in vins:
        if not _VIN_RE.match(vin):
            raise ValueError(
                f"VIN {vin!r} does not match the canonical VIN pattern; "
                "cannot embed in SQL"
            )
        validated.append(vin)
    return ", ".join(f"'{v}'" for v in validated)


# ---------------------------------------------------------------------------
# SQL templates
# ---------------------------------------------------------------------------

#: All rows for the pattern group (ranked candidates).
#: Database: adp_${stage}_engineering (fault_patterns)
#:
#: Mutation guard (test_pattern_sql_names_adp_engineering):
#:   Using a wrong database name (e.g. adp_${stage}_fleet_health) or
#:   table name means the query targets the wrong table; the SQL text
#:   test pins the exact qualified name.
#: Mutation guard (test_two_page_paging_pattern_group):
#:   Stopping after the first GetQueryResults page silently drops rows
#:   when the result set spans multiple pages.
_PATTERN_SQL = """\
SELECT
    pattern_id,
    code_family,
    cohort_dimension,
    cohort_value,
    CAST(candidate_rank AS VARCHAR)       AS candidate_rank,
    pattern_group_id,
    CAST(vehicles_affected AS VARCHAR)    AS vehicles_affected,
    CAST(vehicles_in_service AS VARCHAR)  AS vehicles_in_service,
    CAST(rate AS VARCHAR)                 AS rate,
    CAST(rate_rest AS VARCHAR)            AS rate_rest,
    CAST(rate_ratio AS VARCHAR)           AS rate_ratio,
    CAST(p_value AS VARCHAR)              AS p_value,
    CAST(first_detected_month AS VARCHAR) AS first_detected_month,
    trend,
    method,
    array_join(affected_vins_sample, ',') AS affected_vins_sample_csv,
    CAST(computed_at AS VARCHAR)          AS computed_at,
    parameters_version
FROM adp_${stage}_engineering.fault_patterns
WHERE pattern_group_id = '${pattern_id}'
ORDER BY candidate_rank ASC\
"""

#: Affected VINs with build facts and component lots.
#: Tables: adp_${stage}_engineering.vehicle_build_components,
#:         adp_${stage}_vehicle_identity.vehicle_identity
#:
#: VINs are embedded as a pre-validated IN literal built by _safe_vin_list.
_AFFECTED_VINS_SQL = """\
SELECT
    vbc.vin,
    vi.model,
    CAST(vi.model_year AS VARCHAR)             AS model_year,
    CAST(vi.manufacture_date AS VARCHAR)       AS build_month,
    vi.assembly_plant                          AS plant,
    vbc.component_id,
    vbc.supplier_id,
    vbc.supplier_lot,
    vbc.part_number
FROM adp_${stage}_engineering.vehicle_build_components vbc
JOIN adp_${stage}_vehicle_identity.vehicle_identity vi
    ON vbc.vin = vi.vin
WHERE vbc.vin IN (${vins})\
"""

#: Matched controls — same model and year, not in the affected set,
#: managed VINs only.
#: Tables: adp_${stage}_vehicle_identity.vehicle_identity,
#:         adp_${stage}_fleet_recall.managed_vehicles (newest snapshot)
_CONTROLS_SQL = """\
SELECT
    vi.vin,
    vi.model,
    CAST(vi.model_year AS VARCHAR)       AS model_year,
    CAST(vi.manufacture_date AS VARCHAR) AS build_month,
    vi.assembly_plant                    AS plant
FROM adp_${stage}_vehicle_identity.vehicle_identity vi
WHERE vi.model = '${model}'
  AND vi.model_year = ${model_year}
  AND vi.vin NOT IN (${excluded_vins})
  AND vi.vin IN (
      SELECT vin
      FROM adp_${stage}_fleet_recall.managed_vehicles
      WHERE snapshot_date = (
          SELECT MAX(snapshot_date)
          FROM adp_${stage}_fleet_recall.managed_vehicles
      )
  )
LIMIT ${cap}\
"""

#: Co-occurring codes — other open fault codes on the affected VINs
#: (excluding the pattern's own code family).
#: Table: adp_${stage}_fleet_health.vehicle_fault_status_daily (newest snapshot)
_CO_OCCURRING_SQL = """\
SELECT
    code,
    CAST(COUNT(DISTINCT vin) AS VARCHAR) AS affected_vehicles
FROM adp_${stage}_fleet_health.vehicle_fault_status_daily
WHERE vin IN (${vins})
  AND snapshot_date = (
      SELECT MAX(snapshot_date)
      FROM adp_${stage}_fleet_health.vehicle_fault_status_daily
  )
  AND status = 'open'
  AND NOT starts_with(code, '${code_family}')
GROUP BY code
ORDER BY COUNT(DISTINCT vin) DESC
LIMIT 20\
"""

#: Failure modes whose component appears in the affected VINs' build
#: components, joined to remedy options.
#: Tables: adp_${stage}_engineering.engineering_failure_modes,
#:         adp_${stage}_engineering.engineering_remedy_options
#:
#: Python-side filtering further restricts to failure modes whose
#: symptom_codes overlap the pattern's code_family (prefix match).
_FAILURE_MODES_SQL = """\
SELECT
    fm.failure_mode_id,
    fm.component_id,
    fm.mechanism,
    array_join(fm.symptom_codes, ',')       AS symptom_codes_csv,
    fm.cause_dimension,
    CAST(fm.do_not_drive AS VARCHAR)        AS do_not_drive,
    CAST(fm.park_outside AS VARCHAR)        AS park_outside,
    ro.remedy_option_id,
    ro.remedy_type,
    CAST(ro.parts_cost_per_vehicle_usd AS VARCHAR) AS parts_cost_per_vehicle,
    CAST(ro.labor_hours_per_vehicle AS VARCHAR)    AS labor_hours_per_vehicle,
    CAST(ro.lead_time_weeks AS VARCHAR)             AS lead_time_weeks,
    CAST(ro.over_the_air AS VARCHAR)               AS over_the_air,
    ro.trade_off_notes
FROM adp_${stage}_engineering.engineering_failure_modes fm
JOIN adp_${stage}_engineering.engineering_remedy_options ro
    ON fm.failure_mode_id = ro.failure_mode_id
${component_filter}ORDER BY fm.failure_mode_id, ro.remedy_option_id\
"""

#: Existing software campaigns on the affected VINs.
#: Tables: adp_${stage}_ota_campaigns.ota_campaign_events,
#:         adp_${stage}_ota_campaigns.ota_campaigns
#: Columns are the tables' own (ADP ota_campaigns/schema.yaml; campaign_id is the header's
#: partition key). The header has no ``component``: selecting it failed every investigation
#: (issue 2026-10-20-fi-quality-campaign-reads-name-missing-columns; pinned by
#: tests/test_campaign_reads_real_columns.py, which runs this SQL on the live columns).
_OTA_CAMPAIGNS_SQL = """\
SELECT DISTINCT
    h.campaign_id,
    h.campaign_name,
    h.status,
    h.release_version
FROM adp_${stage}_ota_campaigns.ota_campaign_events e
JOIN adp_${stage}_ota_campaigns.ota_campaigns h
    ON e.campaign_id = h.campaign_id
WHERE e.vin IN (${vins})\
"""

#: Existing recall campaigns on the affected VINs (Stage C's catalog).
#: Tables: adp_${stage}_fleet_recall.recall_vehicle_status,
#:         adp_${stage}_fleet_recall.recall_campaigns_received
#: The received view has no name or status column (ADP recall_campaigns/interface.md § 1-2):
#: a recall is its id and component. Same issue and test as above.
_RECALL_CAMPAIGNS_SQL = """\
SELECT DISTINCT
    rvs.campaign_id,
    rcr.component,
    rvs.vin
FROM adp_${stage}_fleet_recall.recall_vehicle_status rvs
JOIN adp_${stage}_fleet_recall.recall_campaigns_received rcr
    ON rvs.campaign_id = rcr.campaign_id
WHERE rvs.vin IN (${vins})\
"""

#: Service history for the affected VINs — qualifying repairs and parts.
#: Table: adp_${stage}_service_records.service_records
_SERVICE_HISTORY_SQL = """\
SELECT
    vin,
    CAST(service_date AS VARCHAR) AS service_date,
    service_type,
    array_join(dtc_codes, ',')     AS dtc_codes_csv,
    CAST(total_cost_usd AS VARCHAR) AS total_cost_usd,
    outcome
FROM adp_${stage}_service_records.service_records
WHERE vin IN (${vins})
ORDER BY service_date DESC\
"""

# ---------------------------------------------------------------------------
# Default caps
# ---------------------------------------------------------------------------

#: Maximum matched control VINs per investigation.
_CONTROLS_CAP: int = 50

#: Maximum affected VINs passed into follow-up queries (VIN set size).
_VIN_QUERY_CAP: int = 50


# ---------------------------------------------------------------------------
# Reader functions
# ---------------------------------------------------------------------------


def read_pattern_group(
    pattern_id: str,
    stage: str,
    *,
    athena_client: Any,
    workgroup: str,
    results_s3_path: str,
) -> list[dict[str, Any]]:
    """Read all candidate rows for the pattern group identified by *pattern_id*.

    Queries ``adp_{stage}_engineering.fault_patterns`` for all rows with
    ``pattern_group_id = pattern_id``.  Returns them sorted by
    ``candidate_rank`` (ascending, i.e. rank 1 = top candidate first).

    Returns an empty list when no rows exist — the caller (run_investigation)
    treats an empty result as an unknown pattern and raises PatternNotFound.

    Mutation guard (test_two_page_paging_pattern_group):
        Stopping after the first ``GetQueryResults`` page misses rows on
        subsequent pages.  The test stubs two pages and asserts both are
        consumed.

    Parameters
    ----------
    pattern_id:
        The pattern's group identifier (equals the top-ranked candidate's
        own ``pattern_id`` per the engineering interface § 6).
    stage:
        Deployment stage (``staging`` / ``prod``).
    athena_client, workgroup, results_s3_path:
        Injected by the caller; passed through to ``run_query``.
    """
    sql = (
        _sql(_PATTERN_SQL, stage)
        .replace("${pattern_id}", pattern_id)
    )
    rows = run_query(
        sql,
        athena_client=athena_client,
        workgroup=workgroup,
        results_s3_path=results_s3_path,
    )
    return [_coerce_pattern_row(r) for r in rows]


def read_affected_vins(
    vins: list[str],
    stage: str,
    *,
    athena_client: Any,
    workgroup: str,
    results_s3_path: str,
) -> list[dict[str, Any]]:
    """Read build facts for *vins* from ``vehicle_build_components``.

    Joins ``vehicle_build_components`` to ``vehicle_identity`` to add
    model, model year, manufacture date and plant.  One row per
    (VIN, component_id).

    *vins* must be non-empty; each must match the canonical VIN pattern.

    Mutation guard (test_two_page_paging_affected_vins):
        Stopping after the first page silently drops VINs for large
        affected sets.
    """
    vin_literal = _safe_vin_list(vins[:_VIN_QUERY_CAP])
    sql = (
        _sql(_AFFECTED_VINS_SQL, stage)
        .replace("${vins}", vin_literal)
    )
    rows = run_query(
        sql,
        athena_client=athena_client,
        workgroup=workgroup,
        results_s3_path=results_s3_path,
    )
    return rows


def read_controls(
    model: str,
    model_year: int | str,
    excluded_vins: list[str],
    stage: str,
    *,
    athena_client: Any,
    workgroup: str,
    results_s3_path: str,
    cap: int = _CONTROLS_CAP,
) -> list[dict[str, Any]]:
    """Read matched controls — same model and year, not in the affected set.

    Queries ``vehicle_identity`` joined to ``fleet_recall.managed_vehicles``
    (newest snapshot; managed VINs only) and excludes the affected VINs.

    When *excluded_vins* is empty (no affected VINs in the pattern group
    sample), returns up to *cap* VINs with no exclusion.

    Mutation guard (test_two_page_paging_controls):
        Stopping after the first page silently returns fewer controls.
    """
    # Build the NOT IN clause; if excluded_vins is empty, use a dummy that
    # never matches (Athena requires at least one value in IN).
    if excluded_vins:
        exc_literal = _safe_vin_list(excluded_vins[:_VIN_QUERY_CAP])
    else:
        exc_literal = "'__no_exclusion__'"

    sql = (
        _sql(_CONTROLS_SQL, stage)
        .replace("${model}", model.replace("'", "''"))
        .replace("${model_year}", str(int(model_year)))
        .replace("${excluded_vins}", exc_literal)
        .replace("${cap}", str(cap))
    )
    rows = run_query(
        sql,
        athena_client=athena_client,
        workgroup=workgroup,
        results_s3_path=results_s3_path,
    )
    return rows


def read_co_occurring_codes(
    vins: list[str],
    code_family: str,
    stage: str,
    *,
    athena_client: Any,
    workgroup: str,
    results_s3_path: str,
) -> list[str]:
    """Return other open fault codes on the affected VINs.

    Excludes the pattern's own code family (prefix match).  Returns a list
    of distinct code strings ordered by the number of affected vehicles
    (descending), capped at 20.

    Returns an empty list if *vins* is empty.

    Mutation guard (test_two_page_paging_co_occurring_codes):
        Stopping after the first page drops low-frequency codes from the
        result.
    """
    if not vins:
        return []
    vin_literal = _safe_vin_list(vins[:_VIN_QUERY_CAP])
    # code_family is a 4-char OBD prefix (e.g. P0AA); safe to embed directly.
    safe_family = code_family[:4] if code_family else ""
    sql = (
        _sql(_CO_OCCURRING_SQL, stage)
        .replace("${vins}", vin_literal)
        .replace("${code_family}", safe_family)
    )
    rows = run_query(
        sql,
        athena_client=athena_client,
        workgroup=workgroup,
        results_s3_path=results_s3_path,
    )
    return [r["code"] for r in rows if r.get("code")]


def read_failure_modes(
    code_family: str,
    component_ids: list[str],
    stage: str,
    *,
    athena_client: Any,
    workgroup: str,
    results_s3_path: str,
) -> list[dict[str, Any]]:
    """Read failure modes for *component_ids* whose symptom codes overlap *code_family*.

    Queries ``engineering_failure_modes`` joined to ``engineering_remedy_options``.
    Filters in Python for modes whose ``symptom_codes`` contain a code that
    starts with *code_family* (4-char prefix).

    Returns a list of failure mode dicts, each with a nested ``remedy_options``
    list.  Options are in catalog order (as returned by Athena).

    With an empty *component_ids* (no ``supplier_lot`` candidate names a component), every
    failure mode is read and matched by symptom codes only (decisions.md "Group 3 review
    cycle 3" answer 7): a plant or build-window pattern still finds the failure mode whose
    symptoms it shows.  Rows are ordered by ``failure_mode_id``, ``remedy_option_id`` so
    "catalog order" (the rules fallback recommends the first option) is the same on every
    run; Athena guarantees no order without ``ORDER BY``.

    Mutation guard (test_two_page_paging_failure_modes):
        Stopping after the first page silently drops failure modes or options
        from the result.
    """
    if component_ids:
        # component_ids are ADP identifiers (e.g. "hv_battery_module") — no quoting risk.
        comp_literal = ", ".join(
            f"'{_safe_component_id(c)}'" for c in component_ids
        )
        component_filter = f"WHERE fm.component_id IN ({comp_literal})\n"
    else:
        component_filter = ""
    sql = (
        _sql(_FAILURE_MODES_SQL, stage)
        .replace("${component_filter}", component_filter)
    )
    rows = run_query(
        sql,
        athena_client=athena_client,
        workgroup=workgroup,
        results_s3_path=results_s3_path,
    )
    return _group_failure_mode_rows(rows, code_family)


def read_campaigns(
    vins: list[str],
    stage: str,
    *,
    athena_client: Any,
    workgroup: str,
    results_s3_path: str,
) -> dict[str, list[dict[str, Any]]]:
    """Return recall and OTA campaigns on the affected VINs.

    Queries:
      * ``adp_{stage}_ota_campaigns`` — software campaigns
      * ``adp_{stage}_fleet_recall`` — Stage C's recall catalog

    Returns a dict ``{"recall": [...], "ota": [...]}`` where each entry
    is a distinct campaign dict.  Returns empty lists when *vins* is empty.

    Mutation guard (test_two_page_paging_campaigns):
        Stopping after the first page silently drops campaigns from the
        result.
    """
    if not vins:
        return {"recall": [], "ota": []}

    vin_literal = _safe_vin_list(vins[:_VIN_QUERY_CAP])

    ota_sql = _sql(_OTA_CAMPAIGNS_SQL, stage).replace("${vins}", vin_literal)
    recall_sql = _sql(_RECALL_CAMPAIGNS_SQL, stage).replace("${vins}", vin_literal)

    ota_rows = run_query(
        ota_sql,
        athena_client=athena_client,
        workgroup=workgroup,
        results_s3_path=results_s3_path,
    )
    recall_rows = run_query(
        recall_sql,
        athena_client=athena_client,
        workgroup=workgroup,
        results_s3_path=results_s3_path,
    )
    return {
        "recall": recall_rows,
        "ota": ota_rows,
    }


def read_service_history(
    vins: list[str],
    stage: str,
    *,
    athena_client: Any,
    workgroup: str,
    results_s3_path: str,
) -> list[dict[str, Any]]:
    """Return service records for the affected VINs.

    Queries ``adp_{stage}_service_records.service_records``.  ``dtc_codes``
    is serialised by Athena as ``[CODE1, CODE2, ...]``; this reader returns
    the raw string under ``dtc_codes_csv``.

    Returns an empty list when *vins* is empty.
    """
    if not vins:
        return []

    vin_literal = _safe_vin_list(vins[:_VIN_QUERY_CAP])
    sql = _sql(_SERVICE_HISTORY_SQL, stage).replace("${vins}", vin_literal)
    rows = run_query(
        sql,
        athena_client=athena_client,
        workgroup=workgroup,
        results_s3_path=results_s3_path,
    )
    return rows


def read_telemetry_aggregates(
    vins: list[str],  # noqa: ARG001
    stage: str,       # noqa: ARG001
    *,
    athena_client: Any,    # noqa: ARG001
    workgroup: str,        # noqa: ARG001
    results_s3_path: str,  # noqa: ARG001
) -> list[dict[str, Any]]:
    """Return telemetry aggregates for the affected VINs — stub, always empty.

    ADP telemetry data ends 2026-03-31 (spec § "What exists today"), before
    the 18-month fault-event backfill window.  Querying
    ``adp_{stage}_vehicle_telemetry_aggregated.vehicle_telemetry_aggregated``
    would return empty results for all managed (MRD) VINs.

    Task 4.x can replace this stub with a real query when production
    telemetry resumes.  The investigation bundle always contains this
    key; a non-empty return is simply an additional evidence entry.
    """
    log.debug(
        "read_telemetry_aggregates: telemetry data ends 2026-03-31; "
        "returning empty list (task 4.x follow-on)"
    )
    return []


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _coerce_pattern_row(row: dict[str, Any]) -> dict[str, Any]:
    """Coerce string columns from Athena to their canonical Python types."""
    def _int(val: Any, default: int = 0) -> int:
        try:
            return int(val)
        except (TypeError, ValueError):
            return default

    def _float(val: Any, default: float = 0.0) -> float:
        try:
            return float(val)
        except (TypeError, ValueError):
            return default

    return {
        **row,
        "candidate_rank":     _int(row.get("candidate_rank"), 1),
        "vehicles_affected":  _int(row.get("vehicles_affected"), 0),
        "vehicles_in_service": _int(row.get("vehicles_in_service"), 0),
        "rate":               _float(row.get("rate"), 0.0),
        "rate_rest":          _float(row.get("rate_rest"), 0.0),
        "rate_ratio":         _float(row.get("rate_ratio"), 0.0),
        "p_value":            _float(row.get("p_value"), 1.0),
        "affected_vins_sample": _parse_csv_array(row.get("affected_vins_sample_csv")),
    }


def _group_failure_mode_rows(
    rows: list[dict[str, Any]],
    code_family: str,
) -> list[dict[str, Any]]:
    """Group flat (failure_mode, option) rows into nested dicts.

    Filters failure modes whose ``symptom_codes_csv`` contains a code
    starting with the 4-char ``code_family`` prefix.

    Returns one dict per failure mode, each with nested ``remedy_options``.
    """
    prefix4 = code_family[:4] if code_family else ""
    modes: dict[str, dict[str, Any]] = {}

    for row in rows:
        fm_id = row.get("failure_mode_id") or ""
        if fm_id not in modes:
            symptom_codes = _parse_csv_array(row.get("symptom_codes_csv"))
            # Filter: only failure modes whose symptoms overlap code_family.
            if prefix4:
                has_overlap = any(c.startswith(prefix4) for c in symptom_codes)
                if not has_overlap:
                    continue
            modes[fm_id] = {
                "failure_mode_id": fm_id,
                "component_id":    row.get("component_id", ""),
                "mechanism":       row.get("mechanism", ""),
                "symptom_codes":   symptom_codes,
                "cause_dimension": row.get("cause_dimension", ""),
                "do_not_drive":    row.get("do_not_drive", "").lower() == "true",
                "park_outside":    row.get("park_outside", "").lower() == "true",
                "remedy_options":  [],
            }

        opt_id = row.get("remedy_option_id")
        if opt_id and fm_id in modes:
            modes[fm_id]["remedy_options"].append({
                "remedy_option_id":        opt_id,
                "remedy_type":             row.get("remedy_type", ""),
                "parts_cost_per_vehicle":  _to_float(row.get("parts_cost_per_vehicle")),
                "labor_hours_per_vehicle": _to_float(row.get("labor_hours_per_vehicle")),
                "lead_time_weeks":         _to_int(row.get("lead_time_weeks")),
                "over_the_air":            (row.get("over_the_air") or "").lower() == "true",
                "trade_off_notes":         row.get("trade_off_notes") or "",
            })

    return list(modes.values())


def _to_float(val: Any) -> float | None:
    if val is None:
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def _to_int(val: Any) -> int | None:
    if val is None:
        return None
    try:
        return int(val)
    except (TypeError, ValueError):
        return None


def _safe_component_id(component_id: str) -> str:
    """Validate and return a component_id safe for SQL embedding.

    Component ids are ADP-defined identifiers (lowercase, underscores,
    e.g. ``hv_battery_module``).  Only letters, digits and underscores
    are allowed.  A non-conforming value raises ``ValueError``.
    """
    if not re.match(r"^[a-zA-Z0-9_]+$", component_id):
        raise ValueError(
            f"Component id {component_id!r} contains characters not safe "
            "for SQL embedding"
        )
    return component_id
