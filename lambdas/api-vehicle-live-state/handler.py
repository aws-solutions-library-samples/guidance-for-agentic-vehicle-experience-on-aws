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
`lastConnectedAt` within the live window (LIVE_WINDOW_SECONDS, 120 s here) is considered "connected".

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
  "connected" when Redis lastConnectedAt < LIVE_WINDOW_SECONDS (120 s) ago. CMS main_api
  now uses 180 s (`connection_status.py` STALENESS_WINDOW_S).
- `lastConnectedAt`: ISO-8601 string, max-of(DDB.lastSeenAt, Redis value).
- `lastSeenAgoSeconds`: helper so the client doesn't need to re-parse.
- Every value read from the Redis signals hash is served only when its own time (the
  `vehicle:{id}:timestamps` hash) passes its rule, else null; a value with no time, a
  time more than FUTURE_TOLERANCE_SECONDS ahead, or a non-finite value is never served
  (issues/2026-11-07-live-state-stale-values/). Meta-hash values carry no time and
  are not served; neither is the vehicles row, except the labelled location fallback.
- `speed`, `heading`, `engineTemp`, `batteryVoltage`, `batteryLevel` (current state):
  the newest timed reading among the field's aliases if it is at most
  CURRENT_READING_MAX_AGE_SECONDS (= LIVE_WINDOW_SECONDS, 120 s) old.
- `latitude`, `longitude` (last known): the newest timed fix if it is at most
  LAST_KNOWN_MAX_AGE_SECONDS (7 days, Flink's last-known-state TTL) old and on the globe,
  else the vehicles row's lastLatitude/lastLongitude; `locationSource` says which.
- `locationAgeSeconds`: how long ago CMS Flink stored the Redis fix (the older axis's
  `vehicle:{id}:timestamps` time, whole seconds rounded up), null unless `locationSource`
  is "redis". Not the connection's age: `lastSeenAgoSeconds` can be fresh while the fix is
  old. /escalate gates roadside dispatch on it
  (issues/2026-11-07-escalate-gate-uses-connection-age-not-fix-age/). The time is when CMS
  Flink stored the fix (its processing clock), not when the car sampled it, so a fix the
  car stored and uploaded late reads as new.
- `odometer` and `fuelLevel` (charge): the Redis reading CMS's vehicle page
  would show, else null. A reading counts only when its own time (the
  `vehicle:{id}:timestamps` hash) is after the end of the snapshot value's
  `asOf` day, CMS's one precedence rule (spec 2026-10-22-vehicle-data-consistency
  § D2); null makes iOS fall back to /drivers/me, which carries the snapshot.
  Signal lists and tiers match CMS `vehicle_values.py`. A fuel car's fuel
  level has no history value: any timed `fuelLevel` reading is returned.
  Deliberate differences from CMS (all fail closed; none occurs on staging
  data as of 2026-11-06): a signal with no time of its own is withheld (CMS
  times it by meta `lastSeenAt`, which Flink refreshes on every message, so it
  would pass a weeks-old odometer); an unreadable `asOf` withholds (CMS lets
  live win); no fallback to the DDB telemetry table (this route is Redis-only).
  issues/2026-11-06-live-state-stale-odometer/.

