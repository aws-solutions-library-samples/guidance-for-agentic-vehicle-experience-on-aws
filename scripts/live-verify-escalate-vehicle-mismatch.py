#!/usr/bin/env python3
"""Live check: a mismatched `vehicleId` yields no RSA dispatch and no outbound call,
while the Connect chat still opens — the F4.8 mismatch half carried in backlog row
`Escalation live checks carried`.

Spec `.kiro/specs/2026-09-23-cvx-escalation-recommend-not-act/` Fix Group 4,
task F4.8. `_vehicle_matches_vin` at `lambdas/api-escalate/handler.py:1069` fails
closed when the body `vehicleId`'s stored VIN does not equal the body `vin`:
`p0_verified` becomes False, so `dispatch_roadside` is forced False (no RSA row is
written) and the P0 outbound callback branch is skipped. Every pre-existing test
stubs the vehicles table; a stub cannot fail the way a service fails, so this
check drives the deployed Lambda against the real `cms-{stage}-storage-vehicles`
row.

Direct Lambda invoke, no iOS. Mirrors `scripts/live-verify-rsa-dispatch.py` Part B
(the position-refusal half of F4.8) in shape, safety preflight and cleanup. The
one unavoidable side effect is a real Amazon Connect chat contact — opens one,
stops it after.

Safety
------
- Refuses to run without LIVE_TESTS=1 (exit 2).
- Preflight refuses a tenant whose config has a callback flow configured
  (defense-in-depth: `p0_verified=False` already skips the callback branch, but
  the preflight means the outbound dial path is structurally unreachable even
  under a hypothetical bug).
- Preflight refuses if the assigned driver's phone is not absent and not a
  fictional 555-01xx number.
- Caller claims deliberately present `custom:tenantId` and `custom:driverId`
  (meridian): if `_vehicle_matches_vin` returned True (it does not), the
  subsequent `_caller_assigned_driver` gate would still refuse because the
  caller drives `MRDN0000000000015`, not the mismatch vehicleId.
- Deletes the triage fixture row and stops the Connect chat on exit.
- Never prints a VIN, phone number, or driver name.

Anti-vacuity: if any of the triage table, escalate Lambda, tenant config,
vehicle row, assigned driver, or the handler's log tail are missing, the script
exits non-zero with the reason. It never passes without checking.

Usage
-----
    LIVE_TESTS=1 .venv/bin/python3 scripts/live-verify-escalate-vehicle-mismatch.py
    LIVE_TESTS=1 .venv/bin/python3 scripts/live-verify-escalate-vehicle-mismatch.py \
        --stage staging --region us-west-2 --tenant meridian-fleet \
        --driver-id DRV-MRDN-0015 --body-vehicle-id MRDN0000000000015

Exit codes: 0 all checks passed · 1 a check failed or preflight failed · 2 skipped
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import secrets
import sys
import time
import uuid
from datetime import datetime, timezone
from typing import Any

TEST_SOURCE = "live-verify-escalate-vehicle-mismatch"
# A synthetic VIN that must NEVER match a real vehicles row. The point of the
# test is that `_vehicle_matches_vin(body_vehicle_id, body_vin)` reads the
# vehicles row for `body_vehicle_id` and finds its stored vin ≠ this value.
SYNTH_VIN_PREFIX = "LVEMISMATCH"


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class Preflight(Exception):
    pass


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


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--stage", default="staging")
    ap.add_argument("--region", default="us-west-2")
    ap.add_argument("--tenant", default="meridian-fleet",
                    help="tenant for /escalate; must have Connect configured and NO callback flow")
    ap.add_argument("--driver-id", default="DRV-MRDN-0015",
                    help="caller's `custom:driverId` claim; must be the assigned driver of --body-vehicle-id")
    ap.add_argument("--body-vehicle-id", default="MRDN0000000000015",
                    help="the vehicleId the mismatched request body carries; its real VIN must not equal the synthetic one")
    args = ap.parse_args()

    if os.environ.get("LIVE_TESTS") != "1":
        print("SKIP: set LIVE_TESTS=1. This run writes one triage-fixture row and opens one Connect chat.")
        return 2

    import boto3

    s, r = args.stage, args.region
    ddb = boto3.resource("dynamodb", region_name=r)
    ddbc = boto3.client("dynamodb", region_name=r)
    lam = boto3.client("lambda", region_name=r)
    sts = boto3.client("sts", region_name=r)

    names = {
        "escalate": f"vsa-{s}-api-escalate",
        "tenant_config": f"vsa-{s}-tenant-config",
        "triage": f"vsa-{s}-triage-decisions",
        "rsa_dispatch": f"vsa-{s}-rsa-dispatch",
        "vehicles": f"cms-{s}-storage-vehicles",
        "drivers": f"cms-{s}-storage-drivers",
    }
    run = Run()
    chat: dict[str, str] | None = None
    triage_fixture: str | None = None

    # ─── Preflight ────────────────────────────────────────────────────────────
    print(f"Preflight ({s}, {r})")
    try:
        account = sts.get_caller_identity()["Account"]
        _ = account  # used only to prove credentials
        cfg_conf = lam.get_function_configuration(FunctionName=names["escalate"])
        if cfg_conf["State"] != "Active":
            raise Preflight(f"{names['escalate']} is {cfg_conf['State']}")
        esc_env = cfg_conf.get("Environment", {}).get("Variables", {})
        for key, expected in (("TRIAGE_DECISIONS_TABLE", names["triage"]),
                              ("CMS_VEHICLES_TABLE", names["vehicles"]),
                              ("CMS_DRIVERS_TABLE", names["drivers"]),
                              ("RSA_DISPATCH_TABLE", names["rsa_dispatch"]),
                              ("TENANT_CONFIG_TABLE", names["tenant_config"])):
            if esc_env.get(key) != expected:
                raise Preflight(f"escalate {key} is {esc_env.get(key)!r}, expected {expected!r}")
        tc = ddb.Table(names["tenant_config"])
        ptr = tc.get_item(Key={"tenantId": args.tenant, "version": "current"}).get("Item")
        if not ptr or "activeVersion" not in ptr:
            raise Preflight(f"tenant {args.tenant} has no current config pointer")
        cfg = tc.get_item(Key={"tenantId": args.tenant, "version": ptr["activeVersion"]}).get("Item") or {}
        connect = ((cfg.get("channels") or {}).get("connect") or {})
        if not (connect.get("instanceArn") and connect.get("contactFlowId")):
            raise Preflight(f"tenant {args.tenant} has no Connect channel; /escalate would 500")
        if ((cfg.get("escalation") or {}).get("callback") or {}).get("contactFlowId"):
            raise Preflight(f"tenant {args.tenant} has a callback flow configured; refusing to run")
        instance_arn = connect["instanceArn"]
        veh = ddb.Table(names["vehicles"]).get_item(Key={"vehicleId": args.body_vehicle_id}).get("Item")
        if not veh or not veh.get("vin"):
            raise Preflight(f"vehicle {args.body_vehicle_id} not found or has no vin")
        real_vin = str(veh["vin"])
        drv = ddb.Table(names["drivers"]).get_item(Key={"driverId": args.driver_id}).get("Item")
        if not drv:
            raise Preflight(f"driver {args.driver_id} not found")
        if drv.get("assignedVehicleId") != args.body_vehicle_id:
            raise Preflight(
                f"driver {args.driver_id} is not assigned to {args.body_vehicle_id}; "
                "the caller must be the assigned driver as defense-in-depth"
            )
        phone_digits = "".join(ch for ch in str(drv.get("phone") or "") if ch.isdigit())
        fictional = (len(phone_digits) == 11 and phone_digits[0] == "1"
                     and phone_digits[4:7] == "555" and phone_digits[7:9] == "01")
        if phone_digits and not fictional:
            raise Preflight(
                f"driver {args.driver_id} has a phone number that is not a fictional 555-01xx number; "
                "refusing (defense-in-depth)"
            )
    except Preflight as e:
        print(f"PREFLIGHT FAILED: {e}")
        return 1
    except Exception as e:  # noqa: BLE001
        print(f"PREFLIGHT FAILED: {type(e).__name__}: {e}")
        return 1
    print(f"  resolved escalate wiring, tenant={args.tenant} (no callback flow), "
          f"body vehicleId resolved to a real VIN, assigned driver has a fictional phone")

    # ─── Build the mismatched request ────────────────────────────────────────
    # The body's VIN must NOT equal the vehicles row's stored VIN. The triage
    # decision is keyed on the body's VIN so `_verified_p0_decision` passes; the
    # subsequent `_vehicle_matches_vin(body_vehicle_id, body_vin)` call then
    # looks up the vehicles row and finds a different VIN → False → p0_verified
    # is False and no dispatch nor callback happens.
    mismatch_vin = f"{SYNTH_VIN_PREFIX}{secrets.token_hex(3).upper()}"
    assert mismatch_vin != real_vin
    triage_fixture = f"live-verify-escalate-mismatch-{uuid.uuid4()}"
    ddb.Table(names["triage"]).put_item(Item={
        "sessionId": triage_fixture, "tenantId": args.tenant, "vin": mismatch_vin,
        "classification": "P0", "decidedAt": iso(datetime.now(timezone.utc)),
        "source": TEST_SOURCE, "ttl": int(time.time()) + 3600,
    })
    print(f"\nWrote triage fixture {triage_fixture} (vin={mismatch_vin})")

    try:
        print("\nInvoking /escalate with a mismatched (vehicleId, vin) and dispatchRoadside=true")
        payload = {
            "httpMethod": "POST",
            "requestContext": {"authorizer": {"claims": {
                "custom:tenantId": args.tenant,
                "custom:driverId": args.driver_id,  # defense-in-depth; _caller_assigned_driver is never reached
            }}},
            "body": json.dumps({
                "severity": "P0", "escalationType": "safety",
                "vin": mismatch_vin, "vehicleId": args.body_vehicle_id,
                "dispatchRoadside": True,  # explicit — exercises both P0 side-effect gates
                "triageSessionId": triage_fixture,
                "reason": "LIVE-VERIFY escalate 4.8 mismatch half; not a real incident",
            }),
        }
        resp = lam.invoke(
            FunctionName=names["escalate"], InvocationType="RequestResponse",
            LogType="Tail", Payload=json.dumps(payload).encode(),
        )
        if resp.get("FunctionError"):
            run.check("escalate did not FunctionError", False, resp.get("FunctionError"))
            return 1
        env = json.loads(resp["Payload"].read())
        logs = base64.b64decode(resp.get("LogResult", "")).decode("utf-8", "replace")
        body = json.loads(env.get("body") or "{}")
        if body.get("contactId"):
            chat = {"contactId": body["contactId"],
                    "instanceId": instance_arn.split("/")[-1],
                    "region": instance_arn.split(":")[3]}

        # ── Assertions ───────────────────────────────────────────────────────
        run.check("E1 /escalate returned 200", env.get("statusCode") == 200,
                  f"status={env.get('statusCode')} error={body.get('error')}")
        run.check("E2 the chat opened (contactId present)", bool(body.get("contactId")),
                  f"contactId={('present' if body.get('contactId') else 'missing')}")
        run.check("E3 rsaDispatched is false in the response", body.get("rsaDispatched") is False,
                  f"rsaDispatched={body.get('rsaDispatched')!r}")
        run.check("E4 no RSA dispatch id was returned", not body.get("dispatchId"),
                  f"dispatchId={body.get('dispatchId')!r}")
        bind_lines = [ln for ln in logs.splitlines() if "vehicle-binding:" in ln]
        refused = any(("does not carry the request VIN" in ln) and args.body_vehicle_id in ln
                      for ln in bind_lines)
        run.check("E5 the vehicle-binding refusal line is in the Lambda log",
                  refused and bool(bind_lines),
                  (bind_lines[-1].split("vehicle-binding:", 1)[1].strip()[:140]
                   if bind_lines else "no vehicle-binding log line"))
        run.check("E6 no outbound callback was initiated",
                  "callback: initiated" not in logs,
                  "no 'callback: initiated' log line")
        # Mirror live-verify-rsa-dispatch Part B: assert the P0-driver-gate
        # never spoke, since _vehicle_matches_vin failing short-circuits
        # before _caller_assigned_driver even runs.
        p0_gate_lines = [ln for ln in logs.splitlines() if "p0-driver-gate:" in ln]
        run.check("E7 p0-driver-gate was not reached (vehicle gate short-circuited)",
                  not p0_gate_lines,
                  f"{len(p0_gate_lines)} p0-driver-gate log line(s)")
        # Confirm no RSA row was written for the mismatch session. The handler
        # does not expose a dispatchId when dispatch is skipped, so there is
        # nothing to point at; this is a defensive scan on the triage session.
        scan_kw: dict[str, Any] = {
            "FilterExpression": "triageSessionId = :s",
            "ExpressionAttributeValues": {":s": triage_fixture},
            "Limit": 10,
        }
        rsa_matches = ddb.Table(names["rsa_dispatch"]).scan(**scan_kw).get("Items") or []
        run.check("E8 no row with this triageSessionId exists in the dispatch table",
                  not rsa_matches, f"rows={len(rsa_matches)}")
    finally:
        print("\nCleanup")
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

    print(f"\n{len(run.results) - len(run.failed)}/{len(run.results)} checks passed")
    if not run.results:
        print("FAIL: no checks ran")
        return 1
    return 1 if run.failed else 0


if __name__ == "__main__":
    sys.exit(main())
