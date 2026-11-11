"""Stage C: api-vehicle-context reads the vehicle snapshot and overlays four fields.

Same rules as api-drivers-me/tests/test_snapshot_read.py; adapted to the
vehicle-context response shape (vehicle nested under "vehicle" key, additional
healthScore/activeDtcs/driver keys present).

Mutations guarded:
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

VEHICLE_ID = "VEH-001"

VEHICLE_ROW = {
    "vehicleId": VEHICLE_ID,
    "vin": "VIN-TEST-001",
    "odometer": 39000,
    "mileage": 38500,
    "fuelLevel": 60.0,
    "batterySoh": 92.0,
    "fuelType": "bev",
    "connectionStatus": "connected",
}

SNAPSHOT_ITEM = {
    "vehicleId": VEHICLE_ID,
    "schemaVersion": 1,
    "odometer":      {"value": Decimal("45000"), "asOf": "2026-10-01", "basis": "history"},
    "batteryCharge": {"value": Decimal("72"),    "asOf": "2026-10-01T10:00:00Z", "basis": "feed"},
    "batteryHealth": {"value": Decimal("85"),    "asOf": "2026-10-01", "basis": "history"},
    "batteryAge":    {"value": Decimal("365"),   "asOf": "2026-10-01", "basis": "history"},
    "tires": {"asOf": None, "basis": "history", "positions": {}},
    "service": {"open": 0, "openAsOf": None, "completed": 1, "completedAsOf": "2026-09-01", "basis": "history"},
    "faults": {"open": [], "history": [], "asOf": None, "basis": "history"},
    "runDate": "2026-10-01",
    "dataThrough": "2026-09-30",
    "computedAt": "2026-10-01T02:00:00Z",
}

SNAPSHOT_NULL_ODO = dict(SNAPSHOT_ITEM, odometer={"value": None, "asOf": None, "basis": "history"})


# ---------------------------------------------------------------------------
# Mock infrastructure
# ---------------------------------------------------------------------------

class _SnapshotTable:
    def __init__(self, item=None, raises: bool = False):
        self._item = item
        self._raises = raises
        self.calls: list[dict] = []

    def get_item(self, **kw: Any) -> dict:
        self.calls.append(dict(kw.get("Key", {})))
        if self._raises:
            raise RuntimeError("simulated DDB error")
        if self._item is not None:
            return {"Item": dict(self._item)}
        return {}


class _VehicleTable:
    """Returns a vehicle row on get_item (for the vehicles table)."""
    def __init__(self, vehicle):
        self._vehicle = vehicle

    def get_item(self, **kw: Any) -> dict:
        return {"Item": dict(self._vehicle)}


class _ScanTable:
    """Returns empty on both scan and query (for drivers, dtc-history, service tables)."""
    def scan(self, **kw: Any) -> dict:
        return {"Items": []}

    def query(self, **kw: Any) -> dict:
        return {"Items": []}


class _Resource:
    """Routes Table() calls by name substring."""
    def __init__(self, vehicle: dict, snapshot: _SnapshotTable):
        self._vehicle = _VehicleTable(vehicle)
        self._snapshot = snapshot
        self._empty = _ScanTable()

    def Table(self, name: str) -> Any:  # noqa: N802
        if "snapshot" in name:
            return self._snapshot
        if "vehicle" in name and "dtc" not in name and "service" not in name:
            return self._vehicle
        return self._empty


def _load():
    spec = importlib.util.spec_from_file_location("vc_snapshot_h", _HANDLER)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _event(vehicle_id: str = VEHICLE_ID) -> dict:
    # A platform-admin caller: these tests are about the snapshot overlay, and the route
    # refuses a request with no caller scope (tests/test_context_scope.py).
    return {
        "pathParameters": {"vehicleId": vehicle_id},
        "requestContext": {"authorizer": {"claims": {
            "sub": "test-admin", "cognito:groups": "platform-admin",
        }}},
    }


# ---------------------------------------------------------------------------
# Helpers: call the handler with mocked DDB + no live-state invoke
# ---------------------------------------------------------------------------

@pytest.fixture
def h(monkeypatch):
    mod = _load()
    # Disable the live-state Lambda invoke — not needed for snapshot tests
    monkeypatch.setattr(mod, "_load_live_connection_state", lambda vid: None)
    return mod


def _call(h, monkeypatch, snapshot=None, snapshot_raises=False, vehicle=None):
    veh = vehicle or dict(VEHICLE_ROW)
    r = _Resource(vehicle=veh, snapshot=_SnapshotTable(item=snapshot, raises=snapshot_raises))
    monkeypatch.setattr(h, "_get_ddb", lambda: r)
    resp = h.handler(_event(), None)
    return resp, r


# ---------------------------------------------------------------------------
# Snapshot overrides
# ---------------------------------------------------------------------------

def test_snapshot_odometer_overwrites_both_fields(h, monkeypatch):
    """Snapshot odometer (45 000) overwrites vehicle.odometer and .mileage (was 38 500)."""
    resp, _ = _call(h, monkeypatch, snapshot=SNAPSHOT_ITEM)
    assert resp["statusCode"] == 200
    body = json.loads(resp["body"])
    veh = body["vehicle"]
    assert veh["odometer"] == 45000
    assert veh["mileage"] == 45000, "mileage must equal odometer, not row value 38500"


def test_snapshot_battery_charge_becomes_fuel_level(h, monkeypatch):
    resp, _ = _call(h, monkeypatch, snapshot=SNAPSHOT_ITEM)
    assert json.loads(resp["body"])["vehicle"]["fuelLevel"] == 72


def test_snapshot_battery_health_becomes_battery_soh(h, monkeypatch):
    resp, _ = _call(h, monkeypatch, snapshot=SNAPSHOT_ITEM)
    assert json.loads(resp["body"])["vehicle"]["batterySoh"] == 85


def test_snapshot_status_ok_when_item_present(h, monkeypatch):
    resp, _ = _call(h, monkeypatch, snapshot=SNAPSHOT_ITEM)
    assert json.loads(resp["body"])["snapshotStatus"] == "ok"


# ---------------------------------------------------------------------------
# Null snapshot value — mutation guard M1
# ---------------------------------------------------------------------------

def test_snapshot_null_odometer_sets_both_to_null(h, monkeypatch):
    """M1: snapshot item with odometer.value=null → both fields null, not row value."""
    resp, _ = _call(h, monkeypatch, snapshot=SNAPSHOT_NULL_ODO)
    body = json.loads(resp["body"])
    veh = body["vehicle"]
    assert veh["odometer"] is None, "odometer must be null, not row value 39000"
    assert veh["mileage"] is None, "mileage must be null, not row value 38500 (M1 guard)"


# ---------------------------------------------------------------------------
# No snapshot item
# ---------------------------------------------------------------------------

def test_no_snapshot_item_leaves_row_values_intact(h, monkeypatch):
    """No snapshot item → row values unchanged, snapshotStatus 'none'."""
    resp, _ = _call(h, monkeypatch, snapshot=None)
    body = json.loads(resp["body"])
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
    """M2: DDB exception → all four fields null, not row values."""
    resp, _ = _call(h, monkeypatch, snapshot_raises=True)
    body = json.loads(resp["body"])
    veh = body["vehicle"]
    assert veh["odometer"] is None, "odometer must be null on error, not row value (M2 guard)"
    assert veh["mileage"] is None
    assert veh["fuelLevel"] is None
    assert veh["batterySoh"] is None


def test_snapshot_error_returns_200(h, monkeypatch):
    """Snapshot read error must not surface as a non-200 to iOS."""
    resp, _ = _call(h, monkeypatch, snapshot_raises=True)
    assert resp["statusCode"] == 200


def test_snapshot_error_sets_status_error(h, monkeypatch):
    resp, _ = _call(h, monkeypatch, snapshot_raises=True)
    assert json.loads(resp["body"])["snapshotStatus"] == "error"


# ---------------------------------------------------------------------------
# GetItem key — mutation guard M3
# ---------------------------------------------------------------------------

def test_snapshot_key_is_vehicle_id_not_vin(h, monkeypatch):
    """M3: GetItem must use Key={'vehicleId': id}, never {'vin': id}."""
    resp, r = _call(h, monkeypatch, snapshot=SNAPSHOT_ITEM)
    snap_calls = r._snapshot.calls
    assert snap_calls, "snapshot GetItem must be called"
    assert all("vehicleId" in call for call in snap_calls), \
        f"Key must use vehicleId, got {snap_calls}"
    assert not any("vin" in call for call in snap_calls), \
        f"Key must NOT use vin, got {snap_calls}"


# ---------------------------------------------------------------------------
# snapshotStatus "none" when no item
# ---------------------------------------------------------------------------

def test_snapshot_status_none_when_no_item(h, monkeypatch):
    resp, _ = _call(h, monkeypatch, snapshot=None)
    assert json.loads(resp["body"])["snapshotStatus"] == "none"


# ---------------------------------------------------------------------------
# values / snapshot blocks
# ---------------------------------------------------------------------------

def test_values_block_present_when_snapshot_item(h, monkeypatch):
    resp, _ = _call(h, monkeypatch, snapshot=SNAPSHOT_ITEM)
    body = json.loads(resp["body"])
    assert body["values"] is not None
    assert "batteryCharge" in body["values"]
    assert "batteryHealth" in body["values"]


def test_values_block_null_when_no_item(h, monkeypatch):
    resp, _ = _call(h, monkeypatch, snapshot=None)
    assert json.loads(resp["body"])["values"] is None


def test_values_block_null_when_error(h, monkeypatch):
    resp, _ = _call(h, monkeypatch, snapshot_raises=True)
    assert json.loads(resp["body"])["values"] is None


# ---------------------------------------------------------------------------
# Decimal conversion
# ---------------------------------------------------------------------------

def test_decimal_zero_survives_as_int_zero(h, monkeypatch):
    """A real snapshot odometer of 0 must not be suppressed."""
    zero_snap = dict(SNAPSHOT_ITEM, odometer={"value": Decimal("0"), "asOf": "2026-10-01", "basis": "history"})
    resp, _ = _call(h, monkeypatch, snapshot=zero_snap)
    body = json.loads(resp["body"])
    assert body["vehicle"]["odometer"] == 0
    assert body["vehicle"]["mileage"] == 0


def test_route_still_returns_200_and_health_score_when_snapshot_ok(h, monkeypatch):
    """Smoke: snapshot read doesn't disrupt the existing healthScore/activeDtcs path."""
    resp, _ = _call(h, monkeypatch, snapshot=SNAPSHOT_ITEM)
    body = json.loads(resp["body"])
    assert resp["statusCode"] == 200
    assert "healthScore" in body
    assert "activeDtcs" in body

