#!/usr/bin/env python3
"""Live-verification of the Agent 2 → AVX dual-write against DEPLOYED staging.

This is the gate Fix Group 16 T16.4 defines, and the group's standing constraint
says no task in it closes on a stubbed-client test alone: **every** test in Fix
Groups 10-14 stubbed the DynamoDB client, which is the exact condition that let
seven read-surface defects pass both review gates. A stub cannot fail the way a
service fails — it accepts any `IndexName`, any `UpdateExpression`, and returns
whatever the fixture supplies. It also cannot produce an `AccessDeniedException`,
which is the failure the dual-write's IAM grant actually risks.

WHAT EACH PART PROVES — read this before trusting any single result
==================================================================

The three parts prove *different* properties and are NOT interchangeable. Stating
this explicitly because the failure mode being guarded against is a reader taking
Part C's green tick as evidence of a permission that Part C never exercises.

  Part A — the DEPLOYED wiring is correct and narrowly scoped.
           Reads the live Lambda config + role policy. Asserts the *scope* of the
           grant, not the existence of a statement: a presence-only assertion
           passes against `Resource: "*"`, and this repo has shipped that exact
           defect (see `~/.kiro/steering/testing.md` § "Mutation testing at the
           green boundary"). No writes.

  Part B — the real Lambda, under ITS OWN execution role, dual-wrote.
           This is the only part that proves the IAM grant works, because the
           writes are performed by the Lambda, never by this script. Attribution
           is established by correlating rows against a specific run's window
           taken from that run's own CloudWatch summary, then by a fresh invoke.

  Part C — the D2 in-place-update algorithm is correct against the real table.
           Runs under THIS caller's credentials, which on an operator machine are
           typically administrative. Part C therefore proves NOTHING about IAM and
           must never be cited for it. Its subject is the put/noop/update
           transition and the anti-duplicate property, which is a data-shape
           property independent of who is authenticated.

USAGE
  LIVE_TESTS=1 AWS_REGION=us-west-2 VSA_STAGE=staging \
    python3 scripts/live-verify-avx-dual-write.py

  Optional:
    AVX_LIVE_SKIP_INVOKE=1   skip the Part B fresh invoke (costs ~$0.03 of
                             Bedrock tokens and ~15-60s). The historical-run
                             checks still run and still attribute to the Lambda.

EXIT  0 all checks passed · 1 a check failed · 2 refused to run
"""
from __future__ import annotations

import datetime
import json
import os
import pathlib
import re
import sys
import uuid
from decimal import Decimal
from typing import Any

REPO = pathlib.Path(__file__).resolve().parent.parent

# ─────────────────────────────────────────────────────────────────────────────
# Pre-flight. Fail closed, and require the targeting to be EXPLICIT.
# ─────────────────────────────────────────────────────────────────────────────
#
# T16.4's Constraints: "run with AWS_REGION=us-west-2 VSA_STAGE=staging
# explicitly: this machine exports us-east-1 and prod, and both silently
# retarget scripts here."
#
# So neither value may be defaulted. A default is what makes the retarget silent
# — and this is not hypothetical: during T16.3's verification a bare
# `npx cdk synth` defaulted VSA_STAGE to `prod` and failed closed against
# /cvx/prod/* parameters that no longer exist, because CVX prod was torn down.
# Defaulting here would not have failed closed; it would have pointed live writes
# at the wrong stage.

if os.environ.get("LIVE_TESTS") != "1":
    print("refusing to run: set LIVE_TESTS=1 (this invokes a real Lambda and "
          "writes to a real DynamoDB table)")
    sys.exit(2)

REGION = os.environ.get("AWS_REGION")
STAGE = os.environ.get("VSA_STAGE")

if REGION != "us-west-2":
    print(f"refusing to run: AWS_REGION must be explicitly 'us-west-2', got {REGION!r}.\n"
          "  This is not a default — see the pre-flight note in this file's docstring.")
    sys.exit(2)
if STAGE != "staging":
    print(f"refusing to run: VSA_STAGE must be explicitly 'staging', got {STAGE!r}.\n"
          "  This script writes to live tables; it will not infer the stage.")
    sys.exit(2)

