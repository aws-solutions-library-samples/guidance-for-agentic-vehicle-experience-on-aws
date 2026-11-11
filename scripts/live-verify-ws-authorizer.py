#!/usr/bin/env python3
"""Live-verify the WebSocket $connect authorizer against real Cognito and API Gateway.

Gated on ``LIVE_TESTS=1`` per ``~/.kiro/steering/spec-workflow.md`` § "Live-Verification
Gate". Written 2026-10-01 for
``issues/2026-10-01-ws-authorizer-returns-http-api-response-shape/`` and
``issues/2026-10-01-ws-authorizer-bundle-built-for-python-3-14/``: the deployed authorizer
answered 500 for every token, because its response had the HTTP-API shape and its crypto
backend was a Python 3.14 binary on a 3.12 Lambda. The unit tests sign with self-minted
keys and replace the JWKS fetch, so neither defect was reachable from them.

Four cases, each a real WebSocket upgrade against the deployed API:

  1. a real Cognito ID token for the configured tenant  -> connection accepted
  2. no token                                           -> refused, HTTP 401
  3. the same token with a corrupted signature          -> refused, HTTP 403
  4. a real ID token from the pool's KIOSK app client   -> refused, HTTP 403
     (same user, same pool, different `aud`; added with
     issues/2026-10-01-ws-authorizer-accepts-any-app-client/)

The kiosk client allows only SRP sign-in, so case 4 signs in with Cognito's
USER_SRP_AUTH flow, implemented below (no extra dependency).

Writes: one Cognito user, created in the CVX staging tenant pool with
``MessageAction=SUPPRESS`` and deleted in a ``finally`` block (deletion is then
confirmed); the accepted connection adds one row to the stage's ws-connections table,
removed again on disconnect. If the script is killed between create and delete, the
user is left behind; find it by its name prefix:

    aws cognito-idp list-users --user-pool-id <pool> --region us-west-2 \\
      --filter 'email ^= "ws-authz-live-verify-"'

Nothing is hardcoded: the pool, the WebSocket URL and the
tenant come from the ``vsa-staging-api`` stack outputs and the authorizer's own
configuration, so describing them is also the account and region check.
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import sys
import time
import urllib.parse

REGION = "us-west-2"
STAGE = "staging"
API_STACK = f"vsa-{STAGE}-api"
AUTHORIZER_FN = f"vsa-{STAGE}-ws-authorizer"
# The app client that permits USER_PASSWORD_AUTH. Resolved by NAME, never by id.
CLIENT_NAME = f"vsa-{STAGE}-meridian-fleet-app"
# The same pool's kiosk client: SRP only, and NOT allowed to open a WebSocket.
KIOSK_CLIENT_NAME = f"vsa-{STAGE}-meridian-fleet-kiosk"
#: The pool uses email as the username; `example.invalid` is reserved (RFC 2606).
USERNAME = f"ws-authz-live-verify-{int(time.time())}@example.invalid"


# ── Cognito USER_SRP_AUTH (SRP-6a over the RFC 3526 3072-bit group) ────────────────
# The prime is the public RFC 3526 group-15 modulus that Cognito uses; g = 2.
_SRP_N_HEX = (
    "FFFFFFFFFFFFFFFFC90FDAA22168C234C4C6628B80DC1CD129024E088A67CC74"
    "020BBEA63B139B22514A08798E3404DDEF9519B3CD3A431B302B0A6DF25F1437"
    "4FE1356D6D51C245E485B576625E7EC6F44C42E9A637ED6B0BFF5CB6F406B7ED"
    "EE386BFB5A899FA5AE9F24117C4B1FE649286651ECE45B3DC2007CB8A163BF05"
    "98DA48361C55D39A69163FA8FD24CF5F83655D23DCA3AD961C62F356208552BB"
    "9ED529077096966D670C354E4ABC9804F1746C08CA18217C32905E462E36CE3B"
    "E39E772C180E86039B2783A2EC07A28FB5C55DF06F4C52C9DE2BCBF695581718"
    "3995497CEA956AE515D2261898FA051015728E5A8AAAC42DAD33170D04507A33"
    "A85521ABDF1CBA64ECFB850458DBEF0A8AEA71575D060C7DB3970F85A6E1E4C7"
    "ABF5AE8CDB0933D71E8C94E04A25619DCEE3D2261AD2EE6BF12FFA06D98A0864"
    "D87602733EC86A64521F2B18177B200CBBE117577A615D6C770988C0BAD946E2"
    "08E24FA074E5AB3143DB5BFCE0FD108E4B82D120A93AD2CAFFFFFFFFFFFFFFFF"
)
_SRP_N = int(_SRP_N_HEX, 16)
_SRP_G = 2


def _sha256_hex(hex_str: str) -> str:
    import hashlib

    return hashlib.sha256(bytes.fromhex(hex_str)).hexdigest()


def _pad_hex(value) -> str:
    """Hex with an even length and no high bit set, as Cognito's SRP expects."""
    h = value if isinstance(value, str) else format(value, "x")
    if len(h) % 2:
        h = "0" + h
    elif h[0] in "89abcdefABCDEF":
        h = "00" + h
    return h


