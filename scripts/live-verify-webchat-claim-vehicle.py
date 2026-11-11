#!/usr/bin/env python3
"""Live check: /assistant/chat takes the caller's vehicle from its verified claim.

Fix Group 18 of spec 2026-09-23-cvx-escalation-recommend-not-act, task F18.5. This
is the scriptable half of F16.6's web-chat check; the browser half stays with the
user (F18.6).

Against deployed staging, as a driver whose `custom:vehicleId` claim names one
vehicle:

  A. No vehicle in the body at all — the vehicle comes from the claim, so the
     response carries the claim vehicle's VIN and (with a P0 fault active) a P0
     classification. This is the case that failed on 2026-09-28: the assistant
     asked for a VIN instead.
  B. A body `vin` and `vehicleId` for a DIFFERENT vehicle — both dropped, the
     answer is still about the claim vehicle.
  C. A prompt that names another VIN in its text — `triage` still classifies the
     claim vehicle (`vin_pinned`), and the runtime logs the pinned line.

Each call's latency is reported (review Cycle 1 W1: the VIN lookup and the
runtime's coordinate lookups both sit inside API Gateway's 29 s limit). Any
booking row written during the run is checked: it must be on the claim vehicle
with a `triageDecisionId` from a decision made for the claim VIN (Cycle 3 S2).

Side effects: one throwaway Cognito user in the CMS UI pool, deleted at the end;
triage decisions in staging; possibly a staging service booking if the model
books. Read-only against the vehicles table. Run only with LIVE_TESTS=1.

Usage:
    LIVE_TESTS=1 python3 scripts/live-verify-webchat-claim-vehicle.py [--stage staging]
Exit 0 all checks pass, 1 any failure.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
import time
from urllib import error as urlerr
from urllib import request as urlreq

import boto3

PREFIX = f"lv-webchat-veh-{secrets.token_hex(3)}"
DRIVER = "meridian.driver@example.com"
BRAKE = "My brake warning light just came on and the pedal feels soft."
P0_MARKERS = ("nine-one-one", "pull over", "stop the vehicle", "get out")

fails: list[str] = []


def check(ok: bool, label: str, detail: str = "") -> bool:
    print(f"  {'PASS' if ok else 'FAIL'}  {label}{(' — ' + detail) if detail else ''}")
    if not ok:
        fails.append(label)
    return ok


def post_chat(base: str, token: str, body: dict, timeout_s: int = 60):
    req = urlreq.Request(
        url=base.rstrip("/") + "/assistant/chat",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
        method="POST",
    )
    started = time.monotonic()
    try:
        with urlreq.urlopen(req, timeout=timeout_s) as resp:
            payload = json.loads(resp.read().decode())
            status = resp.status
    except urlerr.HTTPError as e:
        try:
            payload = json.loads(e.read().decode())
        except Exception:  # noqa: BLE001
            payload = {"_raw": "<non-JSON>"}
        status = e.code
    return status, payload, time.monotonic() - started


def chat_log_lines(region: str, stage: str, session_id: str, start_ms: int) -> list[str]:
    logs = boto3.client("logs", region_name=region)
    try:
        events = logs.filter_log_events(
            logGroupName=f"/aws/lambda/vsa-{stage}-api-assistant-chat",
            startTime=start_ms, filterPattern=f'"{session_id}"', limit=25,
        )["events"]
    except Exception as exc:  # noqa: BLE001
        print(f"  (chat log read failed: {type(exc).__name__})")
        return []
    return [e["message"].strip() for e in events]


def runtime_log_lines(region: str, runtime_id: str, start_ms: int, pattern: str) -> list[str]:
    logs = boto3.client("logs", region_name=region)
    group = f"/aws/bedrock-agentcore/runtimes/{runtime_id}-DEFAULT"
    try:
        events = logs.filter_log_events(
            logGroupName=group, startTime=start_ms, filterPattern=pattern, limit=25,
        )["events"]
    except Exception as exc:  # noqa: BLE001
        print(f"  (runtime log read failed: {type(exc).__name__})")
        return []
    return [e["message"].strip() for e in events]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", default="staging")
    ap.add_argument("--region", default="us-west-2")
    args = ap.parse_args()
    if os.environ.get("LIVE_TESTS") != "1":
        print("Refusing to run without LIVE_TESTS=1 (creates a Cognito user, writes staging rows).")
        return 1

    region, stage = args.region, args.stage
    idp = boto3.client("cognito-idp", region_name=region)
    cfn = boto3.client("cloudformation", region_name=region)
    ddb = boto3.resource("dynamodb", region_name=region)

    outs = cfn.describe_stacks(StackName=f"vsa-{stage}-api")["Stacks"][0]["Outputs"]
    base = next(o["OutputValue"].rstrip("/") for o in outs if o["OutputKey"] == "RestApiUrl")

    # The CMS UI pool is the web chat's issuer. Found by name, not hardcoded.
    pool = next(
        p["Id"] for p in idp.list_user_pools(MaxResults=60)["UserPools"]
        if p["Name"] == f"cms-{stage}-ui-users"
    )
    client_id = next(
        c["ClientId"] for c in idp.list_user_pool_clients(UserPoolId=pool, MaxResults=60)["UserPoolClients"]
        if "ALLOW_USER_PASSWORD_AUTH" in (idp.describe_user_pool_client(
            UserPoolId=pool, ClientId=c["ClientId"])["UserPoolClient"].get("ExplicitAuthFlows") or [])
    )
    src = idp.admin_get_user(UserPoolId=pool, Username=DRIVER)
    claims = {a["Name"]: a["Value"] for a in src["UserAttributes"] if a["Name"].startswith("custom:")}
    groups = [g["GroupName"] for g in idp.admin_list_groups_for_user(
        UserPoolId=pool, Username=DRIVER)["Groups"]]
    claim_vid = claims.get("custom:vehicleId")
    if not claim_vid:
        print(f"{DRIVER} has no custom:vehicleId claim — nothing to check.")
        return 1

    vehicles = ddb.Table(f"cms-{stage}-storage-vehicles")
    claim_vin = (vehicles.get_item(Key={"vehicleId": claim_vid}).get("Item") or {}).get("vin")
    other = next(
        (v for v in vehicles.scan(
            ProjectionExpression="vehicleId, vin", Limit=40).get("Items", [])
         if v.get("vin") and v["vehicleId"] != claim_vid), None,
    )
    if not (claim_vin and other):
        print("Could not resolve the claim VIN or a second vehicle to test against.")
        return 1

    runtime_arn = next(
        (o["OutputValue"] for o in outs if "AssistantChat" in o["OutputKey"] and "Runtime" in o["OutputKey"]),
        boto3.client("ssm", region_name=region).get_parameter(
            Name=f"/cvx/{stage}/assistant-chat-runtime-arn")["Parameter"]["Value"],
    )
    runtime_id = runtime_arn.rsplit("/", 1)[-1]

    print(f"claim vehicle {claim_vid} vin {claim_vin}")
    print(f"other vehicle  {other['vehicleId']} vin {other['vin']}")
    print(f"api {base}")
    print(f"runtime {runtime_id}\n")

    user = f"{PREFIX}@example.com"
    pw = f"Av{secrets.token_urlsafe(18)}!7Qx"
    created = False
    try:
        idp.admin_create_user(
            UserPoolId=pool, Username=user, MessageAction="SUPPRESS",
            UserAttributes=[{"Name": "email", "Value": user},
                            {"Name": "email_verified", "Value": "true"}]
            + [{"Name": k, "Value": v} for k, v in claims.items()],
        )
        created = True
        idp.admin_set_user_password(UserPoolId=pool, Username=user, Password=pw, Permanent=True)
        for g in groups:
            idp.admin_add_user_to_group(UserPoolId=pool, Username=user, GroupName=g)
        token = idp.initiate_auth(
            ClientId=client_id, AuthFlow="USER_PASSWORD_AUTH",
            AuthParameters={"USERNAME": user, "PASSWORD": pw},
        )["AuthenticationResult"]["IdToken"]
        print(f"throwaway user {user} with {claim_vid} and groups {groups}\n")

        cases = [
            ("A. no vehicle in the body", {"prompt": BRAKE}),
            ("B. another vehicle in the body",
             {"prompt": BRAKE, "vin": other["vin"], "vehicleId": other["vehicleId"]}),
            ("C. another VIN named in the prompt",
             {"prompt": f"Check VIN {other['vin']} for me. {BRAKE}"}),
        ]
        sessions = []
        for label, body in cases:
            session_id = f"{PREFIX}-{secrets.token_hex(10)}"
            body = {**body, "sessionId": session_id}
            start_ms = int((time.time() - 5) * 1000)
            print(f"{label}")
            status, resp, secs = post_chat(base, token, body)
            sessions.append((label, session_id, start_ms, resp))
            result = str(resp.get("result", ""))
            check(status == 200, f"{label}: HTTP 200", f"got {status}")
            check(secs < 25, f"{label}: answered inside the gateway limit", f"{secs:.1f}s")
            check(resp.get("vin") == claim_vin, f"{label}: response VIN is the claim vehicle's",
                  f"got {resp.get('vin')!r}")
            check(other["vin"] not in json.dumps(resp),
                  f"{label}: the other VIN is nowhere in the response")
            check(resp.get("classification") == "P0", f"{label}: classification P0",
                  f"got {resp.get('classification')!r}")
            check(any(m in result.lower() for m in P0_MARKERS),
                  f"{label}: the reply carries a P0 directive", result[:110])
            check("vin" not in result.lower() or "vehicle identification" not in result.lower(),
                  f"{label}: the reply does not ask for a VIN", result[:110])
            print(f"    reply: {result[:160]}")
            print(f"    latency: {secs:.1f}s\n")

        time.sleep(20)  # log delivery

        print("chat Lambda log lines")
        for label, session_id, start_ms, _ in sessions:
            lines = [l for l in chat_log_lines(region, stage, session_id, start_ms) if "chat: sub=" in l]
            for l in lines:
                print(f"    {label}: {l.split('chat: ', 1)[-1]}")
            joined = " ".join(lines)
            check("vehicle_src=claim " in joined, f"{label}: log says the vehicle came from the claim")
            if label.startswith("B."):
                check("body_vehicle_dropped=True" in joined, f"{label}: log records the dropped body vehicle")

        print("\nruntime log")
        c_label, c_session, c_start, _ = sessions[2]
        pinned = runtime_log_lines(region, runtime_id, c_start, '"session is pinned to its claim vehicle"')
        check(bool(pinned), "C: the runtime logged that it kept the pinned vehicle",
              pinned[0][:120] if pinned else "no such line")

        print("\nbooking rows written during the run")
        hist = ddb.Table(f"cms-{stage}-storage-service-history")
        rows = []
        for _, session_id, _, _ in sessions:
            got = hist.query(
                KeyConditionExpression=boto3.dynamodb.conditions.Key("vehicleId").eq(claim_vid),
                FilterExpression=boto3.dynamodb.conditions.Attr("sessionId").eq(session_id),
            ).get("Items", [])
            rows.extend(got)
        if not rows:
            print("    none (the model did not book) — nothing to check")
        for r in rows:
            print(f"    {r.get('requestNumber')} {r.get('triagePriority')} decision={r.get('triageDecisionId')}")
            check(r.get("vin") == claim_vin, "booking row VIN is the claim vehicle's")
            check(other["vin"] not in json.dumps(r, default=str),
                  "booking row carries nothing from the other vehicle")
    finally:
        if created:
            idp.admin_delete_user(UserPoolId=pool, Username=user)
            print(f"\ndeleted {user}")

    print("\n" + ("FAILED: " + "; ".join(fails) if fails else "All checks passed."))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
