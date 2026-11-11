"""Run records for Tier 2 health agents — spec D10, contract § 8.

Table ``vsa-{stage}-avx-agent-runs``:
  pk: ``agent_component`` = ``"{agent_id}#{component}"``
  sk: ``started_at``

CMS reads the newest record per ``agent_component`` to show "last ran at …".
With no record it shows "has not run yet", never an empty queue presented as
"nothing to do".

A failed DynamoDB write is **logged and does not fail the run** (spec D10).
The table name is read from the ``AVX_AGENT_RUNS_TABLE`` environment variable;
absence raises ``RuntimeError`` (fail closed — the caller must notice at startup
rather than silently skipping records for every run).
"""
from __future__ import annotations

import calendar
import logging
import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

log = logging.getLogger(__name__)

_TABLE_ENV_VAR = "AVX_AGENT_RUNS_TABLE"

# T1.5: run-record TTL — 400 days from finished_at (epoch seconds).
# Matches the spec decision (400 days so CMS can show "last ran months ago"
# even for quiet auto-approver batches).  The stack sets timeToLiveAttribute='ttl'.
_RUN_RECORD_TTL_DAYS = 400

# managed_coverage (stage D U7): the fields each mode carries, all ints besides ``mode``.
_COVERAGE_UNMEASURED_FIELDS = ("cycle_nights", "cap", "slice_size")
_COVERAGE_FALLBACK_FIELDS = (
    "cycle_nights", "cap", "managed_pool", "nights_to_cover", "cycle_covers",
    "slice_start", "slice_size",
)


def _coverage_item(coverage: dict) -> dict[str, Any]:
    """Return the stored ``managed_coverage`` map: known fields only, ints, one of two modes.

    A ``7_day_fallback`` map missing any field (or carrying a non-int) is stored as
    ``unmeasured``, so a partial record can never read as the fallback.
    """
    def _ints(fields: tuple[str, ...]) -> dict[str, int] | None:
        out: dict[str, int] = {}
        for name in fields:
            value = coverage.get(name)
            if type(value) is not int:
                return None
            out[name] = value
        return out

    if coverage.get("mode") == "7_day_fallback":
        fields = _ints(_COVERAGE_FALLBACK_FIELDS)
        if fields is not None:
            return {"mode": "7_day_fallback", **fields}
    unmeasured: dict[str, Any] = {"mode": "unmeasured"}
    for name in _COVERAGE_UNMEASURED_FIELDS:
        value = coverage.get(name)
        if type(value) is int:
            unmeasured[name] = value
    return unmeasured


