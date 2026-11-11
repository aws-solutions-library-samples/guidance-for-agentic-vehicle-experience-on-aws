"""Live, read-only check: live-state serves Redis values only under their age rules.

issues/2026-11-07-live-state-stale-values/. Run after the vsa-{stage}-api deploy that
ships the rules (and once before it, as the "before" capture: the demo car fails then).
It invokes deployed Lambdas directly with API Gateway-shaped events whose claims it
builds, so no password or token is needed. Nothing is written.

For each vehicle id, from CMS's `GET /api/v1/vehicles/{id}` `liveSignals` (the same
Redis signals and timestamps hashes, with each reading's own time):
  1. each current-state value (speed, heading, engineTemp, batteryVoltage, batteryLevel)
     is the newest timed reading among the handler's aliases when that reading is at
     most 120 s old, else null. A reading within 5 s of the limit may go either way
     (the two invokes are not simultaneous) and is not judged.
  2. the position is the newest timed fix when it is at most 7 days old
     (`locationSource` "redis"), else not a Redis fix.
  3. the body is strictly valid JSON.
For the demo driver's car: Home's Speed cell (live-state, else /drivers/me `lastSpeed`, as
HomeTabView reads it) never comes from a Redis reading older than 120 s. For each car an app
persona signs in to (APP_CAR_IDS, default the demo car): with no current speed, the row's
`lastSpeed` is a numeric 0, since without one the Vehicle tab shows the mock frame's ~58 mph.

Run:
  LIVE_TESTS=1 CVX_LIVE_STAGE=staging AWS_REGION=us-west-2 \\
    LIVE_STATE_CHECK_IDS=<the app cars>,<FleetWise live-tier cars, comma-separated> \\
    APP_CAR_IDS=<the app cars> \\
    .venv/bin/python -m pytest lambdas/api-vehicle-live-state/tests/test_live_value_ages_live.py -v -s
List each app car in both, so its current reading is age-checked too.
Optional: DEMO_VEHICLE_ID (default MRDN0000000000015, the demo car's id since vehicleId = VIN
cohort 3, 2026-11-07), DEMO_DRIVER_EMAIL (default meridian.driver@example.com).
"""
from __future__ import annotations

import json
import math
import os
import time

import pytest

STAGE = os.environ.get("CVX_LIVE_STAGE", "staging")
REGION = os.environ.get("AWS_REGION", "us-west-2")
LIVE_STATE_FN = f"vsa-{STAGE}-api-vehicle-live-state"
DRIVERS_ME_FN = f"vsa-{STAGE}-api-drivers-me"
CMS_API_PREFIX = f"cms-{STAGE}-ui-FleetAPIFunction"
DEMO_VEHICLE = os.environ.get("DEMO_VEHICLE_ID", "MRDN0000000000015")
DEMO_EMAIL = os.environ.get("DEMO_DRIVER_EMAIL", "meridian.driver@example.com")
CHECK_IDS = [v.strip() for v in os.environ.get("LIVE_STATE_CHECK_IDS", DEMO_VEHICLE).split(",") if v.strip()]
# Cars an iOS persona signs in to (scripts/seed-persona-users.py); their speed fallback must be 0.
APP_CAR_IDS = [v.strip() for v in os.environ.get("APP_CAR_IDS", DEMO_VEHICLE).split(",") if v.strip()]

CURRENT_LIMIT_MS = 120_000
LAST_KNOWN_LIMIT_MS = 7 * 86_400_000
FUTURE_TOLERANCE_MS = 60_000
EDGE_MS = 5_000
# The handler's alias lists (lambdas/api-vehicle-live-state/handler.py `aliases`).
CURRENT = {
    "speed": ["speed", "lastSpeed", "VehicleSpeed"],
    "engineTemp": ["engineTemp", "engine_temp", "coolantTemp", "catalyst_temp"],
    "batteryVoltage": ["batteryVoltage", "battery_voltage"],
    "heading": ["heading", "Heading", "gps_heading"],
    "batteryLevel": ["batteryLevel", "battery_level"],
}
POSITION = {
    "latitude": (["lat", "latitude", "Latitude", "gps_lat", "currentLocation.latitude"], 90.0),
    "longitude": (["lng", "longitude", "Longitude", "lon", "gps_lng", "gps_lon",
                   "currentLocation.longitude"], 180.0),
}

