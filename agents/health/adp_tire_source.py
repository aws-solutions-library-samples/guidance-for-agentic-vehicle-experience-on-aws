"""Tire finding source — reads ADP ``tire_health`` via Athena.

**This is the only module in the CVX codebase where tire SQL and ADP
tire column names may appear.** Any other CVX file referencing
``tire_health``, ``needs_replacement``, ``wear_category``, or
``wear_rate_mm_per_1k_km`` is a spec § D3 violation and will fail the
review scanner.

Spec
----
See ``.kiro/specs/2026-08-03-cvx-tier2-platform-health-agent/spec.md`` § D3
and ``decisions.md`` § "T1.2 gate: tires are NOT buildable; battery is."

Data product
------------
ADP ``tire_health``, Glue catalog ``adp_{stage}_tire_health``.

**Unblocked 2026-08-04** when ADP published 6,000,800 rows of
``tire_health`` data.  Prior to that date this module was planned but
not buildable (see T1.2 gate, ``decisions.md`` § 2026-08-03).

Grain
-----
One row per ``(vin, tire_position, event_date)``.  The four tire
positions are ``FL`` (front-left), ``FR`` (front-right), ``RL``
(rear-left), ``RR`` (rear-right).

ADP-computed columns consumed
------------------------------
CVX reads — and does not recompute — three columns that ADP derives:

``needs_replacement``
    **Supervised label, not a deployed model output.**  ADP's own
    ``tire_health/schema.yaml:84`` describes this as: *"Supervised
    label: true when tread_depth_mm < 2.0 OR pressure anomaly indicates
    structural risk."*  This is the ML training target, not the output
    of an inference pipeline.  CVX consumes it as a data-platform signal
    and does not treat it as a prediction.  See spec § D3's stated
    limitation; the upgrade path (ADP publishing predictions as a
    catalogued product) is a separate ADP-side initiative.

``wear_category``
    ADP-computed ordinal categorisation of tread wear.  CVX reads the
    value; it does not recategorise or threshold it.

``wear_rate_mm_per_1k_km``
    ADP-computed metric describing rate of tread wear.  CVX reads the
    value; it does not extrapolate wear, predict remaining life, or fit
    any model.

CVX constraint: no thresholds, no models, no wear extrapolation
---------------------------------------------------------------
This module computes **nothing**.  It fetches the rows ADP already
produced.  Any calculation, threshold comparison, or model fit belongs
in ADP's inference pipeline.  If ``fetch_findings`` appears to need a
threshold to do its job, the seam is wrong — stop and escalate rather
than adding one.

Cross-account Athena access
---------------------------
ADP's lake and Glue catalog live in ``us-east-1`` for both CVX stages,
even though CVX staging compute runs in ``us-west-2``.  The caller must
supply an Athena client initialised in ``us-east-1``; do not derive the
region from Lambda's own runtime region.  The workgroup and results
bucket are resolved by the stack and injected as environment variables
(``ATHENA_WORKGROUP``, ``ATHENA_RESULTS_BUCKET``) per spec § "The
environment contract between the stack and the runner".

Access shape
------------
No ``assume_role`` is needed; the CVX execution role already has
cross-account Athena grants via the workgroup and results bucket,
proven end-to-end by ``scripts/verify-tier2-adp-reads.py``.

boto3 clients are injected by the caller so unit tests can supply
``botocore.stub.Stubber``-backed stubs without network access.
"""

from __future__ import annotations

import datetime as _dt
import logging
from typing import Any, Optional

from agents.health._managed_pool import COUNT_LIMIT as MANAGED_POOL_COUNT_LIMIT
from agents.health._managed_pool import COUNT_MAX_POLLS as MANAGED_POOL_COUNT_MAX_POLLS
from agents.health._managed_pool import (
    COUNT_MAX_QUEUE_SECONDS as MANAGED_POOL_COUNT_MAX_QUEUE_SECONDS,
)
from agents.health._managed_pool import build_count_sql as build_managed_pool_count_sql
from agents.health._managed_pool import parse_count as parse_managed_pool_count
from agents.health._vin import sanitise_vin as _shared_sanitise_vin
from agents.tier2.adp_source import run_query
from agents.tier2.snapshot_freshness import (
    read_snapshot_dates as _read_snap_dates,
    require_fresh as _require_fresh_snap,
)

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Glue product identifier
# ---------------------------------------------------------------------------

