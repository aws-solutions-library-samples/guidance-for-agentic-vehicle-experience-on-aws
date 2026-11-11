"""IosOwnerExecutor — writes an inbox row to the owner's Action feed.

Spec: `.kiro/specs/2026-09-18-avx-tier2-ios-cards-and-actions/spec.md`
      § "Executor dispatch abstraction", target_system = 'ios-owner'

This executor is the *reverse direction* from the other executors: a
dealer-originated or system-originated item is proposed TO the customer by
writing a row into the AVX Actions table under the owner's scope.

An APNs push is triggered INDIRECTLY: the DynamoDB write is picked up by a
DynamoDB Streams consumer, and the broker decides whether to notify — applying
the owner's consent settings, quiet-hour policy, severity threshold, and
per-channel rate limits.  An executor that called SNS directly would bypass
every one of those controls and could notify an owner who had explicitly
opted out.

LOAD-BEARING CONSTRAINT — verified by test_no_sns_import:
    This executor MUST NOT call sns:Publish.
    This executor MUST NOT import, construct, or reference any SNS client.
    The DynamoDB write is the ONLY outbound side effect.

The DynamoDB client/resource is lazily constructed and injectable via the
constructor so tests can stub it without touching real AWS.

Table resolution:
    Table name = os.environ.get("AVX_ACTIONS_TABLE", f"vsa-{stage}-avx-actions")
    where stage = os.environ.get("VSA_STAGE", "staging").

`target_ref` on a successful result is the `action_id` of the written row,
giving the caller a correlation handle for the created inbox item.
"""

from __future__ import annotations

import logging
import os
import uuid
from typing import Any, Mapping

import boto3

from agents.executors.base import (
    _stage as _base_stage,
    ActionStatus,
    ExecuteResult,
    never_raises,
    utc_now_iso,
)

log = logging.getLogger(__name__)

_TARGET_SYSTEM = "ios-owner"


def _stage() -> str:
    """Delegate to base._stage() so the stage is validated once, in one place.

    This resolves the TABLE NAME — the write target — so an unvalidated stage
    here silently points writes at `vsa-<typo>-avx-actions`. A private copy
    bypassed base.KNOWN_STAGES and its warning, which made it the amplifier on
    the un-stubbed-client hazard rather than an independent nit.
    """
    return _base_stage()


def _actions_table_name() -> str:
    """Resolve the Actions table name from the environment.

    AVX_ACTIONS_TABLE is the explicit override; otherwise constructed from
    VSA_STAGE so the default matches every other table in the stack.
    """
    return os.environ.get("AVX_ACTIONS_TABLE", f"vsa-{_stage()}-avx-actions")


def _make_ddb_resource() -> Any:
    """Lazily construct the DynamoDB resource.  Called only when no stub is provided."""
    return boto3.resource("dynamodb", region_name=os.environ.get("AWS_REGION", "us-west-2"))


