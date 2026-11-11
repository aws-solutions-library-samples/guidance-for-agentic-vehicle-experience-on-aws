"""A ``make seed-staging`` run never rewrites or deletes a persona car's live state.

CMS issues/2026-10-22-persona-seed-overwrites-live-rows/: ``scripts/seed-persona-users.py``
wrote all three persona cars with a wholesale ``put_item`` on every run. That moved each
car back to its seeded position and deleted every field the seed doesn't carry, among them
``sold_to`` (the owner link ``agents/tier2/owner_resolver.py`` reads), ``faultState`` (the
injected demo DTCs), ``locationId``, ``connectionStatus`` and the running agent's
``presenceOwner``. It also fought CMS's ``seed_driver_users.py`` over MRDN0000000000015, so the
car was whatever the last seed to run said.

Now an existing row gets only its identity fields from this seed, and MRDN0000000000015, whose
identity CMS's seed owns, is created here only when it is missing. Run against moto, which
evaluates the update and condition expressions; moto is an emulator (it accepts some
expressions real DynamoDB rejects), so the run against the real staging table is recorded in
the issue's summary.md.
"""
from __future__ import annotations

import importlib.util
from decimal import Decimal
from pathlib import Path

import boto3
import pytest
from moto import mock_aws

SEED = Path(__file__).resolve().parents[1] / "seed-persona-users.py"
TABLE = "cms-test-storage-vehicles"

# What this seed rewrites on an existing row. Pinned here, not derived from the module, so a
# field added to an entry fails this file until someone decides which kind it is: a
# live-state field left off SEED_INITIAL_ONLY_FIELDS would be rewritten on every run.
IDENTITY_FIELDS = frozenset({
    "vin", "make", "model", "year", "color", "licensePlate", "vehicleType", "fuelType",
    "batteryCapacityKwh", "batteryVoltage", "purchaseDate", "purchasePrice",
    "annualInsurancePremium", "name", "status", "enrollmentStatus", "modelManifestName",
    "modelManifestVersion", "decoderManifestRef", "dataSource", "attributes", "producer",
})

# The same list as CMS deployment/scripts/seed_driver_users.py SEED_INITIAL_ONLY_FIELDS
# plus the D1 (spec 2026-10-22-vehicle-data-consistency) identity fields that are
# initial-only on an existing car (make, model, year, vehicleType, fuelType, purchaseDate).
EXPECTED_INITIAL_ONLY = frozenset({
    "lastLatitude", "lastLongitude", "lastSpeed", "connectionStatus",
    "fuelLevel", "batterySoh", "odometer", "mileage", "totalTrips",
    "engineRPM", "engineTemp", "fleetId", "locationId",
    "make", "model", "year", "vehicleType", "fuelType", "purchaseDate",
})

# Fields the running system writes. None is in MRDN0000002000013's or MRDCW01H8PC00V7RD's entry (the two
# rows this seed rewrites); MRDN0000000000015's entry carries `producer` for a fresh row.
LIVE_ONLY = {
    "sold_to": "CUST-004000DD", "faultState": "{'ecus': {'2': {'dtcs': ['P0171']}}}",
    "presenceOwner": "ip-10-0-49-61:ad7a7700bb12", "lastSeenAt": "2026-10-22T13:05:01+00:00",
    "lastTripId": "trip-a5ea47b8", "locationId": "loc-city-pdx-south",
    "producer": "meridian", "createdAt": "2026-09-11T00:34:44+00:00",
}


@pytest.fixture(scope="module")
def seed():
    # USER_POOL_ID short-circuits the module-level CloudFormation lookup, so loading the
    # seed makes no AWS call. An angle-bracket placeholder: a pool-id-shaped value trips the
    # publish scanner.
    mp = pytest.MonkeyPatch()
    mp.setenv("USER_POOL_ID", "<user-pool-id>")
    mp.setenv("STAGE", "staging")
    mp.setenv("AWS_REGION", "us-west-2")
    try:
        spec = importlib.util.spec_from_file_location("_seed_persona_users_no_overwrite", SEED)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        yield module
    finally:
        mp.undo()


