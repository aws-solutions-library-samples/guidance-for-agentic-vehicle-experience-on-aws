"""EventBridge scheduled Lambda — advances acquire orders through the 10-stage pipeline.

Cron: every 5 minutes via EventBridge Rule.

Logic:
  1. Determine tenants to scan: from event payload `tenantIds` list,
     or from env var VSA_ACQUIRE_ACTIVE_TENANTS (comma-separated).
  2. For each tenant: Query `by-progression-status` GSI for orders
     where nextTransitionAt <= now().
  3. For each such order: advance `stage` to the next in the fixed sequence.
     Append to stageHistory.  Compute new nextTransitionAt from tenant
     deliveryCadence (read from vsa-acquire-catalog) or fall back to
     24-hour default.  On terminal stage (readyForDelivery): REMOVE
     nextTransitionAt so the order drops out of the GSI.
  4. UpdateItem with ConditionExpression on the observed current stage +
     nextTransitionAt — idempotent (duplicate invocations are no-ops).

Returns:
    {
      "advanced": <int>,
      "skipped":  <int>,
      "errors":   [{"orderId": ..., "reason": ...}]
    }
"""
from __future__ import annotations

import json
import logging
import os
import random
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import boto3
from boto3.dynamodb.conditions import Key
from botocore.exceptions import BotoCoreError, ClientError

log = logging.getLogger()
log.setLevel(logging.INFO)

_ORDERS_TABLE = os.environ.get("VSA_ACQUIRE_ORDERS_TABLE", "vsa-dev-acquire-orders")
_CATALOG_TABLE = os.environ.get("VSA_ACQUIRE_CATALOG_TABLE", "vsa-dev-acquire-catalog")
_ACTIVE_TENANTS_ENV = os.environ.get("VSA_ACQUIRE_ACTIVE_TENANTS", "")

# Fixed stage sequence — identifiers are stable across tenants.
STAGES = [
    "orderPlaced",
    "paymentReceived",
    "assignedToPlant",
    "subAssembly",
    "paint",
    "finalAssembly",
    "qualityControl",
    "shipped",
    "atDealer",
    "readyForDelivery",
]
TERMINAL_STAGE = "readyForDelivery"

# Mapping from outgoing stage → deliveryCadence key used to compute
# the dwell time before the *next* stage.  Only non-terminal stages
# have an entry; terminal orders have nextTransitionAt removed.
_CADENCE_KEY: dict[str, str] = {
    "orderPlaced":       "orderPlacedToPayment",
    "paymentReceived":   "paymentToAssignedToPlant",
    "assignedToPlant":   "assignedToPlantToSubAssembly",
    "subAssembly":       "subAssemblyToPaint",
    "paint":             "paintToFinalAssembly",
    "finalAssembly":     "finalAssemblyToQualityControl",
    "qualityControl":    "qualityControlToShipped",
    "shipped":           "shippedToAtDealer",
    "atDealer":          "atDealerToReadyForDelivery",
}

# Default dwell (hours) when tenant config is missing — 24h.
_DEFAULT_DWELL_HOURS = 24.0

_ddb = None


def _get_ddb():
    global _ddb
    if _ddb is None:
        _ddb = boto3.resource("dynamodb")
    return _ddb


def _to_ddb_safe(value: Any) -> Any:
    """Convert floats to Decimal so DynamoDB accepts them."""
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, dict):
        return {k: _to_ddb_safe(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_to_ddb_safe(v) for v in value]
    return value


def _next_stage(current: str) -> str | None:
    """Return the next stage after `current`, or None if already terminal."""
    try:
        idx = STAGES.index(current)
    except ValueError:
        return None
    next_idx = idx + 1
    if next_idx >= len(STAGES):
        return None
    return STAGES[next_idx]


def _get_tenant_cadence(tenant_id: str) -> dict:
    """Fetch deliveryCadence from tenant config in vsa-acquire-catalog.

    Item key: (tenantId=<tenant_id>, itemKey="META#tenantConfig").
    Falls back to an empty dict (all dwell times use _DEFAULT_DWELL_HOURS).
    """
    try:
        resp = _get_ddb().Table(_CATALOG_TABLE).get_item(
            Key={"tenantId": tenant_id, "itemKey": "META#tenantConfig"}
        )
        item = resp.get("Item") or {}
        return item.get("deliveryCadence") or {}
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "order_progressor: failed to fetch tenantConfig for %s: %s — "
            "using default dwell times",
            tenant_id, exc,
        )
        return {}


