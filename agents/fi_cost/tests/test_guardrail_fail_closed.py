"""Tests — guardrail fail-closed on unknown action (T1.3 item 8, fi_cost).

``_apply_guardrail`` in fi_cost/agent.py blocks when action is not exactly "NONE".
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

# Evidence matches the valid fixture used in test_guardrail.py
_EVIDENCE = {
    "vin": "VIN-FC-COST-001",
    "fleet_id": "fleet-meridian-fc",
    "year_month": "2026-09",
    "cost.month": "2026-09",
    "ratio_cohort": 1.7,
    "ratio_own": 1.4,
    "severity": "soon",
    "energy.public_and_peak_kwh": 400.0,
    "energy.mean_public_peak_price_per_kwh": 0.38,
    "energy.depot_off_peak_price_per_kwh": 0.09,
    "energy.mean_price_per_kwh": 0.35,
    "energy.cohort_mean_price_per_kwh": 0.15,
    "energy.public_share": 0.75,
    "energy.cohort_public_share": 0.30,
    "energy.peak_share": 0.40,
    "energy.cohort_peak_share": 0.15,
    "energy.dc_fast_share": 0.60,
    "energy.cohort_dc_fast_share": 0.25,
    "energy.vehicle_kwh_per_100mi": 35.0,
    "energy.cohort_kwh_per_100mi": 25.0,
    "cost.maintenance": 200.0,
    "cost.lease": 600.0,
    "cost.insurance": 150.0,
    "cost.miles": 800.0,
    "cost.fleet_median_miles": 1000.0,
    "cost.cohort_baseline": 0.28,
    "cost.energy": 280.0,
    "delta.maintenance": 0.06,
    "delta.energy": 0.375,
    "delta.lease": 0.025,
    "delta.insurance": 0.0125,
    "delta.maintenance_usd": 48.0,
    "delta.energy_usd": 300.0,
    "delta.lease_usd": 20.0,
    "delta.insurance_usd": 10.0,
    "maintenance.max_repeat_count": 0,
}

_PII_HEADLINE = "COST-PII-HEADLINE-SECRET Driver Jane Doe high charging costs"
_PII_DETAIL = "COST-PII-DETAIL-SECRET Phone: 555-0199."

_CLEAN_HEADLINE = "High charging costs detected at public DC-fast stations"
_CLEAN_DETAIL = "Vehicle charges at public stations above fleet baseline; shift to depot."


def _valid_reply(headline: str = _CLEAN_HEADLINE, detail: str = _CLEAN_DETAIL) -> dict:
    return {
        "output": {"message": {"content": [{"toolUse": {
            "toolUseId": "t-fc-cost",
            "name": "record_diagnosis",
            "input": {
                "cause": "charging_rate",
                "action": "shift_to_depot_off_peak",
                "evidence_refs": ["energy.public_and_peak_kwh"],
                "headline": headline,
                "detail": detail,
                "confidence": "high",
            },
        }}]}},
        "usage": {"inputTokens": 80, "outputTokens": 40},
    }


def _run_cost_with_action(guardrail_action: str) -> dict:
    from agents.fi_cost.agent import process_vehicle
    stub = MagicMock()
    stub.converse.return_value = _valid_reply(_PII_HEADLINE, _PII_DETAIL)
    stub.apply_guardrail.return_value = {"action": guardrail_action}
    written: list[dict] = []
    process_vehicle(_EVIDENCE, None, stub, lambda f: written.append(f))
    return written[0] if written else {}


def _has_rules_fallback(finding: dict) -> bool:
    return (
        any(
            e.get("kind") == "cost.diagnosis_source" and e.get("value") == "rules"
            for e in finding.get("evidence", [])
        )
        or finding.get("confidence") == "low"
    )


def test_unknown_action_blocks_model_text_cost():
    """Unknown guardrail action must block in fi_cost/agent.py — falls back to rules."""
    finding = _run_cost_with_action("GUARDRAIL_UNKNOWN")
    assert finding, "write_finding must be called (rules fallback)"
    assert _has_rules_fallback(finding), (
        "Unknown action must trigger rules fallback"
    )
    assert _PII_HEADLINE not in json.dumps(finding)


def test_empty_string_action_blocks_cost():
    """Empty string action must block in fi_cost."""
    finding = _run_cost_with_action("")
    assert finding
    assert _has_rules_fallback(finding), "Empty string action must trigger rules fallback"


def test_missing_action_key_blocks_cost():
    """Missing 'action' key → None → must block in fi_cost."""
    from agents.fi_cost.agent import process_vehicle
    stub = MagicMock()
    stub.converse.return_value = _valid_reply(_PII_HEADLINE, _PII_DETAIL)
    stub.apply_guardrail.return_value = {}  # no 'action' key
    written: list[dict] = []
    process_vehicle(_EVIDENCE, None, stub, lambda f: written.append(f))
    assert written
    assert _has_rules_fallback(written[0]), "Missing action key must trigger rules fallback"
