#!/usr/bin/env python3
"""live-verify-fi-cms-fleet.py — Task 7.7 fleet-track live verification.

Parts: pre, perf, isolation.

FACTS (cms:services/fleet_intelligence/1ce93a75b):
  index.py
    _authorize_or_403:2827 guards every /api/v1/fleet-intelligence/* route.
    Fleet-B 403 body: {"error": "Not authorized for the requested fleet scope"}
      (index.py:2272, 2855, health_source.py:153 — identical message).
    GET /faults:2276           — scope via _auth.authorize_fleet_scope
    GET /faults/summary:2290   — explicit authorize_fleet_scope:2299
    GET /vehicles/{vin}/faults:2312 — fleet resolved from vehicles table via
      groundings.resolve_vehicle; fleet-B vin → fleet-B → authorize_fleet_scope → 403
    GET /health-findings:2344  — scope inside health_source.get_health_findings
    GET /pm/compliance:2358    — authorize_fleet_scope
    GET /pm/schedules:2374     — same handler as /pm/compliance
    GET /pm/plans:2409         — pm_plans.list_plans; authorize_fleet_scope:pm_plans.py:291
    POST /pm/plans:2409        — _authorize_or_403 write check at body.fleetId (line 2858);
      fleet-B → 403; control fleet-A invalid taskCode → 400 "taskCode is not a PM task"
      (pm_plans.py:243, after scope check at pm_plans.py:237)
    PUT /pm/plans/{planId}:2409 — _authorize_or_403 write check at qs.fleetId;
      fleet-B → 403; control fleet-A empty update → 400 "Nothing to change"
      (pm_plans.py:321, after scope check at pm_plans.py:315)
    POST /pm/plans/{planId}/complete:2409 — _authorize_or_403 write check at body.fleetId;
      fleet-B → 403; control fleet-A missing vehicleId → 400 "vehicleId is required"
      (pm_plans.py:380, after scope check at pm_plans.py:375)
    POST /groundings:2926 — scope deferred; fleet-B vin resolves to fleet-B →
      authorize_fleet_write:2951 → 403; control fleet-A vin from a discovered returned
      row (runtime pick) with a mismatched findingId → 409 "grounding already recorded"
      (refused after scope; conditional put fails on existing row — groundings.py:228-242)
    POST /groundings/{vid}/return-to-service:3012 — scope deferred; fleet-B vid
      resolves to fleet-B → authorize_fleet_write:3025 → 403; control fleet-A vid with
      no open grounding → 409 {"reason":"not_grounded"} (groundings.py:return_to_service:314)
  pm_plans.py  _FORBIDDEN = "Not authorized for the requested fleet scope" :44
  groundings.py _403 body same message :23

grounding-items / ground FACTS (avx:agents/fi_fleet_health/writer.py:861a366e):
  subject_key = f"ground/{attempt}"  writer.py:161
  action_kind = "ground_vehicle"     writer.py:170
  severity = "p0"                    writer.py:162
  disclosure_txt = catalog_entry.get("directive") byte for byte  writer.py:155
  FINDING_KIND = "fleet.health.service"  write.py:35
  Two Findings per p0 VIN: ground/<n> + service/<n>  writer.py:278-296

grounding-items / ground FACTS (avx:lambdas/api-avx-findings/handler.py:861a366e):
  GET /findings/fleet/{fleet_id} paged with ?next_token=  handler.py:2226,2455-2463
  fleet_id in _PROJECTED_SCOPE_ATTRS (stripped at API boundary)  handler.py:109
    → read from scope.fleet_id in API responses
  POST /findings/{id}/approve → _approve_finding  handler.py:1199
  approve body fields: action_kind, target_system, parameters  handler.py:1213-1215
  AVX URL from vsa-{stage}-api CFN stack RestApiUrl  world.py:708

grounding-items / ground FACTS (cms:services/fleet_intelligence/groundings.py:1ce93a75b):
  409 fault_not_repaired: reason="fault_not_repaired", fault_codes  groundings.py:350-354
  dtc-history table: PK vehicleId (S), SK timestamp (N)  world.py:47
  CLEARED_REMOTE is cleared  cms:lambdas/shared/cms_client_real.py:60
  dtc-history row shape: vehicleId, timestamp, code, status, severity, clearedDate
    cms:lambdas/shared/tests/test_cms_client_real_active_dtcs.py:162-178

ground S4-S6 / cleanup FACTS (dms:de6c181):
  create_repair_order: POST /dms/dealers/{dealerId}/repair-orders; auth service-advisor or
    dealer-admin; custom:dealerIds scope (repair_orders.py:492,535,547); body vehicle_vin,
    lines via _normalize_lines, description; returns 201 with ro_id (repair_orders.py:625)
  update_repair_order: PATCH /dms/dealers/{dealerId}/repair-orders/{roId}; same auth
    (repair_orders.py:625); body status (repair_orders.py:670)
  RO_ACTIVE_STATUSES = frozenset({"Draft","InProgress","AwaitingParts"}) (repair_orders.py:193)
  is_fi_booking: fleet-health = any line with non-empty need_id (fi_stand_ins.py:101-117);
    recall = avx_action_id + customer_type=fleet + recall line with MRD-YYYY- catalog id
    (fi_stand_ins.py:89-96); is_fi_booking = is_fi_recall OR is_fi_fleet_health (fi_stand_ins.py:122)
  DMS API base: DmsApiStack-{stage} CFN output ApiUrl (live-verify-fi-recall-warranty.py:4377)
  Labor ops: agents/fi_fleet_health/adp_code_labor_op_map.json fault_codes section;
    one op per p0 code listed (P0217 → ["LAB-COOLOT-01"])
"""
from __future__ import annotations

import argparse
import datetime
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

from scripts.fi_cms_live.harness import (  # noqa: E402
    GuardViolation,
    _defer_signals_for_restore,
    Http,
    Results,
    _check_residue_cognito,
    _client,
    _install_signal_handlers,
    _now,
    _password_client,
    check_stage,
    delete_manifest_users,
    make_run_id,
    open_manifest,
    principal,
    require_write_window,
    update_manifest,
    validate_run_id,
)

# ---------------------------------------------------------------------------
# Live-write go (shared helper: scripts/lib/live_write_guard.py)
# ---------------------------------------------------------------------------

if str(Path(__file__).resolve().parent / "lib") not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))
import live_write_guard as _lwg  # noqa: E402

#: Set to ``--stage`` by the live-run step, on the user's go, and by nothing else.
_LIVE_GO_ENV = "FI_CMS_FLEET_LIVE_GO"

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

FLEET_A = "demo-rental-carshare"
FLEET_B = "demo-lastmile-delivery"
_ALL_FLEETS_LIST = [FLEET_A, FLEET_B, "demo-field-service", "demo-city-public"]
_ALL_FLEETS = ",".join(_ALL_FLEETS_LIST)
_AVX_FINDINGS_PAGE = 100
_DTC_HISTORY_TABLE_TPL = "cms-{stage}-storage-dtc-history"
_REGION = "us-west-2"
_ADP_REGION = "us-east-1"
_FI = "/api/v1/fleet-intelligence"
_403_ERR = "Not authorized for the requested fleet scope"
_PERF_REQUESTS = 20
_PERF_P95_S = 3.0
_FAULTS_LIMIT = 500      # GET /faults limit; max 500 per health_source._parse_limit
_FAULTS_MAX_PAGES = 100  # fail-closed cap; avoids unbounded reads

_PRE_OPS = frozenset({
    "cloudformation.DescribeStacks",
    "cloudwatch.DescribeAlarms",
    "lambda.GetFunctionConfiguration",
    "dynamodb.Scan",
    "dynamodb.Query",
    "dynamodb.GetItem",
})
_GROUNDING_ITEMS_OPS = _PRE_OPS | frozenset({
    "cognito-idp.ListUserPoolClients", "cognito-idp.DescribeUserPoolClient",
    "cognito-idp.AdminCreateUser", "cognito-idp.AdminSetUserPassword",
    "cognito-idp.AdminAddUserToGroup", "cognito-idp.InitiateAuth",
    "cognito-idp.AdminDeleteUser", "cognito-idp.AdminGetUser",
    "cognito-idp.ListUsers",
    "s3.GetObject",
    "sts.GetCallerIdentity",
})
_GROUND_OPS = _PRE_OPS | frozenset({
    "cognito-idp.ListUserPoolClients", "cognito-idp.DescribeUserPoolClient",
    "cognito-idp.AdminCreateUser", "cognito-idp.AdminSetUserPassword",
    "cognito-idp.AdminAddUserToGroup", "cognito-idp.InitiateAuth",
    "cognito-idp.AdminDeleteUser", "cognito-idp.AdminGetUser",
    "cognito-idp.ListUsers",
    # S3 dtc-history write dropped (W1); DynamoDB writes now only in cleanup
})
_COGNITO_OPS = frozenset({
    "cognito-idp.ListUserPoolClients", "cognito-idp.DescribeUserPoolClient",
    "cognito-idp.AdminCreateUser", "cognito-idp.AdminSetUserPassword",
    "cognito-idp.AdminAddUserToGroup", "cognito-idp.InitiateAuth",
    "cognito-idp.AdminDeleteUser", "cognito-idp.AdminGetUser",
    "cognito-idp.ListUsers",
})
_PERF_OPS = _PRE_OPS | _COGNITO_OPS
_ISOLATION_OPS = _PRE_OPS | _COGNITO_OPS
_SCHEDULER_OPS = frozenset({
    "scheduler.ListSchedules",
    "scheduler.GetSchedule",
})
# Note: _GROUNDING_ITEMS_OPS and _GROUND_OPS are defined above _PRE_OPS block

# ---------------------------------------------------------------------------
# Stack discovery
# ---------------------------------------------------------------------------


def _discover_cms_stack(cfn: Any, stage: str) -> tuple[str, str]:
    """Return (api_base, pool_id) from cms-{stage}-ui outputs."""
    outs = cfn.describe_stacks(StackName=f"cms-{stage}-ui")["Stacks"][0]["Outputs"]
    api_base = next(
        o["OutputValue"].rstrip("/") for o in outs if o["OutputKey"] == "APIEndpoint"
    )
    pool_id = next(o["OutputValue"] for o in outs if o["OutputKey"] == "UserPoolId")
    return api_base, pool_id


def _has_open_grounding(ddb: Any, groundings_table: str, vehicle_id: str) -> bool:
    """Return True if vehicleId has any grounding row with no returnedAt (open)."""
    kw: dict[str, Any] = {
        "TableName": groundings_table,
        "KeyConditionExpression": "vehicleId = :vid",
        "ExpressionAttributeValues": {":vid": {"S": vehicle_id}},
        "ConsistentRead": True,
    }
    while True:
        page = ddb.query(**kw)
        for item in page.get("Items", []):
            if "returnedAt" not in item:
                return True
        lek = page.get("LastEvaluatedKey")
        if not lek:
            break
        kw["ExclusiveStartKey"] = lek
    return False


