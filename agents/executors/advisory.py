"""AdvisoryExecutor — the acknowledge-only / no-downstream path.

Spec: `.kiro/specs/2026-09-18-avx-tier2-ios-cards-and-actions/spec.md`
      § "Executor dispatch abstraction", target_system = 'none'

Advisory actions have no external downstream: the act of recording them *is*
the act of completing them.  There is no dealer system to call, no HTTP client
to construct, no SNS topic to publish to.  The Action row is written by the
approve route before dispatch is called, so when dispatch returns the audit
trail already exists — that is the whole durable record.

Status choice — `ActionStatus.COMPLETED` rather than `ActionStatus.EXECUTED`:
    `EXECUTED` means "I handed the work to a downstream and it acknowledged
    receipt; I am waiting for a later webhook or poll to confirm the outcome."
    There is no downstream here: the work is fully done the moment it is
    recorded.  `COMPLETED` is the correct terminal success status — it signals
    that no further transition is expected and no polling is needed.  Using
    `EXECUTED` would leave the approve route believing a webhook must arrive,
    which can never happen and would leave the owner's card permanently in a
    non-terminal state.

Idempotency:
    Because this executor touches no external system and produces no side
    effects, dispatching the same Action twice returns the same
    `ExecuteResult` and produces no duplicate entry anywhere outside the
    Action row itself (which the approve route is responsible for).

HARD CONSTRAINTS enforced by this module:
    - No `requests` import.
    - No `boto3` client or resource construction of any kind.
    - No network call of any kind.
    - No threading or concurrency primitives beyond what base.py already uses.

These constraints are verified by `tests/test_advisory.py` via AST scanning,
not merely asserted in prose.
"""

from __future__ import annotations

import logging
from typing import Any, Mapping

from agents.executors.base import (
    ActionStatus,
    ExecuteResult,
    never_raises,
)

log = logging.getLogger(__name__)


class AdvisoryExecutor:
    """Executor for advisory / acknowledge-only actions (target_system='none').

    Records the action for audit and returns COMPLETED immediately.  No
    external system is contacted; no client of any kind is constructed.
    """

    #: Identifies this executor in the registry. Matches Action.target_system.
    target_system: str = "none"

    @never_raises
    def dispatch(
        self,
        action: Mapping[str, Any],
        claims: Mapping[str, Any],
    ) -> ExecuteResult:
        """Record the advisory action and return COMPLETED.

        Parameters
        ----------
        action:
            The approved Action document.  The executor reads only
            `action.get('id')` for logging; it never mutates the document.
        claims:
            The approver's JWT claims.  Unused by this executor; accepted
            to satisfy the Protocol so callers never need to branch on
            executor type.

        Returns
        -------
        ExecuteResult
            `status=COMPLETED`, `target_ref=None` — there is no downstream
            reference to record because no downstream was contacted.
        """
        # Schema key is action_id; "id" is a legacy alias kept for log continuity.
        action_id = action.get("action_id") or action.get("id", "<unknown>")
        log.info(
            "advisory executor: recording action id=%s as completed (no downstream)",
            action_id,
        )

        # `target_ref=None` is correct and intentional: there is no downstream
        # ticket, booking reference, or correlation ID to surface to the owner.
        return ExecuteResult.success(
            target_ref=None,
            status=ActionStatus.COMPLETED,
            note="Advisory action recorded; no downstream contacted.",
        )