_ADMIN = {"sub": "live-check-p316", "email": "live-check@example.invalid",
          "email_verified": "true", "cognito:groups": "platform-admin"}

_ALLOWED = {("lambda", "Invoke"), ("lambda", "ListFunctions"), ("dynamodb", "GetItem")}
VEHICLES_TABLE = f"cms-{STAGE}-storage-vehicles"


@pytest.fixture
def read_only_guard(monkeypatch):
    """Refuse any AWS call outside _ALLOWED; Invoke only the read routes; GetItem only
    the vehicles table's `lastSpeed`."""
    import botocore.client

    orig = botocore.client.BaseClient._make_api_call
    allowed_fns = {LIVE_STATE_FN, DRIVERS_ME_FN}

    def guarded(self, operation_name, api_params):
        key = (self.meta.service_model.service_name, operation_name)
        if key not in _ALLOWED:
            raise AssertionError(f"live-state ages guard: {key} is not a permitted read")
        if key == ("lambda", "Invoke"):
            fn = api_params.get("FunctionName", "")
            if fn not in allowed_fns and not fn.startswith(CMS_API_PREFIX):
                raise AssertionError(f"live-state ages guard: Invoke of {fn!r} refused")
        if key == ("dynamodb", "GetItem"):
            if api_params.get("TableName") != VEHICLES_TABLE or api_params.get("ProjectionExpression") != "lastSpeed":
                raise AssertionError(f"live-state ages guard: GetItem {api_params.get('TableName')!r} refused")
        return orig(self, operation_name, api_params)

    monkeypatch.setattr(botocore.client.BaseClient, "_make_api_call", guarded)


def _lambda():
    import boto3
    return boto3.client("lambda", region_name=REGION)


def _refuse_constant(token):
    raise AssertionError(f"non-JSON constant {token} in a live-state body")


def _invoke(client, fn: str, event: dict) -> dict:
    out = client.invoke(FunctionName=fn, Payload=json.dumps(event).encode())
    payload = json.loads(out["Payload"].read())
    assert "FunctionError" not in out, f"{fn}: {payload}"
    assert payload.get("statusCode") == 200, f"{fn}: {payload.get('statusCode')} {payload.get('body')}"
    return json.loads(payload["body"], parse_constant=_refuse_constant)


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


def _newest(signals: dict, names: list, now_ms: int, max_abs: float | None = None):
    """(value, ts_ms) of the newest timed, finite reading among `names`, as the handler picks."""
    best = None
    for name in names:
        s = signals.get(name)
        if not s:
            continue
        try:
            value = float(s.get("value"))
        except (TypeError, ValueError):
            continue
        if not math.isfinite(value) or (max_abs is not None and abs(value) > max_abs):
            continue
        raw = str(s.get("timestamp") or "").strip()
        if not raw.isdigit() or int(raw) > now_ms + FUTURE_TOLERANCE_MS:
            continue
        ts = int(raw)
        if best is None or ts > best[1]:
            best = (value, ts)
    return best


def _close(a, b) -> bool:
    return a is not None and b is not None and math.isclose(float(a), float(b), rel_tol=0, abs_tol=1e-6)


