"""W2: RO-to-repair flow — p0 fault, 409 fault_not_repaired, repair closes RTS.

FACTS (cms:services/fleet_intelligence/:1ce93a75b):
  - GET /faults → health_source.get_faults() reading health-cache S3 (index.py:2276)
  - POST /groundings/{vehicleId}/return-to-service (index.py:3509)
    → groundings.return_to_service() (groundings.py:288)
    → fault_check_fn calls health_source.fault_repair_blockers() (health_source.py:527)
    → 409 {"reason": "fault_not_repaired", ...} when repairRef absent (groundings.py:350)
    → 200 when all fault codes have a repairRef in the health cache
  - fleetIntelligenceTask key for refresh tasks (index.py:690-700)
  - repair_ref comes from dms_staging.repair_orders via vehicle_fault_status.sql (ADP 895f7b61)
"""
from __future__ import annotations

import datetime
import json
import urllib.error
import urllib.request

from scripts.tests.fi_cms_world.world import CmsWorld, GROUNDINGS_TABLE
from scripts.tests.fi_rw_world import seed

VIN = seed.vin(1)           # "MRDAZ01S0PC000001"
VEHICLE_ID = seed.vehicle_id(1)  # same value for seed vehicles
FLEET = seed.FLEETS[0]      # "demo-rental-carshare"
CODE = "P0217"              # p0, powertrain, LAB-COOLOT-01
LABOR_OP = "LAB-COOLOT-01"  # from code_labor_op_map.json fault_codes section
FAULT_DATE = datetime.date(2026, 10, 14)
RO_ID = "RO-WORLD-W2-001"
RO_TS = "2026-10-15T12:00:00+00:00"   # one day after FAULT_DATE → repair_date >= anchor


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


def test_fault_repair_flow() -> None:
    with CmsWorld(run_scheduled=False) as w:
        # 1. Seed a p0 open fault on VIN
        w.adp_faults.add_fault(VIN, CODE, FAULT_DATE, FAULT_DATE)

        # 2. Populate recall cache stub (makes RTS recall check return [])
        w.seed_recall_cache()

        # 3. Populate CMS health cache (faults group reads vehicle_fault_status view)
        result = w.refresh_health()
        assert result.get("groups", {}).get("faults") == "ok", (
            f"faults group not ok: {result}")

        tok = w.cms_fi_token(["fleet-operator"], [FLEET])
        base = w.cms_fi_url + "api/v1/fleet-intelligence/"

        # 4. GET /faults → fault listed as open, no repairRef
        status, body = _request(f"{base}faults?fleetId={FLEET}", token=tok)
        assert status == 200, f"GET /faults: {status} {body}"
        items = body.get("data") or []
        fault = next((f for f in items if f.get("vin") == VIN and f.get("code") == CODE), None)
        assert fault is not None, f"P0217 not in faults: {items}"
        assert fault.get("status") == "open", f"expected open: {fault}"
        assert fault.get("repairRef") is None, f"unexpected repairRef: {fault}"

        # 5. Seed an open fault grounding directly (vehicleId → VIN already in vehicles table)
        w.put(GROUNDINGS_TABLE, {
            "vehicleId": VEHICLE_ID, "actionId": "act-world-w2",
            "vin": VIN, "fleetId": FLEET, "openFleetId": FLEET,
            "reason": "fault", "faultCodes": [CODE],
            "findingId": "finding-world-w2", "approverSub": "sub-world-w2",
            "groundedAt": w.clock.iso(),
        })

        # 6. POST return-to-service → 409 fault_not_repaired (repairRef absent)
        status, body = _request(
            f"{base}groundings/{VEHICLE_ID}/return-to-service",
            method="POST", token=tok, body={})
        assert status == 409, f"expected 409, got {status}: {body}"
        assert body.get("reason") == "fault_not_repaired", (
            f"expected fault_not_repaired: {body}")
        assert CODE in (body.get("fault_codes") or []), f"code missing from body: {body}"

        # 7. Add DMS RO image with the mapped labor_op dated after the fault sighting
        w.add_ro_image(RO_ID, VIN, "Ready", LABOR_OP, RO_TS)

        # 8. Refresh health cache → vehicle_fault_status now has repair_ref = RO_ID
        result = w.refresh_health()
        assert result.get("groups", {}).get("faults") == "ok", (
            f"faults group not ok after RO: {result}")

        # 9. GET /faults → repairRef now set
        status, body = _request(f"{base}faults?fleetId={FLEET}", token=tok)
        assert status == 200, f"GET /faults after repair: {status} {body}"
        items = body.get("data") or []
        fault = next((f for f in items if f.get("vin") == VIN and f.get("code") == CODE), None)
        assert fault is not None, f"P0217 missing after repair: {items}"
        assert fault.get("repairRef") == RO_ID, f"expected repairRef={RO_ID}: {fault}"

        # 10. POST return-to-service → 200 (fault now repaired)
        status, body = _request(
            f"{base}groundings/{VEHICLE_ID}/return-to-service",
            method="POST", token=tok, body={})
        assert status == 200, f"expected 200 RTS, got {status}: {body}"
