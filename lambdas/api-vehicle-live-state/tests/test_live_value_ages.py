"""Every value live-state reads from Redis has an age rule.

issues/2026-11-07-live-state-stale-values/ (PO prompt 316). Flink re-sets the signals
hash's TTL on every message, so a car that keeps sending doors and position keeps every
older reading alive: on 2026-11-07 VEH-MRDN-0015's Redis `speed` was 0 from 2026-10-20
and iOS Home showed it as the car's speed. The rules:

- current-state values (speed, heading, engineTemp, batteryVoltage, batteryLevel): the
  newest timed reading among the field's aliases, if it is at most
  CURRENT_READING_MAX_AGE_SECONDS (120 s, this route's connected window) old; else null.
  Never the meta hash or the vehicles row.
- position: the newest timed fix on the globe, if it is at most
  LAST_KNOWN_MAX_AGE_SECONDS (7 days, Flink's last-known-state TTL) old; else the
  vehicles row's lastLatitude/lastLongitude, labelled `locationSource: "ddb"`.
- every value: no time of its own, a time more than FUTURE_TOLERANCE_SECONDS ahead, or a
  non-finite/non-numeric value means no reading. The response is still a 200 with
  strictly valid JSON.

Mutations this file is meant to catch: issues/2026-11-07-live-state-stale-values/mutate_ages.py.
"""
from __future__ import annotations

import json
import os
import sys
from decimal import Decimal
from typing import Any, Dict, Optional
from unittest.mock import MagicMock

import pytest

_LAMBDA_ROOT = os.path.join(os.path.dirname(__file__), "..")
if _LAMBDA_ROOT not in sys.path:
    sys.path.insert(0, _LAMBDA_ROOT)

os.environ.setdefault("REDIS_ENDPOINT", "cache.example.internal")
os.environ.setdefault("REDIS_PORT", "6379")
os.environ.setdefault("CMS_VEHICLES_TABLE", "vsa-test-vehicles")

import handler  # noqa: E402

VID = "VEH-MRDN-0015"
NOW_MS = 1_794_018_000_000            # 2026-11-07T02:20:00Z, the probe's time
STALE_MS = 1_792_499_990_607          # VEH-MRDN-0015's MQTT-era readings, 2026-10-20
S = 1000                              # ms per second
LIMIT_MS = 120 * S                    # the current-state limit this file pins
DAY_MS = 86_400 * S

# Every alias the handler reads for these fields, with distinct signal ids.
NAMES = [
    "speed", "lastSpeed", "VehicleSpeed",
    "engineTemp", "engine_temp", "coolantTemp", "catalyst_temp",
    "batteryVoltage", "battery_voltage",
    "heading", "Heading", "gps_heading",
    "batteryLevel", "battery_level",
    "lat", "latitude", "Latitude", "gps_lat",
    "lng", "longitude", "Longitude", "lon", "gps_lng", "gps_lon",
    "odometer", "powertrainOdometer",
]
SID = {name: str(500 + i) for i, name in enumerate(NAMES)}
CATALOG = {sid: f"{name}|vss.{name}||float" for name, sid in SID.items()}
CURRENT_FIELDS = ["speed", "heading", "engineTemp", "batteryVoltage", "batteryLevel"]


class FakeRedis:
    def __init__(self, readings: Dict[str, tuple], meta: Optional[Dict[str, str]] = None,
                 timestamps_error: bool = False, catalog: Optional[Dict[str, str]] = None):
        # readings: name -> (raw value, raw ts or None)
        self.signals = {SID[n]: str(v) for n, (v, _t) in readings.items()}
        self.timestamps = {SID[n]: str(t) for n, (_v, t) in readings.items() if t is not None}
        self.meta = meta if meta is not None else {"lastConnectedAt": str(NOW_MS - 5 * S),
                                                    "lastSeenAt": str(NOW_MS)}
        self.timestamps_error = timestamps_error
        self.catalog = CATALOG if catalog is None else catalog

    def ping(self):
        return True

    def hgetall(self, key: str) -> Dict[str, str]:
        if key == f"vehicle:{VID}:signals":
            return dict(self.signals)
        if key == f"vehicle:{VID}:timestamps":
            if self.timestamps_error:
                raise ConnectionError("timestamps read failed")
            return dict(self.timestamps)
        if key == f"vehicle:{VID}:meta":
            return dict(self.meta)
        if key == "signal_catalog:reverse":
            return dict(self.catalog)
        return {}