def _compare_once(client, cms_fn: str, vehicle_id: str) -> list:
    t0 = int(time.time() * 1000)
    live = _invoke(client, LIVE_STATE_FN, _get(f"/vehicles/{vehicle_id}/live-state",
                                               "/vehicles/{vehicleId}/live-state",
                                               {"vehicleId": vehicle_id}, _ADMIN))
    cms = _invoke(client, cms_fn, _get(f"/api/v1/vehicles/{vehicle_id}",
                                       "/api/v1/vehicles/{id}", {"id": vehicle_id}, _ADMIN))
    cms = cms.get("vehicle", cms)
    signals = {s.get("name"): s for s in (cms.get("liveSignals") or []) if s.get("name")}
    problems = []
    for field, names in CURRENT.items():
        reading = _newest(signals, names, t0)
        age = None if reading is None else t0 - reading[1]
        print(f"{vehicle_id}: {field} live-state={live.get(field)!r} newest Redis="
              f"{None if reading is None else reading[0]!r} age_s={None if age is None else age // 1000}")
        if reading is not None and abs(age - CURRENT_LIMIT_MS) <= EDGE_MS:
            continue   # too close to the limit to judge from two separate invokes
        expected = reading[0] if reading is not None and age <= CURRENT_LIMIT_MS else None
        if expected is None and live.get(field) is not None:
            problems.append(f"{field}: live-state {live.get(field)!r}, but the newest Redis reading is "
                            f"{'absent' if reading is None else f'{age // 1000} s old'}")
        elif expected is not None and not _close(live.get(field), expected):
            problems.append(f"{field}: live-state {live.get(field)!r}, newest fresh Redis reading {expected!r}")
    fixes = {}
    for field, (names, bound) in POSITION.items():
        fix = _newest(signals, names, t0, bound)
        fixes[field] = fix if fix is not None and t0 - fix[1] <= LAST_KNOWN_LIMIT_MS else None
    print(f"{vehicle_id}: position live-state=({live.get('latitude')}, {live.get('longitude')}) "
          f"source={live.get('locationSource')!r}; Redis fix ages_s="
          f"{[None if f is None else (t0 - f[1]) // 1000 for f in fixes.values()]}")
    if all(fixes.values()):
        if live.get("locationSource") != "redis":
            problems.append(f"position: a Redis fix within 7 days exists, locationSource {live.get('locationSource')!r}")
        for field, fix in fixes.items():
            if not _close(live.get(field), fix[0]):
                problems.append(f"{field}: live-state {live.get(field)!r}, Redis fix {fix[0]!r}")
    elif live.get("locationSource") == "redis":
        problems.append("position: locationSource 'redis' without a Redis fix on both axes within 7 days")
    return problems


@pytest.mark.live_aws
@pytest.mark.skipif(os.environ.get("LIVE_TESTS") != "1", reason="live test; set LIVE_TESTS=1")
@pytest.mark.parametrize("vehicle_id", CHECK_IDS)
@pytest.mark.usefixtures("read_only_guard")
def test_live_state_serves_redis_values_only_under_their_age_rules(vehicle_id):
    client = _lambda()
    cms_fn = _cms_api_fn(client)
    # Two invokes a second apart can straddle a new message; one retry before failing.
    for attempt in (1, 2):
        problems = _compare_once(client, cms_fn, vehicle_id)
        if not problems:
            return
        print(f"{vehicle_id}: attempt {attempt} disagreed: {problems}")
    pytest.fail(f"{vehicle_id}: {problems}")


