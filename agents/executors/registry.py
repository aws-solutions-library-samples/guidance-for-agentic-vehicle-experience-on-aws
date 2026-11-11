"""Executor registry — the single dispatch table for approved Actions.

Spec: `.kiro/specs/2026-09-18-avx-tier2-ios-cards-and-actions/spec.md`
      § "Executor dispatch abstraction"

`EXECUTORS` maps an `Action.target_system` to the executor that dispatches it.
The mapping is validated at import time: a key that disagrees with its
executor's own `target_system` would silently route one target's Actions to
another target's API — a DMS repair order submitted to the retail leads
endpoint — so the disagreement is a hard failure here rather than a support
ticket later.

`get_executor()` **fails closed** on an unrecognised target. The tempting
alternative — falling back to `AdvisoryExecutor` because it is the harmless
no-op one — is the bug it looks like a safety measure: `AdvisoryExecutor`
returns a *success* status, so an Action aimed at a target we cannot dispatch to
would be recorded as completed and the owner would be told their request went
through when nothing was ever sent.
"""

from __future__ import annotations

import dataclasses
import logging
import os
import re
from typing import Any, Callable, Mapping, Optional

from agents.executors.adp import AdpExecutor
from agents.executors.advisory import AdvisoryExecutor
from agents.executors.base import ActionStatus, ExecuteResult, Executor, FailureKind
from agents.executors.chat import ChatDrillDownExecutor
from agents.executors.cms import CmsExecutor
from agents.executors.dms import DmsExecutor
from agents.executors.ios_owner import IosOwnerExecutor
from agents.executors.retail import RetailExecutor
from agents.executors.simulated_operator import (
    DECIDER_KIND as _SIM_DECIDER_KIND,
    DEMO_FLEET_IDS as _SIM_DEMO_FLEET_IDS,
    SIMULATED_KINDS as _SIM_SIMULATED_KINDS,
    is_simulated_decider as _is_simulated_decider,
    process_enabled as _sim_process_enabled,
)
from agents.executors.vehicle_command import VehicleCommandExecutor

log = logging.getLogger(__name__)

#: The `Action.target_system` enum, verbatim from spec § "Cross-surface Action
#: data model" and contract § 1.2 (v2.5 adds "cms"; v2.7 adds "adp").
#: Pinned here so a registry that drifts from the schema fails at import rather
#: than at dispatch.
TARGET_SYSTEMS = (
    "adp",
    "dms",
    "vehicle-command",
    "retail",
    "none",
    "chat",
    "ios-owner",
    "cms",
)


def _make_adp_hook() -> Any:
    """Return a callable that lazily imports and wraps ``make_issue_hook``.

    ``agents.fi_quality`` is NOT imported at module scope so that Lambda bundles
    which do not stage it (``avx-auto-approver``, which never dispatches
    engineering Action kinds and therefore never invokes the hook) cold-start
    without a ``No module named 'agents.fi_quality'`` error.

    The inner hook is created once and cached; subsequent calls reuse it so the
    DynamoDB client inside ``make_issue_hook`` is also reused.
    """
    _cached: list[Any] = []

    def _hook(action: Any, response: Any) -> None:
        if not _cached:
            from agents.fi_quality.issue import make_issue_hook  # noqa: PLC0415
            _cached.append(make_issue_hook())
        _cached[0](action, response)

    return _hook


EXECUTORS: dict[str, Executor] = {
    "adp": AdpExecutor(thread_writer_arn="", on_decision_recorded=_make_adp_hook()),
    "dms": DmsExecutor(),
    "vehicle-command": VehicleCommandExecutor(),
    "retail": RetailExecutor(),
    "none": AdvisoryExecutor(),
    "chat": ChatDrillDownExecutor(),
    "ios-owner": IosOwnerExecutor(),
    "cms": CmsExecutor(),
}


class UnknownTargetSystemError(KeyError):
    """Raised for a target with no registered executor. Never swallowed."""