def _compute_next_transition(current_stage: str, cadence: dict, now: datetime) -> str:
    """Sample a dwell time from cadence for `current_stage` transition and
    return the next wall-clock transition timestamp as ISO-8601."""
    cadence_key = _CADENCE_KEY.get(current_stage)
    if cadence_key and cadence_key in cadence:
        band = cadence[cadence_key]
        min_h = float(band.get("minHours", _DEFAULT_DWELL_HOURS))
        max_h = float(band.get("maxHours", _DEFAULT_DWELL_HOURS))
    else:
        min_h = max_h = _DEFAULT_DWELL_HOURS

    dwell_hours = random.uniform(min_h, max_h)
    return (now + timedelta(hours=dwell_hours)).isoformat()


def _advance_order(order: dict, tenant_cadence: dict, now: datetime) -> str:
    """Attempt to advance a single order.  Returns 'advanced', 'skipped', or 'error:<msg>'."""
    order_id = order.get("orderId")
    current_stage = order.get("stage")
    current_next_at = order.get("nextTransitionAt")

    if current_stage == TERMINAL_STAGE:
        log.debug("order_progressor: orderId=%s already terminal, skipping", order_id)
        return "skipped"

    if current_next_at is None:
        log.debug("order_progressor: orderId=%s has no nextTransitionAt, skipping", order_id)
        return "skipped"

    next_stage = _next_stage(current_stage)
    if next_stage is None:
        log.warning("order_progressor: orderId=%s unknown stage=%s", order_id, current_stage)
        return "skipped"

    now_iso = now.isoformat()
    is_terminal = (next_stage == TERMINAL_STAGE)

    # Build the UpdateExpression.  If the next stage is terminal, REMOVE
    # nextTransitionAt so the order drops out of the by-progression-status GSI.
    if is_terminal:
        update_expr = (
            "SET #stage = :nextStage, "
            "#sh = list_append(#sh, :newEntry) "
            "REMOVE nextTransitionAt"
        )
        expr_values = _to_ddb_safe({
            ":nextStage": next_stage,
            ":newEntry": [{"stage": next_stage, "enteredAt": now_iso}],
            ":curStage": current_stage,
            ":curNextAt": current_next_at,
        })
    else:
        new_next_at = _compute_next_transition(next_stage, tenant_cadence, now)
        update_expr = (
            "SET #stage = :nextStage, "
            "nextTransitionAt = :newNextAt, "
            "#sh = list_append(#sh, :newEntry)"
        )
        expr_values = _to_ddb_safe({
            ":nextStage": next_stage,
            ":newNextAt": new_next_at,
            ":newEntry": [{"stage": next_stage, "enteredAt": now_iso}],
            ":curStage": current_stage,
            ":curNextAt": current_next_at,
        })

    try:
        _get_ddb().Table(_ORDERS_TABLE).update_item(
            Key={"orderId": order_id},
            UpdateExpression=update_expr,
            ExpressionAttributeNames={
                "#stage": "stage",
                "#sh": "stageHistory",
            },
            ExpressionAttributeValues=expr_values,
            # Idempotency: only advance if stage + nextTransitionAt still match
            # what we observed.  A concurrent invocation that already advanced
            # the order will cause a ConditionalCheckFailed.
            ConditionExpression=(
                "#stage = :curStage AND nextTransitionAt = :curNextAt"
            ),
        )
        log.info(
            "order_progressor: advanced orderId=%s %s → %s",
            order_id, current_stage, next_stage,
        )
        return "advanced"

    except ClientError as exc:
        code = exc.response["Error"]["Code"]
        if code == "ConditionalCheckFailedException":
            log.info(
                "order_progressor: idempotent no-op for orderId=%s "
                "(already advanced by concurrent invocation)",
                order_id,
            )
            return "skipped"
        log.exception(
            "order_progressor: UpdateItem failed for orderId=%s: %s",
            order_id, exc,
        )
        return f"error:{exc}"
    except (BotoCoreError, Exception) as exc:  # noqa: BLE001
        log.exception(
            "order_progressor: unexpected error for orderId=%s: %s",
            order_id, exc,
        )
        return f"error:{exc}"


