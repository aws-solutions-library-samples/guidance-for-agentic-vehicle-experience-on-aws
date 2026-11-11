#!/usr/bin/env python3
"""Live check: a driver-confirmed P0 shows the catalog directive as a platform line,
AND books at P0 (not the classifier's grade).

Issue `issues/2026-09-27-p0-safety-lines-p0a80-and-driver-confirmed/` and spec
`.kiro/specs/2026-09-23-cvx-escalation-recommend-not-act/` Fix Group F17.4 /
F17.6(c). Against the deployed staging bidi runtime:

1. Creates a throwaway user in the voice-auth pool with meridian.driver's groups and
   tenant claim (not its vehicle or driver claim), and deletes it at the end.
2. Picks a vehicle the classifier grades P2 or P3 (the confirmation path only runs
   below P1), by calling the real `/triage` endpoint, then binds the user to that
   vehicle (`custom:vehicleId`) and signs in again, because a vehicle-bound token may
   only triage and open a session on its own vehicle.
3. Speaks (macOS `say`, 16 kHz mono PCM) "My engine is on fire and there's smoke
   coming from the hood", waits for the server's `driver_confirmation` step, then
   speaks "Yes."
4. Expects, in order: a `transcript` with `_platform_safety_directive` whose text is
   the `exit_vehicle` first sentence (optionally after the driver's name), then a
   `classification` P0 with source `driver-confirmed`. Records what the model said.
5. After the session ends, polls `cms-{stage}-storage-service-history` for the row
   `call_book_endpoint` wrote on the confirmation path (sessionId filter), and
   asserts `triagePriority="P0"` — the F17.6(c) property. Records the row's
   `requestNumber` and `serviceDate`.

Side effects: one throwaway Cognito user (deleted), triage decisions and one
driver-confirmation audit row in staging, and the single booking row the F17.4 code
writes synchronously on the confirmation path. The booking id is printed and left
in place for audit (matches tasks.md F17.6(c) framing: "record their IDs"). Run only
with LIVE_TESTS=1.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import secrets
import subprocess
import sys
import tempfile
import time
import wave
from pathlib import Path

import boto3
import requests
import websockets
import yaml
from websockets.asyncio.client import connect as ws_connect

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
from agents.supervisor import dtc_response_catalog as cat  # noqa: E402

PREFIX = f"lv-confirmed-p0-{secrets.token_hex(3)}"
FIRE = "My engine is on fire and there's smoke coming from the hood."
CHUNK_MS = 200


def speech_pcm(text: str) -> bytes:
    with tempfile.TemporaryDirectory() as d:
        aiff, wav = Path(d) / "s.aiff", Path(d) / "s.wav"
        subprocess.run(["say", "-o", str(aiff), text], check=True)
        subprocess.run(["afconvert", "-f", "WAVE", "-d", "LEI16@16000", "-c", "1", str(aiff), str(wav)], check=True)
        with wave.open(str(wav), "rb") as w:
            assert w.getnchannels() == 1 and w.getsampwidth() == 2 and w.getframerate() == 16000
            return w.readframes(w.getnframes())


async def send_pcm(ws, pcm: bytes) -> None:
    step = 16000 * 2 * CHUNK_MS // 1000
    for i in range(0, len(pcm), step):
        await ws.send(json.dumps({"type": "audio.chunk", "sampleRate": 16000,
                                  "data": base64.b64encode(pcm[i:i + step]).decode()}))
        await asyncio.sleep(CHUNK_MS / 1000)
    for _ in range(10):  # 2 s of silence so end-of-turn fires
        await ws.send(json.dumps({"type": "audio.chunk", "sampleRate": 16000,
                                  "data": base64.b64encode(b"\x00" * step).decode()}))
        await asyncio.sleep(CHUNK_MS / 1000)


def pick_vin(base: str, token: str, table, tenant: str) -> tuple[str, str, str]:
    """A (vin, vehicleId, classification) the classifier grades P2 or P3."""
    seen = 0
    for page in table.meta.client.get_paginator("scan").paginate(
            TableName=table.name, ProjectionExpression="vin, vehicleId", PaginationConfig={"MaxItems": 200}):
        for item in page.get("Items", []):
            vin, vid = (item.get(k) for k in ("vin", "vehicleId"))
            vin = vin.get("S") if isinstance(vin, dict) else vin
            vid = vid.get("S") if isinstance(vid, dict) else vid
            if not vin or not vid:
                continue
            seen += 1
            r = requests.post(f"{base}/triage", json={"vin": vin, "sessionId": f"{PREFIX}-pick", "tenantId": tenant},
                              headers={"Authorization": f"Bearer {token}"}, timeout=15)
            cls = r.json().get("classification") if r.ok else None
            print(f"  try {vid}: {r.status_code} {cls}")
            if cls in ("P2", "P3"):
                return vin, vid, cls
            if seen >= 40:
                break
    raise SystemExit("no P2/P3 vehicle found in the first 40 tried")


async def run(args) -> int:
    region, stage = args.region, args.stage
    idp = boto3.client("cognito-idp", region_name=region)
    pool = boto3.client("ssm", region_name=region).get_parameter(
        Name=f"/cvx/{stage}/voice-auth-user-pool-id")["Parameter"]["Value"].strip()
    outs = boto3.client("cloudformation", region_name=region).describe_stacks(
        StackName=f"vsa-{stage}-api")["Stacks"][0]["Outputs"]
    base = next(o["OutputValue"].rstrip("/") for o in outs if o["OutputKey"] == "RestApiUrl")
    client_id = next(
        c["ClientId"] for c in idp.list_user_pool_clients(UserPoolId=pool, MaxResults=60)["UserPoolClients"]
        if "ALLOW_USER_PASSWORD_AUTH" in (idp.describe_user_pool_client(
            UserPoolId=pool, ClientId=c["ClientId"])["UserPoolClient"].get("ExplicitAuthFlows") or []))
    md = idp.admin_get_user(UserPoolId=pool, Username="meridian.driver@example.com")
    claims = {a["Name"]: a["Value"] for a in md["UserAttributes"] if a["Name"].startswith("custom:")}
    # Vehicle scope (issues/2026-10-05-bidi-and-triage-accept-any-vehicle/): a token
    # carrying custom:vehicleId may triage and open a session only on that vehicle.
    # The throwaway user starts without the vehicle and driver claims so pick_vin
    # can try other vehicles, then is bound to the vehicle it picked.
    claims.pop("custom:vehicleId", None)
    claims.pop("custom:driverId", None)
    groups = [g["GroupName"] for g in idp.admin_list_groups_for_user(
        UserPoolId=pool, Username="meridian.driver@example.com")["Groups"]]
    arn = yaml.safe_load((REPO / ".bedrock_agentcore.yaml").read_text())["agents"][
        "vsa_supervisor_bidi_staging"]["bedrock_agentcore"]["agent_arn"]

    user = f"{PREFIX}@example.com"
    pw = f"Av{secrets.token_urlsafe(18)}!7Qx"
    created = False
    ok = True
    try:
        idp.admin_create_user(UserPoolId=pool, Username=user, MessageAction="SUPPRESS",
                              UserAttributes=[{"Name": "email", "Value": user}, {"Name": "email_verified", "Value": "true"}]
                              + [{"Name": k, "Value": v} for k, v in claims.items()])
        created = True
        idp.admin_set_user_password(UserPoolId=pool, Username=user, Password=pw, Permanent=True)
        for g in groups:
            idp.admin_add_user_to_group(UserPoolId=pool, Username=user, GroupName=g)
        token = idp.initiate_auth(ClientId=client_id, AuthFlow="USER_PASSWORD_AUTH",
                                  AuthParameters={"USERNAME": user, "PASSWORD": pw})["AuthenticationResult"]["IdToken"]

        vehicles = boto3.resource("dynamodb", region_name=region).Table(f"cms-{stage}-storage-vehicles")
        tenant = claims.get("custom:tenantId") or "meridian-fleet"
        vin, vid, cls = pick_vin(base, token, vehicles, tenant)
        print(f"vehicle {vid} vin {vin}: classifier {cls}")
        # Bind the test driver to the picked vehicle, as a real driver is bound to
        # theirs, and sign in again so the token carries the claim.
        idp.admin_update_user_attributes(UserPoolId=pool, Username=user, UserAttributes=[
            {"Name": "custom:vehicleId", "Value": vid}])
        token = idp.initiate_auth(ClientId=client_id, AuthFlow="USER_PASSWORD_AUTH",
                                  AuthParameters={"USERNAME": user, "PASSWORD": pw})["AuthenticationResult"]["IdToken"]

        from bedrock_agentcore.runtime import AgentCoreRuntimeClient
        session_id = f"{PREFIX}-{secrets.token_hex(12)}"
        ws_url, headers = AgentCoreRuntimeClient(region=region).generate_ws_connection(runtime_arn=arn, session_id=session_id)
        headers["X-Amzn-Bedrock-AgentCore-Runtime-Custom-User-Token"] = token
        headers["X-Amzn-Bedrock-AgentCore-Runtime-Custom-Tenant-Id"] = tenant
        headers["X-Amzn-Bedrock-AgentCore-Runtime-Custom-Vin"] = vin
        headers["X-Amzn-Bedrock-AgentCore-Runtime-Custom-VehicleId"] = vid
        print(f"session {session_id} started {time.strftime('%H:%M:%SZ', time.gmtime())}")

        fire, yes = speech_pcm(FIRE), speech_pcm("Yes, I confirm, there is fire.")
        events: list[dict] = []
        asked = asyncio.Event()
        done = asyncio.Event()
        async with ws_connect(ws_url, additional_headers=headers, max_size=2**24, open_timeout=15) as ws:
            async def recv():
                while True:
                    try:
                        evt = json.loads(await asyncio.wait_for(ws.recv(), timeout=args.timeout))
                    except (asyncio.TimeoutError, websockets.ConnectionClosed):
                        done.set()
                        return
                    if evt.get("type") == "audio.chunk":
                        continue
                    events.append(evt)
                    t = evt.get("type")
                    if t == "transcript" and evt.get("isFinal"):
                        flag = " [PLATFORM]" if evt.get("_platform_safety_directive") else ""
                        print(f"  transcript {evt.get('role')}{flag}: {evt.get('text', '')[:160]}")
                    elif t in ("tool.call", "classification"):
                        print(f"  {t}: {json.dumps({k: evt.get(k) for k in ('name', 'value', 'source', 'category')})}")
                    if t == "tool.call" and evt.get("name") == "driver_confirmation":
                        asked.set()
            rt = asyncio.create_task(recv())
            await asyncio.sleep(3)  # priming
            await send_pcm(ws, fire)
            try:
                await asyncio.wait_for(asked.wait(), timeout=60)
            except asyncio.TimeoutError:
                print("FAIL: no driver_confirmation step within 60 s")
                ok = False
            if ok:
                # The tool.call fires immediately when the server sets the
                # awaiting state, BEFORE Nova has composed and spoken the
                # question. Wait long enough for Nova to finish its question
                # and for VAD to lock onto the following silence.
                await asyncio.sleep(args.ask_wait)
                await send_pcm(ws, yes)
                await asyncio.sleep(args.after)
            rt.cancel()

        directive_first = cat.RESPONSE_TEMPLATES["exit_vehicle"]["first_sentence"]
        lines = [(i, e) for i, e in enumerate(events) if e.get("type") == "transcript" and e.get("_platform_safety_directive")]
        cls_evts = [(i, e) for i, e in enumerate(events) if e.get("type") == "classification" and e.get("source") == "driver-confirmed"]
        print()
        if ok:
            c1 = len(lines) == 1 and (lines[0][1]["text"].endswith(", " + directive_first)
                                      or lines[0][1]["text"] == directive_first[0].upper() + directive_first[1:])
            print(("PASS" if c1 else "FAIL") + f": one platform line with the exit_vehicle sentence ({[e['text'] for _, e in lines]})")
            c2 = bool(cls_evts) and cls_evts[0][1].get("value") == "P0" and cls_evts[0][1].get("category") == "fire_or_smoke"
            print(("PASS" if c2 else "FAIL") + f": classification P0 driver-confirmed fire_or_smoke ({[e for _, e in cls_evts]})")
            c3 = c1 and c2 and lines[0][0] < cls_evts[0][0]
            print(("PASS" if c3 else "FAIL") + ": the platform line comes before the classification event")
            after = [e.get("text", "") for i, e in enumerate(events)
                     if lines and i > lines[0][0] and e.get("type") == "transcript" and e.get("role") == "assistant"
                     and not e.get("_platform_safety_directive") and e.get("isFinal")]
            print(f"INFO: model assistant lines after the platform line: {after[:4]}")
            spoke = any("get out" in t.lower() and "fifty feet" in t.lower() for t in after)
            print(("INFO" if spoke else "INFO (not seen)") + ": the model repeated the directive")
            # F17.6(c): the booking row for this session is at P0, not the
            # classifier's P2/P3. Poll service-history for up to 30 s so the
            # synchronous put_item in call_book_endpoint has time to land after
            # the WS closes.
            history = boto3.resource("dynamodb", region_name=region).Table(f"cms-{stage}-storage-service-history")
            booking_rows: list[dict] = []
            for _ in range(15):
                try:
                    resp = history.query(
                        KeyConditionExpression="vehicleId = :v",
                        FilterExpression="sessionId = :s",
                        ExpressionAttributeValues={":v": vid, ":s": session_id},
                    )
                except Exception as e:  # noqa: BLE001
                    print(f"FAIL: service-history query errored ({type(e).__name__}: {e})")
                    booking_rows = []
                    break
                booking_rows = resp.get("Items") or []
                if booking_rows:
                    break
                await asyncio.sleep(2)
            if not booking_rows:
                c4 = False
                print(f"FAIL: no cms-{stage}-storage-service-history row found for sessionId={session_id} on {vid} (30s)")
            else:
                priorities = sorted({str(r.get("triagePriority", "")) for r in booking_rows})
                req_nums = sorted({str(r.get("requestNumber", "")) for r in booking_rows})
                svc_dates = sorted({str(r.get("serviceDate", "")) for r in booking_rows})
                # DS-3 assertion: exactly one row AND it is at P0.
                # Before this fix a second row at the classifier's grade
                # (P2/P3) was minted ~0.6 s after the P0.
                c4_exactly_one = len(booking_rows) == 1
                c4_at_p0 = priorities == ["P0"]
                c4 = c4_exactly_one and c4_at_p0
                print(("PASS" if c4_exactly_one else "FAIL")
                      + f": exactly one cms-{stage}-storage-service-history row for this session"
                      + f" (rows={len(booking_rows)}, requestNumber={req_nums})")
                print(("PASS" if c4_at_p0 else "FAIL")
                      + f": the single booking row is at P0, not the classifier's {cls}"
                      + f" (triagePriority={priorities})")
                if not c4:
                    print(f"FAIL detail: svc_dates={svc_dates} vehicleId={vid} sessionId={session_id}")
                print(f"INFO: booking IDs — requestNumber={req_nums} serviceDate={svc_dates} vehicleId={vid} sessionId={session_id}")
            ok = c1 and c2 and c3 and c4
        return 0 if ok else 1
    finally:
        if created:
            idp.admin_delete_user(UserPoolId=pool, Username=user)
            print(f"deleted {user}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", default="staging")
    ap.add_argument("--region", default="us-west-2")
    ap.add_argument("--timeout", type=float, default=45)
    ap.add_argument("--after", type=float, default=25, help="seconds to listen after 'yes'")
    ap.add_argument("--ask-wait", type=float, default=12,
                    help="seconds to wait between the driver_confirmation tool.call and sending 'yes' (lets Nova finish the question)")
    args = ap.parse_args()
    if os.environ.get("LIVE_TESTS") != "1":
        print("skipped: set LIVE_TESTS=1 to run against real AWS")
        return 2
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())
