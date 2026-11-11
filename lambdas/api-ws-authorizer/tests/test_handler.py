"""The WebSocket $connect authorizer: what it allows, what it denies, and how wide.

First tests for this handler (2026-10-01). Until then it returned the HTTP-API
`{"isAuthorized": ...}` shape, which a WebSocket REQUEST authorizer cannot use, so API
Gateway answered 500 for every token
(issues/2026-10-01-ws-authorizer-returns-http-api-response-shape/).

Tokens are signed with real RSA keys and verified by real PyJWT. Only the JWKS fetch is
replaced, with a client that serves this file's public keys by `kid`. The live behaviour
against Cognito and API Gateway is covered by scripts/live-verify-ws-authorizer.py.

Run: python3 -m pytest lambdas/api-ws-authorizer/tests/test_handler.py -v

Mutations verified 2026-10-01 against handler.py (each fails at least one test here):
  H1  Allow on "*".
  H2  Allow on the whole API (`<api>/*`) instead of this route.
  H3  The failure path returns Allow.
  H4  Tenant check deleted.
  H5  A verification exception falls through to Allow.
  H6  Expiry not checked.
  H7  Issuer not checked.
  H8  Missing `sub` accepted.
  H9  Missing methodArn guard deleted.
  H10 principalId is not the token's sub.
  H11 Context carries an extra, non-string value.
  H12 token_use not checked (an access token accepted).
  H13 exp / iss / sub not required to be present.
  H14 JWKS fetch keeps PyJWKClient's 30 s default timeout.
  H15 An empty methodArn accepted.
  H16 A bare `tenantId` claim accepted.
  H17 HS256 added to the accepted algorithms.
  A1  aud not verified.               A2 no audience passed to PyJWT.
  A3  empty-allowlist guard removed.  A5 audience = every pool's clients.
  A6  aud not required (caught only by the spy test; PyJWT raises anyway).
  A7  a bare-string clientIds accepted.
  A8  an unconfigured pool ends the loop with a Deny.
  A9  non-string clientIds entries accepted.
  A10 a list aud accepted when one entry is allowed.
  A11 a malformed pool entry raises instead of being skipped.
"""
from __future__ import annotations

import importlib.util
import json
import time
from pathlib import Path

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

_HANDLER = Path(__file__).resolve().parents[1] / "handler.py"

REGION = "us-west-2"
# Fixture pool ids are deliberately shorter than a real one: the publish scanner flags
# `<region>_<8+ chars>` as a hardcoded pool id (review, 2026-10-01).
POOL_A = "us-west-2_TestA"
POOL_B = "us-west-2_TestB"
TENANT_A = "meridian-fleet"
TENANT_B = "other-tenant"
# App client ids (the token's `aud`). Pool A also has a kiosk client, which is NOT allowed.
CLIENT_A = "appclienta1"
KIOSK_A = "kioskclienta"
CLIENT_B = "appclientb1"
POOLS = [
    {"tenantId": TENANT_A, "userPoolId": POOL_A, "clientIds": [CLIENT_A]},
    {"tenantId": TENANT_B, "userPoolId": POOL_B, "clientIds": [CLIENT_B]},
]
METHOD_ARN = "arn:aws:execute-api:us-west-2:123456789012:abcdef1234/staging/$connect"
SUB = "11111111-2222-3333-4444-555555555555"


def _rsa_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


KEY_A = _rsa_key()
KEY_B = _rsa_key()
KEY_ROGUE = _rsa_key()
KIDS = {"kid-a": KEY_A, "kid-b": KEY_B}


class _FakeJwksClient:
    """Serves this file's public keys by kid, as PyJWKClient does from Cognito."""

    def __init__(self, keys: dict):
        self._keys = keys

    def get_signing_key_from_jwt(self, token: str):
        kid = jwt.get_unverified_header(token).get("kid")
        if kid not in self._keys:
            raise jwt.PyJWKClientError(f"no key for kid {kid!r}")
        jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(self._keys[kid].public_key()))
        jwk.update({"kid": kid, "alg": "RS256", "use": "sig"})
        return jwt.PyJWK.from_dict(jwk)