def _query_due_orders(tenant_id: str, now_iso: str) -> list[dict]:
    """Query the by-progression-status GSI for orders whose nextTransitionAt <= now_iso."""
    orders: list[dict] = []
    try:
        paginator_kwargs: dict = {
            "IndexName": "by-progression-status",
            "KeyConditionExpression": (
                Key("tenantId").eq(tenant_id) &
                Key("nextTransitionAt").lte(now_iso)
            ),
        }
        table = _get_ddb().Table(_ORDERS_TABLE)
        resp = table.query(**paginator_kwargs)
        orders.extend(resp.get("Items") or [])

        while resp.get("LastEvaluatedKey"):
            resp = table.query(
                **paginator_kwargs,
                ExclusiveStartKey=resp["LastEvaluatedKey"],
            )
            orders.extend(resp.get("Items") or [])
    except Exception as exc:  # noqa: BLE001
        log.exception(
            "order_progressor: GSI query failed for tenantId=%s: %s",
            tenant_id, exc,
        )
    return orders


def _get_order(order_id: str) -> dict | None:
    """Fetch a single order by orderId.  Returns None if not found."""
    try:
        resp = _get_ddb().Table(_ORDERS_TABLE).get_item(
            Key={"orderId": order_id}
        )
        return resp.get("Item") or None
    except Exception as exc:  # noqa: BLE001
        log.exception(
            "order_progressor: GetItem failed for orderId=%s: %s",
            order_id, exc,
        )
        return None


def handler(event: dict, _context) -> dict:
    """EventBridge cron / Step Functions single-order entry point.

    Two invocation shapes are supported:

    1. Single-order (Step Functions):
         {"orderId": "<id>"}
       Fetches that one order and advances it exactly once.  Idempotent —
       safe to retry via a Step Functions Standard workflow.

    2. Tenant-scan (EventBridge cron):
         {"tenantIds": ["tenant-a", "tenant-b"]}
       — or configured via env VSA_ACQUIRE_ACTIVE_TENANTS (comma-separated).
       Queries the by-progression-status GSI for each tenant and advances
       all due orders.

    Returns: {"advanced": int, "skipped": int, "errors": [{orderId, reason}]}
    """
    now = datetime.now(timezone.utc)

    # ------------------------------------------------------------------ #
    # Single-order mode — Step Functions sends {"orderId": "..."}         #
    # ------------------------------------------------------------------ #
    order_id = event.get("orderId")
    if order_id:
        log.info("order_progressor: single-order mode orderId=%s", order_id)
        order = _get_order(order_id)
        if order is None:
            log.warning(
                "order_progressor: orderId=%s not found — treating as skipped "
                "(safe to retry; DDB item may not exist yet or was deleted)",
                order_id,
            )
            return {"advanced": 0, "skipped": 1, "errors": []}

        # Cadence comes from the order's own tenantId.
        tenant_id = order.get("tenantId", "")
        cadence = _get_tenant_cadence(tenant_id) if tenant_id else {}

        result = _advance_order(order, cadence, now)
        if result == "advanced":
            return {"advanced": 1, "skipped": 0, "errors": []}
        if result == "skipped":
            return {"advanced": 0, "skipped": 1, "errors": []}
        # error path
        return {
            "advanced": 0,
            "skipped": 0,
            "errors": [{"orderId": order_id, "reason": result}],
        }

    # ------------------------------------------------------------------ #
    # Tenant-scan mode — EventBridge cron                                 #
    # ------------------------------------------------------------------ #
    now_iso = now.isoformat()

    tenant_ids: list[str] = list(event.get("tenantIds") or [])
    if not tenant_ids and _ACTIVE_TENANTS_ENV:
        tenant_ids = [t.strip() for t in _ACTIVE_TENANTS_ENV.split(",") if t.strip()]

    if not tenant_ids:
        log.info("order_progressor: no tenants configured, nothing to do")
        return {"advanced": 0, "skipped": 0, "errors": []}

    total_advanced = 0
    total_skipped = 0
    errors: list[dict] = []

    for tenant_id in tenant_ids:
        cadence = _get_tenant_cadence(tenant_id)
        due_orders = _query_due_orders(tenant_id, now_iso)
        log.info(
            "order_progressor: tenantId=%s found %d due orders",
            tenant_id, len(due_orders),
        )

        for order in due_orders:
            result = _advance_order(order, cadence, now)
            if result == "advanced":
                total_advanced += 1
            elif result == "skipped":
                total_skipped += 1
            else:
                errors.append({"orderId": order.get("orderId"), "reason": result})

    log.info(
        "order_progressor: done — advanced=%d skipped=%d errors=%d",
        total_advanced, total_skipped, len(errors),
    )
    return {
        "advanced": total_advanced,
        "skipped": total_skipped,
        "errors": errors,
    }
