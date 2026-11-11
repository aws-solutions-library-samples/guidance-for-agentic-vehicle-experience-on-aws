"""The AVX persona seed must not write vehicles or drivers into the retired CMS fleets.

CMS issues/2026-10-21-staging-empty-fleets/: eight CMS staging fleets were emptied on
2026-10-06 (CMS->ADP stage 1 moved every vehicle into four demo fleets) and retired on
2026-10-21. ``scripts/seed-persona-users.py`` (``make seed-staging``) writes Marcus's
``MRDN0000000000015`` and Priya's ``MRDN0000002000013`` with a wholesale ``put_item``, and Priya's
driver row the same way, so the fleet ids in this file decide where those rows land on
every run. They named ``FLEET-MERIDIAN-ONBOARD`` and ``WPR-RENTAL``.

Checked at the write boundary (the item handed to ``put_item``), not just in the persona
dicts. ``MRDCW01H8PC00V7RD``'s vehicle row is pinned too: it named ``VO-OEM``, a fleet id that has
never existed, and would have been moved out of demo-city-public the same way. The persona
DRIVER rows' never-existing fleet ids (``MERIDIAN-OEM``, ``VO-OEM``, ``SA-FLEET``,
``TECH-FLEET``) are a separate backlog item and deliberately not asserted.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

SEED = Path(__file__).resolve().parents[1] / "seed-persona-users.py"

# The eight CMS fleets retired by CMS issues/2026-10-21-staging-empty-fleets/.
RETIRED_FLEETS = frozenset({
    "FLEET-1780002982", "FLEET-DEMO-PUBLIC", "FLEET-MERIDIAN-EV-OFFBOARD",
    "FLEET-MERIDIAN-OFFBOARD", "FLEET-MERIDIAN-ONBOARD", "WPR-RENTAL",
    "flt-cs-demo-001", "flt-meridian-range-001",
})

# Where each repointed row lives on CMS staging (read 2026-10-21).
EXPECTED_VEHICLE_FLEETS = {
    "MRDN0000000000015": "demo-city-public",
    "MRDN0000002000013": "demo-rental-carshare",
    # Not one of the eight: its `VO-OEM` never existed (review cycle 1 W1).
    "MRDCW01H8PC00V7RD": "demo-city-public",
}
EXPECTED_DRIVER_FLEETS = {"DRV-WPR-001": "demo-rental-carshare"}


@pytest.fixture(scope="module")
def seed():
    # USER_POOL_ID short-circuits the module-level CloudFormation lookup, so loading
    # the seed makes no AWS call; STAGE keeps its production guard quiet.
    mp = pytest.MonkeyPatch()
    # Not pool-id-shaped (the publish scanner flags those in shipped files).
    mp.setenv("USER_POOL_ID", "us-west-2_EXAMPLE")
    mp.setenv("STAGE", "staging")
    mp.setenv("AWS_REGION", "us-west-2")
    try:
        spec = importlib.util.spec_from_file_location("_seed_persona_users_fleets", SEED)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        yield module
    finally:
        mp.undo()


class _FakeTable:
    def __init__(self):
        self.puts = []

    def put_item(self, Item, **_kwargs):
        self.puts.append(Item)


def _persona_with(seed, key, value):
    if key == "vehicleId":
        return next(p for p in seed.PERSONAS if (p.get("vehicle") or {}).get("vehicleId") == value)
    return next(p for p in seed.PERSONAS if p["driver"]["driverId"] == value)


@pytest.mark.parametrize("vehicle_id,fleet_id", sorted(EXPECTED_VEHICLE_FLEETS.items()))
def test_vehicle_put_carries_its_current_cms_fleet(seed, vehicle_id, fleet_id):
    table = _FakeTable()
    seed._put_vehicle(table, _persona_with(seed, "vehicleId", vehicle_id))
    assert len(table.puts) == 1
    assert table.puts[0]["vehicleId"] == vehicle_id
    assert table.puts[0].get("fleetId") == fleet_id


@pytest.mark.parametrize("driver_id,fleet_id", sorted(EXPECTED_DRIVER_FLEETS.items()))
def test_driver_put_carries_its_current_cms_fleet(seed, driver_id, fleet_id):
    table = _FakeTable()
    seed._put_driver(table, _persona_with(seed, "driverId", driver_id))
    assert len(table.puts) == 1
    assert table.puts[0]["driverId"] == driver_id
    assert table.puts[0].get("fleetId") == fleet_id


def test_no_seeded_row_names_a_retired_fleet(seed):
    # Every row the seed would put, run through the real writers.
    drivers, vehicles = _FakeTable(), _FakeTable()
    for p in seed.PERSONAS:
        seed._put_driver(drivers, p)
        if p.get("vehicle"):
            seed._put_vehicle(vehicles, p)
    assert drivers.puts and vehicles.puts  # anti-vacuity
    named = {r.get("fleetId") for r in drivers.puts + vehicles.puts}
    assert not named & RETIRED_FLEETS, sorted(named & RETIRED_FLEETS)