def _discover_fleet_vins(ddb: Any, stage: str) -> tuple[str, str, str, str]:
    """Page cms-{stage}-storage-vehicles for one fleet-B and one fleet-A vehicle.

    Fleet-A vehicle must have no open grounding; tries the next if it does.
    Returns (fleet_a_vid, fleet_a_vin, fleet_b_vid, fleet_b_vin).
    Raises RuntimeError if either fleet lacks a suitable vehicle.
    """
    vehicles_table = f"cms-{stage}-storage-vehicles"
    groundings_table = f"cms-{stage}-storage-vehicle-groundings"
    fleet_a: tuple[str, str] | None = None   # (vid, vin)
    fleet_b: tuple[str, str] | None = None
    kw: dict[str, Any] = {
        "TableName": vehicles_table,
        "FilterExpression": "fleetId IN (:a, :b)",
        "ExpressionAttributeValues": {":a": {"S": FLEET_A}, ":b": {"S": FLEET_B}},
    }
    while True:
        page = ddb.scan(**kw)
        for raw in page.get("Items", []):
            fid = (raw.get("fleetId") or {}).get("S", "")
            vid = (raw.get("vehicleId") or {}).get("S", "")
            vin_val = (raw.get("vin") or {}).get("S", "")
            if not fid or not vid or not vin_val:
                continue
            if fid == FLEET_B and fleet_b is None:
                # Review g78 B W7: the isolation RTS probe targets this vehicle, so a scope
                # leak must find it NOT grounded (409 not_grounded, no write), never return
                # a real grounded fleet-B vehicle to service.
                if not _has_open_grounding(ddb, groundings_table, vid):
                    fleet_b = (vid, vin_val)
            elif fid == FLEET_A and fleet_a is None:
                if not _has_open_grounding(ddb, groundings_table, vid):
                    fleet_a = (vid, vin_val)
        if fleet_a and fleet_b:
            break
        lek = page.get("LastEvaluatedKey")
        if not lek:
            break
        kw["ExclusiveStartKey"] = lek
    if fleet_a is None:
        raise RuntimeError(
            f"no {FLEET_A} vehicle without open grounding in cms-{stage}-storage-vehicles"
        )
    if fleet_b is None:
        raise RuntimeError(
            f"no {FLEET_B} vehicle without open grounding in cms-{stage}-storage-vehicles"
        )
    return fleet_a[0], fleet_a[1], fleet_b[0], fleet_b[1]


def _pick_grounding_collision_row(
    ddb: Any, stage: str, fleet_id: str
) -> "dict[str, str] | None":
    """Scan cms-{stage}-storage-vehicle-groundings for a returned row on *fleet_id*.

    Requires: fleetId == fleet_id AND returnedAt present (never an open row).
    Sorting: non-LIVETEST actionIds first, then by groundedAt ascending.
    For each candidate, checks the vehicle registry to confirm fleetId and VIN.
    Returns {vehicleId, vin, actionId, findingId} or None.
    DynamoDB read ops: Scan, GetItem (already in _ISOLATION_OPS via _PRE_OPS).
    """
    groundings_table = f"cms-{stage}-storage-vehicle-groundings"
    vehicles_table = f"cms-{stage}-storage-vehicles"
    candidates: list[dict] = []
    kw: dict[str, Any] = {"TableName": groundings_table, "ConsistentRead": True}
    while True:
        page = ddb.scan(**kw)
        for raw in page.get("Items", []):
            fid = (raw.get("fleetId") or {}).get("S", "")
            ret = (raw.get("returnedAt") or {}).get("S", "")
            if fid == fleet_id and ret:
                candidates.append(raw)
        lek = page.get("LastEvaluatedKey")
        if not lek:
            break
        kw["ExclusiveStartKey"] = lek

    def _sort_key(raw: dict) -> tuple:
        aid = (raw.get("actionId") or {}).get("S", "")
        gat = (raw.get("groundedAt") or {}).get("S", "")
        return (1 if aid.startswith("LIVETEST") else 0, gat)

    candidates.sort(key=_sort_key)
    for raw in candidates:
        vid = (raw.get("vehicleId") or {}).get("S", "")
        aid = (raw.get("actionId") or {}).get("S", "")
        fid_row = (raw.get("findingId") or {}).get("S", "")
        if not vid or not aid:
            continue
        # Verify vehicle exists in registry with matching fleetId and non-empty VIN
        vresp = ddb.get_item(
            TableName=vehicles_table,
            Key={"vehicleId": {"S": vid}},
            ConsistentRead=True,
        )
        vitem = vresp.get("Item")
        if not vitem:
            continue
        vfleet = (vitem.get("fleetId") or {}).get("S", "")
        vvin = (vitem.get("vin") or {}).get("S", "")
        if vfleet != fleet_id or not vvin:
            continue
        return {"vehicleId": vid, "vin": vvin, "actionId": aid, "findingId": fid_row}
    return None


# ---------------------------------------------------------------------------
# Helpers for grounding-items / ground
# ---------------------------------------------------------------------------


def _discover_avx_stack(cfn: Any, stage: str) -> str:
    """Return AVX API base URL from vsa-{stage}-api CFN stack RestApiUrl output."""
    outs = cfn.describe_stacks(StackName=f"vsa-{stage}-api")["Stacks"][0]["Outputs"]
    return next(o["OutputValue"].rstrip("/") for o in outs if o["OutputKey"] == "RestApiUrl")


def _discover_dms_stack(cfn: Any, stage: str) -> str:
    """Return DMS API base URL from DmsApiStack-{stage} CFN output ApiUrl."""
    outs = cfn.describe_stacks(StackName=f"DmsApiStack-{stage}")["Stacks"][0]["Outputs"]
    return next(o["OutputValue"].rstrip("/") for o in outs if o["OutputKey"] == "ApiUrl")


#: Matches FI recall campaign ids of the form MRD-YYYY-<suffix> (fi_stand_ins.py FI_CAMPAIGN_ID_RE).
_FI_RECALL_ID_RE = re.compile(r"MRD-\d{4}-.+")
#: Lazy-loaded adp_code_labor_op_map.json (agents/fi_fleet_health).
_CODE_LABOR_OP_MAP: "dict | None" = None


def _labor_op_for_code(code: str) -> str:
    """First labor op for *code* from the vendored ADP fault_codes map.

    FACTS: agents/fi_fleet_health/adp_code_labor_op_map.json fault_codes section;
    P0217 → ["LAB-COOLOT-01"] (one op per p0 code by design).
    """
    global _CODE_LABOR_OP_MAP
    if _CODE_LABOR_OP_MAP is None:
        p = Path(_REPO_ROOT) / "agents" / "fi_fleet_health" / "adp_code_labor_op_map.json"
        _CODE_LABOR_OP_MAP = json.loads(p.read_text())
    ops = (_CODE_LABOR_OP_MAP.get("fault_codes") or {}).get(code)
    if ops:
        return ops[0]
    raise RuntimeError(f"no labor op for {code!r} in adp_code_labor_op_map.json")


def is_fi_booking(ro: dict) -> bool:
    """True if this RO would be advanced by the FI dealer-progress stand-in.

    Ports DMS de6c181 source/simulation/fi_stand_ins.py:is_fi_booking:
      fleet-health: avx_action_id + customer_type=fleet + any line with non-empty need_id;
      recall: avx_action_id + customer_type=fleet + recall line with MRD catalog id.
    Both cases require avx_action_id and customer_type==fleet (fi_stand_ins._is_fi_avx_fleet_ro).
    """
    lines = ro.get("lines") or []
    if not (ro.get("avx_action_id") and ro.get("customer_type") == "fleet"):
        return False
    if any(isinstance(ln.get("need_id"), str) and ln["need_id"].strip() for ln in lines):
        return True
    return bool(
        any(
            ln.get("pay_type") == "recall"
            and isinstance(ln.get("recall_campaign_id"), str)
            and _FI_RECALL_ID_RE.match(ln["recall_campaign_id"]) is not None
            for ln in lines
        )
    )


def _first_dealer(ddb: Any, stage: str) -> str:
    """First sorted dealer_id from dms-{stage}-rooftops (all pages, fail-closed)."""
    kw: dict[str, Any] = {"TableName": f"dms-{stage}-rooftops",
                          "ProjectionExpression": "dealer_id"}
    dealers: list[str] = []
    for _ in range(_FAULTS_MAX_PAGES):
        page = ddb.scan(**kw)
        for item in page.get("Items", []):
            d = (item.get("dealer_id") or {}).get("S", "")
            if d:
                dealers.append(d)
        if not page.get("LastEvaluatedKey"):
            break
        kw["ExclusiveStartKey"] = page["LastEvaluatedKey"]
    if not dealers:
        raise RuntimeError(f"dms-{stage}-rooftops: no dealers found")
    return sorted(dealers)[0]


def _find_existing_marker_ro(ddb: Any, stage: str, vin: str, marker: str) -> "str | None":
    """Return ro_id of the first RO on VIN whose description starts with *marker*."""
    kw: dict[str, Any] = {
        "TableName": f"dms-{stage}-repair-orders", "IndexName": "vin-index",
        "KeyConditionExpression": "vehicle_vin = :v",
        "ExpressionAttributeValues": {":v": {"S": vin}},
    }
    for _ in range(_FAULTS_MAX_PAGES):
        page = ddb.query(**kw)
        for item in page.get("Items", []):
            desc = (item.get("description") or {}).get("S", "")
            ro = (item.get("ro_id") or {}).get("S", "")
            if desc.startswith(marker) and ro:
                return ro
        if not page.get("LastEvaluatedKey"):
            return None
        kw["ExclusiveStartKey"] = page["LastEvaluatedKey"]
    raise RuntimeError("dms repair orders (vin-index marker scan): page cap hit")


def _faults_repair_ref(
    cms_api: "Http", token: str, fleet_id: str, vin: str, code: str,
) -> "str | None":
    """Page GET /faults?fleetId=...; return repairRef for (vin, code), or None."""
    page_token: str | None = None
    for _ in range(_FAULTS_MAX_PAGES):
        qs = f"fleetId={urllib.parse.quote(fleet_id, safe='')}&limit={_FAULTS_LIMIT}"
        if page_token:
            qs += f"&pageToken={urllib.parse.quote(page_token, safe='')}"
        status, body = cms_api.call("GET", f"{_FI}/faults?{qs}", token)
        if status != 200:
            raise RuntimeError(
                f"GET /faults fleetId={fleet_id} → {status}: {json.dumps(body)[:80]}")
        body = body if isinstance(body, dict) else {}
        for row in body.get("data") or []:
            if row.get("vin") == vin and row.get("code") == code:
                return row.get("repairRef")
        page_token = body.get("nextPageToken")
        if not page_token:
            return None
    raise RuntimeError(f"GET /faults fleetId={fleet_id}: page cap hit")


def _page_avx_findings(api: "Http", fleet_id: str, token: str) -> list:
    """Page GET /findings/fleet/{fleet_id} and return all items.

    Raises RuntimeError on any non-200 status (fail-closed; caller records FAIL row).
    URL-encodes next_token before appending to the query string.
    """
    items: list = []
    cursor: str | None = None
    while True:
        path = f"/findings/fleet/{fleet_id}?limit={_AVX_FINDINGS_PAGE}"
        if cursor:
            path += f"&next_token={urllib.parse.quote(cursor, safe='')}"
        status, body = api.call("GET", path, token)
        if status != 200:
            raise RuntimeError(
                f"GET /findings/fleet/{fleet_id} → HTTP {status}: "
                f"{json.dumps(body)[:80]}"
            )
        items.extend(body.get("findings") or [])
        cursor = body.get("next_token")
        if not cursor:
            break
        if len(items) > 10_000:
            raise RuntimeError(f"_page_avx_findings: cap hit for fleet {fleet_id}")
    return items


