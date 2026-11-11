"""C2 (review cycle 1): api-vehicle-live-state must return odometer and fuelLevel
from Redis only — no ddb_row fallback.

When Redis has no signal and the meta hash has no reading, odometer and fuelLevel
must be null.  Returning the vehicles row's seeded/undated value would make iOS
Home disagree with /drivers/me (which carries the snapshot or record value per
decisions.md 2026-10-24 ~01:25Z).  Location keeps its ddb_row fallback.

Mutations guarded (L1): restoring `or ddb_row.get(key)` in the Redis-only path
returns the seeded row value (e.g. fuelLevel=72, odometer=39840) instead of
null — the test catches this by asserting null when Redis is empty.
"""
from __future__ import annotations

import json
import os
import sys
import time
from typing import Any, Dict
from unittest.mock import MagicMock

import pytest

_LAMBDA_ROOT = os.path.join(os.path.dirname(__file__), "..")
if _LAMBDA_ROOT not in sys.path:
    sys.path.insert(0, _LAMBDA_ROOT)

os.environ.setdefault("REDIS_ENDPOINT", "cache.example.internal")
os.environ.setdefault("REDIS_PORT", "6379")
os.environ.setdefault("CMS_VEHICLES_TABLE", "vsa-test-vehicles")

import handler  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

VID = "VEH-0015"
SEEDED_FUEL  = "72"       # row value that must NOT appear when Redis is empty
SEEDED_ODO   = "39840"
REDIS_FUEL   = "65.3"     # live signal value
REDIS_ODO    = "21270.0"  # live signal value
REDIS_META_ODO = "21268"  # meta hash value (no catalog; falls through to _num)


def _make_event(vehicle_id: str = VID) -> Dict[str, Any]:
    return {"pathParameters": {"vehicleId": vehicle_id}}


def _ddb_mock_with_seeded_values(fuel: str = SEEDED_FUEL, odo: str = SEEDED_ODO) -> MagicMock:
    """DDB returns a row with seeded fuelLevel and odometer (the pre-snapshot values)."""
    table_mock = MagicMock()
    table_mock.get_item.return_value = {"Item": {
        "vehicleId": VID,
        "connectionStatus": "connected",
        "lastSeenAt": str(int(time.time()) - 10),
        "fuelLevel": fuel,
        "odometer": odo,
        "vin": "1HGBH41JXMN000024",
    }}
    ddb_mock = MagicMock()
    ddb_mock.Table.return_value = table_mock
    return ddb_mock


def _redis_empty() -> MagicMock:
    """Redis returns empty meta and signals (no live reading)."""
    r = MagicMock()
    r.hgetall.return_value = {}
    r.ping.return_value = True
    return r


def _redis_with_signal(fuel: str, odo: str) -> MagicMock:
    """Redis returns a catalog-resolved signal for fuelLevel and odometer.

    Both carry a time in the timestamps hash, as Flink writes them: since
    issues/2026-11-06-live-state-stale-odometer/ an untimed reading is not returned.
    """
    r = MagicMock()
    SIG_ID_FUEL = "10"
    SIG_ID_ODO  = "20"
    now_ms = str(int(time.time() * 1000))

    def hgetall(key: str) -> Dict[str, str]:
        if key.endswith(":meta"):
            return {"lastConnectedAt": str(int(time.time()) - 5)}
        if key.endswith(":signals"):
            return {SIG_ID_FUEL: fuel, SIG_ID_ODO: odo}
        if key.endswith(":timestamps"):
            return {SIG_ID_FUEL: now_ms, SIG_ID_ODO: now_ms}
        if key == "signal_catalog:reverse":
            return {SIG_ID_FUEL: "fuelLevel|vss.FuelLevel||float", SIG_ID_ODO: "odometer|vss.Odometer||float"}
        return {}

    r.hgetall.side_effect = hgetall
    r.ping.return_value = True
    return r


class _NoSnapshotItem:
    """Snapshot table with no item for the car (a timed reading then wins)."""

    def get_item(self, **_kwargs):
        return {}


def _redis_meta_only(fuel: str, odo: str) -> MagicMock:
    """Redis returns readings in the meta hash only (no catalog / signals)."""
    r = MagicMock()

    def hgetall(key: str) -> Dict[str, str]:
        if key.endswith(":meta"):
            return {"lastConnectedAt": str(int(time.time()) - 5), "fuelLevel": fuel, "odometer": odo}
        return {}

    r.hgetall.side_effect = hgetall
    r.ping.return_value = True
    return r


# ---------------------------------------------------------------------------
# C2 / L1 guard: empty Redis → odometer and fuelLevel null
# ---------------------------------------------------------------------------

