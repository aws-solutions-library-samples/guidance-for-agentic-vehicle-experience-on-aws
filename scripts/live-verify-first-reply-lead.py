#!/usr/bin/env python3
"""Live check (field video F): on the deployed staging bidi runtime, one typed warning-light
question gets the fixed first-reply lead, and the session's knowledge-base lookup reads the ADP KB.

Issues: issues/2026-10-30-safe-to-drive-narration/ and issues/2026-10-30-bidi-kb-access/.

Interpreter: .venv/bin/python3 (needs websockets, bedrock_agentcore, requests).

1. Creates a throwaway user in the voice-auth pool with meridian.driver's groups and tenant claim
   (not its vehicle or driver claim), and deletes it in cleanup.
2. Picks a vehicle, never the field-video car VEH-MRDN-0015, whose /triage grade is P1-P3 and for
   which first_reply_lead.build_lead(activeDtcs, grade) builds a lead (a "limited" verdict is
   preferred), binds the user to it and signs in again.
3. Opens a bidi session, sends the question as text.input, and waits for find_service_center and
   the assistant's reply. The model may call book unprompted (seen 2026-10-30); any booking this
   session created is deleted in cleanup.
4. Reads the runtime log group over the session window for "first-reply-lead: told the model" and
   for any retrieve: warning (AccessDenied, empty result, KB not configured).
   If escalate_to_human was called, its contact/dispatch ids are printed and the run exits non-zero.

Writes: the throwaway user; the triage decision rows of the vehicles tried (<PREFIX>-pick and the
session id, 30-day TTL); the session's memory events under the deleted user's sub; and, if the
model books, a service-history row (deleted in cleanup). The triage rows and memory events are
left in place for their TTL.

Run only with LIVE_TESTS=1 and the live go:

    LIVE_TESTS=1 FIRST_REPLY_LEAD_LIVE_GO=staging \\
      .venv/bin/python3 scripts/live-verify-first-reply-lead.py --stage staging --allow-writes

Exit 0: PASS. Exit 1: FAIL. Exit 2: inconclusive (retrieve was never called).
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import datetime as _dt
import json
import os
import secrets
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
for _p in (str(REPO), str(HERE / "lib")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import live_write_guard as _lwg  # noqa: E402
from live_check_helpers import (  # noqa: E402
    _delete_session_bookings,
    _query_session_bookings,
    _run_cleanup,
    log_lines,
    pick_vehicle,
)

_LIVE_GO_ENV = "FIRST_REPLY_LEAD_LIVE_GO"
QUESTION = "A warning light just came on. What's wrong, and is it safe to drive?"
FOLLOW_UP = "Where can I get it checked?"
EXCLUDED_VEHICLES = frozenset({"VEH-MRDN-0015", "MRDN0000000000015"})
PREFIX = f"lv-first-reply-lead-{secrets.token_hex(3)}"


async def run(args) -> int:
    import boto3
    from websockets.asyncio.client import connect as ws_connect
    import websockets

    region, stage = args.region, args.stage
    idp = boto3.client("cognito-idp", region_name=region)
    pool = boto3.client("ssm", region_name=region).get_parameter(
        Name=f"/cvx/{stage}/voice-auth-user-pool-id")["Parameter"]["Value"].strip()
    outs = boto3.client("cloudformation", region_name=region).describe_stacks(
        StackName=f"vsa-{stage}-api")["Stacks"][0]["Outputs"]
    base = next(o["OutputValue"].rstrip("/") for o in outs if o["OutputKey"] == "RestApiUrl")

    # Paginate UserPoolClients; raise a named error rather than a bare StopIteration.
    client_id: str | None = None
    pager = idp.get_paginator("list_user_pool_clients")
    for _page in pager.paginate(UserPoolId=pool):
        for c in _page["UserPoolClients"]:
            detail = idp.describe_user_pool_client(
                UserPoolId=pool, ClientId=c["ClientId"])["UserPoolClient"]
            if "ALLOW_USER_PASSWORD_AUTH" in (detail.get("ExplicitAuthFlows") or []):
                client_id = c["ClientId"]
                break
        if client_id:
            break
    if not client_id:
        raise RuntimeError("no UserPoolClient with ALLOW_USER_PASSWORD_AUTH in the voice-auth pool")

    md = idp.admin_get_user(UserPoolId=pool, Username="meridian.driver@example.com")
    claims = {a["Name"]: a["Value"] for a in md["UserAttributes"] if a["Name"].startswith("custom:")}
    claims.pop("custom:vehicleId", None)
    claims.pop("custom:driverId", None)
    groups = [g["GroupName"] for g in idp.admin_list_groups_for_user(
        UserPoolId=pool, Username="meridian.driver@example.com")["Groups"]]

    # Paginate agent runtimes; raise a named error rather than a bare StopIteration.
    name = f"vsa_supervisor_bidi_{stage}"
    ctl = boto3.client("bedrock-agentcore-control", region_name=region)
    runtime = None
    _rt_kw: dict = {}
    while True:
        _rt_page = ctl.list_agent_runtimes(maxResults=50, **_rt_kw)
        for r in _rt_page.get("agentRuntimes", []):
            if r["agentRuntimeName"] == name:
                runtime = r
                break
        if runtime or not _rt_page.get("nextToken"):
            break
        _rt_kw = {"nextToken": _rt_page["nextToken"]}
    if not runtime:
        raise RuntimeError(f"bidi runtime {name!r} not found in {region}")

    arn, runtime_id = runtime["agentRuntimeArn"], runtime["agentRuntimeId"]
    group = f"/aws/bedrock-agentcore/runtimes/{runtime_id}-DEFAULT"
    print(f"runtime {name}: version={runtime.get('agentRuntimeVersion')!r} "
          f"lastUpdatedAt={runtime.get('lastUpdatedAt')!r}")

    user, pw = f"{PREFIX}@example.com", f"Av{secrets.token_urlsafe(18)}!7Qx"
    created, ok = False, True
    vid: str | None = None
    session_id: str | None = None
    start_ms: int | None = None
    history_table = boto3.resource("dynamodb", region_name=region).Table(
        f"cms-{stage}-storage-service-history")
    try:
        idp.admin_create_user(UserPoolId=pool, Username=user, MessageAction="SUPPRESS",
                              UserAttributes=[{"Name": "email", "Value": user},
                                             {"Name": "email_verified", "Value": "true"}]
                              + [{"Name": k, "Value": v} for k, v in claims.items()])
        created = True
        idp.admin_set_user_password(UserPoolId=pool, Username=user, Password=pw, Permanent=True)
        for g in groups:
            idp.admin_add_user_to_group(UserPoolId=pool, Username=user, GroupName=g)

        def sign_in():
            return idp.initiate_auth(ClientId=client_id, AuthFlow="USER_PASSWORD_AUTH",
                                     AuthParameters={"USERNAME": user, "PASSWORD": pw}
                                     )["AuthenticationResult"]["IdToken"]

        token = sign_in()
        vehicles = boto3.resource("dynamodb", region_name=region).Table(f"cms-{stage}-storage-vehicles")
        tenant = claims.get("custom:tenantId") or "meridian-fleet"
        history = boto3.resource("dynamodb", region_name=region).Table(f"cms-{stage}-storage-dtc-history")
        vin, vid, grade, lead = pick_vehicle(
            base, token, vehicles, history, tenant, args.max_tries, EXCLUDED_VEHICLES, PREFIX)
        print(f"vehicle {vid}: {grade}, lead code={lead.code} verdict={lead.verdict}\n  expected: {lead.text}")
        idp.admin_update_user_attributes(UserPoolId=pool, Username=user,
                                         UserAttributes=[{"Name": "custom:vehicleId", "Value": vid}])
        token = sign_in()

        from bedrock_agentcore.runtime import AgentCoreRuntimeClient
        session_id = f"{PREFIX}-{secrets.token_hex(12)}"
        ws_url, headers = AgentCoreRuntimeClient(region=region).generate_ws_connection(
            runtime_arn=arn, session_id=session_id)
        headers.update({
            "X-Amzn-Bedrock-AgentCore-Runtime-Custom-User-Token": token,
            "X-Amzn-Bedrock-AgentCore-Runtime-Custom-Tenant-Id": tenant,
            "X-Amzn-Bedrock-AgentCore-Runtime-Custom-Vin": vin,
            "X-Amzn-Bedrock-AgentCore-Runtime-Custom-VehicleId": vid,
        })
        start_ms = int(time.time() * 1000)
        print(f"session {session_id} started {time.strftime('%H:%M:%SZ', time.gmtime())}")
        events: list[dict] = []
        found = asyncio.Event()
        async with ws_connect(ws_url, additional_headers=headers, max_size=2**24, open_timeout=15) as ws:
            async def recv():
                while True:
                    try:
                        evt = json.loads(await asyncio.wait_for(ws.recv(), timeout=args.timeout))
                    except (asyncio.TimeoutError, websockets.ConnectionClosed):
                        return
                    if evt.get("type") == "audio.chunk":
                        continue
                    events.append(evt)
                    t = evt.get("type")
                    if t == "transcript":
                        mark = "" if evt.get("isFinal") else "~"
                        print(f"  transcript{mark} {evt.get('role')}: {evt.get('text', '')[:200]}")
                    elif t in ("tool.call", "classification"):
                        print(f"  {t}: {evt.get('name') or evt.get('value')}")
                    elif t == "escalation":
                        print(f"  escalation: status={evt.get('status')} contactId={evt.get('contactId')} "
                              f"rsaActionId={evt.get('rsaActionId')}")
                    if t == "tool.call" and evt.get("name") == "find_service_center":
                        found.set()

            async def silence():
                frame = base64.b64encode(b"\x00" * (16000 * 2 * 200 // 1000)).decode()
                while True:
                    await ws.send(json.dumps({"type": "audio.chunk", "sampleRate": 16000, "data": frame}))
                    await asyncio.sleep(0.2)

            rt = asyncio.create_task(recv())
            st = asyncio.create_task(silence())
            await asyncio.sleep(3)
            await ws.send(json.dumps({"type": "text.input", "text": QUESTION}))
            first_answer = True
            try:
                await asyncio.wait_for(found.wait(), timeout=60)
            except asyncio.TimeoutError:
                first_answer = False
                print(f"INFO: no find_service_center in the first answer; asking: {FOLLOW_UP!r}")
                await ws.send(json.dumps({"type": "text.input", "text": FOLLOW_UP}))
                try:
                    await asyncio.wait_for(found.wait(), timeout=60)
                except asyncio.TimeoutError:
                    pass
            await asyncio.sleep(args.after)
            st.cancel()
            rt.cancel()
        end_ms = int(time.time() * 1000) + 30_000
        await asyncio.sleep(20)  # log delivery

        logs = boto3.client("logs", region_name=region)
        told = log_lines(logs, group, start_ms, end_ms, '"first-reply-lead: told the model"')
        mine = [m for m in told if f"code={lead.code}" in m and f"session={session_id}" in m]
        kb_warn = log_lines(logs, group, start_ms, end_ms, '"retrieve:"')
        tools = [e.get("name") for e in events if e.get("type") == "tool.call"]
        escalation_evts = [e for e in events if e.get("type") == "escalation"]
        # W3(a) fix: the tool is escalate_to_human, not escalate.
        escalate_calls = [t for t in tools if t == "escalate_to_human"]
        said = " ".join(e.get("text", "") for e in events
                        if e.get("type") == "transcript" and e.get("role") == "assistant")
        print()
        c1 = bool(mine)
        print(("PASS" if c1 else "FAIL") + f": runtime told the model the lead (code={lead.code}): "
              f"{mine[:2] or told[:2]}")
        print("INFO: the lead came with the "
              + ("first answer" if first_answer else "service-center answer to the follow-up, not the first answer"))
        verbatim = lead.text in said
        print(("PASS" if verbatim else "INFO") + ": the assistant said the lead word for word"
              + ("" if verbatim else f" (not exact; said: {said[:300]!r})"))

        # W3(b) fix: check escalation BEFORE the inconclusive return so a session that escalated
        # but never called retrieve exits 1 (FAIL) rather than 2 (inconclusive).
        if escalate_calls or escalation_evts:
            print(f"FAIL: escalate_to_human was called or fired ({len(escalate_calls)} tool.call(s), "
                  f"{len(escalation_evts)} escalation event(s)). "
                  f"This run touched a real queue; clean up manually.", file=sys.stderr)
            for ev in escalation_evts:
                print(f"  contactId={ev.get('contactId')} rsaActionId={ev.get('rsaActionId')} "
                      f"status={ev.get('status')}", file=sys.stderr)
            ok = False

        if "retrieve" not in tools:
            if ok:
                print("INCONCLUSIVE: retrieve was not called; cannot confirm KB access (exit 2)")
                return 2
            # else: escalation already set ok=False; fall through to exit 1
            c2 = True  # vacuous; ok stays False
        else:
            c2 = not kb_warn
            # W2 note: c2 is window-scoped; retrieve: warnings from other concurrent sessions on
            # this shared runtime may appear (tools/retrieve.py carries no session id in its lines).
            print("INFO: c2 is window-scoped — retrieve: warnings may include other concurrent "
                  "sessions on this shared runtime")
            print(("PASS" if c2 else "FAIL") + f": retrieve ran {tools.count('retrieve')}x with no retrieve warning "
                  f"in the window ({kb_warn[:2]})")

        print(f"INFO: tools in order: {tools}")
        ok = ok and c1 and c2
        return 0 if ok else 1
    finally:
        _run_cleanup(
            idp=idp,
            pool=pool,
            user=user,
            created=created,
            history_table=history_table,
            vid=vid,
            session_id=session_id,
            start_ms=start_ms,
            stage=stage,
        )


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--stage", default="staging", choices=["staging"])
    ap.add_argument("--region", default="us-west-2", choices=["us-west-2"])
    ap.add_argument("--allow-writes", action="store_true",
                    help="required: the run creates a throwaway user")
    ap.add_argument("--max-tries", type=int, default=25)
    ap.add_argument("--timeout", type=float, default=45.0)
    ap.add_argument("--after", type=float, default=25.0,
                    help="seconds to wait after find_service_center")
    args = ap.parse_args(argv)
    if os.environ.get("LIVE_TESTS") != "1":
        print("refused: set LIVE_TESTS=1", file=sys.stderr)
        return 1
    if not args.allow_writes:
        print("refused: this check writes (a throwaway user, triage rows); pass --allow-writes",
              file=sys.stderr)
        return 1
    msg = _lwg.check_write_flags(stage=args.stage, go_env=_LIVE_GO_ENV,
                                  flags={"--allow-writes": args.allow_writes})
    if msg:
        print(msg, file=sys.stderr)
        return 1
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())
