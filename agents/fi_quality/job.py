"""Investigation job for the FI quality agent — Stage D task 3.3.

Entry point: ``run_investigation``.

Spec D10 steps 1–5:
  1. **Bundle** (deterministic): read pattern group + candidates; affected
     VINs with build facts; matched controls; co-occurring codes; failure
     modes with remedy options; existing recall and software campaigns.
  2-3. Root cause and design options: with a Bedrock client, the two model
     calls in ``agents.fi_quality.llm.investigate`` (task 4.2); without one,
     the rules fallback (``investigation_fallback``).  Numbers always come from
     the corpus via ``build_options_evidence``.
  4. Write the Finding via the injected ``write_finding`` callable.
  5. Set ``completed`` with the new ``finding_id``.

Any exception during steps 1–5 sets the investigation status to ``failed``
with the fixed sentence and re-raises so the SQS Lambda errors, triggering
one redrive (spec D19).

Status transitions
------------------
queued  → running   (conditional: only from ``queued``)
running → completed
*       → failed    (on error; unconditional)

No-op states
------------
``completed`` and ``running`` are no-ops on redelivery.  The caller returns
without any Athena or DDB write.

Unknown pattern
---------------
When ``read_pattern_group`` returns no rows the investigation is set to
``failed`` with the same fixed sentence and the exception re-raises.

Mutation guards (verified by tests)
------------------------------------
1. **Drop the ``queued`` condition on the running transition.**
   Without the ConditionExpression a ``failed`` investigation (on its
   SQS redrive) is silently set to ``running`` instead of raising
   ConditionalCheckFailedException.  The test stubs a ``failed`` row and
   asserts the function raises.

2. **Leave the status ``running`` on failure.**
   If ``_set_failed`` is not called (or is called after re-raise), the
   DDB row keeps ``status=running`` instead of ``status=failed``.  The
   test stubs a pattern-query failure and asserts the final DDB status is
   ``failed``.

3. **Read only the first Athena page.**
   Missing ``NextToken`` follow-through silently truncates results.  Each
   reader test stubs two pages and asserts both are consumed.

4. **Take affected VINs from the request instead of ``fault_patterns``.**
   The investigation request row (DDB) has no VIN list; reading VINs from
   it would produce an empty list and a zero-count Finding.  The test
   asserts that ``quality.affected_vins.count`` matches
   ``fault_patterns.vehicles_affected``, not 0.
"""
from __future__ import annotations

import datetime
import logging
from typing import Any, Callable, Optional

from botocore.exceptions import ClientError
from boto3.dynamodb.types import TypeDeserializer

from agents.fi_quality.bundle import build_bundle
from agents.fi_quality.candidates import build_candidates_from_pattern_rows
from agents.fi_quality.fallback import investigation_fallback
from agents.fi_quality.options import build_options_evidence
from agents.fi_quality.readers import (
    read_affected_vins,
    read_campaigns,
    read_co_occurring_codes,
    read_controls,
    read_failure_modes,
    read_service_history,
    read_telemetry_aggregates,
    read_pattern_group,
)
from agents.fi_quality.write import build_investigation_finding, is_live_investigation
from agents.tier2.snapshot_freshness import (
    read_snapshot_dates as _read_snap_dates,
    require_fresh as _require_fresh_snap,
    empty_keys as _empty_snap_keys,
)

log = logging.getLogger(__name__)

_DESER = TypeDeserializer()

# ---------------------------------------------------------------------------
# Public exception
# ---------------------------------------------------------------------------

class PatternNotFound(KeyError):
    """No ``fault_patterns`` rows found for the requested pattern_id."""


# ---------------------------------------------------------------------------
# Fixed failure message (spec D10 step 5 / D19)
# ---------------------------------------------------------------------------

#: Written to DDB and surfaced on the CS screen.
#: Mutation guard (test_failure_sets_status_failed):
#:   Any deviation (e.g. a different string or no update at all) fails the
#:   assertion that checks ``error_message`` in the updated DDB item.
_FAILURE_MSG: str = "The investigation could not finish. Start it again."

# ---------------------------------------------------------------------------
# Investigation field names in the DDB item
# ---------------------------------------------------------------------------

