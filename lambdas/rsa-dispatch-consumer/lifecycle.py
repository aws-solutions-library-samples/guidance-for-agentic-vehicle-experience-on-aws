"""RSA Lifecycle Advancer — simulated dispatch state machine.

Triggered on a ``rate(1 minute)`` EventBridge Scheduler schedule
(G3-D1: EventBridge chosen over Step Functions — idle cost is effectively
zero and the DLQ reconciliation loop requires a periodic query anyway).

Each invocation:
  1. EN_ROUTE → ARRIVED — rows whose createdAt ≤ (now − arrivedAfterSeconds).
  2. DISPATCHED → EN_ROUTE — rows whose createdAt ≤ (now − enRouteAfterSeconds).
  3. Stale REQUESTED → FAILED — rows whose createdAt ≤ (now − staleAfterSeconds)
     that never made it to DISPATCHED (DLQ reconciliation, Accept 5 / review W5-2).

Ordering per G3-D5: EN_ROUTE→ARRIVED first so a DISPATCHED row with createdAt
in the ARRIVED window moves one state this run and will advance again next run.
Processing EN_ROUTE first prevents a DISPATCHED row from jumping two states in a
single invocation.

Design decisions (G3-D2 … G3-D6):
  - Only ``simulated = true`` rows are advanced (G3-D4); enforced in the
    UpdateItem ConditionExpression, not in the query (the GSI is KEYS_ONLY).
  - Time base: ``createdAt`` (GSI sort key), not ``dispatchedAt`` (G3-D2).
  - Timestamps are ISO-8601 UTC with milliseconds and a ``Z`` suffix (G3-D3).
  - Exactly one state transition per invocation (G3-D5); ConditionExpression
    names the exact expected current status.
  - Timings come from env vars (G3-D6); fail closed on invalid values.
  - DLQ reconciliation does NOT depend on ``simulated`` — a stranded REQUESTED
    row never claimed progress regardless of whether it is real or fake.
    It also does NOT require a DLQ payload — a DDB-stream DLQ message carries
    batch metadata, not row identifiers (review Cycle 6). The advancer scans
    for stale REQUESTED rows and marks them FAILED; rows that are healthy
    (the consumer is still retrying) are left alone because their ``createdAt``
    is recent.

Terminal-state guard (spec R7):
  Every UpdateItem carries a ConditionExpression requiring the row is in the
  exact expected state AND is not terminal (belt-and-suspenders, identical to
  the consumer). ConditionExpression on the stale-REQUESTED path requires
  status == REQUESTED only (the stale threshold already exceeded the retry
  window, so a row in REQUESTED at T+stale must be stuck; a healthy row will
  be in DISPATCHED/EN_ROUTE before the stale threshold fires).

Pagination:
  Every GSI Query follows LastEvaluatedKey to exhaustion (G3-D2 mandates no
  Scan, KEYS_ONLY projection is used, and a demo fleet is small; see test
  guard on pagination).

PII note: vin/lat/lon are NOT in the KEYS_ONLY GSI projection. This handler
never reads or logs PII.

Module-level os.environ reads:
  RSA_DISPATCH_TABLE  — table name (mandatory; fail closed if absent)
  RSA_SIM_EN_ROUTE_AFTER_SECONDS   — positive int; default 60
  RSA_SIM_ARRIVED_AFTER_SECONDS    — positive int > en_route_seconds; default 300
  RSA_REQUESTED_STALE_AFTER_SECONDS — positive int; default 900

Emits structured log lines so CloudWatch Logs metric filter can count
reconciliations:  ``[RSA_RECONCILIATION] dispatchId=<id>``
"""
from __future__ import annotations

import datetime
import logging
import os
from typing import Any

import boto3
from boto3.dynamodb.conditions import Attr
from botocore.exceptions import BotoCoreError, ClientError

