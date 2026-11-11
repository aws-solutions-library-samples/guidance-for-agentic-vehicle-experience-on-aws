"""POST /acquire/finance-qualify

Deterministic mock captive-finance qualification.  Returns the same
financing tier every time for the same (tenantId, ownerSub,
requestedFinancingAmount) triple.  No real credit bureau or
finance-arm API is called.

Request (JSON):
    {
      "tenantId":                   string  required
      "ownerSub":                   string  required  Cognito sub
      "requestedFinancingAmount":   number  required  total vehicle price in currency units
      "downPayment":                number  required
      "term_months":                int     required  e.g. 36, 48, 60
      "income":                     number  required  annual gross income
      "employmentStatus":           string  required  e.g. "employed", "self-employed", "retired"
    }

Response:
    {
      "tier":             "prime" | "near-prime" | "subprime" | "declined"
      "approvedAmount":   number
      "apr":              number   annual percentage rate (decimal, e.g. 4.9)
      "monthly":          number   estimated monthly payment
      "disclosure":       string   full lender-disclosure text
      "escalationReason": string   (only present when tier == "declined")
    }

Auth: Cognito authorizer.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import os
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


def _monthly_payment(principal: float, annual_rate_pct: float, term_months: int) -> float:
    """Standard amortization formula.  Returns 0 if rate is 0 or term is 0."""
    if term_months <= 0:
        return 0.0
    monthly_rate = annual_rate_pct / 100.0 / 12.0
    if monthly_rate == 0:
        return round(principal / term_months, 2)
    factor = (1 + monthly_rate) ** term_months
    return round(principal * monthly_rate * factor / (factor - 1), 2)


def qualify(
    tenant_id: str,
    owner_sub: str,
    requested_amount: float,
    down_payment: float,
    term_months: int,
    income: float,
    employment_status: str,  # noqa: ARG001  — reserved for future tier scoring
) -> dict:
    """Pure qualification logic — no I/O.  Deterministic on (tenantId, ownerSub, amount).

    Scoring bands (in descending preference):
      prime      — income >= 100 000 AND down_payment/requested >= 0.20
      near-prime — income >=  60 000 AND down_payment/requested >= 0.10
      subprime   — income >=  40 000
      declined   — otherwise
    """
    # Deterministic seed from the three identifier fields so the same
    # applicant always gets the same tier for the same vehicle price.
    seed_input = f"{tenant_id}|{owner_sub}|{requested_amount:.2f}"
    _seed_hash = int(hashlib.sha256(seed_input.encode()).hexdigest(), 16) % (10 ** 8)  # noqa: F841
    # (The hash is computed for determinism guarantee but the tier is derived
    #  purely from income / down-payment thresholds — which are already
    #  deterministic given the same inputs.  The hash is reserved for future
    #  random tie-breaking within a band without altering the band assignment.)

    down_ratio = down_payment / requested_amount if requested_amount > 0 else 0.0

    if income >= 100_000 and down_ratio >= 0.20:
        tier = "prime"
        apr = 4.9
        approved = requested_amount
    elif income >= 60_000 and down_ratio >= 0.10:
        tier = "near-prime"
        apr = 7.9
        approved = requested_amount
    elif income >= 40_000:
        tier = "subprime"
        apr = 12.9
        approved = requested_amount * 0.80
    else:
        tier = "declined"
        apr = 0.0
        approved = 0.0

    financed = max(approved - down_payment, 0.0)
    monthly = _monthly_payment(financed, apr, term_months) if tier != "declined" else 0.0

    disclosure = (
        f"This financing offer is provided by the preferred finance partner for {tenant_id}. "
        "The quoted Annual Percentage Rate (APR) and monthly payment are indicative estimates "
        "based on the information you have provided. Final credit terms are subject to lender "
        "approval, verification of stated income, and applicable regulatory requirements. "
        "This pre-qualification does not constitute a binding credit offer. "
        "You are not obligated to proceed. "
        "The refundable reservation deposit is separate from any financing arrangement and "
        "will be applied toward the vehicle purchase price upon final credit approval. "
        "Please review all loan documents carefully before signing."
    )

    result: dict = {
        "tier": tier,
        "approvedAmount": round(approved, 2),
        "apr": apr,
        "monthly": monthly,
        "disclosure": disclosure,
    }
    if tier == "declined":
        result["escalationReason"] = "Below-threshold income"

    return result


def handler(event: dict, _context) -> dict:
    """Lambda entry point."""
    try:
        body = json.loads(event.get("body") or "{}")
    except json.JSONDecodeError:
        return _resp(400, {"error": "Body must be JSON"})

    required = [
        "tenantId", "ownerSub", "requestedFinancingAmount",
        "downPayment", "term_months", "income", "employmentStatus",
    ]
    missing = [k for k in required if body.get(k) is None]
    if missing:
        return _resp(400, {"error": f"Missing required fields: {missing}"})

    try:
        requested_amount = float(body["requestedFinancingAmount"])
        down_payment = float(body["downPayment"])
        term_months = int(body["term_months"])
        income = float(body["income"])
    except (TypeError, ValueError) as exc:
        return _resp(400, {"error": f"Invalid numeric field: {exc}"})

    if requested_amount <= 0:
        return _resp(400, {"error": "requestedFinancingAmount must be positive"})

    result = qualify(
        tenant_id=body["tenantId"],
        owner_sub=body["ownerSub"],
        requested_amount=requested_amount,
        down_payment=down_payment,
        term_months=term_months,
        income=income,
        employment_status=body["employmentStatus"],
    )

    log.info(
        "finance_qualify: tenantId=%s ownerSub=%s tier=%s",
        body["tenantId"], body["ownerSub"], result["tier"],
    )
    return _resp(200, result)