_F_INV_ID   = "investigation_id"
_F_PATTERN  = "pattern_id"
_F_STATUS   = "status"
_F_FINDING  = "finding_id"
_F_ERR_MSG  = "error_message"


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def run_investigation(
    investigation_id: str,
    *,
    athena_client: Any,
    workgroup: str,
    results_s3_path: str,
    stage: str,
    ddb_client: Any,
    investigations_table: str,
    write_finding: Callable[[dict[str, Any]], None],
    now: Optional[Callable[[], str]] = None,
    approximate_receive_count: int = 1,
    max_receive_count: int = 2,
    bedrock_client: Optional[Any] = None,
    model_config: Optional[Any] = None,
    remaining_seconds: Optional[Callable[[], float]] = None,
    model_usage_out: Optional[dict] = None,
) -> None:
    """Run the investigation job for *investigation_id*.

    Model path (task 4.2): with ``bedrock_client`` and ``model_config`` (from
    ``agents.fi_quality.llm.investigation_config``) the root cause and design
    options come from the two model calls, within the config's per-investigation
    call and token budget; ``model_usage_out`` (a dict) receives the calls, tokens,
    ``diagnosis_source`` and ``rules_reason``.  Without a client: the rules path.

    Reads the investigation request from *investigations_table*, builds the
    bundle from ADP Athena, applies the rules fallback, writes the Finding,
    and marks the request ``completed``.

    On failure below ``max_receive_count``, the row is reset to ``queued``
    and the exception re-raises so SQS redelivers the message (one retry).
    At ``max_receive_count`` the row is set to ``failed`` (DLQ path, spec D19).

    Parameters
    ----------
    investigation_id:
        Primary key in *investigations_table*.
    athena_client:
        boto3 Athena client (or botocore-stubber-backed client in tests).
    workgroup:
        Athena workgroup, e.g. ``cvx-staging-analytics``.
    results_s3_path:
        S3 URI for Athena results, e.g.
        ``s3://cvx-staging-athena-results-<acct>-us-east-1/tier2/``.
    stage:
        Deployment stage (``staging`` / ``prod``).
    ddb_client:
        boto3 DynamoDB client (low-level; items use type-descriptor format).
    investigations_table:
        DynamoDB table name, e.g. ``vsa-staging-avx-investigations``.
    write_finding:
        Pre-bound callable ``(finding: dict) -> None``.  Injected so tests
        can substitute a fake without real DynamoDB.
    now:
        Optional callable ``() -> str`` returning an ISO-8601 UTC string.
        Defaults to ``_utc_now``.
    approximate_receive_count:
        SQS ``ApproximateReceiveCount`` for the current delivery (default 1).
        Below ``max_receive_count``, an error resets the row to ``queued``
        (one redrive); at the limit, the row is set to ``failed`` (DLQ).
    max_receive_count:
        Maximum SQS receive count before the message goes to the DLQ.
        Defaults to 2 (one delivery + one redrive).

    Raises
    ------
    ConditionalCheckFailedException (botocore ClientError)
        When the status is ``failed`` on a SQS redrive and the conditional
        update to ``running`` fails.  The Lambda errors → DLQ.
    PatternNotFound
        When ``fault_patterns`` returns no rows for the pattern.  Sets the
        DDB status to ``failed`` before re-raising.
    """
    if bedrock_client is not None:
        from agents.fi_fleet_health.llm import ModelConfig
        if not isinstance(model_config, ModelConfig):
            raise ValueError("the model path needs a ModelConfig (guardrail, caps)")
    _now = now or _utc_now
    ts = _now()

    # ── 1. Read the request row ───────────────────────────────────────────
    item = _get_investigation(ddb_client, investigations_table, investigation_id)

    pattern_id: str = item.get(_F_PATTERN, "")
    status: str = item.get(_F_STATUS, "")

    log.debug(
        "run_investigation: investigation_id=%s pattern_id=%s status=%s",
        investigation_id, pattern_id, status,
    )

    # ── 2. No-op for completed (redelivery); handle stale running ────────────
    #
    # C1 fix (review-b C1, review-a2 C3):
    # A running row older than _STALE_AGE_SECONDS means the Lambda timed out
    # mid-job.  On SQS redelivery we mark it failed and re-raise so SQS
    # counts it as a failure and can DLQ it.  A fresh running row (concurrent
    # redelivery) is still a no-op.
    if status == "completed":
        log.debug(
            "run_investigation: no-op for status=completed investigation_id=%s",
            investigation_id,
        )
        return

    # C2/W2 fix (decision 5 / b W2): A row the route took over is marked
    # ``failed`` before a new investigation is admitted.  If the old SQS
    # message for the superseded row arrives, it is a no-op success — the
    # DLQ alarm must NOT fire on a normal "Start it again" path.
    #
    # Mutation guard (test_failed_row_is_noop_redelivery):
    #   Raising on a failed row sends it to the DLQ, which fires the alarm
    #   on every user restart; the test asserts batchItemFailures == [].
    if status == "failed":
        log.info(
            "run_investigation: no-op for status=failed (superseded) "
            "investigation_id=%s",
            investigation_id,
        )
        return

    if status == "running":
        req_at = item.get("started_at") or item.get("requested_at", "")
        if is_live_investigation("running", req_at):
            # C1 fix (cycle-3 answer 4): a fresh running row means another Lambda
            # is currently executing this investigation.  Raise so the SQS message
            # comes back (retryable); never a silent success.
            #
            # Mutation guard (test_fresh_running_raises_retryable):
            #   Returning silently (no-op) means probe N4 gets batchItemFailures=0
            #   and the row stays running indefinitely; the test asserts raise.
            log.info(
                "run_investigation: fresh running, raising retryable error "
                "investigation_id=%s",
                investigation_id,
            )
            raise RuntimeError(
                f"investigation_id {investigation_id!r} is already being processed"
            )
        # Stale running row — Lambda died mid-job.
        # C1 fix (cycle-3 answer 4): re-run in this delivery (not requeued via SQS).
        # Reset to queued (conditional on old started_at so we don't race a fresh
        # Lambda), then fall through to _set_running which moves queued → running
        # with the new ts.  If _reset fails (another Lambda won), _set_running raises
        # ConditionalCheckFailedException → except block handles it normally.
        #
        # Mutation guard (test_stale_running_reruns_in_same_delivery):
        #   Always raising after stale detection skips the re-run; the test asserts
        #   write_finding is called and status becomes completed.
        log.error(
            "run_investigation: stale running row investigation_id=%s "
            "approximate_receive_count=%d max=%d; re-running in this delivery",
            investigation_id, approximate_receive_count, max_receive_count,
        )
        _reset_stale_running_to_queued(
            ddb_client, investigations_table, investigation_id,
            old_started_at=req_at,
            queued_at=ts,
        )
        # Fall through: _set_running moves queued → running with ts.

    # ── 3. Conditional: set running (only from queued) ────────────────────
    #
    # Mutation guard: dropping this ConditionExpression lets a 'failed'
    # investigation (on SQS redrive) proceed to run again instead of raising.
    _set_running(ddb_client, investigations_table, investigation_id, ts)

    # ── 4. Do the work ────────────────────────────────────────────────────
    try:
        # Snapshot freshness guard (G3F6F1.5 Part 2A):
        # read_controls reads managed_vehicles; read_co_occurring_codes reads
        # vehicle_fault_status_daily.  Both must be fresh before the bundle
        # assembly reads them.  Raising here falls through to the except block
        # below, which sets status=failed (or queued for redrive) and re-raises.
        _snap_today = datetime.date.fromisoformat(ts[:10])
        _fi_snap_dates = _read_snap_dates(
            stage,
            ["managed_vehicles", "vehicle_fault_status_daily"],
            athena_client=athena_client,
            workgroup=workgroup,
            results_s3_path=results_s3_path,
        )
        _require_fresh_snap(_fi_snap_dates, today=_snap_today, same_date=False)
        _fi_empty = _empty_snap_keys(_fi_snap_dates)

        bundle = _build_investigation_bundle(
            pattern_id=pattern_id,
            stage=stage,
            athena_client=athena_client,
            workgroup=workgroup,
            results_s3_path=results_s3_path,
            skip_co_occurring="vehicle_fault_status_daily" in _fi_empty,
        )

        model_options = None
        detail = ""
        tokens_used, cost = 0, 0.0
        if bedrock_client is not None:
            from agents.fi_fleet_health.llm import cost_usd as _cost_usd
            from agents.fi_quality.llm import investigate
            outcome = investigate(bundle, bedrock_client=bedrock_client,
                                  config=model_config,
                                  remaining_seconds=remaining_seconds)
            fallback_result = outcome["root_cause"]
            model_options = outcome["model_options"]
            usage = outcome["usage"]
            tokens_used = usage["input_tokens"] + usage["output_tokens"]
            cost = _cost_usd(usage["input_tokens"], usage["output_tokens"])
            if outcome["diagnosis_source"] == "model":
                detail = fallback_result.get("reasoning") or ""
            if model_usage_out is not None:
                model_usage_out.update(usage)
                model_usage_out["diagnosis_source"] = outcome["diagnosis_source"]
                model_usage_out["rules_reason"] = outcome["rules_reason"]
        else:
            fallback_result = investigation_fallback(bundle)

        failure_mode = _get_failure_mode(bundle, fallback_result)
        total_vehicles = _count_affected(bundle)
        options = build_options_evidence(
            failure_mode=failure_mode,
            total_vehicles=total_vehicles,
            model_options=model_options,
        )

        finding = build_investigation_finding(
            investigation_id=investigation_id,
            pattern_id=pattern_id,
            bundle=bundle,
            root_cause=fallback_result,
            options=options,
            computed_at=ts,
            detail=detail,
            tokens_used=tokens_used,
            cost_usd=cost,
        )

        write_result = write_finding(finding)
        # W4 b fix: the writer is idempotent on finding_key; on a _set_completed
        # failure + redrive the writer returns the existing row's finding_id.
        # Use the writer's returned id (authoritative) not the locally-generated one.
        finding_id: str = (
            (write_result or {}).get("finding_id") if isinstance(write_result, dict) else None
        ) or finding["finding_id"]

        # ── 5. Set completed ──────────────────────────────────────────────
        # C1 fix: _set_completed is inside the try/except so a throttle after
        # write_finding doesn't leave the Finding written with the row stuck
        # in 'running' forever.
        #
        # C2 fix (decision 5): conditional on status=running AND started_at=ts
        # so a row the route has already taken over (marked failed) cannot flip
        # back to completed.  If the condition fails, the except block handles
        # it as an error (requeue below max, failed at max).
        _set_completed(ddb_client, investigations_table, investigation_id, finding_id, started_at=ts)

    except Exception:
        # ── Mutation guard: set_failed MUST be called before re-raise ────
        # Leaving status=running causes test_failure_sets_status_failed to fail.
        #
        # W2 fix (decision 4): Below the redrive limit → reset to queued and
        # re-raise so SQS redelivers.  At the limit → set failed (DLQ).
        # Mutation guard (test_first_error_requeues_for_redrive):
        #   Always setting failed skips the redrive; the test catches it.
        # Mutation guard (test_last_attempt_sets_failed):
        #   Always requeueing on the last attempt means the row never reaches
        #   failed; the test catches it.
        if approximate_receive_count < max_receive_count:
            _set_queued(ddb_client, investigations_table, investigation_id, ts=ts)
        else:
            _set_failed(ddb_client, investigations_table, investigation_id)
        raise

    log.info(
        "run_investigation: completed investigation_id=%s pattern_id=%s finding_id=%s",
        investigation_id, pattern_id, finding_id,
    )


