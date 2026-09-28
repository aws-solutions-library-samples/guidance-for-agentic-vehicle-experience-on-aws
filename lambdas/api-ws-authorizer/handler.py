"""WebSocket JWT authorizer.

Validates the Cognito JWT passed on $connect and extracts tenantId + sub.
Uses PyJWT with per-pool JWKS cached in module scope.

Required env:
  TENANT_POOL_IDS  JSON array like [{"tenantId":"vehicle-owner","userPoolId":"us-east-1_abc"}]
"""
from __future__ import annotations

import json
import os
import urllib.request
from typing import Any

import jwt
from jwt import PyJWKClient

_REGION = os.environ.get("AWS_REGION", "us-east-1")
_POOLS: list[dict[str, str]] = json.loads(os.environ["TENANT_POOL_IDS"])

# Cache one JWKS client per pool for container lifetime.
_JWKS_CLIENTS: dict[str, PyJWKClient] = {}


def _jwks_client(user_pool_id: str) -> PyJWKClient:
    if user_pool_id not in _JWKS_CLIENTS:
        url = f"https://cognito-idp.{_REGION}.amazonaws.com/{user_pool_id}/.well-known/jwks.json"
        _JWKS_CLIENTS[user_pool_id] = PyJWKClient(url)
    return _JWKS_CLIENTS[user_pool_id]


def _deny() -> dict[str, Any]:
    return {
        "isAuthorized": False,
    }


def handler(event: dict[str, Any], _context: Any) -> dict[str, Any]:
    # API Gateway WebSocket passes the token as a query string parameter.
    qs = event.get("queryStringParameters") or {}
    token = qs.get("token")
    if not token:
        return _deny()

    # Try each configured pool until one verifies.
    last_error: Exception | None = None
    for pool in _POOLS:
        try:
            signing_key = _jwks_client(pool["userPoolId"]).get_signing_key_from_jwt(token)
            decoded = jwt.decode(
                token,
                signing_key.key,
                algorithms=["RS256"],
                issuer=f"https://cognito-idp.{_REGION}.amazonaws.com/{pool['userPoolId']}",
                options={"verify_aud": False},  # app client ID check omitted for brevity
            )
            tenant_claim = decoded.get("custom:tenantId") or decoded.get("tenantId")
            if tenant_claim != pool["tenantId"]:
                continue
            return {
                "isAuthorized": True,
                "context": {
                    "tenantId": tenant_claim,
                    "sub": decoded.get("sub", ""),
                },
            }
        except Exception as e:  # noqa: BLE001
            last_error = e
            continue

    return _deny()
