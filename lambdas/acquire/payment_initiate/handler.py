"""POST /acquire/payment-initiate

Mock payment-capture endpoint.  No real payment processor is called.
Returns a deterministic transaction reference derived from the
(tenantId, orderId, amount) triple — so the same inputs always produce
the same transactionRef.

Request (JSON):
    {
      "tenantId":      string  required
      "orderId":       string  required  correlates to vsa-acquire-orders
      "amount":        number  required  deposit amount in currency units
      "currencyCode":  string  required  ISO 4217 (e.g. "USD", "INR")
      "method":        string  required  "card" | "ach" | "upi" | "wallet"
      "partnerId":     string  required  opaque partner identifier
    }

Response:
    {
      "transactionRef": "TXN-<uuid5>",
      "status":         "captured" | "pending" | "declined"
      "providerName":   string
      "capturedAt":     ISO-8601 or null
      "receipt":        string
    }

Auth: Cognito authorizer.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

import boto3
from botocore.exceptions import BotoCoreError, ClientError

log = logging.getLogger()
log.setLevel(logging.INFO)

_ORDERS_TABLE = os.environ.get("VSA_ACQUIRE_ORDERS_TABLE", "vsa-dev-acquire-orders")

_ddb = None


def _get_ddb():
    global _ddb
    if _ddb is None:
        _ddb = boto3.resource("dynamodb")
    return _ddb


class _DecimalEncoder(json.JSONEncoder):
    def default(self, o):
        if isinstance(o, Decimal):
            return int(o) if o % 1 == 0 else float(o)
        return super().default(o)


def _resp(status: int, body: dict) -> dict:
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body, cls=_DecimalEncoder),
    }


def _to_ddb_safe(value: Any) -> Any:
    """Convert floats to Decimal so DynamoDB accepts them."""
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, dict):
        return {k: _to_ddb_safe(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_to_ddb_safe(v) for v in value]
    return value


# UUIDv5 namespace for deterministic transactionRef generation.
_TXN_NAMESPACE = uuid.UUID("d9e6f3b2-4a1c-5e7d-8f0a-1b2c3d4e5f60")

_PROVIDER_NAMES = {
    "card": "Mock Card Network",
    "ach": "Mock ACH Network",
    "upi": "Mock UPI Gateway",
    "wallet": "Mock Digital Wallet",
}


def _make_txn_ref(tenant_id: str, order_id: str, amount: float) -> str:
    """Deterministic UUIDv5-based transaction reference.
    Same (tenantId, orderId, amount) → same ref, always.
    Wall-clock is NOT used in derivation."""
    name = f"{tenant_id}:{order_id}:{amount:.4f}"
    txn_uuid = uuid.uuid5(_TXN_NAMESPACE, name)
    return f"TXN-{txn_uuid}"


def handler(event: dict, _context) -> dict:
    """Lambda entry point."""
    try:
        body = json.loads(event.get("body") or "{}")
    except json.JSONDecodeError:
        return _resp(400, {"error": "Body must be JSON"})

    required = ["tenantId", "orderId", "amount", "currencyCode", "method", "partnerId"]
    missing = [k for k in required if not body.get(k)]
    if missing:
        return _resp(400, {"error": f"Missing required fields: {missing}"})

    method = body["method"]
    valid_methods = {"card", "ach", "upi", "wallet"}
    if method not in valid_methods:
        return _resp(400, {"error": f"method must be one of {sorted(valid_methods)}"})

    try:
        amount = float(body["amount"])
    except (TypeError, ValueError) as exc:
        return _resp(400, {"error": f"Invalid amount: {exc}"})

    if amount <= 0:
        return _resp(400, {"error": "amount must be positive"})

    tenant_id = body["tenantId"]
    order_id = body["orderId"]
    currency_code = body["currencyCode"]
    partner_id = body["partnerId"]

    txn_ref = _make_txn_ref(tenant_id, order_id, amount)
    now = datetime.now(timezone.utc)
    captured_at = now.isoformat()
    provider_name = _PROVIDER_NAMES.get(method, "Mock Payment Provider")

    receipt = (
        f"Receipt: {txn_ref} | "
        f"{currency_code} {amount:.2f} via {provider_name} | "
        f"Captured: {captured_at}"
    )

    payment_update = {
        "transactionRef": txn_ref,
        "status": "captured",
        "capturedAt": captured_at,
        "method": method,
        "partnerId": partner_id,
    }

    # Update the order record if orderId is present (best-effort).
    # Uses conditional UpdateItem; if the item doesn't exist yet, the
    # update is silently skipped — the reservation_handoff write may come
    # after payment in some flows.
    try:
        _get_ddb().Table(_ORDERS_TABLE).update_item(
            Key={"orderId": order_id},
            UpdateExpression=(
                "SET #pay.transactionRef = :txnRef, "
                "#pay.#st = :status, "
                "#pay.capturedAt = :cap"
            ),
            ExpressionAttributeNames={
                "#pay": "payment",
                "#st": "status",
            },
            ExpressionAttributeValues=_to_ddb_safe({
                ":txnRef": txn_ref,
                ":status": "captured",
                ":cap": captured_at,
            }),
            ConditionExpression="attribute_exists(orderId)",
        )
        log.info(
            "payment_initiate: updated orderId=%s txnRef=%s",
            order_id, txn_ref,
        )
    except ClientError as exc:
        code = exc.response["Error"]["Code"]
        if code == "ConditionalCheckFailedException":
            # Order not yet created — ignore; reservation_handoff will
            # record the payment data when it writes the full order.
            log.info(
                "payment_initiate: orderId=%s not found in table — "
                "skipping DDB update (pre-reservation flow)",
                order_id,
            )
        else:
            log.exception(
                "payment_initiate: DDB UpdateItem failed for orderId=%s: %s",
                order_id, exc,
            )
            return _resp(500, {"error": "Failed to update order payment record"})
    except (BotoCoreError, Exception) as exc:  # noqa: BLE001
        log.exception(
            "payment_initiate: unexpected error for orderId=%s: %s",
            order_id, exc,
        )
        return _resp(500, {"error": "Internal server error"})

    return _resp(200, {
        "transactionRef": txn_ref,
        "status": "captured",
        "providerName": provider_name,
        "capturedAt": captured_at,
        "receipt": receipt,
    })
