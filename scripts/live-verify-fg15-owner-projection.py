#!/usr/bin/env python3
"""Live-verification of Fix Group 15 / F11.2 (standing projection) against real staging.

F11.2's own testing constraint: no task closes on stubbed-client evidence alone. Every
unit test of `agents/tier2/owner_resolver.py` stubs the DynamoDB client, and a stub
accepts any `IndexName`, any attribute name, and any key shape. This script checks the
same code against the real CMS registry and the real AVX findings table.

WHAT EACH PART PROVES — the parts are not interchangeable
=========================================================

  Part A — the resolver's READ PATH is correct against the real registry.
           Runs `resolve_standing_facts` under THIS caller's credentials and
           compares it to an independent oracle written here (Query `vin-index`,
           then GetItem the base row). VINs are DERIVED from the findings table,
           not pinned. Proves index name, key shape and attribute names. Proves
           NOTHING about the Lambda's IAM grant. Safe before any deploy.

  Part B — the DEPLOYED Tier 2 Lambda carries F11.2.
           Checks the deployed package contains `owner_resolver.py`, the Lambda has
           `CMS_VEHICLES_TABLE` set, and its role grants `dynamodb:Query` ONLY on the
           `vin-index` ARN and `dynamodb:GetItem` ONLY on the table ARN. It checks
           the scope of each grant, not just that a statement exists. Read-only.

  Part C — the Lambda actually PROJECTED onto real items.
           For every VIN-scoped Finding: when the oracle resolves a fact, the item
           carries it top-level with the oracle's value, and `by_vin` / `by_owner`
           return the item. When the oracle does not resolve, the attribute is ABSENT
           (never "", never the VIN). Only meaningful after a deploy AND a Tier 2 run
           that wrote or re-converged the items. Before that it fails, which is the
           correct result and shows this part can fail.

USAGE
  LIVE_TESTS=1 AWS_REGION=us-west-2 VSA_STAGE=staging \\
    .venv/bin/python scripts/live-verify-fg15-owner-projection.py [--parts A,B,C,D]

EXIT  0 every selected part passed · 1 a check failed · 2 refused to run
"""
from __future__ import annotations

import argparse
import io
import os
import pathlib
import sys
import urllib.request
import zipfile
from typing import Any, Callable

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import boto3  # noqa: E402

from agents.tier2.owner_resolver import (  # noqa: E402
    FLEET_ID_ATTR,
    SOLD_TO_ATTR,
    VEHICLE_ID_ATTR,
    VIN_INDEX_NAME,
    StandingFacts,
    resolve_standing_facts,
)

# Top-level item attribute <- StandingFacts field. Mirrors writer._STANDING_ATTRS.
PROJECTED = {"owner_id": "owner_id", "fleet_id": "fleet_id", "vehicle_id": "vehicle_id"}
NEGATIVE_VINS = ["NOTAVIN0000000000"]  # a VIN shape the registry cannot hold

failures: list[str] = []


def check(ok: bool, msg: str) -> None:
    print(("  PASS " if ok else "  FAIL ") + msg)
    if not ok:
        failures.append(msg)


def oracle(vin: str, ddb: Any, table: str) -> StandingFacts:
    """Independent registry read. Deliberately not shared with the resolver."""
    r = ddb.query(
        TableName=table,
        IndexName=VIN_INDEX_NAME,
        KeyConditionExpression="vin = :v",
        ExpressionAttributeValues={":v": {"S": vin}},
    )
    if not r.get("Items"):
        return StandingFacts()
    vid = r["Items"][0][VEHICLE_ID_ATTR]["S"]
    row = ddb.get_item(TableName=table, Key={VEHICLE_ID_ATTR: {"S": vid}}).get("Item", {})

    def s(attr: str) -> str | None:
        v = row.get(attr, {}).get("S")
        return v if v and v.strip() and v != vin else None

    return StandingFacts(owner_id=s(SOLD_TO_ATTR), fleet_id=s(FLEET_ID_ATTR), vehicle_id=vid)


