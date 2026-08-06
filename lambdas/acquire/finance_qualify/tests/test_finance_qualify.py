"""Tests for lambdas/acquire/finance_qualify/handler.py

Run:
    python3 -m pytest lambdas/acquire/finance_qualify/ -v
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Load handler by file path so the module name is unique per Lambda and
# never collides with sibling Lambda handlers in the same pytest session.
# ---------------------------------------------------------------------------
_HANDLER_PATH = Path(__file__).parent.parent / "handler.py"
_spec = importlib.util.spec_from_file_location("finance_qualify_handler", _HANDLER_PATH)
fq = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fq)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _event(body: dict) -> dict:
    return {"body": json.dumps(body)}


_BASE = {
    "tenantId": "genericmoto-demo",
    "ownerSub": "a1b2c3d4-0000-0000-0000-000000000001",
    "requestedFinancingAmount": 10000.0,
    "downPayment": 2000.0,
    "term_months": 48,
    "income": 120000.0,
    "employmentStatus": "employed",
}


# ---------------------------------------------------------------------------
# Unit tests for pure qualify() function
# ---------------------------------------------------------------------------

class TestQualifyLogic:
    def test_prime_tier_high_income_high_down(self):
        result = fq.qualify(
            tenant_id="t1", owner_sub="s1",
            requested_amount=10000, down_payment=2500,   # 25% → ≥20%
            term_months=48, income=120000, employment_status="employed",
        )
        assert result["tier"] == "prime"
        assert result["apr"] == 4.9
        assert result["approvedAmount"] == 10000.0
        assert "escalationReason" not in result

    def test_near_prime_tier(self):
        result = fq.qualify(
            tenant_id="t1", owner_sub="s1",
            requested_amount=10000, down_payment=1100,   # 11% → ≥10%
            term_months=48, income=70000, employment_status="employed",
        )
        assert result["tier"] == "near-prime"
        assert result["apr"] == 7.9

    def test_subprime_tier(self):
        result = fq.qualify(
            tenant_id="t1", owner_sub="s1",
            requested_amount=10000, down_payment=500,    # 5% → <10%
            term_months=48, income=45000, employment_status="employed",
        )
        assert result["tier"] == "subprime"
        assert result["approvedAmount"] == 8000.0   # 80% of 10 000
        assert result["apr"] == 12.9

    def test_declined_tier(self):
        result = fq.qualify(
            tenant_id="t1", owner_sub="s1",
            requested_amount=10000, down_payment=500,
            term_months=48, income=30000, employment_status="part-time",
        )
        assert result["tier"] == "declined"
        assert result["approvedAmount"] == 0.0
        assert result["escalationReason"] == "Below-threshold income"

    def test_determinism_same_inputs(self):
        """Same inputs → same tier and amounts every call."""
        a = fq.qualify("t1", "s1", 10000, 2500, 48, 120000, "employed")
        b = fq.qualify("t1", "s1", 10000, 2500, 48, 120000, "employed")
        assert a == b

    def test_determinism_different_ownerSub(self):
        """Different ownerSub but same financial profile → same tier (income-driven)."""
        a = fq.qualify("t1", "s1", 10000, 2500, 48, 120000, "employed")
        b = fq.qualify("t1", "s2", 10000, 2500, 48, 120000, "employed")
        assert a["tier"] == b["tier"]

    def test_disclosure_contains_tenant_id(self):
        result = fq.qualify("acme-motors", "s1", 10000, 2500, 48, 120000, "employed")
        assert "acme-motors" in result["disclosure"]

    def test_monthly_payment_non_zero_for_prime(self):
        result = fq.qualify("t1", "s1", 10000, 2500, 48, 120000, "employed")
        assert result["monthly"] > 0

    def test_monthly_payment_zero_for_declined(self):
        result = fq.qualify("t1", "s1", 10000, 0, 48, 20000, "unemployed")
        assert result["monthly"] == 0.0


# ---------------------------------------------------------------------------
# Handler (HTTP) tests
# ---------------------------------------------------------------------------

class TestHandler:
    def test_happy_path_prime(self):
        resp = fq.handler(_event(_BASE), None)
        assert resp["statusCode"] == 200
        body = json.loads(resp["body"])
        assert body["tier"] == "prime"
        assert "disclosure" in body
        assert "monthly" in body

    def test_missing_required_field(self):
        bad = {k: v for k, v in _BASE.items() if k != "income"}
        resp = fq.handler(_event(bad), None)
        assert resp["statusCode"] == 400
        body = json.loads(resp["body"])
        assert "income" in body["error"]

    def test_invalid_json_body(self):
        resp = fq.handler({"body": "NOT JSON"}, None)
        assert resp["statusCode"] == 400

    def test_zero_requested_amount_rejected(self):
        bad = {**_BASE, "requestedFinancingAmount": 0}
        resp = fq.handler(_event(bad), None)
        assert resp["statusCode"] == 400

    def test_non_numeric_income_rejected(self):
        bad = {**_BASE, "income": "lots"}
        resp = fq.handler(_event(bad), None)
        assert resp["statusCode"] == 400

    def test_determinism_via_http_same_input(self):
        """Same HTTP request body → same response body (tier, amounts)."""
        r1 = json.loads(fq.handler(_event(_BASE), None)["body"])
        r2 = json.loads(fq.handler(_event(_BASE), None)["body"])
        assert r1["tier"] == r2["tier"]
        assert r1["approvedAmount"] == r2["approvedAmount"]
        assert r1["monthly"] == r2["monthly"]

    def test_declined_has_escalation_reason(self):
        low_income = {**_BASE, "income": 20000, "downPayment": 100}
        resp = fq.handler(_event(low_income), None)
        body = json.loads(resp["body"])
        assert body["tier"] == "declined"
        assert "escalationReason" in body
