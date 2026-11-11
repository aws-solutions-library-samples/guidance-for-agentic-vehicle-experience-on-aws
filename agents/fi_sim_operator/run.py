"""Fleet run logic for the simulated fleet operator (spec D10e, task 11.3).

Deciding and dispatching: same shape as the auto-approver (``lambdas/avx-auto-
approver/handler.py``).  The Action is built exactly as the approve route would
for an unchanged approval — using the same server-set rule
(``agents.executors.server_set.check_server_set_params``) — then written with a
conditional put on ``action_id`` (one decision per Finding), dispatched through
``registry.dispatch(action, {})``, and the result recorded as the auto-approver
does.

Ordering invariant (spec D10d/e): the grounding Action is dispatched and its
result recorded before the booking Action is written.  The booking Action is
written only after the grounding's result status is one of
``{in_progress, executed, completed}`` — i.e. the grounding was made (11.3b
item 1: ``pending_execution`` is not a made grounding).

Modes (spec D10e):
  ``dry_run``  — reads only; reports would-approve without any write.
  ``observe``  — the handler writes SEEN ledger records; ``run_fleet`` runs no
                 units, reserves no cap and writes no Actions.
  Any other mode is refused (``_execute_unit`` raises; the handler answers
  ``config_error`` before reading anything).
  ``approve``  — full run: reserve cap, write SEEN, write Actions, dispatch.

Mutation guards:
  L1: reserve_cap is called BEFORE writing the grounding Action.
  L2: the booking Action is written only AFTER the grounding result is recorded.
  L3: dry_run never calls put_item/update_item on the actions or ledger tables.
  L4: the conditional put on ``action_id`` (``attribute_not_exists``) ensures a
      person's prior decision is not overwritten.

Fix (e): ``hold_vehicle_ids`` from the config are resolved to VINs via the CMS
registry (``CMS_VEHICLES_TABLE``) at run start and added to the held VINs set.
An unresolvable id stops every fleet for the run.

Fix (f): both Actions in a pair are validated through ``check_server_set_params``
before either is written.  A booking with missing dealer_id or an invalid
server-set field causes the whole pair to be skipped, so no grounding is written
for a booking that would be refused.

Public API
----------
RunConfig     — env-table-name config for one Lambda invocation.
UnitResult    — per-unit outcome.
FleetRunResult — per-fleet counts and skip_counts.
run_fleet(fleet_id, config, mode, ddb, actions_table, findings_table,
          ledger_table, today, run_at, remaining_cap, hold_vins) -> FleetRunResult
"""
from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from agents.executors.server_set import check_server_set_params
from agents.executors.simulated_operator import (
    DECIDER_ID,
    DECIDER_KIND,
    decided_by as _sim_decided_by,
)
from agents.fi_sim_operator.ledger import record_first_seen, reserve_cap
from agents.fi_sim_operator.plan import WorkUnit
from agents.fi_sim_operator.select import SKIP_CAP_REACHED

log = logging.getLogger(__name__)

# ── Modes ─────────────────────────────────────────────────────────────────────
MODE_DRY_RUN = "dry_run"
MODE_OBSERVE = "observe"
MODE_APPROVE = "approve"

#: Action statuses that mean "the grounding was made" (spec D3 seam, D10 Seams).
#: A booking is written only after its grounding reaches one of these.
#: ``pending_execution`` is NOT here: a grounding still pending (an executor that
#: never answered, a recorded status the update lost) is not a grounded car, and a
#: p0 booking must never be approved for a car that isn't grounded (11.3b item 1).
_GROUNDING_MADE_STATUSES = frozenset({
    "in_progress",
    "executed",
    "completed",
})

#: Audit-entry keys from ``action.schema.json`` (verbatim; same as auto-approver).
_AUDIT_ENTRY_KEYS = frozenset({
    "at", "by", "from_status", "to_status", "note",
    "failure_kind", "retry_count",
    "failure_reason", "error_status", "guardrail",
    "target_reason",
})


