"""API: POST /triage

Proxies to the deterministic triage classifier Lambda after enforcing that the
caller's JWT tenantId claim matches the request body tenantId.
"""
from __future__ import annotations

import json
import os
from typing import Any

import boto3

_lambda = boto3.client("lambda")
_CLASSIFIER_ARN = os.environ["CLASSIFIER_FUNCTION_ARN"]


def _response(status: int, body: dict[str, Any]) -> dict[str, Any]:
    return {
        "statusCode": status,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": os.environ.get("ALLOWED_ORIGIN", "*"),
        },
        "body": json.dumps(body),
    }


def handler(event: dict[str, Any], _context: Any) -> dict[str, Any]:
    claims = (event.get("requestContext") or {}).get("authorizer", {}).get("claims") or {}
    caller_tenant = claims.get("custom:tenantId") or claims.get("tenantId")
    if not caller_tenant:
        return _response(401, {"error": "missing tenantId claim"})

    try:
        body = json.loads(event.get("body") or "{}")
    except json.JSONDecodeError:
        return _response(400, {"error": "invalid JSON"})

    requested_tenant = body.get("tenantId")
    if requested_tenant and requested_tenant != caller_tenant:
        return _response(403, {"error": "tenant mismatch"})
    body["tenantId"] = caller_tenant

    if not body.get("vin"):
        return _response(400, {"error": "vin required"})

    resp = _lambda.invoke(
        FunctionName=_CLASSIFIER_ARN,
        InvocationType="RequestResponse",
        Payload=json.dumps(body).encode(),
    )
    payload = json.loads(resp["Payload"].read() or b"{}")
    if payload.get("errorType"):
        return _response(500, {"error": "classifier failed", "detail": payload.get("errorMessage")})
    return _response(200, payload)
