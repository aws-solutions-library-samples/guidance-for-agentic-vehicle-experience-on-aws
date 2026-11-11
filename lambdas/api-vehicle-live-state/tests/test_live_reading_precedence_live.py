"""Live, read-only check: iOS Home and the Assistant header show the same odometer.

issues/2026-11-06-live-state-stale-odometer/. Run after the vsa-{stage}-api deploy that
ships the precedence rule (and once before it, as the "before" capture: leg 1 fails then).
It invokes deployed Lambdas directly with API Gateway-shaped events whose claims it
builds, so no password or token is needed. Nothing is written.

For each vehicle id:
  1. live-state's odometer, and an electric car's charge, are what CMS's vehicle page
     chose: null unless CMS took a live/feed reading (`values.<field>.basis` live or feed),
     else the same value. A fuel car's fuel level isn't compared (CMS's W3 rule isn't
     exposed in `values`).
  2. For the demo driver's car: Home's odometer (live-state, else /drivers/me, as
     HomeTabView reads it) equals the Assistant header's (/vehicles/{id}/context)
     whenever live-state returns none.

Run:
  LIVE_TESTS=1 CVX_LIVE_STAGE=staging AWS_REGION=us-west-2 \\
    .venv/bin/python -m pytest lambdas/api-vehicle-live-state/tests/test_live_reading_precedence_live.py -v
Optional: LIVE_STATE_CHECK_IDS (comma-separated; default the demo car), DEMO_VEHICLE_ID
(default MRDN0000000000015, the demo car's id since vehicleId = VIN cohort 3, 2026-11-07),
DEMO_DRIVER_EMAIL (default meridian.driver@example.com).
"""
from __future__ import annotations

import json
import math
import os

import pytest

STAGE = os.environ.get("CVX_LIVE_STAGE", "staging")
REGION = os.environ.get("AWS_REGION", "us-west-2")
LIVE_STATE_FN = f"vsa-{STAGE}-api-vehicle-live-state"
CONTEXT_FN = f"vsa-{STAGE}-api-vehicle-context"
DRIVERS_ME_FN = f"vsa-{STAGE}-api-drivers-me"
CMS_API_PREFIX = f"cms-{STAGE}-ui-FleetAPIFunction"
DEMO_VEHICLE = os.environ.get("DEMO_VEHICLE_ID", "MRDN0000000000015")
DEMO_EMAIL = os.environ.get("DEMO_DRIVER_EMAIL", "meridian.driver@example.com")
CHECK_IDS = [v.strip() for v in os.environ.get("LIVE_STATE_CHECK_IDS", DEMO_VEHICLE).split(",") if v.strip()]

_ADMIN = {"sub": "live-check-p303", "email": "live-check@example.invalid",
          "email_verified": "true", "cognito:groups": "platform-admin"}

_ALLOWED = {("lambda", "Invoke"), ("lambda", "ListFunctions")}


@pytest.fixture
def read_only_guard(monkeypatch):
    """Refuse any AWS call outside _ALLOWED; Invoke only the four read routes."""
    import botocore.client

    orig = botocore.client.BaseClient._make_api_call
    allowed_fns = {LIVE_STATE_FN, CONTEXT_FN, DRIVERS_ME_FN}

    def guarded(self, operation_name, api_params):
        key = (self.meta.service_model.service_name, operation_name)
        if key not in _ALLOWED:
            raise AssertionError(f"live-state live guard: {key} is not a permitted read")
        if key == ("lambda", "Invoke"):
            fn = api_params.get("FunctionName", "")
            if fn not in allowed_fns and not fn.startswith(CMS_API_PREFIX):
                raise AssertionError(f"live-state live guard: Invoke of {fn!r} refused")
        return orig(self, operation_name, api_params)

    monkeypatch.setattr(botocore.client.BaseClient, "_make_api_call", guarded)


def _lambda():
    import boto3
    return boto3.client("lambda", region_name=REGION)


def _invoke(client, fn: str, event: dict) -> dict:
    out = client.invoke(FunctionName=fn, Payload=json.dumps(event).encode())
    payload = json.loads(out["Payload"].read())
    assert "FunctionError" not in out, f"{fn}: {payload}"
    assert payload.get("statusCode") == 200, f"{fn}: {payload.get('statusCode')} {payload.get('body')}"
    return json.loads(payload["body"])


def _cms_api_fn(client) -> str:
    names = []
    for page in client.get_paginator("list_functions").paginate():
        names += [f["FunctionName"] for f in page["Functions"] if f["FunctionName"].startswith(CMS_API_PREFIX)]
    assert len(names) == 1, f"expected one {CMS_API_PREFIX}*, got {names}"
    return names[0]


def _get(path: str, resource: str, params: dict, claims: dict) -> dict:
    return {"httpMethod": "GET", "resource": resource, "path": path, "pathParameters": params,
            "queryStringParameters": None, "headers": {},
            "requestContext": {"authorizer": {"claims": claims}}}


def _close(a, b) -> bool:
    return a is not None and b is not None and math.isclose(float(a), float(b), rel_tol=0, abs_tol=1e-6)


