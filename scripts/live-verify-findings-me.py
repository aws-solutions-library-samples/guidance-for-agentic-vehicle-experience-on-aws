#!/usr/bin/env python3
"""Live-verify GET /findings/me and the owner-group reads on GET /findings/vehicle/{vin}.

Spec `.kiro/specs/2026-09-25-avx-own-vehicle-findings/` T2.3. Every unit test for these
routes stubs DynamoDB; a stub cannot fail the way a service fails, and the W1 oracle
(review Cycle 1) was a property of DynamoDB's own order of operations (Limit applied
before FilterExpression). This runs the same checks against the real table and API.

What it does
------------
1. Scans `vsa-{stage}-avx-findings` (read-only) and derives every expectation from the
   live rows: nothing below is a hardcoded Finding id.
2. Creates throwaway users in the CMS pool (`/cvx/{stage}/voice-auth-user-pool-id`),
   each with admin-set groups and claims, mints ID tokens with USER_PASSWORD_AUTH, and
   calls the deployed API.
3. Deletes every user it created in a `finally`, then checks the pool for leftovers
   under its email prefix.

Principals (tasks.md T2.3):
  driver   vehicle-owner + custom:vehicleId          (no customer id)
  owner    vehicle-owner + custom:customerId         (no vehicle id)
  fleet    fleet-operator + CSV custom:fleetIds      (the live claim shape)
  nonop    vehicle-owner + the same CSV fleetIds     (must get no fleet read)
  none     no group
  mdriver  meridian.driver@example.com's own groups and custom:* claims, copied at runtime

Usage
-----
    LIVE_TESTS=1 .venv/bin/python3 scripts/live-verify-findings-me.py [--stage staging] [--region us-west-2]

Exit codes: 0 all checks pass · 1 a check failed · 2 skipped (no LIVE_TESTS)
"""
from __future__ import annotations

import argparse
import base64
import collections
import datetime as dt
import json
import os
import secrets
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

import boto3

FAILURES: list[str] = []
PREFIX = f"avx-me-live-{int(time.time())}"


def check(ok: bool, msg: str) -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {msg}")
    if not ok:
        FAILURES.append(msg)


def _get(base: str, path: str, token: str, qs: dict | None = None) -> tuple[int, dict]:
    url = f"{base}{path}" + (f"?{urllib.parse.urlencode(qs)}" if qs else "")
    req = urllib.request.Request(url, headers={"Authorization": token})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        body = e.read()
        try:
            return e.code, json.loads(body or b"{}")
        except json.JSONDecodeError:
            return e.code, {"raw": body.decode(errors="replace")[:200]}


def _live_findings(table) -> list[dict]:
    now = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    items, kw = [], {}
    while True:
        r = table.scan(**kw)
        items += r.get("Items", [])
        if "LastEvaluatedKey" not in r:
            break
        kw["ExclusiveStartKey"] = r["LastEvaluatedKey"]
    return [i for i in items if not i.get("expires_at") or i["expires_at"] > now]


def _ids(body: dict) -> set[str]:
    return {f["finding_id"] for f in body.get("findings", [])}


