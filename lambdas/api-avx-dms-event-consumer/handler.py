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

## Correlation id shape

DMS's `avx_action_id` is NOT the AVX Action key. AVX Action rows are keyed
`AVX-ACT-{uuid}` (action.schema.json); DMS accepts and stores only the bare
`{uuid}` (DMS `repair_orders.py` `_AVX_ACTION_ID_RE`), so the DMS executor strips
the prefix before booking (`agents/executors/dms.py` `_dms_correlation_id`).
This handler re-adds it (`_action_key`). Until 2026-10-01 it looked up the bare
UUID, every event matched no row, and every Action booked through DMS stayed
`in_progress` — see issues/2026-10-01-dms-event-consumer-bare-uuid-key/.

An id that is neither `{uuid}` nor `AVX-ACT-{uuid}` is a permanent no-op
(`no_op_malformed_avx_action_id`), not an error: retrying cannot fix it.

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
import re
from typing import Any, Optional

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
    "Invoiced":  "completed",   # Stage C — D12: Invoiced is work-done
}

#: Closed-from-these → completed (work was done; RO closed administratively).
_CLOSED_COMPLETED_FROM: frozenset[str] = frozenset({"Ready", "Delivered", "Invoiced"})

#: Closed-from-these → cancelled (RO closed before work was done — D12).
_CLOSED_CANCELLED_FROM: frozenset[str] = frozenset({"Draft", "InProgress", "AwaitingParts"})

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

#: AVX Action ids are `AVX-ACT-{uuid}` (action.schema.json `action_id` pattern).
#: Same constant as `agents/executors/dms.py` `_ACTION_ID_PREFIX`, which strips it.
_ACTION_ID_PREFIX = "AVX-ACT-"

#: The bare UUID DMS stores and emits (DMS `repair_orders.py` `_AVX_ACTION_ID_RE`).
#: Used with `fullmatch`, not `match` + `$`: `$` also matches before a trailing
#: newline, so `match` would accept `"{uuid}\n"` and build a key no row has.
_UUID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)


def _action_key(avx_action_id: Any) -> Optional[str]:
    """Return the AVX Action key for a DMS `avx_action_id`, or `None`.

    Inverse of `agents/executors/dms.py` `_dms_correlation_id`: DMS emits the
    bare `{uuid}`; the Action row is keyed `AVX-ACT-{uuid}`. An already-prefixed
    id is accepted unchanged, so the mapping is idempotent. The hex is
    lower-cased: action.schema.json admits only lowercase (`[0-9a-f]`), while
    DMS's regex admits A-F and dedupes case-insensitively, so lower case is the
    only form a row can carry. Anything else returns `None`; there is no row it
    could name.
    """
    if not isinstance(avx_action_id, str):
        return None
    bare = (
        avx_action_id[len(_ACTION_ID_PREFIX):]
        if avx_action_id.startswith(_ACTION_ID_PREFIX)
        else avx_action_id
    )
    if not _UUID_RE.fullmatch(bare):
        return None
    return _ACTION_ID_PREFIX + bare.lower()

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
    - `"no_op_malformed_avx_action_id"` — avx_action_id is not `{uuid}` or
      `AVX-ACT-{uuid}`, so it cannot name an Action row.

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
    from_status: Optional[str] = detail.get("from_status")
    ro_id: Optional[str] = detail.get("ro_id")  # for log correlation only

    # Non-AVX-originated RO: nothing to project. This is a legitimate frequent
    # case (any RO created outside the DmsExecutor path), not an error.
    if not avx_action_id:
        log.info(
            "no_op_no_avx_action_id ro_id=%.80r to_status=%.80r — event carried no avx_action_id",
            ro_id, to_status,
        )
        return {"status": "no_op_no_avx_action_id"}

    # DMS emits the bare UUID; the Action row is keyed AVX-ACT-{uuid}.
    action_key = _action_key(avx_action_id)
    if action_key is None:
        # WARNING, not INFO: DMS validates this field, so a malformed value means the
        # DMS contract drifted, which is the class of defect that hid this consumer's
        # bare-id lookup. Bounded reprs (%.80r) on every event-supplied field: they are
        # untrusted input headed for a log line, and repr escapes newlines.
        log.warning(
            "no_op_malformed_avx_action_id avx_action_id=%.80r ro_id=%.80r to_status=%.80r — "
            "not {uuid} or AVX-ACT-{uuid}; no Action row can match",
            avx_action_id, ro_id, to_status,
        )
        return {"status": "no_op_malformed_avx_action_id"}

    projected_status = _STATUS_PROJECTION.get(to_status or "")
    cancel_audit = False  # whether to append a cancel audit entry on this update

    if not projected_status:
        if to_status == "Closed":
            fs = from_status or ""
            if fs in _CLOSED_COMPLETED_FROM:
                projected_status = "completed"
            elif fs in _CLOSED_CANCELLED_FROM:
                projected_status = "cancelled"
                cancel_audit = True
            else:
                log.info(
                    "no_op_unmapped_status action_id=%s ro_id=%.80r "
                    "to_status=Closed from_status=%.80r — "
                    "from_status not in recognised sets; ignoring",
                    action_key, ro_id, from_status,
                )
                return {"status": "no_op_unmapped_status"}
        else:
            log.info(
                "no_op_unmapped_status action_id=%s ro_id=%.80r to_status=%.80r — "
                "no projection for this DMS state",
                action_key, ro_id, to_status,
            )
            return {"status": "no_op_unmapped_status"}

    # Build the UpdateExpression and attribute maps.  Cancel path appends a
    # structured audit entry via list_append (D12: no failure_reason on cancel).
    _condition = (
        "attribute_exists(action_id) "
        "AND #s <> :new_status "
        "AND #s <> :completed "
        "AND #s <> :cancelled "
        "AND #s <> :declined "
        "AND #s <> :dismissed "
        "AND #s <> :failed"
    )
    _ean: dict = {"#s": "status", "#u": "updated_at"}
    _eav: dict = {
        ":new_status": projected_status,
        ":completed":  "completed",
        ":cancelled":  "cancelled",
        ":declined":   "declined",
        ":dismissed":  "dismissed",
        ":failed":     "failed",
        ":now":        _iso_utc_now(),
    }

    if cancel_audit:
        return _apply_cancel(action_key, ro_id, to_status, _condition, _ean, _eav)
    _update_expr = "SET #s = :new_status, #u = :now"

    try:
        _table().update_item(
            Key={"action_id": action_key},
            UpdateExpression=_update_expr,
            # Idempotency + terminal-state protection. Five terminal
            # exclusions mirror AVX core's `_TERMINAL_STATUSES` (see
            # lambdas/api-avx-actions/handler.py:106-113) — this consumer
            # must refuse to overwrite `dismissed`/`cancelled`/`declined`/
            # `failed`/`completed` for the same reason the client-facing
            # PATCH refuses transitions out of them (400 at handler.py:
            # 594-599). Filed as review Cycle 1 Warning 2026-09-21.
            ConditionExpression=_condition,
            ExpressionAttributeNames=_ean,
            ExpressionAttributeValues=_eav,
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
                "no_op_conditional_check_failed action_id=%s ro_id=%.80r to_status=%.80r "
                "projected=%s — one of: Action missing, already at projected "
                "status, or in a terminal state",
                action_key, ro_id, to_status, projected_status,
            )
            return {"status": "no_op_conditional_check_failed"}
        # Any other DDB error is unexpected. Log and re-raise so EventBridge
        # retries per the rule's retryAttempts policy.
        log.exception(
            "ddb_update_item_failed action_id=%s ro_id=%.80r to_status=%.80r code=%s",
            action_key, ro_id, to_status, code,
        )
        raise

    log.info(
        "projected action_id=%s ro_id=%.80r to_status=%.80r -> action.status=%s",
        action_key, ro_id, to_status, projected_status,
    )
    return {"status": "projected"}