def _vehicles_table(extra: Optional[Dict[str, Any]] = None) -> MagicMock:
    """A vehicles row carrying seeded, undated copies of every current-state value."""
    row = {
        "vehicleId": VID, "connectionStatus": "connected", "fuelType": "electric",
        "lastSeenAt": "2026-11-07T02:19:55+00:00", "vin": "MRDN0000000000015",
        "speed": Decimal("33"), "lastSpeed": Decimal("0"), "engineTemp": Decimal("180"),
        "batteryVoltage": Decimal("398.4"), "batteryLevel": Decimal("80"),
        "battery_level": Decimal("81"), "heading": Decimal("90"),
        "lastLatitude": Decimal("45.50415"), "lastLongitude": Decimal("-122.66191"),
    }
    row.update(extra or {})
    table = MagicMock()
    table.get_item.return_value = {"Item": row}
    return table


def _strict_json(text: str) -> Dict[str, Any]:
    def _refuse(token):
        raise AssertionError(f"non-JSON constant {token} in the response body")
    return json.loads(text, parse_constant=_refuse)


def _run(monkeypatch, redis_fake, vehicles_table=None, now_ms: int = NOW_MS) -> Dict[str, Any]:
    monkeypatch.setattr(handler, "_now", lambda: now_ms / 1000)
    monkeypatch.setattr(handler, "_get_redis", lambda: redis_fake)
    monkeypatch.setattr(handler, "_vehicles_table", vehicles_table or _vehicles_table())
    snapshot = MagicMock()
    snapshot.get_item.return_value = {}
    monkeypatch.setattr(handler, "_snapshot_table", snapshot)
    resp = handler.handler({"pathParameters": {"vehicleId": VID}}, None)
    assert resp["statusCode"] == 200
    return _strict_json(resp["body"])


# ---------------------------------------------------------------------------
# The reported case
# ---------------------------------------------------------------------------

def test_reported_case_17_day_old_current_values_are_null_and_fresh_position_stays(monkeypatch):
    """VEH-MRDN-0015, 2026-11-07 02:15Z: speed 0, engineTemp 191.1, batteryVoltage 13.9,
    heading 76 all from 2026-10-20; lat/lng 22 s old. Home must not show the 17-day-old speed."""
    body = _run(monkeypatch, FakeRedis({
        "speed": (0.0, STALE_MS), "engineTemp": (191.1, STALE_MS),
        "batteryVoltage": (13.9, STALE_MS), "heading": (76.0, STALE_MS),
        "lat": (45.49, NOW_MS - 22 * S), "lng": (-122.66, NOW_MS - 22 * S),
    }))
    for field in CURRENT_FIELDS:
        assert body[field] is None, field
    assert body["latitude"] == pytest.approx(45.49)
    assert body["longitude"] == pytest.approx(-122.66)
    assert body["locationSource"] == "redis"


def test_fresh_current_values_are_served(monkeypatch):
    t = NOW_MS - 15 * S
    body = _run(monkeypatch, FakeRedis({
        "speed": (104.6, t), "engineTemp": (192.0, t), "batteryVoltage": (13.8, t),
        "heading": (270.0, t), "batteryLevel": (77.0, t),
    }))
    assert (body["speed"], body["engineTemp"], body["batteryVoltage"], body["heading"],
            body["batteryLevel"]) == (104.6, 192.0, 13.8, 270.0, 77.0)


# ---------------------------------------------------------------------------
# The current-state limit: 120 s
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("field", CURRENT_FIELDS)
def test_current_value_at_exactly_the_limit_is_served(monkeypatch, field):
    body = _run(monkeypatch, FakeRedis({field: (12.5, NOW_MS - LIMIT_MS)}))
    assert body[field] == 12.5


@pytest.mark.parametrize("field", CURRENT_FIELDS)
def test_current_value_one_millisecond_past_the_limit_is_null(monkeypatch, field):
    body = _run(monkeypatch, FakeRedis({field: (12.5, NOW_MS - LIMIT_MS - 1)}))
    assert body[field] is None


def test_limit_is_the_connected_window(monkeypatch):
    """The age rule follows this route's own "connected" window, not a second number."""
    monkeypatch.setattr(handler, "CURRENT_READING_MAX_AGE_SECONDS", 300)
    body = _run(monkeypatch, FakeRedis({"speed": (40.0, NOW_MS - 200 * S)}))
    assert body["speed"] == 40.0, "the handler must read the constant, not a literal"
    assert handler.LIVE_WINDOW_SECONDS == 120


# ---------------------------------------------------------------------------
# Which reading: the newest timed alias
# ---------------------------------------------------------------------------

def test_stale_first_alias_does_not_shadow_a_fresh_later_alias(monkeypatch):
    body = _run(monkeypatch, FakeRedis({
        "speed": (0.0, STALE_MS), "VehicleSpeed": (88.0, NOW_MS - 10 * S),
        "engineTemp": (191.1, STALE_MS), "catalyst_temp": (205.0, NOW_MS - 10 * S),
    }))
    assert body["speed"] == 88.0
    assert body["engineTemp"] == 205.0


def test_newest_alias_wins_when_both_are_fresh(monkeypatch):
    body = _run(monkeypatch, FakeRedis({
        "speed": (50.0, NOW_MS - 30 * S), "lastSpeed": (61.0, NOW_MS - 5 * S),
        "heading": (10.0, NOW_MS - 5 * S), "gps_heading": (20.0, NOW_MS - 30 * S),
    }))
    assert body["speed"] == 61.0
    assert body["heading"] == 10.0


def test_an_untimed_alias_does_not_beat_a_timed_one(monkeypatch):
    body = _run(monkeypatch, FakeRedis({"speed": (99.0, None), "VehicleSpeed": (44.0, NOW_MS - S)}))
    assert body["speed"] == 44.0


# ---------------------------------------------------------------------------
# What is never served
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("field", CURRENT_FIELDS)
def test_reading_without_its_own_time_is_null(monkeypatch, field):
    # The meta hash's lastSeenAt is fresh (Flink refreshes it on every message); it must
    # not lend its time to an untimed reading.
    body = _run(monkeypatch, FakeRedis({field: (12.5, None)}))
    assert body[field] is None


def test_meta_hash_values_are_not_served(monkeypatch):
    meta = {"lastConnectedAt": str(NOW_MS - 5 * S), "lastSeenAt": str(NOW_MS),
            "speed": "55", "engineTemp": "190", "batteryVoltage": "13.7", "heading": "45",
            "batteryLevel": "66", "battery_level": "67"}
    body = _run(monkeypatch, FakeRedis({}, meta=meta))
    for field in CURRENT_FIELDS:
        assert body[field] is None, field


def test_vehicles_row_values_are_not_served_for_current_state(monkeypatch):
    """The row's seeded speed 33 / lastSpeed 0 / engineTemp 180 / 398.4 V / 80% / 90 deg
    are undated. iOS falls back to /drivers/me's row values itself; live-state must not."""
    body = _run(monkeypatch, FakeRedis({"speed": (0.0, STALE_MS)}))
    for field in CURRENT_FIELDS:
        assert body[field] is None, field


@pytest.mark.parametrize("raw_ts", ["", "abc", "-5", "1.5e12", "1794017990000.0", " "])
def test_unusable_time_is_null(monkeypatch, raw_ts):
    body = _run(monkeypatch, FakeRedis({"speed": (40.0, raw_ts)}))
    assert body["speed"] is None


@pytest.mark.parametrize("raw", ["nan", "NaN", "inf", "-inf", "fast", ""])
def test_non_finite_or_non_numeric_value_is_null_and_the_body_is_strict_json(monkeypatch, raw):
    body = _run(monkeypatch, FakeRedis({f: (raw, NOW_MS - S) for f in CURRENT_FIELDS}))
    for field in CURRENT_FIELDS:
        assert body[field] is None, field


# ---------------------------------------------------------------------------
# Times in the future
# ---------------------------------------------------------------------------

def test_reading_more_than_the_tolerance_ahead_is_null(monkeypatch):
    body = _run(monkeypatch, FakeRedis({"speed": (40.0, NOW_MS + 60 * S + 1)}))
    assert body["speed"] is None


def test_reading_within_the_tolerance_ahead_is_served(monkeypatch):
    body = _run(monkeypatch, FakeRedis({"speed": (40.0, NOW_MS + 60 * S)}))
    assert body["speed"] == 40.0