# ---------------------------------------------------------------------------
# PINNED item (lane S pinned shape; fractional odometer, numeric faults, tires)
# ---------------------------------------------------------------------------
PINNED_ITEM = {
    "vehicleId": VEHICLE_ID,
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

# Battery snapshot item with health below threshold (75%) and row SoH above (92%).
# R5 guard: score must use the snapshot SoH (75%), not the row SoH (92%).
SNAPSHOT_LOW_SOH = dict(SNAPSHOT_ITEM,
    batteryHealth={"value": Decimal("75"), "asOf": "2026-10-01", "basis": "history"})
VEHICLE_ROW_HIGH_SOH = dict(VEHICLE_ROW, batterySoh=Decimal("92"))


class _StrictSnapshotTable:
    """Returns item only for the exact expected vehicleId value (R3/R4 guard)."""
    def __init__(self, item=None, raises: bool = False, expected_vehicle_id: str = VEHICLE_ID):
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


def _make_resource_strict(vehicle=None, snapshot=None, snapshot_raises=False,
                            expected_vehicle_id=VEHICLE_ID):
    veh = vehicle or dict(VEHICLE_ROW)
    return _Resource(
        vehicle=veh,
        snapshot=_StrictSnapshotTable(item=snapshot, raises=snapshot_raises,
                                       expected_vehicle_id=expected_vehicle_id),
    )


# ---------------------------------------------------------------------------
# C1: fractional odometer → floored int (M2-1 guard)
# ---------------------------------------------------------------------------

def test_fractional_odometer_floors_to_int(h, monkeypatch):
    """C1 / M2-1: Decimal('21268.4') → odometer == mileage == 21268, type int."""
    resp, _ = _call(h, monkeypatch, snapshot=PINNED_ITEM)
    body = json.loads(resp["body"])
    veh = body["vehicle"]
    assert veh["odometer"] == 21268, f"expected 21268 (floor), got {veh['odometer']!r}"
    assert veh["mileage"] == 21268, f"mileage must equal odometer, got {veh['mileage']!r}"
    assert isinstance(veh["odometer"], int), f"odometer must be int (M2-1 guard), got {type(veh['odometer'])}"
    assert isinstance(veh["mileage"], int), f"mileage must be int, got {type(veh['mileage'])}"


def test_ios_int_fields_are_int_or_null(h, monkeypatch):
    """C1 iOS contract: year, odometer, mileage, totalTrips must be int or null."""
    vehicle = dict(VEHICLE_ROW, year=2022, totalTrips=12)
    r = _Resource(vehicle=vehicle, snapshot=_SnapshotTable(item=PINNED_ITEM))
    monkeypatch.setattr(h, "_get_ddb", lambda: r)
    resp = h.handler(_event(), None)
    body = json.loads(resp["body"])
    veh = body["vehicle"]
    for field in ("year", "odometer", "mileage", "totalTrips"):
        val = veh.get(field)
        assert val is None or isinstance(val, int), (
            f"iOS Int? field '{field}' must be int or null, got {val!r} ({type(val).__name__})"
        )


# ---------------------------------------------------------------------------
# R1vc / R2vc: null charge and null health stay null — never row fallback
# ---------------------------------------------------------------------------

def test_snapshot_null_charge_stays_null(h, monkeypatch):
    """R1vc guard: batteryCharge.value = null → fuelLevel null, NOT row value 60.0."""
    resp, _ = _call(h, monkeypatch, snapshot=SNAPSHOT_NULL_CHARGE)
    body = json.loads(resp["body"])
    assert body["vehicle"]["fuelLevel"] is None, (
        f"fuelLevel must be null when batteryCharge is null, got "
        f"{body['vehicle']['fuelLevel']!r} (R1vc guard)"
    )


def test_snapshot_null_health_stays_null(h, monkeypatch):
    """R2vc guard: batteryHealth.value = null → batterySoh null, NOT row value 92.0."""
    resp, _ = _call(h, monkeypatch, snapshot=SNAPSHOT_NULL_HEALTH)
    body = json.loads(resp["body"])
    assert body["vehicle"]["batterySoh"] is None, (
        f"batterySoh must be null when batteryHealth is null, got "
        f"{body['vehicle']['batterySoh']!r} (R2vc guard)"
    )


# ---------------------------------------------------------------------------
# R5: health score uses snapshot SoH, not row SoH
# ---------------------------------------------------------------------------

def test_health_score_uses_snapshot_battery_soh_not_row(h, monkeypatch):
    """R5 guard: snapshot SoH 75 / row SoH 92 → deduction 'Battery health 75%' appears.

    Mutant R5 (_compute_health_score receives row SoH 92 instead of snapshot 75):
    SoH 92 is above the 80% threshold so no battery deduction appears — caught.
    """
    r = _Resource(
        vehicle=dict(VEHICLE_ROW_HIGH_SOH),
        snapshot=_SnapshotTable(item=SNAPSHOT_LOW_SOH),
    )
    monkeypatch.setattr(h, "_get_ddb", lambda: r)
    resp = h.handler(_event(), None)
    body = json.loads(resp["body"])
    deductions = body["healthScoreBreakdown"]["deductions"]
    reasons = [d["reason"] for d in deductions]
    assert any("Battery health 75%" in r for r in reasons), (
        f"Expected 'Battery health 75%' deduction (from snapshot SoH, not row SoH 92); "
        f"deductions: {reasons!r} (R5 guard)"
    )


# ---------------------------------------------------------------------------
# W3: snapshot error on BEV → healthScore null with breakdown entry (R5e guard)
# ---------------------------------------------------------------------------

def test_bev_snapshot_error_withholds_health_score(h, monkeypatch):
    """W3/R5e/N1 guard: snapshot error on BEV → healthScore null, breakdown null.

    C1 (cycle 2): iOS declares HealthScoreBreakdown.score: Int (non-optional) and
    Deduction.amount: Int (non-optional).  A present breakdown with null fields causes
    DecodingError.valueNotFound for the whole /context response.  Fix: null the
    entire breakdown and surface the reason via healthScoreUnavailable.  (N2 guard
    checks the reason key in test_bev_snapshot_error_has_unavailable_key.)
    """
    bev_vehicle = dict(VEHICLE_ROW, fuelType="bev")
    resp, _ = _call(h, monkeypatch, vehicle=bev_vehicle, snapshot_raises=True)
    body = json.loads(resp["body"])
    assert body["healthScore"] is None, (
        f"BEV healthScore must be null on snapshot error, got {body['healthScore']!r} (W3/R5e guard)"
    )
    # N1 guard: breakdown must be null (iOS Int is non-optional — null inside fails decoding)
    assert body["healthScoreBreakdown"] is None, (
        f"BEV healthScoreBreakdown must be null on error (N1 guard), "
        f"got {body['healthScoreBreakdown']!r}"
    )


def test_non_bev_snapshot_error_still_has_health_score(h, monkeypatch):
    """W3: snapshot error on a non-EV does NOT withhold the score (battery factor absent is ok)."""
    gasoline_vehicle = dict(VEHICLE_ROW, fuelType="gasoline")
    resp, _ = _call(h, monkeypatch, vehicle=gasoline_vehicle, snapshot_raises=True)
    body = json.loads(resp["body"])
    # Score should still be an int (possibly reduced for connectivity/DTCs etc.)
    assert isinstance(body["healthScore"], int), (
        f"Non-EV healthScore must be an int even on snapshot error, got {body['healthScore']!r}"
    )


# ---------------------------------------------------------------------------
# R6–R8: values and snapshot blocks equal pinned item
# ---------------------------------------------------------------------------

def _dec(v):
    if isinstance(v, Decimal):
        return int(v) if v % 1 == 0 else float(v)
    if isinstance(v, dict):
        return {k: _dec(vv) for k, vv in v.items()}
    if isinstance(v, list):
        return [_dec(i) for i in v]
    return v


def test_values_block_equals_pinned_item(h, monkeypatch):
    """R6 / R7 guard: every sub-field in the values block matches the pinned item."""
    resp, _ = _call(h, monkeypatch, snapshot=PINNED_ITEM)
    body = json.loads(resp["body"])
    v = body["values"]
    assert v is not None, "values block must be present"
    for key in ("odometer", "batteryCharge", "batteryHealth", "batteryAge", "tires", "service", "faults"):
        assert v[key] == _dec(PINNED_ITEM[key]), (
            f"values.{key} mismatch (R6/R7 guard): got {v[key]!r}, expected {_dec(PINNED_ITEM[key])!r}"
        )


def test_snapshot_block_equals_pinned_item(h, monkeypatch):
    """R8 guard: snapshot block carries runDate/dataThrough/computedAt from pinned item."""
    resp, _ = _call(h, monkeypatch, snapshot=PINNED_ITEM)
    body = json.loads(resp["body"])
    s = body["snapshot"]
    assert s is not None, "snapshot block must be present"
    assert s["runDate"]     == PINNED_ITEM["runDate"]
    assert s["dataThrough"] == PINNED_ITEM["dataThrough"]
    assert s["computedAt"]  == PINNED_ITEM["computedAt"]


# ---------------------------------------------------------------------------
# R11: snapshot read error is logged
# ---------------------------------------------------------------------------

def test_error_is_logged_on_snapshot_exception(h, monkeypatch, caplog):
    """R11 guard: a DDB exception on the snapshot read must be logged at WARNING."""
    import logging
    resp, _ = _call(h, monkeypatch, snapshot_raises=True)
    with caplog.at_level(logging.WARNING):
        _call(h, monkeypatch, snapshot_raises=True)
    assert any("snapshot read failed" in rec.message for rec in caplog.records), (
        "snapshot read error must be logged at WARNING; no matching record (R11 guard)"
    )


# ---------------------------------------------------------------------------
# R4 (key value guard): GetItem value must be vehicleId, not VIN
# ---------------------------------------------------------------------------

def test_snapshot_key_value_is_vehicle_id_not_vin(h, monkeypatch):
    """R4 guard: GetItem key value must be vehicleId 'VEH-001', not VIN 'VIN-TEST-001'."""
    r = _make_resource_strict(snapshot=PINNED_ITEM, expected_vehicle_id=VEHICLE_ID)
    monkeypatch.setattr(h, "_get_ddb", lambda: r)
    resp = h.handler(_event(), None)
    body = json.loads(resp["body"])
    assert body["snapshotStatus"] == "ok", (
        f"strict fake must find item via vehicleId VALUE, got snapshotStatus={body['snapshotStatus']!r} (R4 guard)"
    )
    assert body["vehicle"]["odometer"] == 21268, (
        f"odometer must be snapshot value 21268, not row value 39000 (R4 guard)"
    )


# ---------------------------------------------------------------------------
# Non-electric fuelLevel rule (decisions.md 2026-10-24 ~01:25Z, AVX S3)
# ---------------------------------------------------------------------------

def test_non_electric_car_fuel_level_unchanged(h, monkeypatch):
    """Non-EV: snapshot batteryCharge must NOT overwrite fuelLevel."""
    gasoline_vehicle = dict(VEHICLE_ROW, fuelType="gasoline", fuelLevel=58.0)
    resp, _ = _call(h, monkeypatch, vehicle=gasoline_vehicle, snapshot=SNAPSHOT_ITEM)
    body = json.loads(resp["body"])
    assert body["vehicle"]["fuelLevel"] == 58.0, (
        f"non-EV fuelLevel must stay as row value 58.0, got {body['vehicle']['fuelLevel']!r}"
    )


def test_electric_car_fuel_level_becomes_battery_charge(h, monkeypatch):
    """BEV: snapshot batteryCharge fills fuelLevel."""
    bev_vehicle = dict(VEHICLE_ROW, fuelType="bev", fuelLevel=55.0)
    resp, _ = _call(h, monkeypatch, vehicle=bev_vehicle, snapshot=SNAPSHOT_ITEM)
    body = json.loads(resp["body"])
    # SNAPSHOT_ITEM.batteryCharge.value = 72
    assert body["vehicle"]["fuelLevel"] == 72, (
        f"BEV fuelLevel must be snapshot batteryCharge 72, got {body['vehicle']['fuelLevel']!r}"
    )


@pytest.mark.parametrize("fuel_type", ["bev", "electric", "ev", "EV", " Electric "])
def test_electric_car_fuel_level_becomes_battery_charge_synonyms(h, monkeypatch, fuel_type):
    """BEV3vc guard: every EV synonym triggers the snapshot charge → fuelLevel overlay.

    BEV3vc mutant changes the overlay call site to ``== "bev"``, so any synonym
    (electric, EV, ' Electric ') would keep the row's stale fuelLevel (55.0) instead
    of the snapshot charge (72).  This test catches that drift.
    """
    ev_vehicle = dict(VEHICLE_ROW, fuelType=fuel_type, fuelLevel=55.0)
    resp, _ = _call(h, monkeypatch, vehicle=ev_vehicle, snapshot=SNAPSHOT_ITEM)
    body = json.loads(resp["body"])
    assert body["vehicle"]["fuelLevel"] == 72, (
        f"fuelType={fuel_type!r}: fuelLevel must be snapshot batteryCharge 72 "
        f"(BEV3vc guard — overlay call site must use _is_bev, not == 'bev'), "
        f"got {body['vehicle']['fuelLevel']!r}"
    )


# ---------------------------------------------------------------------------
# C1 (cycle 2): iOS decode contract over every /context body shape (N1/N2 guards)
# ---------------------------------------------------------------------------

def test_bev_snapshot_error_has_unavailable_key(h, monkeypatch):
    """N2 guard: BEV snapshot error must carry healthScoreUnavailable with a reason.

    iOS ignores unknown keys (Codable), so this field is safe to add.
    """
    bev_vehicle = dict(VEHICLE_ROW, fuelType="bev")
    resp, _ = _call(h, monkeypatch, vehicle=bev_vehicle, snapshot_raises=True)
    body = json.loads(resp["body"])
    unavail = body.get("healthScoreUnavailable")
    assert unavail is not None, (
        "healthScoreUnavailable key must be present on BEV snapshot error (N2 guard)"
    )
    reason = unavail.get("reason", "")
    assert "Battery health unavailable" in reason, (
        f"healthScoreUnavailable.reason must mention 'Battery health unavailable', got {reason!r}"
    )


def test_decode_contract_all_context_bodies(h, monkeypatch):
    """N1 guard: every /context body satisfies the iOS Codable decode contract.

    iOS types (Models.swift:660-672):
        healthScore: Int?                           — null ok, float not ok
        healthScoreBreakdown: HealthScoreBreakdown? — null ok
        HealthScoreBreakdown.score: Int             — NON-optional (null → DecodingError)
        HealthScoreBreakdown.Deduction.amount: Int  — NON-optional

    Exercises all five body shapes the suite creates and fails if any would be
    un-decodable on iOS.
    """
    def _check(body, scenario):
        hs = body.get("healthScore")
        assert hs is None or isinstance(hs, int), (
            f"{scenario}: healthScore must be int or null, got {hs!r} ({type(hs).__name__})"
        )
        bd = body.get("healthScoreBreakdown")
        if bd is not None:
            score = bd.get("score")
            assert isinstance(score, int), (
                f"{scenario}: breakdown.score must be int (non-optional), got {score!r}"
            )
            for i, d in enumerate(bd.get("deductions", [])):
                amt = d.get("amount")
                assert isinstance(amt, int), (
                    f"{scenario}: deductions[{i}].amount must be int (non-optional), got {amt!r}"
                )

    bev = dict(VEHICLE_ROW, fuelType="bev")
    gas = dict(VEHICLE_ROW, fuelType="gasoline")

    # BEV: snapshot ok — score+breakdown present with int fields
    _check(json.loads(_call(h, monkeypatch, vehicle=bev, snapshot=SNAPSHOT_ITEM)[0]["body"]),
           "BEV snapshot-ok")
    # BEV: no snapshot item (status none) — battery factor included, score int
    _check(json.loads(_call(h, monkeypatch, vehicle=bev, snapshot=None)[0]["body"]),
           "BEV snapshot-none")
    # BEV: snapshot error — C1: breakdown must be null, not a dict with null fields
    _check(json.loads(_call(h, monkeypatch, vehicle=bev, snapshot_raises=True)[0]["body"]),
           "BEV snapshot-error")
    # non-BEV: snapshot ok
    _check(json.loads(_call(h, monkeypatch, vehicle=gas, snapshot=SNAPSHOT_ITEM)[0]["body"]),
           "gasoline snapshot-ok")
    # non-BEV: no snapshot item (status none) — row values unchanged, no battery factor
    _check(json.loads(_call(h, monkeypatch, vehicle=gas, snapshot=None)[0]["body"]),
           "gasoline snapshot-none")
    # non-BEV: snapshot error — battery factor absent but score is still an int
    _check(json.loads(_call(h, monkeypatch, vehicle=gas, snapshot_raises=True)[0]["body"]),
           "gasoline snapshot-error")


# ---------------------------------------------------------------------------
# R5g (cycle 2): BEV with status "none" keeps an int health score
# ---------------------------------------------------------------------------

def test_bev_snapshot_none_keeps_int_score(h, monkeypatch):
    """R5g guard: BEV with no snapshot item (status 'none') → healthScore is an int.

    Mutant R5g changes the condition to fire on 'none' as well as 'error',
    making the score null when the snapshot table simply has no row for the car.
    This test pins that 'none' keeps a real (int) score.
    """
    bev_vehicle = dict(VEHICLE_ROW, fuelType="bev")
    resp, _ = _call(h, monkeypatch, vehicle=bev_vehicle, snapshot=None)
    body = json.loads(resp["body"])
    assert body["snapshotStatus"] == "none"
    assert isinstance(body["healthScore"], int), (
        f"BEV with no snapshot must have int healthScore (R5g guard), got {body['healthScore']!r}"
    )


# ---------------------------------------------------------------------------
# W1 / M2-2vc (cycle 2): floor is pinned against round for .7 fractions
# ---------------------------------------------------------------------------

def test_fractional_odometer_floors_not_rounds_7(h, monkeypatch):
    """M2-2vc guard: Decimal('21268.7') → 21268 (floor), not 21269 (round).

    M2-2vc replaces math.floor with round; floor(21268.7)=21268 but round(21268.7)=21269.
    The earlier fixture ('21268.4') cannot distinguish them (both give 21268).
    """
    item_07 = dict(PINNED_ITEM, odometer={"value": Decimal("21268.7"), "asOf": "2026-10-01", "basis": "history"})
    resp, _ = _call(h, monkeypatch, snapshot=item_07)
    body = json.loads(resp["body"])
    veh = body["vehicle"]
    assert veh["odometer"] == 21268, (
        f"floor(21268.7) must give 21268, not {veh['odometer']!r} "
        f"(M2-2vc guard — round gives 21269)"
    )
    assert isinstance(veh["odometer"], int)


# ---------------------------------------------------------------------------
# S1 (cycle 2): snapshot read error leaves non-EV fuelLevel as the row value
# ---------------------------------------------------------------------------

def test_snapshot_error_non_bev_fuel_level_unchanged(h, monkeypatch):
    """S1 guard: snapshot read error on a non-electric car must NOT null fuelLevel.

    The row already carries a valid fuel reading; the snapshot has no fuel gauge
    for ICE/hybrid cars, so a read failure provides no substitute and fuelLevel
    must stay as the row value.  Odometer and batterySoh are still nulled.
    """
    gasoline_vehicle = dict(VEHICLE_ROW, fuelType="gasoline", fuelLevel=45.5)
    resp, _ = _call(h, monkeypatch, vehicle=gasoline_vehicle, snapshot_raises=True)
    body = json.loads(resp["body"])
    assert body["snapshotStatus"] == "error"
    assert body["vehicle"]["fuelLevel"] == 45.5, (
        f"non-EV fuelLevel must stay as row value 45.5 on snapshot error (S1 guard), "
        f"got {body['vehicle']['fuelLevel']!r}"
    )
    assert body["vehicle"]["odometer"] is None, "odometer still null on error"
    assert body["vehicle"]["batterySoh"] is None, "batterySoh still null on error"


# ---------------------------------------------------------------------------
# S2 (cycle 2): non-number odometer value → null fields, not a 500
# ---------------------------------------------------------------------------

def test_floor_odometer_non_number_returns_null(h, monkeypatch):
    """S2 guard: a non-number odometer.value gives null fields, not a TypeError 500.

    _snapshot_scalar passes non-Decimal values through unchanged; a string then
    reaches _floor_odometer and raises TypeError, making the whole route 500.
    Fix: guard non-numeric types in _floor_odometer and return None.
    """
    string_snap = dict(PINNED_ITEM,
                       odometer={"value": "21268.4km", "asOf": "2026-10-01", "basis": "history"})
    resp, _ = _call(h, monkeypatch, snapshot=string_snap)
    assert resp["statusCode"] == 200, (
        f"non-number odometer must give 200, not a 500 (S2 guard), got {resp['statusCode']}"
    )
    body = json.loads(resp["body"])
    assert body["vehicle"]["odometer"] is None, (
        f"non-number odometer must be null (S2 guard), got {body['vehicle']['odometer']!r}"
    )
    assert body["vehicle"]["mileage"] is None, "mileage must also be null"


# ---------------------------------------------------------------------------
# S2b/S2vc guard: bool and string scalar values → null (not passed through)
# ---------------------------------------------------------------------------

def test_bool_odometer_returns_null_vc(h, monkeypatch):
    """S2vcb guard: snapshot odometer.value=True must produce null, not floor(True)==1.

    S2vcb drops isinstance(val, bool) from _floor_odometer; without the guard
    math.floor(True)==1 and odometer/mileage arrive as 1 instead of null.
    """
    bool_snap = dict(SNAPSHOT_ITEM,
                     odometer={"value": True, "asOf": "2026-10-01", "basis": "history"})
    resp, _ = _call(h, monkeypatch, snapshot=bool_snap)
    body = json.loads(resp["body"])
    assert body["vehicle"]["odometer"] is None, (
        f"odometer True must be null (S2vcb guard), got {body['vehicle']['odometer']!r}"
    )
    assert body["vehicle"]["mileage"] is None, "mileage must also be null"


def test_string_battery_charge_returns_null_fuel_level(h, monkeypatch):
    """S2vc guard: batteryCharge.value='72%' must produce null fuelLevel, not pass the string.

    A string in fuelLevel causes Swift JSONDecoder typeMismatch for the entire
    /context response — currentDriver/currentVehicle both become nil on iOS.
    _snapshot_scalar must reject non-int/float/Decimal values.
    """
    bev = dict(VEHICLE_ROW, fuelType="bev", fuelLevel=55.0)
    str_charge_snap = dict(SNAPSHOT_ITEM, batteryCharge={"value": "72%", "asOf": "2026-10-01", "unit": "pct"})
    resp, _ = _call(h, monkeypatch, vehicle=bev, snapshot=str_charge_snap)
    body = json.loads(resp["body"])
    assert resp["statusCode"] == 200, f"expected 200, got {resp['statusCode']}"
    assert body["vehicle"]["fuelLevel"] is None, (
        f"batteryCharge.value='72%' must give null fuelLevel (S2vc guard — string breaks iOS decode), "
        f"got {body['vehicle']['fuelLevel']!r}"
    )


def test_bool_battery_values_return_null_vc(h, monkeypatch):
    """SCALBvc guard: bool batteryCharge/batteryHealth must produce null fuelLevel/batterySoh.

    {"value": True} in batteryCharge or batteryHealth reaches fuelLevel/batterySoh
    (typed Double? on iOS); a bool there raises Swift JSONDecoder typeMismatch.
    _snapshot_scalar must reject bool values (isinstance(val, bool) check).
    """
    bool_snap = dict(SNAPSHOT_ITEM,
                     batteryCharge={"value": True, "asOf": "2026-10-01", "unit": "pct"},
                     batteryHealth={"value": True, "asOf": "2026-10-01", "basis": "history"})
    # VEHICLE_ROW is already bev; fuelLevel=60.0 would become True without the guard
    resp, _ = _call(h, monkeypatch, snapshot=bool_snap)
    body = json.loads(resp["body"])
    assert resp["statusCode"] == 200, f"expected 200, got {resp['statusCode']}"
    assert body["vehicle"]["fuelLevel"] is None, (
        f"batteryCharge.value=True must give null fuelLevel (SCALBvc guard — bool breaks iOS decode), "
        f"got {body['vehicle']['fuelLevel']!r}"
    )
    assert body["vehicle"]["batterySoh"] is None, (
        f"batteryHealth.value=True must give null batterySoh (SCALBvc guard — bool breaks iOS decode), "
        f"got {body['vehicle']['batterySoh']!r}"
    )


# ---------------------------------------------------------------------------
# W2(a) guard: live test calls fail() not skip() when vehicle doesn't resolve.
# Unit test (no LIVE_TESTS, no AWS) showing the handler path that triggers fail.
# ---------------------------------------------------------------------------

def test_handler_returns_404_when_vehicle_not_found(h, monkeypatch):
    """W2(a) guard: handler returns 404 when the vehicle row is absent.

    The live test now FAILs on a 404 (not skips).
    Confirms the fail-not-skip path by monkeypatching _get_ddb to return no vehicle.
    """
    class _EmptyVehicleTable:
        def get_item(self, **kw):
            return {}  # no Item key → vehicle not found

    class _EmptyRes:
        def Table(self, name):
            return _EmptyVehicleTable()

    monkeypatch.setattr(h, "_get_ddb", lambda: _EmptyRes())
    resp = h.handler(_event("VEH-DOES-NOT-EXIST"), None)
    assert resp["statusCode"] == 404, (
        f"expected 404 when vehicle not found, got {resp['statusCode']}; "
        "the live test now calls pytest.fail() on this outcome (W2a guard)"
    )


# ---------------------------------------------------------------------------
# LIVE_TESTS=1 (skipped by default; runs in 7.4/7.5 after snapshot table exists)
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


def test_live_read_only_guard_unit_vc(_live_read_only_guard):
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
def test_live_snapshot_read_via_vehicle_context(_live_read_only_guard):
    """LIVE: call api-vehicle-context handler → verify snapshotStatus ok, int odometer, decode contract.

    W3 (cycle 2) fixes:
      - @pytest.mark.live_aws required (conftest blocks real calls without it)
      - CMS_STAGE must be set (fails rather than defaulting to cms-dev-*)
      - asserts snapshotStatus == 'ok' (LV1: fails on wrong table → snapshotStatus 'error')
      - compares odometer against floor(GetItem value) to pin the precedence rule
      - runs C1 decode-contract check on the live body
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
    _spec = _ilu.spec_from_file_location("vc_live", _handler_path)
    _mod = _ilu.module_from_spec(_spec)
    _spec.loader.exec_module(_mod)
    _mod._load_live_connection_state = lambda vid: None

    vid = _os.environ.get("TEST_VEHICLE_ID", "MRDN0000000000015")
    event = _event(vid)
    resp = _mod.handler(event, None)
    if resp["statusCode"] not in (200, 404, 503):
        _pytest.fail(f"unexpected status {resp['statusCode']}: {resp['body'][:200]}")
    if resp["statusCode"] == 404:
        _pytest.fail(f"vehicle {vid!r} not found in this stage — live check failed (W2a guard)")
    elif resp["statusCode"] != 200:
        _pytest.skip(f"vehicle {vid!r}: status {resp['statusCode']} — DTC or transient error")

    body = _json.loads(resp["body"])

    # LV1 negative control: wrong table → snapshotStatus 'error' → this assertion fails
    assert body["snapshotStatus"] == "ok", (
        f"snapshotStatus must be 'ok' (table may not exist or have no item for {vid!r}); "
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
    _raw = _ddb.Table(_table_name).get_item(Key={"vehicleId": vid}).get("Item", {})
    _raw_odo_field = _raw.get("odometer", {})
    _raw_odo_val = _raw_odo_field.get("value") if isinstance(_raw_odo_field, dict) else None
    if _raw_odo_val is not None and isinstance(_raw_odo_val, (int, float, _Dec)):
        expected_odo = int(_math.floor(float(_raw_odo_val)))
        assert odo == expected_odo, (
            f"odometer {odo!r} must equal floor({_raw_odo_val!r}) = {expected_odo}"
        )

    # C1 decode-contract check on the live body
    hs = body.get("healthScore")
    assert hs is None or isinstance(hs, int), f"live healthScore must be int or null, got {hs!r}"
    bd = body.get("healthScoreBreakdown")
    if bd is not None:
        assert isinstance(bd.get("score"), int), (
            f"live breakdown.score must be int (non-optional), got {bd.get('score')!r}"
        )
        for i, d in enumerate(bd.get("deductions", [])):
            amt = d.get("amount")
            assert isinstance(amt, int), (
                f"live deductions[{i}].amount must be int, got {amt!r}"
            )


# ---------------------------------------------------------------------------
# Pagination: _load_driver_for_vehicle must follow LastEvaluatedKey
# ---------------------------------------------------------------------------

class _MultiPageDriversTableVC:
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

    def query(self, **kw: Any) -> dict:
        return {"Items": []}

    def get_item(self, **kw: Any) -> dict:
        return {}


def test_load_driver_for_vehicle_follows_pagination(h, monkeypatch):
    """_load_driver_for_vehicle must follow LastEvaluatedKey; match is on page 3."""
    driver_row = {
        "driverId": "DRV-MRDN-0015",
        "assignedVehicleId": VEHICLE_ID,
        "safetyScore": Decimal("85"),
        "status": "active",
    }
    t = _MultiPageDriversTableVC(driver_row)

    class _R:
        def Table(self, name: str) -> Any:  # noqa: N802
            if "driver" in name:
                return t
            return _ScanTable()

    monkeypatch.setattr(h, "_get_ddb", lambda: _R())
    result = h._load_driver_for_vehicle(VEHICLE_ID)
    assert result is not None, "expected driver on page 3 — pagination not followed"
    assert result["driverId"] == "DRV-MRDN-0015"
    assert t._calls == 3, f"expected exactly 3 scan calls, got {t._calls}"


class _InfiniteDriversTableVC:
    """Always returns LastEvaluatedKey — never finds a match. Safety-cap at 100 calls."""

    def __init__(self):
        self.calls = 0

    def scan(self, **kw: Any) -> dict:
        self.calls += 1
        if self.calls >= 100:
            raise AssertionError(f"scan called {self.calls} times — page cap not effective")
        return {"Items": [], "LastEvaluatedKey": {"pk": str(self.calls)}}

    def query(self, **kw: Any) -> dict:
        return {"Items": []}

    def get_item(self, **kw: Any) -> dict:
        return {}


def test_load_driver_page_cap_returns_none(h, monkeypatch, caplog):
    """CAPvc guard: _load_driver_for_vehicle stops at _MAX_SCAN_PAGES, logs a warning, returns None.

    CAPvc replaces range(_MAX_SCAN_PAGES) with an unbounded loop; the fake raises at 100
    to surface that, and the scan-count assertion fails before that point.
    """
    import logging
    t = _InfiniteDriversTableVC()

    class _R:
        def Table(self, name: str) -> Any:  # noqa: N802
            if "driver" in name:
                return t
            return _ScanTable()

    monkeypatch.setattr(h, "_get_ddb", lambda: _R())
    with caplog.at_level(logging.WARNING):
        result = h._load_driver_for_vehicle(VEHICLE_ID)
    assert result is None, "expected None after page cap"
    assert t.calls == h._MAX_SCAN_PAGES, (
        f"expected exactly {h._MAX_SCAN_PAGES} scans (page cap), got {t.calls}"
    )
    assert "exceeded page cap" in caplog.text, "expected cap warning in logs"