This Lambda is intentionally read-only and narrow. If richer live telemetry
is needed (coolant temp, tire PSI, etc.), add a separate endpoint rather
than bloating this one.
"""

import json
import logging
import math
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
# The daily vehicle snapshot (CMS `cms-{stage}-storage-vehicle-snapshot`, pk vehicleId):
# the history value each live odometer/charge reading is compared with. Unset means
# no comparison is possible, and those two readings are withheld (see `_load_snapshot`).
VEHICLE_SNAPSHOT_TABLE = os.environ.get("CMS_VEHICLE_SNAPSHOT_TABLE", "").strip()
# This route's connection-freshness window. It was set to match CMS's 120 s; CMS main_api
# now uses 180 s (`connection_status.py` STALENESS_WINDOW_S) and this route keeps 120 s.
# CURRENT_READING_MAX_AGE_SECONDS inherits it, so changing it moves every current-state
# limit too (issues/2026-11-07-live-state-stale-values/decisions.md 02:30Z).
LIVE_WINDOW_SECONDS = 120

# ── Age rules for values read from Redis (issues/2026-11-07-live-state-stale-values/) ──
#
# Flink writes each signal's time beside it in `vehicle:{id}:timestamps` and re-sets the
# whole hash's TTL on every message, so one fresh signal keeps every old one alive (a
# 17-day-old speed of 0 sat beside fresh doors and position on VEH-MRDN-0015). Every
# value this route reads from the signals hash is therefore served only when its own time
# passes its rule; otherwise it is null. Meta-hash values carry no time and are not served.
#
# Current-state values (speed, heading, engine temperature, battery voltage and level)
# describe the car now. A reading counts for the same window this route uses to call the
# car connected: a car that is "disconnected" here has no current speed. The FleetWise
# live campaign collects every 15 s (CMS `deployment/scripts/_lib/d8_signals.py`) and
# MQTT-direct cars send about every 15 s, so a driving car stays well inside it.
CURRENT_READING_MAX_AGE_SECONDS = LIVE_WINDOW_SECONDS
# Position is a last-known value: a parked car is where its last fix put it, CMS's
# vehicle page shows that fix with no age limit, and the fallbacks (iOS and the agents'
# nearest-dealer lookup read the vehicles row's lastLatitude/lastLongitude) are older or
# another place. A fix counts for Flink's own lifetime for last-known state (`LKS_TTL`,
# 7 days, EventDrivenTelemetryProcessor), which Flink enforces only per hash.
LAST_KNOWN_MAX_AGE_SECONDS = 7 * 86_400
# A reading time this far ahead of the Lambda clock is not a usable time. Without this a
# future-stamped reading would pass every age rule for as long as it stays in Redis.
FUTURE_TOLERANCE_SECONDS = 60

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
_snapshot_table = _ddb_resource.Table(VEHICLE_SNAPSHOT_TABLE) if VEHICLE_SNAPSHOT_TABLE else None


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


# ── Live reading vs history value (odometer, charge) ──────────────────────────
#
# CMS spec 2026-10-22-vehicle-data-consistency § D2: every screen uses one precedence,
# "live or feed reading if newer than the history value, else the history value".
# CMS applies it in modules/cms_ui/source/handlers/main_api/vehicle_values.py
# (`_apply_precedence`). iOS Home shows this route's odometer and charge first and falls
# back to /drivers/me (the snapshot) on null, so this route returns a Redis reading only
# when it would win there. Flink re-sets the signals hash's TTL on every message, so a car
# that keeps sending doors and position keeps an odometer from weeks ago
# (issues/2026-11-06-live-state-stale-odometer/: 862 mi from 2026-10-20 against a
# 3,951 mi history value).

_DIGITS = frozenset("0123456789")

# The signals CMS reads for each value, in its order (vehicle_values.py
# `_ODOMETER_SIGNAL_NAMES`, `_live_charge_ts` tiers, `_live_fuel_level_direct_ts`).
# Reading the same signals is what makes the numbers match: on 2026-11-06 every
# FleetWise live-tier car sent a fresh `powertrainOdometer` (e.g. 42,742 mi) while the
# first alias, an old MQTT `odometer` near the simulator's 50,000 mi baseline, was
# what this route returned.
_ODOMETER_SIGNALS = ("odometer", "powertrainOdometer", "odo")
# Electric cars: any state-of-charge signal first; `fuelLevel` only when none has a
# reading (CMS tier 2).
_EV_CHARGE_SIGNALS = ("soc", "powertrainEVStateOfCharge", "battSOC", "socCurrent", "ev_soc")
_FUEL_SIGNALS = ("fuelLevel",)


def _signal_ts_ms(raw: Any) -> int:
    """A signal's time from `vehicle:{id}:timestamps` (Flink writes epoch ms), or 0.

    0 means unusable: missing, empty, signed, fractional or not ASCII digits.
    """
    s = "" if raw is None else str(raw).strip()
    if not s or not set(s) <= _DIGITS:
        return 0
    return int(s)


def _usable_ts_ms(raw: Any, now_ms: int) -> int:
    """`_signal_ts_ms`, also 0 for a time more than FUTURE_TOLERANCE_SECONDS ahead of now."""
    ts = _signal_ts_ms(raw)
    if ts > now_ms + FUTURE_TOLERANCE_SECONDS * 1000:
        return 0
    return ts


def _now() -> float:
    """The handler's clock, in epoch seconds (one seam for tests)."""
    return time.time()


