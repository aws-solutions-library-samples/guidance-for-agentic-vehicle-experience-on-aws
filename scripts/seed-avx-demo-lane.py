#!/usr/bin/env python3
"""Produce AVX Findings on vehicles a signed-in CMS owner actually owns.

WHY THIS EXISTS
===============

Agent 2's nightly sweep paginates the ADP fleet with `ORDER BY vin` over a persisted
cursor. The CMS demo vehicles — the only ones a signed-in iOS/web user owns — sit at
VIN-pool ordinals 0-20 and therefore at the very front of that order, so the cursor
passed them long ago and will not return for years at 375 VINs/night. The nightly
sweep consequently produces Findings that are real and correct and that **no owner can
ever see**.

See `issues/2026-09-23-avx-findings-have-no-resolvable-owner/` for the full diagnosis,
including the two corrections it went through. The short version: this is a *selection*
problem, not a data problem and not a code defect. Battery and brakes both have ADP
health data for owner-bearing demo VINs today; only tires has none.

`runner.lambda_handler` already accepts an explicit VIN list (`event.get("vins")`), so
no code change and no ADP seed is needed — only a deliberate invocation. That is this
script.

OWNERSHIP KEYS ON THE DRIVER, NOT ON `sold_to`
==============================================

User decision 2026-09-23: *"follow the same construct as the drivers so a vin is
assigned a driver/user"*. So the target set is vehicles assigned to a driver whose
`cognitoEmail` resolves to a real user in the CMS pool — those are the findings a
signed-in principal can actually be authorized against. `sold_to` is a `CUST-`
commercial value with no customer record and no matching claim, so it can be
attributed but never authorized; using it was the sibling issue's error.

CURSOR SAFETY — the property that makes this safe to run any time
=================================================================

`runner.run()` gates the entire cursor path behind `if vins is None:` (runner.py:384).
An explicit VIN list therefore never reads or writes cursor state, so this script cannot
disturb the nightly sweep's progress. Verified empirically 2026-09-23 (cursors byte-identical
before and after) and asserted below rather than assumed.

WHAT IT DOES NOT DO
===================

It does not delete anything. The Findings it produces are legitimate, carry the normal
8-day TTL, and are what FG15's and G7's live-verification consume — so cleaning them up
would defeat the purpose. Re-running is safe: D2's `write_finding()` updates in place on
a matching `finding_key` rather than duplicating.

USAGE
  # Derive the VIN set and show what would be invoked. Costs nothing. DEFAULT.
  AWS_REGION=us-west-2 VSA_STAGE=staging python3 scripts/seed-avx-demo-lane.py

  # Actually invoke. Costs roughly $0.40 of Bedrock tokens per full run.
  AWS_REGION=us-west-2 VSA_STAGE=staging python3 scripts/seed-avx-demo-lane.py --invoke

EXIT  0 ok · 1 a step failed · 2 refused to run
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import defaultdict
from typing import Any

# ─── Pre-flight: explicit targeting, no defaults ────────────────────────────
#
# This machine exports AWS_REGION=us-east-1 and VSA_STAGE=prod, and both silently
# retarget scripts in this repo. A default is what makes a retarget silent, so both
# values must be passed explicitly — same discipline as
# scripts/live-verify-avx-dual-write.py.

REGION = os.environ.get("AWS_REGION")
STAGE = os.environ.get("VSA_STAGE")

if REGION != "us-west-2":
    print(f"refusing to run: AWS_REGION must be explicitly 'us-west-2', got {REGION!r}")
    sys.exit(2)
if STAGE != "staging":
    print(f"refusing to run: VSA_STAGE must be explicitly 'staging', got {STAGE!r}")
    sys.exit(2)

ADP_REGION = "us-east-1"          # ADP lake + Athena are always us-east-1
WORKGROUP = "cvx-staging-analytics"
FUNCTION = f"vsa-{STAGE}-tier2-health-agent-{REGION}"
CMS_VEHICLES = f"cms-{STAGE}-storage-vehicles"
DRIVERS_TABLE = f"cms-{STAGE}-storage-drivers"
#: The pool the AVX API authorizes against. Not derived — a wrong pool would
#: silently classify every driver as unresolvable and report a clean skip.
#: There is deliberately NO default. Pass the CMS staging pool ID explicitly:
#:   export CMS_UI_USER_POOL_ID=<user-pool-id>
#: The pool ID can be found in the CloudFormation export
#: `cms-{stage}-ui-user-pool-id` or in the CMS stack outputs.
_pool_id_raw = os.environ.get("CMS_UI_USER_POOL_ID", "").strip()
if not _pool_id_raw:
    print(
        "refusing to run: CMS_UI_USER_POOL_ID is not set.\n"
        "  A missing or wrong pool silently classifies every driver as\n"
        "  unresolvable and reports a clean skip — absent-and-loud is correct.\n"
        "  Set it to the CMS staging pool ID before running:\n"
        "    export CMS_UI_USER_POOL_ID=<user-pool-id>\n"
        "  Find the value in the CloudFormation export "
        "cms-staging-ui-user-pool-id."
    )
    sys.exit(2)
POOL_ID = _pool_id_raw
HEALTH_TABLE = f"vsa-{STAGE}-health-findings"
AVX_TABLE = f"vsa-{STAGE}-avx-findings"

# Component → (Glue database, table, extra WHERE). Mirrors the per-component sources
# in agents/health/adp_{tire,battery,brake}_source.py. `tires` is included so the
# script reports its zero coverage rather than silently omitting the component —
# an absent row and an unqueried source are different facts.
SOURCES: dict[str, tuple[str, str, str]] = {
    "battery": (f"adp_{STAGE}_energy_usage", "energy_usage", ""),
    "brakes": (f"adp_{STAGE}_service_records", "service_records",
               "service_type = 'brake_service' AND "),
    "tires": (f"adp_{STAGE}_tire_health", "tire_health", ""),
}

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--invoke", action="store_true",
                    help="actually invoke the Lambda (default is a no-cost dry run)")
args = parser.parse_args()

import boto3                                                        # noqa: E402
from botocore.config import Config as BotoConfig                    # noqa: E402

ddb = boto3.client("dynamodb", region_name=REGION)
athena = boto3.client("athena", region_name=ADP_REGION)
cognito = boto3.client("cognito-idp", region_name=REGION)
lam = boto3.client(
    "lambda", region_name=REGION,
    # The runner's wall-clock ceiling is 600s; boto3's 60s default would abort a
    # legitimate invoke and report it as a failure.
    config=BotoConfig(read_timeout=660, connect_timeout=15, retries={"max_attempts": 0}),
)

failures: list[str] = []

# Read the deployed Lambda's own token budget rather than assuming one. Chunking
# below depends on it, and a hardcoded copy would silently go stale the moment an
# operator raises the limit.
try:
    lambda_env: dict[str, str] = (
        lam.get_function_configuration(FunctionName=FUNCTION)
        .get("Environment", {})
        .get("Variables", {})
    )
except Exception as exc:
    print(f"refusing to run: cannot read {FUNCTION} configuration ({exc})")
    sys.exit(2)


def run_athena(database: str, sql: str) -> list[str]:
    """Run a single-column query and return the values. Raises on failure."""
    eid = athena.start_query_execution(
        QueryString=sql,
        QueryExecutionContext={"Database": database},
        WorkGroup=WORKGROUP,
    )["QueryExecutionId"]
    for _ in range(60):
        st = athena.get_query_execution(QueryExecutionId=eid)["QueryExecution"]["Status"]
        state = st["State"]
        if state == "SUCCEEDED":
            break
        if state in ("FAILED", "CANCELLED"):
            raise RuntimeError(f"Athena {state}: {st.get('StateChangeReason', '')}")
        time.sleep(2)
    else:
        raise RuntimeError("Athena query timed out")

    out: list[str] = []
    token = None
    while True:
        kw: dict[str, Any] = {"QueryExecutionId": eid}
        if token:
            kw["NextToken"] = token
        page = athena.get_query_results(**kw)
        rows = page["ResultSet"]["Rows"]
        if not token:
            rows = rows[1:]            # strip the header row on the first page only
        out.extend(r["Data"][0].get("VarCharValue", "") for r in rows)
        token = page.get("NextToken")
        if not token:
            return [v for v in out if v]


def cursors() -> dict[str, str]:
    """Current per-component selection cursors, for the no-disturbance assertion."""
    got: dict[str, str] = {}
    for comp in SOURCES:
        item = ddb.get_item(
            TableName=HEALTH_TABLE,
            Key={"pk": {"S": f"CURSOR#{comp}"}, "sk": {"S": "STATE"}},
        ).get("Item")
        got[comp] = (item or {}).get("last_vin", {}).get("S", "(none)")
    return got


# ─── 1. Owner map — the DRIVERS construct (user decision 2026-09-23) ────────
#
# Ownership keys on the DRIVER, not on `vehicles.sold_to`. User's instruction:
# *"can we follow the same construct as the drivers so a vin is assigned a
# driver/user"*. See
# `issues/2026-09-23-avx-owner-id-not-comparable-to-cognito-sub/` § "USER DECISION".
#
# Chain: Cognito principal -> drivers.cognitoEmail -> driverId + assignedVehicleId
#        -> vehicles.vehicleId -> vin -> Finding.scope.vin
#
# Why `email` is the join key and not `custom:driverId`: that attribute is
# `mutable=False` and populated on 1 of 16 users, and an immutable Cognito
# attribute is writable only at user creation — so the rest cannot be backfilled
# without delete-and-recreate. `email` is this pool's `UsernameAttributes` AND
# `AutoVerifiedAttributes`, so Cognito enforces uniqueness and verification, and
# `drivers.cognitoEmail` exists as a column precisely for this join.
#
# `sold_to` is deliberately NOT used. It is a `CUST-XXXXXXXX` commercial value with
# no corresponding customer record anywhere and no claim on the pool carrying it, so
# it can be *attributed* but never *authorized* against. That distinction is what
# the sibling issue got wrong; it is not repeated here.
print("→ resolving owners via the drivers construct …")

pool_emails: set[str] = set()
kw: dict[str, Any] = {"UserPoolId": POOL_ID, "Limit": 60}
while True:
    page = cognito.list_users(**kw)
    for u in page.get("Users", []):
        for a in u.get("Attributes", []):
            if a["Name"] == "email" and a.get("Value"):
                pool_emails.add(a["Value"].lower())
    tok = page.get("PaginationToken")
    if not tok:
        break
    kw["PaginationToken"] = tok

drivers: list[dict[str, Any]] = []
scan_kw: dict[str, Any] = {"TableName": DRIVERS_TABLE,
                           "ProjectionExpression": "driverId, assignedVehicleId, cognitoEmail"}
while True:
    page = ddb.scan(**scan_kw)
    drivers.extend(page.get("Items", []))
    if not page.get("LastEvaluatedKey"):
        break
    scan_kw["ExclusiveStartKey"] = page["LastEvaluatedKey"]

vehicle_vin: dict[str, str] = {}
scan_kw = {"TableName": CMS_VEHICLES, "ProjectionExpression": "vehicleId, vin"}
while True:
    page = ddb.scan(**scan_kw)
    for it in page.get("Items", []):
        vid, vin = it.get("vehicleId", {}).get("S"), it.get("vin", {}).get("S")
        if vid and vin:
            vehicle_vin[vid] = vin
    if not page.get("LastEvaluatedKey"):
        break
    scan_kw["ExclusiveStartKey"] = page["LastEvaluatedKey"]

# owner_of maps VIN -> driverId, but ONLY for drivers whose cognitoEmail resolves to
# a real pool user. A driver with no cognitoEmail, or one naming a user in a
# different pool, is not an authorizable identity — some seeded cognitoEmail
# values exist only in the meridian-fleet tenant pool (samantha.carter,
# priya.shah) or have no corresponding Cognito account at all, so skipping
# silently would overstate coverage exactly the way the sibling issue did.
# Note: a third rental-brand driver email listed in earlier versions of this
# comment was retired when the rental tenant was renamed to waypoint-rentals
# on 2026-08-10 (commit c6dbaaa); the persona no longer exists.
owner_of: dict[str, str] = {}
unresolvable: list[str] = []
for d in drivers:
    g = lambda k: d.get(k, {}).get("S")
    em, did, av = g("cognitoEmail"), g("driverId"), g("assignedVehicleId")
    if not (em and av):
        continue
    if em.lower() not in pool_emails:
        unresolvable.append(f"{did} ({em})")
        continue
    vin = vehicle_vin.get(av)
    if vin:
        owner_of[vin] = did

if unresolvable:
    print(f"  NOTE {len(unresolvable)} driver(s) name a cognitoEmail absent from "
          f"{POOL_ID} — not authorizable, skipped:")
    for u in unresolvable:
        print(f"       {u}")

if not owner_of:
    print(f"  FAIL: no driver in {DRIVERS_TABLE} has BOTH a cognitoEmail in the pool "
          f"AND an assignedVehicleId resolving to a vin — nothing to target")
    sys.exit(1)
print(f"  {len(owner_of)} authorizable vehicle(s) across "
      f"{len(set(owner_of.values()))} driver(s)")

# ─── 2. Which owned VINs have ADP health data, per component ────────────────
#
# Derived live rather than hardcoded, so the lane follows the CMS fleet as it
# changes. A pinned list would rot the first time a demo vehicle is added or sold.
quoted = ",".join(f"'{v}'" for v in sorted(owner_of))
per_component: dict[str, list[str]] = {}
for comp, (db, table, extra) in SOURCES.items():
    try:
        vins = run_athena(
            db,
            f"SELECT DISTINCT vin FROM {table} "
            f"WHERE {extra}vin IN ({quoted}) ORDER BY vin",
        )
    except Exception as exc:
        print(f"  FAIL {comp}: {exc}")
        failures.append(f"{comp} source query")
        continue
    per_component[comp] = vins
    print(f"  {comp:8} {len(vins):3} owned VIN(s) with data in {db}.{table}")

if not any(per_component.values()):
    print("  FAIL: no owned VIN has ADP health data in any component")
    sys.exit(1)

# ─── 3. Report the owner distribution, so limiter coverage is visible ───────
by_owner: dict[str, list[tuple[str, str]]] = defaultdict(list)
for comp, vins in per_component.items():
    for v in vins:
        by_owner[owner_of[v]].append((v, comp))

total = sum(len(v) for v in per_component.values())
print(f"\n→ demo lane: {total} finding(s) across {len(by_owner)} owner(s)")
for owner, rows in sorted(by_owner.items(), key=lambda kv: -len(kv[1])):
    print(f"  {owner}  {len(rows)} card(s)")
    for v, c in sorted(rows):
        print(f"      {v}  {c}")

peak = max((len(r) for r in by_owner.values()), default=0)
print(f"\n  peak findings for one owner: {peak}")
print("  G7 staging policy defaults: rate-limit day:10 hour:3, cardinality-cap 15, APNs p0")
if peak <= 3:
    # Stated as a limitation rather than left for someone to discover after building
    # G7 and concluding its limiters work because nothing tripped them.
    print("  NOTE: this volume does NOT exercise G7's rate limit or cardinality cap.")
    print("        Those thresholds are SSM config (spec Addendum D3), not code constants —")
    print("        lower them during G7 verification instead of seeding for volume:")
    print(f"          /vsa/{STAGE}/avx/broker/rate-limit, …/cardinality-cap,")
    print(f"          …/apns-severity-threshold  (p0 default fires on no routine/soon finding)")

if not args.invoke:
    _b = int(lambda_env.get("TIER2_MAX_TOKENS", "50000"))
    _c = max(1, int(_b * 0.8 // 9000))
    print(f"\n  would invoke in chunks of {_c} VIN(s) "
          f"(deployed TIER2_MAX_TOKENS={_b}; ~9000 tokens/VIN observed)")
    print("\n(dry run — no Lambda invoked, nothing written. Re-run with --invoke to execute.)")
    sys.exit(1 if failures else 0)

# ─── 4. Invoke, chunked to fit the runner's token budget ───────────────────
#
# CHUNKING IS LOAD-BEARING, not an optimisation. The runner aborts a run on
# BudgetExceeded when accumulated tokens exceed TIER2_MAX_TOKENS, and it does so
# MID-LIST: the VINs after the breach are simply never processed. Measured
# 2026-09-23, battery costs ~8,675 tokens/VIN against a 50,000 limit, so only ~5.8
# VINs fit and a 7-VIN pinned run silently drops the tail.
#
# This script shipped with exactly that defect for one revision: it passed the full
# list, reported `avx_write_failures=0`, exited PASS, and left one owner's Finding
# unwritten. `avx_write_failures` counts AVX writes that FAILED, not VINs that were
# never reached, so it cannot see a truncation — the same shape as the unpaged
# filter_log_events call in scripts/live-verify-avx-dual-write.py.
#
# So: chunk to fit, and additionally treat `status == "aborted"` as a problem rather
# than trusting the counters.
budget = int(lambda_env.get("TIER2_MAX_TOKENS", "50000"))
# Deliberately pessimistic: the most expensive component measured, with headroom.
# Underestimating silently truncates; overestimating only costs an extra invoke.
TOKENS_PER_VIN_EST = 9000
chunk_size = max(1, int(budget * 0.8 // TOKENS_PER_VIN_EST))
print(f"\n→ invoking {FUNCTION} (budget {budget} tokens; "
      f"chunking at {chunk_size} VIN(s)/invoke) …")

before = cursors()
produced = 0
skipped = 0
for comp, vins in per_component.items():
    if not vins:
        print(f"  {comp:8} skipped (no owned VIN has data)")
        continue
    for start in range(0, len(vins), chunk_size):
        batch = vins[start:start + chunk_size]
        resp = lam.invoke(
            FunctionName=FUNCTION,
            InvocationType="RequestResponse",
            Payload=json.dumps({"stage": STAGE, "component": comp, "vins": batch}).encode(),
        )
        payload = json.loads(resp["Payload"].read() or b"{}")
        err = resp.get("FunctionError")
        wrote = payload.get("artifacts_written", 0)
        dupes = payload.get("duplicates_skipped", 0)
        avx_failed = payload.get("avx_write_failures")
        status = payload.get("status")
        produced += wrote
        skipped += dupes

        # An aborted run means VINs after the breach were never processed. Report it
        # as a failure and name them, rather than letting a clean avx_write_failures
        # count imply the batch completed.
        truncated = status == "aborted"
        flag = "" if not err and avx_failed == 0 and not truncated else "  <-- CHECK"
        print(f"  {comp:8} [{start + 1}-{start + len(batch)}/{len(vins)}] "
              f"artifacts_written={wrote} duplicates_skipped={dupes} "
              f"avx_write_failures={avx_failed} status={status}{flag}")
        if err or avx_failed != 0:
            failures.append(
                f"{comp} invoke (FunctionError={err}, avx_write_failures={avx_failed})")
        if truncated:
            failures.append(
                f"{comp} batch {start + 1}-{start + len(batch)} aborted on the token "
                f"budget — VINs after the breach were NOT processed "
                f"({', '.join(batch)}). Re-run, or raise TIER2_MAX_TOKENS.")

# `artifacts_written` overcounts AVX Findings: an aborted run also writes a partial
# artifact under pk='VEH#REDACTED' via writer.put() WITHOUT the AVX dual-write. Every
# nightly run currently aborts on the token budget, so the gap is routine. Named here
# so the count above is not read as an AVX-row count.
print("  (note: artifacts_written counts the BudgetExceeded partial artifact too,")
print("   which is legacy-only — expect AVX rows to be slightly fewer. See the issue.)")

# ─── 5. Assert the nightly sweep was not disturbed ─────────────────────────
after = cursors()
if before == after:
    print(f"\n  cursors unchanged — nightly sweep undisturbed: {after}")
else:
    # Would mean the explicit-vins path started touching cursor state, i.e. the
    # runner.py:384 guard regressed. That is a real defect, not a warning.
    print(f"\n  FAIL: cursor moved. before={before} after={after}")
    failures.append("cursor disturbed — the runner.py:384 'if vins is None' guard regressed")

if failures:
    print(f"\nFAIL — {len(failures)} problem(s):")
    for f in failures:
        print(f"  · {f}")
    sys.exit(1)
active = len([c for c, v in per_component.items() if v])
if produced:
    print(f"\nPASS — demo lane seeded; {produced} new artifact(s) across {active} component(s)"
          + (f", {skipped} already current" if skipped else ""))
else:
    # D2 updates in place on a matching finding_key and the legacy writer reports a
    # duplicate when inputs_hash is unchanged, so a re-run against unchanged ADP data
    # legitimately writes nothing. That is the lane being ALREADY CURRENT, not a failure.
    print(f"\nPASS — demo lane already current; nothing to write "
          f"({skipped} duplicate(s) skipped across {active} component(s)). "
          f"Re-running is idempotent by design.")
sys.exit(0)
