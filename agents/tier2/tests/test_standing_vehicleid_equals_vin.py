"""Standing for a car whose vehicleId IS its VIN (issue 2026-11-07-standing-dropped-when-vehicleid-equals-vin).

CMS spec ``2026-10-28-vehicleid-equals-vin`` moved every car to ``vehicleId = vin``: the
registry row, ``custom:vehicleId`` and the drivers table's ``assignedVehicleId`` all carry the
VIN. The resolver and the writer still refused a vehicle id equal to the VIN (a pre-migration
"bad row" rule), so every moved car's new Findings lost ``vehicle_id`` and, because the driver
lookup keys on that value, ``owner_id`` too.

What these tests pin, each an authorization property:

* a moved car resolves to the assigned driver's ``sub``, its fleet, and ``vehicle_id == vin``,
  and the writer persists all three;
* the VIN is still never a party (``owner_id``) or a fleet (``fleet_id``), in the resolver
  and again in the writer;
* a blank vehicle id is still absent, VIN rule lifted or not;
* a live row written without them converges on its next write, including the 5-minute
  diagnostics keep-alive, which otherwise only extends expiry.

Fakes answer by what the code asked for (registry by ``vehicleId`` key, drivers by
``assignedVehicleId``, Cognito by the email in the filter), so a resolver that asked for the
wrong thing gets the wrong answer.
"""
from __future__ import annotations

import logging
import re
import sys
from pathlib import Path
from typing import Any, Optional

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from agents.tier2 import owner_resolver as OR  # noqa: E402
from agents.tier2 import writer as W  # noqa: E402
from agents.tier2.owner_resolver import AssignedDriverDirectory, StandingFacts  # noqa: E402
from agents.tier2.runner import _build_artifact  # noqa: E402

VIN = "MRDN0000001000077"  # test-only VIN; the registry row's vehicleId is the same value
FLEET = "demo-city-public"
EMAIL = "moved.driver@example.com"
DRIVER_SUB = "3333cccc-4444-4555-8666-7777dddd7777"
POOL = "us-west-2_TestP2"  # under 8 chars after the underscore: the publish scanner flags pool ids
AVX_TABLE = "vsa-test-avx-findings"
NOW = "2026-11-07T18:00:00Z"


# ---------------------------------------------------------------------------
# Doubles
# ---------------------------------------------------------------------------


class _Registry:
    """vin-index Query + GetItem, keyed on what was asked. vehicleId == vin by default."""

    def __init__(self, *, vehicle_id: str = VIN, fleet_id: Optional[str] = FLEET):
        self.vehicle_id = vehicle_id
        self.fleet_id = fleet_id
        self.get_item_keys: list[str] = []

    def query(self, **kw: Any) -> dict[str, Any]:
        asked = kw["ExpressionAttributeValues"][":vin"]["S"]
        if asked != VIN:
            return {"Items": []}
        return {"Items": [{"vehicleId": {"S": self.vehicle_id}, "vin": {"S": VIN}}]}

    def get_item(self, **kw: Any) -> dict[str, Any]:
        key = kw["Key"]["vehicleId"]["S"]
        self.get_item_keys.append(key)
        if key != self.vehicle_id:
            return {}
        item: dict[str, Any] = {}
        if self.fleet_id is not None:
            item["fleetId"] = {"S": self.fleet_id}
        return {"Item": item}


class _Drivers:
    def __init__(self, rows: list[tuple[str, Optional[str], Optional[str]]]):
        self.rows = rows

    def scan(self, **kw: Any) -> dict[str, Any]:
        items = []
        for driver_id, vehicle, email in self.rows:
            item: dict[str, Any] = {"driverId": {"S": driver_id}}
            if vehicle is not None:
                item["assignedVehicleId"] = {"S": vehicle}
            if email is not None:
                item["cognitoEmail"] = {"S": email}
            items.append(item)
        return {"Items": items}


class _Pool:
    def __init__(self, users: dict[str, str]):
        self.users = users  # email -> sub
        self.calls = 0

    def list_users(self, **kw: Any) -> dict[str, Any]:
        self.calls += 1
        m = re.fullmatch(r'email = "([^"]*)"', kw["Filter"])
        assert m, kw["Filter"]
        want = m.group(1).lower()
        return {"Users": [
            {"Username": sub, "Enabled": True, "Attributes": [
                {"Name": "sub", "Value": sub},
                {"Name": "email", "Value": email},
                {"Name": "email_verified", "Value": "true"},
            ]}
            for email, sub in self.users.items() if email.lower() == want
        ]}