# ---------------------------------------------------------------------------
# DynamoDB helpers
# ---------------------------------------------------------------------------


def _get_investigation(
    ddb_client: Any,
    table: str,
    investigation_id: str,
) -> dict[str, Any]:
    """Read and deserialise the investigation row; raises if absent."""
    response = ddb_client.get_item(
        TableName=table,
        Key={_F_INV_ID: {"S": investigation_id}},
        ConsistentRead=True,
    )
    raw = response.get("Item")
    if raw is None:
        raise PatternNotFound(
            f"investigation_id {investigation_id!r} not found in {table!r}"
        )
    return {k: _DESER.deserialize(v) for k, v in raw.items()}


def _reset_stale_running_to_queued(
    ddb_client: Any,
    table: str,
    investigation_id: str,
    old_started_at: str,
    *,
    queued_at: str = "",
) -> None:
    """Conditionally reset a stale ``running`` row to ``queued`` (C1 fix).

    Called when a redelivered SQS message finds a ``running`` row that is
    older than the stale age limit (the Lambda timed out mid-job).

    Condition: ``status = running AND started_at = :old_ts`` — both must
    be unchanged since our read.  If another Lambda has since updated either
    (e.g. a fresh delivery set a new ``started_at``), the condition fails
    silently; the fresh Lambda is handling the row.

    Sets ``queued_at`` so the staleness predicate (route, GET) measures from
    the requeue time, not the original ``requested_at``.

    Mutation guard (test_stale_running_requeues_below_max):
        Always setting ``failed`` instead of ``queued`` skips the redrive;
        the test asserts ``status = queued`` on the first attempt.

    Mutation guard (test_reset_condition_uses_started_at):
        An always-true condition resets even when started_at changed, which
        kills a freshly-started Lambda's work; the test seeds a row with a
        new started_at and asserts the reset is rejected.
    """
    _qa = queued_at or _utc_now()
    try:
        ddb_client.update_item(
            TableName=table,
            Key={_F_INV_ID: {"S": investigation_id}},
            UpdateExpression="SET #s = :queued, queued_at = :qa REMOVE started_at",
            ConditionExpression="#s = :running AND started_at = :old_ts",
            ExpressionAttributeNames={"#s": _F_STATUS},
            ExpressionAttributeValues={
                ":queued":  {"S": "queued"},
                ":running": {"S": "running"},
                ":old_ts":  {"S": old_started_at},
                ":qa":      {"S": _qa},
            },
        )
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "run_investigation: _reset_stale_running_to_queued failed for "
            "investigation_id=%s (may have been taken by another delivery): %s",
            investigation_id,
            type(exc).__name__,
        )


