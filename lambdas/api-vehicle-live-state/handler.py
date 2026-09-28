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

# ── TLS + AUTH toggles (2026-09-16) ────────────────────────────────────────────
#
# Both off by default so the current CMS-cluster posture (no transitEncryptionEnabled,
# no authToken) is byte-identical to pre-change behavior. See the DMS sibling
# `~/guidance-for-dealer-management-system-on-aws/source/_lib/redis_client.py` for the
# design and the coordinated CMS+DMS+CVX rollout note.
#
# `REDIS_TLS_ENABLED` — strict case-insensitive `"true"`. Any other value — unset, empty,
# `"false"`, `"0"`, a typo like `"ture"` — reads as False. A `bool(os.environ.get(...))`
# predicate reads any non-empty string as True and silently enables TLS on a misspelling,
# which is worse than never enabling it because there is no visible symptom until the
# handshake fails.
#
# `REDIS_AUTH_TOKEN_SECRET_ARN` — ARN of a Secrets Manager secret whose SecretString is
# the raw AUTH token. Fetched at first connect. Never logged, never in exception messages.
# A Lambda env var containing the raw token would be visible to any principal with
# `lambda:GetFunctionConfiguration` per `~/.kiro/steering/secrets-handling.md`
# § "Runtime launch parameters", so the ARN indirection is load-bearing.
_TLS_ENV_VAR = "REDIS_TLS_ENABLED"
_AUTH_SECRET_ARN_ENV_VAR = "REDIS_AUTH_TOKEN_SECRET_ARN"

# Module-level clients — reused across warm invocations.
_redis_client: Optional[redis.Redis] = None
_ddb_resource = boto3.resource("dynamodb")
_vehicles_table = _ddb_resource.Table(VEHICLES_TABLE)


def _tls_enabled() -> bool:
    """Return True iff ``REDIS_TLS_ENABLED`` is set to ``"true"`` (case-insensitive).

    Strict-string on purpose. A permissive ``bool(os.environ.get(...))`` would read
    ``"ture"`` (typo), ``"0"``, ``"false"``, and every other non-empty misspelling as True
    and enable TLS silently.
    """
    return os.environ.get(_TLS_ENV_VAR, "").strip().lower() == "true"


def _auth_token_secret_arn() -> str:
    """Return the AUTH-token secret ARN, or the empty string when unset.

    A separate helper so tests can patch the env at runtime — a module-level constant
    would freeze the value at import time.
    """
    return os.environ.get(_AUTH_SECRET_ARN_ENV_VAR, "").strip()


def _fetch_auth_token(secret_arn: str) -> Optional[str]:
    """Fetch the Redis AUTH token from AWS Secrets Manager. Never raises.

    Returns the raw token string, or ``None`` when the fetch fails for any reason
    (IAM AccessDenied, ResourceNotFoundException, network, wrong ARN, binary-only
    secret). Fail-open per the module contract in `_get_redis()`.

    The token is never logged and never included in exception messages: exceptions are
    caught broadly and logged as the exception TYPE only. This avoids the historical
    footgun where a mid-handshake error message from a downstream library includes
    fragments of the credential in server-echoed text.
    """
    try:
        sm = boto3.client("secretsmanager")
        response = sm.get_secret_value(SecretId=secret_arn)
        secret_string = response.get("SecretString")
        if not secret_string:
            logger.warning(
                "Redis AUTH secret has no SecretString (binary secret unsupported); "
                "AUTH disabled"
            )
            return None
        return str(secret_string)
    except Exception as exc:  # noqa: BLE001 — fail open on ANY secret-fetch failure
        # Log the exception TYPE only. Do NOT include the secret ARN body in the message
        # — reproducing `str(exc)` risks including whatever the SDK decided to interpolate.
        logger.warning(
            "Redis AUTH secret fetch failed (%s); AUTH disabled", type(exc).__name__
        )
        return None


def _get_redis() -> Optional[redis.Redis]:
    """Lazy Redis client. Returns None on connection failure so the Lambda
    still returns a useful (DDB-only) response rather than 500'ing.

    TLS + AUTH
    ----------
    When ``REDIS_TLS_ENABLED=true`` the connection is upgraded to TLS with server-cert
    verification (``ssl_cert_reqs="required"``). When ``REDIS_AUTH_TOKEN_SECRET_ARN`` is
    set the module fetches the AUTH token from Secrets Manager and passes it as
    ``password=``. Either may be enabled independently; ElastiCache supports each toggle
    separately. Both are inert until CMS enables the matching cluster-side setting.

    Fail-open discipline: TLS handshake failure, secret-fetch failure, or a binary-secret
    entry all disable the cache (return None). We do NOT fall back to a plaintext or
    unauthenticated connection when TLS/AUTH is configured — the whole point of the
    toggle is to prevent on-network eavesdroppers from reading the tier in transit.
    """
    global _redis_client
    if _redis_client is not None:
        return _redis_client

    # Build kwargs incrementally so the default (TLS off, AUTH off) path is byte-identical
    # to the pre-2026-09-16 constructor. This matters for the "no regression on the current
    # cluster posture" guarantee — a call with both env vars unset must produce exactly the
    # client that existed before this change.
    kwargs: Dict[str, Any] = {
        "host": REDIS_ENDPOINT,
        "port": REDIS_PORT,
        "socket_connect_timeout": 2,
        "socket_timeout": 2,
        "decode_responses": True,
    }

    if _tls_enabled():
        # ``ssl_cert_reqs="required"`` means the server cert MUST be present AND validate
        # against the system CA bundle. Never weaken to ``"none"`` — that would defeat the
        # point of TLS and let an active attacker intercept/forge with a self-signed cert.
        kwargs["ssl"] = True
        kwargs["ssl_cert_reqs"] = "required"

    auth_arn = _auth_token_secret_arn()
    if auth_arn:
        token = _fetch_auth_token(auth_arn)
        if token is None:
            # Secret fetch failed. Do NOT connect unauthenticated — the operator signaled
            # they wanted AUTH; falling back to no-AUTH would silently downgrade. Fail-open
            # here (cache disabled, DDB fallback) is the correct outcome and preserves the
            # module contract that Redis is best-effort.
            logger.warning("Redis AUTH configured but token fetch failed; cache disabled")
            return None
        kwargs["password"] = token

    try:
        client = redis.Redis(**kwargs)
        client.ping()
        _redis_client = client
        return client
    except Exception as exc:
        # Log the exception TYPE only, not str(exc). redis-py has historically echoed
        # server error content back verbatim, which for a mid-handshake failure can
        # include partial credentials.
        logger.warning("Redis unreachable: %s", type(exc).__name__)
        return None


