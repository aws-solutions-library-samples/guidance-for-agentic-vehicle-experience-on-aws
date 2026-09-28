"""
AVX DMS event consumer (DMS Spec 2 T5.4).

Subscribes to `dms.ro.status_changed` on the DMS event bus (`dms-{stage}-events`,
same account in staging) and projects RO state transitions onto the AVX Action
row identified by the event's `avx_action_id`.

## Contract

Event source and detail-type MUST match the DMS publisher constants in
`~/guidance-for-dealer-management-system-on-aws/source/_lib/events.py`:

    SOURCE_SERVICE_LANE         = "dms.service-lane"
    DETAIL_TYPE_RO_STATUS_CHANGED = "dms.ro.status_changed"

The event detail is an allowlisted set of stored, deterministic fields (DMS
`RO_STATUS_CHANGED_FIELDS`). This handler reads only `avx_action_id` and
`to_status`; other fields are ignored.

## Projection

The `to_status` from DMS's RO state machine maps to AVX Action `status`:

    Draft      → in_progress
    Ready      → completed
    Delivered  → completed

Any other `to_status` (Scheduled, InProgress, Complete, Cancelled, etc.) is
intentionally NOT projected — AVX Action's terminal states are
`completed` / `cancelled` / `declined` / `dismissed` / `failed` (see AVX core
`_TERMINAL_STATUSES` at `lambdas/api-avx-actions/handler.py:106-113`), and
the RO's intermediate states don't map to any of them without introducing
narration. Unmapped statuses log at INFO and return cleanly.

## Idempotency and terminal-state protection

Every update uses:

    ConditionExpression =
        'attribute_exists(action_id) '
        'AND #s <> :new_status '
        'AND #s <> :completed '
        'AND #s <> :cancelled '
        'AND #s <> :declined '
        'AND #s <> :dismissed '
        'AND #s <> :failed'

Two orthogonal properties enforced in one call:

- `attribute_exists(action_id)` — the Action must already exist. An event
  carrying an `avx_action_id` for an Action that was never written is either
  stale, a test fixture, or a bug. Never create one silently.
- `#s <> :new_status` — a second delivery of the same `(action_id, to_status)`
  is a no-op. This is the (action_id, to_status) idempotency key that the
  spec calls for, enforced at DDB rather than in application code.
- Five terminal-state exclusions — the current status must NOT be one of
  `{completed, cancelled, declined, dismissed, failed}`, matching AVX
  core's `_TERMINAL_STATUSES` frozenset at
  `lambdas/api-avx-actions/handler.py:106-113`. Reason: the client-facing
  PATCH refuses transitions out of terminal states with a 400 (`handler.py:
  594-599`); this event-driven back door must not silently violate the same
  invariant. Concrete case this prevents: owner dismisses an Action
  (`status=dismissed`) between Draft-fired and Ready-fired events; without
  this exclusion, the Ready event would overwrite `dismissed` with
  `completed`, silently erasing owner intent — the exact D4 semantic
  distinction the schema draws deliberately. Filed in review Cycle 1 by the
  reviewer subagent 2026-09-21 as a Warning; fix landed same session.

Any `ConditionalCheckFailedException` is a legitimate no-op and logs at
INFO, not ERROR. Any other error raises so EventBridge retries.

## Non-goals

- **No agent output.** The handler is deterministic — no LLM, no narration.
- **No CVX-side write of new Actions.** Only projects transitions onto
  existing rows. Rows are created by AVX core's approve path.
- **No cross-account trust.** Same-account topology in v1. See
  `.kiro/specs/2026-09-18-dms-avx-agentic-surface/decisions.md § "2026-09-21 —
  T5.4 + T5.5 design"` for the same-account/future-cross-account rationale.
- **No transitions OUT of terminal states.** The ConditionExpression's five
  terminal-state exclusions match AVX core's `_TERMINAL_STATUSES`. See § "Idempotency and terminal-state protection" above.
"""
from __future__ import annotations

import datetime as dt
import logging
import os
from typing import Optional

import boto3
from botocore.exceptions import ClientError

log = logging.getLogger()
log.setLevel(logging.INFO)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

#: DynamoDB table name for AVX Actions. Injected at deploy time from the CDK
#: (`vsa-tier2-stack.ts` — `avxActionsTable.tableName`). No fallback: if the
#: env var is absent the handler refuses to run rather than guess a stage.
_ACTIONS_TABLE_NAME = os.environ.get("AVX_ACTIONS_TABLE")

#: Status projection map. Deliberately narrow — the mapping is the contract
#: with the AVX Action state machine; adding a row here is a contract change.
_STATUS_PROJECTION: dict[str, str] = {
    "Draft":     "in_progress",
    "Ready":     "completed",
    "Delivered": "completed",
}

#: Terminal states of the AVX Action — the consumer refuses to overwrite any
#: of them. Mirrors AVX core's `_TERMINAL_STATUSES` frozenset at
#: `lambdas/api-avx-actions/handler.py:106-113`. Kept in sync deliberately;
#: any divergence between this constant and AVX core's is a defect.
_TERMINAL_STATUSES: frozenset[str] = frozenset({
    "completed",
    "cancelled",
    "declined",
    "dismissed",
    "failed",
})

