"""Lambda handler for the AVX investigations API — Stage D task 3.4.

Routes
------
POST /investigations
    Body: {"pattern_id": "<pattern_id>"}
    Group: product-engineer only (spec D10, D16).
    Creates a new investigation row in DynamoDB via a single atomic
    ``transact_write_items`` call (lock Put + row Put + cap Update), sends
    the investigation id to SQS, and returns 202.  If a live investigation
    already exists for the pattern the lock condition fires and the existing
    id is returned 200 — no new row, no SQS message.  Returns 429 when the
    UTC-day cap condition fires.  Returns 503 when the SQS send fails
    (investigation row marked "failed").  Returns 500 when required
    environment variables are absent (no AWS call made).

GET /investigations/{investigationId}
    Group: product-engineer only.
    Returns status, pattern_id, requested_at, finding_id (and error_message
    when status is "failed").  Never returns requested_by.  Returns 400 for
    ids that are not shaped like the ones POST creates (UUID format), so
    ``lock#`` / ``cap#`` special items can never be read through this route.

DynamoDB table: vsa-{stage}-avx-investigations (env AVX_INVESTIGATIONS_TABLE)
  Primary key: investigation_id (string).
  Special items written by this handler:
    lock#<pattern_id>  — one-per-pattern live lock; holds investigation_id_ref
    cap#<YYYY-MM-DD>   — UTC-day counter; holds count (Number)

Optimistic-concurrency design (spec D10, D10-fix)
--------------------------------------------------
The handler first reads the lock item, then (if a reference exists) reads
the investigation row and calls ``is_live_investigation``.  If live the
existing id is returned 200 with no write and no SQS send.

Otherwise a ``transact_write_items`` call atomically:

  Item 0 — lock Put (no status attribute — status lives on the row)
    Condition: attribute_not_exists(investigation_id) when no lock was seen,
               OR investigation_id_ref = :seen_ref when a non-live lock was seen.
    Either fires when another concurrent request won the race; the lock is
    re-read and the winner's id is returned 200 (no SQS send).

  Item 1 — investigation row Put
    Condition: attribute_not_exists(investigation_id)
    Guards against the astronomically unlikely UUID collision.

  Item 2 — UTC-day counter Update
    Condition: absent  OR  count < cap
    Fires when the cap is reached; 429 is returned to the caller (no SQS send).

Environment variables
---------------------
AVX_INVESTIGATIONS_TABLE   — DynamoDB table name (required; 500 if absent)
AVX_INVESTIGATIONS_QUEUE_URL — SQS queue URL   (required; 500 if absent)
AVX_INVESTIGATIONS_DAILY_CAP — daily cap (default 20)

Mutations and their guards
--------------------------
  remove the group check          → test_group_check_removal_test (403 test)
  take requested_by from body     → test_body_requested_by_is_ignored
  drop the lock condition         → test_lock_cancellation_returns_existing_id_no_sqs
  drop the cap condition          → test_cap_cancellation_returns_429_no_sqs
  return requested_by on GET      → test_get_never_returns_requested_by_*
  send more than the id to SQS    → test_sqs_message_body_is_investigation_id_only
  GET returns lock# / cap# item   → test_get_lock_prefix_id_returns_400 /
                                     test_get_cap_prefix_id_returns_400
  500 without env var             → test_missing_table_env_var_returns_500 /
                                     test_missing_queue_env_var_returns_500
"""
from __future__ import annotations

import json
import logging
import os
import sys
import uuid
from datetime import datetime, timezone
from typing import Any

import boto3
from boto3.dynamodb.types import TypeDeserializer, TypeSerializer
from botocore.exceptions import ClientError

