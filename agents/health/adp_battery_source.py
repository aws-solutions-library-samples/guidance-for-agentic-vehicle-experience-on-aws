"""Battery health data source — queries ADP ``energy_usage`` for ``state_of_health_pct``.

This is the **only** module in the CVX codebase that holds battery SQL or ADP battery
column names.  Any SQL or ADP column name appearing outside this module is a review
failure (spec § D3a).

ADP source: ``energy_usage``
-----------------------------
``energy_usage.state_of_health_pct`` is the correct column.  It is generated from
``battery_age_days`` with a ~2% annual decline plus ±0.5% noise, producing genuinely
age-correlated per-vehicle variation (range 85.76–100.0, stddev ~4.0 at 10M rows /
100k VINs, verified 2026-08-03 by T1.2).

Do **NOT** use ``vehicle_telemetry_aggregated.state_of_health_pct``.  That column is
driven by a global row index and is effectively identical for every VIN (fleet-wide span
~95→90).  Using it makes per-owner battery judgment impossible while appearing correct
(spec § D3a).

What this module returns
------------------------
Per-VIN summary rows from ``energy_usage``, translated to **agent-facing vocabulary**
so the caller (the health agent) never sees ADP column names:

ADP column              → agent-facing key
──────────────          ──────────────────
state_of_health_pct     → health_pct
battery_age_days        → age_days
vin                     → vin            (unchanged; not ADP-specific)

The translation is performed in :func:`_translate_row` and applied in
:func:`fetch_findings` before the rows leave this module.

CVX computes no thresholds and fits no degradation
model (spec § D3a, Q7).

The honest output on current ADP data is **reassurance**: every synthetic vehicle is
well above any typical warranty floor (~70%), so the agent will say "your battery is
at 92%, degrading normally for its age" rather than manufacturing urgency that the
data does not support (spec § D3a stated limitations).
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any, Optional

from agents.health._managed_pool import COUNT_LIMIT as MANAGED_POOL_COUNT_LIMIT
from agents.health._managed_pool import COUNT_MAX_POLLS as MANAGED_POOL_COUNT_MAX_POLLS
from agents.health._managed_pool import (
    COUNT_MAX_QUEUE_SECONDS as MANAGED_POOL_COUNT_MAX_QUEUE_SECONDS,
)
from agents.health._managed_pool import build_count_sql as build_managed_pool_count_sql
from agents.health._managed_pool import parse_count as parse_managed_pool_count
from agents.health._vin import sanitise_vin
from agents.tier2.adp_source import run_query
from agents.tier2.snapshot_freshness import (
    read_snapshot_dates as _read_snap_dates,
    require_fresh as _require_fresh_snap,
)

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# ADP source product — the sole location this name appears in CVX code.
# A test in agents/health/tests/test_adp_battery_source.py asserts this
# constant equals "energy_usage" so the source cannot silently drift.
# ---------------------------------------------------------------------------

ADP_PRODUCT: str = "energy_usage"

# ---------------------------------------------------------------------------
# SQL — the sole location battery SQL lives in CVX code.
# Column names: vin, state_of_health_pct, battery_age_days from energy_usage.
# No thresholds, no degradation model — ADP already inferred; CVX judges.
# ---------------------------------------------------------------------------

_BATTERY_SQL_TEMPLATE: str = """
SELECT
    vin,
    state_of_health_pct,
    battery_age_days