def test_future_stamped_odometer_does_not_beat_history(monkeypatch):
    """Before this, a reading stamped tomorrow would beat the snapshot for as long as it
    stayed in Redis. No snapshot item here, so any usable time would win."""
    body = _run(monkeypatch, FakeRedis({"odometer": (99999.0, NOW_MS + DAY_MS)}))
    assert body["odometer"] is None
    ok = _run(monkeypatch, FakeRedis({"odometer": (4010.0, NOW_MS - S)}))
    assert ok["odometer"] == 4010.0


def test_future_stamped_fix_falls_back_to_the_row(monkeypatch):
    body = _run(monkeypatch, FakeRedis({"lat": (47.6, NOW_MS + DAY_MS), "lng": (-122.3, NOW_MS + DAY_MS)}))
    assert body["latitude"] == pytest.approx(45.50415)
    assert body["locationSource"] == "ddb"


# ---------------------------------------------------------------------------
# Position: a last-known value, 7 days
# ---------------------------------------------------------------------------

def test_fix_hours_old_is_still_the_cars_position(monkeypatch):
    """A live-tier car parked after its last drive (MRDAZ01S1SR035767: 5 h; its row says
    another place). The fix stays, as on CMS's vehicle page."""
    t = NOW_MS - 18_159 * S
    body = _run(monkeypatch, FakeRedis({"lat": (33.616664, t), "lng": (-84.510934, t)}))
    assert (body["latitude"], body["longitude"]) == (pytest.approx(33.616664), pytest.approx(-84.510934))
    assert body["locationSource"] == "redis"


def test_fix_at_exactly_seven_days_is_served(monkeypatch):
    t = NOW_MS - 7 * DAY_MS
    body = _run(monkeypatch, FakeRedis({"lat": (47.6, t), "lng": (-122.3, t)}))
    assert body["latitude"] == pytest.approx(47.6)
    assert body["locationSource"] == "redis"


def test_fix_older_than_seven_days_falls_back_to_the_row(monkeypatch):
    t = NOW_MS - 7 * DAY_MS - 1
    body = _run(monkeypatch, FakeRedis({"lat": (47.6, t), "lng": (-122.3, t)}))
    assert (body["latitude"], body["longitude"]) == (pytest.approx(45.50415), pytest.approx(-122.66191))
    assert body["locationSource"] == "ddb"


def test_one_stale_axis_makes_the_pair_ddb(monkeypatch):
    """F7.1 per-axis provenance holds under the age rule: a fresh Redis latitude with an
    8-day-old longitude is not a Redis-backed pair."""
    body = _run(monkeypatch, FakeRedis({"lat": (47.6, NOW_MS - S), "lng": (-122.3, NOW_MS - 8 * DAY_MS)}))
    assert body["latitude"] == pytest.approx(47.6)
    assert body["longitude"] == pytest.approx(-122.66191)
    assert body["locationSource"] == "ddb"


def test_untimed_fix_falls_back_to_the_row(monkeypatch):
    body = _run(monkeypatch, FakeRedis({"lat": (47.6, None), "lng": (-122.3, None)}))
    assert body["latitude"] == pytest.approx(45.50415)
    assert body["locationSource"] == "ddb"


def test_newest_fix_alias_wins(monkeypatch):
    body = _run(monkeypatch, FakeRedis({
        "lat": (10.0, NOW_MS - 8 * DAY_MS), "latitude": (47.6, NOW_MS - S),
        "lng": (-122.3, NOW_MS - 2 * S), "longitude": (-100.0, NOW_MS - 3 * S),
    }))
    assert body["latitude"] == pytest.approx(47.6)
    assert body["longitude"] == pytest.approx(-122.3)
    assert body["locationSource"] == "redis"


@pytest.mark.parametrize("lat,lng", [("nan", "-122.3"), ("47.6", "inf"), ("90.0001", "-122.3"),
                                     ("47.6", "-180.0001"), ("-91", "0")])
def test_non_finite_or_off_the_globe_fix_falls_back_to_the_row(monkeypatch, lat, lng):
    body = _run(monkeypatch, FakeRedis({"lat": (lat, NOW_MS - S), "lng": (lng, NOW_MS - S)}))
    assert body["locationSource"] == "ddb"
    assert body["latitude"] is not None and body["longitude"] is not None


def test_fix_on_the_edge_of_the_globe_is_served(monkeypatch):
    body = _run(monkeypatch, FakeRedis({"lat": ("-90", NOW_MS - S), "lng": ("180", NOW_MS - S)}))
    assert (body["latitude"], body["longitude"]) == (-90.0, 180.0)
    assert body["locationSource"] == "redis"


