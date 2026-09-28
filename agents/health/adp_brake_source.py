"""Brake service-history findings from ADP ``service_records``.

This is the **only** module in the CVX codebase that may contain:
  - SQL referencing ``service_records``
  - Column names from that product (``service_date``, ``dtc_codes``,
    ``complaint_text``, ``warranty_covered``, ``total_cost_usd``, ``outcome``)

Per spec § D3b: brake findings are **service-history facts, not predictions**.
The signal that matters most needs no model: ``outcome IN ('parts_pending',
'follow_up_required')`` means an open, unresolved brake job on this vehicle.
That is what an owner would be annoyed to discover was knowable and unsaid.

Source product
--------------
``service_records`` filtered to ``service_type = 'brake_service'``.
499,713 rows across 475,657 VINs as of 2026-08-04.

Why no trend analysis
---------------------
452,446 of 475,657 VINs (≈95 %) have exactly one brake record.  A trend
requires at least two data points; it is therefore not computable for the
vast majority of vehicles and **must not be implied**.  This module returns
the most-recent brake service record only.  If no record exists for a VIN,
``fetch_findings`` returns ``None``.

dtc_codes is an ``array<string>`` in Athena
--------------------------------------------
The ``dtc_codes`` column is stored as Athena type ``array<string>``.
This module reads it as a string from the CSV-serialised result set and
provides ``parse_dtc_codes()`` to turn that into a Python list.  Callers
should not cast or coerce the value themselves.

Provenance field
----------------
Each evidence entry carries ``provenance = 'live'`` when a real row is
returned.  ``provenance = 'absent'`` is used when the query returns no rows
(no brake history for this VIN), so the agent can report that the absence
of history was checked rather than unknown.

This module has no boto3 import-time side-effects; the Athena client is
injected by the caller so tests can supply a stub.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Optional

from agents.health._vin import sanitise_vin
from agents.tier2.adp_source import run_query

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# DTC format validation (security-review Cycle 8 W-C8-2 / S-DTC-VALIDATION)
# ---------------------------------------------------------------------------

#: Regex that a well-formed OBD-II / SAE DTC must match.
#: One letter (subsystem prefix: B/C/P/U) followed by exactly four
#: alphanumerics — the subset that real diagnostic codes use in practice
#: and that the ADP KB catalogue covers.
#: Anything outside this pattern is treated as malformed and silently dropped.
_DTC_FORMAT_RE = re.compile(r"^[A-Z][0-9A-Z]{4}$")

# ---------------------------------------------------------------------------
# ADP product name — asserted by tests to ensure no other module leaks it
# ---------------------------------------------------------------------------

#: The ADP product this module reads.  Imported by tests to assert isolation.
ADP_PRODUCT = "service_records"

# ---------------------------------------------------------------------------
# Open-outcome vocabulary
# ---------------------------------------------------------------------------

#: Outcomes that mean a brake job is **still open** on this vehicle.
#: The agent surfaces these plainly — they are actionable without a model.
OPEN_OUTCOMES: frozenset[str] = frozenset({"parts_pending", "follow_up_required"})

# ---------------------------------------------------------------------------
# SQL
# ---------------------------------------------------------------------------

# ⚠️  DEPRECATED — SUPERSEDED BY AdpBrakeSource._build_sql.  DO NOT USE.
#
# This template and the module-level `fetch_findings` below are retained ONLY
# because ~80 existing unit tests exercise them through a stubbed Athena client.
# **It does not execute against real Athena**, in two independent ways, both
# reproduced live on 2026-08-05 and documented in
# `issues/2026-08-05-tier2-adp-read-path-never-exercised-live/report.md` § F2:
#
#   FROM service_records         → TABLE_NOT_FOUND: awsdatacatalog.default.service_records
#                                  (the workgroup carries no default database, and this
#                                   template has no `stage` to qualify it with)
#   CAST(dtc_codes AS VARCHAR)   → TYPE_MISMATCH: Cannot cast array(varchar) to varchar
#
# It is left broken and labelled rather than half-fixed on purpose: qualifying the
# table needs a `stage`, and adding one with a default value is how the same class
# of silent-wrong-default defect gets reintroduced.  `AdpBrakeSource` takes `stage`
# explicitly and emits the verified-working `array_join(dtc_codes, ',', '')` form.
#
# The Tier 2 runner instantiates `AdpBrakeSource` and never reaches this path, so
# nothing deployed depends on it.  Deleting this template, `_build_sql`, and the
# module-level `fetch_findings` — migrating their tests onto the class — is filed
# as a follow-on in the spec's `decisions.md`.  Raised as review Cycle 12
# Suggestion 3.
_SQL = """
SELECT
    vin,
    service_date,
    CAST(dtc_codes AS VARCHAR)   AS dtc_codes,
    complaint_text,
    warranty_covered,
    CAST(total_cost_usd AS VARCHAR) AS total_cost_usd,
    outcome
