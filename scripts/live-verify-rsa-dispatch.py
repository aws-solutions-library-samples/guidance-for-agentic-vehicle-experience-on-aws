#!/usr/bin/env python3
"""Live-verify the RSA stub dispatch path against a deployed stage (spec Task 3.3).

Spec: .kiro/specs/2026-09-23-cvx-rsa-stub-dispatch/ Task 3.3, including F8.5.

Every test in Groups 1-3 stubs DynamoDB, Lambda, Connect or the provider, and a
stub cannot fail the way a service fails. This script drives the deployed
resources and asserts the properties that only a real service can show:

  Part A  lifecycle (3.3 Accept 1)
          A REQUESTED row written directly to the dispatch table is picked up by
          the stream consumer (DISPATCHED, SIM- providerRef, ETA, simulated=true),
          read back through the deployed GET handler (tenant-scoped: the owning
          tenant gets 200, another tenant gets the same 404 as a missing id), then
          advanced by the scheduled advancer to EN_ROUTE and ARRIVED, and never
          advanced out of ARRIVED.
  Part B  /escalate (F8.5)
          The deployed escalate Lambda, on a platform-recorded P0 for a vehicle
          with no fresh Redis position, really invokes api-vehicle-live-state,
          gets a real locationSource back, refuses the position, and the row it
          writes reaches FAILED with the location reason instead of dispatching.
          The live-state response is also checked directly for locationSource
          and vin, which F6.1/F7.2 depend on.
  Part C  reconciliation (3.2 Accept 5)
          A REQUESTED row the consumer never saw, older than the stale threshold,
          is reconciled to FAILED by the advancer, and the reconciliation alarm
          (real metric filter on real logs) goes to ALARM and executes its action.
  Part D  forced alarm (3.3, review Cycle 3 S3)
          The advancer errors alarm is forced to ALARM and its newest Action
          history item must be "Succeeded". With CVX_RSA_ALARM_EMAIL=none the
          topic has no subscriber: this proves CloudWatch can publish to the
          topic, not that a person is paged. The script reports the count.

Safety
------
- Refuses to run without LIVE_TESTS=1 (exit 2).
- /escalate side effects that reach the physical world are ruled out twice:
  the script refuses a tenant whose config has a callback flow, and it refuses a
  refusal vehicle whose assigned driver has a phone number that is not a
  fictional 555-01xx number. Since escalation security W1 the P0 gate requires
  the caller to BE the vehicle's assigned driver, so Part B presents that
  driver's `custom:driverId` claim; without it the gate refuses before any
  dispatch row is written and B4 cannot pass. It does open one Amazon Connect
  chat contact, as every /escalate does, and stops it afterwards.
- The P0 gate needs a platform-recorded decision; the script writes one
  clearly-labelled fixture row to the triage-decisions table (ttl 1 h) and
  deletes it afterwards.
- Never prints a VIN, coordinates or a phone number.
- Deletes every row it created unless --keep is given.

Anti-vacuity: if the table, stream, functions, wiring, alarms, tenant or vehicle
cannot be resolved, it exits 1 with the reason. It never passes without checking.

Usage
-----
    LIVE_TESTS=1 .venv/bin/python3 scripts/live-verify-rsa-dispatch.py
    LIVE_TESTS=1 .venv/bin/python3 scripts/live-verify-rsa-dispatch.py --stage staging --region us-west-2

Exit codes: 0 all checks passed · 1 a check failed or preflight failed · 2 skipped
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
from decimal import Decimal
from typing import Any, Callable

TEST_SOURCE = "live-verify-rsa-dispatch"
TENANT_A = "live-verify-rsa-a"
TENANT_B = "live-verify-rsa-b"
# Synthetic, 17 chars, never a real VIN.
SYNTH_VIN = "LVRSA000000000001"
# A plausible roadside point (Seattle). Only ever sent to the fake provider.
SYNTH_LAT, SYNTH_LON = "47.6062", "-122.3321"
NEVER_RETURNED = {
    "vin", "lat", "lon", "callbackContact", "driverId", "driverName", "vehicleId",
    "connectContactId", "summary", "reason", "triageSessionId", "voiceSessionId",
    "tenantId", "failureReason",
}


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class Run:
    def __init__(self) -> None:
        self.results: list[tuple[str, bool, str]] = []

    def check(self, name: str, ok: bool, detail: str = "") -> bool:
        self.results.append((name, ok, detail))
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""), flush=True)
        return ok

    @property
    def failed(self) -> list[tuple[str, bool, str]]:
        return [r for r in self.results if not r[1]]


class Preflight(Exception):
    pass


def wait_for(fn: Callable[[], Any], pred: Callable[[Any], bool], timeout: float, every: float = 10.0) -> Any:
    deadline = time.time() + timeout
    last = None
    while True:
        last = fn()
        if pred(last):
            return last
        if time.time() >= deadline:
            return last
        time.sleep(every)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--stage", default="staging")
    ap.add_argument("--region", default="us-west-2")
    ap.add_argument("--tenant", default="meridian-fleet",
                    help="tenant for the /escalate part; must have Connect configured and NO callback flow")
    ap.add_argument("--refusal-vehicle", default="VEH-MICH-001",
                    help="vehicleId with no fresh Redis position and exactly one assigned driver "
                         "whose phone is absent or a fictional 555-01xx number")
    ap.add_argument("--timeout-seconds", type=int, default=900)
    ap.add_argument("--keep", action="store_true", help="do not delete the rows this run created")
    args = ap.parse_args()

    if os.environ.get("LIVE_TESTS") != "1":
        print("SKIP: set LIVE_TESTS=1. This run writes rows, invokes deployed Lambdas, opens one "
              "Connect chat and forces a CloudWatch alarm.")
        return 2

    import boto3

    s, r = args.stage, args.region
    ddb = boto3.resource("dynamodb", region_name=r)
    ddbc = boto3.client("dynamodb", region_name=r)
    lam = boto3.client("lambda", region_name=r)
    cw = boto3.client("cloudwatch", region_name=r)
    sns = boto3.client("sns", region_name=r)
    sts = boto3.client("sts", region_name=r)

    names = {
        "table": f"vsa-{s}-rsa-dispatch",
        "consumer": f"vsa-{s}-rsa-dispatch-consumer",
        "advancer": f"vsa-{s}-rsa-lifecycle-advancer",
        "reader": f"vsa-{s}-api-rsa-dispatch",
        "escalate": f"vsa-{s}-api-escalate",
        "live_state": f"vsa-{s}-api-vehicle-live-state",
        "tenant_config": f"vsa-{s}-tenant-config",
        "triage": f"vsa-{s}-triage-decisions",
        "vehicles": f"cms-{s}-storage-vehicles",
        "drivers": f"cms-{s}-storage-drivers",
        "errors_alarm": f"vsa-{s}-rsa-lifecycle-advancer-errors",
        "reconciliation_alarm": f"vsa-{s}-rsa-reconciliation-detected",
        "topic": f"vsa-{s}-rsa-operator-alerts",
    }
    run = Run()
    created_rows: list[str] = []
    triage_fixture: str | None = None
    chat: dict[str, str] | None = None
    forced_alarm_reset = False

    # ─── Preflight: resolve everything or refuse ─────────────────────────────
    print(f"Preflight ({s}, {r})")
    try:
        account = sts.get_caller_identity()["Account"]
        t = ddbc.describe_table(TableName=names["table"])["Table"]
        if t["TableStatus"] != "ACTIVE" or not t.get("StreamSpecification", {}).get("StreamEnabled"):
            raise Preflight(f"{names['table']} not ACTIVE with a stream")
        if not any(g["IndexName"] == "status-createdAt-index" for g in t.get("GlobalSecondaryIndexes", [])):
            raise Preflight("status-createdAt-index missing")
        fns = {}
        for k in ("consumer", "advancer", "reader", "escalate", "live_state"):
            c = lam.get_function_configuration(FunctionName=names[k])
            if c["State"] != "Active":
                raise Preflight(f"{names[k]} is {c['State']}")
            fns[k] = c
        esc_env = fns["escalate"].get("Environment", {}).get("Variables", {})
        if esc_env.get("RSA_DISPATCH_TABLE") != names["table"]:
            raise Preflight("escalate RSA_DISPATCH_TABLE does not name the dispatch table")
        if esc_env.get("LIVE_STATE_FN_NAME") != names["live_state"]:
            raise Preflight("escalate LIVE_STATE_FN_NAME does not name the live-state Lambda")
        if esc_env.get("TRIAGE_DECISIONS_TABLE") != names["triage"]:
            raise Preflight("escalate TRIAGE_DECISIONS_TABLE is not the triage table")
        maps = lam.list_event_source_mappings(FunctionName=names["consumer"])["EventSourceMappings"]
        if not any(m["EventSourceArn"] == t["LatestStreamArn"] and m["State"] == "Enabled" for m in maps):
            raise Preflight("consumer has no Enabled mapping on the table's latest stream")
        alarms = cw.describe_alarms(AlarmNamePrefix=f"vsa-{s}-rsa-")["MetricAlarms"]
        by_name = {a["AlarmName"]: a for a in alarms}
        for k in ("errors_alarm", "reconciliation_alarm"):
            a = by_name.get(names[k])
            if not a:
                raise Preflight(f"alarm {names[k]} not found")
            if not a.get("ActionsEnabled") or not a.get("AlarmActions"):
                raise Preflight(f"alarm {names[k]} has actions disabled or empty; it can never produce "
                                "an Action history item")
        topic_arn = f"arn:aws:sns:{r}:{account}:{names['topic']}"
        subs = sns.list_subscriptions_by_topic(TopicArn=topic_arn)["Subscriptions"]
        # Tenant: Connect configured, no callback flow (safety).
        tc = ddb.Table(names["tenant_config"])
        ptr = tc.get_item(Key={"tenantId": args.tenant, "version": "current"}).get("Item")
        if not ptr or "activeVersion" not in ptr:
            raise Preflight(f"tenant {args.tenant} has no current config")
        cfg = tc.get_item(Key={"tenantId": args.tenant, "version": ptr["activeVersion"]}).get("Item") or {}
        connect = ((cfg.get("channels") or {}).get("connect") or {})
        if not (connect.get("instanceArn") and connect.get("contactFlowId")):
            raise Preflight(f"tenant {args.tenant} has no Connect channel; /escalate would 500")
        if ((cfg.get("escalation") or {}).get("callback") or {}).get("contactFlowId"):
            raise Preflight(f"tenant {args.tenant} has a callback flow configured; refusing to run "
                            "/escalate against it (it could place a real phone call)")
        instance_arn = connect["instanceArn"]
        veh = ddb.Table(names["vehicles"]).get_item(Key={"vehicleId": args.refusal_vehicle}).get("Item")
        if not veh or not veh.get("vin"):
            raise Preflight(f"vehicle {args.refusal_vehicle} not found or has no vin")
        refusal_vin = veh["vin"]
        # Escalation security W1: the caller must be the vehicle's assigned driver.
        # The drivers table has no vehicle index, so scan it (paginated) for rows
        # assigned to the refusal vehicle.
        drivers_tbl = ddb.Table(names["drivers"])
        assigned: list[dict[str, Any]] = []
        scan_kw: dict[str, Any] = {"ProjectionExpression": "driverId, assignedVehicleId, phone"}
        while True:
            page = drivers_tbl.scan(**scan_kw)
            assigned += [d for d in page.get("Items", []) if d.get("assignedVehicleId") == args.refusal_vehicle]
            if "LastEvaluatedKey" not in page:
                break
            scan_kw["ExclusiveStartKey"] = page["LastEvaluatedKey"]
        if len(assigned) != 1:
            raise Preflight(f"vehicle {args.refusal_vehicle} has {len(assigned)} assigned drivers; need exactly 1 "
                            "(the P0 gate requires the caller to be the assigned driver)")
        phone_digits = "".join(ch for ch in str(assigned[0].get("phone") or "") if ch.isdigit())
        fictional = (len(phone_digits) == 11 and phone_digits[0] == "1"
                     and phone_digits[4:7] == "555" and phone_digits[7:9] == "01")
        if phone_digits and not fictional:
            raise Preflight(f"the driver assigned to {args.refusal_vehicle} has a phone number that is not a "
                            "fictional 555-01xx number; refusing (it could be dialled if a callback flow appeared)")
        refusal_driver = assigned[0]["driverId"]
    except Preflight as e:
        print(f"PREFLIGHT FAILED: {e}")
        return 1
    except Exception as e:  # noqa: BLE001
        print(f"PREFLIGHT FAILED: {type(e).__name__}: {e}")
        return 1
    print(f"  resolved table, stream mapping, 5 Lambdas, escalate wiring, 2 alarms; "
          f"topic subscriptions={len(subs)}; tenant={args.tenant} (no callback flow); "
          f"refusal vehicle={args.refusal_vehicle}")
    if not subs:
        print("  NOTE: the operator topic has 0 subscriptions (CVX_RSA_ALARM_EMAIL=none): "
              "alarm actions publish to a topic nobody reads.")

    table = ddb.Table(names["table"])

    def row(did: str) -> dict[str, Any]:
        return table.get_item(Key={"dispatchId": did}, ConsistentRead=True).get("Item") or {}

    def invoke(fn: str, event: dict[str, Any], tail: bool = False) -> tuple[dict[str, Any], str]:
        resp = lam.invoke(FunctionName=fn, InvocationType="RequestResponse",
                          LogType="Tail" if tail else "None", Payload=json.dumps(event).encode())
        if resp.get("FunctionError"):
            raise RuntimeError(f"{fn} FunctionError={resp['FunctionError']}")
        logs = base64.b64decode(resp.get("LogResult", "")).decode("utf-8", "replace") if tail else ""
        return json.loads(resp["Payload"].read()), logs

    def get_as(tenant: str, did: str) -> tuple[int, dict[str, Any]]:
        env, _ = invoke(names["reader"], {
            "httpMethod": "GET",
            "pathParameters": {"dispatchId": did},
            "requestContext": {"authorizer": {"claims": {"custom:tenantId": tenant}}},
        })
        return env.get("statusCode"), json.loads(env.get("body") or "{}")

    run_start = datetime.now(timezone.utc)
    try:
        # ─── Start the slow parts first: Part A row and Part C row ──────────
        a_id = f"rsa-{uuid.uuid4()}"
        table.put_item(Item={
            "dispatchId": a_id, "createdAt": iso(datetime.now(timezone.utc)), "status": "REQUESTED",
            "severity": "P0", "source": TEST_SOURCE, "tenantId": TENANT_A,
            "vehicleId": "LIVE-VERIFY-RSA", "vin": SYNTH_VIN, "reason": "live-verify lifecycle",
            "serviceType": "roadside", "lat": SYNTH_LAT, "lon": SYNTH_LON,
        })
        created_rows.append(a_id)

        c_id = f"rsa-{uuid.uuid4()}"
        # Written under a non-REQUESTED status so the consumer (INSERT + REQUESTED
        # only) skips it, then moved to REQUESTED with a createdAt older than the
        # stale threshold. That is the shape of a row whose stream record went to
        # the DLQ: REQUESTED, never dispatched, and old.
        table.put_item(Item={
            "dispatchId": c_id, "createdAt": iso(datetime.now(timezone.utc)), "status": "LIVE_VERIFY_HOLD",
            "severity": "P0", "source": TEST_SOURCE, "tenantId": TENANT_A,
            "vehicleId": "LIVE-VERIFY-RSA", "reason": "live-verify reconciliation", "serviceType": "roadside",
        })
        created_rows.append(c_id)
        stale_threshold = int(fns["advancer"]["Environment"]["Variables"].get(
            "RSA_REQUESTED_STALE_AFTER_SECONDS", "900"))
        table.update_item(
            Key={"dispatchId": c_id},
            UpdateExpression="SET #st = :req, createdAt = :old",
            ExpressionAttributeNames={"#st": "status"},
            ExpressionAttributeValues={":req": "REQUESTED",
                                       ":old": iso(datetime.now(timezone.utc) - timedelta(seconds=stale_threshold + 300))},
        )
        c_marked_at = datetime.now(timezone.utc)
        print(f"\nStarted: lifecycle row {a_id}; reconciliation row {c_id}")

        # ─── Part A (first half): consumer dispatch + tenant-scoped read ────
        print("\nPart A — consumer dispatch and tenant-scoped read")
        a = wait_for(lambda: row(a_id), lambda x: x.get("status") not in (None, "REQUESTED"), 120, 3)
        run.check("A1 consumer moved the REQUESTED row off REQUESTED", a.get("status") in
                  ("DISPATCHED", "EN_ROUTE", "ARRIVED"), f"status={a.get('status')}")
        run.check("A2 providerRef is a SIM- reference", str(a.get("providerRef", "")).startswith("SIM-"),
                  f"providerRef={a.get('providerRef')}")
        eta = a.get("etaMinutes")
        run.check("A3 etaMinutes is a positive integer", isinstance(eta, Decimal) and eta == eta.to_integral()
                  and eta > 0, f"etaMinutes={eta}")
        run.check("A4 simulated is true on the row", a.get("simulated") is True)
        run.check("A5 dispatchedAt written", bool(a.get("dispatchedAt")))

        code_a, body_a = get_as(TENANT_A, a_id)
        run.check("A6 GET as the owning tenant returns 200", code_a == 200, f"status={code_a}")
        run.check("A7 GET body says simulated=true and carries the SIM- ref",
                  body_a.get("simulated") is True and str(body_a.get("providerRef", "")).startswith("SIM-"))
        leaked = sorted(NEVER_RETURNED & set(body_a))
        run.check("A8 GET body returns none of the G3-D8 never-returned fields", not leaked,
                  f"leaked={leaked}" if leaked else "")
        code_b, body_b = get_as(TENANT_B, a_id)
        code_n, body_n = get_as(TENANT_B, f"rsa-{uuid.uuid4()}")
        run.check("A9 GET as another tenant returns 404", code_b == 404, f"status={code_b}")
        run.check("A10 cross-tenant 404 body is identical to the missing-id 404 body",
                  code_n == 404 and body_b == body_n)

        # ─── Part B: /escalate refusal path (F8.5) ──────────────────────────
        print(f"\nPart B — /escalate on {args.refusal_vehicle} (no fresh Redis position)")
        ls_env, _ = invoke(names["live_state"], {"httpMethod": "GET",
                                                  "pathParameters": {"vehicleId": args.refusal_vehicle}})
        ls = json.loads(ls_env.get("body") or "{}")
        run.check("B1 live-state returns 200", ls_env.get("statusCode") == 200, f"status={ls_env.get('statusCode')}")
        run.check("B2 live-state response carries locationSource and vin (F6.1/F7.2)",
                  "locationSource" in ls and bool(ls.get("vin")), f"locationSource={ls.get('locationSource')!r}")
        fresh = ls.get("locationSource") == "redis" and isinstance(ls.get("lastSeenAgoSeconds"), (int, float)) \
            and ls["lastSeenAgoSeconds"] <= int(esc_env.get("RSA_POSITION_MAX_AGE_SECONDS", "900"))
        if fresh:
            raise Preflight(f"{args.refusal_vehicle} now has a fresh Redis position; pick a vehicle without one")

        triage_fixture = f"live-verify-rsa-{uuid.uuid4()}"
        ddb.Table(names["triage"]).put_item(Item={
            "sessionId": triage_fixture, "tenantId": args.tenant, "vin": refusal_vin,
            "classification": "P0", "decidedAt": iso(datetime.now(timezone.utc)),
            "source": TEST_SOURCE, "ttl": int(time.time()) + 3600,
        })
        esc_env_out, esc_logs = invoke(names["escalate"], {
            "httpMethod": "POST",
            # The assigned driver's own identity (security W1). The tenant has no
            # callback flow (preflight), so no number is dialled either way.
            "requestContext": {"authorizer": {"claims": {"custom:tenantId": args.tenant,
                                                         "custom:driverId": refusal_driver}}},
            "body": json.dumps({
                "severity": "P0", "escalationType": "safety", "vin": refusal_vin,
                "vehicleId": args.refusal_vehicle, "dispatchRoadside": True,
                "triageSessionId": triage_fixture,
                "reason": "LIVE-VERIFY rsa 3.3 refusal path; not a real incident",
            }),
        }, tail=True)
        esc_body = json.loads(esc_env_out.get("body") or "{}")
        if esc_body.get("contactId"):
            chat = {"contactId": esc_body["contactId"], "instanceId": instance_arn.split("/")[-1],
                    "region": instance_arn.split(":")[3]}
        run.check("B3 /escalate returns 200", esc_env_out.get("statusCode") == 200,
                  f"status={esc_env_out.get('statusCode')} error={esc_body.get('error')}")
        b_id = esc_body.get("dispatchId")
        if b_id:
            created_rows.append(b_id)
        run.check("B4 the recorded P0 passed the gate and a dispatch row was written",
                  esc_body.get("rsaDispatched") is True and bool(b_id), f"dispatchId={b_id}")
        pos_lines = [ln for ln in esc_logs.splitlines() if "rsa-position:" in ln]
        invoke_failed = any("invoke failed" in ln or "non-200" in ln for ln in pos_lines)
        refused = any(("locationSource=" in ln or "position stale" in ln) and args.refusal_vehicle in ln
                      for ln in pos_lines)
        run.check("B5 escalate really invoked live-state and refused its position on a real locationSource",
                  refused and not invoke_failed,
                  (pos_lines[-1].split("rsa-position:", 1)[1].strip()[:160] if pos_lines else "no rsa-position log line"))
        run.check("B6 no outbound callback was initiated", "callback: initiated" not in esc_logs)
        if b_id:
            b = wait_for(lambda: row(b_id), lambda x: x.get("status") not in (None, "REQUESTED"), 120, 3)
            run.check("B7 the escalate row reached FAILED, not DISPATCHED", b.get("status") == "FAILED",
                      f"status={b.get('status')}")
            run.check("B8 the failure reason is the location reason",
                      "lat/lon" in str(b.get("failureReason", "")), str(b.get("failureReason", ""))[:90])
            run.check("B9 the refused row has no position and no providerRef",
                      "lat" not in b and "lon" not in b and not b.get("providerRef"))

        # ─── Part D: forced alarm delivers its action ───────────────────────
        print(f"\nPart D — force {names['errors_alarm']}")
        before = cw.describe_alarms(AlarmNames=[names["errors_alarm"]])["MetricAlarms"][0]["StateValue"]
        if before == "ALARM":
            cw.set_alarm_state(AlarmName=names["errors_alarm"], StateValue="OK",
                               StateReason="live-verify-rsa-dispatch: pre-transition to OK")
            time.sleep(5)
        since = datetime.now(timezone.utc)
        cw.set_alarm_state(AlarmName=names["errors_alarm"], StateValue="ALARM",
                           StateReason="live-verify-rsa-dispatch: forced to verify action delivery")
        forced_alarm_reset = True

        def newest_action(alarm: str, after: datetime) -> dict[str, Any] | None:
            items = cw.describe_alarm_history(AlarmName=alarm, HistoryItemType="Action",
                                              StartDate=after, MaxRecords=10)["AlarmHistoryItems"]
            return max(items, key=lambda i: i["Timestamp"]) if items else None

        item = wait_for(lambda: newest_action(names["errors_alarm"], since), lambda x: x is not None, 120, 5)
        data = json.loads(item["HistoryData"]) if item else {}
        run.check("D1 forced errors alarm executed its action (actionState=Succeeded)",
                  data.get("actionState") == "Succeeded",
                  f"actionState={data.get('actionState')!r} {(item or {}).get('HistorySummary', '')[:100]}")
        cw.set_alarm_state(AlarmName=names["errors_alarm"], StateValue="OK",
                           StateReason="live-verify-rsa-dispatch: reset after forced ALARM")
        forced_alarm_reset = False

        # ─── Part A (second half): EN_ROUTE, ARRIVED, terminal ──────────────
        print("\nPart A — advancer: EN_ROUTE then ARRIVED")
        a = wait_for(lambda: row(a_id), lambda x: x.get("status") in ("EN_ROUTE", "ARRIVED"), 240, 10)
        run.check("A11 advancer moved the row to EN_ROUTE (or beyond)", a.get("status") in ("EN_ROUTE", "ARRIVED")
                  and bool(a.get("enRouteAt")), f"status={a.get('status')}")
        a = wait_for(lambda: row(a_id), lambda x: x.get("status") == "ARRIVED", args.timeout_seconds, 15)
        run.check("A12 advancer moved the row to ARRIVED", a.get("status") == "ARRIVED" and bool(a.get("arrivedAt")),
                  f"status={a.get('status')}")
        code_a2, body_a2 = get_as(TENANT_A, a_id)
        run.check("A13 GET after arrival reports ARRIVED with arrivedAt and asOf",
                  code_a2 == 200 and body_a2.get("status") == "ARRIVED" and bool(body_a2.get("arrivedAt"))
                  and bool(body_a2.get("asOf")))
        if a.get("status") == "ARRIVED":
            time.sleep(75)  # at least one more advancer tick
            run.check("A14 ARRIVED is terminal: unchanged after another advancer run",
                      row(a_id).get("status") == "ARRIVED")

        # ─── Part C: reconciliation and its alarm ───────────────────────────
        print("\nPart C — reconciliation of a stale REQUESTED row")
        c = wait_for(lambda: row(c_id), lambda x: x.get("status") not in (None, "REQUESTED"), 180, 10)
        run.check("C1 advancer reconciled the stale REQUESTED row to FAILED", c.get("status") == "FAILED",
                  f"status={c.get('status')}")
        reason = str(c.get("failureReason", ""))
        run.check("C2 the reconciliation reason names the DLQ", "DLQ" in reason or "dlq" in reason, reason[:90])
        rec_item = wait_for(lambda: newest_action(names["reconciliation_alarm"], c_marked_at),
                            lambda x: x is not None, max(600, args.timeout_seconds // 2), 20)
        rec_data = json.loads(rec_item["HistoryData"]) if rec_item else {}
        run.check("C3 reconciliation alarm fired from the real metric filter and executed its action",
                  rec_data.get("actionState") == "Succeeded",
                  f"actionState={rec_data.get('actionState')!r} {(rec_item or {}).get('HistorySummary', '')[:100]}")
    except Preflight as e:
        run.check("mid-run precondition", False, str(e))
    except Exception as e:  # noqa: BLE001
        run.check("unexpected error", False, f"{type(e).__name__}: {e}")
    finally:
        print("\nCleanup")
        if forced_alarm_reset:
            try:
                cw.set_alarm_state(AlarmName=names["errors_alarm"], StateValue="OK",
                                   StateReason="live-verify-rsa-dispatch: reset in cleanup")
                print("  errors alarm reset to OK")
            except Exception as e:  # noqa: BLE001
                print(f"  WARNING: could not reset errors alarm: {type(e).__name__}")
        if chat:
            try:
                boto3.client("connect", region_name=chat["region"]).stop_contact(
                    ContactId=chat["contactId"], InstanceId=chat["instanceId"])
                print(f"  stopped Connect chat contact {chat['contactId']}")
            except Exception as e:  # noqa: BLE001
                print(f"  WARNING: could not stop chat contact {chat['contactId']}: {type(e).__name__}")
        if triage_fixture:
            try:
                ddb.Table(names["triage"]).delete_item(Key={"sessionId": triage_fixture})
                print(f"  deleted triage fixture {triage_fixture}")
            except Exception as e:  # noqa: BLE001
                print(f"  WARNING: could not delete triage fixture: {type(e).__name__}")
        if args.keep:
            print(f"  kept dispatch rows: {created_rows}")
        else:
            for did in created_rows:
                try:
                    table.delete_item(Key={"dispatchId": did})
                    print(f"  deleted dispatch row {did}")
                except Exception as e:  # noqa: BLE001
                    print(f"  WARNING: could not delete {did}: {type(e).__name__}")

    elapsed = (datetime.now(timezone.utc) - run_start).total_seconds()
    print(f"\n{len(run.results) - len(run.failed)}/{len(run.results)} checks passed in {elapsed:.0f}s "
          f"(topic subscriptions: {len(subs)})")
    if not run.results:
        print("FAIL: no checks ran")
        return 1
    return 1 if run.failed else 0


if __name__ == "__main__":
    sys.exit(main())