# ---------------------------------------------------------------------------
# Path setup — lambdas/_lib is a sibling of this lambda's directory.
# In the Lambda deployment package this is handled by the zip layout.
# In tests the test conftest adds the lambdas/ directory to sys.path.
# ---------------------------------------------------------------------------
_LAMBDA_DIR = os.path.dirname(os.path.abspath(__file__))
_LAMBDAS_DIR = os.path.dirname(_LAMBDA_DIR)
_REPO_ROOT = os.path.dirname(_LAMBDAS_DIR)
for _p in (_LAMBDAS_DIR, _REPO_ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from _lib.avx_scope import _extract_groups  # noqa: E402

# Shared live predicate — same one used by InvestigationStore in the Tier 2 job.
# Shared investigation id regex — same one used by the Tier 2 runner SQS consumer.
from agents.fi_quality.write import INVESTIGATION_ID_RE, is_live_investigation, _STALE_AGE_SECONDS, _STALE_RUNNING_AGE_SECONDS, _STALE_QUEUED_AGE_SECONDS  # noqa: E402  # noqa: F401

# ADP pattern id shape: alphanumeric characters, hyphens — bounded length.
# Matches ids like "pat-001-P0AA6-plant-CGA" or hex hashes.
import re as _re
_PATTERN_ID_RE: _re.Pattern[str] = _re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9\-_\.]{0,254}$"
)

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Environment variables (no defaults — missing vars are a 500 error)
# ---------------------------------------------------------------------------

_TABLE_NAME: str = os.environ.get("AVX_INVESTIGATIONS_TABLE", "")
_QUEUE_URL: str = os.environ.get("AVX_INVESTIGATIONS_QUEUE_URL", "")
_DAILY_CAP: int = int(os.environ.get("AVX_INVESTIGATIONS_DAILY_CAP", "20"))

# ---------------------------------------------------------------------------
# Fixed messages (spec D10, D19)
# ---------------------------------------------------------------------------

_FAILURE_SENTENCE: str = "The investigation could not finish. Start it again."
_CAP_SENTENCE: str = "The daily investigation limit has been reached. Try again tomorrow."

#: Finding expiry in days (mirrors write.py _EXPIRY_DAYS; duplicated so the
#: handler module has no write-time dependency on it).
_EXPIRY_DAYS_CONST: int = 7

# ---------------------------------------------------------------------------
# Item pk prefixes for lock and cap counter items in the same table
# ---------------------------------------------------------------------------

_LOCK_PFX: str = "lock#"   # lock#<pattern_id>   → live investigation ref
_CAP_PFX: str = "cap#"     # cap#<YYYY-MM-DD>     → UTC-day counter

# ---------------------------------------------------------------------------
# Fields never exposed in the GET response (spec D10: "never requested_by")
# ---------------------------------------------------------------------------

_PRIVATE_FIELDS: frozenset[str] = frozenset({"requested_by"})

# ---------------------------------------------------------------------------
# DynamoDB type helpers
# ---------------------------------------------------------------------------

_serializer = TypeSerializer()
_deserializer = TypeDeserializer()


def _serialize_item(python_item: dict) -> dict:
    """Serialize a plain Python dict to DynamoDB attribute-value format."""
    return {k: _serializer.serialize(v) for k, v in python_item.items()}


def _deserialize_attr(ddb_attr: Any) -> Any:
    """Deserialize a single DynamoDB attribute value to a Python type."""
    if isinstance(ddb_attr, dict):
        return _deserializer.deserialize(ddb_attr)
    return ddb_attr


# ---------------------------------------------------------------------------
# Module-level boto3 clients — replaced via patch.object in tests
# ---------------------------------------------------------------------------

_table: Any = None
_ddb_client: Any = None
_sqs_client: Any = None


def _get_investigations_table() -> Any:
    """Return a boto3 DynamoDB Table resource for the investigations table."""
    global _table
    if _table is None:
        _table = boto3.resource("dynamodb").Table(_TABLE_NAME)
    return _table


def _get_ddb_client() -> Any:
    """Return a boto3 low-level DynamoDB client (for transact_write_items)."""
    global _ddb_client
    if _ddb_client is None:
        _ddb_client = boto3.client("dynamodb")
    return _ddb_client