def _srp_id_token(idp, pool_id: str, client_id: str, username: str, password: str) -> str:
    """Sign in with USER_SRP_AUTH and return the ID token."""
    import base64
    import datetime
    import hashlib
    import hmac

    k = int(_sha256_hex("00" + _SRP_N_HEX + "0" + format(_SRP_G, "x")), 16)
    a = int.from_bytes(secrets.token_bytes(128), "big") % _SRP_N
    big_a = pow(_SRP_G, a, _SRP_N)
    first = idp.initiate_auth(ClientId=client_id, AuthFlow="USER_SRP_AUTH",
                              AuthParameters={"USERNAME": username, "SRP_A": format(big_a, "x")})
    if first.get("ChallengeName") != "PASSWORD_VERIFIER":
        fail(f"SRP sign-in returned challenge {first.get('ChallengeName')!r}, not PASSWORD_VERIFIER")
    p = first["ChallengeParameters"]
    user_id, salt, secret_block = p["USER_ID_FOR_SRP"], p["SALT"], p["SECRET_BLOCK"]
    big_b = int(p["SRP_B"], 16)
    u = int(_sha256_hex(_pad_hex(big_a) + _pad_hex(big_b)), 16)
    pool_name = pool_id.split("_", 1)[1]
    inner = hashlib.sha256(f"{pool_name}{user_id}:{password}".encode()).hexdigest()
    x = int(_sha256_hex(_pad_hex(salt) + inner), 16)
    s = pow(big_b - k * pow(_SRP_G, x, _SRP_N), a + u * x, _SRP_N)
    prk = hmac.new(bytes.fromhex(_pad_hex(u)), bytes.fromhex(_pad_hex(s)), hashlib.sha256).digest()
    key = hmac.new(prk, b"Caldera Derived Key\x01", hashlib.sha256).digest()[:16]
    now = datetime.datetime.now(datetime.timezone.utc)
    stamp = now.strftime("%a %b ") + str(now.day) + now.strftime(" %H:%M:%S UTC %Y")
    msg = pool_name.encode() + user_id.encode() + base64.b64decode(secret_block) + stamp.encode()
    signature = base64.b64encode(hmac.new(key, msg, hashlib.sha256).digest()).decode()
    second = idp.respond_to_auth_challenge(
        ClientId=client_id, ChallengeName="PASSWORD_VERIFIER",
        ChallengeResponses={"TIMESTAMP": stamp, "USERNAME": user_id,
                            "PASSWORD_CLAIM_SECRET_BLOCK": secret_block,
                            "PASSWORD_CLAIM_SIGNATURE": signature})
    return second["AuthenticationResult"]["IdToken"]


