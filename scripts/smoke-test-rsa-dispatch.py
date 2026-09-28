#!/usr/bin/env python3
"""Post-deploy smoke test for the RSA stub dispatch path (spec Task 5.3).

Read-only. Checks that a `vsa-{stage}-rsa` + `vsa-{stage}-api` deploy left the
dispatch path wired and running, without writing a row, opening a Connect chat
or forcing an alarm. For the full end-to-end run, use
`scripts/live-verify-rsa-dispatch.py` (LIVE_TESTS=1).

Checks
------
1. Both stacks are CREATE_COMPLETE or UPDATE_COMPLETE.
2. The consumer, advancer and status-read Lambdas are Active with a Successful
   last update, and the consumer and reader run with VSA_RSA_CLIENT=fake.
3. The consumer has an Enabled event-source mapping on the table's latest stream.
4. The escalate Lambda writes to this table (RSA_DISPATCH_TABLE).
5. The four RSA alarms exist with actions enabled, and each alarm's actions
   include the RSA operator topic.
6. The advancer logged a "lifecycle run complete ... errors=0" line in the last
   5 minutes, and no ERROR/Traceback lines (so it is scheduled and succeeding).
7. GET /rsa/dispatch/{id} without a token returns 401 (the route exists and is
   behind the Cognito authorizer).

It also reports the operator topic's subscription count. Zero is allowed
(CVX_RSA_ALARM_EMAIL=none) but printed, because it means nobody is paged.

Usage:  .venv/bin/python3 scripts/smoke-test-rsa-dispatch.py [--stage staging] [--region us-west-2]
Exit:   0 all checks passed · 1 any check failed
"""

from __future__ import annotations

import argparse
import sys
import time
import urllib.error
import urllib.request