@pytest.mark.live_aws
@pytest.mark.skipif(os.environ.get("LIVE_TESTS") != "1", reason="live test; set LIVE_TESTS=1")
@pytest.mark.parametrize("vehicle_id", CHECK_IDS)
@pytest.mark.usefixtures("read_only_guard")
def test_live_state_returns_what_the_cms_page_chose(vehicle_id):
    client = _lambda()
    cms_fn = _cms_api_fn(client)
    # Two invokes a second apart can straddle a new message; one retry before failing.
    for attempt in (1, 2):
        problems = _compare_once(client, cms_fn, vehicle_id)
        if not problems:
            return
        print(f"{vehicle_id}: attempt {attempt} disagreed: {problems}")
    pytest.fail(f"{vehicle_id}: {problems}")


def _is_electric(vehicle: dict) -> bool:
    """CMS vehicle_values._is_electric, on the CMS vehicle-detail response."""
    facts = vehicle.get("startingFacts")
    if isinstance(facts, dict) and facts.get("powertrain") is not None:
        return facts.get("powertrain") == "electric"
    return vehicle.get("fuelType") == "electric"


def _compare_once(client, cms_fn: str, vehicle_id: str) -> list:
    live = _invoke(client, LIVE_STATE_FN, _get(f"/vehicles/{vehicle_id}/live-state",
                                               "/vehicles/{vehicleId}/live-state",
                                               {"vehicleId": vehicle_id}, _ADMIN))
    cms = _invoke(client, cms_fn, _get(f"/api/v1/vehicles/{vehicle_id}",
                                       "/api/v1/vehicles/{id}", {"id": vehicle_id}, _ADMIN))
    cms = cms.get("vehicle", cms)
    values = cms.get("values") or {}
    electric = _is_electric(cms)
    print(f"{vehicle_id}: live-state odometer={live.get('odometer')} fuelLevel={live.get('fuelLevel')}; "
          f"CMS odometer={values.get('odometer')} batteryCharge={values.get('batteryCharge')} "
          f"electric={electric}")
    checks = [("odometer", "odometer")]
    if electric:
        checks.append(("fuelLevel", "batteryCharge"))
    else:
        # A fuel car's fuel follows CMS's separate W3 rule (any timed fuelLevel signal, no
        # snapshot), which CMS's `values` doesn't expose, so it isn't compared here.
        print(f"{vehicle_id}: fuel car, charge not compared (CMS W3 rule; review cycle 1 W2)")
    problems = []
    for field, cms_key in checks:
        chosen = values.get(cms_key) or {}
        if chosen.get("basis") in ("live", "feed"):
            if not _close(live.get(field), chosen.get("value")):
                problems.append(f"{field}: live-state {live.get(field)!r}, CMS live {chosen!r}")
        elif live.get(field) is not None:
            problems.append(f"{field}: live-state {live.get(field)!r} while CMS shows the history "
                            f"value {chosen!r}")
    return problems


@pytest.mark.usefixtures("read_only_guard")
@pytest.mark.live_aws
@pytest.mark.skipif(os.environ.get("LIVE_TESTS") != "1", reason="live test; set LIVE_TESTS=1")
def test_home_and_assistant_show_the_same_odometer_for_the_demo_car():
    client = _lambda()
    live = _invoke(client, LIVE_STATE_FN, _get(f"/vehicles/{DEMO_VEHICLE}/live-state",
                                               "/vehicles/{vehicleId}/live-state",
                                               {"vehicleId": DEMO_VEHICLE}, _ADMIN))
    me = _invoke(client, DRIVERS_ME_FN, {"httpMethod": "GET", "resource": "/drivers/me",
                                         "requestContext": {"authorizer": {"claims": {
                                             "sub": "live-check-p303", "email": DEMO_EMAIL,
                                             "email_verified": "true"}}}})
    ctx = _invoke(client, CONTEXT_FN, _get(f"/vehicles/{DEMO_VEHICLE}/context",
                                           "/vehicles/{vehicleId}/context",
                                           {"vehicleId": DEMO_VEHICLE}, _ADMIN))
    me_vehicle = me.get("vehicle") or {}
    assert me_vehicle.get("vehicleId") == DEMO_VEHICLE, f"/drivers/me resolved {me_vehicle.get('vehicleId')!r}"
    # HomeTabView: (live?.odometer).map { Int($0) } ?? v.odometer ?? v.mileage
    live_odo = live.get("odometer")
    home = int(live_odo) if live_odo is not None else (me_vehicle.get("odometer") or me_vehicle.get("mileage"))
    # AssistantTabView nameplate: ctx.vehicle.odometer ?? ctx.vehicle.mileage
    ctx_vehicle = ctx.get("vehicle") or {}
    assistant = ctx_vehicle.get("odometer") or ctx_vehicle.get("mileage")
    print(f"{DEMO_VEHICLE}: Home {home} mi (live-state {live_odo}, drivers/me {me_vehicle.get('odometer')}); "
          f"Assistant {assistant} mi")
    if live_odo is None:
        assert home == assistant, f"Home {home} mi vs Assistant {assistant} mi"
    else:
        # A live reading newer than the history value: Home shows it, the Assistant shows the
        # history value (accepted deviation AVX W1, decisions 2026-10-24). It can only be ahead.
        assert home >= assistant, f"live {home} mi is behind history {assistant} mi"