def scan_vin_findings(ddb_res: Any, table: str) -> list[dict]:
    t = ddb_res.Table(table)
    items: list[dict] = []
    kw: dict = {}
    while True:
        r = t.scan(**kw)
        items += r["Items"]
        if "LastEvaluatedKey" not in r:
            break
        kw["ExclusiveStartKey"] = r["LastEvaluatedKey"]
    return [i for i in items if (i.get("scope") or {}).get("vin")]


def part_a(findings: list[dict], ddb: Any, table: str,
           resolver: Callable[..., StandingFacts] = resolve_standing_facts) -> dict[str, StandingFacts]:
    print("\nPart A — resolver read path vs independent oracle (caller credentials)")
    vins = sorted({i["scope"]["vin"] for i in findings}) + NEGATIVE_VINS
    truth: dict[str, StandingFacts] = {}
    mismatches = 0
    for vin in vins:
        want = oracle(vin, ddb, table)
        got = resolver(vin, ddb_client=ddb, vehicles_table=table)
        again = resolver(vin, ddb_client=ddb, vehicles_table=table)
        truth[vin] = want
        if got != want or got != again or vin in tuple(got):
            mismatches += 1
            print(f"    mismatch vin=…{vin[-4:]} resolver={got} oracle={want} reread={again}")
    resolved = [v for v, f in truth.items() if f.owner_id]
    unresolved = [v for v, f in truth.items() if not f.any_resolved]
    check(mismatches == 0, f"resolver == oracle, stable on re-read, never the VIN ({len(vins)} VINs)")
    # Floors derived from what was read: both branches must have been exercised live.
    check(len(resolved) >= 1, f"at least one VIN resolved an owner live ({len(resolved)})")
    check(len(unresolved) >= 1, f"at least one VIN resolved nothing live ({len(unresolved)})")
    fault = resolver("DEMO0000000000001", ddb_client=ddb, vehicles_table=table + "-does-not-exist")
    check(not fault.any_resolved, "missing registry table fails closed without raising")
    return truth


def part_b(fn_name: str, region: str, table_arn: str) -> None:
    print("\nPart B — deployed Tier 2 Lambda carries F11.2 (read-only)")
    lam = boto3.client("lambda", region_name=region)
    fn = lam.get_function(FunctionName=fn_name)
    env = fn["Configuration"].get("Environment", {}).get("Variables", {})
    check(bool(env.get("CMS_VEHICLES_TABLE")), f"CMS_VEHICLES_TABLE set on Lambda (={env.get('CMS_VEHICLES_TABLE')})")
    with urllib.request.urlopen(fn["Code"]["Location"], timeout=60) as resp:  # noqa: S310 (presigned AWS URL)
        names = zipfile.ZipFile(io.BytesIO(resp.read())).namelist()
    check("agents/tier2/owner_resolver.py" in names, "deployed package contains agents/tier2/owner_resolver.py")

    iam = boto3.client("iam")
    role = fn["Configuration"]["Role"].split("/")[-1]
    stmts: list[dict] = []
    for p in iam.list_role_policies(RoleName=role)["PolicyNames"]:
        doc = iam.get_role_policy(RoleName=role, PolicyName=p)["PolicyDocument"]
        stmts += doc["Statement"] if isinstance(doc["Statement"], list) else [doc["Statement"]]

    def as_list(x: Any) -> list:
        return x if isinstance(x, list) else [x]

    def grants(action: str) -> list[list[str]]:
        return [as_list(s.get("Resource", [])) for s in stmts
                if s.get("Effect") == "Allow"
                and any(a in (action, "dynamodb:*", "*") for a in as_list(s.get("Action", [])))]

    cms = [r for rs in grants("dynamodb:Query") + grants("dynamodb:GetItem") for r in rs
           if "storage-vehicles" in r or r == "*"]
    q = [rs for rs in grants("dynamodb:Query") if any("storage-vehicles" in r for r in rs)]
    g = [rs for rs in grants("dynamodb:GetItem") if any("storage-vehicles" in r for r in rs)]
    check(any(rs == [f"{table_arn}/index/{VIN_INDEX_NAME}"] for rs in q),
          "Query granted on exactly the vin-index ARN")
    check(any(rs == [table_arn] for rs in g), "GetItem granted on exactly the table ARN")
    check("*" not in cms and all(r in (table_arn, f"{table_arn}/index/{VIN_INDEX_NAME}") for r in cms),
          "no wider CMS registry grant (no *, no other index, no other table)")