def _validate_registry() -> None:
    """Import-time integrity check on `EXECUTORS`.

    Three ways the table can be wrong, all caught here:

    1.  A missing target — an Action arrives with nowhere to go.
    2.  An extra target not in the schema enum — dead code, or a schema change
        that only landed on one side.
    3.  A key that disagrees with the executor's own `target_system` — the
        dangerous one, because everything still imports and dispatch silently
        goes to the wrong downstream system.
    """
    registered = set(EXECUTORS)
    expected = set(TARGET_SYSTEMS)

    missing = expected - registered
    if missing:
        raise RuntimeError(
            f"EXECUTORS is missing target_system(s): {sorted(missing)}. "
            "Every value in the Action.target_system enum needs an executor, or "
            "an approved Action for that target has nowhere to dispatch."
        )

    extra = registered - expected
    if extra:
        raise RuntimeError(
            f"EXECUTORS registers unknown target_system(s): {sorted(extra)}. "
            f"Permitted values are {sorted(expected)} (spec § Action data model)."
        )

    for key, executor in EXECUTORS.items():
        declared = getattr(executor, "target_system", None)
        if declared != key:
            raise RuntimeError(
                f"EXECUTORS key {key!r} maps to {type(executor).__name__} whose "
                f"target_system is {declared!r}. A key/executor mismatch routes "
                "one target's Actions to another target's API."
            )


_validate_registry()


def get_executor(target_system: str) -> Executor:
    """Resolve the executor for `target_system`, or fail closed.

    Raises `UnknownTargetSystemError` rather than returning a default. See the
    module docstring for why a no-op default would be worse than an error.
    """
    try:
        return EXECUTORS[target_system]
    except KeyError:
        raise UnknownTargetSystemError(
            f"no executor registered for target_system={target_system!r}; "
            f"known targets are {sorted(EXECUTORS)}. Failing closed: dispatching "
            "this Action to a default executor would record a success for work "
            "that was never sent."
        ) from None


def _is_valid_operator(decided_by: Any) -> bool:
    """Return True iff ``decided_by`` represents a valid operator.

    An operator is ``{kind: "operator", id: <non-empty string>}``.  Anything
    else — missing, wrong shape, wrong case, empty id — is NOT an operator
    (spec D5: "fail closed").
    """
    if not isinstance(decided_by, dict):
        return False
    if decided_by.get("kind") != "operator":
        return False
    id_val = decided_by.get("id")
    return isinstance(id_val, str) and bool(id_val)


#: Rule-decider id must be ``<fleet_id>/<rule_id>`` where fleet_id matches
#: ``^[A-Za-z0-9_-]{1,64}$`` (CMS enrolment shape: admin_add_vehicle/handler.py:67
#: on CMS main) and rule_id matches ``^[^/\s]{1,128}$`` with C0 controls,
#: DEL, and \u200b (zero-width space) excluded via the character class —
#: stricter than the auto-approver's ``_RULE_ID_RE`` (avx-auto-approver/handler.py:94),
#: which checks only ``^[^/\s]{1,128}$`` (a rule outside this shape is approved there
#: and then declined here: fail closed; carried follow-on).  ``fullmatch`` on the combined string.
#: ``_is_valid_rule_decider`` also requires ``str.isprintable()``, which declines
#: C1 controls and Unicode format characters (RLO, BOM, soft hyphen, ...) that the
#: class does not list. Tightened from G2.4 by H3.1 and Fix Group 4 (review c3 W1, c4 W1).
_RULE_DECIDER_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}/[^\x00-\x1f\x7f\u200b/\s]{1,128}$")


def _is_valid_rule_decider(decided_by: Any) -> bool:
    """Return True iff ``decided_by`` represents a valid rule decider.

    The id must be ``<fleet_id>/<rule_id>`` where:
    - fleet_id matches ``^[A-Za-z0-9_-]{1,64}$`` (CMS enrolment shape).
    - rule_id matches ``^[^\\x00-\\x1f\\x7f\\u200b/\\s]{1,128}$`` (no slashes,
      no whitespace, no C0 controls, no DEL, no zero-width space).
    - the whole id is ``str.isprintable()`` (no C1 controls, no Unicode format
      characters such as RLO, BOM or soft hyphen).
    Checked with ``fullmatch`` on the whole id (no trailing content accepted).

    This is the shape the auto-approver writes (avx-auto-approver handler.py:410)
    after validating ``rule_id`` against ``^[^/\\s]{1,128}$`` (handler.py:94).
    Anything else — wrong shape, control/format characters, wrong case,
    whitespace, missing/extra slash, empty part, or over-length part — is
    NOT a valid rule decider (spec D5 / stage E G8 U6: fail closed; H3.1).
    """
    if not isinstance(decided_by, dict):
        return False
    if decided_by.get("kind") != "rule":
        return False
    id_val = decided_by.get("id")
    return (
        isinstance(id_val, str)
        and id_val.isprintable()
        and bool(_RULE_DECIDER_ID_RE.fullmatch(id_val))
    )


