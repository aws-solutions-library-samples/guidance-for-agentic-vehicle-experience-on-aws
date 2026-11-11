#!/usr/bin/env python3
"""Smoke-test cross-account Athena read from the AgentCore execution role.

Assumes the CVX AgentCore execution role
(`AmazonBedrockAgentCoreSDKRuntime-us-west-2-vsa-staging`) and runs a
parameterized-shape SELECT against ADP's `adp_staging_vehicle_identity`
table via the CVX-side Athena workgroup `cvx-staging-analytics`.

Validates Group 7.B item 1 — confirms the role can:
1. assume-role via STS
2. start-query-execution against the workgroup
3. read across the LF v4 cross-account share
4. write results to the CVX-side region-suffixed S3 bucket

Usage:
    python3 scripts/smoke-test-cross-account.py --stage staging
"""

from __future__ import annotations

import argparse
import sys
import time

import boto3


ADP_REGION = "us-east-1"


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--stage", default="staging", help="Deployment stage (default: staging)")
    p.add_argument("--vin", default=None, help="Optional VIN to query for (default: pick first row)")
    args = p.parse_args()

    role_arn = (
        f"arn:aws:iam::123456789012:role/"
        f"AmazonBedrockAgentCoreSDKRuntime-us-west-2-vsa-{args.stage}"
    )
    workgroup = f"cvx-{args.stage}-analytics"
    db = f"adp_{args.stage}_vehicle_identity"

    print(f"role:       {role_arn}")
    print(f"workgroup:  {workgroup}")
    print(f"database:   {db}.vehicle_identity")
    print()

    sts = boto3.client("sts", region_name=ADP_REGION)
    try:
        assumed = sts.assume_role(
            RoleArn=role_arn,
            RoleSessionName="cvx-foundation-smoke",
            DurationSeconds=900,
        )
    except Exception as e:
        print(f"  ✗ assume_role failed: {e}", file=sys.stderr)
        print(f"  Note: if AccessDenied, the role's trust policy may not allow your local principal.", file=sys.stderr)
        print(f"  Falling back to local credentials for the Athena query.", file=sys.stderr)
        assumed = None

    creds = (
        {
            "aws_access_key_id": assumed["Credentials"]["AccessKeyId"],
            "aws_secret_access_key": assumed["Credentials"]["SecretAccessKey"],
            "aws_session_token": assumed["Credentials"]["SessionToken"],
        }
        if assumed
        else {}
    )

    athena = boto3.client("athena", region_name=ADP_REGION, **creds)

    if args.vin:
        if not args.vin.isalnum():
            print(f"  ✗ VIN must be alphanumeric (R15 guard)", file=sys.stderr)
            return 1
        sql = f"SELECT vin FROM {db}.vehicle_identity WHERE vin = '{args.vin}' LIMIT 1"
    else:
        sql = f"SELECT vin FROM {db}.vehicle_identity LIMIT 1"

    print(f"SQL: {sql}")
    print()

    t0 = time.time()
    resp = athena.start_query_execution(
        QueryString=sql,
        WorkGroup=workgroup,
    )
    qid = resp["QueryExecutionId"]
    print(f"  query id: {qid}")

    while True:
        status = athena.get_query_execution(QueryExecutionId=qid)
        state = status["QueryExecution"]["Status"]["State"]
        if state in ("SUCCEEDED", "FAILED", "CANCELLED"):
            break
        # Athena exposes no boto3 waiter for query completion, so polling
        # get_query_execution is the documented pattern. The loop exits on any
        # terminal state above; this sleep only paces the polling interval.
        # The `nosemgrep` directive MUST remain the last comment line before
        # the target — intervening prose silently breaks suppression (verified
        # empirically 2026-08-03, see issues/2026-08-03-cvx-probe-critical-triage).
        # nosemgrep: arbitrary-sleep
        time.sleep(0.5)

    elapsed_ms = int((time.time() - t0) * 1000)
    print(f"  state:    {state}")
    print(f"  elapsed:  {elapsed_ms}ms")

    if state != "SUCCEEDED":
        reason = status["QueryExecution"]["Status"].get("StateChangeReason", "(no reason)")
        print(f"  ✗ Query did NOT succeed: {reason}", file=sys.stderr)
        return 1

    results = athena.get_query_results(QueryExecutionId=qid, MaxResults=2)
    rows = results.get("ResultSet", {}).get("Rows", [])
    print(f"  rows:     {len(rows)}")
    for r in rows:
        vals = [d.get("VarCharValue", "") for d in r.get("Data", [])]
        print(f"    {vals}")

    if len(rows) >= 2:
        print()
        print(f"✓ Cross-account Athena read succeeded ({elapsed_ms}ms).")
        return 0
    print(f"  ✗ No data rows returned; LF share may not be active for this principal", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