def _directory(rows, users=None) -> AssignedDriverDirectory:
    return AssignedDriverDirectory(
        ddb_client=_Drivers(rows), drivers_table="cms-test-storage-drivers",
        cognito_client=_Pool(users if users is not None else {EMAIL: DRIVER_SUB}),
        user_pool_id=POOL,
    )


def _resolve(registry: Optional[_Registry] = None,
             directory: Optional[AssignedDriverDirectory] = None) -> StandingFacts:
    return OR.resolve_standing_facts(
        VIN, ddb_client=registry or _Registry(), vehicles_table="cms-test-storage-vehicles",
        drivers=directory,
    )


class _CapturingDdb:
    """No live row on Query, so write_finding takes the PutItem branch."""

    def __init__(self) -> None:
        self.put_items: list[dict[str, Any]] = []
        self.update_calls: list[dict[str, Any]] = []

    def query(self, **kw: Any) -> dict[str, Any]:
        return {"Items": []}

    def put_item(self, **kw: Any) -> dict[str, Any]:
        self.put_items.append(kw)
        return {}

    def update_item(self, **kw: Any) -> dict[str, Any]:
        self.update_calls.append(kw)
        return {}


def _agent_fields() -> dict[str, Any]:
    return {
        "finding": "Battery degradation detected.",
        "severity_hint": "attention",
        "confidence": "high",
        "evidence": [{
            "kind": "battery_soh_reading", "source": "adp",
            "source_ref": "adp_staging_energy_usage#row1", "value": {"soh_pct": 78},
            "computed_at": "2026-11-07T01:45:00Z", "provenance": "live",
        }],
    }


def _vin_finding() -> dict[str, Any]:
    artifact, _ = _build_artifact(VIN, "battery", [{"vin": VIN, "soh": 78}], _agent_fields(),
                                  owner_resolver=None)
    return artifact


# ---------------------------------------------------------------------------
# The resolver
# ---------------------------------------------------------------------------


def test_moved_car_resolves_owner_fleet_and_vehicle_id(caplog):
    """The 2026-11-07 regression: vehicleId == vin must yield all three facts."""
    caplog.set_level(logging.INFO, logger=OR.__name__)
    registry = _Registry()
    facts = _resolve(registry, _directory([("DRV-MOVED-1", VIN, EMAIL)]))

    assert facts.vehicle_id == VIN, "a vehicleId equal to its VIN is the registry invariant"
    assert facts.owner_id == DRIVER_SUB, (
        "the driver lookup keys on the vehicle id; refusing it loses the owner too"
    )
    assert facts.fleet_id == FLEET
    assert registry.get_item_keys == [VIN]
    assert "vehicleId_equals_vin" not in caplog.text


def test_moved_car_with_no_driver_still_projects_vehicle_id():
    facts = _resolve(directory=_directory([]))
    assert facts == StandingFacts(owner_id=None, fleet_id=FLEET, vehicle_id=VIN)


def test_driver_assigned_to_an_old_veh_id_does_not_own_the_moved_car():
    """A drivers row still naming a surrogate id is a different vehicle: no owner."""
    facts = _resolve(directory=_directory([("DRV-STALE", "VEH-TEST-0077", EMAIL)]))
    assert facts.owner_id is None
    assert facts.vehicle_id == VIN


def test_fleet_id_equal_to_the_vin_is_still_refused_on_a_moved_car():
    facts = _resolve(_Registry(fleet_id=VIN), _directory([("DRV-MOVED-1", VIN, EMAIL)]))
    assert facts.fleet_id is None, "a VIN is not a fleet"
    assert facts.vehicle_id == VIN
    assert facts.owner_id == DRIVER_SUB


def test_surrogate_vehicle_id_still_resolves():
    """Rows not (yet) moved keep working: the VIN rule's lift adds, it does not replace."""
    facts = _resolve(_Registry(vehicle_id="VEH-TEST-0077"),
                     _directory([("DRV-OLD-1", "VEH-TEST-0077", EMAIL)]))
    assert facts == StandingFacts(owner_id=DRIVER_SUB, fleet_id=FLEET, vehicle_id="VEH-TEST-0077")


@pytest.mark.parametrize("field", ["fleetId", "owner_id", "anything"])
def test_validate_opaque_refuses_the_vin_by_default(field):
    assert OR._validate_opaque(VIN, vin=VIN, field=field) is None