AVX_TABLE = f"vsa-{STAGE}-avx-findings"
LEGACY_TABLE = f"vsa-{STAGE}-health-findings"
FUNCTION = f"vsa-{STAGE}-tier2-health-agent-{REGION}"
LOG_GROUP = f"/aws/lambda/{FUNCTION}"
AGENT_ID = "agent-2-vehicle-health"

# Import the REAL modules. Reimplementing their queries or their key derivation
# here would test this script rather than the code, which is precisely the
# substitution that produced the original defects.
sys.path.insert(0, str(REPO))

import boto3                                                       # noqa: E402
from botocore.config import Config as BotoConfig                   # noqa: E402

from agents.tier2 import runner as R                               # noqa: E402
from agents.tier2 import writer as W                               # noqa: E402
from agents.tier2.finding_transform import (                       # noqa: E402
    health_artifact_to_finding,
    _validate_finding,
)

ddb = boto3.client("dynamodb", region_name=REGION)
logs = boto3.client("logs", region_name=REGION)
iam = boto3.client("iam")
lam = boto3.client("lambda", region_name=REGION)
# The runner's wall-clock ceiling is 600s; boto3's default 60s read timeout would
# abort a legitimate invoke and report it as a dual-write failure.
lam_invoke = boto3.client(
    "lambda",
    region_name=REGION,
    config=BotoConfig(read_timeout=660, connect_timeout=15, retries={"max_attempts": 0}),
)

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    results.append((name, bool(ok), detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"\n        {detail}" if detail else ""))
    return bool(ok)


def _native(obj: Any) -> Any:
    """Decimal → int/float, recursively.

    `writer._from_ddb_item` round-trips through boto3's TypeDeserializer, which
    yields `Decimal` for every DynamoDB `N`. jsonschema rejects `Decimal` for
    `"type": "number"`, so validating a persisted row without this adapter fails
    on the serialisation format rather than on the Finding's content — a false
    red that would discredit a real check.
    """
    if isinstance(obj, Decimal):
        as_float = float(obj)
        return int(as_float) if as_float.is_integer() else as_float
    if isinstance(obj, dict):
        return {k: _native(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_native(v) for v in obj]
    return obj


_SCHEMA_PROPS: set[str] = set(
    json.loads((REPO / "agents/tier2/schemas/finding.schema.json").read_text())["properties"]
)


def validate_persisted_row(row: dict[str, Any], label: str) -> bool:
    """Validate a row read back from DynamoDB against the frozen Finding schema.

    The schema is `additionalProperties: false` and governs the ARTIFACT, not the
    storage shape: `writer._put_item` deliberately denormalises the active scope
    field to a top-level attribute so the five sparse scope GSIs are queryable
    (T10.2). Validating the raw row would therefore always fail on that attribute.

    Rather than projecting the row down to the schema's keys and silently
    discarding whatever else is present — which would hide an unexpected
    attribute, i.e. exactly the class of drift worth catching — this asserts the
    extra keys are EXACTLY the documented storage-only set, then validates the
    schema-governed subset.
    """
    native = _native(row)
    extra = set(native) - _SCHEMA_PROPS

    scope = native.get("scope") or {}
    expected_extra = {f for f in scope if scope.get(f) is not None}

    unexpected = extra - expected_extra
    if unexpected:
        return check(
            f"{label} — persisted row carries no undocumented top-level attributes",
            False,
            f"unexpected: {sorted(unexpected)} (expected storage-only: {sorted(expected_extra)})",
        )

    for field in expected_extra:
        if native.get(field) != scope.get(field):
            return check(
                f"{label} — denormalised scope attribute agrees with nested scope",
                False,
                f"top-level {field}={native.get(field)!r} != scope.{field}={scope.get(field)!r}",
            )

    try:
        _validate_finding({k: v for k, v in native.items() if k in _SCHEMA_PROPS})
    except Exception as exc:
        return check(f"{label} — schema-valid against the frozen contract", False, str(exc)[:300])
    return check(
        f"{label} — schema-valid against the frozen contract",
        True,
        f"storage-only attrs: {sorted(expected_extra)}",
    )


def query_by_finding_key(finding_key: str) -> list[dict[str, Any]]:
    """All rows for a finding_key via the real GSI, paged to exhaustion.

    Paging matters: the D2 anti-duplicate property is a COUNT, and a single-page
    read that silently stops at 1 MB would report `1` for a table that holds two.
    That is the same defect shape as the CVX Tier 2 Athena reader which hashed
    only its first page and so made its own idempotency claim untrue.
    """
    rows: list[dict[str, Any]] = []
    kwargs: dict[str, Any] = {
        "TableName": AVX_TABLE,
        "IndexName": "by_finding_key",
        "KeyConditionExpression": "finding_key = :fk",
        "ExpressionAttributeValues": {":fk": {"S": finding_key}},
    }
    while True:
        resp = ddb.query(**kwargs)
        rows.extend(W._from_ddb_item(i) for i in resp.get("Items", []))
        if not resp.get("LastEvaluatedKey"):
            return rows
        kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]