@pytest.mark.live_aws
@pytest.mark.skipif(os.environ.get("LIVE_TESTS") != "1", reason="live test; set LIVE_TESTS=1")
@pytest.mark.usefixtures("read_only_guard")
def test_home_speed_never_comes_from_an_old_redis_reading():
    client = _lambda()
    cms_fn = _cms_api_fn(client)
    t0 = int(time.time() * 1000)
    live = _invoke(client, LIVE_STATE_FN, _get(f"/vehicles/{DEMO_VEHICLE}/live-state",
                                               "/vehicles/{vehicleId}/live-state",
                                               {"vehicleId": DEMO_VEHICLE}, _ADMIN))
    cms = _invoke(client, cms_fn, _get(f"/api/v1/vehicles/{DEMO_VEHICLE}",
                                       "/api/v1/vehicles/{id}", {"id": DEMO_VEHICLE}, _ADMIN))
    cms = cms.get("vehicle", cms)
    me = _invoke(client, DRIVERS_ME_FN, {"httpMethod": "GET", "resource": "/drivers/me",
                                         "requestContext": {"authorizer": {"claims": {
                                             "sub": "live-check-p316", "email": DEMO_EMAIL,
                                             "email_verified": "true"}}}})
    row = me.get("vehicle") or {}
    assert row.get("vehicleId") == DEMO_VEHICLE, f"/drivers/me resolved {row.get('vehicleId')!r}"
    signals = {s.get("name"): s for s in (cms.get("liveSignals") or []) if s.get("name")}
    reading = _newest(signals, CURRENT["speed"], t0)
    age_s = None if reading is None else (t0 - reading[1]) // 1000
    home = live.get("speed") if live.get("speed") is not None else row.get("lastSpeed")
    source = "live-state" if live.get("speed") is not None else "/drivers/me lastSpeed (vehicles row)"
    print(f"{DEMO_VEHICLE}: Home Speed {home!r} km/h from {source}; newest Redis speed "
          f"{None if reading is None else reading[0]!r}, {age_s} s old")
    if live.get("speed") is not None:
        assert reading is not None and t0 - reading[1] <= CURRENT_LIMIT_MS + EDGE_MS, (
            f"Home shows live-state speed {live.get('speed')!r} but Redis's newest speed is {age_s} s old")


@pytest.mark.live_aws
@pytest.mark.skipif(os.environ.get("LIVE_TESTS") != "1", reason="live test; set LIVE_TESTS=1")
@pytest.mark.parametrize("vehicle_id", APP_CAR_IDS)
@pytest.mark.usefixtures("read_only_guard")
def test_speed_fallback_shows_no_old_or_mock_speed(vehicle_id):
    """Review cycles 1 W2 and 2 W1. With no current speed, iOS shows the vehicles row's
    `lastSpeed` (/drivers/me passes it through); Home shows "—" without one, but the Vehicle
    tab's Live Signals row then shows the mock telemetry frame's ~58 mph
    (`VehicleTabView.swift:411-413`). For a car an app persona signs in to, the fallback must
    therefore be a numeric 0: anything else is an undated or invented speed on screen.
    Run over APP_CAR_IDS (default the demo car): the cars app personas sign in to
    (AVX scripts/seed-persona-users.py and CMS deployment/scripts/seed_driver_users.py
    --demo-personas, read 2026-11-07); no app persona is assigned a live-tier car."""
    import boto3
    client = _lambda()
    live = _invoke(client, LIVE_STATE_FN, _get(f"/vehicles/{vehicle_id}/live-state",
                                               "/vehicles/{vehicleId}/live-state",
                                               {"vehicleId": vehicle_id}, _ADMIN))
    item = boto3.client("dynamodb", region_name=REGION).get_item(
        TableName=VEHICLES_TABLE, Key={"vehicleId": {"S": vehicle_id}},
        ProjectionExpression="lastSpeed").get("Item") or {}
    attr = item.get("lastSpeed")
    print(f"{vehicle_id}: live-state speed {live.get('speed')!r}; row lastSpeed {attr!r}")
    if live.get("speed") is not None:
        return   # a current reading; its age is judged above when the car is also in LIVE_STATE_CHECK_IDS
    assert attr is not None, (
        f"{vehicle_id}: no current speed and no row lastSpeed: the Vehicle tab shows the mock frame's speed")
    assert set(attr) == {"N"}, f"{vehicle_id}: row lastSpeed is not a number: {attr!r} (iOS decodes Double?)"
    assert float(attr["N"]) == 0.0, (
        f"{vehicle_id}: no current speed, so iOS shows the row's undated lastSpeed {attr['N']} km/h")