def _resp(status: int, body: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "statusCode": status,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": os.environ.get("ALLOWED_ORIGIN", "*"),
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

    # F7.1: Track provenance PER AXIS and report "redis" only when BOTH axes came from Redis.
    # F6.1 latched on the first non-None resolution (latitude resolves first).  That meant a
    # Redis latitude + DDB longitude was labelled "redis" — a blended coordinate that looks
    # Redis-fresh but is not.  Cycle 7 demonstrated the failure: (47.6062, -115.1398) reached
    # the escalate gate, latitude was fresh (Redis), longitude was days-old (DDB), and the
    # gate accepted it because locationSource said "redis".
    #
    # Fix: record the source for each axis independently.  After both _live_location calls,
    # set locationSource to "redis" only when BOTH axes are "redis"; otherwise "ddb" (the
    # weaker of the two, because the field describes the provenance of the coordinate pair).
    # None when neither source had any coordinate.
    #
    # The escalate gate predicate (locationSource == "redis") is UNCHANGED — it already
    # refuses anything that is not "redis".  This change makes the field's value accurate.
    _lat_source: Optional[str] = None
    _lon_source: Optional[str] = None

    def _live_location(field: str, ddb_fallback_key: str) -> Optional[float]:
        """Same as _live but with a dedicated DDB fallback key because
        the vehicles table uses `lastLatitude`/`lastLongitude` rather
        than `latitude`/`longitude`. Fallback is intentionally last so
        fresh Redis values always win over potentially-stale DDB rows.

        F7.1: records per-axis provenance (_lat_source / _lon_source) so
        locationSource can be set to "redis" only when BOTH axes came from
        Redis signals, not just the first-resolving axis.
        """
        nonlocal _lat_source, _lon_source
        for candidate in aliases.get(field, [field]):
            if candidate in sig_by_name and sig_by_name[candidate] not in ("", None):
                try:
                    val = float(sig_by_name[candidate])
                    if field == "latitude":
                        _lat_source = "redis"
                    else:
                        _lon_source = "redis"
                    return val
                except (TypeError, ValueError):
                    pass
        raw = ddb_row.get(ddb_fallback_key)
        if raw is None or raw == "":
            return None
        try:
            val = float(raw)
            if field == "latitude":
                _lat_source = "ddb"
            else:
                _lon_source = "ddb"
            return val
        except (TypeError, ValueError):
            return None

    lat_val = _live_location("latitude", "lastLatitude")
    lon_val = _live_location("longitude", "lastLongitude")

    # Derive the pair's provenance: "redis" only when both axes came from Redis.
    # "ddb" when either axis fell through to the DDB fallback (the weaker source wins).
    # None when neither source produced a coordinate.
    if _lat_source == "redis" and _lon_source == "redis":
        _location_source: Optional[str] = "redis"
    elif _lat_source is not None or _lon_source is not None:
        _location_source = "ddb"
    else:
        _location_source = None

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
        "latitude": lat_val,
        "longitude": lon_val,
        "heading": _live("heading"),
        # F7.1 / F6.1: Provenance field — "redis" / "ddb" / None.
        # "redis" means BOTH axes came from live signal aliases (telemetry-backed).
        # "ddb" means either axis fell through to lastLatitude/lastLongitude (potentially stale).
        # None means neither source had a coordinate.
        # This is the field the escalate freshness gate relies on.
        # `redisReachable` is client connectivity, not coordinate provenance.
        "locationSource": _location_source,
        # F7.2: vin from the vehicles row (additive).
        # Used by the escalate handler to cross-check that the caller-supplied vin
        # matches the vehicleId being located — a mismatch sends a truck to vehicle A's
        # location under vehicle B's VIN. Absent when the DDB row has no vin field
        # (e.g. old records), which the escalate handler treats as a refusal (version-skew).
        "vin": ddb_row.get("vin"),
        # Debug / observability fields. Trim these later if payload matters.
        "ddbConnectionStatus": ddb_conn_status,
        "ddbLastSeenAt": ddb_last_seen,
        "redisReachable": r is not None,
        "redisSignalsCount": len(sig_by_name),
    }

    return _resp(200, body)
