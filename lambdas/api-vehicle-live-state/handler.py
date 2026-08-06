"""
GET /vehicles/{vehicleId}/live-state

Returns the fresh, realtime fields CMS's UI surfaces for a vehicle — primarily
connection status and last-connected timestamp — by reading the same Redis
hash (`vehicle:{vehicleId}:meta`) the CMS main_api Lambda reads.

Why this exists
---------------
The CMS DDB `vehicles` row is a long-lived record that gets updated on IoT
connect/disconnect events, but it can lag reality by minutes or longer when
the simulator / real vehicles are actively emitting telemetry. The CMS UI
masks this by cross-referencing Redis — any vehicle with a Redis
`lastConnectedAt` within the last 5 minutes is considered "connected".

The iOS app was showing "Offline" for VEH-0025 while the CMS UI showed
"Connected" because iOS only saw the stale DDB field. This Lambda gives iOS
access to the same real-time view.

VPC placement
-------------
The Redis cluster is inside the CMS VPC (`CMS_VPC_ID`) with no public endpoint
and no cross-VPC peering, so this Lambda is VPC-attached into the same subnets
as the CMS main_api Lambda. We reuse the existing Redis security group for
ingress (already allows same-SG → 6379), mirroring the CMS shortcut. If we
ever split ownership of that SG, we'll need a dedicated VFO SG with an
explicit ingress rule.

Freshness semantics
-------------------
- `connectionStatus`: "connected" | "disconnected". Overridden to
  "connected" when Redis lastConnectedAt < 5 minutes ago (matches CMS).
- `lastConnectedAt`: ISO-8601 string, max-of(DDB.lastSeenAt, Redis value).
- `lastSeenAgoSeconds`: helper so the client doesn't need to re-parse.
- Also surfaces `fuelLevel`, `batteryLevel`, and any other scalar fields
  present in the Redis meta hash — these are where CMS UI gets its live
  numbers from when telemetry is flowing.

This Lambda is intentionally read-only and narrow. If richer live telemetry
is needed (coolant temp, tire PSI, etc.), add a separate endpoint rather
than bloating this one.
"""

import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import boto3
import redis

logger = logging.getLogger()
logger.setLevel(logging.INFO)

REDIS_ENDPOINT = os.environ["REDIS_ENDPOINT"]
REDIS_PORT = int(os.environ.get("REDIS_PORT", "6379"))
VEHICLES_TABLE = os.environ["CMS_VEHICLES_TABLE"]
# CMS UI uses a 2-minute window (120s) for connection freshness.
# Match exactly so iOS and CMS UI never disagree.
LIVE_WINDOW_SECONDS = 120

# Module-level clients — reused across warm invocations.
_redis_client: Optional[redis.Redis] = None
_ddb_resource = boto3.resource("dynamodb")
_vehicles_table = _ddb_resource.Table(VEHICLES_TABLE)


def _get_redis() -> Optional[redis.Redis]:
    """Lazy Redis client. Returns None on connection failure so the Lambda
    still returns a useful (DDB-only) response rather than 500'ing."""
    global _redis_client
    if _redis_client is not None:
        return _redis_client
    try:
        client = redis.Redis(
            host=REDIS_ENDPOINT,
            port=REDIS_PORT,
            socket_connect_timeout=2,
            socket_timeout=2,
            decode_responses=True,
        )
        client.ping()
        _redis_client = client
        return client
    except Exception as exc:
        logger.warning("Redis unreachable: %s", exc)
        return None


def _resp(status: int, body: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "statusCode": status,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*",
        },
        "body": json.dumps(body, default=str),
    }


def _parse_ts_to_epoch_seconds(raw: Any) -> Optional[float]:
    """Redis stores timestamps in several formats depending on who wrote them:
    ms-since-epoch ints as strings, s-since-epoch ints, ISO strings. Handle all."""
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    # Numeric: treat as seconds if < 10^12, else milliseconds.
    try:
        n = float(s)
        if n > 1e12:
            return n / 1000.0
        return n
    except ValueError:
        pass
    # ISO 8601.
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return dt.timestamp()
    except ValueError:
        return None


def _epoch_to_iso(epoch: Optional[float]) -> Optional[str]:
    if epoch is None:
        return None
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()


