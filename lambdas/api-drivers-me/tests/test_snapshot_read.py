"""Stage C: api-drivers-me reads the vehicle snapshot and overlays four fields.

Rules under test (brief Stage C + all-stages rules):
  - Snapshot item present: set vehicle.odometer, .mileage (both), .fuelLevel,
    .batterySoh from snapshot values; each may be null.
  - Snapshot .value null → field is null, NOT the row value (no `or row_val`).
  - No snapshot item: row values unchanged, snapshotStatus "none".
  - Read error (DDB exception): all four fields null, snapshotStatus "error",
    route still 200. Never show row values on error.
  - GetItem key is {"vehicleId": id}, never {"vin": id}.

Mutations guarded (each must fail a test; run on git archive copy under
/tmp/2026-10-22-vehicle-data-consistency-g5-51/mutC/):
  M1: mileage left as row value when snapshot odometer is null →
      test_snapshot_null_odometer_sets_both_to_null
  M2: row values used on a read error →
      test_snapshot_error_nulls_four_fields
  M3: snapshot read under VIN key instead of vehicleId →
      test_snapshot_key_is_vehicle_id_not_vin
  M4: CDK grant widened to table/* → CDK test (vsa-api-stack-snapshot-read.test.ts)
"""
from __future__ import annotations

import importlib.util
import json
import pathlib
from decimal import Decimal
from typing import Any

import pytest

_HANDLER = pathlib.Path(__file__).resolve().parents[1] / "handler.py"

EMAIL = "driver@example.com"

DRIVER_ROW = {
    "driverId": "DRV-001",
    "cognitoEmail": EMAIL,
    "firstName": "Test",
    "assignedVehicleId": "VEH-001",
}

VEHICLE_ROW = {
    "vehicleId": "VEH-001",
    "vin": "VIN-TEST-001",
    "odometer": 39000,
    "mileage": 38500,
    "fuelLevel": 60.0,
    "batterySoh": 92.0,
    "fuelType": "bev",
}

# Snapshot with real (non-null) values
SNAPSHOT_ITEM = {
    "vehicleId": "VEH-001",
    "schemaVersion": 1,
    "odometer": {"value": Decimal("45000"), "asOf": "2026-10-01", "basis": "history"},
    "batteryCharge": {"value": Decimal("72"), "asOf": "2026-10-01T10:00:00Z", "basis": "feed"},
    "batteryHealth": {"value": Decimal("85"), "asOf": "2026-10-01", "basis": "history"},
    "batteryAge":    {"value": Decimal("365"), "asOf": "2026-10-01", "basis": "history"},
    "tires": {"asOf": None, "basis": "history", "positions": {}},
    "service": {"open": 0, "openAsOf": None, "completed": 1, "completedAsOf": "2026-09-01", "basis": "history"},
    "faults": {"open": [], "history": [], "asOf": None, "basis": "history"},
    "runDate": "2026-10-01",
    "dataThrough": "2026-09-30",
    "computedAt": "2026-10-01T02:00:00Z",
}

# Snapshot where the odometer value is null (field present, value absent)
SNAPSHOT_NULL_ODO = dict(SNAPSHOT_ITEM, odometer={"value": None, "asOf": None, "basis": "history"})


# ---------------------------------------------------------------------------
# Mock infrastructure
# ---------------------------------------------------------------------------

class _DriversTable:
    def __init__(self, driver=None):
        self._driver = driver

    def scan(self, **kw: Any) -> dict:
        cond = kw["FilterExpression"].get_expression()
        attr, value = cond["values"]
        if self._driver and self._driver.get(attr.name) == value:
            return {"Items": [dict(self._driver)]}
        return {"Items": []}


class _VehiclesTable:
    def __init__(self, vehicle=None):
        self._vehicle = vehicle

    def get_item(self, **kw: Any) -> dict:
        if self._vehicle is None:
            return {}
        return {"Item": dict(self._vehicle)}


class _SnapshotTable:
    def __init__(self, item=None, raises: bool = False):
        self._item = item
        self._raises = raises
        self.calls: list[dict] = []  # Key dicts passed to get_item

    def get_item(self, **kw: Any) -> dict:
        self.calls.append(dict(kw.get("Key", {})))
        if self._raises:
            raise RuntimeError("simulated DDB error")
        if self._item is not None:
            return {"Item": dict(self._item)}
        return {}


class _Resource:
    """Routes Table() calls by name substring: 'snapshot', 'driver', else vehicles."""
    def __init__(self, drivers: _DriversTable, vehicles: _VehiclesTable, snapshot: _SnapshotTable):
        self._d = drivers
        self._v = vehicles
        self._s = snapshot

    def Table(self, name: str) -> Any:  # noqa: N802
        if "snapshot" in name:
            return self._s
        if "driver" in name:
            return self._d
        return self._v


def _load():
    spec = importlib.util.spec_from_file_location("dm_snapshot_h", _HANDLER)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _event():
    return {"requestContext": {"authorizer": {"claims": {
        "email": EMAIL,
        "email_verified": "true",
    }}}}


@pytest.fixture
def h(monkeypatch):
    return _load()


def _make_resource(snapshot=None, snapshot_raises=False, vehicle=None):
    drivers = _DriversTable(dict(DRIVER_ROW))
    veh = _VehiclesTable(dict(vehicle or VEHICLE_ROW))
    snap = _SnapshotTable(item=snapshot, raises=snapshot_raises)
    return _Resource(drivers=drivers, vehicles=veh, snapshot=snap)


# ---------------------------------------------------------------------------
# Snapshot overrides
# ---------------------------------------------------------------------------

