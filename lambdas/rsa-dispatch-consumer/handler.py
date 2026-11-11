"""RSA Dispatch Consumer — DynamoDB stream → provider → status writeback.

Triggered by the vsa-{stage}-rsa-dispatch table stream on INSERT of a
REQUESTED row. Calls the RSA provider and writes DISPATCHED (with
providerRef, etaMinutes, simulated) back to the same row.

Error handling:
  Any provider failure writes FAILED with a reason. The row is NEVER left
  in REQUESTED silently — that is the exact dead-letterbox defect this spec
  exists to close. A consumer that swallows an exception recreates it one
  layer up. (spec § Decision, R7)

Idempotency:
  The handler is keyed on dispatchId (table PK). Processing the same stream
  record twice must not produce two provider calls. A ConsistentRead=True
  get_item check before the provider call skips records whose status has
  already transitioned away from REQUESTED — this *reduces* duplicate provider
  calls but cannot *eliminate* them under concurrent redelivery, because the
  check precedes the provider call while the ConditionExpression on the write
  only guards the writeback. A second redelivery arriving between the check and
  the provider call will call the provider twice. The ConditionExpression
  prevents two DISPATCHED writes for the same row (only the first succeeds),
  but two provider calls may still occur. This is documented rather than
  silently overclaimed — the docstring previously stated "prevents two provider
  calls", which is not what the mechanism does.

Terminal-state enforcement (spec R7):
  Every update carries a ConditionExpression that refuses to overwrite a
  terminal state. The list of terminal-state placeholders is built from
  sorted(LIFECYCLE_TERMINAL_STATES) so the expression is deterministic
  across processes — frozenset iteration order varies per process and a
  literal expression built from an unsorted frozenset fails tests randomly.

No LLM, no Bedrock, no reasoning. This is a state machine. (spec § Tier
classification)

PII NOTE: vin, lat, lon, callback_contact are personally identifiable
information. They are read from the DynamoDB stream record and passed to the
provider client. They must NEVER be logged. The log statements below log
only dispatchId and status transitions.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any

import boto3
from boto3.dynamodb.conditions import Attr
from botocore.exceptions import ClientError

# ---------------------------------------------------------------------------
# Shared library path wiring
# ---------------------------------------------------------------------------
# In the deployed Lambda bundle, shared/rsa_client*.py is copied alongside
# handler.py by scripts/build-rsa-lambda.sh, so Lambda's default code-root
# (/var/task) on sys.path is sufficient for `from shared.rsa_client import`.
# In tests, lambdas/rsa-dispatch-consumer/tests/conftest.py adds the lambdas/
# dir so the same import resolves from the source tree.

from shared.rsa_client import (
    LIFECYCLE_DISPATCHED,
    LIFECYCLE_FAILED,
    LIFECYCLE_REQUESTED,
    LIFECYCLE_TERMINAL_STATES,
    get_client,
    iso_now as _iso_now_shared,
)

# ---------------------------------------------------------------------------
# Timestamp helper (G3-D3 / F10.13: delegate to shared single-copy helper)
# ---------------------------------------------------------------------------


def _iso_now() -> str:
    """Return the current UTC time as ISO-8601 with milliseconds and a Z suffix.

    Delegates to shared.rsa_client.iso_now — the single-copy G3-D3 helper.
    """
    return _iso_now_shared()

log = logging.getLogger()
log.setLevel(logging.INFO)

# ---------------------------------------------------------------------------
# Environment — fail closed: no default so a missing value errors at init
# ---------------------------------------------------------------------------
_RSA_DISPATCH_TABLE = os.environ["RSA_DISPATCH_TABLE"]

# Lazy DynamoDB resource — amortized across warm invocations
_ddb = None


def _get_ddb():
    global _ddb
    if _ddb is None:
        _ddb = boto3.resource("dynamodb")
    return _ddb


# ---------------------------------------------------------------------------
# Terminal-state ConditionExpression builder (spec R7)
#
# Built once from sorted(LIFECYCLE_TERMINAL_STATES) so the placeholder list
# and ExpressionAttributeValues are deterministic across processes.
# frozenset iteration order is not guaranteed — building from unsorted
# frozenset yields different orderings per process, causing flaky tests.
# ---------------------------------------------------------------------------
_SORTED_TERMINALS = sorted(LIFECYCLE_TERMINAL_STATES)
_TERMINAL_PLACEHOLDERS = [f":t{i}" for i in range(len(_SORTED_TERMINALS))]
_TERMINAL_VALUES = {ph: t for ph, t in zip(_TERMINAL_PLACEHOLDERS, _SORTED_TERMINALS)}

# Expression: attribute_not_exists(status)  OR  status = :requested
# combined with: AND status NOT IN (:t0, :t1, ...) via Attr conditions
#
# For the writeback we use a two-part condition:
#   1. The row must currently be in REQUESTED (to reject idempotent replays that
#      already transitioned, and to reject writes onto non-REQUESTED states).
#   2. The status must not be a terminal state (belt-and-suspenders for R7;
#      the REQUESTED check already handles this, but R7 requires explicit terminal
#      enforcement at every write path).
#
# We implement R7 as: status attribute_not_exists OR status = :requested
# which rejects any write if the row has already transitioned.
# The separate terminal guard is the FAILED writeback path, which also carries
# a ConditionExpression blocking overwrites of terminal states.


def _build_not_terminal_condition():
    """Return a boto3 Attr condition that is true iff status is not terminal.

    Built from sorted(LIFECYCLE_TERMINAL_STATES) to ensure deterministic
    placeholder order across processes (frozenset iteration varies per run).
    """
    conditions = [Attr("status").ne(t) for t in _SORTED_TERMINALS]
    result = conditions[0]
    for cond in conditions[1:]:
        result = result & cond
    return result


def _build_requested_condition():
    """Return a condition that is true iff status == REQUESTED (or does not exist).

    Used for the DISPATCHED writeback to enforce idempotency:
    - status must equal REQUESTED (the only valid precursor for DISPATCHED)
    - OR the attribute does not yet exist (shouldn't happen but safe to handle)
    """
    return Attr("status").eq(LIFECYCLE_REQUESTED) | Attr("status").not_exists()


def _build_dispatched_write_condition():
    """Condition for writing DISPATCHED: row must be in REQUESTED and not terminal.

    Combines the REQUESTED precursor check with the R7 terminal guard.
    Returns the combined boto3 Attr condition.
    """
    return _build_requested_condition() & _build_not_terminal_condition()


def _build_failed_write_condition():
    """Condition for writing FAILED: row must not already be in a terminal state.

    Even on error paths we enforce R7 — we must not overwrite ARRIVED or CANCELLED
    with FAILED. If the row is already terminal, we let it be.
    """
    return _build_not_terminal_condition()


# ---------------------------------------------------------------------------
# Core dispatch logic
# ---------------------------------------------------------------------------


def _extract_dispatch_record(new_image: dict[str, Any]) -> dict[str, Any]:
    """Unmarshal a DynamoDB stream NewImage into a plain dict.

    DynamoDB stream images use the typed attribute map format:
    {"dispatchId": {"S": "DISP-…"}, "status": {"S": "REQUESTED"}, …}

    We unmarshal only the fields we need rather than pulling in a full
    deserializer dependency.
    """
    def _untyped(attr: dict[str, Any]) -> Any:
        """Extract the value from a single DynamoDB typed attribute."""
        if "S" in attr:
            return attr["S"]
        if "N" in attr:
            return attr["N"]  # keep as string; callers cast as needed
        if "BOOL" in attr:
            return attr["BOOL"]
        if "NULL" in attr:
            return None
        # For M (map) and L (list) types, return as-is for now
        return attr

    return {k: _untyped(v) for k, v in new_image.items()}


def _process_record(record: dict[str, Any]) -> None:
    """Process a single DynamoDB stream record.

    Only INSERT events for REQUESTED rows trigger a provider call.
    All other events are silently skipped (MODIFY, REMOVE, or non-REQUESTED INSERTs).
    """
    event_name = record.get("eventName", "")
    if event_name != "INSERT":
        log.debug("Skipping non-INSERT event: %s", event_name)
        return

    new_image = record.get("dynamodb", {}).get("NewImage")
    if new_image is None:
        log.warning("INSERT record has no NewImage; skipping")
        return

    row = _extract_dispatch_record(new_image)
    dispatch_id = row.get("dispatchId", "")
    status = row.get("status", "")

    if status != LIFECYCLE_REQUESTED:
        log.info("Skipping non-REQUESTED INSERT for dispatchId=%s (status=%s)", dispatch_id, status)
        return

    # Extract fields needed for the provider call.
    # PII: vin, lat, lon, callback_contact — NOT logged.
    vin = row.get("vin", "")
    service_type = row.get("serviceType", "")
    tenant_id = row.get("tenantId", "")
    callback_contact = row.get("callbackContact", "")

    # F4.2: Fail closed on absent or invalid location.
    # float(row.get("lat", 0)) turns an absent key into valid-looking 0.0 (Gulf of Guinea).
    # Dispatching to (0,0) is a safety and money error — refuse instead.
    if "lat" not in row or "lon" not in row:
        _write_failed(dispatch_id, "Dispatch location (lat/lon) absent from row — location source is an open design question; failing closed rather than dispatching to (0,0)")
        return
    try:
        lat = float(row["lat"])
        lon = float(row["lon"])
    except (TypeError, ValueError) as exc:
        # F8.3: type name only in the reason — the str(exc) from float() includes the
        # raw coordinate value, which would be echoed to CloudWatch via _write_failed.
        _write_failed(dispatch_id, f"Invalid lat/lon in row: {type(exc).__name__}")
        return

    if lat == 0.0 and lon == 0.0:
        _write_failed(dispatch_id, "lat=0.0/lon=0.0 is not a valid dispatch location — failing closed")
        return

    # F6.4: Validate coordinate range. Out-of-range values indicate a bad telemetry
    # reading or seeding bug. A tow truck dispatched to lat=999 is a safety error.
    if not (-90.0 <= lat <= 90.0) or not (-180.0 <= lon <= 180.0):
        # F8.3: do NOT interpolate lat/lon values into the reason — the reason string is
        # echoed verbatim to CloudWatch by _write_failed (log.info level).  The range
        # violation is the diagnostic; the specific values are not needed to act on it.
        _write_failed(
            dispatch_id,
            "Coordinates in row are outside valid range "
            "(lat ∈ [-90, 90], lon ∈ [-180, 180]); failing closed",
        )
        return

    # Idempotency check: before calling the provider, verify the row is still
    # REQUESTED. If the row has already transitioned (e.g., a stream redelivery
    # after a successful dispatch), skip the provider call entirely.
    # NOTE: This *reduces* duplicate provider calls but cannot eliminate them
    # under concurrent redelivery — the check precedes the provider call, so
    # a second thread arriving between this get_item and the provider invocation
    # will still call the provider twice. The ConditionExpression on the
    # subsequent update_item prevents two DISPATCHED writes but not two calls.
    # Accept 4 / F4.5.
    table = _get_ddb().Table(_RSA_DISPATCH_TABLE)
    try:
        response = table.get_item(
            Key={"dispatchId": dispatch_id},
            ProjectionExpression="#st",
            ExpressionAttributeNames={"#st": "status"},
            ConsistentRead=True,
        )
        existing_status = response.get("Item", {}).get("status")
        if existing_status is not None and existing_status != LIFECYCLE_REQUESTED:
            log.info(
                "dispatchId=%s already in status=%s; skipping provider call (idempotent replay)",
                dispatch_id,
                existing_status,
            )
            return
    except ClientError as exc:
        # If we can't read the row, proceed with the provider call anyway and let
        # the subsequent ConditionExpression catch any race.
        log.warning(
            "dispatchId=%s idempotency check failed (%s); proceeding with provider call",
            dispatch_id,
            exc.response["Error"]["Code"],
        )

    # Call the provider (fake by default per VSA_RSA_CLIENT=fake).
    try:
        provider = get_client()
        ack = provider.request_dispatch(
            vin=vin,
            lat=lat,
            lon=lon,
            service_type=service_type,
            tenant_id=tenant_id,
            callback_contact=callback_contact,
            dispatch_id=dispatch_id,
        )
    except Exception as exc:  # noqa: BLE001
        # Provider error — write FAILED so the row is never left in REQUESTED.
        # This is the load-bearing behaviour: Accept 2 / spec § Decision.
        log.error("RSA provider call failed for dispatchId=%s: %s", dispatch_id, type(exc).__name__)
        _write_failed(dispatch_id, f"Provider call failed: {type(exc).__name__}: {exc}")
        return

    # Write DISPATCHED back to the row.
    # ConditionExpression ensures:
    #   - Only writes if status is still REQUESTED (belt-and-suspenders idempotency, R7)
    #   - Never writes onto a terminal state (R7)
    try:
        table.update_item(
            Key={"dispatchId": dispatch_id},
            UpdateExpression=(
                "SET #st = :dispatched, providerRef = :ref, "
                "etaMinutes = :eta, simulated = :sim, "
                "dispatchedAt = :ts, updatedAt = :ts"
            ),
            ConditionExpression=_build_dispatched_write_condition(),
            ExpressionAttributeNames={"#st": "status"},
            ExpressionAttributeValues={
                ":dispatched": LIFECYCLE_DISPATCHED,
                ":ref": ack.provider_ref,
                ":eta": ack.eta_minutes,
                ":sim": ack.simulated,
                ":ts": _iso_now(),
            },
        )
        log.info("dispatchId=%s transitioned to DISPATCHED", dispatch_id)
    except ClientError as exc:
        error_code = exc.response["Error"]["Code"]
        if error_code == "ConditionalCheckFailedException":
            # Row already transitioned (idempotent replay or terminal state).
            # Do NOT write FAILED — the row is in a valid non-REQUESTED state.
            log.info(
                "dispatchId=%s ConditionCheck failed on DISPATCHED write "
                "(already transitioned or terminal); skipping",
                dispatch_id,
            )
        else:
            log.error("DynamoDB UpdateItem failed for dispatchId=%s: %s", dispatch_id, error_code)
            _write_failed(dispatch_id, f"DynamoDB UpdateItem failed: {error_code}")


def _write_failed(dispatch_id: str, reason: str) -> None:
    """Write FAILED with a reason to the dispatch row.

    Carries the terminal-state ConditionExpression (R7) so we never overwrite
    a terminal state (e.g., ARRIVED or CANCELLED) with FAILED.

    If the row is already in a terminal state, the ConditionalCheckFailed is
    caught silently — the row is already done.

    Raises ClientError for any non-ConditionalCheckFailed DynamoDB error so the
    outer handler can report a batchItemFailure and trigger retry. A FAILED
    write that silently succeeds at logging but not at writing leaves the row in
    REQUESTED — the same dead letterbox this spec exists to close.
    """
    table = _get_ddb().Table(_RSA_DISPATCH_TABLE)
    try:
        table.update_item(
            Key={"dispatchId": dispatch_id},
            UpdateExpression="SET #st = :failed, failureReason = :reason, failedAt = :ts, updatedAt = :ts",
            ConditionExpression=_build_failed_write_condition(),
            ExpressionAttributeNames={"#st": "status"},
            ExpressionAttributeValues={
                ":failed": LIFECYCLE_FAILED,
                ":reason": reason,
                ":ts": _iso_now(),
            },
        )
        log.info("dispatchId=%s written to FAILED: %s", dispatch_id, reason)
    except ClientError as exc:
        error_code = exc.response["Error"]["Code"]
        if error_code == "ConditionalCheckFailedException":
            log.info(
                "dispatchId=%s already in terminal state; not overwriting with FAILED",
                dispatch_id,
            )
        else:
            # Re-raise so the outer handler can report a batchItemFailure.
            # Swallowing this error would leave the row in REQUESTED — the dead letterbox.
            log.error(
                "Failed to write FAILED status for dispatchId=%s: %s",
                dispatch_id,
                error_code,
            )
            raise


# ---------------------------------------------------------------------------
# Lambda entry point
# ---------------------------------------------------------------------------


def handler(event: dict[str, Any], context: Any) -> dict[str, Any] | None:  # noqa: ANN401
    """Process a batch of DynamoDB stream records from the RSA dispatch table.

    Each INSERT of a REQUESTED row triggers a provider call and a DISPATCHED
    writeback. Errors write FAILED — the row is never left in REQUESTED.

    Returns batchItemFailures for any record whose processing failed so Lambda
    retries only that record (reportBatchItemFailures: true on the event source).
    Returning None or an empty list means complete batch success.
    """
    records = event.get("Records", [])
    batch_item_failures: list[dict[str, str]] = []

    for record in records:
        try:
            _process_record(record)
        except Exception as exc:  # noqa: BLE001
            # A record that raised an unhandled exception (e.g., DynamoDB unavailable
            # when writing FAILED) should be reported as a batch failure so Lambda
            # retries it. The row may still be in REQUESTED at this point.
            seq = record.get("dynamodb", {}).get("SequenceNumber") or ""
            dispatch_id = ""
            try:
                new_image = record.get("dynamodb", {}).get("NewImage") or {}
                dispatch_id = new_image.get("dispatchId", {}).get("S", "")
            except Exception:  # noqa: BLE001
                pass
            log.error(
                "Unhandled exception processing record (dispatchId=%s seq=%s): %s",
                dispatch_id, seq, exc,
            )
            # F5.5: Even when SequenceNumber is absent, we must NOT return None
            # for the whole batch (None => Lambda treats the batch as fully
            # succeeded, advances the checkpoint, and discards this record
            # permanently — the row stays REQUESTED with no retry).
            # AWS always populates SequenceNumber on DDB stream records, so this
            # branch is unreachable with well-formed input; but if it does fire,
            # raising is safer than silently reporting batch success. We use a
            # sentinel so Lambda knows at least one record failed.
            if seq:
                batch_item_failures.append({"itemIdentifier": seq})
            else:
                # No SequenceNumber: cannot identify the record for targeted retry,
                # but we MUST NOT return None. Raise so Lambda retries the whole
                # batch rather than silently discarding this record.
                raise RuntimeError(
                    f"Stream record has no SequenceNumber (dispatchId={dispatch_id!r}); "
                    "raising to force whole-batch retry rather than reporting success "
                    "and leaving the row in REQUESTED."
                ) from exc

    if batch_item_failures:
        return {"batchItemFailures": batch_item_failures}
    return None
