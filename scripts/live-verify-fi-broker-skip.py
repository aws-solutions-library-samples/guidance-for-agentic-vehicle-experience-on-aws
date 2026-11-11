#!/usr/bin/env python3
"""Live verification of the AVX broker's FI-item skip (FI Stage C task 1.10, spec D13).

The broker (``agents/supervisor/lambdas/avx_broker.py``) must return
``skip_reason="fi_item"`` for any FI Finding before it resolves the owner, reads a
gate, counts, writes a card or publishes. Its unit tests stub DynamoDB, SSM and
SNS; this script checks the deployed broker against the real findings stream.

What it creates, all removed in ``finally``:
  * one ephemeral user in the CMS Cognito pool (``livetest-fi-broker-...@example.invalid``),
    with no password and no registered device;
  * one Finding ``LIVETEST-FI-BROKER-<run>``: ``agent_id=agent-fi-recall-warranty``,
    ``finding_kind=fleet.recall.vehicle``, severity ``p0``, fleet ``LIVETEST-FLEET-<run>``
    (a fleet no iOS or CMS user holds), and the user's ``sub`` projected as ``owner_id``.
    It carries no ``recommended_action``, so the auto-approver's stream filter (which
    needs ``recommended_action.action_kind``) never receives it, and the agent-id branch
    of the broker's predicate is the one under test.

Checks:
  1. preconditions: the broker's log group exists; the user has no registered device;
  2. the broker logs ``fi_item finding_id=<id>`` within the wait;
  3. the broker never logs ``processing finding_id=<id>`` (that line comes after the FI
     check, at owner resolution), nor any other skip or push outcome for the id;
  4. no card Action exists for the Finding: the broker's own action_id (derived with its
     ``_card_identity``) is absent on a consistent read, and the ``by_finding`` and
     ``by_created_by`` indexes return nothing for it;
  5. the broker wrote no rate-limit row for the user's sub;
  6. residue after teardown: the Finding, any Action and rate rows, and every
     ``livetest-fi-broker-`` user are gone.

A run against a broker without the skip fails check 2 and 3: the user has no device,
so the old code reaches the consent gate and logs ``consent_off`` instead.

Usage:
  LIVE_TESTS=1 .venv/bin/python scripts/live-verify-fi-broker-skip.py --stage staging --region us-west-2

Exit 0 every check passed, 1 a check failed, 2 refused to run.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import os
import pathlib
import re
import secrets
import signal
import sys
import time
from typing import Any

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

RUN_PREFIX = "LIVETEST-FI-BROKER-"
USER_PREFIX = "livetest-fi-broker-"
WAIT_S = 180
POLL_S = 5
# Per-finding lines the broker logs for a Finding it did NOT skip as an FI item. Seeing
# any of them for our finding_id means the skip did not run first. Each carries its own
# key=value suffix, so the handler's batch summary line (a dict repr, which may name
# other findings' outcomes) never matches.
OTHER_OUTCOMES = (
    "processing finding_id=",
    "owner_absent finding_id=",
    "owner_namespace_unmatched owner_id=",
    "already_carded action_id=",
    "cardinality_cap exceeded finding_id=",
    "consent_off finding_id=",
    "rate_limit exceeded finding_id=",
    "apns_push:",
    "apns skipped",
)


class Results:
    def __init__(self) -> None:
        self.rows: list[tuple[str, str, str]] = []

    def check(self, item: str, ok: bool, detail: str) -> bool:
        self.rows.append((item, "PASS" if ok else "FAIL", detail))
        print(f"  [{'PASS' if ok else 'FAIL'}] {item}: {detail}", flush=True)
        return ok

    @property
    def failed(self) -> list[tuple[str, str, str]]:
        return [r for r in self.rows if r[1] == "FAIL"]


def _iso(t: dt.datetime) -> str:
    return t.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def make_finding(fid: str, fleet: str, owner_sub: str) -> dict[str, Any]:
    now = dt.datetime.now(dt.timezone.utc)
    return {
        "finding_id": fid,
        "finding_key": hashlib.sha256(fid.encode()).hexdigest(),
        "agent_id": "agent-fi-recall-warranty",
        "agent_version": "0.0.0-livetest",
        "scope": {"fleet_id": fleet},
        "fleet_id": fleet,
        "owner_id": owner_sub,
        "severity": "p0",
        "computed_at": _iso(now),
        "expires_at": _iso(now + dt.timedelta(hours=1)),
        "confidence": "high",
        "finding_kind": "fleet.recall.vehicle",
        "headline": "LIVETEST fixture (FI Stage C broker skip live verification)",
        "detail": "LIVETEST fixture; deleted by scripts/live-verify-fi-broker-skip.py.",
        "evidence": [],
        "inputs_hash": hashlib.sha256(b"livetest").hexdigest(),
        "tokens_used": 0,
        "cost_usd": 0,
        "audit_trail": [],
        "subject_key": fid,
    }


def query_all(table: Any, **kwargs: Any) -> list[dict]:
    items: list[dict] = []
    while True:
        page = table.query(**kwargs)
        items += page.get("Items", [])
        if "LastEvaluatedKey" not in page:
            return items
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def log_messages(logs: Any, group: str, start_ms: int, fid: str) -> list[str]:
    """Every broker log message since start_ms that names fid (all pages)."""
    msgs: list[str] = []
    kwargs: dict[str, Any] = {"logGroupName": group, "startTime": start_ms,
                              "filterPattern": f'"{fid}"'}
    while True:
        page = logs.filter_log_events(**kwargs)
        msgs += [e["message"] for e in page.get("events", [])]
        token = page.get("nextToken")
        if not token:
            return msgs
        kwargs["nextToken"] = token


def main() -> int:
    if os.environ.get("LIVE_TESTS") != "1":
        print("SKIPPED: set LIVE_TESTS=1. This writes one LIVETEST- Finding to the AVX "
              "findings table and creates one ephemeral Cognito user.")
        return 2
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", default="staging")
    ap.add_argument("--region", default="us-west-2")
    args = ap.parse_args()
    if args.stage != "staging":
        print("REFUSED: staging only (tasks.md task 1.10)", file=sys.stderr)
        return 2

    import boto3
    from boto3.dynamodb.conditions import Key

    from agents.supervisor.lambdas.avx_broker import _card_identity

    stage, region = args.stage, args.region
    fn_name = f"vsa-{stage}-avx-broker-{region}"
    log_group = f"/aws/lambda/{fn_name}"
    lam = boto3.client("lambda", region_name=region)
    logs = boto3.client("logs", region_name=region)
    idp = boto3.client("cognito-idp", region_name=region)
    ssm = boto3.client("ssm", region_name=region)
    ddb = boto3.resource("dynamodb", region_name=region)

    # Use the tables the deployed broker uses, not names assumed here.
    env = lam.get_function_configuration(FunctionName=fn_name)["Environment"]["Variables"]
    findings = ddb.Table(env["AVX_FINDINGS_TABLE"])
    actions = ddb.Table(env["AVX_ACTIONS_TABLE"])
    rates = ddb.Table(env["AVX_RATE_LIMITS_TABLE"])
    devices = ddb.Table(env["AVX_NOTIFICATION_DEVICES_TABLE"])
    pool_id = ssm.get_parameter(Name=f"/cvx/{stage}/voice-auth-user-pool-id")["Parameter"]["Value"].strip()

    if not logs.describe_log_groups(logGroupNamePrefix=log_group).get("logGroups"):
        print(f"REFUSED: log group {log_group} not found", file=sys.stderr)
        return 2

    run = f"{int(time.time())}-{secrets.token_hex(3)}"
    fid = f"{RUN_PREFIX}{run}"
    fleet = f"LIVETEST-FLEET-{run}"
    username = f"{USER_PREFIX}{run}@example.invalid"
    res = Results()
    user_created = False
    finding_written = False
    sub = ""

    def _sigterm(_signum: int, _frame: Any) -> None:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)  # a second SIGTERM must not cut teardown
        raise SystemExit(1)

    previous = signal.signal(signal.SIGTERM, _sigterm)
    print(f"run {run}: finding {fid}, fleet {fleet}, broker {fn_name}")
    try:
        user_created = True  # set before the call: a create can land with its response lost
        resp = idp.admin_create_user(
            UserPoolId=pool_id, Username=username, MessageAction="SUPPRESS",
            UserAttributes=[{"Name": "email", "Value": username},
                            {"Name": "email_verified", "Value": "true"}])
        sub = next(a["Value"] for a in resp["User"]["Attributes"] if a["Name"] == "sub")
        res.check("1a", bool(re.fullmatch(r"[0-9a-f-]{36}", sub)), "ephemeral user created; its sub is UUID-shaped")
        dev = query_all(devices, KeyConditionExpression=Key("owner_id").eq(sub))
        res.check("1b", not dev, f"user has no registered device ({len(dev)} rows)")

        start_ms = int(time.time() * 1000) - 5000
        finding_written = True  # before the put, for the same reason as above
        findings.put_item(Item=make_finding(fid, fleet, sub),
                          ConditionExpression="attribute_not_exists(finding_id)")

        print(f"\nWaiting up to {WAIT_S}s for the broker's log line")
        deadline = time.time() + WAIT_S
        msgs: list[str] = []
        while time.time() < deadline:
            msgs = log_messages(logs, log_group, start_ms, fid)
            if any(f"fi_item finding_id={fid}" in m for m in msgs) or \
                    any(o in m for m in msgs for o in OTHER_OUTCOMES):
                time.sleep(15)  # let the handler's own summary line land too
                msgs = log_messages(logs, log_group, start_ms, fid)
                break
            time.sleep(POLL_S)
        for m in msgs:
            print(f"    log: {m.strip()[:300]}")
        res.check("2", any(f"fi_item finding_id={fid}" in m for m in msgs),
                  "broker logged fi_item for the Finding")
        others = sorted({o for m in msgs for o in OTHER_OUTCOMES if o in m})
        res.check("3", not others, f"no owner resolution, gate, skip or push line for it: {others or 'none'}")

        action_id, _kind, created_by = _card_identity(make_finding(fid, fleet, sub), sub)
        row = actions.get_item(Key={"action_id": action_id}, ConsistentRead=True).get("Item")
        by_finding = query_all(actions, IndexName="by_finding", KeyConditionExpression=Key("finding_id").eq(fid))
        by_owner = query_all(actions, IndexName="by_created_by", KeyConditionExpression=Key("created_by").eq(created_by))
        res.check("4", row is None and not by_finding and not by_owner,
                  f"no card Action: get_item {'absent' if row is None else 'PRESENT'}, "
                  f"by_finding {len(by_finding)}, by_created_by {len(by_owner)}")
        rate_rows = query_all(rates, KeyConditionExpression=Key("owner_id").eq(sub))
        res.check("5", not rate_rows, f"no rate-limit row for the user ({len(rate_rows)})")
    finally:
        print("\nTeardown")
        if finding_written:
            findings.delete_item(Key={"finding_id": fid})
        if sub:
            # Only rows this run's Finding or ephemeral user could own.
            for it in query_all(actions, IndexName="by_finding", KeyConditionExpression=Key("finding_id").eq(fid)):
                actions.delete_item(Key={"action_id": it["action_id"]},
                                    ConditionExpression="finding_id = :f", ExpressionAttributeValues={":f": fid})
            for it in query_all(rates, KeyConditionExpression=Key("owner_id").eq(sub)):
                rates.delete_item(Key={"owner_id": sub, "window_key": it["window_key"]})
        if user_created:
            try:
                idp.admin_delete_user(UserPoolId=pool_id, Username=username)
            except idp.exceptions.UserNotFoundException:
                pass
            except Exception as exc:  # noqa: BLE001
                print(f"  cleanup FAILED for the user: {type(exc).__name__}. Run:\n"
                      f"    aws cognito-idp admin-delete-user --user-pool-id {pool_id} "
                      f"--username {username} --region {region}", file=sys.stderr)

        left_finding = findings.get_item(Key={"finding_id": fid}, ConsistentRead=True).get("Item")
        left_actions = query_all(actions, IndexName="by_finding", KeyConditionExpression=Key("finding_id").eq(fid))
        left_rates = query_all(rates, KeyConditionExpression=Key("owner_id").eq(sub)) if sub else []
        left_users: list[str] = []
        for page in idp.get_paginator("list_users").paginate(
                UserPoolId=pool_id, Filter=f'email ^= "{USER_PREFIX}"'):
            left_users += [u["Username"] for u in page.get("Users", [])]
        res.check("6", left_finding is None and not left_actions and not left_rates and not left_users,
                  f"residue: finding {'none' if left_finding is None else 'LEFT'}, actions {len(left_actions)}, "
                  f"rate rows {len(left_rates)}, {USER_PREFIX}* users {len(left_users)}")
        signal.signal(signal.SIGTERM, previous)

    print("\nSummary")
    for item, status, detail in res.rows:
        print(f"  {status:4}  {item:3}  {detail}")
    if res.failed:
        print(f"\nFAIL: {len(res.failed)} check(s) failed")
        return 1
    print("\nPASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
