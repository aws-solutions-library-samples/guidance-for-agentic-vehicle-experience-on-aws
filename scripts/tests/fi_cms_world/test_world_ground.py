"""W3: ground Finding approval → CMS grounding → 409 fault_not_repaired → DMS RO → 200 RTS.

FACTS (cms:services/fleet_intelligence/:1ce93a75b):
  - POST /groundings (G6-7) → groundings.create_fault_grounding (groundings.py:179)
    writes {vehicleId, actionId, vin, fleetId, openFleetId, reason:"fault", faultCodes, ...}
  - POST /groundings/{vehicleId}/return-to-service (G6-9) → groundings.return_to_service
    (groundings.py:288) → 409 fault_not_repaired when repairRef absent (groundings.py:350)
    → 200 when all fault codes have repairRef in health cache
  - CMS auth trusts custom:fleetIds directly from pool WriteAttributes (auth.py)
FACTS (avx:lambdas/api-avx-findings/handler.py:861a366e):
  - POST /findings/{finding_id}/approve → _approve_finding (handler.py:1198)
  - ground_vehicle is server_set (handler.py:337); caller Bearer token forwarded to
    CmsExecutor via _bearer_token (handler.py:777)
  - _dispatch_action calls registry.dispatch → CmsExecutor → POST /groundings (cms.py:503)
  - CmsWorld._patch_cms_requests proxies https://cms-fi.world.test/ to cms_fi_url (world.py)
  - approve response body: {status: "executed"} on successful CMS dispatch (handler.py:1590)
FACTS (avx:agents/fi_fleet_health/writer.py:861a366e):
  - grounding item subject_key = "ground/{attempt}" (writer.py:161)
  - recommended_action = {action_kind:"ground_vehicle", target_system:"cms",
    parameters:{vehicle_vin, fault_codes}} (writer.py:170)
"""
from __future__ import annotations

import datetime
import json
import urllib.error
import urllib.request

from scripts.tests.fi_cms_world.world import CmsWorld, GROUNDINGS_TABLE
from scripts.tests.fi_rw_world import seed

VIN = seed.vin(1)            # "MRDAZ01S0PC000001"
VEHICLE_ID = seed.vehicle_id(1)
FLEET = seed.FLEETS[0]       # "demo-rental-carshare"
CODE = "P0217"               # p0, powertrain; LAB-COOLOT-01 labor op
LABOR_OP = "LAB-COOLOT-01"  # code_labor_op_map entry for P0217 (test_world_health.py)
FAULT_DATE = datetime.date(2026, 10, 14)
RO_ID = "RO-WORLD-W3-001"
RO_TS = "2026-10-15T14:00:00+00:00"
DEALER_ID = "dealer-sacramento"  # seed.py:104 first seeded dealer