FROM service_records
WHERE service_type = 'brake_service'
  AND vin = '{vin}'
ORDER BY service_date DESC
LIMIT 1
""".strip()


# ---------------------------------------------------------------------------
# Result dataclass (plain dict — no third-party deps for schema)
# ---------------------------------------------------------------------------

def _build_sql(vin: str) -> str:
    """Return the parameterised SQL for *vin*.

    Uses string-format substitution on a sanitised VIN.  The VIN is
    allow-listed to ``[A-Z0-9-]`` via :func:`agents.health._vin.sanitise_vin`
    before embedding in the SQL literal.  Athena does not support
    prepared-statement bindings via boto3 ``start_query_execution``.

    Security-review Cycle 7 W7: all three ADP health sources use the same
    allow-list helper (:mod:`agents.health._vin`) rather than per-source
    ad-hoc escaping.
    """
    # Allow-list: reject anything outside the VIN alphabet before embedding.
    # sanitise_vin raises ValueError on empty result, which propagates to
    # the caller (runner) as an unrecoverable per-VIN error.
    safe_vin = sanitise_vin(vin)
    return _SQL.format(vin=safe_vin)


def parse_dtc_codes(raw: str | None) -> list[str]:
    """Parse the Athena-serialised array representation of ``dtc_codes``.

    Athena serialises ``array<string>`` as ``[item1, item2, …]`` when returned
    through ``GetQueryResults`` CSV.  Returns an empty list for NULL / empty.

    Examples
    --------
    >>> parse_dtc_codes("[C0161, C0040]")
    ['C0161', 'C0040']
    >>> parse_dtc_codes("[]")
    []
    >>> parse_dtc_codes(None)
    []
    """
    if not raw:
        return []
    raw = raw.strip()
    if raw in ("[]", "", "null", "NULL"):
        return []
    # Strip outer brackets if present.
    if raw.startswith("[") and raw.endswith("]"):
        raw = raw[1:-1]
    # Split on commas and strip whitespace + quotes from each token.
    return [item.strip().strip("'\"") for item in raw.split(",") if item.strip()]


def validate_dtc_codes(codes: list[str]) -> list[str]:
    """Filter *codes* to only those matching the well-formed DTC format.

    Accepts codes of the form ``[A-Z][0-9A-Z]{4}`` (one subsystem-prefix
    letter followed by exactly four alphanumerics).  Any entry that does not
    match is **silently dropped** — its content is never logged.  Only the
    count of dropped entries is recorded at DEBUG level, so there is nothing
    for an injection payload to exfiltrate via log output.

    This is the boundary guard introduced by security-review Cycle 8 W-C8-2
    (previously Suggestion ``S-DTC-VALIDATION`` from Cycle 7).  The ADP
    ``dtc_codes`` column is typed ``array<string>``; any value can appear in
    it.  Filtering here prevents malformed strings from propagating to
    ``assemble_brake_dtc_note`` or to the ``_enrich_with_llm`` structured
    summary — both callers now see only validated codes.

    Parameters
    ----------
    codes:
        Output of :func:`parse_dtc_codes` — a list of raw strings.

    Returns
    -------
    list[str]
        Subset of *codes* whose entries match ``_DTC_FORMAT_RE``.

    Examples
    --------
    >>> validate_dtc_codes(["C0161", "C0040"])
    ['C0161', 'C0040']
    >>> validate_dtc_codes(["C0161", "'; DROP", "IGNORE PREV"])
    ['C0161']
    >>> validate_dtc_codes([])
    []
    """
    valid: list[str] = []
    dropped = 0
    for code in codes:
        if _DTC_FORMAT_RE.match(code):
            valid.append(code)
        else:
            dropped += 1
    if dropped:
        # Log count only — never the content, which may be malicious.
        log.debug(
            "adp_brake_source: dropped %d malformed dtc_codes entr%s",
            dropped,
            "y" if dropped == 1 else "ies",
        )
    return valid


def fetch_findings(
    vin: str,
    *,
    athena_client: Any,
    workgroup: str,
    results_s3_path: str,
) -> dict | None:
    """Return the most-recent brake-service finding for *vin*, or ``None``.

    Queries ADP ``service_records`` (``service_type = 'brake_service'``) for
    the given VIN and returns the most-recent record as a plain dict.

    The finding that matters most — ``outcome in OPEN_OUTCOMES`` — is surfaced
    plainly so the agent can report it without prediction or extrapolation.

    Parameters
    ----------
    vin:
        Vehicle Identification Number.
    athena_client:
        boto3 Athena client (or stub).  Injected for testability.
    workgroup:
        Athena workgroup, e.g. ``cvx-staging-analytics``.
    results_s3_path:
        S3 URI for Athena result output.

    Returns
    -------
    dict | None
        A finding dict when a brake record exists, or ``None`` when no record
        is found.  The finding dict contains:

        ``vin``
            The queried VIN.
        ``service_date``
            Date of the brake service (ISO-8601 string).
        ``dtc_codes``
            List of DTC code strings (may be empty).  Parsed from the
            Athena array-as-string representation.
        ``complaint_text``
            Free-text complaint description, or ``None``.
        ``warranty_covered``
            Whether the service was warranty-covered (string ``"true"`` /
            ``"false"`` / ``None``).
        ``total_cost_usd``
            Total cost in USD (string representation of decimal, or ``None``).
        ``outcome``
            Service outcome code string.
        ``is_open``
            ``True`` when ``outcome`` indicates an unresolved brake job
            (``outcome in OPEN_OUTCOMES``).  ``False`` otherwise.
        ``provenance``
            Always ``"live"`` — this record came from live ADP data.
    """
    sql = _build_sql(vin)
    # Log count only — never the VIN.  Tier 2 runs unattended; CloudWatch is
    # the widest exposure surface.  See security-review Cycle 7 Warning W3 +
    # the platform's test-enforced "no VINs in logs" contract in
    # agents/tier2/tests/test_budget.py::test_no_vins_in_budget_exception_repr.
    log.debug("adp_brake_source: querying service_records vin_count=1")

    rows = run_query(
        sql,
        athena_client=athena_client,
        workgroup=workgroup,
        results_s3_path=results_s3_path,
    )

    if not rows:
        log.debug("adp_brake_source: no brake record found vin_count=1")
        return None

    row = rows[0]
    outcome = row.get("outcome") or ""
    dtc_codes = validate_dtc_codes(parse_dtc_codes(row.get("dtc_codes")))

    return {
        "vin": vin,
        "service_date": row.get("service_date"),
        "dtc_codes": dtc_codes,
        "complaint_text": row.get("complaint_text"),
        "warranty_covered": row.get("warranty_covered"),
        "total_cost_usd": row.get("total_cost_usd"),
        "outcome": outcome,
        "is_open": outcome in OPEN_OUTCOMES,
        "provenance": "live",
        # model_year is not available in the deprecated module-level SQL (no JOIN).
        # Set to None to keep the key contract consistent with AdpBrakeSource.
        "model_year": None,
    }


# ---------------------------------------------------------------------------
# Batch SQL template — database-qualified, array_join for dtc_codes
# ---------------------------------------------------------------------------

# This SQL is the verified-working form confirmed against live staging Athena
# (see issues/2026-08-05-tier2-adp-read-path-never-exercised-live/report.md § F2).
#
# Key differences from the module-level _SQL above (which is used by the
# legacy per-VIN fetch_findings function):
#
#   1. Database-qualified:  ``adp_{stage}_service_records.service_records``
#      instead of bare ``service_records``.  The workgroup has no default
#      database, so the unqualified form raises TABLE_NOT_FOUND at Athena.
#
#   2. array_join() instead of CAST(dtc_codes AS VARCHAR):
#      ``dtc_codes`` is ``array<string>`` in Athena; CAST to VARCHAR raises
#      TYPE_MISMATCH.  ``array_join(dtc_codes, ',', '')`` produces a
#      comma-separated string that parse_dtc_codes() can consume (it strips
#      brackets only when present, then splits on commas).
#
#   3. Most-recent-per-VIN via row_number() OVER PARTITION:
#      Returns exactly one row per VIN — the latest brake service record —
#      in a single batch query instead of one query per VIN (spec § D2:
#      "never per-VIN fan-out").
#
#   4. Optional VIN list injection via ``{vin_filter}``.
#      When vins are specified, ``{vin_filter}`` expands to
#      ``AND vin IN ('VIN1', 'VIN2', ...)``.  When None, it is empty string.
#
#   5. model_year from vehicle_identity (Review Cycle 1 W6; spec T4.3):
#      ``model_year`` is a partition key (type int) on the
#      ``adp_{stage}_vehicle_identity.vehicle_identity`` table, alongside
#      ``vin`` as a regular column.  A LEFT JOIN on vin brings the year
#      in without dropping service records for vehicles with no identity row
#      (NULL is handled by _build_brake_finding's model_year_raw sentinel).
#      The partition key is selectable directly in the outer query as
#      ``vi.model_year``; no CAST is needed (Athena returns it as a string
#      in the CSV result set regardless of declared type).
#
# ``{stage}`` is substituted at call time from the class's _stage attribute.
_BATCH_SQL_TEMPLATE: str = """
SELECT sr.vin, sr.service_date, sr.dtc_codes_str, sr.complaint_text,
       sr.warranty_covered, sr.total_cost_usd, sr.outcome,
       vi.model_year