def _get_sqs_client() -> Any:
    """Return a boto3 SQS client."""
    global _sqs_client
    if _sqs_client is None:
        _sqs_client = boto3.client("sqs")
    return _sqs_client


# ---------------------------------------------------------------------------
# Response helper
# ---------------------------------------------------------------------------

def _respond(status_code: int, body: dict) -> dict:
    return {
        "statusCode": status_code,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body),
    }


# ---------------------------------------------------------------------------
# POST /investigations
# ---------------------------------------------------------------------------

def _post_investigation(event: dict, claims: dict) -> dict:
    """Create a new investigation or return the existing live one.

    Optimistic-concurrency design (spec D10, D10-fix):

      1. Read the lock item.  If it references an investigation, read that
         row and call ``is_live_investigation``.  Live → return 200 with the
         existing id (no write, no SQS send).
      2. Otherwise run a ``transact_write_items`` that atomically:
           - Puts the lock item (no status attribute; condition guards the race)
           - Puts the investigation row (UUID collision guard)
           - Updates the UTC-day counter (cap guard)
      3. The lock condition is:
           - ``attribute_not_exists(investigation_id)`` when no lock was seen
             (fires if another request wrote the lock between our read and our
             write).
           - ``investigation_id_ref = :seen_ref`` when a non-live lock was seen
             (fires if another request already advanced the lock to a new
             investigation id).
         Either cancellation means another request won the race; re-read the
         lock and return the winner's id (200, no SQS send).
      4. Index-2 cancellation → daily cap reached → 429.

    The lock item carries ``investigation_id_ref`` only; status lives solely
    on the investigation row (updated by the Tier 2 job).  Keeping status on
    the lock caused the original defect: the lock always showed ``queued``
    because the job never updates it.
    """
    # requested_by comes from the verified token sub — body values are ignored.
    sub: str = claims.get("sub", "")

    raw_body = event.get("body") or "{}"
    try:
        body: dict = json.loads(raw_body)
    except (json.JSONDecodeError, TypeError):
        body = {}

    pattern_id: str = body.get("pattern_id", "") if isinstance(body, dict) else ""
    # W8 fix: validate pattern_id shape — must be a string matching ADP's hex hash
    # format.  A non-string, oversized, or missing pattern_id gets a 400 response.
    if not isinstance(body, dict):
        return _respond(400, {"error": "request body must be a JSON object"})
    raw_pattern_id = body.get("pattern_id")
    if raw_pattern_id is None or not isinstance(raw_pattern_id, str):
        return _respond(400, {"error": "pattern_id must be a string"})
    pattern_id = raw_pattern_id.strip()
    if not pattern_id:
        return _respond(400, {"error": "pattern_id is required"})
    # ADP pattern ids are hex hashes: only alphanumeric and hyphens, bounded length.
    if len(pattern_id) > 256 or not _PATTERN_ID_RE.match(pattern_id):
        return _respond(400, {"error": "pattern_id has an invalid format"})

    lock_key: str = f"{_LOCK_PFX}{pattern_id}"
    table_name = _TABLE_NAME  # read once (module-level may be updated in tests)
    table = _get_investigations_table()
    ddb = _get_ddb_client()

    import time
    import random
    from datetime import timedelta

    # ── Bounded retry loop: up to 3 rounds ───────────────────────────────────
    # Each round begins with a fresh consistent pre-read so any cancellation
    # caused by someone else changing state (prefix condition or lock condition)
    # is resolved by re-observing the current winner, not by reading ALL_OLD
    # items or making extra GetItem calls after the fact.
    #
    # Round outcome:
    #   live referenced row      → 200, no write, no SQS
    #   stale/absent ref         → takeover Update (if stale) + create + cap
    #   success                  → break → SQS send → 202
    #   prefix condition fired   → continue (another session changed state)
    #   lock condition fired     → continue (another session won the lock)
    #   cap condition fired      → 429
    #   TransactionConflict      → sleep + continue
    #   rounds exhausted         → 503
    #
    # Mutation guard (test_n1_interleaving_b_gets_a_id_no_second_send):
    #   Returning seen_ref on prefix cancellation gives the caller a dead-row
    #   id; the test catches it.
    # Mutation guard (test_stale_queued_winner_after_lock_cancellation_marked_failed):
    #   Skipping the takeover Update on the retry round leaves the stale row
    #   live; the test catches it.

    _MAX_ROUNDS = 3

    for _round in range(_MAX_ROUNDS):
        # ── Fresh pre-read each round (consistent) ────────────────────────────
        lock_resp = table.get_item(
            Key={"investigation_id": lock_key},
            ConsistentRead=True,
        )
        lock_data = (lock_resp.get("Item") or {}) if isinstance(lock_resp, dict) else {}
        seen_ref: str | None = (
            lock_data.get("investigation_id_ref") if isinstance(lock_data, dict) else None
        )

        # ── Liveness check if lock references an investigation ────────────────
        # Mutation guard (test_queued_returns_existing_id,
        #                 test_completed_within_window_returns_existing_id):
        #   Deciding liveness from the lock item (no status there) would always
        #   treat the investigation as not-live; those tests catch it.
        if seen_ref:
            inv_resp = table.get_item(
                Key={"investigation_id": str(seen_ref)},
                ConsistentRead=True,
            )
            inv_data = (
                inv_resp.get("Item") or {}
                if isinstance(inv_resp, dict)
                else {}
            )
            inv_status = inv_data.get("status", "") if isinstance(inv_data, dict) else ""
            # Use started_at for running rows (measures the Lambda's actual start
            # time) vs requested_at / queued_at for queued rows.
            if inv_status == "running":
                inv_ts = (
                    inv_data.get("started_at") or inv_data.get("requested_at")
                    if isinstance(inv_data, dict) else None
                )
            else:
                # a2 W3 fix: requeued rows set queued_at; use it so a requeued row
                # doesn't appear stale while its redrive is still in-flight.
                inv_ts = (
                    inv_data.get("queued_at") or inv_data.get("requested_at")
                    if isinstance(inv_data, dict) else None
                )
            if is_live_investigation(inv_status, inv_ts):
                return _respond(200, {"investigation_id": str(seen_ref)})
        else:
            inv_data = {}
            inv_status = ""

        # ── No live investigation — build a new one ────────────────────────────
        investigation_id: str = str(uuid.uuid4())
        requested_at: str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        utc_date: str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        cap_key: str = f"{_CAP_PFX}{utc_date}"

        # ── Lock condition: optimistic-concurrency guard ──────────────────────
        # No lock seen → condition: attribute_not_exists(investigation_id)
        #   Fires if another request wrote the lock between our pre-read and our
        #   transaction.
        # Lock seen (non-live) → condition: investigation_id_ref = :seen_ref
        #   Fires if another request already replaced the lock's ref with a new id.
        #
        # Either cancellation means another request won the race; the next
        # round's fresh pre-read will see the current state and decide correctly.
        if seen_ref is None:
            lock_condition_expr = "attribute_not_exists(investigation_id)"
            lock_extra: dict = {}
        else:
            lock_condition_expr = "investigation_id_ref = :seen_ref"
            lock_extra = {
                "ExpressionAttributeValues": {":seen_ref": {"S": str(seen_ref)}},
            }

        # ── Prefix items ─────────────────────────────────────────────────────
        # When seen_ref is set, item 0 is either a takeover Update (stale
        # queued/running row) or a ConditionCheck (failed/expired-completed).
        transact_items_prefix: list[dict] = []
        if seen_ref:
            stale_cutoff = (
                datetime.now(timezone.utc) - timedelta(seconds=_STALE_AGE_SECONDS)
            ).strftime("%Y-%m-%dT%H:%M:%SZ")
            expiry_cutoff = (
                datetime.now(timezone.utc) - timedelta(days=_EXPIRY_DAYS_CONST)
            ).strftime("%Y-%m-%dT%H:%M:%SZ")
            _pre_is_stale = (
                isinstance(inv_data, dict)
                and inv_status in ("queued", "running")
            )
            if _pre_is_stale:
                # Stale queued/running: atomically mark it failed so the old SQS
                # message cannot _set_running later.
                # Mutation guard (test_stale_queued_winner_after_lock_cancellation_marked_failed,
                #                 test_stale_queued_row_admits_new_id):
                #   Dropping this Update leaves the stale row queued; the test
                #   catches it via stale_row.status != "failed".
                ts_field = "started_at" if inv_status == "running" else "requested_at"
                old_ts = (
                    (inv_data.get("started_at") or inv_data.get("requested_at"))
                    if inv_status == "running"
                    else inv_data.get("requested_at", "")
                ) or ""
                transact_items_prefix = [
                    {
                        "Update": {
                            "TableName": table_name,
                            "Key": _serialize_item({"investigation_id": str(seen_ref)}),
                            "UpdateExpression": "SET #s = :failed, #err = :emsg",
                            "ConditionExpression": (
                                "#s = :old_status AND #ts = :old_ts"
                            ),
                            "ExpressionAttributeNames": {
                                "#s": "status",
                                "#ts": ts_field,
                                "#err": "error_message",
                            },
                            "ExpressionAttributeValues": {
                                ":failed":     {"S": "failed"},
                                ":emsg":       {"S": _FAILURE_SENTENCE},
                                ":old_status": {"S": inv_status},
                                ":old_ts":     {"S": old_ts},
                            },
                            "ReturnValuesOnConditionCheckFailure": "ALL_OLD",
                        }
                    }
                ]
            else:
                # Not stale: verify inside the transaction that the row is still
                # not-live (TOCTOU guard for failed / expired-completed pre-reads).
                transact_items_prefix = [
                    {
                        "ConditionCheck": {
                            "TableName": table_name,
                            "Key": _serialize_item({"investigation_id": str(seen_ref)}),
                            "ConditionExpression": (
                                "attribute_not_exists(investigation_id) OR "
                                "#s = :failed OR "
                                "(#s IN (:queued, :running) AND requested_at < :stale_cutoff) OR "
                                "(#s = :completed AND requested_at < :expiry_cutoff)"
                            ),
                            "ExpressionAttributeNames": {"#s": "status"},
                            "ExpressionAttributeValues": {
                                ":failed":        {"S": "failed"},
                                ":queued":        {"S": "queued"},
                                ":running":       {"S": "running"},
                                ":completed":     {"S": "completed"},
                                ":stale_cutoff":  {"S": stale_cutoff},
                                ":expiry_cutoff": {"S": expiry_cutoff},
                            },
                            "ReturnValuesOnConditionCheckFailure": "ALL_OLD",
                        }
                    }
                ]

        # Cancellation reason indices for this round's transaction.
        # W3 b fix: cap_idx = len(prefix) + 2 always — correct when prefix present.
        prefix_idx = 0 if transact_items_prefix else -1
        lock_idx = len(transact_items_prefix)       # Lock Put right after prefix
        cap_idx = len(transact_items_prefix) + 2    # Cap Update = lock + 2

        try:
            ddb.transact_write_items(
                TransactItems=transact_items_prefix + [
                    # ── Lock Put (lock_idx) ───────────────────────────────────
                    # No 'status' field — status lives on the investigation row.
                    # Condition guards the race between pre-read and this write.
                    #
                    # Mutation guard (test_race_returns_winners_id_no_sqs):
                    #   Removing this condition lets two concurrent requests both
                    #   write new locks; the test catches it.
                    {
                        "Put": {
                            "TableName": table_name,
                            "Item": _serialize_item({
                                "investigation_id": lock_key,
                                "pattern_id": pattern_id,
                                "investigation_id_ref": investigation_id,
                                "requested_at": requested_at,
                            }),
                            "ConditionExpression": lock_condition_expr,
                            **lock_extra,
                            "ReturnValuesOnConditionCheckFailure": "ALL_OLD",
                        }
                    },
                    # ── Investigation row Put (lock_idx + 1) ─────────────────
                    # Condition: id absent (UUID collision guard).
                    # requested_by = token sub, NEVER the body value (spec D10, D16).
                    #
                    # Mutation guard (test_body_requested_by_is_ignored,
                    #                 test_requested_by_from_token_sub):
                    #   Storing body['requested_by'] instead of the token sub causes
                    #   those tests to fail.
                    {
                        "Put": {
                            "TableName": table_name,
                            "Item": _serialize_item({
                                "investigation_id": investigation_id,
                                "pattern_id": pattern_id,
                                "status": "queued",
                                "requested_by": sub,          # token sub — never body
                                "requested_at": requested_at,
                            }),
                            "ConditionExpression": "attribute_not_exists(investigation_id)",
                        }
                    },
                    # ── UTC-day counter Update (cap_idx) ─────────────────────
                    # Condition: absent (first of the day) OR count < cap.
                    # Fires when the daily cap has been reached → 429.
                    #
                    # Mutation guard (test_cap_cancellation_returns_429_no_sqs):
                    #   Removing this condition means the cap is never enforced.
                    {
                        "Update": {
                            "TableName": table_name,
                            "Key": _serialize_item({"investigation_id": cap_key}),
                            "UpdateExpression": "ADD #cnt :one",
                            "ConditionExpression": (
                                "attribute_not_exists(investigation_id) OR #cnt < :cap"
                            ),
                            "ExpressionAttributeNames": {"#cnt": "count"},
                            "ExpressionAttributeValues": {
                                ":one": {"N": "1"},
                                ":cap": {"N": str(_DAILY_CAP)},
                            },
                        }
                    },
                ]
            )
            # Transaction succeeded — break out to send SQS.
            break

        except ClientError as exc:
            code = exc.response["Error"]["Code"]

            if code == "TransactionConflict":
                # Top-level TransactionConflict (botocore edge case); retry.
                time.sleep(random.uniform(0.05, 0.2))
                continue

            if code == "TransactionCanceledException":
                reasons: list = exc.response.get("CancellationReasons", [])

                # ── Prefix condition fired (item 0) ───────────────────────────
                # The referenced row changed between the pre-read and the
                # transaction — another session took it over or a ConditionCheck
                # found the row still live.
                #
                # Do NOT return seen_ref: it may now point to a dead row (N1
                # probe: request B's takeover Update fires because A already
                # marked X failed and updated the lock; returning seen_ref=X
                # here gives the caller the dead id).
                #
                # The next round's fresh pre-read will see the current winner
                # and return it (if live) or take over (if still stale).
                #
                # Mutation guard (test_n1_interleaving_b_gets_a_id_no_second_send):
                #   Returning seen_ref on this branch gives B a dead id; the
                #   test asserts B gets A's id.
                if (
                    prefix_idx >= 0
                    and len(reasons) > prefix_idx
                    and reasons[prefix_idx].get("Code") == "ConditionalCheckFailed"
                ):
                    continue

                # ── Lock condition fired ──────────────────────────────────────
                # Another session won the lock between our pre-read and our
                # transaction.  The next round's fresh pre-read will see the
                # current lock and decide (live winner → 200; stale winner →
                # takeover + create; no lock → create).
                if (
                    len(reasons) > lock_idx
                    and reasons[lock_idx].get("Code") == "ConditionalCheckFailed"
                ):
                    continue

                # ── Cap condition fired ───────────────────────────────────────
                if (
                    len(reasons) > cap_idx
                    and reasons[cap_idx].get("Code") == "ConditionalCheckFailed"
                ):
                    return _respond(429, {"error": _CAP_SENTENCE})

                # ── TransactionConflict as a cancellation reason ──────────────
                if any(
                    r.get("Code") == "TransactionConflict" for r in reasons
                ) and not any(
                    r.get("Code") == "ConditionalCheckFailed" for r in reasons
                ):
                    time.sleep(random.uniform(0.05, 0.2))
                    continue

            raise

    else:
        # for-else: all rounds exhausted without a successful transaction.
        log.warning(
            "POST /investigations: all %d rounds exhausted for pattern_id=%s; returning 503",
            _MAX_ROUNDS, pattern_id,
        )
        return _respond(503, {"error": _FAILURE_SENTENCE})
    # ── Success: send SQS message — investigation id ONLY (spec D10) ─────────
    # The message body is the bare investigation_id string.
    # Mutation guard (test_sqs_message_body_is_investigation_id_only):
    #   Sending a JSON payload or anything extra causes the test to fail.
    sqs = _get_sqs_client()
    try:
        sqs.send_message(
            QueueUrl=_QUEUE_URL,
            MessageBody=investigation_id,   # bare id — nothing else
        )
    except Exception:  # noqa: BLE001
        # SQS failure: mark the investigation row as failed and return 503.
        log.error(
            "SQS send_message failed for investigation_id=%s pattern_id=%s",
            investigation_id, pattern_id,
        )
        try:
            _get_investigations_table().update_item(
                Key={"investigation_id": investigation_id},
                UpdateExpression="SET #s = :failed, #err = :msg",
                ExpressionAttributeNames={"#s": "status", "#err": "error_message"},
                ExpressionAttributeValues={
                    ":failed": "failed",
                    ":msg": _FAILURE_SENTENCE,
                },
            )
        except Exception as mark_exc:  # noqa: BLE001
            # C1 fix: log at ERROR when the failed-mark write itself fails.
            # CS would show "Investigating since…" forever; the alarm on
            # MetricName=InvestigationMarkFailedError lets ops recover.
            log.error(
                "POST /investigations: failed to mark investigation as failed "
                "after SQS error — investigation_id=%s pattern_id=%s error=%s. "
                "Manual recovery required: set status=failed in DynamoDB.",
                investigation_id, pattern_id, type(mark_exc).__name__,
            )
            try:
                import boto3 as _boto3
                _cw = _boto3.client("cloudwatch", region_name=os.environ.get("AWS_REGION") or "us-west-2")
                _cw.put_metric_data(
                    Namespace="AVX/Investigations",
                    MetricData=[{
                        "MetricName": "InvestigationMarkFailedError",
                        "Value": 1.0,
                        "Unit": "Count",
                    }],
                )
            except Exception:  # noqa: BLE001
                pass  # metric emission must not raise
        return _respond(503, {"error": _FAILURE_SENTENCE})

    return _respond(202, {
        "investigation_id": investigation_id,
        "pattern_id": pattern_id,
        "status": "queued",
        "requested_at": requested_at,
    })