from shared.rsa_client import (
    LIFECYCLE_ARRIVED,
    LIFECYCLE_DISPATCHED,
    LIFECYCLE_EN_ROUTE,
    LIFECYCLE_FAILED,
    LIFECYCLE_REQUESTED,
    LIFECYCLE_TERMINAL_STATES,
    iso_now as _iso_now_shared,
)

log = logging.getLogger()
log.setLevel(logging.INFO)

# ---------------------------------------------------------------------------
# Constants — log token for metric filter
# ---------------------------------------------------------------------------

RECONCILIATION_LOG_TOKEN = "[RSA_RECONCILIATION]"

# ---------------------------------------------------------------------------
# Environment — fail closed: invalid values disable the advancer fleet-wide,
# which is safer than a silent wrong-threshold deploy.
# ---------------------------------------------------------------------------

_RSA_DISPATCH_TABLE = os.environ["RSA_DISPATCH_TABLE"]


def _parse_positive_int(name: str, default: int) -> int:
    """Parse an env var as a positive integer; fail closed on invalid values.

    A value of 0 or a negative number would disable lifecycle advancement,
    which matches ``abc`` in effect but is harder to detect in monitoring.
    Raising here surfaces the misconfiguration at the Lambda cold-start log
    line rather than at the first dispatch row check.
    """
    raw = os.environ.get(name, "")
    if not raw:
        return default
    try:
        val = int(raw)
    except ValueError:
        raise ValueError(
            f"{name}={raw!r} is not an integer — "
            "RSA lifecycle advancer disabled (fail-closed on invalid timing config)"
        )
    if val <= 0:
        raise ValueError(
            f"{name}={val} is not a positive integer — "
            "RSA lifecycle advancer disabled (fail-closed on non-positive timing config)"
        )
    return val


_EN_ROUTE_AFTER_SECONDS = _parse_positive_int("RSA_SIM_EN_ROUTE_AFTER_SECONDS", 60)
_ARRIVED_AFTER_SECONDS = _parse_positive_int("RSA_SIM_ARRIVED_AFTER_SECONDS", 300)
_STALE_AFTER_SECONDS = _parse_positive_int("RSA_REQUESTED_STALE_AFTER_SECONDS", 900)

if _ARRIVED_AFTER_SECONDS <= _EN_ROUTE_AFTER_SECONDS:
    raise ValueError(
        f"RSA_SIM_ARRIVED_AFTER_SECONDS ({_ARRIVED_AFTER_SECONDS}) must be greater than "
        f"RSA_SIM_EN_ROUTE_AFTER_SECONDS ({_EN_ROUTE_AFTER_SECONDS}) — "
        "a truck that arrives before it is en-route is nonsensical; fail-closed."
    )

# ---------------------------------------------------------------------------
# Lazy DynamoDB resource
# ---------------------------------------------------------------------------

_ddb = None


def _get_ddb():
    global _ddb
    if _ddb is None:
        _ddb = boto3.resource("dynamodb")
    return _ddb


# ---------------------------------------------------------------------------
# Terminal-state guard (spec R7) — built from sorted constants for
# deterministic ConditionExpression ordering across processes.
# ---------------------------------------------------------------------------

_SORTED_TERMINALS = sorted(LIFECYCLE_TERMINAL_STATES)


def _not_terminal_condition() -> Attr:
    """Return a boto3 Attr condition: status is not any terminal state."""
    conditions = [Attr("status").ne(t) for t in _SORTED_TERMINALS]
    result = conditions[0]
    for cond in conditions[1:]:
        result = result & cond
    return result


# ---------------------------------------------------------------------------
# Timestamp helpers (G3-D3 / F10.13: delegate to shared single-copy helper)
# ---------------------------------------------------------------------------

def _iso_now() -> str:
    """Return the current UTC time as ISO-8601 with milliseconds and Z suffix.

    Delegates to shared.rsa_client.iso_now — the single-copy G3-D3 helper
    (F10.13: lifecycle, consumer, and test helpers must all use the same
    implementation to prevent format drift).

    Reads the clock exactly once so the seconds and milliseconds are
    guaranteed to come from the same instant.
    """
    return _iso_now_shared()