def _claims(token: str) -> dict:
    """Unverified claims, only to confirm which client a test token came from."""
    import base64

    payload = token.split(".")[1]
    return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))


def fail(msg: str) -> None:
    print(f"\nFAIL: {msg}")
    sys.exit(1)


def _cfn_output(cfn, key_contains: str) -> str:
    """The one stack output whose key CONTAINS `key_contains` (CDK suffixes keys)."""
    try:
        outs = cfn.describe_stacks(StackName=API_STACK)["Stacks"][0].get("Outputs") or []
    except Exception as exc:  # noqa: BLE001
        fail(f"cannot describe {API_STACK} in {REGION}: {type(exc).__name__}: {exc}\n"
             f"      This doubles as the account/region check.")
    hits = [o for o in outs if key_contains in o["OutputKey"]]
    if len(hits) != 1:
        fail(f"expected exactly 1 output on {API_STACK} containing {key_contains!r}, "
             f"found {[o['OutputKey'] for o in hits]}")
    return hits[0]["OutputValue"]


def _status(exc: BaseException) -> int | None:
    """HTTP status from a rejected upgrade, across websockets' legacy and new clients."""
    code = getattr(exc, "status_code", None)
    if code is None:
        code = getattr(getattr(exc, "response", None), "status_code", None)
    return code


async def _connect(url: str) -> tuple[bool, int | None, str]:
    """(accepted, http_status, detail) for one upgrade attempt."""
    import websockets

    try:
        async with websockets.connect(url, open_timeout=15, close_timeout=5):
            return True, 101, "upgraded"
    except Exception as exc:  # noqa: BLE001
        return False, _status(exc), f"{type(exc).__name__}: {str(exc)[:120]}"


def _corrupt_signature(token: str) -> str:
    header, payload, sig = token.split(".")
    flipped = ("A" if sig[0] != "A" else "B") + sig[1:]
    return f"{header}.{payload}.{flipped}"