# ── Result types ──────────────────────────────────────────────────────────────

@dataclass
class UnitResult:
    """Outcome of processing one WorkUnit."""

    finding_id: str
    class_name: str
    mode: str
    #: "approved" | "would_approve" | "skipped" | "person_first"
    outcome: str
    grounding_status: Optional[str] = None
    booking_status: Optional[str] = None
    skip_reason: Optional[str] = None
    error: Optional[str] = None


@dataclass
class FleetRunResult:
    """Per-fleet outcome of one simulated-operator run."""

    fleet_id: str
    mode: str
    units_approved: int = 0
    units_would_approve: int = 0
    units_skipped: int = 0
    units_person_first: int = 0
    units_error: int = 0
    decisions_written: int = 0
    # W3: finer-grained approved-but-incomplete counters
    grounding_not_made: int = 0          # pair: grounding written, booking skipped
    booking_person_first: int = 0        # pair: grounding written, booking person-first
    # W3: per-unit executor outcomes (grounding dispatch result)
    executor_executed: int = 0
    executor_failed: int = 0
    executor_declined: int = 0
    skip_counts: dict[str, int] = field(default_factory=dict)
    unit_results: list[UnitResult] = field(default_factory=list)
    error: Optional[str] = None


# ── Helpers ────────────────────────────────────────────────────────────────────

def _now_iso() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _derive_action_id(finding_id: str) -> str:
    """One decision slot per Finding (same formula as the approve route and auto-approver).

    sha256("{finding_id}|fi-decision")[:32] formatted as AVX-ACT-{uuid-like}.
    """
    raw = hashlib.sha256(f"{finding_id}|fi-decision".encode()).hexdigest()[:32]
    return (
        "AVX-ACT-"
        + raw[0:8] + "-" + raw[8:12] + "-" + raw[12:16]
        + "-" + raw[16:20] + "-" + raw[20:32]
    )


def _build_action(
    finding: dict,
    action_id: str,
    now: str,
) -> Optional[tuple[dict, str]]:
    """Build the Action dict using the server-set rule (approve route shape).

    Returns ``(action, error_str)`` where ``error_str`` is ``None`` on success.
    Applies ``check_server_set_params`` with an empty body (unchanged approval).
    """
    rec = finding.get("recommended_action") or {}
    action_kind = rec.get("action_kind", "")
    target_system = rec.get("target_system", "")

    params, err, _ = check_server_set_params(finding, action_kind, {})
    if err:
        return None, f"server_set error for {action_kind}: {err}"

    db = _sim_decided_by()
    action = {
        "action_id": action_id,
        "finding_id": finding.get("finding_id", ""),
        "action_kind": action_kind,
        "target_system": target_system,
        "parameters": params,
        "evidence": finding.get("evidence") or [],
        "status": "pending_execution",
        "decided_by": db,
        "created_by": DECIDER_ID,
        "decision": "approved",
        "fleet_id": finding.get("fleet_id", ""),
        "audit_trail": [],
        "created_at": now,
        "updated_at": now,
    }
    return action, None


def _persisted_audit_entry(result: Any, *, target_system: str, at: str) -> dict:
    """Same projection as the auto-approver and findings handler."""
    raw = result.audit_entry(by=f"executor-{target_system}", at=at)
    entry = {k: raw[k] for k in _AUDIT_ENTRY_KEYS if k in raw}
    if raw.get("last_error_class") is not None:
        entry["note"] = "; ".join(
            filter(None, [entry.get("note"), f"last_error_class={raw['last_error_class']}"])
        )
    return entry


def _write_action(
    ddb_client: Any,
    actions_table: str,
    action: dict,
) -> str:
    """Conditional put for the Action. Returns "written" or "person_first".

    Mutation guard L4: ``attribute_not_exists(action_id)`` ensures a person's
    prior decision (or a concurrent sim-operator run) is never overwritten.
    """
    from botocore.exceptions import ClientError  # noqa: PLC0415

    try:
        ddb_client.put_item(
            TableName=actions_table,
            Item={k: _serialize_val(v) for k, v in action.items()},
            ConditionExpression="attribute_not_exists(action_id)",
        )
        return "written"
    except ClientError as exc:
        code = (exc.response or {}).get("Error", {}).get("Code", "")
        if code == "ConditionalCheckFailedException":
            return "person_first"
        raise