import boto3


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", default="staging")
    ap.add_argument("--region", default="us-west-2")
    a = ap.parse_args()
    s, r = a.stage, a.region

    cfn = boto3.client("cloudformation", region_name=r)
    lam = boto3.client("lambda", region_name=r)
    ddb = boto3.client("dynamodb", region_name=r)
    cw = boto3.client("cloudwatch", region_name=r)
    logs = boto3.client("logs", region_name=r)
    sns = boto3.client("sns", region_name=r)
    account = boto3.client("sts", region_name=r).get_caller_identity()["Account"]

    failures: list[str] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
        if not ok:
            failures.append(name)

    def guarded(name: str, fn) -> None:
        try:
            fn()
        except Exception as e:  # noqa: BLE001
            check(name, False, f"{type(e).__name__}: {e}")

    def stacks() -> None:
        for st in (f"vsa-{s}-rsa", f"vsa-{s}-api"):
            status = cfn.describe_stacks(StackName=st)["Stacks"][0]["StackStatus"]
            check(f"stack {st}", status in ("CREATE_COMPLETE", "UPDATE_COMPLETE"), status)

    def lambdas() -> None:
        for fn, fake in ((f"vsa-{s}-rsa-dispatch-consumer", True), (f"vsa-{s}-rsa-lifecycle-advancer", False),
                         (f"vsa-{s}-api-rsa-dispatch", True)):
            c = lam.get_function_configuration(FunctionName=fn)
            ok = c["State"] == "Active" and c.get("LastUpdateStatus") == "Successful"
            if fake:
                ok = ok and c.get("Environment", {}).get("Variables", {}).get("VSA_RSA_CLIENT") == "fake"
            check(f"lambda {fn}", ok, f"{c['State']}/{c.get('LastUpdateStatus')}"
                  + (f" VSA_RSA_CLIENT={c.get('Environment', {}).get('Variables', {}).get('VSA_RSA_CLIENT')}" if fake else ""))

    def stream_and_escalate() -> None:
        table = f"vsa-{s}-rsa-dispatch"
        stream = ddb.describe_table(TableName=table)["Table"].get("LatestStreamArn")
        maps = lam.list_event_source_mappings(FunctionName=f"vsa-{s}-rsa-dispatch-consumer")["EventSourceMappings"]
        check("consumer mapping on the table's latest stream is Enabled",
              any(m["EventSourceArn"] == stream and m["State"] == "Enabled" for m in maps))
        env = lam.get_function_configuration(FunctionName=f"vsa-{s}-api-escalate")["Environment"]["Variables"]
        check("escalate writes to the RSA dispatch table", env.get("RSA_DISPATCH_TABLE") == table,
              f"RSA_DISPATCH_TABLE={env.get('RSA_DISPATCH_TABLE')}")

    def alarms() -> None:
        want = [f"vsa-{s}-rsa-dispatch-dlq-depth", f"vsa-{s}-rsa-lifecycle-advancer-errors",
                f"vsa-{s}-rsa-lifecycle-advancer-not-running", f"vsa-{s}-rsa-reconciliation-detected"]
        got = {x["AlarmName"]: x for x in cw.describe_alarms(AlarmNames=want)["MetricAlarms"]}
        topic = f"arn:aws:sns:{r}:{account}:vsa-{s}-rsa-operator-alerts"
        for n in want:
            x = got.get(n)
            # Pin the action to the RSA topic, not just "some action": an alarm wired to
            # another topic would page the wrong people and still pass a non-empty check.
            check(f"alarm {n} on the RSA topic",
                  bool(x) and x["ActionsEnabled"] and topic in x["AlarmActions"],
                  f"state={x['StateValue']} actions={x['AlarmActions']}" if x else "missing")
        n_subs = len(sns.list_subscriptions_by_topic(TopicArn=topic)["Subscriptions"])
        print(f"[INFO] operator topic subscriptions: {n_subs}"
              + (" (nobody is paged; CVX_RSA_ALARM_EMAIL=none)" if n_subs == 0 else ""))

    def advancer_running() -> None:
        group = f"/aws/lambda/vsa-{s}-rsa-lifecycle-advancer"
        start = int((time.time() - 300) * 1000)
        # Page to exhaustion: filter_log_events can return an empty page with a
        # nextToken, so reading one page gives a false FAIL (no clean runs seen) or a
        # false PASS (ERROR lines on a later page).
        pager = logs.get_paginator("filter_log_events")

        def events(pattern: str) -> list:
            return [e for page in pager.paginate(logGroupName=group, startTime=start,
                                                 filterPattern=pattern)
                    for e in page.get("events", [])]

        ok_lines = events('"lifecycle run complete"')
        clean = [e for e in ok_lines if "errors=0" in e["message"]]
        errs = events("?ERROR ?Traceback")
        check("advancer ran in the last 5 minutes with errors=0", bool(clean) and not errs,
              f"{len(clean)} clean runs, {len(errs)} error lines")

    def route_401() -> None:
        outs = cfn.describe_stacks(StackName=f"vsa-{s}-api")["Stacks"][0].get("Outputs", [])
        base = next((o["OutputValue"] for o in outs if o["OutputKey"] == "RestApiUrl"), None)
        if not base:
            check("RestApiUrl output resolves", False)
            return
        url = base.rstrip("/") + "/rsa/dispatch/rsa-00000000-0000-4000-8000-000000000000"
        try:
            code = urllib.request.urlopen(urllib.request.Request(url), timeout=10).status
        except urllib.error.HTTPError as e:
            code = e.code
        check("GET /rsa/dispatch/{id} without a token returns 401", code == 401, f"HTTP {code}")

    print(f"RSA dispatch smoke test ({s}, {r})")
    for name, fn in (("stacks", stacks), ("lambdas", lambdas), ("stream + escalate wiring", stream_and_escalate),
                     ("alarms", alarms), ("advancer running", advancer_running), ("route auth", route_401)):
        guarded(name, fn)
    print(f"\n{'FAILED: ' + ', '.join(failures) if failures else 'All checks passed.'}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