def _set_running(
    ddb_client: Any,
    table: str,
    investigation_id: str,
    started_at: str,
) -> None:
    """Conditionally set status to ``running`` — only if currently ``queued``.

    Also writes ``started_at`` so the stale-age predicate can measure elapsed
    time on a redelivered ``running`` row (C1 fix / review-b C1).

    Raises ``ClientError`` with code ``ConditionalCheckFailedException`` when
    the status is not ``queued`` (e.g. ``failed`` on a SQS redrive).

    Mutation guard (TestCycle4SetRunningOnlyFromQueued, on moto): a row the
        route takes over after the job's read must not run. Dropping or widening
        the ConditionExpression runs the superseded id and writes a Finding.
    """
    ddb_client.update_item(
        TableName=table,
        Key={_F_INV_ID: {"S": investigation_id}},
        UpdateExpression="SET #s = :running, started_at = :ts",
        ConditionExpression="#s = :queued",
        ExpressionAttributeNames={"#s": _F_STATUS},
        ExpressionAttributeValues={
            ":running": {"S": "running"},
            ":queued":  {"S": "queued"},
            ":ts":      {"S": started_at},
        },
    )


def _set_completed(
    ddb_client: Any,
    table: str,
    investigation_id: str,
    finding_id: str,
    *,
    started_at: str = "",
) -> None:
    """Set status to ``completed`` and record the ``finding_id``.

    C2 fix (decision 5): conditional on ``status = running AND started_at = :ts``
    so a row the route has already taken over (marked ``failed``) cannot flip
    back to ``completed``.  A failed condition raises ClientError
    (ConditionalCheckFailedException) which propagates to the caller's except
    block and triggers the requeue / failed path.

    Mutation guard (test_set_completed_cannot_overwrite_route_takeover):
        Removing the condition lets a superseded job's completion overwrite the
        route's ``failed`` mark; the test asserts the row stays ``failed``.
    """
    kwargs: dict = {
        "TableName": table,
        "Key": {_F_INV_ID: {"S": investigation_id}},
        "UpdateExpression": "SET #s = :completed, finding_id = :fid",
        "ExpressionAttributeNames": {"#s": _F_STATUS},
        "ExpressionAttributeValues": {
            ":completed": {"S": "completed"},
            ":fid":       {"S": finding_id},
        },
    }
    if started_at:
        kwargs["ConditionExpression"] = "#s = :running AND started_at = :ts"
        kwargs["ExpressionAttributeValues"][":running"] = {"S": "running"}
        kwargs["ExpressionAttributeValues"][":ts"] = {"S": started_at}
    ddb_client.update_item(**kwargs)