FROM {database}.energy_usage
WHERE vin = '{vin}'
ORDER BY usage_date DESC, battery_age_days DESC
LIMIT 1
""".strip()

#: Batch SQL for the energy_usage product is built per-call by
#: :meth:`AdpBatterySource._build_sql`.
#:
#: Historical note (removed 2026-10-04 review cycle 1 W5): two module-level
#: constants ``_BATCH_SQL_BASE`` and ``_BATCH_SQL_MAX_VINS_CLAUSE`` existed
#: alongside the inline builder.  They were unused — ``_build_sql`` composed
#: its own string inline — and their comments described them as the live
#: query, which was misleading.  A reader editing the constants to change
#: the query would change nothing.  The builder is the single source of
#: truth; see :meth:`AdpBatterySource._build_sql` below.
#:
#: Per-VIN deduplication is **inside** the SQL via
#: ``ROW_NUMBER() OVER (PARTITION BY vin ORDER BY usage_date DESC) AS rn``
#: with an outer ``WHERE rn = 1``.  Only the latest row per VIN leaves Athena,
#: so the result-set size is bounded by VIN count, not by rows-per-VIN.
#:
#: The historical shape (no PARTITION, no rn filter) assumed ~100 rows/VIN —
#: the sparse ``energy_usage`` product — and relied on the 50,000-row Athena
#: result cap being far above ``max_vins * rows_per_vin``.  The 2026-10-04
#: ``energy_usage`` densify (10.0M → 54.8M rows, ~548 rows/VIN) breaks that
#: assumption: ``375 vins * 548 rows ≈ 205k rows`` against a 50k cap, and the
#: nightly battery run died with ``AthenaResultTooLargeError`` on 2026-10-04
#: 02:00:37Z and 02:02:03Z.  See ``issues/2026-10-04-tier2-nightly-runs-broken/``.


# ---------------------------------------------------------------------------
# Class API (batch / FindingSource protocol)
# ---------------------------------------------------------------------------


class AdpBatterySource:
    """Fetch per-VIN battery findings from ADP ``energy_usage`` (batch interface).

    Implements the ``FindingSource`` protocol consumed by
    ``agents.tier2.runner``.

    Lifetime: one instance per Tier 2 run.  AWS clients are injected so test
    doubles can be supplied without network access.

    Parameters
    ----------
    stage:
        Deployment stage — ``"staging"`` or ``"prod"``.  Used to resolve the
        Glue database name ``adp_{stage}_energy_usage``.
    athena_client:
        A boto3 Athena client initialised in ``us-east-1`` (ADP's catalog
        region for both CVX stages).
    workgroup:
        Athena workgroup name, e.g. ``"cvx-staging-analytics"``.
    results_s3_path:
        S3 URI for Athena query results.
    max_polls:
        Forwarded to :func:`agents.tier2.adp_source.run_query`; controls the
        Athena poll ceiling.
    poll_interval:
        Sleep between poll attempts (seconds).  Set to ``0`` in unit tests.
    max_vins:
        When no explicit VIN list is supplied via :meth:`fetch_findings`, bound
        the query to this many VINs via ``ORDER BY vin`` (which provides
        pagination order).  ``None`` means no bound (full fleet — only safe at
        very small fleet sizes).  Defaults to ``25`` which suits the default
        per-run budget.
    max_rows:
        Hard ceiling on total result rows forwarded to
        :func:`agents.tier2.adp_source.run_query`.  When ``None``, the default
        in ``adp_source._DEFAULT_MAX_ROWS`` applies.  The Tier 2 runner passes
        ``TIER2_MAX_ROWS_PER_QUERY`` from the environment so the stack-configured
        ceiling is actually enforced rather than ignored (spec T6b.1).
    """

    def __init__(
        self,
        stage: str = "staging",
        *,
        athena_client: Any,
        workgroup: str,
        results_s3_path: str,
        max_polls: int = 60,
        poll_interval: float = 1.0,
        max_vins: Optional[int] = 25,
        max_rows: Optional[int] = None,
    ) -> None:
        self._stage = stage
        self._athena_client = athena_client
        self._workgroup = workgroup
        self._results_s3_path = results_s3_path
        self._max_polls = max_polls
        self._poll_interval = poll_interval
        self._max_vins = max_vins
        self._max_rows = max_rows
        self._database = f"adp_{stage}_energy_usage"
        # G3F6F1.5 Part 2B: freshness check done at most once per instance.
        self._managed_freshness_checked: bool = False

    # -----------------------------------------------------------------------
    # FindingSource protocol
    # -----------------------------------------------------------------------

    def fetch_findings(
        self,
        vins: Optional[list[str]] = None,
    ) -> list[dict[str, Any]]:
        """Return agent-facing battery readings for the given VINs (or a bounded
        subset via ``ORDER BY vin`` when no VIN list is supplied).

        Translates ADP ``energy_usage`` column names into agent-facing vocabulary
        at this boundary so the agent never sees an ADP column name.

        **ADP column → agent-facing key mapping:**

        ``vin``                 → ``vin``              (unchanged)
        ``state_of_health_pct`` → ``health_pct``
        ``battery_age_days``    → ``age_days``

        Parameters
        ----------
        vins:
            Optional list of VINs to restrict the query.  If ``None`` (default),
            the query returns all VINs bounded by ``max_vins`` with
            ``ORDER BY vin`` for pagination order.  Idempotency is NOT a
            property of which VINs are selected; it comes from per-VIN
            content-addressing in ``runner.py:355`` (spec § D1).

        Returns
        -------
        list[dict]
            One dict per (VIN, age reading) with agent-facing keys.  May be empty.
        """
        sql = self._build_sql(vins)
        log.debug(
            "adp_battery_source: querying energy_usage stage=%s vins=%s",
            self._stage,
            len(vins) if vins else "all",
        )
        kwargs: dict = dict(
            athena_client=self._athena_client,
            workgroup=self._workgroup,
            results_s3_path=self._results_s3_path,
            max_polls=self._max_polls,
            poll_interval=self._poll_interval,
        )
        if self._max_rows is not None:
            kwargs["max_rows"] = self._max_rows
        raw_rows = run_query(sql, **kwargs)
        return [_to_agent_row(r) for r in raw_rows]

    # -----------------------------------------------------------------------
    # SQL builder (internal — ADP column names isolated here)
    # -----------------------------------------------------------------------

    def _build_sql(self, vins: Optional[list[str]]) -> str:
        """Construct the Athena SQL for the energy_usage product.

        SQL and ADP column names are isolated here.  Nothing outside this
        module may reference ``energy_usage``, ``state_of_health_pct``, or
        ``battery_age_days``.

        Per-VIN latest-row deduplication is **inside** the SQL
        (``ROW_NUMBER() OVER (PARTITION BY vin ORDER BY usage_date DESC)``
        with an outer ``WHERE rn = 1``), so only one row per VIN is returned
        regardless of how many rows ADP ``energy_usage`` carries for that VIN.
        The 2026-10-04 densify (10.0M → 54.8M rows, ~548 rows/VIN) made this
        a hard correctness requirement — the prior "fetch everything then let
        the agent sort it out" shape blew the Athena 50,000-row ceiling.

        The VIN filter is applied **inside** the window-function subquery so
        the window only partitions over the requested VINs — cheaper and
        bounds the intermediate row set.

        When *vins* is provided the query filters to those VINs exactly.
        When *vins* is ``None`` and ``max_vins`` is set, the query uses
        ``ORDER BY vin LIMIT {max_vins}`` for stable **pagination order**.
        Idempotency is NOT a property of which VINs are selected; it comes
        from per-VIN content-addressing in ``runner.py:355``, where
        ``inputs_hash`` is computed over the rows for a single VIN and the
        conditional put deduplicates at write time (spec § D1).
        """
        if vins:
            # Explicit VIN list takes precedence over max_vins.
            safe_vins = [sanitise_vin(v) for v in vins]
            vin_list = ", ".join(f"'{v}'" for v in safe_vins)
            inner_filter = f"\n  WHERE vin IN ({vin_list})"
        elif self._max_vins is not None:
            # ORDER BY vin provides pagination order for the max_vins bound.
            # TABLESAMPLE and rand() are explicitly forbidden — both break the
            # pagination invariant that select_vins relies on.
            inner_filter = (
                "\n  WHERE vin IN ("
                "SELECT vin FROM ("
                f"SELECT DISTINCT vin FROM {self._database}.energy_usage "
                f"ORDER BY vin LIMIT {self._max_vins}"
                "))"
            )
        else:
            # No VIN filter: full fleet.  Only safe for very small fleets.
            inner_filter = ""

        return (
            "SELECT vin, state_of_health_pct, battery_age_days\n"
            "FROM (\n"
            "  SELECT vin, state_of_health_pct, battery_age_days,\n"
            # Task 3.3 (2026-10-22): order by usage_date DESC so the newest
            # calendar day always wins, even when battery_age_days is non-monotone
            # (ties or decreases due to data quality).  battery_age_days DESC is
            # the deterministic second key for rows that share a usage_date (e.g.
            # duplicate (vin, usage_date) pairs from additive lake publishes per
            # ADP issue 2026-08-31-adp-curated-cross-run-partition-accumulation),
            # keeping rn=1 and inputs_hash stable across re-runs.
            "         ROW_NUMBER() OVER (PARTITION BY vin ORDER BY usage_date DESC, battery_age_days DESC) AS rn\n"
            f"  FROM {self._database}.energy_usage"
            f"{inner_filter}\n"
            ") t\n"
            "WHERE rn = 1"
        )

    # -----------------------------------------------------------------------
    # Selection API (FindingSource protocol — rotation only)
    # -----------------------------------------------------------------------

    def _build_select_managed_vins_sql(
        self, *, cursor: Optional[str], limit: int
    ) -> str:
        """Construct the SQL that selects managed VINs from the battery rotation pool.

        Battery selection is rotation only — there is no priority predicate.
        Managed VINs come from ``fleet_recall.managed_vehicles`` (newest
        snapshot; no tier filter — the table holds managed VINs only).

        The JOIN keeps both table references inside this module (isolation rule,
        spec § D3).  ``ORDER BY eu.vin`` provides stable pagination order.

        Parameters
        ----------
        cursor:
            Last managed VIN from the previous batch, or ``None`` to start fresh.
        limit:
            Maximum number of VINs to return.
        """
        recall_db = f"adp_{self._stage}_fleet_recall"
        cursor_clause = ""
        if cursor is not None:
            safe_cursor = sanitise_vin(cursor)
            cursor_clause = f" AND eu.vin > '{safe_cursor}'"
        return (
            f"SELECT DISTINCT eu.vin"
            f" FROM {self._database}.energy_usage eu"
            f" INNER JOIN {recall_db}.managed_vehicles mv ON mv.vin = eu.vin"
            f" AND mv.snapshot_date = ("
            f"SELECT MAX(snapshot_date)"
            f" FROM {recall_db}.managed_vehicles)"
            f" WHERE TRUE"
            f"{cursor_clause}"
            f" ORDER BY eu.vin"
            f" LIMIT {limit}"
        )

    def select_managed_vins(
        self,
        *,
        cursor: Optional[str],
        limit: int,
    ) -> tuple[list[str], Optional[str]]:
        """Return up to *limit* managed VINs from the battery rotation pool.

        Managed VINs are those in ``fleet_recall.managed_vehicles`` (newest
        snapshot; no tier filter) that also have energy-usage data (spec § D7).
        Battery has no priority predicate — selection is rotation only.

        The JOIN SQL lives inside this module only (isolation rule, spec § D3).

        Returns ``([], None)`` when the managed pool is exhausted.

        Parameters
        ----------
        cursor:
            Last managed VIN from the previous batch, or ``None`` to start fresh.
        limit:
            Maximum number of VINs to return.
        """
        sql = self._build_select_managed_vins_sql(cursor=cursor, limit=limit)
        log.debug(
            "adp_battery_source.select_managed_vins: stage=%s cursor_present=%s limit=%d",
            self._stage,
            cursor is not None,
            limit,
        )
        if not self._managed_freshness_checked:
            import datetime as _dt
            _today = _dt.datetime.now(_dt.timezone.utc).date()
            _dates = _read_snap_dates(
                self._stage, ["managed_vehicles"],
                athena_client=self._athena_client,
                workgroup=self._workgroup,
                results_s3_path=self._results_s3_path,
            )
            _require_fresh_snap(_dates, today=_today)
            self._managed_freshness_checked = True
        kwargs: dict = dict(
            athena_client=self._athena_client,
            workgroup=self._workgroup,
            results_s3_path=self._results_s3_path,
            max_polls=self._max_polls,
            poll_interval=self._poll_interval,
        )
        if self._max_rows is not None:
            kwargs["max_rows"] = self._max_rows
        try:
            raw_rows = run_query(sql, **kwargs)
        except AttributeError:
            if self._athena_client is None:
                return [], None
            raise
        vins = [row["vin"] for row in raw_rows if row.get("vin")]
        next_cursor = vins[-1] if vins else None
        return vins, next_cursor

    def count_managed_vins(self, *, after: Optional[str]) -> tuple[int, int]:
        """Return ``(managed_pool, at_or_before)`` for the pool :meth:`select_managed_vins` pages.

        One Athena query over the same managed-pool SQL (``agents.health._managed_pool``), used
        by selection to write the run's 7-day fallback record (stage D U7). *after* is the run's
        managed cursor; ``at_or_before`` counts the managed VINs sorting at or before it. Raises on
        any failure; selection then records the run's coverage as unmeasured.
        """
        sql = build_managed_pool_count_sql(
            self._build_select_managed_vins_sql(cursor=None, limit=MANAGED_POOL_COUNT_LIMIT),
            after=after,
        )
        rows = run_query(
            sql,
            athena_client=self._athena_client,
            workgroup=self._workgroup,
            results_s3_path=self._results_s3_path,
            max_polls=min(self._max_polls, MANAGED_POOL_COUNT_MAX_POLLS),
            poll_interval=self._poll_interval,
            max_queue_seconds=MANAGED_POOL_COUNT_MAX_QUEUE_SECONDS,
        )
        return parse_managed_pool_count(rows)

    def _build_select_vins_sql(self, *, cursor: Optional[str], limit: int) -> str:
        """Construct the keyset-paginated VIN selection SQL for battery rotation.

        Battery has NO priority pool predicate.  The only available ADP signals
        for battery are ``state_of_health_pct`` and ``battery_age_days``, both
        continuous.  Any numeric cut-off applied here would be CVX inference —
        a spec § D2/D3 violation regardless of how reasonable the threshold looks.

        The correct fix is upstream: ADP publishing a categorical battery health
        band alongside ``state_of_health_pct`` (follow-on: ADP battery health
        category).  Until that signal exists, battery selection is rotation only.

        ``ORDER BY vin`` provides stable pagination order for the keyset cursor.
        It does NOT protect ``inputs_hash`` — idempotency is per-VIN
        content-addressing in ``runner.py:355`` (spec § D1).

        Parameters
        ----------
        cursor:
            Last VIN processed in the previous run.  ``None`` means start from
            the beginning of the fleet.
        limit:
            Maximum number of VINs to return.
        """
        # Simple shape per Group 2 note: a bare SELECT DISTINCT vin ... ORDER BY vin LIMIT N.
        # The tire/brake sources inline a correlated subquery in their own fetch
        # SQL; here the selection and fetch shapes differ, so the query is built
        # directly rather than reusing a module-level constant.
        if cursor is None:
            return (
                f"SELECT DISTINCT vin"
                f" FROM {self._database}.energy_usage"
                f" ORDER BY vin"
                f" LIMIT {limit}"
            )
        safe_cursor = sanitise_vin(cursor)
        return (
            f"SELECT DISTINCT vin"
            f" FROM {self._database}.energy_usage"
            f" WHERE vin > '{safe_cursor}'"
            f" ORDER BY vin"
            f" LIMIT {limit}"
        )

    def select_vins(
        self, *, cursor: Optional[str], limit: int
    ) -> tuple[list[str], Optional[str]]:
        """Return up to *limit* VINs after *cursor* in the rotation pool, plus the next cursor.

        Battery selection is **rotation only** — there is no priority pool.
        See :meth:`_build_select_vins_sql` for the rationale.

        Returns ``([], None)`` when the pool is exhausted, which the caller
        reads as "wrap to the start".

        Parameters
        ----------
        cursor:
            Last VIN processed, or ``None`` to start from the beginning.
        limit:
            Maximum number of VINs to return.

        Returns
        -------
        tuple[list[str], str | None]
            A list of VIN strings (may be empty) and the next cursor (the last
            VIN in the batch, or ``None`` if the batch is empty).
        """
        sql = self._build_select_vins_sql(cursor=cursor, limit=limit)
        kwargs: dict = dict(
            athena_client=self._athena_client,
            workgroup=self._workgroup,
            results_s3_path=self._results_s3_path,
            max_polls=self._max_polls,
            poll_interval=self._poll_interval,
        )
        if self._max_rows is not None:
            kwargs["max_rows"] = self._max_rows
        raw_rows = run_query(sql, **kwargs)
        vins = [r["vin"] for r in raw_rows if r.get("vin")]
        next_cursor: Optional[str] = vins[-1] if vins else None
        return vins, next_cursor


# ---------------------------------------------------------------------------
# Public API (legacy per-VIN function — kept for backward compatibility)
# ---------------------------------------------------------------------------


def fetch_findings(
    vin: str,
    *,
    athena_client: Any,
    workgroup: str,
    results_s3_path: str,
    database: str,
) -> dict:
    """Query ADP ``energy_usage`` for battery health data for *vin*.

    Returns a findings dict with the raw ADP rows, an ``inputs_hash`` for
    idempotent artifact writes (spec § D4/M6), and structured evidence entries.

    The caller (the health agent) performs all judgment: what the SoH means for
    this owner, how urgent (if at all) it is, and what to say.  CVX computes no
    thresholds here (spec § D3a).

    Parameters
    ----------
    vin:
        Vehicle Identification Number to query.
    athena_client:
        A boto3 Athena client (or stub).  Injected so tests can supply a stubbed
        client without network access.
    workgroup:
        Athena workgroup, e.g. ``cvx-staging-analytics``.
    results_s3_path:
        S3 URI for query output, e.g.
        ``s3://cvx-staging-athena-results-{account}-us-east-1/tier2/``.
    database:
        Glue database name for the ADP ``energy_usage`` product, e.g.
        ``adp_staging_energy_usage``.

    Returns
    -------
    dict with keys:
        ``vin``          — the queried VIN
        ``rows``         — list of normalised row dicts from ``energy_usage``
                           (empty list when no data for this VIN)
        ``inputs_hash``  — SHA-256 over the normalised rows; stable across
                           re-runs over unchanged data (spec § D4/M6)
        ``evidence``     — list of evidence entries (spec § D4 provenance contract)

    Raises
    ------
    ``agents.tier2.adp_source.AthenaCursorError``
        Propagated if the Athena query reaches FAILED or CANCELLED.
    ``agents.tier2.adp_source.AthenaTimeoutError``
        Propagated if the Athena query does not complete within the poll ceiling.
    """
    # Log count only — never the VIN.  Tier 2 runs unattended; CloudWatch is
    # the widest exposure surface.  See security-review Cycle 7 Warning W3 +
    # the platform's test-enforced "no VINs in logs" contract in
    # agents/tier2/tests/test_budget.py::test_no_vins_in_budget_exception_repr.
    log.debug("adp_battery_source: querying energy_usage vin_count=1")

    # Allow-list the VIN before embedding it in the SQL literal.
    # Athena has no parameterised-query API; sanitise_vin rejects any
    # character outside [A-Z0-9-] to prevent SQL injection.
    # Security-review Cycle 7 W7 — uniform defence across all three sources.
    safe_vin = sanitise_vin(vin)
    sql = _BATTERY_SQL_TEMPLATE.format(database=database, vin=safe_vin)
    rows = run_query(
        sql,
        athena_client=athena_client,
        workgroup=workgroup,
        results_s3_path=results_s3_path,
    )

    # Hash and evidence are built from the RAW ADP rows so `inputs_hash` stays a
    # function of upstream data, not of our naming choices.
    inputs_hash = _compute_hash(rows)
    evidence = _build_evidence(rows)

    return {
        "vin": vin,
        "rows": [_to_agent_row(r) for r in rows],
        "inputs_hash": inputs_hash,
        "evidence": evidence,
    }


def _to_agent_row(raw: dict) -> dict:
    """Translate one ADP row into this module's agent-facing vocabulary.

    The agent must never see an ADP column name: spec § D3 makes each source a
    swappable interface, so an upstream rename has to stop here rather than
    reaching `agent.py`.  The tire source already worked this way; battery did
    not, and `test_agent_reads_only_keys_the_sources_emit` caught it.
    """
    return {
        "vin": raw.get("vin"),
        "health_pct": raw.get("state_of_health_pct"),
        "age_days": raw.get("battery_age_days"),
    }


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _compute_hash(rows: list[dict]) -> str:
    """Return a stable SHA-256 hex digest over *rows*.

    Normalisation (spec § D4):
    1. Sort rows by ``(vin, battery_age_days)`` for a stable ordering.
    2. Serialise each row's numeric fields with 6 decimal places of precision.
    3. Serialise to JSON with sorted keys, no extra whitespace.
    4. Hash the UTF-8 bytes of the joined JSON strings.

    An empty row list (no ADP data for this VIN) hashes to a fixed sentinel
    so the artifact writer can still record an ``absent``-provenance finding
    idempotently.
    """
    if not rows:
        return hashlib.sha256(b"energy_usage:empty").hexdigest()

    normalised = []
    for row in sorted(rows, key=lambda r: (r.get("vin", ""), r.get("battery_age_days", ""))):
        entry: dict = {}
        for k, v in sorted(row.items()):
            # Serialise numeric strings with fixed precision for hash stability.
            try:
                entry[k] = f"{float(v):.6f}" if v is not None else None
            except (TypeError, ValueError):
                entry[k] = v
        normalised.append(entry)

    serialised = json.dumps(normalised, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(serialised.encode()).hexdigest()


def _build_evidence(rows: list[dict]) -> list[dict]:
    """Build evidence entries from ADP rows (spec § D4 provenance contract).

    Each evidence entry carries ``source``, ``value``, and
    ``provenance ∈ {live, absent}``.  S1 has no mocked inputs so every
    entry is ``live`` (data present) or ``absent`` (no rows for this VIN).
    """
    if not rows:
        return [
            {
                "source": "energy_usage.state_of_health_pct",
                "value": None,
                "provenance": "absent",
            },
            {
                "source": "energy_usage.battery_age_days",
                "value": None,
                "provenance": "absent",
            },
        ]

    # Use the first (most recent by ORDER BY usage_date DESC) row.
    row = rows[0]
    return [
        {
            "source": "energy_usage.state_of_health_pct",
            "value": row.get("state_of_health_pct"),
            "provenance": "live",
        },
        {
            "source": "energy_usage.battery_age_days",
            "value": row.get("battery_age_days"),
            "provenance": "live",
        },
    ]