def _record_dispatch_result(
    ddb_client: Any,
    actions_table: str,
    action_id: str,
    result: Any,
    *,
    target_system: str,
) -> str:
    """Advance the Action with the executor result. Returns the new status.

    ``target_system`` is the Action's own (``cms``/``dms``): the audit entry's
    ``by`` is ``executor-<target_system>``, as the auto-approver writes it
    (11.3b item 9; the first pass passed ``result.status``).

    Same condition as the auto-approver and findings handler:
    ``#s = :pending OR (#s = :in_progress AND attribute_not_exists(#r))``.
    """
    from botocore.exceptions import ClientError  # noqa: PLC0415

    at = _now_iso()
    entry = _persisted_audit_entry(result, target_system=target_system, at=at)
    update = "SET #s = :to, #u = :now, #a = list_append(#a, :entry)"
    values: dict = {
        ":to": {"S": result.status},
        ":now": {"S": at},
        ":entry": {"L": [{"M": {k: _serialize_val(v) for k, v in entry.items()}}]},
        ":pending": {"S": "pending_execution"},
        ":in_progress": {"S": "in_progress"},
    }
    if getattr(result, "target_ref", None):
        update += ", #r = :ref"
        values[":ref"] = {"S": result.target_ref}

    try:
        ddb_client.update_item(
            TableName=actions_table,
            Key={"action_id": {"S": action_id}},
            UpdateExpression=update,
            ConditionExpression="#s = :pending OR (#s = :in_progress AND attribute_not_exists(#r))",
            ExpressionAttributeNames={
                "#s": "status", "#u": "updated_at", "#a": "audit_trail", "#r": "target_ref",
            },
            ExpressionAttributeValues=values,
        )
    except ClientError as exc:
        # S4: re-raise so _execute_unit propagates up to run_fleet's except clause,
        # which counts the unit in units_error and fires the Errors alarm.
        # The grounding or booking Action was already written and dispatched; a lost
        # status-update is an operational error that must not be silently swallowed.
        log.error(
            "sim-operator: could not advance action %s (%s) — status update lost",
            action_id, type(exc).__name__,
        )
        raise

    return result.status


def _serialize_val(v: Any) -> Any:
    """Minimal DynamoDB low-level serialization for Action attributes."""
    from decimal import Decimal  # noqa: PLC0415

    if isinstance(v, bool):
        return {"BOOL": v}
    if isinstance(v, int):
        return {"N": str(v)}
    if isinstance(v, float):
        return {"N": str(Decimal(str(v)))}
    if isinstance(v, Decimal):
        return {"N": str(v)}
    if isinstance(v, str):
        return {"S": v}
    if isinstance(v, list):
        return {"L": [_serialize_val(i) for i in v]}
    if isinstance(v, dict):
        return {"M": {k: _serialize_val(vv) for k, vv in v.items()}}
    if v is None:
        return {"NULL": True}
    return {"S": str(v)}


# ── Unit execution ─────────────────────────────────────────────────────────────