created_finding_ids: list[str] = []   # rows THIS script caused to be created

# ═════════════════════════════════════════════════════════════════════════════
# PART A — the deployed wiring. Config only; proves scope, not behaviour.
# ═════════════════════════════════════════════════════════════════════════════
print("\n── Part A — deployed wiring (no writes) ─────────────────────────────")

cfg = lam.get_function_configuration(FunctionName=FUNCTION)
env = cfg.get("Environment", {}).get("Variables", {})
role_arn = cfg["Role"]
role_name = role_arn.rsplit("/", 1)[-1]

check("A1 AVX_FINDINGS_TABLE threaded to the deployed Lambda",
      env.get("AVX_FINDINGS_TABLE") == AVX_TABLE,
      f"AVX_FINDINGS_TABLE={env.get('AVX_FINDINGS_TABLE')!r}")

# T16.2's Constraints freeze the legacy path: spec.md:512 requires it and CVX
# Tier 1's get_health_findings reads it. A dual-write that quietly replaced the
# legacy table would satisfy "an AVX row exists" while breaking Tier 1.
check("A2 HEALTH_FINDINGS_TABLE still threaded (legacy path unchanged)",
      env.get("HEALTH_FINDINGS_TABLE") == LEGACY_TABLE,
      f"HEALTH_FINDINGS_TABLE={env.get('HEALTH_FINDINGS_TABLE')!r}")

ddb_statements: list[dict[str, Any]] = []
for pol in iam.list_role_policies(RoleName=role_name).get("PolicyNames", []):
    doc = iam.get_role_policy(RoleName=role_name, PolicyName=pol)["PolicyDocument"]
    for st in doc.get("Statement", []):
        acts = st.get("Action", [])
        acts = [acts] if isinstance(acts, str) else acts
        if any(str(a).startswith("dynamodb:") for a in acts):
            ddb_statements.append(st)


def _resources(st: dict[str, Any]) -> list[str]:
    r = st.get("Resource", [])
    return [r] if isinstance(r, str) else list(r)


def _actions(st: dict[str, Any]) -> list[str]:
    a = st.get("Action", [])
    return [a] if isinstance(a, str) else list(a)


avx_arn = f"arn:aws:dynamodb:{REGION}:{cfg['FunctionArn'].split(':')[4]}:table/{AVX_TABLE}"
key_index_arn = f"{avx_arn}/index/by_finding_key"

write_sts = [s for s in ddb_statements if avx_arn in _resources(s)]
query_sts = [s for s in ddb_statements if key_index_arn in _resources(s)]

write_acts = {a for s in write_sts for a in _actions(s)}
check("A3a deployed role grants PutItem + UpdateItem on the AVX table",
      {"dynamodb:PutItem", "dynamodb:UpdateItem"} <= write_acts,
      f"actions on table: {sorted(write_acts) or 'NONE — statement absent'}")

query_acts = {a for s in query_sts for a in _actions(s)}
check("A3b deployed role grants Query on index/by_finding_key",
      "dynamodb:Query" in query_acts,
      f"actions on by_finding_key: {sorted(query_acts) or 'NONE — statement absent'}")

# Scope, not presence. Each of these passes trivially if you only assert that a
# statement exists, which is the defect this repo has already shipped.
wildcard_res = [
    (s.get("Sid", "<no-sid>"), r)
    for s in ddb_statements for r in _resources(s)
    if r == "*" or r.endswith("/index/*") or r.endswith(":table/*")
]
check("A4a no DynamoDB statement widens Resource to a wildcard",
      not wildcard_res, f"offending: {wildcard_res}" if wildcard_res else "checked "
      f"{len(ddb_statements)} dynamodb statement(s)")

