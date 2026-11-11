"""API: POST /triage

Proxies to the deterministic triage classifier Lambda after resolving the
caller's tenant and enforcing that the request body names the same tenant.

Tenant resolution (issues/2026-09-29-web-chat-triage-401-claimless-callers/):

1. The caller's JWT ``custom:tenantId`` (or ``tenantId``) claim, when present.
2. Otherwise ``DEFAULT_TENANT_ID``, the same value the ``/assistant/chat``
   Lambda uses for the same caller. Most CMS web-chat users have no tenant
   claim (the CMS pool's ``custom:tenantId`` is immutable and Federate users
   are created without it), and before this fallback the chat Lambda served
   them as the default tenant while this endpoint refused them with 401, so
   every web-chat triage failed.
3. Neither: 401.

A body ``tenantId`` that differs from the resolved tenant is refused with 403
in both cases, so a claimless caller cannot choose another tenant's rules.
The classifier uses the tenant only to select triage rules; telemetry, DTCs
and recalls are read by VIN.
"""
from __future__ import annotations

import json
import logging
import os
import re
from typing import Any

import boto3
import botocore.config

# Unique module name: a top-level `vehicle_scope` already exists in
# lambdas/api-assistant-chat, and the two collide in a single pytest process.
import triage_vehicle_scope as _vehicle_scope

log = logging.getLogger()
log.setLevel(logging.INFO)

_lambda = boto3.client("lambda")
_CLASSIFIER_ARN = os.environ["CLASSIFIER_FUNCTION_ARN"]

# DynamoDB client for vehicle scope checks (fail-closed: short timeouts so a
# slow table does not stall the 15s Lambda budget).
_DDB_CONFIG = botocore.config.Config(
    connect_timeout=1,
    read_timeout=1,
    retries={"total_max_attempts": 2},
)
_ddb: boto3.client = None  # type: ignore[assignment]


def _get_ddb():
    global _ddb
    if _ddb is None:
        _ddb = boto3.client("dynamodb", config=_DDB_CONFIG)
    return _ddb

_LOG_UNSAFE = re.compile(r"[^A-Za-z0-9._:@-]")


def _log_safe(value: Any, limit: int = 64) -> str:
    """Client-sent value made safe for a log line: no newlines or other
    control characters, so a caller cannot forge a log entry."""
    return _LOG_UNSAFE.sub("?", str(value))[:limit]


def _response(status: int, body: dict[str, Any]) -> dict[str, Any]:
    return {
        "statusCode": status,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": os.environ.get("ALLOWED_ORIGIN", "*"),
        },
        "body": json.dumps(body),
    }


def resolve_tenant(claims: dict[str, Any]) -> tuple[str | None, str]:
    """Return ``(tenant_id, source)`` for the caller.

    ``source`` is ``"claim"``, ``"default"`` or ``"none"``. The env var is read
    at call time so tests can set it per case.
    """
    claim = str(claims.get("custom:tenantId") or claims.get("tenantId") or "").strip()
    if claim:
        return claim, "claim"
    default = os.environ.get("DEFAULT_TENANT_ID", "").strip()
    if default:
        return default, "default"
    return None, "none"


def handler(event: dict[str, Any], _context: Any) -> dict[str, Any]:
    claims = (event.get("requestContext") or {}).get("authorizer", {}).get("claims") or {}
    sub = claims.get("sub", "")
    caller_tenant, tenant_source = resolve_tenant(claims)
    if not caller_tenant:
        log.warning("triage: refused 401 reason=no tenantId claim and no DEFAULT_TENANT_ID sub=%s", sub)
        return _response(401, {"error": "missing tenantId claim"})

    try:
        body = json.loads(event.get("body") or "{}")
    except json.JSONDecodeError:
        log.warning("triage: refused 400 reason=invalid JSON sub=%s", sub)
        return _response(400, {"error": "invalid JSON"})

    requested_tenant = body.get("tenantId")
    if requested_tenant and requested_tenant != caller_tenant:
        log.warning(
            "triage: refused 403 reason=tenant mismatch sub=%s tenant=%s source=%s requested=%s",
            sub, caller_tenant, tenant_source, _log_safe(requested_tenant),
        )
        return _response(403, {"error": "tenant mismatch"})
    body["tenantId"] = caller_tenant

    if not body.get("vin"):
        log.warning("triage: refused 400 reason=vin required sub=%s", sub)
        return _response(400, {"error": "vin required"})

    # DS-1: enforce vehicle scope before forwarding to the classifier.
    # A vehicle-bound or fleet-bound caller may only triage their own vehicle.
    # The check runs for every caller; it is a no-op (UNCHANGED) for a caller
    # with no scope claim, and fails closed for a scoped one if the table name
    # is missing.
    _cms_table = os.environ.get("CMS_VEHICLES_TABLE", "")
    _scope = _vehicle_scope.check(claims, body["vin"], _get_ddb(), _cms_table)
    if _scope.status == _vehicle_scope.STATUS_DENY_MALFORMED_CLAIM:
        log.warning("triage: refused 403 reason=malformed_vehicle_claim sub=%s", sub)
        return _response(403, {"error": "vehicle not in caller scope"})
    if _scope.status in (
        _vehicle_scope.STATUS_DENY_VIN_MISMATCH,
        _vehicle_scope.STATUS_DENY_NOT_IN_FLEET,
    ):
        log.warning(
            "triage: refused 403 reason=vehicle_scope status=%s sub=%s",
            _scope.status, sub,
        )
        return _response(403, {"error": "vehicle not in caller scope"})
    if _scope.status == _vehicle_scope.STATUS_ERROR:
        log.error("triage: refused 503 reason=vehicle_scope_unavailable sub=%s detail=%s", sub, _scope.detail)
        return _response(503, {"error": "vehicle scope unavailable"})
    if _scope.status == _vehicle_scope.STATUS_PASS and _scope.vin_of_record:
        body["vin"] = _scope.vin_of_record
    if not _scope.ok:
        # Any status this handler does not know refuses (fail closed).
        log.warning("triage: refused 403 reason=vehicle_scope status=%s sub=%s", _scope.status, sub)
        return _response(403, {"error": "vehicle not in caller scope"})

    log.info(
        "triage: sub=%s tenant=%s source=%s vin=%s",
        sub, caller_tenant, tenant_source, _log_safe(body.get("vin"), 32),
    )
    resp = _lambda.invoke(
        FunctionName=_CLASSIFIER_ARN,
        InvocationType="RequestResponse",
        Payload=json.dumps(body).encode(),
    )
    payload = json.loads(resp["Payload"].read() or b"{}")
    if payload.get("errorType"):
        log.error("triage: classifier failed sub=%s errorType=%s", sub, payload.get("errorType"))
        return _response(500, {"error": "classifier failed", "detail": payload.get("errorMessage")})
    return _response(200, payload)
