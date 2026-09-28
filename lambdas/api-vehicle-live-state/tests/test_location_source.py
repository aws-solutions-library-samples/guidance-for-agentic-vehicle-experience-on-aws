"""Tests for F6.1: locationSource field in the live-state response.

The load-bearing property: `_live_location` must report which source answered.
  "redis" — BOTH lat AND lon signal aliases in the live signals hash supplied the coordinate.
  "ddb"   — either axis fell through to the DDB fallback (lastLatitude/lastLongitude).
  None    — neither source had a coordinate.

This is the field the escalate gate relies on. `redisReachable` (r is not None) is
client connectivity, not provenance, and must NOT be used as the gate predicate.

F7.1 (Critical C7-1): Track provenance PER AXIS.
The old implementation latched on the first non-None resolution (latitude resolves first).
A Redis latitude + DDB longitude was labelled "redis" — a blended coordinate ~540 km off.
The fix: "redis" only when BOTH axes came from Redis; otherwise "ddb".

Two routes reproduced from Cycle 7:
  Route A: signal_catalog:reverse entry with no name part (silently skipped at :330)
  Route B: non-numeric signal value on one axis (float() → ValueError → DDB fallback)

Mutation guards are defined inline at each critical assertion:
  (a) restore the single latching nonlocal (first-resolution wins) →
      axis-disagreement tests MUST fail.
  (b) omit locationSource from the response body →
      every assertion on "locationSource" must fail.
"""

from __future__ import annotations

import json
import os
import sys
import time
from typing import Any, Dict
from unittest.mock import MagicMock, patch

import pytest

# Allow direct import from the lambda root (CVX convention: no package install).
_LAMBDA_ROOT = os.path.join(os.path.dirname(__file__), "..")
if _LAMBDA_ROOT not in sys.path:
    sys.path.insert(0, _LAMBDA_ROOT)

os.environ.setdefault("REDIS_ENDPOINT", "cache.example.internal")
os.environ.setdefault("REDIS_PORT", "6379")
os.environ.setdefault("CMS_VEHICLES_TABLE", "vsa-test-vehicles")

import handler  # noqa: E402 — must follow the env-var setup above


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_event(vehicle_id: str = "VEH-0001") -> Dict[str, Any]:
    return {"pathParameters": {"vehicleId": vehicle_id}}


def _make_redis_mock(signals: Dict[str, str] | None = None, catalog: Dict[str, str] | None = None) -> MagicMock:
    """Return a mock Redis client whose hgetall returns controlled data."""
    r = MagicMock()

    def hgetall_side_effect(key: str) -> Dict[str, str]:
        if key.startswith("vehicle:") and key.endswith(":meta"):
            return {
                "lastConnectedAt": str(int(time.time()) - 5),
                "connectionStatus": "connected",
            }
        if key.startswith("vehicle:") and key.endswith(":signals"):
            return signals or {}
        if key == "signal_catalog:reverse":
            return catalog or {}
        return {}

    r.hgetall.side_effect = hgetall_side_effect
    r.ping.return_value = True
    return r