# ---------------------------------------------------------------------------
# Lazy DDB client — module-level for warm-Lambda reuse; injected in tests
# ---------------------------------------------------------------------------

_ddb_table = None


def _table():
    """Return the DDB Table resource. Cached across warm Lambda invocations."""
    global _ddb_table
    if _ddb_table is None:
        _ddb_table = boto3.resource("dynamodb").Table(_ACTIONS_TABLE_NAME)
    return _ddb_table


def _iso_utc_now() -> str:
    """ISO-8601 UTC timestamp with second precision. Deterministic-ish (call time)."""
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------

def handler(event: dict, context) -> dict:
    """
    EventBridge entrypoint.

    Returns a small dict summarising the outcome for CloudWatch Log Insights
    queries. Not consumed by any caller — EventBridge does not read return
    values. `{"status": "..."}` shapes:

    - `"projected"`  — Action.status was updated.
    - `"no_op_same_status"`  — second delivery / redundant transition.
    - `"no_op_action_missing"` — Action row does not exist yet.
    - `"no_op_terminal_state"` — Action already in a terminal state; refuse
      to overwrite (e.g. owner dismissed the Action between DMS events).
    - `"no_op_unmapped_status"` — DMS to_status has no projection.
    - `"no_op_no_avx_action_id"` — event carried no avx_action_id.

    Any non-idempotency exception raises so EventBridge retries.
    """
    if not _ACTIONS_TABLE_NAME:
        raise RuntimeError(
            "AVX_ACTIONS_TABLE env var is not set; refusing to project against "
            "an unknown table rather than guess a stage."
        )

    detail = event.get("detail") or {}
    avx_action_id: Optional[str] = detail.get("avx_action_id")
    to_status: Optional[str] = detail.get("to_status")
    ro_id: Optional[str] = detail.get("ro_id")  # for log correlation only

    # Non-AVX-originated RO: nothing to project. This is a legitimate frequent
    # case (any RO created outside the DmsExecutor path), not an error.
    if not avx_action_id:
        log.info(
            "no_op_no_avx_action_id ro_id=%s to_status=%s — event carried no avx_action_id",
            ro_id, to_status,
        )
        return {"status": "no_op_no_avx_action_id"}

    projected_status = _STATUS_PROJECTION.get(to_status or "")
    if not projected_status:
        log.info(
            "no_op_unmapped_status action_id=%s ro_id=%s to_status=%s — "
            "no projection for this DMS state",
            avx_action_id, ro_id, to_status,
        )
        return {"status": "no_op_unmapped_status"}

    try:
        _table().update_item(
            Key={"action_id": avx_action_id},
            UpdateExpression="SET #s = :new_status, #u = :now",
            # Idempotency + terminal-state protection. Five terminal
            # exclusions mirror AVX core's `_TERMINAL_STATUSES` (see
            # lambdas/api-avx-actions/handler.py:106-113) — this consumer
            # must refuse to overwrite `dismissed`/`cancelled`/`declined`/
            # `failed`/`completed` for the same reason the client-facing
            # PATCH refuses transitions out of them (400 at handler.py:
            # 594-599). Filed as review Cycle 1 Warning 2026-09-21.
            ConditionExpression=(
                "attribute_exists(action_id) "
                "AND #s <> :new_status "
                "AND #s <> :completed "
                "AND #s <> :cancelled "
                "AND #s <> :declined "
                "AND #s <> :dismissed "
                "AND #s <> :failed"
            ),
            ExpressionAttributeNames={
                "#s": "status",
                "#u": "updated_at",
            },
            ExpressionAttributeValues={
                ":new_status": projected_status,
                ":completed": "completed",
                ":cancelled": "cancelled",
                ":declined":  "declined",
                ":dismissed": "dismissed",
                ":failed":    "failed",
                ":now":       _iso_utc_now(),
            },
        )
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code")
        if code == "ConditionalCheckFailedException":
            # The ConditionExpression's SEVEN clauses each represent a
            # legitimate no-op:
            #   1. `attribute_exists(action_id)` — Action row missing
            #   2. `#s <> :new_status` — same status, duplicate delivery
            #   3-7. `#s <> :{completed,cancelled,declined,dismissed,failed}`
            #      — Action already in a terminal state; refuse to overwrite.
            # We don't distinguish them here because a single GetItem to
            # decide which one fired would defeat the purpose of a
            # conditional-put. If the difference matters operationally, an
            # observer can look at the current Action row.
            log.info(
                "no_op_conditional_check_failed action_id=%s ro_id=%s to_status=%s "
                "projected=%s — one of: Action missing, already at projected "
                "status, or in a terminal state",
                avx_action_id, ro_id, to_status, projected_status,
            )
            return {"status": "no_op_conditional_check_failed"}
        # Any other DDB error is unexpected. Log and re-raise so EventBridge
        # retries per the rule's retryAttempts policy.
        log.exception(
            "ddb_update_item_failed action_id=%s ro_id=%s to_status=%s code=%s",
            avx_action_id, ro_id, to_status, code,
        )
        raise

    log.info(
        "projected action_id=%s ro_id=%s to_status=%s -> action.status=%s",
        avx_action_id, ro_id, to_status, projected_status,
    )
    return {"status": "projected"}
