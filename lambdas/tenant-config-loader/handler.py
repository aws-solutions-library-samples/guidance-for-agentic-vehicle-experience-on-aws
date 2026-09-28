"""Tenant config loader Lambda.

Resolves tenantId -> active tenant config by reading the pointer table and
then fetching the versioned config row. Simple TTL cache in module scope keeps
hot tenants fast without needing ElastiCache for Phase 0.
"""
from __future__ import annotations

import os
import time
from typing import Any

import boto3

_ddb = boto3.resource("dynamodb")
_config_table = _ddb.Table(os.environ["TENANT_CONFIG_TABLE"])

_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_TTL_SECONDS = 60


def _get(tenant_id: str) -> dict[str, Any]:
    # Pointer row: tenantId + version="current" stores {activeVersion: "x.y.z"}
    pointer = _config_table.get_item(Key={"tenantId": tenant_id, "version": "current"}).get("Item")
    if not pointer:
        raise KeyError(f"No active config for tenant {tenant_id}")
    active_version = pointer["activeVersion"]
    item = _config_table.get_item(Key={"tenantId": tenant_id, "version": active_version}).get("Item")
    if not item:
        raise KeyError(f"Active version {active_version} missing for tenant {tenant_id}")
    return item


def handler(event: dict[str, Any], _context: Any) -> dict[str, Any]:
    tenant_id = event.get("tenantId")
    if not tenant_id:
        return {"statusCode": 400, "error": "tenantId required"}

    now = time.time()
    cached = _CACHE.get(tenant_id)
    if cached and now - cached[0] < _TTL_SECONDS:
        return {"statusCode": 200, "config": cached[1], "cacheHit": True}

    try:
        config = _get(tenant_id)
    except KeyError as e:
        return {"statusCode": 404, "error": str(e)}

    _CACHE[tenant_id] = (now, config)
    return {"statusCode": 200, "config": config, "cacheHit": False}