def _load(monkeypatch):
    monkeypatch.setenv("AWS_REGION", REGION)
    monkeypatch.setenv("TENANT_POOL_IDS", json.dumps(POOLS))
    spec = importlib.util.spec_from_file_location("_ws_authorizer_under_test", _HANDLER)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


@pytest.fixture
def mod(monkeypatch):
    """A fresh handler module per test, with this file's pools and keys."""
    m = _load(monkeypatch)
    clients = {POOL_A: _FakeJwksClient({"kid-a": KEY_A}), POOL_B: _FakeJwksClient({"kid-b": KEY_B})}
    monkeypatch.setattr(m, "_jwks_client", lambda pool_id: clients[pool_id])
    return m


def _token(*, key=KEY_A, kid="kid-a", pool=POOL_A, tenant=TENANT_A, sub=SUB, aud=CLIENT_A,
           exp_offset=3600, alg="RS256", drop=(), extra=None) -> str:
    now = int(time.time())
    claims = {
        "sub": sub,
        "aud": aud,
        "iss": f"https://cognito-idp.{REGION}.amazonaws.com/{pool}",
        "token_use": "id",
        "custom:tenantId": tenant,
        "iat": now,
        "exp": now + exp_offset,
    }
    claims.update(extra or {})
    for name in drop:
        claims.pop(name, None)
    return jwt.encode(claims, key, algorithm=alg, headers={"kid": kid})


def _event(token: str | None, method_arn: str | None = METHOD_ARN) -> dict:
    event: dict = {"type": "REQUEST"}
    if method_arn is not None:
        event["methodArn"] = method_arn
    if token is not None:
        event["queryStringParameters"] = {"token": token}
    return event


def _statement(response: dict) -> dict:
    statements = response["policyDocument"]["Statement"]
    assert len(statements) == 1, statements
    return statements[0]


def _assert_deny(response: dict, resource: str = METHOD_ARN) -> None:
    assert set(response) == {"principalId", "policyDocument"}, response
    assert response["principalId"] == "unauthorized"
    assert response["policyDocument"]["Version"] == "2012-10-17"
    s = _statement(response)
    assert s == {"Action": "execute-api:Invoke", "Effect": "Deny", "Resource": resource}


# ---------- Allow -----------------------------------------------------------------


def test_a_valid_token_allows_exactly_this_route(mod):
    response = mod.handler(_event(_token()), None)

    assert set(response) == {"principalId", "policyDocument", "context"}, response
    assert response["principalId"] == SUB
    assert response["policyDocument"]["Version"] == "2012-10-17"
    assert _statement(response) == {
        "Action": "execute-api:Invoke",
        "Effect": "Allow",
        "Resource": METHOD_ARN,
    }
    assert response["context"] == {"tenantId": TENANT_A, "sub": SUB}
    assert all(type(v) is str for v in response["context"].values())


def test_the_allow_resource_is_the_request_methodarn_not_a_constant(mod):
    other = "arn:aws:execute-api:us-west-2:123456789012:zzzz999999/prod/$connect"
    assert _statement(mod.handler(_event(_token(), method_arn=other), None))["Resource"] == other


def test_a_token_from_the_second_pool_gets_that_pools_tenant(mod):
    token = _token(key=KEY_B, kid="kid-b", pool=POOL_B, tenant=TENANT_B, aud=CLIENT_B)
    response = mod.handler(_event(token), None)
    assert _statement(response)["Effect"] == "Allow"
    assert response["context"]["tenantId"] == TENANT_B


def test_a_plain_tenantid_claim_is_not_accepted(mod):
    """Only `custom:tenantId` counts; Cognito cannot emit a bare `tenantId` without a
    pre-token trigger, and none is configured (security review 2026-10-01, S2)."""
    token = _token(drop=("custom:tenantId",), extra={"tenantId": TENANT_A})
    _assert_deny(mod.handler(_event(token), None))


def test_only_rs256_is_accepted(mod, monkeypatch):
    """Pin the algorithm list itself, not just PyJWT's current refusal to use an RSA key
    as an HMAC secret (security review 2026-10-01, S1)."""
    seen = []
    real_decode = mod.jwt.decode

    def _spy(*args, **kwargs):
        seen.append(kwargs.get("algorithms"))
        return real_decode(*args, **kwargs)

    monkeypatch.setattr(mod.jwt, "decode", _spy)
    assert _statement(mod.handler(_event(_token()), None))["Effect"] == "Allow"
    assert seen == [["RS256"]]


