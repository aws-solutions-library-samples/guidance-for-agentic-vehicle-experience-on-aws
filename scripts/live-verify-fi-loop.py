#!/usr/bin/env python3
"""Live verification for FI Fleet-Health Stage D, task 9.3.

Spec:   .kiro/specs/2026-10-05-fi-fleet-health-stage-d/g93lv-brief.md
Facts:  .kiro/specs/2026-10-05-fi-fleet-health-stage-d/g93lv-facts.md
Branch: wip/fi-health-d-g93-lv (worktree ~/cvx-wt-fi-health-d-g93-lv)
Stage:  g93lv  (stage D / 9.3: all parts built)

Parts:
  --part pre            read-only: stage/identity/windows; every deployed-fact
                        precondition; residue snapshot; pattern pick; manifest
  --part issue          --allow-writes  temp user; investigation POST/poll;
                        decision approve; quality.issue check; issue_recall approve;
                        DDB draft/issued row; S3 SSE-KMS head; thread versions
  --part after-run      read-only: run record (status+counters); recall_vehicle_status;
                        AVX recall Findings per VIN (item types, component_lots, null
                        years, disclosure_txt); fleet queues; CMS recall list + drill-down;
                        thread lake flat-key objects; ADP reconcile dry run
  --part cleanup        --allow-writes: delete manifest ids (incl. digital-thread rows +
                        lake objects); confirm each absent; residue == pre snapshot
  --part after-cleanup  read-only: recall run after cleanup; id gone from views; no
                        new Findings
  --part ground         --allow-writes --allow-managed-writes  fault-grounding flow

Flags:
  --stage staging       required; refuses before any AWS client is created
  --part <name>         run one named part
  --allow-writes        gates write parts (not used by ``pre``)

Exit 0: every selected item ran and passed.
Exit 1: any FAIL.
Exit 2: any SKIP.

Facts cited (``file:line`` in the AVX worktree unless noted):
  see g93lv-facts.md beside the brief for full attribution.

  AVX investigations route
    handler.py:5               POST path /investigations
    handler.py:251-263         pattern_id validation regex
    handler.py:759             token group product-engineer
    handler.py:648-653         202 shape
    handler.py:399-450         lock/cap transaction
  fi_quality agent
    write.py:100               FINDING_KIND = "quality.investigation"
    write.py:103               POPULATION_ID = "meridian-motors"
  Tier 2 recall run
    run_records.py:1-12        agent_component pk
    agents/fi_recall_warranty/evidence.py:148-165  recall.rule shape
  Schedules  (live reads scrubbed to 123456789012)
    vsa-staging-tier2-daily-recall-us-west-2  cron(0 5 * * ? *)   Etc/UTC
    vsa-staging-tier2-daily-warranty-us-west-2 cron(20 5 * * ? *) Etc/UTC
    dms-staging-fi-dealer-progress            cron(40 5 * * ? *)  UTC
  Code-check zip paths
    agents/fi_quality/readers.py          must have no ``rcr.name``
    agents/fi_recall_warranty/sql/select_recalls.sql  must join recall_campaigns_issued
  Glue / LF
    g93lv-facts.md §ADP views            VIRTUAL_VIEW, LF admin = the operator's IAM user
    g93lv-facts.md §OPEN                 recall_campaigns_issued absent as of 2026-10-20
  ESM
    g93lv-facts.md §C5 guard             State: Disabled as of 2026-10-20
  Corpus
    adp_writer.py seed_failure_mode      pk = fm#{failure_mode_id}, component_name field
"""
from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import io
import json
import os
import re
import secrets
import signal as _sig
import sys
import zipfile
from typing import Any, Iterator

# ── repo root on sys.path (brief §Imports) ───────────────────────────────────
# Mirrors live-verify-fi-quality.py:53-55.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

# ── live-write guard (scripts/lib/live_write_guard.py) ───────────────────────
_SCRIPTS_LIB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib")
if _SCRIPTS_LIB not in sys.path:
    sys.path.insert(0, _SCRIPTS_LIB)
import live_write_guard as _lwg  # noqa: E402

#: Set to ``--stage`` by the live-run step, on the user's go, and by nothing else.
_LIVE_GO_ENV = "FI_LOOP_LIVE_GO"

# ── constants ─────────────────────────────────────────────────────────────────

_ALLOWED_STAGE = "staging"
_AVX_REGION = "us-west-2"
_ADP_REGION = "us-east-1"

# Tier 2 Lambda name (brief §Names; live read 2026-10-20 ~22:55Z)
_TIER2_FN = "vsa-{stage}-tier2-health-agent-{region}"

# Investigation ESM queue (g93lv-facts.md §AVX investigations route)
_INV_QUEUE = "vsa-{stage}-avx-investigations"
_INV_TABLE = "vsa-{stage}-avx-investigations"

# Schedules to guard (g93lv-facts.md §Schedules; W4 fix: all cron-based Tier 2
# schedules + DMS dealer-progress stand-in).
# diagnostics (vsa-{stage}-tier2-diagnostics-{region}) uses rate(5 minutes) —
# an unsupported expression that the guard already refuses; listed as safe in
# g93lv-facts.md §W4 (writes only CMS DTC Findings, not the recall write path).
# DMS warranty stand-ins (adjudicator/candidates) are listed as safe in
# g93lv-facts.md §W4 (don't affect the recall issue path).
# Source: infrastructure/lib/vsa-tier2-stack.ts:2072-2142.
_SCHEDULES: list[tuple[str, str, str]] = [
    # (name_template, _ignored_group, region)
    # Tier 2 cron-based component schedules — staggered, all UTC.
    ("vsa-{stage}-tier2-daily-battery-{region}",   "default", _AVX_REGION),  # cron(0 2 * * ? *)
    ("vsa-{stage}-tier2-daily-tires-{region}",     "default", _AVX_REGION),  # cron(20 2 * * ? *)
    ("vsa-{stage}-tier2-daily-brakes-{region}",    "default", _AVX_REGION),  # cron(40 2 * * ? *)
    ("vsa-{stage}-tier2-daily-cost-{region}",      "default", _AVX_REGION),  # cron(0 4 * * ? *)
    ("vsa-{stage}-tier2-daily-recall-{region}",    "default", _AVX_REGION),  # cron(0 5 * * ? *)
    ("vsa-{stage}-tier2-daily-warranty-{region}",  "default", _AVX_REGION),  # cron(20 5 * * ? *)
    ("vsa-{stage}-tier2-daily-service-{region}",   "default", _AVX_REGION),  # cron(40 5 * * ? *)
    ("vsa-{stage}-tier2-daily-rebalance-{region}", "default", _AVX_REGION),  # cron(0 6 * * ? *)
    # DMS fleet-health stand-in (produces leg G's Findings).
    ("dms-{stage}-fi-dealer-progress",              "default", _AVX_REGION),  # cron(40 5 * * ? *)
]

# Glue databases (g93lv-facts.md §ADP views)
_GLUE_ENGINEERING_DB = "adp_{stage}_engineering"
_GLUE_FLEET_RECALL_DB = "adp_{stage}_fleet_recall"
_RECALL_CAMPAIGNS_ISSUED_TABLE = "recall_campaigns_issued"
_RECALL_CAMPAIGNS_RECEIVED_TABLE = "recall_campaigns_received"

# ADP DDB tables
_FAULT_PATTERNS_TABLE = "adp-{stage}-fault-patterns"
_ENGINEERING_CORPUS_TABLE = "adp-{stage}-engineering-corpus"

# Maintenance write window: never 01:45–03:30 UTC (brief §Windows)
_MAINT_START_MIN = 1 * 60 + 45  # 105 min
_MAINT_END_MIN = 3 * 60 + 30    # 210 min

# Schedule guard margin (brief §Windows)
_SCHED_GUARD_MIN = 45

# SQL markers for code-check (brief §Tier 2 Lambda code check)
_READERS_FORBIDDEN = "rcr.name"
_SELECT_RECALLS_REQUIRED = "recall_campaigns_issued"

# Livetest markers (brief §Cognito; K7: one constant each for writes and residue checks)
_LIVETEST_USER_PREFIX = "livetest-fi-loop-"   # temp username: livetest-fi-loop-<run>-<role>
_LIVETEST_NOTES_PREFIX = "LIVETEST g9.3 "     # notes field:  LIVETEST g9.3 <run>
# Run-id regex — validated before any client is created (brief §K1 / security-review warning).
# Format: fi-loop-YYYYMMDDTHHMMSS-8hex
_RUN_ID_RE = re.compile(r"^fi-loop-\d{8}T\d{6}-[0-9a-f]{8}$")

# Manifest directory (brief §Manifest: durable dir outside /tmp; S2 fix).
# macOS cleans /tmp on reboot and periodically — use ~/.fi-loop-manifests instead.
_MANIFEST_DIR = os.path.expanduser("~/.fi-loop-manifests")

# Manifest filename template (created in pre, read by later parts)
_MANIFEST_TEMPLATE = "{manifest_dir}/{run_id}-fi-loop-manifest.json"

# VIN validation regex (brief §SQL)
_VIN_RE = re.compile(r"^[A-HJ-NPR-Z0-9]{17}$")
# Campaign id regex (brief §SQL; g93lv-facts.md §CMS)
_CAMPAIGN_ID_RE = re.compile(r"^MRD-\d{4}-\d{3}$")
# Lot regex (brief §SQL)
_LOT_RE = re.compile(r"^[A-Z0-9-]+/[A-Z0-9-]+$")
# DTC fault code regex (brief §SQL; W7 fix: shape-check every interpolated code)
# The same rule as CMS's grounding route (services/fleet_intelligence/groundings.py
# `_FAULT_CODE_RE`): 2-32 of uppercase letters, digits and underscore. No quote,
# space or punctuation can pass, so an interpolated code can't leave its SQL literal.
# The earlier `^[A-Z][0-9]{4}$` refused real catalog codes with hex digits or a
# suffix (P0AA6, P0A0D, P0A80, B0001_FIRE, U3000_CRASH: 5 of the 10 p0 codes), so
# leg G's step 8 raised for those vehicles (2026-10-26, prompt 124).
_DTC_RE = re.compile(r"^[A-Z0-9_]{2,32}$")

# ── issue-part constants ──────────────────────────────────────────────────────
# Cognito pool and API stack names (live reads, scrubbed).
_CMS_POOL_NAME = "cms-staging-ui-users"
_VSA_API_STACK = "vsa-{stage}-api"

# AVX findings / digital-thread DDB tables (g93lv-facts.md §Approve route, §ADP writer)
_FINDINGS_TABLE = "vsa-{stage}-avx-findings"
_ACTIONS_TABLE = "vsa-{stage}-avx-actions"
_AGENT_RUNS_TABLE = "vsa-{stage}-avx-agent-runs"
# DMS RO table name.
# Source: DMS 15231e4 source/simulation/fi_stand_ins.py:495 (_ro_table).
_DMS_RO_TABLE = "dms-{stage}-repair-orders"
_DIGITAL_THREAD_TABLE = "adp-{stage}-digital-thread"
_RECALL_CAMPAIGNS_ISSUED_DDB = "adp-{stage}-recall-campaigns-issued"

# Recall agent identifiers (g93lv-facts.md §Tier 2 recall run)
# agent_component pk: "agent-fi-recall-warranty#recall" (run_records.py:4)
_RECALL_AGENT_COMPONENT = "agent-fi-recall-warranty#recall"
# Recall Finding kind (writer.py:36)
_RECALL_FINDING_KIND = "fleet.recall.vehicle"
# Recall agent id (writer.py:30)
_RECALL_AGENT_ID = "agent-fi-recall-warranty"

# recall_vehicle_status Athena query for a single campaign_id (after-run check).
# Source: recall_vehicle_status.sql (ADP 9bb82e96) — the view is available as a
# Glue VIRTUAL_VIEW; query it like any table.
# The script queries adp_{stage}_fleet_recall.recall_vehicle_status directly.
# Parameter: campaign_id (shape-checked against _CAMPAIGN_ID_RE before use).
_RECALL_STATUS_SQL = """\
SELECT vin, fleet_id, match_basis, confirming_code, remedied
FROM adp_{stage}_fleet_recall.recall_vehicle_status
WHERE campaign_id = '{campaign_id}'
  AND remedied = FALSE
ORDER BY vin
"""

# recall_vehicle_status query for after-cleanup: counts ALL rows (including remedied).
# W6 rest fix: a remedied row for the campaign_id still means the campaign is present;
# after cleanup both remedied and non-remedied rows should be 0.
_RECALL_STATUS_ALL_SQL = """\
SELECT vin, remedied
FROM adp_{stage}_fleet_recall.recall_vehicle_status
WHERE campaign_id = '{campaign_id}'
ORDER BY vin
"""

# Query the received-view catalog row for disclosure_txt byte-equality (C5 fix).
# Source: recall_campaigns_received view (ADP 9bb82e96).
_RECALL_CATALOG_SQL = """\
SELECT summary, consequence, remedy
FROM adp_{stage}_fleet_recall.recall_campaigns_received
WHERE campaign_id = '{campaign_id}'
LIMIT 1
"""

# CMS recall endpoints (g93lv-facts.md §CMS)
_CMS_RECALLS_PATH = "/api/v1/fleet-intelligence/recalls"          # GET ?fleetId=
_CMS_VEHICLE_RECALLS_PATH = "/api/v1/fleet-intelligence/vehicles/{vin}/recalls"  # GET ?fleetId=

# Ground-leg (leg G) constants.
# Finding kind for fleet-health items.
# Source: agents/fi_fleet_health/write.py:35.
_FLEET_HEALTH_FINDING_KIND = "fleet.health.service"
# Action kinds approved by the ground leg.
# Source: lambdas/api-avx-findings/handler.py:337 (ground_vehicle), :351 (book_fleet_service).
_GROUND_VEHICLE_KIND = "ground_vehicle"
_BOOK_FLEET_SERVICE_KIND = "book_fleet_service"
#: Action statuses that mean book_fleet_service went through: the DMS executor answers
#: `in_progress` once DMS has the RO (agents/executors/dms.py); the DMS event consumer
#: moves it to `completed` at Ready/Delivered/Invoiced, which an idempotent re-approve on
#: resume returns; `executed` is kept for an executor that finishes synchronously. Every
#: other Action status (pending_execution, cancelled, declined, dismissed, failed) is not
#: a booking: an early close cancels the Action.
_BOOK_OK_STATUSES = frozenset({"in_progress", "executed", "completed"})
#: AVX Action ids are `AVX-ACT-<uuid>`; DMS stores `avx_action_id` as the bare UUID
#: (agents/executors/dms.py `_dms_correlation_id`).
_AVX_ACTION_PREFIX = "AVX-ACT-"
_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


def _dms_action_ids(action_id: str) -> frozenset:
    """The `avx_action_id` values a DMS RO for *action_id* can carry.

    DMS stores the bare UUID (agents/executors/dms.py `_dms_correlation_id`); the
    prefixed form is kept so a row written by an older path still matches. Empty for
    an empty id, so "ours" can never match a row with no recorded booking.
    """
    if not isinstance(action_id, str) or not action_id or action_id == "pending":
        return frozenset()
    bare = action_id.removeprefix(_AVX_ACTION_PREFIX)
    return frozenset({action_id, bare}) if _UUID_RE.fullmatch(bare) else frozenset({action_id})
# CMS groundings and return-to-service paths.
# Source: CMS main 81a90d385 services/fleet_intelligence/index.py:3507-3512.
_CMS_GROUNDINGS_CREATE_PATH = "api/v1/fleet-intelligence/groundings"    # POST
_CMS_RTS_PATH = "api/v1/fleet-intelligence/groundings/{vehicleId}/return-to-service"  # POST
# CMS UI stack output key for the API URL.
# Source: CMS main 81a90d385 deployment/stacks/ui_stack.py:5527.
_CMS_UI_STACK = "cms-{stage}-ui"
_CMS_API_OUTPUT_KEY = "APIEndpoint"
# CMS vehicles table (for registry vehicleId lookup — W8 fix).
# Key: vehicleId; vin is a non-key attribute.
# Source: CMS main 81a90d385 deployment/stacks/ui_stack.py (storage-vehicles).
_CMS_VEHICLES_TABLE = "cms-{stage}-storage-vehicles"
# CMS groundings table (key: vehicleId PK + actionId SK; sparse GSI open-groundings-index).
# Source: CMS main 81a90d385 services/fleet_intelligence/groundings.py:13 + deployment
#         stacks/tests/test_storage_stack_fi_groundings.py:9.
_CMS_GROUNDINGS_TABLE = "cms-{stage}-storage-vehicle-groundings"
# DMS stand-in Lambda name.
# Source: DMS 15231e4 source/simulation/fi_stand_ins.py:536 (dealer_progress_handler).
_DMS_DEALER_PROGRESS_FN = "dms-{stage}-fi-dealer-progress"
# ADP fault view database and table.
# Source: ADP 9a08bca1 platform-foundation/source/data-products/fleet_fault_events/views/vehicle_fault_status.sql.
_FAULT_STATUS_DB = "adp_{stage}_fleet_health"
_FAULT_STATUS_TABLE = "vehicle_fault_status"
# DMS lake lag and CMS health-cache lag (established from code — C7 fix).
# vehicle_fault_status is a Glue VIRTUAL_VIEW over the DMS lake (no daily job);
# lag = DMS stream merge cadence (fi_rw_world/world.py comment + DMS 15231e4 facts.md).
# CMS return-to-service reads CMS's hourly health cache (lifecycle_cache.py; ui_stack.py:4306 cron).
# Source: g93lv-facts.md §DMS, §CMS.
_DMS_LAKE_LAG_MINUTES = 30   # DMS stream merge cadence (fi_rw_world/world.py comment)
_CMS_HEALTH_CACHE_LAG_HOURS = 1  # CMS hourly health-cache refresh (ui_stack.py:4306 cron)
_ADP_DAILY_JOB_UTC = 5        # recall/health Tier 2 run at 05:00Z

# CMS recall cache S3 location (g93lv-facts.md §CMS).
# Source: CMS main 81a90d385 deployment/stacks/_fleet_intelligence_naming.py:168-186:
#   bucket = cms-{stage}-athena-results-{account}-us-east-1
#   ATHENA_OUTPUT_LOC = s3://{bucket}/fleet-intelligence/
# Source: CMS main 81a90d385 services/fleet_intelligence/lifecycle_cache.py:203-215:
#   recall_views_root() → "{prefix}_cache/fi-recall-views-v1/"
_CMS_RECALL_BUCKET = "cms-{stage}-athena-results-{account}-us-east-1"
_CMS_RECALL_CACHE_PREFIX = "fleet-intelligence/_cache/fi-recall-views-v1/"
# The CMS Athena results bucket lives in us-east-1 (same region as ADP's catalog).
# Source: CMS main 81a90d385 deployment/stacks/_fleet_intelligence_naming.py:35 (ADP_REGION).
_CMS_RECALL_S3_REGION = "us-east-1"

# Max cache age for CMS recall slices.
# Source: CMS main 81a90d385 services/fleet_intelligence/recall_source.py:28
#   RECALL_MAX_AGE_SECONDS = 26 * 3600
# W10 fix: a slice older than this would make CMS show "unavailable"; the check
# should fail in that case rather than passing on stale data.
_CMS_RECALL_MAX_AGE_SECONDS: int = 26 * 3600

# ADP lake bucket (g93lv-facts.md §adp_writer.py world setup)
_ADP_LAKE_BUCKET = "adp-{stage}-foundation-lake-{account}-us-east-1"

# ADP VIN-list bucket (fleet_health_stack.py vin_bucket_name; account- and region-qualified).
# Source: platform-foundation/stacks/optional/fleet_health_stack.py:869 VIN_BUCKET env var.
_ADP_VIN_BUCKET = "adp-{stage}-engineering-vins-{account}-us-east-1"

# Thread-id pattern (ADP handler a946eb98:handler.py:201).
_THREAD_ID_RE = re.compile(r"^thread-[0-9a-f]{16}$")

# ADP repo root and venv for reconcile_thread_lake.py (pinned to f54ea20d).
# The script is extracted via git archive (no .git → _checkout() sets git_head=None; fine).
# Source: ADP f54ea20d:platform-foundation/scripts/reconcile_thread_lake.py.
_ADP_REPO = os.path.join(os.path.expanduser("~"), "automotive-data-platform-on-aws")
_ADP_RECONCILE_COMMIT = "f54ea20d"
_ADP_RECONCILE_SCRIPT_PATH = "platform-foundation/scripts/reconcile_thread_lake.py"
_ADP_RECONCILE_HANDLER_PATH = (
    "platform-foundation/source/lambda/digital_thread_writer/handler.py"
)
_ADP_PYTHON = os.path.join(
    _ADP_REPO, "platform-foundation", ".venv", "bin", "python"
)

# ── subprocess allowlist (documented; verified by test_f2g_subprocess_allowlist) ──
# Every external command this script may launch.  Tests that import this list
# verify no undocumented subprocess is added.
_SUBPROCESS_ALLOWLIST: tuple[str, ...] = (
    "git archive",                   # extract reconcile_thread_lake.py from ADP pinned commit
    "reconcile_thread_lake.py",      # ADP reconcile dry run (no --apply)
)

# Athena workgroup for expected-VINs query (brief §K9).
# Use named WorkGroup, not an OutputLocation built from a defaulted account (K9).
# Source: agents/tier2/runner.py:1566 — ATHENA_WORKGROUP env default = "cvx-{stage}-analytics".
_ATHENA_WORKGROUP = "cvx-{stage}-analytics"

# Poll parameters for GET /investigations/{id}
_POLL_MAX_RETRIES = 60    # up to 5 min at 5-s intervals
_POLL_SLEEP_S = 5


# ── GuardViolation ────────────────────────────────────────────────────────────

class GuardViolation(RuntimeError):
    """Raised by the botocore guard when a call is not on the part's allowlist."""


# ── per-part allowlists (keyed service_model.service_name + operation name) ──
# Brief §Guard: "per-operation allowlist keyed by service_model.service_name
# (not endpoint_prefix)".  CloudWatch's endpoint_prefix is 'monitoring';
# service_name is 'cloudwatch'.

_READ_OPS: frozenset[tuple[str, str]] = frozenset({
    ("sts", "GetCallerIdentity"),
    ("scheduler", "ListSchedules"),
    ("scheduler", "GetSchedule"),
    ("lambda", "GetFunction"),
    ("lambda", "ListEventSourceMappings"),
    ("glue", "GetTable"),
    ("sqs", "GetQueueAttributes"),
    ("sqs", "GetQueueUrl"),
    ("dynamodb", "Scan"),
    ("dynamodb", "Query"),
    ("dynamodb", "GetItem"),
    ("cognito-idp", "ListUserPools"),
    ("cognito-idp", "ListUsers"),
    ("athena", "StartQueryExecution"),
    ("athena", "GetQueryExecution"),
    ("athena", "GetQueryResults"),
    # CMS recall cache is stored in S3 (lifecycle_cache.recall_views_root()).
    # After-run reads the manifest + fleet-slice to verify CMS's recall list.
    # Source: CMS main 81a90d385 services/fleet_intelligence/lifecycle_cache.py:203-215.
    ("s3", "GetObject"),
})

# Extra ops the ``issue`` part needs beyond reads.
# Brief §Guard: allowlist keyed by service_model.service_name, per part.
_ISSUE_EXTRA_OPS: frozenset[tuple[str, str]] = frozenset({
    ("cloudformation", "DescribeStacks"),          # get AVX API URL
    ("cognito-idp", "ListUserPools"),              # resolve pool id
    ("cognito-idp", "ListUserPoolClients"),        # find password-auth client
    ("cognito-idp", "DescribeUserPoolClient"),     # verify ExplicitAuthFlows
    ("cognito-idp", "AdminCreateUser"),            # create temp engineer user
    ("cognito-idp", "AdminSetUserPassword"),       # set permanent password
    ("cognito-idp", "AdminAddUserToGroup"),        # add to product-engineer group
    ("cognito-idp", "InitiateAuth"),               # get id token
    ("cognito-idp", "AdminDeleteUser"),            # cleanup in issue (on error)
    ("cognito-idp", "ListUsers"),                  # confirm user absent on error
    ("s3", "HeadObject"),                          # SSE-KMS check on issued recall
})

# Extra ops the ``ground`` part needs beyond reads.
# Brief §Guard: ground_vehicle and book_fleet_service go via _http (AVX approve route
# → executor); the script itself only needs Cognito (fleet-operator user), a Lambda
# InvokeFunction (DMS stand-in), CloudFormation (CMS + AVX API URLs), and S3.
_GROUND_EXTRA_OPS: frozenset[tuple[str, str]] = frozenset({
    ("cloudformation", "DescribeStacks"),          # get AVX API URL + CMS API URL
    ("cognito-idp", "ListUserPools"),              # resolve pool id
    ("cognito-idp", "ListUserPoolClients"),        # find password-auth client
    ("cognito-idp", "DescribeUserPoolClient"),     # verify ExplicitAuthFlows
    ("cognito-idp", "AdminCreateUser"),            # create temp fleet-operator user
    ("cognito-idp", "AdminSetUserPassword"),       # set permanent password
    ("cognito-idp", "AdminAddUserToGroup"),        # add to fleet-operator group
    ("cognito-idp", "InitiateAuth"),               # get id token
    ("cognito-idp", "AdminDeleteUser"),            # cleanup fleet-operator user on error
    ("cognito-idp", "ListUsers"),                  # confirm user absent on error
    ("lambda", "Invoke"),                          # invoke DMS stand-in (botocore op name)
})

_PART_ALLOWLISTS: dict[str, frozenset[tuple[str, str]]] = {
    "pre": _READ_OPS,
    "issue": _READ_OPS | _ISSUE_EXTRA_OPS,
    # after-run: reads + S3 HeadObject for thread-lake flat-key objects.
    # F2g fix: after-run checks each thread version's flat-key lake object via HeadObject.
    "after-run": _READ_OPS | frozenset({
        ("s3", "HeadObject"),  # check curated/engineering/digital_thread/<id>.v<n>.json exists
    }),
    # cleanup: reads + deletes driven by the manifest.
    # Brief §cleanup: "delete everything the manifest names", "confirm each deletion by a read",
    # "residue scans page fully" (DDB Scan), Cognito AdminDeleteUser/ListUsers.
    # F2g fix: cleanup scans S3 thread-lake prefix with list_objects_v2 before and after deletion.
    "cleanup": _READ_OPS | frozenset({
        ("dynamodb", "DeleteItem"),    # delete investigation, findings, thread, issued rows
        ("s3", "DeleteObject"),        # delete issued-recall S3 object and thread-lake objects
        ("s3", "HeadObject"),          # confirm S3 object absent after delete
        ("s3", "ListObjectsV2"),       # scan thread-lake flat prefix and residue check
        ("cognito-idp", "AdminDeleteUser"),  # delete temp users
        ("cloudformation", "DescribeStacks"),  # get AVX API URL for findings read-back
    }),
    "after-cleanup": _READ_OPS,
    "ground": _READ_OPS | _GROUND_EXTRA_OPS,
}


def _make_guarded_bcsession(allowed: frozenset[tuple[str, str]]) -> Any:
    """Return a botocore Session that raises GuardViolation for ops not in *allowed*.

    The event hook fires on ``before-parameter-build.*.*``.  The service name
    is read from ``model.service_model.service_name`` (not from the event name,
    which carries the endpoint prefix — CloudWatch's prefix is ``monitoring``
    but its service_name is ``cloudwatch``).  Brief §Guard.
    """
    import botocore.session as _bcs

    bc = _bcs.Session()

    def _guard(params: dict, model: Any, **_kw: Any) -> None:
        svc = model.service_model.service_name
        op = model.name
        if (svc, op) not in allowed:
            raise GuardViolation(
                f"{svc}.{op} is not on this part's allowlist "
                f"(allowlist size {len(allowed)})"
            )

    bc.get_component("event_emitter").register(
        "before-parameter-build.*.*", _guard, unique_id="fi-loop-guard"
    )
    return bc


# ── singleton guarded session ─────────────────────────────────────────────────

_SESSION: Any = None


def _init_session(part: str) -> None:
    """Initialize the singleton guarded session for *part* (idempotent).

    Must be called before the first _client() call.  The session is tied to
    this process's lifetime; tests reset ``_SESSION = None`` between runs.
    Brief §Guard: one session for the whole run.
    """
    global _SESSION
    if _SESSION is not None:
        return
    import boto3  # noqa: PLC0415
    allowed = _PART_ALLOWLISTS.get(part, frozenset())
    _SESSION = boto3.Session(botocore_session=_make_guarded_bcsession(allowed))


def _client(service: str, region: str, **kwargs: Any) -> Any:
    """Return a boto3 client through the singleton guarded session.

    Raises RuntimeError if _init_session has not been called.  Brief §Guard:
    every client must go through the session, residue readers included.
    """
    if _SESSION is None:
        raise RuntimeError(
            "_client called before _init_session; call _init_session(part) first"
        )
    return _SESSION.client(service, region_name=region, **kwargs)


# ── Results ───────────────────────────────────────────────────────────────────


class Results:
    """PASS / FAIL / SKIP tracker; drives exit code.  Brief §Exit."""

    def __init__(self) -> None:
        self.rows: list[tuple[str, str, str]] = []

    def skip(self, label: str, reason: str) -> None:
        self.rows.append((label, "SKIP", reason))
        print(f"  [SKIP] {label}: {reason}", flush=True)

    @property
    def has_any_fail(self) -> bool:
        return any(r[1] == "FAIL" for r in self.rows)

    @property
    def has_any_skip(self) -> bool:
        return any(r[1] == "SKIP" for r in self.rows)

    def summary(self, run_id: str) -> None:
        print("\nSummary")
        for label, status, detail in self.rows:
            print(f"  {status:4}  {label}{(': ' + detail[:120]) if detail else ''}")
        n_p = sum(1 for _, s, _ in self.rows if s == "PASS")
        n_f = sum(1 for _, s, _ in self.rows if s == "FAIL")
        n_s = sum(1 for _, s, _ in self.rows if s == "SKIP")
        print(f"\n  pass={n_p}  fail={n_f}  skip={n_s}  run_id={run_id}")


def check(
    res: Results,
    label: str,
    ok: bool,
    detail: str = "",
    *,
    on_fail: str | None = None,
) -> bool:
    """Record PASS or FAIL; return *ok*.

    On a FAIL, *on_fail* is shown instead of *detail* when provided so that
    failure rows carry actionable text (brief §check).  A PASS row must read
    as a pass — do not put failure wording in *detail*.
    """
    status = "PASS" if ok else "FAIL"
    shown = detail if (ok or on_fail is None) else on_fail
    res.rows.append((label, status, shown))
    print(f"  [{status}] {label}{(': ' + shown) if shown else ''}", flush=True)
    return ok


# ── manifest (0600 local file) ────────────────────────────────────────────────

def _manifest_path(run_id: str) -> str:
    """Absolute path for this run's manifest file (brief §Manifest)."""
    return _MANIFEST_TEMPLATE.format(manifest_dir=_MANIFEST_DIR, run_id=run_id)


def _write_manifest(path: str, data: dict) -> None:
    """Write *data* atomically to *path*, 0600, in a durable 0700 dir (S2 fix).

    Steps:
    1. Create the parent directory with mode 0700 if absent.
    2. Open a uniquely-named temp file with O_EXCL|O_CREAT at mode 0600.
    3. Write JSON; if the write fails, unlink the temp file.
    4. os.replace for an atomic rename to the final path.
    """
    parent = os.path.dirname(path)
    os.makedirs(parent, exist_ok=True)
    os.chmod(parent, 0o700)
    tmp = path + ".tmp." + secrets.token_hex(4)
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(data, fh, indent=2, default=str)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    os.replace(tmp, path)


def _read_manifest(path: str) -> dict:
    """Read and return the manifest at *path*.  Fails closed on any error."""
    with open(path) as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        raise ValueError(f"manifest at {path} is not a JSON object")
    return data


# ── signal handling ───────────────────────────────────────────────────────────
# Brief §Signals: SIGINT/SIGTERM/SIGHUP each raise SystemExit(143); install
# from __main__; _defer_signals_for_restore holds them during cleanup.

_SIGNAL_STATE: dict[str, Any] = {}


def _install_signal_handlers() -> None:
    """SIGINT, SIGTERM, SIGHUP each raise SystemExit(143) once (brief §Signals)."""
    _SIGNAL_STATE["raised"] = False

    def _exit_once(signum: int, frame: object) -> None:
        if not _SIGNAL_STATE["raised"]:
            _SIGNAL_STATE["raised"] = True
            raise SystemExit(143)

    _sig.signal(_sig.SIGINT, _exit_once)
    _sig.signal(_sig.SIGTERM, _exit_once)
    _sig.signal(_sig.SIGHUP, _exit_once)


@contextlib.contextmanager
def _defer_signals_for_restore() -> Iterator[None]:
    """Hold SIGINT/SIGTERM/SIGHUP during cleanup; re-deliver after (brief §Signals)."""
    _pending: list[int] = []

    def _record(sig: int, _frame: object) -> None:
        _pending.append(sig)

    old_int = _sig.signal(_sig.SIGINT, _record)
    old_term = _sig.signal(_sig.SIGTERM, _record)
    old_hup = _sig.signal(_sig.SIGHUP, _record)
    try:
        yield
    finally:
        _sig.signal(_sig.SIGINT, old_int)
        _sig.signal(_sig.SIGTERM, old_term)
        _sig.signal(_sig.SIGHUP, old_hup)
        for s in _pending:
            os.kill(os.getpid(), s)


# ── window / schedule guard ───────────────────────────────────────────────────
# Brief §Windows: never 01:45–03:30Z; ±45 min of any Tier 2 or DMS stand-in
# cron; unknown zone or unparseable expression refuses.

def _utc_now() -> dt.datetime:
    """The clock window checks read (tests replace this)."""
    return dt.datetime.now(dt.timezone.utc)


def _cron_minute_of_day(expr: str) -> int:
    """Minute of day of ``cron(M H * * ? *)`` with plain numbers; anything else raises."""
    m = re.fullmatch(r"cron\((\d{1,2}) (\d{1,2}) \* \* \? \*\)", expr.strip())
    if not m or int(m.group(1)) > 59 or int(m.group(2)) > 23:
        raise ValueError(f"unsupported schedule expression {expr!r}")
    return int(m.group(2)) * 60 + int(m.group(1))


def _discover_schedule_group(
    scheduler: Any,
    name: str,
) -> str:
    """Return the EventBridge Scheduler group name for *name* via list_schedules.

    Pages through all schedules (no group filter) to find the one whose Name
    matches *name* exactly.  Raises ValueError if not found.
    Brief §Schedules / K8: don't assume 'default'.
    """
    kwargs: dict[str, Any] = {}
    while True:
        page = scheduler.list_schedules(**kwargs)
        for s in page.get("Schedules", []):
            if s.get("Name") == name:
                return s["GroupName"]
        token = page.get("NextToken")
        if not token:
            break
        kwargs["NextToken"] = token
    raise ValueError(f"schedule {name!r} not found in any group via list_schedules")


def _schedule_guard(
    scheduler: Any,
    names: list[tuple[str, str, str]],
    stage: str,
    now: dt.datetime,
) -> str | None:
    """Return a reason to refuse if *now* is within ±45 min of any schedule, else None.

    *names* is a list of (name_template, _ignored_group, region) triples; ``{stage}``
    and ``{region}`` are substituted before calling list_schedules + get_schedule to
    discover the real group (K8: don't assume 'default').

    An unparseable expression, an unknown timezone, a rate() expression, or a read
    error raises (fail closed).  Brief §Windows / K8.
    """
    n = now.hour * 60 + now.minute
    _UTC_ZONES = {"UTC", "Etc/UTC"}
    for tmpl, _ignored_group, _region in names:
        name = tmpl.format(stage=stage, region=_region)
        try:
            group = _discover_schedule_group(scheduler, name)
        except ValueError as exc:
            return str(exc)
        sc = scheduler.get_schedule(Name=name, GroupName=group)
        tz = sc.get("ScheduleExpressionTimezone") or "UTC"
        if tz not in _UTC_ZONES:
            return f"{name} uses timezone {tz!r}; only UTC/Etc/UTC are supported"
        try:
            mnt = _cron_minute_of_day(sc["ScheduleExpression"])
        except ValueError as exc:
            # K8: rate() and other unsupported expressions are a refusal, not an
            # exception that escapes the guard (brief §Windows).
            return (
                f"{name} has unsupported schedule expression "
                f"{sc['ScheduleExpression']!r}: {exc}; "
                f"only cron(M H * * ? *) with plain numbers is accepted"
            )
        gap = min((n - mnt) % 1440, (mnt - n) % 1440)
        if gap < _SCHED_GUARD_MIN:
            return (
                f"{name} ({sc['ScheduleExpression']}) is {gap} min away; "
                f"needs {_SCHED_GUARD_MIN}"
            )
    return None


def _maint_window_guard(now: dt.datetime) -> str | None:
    """Return a reason if *now* is in the 01:45–03:30 UTC maintenance window."""
    n = now.hour * 60 + now.minute
    if _MAINT_START_MIN <= n < _MAINT_END_MIN:
        return (
            f"current time {now.strftime('%H:%M')} UTC is in the maintenance "
            f"window {_MAINT_START_MIN // 60:02d}:{_MAINT_START_MIN % 60:02d}–"
            f"{_MAINT_END_MIN // 60:02d}:{_MAINT_END_MIN % 60:02d} UTC"
        )
    return None


# ── Lambda code-download helper (patchable in tests) ─────────────────────────

def _download_code_zip(url: str) -> bytes:
    """Download a Lambda code zip from a presigned URL and return the raw bytes.

    Abstracted as a separate function so tests can patch it to avoid real HTTP
    or moto S3 presigned-URL issues.  Brief §Tier 2 Lambda code check.
    """
    import urllib.request  # noqa: PLC0415
    return urllib.request.urlopen(url, timeout=60).read()


# ── pre-part helpers ──────────────────────────────────────────────────────────

def _check_tier2_code(res: Results, lam: Any, fn_name: str) -> bool:
    """Download the Tier 2 Lambda zip and verify it carries 9.3's code.

    Checks (brief §Preconditions + user rule 2026-10-20):
      1. agents/fi_quality/readers.py has no ``rcr.name``
      2. agents/fi_recall_warranty/sql/select_recalls.sql joins
         recall_campaigns_issued

    Returns True only when BOTH checks pass.  A failed download is FAIL.
    """
    try:
        r = lam.get_function(FunctionName=fn_name)
    except Exception as exc:  # noqa: BLE001
        check(res, "pre-tier2-code", False,
              on_fail=f"GetFunction({fn_name!r}) failed: {type(exc).__name__}: {exc}")
        return False

    url = r.get("Code", {}).get("Location", "")
    if not url:
        check(res, "pre-tier2-code", False, on_fail="GetFunction returned no Code.Location")
        return False

    try:
        raw = _download_code_zip(url)
    except Exception as exc:  # noqa: BLE001
        check(res, "pre-tier2-code", False,
              on_fail=f"code download failed: {type(exc).__name__}: {exc}")
        return False

    try:
        zf = zipfile.ZipFile(io.BytesIO(raw))
    except Exception as exc:  # noqa: BLE001
        check(res, "pre-tier2-code", False,
              on_fail=f"code zip is not a valid ZIP: {type(exc).__name__}: {exc}")
        return False

    names = zf.namelist()

    # Check 1: fi_quality/readers.py has no rcr.name
    readers_path = "agents/fi_quality/readers.py"
    if readers_path not in names:
        check(res, "pre-tier2-code-readers", False,
              on_fail=f"{readers_path!r} not found in Lambda zip (files: {names[:5]})")
        return False
    readers_text = zf.read(readers_path).decode("utf-8", "replace")
    no_rcr = _READERS_FORBIDDEN not in readers_text
    check(res, "pre-tier2-code-readers", no_rcr,
          f"{readers_path!r} has no {_READERS_FORBIDDEN!r} (9.3 code present)",
          on_fail=f"{readers_path!r} still contains {_READERS_FORBIDDEN!r}: "
                  f"Lambda not yet deployed with 9.3's code (user rule 2026-10-20)")

    # Check 2: select_recalls.sql joins recall_campaigns_issued
    sql_path = "agents/fi_recall_warranty/sql/select_recalls.sql"
    if sql_path not in names:
        check(res, "pre-tier2-code-sql", False,
              on_fail=f"{sql_path!r} not found in Lambda zip (files: {names[:5]})")
        return False
    sql_text = zf.read(sql_path).decode("utf-8", "replace")
    has_join = _SELECT_RECALLS_REQUIRED in sql_text
    check(res, "pre-tier2-code-sql", has_join,
          f"{sql_path!r} joins {_SELECT_RECALLS_REQUIRED!r} (9.3 code present)",
          on_fail=f"{sql_path!r} does not join {_SELECT_RECALLS_REQUIRED!r}: "
                  f"Lambda not yet deployed with 9.3's code")

    return no_rcr and has_join


def _check_recall_table_exists(res: Results, glue: Any, stage: str) -> bool:
    """Check the recall_campaigns_issued Glue table exists (brief §Preconditions)."""
    db = _GLUE_ENGINEERING_DB.format(stage=stage)
    tbl = _RECALL_CAMPAIGNS_ISSUED_TABLE
    try:
        glue.get_table(DatabaseName=db, Name=tbl)
        check(res, "pre-recall-table", True,
              f"{db}.{tbl} exists in Glue")
        return True
    except Exception as exc:  # noqa: BLE001
        code = getattr(exc, "response", {}).get("Error", {}).get("Code", "")
        check(res, "pre-recall-table", False,
              on_fail=f"{db}.{tbl} not found in Glue ({code or type(exc).__name__}): "
                      f"deploy 9.3 ADP FleetHealthStack first")
        return False


def _decode_presto_view_text(view_text: str) -> str:
    """Decode a Glue ``ViewOriginalText`` for a Presto/Athena VIRTUAL_VIEW.

    Live Glue returns the SQL wrapped as::

        /* Presto View: <base64-of-JSON> */

    The JSON object carries ``originalSql`` (the real SQL).  This function
    decodes strictly — any step failing raises ``ValueError`` naming the step.
    Never falls back to searching the raw text.

    F6a fix (brief §Fix(a)): the old code searched the raw ``ViewOriginalText``,
    which never contains the SQL keyword — the SQL is inside the base64 payload.
    """
    import base64 as _b64  # noqa: PLC0415
    prefix = "/* Presto View: "
    suffix = " */"
    raw = view_text.strip()
    if not (raw.startswith(prefix) and raw.endswith(suffix)):
        snippet = raw[:80] if len(raw) > 80 else raw
        raise ValueError(
            f"ViewOriginalText does not match '/* Presto View: <base64> */' "
            f"envelope; got: {snippet!r}"
        )
    b64_part = raw[len(prefix):-len(suffix)]
    try:
        decoded_bytes = _b64.b64decode(b64_part, validate=True)
    except Exception as exc:  # noqa: BLE001
        raise ValueError(
            f"base64 decode failed: {type(exc).__name__}: {exc}; "
            f"first 80 chars of b64 payload: {b64_part[:80]!r}"
        ) from exc
    try:
        obj = json.loads(decoded_bytes)
    except ValueError as exc:
        raise ValueError(
            f"base64 payload is not valid JSON: {exc}; "
            f"first 80 decoded chars: {decoded_bytes[:80]!r}"
        ) from exc
    if not isinstance(obj, dict):
        raise ValueError(
            f"decoded JSON is not an object; got type {type(obj).__name__}"
        )
    sql = obj.get("originalSql")
    if not isinstance(sql, str):
        raise ValueError(
            f"decoded JSON has no 'originalSql' string key; "
            f"top-level keys: {list(obj)[:10]}"
        )
    return sql


def _check_view_sql_unions_issued(res: Results, glue: Any, stage: str) -> bool:
    """Check BOTH Glue views decode and reference stage-qualified recall_campaigns_issued.

    Both ``recall_campaigns_received`` and ``recall_vehicle_status`` are
    VIRTUAL_VIEWs; their ``ViewOriginalText`` is ``/* Presto View: <base64> */``.
    Each decoded ``originalSql`` must reference
    ``adp_{stage}_engineering.recall_campaigns_issued`` (word-bounded) so no
    other stage's database matches.

    F6a fix (brief §Fix(a)): replaces the raw-text search that never found the
    keyword (it is inside the base64 payload), and adds the status-view check.
    """
    import re as _re  # noqa: PLC0415
    db = _GLUE_FLEET_RECALL_DB.format(stage=stage)
    # Stage-qualified table name to check (word-bounded, break a4 guard).
    qual_table = f"adp_{stage}_engineering.{_RECALL_CAMPAIGNS_ISSUED_TABLE}"
    qual_pattern = _re.compile(r"\b" + _re.escape(qual_table) + r"\b")
    all_ok = True
    for tbl, label in [
        (_RECALL_CAMPAIGNS_RECEIVED_TABLE, "pre-view-sql-received"),
        ("recall_vehicle_status", "pre-view-sql-status"),
    ]:
        try:
            r = glue.get_table(DatabaseName=db, Name=tbl)
        except Exception as exc:  # noqa: BLE001
            check(res, label, False,
                  on_fail=f"{db}.{tbl} not found in Glue: {type(exc).__name__}: {exc}")
            all_ok = False
            continue
        view_text = r.get("Table", {}).get("ViewOriginalText", "")
        try:
            sql = _decode_presto_view_text(view_text)
        except ValueError as exc:
            check(res, label, False,
                  on_fail=f"{db}.{tbl} ViewOriginalText decode failed: {exc}")
            all_ok = False
            continue
        has_issued = bool(qual_pattern.search(sql))
        check(res, label, has_issued,
              f"{db}.{tbl} decoded SQL references {qual_table!r}",
              on_fail=f"{db}.{tbl} decoded SQL does not reference {qual_table!r} "
                      f"(word-bounded); first 200 chars: {sql[:200]!r}")
        if not has_issued:
            all_ok = False
    return all_ok


def _check_esm_enabled(res: Results, lam: Any, stage: str, region: str) -> bool:
    """Check the investigation ESM is Enabled (brief §Preconditions).

    g93lv-facts.md §C5: as of 2026-10-20 the ESM is Disabled; this check
    will FAIL until the ESM is re-enabled.  No POST may be made otherwise
    (user rule 2026-10-20).
    """
    fn = _TIER2_FN.format(stage=stage, region=region)
    q_name = _INV_QUEUE.format(stage=stage)
    esms: list[dict] = []
    kwargs: dict[str, Any] = {"FunctionName": fn}
    while True:
        page = lam.list_event_source_mappings(**kwargs)
        esms += [
            m for m in page.get("EventSourceMappings", [])
            if (m.get("EventSourceArn") or "").endswith(f":{q_name}")
        ]
        if not page.get("NextMarker"):
            break
        kwargs["Marker"] = page["NextMarker"]

    if not esms:
        check(res, "pre-esm-enabled", False,
              on_fail=f"no ESM found on {fn} for queue {q_name}")
        return False
    states = [m.get("State", "?") for m in esms]
    all_enabled = all(s == "Enabled" for s in states)
    check(res, "pre-esm-enabled", all_enabled,
          f"ESM State=Enabled ({states})",
          on_fail=f"ESM State={states}; must be Enabled before any investigation POST "
                  f"(user rule 2026-10-20); re-enable in Lambda console or wait for deploy")
    return all_enabled


def _check_dlq_empty(res: Results, sqs: Any, stage: str) -> bool:
    """Check the investigation DLQ is empty (brief §Preconditions).

    Reads RedrivePolicy from the investigation queue to find the DLQ ARN,
    then checks its approximate message count.  Brief §Preconditions.
    """
    q_name = _INV_QUEUE.format(stage=stage)
    try:
        q_url = sqs.get_queue_url(QueueName=q_name)["QueueUrl"]
    except Exception as exc:  # noqa: BLE001
        check(res, "pre-dlq-empty", False,
              on_fail=f"GetQueueUrl({q_name!r}) failed: {type(exc).__name__}: {exc}")
        return False

    try:
        attrs = sqs.get_queue_attributes(
            QueueUrl=q_url,
            AttributeNames=["RedrivePolicy"],
        )["Attributes"]
    except Exception as exc:  # noqa: BLE001
        check(res, "pre-dlq-empty", False,
              on_fail=f"GetQueueAttributes({q_name!r}) failed: {type(exc).__name__}: {exc}")
        return False

    rp = attrs.get("RedrivePolicy")
    if not rp:
        check(res, "pre-dlq-empty", False,
              on_fail=f"{q_name} has no redrive policy; the investigation queue must "
                      f"have a DLQ (brief §Preconditions; K4)")
        return False

    try:
        dlq_arn = json.loads(rp).get("deadLetterTargetArn", "")
    except (ValueError, KeyError) as exc:
        check(res, "pre-dlq-empty", False,
              on_fail=f"unparseable RedrivePolicy {rp!r}: {exc}")
        return False

    if not dlq_arn:
        check(res, "pre-dlq-empty", False,
              on_fail=f"{q_name} RedrivePolicy has no deadLetterTargetArn; "
                      f"investigation queue must have a DLQ (brief §Preconditions; K4)")
        return False

    # The DLQ name is the last component of the ARN
    dlq_name = dlq_arn.rsplit(":", 1)[-1]
    try:
        dlq_url = sqs.get_queue_url(QueueName=dlq_name)["QueueUrl"]
        dlq_attrs = sqs.get_queue_attributes(
            QueueUrl=dlq_url,
            AttributeNames=["ApproximateNumberOfMessages"],
        )["Attributes"]
    except Exception as exc:  # noqa: BLE001
        check(res, "pre-dlq-empty", False,
              on_fail=f"GetQueueAttributes(DLQ {dlq_name!r}) failed: "
                      f"{type(exc).__name__}: {exc}")
        return False

    count = int(dlq_attrs.get("ApproximateNumberOfMessages", 0))
    ok = count == 0
    check(res, "pre-dlq-empty", ok,
          f"DLQ {dlq_name} ApproximateNumberOfMessages={count} (empty)",
          on_fail=f"DLQ {dlq_name} has {count} message(s); drain before proceeding")
    return ok


def _ddb_val(v: Any) -> Any:
    """Unwrap a DynamoDB AttributeValue wrapper ``{"S": "..."}``, etc."""
    if isinstance(v, dict) and len(v) == 1:
        k, inner = next(iter(v.items()))
        if k in ("S", "N", "BOOL", "NULL", "L", "M", "SS", "NS", "BS"):
            return inner
    return v


def _ddb_str(item: dict, key: str) -> str:
    """Return a string field from a DDB low-level item (unwraps AttributeValue)."""
    raw = item.get(key)
    if raw is None:
        return ""
    val = _ddb_val(raw) if isinstance(raw, dict) else raw
    return str(val) if val is not None else ""


def _ddb_bool(item: dict, key: str) -> Any:
    """Return a boolean field from a DDB low-level item; None if absent."""
    raw = item.get(key)
    if raw is None:
        return None
    return _ddb_val(raw) if isinstance(raw, dict) else raw


def _ddb_list(item: dict, key: str) -> list:
    """Return a list field from a DDB low-level item (unwraps L wrapper)."""
    raw = item.get(key) or {}
    val = _ddb_val(raw) if isinstance(raw, dict) else raw
    return val if isinstance(val, list) else []


def _corpus_fm_to_dict(item: dict) -> dict:
    """Convert a DDB low-level corpus row into a plain dict for _rules_match_failure_mode.

    Extracts: failure_mode_id, cause_dimension, component_id, do_not_drive,
    symptom_codes (list of str), options (list of plain dicts).
    """
    symptom_codes_raw = _ddb_list(item, "symptom_codes")
    symptom_codes = [
        str(_ddb_val(c)) for c in symptom_codes_raw
        if isinstance(_ddb_val(c), str)
    ]
    options_raw = _ddb_list(item, "options")
    options: list[dict] = []
    for opt_entry in options_raw:
        opt_m = _ddb_val(opt_entry) if isinstance(opt_entry, dict) else {}
        if not isinstance(opt_m, dict):
            continue
        plain: dict = {}
        for k in ("remedy_option_id", "remedy_type"):
            plain[k] = _ddb_str(opt_m, k)
        ltw_raw = opt_m.get("lead_time_weeks") or {}
        ltw_val = _ddb_val(ltw_raw) if isinstance(ltw_raw, dict) else ltw_raw
        try:
            plain["lead_time_weeks"] = float(ltw_val) if ltw_val is not None else 0.0
        except (ValueError, TypeError):
            plain["lead_time_weeks"] = 0.0
        options.append(plain)
    dnd_raw = item.get("do_not_drive")
    dnd = _ddb_val(dnd_raw) if isinstance(dnd_raw, dict) else dnd_raw
    return {
        "failure_mode_id": _ddb_str(item, "failure_mode_id"),
        "cause_dimension":  _ddb_str(item, "cause_dimension"),
        "component_id":     _ddb_str(item, "component_id"),
        "component_name":   _ddb_str(item, "component_name"),
        "do_not_drive":     dnd,
        "symptom_codes":    symptom_codes,
        "options":          options,
    }


def _tiered_fm_pool(
    corpus_fms: list[dict],
    code_family: str,
) -> "tuple[list[dict], list[dict]]":
    """Partition *corpus_fms* into (exact_tier, prefix_tier) for *code_family*.

    Mirrors the agent's agents.fi_quality.fallback._match_failure_mode loop:
    each FM is scanned over ALL its symptom_codes before being classified.
    An FM enters the exact tier if ANY code equals code_family (scanning
    continues past prefix hits to find an exact one); otherwise it enters the
    prefix tier if ANY code starts with code_family OR code[:4] == cf[:4].

    W1/S4 fix: the old loop broke on the first prefix hit, so an FM whose
    exact code appeared after a prefix code was mis-classified.  Not breaking
    on prefix matches fixes the divergence between the script and the agent.

    Returns (exact_pool, prefix_pool).  Caller chooses ``exact or prefix``.
    Empty code_family → ([], []).
    """
    if not code_family:
        return [], []
    prefix4 = code_family[:4]
    exact: list[dict] = []
    prefix: list[dict] = []
    for fm in corpus_fms:
        matched_exact = False
        matched_prefix = False
        for code in fm.get("symptom_codes") or []:
            if not isinstance(code, str):
                continue
            if code == code_family:
                matched_exact = True
                break          # exact match found — no need to keep scanning
            if code.startswith(code_family) or code[:4] == prefix4:
                matched_prefix = True  # keep scanning — an exact code may follow
        if matched_exact:
            exact.append(fm)
        elif matched_prefix:
            prefix.append(fm)
    return exact, prefix


def _rules_match_failure_mode(
    corpus_fms: list[dict],
    code_family: str,
) -> "dict | None":
    """Deterministic rules-path FM selection — mirrors agents.fi_quality.fallback._match_failure_mode(fms, cf, {}).

    Implements the same logic as the deployed rules fallback so ``pre`` can
    predict what failure mode the investigation will choose when it runs with
    ``FI_INVESTIGATION_MODEL_ENABLED=false`` (the live Tier 2 config).

    Does NOT import agent code (brief §Fix(b): implement in the script).
    Pinned with test_b_rules_match_equals_fallback (brief §Fix(b)).

    Ranking: exact match > prefix match (code.startswith(cf) or code[:4]==cf[:4]);
    within a tier: sorted by failure_mode_id for stability.
    Empty code_family → None.

    W1/S4 fix: delegates to _tiered_fm_pool so both choice and pool use identical
    tier classification logic.
    """
    exact, prefix = _tiered_fm_pool(corpus_fms, code_family)
    best = exact or prefix
    if not best:
        return None
    return sorted(best, key=lambda fm: (9999, fm.get("failure_mode_id", "")))[0]


def _is_safe_fm(fm: dict) -> bool:
    """Return True iff ``fm`` meets the safe-failure-mode rule (brief §Fix(b)).

    Safe rule: cause_dimension == "supplier_lot", do_not_drive is exactly False,
    and has an option with remedy_type == "hardware_recall" and lead_time_weeks > 0.
    """
    if fm.get("cause_dimension") != "supplier_lot":
        return False
    if fm.get("do_not_drive") is not False:  # absent counts as not-False
        return False
    for opt in fm.get("options") or []:
        if opt.get("remedy_type") == "hardware_recall":
            try:
                if float(opt.get("lead_time_weeks", 0)) > 0:
                    return True
            except (ValueError, TypeError):
                pass
    return False


def _hw_recall_opt_id(fm: dict) -> str:
    """Return the remedy_option_id of the first hardware_recall option with lead_time_weeks>0."""
    for opt in fm.get("options") or []:
        if opt.get("remedy_type") == "hardware_recall":
            try:
                if float(opt.get("lead_time_weeks", 0)) > 0:
                    return opt.get("remedy_option_id", "")
            except (ValueError, TypeError):
                pass
    return ""


def _safe_failure_modes_for_pool(corpus_fms: list[dict], code_family: str) -> dict:
    """Return {fm_id: hw_recall_opt_id} for every safe FM in the match pool for code_family.

    The match pool is the set of FMs that _rules_match_failure_mode would consider
    (exact-match tier OR prefix-match tier, whichever is non-empty).
    Only FMs passing the safe rule are included.
    Used for manifest ``safe_failure_modes``.  Brief §Fix(b).

    W1/S4 fix: delegates to _tiered_fm_pool so the pool and the choice use
    identical tier classification — they agree by construction.
    """
    exact, prefix = _tiered_fm_pool(corpus_fms, code_family)
    pool = exact or prefix
    return {
        fm["failure_mode_id"]: _hw_recall_opt_id(fm)
        for fm in pool
        if _is_safe_fm(fm)
    }


def _pick_pattern(
    res: Results, ddb: Any, stage: str, athena: Any = None
) -> "tuple[dict, str, str, str, dict] | tuple[None, None, None, None, None]":
    """Pick a fault-pattern candidate satisfying the four pick rules.

    F6a fix (brief §Fix(b)): no longer reads ``failure_mode_id`` from the pattern
    row (the live ADP producer does not write it).  Uses ``code_family`` + corpus
    scan + _rules_match_failure_mode to determine the predicted failure mode.

    Pick rules:
      1. cohort_dimension == "supplier_lot" and cohort_value matches _LOT_RE.
      2. pattern_id == pattern_group_id (group-top row only).
      3. _rules_match_failure_mode(corpus_fms, code_family) returns a safe FM.
      4. (Athena branch) managed VINs on the chosen FM's component_id > 0 for the lot
         (GROUP BY bc.component_id; brief §Fix(b) rule 4).

    Returns (candidate_row, pattern_id, cause_value, predicted_fm_id, safe_fms)
    on success, (None, None, None, None, None) on failure.
    Fails with per-candidate skip reasons when nothing qualifies.
    Candidates are processed in deterministic order (sorted by pattern_id).
    """
    table = _FAULT_PATTERNS_TABLE.format(stage=stage)
    corpus_table = _ENGINEERING_CORPUS_TABLE.format(stage=stage)

    # 0. Scan the full corpus to build FM list for _rules_match_failure_mode.
    try:
        corpus_items: list[dict] = []
        kwargs: dict[str, Any] = {"TableName": corpus_table}
        while True:
            page = ddb.scan(**kwargs)
            corpus_items.extend(page.get("Items", []))
            if not page.get("LastEvaluatedKey"):
                break
            kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]
    except Exception as exc:  # noqa: BLE001
        check(res, "pre-pattern-pick", False,
              on_fail=f"scan {corpus_table!r} failed: {type(exc).__name__}: {exc}")
        return None, None, None, None, None

    corpus_fms = [_corpus_fm_to_dict(it) for it in corpus_items]

    # 1. Scan fault-patterns table.
    try:
        items: list[dict] = []
        kwargs2: dict[str, Any] = {"TableName": table}
        while True:
            page2 = ddb.scan(**kwargs2)
            items.extend(page2.get("Items", []))
            if not page2.get("LastEvaluatedKey"):
                break
            kwargs2["ExclusiveStartKey"] = page2["LastEvaluatedKey"]
    except Exception as exc:  # noqa: BLE001
        check(res, "pre-pattern-pick", False,
              on_fail=f"scan {table!r} failed: {type(exc).__name__}: {exc}")
        return None, None, None, None, None

    if not items:
        check(res, "pre-pattern-pick", False,
              on_fail=f"{table!r} is empty; no pattern to pick")
        return None, None, None, None, None

    # Sort for deterministic pick order (brief §Fix(b): "sort candidates by pattern_id").
    # S8 fix: record a skip reason for supplier_lot rows whose cohort_value fails _LOT_RE
    # so the FAIL message lists every supplier_lot row, not just the ones that passed rule 1.
    supplier_lot_items = []
    skip_reasons: dict[str, str] = {}  # pattern_id → reason
    for _it in items:
        _pid = _ddb_str(_it, "pattern_id")
        _cdim = _ddb_str(_it, "cohort_dimension")
        _cval = _ddb_str(_it, "cohort_value")
        if _cdim != "supplier_lot":
            continue
        if not _LOT_RE.fullmatch(_cval):
            skip_reasons[_pid] = (
                f"cohort_value {_cval!r} does not match lot regex {_LOT_RE.pattern!r}"
            )
            continue
        supplier_lot_items.append(_it)
    supplier_lot_items.sort(key=lambda it: _ddb_str(it, "pattern_id"))

    candidate: dict | None = None
    chosen_fm: dict | None = None
    chosen_cause_value = ""

    for it in supplier_lot_items:
        pid = _ddb_str(it, "pattern_id")
        pgid = _ddb_str(it, "pattern_group_id")
        cval = _ddb_str(it, "cohort_value")
        cf = _ddb_str(it, "code_family")

        # Rule 2: group-top row only (brief §Fix(b) rule 2).
        if pid != pgid:
            skip_reasons[pid] = "not the group-top row (pattern_id != pattern_group_id)"
            continue

        # Rule 3: rules-path choice is a safe failure mode.
        fm = _rules_match_failure_mode(corpus_fms, cf)
        if fm is None:
            skip_reasons[pid] = f"no FM matches code_family {cf!r}"
            continue
        if not _is_safe_fm(fm):
            skip_reasons[pid] = (
                f"rules choice {fm['failure_mode_id']!r} is unsafe "
                f"(cause_dimension={fm.get('cause_dimension')!r} "
                f"do_not_drive={fm.get('do_not_drive')!r})"
            )
            continue

        # Rule 4: lot has managed VINs on the chosen FM's component_id.
        comp_id = fm.get("component_id", "")
        if athena is not None:
            try:
                eng_db = _GLUE_ENGINEERING_DB.format(stage=stage)
                fleet_db = f"adp_{stage}_fleet_recall"
                # GROUP BY bc.component_id; brief §Fix(b) rule 4.
                comp_sql = (
                    f"SELECT bc.component_id, COUNT(DISTINCT bc.vin) AS cnt "
                    f"FROM \"{eng_db}\".vehicle_build_components bc "
                    f"JOIN ("
                    f"  SELECT mv.vin FROM \"{fleet_db}\".managed_vehicles mv "
                    f"  WHERE mv.snapshot_date = "
                    f"    (SELECT MAX(snapshot_date) FROM \"{fleet_db}\".managed_vehicles)"
                    f") managed ON managed.vin = bc.vin "
                    f"WHERE bc.supplier_id || '/' || bc.supplier_lot = '{cval}' "
                    f"GROUP BY bc.component_id"
                )
                comp_rows = _athena_query_paged(athena, stage, comp_sql, "pick-component-count")
                comp_counts: dict[str, int] = {}
                for row in comp_rows:
                    cid = row.get("component_id", "")
                    try:
                        comp_counts[cid] = int(row.get("cnt", 0))
                    except (ValueError, TypeError):
                        comp_counts[cid] = 0
                if comp_counts.get(comp_id, 0) == 0:
                    skip_reasons[pid] = (
                        f"component {comp_id!r} has 0 managed VINs in lot {cval!r} "
                        f"(components found: {sorted(comp_counts)})"
                    )
                    continue
            except Exception as exc:  # noqa: BLE001  b9: report Athena error as skip reason
                skip_reasons[pid] = (
                    f"Athena error checking component count: "
                    f"{type(exc).__name__}: {exc}"
                )
                continue
        else:
            # No Athena (unit tests): use affected_vins list as proxy.
            vins_l = _ddb_list(it, "affected_vins")
            if not vins_l:
                skip_reasons[pid] = "no affected_vins (no Athena branch)"
                continue

        candidate = it
        chosen_fm = fm
        chosen_cause_value = cval
        break

    if candidate is None or chosen_fm is None:
        reasons_txt = "; ".join(
            f"{pid}: {r}" for pid, r in sorted(skip_reasons.items())
        ) if skip_reasons else "no supplier_lot rows in table"
        check(res, "pre-pattern-pick", False,
              on_fail=f"{table!r} has no eligible supplier_lot candidate: {reasons_txt}")
        return None, None, None, None, None

    pattern_id = _ddb_str(candidate, "pattern_id")
    predicted_fm_id = chosen_fm["failure_mode_id"]

    # Compute safe_failure_modes: {fm_id: hw_recall_opt_id} for all safe FMs in the pool.
    safe_fms = _safe_failure_modes_for_pool(corpus_fms, _ddb_str(candidate, "code_family"))

    # Check component_name for each safe FM (existing check row, per failure mode).
    for fm_id in sorted(safe_fms):
        fm_item = next(
            (fm for fm in corpus_fms if fm["failure_mode_id"] == fm_id), None
        )
        comp_name = fm_item.get("component_name", "") if fm_item else ""
        check(res, "pre-corpus-component-name",
              bool(comp_name and comp_name.strip()),
              f"corpus {fm_id!r} component_name={comp_name!r}",
              on_fail=f"corpus {fm_id!r} missing component_name "
                      f"(brief §Preconditions: corpus must carry component_name)")

    check(res, "pre-pattern-pick", True,
          f"picked pattern_id={pattern_id!r} predicted_failure_mode_id={predicted_fm_id!r} "
          f"cause_value={chosen_cause_value!r} "
          f"safe_failure_modes={sorted(safe_fms)!r}")
    return candidate, pattern_id, chosen_cause_value, predicted_fm_id, safe_fms


def _compute_expected_vins(
    res: Results,
    athena: Any,
    stage: str,
    cause_value: str,
) -> list[str]:
    """Compute expected VINs — managed VINs intersected with lot VINs (brief §K9).

    Queries the intersection of:
      - ``adp_{stage}_fleet_recall.managed_vehicles`` (newest snapshot via
        MAX(snapshot_date); source: recall_vehicle_status.sql lines 58–68,
        facts.md §Tier 2 recall run)
      - ``adp_{stage}_engineering.vehicle_build_components`` where
        ``supplier_id || '/' || supplier_lot = cause_value``

    This is the independent computation the brief requires (§K2, §K9) — never
    reading from the writer, the views, or the world.

    Uses the named Athena WorkGroup ``cvx-{stage}-analytics`` (K9; source:
    agents/tier2/runner.py:1566 ``ATHENA_WORKGROUP`` env default).  No
    OutputLocation / defaulted account (K9 fix).

    W3 fix: uses _athena_query_paged (follows NextToken) instead of a single
    GetQueryResults call; fleet×lot intersections can exceed 1000 rows.

    Shape-checks *cause_value* against ``_LOT_RE`` before running the query.
    g93lv-facts.md §SQL: lot regex ``^[A-Z0-9-]+/[A-Z0-9-]+$``.

    Returns a sorted, deduplicated list of valid VINs (``_VIN_RE``).
    """
    if not _LOT_RE.fullmatch(cause_value):
        check(res, "pre-expected-vins", False,
              on_fail=f"cause_value {cause_value!r} does not match lot regex "
                      f"{_LOT_RE.pattern!r} (brief §SQL)")
        return []

    eng_db = _GLUE_ENGINEERING_DB.format(stage=stage)
    fleet_db = f"adp_{stage}_fleet_recall"
    # K9: intersect lot VINs with the newest managed snapshot.
    # managed_vehicles source: recall_vehicle_status.sql lines 58-68 (ADP 9bb82e96):
    #   newest_snapshot AS (SELECT MAX(snapshot_date) ... FROM managed_vehicles),
    #   managed AS (SELECT mv.vin FROM managed_vehicles mv JOIN newest_snapshot ...)
    # Produces only VINs that are both in the lot and in the managed fleet.
    sql = (
        f"SELECT DISTINCT bc.vin "
        f"FROM \"{eng_db}\".vehicle_build_components bc "
        f"JOIN ("
        f"  SELECT mv.vin "
        f"  FROM \"{fleet_db}\".managed_vehicles mv "
        f"  WHERE mv.snapshot_date = ("
        f"    SELECT MAX(snapshot_date) "
        f"    FROM \"{fleet_db}\".managed_vehicles"
        f"  )"
        f") managed ON managed.vin = bc.vin "
        f"WHERE bc.supplier_id || '/' || bc.supplier_lot = '{cause_value}' "
        f"AND bc.vin IS NOT NULL"
    )
    # W3 fix: _athena_query_paged follows NextToken so large fleets don't truncate.
    try:
        rows = _athena_query_paged(athena, stage, sql, "pre-expected-vins")
    except Exception as exc:  # noqa: BLE001
        check(res, "pre-expected-vins", False,
              on_fail=f"Athena query pre-expected-vins failed: {type(exc).__name__}: {exc}")
        return []

    vins: list[str] = []
    for row in rows:
        v = row.get("vin", "")
        if _VIN_RE.fullmatch(v):
            vins.append(v)

    vins = sorted(set(vins))  # deduplicate, deterministic order
    check(res, "pre-expected-vins", bool(vins),
          f"{len(vins)} expected VINs from vehicle_build_components "
          f"for cause_value={cause_value!r}",
          on_fail=f"no valid VINs found in vehicle_build_components for {cause_value!r}")
    return vins


def _residue_snapshot(ddb_avx: Any, ddb_adp: Any, stage: str) -> dict:
    """Scan the residue tables with per-table regional clients; return snapshot dict.

    C4 fix: AVX table uses the AVX-region client; ADP tables use the ADP-region client.
    Any scan error is stored as {"error": ...} and _check_residue_errors_in_snapshot
    will fail pre-residue.
    """
    snapshot: dict[str, Any] = {}
    # Table → client mapping (C4 fix: per-table regional client)
    inv_table = _INV_TABLE.format(stage=stage)
    fp_table = _FAULT_PATTERNS_TABLE.format(stage=stage)
    corpus_table = _ENGINEERING_CORPUS_TABLE.format(stage=stage)
    table_clients: list[tuple[str, Any]] = [
        (inv_table, ddb_avx),
        (fp_table, ddb_adp),
        (corpus_table, ddb_adp),
    ]
    for tbl, ddb in table_clients:
        try:
            items: list[dict] = []
            kwargs: dict[str, Any] = {"TableName": tbl}
            while True:
                page = ddb.scan(**kwargs)
                items.extend(page.get("Items", []))
                if not page.get("LastEvaluatedKey"):
                    break
                kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]
            snapshot[tbl] = {"count": len(items)}
        except Exception as exc:  # noqa: BLE001
            snapshot[tbl] = {"error": f"{type(exc).__name__}: {exc}"}
    return snapshot


def _check_residue_errors_in_snapshot(res: Results, snapshot: dict) -> bool:
    """Fail pre-residue if any table in the snapshot has a read error or no count.

    C4 fix: read errors (from wrong-region clients or network failures) must not
    silently pass.  An entry without a 'count' key (e.g. snapshot stores empty {})
    is also treated as an error because the residue comparison would be vacuous.
    Returns True if any error/missing-count is present (pre-residue FAIL).
    """
    issues = []
    for tbl, v in snapshot.items():
        if "error" in v:
            issues.append(f"{tbl}: {v['error']}")
        elif "count" not in v:
            issues.append(f"{tbl}: snapshot has no 'count' (scan succeeded but stored no data)")
    if issues:
        check(
            res, "pre-residue", False,
            on_fail=f"residue snapshot has {len(issues)} issue(s): "
                    f"{'; '.join(issues[:3])}",
        )
        return True
    return False


def _check_no_open_livetest(res: Results, idp: Any, stage: str) -> bool:
    """Check no leftover livetest temp users exist in the Cognito pool (brief §Preconditions; K6).

    The old implementation scanned the investigations DDB table for
    investigation_ids starting with the LIVETEST label — vacuous because
    investigation_ids are server-generated UUIDs.  This implementation lists
    Cognito users whose email attribute starts with _LIVETEST_USER_PREFIX
    (``livetest-fi-loop-``).  Any confirmed user with that prefix means a
    previous run did not fully clean up.

    Requires cognito-idp.ListUserPools + ListUsers in _READ_OPS (K6).
    """
    # Resolve pool_id for _CMS_POOL_NAME — same "exactly one" rule as _resolve_pool_id.
    # S7 fix: previously used pools[0]["Id"] (silently took the first pool).
    try:
        pools: list[dict] = []
        kwargs: dict[str, Any] = {"MaxResults": 60}
        while True:
            page = idp.list_user_pools(**kwargs)
            pools.extend(p for p in page.get("UserPools", []) if p["Name"] == _CMS_POOL_NAME)
            token = page.get("NextToken")
            if not token:
                break
            kwargs["NextToken"] = token
    except Exception as exc:  # noqa: BLE001
        check(res, "pre-no-open-livetest", False,
              on_fail=f"ListUserPools failed: {type(exc).__name__}: {exc}")
        return False

    if len(pools) != 1:
        check(res, "pre-no-open-livetest", False,
              on_fail=f"expected exactly one pool named {_CMS_POOL_NAME!r}, "
                      f"found {len(pools)} (S7 fix: same rule as _resolve_pool_id)")
        return False

    pool_id = pools[0]["Id"]

    # List users whose email starts with the livetest user prefix
    try:
        livetest_users: list[str] = []
        kwargs2: dict[str, Any] = {
            "UserPoolId": pool_id,
            "Filter": f'email ^= "{_LIVETEST_USER_PREFIX}"',
        }
        while True:
            page2 = idp.list_users(**kwargs2)
            for u in page2.get("Users", []):
                # C3 fix: the pool uses email aliases, so Username is the sub UUID.
                # Any user returned by the email filter IS a livetest leftover.
                email_val = ""
                for attr in u.get("Attributes", []):
                    if attr.get("Name") == "email":
                        email_val = attr.get("Value", "")
                        break
                livetest_users.append(email_val or u.get("Username", ""))
            token2 = page2.get("PaginationToken") or page2.get("NextToken")
            if not token2:
                break
            kwargs2["PaginationToken"] = token2
    except Exception as exc:  # noqa: BLE001
        check(res, "pre-no-open-livetest", False,
              on_fail=f"ListUsers(pool={pool_id!r}) failed: {type(exc).__name__}: {exc}")
        return False

    ok = not livetest_users
    check(res, "pre-no-open-livetest", ok,
          f"no leftover {_LIVETEST_USER_PREFIX!r} users in Cognito pool {pool_id!r}",
          on_fail=f"{len(livetest_users)} leftover livetest user(s) in pool {pool_id!r}: "
                  f"{livetest_users[:3]}; run --part cleanup (or delete manually) before re-running")
    return ok


# ── part pre ──────────────────────────────────────────────────────────────────

def _part_pre(res: Results, *, stage: str, run_id: str, **_: Any) -> None:
    """Part pre: stage/identity/windows; preconditions; pattern pick; manifest.

    Brief §pre (read-only).  Every item runs in its own try/except; a single
    failure does not skip the remaining items.  The manifest is written at the
    end regardless (even if some items FAIL) so the operator can diagnose which
    preconditions to fix.
    """
    _init_session("pre")

    # 1. Stage and caller identity
    print(f"\n── pre: stage={stage!r} run_id={run_id!r}")
    try:
        sts = _client("sts", _AVX_REGION)
        identity = sts.get_caller_identity()
        account = identity.get("Account", "?")
        arn = identity.get("Arn", "?")
        print(f"  identity: account={account!r} arn={arn!r}")
        check(res, "pre-stage-identity", True,
              f"stage={stage!r} account={account!r} arn={arn!r}")
    except Exception as exc:  # noqa: BLE001
        check(res, "pre-stage-identity", False,
              on_fail=f"GetCallerIdentity failed: {type(exc).__name__}: {exc}")
        account = ""
        arn = ""

    # 2. Window check: maintenance window + schedule guard
    try:
        now = _utc_now()
        maint_reason = _maint_window_guard(now)
        if maint_reason:
            check(res, "pre-windows-maintenance", False,
                  on_fail=maint_reason)
        else:
            check(res, "pre-windows-maintenance", True,
                  f"not in maintenance window ({now.strftime('%H:%M')} UTC)")

        sched_client = _client("scheduler", _AVX_REGION)
        reason = _schedule_guard(sched_client, _SCHEDULES, stage, now)
        if reason:
            check(res, "pre-windows-schedule", False, on_fail=reason)
        else:
            check(res, "pre-windows-schedule", True,
                  f"not within {_SCHED_GUARD_MIN} min of any scheduled job")
    except Exception as exc:  # noqa: BLE001
        check(res, "pre-windows", False,
              on_fail=f"window check failed: {type(exc).__name__}: {exc}")

    # 3. Tier 2 Lambda carries 9.3's code
    try:
        lam = _client("lambda", _AVX_REGION)
        fn_name = _TIER2_FN.format(stage=stage, region=_AVX_REGION)
        _check_tier2_code(res, lam, fn_name)
    except Exception as exc:  # noqa: BLE001
        check(res, "pre-tier2-code", False,
              on_fail=f"code check raised: {type(exc).__name__}: {exc}")

    # 4. recall_campaigns_issued Glue table exists
    try:
        glue = _client("glue", _ADP_REGION)
        _check_recall_table_exists(res, glue, stage)
    except Exception as exc:  # noqa: BLE001
        check(res, "pre-recall-table", False,
              on_fail=f"Glue check raised: {type(exc).__name__}: {exc}")

    # 5. Received view SQL unions the issued table
    try:
        glue = _client("glue", _ADP_REGION)
        _check_view_sql_unions_issued(res, glue, stage)
    except Exception as exc:  # noqa: BLE001
        check(res, "pre-view-sql", False,
              on_fail=f"view SQL check raised: {type(exc).__name__}: {exc}")

    # 6. Investigation ESM is Enabled
    try:
        lam_esm = _client("lambda", _AVX_REGION)
        _check_esm_enabled(res, lam_esm, stage, _AVX_REGION)
    except Exception as exc:  # noqa: BLE001
        check(res, "pre-esm-enabled", False,
              on_fail=f"ESM check raised: {type(exc).__name__}: {exc}")

    # 7. Investigation DLQ is empty
    try:
        sqs = _client("sqs", _AVX_REGION)
        _check_dlq_empty(res, sqs, stage)
    except Exception as exc:  # noqa: BLE001
        check(res, "pre-dlq-empty", False,
              on_fail=f"DLQ check raised: {type(exc).__name__}: {exc}")

    # 8. Pick pattern (F6a: no failure_mode_id in pattern row; uses code_family + corpus scan)
    pattern_row = None
    pattern_id = None
    cause_value = None
    predicted_fm_id = None
    safe_fms: dict = {}
    expected_vins: list[str] = []
    try:
        ddb_adp = _client("dynamodb", _ADP_REGION)
        athena_adp = _client("athena", _ADP_REGION)
        pattern_row, pattern_id, cause_value, predicted_fm_id, safe_fms = _pick_pattern(
            res, ddb_adp, stage, athena=athena_adp
        )
        if pattern_row is not None and cause_value:
            # K2/K9: compute expected VINs — managed × lot intersection via Athena.
            # K9: WorkGroup, not OutputLocation.
            expected_vins = _compute_expected_vins(
                res, athena_adp, stage, cause_value,
            )
    except Exception as exc:  # noqa: BLE001
        check(res, "pre-pattern-pick", False,
              on_fail=f"pattern pick raised: {type(exc).__name__}: {exc}")

    # 9. No open LIVETEST g9.3 records (K6: scan Cognito for leftover temp users)
    try:
        idp_pre = _client("cognito-idp", _AVX_REGION)
        _check_no_open_livetest(res, idp_pre, stage)
    except Exception as exc:  # noqa: BLE001
        check(res, "pre-no-open-livetest", False,
              on_fail=f"LIVETEST check raised: {type(exc).__name__}: {exc}")

    # 10. Residue snapshot
    residue: dict = {}
    try:
        ddb_avx2 = _client("dynamodb", _AVX_REGION)
        ddb_adp2 = _client("dynamodb", _ADP_REGION)
        # C4 fix: one flat snapshot; per-table regional clients
        snapshot = _residue_snapshot(ddb_avx2, ddb_adp2, stage)
        if _check_residue_errors_in_snapshot(res, snapshot):
            residue = snapshot  # record even if errors so manifest has context
        else:
            residue = snapshot
            check(res, "pre-residue", True,
                  f"residue snapshot taken ({len(snapshot)} tables)")
    except Exception as exc:  # noqa: BLE001
        check(res, "pre-residue", False,
              on_fail=f"residue snapshot raised: {type(exc).__name__}: {exc}")

    # 11. Write manifest (brief §Manifest: create in pre)
    # W7 fix: record whether pre passed — "passed" iff no FAIL rows AND
    # expected_vins is non-empty; otherwise "failed".  issue refuses unless
    # pre_verdict == "passed".
    pre_verdict = "passed" if (not res.has_any_fail and bool(expected_vins)) else "failed"
    manifest_path = _manifest_path(run_id)
    manifest: dict[str, Any] = {
        "run_id": run_id,
        "stage": stage,
        "created_at": _utc_now().isoformat(),
        "part": "pre",
        "account": account,
        "identity_arn": arn,
        "predicted_failure_mode_id": predicted_fm_id,  # F6a: rules-path FM choice
        "safe_failure_modes": safe_fms,                # F6a: {fm_id: hw_recall_opt_id}
        "pattern_id": pattern_id,
        "cause_value": cause_value,
        "expected_vins": expected_vins,
        "pre_verdict": pre_verdict,
        "residue": residue,
        "records": {},  # filled by later write parts
    }
    try:
        with _defer_signals_for_restore():
            _write_manifest(manifest_path, manifest)
        check(res, "pre-manifest", True,
              f"manifest written to {manifest_path!r} (0600)")
    except Exception as exc:  # noqa: BLE001
        check(res, "pre-manifest", False,
              on_fail=f"failed to write manifest to {manifest_path!r}: "
                      f"{type(exc).__name__}: {exc}")


# ── issue-part helpers ────────────────────────────────────────────────────────


def _http(
    url: str,
    method: str,
    path: str,
    token: str,
    body: dict | None = None,
) -> tuple[int, dict]:
    """HTTP call to an API endpoint; returns (status_code, parsed_body).

    Uses stdlib ``urllib`` so no extra dependency is needed.
    Brief §HTTP: one test per helper capturing the real bytes it sends,
    with every field the server needs.
    Refusal bodies pinned: {"error": "Forbidden"} (403), API GW {"message": ...},
    CMS {"reason": ...} (brief §HTTP).
    """
    import json as _json   # noqa: PLC0415
    import urllib.error    # noqa: PLC0415
    import urllib.request  # noqa: PLC0415

    full_url = url.rstrip("/") + "/" + path.lstrip("/")
    data = _json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(full_url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    if data:
        req.add_header("Content-Type", "application/json")

    def _parse(raw: bytes) -> dict:
        try:
            out = _json.loads(raw or b"{}")
        except ValueError:
            return {"_raw": raw[:200].decode("utf-8", "replace")}
        return out if isinstance(out, dict) else {"_raw": str(out)[:200]}

    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, _parse(r.read())
    except urllib.error.HTTPError as e:
        return e.code, _parse(e.read())
    except (urllib.error.URLError, OSError) as e:  # includes timeouts
        return 0, {"_error": f"{type(e).__name__}: {e}"}


def _get_avx_api_url(cfn: Any, stage: str) -> str:
    """Return the RestApiUrl from the VSA API CloudFormation stack.

    Brief §issue: get API URL from CloudFormation outputs.
    Stack name: vsa-{stage}-api; output key: RestApiUrl.
    """
    stack_name = _VSA_API_STACK.format(stage=stage)
    r = cfn.describe_stacks(StackName=stack_name)
    outputs = {
        o["OutputKey"]: o["OutputValue"]
        for o in r["Stacks"][0]["Outputs"]
    }
    url = outputs.get("RestApiUrl", "")
    if not url:
        raise ValueError(f"RestApiUrl not found in {stack_name} outputs: {list(outputs)}")
    return url


def _get_cms_api_url(cfn: Any, stage: str) -> str:
    """Return the APIEndpoint from the CMS UI CloudFormation stack.

    Brief §ground: CMS API URL needed for groundings POST and return-to-service POST.
    Stack name: cms-{stage}-ui; output key: APIEndpoint.
    Source: CMS main 81a90d385 deployment/stacks/ui_stack.py:5527 (CfnOutput 'APIEndpoint').
    """
    stack_name = _CMS_UI_STACK.format(stage=stage)
    r = cfn.describe_stacks(StackName=stack_name)
    outputs = {
        o["OutputKey"]: o["OutputValue"]
        for o in r["Stacks"][0]["Outputs"]
    }
    url = outputs.get(_CMS_API_OUTPUT_KEY, "")
    if not url:
        raise ValueError(
            f"{_CMS_API_OUTPUT_KEY!r} not found in {stack_name} outputs: {list(outputs)}"
        )
    return url


def _resolve_pool_id(idp: Any) -> str:
    """Return pool_id for cms-staging-ui-users; fail if not exactly one.

    Pages list_user_pools; brief §Cognito.
    """
    token: str | None = None
    matching: list[str] = []
    while True:
        kwargs: dict[str, Any] = {"MaxResults": 60}
        if token:
            kwargs["NextToken"] = token
        page = idp.list_user_pools(**kwargs)
        for p in page.get("UserPools", []):
            if p["Name"] == _CMS_POOL_NAME:
                matching.append(p["Id"])
        token = page.get("NextToken")
        if not token:
            break
    if len(matching) != 1:
        raise RuntimeError(
            f"expected exactly one pool named {_CMS_POOL_NAME!r}, "
            f"found {len(matching)}"
        )
    return matching[0]


def _password_client(idp: Any, pool_id: str) -> str:
    """Return the first app-client id that allows USER_PASSWORD_AUTH.

    Brief §Cognito.
    """
    kwargs: dict[str, Any] = {"UserPoolId": pool_id, "MaxResults": 60}
    while True:
        page = idp.list_user_pool_clients(**kwargs)
        for c in page.get("UserPoolClients", []):
            desc = idp.describe_user_pool_client(
                UserPoolId=pool_id, ClientId=c["ClientId"]
            )
            flows = desc["UserPoolClient"].get("ExplicitAuthFlows") or []
            if "ALLOW_USER_PASSWORD_AUTH" in flows:
                return c["ClientId"]
        if not page.get("NextToken"):
            break
        kwargs["NextToken"] = page["NextToken"]
    raise RuntimeError(f"no app client on {pool_id} allows USER_PASSWORD_AUTH")


def _create_temp_user(
    idp: Any,
    pool_id: str,
    client_id: str,
    run_id: str,
    role: str = "pe",
) -> tuple[str, str, str]:
    """Create a temp Cognito user; return (email, password, id_token).

    The pool uses UsernameAttributes=[email], so ``Username`` in ``AdminCreateUser``
    IS the email address and the sub UUID is generated server-side.
    Email = ``livetest-fi-loop-<run_id>-<role>@example.invalid``.
    Adds to product-engineer group.  Signals held outside.
    Brief §Cognito (C3 fix): email-format Username; match email attribute for lookup.
    """
    username = f"{_LIVETEST_USER_PREFIX}{run_id}-{role}"
    email = f"{username}@example.invalid"
    password = f"Lv{secrets.token_urlsafe(18)}!9Qz"
    attrs: list[dict[str, str]] = [
        {"Name": "email", "Value": email},
        {"Name": "email_verified", "Value": "true"},
    ]
    idp.admin_create_user(
        UserPoolId=pool_id,
        Username=email,
        MessageAction="SUPPRESS",
        UserAttributes=attrs,
    )
    idp.admin_set_user_password(
        UserPoolId=pool_id, Username=email, Password=password, Permanent=True
    )
    idp.admin_add_user_to_group(
        UserPoolId=pool_id, Username=email, GroupName="product-engineer"
    )
    auth = idp.initiate_auth(
        ClientId=client_id,
        AuthFlow="USER_PASSWORD_AUTH",
        AuthParameters={"USERNAME": email, "PASSWORD": password},
    )
    token = auth["AuthenticationResult"]["IdToken"]
    return email, password, token


def _delete_temp_user(idp: Any, pool_id: str, username: str) -> None:
    """AdminDeleteUser with retries; UserNotFoundException counts as deleted.

    Brief §Cognito: "a failed delete, or a user still present, fails cleanup".
    """
    for attempt in range(3):
        try:
            idp.admin_delete_user(UserPoolId=pool_id, Username=username)
            return
        except Exception as exc:  # noqa: BLE001
            code = getattr(exc, "response", {}).get("Error", {}).get("Code", "")
            if code == "UserNotFoundException":
                return
            if attempt == 2:
                raise
            import time as _time  # noqa: PLC0415
            _time.sleep(0.5 * (attempt + 1))


def _poll_investigation(
    api_url: str,
    investigation_id: str,
    token: str,
    *,
    max_retries: int = _POLL_MAX_RETRIES,
    sleep_s: float = _POLL_SLEEP_S,
) -> dict:
    """Poll GET /investigations/{id} until status is completed or failed.

    Returns the final poll response dict.
    g93lv-facts.md §AVX investigations route: status ∈ {queued, running,
    completed, failed}.
    """
    import time as _time  # noqa: PLC0415

    for attempt in range(max_retries):
        status_code, body = _http(
            api_url, "GET", f"investigations/{investigation_id}", token
        )
        if status_code != 200:
            return {"_poll_error": f"HTTP {status_code}", **body}
        inv_status = body.get("status", "")
        if inv_status in ("completed", "failed"):
            return body
        if attempt < max_retries - 1:
            _time.sleep(sleep_s)
    return body  # timeout; return last response


def _read_ddb_item(ddb: Any, table: str, key: dict) -> dict | None:
    """GetItem with ConsistentRead; return Item or None."""
    r = ddb.get_item(TableName=table, Key=key, ConsistentRead=True)
    item = r.get("Item")
    if not item:
        return None
    # Unwrap AttributeValue wrappers for simple types
    def _v(x: Any) -> Any:
        if isinstance(x, dict) and len(x) == 1:
            k, val = next(iter(x.items()))
            if k in ("S", "N", "BOOL", "NULL"):
                return val
        return x
    return {k: _v(v) for k, v in item.items()}


def _scan_ddb_filter(ddb: Any, table: str, filter_expr: str, expr_vals: dict) -> list[dict]:
    """Scan a DDB table with a FilterExpression; page fully (brief §Manifest).

    Brief §Manifest: "Residue scans page fully (no Limit with a filter)."
    """
    items: list[dict] = []
    kwargs: dict[str, Any] = {
        "TableName": table,
        "FilterExpression": filter_expr,
        "ExpressionAttributeValues": expr_vals,
    }
    while True:
        page = ddb.scan(**kwargs)
        items.extend(page.get("Items", []))
        if not page.get("LastEvaluatedKey"):
            break
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]
    return items


def _check_ddb_draft_row(
    res: Results, ddb: Any, stage: str, campaign_id: str, thread_id: str
) -> bool:
    """Check the ADP recall-campaigns-issued DDB row is in status='draft'.

    Brief §issue checks: "DDB draft row after record_engineering_decision".
    g93lv-facts.md §ADP writer: issue path step 1 (mark_recall_issued) is the
    SECOND approve; after record_engineering_decision the row is status='draft'.
    """
    if not _CAMPAIGN_ID_RE.fullmatch(campaign_id):
        check(res, "issue-ddb-draft", False,
              on_fail=f"campaign_id {campaign_id!r} does not match {_CAMPAIGN_ID_RE.pattern} "
                      f"(brief §SQL)")
        return False
    table = _RECALL_CAMPAIGNS_ISSUED_DDB.format(stage=stage)
    try:
        row = _read_ddb_item(
            ddb, table, {"campaign_id": {"S": campaign_id}}
        )
    except Exception as exc:  # noqa: BLE001
        check(res, "issue-ddb-draft", False,
              on_fail=f"GetItem {table!r} campaign_id={campaign_id!r} failed: "
                      f"{type(exc).__name__}: {exc}")
        return False
    if row is None:
        check(res, "issue-ddb-draft", False,
              on_fail=f"no draft row in {table!r} for campaign_id={campaign_id!r}")
        return False
    status = row.get("status", "")
    thread_ok = row.get("thread_id") == thread_id
    ok = status == "draft" and thread_ok
    check(res, "issue-ddb-draft", ok,
          f"draft row status={status!r} thread_id={row.get('thread_id')!r}",
          on_fail=f"expected status='draft', thread_id={thread_id!r}; "
                  f"got status={status!r} thread_id={row.get('thread_id')!r}")
    if not ok:
        return False

    # S1 fix: before issue_recall approve, refuse unless draft's do_not_drive is
    # False and recall_class is "non_safety".  The writer reads the corpus a few
    # hundred ms after the re-check, so this is the one approve whose content
    # nothing else in issue verifies against what was vetted.
    dnd_raw = row.get("do_not_drive")
    dnd_ok = dnd_raw is False
    if not dnd_ok:
        check(res, "issue-draft-dnd-safe", False,
              on_fail=f"draft row do_not_drive={dnd_raw!r}; must be exactly False before "
                      f"issue_recall approve (brief §S1)")
        return False
    check(res, "issue-draft-dnd-safe", True,
          f"draft row do_not_drive={dnd_raw!r} (False)")
    recall_class = row.get("recall_class", "")
    rc_ok = recall_class == "non_safety"
    if not rc_ok:
        check(res, "issue-draft-recall-class", False,
              on_fail=f"draft row recall_class={recall_class!r}; must be 'non_safety' before "
                      f"issue_recall approve (brief §S1)")
        return False
    check(res, "issue-draft-recall-class", True,
          f"draft row recall_class={recall_class!r}")
    return True


def _find_quality_issue_finding(
    ddb: Any, stage: str, thread_id: str
) -> dict | None:
    """Scan AVX findings for the quality.issue Finding for THIS run's thread.

    Brief §issue checks: "quality.issue Finding" (after record_engineering_decision).
    g93lv-facts.md §fi_quality agent: ISSUE_FINDING_KIND = 'quality.issue';
    subject_key = f"{thread_id}/issue" (issue.py:234).

    C1 fix: filter by subject_key == f"{thread_id}/issue" (from the decision response's
    target_ref) so we never approve a different run's quality.issue Finding.  Requires
    exactly one match; returns None if zero or >1.
    """
    if not thread_id:
        return None
    table = _FINDINGS_TABLE.format(stage=stage)
    # Full scan with ConsistentRead — the quality.issue Finding may have just been
    # written by the approve call; without ConsistentRead a stale read could miss it
    # and produce a false FAIL.  S2 fix.
    items: list[dict] = []
    kwargs: dict[str, Any] = {"TableName": table, "ConsistentRead": True}
    while True:
        page = ddb.scan(**kwargs)
        items.extend(page.get("Items", []))
        if not page.get("LastEvaluatedKey"):
            break
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]

    def _sv(x: Any) -> str:
        """Unwrap a DDB AttributeValue string or return plain string."""
        if isinstance(x, dict):
            return str(x.get("S", x.get("N", "")))
        return str(x or "")

    target_subject_key = f"{thread_id}/issue"
    # Filter to quality.issue Findings with matching subject_key
    matched = [
        it for it in items
        if _sv(it.get("finding_kind") or "") == "quality.issue"
        and _sv(it.get("subject_key") or "") == target_subject_key
    ]
    if len(matched) != 1:
        return None
    return matched[0]


def _check_quality_issue_finding(
    res: Results, ddb: Any, stage: str, thread_id: str
) -> tuple[str, str, str]:
    """Check the quality.issue Finding for THIS run's thread; return (issue_finding_id, thread_id, draft_id).

    Returns ("", "", "") on failure.
    Brief §issue checks: quality.issue Finding after record_engineering_decision.
    g93lv-facts.md §fi_quality agent issue.py: recommended action_kind='issue_recall';
    server-set parameters.thread_id and parameters.draft_id (issue.py:316-319).
    C1 fix: thread_id comes from the decision response's target_ref.
    """
    try:
        finding = _find_quality_issue_finding(ddb, stage, thread_id)
    except Exception as exc:  # noqa: BLE001
        check(res, "issue-quality-issue-finding", False,
              on_fail=f"DDB scan for quality.issue Finding failed: "
                      f"{type(exc).__name__}: {exc}")
        return "", "", ""

    if finding is None:
        check(res, "issue-quality-issue-finding", False,
              on_fail="no quality.issue Finding found after record_engineering_decision")
        return "", "", ""

    def _v(x: Any) -> str:
        if isinstance(x, dict):
            return str(x.get("S", x.get("N", "")))
        return str(x or "")

    issue_finding_id = _v(finding.get("finding_id"))

    # Extract recommended_action.parameters (handles nested M wrappers)
    rec_raw = finding.get("recommended_action") or {}
    if isinstance(rec_raw, dict) and "M" in rec_raw:
        rec_raw = rec_raw["M"]
    action_kind_raw = rec_raw.get("action_kind") or {}
    action_kind = _v(action_kind_raw)
    params_raw = rec_raw.get("parameters") or {}
    if isinstance(params_raw, dict) and "M" in params_raw:
        params_raw = params_raw["M"]
    thread_id = _v(params_raw.get("thread_id") or {})
    draft_id = _v(params_raw.get("draft_id") or {})

    ok = bool(issue_finding_id and action_kind == "issue_recall" and thread_id and draft_id)
    check(res, "issue-quality-issue-finding", ok,
          f"quality.issue finding_id={issue_finding_id!r} action_kind={action_kind!r} "
          f"thread_id={thread_id!r} draft_id={draft_id!r}",
          on_fail=f"quality.issue Finding missing fields: "
                  f"finding_id={issue_finding_id!r} action_kind={action_kind!r} "
                  f"thread_id={thread_id!r} draft_id={draft_id!r}")
    return issue_finding_id, thread_id, draft_id


def _check_ddb_issued_row(
    res: Results, ddb: Any, stage: str, campaign_id: str
) -> bool:
    """Check the ADP recall-campaigns-issued DDB row transitioned to status='issued'.

    Brief §issue checks: "DDB issued row" after issue_recall approve.
    g93lv-facts.md §ADP writer issue path step 1: mark_recall_issued DDB conditional
    update status draft→issued with issued_at and received_date.
    """
    table = _RECALL_CAMPAIGNS_ISSUED_DDB.format(stage=stage)
    try:
        row = _read_ddb_item(ddb, table, {"campaign_id": {"S": campaign_id}})
    except Exception as exc:  # noqa: BLE001
        check(res, "issue-ddb-issued", False,
              on_fail=f"GetItem {table!r} campaign_id={campaign_id!r} failed: "
                      f"{type(exc).__name__}: {exc}")
        return False
    if row is None:
        check(res, "issue-ddb-issued", False,
              on_fail=f"no row in {table!r} for campaign_id={campaign_id!r}")
        return False
    status = row.get("status", "")
    received_date = row.get("received_date", "")
    ok = status == "issued" and bool(received_date)
    check(res, "issue-ddb-issued", ok,
          f"issued row status={status!r} received_date={received_date!r}",
          on_fail=f"expected status='issued' with received_date; "
                  f"got status={status!r} received_date={received_date!r}")
    if not ok:
        return False
    # W4: received_date must be <= today's UTC date so the next recall run
    # (cron(0 5 * * ? *) UTC, same day) picks it up (recall_campaigns_received
    # view WHERE received_date <= CURRENT_DATE).
    next_run_date = _utc_now().strftime("%Y-%m-%d")
    date_ok = received_date <= next_run_date
    check(res, "issue-ddb-issued-date", date_ok,
          f"received_date={received_date!r} <= next recall run date {next_run_date!r}",
          on_fail=f"received_date={received_date!r} > next recall run date {next_run_date!r}; "
                  f"recall run may not process this campaign")
    return date_ok


def _check_s3_issued_recall(
    res: Results, s3: Any, stage: str, account: str, campaign_id: str
) -> bool:
    """Check the issued-recall S3 object exists and inspect SSE-KMS.

    Brief §issue checks: "lake object key + SSE-KMS head".
    g93lv-facts.md §ADP writer issue path step 2:
      Bucket = ISSUED_RECALLS_BUCKET = adp-{stage}-foundation-lake-{account}-us-east-1
      Key    = curated/engineering/recall_campaigns_issued/{campaign_id}.json
    SSE-KMS: no explicit SSE arg in put_object (handler.py:427-434); bucket-level
    encryption may supply it; check HeadObject ServerSideEncryption (OPEN fact).
    """
    bucket = _ADP_LAKE_BUCKET.format(stage=stage, account=account)
    key = f"curated/engineering/recall_campaigns_issued/{campaign_id}.json"
    try:
        r = s3.head_object(Bucket=bucket, Key=key)
    except Exception as exc:  # noqa: BLE001
        check(res, "issue-s3-key", False,
              on_fail=f"HeadObject s3://{bucket}/{key} failed: "
                      f"{type(exc).__name__}: {exc}")
        return False
    check(res, "issue-s3-key", True,
          f"s3://{bucket}/{key} exists (ContentLength={r.get('ContentLength')})")
    sse = r.get("ServerSideEncryption", "")
    sse_ok = sse == "aws:kms"
    check(res, "issue-s3-sse-kms", sse_ok,
          f"ServerSideEncryption={sse!r} (aws:kms expected)",
          on_fail=f"ServerSideEncryption={sse!r}; expected aws:kms — "
                  f"bucket-level SSE-KMS not configured or not established "
                  f"(g93lv-facts.md §OPEN 3)")
    return sse_ok  # W2 fix: return sse_ok so a FAIL propagates to the caller


def _pick_hardware_recall_option_from_finding(
    ddb: Any, stage: str, finding_id: str
) -> tuple[str, dict]:
    """Read a quality.investigation Finding and return its hardware_recall option_id.

    C10 fix: always pick the hardware_recall option from the quality.options evidence,
    not the recommended option.  K3 guarantees the chosen pattern has a hardware_recall
    option with do_not_drive=False, so it must be present.  Returns ("", {}) if not found.

    Returns (option_id, option_dict) or ("", {}) on failure.
    """
    findings_table = _FINDINGS_TABLE.format(stage=stage)
    try:
        item = _read_ddb_item(ddb, findings_table, {"finding_id": {"S": finding_id}})
    except Exception:  # noqa: BLE001
        return "", {}
    if item is None:
        return "", {}

    def _sv(x: Any) -> str:
        if isinstance(x, dict):
            return str(x.get("S", x.get("N", "")))
        return str(x or "")

    def _bv(x: Any) -> bool:
        if isinstance(x, dict):
            return bool(x.get("BOOL", False))
        return bool(x)

    # Scan evidence list for quality.options entry
    evidence_raw = item.get("evidence", {})
    if isinstance(evidence_raw, dict) and "L" in evidence_raw:
        evidence_list = evidence_raw["L"]
    elif isinstance(evidence_raw, list):
        evidence_list = evidence_raw
    else:
        return "", {}

    for ev in evidence_list:
        if isinstance(ev, dict) and "M" in ev:
            ev = ev["M"]
        if not isinstance(ev, dict):
            continue
        kind = _sv(ev.get("kind") or "")
        if kind != "quality.options":
            continue
        # Found quality.options entry; scan its value list
        val_raw = ev.get("value", {})
        if isinstance(val_raw, dict) and "L" in val_raw:
            opts = val_raw["L"]
        elif isinstance(val_raw, list):
            opts = val_raw
        else:
            opts = []
        for opt in opts:
            if isinstance(opt, dict) and "M" in opt:
                opt = opt["M"]
            if not isinstance(opt, dict):
                continue
            rtype = _sv(opt.get("remedy_type") or "")
            if rtype == "hardware_recall":
                oid = _sv(opt.get("remedy_option_id") or "")
                return oid, opt
    return "", {}


def _check_thread_versions(
    res: Results, ddb: Any, stage: str, thread_id: str,
    expected_action_ids: "set[str] | None" = None,
) -> list[int]:
    """Check DDB digital-thread has v1 (decided) and v2 (issued) after issue_recall.

    F2g: returns the list of integer version numbers found (e.g. [1, 2]) so the
    caller can record them in the manifest for conditional cleanup.  Returns [] on
    failure so callers can treat the result as falsy in error paths.

    W3 F4b: if expected_action_ids is provided, FAIL on any version whose action_id
    is not in that set, and return only matching versions.

    Brief §issue checks: "thread versions" after issue_recall.
    g93lv-facts.md §ADP writer: thread v1 status='decided' (record_engineering_decision);
    thread v2 status='issued' (issue_recall, _handle_issue_recall handler.py:1211-1234).
    """
    table = _DIGITAL_THREAD_TABLE.format(stage=stage)
    try:
        rows: list[dict] = []
        kwargs: dict[str, Any] = {
            "TableName": table,
            "KeyConditionExpression": "thread_id = :t",
            "ExpressionAttributeValues": {":t": {"S": thread_id}},
        }
        while True:
            page = ddb.query(**kwargs)
            rows.extend(page.get("Items", []))
            if not page.get("LastEvaluatedKey"):
                break
            kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]
    except Exception as exc:  # noqa: BLE001
        check(res, "issue-thread-versions", False,
              on_fail=f"Query {table!r} thread_id={thread_id!r} failed: "
                      f"{type(exc).__name__}: {exc}")
        return []

    def _sv(item: dict, field: str) -> str:
        v = item.get(field, {})
        return v.get("S", str(v)) if isinstance(v, dict) else str(v)

    # W3 F4b: filter by action_id when expected_action_ids is provided.
    if expected_action_ids:
        foreign: list[int] = []
        own: list[dict] = []
        for r in rows:
            ver_n = r.get("version", {}).get("N") or "0"
            aid = _sv(r, "action_id")
            if aid and aid not in expected_action_ids:
                foreign.append(int(ver_n))
            else:
                own.append(r)
        if foreign:
            check(res, "issue-thread-versions-foreign", False,
                  on_fail=f"thread {thread_id!r}: version(s) {foreign!r} have action_ids "
                          f"not in this run's expected set {sorted(expected_action_ids)!r}; "
                          f"only recording own versions")
        rows = own

    statuses = [_sv(r, "status") for r in rows]
    has_decided = "decided" in statuses
    has_issued = "issued" in statuses
    ok = has_decided and has_issued
    check(res, "issue-thread-versions", ok,
          f"thread {thread_id!r} versions={len(rows)} statuses={statuses}",
          on_fail=f"expected statuses ['decided', 'issued']; got {statuses} "
                  f"({len(rows)} rows) for thread_id={thread_id!r}")
    if not ok:
        return []
    # Return all integer version numbers for manifest recording.
    return sorted(
        int(r.get("version", {}).get("N", "0") or "0")
        for r in rows
        if r.get("version", {}).get("N")
    )


def _recheck_investigation_finding(
    res: Results,
    ddb_avx: Any,
    ddb_adp: Any,
    stage: str,
    finding_id: str,
    safe_fms: dict,
) -> "tuple[str, str, bool]":
    """Re-check the investigation Finding after poll and before decision approve.

    Brief §Fix(b) issue re-check steps 1–4 (without the signal-hold; caller does that):
      1. Read quality.root_cause evidence from the Finding to get failure_mode_id.
      2. Verify that id is a key of safe_fms.
      3. Verify the live corpus row for it still passes the safe rule and still has
         the expected hardware_recall option_id from safe_fms.
      4. Verify the Finding's quality.options contains that option_id with
         remedy_type == "hardware_recall".

    Returns (chosen_fm_id, option_id, ok):
      - chosen_fm_id: the failure_mode_id from the Finding's root_cause (or "" on error).
      - option_id:    the hardware_recall option_id from safe_fms for chosen_fm_id
                      (or "" on error/fail).
      - ok:           True only if all four steps pass.

    Records check rows in res for each step.  On FAIL the caller must NOT approve.
    """
    findings_table = _FINDINGS_TABLE.format(stage=stage)
    corpus_table = _ENGINEERING_CORPUS_TABLE.format(stage=stage)

    # --- helpers ---
    def _sv(x: Any) -> str:
        if isinstance(x, dict):
            return str(x.get("S", x.get("N", "")))
        return str(x or "")

    # Step 1: read the Finding
    try:
        item = _read_ddb_item(ddb_avx, findings_table, {"finding_id": {"S": finding_id}})
    except Exception as exc:  # noqa: BLE001
        check(res, "issue-recheck-finding-read", False,
              on_fail=f"ConsistentRead Finding {finding_id!r} failed: "
                      f"{type(exc).__name__}: {exc}")
        return "", "", False
    if item is None:
        check(res, "issue-recheck-finding-read", False,
              on_fail=f"investigation Finding {finding_id!r} not found; "
                      "investigation may have failed silently")
        return "", "", False

    # Extract quality.root_cause.value.failure_mode_id from evidence list
    chosen_fm_id = ""
    evidence_raw = item.get("evidence", {})
    if isinstance(evidence_raw, dict) and "L" in evidence_raw:
        ev_list = evidence_raw["L"]
    elif isinstance(evidence_raw, list):
        ev_list = evidence_raw
    else:
        ev_list = []
    for ev in ev_list:
        if isinstance(ev, dict) and "M" in ev:
            ev = ev["M"]
        if not isinstance(ev, dict):
            continue
        if _sv(ev.get("kind") or "") != "quality.root_cause":
            continue
        val_raw = ev.get("value", {})
        if isinstance(val_raw, dict) and "M" in val_raw:
            val_raw = val_raw["M"]
        if isinstance(val_raw, dict):
            chosen_fm_id = _sv(val_raw.get("failure_mode_id") or "")
        break

    if not chosen_fm_id:
        check(res, "issue-recheck-finding-read", False,
              on_fail=f"Finding {finding_id!r} has no quality.root_cause.value.failure_mode_id")
        return "", "", False
    check(res, "issue-recheck-finding-read", True,
          f"Finding {finding_id!r} root_cause failure_mode_id={chosen_fm_id!r}")

    # Step 2: verify chosen FM is in safe_failure_modes
    if chosen_fm_id not in safe_fms:
        check(res, "issue-recheck-fm-in-safe", False,
              on_fail=f"investigation chose failure_mode_id={chosen_fm_id!r} which is NOT "
                      f"in safe_failure_modes={sorted(safe_fms)!r}; "
                      f"rules path and model disagree — do not approve")
        return chosen_fm_id, "", False
    expected_opt_id = safe_fms[chosen_fm_id]
    check(res, "issue-recheck-fm-in-safe", True,
          f"chosen failure_mode_id={chosen_fm_id!r} is in safe_failure_modes; "
          f"expected option_id={expected_opt_id!r}")

    # Step 3: re-read corpus row; verify it still passes safe rule with the expected option
    try:
        corpus_item = _read_ddb_item(
            ddb_adp, corpus_table,
            {"pk": {"S": f"fm#{chosen_fm_id}"}},
        )
    except Exception as exc:  # noqa: BLE001
        check(res, "issue-recheck-corpus-safe", False,
              on_fail=f"corpus re-read fm#{chosen_fm_id!r} failed: "
                      f"{type(exc).__name__}: {exc}")
        return chosen_fm_id, "", False
    if corpus_item is None:
        check(res, "issue-recheck-corpus-safe", False,
              on_fail=f"corpus fm#{chosen_fm_id!r} absent (corpus may have been rebuilt); "
                      "do not approve")
        return chosen_fm_id, "", False
    corpus_fm = _corpus_fm_to_dict(corpus_item)
    if not _is_safe_fm(corpus_fm):
        check(res, "issue-recheck-corpus-safe", False,
              on_fail=f"corpus fm#{chosen_fm_id!r} no longer passes safe rule "
                      "(cause_dimension, do_not_drive, or hardware_recall option changed); "
                      "do not approve")
        return chosen_fm_id, "", False
    live_opt_id = _hw_recall_opt_id(corpus_fm)
    if live_opt_id != expected_opt_id:
        check(res, "issue-recheck-corpus-safe", False,
              on_fail=f"corpus fm#{chosen_fm_id!r} hardware_recall option changed: "
                      f"expected {expected_opt_id!r}, live={live_opt_id!r}; "
                      "manifest option no longer valid — do not approve")
        return chosen_fm_id, "", False
    check(res, "issue-recheck-corpus-safe", True,
          f"corpus fm#{chosen_fm_id!r} still safe; option_id={live_opt_id!r} unchanged")

    # Step 4: verify Finding's quality.options contains the expected option_id
    # with remedy_type == "hardware_recall"
    finding_has_option = False
    for ev in ev_list:
        if isinstance(ev, dict) and "M" in ev:
            ev = ev["M"]
        if not isinstance(ev, dict):
            continue
        if _sv(ev.get("kind") or "") != "quality.options":
            continue
        val_raw = ev.get("value", {})
        if isinstance(val_raw, dict) and "L" in val_raw:
            opts_raw = val_raw["L"]
        elif isinstance(val_raw, list):
            opts_raw = val_raw
        else:
            opts_raw = []
        for opt in opts_raw:
            if isinstance(opt, dict) and "M" in opt:
                opt = opt["M"]
            if not isinstance(opt, dict):
                continue
            if (_sv(opt.get("remedy_option_id") or "") == expected_opt_id
                    and _sv(opt.get("remedy_type") or "") == "hardware_recall"):
                finding_has_option = True
                break
        break

    if not finding_has_option:
        check(res, "issue-recheck-finding-option", False,
              on_fail=f"Finding {finding_id!r} quality.options does not contain "
                      f"option_id={expected_opt_id!r} with remedy_type=hardware_recall; "
                      "do not approve with this option")
        return chosen_fm_id, "", False
    check(res, "issue-recheck-finding-option", True,
          f"Finding quality.options has option_id={expected_opt_id!r} "
          f"(remedy_type=hardware_recall)")

    return chosen_fm_id, expected_opt_id, True


def _record_vin_s3_keys(
    manifest: dict,
    manifest_path: str,
    stage: str,
    thread_id: str,
    action_ids: "set[str]",
) -> list[str]:
    """Merge each recorded thread version's ``affected_vins_s3_key`` into the manifest.

    F6e (review cycle 2): ``vin_s3_keys`` used to be recorded only at the end of
    ``issue`` (step 17), while the writer creates the VIN-list object at the decision
    approve.  A stop in between (the S1 draft refusal, the pre-issue window check, a
    crash) left a manifest cleanup couldn't use, and cleanup deletes the thread rows
    that name the key before it reaches the VIN objects.  So this runs right after the
    decision approve, again at step 17, and in cleanup before the thread rows go.

    Only rows whose ``action_id`` is one of *action_ids* (this run's) contribute.
    Existing keys are kept (union); the manifest is written under a signal hold only
    when a key was added.  Read errors are skipped here: cleanup's step 7c FAILs when
    no key is recorded, so a missed read stays visible.  Returns the key list.
    """
    records = manifest.setdefault("records", {})
    keys: list[str] = list(records.get("vin_s3_keys") or [])
    if not thread_id or not action_ids:
        return keys
    try:
        ddb_t = _client("dynamodb", _ADP_REGION)
        dt_table = _DIGITAL_THREAD_TABLE.format(stage=stage)
        versions = sorted(int(v) for v in (records.get("thread_versions") or []))
    except Exception:  # noqa: BLE001
        return keys
    added = False
    for ver in versions:
        try:
            row = _read_ddb_item(
                ddb_t, dt_table,
                {"thread_id": {"S": thread_id}, "version": {"N": str(ver)}},
            )
        except Exception:  # noqa: BLE001
            continue
        if not row:
            continue
        aid = row.get("action_id") or ""
        if isinstance(aid, dict):
            aid = aid.get("S", "")
        if aid not in action_ids:
            continue
        k = row.get("affected_vins_s3_key") or ""
        if isinstance(k, dict):
            k = k.get("S", "")
        if k and str(k) not in keys:
            keys.append(str(k))
            added = True
    if added:
        with _defer_signals_for_restore():
            records["vin_s3_keys"] = keys
            _write_manifest(manifest_path, manifest)
    return keys


def _recover_issue_records(
    res: Results, manifest: dict, manifest_path: str, stage: str,
) -> dict:
    """Fill in ``campaign_id`` / ``issue_finding_id`` for cleanup when issue never did.

    F6f (review cycle 3 W1).  Runs only when the manifest records a ``thread_id``, its
    ``thread_versions`` and a ``decision_action_id`` (the decision approve happened).
    - ``campaign_id``: the ``recall_campaign_id`` on a recorded thread row whose
      ``action_id`` is this run's decision action id (the writer created that draft).
    - ``issue_finding_id``: ``_find_quality_issue_finding`` (exactly one Finding with
      ``subject_key == "<thread_id>/issue"``; the thread id is this run's).
    Anything recovered is written to the manifest under a signal hold.  A read error,
    or a decided row naming no draft, is a FAIL row: cleanup must not report PASS
    while it can't tell whether a draft or an issue Finding was left behind.
    Returns the (possibly updated) ``records`` dict.
    """
    records = manifest.setdefault("records", {})
    thread_id = records.get("thread_id") or ""
    dec_aid = records.get("decision_action_id") or ""
    versions = records.get("thread_versions") or []
    if not (thread_id and dec_aid and versions):
        return records
    need_campaign = not (records.get("issued_campaign_id") or records.get("campaign_id"))
    need_finding = not records.get("issue_finding_id")
    if not (need_campaign or need_finding):
        return records
    recovered: dict[str, str] = {}
    if need_campaign:
        try:
            ddb_t = _client("dynamodb", _ADP_REGION)
            dt_table = _DIGITAL_THREAD_TABLE.format(stage=stage)
            cid = ""
            for ver in sorted(int(v) for v in versions):
                row = _read_ddb_item(
                    ddb_t, dt_table,
                    {"thread_id": {"S": thread_id}, "version": {"N": str(ver)}},
                )
                if not row:
                    continue
                aid = row.get("action_id") or ""
                if isinstance(aid, dict):
                    aid = aid.get("S", "")
                if aid != dec_aid:
                    continue
                c = row.get("recall_campaign_id") or ""
                if isinstance(c, dict):
                    c = c.get("S", "")
                if c:
                    cid = str(c)
                    break
            if cid and _CAMPAIGN_ID_RE.fullmatch(cid):
                recovered["campaign_id"] = cid
            elif cid:
                check(res, "cleanup-recover-campaign-id", False,
                      on_fail=f"decided thread row names campaign {cid!r}, which fails "
                              f"{_CAMPAIGN_ID_RE.pattern!r}; not deleting it")
            else:
                check(res, "cleanup-recover-campaign-id", False,
                      on_fail=f"campaign_id not in the manifest and no decided row of thread "
                              f"{thread_id!r} (action {dec_aid!r}) names a recall draft; "
                              f"check adp-{stage}-recall-campaigns-issued for thread_id="
                              f"{thread_id!r} by hand")
        except Exception as exc:  # noqa: BLE001
            check(res, "cleanup-recover-campaign-id", False,
                  on_fail=f"reading thread {thread_id!r} to recover the draft id raised: "
                          f"{type(exc).__name__}: {exc}")
    if need_finding:
        try:
            ddb_f = _client("dynamodb", _AVX_REGION)
            found = _find_quality_issue_finding(ddb_f, stage, thread_id)
            if found:
                fid = found.get("finding_id") or ""
                if isinstance(fid, dict):
                    fid = fid.get("S", "")
                if fid:
                    recovered["issue_finding_id"] = str(fid)
        except Exception as exc:  # noqa: BLE001
            check(res, "cleanup-recover-issue-finding-id", False,
                  on_fail=f"scanning for the quality.issue Finding of thread {thread_id!r} "
                          f"raised: {type(exc).__name__}: {exc}")
    if recovered:
        with _defer_signals_for_restore():
            records.update(recovered)
            _write_manifest(manifest_path, manifest)
        check(res, "cleanup-recover-issue-records", True,
              f"recovered {sorted(recovered.items())!r} from this run's thread "
              f"{thread_id!r} (absent from the manifest)")
    return records


# ── part issue ────────────────────────────────────────────────────────────────

def _part_issue(res: Results, *, stage: str, run_id: str, allow_writes: bool = False, **_: Any) -> None:
    """Part issue: temp user; investigation POST/poll; decision approve; issue_recall approve.

    Brief §issue (--allow-writes).  Re-checks every pre precondition at the start;
    also re-checks windows before each write.  Every id goes into the manifest before
    or atomically with the write (signals held around each create+record pair).

    Checks at each step (brief §issue):
      - DDB draft/issued row in adp-{stage}-recall-campaigns-issued
      - Lake object key + SSE-KMS head in S3
      - Thread versions v1 (decided) + v2 (issued) in adp-{stage}-digital-thread
    """
    # 1. --allow-writes guard (brief §issue: "temp users; investigation POST ... decision")
    if not allow_writes:
        res.skip(
            "issue",
            "requires --allow-writes; re-run with --allow-writes flag",
        )
        return

    _init_session("issue")
    print(f"\n── issue: stage={stage!r} run_id={run_id!r}")

    # ── C9 fix: read manifest FIRST so window/precondition checks can return
    # ── before any write.  Tests can provide a real manifest and assert no write.
    # ── 1b. Read manifest ─────────────────────────────────────────────────────
    manifest_path = _manifest_path(run_id)
    try:
        manifest = _read_manifest(manifest_path)
    except FileNotFoundError:
        check(res, "issue-manifest-read", False,
              on_fail=f"manifest not found at {manifest_path!r}; run --part pre first")
        return
    except Exception as exc:  # noqa: BLE001
        check(res, "issue-manifest-read", False,
              on_fail=f"manifest read failed: {type(exc).__name__}: {exc}")
        return

    # ── W2 fix: refuse re-run over a completed manifest ────────────────────────
    if manifest.get("records", {}).get("part_issue_completed_at"):
        check(res, "issue-already-completed", False,
              on_fail=f"manifest already has part_issue_completed_at="
                      f"{manifest['records']['part_issue_completed_at']!r}; "
                      f"issue was already run for this run_id; "
                      f"re-running would orphan the first investigation set")
        return

    # ── W7 fix: refuse issue if pre did not pass with non-empty expected_vins ──
    # pre writes pre_verdict="passed" only when all checks passed AND
    # expected_vins is non-empty; a failed/missing verdict means pre checks
    # failed or the expected-VIN query produced an empty set.
    pre_verdict_chk = manifest.get("pre_verdict", "")
    if pre_verdict_chk != "passed":
        check(res, "issue-pre-verdict", False,
              on_fail=f"pre_verdict={pre_verdict_chk!r} (expected 'passed'); "
                      "re-run --part pre; a failed or absent pre_verdict means "
                      "a pre check failed or expected_vins was empty")
        return
    check(res, "issue-pre-verdict", True,
          f"pre ran OK: pre_verdict={pre_verdict_chk!r}")

    # ── 2. Re-check windows ───────────────────────────────────────────────────
    maint_reason: str | None = None
    window_reason: str | None = None
    try:
        now = _utc_now()
        maint_reason = _maint_window_guard(now)
        if maint_reason:
            check(res, "issue-windows-maintenance", False, on_fail=maint_reason)
        else:
            check(res, "issue-windows-maintenance", True,
                  f"not in maintenance window ({now.strftime('%H:%M')} UTC)")
        sched_client = _client("scheduler", _AVX_REGION)
        window_reason = _schedule_guard(sched_client, _SCHEDULES, stage, now)
        if window_reason:
            check(res, "issue-windows-schedule", False, on_fail=window_reason)
        else:
            check(res, "issue-windows-schedule", True,
                  f"not within {_SCHED_GUARD_MIN} min of any scheduled job")
    except Exception as exc:  # noqa: BLE001
        check(res, "issue-windows", False,
              on_fail=f"window check raised: {type(exc).__name__}: {exc}")
        return  # C9 fix: return on exception before first write
    # C9 fix: return after any window FAIL, before first write
    if maint_reason or window_reason:
        return

    # ── 3. Re-check preconditions (same as pre) ───────────────────────────────
    try:
        lam = _client("lambda", _AVX_REGION)
        fn_name = _TIER2_FN.format(stage=stage, region=_AVX_REGION)
        if not _check_tier2_code(res, lam, fn_name):
            check(res, "issue-precondition-tier2", False,
                  on_fail="Tier 2 Lambda does not carry 9.3's code (user rule 2026-10-20); "
                          "deploy 9.3 before proceeding")
            return
    except Exception as exc:  # noqa: BLE001
        check(res, "issue-precondition-tier2", False,
              on_fail=f"tier2 code re-check raised: {type(exc).__name__}: {exc}")
        return

    try:
        glue = _client("glue", _ADP_REGION)
        recall_table_ok = _check_recall_table_exists(res, glue, stage)
        view_sql_ok = _check_view_sql_unions_issued(res, glue, stage)
    except Exception as exc:  # noqa: BLE001
        check(res, "issue-precondition-glue", False,
              on_fail=f"Glue re-check raised: {type(exc).__name__}: {exc}")
        return  # C9 fix: return on exception
    # C9 fix: return on Glue precondition FAIL
    if not recall_table_ok or not view_sql_ok:
        return

    try:
        lam_esm = _client("lambda", _AVX_REGION)
        if not _check_esm_enabled(res, lam_esm, stage, _AVX_REGION):
            check(res, "issue-precondition-esm", False,
                  on_fail="ESM is not Enabled; no investigation POST may be made "
                          "(user rule 2026-10-20)")
            return
    except Exception as exc:  # noqa: BLE001
        check(res, "issue-precondition-esm", False,
              on_fail=f"ESM re-check raised: {type(exc).__name__}: {exc}")
        return

    try:
        sqs = _client("sqs", _AVX_REGION)
        dlq_ok = _check_dlq_empty(res, sqs, stage)
    except Exception as exc:  # noqa: BLE001
        check(res, "issue-precondition-dlq", False,
              on_fail=f"DLQ re-check raised: {type(exc).__name__}: {exc}")
        return  # C9 fix: return on exception
    # C9 fix: return on DLQ FAIL
    if not dlq_ok:
        return

    try:
        idp_chk = _client("cognito-idp", _AVX_REGION)
        livetest_ok = _check_no_open_livetest(res, idp_chk, stage)
    except Exception as exc:  # noqa: BLE001
        check(res, "issue-precondition-livetest", False,
              on_fail=f"open-livetest re-check raised: {type(exc).__name__}: {exc}")
        return  # C9 fix: return on exception
    # C9 fix: return on open-livetest FAIL
    if not livetest_ok:
        return

    pattern_id: str = manifest.get("pattern_id") or ""
    account: str = manifest.get("account") or ""
    # F6b: safe_failure_modes required — refuse a manifest without it
    safe_fms_manifest: dict = manifest.get("safe_failure_modes") or {}
    if not safe_fms_manifest:
        check(res, "issue-manifest-safe-failure-modes", False,
              on_fail="manifest has no safe_failure_modes; re-run --part pre")
        return
    if not pattern_id:
        check(res, "issue-manifest-pattern-id", False,
              on_fail="manifest has no pattern_id; re-run --part pre")
        return
    # K1: verify manifest stage matches this run's stage (brief §K1)
    manifest_stage = manifest.get("stage") or ""
    if manifest_stage != stage:
        check(res, "issue-manifest-stage", False,
              on_fail=f"manifest stage={manifest_stage!r} != --stage={stage!r}; "
                      f"run --part pre with the same --stage")
        return
    check(res, "issue-manifest-read", True,
          f"manifest read: pattern_id={pattern_id!r} account={account!r} "
          f"stage={manifest_stage!r}")

    # ── 3b. S9 fix: re-check corpus component_name before the POST ────────────
    # The manifest may be days old.  Verify the corpus still carries a
    # component_name for this failure mode; a missing name means the ADP corpus
    # was rebuilt without this entry.
    predicted_fm_id: str = manifest.get("predicted_failure_mode_id") or ""
    if predicted_fm_id:
        try:
            corpus_tbl = _ENGINEERING_CORPUS_TABLE.format(stage=stage)
            ddb_corpus = _client("dynamodb", _ADP_REGION)
            corpus_chk = _read_ddb_item(
                ddb_corpus, corpus_tbl,
                {"pk": {"S": f"fm#{predicted_fm_id}"}},
            )
            cn = str((corpus_chk or {}).get("component_name") or "").strip()
            if cn:
                check(res, "issue-corpus-component-name", True,
                      f"corpus fm#{predicted_fm_id} has component_name={cn!r}")
            else:
                check(res, "issue-corpus-component-name", False,
                      on_fail=f"corpus fm#{predicted_fm_id} missing component_name; "
                              "brief §Preconditions: corpus must carry component_name before POST")
                return
        except Exception as exc:  # noqa: BLE001
            check(res, "issue-corpus-component-name", False,
                  on_fail=f"corpus component_name re-check raised: "
                          f"{type(exc).__name__}: {exc}")
            return

    # ── 5. Get AVX API URL from CloudFormation ────────────────────────────────
    avx_api_url = ""
    try:
        cfn = _client("cloudformation", _AVX_REGION)
        avx_api_url = _get_avx_api_url(cfn, stage)
        check(res, "issue-api-url", True, f"API URL={avx_api_url!r}")
    except Exception as exc:  # noqa: BLE001
        check(res, "issue-api-url", False,
              on_fail=f"get AVX API URL failed: {type(exc).__name__}: {exc}")
        return

    # ── 6. Cognito: resolve pool + password-auth client ───────────────────────
    pool_id = ""
    client_id = ""
    try:
        idp = _client("cognito-idp", _AVX_REGION)
        pool_id = _resolve_pool_id(idp)
        client_id = _password_client(idp, pool_id)
        check(res, "issue-cognito-pool", True,
              f"pool_id={pool_id!r} client_id={client_id!r}")
    except Exception as exc:  # noqa: BLE001
        check(res, "issue-cognito-pool", False,
              on_fail=f"Cognito setup failed: {type(exc).__name__}: {exc}")
        return

    # ── 7. Create temp product-engineer user; record in manifest ──────────────
    # W6 fix: the username is deterministic; record it BEFORE AdminCreateUser so
    # cleanup can delete it even if a later _create_temp_user call fails.
    username = f"{_LIVETEST_USER_PREFIX}{run_id}-pe@example.invalid"
    eng_token = ""
    try:
        idp = _client("cognito-idp", _AVX_REGION)
        manifest.setdefault("records", {}).setdefault("users", [])
        if username not in manifest["records"]["users"]:
            manifest["records"]["users"].append(username)
            _write_manifest(manifest_path, manifest)
        with _defer_signals_for_restore():
            _username_actual, _pw, eng_token = _create_temp_user(
                idp, pool_id, client_id, run_id, role="pe"
            )
        check(res, "issue-create-user", True,
              f"temp user {username!r} created and added to product-engineer group")
    except Exception as exc:  # noqa: BLE001
        check(res, "issue-create-user", False,
              on_fail=f"create temp user failed: {type(exc).__name__}: {exc}")
        return

    # From here: try to delete user on any early return.
    def _best_effort_delete_user() -> None:
        if not username:
            return
        try:
            _idp = _client("cognito-idp", _AVX_REGION)
            with _defer_signals_for_restore():
                _delete_temp_user(_idp, pool_id, username)
        except Exception:  # noqa: BLE001
            pass

    # ── 8. Re-check window before investigation POST ──────────────────────────
    try:
        now2 = _utc_now()
        maint_r2 = _maint_window_guard(now2)
        if maint_r2:
            check(res, "issue-pre-post-window", False, on_fail=maint_r2)
            _best_effort_delete_user()
            return
        sched_cl2 = _client("scheduler", _AVX_REGION)
        sched_r2 = _schedule_guard(sched_cl2, _SCHEDULES, stage, now2)
        if sched_r2:
            check(res, "issue-pre-post-window", False, on_fail=sched_r2)
            _best_effort_delete_user()
            return
        # Brief §Windows: "issue must finish before next recall run start minus 45 min"
        # and "received_date <= that run's UTC date."
        # The recall run is cron(0 5 * * ? *) UTC; 45-min guard already checked above.
        check(res, "issue-pre-post-window", True,
              f"window OK at {now2.strftime('%H:%M')} UTC")
    except Exception as exc:  # noqa: BLE001
        check(res, "issue-pre-post-window", False,
              on_fail=f"pre-POST window check raised: {type(exc).__name__}: {exc}")
        _best_effort_delete_user()
        return

    # ── 8b. W1 fix: re-read pattern from DDB (ConsistentRead) right before POST ─
    # The manifest may be days old; verify the pattern still exists and is supplier_lot.
    try:
        ddb_adp_w1 = _client("dynamodb", _ADP_REGION)
        patterns_table = _FAULT_PATTERNS_TABLE.format(stage=stage)
        pattern_fresh = _read_ddb_item(
            ddb_adp_w1, patterns_table,
            {"pattern_id": {"S": pattern_id}},
        )
        if pattern_fresh is None:
            check(res, "issue-pre-post-pattern-reread", False,
                  on_fail=f"pattern {pattern_id!r} no longer in {patterns_table!r}; "
                          f"manifest may be stale — re-run --part pre")
            _best_effort_delete_user()
            return
        # W1: also verify it is still supplier_lot (brief §pattern pick)
        def _pv(v: Any) -> Any:
            if isinstance(v, dict) and len(v) == 1:
                k, inner = next(iter(v.items()))
                if k in ("S", "N", "BOOL"):
                    return inner
            return v
        cdim_fresh = str(_pv(pattern_fresh.get("cohort_dimension", "")))
        if cdim_fresh != "supplier_lot":
            check(res, "issue-pre-post-pattern-reread", False,
                  on_fail=f"pattern {pattern_id!r} cohort_dimension={cdim_fresh!r} "
                          f"(expected supplier_lot); manifest may reference a stale pattern")
            _best_effort_delete_user()
            return
        check(res, "issue-pre-post-pattern-reread", True,
              f"pattern {pattern_id!r} still present, cohort_dimension={cdim_fresh!r}")
    except Exception as exc:  # noqa: BLE001
        check(res, "issue-pre-post-pattern-reread", False,
              on_fail=f"pattern reread failed: {type(exc).__name__}: {exc}")
        _best_effort_delete_user()
        return

    # ── 9. POST investigation + record in manifest ────────────────────────────
    investigation_id = ""
    try:
        post_body = {"pattern_id": pattern_id}
        with _defer_signals_for_restore():
            status_code, resp_body = _http(
                avx_api_url, "POST", "investigations", eng_token, post_body
            )
            investigation_id = resp_body.get("investigation_id", "")
            # W2 fix: only record the id when the POST succeeded (202).
            # A 200 means another session holds the lock and the id is theirs;
            # recording it would cause cleanup to delete their investigation.
            # W9 fix: never overwrite a previously recorded investigation_id (would orphan it).
            if status_code == 202 and investigation_id:
                existing_inv_id = manifest.get("records", {}).get("investigation_id", "")
                if existing_inv_id and existing_inv_id != investigation_id:
                    check(res, "issue-post-investigation", False,
                          on_fail=f"manifest already has investigation_id="
                                  f"{existing_inv_id!r}; new id={investigation_id!r} "
                                  f"would orphan the first set — re-run cleanup first")
                    _best_effort_delete_user()
                    return
                manifest["records"]["investigation_id"] = investigation_id
                # C4 F3a: record the UTC date of the POST to derive the cap row key.
                # The handler writes cap#<UTC date at POST time>; we capture it inside
                # the same signal hold so cleanup always uses the right key.
                manifest["records"]["cap_date"] = _utc_now().date().isoformat()
                _write_manifest(manifest_path, manifest)

        ok = status_code == 202 and bool(investigation_id)
        check(res, "issue-post-investigation", ok,
              f"POST /investigations → {status_code} investigation_id={investigation_id!r}",
              on_fail=f"POST /investigations returned {status_code}: {resp_body!r}")
        if not ok:
            _best_effort_delete_user()
            return
    except Exception as exc:  # noqa: BLE001
        check(res, "issue-post-investigation", False,
              on_fail=f"POST /investigations raised: {type(exc).__name__}: {exc}")
        _best_effort_delete_user()
        return

    # ── 10. Poll investigation until completed/failed ─────────────────────────
    finding_id = ""
    try:
        poll_resp = _poll_investigation(avx_api_url, investigation_id, eng_token)
        inv_status = poll_resp.get("status", "")
        finding_id = poll_resp.get("finding_id", "")
        poll_ok = inv_status == "completed" and bool(finding_id)
        check(res, "issue-poll-investigation", poll_ok,
              f"investigation status={inv_status!r} finding_id={finding_id!r}",
              on_fail=f"investigation status={inv_status!r} "
                      f"finding_id={finding_id!r}; expected completed+finding_id: "
                      f"{poll_resp!r}")
        if not poll_ok:
            _best_effort_delete_user()
            return
    except Exception as exc:  # noqa: BLE001
        check(res, "issue-poll-investigation", False,
              on_fail=f"poll investigation raised: {type(exc).__name__}: {exc}")
        _best_effort_delete_user()
        return

    # ── 10.5 Record investigation_finding_id in manifest (signal hold) ────────
    # Brief §F6b step 1: record BEFORE decision approve so cleanup can delete
    # the Finding even when the re-check stops the run.
    try:
        with _defer_signals_for_restore():
            manifest["records"]["investigation_finding_id"] = finding_id
            _write_manifest(manifest_path, manifest)
        check(res, "issue-record-inv-finding-id", True,
              f"investigation_finding_id={finding_id!r} recorded in manifest")
    except Exception as exc:  # noqa: BLE001
        check(res, "issue-record-inv-finding-id", False,
              on_fail=f"recording investigation_finding_id raised: "
                      f"{type(exc).__name__}: {exc}")
        _best_effort_delete_user()
        return

    # ── 10.6 Re-check investigation Finding before decision approve ────────────
    # Brief §F6b steps 2-3: verify the investigation chose a safe failure mode;
    # verify corpus row still passes safe rule; verify Finding's quality.options
    # contains the expected hardware_recall option.
    option_id = ""
    chosen_fm_id = ""
    try:
        ddb_avx_rc = _client("dynamodb", _AVX_REGION)
        ddb_adp_rc = _client("dynamodb", _ADP_REGION)
        chosen_fm_id, option_id, recheck_ok = _recheck_investigation_finding(
            res, ddb_avx_rc, ddb_adp_rc, stage, finding_id, safe_fms_manifest
        )
        if not recheck_ok:
            # Brief §F6b: FAIL and stop — no decision approve
            _best_effort_delete_user()
            return
        # Record chosen_failure_mode_id (brief §F6b step 3 / step 4)
        with _defer_signals_for_restore():
            manifest["records"]["chosen_failure_mode_id"] = chosen_fm_id
            _write_manifest(manifest_path, manifest)
    except Exception as exc:  # noqa: BLE001
        check(res, "issue-recheck-finding-read", False,
              on_fail=f"investigation Finding re-check raised: "
                      f"{type(exc).__name__}: {exc}")
        _best_effort_delete_user()
        return

    if not option_id:
        _best_effort_delete_user()
        return

    # ── 12. Re-check window before decision approve ───────────────────────────
    try:
        now3 = _utc_now()
        maint_r3 = _maint_window_guard(now3)
        sched_r3 = None
        if not maint_r3:
            sched_cl3 = _client("scheduler", _AVX_REGION)
            sched_r3 = _schedule_guard(sched_cl3, _SCHEDULES, stage, now3)
        if maint_r3 or sched_r3:
            check(res, "issue-pre-decision-window", False,
                  on_fail=maint_r3 or sched_r3 or "window violation")
            _best_effort_delete_user()
            return
        check(res, "issue-pre-decision-window", True,
              f"window OK at {now3.strftime('%H:%M')} UTC before decision approve")
    except Exception as exc:  # noqa: BLE001
        check(res, "issue-pre-decision-window", False,
              on_fail=f"pre-decision window check raised: {type(exc).__name__}: {exc}")
        _best_effort_delete_user()
        return

    # ── 13. Approve record_engineering_decision ───────────────────────────────
    # Brief §issue: notes = "LIVETEST g9.3 <UTC date>" (hardware_recall option)
    notes = f"{_LIVETEST_NOTES_PREFIX}{run_id}"
    approve_body = {
        "action_kind": "record_engineering_decision",
        "target_system": "adp",
        "parameters": {"option_id": option_id, "notes": notes},
    }
    thread_id = ""
    draft_id = ""
    dec_thread_id = ""
    dec_action_id = ""
    try:
        with _defer_signals_for_restore():
            st_dec, resp_dec = _http(
                avx_api_url,
                "POST",
                f"findings/{finding_id}/approve",
                eng_token,
                approve_body,
            )
            # F6b: investigation_finding_id already recorded in step 10.5 (signal hold).
            # C4/W9 fix: record decision action_id; never overwrite an existing one.
            dec_action_id = resp_dec.get("action_id", "") if isinstance(resp_dec, dict) else ""
            if dec_action_id:
                existing_dec = manifest["records"].get("decision_action_id", "")
                if not existing_dec:
                    manifest["records"]["decision_action_id"] = dec_action_id
            # W3 F3b fix: record thread_id (from target_ref) and v1 inside the signal hold,
            # as soon as the decision executes — before any later step can fail.
            dec_thread_id = resp_dec.get("target_ref", "") if isinstance(resp_dec, dict) else ""
            if dec_thread_id and _THREAD_ID_RE.fullmatch(dec_thread_id):
                if not manifest["records"].get("thread_id"):
                    manifest["records"]["thread_id"] = dec_thread_id
                # Record v1 immediately; issue_recall will add v2.
                if not manifest["records"].get("thread_versions"):
                    manifest["records"]["thread_versions"] = [1]
            _write_manifest(manifest_path, manifest)
        ok_dec = (st_dec in (200, 202)
                  and isinstance(resp_dec, dict)
                  and resp_dec.get("status") == "executed"
                  and resp_dec.get("action_kind") == "record_engineering_decision")
        # C1 fix: extract thread_id from decision response's target_ref
        dec_thread_id = resp_dec.get("target_ref", "") if isinstance(resp_dec, dict) else ""
        check(res, "issue-decision-approve", ok_dec,
              f"POST /findings/{finding_id}/approve → {st_dec} (record_engineering_decision)",
              on_fail=f"POST /findings/{finding_id}/approve returned {st_dec}: "
                      f"{resp_dec!r}")
        if not ok_dec:
            _best_effort_delete_user()
            return
    except Exception as exc:  # noqa: BLE001
        check(res, "issue-decision-approve", False,
              on_fail=f"approve record_engineering_decision raised: "
                      f"{type(exc).__name__}: {exc}")
        _best_effort_delete_user()
        return

    # F6e: record the VIN-list key the writer made at the decision, before any later
    # stop (S1 refusal, window check), so cleanup can delete that object.
    if dec_thread_id and _THREAD_ID_RE.fullmatch(dec_thread_id) and dec_action_id:
        try:
            _record_vin_s3_keys(manifest, manifest_path, stage, dec_thread_id, {dec_action_id})
        except Exception as exc:  # noqa: BLE001
            check(res, "issue-record-vin-keys", False,
                  on_fail=f"recording VIN-list keys after the decision raised: "
                          f"{type(exc).__name__}: {exc}")
            _best_effort_delete_user()
            return

    # ── 14. Check DDB draft row + quality.issue Finding ──────────────────────
    # Brief §issue checks: DDB draft row after record_engineering_decision.
    issue_finding_id = ""
    draft_ok = False
    try:
        ddb_adp = _client("dynamodb", _ADP_REGION)
        ddb_avx3 = _client("dynamodb", _AVX_REGION)

        # C1 fix: pass dec_thread_id (from decision response target_ref) to filter
        # the quality.issue Finding by subject_key == f"{dec_thread_id}/issue"
        issue_finding_id, thread_id, draft_id = _check_quality_issue_finding(
            res, ddb_avx3, stage, dec_thread_id
        )
        if thread_id and draft_id:
            # C9 fix: capture draft_ok to gate issue_recall approve
            draft_ok = _check_ddb_draft_row(res, ddb_adp, stage, draft_id, thread_id)
        else:
            check(res, "issue-ddb-draft", False,
                  on_fail=f"thread_id={thread_id!r} draft_id={draft_id!r} "
                          f"from quality.issue Finding; cannot check draft row")
    except Exception as exc:  # noqa: BLE001
        check(res, "issue-draft-checks", False,
              on_fail=f"draft row / quality.issue checks raised: "
                      f"{type(exc).__name__}: {exc}")

    # Update manifest with thread/draft ids as soon as we have them
    if thread_id or draft_id or issue_finding_id:
        manifest["records"]["thread_id"] = thread_id
        manifest["records"]["campaign_id"] = draft_id
        manifest["records"]["issue_finding_id"] = issue_finding_id
        with _defer_signals_for_restore():
            _write_manifest(manifest_path, manifest)

    # C9 fix: gate issue_recall on both issue_finding_id and draft_ok
    if not issue_finding_id or not draft_ok:
        _best_effort_delete_user()
        return

    # ── 15. Re-check window before issue_recall approve ───────────────────────
    try:
        now4 = _utc_now()
        maint_r4 = _maint_window_guard(now4)
        sched_r4 = None
        if not maint_r4:
            sched_cl4 = _client("scheduler", _AVX_REGION)
            sched_r4 = _schedule_guard(sched_cl4, _SCHEDULES, stage, now4)
        if maint_r4 or sched_r4:
            check(res, "issue-pre-issue-window", False,
                  on_fail=maint_r4 or sched_r4 or "window violation")
            _best_effort_delete_user()
            return
        check(res, "issue-pre-issue-window", True,
              f"window OK at {now4.strftime('%H:%M')} UTC before issue_recall approve")
    except Exception as exc:  # noqa: BLE001
        check(res, "issue-pre-issue-window", False,
              on_fail=f"pre-issue-recall window check raised: {type(exc).__name__}: {exc}")
        _best_effort_delete_user()
        return

    # ── 16. Approve issue_recall ──────────────────────────────────────────────
    # Brief §issue: "issue approve" on the quality.issue Finding.
    # g93lv-facts.md §Approve route: body parameters is {} (server-set from Finding)
    issue_body = {
        "action_kind": "issue_recall",
        "target_system": "adp",
        "parameters": {},
    }
    issue_action_id = ""
    try:
        with _defer_signals_for_restore():
            st_iss, resp_iss = _http(
                avx_api_url,
                "POST",
                f"findings/{issue_finding_id}/approve",
                eng_token,
                issue_body,
            )
            manifest["records"]["issued_campaign_id"] = draft_id
            # C4/W9 fix: record issue action_id; never overwrite an existing one.
            issue_action_id = resp_iss.get("action_id", "") if isinstance(resp_iss, dict) else ""
            if issue_action_id:
                existing_iss = manifest["records"].get("issue_action_id", "")
                if not existing_iss:
                    manifest["records"]["issue_action_id"] = issue_action_id
            # W3 F3b fix: record v2 inside the signal hold as soon as issue_recall executes.
            existing_vs = list(manifest["records"].get("thread_versions") or [])
            if 2 not in existing_vs:
                manifest["records"]["thread_versions"] = sorted(set(existing_vs) | {2})
            _write_manifest(manifest_path, manifest)
        ok_iss = (st_iss in (200, 202)
                  and isinstance(resp_iss, dict)
                  and resp_iss.get("status") == "executed"
                  and resp_iss.get("action_kind") == "issue_recall")
        check(res, "issue-issue-recall-approve", ok_iss,
              f"POST /findings/{issue_finding_id}/approve → {st_iss} (issue_recall)",
              on_fail=f"POST /findings/{issue_finding_id}/approve returned {st_iss}: "
                      f"{resp_iss!r}")
        if not ok_iss:
            _best_effort_delete_user()
            return
    except Exception as exc:  # noqa: BLE001
        check(res, "issue-issue-recall-approve", False,
              on_fail=f"approve issue_recall raised: {type(exc).__name__}: {exc}")
        _best_effort_delete_user()
        return

    # ── 17. Post-issue checks: DDB issued row, S3 key/SSE-KMS, thread versions ─
    try:
        ddb_adp2 = _client("dynamodb", _ADP_REGION)
        _check_ddb_issued_row(res, ddb_adp2, stage, draft_id)
    except Exception as exc:  # noqa: BLE001
        check(res, "issue-ddb-issued", False,
              on_fail=f"DDB issued row check raised: {type(exc).__name__}: {exc}")

    try:
        s3 = _client("s3", _ADP_REGION)
        _check_s3_issued_recall(res, s3, stage, account, draft_id)
    except Exception as exc:  # noqa: BLE001
        check(res, "issue-s3-key", False,
              on_fail=f"S3 issued-recall check raised: {type(exc).__name__}: {exc}")

    try:
        ddb_adp3 = _client("dynamodb", _ADP_REGION)
        # W3 F4b: pass this run's action_ids so _check_thread_versions filters out
        # any version written by another run that shares this thread_id.
        _expected_aids = {a for a in [dec_action_id, issue_action_id] if a}
        _thread_versions_found = _check_thread_versions(
            res, ddb_adp3, stage, thread_id,
            expected_action_ids=_expected_aids if _expected_aids else None,
        )
        # W3 F3b: thread_versions is now already recorded at decision/issue_recall time
        # (inside the signal holds).  _check_thread_versions is a verification-only step:
        # only update the manifest if it adds versions the signal holds didn't record
        # (e.g. a retry that sees a version created by an earlier partial run).
        if _thread_versions_found:
            existing_tv = sorted(int(v) for v in (manifest["records"].get("thread_versions") or []))
            combined = sorted(set(existing_tv) | set(_thread_versions_found))
            if combined != existing_tv:
                manifest["records"]["thread_versions"] = combined
    except Exception as exc:  # noqa: BLE001
        check(res, "issue-thread-versions", False,
              on_fail=f"thread-versions check raised: {type(exc).__name__}: {exc}")

    # S4 F3b fix: record VIN-list S3 keys from each recorded version's affected_vins_s3_key.
    # F6e: the same helper already ran right after the decision approve; this call adds
    # any key the issued version carries.
    _record_vin_s3_keys(manifest, manifest_path, stage, thread_id,
                        {a for a in [dec_action_id, issue_action_id] if a})

    # ── 18. Final manifest update ─────────────────────────────────────────────
    # S12 fix: only write part_issue_completed_at when every check in issue passed.
    issue_failed = any(r[1] == "FAIL" for r in res.rows)
    if not issue_failed:
        manifest["records"]["part_issue_completed_at"] = _utc_now().isoformat()
    with _defer_signals_for_restore():
        _write_manifest(manifest_path, manifest)
    check(res, "issue-manifest-final", True,
          f"manifest updated with all ids at {manifest_path!r}")


# ── placeholder parts ─────────────────────────────────────────────────────────

# ── after-run helpers ─────────────────────────────────────────────────────────

# ── reconcile_thread_lake.py helpers (F2g) ────────────────────────────────────


def _run_reconcile_subprocess(stage: str) -> tuple[int, dict | None]:
    """Run ADP reconcile_thread_lake.py (dry run, pinned to _ADP_RECONCILE_COMMIT).

    Uses ``git archive`` to extract the script and its handler dependency into a
    temp dir (no .git → _checkout() returns git_head=None; acceptable for read-only
    dry run).  Runs it with the ADP venv's Python.

    Returns (returncode, report_dict_or_None).  report_dict is None when stdout is
    not parseable JSON.  Exit 2 or unparsable output is treated as FAIL by callers.

    Subprocess commands: ``git archive`` and ``reconcile_thread_lake.py`` — both
    listed in _SUBPROCESS_ALLOWLIST.
    Source: ADP _ADP_RECONCILE_COMMIT:platform-foundation/scripts/reconcile_thread_lake.py.
    """
    import subprocess  # noqa: PLC0415
    import tempfile  # noqa: PLC0415
    import tarfile  # noqa: PLC0415
    import io as _io  # noqa: PLC0415

    adp_repo = _ADP_REPO
    if not os.path.isdir(adp_repo):
        return 2, None

    # Extract script + handler via git archive (read-only; no write to ADP repo).
    with tempfile.TemporaryDirectory(prefix="reconcile_tl_") as tmpdir:
        arc_result = subprocess.run(
            ["git", "-C", adp_repo, "archive", _ADP_RECONCILE_COMMIT,
             "--", _ADP_RECONCILE_SCRIPT_PATH, _ADP_RECONCILE_HANDLER_PATH],
            capture_output=True, check=False,
        )
        if arc_result.returncode != 0:
            return 2, None

        try:
            with tarfile.open(fileobj=_io.BytesIO(arc_result.stdout)) as tf:
                tf.extractall(tmpdir)
        except Exception:  # noqa: BLE001
            return 2, None

        script_path = os.path.join(tmpdir, _ADP_RECONCILE_SCRIPT_PATH)
        if not os.path.isfile(script_path):
            return 2, None
        if not os.path.isfile(_ADP_PYTHON):
            return 2, None

        run_result = subprocess.run(
            [_ADP_PYTHON, script_path, "--stage", stage],
            capture_output=True, text=True, check=False,
        )
        rc = run_result.returncode
        try:
            report = json.loads(run_result.stdout) if run_result.stdout.strip() else None
        except Exception:  # noqa: BLE001
            report = None
        return rc, report


def _run_reconcile_check(
    res: Results, stage: str, thread_id: str,
    label_prefix: str, *, after_cleanup: bool,
) -> None:
    """Run reconcile_thread_lake.py (dry run) and check the result for ``thread_id``.

    after_cleanup=False (after-run): FAIL if report has missing/differs/unprojectable
    entries for this thread.  Other threads' entries are ignored.
    after_cleanup=True (after cleanup): FAIL if report has ANY entry (missing, differs,
    extra, unprojectable) for this thread.

    Exit 2 or unparsable output → FAIL with label ``{label_prefix}-run``.
    No entries for this thread → PASS with label ``{label_prefix}-ok``.
    """
    try:
        rc, report = _run_reconcile_subprocess(stage)
    except Exception as exc:  # noqa: BLE001
        check(res, f"{label_prefix}-run", False,
              on_fail=f"reconcile subprocess raised: {type(exc).__name__}: {exc}")
        return

    if rc == 2 or report is None:
        check(res, f"{label_prefix}-run", False,
              on_fail=f"reconcile_thread_lake.py exited {rc} or produced unparsable "
                      f"output (stdout={'' if report is None else 'non-JSON'}); "
                      f"this is a FAIL (exit 2 = precondition/AWS error)")
        return

    check(res, f"{label_prefix}-run", True,
          f"reconcile_thread_lake.py exited {rc} (mode=dry-run)")

    # Filter entries for this thread_id.
    flat_pfx = f"curated/engineering/digital_thread/{thread_id}.v"
    bad: list[str] = []

    for cat in ("missing", "differs", "unprojectable"):
        entries = report.get(cat) or []
        for entry in entries:
            # W5 F3b fix: reconcile() emits dicts for unprojectable entries
            # ({"key": ..., "missing_columns": [...]}).  Extract the "key" field
            # from dicts; don't stringify and search — that would turn
            # {"key": "curated/.../thread-X.v1.json", ...} into the whole repr.
            if isinstance(entry, str):
                key = entry
            elif isinstance(entry, dict):
                key = entry.get("key") or ""
            else:
                key = str(entry)
            if key and (thread_id in key or key.startswith(flat_pfx)):
                bad.append(f"{cat}: {key!r}")

    if after_cleanup:
        # Also check extra: after cleanup, no entry of any kind for this thread.
        for entry in (report.get("extra") or []):
            key = entry if isinstance(entry, str) else str(entry)
            if thread_id in key or key.startswith(flat_pfx):
                bad.append(f"extra: {key!r}")

    if bad:
        check(res, f"{label_prefix}-ok", False,
              on_fail=f"reconcile_thread_lake.py dry run reports entries for thread "
                      f"{thread_id!r}: {bad!r}")
    else:
        check(res, f"{label_prefix}-ok", True,
              f"reconcile_thread_lake.py dry run: no "
              f"{'entry' if after_cleanup else 'missing/differs/unprojectable'} "
              f"for thread {thread_id!r}")


def _athena_query_paged(
    athena: Any, stage: str, sql: str, label: str
) -> list[dict[str, str]]:
    """Run an Athena query (WorkGroup=cvx-{stage}-analytics); return all rows as dicts.

    Pages through GetQueryResults following NextToken.  Raises on FAILED/CANCELLED.
    Returns header-less dicts keyed by column name.
    Source: agents/tier2/runner.py:1566 workgroup default = "cvx-{stage}-analytics".
    """
    import time as _time  # noqa: PLC0415
    wg = _ATHENA_WORKGROUP.format(stage=stage)
    eng_db = _GLUE_ENGINEERING_DB.format(stage=stage)
    exec_r = athena.start_query_execution(
        QueryString=sql,
        QueryExecutionContext={"Database": eng_db},
        WorkGroup=wg,
    )
    qid = exec_r["QueryExecutionId"]
    for _attempt in range(60):
        exec_s = athena.get_query_execution(QueryExecutionId=qid)
        state = exec_s.get("QueryExecution", {}).get("Status", {}).get("State", "")
        if state == "SUCCEEDED":
            break
        if state in ("FAILED", "CANCELLED"):
            reason = (
                exec_s.get("QueryExecution", {})
                .get("Status", {})
                .get("StateChangeReason", state)
            )
            raise RuntimeError(f"Athena query {label!r} {state}: {reason}")
        _time.sleep(0.5)
    else:
        raise TimeoutError(f"Athena query {label!r} timed out (state={state!r})")

    header: list[str] = []
    rows_out: list[dict[str, str]] = []
    next_token: str | None = None
    first_page = True
    while True:
        kwargs: dict[str, Any] = {"QueryExecutionId": qid}
        if next_token:
            kwargs["NextToken"] = next_token
        page_r = athena.get_query_results(**kwargs)
        raw_rows = page_r.get("ResultSet", {}).get("Rows", [])
        for i, row in enumerate(raw_rows):
            cells = [c.get("VarCharValue", "") for c in row.get("Data", [])]
            if first_page and i == 0:
                header = cells
                continue
            if header:
                rows_out.append(dict(zip(header, cells)))
        first_page = False
        next_token = page_r.get("NextToken")
        if not next_token:
            break
    return rows_out


def _query_findings_by_campaign(
    ddb: Any, stage: str, campaign_id: str
) -> list[dict]:
    """Query AVX findings for all rows with subject_key starting with campaign_id.

    Uses the by_fleet GSI? No — uses the main table scan with FilterExpression
    on subject_key prefix.  Brief §after-run: "per-VIN recall Findings".
    The Tier 2 writer stores subject_key as "{campaign_id}/{action}/{attempt}".
    Source: recall.py:512 subject_key = f"{row['campaign_id']}/{action}/{attempt}".

    Full-paged scan with FilterExpression (brief §Manifest: "page fully, no Limit").
    Returns unwrapped item dicts.
    """
    table = _FINDINGS_TABLE.format(stage=stage)
    prefix = f"{campaign_id}/"
    items: list[dict[str, Any]] = []
    kwargs: dict[str, Any] = {
        "TableName": table,
        "FilterExpression": "begins_with(subject_key, :pfx) AND finding_kind = :kind",
        "ExpressionAttributeValues": {
            ":pfx": {"S": prefix},
            ":kind": {"S": _RECALL_FINDING_KIND},
        },
    }
    while True:
        page = ddb.scan(**kwargs)
        for item in page.get("Items", []):
            items.append(_unwrap_ddb_item(item))
        if not page.get("LastEvaluatedKey"):
            break
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]
    return items


def _unwrap_ddb_item(item: dict) -> dict:
    """Unwrap AttributeValue wrappers one level deep (S, N, BOOL, NULL, L, M)."""
    def _v(x: Any) -> Any:
        if not isinstance(x, dict):
            return x
        if len(x) == 1:
            k, val = next(iter(x.items()))
            if k == "S":
                return val
            if k == "N":
                return val
            if k == "BOOL":
                return val
            if k == "NULL":
                return None
            if k == "L":
                return [_v(i) for i in val]
            if k == "M":
                return {ik: _v(iv) for ik, iv in val.items()}
        return x
    return {k: _v(v) for k, v in item.items()}


def _ev_val_by_kind(evidence: Any, kind: str) -> Any:
    """Return the value of the first evidence entry with the given kind, or None.

    C5 fix: real recall Findings carry evidence as a list of
    {kind, value, source, source_ref, computed_at, provenance, [vin]}
    (agents/fi_recall_warranty/recall.py _to_schema_evidence, :178-209).
    """
    if not isinstance(evidence, list):
        return None
    for e in evidence:
        if isinstance(e, dict) and e.get("kind") == kind:
            return e.get("value")
    return None


def _check_recall_findings(
    res: Results, ddb: Any, stage: str,
    campaign_id: str, expected_vins: list[str],
    catalog_row: "dict | None" = None,
    cause_value: str = "",
) -> list[dict]:
    """Check AVX recall Findings for each expected VIN after the recall run.

    Brief §after-run: "per-VIN recall Findings (item types for parts weeks out,
    evidence recall.rule carries component_lots and null years, disclosure_txt
    byte for byte from the received view)".

    C5 fix: evidence is a list of {kind, value, ...}; parse by kind.
    C6 fix: assert set equality (expected == found); FAIL on empty expected.

    catalog_row: optional dict with keys summary, consequence, remedy from
    recall_campaigns_received, used for byte-exact disclosure_txt comparison.

    cause_value: manifest cause_value (supplier_id/supplier_lot); when given,
    the recall.rule's component_lots must contain a matching supplier_lot entry.
    W8 fix: compare component_lots content against cause_value, not just presence.

    Returns the list of found findings for further checks.
    """
    findings = _query_findings_by_campaign(ddb, stage, campaign_id)
    vins_with_finding = {f.get("scope", {}).get("vin") for f in findings
                         if isinstance(f.get("scope"), dict)}
    expected_set = set(expected_vins)

    # C6 fix: FAIL on empty expected set (pre-expected-vins must have run).
    if not expected_set:
        check(
            res, "after-run-findings-count", False,
            on_fail="expected_vins is empty; pre-expected-vins check must have failed "
                    "or --part pre was not run — cannot verify Findings",
        )
    else:
        # C6 fix: set equality — missing AND extras both fail.
        missing = sorted(expected_set - vins_with_finding)
        extras = sorted(vins_with_finding - expected_set)
        ok = not missing and not extras
        check(
            res, "after-run-findings-count",
            ok,
            f"{len(findings)} recall Findings for campaign {campaign_id!r}; "
            f"{len(expected_set)} expected VINs, exact match",
            on_fail=(
                (f"missing Findings for {len(missing)} VINs: {missing[:5]!r}"
                 f"{'...' if len(missing) > 5 else ''}; " if missing else "")
                + (f"unexpected Findings for {len(extras)} extra VINs: {extras[:5]!r}"
                   f"{'...' if len(extras) > 5 else ''}" if extras else "")
            ).rstrip("; "),
        )

    # C5 fix: read item type from evidence list via recall.item.value.item.
    # Source: recall.py:178-209 _to_schema_evidence; evidence.py:184-196.
    bad_types: list[str] = []
    for f in findings:
        item_ev = _ev_val_by_kind(f.get("evidence"), "recall.item")
        itype = item_ev.get("item") if isinstance(item_ev, dict) else ""
        if itype and itype not in ("parts_expected",):
            bad_types.append(f"{f.get('finding_id')}:{itype}")
        elif not itype:
            # No recall.item evidence or no item key — flag for visibility
            bad_types.append(f"{f.get('finding_id')}:missing-recall.item")
    check(
        res, "after-run-findings-item-types",
        not bad_types,
        "all recall Findings have item_type=parts_expected (remedy_available_from > today)",
        on_fail=f"unexpected item types: {bad_types[:3]!r}",
    )

    # C5 fix: read recall.rule from evidence list; check component_lots + null years.
    # Source: evidence.py:148-165 — supplier_lot cause gives component_lots;
    #   ADP sentinel 0/9999 → None (null years).
    ev_fails: list[str] = []
    for f in findings:
        rule = _ev_val_by_kind(f.get("evidence"), "recall.rule")
        if not isinstance(rule, dict):
            ev_fails.append(
                f"{f.get('finding_id')!r}: recall.rule value not a dict ({rule!r})"
            )
            continue
        if "component_lots" not in rule:
            ev_fails.append(
                f"{f.get('finding_id')!r}: recall.rule missing component_lots "
                f"(supplier_lot cause should add them — evidence.py:152-153)"
            )
        elif cause_value:
            # W8 fix: compare with cause_value, split on the first "/" as
            # evidence.py's _lot does:
            #   cause_value = "SUP-1/LOT-A" → supplier_lot = "LOT-A"
            #   cause_value = "LOT-A" (no "/") → supplier_lot = "LOT-A"
            _, sep, lot_part = cause_value.partition("/")
            expected_lot = lot_part if sep else cause_value
            lots = rule.get("component_lots") or []
            if not any(
                isinstance(entry, dict) and entry.get("supplier_lot") == expected_lot
                for entry in lots
            ):
                ev_fails.append(
                    f"{f.get('finding_id')!r}: component_lots {lots!r} has no entry "
                    f"with supplier_lot={expected_lot!r} "
                    f"(from cause_value={cause_value!r}; evidence.py:_lot)"
                )
        if rule.get("model_year_min") is not None:
            ev_fails.append(
                f"{f.get('finding_id')!r}: model_year_min={rule.get('model_year_min')!r} "
                f"not None (ADP sentinel 0 → None, evidence.py:151)"
            )
        if rule.get("model_year_max") is not None:
            ev_fails.append(
                f"{f.get('finding_id')!r}: model_year_max={rule.get('model_year_max')!r} "
                f"not None (ADP sentinel 9999 → None, evidence.py:153)"
            )
    check(
        res, "after-run-findings-recall-rule",
        not ev_fails,
        "recall.rule evidence: component_lots present, model years null",
        on_fail="; ".join(ev_fails[:3]),
    )

    # C5 fix: compare disclosure_txt byte-for-byte with the received view's row.
    # build_disclosure_txt: summary + "\n\n" + consequence + "\n\n" + remedy
    # Source: writer.py:80-93; agentic-tiers.md (disclosure seam, verbatim).
    # W1 fix: never PASS without a byte comparison — fail immediately when the
    # catalog row could not be read (empty or missing).  An empty catalog_row
    # means Athena raised an error or returned no rows; either way we have no
    # expected text to compare against, so the check must FAIL, not skip silently.
    disc_fails: list[str] = []
    if not catalog_row:
        disc_fails.append(
            "catalog_row not read; byte comparison required "
            "(agentic-tiers.md: disclosure seam, verbatim)"
        )
    else:
        expected_disc = (
            str(catalog_row.get("summary", ""))
            + "\n\n"
            + str(catalog_row.get("consequence", ""))
            + "\n\n"
            + str(catalog_row.get("remedy", ""))
        )
        for f in findings:
            disc = f.get("disclosure_txt")
            if disc is None:
                disc_fails.append(f"{f.get('finding_id')!r}: missing disclosure_txt")
            elif disc is not None and disc != expected_disc:
                disc_fails.append(
                    f"{f.get('finding_id')!r}: disclosure_txt mismatch "
                    f"(expected={expected_disc!r}, got={disc!r})"
                )
    check(
        res, "after-run-findings-disclosure-txt",
        not disc_fails,
        "all recall Findings have disclosure_txt matching received view (byte-exact)",
        on_fail="; ".join(disc_fails[:2]),
    )

    return findings


# ── CMS recall cache helpers (K10) ────────────────────────────────────────────

def _read_cms_recall_manifest(s3: Any, bucket: str) -> dict[str, Any] | None:
    """Read the CMS recall cache manifest from S3; return None on any error.

    Source: CMS main 81a90d385 services/fleet_intelligence/lifecycle_cache.py:278-280
    (manifest key = {prefix}manifest.json).
    After-run is read-only; uses s3.GetObject (in _READ_OPS as of K10 fix).
    """
    import json as _json  # noqa: PLC0415
    key = f"{_CMS_RECALL_CACHE_PREFIX}manifest.json"
    try:
        resp = s3.get_object(Bucket=bucket, Key=key)
        return _json.loads(resp["Body"].read().decode("utf-8"))
    except Exception:  # noqa: BLE001
        return None


def _read_cms_recall_fleet_slice(
    s3: Any, bucket: str, generation: str, fleet_id: str,
) -> dict[str, Any] | None:
    """Read and decompress a fleet recall slice from the CMS S3 cache.

    Source: CMS main 81a90d385 services/fleet_intelligence/lifecycle_cache.py:265-278
    (scope_name = fleet-{sha256(fleet_id)}; view key = {prefix}{gen}/recall/{name}.json.gz).
    """
    import gzip as _gzip  # noqa: PLC0415
    import hashlib as _hashlib  # noqa: PLC0415
    import json as _json  # noqa: PLC0415

    scope_hash = _hashlib.sha256(fleet_id.encode("utf-8")).hexdigest()
    scope_name = f"fleet-{scope_hash}"
    key = f"{_CMS_RECALL_CACHE_PREFIX}{generation}/recall/{scope_name}.json.gz"
    try:
        resp = s3.get_object(Bucket=bucket, Key=key)
        raw = resp["Body"].read()
        return _json.loads(_gzip.decompress(raw).decode("utf-8"))
    except Exception:  # noqa: BLE001
        return None


def _check_cms_recall_cache(
    res: Results,
    s3: Any,
    stage: str,
    account: str,
    fleet_ids: list[str],
    campaign_id: str,
    expected_vins: list[str],
) -> None:
    """Check CMS's recall list and vehicle drill-down via CMS's S3 cache.

    Brief §K10: the "after-run" part is read-only (no Cognito user creation),
    so we read CMS's cache object for the id instead of calling the live route.

    Source: CMS main 81a90d385 services/fleet_intelligence/lifecycle_cache.py:203-215
    (recall_views_root → s3://cms-{stage}-athena-results-{account}-us-east-1/
    fleet-intelligence/_cache/fi-recall-views-v1/).
    Source: CMS main 81a90d385 services/fleet_intelligence/recall_source.py:126-188
    (get_fleet_summary reads campaigns list; get_vehicle_recalls checks vehicle_status).
    """
    if not account:
        check(res, "after-run-cms-recall-list", False,
              on_fail="no account in manifest; cannot build CMS recall cache bucket name")
        check(res, "after-run-cms-vehicle-drill-down", False,
              on_fail="no account in manifest; cannot build CMS recall cache bucket name")
        return

    bucket = _CMS_RECALL_BUCKET.format(stage=stage, account=account)

    try:
        manifest = _read_cms_recall_manifest(s3, bucket)
        if manifest is None:
            check(res, "after-run-cms-recall-list", False,
                  on_fail=f"CMS recall cache manifest not readable "
                          f"(bucket={bucket!r}, key={_CMS_RECALL_CACHE_PREFIX}manifest.json)")
            check(res, "after-run-cms-vehicle-drill-down", False,
                  on_fail="CMS recall cache manifest missing; cannot check vehicle drill-down")
            return

        generation = manifest.get("generation", "")
        if not generation:
            check(res, "after-run-cms-recall-list", False,
                  on_fail=f"CMS recall manifest has no generation: {manifest!r}")
            check(res, "after-run-cms-vehicle-drill-down", False,
                  on_fail="CMS recall manifest missing generation")
            return

        # Read fleet slices for each fleet_id found in the recall Findings.
        campaign_found = False
        vin_found = False
        for fid in fleet_ids[:3]:  # check up to 3 fleets
            slice_obj = _read_cms_recall_fleet_slice(s3, bucket, generation, fid)
            if slice_obj is None:
                continue
            # W10 fix: check computed_at of the slice.
            # CMS refuses slices older than RECALL_MAX_AGE_SECONDS (26 h).
            # Source: CMS main 81a90d385 recall_source.py:28,47-69 (load_recall_cache).
            # A stale slice still listing the id would give a false PASS; CMS would
            # show "unavailable" for that fleet.
            import datetime as _datetime_w10  # noqa: PLC0415
            raw_ts = slice_obj.get("computed_at")
            if not raw_ts:
                check(res, "after-run-cms-recall-list", False,
                      on_fail=f"CMS recall slice for fleet {fid!r} has no computed_at; "
                              "CMS would refuse it (W10 fix)")
                check(res, "after-run-cms-vehicle-drill-down", False,
                      on_fail="CMS recall slice has no computed_at; drill-down check skipped")
                return
            try:
                computed_at = _datetime_w10.datetime.fromisoformat(str(raw_ts))
                if computed_at.tzinfo is None:
                    computed_at = computed_at.replace(tzinfo=_datetime_w10.timezone.utc)
                age_s = (_utc_now() - computed_at).total_seconds()
            except Exception as _exc_ts:  # noqa: BLE001
                check(res, "after-run-cms-recall-list", False,
                      on_fail=f"CMS recall slice computed_at {raw_ts!r} not parseable: "
                              f"{_exc_ts}; CMS would refuse it (W10 fix)")
                check(res, "after-run-cms-vehicle-drill-down", False,
                      on_fail="CMS recall slice computed_at unparseable; drill-down skipped")
                return
            if age_s > _CMS_RECALL_MAX_AGE_SECONDS:
                age_h = age_s / 3600
                check(res, "after-run-cms-recall-list", False,
                      on_fail=f"CMS recall slice for fleet {fid!r} is {age_h:.1f}h old "
                              f"(max {_CMS_RECALL_MAX_AGE_SECONDS // 3600}h); "
                              "CMS would show unavailable (W10 fix)")
                check(res, "after-run-cms-vehicle-drill-down", False,
                      on_fail=f"CMS recall slice stale ({age_h:.1f}h); drill-down skipped")
                return

            # W5 fix: CMS's recall list is built from fleet_summary rows, not campaigns.
            # Source: CMS 81a90d385 services/fleet_intelligence/recall_source.py:126-186
            # get_fleet_summary reads the slice's fleet_summary list; a campaign in the
            # catalog (campaigns) with no fleet_summary row shows nothing in CMS.
            fleet_summary = slice_obj.get("fleet_summary") or []
            if any(r.get("campaign_id") == campaign_id for r in fleet_summary):
                campaign_found = True
            # VIN in vehicle_status with campaign_id → CMS vehicle drill-down shows it.
            # W5 fix: also check cohort membership (VIN in cohort list).
            # Source: recall_source.py:194-202 — CMS returns not_in_cohort for VINs
            # not in the slice's cohort; only cohort members show in drill-down.
            cohort_vins = {e.get("vin") for e in (slice_obj.get("cohort") or [])}
            vs = slice_obj.get("vehicle_status") or []
            for row in vs:
                if (row.get("campaign_id") == campaign_id
                        and row.get("vin") in expected_vins
                        and row.get("vin") in cohort_vins):
                    vin_found = True
                    break

        check(
            res, "after-run-cms-recall-list",
            campaign_found,
            f"CMS recall list (S3 cache, generation={generation!r}): "
            f"{campaign_id!r} present in campaigns list",
            on_fail=f"CMS recall list not showing {campaign_id!r}; "
                    f"bucket={bucket!r} generation={generation!r} "
                    f"fleet_ids_checked={fleet_ids[:3]!r}",
        )
        check(
            res, "after-run-cms-vehicle-drill-down",
            vin_found,
            f"CMS vehicle drill-down (S3 cache): VIN from expected_vins "
            f"has {campaign_id!r} in vehicle_status",
            on_fail=f"no expected VIN has {campaign_id!r} in CMS S3 vehicle_status; "
                    f"expected_vins={expected_vins[:3]!r}",
        )
    except Exception as exc:  # noqa: BLE001
        check(res, "after-run-cms-recall-list", False,
              on_fail=f"CMS recall cache S3 read raised: {type(exc).__name__}: {exc}")
        check(res, "after-run-cms-vehicle-drill-down", False,
              on_fail=f"CMS vehicle drill-down S3 read raised: {type(exc).__name__}: {exc}")


def _check_fleet_queues(
    res: Results, ddb: Any, stage: str, fleet_id: str, campaign_id: str,
) -> None:
    """Check CMS fleet queue (DDB by_fleet GSI) for recall items.

    Brief §after-run: "fleet queues via the source the facts name".
    g93lv-facts.md §Fleet queues: CMS reads from DDB by_fleet GSI (NOT REST API).
    Checks findings table by_fleet GSI for the fleet.
    Source: cms_ui fi_action_queue.py:65-66 (facts).
    """
    try:
        table = _FINDINGS_TABLE.format(stage=stage)
        rows: list[dict] = []
        kwargs: dict[str, Any] = {
            "TableName": table,
            "IndexName": "by_fleet",
            "KeyConditionExpression": "fleet_id = :f",
            "ExpressionAttributeValues": {":f": {"S": fleet_id}},
        }
        while True:
            page = ddb.query(**kwargs)
            for item in page.get("Items", []):
                rows.append(_unwrap_ddb_item(item))
            if not page.get("LastEvaluatedKey"):
                break
            kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]
        recall_items = [r for r in rows
                        if r.get("finding_kind") == _RECALL_FINDING_KIND
                        and (r.get("subject_key") or "").startswith(f"{campaign_id}/")]
        check(
            res, "after-run-fleet-queue",
            bool(recall_items),
            f"fleet queue (by_fleet GSI, fleet={fleet_id!r}): "
            f"{len(recall_items)} recall items for {campaign_id!r}",
            on_fail=f"no recall items for campaign {campaign_id!r} in fleet queue "
                    f"({len(rows)} total items in fleet)",
        )
    except Exception as exc:  # noqa: BLE001
        check(
            res, "after-run-fleet-queue", False,
            on_fail=f"fleet queue query raised: {type(exc).__name__}: {exc}",
        )



# ── after-run part ────────────────────────────────────────────────────────────

def _part_after_run(res: Results, *, stage: str, run_id: str, **_: Any) -> None:
    """Part after-run: verify the recall run effects after the next scheduled recall run.

    Read-only (no writes).  Reads manifest for ids; checks:
      1. Run record in vsa-{stage}-avx-agent-runs (pk=agent-fi-recall-warranty#recall)
         with started_at after the issue's part_issue_completed_at.
      2. recall_vehicle_status for the campaign_id = expected VINs (Athena).
      3. AVX recall Findings per VIN (item types, recall.rule evidence, disclosure_txt).
      4. Fleet queues (DDB by_fleet GSI on findings table).
      5. CMS recall list + vehicle drill-down.
    """
    _init_session("after-run")
    print(f"\n── after-run: stage={stage!r} run_id={run_id!r}")

    # Read manifest
    manifest_path = _manifest_path(run_id)
    try:
        manifest = _read_manifest(manifest_path)
    except FileNotFoundError:
        check(res, "after-run-manifest-read", False,
              on_fail=f"manifest not found at {manifest_path!r}; run --part issue first")
        return
    except Exception as exc:  # noqa: BLE001
        check(res, "after-run-manifest-read", False,
              on_fail=f"manifest read failed: {type(exc).__name__}: {exc}")
        return

    if manifest.get("stage") != stage:
        check(res, "after-run-manifest-stage", False,
              on_fail=f"manifest stage={manifest.get('stage')!r} != {stage!r}")
        return
    check(res, "after-run-manifest-read", True, "manifest read OK")

    records = manifest.get("records", {})
    campaign_id: str = records.get("issued_campaign_id") or records.get("campaign_id") or ""
    expected_vins: list[str] = manifest.get("expected_vins") or []
    issue_completed_at: str = records.get("part_issue_completed_at") or ""
    account: str = manifest.get("account") or ""

    if not campaign_id or not _CAMPAIGN_ID_RE.fullmatch(campaign_id):
        check(res, "after-run-manifest-campaign-id", False,
              on_fail=f"no valid campaign_id in manifest (got {campaign_id!r}); "
                      f"run --part issue first")
        return
    check(res, "after-run-manifest-campaign-id", True,
          f"campaign_id={campaign_id!r}")

    # W7 fix: gate on part_issue_completed_at being non-empty.
    # Without it, "" < started_at is True for any run → run-record check passes
    # vacuously even when issue never ran.  Require it before reading the run table.
    if not issue_completed_at:
        check(res, "after-run-issue-completed", False,
              on_fail="no part_issue_completed_at in manifest; "
                      "run --part issue first and wait for the scheduled recall run "
                      "at 05:00Z before running --part after-run")
        return
    check(res, "after-run-issue-completed", True,
          f"part_issue_completed_at={issue_completed_at!r}")

    # ── 1. Run record (DDB agent runs table) ──────────────────────────────────
    try:
        ddb_avx = _client("dynamodb", _AVX_REGION)
        run_table = _AGENT_RUNS_TABLE.format(stage=stage)
        # Query by pk=agent_component (brief §after-run "run record").
        # Source: run_records.py:191 pk = f"{agent_id}#{component}" = "agent-fi-recall-warranty#recall"
        runs: list[dict] = []
        kwargs: dict[str, Any] = {
            "TableName": run_table,
            "KeyConditionExpression": "agent_component = :pk",
            "ExpressionAttributeValues": {":pk": {"S": _RECALL_AGENT_COMPONENT}},
        }
        while True:
            page = ddb_avx.query(**kwargs)
            for item in page.get("Items", []):
                runs.append(_unwrap_ddb_item(item))
            if not page.get("LastEvaluatedKey"):
                break
            kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]

        # Find a run with started_at after the issue's completion (issue must have
        # finished before the scheduled recall run at 05:00Z)
        matching = [
            r for r in runs
            if r.get("started_at", "") > issue_completed_at
        ]
        if not matching:
            check(
                res, "after-run-run-record",
                False,
                on_fail=f"no recall run record after issue completion "
                        f"({issue_completed_at!r}); "
                        f"all runs: {[r.get('started_at') for r in runs]!r}",
            )
        else:
            # W7 rest fix: require status == "completed" and clean counters.
            # Source: run_records.py:194 (status field), :73-74 (rows_refused,
            # grounding_read_errors written when non-None).
            latest = max(matching, key=lambda r: r.get("started_at", ""))
            run_status = latest.get("status", "")
            rows_refused = int(latest.get("rows_refused") or 0)
            grounding_read_errors = int(latest.get("grounding_read_errors") or 0)
            ok = (run_status == "completed"
                  and rows_refused == 0
                  and grounding_read_errors == 0)
            check(
                res, "after-run-run-record",
                ok,
                f"run record: status={run_status!r} rows_refused={rows_refused} "
                f"grounding_read_errors={grounding_read_errors}; "
                f"latest started_at={latest.get('started_at')!r}",
                on_fail=(
                    (f"status={run_status!r} (expected completed); " if run_status != "completed" else "")
                    + (f"rows_refused={rows_refused} (expected 0); " if rows_refused else "")
                    + (f"grounding_read_errors={grounding_read_errors} (expected 0)" if grounding_read_errors else "")
                ).rstrip("; "),
            )
    except Exception as exc:  # noqa: BLE001
        check(res, "after-run-run-record", False,
              on_fail=f"agent runs query raised: {type(exc).__name__}: {exc}")

    # ── 2. recall_vehicle_status + catalog row via Athena ────────────────────
    rvs_vins: list[str] = []
    catalog_row: dict = {}
    try:
        athena = _client("athena", _ADP_REGION)
        if not _CAMPAIGN_ID_RE.fullmatch(campaign_id):
            check(res, "after-run-recall-vehicle-status", False,
                  on_fail=f"campaign_id {campaign_id!r} fails shape check")
        else:
            sql = _RECALL_STATUS_SQL.format(stage=stage, campaign_id=campaign_id)
            rvs_rows = _athena_query_paged(athena, stage, sql, "recall_vehicle_status")
            rvs_vins = [r.get("vin", "") for r in rvs_rows if _VIN_RE.fullmatch(r.get("vin", ""))]
            # C6 fix: set equality for recall_vehicle_status.
            expected_set = set(expected_vins)
            if not expected_set:
                check(res, "after-run-recall-vehicle-status", False,
                      on_fail="expected_vins empty; cannot check recall_vehicle_status")
            else:
                rvs_set = set(rvs_vins)
                missing_vins = sorted(expected_set - rvs_set)
                extra_vins = sorted(rvs_set - expected_set)
                rvs_ok = not missing_vins and not extra_vins
                check(
                    res, "after-run-recall-vehicle-status",
                    rvs_ok,
                    f"recall_vehicle_status: {len(rvs_vins)} VINs for {campaign_id!r} "
                    f"(expected {len(expected_set)}, exact match)",
                    on_fail=(
                        (f"missing {len(missing_vins)} VINs: {missing_vins[:5]!r}; "
                         if missing_vins else "")
                        + (f"extra {len(extra_vins)} VINs: {extra_vins[:5]!r}"
                           if extra_vins else "")
                    ).rstrip("; "),
                )

            # C5 fix: query catalog row for disclosure_txt byte-equality check.
            # Source: recall_campaigns_received view (ADP 9bb82e96).
            try:
                cat_sql = _RECALL_CATALOG_SQL.format(
                    stage=stage, campaign_id=campaign_id
                )
                cat_rows = _athena_query_paged(athena, stage, cat_sql,
                                               "recall_catalog_row")
                catalog_row = cat_rows[0] if cat_rows else {}
            except Exception:  # noqa: BLE001
                catalog_row = {}

    except Exception as exc:  # noqa: BLE001
        check(res, "after-run-recall-vehicle-status", False,
              on_fail=f"recall_vehicle_status query raised: {type(exc).__name__}: {exc}")

    # ── 3. AVX recall Findings per VIN ───────────────────────────────────────
    findings: list[dict] = []
    try:
        ddb_findings = _client("dynamodb", _AVX_REGION)
        findings = _check_recall_findings(
            res, ddb_findings, stage, campaign_id, expected_vins,
            catalog_row=catalog_row,
            cause_value=manifest.get("cause_value") or "",
        )
    except Exception as exc:  # noqa: BLE001
        check(res, "after-run-findings", False,
              on_fail=f"findings check raised: {type(exc).__name__}: {exc}")

    # ── 3c. S9 fix: recall Findings must have no booking Actions ─────────────
    # A booking (book_fleet_service) Action on a recall Finding would mean the
    # booking flow was incorrectly routed to a recall Finding; booking belongs
    # on fleet.health.service Findings only.
    # Source: actions table "by_finding" GSI (IndexName="by_finding"); DDB key
    # is finding_id.
    try:
        ddb_act = _client("dynamodb", _AVX_REGION)
        actions_table = _ACTIONS_TABLE.format(stage=stage)
        book_on_recall: list[str] = []
        for f in findings:
            fid = f.get("finding_id") or ""
            if not fid:
                continue
            resp_act = ddb_act.query(
                TableName=actions_table,
                IndexName="by_finding",
                KeyConditionExpression="finding_id = :fid",
                ExpressionAttributeValues={":fid": {"S": fid}},
            )
            for act_item in resp_act.get("Items", []):
                act = _unwrap_ddb_item(act_item)
                if act.get("action_kind") == _BOOK_FLEET_SERVICE_KIND:
                    book_on_recall.append(fid)
                    break
        check(
            res, "after-run-recall-no-booking-actions",
            not book_on_recall,
            "recall Findings carry no booking Actions",
            on_fail=f"recall Findings have booking Actions: {book_on_recall[:3]!r}; "
                    "book_fleet_service belongs on fleet.health.service Findings only "
                    "(S9 fix: guard against misrouted bookings)",
        )
    except Exception as exc:  # noqa: BLE001
        check(res, "after-run-recall-no-booking-actions", False,
              on_fail=f"recall booking-Actions check raised: {type(exc).__name__}: {exc}")

    # ── 4. Fleet queues (DDB by_fleet GSI) ────────────────────────────────────
    try:
        ddb_q = _client("dynamodb", _AVX_REGION)
        # Use the first fleet_id from the recall_vehicle_status rows (or from manifest)
        fleet_ids: set[str] = set()
        for row in findings:
            fid = row.get("fleet_id")
            if fid and isinstance(fid, str):
                fleet_ids.add(fid)
        if not fleet_ids and rvs_vins:
            # Try to get fleet_id from rvs_rows
            pass  # fleet_ids already empty; check will FAIL with explanation
        for fid in list(fleet_ids)[:3]:  # check up to 3 fleets
            _check_fleet_queues(res, ddb_q, stage, fid, campaign_id)
        if not fleet_ids:
            check(res, "after-run-fleet-queue", False,
                  on_fail="no fleet_id found in recall Findings; cannot check fleet queue")
    except Exception as exc:  # noqa: BLE001
        check(res, "after-run-fleet-queue", False,
              on_fail=f"fleet queue check raised: {type(exc).__name__}: {exc}")

    # ── 5. CMS recall list + vehicle drill-down (CMS S3 cache) ──────────────
    # Brief §after-run: "CMS recall list + drill-down show the id".
    # K10: the check must read from CMS's own recall cache (S3), NOT from AVX DDB.
    # CMS's recall-list route reads the S3-backed lifecycle_cache slice, refreshed
    # hourly from ADP's views; AVX DDB findings prove nothing about what CMS serves.
    # After-run is read-only (no Cognito users), so we read the S3 cache object.
    # Source: CMS main 81a90d385 services/fleet_intelligence/recall_source.py:126-188.
    # fleet_ids from step 3's findings tell us which fleet slices to read.
    # S5 fix: wrap client creation and fleet_ids derivation in their own try/except
    # (brief: each step in its own try/except).
    try:
        fleet_ids_list = sorted({f.get("fleet_id") for f in findings
                                  if isinstance(f.get("fleet_id"), str)})
        s3_cms = _client("s3", _CMS_RECALL_S3_REGION)
        _check_cms_recall_cache(
            res, s3_cms, stage, account, fleet_ids_list, campaign_id, expected_vins,
        )
    except Exception as exc:  # noqa: BLE001
        check(res, "after-run-cms-recall-list", False,
              on_fail=f"CMS recall cache check raised: {type(exc).__name__}: {exc}")

    # ── 6. Thread-lake objects: each recorded version has its flat-key object ─
    # F2g: after the issue step the writer puts a lake object per version at
    # curated/engineering/digital_thread/<thread_id>.v<n>.json.  Verify each.
    thread_id_ar: str = records.get("thread_id") or ""
    thread_versions_ar: list[int] = [
        int(v) for v in (records.get("thread_versions") or [])
        if str(v).isdigit()
    ]
    if not thread_id_ar or not _THREAD_ID_RE.fullmatch(thread_id_ar):
        # S5 F3b fix: missing or malformed thread_id must FAIL, not silently skip.
        check(res, "after-run-thread-lake-objects", False,
              on_fail=f"thread_id missing or malformed in manifest: {thread_id_ar!r}; "
                      f"run --part issue first")
    elif not thread_versions_ar:
        check(res, "after-run-thread-lake-objects", False,
              on_fail=f"thread_id {thread_id_ar!r} in manifest but no "
                      f"thread_versions recorded; run --part issue first")
    else:
        try:
            bucket_ar = _ADP_LAKE_BUCKET.format(stage=stage, account=account)
            s3_ar = _client("s3", _ADP_REGION)
            missing_keys: list[str] = []
            for _v in thread_versions_ar:
                _key = (
                    f"curated/engineering/digital_thread/"
                    f"{thread_id_ar}.v{_v}.json"
                )
                # Use HeadObject; 404 → missing.
                try:
                    s3_ar.head_object(Bucket=bucket_ar, Key=_key)
                except Exception:  # noqa: BLE001
                    missing_keys.append(_key)
            if not missing_keys:
                check(res, "after-run-thread-lake-objects", True,
                      f"thread {thread_id_ar!r}: all {len(thread_versions_ar)} "
                      f"flat-key lake object(s) present")
            else:
                check(res, "after-run-thread-lake-objects", False,
                      on_fail=f"thread {thread_id_ar!r}: lake objects missing: "
                              f"{missing_keys!r}")
        except Exception as exc:  # noqa: BLE001
            check(res, "after-run-thread-lake-objects", False,
                  on_fail=f"thread-lake object check raised: "
                          f"{type(exc).__name__}: {exc}")

    # ── 7. reconcile_thread_lake.py: no missing/differs/unprojectable for thread ─
    # F2g: dry run (no --apply); other threads' entries are ignored.
    # Exit 2 or unparsable output → FAIL.
    # S5 F3b fix: missing or malformed thread_id FAILs the reconcile check too.
    if not thread_id_ar or not _THREAD_ID_RE.fullmatch(thread_id_ar):
        check(res, "after-run-reconcile-ok", False,
              on_fail=f"thread_id missing or malformed in manifest: {thread_id_ar!r}; "
                      f"cannot run reconcile check")
    else:
        _run_reconcile_check(
            res, stage, thread_id_ar,
            label_prefix="after-run-reconcile",
            after_cleanup=False,
        )


# ── ground-leg helpers (leg G) ────────────────────────────────────────────────


def _ground_pick_recall_check(vin: str, cache: dict | None) -> list[str] | None:
    """Return blocking campaign_ids for vin, or None if cache unavailable/vin not in cohort.

    W1 fix: before grounding, the pick uses CMS's recall slice to skip VINs that are
    outside the cohort, have a blocking recall, or whose cache is stale.

    Source: CMS 81a90d385 services/fleet_intelligence/recall_source.py
      load_recall_cache (staleness + cohort validation, G6-1/G6-3/G6-4)
      blocking_recalls (G6-9: blocks on do_not_drive OR safety-class CONFIRMED)

    Returns:
      None   — cache absent/stale, or VIN not in cohort (fail-closed; skip the pick).
      []     — VIN is in cohort and has no blocking recalls (eligible).
      [cid]  — VIN has blocking recall(s); skip the pick.
    """
    import datetime as _dt_rpc  # noqa: PLC0415
    if cache is None:
        return None
    raw_ts = cache.get("computed_at")
    if not raw_ts:
        return None
    try:
        computed_at = _dt_rpc.datetime.fromisoformat(str(raw_ts))
        if computed_at.tzinfo is None:
            computed_at = computed_at.replace(tzinfo=_dt_rpc.timezone.utc)
    except (ValueError, TypeError):
        return None
    now = _utc_now()
    # Reject slices more than 5 min in the future (clock skew / wrong tz).
    if (computed_at - now).total_seconds() > 300:
        return None
    # Reject stale slices (>26 h old). Source: recall_source.RECALL_MAX_AGE_SECONDS.
    if (now - computed_at).total_seconds() > 26 * 3600:
        return None
    cohort = cache.get("cohort")
    if not isinstance(cohort, list):
        return None
    cohort_vins = {r["vin"] for r in cohort if isinstance(r, dict)}
    if vin not in cohort_vins:
        return None
    campaigns_by_id: dict[str, Any] = {
        c["campaign_id"]: c
        for c in (cache.get("campaigns") or [])
        if isinstance(c, dict) and c.get("campaign_id")
    }
    blocking: list[str] = []
    for row in (cache.get("vehicle_status") or []):
        if not isinstance(row, dict) or row.get("vin") != vin:
            continue
        if row.get("remedied") is True:
            continue
        cid = row.get("campaign_id", "")
        if cid not in campaigns_by_id:
            # Fail-closed: campaign absent from catalog blocks. Source: recall_source.py.
            blocking.append(cid)
            continue
        campaign = campaigns_by_id[cid]
        if campaign.get("do_not_drive"):
            blocking.append(cid)
        elif (
            campaign.get("recall_class") == "safety"
            and row.get("match_basis") == "CONFIRMED"
        ):
            blocking.append(cid)
    return blocking


def _find_p0_health_finding(
    ddb: Any, stage: str,
) -> dict[str, Any] | None:
    """Return the first p0 fleet.health.service Finding with a ground_vehicle action.

    Scans the findings table for fleet-health findings with:
    - severity == "p0"
    - recommended_action.action_kind == "ground_vehicle"
    No open-booking check here; caller checks separately.

    Retained for compatibility; new callers should prefer ``_iter_p0_health_findings``.
    Source: agents/fi_fleet_health/write.py:35 (FINDING_KIND = "fleet.health.service"),
            lambdas/api-avx-findings/handler.py:337 (ground_vehicle in KINDS_TABLE).
    Returns None if no matching Finding exists.
    """
    return next(_iter_p0_health_findings(ddb, stage), None)


def _iter_p0_health_findings(
    ddb: Any, stage: str,
) -> "Iterator[dict[str, Any]]":
    """Yield every p0 fleet.health.service Finding with a ground_vehicle action.

    Performs a full table scan and yields each matching row.  Callers apply their
    own eligibility filters (expiry, managed, grounded, open-booking) and take the
    first candidate that passes all checks.

    Source: agents/fi_fleet_health/write.py:35 (FINDING_KIND), write.py:104 (expires_at),
            lambdas/api-avx-findings/handler.py:337 (ground_vehicle in KINDS_TABLE).
    """
    findings_table = _FINDINGS_TABLE.format(stage=stage)
    kwargs: dict[str, Any] = {
        "TableName": findings_table,
        "FilterExpression": (
            "finding_kind = :kind AND severity = :sev"
        ),
        "ExpressionAttributeValues": {
            ":kind": {"S": _FLEET_HEALTH_FINDING_KIND},
            ":sev": {"S": "p0"},
        },
    }
    while True:
        page = ddb.scan(**kwargs)
        for item in page.get("Items", []):
            row = _unwrap_ddb_item(item)
            rec = row.get("recommended_action") or {}
            if rec.get("action_kind") == _GROUND_VEHICLE_KIND:
                yield row
        lk = page.get("LastEvaluatedKey")
        if not lk:
            break
        kwargs["ExclusiveStartKey"] = lk


def _has_open_grounding(ddb: Any, stage: str, vehicle_id: str) -> bool:
    """Return True if *vehicle_id* has an open grounding in the CMS groundings table.

    An open grounding is a row without ``returnedAt`` (it is removed on RTS).
    Uses a DDB Query on the table's base key (vehicleId PK, actionId SK).

    Returns False if the groundings table does not yet exist (the table is created
    by FleetHealthStack; if it hasn't been deployed, no groundings can exist).

    Source: CMS main 81a90d385 services/fleet_intelligence/groundings.py:245-265
            (open_groundings_for_vehicle: query by vehicleId, filter no returnedAt).
    W8 fix: the pick skips already-grounded vehicles and tries the next candidate.
    """
    from botocore.exceptions import ClientError  # noqa: PLC0415
    table = _CMS_GROUNDINGS_TABLE.format(stage=stage)
    kwargs: dict[str, Any] = {
        "TableName": table,
        "KeyConditionExpression": "vehicleId = :vid",
        "ExpressionAttributeValues": {":vid": {"S": vehicle_id}},
    }
    try:
        while True:
            page = ddb.query(**kwargs)
            for item in page.get("Items", []):
                row = _unwrap_ddb_item(item)
                if not row.get("returnedAt"):
                    return True  # at least one open grounding
            lk = page.get("LastEvaluatedKey")
            if not lk:
                break
            kwargs["ExclusiveStartKey"] = lk
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "ResourceNotFoundException":
            # Carried fix: fail closed — if the groundings table doesn't exist the
            # FleetHealthStack hasn't been deployed and leg G can't run safely.
            # Re-raise so the caller's try/except propagates a FAIL, not a False pass.
            raise
        raise
    return False


def _find_booking_finding_for_vin(
    ddb: Any, stage: str, vin: str
) -> "dict[str, Any] | None":
    """Return the first p0 fleet.health.service Finding with a book_fleet_service action for vin.

    In the live system the Tier 2 run writes a booking Finding (subject_key service/N,
    recommended_action.action_kind book_fleet_service) alongside the grounding Finding
    (ground/N) for the same VIN.  The operator approves book_fleet_service on the
    *booking* Finding, not the grounding one.

    Source: agents/fi_fleet_health/writer.py:264 (book_fleet_service recommended_action),
            agents/fi_fleet_health/loop.py:1474 (subject_key "service/{attempt}").
    """
    findings_table = _FINDINGS_TABLE.format(stage=stage)
    kwargs: dict[str, Any] = {
        "TableName": findings_table,
        "FilterExpression": (
            "finding_kind = :kind AND severity = :sev"
        ),
        "ExpressionAttributeValues": {
            ":kind": {"S": _FLEET_HEALTH_FINDING_KIND},
            ":sev": {"S": "p0"},
        },
    }
    while True:
        page = ddb.scan(**kwargs)
        for item in page.get("Items", []):
            row = _unwrap_ddb_item(item)
            scope = row.get("scope") or {}
            if scope.get("vin") != vin:
                continue
            rec = row.get("recommended_action") or {}
            if rec.get("action_kind") == _BOOK_FLEET_SERVICE_KIND:
                return row
        lk = page.get("LastEvaluatedKey")
        if not lk:
            break
        kwargs["ExclusiveStartKey"] = lk
    return None


def _resolve_registry_vehicle_id(ddb: Any, stage: str, vin: str) -> str | None:
    """Look up the CMS registry vehicleId for a VIN.

    The CMS vehicles table key is vehicleId; a reverse lookup by vin requires a
    scan with FilterExpression.  Staging has 101 managed vehicles whose vehicleId
    differs from the VIN (seed.py vehicle_id(10)='VEH-WORLD-0010'); the RTS path
    must carry the registry vehicleId, not the bare VIN.

    Source: CMS main 81a90d385 services/fleet_intelligence/groundings.py:35-64
            (registry GetItem by vehicleId; the reverse lookup uses a scan since
             vehicleId is the table key, not vin).
    W8 fix: g93lv-facts.md §CMS.
    """
    table = _CMS_VEHICLES_TABLE.format(stage=stage)
    kwargs: dict[str, Any] = {
        "TableName": table,
        "FilterExpression": "#v = :vin",
        "ExpressionAttributeNames": {"#v": "vin"},
        "ExpressionAttributeValues": {":vin": {"S": vin}},
    }
    while True:
        page = ddb.scan(**kwargs)
        for item in page.get("Items", []):
            row = _unwrap_ddb_item(item)
            vid = row.get("vehicleId")
            if vid:
                return vid
        lk = page.get("LastEvaluatedKey")
        if not lk:
            break
        kwargs["ExclusiveStartKey"] = lk
    return None


# ── DMS is_fi_booking predicate (ported verbatim from DMS de6c181) ─────────────
# Source: DMS de6c181 source/simulation/fi_stand_ins.py:67-127.
# Ported here so this script and the stand-in always use the same definition.

#: Recall campaign ids created by this script's recall flow (MRD-<year>-<alphanum>).
#: More permissive than CMS's three-digit rule (^MRD-\d{4}-\d{3}$) because the
#: stand-in acts on any catalog id from AVX's DMS executor.
#: Source: DMS de6c181 source/simulation/fi_stand_ins.py:71.
_FI_CAMPAIGN_ID_RE = re.compile(r"^MRD-\d{4}-[A-Z0-9]+$")


def _is_fi_avx_fleet_ro(ro: dict) -> bool:
    """An AVX booking on DMS's fleet route: ``avx_action_id`` set, fleet customer.

    Source: DMS de6c181 source/simulation/fi_stand_ins.py:75-77.
    """
    return bool(ro.get("avx_action_id")) and ro.get("customer_type") == "fleet"


def _is_fi_recall_booking(ro: dict) -> bool:
    """True only for an RO created by an FI recall booking.

    A recall booking is a fleet AVX RO with a ``pay_type == "recall"`` line
    whose ``recall_campaign_id`` matches ``_FI_CAMPAIGN_ID_RE``.
    Source: DMS de6c181 source/simulation/fi_stand_ins.py:80-91.
    """
    if not _is_fi_avx_fleet_ro(ro):
        return False
    return any(
        ln.get("pay_type") == "recall"
        and isinstance(ln.get("recall_campaign_id"), str)
        and _FI_CAMPAIGN_ID_RE.match(ln["recall_campaign_id"]) is not None
        for ln in ro.get("lines") or []
    )


def _is_fi_fleet_health_booking(ro: dict) -> bool:
    """True only for an RO created by an FI fleet-health booking (Stage D).

    A fleet-health booking is a fleet AVX RO with at least one line that has
    a non-blank ``need_id``.
    Source: DMS de6c181 source/simulation/fi_stand_ins.py:93-117.
    """
    if not _is_fi_avx_fleet_ro(ro):
        return False
    return any(
        isinstance(ln.get("need_id"), str) and ln["need_id"].strip() != ""
        for ln in ro.get("lines") or []
    )


def _is_fi_booking(ro: dict) -> bool:
    """True only for an RO an FI booking created: a recall booking or a fleet-health one.

    Advisors' ROs (no ``avx_action_id``) and retail AVX bookings (customer_type != "fleet")
    never qualify.
    Source: DMS de6c181 source/simulation/fi_stand_ins.py:119-127.
    C11 fix: this script previously used only ``_is_fi_fleet_health_booking``, missing
    recall bookings and allowing the stand-in to advance another fleet's RO.
    """
    return _is_fi_recall_booking(ro) or _is_fi_fleet_health_booking(ro)


def _has_open_booking(ddb: Any, stage: str, vin: str) -> bool:
    """Return True if *vin* has an open FI booking in the DMS RO table.

    C8 fix: checks DMS ROs directly (not Actions table for the grounding Finding).
    Booking Actions live on the *booking* Finding, not the grounding Finding.
    C11 fix: uses the full ``_is_fi_booking`` predicate (both recall and fleet-health
    bookings), not just fleet-health (``avx_action_id`` + ``need_id``).
    Also treats executed bookings as open (an executed booking = an open RO in DMS).

    Source: DMS de6c181 source/simulation/fi_stand_ins.py:119-127 (is_fi_booking),
            85-86 (status); g93lv-facts.md §DMS.
    """
    dms_table = _DMS_RO_TABLE.format(stage=stage)
    # An executed booking created an open RO in DMS; any non-final status counts.
    closed_statuses = {"Completed", "Closed", "Invoiced"}
    kwargs: dict[str, Any] = {
        "TableName": dms_table,
        # DMS fleet ROs carry the VIN as `vehicle_vin` and leave `vin` null (live RO
        # 94a428c8, 2026-10-26; the stand-in reads vehicle_vin then vin), so match either.
        "FilterExpression": "#vv = :vin OR #v = :vin",
        "ExpressionAttributeNames": {"#vv": "vehicle_vin", "#v": "vin"},
        "ExpressionAttributeValues": {":vin": {"S": vin}},
    }
    while True:
        page = ddb.scan(**kwargs)
        for item in page.get("Items", []):
            row = _unwrap_ddb_item(item)
            status = row.get("status", "")
            # C11 fix: use _is_fi_booking (both recall and fleet-health bookings).
            if status not in closed_statuses and _is_fi_booking(row):
                return True
        lk = page.get("LastEvaluatedKey")
        if not lk:
            break
        kwargs["ExclusiveStartKey"] = lk
    return False


def _count_open_fi_bookings(ddb: Any, stage: str, book_action_id: str) -> tuple[int, int]:
    """Count (total_open, ours) open FI bookings in the DMS RO table.

    "Our" booking: avx_action_id in ``_dms_action_ids(book_action_id)``. The manifest
    records the AVX id (``AVX-ACT-<uuid>``); DMS stores the bare UUID.  Used to decide if it's safe to invoke the
    stand-in, which advances ALL open FI bookings.

    C7 fix: "ours" is detected by avx_action_id == recorded book_action_id (not
    by notes containing a marker string with a double-space bug).
    C11 fix: uses the full ``_is_fi_booking`` predicate (both recall and fleet-health
    bookings) so that recall bookings are counted and the stand-in is only invoked
    when every open FI booking is this run's.

    Source: DMS de6c181 source/simulation/fi_stand_ins.py:119-127 (is_fi_booking).
    Returns (total_open, ours) where total_open = all FI bookings in active states.
    """
    dms_table = _DMS_RO_TABLE.format(stage=stage)
    active_statuses = {"Draft", "InProgress", "AwaitingParts", "Ready"}
    total_open = 0
    ours = 0
    # DMS stores the bare UUID; the recorded id is `AVX-ACT-<uuid>` (2026-10-26:
    # comparing the prefixed id made "ours" 0 and the stand-in never ran).
    our_ids = _dms_action_ids(book_action_id)
    kwargs: dict[str, Any] = {"TableName": dms_table}
    while True:
        page = ddb.scan(**kwargs)
        for item in page.get("Items", []):
            row = _unwrap_ddb_item(item)
            status = row.get("status", "")
            # C11 fix: use _is_fi_booking (both recall and fleet-health bookings).
            if status in active_statuses and _is_fi_booking(row):
                total_open += 1
                # "Ours" = the RO whose avx_action_id is the recorded Action's id as DMS
                # stores it (bare UUID; the manifest holds AVX-ACT-<uuid>).
                if row.get("avx_action_id") in our_ids:
                    ours += 1
        lk = page.get("LastEvaluatedKey")
        if not lk:
            break
        kwargs["ExclusiveStartKey"] = lk
    return total_open, ours


def _find_ro_id_for_action(ddb: Any, stage: str, action_id: str) -> str:
    """Return the ro_id of the DMS RO whose avx_action_id matches *action_id*.

    Returns an empty string if not found.  Used by the S7 early-close check.
    Source: DMS 15231e4 source/simulation/fi_stand_ins.py:280 (ro.get("ro_id")).
    """
    if not action_id:
        return ""
    dms_table = _DMS_RO_TABLE.format(stage=stage)
    our_ids = _dms_action_ids(action_id)
    if not our_ids:
        return ""
    values = {f":aid{i}": {"S": v} for i, v in enumerate(sorted(our_ids))}
    kwargs: dict[str, Any] = {
        "TableName": dms_table,
        "FilterExpression": " OR ".join(f"avx_action_id = {k}" for k in values),
        "ExpressionAttributeValues": values,
    }
    while True:
        page = ddb.scan(**kwargs)
        for item in page.get("Items", []):
            row = _unwrap_ddb_item(item)
            if row.get("avx_action_id") in our_ids:
                return str(row.get("ro_id", ""))
        lk = page.get("LastEvaluatedKey")
        if not lk:
            break
        kwargs["ExclusiveStartKey"] = lk
    return ""


def _would_early_close(ro_id: str) -> bool:
    """Return True if the DMS stand-in's ~5% early-close rule applies to *ro_id*.

    Rule: sha256(ro_id.encode()).hexdigest() % 100 < 5.
    Source: DMS 15231e4 source/simulation/fi_stand_ins.py:106-117 (_would_close_early).
    An RO that would early-close goes to 'Closed' on the first stand-in pass
    and can never become a qualifying repair (status must be Ready/Delivered/Invoiced).
    """
    import hashlib as _hashlib  # noqa: PLC0415
    digest = int(_hashlib.sha256(ro_id.encode()).hexdigest(), 16)
    return (digest % 100) < 5


def _query_fault_status(
    athena: Any, stage: str, vin: str, codes: list[str],
) -> list[dict[str, str]]:
    """Query ADP's vehicle_fault_status view for repair_ref rows.

    Source: ADP 9a08bca1 platform-foundation/source/data-products/fleet_fault_events/views/vehicle_fault_status.sql.
    Returns rows with columns: vin, code, fleet_id, repair_ref, repaired_on, status.
    Lags: DMS lake merge 30 min; no ADP daily job (vehicle_fault_status is a Glue
    VIRTUAL_VIEW over the DMS lake — g93lv-facts.md §C7); CMS health cache up to 1 h.
    S11 fix: removed incorrect "ADP daily job up to 24h" (the view has no daily job).
    W7 fix: shape-checks every fault code against _DTC_RE before SQL interpolation.
    """
    if not _VIN_RE.fullmatch(vin):
        raise ValueError(f"VIN shape check failed: {vin!r}")
    codes_clean = sorted({c for c in codes if isinstance(c, str) and c.strip()})
    if not codes_clean:
        return []
    # W7 fix: refuse codes that don't match the DTC pattern (brief §SQL).
    bad_codes = [c for c in codes_clean if not _DTC_RE.fullmatch(c)]
    if bad_codes:
        raise ValueError(
            f"fault code shape check failed: {bad_codes!r}; "
            f"codes must match {_DTC_RE.pattern!r} (brief §SQL)"
        )
    db = _FAULT_STATUS_DB.format(stage=stage)
    table = _FAULT_STATUS_TABLE
    codes_lit = ", ".join(f"'{c}'" for c in codes_clean)
    sql = (
        f"SELECT vin, code, fleet_id, repair_ref, repaired_on, status "
        f"FROM {db}.{table} "
        f"WHERE vin = '{vin}' AND code IN ({codes_lit})"
    )
    return _athena_query_paged(athena, stage, sql, "vehicle_fault_status_ground")

def _delete_ddb_item(
    ddb: Any, table: str, key: dict, label: str,
) -> bool:
    """Delete a DDB item and confirm absent by a subsequent GetItem.

    Brief §cleanup: "confirm each deletion by a read".
    Returns True if deleted (confirmed absent after delete), False on error.
    """
    try:
        ddb.delete_item(TableName=table, Key=key)
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(
            f"DeleteItem {table!r} key={key!r} failed: {type(exc).__name__}: {exc}"
        ) from exc
    # Confirm absent
    r = ddb.get_item(TableName=table, Key=key, ConsistentRead=True)
    if r.get("Item"):
        raise RuntimeError(
            f"Item still present after delete: {table!r} key={key!r}"
        )
    return True


def _delete_s3_object(
    s3: Any, bucket: str, key: str, version_id: str = "",
) -> bool:
    """Delete an S3 object (and a specific version if the bucket is versioned).

    S5 fix: match ClientError codes, not exception text.  For versioned buckets,
    delete_object on the key deletes only the current version; a specific VersionId
    is needed to delete that exact version.  The caller passes version_id from the
    put_object response when versioning may be enabled.

    Brief §cleanup: "confirm each deletion by a read; read errors fail closed".
    Returns True if deleted (or already absent).
    Raises RuntimeError on unexpected errors so the caller can emit FAIL.
    """
    from botocore.exceptions import ClientError  # noqa: PLC0415

    delete_kwargs: dict[str, Any] = {"Bucket": bucket, "Key": key}
    if version_id:
        delete_kwargs["VersionId"] = version_id
    try:
        s3.delete_object(**delete_kwargs)
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code in ("NoSuchKey", "404"):
            return True  # already gone
        raise RuntimeError(
            f"DeleteObject s3://{bucket}/{key} failed: {code}: {exc}"
        ) from exc
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(
            f"DeleteObject s3://{bucket}/{key} failed: {type(exc).__name__}: {exc}"
        ) from exc

    # Confirm absent (HeadObject should return 404)
    head_kwargs: dict[str, Any] = {"Bucket": bucket, "Key": key}
    if version_id:
        head_kwargs["VersionId"] = version_id
    try:
        s3.head_object(**head_kwargs)
        raise RuntimeError(f"S3 object still present after delete: s3://{bucket}/{key}")
    except ClientError as exc2:
        code2 = exc2.response.get("Error", {}).get("Code", "")
        if code2 in ("404", "NoSuchKey"):
            return True
        raise RuntimeError(
            f"HeadObject after delete s3://{bucket}/{key} unexpected: {code2}: {exc2}"
        ) from exc2
    except RuntimeError:
        raise
    except Exception as exc2:  # noqa: BLE001
        raise RuntimeError(
            f"HeadObject after delete s3://{bucket}/{key} unexpected: "
            f"{type(exc2).__name__}: {exc2}"
        ) from exc2


def _check_residue(
    res: Results, ddb_avx: Any, ddb_adp: Any, stage: str,
    pre_residue: dict,
) -> None:
    """Check that this run's DDB keys are absent after cleanup.

    C4 F3a fix: use ConsistentRead GetItem per recorded key — no table scan.
    Each entry in "must_be_gone" is looked up by key; presence is a drift.
    Each entry in "expected_residue" is looked up by key; absence is a drift.
    A read error is a FAIL (fail closed).

    F2g: thread rows are now deleted (not recorded); digital-thread table is
    not covered here — thread row deletion and its verification live in step 6.
    W4 F3a: Action rows (avx-actions) are also covered by the key-based check.
    """
    # ── per-table DDB clients ─────────────────────────────────────────────────
    inv_table = _INV_TABLE.format(stage=stage)
    fp_table = _FAULT_PATTERNS_TABLE.format(stage=stage)
    corpus_table = _ENGINEERING_CORPUS_TABLE.format(stage=stage)
    actions_table = _ACTIONS_TABLE.format(stage=stage)
    table_to_client: dict[str, Any] = {
        inv_table: ddb_avx,
        fp_table: ddb_adp,
        corpus_table: ddb_adp,
        actions_table: ddb_avx,
    }

    def _pk_label(key_dict: dict) -> str:
        """Return a human-readable 'attr=val' for the pk of key_dict."""
        pk_attr, pk_val_raw = next(iter(key_dict.items()), ("", ""))
        if isinstance(pk_val_raw, dict):
            pk_val = pk_val_raw.get("S") or pk_val_raw.get("N") or str(pk_val_raw)
        else:
            pk_val = str(pk_val_raw)
        return f"{pk_attr}={pk_val!r}"

    drifts: list[str] = []

    # ── check "must be gone" keys — each should be absent ────────────────────
    for entry in pre_residue.get("must_be_gone", []):
        tbl = entry.get("table", "")
        key_dict = entry.get("key", {})
        ddb = table_to_client.get(tbl)
        if not ddb or not key_dict:
            continue
        try:
            resp = ddb.get_item(TableName=tbl, Key=key_dict, ConsistentRead=True)
            if resp.get("Item"):
                drifts.append(
                    f"{tbl}: key {_pk_label(key_dict)} still present after cleanup"
                )
        except Exception as exc:  # noqa: BLE001
            check(res, "cleanup-residue-match", False,
                  on_fail=f"residue read of {tbl!r} key failed: "
                          f"{type(exc).__name__}: {exc}")
            return

    # ── check "expected residue" keys — each should be present ───────────────
    for entry in pre_residue.get("expected_residue", []):
        tbl = entry.get("table", "")
        key_dict = entry.get("key", {})
        ddb = table_to_client.get(tbl)
        if not ddb or not key_dict:
            continue
        try:
            resp = ddb.get_item(TableName=tbl, Key=key_dict, ConsistentRead=True)
            if not resp.get("Item"):
                drifts.append(
                    f"{tbl}: expected residue key {_pk_label(key_dict)} is absent"
                )
        except Exception as exc:  # noqa: BLE001
            check(res, "cleanup-residue-match", False,
                  on_fail=f"residue read of {tbl!r} expected key failed: "
                          f"{type(exc).__name__}: {exc}")
            return

    check(
        res, "cleanup-residue-match",
        not drifts,
        "residue check: all run keys absent; expected residue present",
        on_fail=f"residue drift: {drifts!r}",
    )


# ── cleanup part ─────────────────────────────────────────────────────────────

def _part_cleanup(
    res: Results, *, stage: str, run_id: str, allow_writes: bool = False, **_: Any
) -> None:
    """Part cleanup: delete everything the manifest names; confirm each deletion.

    Brief §cleanup (--allow-writes):
    - Cognito temp users: AdminDeleteUser; confirm absent via ListUsers.
    - DDB investigation row; confirm absent.
    - DDB quality.investigation finding; confirm absent.
    - DDB quality.issue finding; confirm absent.
    - DDB recall-campaigns-issued row (the draft/issued record); confirm absent.
    - DDB digital-thread rows (exact manifest-recorded versions, conditional delete); confirm absent.
    - S3 thread-lake flat-key objects (curated/engineering/digital_thread/<id>.v<n>.json); confirm absent.
    - S3 ADP VIN-list objects; confirm deleted.
    - S3 issued-recall object; confirm absent.
    - Residue equals pre snapshot by this run's keys (paged scans, read errors FAIL).
    - After-cleanup reconcile dry run: no entry of any kind for this thread.
    Cleanup is driven by the manifest; each deletion confirmed by a read.
    Read errors fail closed (brief §Manifest).
    Signals held during each delete+confirm pair.
    """
    if not allow_writes:
        res.skip("cleanup",
                 "requires --allow-writes; re-run with --allow-writes flag")
        return

    _init_session("cleanup")
    print(f"\n── cleanup: stage={stage!r} run_id={run_id!r}")

    # Read manifest
    manifest_path = _manifest_path(run_id)
    try:
        manifest = _read_manifest(manifest_path)
    except FileNotFoundError:
        check(res, "cleanup-manifest-read", False,
              on_fail=f"manifest not found at {manifest_path!r}; run --part issue first")
        return
    except Exception as exc:  # noqa: BLE001
        check(res, "cleanup-manifest-read", False,
              on_fail=f"manifest read failed: {type(exc).__name__}: {exc}")
        return

    if manifest.get("stage") != stage:
        check(res, "cleanup-manifest-stage", False,
              on_fail=f"manifest stage={manifest.get('stage')!r} != {stage!r}")
        return
    check(res, "cleanup-manifest-read", True, "manifest read OK")

    records = manifest.get("records", {})
    account: str = manifest.get("account") or ""
    # pre_residue from manifest is not used by the key-based residue check (C4 fix).
    # The cleanup builds must_be_gone / expected_residue from manifest records directly.

    # ── 1. Delete Cognito temp users ─────────────────────────────────────────
    users: list[str] = records.get("users", [])
    try:
        idp = _client("cognito-idp", _AVX_REGION)
        pool_id = _resolve_pool_id(idp)
        for username in users:
            try:
                with _defer_signals_for_restore():
                    _delete_temp_user(idp, pool_id, username)
                # Confirm absent: ListUsers filter by email (C3 fix: pool uses email aliases)
                remaining = idp.list_users(
                    UserPoolId=pool_id,
                    Filter=f'email = "{username}"',
                ).get("Users", [])
                ok = len(remaining) == 0
                check(
                    res, f"cleanup-user-{username[:16]}",
                    ok,
                    f"Cognito user {username!r} deleted and confirmed absent",
                    on_fail=f"user {username!r} still present after delete (1 user = fail)",
                )
            except Exception as exc:  # noqa: BLE001
                check(
                    res, f"cleanup-user-{username[:16]}", False,
                    on_fail=f"delete user {username!r} raised: {type(exc).__name__}: {exc}",
                )
    except Exception as exc:  # noqa: BLE001
        check(res, "cleanup-users", False,
              on_fail=f"Cognito cleanup raised: {type(exc).__name__}: {exc}")

    # ── 2. Delete DDB investigation row ─────────────────────────────────────
    investigation_id: str = records.get("investigation_id") or ""
    if investigation_id:
        try:
            ddb_inv = _client("dynamodb", _AVX_REGION)
            inv_table = _INV_TABLE.format(stage=stage)

            # S2 fix: before deleting, read the investigation row to recover
            # finding_id in case issue stopped before recording it in the manifest
            # (e.g. poll timeout; investigation may have completed later).
            _inv_finding_from_row: str = ""
            try:
                _inv_row = _read_ddb_item(
                    ddb_inv, inv_table,
                    {"investigation_id": {"S": investigation_id}},
                )
                if _inv_row:
                    _inv_finding_from_row = (
                        _inv_row.get("finding_id")
                        or _ddb_str(_inv_row, "finding_id")
                        or ""
                    )
            except Exception:  # noqa: BLE001
                pass  # best-effort; proceed with deletion regardless

            if _inv_finding_from_row and not records.get("investigation_finding_id"):
                # Record the recovered id in the manifest so the Finding
                # deletion below picks it up.
                with _defer_signals_for_restore():
                    manifest["records"]["investigation_finding_id"] = _inv_finding_from_row
                    _write_manifest(manifest_path, manifest)
                check(res, "cleanup-recover-inv-finding-id", True,
                      f"recovered investigation_finding_id={_inv_finding_from_row!r} "
                      f"from investigation row (was absent from manifest)")
                # Update the local alias so the Finding-deletion step sees it.
                records = manifest.get("records", {})

            with _defer_signals_for_restore():
                _delete_ddb_item(
                    ddb_inv, inv_table,
                    {"investigation_id": {"S": investigation_id}},
                    f"investigation {investigation_id!r}",
                )
            check(res, "cleanup-investigation-row", True,
                  f"investigation {investigation_id!r} deleted and confirmed absent")
        except Exception as exc:  # noqa: BLE001
            check(res, "cleanup-investigation-row", False,
                  on_fail=f"delete investigation {investigation_id!r} raised: "
                          f"{type(exc).__name__}: {exc}")

    # F6f (review cycle 3 W1): an issue that stopped after the decision approve but
    # before step 14 recorded neither the draft id nor the quality.issue Finding id,
    # and step 5 / step 4 then deleted nothing while cleanup reported PASS.  Recover
    # both from data this run owns: the decided thread row (this run's decision
    # action id) names the draft; the Finding is the one whose subject_key is
    # "<thread_id>/issue".  Recorded under a signal hold before anything is deleted.
    records = _recover_issue_records(res, manifest, manifest_path, stage)

    # campaign_id is used by the moved steps 5 and 7 as well as step 3.
    campaign_id: str = records.get("issued_campaign_id") or records.get("campaign_id") or ""

    # ── 5. Delete DDB recall-campaigns-issued row (W5 F4b: moved before step 3) ──
    # W6 F4b: conditional on thread_id so a later recall that reused the campaign_id
    # is not accidentally deleted.  _issued_ddb_deleted tracks the outcome for the
    # S3 deletion gate below.
    _issued_ddb_deleted = False
    if campaign_id:
        try:
            ddb_issued = _client("dynamodb", _ADP_REGION)
            issued_table = _RECALL_CAMPAIGNS_ISSUED_DDB.format(stage=stage)
            _tid_for_issued: str = records.get("thread_id") or ""
            with _defer_signals_for_restore():
                if _tid_for_issued:
                    from botocore.exceptions import ClientError as _CE_iss  # noqa: PLC0415
                    try:
                        ddb_issued.delete_item(
                            TableName=issued_table,
                            Key={"campaign_id": {"S": campaign_id}},
                            # S2 fix: drop attribute_not_exists(thread_id) arm.
                            # The ADP writer always sets thread_id on draft rows, so
                            # that arm can only match rows written outside the writer
                            # (manual DDB writes, pre-task-9.1 legacy rows).  Those
                            # rows would be silently deleted by the arm; dropping it
                            # means a CCF for an absent row (re-run case) is handled
                            # by the re-read below, and a present row without our
                            # thread_id FAILs rather than being silently deleted.
                            ConditionExpression="thread_id = :our_tid",
                            ExpressionAttributeValues={":our_tid": {"S": _tid_for_issued}},
                        )
                        _issued_ddb_deleted = True
                    except _CE_iss as _cce_iss:
                        if (_cce_iss.response["Error"]["Code"]
                                == "ConditionalCheckFailedException"):
                            # S2 fix: re-read to distinguish absent vs. present-wrong.
                            # Absent row = already deleted (re-run): treat as deleted.
                            # Present row with wrong/absent thread_id = belongs to
                            # another recall: FAIL, nothing deleted.
                            try:
                                _ri = ddb_issued.get_item(
                                    TableName=issued_table,
                                    Key={"campaign_id": {"S": campaign_id}},
                                )
                                if "Item" not in _ri:
                                    # Row already absent — treat as deleted.
                                    _issued_ddb_deleted = True
                                else:
                                    check(res, "cleanup-issued-ddb-row", False,
                                          on_fail=f"issued row {campaign_id!r} has a "
                                                  f"different or absent thread_id; "
                                                  f"belongs to another recall; "
                                                  f"not deleting")
                            except Exception as _gex:  # noqa: BLE001
                                check(res, "cleanup-issued-ddb-row", False,
                                      on_fail=f"re-read of issued row {campaign_id!r} "
                                              f"raised: {type(_gex).__name__}: {_gex}")
                        else:
                            raise
                else:
                    _delete_ddb_item(
                        ddb_issued, issued_table,
                        {"campaign_id": {"S": campaign_id}},
                        f"issued row {campaign_id!r}",
                    )
                    _issued_ddb_deleted = True
            if _issued_ddb_deleted:
                check(res, "cleanup-issued-ddb-row", True,
                      f"recall-campaigns-issued row {campaign_id!r} deleted and "
                      f"confirmed absent")
        except Exception as exc:  # noqa: BLE001
            check(res, "cleanup-issued-ddb-row", False,
                  on_fail=f"delete issued DDB row {campaign_id!r}: {exc}")

    # ── 7. Delete S3 issued-recall object (W5 F4b: moved before step 3) ──────
    # W6 F4b: only delete when the DDB row was confirmed as ours (_issued_ddb_deleted).
    # If the DDB row had a different thread_id, skip S3 deletion too.
    if campaign_id and account:
        if _issued_ddb_deleted:
            bucket = _ADP_LAKE_BUCKET.format(stage=stage, account=account)
            key = f"curated/engineering/recall_campaigns_issued/{campaign_id}.json"
            try:
                s3 = _client("s3", _ADP_REGION)
                with _defer_signals_for_restore():
                    _delete_s3_object(s3, bucket, key)
                check(res, "cleanup-s3-issued-recall", True,
                      f"s3://{bucket}/{key} deleted and confirmed absent")
            except Exception as exc:  # noqa: BLE001
                check(res, "cleanup-s3-issued-recall", False,
                      on_fail=f"delete s3://{bucket}/{key}: {exc}")
        # else: issued-ddb-row FAIL already recorded; S3 skip is implied.
    elif campaign_id and not account:
        check(res, "cleanup-s3-issued-recall", False,
              on_fail="no account in manifest; cannot construct S3 key")

    # ── 3. Delete AVX recall Findings for the campaign ───────────────────────
    # W5 F4b: record the full finding list BEFORE deleting so re-runs retry all ids.
    # W6 F4b: record empty list as [] to distinguish "no findings" from "not queried".
    # S4 F4b: don't swallow the manifest write error.
    if campaign_id:
        try:
            ddb_f = _client("dynamodb", _AVX_REGION)
            # Distinguish "not queried yet" (key absent) from "queried, found none" (key=[]).
            recorded_fids_raw = records.get("deleted_recall_finding_ids")
            if recorded_fids_raw is not None:
                # Re-run: retry only the recorded ids (includes the empty-list case).
                findings = [{"finding_id": fid} for fid in recorded_fids_raw]
            else:
                findings = _query_findings_by_campaign(ddb_f, stage, campaign_id)
                # W5 F4b: record before deleting (even an empty list).
                # S4 F4b: propagate write errors (don't swallow).
                _fids_to_record = [f.get("finding_id") for f in findings
                                   if f.get("finding_id")]
                _ms5 = _read_manifest(manifest_path)
                _ms5.setdefault("records", {})["deleted_recall_finding_ids"] = _fids_to_record
                with _defer_signals_for_restore():
                    _write_manifest(manifest_path, _ms5)
                manifest["records"]["deleted_recall_finding_ids"] = _fids_to_record
            deleted_count = 0
            for f in findings:
                fid = f.get("finding_id")
                if not fid:
                    continue
                try:
                    with _defer_signals_for_restore():
                        _delete_ddb_item(
                            ddb_f,
                            _FINDINGS_TABLE.format(stage=stage),
                            {"finding_id": {"S": fid}},
                            f"finding {fid!r}",
                        )
                    deleted_count += 1
                except Exception as exc2:  # noqa: BLE001
                    check(res, f"cleanup-finding-{fid[:16]}", False,
                          on_fail=f"delete finding {fid!r}: {exc2}")
            check(
                res, "cleanup-recall-findings",
                deleted_count == len(findings),
                f"deleted {deleted_count}/{len(findings)} recall Findings for {campaign_id!r}",
                on_fail=f"only deleted {deleted_count} of {len(findings)} findings",
            )
        except Exception as exc:  # noqa: BLE001
            check(res, "cleanup-recall-findings", False,
                  on_fail=f"findings cleanup raised: {type(exc).__name__}: {exc}")

    # ── 4. Delete AVX quality findings (investigation + issue findings) ───────
    # W7 F4b: track per-finding delete success to gate Action row deletion below.
    inv_finding_id: str = records.get("investigation_finding_id") or ""
    issue_finding_id: str = records.get("issue_finding_id") or ""
    _inv_finding_passed = False
    _iss_finding_passed = False
    for fid, label, _flag_name in [
        (inv_finding_id, "quality.investigation", "inv"),
        (issue_finding_id, "quality.issue", "iss"),
    ]:
        if fid:
            try:
                ddb_qf = _client("dynamodb", _AVX_REGION)
                with _defer_signals_for_restore():
                    _delete_ddb_item(
                        ddb_qf,
                        _FINDINGS_TABLE.format(stage=stage),
                        {"finding_id": {"S": fid}},
                        f"{label} finding {fid!r}",
                    )
                check(res, f"cleanup-{label.replace('.', '-')}-finding", True,
                      f"{label} finding {fid!r} deleted and confirmed absent")
                if _flag_name == "inv":
                    _inv_finding_passed = True
                else:
                    _iss_finding_passed = True
            except Exception as exc:  # noqa: BLE001
                check(res, f"cleanup-{label.replace('.', '-')}-finding", False,
                      on_fail=f"delete {label} finding {fid!r}: {exc}")

    # ── 4b. Delete Action rows (W7 F4b: only after parent Finding PASSed) ────
    # W7 F4b: decision Action deleted only when quality.investigation finding PASSed;
    # issue Action only when quality.issue finding PASSed.
    decision_action_id: str = records.get("decision_action_id") or ""
    issue_action_id_cl: str = records.get("issue_action_id") or ""
    for action_id_cl, action_label, _finding_ok in [
        (decision_action_id, "decision", _inv_finding_passed),
        (issue_action_id_cl, "issue", _iss_finding_passed),
    ]:
        if action_id_cl:
            if not _finding_ok:
                check(res, f"cleanup-{action_label}-action", False,
                      on_fail=f"{action_label} action {action_id_cl!r} not deleted: "
                              f"parent Finding delete did not PASS; re-run after "
                              f"the Finding is gone")
                continue
            try:
                ddb_ac = _client("dynamodb", _AVX_REGION)
                act_table = _ACTIONS_TABLE.format(stage=stage)
                with _defer_signals_for_restore():
                    _delete_ddb_item(
                        ddb_ac, act_table,
                        {"action_id": {"S": action_id_cl}},
                        f"{action_label} action {action_id_cl!r}",
                    )
                check(res, f"cleanup-{action_label}-action", True,
                      f"{action_label} action {action_id_cl!r} deleted and confirmed absent")
            except Exception as exc:  # noqa: BLE001
                check(res, f"cleanup-{action_label}-action", False,
                      on_fail=f"delete {action_label} action {action_id_cl!r}: {exc}")

    # ── 6. Delete digital-thread rows with conditional delete ────────────────
    # F2g: use only the versions recorded in the manifest (not a live DDB query),
    # each with ConditionExpression to prevent deleting rows we didn't write.
    # If the manifest has a thread_id but no thread_versions, cleanup FAIL.
    # If DDB has a version not in the manifest's recorded set, FAIL that version
    # and delete nothing for it (recorded versions are still deleted).
    # W3 F3b guard: FAIL when decision_action_id is recorded but thread_id isn't.
    if decision_action_id and not records.get("thread_id"):
        check(res, "cleanup-thread-rows-deleted", False,
              on_fail="decision_action_id is recorded but thread_id is missing; "
                      "issue may have stopped partway (after the decision but before "
                      "recording thread_id); cannot safely delete thread rows")
    thread_id: str = records.get("thread_id") or ""
    thread_int_versions: list[int] = []  # int version numbers; used in step 7c
    # W3 F4b: track per-version row-delete success (or confirmed-absent) so lake
    # objects and VIN keys are only deleted for versions we actually removed.
    deleted_version_nums: set[int] = set()
    if thread_id:
        # F6e: before deleting the thread rows that name the VIN-list key, merge their
        # keys into the manifest (covers an issue that stopped before recording them).
        try:
            _record_vin_s3_keys(
                manifest, manifest_path, stage, thread_id,
                {a for a in [records.get("decision_action_id") or "",
                             records.get("issue_action_id") or ""] if a},
            )
            records = manifest.get("records", {})
        except Exception as exc:  # noqa: BLE001
            check(res, "cleanup-record-vin-keys", False,
                  on_fail=f"recording VIN-list keys before the thread delete raised: "
                          f"{type(exc).__name__}: {exc}")
        recorded_versions_raw = records.get("thread_versions")
        if not recorded_versions_raw:
            check(res, "cleanup-thread-rows-deleted", False,
                  on_fail=f"thread_id {thread_id!r} in manifest but thread_versions "
                          f"not recorded; issue may have failed before recording them; "
                          f"cannot safely delete thread rows")
        else:
            try:
                manifest_versions: list[int] = sorted(int(v) for v in recorded_versions_raw)
                thread_int_versions = manifest_versions
                ddb_dt = _client("dynamodb", _ADP_REGION)
                dt_table = _DIGITAL_THREAD_TABLE.format(stage=stage)

                # Query all DDB versions to detect any not in the manifest.
                ddb_versions: list[int] = []
                kwargs_dt: dict[str, Any] = {
                    "TableName": dt_table,
                    "KeyConditionExpression": "thread_id = :t",
                    "ExpressionAttributeValues": {":t": {"S": thread_id}},
                }
                while True:
                    page = ddb_dt.query(**kwargs_dt)
                    for item in page.get("Items", []):
                        ver_n = item.get("version", {}).get("N")
                        if ver_n:
                            ddb_versions.append(int(ver_n))
                    if not page.get("LastEvaluatedKey"):
                        break
                    kwargs_dt["ExclusiveStartKey"] = page["LastEvaluatedKey"]

                unrecorded = sorted(v for v in ddb_versions if v not in manifest_versions)
                if unrecorded:
                    check(res, "cleanup-thread-rows-deleted", False,
                          on_fail=f"thread {thread_id!r}: DDB has version(s) {unrecorded!r} "
                                  f"not recorded in manifest (manifest has {manifest_versions!r}); "
                                  f"refusing to delete unrecorded versions")
                    # Still attempt deletion of recorded versions below.

                # Delete each recorded version with a conditional delete.
                # W2 F3b fix: tie the condition to this run's action_id values so
                # cleanup can't silently delete a version that was written by a
                # concurrent run.  On ConditionalCheckFailed: read the key — if
                # absent it was already deleted (re-run passes); if present with a
                # different action_id it belongs to another run (FAIL, don't delete).
                from botocore.exceptions import ClientError as _CE_thread  # noqa: PLC0415
                dec_action_id_cl: str = records.get("decision_action_id") or ""
                iss_action_id_cl2: str = records.get("issue_action_id") or ""
                # Build the per-run condition.  At least one action_id must be recorded.
                if dec_action_id_cl and iss_action_id_cl2:
                    thread_condition = "action_id IN (:dec_aid, :iss_aid)"
                    thread_condition_values = {
                        ":dec_aid": {"S": dec_action_id_cl},
                        ":iss_aid": {"S": iss_action_id_cl2},
                    }
                elif dec_action_id_cl:
                    thread_condition = "action_id = :dec_aid"
                    thread_condition_values = {":dec_aid": {"S": dec_action_id_cl}}
                else:
                    # No action_ids recorded → cannot produce a per-run condition.
                    check(res, "cleanup-thread-rows-deleted", False,
                          on_fail="no action_id values in manifest; cannot build "
                                  "per-run condition for thread row delete")
                    thread_condition = None
                    thread_condition_values = {}
                deleted_rows = 0
                delete_errors: list[str] = []
                if thread_condition:
                    for _ver in manifest_versions:
                        try:
                            with _defer_signals_for_restore():
                                ddb_dt.delete_item(
                                    TableName=dt_table,
                                    Key={
                                        "thread_id": {"S": thread_id},
                                        "version": {"N": str(_ver)},
                                    },
                                    ConditionExpression=thread_condition,
                                    ExpressionAttributeValues=thread_condition_values,
                                )
                            deleted_rows += 1
                            deleted_version_nums.add(_ver)  # W3 F4b: track success
                        except _CE_thread as _ccf:
                            if _ccf.response["Error"]["Code"] == "ConditionalCheckFailedException":
                                # Read the key to distinguish "already deleted" from
                                # "belongs to another run".
                                _read = ddb_dt.get_item(
                                    TableName=dt_table,
                                    Key={"thread_id": {"S": thread_id}, "version": {"N": str(_ver)}},
                                    ConsistentRead=True,
                                )
                                if _read.get("Item"):
                                    # Row present with a different action_id — another run's.
                                    delete_errors.append(
                                        f"v{_ver}: ConditionalCheckFailed and row still present "
                                        f"(action_id not this run's); belongs to another run"
                                    )
                                else:
                                    # Row already absent — a previous cleanup deleted it.
                                    deleted_rows += 1
                                    deleted_version_nums.add(_ver)  # W3 F4b: absent = ok
                            else:
                                delete_errors.append(
                                    f"v{_ver}: {type(_ccf).__name__}: {_ccf}"
                                )
                        except Exception as _exc_del:  # noqa: BLE001
                            delete_errors.append(
                                f"v{_ver}: {type(_exc_del).__name__}: {_exc_del}"
                            )

                if not delete_errors and not unrecorded and thread_condition:
                    check(res, "cleanup-thread-rows-deleted", True,
                          f"deleted {deleted_rows} thread row(s) for {thread_id!r} "
                          f"versions={manifest_versions!r} with ConditionExpression")
                elif delete_errors:
                    check(res, "cleanup-thread-rows-deleted", False,
                          on_fail=f"thread row delete errors for {thread_id!r}: "
                                  f"{delete_errors!r}")

            except Exception as exc:  # noqa: BLE001
                check(res, "cleanup-thread-rows-deleted", False,
                      on_fail=f"thread row deletion raised: {type(exc).__name__}: {exc}")

    # ── 6b. Delete investigations lock row ────────────────────────────────────
    # C4 fix (facts.md §C4): cleanup must delete the lock#<pattern_id> row that
    # POST /investigations writes.  The investigation may leave it behind on failure.
    # W13 F3a fix: delete is conditional on investigation_id_ref = :investigation_id
    # (attribute_not_exists covers the already-absent / re-run case; ConditionalCheckFailed
    # when the lock has a different ref means it belongs to another run).
    # W4 F4b: a different investigation_id_ref means "not ours" — PASS with a note,
    # and signal step 8 to omit the lock from must_be_gone (_lock_foreign flag).
    pattern_id_cl: str = manifest.get("pattern_id") or ""
    _lock_foreign = False  # W4 F4b: set when lock belongs to another investigation
    if pattern_id_cl and investigation_id:
        lock_pk = f"lock#{pattern_id_cl}"
        try:
            from botocore.exceptions import ClientError as _CE_lock  # noqa: PLC0415
            ddb_lock = _client("dynamodb", _AVX_REGION)
            inv_table_cl = _INV_TABLE.format(stage=stage)
            with _defer_signals_for_restore():
                try:
                    ddb_lock.delete_item(
                        TableName=inv_table_cl,
                        Key={"investigation_id": {"S": lock_pk}},
                        ConditionExpression=(
                            "attribute_not_exists(investigation_id) OR "
                            "investigation_id_ref = :inv_id"
                        ),
                        ExpressionAttributeValues={":inv_id": {"S": investigation_id}},
                    )
                except _CE_lock as _cce:
                    if _cce.response["Error"]["Code"] == "ConditionalCheckFailedException":
                        # Lock exists with a different investigation_id_ref — belongs to
                        # another run.  Read and report.
                        _r = ddb_lock.get_item(
                            TableName=inv_table_cl,
                            Key={"investigation_id": {"S": lock_pk}},
                            ConsistentRead=True,
                        )
                        if _r.get("Item"):
                            # W4 F4b: lock has a different investigation_id_ref —
                            # it belongs to another run.  PASS with a note and leave
                            # it untouched; _lock_foreign signals step 8 to omit it
                            # from must_be_gone.
                            _lock_foreign = True
                        # If absent: already deleted (falls through to confirm/PASS).
                    else:
                        raise
                if _lock_foreign:
                    check(res, "cleanup-lock-row", True,
                          f"lock row {lock_pk!r} belongs to another investigation "
                          f"(investigation_id_ref differs); leaving untouched")
                else:
                    # Confirm absent
                    _confirm = ddb_lock.get_item(
                        TableName=inv_table_cl,
                        Key={"investigation_id": {"S": lock_pk}},
                        ConsistentRead=True,
                    )
                    if _confirm.get("Item"):
                        raise RuntimeError(
                            f"lock row {lock_pk!r} still present after conditional delete"
                        )
                    check(res, "cleanup-lock-row", True,
                          f"lock row {lock_pk!r} conditionally deleted and confirmed absent "
                          f"(investigation_id_ref matched)")
        except Exception as exc:  # noqa: BLE001
            check(res, "cleanup-lock-row", False,
                  on_fail=f"delete lock row {lock_pk!r}: {exc}")

    # ── 7b. Delete ADP thread-lake objects (flat layout only) ────────────────
    # F2g: flat layout confirmed (deployed writer a946eb98 + reconcile_thread_lake.py
    # both use curated/engineering/digital_thread/<thread_id>.v<n>.json).
    # The nested curated/engineering/digital_thread/<thread_id>/ prefix is dropped.
    # W1 F3b fix: delete ONLY the keys for each recorded version; FAIL on any other
    # key under the flat prefix (an unrecorded version's lake object).
    # W3 F4b: only delete lake objects for versions whose row delete succeeded or was
    # confirmed absent (tracked in deleted_version_nums).
    # thread_id must match ^thread-[0-9a-f]{16}$.
    if thread_id and account:
        if not _THREAD_ID_RE.fullmatch(thread_id):
            check(res, "cleanup-thread-lake-objects", False,
                  on_fail=f"thread_id {thread_id!r} does not match "
                          f"^thread-[0-9a-f]{{16}}$")
        else:
            bucket_tl = _ADP_LAKE_BUCKET.format(stage=stage, account=account)
            flat_prefix = f"curated/engineering/digital_thread/{thread_id}.v"
            # Build expected keys from recorded versions only.
            expected_tl_keys: set[str] = {
                f"curated/engineering/digital_thread/{thread_id}.v{_v}.json"
                for _v in thread_int_versions
            }
            listed_tl_keys: list[str] = []
            tl_list_ok = True
            try:
                s3_tl = _client("s3", _ADP_REGION)
                _kwargs_tl: dict[str, Any] = {
                    "Bucket": bucket_tl,
                    "Prefix": flat_prefix,
                }
                while True:
                    _page_tl = s3_tl.list_objects_v2(**_kwargs_tl)
                    for _obj in _page_tl.get("Contents", []):
                        listed_tl_keys.append(_obj["Key"])
                    if not _page_tl.get("IsTruncated"):
                        break
                    _kwargs_tl["ContinuationToken"] = (
                        _page_tl["NextContinuationToken"]
                    )
            except Exception as exc:  # noqa: BLE001
                check(res, "cleanup-thread-lake-objects", False,
                      on_fail=f"thread-lake list raised: {type(exc).__name__}: {exc}")
                tl_list_ok = False

            if tl_list_ok:
                # Detect any key that is not in the expected set.
                unrecorded_tl = sorted(k for k in listed_tl_keys if k not in expected_tl_keys)
                if unrecorded_tl:
                    check(res, "cleanup-thread-lake-objects", False,
                          on_fail=f"thread {thread_id!r}: lake has object(s) for "
                                  f"unrecorded version(s): {unrecorded_tl!r}; "
                                  f"manifest records versions {sorted(thread_int_versions)!r}; "
                                  f"refusing to delete unrecorded lake objects")
                    # Fall through: still attempt deletion of recorded-version keys.

                # W3 F4b: only delete lake objects for versions whose row delete
                # succeeded or was confirmed absent (tracked in deleted_version_nums).
                safe_tl_keys: set[str] = {
                    f"curated/engineering/digital_thread/{thread_id}.v{_sv}.json"
                    for _sv in deleted_version_nums
                }
                recorded_to_delete = [
                    k for k in safe_tl_keys if k in set(listed_tl_keys)
                ]
                deleted_tl = 0
                for _tl_key in sorted(recorded_to_delete):
                    try:
                        with _defer_signals_for_restore():
                            _delete_s3_object(s3_tl, bucket_tl, _tl_key)
                        deleted_tl += 1
                    except Exception as _exc_tl:  # noqa: BLE001
                        check(
                            res, f"cleanup-thread-lake-{_tl_key[-16:]}", False,
                            on_fail=f"delete s3://{bucket_tl}/{_tl_key}: "
                                    f"{_exc_tl}",
                        )

                if not unrecorded_tl:
                    if deleted_tl == len(recorded_to_delete):
                        check(res, "cleanup-thread-lake-objects", True,
                              f"deleted {deleted_tl} thread-lake object(s) for "
                              f"recorded versions {sorted(thread_int_versions)!r}")
                    else:
                        check(res, "cleanup-thread-lake-objects", False,
                              on_fail=f"only deleted {deleted_tl}/"
                                      f"{len(recorded_to_delete)} recorded "
                                      f"thread-lake objects")

    # ── 7c. Delete ADP VIN-list objects (required; not best-effort) ──────────
    # S4 F3b fix: use the keys named by each recorded row's affected_vins_s3_key
    # (stored as vin_s3_keys in the manifest), not a derived path.
    # The issued version (v2) copies the decided version's key (both share v1's path);
    # the current code's derived "v2/affected-vins.json" was never written by the writer.
    # If thread_id is present but vin_s3_keys are not recorded, FAIL (cannot derive safely).
    # W3 F4b: only delete VIN keys whose version (extracted from the path) was
    # successfully deleted (or confirmed absent) from DDB (deleted_version_nums).
    if thread_id and account:
        vin_s3_keys_cl: list[str] = records.get("vin_s3_keys") or []
        if not vin_s3_keys_cl:
            check(res, "cleanup-vin-list-objects", False,
                  on_fail="VIN-list keys cannot be determined: "
                          "vin_s3_keys not recorded in manifest "
                          "(run --part issue first to record affected_vins_s3_key per version); "
                          "refusing to silently skip VIN-list deletion")
        else:
            vin_bucket = _ADP_VIN_BUCKET.format(stage=stage, account=account)
            deleted_vl = 0
            s3_vl = _client("s3", _ADP_REGION)

            def _ver_from_vin_key(_vk_: str) -> "int | None":
                # key: digital-thread/{stage}/{thread_id}/v{N}/affected-vins.json
                for _part_ in _vk_.split("/"):
                    if _part_.startswith("v") and _part_[1:].isdigit():
                        return int(_part_[1:])
                return None

            for _vk in vin_s3_keys_cl:
                # W3 F4b: skip if this key's version was not successfully deleted.
                _vk_ver = _ver_from_vin_key(_vk)
                if _vk_ver is not None and deleted_version_nums and _vk_ver not in deleted_version_nums:
                    continue  # row delete failed/skipped for this version; skip VIN key too
                try:
                    with _defer_signals_for_restore():
                        _delete_s3_object(s3_vl, vin_bucket, _vk)
                    deleted_vl += 1
                except Exception as _exc_vl:  # noqa: BLE001
                    check(res, f"cleanup-vin-list-{_vk[-20:]}", False,
                          on_fail=f"delete s3://{vin_bucket}/{_vk}: {_exc_vl}")
            # Count keys eligible for deletion (only those whose version was deleted).
            eligible_vl = [
                _vk for _vk in vin_s3_keys_cl
                if not (
                    _ver_from_vin_key(_vk) is not None
                    and deleted_version_nums
                    and _ver_from_vin_key(_vk) not in deleted_version_nums
                )
            ]
            if deleted_vl == len(eligible_vl):
                check(res, "cleanup-vin-list-objects", True,
                      f"deleted {deleted_vl} VIN-list object(s): {eligible_vl!r}")
            else:
                check(res, "cleanup-vin-list-objects", False,
                      on_fail=f"only deleted {deleted_vl}/{len(eligible_vl)} "
                              f"VIN-list objects")

    # ── 7d. S3 residue check: confirm no current objects remain ──────────────
    # F2g: flat layout only; nested prefix dropped.  Delete markers are acceptable
    # (bucket has versioning Enabled); list_objects_v2 skips delete markers.
    if thread_id and account and _THREAD_ID_RE.fullmatch(thread_id):
        bucket_r = _ADP_LAKE_BUCKET.format(stage=stage, account=account)
        flat_pfx_r = f"curated/engineering/digital_thread/{thread_id}.v"
        s3_r = _client("s3", _ADP_REGION)
        residue_keys: list[str] = []
        try:
            _kw_r: dict[str, Any] = {"Bucket": bucket_r, "Prefix": flat_pfx_r}
            while True:
                _pg_r = s3_r.list_objects_v2(**_kw_r)
                for _ro in _pg_r.get("Contents", []):
                    residue_keys.append(_ro["Key"])
                if not _pg_r.get("IsTruncated"):
                    break
                _kw_r["ContinuationToken"] = _pg_r["NextContinuationToken"]
            if not residue_keys:
                check(res, "cleanup-thread-lake-residue", True,
                      f"no current thread-lake objects remain under {flat_pfx_r!r} "
                      f"(noncurrent versions / delete markers may remain by design)")
            else:
                check(res, "cleanup-thread-lake-residue", False,
                      on_fail=f"current thread-lake residue after cleanup: "
                               f"{residue_keys!r}")
        except Exception as exc:  # noqa: BLE001
            check(res, "cleanup-thread-lake-residue", False,
                  on_fail=f"thread-lake residue check raised: "
                          f"{type(exc).__name__}: {exc}")

    # ── 7e. After-cleanup reconcile: no entry for this thread ────────────────
    # F2g: after deleting rows + lake objects, reconcile_thread_lake.py (dry run)
    # must report no entry (missing, differs, extra, unprojectable) for this thread.
    if thread_id and _THREAD_ID_RE.fullmatch(thread_id):
        _run_reconcile_check(
            res, stage, thread_id,
            label_prefix="cleanup-reconcile",
            after_cleanup=True,
        )

    # ── 8. Residue check: compare run keys against post-cleanup scans ────────
    # C4 fix: build must_be_gone from manifest records; expected_residue = cap row.
    try:
        ddb_residue_avx = _client("dynamodb", _AVX_REGION)
        ddb_residue_adp = _client("dynamodb", _ADP_REGION)
        inv_table_r = _INV_TABLE.format(stage=stage)
        must_be_gone: list[dict] = []
        # investigation row
        if records.get("investigation_id"):
            must_be_gone.append({
                "table": inv_table_r,
                "key": {"investigation_id": {"S": records["investigation_id"]}},
            })
        # lock row (W4 F4b: skip when the lock belongs to another investigation)
        if pattern_id_cl and not _lock_foreign:
            must_be_gone.append({
                "table": inv_table_r,
                "key": {"investigation_id": {"S": f"lock#{pattern_id_cl}"}},
            })
        # Action rows (W4 F3a fix)
        if decision_action_id:
            must_be_gone.append({
                "table": _ACTIONS_TABLE.format(stage=stage),
                "key": {"action_id": {"S": decision_action_id}},
            })
        if issue_action_id_cl:
            must_be_gone.append({
                "table": _ACTIONS_TABLE.format(stage=stage),
                "key": {"action_id": {"S": issue_action_id_cl}},
            })
        # cap row is expected residue (not deleted)
        # C4 F3a fix: use cap_date recorded at POST time, not date.today().
        expected_residue: list[dict] = []
        cap_date_cl = records.get("cap_date")
        if cap_date_cl:
            expected_residue.append({
                "table": inv_table_r,
                "key": {"investigation_id": {"S": f"cap#{cap_date_cl}"}},
            })
        run_residue_spec: dict = {
            "must_be_gone": must_be_gone,
            "expected_residue": expected_residue,
        }
        _check_residue(res, ddb_residue_avx, ddb_residue_adp, stage,
                       run_residue_spec)
    except Exception as exc:  # noqa: BLE001
        check(res, "cleanup-residue-match", False,
              on_fail=f"residue check raised: {type(exc).__name__}: {exc}")

    # ── 9. Record cleanup completion time in manifest (W6 rest fix) ──────────
    # after-cleanup requires a recall run started after cleanup completed;
    # record completion time so after-cleanup can gate on it.
    # W2 F3a fix: only write the stamp when there are no FAIL rows; a failed
    # cleanup must not advance after-cleanup as if it succeeded.
    if res.has_any_fail:
        check(res, "cleanup-completion-time", False,
              on_fail="not recorded: part has FAIL row(s); re-run cleanup after fixing")
    else:
        try:
            existing_cl = _read_manifest(manifest_path)
            existing_cl.setdefault("records", {})["part_cleanup_completed_at"] = (
                _utc_now().isoformat()
            )
            _write_manifest(manifest_path, existing_cl)
            check(res, "cleanup-completion-time", True,
                  f"part_cleanup_completed_at recorded in manifest at {manifest_path!r}")
        except Exception as exc:  # noqa: BLE001
            check(res, "cleanup-completion-time", False,
                  on_fail=f"recording cleanup completion time raised: "
                          f"{type(exc).__name__}: {exc}")


# ── after-cleanup part ────────────────────────────────────────────────────────

def _part_after_cleanup(res: Results, *, stage: str, run_id: str, **_: Any) -> None:
    """Part after-cleanup: verify the campaign id is gone after the following recall run.

    Read-only.  Brief §after-cleanup:
    - id gone from recall_vehicle_status and recall_campaigns_received views;
    - no new Findings for it.
    """
    _init_session("after-cleanup")
    print(f"\n── after-cleanup: stage={stage!r} run_id={run_id!r}")

    # Read manifest
    manifest_path = _manifest_path(run_id)
    try:
        manifest = _read_manifest(manifest_path)
    except FileNotFoundError:
        check(res, "after-cleanup-manifest-read", False,
              on_fail=f"manifest not found at {manifest_path!r}; run --part cleanup first")
        return
    except Exception as exc:  # noqa: BLE001
        check(res, "after-cleanup-manifest-read", False,
              on_fail=f"manifest read failed: {type(exc).__name__}: {exc}")
        return

    if manifest.get("stage") != stage:
        check(res, "after-cleanup-manifest-stage", False,
              on_fail=f"manifest stage={manifest.get('stage')!r} != {stage!r}")
        return
    check(res, "after-cleanup-manifest-read", True, "manifest read OK")

    records = manifest.get("records", {})
    campaign_id: str = records.get("issued_campaign_id") or records.get("campaign_id") or ""
    cleanup_completed_at: str = records.get("part_cleanup_completed_at") or ""

    if not campaign_id or not _CAMPAIGN_ID_RE.fullmatch(campaign_id):
        check(res, "after-cleanup-campaign-id", False,
              on_fail=f"no valid campaign_id in manifest (got {campaign_id!r})")
        return
    check(res, "after-cleanup-campaign-id", True, f"campaign_id={campaign_id!r}")

    # W6 rest fix: require part_cleanup_completed_at in manifest before any check.
    if not cleanup_completed_at:
        check(res, "after-cleanup-cleanup-completed", False,
              on_fail="no part_cleanup_completed_at in manifest; "
                      "run --part cleanup first and wait for the scheduled recall run "
                      "at 05:00Z before running --part after-cleanup")
        return
    check(res, "after-cleanup-cleanup-completed", True,
          f"part_cleanup_completed_at={cleanup_completed_at!r}")

    # ── 1. Recall run happened after cleanup ──────────────────────────────────
    # W6 rest fix: after-cleanup requires a completed recall run that started after
    # cleanup finished (not just after issue), so we confirm the view-clearing effect
    # is from a run that saw the cleaned-up state.
    try:
        ddb_avx_runs = _client("dynamodb", _AVX_REGION)
        run_table = _AGENT_RUNS_TABLE.format(stage=stage)
        runs: list[dict] = []
        kw_runs: dict[str, Any] = {
            "TableName": run_table,
            "KeyConditionExpression": "agent_component = :pk",
            "ExpressionAttributeValues": {":pk": {"S": _RECALL_AGENT_COMPONENT}},
        }
        while True:
            page = ddb_avx_runs.query(**kw_runs)
            for item in page.get("Items", []):
                runs.append(_unwrap_ddb_item(item))
            if not page.get("LastEvaluatedKey"):
                break
            kw_runs["ExclusiveStartKey"] = page["LastEvaluatedKey"]
        runs_after_cleanup = [
            r for r in runs
            if r.get("started_at", "") > cleanup_completed_at
        ]
        check(
            res, "after-cleanup-recall-run",
            bool(runs_after_cleanup),
            f"recall run record found after cleanup_completed_at={cleanup_completed_at!r}; "
            f"latest started_at="
            f"{max((r.get('started_at','') for r in runs_after_cleanup), default='none')!r}",
            on_fail=f"no recall run after cleanup_completed_at={cleanup_completed_at!r}; "
                    f"wait for the scheduled 05:00Z recall run before running after-cleanup",
        )
    except Exception as exc:  # noqa: BLE001
        check(res, "after-cleanup-recall-run", False,
              on_fail=f"recall run record query raised: {type(exc).__name__}: {exc}")

    # ── 2. recall_vehicle_status should have 0 rows for the campaign ──────────
    # W6 rest fix: count ALL rows (including remedied=TRUE); a remedied row for the
    # campaign_id still means the campaign is present in the view.
    try:
        athena = _client("athena", _ADP_REGION)
        sql = _RECALL_STATUS_ALL_SQL.format(stage=stage, campaign_id=campaign_id)
        rvs_rows = _athena_query_paged(athena, stage, sql, "recall_vehicle_status-cleanup")
        remedied_count = sum(1 for r in rvs_rows if r.get("remedied") is True)
        check(
            res, "after-cleanup-recall-vehicle-status",
            len(rvs_rows) == 0,
            f"recall_vehicle_status: 0 rows for {campaign_id!r} after cleanup",
            on_fail=f"recall_vehicle_status still has {len(rvs_rows)} rows for "
                    f"{campaign_id!r} ({remedied_count} remedied); "
                    f"S3 object not removed or recall run reprocessed",
        )

        # W6 fix: also check recall_campaigns_received (brief §after-cleanup:
        # "id gone from recall_vehicle_status AND recall_campaigns_received views").
        rcr_sql = _RECALL_CATALOG_SQL.format(stage=stage, campaign_id=campaign_id)
        rcr_rows = _athena_query_paged(
            athena, stage, rcr_sql, "recall_campaigns_received-cleanup"
        )
        check(
            res, "after-cleanup-recall-campaigns-received",
            len(rcr_rows) == 0,
            f"recall_campaigns_received: 0 rows for {campaign_id!r} after cleanup",
            on_fail=f"recall_campaigns_received still has {len(rcr_rows)} rows for "
                    f"{campaign_id!r}; S3 issued object not removed or recall run "
                    f"re-ingested it",
        )
    except Exception as exc:  # noqa: BLE001
        check(res, "after-cleanup-recall-vehicle-status", False,
              on_fail=f"recall_vehicle_status query raised: {type(exc).__name__}: {exc}")

    # ── 3. No new Findings for the campaign ───────────────────────────────────
    try:
        ddb_f = _client("dynamodb", _AVX_REGION)
        findings = _query_findings_by_campaign(ddb_f, stage, campaign_id)
        check(
            res, "after-cleanup-no-new-findings",
            len(findings) == 0,
            f"0 Findings for {campaign_id!r} after cleanup",
            on_fail=f"{len(findings)} Findings still present for {campaign_id!r} "
                    f"after cleanup; the recall run may have re-written them",
        )
    except Exception as exc:  # noqa: BLE001
        check(res, "after-cleanup-no-new-findings", False,
              on_fail=f"findings check raised: {type(exc).__name__}: {exc}")

def _part_ground(res: Results, *, stage: str, run_id: str, allow_writes: bool = False,
                 allow_managed_writes: bool = False, **_: Any) -> None:
    """Part ground: leg G — fault-grounding flow for a managed fleet vehicle.

    Brief §ground: --allow-writes --allow-managed-writes.
    Steps:
    1. preconditions (window guard, no open LIVETEST; S11 fix: Tier 2 code and
       recall-table checks are not part of this part's preconditions)
    2. pick a managed VIN with a p0 fleet-health Finding carrying ground_vehicle
       recommended_action and no open booking
    3. create temp fleet-operator user for the vehicle's fleet
    4. approve ground_vehicle → CMS fault grounding
    5. negative control: return-to-service refused (fault_not_repaired; S10 fix:
       compare refusal's fault_codes with the Finding's)
    6. approve book_fleet_service → DMS RO
    7. stand-in: invoke dms-{stage}-fi-dealer-progress only when every OPEN FI
       booking is this run's; otherwise note for the scheduled run
    8. ADP fault view: confirm repair_ref (ALL codes; S11 fix: DMS lake merge
       30 min lag; no ADP daily job; CMS health cache up to 1 h lag)
    9. return-to-service accepted
    10. residue = manifest's planned set (grounding + RO are intentional residue)

    C7 fix: per-sub-step manifest state recorded inside _defer_signals_for_restore
    BEFORE each write.  On --run-id resume, skip completed sub-steps without
    re-creating the user or re-approving.  Manifest write errors are fatal (not
    swallowed) so interrupted runs leave an accurate state.

    Sub-steps in manifest records:
      user             username of the fleet-operator temp user (set before AdminCreateUser)
      grounded         "true" once ground_vehicle approve succeeds
      neg_ctrl         "sent" before step-5 POST (C1 fix); updated to "{status}:{reason}"
                       if the POST returned something other than the expected 409 refusal
      neg_ctrl_refused "true" once RTS is confirmed refused before repair (step 5; C1 fix:
                       recorded only when the exact expected 409 fault_not_repaired arrived)
      return_blocked   reason string if step-9 refused for anything other than
                       fault_not_repaired; grounding is then operator residue (W1 fix)
      booked           "true" once book_fleet_service approve succeeds
      advanced_N       "true" for each stand-in invocation pass N=1,2,...
      repaired         "true" once ADP fault view confirms repair_ref for all codes
      returned         "true" once RTS is accepted; "pending" while the POST is in flight

    W5 fix: re-check preconditions at part start (incl. no open LIVETEST), and
    re-check window before each write (ground approve, booking approve, stand-in
    Invoke, RTS POST).
    """
    if not allow_writes:
        res.skip("ground", "requires --allow-writes; run with --allow-writes to enable")
        return
    if not allow_managed_writes:
        res.skip("ground",
                 "requires --allow-managed-writes; run with both --allow-writes and "
                 "--allow-managed-writes to enable writes to managed fleet tables")
        return
    _init_session("ground")
    print(f"\n── ground: stage={stage!r} run_id={run_id!r}")

    # ── C7: read manifest sub-step state (K1 stage check + resume guard) ─────
    manifest_path = _manifest_path(run_id)
    manifest_records: dict[str, Any] = {}
    account: str = ""  # W1: needed for CMS recall-slice pick check
    try:
        existing = _read_manifest(manifest_path)
        if existing.get("stage") != stage:
            check(res, "ground-manifest-stage", False,
                  on_fail=f"manifest stage={existing.get('stage')!r} != {stage!r} (K1: "
                          f"run --part pre --stage {stage!r} first)")
            return
        manifest_records = existing.get("records", {}) or {}
        account = existing.get("account") or ""
    except FileNotFoundError:
        # S1 fix: FAIL early — without a manifest, the recall-slice pick check
        # is bypassed (account == ""), which can cause a VIN outside the recall
        # cohort to be grounded.  Mirroring _part_cleanup's manifest guard.
        check(res, "ground-manifest-read", False,
              on_fail=f"manifest not found at {manifest_path!r}; "
                      f"run --part pre --stage {stage!r} first "
                      "(S1 fix: missing manifest bypasses recall-slice pick check)")
        return
    except Exception as exc:  # noqa: BLE001
        check(res, "ground-manifest-read", False,
              on_fail=f"manifest read failed: {type(exc).__name__}: {exc}")
        return

    # Helper: write manifest record; errors are fatal (C7: don't swallow).
    def _record_sub_step(key: str, value: str) -> None:
        """Record a sub-step in the manifest; raises on write error (C7 fix)."""
        m: dict[str, Any] = {}
        try:
            m = _read_manifest(manifest_path)
        except FileNotFoundError:
            pass
        m.setdefault("stage", stage)
        m.setdefault("run_id", run_id)
        m.setdefault("records", {}).update({key: value})
        _write_manifest(manifest_path, m)  # raises on error — C7: no swallowing
        manifest_records[key] = value

    # Helper: window guard (W5: re-checked before every write).
    def _assert_window_ok(label: str) -> bool:
        """Check window; records FAIL and returns False if outside window."""
        try:
            now = _utc_now()
            maint_reason = _maint_window_guard(now)
            if maint_reason:
                check(res, label, False, on_fail=maint_reason)
                return False
            sched_client = _client("scheduler", _AVX_REGION)
            reason = _schedule_guard(sched_client, _SCHEDULES, stage, now)
            if reason:
                check(res, label, False, on_fail=reason)
                return False
        except Exception as exc:  # noqa: BLE001
            check(res, label, False,
                  on_fail=f"window check failed: {type(exc).__name__}: {exc}")
            return False
        return True

    # ── 1. preconditions ─────────────────────────────────────────────────────
    # W5: re-check at part start (brief: preconditions re-checked at ground start).
    # Includes initial window guard + no open LIVETEST.
    if not _assert_window_ok("ground-window"):
        return
    check(res, "ground-window", True,
          f"not within {_SCHED_GUARD_MIN} min of any scheduled job")

    # W5: no open LIVETEST (brief precondition re-checked at ground start).
    # On resume, this run's user(s) may already exist in the pool;
    # exclude them from the check (intentional residue from this run).
    # S3 fix: use the same "exactly one pool" rule as _check_no_open_livetest;
    # exempt every user this run has recorded (the ground user plus any user
    # already in manifest_records that starts with _LIVETEST_USER_PREFIX).
    current_run_user = f"{_LIVETEST_USER_PREFIX}{run_id}-fo@example.invalid"
    try:
        idp_pre = _client("cognito-idp", _AVX_REGION)
        try:
            pools: list[dict[str, Any]] = []
            kw: dict[str, Any] = {"MaxResults": 60}
            while True:
                page = idp_pre.list_user_pools(**kw)
                pools.extend(p for p in page.get("UserPools", []) if p["Name"] == _CMS_POOL_NAME)
                tok = page.get("NextToken")
                if not tok:
                    break
                kw["NextToken"] = tok
            # S3 fix: same one-pool rule as _check_no_open_livetest.
            if len(pools) != 1:
                check(res, "ground-no-open-livetest", False,
                      on_fail=f"expected exactly one pool named {_CMS_POOL_NAME!r}, "
                              f"found {len(pools)} (S3 fix: same rule as _resolve_pool_id)")
                livetest_ok = False
            else:
                pool_id_lv = pools[0]["Id"]
                # S3 fix: exempt every user this run has recorded, not just the
                # ground user.  A prior leg (e.g., R) may have left a user in the pool
                # that belongs to this run_id and should not block ground from resuming.
                current_run_users: set[str] = {current_run_user}
                for _v in manifest_records.values():
                    if isinstance(_v, str) and _v.startswith(_LIVETEST_USER_PREFIX):
                        current_run_users.add(_v)
                leftover: list[str] = []
                kw2: dict[str, Any] = {
                    "UserPoolId": pool_id_lv,
                    "Filter": f'email ^= "{_LIVETEST_USER_PREFIX}"',
                }
                while True:
                    page2 = idp_pre.list_users(**kw2)
                    for u in page2.get("Users", []):
                        email_val = ""
                        for attr in u.get("Attributes", []):
                            if attr.get("Name") == "email":
                                email_val = attr.get("Value", "")
                                break
                        user_email = email_val or u.get("Username", "")
                        # S3 fix: exclude any user this run recorded.
                        if user_email not in current_run_users:
                            leftover.append(user_email)
                    tok2 = page2.get("PaginationToken") or page2.get("NextToken")
                    if not tok2:
                        break
                    kw2["PaginationToken"] = tok2
                livetest_ok = not leftover
                check(res, "ground-no-open-livetest", livetest_ok,
                      f"no leftover {_LIVETEST_USER_PREFIX!r} users (excl. current run) "
                      f"in pool {pool_id_lv!r}",
                      on_fail=f"{len(leftover)} leftover livetest user(s) from other runs: "
                              f"{leftover[:3]}; run --part cleanup before resuming")
        except Exception as exc:  # noqa: BLE001
            check(res, "ground-no-open-livetest", False,
                  on_fail=f"LIVETEST check raised: {type(exc).__name__}: {exc}")
            livetest_ok = False
    except Exception as exc:  # noqa: BLE001
        check(res, "ground-no-open-livetest", False,
              on_fail=f"open-livetest re-check raised: {type(exc).__name__}: {exc}")
        livetest_ok = False
    if not livetest_ok:
        return

    # ── 2. pick target Finding (or resume from manifest) ─────────────────────
    vin = ""
    fleet_id = ""
    fault_codes: list[str] = []
    finding_id = ""
    # C7 resume: if manifest has ground_vin, re-use it (skip the pick scan).
    if manifest_records.get("ground_vin") and manifest_records.get("ground_finding_id"):
        vin = str(manifest_records["ground_vin"])
        fleet_id = str(manifest_records.get("ground_fleet_id", ""))
        finding_id = str(manifest_records["ground_finding_id"])
        fc_raw = str(manifest_records.get("ground_fault_codes", ""))
        fault_codes = [c for c in fc_raw.split(",") if c] if fc_raw else []
        check(res, "ground-finding-pick", True,
              f"resume: using manifest vin={vin!r} fleet={fleet_id!r} "
              f"finding={finding_id!r} fault_codes={fault_codes!r}")
    else:
        try:
            ddb_avx = _client("dynamodb", _AVX_REGION)
            now = _utc_now()
            skipped_picks: list[str] = []
            finding: dict[str, Any] | None = None
            for candidate in _iter_p0_health_findings(ddb_avx, stage):
                c_vin = (candidate.get("scope") or {}).get("vin", "")
                c_fleet_id = candidate.get("fleet_id", "")
                c_finding_id = candidate.get("finding_id", "")
                if not c_vin or not c_fleet_id or not c_finding_id:
                    skipped_picks.append(f"missing fields in {c_finding_id!r}")
                    continue
                # W8: skip expired Findings.
                exp = candidate.get("expires_at", "")
                if exp and exp <= now.isoformat():
                    skipped_picks.append(f"expired {c_finding_id!r} (expires_at={exp!r})")
                    continue
                # W8: skip unmanaged VINs (not in CMS registry).
                c_vehicle_id = _resolve_registry_vehicle_id(ddb_avx, stage, c_vin)
                if not c_vehicle_id:
                    skipped_picks.append(f"unmanaged vin {c_vin!r} in {c_finding_id!r}")
                    continue
                # W8: skip already-grounded vehicles.
                # Carried fix: fail closed — ResourceNotFoundException means the table
                # is missing (FleetHealthStack not deployed); the pick must FAIL.
                try:
                    if _has_open_grounding(ddb_avx, stage, c_vehicle_id):
                        skipped_picks.append(
                            f"already grounded vehicleId={c_vehicle_id!r} in {c_finding_id!r}"
                        )
                        continue
                except Exception as exc:  # noqa: BLE001
                    check(res, "ground-finding-pick", False,
                          on_fail=f"groundings table check raised for {c_finding_id!r}: "
                                  f"{type(exc).__name__}: {exc}")
                    return
                # W8: skip VINs with open FI bookings (C11: full is_fi_booking predicate).
                if _has_open_booking(ddb_avx, stage, c_vin):
                    skipped_picks.append(
                        f"vin {c_vin!r} has open FI booking in {c_finding_id!r}"
                    )
                    continue
                # W1: skip VINs outside the CMS recall cohort, with a blocking recall,
                # or whose cache is stale.  Source: CMS 81a90d385 recall_source.py
                # blocking_recalls (G6-9). Requires account from the manifest.
                if account:
                    try:
                        s3_pick = _client("s3", _CMS_RECALL_S3_REGION)
                        bucket_pick = _CMS_RECALL_BUCKET.format(
                            stage=stage, account=account)
                        mf_pick = _read_cms_recall_manifest(s3_pick, bucket_pick)
                        if mf_pick is None:
                            skipped_picks.append(
                                f"CMS recall cache unavailable for "
                                f"vin {c_vin!r} in {c_finding_id!r}"
                            )
                            continue
                        gen_pick = mf_pick.get("generation", "")
                        if not gen_pick:
                            skipped_picks.append(
                                f"CMS recall cache manifest has no generation for "
                                f"vin {c_vin!r} in {c_finding_id!r}"
                            )
                            continue
                        slice_pick = _read_cms_recall_fleet_slice(
                            s3_pick, bucket_pick, gen_pick, c_fleet_id)
                        blocks = _ground_pick_recall_check(c_vin, slice_pick)
                        if blocks is None:
                            skipped_picks.append(
                                f"CMS recall cache stale/absent or "
                                f"vin {c_vin!r} not in recall cohort "
                                f"in {c_finding_id!r}"
                            )
                            continue
                        if blocks:
                            skipped_picks.append(
                                f"vin {c_vin!r} has blocking recall(s) "
                                f"{blocks!r} in {c_finding_id!r}"
                            )
                            continue
                    except Exception as exc:  # noqa: BLE001
                        skipped_picks.append(
                            f"recall check raised for vin {c_vin!r} in "
                            f"{c_finding_id!r}: {type(exc).__name__}"
                        )
                        continue
                finding = candidate
                break

            if finding is None:
                skip_detail = (
                    f"; skipped {len(skipped_picks)}: {skipped_picks[:3]!r}"
                    if skipped_picks else ""
                )
                check(res, "ground-finding-pick", False,
                      on_fail="no eligible p0 fleet.health.service Finding with "
                              f"ground_vehicle action found{skip_detail}")
                return
            vin = (finding.get("scope") or {}).get("vin", "")
            fleet_id = finding.get("fleet_id", "")
            rec = finding.get("recommended_action") or {}
            fault_codes = (rec.get("parameters") or {}).get("fault_codes") or []
            finding_id = finding.get("finding_id", "")
            check(res, "ground-finding-pick", True,
                  f"target: vin={vin!r} fleet={fleet_id!r} finding={finding_id!r} "
                  f"fault_codes={fault_codes!r}")
        except Exception as exc:  # noqa: BLE001
            check(res, "ground-finding-pick", False,
                  on_fail=f"finding pick raised: {type(exc).__name__}: {exc}")
            return

    # ── 3. create temp fleet-operator user (or resume) ────────────────────────
    fleet_op_username = f"{_LIVETEST_USER_PREFIX}{run_id}-fo@example.invalid"
    fleet_op_token = ""
    vehicle_id = ""
    # C7 resume: if user sub-step already done, the user may or may not still exist.
    # Always create fresh so we get a valid token; we skip the manifest record
    # if user was already recorded (it may have been cleaned up on a prior run).
    user_already_recorded = manifest_records.get("user") == fleet_op_username
    try:
        idp = _client("cognito-idp", _AVX_REGION)
        pool_id = _resolve_pool_id(idp)
        client_id = _password_client(idp, pool_id)
        password = f"Lv{__import__('secrets').token_urlsafe(18)}!9Qz"
        attrs: list[dict[str, str]] = [
            {"Name": "email", "Value": fleet_op_username},
            {"Name": "email_verified", "Value": "true"},
            {"Name": "custom:fleetIds", "Value": fleet_id},
        ]
        if user_already_recorded:
            # User was recorded in a prior run's manifest.  Try to re-use or create fresh.
            try:
                # If the user still exists, update their password and auth.
                idp.admin_set_user_password(
                    UserPoolId=pool_id, Username=fleet_op_username,
                    Password=password, Permanent=True,
                )
                auth = idp.initiate_auth(
                    ClientId=client_id,
                    AuthFlow="USER_PASSWORD_AUTH",
                    AuthParameters={"USERNAME": fleet_op_username, "PASSWORD": password},
                )
                fleet_op_token = auth["AuthenticationResult"]["IdToken"]
                check(res, "ground-user-create", True,
                      f"resume: fleet-operator user {fleet_op_username!r} re-authenticated")
            except Exception:  # noqa: BLE001
                # User was deleted (e.g., by cleanup in a prior completed run).
                # Create fresh for this resume invocation.
                with _defer_signals_for_restore():
                    _record_sub_step("user", fleet_op_username)
                    idp.admin_create_user(
                        UserPoolId=pool_id,
                        Username=fleet_op_username,
                        MessageAction="SUPPRESS",
                        UserAttributes=attrs,
                    )
                    idp.admin_set_user_password(
                        UserPoolId=pool_id, Username=fleet_op_username,
                        Password=password, Permanent=True,
                    )
                    idp.admin_add_user_to_group(
                        UserPoolId=pool_id, Username=fleet_op_username,
                        GroupName="fleet-operator",
                    )
                auth = idp.initiate_auth(
                    ClientId=client_id,
                    AuthFlow="USER_PASSWORD_AUTH",
                    AuthParameters={"USERNAME": fleet_op_username, "PASSWORD": password},
                )
                fleet_op_token = auth["AuthenticationResult"]["IdToken"]
                check(res, "ground-user-create", True,
                      f"resume: fleet-operator user {fleet_op_username!r} re-created "
                      f"(was deleted in prior run's cleanup)")
        else:
            # C7: record username + finding info BEFORE AdminCreateUser so the user
            # can be deleted on cleanup even if subsequent calls fail (W6).
            with _defer_signals_for_restore():
                _record_sub_step("user", fleet_op_username)
                _record_sub_step("ground_vin", vin)
                _record_sub_step("ground_fleet_id", fleet_id)
                _record_sub_step("ground_finding_id", finding_id)
                _record_sub_step("ground_fault_codes", ",".join(fault_codes))
                idp.admin_create_user(
                    UserPoolId=pool_id,
                    Username=fleet_op_username,  # C3 fix: email-format Username
                    MessageAction="SUPPRESS",
                    UserAttributes=attrs,
                )
                idp.admin_set_user_password(
                    UserPoolId=pool_id, Username=fleet_op_username,
                    Password=password, Permanent=True,
                )
                idp.admin_add_user_to_group(
                    UserPoolId=pool_id, Username=fleet_op_username,
                    GroupName="fleet-operator",
                )
            auth = idp.initiate_auth(
                ClientId=client_id,
                AuthFlow="USER_PASSWORD_AUTH",
                AuthParameters={"USERNAME": fleet_op_username, "PASSWORD": password},
            )
            fleet_op_token = auth["AuthenticationResult"]["IdToken"]
            check(res, "ground-user-create", True,
                  f"fleet-operator user {fleet_op_username!r} created; fleet={fleet_id!r}")
    except Exception as exc:  # noqa: BLE001
        check(res, "ground-user-create", False,
              on_fail=f"fleet-operator user creation raised: {type(exc).__name__}: {exc}")
        return

    # ── get API URLs ──────────────────────────────────────────────────────────
    avx_api_url = ""
    cms_api_url = ""
    try:
        cfn = _client("cloudformation", _AVX_REGION)
        avx_api_url = _get_avx_api_url(cfn, stage)
        cms_api_url = _get_cms_api_url(cfn, stage)
        check(res, "ground-api-urls", True,
              f"AVX={avx_api_url!r} CMS={cms_api_url!r}")
    except Exception as exc:  # noqa: BLE001
        check(res, "ground-api-urls", False,
              on_fail=f"API URL lookup raised: {type(exc).__name__}: {exc}")
        _delete_temp_user(_client("cognito-idp", _AVX_REGION),
                          _resolve_pool_id(_client("cognito-idp", _AVX_REGION)),
                          fleet_op_username)
        return

    # ── 4. approve ground_vehicle (or resume) ─────────────────────────────────
    # C7 resume: if "grounded" sub-step is already "true", skip the approve.
    action_id_ground = str(manifest_records.get("ground_action_id", ""))
    if manifest_records.get("grounded") == "true":
        check(res, "ground-vehicle-approve", True,
              f"resume: already grounded (action_id={action_id_ground!r})")
    else:
        if action_id_ground and action_id_ground != "pending":
            # guard: ground_action_id holds a real action id but grounded sub-step
            # was not confirmed — stale/interrupted state; refuse re-approve because
            # we already have a concrete action id (grounding may be live in CMS).
            # If ground_action_id == "pending" the outcome is unknown; re-approve
            # is safe because FI items use _fi_decision_id(finding_id) which returns
            # the same deterministic id for the same Finding, so a second POST on the
            # same Finding returns the existing Action rather than creating a duplicate.
            # (S8 fix: the "pending" branch falls through to re-approve, not to refuse.)
            check(res, "ground-vehicle-approve", False,
                  on_fail=(
                      f"run {run_id!r} has ground_action_id={action_id_ground!r} "
                      f"in manifest but grounded sub-step not confirmed; "
                      f"inspect the vehicle's grounding status manually before resuming "
                      f"(if the grounding is open in CMS, do not re-approve)"
                  ))
            _delete_temp_user(_client("cognito-idp", _AVX_REGION),
                              _resolve_pool_id(_client("cognito-idp", _AVX_REGION)),
                              fleet_op_username)
            return
        # W5: re-check window before write.
        if not _assert_window_ok("ground-vehicle-approve-window"):
            _delete_temp_user(_client("cognito-idp", _AVX_REGION),
                              _resolve_pool_id(_client("cognito-idp", _AVX_REGION)),
                              fleet_op_username)
            return
        try:
            with _defer_signals_for_restore():
                # C7: record sub-step BEFORE the write (sentinel "pending").
                _record_sub_step("ground_action_id", "pending")
                status, body = _http(
                    avx_api_url, "POST",
                    f"findings/{finding_id}/approve",
                    fleet_op_token,
                    {"action_kind": _GROUND_VEHICLE_KIND,
                     "target_system": "cms", "parameters": {}},
                )
                action_id_ground = (body.get("action_id") or "")
                ok = (200 <= status < 300
                      and isinstance(body, dict)
                      and body.get("status") == "executed"
                      and body.get("action_kind") == _GROUND_VEHICLE_KIND)
                if ok:
                    _record_sub_step("ground_action_id", action_id_ground)
                    _record_sub_step("grounded", "true")
            check(
                res, "ground-vehicle-approve",
                ok,
                f"ground_vehicle approved: action_id={action_id_ground!r}",
                on_fail=f"ground_vehicle approve returned {status!r}: {body!r}",
            )
        except Exception as exc:  # noqa: BLE001
            check(res, "ground-vehicle-approve", False,
                  on_fail=f"ground_vehicle approve raised: {type(exc).__name__}: {exc}")

        # If grounding failed, delete user and return.
        rows_so_far = {r[0]: r[1] for r in res.rows}
        if rows_so_far.get("ground-vehicle-approve") != "PASS":
            _delete_temp_user(_client("cognito-idp", _AVX_REGION),
                              _resolve_pool_id(_client("cognito-idp", _AVX_REGION)),
                              fleet_op_username)
            return

    # W2 fix: track whether step 9's code was reached.  Set to True inside
    # every step-9 branch (including operational SKIPs like fault_not_repaired
    # and awaiting-repair-ref) so that only early exits — registry miss, window
    # closes, stand-in SKIPs, exceptions — trigger the residue FAIL below.
    _step9_ran = [False]

    def _w2_residue_emit() -> None:
        """W2 fix: if the car is grounded but not returned, emit a FAIL row naming it.

        Called before every early-return from a resumable wait (window close, step 7
        stand-in wait, step 8 awaiting repair, registry miss).  Emits the FAIL row so
        the operator can see the car is grounded, but records only a NON-BLOCKING
        'last_exit' sub-step — never 'return_blocked'.

        'return_blocked' is reserved for CMS RTS refusals that won't clear by waiting
        (step-9 non-fault_not_repaired refusal).  Using it on resumable exits would
        cause step 9's pending-resume guard to treat the car as permanently blocked and
        never retry the return.
        """
        try:
            _mf_w2 = _read_manifest(manifest_path)
            _rec_w2 = _mf_w2.get("records", {}) or {}
        except Exception:  # noqa: BLE001
            _rec_w2 = dict(manifest_records)
        if _rec_w2.get("grounded") != "true":
            return  # not yet grounded; nothing to report
        _vin_r = _rec_w2.get("ground_vin", vin)
        _flt_r = _rec_w2.get("ground_fleet_id", "?")
        # Record a non-blocking last_exit (not return_blocked) so a subsequent run
        # can still reach step 9 and retry the return-to-service.
        try:
            _record_sub_step("last_exit", "exited-before-rts")
        except Exception:  # noqa: BLE001
            pass
        check(
            res, "ground-operator-residue", False,
            on_fail=(
                f"grounding is operator residue: VIN={_vin_r!r} "
                f"fleet={_flt_r!r} vehicleId={vehicle_id!r}; "
                "resumable — re-run --part ground "
                f"--run-id {run_id!r} after 05:40Z to retry return-to-service"
            ),
        )

    # W8 fix: resolve registry vehicleId from CMS vehicles table.
    # Staging has 101 managed vehicles whose vehicleId differs from the VIN;
    # the RTS path must carry the registry vehicleId, not the bare VIN.
    # Source: CMS main 81a90d385 services/fleet_intelligence/groundings.py:35-64.
    # S10 fix: FAIL on an error or a miss — silently falling back to VIN would
    # let RTS proceed with a bare VIN and pass a check that it shouldn't.
    try:
        ddb_cms = _client("dynamodb", _AVX_REGION)
        vehicle_id = _resolve_registry_vehicle_id(ddb_cms, stage, vin)
    except Exception as exc:  # noqa: BLE001
        check(res, "ground-registry-vehicle-id", False,
              on_fail=f"registry vehicleId lookup raised: {type(exc).__name__}: {exc}")
        _w2_residue_emit()  # W2 fix: name grounding as operator residue on early exit
        return
    if vehicle_id is None:
        check(res, "ground-registry-vehicle-id", False,
              on_fail=f"VIN {vin!r} not found in CMS registry; "
                      "cannot determine vehicleId for RTS (S10 fix: no VIN fallback)")
        _w2_residue_emit()  # W2 fix: name grounding as operator residue on early exit
        return
    check(res, "ground-registry-vehicle-id", True,
          f"registry vehicleId={vehicle_id!r} for VIN={vin!r}")

    # ── 5. negative control: RTS refused before repair ────────────────────────
    # C1 fix: record neg_ctrl: "sent" inside the hold BEFORE the POST, so any
    # resume skips the re-send once something is recorded.  After the POST, record
    # neg_ctrl_refused: "true" on the expected refusal, or update neg_ctrl to
    # "{status}:{reason}" on any other answer.  On resume, report the recorded
    # outcome without re-sending.
    neg_ctrl_sent = manifest_records.get("neg_ctrl")  # None = never attempted
    if neg_ctrl_sent is not None or manifest_records.get("neg_ctrl_refused") is not None:
        # Already attempted in a prior run.  Report the recorded outcome.
        if manifest_records.get("neg_ctrl_refused") == "true":
            check(res, "ground-rts-before-repair", True,
                  "resume: negative control already confirmed in prior run")
        else:
            check(res, "ground-rts-before-repair", False,
                  on_fail=f"resume: negative control failed in prior run "
                          f"(neg_ctrl={neg_ctrl_sent!r}); continuing to steps 6-9 "
                          "to ensure vehicle is returned to service")
    else:
        try:
            rts_path = _CMS_RTS_PATH.format(vehicleId=vehicle_id)
            with _defer_signals_for_restore():
                # C1 fix: record "sent" inside the hold before the POST.
                # The hold defers SIGTERM so a signal between this record and
                # the POST outcome is not lost.  On resume, neg_ctrl is present
                # → skip the re-send regardless of outcome.
                _record_sub_step("neg_ctrl", "sent")
                status, body = _http(cms_api_url, "POST", rts_path, fleet_op_token, {})
                reason = body.get("reason", "")
                # S10 fix: also compare fault_codes in the refusal body with the Finding's.
                # CMS returns the unrepaired fault codes in the body; they must match.
                body_fault_codes = sorted(body.get("fault_codes") or [])
                expected_fault_codes = sorted(fault_codes)
                codes_match = body_fault_codes == expected_fault_codes
                ok = status == 409 and reason == "fault_not_repaired" and codes_match
                if ok:
                    # C1 fix: record inside the hold so the resume path can skip this POST.
                    _record_sub_step("neg_ctrl_refused", "true")
                else:
                    # Record the failure outcome so resume skips re-sending.
                    _record_sub_step("neg_ctrl", f"{status}:{reason}")
            check(
                res, "ground-rts-before-repair",
                ok,
                f"RTS correctly refused before repair (status=409 reason={reason!r} "
                f"fault_codes match expected={expected_fault_codes!r})",
                on_fail=f"expected 409 fault_not_repaired with fault_codes={expected_fault_codes!r}; "
                        f"got status={status!r} reason={reason!r} "
                        f"fault_codes={body_fault_codes!r} body={body!r}",
            )
        except Exception as exc:  # noqa: BLE001
            check(res, "ground-rts-before-repair", False,
                  on_fail=f"RTS negative control raised: {type(exc).__name__}: {exc}")

    # ── 6. approve book_fleet_service (or resume) ─────────────────────────────
    action_id_book = str(manifest_records.get("book_action_id", ""))
    booking_finding_id = str(manifest_records.get("booking_finding_id", ""))
    if manifest_records.get("booked") == "true":
        check(res, "ground-book-fleet-service", True,
              f"resume: already booked (action_id={action_id_book!r})")
    else:
        # W5: re-check window before write.
        if not _assert_window_ok("ground-book-fleet-service-window"):
            _delete_temp_user(_client("cognito-idp", _AVX_REGION),
                              _resolve_pool_id(_client("cognito-idp", _AVX_REGION)),
                              fleet_op_username)
            _w2_residue_emit()  # W2 fix
            return
        try:
            ddb_avx2 = _client("dynamodb", _AVX_REGION)
            # Resume: re-approve the booking Finding this run recorded, never a newer
            # service/N a later Tier 2 run wrote (a second Finding is a second RO).
            booking_finding = (
                {"finding_id": booking_finding_id} if booking_finding_id
                else _find_booking_finding_for_vin(ddb_avx2, stage, vin)
            )
            if booking_finding is None:
                check(res, "ground-book-fleet-service", False,
                      on_fail=f"no booking Finding (book_fleet_service) found for vin={vin!r}; "
                              f"Tier 2 must write both service/N and ground/N items")
            else:
                booking_finding_id = booking_finding.get("finding_id", "")
                with _defer_signals_for_restore():
                    _record_sub_step("booking_finding_id", booking_finding_id)
                    _record_sub_step("book_action_id", "pending")
                    status, body = _http(
                        avx_api_url, "POST",
                        f"findings/{booking_finding_id}/approve",
                        fleet_op_token,
                        {"action_kind": _BOOK_FLEET_SERVICE_KIND,
                         "target_system": "dms", "parameters": {}},
                    )
                    action_id_book = (body.get("action_id") or "")
                    # The DMS executor maps DMS's 2xx to `in_progress` (the RO is open;
                    # agents/executors/dms.py "Response mapping: 2xx → in_progress"),
                    # with the RO id in `target_ref`. Requiring `executed` here FAILed
                    # every real booking (2026-10-26, prompt 124's first ground run).
                    ok = (200 <= status < 300
                          and isinstance(body, dict)
                          and body.get("status") in _BOOK_OK_STATUSES
                          and body.get("action_kind") == _BOOK_FLEET_SERVICE_KIND)
                    if ok:
                        _record_sub_step("book_action_id", action_id_book)
                        if isinstance(body.get("target_ref"), str) and body["target_ref"]:
                            _record_sub_step("book_ro_id", body["target_ref"])
                        _record_sub_step("booked", "true")
                check(
                    res, "ground-book-fleet-service",
                    ok,
                    f"book_fleet_service approved: action_id={action_id_book!r}",
                    on_fail=f"book_fleet_service approve returned {status!r}: {body!r}",
                )
        except Exception as exc:  # noqa: BLE001
            check(res, "ground-book-fleet-service", False,
                  on_fail=f"book_fleet_service approve raised: {type(exc).__name__}: {exc}")

    # ── 7. stand-in (or resume) ───────────────────────────────────────────────
    # Brief §ground: invoke dms-{stage}-fi-dealer-progress only when every OPEN
    # FI booking is this run's.  Otherwise SKIP for the 05:40Z scheduled run.
    # C7 fix: SKIP (not PASS) when deferred; use action_id_book for "ours" detection.
    # C7: record advanced_N sub-step BEFORE Invoke call.
    # W5: re-check window before Invoke.
    stand_in_invoked = False
    if manifest_records.get("repaired") == "true" or manifest_records.get("returned") == "true":
        check(res, "ground-stand-in", True,
              "resume: repair already confirmed in prior run; stand-in step skipped")
        stand_in_invoked = True
    else:
        # W5: re-check window before Invoke.
        if not _assert_window_ok("ground-stand-in-window"):
            _delete_temp_user(_client("cognito-idp", _AVX_REGION),
                              _resolve_pool_id(_client("cognito-idp", _AVX_REGION)),
                              fleet_op_username)
            _w2_residue_emit()  # W2 fix
            return
        try:
            ddb_dms = _client("dynamodb", _AVX_REGION)
            total_open, ours = _count_open_fi_bookings(ddb_dms, stage, action_id_book)
            if total_open == 0:
                check(res, "ground-stand-in", False,
                      on_fail="no open FI bookings found after book_fleet_service; "
                              "DMS executor may not have completed yet")
            elif total_open != ours:
                res.skip("ground-stand-in",
                         f"{total_open} open FI bookings; only {ours} ours (by action_id "
                         f"{action_id_book!r}); waiting for 05:40Z scheduled stand-in "
                         f"(re-run --part ground --run-id {run_id!r} after 05:40Z)")
            else:
                # S7 fix: check early-close hash before invoking.
                our_ro_id = _find_ro_id_for_action(ddb_dms, stage, action_id_book)
                recorded_ro = str(manifest_records.get("book_ro_id", "") or "")
                if not our_ro_id:
                    # Fail closed: without the RO id the early-close check can't run,
                    # and an early close would end the repair permanently.
                    check(res, "ground-stand-in", False,
                          on_fail=f"no DMS RO found for action {action_id_book!r}; "
                                  "early-close check can't run, stand-in not invoked")
                elif recorded_ro and recorded_ro != our_ro_id:
                    check(res, "ground-stand-in", False,
                          on_fail=f"DMS RO {our_ro_id!r} for action {action_id_book!r} is not "
                                  f"the RO the approve returned ({recorded_ro!r}); "
                                  "stand-in not invoked")
                elif _would_early_close(our_ro_id):
                    res.skip(
                        "ground-stand-in",
                        f"RO {our_ro_id!r} falls in the stand-in's ~5% early-close set "
                        f"(sha256({our_ro_id!r}) % 100 < 5); invoking would close it "
                        f"permanently — SKIP. Re-run with a different vehicle.",
                    )
                else:
                    # Track invocation count (C7: advanced_N sub-steps).
                    inv_n = (
                        sum(1 for k in manifest_records if k.startswith("advanced_")) + 1
                    )
                    with _defer_signals_for_restore():
                        _record_sub_step(f"advanced_{inv_n}", "pending")
                        lam = _client("lambda", _AVX_REGION)
                        fn_name = _DMS_DEALER_PROGRESS_FN.format(stage=stage)
                        resp = lam.invoke(
                            FunctionName=fn_name,
                            InvocationType="RequestResponse",
                            Payload=b"{}",
                        )
                        status_code = resp.get("StatusCode", 0)
                        ok = 200 <= status_code < 300 and not resp.get("FunctionError")
                        if ok:
                            _record_sub_step(f"advanced_{inv_n}", "true")
                    stand_in_invoked = ok
                    check(
                        res, "ground-stand-in",
                        ok,
                        f"stand-in invoked (pass {inv_n}): {fn_name!r} "
                        f"status={status_code}",
                        on_fail=f"stand-in invocation failed: status={status_code} "
                                f"error={resp.get('FunctionError')!r}",
                    )
        except Exception as exc:  # noqa: BLE001
            check(res, "ground-stand-in", False,
                  on_fail=f"stand-in raised: {type(exc).__name__}: {exc}")

    # ── 8. ADP fault view: repair_ref ─────────────────────────────────────────
    # S4 fix: require repair_ref for EVERY code — CMS refuses RTS unless all codes
    #   are repaired (health_source.fault_repair_blockers checks every code).
    # C7 fix: SKIP (not PASS) when repair_ref is not yet available.
    # no-fault-codes branch: FAIL (C7) — a p0 Finding without fault_codes cannot
    #   establish the repair property; fail closed, not NOTE-and-pass.
    # Lag 1: DMS lake merge 30 min (vehicle_fault_status is a view, no daily job).
    # Lag 2: CMS health-cache hourly refresh (lifecycle_cache.py cron, ui_stack.py:4306).
    # Source: g93lv-facts.md §DMS, §CMS; CMS 81a90d385 health_source.py:527-574.
    repair_ref_found = False
    if manifest_records.get("repaired") == "true" or manifest_records.get("returned") == "true":
        check(res, "ground-repair-ref", True,
              "resume: repair already confirmed in prior run")
        repair_ref_found = True
    else:
        try:
            if fault_codes:
                athena = _client("athena", _ADP_REGION)
                rows = _query_fault_status(athena, stage, vin, fault_codes)
                by_code = {r.get("code"): r for r in rows}
                codes_missing_ref = [
                    c for c in fault_codes if not (by_code.get(c) or {}).get("repair_ref")
                ]
                codes_with_ref = [
                    c for c in fault_codes if (by_code.get(c) or {}).get("repair_ref")
                ]
                if not codes_missing_ref:
                    # S4: all codes have repair_ref.
                    repair_ref_found = True
                    _record_sub_step("repaired", "true")
                    check(
                        res, "ground-repair-ref",
                        True,
                        f"ADP fault view: repair_ref found for all {len(fault_codes)} "
                        f"code(s); first repair_ref="
                        f"{(by_code.get(fault_codes[0]) or {}).get('repair_ref')!r}",
                    )
                elif codes_with_ref:
                    # S4: some but not all codes repaired — FAIL (CMS will refuse RTS).
                    check(
                        res, "ground-repair-ref",
                        False,
                        on_fail=(
                            f"ADP fault view: repair_ref missing for "
                            f"{len(codes_missing_ref)}/{len(fault_codes)} code(s): "
                            f"{codes_missing_ref!r}; CMS requires ALL codes repaired "
                            f"for RTS (health_source.fault_repair_blockers). "
                            f"Repaired: {codes_with_ref!r}"
                        ),
                    )
                else:
                    # C7 fix: SKIP — lag expected.
                    res.skip(
                        "ground-repair-ref",
                        f"ADP fault view: repair_ref not yet present for any code "
                        f"(lag: DMS lake merge {_DMS_LAKE_LAG_MINUTES} min + "
                        f"stand-in passes to reach Delivered/Ready status); "
                        f"vin={vin!r} stand_in_invoked={stand_in_invoked} "
                        f"ro_rows={len(rows)}; "
                        f"re-run --part ground --run-id {run_id!r} after 05:40Z",
                    )
            else:
                # C7/no-fault-codes fix: FAIL — cannot establish repair property.
                check(
                    res, "ground-repair-ref",
                    False,
                    on_fail=(
                        "no fault_codes in Finding; cannot check ADP fault view — "
                        "this run cannot establish the repair property. "
                        "Pick a Finding whose recommended_action.parameters.fault_codes "
                        "is non-empty."
                    ),
                )
        except Exception as exc:  # noqa: BLE001
            check(res, "ground-repair-ref", False,
                  on_fail=f"ADP fault view query raised: {type(exc).__name__}: {exc}")

    # ── 9. return-to-service accepted (or resume) ─────────────────────────────
    # W1 fix: if a prior run recorded return_blocked, the grounding is operator
    # residue.  FAIL immediately without retrying; the operator must resolve the
    # blocking condition (recall / VIN-not-in-cohort) and return the vehicle manually.
    if manifest_records.get("return_blocked"):
        _step9_ran[0] = True  # W2 fix: step 9 reached
        check(res, "ground-rts-after-repair", False,
              on_fail=f"grounding is operator residue: CMS refused return-to-service "
                      f"with reason {manifest_records['return_blocked']!r}; "
                      "operator must resolve the blocking condition and return the "
                      "vehicle manually before re-running --part ground")
    elif manifest_records.get("returned") == "true":
        _step9_ran[0] = True  # W2 fix: step 9 reached
        check(res, "ground-rts-after-repair", True,
              "resume: RTS already accepted in prior run")
    elif manifest_records.get("returned") == "pending":
        # A prior run recorded returned:pending (it started the POST but didn't
        # finish recording the outcome).  Read the grounding row to decide:
        #   - grounding absent → RTS was accepted on the prior run; record it.
        #   - grounding present → retry the POST (repair_ref was found earlier).
        # C7 fix: don't FAIL on not_grounded — read the grounding row first.
        try:
            ddb_rts_chk = _client("dynamodb", _AVX_REGION)
            if not _has_open_grounding(ddb_rts_chk, stage, vehicle_id):
                # Grounding absent — vehicle was returned on the prior run.
                _step9_ran[0] = True  # W2 fix: step 9 reached
                _record_sub_step("returned", "true")
                check(res, "ground-rts-after-repair", True,
                      f"resumed from pending: grounding row absent — vehicle already "
                      f"returned to service; vehicleId={vehicle_id!r}")
            else:
                # Grounding still present; retry the RTS POST.
                # W5: re-check window before write.
                if not _assert_window_ok("ground-rts-after-repair-window"):
                    _delete_temp_user(_client("cognito-idp", _AVX_REGION),
                                      _resolve_pool_id(_client("cognito-idp", _AVX_REGION)),
                                      fleet_op_username)
                    _w2_residue_emit()  # W2 fix
                    return
                _step9_ran[0] = True  # W2 fix: step 9 reached (window check passed)
                try:
                    rts_path = _CMS_RTS_PATH.format(vehicleId=vehicle_id)
                    with _defer_signals_for_restore():
                        status, body = _http(cms_api_url, "POST", rts_path, fleet_op_token, {})
                        ok = 200 <= status < 300
                        if ok:
                            _record_sub_step("returned", "true")
                    reason = body.get("reason", "")
                    if reason == "fault_not_repaired":
                        # C7 fix: cache lag — SKIP, not FAIL.
                        res.skip(
                            "ground-rts-after-repair",
                            f"resumed from pending: cache lag — fault still unrepaired "
                            f"per CMS health cache ({_CMS_HEALTH_CACHE_LAG_HOURS}h refresh); "
                            f"re-run --part ground --run-id {run_id!r} after 05:40Z",
                        )
                    else:
                        if not ok and reason:
                            # W1 fix: non-fault_not_repaired refusal; grounding is operator
                            # residue.  Record return_blocked so resume skips the retry.
                            _record_sub_step("return_blocked", reason)
                        check(
                            res, "ground-rts-after-repair",
                            ok,
                            f"return-to-service accepted (status={status}); "
                            f"vehicle {vehicle_id!r} returned to service",
                            on_fail=(
                                f"RTS refused after repair (resumed from pending): "
                                f"status={status!r} reason={reason!r} body={body!r}"
                                + ("; grounding is operator residue — operator must "
                                   "resolve the blocking condition before retrying"
                                   if reason else "")
                            ),
                        )
                except Exception as exc:  # noqa: BLE001
                    check(res, "ground-rts-after-repair", False,
                          on_fail=f"RTS retry (pending-resume) raised: "
                                  f"{type(exc).__name__}: {exc}")
        except Exception as exc:  # noqa: BLE001
            _step9_ran[0] = True  # W2 fix: step 9 attempted (outer exception)
            check(res, "ground-rts-after-repair", False,
                  on_fail=f"grounding row check (pending-resume) raised: "
                          f"{type(exc).__name__}: {exc}")
    elif repair_ref_found:
        # W5: re-check window before RTS POST.
        if not _assert_window_ok("ground-rts-after-repair-window"):
            _delete_temp_user(_client("cognito-idp", _AVX_REGION),
                              _resolve_pool_id(_client("cognito-idp", _AVX_REGION)),
                              fleet_op_username)
            _w2_residue_emit()  # W2 fix
            return
        _step9_ran[0] = True  # W2 fix: step 9 reached (window check passed)
        try:
            rts_path = _CMS_RTS_PATH.format(vehicleId=vehicle_id)
            with _defer_signals_for_restore():
                _record_sub_step("returned", "pending")
                status, body = _http(cms_api_url, "POST", rts_path, fleet_op_token, {})
                ok = 200 <= status < 300
                if ok:
                    _record_sub_step("returned", "true")
            reason = body.get("reason", "")
            if reason == "fault_not_repaired":
                # C7 fix: cache lag — SKIP, not FAIL.  The health cache may take
                # up to _CMS_HEALTH_CACHE_LAG_HOURS to reflect the repair; re-run
                # will pick up the pending state and retry.
                res.skip(
                    "ground-rts-after-repair",
                    f"cache lag: fault still unrepaired per CMS health cache "
                    f"({_CMS_HEALTH_CACHE_LAG_HOURS}h refresh); "
                    f"re-run --part ground --run-id {run_id!r} after 05:40Z",
                )
            else:
                if not ok and reason:
                    # W2 fix: mirror pending-resume — record return_blocked so the
                    # next run enters the pending-resume branch and the operator sees
                    # the grounding as residue.
                    _record_sub_step("return_blocked", reason)
                check(
                    res, "ground-rts-after-repair",
                    ok,
                    f"return-to-service accepted (status={status}); "
                    f"vehicle {vehicle_id!r} returned to service",
                    on_fail=(
                        f"RTS refused after repair: status={status!r} "
                        f"reason={reason!r} body={body!r}"
                        + (f"; grounding is operator residue: "
                           f"VIN={vin!r} fleet={manifest_records.get('ground_fleet_id', '?')!r} "
                           f"vehicleId={vehicle_id!r}; "
                           f"operator must re-run --part ground --run-id {run_id!r} after 05:40Z "
                           "or resolve the blocking condition manually"
                           if (not ok and reason) else "")
                    ),
                )
        except Exception as exc:  # noqa: BLE001
            check(res, "ground-rts-after-repair", False,
                  on_fail=f"RTS after repair raised: {type(exc).__name__}: {exc}")
    else:
        # C7 fix: SKIP — awaiting repair_ref + CMS health-cache refresh.
        _step9_ran[0] = True  # W2 fix: step 9 reached (operational SKIP, not an error)
        res.skip(
            "ground-rts-after-repair",
            f"awaiting repair_ref in ADP fault view "
            f"(DMS lake merge {_DMS_LAKE_LAG_MINUTES} min + "
            f"CMS health-cache {_CMS_HEALTH_CACHE_LAG_HOURS} h refresh); "
            f"re-run --part ground --run-id {run_id!r} after 05:40Z",
        )

    # W2 fix: if step 9 never ran (early exit from steps 5-8 or window close before
    # step-9 POST), emit a FAIL row naming the grounding as operator residue.
    # Uses last_exit (not return_blocked) so a subsequent run can still retry step 9.
    if not _step9_ran[0]:
        try:
            _mf_fin = _read_manifest(manifest_path)
            _rec_fin = _mf_fin.get("records", {}) or {}
        except Exception:  # noqa: BLE001
            _rec_fin = dict(manifest_records)
        if _rec_fin.get("grounded") == "true":
            _vin_res = _rec_fin.get("ground_vin", vin)
            _flt_res = _rec_fin.get("ground_fleet_id", "?")
            try:
                _record_sub_step("last_exit", "exited-before-rts")
            except Exception:  # noqa: BLE001
                pass
            check(
                res, "ground-operator-residue", False,
                on_fail=(
                    f"grounding is operator residue: VIN={_vin_res!r} "
                    f"fleet={_flt_res!r} vehicleId={vehicle_id!r}; "
                    "resumable — re-run --part ground "
                    f"--run-id {run_id!r} after 05:40Z to retry return-to-service"
                ),
            )

    # ── 10. residue check: planned set ────────────────────────────────────────
    # C7 docstring promise: residue = manifest's planned set.
    # The grounding and RO are intentional residue; temp user is cleaned up below.
    # C1 fix: build attempted from recorded outcomes (manifest_records), not row status.
    # neg_ctrl is recorded before the POST; neg_ctrl_refused only on the exact refusal.
    try:
        rows_so_far = {r[0]: r[1] for r in res.rows}
        attempted: set[str] = set()
        if rows_so_far.get("ground-user-create") == "PASS":
            attempted.add("user")
        if rows_so_far.get("ground-vehicle-approve") == "PASS":
            attempted.update({"ground_action_id", "grounded", "ground_vin",
                               "ground_fleet_id", "ground_finding_id",
                               "ground_fault_codes"})
        if rows_so_far.get("ground-book-fleet-service") == "PASS":
            attempted.update({"book_action_id", "booked", "booking_finding_id"})
        # C1 fix: use recorded outcomes, not row status, for the neg_ctrl keys.
        if manifest_records.get("neg_ctrl"):
            attempted.add("neg_ctrl")
        if manifest_records.get("neg_ctrl_refused") == "true":
            attempted.add("neg_ctrl_refused")
        if rows_so_far.get("ground-repair-ref") == "PASS":
            attempted.add("repaired")
        if rows_so_far.get("ground-rts-after-repair") == "PASS":
            attempted.add("returned")
        missing = attempted - {k for k, v in manifest_records.items()
                               if v and v != "pending"}
        ok = len(missing) == 0
        check(
            res, "ground-residue-planned-set",
            ok,
            # S3 fix: name the grounded car and its state in the residue message.
            f"residue set matches planned set for completed sub-steps; "
            f"VIN={manifest_records.get('ground_vin', vin)!r} "
            f"vehicleId={vehicle_id!r} "
            f"fleet={manifest_records.get('ground_fleet_id', '?')!r}; "
            f"returned={manifest_records.get('returned', 'no')!r} "
            f"(grounding + RO are intentional residue in manifest)",
            on_fail=(
                f"residue check: {len(missing)} planned key(s) not in manifest: "
                f"{sorted(missing)!r}; manifest_records={sorted(manifest_records)!r}"
            ),
        )
    except Exception as exc:  # noqa: BLE001
        check(res, "ground-residue-planned-set", False,
              on_fail=f"residue check raised: {type(exc).__name__}: {exc}")

    # ── cleanup temp user ─────────────────────────────────────────────────────
    # The grounding and RO are intentional residue (planned set); the user is cleanup.
    try:
        idp = _client("cognito-idp", _AVX_REGION)
        pool_id = _resolve_pool_id(idp)
        with _defer_signals_for_restore():
            _delete_temp_user(idp, pool_id, fleet_op_username)
        # Confirm absent: email filter (C3 fix: pool uses email aliases)
        remaining = idp.list_users(
            UserPoolId=pool_id,
            Filter=f'email = "{fleet_op_username}"',
        ).get("Users", [])
        ok = len(remaining) == 0
        check(
            res, "ground-user-cleanup",
            ok,
            f"fleet-operator user {fleet_op_username!r} deleted",
            on_fail=f"user {fleet_op_username!r} still present after delete",
        )
    except Exception as exc:  # noqa: BLE001
        check(res, "ground-user-cleanup", False,
              on_fail=f"user cleanup raised: {type(exc).__name__}: {exc}")

# ── part dispatch ─────────────────────────────────────────────────────────────

_PARTS: dict[str, Any] = {
    "pre": _part_pre,
    "issue": _part_issue,
    "after-run": _part_after_run,
    "cleanup": _part_cleanup,
    "after-cleanup": _part_after_cleanup,
    "ground": _part_ground,
}


def _run_part(
    part: str,
    res: Results,
    *,
    stage: str,
    run_id: str,
    allow_writes: bool = False,
    allow_managed_writes: bool = False,
) -> None:
    """Run the given part in its own try/except."""
    fn = _PARTS.get(part)
    if fn is None:
        check(res, f"part-{part}", False,
              on_fail=f"unknown part {part!r}; choices: {sorted(_PARTS)}")
        return
    try:
        fn(res, stage=stage, run_id=run_id, allow_writes=allow_writes,
           allow_managed_writes=allow_managed_writes)
    except Exception as exc:  # noqa: BLE001
        check(res, f"part-{part}-exception", False,
              on_fail=f"{type(exc).__name__}: {exc}")


# ── CLI / main ────────────────────────────────────────────────────────────────

def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Live verification for FI Fleet-Health Stage D task 9.3"
    )
    p.add_argument("--stage", required=True,
                   help="AWS stage; only 'staging' is accepted")
    p.add_argument("--part", required=True, choices=sorted(_PARTS),
                   help="which part to run")
    p.add_argument("--allow-writes", action="store_true",
                   help="gates write parts (issue, cleanup, ground)")
    p.add_argument("--allow-managed-writes", action="store_true",
                   help="gates managed-fleet writes in ground part "
                        "(grounding, DMS RO); requires --allow-writes")
    p.add_argument("--run-id", default=None,
                   help=(
                       "run id minted by --part pre; required for all non-pre parts "
                       "(brief §K1). Format: fi-loop-YYYYMMDDTHHMMSS-8hex"
                   ))
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Entry point.

    Exit 0: all pass.  Exit 1: any FAIL.  Exit 2: any SKIP.
    Brief §Exit.
    """
    # Guard: refuse any stage but staging before any AWS call (brief §Rules)
    args = _parse_args(argv)
    if args.stage != _ALLOWED_STAGE:
        print(
            f"refused: --stage must be {_ALLOWED_STAGE!r} (got {args.stage!r})",
            file=sys.stderr,
        )
        return 1

    # Writes outside the conftest AWS block run as the caller with real credentials.
    # Refuse before any client unless the live-run step gave its go.
    _refusal = _lwg.check_write_flags(
        stage=args.stage, go_env=_LIVE_GO_ENV,
        flags={
            "--allow-writes": args.allow_writes,
            "--allow-managed-writes": args.allow_managed_writes,
        },
    )
    if _refusal:
        print(_refusal, file=sys.stderr)
        return 1

    # K1: pre mints run_id; all other parts require --run-id (validated strictly
    # before any client is created — analogous to the security-review Warning on
    # Group 6's script).
    if args.part == "pre":
        run_id = (
            f"fi-loop-{dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%S')}"
            f"-{secrets.token_hex(4)}"
        )
    else:
        if args.run_id is None:
            print(
                f"error: --run-id is required for --part {args.part!r}; "
                f"run --part pre first to obtain the run id",
                file=sys.stderr,
            )
            return 1
        if not _RUN_ID_RE.fullmatch(args.run_id):
            print(
                f"error: --run-id {args.run_id!r} does not match "
                f"{_RUN_ID_RE.pattern!r}; use the id printed by --part pre",
                file=sys.stderr,
            )
            return 1
        run_id = args.run_id

    print(f"run_id: {run_id}  stage: {args.stage}  part: {args.part}")

    res = Results()

    try:
        _run_part(
            args.part,
            res,
            stage=args.stage,
            run_id=run_id,
            allow_writes=args.allow_writes,
            allow_managed_writes=args.allow_managed_writes,
        )
    finally:
        res.summary(run_id)

    if res.has_any_fail:
        return 1
    if res.has_any_skip:
        return 2
    return 0


# ── lazy-import list (for the subprocess import test) ────────────────────────
# The test derives this list from the script's AST; it is not used at runtime.
# Listed here as documentation; must match every ``import x  # noqa: PLC0415``
# inside function bodies.

if __name__ == "__main__":
    _install_signal_handlers()
    sys.exit(main())