def _is_valid_engineer(decided_by: Any) -> bool:
    """Return True iff ``decided_by`` represents a valid product engineer.

    An engineer is ``{kind: "engineer", id: <non-empty string>}``.  Anything
    else — missing, wrong shape, wrong case, empty id — is NOT an engineer
    (spec D14: "fail closed"; G5 blocks a missing, malformed or empty decided_by).
    """
    if not isinstance(decided_by, dict):
        return False
    if decided_by.get("kind") != "engineer":
        return False
    id_val = decided_by.get("id")
    return isinstance(id_val, str) and bool(id_val)


#: Guardrails G1–G3: kinds that require an operator (pinned by name, never
#: imported from policy.py — spec D5).
_G1_TO_G3: dict[str, str] = {
    "exempt_recall": "G1",
    "ground_vehicle": "G2",
    "submit_warranty_claim": "G3",
}

#: Guardrail G5: engineering action kinds that require a product engineer (spec D11, D14).
#: Pinned by name, never imported from policy.py — same discipline as G1–G3 (spec D5).
_G5_KINDS: frozenset[str] = frozenset({
    "record_engineering_decision",
    "issue_recall",
    "issue_software_campaign",
})

#: RO statuses that mean "completed work" for G4 (spec D5).
_G4_COMPLETED_STATUSES = frozenset({"Ready", "Delivered", "Invoiced"})


def _guardrail_result(
    target: str,
    guardrail: Optional[str],
    reason: str,
) -> ExecuteResult:
    """Build a guardrail-block ExecuteResult."""
    return ExecuteResult(
        status=ActionStatus.DECLINED,
        failure_kind=FailureKind.TARGET_REFUSED,
        escalate=False,
        guardrail=guardrail,
        failure_reason=reason,
    )