# ---------- Deny ------------------------------------------------------------------


@pytest.mark.parametrize("label, token", [
    ("wrong tenant for the pool", lambda: _token(tenant=TENANT_B)),
    ("no tenant claim", lambda: _token(drop=("custom:tenantId",))),
    ("pool B token claiming pool A's tenant", lambda: _token(key=KEY_B, kid="kid-b", pool=POOL_B, tenant=TENANT_A, aud=CLIENT_B)),
    ("another app client of the same pool (kiosk)", lambda: _token(aud=KIOSK_A)),
    ("pool B's app client on a pool A token", lambda: _token(aud=CLIENT_B)),
    ("no aud", lambda: _token(drop=("aud",))),
    ("empty aud", lambda: _token(aud="")),
    ("a list aud naming the allowed client and the kiosk", lambda: _token(aud=[CLIENT_A, KIOSK_A])),
    ("a list aud naming only the allowed client", lambda: _token(aud=[CLIENT_A])),
    ("expired", lambda: _token(exp_offset=-60)),
    ("issuer of an unconfigured pool", lambda: _token(pool="us-west-2_Nope")),
    ("issuer in another region", lambda: _token(extra={"iss": f"https://cognito-idp.us-east-1.amazonaws.com/{POOL_A}"})),
    ("signed by an unknown key under a known kid", lambda: _token(key=KEY_ROGUE)),
    ("unknown kid", lambda: _token(key=KEY_ROGUE, kid="kid-rogue")),
    ("no sub", lambda: _token(drop=("sub",))),
    ("no exp", lambda: _token(drop=("exp",))),
    ("no iss", lambda: _token(drop=("iss",))),
    ("an access token", lambda: _token(extra={"token_use": "access"})),
    ("no token_use", lambda: _token(drop=("token_use",))),
    ("empty sub", lambda: _token(sub="")),
    ("HS256 signed with a shared secret", lambda: jwt.encode(
        {"sub": SUB, "custom:tenantId": TENANT_A, "exp": int(time.time()) + 3600,
         "iss": f"https://cognito-idp.{REGION}.amazonaws.com/{POOL_A}"},
        "secret-secret-secret-secret-secret!", algorithm="HS256", headers={"kid": "kid-a"})),
    ("alg none", lambda: jwt.encode(
        {"sub": SUB, "custom:tenantId": TENANT_A, "exp": int(time.time()) + 3600,
         "iss": f"https://cognito-idp.{REGION}.amazonaws.com/{POOL_A}"},
        None, algorithm="none", headers={"kid": "kid-a"})),
    ("malformed", lambda: "not.a.jwt"),
    ("empty string", lambda: ""),
])
def test_every_failure_is_an_explicit_deny_for_this_route(mod, label, token):
    _assert_deny(mod.handler(_event(token()), None))


def test_no_query_string_is_a_deny(mod):
    _assert_deny(mod.handler(_event(None), None))


def test_a_tampered_signature_is_a_deny(mod):
    header, payload, sig = _token().split(".")
    tampered = f"{header}.{payload}.{sig[:-4]}{'AAAA' if sig[-4:] != 'AAAA' else 'BBBB'}"
    _assert_deny(mod.handler(_event(tampered), None))


def test_a_tampered_payload_is_a_deny(mod):
    import base64

    header, payload, sig = _token().split(".")
    claims = json.loads(base64.urlsafe_b64decode(payload + "=="))
    claims["custom:tenantId"] = TENANT_B
    forged = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode()
    _assert_deny(mod.handler(_event(f"{header}.{forged}.{sig}"), None))


@pytest.mark.parametrize("method_arn", [None, ""])
def test_a_valid_token_without_a_methodarn_is_denied_everywhere(mod, method_arn):
    _assert_deny(mod.handler(_event(_token(), method_arn=method_arn), None), resource="*")


def test_a_jwks_outage_is_a_deny(mod, monkeypatch):
    def _down(_pool_id):
        raise OSError("JWKS unreachable")
    monkeypatch.setattr(mod, "_jwks_client", _down)
    _assert_deny(mod.handler(_event(_token()), None))