def _history_end_ms(field: Any) -> Optional[int]:
    """When a snapshot field's history value ends, in epoch ms.

    - no field, or a field without ``asOf``: 0 (no history value; a timed reading wins)
    - ``asOf`` ``YYYY-MM-DD``: the last millisecond of that UTC day (the day's miles are
      in the history value, so a reading from that day loses)
    - ``asOf`` an ISO-8601 time with a zone: that instant
    - anything else: None (can't compare; the reading is withheld)
    """
    if not isinstance(field, dict):
        return 0
    asof = field.get("asOf")
    if asof is None or (isinstance(asof, str) and not asof.strip()):
        return 0
    s = str(asof).strip()
    try:
        if len(s) == 10:
            day = datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            return int(day.timestamp() * 1000) + 86_400_000 - 1
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        return None
    return int(dt.timestamp() * 1000)


def _is_electric(row: Dict[str, Any]) -> bool:
    """Same rule as CMS vehicle_values._is_electric: the powertrain fact, then fuelType."""
    facts = row.get("startingFacts")
    if isinstance(facts, dict) and facts.get("powertrain") is not None:
        return facts.get("powertrain") == "electric"
    return row.get("fuelType") == "electric"


def _load_snapshot(vehicle_id: str):
    """(item, status) from the snapshot table; status 'ok' | 'none' | 'error'.

    Not configured counts as an error: without the history value no reading can be
    shown to be newer, so odometer and charge are withheld rather than guessed.
    """
    if _snapshot_table is None:
        logger.warning("CMS_VEHICLE_SNAPSHOT_TABLE not set; live odometer/charge withheld")
        return None, "error"
    try:
        item = _snapshot_table.get_item(Key={"vehicleId": vehicle_id}).get("Item")
    except Exception as exc:  # noqa: BLE001 — any read failure withholds both readings
        logger.warning("snapshot get_item failed for %s: %s", vehicle_id, type(exc).__name__)
        return None, "error"
    return (item, "ok") if item else (None, "none")