def test_no_fix_and_no_row_location_gives_null_position(monkeypatch):
    table = _vehicles_table({"lastLatitude": None, "lastLongitude": None})
    body = _run(monkeypatch, FakeRedis({"lat": (47.6, NOW_MS - 8 * DAY_MS)}), vehicles_table=table)
    assert body["latitude"] is None and body["longitude"] is None
    assert body["locationSource"] is None


@pytest.mark.parametrize("lat,lng", [("nan", "-122.3"), ("47.6", "inf"), ("91", "-122.3"), ("47.6", "-181")])
def test_unusable_row_fallback_axis_is_null_and_the_body_is_strict_json(monkeypatch, lat, lng):
    """Review cycle 1 S3: the row fallback gets the same finite/globe check as a Redis fix.
    A string attribute "nan" on the row would otherwise put NaN in the body."""
    table = _vehicles_table({"lastLatitude": lat, "lastLongitude": lng})
    body = _run(monkeypatch, FakeRedis({}), vehicles_table=table)
    bad = "latitude" if lat in ("nan", "91") else "longitude"
    good = "longitude" if bad == "latitude" else "latitude"
    assert body[bad] is None
    assert body[good] is not None
    assert body["locationSource"] == "ddb"


def test_row_fallback_on_the_edge_of_the_globe_is_served(monkeypatch):
    """Review cycle 2 S2: the row bound is inclusive, like the Redis one (A21)."""
    table = _vehicles_table({"lastLatitude": Decimal("-90"), "lastLongitude": Decimal("180")})
    body = _run(monkeypatch, FakeRedis({}), vehicles_table=table)
    assert (body["latitude"], body["longitude"]) == (-90.0, 180.0)
    assert body["locationSource"] == "ddb"


def test_catalog_read_failure_is_a_200_with_nulls_and_the_row_position(monkeypatch):
    class CatalogDown(FakeRedis):
        def hgetall(self, key):
            if key == "signal_catalog:reverse":
                raise ConnectionError("catalog read failed")
            return super().hgetall(key)
    t = NOW_MS - S
    body = _run(monkeypatch, CatalogDown({"speed": (40.0, t), "lat": (47.6, t), "lng": (-122.3, t)}))
    assert body["speed"] is None
    assert body["latitude"] == pytest.approx(45.50415)
    assert body["locationSource"] == "ddb"


# ---------------------------------------------------------------------------
# Never a failed response
# ---------------------------------------------------------------------------

def test_garbage_everywhere_is_a_200_with_nulls(monkeypatch):
    readings = {name: (raw, ts) for name, raw, ts in [
        ("speed", "nan", "abc"), ("VehicleSpeed", "12", "-1"), ("engineTemp", "x", str(NOW_MS)),
        ("batteryVoltage", "13.9", "99999999999999999999"), ("heading", "inf", "1e3"),
        ("batteryLevel", "", str(NOW_MS)), ("lat", "nan", str(NOW_MS)), ("lng", "1e400", str(NOW_MS)),
        ("odometer", "862", "1792499990607.5"),
    ]}
    body = _run(monkeypatch, FakeRedis(readings))
    for field in CURRENT_FIELDS + ["odometer"]:
        assert body[field] is None, field
    assert body["locationSource"] == "ddb"


def test_timestamps_read_failure_withholds_every_redis_value(monkeypatch):
    t = NOW_MS - S
    body = _run(monkeypatch, FakeRedis({"speed": (40.0, t), "lat": (47.6, t), "lng": (-122.3, t)},
                                       timestamps_error=True))
    assert body["speed"] is None
    assert body["latitude"] == pytest.approx(45.50415)
    assert body["locationSource"] == "ddb"


def test_malformed_catalog_entries_are_skipped(monkeypatch):
    catalog = dict(CATALOG)
    catalog[SID["speed"]] = "|no-name|"
    catalog[SID["VehicleSpeed"]] = ""
    body = _run(monkeypatch, FakeRedis({"speed": (40.0, NOW_MS - S), "VehicleSpeed": (41.0, NOW_MS - S),
                                        "lastSpeed": (42.0, NOW_MS - S)}, catalog=catalog))
    assert body["speed"] == 42.0
