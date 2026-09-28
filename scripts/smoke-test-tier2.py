#!/usr/bin/env python3
"""Post-deploy smoke test for the Tier 2 platform health agent.

Specs:
  `.kiro/specs/2026-08-03-cvx-tier2-platform-health-agent/spec.md` (original)
  `.kiro/specs/2026-08-10-cvx-tier2-vin-selection-and-alerting/spec.md` (this spec)

Per `~/.kiro/steering/deploy-validation.md`, a deploy is not successful until
something has exercised it. This script invokes the deployed Lambda once per
component, then asserts the artifacts it produced satisfy the M4 contract.

    python3 scripts/smoke-test-tier2.py --stage staging
    python3 scripts/smoke-test-tier2.py --stage staging --component brakes
    python3 scripts/smoke-test-tier2.py --stage staging --no-invoke   # assert only
    python3 scripts/smoke-test-tier2.py --stage staging --no-infra-checks  # skip new checks

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

New checks added by spec `2026-08-10-cvx-tier2-vin-selection-and-alerting`:

7. All eight alarms have non-empty ``AlarmActions`` (spec § D6): two stage-level
   alarms (run-failure, run-duration) plus six per-component alarms (no-vins and
   artifacts-stale for each of the three components). Before Group 4 deploys this
   MUST fail — empty AlarmActions is the exact defect the predecessor spec shipped
   and this spec exists to fix.  Alarm names are derived from ``VALID_COMPONENTS``
   so a future component addition is automatically covered.
8. The operator SNS topic ``vsa-{stage}-tier2-operator-alerts`` exists (spec § D6).
9. The three CVX/Tier2 custom metrics have datapoints from the last 25 h
   (spec § D5). Before Group 4 deploys, ``PutMetricData`` is never called.
10. The cursor item exists and ``last_vin`` advances between two consecutive
    invocations (spec § D3). Before Group 4 deploys, no cursor item exists.

The deliberate design: checks 7-10 MUST exit non-zero against the current
deployment.  Capturing that failing exit before the deploy is how we prove the
test is real.  A smoke test that cannot fail is not a smoke test — the
predecessor spec shipped four defects past 772 passing tests for exactly this
reason (tests stubbed the Athena client; a stub cannot fail the way a service
fails).

VIN handling
------------
This script reads artifacts whose partition key contains a VIN. It prints
``VEH#<redacted>`` and never the VIN itself, matching the platform's
test-enforced "no VINs in logs" contract.  ``check_cursor_advancement`` logs
``before=<redacted> after=<redacted>`` for the same reason.

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


def check_alarm_actions(cw: Any, stage: str, region: str) -> bool:
    """Assert all eight Tier 2 alarms have non-empty AlarmActions.

    This is the check that would have caught the predecessor spec's defect:
    alarms with empty AlarmActions notify nobody, making a silent no-op run
    indistinguishable from a healthy one.  All eight alarms must be wired to
    the operator topic before this passes.

    Expected alarms (spec § D5/D6):

      Stage-level (2):
        vsa-{stage}-tier2-run-failure-{region}
        vsa-{stage}-tier2-run-duration-{region}

      Per-component (6, one per component per alarm type, derived from
      VALID_COMPONENTS so a component change cannot leave this stale):
        vsa-{stage}-tier2-no-vins-selected-{component}-{region}
        vsa-{stage}-tier2-artifacts-stale-{component}-{region}

    Fix Group 4a replaced the two stage-only per-component alarms with six
    component-specific ones (one per VALID_COMPONENT × two alarm types).
    The alarm names are derived here from the same COMPONENTS constant used
    everywhere else, so a future component addition is caught automatically.
    """
    # Two stage-level alarms that predate Fix Group 4a — names are stable.
    stage_level = [
        f"vsa-{stage}-tier2-run-failure-{region}",
        f"vsa-{stage}-tier2-run-duration-{region}",
    ]
    # Six per-component alarms introduced by Fix Group 4a.  Derived from
    # COMPONENTS (which is sourced from contract.VALID_COMPONENTS) so neither
    # this list nor the stack can drift without the other failing first.
    per_component = [
        f"vsa-{stage}-tier2-{alarm_type}-{component}-{region}"
        for alarm_type in ("no-vins-selected", "artifacts-stale")
        for component in COMPONENTS
    ]
    expected = stage_level + per_component

    resp = cw.describe_alarms(AlarmNames=expected)
    alarms = {a["AlarmName"]: a for a in resp.get("MetricAlarms", [])}

    ok = True
    for name in expected:
        if name not in alarms:
            print(f"{_FAIL} alarm not found: {name}")
            ok = False
            continue
        actions = alarms[name].get("AlarmActions", [])
        if not actions:
            print(
                f"{_FAIL} {name} — AlarmActions is empty. "
                "This alarm notifies nobody when it fires."
            )
            ok = False
        else:
            print(f"{_PASS} {name} — AlarmActions={actions}")
    return ok


def check_operator_topic(sns: Any, stage: str, region: str) -> bool:
    """Assert the operator SNS topic exists (spec § D6).

    The topic name is vsa-{stage}-tier2-operator-alerts.  Its existence is a
    prerequisite for alarm actions — an AlarmActions ARN pointing at a
    nonexistent topic is silently ignored by CloudWatch.
    """
    account_id: str
    try:
        sts = boto3.client("sts", region_name=region)
        account_id = sts.get_caller_identity()["Account"]
    except Exception as exc:  # noqa: BLE001
        print(f"{_FAIL} could not resolve account ID to build topic ARN: {exc}")
        return False

    topic_name = f"vsa-{stage}-tier2-operator-alerts"
    topic_arn = f"arn:aws:sns:{region}:{account_id}:{topic_name}"

    try:
        sns.get_topic_attributes(TopicArn=topic_arn)
    except sns.exceptions.NotFoundException:
        print(
            f"{_FAIL} operator topic not found: {topic_arn}. "
            "Deploy the stack changes (Group 4) before this can pass."
        )
        return False
    except Exception as exc:  # noqa: BLE001
        print(f"{_FAIL} error checking topic {topic_arn}: {exc}")
        return False

    print(f"{_PASS} operator topic exists: {topic_arn}")
    return True


def _get_emitted_dimension_names() -> list[str]:
    """Return the dimension *names* that ``observability.emit_metrics`` actually publishes.

    Derived by calling ``emit_metrics`` with a capturing stub and reading the
    dimension-name list from the first datum, following the same approach as
    ``test_tier2_alarm_wiring_guard.py::TestAlarmDimensionInvariant._get_emitted_dimension_names``.

    This means a change to the emission code (add/rename/remove a dimension) is
    automatically reflected here without requiring a change to this file, so the
    smoke test cannot silently drift behind the metric definition.

    Returns a *sorted* list so the order is deterministic across Python versions.
    Falls back to ``["component", "stage"]`` if the agents package is not importable,
    which preserves operator-shell usability at the cost of import-path sensitivity.
    """
    try:
        from agents.tier2.observability import emit_metrics  # noqa: PLC0415

        class _CapturingClient:
            def __init__(self) -> None:
                self.calls: list[dict] = []

            def put_metric_data(self, **kwargs: Any) -> None:
                self.calls.append(kwargs)

        stub = _CapturingClient()
        emit_metrics(
            stub,
            "probe-stage",
            "probe-component",
            vins_selected=0,
            artifacts_written=0,
            duplicates_skipped=0,
        )
        if stub.calls:
            first_datum = stub.calls[0]["MetricData"][0]
            return sorted(d["Name"] for d in first_datum["Dimensions"])
    except Exception:  # noqa: BLE001
        pass
    # Fallback: match the known published shape so operator shells stay usable.
    return ["component", "stage"]


def check_metrics_datapoints(cw: Any, stage: str, region: str) -> bool:
    """Assert the three CVX/Tier2 custom metrics have datapoints from a recent run.

    Metrics: VinsSelected, ArtifactsWritten, DuplicatesSkipped (spec § D5).
    'Recent' is defined as the last 25 hours — enough to cover one nightly run
    plus clock skew, without going so wide that a stale datapoint looks current.

    A metric with no datapoints at all means PutMetricData was never called,
    which means either (a) the observability module is not wired, or (b) the
    Lambda never ran successfully since the metric was introduced.

    Dimension set: derived from ``observability.emit_metrics`` at call time via
    ``_get_emitted_dimension_names()``, so this check stays aligned with the
    actual emission code across future changes (Fix Group 4b — CloudWatch treats
    dimension sets as part of a metric's identity; a ``{stage}``-only query
    against ``{stage, component}`` data returns zero datapoints always).

    Queries one datapoint per component per metric (i.e. 9 calls total for three
    components × three metrics), aggregated over the 25-hour window.  At least
    one datapoint across the window is enough to prove the emission path is wired.
    """
    metric_names = ["VinsSelected", "ArtifactsWritten", "DuplicatesSkipped"]
    # Derive dimension names from the actual emission code — not re-typed literals.
    # If observability.py renames or adds a dimension, this adapts automatically.
    emitted_dim_names = _get_emitted_dimension_names()  # e.g. ["component", "stage"]

    end = time.time()
    start = end - 25 * 3600  # 25-hour window

    ok = True
    for component in COMPONENTS:
        # Build the dimension list in the order the emitter uses, with concrete values.
        dimensions = [
            {"Name": name, "Value": (stage if name == "stage" else component)}
            for name in emitted_dim_names
        ]
        for metric_name in metric_names:
            resp = cw.get_metric_statistics(
                Namespace="CVX/Tier2",
                MetricName=metric_name,
                Dimensions=dimensions,
                StartTime=start,
                EndTime=end,
                Period=86400,
                Statistics=["Sum"],
            )
            datapoints = resp.get("Datapoints", [])
            if not datapoints:
                dim_summary = ", ".join(f"{d['Name']}={d['Value']!r}" for d in dimensions)
                print(
                    f"{_FAIL} CVX/Tier2/{metric_name} [{dim_summary}] — "
                    "no datapoints in the last 25h. "
                    "Either PutMetricData is not wired or the Lambda has not run since "
                    "observability was deployed."
                )
                ok = False
            else:
                total = sum(dp.get("Sum", 0) for dp in datapoints)
                print(
                    f"{_PASS} CVX/Tier2/{metric_name} "
                    f"[component={component!r}] — "
                    f"{len(datapoints)} datapoint(s), sum={total:.0f}"
                )
    return ok


def check_cursor_advancement(ddb: Any, lambda_client: Any, table: str, function_name: str, stage: str) -> bool:
    """Assert the cursor item exists and last_vin advances between two invocations.

    This is the core liveness check for spec § D3: the cursor must persist and
    advance, which is the only observable proof that the frozen-window defect
    has been fixed.  The check:

    1. Reads the current cursor state for all three components.
    2. Invokes the Lambda once for each component.
    3. Reads the cursor state again.
    4. Asserts that at least one component's last_vin changed.

    A smoke test that cannot fail is not a smoke test (predecessor lesson).
    Before Group 6 deploys: the cursor items do not exist, so step 1 returns
    nothing and the check fails.  After deploy: step 4 must show advancement.
    """
    def read_cursor(component: str) -> str | None:
        resp = ddb.get_item(
            TableName=table,
            Key={
                "pk": {"S": f"CURSOR#{component}"},
                "sk": {"S": "STATE"},
            },
        )
        item = resp.get("Item")
        if not item:
            return None
        return item.get("last_vin", {}).get("S")

    ok = True
    before: dict[str, str | None] = {}
    for component in COMPONENTS:
        before[component] = read_cursor(component)

    any_cursor_present = any(v is not None for v in before.values())
    if not any_cursor_present:
        print(
            f"{_FAIL} no cursor item found for any component in {table}. "
            "The cursor is written by selection.py on the first run after "
            "Group 4 deploys.  Deploy first, then invoke the Lambda to seed it."
        )
        return False

    # Invoke each component once so the cursor can advance.
    print("       invoking Lambda for each component to advance cursors…")
    for component in COMPONENTS:
        payload = json.dumps({"stage": stage, "component": component}).encode()
        resp = lambda_client.invoke(FunctionName=function_name, Payload=payload)
        err = resp.get("FunctionError")
        if err:
            print(f"       WARNING: {component} invocation returned FunctionError={err!r}")

    after: dict[str, str | None] = {}
    for component in COMPONENTS:
        after[component] = read_cursor(component)

    advanced = []
    for component in COMPONENTS:
        b, a = before[component], after[component]
        if b is None and a is None:
            print(f"       {component}: cursor absent before and after — not yet seeded")
        elif b == a:
            print(f"       {component}: last_vin unchanged (pool may be exhausted or batch is 0)")
        else:
            print(f"       {component}: last_vin advanced (before=<redacted> after=<redacted>)")
            advanced.append(component)

    if not advanced:
        print(
            f"{_FAIL} last_vin did not advance for any component. "
            "Either selection is not wired, the pool is empty, or the cursor "
            "write is failing silently (check dynamodb:GetItem grant — spec § D3)."
        )
        ok = False
    else:
        print(f"{_PASS} cursor advanced for: {', '.join(advanced)}")
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
    parser.add_argument(
        "--no-infra-checks",
        action="store_true",
        help=(
            "Skip alarm-wiring, operator-topic, metrics, and cursor checks. "
            "Use only when the stack changes from this spec are not yet deployed."
        ),
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
    cw = boto3.client("cloudwatch", region_name=region)
    sns = boto3.client("sns", region_name=region)

    started_ms = int(time.time() * 1000) - 5_000
    results: dict[str, bool] = {}

    # ── Infrastructure checks (spec §D5/D6, Group 4) ─────────────────────────
    # These checks assert properties of the deployed stack itself, not of a
    # specific Lambda run.  They MUST fail against the pre-Group-4 deployment
    # (alarms have empty AlarmActions, no topic exists, no CVX/Tier2 metrics).
    # That deliberate failure is the evidence the task spec requires — a smoke
    # test that cannot fail is not a smoke test.
    if not args.no_infra_checks:
        print("[alarm-actions]")
        results["alarm-actions"] = check_alarm_actions(cw, args.stage, region)
        print()

        print("[operator-topic]")
        results["operator-topic"] = check_operator_topic(sns, args.stage, region)
        print()

        print("[metrics-datapoints]")
        results["metrics-datapoints"] = check_metrics_datapoints(cw, args.stage, region)
        print()

        print("[cursor-advancement]")
        results["cursor-advancement"] = check_cursor_advancement(
            ddb, lambda_client, table, function_name, args.stage
        )
        print()

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
        print(f"  {name:<20} {'PASS' if ok else 'FAIL'}")

    failed = [n for n, ok in results.items() if not ok]
    if failed:
        print(f"\nSMOKE TEST FAILED: {', '.join(sorted(failed))}")
        return 1
    print("\nSMOKE TEST PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