def _read_cms_p0_faults(
    api: "Http", fleet_id: str, token: str
) -> "dict[str, dict]":
    """Page GET /faults; return open p0 faults with no repair record, per VIN.

    ``{vin: {"vehicle_id": str, "fleet_id": str, "codes": set[str]}}``. A vehicle can
    hold several open p0 codes (39 staging vehicles held both P0AA6 and P0A0D,
    2026-10-21), so every code is kept. Pages with limit=500, URL-encodes the
    pageToken, and raises RuntimeError on any non-200 (the caller records a FAIL).
    """
    out: dict[str, dict] = {}
    page_token: str | None = None
    for _ in range(_FAULTS_MAX_PAGES):
        path = f"{_FI}/faults?fleetId={fleet_id}&limit={_FAULTS_LIMIT}"
        if page_token:
            path += f"&pageToken={urllib.parse.quote(page_token, safe='')}"
        status, body = api.call("GET", path, token)
        if status != 200:
            raise RuntimeError(
                f"GET /faults fleetId={fleet_id} → HTTP {status}: "
                f"{json.dumps(body)[:80]}"
            )
        for row in body.get("data") or []:
            if (row.get("severityBand") == "p0" and row.get("status") == "open"
                    and not row.get("repairRef")):
                vin = row.get("vin")
                code = row.get("code")
                if vin and code:
                    slot = out.setdefault(vin, {
                        "vehicle_id": row.get("vehicleId") or "",
                        "fleet_id": row.get("fleetId") or fleet_id,
                        "codes": set(),
                    })
                    slot["codes"].add(code)
        page_token = body.get("nextPageToken")
        if not page_token:
            break
    else:
        raise RuntimeError(
            f"GET /faults fleetId={fleet_id} exceeded {_FAULTS_MAX_PAGES} pages"
        )
    return out


# ---------------------------------------------------------------------------
# pre part
# ---------------------------------------------------------------------------


def run_pre(
    stage: str,
    run_id: str,
    manifest: Path,
    res: Results,
    cfn: Any,
    cw: Any,
    lam: Any,
    ddb: Any,
) -> None:
    """pre: stage/identity/windows; alarm checks; plan count; manifest."""
    print("\n--- pre ---", flush=True)
    now = _now()
    res.note("stage", stage)
    res.note("run_id", run_id)
    res.note("utc", now.isoformat())

    # Alarm: cms-{stage}-ui-fi-health-views-stale must be OK
    alarm_name = f"cms-{stage}-ui-fi-health-views-stale"
    try:
        alarms = cw.describe_alarms(AlarmNames=[alarm_name], AlarmTypes=["MetricAlarm"])
        state = (alarms.get("MetricAlarms") or [{}])[0].get("StateValue", "MISSING")
        res.check(f"alarm {alarm_name}", state == "OK", f"state={state}")
    except Exception as exc:  # noqa: BLE001
        res.check(f"alarm {alarm_name}", False, f"describe_alarms error: {exc}")

    # FAULT_GROUNDING_ENABLED from the tier-2 health agent Lambda
    # Records 'true', 'false', or 'unreadable' (not a bool string).
    fn_name = f"vsa-{stage}-tier2-health-agent-{_REGION}"
    grounding_enabled: str = "unreadable"
    try:
        cfg = lam.get_function_configuration(FunctionName=fn_name)
        raw = (cfg.get("Environment") or {}).get("Variables", {}).get(
            "FAULT_GROUNDING_ENABLED", ""
        )
        grounding_enabled = "true" if raw.lower() == "true" else "false"
    except Exception:  # noqa: BLE001
        pass
    res.note("FAULT_GROUNDING_ENABLED", grounding_enabled)

    # Migration plan count (a NOTE): compare with the migration dry run's `planned` total,
    # which changes as cars change (36 on 2026-10-23: one EV oil change skipped).
    pm_table = f"cms-{stage}-storage-pm-plans"
    try:
        kw: dict[str, Any] = {
            "TableName": pm_table,
            "Select": "COUNT",
            "FilterExpression": "begins_with(planId, :pfx)",
            "ExpressionAttributeValues": {":pfx": {"S": "plan-mig-"}},
        }
        count = 0
        while True:
            page = ddb.scan(**kw)
            count += page.get("Count", 0)
            lek = page.get("LastEvaluatedKey")
            if not lek:
                break
            kw["ExclusiveStartKey"] = lek
    except Exception as exc:  # noqa: BLE001
        count = -1
        res.check("pm-plans: migration count readable", False, str(exc))
    if count >= 0:
        res.note(
            "pm-plans: plan-mig-* count",
            f"{count} (compare with the migration dry run's planned total)",
        )

    update_manifest(manifest, "pre", {
        "completed_at": _now().isoformat(),
        "alarm_state": state if "state" in dir() else "not_checked",
        "fault_grounding_enabled": grounding_enabled,
        "pm_mig_count": count if count >= 0 else "error",
    })
    print(f"manifest: {manifest}", flush=True)

# ---------------------------------------------------------------------------
# perf part
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
    """perf: p95 of GET /faults over 20 requests < 3.0 s (temp fleet-operator)."""
    print("\n--- perf ---", flush=True)
    _refuse = require_write_window(scheduler, stage, _now())
    if _refuse is not None:
        res.skip("perf: write window",
                 f"{_refuse} — resume: --part perf --run-id {run_id}")
        return
    with principal(
        idp, pool_id, client_id, run_id, "perf-op",
        manifest, res,
        groups=["fleet-operator"],
        fleet_ids=_ALL_FLEETS,
    ) as (token, _):
        latencies: list[float] = []
        fleet_totals: dict[str, int] = {}
        bad_requests: list[str] = []
        for i in range(_PERF_REQUESTS):
            fleet_id = _ALL_FLEETS_LIST[i % len(_ALL_FLEETS_LIST)]
            t0 = time.monotonic()
            status, body = api.call("GET", f"{_FI}/faults?fleetId={fleet_id}", token)
            elapsed = time.monotonic() - t0
            if status != 200:
                bad_requests.append(f"{fleet_id} status={status}")
                continue
            if (not isinstance(body, dict)
                    or not isinstance(body.get("data"), list)
                    or "total" not in body
                    or "unavailableReason" in body):
                bad_requests.append(f"{fleet_id} bad body (missing data/total or unavailableReason)")
                continue
            latencies.append(elapsed)
            # review c2 S11: a non-integer total is a bad body, never a crash.
            _tot = body.get("total")
            if isinstance(_tot, bool) or not isinstance(_tot, int):
                bad_requests.append(f"{fleet_id} bad body (total={_tot!r} is not an integer)")
                continue
            if fleet_id not in fleet_totals:
                fleet_totals[fleet_id] = _tot
        if bad_requests:
            res.check(
                f"perf /faults p95 < {_PERF_P95_S}s",
                False,
                f"non-200 or unavailable responses: {bad_requests[:5]}",
            )
            p95 = -1.0
            ok = False
        else:
            latencies.sort()
            p95_idx = int(len(latencies) * 0.95) - 1
            p95 = latencies[max(p95_idx, 0)]
            ok = p95 < _PERF_P95_S
            totals_str = " ".join(f"{f}={fleet_totals.get(f, '?')}" for f in _ALL_FLEETS_LIST)
            res.check(
                f"perf /faults p95 < {_PERF_P95_S}s",
                ok,
                f"p95={p95:.3f}s (n={len(latencies)}) {totals_str}",
            )
        # Also record /faults/summary (no gate)
        summary_status, _ = api.call("GET", f"{_FI}/faults/summary?fleetId={FLEET_A}", token)
        res.note("/faults/summary status", str(summary_status))
        update_manifest(manifest, "perf", {
            "p95_s": round(p95, 4),
            "n_requests": len(latencies),
            "fleet_totals": fleet_totals,
            "passed": ok,
        })


# ---------------------------------------------------------------------------
# isolation part
# ---------------------------------------------------------------------------


def _check_probe_403(
    res: Results,
    label: str,
    status: int,
    body: Any,
) -> None:
    """Assert probe returned 403 with the fleet-scope error message."""
    ok_status = status == 403
    ok_body = isinstance(body, dict) and _403_ERR in body.get("error", "")
    res.check(f"isolation probe {label}: 403", ok_status and ok_body,
              f"got {status} {json.dumps(body)[:120]}")


def _check_control(
    res: Results,
    label: str,
    status: int,
    body: Any,
    expected_status: int,
    expected_key: str | None = None,
    expected_val: str | None = None,
) -> None:
    """Assert control returned expected_status (and optionally expected body key/val)."""
    ok = status == expected_status
    if ok and expected_key and expected_val:
        ok = isinstance(body, dict) and expected_val in str(body.get(expected_key, ""))
    res.check(
        f"isolation control {label}: {expected_status}",
        ok,
        f"got {status} {json.dumps(body)[:100]}" if not ok else "",
    )

