"""Addendum D5 dual escalation — shared by every executor.

Spec: `.kiro/specs/2026-09-18-avx-tier2-ios-cards-and-actions/spec.md`
      Addendum D5 + § "Retry count + escalation target — CONFIRMED 2026-09-18"

D5 defines escalation as a property of **the `failed` transition**, not of one
target. This module exists because the first cut of the package implemented it
inside `dms.py` only: the other five executors returned `escalate=True` and
nothing fired — no owner card, no event, no metric. The `AVXActionTerminalFailure`
metric is dimensioned by `target_system`, which is only meaningful if more than
one target can emit it, so an operator alarm on that metric was silent for every
vehicle-command, retail, chat and ios-owner terminal failure.

Both halves, per the confirmed config:

* **Owner-visible** — a synthetic `informational` Finding on the owner's feed via
  `IosOwnerExecutor`, so the owner learns we could not reach the target and is
  offered a next step. Routed through the executor (and thence the broker) rather
  than pushed directly, so consent and channel policy still apply.
* **Operator-visible** — an EventBridge `vsa.avx.action.terminal_failure` event
  plus a `AVXActionTerminalFailure` CloudWatch metric. Alarming on the metric is
  wired in infrastructure, not here.

Two invariants that are easy to get wrong and are both test-enforced:

1.  **No recursion through `ios-owner`.** The owner-visible half dispatches via
    `IosOwnerExecutor`. If an `IosOwnerExecutor` failure escalated the same way,
    it would escalate through itself — and since the failure that triggered it
    would recur, unboundedly. When the failing target *is* `ios-owner`, the
    owner-visible half is skipped and only the operator half fires. That is the
    correct trade: if we cannot write to the owner's feed, writing to the owner's
    feed is not a way to tell them so.
2.  **Escalation never raises.** It runs on a path that is already failing. An
    exception here would escape `dispatch`, be caught by `@never_raises`, and
    overwrite a precise `executor_exhausted_retries` with a vague
    `executor_terminal_error` — losing the retry count the audit trail and the
    escalation event both carry.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Callable, Mapping, Optional

from agents.executors.base import (
    DEFAULT_ESCALATION,
    ExecuteResult,
    load_escalation_config,
    utc_now_iso,
)

log = logging.getLogger(__name__)

#: The target whose own failures must not be escalated back through itself.
_OWNER_FEED_TARGET = "ios-owner"

#: Operator-alarm routing keys that are NOT remotely reconfigurable. See the
#: rationale at the operator-visible half in `fire()`.
#: Statuses that mean the owner card was NOT written.
_FAILED_STATUSES = frozenset({"failed", "declined"})

_PINNED_OPERATOR_KEYS = frozenset(
    {
        "eventbridge_source",
        "eventbridge_detail_type",
        "cloudwatch_metric",
        "cloudwatch_dimension",
    }
)

_events_client: Any = None
_cloudwatch_client: Any = None


def _region() -> str:
    return os.environ.get("AWS_REGION", "us-west-2")


def _get_events() -> Any:
    global _events_client
    if _events_client is None:
        import boto3  # noqa: PLC0415 — lazy: no import-time client construction

        _events_client = boto3.client("events", region_name=_region())
    return _events_client


def _get_cloudwatch() -> Any:
    global _cloudwatch_client
    if _cloudwatch_client is None:
        import boto3  # noqa: PLC0415

        _cloudwatch_client = boto3.client("cloudwatch", region_name=_region())
    return _cloudwatch_client


def reset_clients() -> None:
    """Drop cached clients. For tests."""
    global _events_client, _cloudwatch_client
    _events_client = None
    _cloudwatch_client = None


def _action_kind_verb(action_kind: str) -> str:
    """Human-readable verb phrase for the owner-facing headline."""
    return (action_kind or "complete that request").replace("_", " ")


def build_owner_finding(
    *,
    action: Mapping[str, Any],
    target_system: str,
    result: ExecuteResult,
    cfg: Mapping[str, Any],
) -> dict[str, Any]:
    """Build the owner-visible synthetic Finding.

    `owner_id` is carried from the failing Action. It is **required**: an earlier
    version omitted it, and because `IosOwnerExecutor` deliberately takes owner
    identity only from the action (never from claims), the escalation card was
    written with `owner_id: ""` — addressed to nobody, invisible to every
    owner-scoped query — and the executor reported success. The owner was never
    told we could not reach the dealer, and the system recorded that they were.

    **The card carries a DERIVED `action_id`, never the failing Action's own.**
    `IosOwnerExecutor` uses `action_id` as the DynamoDB row id (deliberately, for
    retry idempotency) and `avx-actions` is keyed on `action_id` alone — so
    passing the original id through would make `put_item` a full replace of the
    very Action that just failed: `finding_id`, `created_by`, `target_system`,
    `action_kind` and, worst, the `audit_trail` carrying the D5 failure record
    would all be destroyed, the row would drop out of every GSI, and its status
    would be reset to the card's. The failure record would be erased by the act
    of telling the owner about the failure.

    This is a fix for an interaction between two separately-correct changes
    (carrying `action_id` into the card, and deriving `row_id` from `action_id`),
    which is why neither change looked wrong on its own. The suffix keeps the
    correlation handle while guaranteeing a distinct key, and `source_action_id`
    preserves the link back.
    """
    owner_cfg = dict(cfg.get("owner_visible") or {})
    target_display = target_system.replace("-", " ")
    source_action_id = action.get("action_id")
    return {
        # Derived, NOT `source_action_id` — see the docstring.
        "action_id": f"{source_action_id}#escalation" if source_action_id else None,
        "source_action_id": source_action_id,
        "owner_id": action.get("owner_id"),
        "scope": {"owner_id": action.get("owner_id")},
        "finding_kind": "service.reach_failure",
        "category": owner_cfg.get("category", "service"),
        "severity": owner_cfg.get("severity", "informational"),
        "headline": (
            f"we couldn't reach {target_display} to "
            f"{_action_kind_verb(str(action.get('action_kind') or ''))}. "
            f"Options: retry with a different {target_display}, or contact us."
        ),
        "computed_at": utc_now_iso(),
        "evidence": [],
        "target_system": target_system,
        "action_kind": action.get("action_kind"),
        "retry_count": result.retry_count,
        "last_error_class": result.last_error_class,
        "last_error_status": result.last_error_status,
    }


def fire(
    *,
    action: Mapping[str, Any],
    claims: Mapping[str, Any],
    target_system: str,
    result: ExecuteResult,
    enqueue_owner_card: Optional[Callable[..., Any]] = None,
    events_client: Any = None,
    cloudwatch_client: Any = None,
    ssm_client: Any = None,
) -> dict[str, bool]:
    """Fire both halves of the D5 escalation. Never raises.

    Returns a per-half outcome map so a caller (and a test) can assert on what
    actually fired rather than on a flag. Each half is attempted independently:
    a failure to write the owner card must not suppress the operator signal,
    which is the only half a human is alarmed on.
    """
    outcome = {"owner_visible": False, "operator_visible": False}

    if not result.escalate:
        return outcome

    # `load_escalation_config` and the payload construction below used to sit
    # OUTSIDE any try, so the module's stated invariant #2 ("escalation never
    # raises") was asserted in two docstrings and enforced in neither. Low
    # data-driven reachability is not the same as an enforced invariant.
    try:
        cfg = load_escalation_config(target_system, ssm_client=ssm_client)
    except Exception as exc:  # noqa: BLE001
        log.error(
            "escalation: could not load config for target=%s (%s); using defaults",
            target_system,
            type(exc).__name__,
        )
        cfg = dict(DEFAULT_ESCALATION)

    # ---- owner-visible half -------------------------------------------------
    if target_system == _OWNER_FEED_TARGET:
        log.warning(
            "escalation: skipping the owner-visible half for target=%s — "
            "escalating an owner-feed failure through the owner feed would "
            "recurse; operator half still fires",
            target_system,
        )
    else:
        try:
            finding = build_owner_finding(
                action=action, target_system=target_system, result=result, cfg=cfg
            )
            if not finding.get("owner_id"):
                # Fail loudly rather than writing a card addressed to nobody.
                log.error(
                    "escalation: action %s has no owner_id; cannot address the "
                    "owner-visible card. Operator half still fires.",
                    action.get("action_id"),
                )
            else:
                enqueue = enqueue_owner_card or _default_enqueue_owner_card
                enqueued = enqueue(finding, claims)
                # Believe the enqueue's own verdict rather than the absence of an
                # exception. `IosOwnerExecutor.dispatch` is @never_raises-wrapped,
                # so a failed DynamoDB write returns a `failed` ExecuteResult and
                # raises nothing — an earlier version set owner_visible=True on
                # any return at all, and therefore reported a delivered owner card
                # for a write that never landed. That is the same
                # reports-success-for-work-that-did-not-happen shape this module
                # exists to prevent, sited in the module itself.
                #
                # A collaborator that returns None (a plain callable, or a test
                # stub) is treated as success, since it has no verdict to offer.
                status = getattr(enqueued, "status", None)
                if status is None or status not in _FAILED_STATUSES:
                    outcome["owner_visible"] = True
                else:
                    log.error(
                        "escalation: owner-visible card for action %s was NOT "
                        "written (enqueue returned status=%s kind=%s); the owner "
                        "has not been told the dispatch failed",
                        action.get("action_id"),
                        status,
                        getattr(enqueued, "failure_kind", None),
                    )
        except Exception as exc:  # noqa: BLE001 — escalation must not raise
            log.error(
                "escalation: owner-visible half failed (%s); operator half still "
                "attempted",
                type(exc).__name__,
            )

    # ---- operator-visible half ---------------------------------------------
    #
    # The routing keys below are read from the DEFAULTS, not from the merged
    # config, and that is deliberate. `load_escalation_config` merges any SSM
    # `operator_visible` dict over the defaults, so an actor holding
    # `ssm:PutParameter` on the escalation parameter could otherwise rename
    # `eventbridge_source` / `eventbridge_detail_type` / `cloudwatch_metric` /
    # `cloudwatch_dimension` and silence the only operator alarm D5 provides —
    # no code change, no deploy, nothing in a diff to notice. The spec identifies
    # no operational benefit to making the routing itself tunable (the tunable
    # parts are the owner-facing category/severity, which stay merged), so the
    # alarm's own address is pinned in code where changing it requires review.
    op = dict(DEFAULT_ESCALATION["operator_visible"])
    _ignored = {
        k: v
        for k, v in (cfg.get("operator_visible") or {}).items()
        if k in _PINNED_OPERATOR_KEYS and v != op.get(k)
    }
    if _ignored:
        log.warning(
            "escalation: ignoring SSM override of pinned operator routing key(s) "
            "%s for target=%s — the operator alarm address is not remotely "
            "reconfigurable; change it in code under review",
            sorted(_ignored),
            target_system,
        )
    detail = {
        "action_id": action.get("action_id"),
        "target_system": target_system,
        "action_kind": action.get("action_kind"),
        "retry_count": result.retry_count,
        "last_error_class": result.last_error_class,
        "last_error_status": result.last_error_status,
    }
    try:
        (events_client or _get_events()).put_events(
            Entries=[
                {
                    "Source": op.get("eventbridge_source", "vsa.avx.executor"),
                    "DetailType": op.get("eventbridge_detail_type", "TerminalFailure"),
                    "Detail": json.dumps(detail),
                }
            ]
        )
        (cloudwatch_client or _get_cloudwatch()).put_metric_data(
            Namespace="AVX/Executor",
            MetricData=[
                {
                    "MetricName": op.get("cloudwatch_metric", "AVXActionTerminalFailure"),
                    "Dimensions": [
                        {
                            "Name": op.get("cloudwatch_dimension", "target_system"),
                            "Value": target_system,
                        }
                    ],
                    "Value": 1,
                    "Unit": "Count",
                }
            ],
        )
        outcome["operator_visible"] = True
    except Exception as exc:  # noqa: BLE001 — escalation must not raise
        log.error(
            "escalation: operator-visible half failed for action %s target=%s (%s)",
            action.get("action_id"),
            target_system,
            type(exc).__name__,
        )

    return outcome


def _default_enqueue_owner_card(finding: Mapping[str, Any], claims: Mapping[str, Any]) -> Any:
    """Dispatch the owner card through `IosOwnerExecutor`.

    Imported lazily to avoid a module-level import cycle: `ios_owner` imports
    nothing from here, but the registry imports both.
    """
    from agents.executors.ios_owner import IosOwnerExecutor  # noqa: PLC0415

    return IosOwnerExecutor().dispatch(dict(finding), dict(claims))