catch_all = [
    (s.get("Sid", "<no-sid>"), a)
    for s in ddb_statements for a in _actions(s)
    if a in ("dynamodb:*", "*")
]
check("A4b no DynamoDB statement uses a catch-all action",
      not catch_all, f"offending: {catch_all}" if catch_all else "no dynamodb:* / *")

# Deploy-timestamp rule: `~/.kiro/steering/deploy-validation.md` § "Committed,
# deployed, and live-verified are three different claims". A fix that is
# committed but not deployed has repeatedly been reported as done in this
# portfolio while the old code kept serving traffic.
import subprocess  # noqa: E402


def _newest_commit(*pathspec: str) -> str:
    return subprocess.run(
        ["git", "log", "-1", "--format=%cI", "--", *pathspec],
        cwd=REPO, capture_output=True, text=True,
    ).stdout.strip()


# Scoped to the modules that actually determine behaviour. The CDK asset's
# `exclude` list does NOT exclude test directories, so a test-only edit does
# change the asset hash and CDK will redeploy — but it cannot change what the
# Lambda does. Comparing against all of agents/tier2/ therefore reports a
# behaviourally-current bundle as stale: that false FAIL was observed on
# 2026-09-23 against commit 088c448, a one-line VIN rename in
# agents/tier2/tests/test_boundaries.py.
newest_src_commit = _newest_commit("agents/tier2/", ":(exclude)agents/tier2/tests/")
newest_any_commit = _newest_commit("agents/tier2/")
deployed_at = cfg["LastModified"]

# Positive control: an empty pathspec result is not evidence of "no changes", it
# is evidence the predicate did not run. Without this a typo'd pathspec would
# make A5 pass unconditionally — the vacuous-guard shape this repo has shipped.
if not newest_src_commit:
    check("A5 deployed bundle postdates the newest production-module commit", False,
          "git log returned nothing for the pathspec — the predicate did not run, "
          "so its result carries no information")
else:
    try:
        src_dt = datetime.datetime.fromisoformat(newest_src_commit)
        dep_dt = datetime.datetime.fromisoformat(deployed_at.replace("+0000", "+00:00"))
        note = f"deployed {dep_dt.isoformat()} vs newest production-module commit {src_dt.isoformat()}"
        if newest_any_commit and newest_any_commit != newest_src_commit:
            note += (f"\n        (test-only commits are newer at {newest_any_commit} — "
                     "these invalidate the asset hash but not behaviour)")
        check("A5 deployed bundle postdates the newest production-module commit",
              dep_dt >= src_dt, note)
    except Exception as exc:
        check("A5 deployed bundle postdates the newest production-module commit",
              False, repr(exc))

# ═════════════════════════════════════════════════════════════════════════════
# PART B — the real Lambda, under its own role, dual-wrote. The IAM proof.
# ═════════════════════════════════════════════════════════════════════════════
print("\n── Part B — real Lambda dual-write, attributed to its own role ──────")

# B1: the run's own summary. `avx_write_failures` is the distinct countable
# signal T16.2 required and T17.4 (W1) routed through RunObserver. Its ABSENCE
# from the summary means the build that produced that run predates the
# observability, so a zero could not be distinguished from a field never emitted.
now_ms = int(datetime.datetime.now(datetime.timezone.utc).timestamp() * 1000)

# Paged to exhaustion. `limit` caps events PER PAGE, and filter_log_events returns
# them oldest-first — so a single unpaged call with limit=40 returns the OLDEST
# matching events in the window, not the newest. That is not a hypothetical: the
# first run of this script found only the three 2026-09-22 (pre-deploy) summaries
# and reported the signal ABSENT, which would have read as a product defect.
# Same defect class as the CVX Tier 2 Athena reader that hashed only its first
# page and so made its own idempotency claim untrue.
events: list[dict[str, Any]] = []
page_kwargs: dict[str, Any] = {
    "logGroupName": LOG_GROUP,
    "startTime": now_ms - 48 * 3600 * 1000,
    "filterPattern": '"tier2_run"',
}
while True:
    page = logs.filter_log_events(**page_kwargs)
    events.extend(page.get("events", []))
    token = page.get("nextToken")
    if not token:
        break
    page_kwargs["nextToken"] = token

