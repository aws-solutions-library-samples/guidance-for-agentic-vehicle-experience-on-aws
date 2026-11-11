"""A moved car's Finding (vehicleId == VIN) reaches its assigned driver.

Issue ``issues/2026-11-07-standing-dropped-when-vehicleid-equals-vin/``. After CMS spec
``2026-10-28-vehicleid-equals-vin`` the driver's trusted ``custom:vehicleId`` is the VIN, and the
Tier 2 writer now projects ``vehicle_id`` equal to the VIN. This pins the read side against what
the REAL writer persists (not a hand-built row): ``/findings/me`` admits the Finding as
``fleet_driver`` and the approve check admits the driver, while a caller holding a different
vehicle claim gets neither.
"""
from __future__ import annotations

import json
import os
import sys
from typing import Any
from unittest.mock import patch

import pytest

_LAMBDA_ROOT = os.path.join(os.path.dirname(__file__), "..")
_LAMBDAS_ROOT = os.path.dirname(_LAMBDA_ROOT)
_REPO_ROOT = os.path.abspath(os.path.join(_LAMBDA_ROOT, "..", ".."))
for _p in (_LAMBDA_ROOT, _LAMBDAS_ROOT, _REPO_ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import handler as h  # noqa: E402
from agents.tier2 import writer as W  # noqa: E402
from agents.tier2.owner_resolver import StandingFacts  # noqa: E402
from agents.tier2.runner import _build_artifact  # noqa: E402

VIN = "MRDN0000001000078"
OTHER_VIN = "MRDN0000001000079"
FLEET = "demo-city-public"
DRIVER_SUB = "3333cccc-4444-4555-8666-7777dddd7778"


@pytest.fixture(autouse=True)
def _trust_vehicle_claim(monkeypatch):
    monkeypatch.setenv("AVX_VEHICLE_ID_CLAIM_TRUSTED", "true")


class _CapturingDdb:
    def __init__(self) -> None:
        self.put_items: list[dict[str, Any]] = []

    def query(self, **kw: Any) -> dict[str, Any]:
        return {"Items": []}

    def put_item(self, **kw: Any) -> dict[str, Any]:
        self.put_items.append(kw)
        return {}


def _persisted_moved_car_finding(standing: StandingFacts | None = None) -> dict[str, Any]:
    """What the writer stores for a moved car, read back as the API's resource client would."""
    if standing is None:
        standing = StandingFacts(DRIVER_SUB, FLEET, VIN)
    artifact, _ = _build_artifact(
        VIN, "battery", [{"vin": VIN, "soh": 78}],
        {"finding": "Battery degradation detected.", "severity_hint": "attention",
         "confidence": "high",
         "evidence": [{"kind": "battery_soh_reading", "source": "adp",
                       "source_ref": "adp_staging_energy_usage#row1", "value": {"soh_pct": 78},
                       "computed_at": "2026-11-07T01:45:00Z", "provenance": "live"}]},
        owner_resolver=None,
    )
    ddb = _CapturingDdb()
    W.write_finding(artifact, table_name="vsa-test-avx-findings", dynamodb_client=ddb,
                    standing=standing)
    (call,) = ddb.put_items
    item = W._from_ddb_item(call["Item"])
    assert item.get("vehicle_id") == standing.vehicle_id, "precondition: what the writer projected"
    assert item["vin"] == VIN
    return item


def _driver_claims(vehicle_id: str) -> dict[str, Any]:
    return {"sub": DRIVER_SUB, "cognito:groups": json.dumps(["fleet-driver"]),
            "custom:vehicleId": vehicle_id}


def _findings_me(claims: dict[str, Any], by_vehicle: list[dict[str, Any]]):
    event = {"httpMethod": "GET", "resource": "/findings/me", "pathParameters": {},
             "queryStringParameters": {}, "requestContext": {"authorizer": {"claims": claims}},
             "body": None}
    with patch.object(h, "_list_findings_by_vehicle", return_value=(by_vehicle, None)), \
         patch.object(h, "_list_findings_by_owner", return_value=([], None)), \
         patch.object(h, "_list_findings_by_fleet", return_value=([], None)):
        resp = h.handler(event, None)
    return resp["statusCode"], json.loads(resp["body"])


def test_findings_me_admits_a_moved_cars_finding_as_fleet_driver():
    item = _persisted_moved_car_finding()
    status, body = _findings_me(_driver_claims(VIN), [item])
    assert status == 200
    assert [f["finding_id"] for f in body["findings"]] == [item["finding_id"]]
    assert body["admitted_by"] == {item["finding_id"]: ["fleet_driver"]}


def test_findings_me_refuses_a_driver_of_another_car():
    item = _persisted_moved_car_finding()
    status, body = _findings_me(_driver_claims(OTHER_VIN), [item])
    assert status == 200
    assert body["findings"] == []


def test_approve_scope_admits_the_assigned_driver_of_a_moved_car():
    item = _persisted_moved_car_finding()
    assert h._check_approve_scope(_driver_claims(VIN), item) == "user"


def test_approve_scope_refuses_a_driver_of_another_car():
    item = _persisted_moved_car_finding()
    assert h._check_approve_scope(_driver_claims(OTHER_VIN), item) is None


# ---------------------------------------------------------------------------
# Fail closed when the registry did not vouch for the car (review cycle 1 W1)
# ---------------------------------------------------------------------------
#
# Since the migration the driver's claim EQUALS the Finding's VIN, so the projected
# vehicle_id is the only proof the registry knows the car. A row without it (registry
# fault, VIN not in the registry, projection removed as stale) must admit nobody by
# this route, even though claim == vin. These are the shapes the writer really stores.

_UNVOUCHED = [
    pytest.param(StandingFacts(DRIVER_SUB, FLEET, None), id="owner-and-fleet-but-no-vehicle_id"),
    pytest.param(StandingFacts(), id="nothing-resolved"),
]


@pytest.mark.parametrize("standing", _UNVOUCHED)
def test_approve_refuses_a_vin_claim_when_vehicle_id_is_absent(standing):
    item = _persisted_moved_car_finding(standing)
    assert "vehicle_id" not in item
    assert h._check_approve_scope(_driver_claims(VIN), item) is None


@pytest.mark.parametrize("standing", _UNVOUCHED)
def test_findings_me_admits_nothing_for_a_vin_claim_when_vehicle_id_is_absent(standing):
    item = _persisted_moved_car_finding(standing)
    status, body = _findings_me(_driver_claims(VIN), [item])
    assert status == 200
    assert body["findings"] == []
    assert body["admitted_by"] == {}
