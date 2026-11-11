"""GET /vehicles/{vehicleId}/context answers only callers with standing over the vehicle.

issues/2026-10-24-vehicle-context-driver-pii-unscoped/. Before this change any signed-in
user could read the assigned driver's name, email, phone and licence for any vehicle id.
The rules are in ``context_scope.py``; these tests drive the real handler over moto
DynamoDB tables (real key schemas), and record every AWS call so "refused" also means
"nothing was read".

The four properties the fix was asked for, and the tests that pin each:
  own car allowed                 test_driver_reads_own_vehicle_with_driver_details
  another fleet's car refused     test_operator_is_refused_another_fleets_vehicle
  missing claims refused          test_missing_or_empty_claims_are_401 / ..._without_standing_are_403
  driver fields absent if refused _assert_refused (every refusal test), and the
                                  "no read before refusal" tests

Run:
    python3 -m pytest lambdas/api-vehicle-context/tests -q
"""
from __future__ import annotations

import ast
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import boto3
import botocore.client
import pytest
from moto import mock_aws

REGION = "us-west-2"
TABLES = {
    "CMS_VEHICLES_TABLE": "t-vehicles",
    "CMS_DRIVERS_TABLE": "t-drivers",
    "CMS_DTC_HISTORY_TABLE": "t-dtc-history",
    "CMS_SERVICE_HISTORY_TABLE": "t-service-history",
}
LAMBDA_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = LAMBDA_DIR.parent.parent

OWN = "VEH-TEST-OWN1"          # in FLEET-A, driven by the "own" driver
OTHER = "VEH-TEST-OTHER"       # in FLEET-B, a different driver
NO_FLEET = "VEH-TEST-NOFLT"    # a row with no fleetId
NEAR = "VEH-TEST-NEAR1"        # in FLEET-AA: a fleet id that contains "FLEET-A"
MISSING = "VEH-TEST-GONE1"     # no row at all

# Driver details that must never appear in a refusal. Fictional values.
OWN_DRIVER = {"driverId": "DRV-TEST-OWN", "firstName": "Ownfirst", "lastName": "Ownlast",
              "email": "own.driver@example.com", "phone": "404-555-0111",
              "licenseState": "GA", "assignedVehicleId": OWN, "safetyScore": 90}
OTHER_DRIVER = {"driverId": "DRV-TEST-OTH", "firstName": "Otherfirst", "lastName": "Otherlast",
                "email": "other.driver@example.com", "phone": "404-555-0199",
                "licenseState": "TX", "assignedVehicleId": OTHER, "safetyScore": 80}
NOFLEET_DRIVER = {"driverId": "DRV-TEST-NOF", "firstName": "Nofleetfirst", "lastName": "Nofleetlast",
                  "email": "nofleet.driver@example.com", "phone": "404-555-0177",
                  "assignedVehicleId": NO_FLEET, "safetyScore": 70}
PII = [v for d in (OWN_DRIVER, OTHER_DRIVER, NOFLEET_DRIVER)
       for k, v in d.items() if k in ("firstName", "lastName", "email", "phone")]

REFUSAL_BODY = {"error": "vehicle not in caller scope"}