def test_snapshot_odometer_overwrites_both_fields(h, monkeypatch):
    """Snapshot odometer (45 000) overwrites both .odometer and .mileage (was 38 500)."""
    r = _make_resource(snapshot=SNAPSHOT_ITEM)
    monkeypatch.setattr(h, "_get_ddb", lambda: r)
    body = json.loads(h.handler(_event(), None)["body"])
    veh = body["vehicle"]
    assert veh["odometer"] == 45000
    assert veh["mileage"] == 45000, "mileage must equal odometer, not the row value 38500"


def test_snapshot_battery_charge_becomes_fuel_level(h, monkeypatch):
    r = _make_resource(snapshot=SNAPSHOT_ITEM)
    monkeypatch.setattr(h, "_get_ddb", lambda: r)
    body = json.loads(h.handler(_event(), None)["body"])
    assert body["vehicle"]["fuelLevel"] == 72


def test_snapshot_battery_health_becomes_battery_soh(h, monkeypatch):
    r = _make_resource(snapshot=SNAPSHOT_ITEM)
    monkeypatch.setattr(h, "_get_ddb", lambda: r)
    body = json.loads(h.handler(_event(), None)["body"])
    assert body["vehicle"]["batterySoh"] == 85


def test_snapshot_status_ok_when_item_present(h, monkeypatch):
    r = _make_resource(snapshot=SNAPSHOT_ITEM)
    monkeypatch.setattr(h, "_get_ddb", lambda: r)
    body = json.loads(h.handler(_event(), None)["body"])
    assert body["snapshotStatus"] == "ok"


# ---------------------------------------------------------------------------
# Null snapshot value — mutation guard M1
# ---------------------------------------------------------------------------

def test_snapshot_null_odometer_sets_both_to_null(h, monkeypatch):
    """M1: snapshot item present but odometer.value = null → both fields null, not row value."""
    r = _make_resource(snapshot=SNAPSHOT_NULL_ODO)
    monkeypatch.setattr(h, "_get_ddb", lambda: r)
    body = json.loads(h.handler(_event(), None)["body"])
    veh = body["vehicle"]
    assert veh["odometer"] is None, "odometer must be null, not the row value 39000"
    assert veh["mileage"] is None, "mileage must be null, not the row value 38500 (M1 guard)"


# ---------------------------------------------------------------------------
# No snapshot item
# ---------------------------------------------------------------------------

def test_no_snapshot_item_leaves_row_values_intact(h, monkeypatch):
    """No snapshot item (status 'none'): vehicle row values unchanged."""
    r = _make_resource(snapshot=None)
    monkeypatch.setattr(h, "_get_ddb", lambda: r)
    body = json.loads(h.handler(_event(), None)["body"])
    veh = body["vehicle"]
    assert veh["odometer"] == 39000
    assert veh["mileage"] == 38500
    assert veh["fuelLevel"] == 60.0
    assert veh["batterySoh"] == 92.0
    assert body["snapshotStatus"] == "none"


# ---------------------------------------------------------------------------
# Read error — mutation guard M2
# ---------------------------------------------------------------------------

def test_snapshot_error_nulls_four_fields(h, monkeypatch):
    """M2: DDB exception on snapshot read → all four vehicle fields null, not row values."""
    r = _make_resource(snapshot_raises=True)
    monkeypatch.setattr(h, "_get_ddb", lambda: r)
    body = json.loads(h.handler(_event(), None)["body"])
    veh = body["vehicle"]
    assert veh["odometer"] is None, "odometer must be null on error, not row value (M2 guard)"
    assert veh["mileage"] is None
    assert veh["fuelLevel"] is None
    assert veh["batterySoh"] is None


def test_snapshot_error_returns_200(h, monkeypatch):
    """Snapshot read error must NOT surface as a non-200 to the iOS client."""
    r = _make_resource(snapshot_raises=True)
    monkeypatch.setattr(h, "_get_ddb", lambda: r)
    resp = h.handler(_event(), None)
    assert resp["statusCode"] == 200


def test_snapshot_error_sets_status_error(h, monkeypatch):
    r = _make_resource(snapshot_raises=True)
    monkeypatch.setattr(h, "_get_ddb", lambda: r)
    body = json.loads(h.handler(_event(), None)["body"])
    assert body["snapshotStatus"] == "error"


# ---------------------------------------------------------------------------
# GetItem key — mutation guard M3
# ---------------------------------------------------------------------------

def test_snapshot_key_is_vehicle_id_not_vin(h, monkeypatch):
    """M3: GetItem must use Key={'vehicleId': id}, never {'vin': id}."""
    r = _make_resource(snapshot=SNAPSHOT_ITEM)
    monkeypatch.setattr(h, "_get_ddb", lambda: r)
    h.handler(_event(), None)
    snap_calls = r._s.calls
    assert snap_calls, "snapshot GetItem must be called"
    assert all("vehicleId" in call for call in snap_calls), \
        f"Key must use vehicleId, got {snap_calls}"
    assert not any("vin" in call for call in snap_calls), \
        f"Key must NOT use vin, got {snap_calls}"


# ---------------------------------------------------------------------------
# snapshotStatus "none" when no item
# ---------------------------------------------------------------------------

def test_snapshot_status_none_when_no_item(h, monkeypatch):
    r = _make_resource(snapshot=None)
    monkeypatch.setattr(h, "_get_ddb", lambda: r)
    body = json.loads(h.handler(_event(), None)["body"])
    assert body["snapshotStatus"] == "none"


# ---------------------------------------------------------------------------
# values / snapshot blocks present and absent
# ---------------------------------------------------------------------------

def test_values_block_present_when_snapshot_item(h, monkeypatch):
    r = _make_resource(snapshot=SNAPSHOT_ITEM)
    monkeypatch.setattr(h, "_get_ddb", lambda: r)
    body = json.loads(h.handler(_event(), None)["body"])
    assert body["values"] is not None
    assert "odometer" in body["values"]
    assert "batteryCharge" in body["values"]


def test_values_block_null_when_no_item(h, monkeypatch):
    r = _make_resource(snapshot=None)
    monkeypatch.setattr(h, "_get_ddb", lambda: r)
    body = json.loads(h.handler(_event(), None)["body"])
    assert body["values"] is None


def test_values_block_null_when_error(h, monkeypatch):
    r = _make_resource(snapshot_raises=True)
    monkeypatch.setattr(h, "_get_ddb", lambda: r)
    body = json.loads(h.handler(_event(), None)["body"])
    assert body["values"] is None


# ---------------------------------------------------------------------------
# Decimal conversion (real 0 and real non-integer pass through correctly)
# ---------------------------------------------------------------------------

def test_decimal_zero_survives_as_int_zero(h, monkeypatch):
    """A real snapshot odometer of 0 (e.g. new car) must not be suppressed."""
    zero_snap = dict(SNAPSHOT_ITEM, odometer={"value": Decimal("0"), "asOf": "2026-10-01", "basis": "history"})
    r = _make_resource(snapshot=zero_snap)
    monkeypatch.setattr(h, "_get_ddb", lambda: r)
    body = json.loads(h.handler(_event(), None)["body"])
    assert body["vehicle"]["odometer"] == 0
    assert body["vehicle"]["mileage"] == 0


def test_decimal_fractional_value_becomes_float(h, monkeypatch):
    snap = dict(SNAPSHOT_ITEM, batteryCharge={"value": Decimal("72.5"), "asOf": "2026-10-01T10:00:00Z", "basis": "feed"})
    r = _make_resource(snapshot=snap)
    monkeypatch.setattr(h, "_get_ddb", lambda: r)
    body = json.loads(h.handler(_event(), None)["body"])
    assert body["vehicle"]["fuelLevel"] == 72.5


# ---------------------------------------------------------------------------
# PINNED item (matches lane S pinned snapshot shape):
#   - fractional odometer (guard C1 / M2-1)
#   - numeric faults.open / faults.history (not lists — brief shape)
#   - populated tire positions with nested Decimals
# ---------------------------------------------------------------------------
PINNED_ITEM = {
    "vehicleId": "VEH-001",
    "schemaVersion": 1,
    "runDate":    "2026-10-01",
    "dataThrough": "2026-09-30",
    "computedAt": "2026-10-01T02:00:00Z",
    "odometer":      {"value": Decimal("21268.4"), "asOf": "2026-10-01", "basis": "history"},
    "batteryCharge": {"value": Decimal("68"),       "asOf": "2026-10-01T08:00:00Z", "basis": "feed"},
    "batteryHealth": {"value": Decimal("88"),       "asOf": "2026-10-01", "basis": "history"},
    "batteryAge":    {"value": Decimal("420"),      "asOf": "2026-10-01", "basis": "history"},
    "tires": {
        "asOf": "2026-10-01", "basis": "history",
        "positions": {
            "FL": {"treadDepthMm": Decimal("7.2"), "pressurePsi": Decimal("34"), "wearCategory": "good",  "needsReplacement": False, "asOf": "2026-10-01"},
            "FR": {"treadDepthMm": Decimal("6.9"), "pressurePsi": Decimal("33"), "wearCategory": "good",  "needsReplacement": False, "asOf": "2026-10-01"},
            "RL": {"treadDepthMm": Decimal("5.1"), "pressurePsi": Decimal("32"), "wearCategory": "worn",  "needsReplacement": False, "asOf": "2026-10-01"},
            "RR": {"treadDepthMm": Decimal("4.8"), "pressurePsi": Decimal("31"), "wearCategory": "worn",  "needsReplacement": True,  "asOf": "2026-10-01"},
        },
    },
    "service": {"open": 1, "openAsOf": "2026-10-01T08:00:00Z", "completed": 3, "completedAsOf": "2026-09-01", "basis": "history"},
    "faults":  {"open": 2, "history": 5, "asOf": "2026-10-01T08:00:00Z", "basis": "history"},
}

SNAPSHOT_NULL_CHARGE = dict(SNAPSHOT_ITEM, batteryCharge={"value": None, "asOf": None, "basis": "history"})
SNAPSHOT_NULL_HEALTH = dict(SNAPSHOT_ITEM, batteryHealth={"value": None, "asOf": None, "basis": "history"})


class _StrictSnapshotTable:
    """Returns the item only when the key VALUE equals the expected vehicleId.

    Catches mutant R3: _load_snapshot(vehicle.get("vin") or vid) calls with
    Key={"vehicleId": "VIN-TEST-001"} instead of Key={"vehicleId": "VEH-001"},
    so the fake returns {} and snapshotStatus is "none" — the test asserts
    snapshotStatus == "ok", which fails under the mutant.
    """
    def __init__(self, item=None, raises: bool = False, expected_vehicle_id: str = "VEH-001"):
        self._item = item
        self._raises = raises
        self._expected_vid = expected_vehicle_id
        self.calls: list[dict] = []

    def get_item(self, **kw: Any) -> dict:
        key = dict(kw.get("Key", {}))
        self.calls.append(key)
        if self._raises:
            raise RuntimeError("simulated DDB error")
        if self._item is not None and key.get("vehicleId") == self._expected_vid:
            return {"Item": dict(self._item)}
        return {}


def _make_resource_strict(snapshot=None, snapshot_raises=False, vehicle=None,
                           expected_vehicle_id="VEH-001"):
    drivers = _DriversTable(dict(DRIVER_ROW))
    veh = _VehiclesTable(dict(vehicle or VEHICLE_ROW))
    snap = _StrictSnapshotTable(item=snapshot, raises=snapshot_raises,
                                 expected_vehicle_id=expected_vehicle_id)
    return _Resource(drivers=drivers, vehicles=veh, snapshot=snap)


