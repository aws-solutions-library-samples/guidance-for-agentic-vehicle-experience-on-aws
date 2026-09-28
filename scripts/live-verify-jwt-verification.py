#!/usr/bin/env python3
"""Live-verify `agents/supervisor/jwt_verify.py` against real Cognito, not a stub.

Fix Group 18 T18.6. Gated on ``LIVE_TESTS=1`` per
``~/.kiro/steering/spec-workflow.md`` § "Live-Verification Gate".

Why this exists
---------------
Every one of the 35 unit tests in ``test_jwt_verify.py`` installs a **fake**
``PyJWKClient`` and signs with **self-minted** RSA keys. That is the right shape for unit
tests, and it means the following have never been exercised:

* the real Cognito JWKS URL, its reachability, and its parseability;
* whether a genuine Cognito **ID token** actually satisfies the checks — in particular
  whether Cognito's real claim shape matches what ``verify_id_token`` assumes
  (``token_use == "id"``, ``iss`` exactly as constructed, RS256);
* the latency the JWKS fetch adds to session establish.

A stub cannot fail the way a service fails. If Cognito's claim shape differed from the
assumption, `observe` mode would log `jwt verification FAILED` for every real user and
the enforce flip would reject all of them — a defect this script surfaces in seconds
instead of waiting on iOS traffic.

What it does
------------
Provisions an **ephemeral** user in the CVX-owned staging pool, mints a real ID token via
``USER_PASSWORD_AUTH``, verifies it, and deletes the user in a ``finally`` block. The
CMS UI pool is deliberately NOT used — it holds real demo users.

Writes: one Cognito user created and deleted, in **staging only**. `MessageAction=SUPPRESS`
so no email is sent. The script refuses to run against any pool it was not told to use.
"""

from __future__ import annotations

import os
import secrets
import sys
import time
from pathlib import Path

# Run from anywhere: `agents.supervisor` is importable only with the repo root on the
# path, and a script under scripts/ does not get that for free.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

REGION = "us-west-2"
STAGE = "staging"
API_STACK = f"vsa-{STAGE}-api"
# The app client that permits USER_PASSWORD_AUTH. Resolved by NAME, never by id.
CLIENT_NAME = f"vsa-{STAGE}-meridian-fleet-app"

#: This pool is configured with email as the username attribute (AdminCreateUser
#: rejects a non-email username), so the ephemeral name is an address at
#: `example.invalid` — a reserved TLD per RFC 2606, guaranteed undeliverable.
USERNAME = f"t186-live-verify-{int(time.time())}@example.invalid"

# NOTHING here is a hardcoded pool id or account id, deliberately.
#
# An earlier revision hardcoded both trusted pool ids, plus the account id inside the
# very assertion meant to keep this script off the wrong account. That shipped 4
# publish-scanner criticals in a brand-new file, and the account one is the trap
# ~/.kiro/steering/public-mirror-publish.md names directly: a guard written in terms of
# the secret embeds the secret.
#
# Both are now resolved at runtime, which is strictly better than a scanner workaround:
#
#   * the tenant pool + client come from the CloudFormation stack, so describing it
#     PROVES we are in the intended account and region — stronger than the account
#     assertion it replaces, since a literal can go stale;
#   * the trusted-pool allowlist comes from the voice identity pool's providers, the
#     same derivation `make deploy-staging-bidi-runtime` uses, so this script and the
#     deploy cannot disagree about what is trusted.


def fail(msg: str) -> None:
    print(f"\nFAIL: {msg}")
    sys.exit(1)


def _cfn_output(cfn, stack: str, key_contains: str) -> str:
    """The one stack output whose key CONTAINS `key_contains`.

    Substring-matched because CDK appends a hash suffix to output keys, and that suffix
    changes when the construct is replaced. Fails loudly on zero or multiple matches
    rather than silently taking the first.
    """
    try:
        outs = cfn.describe_stacks(StackName=stack)["Stacks"][0].get("Outputs") or []
    except Exception as exc:  # noqa: BLE001
        fail(f"cannot describe {stack} in {REGION}: {type(exc).__name__}: {exc}\n"
             f"      This doubles as the account/region check — if the stack is absent, "
             f"these credentials are not pointed at CVX {STAGE} in {REGION}.")
    hits = [o for o in outs if key_contains in o["OutputKey"]]
    if len(hits) != 1:
        fail(f"expected exactly 1 output on {stack} containing {key_contains!r}, "
             f"found {len(hits)}: {[o['OutputKey'] for o in hits]}")
    return hits[0]["OutputValue"]


def _derive_trusted_pools(cognito_identity, identity_pool_id: str) -> str:
    """Trusted pool ids = the voice identity pool's Cognito providers.

    That provider set IS definitionally the issuers whose tokens can reach the runtime,
    so deriving it here keeps this script in agreement with the deploy by construction.
    """
    providers = cognito_identity.describe_identity_pool(
        IdentityPoolId=identity_pool_id
    ).get("CognitoIdentityProviders") or []
    prefix = f"cognito-idp.{REGION}.amazonaws.com/"
    pools = [p["ProviderName"][len(prefix):] for p in providers
             if p.get("ProviderName", "").startswith(prefix)]
    if not pools:
        fail(f"identity pool {identity_pool_id} lists no Cognito providers — with no "
             f"trusted issuer there is nothing to verify against.")
    return ",".join(sorted(set(pools)))


