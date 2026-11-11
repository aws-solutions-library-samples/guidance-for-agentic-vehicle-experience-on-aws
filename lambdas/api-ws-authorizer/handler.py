"""WebSocket JWT authorizer.

Validates the Cognito JWT passed on $connect and extracts tenantId + sub.
Uses PyJWT with per-pool JWKS cached in module scope.

Response shape. A WebSocket API's $connect authorizer is a REQUEST authorizer and
must return an IAM policy (principalId + policyDocument, optional context map of
plain values); the `{"isAuthorized": ...}` "simple response" exists only for HTTP
APIs. Until 2026-10-01 this handler returned the simple shape, so API Gateway
answered 500 for every token, valid or not
(issues/2026-10-01-ws-authorizer-returns-http-api-response-shape/). See
https://docs.aws.amazon.com/apigateway/latest/developerguide/apigateway-websocket-api-lambda-auth.html

  * Allow: `execute-api:Invoke` on exactly this request's `methodArn`, nothing wider.
  * Deny: an explicit Deny on every failure, so the client gets 403, not 500.

Audience. A token is accepted only if its `aud` (the Cognito app client id) is one of
the pool's `clientIds`. A pool can have several app clients (the meridian-fleet pool also
has a kiosk client); without this check a token from any of them opened a WebSocket
(issues/2026-10-01-ws-authorizer-accepts-any-app-client/). A pool entry with no
`clientIds` accepts nothing: a missing allowlist fails closed.

Required env:
  TENANT_POOL_IDS  JSON array like
                   [{"tenantId":"vehicle-owner","userPoolId":"us-east-1_abc","clientIds":["abc123"]}]
"""
from __future__ import annotations

import json
import os
from typing import Any

import jwt
from jwt import PyJWKClient

_REGION = os.environ.get("AWS_REGION", "us-east-1")
# Each entry: {"tenantId": str, "userPoolId": str, "clientIds": [str, ...]}.
_POOLS: list[dict[str, Any]] = json.loads(os.environ["TENANT_POOL_IDS"])

# Cache one JWKS client per pool for container lifetime.
_JWKS_CLIENTS: dict[str, PyJWKClient] = {}

# Seconds to wait for a pool's JWKS. PyJWKClient's default is 30, longer than this
# Lambda's 5 s timeout, so a stalled fetch would end in a Lambda timeout and a 500
# instead of a Deny.
_JWKS_TIMEOUT_SECONDS = 2

# principalId for a denied request. Never a value taken from the token, because a
# denied token's claims are unverified.
_DENIED_PRINCIPAL = "unauthorized"


def _jwks_client(user_pool_id: str) -> PyJWKClient:
    if user_pool_id not in _JWKS_CLIENTS:
        url = f"https://cognito-idp.{_REGION}.amazonaws.com/{user_pool_id}/.well-known/jwks.json"
        _JWKS_CLIENTS[user_pool_id] = PyJWKClient(url, timeout=_JWKS_TIMEOUT_SECONDS)
    return _JWKS_CLIENTS[user_pool_id]


def _policy(principal_id: str, effect: str, resource: str) -> dict[str, Any]:
    return {
        "principalId": principal_id,
        "policyDocument": {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Action": "execute-api:Invoke",
                    "Effect": effect,
                    "Resource": resource,
                }
            ],
        },
    }


def _deny(event: dict[str, Any]) -> dict[str, Any]:
    # Deny the request's own route when it is known. Without a methodArn there is no
    # specific route to name, and denying everything is the safe fallback.
    return _policy(_DENIED_PRINCIPAL, "Deny", event.get("methodArn") or "*")


def _allowed_clients(pool: dict[str, Any]) -> list[str]:
    """The pool's accepted app client ids, or [] (accept nothing) if not configured."""
    ids = pool.get("clientIds")
    if not isinstance(ids, list):
        return []
    return [i for i in ids if isinstance(i, str) and i]


# Say once, at cold start, which pools accept nothing, so a missing allowlist shows up
# in the logs instead of only as denied connections.
for _p in _POOLS:
    if not _allowed_clients(_p):
        print(f"ws-authorizer: pool {_p.get('userPoolId')!r} has no clientIds; it accepts no tokens")


def _allow(method_arn: str, tenant_id: str, sub: str) -> dict[str, Any]:
    response = _policy(sub, "Allow", method_arn)
    # Context values must be plain strings, numbers or booleans; strings here.
    response["context"] = {"tenantId": tenant_id, "sub": sub}
    return response


def handler(event: dict[str, Any], _context: Any) -> dict[str, Any]:
    method_arn = event.get("methodArn")
    if not isinstance(method_arn, str) or not method_arn:
        return _deny(event)

    # API Gateway WebSocket passes the token as a query string parameter.
    qs = event.get("queryStringParameters") or {}
    token = qs.get("token")
    if not token:
        return _deny(event)

    # Try each configured pool until one verifies.
    for pool in _POOLS:
        clients = _allowed_clients(pool)
        if not clients:
            continue
        try:
            # A malformed entry (no userPoolId) raises here and is skipped like any other
            # verification failure (security review 2026-10-01, S2).
            signing_key = _jwks_client(pool["userPoolId"]).get_signing_key_from_jwt(token)
            decoded = jwt.decode(
                token,
                signing_key.key,
                algorithms=["RS256"],
                issuer=f"https://cognito-idp.{_REGION}.amazonaws.com/{pool['userPoolId']}",
                audience=clients,
                # aud must be present and one of this pool's accepted app clients; exp,
                # iss and sub must be present, not just valid when present.
                options={"verify_aud": True, "require": ["exp", "iss", "sub", "aud"]},
            )
        except Exception:  # noqa: BLE001 - any verification failure is a deny
            continue
        # Cognito issues an ID token and an access token from one sign-in. Only the ID
        # token carries the tenant attribute; accept nothing else.
        if decoded.get("token_use") != "id":
            continue
        # Cognito puts one app client id in an ID token's aud. PyJWT accepts a list aud
        # if ANY entry is allowed, so a list (e.g. [app, kiosk]) is refused here
        # (security review 2026-10-01, S3).
        if not isinstance(decoded.get("aud"), str) or decoded["aud"] not in clients:
            continue
        # Only the Cognito custom attribute. A bare `tenantId` claim was also accepted
        # until 2026-10-01; no pool has a pre-token trigger that could add one, so it
        # was dead code that would turn into a bypass if a trigger or client ever
        # emitted it (security review, S2).
        tenant_claim = decoded.get("custom:tenantId")
        sub = decoded.get("sub")
        if not isinstance(pool.get("tenantId"), str) or tenant_claim != pool["tenantId"]:
            continue
        if not isinstance(sub, str) or not sub:
            continue
        return _allow(method_arn, str(tenant_claim), sub)

    return _deny(event)