def run_isolation(
    stage: str,
    run_id: str,
    manifest: Path,
    res: Results,
    api: Http,
    idp: Any,
    pool_id: str,
    client_id: str,
    ddb: Any,
    scheduler: Any,
) -> None:
    """isolation: fleet-A operator; probe fleet-B → 403; control fleet-A → expected."""
    print("\n--- isolation ---", flush=True)
    _refuse = require_write_window(scheduler, stage, _now())
    if _refuse is not None:
        res.skip("isolation: write window",
                 f"{_refuse} — resume: --part isolation --run-id {run_id}")
        return

    # Discover fleet VINs from the vehicles table
    try:
        fleet_a_vid, fleet_a_vin, fleet_b_vid, fleet_b_vin = _discover_fleet_vins(ddb, stage)
    except RuntimeError as exc:
        res.check("isolation: discover fleet vins", False, str(exc))
        update_manifest(manifest, "isolation", {"error": str(exc)})
        return
    update_manifest(manifest, "isolation_vins", {
        "fleet_a_vid": fleet_a_vid, "fleet_a_vin": fleet_a_vin,
        "fleet_b_vid": fleet_b_vid, "fleet_b_vin": fleet_b_vin,
    })

    # Verify fleet-B vehicle exists in registry (so its probe 403 is scope, not missing)
    vehicles_table = f"cms-{stage}-storage-vehicles"
    vresp = ddb.get_item(
        TableName=vehicles_table,
        Key={"vehicleId": {"S": fleet_b_vid}},
        ConsistentRead=True,
    )
    res.check(
        "isolation pre: fleet-B vehicle in registry",
        vresp.get("Item") is not None,
        f"fleet-B vehicleId={fleet_b_vid}",
    )

    with principal(
        idp, pool_id, client_id, run_id, "iso-op",
        manifest, res,
        groups=["fleet-operator"],
        fleet_ids=FLEET_A,
    ) as (token, _):
        # -- GET routes -------------------------------------------------------
        for route, probe_qs, control_qs in [
            ("/faults",         f"fleetId={FLEET_B}",  f"fleetId={FLEET_A}"),
            ("/faults/summary", f"fleetId={FLEET_B}",  f"fleetId={FLEET_A}"),
            ("/health-findings", f"fleetId={FLEET_B}", f"fleetId={FLEET_A}"),
            ("/pm/compliance",  f"fleetId={FLEET_B}",  f"fleetId={FLEET_A}"),
            ("/pm/schedules",   f"fleetId={FLEET_B}",  f"fleetId={FLEET_A}"),
            ("/pm/plans",       f"fleetId={FLEET_B}",  f"fleetId={FLEET_A}"),
        ]:
            s, b = api.call("GET", f"{_FI}{route}?{probe_qs}", token)
            _check_probe_403(res, f"GET {route}", s, b)
            s, b = api.call("GET", f"{_FI}{route}?{control_qs}", token)
            _check_control(res, f"GET {route}", s, b, 200)

        # -- GET /vehicles/{vin}/faults (fleet resolved from registry) --------
        s, b = api.call("GET", f"{_FI}/vehicles/{fleet_b_vin}/faults", token)
        _check_probe_403(res, "GET /vehicles/{vin}/faults", s, b)
        s, b = api.call("GET", f"{_FI}/vehicles/{fleet_a_vin}/faults", token)
        _check_control(res, "GET /vehicles/{vin}/faults", s, b, 200)

        # -- POST /pm/plans ---------------------------------------------------
        # probe: fleet-B, invalid taskCode → scope refuses before taskCode check; if scope
        # leaks the 400 from taskCode validation is still no write (W7)
        body_b = {"fleetId": FLEET_B, "taskCode": "NOT_A_TASK", "basis": "calendar",
                  "intervalValue": 6}
        s, b = api.call("POST", f"{_FI}/pm/plans", token, body_b)
        _check_probe_403(res, "POST /pm/plans", s, b)
        # control: fleet-A, invalid taskCode → 400 "taskCode is not a PM task" (after scope)
        body_ctrl = {"fleetId": FLEET_A, "taskCode": "NOT_A_TASK", "basis": "calendar",
                     "intervalValue": 6}
        s, b = api.call("POST", f"{_FI}/pm/plans", token, body_ctrl)
        _check_control(res, "POST /pm/plans", s, b, 400, "error", "taskCode is not a PM task")

        # -- PUT /pm/plans/{planId} -------------------------------------------
        s, b = api.call("PUT", f"{_FI}/pm/plans/plan-test-001?fleetId={FLEET_B}", token,
                        {"intervalValue": 12})
        _check_probe_403(res, "PUT /pm/plans/{planId}", s, b)
        # control: fleet-A, empty update body → 400 "Nothing to change" (after scope)
        s, b = api.call("PUT", f"{_FI}/pm/plans/plan-test-001?fleetId={FLEET_A}", token,
                        {"fleetId": FLEET_A})
        _check_control(res, "PUT /pm/plans/{planId}", s, b, 400, "error", "Nothing to change")

        # -- POST /pm/plans/{planId}/complete ---------------------------------
        body_b = {"fleetId": FLEET_B, "vehicleId": fleet_b_vid,
                  "completedDate": "2026-01-01"}
        s, b = api.call("POST", f"{_FI}/pm/plans/plan-test-001/complete", token, body_b)
        _check_probe_403(res, "POST /pm/plans/{planId}/complete", s, b)
        # control: fleet-A, missing vehicleId → 400 "vehicleId is required" (after scope)
        body_ctrl = {"fleetId": FLEET_A, "completedDate": "2026-01-01"}
        s, b = api.call("POST", f"{_FI}/pm/plans/plan-test-001/complete", token, body_ctrl)
        _check_control(res, "POST /pm/plans/{planId}/complete", s, b, 400,
                       "error", "vehicleId is required")

        # -- POST /groundings -------------------------------------------------
        # B-3 N1 fix: discover a returned fleet-A grounding row at runtime.
        # Body uses that row's (actionId) with a mismatched findingId → conditional put
        # fails → 409, no write.  If no returned row exists, skip both requests.
        # FACTS: rows discovered at run time; no pre-seeded (vehicleId, actionId) row needed.
        groundings_table = f"cms-{stage}-storage-vehicle-groundings"
        coll = _pick_grounding_collision_row(ddb, stage, FLEET_A)
        if coll is None:
            res.skip(
                "isolation POST /groundings pair",
                "no returned grounding on a fleet-A vehicle; "
                "a body that could create a row is never sent",
            )
        else:
            mismatch_finding_id = f"LIVETEST-{run_id}-iso-mismatch"
            # Record BEFORE the first POST so cleanup can verify no residue (step 6)
            update_manifest(manifest, "isolation_grounding_collision", {
                "vehicle_id": coll["vehicleId"],
                "vin": coll["vin"],
                "action_id": coll["actionId"],
                "mismatch_finding_id": mismatch_finding_id,
            })
            body_iso = {
                "vin": coll["vin"],
                "faultCodes": ["P0420"],
                "actionId": coll["actionId"],
                "findingId": mismatch_finding_id,  # differs from stored row → 409, no write
            }

            def _reread_ok(label: str) -> bool:
                """GetItem the collision row; FAIL and return False if gone or findingId changed."""
                rr = ddb.get_item(
                    TableName=groundings_table,
                    Key={"vehicleId": {"S": coll["vehicleId"]},
                         "actionId": {"S": coll["actionId"]}},
                    ConsistentRead=True,
                )
                rr_item = rr.get("Item")
                if not rr_item:
                    res.check(f"isolation {label} pre-read", False,
                              "collision row gone before POST — skipping")
                    return False
                rr_fid = (rr_item.get("findingId") or {}).get("S", "")
                if rr_fid != coll["findingId"]:
                    res.check(f"isolation {label} pre-read", False,
                              f"collision row findingId changed to {rr_fid!r} — skipping")
                    return False
                return True

            _posts_sent = False
            if _reread_ok("control POST /groundings"):
                # control: fleet-A operator posts body_iso → 409 after scope (conditional put
                # fails on existing (vehicleId, actionId); proves route reached authorize_fleet_write)
                s, b = api.call("POST", f"{_FI}/groundings", token, body_iso)
                # Architect (c2 review fix): the residue query must run once ANY POST was
                # sent, not only when the probe was sent too.
                _posts_sent = True
                if s in (200, 201):
                    vid_k = coll["vehicleId"]
                    aid_k = coll["actionId"]
                    res.check(
                        "isolation control POST /groundings", False,
                        f"got {s} — row written; "
                        f"aws dynamodb delete-item --table-name {groundings_table} "
                        f"--key '{{\"vehicleId\":{{\"S\":\"{vid_k}\"}},\"actionId\":{{\"S\":\"{aid_k}\"}}}}' "
                        f"--region {_REGION}",
                    )
                else:
                    _check_control(res, "POST /groundings", s, b, 409, "error",
                                   "grounding already recorded")
                # probe: iso-op-b (fleet-B operator) posts the same body → scope refuses → 403
                # (fleet-A vin resolves to fleet-A; iso-op-b's custom:fleetIds: fleet-B only)
                # If scope leaks: conditional put fails on existing row → 409; no write occurs
                if _reread_ok("probe POST /groundings"):
                    with principal(
                        idp, pool_id, client_id, run_id, "iso-op-b",
                        manifest, res,
                        groups=["fleet-operator"],
                        fleet_ids=FLEET_B,
                    ) as (token_b, _):
                        s_p, b_p = api.call("POST", f"{_FI}/groundings", token_b, body_iso)
                    _check_probe_403(res, "POST /groundings", s_p, b_p)
                    _posts_sent = True

            # Post-POST: Query vehicle's partition; FAIL if mismatch findingId appears or
            # the collision row's findingId changed (both mean a write occurred)
            if _posts_sent:
                kw_q: dict[str, Any] = {
                    "TableName": groundings_table,
                    "KeyConditionExpression": "vehicleId = :v",
                    "ExpressionAttributeValues": {":v": {"S": coll["vehicleId"]}},
                    "ConsistentRead": True,
                }
                all_g_rows: list[dict] = []
                while True:
                    pg = ddb.query(**kw_q)
                    all_g_rows.extend(pg.get("Items", []))
                    if not pg.get("LastEvaluatedKey"):
                        break
                    kw_q["ExclusiveStartKey"] = pg["LastEvaluatedKey"]
                for gr in all_g_rows:
                    gr_fid = (gr.get("findingId") or {}).get("S", "")
                    gr_aid = (gr.get("actionId") or {}).get("S", "")
                    if gr_fid == mismatch_finding_id:
                        res.check(
                            "isolation POST /groundings residue check", False,
                            f"mismatch findingId found (vehicleId={coll['vehicleId']} "
                            f"actionId={gr_aid}); "
                            f"aws dynamodb delete-item --table-name {groundings_table} "
                            f"--key '{{\"vehicleId\":{{\"S\":\"{coll['vehicleId']}\"}},\"actionId\":{{\"S\":\"{gr_aid}\"}}}}' "
                            f"--region {_REGION}",
                        )
                    elif gr_aid == coll["actionId"]:
                        if gr_fid != coll["findingId"]:
                            res.check("isolation POST /groundings collision row changed", False,
                                      f"findingId changed: {coll['findingId']!r} → {gr_fid!r}")

        # -- POST /groundings/{vehicleId}/return-to-service -------------------
        # probe: fleet-B vehicleId → handler resolves fleet-B → authorize_fleet_write → 403
        s, b = api.call("POST", f"{_FI}/groundings/{fleet_b_vid}/return-to-service",
                        token, {})
        _check_probe_403(res, "POST /groundings/rts", s, b)
        # control: re-check fleet-A vehicle for open grounding before issuing RTS
        # (prevents issuing a real return-to-service on a live grounded vehicle)
        if _has_open_grounding(ddb, groundings_table, fleet_a_vid):
            res.check(
                "isolation control POST /groundings/rts",
                False,
                f"fleet-A {fleet_a_vid} has open grounding — skip",
            )
        else:
            s, b = api.call("POST", f"{_FI}/groundings/{fleet_a_vid}/return-to-service",
                            token, {})
            _check_control(res, "POST /groundings/rts", s, b, 409, "reason", "not_grounded")

    update_manifest(manifest, "isolation", {"completed_at": _now().isoformat()})

# ---------------------------------------------------------------------------
# grounding-items part
# ---------------------------------------------------------------------------


def _load_fault_catalog_s3(s3: Any, bucket: str) -> "dict[str, dict]":
    """Read config/fleet_health/fault_codes/fault_codes.parquet from the ADP lake.

    Returns ``{code: {code, band, directive_text}}``.
    Uses duckdb; raises on any error (caller wraps in try/except).
    S3 op: GetObject only (already in _GROUNDING_ITEMS_OPS).
    """
    import os as _os  # noqa: PLC0415
    import tempfile   # noqa: PLC0415
    import duckdb     # noqa: PLC0415

    key = "config/fleet_health/fault_codes/fault_codes.parquet"
    resp = s3.get_object(Bucket=bucket, Key=key)
    body = resp["Body"].read()

    with tempfile.NamedTemporaryFile(suffix=".parquet", delete=False) as tmp:
        tmp.write(body)
        tmp_path = tmp.name
    try:
        conn = duckdb.connect()
        rows = conn.execute(
            f"SELECT code, band, directive_text FROM read_parquet('{tmp_path}')"
        ).fetchall()
    finally:
        _os.unlink(tmp_path)

    catalog: dict[str, dict] = {}
    for (code, band, directive_text) in rows:
        if not code:
            continue
        catalog[str(code).strip()] = {
            "code": str(code).strip(),
            "band": str(band or "").strip(),
            "directive_text": directive_text,
        }
    return catalog