summaries: list[dict[str, Any]] = []
for ev in events:
    for m in re.finditer(r'\{"event": "tier2_run".*?\}', ev["message"]):
        try:
            summaries.append(json.loads(m.group(0)))
        except json.JSONDecodeError:
            pass

# Newest first. The assertion below is about the CURRENTLY deployed build's
# behaviour, so it must read the most recent run — older pre-deploy runs
# legitimately lack the signal and holding them to it would be a false failure.
summaries.sort(key=lambda s: str(s.get("started_at", "")), reverse=True)
productive = [s for s in summaries if s.get("artifacts_written", 0) > 0]

check("B1a a real scheduled run wrote artifacts in the last 48h",
      bool(productive),
      f"{len(summaries)} summary/ies found across {len(events)} log event(s), "
      f"{len(productive)} productive; newest={productive[0]['started_at'] if productive else 'n/a'}")

# Group the newest runs into one "latest nightly sweep" — the three components
# run 20 minutes apart, so all three belong to the same sweep and all three
# should carry the signal.
latest_sweep: list[dict[str, Any]] = []
if productive:
    newest_day = str(productive[0]["started_at"])[:10]
    latest_sweep = [s for s in productive if str(s.get("started_at", ""))[:10] == newest_day]

with_signal = [s for s in latest_sweep if "avx_write_failures" in s]
check("B1b the latest sweep's summaries carry the avx_write_failures signal",
      bool(with_signal) and len(with_signal) == len(latest_sweep),
      f"{len(with_signal)}/{len(latest_sweep)} summaries in the latest sweep carry it"
      if latest_sweep else "no productive run to inspect")

clean = [s for s in with_signal if s["avx_write_failures"] == 0]
check("B1c the AVX write path reported zero failures",
      bool(with_signal) and len(clean) == len(with_signal),
      "; ".join(f"{s['component']}: written={s['artifacts_written']} "
                f"avx_failures={s['avx_write_failures']}" for s in with_signal)
      or "no summary carried the signal")

# B2: attribute rows to that run. The window comes from the run's OWN summary,
# so a row inside it was written by the Lambda — this script has written nothing
# at this point. This is what makes the IAM claim non-vacuous: had the grant been
# missing, avx_write_failures would equal artifacts_written and this set would be
# empty.
attributed: list[dict[str, Any]] = []
run_windows = [(s["started_at"], s["finished_at"], s["component"]) for s in with_signal]

scan_kwargs: dict[str, Any] = {"TableName": AVX_TABLE}
all_rows: list[dict[str, Any]] = []
while True:
    resp = ddb.scan(**scan_kwargs)
    all_rows.extend(W._from_ddb_item(i) for i in resp.get("Items", []))
    if not resp.get("LastEvaluatedKey"):
        break
    scan_kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]

for row in all_rows:
    if row.get("agent_id") != AGENT_ID:
        continue
    ca = str(row.get("computed_at", ""))
    for started, finished, comp in run_windows:
        if started <= ca <= finished and str(row.get("finding_kind", "")).endswith(comp):
            attributed.append(row)
            break

check("B2 AVX rows exist inside a real run's own window (Lambda-attributed)",
      bool(attributed),
      f"{len(attributed)} row(s) attributable to {len(run_windows)} run window(s); "
      f"{len(all_rows)} row(s) in table total")

# B3: schema validity of what actually landed — not of what a fixture claims.
if attributed:
    validate_persisted_row(attributed[0], "B3 persisted AVX Finding")
else:
    check("B3 persisted AVX Finding — schema-valid against the frozen contract",
          False, "no attributed row to validate")

# B4: BOTH writes. T16.4's Accept requires the legacy artifact AND the AVX
# Finding. Checking only the AVX side would pass while the legacy path — which
# CVX Tier 1 reads — was broken.
if attributed:
    sample = attributed[0]
    sample_vin = (sample.get("scope") or {}).get("vin")
    legacy = ddb.query(
        TableName=LEGACY_TABLE,
        KeyConditionExpression="pk = :pk",
        ExpressionAttributeValues={":pk": {"S": f"VEH#{sample_vin}"}},
        Limit=5,
    )
    check("B4 BOTH writes landed — legacy artifact present for the same VIN",
          legacy.get("Count", 0) >= 1,
          f"vin={sample_vin} legacy rows={legacy.get('Count')} in {LEGACY_TABLE}")