def _set_failed(
    ddb_client: Any,
    table: str,
    investigation_id: str,
) -> None:
    """Set status to ``failed`` with the fixed failure message.

    Unconditional: called from the except clause so it must not raise on
    any pre-condition.

    Mutation guard (test_failure_sets_status_failed):
        Not calling _set_failed (leaving status=running) means the DDB
        row retains status=running and the assertion on the updated item
        fails.
    """
    try:
        ddb_client.update_item(
            TableName=table,
            Key={_F_INV_ID: {"S": investigation_id}},
            UpdateExpression="SET #s = :failed, #em = :msg",
            ExpressionAttributeNames={
                "#s":  _F_STATUS,
                "#em": _F_ERR_MSG,
            },
            ExpressionAttributeValues={
                ":failed": {"S": "failed"},
                ":msg":    {"S": _FAILURE_MSG},
            },
        )
    except Exception as exc:  # noqa: BLE001
        # Never let the _set_failed write shadow the original exception.
        log.error(
            "run_investigation: _set_failed write itself failed for "
            "investigation_id=%s: %s",
            investigation_id,
            type(exc).__name__,
        )


def _set_queued(
    ddb_client: Any,
    table: str,
    investigation_id: str,
    ts: str = "",
) -> None:
    """Reset status to ``queued`` for a first-attempt failure (W2 fix).

    Called when the job fails on its first SQS delivery.  The SQS message
    is then redelivered (one redrive) and the job can try again.  On the
    second failure (or if the row is missing), ``_set_failed`` is used instead.

    Sets ``queued_at`` so GET and the pre-read measure staleness from the
    requeue time, not the original ``requested_at``.  This prevents GET from
    reporting ``failed`` while a redrive is still coming (b W3 / a2 W3 fix).

    Mutation guard (test_first_error_requeues_for_redrive):
        If this always sets failed instead of queued, the row cannot be
        retried and the test asserts status=queued, which fails.

    Mutation guard (test_set_queued_condition_prevents_failed_overwrite):
        The condition ``#s = :running`` prevents overwriting a ``failed`` row
        that the route has taken over; an always-true condition sets a
        route-failed row back to ``queued`` and the test catches it.
    """
    _qa = ts or _utc_now()
    try:
        ddb_client.update_item(
            TableName=table,
            Key={_F_INV_ID: {"S": investigation_id}},
            UpdateExpression="SET #s = :queued, queued_at = :qa",
            ConditionExpression="#s = :running",
            ExpressionAttributeNames={"#s": _F_STATUS},
            ExpressionAttributeValues={
                ":queued":  {"S": "queued"},
                ":running": {"S": "running"},
                ":qa":      {"S": _qa},
            },
        )
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "run_investigation: _set_queued write failed for "
            "investigation_id=%s (may already be failed): %s",
            investigation_id,
            type(exc).__name__,
        )


