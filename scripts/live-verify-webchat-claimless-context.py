#!/usr/bin/env python3
"""Live check: web chat works for a caller with no tenant claim, and carries screen context.

issues/2026-09-29-web-chat-triage-401-claimless-callers/. Before the fix every
web-chat turn that called `triage` ended in the fail-closed "triage system is
unavailable" reply, because /triage returned 401 to callers whose token has no
`custom:tenantId` (15 of 17 CMS staging users). This drives the deployed
/assistant/chat route as such a caller, the way the CMS web chat does:

  A. Vehicle page, brake symptom: no refusal script; /triage resolved the
     caller to the default tenant (api-triage log `source=default`).
  B. Vehicle page, booking request: no refusal script.
  D. Vehicle page, "why is my check engine light on": the reply names the
     vehicle's active codes, read by get_active_dtcs
     (issues/2026-09-29-agent-cannot-name-active-dtcs/).
  C. Fleet page, "which vehicles should we replace soon?" with the fleet_manager
     hint: fleet_lifecycle read Fleet Intelligence (200), the reply carries
     figures, the runtime built fleet_manager, and the chat Lambda logged the page.
  E. Fleet page, "which vehicles cost the most per mile?": fleet_cost_outliers
     read Fleet Intelligence (200) and the reply carries figures.

Side effects: one throwaway Cognito user in the CMS UI pool (group
`fleet-viewer`, no tenant claim), deleted at the end; triage decisions in
staging; possibly a staging service booking. Run only with LIVE_TESTS=1.

Usage:
    LIVE_TESTS=1 python3 scripts/live-verify-webchat-claimless-context.py [--stage staging] [--vin MRDN0000000000015]
Exit 0 all checks pass, 1 any failure.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import secrets
import sys
import time
from urllib import error as urlerr
from urllib import request as urlreq

import boto3

PREFIX = f"lv-webchat-ctx-{secrets.token_hex(3)}"
GROUP = "fleet-viewer"
REFUSAL = "the triage system is unavailable"
BRAKE = "My brake warning light just came on and the pedal feels soft."

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
            return resp.status, json.loads(resp.read().decode()), time.monotonic() - started
    except urlerr.HTTPError as e:
        try:
            payload = json.loads(e.read().decode())
        except Exception:  # noqa: BLE001
            payload = {"_raw": "<non-JSON>"}
        return e.code, payload, time.monotonic() - started


def log_lines(group: str, start_ms: int, pattern: str, region: str) -> list[str]:
    logs = boto3.client("logs", region_name=region)
    for _ in range(6):  # CloudWatch delivery lags a few seconds
        try:
            events = logs.filter_log_events(
                logGroupName=group, startTime=start_ms, filterPattern=pattern, limit=25,
            )["events"]
        except Exception as exc:  # noqa: BLE001
            print(f"  (log read failed on {group}: {type(exc).__name__})")
            return []
        if events:
            return [e["message"].strip() for e in events]
        time.sleep(5)
    return []


def sub_of(id_token: str) -> str:
    part = id_token.split(".")[1]
    return json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))).get("sub", "")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", default="staging")
    ap.add_argument("--region", default="us-west-2")
    ap.add_argument("--vin", default="MRDN0000000000015")
    args = ap.parse_args()
    if os.environ.get("LIVE_TESTS") != "1":
        print("Refusing to run without LIVE_TESTS=1 (creates a Cognito user, writes staging rows).")
        return 1

    region, stage, vin = args.region, args.stage, args.vin
    idp = boto3.client("cognito-idp", region_name=region)
    cfn = boto3.client("cloudformation", region_name=region)

    outs = cfn.describe_stacks(StackName=f"vsa-{stage}-api")["Stacks"][0]["Outputs"]
    base = next(o["OutputValue"].rstrip("/") for o in outs if o["OutputKey"] == "RestApiUrl")
    pool = next(
        p["Id"] for p in idp.list_user_pools(MaxResults=60)["UserPools"]
        if p["Name"] == f"cms-{stage}-ui-users"
    )
    client_id = next(
        c["ClientId"] for c in idp.list_user_pool_clients(UserPoolId=pool, MaxResults=60)["UserPoolClients"]
        if "ALLOW_USER_PASSWORD_AUTH" in (idp.describe_user_pool_client(
            UserPoolId=pool, ClientId=c["ClientId"])["UserPoolClient"].get("ExplicitAuthFlows") or [])
    )
    runtime_arn = boto3.client("ssm", region_name=region).get_parameter(
        Name=f"/cvx/{stage}/assistant-chat-runtime-arn")["Parameter"]["Value"]
    runtime_group = f"/aws/bedrock-agentcore/runtimes/{runtime_arn.rsplit('/', 1)[-1]}-DEFAULT"

    user = f"{PREFIX}@example.com"
    pw = f"Av{secrets.token_urlsafe(18)}!7Qx"
    created = False
    try:
        idp.admin_create_user(
            UserPoolId=pool, Username=user, MessageAction="SUPPRESS",
            UserAttributes=[{"Name": "email", "Value": user}, {"Name": "email_verified", "Value": "true"}],
        )
        created = True
        idp.admin_set_user_password(UserPoolId=pool, Username=user, Password=pw, Permanent=True)
        idp.admin_add_user_to_group(UserPoolId=pool, Username=user, GroupName=GROUP)
        token = idp.initiate_auth(
            ClientId=client_id, AuthFlow="USER_PASSWORD_AUTH",
            AuthParameters={"USERNAME": user, "PASSWORD": pw},
        )["AuthenticationResult"]["IdToken"]
        sub = sub_of(token)
        claims = json.loads(base64.urlsafe_b64decode(token.split(".")[1] + "=="))
        print(f"throwaway user {user} group {GROUP} sub {sub}")
        check("custom:tenantId" not in claims, "token has no tenant claim (the failing case)")
        print(f"api {base}\n")

        vehicle_screen = {"page": "vehicle-detail", "label": "Vehicle detail"}
        cases = [
            ("A", "vehicle page, brake symptom",
             {"prompt": BRAKE, "vin": vin, "screenContext": vehicle_screen}),
            ("B", "vehicle page, booking",
             {"prompt": f"Book service for {vin} next Tuesday.", "vin": vin, "screenContext": vehicle_screen}),
            ("D", "vehicle page, check engine light",
             {"prompt": "Why is my check engine light on right now?", "vin": vin, "screenContext": vehicle_screen}),
            ("C", "fleet page, replacement timing",
             {"prompt": "Which vehicles should we replace soon?", "personaId": "fleet_manager",
              "screenContext": {"page": "fleet-intelligence", "label": "Fleet intelligence"}}),
            ("E", "fleet page, cost per mile",
             {"prompt": "Which vehicles cost the most per mile?", "personaId": "fleet_manager",
              "screenContext": {"page": "fleet-intelligence", "label": "Fleet intelligence"}}),
        ]
        for key, label, body in cases:
            sid = f"lv-ctx-{key}-{secrets.token_hex(8)}"
            start_ms = int(time.time() * 1000) - 2000
            status, resp, secs = post_chat(base, token, {"sessionId": sid, **body})
            result = str(resp.get("result", ""))
            print(f"{key}. {label}  ({secs:.1f}s, classification={resp.get('classification')})")
            print(f"     reply: {result[:220].replace(chr(10), ' ')}")
            check(status == 200, f"{key} HTTP 200", str(status))
            check(REFUSAL not in result.lower(), f"{key} no 'triage unavailable' refusal")
            if key in ("A", "B"):
                triage_log = log_lines(f"/aws/lambda/vsa-{stage}-api-triage", start_ms, f'"sub={sub}"', region)
                check(any("source=default" in m for m in triage_log),
                      f"{key} /triage resolved the default tenant", f"{len(triage_log)} line(s)")
                check(not any("refused 401" in m for m in triage_log), f"{key} /triage did not refuse 401")
            if key == "A":
                check(resp.get("classification") in ("P0", "P1", "P2", "P3"),
                      "A classification returned", str(resp.get("classification")))
            if key == "D":
                # issues/2026-09-29-agent-cannot-name-active-dtcs/: the reply must name
                # the vehicle's active codes, read by get_active_dtcs.
                rt = log_lines(runtime_group, start_ms, '"get_active_dtcs: session="', region)
                check(any("status=complete" in m for m in rt), "D runtime read the codes (get_active_dtcs)",
                      rt[-1][-120:] if rt else "no log line")
                active = [c for c in ("P0420", "B1234") if c in result]
                check(bool(active) or "catalyst" in result.lower(),
                      "D reply names an active code", ",".join(active) or "none named")
                check("can't" not in result.lower() or "code" not in result.lower()[:200],
                      "D reply does not say it can't see the codes")
            if key in ("C", "E"):
                # issues/2026-09-29-fleet-manager-tools-call-removed-cms-routes/
                tool = "fleet_lifecycle" if key == "C" else "fleet_cost_outliers"
                rt_tool = log_lines(runtime_group, start_ms, f'"{tool}: GET"', region)
                check(any("status=200" in m for m in rt_tool), f"{key} {tool} read Fleet Intelligence (HTTP 200)",
                      rt_tool[-1][-100:] if rt_tool else "no log line")
                check("not configured" not in result.lower(), f"{key} reply is not 'not configured'")
                check(any(ch.isdigit() for ch in result), f"{key} reply carries figures")
            if key == "C":
                rt = log_lines(runtime_group, start_ms, '"persona=fleet_manager"', region)
                check(bool(rt), "C runtime built the fleet_manager persona")
                chat = log_lines(f"/aws/lambda/vsa-{stage}-api-assistant-chat", start_ms,
                                 f'"chat: screen page=fleet-intelligence"', region)
                check(bool(chat), "C chat Lambda forwarded screenContext")
            print()
    finally:
        if created:
            idp.admin_delete_user(UserPoolId=pool, Username=user)
            print(f"deleted throwaway user {user}")

    print(f"\n{'ALL PASS' if not fails else 'FAILED: ' + ', '.join(fails)}")
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(main())