def _health_agent_switch(lam: Any, stage: str) -> "tuple[str, datetime.datetime | None]":
    """``(value, last_modified)`` of FAULT_GROUNDING_ENABLED on the health agent.

    value is ``"true"``, ``"false"`` or ``"unreadable"``; last_modified is the config's
    LastModified (the deploy that set the current value), or None when unreadable.
    """
    fn_name = f"vsa-{stage}-tier2-health-agent-{_REGION}"
    try:
        cfg = lam.get_function_configuration(FunctionName=fn_name)
    except Exception:  # noqa: BLE001
        return "unreadable", None
    raw = ((cfg.get("Environment") or {}).get("Variables") or {}).get("FAULT_GROUNDING_ENABLED")
    value = "true" if raw == "true" else ("false" if raw == "false" else "unreadable")
    # Lambda answers "2026-10-21T16:57:58.000+0000"; moto "…T22:13:35.392901000Z". Seconds
    # precision is enough: it is compared with run records' started_at.
    m = re.fullmatch(r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.\d+)?(Z|[+-]\d{4})",
                     str(cfg.get("LastModified") or ""))
    if not m:
        return "unreadable", None
    tz = "+0000" if m.group(2) == "Z" else m.group(2)
    lm = datetime.datetime.strptime(m.group(1) + tz, "%Y-%m-%dT%H:%M:%S%z")
    return value, lm.astimezone(datetime.timezone.utc)


def _newest_completed_service_run(ddb: Any, stage: str, since: datetime.datetime) -> "dict | None":
    """Newest ``completed`` service run record started at or after *since*, or None.

    Pages newest first (``vsa-{stage}-avx-agent-runs``, pk ``agent-fi-fleet-health#service``,
    sk ``started_at``). Raises on a read error (the caller records a FAIL).
    """
    kw: dict[str, Any] = {
        "TableName": f"vsa-{stage}-avx-agent-runs",
        "KeyConditionExpression": "agent_component = :pk AND started_at >= :since",
        "ExpressionAttributeValues": {
            ":pk": {"S": "agent-fi-fleet-health#service"},
            ":since": {"S": since.strftime("%Y-%m-%dT%H:%M:%S")},
        },
        "ScanIndexForward": False,
        "ConsistentRead": True,
    }
    for _ in range(_FAULTS_MAX_PAGES):
        page = ddb.query(**kw)
        for item in page.get("Items", []):
            if (item.get("status") or {}).get("S") == "completed":
                return item
        if not page.get("LastEvaluatedKey"):
            return None
        kw["ExclusiveStartKey"] = page["LastEvaluatedKey"]
    raise RuntimeError("run records: page cap hit")


def _live_ground_findings(findings: list, now_iso: str) -> "dict[str, list]":
    """``{vin: [Finding, ...]}`` for unexpired fleet-health ``ground/<n>`` Findings."""
    out: dict[str, list] = {}
    for f in findings:
        if f.get("finding_kind") != "fleet.health.service":
            continue
        if not (f.get("subject_key") or "").startswith("ground/"):
            continue
        exp = f.get("expires_at")
        if isinstance(exp, str) and exp and exp <= now_iso:
            continue
        vin = (f.get("scope") or {}).get("vin")
        if vin:
            out.setdefault(vin, []).append(f)
    return out


def run_grounding_items(
    stage: str, run_id: str, manifest: Any, res: "Results",
    cms_api: "Http", avx_api: "Http",
    idp: Any, pool_id: str, client_id: str, s3: Any, lake_bucket: str,
    lam: Any, ddb: Any, scheduler: Any,
) -> None:
    """grounding-items: the grounding items the service run raised once the switch is on.

    Per unexpired ``ground/<n>`` Finding (AVX ``agents/fi_fleet_health/writer.py``
    ``build_grounding_item``): one per VIN; ``ground_vehicle``; ``p0``; in the VIN's fleet;
    ``disclosure_txt`` byte for byte the catalog directive of its primary code
    (``parameters.fault_codes[0]``); every code it names is an open p0 fault on the
    vehicle with no repair record. No ground Finding on a vehicle without one.

    Coverage: the service run takes the 500 most severe vehicles a night, ties rotated by
    a date-salted hash (AVX ``agents/tier2/runner.py`` ``_severity_key``), so with more p0
    vehicles than the cap some get their item on a later night. All covered → PASS;
    gaps while the newest completed run since the switch reports ``vehicles_over_cap`` > 0
    and fewer than 7 days have passed → SKIP; any other gap → FAIL.
    """
    print("\n--- grounding-items ---", flush=True)
    _refuse = require_write_window(scheduler, stage, _now())
    if _refuse is not None:
        res.skip("grounding-items: write window",
                 f"{_refuse} — resume: --part grounding-items --run-id {run_id}")
        return
    switch, switched_at = _health_agent_switch(lam, stage)
    res.note("grounding-items: FAULT_GROUNDING_ENABLED", switch)
    if switch != "true":
        if switch == "unreadable":
            res.check("grounding-items: health agent config readable", False,
                      "", on_fail="FAULT_GROUNDING_ENABLED or LastModified unreadable")
        else:
            res.skip("grounding-items", "FAULT_GROUNDING_ENABLED is 'false' on the health agent")
        return
    try:
        catalog = _load_fault_catalog_s3(s3, lake_bucket)
    except Exception as exc:  # noqa: BLE001
        res.check("grounding-items: fault catalog readable", False, "",
                  on_fail=f"{type(exc).__name__}: {exc}")
        return

    now_iso = _now().strftime("%Y-%m-%dT%H:%M:%SZ")
    with principal(idp, pool_id, client_id, run_id, "gi-op", manifest, res,
                   groups=["fleet-operator"], fleet_ids=_ALL_FLEETS) as (token, _):
        p0: dict[str, dict] = {}
        ground: dict[str, list] = {}
        partition: dict[str, set] = {}
        for fid in _ALL_FLEETS_LIST:
            try:
                p0.update(_read_cms_p0_faults(cms_api, fid, token))
                findings = _page_avx_findings(avx_api, fid, token)
            except RuntimeError as exc:
                res.check(f"grounding-items: read fleet {fid}", False, "", on_fail=str(exc))
                return
            for vin, fl in _live_ground_findings(findings, now_iso).items():
                ground.setdefault(vin, []).extend(fl)
                partition.setdefault(vin, set()).add(fid)
    if not p0:
        res.check("grounding-items: open p0 faults present", False, "",
                  on_fail="no open p0 fault in any fleet; nothing to check")
        return

    bad = 0
    for vin in sorted(ground):
        fl = ground[vin]
        problems: list[str] = []
        if len(fl) != 1:
            problems.append(f"{len(fl)} ground Findings")
        f = fl[0]
        rec = f.get("recommended_action") or {}
        codes = list((rec.get("parameters") or {}).get("fault_codes") or [])
        open_codes = (p0.get(vin) or {}).get("codes") or set()
        if vin not in p0:
            problems.append("no open p0 fault without a repair record")
        if rec.get("action_kind") != "ground_vehicle":
            problems.append(f"action_kind={rec.get('action_kind')!r}")
        if f.get("severity") != "p0":
            problems.append(f"severity={f.get('severity')!r}")
        if vin in p0 and partition.get(vin) != {p0[vin]["fleet_id"]}:
            problems.append(f"fleet {sorted(partition.get(vin) or [])} != {p0[vin]['fleet_id']}")
        if not codes:
            problems.append("no fault_codes")
        elif not set(codes) <= open_codes:
            problems.append(f"codes {sorted(set(codes) - open_codes)} not open p0")
        entry = catalog.get(codes[0]) if codes else None
        if not entry or entry.get("band") != "p0":
            problems.append(f"primary code {codes[:1]} not p0 in the catalog")
        elif f.get("disclosure_txt") != entry.get("directive_text"):
            problems.append("disclosure_txt != catalog directive")
        if problems:
            bad += 1
            res.check(f"grounding-items {vin}", False, "", on_fail="; ".join(problems))
    res.check("grounding-items: every ground Finding well formed", bad == 0,
              f"{len(ground)} ground Findings checked",
              on_fail=f"{bad} of {len(ground)} ground Findings wrong (rows above)")

    missing = sorted(set(p0) - set(ground))
    update_manifest(manifest, "grounding_items", {
        "completed_at": _now().isoformat(), "p0_vehicles": len(p0),
        "ground_findings": len(ground), "missing": len(missing),
        "switched_at": switched_at.isoformat() if switched_at else None,
    })
    if not missing:
        res.check("grounding-items: every open p0 vehicle has a ground Finding", True,
                  f"{len(p0)} of {len(p0)}")
        return
    try:
        run = _newest_completed_service_run(ddb, stage, switched_at)
    except Exception as exc:  # noqa: BLE001
        res.check("grounding-items: service run records readable", False, "",
                  on_fail=f"{type(exc).__name__}: {exc}")
        return
    over_cap = int(((run or {}).get("vehicles_over_cap") or {}).get("N") or 0)
    days = (_now() - switched_at).total_seconds() / 86400
    detail = (f"{len(p0) - len(missing)} of {len(p0)} covered; vehicles_over_cap={over_cap}; "
              f"{days:.1f} days since the switch; first gaps {missing[:10]}")
    if run is not None and over_cap > 0 and days < 7:
        res.skip("grounding-items: coverage", detail + "; re-run after the next 05:40Z")
    else:
        res.check("grounding-items: every open p0 vehicle has a ground Finding", False, "",
                  on_fail=detail + ("" if run else "; no completed service run since the switch"))


# ---------------------------------------------------------------------------
# ground part (sub-steps 1-3)
# ---------------------------------------------------------------------------


#: Fleets in the order the ground target is picked from: field service and delivery first,
#: the rental fleet (stage E moves its cars between locations) last.
_GROUND_FLEET_ORDER = ["demo-field-service", "demo-lastmile-delivery", "demo-city-public",
                       FLEET_A]
#: DMS de6c181 source/handlers/repair_orders.py:193 RO_ACTIVE_STATUSES.
_DMS_ACTIVE_STATUSES = frozenset({"Draft", "InProgress", "AwaitingParts"})


def _live_tier_vehicle_ids(ddb: Any, stage: str) -> set:
    """vehicleIds named by any row of ``cms-{stage}-storage-live-plan`` (the live roster)."""
    out: set = set()
    kw: dict[str, Any] = {"TableName": f"cms-{stage}-storage-live-plan",
                          "ProjectionExpression": "vehicleId"}
    for _ in range(_FAULTS_MAX_PAGES):
        page = ddb.scan(**kw)
        for item in page.get("Items", []):
            vid = (item.get("vehicleId") or {}).get("S")
            if vid:
                out.add(vid)
        if not page.get("LastEvaluatedKey"):
            return out
        kw["ExclusiveStartKey"] = page["LastEvaluatedKey"]
    raise RuntimeError("live-plan: page cap hit")