def _make_ddb_mock(last_lat: str | None = "36.1699", last_lon: str | None = "-115.1398", vin: str | None = "1HGBH41JXMN000024") -> MagicMock:
    """Return a mock DDB table that yields a vehicle row with optional lastLatitude/lastLongitude."""
    row: Dict[str, Any] = {
        "vehicleId": "VEH-0001",
        "connectionStatus": "connected",
        "lastSeenAt": str(int(time.time()) - 3600),
    }
    if last_lat is not None:
        row["lastLatitude"] = last_lat
    if last_lon is not None:
        row["lastLongitude"] = last_lon
    if vin is not None:
        row["vin"] = vin

    table_mock = MagicMock()
    table_mock.get_item.return_value = {"Item": row}
    ddb_mock = MagicMock()
    ddb_mock.Table.return_value = table_mock
    return ddb_mock


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestLocationSourceField:
    """F6.1: locationSource is present in every live-state response and is accurate."""

    def test_redis_coordinate_reports_redis(self):
        """When a signal alias in the signals hash supplies lat/lon, locationSource='redis'.

        This is the happy path: fresh telemetry is flowing.

        Mutation guard (a): make _live_location report 'ddb' on the Redis path →
        this assertion fails (expected 'redis', got 'ddb').
        """
        signals = {"101": "47.6062", "102": "-122.3321"}
        catalog = {
            "101": "lat|Vehicle.CurrentLocation.Latitude|deg|DOUBLE",
            "102": "lng|Vehicle.CurrentLocation.Longitude|deg|DOUBLE",
        }
        redis_mock = _make_redis_mock(signals=signals, catalog=catalog)
        ddb_mock = _make_ddb_mock(last_lat="36.1699", last_lon="-115.1398")

        with patch.object(handler, "_get_redis", return_value=redis_mock), \
             patch.object(handler, "_ddb_resource", ddb_mock), \
             patch.object(handler, "_vehicles_table", ddb_mock.Table("x")):
            resp = handler.handler(_make_event(), None)

        body = json.loads(resp["body"])
        assert body["locationSource"] == "redis", (
            f"Expected locationSource='redis' when coordinate came from Redis signals; "
            f"got {body['locationSource']!r}. "
            "Mutation guard (a): report 'ddb' on Redis path → this assertion fails."
        )
        assert body["latitude"] == 47.6062
        assert body["longitude"] == -122.3321

    def test_ddb_coordinate_reports_ddb(self):
        """When Redis is up but has no location signals, _live_location falls through to DDB.
        locationSource must be 'ddb' — not 'redis'.

        This is C6-1 Route 1: Redis reachable but the signals hash has no lat/lon aliases.
        The old escalate gate (redisReachable) accepted this; it must not.

        Mutation guard (a): make _live_location report 'redis' on the DDB path →
        this assertion fails (expected 'ddb', got 'redis').
        """
        # Signals hash present but contains only non-location signals
        signals = {"50": "85.2"}   # fuelLevel-like, not a location alias
        catalog = {"50": "fuelLevel|Vehicle.Powertrain.FuelSystem.Fuel|pct|FLOAT"}
        redis_mock = _make_redis_mock(signals=signals, catalog=catalog)
        # DDB row has lastLatitude/lastLongitude
        ddb_mock = _make_ddb_mock(last_lat="36.1699", last_lon="-115.1398")

        with patch.object(handler, "_get_redis", return_value=redis_mock), \
             patch.object(handler, "_ddb_resource", ddb_mock), \
             patch.object(handler, "_vehicles_table", ddb_mock.Table("x")):
            resp = handler.handler(_make_event(), None)

        body = json.loads(resp["body"])
        assert body["locationSource"] == "ddb", (
            f"Expected locationSource='ddb' when coordinate came from DDB fallback; "
            f"got {body['locationSource']!r}. "
            "Mutation guard (a): report 'redis' on the DDB path → this assertion fails."
        )
        # Coordinate is still surfaced (DDB fallback works)
        assert body["latitude"] == 36.1699

    def test_empty_catalog_reports_ddb(self):
        """Redis up, signal_catalog:reverse empty → DDB fallback → locationSource='ddb'.

        This is C6-1 Route 2: the catalog is unseeded so no signal alias matches.
        """
        signals = {"101": "47.6", "102": "-122.3"}  # signals exist, but catalog is empty
        redis_mock = _make_redis_mock(signals=signals, catalog={})  # empty catalog
        ddb_mock = _make_ddb_mock(last_lat="36.1699", last_lon="-115.1398")

        with patch.object(handler, "_get_redis", return_value=redis_mock), \
             patch.object(handler, "_ddb_resource", ddb_mock), \
             patch.object(handler, "_vehicles_table", ddb_mock.Table("x")):
            resp = handler.handler(_make_event(), None)

        body = json.loads(resp["body"])
        # The catalog is empty → no alias resolves → DDB fallback
        assert body["locationSource"] == "ddb", (
            f"Empty catalog means no signal alias resolves; expected locationSource='ddb', "
            f"got {body['locationSource']!r}."
        )

    def test_no_coordinate_anywhere_reports_none(self):
        """No Redis location and no DDB lastLatitude/lastLongitude → locationSource=None."""
        # Signals with no location, empty catalog
        redis_mock = _make_redis_mock(signals={}, catalog={})
        ddb_mock = _make_ddb_mock(last_lat=None, last_lon=None)

        with patch.object(handler, "_get_redis", return_value=redis_mock), \
             patch.object(handler, "_ddb_resource", ddb_mock), \
             patch.object(handler, "_vehicles_table", ddb_mock.Table("x")):
            resp = handler.handler(_make_event(), None)

        body = json.loads(resp["body"])
        assert body["locationSource"] is None, (
            f"Expected locationSource=None when no coordinate source found; "
            f"got {body['locationSource']!r}."
        )
        assert body["latitude"] is None
        assert body["longitude"] is None

    def test_redis_unreachable_ddb_fallback_reports_ddb(self):
        """When Redis is unreachable, DDB fallback supplies the coordinate → locationSource='ddb'."""
        ddb_mock = _make_ddb_mock(last_lat="36.1699", last_lon="-115.1398")

        with patch.object(handler, "_get_redis", return_value=None), \
             patch.object(handler, "_ddb_resource", ddb_mock), \
             patch.object(handler, "_vehicles_table", ddb_mock.Table("x")):
            resp = handler.handler(_make_event(), None)

        body = json.loads(resp["body"])
        assert body["locationSource"] == "ddb", (
            f"When Redis is unreachable and DDB has lat/lon, locationSource must be 'ddb'; "
            f"got {body['locationSource']!r}."
        )
        # redisReachable is False — confirming it's a separate field from locationSource
        assert body["redisReachable"] is False

    def test_location_source_is_additive_not_replacing_redis_reachable(self):
        """locationSource is ADDITIVE. redisReachable must still be in the response body.

        Mutation guard (b): omit locationSource from the response body →
        body.get('locationSource') returns None for everything, masking the defect.
        This test ensures the field is present (not just truthy), enforcing the additive contract.
        """
        redis_mock = _make_redis_mock(signals={}, catalog={})
        ddb_mock = _make_ddb_mock(last_lat="36.1699", last_lon="-115.1398")

        with patch.object(handler, "_get_redis", return_value=redis_mock), \
             patch.object(handler, "_ddb_resource", ddb_mock), \
             patch.object(handler, "_vehicles_table", ddb_mock.Table("x")):
            resp = handler.handler(_make_event(), None)

        body = json.loads(resp["body"])
        assert "locationSource" in body, (
            "locationSource must be present in the response body (additive field, F6.1). "
            "Mutation guard (b): omit it → this key check fails, exposing that the "
            "escalate gate would get None for every call."
        )
        # Existing fields must still be present (additive-only change)
        assert "redisReachable" in body, "redisReachable must still be in the body (existing field)"
        assert "latitude" in body, "latitude must still be in the body (existing field)"
        assert "longitude" in body, "longitude must still be in the body (existing field)"
        assert "connectionStatus" in body, "connectionStatus must still be in the body"


