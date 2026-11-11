#!/usr/bin/env python3
"""Live check: a DMS repair-order status change reaches its AVX Action row.

issues/2026-10-01-dms-event-consumer-bare-uuid-key/. DMS emits `avx_action_id` as a bare
UUID; the Action row is keyed `AVX-ACT-{uuid}`. Before the fix the consumer looked up the
bare id and every event was `no_op_conditional_check_failed`. Unit tests (MagicMock and moto)
cannot show the real chain works: DMS handler -> DMS EventBridge bus -> CVX rule -> consumer
Lambda -> DynamoDB. This script drives that chain once.

Steps:
  1. Preflight (read-only): the RO exists, carries an avx_action_id, and its Action row exists
     under AVX-ACT-{uuid} in a non-terminal status other than the projected one. The deployed
     consumer zip must contain `_action_key` (proves the fix is what will run).
  2. Move the RO by invoking DMS's real update_repair_order Lambda with synthetic dealer-admin
     claims scoped to the RO's dealer. This is the pattern DMS's own live tests use
     (tests/integration/test_service_lane_live.py); the DMS API authorizer is not under test.
  3. Poll the Action row until its status is the projection of the new RO status.
  4. Find the consumer's `projected action_id=AVX-ACT-...` log line for this RO.

Side effects (printed): the RO's status changes, and the Action's status/updated_at change.
DMS's bus has no other consumer of `dms.ro.status_changed` in staging (checked 2026-10-01), so
no customer notification is sent.

Usage:
  LIVE_TESTS=1 .venv/bin/python scripts/live-verify-dms-event-consumer.py \\
      --ro-id <ro_id> [--to-status Ready|Delivered] [--preflight-only]
"""
from __future__ import annotations

import argparse
import io
import json
import os
import re
import sys
import time
import urllib.request
import zipfile

REGION = "us-west-2"
STAGE = "staging"
RO_TABLE = f"dms-{STAGE}-repair-orders"
ACTIONS_TABLE = f"vsa-{STAGE}-avx-actions"
CONSUMER_FN = f"vsa-{STAGE}-avx-dms-event-consumer-{REGION}"
CONSUMER_LOG_GROUP = f"/aws/lambda/{CONSUMER_FN}"
DMS_UPDATE_HANDLER = "source.handlers.repair_orders.update_repair_order"
PROJECTION = {"Draft": "in_progress", "Ready": "completed", "Delivered": "completed"}
TERMINAL = {"completed", "cancelled", "declined", "dismissed", "failed"}
UUID_RE = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")

failures: list[str] = []


def check(ok: bool, msg: str) -> bool:
    print(("  PASS " if ok else "  FAIL ") + msg)
    if not ok:
        failures.append(msg)
    return ok


def _dms_update_fn(lam) -> str:
    """Resolve the DMS update Lambda by its handler; fail if not exactly one."""
    names = []
    for page in lam.get_paginator("list_functions").paginate():
        names += [f["FunctionName"] for f in page["Functions"]
                  if f.get("Handler") == DMS_UPDATE_HANDLER and STAGE in f["FunctionName"]]
    if len(names) != 1:
        sys.exit(f"expected one DMS update_repair_order Lambda in {STAGE}, found {names}")
    return names[0]


def _deployed_consumer_has_fix(lam) -> bool:
    url = lam.get_function(FunctionName=CONSUMER_FN)["Code"]["Location"]
    with urllib.request.urlopen(url, timeout=30) as resp:  # presigned S3 URL from AWS
        data = resp.read()
    src = zipfile.ZipFile(io.BytesIO(data)).read("handler.py").decode()
    return "def _action_key(" in src and "_ACTION_ID_PREFIX + bare.lower()" in src