def _all_pages(base: str, path: str, token: str, page_size: int) -> tuple[int, set[str], list[str]]:
    """Follow next_token to the end; return (last status, ids, decoded cursor finding_ids)."""
    ids, cursors, token_qs = set(), [], None
    for _ in range(200):
        qs = {"page_size": str(page_size), **({"next_token": token_qs} if token_qs else {})}
        status, body = _get(base, path, token, qs)
        if status != 200:
            return status, ids, cursors
        ids |= _ids(body)
        token_qs = body.get("next_token")
        if not token_qs:
            return status, ids, cursors
        cursors.append(json.loads(base64.b64decode(token_qs)).get("finding_id"))
    return 599, ids, cursors


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", default="staging")
    ap.add_argument("--region", default="us-west-2")
    args = ap.parse_args()
    if os.environ.get("LIVE_TESTS") != "1":
        print("skipped: set LIVE_TESTS=1 to run against real AWS")
        return 2

    stage, region = args.stage, args.region
    idp = boto3.client("cognito-idp", region_name=region)
    table = boto3.resource("dynamodb", region_name=region).Table(f"vsa-{stage}-avx-findings")
    pool = boto3.client("ssm", region_name=region).get_parameter(
        Name=f"/cvx/{stage}/voice-auth-user-pool-id")["Parameter"]["Value"].strip()
    outs = boto3.client("cloudformation", region_name=region).describe_stacks(
        StackName=f"vsa-{stage}-api")["Stacks"][0]["Outputs"]
    base = next(o["OutputValue"].rstrip("/") for o in outs if o["OutputKey"] == "RestApiUrl")
    client_id = next(
        c["ClientId"] for c in idp.list_user_pool_clients(UserPoolId=pool, MaxResults=60)["UserPoolClients"]
        if "ALLOW_USER_PASSWORD_AUTH" in (idp.describe_user_pool_client(
            UserPoolId=pool, ClientId=c["ClientId"])["UserPoolClient"].get("ExplicitAuthFlows") or []))

    live = _live_findings(table)
    by = collections.defaultdict(lambda: collections.defaultdict(set))
    for f in live:
        for k in ("vehicle_id", "owner_id", "fleet_id", "vin"):
            if f.get(k):
                by[k][f[k]].add(f["finding_id"])
    row = {f["finding_id"]: f for f in live}
    print(f"live Findings: {len(live)}; api {base}; pool {pool}")

    # Pick test subjects from live data: a vehicle with Findings, an owner with Findings on
    # at least two VINs, and two fleets (one with the CSV claim's first value).
    veh = max(by["vehicle_id"], key=lambda v: len(by["vehicle_id"][v]))
    veh_vin = row[next(iter(by["vehicle_id"][veh]))]["vin"]
    owner = max(by["owner_id"], key=lambda o: len({row[i]["vin"] for i in by["owner_id"][o]}))
    owner_vin = sorted({row[i]["vin"] for i in by["owner_id"][owner]})[0]
    fleets = sorted(by["fleet_id"], key=lambda f: -len(by["fleet_id"][f]))[:2]
    check(len(fleets) == 2, f"two fleets with live Findings exist ({fleets})")
    # Negative VINs: carry another party's Findings, none of the subject's.
    driver_neg_vin = next(v for v in sorted(by["vin"])
                          if not (by["vin"][v] & by["vehicle_id"][veh]))
    owner_neg_vin = next(v for v in sorted(by["vin"])
                         if not (by["vin"][v] & by["owner_id"][owner]))
    csv = ",".join(fleets)
    print(f"driver {veh} vin {veh_vin} neg {driver_neg_vin}; owner {owner} vin {owner_vin} "
          f"neg {owner_neg_vin}; fleets {csv}")

    md = idp.admin_get_user(UserPoolId=pool, Username="meridian.driver@example.com")
    md_claims = {a["Name"]: a["Value"] for a in md["UserAttributes"] if a["Name"].startswith("custom:")}
    md_groups = [g["GroupName"] for g in idp.admin_list_groups_for_user(
        UserPoolId=pool, Username="meridian.driver@example.com")["Groups"]]

    principals = {
        "driver": (["vehicle-owner"], {"custom:vehicleId": veh}),
        "owner": (["vehicle-owner"], {"custom:customerId": owner}),
        "fleet": (["fleet-operator"], {"custom:fleetIds": csv}),
        "nonop": (["vehicle-owner"], {"custom:fleetIds": csv}),
        "none": ([], {}),
        "mdriver": (md_groups, md_claims),
    }
    created: list[str] = []
    tokens: dict[str, str] = {}
    try:
        for name, (groups, claims) in principals.items():
            user = f"{PREFIX}-{name}@example.com"
            pw = f"Av{secrets.token_urlsafe(18)}!7Qx"
            idp.admin_create_user(
                UserPoolId=pool, Username=user, MessageAction="SUPPRESS",
                UserAttributes=[{"Name": "email", "Value": user},
                                {"Name": "email_verified", "Value": "true"}]
                + [{"Name": k, "Value": v} for k, v in claims.items()])
            created.append(user)
            idp.admin_set_user_password(UserPoolId=pool, Username=user, Password=pw, Permanent=True)
            for g in groups:
                idp.admin_add_user_to_group(UserPoolId=pool, Username=user, GroupName=g)
            tokens[name] = idp.initiate_auth(
                ClientId=client_id, AuthFlow="USER_PASSWORD_AUTH",
                AuthParameters={"USERNAME": user, "PASSWORD": pw})["AuthenticationResult"]["IdToken"]

        print("\n/findings/me")
        s, b = _get(base, "/findings/me", tokens["driver"])
        check(s == 200 and b.get("standings") == ["fleet_driver"], f"driver: 200 fleet_driver ({s} {b.get('standings')})")
        check(_ids(b) == by["vehicle_id"][veh], f"driver: exactly {veh}'s {len(by['vehicle_id'][veh])} Findings (got {len(_ids(b))})")

        s, b = _get(base, "/findings/me", tokens["owner"])
        check(s == 200 and b.get("standings") == ["owner"], f"owner: 200 owner ({s} {b.get('standings')})")
        check(_ids(b) == by["owner_id"][owner], f"owner: exactly {owner}'s {len(by['owner_id'][owner])} Findings (got {len(_ids(b))})")
        other_owner = set().union(*(v for k, v in by["owner_id"].items() if k != owner))
        check(not (_ids(b) & other_owner), "owner: no other customer's Finding")

        s, b = _get(base, "/findings/me", tokens["fleet"])
        want = by["fleet_id"][fleets[0]] | by["fleet_id"][fleets[1]]
        check(s == 200 and b.get("standings") == ["fleet_manager"], f"fleet (CSV claim): 200 fleet_manager ({s} {b.get('standings')})")
        check(_ids(b) == want, f"fleet: both fleets' {len(want)} Findings (got {len(_ids(b))})")
        check(b.get("truncated") is False, "fleet: truncated is false")

        s, b = _get(base, "/findings/me", tokens["nonop"])
        check(s == 200 and b.get("standings") == [] and b.get("findings") == [],
              f"non-operator with fleetIds: no standing, no Findings ({s} {b.get('standings')} {len(b.get('findings', []))})")

        s, b = _get(base, "/findings/me", tokens["none"])
        check(s == 200 and b.get("standings") == [] and b.get("findings") == [], f"no group: empty answer ({s})")

        s, b = _get(base, "/findings/me", tokens["mdriver"], {"page_size": "x", "vin": owner_vin})
        md_veh = md_claims.get("custom:vehicleId")
        check(s == 200 and b.get("standings") == ["fleet_driver"],
              f"meridian.driver's claims: fleet_driver only ({s} {b.get('standings')}); query string ignored")
        check(_ids(b) == by["vehicle_id"].get(md_veh, set()),
              f"meridian.driver's claims: only {md_veh}'s Findings ({len(_ids(b))} returned, "
              f"{len(by['vehicle_id'].get(md_veh, set()))} live)")

        print("\n/findings/vehicle/{vin}")
        s, ids, _ = _all_pages(base, f"/findings/vehicle/{veh_vin}", tokens["driver"], 50)
        want = by["vehicle_id"][veh] & by["vin"][veh_vin]
        check(s == 200 and ids == want, f"driver positive {veh_vin}: {len(want)} Findings (got {s} {len(ids)})")
        s, ids, cursors = _all_pages(base, f"/findings/vehicle/{driver_neg_vin}", tokens["driver"], 1)
        check(s == 200 and ids == set(), f"driver negative {driver_neg_vin}: no Findings ({s} {len(ids)})")
        check(all(c in by["vehicle_id"][veh] for c in cursors),
              f"driver negative at page_size=1: every cursor names the caller's own row ({len(cursors)} cursors)")

        s, ids, _ = _all_pages(base, f"/findings/vehicle/{owner_vin}", tokens["owner"], 50)
        want = by["owner_id"][owner] & by["vin"][owner_vin]
        check(s == 200 and ids == want, f"owner positive {owner_vin}: {len(want)} Findings (got {s} {len(ids)})")
        s, ids, cursors = _all_pages(base, f"/findings/vehicle/{owner_neg_vin}", tokens["owner"], 1)
        check(s == 200 and ids == set(), f"owner negative {owner_neg_vin}: no Findings ({s} {len(ids)})")
        check(bool(cursors) and all(c in by["owner_id"][owner] for c in cursors),
              f"owner negative at page_size=1: cursors walk only the caller's own rows ({len(cursors)} cursors)")
    finally:
        for user in created:
            try:
                idp.admin_delete_user(UserPoolId=pool, Username=user)
            except Exception as exc:  # noqa: BLE001
                FAILURES.append(f"cleanup: could not delete {user}: {exc}")
        left = idp.list_users(UserPoolId=pool, Filter=f'email ^= "{PREFIX}"')["Users"]
        check(not left, f"cleanup: {len(created)} users deleted, {len(left)} left under {PREFIX}")

    print(f"\n{'ALL PASS' if not FAILURES else f'{len(FAILURES)} FAILED'}")
    return 0 if not FAILURES else 1


if __name__ == "__main__":
    sys.exit(main())