def _cutoff_iso(seconds_ago: int) -> str:
    """Return an ISO-8601 UTC timestamp for `seconds_ago` seconds in the past."""
    cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=seconds_ago)
    return cutoff.strftime("%Y-%m-%dT%H:%M:%S.") + f"{cutoff.microsecond // 1000:03d}Z"


# ---------------------------------------------------------------------------
# GSI query — paginated (G3-D2: no Scan, follow LastEvaluatedKey)
# ---------------------------------------------------------------------------

def _query_by_status_before(
    table,
    status: str,
    cutoff_iso: str,
) -> list[dict[str, str]]:
    """Query the ``status-createdAt-index`` GSI for rows with status == status
    and createdAt <= cutoff_iso.

    Returns a list of {dispatchId, createdAt} dicts (KEYS_ONLY projection
    gives dispatchId and the GSI keys status + createdAt).

    Paginates to exhaustion on LastEvaluatedKey — G3-D2: no Scan.
    """
    rows: list[dict[str, str]] = []
    kwargs: dict[str, Any] = {
        "IndexName": "status-createdAt-index",
        "KeyConditionExpression": (
            "   #st = :status"
            " AND createdAt <= :cutoff"
        ),
        "ExpressionAttributeNames": {"#st": "status"},
        "ExpressionAttributeValues": {
            ":status": status,
            ":cutoff": cutoff_iso,
        },
    }

    while True:
        resp = table.query(**kwargs)
        rows.extend(resp.get("Items", []))
        lek = resp.get("LastEvaluatedKey")
        if not lek:
            break
        kwargs["ExclusiveStartKey"] = lek

    return rows


# ---------------------------------------------------------------------------
# UpdateItem helpers — one per transition
# ---------------------------------------------------------------------------

def _advance_to_en_route(table, dispatch_id: str, now_iso: str) -> bool:
    """Advance DISPATCHED → EN_ROUTE.

    ConditionExpression:
      - status == DISPATCHED (exact precursor)
      - simulated == true (G3-D4)
      - status not in terminal states (R7 belt-and-suspenders)

    Returns True on success, False on ConditionalCheckFailed (already transitioned).
    Raises ClientError for any other DynamoDB error.
    """
    condition = (
        Attr("status").eq(LIFECYCLE_DISPATCHED)
        & Attr("simulated").eq(True)
        & _not_terminal_condition()
    )
    try:
        table.update_item(
            Key={"dispatchId": dispatch_id},
            UpdateExpression=(
                "SET #st = :en_route, enRouteAt = :ts, updatedAt = :ts"
            ),
            ConditionExpression=condition,
            ExpressionAttributeNames={"#st": "status"},
            ExpressionAttributeValues={
                ":en_route": LIFECYCLE_EN_ROUTE,
                ":ts": now_iso,
            },
        )
        log.info("dispatchId=%s advanced to EN_ROUTE", dispatch_id)
        return True
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
            log.info(
                "dispatchId=%s ConditionalCheck failed on EN_ROUTE advance "
                "(already transitioned, non-simulated, or terminal); skipping",
                dispatch_id,
            )
            return False
        raise


def _advance_to_arrived(table, dispatch_id: str, now_iso: str) -> bool:
    """Advance EN_ROUTE → ARRIVED.

    ConditionExpression:
      - status == EN_ROUTE (exact precursor)
      - simulated == true (G3-D4)
      - status not in terminal states (R7)

    Returns True on success, False on ConditionalCheckFailed.
    """
    condition = (
        Attr("status").eq(LIFECYCLE_EN_ROUTE)
        & Attr("simulated").eq(True)
        & _not_terminal_condition()
    )
    try:
        table.update_item(
            Key={"dispatchId": dispatch_id},
            UpdateExpression=(
                "SET #st = :arrived, arrivedAt = :ts, updatedAt = :ts"
            ),
            ConditionExpression=condition,
            ExpressionAttributeNames={"#st": "status"},
            ExpressionAttributeValues={
                ":arrived": LIFECYCLE_ARRIVED,
                ":ts": now_iso,
            },
        )
        log.info("dispatchId=%s advanced to ARRIVED", dispatch_id)
        return True
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
            log.info(
                "dispatchId=%s ConditionalCheck failed on ARRIVED advance "
                "(already transitioned, non-simulated, or terminal); skipping",
                dispatch_id,
            )
            return False
        raise