# ---------------------------------------------------------------------------
# Bundle assembly
# ---------------------------------------------------------------------------


def _build_investigation_bundle(
    *,
    pattern_id: str,
    stage: str,
    athena_client: Any,
    workgroup: str,
    results_s3_path: str,
    skip_co_occurring: bool = False,
) -> dict[str, Any]:
    """Build the investigation bundle for *pattern_id* from ADP Athena.

    Steps (spec D10 step 1):
    1. Read pattern group rows (all candidates).
       Raises ``PatternNotFound`` when no rows exist (unknown pattern).
    2. Extract affected VIN sample and vehicle model/year from the pattern.
    3. Read build facts from ``vehicle_build_components`` for affected VINs.
    4. Read matched controls (same model/year, unaffected).
    5. Read co-occurring codes on affected VINs.
    6. Read failure modes whose symptom codes overlap the code family.
    7. Read recall and OTA campaigns on affected VINs.
    8. Read service history for affected VINs.

    Mutation guard (test_affected_vins_from_fault_patterns_not_request):
        VINs come from ``fault_patterns.affected_vins_sample``, not from
        the DDB investigation request row (which has no VIN list).
        A mutation that reads VINs from the request would return an empty
        list, causing quality.affected_vins.count to be 0, failing the test.
    """
    _athena = dict(
        athena_client=athena_client,
        workgroup=workgroup,
        results_s3_path=results_s3_path,
    )

    # ── Read pattern group ────────────────────────────────────────────────
    pattern_rows = read_pattern_group(
        pattern_id, stage, **_athena  # type: ignore[arg-type]
    )
    if not pattern_rows:
        raise PatternNotFound(
            f"No fault_patterns rows found for pattern_id={pattern_id!r}"
        )

    # Top-ranked row provides the authoritative pattern metadata.
    top_row = pattern_rows[0]  # sorted ascending by candidate_rank
    code_family: str = top_row.get("code_family", "")
    # Affected VIN SAMPLE from fault_patterns (spec D10 step 1, D12 mutation guard).
    # The investigation request row has NO VIN list; VINs come from ADP here.
    affected_vins_sample: list[str] = top_row.get("affected_vins_sample") or []
    vehicles_affected: int = int(top_row.get("vehicles_affected") or 0)
    model: str = top_row.get("cohort_value", "") if top_row.get("cohort_dimension") == "model" else ""
    model_year_raw = top_row.get("cohort_value", "") if top_row.get("cohort_dimension") == "model_year" else ""

    # Build ranked candidates list for the bundle.
    candidates = build_candidates_from_pattern_rows(pattern_rows)

    # Derive model and model_year from the affected VINs' identity if the
    # pattern dimension is not model/model_year (plant, supplier_lot, etc.).
    # We use the first returned VIN's model from the build-facts query.

    # ── Read build facts for affected VINs ───────────────────────────────
    build_facts: list[dict[str, Any]] = []
    if affected_vins_sample:
        build_facts = read_affected_vins(
            affected_vins_sample, stage, **_athena  # type: ignore[arg-type]
        )

    # Derive component_ids from affected VINs' build facts
    # (for the failure mode filter — must be among the candidate causes).
    component_ids: list[str] = list({
        r["component_id"] for r in build_facts if r.get("component_id")
    })

    # W6 b fix (design answer 5): derive candidate_components from ranked candidates
    # (lot → its component via build_facts lookup; component dimension directly).
    # These replace all-build-facts component_ids for the failure-mode query so the
    # fallback can only recommend a failure mode whose component is a candidate cause.
    lot_to_component: dict[str, str] = {}
    for row in build_facts:
        lot = row.get("supplier_lot", "")
        comp = row.get("component_id", "")
        if lot and comp and lot not in lot_to_component:
            lot_to_component[lot] = comp

    candidate_components: dict[str, int] = {}  # component_id → best candidate rank
    for c in candidates:
        dim = c.get("dimension", "")
        val = c.get("value", "")
        rank = c.get("rank", 9999)
        if dim == "component" and val:
            if val not in candidate_components or rank < candidate_components[val]:
                candidate_components[val] = rank
        elif dim == "supplier_lot" and val:
            comp = lot_to_component.get(val, "")
            if comp:
                if comp not in candidate_components or rank < candidate_components[comp]:
                    candidate_components[comp] = rank

    # W6 fix (cycle-3 answer 7): only supplier_lot (and component) candidates contribute
    # components; other candidates (plant, build_window, model_year, …) match failure
    # modes by symptom codes only.  An empty component list makes read_failure_modes
    # match by symptom codes (5c45785).  Never fall back to all build_facts
    # component_ids, which includes components unrelated to the candidate cause.
    #
    # Cycle 4 b W5 (G3F4.7, recorded in decisions.md): the top-ranked candidate decides.
    # When it contributes no component (a plant or build window ranked above every lot),
    # a lower-ranked lot's components must not narrow the match, or the plant pattern is
    # investigated as `unknown`: match by symptom codes only (reader and fallback).
    #
    # Mutation guard (test_only_candidate_components_passed_to_read_failure_modes):
    #   `or component_ids` fallback passes all build_facts components including unrelated
    #   ones; the spy-on-read_failure_modes test catches when hv_battery reaches the query.
    ranks = [c.get("rank", 9999) for c in candidates if c.get("value")]
    if candidate_components and ranks and min(candidate_components.values()) > min(ranks):
        candidate_components = {}
    component_ids_for_fm: list[str] = list(candidate_components.keys())

    # Derive model and model_year from build facts when not set from pattern.
    if not model and build_facts:
        model = build_facts[0].get("model", "")
    if not model_year_raw and build_facts:
        model_year_raw = build_facts[0].get("model_year", "")

    # ── Assemble affected_vins for bundle (count + per-VIN build facts) ──
    # De-duplicate by VIN; one row per VIN (using the first component row
    # as the build fact representative for the bundle).
    vin_seen: set[str] = set()
    affected_vins_for_bundle: list[dict[str, Any]] = []
    for row in build_facts:
        vin = row.get("vin", "")
        if vin and vin not in vin_seen:
            vin_seen.add(vin)
            affected_vins_for_bundle.append({
                "vin":          vin,
                "model":        row.get("model", ""),
                "model_year":   row.get("model_year", ""),
                "build_month":  row.get("build_month", ""),
                "plant":        row.get("plant", ""),
                "component_lot": row.get("supplier_lot", ""),
                # Carry build fields for the evidence; supplier stripped by bundle
            })

    # Pad count to match vehicles_affected when sample is smaller.
    bundle_affected = affected_vins_for_bundle or [
        {"vin": v} for v in affected_vins_sample
    ]

    # ── Read controls ────────────────────────────────────────────────────
    controls: list[dict[str, Any]] = []
    try:
        model_year_int = int(model_year_raw) if model_year_raw else 0
    except (ValueError, TypeError):
        model_year_int = 0

    if model and model_year_int:
        controls = read_controls(
            model, model_year_int, affected_vins_sample, stage,
            **_athena,  # type: ignore[arg-type]
        )

    # ── Read co-occurring codes ──────────────────────────────────────────
    # Skip when the vehicle_fault_status_daily marker says row_count == 0
    # (the ADP job ran but found no fault rows today) — the table's MAX(snapshot_date)
    # may still point at an older snapshot, so reading it would mix vintages.
    co_codes = (
        [] if skip_co_occurring
        else read_co_occurring_codes(
            affected_vins_sample, code_family, stage, **_athena  # type: ignore[arg-type]
        )
    )

    # ── Read failure modes ───────────────────────────────────────────────
    failure_modes = read_failure_modes(
        code_family, component_ids_for_fm, stage, **_athena  # type: ignore[arg-type]
    )

    # ── Read campaigns ───────────────────────────────────────────────────
    campaigns = read_campaigns(
        affected_vins_sample, stage, **_athena  # type: ignore[arg-type]
    )

    # ── Read service history ─────────────────────────────────────────────
    service_history = read_service_history(
        affected_vins_sample, stage, **_athena  # type: ignore[arg-type]
    )

    # ── Read telemetry (no-op stub; task 4.x follow-on) ─────────────────
    telemetry = read_telemetry_aggregates(
        affected_vins_sample, stage, **_athena  # type: ignore[arg-type]
    )

    # ── Build pattern dict for bundle ────────────────────────────────────
    pattern_dict = {
        "pattern_id":      top_row.get("pattern_id", ""),
        "code_family":     code_family,
        "dimension":       top_row.get("cohort_dimension", ""),
        "value":           top_row.get("cohort_value", ""),
        "ratio":           float(top_row.get("rate_ratio") or 0.0),
        "p_value":         float(top_row.get("p_value") or 1.0),
        "method":          top_row.get("method", "poisson_bonferroni"),
        "first_detected":  top_row.get("first_detected_month", ""),
        "trend":           top_row.get("trend", ""),
        "vehicles_affected": vehicles_affected,
        # Co-occurring codes for the model's awareness.
        "co_occurring_codes": co_codes,
        # Campaign summaries for context.
        "existing_recall_campaigns": campaigns.get("recall", []),
        "existing_ota_campaigns":    campaigns.get("ota", []),
    }

    # fleet_type: not available at investigation time without a specific
    # managed fleet's identity; leave as empty string (cohort label).
    fleet_type = ""

    bundle = build_bundle(
        pattern=pattern_dict,
        failure_modes=failure_modes,
        fleet_type=fleet_type,
        affected_vins=bundle_affected,
        controls=controls,
        service_history=service_history,
        candidates=candidates,
    )

    # Attach telemetry as a non-fleet-private supplement (not filtered by bundle).
    bundle["telemetry"] = telemetry
    # W6 b fix: carry candidate_components map so the fallback can rank failure
    # modes by the candidate whose component they target.
    bundle["candidate_components"] = candidate_components

    return bundle


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_failure_mode(
    bundle: dict[str, Any],
    fallback_result: dict[str, Any],
) -> dict[str, Any]:
    """Return the failure mode dict chosen by the fallback result."""
    fm_id = fallback_result.get("failure_mode_id", "unknown")
    for fm in bundle.get("failure_modes") or []:
        if fm.get("failure_mode_id") == fm_id:
            return fm
    return {"failure_mode_id": "unknown", "remedy_options": []}


def _count_affected(bundle: dict[str, Any]) -> int:
    """Return the affected vehicle count from the bundle's pattern."""
    return int(
        (bundle.get("pattern") or {}).get("vehicles_affected") or 0
    )


def _utc_now() -> str:
    """Return the current UTC time as an ISO-8601 Z string."""
    return (
        datetime.datetime.now(datetime.timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )
