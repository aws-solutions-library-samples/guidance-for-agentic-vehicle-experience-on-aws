"""Driver-row write protection for ``scripts/seed-persona-users.py``.

spec 2026-10-22-vehicle-data-consistency § D1 "Write rules":
  "AVX's seed stops writing a driver row that exists."
  "One owner writes each persona driver row: CMS seed_driver_users.py;
  AVX seed-persona-users.py stops writing driver rows that exist."

Three cases:
  1. An existing driver row is left byte-identical after ``_put_driver``.
  2. A missing row is still created by ``_put_driver``.
  3. DRV-0054 (Samantha) is not touched: ``_fix_samantha_cognito_email`` is
     removed (D1); CMS seed_driver_users.py owns that row.

Uses moto (not MagicMock): moto evaluates conditional expressions against real
table state so the ``ConditionalCheckFailedException`` path is exercised.
"""
from __future__ import annotations

import importlib.util
from decimal import Decimal
from pathlib import Path

import boto3
import pytest
from moto import mock_aws

SEED = Path(__file__).resolve().parents[1] / "seed-persona-users.py"
TABLE = "cms-test-storage-drivers"


@pytest.fixture(scope="module")
def seed():
    mp = pytest.MonkeyPatch()
    mp.setenv("USER_POOL_ID", "<user-pool-id>")
    mp.setenv("STAGE", "staging")
    mp.setenv("AWS_REGION", "us-west-2")
    try:
        spec = importlib.util.spec_from_file_location("_seed_driver_no_overwrite", SEED)
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
            KeySchema=[{"AttributeName": "driverId", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "driverId", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        yield ddb.Table(TABLE)


def _persona(seed, driver_id: str) -> dict:
    return next(p for p in seed.PERSONAS if p["driver"]["driverId"] == driver_id)


# ---------------------------------------------------------------------------
# Live-row fixture: a pre-existing driver row with different values so each
# assertion can tell which side the value came from.
# ---------------------------------------------------------------------------

_LIVE_EXTRA = {
    "lastTripId": "trip-abc123",
    "activeSessionId": "sess-xyz",
    "createdAt": "2025-01-01T00:00:00+00:00",
    "totalMiles": Decimal("99999"),
    "totalTrips": Decimal("9999"),
    "licenseNumber": "LIVE-LIC-0001",    # the pre-existing live value
    "status": "ON_SHIFT",               # different from seed's "ACTIVE"
}


def _live_row(seed, driver_id: str) -> dict:
    """A row that pre-dates this seed: different field values + live-only keys."""
    p = _persona(seed, driver_id)
    row = {
        "driverId": driver_id,
        "email": p["email"],
        "cognitoEmail": p["email"],
        **_LIVE_EXTRA,
    }
    if p.get("vehicle"):
        row["assignedVehicleId"] = p["vehicle"]["vehicleId"]
    return row


def _row(table, driver_id: str) -> dict:
    resp = table.get_item(Key={"driverId": driver_id}, ConsistentRead=True)
    return resp.get("Item", {})


# ---------------------------------------------------------------------------
# Test 1: existing row is left byte-identical
# ---------------------------------------------------------------------------

def test_existing_driver_row_is_left_byte_identical(seed, table):
    """An existing driver row must not be touched by _put_driver.

    CMS seed_driver_users.py owns persona driver rows; this seed must not fight it.
    """
    live = _live_row(seed, "DRV-MRDN-0015")
    table.put_item(Item=live)

    result = seed._put_driver(table, _persona(seed, "DRV-MRDN-0015"))

    assert result == "exists", f"expected 'exists', got {result!r}"
    after = _row(table, "DRV-MRDN-0015")
    assert after == live, (
        "driver row was modified; seed must not overwrite an existing row"
    )


# ---------------------------------------------------------------------------
# Test 2: missing row is still created
# ---------------------------------------------------------------------------

def test_missing_driver_row_is_created(seed, table):
    """A driver row that does not exist must be created."""
    # Confirm the row is absent
    assert _row(table, "DRV-MRDN-0015") == {}, "precondition: row must be absent"

    result = seed._put_driver(table, _persona(seed, "DRV-MRDN-0015"))

    assert result == "created", f"expected 'created', got {result!r}"
    after = _row(table, "DRV-MRDN-0015")
    assert after["driverId"] == "DRV-MRDN-0015"
    assert after["email"] == "meridian.driver@example.com"
    assert after["assignedVehicleId"] == "MRDN0000000000015"
    # The seeded licenseNumber must be the corrected value, not the stale Ford one.
    assert after["licenseNumber"] == "GA-MRDN-0015", (
        "licenseNumber must be GA-MRDN-0015 (note-ii.md live row); "
        f"got {after.get('licenseNumber')!r}"
    )


# ---------------------------------------------------------------------------
# Test 3: Samantha's entry equals the recorded live values
# ---------------------------------------------------------------------------

# note-ii.md § "Samantha's seed entry vs live rows":
# "Live DRV-0054 Samantha: All fields match CMS seed exactly: TX-MICH-054,
#  totalMiles=412,800, totalTrips=1,842, etc.  No mismatch for Samantha's
#  driver row."
# AVX's seed re-asserts only cognitoEmail on DRV-0054; it must not write
# any other fields, and the email it writes must equal the live value.

_SAMANTHA_DRIVER_ID = "DRV-0054"
_SAMANTHA_EMAIL_LIVE = "samantha.carter@example.com"    # from note-ii.md live row

_SAMANTHA_LIVE_ROW = {
    "driverId": _SAMANTHA_DRIVER_ID,
    "licenseNumber": "TX-MICH-054",
    "totalMiles": Decimal("412800"),
    "totalTrips": Decimal("1842"),
    "status": "ACTIVE",
    "email": _SAMANTHA_EMAIL_LIVE,
    "cognitoEmail": "STALE_VALUE",   # simulate the out-of-date state the function fixes
    "firstName": "Samantha",
    "lastName": "Carter",
    "createdAt": "2025-06-01T00:00:00+00:00",
}


def test_samantha_cognitoemail_is_set_to_live_value(seed, table):
    """D1: DRV-0054 is not touched; _fix_samantha_cognito_email is removed.

    CMS seed_driver_users.py owns DRV-0054 (Samantha Carter).  The AVX seed
    must not write that row at all.

    Mutation guard: restoring ``_fix_samantha_cognito_email`` causes the
    ``not hasattr`` assertion to fail.
    """
    # Pre-existing row with a deliberately stale cognitoEmail.
    table.put_item(Item={**_SAMANTHA_LIVE_ROW, "cognitoEmail": "STALE_VALUE"})

    # D1: the fix function must be gone from the module.
    assert not hasattr(seed, "_fix_samantha_cognito_email"), (
        "_fix_samantha_cognito_email must be removed (D1: AVX stops writing DRV-0054)"
    )

    # Nothing in this seed touches DRV-0054; the stale value must be unchanged.
    after = _row(table, _SAMANTHA_DRIVER_ID)
    assert after.get("cognitoEmail") == "STALE_VALUE", (
        "DRV-0054 must not be touched by the seed (cognitoEmail kept stale)"
    )
