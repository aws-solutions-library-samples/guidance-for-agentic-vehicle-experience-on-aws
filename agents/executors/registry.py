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

import logging
from typing import Any, Mapping, Optional

from agents.executors.advisory import AdvisoryExecutor
from agents.executors.base import Executor
from agents.executors.chat import ChatDrillDownExecutor
from agents.executors.dms import DmsExecutor
from agents.executors.ios_owner import IosOwnerExecutor
from agents.executors.retail import RetailExecutor
from agents.executors.vehicle_command import VehicleCommandExecutor

log = logging.getLogger(__name__)

#: The `Action.target_system` enum, verbatim from spec § "Cross-surface Action
#: data model". Pinned here so a registry that drifts from the schema fails at
#: import rather than at dispatch.
TARGET_SYSTEMS = (
    "dms",
    "vehicle-command",
    "retail",
    "none",
    "chat",
    "ios-owner",
)

EXECUTORS: dict[str, Executor] = {
    "dms": DmsExecutor(),
    "vehicle-command": VehicleCommandExecutor(),
    "retail": RetailExecutor(),
    "none": AdvisoryExecutor(),
    "chat": ChatDrillDownExecutor(),
    "ios-owner": IosOwnerExecutor(),
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


def dispatch(
    action: Mapping[str, Any],
    claims: Mapping[str, Any],
    *,
    fire_escalation: bool = True,
    escalation_clients: Optional[Mapping[str, Any]] = None,
) -> Any:
    """Dispatch `action` to its target executor, and escalate if it failed.

    **This is the single enforcement point for the Addendum D5 escalation, and it
    is why callers must come through here rather than reaching into `EXECUTORS`.**

    Escalation started life inside each executor, which left it structurally
    unreliable: `chat` and `ios_owner` never called it at all, and neither did
    `@never_raises` — the catch-all in `base.py` that synthesises
    `escalate=True` for *every* executor when an exception escapes. So the paths
    most likely to fail unexpectedly were exactly the paths that escalated to
    nobody, and the `AVXActionTerminalFailure` metric is dimensioned by
    `target_system` while only some targets could ever emit it.

    Firing once here fixes all of those at the same point, and makes the property
    checkable in one place instead of six: any result carrying `escalate=True`
    escalates, whichever executor produced it and however it was produced.

    `fire_escalation=False` exists for the executors' own unit tests, which
    assert dispatch behaviour in isolation; production callers leave it on.
    """
    target = action.get("target_system")
    if not isinstance(target, str) or not target:
        raise UnknownTargetSystemError(
            f"action {action.get('action_id')!r} has no usable target_system "
            f"(got {target!r}); cannot route."
        )

    executor = get_executor(target)
    result = executor.dispatch(action, claims)

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
