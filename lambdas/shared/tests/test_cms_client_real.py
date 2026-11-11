"""Unit tests for RealCmsClient using fakeredis and a stub DynamoDB resource.

No AWS credentials, no live network. Verifies protocol compliance, schema
handling, unit conversion (F→C), staleness detection, catalog lookup, and
DTC collapse-to-latest-per-code with reset filtering.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import fakeredis
import pytest

HERE = Path(__file__).resolve()
ROOT = HERE.parents[3]
sys.path.insert(0, str(ROOT / "lambdas"))

from shared.cms_client_real import RealCmsClient  # noqa: E402
from triage_classifier.types import ActiveDtc, TelemetrySnapshot  # noqa: E402


VIN = "1FMCU0F70MUA12345"
VEHICLE_ID = "VEH-0025"


# ---- Catalog + Redis fixtures ----------------------------------------------


def _seed_catalog(r: fakeredis.FakeRedis) -> None:
    # Match the CMS signal-catalog.json names; IDs are arbitrary for the test.
    mapping = {
        "speed": 1, "engineRPM": 2, "coolant_temp": 3, "ignitionOn": 4,
        "lat": 14, "lng": 15, "heading": 16,
        "tire_fl": 20, "tire_fr": 21, "tire_rl": 22, "tire_rr": 23,
        "brake_wear": 30, "fuel_level": 31, "battery_soc": 32,
        "oil_pressure": 33, "oil_temp": 34, "abs_status": 40,
        "odometer": 50,
    }
    r.hset("signal_catalog:map", mapping={k: str(v) for k, v in mapping.items()})


def _seed_vehicle(r: fakeredis.FakeRedis, signals: dict[str, str], meta: dict[str, str]) -> None:
    r.hset(f"vehicle:{VEHICLE_ID}:signals", mapping=signals)
    r.hset(f"vehicle:{VEHICLE_ID}:meta", mapping=meta)


# ---- Stub DynamoDB ----------------------------------------------------------


class _StubTable:
    def __init__(self, items: list[dict]) -> None:
        self._items = items

    def get_item(self, Key):  # noqa: N803 - mimic boto3 kwargs
        for it in self._items:
            if all(it.get(k) == v for k, v in Key.items()):
                return {"Item": it}
        return {}

    def scan(self, **kwargs):  # noqa: N803 - mimic boto3 kwargs
        # Minimal support for FilterExpression of form "<field> = :<placeholder>"
        # which is what _resolve_vehicle_id uses. Other filters fall through and
        # return all items.
        items = list(self._items)
        filter_expr = kwargs.get("FilterExpression") or ""
        values = kwargs.get("ExpressionAttributeValues") or {}
        if "=" in filter_expr and values:
            # Parse "field = :placeholder"
            try:
                lhs, rhs = [p.strip() for p in filter_expr.split("=", 1)]
                placeholder = rhs
                expected = values.get(placeholder)
                items = [it for it in items if it.get(lhs) == expected]
            except Exception:
                pass
        limit = kwargs.get("Limit")
        if limit is not None:
            items = items[:limit]
        return {"Items": items}

    def query(self, **kwargs):
        # Intentionally minimal: return pre-seeded items, honor ScanIndexForward=False.
        items = list(self._items)
        if not kwargs.get("ScanIndexForward", True):
            items.reverse()
        return {"Items": items}


class _StubDdb:
    def __init__(self, tables: dict[str, list[dict]]) -> None:
        self._tables = {name: _StubTable(items) for name, items in tables.items()}

    def Table(self, name):  # noqa: N802
        return self._tables[name]


@pytest.fixture
def redis_client() -> fakeredis.FakeRedis:
    r = fakeredis.FakeRedis(decode_responses=True)
    _seed_catalog(r)
    return r


# ---- Tests -----------------------------------------------------------------


def test_telemetry_green_baseline(redis_client):
    now = int(time.time() * 1000)
    _seed_vehicle(
        redis_client,
        signals={
            "1": "58", "3": "190.4", "20": "34", "21": "34", "22": "34", "23": "34",
            "30": "6.0", "32": "82", "40": "true",
        },
        meta={"lastSeenAt": str(now), "connectionStatus": "connected"},
    )
    client = RealCmsClient(
        redis_client=redis_client,
        ddb_resource=_StubDdb(
            {"cms-prod-storage-vehicles": [{"vin": VIN, "vehicleId": VEHICLE_ID}]}
        ),
    )
    t = client.get_telemetry(VIN)
    assert isinstance(t, TelemetrySnapshot)
    assert t.stale is False
    assert t.speed_mph == 58
    assert round(t.coolant_temp_c, 1) == round((190.4 - 32) * 5 / 9, 1)  # F→C
    assert t.tire_pressure_fl_psi == 34
    assert t.abs_status_ok is True


def test_telemetry_stale_when_last_seen_old(redis_client):
    old = int(time.time() * 1000) - (10 * 60 * 1000)  # 10 min ago
    _seed_vehicle(
        redis_client,
        signals={"1": "58", "3": "190.4"},
        meta={"lastSeenAt": str(old), "connectionStatus": "connected"},
    )
    client = RealCmsClient(
        redis_client=redis_client,
        ddb_resource=_StubDdb(
            {"cms-prod-storage-vehicles": [{"vin": VIN, "vehicleId": VEHICLE_ID}]}
        ),
    )
    t = client.get_telemetry(VIN)
    assert t.stale is True
    # Values still present for debugging visibility
    assert t.speed_mph == 58


def test_telemetry_absent_when_vin_unknown(redis_client):
    client = RealCmsClient(
        redis_client=redis_client,
        ddb_resource=_StubDdb({"cms-prod-storage-vehicles": []}),
    )
    t = client.get_telemetry(VIN)
    assert t.stale is True
    assert t.speed_mph is None


def test_telemetry_p1_coolant_scenario(redis_client):
    """Classifier-critical: coolant 245°F ≈ 118°C, our P1 threshold."""
    now = int(time.time() * 1000)
    _seed_vehicle(
        redis_client,
        signals={"1": "58", "3": "245.0", "40": "true"},
        meta={"lastSeenAt": str(now)},
    )
    client = RealCmsClient(
        redis_client=redis_client,
        ddb_resource=_StubDdb(
            {"cms-prod-storage-vehicles": [{"vin": VIN, "vehicleId": VEHICLE_ID}]}
        ),
    )
    t = client.get_telemetry(VIN)
    assert abs(t.coolant_temp_c - 118.3) < 0.5


def test_abs_off_detection(redis_client):
    now = int(time.time() * 1000)
    _seed_vehicle(
        redis_client,
        signals={"1": "58", "40": "false"},
        meta={"lastSeenAt": str(now)},
    )
    client = RealCmsClient(
        redis_client=redis_client,
        ddb_resource=_StubDdb(
            {"cms-prod-storage-vehicles": [{"vin": VIN, "vehicleId": VEHICLE_ID}]}
        ),
    )
    t = client.get_telemetry(VIN)
    assert t.abs_status_ok is False


def test_dtcs_collapse_to_latest_per_code_and_skip_reset(redis_client):
    now = int(time.time() * 1000)
    # Note: table is keyed by vehicleId (not vin) per CMS schema. Rows still
    # carry `vin` as a regular attribute for human-readable audit but the
    # query runs against vehicleId.
    dtc_rows = [
        {"vin": VIN, "vehicleId": VEHICLE_ID, "timestamp": now - 1000, "code": "P0217", "firstSeenAt": now - 5000, "persistent": True, "status": "ACTIVE"},
        {"vin": VIN, "vehicleId": VEHICLE_ID, "timestamp": now - 2000, "code": "P0217", "firstSeenAt": now - 7000, "status": "ACTIVE"},  # older, should be dropped
        {"vin": VIN, "vehicleId": VEHICLE_ID, "timestamp": now - 3000, "code": "P0420", "firstSeenAt": now - 8000, "resetAt": now - 100},  # reset via legacy resetAt field
        {"vin": VIN, "vehicleId": VEHICLE_ID, "timestamp": now - 4000, "code": "C1234", "firstSeenAt": now - 9000, "status": "ACTIVE"},
        # New-style cleared: uses status field per CMS schema.
        {"vin": VIN, "vehicleId": VEHICLE_ID, "timestamp": now - 5000, "code": "U0100", "firstSeenAt": now - 9500, "status": "CLEARED"},
    ]
    client = RealCmsClient(
        redis_client=redis_client,
        ddb_resource=_StubDdb(
            {
                "cms-prod-storage-vehicles": [{"vin": VIN, "vehicleId": VEHICLE_ID}],
                "cms-prod-storage-dtc-history": dtc_rows,
            }
        ),
    )
    dtcs = client.get_active_dtcs(VIN)
    codes = [d.code for d in dtcs]
    assert "P0217" in codes
    assert "C1234" in codes
    assert "P0420" not in codes  # legacy resetAt — excluded
    assert "U0100" not in codes  # status=CLEARED — excluded


def test_recalls_empty_when_no_url(redis_client, monkeypatch):
    monkeypatch.delenv("CMS_RECALL_API_URL", raising=False)
    client = RealCmsClient(
        redis_client=redis_client,
        ddb_resource=_StubDdb({}),
    )
    assert client.get_open_recalls(VIN) == ()


def test_catalog_missing_signal_returns_none(redis_client):
    now = int(time.time() * 1000)
    # Vehicle reports a signal but catalog has no mapping for it (simulate OEM new signal)
    redis_client.delete("signal_catalog:map")
    redis_client.hset("signal_catalog:map", mapping={"speed": "1"})  # only speed
    _seed_vehicle(
        redis_client,
        signals={"1": "58", "99": "unknown"},
        meta={"lastSeenAt": str(now)},
    )
    client = RealCmsClient(
        redis_client=redis_client,
        ddb_resource=_StubDdb(
            {"cms-prod-storage-vehicles": [{"vin": VIN, "vehicleId": VEHICLE_ID}]}
        ),
    )
    t = client.get_telemetry(VIN)
    assert t.speed_mph == 58
    assert t.coolant_temp_c is None  # no mapping, no crash