# ---------------------------------------------------------------------------
# C1: fractional odometer → floored int (M2-1 guard)
# ---------------------------------------------------------------------------

def test_fractional_odometer_floors_to_int(h, monkeypatch):
    """C1 / M2-1: Decimal('21268.4') → odometer == mileage == 21268, type int.

    Mutant M2-1 (unrounded value) keeps the Decimal which serialises to 21268.4
    (a float), breaking iOS Int? decoding.
    """
    r = _make_resource(snapshot=PINNED_ITEM)
    monkeypatch.setattr(h, "_get_ddb", lambda: r)
    body = json.loads(h.handler(_event(), None)["body"])
    veh = body["vehicle"]
    assert veh["odometer"] == 21268, f"expected 21268, got {veh['odometer']!r}"
    assert veh["mileage"] == 21268, f"mileage must equal odometer, got {veh['mileage']!r}"
    assert isinstance(veh["odometer"], int), f"odometer must be int, got {type(veh['odometer'])}"
    assert isinstance(veh["mileage"], int), f"mileage must be int, got {type(veh['mileage'])}"


def test_ios_int_fields_are_int_or_null(h, monkeypatch):
    """C1 iOS contract: every VehicleInfo Int? field in the body is int or null.

    iOS synthesised Codable raises DecodingError.dataCorrupted on a JSON number
    that isn't representable as Swift Int (e.g. 21268.4).  This catches any path
    that emits a float for year, odometer, mileage, or totalTrips.
    """
    # Vehicle row with totalTrips and year, snapshot with fractional odometer.
    vehicle = dict(VEHICLE_ROW, year=2022, totalTrips=47)
    r = _make_resource(snapshot=PINNED_ITEM, vehicle=vehicle)
    monkeypatch.setattr(h, "_get_ddb", lambda: r)
    body = json.loads(h.handler(_event(), None)["body"])
    veh = body["vehicle"]
    for field in ("year", "odometer", "mileage", "totalTrips"):
        val = veh.get(field)
        assert val is None or isinstance(val, int), (
            f"iOS Int? field '{field}' must be int or null, got {val!r} ({type(val).__name__})"
        )


# ---------------------------------------------------------------------------
# R1 / R2: null charge and null health stay null — never row fallback
# ---------------------------------------------------------------------------

def test_snapshot_null_charge_stays_null(h, monkeypatch):
    """R1 guard: batteryCharge.value = null → fuelLevel null, NOT the row value 60.0."""
    r = _make_resource(snapshot=SNAPSHOT_NULL_CHARGE)
    monkeypatch.setattr(h, "_get_ddb", lambda: r)
    body = json.loads(h.handler(_event(), None)["body"])
    assert body["vehicle"]["fuelLevel"] is None, \
        f"fuelLevel must be null when batteryCharge is null, got {body['vehicle']['fuelLevel']!r} (R1 guard)"


def test_snapshot_null_health_stays_null(h, monkeypatch):
    """R2 guard: batteryHealth.value = null → batterySoh null, NOT the row value 92.0."""
    r = _make_resource(snapshot=SNAPSHOT_NULL_HEALTH)
    monkeypatch.setattr(h, "_get_ddb", lambda: r)
    body = json.loads(h.handler(_event(), None)["body"])
    assert body["vehicle"]["batterySoh"] is None, \
        f"batterySoh must be null when batteryHealth is null, got {body['vehicle']['batterySoh']!r} (R2 guard)"


# ---------------------------------------------------------------------------
# R6–R8: values and snapshot blocks equal the pinned-item contents
# ---------------------------------------------------------------------------

def _dec(v):
    """Convert Decimal to int/float as _DecimalEncoder does."""
    if isinstance(v, Decimal):
        return int(v) if v % 1 == 0 else float(v)
    if isinstance(v, dict):
        return {k: _dec(vv) for k, vv in v.items()}
    if isinstance(v, list):
        return [_dec(i) for i in v]
    return v


def test_values_block_equals_pinned_item(h, monkeypatch):
    """R6 / R7 guard: values block carries all seven sub-fields from the pinned item.

    Dropping any sub-field (R6: odometer; R7: service) leaves a key missing or None
    where the pinned item's data should appear.
    """
    r = _make_resource(snapshot=PINNED_ITEM)
    monkeypatch.setattr(h, "_get_ddb", lambda: r)
    body = json.loads(h.handler(_event(), None)["body"])
    v = body["values"]
    assert v is not None, "values block must be present with a snapshot item"
    # Each sub-field equals the decoded pinned item field.
    for key in ("odometer", "batteryCharge", "batteryHealth", "batteryAge", "tires", "service", "faults"):
        assert v[key] == _dec(PINNED_ITEM[key]), (
            f"values.{key} must equal pinned item field (R6/R7 guard); "
            f"got {v[key]!r}, expected {_dec(PINNED_ITEM[key])!r}"
        )


def test_snapshot_block_equals_pinned_item(h, monkeypatch):
    """R8 guard: snapshot block carries runDate, dataThrough, computedAt."""
    r = _make_resource(snapshot=PINNED_ITEM)
    monkeypatch.setattr(h, "_get_ddb", lambda: r)
    body = json.loads(h.handler(_event(), None)["body"])
    s = body["snapshot"]
    assert s is not None, "snapshot block must be present"
    assert s["runDate"]    == PINNED_ITEM["runDate"],    f"runDate mismatch (R8 guard)"
    assert s["dataThrough"] == PINNED_ITEM["dataThrough"], f"dataThrough mismatch"
    assert s["computedAt"] == PINNED_ITEM["computedAt"], f"computedAt mismatch"


# ---------------------------------------------------------------------------
# R11: snapshot read error is logged at WARNING
# ---------------------------------------------------------------------------

def test_error_is_logged_on_snapshot_exception(h, monkeypatch, caplog):
    """R11 guard: a DDB exception on the snapshot read must be logged at WARNING."""
    import logging
    r = _make_resource(snapshot_raises=True)
    monkeypatch.setattr(h, "_get_ddb", lambda: r)
    with caplog.at_level(logging.WARNING):
        h.handler(_event(), None)
    assert any("snapshot read failed" in rec.message for rec in caplog.records), (
        "snapshot read error must be logged at WARNING; no matching log record found (R11 guard)"
    )


# ---------------------------------------------------------------------------
# R3 (key value guard): GetItem value must be vehicleId, not VIN
# ---------------------------------------------------------------------------

def test_snapshot_key_value_is_vehicle_id_not_vin(h, monkeypatch):
    """R3 guard: the snapshot GetItem key value must be the vehicleId ('VEH-001'),
    not the VIN ('VIN-TEST-001').  The strict fake returns the item only for the
    vehicleId value; a vin-value call returns {} → snapshotStatus 'none'.
    """
    r = _make_resource_strict(snapshot=PINNED_ITEM, expected_vehicle_id="VEH-001")
    monkeypatch.setattr(h, "_get_ddb", lambda: r)
    body = json.loads(h.handler(_event(), None)["body"])
    assert body["snapshotStatus"] == "ok", (
        f"strict fake must find item via vehicleId VALUE 'VEH-001', "
        f"got snapshotStatus={body['snapshotStatus']!r} (R3 guard)"
    )
    # Confirm it used the vehicleId value (21268 after floor, not row's 39000).
    assert body["vehicle"]["odometer"] == 21268, (
        f"odometer must be snapshot value 21268, not row value 39000 (R3 guard)"
    )


# ---------------------------------------------------------------------------
# Non-electric fuelLevel rule (decisions.md 2026-10-24 ~01:25Z, AVX S3):
# batteryCharge fills fuelLevel only for BEV; non-EV keeps row fuelLevel.
# ---------------------------------------------------------------------------

def test_non_electric_car_fuel_level_unchanged(h, monkeypatch):
    """Non-electric car: snapshot batteryCharge must NOT overwrite fuelLevel.
    The row's fuelLevel (60.0 for a gasoline car) should remain.
    """
    gasoline_vehicle = dict(VEHICLE_ROW, fuelType="gasoline", fuelLevel=60.0)
    r = _make_resource(snapshot=SNAPSHOT_ITEM, vehicle=gasoline_vehicle)
    monkeypatch.setattr(h, "_get_ddb", lambda: r)
    body = json.loads(h.handler(_event(), None)["body"])
    veh = body["vehicle"]
    assert veh["fuelLevel"] == 60.0, (
        f"non-EV fuelLevel must stay as row value 60.0, got {veh['fuelLevel']!r}"
    )


def test_electric_car_fuel_level_becomes_battery_charge(h, monkeypatch):
    """BEV: snapshot batteryCharge DOES overwrite fuelLevel (confirm the rule fires)."""
    bev_vehicle = dict(VEHICLE_ROW, fuelType="bev", fuelLevel=55.0)
    r = _make_resource(snapshot=SNAPSHOT_ITEM, vehicle=bev_vehicle)
    monkeypatch.setattr(h, "_get_ddb", lambda: r)
    body = json.loads(h.handler(_event(), None)["body"])
    # SNAPSHOT_ITEM.batteryCharge.value = 72
    assert body["vehicle"]["fuelLevel"] == 72, (
        f"BEV fuelLevel must be snapshot batteryCharge 72, got {body['vehicle']['fuelLevel']!r}"
    )


@pytest.mark.parametrize("fuel_type", ["bev", "electric", "ev", "EV", " Electric "])
def test_electric_car_fuel_level_becomes_battery_charge_synonyms(h, monkeypatch, fuel_type):
    """BEV1 guard: all EV fuel-type synonyms trigger the batteryCharge→fuelLevel rule.

    BEV1 mutant drops 'electric' from _is_bev; a staging electric car (4 166 of 4 250 rows)
    would then keep the undated row fuelLevel instead of the snapshot charge.
    """
    ev_vehicle = dict(VEHICLE_ROW, fuelType=fuel_type, fuelLevel=55.0)
    r = _make_resource(snapshot=SNAPSHOT_ITEM, vehicle=ev_vehicle)
    monkeypatch.setattr(h, "_get_ddb", lambda: r)
    body = json.loads(h.handler(_event(), None)["body"])
    assert body["vehicle"]["fuelLevel"] == 72, (
        f"fuelType={fuel_type!r}: fuelLevel must be snapshot batteryCharge 72 "
        f"(BEV1 guard), got {body['vehicle']['fuelLevel']!r}"
    )


# ---------------------------------------------------------------------------
# W1 / M2-2 (cycle 2): floor pinned against round for .7 fractions
# ---------------------------------------------------------------------------

def test_fractional_odometer_floors_not_rounds_7(h, monkeypatch):
    """M2-2 guard: Decimal('21268.7') → 21268 (floor), not 21269 (round).

    M2-2 replaces math.floor with round; floor(21268.7)=21268 but round(21268.7)=21269.
    The existing PINNED_ITEM fixture ('21268.4') cannot distinguish them (both give 21268).
    decisions.md 2026-10-24 ~02:40Z: pin it.
    """
    item_07 = dict(PINNED_ITEM, odometer={"value": Decimal("21268.7"), "asOf": "2026-10-01", "basis": "history"})
    r = _make_resource(snapshot=item_07)
    monkeypatch.setattr(h, "_get_ddb", lambda: r)
    body = json.loads(h.handler(_event(), None)["body"])
    veh = body["vehicle"]
    assert veh["odometer"] == 21268, (
        f"floor(21268.7) must give 21268, not {veh['odometer']!r} "
        f"(M2-2 guard — round gives 21269)"
    )
    assert isinstance(veh["odometer"], int)


# ---------------------------------------------------------------------------
# S1 (cycle 2): snapshot read error leaves non-EV fuelLevel as the row value
# ---------------------------------------------------------------------------

def test_snapshot_error_non_bev_fuel_level_unchanged(h, monkeypatch):
    """S1 guard: snapshot read error on a non-electric car must NOT null fuelLevel.

    The row already carries a valid fuel reading; a read failure provides no substitute.
    """
    gasoline_vehicle = dict(VEHICLE_ROW, fuelType="gasoline", fuelLevel=45.5)
    r = _make_resource(snapshot_raises=True, vehicle=gasoline_vehicle)
    monkeypatch.setattr(h, "_get_ddb", lambda: r)
    body = json.loads(h.handler(_event(), None)["body"])
    assert body["snapshotStatus"] == "error"
    assert body["vehicle"]["fuelLevel"] == 45.5, (
        f"non-EV fuelLevel must stay as row value 45.5 on error (S1 guard), "
        f"got {body['vehicle']['fuelLevel']!r}"
    )
    assert body["vehicle"]["odometer"] is None, "odometer still null on error"
    assert body["vehicle"]["batterySoh"] is None, "batterySoh still null on error"


# ---------------------------------------------------------------------------
# S2 (cycle 2): non-number odometer value → null fields, not a 500
# ---------------------------------------------------------------------------

def test_floor_odometer_non_number_returns_null(h, monkeypatch):
    """S2 guard: a non-number odometer.value gives null, not a TypeError 500.

    _snapshot_scalar passes non-Decimal values through; a string then reaches
    _floor_odometer and raises TypeError → 500.  Fix: guard non-numeric types.
    """
    string_snap = dict(PINNED_ITEM,
                       odometer={"value": "21268.4km", "asOf": "2026-10-01", "basis": "history"})
    r = _make_resource(snapshot=string_snap)
    monkeypatch.setattr(h, "_get_ddb", lambda: r)
    resp = h.handler(_event(), None)
    assert resp["statusCode"] == 200, (
        f"non-number odometer must give 200, not a 500 (S2 guard), got {resp['statusCode']}"
    )
    body = json.loads(resp["body"])
    assert body["vehicle"]["odometer"] is None, (
        f"non-number odometer must be null (S2 guard), got {body['vehicle']['odometer']!r}"
    )
    assert body["vehicle"]["mileage"] is None, "mileage must also be null"


# ---------------------------------------------------------------------------
# S2b guard: bool odometer value → null (not floor(True)==1)
# ---------------------------------------------------------------------------

def test_bool_odometer_returns_null(h, monkeypatch):
    """S2b guard: snapshot odometer.value=True must produce null, not floor(True)==1.

    S2b drops isinstance(val, bool) from _floor_odometer; without the guard
    math.floor(True) == 1 and odometer/mileage arrive as 1 instead of null.
    """
    bool_snap = dict(PINNED_ITEM,
                     odometer={"value": True, "asOf": "2026-10-01", "basis": "history"})
    r = _make_resource(snapshot=bool_snap)
    monkeypatch.setattr(h, "_get_ddb", lambda: r)
    body = json.loads(h.handler(_event(), None)["body"])
    assert body["vehicle"]["odometer"] is None, (
        f"odometer True must be null (S2b guard — bool is not an odometer), "
        f"got {body['vehicle']['odometer']!r}"
    )
    assert body["vehicle"]["mileage"] is None, "mileage must also be null when odometer is null"


# ---------------------------------------------------------------------------
# S3 guard: string / bool batteryCharge/batteryHealth → null fuelLevel/batterySoh
# (SCAL + SCALB mutations, drivers-me)
# ---------------------------------------------------------------------------

def test_string_battery_charge_returns_null_fuel_level(h, monkeypatch):
    """SCAL guard: batteryCharge.value='72%' must produce null fuelLevel, not pass the string.

    A string in fuelLevel causes Swift JSONDecoder typeMismatch for the entire
    /drivers/me response — currentDriver/currentVehicle both become nil on iOS.
    _snapshot_scalar must reject non-int/float/Decimal values.
    """
    str_charge_snap = dict(SNAPSHOT_ITEM, batteryCharge={"value": "72%", "asOf": "2026-10-01", "unit": "pct"})
    r = _make_resource(snapshot=str_charge_snap)
    monkeypatch.setattr(h, "_get_ddb", lambda: r)
    result = h.handler(_event(), None)
    assert result["statusCode"] == 200, f"expected 200, got {result['statusCode']}"
    body = json.loads(result["body"])
    assert body["vehicle"]["fuelLevel"] is None, (
        f"batteryCharge.value='72%' must give null fuelLevel (SCAL guard — string breaks iOS decode), "
        f"got {body['vehicle']['fuelLevel']!r}"
    )


