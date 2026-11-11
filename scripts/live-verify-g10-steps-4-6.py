#!/usr/bin/env python3
"""AVX core Group 10, Steps 4–6, against real staging.

Spec ``.kiro/specs/2026-09-18-avx-tier2-ios-cards-and-actions`` § Group 10 / spec.md
"Live-verification path — AVX core's slice (Rev 6)". Steps 1–3 are recorded in that
spec's ``verification-run.md``.

  Step 4  GET /findings/owner/{owner_id} with an OWNER's token. The principal is a
          throwaway CMS-pool user holding exactly the owner standing claim
          (``custom:customerId=<owner_id>`` + group ``vehicle-owner``), the same
          construction as the standing-claims live check Part D. The Finding must come
          back and validate against ``finding.schema.json``.
  Step 5  POST /findings/{id}/approve target_system=dms with the platform-admin +
          fleet-operator principal (the only callers DMS's create-RO admits). The Action
          row must exist, DMS must hold a repair order at ``target_ref``, and that RO must
          carry the Action's correlation id.
  Step 6  The Action moved pending_execution → in_progress, with a fresh updated_at and
          an executor-authored audit entry, and validates against ``action.schema.json``.

Side effects, printed: one Action row in ``vsa-staging-avx-actions`` and one Draft RO in
``dms-staging-repair-orders``. Both Cognito users are deleted in ``finally``. Tokens are
never printed.

Usage: ``LIVE_TESTS=1 .venv/bin/python scripts/live-verify-g10-steps-4-6.py [--owner CUST-…]``
"""
from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import json
import os
import pathlib
import secrets
import sys
import time
import urllib.error
import urllib.request

REGION = "us-west-2"
STAGE = "staging"
ROOT = pathlib.Path(__file__).resolve().parents[1]
failures: list[str] = []


def check(ok: bool, msg: str) -> None:
    print(("  PASS " if ok else "  FAIL ") + msg)
    if not ok:
        failures.append(msg)


def _ssm(name: str) -> str:
    import boto3
    return boto3.client("ssm", region_name=REGION).get_parameter(Name=name)["Parameter"]["Value"].strip()


@contextlib.contextmanager
def principal(label: str, groups: list[str], attrs: dict[str, str]):
    """Ephemeral CMS-pool user; yields an ID token. Deleted in finally."""
    import boto3

    idp = boto3.client("cognito-idp", region_name=REGION)
    pool = _ssm(f"/cvx/{STAGE}/voice-auth-user-pool-id")
    client = None
    for c in idp.list_user_pool_clients(UserPoolId=pool, MaxResults=30)["UserPoolClients"]:
        d = idp.describe_user_pool_client(UserPoolId=pool, ClientId=c["ClientId"])["UserPoolClient"]
        if "ALLOW_USER_PASSWORD_AUTH" in (d.get("ExplicitAuthFlows") or []):
            client = d
            break
    if client is None:
        raise SystemExit("no USER_PASSWORD_AUTH client on the CMS pool")
    user = f"avx-g10-{label}-{int(time.time())}@example.invalid"
    pw = f"Av{secrets.token_urlsafe(18)}!7Qx"
    created = False
    try:
        idp.admin_create_user(
            UserPoolId=pool, Username=user, MessageAction="SUPPRESS",
            UserAttributes=[{"Name": "email", "Value": user},
                            {"Name": "email_verified", "Value": "true"},
                            *({"Name": k, "Value": v} for k, v in attrs.items())])
        created = True
        idp.admin_set_user_password(UserPoolId=pool, Username=user, Password=pw, Permanent=True)
        for g in groups:
            idp.admin_add_user_to_group(UserPoolId=pool, Username=user, GroupName=g)
        auth = idp.initiate_auth(ClientId=client["ClientId"], AuthFlow="USER_PASSWORD_AUTH",
                                 AuthParameters={"USERNAME": user, "PASSWORD": pw})
        print(f"  principal {label}: groups={groups} attrs={sorted(attrs)} (ephemeral)")
        yield auth["AuthenticationResult"]["IdToken"]
    finally:
        if created:
            try:
                idp.admin_delete_user(UserPoolId=pool, Username=user)
                print(f"  principal {label}: deleted")
            except Exception as exc:  # noqa: BLE001
                print(f"  principal {label}: DELETE FAILED ({type(exc).__name__}) — "
                      f"aws cognito-idp admin-delete-user --user-pool-id {pool} --username {user}",
                      file=sys.stderr)


def call(base: str, method: str, path: str, token: str, body: dict | None = None):
    req = urllib.request.Request(
        base + path, method=method,
        headers={"Authorization": token, "Content-Type": "application/json"},
        data=json.dumps(body).encode() if body is not None else None)
    try:
        with urllib.request.urlopen(req, timeout=35) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except ValueError:
            return e.code, {}


def validator(name: str):
    import jsonschema
    schema = json.loads((ROOT / "agents" / "tier2" / "schemas" / name).read_text())
    return jsonschema.Draft202012Validator(
        schema, format_checker=jsonschema.Draft202012Validator.FORMAT_CHECKER)