def _open_dms_ros(ddb: Any, stage: str, vin: str) -> list:
    """ro_ids of the VIN's DMS repair orders in an active status (DMS RO_ACTIVE_STATUSES)."""
    kw: dict[str, Any] = {
        "TableName": f"dms-{stage}-repair-orders", "IndexName": "vin-index",
        "KeyConditionExpression": "vehicle_vin = :v",
        "ExpressionAttributeValues": {":v": {"S": vin}},
    }
    out: list = []
    for _ in range(_FAULTS_MAX_PAGES):
        page = ddb.query(**kw)
        for item in page.get("Items", []):
            status = (item.get("status") or {}).get("S", "")
            if status in _DMS_ACTIVE_STATUSES:
                out.append((item.get("ro_id") or {}).get("S", "?"))
        if not page.get("LastEvaluatedKey"):
            return out
        kw["ExclusiveStartKey"] = page["LastEvaluatedKey"]
    raise RuntimeError("dms repair orders: page cap hit")


def _pick_ground_target(
    cms_api: "Http", avx_api: "Http", token: str, stage: str, ddb: Any,
) -> "tuple[dict | None, list]":
    """Pick the ground target and say why each earlier candidate was skipped.

    A managed vehicle with an open p0 fault and no repair record, exactly one unexpired
    ``ground/<n>`` Finding whose codes are all open, no decision Action on that Finding,
    no open CMS grounding, not on the live-tier roster, and no open DMS repair order.
    Fleets in ``_GROUND_FLEET_ORDER``, VINs sorted. Read errors raise RuntimeError.
    Returns ``(target or None, skipped)``.
    """
    from agents.supervisor.tools.fleet_recommendation import (  # noqa: PLC0415
        _derive_decision_action_id,
    )
    now_iso = _now().strftime("%Y-%m-%dT%H:%M:%SZ")
    live = _live_tier_vehicle_ids(ddb, stage)
    gt = f"cms-{stage}-storage-vehicle-groundings"
    skipped: list = []
    for fid in _GROUND_FLEET_ORDER:
        p0 = _read_cms_p0_faults(cms_api, fid, token)
        ground = _live_ground_findings(_page_avx_findings(avx_api, fid, token), now_iso)
        for vin in sorted(p0):
            info = p0[vin]
            fl = ground.get(vin) or []
            if len(fl) != 1:
                skipped.append((vin, f"{len(fl)} ground Findings"))
                continue
            f = fl[0]
            codes = list(((f.get("recommended_action") or {}).get("parameters") or {})
                         .get("fault_codes") or [])
            if not codes or not set(codes) <= info["codes"]:
                skipped.append((vin, "Finding codes not all open"))
                continue
            vid = info["vehicle_id"]
            if not vid or vid in live:
                skipped.append((vin, "no vehicleId" if not vid else "on the live-tier roster"))
                continue
            act = ddb.get_item(TableName=f"vsa-{stage}-avx-actions", ConsistentRead=True,
                               Key={"action_id": {"S": _derive_decision_action_id(f["finding_id"])}})
            if act.get("Item"):
                skipped.append((vin, "ground Finding already decided"))
                continue
            if _has_open_grounding(ddb, gt, vid):
                skipped.append((vin, "open CMS grounding"))
                continue
            open_ros = _open_dms_ros(ddb, stage, vin)
            if open_ros:
                skipped.append((vin, f"open DMS RO {open_ros[:3]}"))
                continue
            return ({"vin": vin, "vehicle_id": vid, "fleet_id": info["fleet_id"],
                     "ground_finding_id": f["finding_id"], "fault_codes": codes,
                     "recommended_action": f.get("recommended_action")}, skipped)
    return None, skipped


def _open_grounding_rows(ddb: Any, stage: str, vid: str) -> list:
    """The vehicle's open grounding rows (no returnedAt), consistent read, all pages."""
    kw: dict[str, Any] = {
        "TableName": f"cms-{stage}-storage-vehicle-groundings",
        "KeyConditionExpression": "vehicleId = :vid",
        "ExpressionAttributeValues": {":vid": {"S": vid}}, "ConsistentRead": True,
    }
    rows: list = []
    for _ in range(_FAULTS_MAX_PAGES):
        page = ddb.query(**kw)
        rows.extend(r for r in page.get("Items", []) if "returnedAt" not in r)
        if not page.get("LastEvaluatedKey"):
            return rows
        kw["ExclusiveStartKey"] = page["LastEvaluatedKey"]
    raise RuntimeError("groundings: page cap hit")


def _rts_refused(res: "Results", label: str, status: int, body: Any, codes: list) -> bool:
    """Record one return-to-service attempt that must be refused fault_not_repaired."""
    body = body if isinstance(body, dict) else {}
    named = set(body.get("fault_codes") or [])
    ok = (status == 409 and body.get("reason") == "fault_not_repaired"
          and set(codes) <= named)
    return res.check(label, ok, f"409 fault_not_repaired naming {sorted(named)}",
                     on_fail=f"got {status} {json.dumps(body)[:120]}")


def _dtc_write_guard(table: str, allowed_keys: list) -> Any:
    """``before-parameter-build`` handler: DynamoDB PutItem/DeleteItem only on the
    dtc-history table and only for a key this run recorded in its manifest first."""
    def _guard(params: dict, **_kw: Any) -> None:
        key = params.get("Key") or {
            k: (params.get("Item") or {}).get(k) for k in ("vehicleId", "timestamp")}
        if params.get("TableName") != table or key not in allowed_keys:
            raise GuardViolation(
                f"DynamoDB write refused: table={params.get('TableName')!r} key={key!r}")
    return _guard


def _remove_dtc_row(ddb: Any, res: "Results", table: str, key: dict, marker: str) -> None:
    """Delete this run's LIVETEST dtc-history row and confirm it is gone (signals held)."""
    from botocore.exceptions import ClientError  # noqa: PLC0415
    with _defer_signals_for_restore():
        try:
            ddb.delete_item(TableName=table, Key=key,
                            ConditionExpression="clearedBy = :m",
                            ExpressionAttributeValues={":m": {"S": marker}})
        except ClientError as exc:
            if exc.response["Error"]["Code"] != "ConditionalCheckFailedException":
                res.mark_cleanup_failed()
                res.check("ground S3: dtc-history row deleted", False, "",
                          on_fail=f"delete failed: {exc.response['Error']['Code']}")
                return
        try:
            gone = "Item" not in ddb.get_item(TableName=table, Key=key, ConsistentRead=True)
        except Exception as exc:  # noqa: BLE001
            gone = False
            res.note("ground S3: confirm read failed", type(exc).__name__)
        if not gone:
            res.mark_cleanup_failed()
        res.check("ground S3: dtc-history row deleted", gone, f"{key} gone",
                  on_fail=f"{key} still present or unreadable; delete it by hand")


