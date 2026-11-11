"""Red tests: seed-persona-users.py must write vehicleId == VIN on vehicle rows,
assignedVehicleId == VIN on driver rows, and set custom:vehicleId == VIN in Cognito.

All assertions fail today because vehicleId is still 'VEH-...' rather than the car's
VIN.  Load and fake pattern follows test_seed_persona_users_fleets.py.

VIN mapping (spec 2026-10-28-vehicleid-equals-vin § D1, user decision U2):
  MRDN0000000000015 -> MRDN0000000000015
  MRDCW01H8PC00V7RD    -> MRDCW01H8PC00V7RD
  MRDN0000002000013   -> MRDN0000002000013
"""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path

import pytest

SEED = Path(__file__).resolve().parents[1] / "seed-persona-users.py"

# Maps the pre-cutover vehicleId → VIN (= post-cutover vehicleId).
# Column 1 is the old id that must NOT appear in any Cognito attribute after cutover.
# Column 2 is the VIN that equals the post-cutover vehicleId.
# This test is allowlisted in rekey_files.py because its subject is the transition.
VEHICLE_VIN_MAP = [
    ("VEH-MRDN-0015",      "MRDN0000000000015"),
    ("VEH-WPR-001",        "MRDN0000002000013"),
    ("VEH-VO-001",         "MRDCW01H8PC00V7RD"),
]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _strip_aws_env(monkeypatch):
    """Remove every AWS_* env var for each test so no call can reach a live endpoint."""
    for var in [k for k in os.environ if k.startswith("AWS_")]:
        monkeypatch.delenv(var, raising=False)


@pytest.fixture(scope="module")
def seed():
    """Load seed-persona-users.py without hitting AWS (USER_POOL_ID bypasses CFN)."""
    mp = pytest.MonkeyPatch()
    mp.setenv("USER_POOL_ID", "us-west-2_EXAMPLE")
    mp.setenv("STAGE", "staging")
    mp.setenv("AWS_REGION", "us-west-2")
    try:
        spec = importlib.util.spec_from_file_location("_seed_persona_users_vin_ids", SEED)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        yield mod
    finally:
        mp.undo()


# ---------------------------------------------------------------------------
# Fakes — record writes, never reach AWS
# ---------------------------------------------------------------------------

class _FakeTable:
    def __init__(self):
        self.puts: list[dict] = []

    def put_item(self, Item, **kwargs):   # noqa: N803 — boto3 convention
        self.puts.append(Item)

    def update_item(self, **kwargs):
        pass


class _FakeCognito:
    def __init__(self):
        # email -> {attr_name: value} recorded at admin_create_user time
        self.created: dict[str, dict[str, str]] = {}

    def admin_create_user(self, *, UserPoolId, Username, UserAttributes, **kwargs):
        self.created[Username] = {a["Name"]: a["Value"] for a in UserAttributes}

    def admin_set_user_password(self, **kwargs):
        pass

    def admin_update_user_attributes(self, **kwargs):
        pass


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _persona_with_vehicle(seed_mod, _legacy_vehicle_id: str, vin: str) -> dict:
    """Return the persona whose vehicle dict carries the given VIN (post-cutover vehicleId)."""
    return next(
        p for p in seed_mod.PERSONAS
        if (p.get("vehicle") or {}).get("vehicleId") == vin
    )


# ---------------------------------------------------------------------------
# Tests — all three fail today on the id assertions
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("vehicle_id,vin", VEHICLE_VIN_MAP)
def test_vehicle_row_vehicleId_equals_vin(seed, vehicle_id, vin):
    """The put_item call for a vehicle row must carry vehicleId == VIN."""
    persona = _persona_with_vehicle(seed, vehicle_id, vin)
    table = _FakeTable()
    seed._put_vehicle(table, persona)
    assert table.puts, f"{persona['label']}: _put_vehicle issued no put_item call"
    row = table.puts[0]
    assert row["vehicleId"] == vin, (
        f"{persona['label']}: vehicleId={row['vehicleId']!r}, want {vin!r}"
    )


@pytest.mark.parametrize("vehicle_id,vin", VEHICLE_VIN_MAP)
def test_driver_row_assignedVehicleId_equals_vin(seed, vehicle_id, vin):
    """The put_item call for a driver row must carry assignedVehicleId == VIN."""
    persona = _persona_with_vehicle(seed, vehicle_id, vin)
    table = _FakeTable()
    seed._put_driver(table, persona)
    assert table.puts, f"{persona['label']}: _put_driver issued no put_item call"
    row = table.puts[0]
    assert row.get("assignedVehicleId") == vin, (
        f"{persona['label']}: assignedVehicleId={row.get('assignedVehicleId')!r}, want {vin!r}"
    )


@pytest.mark.parametrize("vehicle_id,vin", VEHICLE_VIN_MAP)
def test_cognito_custom_vehicleId_never_names_old_id(seed, vehicle_id, vin):
    """Any custom:vehicleId the seed sets equals the VIN, and no attribute names the old id.

    Architect (2026-10-28): the seed sets no custom:vehicleId today. That claim scopes a
    driver to a car, so the seed must not start setting it here; the one AVX user that
    carries it is moved by the migration tool (spec § D2/D3). This test passes today and
    pins that the cutover never writes the old id or a different car's id.
    """
    persona = _persona_with_vehicle(seed, vehicle_id, vin)
    cidp = _FakeCognito()
    seed._ensure_cognito_user(
        cidp,
        persona["email"],
        persona["firstName"],
        persona["lastName"],
        tenant_id=persona.get("tenantId"),
    )
    attrs = cidp.created.get(persona["email"], {})
    assert cidp.created, f"{persona['label']}: _ensure_cognito_user created no user"
    assert attrs.get("custom:vehicleId") in (None, vin), (
        f"{persona['label']}: custom:vehicleId={attrs.get('custom:vehicleId')!r}, want absent or {vin!r}"
    )
    assert vehicle_id not in attrs.values(), (
        f"{persona['label']}: a Cognito attribute names the old id {vehicle_id!r}"
    )