else:
    check("B4 BOTH writes landed — legacy artifact present for the same VIN",
          False, "no attributed row to correlate")

# B5: a fresh invoke, so the claim is "works now", not only "worked overnight".
# Also re-asserts the anti-duplicate property against a real pre-existing row.
if os.environ.get("AVX_LIVE_SKIP_INVOKE") == "1":
    print("        (B5 skipped: AVX_LIVE_SKIP_INVOKE=1)")
elif attributed:
    target = attributed[0]
    vin = (target.get("scope") or {}).get("vin")
    component = str(target["finding_kind"]).split(".", 1)[1]
    fkey = target["finding_key"]
    before = query_by_finding_key(fkey)
    before_ids = {r["finding_id"] for r in before}

    print(f"        invoking {FUNCTION} vin={vin} component={component} …")
    resp = lam_invoke.invoke(
        FunctionName=FUNCTION,
        InvocationType="RequestResponse",
        Payload=json.dumps({"stage": STAGE, "component": component, "vins": [vin]}).encode(),
    )
    payload = json.loads(resp["Payload"].read() or b"{}")
    func_error = resp.get("FunctionError")

    check("B5a fresh invoke of the deployed Lambda returned without a function error",
          not func_error, f"FunctionError={func_error} payload={str(payload)[:200]}")
    check("B5b fresh invoke reported zero AVX write failures",
          payload.get("avx_write_failures") == 0,
          f"avx_write_failures={payload.get('avx_write_failures')} "
          f"artifacts_written={payload.get('artifacts_written')} "
          f"duplicates_skipped={payload.get('duplicates_skipped')} "
          f"status={payload.get('status')}")

    after = query_by_finding_key(fkey)
    after_ids = {r["finding_id"] for r in after}
    # The load-bearing D2 property against production data: one logical condition
    # is ONE row. A second row here means dedup is broken and the broker would
    # fire twice for the same condition.
    check("B5c D2 anti-duplicate — finding_key still resolves to exactly one row",
          len(after) == 1, f"rows before={len(before)} after={len(after)}")
    check("B5d D2 identity — finding_id preserved, not re-minted",
          after_ids == before_ids and len(after_ids) == 1,
          f"before={sorted(before_ids)} after={sorted(after_ids)}")

    # This script must not silently leave a row it caused to exist. An invoke that
    # PUT a new row (rather than updating) is the script's residue.
    for new_id in after_ids - before_ids:
        created_finding_ids.append(new_id)
else:
    check("B5 fresh invoke of the deployed Lambda", False,
          "no attributed row to target — refusing to guess a VIN")

# ═════════════════════════════════════════════════════════════════════════════
# PART C — D2 put → noop → update, against the real table.
# NOT an IAM proof: runs under this caller's credentials.
# ═════════════════════════════════════════════════════════════════════════════
print("\n── Part C — D2 idempotency, script-owned scope (NOT an IAM proof) ───")

run_id = uuid.uuid4().hex[:10]
synth_vin = f"LIVEVERIFY{run_id.upper()[:7]}"
agent_fields = {
    "finding": "Live-verification synthetic finding; safe to delete.",
    "severity_hint": "routine",
    "confidence": "low",
    "evidence": [{"source": "adp.liveverify", "value": "synthetic", "provenance": "mocked"}],
}