def handler(event: Dict[str, Any], _context: Any) -> Dict[str, Any]:
    vehicle_id = (event.get("pathParameters") or {}).get("vehicleId")
    if not vehicle_id:
        return _resp(400, {"error": "vehicleId required"})

    now = _now()
    now_ms = int(now * 1000)
    ddb_row: Dict[str, Any] = {}
    try:
        result = _vehicles_table.get_item(Key={"vehicleId": vehicle_id})
        ddb_row = result.get("Item") or {}
    except Exception as exc:
        logger.warning("DDB get_item failed for %s: %s", vehicle_id, exc)

    ddb_conn_status = (ddb_row.get("connectionStatus") or "disconnected").lower()
    ddb_last_seen = ddb_row.get("lastSeenAt")
    ddb_last_seen_epoch = _parse_ts_to_epoch_seconds(ddb_last_seen)

    # Redis meta hash: read for the connection fields only (lastConnectedAt, lastSeenAt).
    # Its other keys carry no time of their own, so no value is served from it
    # (issues/2026-11-07-live-state-stale-values/).
    redis_meta: Dict[str, Any] = {}
    redis_signals: Dict[str, Any] = {}
    redis_timestamps: Dict[str, Any] = {}
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
        # Each signal's time, written by Flink beside its value (same signal-id keys).
        # Odometer and charge are returned only when their time beats the history
        # value (`_newer_than_history`); without this hash they are withheld.
        try:
            raw = r.hgetall(f"vehicle:{vehicle_id}:timestamps")
            redis_timestamps = dict(raw or {})
        except Exception as exc:
            logger.warning("Redis timestamps hgetall failed for %s: %s",
                           vehicle_id, type(exc).__name__)

    # Apply the live window (LIVE_WINDOW_SECONDS, 120 s).
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

    # -----------------------------------------------------------------
    # Resolve the signals hash (numeric signal_id → named value) via
    # the signal catalog cached in Redis at `signal_catalog:reverse`.
    # The catalog is seeded at deploy time; each entry is a pipe-
    # delimited "name|vssPath|unit|dataType" string. We only need
    # `name` here. If the catalog lookup fails no signal is named, so every
    # signals-hash value is null and the position falls back to the vehicles
    # row (labelled "ddb"); the response is still a 200. Added 2026-05-05.
    # -----------------------------------------------------------------
    sig_by_name: Dict[str, str] = {}
    sig_ts_by_name: Dict[str, int] = {}
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
                    sig_ts_by_name[name] = _usable_ts_ms(redis_timestamps.get(sig_id_str), now_ms)
        except Exception as exc:
            logger.warning("signal catalog reverse lookup failed: %s", exc)

    # Name-aliasing: CMS aliases a handful of semantic synonyms into
    # the canonical UI fields. Mirror those so iOS never shows a
    # different number than CMS for the same vehicle.
    # Odometer and charge are not here: they use CMS's own signal lists
    # (_ODOMETER_SIGNALS, _EV_CHARGE_SIGNALS, _FUEL_SIGNALS) and its precedence rule.
    aliases = {
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
        # No Flink writer produces this today (it was read from the meta hash and the
        # vehicles row, neither of which carries a time); kept as a signal lookup so a
        # future timed reading is served under the same rule as the others.
        "batteryLevel":   ["batteryLevel", "battery_level"],
    }

    def _timed_reading(signals, max_abs: Optional[float] = None) -> Optional[tuple]:
        """(value, ts_ms) of the newest timed Redis signal among `signals`.

        Value and time come from the same signal. Ties keep list order. A signal with no
        usable time is not a reading: it can't be shown to be newer than anything. A
        non-finite value, or one with `abs(value) > max_abs`, is not a reading either
        (NaN would make the body invalid JSON and fail the whole iOS decode).

        C2 (review cycle 1, decisions.md 2026-10-24 ~01:25Z) still holds: never the
        vehicles row, whose undated seeded value (39,840 mi) would disagree with every
        other surface. The Redis meta hash is not read for these fields either: Flink
        writes only connection fields there, and a meta value carries no time of its own.
        """
        best: Optional[tuple] = None
        for candidate in signals:
            raw = sig_by_name.get(candidate)
            if raw in ("", None):
                continue
            try:
                value = float(raw)
            except (TypeError, ValueError):
                continue
            if not math.isfinite(value):
                continue
            if max_abs is not None and abs(value) > max_abs:
                continue
            ts = sig_ts_by_name.get(candidate, 0)
            if ts <= 0:
                continue
            if best is None or ts > best[1]:
                best = (value, ts)
        return best

    def _within(field: str, reading: Optional[tuple], max_age_seconds: int) -> Optional[tuple]:
        """The reading if its own time is at most `max_age_seconds` old, else None."""
        if reading is None:
            return None
        age_ms = now_ms - reading[1]
        if age_ms > max_age_seconds * 1000:
            logger.info("live-state %s: newest Redis reading is %d s old (limit %d s); "
                        "withheld for %s", field, age_ms // 1000, max_age_seconds, vehicle_id)
            return None
        return reading

    def _current(field: str) -> Optional[float]:
        """A current-state value: the newest timed reading, if it is CURRENT_READING_MAX_AGE_SECONDS
        old or less; else None. Never the meta hash or the vehicles row (iOS falls back to
        /drivers/me's row values itself)."""
        reading = _within(field, _timed_reading(aliases[field]), CURRENT_READING_MAX_AGE_SECONDS)
        return None if reading is None else reading[0]

    snapshot_cache: Dict[str, Any] = {}

    def _snapshot() -> tuple:
        # Read at most once, and only when a timed reading needs comparing.
        if "status" not in snapshot_cache:
            snapshot_cache["item"], snapshot_cache["status"] = _load_snapshot(vehicle_id)
        return snapshot_cache["item"], snapshot_cache["status"]

    def _newer_than_history(field: str, reading: Optional[tuple],
                            snapshot_field: Optional[str]) -> Optional[float]:
        """The reading's value if it beats the history value, else None.

        `snapshot_field` None means the car's history has no such value (a fuel car's
        fuel level, CMS W3): a timed reading wins without reading the snapshot.
        """
        if reading is None:
            return None
        value, ts = reading
        if snapshot_field is None:
            return value
        item, status = _snapshot()
        if status == "error":
            return None
        if item is None:
            return value
        end_ms = _history_end_ms(item.get(snapshot_field))
        if end_ms is None:
            logger.warning("snapshot %s.asOf unreadable for %s; live reading withheld",
                           snapshot_field, vehicle_id)
            return None
        if ts > end_ms:
            return value
        logger.info("live-state %s: Redis reading at %d is not newer than history (%s); "
                    "withheld for %s", field, ts, snapshot_field, vehicle_id)
        return None

    def _charge() -> Optional[float]:
        """Charge as CMS picks it (`_live_charge_ts` / `_live_fuel_level_direct_ts`)."""
        if not ddb_row:
            # No vehicles row (read failed or missing): the powertrain is unknown, so it
            # is unknown which signals and which history value apply. Withheld.
            return None
        if _is_electric(ddb_row):
            reading = _timed_reading(_EV_CHARGE_SIGNALS) or _timed_reading(_FUEL_SIGNALS)
            return _newer_than_history("charge", reading, "batteryCharge")
        return _newer_than_history("fuelLevel", _timed_reading(_FUEL_SIGNALS), None)

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
    # Each Redis axis's own fix time (epoch ms), for locationAgeSeconds below.
    _fix_ts_ms: Dict[str, int] = {}

    def _live_location(field: str, ddb_fallback_key: str) -> Optional[float]:
        """One axis of the position: the newest timed Redis fix, else the vehicles row.

        The Redis fix counts when its own time is at most LAST_KNOWN_MAX_AGE_SECONDS old
        (a last-known value; see the constant) and its value is finite and on the globe
        (|lat| <= 90, |lng| <= 180). Otherwise this axis falls through to the DDB
        fallback key (`lastLatitude`/`lastLongitude`), labelled "ddb", exactly as when
        Redis has no fix at all. Fallback is intentionally last so a Redis fix always
        wins over the potentially-stale DDB row.

        F7.1: records per-axis provenance (_lat_source / _lon_source) so
        locationSource can be set to "redis" only when BOTH axes came from
        Redis signals, not just the first-resolving axis.
        """
        nonlocal _lat_source, _lon_source
        fix = _within(field, _timed_reading(aliases[field],
                                            max_abs=90.0 if field == "latitude" else 180.0),
                      LAST_KNOWN_MAX_AGE_SECONDS)
        if fix is not None:
            if field == "latitude":
                _lat_source = "redis"
            else:
                _lon_source = "redis"
            _fix_ts_ms[field] = fix[1]
            return fix[0]
        raw = ddb_row.get(ddb_fallback_key)
        if raw is None or raw == "":
            return None
        try:
            val = float(raw)
        except (TypeError, ValueError):
            return None
        # Same validity as a Redis fix: a string "nan"/"inf" or an off-globe value on the
        # row would otherwise put NaN in the body (invalid JSON) or a non-position on the map.
        if not math.isfinite(val) or abs(val) > (90.0 if field == "latitude" else 180.0):
            return None
        if field == "latitude":
            _lat_source = "ddb"
        else:
            _lon_source = "ddb"
        return val

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

    # How long ago the position fix was stored, in whole seconds rounded up, from the
    # OLDER of the two axes' times (issues/2026-11-07-escalate-gate-uses-connection-age-not-fix-age/).
    # Those times are CMS Flink's processing clock when it stored each value
    # (EventDrivenTelemetryProcessor), not the car's sample time: a fix the car spooled
    # (FleetWise store-and-forward) or a Kinesis backlog replay reads as new. It is younger
    # than the sample age by the delivery delay (seconds on time, unbounded when late) and at
    # most 1 s older from rounding up; never the connection's age.
    # `lastSeenAgoSeconds` is the connection's age: CMS rewrites meta `lastConnectedAt` on
    # every FleetWise check-in, which carries no position, so a car that checks in but sends
    # no fix looks fresh there with an old fix. /escalate gates dispatch on this field.
    # Set only when the pair came from Redis (locationSource "redis"); null otherwise, and
    # the vehicles-row fallback has no time of its own. Rounded up so a fix one millisecond
    # past a whole-second limit is past it here too; a time within FUTURE_TOLERANCE_SECONDS
    # ahead (accepted as usable above) reads 0, not a negative age.
    location_age_seconds: Optional[int] = None
    if _location_source == "redis":
        oldest_ms = min(_fix_ts_ms["latitude"], _fix_ts_ms["longitude"])
        location_age_seconds = max(0, -(-(now_ms - oldest_ms) // 1000))

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
        # Odometer and charge: a Redis reading only when newer than the history value
        # (issues/2026-11-06-live-state-stale-odometer/); null sends iOS to /drivers/me.
        "fuelLevel": _charge(),
        # Current-state values: a Redis reading at most CURRENT_READING_MAX_AGE_SECONDS
        # old, else null (issues/2026-11-07-live-state-stale-values/). iOS Home then
        # shows /drivers/me's row value (speed: `lastSpeed`).
        "batteryLevel": _current("batteryLevel"),
        "speed": _current("speed"),
        "engineTemp": _current("engineTemp"),
        "odometer": _newer_than_history("odometer", _timed_reading(_ODOMETER_SIGNALS), "odometer"),
        "batteryVoltage": _current("batteryVoltage"),
        # Location fields: the Redis fix while it is a last-known value
        # (LAST_KNOWN_MAX_AGE_SECONDS), else the vehicles-row fallback, labelled by
        # locationSource. Parity with CMS's `currentLocation`
        # (which reads the same sig_by_name["lat"]/["lng"] values).
        # Added 2026-05-06 to fix the iOS Vehicle tab map displaying
        # stale DDB coordinates.
        "latitude": lat_val,
        "longitude": lon_val,
        "heading": _current("heading"),
        # F7.1 / F6.1: Provenance field — "redis" / "ddb" / None.
        # "redis" means BOTH axes came from live signal aliases (telemetry-backed).
        # "ddb" means either axis fell through to lastLatitude/lastLongitude (potentially stale).
        # None means neither source had a coordinate.
        # The escalate gate requires "redis" here and a fresh locationAgeSeconds.
        # `redisReachable` is client connectivity, not coordinate provenance.
        "locationSource": _location_source,
        # Seconds since the fix was stored (see location_age_seconds above); null unless "redis".
        "locationAgeSeconds": location_age_seconds,
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