def _execute_unit(
    unit: WorkUnit,
    mode: str,
    ddb_client: Any,
    actions_table: str,
    ledger_table: str,
    fleet_id: str,
    today: str,
    run_at: str,
    daily_cap: int,
) -> UnitResult:
    """Execute one WorkUnit.

    For pair classes (fh_ground_book, recall_ground_book):
      fix (f): validate BOTH Actions through server_set before writing either.
      L1: reserve cap BEFORE writing any Action.
      L2: write and dispatch the grounding; only then write the booking.

    For singleton classes:
      L1: reserve cap BEFORE writing the Action.
    """
    from agents.executors import registry  # noqa: PLC0415

    primary = unit.findings[0]
    fid = primary.get("finding_id", "")
    now = _now_iso()

    is_pair = len(unit.findings) == 2

    # ── fix (f): pre-validate both Actions through server_set ─────────────────
    grounding = unit.findings[0]
    grnd_action_id = _derive_action_id(grounding.get("finding_id", ""))
    grnd_action, grnd_err = _build_action(grounding, grnd_action_id, now)
    if grnd_err:
        return UnitResult(
            finding_id=fid, class_name=unit.class_name, mode=mode,
            outcome="skipped", skip_reason="server_set_error", error=grnd_err,
        )

    if is_pair:
        booking = unit.findings[1]
        book_action_id = _derive_action_id(booking.get("finding_id", ""))
        book_action, book_err = _build_action(booking, book_action_id, now)
        if book_err:
            # fix (f): booking would be refused → skip the whole pair
            return UnitResult(
                finding_id=fid, class_name=unit.class_name, mode=mode,
                outcome="skipped", skip_reason="booking_server_set_error", error=book_err,
            )

    # ── dry_run: report would-approve, no writes ──────────────────────────────
    # Mutation guard L3: dry_run must not call any DDB write operations.
    if mode == MODE_DRY_RUN:
        return UnitResult(
            finding_id=fid, class_name=unit.class_name, mode=mode,
            outcome="would_approve",
        )

    # Fail closed (11.3b items 2 and 3): only ``approve`` writes Actions. ``observe``
    # never reaches here (``run_fleet`` runs no units in observe), and any other
    # value is a caller bug, never a reason to write.
    if mode != MODE_APPROVE:
        raise ValueError(f"sim-operator: unit execution refused for mode {mode!r}")

    # ── Mutation guard L1: reserve cap BEFORE any Action write ────────────────
    reserved = reserve_cap(
        ddb_client, ledger_table, fleet_id, today, unit.decision_count, daily_cap,
    )
    if not reserved:
        return UnitResult(
            finding_id=fid, class_name=unit.class_name, mode=mode,
            outcome="skipped", skip_reason=SKIP_CAP_REACHED,
        )

    # ── approve: write SEEN, then Actions ─────────────────────────────────────
    record_first_seen(ddb_client, ledger_table, fleet_id, fid, run_at)

    # Write and dispatch the grounding (or singleton)
    write_outcome = _write_action(ddb_client, actions_table, grnd_action)
    if write_outcome == "person_first":
        return UnitResult(
            finding_id=fid, class_name=unit.class_name, mode=mode,
            outcome="person_first",
        )

    # Dispatch the grounding
    grnd_result = registry.dispatch(grnd_action, {})
    grnd_status = _record_dispatch_result(
        ddb_client, actions_table, grnd_action_id, grnd_result,
        target_system=grnd_action["target_system"],
    )

    if not is_pair:
        return UnitResult(
            finding_id=fid, class_name=unit.class_name, mode=mode,
            outcome="approved",
            grounding_status=grnd_status,
        )

    # ── Mutation guard L2: write booking ONLY AFTER the grounding was made ────
    if grnd_status not in _GROUNDING_MADE_STATUSES:
        log.warning(
            "sim-operator: grounding %s ended with status %r; booking skipped",
            grnd_action_id, grnd_status,
        )
        return UnitResult(
            finding_id=fid, class_name=unit.class_name, mode=mode,
            outcome="approved",
            grounding_status=grnd_status,
            booking_status=None,
            skip_reason="grounding_not_made",
        )

    # Write and dispatch the booking
    book_write_outcome = _write_action(ddb_client, actions_table, book_action)
    if book_write_outcome == "person_first":
        return UnitResult(
            finding_id=fid, class_name=unit.class_name, mode=mode,
            outcome="approved",
            grounding_status=grnd_status,
            booking_status="person_first",
        )

    book_result = registry.dispatch(book_action, {})
    book_status = _record_dispatch_result(
        ddb_client, actions_table, book_action_id, book_result,
        target_system=book_action["target_system"],
    )

    return UnitResult(
        finding_id=fid, class_name=unit.class_name, mode=mode,
        outcome="approved",
        grounding_status=grnd_status,
        booking_status=book_status,
    )