def main() -> int:
    if os.environ.get("LIVE_TESTS") != "1":
        print("SKIPPED — set LIVE_TESTS=1 to run (creates + deletes a staging Cognito user).")
        return 0

    import boto3

    cfn = boto3.client("cloudformation", region_name=REGION)
    idp = boto3.client("cognito-idp", region_name=REGION)
    lam = boto3.client("lambda", region_name=REGION)

    pool_id = _cfn_output(cfn, "UserPoolId")
    ws_url = _cfn_output(cfn, "WsApiUrl")
    pools = json.loads(
        lam.get_function_configuration(FunctionName=AUTHORIZER_FN)["Environment"]["Variables"][
            "TENANT_POOL_IDS"
        ]
    )
    entries = [p for p in pools if p.get("userPoolId") == pool_id]
    if len(entries) != 1:
        fail(f"the authorizer has {len(entries)} entries for the stack's pool; expected 1. "
             f"A token from this pool could not be expected to connect.")
    tenant = entries[0]["tenantId"]
    allowed = entries[0].get("clientIds") or []
    if not isinstance(allowed, list) or not all(isinstance(c, str) for c in allowed):
        fail(f"the authorizer's clientIds is not a list of strings: {allowed!r}")

    clients = idp.list_user_pool_clients(UserPoolId=pool_id, MaxResults=20)["UserPoolClients"]
    by_name = {c["ClientName"]: c["ClientId"] for c in clients}
    if CLIENT_NAME not in by_name or KIOSK_CLIENT_NAME not in by_name:
        fail(f"expected app clients {CLIENT_NAME!r} and {KIOSK_CLIENT_NAME!r} in the stack's pool")
    client_id, kiosk_id = by_name[CLIENT_NAME], by_name[KIOSK_CLIENT_NAME]
    if client_id not in allowed:
        fail(f"{CLIENT_NAME} is not in the authorizer's clientIds; case 1 could not pass")
    if kiosk_id in allowed:
        fail(f"{KIOSK_CLIENT_NAME} IS in the authorizer's clientIds; case 4 would prove nothing")

    print(f"caller   : {boto3.client('sts', region_name=REGION).get_caller_identity()['Arn']}")
    print(f"stack    : {API_STACK} (its existence is the account/region check)")
    print(f"ws url   : {ws_url}")
    print(f"tenant   : {tenant} (from {AUTHORIZER_FN} TENANT_POOL_IDS)")
    print(f"clients  : allowed {CLIENT_NAME} ({client_id[:6]}...); refused {KIOSK_CLIENT_NAME} ({kiosk_id[:6]}...)")
    print(f"user     : {USERNAME} (throwaway — deleted in finally)")

    password = f"Tv{secrets.token_urlsafe(18)}!9aZ"
    created = False
    results: list[tuple[str, bool]] = []
    try:
        idp.admin_create_user(
            UserPoolId=pool_id,
            Username=USERNAME,
            MessageAction="SUPPRESS",
            UserAttributes=[
                {"Name": "email", "Value": USERNAME},
                {"Name": "custom:tenantId", "Value": tenant},
            ],
        )
        created = True
        idp.admin_set_user_password(
            UserPoolId=pool_id, Username=USERNAME, Password=password, Permanent=True
        )
        auth = idp.initiate_auth(
            ClientId=client_id,
            AuthFlow="USER_PASSWORD_AUTH",
            AuthParameters={"USERNAME": USERNAME, "PASSWORD": password},
        )
        token = auth["AuthenticationResult"]["IdToken"]
        kiosk_token = _srp_id_token(idp, pool_id, kiosk_id, USERNAME, password)
        if _claims(kiosk_token).get("aud") != kiosk_id or _claims(token).get("aud") != client_id:
            fail("the test tokens do not carry the expected aud; case 4 would prove nothing")

        def _with(tok: str | None) -> str:
            return ws_url if tok is None else f"{ws_url}?token={urllib.parse.quote(tok, safe='')}"

        cases = [
            ("1 valid ID token", _with(token), lambda ok, code: ok),
            ("2 no token", _with(None), lambda ok, code: not ok and code == 401),
            ("3 corrupted signature", _with(_corrupt_signature(token)),
             lambda ok, code: not ok and code == 403),
            ("4 kiosk-client token", _with(kiosk_token), lambda ok, code: not ok and code == 403),
        ]
        print()
        for label, url, passed in cases:
            ok, code, detail = asyncio.run(_connect(url))
            verdict = passed(ok, code)
            results.append((label, verdict))
            outcome = "accepted" if ok else f"refused HTTP {code}"
            print(f"{'PASS' if verdict else 'FAIL'}  case {label:<22} -> {outcome}   ({detail})")
    finally:
        if created:
            try:
                idp.admin_delete_user(UserPoolId=pool_id, Username=USERNAME)
                try:
                    idp.admin_get_user(UserPoolId=pool_id, Username=USERNAME)
                    print(f"\ncleanup  : FAILED — {USERNAME} still exists after delete",
                          file=sys.stderr)
                except idp.exceptions.UserNotFoundException:
                    print(f"\ncleanup  : deleted {USERNAME} (confirmed absent)")
            except Exception as exc:  # noqa: BLE001
                print(f"\ncleanup  : FAILED to delete {USERNAME}: {exc}", file=sys.stderr)
                print(f"           Run: aws cognito-idp admin-delete-user --user-pool-id "
                      f"<the {API_STACK} UserPoolId output> --username {USERNAME} "
                      f"--region {REGION}", file=sys.stderr)

    if len(results) != 4 or not all(v for _, v in results):
        fail("not every case behaved as expected; see above")
    print("\nRESULT   : PASS — a real app-client token connects; a missing, forged or kiosk-client")
    print("           token is refused.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