def test_bool_battery_values_return_null(h, monkeypatch):
    """SCALB guard: bool batteryCharge/batteryHealth must produce null fuelLevel/batterySoh.

    {"value": True} in batteryCharge or batteryHealth reaches fuelLevel/batterySoh
    (typed Double? on iOS); a bool there raises Swift JSONDecoder typeMismatch.
    _snapshot_scalar must reject bool values (isinstance(val, bool) check).
    """
    bool_snap = dict(SNAPSHOT_ITEM,
                     batteryCharge={"value": True, "asOf": "2026-10-01", "unit": "pct"},
                     batteryHealth={"value": True, "asOf": "2026-10-01", "basis": "history"})
    r = _make_resource(snapshot=bool_snap)
    monkeypatch.setattr(h, "_get_ddb", lambda: r)
    result = h.handler(_event(), None)
    assert result["statusCode"] == 200, f"expected 200, got {result['statusCode']}"
    body = json.loads(result["body"])
    assert body["vehicle"]["fuelLevel"] is None, (
        f"batteryCharge.value=True must give null fuelLevel (SCALB guard — bool breaks iOS decode), "
        f"got {body['vehicle']['fuelLevel']!r}"
    )
    assert body["vehicle"]["batterySoh"] is None, (
        f"batteryHealth.value=True must give null batterySoh (SCALB guard — bool breaks iOS decode), "
        f"got {body['vehicle']['batterySoh']!r}"
    )


# ---------------------------------------------------------------------------
# W2(a) guard: live test path calls fail() not skip() when driver isn't found.
# Unit test (no LIVE_TESTS, no AWS) showing the handler path that triggers fail.
# ---------------------------------------------------------------------------

def test_handler_returns_vehicle_null_when_driver_not_found(h, monkeypatch):
    """W2(a) guard: handler returns vehicle=null when no driver row is found.

    This is the state that the live test now FAILs on (not skips).
    Confirms the fail-not-skip path by monkeypatching _get_ddb to find no driver.
    """
    class _EmptyDriverTable:
        def scan(self, **kw):
            return {"Items": []}  # driver not found, no LastEvaluatedKey = exhausted

    class _EmptyRes:
        def Table(self, name):
            return _EmptyDriverTable()

    monkeypatch.setattr(h, "_get_ddb", lambda: _EmptyRes())
    event = {"requestContext": {"authorizer": {"claims": {
        "email": "meridian.driver@example.com", "email_verified": "true",
    }}}}
    body = json.loads(h.handler(event, None)["body"])
    assert body.get("vehicle") is None, (
        f"expected vehicle=null when driver not found, got {body.get('vehicle')!r}; "
        "the live test now calls pytest.fail() on this outcome (W2a guard)"
    )


# ---------------------------------------------------------------------------
# LIVE_TESTS=1: read a real snapshot item through both handlers (skipped by default).
# Table doesn't exist until stage C deploys; this test is present for 7.4/7.5 execution.
# ---------------------------------------------------------------------------

import os as _os
import pytest as _pytest

_LIVE_READ_ALLOWLIST = frozenset({
    "GetItem", "BatchGetItem", "Query", "Scan", "DescribeTable",
})


@_pytest.fixture
def _live_read_only_guard(monkeypatch):
    """W2: restrict botocore to read operations only for the duration of this test.

    Wraps botocore.client.BaseClient._make_api_call and raises AssertionError
    for any operation not in _LIVE_READ_ALLOWLIST, before any network call is made.
    """
    import botocore.client
    _orig = botocore.client.BaseClient._make_api_call

    def _guarded(self, operation_name, api_params):
        if operation_name not in _LIVE_READ_ALLOWLIST:
            raise AssertionError(
                f"W2 live-read guard: {operation_name!r} is not in the read allowlist "
                f"{sorted(_LIVE_READ_ALLOWLIST)!r}"
            )
        return _orig(self, operation_name, api_params)

    monkeypatch.setattr(botocore.client.BaseClient, "_make_api_call", _guarded)


def test_live_read_only_guard_unit_dm(_live_read_only_guard):
    """W2 self-test (no AWS): the REAL fixture refuses PutItem before any network
    call and lets GetItem through to the (dead) endpoint."""
    import boto3

    client = boto3.client(
        "dynamodb", region_name="us-east-1",
        endpoint_url="http://127.0.0.1:1",
        aws_access_key_id="x", aws_secret_access_key="x",
        config=boto3.session.Config(retries={"max_attempts": 1, "mode": "standard"},
                                    connect_timeout=1, read_timeout=1),
    )
    # PutItem must be blocked by the guard before any network attempt
    with _pytest.raises(Exception) as ei:
        client.put_item(TableName="t", Item={"pk": {"S": "v"}})
    assert type(ei.value) is AssertionError and "W2 live-read guard" in str(ei.value), (
        f"PutItem must be refused by the guard before sending; got {type(ei.value).__name__}: {ei.value}")
    # GetItem must pass the guard (connection refused at dead endpoint — that's fine)
    try:
        client.get_item(TableName="t", Key={"pk": {"S": "v"}})
    except AssertionError:
        _pytest.fail("GetItem should pass W2 guard but was blocked")
    except Exception:
        pass  # connection refused or similar — expected with dead endpoint