def run_ground(
    stage: str, run_id: str, manifest: Any, res: "Results",
    cms_api: "Http", avx_api: "Http", dms_api: "Http",
    idp: Any, pool_id: str, client_id: str, ddb: Any, lam: Any,
    scheduler: Any,
) -> None:
    """ground sub-steps 1-6 (manifest-recorded, resumable via --run-id; never re-approves).

    1. approve the target's ground Finding with its own recommended_action, as a temp
       fleet-operator of its fleet; CMS records an open fault grounding (consistent read);
    2. return to service refused 409 fault_not_repaired naming the codes;
    3. not run: a NOTE only (the user accepted dropping "a remote clear still refused"
       on 2026-10-23: CMS's return-to-service reads only the health cache, and ADP ignores
       clears outside the live tier, so a clear of a managed vehicle's code can't change
       the answer; no dtc-history row is written);
    4. a DMS repair order on the target VIN with one line per code/labor-op, marked
       LIVETEST-<run>, not an FI booking (no need_id, no recall line), patched to Ready
       by a temp DMS service advisor; on resume, found by vin-index rather than re-created;
    5. wait until GET /faults shows the code's repairRef = that RO id (SKIP if not yet);
    6. return to service accepted 200; grounding row has returnedAt (consistent read).
    Stops at the first failed step.
    """
    print("\n--- ground ---", flush=True)
    _refuse = require_write_window(scheduler, stage, _now())
    if _refuse is not None:
        res.skip("ground: write window",
                 f"{_refuse} — resume: --part ground --run-id {run_id}")
        return
    switch, _ = _health_agent_switch(lam, stage)
    if switch != "true":
        res.check("ground: FAULT_GROUNDING_ENABLED is 'true'", False, "",
                  on_fail=f"FAULT_GROUNDING_ENABLED={switch}; no grounding items to approve")
        return
    marker = f"LIVETEST-{run_id}"
    saved = json.loads(manifest.read_text())["items"]
    target = saved.get("ground/target")
    if target is None:
        with principal(idp, pool_id, client_id, run_id, "gnd-read", manifest, res,
                       groups=["fleet-operator"], fleet_ids=_ALL_FLEETS) as (token, _):
            try:
                target, skipped = _pick_ground_target(cms_api, avx_api, token, stage, ddb)
            except RuntimeError as exc:
                res.check("ground: pick target", False, "", on_fail=str(exc))
                return
        update_manifest(manifest, "ground/skipped", skipped[:200])
        if target is None:
            res.check("ground: pick target", False, "",
                      on_fail=f"no candidate passed; {len(skipped)} skipped, first {skipped[:3]}")
            return
        update_manifest(manifest, "ground/target", target)
    res.note("ground: target", f"{target['vin']} {target['vehicle_id']} {target['fleet_id']}")
    vid, codes = target["vehicle_id"], target["fault_codes"]

    with principal(idp, pool_id, client_id, run_id, "gnd-op", manifest, res,
                   groups=["fleet-operator"], fleet_ids=target["fleet_id"]) as (token, _):
        if "ground/step1" not in saved:
            _refuse2 = require_write_window(scheduler, stage, _now())
            if _refuse2 is not None:
                res.skip("ground S1: write window",
                         f"{_refuse2} — resume: --part ground --run-id {run_id}")
                return
            status, body = avx_api.call(
                "POST", f"/findings/{urllib.parse.quote(target['ground_finding_id'], safe='')}"
                "/approve", token, target["recommended_action"])
            body = body if isinstance(body, dict) else {}
            if not res.check("ground S1: approve executed",
                             status == 200 and body.get("status") == "executed",
                             f"action {body.get('action_id')}",
                             on_fail=f"got {status} {json.dumps(body)[:120]}"):
                return
            update_manifest(manifest, "ground/step1",
                            {"approved_at": _now().isoformat(), "action_id": body.get("action_id")})
        if not res.check("ground S1: CMS fault grounding open",
                         bool(_open_grounding_rows(ddb, stage, vid)), f"{vid} grounded",
                         on_fail=f"no open grounding row for {vid}"):
            return

        if "ground/step2" not in saved:
            # S2's request must be refused, but it is a write route: if CMS ever accepted
            # it, the vehicle would be returned to service, so it gets its own window
            # check like every other write (found by test_ww2_fleet_ground_one_check_per_write).
            _refuse_s2 = require_write_window(scheduler, stage, _now())
            if _refuse_s2 is not None:
                res.skip("ground S2: write window",
                         f"{_refuse_s2} — resume: --part ground --run-id {run_id}")
                return
            status, body = cms_api.call(
                "POST", f"{_FI}/groundings/{urllib.parse.quote(vid, safe='')}/return-to-service",
                token, {})
            if not _rts_refused(res, "ground S2: return to service refused", status, body, codes):
                return
            update_manifest(manifest, "ground/step2", {"at": _now().isoformat()})

        # S3: remote-clear leg not exercised for a non-live target.
        # CMS return-to-service reads only the health cache (health_source.fault_repair_blockers);
        # the ADP vehicle_fault_status view ignores CMS dtc-history clears for repair_ref
        # (picks only rows inside the vehicle's live coverage interval). Writing a
        # CLEARED_REMOTE row then calling RTS milliseconds later would prove nothing, because
        # no ingest or health-refresh has run.  The pick excludes live-tier vehicles, so a
        # real remote-clear reaching the view is out of scope.  W1 architect decision.
        res.note("ground S3", (
            "remote-clear leg not exercised: return-to-service reads the health cache, "
            "not dtc-history; ADP view ignores clears for repair_ref"
        ))

        # S4: create (or resume-find) a DMS RO on the target VIN, then PATCH to Ready.
        # gate on step4_done (not ro_id): if the PATCH failed on a prior run the RO
        # exists (step4_ro_id written) but is still Draft (step4_done absent); resume
        # must re-PATCH without re-POSTing.
        vin_str = target["vin"]
        ro_id: "str | None" = saved.get("ground/step4_ro_id")
        if "ground/step4_done" not in saved:
            if ro_id is None:
                # First run (or crash before step4_ro_id was written): create marker,
                # try to find an existing LIVETEST RO by vin-index, or POST a new one.
                update_manifest(manifest, "ground/step4_marker", {"at": _now().isoformat()})
                ro_id = _find_existing_marker_ro(ddb, stage, vin_str, marker)
                if ro_id is not None:
                    res.note("ground S4: resume — found existing LIVETEST RO", ro_id)
                    update_manifest(manifest, "ground/step4_ro_id", ro_id)
                else:
                    try:
                        dealer = _first_dealer(ddb, stage)
                    except RuntimeError as exc:
                        res.check("ground S4: pick dealer", False, "", on_fail=str(exc))
                        return
                    lines = [{"labor_op": _labor_op_for_code(c), "pay_type": "customer_pay"}
                             for c in codes]
                    _refuse3 = require_write_window(scheduler, stage, _now())
                    if _refuse3 is not None:
                        res.skip("ground S4 RO POST: write window",
                                 f"{_refuse3} — resume: --part ground --run-id {run_id}")
                        return
                    with principal(idp, pool_id, client_id, run_id, "dms-sa", manifest, res,
                                   groups=["service-advisor"], dealer_ids=dealer) as (sa_tok, _):
                        s4s, s4b = dms_api.call(
                            "POST",
                            f"/api/dms/dealers/{urllib.parse.quote(dealer, safe='')}"
                            "/repair-orders",
                            sa_tok,
                            {"vehicle_vin": vin_str, "lines": lines,
                             "description": f"{marker} ground repair"},
                        )
                        s4b = s4b if isinstance(s4b, dict) else {}
                        if not res.check("ground S4: DMS RO created", s4s == 201,
                                         f"ro_id={s4b.get('ro_id')}",
                                         on_fail=f"got {s4s} {json.dumps(s4b)[:120]}"):
                            return
                        ro_id = str(s4b.get("ro_id") or "")
                        if not res.check("ground S4: not an FI booking", not is_fi_booking(s4b),
                                         "not fi_booking", on_fail="RO is an FI booking"):
                            return
                        update_manifest(manifest, "ground/step4_ro_id", ro_id)
            if not ro_id:
                return
            # PATCH to Ready: read current status first (consistent read) so a resume
            # after a DMS 500-that-committed skips the PATCH instead of double-PATCHing.
            ro_row = ddb.get_item(
                TableName=f"dms-{stage}-repair-orders",
                Key={"ro_id": {"S": ro_id}},
                ConsistentRead=True,
            ).get("Item") or {}
            ro_status_now = (ro_row.get("status") or {}).get("S", "")
            dealer_id = (ro_row.get("dealer_id") or {}).get("S") or ""
            if ro_status_now != "Ready":
                if not dealer_id:
                    try:
                        dealer_id = _first_dealer(ddb, stage)
                    except RuntimeError as exc:
                        res.check("ground S4: PATCH — pick dealer", False, "",
                                  on_fail=str(exc))
                        return
                _refuse4 = require_write_window(scheduler, stage, _now())
                if _refuse4 is not None:
                    res.skip("ground S4 RO PATCH: write window",
                             f"{_refuse4} — resume: --part ground --run-id {run_id}")
                    return
                with principal(idp, pool_id, client_id, run_id, "dms-sa", manifest, res,
                               groups=["service-advisor"],
                               dealer_ids=dealer_id) as (sa_tok, _):
                    ps, pb = dms_api.call(
                        "PATCH",
                        f"/api/dms/dealers/{urllib.parse.quote(dealer_id, safe='')}"
                        f"/repair-orders/{urllib.parse.quote(ro_id, safe='')}",
                        sa_tok, {"status": "Ready"},
                    )
                    pb = pb if isinstance(pb, dict) else {}
                    if not res.check("ground S4: RO status Ready",
                                     ps == 200 and pb.get("status") == "Ready",
                                     f"status={pb.get('status')}",
                                     on_fail=f"PATCH {ps} {json.dumps(pb)[:80]}"):
                        return
            else:
                res.check("ground S4: RO status Ready", True, f"status={ro_status_now}")
            update_manifest(manifest, "ground/step4_done", {"at": _now().isoformat()})
        if not ro_id:
            return

        # S5: wait until the health cache shows repairRef = ro_id for EVERY target code
        # (return to service needs every code repaired; SKIP while the lake and the :47
        # refresh catch up).
        refs: dict = {}
        try:
            for c in codes:
                refs[c] = _faults_repair_ref(cms_api, token, target["fleet_id"], vin_str, c)
        except RuntimeError as exc:
            res.check("ground S5: health cache repairRef = RO id", False, "",
                      on_fail=str(exc))
            return
        if any(r != ro_id for r in refs.values()):
            res.skip("ground S5: health cache repairRef = RO id",
                     f"repairRef by code {refs}, want {ro_id}; waiting for the DMS lake and "
                     f"the :47 health refresh; re-run --part ground --run-id {run_id}")
            return
        res.check("ground S5: health cache repairRef = RO id", True,
                  f"repairRef={ro_id} for {sorted(refs)}")
        update_manifest(manifest, "ground/step5", {"at": _now().isoformat()})

        # S6: return to service → 200; grounding row has returnedAt
        _refuse6 = require_write_window(scheduler, stage, _now())
        if _refuse6 is not None:
            res.skip("ground S6: write window",
                     f"{_refuse6} — resume: --part ground --run-id {run_id}")
            return
        s6s, s6b = cms_api.call(
            "POST",
            f"{_FI}/groundings/{urllib.parse.quote(vid, safe='')}/return-to-service",
            token, {},
        )
        s6b = s6b if isinstance(s6b, dict) else {}
        if not res.check("ground S6: return to service 200",
                         s6s == 200 and "returnedAt" in s6b,
                         f"returnedAt={s6b.get('returnedAt')}",
                         on_fail=f"got {s6s} {json.dumps(s6b)[:120]}"):
            return
        open_rows = _open_grounding_rows(ddb, stage, vid)
        res.check("ground S6: grounding row has returnedAt", not open_rows,
                  f"grounding row closed for {vid}",
                  on_fail=f"{len(open_rows)} open rows still for {vid}")
        update_manifest(manifest, "ground/step6",
                        {"returned_at": s6b.get("returnedAt"), "at": _now().isoformat()})


# ---------------------------------------------------------------------------
# cleanup part
# ---------------------------------------------------------------------------