class IosOwnerExecutor:
    """Executor for writing an inbox item to the owner's Action feed.

    target_system = 'ios-owner'

    Constructor kwargs allow test injection:

        ddb_resource    boto3 DynamoDB service resource (has .Table())
                        default: lazy boto3.resource("dynamodb")
    """

    target_system: str = _TARGET_SYSTEM

    def __init__(self, *, ddb_resource: Any = None) -> None:
        self._ddb_resource = ddb_resource  # None → lazy init on first dispatch

    @property
    def _ddb(self) -> Any:
        if self._ddb_resource is None:
            self._ddb_resource = _make_ddb_resource()
        return self._ddb_resource

    @never_raises
    def dispatch(
        self,
        action: Mapping[str, Any],
        claims: Mapping[str, Any],
    ) -> ExecuteResult:
        """Write an inbox row into the owner's Action feed.

        The row is written to the table resolved from AVX_ACTIONS_TABLE with
        the owner scope set from the action document.  The write is the ONLY
        side effect; no SNS call is made and no push is triggered directly.

        Parameters
        ----------
        action:
            The approved Action document (or a synthetic finding from the
            DmsExecutor escalation path).  Must contain at minimum:
              - `owner_id`  (str, non-empty) — the target owner's sub / identity.
                An absent or empty value is a terminal error: the executor will
                NOT write a row with an empty owner_id because it would be
                invisible to every owner-scoped query.
            Optional fields consumed:
              - `action_id` — used as the row id for idempotency.  A retry of
                the same action_id overwrites the same DDB item rather than
                creating a duplicate card.  Falls back to `id` for legacy
                callers, then to a fresh UUID (de-duplication is caller's job).
              - `category`, `severity`, `headline`, `evidence` — copied into
                the written row for display by the iOS card renderer.
        claims:
            The caller's JWT claims.  The `sub` claim identifies who triggered
            the write (for the audit trail); it is never used to derive owner
            identity — owner identity comes exclusively from `action['owner_id']`.

        Returns
        -------
        ExecuteResult
            - `status=IN_PROGRESS`, `target_ref=<written action_id>` on success.
            - `status=FAILED`, `failure_kind=executor_terminal_error` on any
              DynamoDB error (ClientError or otherwise).
        """
        # Schema key is action_id. "id" is a legacy alias kept as a fallback.
        action_id = action.get("action_id") or action.get("id") or str(uuid.uuid4())
        # ---- owner identity comes from the ACTION, and that is a trust edge ----
        #
        # Unlike every other executor, the owner named here is deliberately NOT
        # the caller: this is the reverse direction, where a dealer or the system
        # proposes something TO a customer. So `claims` cannot be the source of
        # truth and this executor cannot self-authorize.
        #
        # ==> CALLER CONTRACT (security-review Cycle 1, Warning 3) <==
        # Any caller that dispatches this executor MUST authorize the caller's
        # right to address this owner_id BEFORE dispatching — i.e. confirm the
        # caller is that owner, or holds a dealer/platform role entitled to write
        # to their feed. Passing an unvalidated, caller-supplied owner_id into this
        # executor lets any authenticated caller post a card to any owner's feed,
        # and (because the broker notifies on card creation) trigger a push to them.
        #
        # That check cannot live here: this module cannot tell a legitimate
        # dealer-originated action from a forged one without the role context the
        # caller holds.
        #
        # STATUS 2026-09-20 — the approve route resolved this by REFUSAL, not by
        # authorization, which is stronger. `lambdas/api-avx-findings/handler.py`
        # omits "ios-owner" from `_VALID_TARGET_SYSTEMS` and names it in
        # `_FORBIDDEN_TARGET_SYSTEMS`, returning 400 (security W1, contract
        # v2.2-final). So there is no longer an unenforced requirement on that
        # route, and this is no longer a handback item.
        #
        # The contract above still binds every FUTURE caller — specifically Spec 2's
        # dealer-side "propose to customer" surface, which is the legitimate writer
        # and will need the authorization the approve route sidestepped by refusing.
        # Do not read the approve route's refusal as the contract being satisfied in
        # general; it is satisfied by that route having no reachable path here.
        #
        # Pinned by two mutation-verified tests in
        # lambdas/api-avx-findings/tests/test_handler.py:
        #   test_approve_rejects_ios_owner_target_system
        #   test_approve_ios_owner_error_is_explicit_not_generic
        # The second exists because the refusal is double-guarded: with only a
        # status-code assertion, emptying `_FORBIDDEN_TARGET_SYSTEMS` would still
        # yield 400 from the unknown-target branch, so the specific control could be
        # deleted invisibly. It pins the explicit message instead. Both were
        # re-verified 2026-09-20 by the stronger mutation (empty the forbidden set
        # AND add "ios-owner" to the valid set); both fail, and the request then
        # reaches PutItem — the vulnerability shape itself.
        owner_id = action.get("owner_id", "")
        category = action.get("category", "service")
        severity = action.get("severity", "informational")
        headline = action.get("headline", "")
        evidence = action.get("evidence", [])
        now = utc_now_iso()

        # Fail closed: an empty owner_id means the row would be addressed to
        # nobody — invisible to every owner-scoped query.  Owner identity comes
        # exclusively from action["owner_id"], by design, so a missing value
        # here cannot be recovered from claims.  Return terminal_error rather
        # than writing a card nobody will ever see.
        if not owner_id:
            return ExecuteResult.terminal_error(
                last_error_class="MissingOwner",
                note=(
                    "owner_id is absent or empty in the action document; cannot "
                    "address the inbox row.  Owner identity is never derived from "
                    "claims for this executor — the caller must supply owner_id."
                ),
            )

        # Stable row id: derive from action_id so that retrying the same Action
        # is idempotent — a second dispatch overwrites the same DDB item rather
        # than creating a duplicate card.  When action_id is absent (e.g. a raw
        # synthetic finding that omitted it), fall back to a fresh UUID and
        # accept that de-duplication is the caller's responsibility.
        row_id = action_id if action_id else str(uuid.uuid4())

        table_name = _actions_table_name()
        # owner_id is deliberately NOT logged. It is a durable customer
        # identifier, and this line runs on every card written — the highest
        # frequency path in the module. row_id correlates to the DDB item for
        # anyone who is already authorised to read it, which is what an operator
        # debugging a write actually needs. Same reasoning as the JWT and VIN
        # exclusions elsewhere in this package (security review Cycle 1/2).
        log.info(
            "ios-owner executor: writing inbox row id=%s table=%s",
            row_id,
            table_name,
        )

        item: dict[str, Any] = {
            "action_id": row_id,
            "owner_id": owner_id,
            "scope": {"owner_id": owner_id},
            "category": category,
            "severity": severity,
            "headline": headline,
            "evidence": evidence,
            "status": ActionStatus.IN_PROGRESS,
            # Preserve the incoming source_action_id when present. The
            # escalation card arrives with action_id='<orig>#escalation' and
            # source_action_id='<orig>'; recomputing it from action_id made the
            # stored row point at ITSELF, losing the link back to the failing
            # Action that escalation.py's docstring promises it preserves.
            "source_action_id": action.get("source_action_id") or action_id,
            "created_at": now,
            "updated_at": now,
        }

        try:
            table = self._ddb.Table(table_name)
            table.put_item(Item=item)
        except Exception as exc:  # noqa: BLE001
            err_class = type(exc).__name__
            log.error(
                "ios-owner executor: DynamoDB put_item failed (%s) for row id=%s",
                err_class,
                row_id,
            )
            return ExecuteResult.terminal_error(
                last_error_class=err_class,
                note=f"DynamoDB write failed: {err_class}",
            )

        return ExecuteResult.success(
            target_ref=row_id,
            status=ActionStatus.IN_PROGRESS,
            # IN_PROGRESS is intentional: the write is durably complete on
            # return, but the card's *visible resolution* is deferred to the
            # broker (which decides whether/when to push a notification).  The
            # approve route treats any non-terminal status as "work continues";
            # the broker's acknowledgement (or the owner's explicit dismiss) is
            # what moves the card to COMPLETED.  See advisory.py's docstring for
            # the case where the write *is* the completion — that reasoning does
            # not apply here because the owner has not yet been notified.
            note="Inbox row written; broker will decide whether to push.",
        )
