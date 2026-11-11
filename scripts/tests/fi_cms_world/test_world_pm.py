"""W2b: PM compliance — scoped EV plan + health refresh → /pm/compliance returns rows.

FACTS (cms:services/fleet_intelligence/:1ce93a75b):
  - GET /pm/compliance reads the pm health-cache written by refresh-health-cache.
  - Cache populated from adp_{stage}_fleet_health.pm_vehicle_status (health_refresh.py:44).
  - pm_vehicle_status applies fleet plans to managed EVs via cms_ingest.pm_plans +
    cms_ingest.vehicles (pm_vehicle_status.sql cms_vehicle + plan_candidates CTEs @ ADP 895f7b61).
  - 403 body: {"error": "Not authorized for the requested fleet scope"} (index.py ~2935).
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request

from scripts.tests.fi_cms_world.world import CmsWorld
from scripts.tests.fi_rw_world import seed

FLEET_A = seed.FLEETS[0]   # "demo-rental-carshare" — VINs 1-10
FLEET_B = seed.FLEETS[1]   # "demo-lastmile-delivery" — VINs 11-13
PLAN_ID = "plan-tire-ev-fleet-a"
TASK_CODE = "TIRE_ROTATION"
# DDB-encoded appliesTo.M for powertrain-scoped EV plan
# (pm_vehicle_status.sql plan_powertrains CTE; json_extract(p.applies_to, '$.powertrains.L'))
APPLIES_TO = json.dumps({"powertrains": {"L": [{"S": "ev"}]}})


def _get(url: str, token: str) -> tuple[int, dict]:
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def test_pm_compliance() -> None:
    with CmsWorld(run_scheduled=False) as w:
        # Seed fleet-A EV plan into ADP DuckDB.
        # glue_merge_job.py _pm_plans_projection @ ADP 895f7b61 column mapping.
        w.seed_pm_plan_to_adp(
            FLEET_A, PLAN_ID, TASK_CODE, "calendar", 180,
            start_date="2026-01-01", applies_to=APPLIES_TO,
        )

        # Health refresh writes the pm cache from pm_vehicle_status view.
        result = w.refresh_health()
        assert result.get("groups", {}).get("pm") == "ok", f"pm group failed: {result}"

        tok_a = w.cms_fi_token(["fleet-operator"], [FLEET_A])
        base = w.cms_fi_url + "api/v1/fleet-intelligence/"

        # Fleet A's operator gets compliance rows; seed.vin(1) must be in the data
        # (managed EV in fleet A, covered by the TIRE_ROTATION plan).
        status, body = _get(f"{base}pm/compliance?fleetId={FLEET_A}", tok_a)
        assert status == 200, f"expected 200 for fleet A: {status} {body}"
        vins = {r.get("vin") for r in (body.get("data") or [])}
        assert seed.vin(1) in vins, f"vin(1)={seed.vin(1)} not in compliance rows: {vins}"

        # Fleet B's operator cannot read fleet A — exact 403 body from _forbidden().
        tok_b = w.cms_fi_token(["fleet-operator"], [FLEET_B])
        status, body = _get(f"{base}pm/compliance?fleetId={FLEET_A}", tok_b)
        assert status == 403, f"expected 403 for fleet B reading fleet A: {status} {body}"
        assert body.get("error") == "Not authorized for the requested fleet scope", (
            f"wrong 403 body: {body}")