def part_c(findings: list[dict], truth: dict[str, StandingFacts], ddb_res: Any, table: str) -> None:
    print("\nPart C — projection on real items + index reachability")
    from boto3.dynamodb.conditions import Key
    t = ddb_res.Table(table)
    bad = 0
    for item in findings:
        vin = item["scope"]["vin"]
        want = truth[vin]
        for attr, field in PROJECTED.items():
            exp, got = getattr(want, field), item.get(attr)
            if got != exp or got in ("", vin):
                bad += 1
                print(f"    finding={item['finding_id'][:8]} vin=…{vin[-4:]} {attr}: item={got!r} oracle={exp!r}")
    check(bad == 0, f"every VIN Finding's projection matches the oracle ({len(findings)} items)")
    sample = next((i for i in findings if truth[i["scope"]["vin"]].owner_id), None)
    if sample is None:
        check(False, "no Finding with a resolvable owner to test index reachability")
        return
    fid, owner = sample["finding_id"], truth[sample["scope"]["vin"]].owner_id
    by_vin = t.query(IndexName="by_vin", KeyConditionExpression=Key("vin").eq(sample["scope"]["vin"]))["Items"]
    by_owner = t.query(IndexName="by_owner", KeyConditionExpression=Key("owner_id").eq(owner))["Items"]
    check(any(i["finding_id"] == fid for i in by_vin), "by_vin returns a resolvable-owner Finding")
    check(any(i["finding_id"] == fid for i in by_owner), "by_owner returns the same Finding")


def _http(url: str, token: str, method: str = "GET", body: Any = None) -> int:
    """Make a single HTTP call and return the status code without raising."""
    import json as _json
    headers: dict[str, str] = {"Authorization": token}
    data: bytes | None = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = _json.dumps(body).encode()
    req = urllib.request.Request(url, method=method, headers=headers, data=data)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:  # noqa: S310
            return resp.status
    except urllib.error.HTTPError as exc:
        return exc.code
    except Exception:  # noqa: BLE001
        return -1


def _find_by_gsi(ddb_res: Any, table: str, index_name: str, key_attr: str, key_val: str) -> dict | None:
    """Return the first Finding from *table* matching the GSI, or None."""
    from boto3.dynamodb.conditions import Key
    t = ddb_res.Table(table)
    r = t.query(IndexName=index_name, KeyConditionExpression=Key(key_attr).eq(key_val), Limit=1)
    return r["Items"][0] if r.get("Items") else None


