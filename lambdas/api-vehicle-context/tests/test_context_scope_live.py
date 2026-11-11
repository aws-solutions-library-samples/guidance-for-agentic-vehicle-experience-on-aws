"""Live, read-only check of the deployed /vehicles/{id}/context caller scope.

issues/2026-10-24-vehicle-context-driver-pii-unscoped/. Run after the vsa-{stage}-api
deploy that ships context_scope.py. It invokes the deployed Lambda directly with
API Gateway-shaped events whose authorizer claims it builds, so no password or token is
needed, and checks each standing against real staging rows:

  1. the function carries both trust flags, equal to their SSM attestations
  2. a driver reads their own vehicle (2b, strict xfail: and gets the driver block, which
     also needs the drivers scan to page; see the leg's message)
  3. the same driver is refused another fleet's vehicle: 403, and none of that vehicle's
     driver details in the body
  4. a fleet operator reads a vehicle in its fleet and is refused one in another fleet,
     and gets the same 403 for a vehicle id that doesn't exist
  5. no claims is 401; claims without standing, or a service role alone, is 403
  6. a platform-admin reads any vehicle

Every AWS call goes through an allowlist guard (reads, plus Invoke of this one
function). The handler itself only reads. Before the deploy, legs 3-5 fail: that is the
hole, and running it then is the "before" capture.

Run:
  LIVE_TESTS=1 CVX_LIVE_STAGE=staging AWS_REGION=us-west-2 \\
    .venv/bin/python -m pytest lambdas/api-vehicle-context/tests/test_context_scope_live.py -v
Optional: TEST_VEHICLE_ID (default MRDN0000000000015, the meridian.driver demo vehicle).

``VC_LIVE_TARGET=local`` runs legs 2-6 through THIS checkout's handler in-process
against the real staging tables (same read-only guard; leg 1 skips), so the code can be
checked on real rows before it is deployed.
"""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path

import pytest

STAGE = os.environ.get("CVX_LIVE_STAGE", "staging")
REGION = os.environ.get("AWS_REGION", "us-west-2")
FUNCTION = f"vsa-{STAGE}-api-vehicle-context"
VEHICLES = f"cms-{STAGE}-storage-vehicles"
DRIVERS = f"cms-{STAGE}-storage-drivers"
OWN = os.environ.get("TEST_VEHICLE_ID", "MRDN0000000000015")
MISSING = "VEH-LIVECHECK-NOPE"
TARGET = os.environ.get("VC_LIVE_TARGET", "deployed")
REFUSAL = {"error": "vehicle not in caller scope"}

#: (service, operation) pairs this check may call. Invoke is further limited to FUNCTION.
_ALLOWED = {
    ("lambda", "Invoke"), ("lambda", "GetFunctionConfiguration"),
    ("dynamodb", "GetItem"), ("dynamodb", "Scan"), ("dynamodb", "Query"),
    ("ssm", "GetParameter"), ("sts", "GetCallerIdentity"),
}

_live = [
    pytest.mark.live_aws,
    pytest.mark.skipif(os.environ.get("LIVE_TESTS") != "1", reason="live test; set LIVE_TESTS=1"),
]


@pytest.fixture
def read_only_guard(monkeypatch):
    """Refuse any AWS call outside _ALLOWED, before it is sent."""
    import botocore.client

    orig = botocore.client.BaseClient._make_api_call

    def guarded(self, operation_name, api_params):
        key = (self.meta.service_model.service_name, operation_name)
        if key not in _ALLOWED:
            raise AssertionError(f"vehicle-context live guard: {key} is not a permitted read")
        if key == ("lambda", "Invoke") and api_params.get("FunctionName") != FUNCTION:
            raise AssertionError(
                f"vehicle-context live guard: Invoke of {api_params.get('FunctionName')!r} refused")
        return orig(self, operation_name, api_params)

    monkeypatch.setattr(botocore.client.BaseClient, "_make_api_call", guarded)


def test_guard_refuses_writes_and_other_functions(read_only_guard):
    """Self-test, no AWS: the real fixture blocks a write and a foreign Invoke before
    sending, and lets an allowed read through to the (dead) endpoint."""
    import boto3
    from botocore.config import Config

    cfg = Config(retries={"max_attempts": 1}, connect_timeout=1, read_timeout=1)
    kw = dict(region_name="us-east-1", endpoint_url="http://127.0.0.1:1",
              aws_access_key_id="x", aws_secret_access_key="x", config=cfg)
    ddb = boto3.client("dynamodb", **kw)
    lam = boto3.client("lambda", **kw)
    for call in (lambda: ddb.put_item(TableName="t", Item={"k": {"S": "v"}}),
                 lambda: lam.invoke(FunctionName="some-other-function", Payload=b"{}"),
                 lambda: lam.update_function_configuration(FunctionName=FUNCTION)):
        with pytest.raises(Exception) as ei:
            call()
        assert type(ei.value) is AssertionError and "live guard" in str(ei.value), ei.value
    try:
        ddb.get_item(TableName="t", Key={"k": {"S": "v"}})
    except AssertionError:
        pytest.fail("GetItem must pass the guard")
    except Exception:  # noqa: BLE001 — the dead endpoint refuses the connection
        pass