# ---------- JWKS client -----------------------------------------------------------


def test_the_jwks_client_fetches_the_pools_own_keys_within_the_lambda_timeout(monkeypatch):
    """The fake client above hides the real URL and timeout; check both here.

    The Lambda times out at 5 s (vsa-api-stack.ts WsAuthorizerFn). PyJWKClient's default
    30 s fetch timeout would turn a stalled JWKS into a Lambda timeout and a 500.
    Constructing the client makes no network call.
    """
    m = _load(monkeypatch)
    client = m._jwks_client(POOL_A)
    assert isinstance(client, jwt.PyJWKClient)
    assert client.uri == f"https://cognito-idp.{REGION}.amazonaws.com/{POOL_A}/.well-known/jwks.json"
    assert 0 < client.timeout <= 4
    assert m._jwks_client(POOL_A) is client, "the client is cached per pool"


# ---------- Audience: which app clients may connect -------------------------------


@pytest.mark.parametrize("client_ids", [None, [], "appclienta1", [""], [None], [123]])
def test_a_pool_without_a_usable_client_allowlist_accepts_nothing(mod, monkeypatch, client_ids):
    """A missing or malformed `clientIds` fails closed, including a bare string, which
    would otherwise be read as a set of single characters."""
    pool = {"tenantId": TENANT_A, "userPoolId": POOL_A}
    if client_ids is not None:
        pool["clientIds"] = client_ids
    monkeypatch.setattr(mod, "_POOLS", [pool])
    decoded = []
    monkeypatch.setattr(mod.jwt, "decode", lambda *a, **k: decoded.append(k) or {})
    _assert_deny(mod.handler(_event(_token()), None))
    # The pool is skipped before verification; the deny does not rest on how PyJWT
    # treats an empty audience list.
    assert decoded == []


def test_an_unconfigured_pool_does_not_block_a_configured_one(mod, monkeypatch):
    monkeypatch.setattr(mod, "_POOLS", [
        {"tenantId": TENANT_A, "userPoolId": POOL_A},
        {"tenantId": TENANT_B, "userPoolId": POOL_B, "clientIds": [CLIENT_B]},
    ])
    token = _token(key=KEY_B, kid="kid-b", pool=POOL_B, tenant=TENANT_B, aud=CLIENT_B)
    assert _statement(mod.handler(_event(token), None))["Effect"] == "Allow"


def test_any_listed_client_is_accepted(mod, monkeypatch):
    monkeypatch.setattr(mod, "_POOLS", [
        {"tenantId": TENANT_A, "userPoolId": POOL_A, "clientIds": ["firstclient", CLIENT_A]},
    ])
    assert _statement(mod.handler(_event(_token()), None))["Effect"] == "Allow"


def test_the_audience_passed_to_pyjwt_is_this_pools_allowlist(mod, monkeypatch):
    """Pin the call itself: verify_aud on, aud required, audience = the pool's clientIds."""
    seen = []
    real_decode = mod.jwt.decode

    def _spy(*args, **kwargs):
        seen.append((kwargs.get("audience"), kwargs.get("options")))
        return real_decode(*args, **kwargs)

    monkeypatch.setattr(mod.jwt, "decode", _spy)
    assert _statement(mod.handler(_event(_token()), None))["Effect"] == "Allow"
    audience, options = seen[0]
    assert audience == [CLIENT_A]
    assert options["verify_aud"] is True
    assert "aud" in options["require"]


@pytest.mark.parametrize("pool", [
    {"userPoolId": POOL_A, "clientIds": [CLIENT_A]},                       # no tenantId
    {"tenantId": TENANT_A, "clientIds": [CLIENT_A]},                       # no userPoolId
    {"tenantId": None, "userPoolId": POOL_A, "clientIds": [CLIENT_A]},     # tenantId not a string
])
def test_a_malformed_pool_entry_is_a_deny_not_an_error(mod, monkeypatch, pool):
    """Security review 2026-10-01, S2: a broken entry must Deny (403), not raise (500)."""
    monkeypatch.setattr(mod, "_POOLS", [pool])
    _assert_deny(mod.handler(_event(_token()), None))
