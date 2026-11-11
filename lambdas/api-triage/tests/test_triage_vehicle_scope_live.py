"""Live, read-only check of /triage's vehicle-scope reads against the real CMS
vehicles table (LIVE_TESTS=1; the repo conftest gives only `live_aws` tests real
credentials and transport).

Why it exists: the unit tests stub DynamoDB, and a stub cannot fail the way the
service does. The first draft of the fleet path queried `vin-index` with
`ProjectionExpression="vehicleId, fleetId"`; the real index is KEYS_ONLY and
answers that with ValidationException ("does not project [fleetId]"), which would
have turned every fleet-operator triage into a 503 while every stubbed test
passed. These tests make only GetItem and Query calls.

Run:
  LIVE_TESTS=1 CVX_LIVE_STAGE=staging AWS_REGION=us-west-2 \
    .venv/bin/python -m pytest lambdas/api-triage/tests/test_triage_vehicle_scope_live.py -q
"""

from __future__ import annotations

import importlib.util
import os
import sys

import pytest

pytestmark = [
    pytest.mark.live_aws,
    pytest.mark.skipif(os.environ.get("LIVE_TESTS") != "1", reason="live test; set LIVE_TESTS=1"),
]

_HERE = os.path.dirname(__file__)
_spec = importlib.util.spec_from_file_location(
    "api_triage_vehicle_scope_live", os.path.join(_HERE, "..", "triage_vehicle_scope.py"),
)
vs = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = vs  # dataclasses resolve the module through sys.modules
_spec.loader.exec_module(vs)

STAGE = os.environ.get("CVX_LIVE_STAGE", "staging")
TABLE = f"cms-{STAGE}-storage-vehicles"
REGION = os.environ.get("AWS_REGION", "us-west-2")


@pytest.fixture(scope="module")
def ddb():
    import boto3
    return boto3.client("dynamodb", region_name=REGION)


@pytest.fixture(scope="module")
def two_vehicles(ddb):
    """Two vehicles with a VIN, in two different fleets, read from the table."""
    seen: dict[str, dict] = {}
    for page in ddb.get_paginator("scan").paginate(
        TableName=TABLE, ProjectionExpression="vehicleId, vin, fleetId",
        PaginationConfig={"MaxItems": 300},
    ):
        for it in page.get("Items", []):
            vid = it.get("vehicleId", {}).get("S")
            vin = it.get("vin", {}).get("S")
            fleet = it.get("fleetId", {}).get("S")
            if vid and vin and fleet and fleet not in seen:
                seen[fleet] = {"vehicleId": vid, "vin": vin, "fleetId": fleet}
            if len(seen) >= 2:
                a, b = list(seen.values())[:2]
                return a, b
    pytest.skip("need two vehicles in two fleets")


def test_vehicle_bound_own_vin_passes_and_other_vin_is_refused(ddb, two_vehicles):
    own, other = two_vehicles
    claims = {"sub": "live", "custom:vehicleId": own["vehicleId"]}
    assert vs.check(claims, own["vin"], ddb, TABLE).status == vs.STATUS_PASS
    assert vs.check(claims, other["vin"], ddb, TABLE).status == vs.STATUS_DENY_VIN_MISMATCH


def test_fleet_bound_reads_fleet_through_the_real_keys_only_index(ddb, two_vehicles):
    own, other = two_vehicles
    claims = {"sub": "live", "custom:fleetIds": own["fleetId"]}
    assert vs.check(claims, own["vin"], ddb, TABLE).status == vs.STATUS_PASS
    assert vs.check(claims, other["vin"], ddb, TABLE).status == vs.STATUS_DENY_NOT_IN_FLEET


def test_fleet_bound_unknown_vin_is_refused(ddb, two_vehicles):
    own, _ = two_vehicles
    claims = {"sub": "live", "custom:fleetIds": own["fleetId"]}
    assert vs.check(claims, "NOSUCHVIN00000000", ddb, TABLE).status == vs.STATUS_DENY_NOT_IN_FLEET