# ---------------------------------------------------------------------------
# GET /investigations/{investigationId}
# ---------------------------------------------------------------------------

def _get_investigation(investigation_id: str) -> dict:
    """Return investigation status, pattern_id, requested_at, finding_id.

    Returns 400 for ids that do not match the UUID format produced by POST,
    preventing lock# / cap# special items from being accessed.

    Never exposes requested_by (spec D10: "GET returns status, never
    requested_by").

    Mutation guards:
      test_get_lock_prefix_id_returns_400 / test_get_cap_prefix_id_returns_400:
        Removing the UUID shape check lets special-item keys through.
      test_get_never_returns_requested_by_*:
        Returning the full DynamoDB item leaks requested_by; _PRIVATE_FIELDS
        strips it.
    """
    # ── Id shape guard (fix 3) ────────────────────────────────────────────────
    # Only accept ids that match the UUID format str(uuid.uuid4()) produces.
    # lock# and cap# keys do not match; 400 is returned before any AWS call.
    #
    # Mutation guard (test_get_lock_prefix_id_returns_400,
    #                 test_get_cap_prefix_id_returns_400):
    #   Removing this check lets lock# / cap# ids through; those tests fail.
    if not INVESTIGATION_ID_RE.match(investigation_id):
        return _respond(400, {"error": "invalid investigation id"})

    table = _get_investigations_table()
    resp = table.get_item(Key={"investigation_id": investigation_id})
    item = resp.get("Item") if isinstance(resp, dict) else None

    if not isinstance(item, dict):
        return _respond(404, {"error": "investigation not found"})

    # C1 fix / W6 a2 (design answer 5): a queued/running row past the stale limit
    # is reported as failed with the fixed sentence — "Start it again" becomes
    # available on the CS screen (spec D19).
    status = item.get("status", "") if isinstance(item, dict) else ""
    if status in ("queued", "running"):
        # Use started_at for running (measures Lambda's actual start); else use
        # queued_at (set when requeued, a2 W3 fix) or requested_at for queued.
        if status == "running":
            ts = (
                (item.get("started_at") or item.get("requested_at"))
                if status == "running"
                else item.get("requested_at")
            )
        else:
            # a2 W3 fix: requeued rows set queued_at; measure from it so GET
            # does not report failed while a redrive is still in-flight.
            ts = item.get("queued_at") or item.get("requested_at")
        if not is_live_investigation(status, ts):
            public = {k: v for k, v in item.items() if k not in _PRIVATE_FIELDS}
            public["status"] = "failed"
            public["error_message"] = _FAILURE_SENTENCE
            return _respond(200, public)

    # Strip private fields before returning.
    public = {k: v for k, v in item.items() if k not in _PRIVATE_FIELDS}
    return _respond(200, public)