def test_empty_redis_gives_null_odometer_and_fuel_level(monkeypatch):
    """L1 guard: when Redis has no signal and meta has no reading, odometer and
    fuelLevel must be null — NOT the vehicles row's seeded values.

    Mutant L1: restoring `or ddb_row.get(key)` returns SEEDED_FUEL / SEEDED_ODO.
    This test catches it by asserting null.
    """
    monkeypatch.setattr(handler, "_get_redis", lambda: _redis_empty())
    monkeypatch.setattr(handler, "_vehicles_table", _ddb_mock_with_seeded_values().Table("x"))

    resp = handler.handler(_make_event(), None)
    assert resp["statusCode"] == 200
    body = json.loads(resp["body"])

    assert body["odometer"] is None, (
        f"odometer must be null when Redis is empty, got {body['odometer']!r} "
        f"(row seeded value {SEEDED_ODO!r} must NOT appear — L1 guard)"
    )
    assert body["fuelLevel"] is None, (
        f"fuelLevel must be null when Redis is empty, got {body['fuelLevel']!r} "
        f"(row seeded value {SEEDED_FUEL!r} must NOT appear — L1 guard)"
    )


# ---------------------------------------------------------------------------
# Confirm live Redis signal still works (anti-regression)
# ---------------------------------------------------------------------------

def test_redis_signal_odometer_and_fuel_appear(monkeypatch):
    """When Redis has timed live signals, odometer and fuelLevel are populated from them."""
    monkeypatch.setattr(handler, "_get_redis", lambda: _redis_with_signal(REDIS_FUEL, REDIS_ODO))
    monkeypatch.setattr(handler, "_vehicles_table", _ddb_mock_with_seeded_values().Table("x"))
    monkeypatch.setattr(handler, "_snapshot_table", _NoSnapshotItem())

    resp = handler.handler(_make_event(), None)
    body = json.loads(resp["body"])

    assert body["fuelLevel"] == pytest.approx(float(REDIS_FUEL)), (
        f"fuelLevel must come from live Redis signal, got {body['fuelLevel']!r}"
    )
    assert body["odometer"] == pytest.approx(float(REDIS_ODO)), (
        f"odometer must come from live Redis signal, got {body['odometer']!r}"
    )


def test_redis_meta_odometer_and_fuel_are_not_used(monkeypatch):
    """Meta-hash readings are not returned (changed 2026-11-06).

    This test used to assert the opposite. Flink writes no odometer or charge to the meta
    hash (only connection fields, lastSeenAt, tripId, driverId, source), and a meta value
    has no time of its own, so it can't be shown newer than the history value.
    issues/2026-11-06-live-state-stale-odometer/.
    """
    monkeypatch.setattr(handler, "_get_redis", lambda: _redis_meta_only(REDIS_FUEL, REDIS_META_ODO))
    monkeypatch.setattr(handler, "_vehicles_table", _ddb_mock_with_seeded_values().Table("x"))
    monkeypatch.setattr(handler, "_snapshot_table", _NoSnapshotItem())

    resp = handler.handler(_make_event(), None)
    body = json.loads(resp["body"])

    assert body["fuelLevel"] is None
    assert body["odometer"] is None


# ---------------------------------------------------------------------------
# Location is unaffected (keeps ddb_row fallback)
# ---------------------------------------------------------------------------

def test_location_still_falls_back_to_ddb_row(monkeypatch):
    """Location (latitude/longitude) must still fall back to ddb_row.lastLatitude/
    lastLongitude when Redis has no signal — only odometer/fuelLevel drop the fallback.
    """
    monkeypatch.setattr(handler, "_get_redis", lambda: _redis_empty())
    ddb = MagicMock()
    table_mock = MagicMock()
    table_mock.get_item.return_value = {"Item": {
        "vehicleId": VID,
        "connectionStatus": "connected",
        "lastSeenAt": str(int(time.time()) - 10),
        "lastLatitude": "47.6062",
        "lastLongitude": "-122.3321",
        "fuelLevel": SEEDED_FUEL,
        "odometer": SEEDED_ODO,
    }}
    ddb.Table.return_value = table_mock
    monkeypatch.setattr(handler, "_vehicles_table", table_mock)

    resp = handler.handler(_make_event(), None)
    body = json.loads(resp["body"])

    # Location must still appear from DDB
    assert body["latitude"] == pytest.approx(47.6062), \
        f"latitude must fall back to ddb_row.lastLatitude, got {body['latitude']!r}"
    assert body["longitude"] == pytest.approx(-122.3321), \
        f"longitude must fall back to ddb_row.lastLongitude, got {body['longitude']!r}"

    # But odometer and fuelLevel must still be null (no Redis)
    assert body["odometer"] is None
    assert body["fuelLevel"] is None