def _reconcile_stale_requested(table, dispatch_id: str, now_iso: str) -> bool:
    """Reconcile a stale REQUESTED row to FAILED (DLQ reconciliation, Accept 5).

    Called for rows that exceeded the stale threshold and are still in REQUESTED —
    their stream records must have exhausted retries and landed in the DLQ without
    the consumer moving the row.

    ConditionExpression:
      - status == REQUESTED (exact precursor)
      - status not in terminal states (R7)

    Note: simulated is NOT a condition here. A stranded REQUESTED row from a real
    dispatch is equally stuck — reconciliation must not skip it.

    Note: a row reconciled while the consumer is still retrying produces a false
    FAILED. The stale threshold (900 s) is well above the consumer's max retry
    window (3 × 30 s + bisection time ≈ a few minutes). A row still in REQUESTED
    at 900 s is definitively stuck.

    Returns True on success, False on ConditionalCheckFailed (already transitioned).
    Emits the RECONCILIATION_LOG_TOKEN for CloudWatch Logs metric filter.
    """
    condition = (
        Attr("status").eq(LIFECYCLE_REQUESTED)
        & _not_terminal_condition()
    )
    reason = (
        "Stale REQUESTED row — no consumer transition within "
        f">{_STALE_AFTER_SECONDS}s of dispatch creation. "
        "Either stream consumer retries are exhausted (row landed in DLQ) "
        "or the consumer did not run during that window. "
        "Reconciled by lifecycle advancer."
    )
    try:
        table.update_item(
            Key={"dispatchId": dispatch_id},
            UpdateExpression=(
                "SET #st = :failed, failureReason = :reason, "
                "failedAt = :ts, updatedAt = :ts"
            ),
            ConditionExpression=condition,
            ExpressionAttributeNames={"#st": "status"},
            ExpressionAttributeValues={
                ":failed": LIFECYCLE_FAILED,
                ":reason": reason,
                ":ts": now_iso,
            },
        )
        log.info(
            "%s dispatchId=%s reconciled from REQUESTED to FAILED",
            RECONCILIATION_LOG_TOKEN,
            dispatch_id,
        )
        return True
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
            log.info(
                "dispatchId=%s ConditionalCheck failed on reconciliation "
                "(row transitioned between query and update — healthy, not stuck); skipping",
                dispatch_id,
            )
            return False
        raise


