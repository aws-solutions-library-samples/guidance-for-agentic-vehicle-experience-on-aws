"""Tenant config client — reads vsa-prod-tenant-config, caches for 60s, maps
DynamoDB item shape into typed TenantTriageConfig used by the classifier.

See VSA_TENANT_CONFIG.md for schema semantics.
"""
from __future__ import annotations

import os
import time
from decimal import Decimal
from typing import Any

import boto3

from triage_classifier.types import (
    TenantEscalationRule,
    TenantThresholds,
    TenantTriageConfig,
)

_TTL_SECONDS = 60
_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}

_table = None


def _get_table():  # lazy so unit tests can mock or skip
    global _table
    if _table is None:
        _table = boto3.resource(
            "dynamodb", region_name=os.environ.get("AWS_REGION", "us-east-1")
        ).Table(os.environ["VSA_TENANT_CONFIG_TABLE"])
    return _table


def _to_float(v: Any) -> float:
    return float(v) if isinstance(v, (int, float, Decimal)) else float(v)


def _load_raw(tenant_id: str) -> dict[str, Any]:
    t = _get_table()
    pointer = t.get_item(Key={"tenantId": tenant_id, "version": "current"}).get("Item")
    if not pointer:
        raise KeyError(f"No active config for tenant {tenant_id}")
    item = t.get_item(Key={"tenantId": tenant_id, "version": pointer["activeVersion"]}).get("Item")
    if not item:
        raise KeyError(f"Version {pointer['activeVersion']} missing for tenant {tenant_id}")
    return item


def load_raw(tenant_id: str) -> dict[str, Any]:
    now = time.time()
    cached = _CACHE.get(tenant_id)
    if cached and now - cached[0] < _TTL_SECONDS:
        return cached[1]
    item = _load_raw(tenant_id)
    _CACHE[tenant_id] = (now, item)
    return item


def build_triage_config(raw: dict[str, Any]) -> TenantTriageConfig:
    triage = raw.get("triage") or {}
    overrides = triage.get("policyOverrides") or {}
    t = overrides.get("thresholds") or {}
    thresholds = TenantThresholds(
        coolant_temp_p2_c=_to_float(t.get("coolantTempP2C", 105)),
        coolant_temp_p1_c=_to_float(t.get("coolantTempP1C", 115)),
        coolant_temp_p0_c=_to_float(t.get("coolantTempP0C", 125)),
        tire_pressure_p2_delta_psi=_to_float(t.get("tirePressureP2Delta", 4)),
        tire_pressure_p1_delta_psi=_to_float(t.get("tirePressureP1Delta", 8)),
        brake_pad_wear_p2_mm=_to_float(t.get("brakePadWearP2Mm", 3)),
        brake_pad_wear_p1_mm=_to_float(t.get("brakePadWearP1Mm", 1.5)),
    )
    rules = tuple(
        TenantEscalationRule(
            when=r["when"],
            action=r["action"],
            reason=r.get("reason", ""),
            rule_id=r.get("ruleId") or f"{raw['tenantId']}-{i}",
        )
        for i, r in enumerate(overrides.get("escalations") or [])
    )
    return TenantTriageConfig(
        thresholds=thresholds,
        escalations=rules,
        predictive_weight=triage.get("predictiveMaintenanceWeight", "none"),
    )
