#!/usr/bin/env python3
"""Post-deploy smoke test for the Tier 2 platform health agent.

Spec: `.kiro/specs/2026-08-03-cvx-tier2-platform-health-agent/spec.md`
Task: T6.1

Per `~/.kiro/steering/deploy-validation.md`, a deploy is not successful until
something has exercised it. This script invokes the deployed Lambda once per
component, then asserts the artifacts it produced satisfy the M4 contract.

    python3 scripts/smoke-test-tier2.py --stage staging
    python3 scripts/smoke-test-tier2.py --stage staging --component brakes
    python3 scripts/smoke-test-tier2.py --stage staging --no-invoke   # assert only

Exit codes
----------
    0  every checked component invoked cleanly and its artifacts are well-formed
    1  at least one check failed (details on stdout)

What this asserts, and why each one is here
-------------------------------------------
Each check corresponds to a way this deploy has actually been observed to fail,
or to a contract that nothing else enforces at runtime:

1. ``FunctionError`` absent and ``status == "completed"`` — a Lambda that
   returns 200 with an in-band error payload is not a working Lambda.
2. ``rows_read > 0`` — the read path is the part that crosses accounts, IAM,
   Lake Formation, KMS and Glue. Zero rows means one of those five silently
   returned nothing, which is indistinguishable from "healthy fleet" unless
   asserted separately.
3. Every M4 field present on a sampled artifact — ``computed_at``,
   ``confidence``, ``evidence``, ``agent_version``, ``inputs_hash``.
4. ``evidence`` non-empty with ``provenance`` on each entry. Evidence is what
   makes the Tier 1 narration honest; an artifact asserting a conclusion it
   cannot support is worse than no artifact.
5. ``inputs_hash`` is the ``sk`` suffix — the content-addressing that makes
   re-runs idempotent (M6). If these ever diverge, duplicate rows follow.
6. No VIN in the Lambda's own log output. Tier 2 runs unattended and CloudWatch
   is its widest exposure surface.

VIN handling
------------
This script reads artifacts whose partition key contains a VIN. It prints
``VEH#<redacted>`` and never the VIN itself, matching the platform's
test-enforced "no VINs in logs" contract.

Non-interactive per `~/.kiro/steering/non-interactive.md`. The only writes are
those the Lambda itself performs; this script writes nothing directly.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from typing import Any

import boto3

#: Components the Tier 2 agent covers. Kept in sync with
#: `agents/tier2/contract.py::VALID_COMPONENTS` — imported when the agents
#: package is importable, and falling back to a literal so the script also runs
#: from an operator shell that has not set PYTHONPATH.
try:  # pragma: no cover - import path depends on caller's cwd
    from agents.tier2.contract import VALID_COMPONENTS

    COMPONENTS = sorted(VALID_COMPONENTS)
except ImportError:  # pragma: no cover
    COMPONENTS = ["battery", "brakes", "tires"]

#: Region the Tier 2 Lambda and artifact table are deployed to, per stage.
STAGE_REGIONS = {"staging": "us-west-2", "prod": "us-east-1"}

#: Fields the M4 artifact contract requires on every artifact (spec § D4).
REQUIRED_M4_FIELDS = (
    "pk",
    "sk",
    "computed_at",
    "confidence",
    "evidence",
    "agent_version",
    "inputs_hash",
    "finding",
    "severity_hint",
    "ttl",
)

#: A 17-character VIN as ADP emits them. Used only to assert absence.
_VIN_SHAPE = re.compile(r"\b[A-HJ-NPR-Z0-9]{17}\b")

_PASS = "  PASS"
_FAIL = "  FAIL"


def _deserialise(value: dict) -> Any:
    """Convert one DynamoDB AttributeValue into a plain Python value."""
    kind, raw = next(iter(value.items()))
    if kind == "S":
        return raw
    if kind == "N":
        return raw
    if kind == "BOOL":
        return raw
    if kind == "NULL":
        return None
    if kind == "L":
        return [_deserialise(v) for v in raw]
    if kind == "M":
        return {k: _deserialise(v) for k, v in raw.items()}
    return raw


def invoke(lambda_client: Any, function_name: str, stage: str, component: str) -> tuple[bool, dict]:
    """Invoke the Tier 2 Lambda for *component*; return (ok, run summary)."""
    payload = json.dumps({"stage": stage, "component": component}).encode()
    resp = lambda_client.invoke(FunctionName=function_name, Payload=payload)

    if resp.get("FunctionError"):
        print(f"{_FAIL} Lambda returned FunctionError={resp['FunctionError']!r}")
        print(f"       payload: {resp['Payload'].read().decode()[:600]}")
        return False, {}

    try:
        summary = json.loads(resp["Payload"].read().decode())
    except (ValueError, KeyError) as exc:
        print(f"{_FAIL} could not parse the run summary: {exc}")
        return False, {}

    status = summary.get("status")
    rows_read = summary.get("rows_read", 0)
    written = summary.get("artifacts_written", 0)
    duplicates = summary.get("duplicates_skipped", 0)
    abort = summary.get("abort_reason")

    print(
        f"       status={status} rows_read={rows_read} "
        f"artifacts_written={written} duplicates_skipped={duplicates} "
        f"tokens={summary.get('tokens_used')} abort_reason={abort!r}"
    )

    ok = True
    if status != "completed":
        # An aborted run is a legitimate outcome of the budget cap, but it is
        # not a healthy deploy, so the smoke test fails on it deliberately.
        print(f"{_FAIL} run status is {status!r}, expected 'completed'")
        ok = False
    if rows_read == 0:
        print(
            f"{_FAIL} rows_read=0 — the cross-account read returned nothing. "
            "Check Lake Formation grants, the Glue database, and kms:Decrypt."
        )
        ok = False
    # The assertion is on artifacts PRODUCED OR CONFIRMED, not on new writes.
    # A correct second run over unchanged ADP data writes nothing and reports
    # artifacts_written=0 with duplicates_skipped=N — that is idempotency (M6)
    # working, not a failure. An earlier version of this check asserted
    # `written == 0` was a failure and duly failed the smoke test the second
    # time it ran, on a deploy that was behaving exactly as designed.
    if written + duplicates == 0:
        print(
            f"{_FAIL} artifacts_written=0 and duplicates_skipped=0 — the run "
            "neither wrote nor confirmed a single artifact, so nothing reached "
            "DynamoDB."
        )
        ok = False
    return ok, summary


def check_artifacts(ddb: Any, table: str, component: str) -> bool:
    """Assert the M4 contract against one artifact for *component*."""
    resp = ddb.scan(
        TableName=table,
        FilterExpression="begins_with(sk, :p)",
        ExpressionAttributeValues={":p": {"S": f"FINDING#{component}#"}},
        Limit=200,
    )
    items = resp.get("Items", [])
    if not items:
        print(f"{_FAIL} no artifact found in {table} for component={component}")
        return False

    print(f"       {len(items)} artifact(s) scanned; asserting the M4 contract on one")
    art = {k: _deserialise(v) for k, v in items[0].items()}
    ok = True

    missing = [f for f in REQUIRED_M4_FIELDS if f not in art]
    if missing:
        print(f"{_FAIL} artifact is missing required M4 field(s): {missing}")
        ok = False

    evidence = art.get("evidence") or []
    if not evidence:
        print(
            f"{_FAIL} evidence[] is empty. Evidence is what lets the Tier 1 agent "
            "narrate a conclusion it can support; without it the artifact is an "
            "unsourced assertion."
        )
        ok = False
    else:
        for i, entry in enumerate(evidence[:5]):
            for field in ("source", "value", "provenance"):
                if field not in entry:
                    print(f"{_FAIL} evidence[{i}] is missing {field!r}")
                    ok = False

    sk = art.get("sk", "")
    inputs_hash = art.get("inputs_hash", "")
    if not sk.endswith(inputs_hash) or not inputs_hash:
        print(
            f"{_FAIL} sk does not end with inputs_hash — content-addressing is broken, "
            "so re-runs will write duplicates instead of no-ops (M6). "
            f"sk={sk!r} inputs_hash={inputs_hash!r}"
        )
        ok = False

    if not art.get("computed_at"):
        print(f"{_FAIL} computed_at is empty — Tier 1 cannot surface staleness")
        ok = False

    if ok:
        print(
            f"{_PASS} pk=VEH#<redacted> sk=FINDING#{component}#{inputs_hash[:12]}… "
            f"computed_at={art['computed_at']} confidence={art['confidence']} "
            f"severity_hint={art['severity_hint']} evidence_n={len(evidence)}"
        )
    return ok


def check_no_vins_in_logs(logs_client: Any, log_group: str, since_ms: int) -> bool:
    """Assert the Lambda emitted no VIN-shaped token during this run."""
    try:
        resp = logs_client.filter_log_events(
            logGroupName=log_group, startTime=since_ms, limit=1000
        )
    except logs_client.exceptions.ResourceNotFoundException:
        print(f"{_FAIL} log group {log_group} does not exist")
        return False

    offenders = 0
    for event in resp.get("events", []):
        if _VIN_SHAPE.search(event.get("message", "")):
            offenders += 1

    if offenders:
        # Deliberately does NOT echo the matched text — printing it here would
        # commit the very leak being reported.
        print(
            f"{_FAIL} {offenders} log event(s) contain a VIN-shaped token. "
            "Tier 2 runs unattended; CloudWatch is its widest exposure surface."
        )
        return False
    print(f"{_PASS} no VIN-shaped token in {len(resp.get('events', []))} log event(s)")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", default="staging", choices=sorted(STAGE_REGIONS))
    parser.add_argument("--component", default="all", choices=["all", *COMPONENTS])
    parser.add_argument(
        "--no-invoke",
        action="store_true",
        help="Skip invocation; assert only against artifacts already in the table.",
    )
    args = parser.parse_args()

    region = STAGE_REGIONS[args.stage]
    function_name = f"vsa-{args.stage}-tier2-health-agent-{region}"
    table = f"vsa-{args.stage}-health-findings"
    log_group = f"/aws/lambda/{function_name}"
    components = COMPONENTS if args.component == "all" else [args.component]

    print(f"stage={args.stage}  region={region}")
    print(f"function={function_name}")
    print(f"table={table}\n")

    lambda_client = boto3.client("lambda", region_name=region)
    ddb = boto3.client("dynamodb", region_name=region)
    logs_client = boto3.client("logs", region_name=region)

    started_ms = int(time.time() * 1000) - 5_000
    results: dict[str, bool] = {}

    for component in components:
        print(f"[{component}]")
        ok = True
        if not args.no_invoke:
            ok, _ = invoke(lambda_client, function_name, args.stage, component)
        # Artifacts are asserted even when the invoke failed, because the
        # failure mode is more informative with the table state alongside it.
        ok = check_artifacts(ddb, table, component) and ok
        results[component] = ok
        print()

    if not args.no_invoke:
        print("[logs]")
        # Log delivery to CloudWatch lags the invocation by a few seconds, so a
        # one-shot wait before reading is required — there is no event to await.
        # Keep `nosemgrep` as the last comment line before the target.
        # nosemgrep: arbitrary-sleep
        time.sleep(10)
        results["logs"] = check_no_vins_in_logs(logs_client, log_group, started_ms)
        print()

    print("=== summary ===")
    for name, ok in sorted(results.items()):
        print(f"  {name:<9} {'PASS' if ok else 'FAIL'}")

    failed = [n for n, ok in results.items() if not ok]
    if failed:
        print(f"\nSMOKE TEST FAILED: {', '.join(sorted(failed))}")
        return 1
    print("\nSMOKE TEST PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