def run_cleanup(
    stage: str, run_id: str, manifest: Any, res: "Results",
    ddb: Any, idp: Any, pool_id: str, scheduler: Any,
) -> None:
    """cleanup: confirm LIVETEST residue gone; print kept records (RO, grounding, action).

    Requires --allow-writes (to delete any leftover dtc-history row).
    Residue FAIL: dtc-history rows with marker != none; DMS marker ROs != manifest's RO.
    """
    print("\n--- cleanup ---", flush=True)
    _refuse = require_write_window(scheduler, stage, _now())
    if _refuse is not None:
        res.skip("cleanup: write window",
                 f"{_refuse} — resume: --part cleanup --run-id {run_id}")
        return
    saved = json.loads(manifest.read_text())["items"]
    target = saved.get("ground/target")
    # Delete any manifest-recorded users that failed to delete during the run (W-3)
    # Done before any early return so users are cleaned regardless of ground/target state.
    delete_manifest_users(idp, pool_id, manifest, res)
    # B-3 N1: verify no isolation grounding mismatch row was left behind
    iso_coll = saved.get("isolation_grounding_collision")
    if iso_coll:
        iso_vid = iso_coll.get("vehicle_id", "")
        iso_mismatch_id = iso_coll.get("mismatch_finding_id", "")
        iso_gt = f"cms-{stage}-storage-vehicle-groundings"
        if iso_vid and iso_mismatch_id:
            kw_ic: dict[str, Any] = {
                "TableName": iso_gt,
                "KeyConditionExpression": "vehicleId = :v",
                "ExpressionAttributeValues": {":v": {"S": iso_vid}},
                "ConsistentRead": True,
            }
            ic_rows: list[dict] = []
            while True:
                page = ddb.query(**kw_ic)
                ic_rows.extend(page.get("Items", []))
                if not page.get("LastEvaluatedKey"):
                    break
                kw_ic["ExclusiveStartKey"] = page["LastEvaluatedKey"]
            bad_ic = [
                (r.get("actionId") or {}).get("S", "")
                for r in ic_rows
                if (r.get("findingId") or {}).get("S", "") == iso_mismatch_id
            ]
            res.check(
                "cleanup residue: isolation grounding mismatch row gone",
                not bad_ic,
                "no isolation mismatch rows",
                on_fail=f"mismatch findingId {iso_mismatch_id!r} in rows: {bad_ic}",
            )
    if target is None:
        ok_cu, detail_cu = _check_residue_cognito(idp, pool_id, run_id)
        res.check("cleanup residue: no livetest cognito users", ok_cu, detail_cu)
        res.note("cleanup: no ground target in manifest", "nothing to clean")
        return
    dtc_table = _DTC_HISTORY_TABLE_TPL.format(stage=stage)
    marker = f"LIVETEST-{run_id}"
    step3_key = saved.get("ground/step3_key")
    allowed: list = []
    ddb.meta.events.register_first("before-parameter-build.dynamodb.DeleteItem",
                                   _dtc_write_guard(dtc_table, allowed))
    if step3_key:
        key = {"vehicleId": {"S": step3_key["vehicleId"]},
               "timestamp": {"N": str(step3_key["timestamp"])}}
        allowed.append(key)
        _remove_dtc_row(ddb, res, dtc_table, key, marker)
    # Every page: a Scan's FilterExpression applies per 1 MB page, so one page can miss rows.
    dtc_items: list = []
    kw_d: dict[str, Any] = {"TableName": dtc_table, "FilterExpression": "#src = :m",
                            "ExpressionAttributeNames": {"#src": "source"},
                            "ExpressionAttributeValues": {":m": {"S": marker}},
                            "ConsistentRead": True}
    for _ in range(_FAULTS_MAX_PAGES):
        page = ddb.scan(**kw_d)
        dtc_items.extend(page.get("Items", []))
        if not page.get("LastEvaluatedKey"):
            break
        kw_d["ExclusiveStartKey"] = page["LastEvaluatedKey"]
    else:
        raise RuntimeError("dtc-history residue scan: page cap hit")
    res.check("cleanup residue: dtc-history rows with marker = none", not dtc_items,
              "no dtc marker rows",
              on_fail=f"{len(dtc_items)} dtc rows with marker {marker!r}")
    vin_str = target.get("vin", "")
    ro_expected = saved.get("ground/step4_ro_id")
    found_ros: list[str] = []
    if vin_str:
        kw: dict[str, Any] = {
            "TableName": f"dms-{stage}-repair-orders", "IndexName": "vin-index",
            "KeyConditionExpression": "vehicle_vin = :v",
            "ExpressionAttributeValues": {":v": {"S": vin_str}},
        }
        for _ in range(_FAULTS_MAX_PAGES):
            page = ddb.query(**kw)
            for item in page.get("Items", []):
                desc = (item.get("description") or {}).get("S", "")
                ro = (item.get("ro_id") or {}).get("S", "")
                if desc.startswith(marker) and ro:
                    found_ros.append(ro)
            if not page.get("LastEvaluatedKey"):
                break
            kw["ExclusiveStartKey"] = page["LastEvaluatedKey"]
    if "ground/step4_marker" in saved:
        expected_ros = [ro_expected] if ro_expected is not None else []
        res.check("cleanup residue: DMS marker ROs = exactly the manifest RO",
                  found_ros == expected_ros, f"found {found_ros}",
                  on_fail=f"expected {expected_ros!r}, found {found_ros}")
    step1 = saved.get("ground/step1") or {}
    step6 = saved.get("ground/step6") or {}
    vid = target.get("vehicle_id", "")
    kw2: dict[str, Any] = {"TableName": f"cms-{stage}-storage-vehicle-groundings",
                           "KeyConditionExpression": "vehicleId = :vid",
                           "ExpressionAttributeValues": {":vid": {"S": vid}},
                           "ConsistentRead": True}
    all_g: list[dict] = []
    while True:
        page = ddb.query(**kw2)
        all_g.extend(page.get("Items", []))
        if not page.get("LastEvaluatedKey"):
            break
        kw2["ExclusiveStartKey"] = page["LastEvaluatedKey"]
    print(f"  kept RO id      : {ro_expected}")
    print(f"  action id       : {step1.get('action_id')}")
    print(f"  returned_at     : {step6.get('returned_at')}")
    print(f"  grounding rows  : {len(all_g)}")
    # W4(b): fail if this run opened a grounding (step1 recorded) but never closed it (no step6)
    action_id = step1.get("action_id", "")
    if action_id and "ground/step6" not in saved:
        open_ours = [
            g for g in all_g
            if (g.get("actionId") or {}).get("S") == action_id
            and "returnedAt" not in g
        ]
        if open_ours:
            res.check(
                "cleanup: no open grounding from this run",
                False, "",
                on_fail=(
                    f"grounding for action {action_id!r} is still open "
                    f"(step6 not recorded); resume with --part ground --run-id {run_id}"
                ),
            )
    # Delete any manifest-recorded users that failed to delete during the run (W-3)
    # (Already called above before the ground/target check to cover early-return cases;
    # the call here is removed to avoid a double-delete.)
    ok_cu, detail_cu = _check_residue_cognito(idp, pool_id, run_id)
    res.check("cleanup residue: no livetest cognito users", ok_cu, detail_cu)


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def main() -> int:
    p = argparse.ArgumentParser(description="live-verify-fi-cms-fleet task 7.7")
    p.add_argument("--stage", required=True)
    p.add_argument("--part", choices=["pre", "perf", "isolation", "grounding-items",
                                      "ground", "cleanup"])
    p.add_argument("--run-id")
    p.add_argument("--allow-writes", action="store_true")
    p.add_argument("--allow-managed-writes", action="store_true")
    args = p.parse_args()

    check_stage(args.stage)
    if os.environ.get("LIVE_TESTS") != "1":
        print("SKIP: set LIVE_TESTS=1 to run live checks")
        return 2

    _refusal = _lwg.check_write_flags(
        stage=args.stage, go_env=_LIVE_GO_ENV,
        flags={"--allow-writes": args.allow_writes,
               "--allow-managed-writes": args.allow_managed_writes},
    )
    if _refusal:
        print(_refusal, file=sys.stderr)
        return 1

    run_id = args.run_id or make_run_id()
    validate_run_id(run_id)
    _install_signal_handlers()

    manifest = open_manifest(run_id, args.stage)
    res = Results()
    parts: list[str] = [args.part] if args.part else ["pre", "perf", "isolation"]

    # perf and isolation write temp Cognito users
    for pw_part in ("perf", "isolation"):
        if pw_part in parts and not args.allow_writes:
            res.skip(pw_part, "--allow-writes required for temp Cognito users")
            parts = [p2 for p2 in parts if p2 != pw_part]

    # grounding-items needs Cognito; ground needs both flags
    if "grounding-items" in parts and not args.allow_writes:
        res.skip("grounding-items", "--allow-writes required for temp Cognito users")
        parts = [p2 for p2 in parts if p2 != "grounding-items"]
    if "ground" in parts and not (args.allow_writes and args.allow_managed_writes):
        res.skip("ground", "--allow-writes --allow-managed-writes required")
        parts = [p2 for p2 in parts if p2 != "ground"]
    if "cleanup" in parts and not args.allow_writes:
        res.skip("cleanup", "--allow-writes required")
        parts = [p2 for p2 in parts if p2 != "cleanup"]

    if not parts:
        res.summary()
        return res.exit_code

    # Discover CMS stack
    cfn = _client("cloudformation", _REGION, allowed_ops=_PRE_OPS)
    try:
        api_base, pool_id = _discover_cms_stack(cfn, args.stage)
    except Exception as exc:  # noqa: BLE001
        print(f"FATAL: cannot get cms-{args.stage}-ui stack outputs: {exc}", file=sys.stderr)
        return 1
    api = Http(api_base)
    if not pool_id:
        # security review c1 (M W1): the harness won't build a Cognito write client without
        # the pool id; say why instead of failing on the build.
        print(f"FATAL: cms-{args.stage}-ui stack's UserPoolId output is empty", file=sys.stderr)
        return 1

    # Discover AVX stack (needed for grounding-items/ground/coverage)
    avx_base: str | None = None
    if any(p2 in parts for p2 in ("grounding-items", "ground")):
        try:
            avx_base = _discover_avx_stack(cfn, args.stage)
        except Exception as exc:  # noqa: BLE001
            print(f"FATAL: cannot get vsa-{args.stage}-api stack outputs: {exc}",
                  file=sys.stderr)
            return 1
    avx_api = Http(avx_base) if avx_base else None

    print(f"\nrun_id  : {run_id}")
    print(f"manifest: {manifest}")
    print(f"api_base: {api_base}")

    cw  = _client("cloudwatch",   _REGION, allowed_ops=_PRE_OPS)
    lam = _client("lambda",       _REGION, allowed_ops=_PRE_OPS)
    # ground no longer writes DynamoDB (S3 dtc write dropped per W1); cleanup still needs DeleteItem.
    _ddb_writes: set[str] = set()
    if "cleanup" in parts:
        _ddb_writes.add("dynamodb.DeleteItem")
    ddb = _client("dynamodb",     _REGION, allowed_ops=_PRE_OPS | _ddb_writes)
    # Read-only scheduler client for write-window guard (us-west-2 — DMS schedules live there too)
    scheduler = _client("scheduler", _REGION, allowed_ops=_SCHEDULER_OPS)
    # Build cognito-idp client with only read ops for pre; full ops for write parts (W5)
    _cognito_write_parts = frozenset({"perf", "isolation", "grounding-items", "ground", "cleanup"})
    _idp_ops = (
        _COGNITO_OPS
        if any(p2 in _cognito_write_parts for p2 in parts)
        else frozenset({
            "cognito-idp.ListUserPoolClients",
            "cognito-idp.DescribeUserPoolClient",
            # The residue check in main()'s finally pages ListUsers (found by the live
            # read-only `pre` run 2026-10-23 00:39Z: pre alone FAILed with GuardViolation).
            "cognito-idp.ListUsers",
        })
    )
    idp = _client("cognito-idp",  _REGION, allowed_ops=_idp_ops,
                  cognito_username_prefix=f"livetest-{run_id}-",
                  cognito_pool_id=pool_id)
    client_id = _password_client(idp, pool_id)

    # Discover DMS stack (needed for ground and cleanup)
    dms_base: str | None = None
    if any(p2 in parts for p2 in ("ground", "cleanup")):
        try:
            dms_base = _discover_dms_stack(cfn, args.stage)
        except Exception as exc:  # noqa: BLE001
            print(f"FATAL: cannot get DmsApiStack-{args.stage} outputs: {exc}",
                  file=sys.stderr)
            return 1
    dms_api = Http(dms_base) if dms_base else Http("")

    update_manifest(manifest, "stage", args.stage)
    update_manifest(manifest, "api_base", api_base)

    try:
        if "pre" in parts:
            run_pre(args.stage, run_id, manifest, res, cfn, cw, lam, ddb)
        if "perf" in parts:
            run_perf(args.stage, run_id, manifest, res, api, idp, pool_id, client_id, scheduler)
        if "isolation" in parts:
            run_isolation(args.stage, run_id, manifest, res, api, idp, pool_id, client_id, ddb, scheduler)
        if "grounding-items" in parts:
            sts = _client("sts", _REGION, allowed_ops=_GROUNDING_ITEMS_OPS)
            account = sts.get_caller_identity()["Account"]
            s3 = _client("s3", _ADP_REGION, allowed_ops=_GROUNDING_ITEMS_OPS)
            lake_bucket = f"adp-{args.stage}-foundation-lake-{account}-{_ADP_REGION}"
            run_grounding_items(args.stage, run_id, manifest, res, api, avx_api, idp, pool_id, client_id, s3, lake_bucket, lam, ddb, scheduler)  # noqa: E501
        if "ground" in parts:
            run_ground(args.stage, run_id, manifest, res, api, avx_api, dms_api, idp, pool_id, client_id, ddb, lam, scheduler)  # noqa: E501
        if "cleanup" in parts:
            run_cleanup(args.stage, run_id, manifest, res, ddb, idp, pool_id, scheduler)
    finally:
        ok, detail = _check_residue_cognito(idp, pool_id, run_id)
        res.check("residue: no livetest cognito users", ok, detail)
        res.summary()

    return res.exit_code


if __name__ == "__main__":
    sys.exit(main())