def _compute_run_ttl(finished_at: str | None) -> int:
    """Return the epoch-seconds TTL = finished_at + 400 days.

    Falls back to write time when ``finished_at`` is absent or unparseable.
    The DynamoDB TTL attribute is named ``ttl`` (stack precedent for all AVX tables).
    """
    base_dt: datetime | None = None
    if finished_at:
        try:
            base_dt = datetime.fromisoformat(finished_at.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            pass
    if base_dt is None:
        base_dt = datetime.now(timezone.utc)
    return int(calendar.timegm((base_dt + timedelta(days=_RUN_RECORD_TTL_DAYS)).utctimetuple()))


def get_runs_table_name() -> str:
    """Return the ``AVX_AGENT_RUNS_TABLE`` env var value.

    Raises
    ------
    RuntimeError
        If ``AVX_AGENT_RUNS_TABLE`` is not set in the environment.  Absent
        means fail closed: callers must notice at startup rather than
        silently skipping run records for every run.
    """
    value = os.environ.get(_TABLE_ENV_VAR)
    if not value:
        raise RuntimeError(
            f"Required environment variable {_TABLE_ENV_VAR!r} is not set. "
            "Set it to the DynamoDB table name for agent run records "
            "(e.g. 'vsa-staging-avx-agent-runs')."
        )
    return value


def write_run_record(
    *,
    agent_id: str,
    component: str,
    started_at: str,
    finished_at: str,
    status: str,
    artifacts_written: int,
    duplicates_skipped: int,
    agent_version: str,
    ddb_client: Any,
    table_name: str,
    llm_calls: int = 0,
    llm_skipped_by_breaker: int = 0,
    llm_breaker_trip_reason: str = "",
    data_month: str | None = None,
    vins_selected: int | None = None,
    model_calls: int | None = None,
    deferred: int | None = None,
    tokens_used: int | None = None,
    cost_usd: float | None = None,
    write_failures: int | None = None,
    rules_fallbacks: int | None = None,
    guardrail_interventions: int | None = None,
    guardrail_errors: int | None = None,
    rows_refused: int | None = None,
    grounding_read_errors: int | None = None,
    book_items_without_rooftops: int | None = None,
    rows_without_terms: int | None = None,
    p0_directive_fallbacks: int | None = None,
    vins_refused: int | None = None,
    exhausted_items_written: int | None = None,
    agent2_read_errors: int | None = None,
    # G3F3.1 (a1/a2 C1): fleet-health service run record — fields forwarded by
    # _service_record_fields that were missing from this signature, causing TypeError
    # and silently dropping every completed service run record.
    registry_read_errors: int | None = None,
    vehicles_over_cap: int | None = None,
    # G3F3.1 (a2 W2): managed-first selection fallback count, forwarded by run()
    # at all four exits so the staleness alarm can distinguish a managed-unavailable
    # run from a normal one.
    managed_select_errors: int | None = None,
    # AVX-M2: ADP keys whose snapshot_runs marker said row_count==0 for this run.
    empty_snapshot_keys: list | None = None,
    # W-n5 / W-b2 (G4-2 F8): FI rebalance component counters forwarded by
    # _rebalance_record_fields.  Not written for any other component (no caller
    # passes them), so all other components' records are unchanged.
    open_items_read_errors: int | None = None,
    locations_incomplete: int | None = None,
    gap_items_without_vehicle: int | None = None,
    forecast_days_missing: int | None = None,
    # W-c3-1 (G4-3): deficit locations and move candidates dropped while building
    # candidates for a missing forecast day.
    candidates_forecast_days_missing: int | None = None,
    # Stage D U7 (issue 2026-10-22-battery-fallback-record): the 7-day managed-coverage
    # fallback record from selection; see ``_coverage_item``.
    managed_coverage: dict | None = None,
    # FI 11.3b item 4: the simulated fleet operator's per-run detail (mode, errors,
    # per-fleet counts, skip reasons, cap). Written only when passed.
    sim_operator: dict | None = None,
) -> bool:
    """Write one run record to ``vsa-{stage}-avx-agent-runs``.

    Attributes written (contract § 8):

    ``agent_component``
        ``"{agent_id}#{component}"`` — partition key.  CMS joins on this to
        show the last-run timestamp per agent and component.
    ``started_at``
        ISO-8601 UTC string — sort key.
    ``finished_at``
        ISO-8601 UTC string.
    ``status``
        ``"completed"`` or ``"aborted"``.
    ``artifacts_written``, ``duplicates_skipped``
        Counts for this run.
    ``agent_version``
        Plain semver string (e.g. ``"0.1.0"``).
    ``llm_calls``, ``llm_skipped_by_breaker``, ``llm_breaker_trip_reason``
        LLM circuit-breaker counters, carried from
        ``make_strands_agent_fn``'s per-batch state (review cycle 3
        Warning, 2026-10-04).  The deploy stage's live check reads these
        to tell on tonight's 02:00Z run whether the time-aware breaker
        fired, and whether it tripped on time or on call count.
        ``llm_breaker_trip_reason`` is a short label (``""`` when the
        breaker did not fire; ``"wall_time"`` or ``"call_count"`` when it
        did; ``"other"`` for a future reason).
    ``data_month``, ``vins_selected``, ``model_calls``, ``deferred``, ``tokens_used``,
    ``cost_usd``, ``write_failures``, ``rules_fallbacks``, ``guardrail_interventions``
        The FI cost component's counters (spec 2026-10-02-fi-cost-stage-b task 4.1):
        the data month the run read, anomalies selected, Converse calls made,
        vehicles left for the next daily run because of the diagnosis cap or the wall
        clock, tokens and their list-price cost, failed Finding writes, diagnoses that
        fell back to the rules (a model or guardrail failure on both attempts), and the
        replies the Guardrail blocked, which tells those two causes apart. Written
        only when passed, so every other component's record is unchanged. Additive
        to contract § 8: CMS reads the record's named fields only.

    ``open_items_read_errors``, ``locations_incomplete``, ``gap_items_without_vehicle``,
    ``forecast_days_missing``
        FI rebalancing component counters (spec 2026-10-06-fi-rebalancing-stage-e
        G4-2 F8, W-n5 / W-b2): open-items read errors, locations where the fleet
        data was incomplete, deficit or surplus locations with no assigned vehicle
        (worst-case zero-supply deficit), and forecast days that were missing.
        ``candidates_forecast_days_missing`` counts the deficit locations and move
        candidates dropped while building candidates for a missing forecast day
        (non-zero on every run once the forecast is three or more days old).
        Written only when passed; every other component's record is unchanged.

    ``sim_operator``
        The simulated fleet operator's run detail (FI 11.3b, D10): ``mode``,
        ``errors``, ``fleets`` (per fleet: units approved/skipped/errored, decisions
        written, planned units by class, skip counts by reason, cap used before the
        run), and ``reason`` when the run stopped early. Written only when passed.

    ``managed_coverage``
        Stage D U7: written when a health component's managed pool is larger than its nightly
        cap, so its managed cursor rotates and every managed vehicle is covered over several
        nights. ``mode`` ``7_day_fallback`` with ``cycle_nights`` (7), ``cap``,
        ``managed_pool``, ``nights_to_cover``, ``cycle_covers`` (how many managed vehicles 7
        nights cover), ``slice_start`` and ``slice_size`` (this run's slice, by 1-based VIN-order
        position). ``mode`` ``unmeasured`` (no pool fields) when the pool couldn't be counted, or
        when a failed wrap-around query left the slice short (that night also counts in
        ``managed_select_errors``). Absent when the managed page wasn't full and no managed
        query failed: the pool fits the cap.
    ----------
    agent_id:
        The Finding ``agent_id`` this component belongs to (e.g.
        ``"agent-2-vehicle-health"``).  CMS uses it to join run records to
        the queue.
    component:
        Health component, e.g. ``"battery"``, ``"tires"``, ``"brakes"``.
    started_at:
        ISO-8601 UTC timestamp for when the run began.
    finished_at:
        ISO-8601 UTC timestamp for when the run ended.
    status:
        ``"completed"`` or ``"aborted"``.
    artifacts_written:
        Number of Findings written this run.
    duplicates_skipped:
        Number of duplicate Findings skipped this run.
    agent_version:
        Plain semver string (no prefix).
    ddb_client:
        boto3 DynamoDB **resource** (``boto3.resource("dynamodb")``), or a test
        double; the record is written through ``ddb_client.Table(table_name)``.
    table_name:
        DynamoDB table name (from :func:`get_runs_table_name`).

    Notes
    -----
    A DynamoDB write failure is caught, logged at WARNING level, and does
    **not** propagate.  The nightly health run must never abort because of
    a run-record write failure (spec D10).

    Returns
    -------
    bool
        ``True`` only when the item was stored. The runner emits the
        ``RunRecordsWritten`` heartbeat on ``True`` alone, so the run-staleness
        alarm fires on a skipped or failed write.
    """
    item = {
        "agent_component": f"{agent_id}#{component}",
        "started_at": started_at,
        "finished_at": finished_at,
        "status": status,
        "artifacts_written": artifacts_written,
        "duplicates_skipped": duplicates_skipped,
        "agent_version": agent_version,
    }
    # Cycle 3 Warning (2026-10-04): breaker counters.  Only written when
    # present so a nightly run that never built a Strands agent (an
    # explicit test-double path) does not record spurious zeros.  "0" and
    # "" are valid outcomes when the breaker exists but did not fire — if
    # callers want to suppress zero-writes they must pass the default
    # sentinel instead of calling.  The three fields appear atomically or
    # not at all so a reader can tell whether this record carries them.
    if (
        llm_calls
        or llm_skipped_by_breaker
        or llm_breaker_trip_reason
    ):
        item["llm_calls"] = int(llm_calls)
        item["llm_skipped_by_breaker"] = int(llm_skipped_by_breaker)
        item["llm_breaker_trip_reason"] = str(llm_breaker_trip_reason)
    for name, value in (
        ("vins_selected", vins_selected),
        ("model_calls", model_calls),
        ("deferred", deferred),
        ("tokens_used", tokens_used),
        ("write_failures", write_failures),
        ("rules_fallbacks", rules_fallbacks),
        ("guardrail_interventions", guardrail_interventions),
        ("guardrail_errors", guardrail_errors),
        # FI recall/warranty counters (stage C Fix Group G3-1 F8, review cycle 1 S3).
        ("rows_refused", rows_refused),
        ("grounding_read_errors", grounding_read_errors),
        ("book_items_without_rooftops", book_items_without_rooftops),
        ("rows_without_terms", rows_without_terms),
        # FI fleet-health service counters (stage D G3F2.1 W4).
        ("p0_directive_fallbacks", p0_directive_fallbacks),
        ("vins_refused", vins_refused),
        ("exhausted_items_written", exhausted_items_written),
        ("agent2_read_errors", agent2_read_errors),
        # G3F3.1 (a1/a2 C1): vehicle-id/fleet-location read errors and cap overflow.
        ("registry_read_errors", registry_read_errors),
        ("vehicles_over_cap", vehicles_over_cap),
        # G3F3.1 (a2 W2): managed-first selection fallback count.
        ("managed_select_errors", managed_select_errors),
        # W-n5 / W-b2 (G4-2 F8): FI rebalance component counters.
        ("open_items_read_errors", open_items_read_errors),
        ("locations_incomplete", locations_incomplete),
        ("gap_items_without_vehicle", gap_items_without_vehicle),
        ("forecast_days_missing", forecast_days_missing),
        ("candidates_forecast_days_missing", candidates_forecast_days_missing),
    ):
        if value is not None:
            item[name] = int(value)
    # AVX-M2: list of ADP keys whose marker had row_count==0 this run.
    if empty_snapshot_keys is not None:
        item["empty_snapshot_keys"] = list(empty_snapshot_keys)
    if data_month is not None:
        item["data_month"] = str(data_month)
    if managed_coverage is not None:
        item["managed_coverage"] = _coverage_item(managed_coverage)
    if sim_operator is not None:
        # Plain ints/strings/maps only: the DynamoDB resource rejects floats, so a
        # float anywhere in the detail fails the put (and the caller counts an error).
        item["sim_operator"] = sim_operator
    if cost_usd is not None:
        # The DynamoDB resource rejects float; a Decimal from the rounded string keeps
        # the stored value equal to what the run reported.
        item["cost_usd"] = Decimal(str(round(float(cost_usd), 6)))
    if not started_at:
        # The sort key; DynamoDB rejects an empty string key attribute.
        log.warning("run_records: no started_at for agent_id=%s component=%s; not written",
                    agent_id, component)
        return False
    # T1.5: TTL — set to finished_at + 400 days (epoch seconds).
    # The attribute name is 'ttl' (matches stack timeToLiveAttribute and all AVX tables).
    item["ttl"] = _compute_run_ttl(finished_at)
    # `ddb_client` is a boto3 DynamoDB *resource* (`boto3.resource("dynamodb")`), whose
    # Table serialises plain Python values. The low-level client takes typed values
    # (`{"S": ...}`), and plain values fail its parameter validation against the real
    # service model, which the except below would hide on every run. Pinned by
    # `test_write_validates_against_the_real_service_model` (botocore Stubber).
    #
    # T1.5: conditional put — attribute_not_exists(agent_component) ensures a run record
    # never replaces another.  On ConditionalCheckFailedException the same (PK, SK) already
    # exists; retry once with a fresh millisecond timestamp to get a distinct SK.
    _condition = "attribute_not_exists(agent_component)"
    try:
        ddb_client.Table(table_name).put_item(
            Item=item,
            ConditionExpression=_condition,
        )
        return True
    except Exception as exc:  # noqa: BLE001
        from botocore.exceptions import ClientError  # noqa: PLC0415
        if isinstance(exc, ClientError) and exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
            # Retry once with a new timestamp so the SK is different and the first item is
            # never overwritten.  F1.5 S1: use started_at + 1 ms (keeps start ≤ finish).
            # Using datetime.now() instead would produce a started_at later than finished_at
            # on any non-trivial run.
            try:
                _orig_dt = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
                new_ts = (
                    (_orig_dt + timedelta(milliseconds=1))
                    .isoformat(timespec="milliseconds")
                    .replace("+00:00", "Z")
                )
            except (ValueError, AttributeError):
                # started_at is unparseable (shouldn't happen, but fall back to wall clock)
                new_ts = (
                    datetime.now(timezone.utc)
                    .isoformat(timespec="milliseconds")
                    .replace("+00:00", "Z")
                )
            retry_item = {**item, "started_at": new_ts}
            try:
                ddb_client.Table(table_name).put_item(
                    Item=retry_item,
                    ConditionExpression=_condition,
                )
                return True
            except Exception:  # noqa: BLE001
                log.warning(
                    "run_records: conditional retry failed "
                    "agent_id=%s component=%s started_at=%s table=%s",
                    agent_id, component, new_ts, table_name,
                )
                return False
        log.warning(
            "run_records: failed to write run record "
            "agent_id=%s component=%s started_at=%s table=%s",
            agent_id,
            component,
            started_at,
            table_name,
        )
        return False
