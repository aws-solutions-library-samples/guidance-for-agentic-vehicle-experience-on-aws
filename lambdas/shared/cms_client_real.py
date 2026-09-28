"""Real CMS client implementation.

Reads:
  - cms-prod-storage-vehicles (DynamoDB, PK=vehicleId) → VIN → vehicleId resolution via scan+filter
  - cms-prod-storage-dtc-history (DynamoDB, PK=vehicleId, SK=timestamp) → active DTCs
    (status is the source of truth; see get_active_dtcs)
  - vehicle:{vehicleId}:signals + :timestamps + :meta (Redis) → live telemetry
  - signal_catalog:map (Redis) → signal name → signal id
  - recall-integration (HTTP) → open recalls per VIN

Activated via env var VSA_CMS_CLIENT=real. Required env vars:
  CMS_REDIS_ENDPOINT        e.g., cms-prod.abc123.use1.cache.amazonaws.com
  CMS_REDIS_PORT            default 6379
  CMS_REDIS_TLS             "true" | "false", default "true"
  CMS_VEHICLES_TABLE        default "cms-prod-storage-vehicles"
  CMS_DTC_HISTORY_TABLE     default "cms-prod-storage-dtc-history"
  CMS_RECALL_API_URL        optional; if unset, recalls return ()
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any

import boto3
import redis
import urllib.request

from triage_classifier.types import ActiveDtc, OpenRecall, TelemetrySnapshot

_log = logging.getLogger(__name__)

_SIGNAL_NAMES = (
    "speed", "engineRPM", "coolant_temp", "ignitionOn",
    "lat", "lng", "heading",
    "tire_fl", "tire_fr", "tire_rl", "tire_rr",
    "brake_wear", "fuel_level", "battery_soc",
    "oil_pressure", "oil_temp", "abs_status",
    "odometer",
)
_STALE_AFTER_MS = 5 * 60 * 1000


def _f_to_c(f: float) -> float:
    return (f - 32.0) * 5.0 / 9.0


class RealCmsClient:
    """Protocol-compatible CMS client backed by real AWS + Redis resources.

    All external handles are lazy so unit tests can inject a fakeredis client.
    """

    def __init__(
        self,
        redis_client: redis.Redis | None = None,
        ddb_resource: Any | None = None,
    ) -> None:
        self._redis = redis_client
        self._ddb = ddb_resource
        self._signal_ids: dict[str, int] | None = None
        # Cached VIN → vehicleId lookups (see _resolve_vehicle_id).
        self._vin_to_vehicle_id: dict[str, str] | None = None
        self._vehicles_table = os.environ.get("CMS_VEHICLES_TABLE", f"cms-{os.environ.get('CMS_STAGE', 'dev')}-storage-vehicles")
        self._dtc_table = os.environ.get("CMS_DTC_HISTORY_TABLE", f"cms-{os.environ.get('CMS_STAGE', 'dev')}-storage-dtc-history")
        self._recall_url = os.environ.get("CMS_RECALL_API_URL")
        # Outcome of the most recent get_active_dtcs call, so the triage handler can
        # record a degraded read instead of storing an ordinary "no faults" decision.
        # {"status": "complete" | "partial" | "failed" | "vehicle_lookup_failed"
        #  | "vehicle_unresolved", "pages": int, "rows": int}. None until the first call.
        self.last_dtc_read: dict[str, Any] | None = None
        # Set by each _resolve_vehicle_id call; see there.
        self._vehicle_lookup_failed = False

    # ---- lazy handles ----------------------------------------------------

    def _get_redis(self) -> redis.Redis | None:
        """Lazy Redis handle. Returns None when CMS_REDIS_ENDPOINT is unset so
        callers can treat 'no Redis' as a soft failure (stale telemetry) rather
        than an exception. This makes the classifier runnable from a Lambda
        that hasn't been placed in the CMS VPC — triage still works on DTCs
        and recalls, telemetry just comes back stale."""
        if self._redis is None:
            endpoint = os.environ.get("CMS_REDIS_ENDPOINT")
            if not endpoint:
                return None
            self._redis = redis.Redis(
                host=endpoint,
                port=int(os.environ.get("CMS_REDIS_PORT", "6379")),
                ssl=os.environ.get("CMS_REDIS_TLS", "true").lower() == "true",
                socket_timeout=2.0, socket_connect_timeout=2.0,
                decode_responses=True,
            )
        return self._redis

    def _get_ddb(self):
        if self._ddb is None:
            self._ddb = boto3.resource(
                "dynamodb", region_name=os.environ.get("AWS_REGION", "us-east-1")
            )
        return self._ddb

    def _load_catalog(self) -> dict[str, int]:
        """Signal name → signal id. Cached for Lambda container lifetime.
        Returns empty dict when Redis is unavailable (treat as 'no catalog';
        telemetry reads then find no signal mappings and return None for each)."""
        if self._signal_ids is None:
            r = self._get_redis()
            if r is None:
                self._signal_ids = {}
                return self._signal_ids
            try:
                raw = r.hgetall("signal_catalog:map") or {}
                self._signal_ids = {name: int(sid) for name, sid in raw.items()}
            except Exception as e:  # noqa: BLE001
                _log.warning("signal catalog load failed: %s", e)
                self._signal_ids = {}
        return self._signal_ids

    # ---- VIN → vehicleId -------------------------------------------------

    def _resolve_vehicle_id(self, vin: str) -> str | None:
        """Resolve a VIN to the CMS vehicleId primary key.

        The CMS ``vehicles`` table is keyed by ``vehicleId`` (e.g. VEH-0025)
        rather than VIN, so we can't ``get_item`` directly. Instead we scan
        once with a VIN filter and cache the result for this client
        instance (Lambda container lifetime). The fleet is ~50 vehicles
        so the scan is cheap; a VIN GSI would be the right long-term fix.
        """
        if self._vin_to_vehicle_id is None:
            self._vin_to_vehicle_id = {}
        # True only when THIS call ended because a read raised, not because the VIN
        # is absent. get_active_dtcs uses it to record `vehicle_lookup_failed`
        # instead of `vehicle_unresolved` (review a137e13 cycle 2, W5): an access
        # outage on the vehicles table must not read as "this VIN is not in CMS".
        self._vehicle_lookup_failed = False

        cached = self._vin_to_vehicle_id.get(vin)
        if cached is not None:
            return cached

        t = self._get_ddb().Table(self._vehicles_table)
        # Prefer the `vin-index` GSI (KEYS_ONLY projection of HASH vin)
        # for a ~20ms query instead of a full-table scan. The old scan
        # path (Limit=2 filtered after read, with bug; then paginated
        # full scan, correct but 300–500ms) is kept as a fallback so
        # the classifier stays resilient if the GSI is mid-provisioning
        # or absent in a fresh environment.
        try:
            resp = t.query(
                IndexName="vin-index",
                KeyConditionExpression="vin = :v",
                ExpressionAttributeValues={":v": vin},
                ProjectionExpression="vehicleId, vin",
                Limit=1,
            )
            items = resp.get("Items") or []
            if items:
                vehicle_id = items[0].get("vehicleId")
                if vehicle_id:
                    self._vin_to_vehicle_id[vin] = vehicle_id
                    return vehicle_id
            # Empty response from the GSI is authoritative — VIN not
            # in the index = not in the table. No need to fall back.
            _log.warning("vehicle record not found for vin=%s (via GSI)", vin)
            return None
        except Exception as e:  # noqa: BLE001
            # GSI probably not deployed yet. Fall through to the scan
            # so the classifier stays functional — logged at warning
            # level because this is the slow path.
            _log.warning(
                "vin-index query failed for vin=%s (%s) — falling back to scan",
                vin, type(e).__name__,
            )

        try:
            items: list[dict] = []
            start_key: dict | None = None
            while True:
                kwargs = {
                    "FilterExpression": "vin = :v",
                    "ExpressionAttributeValues": {":v": vin},
                    "ProjectionExpression": "vehicleId, vin",
                }
                if start_key:
                    kwargs["ExclusiveStartKey"] = start_key
                resp = t.scan(**kwargs)
                items.extend(resp.get("Items") or [])
                if items:
                    break
                start_key = resp.get("LastEvaluatedKey")
                if not start_key:
                    break
        except Exception as e:  # noqa: BLE001 - we never want to break triage on this
            _log.warning("vehicle lookup failed for vin=%s: %s", vin, e)
            self._vehicle_lookup_failed = True
            return None
        if not items:
            _log.warning("vehicle record not found for vin=%s", vin)
            return None

        vehicle_id = items[0].get("vehicleId")
        if vehicle_id:
            self._vin_to_vehicle_id[vin] = vehicle_id
        return vehicle_id

    # ---- Protocol methods -----------------------------------------------

    def get_telemetry(self, vin: str) -> TelemetrySnapshot:
        vehicle_id = self._resolve_vehicle_id(vin)
        if not vehicle_id:
            return TelemetrySnapshot(stale=True)

        r = self._get_redis()
        if r is None:
            # No Redis endpoint configured — return stale so triage ignores telemetry
            # and relies on DTCs + recalls. This is the expected path when the
            # classifier Lambda runs outside the CMS VPC.
            return TelemetrySnapshot(stale=True)

        signals_key = f"vehicle:{vehicle_id}:signals"
        meta_key = f"vehicle:{vehicle_id}:meta"
        try:
            pipe = r.pipeline()
            pipe.hgetall(signals_key)
            pipe.hgetall(meta_key)
            signals_raw, meta_raw = pipe.execute()
        except Exception as e:  # noqa: BLE001 - Redis timeout / network must not break triage
            _log.warning("redis telemetry read failed for vehicle_id=%s: %s", vehicle_id, e)
            return TelemetrySnapshot(stale=True)

        if not signals_raw or not meta_raw:
            return TelemetrySnapshot(stale=True)

        last_seen_ms = int(meta_raw.get("lastSeenAt") or 0)
        now_ms = int(time.time() * 1000)
        stale = last_seen_ms == 0 or (now_ms - last_seen_ms) > _STALE_AFTER_MS

        catalog = self._load_catalog()

        def _get(name: str, cast=float):
            sid = catalog.get(name)
            if sid is None:
                return None
            raw = signals_raw.get(str(sid))
            if raw is None or raw == "":
                return None
            try:
                if cast is bool:
                    return raw.lower() == "true"
                return cast(raw)
            except (ValueError, TypeError):
                return None

        coolant_f = _get("coolant_temp")
        oil_f = _get("oil_temp")
        abs_raw = _get("abs_status", str)
        abs_ok: bool | None = None
        if abs_raw is not None:
            abs_ok = abs_raw.lower() not in ("0", "false", "fault", "disabled")

        return TelemetrySnapshot(
            coolant_temp_c=_f_to_c(coolant_f) if coolant_f is not None else None,
            tire_pressure_fl_psi=_get("tire_fl"),
            tire_pressure_fr_psi=_get("tire_fr"),
            tire_pressure_rl_psi=_get("tire_rl"),
            tire_pressure_rr_psi=_get("tire_rr"),
            tire_spec_psi=34.0,
            brake_pad_wear_mm_min=_get("brake_wear"),
            battery_soc_pct=_get("battery_soc"),
            oil_pressure_kpa=_get("oil_pressure"),
            oil_temp_c=_f_to_c(oil_f) if oil_f is not None else None,
            speed_mph=_get("speed"),
            abs_status_ok=abs_ok,
            last_update=str(last_seen_ms) if last_seen_ms else None,
            stale=stale,
        )

    def get_active_dtcs(self, vin: str) -> tuple[ActiveDtc, ...]:
        """Fetch every currently-active DTC for a VIN.

        CMS schema: ``cms-{stage}-storage-dtc-history`` is keyed by
        ``vehicleId`` (HASH) + ``timestamp`` (RANGE). ``timestamp`` is when
        the fault was FIRST raised; a fault that stays active keeps its row
        and only ``lastSeenAt`` advances. So row age says nothing about
        whether the fault is active now. Status does.

        This previously bounded the query to ``timestamp >= now - 24h``,
        which silently dropped every fault first raised more than a day
        ago, including an active CRITICAL P0217, so triage returned P3.
        See issues/2026-09-25-triage-drops-active-dtcs-first-seen-over-24h/.

        "Active" matches what the iOS app shows
        (``lambdas/api-vehicle-context/handler.py`` ``_load_active_dtcs``):
        drop rows whose status is CLEARED or RESOLVED, and rows carrying a
        ``clearedDate`` (some producers set it and leave status ACTIVE).
        The legacy ``resetAt`` field is also honored. Rows are read
        newest-first; the first active row per code wins.

        Reads the whole partition, following ``LastEvaluatedKey``. A failure
        on a later page keeps the rows already read: for a safety
        classifier, a partial list is strictly better than none. The return
        type stays a plain tuple (the Tier 2 diagnostics source depends on
        it); the read outcome goes to ``self.last_dtc_read`` instead:

        * ``complete`` - every page was read.
        * ``partial`` - a later page failed. Rows are newest-first, so the
          rows lost are the oldest, longest-running faults.
        * ``failed`` - the first page failed; the result is ``()``.
        * ``vehicle_lookup_failed`` - resolving the VIN to a vehicleId raised
          (IAM, KMS, throttling, a wrong table name); the result is ``()``.
          An access outage fails here first, so it must not look like a
          missing vehicle.
        * ``vehicle_unresolved`` - the VIN is not in the vehicles table; the
          result is ``()``.

        The triage handler stores this in the decision's ``inputsSnapshot``
        so a degraded read is never recorded as "no faults". See review W3 in
        the issue directory above.
        """
        vehicle_id = self._resolve_vehicle_id(vin)
        if not vehicle_id:
            status = ("vehicle_lookup_failed" if self._vehicle_lookup_failed
                      else "vehicle_unresolved")
            self.last_dtc_read = {"status": status, "pages": 0, "rows": 0}
            return ()

        t = self._get_ddb().Table(self._dtc_table)
        items: list[dict[str, Any]] = []
        start_key: dict[str, Any] | None = None
        pages = 0
        read_status = "complete"
        while True:
            kwargs: dict[str, Any] = {
                "KeyConditionExpression": "vehicleId = :v",
                "ExpressionAttributeValues": {":v": vehicle_id},
                "ScanIndexForward": False,  # newest first
            }
            if start_key:
                kwargs["ExclusiveStartKey"] = start_key
            try:
                resp = t.query(**kwargs)
            except Exception as e:  # noqa: BLE001 - failing DTC lookup must not block triage
                read_status = "partial" if pages else "failed"
                _log.warning(
                    "dtc-history query failed for vin=%s (vehicleId=%s) after %d page(s), "
                    "%d rows; read=%s: %s",
                    vin, vehicle_id, pages, len(items), read_status, e,
                )
                break
            pages += 1
            items.extend(resp.get("Items") or [])
            start_key = resp.get("LastEvaluatedKey")
            if not start_key:
                break
        self.last_dtc_read = {"status": read_status, "pages": pages, "rows": len(items)}

        latest: dict[str, dict[str, Any]] = {}
        for item in items:
            status = (item.get("status") or "").upper()
            if status in ("CLEARED", "RESOLVED"):
                continue
            if item.get("clearedDate") or item.get("resetAt"):
                continue
            code = item.get("code")
            if not code:
                continue
            if code not in latest:
                latest[code] = item
        return tuple(
            ActiveDtc(
                code=code,
                first_seen_at=str(item.get("firstSeenAt") or item.get("timestamp")),
                persistent=bool(item.get("persistent", True)),
            )
            for code, item in latest.items()
        )

    def get_open_recalls(self, vin: str) -> tuple[OpenRecall, ...]:
        if not self._recall_url:
            return ()
        try:
            req = urllib.request.Request(
                f"{self._recall_url.rstrip('/')}?vin={vin}&status=open",
                headers={"Accept": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=2) as resp:  # noqa: S310 - internal service
                import json as _json
                data = _json.loads(resp.read())
        except Exception as e:  # noqa: BLE001 - failing recall lookup must not block triage
            _log.warning("recall lookup failed for vin=%s: %s", vin, e)
            return ()
        return tuple(
            OpenRecall(
                nhtsa_id=r["nhtsaId"],
                affected_systems=tuple(r.get("affectedSystems") or ()),
                remedy_available=bool(r.get("remedyAvailable", True)),
            )
            for r in data.get("recalls", [])
        )