# ── Fleet run ──────────────────────────────────────────────────────────────────

def run_fleet(
    fleet_id: str,
    plan_units: list[WorkUnit],
    plan_skip_counts: dict[str, int],
    mode: str,
    ddb_client: Any,
    actions_table: str,
    ledger_table: str,
    today: str,
    run_at: str,
    daily_cap: int,
) -> FleetRunResult:
    """Execute the plan for one fleet.

    ``plan_units`` and ``plan_skip_counts`` come from ``build_plan``.

    Returns a :class:`FleetRunResult` with per-unit outcomes and aggregate counts.
    """
    result = FleetRunResult(fleet_id=fleet_id, mode=mode)
    result.skip_counts = dict(plan_skip_counts)

    # 11.3b item 2: observe writes first-seen records only (the handler does that
    # for every candidate); it runs no units and reserves no cap.
    if mode == MODE_OBSERVE:
        return result

    for unit in plan_units:
        # 11.3b item 5: one unit's failure doesn't stop the fleet. A slot it
        # reserved stays spent (never above the cap); the handler raises after
        # the run record when any unit erred, so the Lambda Errors alarm fires.
        try:
            ur = _execute_unit(
                unit, mode, ddb_client, actions_table, ledger_table,
                fleet_id, today, run_at, daily_cap,
            )
        except Exception as exc:  # noqa: BLE001
            primary = unit.findings[0] if unit.findings else {}
            log.error(
                "sim-operator: unit %s (%s) failed: %s",
                primary.get("finding_id", ""), unit.class_name, type(exc).__name__,
            )
            ur = UnitResult(
                finding_id=primary.get("finding_id", ""), class_name=unit.class_name,
                mode=mode, outcome="error", error=type(exc).__name__,
            )
        result.unit_results.append(ur)
        if ur.outcome == "approved":
            result.units_approved += 1
            # W3: count Actions actually written, not unit.decision_count.
            # For a pair where the grounding was not made, only 1 Action was
            # written (the grounding itself); the booking was skipped.
            # For a pair where the booking had a person's prior decision, only
            # 1 Action was written (the grounding).
            if ur.skip_reason == "grounding_not_made":
                result.decisions_written += 1
                result.grounding_not_made += 1
            elif ur.booking_status == "person_first":
                result.decisions_written += 1
                result.booking_person_first += 1
            elif ur.booking_status is not None:
                # Pair: grounding + booking both written by us
                result.decisions_written += 2
            else:
                # Singleton: one Action written
                result.decisions_written += 1
            # W1: per-unit executor outcome (grounding's final dispatch status).
            # in_progress, executed, and completed all mean the grounding was accepted.
            gs = ur.grounding_status
            if gs in ("in_progress", "executed", "completed"):
                result.executor_executed += 1
            elif gs == "declined":
                result.executor_declined += 1
            elif gs in ("failed", "terminal_error"):
                result.executor_failed += 1
            # W1: count booking dispatch status for pairs (person_first is not a
            # dispatch status — it's handled via units_person_first / booking_person_first).
            bs = ur.booking_status
            if bs not in (None, "person_first"):
                if bs in ("in_progress", "executed", "completed"):
                    result.executor_executed += 1
                elif bs == "declined":
                    result.executor_declined += 1
                elif bs in ("failed", "terminal_error"):
                    result.executor_failed += 1
        elif ur.outcome == "would_approve":
            result.units_would_approve += 1
        elif ur.outcome == "person_first":
            result.units_person_first += 1
        elif ur.outcome == "skipped":
            result.units_skipped += 1
            if ur.skip_reason:
                result.skip_counts[ur.skip_reason] = (
                    result.skip_counts.get(ur.skip_reason, 0) + 1
                )
        else:
            result.units_error += 1

    return result
