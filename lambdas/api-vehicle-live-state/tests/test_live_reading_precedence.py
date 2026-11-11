"""Odometer and charge from live-state follow the one precedence rule.

issues/2026-11-06-live-state-stale-odometer/. CMS spec 2026-10-22-vehicle-data-consistency
§ D2: "live or feed reading if newer than the history value, else the history value".
CMS applies it in modules/cms_ui/source/handlers/main_api/vehicle_values.py
(`_apply_precedence`, `_live_odometer_ts`). iOS Home shows live-state's odometer and
charge first and falls back to /drivers/me (the snapshot) on null, so live-state must
return a Redis reading only when it would win under that rule:

- the reading's own time is the signal's entry in `vehicle:{id}:timestamps`
  (epoch ms, written by Flink beside the value);
- it wins when that time is after the end of the snapshot field's `asOf` day (UTC);
- with no snapshot item, or a field without `asOf`, a timed reading wins;
- a snapshot read error, an unparseable `asOf`, or a reading with no time gives null;
- among a value's signals the newest reading wins (ties: list order), using CMS's lists:
  odometer `odometer, powertrainOdometer, odo`; an electric car's charge any
  state-of-charge signal, `fuelLevel` only when none has a reading; a fuel car's
  `fuelLevel` only;
- charge is compared with `batteryCharge` only for an electric car (CMS W3 rule);
  without the vehicles row the powertrain is unknown and charge is withheld.

Mutations this file is meant to catch: issues/2026-11-06-live-state-stale-odometer/mutate_live_state.py
lists them; every one must make a test here fail.
"""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timezone
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

# Signal ids as the catalog maps them (values are arbitrary but distinct).
SID = {
    "odometer": "101",
    "powertrainOdometer": "102",
    "odo": "103",
    "fuelLevel": "201",
    "ev_soc": "202",
    "soc": "203",
    "powertrainEVStateOfCharge": "204",
    "lat": "301",
    "lng": "302",
}
CATALOG = {sid: f"{name}|vss.{name}||float" for name, sid in SID.items()}


def _ms(iso: str) -> int:
    return int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp() * 1000)


def _end_of_day_ms(day: str) -> int:
    """Last millisecond of a YYYY-MM-DD day in UTC."""
    start = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    return int(start.timestamp() * 1000) + 86_400_000 - 1


class FakeRedis:
    """Signals, timestamps, meta and catalog hashes for one vehicle."""

    def __init__(self, readings: Dict[str, tuple], meta: Optional[Dict[str, str]] = None):
        # readings: name -> (value, ts_ms or None)
        self.signals = {SID[n]: str(v) for n, (v, _ts) in readings.items()}
        self.timestamps = {SID[n]: str(ts) for n, (_v, ts) in readings.items() if ts is not None}
        # As Flink writes meta: a fresh lastSeenAt on every message. A stale signal on a car
        # that keeps sending doors and position sits beside it, so an untimed reading must not
        # borrow this time (CMS does; review cycle 1 W1, mutant M26).
        now = int(time.time())
        self.meta = meta if meta is not None else {"lastConnectedAt": str(now - 5),
                                                    "lastSeenAt": str(now * 1000)}

    def ping(self):
        return True

    def hgetall(self, key: str) -> Dict[str, str]:
        if key == f"vehicle:{VID}:signals":
            return dict(self.signals)
        if key == f"vehicle:{VID}:timestamps":
            return dict(self.timestamps)
        if key == f"vehicle:{VID}:meta":
            return dict(self.meta)
        if key == "signal_catalog:reverse":
            return dict(CATALOG)
        return {}