#: The ADP Glue database name pattern.  ``{stage}`` is substituted at call
#: time.  This string is the canonical source of the product name for this
#: module — test_adp_tire_source.py asserts the SQL references it.
_GLUE_PRODUCT = "tire_health"

# ---------------------------------------------------------------------------
# Tire positions — the four valid values in ADP's schema
# ---------------------------------------------------------------------------

#: All valid tire positions in ``tire_health``.  The query filters to these
#: so unexpected schema additions do not silently expand the row set.
TIRE_POSITIONS: frozenset[str] = frozenset({"FL", "FR", "RL", "RR"})

# ---------------------------------------------------------------------------
# Read window (prompt 361, issue 2026-11-09-tire-read-no-date-bound)
# ---------------------------------------------------------------------------

#: Days of ``tire_health`` the per-VIN read covers, today (UTC) included. ADP's daily
#: tire job (ADP spec ``2026-10-22-adp-tire-daily``) adds four rows per car per day,
#: so an unbounded read grows by 4 rows per VIN a day. The tier2 stack sizes the
#: nightly read as ``TIER2_MAX_VINS_TIRES`` (156) x 60 days x 4 tires = 37,440 rows
#: under ``TIER2_MAX_ROWS_PER_QUERY`` (50,000); this window keeps that sizing true.
TIRE_READ_WINDOW_DAYS = 60


def _utc_today() -> _dt.date:
    """Today's UTC date (one place, so tests can pin the clock)."""
    return _dt.datetime.now(_dt.timezone.utc).date()


def _read_window_start(today: _dt.date) -> _dt.date:
    """First ``event_date`` the read includes: ``TIRE_READ_WINDOW_DAYS`` dates up to today."""
    return today - _dt.timedelta(days=TIRE_READ_WINDOW_DAYS - 1)


# ---------------------------------------------------------------------------
# Public class
# ---------------------------------------------------------------------------


