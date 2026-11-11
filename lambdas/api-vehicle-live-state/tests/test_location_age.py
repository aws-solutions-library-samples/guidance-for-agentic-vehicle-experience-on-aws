"""`locationAgeSeconds`: seconds since the position fix was stored, which /escalate gates dispatch on.

issues/2026-11-07-escalate-gate-uses-connection-age-not-fix-age/ (user decision 2026-11-07,
PO prompt 320). /escalate compared its 900 s limit with `lastSeenAgoSeconds`, the age of
meta lastConnectedAt/lastSeenAt. CMS rewrites lastConnectedAt on every FleetWise check-in,
which carries no position, so a car that checked in but sent no fix looked fresh with a fix
up to 7 days old. The rules this file pins:

- `locationAgeSeconds` is set only when `locationSource` is "redis" (both axes from timed
  Redis fixes); otherwise null. The vehicles-row fallback has no time of its own.
- It is the age of the OLDER of the two axes' own fix times (`vehicle:{id}:timestamps`, which
  CMS Flink stamps with its processing clock), so a pair whose axes came from
  different fixes is as old as its older half.
- Whole seconds, rounded up: a fix 900.001 s old reads 901, so the gate's `> 900` refuses it.
- A fix time within FUTURE_TOLERANCE_SECONDS ahead (served as usable) reads 0, never negative.
- It never comes from the connection fields.

Mutations this file is meant to catch: the issue folder's mutation record.
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

VID = "VEH-AGE-0001"
NOW_MS = 1_794_052_800_000            # 2026-11-07T12:00:00Z
S = 1000
DAY_MS = 86_400 * S

NAMES = ["lat", "latitude", "lng", "longitude"]
SID = {name: str(700 + i) for i, name in enumerate(NAMES)}
CATALOG = {sid: f"{name}|vss.{name}||float" for name, sid in SID.items()}


class FakeRedis:
    def __init__(self, readings: Dict[str, tuple], connected_ago_ms: int = 5 * S):
        # readings: name -> (raw value, raw ts or None)
        self.signals = {SID[n]: str(v) for n, (v, _t) in readings.items()}
        self.timestamps = {SID[n]: str(t) for n, (_v, t) in readings.items() if t is not None}
        self.meta = {"lastConnectedAt": str(NOW_MS - connected_ago_ms),
                     "lastSeenAt": str(NOW_MS - connected_ago_ms)}

    def ping(self):
        return True

    def hgetall(self, key: str) -> Dict[str, str]:
        return {
            f"vehicle:{VID}:signals": dict(self.signals),
            f"vehicle:{VID}:timestamps": dict(self.timestamps),
            f"vehicle:{VID}:meta": dict(self.meta),
            "signal_catalog:reverse": dict(CATALOG),
        }.get(key, {})


def _vehicles_table(extra: Optional[Dict[str, Any]] = None) -> MagicMock:
    row = {"vehicleId": VID, "connectionStatus": "connected", "vin": "MRDAZ0AGE00000001",
           "lastSeenAt": "2026-11-07T11:59:55+00:00",
           "lastLatitude": Decimal("45.50415"), "lastLongitude": Decimal("-122.66191")}
    row.update(extra or {})
    table = MagicMock()
    table.get_item.return_value = {"Item": row}
    return table


def _run(monkeypatch, redis_fake, vehicles_table=None) -> Dict[str, Any]:
    monkeypatch.setattr(handler, "_now", lambda: NOW_MS / 1000)
    monkeypatch.setattr(handler, "_get_redis", lambda: redis_fake)
    monkeypatch.setattr(handler, "_vehicles_table", vehicles_table or _vehicles_table())
    snapshot = MagicMock()
    snapshot.get_item.return_value = {}
    monkeypatch.setattr(handler, "_snapshot_table", snapshot)
    resp = handler.handler({"pathParameters": {"vehicleId": VID}}, None)
    assert resp["statusCode"] == 200
    return json.loads(resp["body"])


def _fix(age_ms_lat: int, age_ms_lng: Optional[int] = None) -> Dict[str, tuple]:
    age_ms_lng = age_ms_lat if age_ms_lng is None else age_ms_lng
    return {"lat": (47.61, NOW_MS - age_ms_lat), "lng": (-122.33, NOW_MS - age_ms_lng)}


# ---------------------------------------------------------------------------
# The reported case
# ---------------------------------------------------------------------------

def test_fresh_check_in_with_an_old_fix_reports_the_fix_age(monkeypatch):
    """A FleetWise check-in 5 s ago, the last fix 2 days old: the connection looks fresh,
    the fix does not, and the field says so."""
    body = _run(monkeypatch, FakeRedis(_fix(2 * DAY_MS), connected_ago_ms=5 * S))
    assert body["lastSeenAgoSeconds"] == 5
    assert body["locationSource"] == "redis"
    assert body["locationAgeSeconds"] == 2 * 86_400


def test_old_connection_field_does_not_age_a_fresh_fix(monkeypatch):
    """The other direction: a stale connection field does not leak into the fix age."""
    table = _vehicles_table({"lastSeenAt": "2026-11-07T09:00:00+00:00"})   # 3 h before NOW
    body = _run(monkeypatch, FakeRedis(_fix(12 * S), connected_ago_ms=3 * 3_600 * S), table)
    assert body["lastSeenAgoSeconds"] >= 3 * 3_600
    assert body["locationAgeSeconds"] == 12
    assert body["latitude"] == 47.61 and body["longitude"] == -122.33


# ---------------------------------------------------------------------------
# Which time, and how it is rounded
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("lat_ms,lng_ms", [(10 * S, 700 * S), (700 * S, 10 * S)])
def test_the_older_axis_decides(monkeypatch, lat_ms, lng_ms):
    body = _run(monkeypatch, FakeRedis(_fix(lat_ms, lng_ms)))
    assert body["locationAgeSeconds"] == 700


def test_age_is_the_chosen_aliass_own_time(monkeypatch):
    """Review S4 of issue 2026-11-07-live-state-stale-values: the newest `lat` is off the
    globe, so latitude comes from an older `latitude` fix; the pair is as old as that one."""
    body = _run(monkeypatch, FakeRedis({
        "lat": (999.0, NOW_MS - 1 * S), "latitude": (47.60, NOW_MS - 600 * S),
        "lng": (-122.33, NOW_MS - 10 * S),
    }))
    assert body["latitude"] == 47.60
    assert body["locationAgeSeconds"] == 600


@pytest.mark.parametrize("age_ms,want", [
    (900 * S, 900), (900 * S + 1, 901), (899 * S + 999, 900), (0, 0), (1, 1),
])
def test_whole_seconds_rounded_up(monkeypatch, age_ms, want):
    """900.001 s must not read 900, or the gate's `> 900` would let it through."""
    body = _run(monkeypatch, FakeRedis(_fix(age_ms)))
    assert body["locationAgeSeconds"] == want
    assert type(body["locationAgeSeconds"]) is int


