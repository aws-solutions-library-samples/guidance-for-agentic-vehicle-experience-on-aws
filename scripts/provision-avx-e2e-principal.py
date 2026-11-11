#!/usr/bin/env python3
"""Provision the ephemeral principal the AVX E2E gate's Step 5 needs — and prove it fits.

Step-5 prerequisite 5. Gated on ``LIVE_TESTS=1``.

The problem
-----------
Step 5 of the AVX core live-verification gate calls
``POST /findings/{finding_id}/approve`` with ``target_system=dms`` and asserts that
``DmsExecutor`` reached DMS's ``POST /api/dms/fleet/repair-orders``. That single request
crosses two authorizers and two authorization checks, and they do not agree on much:

===========================  ==========================================================
Gate                         Requirement
===========================  ==========================================================
CVX API Gateway authorizer   a token from a CVX tenant pool **or** the CMS pool
                             (``authorizerPools = [...allPools, ...extraPools]``)
CVX approve handler           ``platform-admin`` or ``service-advisor`` group, or owner
                             scope — ``_check_approve_scope``
DMS API Gateway authorizer   ``CmsPoolAuthorizer`` — the **CMS pool only**
DMS create-RO handler        ``fleet-operator`` group
===========================  ==========================================================

``DmsExecutor`` forwards **the caller's own JWT** to DMS rather than using the Lambda's
role (``retail.py`` names that substitution a confused deputy), so ONE token has to
satisfy all four rows. That means a **CMS-pool** user holding **both**
``platform-admin`` and ``fleet-operator``.

Why ephemeral rather than a seeded fixture
------------------------------------------
A standing CMS-pool user carrying ``platform-admin`` is a permanent privileged principal
in the pool that also holds the real demo users. It would outlive the test, and nothing
would ever tell us it was still there. This provisions on demand and deletes in a
``finally``.

What it verifies, and why that is the point
-------------------------------------------
The four-row table above was established by READING CDK. This script checks it against
the running services: it mints a real token and calls both APIs, asserting neither
answers 401/403. A read-only DMS route is used for that probe, so nothing is created.

That matters because the cross-pool acceptance is the single assumption most likely to be
wrong, and the cheapest place to be wrong about it is here — not midway through Step 5,
where a 401 from DMS would look like a dispatch defect.
"""

from __future__ import annotations

import base64
import contextlib
import json
import os
import secrets
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

REGION = "us-west-2"
STAGE = "staging"

#: The CMS pool. Resolved at runtime from CVX's own SSM parameter rather than hardcoded —
#: `/cvx/{stage}/voice-auth-user-pool-id` is the CMS pool id, and a literal here would be
#: both a publish-scanner critical and a value that goes stale on pool replacement.
CMS_POOL_SSM = f"/cvx/{STAGE}/voice-auth-user-pool-id"
DMS_ENDPOINT_SSM = f"/cvx/{STAGE}/dms-api-endpoint"

#: Both are required, for different gates. See the module docstring's table.
REQUIRED_GROUPS = ("platform-admin", "fleet-operator")

USERNAME = f"avx-e2e-step5-{int(time.time())}@example.invalid"


def fail(msg: str) -> None:
    print(f"\nFAIL: {msg}")
    sys.exit(1)


def _ssm(name: str) -> str:
    import boto3

    value = boto3.client("ssm", region_name=REGION).get_parameter(Name=name)["Parameter"]["Value"]
    # The pagination trap that corrupted this very parameter on 2026-09-23 wrote a
    # multi-line value. Assert shape on READ too, so a bad parameter fails here with its
    # own name rather than as a confusing 404 three calls later.
    if len(value.splitlines()) != 1 or value.strip() in ("", "None", "null"):
        fail(f"SSM {name} holds a malformed value ({value!r}). Re-seed it: see "
             f"scripts/seed-stage-parameters.sh.")
    return value.strip()


def _claims(id_token: str) -> dict:
    """Decode a JWT payload WITHOUT verifying. Local inspection only, never a trust decision."""
    payload = id_token.split(".")[1]
    payload += "=" * (-len(payload) % 4)
    return json.loads(base64.urlsafe_b64decode(payload))


def _probe(url: str, token: str, label: str) -> int:
    """GET `url` with the token; return the HTTP status. 401/403 is the failure we care about."""
    req = urllib.request.Request(url, method="GET", headers={"Authorization": token})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status
    except urllib.error.HTTPError as exc:
        return exc.code
    except Exception as exc:  # noqa: BLE001
        fail(f"{label}: request to {url} failed before returning a status: "
             f"{type(exc).__name__}: {exc}")
        return 0  # unreachable