def main() -> int:
    if os.environ.get("LIVE_TESTS") != "1":
        sys.exit("refusing to run without LIVE_TESTS=1 (this writes to staging DMS and AVX tables)")
    ap = argparse.ArgumentParser()
    ap.add_argument("--ro-id", required=True)
    ap.add_argument("--to-status", default="Ready", choices=["Ready", "Delivered"])
    ap.add_argument("--preflight-only", action="store_true",
                    help="run step 1 (read-only) and stop; still requires LIVE_TESTS=1")
    args = ap.parse_args()

    import boto3

    ddb = boto3.resource("dynamodb", region_name=REGION)
    lam = boto3.client("lambda", region_name=REGION)
    logs = boto3.client("logs", region_name=REGION)

    print("1. Preflight")
    ro = ddb.Table(RO_TABLE).get_item(Key={"ro_id": args.ro_id}, ConsistentRead=True).get("Item")
    if not check(ro is not None, f"RO {args.ro_id} exists in {RO_TABLE}"):
        return 1
    corr = str(ro.get("avx_action_id") or "")
    if not check(bool(UUID_RE.fullmatch(corr)), f"RO carries a bare-UUID avx_action_id ({corr!r})"):
        return 1
    key = f"AVX-ACT-{corr.lower()}"
    projected = PROJECTION[args.to_status]
    actions = ddb.Table(ACTIONS_TABLE)
    before = actions.get_item(Key={"action_id": key}, ConsistentRead=True).get("Item")
    if not check(before is not None, f"Action row {key} exists"):
        return 1
    check(actions.get_item(Key={"action_id": corr}).get("Item") is None,
          "no Action row under the bare id (the key the old consumer used)")
    status0 = before.get("status")
    if not check(status0 not in TERMINAL and status0 != projected,
                 f"Action status {status0!r} can move to {projected!r}"):
        return 1
    check(ro.get("status") != args.to_status,
          f"RO status {ro.get('status')!r} differs from target {args.to_status!r}")
    fix_deployed = check(_deployed_consumer_has_fix(lam), f"deployed {CONSUMER_FN} contains _action_key")
    if args.preflight_only:
        print("RESULT (preflight only):", "PASS" if not failures else f"FAIL ({len(failures)})")
        return 0 if not failures else 1
    if not fix_deployed or failures:
        return 1

    print(f"2. Move RO {args.ro_id} {ro.get('status')} -> {args.to_status} via DMS update_repair_order")
    started_ms = int(time.time() * 1000) - 5_000
    fn = _dms_update_fn(lam)
    event = {
        "requestContext": {"authorizer": {"claims": {
            "sub": "live-verify-dms-event-consumer",
            "cognito:groups": "dealer-admin",
            "custom:dealerIds": str(ro["dealer_id"]),
        }}},
        "pathParameters": {"dealerId": str(ro["dealer_id"]), "roId": args.ro_id},
        "body": json.dumps({"status": args.to_status}),
    }
    resp = json.loads(lam.invoke(FunctionName=fn, Payload=json.dumps(event).encode())["Payload"].read())
    if not check(resp.get("statusCode") == 200, f"DMS update returned {resp.get('statusCode')}"):
        print("    body:", str(resp.get("body"))[:300])
        return 1

    print(f"3. Poll {key} for status={projected!r}")
    after = None
    for _ in range(30):
        after = actions.get_item(Key={"action_id": key}, ConsistentRead=True).get("Item") or {}
        if after.get("status") == projected:
            break
        time.sleep(3)
    check(after.get("status") == projected, f"Action status {status0!r} -> {after.get('status')!r}")
    check(after.get("updated_at") != before.get("updated_at"),
          f"updated_at moved ({before.get('updated_at')} -> {after.get('updated_at')})")

    print("4. Consumer log")
    needle = f"projected action_id={key}"
    found = None
    for _ in range(10):
        events = logs.filter_log_events(logGroupName=CONSUMER_LOG_GROUP, startTime=started_ms,
                                        filterPattern='"projected action_id"').get("events", [])
        found = next((e["message"].strip() for e in events if needle in e["message"]), None)
        if found:
            break
        time.sleep(3)
    check(found is not None, f"consumer logged {needle!r}")
    if found:
        print("    ", found[:240])

    print(f"\nSide effects: RO {args.ro_id} status={args.to_status}; Action {key} status={after.get('status')}")
    print("RESULT:", "PASS" if not failures else f"FAIL ({len(failures)})")
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