def main() -> int:
    if os.environ.get("LIVE_TESTS") != "1":
        print("SKIPPED — set LIVE_TESTS=1")
        return 0
    ap = argparse.ArgumentParser()
    ap.add_argument("--owner", default="CUST-004000DD")
    ap.add_argument("--dealer", default="dealer-seattle")
    args = ap.parse_args()

    import boto3
    from boto3.dynamodb.types import TypeDeserializer

    outs = boto3.client("cloudformation", region_name=REGION).describe_stacks(
        StackName=f"vsa-{STAGE}-api")["Stacks"][0]["Outputs"]
    base = next(o["OutputValue"] for o in outs if o["OutputKey"] == "RestApiUrl").rstrip("/")
    ddb = boto3.client("dynamodb", region_name=REGION)
    des = TypeDeserializer()
    started = dt.datetime.now(dt.timezone.utc)
    print(f"run start {started.isoformat(timespec='seconds')}  api={base}")

    # ── Step 4 ──────────────────────────────────────────────────────────────
    print("\nStep 4 — owner reads own Findings")
    with principal("owner", ["vehicle-owner"], {"custom:customerId": args.owner}) as tok:
        s, body = call(base, "GET", f"/findings/owner/{args.owner}", tok)
        s_other, _ = call(base, "GET", "/findings/owner/CUST-0000BEEF", tok)
    findings = body.get("findings", [])
    check(s == 200, f"GET /findings/owner/{args.owner} as the owner → {s}")
    check(s_other == 403, f"same owner reading another party → {s_other} (expect 403)")
    check(len(findings) >= 1, f"{len(findings)} Finding(s) returned")
    if not findings:
        return 1
    finding = findings[0]
    fv = validator("finding.schema.json")
    ferr = [e.message for e in fv.iter_errors(finding)]
    check(not ferr, f"Finding {finding.get('finding_id')} validates against finding.schema.json {ferr[:2]}")
    check(bool(finding.get("computed_at")), f"computed_at={finding.get('computed_at')}")
    check(bool(finding.get("finding_key")), "finding_key populated")
    check(len(finding.get("evidence") or []) > 0, f"evidence[] has {len(finding.get('evidence') or [])}")
    check(bool((finding.get("scope") or {}).get("vin")), f"VIN-scoped: {finding.get('scope')}")

    # ── Step 5 ──────────────────────────────────────────────────────────────
    print("\nStep 5 — approve → DmsExecutor → DMS repair order")
    fid = finding["finding_id"]
    with principal("fleetop", ["platform-admin", "fleet-operator"], {}) as tok:
        t0 = time.monotonic()
        s, action = call(base, "POST", f"/findings/{fid}/approve", tok,
                         {"target_system": "dms", "action_kind": "book_combined_visit",
                          "parameters": {"dealer_id": args.dealer}})
        elapsed = time.monotonic() - t0
    check(s == 200, f"POST approve → {s} in {elapsed:.1f}s {action.get('error', '')}")
    aid = action.get("action_id", "")
    ro = action.get("target_ref")
    print(f"  action_id={aid} target_ref={ro} status={action.get('status')}")
    row = ddb.get_item(TableName=f"vsa-{STAGE}-avx-actions", Key={"action_id": {"S": aid}},
                       ConsistentRead=True).get("Item") if aid else None
    stored = {k: des.deserialize(v) for k, v in (row or {}).items()}
    check(bool(stored), "Action row exists in vsa-staging-avx-actions")
    check(bool(ro) and stored.get("target_ref") == ro, "target_ref recorded on the row")
    dms_row = ddb.get_item(TableName=f"dms-{STAGE}-repair-orders", Key={"ro_id": {"S": ro or "-"}},
                           ConsistentRead=True).get("Item")
    dms = {k: des.deserialize(v) for k, v in (dms_row or {}).items()}
    check(bool(dms), f"DMS holds repair order {ro}")
    check(dms.get("avx_action_id") == aid.removeprefix("AVX-ACT-"),
          f"RO avx_action_id={dms.get('avx_action_id')} correlates to the Action")
    check(dms.get("vehicle_vin") == finding["scope"]["vin"],
          f"RO VIN {dms.get('vehicle_vin')} is the Finding's VIN")
    check(dms.get("dealer_id") == args.dealer and dms.get("status") == "Draft",
          f"RO dealer={dms.get('dealer_id')} status={dms.get('status')}")

    # ── Step 6 ──────────────────────────────────────────────────────────────
    print("\nStep 6 — Action pending_execution → in_progress, audited")
    check(stored.get("status") == "in_progress", f"status={stored.get('status')}")
    upd = stored.get("updated_at", "")
    try:
        fresh = dt.datetime.fromisoformat(upd.replace("Z", "+00:00")) >= started.replace(microsecond=0)
    except ValueError:
        fresh = False
    check(fresh, f"updated_at={upd} is after run start")
    trail = stored.get("audit_trail") or []
    last = trail[-1] if trail else {}
    check(last.get("from_status") == "pending_execution" and last.get("to_status") == "in_progress"
          and last.get("by") == "executor-dms",
          f"audit_trail[-1]={last}")
    av = validator("action.schema.json")
    aerr = [e.message for e in av.iter_errors(json.loads(json.dumps(stored, default=int)))]
    check(not aerr, f"stored Action validates against action.schema.json {aerr[:2]}")

    print("\nRESULT:", "PASS" if not failures else f"FAIL ({len(failures)})")
    print(f"side effects: Action {aid}; DMS RO {ro}")
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