def _check_guardrails(
    *,
    action: Mapping[str, Any],
    kind: Any,
    target: str,
    service_record_resolver: Optional[Callable],
    build_reason: Callable,
) -> Optional[ExecuteResult]:
    """Run guardrails G1–G5 and the central decider re-check. Return a declined
    result if any fires, else None.

    Order matters for G4: operator check first so DMS is never queried for a
    non-operator (contract § 10a row 4 before rows 6-7).

    After G1–G5, the central decider re-check (spec D5 / stage E G4 S1) runs for
    FI executor kinds that carry a financial or fleet risk:
    - apply_cost_action: requires a valid operator decider.
    - move_vehicles: requires a valid operator or well-formed rule decider.
    Neither fires a named guardrail (G6/G7 would be a contract change); both
    return target_refused / failure_reason from the target-refused sentence.
    """
    decided_by = action.get("decided_by")

    # 11.1b — top-level simulated-decider guard (spec D10 Decision 3).
    #
    # Any ``decided_by`` whose ``kind`` looks like the stand-in is declined
    # immediately unless ALL four conditions hold:
    #   1. it is exactly the stand-in's identity — kind AND id (no impersonation).
    #   2. the Action's ``action_kind`` is in SIMULATED_KINDS (allowlist, not
    #      a denylist: anything not on the list is refused).
    #   3. the running process has ``FI_SIM_OPERATOR_ENABLED == "true"``.
    #   4. the Action's ``fleet_id`` is one of the four literal demo fleet ids.
    #
    # This fires before G1–G5 and the central re-check so that spec-1a kinds
    # (acknowledge_only, schedule_inspection, …) and retail kinds that previously
    # fell through all per-kind checks are caught here rather than reaching the
    # executor.  G1/G4/G5 still decline those same kinds for non-simulated
    # deciders; the guard only adds a path for the simulated-decider shape.
    if isinstance(decided_by, dict) and decided_by.get("kind") == _SIM_DECIDER_KIND:
        if not (
            _is_simulated_decider(decided_by)
            and kind in _SIM_SIMULATED_KINDS
            and _sim_process_enabled()
            and action.get("fleet_id") in _SIM_DEMO_FLEET_IDS
        ):
            reason = build_reason(
                target_system=target,
                failure_kind="target_refused",
            )
            return _guardrail_result(target, None, reason)
        # All four conditions met: fall through to the per-kind checks below.

    is_operator = _is_valid_operator(decided_by)

    # G1–G3: operator required
    if kind in _G1_TO_G3:
        guardrail = _G1_TO_G3[kind]
        if is_operator:
            return None  # Operator ok, pass through to executor
        # G2 (ground_vehicle) and G3 (submit_warranty_claim): the simulated fleet
        # operator also passes, but only under two conditions (spec D10 Decision 3):
        #   1. The running process has FI_SIM_OPERATOR_ENABLED == "true".
        #   2. The Action's fleet_id is one of the four literal demo fleet ids.
        # G1 (exempt_recall) never admits the simulated decider.
        if guardrail in ("G2", "G3"):
            if (_is_simulated_decider(decided_by)
                    and _sim_process_enabled()
                    and action.get("fleet_id") in _SIM_DEMO_FLEET_IDS):
                return None  # Simulated decider passes G2/G3 under these two conditions
        reason = build_reason(
            target_system=target,
            failure_kind="target_refused",
            guardrail=guardrail,
        )
        return _guardrail_result(target, guardrail, reason)

    # G4: complete_recall — operator check first, then service record check
    if kind == "complete_recall":
        if not is_operator:
            reason = build_reason(
                target_system=target,
                failure_kind="target_refused",
                guardrail="G4",
                guardrail_case="not_operator",
            )
            return _guardrail_result(target, "G4", reason)

        # Operator confirmed; now check the service record.
        parameters = action.get("parameters") or {}
        ref = parameters.get("service_record_ref")
        vin = parameters.get("vehicle_vin")

        if not ref:
            reason = build_reason(
                target_system=target,
                failure_kind="target_refused",
                guardrail="G4",
                guardrail_case="no_ref",
            )
            return _guardrail_result(target, "G4", reason)

        if service_record_resolver is None:
            reason = build_reason(
                target_system=target,
                failure_kind="target_refused",
                guardrail="G4",
                guardrail_case="lookup_failed",
            )
            return _guardrail_result(target, "G4", reason)

        # Call the resolver; treat any exception as "not checked".
        try:
            ro = service_record_resolver(ref, vin)
        except Exception:  # noqa: BLE001 — never echo the exception
            reason = build_reason(
                target_system=target,
                failure_kind="target_refused",
                guardrail="G4",
                guardrail_case="lookup_failed",
            )
            return _guardrail_result(target, "G4", reason)

        if ro is None:
            reason = build_reason(
                target_system=target,
                failure_kind="target_refused",
                guardrail="G4",
                guardrail_case="not_found",
            )
            return _guardrail_result(target, "G4", reason)

        # review FG2b W1: both VINs must be non-empty strings that are equal.
        # `None != None` is False, so the old single-line check let a None/None
        # pair pass through to the executor unchecked.  An absent VIN on either
        # side is not a match — block with "other_vehicle".
        ro_vin = ro.get("vehicle_vin")
        if not (
            isinstance(vin, str)
            and vin
            and isinstance(ro_vin, str)
            and ro_vin
            and ro_vin == vin
        ):
            reason = build_reason(
                target_system=target,
                failure_kind="target_refused",
                guardrail="G4",
                guardrail_case="other_vehicle",
            )
            return _guardrail_result(target, "G4", reason)

        ro_status = ro.get("status", "")
        # Group 2 security review S2: a non-string status (e.g. a list from a
        # malformed DMS body) is not hashable; it must block, not raise.
        if not isinstance(ro_status, str):
            ro_status = ""
        if ro_status not in _G4_COMPLETED_STATUSES:
            reason = build_reason(
                target_system=target,
                failure_kind="target_refused",
                guardrail="G4",
                guardrail_case="not_completed",
                ro_status=ro_status,
            )
            return _guardrail_result(target, "G4", reason)

        # All G4 checks passed.
        return None

    # G5: engineering action kinds require a product engineer (spec D11, D14).
    # Contract § 10: fail closed — missing, malformed or empty decided_by is not
    # an engineer.  G5 runs after G1–G4 so a kind that hits G2 (ground_vehicle) is
    # caught there first; G5 only fires for the three engineering kinds.
    if kind in _G5_KINDS:
        if not _is_valid_engineer(action.get("decided_by")):
            reason = build_reason(
                target_system=target,
                failure_kind="target_refused",
                guardrail="G5",
            )
            return _guardrail_result(target, "G5", reason)
        return None

    # Simulated fleet operator re-check for DMS booking kinds (spec D10 Decision 3).
    # book_fleet_service and book_recall_service admit the simulated decider only under
    # the same two conditions as G2/G3: process flag set and fleet is a demo fleet.
    # Other deciders (operator, rule, user) pass through unchanged.
    if kind in ("book_fleet_service", "book_recall_service"):
        if _is_simulated_decider(decided_by):
            if (_sim_process_enabled()
                    and action.get("fleet_id") in _SIM_DEMO_FLEET_IDS):
                return None  # Simulated decider passes under these conditions
            # Simulated decider present but conditions not met → decline.
            reason = build_reason(
                target_system=target,
                failure_kind="target_refused",
            )
            return _guardrail_result(target, None, reason)
        # A rule decider may only decide book_fleet_service (spec D7 / G8-AVX-1),
        # not book_recall_service. Requires a valid rule shape AND the per-process
        # flag FI_AUTO_REBOOK_ENABLED == "true"; otherwise decline (no guardrail id).
        # book_recall_service never admits a rule decider (spec D7: "never").
        if (decided_by or {}).get("kind") == "rule":
            if kind != "book_fleet_service" or not (
                _is_valid_rule_decider(decided_by)
                and os.environ.get("FI_AUTO_REBOOK_ENABLED") == "true"
            ):
                reason = build_reason(
                    target_system=target,
                    failure_kind="target_refused",
                )
                return _guardrail_result(target, None, reason)
            return None
        return None  # operator and user deciders pass through to the executor.

    # Central decider re-check for FI executor kinds (spec D5 / stage E G4 S1):
    # No new guardrail id — no G6/G7 — because contract v2.8 is copied into iOS
    # and DMS; a new id would be a contract change.  Not reachable today via the
    # approve route (which enforces operator standing before dispatch) or the
    # auto-approver route (which dispatches only acknowledge_only and
    # rule-approved move_vehicles), but removes the path for any future caller
    # that bypasses both.
    #
    # apply_cost_action: a valid operator is required (spec D9 "no rule can
    #   approve a cost action").
    # move_vehicles: a valid operator OR a well-formed rule decider
    #   ({kind:"rule", id:<fleet_id>/<rule_id>}) passes — the auto-approver's shape
    #   (avx-auto-approver handler.py:410); every other decider is declined.
    if kind == "apply_cost_action":
        if not is_operator:
            reason = build_reason(
                target_system=target,
                failure_kind="target_refused",
            )
            return _guardrail_result(target, None, reason)
        return None

    if kind == "move_vehicles":
        if not (is_operator or _is_valid_rule_decider(decided_by)):
            reason = build_reason(
                target_system=target,
                failure_kind="target_refused",
            )
            return _guardrail_result(target, None, reason)
        return None

    return None