def _load_handler():
    for k, v in TABLES.items():
        os.environ[k] = v
    os.environ.pop("LIVE_STATE_FN_NAME", None)
    spec = importlib.util.spec_from_file_location(
        "vehicle_context_handler_scope", LAMBDA_DIR / "handler.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


h = _load_handler()
cs = h.context_scope


def _event(vehicle_id, claims=None):
    event = {"pathParameters": {"vehicleId": vehicle_id}}
    if claims is not None:
        event["requestContext"] = {"authorizer": {"claims": claims}}
    return event


def driver_claims(vehicle_id=OWN, groups="vehicle-owner", **extra):
    return {"sub": "sub-driver", "cognito:groups": groups, "custom:vehicleId": vehicle_id, **extra}


def operator_claims(fleets="FLEET-A", groups="fleet-operator", **extra):
    return {"sub": "sub-operator", "cognito:groups": groups, "custom:fleetIds": fleets, **extra}


def _body(resp):
    return json.loads(resp["body"])


@pytest.fixture(autouse=True)
def _trusted(monkeypatch):
    # The deployed staging state (both flags "true", contract § 5.2). Tests of the
    # untrusted default override these.
    monkeypatch.setenv(cs.VEHICLE_ID_CLAIM_TRUSTED_ENV, "true")
    monkeypatch.setenv(cs.FLEET_IDS_CLAIM_TRUSTED_ENV, "true")


@pytest.fixture
def world(monkeypatch):
    for var, val in (("AWS_ACCESS_KEY_ID", "testing"), ("AWS_SECRET_ACCESS_KEY", "testing"),
                     ("AWS_SESSION_TOKEN", "testing"), ("AWS_DEFAULT_REGION", REGION)):
        monkeypatch.setenv(var, val)
    with mock_aws():
        res = boto3.resource("dynamodb", region_name=REGION)

        def table(name, hash_key, range_key=None, range_type="S"):
            ks = [{"AttributeName": hash_key, "KeyType": "HASH"}]
            ad = [{"AttributeName": hash_key, "AttributeType": "S"}]
            if range_key:
                ks.append({"AttributeName": range_key, "KeyType": "RANGE"})
                ad.append({"AttributeName": range_key, "AttributeType": range_type})
            return res.create_table(TableName=name, KeySchema=ks, AttributeDefinitions=ad,
                                    BillingMode="PAY_PER_REQUEST")

        vehicles = table(TABLES["CMS_VEHICLES_TABLE"], "vehicleId")
        vehicles.put_item(Item={"vehicleId": OWN, "vin": "MRDN0000000000011", "make": "Meridian",
                                "fleetId": "FLEET-A", "connectionStatus": "connected"})
        vehicles.put_item(Item={"vehicleId": OTHER, "vin": "MRDN0000000000022", "make": "Meridian",
                                "fleetId": "FLEET-B", "connectionStatus": "connected"})
        vehicles.put_item(Item={"vehicleId": NO_FLEET, "vin": "MRDN0000000000033", "make": "Meridian",
                                "connectionStatus": "connected"})
        vehicles.put_item(Item={"vehicleId": NEAR, "vin": "MRDN0000000000044", "make": "Meridian",
                                "fleetId": "FLEET-AA", "connectionStatus": "connected"})
        drivers = table(TABLES["CMS_DRIVERS_TABLE"], "driverId")
        for d in (OWN_DRIVER, OTHER_DRIVER, NOFLEET_DRIVER):
            drivers.put_item(Item=d)
        table(TABLES["CMS_DTC_HISTORY_TABLE"], "vehicleId", "timestamp", "N")
        table(TABLES["CMS_SERVICE_HISTORY_TABLE"], "vehicleId", "serviceDate")
        monkeypatch.setattr(h, "_ddb", None)
        yield res


@pytest.fixture
def calls(world, monkeypatch):
    """Every AWS API call the handler makes, as (service, operation, TableName)."""
    rec = []
    orig = botocore.client.BaseClient._make_api_call

    def recording(self, operation_name, api_params):
        rec.append((self.meta.service_model.service_name, operation_name,
                    api_params.get("TableName")))
        return orig(self, operation_name, api_params)

    monkeypatch.setattr(botocore.client.BaseClient, "_make_api_call", recording)
    return rec


def _assert_refused(resp, status=403):
    assert resp["statusCode"] == status, f"expected {status}, got {resp['statusCode']}: {resp['body']}"
    body = _body(resp)
    if status == 403:
        assert body == REFUSAL_BODY, f"a refusal must carry only the error, got {body}"
    else:
        assert body == {"error": "no caller claims"}, f"a 401 must carry only the error, got {body}"
    assert "driver" not in body and "vehicle" not in body
    for value in PII:
        assert value not in resp["body"], f"refusal leaked driver detail {value!r}"


def _tables_read(calls):
    return [t for (svc, _op, t) in calls if svc == "dynamodb"]


# --------------------------------------------------------------------------
# Admitted callers
# --------------------------------------------------------------------------

def test_driver_reads_own_vehicle_with_driver_details(calls):
    resp = h.handler(_event(OWN, driver_claims(OWN)), None)
    assert resp["statusCode"] == 200, resp["body"]
    body = _body(resp)
    assert body["vehicle"]["vehicleId"] == OWN
    assert body["driver"]["firstName"] == OWN_DRIVER["firstName"]
    assert body["driver"]["email"] == OWN_DRIVER["email"]


@pytest.mark.parametrize("groups", ["vehicle-owner", "consumer", "fleet-driver", "fleet-operator"])
def test_every_vehicle_standing_group_admits_the_own_vehicle(calls, groups):
    resp = h.handler(_event(OWN, driver_claims(OWN, groups=groups)), None)
    assert resp["statusCode"] == 200, resp["body"]
    assert _body(resp)["driver"]["driverId"] == OWN_DRIVER["driverId"]


def test_operator_reads_a_vehicle_in_its_fleet(calls):
    resp = h.handler(_event(OWN, operator_claims("FLEET-A")), None)
    assert resp["statusCode"] == 200, resp["body"]
    assert _body(resp)["driver"]["driverId"] == OWN_DRIVER["driverId"]


def test_operator_with_several_fleets_reads_each(calls):
    for vid, driver in ((OWN, OWN_DRIVER), (OTHER, OTHER_DRIVER)):
        resp = h.handler(_event(vid, operator_claims("FLEET-A,FLEET-B")), None)
        assert resp["statusCode"] == 200, resp["body"]
        assert _body(resp)["driver"]["driverId"] == driver["driverId"]


def test_platform_admin_reads_any_vehicle(calls):
    resp = h.handler(_event(OTHER, {"sub": "sub-admin", "cognito:groups": "platform-admin"}), None)
    assert resp["statusCode"] == 200, resp["body"]
    assert _body(resp)["driver"]["driverId"] == OTHER_DRIVER["driverId"]


def test_platform_admin_needs_neither_trust_flag(calls, monkeypatch):
    monkeypatch.delenv(cs.VEHICLE_ID_CLAIM_TRUSTED_ENV, raising=False)
    monkeypatch.delenv(cs.FLEET_IDS_CLAIM_TRUSTED_ENV, raising=False)
    resp = h.handler(_event(OTHER, {"sub": "sub-admin", "cognito:groups": "platform-admin"}), None)
    assert resp["statusCode"] == 200, resp["body"]


def test_standing_is_a_set_driver_arm_admits_outside_the_operators_fleets(calls):
    # Contract § 3: a fleet operator who also drives a vehicle outside its claimed fleets
    # reads that vehicle through the driver arm. Each fleet arm alone would refuse both.
    for vid, driver in ((NO_FLEET, NOFLEET_DRIVER), (OTHER, OTHER_DRIVER)):
        claims = operator_claims("FLEET-A", **{"custom:vehicleId": vid})
        resp = h.handler(_event(vid, claims), None)
        assert resp["statusCode"] == 200, (vid, resp["body"])
        assert _body(resp)["driver"]["driverId"] == driver["driverId"]
    # The operator's own fleet still reads through the fleet arm.
    claims = operator_claims("FLEET-A", **{"custom:vehicleId": NO_FLEET})
    assert h.handler(_event(OWN, claims), None)["statusCode"] == 200


def test_admin_and_own_vehicle_get_404_for_a_missing_vehicle(calls):
    admin = {"sub": "sub-admin", "cognito:groups": "platform-admin"}
    assert h.handler(_event(MISSING, admin), None)["statusCode"] == 404
    assert h.handler(_event(MISSING, driver_claims(MISSING)), None)["statusCode"] == 404


@pytest.mark.parametrize("groups", [
    "fleet-operator",
    "other fleet-operator",
    "other,fleet-operator",
    '["other","fleet-operator"]',
    ["other", "fleet-operator"],
])
def test_group_claim_shapes_are_all_read(calls, groups):
    resp = h.handler(_event(OWN, operator_claims("FLEET-A", groups=groups)), None)
    assert resp["statusCode"] == 200, (groups, resp["body"])


@pytest.mark.parametrize("fleets", ["FLEET-A", '["FLEET-A"]', "FLEET-X, FLEET-A", ["FLEET-A"]])
def test_fleet_claim_shapes_are_all_read(calls, fleets):
    resp = h.handler(_event(OWN, operator_claims(fleets)), None)
    assert resp["statusCode"] == 200, (fleets, resp["body"])


def test_trust_flag_is_case_and_space_insensitive(calls, monkeypatch):
    monkeypatch.setenv(cs.VEHICLE_ID_CLAIM_TRUSTED_ENV, " TRUE ")
    assert h.handler(_event(OWN, driver_claims(OWN)), None)["statusCode"] == 200


def test_vehicle_claim_is_trimmed(calls):
    assert h.handler(_event(OWN, driver_claims(f"  {OWN} ")), None)["statusCode"] == 200


# --------------------------------------------------------------------------
# Refused callers: 403/401, no driver details, nothing read
# --------------------------------------------------------------------------

def test_driver_is_refused_another_vehicle_and_nothing_is_read(calls):
    resp = h.handler(_event(OTHER, driver_claims(OWN)), None)
    _assert_refused(resp)
    assert _tables_read(calls) == [], f"a refused driver must read no table: {calls}"


def test_operator_is_refused_another_fleets_vehicle(calls):
    resp = h.handler(_event(OTHER, operator_claims("FLEET-A")), None)
    _assert_refused(resp)
    # Only the vehicle row, to learn its fleet. Never the drivers table.
    assert _tables_read(calls) == [TABLES["CMS_VEHICLES_TABLE"]], calls


def test_operator_gets_the_same_403_for_a_missing_vehicle(calls):
    other_fleet = h.handler(_event(OTHER, operator_claims("FLEET-A")), None)
    missing = h.handler(_event(MISSING, operator_claims("FLEET-A")), None)
    _assert_refused(missing)
    assert missing["statusCode"] == other_fleet["statusCode"] == 403
    assert missing["body"] == other_fleet["body"], "missing and other-fleet must look the same"


def test_operator_is_refused_a_vehicle_with_no_fleet(calls):
    _assert_refused(h.handler(_event(NO_FLEET, operator_claims("FLEET-A")), None))


@pytest.mark.parametrize("fleets", ["FLEET-A", "FLEET-AAA", "LEET-A", "FLEET-", "fleet-aa",
                                    "Fleet-AA", " FLEET-A A"])
def test_fleet_ids_must_match_the_row_exactly(calls, fleets):
    # NEAR sits in FLEET-AA; none of these claims names that fleet.
    _assert_refused(h.handler(_event(NEAR, operator_claims(fleets)), None))


def test_fleet_ids_without_the_operator_group_admit_nothing(calls):
    # A driver's token can carry custom:fleetIds; a driver sees only the assigned
    # vehicle (contract § 3). OTHER is in FLEET-B, which the claim names.
    claims = driver_claims(OWN, groups="fleet-driver", **{"custom:fleetIds": "FLEET-A,FLEET-B"})
    _assert_refused(h.handler(_event(OTHER, claims), None))
    assert _tables_read(calls) == [], calls
    assert h.handler(_event(OWN, claims), None)["statusCode"] == 200


@pytest.mark.parametrize("groups", ["", "service-advisor", "technician", "fleet-viewer",
                                    "dealer-admin", "kiosk", "admin", "platform-admins",
                                    "Platform-Admin", "PLATFORM-ADMIN", "Vehicle-Owner"])
def test_vehicle_claim_without_a_vehicle_standing_group_is_refused(calls, groups):
    _assert_refused(h.handler(_event(OWN, driver_claims(OWN, groups=groups)), None))
    assert _tables_read(calls) == [], calls


def test_fleet_arm_needs_the_operator_group_not_a_service_role(calls):
    # SM7 (security review cycle 1): a service role holding a trusted fleet claim is not a
    # fleet manager. And the gate is the operator group, not the absence of a service role.
    _assert_refused(h.handler(_event(OWN, operator_claims("FLEET-A", groups="service-advisor")), None))
    _assert_refused(h.handler(_event(OWN, operator_claims("FLEET-A", groups="technician")), None))
    both = operator_claims("FLEET-A", groups="service-advisor,fleet-operator")
    assert h.handler(_event(OWN, both), None)["statusCode"] == 200


@pytest.mark.parametrize("groups", ['{"platform-admin": true}', '{"platform-admin": 1}',
                                    '[["platform-admin"]]', '[{"platform-admin": 1}]'])
def test_group_claim_that_is_not_a_list_of_names_grants_nothing(calls, groups):
    # Security review SS1/SS2. A brace string is not parsed as JSON at all (only "[" is), and a
    # JSON list's non-string members are not group names. Either way: no grant, and no crash
    # (a crash would be a 500, so the call is wrapped to make that an assertion failure).
    try:
        resp = h.handler(_event(OWN, {"sub": "x", "cognito:groups": groups}), None)
    except Exception as exc:  # noqa: BLE001
        raise AssertionError(f"handler raised on cognito:groups={groups!r}: "
                             f"{type(exc).__name__}: {exc}") from exc
    _assert_refused(resp)
    assert _tables_read(calls) == [], calls


def test_mixed_json_group_list_keeps_only_the_names():
    try:
        groups = cs.extract_groups({"cognito:groups": '[["x"], "fleet-operator", 3]'})
    except Exception as exc:  # noqa: BLE001
        raise AssertionError(f"extract_groups raised: {type(exc).__name__}: {exc}") from exc
    assert groups == {"fleet-operator"}


def test_admitted_read_is_logged_with_standing_and_sub(calls, caplog):
    caplog.set_level("INFO")
    h.handler(_event(OWN, operator_claims("FLEET-A")), None)
    lines = [r.getMessage() for r in caplog.records if "vehicle-context: admit" in r.getMessage()]
    assert len(lines) == 1, lines
    assert "standing=fleet" in lines[0] and "sub-operator" in lines[0] and OWN in lines[0]
    for value in PII:
        assert value not in lines[0]


@pytest.mark.parametrize("groups", ["service-advisor", "technician", "service-advisor technician"])
def test_service_roles_alone_are_refused(calls, groups):
    # Decision: the findings routes' all-VIN read for service roles is not carried
    # over to a route that returns driver contact details (context_scope.py).
    _assert_refused(h.handler(_event(OWN, {"sub": "sub-svc", "cognito:groups": groups}), None))
    assert _tables_read(calls) == [], calls


@pytest.mark.parametrize("claim", ["veh-test-own1", "VEH-TEST-OWN", "VEH TEST OWN1", "",
                                   "   ", "VEH-TEST-OWN1/../x", 12345, None])
def test_vehicle_claim_must_equal_the_path_id_exactly(calls, claim):
    _assert_refused(h.handler(_event(OWN, driver_claims(claim)), None))


@pytest.mark.parametrize("claim", ["VEH TEST/X", "VEH-TEST-OWN1;", "x" * 65])
def test_malformed_vehicle_claim_is_refused_even_when_it_equals_the_path(calls, claim):
    # A claim that fails the vehicle-id shape grants nothing, even for the identical
    # path id (which would otherwise answer 404, telling the caller it is "theirs").
    _assert_refused(h.handler(_event(claim, driver_claims(claim)), None))
    assert _tables_read(calls) == [], calls


@pytest.mark.parametrize("fleets", ["", ",", " , ", "[]", '[""]', "---", None, []])
def test_blank_fleet_claim_is_no_membership(calls, fleets):
    _assert_refused(h.handler(_event(OWN, operator_claims(fleets)), None))
    assert _tables_read(calls) == [], calls


@pytest.mark.parametrize("value", [None, "", "false", "1", "yes", "True!", "truee"])
def test_untrusted_vehicle_claim_is_refused(calls, monkeypatch, value):
    if value is None:
        monkeypatch.delenv(cs.VEHICLE_ID_CLAIM_TRUSTED_ENV, raising=False)
    else:
        monkeypatch.setenv(cs.VEHICLE_ID_CLAIM_TRUSTED_ENV, value)
    _assert_refused(h.handler(_event(OWN, driver_claims(OWN)), None))
    assert _tables_read(calls) == [], calls


@pytest.mark.parametrize("value", [None, "", "false", "1", "yes"])
def test_untrusted_fleet_claim_is_refused(calls, monkeypatch, value):
    if value is None:
        monkeypatch.delenv(cs.FLEET_IDS_CLAIM_TRUSTED_ENV, raising=False)
    else:
        monkeypatch.setenv(cs.FLEET_IDS_CLAIM_TRUSTED_ENV, value)
    _assert_refused(h.handler(_event(OWN, operator_claims("FLEET-A")), None))
    assert _tables_read(calls) == [], calls


def test_each_trust_flag_gates_only_its_own_claim(calls, monkeypatch):
    # The fleet flag must not open the vehicle arm, and the vehicle flag must not open
    # the fleet arm.
    monkeypatch.delenv(cs.VEHICLE_ID_CLAIM_TRUSTED_ENV, raising=False)
    _assert_refused(h.handler(_event(OWN, driver_claims(OWN)), None))
    assert h.handler(_event(OWN, operator_claims("FLEET-A")), None)["statusCode"] == 200
    monkeypatch.setenv(cs.VEHICLE_ID_CLAIM_TRUSTED_ENV, "true")
    monkeypatch.delenv(cs.FLEET_IDS_CLAIM_TRUSTED_ENV, raising=False)
    _assert_refused(h.handler(_event(OWN, operator_claims("FLEET-A")), None))
    assert h.handler(_event(OWN, driver_claims(OWN)), None)["statusCode"] == 200


@pytest.mark.parametrize("event", [
    {"pathParameters": {"vehicleId": OWN}},
    {"pathParameters": {"vehicleId": OWN}, "requestContext": {}},
    {"pathParameters": {"vehicleId": OWN}, "requestContext": {"authorizer": {}}},
    {"pathParameters": {"vehicleId": OWN}, "requestContext": {"authorizer": {"claims": None}}},
    {"pathParameters": {"vehicleId": OWN}, "requestContext": {"authorizer": {"claims": "x"}}},
    _event(OWN, {}),
    _event(OWN, {"cognito:groups": "platform-admin", "custom:vehicleId": OWN}),  # no sub
    _event(OWN, {"sub": "  ", "cognito:groups": "platform-admin"}),
    # Claims belong under requestContext.authorizer.claims; an authorizer dict holding them
    # directly is not read as claims.
    {"pathParameters": {"vehicleId": OWN}, "requestContext": {"authorizer": {
        "sub": "x", "cognito:groups": "platform-admin", "custom:vehicleId": OWN}}},
])
def test_missing_or_empty_claims_are_401(calls, event):
    _assert_refused(h.handler(event, None), status=401)
    assert _tables_read(calls) == [], calls


@pytest.mark.parametrize("groups", [None, "admin", "platform-admins", "Platform-Admin",
                                    "platform_admin", "admin platform"])
def test_claims_without_standing_are_403(calls, groups):
    claims = {"sub": "sub-nobody"}
    if groups is not None:
        claims["cognito:groups"] = groups
    _assert_refused(h.handler(_event(OWN, claims), None))
    assert _tables_read(calls) == [], calls


@pytest.mark.parametrize("claims", [None, {}])
def test_identity_comes_only_from_the_authorizer(calls, claims):
    # Claims in the query string, the headers or the body are ignored.
    admin = {"sub": "x", "cognito:groups": "platform-admin", "custom:vehicleId": OWN}
    event = _event(OWN, claims)
    event["queryStringParameters"] = admin
    event["headers"] = {k.replace(":", "-"): v for k, v in admin.items()}
    event["body"] = json.dumps(admin)
    _assert_refused(h.handler(event, None), status=401)
    assert _tables_read(calls) == [], calls


def test_refusal_happens_before_any_lookup(world, monkeypatch):
    # A second check of order that does not rely on the call recorder: every loader
    # raises, so reaching one would turn the 403 into an exception.
    def boom(*_a, **_k):
        raise AssertionError("a loader ran for a refused caller")

    for name in ("_load_vehicle", "_load_driver_for_vehicle", "_load_active_dtcs",
                 "_load_first_scheduled_service", "_load_live_connection_state"):
        monkeypatch.setattr(h, name, boom)
    _assert_refused(h.handler(_event(OTHER, driver_claims(OWN)), None))
    _assert_refused(h.handler(_event(OWN, {"sub": "x"}), None))
    _assert_refused(h.handler(_event(OWN), None), status=401)


def test_operator_refusal_does_not_load_the_driver(world, monkeypatch):
    def boom(*_a, **_k):
        raise AssertionError("driver lookup ran for a refused caller")

    monkeypatch.setattr(h, "_load_driver_for_vehicle", boom)
    _assert_refused(h.handler(_event(OTHER, operator_claims("FLEET-A")), None))
    _assert_refused(h.handler(_event(MISSING, operator_claims("FLEET-A")), None))


def test_refusal_log_names_the_standing_but_no_driver_details(calls, caplog):
    caplog.set_level("WARNING")
    h.handler(_event(OTHER, operator_claims("FLEET-A")), None)
    lines = [r.getMessage() for r in caplog.records if "vehicle-context: refused" in r.getMessage()]
    assert len(lines) == 1, lines
    assert "standing=fleet" in lines[0] and OTHER in lines[0]
    for value in PII:
        assert value not in lines[0]


# --------------------------------------------------------------------------
# The copies of the claim parsing must agree with the shared library they repeat
# --------------------------------------------------------------------------

def _load_avx_scope():
    spec = importlib.util.spec_from_file_location(
        "avx_scope_for_context_parity", REPO_ROOT / "lambdas" / "_lib" / "avx_scope.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_GROUP_INPUTS = ["", "  ", "platform-admin", "Platform-Admin", "fleet-operator service-advisor", "a,b , c",
                 '["fleet-operator","x"]', "[not json", ["vehicle-owner"], []]
_FLEET_INPUTS = ["", ",", " , ", "FLEET-A", "FLEET-A,FLEET-B", " FLEET-A , ,FLEET-B ",
                 '["FLEET-A","FLEET-B"]', '[""]', "[]", '["  "]', "---", ["FLEET-A", ""], [],
                 '[" FLEET-A "]', '["FLEET-A ,FLEET-B"]']
_FLAG_INPUTS = [None, "", "true", "TRUE", " true ", "false", "1", "yes", "true!"]


def test_env_var_names_match_avx_scope():
    avx = _load_avx_scope()
    assert cs.VEHICLE_ID_CLAIM_TRUSTED_ENV == avx.VEHICLE_ID_CLAIM_TRUSTED_ENV
    assert cs.FLEET_IDS_CLAIM_TRUSTED_ENV == avx.FLEET_IDS_CLAIM_TRUSTED_ENV


@pytest.mark.parametrize("flag", _FLAG_INPUTS)
def test_claim_trust_matches_avx_scope(monkeypatch, flag):
    avx = _load_avx_scope()
    for env in (cs.VEHICLE_ID_CLAIM_TRUSTED_ENV, cs.FLEET_IDS_CLAIM_TRUSTED_ENV):
        if flag is None:
            monkeypatch.delenv(env, raising=False)
        else:
            monkeypatch.setenv(env, flag)
        assert cs.claim_is_trusted(env) == avx.claim_is_trusted(env), (env, flag)


@pytest.mark.parametrize("raw", _GROUP_INPUTS)
def test_group_parsing_matches_avx_scope(raw):
    avx = _load_avx_scope()
    claims = {"cognito:groups": raw}
    assert cs.extract_groups(claims) == avx._extract_groups(claims)


@pytest.mark.parametrize("flag", ["true", "false"])
@pytest.mark.parametrize("raw", _FLEET_INPUTS)
def test_fleet_claim_parsing_matches_avx_scope(monkeypatch, raw, flag):
    avx = _load_avx_scope()
    monkeypatch.setenv(cs.FLEET_IDS_CLAIM_TRUSTED_ENV, flag)
    claims = {"custom:fleetIds": raw}
    ours = [str(v) for v in cs.trusted_fleet_ids(claims)]
    theirs = [str(v) for v in avx._trusted_claim_list(
        claims, "custom:fleetIds", avx.FLEET_IDS_CLAIM_TRUSTED_ENV)]
    assert ours == theirs, (raw, flag)


def test_vehicle_standing_groups_match_findings_me():
    """``_VEHICLE_STANDING_GROUPS`` in the findings handler, read from its source."""
    src = (REPO_ROOT / "lambdas" / "api-avx-findings" / "handler.py").read_text()
    found = []
    for node in ast.walk(ast.parse(src)):
        if (isinstance(node, ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
                and node.targets[0].id == "_VEHICLE_STANDING_GROUPS"):
            call = node.value
            assert isinstance(call, ast.Call) and call.func.id == "frozenset"
            found.append(frozenset(ast.literal_eval(call.args[0])))
    assert len(found) == 1, "expected exactly one _VEHICLE_STANDING_GROUPS assignment"
    assert cs.VEHICLE_STANDING_GROUPS == found[0]


# --------------------------------------------------------------------------
# Packaging: the asset is this directory, so the handler must import from it alone
# --------------------------------------------------------------------------

def test_handler_imports_with_only_its_own_directory_on_the_path():
    # `-I` drops PYTHONPATH, the user site and the current directory, so the only
    # source directory on the path is the Lambda's own, as in its deployed asset.
    code = (
        f"import sys; sys.path.insert(0, {str(LAMBDA_DIR)!r}); "
        "import handler, context_scope; "
        f"assert context_scope.__file__.startswith({str(LAMBDA_DIR)!r}); "
        f"assert not set(sys.path) & {{{str(REPO_ROOT)!r}, {str(REPO_ROOT / 'lambdas')!r}}}, sys.path; "
        "print('ok')"
    )
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    env.update({"AWS_DEFAULT_REGION": REGION, "PYTHONDONTWRITEBYTECODE": "1"})
    # -B: -I ignores PYTHONDONTWRITEBYTECODE, and the run must leave no __pycache__ behind.
    out = subprocess.run([sys.executable, "-I", "-B", "-c", code], capture_output=True, text=True,
                         env=env, cwd="/", timeout=60)
    assert out.returncode == 0 and out.stdout.strip() == "ok", out.stderr