# --------------------------------------------------------------------------
# Live legs
# --------------------------------------------------------------------------

@pytest.fixture(scope="module")
def aws():
    import boto3
    return {s: boto3.client(s, region_name=REGION) for s in ("lambda", "dynamodb", "ssm")}


_local_handler = None


def _local(monkeypatch_env):
    """This checkout's handler, loaded once, reading the real stage tables."""
    global _local_handler
    if _local_handler is None:
        os.environ.update({
            "CMS_VEHICLES_TABLE": VEHICLES, "CMS_DRIVERS_TABLE": DRIVERS,
            "CMS_DTC_HISTORY_TABLE": f"cms-{STAGE}-storage-dtc-history",
            "CMS_SERVICE_HISTORY_TABLE": f"cms-{STAGE}-storage-service-history",
        })
        os.environ.pop("LIVE_STATE_FN_NAME", None)
        spec = importlib.util.spec_from_file_location(
            "vehicle_context_live_local", Path(__file__).resolve().parents[1] / "handler.py")
        _local_handler = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(_local_handler)
    for var in ("AVX_VEHICLE_ID_CLAIM_TRUSTED", "AVX_FLEET_IDS_CLAIM_TRUSTED"):
        monkeypatch_env.setenv(var, "true")  # the staging attestation leg 1 checks
    return _local_handler


def _invoke(aws, vehicle_id, claims, monkeypatch=None):
    event = {"resource": "/vehicles/{vehicleId}/context", "httpMethod": "GET",
             "pathParameters": {"vehicleId": vehicle_id}}
    if claims is not None:
        event["requestContext"] = {"authorizer": {"claims": claims}}
    if TARGET == "local":
        out = _local(monkeypatch).handler(event, None)
        return out["statusCode"], out["body"]
    resp = aws["lambda"].invoke(FunctionName=FUNCTION, Payload=json.dumps(event).encode())
    out = json.loads(resp["Payload"].read())
    assert "FunctionError" not in resp, f"function error: {out.get('errorType')}: {out.get('errorMessage')}"
    return out["statusCode"], out["body"]


def _fleet_of(aws, vehicle_id):
    item = aws["dynamodb"].get_item(TableName=VEHICLES, Key={"vehicleId": {"S": vehicle_id}},
                                    ProjectionExpression="fleetId").get("Item") or {}
    return (item.get("fleetId") or {}).get("S")


@pytest.fixture(scope="module")
def world(aws):
    """OWN and its fleet, plus OTHER: a vehicle in a different fleet with an assigned
    driver, and that driver's name and email (to prove they never leak)."""
    own_fleet = _fleet_of(aws, OWN)
    assert own_fleet, f"{OWN} has no fleetId in {VEHICLES}; set TEST_VEHICLE_ID"
    paginator = aws["dynamodb"].get_paginator("scan")
    for page in paginator.paginate(
            TableName=DRIVERS, ProjectionExpression="assignedVehicleId, firstName, lastName, email",
            PaginationConfig={"MaxItems": 3000}):
        for d in page.get("Items", []):
            vid = (d.get("assignedVehicleId") or {}).get("S")
            first = (d.get("firstName") or {}).get("S")
            email = (d.get("email") or {}).get("S")
            if not vid or vid == OWN or not first or not email:
                continue
            fleet = _fleet_of(aws, vid)
            if fleet and fleet != own_fleet:
                return {"own_fleet": own_fleet, "other": vid, "other_fleet": fleet,
                        "other_pii": [first, email, (d.get("lastName") or {}).get("S") or first]}
    pytest.fail("no driver-assigned vehicle outside the test vehicle's fleet")


def _driver(groups, vehicle_id=None, fleets=None):
    claims = {"sub": "livecheck-vehicle-context-scope", "cognito:groups": groups}
    if vehicle_id is not None:
        claims["custom:vehicleId"] = vehicle_id
    if fleets is not None:
        claims["custom:fleetIds"] = fleets
    return claims


def _shape(body):
    """Top-level keys only: a failure message must never print driver details."""
    try:
        data = json.loads(body)
    except (TypeError, ValueError):
        return "<not JSON>"
    return sorted(data) if isinstance(data, dict) else type(data).__name__