def handler(event: Dict[str, Any], _context: Any) -> Dict[str, Any]:
    vehicle_id = (event.get("pathParameters") or {}).get("vehicleId")
    if not vehicle_id:
        return _resp(400, {"error": "vehicleId required"})

    now = time.time()
    ddb_row: Dict[str, Any] = {}
    try:
        result = _vehicles_table.get_item(Key={"vehicleId": vehicle_id})
        ddb_row = result.get("Item") or {}
    except Exception as exc:
        logger.warning("DDB get_item failed for %s: %s", vehicle_id, exc)

    ddb_conn_status = (ddb_row.get("connectionStatus") or "disconnected").lower()
    ddb_last_seen = ddb_row.get("lastSeenAt")
    ddb_last_seen_epoch = _parse_ts_to_epoch_seconds(ddb_last_seen)

    # Redis meta hash. Keys observed in CMS code: lastConnectedAt, lastSeenAt,
    # connectionStatus, fuelLevel, batteryLevel, speed, plus a handful of
    # other telemetry scalars. We surface a curated subset.
    redis_meta: Dict[str, Any] = {}
    redis_signals: Dict[str, Any] = {}
    redis_last_connected_epoch: Optional[float] = None
    r = _get_redis()
    if r is not None:
        try:
            raw = r.hgetall(f"vehicle:{vehicle_id}:meta")
            redis_meta = dict(raw or {})
            lc = redis_meta.get("lastConnectedAt") or redis_meta.get("lastSeenAt")
            redis_last_connected_epoch = _parse_ts_to_epoch_seconds(lc)
        except Exception as exc:
            logger.warning("Redis hgetall failed for %s: %s", vehicle_id, exc)
        # Also pull the signals hash. The CMS /api/v1/vehicles/{id}
        # handler reads this in _build_live_vehicle_state to merge
        # real-time fuelLevel/engineTemp/odometer into its response.
        # Previously iOS's live-state Lambda only read `meta`, so iOS
        # displayed stale DDB values while CMS showed live numbers —
        # which is the bug the user flagged (odometer=67609 iOS vs
        # 863 CMS). Reading signals here unifies the two surfaces.
        # Added 2026-05-05.
        try:
            raw = r.hgetall(f"vehicle:{vehicle_id}:signals")
            redis_signals = dict(raw or {})
        except Exception as exc:
            logger.warning("Redis signals hgetall failed for %s: %s", vehicle_id, exc)

    # Apply CMS's 5-min live-window heuristic.
    effective_conn_status = ddb_conn_status
    if (redis_last_connected_epoch is not None
            and (now - redis_last_connected_epoch) < LIVE_WINDOW_SECONDS):
        effective_conn_status = "connected"
    # Note: do NOT trust redis_meta["connectionStatus"] alone — it persists
    # from the last session and is never explicitly set to "disconnected".
    # Only the timestamp-based window is authoritative.

    # Pick the freshest last-connected we've got.
    candidates = [e for e in (redis_last_connected_epoch, ddb_last_seen_epoch) if e]
    last_connected_epoch = max(candidates) if candidates else None

    def _num(key: str) -> Optional[float]:
        raw = redis_meta.get(key) or ddb_row.get(key)
        if raw is None or raw == "":
            return None
        try:
            return float(raw)
        except (TypeError, ValueError):
            return None

    # -----------------------------------------------------------------
    # Resolve the signals hash (numeric signal_id → named value) via
    # the signal catalog cached in Redis at `signal_catalog:reverse`.
    # The catalog is seeded at deploy time; each entry is a pipe-
    # delimited "name|vssPath|unit|dataType" string. We only need
    # `name` here. If the catalog lookup fails we fall through with
    # only meta-hash values — caller will see stale numbers but no
    # error. Added 2026-05-05.
    # -----------------------------------------------------------------
    sig_by_name: Dict[str, str] = {}
    if redis_signals and r is not None:
        try:
            catalog_raw = r.hgetall("signal_catalog:reverse") or {}
            id_to_name: Dict[str, str] = {}
            for sig_id, meta_str in catalog_raw.items():
                # meta_str format: "name|vssPath|unit|dataType"
                parts = (meta_str or "").split("|", 1)
                if parts and parts[0]:
                    id_to_name[str(sig_id)] = parts[0]
            for sig_id_str, value in redis_signals.items():
                name = id_to_name.get(str(sig_id_str))
                if name:
                    sig_by_name[name] = value
        except Exception as exc:
            logger.warning("signal catalog reverse lookup failed: %s", exc)

    # Name-aliasing: CMS aliases a handful of semantic synonyms into
    # the four canonical UI fields. Mirror those so iOS never shows a
    # different number than CMS for the same vehicle. Added `ev_soc`
    # for BEVs — the EV state-of-charge signal is the BEV equivalent
    # of fuelLevel.
    aliases = {
        "fuelLevel":      ["fuelLevel", "fuel_level", "currentFuelLevel", "ev_soc"],
        "odometer":       ["odometer", "powertrainOdometer", "odo"],
        "batteryVoltage": ["batteryVoltage", "battery_voltage"],
        "engineTemp":     ["engineTemp", "engine_temp", "coolantTemp", "catalyst_temp"],
        "speed":          ["speed", "lastSpeed", "VehicleSpeed"],
        # Location aliases — CMS's main_api reads sig_by_name["lat"]/["lng"]
        # from Redis signals to build `currentLocation`. iOS's Vehicle tab
        # was displaying the DDB vehicles-row lastLatitude/lastLongitude
        # which can be days stale (VEH-0047 was showing Phoenix from Apr 24
        # while CMS showed Seattle live). Surfacing these here so iOS can
        # overlay the map marker with the same coords CMS displays.
        # Added 2026-05-06.
        "latitude":       ["lat", "latitude", "Latitude", "gps_lat", "currentLocation.latitude"],
        "longitude":      ["lng", "longitude", "Longitude", "lon", "gps_lng", "gps_lon", "currentLocation.longitude"],
        "heading":        ["heading", "Heading", "gps_heading"],
    }

    def _live(field: str) -> Optional[float]:
        """Return the freshest numeric value for a canonical UI field,
        trying signals-hash aliases first, then meta hash, then DDB."""
        for candidate in aliases.get(field, [field]):
            if candidate in sig_by_name and sig_by_name[candidate] not in ("", None):
                try:
                    return float(sig_by_name[candidate])
                except (TypeError, ValueError):
                    pass
        return _num(field)

    def _live_location(field: str, ddb_fallback_key: str) -> Optional[float]:
        """Same as _live but with a dedicated DDB fallback key because
        the vehicles table uses `lastLatitude`/`lastLongitude` rather
        than `latitude`/`longitude`. Fallback is intentionally last so
        fresh Redis values always win over potentially-stale DDB rows."""
        for candidate in aliases.get(field, [field]):
            if candidate in sig_by_name and sig_by_name[candidate] not in ("", None):
                try:
                    return float(sig_by_name[candidate])
                except (TypeError, ValueError):
                    pass
        raw = ddb_row.get(ddb_fallback_key)
        if raw is None or raw == "":
            return None
        try:
            return float(raw)
        except (TypeError, ValueError):
            return None

    body: Dict[str, Any] = {
        "vehicleId": vehicle_id,
        "connectionStatus": effective_conn_status,
        "connectionStatusSource": (
            "redis-live-window"
            if effective_conn_status == "connected"
            and ddb_conn_status != "connected"
            else ("redis" if r is not None else "ddb")
        ),
        "lastConnectedAt": _epoch_to_iso(last_connected_epoch),
        "lastSeenAgoSeconds": (
            int(now - last_connected_epoch) if last_connected_epoch else None
        ),
        "fuelLevel": _live("fuelLevel"),
        "batteryLevel": _num("batteryLevel") or _num("battery_level"),
        "speed": _live("speed"),
        "engineTemp": _live("engineTemp"),
        "odometer": _live("odometer"),
        "batteryVoltage": _live("batteryVoltage"),
        # Location fields — Redis-fresh when telemetry is flowing,
        # DDB-fallback otherwise. Parity with CMS's `currentLocation`
        # (which reads the same sig_by_name["lat"]/["lng"] values).
        # Added 2026-05-06 to fix the iOS Vehicle tab map displaying
        # stale DDB coordinates.
        "latitude": _live_location("latitude", "lastLatitude"),
        "longitude": _live_location("longitude", "lastLongitude"),
        "heading": _live("heading"),
        # Debug / observability fields. Trim these later if payload matters.
        "ddbConnectionStatus": ddb_conn_status,
        "ddbLastSeenAt": ddb_last_seen,
        "redisReachable": r is not None,
        "redisSignalsCount": len(sig_by_name),
    }

    return _resp(200, body)