def dispatch(
    action: Mapping[str, Any],
    claims: Mapping[str, Any],
    *,
    fire_escalation: bool = True,
    escalation_clients: Optional[Mapping[str, Any]] = None,
    service_record_resolver: Optional[Callable[[str, str], Optional[Mapping[str, Any]]]] = None,
) -> Any:
    """Dispatch `action` to its target executor, running guardrails first.

    **Guardrails G1–G5 and the central decider re-check run here, before the
    executor, for every Action.**

    G1–G3 refuse a kind that requires an operator unless ``decided_by.kind ==
    "operator"``.  G4 refuses ``complete_recall`` unless
    ``parameters.service_record_ref`` resolves to a completed repair order for
    the Finding's VIN.  G5 refuses engineering action kinds unless
    ``decided_by.kind == "engineer"``.  Guardrail refusals return ``declined`` /
    ``target_refused`` with ``escalate=False`` and carry ``guardrail`` and
    ``failure_reason`` in the audit entry; they never reach the executor.

    The central decider re-check (spec D5 / stage E G4 S1) runs after G1–G5 for
    FI executor kinds (``apply_cost_action``, ``move_vehicles``).  It uses no
    named guardrail id (no G6/G7 — that would be a contract change) and returns
    the target-refused sentence.

    **This is the single enforcement point for the Addendum D5 escalation, and it
    is why callers must come through here rather than reaching into `EXECUTORS`.**

    ``fire_escalation=False`` exists for the executors' own unit tests, which
    assert dispatch behaviour in isolation; production callers leave it on.

    ``service_record_resolver`` is the injected interface for G4: a callable
    ``(ref: str, vin: str) -> dict | None`` that returns the DMS repair order
    dict for ``ref``, or ``None`` when DMS has no such repair order (404).  It
    raises for any other error or timeout.  Pass ``None`` (the default) to mean
    "not configured"; G4 refuses in that case.
    """
    from agents.executors.failure_reason import failure_reason as _build_reason  # noqa: PLC0415
    from agents.executors.target_reason import kept_target_reason as _kept_target_reason  # noqa: PLC0415

    target = action.get("target_system")
    if not isinstance(target, str) or not target:
        raise UnknownTargetSystemError(
            f"action {action.get('action_id')!r} has no usable target_system "
            f"(got {target!r}); cannot route."
        )

    # -----------------------------------------------------------------
    # Guardrails G1–G4 (spec D5, contract § 10)
    # -----------------------------------------------------------------
    kind = action.get("action_kind")
    guardrail_result = _check_guardrails(
        action=action, kind=kind, target=target,
        service_record_resolver=service_record_resolver,
        build_reason=_build_reason,
    )
    if guardrail_result is not None:
        return guardrail_result

    executor = get_executor(target)
    result = executor.dispatch(action, claims)

    # Attach failure_reason to every non-success result from an executor.
    # review FG2b W2 / D14: dispatch() is the one builder — always rebuild,
    # ignoring any failure_reason an executor may have set, so the sentence
    # cannot leak a token, a response body, or free-text from an exception.
    if result.failure_kind is not None:
        reason = _build_reason(
            target_system=target,
            failure_kind=result.failure_kind,
            error_status=result.last_error_status,
            retry_count=result.retry_count,
        )
        # Contract § 10b: the target's own refusal sentence is kept only here,
        # only on an allowed 4xx refusal, and only cleaned; any other text an
        # executor carried (a 5xx body, a terminal error) is dropped.
        kept = _kept_target_reason(
            target_system=target,
            failure_kind=result.failure_kind,
            error_status=result.last_error_status,
            raw=result.target_reason,
        )
        result = dataclasses.replace(result, failure_reason=reason, target_reason=kept)
    elif result.failure_reason is not None or result.target_reason is not None:
        # review FG3b S2: a success result must never carry a failure_reason into
        # the audit trail. failure_kind=None means the executor succeeded, so any
        # failure_reason it pre-set is cleared: one builder, called in one place.
        # The same holds for target_reason (§ 10b).
        result = dataclasses.replace(result, failure_reason=None, target_reason=None)
    # Group 2 security review S1: only the guardrail block above sets `guardrail`.
    # This result came from an executor, so a guardrail it set is dropped;
    # otherwise an executor could make its own failure read as a G1-G4 block.
    if getattr(result, "guardrail", None) is not None:
        result = dataclasses.replace(result, guardrail=None)

    if fire_escalation and getattr(result, "escalate", False):

        # Guarded, because centralising the fire moved it OUTSIDE the executors'
        # `@never_raises` bodies. Inside them, an escape was caught and mapped to
        # `terminal_error`; out here nothing caught it, so an exception in
        # escalation destroyed the `ExecuteResult` — reintroducing, one layer
        # above the wrapper, the exact failure the wrapper exists to prevent:
        # "an Action row written but statusless, with the owner's card stuck on
        # pending_execution forever" (base.py's own words).
        #
        # Reporting a failure must never be able to lose the failure. The result
        # is returned regardless of whether escalation worked.
        try:
            # Imported INSIDE the try, not above it: `escalation` resolves
            # `IosOwnerExecutor` lazily for the owner-visible half, so a top-level
            # import would be a cycle through this module — but review Cycle 5
            # noted that an import left outside the guard means an ImportError
            # destroys the ExecuteResult while the comment below claims it cannot.
            # The guard must cover everything it promises to cover.
            from agents.executors import escalation  # noqa: PLC0415

            escalation.fire(
                action=action,
                claims=claims,
                target_system=target,
                result=result,
                **(dict(escalation_clients) if escalation_clients else {}),
            )
        except Exception as exc:  # noqa: BLE001
            # `exc_info` is deliberately absent (see base.never_raises) but the
            # class name IS recorded: review Cycle 4 noted that the first version
            # of this handler discarded the cause entirely, which made an
            # escalation failure undiagnosable.
            log.error(
                "escalation raised for action %s target=%s (%s); returning the "
                "executor result anyway so the Action row can still be advanced",
                action.get("action_id"),
                target,
                type(exc).__name__,
            )

    return result