def main() -> int:
    if os.environ.get("LIVE_TESTS") != "1":
        print("SKIPPED — set LIVE_TESTS=1 to run (creates + deletes a staging Cognito user).")
        return 0

    import boto3

    os.environ["AWS_REGION"] = REGION

    from agents.supervisor import jwt_verify as V

    cfn = boto3.client("cloudformation", region_name=REGION)
    idp = boto3.client("cognito-idp", region_name=REGION)
    cid = boto3.client("cognito-identity", region_name=REGION)

    # Resolving from the stack IS the account/region guard: these outputs exist only in
    # the account and region that deployed CVX staging.
    pool_id = _cfn_output(cfn, API_STACK, "UserPoolId")
    identity_pool_id = _cfn_output(cfn, API_STACK, "IdentityPoolId")
    trusted = _derive_trusted_pools(cid, identity_pool_id)

    os.environ["VSA_TRUSTED_USER_POOL_IDS"] = trusted
    os.environ.setdefault("VSA_JWT_VERIFY_MODE", "observe")

    caller = boto3.client("sts", region_name=REGION).get_caller_identity()["Arn"]
    print(f"caller   : {caller}")
    print(f"stack    : {API_STACK} (its existence is the account/region check)")
    print(f"pool     : {pool_id} (CVX tenant pool, from stack output)")
    print(f"trusted  : {trusted}")
    print(f"           (derived from identity pool {identity_pool_id})")

    if pool_id not in trusted.split(","):
        fail(f"the tenant pool {pool_id} is not among the identity pool's trusted "
             f"providers ({trusted}) — a token from it could not reach the runtime, so "
             f"this test would prove nothing about the deployed allowlist.")

    # Resolve the client id by NAME. Hardcoding an id couples this script to a value
    # that changes on pool replacement; resolving by name fails loudly instead.
    clients = idp.list_user_pool_clients(UserPoolId=pool_id, MaxResults=20)["UserPoolClients"]
    match = [c for c in clients if c["ClientName"] == CLIENT_NAME]
    if not match:
        fail(f"no app client named {CLIENT_NAME!r} in {pool_id} "
             f"(found: {[c['ClientName'] for c in clients]})")
    client_id = match[0]["ClientId"]
    print(f"client   : {CLIENT_NAME} ({client_id[:8]}...)")
    print(f"user     : {USERNAME} (ephemeral — deleted in finally)")

    password = f"Tv{secrets.token_urlsafe(18)}!9aZ"
    created = False
    try:
        idp.admin_create_user(
            UserPoolId=pool_id,
            Username=USERNAME,
            MessageAction="SUPPRESS",
            UserAttributes=[{"Name": "email", "Value": USERNAME}],
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
        access = auth["AuthenticationResult"]["AccessToken"]

        # ── the check ────────────────────────────────────────────────────────
        t0 = time.time()
        out = V.verify_id_token(token)
        cold_ms = (time.time() - t0) * 1000
        t0 = time.time()
        V.verify_id_token(token)
        warm_ms = (time.time() - t0) * 1000

        print()
        print(f"ID token verify : ok={out.ok} reason={out.reason}")
        print(f"  latency       : {cold_ms:.0f}ms cold (JWKS fetch)  {warm_ms:.1f}ms warm")

        if not out.ok:
            fail(f"a REAL Cognito ID token from a trusted pool did not verify "
                 f"(reason={out.reason}). Under enforce this would reject every session.")

        # Anti-vacuity: prove the check is actually inspecting this token, not
        # returning ok for anything. An allowlist of expected properties, not a
        # denylist — a denylist here would pass on a token it never looked at.
        if not out.sub:
            fail("verified but carried no sub — the outcome is not reading the claims")
        claims = out.claims or {}
        if claims.get("token_use") != "id":
            fail(f"token_use={claims.get('token_use')!r} on a token that verified as an ID token")
        expected_iss = f"https://cognito-idp.{REGION}.amazonaws.com/{pool_id}"
        if claims.get("iss") != expected_iss:
            fail(f"iss mismatch: {claims.get('iss')!r} != {expected_iss!r}")
        print(f"  sub           : {out.sub[:8]}... (present)")
        print(f"  token_use     : {claims.get('token_use')}")
        print(f"  iss           : matches the constructed issuer")
        print(f"  aud           : {str(claims.get('aud'))[:8]}... (present but NOT verified, by design)")

        # The ACCESS token must be rejected. Cognito issues both from one call, and
        # accepting either interchangeably is how token-type confusion starts. This is
        # the one rejection case a real Cognito response can produce that a
        # self-minted token cannot make convincing.
        acc = V.verify_id_token(access)
        print()
        print(f"access token    : ok={acc.ok} reason={acc.reason}")
        if acc.ok or acc.reason != "wrong_token_use":
            fail(f"a real Cognito ACCESS token was not rejected as wrong_token_use "
                 f"(got ok={acc.ok} reason={acc.reason})")

        print()
        print("RESULT   : PASS — a real Cognito ID token verifies against live JWKS,")
        print("           a real access token is rejected, and the claim shape matches")
        print("           what verify_id_token assumes. Safe to consider `enforce`.")
        return 0

    finally:
        if created:
            try:
                idp.admin_delete_user(UserPoolId=pool_id, Username=USERNAME)
                print(f"\ncleanup  : deleted {USERNAME}")
            except Exception as exc:  # noqa: BLE001
                # Loud, because a leaked user in a staging pool is real residue.
                print(f"\ncleanup  : FAILED to delete {USERNAME}: {exc}", file=sys.stderr)
                print(f"           Run: aws cognito-idp admin-delete-user "
                      f"--user-pool-id {pool_id} --username {USERNAME} --region {REGION}",
                      file=sys.stderr)


if __name__ == "__main__":
    sys.exit(main())
