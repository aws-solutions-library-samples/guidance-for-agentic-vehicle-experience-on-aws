"""delivery_status tool — order pipeline stage + history lookup.

Reads the order record from ``vsa-acquire-orders`` by orderId and returns
the current pipeline stage, stage history, next transition timestamp, and
logistics references.

TENANT ISOLATION IS MANDATORY: the fetched item's ``tenantId`` MUST match
``ctx.tenant_id`` before any data is returned. A mismatch returns
``{found: false, reason: "tenant mismatch"}`` and logs a warning. This
prevents a compromised or confused LLM from leaking another tenant's
order data by guessing an orderId.

When ``acquire_orders_table`` is not configured the tool returns
``{available: false}``.
"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any

import boto3
from botocore.exceptions import BotoCoreError, ClientError
from strands import tool

from agents.supervisor.config import Config
from agents.supervisor.session import SessionContext

log = logging.getLogger(__name__)

_ddb_resource: Any = None


def _get_ddb(region: str = "us-west-2"):
    global _ddb_resource
    if _ddb_resource is None:
        _ddb_resource = boto3.resource("dynamodb", region_name=region)
    return _ddb_resource


def _decimal_to_float(value: Any) -> Any:
    """Recursively convert Decimal values returned by DDB to float."""
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, dict):
        return {k: _decimal_to_float(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_decimal_to_float(v) for v in value]
    return value


def make_delivery_status_tool(ctx: SessionContext, config: Config):
    """Build the delivery_status tool bound to a session context."""

    orders_table: str = getattr(config, "acquire_orders_table", "") or ""
    aws_region: str = getattr(config, "aws_region", "us-west-2") or "us-west-2"

    @tool
    def delivery_status(
        orderId: str,
    ) -> dict[str, Any]:
        """Look up the current delivery pipeline stage for a vehicle order.

        Use this tool when the customer asks about their order status, which
        manufacturing stage their vehicle is in, or when it will be ready.

        Args:
            orderId: The order identifier, e.g. "ORD-20260721-abc123".

        Returns:
            Dict with ``orderId``, ``stage`` (current pipeline stage),
            ``stageHistory`` (list of entered stages with timestamps),
            ``nextTransitionAt`` (ISO-8601 timestamp of next expected
            stage advance, may be absent for terminal stage),
            ``plantId``, ``dealerRef``, and ``hasEta`` (bool).
            Returns ``{found: false, reason: ...}`` if the order is not
            found or belongs to a different tenant.
        """
        if not orders_table:
            return {"available": False, "reason": "orders table not configured"}

        try:
            ddb = _get_ddb(aws_region)
            table = ddb.Table(orders_table)
            resp = table.get_item(Key={"orderId": orderId})
        except (BotoCoreError, ClientError) as exc:
            log.warning("delivery_status: GetItem failed for %s: %s", orderId, exc)
            return {"found": False, "reason": f"lookup failed: {exc}"}

        item = resp.get("Item")
        if not item:
            return {"found": False, "reason": "order not found"}

        item = _decimal_to_float(item)

        # MANDATORY tenant isolation check — MUST come before any data return
        item_tenant_id = item.get("tenantId", "")
        if item_tenant_id != ctx.tenant_id:
            log.warning(
                "delivery_status: tenant mismatch for orderId=%s "
                "(expected=%s, got=%s) — blocking response",
                orderId,
                ctx.tenant_id,
                item_tenant_id,
            )
            return {"found": False, "reason": "tenant mismatch"}

        next_transition = item.get("nextTransitionAt")
        return {
            "found": True,
            "orderId": orderId,
            "stage": item.get("stage", ""),
            "stageHistory": item.get("stageHistory", []),
            "nextTransitionAt": next_transition,
            "plantId": item.get("plantId", ""),
            "dealerRef": item.get("dealerRef", ""),
            "hasEta": next_transition is not None,
        }

    return delivery_status