@_pytest.mark.live_aws
@_pytest.mark.skipif(
    _os.environ.get("LIVE_TESTS") != "1",
    reason="LIVE_TESTS=1 required; runs in 7.4/7.5 after the snapshot table exists",
)
def test_live_snapshot_read_via_drivers_me(_live_read_only_guard):
    """LIVE: call api-drivers-me handler → verify snapshotStatus ok and int odometer.

    W3 (cycle 2) fixes:
      - @pytest.mark.live_aws required (conftest blocks real calls without it)
      - CMS_STAGE must be set (fails rather than defaulting to cms-dev-*)
      - asserts snapshotStatus == 'ok' (LV1: fails on wrong table → snapshotStatus 'error')
      - compares odometer against floor(GetItem value) to pin the precedence rule
    """
    import importlib.util as _ilu
    import json as _json
    import math as _math
    import pathlib as _pl

    stage = _os.environ.get("CMS_STAGE")
    if not stage:
        _pytest.fail(
            "CMS_STAGE must be set for live snapshot tests (e.g. export CMS_STAGE=staging)"
        )

    _handler_path = _pl.Path(__file__).resolve().parents[1] / "handler.py"
    _spec = _ilu.spec_from_file_location("dm_live", _handler_path)
    _mod = _ilu.module_from_spec(_spec)
    _spec.loader.exec_module(_mod)

    email = _os.environ.get("TEST_DRIVER_EMAIL", "meridian.driver@example.com")
    event = {"requestContext": {"authorizer": {"claims": {
        "email": email, "email_verified": "true",
    }}}}
    resp = _mod.handler(event, None)
    assert resp["statusCode"] == 200, f"expected 200, got {resp['statusCode']}: {resp['body'][:200]}"
    body = _json.loads(resp["body"])

    if body.get("vehicle") is None:
        _pytest.fail(f"no driver/vehicle for {email!r} in this stage — driver not found on any page (W2a guard)")

    # LV1 negative control: wrong table → snapshotStatus 'error' → this assertion fails
    assert body["snapshotStatus"] == "ok", (
        f"snapshotStatus must be 'ok' (table may not exist or have no item); "
        f"got {body['snapshotStatus']!r}"
    )

    veh = body["vehicle"]
    odo = veh.get("odometer")
    mil = veh.get("mileage")
    assert odo is None or isinstance(odo, int), (
        f"live odometer must be int or null (iOS Int?), got {odo!r} ({type(odo).__name__})"
    )
    assert mil is None or isinstance(mil, int), f"live mileage must be int or null, got {mil!r}"
    assert odo == mil, f"odometer {odo!r} must equal mileage {mil!r}"

    # Compare against direct GetItem — pins floor(item.odometer.value)
    import boto3 as _boto3
    from decimal import Decimal as _Dec
    _ddb = _boto3.resource("dynamodb")
    _table_name = _mod._SNAPSHOT_TABLE
    vid = veh.get("vehicleId")
    if vid:
        _raw = _ddb.Table(_table_name).get_item(Key={"vehicleId": vid}).get("Item", {})
        _raw_odo_field = _raw.get("odometer", {})
        _raw_odo_val = _raw_odo_field.get("value") if isinstance(_raw_odo_field, dict) else None
        if _raw_odo_val is not None and isinstance(_raw_odo_val, (int, float, _Dec)):
            expected_odo = int(_math.floor(float(_raw_odo_val)))
            assert odo == expected_odo, (
                f"odometer {odo!r} must equal floor({_raw_odo_val!r}) = {expected_odo}"
            )


# ---------------------------------------------------------------------------
# Pagination: _resolve_driver_by_email must follow LastEvaluatedKey
# ---------------------------------------------------------------------------

class _MultiPageDriversTable:
    """Puts the match on page 3 (ExclusiveStartKey == {"pk": "3"}).
    Raises on call 100+ to expose an infinite loop in broken code."""

    def __init__(self, match_row: dict):
        self._match = match_row
        self._calls = 0

    def scan(self, **kw: Any) -> dict:
        self._calls += 1
        if self._calls >= 100:
            raise AssertionError(f"scan called {self._calls} times — pagination loop")
        esk = kw.get("ExclusiveStartKey")
        if esk == {"pk": "3"}:
            return {"Items": [dict(self._match)]}
        if esk is None:
            return {"Items": [], "LastEvaluatedKey": {"pk": "2"}}
        if esk == {"pk": "2"}:
            return {"Items": [], "LastEvaluatedKey": {"pk": "3"}}
        return {"Items": []}


def test_resolve_driver_follows_pagination(h, monkeypatch):
    """_resolve_driver_by_email must follow LastEvaluatedKey; match is on page 3."""
    t = _MultiPageDriversTable(dict(DRIVER_ROW))

    class _R:
        def Table(self, _name: str) -> Any:  # noqa: N802
            return t

    monkeypatch.setattr(h, "_get_ddb", lambda: _R())
    result = h._resolve_driver_by_email(EMAIL)
    assert result is not None, "expected driver on page 3 — pagination not followed"
    assert result["driverId"] == DRIVER_ROW["driverId"]
    assert t._calls == 3, f"expected exactly 3 scan calls, got {t._calls}"


class _InfiniteDriversTable:
    """Always returns LastEvaluatedKey — never finds a match. Safety-cap at 100 calls."""

    def __init__(self):
        self.calls = 0

    def scan(self, **kw: Any) -> dict:
        self.calls += 1
        if self.calls >= 100:
            raise AssertionError(f"scan called {self.calls} times — page cap not effective")
        return {"Items": [], "LastEvaluatedKey": {"pk": str(self.calls)}}


def test_resolve_driver_page_cap_returns_none(h, monkeypatch, caplog):
    """CAP guard: _resolve_driver_by_email stops at _MAX_SCAN_PAGES, logs a warning, returns None.

    CAP replaces range(_MAX_SCAN_PAGES) with an unbounded loop; the fake raises at 100
    to surface that, and the scan-count assertion fails before that point.
    """
    import logging
    t = _InfiniteDriversTable()

    class _R:
        def Table(self, _name: str) -> Any:  # noqa: N802
            return t

    monkeypatch.setattr(h, "_get_ddb", lambda: _R())
    with caplog.at_level(logging.WARNING):
        result = h._resolve_driver_by_email(EMAIL)
    assert result is None, "expected None after page cap"
    assert t.calls == h._MAX_SCAN_PAGES, (
        f"expected exactly {h._MAX_SCAN_PAGES} scans (page cap), got {t.calls}"
    )
    assert "exceeded page cap" in caplog.text, "expected cap warning in logs"