# ---------------------------------------------------------------------------
# F6.4: Consumer-side coordinate range — existing (0,0) guard gains a test
# ---------------------------------------------------------------------------
# The consumer tests already cover absent lat/lon. The round-trip tests in
# test_consumer_handler.py cover (0,0). These tests for the consumer's
# range-check are in the escalate suite because F6.4 is owned there; but the
# consumer's own range guard makes consumer tests authoritative too.
# See TestCoordinateRangeValidation in lambdas/api-escalate/tests/test_handler.py.



# ---------------------------------------------------------------------------
# F7.1: Per-axis provenance — blended coordinates must report "ddb"
# ---------------------------------------------------------------------------

class TestF71PerAxisProvenance:
    """F7.1: locationSource is 'redis' ONLY when BOTH axes came from Redis.

    Cycle 7 Critical C7-1: the old latch-on-first-resolution implementation labelled
    a Redis-latitude/DDB-longitude pair as 'redis', sending a truck ~540 km off.

    Mutation guard: restore the single latching `nonlocal` (first-resolution wins) →
    both axis-disagreement tests below MUST fail.
    """

    def test_redis_lat_ddb_lon_reports_ddb(self):
        """Route A (Cycle 7): lat comes from Redis signals, lon falls to DDB fallback.

        The catalog entry for longitude has no name part (empty first pipe-field),
        so the name lookup returns '' and the alias is silently skipped (:330).
        Result: lat='redis', lon='ddb' → pair must report 'ddb'.

        Mutation guard: restore first-resolution latch → locationSource='redis', this FAILS.
        """
        # Catalog entry for longitude has no name part — silently skipped
        signals = {"101": "47.6062", "102": "-122.3321"}
        catalog = {
            "101": "lat|Vehicle.CurrentLocation.Latitude|deg|DOUBLE",
            # No name part — parts[0] is '' → skipped at the `if parts and parts[0]` guard
            "102": "|Vehicle.CurrentLocation.Longitude|deg|DOUBLE",
        }
        redis_mock = _make_redis_mock(signals=signals, catalog=catalog)
        ddb_mock = _make_ddb_mock(last_lat="36.1699", last_lon="-115.1398")

        with patch.object(handler, "_get_redis", return_value=redis_mock), \
             patch.object(handler, "_ddb_resource", ddb_mock), \
             patch.object(handler, "_vehicles_table", ddb_mock.Table("x")):
            resp = handler.handler(_make_event(), None)

        body = json.loads(resp["body"])
        assert body["locationSource"] == "ddb", (
            f"lat from Redis + lon from DDB (no-name catalog entry) must report 'ddb'; "
            f"got {body['locationSource']!r}. "
            "Mutation guard: restore single latch → 'redis', this assertion fails."
        )
        # lat is Redis-fresh (47.6062), lon is DDB-fallback (-115.1398)
        assert body["latitude"] == 47.6062
        assert body["longitude"] == -115.1398

    def test_non_numeric_lon_signal_falls_to_ddb_reports_ddb(self):
        """Route B (Cycle 7): lat from Redis, non-numeric lon value → ValueError → DDB fallback.

        float() raises ValueError on a non-numeric lon value, so the alias is skipped
        and the DDB fallback supplies lon. lat='redis', lon='ddb' → pair must report 'ddb'.

        Mutation guard: restore first-resolution latch → locationSource='redis', this FAILS.
        """
        signals = {"101": "47.6062", "102": "not-a-number"}  # lon is non-numeric
        catalog = {
            "101": "lat|Vehicle.CurrentLocation.Latitude|deg|DOUBLE",
            "102": "lng|Vehicle.CurrentLocation.Longitude|deg|DOUBLE",
        }
        redis_mock = _make_redis_mock(signals=signals, catalog=catalog)
        ddb_mock = _make_ddb_mock(last_lat="36.1699", last_lon="-115.1398")

        with patch.object(handler, "_get_redis", return_value=redis_mock), \
             patch.object(handler, "_ddb_resource", ddb_mock), \
             patch.object(handler, "_vehicles_table", ddb_mock.Table("x")):
            resp = handler.handler(_make_event(), None)

        body = json.loads(resp["body"])
        assert body["locationSource"] == "ddb", (
            f"lat from Redis + non-numeric lon (ValueError → DDB) must report 'ddb'; "
            f"got {body['locationSource']!r}. "
            "Mutation guard: restore single latch → 'redis', this assertion fails."
        )
        assert body["latitude"] == 47.6062  # Redis value
        assert body["longitude"] == -115.1398  # DDB fallback

    def test_ddb_lat_redis_lon_reports_ddb(self):
        """Reverse axis disagreement: lat from DDB, lon from Redis → pair must report 'ddb'.

        This is the symmetric case. Not the attack vector Cycle 7 observed
        (which was Redis-lat/DDB-lon and latched 'redis') but the fix must
        handle both directions: the weaker source always wins.
        """
        # lat catalog entry has no name → falls to DDB; lon resolves from Redis
        signals = {"101": "47.6062", "102": "-122.3321"}
        catalog = {
            # No name part for lat
            "101": "|Vehicle.CurrentLocation.Latitude|deg|DOUBLE",
            "102": "lng|Vehicle.CurrentLocation.Longitude|deg|DOUBLE",
        }
        redis_mock = _make_redis_mock(signals=signals, catalog=catalog)
        ddb_mock = _make_ddb_mock(last_lat="36.1699", last_lon="-115.1398")

        with patch.object(handler, "_get_redis", return_value=redis_mock), \
             patch.object(handler, "_ddb_resource", ddb_mock), \
             patch.object(handler, "_vehicles_table", ddb_mock.Table("x")):
            resp = handler.handler(_make_event(), None)

        body = json.loads(resp["body"])
        assert body["locationSource"] == "ddb", (
            f"DDB-lat + Redis-lon must report 'ddb' (weaker-source-wins rule); "
            f"got {body['locationSource']!r}."
        )