def _delete_actions_for(finding_id: str, created_by_prefix: str, ddb_res: Any, actions_table: str) -> int:
    """Delete any Action rows the Part D run may have created. Returns count deleted.

    Fully paginates the scan so rows on page 2+ are not silently missed (W2 fix).
    """
    from boto3.dynamodb.conditions import Attr
    t = ddb_res.Table(actions_table)
    deleted = 0
    kw: dict = {"FilterExpression": Attr("finding_id").eq(finding_id) & Attr("created_by").begins_with(created_by_prefix)}
    while True:
        resp = t.scan(**kw)
        for item in resp.get("Items", []):
            t.delete_item(Key={"action_id": item["action_id"]})
            deleted += 1
        if "LastEvaluatedKey" not in resp:
            break
        kw["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
    return deleted


def part_d(cvx_base: str, stage: str, region: str) -> None:
    """Approve-matrix: 3 throwaway single-standing principals, positive + 2 negatives each.

    Chooses `target_system="none"` (AdvisoryExecutor) because it makes NO external call
    — no DMS request, no SNS, no boto3 client of any kind — so the test can run against
    real staging without depending on DMS or any other downstream being available.

    Three routes:
      owner   — `custom:customerId = CUST-004000DD` + `vehicle-owner` group.
                Positive finding: Finding whose `owner_id == CUST-004000DD` (by_owner GSI).
      driver  — `custom:vehicleId = VEH-VO-001` + `fleet-driver` group.
                Positive finding: Finding whose `vehicle_id == VEH-VO-001` (by_vehicle_id GSI).
      fleet   — `custom:fleetIds = flt-meridian-range-001` + `fleet-operator` group.
                Positive finding: Finding whose `fleet_id == flt-meridian-range-001` (by_fleet GSI).

    For each route:
      1. Positive: POST approve on the route's own Finding → expect 200 (before deploy: 403/503).
      2. Wrong-finding: approve on a Finding whose projections exclude this principal's claim
         value → expect 403 always. Asserted before sending.
      3. Stripped-claim: same principal without the standing claim → expect 403 always.

    Pre-deploy invariants:
      - A missing positive Finding, unprovisioned claim, or missing group is recorded as
        FAILURE (not a note or skip), so the run exits 1 before T4.1 deploys.
      - Positives are expected to return 403/503 before deploy; the script exits 0 only
        when ALL positives return 200 and all negatives return 403.

    Every user is deleted in a `finally`. Every Action row created is also deleted.
    A post-check scans the pool on the `avx-d-` email prefix (not just tracked users)
    and asserts 0 users and 0 Action rows remain.
    """
    import json as _json
    import secrets
    import time

    import boto3

    print("\nPart D — approve matrix with throwaway single-standing principals")
    print("  target_system=none (AdvisoryExecutor — no external call)")

    idp = boto3.client("cognito-idp", region_name=region)
    ddb_res = boto3.resource("dynamodb", region_name=region)

    # Resolve pool_id from SSM (same as provision-avx-e2e-principal.py).
    ssm_name = f"/cvx/{stage}/voice-auth-user-pool-id"
    pool_id = boto3.client("ssm", region_name=region).get_parameter(Name=ssm_name)["Parameter"]["Value"].strip()

    # Find the PASSWORD-auth-enabled app client.
    clients = idp.list_user_pool_clients(UserPoolId=pool_id, MaxResults=30)["UserPoolClients"]
    auth_client: dict | None = None
    for c in clients:
        desc = idp.describe_user_pool_client(
            UserPoolId=pool_id, ClientId=c["ClientId"])["UserPoolClient"]
        if "ALLOW_USER_PASSWORD_AUTH" in (desc.get("ExplicitAuthFlows") or []):
            auth_client = desc
            break
    if not auth_client:
        check(False, "no app client permits USER_PASSWORD_AUTH — cannot mint tokens")
        return

    findings_table = f"vsa-{stage}-avx-findings"
    actions_table = f"vsa-{stage}-avx-actions"

    # ── Discover positive findings by GSI (fully paginated) ───────────────────
    owner_finding = _find_by_gsi(ddb_res, findings_table, "by_owner", "owner_id", "CUST-004000DD")
    fleet_finding = _find_by_gsi(ddb_res, findings_table, "by_fleet", "fleet_id", "flt-meridian-range-001")

    # Driver: scan the full findings table for vehicle_id == VEH-VO-001 (no Limit,
    # fully paginated — W2 fix: Limit=50 capped items examined before filter runs,
    # potentially missing the finding).
    vehicle_id_val = "VEH-VO-001"
    from boto3.dynamodb.conditions import Attr as _Attr
    _ft = ddb_res.Table(findings_table)
    driver_finding: dict | None = None
    _scan_kw: dict = {"FilterExpression": _Attr("vehicle_id").eq(vehicle_id_val)}
    while True:
        _scan_r = _ft.scan(**_scan_kw)
        if _scan_r.get("Items"):
            driver_finding = _scan_r["Items"][0]
            break
        if "LastEvaluatedKey" not in _scan_r:
            break
        _scan_kw["ExclusiveStartKey"] = _scan_r["LastEvaluatedKey"]

    print(f"  owner  finding: {owner_finding['finding_id'][:12] + '…' if owner_finding else 'NOT FOUND'}")
    print(f"  driver finding: {driver_finding['finding_id'][:12] + '…' if driver_finding else 'NOT FOUND'}")
    print(f"  fleet  finding: {fleet_finding['finding_id'][:12] + '…' if fleet_finding else 'NOT FOUND'}")

    # ── Discover negative findings: must have NO standing for the tested principal ──
    # For each route, we need a Finding whose projections exclude that principal's claim.
    # owner negative: owner_id != CUST-004000DD (or owner_id absent)
    # driver negative: vehicle_id != VEH-VO-001 (or vehicle_id absent)
    # fleet negative: fleet_id != flt-meridian-range-001 (or fleet_id absent)
    def _find_negative(
        exclude_attr: str, exclude_val: str, owner_finding: dict | None,
        fleet_finding: dict | None, driver_finding: dict | None,
    ) -> dict | None:
        """Scan findings for one that provably excludes the given claim value.

        F2.1 fix: the negative MUST have exclude_attr PRESENT with a DIFFERENT value.
        A finding where the attribute is absent passes `None != val` but does NOT close
        the 'grant any holder' path — an authorizer that drops the equality and grants any
        holder of the claim on any Finding with a non-null attribute would still return 200.
        Only a present-and-different value falsifies that hypothesis.

        For fleet, _check_approve_scope also checks scope.fleet_id (the nested attribute).
        To fully close the fleet grant path, a negative must also exclude the nested value.
        """
        def _qualifies(f: dict) -> bool:
            val = f.get(exclude_attr)
            if val is None or val == exclude_val:
                return False
            # For fleet_id, also exclude any finding whose scope.fleet_id matches.
            if exclude_attr == "fleet_id":
                nested = (f.get("scope") or {}).get("fleet_id")
                if nested == exclude_val:
                    return False
            return True

        # First try the already-discovered findings from the other routes.
        candidates = [f for f in [owner_finding, fleet_finding, driver_finding]
                      if f is not None and _qualifies(f)]
        if candidates:
            return candidates[0]
        # Fall back to a full scan — paginate to exhaustion (W2 fix preserved).
        _neg_kw: dict = {}
        while True:
            _neg_r = ddb_res.Table(findings_table).scan(**_neg_kw)
            for it in _neg_r.get("Items", []):
                if _qualifies(it):
                    return it
            if "LastEvaluatedKey" not in _neg_r:
                break
            _neg_kw["ExclusiveStartKey"] = _neg_r["LastEvaluatedKey"]
        return None

    neg_for_owner  = _find_negative("owner_id",   "CUST-004000DD",         owner_finding, fleet_finding, driver_finding)
    neg_for_driver = _find_negative("vehicle_id",  "VEH-VO-001",            owner_finding, fleet_finding, driver_finding)
    neg_for_fleet  = _find_negative("fleet_id",    "flt-meridian-range-001", owner_finding, fleet_finding, driver_finding)

    existing_pool_groups = {g["GroupName"] for g in idp.list_groups(UserPoolId=pool_id, Limit=60)["Groups"]}

    ROUTES = [
        ("owner",  "custom:customerId", "CUST-004000DD",           "vehicle-owner", owner_finding,  neg_for_owner),
        ("driver", "custom:vehicleId",  "VEH-VO-001",              "fleet-driver",  driver_finding, neg_for_driver),
        ("fleet",  "custom:fleetIds",   "flt-meridian-range-001",  "fleet-operator", fleet_finding, neg_for_fleet),
    ]

    created_users: list[tuple[str, str, str]] = []  # [(username, sub, route_name)]
    action_ids_to_delete: list[tuple[str, str]] = []  # [(finding_id, created_by_prefix)]

    def _mint(username: str, claim_attr: str, claim_val: str, group: str) -> str | None:
        """Create user, add group, mint token. Returns id_token or None on failure.

        Appends to created_users IMMEDIATELY after admin_create_user succeeds, so a
        failure in the subsequent steps (password set, group add, auth) still leaves a
        record for the post-check cleanup (W2 fix: previously only appended after the
        full _mint returned successfully).
        """
        password = f"Av{secrets.token_urlsafe(18)}!7Qx"
        user_attrs = [
            {"Name": "email", "Value": username},
            {"Name": "email_verified", "Value": "true"},
        ]
        attr_name = claim_attr.replace("custom:", "custom:")
        user_attrs.append({"Name": attr_name, "Value": claim_val})
        idp.admin_create_user(
            UserPoolId=pool_id, Username=username, MessageAction="SUPPRESS",
            UserAttributes=user_attrs,
        )
        # Append immediately after create so cleanup can find leaks (W2 fix).
        created_users.append((username, "", f"claim-{username}"))
        idp.admin_set_user_password(
            UserPoolId=pool_id, Username=username, Password=password, Permanent=True)
        if group in existing_pool_groups:
            idp.admin_add_user_to_group(
                UserPoolId=pool_id, Username=username, GroupName=group)
        else:
            print(f"    NOTE: group '{group}' not yet in pool (expected before T4.1 deploys)")
        auth = idp.initiate_auth(
            ClientId=auth_client["ClientId"], AuthFlow="USER_PASSWORD_AUTH",
            AuthParameters={"USERNAME": username, "PASSWORD": password},
        )
        return auth["AuthenticationResult"]["IdToken"]

    def _mint_stripped(username: str, group: str) -> str | None:
        """Create a user with NO standing claim, only the group (group may also be absent)."""
        password = f"Av{secrets.token_urlsafe(18)}!7Qx"
        idp.admin_create_user(
            UserPoolId=pool_id, Username=username, MessageAction="SUPPRESS",
            UserAttributes=[
                {"Name": "email", "Value": username},
                {"Name": "email_verified", "Value": "true"},
            ],
        )
        # Append immediately after create so cleanup can find leaks (W2 fix).
        created_users.append((username, "", f"strip-{username}"))
        idp.admin_set_user_password(
            UserPoolId=pool_id, Username=username, Password=password, Permanent=True)
        if group in existing_pool_groups:
            idp.admin_add_user_to_group(
                UserPoolId=pool_id, Username=username, GroupName=group)
        auth = idp.initiate_auth(
            ClientId=auth_client["ClientId"], AuthFlow="USER_PASSWORD_AUTH",
            AuthParameters={"USERNAME": username, "PASSWORD": password},
        )
        return auth["AuthenticationResult"]["IdToken"]

    ts = int(time.time())
    try:
        for name, claim_attr, claim_val, group, pos_finding, neg_finding in ROUTES:
            u_claim    = f"avx-d-{name}-{ts}@example.com"
            u_stripped = f"avx-d-{name}-strip-{ts}@example.com"
            print(f"\n  [{name}] claim={claim_attr}={claim_val!r} group={group!r}")

            # ── Pre-route checks: missing prerequisites are FAILURES (C3 fix) ──
            if not pos_finding:
                check(False, f"[{name}] positive: no Finding projected for {name} — cannot run positive (pre-deploy expected; fix before T5.2)")
            if group not in existing_pool_groups:
                check(False, f"[{name}] group '{group}' not yet in pool — standing cannot be provisioned before T4.1")
            if neg_finding is None:
                check(False, f"[{name}] wrong-finding negative: no Finding with {name}'s claim excluded — cannot verify cross-standing denial")

            # Mint principal with the claim.
            token_claim: str | None = None
            sub_claim = ""
            try:
                token_claim = _mint(u_claim, claim_attr, claim_val, group)
                # Decode sub for cleanup tracking.
                payload = token_claim.split(".")[1] + "=="
                sub_claim = _json.loads(__import__("base64").urlsafe_b64decode(payload)).get("sub", "")
            except Exception as exc:  # noqa: BLE001
                exc_str = str(exc)
                if ("InvalidParameterException" in exc_str and "could not be determined" in exc_str
                        and claim_attr.startswith("custom:")):
                    # Schema attribute not yet deployed (T4.1 pending).
                    check(False, f"[{name}] {claim_attr} not yet in pool schema — T4.1 must deploy before this route can run")
                    token_claim = None
                else:
                    check(False, f"[{name}] failed to mint claim principal: {exc}")
                    token_claim = None

            # Mint stripped principal (group only, no standing claim).
            token_stripped: str | None = None
            try:
                token_stripped = _mint_stripped(u_stripped, group)
            except Exception as exc:  # noqa: BLE001
                check(False, f"[{name}] failed to mint stripped principal: {exc}")
                token_stripped = None

            # ── Test 1: positive — approve on own finding (requires 200) ──
            if token_claim and pos_finding:
                fid = pos_finding["finding_id"]
                url = f"{cvx_base}/findings/{fid}/approve"
                body = {"target_system": "none", "action_kind": "acknowledge_only", "parameters": {}}
                status = _http(url, token_claim, method="POST", body=body)
                check(status == 200, f"[{name}] positive: POST /findings/{fid[:8]}…/approve → {status} (want 200; before T4.1 expect 403/503)")
                if status == 200:
                    action_ids_to_delete.append((fid, f"owner-{sub_claim}"))

            # ── Test 2: wrong-finding negative — must be a Finding with NO standing ──
            if token_claim and neg_finding:
                wrong_fid = neg_finding["finding_id"]
                # Assert before sending: the attribute must be PRESENT and DIFFERENT
                # (F2.1 fix: absent-value passes None != val vacuously — requires oracle).
                # For fleet, also close the scope.fleet_id path that _check_approve_scope honours.
                neg_attr_map = {
                    "owner":  ("owner_id",   "CUST-004000DD"),
                    "driver": ("vehicle_id", "VEH-VO-001"),
                    "fleet":  ("fleet_id",   "flt-meridian-range-001"),
                }
                neg_attr, neg_val = neg_attr_map[name]
                neg_present = neg_finding.get(neg_attr)
                check(
                    neg_present is not None and neg_present != neg_val,
                    f"[{name}] wrong-finding oracle: {neg_attr} on wrong-finding must be present and != {neg_val!r}; got {neg_present!r}",
                )
                if name == "fleet":
                    # _check_approve_scope also checks scope.fleet_id (F2.1 fix).
                    nested_fleet = (neg_finding.get("scope") or {}).get("fleet_id")
                    check(
                        nested_fleet != neg_val,
                        f"[fleet] wrong-finding oracle: scope.fleet_id ({nested_fleet!r}) must != {neg_val!r}",
                    )
                url = f"{cvx_base}/findings/{wrong_fid}/approve"
                body = {"target_system": "none", "action_kind": "acknowledge_only", "parameters": {}}
                status = _http(url, token_claim, method="POST", body=body)
                check(status == 403, f"[{name}] wrong-finding: POST /findings/{wrong_fid[:8]}…/approve → {status} (want 403 always)")
                if status == 200:
                    # Unexpected 200: track action for cleanup.
                    action_ids_to_delete.append((wrong_fid, f"owner-{sub_claim}"))

            # ── Test 3: stripped-claim negative → 403 ──
            # F2.1/W1 fix: decode sub_stripped so an unexpected 200 can be tracked for cleanup.
            sub_stripped = ""
            if token_stripped:
                try:
                    payload_stripped = token_stripped.split(".")[1] + "=="
                    sub_stripped = _json.loads(
                        __import__("base64").urlsafe_b64decode(payload_stripped)
                    ).get("sub", "")
                except Exception:  # noqa: BLE001
                    pass
            if token_stripped and pos_finding:
                fid = pos_finding["finding_id"]
                url = f"{cvx_base}/findings/{fid}/approve"
                body = {"target_system": "none", "action_kind": "acknowledge_only", "parameters": {}}
                status = _http(url, token_stripped, method="POST", body=body)
                check(status == 403, f"[{name}] stripped-claim: POST /findings/{fid[:8]}…/approve → {status} (want 403 always)")
                if status == 200:
                    # Track the action so cleanup and post-check can find it (W1 fix).
                    action_ids_to_delete.append((fid, f"owner-{sub_stripped}"))

    finally:
        # ── Clean up Action rows ──
        deleted_actions = 0
        for fid, prefix in action_ids_to_delete:
            deleted_actions += _delete_actions_for(fid, prefix, ddb_res, actions_table)

        # ── Clean up users ──
        deleted_users = 0
        remaining_users = []
        for uname, _, _ in created_users:
            try:
                idp.admin_delete_user(UserPoolId=pool_id, Username=uname)
                deleted_users += 1
            except idp.exceptions.UserNotFoundException:
                deleted_users += 1  # already gone
            except Exception as exc:  # noqa: BLE001
                remaining_users.append(uname)
                print(f"    cleanup FAILED for {uname}: {exc}", file=sys.stderr)
                print(f"    manual: aws cognito-idp admin-delete-user "
                      f"--user-pool-id {pool_id} --username {uname} --region {region}", file=sys.stderr)

        print(f"\n  cleanup: deleted {deleted_users} users, {deleted_actions} action rows")

        # ── Post-check: assert nothing remains ──
        # W2 fix: scan on the avx-d-* EMAIL PREFIX, not just tracked users, so a user
        # that leaked mid-mint (append-before-exception) is caught by the prefix scan.
        paginator = idp.get_paginator("list_users")
        live_users_after: set[str] = set()
        for page in paginator.paginate(UserPoolId=pool_id, Filter='email ^= "avx-d-"'):
            for u in page.get("Users", []):
                for attr in u.get("Attributes", []):
                    if attr["Name"] == "email":
                        live_users_after.add(attr["Value"])
        check(not live_users_after, f"post-check: {len(live_users_after)} avx-d-* user(s) still present: {live_users_after}")

        # Verify no Action rows from this run remain.
        surviving_actions = []
        for fid, prefix in action_ids_to_delete:
            remaining = _delete_actions_for(fid, prefix, ddb_res, actions_table)
            if remaining:
                surviving_actions.append((fid, prefix, remaining))
        check(not surviving_actions, f"post-check: {len(surviving_actions)} Action row(s) remain after cleanup")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--parts", default="A,B,C")
    args = ap.parse_args()
    if os.environ.get("LIVE_TESTS") != "1":
        print("refusing to run: set LIVE_TESTS=1 (this reads real staging resources)")
        return 2
    stage = os.environ.get("VSA_STAGE", "staging")
    region = os.environ.get("AWS_REGION", "us-west-2")
    account = boto3.client("sts").get_caller_identity()["Account"]
    cms_table = f"cms-{stage}-storage-vehicles"
    table_arn = f"arn:aws:dynamodb:{region}:{account}:table/{cms_table}"
    findings_table = f"vsa-{stage}-avx-findings"
    parts = {p.strip().upper() for p in args.parts.split(",")}

    ddb = boto3.client("dynamodb", region_name=region)
    ddb_res = boto3.resource("dynamodb", region_name=region)
    findings = scan_vin_findings(ddb_res, findings_table)
    print(f"stage={stage} region={region} VIN-scoped findings={len(findings)}")
    truth = part_a(findings, ddb, cms_table) if parts & {"A", "C"} else {}
    if "B" in parts:
        part_b(f"vsa-{stage}-tier2-health-agent-{region}", region, table_arn)
    if "C" in parts:
        part_c(findings, truth, ddb_res, findings_table)
    if "D" in parts:
        # Resolve the CVX API base URL from the stack outputs.
        cfn = boto3.client("cloudformation", region_name=region)
        try:
            outs = cfn.describe_stacks(StackName=f"vsa-{stage}-api")["Stacks"][0]["Outputs"]
        except Exception as exc:  # noqa: BLE001
            print(f"cannot describe vsa-{stage}-api: {exc}")
            failures.append("Part D: could not resolve CVX API URL")
            outs = []
        cvx_base = next((o["OutputValue"].rstrip("/") for o in outs if o["OutputKey"] == "RestApiUrl"), "")
        if not cvx_base:
            failures.append(f"Part D: vsa-{stage}-api has no RestApiUrl output")
        else:
            print(f"cvx api: {cvx_base}")
            part_d(cvx_base, stage, region)
    print(f"\n{'FAILED' if failures else 'PASSED'}: {len(failures)} failing check(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