FROM (SELECT vin, service_date,
             array_join(dtc_codes, ',', '') AS dtc_codes_str,
             complaint_text,
             CAST(warranty_covered AS VARCHAR) AS warranty_covered,
             CAST(total_cost_usd  AS VARCHAR) AS total_cost_usd,
             outcome,
             row_number() OVER (PARTITION BY vin ORDER BY service_date DESC) AS rn
      FROM adp_{stage}_service_records.service_records
      WHERE service_type = 'brake_service'{vin_filter}) sr
LEFT JOIN adp_{stage}_vehicle_identity.vehicle_identity vi
       ON vi.vin = sr.vin
WHERE sr.rn = 1
""".strip()


# ---------------------------------------------------------------------------
# Batch class — implements the FindingSource protocol
# ---------------------------------------------------------------------------


class AdpBrakeSource:
    """Fetch per-VIN brake service-history findings from ADP ``service_records``.

    Implements the ``FindingSource`` protocol consumed by
    :mod:`agents.tier2.runner`: ``fetch_findings(vins) -> list[dict]``.

    This is the batch counterpart to the module-level :func:`fetch_findings`
    function.  The module-level function remains for backward compatibility with
    earlier per-VIN callers; new code (the runner, the health agent) must use
    this class.

    Lifetime
    --------
    One instance per Tier 2 run.  AWS clients are injected so test doubles can
    be supplied without network access.

    Parameters
    ----------
    stage:
        Deployment stage — ``"staging"`` or ``"prod"``.
    athena_client:
        A boto3 Athena client initialised in ``us-east-1`` (ADP's catalog
        region for both CVX stages, regardless of the Lambda's own region).
    workgroup:
        Athena workgroup, e.g. ``"cvx-staging-analytics"``.
    results_s3_path:
        S3 URI for Athena query results, e.g.
        ``"s3://cvx-staging-athena-results-{account}-us-east-1/tier2/"``.
    max_polls:
        Forwarded to :func:`agents.tier2.adp_source.run_query`; controls the
        Athena poll ceiling.
    poll_interval:
        Forwarded to :func:`agents.tier2.adp_source.run_query`; seconds to
        sleep between poll attempts.  Set to ``0`` in unit tests.
    max_vins:
        When no explicit VIN list is supplied via :meth:`fetch_findings`,
        bound the query to this many VINs via an ``ORDER BY vin`` subquery
        (which provides pagination order).  ``None`` means no bound (full fleet).
        Defaults to ``25`` which suits the default per-run budget.

        The inner subquery repeats ``WHERE service_type = 'brake_service'``
        so the distinct-VIN set is drawn only from brake records — without
        it, most distinct VINs in ``service_records`` have no brake row and
        the outer query returns fewer than ``max_vins`` results.
    max_rows:
        Hard ceiling on total result rows forwarded to
        :func:`agents.tier2.adp_source.run_query`.  When ``None``, the default
        in ``adp_source._DEFAULT_MAX_ROWS`` applies.  The Tier 2 runner passes
        ``TIER2_MAX_ROWS_PER_QUERY`` from the environment so the stack-configured
        ceiling is actually enforced rather than ignored (spec T6b.1).

    SQL isolation
    -------------
    ``service_records`` table name, database pattern, and all ADP column names
    appear **only** in this module.  No other CVX file may reference them;
    tests assert this invariant (test_adp_brake_source.py).
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

    # -----------------------------------------------------------------------
    # FindingSource protocol
    # -----------------------------------------------------------------------

    def fetch_findings(
        self,
        vins: Optional[list[str]] = None,
    ) -> list[dict[str, Any]]:
        """Return agent-facing brake findings for the given VINs (or all VINs).

        Translates ADP ``service_records`` column names into agent-facing
        vocabulary at this boundary so the agent never sees an ADP column name.

        **ADP column → agent-facing key mapping** (translation lives here and
        only here — no other CVX file may reference these ADP names as dict keys):

        ``vin``               → ``vin``              (unchanged; not ADP-specific)
        ``service_date``      → ``last_service_date``
        ``dtc_codes_str``     → ``dtc_codes``         (parsed + validated list)
        ``complaint_text``    → ``complaint``
        ``warranty_covered``  → ``covered_by_warranty``
        ``total_cost_usd``    → ``cost_usd``
        ``outcome``           → ``outcome``           (unchanged; domain vocab)
        ``is_open``           → ``is_open``           (derived; True when outcome in OPEN_OUTCOMES)
        ``provenance``        → ``provenance``        (always ``"live"``)

        Parameters
        ----------
        vins:
            Optional list of VINs to restrict the query.  If ``None`` or
            empty, all VINs in the product are queried (fleet-wide batch).

        Returns
        -------
        list[dict]
            One dict per VIN with agent-facing keys.  Returns an empty list
            when no brake-service records exist for the requested VINs.
        """
        sql = self._build_sql(vins)
        log.debug(
            "adp_brake_source: querying service_records stage=%s vin_count=%s",
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
    # Selection helpers (spec § D3 — priority-pool keyset pagination)
    # -----------------------------------------------------------------------

    def _build_select_vins_sql(self, *, cursor: str | None, limit: int) -> str:
        """Return the Athena SQL that selects up to *limit* VINs from the open-job pool.

        Pool predicate: ``outcome IN (<OPEN_OUTCOMES values>)`` on
        ``adp_{stage}_service_records.service_records`` filtered to
        ``service_type = 'brake_service'``.

        The predicate reuses the ``OPEN_OUTCOMES`` constant — the two outcome
        values are not re-spelled here so the SQL cannot drift from the
        module's own definition of what "open" means.

        Pagination: keyset on ``vin > '<cursor>'`` (preferred over OFFSET
        so query cost does not grow with progress).

        ``ORDER BY vin`` is retained for pagination order — not for hash
        stability (which is per-VIN content-addressing in runner.py, not a
        fixed-window property; see spec § D1 and the docstring correction on
        the existing ``_build_sql`` path above).

        No numeric comparison is present — the pool is categorical
        (``OPEN_OUTCOMES``), not a threshold (spec § D2, D3).
        """
        db = f"adp_{self._stage}_service_records"
        # Build the IN list from the constant so predicate and constant cannot
        # drift.  Values are plain lowercase strings; no SQL metacharacters in
        # the OPEN_OUTCOMES vocabulary, but we still quote them as string
        # literals to match standard SQL practice.
        outcomes_list = ", ".join(f"'{v}'" for v in sorted(OPEN_OUTCOMES))

        cursor_clause = ""
        if cursor is not None:
            safe_cursor = sanitise_vin(cursor)
            cursor_clause = f"\n  AND vin > '{safe_cursor}'"

        return (
            f"SELECT DISTINCT vin"
            f"\nFROM {db}.service_records"
            f"\nWHERE service_type = 'brake_service'"
            f"\n  AND outcome IN ({outcomes_list})"
            f"{cursor_clause}"
            f"\nORDER BY vin"
            f"\nLIMIT {limit}"
        )

    def select_vins(
        self,
        *,
        cursor: str | None,
        limit: int,
    ) -> tuple[list[str], str | None]:
        """Return up to *limit* VINs after *cursor* in the open-job priority pool.

        The pool is ``outcome IN OPEN_OUTCOMES`` on brake service records.
        Pagination is keyset on ``vin > cursor`` so each call advances through
        the pool without growing Athena cost with progress.

        Returns ``([], None)`` when the pool is exhausted, which the caller
        (``agents/tier2/selection.resolve_batch``) reads as "wrap — re-query
        from the start".

        Parameters
        ----------
        cursor:
            Last VIN seen in the previous call, or ``None`` to start from
            the beginning of the pool.
        limit:
            Maximum number of VINs to return in this batch.

        Returns
        -------
        tuple[list[str], str | None]
            ``(vins, next_cursor)`` where ``next_cursor`` is the last VIN in
            the returned batch (for the next call), or ``None`` when the batch
            is empty (pool exhausted).
        """
        sql = self._build_select_vins_sql(cursor=cursor, limit=limit)
        log.debug(
            "adp_brake_source: select_vins stage=%s cursor_present=%s limit=%d",
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
        rows = run_query(sql, **kwargs)
        if not rows:
            return [], None
        vins = [r["vin"] for r in rows]
        return vins, vins[-1]

    # -----------------------------------------------------------------------
    # Internal helpers
    # -----------------------------------------------------------------------

    def _build_sql(self, vins: Optional[list[str]]) -> str:
        """Construct the database-qualified Athena SQL for service_records.

        SQL and ADP column names are isolated to this module.  The verified-
        working SQL template uses ``array_join(dtc_codes, ',', '')`` (not
        ``CAST(dtc_codes AS VARCHAR)`` which raises TYPE_MISMATCH) and
        qualifies the database as ``adp_{stage}_service_records.service_records``
        (not bare ``service_records`` which raises TABLE_NOT_FOUND).

        When *vins* is provided the query filters to those VINs exactly.
        When *vins* is ``None`` and ``max_vins`` is set, the query uses the
        deterministic distinct-VIN subquery with an inner ``service_type`` filter:

        .. code-block:: sql

            AND vin IN (SELECT vin FROM (SELECT DISTINCT vin
                                         FROM adp_{stage}_service_records.service_records
                                         WHERE service_type = 'brake_service'
                                         ORDER BY vin LIMIT {max_vins}))

        The inner ``WHERE service_type = 'brake_service'`` is intentional: without
        it, ``SELECT DISTINCT vin FROM service_records`` draws from ALL service
        record types.  Most VINs in ``service_records`` have no brake row, so the
        outer query would return far fewer than ``max_vins`` VINs.  Repeating the
        filter ensures exactly ``max_vins`` brake VINs are selected.

        ``ORDER BY vin`` provides **pagination order** — it is not what makes
        idempotency hold.  Idempotency comes from per-VIN content-addressing in
        ``runner.py:355``: ``inputs_hash`` is computed over the rows for a
        *single* VIN inside the per-VIN loop, and the conditional put at
        ``artifact_writer.py:87`` guards on ``attribute_not_exists(inputs_hash)``.
        A different VIN set therefore produces artifacts for those VINs, each
        written at most once per distinct input content (spec § D1).  Do **not**
        replace ``ORDER BY vin`` with TABLESAMPLE or rand() — both break the
        pagination invariant that ``select_vins`` relies on.

        See issues/2026-08-05-tier2-adp-read-path-never-exercised-live/report.md
        § F2 for the live Athena failure reproductions that motivated this.
        """
        vin_filter = ""
        if vins:
            # Explicit VIN list takes precedence over max_vins.
            safe_vins = [sanitise_vin(v) for v in vins]
            vin_list = ", ".join(f"'{v}'" for v in safe_vins)
            vin_filter = f"\n      AND vin IN ({vin_list})"
        elif self._max_vins is not None:
            # ORDER BY vin provides pagination order for the max_vins bound —
            # not idempotency protection (spec § D1).  Idempotency comes from
            # per-VIN content-addressing at runner.py:355 with a conditional put
            # on attribute_not_exists(inputs_hash).  The inner subquery repeats
            # service_type = 'brake_service' so the distinct-VIN set is drawn
            # only from brake records — without it, most VINs in service_records
            # have no brake row and the outer query returns fewer than max_vins results.
            db = f"adp_{self._stage}_service_records"
            vin_filter = (
                f"\n      AND vin IN (SELECT vin FROM (SELECT DISTINCT vin"
                f"\n                                   FROM {db}.service_records"
                f"\n                                   WHERE service_type = 'brake_service'"
                f"\n                                   ORDER BY vin LIMIT {self._max_vins}))"
            )
        return _BATCH_SQL_TEMPLATE.format(stage=self._stage, vin_filter=vin_filter)

    @staticmethod
    def _normalise_row(raw: dict[str, str | None]) -> dict[str, Any]:
        """Translate an Athena result row into agent-facing vocabulary.

        This is the translation boundary for the *shape* of the result set —
        the Athena projection aliases and the `dtc_codes_str` array_join output
        are resolved here and nowhere else.

        Key names deliberately match the ADP column names rather than being
        renamed, because the brake row contract is shared with the module-level
        :func:`fetch_findings` and both are consumed by
        ``agents/health/agent.py`` through its ``_COL_*`` constants.  Renaming
        them here (T6b.3) broke ``_build_brake_finding`` silently.  The tire
        and battery sources do rename, and correctly so — their agent-facing
        vocabulary is defined in ``agent.py`` under matching ``_KEY_*``
        constants.  The rule is that the source and the agent agree, not that
        renaming always happens.

        ADP column          → row-contract key
        ───────────────     ───────────────────
        vin                 → vin
        service_date        → service_date
        dtc_codes_str       → dtc_codes            (parsed + validated list)
        complaint_text      → complaint_text
        warranty_covered    → warranty_covered
        total_cost_usd      → total_cost_usd
        outcome             → outcome
        (derived)           → is_open              (True when outcome in OPEN_OUTCOMES)
        (constant)          → provenance            ("live")
        """
        # dtc_codes_str is the array_join() output from Athena — a plain
        # comma-separated string (no brackets), which parse_dtc_codes() handles.
        raw_dtc = raw.get("dtc_codes_str")
        dtc_codes = validate_dtc_codes(parse_dtc_codes(raw_dtc))
        outcome = raw.get("outcome") or ""
        # Key names MUST match the module-level fetch_findings() contract above,
        # because agents/health/agent.py consumes both through the same
        # _COL_* constants (agent.py:109-120).  T6b.3: this method originally
        # emitted `last_service_date` / `complaint` / `covered_by_warranty` /
        # `cost_usd`, which agent.py does not read — `_build_brake_finding`
        # calls `.get("service_date", "unknown date")`, so a live brake job
        # with a known date of 2021-12-13 rendered as "Brake service on
        # unknown date".  The defensive default turned a broken contract into
        # a plausible-looking sentence instead of an error, which is the worst
        # available failure mode for an artifact whose value is its honesty.
        # test_row_contract_keys_match_agent_expectations pins these names to
        # agent.py's constants so the two cannot drift again.
        return {
            "vin": raw.get("vin"),
            "service_date": raw.get("service_date"),
            "dtc_codes": dtc_codes,
            "complaint_text": raw.get("complaint_text"),
            "warranty_covered": raw.get("warranty_covered"),
            "total_cost_usd": raw.get("total_cost_usd"),
            "outcome": outcome,
            "is_open": outcome in OPEN_OUTCOMES,
            "provenance": "live",
            # model_year is a partition key on vehicle_identity, brought in via
            # a LEFT JOIN in _BATCH_SQL_TEMPLATE.  None when the JOIN finds no
            # identity row for this VIN (unexpected but safe to propagate).
            # Kept as raw string here — _build_brake_finding casts to int.
            "model_year": raw.get("model_year"),
        }