def test_fix_within_the_future_tolerance_reads_zero(monkeypatch):
    """A time 30 s ahead (clock skew) is a usable fix; its age is 0, never negative."""
    body = _run(monkeypatch, FakeRedis(_fix(-30 * S)))
    assert body["locationSource"] == "redis"
    assert body["locationAgeSeconds"] == 0


def test_a_six_day_old_fix_is_served_with_its_age(monkeypatch):
    """Position stays a last-known value for 7 days; the age is what tells /escalate no."""
    body = _run(monkeypatch, FakeRedis(_fix(6 * DAY_MS)))
    assert body["locationSource"] == "redis"
    assert body["locationAgeSeconds"] == 6 * 86_400


# ---------------------------------------------------------------------------
# Null unless the pair came from Redis
# ---------------------------------------------------------------------------

def test_row_fallback_has_no_age(monkeypatch):
    body = _run(monkeypatch, FakeRedis({}))
    assert body["locationSource"] == "ddb"
    assert body["latitude"] == 45.50415
    assert body["locationAgeSeconds"] is None


def test_one_axis_from_the_row_has_no_age(monkeypatch):
    """A fresh Redis latitude with a row longitude is "ddb", and gets no age."""
    body = _run(monkeypatch, FakeRedis({"lat": (47.61, NOW_MS - 3 * S)}))
    assert body["locationSource"] == "ddb"
    assert body["locationAgeSeconds"] is None


def test_untimed_fix_has_no_age(monkeypatch):
    body = _run(monkeypatch, FakeRedis({"lat": (47.61, None), "lng": (-122.33, None)}))
    assert body["locationSource"] == "ddb"
    assert body["locationAgeSeconds"] is None


def test_fix_older_than_seven_days_has_no_age(monkeypatch):
    body = _run(monkeypatch, FakeRedis(_fix(7 * DAY_MS + 1)))
    assert body["locationSource"] == "ddb"
    assert body["locationAgeSeconds"] is None


def test_no_position_at_all_has_no_age(monkeypatch):
    table = _vehicles_table({"lastLatitude": None, "lastLongitude": None})
    body = _run(monkeypatch, FakeRedis({}), table)
    assert body["locationSource"] is None
    assert body["locationAgeSeconds"] is None


def test_redis_unreachable_has_no_age(monkeypatch):
    body = _run(monkeypatch, None)
    assert body["locationSource"] == "ddb"
    assert body["locationAgeSeconds"] is None