@pytest.fixture
def table(monkeypatch):
    monkeypatch.delenv("AWS_ENDPOINT_URL", raising=False)
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    with mock_aws():
        ddb = boto3.resource("dynamodb", region_name="us-west-2")
        ddb.create_table(
            TableName=TABLE,
            KeySchema=[{"AttributeName": "vehicleId", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "vehicleId", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        yield ddb.Table(TABLE)


def _persona(seed, vehicle_id):
    return next(p for p in seed.PERSONAS
                if (p.get("vehicle") or {}).get("vehicleId") == vehicle_id)


def _entry(seed, vehicle_id):
    return {k: v for k, v in _persona(seed, vehicle_id)["vehicle"].items() if k != "create_only"}


def _live_row(seed, vehicle_id):
    """The seed entry with every live-state field changed, plus the fields only the live
    system writes, and stale identity values, so each assertion below can tell which
    side a value came from."""
    entry = _entry(seed, vehicle_id)
    row = {"vehicleId": vehicle_id, **LIVE_ONLY}
    for key, value in entry.items():
        if key in seed.SEED_INITIAL_ONLY_FIELDS:
            row[key] = value + 7 if isinstance(value, Decimal) else f"live-{value}"
    row.update({"name": "An old name", "year": Decimal("2019"), "fuelType": "hybrid"})
    return row


def _row(table, vehicle_id):
    return table.get_item(Key={"vehicleId": vehicle_id}, ConsistentRead=True)["Item"]


REWRITTEN = ("MRDN0000002000013", "MRDCW01H8PC00V7RD")


def test_initial_only_fields_are_the_cms_list(seed):
    assert seed.SEED_INITIAL_ONLY_FIELDS == EXPECTED_INITIAL_ONLY


def test_marcus_is_create_only_and_the_others_are_not(seed):
    assert _persona(seed, "MRDN0000000000015")["vehicle"].get("create_only") is True
    for vehicle_id in REWRITTEN:
        assert not _persona(seed, vehicle_id)["vehicle"].get("create_only"), vehicle_id
    assert {p["vehicle"]["vehicleId"] for p in seed.PERSONAS if p.get("vehicle")} == {
        "MRDN0000000000015", *REWRITTEN}


@pytest.mark.parametrize("vehicle_id", ["MRDN0000000000015", *REWRITTEN])
def test_every_field_in_the_entry_is_classified(seed, vehicle_id):
    keys = set(_entry(seed, vehicle_id)) - {"vehicleId"}
    assert keys & seed.SEED_INITIAL_ONLY_FIELDS >= {"lastLatitude", "fleetId", "odometer"}
    assert keys - seed.SEED_INITIAL_ONLY_FIELDS <= IDENTITY_FIELDS


@pytest.mark.parametrize("vehicle_id", REWRITTEN)
def test_existing_row_keeps_everything_the_seed_does_not_own(seed, table, vehicle_id):
    live = _live_row(seed, vehicle_id)
    table.put_item(Item=live)
    assert seed._put_vehicle(table, _persona(seed, vehicle_id)) == "updated-identity"
    after = _row(table, vehicle_id)
    owned = set(_entry(seed, vehicle_id)) & IDENTITY_FIELDS
    for key, value in live.items():
        if key in owned:
            continue
        assert after.get(key) == value, f"{key}: {after.get(key)!r} != live {value!r}"


@pytest.mark.parametrize("vehicle_id", REWRITTEN)
def test_existing_row_gets_the_seeds_identity_fields(seed, table, vehicle_id):
    """D1: make/model/year/vehicleType/fuelType/purchaseDate are initial-only and kept
    from the live row; other seed-owned identity fields still come from the seed.

    Mutation guard: removing any of the six from SEED_INITIAL_ONLY_FIELDS causes the
    seed to overwrite it with its entry value, which fails the stale-value assertions.
    """
    live = _live_row(seed, vehicle_id)
    # Hard-code stale values for the six initial-only identity fields so the assertion
    # does not depend on how _live_row is built or on seed.SEED_INITIAL_ONLY_FIELDS.
    _STALE = {
        "make": "StaleMotors", "model": "StaleModel", "year": Decimal("2001"),
        "vehicleType": "stale-type", "fuelType": "stale-fuel",
        "purchaseDate": "2001-01-01",
    }
    table.put_item(Item={**live, **_STALE})
    seed._put_vehicle(table, _persona(seed, vehicle_id))
    after = _row(table, vehicle_id)
    entry = _entry(seed, vehicle_id)
    # Non-initial-only identity fields must come from the seed.
    _INITIAL_ONLY_KEYS = frozenset(_STALE)
    for key in (set(entry) & IDENTITY_FIELDS) - _INITIAL_ONLY_KEYS:
        assert after[key] == entry[key], key
    # Initial-only fields must keep the live row's stale values (D1).
    for key, value in _STALE.items():
        assert after[key] == value, (
            f"{key}: expected stale {value!r} — seed must not rewrite initial-only fields"
        )
    assert after["updatedAt"] == seed.now


def test_existing_marcus_row_is_left_untouched(seed, table):
    live = _live_row(seed, "MRDN0000000000015")
    table.put_item(Item=live)
    assert seed._put_vehicle(table, _persona(seed, "MRDN0000000000015")) == "exists"
    assert _row(table, "MRDN0000000000015") == live


@pytest.mark.parametrize("vehicle_id", ["MRDN0000000000015", *REWRITTEN])
def test_missing_row_is_created_from_the_whole_entry(seed, table, vehicle_id):
    assert seed._put_vehicle(table, _persona(seed, vehicle_id)) == "created"
    after = _row(table, vehicle_id)
    for key, value in _entry(seed, vehicle_id).items():
        assert after[key] == value, key
    assert "create_only" not in after


def test_marcus_starts_as_the_car_cms_seeds(seed):
    # CMS deployment/scripts/seed_driver_users.py owns this car's identity; a fresh row this
    # seed creates first should not start as a different car.
    entry = _entry(seed, "MRDN0000000000015")
    assert (entry["make"], entry["model"], entry["year"]) == ("Meridian", "Azimuth", 2023)
    assert (entry["fuelType"], entry["vehicleType"]) == ("electric", "Sedan")
    assert entry["modelManifestName"] == "MERIDIAN-AZIMUTH"
    assert entry["producer"] == "meridian"
    assert "decoderManifestRef" not in entry


def test_existing_row_that_lost_a_live_field_does_not_get_it_back(seed, table):
    # A CMS fleet delete or disassociate REMOVEs fleetId. Refilling it from the seed would
    # put the car back in a fleet with no fleet-enrollment row.
    live = {k: v for k, v in _live_row(seed, "MRDCW01H8PC00V7RD").items()
            if k not in ("fleetId", "lastLatitude")}
    table.put_item(Item=live)
    assert seed._put_vehicle(table, _persona(seed, "MRDCW01H8PC00V7RD")) == "updated-identity"
    after = _row(table, "MRDCW01H8PC00V7RD")
    assert "fleetId" not in after and "lastLatitude" not in after
    assert after["name"] == _entry(seed, "MRDCW01H8PC00V7RD")["name"]  # anti-vacuity


def test_row_deleted_before_the_update_raises(seed, table):
    from botocore.exceptions import ClientError

    table.put_item(Item=_live_row(seed, "MRDCW01H8PC00V7RD"))

    class _DeletedAfterPut:
        def __init__(self, inner):
            self._inner = inner

        def put_item(self, **kwargs):
            try:
                return self._inner.put_item(**kwargs)
            finally:
                self._inner.delete_item(Key={"vehicleId": "MRDCW01H8PC00V7RD"})

        def __getattr__(self, name):
            return getattr(self._inner, name)

    with pytest.raises(ClientError) as raised:
        seed._put_vehicle(_DeletedAfterPut(table), _persona(seed, "MRDCW01H8PC00V7RD"))
    assert raised.value.response["Error"]["Code"] == "ConditionalCheckFailedException"
    assert raised.value.operation_name == "UpdateItem"
    assert "Item" not in table.get_item(Key={"vehicleId": "MRDCW01H8PC00V7RD"})


def test_put_error_other_than_the_condition_propagates(seed):
    from botocore.exceptions import ClientError

    class _Throttled:
        def put_item(self, **_kwargs):
            raise ClientError({"Error": {"Code": "ProvisionedThroughputExceededException"}},
                              "PutItem")

        def update_item(self, **_kwargs):
            raise AssertionError("must not fall through to the update")

    with pytest.raises(ClientError):
        seed._put_vehicle(_Throttled(), _persona(seed, "MRDCW01H8PC00V7RD"))