class FakeSnapshotTable:
    """GetItem-only stand-in for cms-{stage}-storage-vehicle-snapshot."""

    def __init__(self, item: Optional[Dict[str, Any]] = None, error: Optional[Exception] = None):
        self.item = item
        self.error = error
        self.calls = []

    def get_item(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return {"Item": self.item} if self.item is not None else {}


def _vehicles_row(fuel_type: str = "electric", powertrain: Optional[str] = "electric") -> MagicMock:
    row = {
        "vehicleId": VID,
        "connectionStatus": "connected",
        "lastSeenAt": str(int(time.time()) - 10),
        "fuelType": fuel_type,
        "odometer": Decimal("39840"),   # seeded, undated: must never appear
        "fuelLevel": Decimal("72"),
        "vin": "MRDN0000000000015",
    }
    if powertrain is not None:
        row["startingFacts"] = {"powertrain": powertrain}
    table = MagicMock()
    table.get_item.return_value = {"Item": row}
    return table


def _snapshot(odo_asof: Optional[str] = "2026-11-05", charge_asof: Optional[str] = "2026-11-05",
              odo_value: Any = Decimal("3951.9183812812907"),
              charge_value: Any = Decimal("73.9561176014586")) -> Dict[str, Any]:
    item: Dict[str, Any] = {"vehicleId": VID, "runDate": "2026-11-06"}
    item["odometer"] = {"value": odo_value, "asOf": odo_asof, "basis": "history"}
    item["batteryCharge"] = {"value": charge_value, "asOf": charge_asof, "basis": "history"}
    return item


def _run(monkeypatch, redis_fake, snapshot_table, vehicles_table=None) -> Dict[str, Any]:
    monkeypatch.setattr(handler, "_get_redis", lambda: redis_fake)
    monkeypatch.setattr(handler, "_vehicles_table", vehicles_table or _vehicles_row())
    monkeypatch.setattr(handler, "_snapshot_table", snapshot_table)
    resp = handler.handler({"pathParameters": {"vehicleId": VID}}, None)
    assert resp["statusCode"] == 200
    return json.loads(resp["body"])


STALE = _ms("2026-10-20T12:39:50.607Z")   # the live VEH-MRDN-0015 readings, 1792499990607
FRESH = _ms("2026-11-06T18:14:21.804Z")   # today, after the snapshot's asOf day
NOW_S = _ms("2026-11-06T18:20:00Z") / 1000


@pytest.fixture(autouse=True)
def _frozen_clock(monkeypatch):
    """These cases are dated 2026-11-06. Position has a 7-day age rule since
    issues/2026-11-07-live-state-stale-values/, so on the real clock the fixed FRESH fix
    would stop counting on 2026-11-13 and the reported case would fail for that reason."""
    monkeypatch.setattr(handler, "_now", lambda: NOW_S)


# ---------------------------------------------------------------------------
# The reported case
# ---------------------------------------------------------------------------

def test_reported_case_stale_redis_odometer_and_charge_are_not_returned(monkeypatch):
    """VEH-MRDN-0015 on 2026-11-06: Redis odometer 862 / powertrainOdometer 50002 / fuel 74,
    all from 2026-10-20; snapshot 3951.9 mi and 73.96% as of 2026-11-05. iOS Home must
    fall through to /drivers/me, so both come back null.
    """
    redis_fake = FakeRedis({
        "odometer": (862.0, STALE),
        "powertrainOdometer": (50002.0, STALE),
        "fuelLevel": (74.0, STALE),
        "soc": (0.0, STALE),
        "powertrainEVStateOfCharge": (0.0, STALE),
        "ev_soc": (0.0, STALE),
        "lat": (45.49, FRESH),
        "lng": (-122.66, FRESH),
    })
    body = _run(monkeypatch, redis_fake, FakeSnapshotTable(_snapshot()))
    assert body["odometer"] is None
    assert body["fuelLevel"] is None
    # Position is fresh and keeps working.
    assert body["latitude"] == pytest.approx(45.49)
    assert body["locationSource"] == "redis"


def test_fresh_redis_odometer_and_charge_newer_than_history_are_returned(monkeypatch):
    redis_fake = FakeRedis({"odometer": (3960.4, FRESH), "fuelLevel": (71.0, FRESH)})
    body = _run(monkeypatch, redis_fake, FakeSnapshotTable(_snapshot()))
    assert body["odometer"] == pytest.approx(3960.4)
    assert body["fuelLevel"] == pytest.approx(71.0)


# ---------------------------------------------------------------------------
# The asOf boundary
# ---------------------------------------------------------------------------

def test_reading_at_the_last_millisecond_of_the_asof_day_loses(monkeypatch):
    edge = _end_of_day_ms("2026-11-05")
    body = _run(monkeypatch, FakeRedis({"odometer": (3955.0, edge), "fuelLevel": (70.0, edge)}),
                FakeSnapshotTable(_snapshot()))
    assert body["odometer"] is None
    assert body["fuelLevel"] is None


def test_reading_one_millisecond_after_the_asof_day_wins(monkeypatch):
    after = _end_of_day_ms("2026-11-05") + 1
    body = _run(monkeypatch, FakeRedis({"odometer": (3955.0, after), "fuelLevel": (70.0, after)}),
                FakeSnapshotTable(_snapshot()))
    assert body["odometer"] == pytest.approx(3955.0)
    assert body["fuelLevel"] == pytest.approx(70.0)


def test_reading_during_the_asof_day_loses(monkeypatch):
    """Noon on the asOf day is inside the history value: the day's miles are in it."""
    noon = _ms("2026-11-05T12:00:00Z")
    body = _run(monkeypatch, FakeRedis({"odometer": (3940.0, noon)}), FakeSnapshotTable(_snapshot()))
    assert body["odometer"] is None


# ---------------------------------------------------------------------------
# No history value
# ---------------------------------------------------------------------------

def test_no_snapshot_item_a_timed_reading_wins(monkeypatch):
    body = _run(monkeypatch, FakeRedis({"odometer": (862.0, STALE), "fuelLevel": (74.0, STALE)}),
                FakeSnapshotTable(None))
    assert body["odometer"] == pytest.approx(862.0)
    assert body["fuelLevel"] == pytest.approx(74.0)


def test_snapshot_field_without_asof_a_timed_reading_wins(monkeypatch):
    item = _snapshot(odo_asof=None, charge_asof=None, odo_value=None, charge_value=None)
    body = _run(monkeypatch, FakeRedis({"odometer": (862.0, STALE), "fuelLevel": (74.0, STALE)}),
                FakeSnapshotTable(item))
    assert body["odometer"] == pytest.approx(862.0)
    assert body["fuelLevel"] == pytest.approx(74.0)


def test_snapshot_item_missing_the_field_a_timed_reading_wins(monkeypatch):
    item = {"vehicleId": VID, "runDate": "2026-11-06"}
    body = _run(monkeypatch, FakeRedis({"odometer": (862.0, STALE)}), FakeSnapshotTable(item))
    assert body["odometer"] == pytest.approx(862.0)


# ---------------------------------------------------------------------------
# Fail closed
# ---------------------------------------------------------------------------

def test_snapshot_read_error_gives_null(monkeypatch):
    table = FakeSnapshotTable(error=RuntimeError("AccessDeniedException"))
    body = _run(monkeypatch, FakeRedis({"odometer": (3960.4, FRESH), "fuelLevel": (71.0, FRESH)}), table)
    assert table.calls, "the snapshot must have been asked"
    assert body["odometer"] is None
    assert body["fuelLevel"] is None
    # The rest of the response still answers.
    assert body["connectionStatus"] == "connected"


def test_snapshot_table_not_configured_gives_null(monkeypatch):
    body = _run(monkeypatch, FakeRedis({"odometer": (3960.4, FRESH), "fuelLevel": (71.0, FRESH)}), None)
    assert body["odometer"] is None
    assert body["fuelLevel"] is None


def test_unparseable_asof_gives_null(monkeypatch):
    item = _snapshot(odo_asof="last tuesday", charge_asof="??")
    body = _run(monkeypatch, FakeRedis({"odometer": (3960.4, FRESH), "fuelLevel": (71.0, FRESH)}),
                FakeSnapshotTable(item))
    assert body["odometer"] is None
    assert body["fuelLevel"] is None


def test_asof_time_without_a_zone_gives_null(monkeypatch):
    item = _snapshot(odo_asof="2026-11-05T10:00:00")
    body = _run(monkeypatch, FakeRedis({"odometer": (3960.4, FRESH)}), FakeSnapshotTable(item))
    assert body["odometer"] is None


def test_asof_instant_with_a_zone_is_compared_exactly(monkeypatch):
    """An ISO instant (CMS accepts these too) ends the history value at that instant."""
    item = _snapshot(odo_asof="2026-11-06T18:00:00Z")
    before = _run(monkeypatch, FakeRedis({"odometer": (3960.4, _ms("2026-11-06T17:59:59Z"))}),
                  FakeSnapshotTable(item))
    after = _run(monkeypatch, FakeRedis({"odometer": (3960.4, _ms("2026-11-06T18:00:01Z"))}),
                 FakeSnapshotTable(item))
    assert before["odometer"] is None
    assert after["odometer"] == pytest.approx(3960.4)


def test_reading_without_a_timestamp_gives_null_even_with_no_history(monkeypatch):
    body = _run(monkeypatch, FakeRedis({"odometer": (862.0, None), "fuelLevel": (74.0, None)}),
                FakeSnapshotTable(None))
    assert body["odometer"] is None
    assert body["fuelLevel"] is None


def test_untimed_reading_with_a_snapshot_gives_null(monkeypatch):
    """The reported shape with the time missing: a lone odometer beside a fresh meta
    lastSeenAt and a snapshot as of yesterday. Borrowing lastSeenAt would show it."""
    body = _run(monkeypatch, FakeRedis({"odometer": (862.0, None), "fuelLevel": (74.0, None)}),
                FakeSnapshotTable(_snapshot()))
    assert body["odometer"] is None
    assert body["fuelLevel"] is None


@pytest.mark.parametrize("bad_ts", ["", "0", "-5", "1.7e12", "abc"])
def test_reading_with_an_unusable_timestamp_gives_null(monkeypatch, bad_ts):
    redis_fake = FakeRedis({"odometer": (862.0, None)})
    redis_fake.timestamps[SID["odometer"]] = bad_ts
    body = _run(monkeypatch, redis_fake, FakeSnapshotTable(None))
    assert body["odometer"] is None


def test_meta_hash_readings_are_not_used(monkeypatch):
    """Flink writes no odometer or charge to the meta hash, and a meta value has no time."""
    meta = {"lastConnectedAt": str(int(time.time()) - 5), "odometer": "21268", "fuelLevel": "65.3"}
    body = _run(monkeypatch, FakeRedis({}, meta=meta), FakeSnapshotTable(None))
    assert body["odometer"] is None
    assert body["fuelLevel"] is None


def test_seeded_vehicles_row_values_never_appear(monkeypatch):
    body = _run(monkeypatch, FakeRedis({}), FakeSnapshotTable(None))
    assert body["odometer"] is None
    assert body["fuelLevel"] is None


# ---------------------------------------------------------------------------
# Aliases
# ---------------------------------------------------------------------------

def test_newest_odometer_alias_wins_over_an_older_one(monkeypatch):
    """Old MQTT `odometer` residue must not shadow a fresh FleetWise `powertrainOdometer`.

    The live-tier shape on 2026-11-06 (MRDAZ01S1PC00G2BA): `odometer` 50,010.6 from the
    MQTT era, `powertrainOdometer` 42,742 today; CMS shows 42,742.
    """
    redis_fake = FakeRedis({"odometer": (50010.6, STALE), "powertrainOdometer": (42742.0, FRESH)})
    body = _run(monkeypatch, redis_fake, FakeSnapshotTable(_snapshot(odo_value=Decimal("42690.1"))))
    assert body["odometer"] == pytest.approx(42742.0)


def test_newest_odometer_alias_wins_in_the_other_order(monkeypatch):
    redis_fake = FakeRedis({"odometer": (3958.2, FRESH), "powertrainOdometer": (50002.0, STALE)})
    body = _run(monkeypatch, redis_fake, FakeSnapshotTable(_snapshot()))
    assert body["odometer"] == pytest.approx(3958.2)


def test_odometer_alias_tie_keeps_list_order(monkeypatch):
    redis_fake = FakeRedis({"odometer": (3958.2, FRESH), "powertrainOdometer": (50002.0, FRESH),
                            "odo": (1.0, FRESH)})
    body = _run(monkeypatch, redis_fake, FakeSnapshotTable(_snapshot()))
    assert body["odometer"] == pytest.approx(3958.2)


def test_an_untimed_alias_does_not_beat_a_timed_one(monkeypatch):
    redis_fake = FakeRedis({"odometer": (862.0, None), "powertrainOdometer": (3958.2, FRESH)})
    body = _run(monkeypatch, redis_fake, FakeSnapshotTable(_snapshot()))
    assert body["odometer"] == pytest.approx(3958.2)


def test_newest_charge_alias_wins(monkeypatch):
    redis_fake = FakeRedis({"fuelLevel": (74.0, STALE), "ev_soc": (69.5, FRESH)})
    body = _run(monkeypatch, redis_fake, FakeSnapshotTable(_snapshot()))
    assert body["fuelLevel"] == pytest.approx(69.5)


def test_ev_newest_state_of_charge_signal_wins(monkeypatch):
    """Live-tier EVs send `powertrainEVStateOfCharge`; an old MQTT `soc` must not shadow it."""
    redis_fake = FakeRedis({"soc": (0.0, STALE), "powertrainEVStateOfCharge": (71.5, FRESH),
                            "fuelLevel": (55.2, STALE)})
    body = _run(monkeypatch, redis_fake, FakeSnapshotTable(_snapshot()))
    assert body["fuelLevel"] == pytest.approx(71.5)


def test_ev_fuel_level_only_when_no_state_of_charge_signal_has_a_reading(monkeypatch):
    """CMS tier 2: a state-of-charge reading, even an old one, is preferred to fuelLevel."""
    redis_fake = FakeRedis({"soc": (0.0, STALE), "fuelLevel": (70.0, FRESH)})
    body = _run(monkeypatch, redis_fake, FakeSnapshotTable(_snapshot()))
    assert body["fuelLevel"] is None


def test_ev_fuel_level_used_when_state_of_charge_signals_are_absent(monkeypatch):
    body = _run(monkeypatch, FakeRedis({"fuelLevel": (70.0, FRESH)}), FakeSnapshotTable(_snapshot()))
    assert body["fuelLevel"] == pytest.approx(70.0)


def test_fuel_car_reads_fuel_level_only(monkeypatch):
    """CMS `_live_fuel_level_direct_ts`: a fuel car's state-of-charge signal is not its fuel."""
    body = _run(monkeypatch, FakeRedis({"ev_soc": (70.0, FRESH), "soc": (70.0, FRESH)}),
                FakeSnapshotTable(None),
                vehicles_table=_vehicles_row(fuel_type="gasoline", powertrain="ice"))
    assert body["fuelLevel"] is None


# ---------------------------------------------------------------------------
# Charge on a non-electric car (CMS W3: no history charge to compare with)
# ---------------------------------------------------------------------------

def test_non_electric_fuel_reading_is_not_compared_with_battery_charge(monkeypatch):
    """A fuel car's history has no fuel value; CMS shows a timed fuel reading over the row."""
    yesterday_noon = _ms("2026-11-05T12:00:00Z")   # older than the end of batteryCharge's asOf
    body = _run(monkeypatch, FakeRedis({"fuelLevel": (41.0, yesterday_noon),
                                        "odometer": (3940.0, yesterday_noon)}),
                FakeSnapshotTable(_snapshot()),
                vehicles_table=_vehicles_row(fuel_type="gasoline", powertrain="ice"))
    assert body["fuelLevel"] == pytest.approx(41.0)
    # The odometer rule is the same for every car.
    assert body["odometer"] is None


def test_fuel_car_fuel_reading_does_not_read_the_snapshot(monkeypatch):
    table = FakeSnapshotTable(_snapshot())
    body = _run(monkeypatch, FakeRedis({"fuelLevel": (41.0, STALE)}), table,
                vehicles_table=_vehicles_row(fuel_type="gasoline", powertrain="ice"))
    assert body["fuelLevel"] == pytest.approx(41.0)
    assert table.calls == []


def test_fuel_car_keeps_its_fuel_reading_when_the_snapshot_read_fails(monkeypatch):
    """CMS W2: a fuel car's fuel level doesn't depend on the snapshot; the odometer does."""
    table = FakeSnapshotTable(error=RuntimeError("AccessDeniedException"))
    body = _run(monkeypatch, FakeRedis({"fuelLevel": (41.0, FRESH), "odometer": (3960.4, FRESH)}),
                table, vehicles_table=_vehicles_row(fuel_type="gasoline", powertrain="ice"))
    assert body["fuelLevel"] == pytest.approx(41.0)
    assert body["odometer"] is None


def test_powertrain_starting_fact_decides_before_fuel_type(monkeypatch):
    """CMS `_is_electric`: startingFacts.powertrain first, then fuelType."""
    yesterday_noon = _ms("2026-11-05T12:00:00Z")
    body = _run(monkeypatch, FakeRedis({"fuelLevel": (41.0, yesterday_noon)}),
                FakeSnapshotTable(_snapshot()),
                vehicles_table=_vehicles_row(fuel_type="gasoline", powertrain="electric"))
    assert body["fuelLevel"] is None


def test_fuel_type_decides_when_there_is_no_powertrain_fact(monkeypatch):
    yesterday_noon = _ms("2026-11-05T12:00:00Z")
    body = _run(monkeypatch, FakeRedis({"fuelLevel": (41.0, yesterday_noon)}),
                FakeSnapshotTable(_snapshot()),
                vehicles_table=_vehicles_row(fuel_type="electric", powertrain=None))
    assert body["fuelLevel"] is None


def test_charge_withheld_when_the_vehicles_row_is_missing(monkeypatch):
    """Without the row the powertrain is unknown: an EV's stale charge must not pass as fuel."""
    missing = MagicMock()
    missing.get_item.return_value = {}
    body = _run(monkeypatch, FakeRedis({"fuelLevel": (74.0, STALE), "odometer": (3960.4, FRESH)}),
                FakeSnapshotTable(_snapshot()), vehicles_table=missing)
    assert body["fuelLevel"] is None
    # The odometer rule doesn't depend on the row.
    assert body["odometer"] == pytest.approx(3960.4)


def test_charge_withheld_when_the_vehicles_row_read_fails(monkeypatch):
    failing = MagicMock()
    failing.get_item.side_effect = RuntimeError("ProvisionedThroughputExceededException")
    body = _run(monkeypatch, FakeRedis({"fuelLevel": (74.0, FRESH)}),
                FakeSnapshotTable(None), vehicles_table=failing)
    assert body["fuelLevel"] is None


@pytest.mark.parametrize("raw", ["nan", "inf", "-inf", "not-a-number"])
def test_non_finite_or_non_numeric_reading_is_skipped(monkeypatch, raw):
    """json.dumps(nan) is not JSON; iOS would fail the whole response."""
    body = _run(monkeypatch, FakeRedis({"odometer": (raw, FRESH)}), FakeSnapshotTable(None))
    assert body["odometer"] is None


# ---------------------------------------------------------------------------
# Cost: most cars have no live odometer or charge
# ---------------------------------------------------------------------------

def test_snapshot_is_not_read_when_redis_has_no_timed_reading(monkeypatch):
    table = FakeSnapshotTable(_snapshot())
    body = _run(monkeypatch, FakeRedis({"lat": (45.49, FRESH), "lng": (-122.66, FRESH)}), table)
    assert table.calls == []
    assert body["odometer"] is None


def test_snapshot_is_read_once_by_vehicle_id(monkeypatch):
    table = FakeSnapshotTable(_snapshot())
    _run(monkeypatch, FakeRedis({"odometer": (3960.4, FRESH), "fuelLevel": (71.0, FRESH)}), table)
    assert table.calls == [{"Key": {"vehicleId": VID}}]
