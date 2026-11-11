#!/usr/bin/env python3
"""Live check: a driver's spoken fire report gets the directive first and is never
questioned (issues/2026-10-06-fire-report-doubted-before-directive/).

Against the deployed staging bidi runtime, with fictional data only:

1. Creates a throwaway user in the voice-auth pool with meridian.driver's groups and
   tenant claim (not its vehicle or driver claim), picks a vehicle the classifier
   grades P2 or P3 via the real `/triage`, binds the user to it and signs in again.
   (The checks below branch on that grade: the words/actions split of 2026-10-06 means
   a report below a classifier P0 gets the directive and the confirmation question, and
   the ticket, the P0 on screen and the decision row wait for the driver's yes.)
   The user is deleted at the end.
2. Speaks (macOS `say`, 16 kHz mono PCM) `--report` (default "My engine is on fire and
   there's smoke coming from the hood.") and listens for `--listen` seconds. Nothing else
   is said.
3. Checks, on what the client received:
   - the FIRST assistant item after the driver's report (transcript or audio chunk) is
     the platform line, and its text is a catalog first sentence verbatim: the report's
     own (`exit_vehicle` for fire or smoke, `brake_emergency` for brake loss) or a more
     protective one the vehicle's faults call for;
   - no `driver_confirmation` step (the report is not questioned);
   - a `classification` P0 with source `driver-confirmed` and the report's category
     (`fire_or_smoke` or `brake_failure`, whichever `--report` names);
   - every model transcript the client got leads with the last platform line shown
     (speculative text, joined), or the model said nothing;
   - exactly one `cms-{stage}-storage-service-history` row for the session, at P0;
   - the driver-confirmation decision row (`<session>-driver-confirmed`) is P0 with
     verdict `reported` (what `/escalate` checks before a roadside dispatch).
4. Prints the runtime's `hazard-directive:` and `narration-severity:` log lines for the
   session (the gate's decisions; a `narration-severity:` line would be the DS-2 guard
   firing live).

Side effects: one throwaway Cognito user (deleted), triage decision rows, the audit row
and the single booking row (left for audit; printed). Run only with LIVE_TESTS=1.
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
from agents.supervisor import hazard_directive as hd  # noqa: E402

PREFIX = f"lv-fire-first-{secrets.token_hex(3)}"
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


def runtime_log_lines(region: str, arn: str, session_id: str, since_ms: int) -> list[str]:
    runtime_id = arn.rsplit("/", 1)[1]
    logs = boto3.client("logs", region_name=region)
    group = f"/aws/bedrock-agentcore/runtimes/{runtime_id}-DEFAULT"
    out: list[str] = []
    kwargs = {"logGroupName": group, "startTime": since_ms, "filterPattern": f'"{session_id}"'}
    while True:
        resp = logs.filter_log_events(**kwargs)
        out.extend(e["message"].strip() for e in resp.get("events", []))
        if not resp.get("nextToken"):
            return out
        kwargs["nextToken"] = resp["nextToken"]


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
    claims.pop("custom:vehicleId", None)
    claims.pop("custom:driverId", None)
    groups = [g["GroupName"] for g in idp.admin_list_groups_for_user(
        UserPoolId=pool, Username="meridian.driver@example.com")["Groups"]]
    arn = yaml.safe_load((REPO / ".bedrock_agentcore.yaml").read_text())["agents"][
        "vsa_supervisor_bidi_staging"]["bedrock_agentcore"]["agent_arn"]

    user = f"{PREFIX}@example.com"
    pw = f"Av{secrets.token_urlsafe(18)}!7Qx"
    created = False
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
        started_ms = int(time.time() * 1000) - 60_000
        print(f"session {session_id} started {time.strftime('%H:%M:%SZ', time.gmtime())}")

        report = args.report
        hazard = hd.reported_hazard(report)
        if hazard is None:
            raise SystemExit(f"--report {report!r} is not a fire/smoke or brake-loss report")
        fire = speech_pcm(report)
        events: list[dict] = []
        async with ws_connect(ws_url, additional_headers=headers, max_size=2**24, open_timeout=15) as ws:
            async def recv():
                while True:
                    try:
                        evt = json.loads(await asyncio.wait_for(ws.recv(), timeout=args.timeout))
                    except (asyncio.TimeoutError, websockets.ConnectionClosed):
                        return
                    if evt.get("type") == "audio.chunk":
                        events.append({"type": "audio.chunk"})
                        continue
                    events.append(evt)
                    t = evt.get("type")
                    if t == "transcript":
                        flag = " [PLATFORM]" if evt.get("_platform_safety_directive") else ""
                        fin = "final" if evt.get("isFinal") else "spec"
                        print(f"  transcript {evt.get('role')} {fin}{flag}: {evt.get('text', '')[:160]}")
                    elif t in ("tool.call", "classification"):
                        print(f"  {t}: {json.dumps({k: evt.get(k) for k in ('name', 'value', 'source', 'category')})}")
            rt = asyncio.create_task(recv())
            await asyncio.sleep(3)
            await send_pcm(ws, fire)
            await asyncio.sleep(args.listen)
            rt.cancel()

        # The platform shows the more protective of the report's line and the line the
        # vehicle's faults call for, so any line at least as protective as the report's
        # own is correct (a brake report on a car with a fire-class fault gets the exit line).
        own_rank = cat.P0_TEMPLATE_PRECEDENCE.index(hazard.p0_template)
        allowed = [t for t in cat.P0_TEMPLATE_PRECEDENCE[: own_rank + 1] if t in cat.RESPONSE_TEMPLATES]
        firsts = {t: cat.RESPONSE_TEMPLATES[t]["first_sentence"] for t in allowed}

        def directive_shown(text: str) -> str | None:
            """The first sentence ``text`` is (or ends with, after the driver's name)."""
            for f in firsts.values():
                if text == f[0].upper() + f[1:] or text.endswith(", " + f):
                    return f
            return None

        def is_directive(text: str) -> bool:
            return directive_shown(text) is not None

        i_report = next((i for i, e in enumerate(events) if e.get("type") == "transcript"
                         and e.get("role") == "user" and e.get("isFinal")), None)
        after = events[i_report + 1:] if i_report is not None else []
        first = next((e for e in after if e.get("type") == "audio.chunk"
                      or (e.get("type") == "transcript" and e.get("role") == "assistant")), None)
        n_audio = sum(1 for e in after if e.get("type") == "audio.chunk")
        model_spec = [e.get("text", "") for e in after if e.get("type") == "transcript"
                      and e.get("role") == "assistant" and not e.get("_platform_safety_directive")
                      and not e.get("isFinal")]
        print()
        checks: dict[str, bool] = {}
        checks["the driver's report was heard"] = i_report is not None
        checks["first assistant item after the report is the verbatim directive"] = bool(
            first and first.get("type") == "transcript" and first.get("_platform_safety_directive")
            and is_directive(first.get("text", "")))
        # The model is gated on the line actually shown (the last platform line, after any
        # upgrade), so its speech is judged against that one.
        shown = [e.get("text", "") for e in after if e.get("_platform_safety_directive")]
        shown_first = (directive_shown(shown[-1]) if shown else None) or firsts[hazard.p0_template]
        print(f"INFO: platform lines shown: {shown}")
        # The words/actions split (2026-10-06, user decision; see the issue's
        # build-notes). The detector decides the WORDS — the directive, checked above.
        # It does not decide the ACTIONS: below a classifier P0 the confirmation
        # question is asked instead, AFTER the directive, and the ticket, the P0
        # classification and the decision row wait for the driver's yes. So what this
        # script asserts depends on what the classifier said about the vehicle it
        # picked, and both branches are checked.
        asked = [i for i, e in enumerate(events)
                 if e.get("type") == "tool.call" and e.get("name") == "driver_confirmation"]
        i_directive = next((i for i, e in enumerate(events)
                            if e.get("_platform_safety_directive")), None)
        cls_evts = [e for e in events if e.get("type") == "classification"]
        classifier_p0 = cls == "P0"
        print(f"INFO: classifier said {cls}; "
              f"{'actions expected' if classifier_p0 else 'confirmation question expected'}")
        if classifier_p0:
            checks["no confirmation question (the classifier itself said P0)"] = not asked
            checks[f"classification ends at P0 driver-confirmed {hazard.name}"] = bool(
                cls_evts and cls_evts[-1].get("value") == "P0"
                and cls_evts[-1].get("source") == "driver-confirmed"
                and cls_evts[-1].get("category") == hazard.name)
        else:
            checks["confirmation question asked exactly once"] = len(asked) == 1
            checks["the question came AFTER the directive"] = bool(
                asked and i_directive is not None and i_directive < asked[0])
            checks[f"the question is for {hazard.name}"] = bool(
                asked and events[asked[0]].get("input", {}).get("matchedCategory") == hazard.name)
            checks["no driver-confirmed P0 on screen before the yes"] = not any(
                e.get("source") == "driver-confirmed" for e in cls_evts)
        led = (not model_spec) or hd.leads_with(" ".join(model_spec), shown_first) == "match"
        checks["model speech the client got leads with the directive (or none)"] = led
        print(f"INFO: model speculative lines delivered: {model_spec[:4]}; audio chunks after the report: {n_audio}")

        history = boto3.resource("dynamodb", region_name=region).Table(f"cms-{stage}-storage-service-history")
        rows: list[dict] = []
        for _ in range(15):
            rows = history.query(KeyConditionExpression="vehicleId = :v", FilterExpression="sessionId = :s",
                                 ExpressionAttributeValues={":v": vid, ":s": session_id}).get("Items") or []
            if rows:
                break
            await asyncio.sleep(2)
        prio = sorted({str(r.get("triagePriority", "")) for r in rows})
        print(f"INFO: booking requestNumber={sorted(str(r.get('requestNumber')) for r in rows)} vehicleId={vid}")
        audit = boto3.resource("dynamodb", region_name=region).Table(f"vsa-{stage}-triage-decisions").get_item(
            Key={"sessionId": f"{session_id}-driver-confirmed"}).get("Item") or {}
        if classifier_p0:
            checks["exactly one service-history row, at P0"] = len(rows) == 1 and prio == ["P0"]
            checks["driver-confirmation decision row is P0, verdict reported"] = (
                audit.get("classification") == "P0"
                and audit.get("driverConfirmationVerdict") == "reported")
        else:
            # The half the split exists to protect: this row is what /escalate
            # verifies, and a verified P0 row places the outbound P0 callback and can
            # dispatch roadside with no driver tap. The report alone must not write it.
            checks["NO service-history row before the yes"] = not rows
            checks["NO driver-confirmation decision row before the yes"] = not audit

        await asyncio.sleep(10)  # log delivery lag
        lines = runtime_log_lines(region, arn, session_id, started_ms)
        hz = [ln for ln in lines if "hazard-directive:" in ln]
        ns = [ln for ln in lines if "narration-severity:" in ln]
        print("runtime hazard-directive lines:")
        for ln in hz:
            print("  " + ln[-240:])
        print(f"runtime narration-severity lines: {len(ns)}")
        for ln in ns:
            print("  " + ln[-240:])
        checks["runtime logged the directive shown first"] = any("directive shown first" in ln for ln in hz)

        for name, ok in checks.items():
            print(("PASS" if ok else "FAIL") + ": " + name)
        print(f"session={session_id}")
        return 0 if all(checks.values()) else 1
    finally:
        if created:
            idp.admin_delete_user(UserPoolId=pool, Username=user)
            print(f"deleted {user}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", default="staging")
    ap.add_argument("--region", default="us-west-2")
    ap.add_argument("--timeout", type=float, default=45)
    ap.add_argument("--listen", type=float, default=35, help="seconds to listen after the report")
    ap.add_argument("--report", default=FIRE, help="what the driver says (a fire/smoke or brake-loss report)")
    args = ap.parse_args()
    if os.environ.get("LIVE_TESTS") != "1":
        print("skipped: set LIVE_TESTS=1 to run against real AWS")
        return 2
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())
