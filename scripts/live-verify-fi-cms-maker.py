#!/usr/bin/env python3
"""live-verify-fi-cms-maker.py — Task 8.4 maker-track live verification.

Parts (run with --part X): pre, perf, scope.
Sections C parts (investigate, decide, issue) built in a later stage.

Usage
-----
  LIVE_TESTS=1 python scripts/live-verify-fi-cms-maker.py --stage staging --part pre
  FI_CMS_MAKER_LIVE_GO=staging LIVE_TESTS=1 python scripts/live-verify-fi-cms-maker.py --stage staging --part perf  --allow-writes
  FI_CMS_MAKER_LIVE_GO=staging LIVE_TESTS=1 python scripts/live-verify-fi-cms-maker.py --stage staging --part scope --allow-writes
  LIVE_TESTS=1 python scripts/live-verify-fi-cms-maker.py --stage staging --part pre   --fresh

FACTS (cms:services/fleet_intelligence/1ce93a75b, cited file:line):
  maker.py
    FORBIDDEN_MSG = "Not authorized for the requested population scope"  maker.py:48
    PREFIX = "/api/v1/fleet-intelligence/population/"  maker.py:40
    CACHE_KEYS: patterns→_cache/fault-patterns-v1.json, signals, health_rollup,
      threads, campaigns  maker.py:57-63
    MAX_AGE_SECONDS = 26*3600  maker.py:65
    _READ_ROUTES: GET patterns,patterns/{id},signals,health-rollup,threads,
      threads/{id},campaigns/software,investigations/{id}  maker.py:1171-1178
    _WRITE_ROUTES: POST patterns/{id}/investigations,
      investigations/{id}/decision, investigations/{id}/decline,
      threads/{id}/issue  maker.py:1179-1183
    handle_start_investigation body check: 400 "This request takes no body fields"
      when body not ({} or None)  maker.py:1055
    handle_decision body check: 400 "Body may carry optionId and notes only"
      when extra keys  maker.py:1085
    handle_decline body check: 400 "Body may carry reason only"  maker.py:1121
    handle_issue threadId check: 400 "Invalid threadId"  maker.py:1145
    _page: pageToken b64url-decoded {o:int, c:computed_at}  maker.py:406-428
    project_pattern_item keys: patternId,codeFamily,topCause,candidateCount,
      vehiclesAffected,vehiclesInService,rate,firstDetectedMonth,trend,method,
      computedAt,parametersVersion  maker.py:307-322
    project_signal keys: codeFamily,cohortDimension,cohortValue,rate30d,
      rate90d,riseRatio,events30d,vehiclesAffected30d,firstSignalDate,
      parametersVersion  maker.py:331-342
    FORBIDDEN_TOKENS frozenset (includes decidedBySub, requested_by, requestedBy)
      maker.py:83-89
    _COORD_PAIR_RE pattern  maker.py:94
    _FLEET_ID_RE pattern  maker.py:100
  _auth.py
    MAKER_READ_GROUPS = {"connected-services","product-engineer","platform-admin"}
      _auth.py:178
    MAKER_WRITE_GROUP = "product-engineer"  _auth.py:179
  deployment/stacks/ui_stack.py
    fi-population-refresh-failing alarm: {construct_id}-fi-population-refresh-failing
      ui_stack.py:4386
    fi-population-views-stale alarm: {construct_id}-fi-population-views-stale
      ui_stack.py:4403
    ATHENA_OUTPUT_LOC = fi_naming.results_output_location(stage, account)
      = s3://cms-{stage}-athena-results-{account}-us-east-1/fleet-intelligence/
      ui_stack.py:3830; _fleet_intelligence_naming.py:184
    AVX_INVESTIGATIONS_TABLE_NAME: vsa-{fi_stage}-avx-investigations  ui_stack.py:3881
  vsa-tier2-stack.ts
    SQS queue: vsa-{stage}-avx-investigations  vsa-tier2-stack.ts:622
    DLQ:       vsa-{stage}-avx-investigations-dlq  vsa-tier2-stack.ts:615
    DLQ alarm: vsa-{stage}-avx-investigations-dlq-depth-{region} ts:2403
  ADP 895f7b61:
    adp-{stage}-fault-patterns (pk=pattern_id, 85 rows, 45 pattern_group_ids):
      code_family, cohort_dimension, cohort_value, candidate_rank, pattern_group_id;
      NO failure_mode_id.
    adp-{stage}-engineering-corpus (pk=fm#<id>, 15 items):
      failure_mode_id, component_id, symptom_codes L<S>,
      options L<M{remedy_option_id, remedy_type, ...}>.
    CMS cache patternId == pattern_group_id.
  Planted patterns (ADP 895f7b61 plants.py PATTERNS):
    pinned by test_pin_plants_against_adp in the test file.
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import re
import sys
import time
import urllib.parse
from pathlib import Path
from typing import Any

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from agents.fi_quality.fallback import _match_failure_mode  # noqa: E402

from scripts.fi_cms_live.harness import (  # noqa: E402
    Http, Results,
    _check_residue_cognito, _client, _install_signal_handlers, _is_cognito_admin_write, _now,
    _password_client, check_stage, delete_manifest_users, make_run_id,
    open_manifest, principal,
    require_write_window, update_manifest, validate_run_id,
)

# ---------------------------------------------------------------------------
# Live-write go (shared helper: scripts/lib/live_write_guard.py)
# ---------------------------------------------------------------------------

if str(Path(__file__).resolve().parent / "lib") not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))
import live_write_guard as _lwg  # noqa: E402

#: Set to ``--stage`` by the live-run step, on the user's go, and by nothing else.
_LIVE_GO_ENV = "FI_CMS_MAKER_LIVE_GO"

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_FI = "/api/v1/fleet-intelligence"
_POP = f"{_FI}/population"
_403_ERR = "Not authorized for the requested population scope"
# CMS-matching alias for the 403 error string (Pinned by test_pin_forbidden_tokens_against_cms_maker).
FORBIDDEN_MSG = _403_ERR
_PERF_REQUESTS = 20
_PERF_P95_S = 3.0
_REGION = "us-west-2"
_ADP_REGION = "us-east-1"
_MAX_PAGES = 100
# MAX_AGE_SECONDS: default cache freshness (26 h, same as maker.MAX_AGE_SECONDS).
# Used for pre unless --fresh is set (then 2 h).
MAX_AGE_SECONDS = 26 * 3600
_FRESH_MAX_AGE_S = 2 * 3600
_RESULTS_PREFIX = "fleet-intelligence/"
_ADP_COMMIT = "895f7b61"

# Population-finding identity constants (byte-identical to maker.py:51-55).
# Pinned by test_pin_forbidden_tokens_against_cms_maker.
POPULATION_ID = "meridian-motors"
QUALITY_AGENT = "agent-fi-quality"
FINDING_KEY_INDEX = "by_finding_key"

# ID format regexes (byte-identical to maker.py:60-64).
# Pinned by test_pin_forbidden_tokens_against_cms_maker.
INVESTIGATION_ID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)
PATTERN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9\-_\.]{0,254}$")
THREAD_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9\-_\.]{0,127}$")

# Forbidden-token frozenset: byte-identical copy of maker.py:83-89.
# Pinned by test_pin_forbidden_tokens_against_cms_maker; includes the three
# tokens the prior committed copy was missing: decidedBySub, requested_by,
# requestedBy.
_FORBIDDEN_TOKENS = frozenset({
    "fleet_id", "fleetId", "fleetName",
    "driver_id", "driverId", "driverName",
    "owner_id", "ownerId", "ownerName",
    "customer_id", "customerId",
    "lat", "lng", "latitude", "longitude",
    "trip_id", "tripId",
    "dealer_id", "dealerId",
    "rooftop_id", "rooftopId",
    "cost_usd", "totalCost", "dailyCost",
    "decidedBySub", "requested_by", "requestedBy",
})

# Byte-identical regexes to CMS maker.py:90-100.
_FORBIDDEN_TOKEN_RE = re.compile(
    r"\b(" + "|".join(re.escape(t) for t in sorted(_FORBIDDEN_TOKENS, key=len, reverse=True)) + r")\b"
)
_COORD_PAIR_RE = re.compile(r"-?\d{1,3}\.\d{3,}\s*,\s*-?\d{1,3}\.\d{3,}")
_FLEET_ID_RE = re.compile(
    r"\b(?:flt-[A-Za-z0-9-]+|FLEET-[A-Z0-9-]+|demo-[a-z]+-[a-z-]+)\b"
)

# Planted patterns: extracted from ADP 895f7b61 plants.py PATTERNS.
# Pinned by test_pin_plants_against_adp in the test file.
# Fields used: code_family, cause_dimension, cause_value.
# (rate, future_dated, planted_seed, start_date, failure_mode_id not needed here.)
PLANTS: list[dict] = [
    {"code_family": "P0A0",  "cause_dimension": "plant",        "cause_value": "RNO"},
    {"code_family": "P1A0",  "cause_dimension": "build_window",
     "cause_value": ["2023-01", "2023-02", "2023-03", "2023-04"]},
    {"code_family": "P1B1",  "cause_dimension": "build_window",
     "cause_value": ["2022-09", "2022-10", "2022-11"]},
    {"code_family": "U007",  "cause_dimension": "build_window",
     "cause_value": ["2025-07", "2025-08", "2025-09"]},
    {"code_family": "P0AA",  "cause_dimension": "build_window",
     "cause_value": ["2024-02", "2024-03", "2024-04", "2024-05"]},
    {"code_family": "P0A7",  "cause_dimension": "build_window",
     "cause_value": ["2025-10", "2025-11", "2025-12", "2026-01", "2026-02", "2026-03"]},
    {"code_family": "P1AB",  "cause_dimension": "supplier_lot", "cause_value": "SUP-B/OBC-L01-B"},
    {"code_family": "P1B0",  "cause_dimension": "supplier_lot", "cause_value": "SUP-B/DUI-L05-B"},
]

# build_window dimension names (same as in ADP test_patterns_on_simulation.py).
_BW_DIMS = frozenset({"build_window_2m", "build_window_3m", "build_window_4m"})

# Per-route top-level allowlists for GET item responses.
_PATTERNS_LIST_ITEM_KEYS = frozenset({
    "patternId", "codeFamily", "topCause", "candidateCount",
    "vehiclesAffected", "vehiclesInService", "rate", "firstDetectedMonth",
    "trend", "method", "computedAt", "parametersVersion",
})
_PATTERNS_ITEM_KEYS = _PATTERNS_LIST_ITEM_KEYS | frozenset({
    "candidates", "affectedVinsSample", "investigation", "investigationLookup",
})
_TOP_CAUSE_KEYS = frozenset({
    "cohortDimension", "cohortValue", "candidateRank", "rateRatio", "pValue",
})
_CANDIDATE_KEYS = frozenset({
    "candidateId",
    "cohortDimension", "cohortValue", "candidateRank", "patternGroupId",
    "vehiclesAffected", "vehiclesInService", "rate", "rateRest", "rateRatio",
    "pValue", "trend",
})
_VIN_SAMPLE_KEYS = frozenset({
    "vin", "model", "modelYear", "assemblyPlant", "buildMonth", "componentLot",
})
_SIGNALS_ITEM_KEYS = frozenset({
    "codeFamily", "cohortDimension", "cohortValue", "rate30d", "rate90d",
    "riseRatio", "events30d", "vehiclesAffected30d", "firstSignalDate",
    "parametersVersion",
})
_ROLLUP_ITEM_KEYS = frozenset({"cohortValue", "kindCounts"})
_THREADS_ITEM_KEYS = frozenset({
    "threadId", "version", "codeFamily", "causeDimension", "causeValue",
    "detectionMethod", "rateRatio", "pValue", "optionId", "optionRemedyType",
    "decidedAt", "affectedVinsCount", "affectedVinsSha256", "affectedVinsS3Key",
    "softwareCampaignId", "recallCampaignId", "status", "decisionOutcome",
    "campaignType", "campaignStatus", "decidedByKind", "notes",
})
_CAMPAIGNS_ITEM_KEYS = frozenset({
    "campaignId", "campaignName", "status", "releaseVersion", "targetModels",
    "targetModelYearMin", "targetModelYearMax", "targetPlants",
    "targetComponentLots", "overTheAir", "leadTimeWeeks", "affectedVinsCount",
    "threadId", "issuedAt", "createdAt",
})

_ROUTE_ALLOWLISTS: dict[str, frozenset[str]] = {
    "patterns":      _PATTERNS_LIST_ITEM_KEYS,  # list-item level; detail keys not expected here
    "signals":       _SIGNALS_ITEM_KEYS,
    "health-rollup": _ROLLUP_ITEM_KEYS,
    "threads":       _THREADS_ITEM_KEYS,
    "campaigns":     _CAMPAIGNS_ITEM_KEYS,
}

# A-2 W3: per-route wrapper-level allowlist (non-data top-level keys; from CMS maker.py 1ce93a75b).
# patterns:      handle_patterns 542-543 + _unavailable 154-156
# signals:       handle_signals 572 + _unavailable
# health-rollup: handle_health_rollup 582-584 + _unavailable (passes cohortDimension in **extra)
# threads:       handle_threads 599 + _unavailable
# campaigns:     handle_campaigns 620 + _unavailable
_ROUTE_WRAPPER_KEYS: dict[str, frozenset[str]] = {
    "patterns":      frozenset({"nextPageToken", "computedAt", "facets",
                                "unavailableReason", "lastComputedAt"}),
    "signals":       frozenset({"nextPageToken", "computedAt",
                                "unavailableReason", "lastComputedAt"}),
    "health-rollup": frozenset({"cohortDimension", "totalManagedVins", "computedAt",
                                "unavailableReason", "lastComputedAt"}),
    "threads":       frozenset({"nextPageToken", "computedAt",
                                "unavailableReason", "lastComputedAt"}),
    "campaigns":     frozenset({"nextPageToken", "computedAt",
                                "unavailableReason", "lastComputedAt"}),
}

# A-7: position allowlists for every nested position (from cmsmaker.py 1ce93a75b projections).

# Facets wrapper dict (handle_patterns → {models, modelYears, plants, supplierIds}).
_FACETS_KEYS = frozenset({"models", "modelYears", "plants", "supplierIds"})

# kindCounts (project_rollup_row): keys = HEALTH_KINDS.
_KIND_COUNTS_KEYS = frozenset({"battery", "brakes", "tires", "diagnostics"})

# Per-kind severity count dict (values inside kindCounts): keys = SEVERITIES.
_KIND_SEVERITY_KEYS = frozenset({"p0", "attention", "soon", "routine"})

# Pattern detail's embedded live investigation summary (live_investigation_for_pattern).
_INVESTIGATION_INLINE_KEYS = frozenset({"investigationId", "status", "requestedAt"})

# Investigation body (read_investigation).
_INVESTIGATION_BODY_KEYS = frozenset({
    "investigationId", "patternId", "status", "requestedAt", "startedAt",
    "completedAt", "findingId", "failureReason", "result", "resultLookup",
})

# project_investigation_result (the "result" field of an investigation body).
_RESULT_KEYS = frozenset({
    "findingId", "headline", "severity", "confidence", "computedAt", "expiresAt",
    "diagnosisSource", "rootCause", "failureMode", "candidates", "options",
    "affectedVins", "controls", "recommendedOptionId",
})

# result.rootCause (project_investigation_result).
_ROOT_CAUSE_KEYS = frozenset({"candidateId", "failureModeId", "reasoning", "confidence"})

# result.failureMode (project_investigation_result).
_FAILURE_MODE_KEYS = frozenset({"failureModeId", "component", "mechanism", "symptomCodes"})

# Vin-list container dict (returned by _vin_list for affectedVins / controls).
_VIN_LIST_KEYS = frozenset({"count", "sample"})

# result.options[] items (project_investigation_result).
_OPTION_KEYS = frozenset({
    "remedyOptionId", "remedyType", "partsCostPerVehicleUsd", "laborHoursPerVehicle",
    "totalVehicles", "totalRemedyCostUsd", "leadTimeWeeks", "overTheAir",
    "recommended", "tradeoffs",
})

# _project_action output (decision and issue response bodies).
_ACTION_KEYS = frozenset({"actionId", "actionKind", "status", "decision", "failureReason"})

# project_thread_record output (project_thread_item + THREAD_DETAIL_FIELDS + extra fields).
# Pinned against cmsmaker.py 1ce93a75b THREAD_LIST_FIELDS, project_thread_item, THREAD_DETAIL_FIELDS.
_THREAD_RECORD_KEYS = _THREADS_ITEM_KEYS | frozenset({
    "investigationFindingId", "rootCauseCandidateId", "failureModeId", "agentVersion",
    "partsCostedPerVehicleUsd", "laborHoursPerVehicle", "leadTimeWeeks", "overTheAir",
    "patternId", "evidenceRefs", "diagnosisSource",
})

# W2 / A-7: per-key allowlists for known nested dict values.
_NESTED_DICT_ALLOWLISTS: dict[str, frozenset] = {
    "topCause": _TOP_CAUSE_KEYS,
    # A-7: facets wrapper (patterns list response)
    "facets": _FACETS_KEYS,
    # A-7: kindCounts + per-kind severity dicts (rollup item)
    "kindCounts": _KIND_COUNTS_KEYS,
    "battery": _KIND_SEVERITY_KEYS,
    "brakes": _KIND_SEVERITY_KEYS,
    "tires": _KIND_SEVERITY_KEYS,
    "diagnostics": _KIND_SEVERITY_KEYS,
    # A-7: pattern detail inline investigation summary
    "investigation": _INVESTIGATION_INLINE_KEYS,
    # A-7: investigation body positions
    "result": _RESULT_KEYS,
    # A-7: result nested positions
    "rootCause": _ROOT_CAUSE_KEYS,
    "failureMode": _FAILURE_MODE_KEYS,
    # A-7: affectedVins / controls are _vin_list dicts {count, sample[]}
    "affectedVins": _VIN_LIST_KEYS,
    "controls": _VIN_LIST_KEYS,
}

# W2 / A-7: per-key allowlists for items inside known list values.
_LIST_ITEM_ALLOWLISTS: dict[str, frozenset] = {
    "candidates": _CANDIDATE_KEYS,          # pattern-level + result-level (superset is fine)
    "affectedVinsSample": _VIN_SAMPLE_KEYS,
    "sample": _VIN_SAMPLE_KEYS,              # A-7: sample[] inside _vin_list dicts
    "options": _OPTION_KEYS,                 # A-7: result.options[] items
}

# Per-part op allowlists.
# pre uses only read ops; no Cognito, no write ops.
# Pinned by test_pre_allowlist_has_no_write_ops.
_PRE_OPS = frozenset({
    "cloudwatch.DescribeAlarms",
    "cloudformation.DescribeStacks",
    "sqs.GetQueueUrl",
    "sqs.GetQueueAttributes",
    "lambda.ListEventSourceMappings",
    "s3.GetObject",
    "sts.GetCallerIdentity",
    "dynamodb.GetItem",
    "dynamodb.Scan",
})
_COGNITO_OPS = frozenset({
    "cognito-idp.ListUserPoolClients", "cognito-idp.DescribeUserPoolClient",
    "cognito-idp.AdminCreateUser", "cognito-idp.AdminSetUserPassword",
    "cognito-idp.AdminAddUserToGroup", "cognito-idp.InitiateAuth",
    "cognito-idp.AdminDeleteUser", "cognito-idp.AdminGetUser",
    "cognito-idp.ListUsers",
})
# perf and scope need Cognito for temp users; pre does not.
_PERF_OPS = _PRE_OPS | _COGNITO_OPS
_SCOPE_OPS = _PRE_OPS | _COGNITO_OPS

# investigate / decide / issue: Cognito for engineer temp user; no direct DDB/SQS writes.
# issue additionally queries the findings table by GSI (dynamodb.Query).
# Pinned by test_c_allowlist_has_no_write_ops in the test file.
_INVESTIGATE_OPS = _PRE_OPS | _COGNITO_OPS
_DECIDE_OPS = _PRE_OPS | _COGNITO_OPS
_ISSUE_OPS = _PRE_OPS | _COGNITO_OPS | frozenset({"dynamodb.Query"})
# cleanup must delete manifest-recorded users (W-3) and then check residue.
_CLEANUP_OPS = _PRE_OPS | frozenset({
    "cognito-idp.ListUsers", "cognito-idp.ListUserPools",
    "cognito-idp.ListUserPoolClients", "cognito-idp.DescribeUserPoolClient",
    "cognito-idp.AdminDeleteUser", "cognito-idp.AdminGetUser",
})
_SCHEDULER_OPS = frozenset({
    "scheduler.ListSchedules",
    "scheduler.GetSchedule",
})


# ---------------------------------------------------------------------------
# compute_finding_key — byte-identical copy of maker.py:656-668
# ---------------------------------------------------------------------------

def compute_finding_key(
    *,
    agent_id: str,
    scope_field: str,
    scope_value: str,
    finding_kind: str,
    subject_key: str | None = None,
) -> str:
    """Byte-identical copy of AVX agents/tier2/writer.py compute_finding_key."""
    payload: dict[str, str] = {
        "agent_id": agent_id,
        "finding_kind": finding_kind,
        "scope_field": scope_field,
        "scope_value": scope_value,
    }
    if subject_key:
        payload["subject_key"] = subject_key
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# _derive_thread_id — byte-identical copy of ADP dtw.py _derive_thread_id
# (lines 813-819).  Pinned by test_pin_thread_id_against_adp.
# ---------------------------------------------------------------------------

def _derive_thread_id(pattern_id: str, investigation_finding_id: str) -> str:
    """Deterministic, stable hash of (pattern_id, investigation_finding_id).

    Byte-identical copy of ADP 895f7b61 digital_thread_writer/handler.py
    _derive_thread_id (lines 813-819).
    """
    key = f"{pattern_id}|{investigation_finding_id}"
    return "thread-" + hashlib.sha256(key.encode()).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Planted-pattern matching
# ---------------------------------------------------------------------------

def _matches(row: dict, plant: dict) -> bool:
    """True if a fault-patterns row matches a planted-pattern entry.

    Copied from ADP platform-foundation/source/data-products/engineering/
    tests/test_patterns_on_simulation.py lines 36-52, adapted for dict access
    and list cause_value (PLANTS stores cause_value as a list for build_window).
    Also in scripts/live-verify-fi-quality.py:1018-1037.
    """
    if row.get("code_family") != plant.get("code_family"):
        return False
    dim = str(row.get("cohort_dimension", ""))
    val = str(row.get("cohort_value", ""))
    cd = plant.get("cause_dimension", "")
    if cd == "plant":
        return dim == "assembly_plant" and val == plant.get("cause_value")
    if cd == "supplier_lot":
        return dim == "supplier_lot" and val == plant.get("cause_value")
    if cd == "build_window":
        if dim not in _BW_DIMS:
            return False
        parts = val.split(":")
        start, end = (parts[0], parts[-1]) if len(parts) >= 2 else (val, val)
        pv = plant.get("cause_value", [])
        months: list[str] = pv if isinstance(pv, list) else str(pv).split(":")
        return any(start <= m <= end for m in months)
    return False


# ---------------------------------------------------------------------------
# Discovery helpers
# ---------------------------------------------------------------------------

def _discover_cms_stack(cfn: Any, stage: str) -> tuple[str, str]:
    """Return (api_base, pool_id) from cms-{stage}-ui CFN outputs."""
    outs = cfn.describe_stacks(StackName=f"cms-{stage}-ui")["Stacks"][0]["Outputs"]
    api_base = next(
        o["OutputValue"].rstrip("/") for o in outs if o["OutputKey"] == "APIEndpoint"
    )
    pool_id = next(o["OutputValue"] for o in outs if o["OutputKey"] == "UserPoolId")
    return api_base, pool_id


def _athena_output_loc(sts: Any, stage: str) -> tuple[str, str, str]:
    """Return (bucket, prefix, full_loc) for population caches.

    Bucket: cms-{stage}-athena-results-{account}-us-east-1
    Prefix: fleet-intelligence/
    (fi_naming.results_output_location, ui_stack.py:3830)
    """
    account = sts.get_caller_identity()["Account"]
    bucket = f"cms-{stage}-athena-results-{account}-us-east-1"
    prefix = _RESULTS_PREFIX
    return bucket, prefix, f"s3://{bucket}/{prefix}"


# ---------------------------------------------------------------------------
# pre helpers: alarm, cache freshness, SQS / event-source, pattern pick
# ---------------------------------------------------------------------------

def _check_alarm(cw: Any, name: str, res: Results) -> None:
    try:
        alarms = cw.describe_alarms(AlarmNames=[name], AlarmTypes=["MetricAlarm"])
        state = (alarms.get("MetricAlarms") or [{}])[0].get("StateValue", "MISSING")
        res.check(f"alarm {name}", state == "OK", f"state={state}")
    except Exception as exc:  # noqa: BLE001
        res.check(f"alarm {name}", False, f"describe_alarms error: {exc}")


def _check_pop_caches(
    s3: Any, bucket: str, prefix: str,
    res: Results, manifest: Path,
    max_age_s: int = MAX_AGE_SECONDS,
) -> None:
    """Check all 5 population caches have computedAt < max_age_s."""
    cache_keys = {
        "patterns":     "_cache/fault-patterns-v1.json",
        "signals":      "_cache/quality-signals-v1.json",
        "health_rollup":"_cache/health-rollup-v1.json",
        "threads":      "_cache/digital-thread-list-v1.json",
        "campaigns":    "_cache/software-campaigns-v1.json",
    }
    now = _now()
    ages: dict[str, Any] = {}
    age_label = "2h" if max_age_s <= _FRESH_MAX_AGE_S else "26h"
    for name, key in cache_keys.items():
        full_key = prefix + key
        try:
            obj = s3.get_object(Bucket=bucket, Key=full_key)
            payload = json.loads(obj["Body"].read())
            computed_at = payload.get("computedAt", "")
            if not computed_at:
                res.check(f"pop-cache {name}: computedAt present", False, "missing")
                ages[name] = "missing"
                continue
            parsed = datetime.datetime.fromisoformat(computed_at.replace("Z", "+00:00"))
            age_s = (now - parsed).total_seconds()
            ok = 0 <= age_s < max_age_s
            ages[name] = f"{age_s / 3600:.2f}h"
            res.check(
                f"pop-cache {name}: age < {age_label}",
                ok,
                f"age={age_s / 3600:.2f}h computed_at={computed_at}",
            )
        except Exception as exc:  # noqa: BLE001
            res.check(f"pop-cache {name}: readable", False, str(exc)[:120])
            ages[name] = f"error: {exc}"
    update_manifest(manifest, "pop_cache_ages", ages)


def _check_sqs_and_event_source(
    sqs: Any, lam: Any, stage: str, res: Results,
) -> bool:
    """Check investigations queue + DLQ hold no messages (visible AND in flight) and
    the queue's one event source on the health agent is Enabled.

    Returns True only when every check passed; ``investigate`` refuses to POST otherwise
    (a message already in flight, or no consumer, would make the run's result someone
    else's or never arrive).
    """
    ok_all = True
    for qname in (
        f"vsa-{stage}-avx-investigations",
        f"vsa-{stage}-avx-investigations-dlq",
    ):
        try:
            url = sqs.get_queue_url(QueueName=qname)["QueueUrl"]
            attrs = sqs.get_queue_attributes(
                QueueUrl=url, AttributeNames=[
                    "ApproximateNumberOfMessages",
                    "ApproximateNumberOfMessagesNotVisible"],
            )["Attributes"]
            n = int(attrs.get("ApproximateNumberOfMessages", -1))
            n_nv = int(attrs.get("ApproximateNumberOfMessagesNotVisible", -1))
            ok = n == 0 and n_nv == 0
            ok_all &= ok
            res.check(f"queue {qname}: depth=0", ok, f"visible={n} in_flight={n_nv}")
        except Exception as exc:  # noqa: BLE001
            ok_all = False
            res.check(f"queue {qname}: readable", False, str(exc)[:120])

    fn = f"vsa-{stage}-tier2-health-agent-{_REGION}"
    suffix = f":vsa-{stage}-avx-investigations"
    try:
        esms = lam.list_event_source_mappings(FunctionName=fn)
        inv_esms = [
            e for e in esms.get("EventSourceMappings", [])
            if (e.get("EventSourceArn") or "").endswith(suffix)
        ]
        if len(inv_esms) != 1:
            ok_all = False
            res.check("investigations event source: found", False,
                      f"{len(inv_esms)} mappings for {suffix} on {fn}")
        else:
            state = inv_esms[0].get("State", "")
            ok_all &= state == "Enabled"
            res.check(
                "investigations event source: Enabled",
                state == "Enabled",
                f"State={state}",
            )
    except Exception as exc:  # noqa: BLE001
        ok_all = False
        res.check("investigations event source", False, str(exc)[:120])
    return ok_all


def _scan_fp_table(ddb_e: Any, stage: str) -> dict[str, list[dict]]:
    """Full scan of adp-{stage}-fault-patterns; return {pattern_group_id: [rows]}.

    All reads paged (DynamoDB Scan with LastEvaluatedKey).
    Rows carry: pattern_id, pattern_group_id, code_family, cohort_dimension,
    cohort_value, candidate_rank.  (No failure_mode_id — rule 5 uses the corpus.)
    """
    table = f"adp-{stage}-fault-patterns"
    groups: dict[str, list[dict]] = {}
    kw: dict[str, Any] = {"TableName": table}
    for _ in range(_MAX_PAGES):
        page = ddb_e.scan(**kw)
        for item in page.get("Items", []):
            # Item may be DDB-typed {"S": "..."} or plain str (boto3 resource vs client)
            gid = _ddb_str(item.get("pattern_group_id"))
            if gid:
                groups.setdefault(gid, []).append({
                    "pattern_id":       _ddb_str(item.get("pattern_id")) or "",
                    "pattern_group_id": gid,
                    "code_family":      _ddb_str(item.get("code_family")) or "",
                    "cohort_dimension": _ddb_str(item.get("cohort_dimension")) or "",
                    "cohort_value":     _ddb_str(item.get("cohort_value")) or "",
                    "candidate_rank":   _ddb_int(item.get("candidate_rank")) or 0,
                })
        if not page.get("LastEvaluatedKey"):
            break
        kw["ExclusiveStartKey"] = page["LastEvaluatedKey"]
    else:
        # security review c1 (M W2): a partial read could hide a planted row of a group.
        raise RuntimeError(f"{table} scan exceeded {_MAX_PAGES} pages")
    return groups


def _scan_corpus_table(ddb_e: Any, stage: str) -> list[dict]:
    """Full scan of adp-{stage}-engineering-corpus; return list of fm dicts.

    Each fm has: failure_mode_id, component_id, symptom_codes, options.
    options are sorted by remedy_option_id (per readers.py:527-532 Athena order).
    """
    table = f"adp-{stage}-engineering-corpus"
    fms: list[dict] = []
    kw: dict[str, Any] = {"TableName": table}
    for _ in range(_MAX_PAGES):
        page = ddb_e.scan(**kw)
        for item in page.get("Items", []):
            fm_id = _ddb_str(item.get("failure_mode_id"))
            if not fm_id:
                # try parsing from pk "fm#<id>"
                pk = _ddb_str(item.get("pk")) or ""
                if pk.startswith("fm#"):
                    fm_id = pk[3:]
            if not fm_id:
                continue
            raw_codes = item.get("symptom_codes", [])
            symptom_codes = _ddb_str_list(raw_codes)
            raw_opts = item.get("options", [])
            options = _ddb_opts(raw_opts)
            # Sort by remedy_option_id (Athena read order, readers.py:527-532)
            options = sorted(options, key=lambda o: o.get("remedy_option_id", ""))
            fms.append({
                "failure_mode_id": fm_id,
                "component_id": _ddb_str(item.get("component_id")) or "",
                "symptom_codes": symptom_codes,
                "options": options,
            })
        if not page.get("LastEvaluatedKey"):
            break
        kw["ExclusiveStartKey"] = page["LastEvaluatedKey"]
    else:
        # security review c1 (M W2): a partial read could hide a failure mode's options.
        raise RuntimeError(f"{table} scan exceeded {_MAX_PAGES} pages")
    return fms


def _ddb_str(v: Any) -> str | None:
    """Extract a string from a plain Python str or DDB-typed {"S": "..."}."""
    if isinstance(v, str):
        return v if v else None
    if isinstance(v, dict) and "S" in v:
        return v["S"] if v["S"] else None
    return None


def _ddb_int(v: Any) -> int | None:
    if isinstance(v, int):
        return v
    if isinstance(v, float):
        return int(v)
    if isinstance(v, str) and v.lstrip("-").isdigit():
        return int(v)
    if isinstance(v, dict) and "N" in v:
        return int(v["N"])
    return None


def _ddb_str_list(v: Any) -> list[str]:
    """Extract a list of strings from plain Python list or DDB-typed {"L": [...]}."""
    if isinstance(v, list):
        result: list[str] = []
        for item in v:
            s = _ddb_str(item)
            if s:
                result.append(s)
        return result
    if isinstance(v, dict) and "L" in v:
        return _ddb_str_list(v["L"])
    return []


def _ddb_opts(v: Any) -> list[dict]:
    """Extract a list of remedy option dicts from plain list or DDB L<M{...}>."""
    if isinstance(v, list):
        result: list[dict] = []
        for item in v:
            if isinstance(item, dict):
                if "M" in item:
                    # DDB-typed map
                    d = {k: _ddb_str(vv) or "" for k, vv in item["M"].items()}
                else:
                    # Plain Python dict
                    d = {k: (vv if isinstance(vv, (str, bool, int, float)) else str(vv))
                         for k, vv in item.items()}
                result.append(d)
        return result
    if isinstance(v, dict) and "L" in v:
        return _ddb_opts(v["L"])
    return []


def _pick_pattern(
    s3: Any,
    ddb_w: Any,
    ddb_e: Any,
    bucket: str,
    prefix: str,
    stage: str,
    res: Results,
    manifest: Path,
) -> str | None:
    """Pick the first qualifying pattern group; return pattern_group_id or None.

    Rules (brief §B), over pattern groups sorted by pattern_group_id:
    1. In the CMS patterns cache (patternId == pattern_group_id).
    2. Not planted: no candidate of the group matches any PLANTS entry.
    3. No supplier_lot or component candidate.
    4. No lock#<patternId> row in vsa-{stage}-avx-investigations.
    5. Predicted failure mode (_match_failure_mode) has a software_update option.
    """
    # Read CMS patterns cache from S3
    try:
        obj = s3.get_object(Bucket=bucket, Key=prefix + "_cache/fault-patterns-v1.json")
        cache_data = json.loads(obj["Body"].read()).get("data") or []
    except Exception as exc:  # noqa: BLE001
        res.check("pattern pick: cache readable", False, str(exc)[:100])
        return None

    # Read all fault-patterns rows and all corpus items from ADP DDB (full scans). A scan
    # that can't finish refuses the pick (security review c1, M W2).
    try:
        fp_groups = _scan_fp_table(ddb_e, stage)
        fms = _scan_corpus_table(ddb_e, stage)
    except RuntimeError as exc:
        res.check("pattern pick: ADP tables read in full", False, on_fail=str(exc)[:160])
        return None

    inv_table = f"vsa-{stage}-avx-investigations"
    skip_log: list[str] = []

    # Sort CMS cache patterns by patternId (= pattern_group_id)
    sorted_patterns = sorted(cache_data, key=lambda p: p.get("patternId") or "")

    for pat in sorted_patterns:
        gid = pat.get("patternId") or ""
        if not gid:
            continue
        cf = pat.get("codeFamily") or ""

        # Rule 1: in cache (it is, since we read from cache). Already satisfied.

        # Rule 2: not planted — no candidate of the group matches any PLANTS entry
        # §E.1 fix: if no ADP rows exist for this group, skip (PatternNotFound in job.py).
        # Never fall back to the cache's topCause: topCause is one candidate, not all.
        group_rows = fp_groups.get(gid) or []
        if not group_rows:
            skip_log.append(f"{gid}: not in ADP fault-patterns")
            continue
        planted = any(
            _matches(row, plant)
            for row in group_rows
            for plant in PLANTS
        )
        if planted:
            skip_log.append(f"{gid}: matches planted pattern")
            continue

        # Rule 3: no supplier_lot or component candidate
        has_lot_or_comp = any(
            r.get("cohort_dimension") in {"supplier_lot", "component", "component_id"}
            for r in group_rows
        )
        if has_lot_or_comp:
            skip_log.append(f"{gid}: supplier_lot or component candidate")
            continue

        # Rule 4: no lock row in investigations table (consistent read)
        try:
            lock = ddb_w.get_item(
                TableName=inv_table,
                Key={"investigation_id": {"S": f"lock#{gid}"}},
                ConsistentRead=True,
            )
            if lock.get("Item"):
                skip_log.append(f"{gid}: locked")
                continue
        except Exception:  # noqa: BLE001
            skip_log.append(f"{gid}: lock check error")
            continue

        # Rule 5: predicted failure mode has software_update option
        fm = _match_failure_mode(fms, cf, {})
        if fm is None:
            skip_log.append(f"{gid}: no matching failure mode")
            continue
        opts = fm.get("options") or []
        sw_opts = [o for o in opts if o.get("remedy_type") == "software_update"]
        if not sw_opts:
            skip_log.append(f"{gid}: FM has no software_update option")
            continue

        # Found a qualifying pattern — record and return
        sw_opt_id = sw_opts[0].get("remedy_option_id", "")
        # First option (sorted by remedy_option_id) is the recommended option
        rec_opt_id = opts[0].get("remedy_option_id", "") if opts else ""
        fm_id = fm.get("failure_mode_id", "")
        # Guard: refuse to overwrite pattern_pick if an investigation is already recorded
        try:
            _ex = json.loads(manifest.read_text())["items"]
            if _ex.get("investigation_id"):
                res.check(
                    "pattern pick: refuse to overwrite pick — investigation already recorded",
                    False,
                    f"investigation_id={_ex.get('investigation_id')!r} recorded; "
                    f"run a new investigation to change the pick",
                )
                return None
        except Exception:  # noqa: BLE001
            pass  # manifest unreadable — will fail on write
        update_manifest(manifest, "pattern_pick", {
            "pattern_id": gid,
            "code_family": cf,
            "predicted_failure_mode_id": fm_id,
            "software_option_id": sw_opt_id,
            "recommended_option_id": rec_opt_id,
        })
        res.check("pattern pick", True, f"pattern_id={gid} fm={fm_id} sw_opt={sw_opt_id}")
        return gid

    res.check(
        "pattern pick: qualifying pattern found",
        False,
        f"none of {len(cache_data)} patterns qualified; skipped: {skip_log[:5]}",
    )
    return None


# ---------------------------------------------------------------------------
# run_pre
# ---------------------------------------------------------------------------

def run_pre(
    stage: str,
    run_id: str,
    manifest: Path,
    res: Results,
    cfn: Any,
    cw: Any,
    sqs: Any,
    lam: Any,
    s3: Any,
    sts: Any,
    ddb_w: Any,
    ddb_e: Any,
    max_age_s: int = MAX_AGE_SECONDS,
) -> None:
    print("\n--- pre ---", flush=True)
    res.note("stage", stage)
    res.note("run_id", run_id)
    res.note("utc", _now().isoformat())

    # Population alarms
    _check_alarm(cw, f"cms-{stage}-ui-fi-population-refresh-failing", res)
    _check_alarm(cw, f"cms-{stage}-ui-fi-population-views-stale", res)

    # Population cache freshness (5 caches, each < max_age_s)
    bucket, prefix, _ = _athena_output_loc(sts, stage)
    _check_pop_caches(s3, bucket, prefix, res, manifest, max_age_s=max_age_s)

    # Investigations SQS queue/DLQ 0/0 and event source Enabled
    _check_sqs_and_event_source(sqs, lam, stage, res)

    # Pattern pick: write pick if found; clear any stale pick if not.
    # Read investigation state before calling _pick_pattern (the pick must not be
    # cleared while an investigation is already recorded — only a genuine "no pattern"
    # when no investigation is in flight justifies clearing).
    try:
        _pre_items = json.loads(manifest.read_text())["items"]
        _has_investigation = bool(_pre_items.get("investigation_id"))
    except Exception:  # noqa: BLE001
        _has_investigation = False
    pick_result = _pick_pattern(s3, ddb_w, ddb_e, bucket, prefix, stage, res, manifest)
    if pick_result is None and not _has_investigation:
        update_manifest(manifest, "pattern_pick", None)

    update_manifest(manifest, "pre", {
        "completed_at": _now().isoformat(),
        "fresh": max_age_s <= _FRESH_MAX_AGE_S,
        "passed": not bool(res.failed),
    })
    print(f"manifest: {manifest}", flush=True)


# ---------------------------------------------------------------------------
# run_perf
# ---------------------------------------------------------------------------

def run_perf(
    stage: str,
    run_id: str,
    manifest: Path,
    res: Results,
    api: Http,
    idp: Any,
    pool_id: str,
    client_id: str,
    scheduler: Any,
) -> None:
    print("\n--- perf ---", flush=True)
    _refuse = require_write_window(scheduler, stage, _now())
    if _refuse is not None:
        res.skip("perf: write window",
                 f"{_refuse} — resume: --part perf --run-id {run_id}")
        return
    with principal(
        idp, pool_id, client_id, run_id, "perf-eng",
        manifest, res, groups=["product-engineer"],
    ) as (token, _):
        def _measure(path: str) -> tuple[list[float], list[bool]]:
            lats: list[float] = []
            bad: list[bool] = []
            for _ in range(_PERF_REQUESTS):
                t0 = time.monotonic()
                s, b = api.call("GET", path, token)
                lats.append(time.monotonic() - t0)
                bad.append(s != 200 or not isinstance(b, dict) or b.get("data") is None)
            return lats, bad

        for route_name, path in (
            ("patterns", f"{_POP}/patterns"),
            ("signals",  f"{_POP}/signals"),
        ):
            lats, bad = _measure(path)
            lats.sort()
            p95 = lats[max(int(len(lats) * 0.95) - 1, 0)]
            ok = p95 < _PERF_P95_S
            res.check(
                f"perf {route_name} p95 < {_PERF_P95_S}s",
                ok,
                f"p95={p95:.3f}s (n={len(lats)})",
            )
            res.check(
                f"perf {route_name}: all {len(lats)} requests 200 with data",
                not any(bad),
                f"{sum(bad)} of {len(bad)} requests had non-200 or missing data",
            )
    update_manifest(manifest, "perf", {"completed_at": _now().isoformat()})


# ---------------------------------------------------------------------------
# Scope helpers: allowlist check and forbidden-value scan
# ---------------------------------------------------------------------------

def _str_forbidden(val: Any) -> str | None:
    """Return the first forbidden substring found in *val* (a str), or None."""
    if not isinstance(val, str):
        return None
    if _FLEET_ID_RE.search(val):
        return f"fleet id in {val!r:.40}"
    m = _COORD_PAIR_RE.search(val)
    if m:
        return f"coordinate pair {m.group()!r}"
    m2 = _FORBIDDEN_TOKEN_RE.search(val)
    if m2:
        return f"forbidden token {m2.group()!r}"
    return None


def _scan_any(
    obj: Any,
    route_path: str,
    res: Results,
    top_allowlist: frozenset | None = None,
) -> None:
    """Recursive forbidden-token scan (brief §A.3/A.4 + C2/W2 fixes).

    - Every key name in ANY dict at any depth is checked against _FORBIDDEN_TOKENS.
    - Every string value at any depth is checked with _FORBIDDEN_TOKEN_RE,
      _COORD_PAIR_RE, _FLEET_ID_RE — including strings inside lists (C2).
    - At any depth where top_allowlist is not None, extra keys not in the allowlist
      also FAIL.
    - Known nested dict and list fields use position allowlists (W2).
    """
    if isinstance(obj, dict):
        for k, v in obj.items():
            # Check key name at this depth
            if k in _FORBIDDEN_TOKENS:
                res.check(
                    f"scope scan {route_path}: no forbidden key name",
                    False,
                    f"key={k!r} is in FORBIDDEN_TOKENS",
                )
            # Allowlist check at this depth
            if top_allowlist is not None and k not in top_allowlist:
                res.check(
                    f"scope scan {route_path}: no extra key",
                    False,
                    f"key={k!r} not in allowlist",
                )
            # Recurse into value
            if isinstance(v, str):
                reason = _str_forbidden(v)
                if reason:
                    res.check(
                        f"scope scan {route_path}: no forbidden value",
                        False,
                        f"key={k!r} {reason}",
                    )
            elif isinstance(v, list):
                # W2: per-key item allowlist; C2: strings inside lists scanned
                # A-7: fail closed when items are dicts and no allowlist registered.
                item_al = _LIST_ITEM_ALLOWLISTS.get(k)
                for item in v:
                    if isinstance(item, str):
                        reason = _str_forbidden(item)
                        if reason:
                            res.check(
                                f"scope scan {route_path}: no forbidden value",
                                False,
                                f"list element {reason}",
                            )
                    elif isinstance(item, dict):
                        if item_al is None:
                            res.check(
                                f"scope scan {route_path}: no allowlist for key",
                                False,
                                f"no allowlist for list-of-dicts at key={k!r}",
                            )
                        _scan_any(item, route_path, res, top_allowlist=item_al)
                    else:
                        _scan_any(item, route_path, res, top_allowlist=None)
            elif isinstance(v, dict):
                # A-7: per-key allowlist for known nested dicts; fail closed on unknown.
                nested_al = _NESTED_DICT_ALLOWLISTS.get(k)
                if nested_al is None:
                    res.check(
                        f"scope scan {route_path}: no allowlist for key",
                        False,
                        f"no allowlist for nested dict at key={k!r}",
                    )
                _scan_any(v, route_path, res, top_allowlist=nested_al)
    elif isinstance(obj, list):
        for item in obj:
            if isinstance(item, str):
                reason = _str_forbidden(item)
                if reason:
                    res.check(
                        f"scope scan {route_path}: no forbidden value",
                        False,
                        f"list element {reason}",
                    )
            else:
                _scan_any(item, route_path, res, top_allowlist=None)


def _page_and_scan(
    api: Http, token: str, path: str, route_short: str, res: Results,
) -> None:
    """Follow pages for *path* and scan each item recursively."""
    allowed = _ROUTE_ALLOWLISTS.get(route_short, frozenset())
    cursor: str | None = None
    for _ in range(_MAX_PAGES):
        full = path
        if cursor:
            full += ("&" if "?" in path else "?") + f"pageToken={urllib.parse.quote(cursor, safe='')}"
        status, body = api.call("GET", full, token)
        if status != 200:
            res.check(f"scope scan {path}: GET 200", False, f"got {status}")
            return
        if not isinstance(body, dict):
            return
        # A-2 W3: scan wrapper-level keys (all keys except "data") against the per-route allowlist.
        wrapper_keys = _ROUTE_WRAPPER_KEYS.get(route_short)
        wrapper_body = {k: v for k, v in body.items() if k != "data"}
        _scan_any(wrapper_body, f"{path} wrapper", res, top_allowlist=wrapper_keys)
        for item in body.get("data") or []:
            _scan_any(item, path, res, top_allowlist=allowed)
        # A-7: scan facets wrapper dict (patterns list response; HEALTH_KINDS keys via kindCounts).
        facets_obj = body.get("facets")
        if isinstance(facets_obj, dict):
            _scan_any(facets_obj, f"{path} facets", res, top_allowlist=_FACETS_KEYS)
        cursor = body.get("nextPageToken")
        if not cursor:
            break


def _check_probe_403(res: Results, label: str, status: int, body: Any) -> None:
    ok = status == 403 and isinstance(body, dict) and _403_ERR in body.get("error", "")
    res.check(
        f"scope probe {label}: 403 FORBIDDEN_MSG",
        ok,
        f"got {status} {json.dumps(body)[:120]}",
    )


# ---------------------------------------------------------------------------
# run_scope
# ---------------------------------------------------------------------------

def run_scope(
    stage: str,
    run_id: str,
    manifest: Path,
    res: Results,
    api: Http,
    idp: Any,
    pool_id: str,
    client_id: str,
    scheduler: Any,
) -> None:
    print("\n--- scope ---", flush=True)
    _refuse = require_write_window(scheduler, stage, _now())
    if _refuse is not None:
        res.skip("scope: write window",
                 f"{_refuse} — resume: --part scope --run-id {run_id}")
        return

    # Fake ids for scope probes (valid format but won't exist)
    _FAKE_PATTERN_ID = "probe-no-such-pattern-000"
    _FAKE_INV_ID     = "00000000-0000-0000-0000-000000000001"
    _FAKE_THREAD_ID  = "probe-no-such-thread-000"

    # Routes, split into list (need 200+no-unavailable for CS) and detail (need {400,404})
    _list_routes = [
        (f"{_POP}/patterns",           "patterns"),
        (f"{_POP}/signals",            "signals"),
        (f"{_POP}/health-rollup",      "health-rollup"),
        (f"{_POP}/threads",            "threads"),
        (f"{_POP}/campaigns/software", "campaigns"),
    ]
    _detail_routes = [
        (f"{_POP}/patterns/{_FAKE_PATTERN_ID}",   "patterns/{id}"),
        (f"{_POP}/threads/{_FAKE_THREAD_ID}",     "threads/{id}"),
        (f"{_POP}/investigations/{_FAKE_INV_ID}", "investigations/{id}"),
    ]
    _all_get_routes = _list_routes + _detail_routes

    all_post_probes = [
        (f"{_POP}/patterns/{_FAKE_PATTERN_ID}/investigations", {},       "patterns/{id}/investigations"),
        (f"{_POP}/investigations/{_FAKE_INV_ID}/decision",    {},        "investigations/{id}/decision"),
        (f"{_POP}/investigations/{_FAKE_INV_ID}/decline",     {},        "investigations/{id}/decline"),
        (f"{_POP}/threads/{_FAKE_THREAD_ID}/issue",           {},        "threads/{id}/issue"),
    ]

    # ── fleet-operator: 403 on every GET and POST ──────────────────────────
    with principal(
        idp, pool_id, client_id, run_id, "scope-fo",
        manifest, res, groups=["fleet-operator"],
        fleet_ids="demo-rental-carshare",
    ) as (fo_token, _):
        for path, label in _all_get_routes:
            s, b = api.call("GET", path, fo_token)
            _check_probe_403(res, f"GET {label} fleet-op", s, b)
        for path, body, label in all_post_probes:
            s, b = api.call("POST", path, fo_token, body)
            _check_probe_403(res, f"POST {label} fleet-op", s, b)

    # ── connected-services: 200+no-unavailable on list GETs, {400,404} on detail GETs,
    #                         403 on POSTs ─────────────────────────────────
    with principal(
        idp, pool_id, client_id, run_id, "scope-cs",
        manifest, res, groups=["connected-services"],
    ) as (cs_token, _):
        # List routes: must be 200 with no unavailableReason (brief §A.6)
        for path, label in _list_routes:
            s, b = api.call("GET", path, cs_token)
            is_ok = (
                s == 200
                and isinstance(b, dict)
                and b.get("data") is not None
                and "unavailableReason" not in b
            )
            res.check(
                f"scope CS GET {label}: 200 no-unavailable",
                is_ok,
                f"got {s} {json.dumps(b)[:80]}",
            )
        # Detail routes: non-403 AND in {400, 404} (brief §A.6)
        for path, label in _detail_routes:
            s, b = api.call("GET", path, cs_token)
            not_403 = not (
                s == 403
                and isinstance(b, dict)
                and _403_ERR in b.get("error", "")
            )
            in_range = s in {400, 404}
            res.check(
                f"scope CS GET {label}: non-403 in {{400,404}}",
                not_403 and in_range,
                f"got {s} {json.dumps(b)[:80]}",
            )
        # Write routes: 403
        for path, body, label in all_post_probes:
            s, b = api.call("POST", path, cs_token, body)
            res.check(
                f"scope CS POST {label}: 403",
                s == 403 and isinstance(b, dict) and _403_ERR in b.get("error", ""),
                f"got {s} {json.dumps(b)[:80]}",
            )

    # ── engineer: POST controls (validation 400) + GET allowlist scan ──────
    with principal(
        idp, pool_id, client_id, run_id, "scope-eng",
        manifest, res, groups=["product-engineer"],
    ) as (eng_token, _):
        # POST controls: each must fail at VALIDATION (400), not scope check.
        # start investigation: body with extra field → 400 "This request takes no body fields"
        s, b = api.call(
            "POST", f"{_POP}/patterns/{_FAKE_PATTERN_ID}/investigations",
            eng_token, {"__scope_ctrl": True},
        )
        ctrl_ok = s == 400
        ctrl_msg_ok = isinstance(b, dict) and "This request takes no body fields" in b.get("error", "")
        res.check(
            "scope ctrl POST investigations: 400 validation",
            ctrl_ok,
            f"got {s} {json.dumps(b)[:100]}; must be refused at validation not scope",
        )
        res.check(
            "scope ctrl POST investigations: error text",
            ctrl_msg_ok,
            f"got error={b.get('error', '')!r:.80}" if isinstance(b, dict) else "",
        )
        # decision: body with extra key → "Body may carry optionId and notes only"
        s, b = api.call(
            "POST", f"{_POP}/investigations/{_FAKE_INV_ID}/decision",
            eng_token, {"extra": "field"},
        )
        res.check(
            "scope ctrl POST decision: 400 validation",
            s == 400,
            f"got {s} {json.dumps(b)[:100]}",
        )
        # decline: body with extra key → "Body may carry reason only"
        s, b = api.call(
            "POST", f"{_POP}/investigations/{_FAKE_INV_ID}/decline",
            eng_token, {"extra": "field"},
        )
        res.check(
            "scope ctrl POST decline: 400 validation",
            s == 400,
            f"got {s} {json.dumps(b)[:100]}",
        )
        # issue: invalid thread id format → "Invalid threadId"
        s, b = api.call(
            "POST", f"{_POP}/threads/_invalid-ctrl/issue",
            eng_token, {},
        )
        res.check(
            "scope ctrl POST issue: 400 validation",
            s == 400,
            f"got {s} {json.dumps(b)[:100]}",
        )

        # Paged GET allowlist scan of all list routes
        for path, short in _list_routes:
            _page_and_scan(api, eng_token, path, short, res)

        # Scan one real pattern detail: GET the first patternId from the cache (brief §A.7)
        s_list, b_list = api.call("GET", f"{_POP}/patterns", eng_token)
        if s_list != 200 or not isinstance(b_list, dict):
            res.check(
                "scope eng patterns list 200",
                False,
                f"got {s_list} {json.dumps(b_list)[:80]}",
            )
        else:
            items = b_list.get("data") or []
            if not items:
                res.check(
                    "scope eng patterns list non-empty",
                    False,
                    "data is empty or missing — no pattern detail scanned",
                )
            elif isinstance(items[0], dict):
                first_pid = items[0].get("patternId") or ""
                if not first_pid or not PATTERN_ID_RE.match(first_pid):
                    res.check(
                        "scope eng patterns first id valid",
                        False,
                        f"got {first_pid!r}",
                    )
                else:
                    s_det, b_det = api.call("GET", f"{_POP}/patterns/{first_pid}", eng_token)
                    res.check(
                        f"scope eng GET patterns/{first_pid}: 200",
                        s_det == 200,
                        f"got {s_det} {json.dumps(b_det)[:80]}",
                    )
                    if s_det == 200 and isinstance(b_det, dict):
                        # Scan the detail response recursively (A-7: use _PATTERNS_ITEM_KEYS)
                        _scan_any(b_det, f"patterns/{first_pid}", res, top_allowlist=_PATTERNS_ITEM_KEYS)

    update_manifest(manifest, "scope", {"completed_at": _now().isoformat()})


# ---------------------------------------------------------------------------
# _poll_issue_finding
# ---------------------------------------------------------------------------

def _poll_issue_finding(
    ddb_w: Any,
    stage: str,
    thread_id: str,
    poll_interval_s: int = 30,
    max_wait_s: int = 900,
) -> dict | None:
    """Poll vsa-{stage}-avx-findings GSI by_finding_key for the quality.issue Finding.

    Returns the DDB-client item dict or None on timeout.
    """
    fk = compute_finding_key(
        agent_id=QUALITY_AGENT,
        scope_field="population_id",
        scope_value=POPULATION_ID,
        finding_kind="quality.issue",
        subject_key=f"{thread_id}/issue",
    )
    table_name = f"vsa-{stage}-avx-findings"
    deadline = time.monotonic() + max_wait_s
    while time.monotonic() < deadline:
        try:
            resp = ddb_w.query(
                TableName=table_name,
                IndexName=FINDING_KEY_INDEX,
                KeyConditionExpression="finding_key = :fk",
                ExpressionAttributeValues={":fk": {"S": fk}},
                Limit=10,
            )
            for item in resp.get("Items", []):
                # maker.py:671-698 filters: exact population scope, quality agent, kind.
                scope = (item.get("scope") or {}).get("M") or {}
                if (set(scope) == {"population_id"}
                        and (scope["population_id"] or {}).get("S") == POPULATION_ID
                        and (item.get("agent_id") or {}).get("S") == QUALITY_AGENT
                        and (item.get("finding_kind") or {}).get("S") == "quality.issue"
                        and (item.get("finding_key") or {}).get("S") == fk):
                    return item
        except Exception:  # noqa: BLE001
            pass
        time.sleep(poll_interval_s)
    return None


# ---------------------------------------------------------------------------
# run_investigate
# ---------------------------------------------------------------------------

class _Resume(Exception):
    """Internal: skip the pre-POST lock gate when resuming a recorded investigation."""


def run_investigate(
    stage: str,
    run_id: str,
    manifest: Path,
    res: Results,
    api: Http,
    idp: Any,
    pool_id: str,
    client_id: str,
    sqs: Any,
    lam: Any,
    ddb_w: Any,
    scheduler: Any,
) -> None:
    """POST investigation for the manifest pick, poll to completion.

    Prereqs:
    - 'pre' in manifest with completed_at < 2h ago and pattern_pick recorded
    - investigations event source Enabled and queue depth 0/0
    - no lock row for the pick (re-checked)
    """
    print("\n--- investigate ---", flush=True)
    _refuse = require_write_window(scheduler, stage, _now())
    if _refuse is not None:
        res.skip("investigate: write window",
                 f"{_refuse} — resume: --part investigate --run-id {run_id}")
        return

    try:
        with open(manifest) as _f:
            mdata = json.load(_f)["items"]
    except Exception as exc:  # noqa: BLE001
        res.check("investigate prereq: manifest readable", False, str(exc)[:120])
        return

    # Compute resume state early to gate the pre freshness check (item A-1/4).
    resume_id_early = mdata.get("investigation_id") or ""

    # 1. pre < 2h old — required for the first POST only; a resume inherits the old pre.
    pre = mdata.get("pre", {})
    if not pre:
        res.check("investigate prereq: pre in manifest", False, "no pre record in manifest")
        return
    pre_completed = pre.get("completed_at", "")
    if not pre_completed:
        res.check("investigate prereq: pre.completed_at", False, "missing")
        return
    try:
        pre_ts = datetime.datetime.fromisoformat(pre_completed.replace("Z", "+00:00"))
    except ValueError:
        res.check("investigate prereq: pre.completed_at parseable", False, pre_completed)
        return
    age_s = (_now() - pre_ts).total_seconds()
    if not resume_id_early:
        res.check("investigate prereq: pre < 2h old",
                  0 <= age_s < _FRESH_MAX_AGE_S,
                  f"pre completed_at={pre_completed} age={age_s/3600:.2f}h")
        if not (0 <= age_s < _FRESH_MAX_AGE_S) or pre.get("fresh") is not True or pre.get("passed") is not True:
            res.check("investigate prereq: pre --fresh and passed", False, json.dumps(pre)[:120])
            return

    # 2. pattern_pick
    pick = mdata.get("pattern_pick") or {}
    if not pick or not pick.get("pattern_id"):
        res.check("investigate prereq: pattern_pick in manifest", False, "no pick recorded")
        return
    gid = pick["pattern_id"]
    if mdata.get("investigation_id") and mdata.get("investigation_pattern_id") != gid:
        res.check("investigate: recorded investigation belongs to the pick", False,
                  f"recorded for {mdata.get('investigation_pattern_id')!r}, pick is {gid!r}")
        return
    sw_opt_id = pick.get("software_option_id", "")
    predicted_fm_id = pick.get("predicted_failure_mode_id", "")
    rec_opt_id = pick.get("recommended_option_id", "")
    res.note("pick", gid)

    inv_table = f"vsa-{stage}-avx-investigations"
    # A recorded investigation id means this run already POSTed: resume polling it and
    # never POST again (the lock row the gates below refuse on is then our own).
    resume_id = resume_id_early
    if resume_id and not INVESTIGATION_ID_RE.match(str(resume_id)):
        res.check("investigate: recorded investigationId valid", False, repr(resume_id)[:80])
        return

    # 3. Queue depth 0/0 and event source Enabled; refuse to POST otherwise.
    if not resume_id and not _check_sqs_and_event_source(sqs, lam, stage, res):
        res.check("investigate: refused before POST", False,
                  "investigations queue/DLQ not empty or its event source not Enabled")
        return

    # 4. Re-check no lock row (consistent read)
    try:
        if resume_id:
            raise _Resume()
        lock = ddb_w.get_item(
            TableName=inv_table,
            Key={"investigation_id": {"S": f"lock#{gid}"}},
            ConsistentRead=True,
        )
        if lock.get("Item"):
            res.check("investigate: no lock row (re-check)", False,
                      f"lock row already exists for {gid}")
            return
        res.check("investigate: no lock row (re-check)", True, f"clean for {gid}")
    except _Resume:
        res.note("investigate: resuming", str(resume_id))
    except Exception as exc:  # noqa: BLE001
        res.check("investigate: lock re-check", False, str(exc)[:120])
        return

    # 5. POST investigation as product-engineer temp user
    with principal(
        idp, pool_id, client_id, run_id, "inv-eng",
        manifest, res, groups=["product-engineer"],
    ) as (token, _):
        pat_path = urllib.parse.quote(gid, safe="")
        post_path = f"{_POP}/patterns/{pat_path}/investigations"

        if resume_id:
            status, body = 202, {"existing": False, "investigationId": resume_id}
        else:
            _refuse_post = require_write_window(scheduler, stage, _now())
            if _refuse_post is not None:
                res.skip("investigate POST: write window",
                         f"{_refuse_post} — resume: --part investigate --run-id {run_id}")
                return
            status, body = api.call("POST", post_path, token, {})
        is_202_fresh = status == 202 and isinstance(body, dict) and body.get("existing") is False
        is_200_existing = status == 200 and isinstance(body, dict) and body.get("existing") is True

        if is_200_existing:
            # Someone else's investigation (no manifest record) — FAIL, no decide
            res.check(
                "investigate: existing:true → manifest record check",
                False,
                "POST returned 200/existing:true but no record in manifest — another engineer's",
            )
            return

        res.check(
            "investigate: POST 202 existing:false",
            is_202_fresh,
            f"got {status} {json.dumps(body)[:200]}",
        )
        if not is_202_fresh:
            return

        inv_id = body.get("investigationId", "")
        if not INVESTIGATION_ID_RE.match(inv_id):
            res.check("investigate: valid investigationId in POST response",
                      False, f"got {inv_id!r}")
            return

        if not resume_id:
            update_manifest(manifest, "investigation_id", inv_id)
            update_manifest(manifest, "investigation_pattern_id", gid)
        res.note("investigationId", inv_id)

        # 6. Poll GET investigations/{id} every 15 s up to 20 min
        _POLL_INTERVAL = 15
        _MAX_WAIT = 20 * 60
        deadline = time.monotonic() + _MAX_WAIT
        final_body: dict | None = None
        final_status: str | None = None

        while time.monotonic() < deadline:
            s, b = api.call("GET", f"{_POP}/investigations/{inv_id}", token)
            if s != 200 or not isinstance(b, dict):
                time.sleep(_POLL_INTERVAL)
                continue
            st = b.get("status")
            if st in ("queued", "running"):
                time.sleep(_POLL_INTERVAL)
                continue
            # failed or completed
            final_status = st
            final_body = b
            break

        if final_body is None:
            res.check("investigate: completed within 20 min", False,
                      "timed out polling investigation status")
            return

        if final_status == "failed":
            reason = final_body.get("failureReason", "")
            res.check("investigate: not failed", False,
                      f"investigation failed: {reason!r}")
            return

        res.check("investigate: status completed",
                  final_status == "completed",
                  f"got {final_status!r}")
        if final_status != "completed":
            return

        # 7. Verify polled investigation belongs to pick (patternId field in GET response)
        polled_pid = final_body.get("patternId", "")
        res.check("investigate: polled investigation belongs to pick",
                  polled_pid == gid,
                  f"polled patternId={polled_pid!r}, pick={gid!r}")
        if polled_pid != gid:
            return

        # 8. Verify result fields
        result_lookup = final_body.get("resultLookup")
        res.check("investigate: resultLookup=ok",
                  result_lookup == "ok",
                  f"resultLookup={result_lookup!r}")

        result = final_body.get("result") or {}
        fm_id = (result.get("failureMode") or {}).get("failureModeId", "")
        res.check("investigate: FM matches predicted",
                  fm_id == predicted_fm_id,
                  f"got {fm_id!r}, predicted {predicted_fm_id!r}")

        options = result.get("options") or []
        sw_opts = [o for o in options if o.get("remedyType") == "software_update"]
        sw_opt_match = any(o.get("remedyOptionId") == sw_opt_id for o in sw_opts)
        res.check("investigate: software option in result",
                  bool(sw_opts) and sw_opt_match,
                  f"sw_opts_ids={[o.get('remedyOptionId') for o in sw_opts]!r} expected {sw_opt_id!r}")

        # diagnosisSource is a field of the result (maker.py:772), not of the body.
        ds = result.get("diagnosisSource")
        res.check("investigate: diagnosisSource=rules",
                  ds == "rules", f"got {ds!r}")

        result_rec_opt_id = result.get("recommendedOptionId", "")
        res.check("investigate: recommendedOptionId matches pick",
                  result_rec_opt_id == rec_opt_id,
                  f"got {result_rec_opt_id!r}, pick rec_opt={rec_opt_id!r}")

        # 8. Record findingId from GET response
        finding_id = final_body.get("findingId") or ""
        if finding_id:
            update_manifest(manifest, "finding_id", finding_id)
            res.note("findingId", finding_id)
        else:
            res.check("investigate: findingId in GET response",
                      False, "findingId missing or null")

        # 9. Idempotency: repeat POST → 200 existing:true, same investigationId.
        # Run once (recorded as idempotency_checked); skip on resume, skip when
        # requestedAt >= 7 days old (AVX would create a new investigation).
        _SEVEN_DAYS_S = 7 * 24 * 3600
        _req_at = final_body.get("requestedAt", "")
        _req_live = False
        try:
            _req_ts = datetime.datetime.fromisoformat(_req_at.replace("Z", "+00:00"))
            _req_age = (_now() - _req_ts).total_seconds()
            _req_live = 0 <= _req_age < _SEVEN_DAYS_S
        except Exception:  # noqa: BLE001
            pass
        if mdata.get("idempotency_checked"):
            res.note("investigate: idempotency check", "already recorded; skipped")
        elif not _req_live:
            res.note("investigate: idempotency check",
                     f"skipped — requestedAt={_req_at!r} >= 7 days or unparseable")
        else:
            # Use the recorded pattern (not current pick) to build the path.
            _rec_gid = mdata.get("investigation_pattern_id") or gid
            _rec_pat_path = urllib.parse.quote(_rec_gid, safe="")
            _rec_post_path = f"{_POP}/patterns/{_rec_pat_path}/investigations"
            _refuse_rep = require_write_window(scheduler, stage, _now())
            if _refuse_rep is not None:
                res.skip("investigate repeat POST: write window",
                         f"{_refuse_rep} — resume: --part investigate --run-id {run_id}")
                return
            s2, b2 = api.call("POST", _rec_post_path, token, {})
            res.check(
                "investigate: repeat POST → 200 existing:true same id",
                s2 == 200
                and isinstance(b2, dict)
                and b2.get("existing") is True
                and b2.get("investigationId") == inv_id,
                f"got {s2} {json.dumps(b2)[:200]}",
            )
            update_manifest(manifest, "idempotency_checked", True)

        # 10. Table check: exactly one investigation row for the pattern (ours) and the
        # lock row points at it; the repeat POST added nothing.
        try:
            lock_item = ddb_w.get_item(
                TableName=inv_table,
                Key={"investigation_id": {"S": f"lock#{gid}"}},
                ConsistentRead=True,
            ).get("Item") or {}
            ref = (lock_item.get("investigation_id_ref") or {}).get("S", "")
            res.check("investigate: lock row points at this investigation",
                      ref == inv_id, f"lock#{gid} investigation_id_ref={ref!r}")
            rows_for_pattern: list[str] = []
            kw: dict[str, Any] = {
                "TableName": inv_table, "ConsistentRead": True,
                "FilterExpression": "pattern_id = :p",
                "ExpressionAttributeValues": {":p": {"S": gid}},
            }
            for _ in range(_MAX_PAGES):
                page = ddb_w.scan(**kw)
                rows_for_pattern += [
                    (it.get("investigation_id") or {}).get("S", "")
                    for it in page.get("Items", [])
                    if not (it.get("investigation_id") or {}).get("S", "").startswith("lock#")
                ]
                if not page.get("LastEvaluatedKey"):
                    break
                kw["ExclusiveStartKey"] = page["LastEvaluatedKey"]
            else:
                raise RuntimeError("investigations scan exceeded its page cap")
            res.check("investigate: one investigation row for the pattern",
                      rows_for_pattern == [inv_id], f"rows={rows_for_pattern}")
        except Exception as exc:  # noqa: BLE001
            res.check("investigate: investigations table check after completion",
                      False, str(exc)[:120])

        # 11. Scan investigation response (A-7: use _INVESTIGATION_BODY_KEYS)
        _scan_any(final_body, f"investigations/{inv_id}", res, top_allowlist=_INVESTIGATION_BODY_KEYS)

    update_manifest(manifest, "investigate", {"completed_at": _now().isoformat(), "passed": not bool(res.failed)})
    print(f"manifest: {manifest}", flush=True)


# ---------------------------------------------------------------------------
# run_decide
# ---------------------------------------------------------------------------

def run_decide(
    stage: str,
    run_id: str,
    manifest: Path,
    res: Results,
    api: Http,
    idp: Any,
    pool_id: str,
    client_id: str,
    ddb_e: Any,
    scheduler: Any,
) -> None:
    """POST decision; verify thread record."""
    print("\n--- decide ---", flush=True)
    _refuse = require_write_window(scheduler, stage, _now())
    if _refuse is not None:
        res.skip("decide: write window",
                 f"{_refuse} — resume: --part decide --run-id {run_id}")
        return

    try:
        with open(manifest) as _f:
            mdata = json.load(_f)["items"]
    except Exception as exc:  # noqa: BLE001
        res.check("decide prereq: manifest readable", False, str(exc)[:120])
        return

    inv_id = mdata.get("investigation_id")
    pick = mdata.get("pattern_pick") or {}
    finding_id = mdata.get("finding_id", "")

    if not inv_id or not pick:
        res.check("decide prereq: investigation_id + pick in manifest", False,
                  f"inv_id={inv_id!r} pick_present={bool(pick)}")
        return

    gid = pick["pattern_id"]
    sw_opt_id = pick.get("software_option_id", "")
    rec_opt_id = pick.get("recommended_option_id", "")

    investigation_pattern_id = mdata.get("investigation_pattern_id")
    if investigation_pattern_id and gid != investigation_pattern_id:
        res.check(
            "decide prereq: pattern_pick matches investigation_pattern_id",
            False,
            f"pick has pattern_id={gid!r} but investigation was for "
            f"{investigation_pattern_id!r}; re-run from pre",
        )
        return

    if not finding_id or (mdata.get("investigate") or {}).get("passed") is not True:
        res.check("decide prereq: investigate passed", False,
                  "finding_id missing or investigate had FAILs — run investigate first")
        return

    expected_tid = _derive_thread_id(gid, finding_id)

    with principal(
        idp, pool_id, client_id, run_id, "dec-eng",
        manifest, res, groups=["product-engineer"],
    ) as (token, _):
        # POST decision with the software option
        notes = f"LIVETEST-{run_id} 8.4 live check"
        recorded = mdata.get("decision_action")
        if isinstance(recorded, dict) and recorded.get("actionId"):
            # This run already decided: never POST the decision again; verify only.
            status, body = 200, recorded
            res.note("decide: resuming", str(recorded.get("actionId")))
        else:
            _refuse_dec = require_write_window(scheduler, stage, _now())
            if _refuse_dec is not None:
                res.skip("decide POST: write window",
                         f"{_refuse_dec} — resume: --part decide --run-id {run_id}")
                return
            status, body = api.call(
                "POST",
                f"{_POP}/investigations/{inv_id}/decision",
                token,
                {"optionId": sw_opt_id, "notes": notes},
            )
            if status == 200 and isinstance(body, dict):
                update_manifest(manifest, "decision_action", body)

        # "executed": the status AVX's approve route gives an action its executor ran
        # (api-avx-actions/handler.py:144-153 statuses; the fake world runs the real route).
        action_status = body.get("status") if isinstance(body, dict) else None
        res.check(
            "decide: POST 200 status=executed",
            status == 200 and action_status == "executed",
            f"got {status} status={action_status!r} body={json.dumps(body)[:200]}",
        )
        if status != 200:
            return

        decision = body.get("decision")
        res.check(
            "decide: decision matches option vs recommended",
            decision == ("approved" if sw_opt_id == rec_opt_id else "overridden"),
            f"decision={decision!r} sw_opt={sw_opt_id!r} rec_opt={rec_opt_id!r}",
        )
        if decision == "approved":
            res.note("decision_outcome", f"approved (sw_opt={sw_opt_id!r} == rec_opt={rec_opt_id!r})")
        else:
            res.note("decision_outcome",
                     f"overridden (sw_opt={sw_opt_id!r} != rec_opt={rec_opt_id!r})")

        # Scan decision response (A-7: use _ACTION_KEYS)
        _scan_any(body, f"investigations/{inv_id}/decision", res, top_allowlist=_ACTION_KEYS)

        # GET thread detail
        thread_id = expected_tid
        s_t, b_t = api.call("GET", f"{_POP}/threads/{thread_id}", token)
        res.check(
            "decide: GET thread 200",
            s_t == 200,
            f"thread_id={thread_id!r} got {s_t} {json.dumps(b_t)[:200]}",
        )
        if s_t != 200 or not isinstance(b_t, dict):
            return

        # Verify thread fields
        res.check(
            "decide: thread optionRemedyType=software_update",
            b_t.get("optionRemedyType") == "software_update",
            f"optionRemedyType={b_t.get('optionRemedyType')!r}",
        )

        sc_id = b_t.get("softwareCampaignId")
        res.check("decide: softwareCampaignId non-null",
                  bool(sc_id), f"softwareCampaignId={sc_id!r}")

        res.check("decide: campaignStatus=draft",
                  b_t.get("campaignStatus") == "draft",
                  f"campaignStatus={b_t.get('campaignStatus')!r}")

        # Verify affectedVinsSha256 matches ADP fault-patterns rank-1 row
        try:
            fp_item = ddb_e.get_item(
                TableName=f"adp-{stage}-fault-patterns",
                Key={"pattern_id": {"S": gid}},
                ConsistentRead=True,
            ).get("Item") or {}
            raw_sha = fp_item.get("affected_vins_sha256")
            expected_sha = (raw_sha or {}).get("S", "") if isinstance(raw_sha, dict) else str(raw_sha or "")
            actual_sha = b_t.get("affectedVinsSha256") or ""
            # Fail closed: a missing ADP row or sha can't confirm the thread's VIN set.
            res.check(
                "decide: affectedVinsSha256 matches ADP fault-patterns",
                bool(expected_sha) and actual_sha == expected_sha,
                f"got {actual_sha!r} expected {expected_sha!r}",
            )
        except Exception as exc:  # noqa: BLE001
            res.check("decide: affectedVinsSha256 readable", False, f"error: {exc!s:.100}")

        # Record thread_id, version, softwareCampaignId, affectedVinsS3Key
        thread_version = b_t.get("version")
        update_manifest(manifest, "thread_id", thread_id)
        update_manifest(manifest, "thread_version_after_decide", thread_version)
        update_manifest(manifest, "software_campaign_id", sc_id)
        update_manifest(manifest, "affected_vins_s3_key", b_t.get("affectedVinsS3Key"))

        # Scan thread response (A-7: use _THREAD_RECORD_KEYS)
        _scan_any(b_t, f"threads/{thread_id}", res, top_allowlist=_THREAD_RECORD_KEYS)

    update_manifest(manifest, "decide", {"completed_at": _now().isoformat(), "passed": not bool(res.failed)})
    print(f"manifest: {manifest}", flush=True)


# ---------------------------------------------------------------------------
# run_issue
# ---------------------------------------------------------------------------

def run_issue(
    stage: str,
    run_id: str,
    manifest: Path,
    res: Results,
    api: Http,
    idp: Any,
    pool_id: str,
    client_id: str,
    ddb_w: Any,
    scheduler: Any,
) -> None:
    """Poll for quality.issue Finding, then POST thread issue."""
    print("\n--- issue ---", flush=True)
    _refuse = require_write_window(scheduler, stage, _now())
    if _refuse is not None:
        res.skip("issue: write window",
                 f"{_refuse} — resume: --part issue --run-id {run_id}")
        return

    try:
        with open(manifest) as _f:
            mdata = json.load(_f)["items"]
    except Exception as exc:  # noqa: BLE001
        res.check("issue prereq: manifest readable", False, str(exc)[:120])
        return

    thread_id = mdata.get("thread_id")
    recorded_version = mdata.get("thread_version_after_decide")

    if not thread_id:
        res.check("issue prereq: thread_id in manifest", False,
                  "run decide first")
        return

    _issue_pick = mdata.get("pattern_pick") or {}
    _issue_inv_pid = mdata.get("investigation_pattern_id")
    if _issue_inv_pid and _issue_pick.get("pattern_id") != _issue_inv_pid:
        res.check(
            "issue prereq: pattern_pick matches investigation_pattern_id",
            False,
            f"pick has pattern_id={_issue_pick.get('pattern_id')!r} but investigation was for "
            f"{_issue_inv_pid!r}; re-run from pre",
        )
        return

    if (mdata.get("decide") or {}).get("passed") is not True:
        res.check("issue prereq: decide passed", False,
                  "decide had FAILs — run decide first")
        return

    with principal(
        idp, pool_id, client_id, run_id, "issue-eng",
        manifest, res, groups=["product-engineer"],
    ) as (token, _):
        # 1. Poll (read-only) for quality.issue Finding — never POST before it exists
        finding_item = _poll_issue_finding(ddb_w, stage, thread_id)
        res.check(
            "issue: quality.issue Finding appeared",
            finding_item is not None,
            f"timed out waiting for quality.issue Finding for thread_id={thread_id!r}",
        )
        if finding_item is None:
            return

        res.note("issue_finding_key", (finding_item.get("finding_key") or {}).get("S", "") if isinstance(finding_item.get("finding_key"), dict) else finding_item.get("finding_key", ""))

        # 2. POST threads/{threadId}/issue (once per run: a recorded result is reused)
        recorded = mdata.get("issue_action")
        if isinstance(recorded, dict) and recorded.get("actionId"):
            status, body = 200, recorded
            res.note("issue: resuming", str(recorded.get("actionId")))
        else:
            _refuse_issue = require_write_window(scheduler, stage, _now())
            if _refuse_issue is not None:
                res.skip("issue POST: write window",
                         f"{_refuse_issue} — resume: --part issue --run-id {run_id}")
                return
            status, body = api.call(
                "POST",
                f"{_POP}/threads/{urllib.parse.quote(thread_id, safe='')}/issue",
                token,
                {},
            )
            if status == 200 and isinstance(body, dict):
                update_manifest(manifest, "issue_action", body)
        res.check(
            "issue: POST 200",
            status == 200,
            f"got {status} {json.dumps(body)[:200]}",
        )
        if status != 200:
            return

        # Scan issue POST response (A-7: use _ACTION_KEYS)
        _scan_any(body, f"threads/{thread_id}/issue", res, top_allowlist=_ACTION_KEYS)

        # 3. GET thread: version == recorded+1, status issued
        s_t, b_t = api.call("GET", f"{_POP}/threads/{thread_id}", token)
        res.check("issue: GET thread after issue 200",
                  s_t == 200,
                  f"got {s_t} {json.dumps(b_t)[:200]}")
        if s_t != 200 or not isinstance(b_t, dict):
            return

        new_version = b_t.get("version")
        if recorded_version is not None:
            expected_version = (recorded_version if isinstance(recorded_version, int) else int(recorded_version)) + 1
            res.check(
                "issue: thread version == decide_version + 1",
                new_version == expected_version,
                f"got {new_version!r} expected {expected_version!r}",
            )
        else:
            res.check("issue: thread version == decide_version + 1", False,
                      "no decide version in the manifest to compare with")

        res.check("issue: thread status=issued",
                  b_t.get("status") == "issued",
                  f"status={b_t.get('status')!r}")

        update_manifest(manifest, "thread_version_after_issue", new_version)

        # Scan thread response (A-7: use _THREAD_RECORD_KEYS)
        _scan_any(b_t, f"threads/{thread_id}/issued", res, top_allowlist=_THREAD_RECORD_KEYS)

    update_manifest(manifest, "issue", {"completed_at": _now().isoformat()})
    print(f"manifest: {manifest}", flush=True)


# ---------------------------------------------------------------------------
# run_cleanup
# ---------------------------------------------------------------------------

def run_cleanup(
    stage: str,
    run_id: str,
    manifest: Path,
    res: Results,
    idp: Any,
    pool_id: str,
) -> None:
    """No deletes.  Print kept records; verify no livetest users."""
    print("\n--- cleanup ---", flush=True)

    try:
        with open(manifest) as _f:
            mdata = json.load(_f)["items"]
    except Exception as exc:  # noqa: BLE001
        res.check("cleanup: manifest readable", False, str(exc)[:120])
        return

    pick = mdata.get("pattern_pick") or {}
    inv_id = mdata.get("investigation_id")
    finding_id = mdata.get("finding_id")

    _cleanup_inv_pid = mdata.get("investigation_pattern_id")
    if _cleanup_inv_pid and (pick or {}).get("pattern_id") != _cleanup_inv_pid:
        res.check(
            "cleanup prereq: pattern_pick matches investigation_pattern_id",
            False,
            f"pick has pattern_id={(pick or {}).get('pattern_id')!r} but investigation was for "
            f"{_cleanup_inv_pid!r}; re-run from pre",
        )

    thread_id = mdata.get("thread_id")
    sc_id = mdata.get("software_campaign_id")
    ver_decide = mdata.get("thread_version_after_decide")
    ver_issue = mdata.get("thread_version_after_issue")
    decide_action = mdata.get("decision_action") or {}
    issue_action = mdata.get("issue_action") or {}
    vin_key = mdata.get("affected_vins_s3_key")

    print("=== Kept records (permanent residue from this run) ===", flush=True)
    print(f"  pattern_id           : {pick.get('pattern_id', 'N/A')}", flush=True)
    print(f"  investigation_id     : {inv_id or 'N/A'}", flush=True)
    print(f"  finding_id           : {finding_id or 'N/A'}", flush=True)
    print(f"  decide_action_id     : {decide_action.get('actionId') or 'N/A'}", flush=True)
    print(f"  issue_action_id      : {issue_action.get('actionId') or 'N/A'}", flush=True)
    print(f"  thread_id            : {thread_id or 'N/A'}", flush=True)
    print(f"  thread_version_decide: {ver_decide or 'N/A'}", flush=True)
    print(f"  thread_version_issue : {ver_issue or 'N/A'}", flush=True)
    print(f"  software_campaign_id : {sc_id or 'N/A'}  (draft id)", flush=True)
    print(f"  affected_vins_s3_key : {vin_key or 'N/A'}", flush=True)
    print(f"  lock row             : lock#{pick.get('pattern_id', 'N/A')}", flush=True)
    print("These records are intentional and permanent; the thread table is append-only.", flush=True)

    # Delete any manifest-recorded users that failed to delete during the run (W-3)
    if pool_id:
        delete_manifest_users(idp, pool_id, manifest, res)
        ok, detail = _check_residue_cognito(idp, pool_id, run_id)
        res.check("cleanup: no livetest Cognito users", ok, detail)
    else:
        res.check("cleanup: no livetest Cognito users", False, "could not resolve the CMS pool")

    update_manifest(manifest, "cleanup", {"completed_at": _now().isoformat()})
    print(f"manifest: {manifest}", flush=True)

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Task 8.4 maker-track live verification"
    )
    parser.add_argument("--stage", required=True)
    parser.add_argument("--run-id", dest="run_id", default=None)
    parser.add_argument(
        "--part",
        choices=["pre", "perf", "scope", "investigate", "decide", "issue", "cleanup"],
        default=None,
    )
    parser.add_argument("--allow-writes", action="store_true")
    parser.add_argument(
        "--fresh",
        action="store_true",
        help="Check pop-cache age < 2h (required by investigate); default is 26h.",
    )
    args = parser.parse_args()

    check_stage(args.stage)

    # LIVE_TESTS gate: must be before any boto3 client or manifest (brief §A.1).
    if os.environ.get("LIVE_TESTS") != "1":
        print("SKIP: set LIVE_TESTS=1 to run live checks")
        return 2

    _refusal = _lwg.check_write_flags(
        stage=args.stage, go_env=_LIVE_GO_ENV,
        flags={"--allow-writes": args.allow_writes, "--part cleanup": args.part == "cleanup"},
    )
    if _refusal:
        print(_refusal, file=sys.stderr)
        return 1

    run_id = args.run_id or make_run_id()
    validate_run_id(run_id)
    _install_signal_handlers()

    stage = args.stage
    manifest = open_manifest(run_id, stage)
    res = Results()

    # Determine which part to run (default: pre).
    part = args.part or "pre"

    # Parts needing --allow-writes: temp Cognito users (brief §A.1).
    if part in ("perf", "scope", "investigate", "decide", "issue") and not args.allow_writes:
        res.skip(part, "--allow-writes required for temp Cognito users")
        res.summary()
        return res.exit_code

    # Build clients with only the ops needed for this part (brief §A.2).
    if part == "pre":
        ops = _PRE_OPS
    elif part == "investigate":
        ops = _INVESTIGATE_OPS
    elif part == "decide":
        ops = _DECIDE_OPS
    elif part == "issue":
        ops = _ISSUE_OPS
    elif part == "cleanup":
        ops = _CLEANUP_OPS
    else:
        ops = _PRE_OPS | _COGNITO_OPS

    cfn  = _client("cloudformation", _REGION,      allowed_ops=ops)
    cw   = _client("cloudwatch",     _REGION,      allowed_ops=ops)
    sqs  = _client("sqs",            _REGION,      allowed_ops=ops)
    lam  = _client("lambda",         _REGION,      allowed_ops=ops)
    s3   = _client("s3",             "us-east-1",  allowed_ops=ops)
    sts  = _client("sts",            _REGION,      allowed_ops=ops)
    ddb_w = _client("dynamodb",      _REGION,      allowed_ops=ops)
    ddb_e = _client("dynamodb",      _ADP_REGION,  allowed_ops=ops)
    # Before discovery the Cognito client gets no Admin* write op: the harness refuses to
    # build one without the run's username prefix and pool id (security review c1, M W1).
    idp  = _client("cognito-idp",    _REGION,
                   allowed_ops=frozenset(o for o in ops if not _is_cognito_admin_write(o)))
    # Read-only scheduler for write-window guard (us-west-2 — DMS schedules live there too)
    scheduler = _client("scheduler", _REGION,      allowed_ops=_SCHEDULER_OPS)

    # Discover CMS stack (needed for perf/scope, not pre).
    api: Http | None = None
    pool_id: str | None = None
    client_id: str | None = None

    if part != "pre":
        try:
            api_base, pool_id = _discover_cms_stack(cfn, stage)
        except Exception as exc:  # noqa: BLE001
            res.check("cms-ui stack discovery", False, str(exc)[:120])
            res.summary()
            return res.exit_code
        api = Http(api_base)
        if not pool_id:
            res.check("cms-ui stack discovery: user pool id", False,
                      on_fail="the stack's UserPoolId output is empty")
            res.summary()
            return res.exit_code
        # Re-build idp with the pool_id constraint now that we know it (W-2).
        # The write parts need cognito_username_prefix + cognito_pool_id.
        # cleanup also needs the prefix since it calls delete_manifest_users.
        if part not in ("pre",):
            idp = _client("cognito-idp", _REGION, allowed_ops=ops,
                          cognito_username_prefix=f"livetest-{run_id}-",
                          cognito_pool_id=pool_id)
        client_id = _password_client(idp, pool_id)

    max_age_s = _FRESH_MAX_AGE_S if args.fresh else MAX_AGE_SECONDS

    try:
        if part == "pre":
            run_pre(stage, run_id, manifest, res,
                    cfn, cw, sqs, lam, s3, sts, ddb_w, ddb_e,
                    max_age_s=max_age_s)
        elif part == "perf":
            assert api is not None and pool_id and client_id
            run_perf(stage, run_id, manifest, res, api, idp, pool_id, client_id, scheduler)
        elif part == "scope":
            assert api is not None and pool_id and client_id
            run_scope(stage, run_id, manifest, res, api, idp, pool_id, client_id, scheduler)
        elif part == "investigate":
            assert api is not None and pool_id and client_id
            run_investigate(stage, run_id, manifest, res,
                            api, idp, pool_id, client_id,
                            sqs, lam, ddb_w, scheduler)
        elif part == "decide":
            assert api is not None and pool_id and client_id
            run_decide(stage, run_id, manifest, res,
                       api, idp, pool_id, client_id, ddb_e, scheduler)
        elif part == "issue":
            assert api is not None and pool_id and client_id
            run_issue(stage, run_id, manifest, res,
                      api, idp, pool_id, client_id, ddb_w, scheduler)
        elif part == "cleanup":
            run_cleanup(stage, run_id, manifest, res, idp,
                        pool_id if pool_id else "")
        else:
            res.check(f"part {part!r}", False, "unknown part")
    finally:
        # Residue check for parts that create Cognito users.
        if part in ("perf", "scope", "investigate", "decide", "issue", "cleanup"):
            from scripts.fi_cms_live.harness import _resolve_cms_pool_id  # noqa: E402
            actual_pool = pool_id or _resolve_cms_pool_id(idp, stage)
            if actual_pool:
                ok, detail = _check_residue_cognito(idp, actual_pool, run_id)
                res.check("residue: no livetest Cognito users", ok, detail)
            else:
                res.check("residue: no livetest Cognito users", False,
                          "could not resolve the CMS pool")
    res.summary()
    return res.exit_code


if __name__ == "__main__":
    sys.exit(main())
