#!/usr/bin/env python3
"""Live check (DS-4): a verified P0 escalation places the driver callback BEFORE the chat,
and the chat goes to the delayed P0 chat queue through the after-callback flow.

Issue `issues/2026-10-05-p0-callback-waits-behind-chat/`. Direct invoke of the deployed
`vsa-{stage}-api-escalate` Lambda, the same way `live-verify-escalate-vehicle-mismatch.py`
does, but with a MATCHING (vehicleId, vin) so every P0 gate passes.

Checks
  C1 /escalate returns 200 with a chat contactId and no RSA dispatch, and logs
     callback_initiated=True on its chat line (the 200 body carries no callback flag)
  C2 the Lambda log has "callback: initiated" before the "escalate: contact=" line
  C3 the chat's attributes carry p0CallbackPlaced=true and a p0CallbackEscalationId
  C4 an outbound VOICE contact carries that escalationId, and was initiated before the chat
  C5 the chat lands in the tenant's after-callback chat queue (from the flow, read back)

Safety
  - Refuses without LIVE_TESTS=1 (exit 2).
  - Preflight refuses unless the caller's assigned driver phone is a fictional 555-01xx
    number (the call cannot reach a person), and unless the tenant config names a callback
    flow, a caller-id queue and an after-callback chat flow.
  - dispatchRoadside is sent as false, so no RSA dispatch row is written.
  - Side effects: one triage-fixture row (deleted), one outbound call to the fictional
    number and one chat contact (both stopped at the end).
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone

TEST_SOURCE = "live-verify-p0-call-first"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--stage", default="staging")
    ap.add_argument("--region", default="us-west-2")
    ap.add_argument("--tenant", default="meridian")
    ap.add_argument("--driver-id", default="DRV-MRDN-0015")
    ap.add_argument("--vehicle-id", default="MRDN0000000000015")
    args = ap.parse_args()
    if os.environ.get("LIVE_TESTS") != "1":
        print("SKIP: set LIVE_TESTS=1. This places one call to a fictional number and opens one chat.")
        return 2

    import boto3

    s, r = args.stage, args.region
    ddb = boto3.resource("dynamodb", region_name=r)
    lam = boto3.client("lambda", region_name=r)
    fn = f"vsa-{s}-api-escalate"
    results: list[tuple[str, bool, str]] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        results.append((name, ok, detail))
        print(f"{'PASS' if ok else 'FAIL'}: {name}" + (f" — {detail}" if detail else ""))

    # ── Preflight ──────────────────────────────────────────────────────────
    tc = ddb.Table(f"vsa-{s}-tenant-config")
    ver = tc.get_item(Key={"tenantId": args.tenant, "version": "current"})["Item"]["activeVersion"]
    cfg = tc.get_item(Key={"tenantId": args.tenant, "version": ver})["Item"]
    conn = (cfg.get("channels") or {}).get("connect") or {}
    cb = (cfg.get("escalation") or {}).get("callback") or {}
    instance_arn = conn.get("instanceArn") or ""
    instance_id, connect_region = instance_arn.split("/")[-1], instance_arn.split(":")[3]
    if not (cb.get("contactFlowId") and cb.get("queueArn") and cb.get("chatContactFlowId")):
        print(f"PREFLIGHT FAILED: tenant {args.tenant} {ver} lacks callback flow / queue / chat flow")
        return 1
    veh = ddb.Table(f"cms-{s}-storage-vehicles").get_item(Key={"vehicleId": args.vehicle_id})["Item"]
    vin = str(veh["vin"])
    drv = ddb.Table(f"cms-{s}-storage-drivers").get_item(Key={"driverId": args.driver_id})["Item"]
    digits = "".join(c for c in str(drv.get("phone") or "") if c.isdigit())
    if drv.get("assignedVehicleId") != args.vehicle_id or not (
            len(digits) == 11 and digits[0] == "1" and digits[4:7] == "555" and digits[7:9] == "01"):
        print(f"PREFLIGHT FAILED: driver {args.driver_id} not assigned to {args.vehicle_id}, "
              "or phone is not a fictional 555-01xx number")
        return 1
    connect = boto3.client("connect", region_name=connect_region)
    flow = connect.describe_contact_flow(InstanceId=instance_id, ContactFlowId=cb["chatContactFlowId"])
    content = json.loads(flow["ContactFlow"]["Content"])
    flow_queue_arns = [a.get("Parameters", {}).get("QueueId") for a in content.get("Actions", [])
                       if a.get("Type") == "UpdateContactTargetQueue"]
    p0_chat_queue_id = (flow_queue_arns[0] or "").split("/")[-1] if flow_queue_arns else None
    print(f"tenant {args.tenant} {ver}; after-callback flow {flow['ContactFlow']['Name']} -> queue "
          f"{p0_chat_queue_id}; driver phone fictional ({digits[:4]}****)")

    fixture = f"{TEST_SOURCE}-{uuid.uuid4()}"
    ddb.Table(f"vsa-{s}-triage-decisions").put_item(Item={
        "sessionId": fixture, "tenantId": args.tenant, "vin": vin, "classification": "P0",
        "decidedAt": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "source": TEST_SOURCE, "ttl": int(time.time()) + 3600,
    })
    chat_id = None
    ws = None
    voice_ids: list[str] = []
    try:
        t0 = datetime.now(timezone.utc)
        payload = {
            "httpMethod": "POST",
            "requestContext": {"authorizer": {"claims": {
                "custom:tenantId": args.tenant, "custom:driverId": args.driver_id,
                "custom:vehicleId": args.vehicle_id}}},
            "body": json.dumps({
                "severity": "P0", "escalationType": "safety", "vin": vin, "vehicleId": args.vehicle_id,
                "driverId": args.driver_id, "dispatchRoadside": False, "triageSessionId": fixture,
                "reason": "LIVE-VERIFY DS-4 call-first; not a real incident",
            }),
        }
        resp = lam.invoke(FunctionName=fn, InvocationType="RequestResponse", LogType="Tail",
                          Payload=json.dumps(payload).encode())
        env = json.loads(resp["Payload"].read())
        logs = base64.b64decode(resp.get("LogResult", "")).decode("utf-8", "replace")
        body = json.loads(env.get("body") or "{}")
        chat_id = body.get("contactId")
        lines = logs.splitlines()
        # The 200 body carries no callback flag; the handler logs it on the chat line.
        chat_line = next((ln for ln in lines if "escalate: contact=" in ln), "")
        check("C1 200 with a chat contactId, no RSA dispatch, callback_initiated=True logged",
              env.get("statusCode") == 200 and bool(chat_id) and body.get("rsaDispatched") is False
              and "callback_initiated=True" in chat_line,
              f"status={env.get('statusCode')} contactId={'present' if chat_id else 'missing'} "
              f"rsaDispatched={body.get('rsaDispatched')!r} error={body.get('error')!r}")
        i_call = next((i for i, ln in enumerate(lines) if "callback: initiated" in ln), None)
        i_chat = next((i for i, ln in enumerate(lines) if "escalate: contact=" in ln), None)
        check("C2 callback log line precedes the chat log line",
              i_call is not None and i_chat is not None and i_call < i_chat,
              f"callback line {i_call}, chat line {i_chat}")
        if not chat_id:
            return 1
        attrs = connect.get_contact_attributes(InstanceId=instance_id, InitialContactId=chat_id)["Attributes"]
        esc_id = attrs.get("p0CallbackEscalationId")
        check("C3 chat attributes p0CallbackPlaced=true + escalation id",
              attrs.get("p0CallbackPlaced") == "true" and bool(esc_id),
              f"p0CallbackPlaced={attrs.get('p0CallbackPlaced')!r} escalationId={'present' if esc_id else 'missing'}")

        # The chat flow only runs once the customer participant connects (iOS does this
        # with the participant token, the same two connection types, then subscribes on
        # the websocket); without it the chat never reaches a queue.
        pc = boto3.client("connectparticipant", region_name=connect_region).create_participant_connection(
            Type=["WEBSOCKET", "CONNECTION_CREDENTIALS"], ParticipantToken=body["participantToken"])
        from websockets.sync.client import connect as ws_connect
        ws = ws_connect(pc["Websocket"]["Url"])
        ws.send(json.dumps({"topic": "aws/subscribe", "content": {"topics": ["aws/chat"]}}))
        qid = None
        for _ in range(15):
            qid = (connect.describe_contact(InstanceId=instance_id, ContactId=chat_id)["Contact"]
                   .get("QueueInfo") or {}).get("Id")
            if qid:
                break
            time.sleep(2)
        check("C5 chat routed to the after-callback chat queue", bool(qid) and qid == p0_chat_queue_id,
              f"queue={qid} expected={p0_chat_queue_id}")
        chat = connect.describe_contact(InstanceId=instance_id, ContactId=chat_id)["Contact"]
        voice = None
        for _ in range(60):  # contact search indexing lagged > 90 s on 2026-10-05; poll up to ~5 min
            found = connect.search_contacts(
                InstanceId=instance_id,
                TimeRange={"Type": "INITIATION_TIMESTAMP", "StartTime": t0 - timedelta(seconds=5),
                           "EndTime": datetime.now(timezone.utc) + timedelta(seconds=5)},
                SearchCriteria={"Channels": ["VOICE"]}).get("Contacts", [])
            for c in found:
                a = connect.get_contact_attributes(InstanceId=instance_id, InitialContactId=c["Id"])["Attributes"]
                if esc_id and a.get("escalationId") == esc_id:
                    voice = connect.describe_contact(InstanceId=instance_id, ContactId=c["Id"])["Contact"]
            if voice:
                break
            time.sleep(5)
        if voice:
            voice_ids.append(voice["Id"])
        vt, ct = (voice or {}).get("InitiationTimestamp"), chat.get("InitiationTimestamp")
        check("C4 outbound voice contact with the same escalationId, initiated before the chat",
              bool(voice) and vt is not None and ct is not None and vt < ct,
              f"voice={'found' if voice else 'not found'} method={(voice or {}).get('InitiationMethod')} "
              f"voice_at={vt} chat_at={ct} disconnect={(voice or {}).get('DisconnectReason')}")

    finally:
        if ws is not None:
            ws.close()
        for cid in ([chat_id] if chat_id else []) + voice_ids:
            try:
                connect.stop_contact(ContactId=cid, InstanceId=instance_id)
                print(f"stopped contact {cid}")
            except Exception as e:  # noqa: BLE001 - already ended is fine
                print(f"stop_contact {cid}: {type(e).__name__} (already ended is expected for the call)")
        ddb.Table(f"vsa-{s}-triage-decisions").delete_item(Key={"sessionId": fixture})
        print(f"deleted triage fixture {fixture}")
    failed = [n for n, ok, _ in results if not ok]
    print(f"{len(results) - len(failed)}/{len(results)} checks passed")
    return 1 if failed or not results else 0


if __name__ == "__main__":
    sys.exit(main())