def _request(url: str, method: str = "GET", token: str = "", body: dict | None = None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url, data=data, method=method,
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/json"} if data else
                {"Authorization": f"Bearer {token}"},
    )
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def test_ground_flow() -> None:
    with CmsWorld(run_scheduled=False) as w:
        # 1. Seed a p0 open fault on VIN
        w.adp_faults.add_fault(VIN, CODE, FAULT_DATE, FAULT_DATE)

        # 2. Populate recall cache stub (RTS recall check returns [])
        w.seed_recall_cache()

        # 3. Health cache refresh — fleet-health agent reads vehicle_fault_status view
        result = w.refresh_health()
        assert result.get("groups", {}).get("faults") == "ok", (
            f"faults group not ok: {result}")

        # 4. Seed booking + grounding Findings for VIN via real writer
        findings = w.seed_health_findings(VIN, CODE, FLEET)
        ground_f = next(
            (f for f in findings if (f.get("subject_key") or "").startswith("ground/")),
            None,
        )
        assert ground_f is not None, (
            f"no ground/ Finding in {[f.get('subject_key') for f in findings]}")
        ground_id = ground_f["finding_id"]
        rec = ground_f.get("recommended_action", {})
        assert rec.get("action_kind") == "ground_vehicle", f"unexpected rec: {rec}"
        assert rec.get("target_system") == "cms", f"unexpected target: {rec}"

        # 5. Fleet-A operator token (works for both AVX approve and CMS groundings)
        tok = w.cms_fi_token(["fleet-operator"], [FLEET])

        # 6. POST /findings/{id}/approve on AVX → CmsExecutor POSTs /groundings on CMS
        #    Parameters match the recommendation → decision = "approved"
        rec_params = rec.get("parameters", {})
        status, body = _request(
            f"{w.avx_url}findings/{ground_id}/approve",
            method="POST", token=tok,
            body={
                "action_kind": "ground_vehicle",
                "target_system": "cms",
                "parameters": rec_params,
            },
        )
        assert status == 200, f"approve: {status} {body}"
        assert body.get("status") == "executed", (
            f"expected executed, got status={body.get('status')!r}: {body}")
        assert body.get("decision") == "approved", f"expected approved: {body}"

        # 7. Grounding row in cms-staging-storage-vehicle-groundings
        rows = w.scan(GROUNDINGS_TABLE)
        g_row = next((r for r in rows if r.get("vehicleId") == VEHICLE_ID), None)
        assert g_row is not None, f"no grounding row for {VEHICLE_ID}: {rows}"
        assert g_row.get("reason") == "fault", f"wrong reason: {g_row}"
        assert CODE in (g_row.get("faultCodes") or []), f"code missing: {g_row}"

        # 8. POST return-to-service → 409 fault_not_repaired (repairRef absent)
        base = w.cms_fi_url + "api/v1/fleet-intelligence/"
        status, body = _request(
            f"{base}groundings/{VEHICLE_ID}/return-to-service",
            method="POST", token=tok, body={},
        )
        assert status == 409, f"expected 409, got {status}: {body}"
        assert body.get("reason") == "fault_not_repaired", (
            f"expected fault_not_repaired: {body}")
        assert CODE in (body.get("fault_codes") or []), f"code missing: {body}"

        # 9. DMS service-advisor creates a repair order on VIN with the mapped labor op
        sa_tok = w.dms_sa_token(DEALER_ID)
        status, body = _request(
            f"{w.dms_url}api/dms/dealers/{DEALER_ID}/repair-orders",
            method="POST", token=sa_tok,
            body={
                "vehicle_vin": VIN,
                "labor_op": LABOR_OP,
                "description": "LIVETEST-W3 ground repair",
            },
        )
        assert status == 201, f"create RO: {status} {body}"
        ro_id = body.get("ro_id")
        assert ro_id, f"no ro_id in response: {body}"

        # 10. PATCH RO to Ready via the real DMS route
        status, body = _request(
            f"{w.dms_url}api/dms/dealers/{DEALER_ID}/repair-orders/{ro_id}",
            method="PATCH", token=sa_tok,
            body={"status": "Ready"},
        )
        assert status == 200, f"PATCH to Ready: {status} {body}"
        assert body.get("status") == "Ready", f"expected Ready: {body}"

        # 11. Refresh health cache — _refresh_lake reads the real DMS item from DDB
        #     (lines and status preserved by PATCH; no add_ro_image needed — B-2).
        result = w.refresh_health()
        assert result.get("groups", {}).get("faults") == "ok", (
            f"faults group not ok after RO: {result}")

        # 13. GET /faults → repairRef now set
        status, body = _request(
            f"{base}faults?fleetId={FLEET}", token=tok,
        )
        assert status == 200, f"GET /faults: {status} {body}"
        items = body.get("data") or []
        fault = next((f for f in items if f.get("vin") == VIN and f.get("code") == CODE), None)
        assert fault is not None, f"fault {CODE} missing after repair: {items}"
        assert fault.get("repairRef") == ro_id, (
            f"expected repairRef={ro_id!r}: {fault}")

        # 14. POST return-to-service → 200 (fault repaired)
        status, body = _request(
            f"{base}groundings/{VEHICLE_ID}/return-to-service",
            method="POST", token=tok, body={},
        )
        assert status == 200, f"expected 200 RTS, got {status}: {body}"
        assert "returnedAt" in body, f"no returnedAt in body: {body}"
