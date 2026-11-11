"""
Selection step for the fi_cost Tier 2 component — D6 step 1.

Spec: .kiro/specs/2026-10-02-fi-cost-stage-b/spec.md § D6 step 1
Decisions: decisions.md 2026-10-05 item 6 (energy-cost outlier rule; cap
           placement; selection is is_outlier OR is_energy_outlier)

Queries ``vehicle_cost_baseline`` for managed vehicles that are
``is_outlier OR is_energy_outlier`` in the latest complete month, ordered by
the larger of total excess cost and ``energy_excess_usd``.

``max_diagnoses_per_run`` is NOT applied here.  It caps model calls after
D6 step 3's unchanged-skip, so vehicles a run did not reach are taken by the
next daily run.  The query returns the full anomaly set.

(decisions.md 2026-10-05, item 6: "max_diagnoses_per_run caps model calls
after D6 step 3's skip, not the selection query.")

Input validation (defence in depth — stage is interpolated into SQL):
``stage`` must match ``^(staging|prod)$``; raises ``ValueError`` otherwise.

Athena mechanics (polling, NextToken paging) are delegated to
``agents.tier2.adp_source.run_query``.  This module owns only the SQL and
the column names, consistent with the health-component pattern.
"""

from __future__ import annotations

import datetime
import logging
import re
from pathlib import Path
from typing import Any

from agents.tier2.adp_source import run_query

log = logging.getLogger(__name__)

# SQL template lives beside this module in sql/select_anomalies.sql so that
# both the template and the ADP column names are co-located.
_SQL_PATH: Path = Path(__file__).parent / "sql" / "select_anomalies.sql"
_SQL_TEMPLATE: str = _SQL_PATH.read_text()

# Validation pattern for stage (defence in depth — interpolated into SQL).
_STAGE_RE = re.compile(r"^(staging|prod)$")


def _render_sql(stage: str, year_month: str) -> str:
    """Render the SQL template for a given stage and year_month."""
    sql = _SQL_TEMPLATE
    sql = sql.replace("${stage}", stage)
    sql = sql.replace("${year_month}", year_month)
    return sql


_LATEST_MONTH_SQL_TEMPLATE: str = (Path(__file__).parent / "sql" / "latest_month.sql").read_text()

_MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


def previous_calendar_month(now: datetime.datetime) -> str:
    """``"YYYY-MM"`` of the calendar month before ``now`` (UTC)."""
    if now.tzinfo is not None:
        now = now.astimezone(datetime.timezone.utc)
    year, month = now.year, now.month - 1
    if month == 0:
        year, month = year - 1, 12
    return f"{year:04d}-{month:02d}"


def find_latest_complete_month(
    athena_client: Any,
    workgroup: str,
    results_s3_path: str,
    *,
    now: datetime.datetime,
    stage: str = "staging",
    max_polls: int | None = None,
) -> str | None:
    """The newest data month in ``fleet_cost_events`` up to the month before ``now``.

    Returns ``None`` when no month exists yet (nothing published), so the run can say
    so instead of reporting an empty queue as "nothing to do".  Raises ``ValueError``
    for a stage outside ``^(staging|prod)$`` before any Athena call.
    """
    if not _STAGE_RE.match(stage):
        raise ValueError(f"stage must be 'staging' or 'prod', got {stage!r}")
    through = previous_calendar_month(now)
    sql = (
        _LATEST_MONTH_SQL_TEMPLATE
        .replace("${stage}", stage)
        .replace("${through_month}", through)
    )
    kwargs: dict[str, Any] = {}
    if max_polls is not None:
        kwargs["max_polls"] = max_polls
    rows = run_query(
        sql=sql,
        athena_client=athena_client,
        workgroup=workgroup,
        results_s3_path=results_s3_path,
        **kwargs,
    )
    value = rows[0].get("year_month") if rows else None
    if value is None or value == "":
        return None
    if not _MONTH_RE.match(str(value)):
        # A malformed partition value is a data defect; never template it into SQL.
        raise ValueError(f"latest month query returned {value!r}, not YYYY-MM")
    return str(value)


def select_anomalies(
    athena_client: Any,
    workgroup: str,
    results_s3_path: str,
    max_diagnoses_per_run: int,
    latest_complete_month: str,
    stage: str = "staging",
    max_polls: int | None = None,
) -> list[dict[str, Any]]:
    """Query vehicle_cost_baseline for cost anomalies in the latest complete month.

    Returns vehicles with ``is_outlier OR is_energy_outlier``, ordered by the
    larger of total excess cost and ``energy_excess_usd`` (decisions.md
    2026-10-05, item 6).

    The result is NOT capped: ``max_diagnoses_per_run`` is accepted for API
    compatibility but is applied by the runner (D6 step 3), not here.
    This ensures later daily runs reach vehicles that earlier runs skipped.

    Each row dict has keys matching the SELECT aliases in select_anomalies.sql:
    ``vin``, ``fleet_id``, ``year_month``, ``ratio_cohort``, ``ratio_own``,
    ``severity``, ``is_outlier``, ``is_energy_outlier``, ``energy_excess_usd``,
    etc.

    Paging via ``NextToken`` is handled by ``adp_source.run_query`` — the
    health-agent live-verification lesson (agentic-tiers.md): single-page reads
    silently truncate and produce wrong ``inputs_hash`` values.

    Parameters
    ----------
    athena_client:
        Boto3 Athena client or stub.
    workgroup:
        Athena workgroup name (e.g. ``"cvx-staging-analytics"``).
    results_s3_path:
        S3 URI for query results.
    max_diagnoses_per_run:
        Accepted for API compatibility; NOT applied to the query.
        Applied by the runner at D6 step 3 (decisions.md 2026-10-05, item 6).
    latest_complete_month:
        ``"YYYY-MM"`` of the latest complete month to check.
    stage:
        ADP stage name, used to template the database names (default
        ``"staging"``).  Must be ``"staging"`` or ``"prod"``; any other
        value raises ``ValueError`` before the Athena call.

    Returns
    -------
    list[dict[str, Any]]
        One dict per anomaly vehicle.  Empty list when no vehicles qualify.

    Raises
    ------
    ValueError
        If ``latest_complete_month`` is not in ``YYYY-MM`` format or if
        ``stage`` does not match ``^(staging|prod)$``.
    """
    if not re.match(r"^\d{4}-(0[1-9]|1[0-2])$", latest_complete_month):
        raise ValueError(
            f"latest_complete_month must be YYYY-MM, got {latest_complete_month!r}"
        )

    # Validate stage before interpolating it into SQL (defence in depth).
    if not _STAGE_RE.match(stage):
        raise ValueError(
            f"stage must be 'staging' or 'prod', got {stage!r}"
        )

    sql = _render_sql(stage=stage, year_month=latest_complete_month)

    log.info(
        "fi_cost.selection: querying vehicle_cost_baseline "
        "year_month=%s workgroup=%s",
        latest_complete_month,
        workgroup,
    )

    kwargs: dict[str, Any] = {}
    if max_polls is not None:
        kwargs["max_polls"] = max_polls
    rows = run_query(
        sql=sql,
        athena_client=athena_client,
        workgroup=workgroup,
        results_s3_path=results_s3_path,
        **kwargs,
    )

    # max_diagnoses_per_run is NOT applied here per decisions.md 2026-10-05.
    # The _ prefix suppresses linter warnings about the unused variable.
    _ = max_diagnoses_per_run

    log.info(
        "fi_cost.selection: selected %d anomaly vehicles for %s",
        len(rows),
        latest_complete_month,
    )
    return rows