# ---------------------------------------------------------------------------
# Lambda entry point
# ---------------------------------------------------------------------------


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Scheduled lifecycle advancer — invoked every minute by EventBridge Scheduler.

    1. Advance EN_ROUTE → ARRIVED (threshold: arrivedAfterSeconds from createdAt).
    2. Advance DISPATCHED → EN_ROUTE (threshold: enRouteAfterSeconds from createdAt).
    3. Reconcile stale REQUESTED → FAILED (threshold: staleAfterSeconds from createdAt).

    All three loops are always executed, even if one raises on an individual row.
    Partial failures are logged and counted; the whole-invocation summary is
    returned so the caller (EventBridge Scheduler) can judge health.
    """
    table = _get_ddb().Table(_RSA_DISPATCH_TABLE)
    now_iso = _iso_now()

    arrived_cutoff = _cutoff_iso(_ARRIVED_AFTER_SECONDS)
    en_route_cutoff = _cutoff_iso(_EN_ROUTE_AFTER_SECONDS)
    stale_cutoff = _cutoff_iso(_STALE_AFTER_SECONDS)

    stats = {
        "arrivals": 0,
        "en_routes": 0,
        "reconciliations": 0,
        "errors": 0,
    }

    # ── Step 1: EN_ROUTE → ARRIVED ────────────────────────────────────────
    try:
        en_route_rows = _query_by_status_before(table, LIFECYCLE_EN_ROUTE, arrived_cutoff)
    except (ClientError, BotoCoreError) as exc:
        log.error("Failed to query EN_ROUTE rows: %s", exc)
        en_route_rows = []
        stats["errors"] += 1

    for row in en_route_rows:
        dispatch_id = row["dispatchId"]
        try:
            if _advance_to_arrived(table, dispatch_id, now_iso):
                stats["arrivals"] += 1
        except (ClientError, BotoCoreError) as exc:
            log.error(
                "Error advancing dispatchId=%s to ARRIVED: %s",
                dispatch_id,
                exc,
            )
            stats["errors"] += 1

    # ── Step 2: DISPATCHED → EN_ROUTE ────────────────────────────────────
    try:
        dispatched_rows = _query_by_status_before(table, LIFECYCLE_DISPATCHED, en_route_cutoff)
    except (ClientError, BotoCoreError) as exc:
        log.error("Failed to query DISPATCHED rows: %s", exc)
        dispatched_rows = []
        stats["errors"] += 1

    for row in dispatched_rows:
        dispatch_id = row["dispatchId"]
        try:
            if _advance_to_en_route(table, dispatch_id, now_iso):
                stats["en_routes"] += 1
        except (ClientError, BotoCoreError) as exc:
            log.error(
                "Error advancing dispatchId=%s to EN_ROUTE: %s",
                dispatch_id,
                exc,
            )
            stats["errors"] += 1

    # ── Step 3: Stale REQUESTED → FAILED (DLQ reconciliation) ────────────
    try:
        requested_rows = _query_by_status_before(table, LIFECYCLE_REQUESTED, stale_cutoff)
    except (ClientError, BotoCoreError) as exc:
        log.error("Failed to query stale REQUESTED rows: %s", exc)
        requested_rows = []
        stats["errors"] += 1

    for row in requested_rows:
        dispatch_id = row["dispatchId"]
        try:
            if _reconcile_stale_requested(table, dispatch_id, now_iso):
                stats["reconciliations"] += 1
        except (ClientError, BotoCoreError) as exc:
            log.error(
                "Error reconciling dispatchId=%s: %s",
                dispatch_id,
                exc,
            )
            stats["errors"] += 1

    log.info(
        "lifecycle run complete: arrivals=%d en_routes=%d reconciliations=%d errors=%d",
        stats["arrivals"],
        stats["en_routes"],
        stats["reconciliations"],
        stats["errors"],
    )

    # Raise when any step failed so Lambda's Errors metric fires and the
    # advancer-error alarm can activate. A query-level AccessDeniedException
    # or KMS error increments errors and then raises here.
    # (F9.1: returning a stats dict on error means Lambda reports success
    # every minute even when the advancer cannot read the table at all.)
    #
    # EventBridge Scheduler invokes Lambda asynchronously; function-error
    # retries come from Lambda's async invoke config (default 2), not from
    # the Scheduler target `retryAttempts` which only covers delivery.
    # Because the next scheduled tick is effectively the retry, retryAttempts
    # is set to 0 in vsa-rsa-stack.ts to prevent overlapping re-invocations.
    # (Every write is conditional, so overlapping retries are harmless but
    # wasteful and confusing in metrics — better to let the schedule drive
    # retries at the natural 1-minute rate.)
    if stats["errors"] > 0:
        raise RuntimeError(
            f"lifecycle advancer completed with errors: "
            f"arrivals={stats['arrivals']} en_routes={stats['en_routes']} "
            f"reconciliations={stats['reconciliations']} errors={stats['errors']}"
        )

    # Return the stats dict for EventBridge Scheduler / test assertion
    # (only reached when errors == 0).
    return stats