# ---------------------------------------------------------------------------
# F7.2: vin field added to live-state response (additive)
# ---------------------------------------------------------------------------

class TestF72VinInLiveState:
    """F7.2: live-state response includes the vin from the vehicles row.

    This is additive — no existing field is renamed or removed.
    The field is used by the escalate handler to cross-check vehicleId↔VIN identity.
    """

    def test_vin_present_when_ddb_row_has_vin(self):
        """vin from the vehicles row is surfaced in the response."""
        redis_mock = _make_redis_mock(signals={}, catalog={})
        ddb_mock = _make_ddb_mock(vin="1HGBH41JXMN000024")

        with patch.object(handler, "_get_redis", return_value=redis_mock), \
             patch.object(handler, "_ddb_resource", ddb_mock), \
             patch.object(handler, "_vehicles_table", ddb_mock.Table("x")):
            resp = handler.handler(_make_event(), None)

        body = json.loads(resp["body"])
        assert "vin" in body, "vin must be present in the live-state response body (F7.2 additive field)"
        assert body["vin"] == "1HGBH41JXMN000024", (
            f"vin must equal the vehicles row vin; got {body['vin']!r}"
        )

    def test_vin_is_none_when_ddb_row_has_no_vin(self):
        """When the vehicles row has no vin field, vin is None in the response (version-skew)."""
        redis_mock = _make_redis_mock(signals={}, catalog={})
        ddb_mock = _make_ddb_mock(vin=None)  # no vin in row

        with patch.object(handler, "_get_redis", return_value=redis_mock), \
             patch.object(handler, "_ddb_resource", ddb_mock), \
             patch.object(handler, "_vehicles_table", ddb_mock.Table("x")):
            resp = handler.handler(_make_event(), None)

        body = json.loads(resp["body"])
        assert "vin" in body, "vin field must be present even when row has no vin (value will be None)"
        assert body["vin"] is None, (
            f"vin must be None when the vehicles row has no vin; got {body['vin']!r}"
        )

    def test_existing_fields_still_present(self):
        """Additive contract: existing fields are not removed or renamed."""
        redis_mock = _make_redis_mock(signals={}, catalog={})
        ddb_mock = _make_ddb_mock()

        with patch.object(handler, "_get_redis", return_value=redis_mock), \
             patch.object(handler, "_ddb_resource", ddb_mock), \
             patch.object(handler, "_vehicles_table", ddb_mock.Table("x")):
            resp = handler.handler(_make_event(), None)

        body = json.loads(resp["body"])
        for field in ("vehicleId", "connectionStatus", "locationSource", "redisReachable",
                      "latitude", "longitude", "lastSeenAgoSeconds"):
            assert field in body, f"Existing field '{field}' must still be present (additive-only change)"