# ---------------------------------------------------------------------------
# Handler entry point
# ---------------------------------------------------------------------------

def handler(event: dict, context: Any) -> dict:
    """Lambda handler for the AVX investigations API."""
    # ── Env var guard (fix 4) ─────────────────────────────────────────────────
    # Required environment variables must be present.  A missing var means the
    # Lambda is misconfigured; return 500 immediately without touching AWS.
    #
    # Mutation guards (test_missing_table_env_var_returns_500,
    #                  test_missing_queue_env_var_returns_500):
    #   Removing these checks lets a misconfigured Lambda silently use the wrong
    #   table or queue; those tests catch the missing guard.
    if not os.environ.get("AVX_INVESTIGATIONS_TABLE"):
        log.error("AVX_INVESTIGATIONS_TABLE env var is not set")
        return _respond(500, {"error": "server configuration error"})
    if not os.environ.get("AVX_INVESTIGATIONS_QUEUE_URL"):
        log.error("AVX_INVESTIGATIONS_QUEUE_URL env var is not set")
        return _respond(500, {"error": "server configuration error"})

    # Re-read table name and queue URL from environment on each invocation so
    # tests that manipulate os.environ see the updated values.
    global _TABLE_NAME, _QUEUE_URL
    _TABLE_NAME = os.environ["AVX_INVESTIGATIONS_TABLE"]
    _QUEUE_URL = os.environ["AVX_INVESTIGATIONS_QUEUE_URL"]

    claims: dict = (
        event.get("requestContext", {})
        .get("authorizer", {})
        .get("claims", {})
    )
    if not isinstance(claims, dict):
        claims = {}

    # ── Group check: product-engineer only (spec D10, D16) ───────────────────
    # Mutation guard (test_group_check_removal_test, test_fleet_operator_gets_403):
    #   Removing this check lets fleet operators through; those tests catch it.
    groups: set[str] = _extract_groups(claims)
    if "product-engineer" not in groups:
        return _respond(403, {"error": "Forbidden"})

    method: str = event.get("httpMethod", "GET")
    path_params: dict = event.get("pathParameters") or {}
    investigation_id: str | None = path_params.get("investigationId")

    if method == "GET" and investigation_id:
        return _get_investigation(investigation_id)

    if method == "POST":
        return _post_investigation(event, claims)

    return _respond(404, {"error": "Not found"})