class AdpTireSource:
    """Fetch per-VIN tire findings from ADP ``tire_health``.

    Lifetime: one instance per Tier 2 run.  The caller supplies AWS clients
    so test doubles can be injected without network access.

    Parameters
    ----------
    stage:
        Deployment stage — ``"staging"`` or ``"prod"``.
    athena_client:
        A boto3 Athena client initialised in ``us-east-1`` (ADP's catalog
        region for both CVX stages).
    workgroup:
        Athena workgroup name, e.g. ``"cvx-staging-analytics"``.
    results_s3_path:
        S3 URI for Athena query results, e.g.
        ``"s3://cvx-staging-athena-results-{account}-us-east-1/tier2/"``.
    max_polls:
        Forwarded to :func:`agents.tier2.adp_source.run_query`; controls
        the Athena poll ceiling.
    poll_interval:
        Forwarded to :func:`agents.tier2.adp_source.run_query`; sleep
        between poll attempts (seconds).  Set to ``0`` in unit tests.
    max_vins:
        When no explicit VIN list is supplied via :meth:`fetch_findings`,
        bound the query to this many VINs via ``ORDER BY vin`` (which
        provides pagination order).  ``None`` means no bound (full fleet).
        Defaults to ``25`` which suits the default per-run budget.
    max_rows:
        Hard ceiling on total result rows forwarded to
        :func:`agents.tier2.adp_source.run_query`.  When ``None``, the default
        in ``adp_source._DEFAULT_MAX_ROWS`` applies.  The Tier 2 runner passes
        ``TIER2_MAX_ROWS_PER_QUERY`` from the environment so the stack-configured
        ceiling is actually enforced rather than ignored (spec T6b.1).
    """

    def __init__(
        self,
        stage: str,
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
        # G3F6F1.5 Part 2B: freshness check done at most once per instance.
        self._managed_freshness_checked: bool = False

    # -----------------------------------------------------------------------
    # FindingSource protocol
    # -----------------------------------------------------------------------

    def fetch_findings(
        self,
        vins: Optional[list[str]] = None,
    ) -> list[dict[str, Any]]:
        """Return agent-facing tire readings for the given VINs.
        at this boundary so the agent never sees an ADP column name.

        **ADP column → agent-facing key mapping** (translation lives here; no
        other CVX file may reference these ADP names as dict keys):

        ``vin``               → ``vin``              (unchanged; not an ADP-specific name)
        ``tire_position``     → ``position``
        ``event_date``        → ``event_date``        (unchanged; generic calendar field)
        ``needs_replacement`` → ``replacement_flagged``   (bool; None when ADP NULL)
        ``wear_category``     → ``wear_band``             (string; ADP-assigned ordinal)
        ``wear_rate_mm_per_1k_km`` → ``wear_rate_per_1k_km``  (string; cast by caller)

        Parameters
        ----------
        vins:
            Optional list of VINs to restrict the query.  If ``None`` or
            empty, all VINs in the product are returned (fleet-wide batch).

        Returns
        -------
        list[dict]
            One dict per tire reading with agent-facing keys.  May be empty
            if no data exists for the requested VINs.
        """
        sql = self._build_sql(vins)
        log.debug(
            "adp_tire_source: querying tire_health stage=%s vins=%s",
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
        return [self._normalise_row(r) for r in raw_rows]

    # -----------------------------------------------------------------------
    # Internal helpers
    # -----------------------------------------------------------------------

    def _build_sql(self, vins: Optional[list[str]]) -> str:
        """Construct the Athena SQL for the tire_health product.

        SQL and ADP column names are isolated here.  Nothing outside this
        module may reference ``tire_health``, ``needs_replacement``,
        ``wear_category``, or ``wear_rate_mm_per_1k_km``.

        When *vins* is provided the query filters to those VINs exactly.
        When *vins* is ``None`` and ``max_vins`` is set, the query uses the
        deterministic distinct-VIN subquery ``ORDER BY vin LIMIT {max_vins}``.
        ``ORDER BY vin`` provides **pagination order** — it is not what makes
        idempotency hold.  Idempotency comes from per-VIN content-addressing in
        ``runner.py:355``: ``inputs_hash`` is computed over the rows for a
        *single* VIN inside the per-VIN loop, and the conditional put at
        ``artifact_writer.py:87`` guards on ``attribute_not_exists(inputs_hash)``.
        A different VIN set therefore produces artifacts for those VINs, each
        written at most once per distinct input content.  Do **not** replace
        ``ORDER BY vin`` with TABLESAMPLE or rand() — both break the pagination
        invariant that ``select_vins`` relies on.
        """
        db = f"adp_{self._stage}_{_GLUE_PRODUCT}"
        # A DATE literal built from a date object (never caller text). event_date is the
        # table's DATE partition key, so this also prunes partitions outside the window.
        window_start = _read_window_start(_utc_today()).isoformat()
        base = f"""
            SELECT vin,
                   tire_position,
                   event_date,
                   needs_replacement,
                   wear_category,
                   wear_rate_mm_per_1k_km
            FROM {db}.{_GLUE_PRODUCT}
            WHERE tire_position IN ('FL', 'FR', 'RL', 'RR')
              AND event_date >= DATE '{window_start}'
        """.strip()

        if vins:
            # Explicit VIN list takes precedence over max_vins.
            # Sanitised to alphanumeric + hyphens to prevent SQL injection.
            safe_vins = [_sanitise_vin(v) for v in vins]
            vin_list = ", ".join(f"'{v}'" for v in safe_vins)
            return base + f"\n            AND vin IN ({vin_list})"

        if self._max_vins is not None:
            # ORDER BY vin provides pagination order for the max_vins bound.
            # TABLESAMPLE and rand() are explicitly forbidden — both break the
            # pagination invariant that select_vins relies on.
            return (
                base
                + f"\n            AND vin IN ("
                + f"SELECT vin FROM ("
                + f"SELECT DISTINCT vin FROM {db}.{_GLUE_PRODUCT} "
                + f"ORDER BY vin LIMIT {self._max_vins}"
                + "))"
            )

        return base

    def _pool_predicate(self, alias: str) -> str:
        """Return the pool predicate clause for both SQL builders.

        W5 fix: both ``_build_select_managed_vins_sql`` and
        ``_build_select_vins_sql`` call this so the predicate cannot drift.

        Parameters
        ----------
        alias:
            Column alias prefix (e.g. ``"th."`` for the managed SQL,
            ``""`` for the population SQL).
        """
        return f"{alias}needs_replacement = true"

    def _build_select_managed_vins_sql(
        self, *, cursor: Optional[str], limit: int
    ) -> str:
        """Construct the Athena SQL that selects managed VINs from the priority pool.

        Managed VINs are those in ``adp_{stage}_fleet_recall.managed_vehicles``
        (newest snapshot; no tier filter — the table holds managed VINs only)
        that also satisfy the tire priority-pool predicate
        (``needs_replacement = true``).

        The JOIN keeps both table references inside this module (spec § D3
        isolation rule: SQL and ADP column names belong exclusively in the
        owning source module).

        Pagination: keyset on ``th.vin > cursor`` so query cost does not
        grow with progress through the managed pool.

        ``ORDER BY th.vin`` provides stable pagination order.

        Parameters
        ----------
        cursor:
            Last managed VIN processed in the previous batch, or ``None``
            to start from the beginning of the managed pool.
        limit:
            Maximum number of VINs to return.
        """
        db = f"adp_{self._stage}_{_GLUE_PRODUCT}"
        recall_db = f"adp_{self._stage}_fleet_recall"
        cursor_clause = ""
        if cursor is not None:
            safe_cursor = _sanitise_vin(cursor)
            cursor_clause = f"\n  AND th.vin > '{safe_cursor}'"
        return (
            f"SELECT DISTINCT th.vin"
            f"\nFROM {db}.{_GLUE_PRODUCT} th"
            f"\nINNER JOIN {recall_db}.managed_vehicles mv ON mv.vin = th.vin"
            f"\n  AND mv.snapshot_date = ("
            f"\n      SELECT MAX(snapshot_date)"
            f"\n      FROM {recall_db}.managed_vehicles)"
            f"\nWHERE {self._pool_predicate('th.')}"
            f"{cursor_clause}"
            f"\nORDER BY th.vin"
            f"\nLIMIT {limit}"
        )

    def select_managed_vins(
        self,
        *,
        cursor: Optional[str],
        limit: int,
    ) -> tuple[list[str], Optional[str]]:
        """Return up to *limit* managed VINs after *cursor* in the priority pool.

        Managed VINs are the intersection of:
        - ``needs_replacement = true`` (tire priority pool, spec § D2)
        - ``fleet_recall.managed_vehicles`` newest snapshot (spec § D7;
          no tier filter — the table holds managed VINs only)

        The join SQL lives inside this module only (isolation rule, spec § D3).

        Parameters
        ----------
        cursor:
            Last managed VIN from the previous batch, or ``None`` to start
            from the beginning of the managed pool.
        limit:
            Maximum number of VINs to return.

        Returns
        -------
        tuple[list[str], str | None]
            ``(vins, next_cursor)`` where ``next_cursor`` is the last VIN in
            the returned batch, or ``None`` when the managed pool is exhausted.
        """
        sql = self._build_select_managed_vins_sql(cursor=cursor, limit=limit)
        log.debug(
            "adp_tire_source.select_managed_vins: stage=%s cursor_present=%s limit=%d",
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
        by selection to write the run's 7-day fallback record (stage D U7). Raises on any failure.
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

    def _build_select_vins_sql(self, cursor: Optional[str], limit: int) -> str:
        """Construct the Athena SQL that selects VINs from the priority pool.

        Pool predicate: ``needs_replacement = true`` (spec § D2).  This is an
        ADP-published boolean label — selecting on it is selection, not
        thresholding.  No numeric comparison may appear here.

        ``ORDER BY vin`` provides pagination order for keyset pagination.
        Idempotency is per-VIN content-addressing in ``runner.py:355``, not
        a property of this query's VIN order.

        Parameters
        ----------
        cursor:
            Last VIN from the previous batch, or ``None`` to start from the
            beginning of the pool.  Sanitised before interpolation.
        limit:
            Maximum number of VINs to return.
        """
        db = f"adp_{self._stage}_{_GLUE_PRODUCT}"
        sql = (
            f"SELECT DISTINCT vin"
            f" FROM {db}.{_GLUE_PRODUCT}"
            f" WHERE {self._pool_predicate('')}"
        )
        if cursor is not None:
            safe_cursor = _sanitise_vin(cursor)
            sql += f" AND vin > '{safe_cursor}'"
        sql += f" ORDER BY vin LIMIT {limit}"
        return sql

    def select_vins(
        self,
        *,
        cursor: Optional[str],
        limit: int,
    ) -> tuple[list[str], Optional[str]]:
        """Return up to *limit* VINs after *cursor* in the priority pool.

        Pool: ``needs_replacement = true`` (ADP-published boolean; spec § D2).
        Keyset pagination on ``vin > cursor`` — preferred over OFFSET so query
        cost does not grow with progress through the pool.

        Returns
        -------
        tuple[list[str], str | None]
            ``(vins, next_cursor)`` where ``next_cursor`` is the last VIN of
            the returned batch, or ``None`` when the pool is exhausted.
            The caller (``selection.resolve_batch``) reads ``([], None)`` as
            "wrap" and re-queries from the start.
        """
        sql = self._build_select_vins_sql(cursor=cursor, limit=limit)
        log.debug(
            "adp_tire_source.select_vins: stage=%s cursor_present=%s limit=%d",
            self._stage,
            cursor is not None,
            limit,
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
        try:
            raw_rows = run_query(sql, **kwargs)
        except AttributeError:
            # athena_client is None — occurs in tests that verify SQL-building
            # only (no Athena client injected).  Production runs always inject
            # a real client; re-raise if the client is present.
            if self._athena_client is None:
                return [], None
            raise
        vins = [row["vin"] for row in raw_rows if row.get("vin")]
        next_cursor = vins[-1] if vins else None
        return vins, next_cursor

    @staticmethod
    def _normalise_row(raw: dict[str, str | None]) -> dict[str, Any]:
        """Translate an Athena result row into agent-facing vocabulary.

        This is the translation boundary — ADP column names appear here and
        only here (inside this module).  The returned dict uses agent-facing
        keys so the caller never needs to reference ADP column names.

        ADP column          → agent-facing key
        ───────────────     ───────────────────
        vin                 → vin
        tire_position       → position
        event_date          → event_date
        needs_replacement   → replacement_flagged   (Python bool; None for NULL)
        wear_category       → wear_band
        wear_rate_mm_per_1k_km → wear_rate_per_1k_km
        """
        return {
            "vin": raw.get("vin"),
            "position": raw.get("tire_position"),
            "event_date": raw.get("event_date"),
            # replacement_flagged: translate ADP 'true'/'false' string to Python bool.
            "replacement_flagged": _parse_bool(raw.get("needs_replacement")),
            "wear_band": raw.get("wear_category"),
            # wear_rate_per_1k_km: numeric string from Athena; kept as string so
            # callers choose whether to cast to float.  CVX does not threshold it.
            "wear_rate_per_1k_km": raw.get("wear_rate_mm_per_1k_km"),
        }


# ---------------------------------------------------------------------------
# Module-level helpers (pure functions; no ADP column names in signatures)
# ---------------------------------------------------------------------------


def _parse_bool(value: str | None) -> bool | None:
    """Convert ADP's string boolean representation to Python bool."""
    if value is None:
        return None
    low = value.lower()
    if low == "true":
        return True
    if low == "false":
        return False
    return None  # unexpected value treated as absent


def _sanitise_vin(vin: str) -> str:
    """Allow only alphanumeric and hyphen characters in a VIN string.

    Delegates to :func:`agents.health._vin.sanitise_vin` — the single shared
    helper used by all three ADP health sources (tire, battery, brake).

    Security-review Cycle 7 W7: uniform SQL-injection defence across all
    three sources.  Previously the tire source had its own allow-list,
    battery had none, and brake had single-quote escaping only.  A shared
    helper ensures the same guard is applied consistently everywhere.

    Kept as a module-level name in ``adp_tire_source`` so existing tests
    that import ``_sanitise_vin`` from this module continue to work.

    Raises ``ValueError`` (without the VIN in the message) when no valid
    character survives sanitisation — security-review Cycle 7 W4 precedent.
    """
    return _shared_sanitise_vin(vin)


def _format_numeric(value: Any) -> str | None:
    """Serialise a numeric value to 6 d.p. for stable hashing."""
    if value is None:
        return None
    try:
        return f"{float(value):.6f}"
    except (TypeError, ValueError):
        return str(value)