#: The non-terminal Action statuses a DMS cancel can leave, in the order an Action moves
#: through them. Trying them in this order means a concurrent forward move between two
#: attempts is met by a later attempt.
_CANCELLABLE_IN_ORDER = ("pending_execution", "executed", "in_progress")


def _apply_cancel(action_key: str, ro_id: Any, to_status: Any, condition: str,
                  ean: dict, eav: dict) -> dict:
    """D12 cancel, audited with the Action's OWN prior status (G3-18; cycle 1 W3, cycle 2 C1).

    ``action.schema.json`` types ``audit_entry.from_status`` as an Action status, so the entry
    names the status the Action left, never DMS's RO status (``Draft`` / ``InProgress`` /
    ``AwaitingParts`` only chose cancel over complete). There is no read: the consumer role
    holds ``dynamodb:UpdateItem`` only. Each non-terminal status is tried as a conditional
    update (``#s = :prior``); exactly one can match, so the entry is exact. A terminal or
    missing Action matches none and the event is a no-op.
    """
    for prior in _CANCELLABLE_IN_ORDER:
        names = {**ean, "#at": "audit_trail"}
        values = {**eav, ":prior": prior, ":empty_list": [], ":audit_entry": [{
            "from_status": prior,
            "to_status":   "cancelled",
            "at":          _iso_utc_now(),
            "by":          "dms-event",
            "note":        "ro_closed_before_work",
        }]}
        try:
            _table().update_item(
                Key={"action_id": action_key},
                UpdateExpression=("SET #s = :new_status, #u = :now, "
                                  "#at = list_append(if_not_exists(#at, :empty_list), :audit_entry)"),
                ConditionExpression=condition + " AND #s = :prior",
                ExpressionAttributeNames=names,
                ExpressionAttributeValues=values,
            )
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                continue
            log.exception("ddb_update_item_failed action_id=%s ro_id=%.80r to_status=%.80r",
                          action_key, ro_id, to_status)
            raise
        log.info("projected action_id=%s ro_id=%.80r to_status=%.80r -> action.status=cancelled "
                 "(from %s)", action_key, ro_id, to_status, prior)
        return {"status": "projected"}
    log.info("no_op_conditional_check_failed action_id=%s ro_id=%.80r — cancel: Action missing "
             "or already terminal", action_key, ro_id)
    return {"status": "no_op_conditional_check_failed"}