@contextlib.contextmanager
def provision():
    """Yield ``(id_token, username, claims)`` for an ephemeral CMS-pool principal.

    Deletes the user in a ``finally``, and prints the manual delete command if that fails —
    a leaked privileged user is real residue, not a test artifact.
    """
    import boto3

    idp = boto3.client("cognito-idp", region_name=REGION)
    pool_id = _ssm(CMS_POOL_SSM)

    clients = idp.list_user_pool_clients(UserPoolId=pool_id, MaxResults=30)["UserPoolClients"]
    usable = []
    for c in clients:
        desc = idp.describe_user_pool_client(
            UserPoolId=pool_id, ClientId=c["ClientId"])["UserPoolClient"]
        if "ALLOW_USER_PASSWORD_AUTH" in (desc.get("ExplicitAuthFlows") or []):
            usable.append(desc)
    if not usable:
        fail(f"no app client on {pool_id} permits USER_PASSWORD_AUTH, so this script cannot "
             f"mint a token. Clients: {[c['ClientName'] for c in clients]}")
    client = usable[0]

    existing = {g["GroupName"] for g in
                idp.list_groups(UserPoolId=pool_id, Limit=60)["Groups"]}
    absent = [g for g in REQUIRED_GROUPS if g not in existing]
    if absent:
        fail(f"pool {pool_id} has no group(s) {absent}. Step 5 needs a caller in "
             f"{list(REQUIRED_GROUPS)}; see the module docstring for which gate needs which.")

    print(f"pool     : {pool_id} (from {CMS_POOL_SSM})")
    print(f"client   : {client['ClientName']}")
    print(f"user     : {USERNAME} (ephemeral — deleted in finally)")
    print(f"groups   : {', '.join(REQUIRED_GROUPS)}")

    password = f"Av{secrets.token_urlsafe(18)}!7Qx"
    created = False
    try:
        idp.admin_create_user(
            UserPoolId=pool_id, Username=USERNAME, MessageAction="SUPPRESS",
            UserAttributes=[{"Name": "email", "Value": USERNAME},
                            {"Name": "email_verified", "Value": "true"}],
        )
        created = True
        idp.admin_set_user_password(
            UserPoolId=pool_id, Username=USERNAME, Password=password, Permanent=True)
        for group in REQUIRED_GROUPS:
            idp.admin_add_user_to_group(
                UserPoolId=pool_id, Username=USERNAME, GroupName=group)

        auth = idp.initiate_auth(
            ClientId=client["ClientId"], AuthFlow="USER_PASSWORD_AUTH",
            AuthParameters={"USERNAME": USERNAME, "PASSWORD": password},
        )
        id_token = auth["AuthenticationResult"]["IdToken"]
        yield id_token, USERNAME, _claims(id_token)
    finally:
        if created:
            try:
                idp.admin_delete_user(UserPoolId=pool_id, Username=USERNAME)
                print(f"\ncleanup  : deleted {USERNAME}")
            except Exception as exc:  # noqa: BLE001
                print(f"\ncleanup  : FAILED to delete {USERNAME}: {exc}", file=sys.stderr)
                print(f"           aws cognito-idp admin-delete-user --user-pool-id "
                      f"{pool_id} --username {USERNAME} --region {REGION}", file=sys.stderr)


def main() -> int:
    if os.environ.get("LIVE_TESTS") != "1":
        print("SKIPPED — set LIVE_TESTS=1 to run "
              "(creates + deletes an ephemeral staging Cognito user).")
        return 0

    import boto3

    cfn = boto3.client("cloudformation", region_name=REGION)
    try:
        outs = cfn.describe_stacks(StackName=f"vsa-{STAGE}-api")["Stacks"][0]["Outputs"]
    except Exception as exc:  # noqa: BLE001
        fail(f"cannot describe vsa-{STAGE}-api in {REGION}: {type(exc).__name__}: {exc}")
    cvx_base = next((o["OutputValue"] for o in outs if o["OutputKey"] == "RestApiUrl"), "").rstrip("/")
    if not cvx_base:
        fail(f"vsa-{STAGE}-api has no RestApiUrl output")
    dms_base = _ssm(DMS_ENDPOINT_SSM).rstrip("/")

    print(f"cvx api  : {cvx_base}")
    print(f"dms api  : {dms_base} (from {DMS_ENDPOINT_SSM})")
    print()

    with provision() as (id_token, username, claims):
        groups = claims.get("cognito:groups") or []
        print()
        print(f"token iss    : {claims.get('iss', '')[-24:]}")
        print(f"token groups : {sorted(groups)}")

        missing = [g for g in REQUIRED_GROUPS if g not in groups]
        if missing:
            fail(f"the minted token does not carry {missing}. Group membership is applied "
                 f"before initiate_auth, so if this fires the add-to-group call silently "
                 f"did not take effect.")

        # ── The cross-pool assertion, against the running services ──────────
        #
        # Read-only probes. A 401/403 means an authorizer rejected the token, which is
        # the thing being tested; any other status (200/404/400) means the request got
        # PAST auth, which is all this needs to establish.
        cvx_status = _probe(f"{cvx_base}/findings/owner/{claims.get('sub','')}", id_token, "CVX")
        print(f"CVX  probe   : HTTP {cvx_status}")
        if cvx_status in (401, 403):
            fail(f"CVX rejected a CMS-pool token (HTTP {cvx_status}). The API authorizer is "
                 f"supposed to include the CMS pool via extraCognitoUserPoolIds — check that "
                 f"`extraPools` is non-empty for this stage.")

        dms_status = _probe(f"{dms_base}/api/dms/fleet/repair-orders", id_token, "DMS")
        print(f"DMS  probe   : HTTP {dms_status}")
        if dms_status in (401, 403):
            fail(f"DMS rejected the token (HTTP {dms_status}). DMS's authorizer is "
                 f"CmsPoolAuthorizer and this token IS from the CMS pool, and the caller "
                 f"holds fleet-operator — so a rejection here means the assumption Step 5 "
                 f"rests on is wrong, and DmsExecutor would fail the same way.")

        print()
        print("RESULT   : PASS — one ephemeral CMS-pool principal satisfies BOTH")
        print("           authorizers and carries both groups. Step 5's cross-pool")
        print("           premise is verified against the running services, not inferred.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