def _assert_refused(status, body, pii=(), expected=403):
    assert status == expected, f"expected {expected}, got {status} with keys {_shape(body)}"
    if expected == 403:
        exact = json.loads(body) == REFUSAL  # a bool, so pytest can't print the body
        assert exact, f"refusal body has keys {_shape(body)}"
    leaked = sum(value in body for value in pii)
    assert leaked == 0, f"refusal carried {leaked} of the other driver's details"


@pytest.mark.usefixtures("read_only_guard")
class TestDeployedScope:
    pytestmark = _live

    def test_1_trust_flags_are_wired_and_match_ssm(self, aws):
        if TARGET == "local":
            pytest.skip("local target: no deployed function to read")
        env = aws["lambda"].get_function_configuration(FunctionName=FUNCTION)["Environment"]["Variables"]
        for var, kebab in (("AVX_VEHICLE_ID_CLAIM_TRUSTED", "avx-vehicle-id-claim-trusted"),
                           ("AVX_FLEET_IDS_CLAIM_TRUSTED", "avx-fleet-ids-claim-trusted")):
            ssm = aws["ssm"].get_parameter(Name=f"/cvx/{STAGE}/{kebab}")["Parameter"]["Value"]
            assert env.get(var) == ssm, f"{var}={env.get(var)!r} on the function, SSM says {ssm!r}"
            assert ssm == "true", f"{kebab} is {ssm!r}; the driver/fleet arms are closed"

    def test_2_driver_reads_own_vehicle(self, aws, monkeypatch):
        status, body = _invoke(aws, OWN, _driver("vehicle-owner", vehicle_id=OWN), monkeypatch)
        assert status == 200, f"got {status} with keys {_shape(body)}"
        data = json.loads(body)
        assert data["vehicleId"] == OWN and data["vehicle"]["vehicleId"] == OWN

    # Leg 2b was a strict xfail while the drivers lookup read one Scan page (MRDN0000000000015's
    # row is on page 3 of 3, measured 2026-10-24). The scan pages again since the vehicle data
    # branch's vsa-staging-api deploy (2026-10-26 19:00Z), and the leg passed live (XPASS).
    def test_2b_own_vehicle_carries_the_driver_block(self, aws, monkeypatch):
        status, body = _invoke(aws, OWN, _driver("vehicle-owner", vehicle_id=OWN), monkeypatch)
        assert status == 200, f"got {status} with keys {_shape(body)}"
        driver = json.loads(body).get("driver")
        assert isinstance(driver, dict), (
            f"{OWN}'s driver block is {type(driver).__name__}: the scope admitted the caller, but "
            "_load_driver_for_vehicle reads one Scan page of the drivers table and missed the row "
            "(measured 2026-10-24 for MRDN0000000000015). Restoring the scan's pagination is the "
            "vehicle data branch's change; see the issue's merge note.")

    def test_3_driver_is_refused_another_fleets_vehicle(self, aws, world, monkeypatch):
        status, body = _invoke(aws, world["other"], _driver("vehicle-owner", vehicle_id=OWN), monkeypatch)
        _assert_refused(status, body, world["other_pii"])

    def test_4_operator_reads_own_fleet_and_is_refused_others(self, aws, world, monkeypatch):
        op = _driver("fleet-operator", fleets=world["own_fleet"])
        status, body = _invoke(aws, OWN, op, monkeypatch)
        assert status == 200, f"got {status} with keys {_shape(body)}"
        status, body = _invoke(aws, world["other"], op, monkeypatch)
        _assert_refused(status, body, world["other_pii"])
        other_body = body
        status, body = _invoke(aws, MISSING, op, monkeypatch)
        _assert_refused(status, body)
        assert body == other_body, "a missing id must look like another fleet's vehicle"

    def test_5_no_claims_and_no_standing_are_refused(self, aws, world, monkeypatch):
        status, body = _invoke(aws, world["other"], None, monkeypatch)
        _assert_refused(status, body, world["other_pii"], expected=401)
        for claims in ({"sub": "livecheck-vehicle-context-scope"},
                       _driver("service-advisor"),
                       _driver("", vehicle_id=world["other"]),
                       _driver("fleet-driver", vehicle_id=OWN, fleets=world["other_fleet"])):
            status, body = _invoke(aws, world["other"], claims, monkeypatch)
            _assert_refused(status, body, world["other_pii"])

    def test_6_platform_admin_reads_any_vehicle(self, aws, world, monkeypatch):
        status, body = _invoke(aws, world["other"], _driver("platform-admin"), monkeypatch)
        assert status == 200, f"got {status} with keys {_shape(body)}"
        assert json.loads(body)["vehicle"]["vehicleId"] == world["other"]
