"""Cross-account Athena reader for the Tier 2 platform.

Owns Athena mechanics only: start query, poll with backoff, fetch results,
normalise rows into plain Python dicts. No SQL. No ADP column names.

SQL and column names belong to the component modules (``agents/health/``) per
spec § D3/D3a. This module is the only place Athena I/O mechanics live.

Access shape
------------
Proven by T1.2 against live staging data (2026-08-03).  Key facts from
``decisions.md`` § 2026-08-03:

- ADP's lake and Glue catalog are in **us-east-1** for both CVX stages, even
  though CVX staging compute runs in us-west-2.  The ``adp_region`` parameter
  must always be ``us-east-1``; callers that forward ``config.adp_region``
  get this automatically.
- No ``assume_role`` is needed.  The CVX execution role already has cross-
  account Athena grants via the workgroup + results bucket.
- Workgroup: ``cvx-{stage}-analytics``
- Results bucket: ``cvx-{stage}-athena-results-{account}-{region}``

The pattern is extracted from ``agents/supervisor/persona_loader.py:356-411``,
which uses ``start_query_execution`` → poll → ``get_query_results``.  That code
path is in production and was verified against live ADP data.

This module has NO boto3 import-time side-effects; the Athena client is
injected by the caller so tests can supply a stub.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Callable

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Defaults — match persona_loader.py's proven values
# ---------------------------------------------------------------------------

#: Maximum polls while the query RUNS before giving up.  1 s * 60 = 60 s ceiling.
#: Polls while it is still QUEUED don't count (see ``_DEFAULT_MAX_QUEUE_SECONDS``).
_DEFAULT_MAX_POLLS: int = 60

#: Least time a query may wait in Athena's queue before ``run_query`` gives up.
#: Athena's on-demand capacity is shared and regional: at the start of the hour
#: queries wait minutes before they run (measured 2026-10-08, all workgroups:
#: p95 241 s and max 908 s in the :00-:10 slot), and counting that wait against
#: ``max_polls`` failed reads whose queries were only queued (cost B live check
#: 5.3). 240 s covers the p95 of the busiest slot; past it the read fails as
#: before and the next scheduled run retries. A caller that grants a longer
#: running budget may queue that long too: the queue bound is the larger of this
#: and ``max_polls * poll_interval``, so the cost agent, which passes its run's
#: remaining wall time as ``max_polls`` (spec D13), still queues until its
#: ``_DeadlineAthena`` ends the read at the run's deadline.
#: CMS issue 2026-10-08-fi-lifecycle-refresh-athena-load.
_DEFAULT_MAX_QUEUE_SECONDS: float = 240.0

#: A queue wait at least this long is logged (INFO) with the execution id.
_LOG_QUEUE_WAIT_SECONDS: float = 10.0

#: Sleep between polls in seconds.  The same load-bearing sleep as persona_loader.
#: (Athena has no boto3 waiter for query completion.)
# nosemgrep: arbitrary-sleep
_DEFAULT_POLL_INTERVAL: float = 1.0

#: Default maximum rows returned across all pages before raising
#: :class:`AthenaResultTooLargeError`.  Callers may override.
_DEFAULT_MAX_ROWS: int = 100_000


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


class AthenaCursorError(RuntimeError):
    """Query reached a terminal failure state (FAILED or CANCELLED)."""


class AthenaTimeoutError(TimeoutError):
    """Query did not reach SUCCEEDED within the poll ceiling."""


class AthenaResultTooLargeError(ValueError):
    """Result set exceeded the ``max_rows`` ceiling.

    Raised instead of returning a truncated list so that callers cannot
    accidentally compute a finding (or an ``inputs_hash``) over an incomplete
    data set.  The error is fatal: the caller must either raise the ceiling,
    restrict the query, or accept that this component cannot be run today.
    """


def run_query(
    sql: str,
    *,
    athena_client: Any,
    workgroup: str,
    results_s3_path: str,
    max_polls: int = _DEFAULT_MAX_POLLS,
    poll_interval: float = _DEFAULT_POLL_INTERVAL,
    max_rows: int = _DEFAULT_MAX_ROWS,
    max_queue_seconds: float | None = None,
    clock: Callable[[], float] | None = None,
) -> list[dict[str, str | None]]:
    """Execute *sql* against the Athena workgroup and return normalised rows.

    Paginates ``GetQueryResults`` via ``NextToken`` until the result set is
    exhausted.  Raises :class:`AthenaResultTooLargeError` (never silently
    truncates) when the total data-row count would exceed *max_rows*.

    Parameters
    ----------
    sql:
        The query string.  **Must not contain ADP column names** — those belong
        to the component modules that call this function.
    athena_client:
        A ``boto3`` Athena client (or stub).  Injected so tests can supply a
        ``botocore.stub.Stubber``-backed client without network access.
    workgroup:
        Athena workgroup name, e.g. ``cvx-staging-analytics``.
    results_s3_path:
        S3 URI for query-result output, e.g.
        ``s3://cvx-staging-athena-results-{account}-us-east-1/tier2/``.
    max_polls:
        Maximum number of ``get_query_execution`` calls that find the query
        running (any state but ``QUEUED``) before raising
        :class:`AthenaTimeoutError`.  Polls while it is ``QUEUED`` don't count.
        The query is always polled at least once.
    poll_interval:
        Seconds to sleep between poll attempts.
    max_rows:
        Hard ceiling on total data rows returned across all pages.  When the
        result set would exceed this value the function raises
        :class:`AthenaResultTooLargeError` instead of returning a partial list.
        Silent truncation is the defect this parameter exists to prevent: an
        ``inputs_hash`` computed over a truncated slice is wrong and would
        break the M6 idempotency guarantee.
    max_queue_seconds:
        Seconds the query may stay ``QUEUED`` (measured from the start call)
        before :class:`AthenaTimeoutError` is raised.  None (the default) means
        ``max(_DEFAULT_MAX_QUEUE_SECONDS, max_polls * poll_interval)``.
    clock:
        Monotonic clock for the queue bound; ``time.monotonic`` when None.

    Returns
    -------
    list[dict[str, str | None]]
        One dict per **data row** (header row excluded).  Keys are column names
        from the result-set header.  Values are strings or ``None`` for NULLs.
        Returns an empty list when the result set has no data rows.

    Raises
    ------
    AthenaCursorError
        Query reached FAILED or CANCELLED.
    AthenaTimeoutError
        Query still ``QUEUED`` after ``max_queue_seconds``, or not SUCCEEDED
        within ``max_polls`` polls once running.
    AthenaResultTooLargeError
        Total data rows across all pages exceeded ``max_rows``.  Never returns
        a partial result set.
    """
    # --- start ---
    start_resp = athena_client.start_query_execution(
        QueryString=sql,
        QueryExecutionContext={"Catalog": "AwsDataCatalog"},
        WorkGroup=workgroup,
        ResultConfiguration={"OutputLocation": results_s3_path},
    )
    execution_id: str = start_resp["QueryExecutionId"]
    log.debug("adp_source: started query execution_id=%s workgroup=%s", execution_id, workgroup)

    # --- poll ---
    # Queued time and running time have separate bounds (CMS issue
    # 2026-10-08-fi-lifecycle-refresh-athena-load): a QUEUED poll spends no
    # ``max_polls``, and the queue wait is bounded by ``max_queue_seconds`` on the
    # clock. A query never returns to QUEUED once it runs.
    now = clock if clock is not None else time.monotonic
    if max_queue_seconds is None:
        max_queue_seconds = max(_DEFAULT_MAX_QUEUE_SECONDS, max_polls * poll_interval)
    started = now()
    state = "UNKNOWN"
    running_polls = 0
    queue_logged = False
    attempt = 0
    while True:
        # nosemgrep: arbitrary-sleep — Athena has no boto3 waiter; bounded loop.
        time.sleep(poll_interval)
        status_resp = athena_client.get_query_execution(QueryExecutionId=execution_id)
        state = status_resp["QueryExecution"]["Status"]["State"]
        log.debug(
            "adp_source: poll attempt=%d execution_id=%s state=%s",
            attempt,
            execution_id,
            state,
        )
        attempt += 1
        if state != "QUEUED" and not queue_logged:
            queue_logged = True
            waited = now() - started
            if waited >= _LOG_QUEUE_WAIT_SECONDS:
                log.info("adp_source: query waited %.0f s in the Athena queue (execution_id=%s)",
                         waited, execution_id)
        if state == "SUCCEEDED":
            break
        if state in ("FAILED", "CANCELLED"):
            reason = (
                status_resp["QueryExecution"]["Status"].get("StateChangeReason") or "unknown"
            )
            raise AthenaCursorError(
                f"Athena query {state}: {reason} (execution_id={execution_id})"
            )
        if state == "QUEUED":
            waited = now() - started
            if waited > max_queue_seconds:
                raise AthenaTimeoutError(
                    f"Athena query still QUEUED after {waited:.0f} s "
                    f"(max_queue_seconds={max_queue_seconds:g}, execution_id={execution_id})"
                )
            continue
        running_polls += 1
        if running_polls >= max_polls:
            raise AthenaTimeoutError(
                f"Athena query did not complete after {max_polls} polls "
                f"(execution_id={execution_id})"
            )

    # --- fetch (paginated) ---
    # GetQueryResults always returns the column-header as Row 0 on the FIRST
    # page only.  Subsequent pages contain data rows exclusively.
    # We extract the header once, then accumulate data rows across all pages,
    # raising AthenaResultTooLargeError if max_rows is breached rather than
    # returning a silently-truncated (and therefore wrong) result set.
    all_raw_data_rows: list[dict] = []
    header: list[str] | None = None
    next_token: str | None = None
    page_num = 0

    while True:
        kwargs: dict[str, Any] = {
            "QueryExecutionId": execution_id,
            "MaxResults": 1000,
        }
        if next_token is not None:
            kwargs["NextToken"] = next_token

        result = athena_client.get_query_results(**kwargs)
        rows = result.get("ResultSet", {}).get("Rows", [])

        if page_num == 0:
            # First page: Row 0 is the column-header; data rows start at index 1.
            if rows:
                header = [cell.get("VarCharValue", "") for cell in rows[0]["Data"]]
                data_rows_this_page = rows[1:]
            else:
                header = []
                data_rows_this_page = []
        else:
            # Subsequent pages contain data rows only — no header repetition.
            data_rows_this_page = rows

        # Check ceiling BEFORE appending so we never return a partial list.
        if len(all_raw_data_rows) + len(data_rows_this_page) > max_rows:
            raise AthenaResultTooLargeError(
                f"Athena result set exceeded max_rows={max_rows} "
                f"(execution_id={execution_id}). "
                "Restrict the query or raise max_rows; partial results are not returned."
            )

        all_raw_data_rows.extend(data_rows_this_page)
        page_num += 1

        next_token = result.get("NextToken")
        if not next_token:
            break

        log.debug(
            "adp_source: fetching page %d execution_id=%s rows_so_far=%d",
            page_num,
            execution_id,
            len(all_raw_data_rows),
        )

    log.debug(
        "adp_source: fetch complete execution_id=%s total_data_rows=%d pages=%d",
        execution_id,
        len(all_raw_data_rows),
        page_num,
    )

    # --- normalise ---
    if header is None or not all_raw_data_rows:
        return []

    return _normalise_with_header(header, all_raw_data_rows)


def _normalise_with_header(
    header: list[str],
    data_rows: list[dict],
) -> list[dict[str, str | None]]:
    """Convert a pre-extracted header + raw data rows into plain dicts.

    Used by :func:`run_query` after pagination: the header has already been
    stripped from the first page, and subsequent pages are data-rows only.
    """
    if not data_rows:
        return []

    normalised: list[dict[str, str | None]] = []
    for raw_row in data_rows:
        values: list[str | None] = [
            cell.get("VarCharValue")  # None when the cell is absent (NULL)
            for cell in raw_row["Data"]
        ]
        normalised.append(dict(zip(header, values)))

    return normalised


def _normalise(rows: list[dict]) -> list[dict[str, str | None]]:
    """Convert Athena ``ResultSet.Rows`` into plain dicts, stripping the header.

    The first row is always the column-header in Athena ``GetQueryResults``.
    Data rows start at index 1.  Returns an empty list when there are no data
    rows (i.e. ``len(rows) < 2``).

    .. note::
        This function is used directly by the unit tests for the
        ``_normalise`` contract.  :func:`run_query` uses
        :func:`_normalise_with_header` internally after pagination, which
        avoids re-reading the header on each page.
    """
    if len(rows) < 1:
        return []

    header: list[str] = [
        cell.get("VarCharValue", "") for cell in rows[0]["Data"]
    ]
    data_rows = rows[1:]

    if not data_rows:
        return []

    return _normalise_with_header(header, data_rows)