def test_validate_opaque_allows_the_vin_only_when_asked():
    assert OR._validate_opaque(VIN, vin=VIN, field="vehicleId", may_equal_vin=True) == VIN


@pytest.mark.parametrize("raw", ["", "   "])
def test_validate_opaque_blank_is_absent_even_when_the_vin_is_allowed(raw):
    assert OR._validate_opaque(raw, vin=VIN, field="vehicleId", may_equal_vin=True) is None


def test_only_the_vehicle_id_call_lifts_the_vin_rule():
    """The opt-in is passed at exactly one call site, the vehicleId one."""
    src = Path(OR.__file__).read_text()
    calls = re.findall(r"_validate_opaque\((.*?)\)\n", src, flags=re.S)
    lifted = [c for c in calls if "may_equal_vin=True" in c]
    assert len(lifted) == 1, lifted
    assert "VEHICLE_ID_ATTR" in lifted[0]


# ---------------------------------------------------------------------------
# The writer
# ---------------------------------------------------------------------------


def test_writer_projects_a_vehicle_id_equal_to_the_vin():
    proj = W._standing_projection(_vin_finding(), StandingFacts(DRIVER_SUB, FLEET, VIN))
    assert proj == {"owner_id": DRIVER_SUB, "fleet_id": FLEET, "vehicle_id": VIN}


def test_writer_still_refuses_the_vin_as_owner_or_fleet():
    proj = W._standing_projection(_vin_finding(), StandingFacts(VIN, VIN, VIN))
    assert proj == {"vehicle_id": VIN}


@pytest.mark.parametrize("blank", ["", "   "])
def test_writer_refuses_a_blank_vehicle_id(blank):
    proj = W._standing_projection(_vin_finding(), StandingFacts(None, FLEET, blank))
    assert proj == {"fleet_id": FLEET}


def test_moved_car_finding_is_persisted_with_owner_and_vehicle_id():
    """End to end: the real resolver feeds the real writer; read what was persisted."""
    standing = _resolve(directory=_directory([("DRV-MOVED-1", VIN, EMAIL)]))
    ddb = _CapturingDdb()
    W.write_finding(_vin_finding(), table_name=AVX_TABLE, dynamodb_client=ddb,
                    standing=standing, utc_now_fn=lambda: NOW)
    (call,) = ddb.put_items
    item = call["Item"]
    assert item["vehicle_id"] == {"S": VIN}
    assert item["owner_id"] == {"S": DRIVER_SUB}
    assert item["fleet_id"] == {"S": FLEET}
    assert item["vin"] == {"S": VIN}


# ---------------------------------------------------------------------------
# Repair: a live row written without them converges on its next write
# ---------------------------------------------------------------------------


class _LiveRowNoStanding(_CapturingDdb):
    """A live row with the SAME inputs_hash and severity, carrying only fleet_id.

    That is exactly what the unfixed writer left on every moved car. Same hash and
    severity is the hard case: without a standing difference the writer would no-op
    (or, for diagnostics, only extend expiry), so the owner would never come back.
    """

    def __init__(self, finding: dict[str, Any]):
        super().__init__()
        self.finding = finding

    def query(self, **kw: Any) -> dict[str, Any]:
        return {"Items": [{
            "finding_id": {"S": "AVX-FND-moved-live"},
            "expires_at": {"S": "2099-01-01T00:00:00Z"},
            "inputs_hash": {"S": self.finding["inputs_hash"]},
            "severity": {"S": self.finding["severity"]},
            "agent_id": {"S": self.finding["agent_id"]},
            "fleet_id": {"S": FLEET},
        }]}


@pytest.mark.parametrize("refresh_on_noop", [False, True])
def test_live_row_without_owner_converges_on_the_next_write(refresh_on_noop):
    finding = _vin_finding()
    ddb = _LiveRowNoStanding(finding)
    result = W.write_finding(finding, table_name=AVX_TABLE, dynamodb_client=ddb,
                             standing=StandingFacts(DRIVER_SUB, FLEET, VIN),
                             utc_now_fn=lambda: NOW, refresh_on_noop=refresh_on_noop)
    assert result == {"action": "update", "finding_id": "AVX-FND-moved-live"}, (
        "missing standing must route to UpdateItem, not a no-op or an expiry refresh"
    )
    assert ddb.put_items == []
    (call,) = ddb.update_calls
    values = call["ExpressionAttributeValues"]
    assert values[":owner_id"] == {"S": DRIVER_SUB}
    assert values[":vehicle_id"] == {"S": VIN}
    assert values[":fleet_id"] == {"S": FLEET}