try:
    # Build through the REAL production path: runner._build_artifact produces the
    # legacy artifact shape, finding_transform converts it, writer.write_finding
    # applies D2. Any of the three reimplemented here would test the script.
    art1, _ = R._build_artifact(synth_vin, "tires", [{"row": 1}], agent_fields)
    f1 = health_artifact_to_finding(art1)
    r1 = W.write_finding(f1, table_name=AVX_TABLE, dynamodb_client=ddb)
    if r1["action"] == "put":
        created_finding_ids.append(r1["finding_id"])
    check("C1 first write on a fresh finding_key is a PutItem",
          r1["action"] == "put", f"action={r1['action']} finding_id={r1['finding_id']}")

    rows = query_by_finding_key(f1["finding_key"])
    check("C1b exactly one row after the first write", len(rows) == 1, f"rows={len(rows)}")
    if rows:
        validate_persisted_row(rows[0], "C1c persisted synthetic Finding")

    # Same inputs → same inputs_hash → same-run retry is a no-op. This is the
    # retry-safety half of D2: a Lambda retry must not duplicate.
    f2 = health_artifact_to_finding(R._build_artifact(
        synth_vin, "tires", [{"row": 1}], agent_fields)[0])
    r2 = W.write_finding(f2, table_name=AVX_TABLE, dynamodb_client=ddb)
    check("C2a re-run with unchanged inputs is a no-op, not a second row",
          r2["action"] == "noop", f"action={r2['action']}")
    check("C2b finding_id preserved across the no-op",
          r2["finding_id"] == r1["finding_id"],
          f"{r1['finding_id']} → {r2['finding_id']}")
    rows = query_by_finding_key(f1["finding_key"])
    check("C2c still exactly one row", len(rows) == 1, f"rows={len(rows)}")

    # Changed inputs → in-place UPDATE preserving finding_id. This is the clause
    # T16.4 names: "observing an in-place update rather than a duplicate row".
    f3 = health_artifact_to_finding(R._build_artifact(
        synth_vin, "tires", [{"row": 2, "changed": True}], agent_fields)[0])
    check("C3a changed inputs produce a different inputs_hash",
          f3["inputs_hash"] != f1["inputs_hash"],
          f"{f1['inputs_hash'][:16]}… → {f3['inputs_hash'][:16]}…")
    r3 = W.write_finding(f3, table_name=AVX_TABLE, dynamodb_client=ddb)
    check("C3b changed inputs update IN PLACE, not append",
          r3["action"] == "update", f"action={r3['action']}")
    check("C3c finding_id preserved across the in-place update",
          r3["finding_id"] == r1["finding_id"],
          f"{r1['finding_id']} → {r3['finding_id']}")

    rows = query_by_finding_key(f1["finding_key"])
    check("C3d D2 anti-duplicate — still exactly one row after three writes",
          len(rows) == 1, f"rows={len(rows)}")
    if len(rows) == 1:
        persisted = _native(rows[0])
        check("C3e the update actually landed — inputs_hash advanced on the row",
              persisted.get("inputs_hash") == f3["inputs_hash"],
              f"row inputs_hash={str(persisted.get('inputs_hash'))[:16]}…")
        check("C3f audit_trail appended by the in-place update",
              len(persisted.get("audit_trail") or []) >= 1,
              f"audit_trail entries={len(persisted.get('audit_trail') or [])}")

finally:
    # Orphans are a failure of the run, not a printed warning alongside exit 0 —
    # that is how synthetic rows accumulate unnoticed in a real table.
    orphaned: list[str] = []
    for fid in created_finding_ids:
        try:
            ddb.delete_item(TableName=AVX_TABLE, Key={"finding_id": {"S": fid}})
        except Exception as exc:
            orphaned.append(fid)
            print(f"  CLEANUP FAILED: {fid}: {exc!r}")
    if created_finding_ids:
        print(f"\ncleaned up {len(created_finding_ids) - len(orphaned)}/"
              f"{len(created_finding_ids)} row(s) this run created")
    check("cleanup — no rows this run created were left behind",
          not orphaned, f"orphaned: {orphaned}" if orphaned else "zero residue")

    # Independent confirmation, because a successful delete_item call is not the
    # same claim as the row being gone.
    still_there = [
        fid for fid in created_finding_ids
        if ddb.get_item(TableName=AVX_TABLE, Key={"finding_id": {"S": fid}}).get("Item")
    ]
    check("cleanup — re-read confirms deletion", not still_there,
          f"still present: {still_there}" if still_there else "verified by re-read")

failed = [r for r in results if not r[1]]
print(f"\n{'FAIL' if failed else 'PASS'} — {len(results) - len(failed)}/{len(results)} checks passed")
if failed:
    print("\nfailed checks:")
    for name, _, detail in failed:
        print(f"  · {name}" + (f" — {detail}" if detail else ""))
sys.exit(1 if failed else 0)
