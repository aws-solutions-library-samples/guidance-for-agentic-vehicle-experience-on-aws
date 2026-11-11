"""
GET /vehicles/{vehicleId}/context

Returns vehicle + assigned-driver context in a single call. Used by iOS
at voice-session open to render the "nameplate" (e.g. "Stephanie Johnson
· 2022 Chevrolet Equinox") and by the supervisor to enrich the voice
prompt with real make/model/odometer/driver name.

Response shape:
    {
      "vehicleId": "VEH-0025",
      "vehicle": {
        "vehicleId": ..., "vin": ..., "make": ..., "model": ...,
        "year": ..., "odometer": ..., "fleetId": ..., "color": ...,
        "licensePlate": ..., "lastLatitude": ..., "lastLongitude": ...,
        "fuelLevel": ..., "engineTemp": ..., "batteryVoltage": ...
      },
      "driver": {
        "driverId": ..., "firstName": ..., "lastName": ...,
        "email": ..., "phone": ..., "homeBase": ...,
        "safetyScore": ..., "licenseClass": ..., "licenseState": ...
      } or null,
      "activeDtcs": [ { "code": "P0299", "severity": "HIGH", ... }, ... ],
      "healthScore": 58,
      "healthScoreBreakdown": {
        "score": 58,
        "deductions": [
          {"reason": "DTC P0299 HIGH", "amount": 15},
          {"reason": "Scheduled service overdue", "amount": 10},
          {"reason": "Vehicle disconnected", "amount": 5}
        ],
        "computedAt": "2026-05-19T17:48:51.123Z"
      },
      "generatedAt": "..."
    }

If the dtc-history read fails, the route answers **503**
`{"error": ..., "dtcLookup": "failed", "vehicleId": ...}` with no `activeDtcs` and no health
score. It used to answer 200 with `activeDtcs: []`, which every client reads as "no active
faults". iOS decodes `activeDtcs` as optional and treats null as empty, so a non-2xx is the only
shape it shows as "Couldn't load" (issues/2026-09-27-vehicle-context-dtc-lookup-failure-returns-empty/).

Who may call it (`context_scope.py`; issues/2026-10-24-vehicle-context-driver-pii-unscoped/):
a `platform-admin`; the vehicle's driver (a vehicle-standing group plus a trusted
`custom:vehicleId` equal to the path id); or a `fleet-operator` whose trusted
`custom:fleetIds` holds the vehicle's `fleetId`. Anyone else gets **403**
`{"error": "vehicle not in caller scope"}` and nothing is read for them; a request with no
claims gets **401**. A fleet operator gets the same 403 for a vehicle in another fleet and for
a vehicle id that doesn't exist, so the answer doesn't show which ids exist.

Driver is nil when no driver is assigned or we can't find one. The
vehicle record carries an `assignedDriverId` field in some schemas;
we also fall back to scanning drivers for assignedVehicleId = this
vehicleId (the pattern used by the CMS seed scripts).

healthScore is the single source of truth for the iOS Home tab, which does
not recompute it. The breakdown's `deductions` list uses stable reason
strings so a client can render them verbatim without locale-specific
reformatting in v1. (The CMS UI Vehicle Detail page stopped calling this
route on 2026-09-18; it reads CMS's own `/api/v1/vehicles/{id}/health`.)
"""
from __future__ import annotations

import json
import logging
import math
import os
from decimal import Decimal
from typing import Any

import boto3
from boto3.dynamodb.conditions import Attr

# `context_scope.py` sits beside this file and ships in the same asset. Tests load this
# file by path, so its directory may not be on sys.path yet; append (not insert) so it
# never shadows another Lambda's modules in a shared test process.
import sys  # noqa: E402

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.append(_HERE)
import context_scope  # noqa: E402

log = logging.getLogger()
log.setLevel(logging.INFO)

_VEHICLES_TABLE = os.environ.get("CMS_VEHICLES_TABLE", f"cms-{os.environ.get('CMS_STAGE', 'dev')}-storage-vehicles")
_DRIVERS_TABLE = os.environ.get("CMS_DRIVERS_TABLE", f"cms-{os.environ.get('CMS_STAGE', 'dev')}-storage-drivers")
_DTC_HISTORY_TABLE = os.environ.get(
    "CMS_DTC_HISTORY_TABLE",
    f"cms-{os.environ.get('CMS_STAGE', 'dev')}-storage-dtc-history",
)
_SERVICE_HISTORY_TABLE = os.environ.get(
    "CMS_SERVICE_HISTORY_TABLE",
    f"cms-{os.environ.get('CMS_STAGE', 'dev')}-storage-service-history",
)
# Stage C: vehicle snapshot table (pk: vehicleId, one item per car, written daily).
_SNAPSHOT_TABLE = os.environ.get(
    "CMS_VEHICLE_SNAPSHOT_TABLE",
    f"cms-{os.environ.get('CMS_STAGE', 'dev')}-storage-vehicle-snapshot",
)
# Name of the api-vehicle-live-state Lambda. When set, the context
# handler invokes it to get a Redis-backed canonical connectionStatus
# (with the same 2-min live-window logic CMS UI + iOS already use)
# instead of relying on the lagging DDB `connectionStatus` field. When
# unset, falls back to the DDB field — a graceful degradation path
# for local development.
_LIVE_STATE_FN_NAME = os.environ.get("LIVE_STATE_FN_NAME", "")

#: dtc-history paging. Page size keeps each Query small; the page cap bounds the
#: loop inside the 10 s Lambda timeout. 50 x 100 = 5,000 rows; the busiest staging
#: vehicle had 58 on 2026-10-01. Exceeding the cap is a 503, never a partial list.
_DTC_PAGE_SIZE = 100
_DTC_MAX_PAGES = 50

_ddb = None
_lambda_client = None


def _get_lambda():
    global _lambda_client
    if _lambda_client is None:
        _lambda_client = boto3.client("lambda")
    return _lambda_client


def _get_ddb():
    global _ddb
    if _ddb is None:
        _ddb = boto3.resource("dynamodb")
    return _ddb


class _DecimalEncoder(json.JSONEncoder):
    def default(self, o):
        if isinstance(o, Decimal):
            return int(o) if o % 1 == 0 else float(o)
        return super().default(o)


def _respond(status: int, body: dict | str) -> dict[str, Any]:
    payload = body if isinstance(body, str) else json.dumps(body, cls=_DecimalEncoder)
    return {
        "statusCode": status,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": os.environ.get("ALLOWED_ORIGIN", "*"),
            "Access-Control-Allow-Headers": "Content-Type,Authorization",
        },
        "body": payload,
    }


def _refuse(scope: "context_scope.Scope", vehicle_id: str, claims: dict | None) -> dict[str, Any]:
    """401 for a request with no caller claims, else 403. The body names no vehicle data,
    and nothing about the vehicle has been read for this caller beyond, for a fleet
    operator, the row whose ``fleetId`` it failed to match."""
    sub = claims.get("sub") if isinstance(claims, dict) else None
    log.warning(
        "vehicle-context: refused standing=%s sub=%.64r vehicleId=%.80r",
        scope.standing, sub, vehicle_id,
    )
    if scope.standing == context_scope.UNAUTHENTICATED:
        return _respond(401, {"error": "no caller claims"})
    return _respond(403, {"error": "vehicle not in caller scope"})


def _load_vehicle(vehicle_id: str) -> dict | None:
    """Read the vehicle row. Strips internal/attributes subfield to keep the
    API response clean; iOS only needs the primary flat fields."""
    table = _get_ddb().Table(_VEHICLES_TABLE)
    resp = table.get_item(Key={"vehicleId": vehicle_id})
    item = resp.get("Item")
    if not item:
        return None
    # Keep a curated subset — the full row has 40+ fields and many are
    # internal (createdAt, updatedAt, attributes map, enrollment codes).
    return {
        k: item.get(k)
        for k in (
            "vehicleId", "vin", "make", "model", "year", "color",
            "licensePlate", "vehicleType", "fuelType",
            "odometer", "mileage", "fuelLevel",
            "engineTemp", "batteryVoltage", "lastSpeed",
            "lastLatitude", "lastLongitude", "lastSeenAt",
            "fleetId", "status", "connectionStatus",
            "name",
            # Added 2026-07-29. iOS `VehicleInfo` has declared these all along,
            # but they were absent from this curated allowlist, so they arrived
            # nil and the trade-in condition score could only compute its
            # vehicle-health factor — distance-for-age and usage-pattern
            # silently dropped out. Same class as the tenant-config `acquire`
            # omission: a hand-maintained projection drifting behind the client
            # that consumes it, failing silently because absent optionals decode
            # cleanly.
            "totalTrips", "purchaseDate", "purchasePrice",
            "enrollmentStatus",
            # Added 2026-08-19 for the EV battery card. `batterySoh` is state of
            # HEALTH — remaining capacity against original — which is a different
            # signal from state of charge and had no channel before this.
            # `batteryCapacityKwh` was already on the row but unprojected, so the
            # client could show a percentage and never the kWh behind it.
            #
            # Note `fuelLevel` above already carries SoC for a BEV (the backend
            # aliases the Redis `ev_soc` signal onto it), so SoC deliberately does
            # NOT get a second field here.
            "batterySoh", "batteryCapacityKwh",
        )
        if k in item
    }


_MAX_SCAN_PAGES = 50  # guard against unbounded pagination on very large tables


def _load_driver_for_vehicle(vehicle_id: str) -> dict | None:
    """Find the driver assigned to this vehicle, following pagination.

    The drivers table has grown past 1 MB (7 k+ rows as of 2026-10-24); a
    single unpaginated scan silently misses rows beyond page 1.
    issues/2026-10-24-driver-lookup-single-page-scan/

    Paging every page is safe because ``handler()`` decides the caller's scope
    (``context_scope.decide``, and the fleet match after ``_load_vehicle``) before
    this runs, so only a caller with standing for this vehicle reaches the driver
    read. A ``platform-admin`` can still read any car's driver, by design.
    issues/2026-10-24-vehicle-context-driver-pii-unscoped/
    """
    table = _get_ddb().Table(_DRIVERS_TABLE)
    base_kw: dict = {
        "FilterExpression": Attr("assignedVehicleId").eq(vehicle_id),
        "ProjectionExpression": (
            "driverId, firstName, lastName, email, phone, "
            "homeBase, safetyScore, licenseClass, licenseState, "
            "yearsExperience, totalMiles, totalTrips, "
            "lastTripDate, #st, assignedVehicleId"
        ),
        "ExpressionAttributeNames": {"#st": "status"},
    }
    kw = dict(base_kw)
    for _ in range(_MAX_SCAN_PAGES):
        try:
            resp = table.scan(**kw)
        except Exception as e:  # noqa: BLE001
            log.warning("driver lookup failed for vehicleId=%s: %s", vehicle_id, e)
            return None
        items = resp.get("Items") or []
        if items:
            # Prefer the driver with the highest safety score on the first page with a
            # match (stable tiebreak for vehicles shared by multiple drivers in the seed;
            # the global maximum across all pages is not guaranteed).
            def _score(item):
                s = item.get("safetyScore", 0)
                return float(s) if isinstance(s, (int, float, Decimal)) else 0.0
            best = max(items, key=_score)
            return {
                k: best.get(k)
                for k in (
                    "driverId", "firstName", "lastName", "email", "phone",
                    "homeBase", "safetyScore", "licenseClass", "licenseState",
                    "yearsExperience", "totalMiles", "totalTrips",
                    "lastTripDate", "status",
                )
                if k in best
            }
        lek = resp.get("LastEvaluatedKey")
        if not lek:
            return None
        kw = {**base_kw, "ExclusiveStartKey": lek}
    log.warning("driver lookup exceeded page cap (%d) for vehicleId=%s", _MAX_SCAN_PAGES, vehicle_id)
    return None


class DtcLookupError(Exception):
    """The dtc-history read failed, so the vehicle's active faults are unknown.

    Raised instead of returning `[]`. An empty list means "no active faults", and
    every client renders it that way: iOS shows "No active faults — all clear." and a
    healthy score. A failed read must never be presented as that.
    issues/2026-09-27-vehicle-context-dtc-lookup-failure-returns-empty/.
    """


# A dtc-history row is cleared when its status says so, including CMS's vehicle-command clear
# (CLEARED_REMOTE), or when it carries a clear marker: clearedDate, resetAt, or clearedAt (a
# whole epoch-ms number, written by CMS services/commands/command_response_handler.py, which
# leaves clearedDate ""). This is a superset of what CMS's Flink replay guard reads (CMS
# 52eb2687). The guard ignores status and resetAt, skips an unparseable clearedDate or a
# non-Number clearedAt, and drops a clear dated too far ahead; this rule counts all of those
# as cleared, because it judges one row and a later real report writes a new ACTIVE row.
# It judges rows, not codes: an older ACTIVE row the clear did not mark still reads as active
# (CMS issue 2026-10-05-remote-clear-misses-legacy-active-rows).
# Without CLEARED_REMOTE/clearedAt, a fault cleared on the vehicle stayed "active" here
# (issue 2026-10-05-active-dtcs-reads-remote-clears-as-active).
_CLEARED_STATUSES = frozenset({"CLEARED", "CLEARED_REMOTE", "RESOLVED"})


def _row_is_cleared(item: dict) -> bool:
    """True when a dtc-history row is cleared (see the comment above)."""
    if str(item.get("status") or "").upper() in _CLEARED_STATUSES:
        return True
    if item.get("clearedDate") or item.get("resetAt"):
        return True
    cleared_at = item.get("clearedAt")
    if cleared_at is None or isinstance(cleared_at, bool):
        return False
    try:
        from decimal import Decimal, InvalidOperation
        value = Decimal(str(cleared_at))
    except (InvalidOperation, ValueError):
        return False
    return value.is_finite() and value == value.to_integral_value() and value > 0


def _load_active_dtcs(vehicle_id: str) -> list[dict[str, Any]]:
    """Return all currently-active DTCs for the vehicle, deduped by code,
    newest-first. Raises `DtcLookupError` if the dtc-history read fails;
    `[]` means the read succeeded and found nothing active.

    This is the data source the iOS Home tab's vehicle-health card and
    the Alerts tab's "Vehicle faults" section consume. Both rely on
    `session.activeDtcs` (derived from `vehicleContext.activeDtcs`)
    being populated; before this was added the context endpoint
    returned no DTCs at all and the health score was always 100.

    All severities are returned. The agent's `_fetch_active_dtcs`
    filters to CRITICAL+HIGH for prompt-priming purposes; here we
    surface everything so the iOS health-score deductions and the
    Alerts list match the actual fleet state.

    Uses the same dedup and `_row_is_cleared` rule as
    `agents/supervisor/bidi_app.py:_fetch_active_dtcs` (status CLEARED,
    CLEARED_REMOTE or RESOLVED, or a clearedDate / resetAt / clearedAt
    marker, is cleared), so iOS and the voice agent agree on which rows
    are "active right now". `lambdas/shared/tests/test_dtc_cleared_rule_parity.py`
    enforces the shared rule.

    Response shape matches iOS `ActiveDtc` model (Models.swift).
    """
    if not vehicle_id:
        return []
    from boto3.dynamodb.conditions import Key
    import time as _time
    # Status is the source of truth for "active right now". A 10-year cutoff
    # effectively reads every row for the vehicle but keeps the query bounded,
    # the same approach as the agent's helper.
    #
    # Every page is read. This used to be one `Limit=200` query with no paging:
    # past 200 history rows, an older still-ACTIVE fault silently fell off and the
    # vehicle read as having fewer faults, or none, which is the same false "no
    # faults" as a swallowed error. A vehicle with more pages than
    # `_DTC_MAX_PAGES` fails closed (DtcLookupError) rather than report a partial list.
    cutoff_ms = int(_time.time() * 1000) - 87600 * 3600 * 1000
    query_kwargs: dict[str, Any] = {
        "KeyConditionExpression": (
            Key("vehicleId").eq(vehicle_id) & Key("timestamp").gte(cutoff_ms)
        ),
        "ScanIndexForward": False,  # newest first, across pages too
        "Limit": _DTC_PAGE_SIZE,
    }
    items: list[dict[str, Any]] = []
    try:
        table = _get_ddb().Table(_DTC_HISTORY_TABLE)
        for _page in range(_DTC_MAX_PAGES):
            resp = table.query(**query_kwargs)
            items.extend(resp.get("Items", []))
            last_key = resp.get("LastEvaluatedKey")
            if not last_key:
                break
            query_kwargs["ExclusiveStartKey"] = last_key
        else:
            raise DtcLookupError(
                f"dtc-history has more than {_DTC_MAX_PAGES} pages for this vehicle; "
                "refusing to report a partial fault list"
            )
    except DtcLookupError:
        raise
    except Exception as e:  # noqa: BLE001 — any failure means the faults are unknown
        # Not `return []`: that answered 200 with "no faults" for a read that never
        # happened. The handler turns this into a 503.
        raise DtcLookupError(f"dtc-history query failed: {type(e).__name__}") from e

    seen_codes: set[str] = set()
    out: list[dict[str, Any]] = []
    for item in items:
        if _row_is_cleared(item):
            continue
        code = item.get("code")
        if not code or code in seen_codes:
            continue
        sev_raw = (item.get("severity") or "").upper()
        # Normalize numeric severity (4=CRITICAL, 3=HIGH, 2=MEDIUM, 1=LOW)
        # to the string form iOS expects.
        sev = {
            "4": "CRITICAL", "3": "HIGH", "2": "MEDIUM", "1": "LOW",
        }.get(sev_raw, sev_raw or None)
        seen_codes.add(code)
        out.append({
            "dtcId": item.get("dtcId"),
            "code": code,
            "status": item.get("status") or "ACTIVE",
            "severity": sev,
            "system": item.get("system"),
            "description": item.get("description"),
            "timestamp": item.get("timestamp"),
            "firstSeenAt": item.get("firstSeenAt") or item.get("timestamp"),
            "source": item.get("source"),
            "serviceRequired": item.get("serviceRequired"),
        })
    return out


def _load_live_connection_state(vehicle_id: str) -> dict | None:
    """Invoke the api-vehicle-live-state Lambda to get the canonical
    Redis-backed connectivity signal for the vehicle.

    The DDB row's `connectionStatus` field lags reality in both
    directions:
      - Vehicles actively pumping telemetry can still show
        `connectionStatus = "disconnected"` because the ingestion
        pipeline hasn't caught up.
      - Vehicles that just disconnected can still show
        `connectionStatus = "connected"` for similar reasons, and
        `lastSeenAt` is "last time we saw any data" not "is the
        vehicle live now" — using lastSeenAt as a freshness override
        produces false positives.

    The CMS UI and iOS Home tab already solve this by reading a
    Redis hash via `api-vehicle-live-state`, which applies a 2-min
    `LIVE_WINDOW_SECONDS` heuristic to decide if the vehicle is
    actually live right now. This helper invokes that Lambda so the
    health-score path uses the same canonical signal — no more
    DDB-vs-Redis disagreement between iOS, CMS UI, and the score.

    Returns:
      {"connectionStatus": "connected"|"disconnected",
       "lastConnectedAt": iso_str|None,
       "lastSeenAgoSeconds": int|None}
      or None if invocation failed.

    On any error (lambda not configured, invocation throws, response
    malformed) we return None and the caller falls back to the DDB
    `connectionStatus` field — graceful degradation.
    """
    if not _LIVE_STATE_FN_NAME or not vehicle_id:
        return None
    try:
        # api-vehicle-live-state expects the API-Gateway-style event
        # shape (it reads pathParameters.vehicleId). Synthesize that
        # shape so the same handler works whether invoked via API
        # Gateway or directly Lambda-to-Lambda.
        payload = json.dumps({
            "pathParameters": {"vehicleId": vehicle_id},
            "httpMethod": "GET",
        }).encode("utf-8")
        resp = _get_lambda().invoke(
            FunctionName=_LIVE_STATE_FN_NAME,
            InvocationType="RequestResponse",
            Payload=payload,
        )
        envelope = json.loads(resp["Payload"].read().decode("utf-8"))
        if envelope.get("statusCode") != 200:
            log.warning(
                "live-state non-200 for %s: %s",
                vehicle_id, str(envelope.get("body"))[:200],
            )
            return None
        body = json.loads(envelope.get("body") or "{}")
        return {
            "connectionStatus": body.get("connectionStatus"),
            "lastConnectedAt": body.get("lastConnectedAt"),
            "lastSeenAgoSeconds": body.get("lastSeenAgoSeconds"),
        }
    except Exception as e:  # noqa: BLE001
        log.warning("live-state invoke failed for %s: %s", vehicle_id, e)
        return None


#: State-of-health percentage below which a traction pack costs the vehicle
#: health points. Chosen to match what iOS already warns at
#: (`VehicleTabView.signalRow(... warn: batterySoh < 80)`); ~70-80% is also the
#: capacity retention an OEM EV battery warranty typically guarantees. If this
#: moves, move the iOS warn threshold in the same change or the app will flag a
#: pack the score considers unremarkable, or vice versa.
_BATTERY_SOH_THRESHOLD = 80.0

#: Weight of a degraded pack, on the same scale as the other deductions
#: (CRITICAL DTC 30, HIGH 15, overdue service 10, MEDIUM 8, disconnected 5).
#: Parity with overdue service is deliberate: both describe a vehicle that needs
#: attention soon rather than one that is unsafe now.
_BATTERY_SOH_DEDUCTION = 10


def _is_bev(fuel_type: str | None) -> bool:
    """Whether this vehicle has a traction pack whose SoH is meaningful.

    Mirrors iOS `VehicleTabView.isElectric(_:)` value-for-value —
    `bev` / `electric` / `ev`, lowercased, empty treated as unknown. Kept in step
    deliberately: iOS uses the same predicate to decide whether to SHOW pack
    health, so a divergence would let the app display a figure the score ignored.

    Hybrids are excluded on purpose. `phev` / `hybrid` have a pack, but its state
    of health carries different warranty and replacement semantics, and iOS does
    not render the battery card for them either. Scoring them on a BEV threshold
    would penalise a vehicle whose engine covers the degradation.
    """
    ft = (fuel_type or "").strip().lower()
    return ft in ("bev", "electric", "ev")


def _matches_swift_internet_date_time(s: str) -> bool:
    """Return True if `s` matches Swift's default ISO8601DateFormatter format.

    Swift's `ISO8601DateFormatter()` defaults to `formatOptions = [.withInternetDateTime]`,
    which accepts strings of the form `YYYY-MM-DDTHH:MM:SS` followed by either
    `Z` or a `±HH:MM` offset. Notably it REJECTS:
      - Date-only strings ("2026-05-05").
      - Fractional seconds ("...:10.141648+00:00") — those need an extra
        .withFractionalSeconds option that the iOS Home tab health-score
        code did not specify.
    The health-score helper uses this predicate to mirror that behaviour
    exactly so the score the backend now serves matches what the iOS
    client computed before this moved server-side.
    """
    import re
    # `\.\d+` would match the rejected fractional-seconds form, so we
    # explicitly forbid it. Timezone is either `Z` or a signed `HH:MM` offset.
    return bool(re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:Z|[+-]\d{2}:\d{2})",
        s,
    ))


def _load_first_scheduled_service(vehicle_id: str) -> dict | None:
    """Return the most-recently-scheduled service row for the vehicle, or None.

    Used by the health-score helper to apply a "scheduled service overdue"
    deduction when serviceDate < now. Surface only the few fields the
    caller actually needs (serviceDate is the one that matters today;
    serviceType/provider are included so future callers can surface a
    reason without a second query).

    Implementation notes:
    - Service-history rows are partitioned by vehicleId with serviceDate
      as the sort key. The iOS pre-2026-05-19 health-score formula used
      `session.scheduledService.first`, where that list comes from
      `/vehicles/{id}/service-history` which queries with
      `ScanIndexForward=False` (newest serviceDate first). To preserve
      the visible score across the iOS migration, this helper also uses
      `ScanIndexForward=False` and picks the first matching row — i.e.
      the most-recently-scheduled service, NOT the soonest-by-date.
      A vehicle can have an old "2026-05-05 DIAGNOSTIC_REPAIR" row sitting
      below a newer "2026-05-19T23:13 VSA_VOICE_TRIAGE" row; iOS used
      to look at the newer one first, and so do we.
    - Status filter accepts both "scheduled" (the lowercase form written
      by the voice-agent book tool and the seed scripts) and the
      uppercase "SCHEDULED" some older CMS seeds emit.
    - Limit=50 + FilterExpression: DDB applies the filter AFTER the page
      is read, so a tiny Limit can return 0 items even when matching
      rows exist on the second page. Use a generous Limit to make a
      "no scheduled service" answer reliable in practice — the
      service-history table is small and one vehicle has at most a
      handful of rows.
    - On any error we return None and log a warning; the health-score
      computation degrades gracefully (no overdue-service deduction).
    """
    if not vehicle_id:
        return None
    from boto3.dynamodb.conditions import Key
    try:
        resp = _get_ddb().Table(_SERVICE_HISTORY_TABLE).query(
            KeyConditionExpression=Key("vehicleId").eq(vehicle_id),
            FilterExpression=Attr("status").eq("scheduled") | Attr("status").eq("SCHEDULED"),
            ScanIndexForward=False,  # newest serviceDate first — matches iOS ordering
            Limit=50,
        )
    except Exception as e:  # noqa: BLE001
        log.warning("scheduled-service lookup failed for %s: %s", vehicle_id, e)
        return None

    items = resp.get("Items") or []
    if not items:
        return None
    # Items are already in descending serviceDate order from the query —
    # take the most-recently-scheduled row to match iOS.
    first = items[0]
    return {
        "serviceDate": first.get("serviceDate"),
        "serviceType": first.get("serviceType"),
        "provider": first.get("provider"),
        "status": first.get("status"),
    }


def _compute_health_score(
    active_dtcs: list[dict[str, Any]],
    scheduled_service_first_row: dict | None,
    live_connection_status: str | None,
    last_seen_at: str | None = None,
    battery_soh: Any = None,
    fuel_type: str | None = None,
) -> dict[str, Any]:
    """Compute the 0..100 vehicle health score and the deduction breakdown.

    Single source of truth for both iOS and CMS UI.

    Formula:
        score = 100
        for each active DTC:
            CRITICAL → -30   HIGH → -15   MEDIUM → -8   default → -4
        if scheduled service serviceDate < now: -10
        if connectionStatus != "connected": -5
        if BEV and batterySoh < 80: -10
        clamp to 0..100

    ORIGIN, AND THE INVARIANT THAT NO LONGER HOLDS
    ----------------------------------------------
    This mirrored the formula that lived in iOS `HomeTabView.computeHealthScore()`
    pre-2026-05-19, and used to promise that "the visible score doesn't change for
    any vehicle when iOS swaps to the server value". That promise was about the
    migration, and it is **deliberately retired as of 2026-08-21** by the battery
    factor below. Recorded rather than deleted because a docstring asserting an
    invariant the code no longer honours is worse than no docstring.

    THE BATTERY FACTOR
    ------------------
    Before this, a BEV whose pack had degraded to any degree still scored 100:
    the formula only saw DTCs, overdue service, and connectivity. For an electric
    vehicle, pack degradation IS the dominant long-term condition signal — iOS
    already treats it that way, giving state of health the slot engine temperature
    occupies on an ICE vehicle, explicitly as "the equivalent long-term-condition
    signal". So the gap was in the scoring, not in a missing second score. A
    parallel `batteryHealthScore` was considered and rejected: `healthScore` is
    documented as the single source of truth, and two competing "health" numbers
    on one screen leave the reader unable to say which governs.

    The 80 threshold is not invented here. iOS already warns at
    `batterySoh < 80` (`VehicleTabView`), and ~70-80% capacity retention is the
    typical floor an OEM EV battery warranty guarantees, so 80 is the point at
    which a pack stops being unremarkable. A graduated curve (a steeper deduction
    below ~70) is defensible and deliberately NOT built: it would invent a second
    threshold no other surface uses, and the reason strings below are rendered
    verbatim by two clients.

    Gated on BEV because an ICE vehicle's 12V reading is not state of health and
    must not be scored as though it were. A vehicle with no `batterySoh` is not
    penalised — absent is not degraded.

    KNOWN CONSEQUENCE: `healthScore` feeds iOS `TradeInScore`, so a degraded pack
    now also lowers the indicative trade-in condition score. That is directionally
    correct — a worn pack does reduce what the vehicle is worth — but it is a
    change to a customer-visible, money-adjacent number, and is called out here
    rather than discovered later.

    Returns:
        {
          "score": int,
          "deductions": [
            {"reason": "DTC P0299 HIGH",            "amount": 15},
            ...
            {"reason": "Scheduled service overdue", "amount": 10},
            {"reason": "Vehicle disconnected",      "amount": 5},
            {"reason": "Battery health 74%",        "amount": 10},
          ],
          "computedAt": "2026-05-19T17:48:51.123Z",
        }

    Notes:
    - Deduction reason strings are stable — both clients render them
      verbatim. CMS UI uses them in tooltips / drill-down with no
      locale-specific reformatting in v1.
    - Severity is treated case-insensitively because upstream rows
      occasionally arrive lowercase ("critical", "high"). MODERATE is
      normalized to MEDIUM in the reason string so the display matches
      what iOS used to render before this moved server-side.
    - Unknown / missing severities fall through to the default -4 case
      and are labeled "LOW" in the reason string. Keeping the catch-all
      label as "LOW" rather than "UNKNOWN" matches the visible behavior
      of the prior iOS implementation, where any non-(CRITICAL/HIGH/
      MEDIUM) severity also deducted 4 and the user saw the same total.
    """
    deductions: list[dict[str, Any]] = []
    score = 100

    for dtc in active_dtcs or []:
        code = (dtc.get("code") or "").upper()
        sev_raw = (dtc.get("severity") or "").upper()
        if sev_raw == "CRITICAL":
            amount = 30
            label = "CRITICAL"
        elif sev_raw == "HIGH":
            amount = 15
            label = "HIGH"
        elif sev_raw in ("MEDIUM", "MODERATE"):
            amount = 8
            label = "MEDIUM"
        else:
            # default branch — matches iOS's `default: score -= 4`. Any
            # severity we don't recognize (LOW, "", or a numeric form
            # that escaped normalization) lands here.
            amount = 4
            label = sev_raw if sev_raw else "LOW"
            # If sev_raw was something exotic we don't want to surface
            # it as the reason; keep the label clean by mapping anything
            # not in the known set back to "LOW".
            if label not in ("CRITICAL", "HIGH", "MEDIUM", "LOW"):
                label = "LOW"
        deductions.append({
            "reason": f"DTC {code} {label}" if code else f"DTC {label}",
            "amount": amount,
        })
        score -= amount

    # Scheduled-service overdue check. Compare the row's serviceDate
    # against now. We DELIBERATELY mimic Swift's default
    # `ISO8601DateFormatter()` parser, which accepts only the strict
    # "internet date-time" format — `YYYY-MM-DDTHH:MM:SS` followed by
    # either `Z` or a `±HH:MM` offset. Specifically, Swift's default
    # rejects:
    #   - Date-only strings ("2026-05-05") — no time component.
    #   - Fractional seconds ("...:10.141648+00:00") — Swift requires
    #     the .withFractionalSeconds option to accept these, and the
    #     iOS Home tab's `ISO8601DateFormatter()` call did not opt in.
    # Matching that behaviour is intentional. The pre-2026-05-19 iOS
    # health-score formula skipped the deduction whenever the parse
    # failed (the optional binding `let date = …` returned nil). VEH-
    # 0047's seeded scheduled rows fall into both rejected buckets
    # ("2026-05-05" and "2026-05-19T23:13:10.141648+00:00"), and iOS
    # silently skipped the deduction for both — yielding the demo's
    # documented score of 58. Loosening this check to accept either
    # format would cause every freshly-deployed VSA voice-triage
    # booking (which writes serviceDate with fractional seconds via
    # `datetime.now(timezone.utc).isoformat()`) to be flagged as
    # overdue the moment its serviceDate slips into the past, which
    # the iOS surface never did.
    if scheduled_service_first_row:
        from datetime import datetime, timezone
        raw = scheduled_service_first_row.get("serviceDate")
        if isinstance(raw, str) and _matches_swift_internet_date_time(raw):
            try:
                normalized = raw.replace("Z", "+00:00") if raw.endswith("Z") else raw
                parsed = datetime.fromisoformat(normalized)
            except ValueError:
                parsed = None
            if parsed is not None:
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                if parsed < datetime.now(timezone.utc):
                    deductions.append({
                        "reason": "Scheduled service overdue",
                        "amount": 10,
                    })
                    score -= 10

    # Connection-status check.
    #
    # `live_connection_status` is the canonical signal — when the
    # caller has access to api-vehicle-live-state (Redis-backed,
    # 2-min live-window heuristic) it'll be that. When the live-state
    # Lambda is unreachable, the caller falls back to the DDB row's
    # `connectionStatus` field, which lags reality.
    #
    # We deliberately do NOT use a `lastSeenAt`-based freshness
    # override here anymore — that was tried and produced false
    # positives (vehicles that recently disconnected still showed
    # `lastSeenAt` < 5 min and were marked connected). The Redis
    # signal is the only thing that means "live right now"; if it
    # isn't available we accept the DDB lag rather than guess.
    #
    # `last_seen_at` is still accepted as a parameter so callers can
    # pass it through for any future use, but it's currently unused
    # in the score computation.
    _ = last_seen_at  # intentionally unused; see comment above.
    if (live_connection_status or "").lower() != "connected":
        deductions.append({
            "reason": "Vehicle disconnected",
            "amount": 5,
        })
        score -= 5

    # Battery state-of-health check — BEV only.
    #
    # Gated on fuel type because `batterySoh` means remaining capacity against
    # original on a traction pack. An ICE row's battery fields describe a 12V
    # starter battery, where the same number is not state of health and must not
    # be scored as one.
    #
    # `Decimal` is what boto3 hands back for a DDB number, and a bare
    # `Decimal("74") < 80` works but `float()` keeps the reason string and the
    # comparison reading the same way. A non-numeric or absent value is skipped
    # rather than defaulted: absent is not degraded, and defaulting to 100 would
    # be indistinguishable from a healthy pack while defaulting to 0 would
    # penalise every vehicle whose telemetry has not reported yet.
    if _is_bev(fuel_type) and battery_soh is not None:
        try:
            soh = float(battery_soh)
        except (TypeError, ValueError):
            soh = None
        if soh is not None and soh < _BATTERY_SOH_THRESHOLD:
            deductions.append({
                # Carries the measured figure, not just "below threshold", so the
                # drill-down can show why without a second round trip.
                "reason": f"Battery health {int(soh)}%",
                "amount": _BATTERY_SOH_DEDUCTION,
            })
            score -= _BATTERY_SOH_DEDUCTION

    # Clamp 0..100 to match the prior iOS clamp.
    score = max(0, min(100, score))

    from datetime import datetime, timezone
    computed_at = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    return {
        "score": score,
        "deductions": deductions,
        "computedAt": computed_at,
    }


def _load_snapshot(vehicle_id: str) -> tuple["dict | None", str]:
    """GetItem the vehicle snapshot by vehicleId (never by VIN).

    Returns:
        (item, "ok")   — item found.
        (None, "none") — no item for this vehicleId.
        (None, "error") — DDB exception; logged, route continues at 200.
    """
    try:
        table = _get_ddb().Table(_SNAPSHOT_TABLE)
        resp = table.get_item(Key={"vehicleId": vehicle_id})
        item = resp.get("Item")
        if item is None:
            return None, "none"
        return item, "ok"
    except Exception as e:  # noqa: BLE001
        log.warning("snapshot read failed for vehicleId=%s: %s", vehicle_id, e)
        return None, "error"


def _snapshot_scalar(field: Any) -> "int | float | None":
    """Extract the .value from a snapshot field dict, converting Decimal → int/float.

    Returns None when the field is absent, null, or not a dict — never 0 as a stand-in.
    Returns None for any .value that is not int, float, or Decimal (bools included);
    a string or bool passed through to fuelLevel/batterySoh (typed Double? on iOS)
    causes Swift JSONDecoder to raise typeMismatch for the whole response.
    """
    if not isinstance(field, dict):
        return None
    val = field.get("value")
    if val is None:
        return None
    if isinstance(val, Decimal):
        return int(val) if val % 1 == 0 else float(val)
    if isinstance(val, bool) or not isinstance(val, (int, float)):
        return None
    return val


def _floor_odometer(val: "int | float | None") -> "int | None":
    """Convert an odometer reading to whole miles by flooring (odometers don't round up).

    iOS declares odometer and mileage as Int? — a fractional value makes Swift's JSONDecoder
    raise DecodingError.dataCorrupted and the whole response is rejected.  (C1 fix.)

    S2 (cycle 2): return None for non-numeric types (str, bool, etc.) rather than
    raising TypeError and turning the route into a 500.  bool is excluded explicitly
    because isinstance(True, int) is True in Python but True/False are not odometers.
    """
    if val is None:
        return None
    if isinstance(val, bool) or not isinstance(val, (int, float, Decimal)):
        return None
    return int(math.floor(val))


def handler(event, context):
    path_params = event.get("pathParameters") or {}
    vehicle_id = path_params.get("vehicleId")
    if not vehicle_id:
        return _respond(400, {"error": "missing vehicleId path parameter"})

    # Caller scope before any read (context_scope.py). A refused caller gets nothing:
    # no vehicle row, no driver, no faults.
    claims = context_scope.claims_from_event(event)
    scope = context_scope.decide(claims, vehicle_id)
    if not scope.admitted:
        return _refuse(scope, vehicle_id, claims)
    log.info("vehicle-context: admit standing=%s sub=%.64r vehicleId=%.80r",
             scope.standing, claims.get("sub"), vehicle_id)

    vehicle = _load_vehicle(vehicle_id)
    # A fleet operator is admitted only for a vehicle in one of its fleets. A missing
    # vehicle gets the same 403 as another fleet's, so the answer doesn't show which
    # ids exist.
    if scope.needs_fleet_match and not context_scope.fleet_admits(scope, vehicle):
        return _refuse(scope, vehicle_id, claims)
    if not vehicle:
        return _respond(404, {"error": f"vehicle {vehicle_id} not found"})

    # Stage C: overlay snapshot values on the vehicle (same rules as api-drivers-me).
    snapshot_status = "none"
    values: "dict | None" = None
    snapshot_meta: "dict | None" = None
    snapshot_item, snapshot_status = _load_snapshot(vehicle_id)
    if snapshot_status == "error":
        vehicle["odometer"] = None
        vehicle["mileage"] = None
        # S1 (cycle 2): leave fuelLevel for non-electric cars (no snapshot fuel substitute).
        if _is_bev(vehicle.get("fuelType")):
            vehicle["fuelLevel"] = None
        vehicle["batterySoh"] = None
    elif snapshot_item is not None:
        # C1 (review cycle 1): floor to whole miles so iOS Int? fields decode.
        odo = _floor_odometer(_snapshot_scalar(snapshot_item.get("odometer")))
        vehicle["odometer"] = odo
        vehicle["mileage"] = odo  # iOS reads `odometer ?? mileage`; keep in step
        # Non-electric fuelLevel rule (decisions.md 2026-10-24 ~01:25Z, AVX S3):
        # batteryCharge fills fuelLevel only for BEVs.
        if _is_bev(vehicle.get("fuelType")):
            vehicle["fuelLevel"] = _snapshot_scalar(snapshot_item.get("batteryCharge"))
        # Non-EV: leave fuelLevel as the row value (no change).
        vehicle["batterySoh"] = _snapshot_scalar(snapshot_item.get("batteryHealth"))
        values = {
            "odometer":      snapshot_item.get("odometer"),
            "batteryCharge": snapshot_item.get("batteryCharge"),
            "batteryHealth": snapshot_item.get("batteryHealth"),
            "batteryAge":    snapshot_item.get("batteryAge"),
            "tires":         snapshot_item.get("tires"),
            "service":       snapshot_item.get("service"),
            "faults":        snapshot_item.get("faults"),
        }
        snapshot_meta = {
            "runDate":    snapshot_item.get("runDate"),
            "dataThrough": snapshot_item.get("dataThrough"),
            "computedAt": snapshot_item.get("computedAt"),
        }

    driver = _load_driver_for_vehicle(vehicle_id)
    try:
        active_dtcs = _load_active_dtcs(vehicle_id)
    except DtcLookupError as e:
        # 503, not 200 with an empty or null list. iOS decodes `activeDtcs` as
        # optional and reads null as empty, so only a non-2xx reaches its Alerts-tab
        # "Couldn't load" state. The supervisor (`vehicle_location.py`) also treats
        # non-2xx as a failure. (The CMS UI reads CMS's own /health route, not this one.)
        # No health score either: without the faults it would overstate health. iOS Home
        # shows no number from this response: after a failed first read it shows its
        # loading skeleton, and after an earlier success it keeps that real score. Its
        # health card shows only a score from a successful read for the current vehicle
        # (CMS issue 2026-10-05-ios-home-health-score-on-failed-read). This comment used
        # to say Home showed a fallback 100 after a failed read; on CMS iOS builds before
        # 80dd36a8 (2026-09-27) a driver refresh could show it, not since.
        log.error(
            "dtc lookup failed for vehicleId=%.80r; answering 503: %s",
            vehicle_id, e.__cause__ or e,
        )
        return _respond(503, {
            "error": "vehicle faults could not be loaded",
            "dtcLookup": "failed",
            "vehicleId": vehicle_id,
        })
    scheduled_first = _load_first_scheduled_service(vehicle_id)

    # Canonical connectivity signal via api-vehicle-live-state, which
    # reads the Redis meta hash (CMS UI + iOS Home tab use the same
    # backend). When unavailable, fall back to the DDB row's
    # `connectionStatus` field — same behaviour the helper had before
    # this Lambda gained the inter-Lambda invoke. Override the vehicle
    # dict's connectionStatus + lastSeenAt with the canonical values so
    # downstream consumers (iOS, the supervisor) see
    # one source of truth.
    live_state = _load_live_connection_state(vehicle_id)
    if vehicle is not None:
        if live_state and live_state.get("connectionStatus"):
            vehicle["connectionStatus"] = live_state["connectionStatus"]
        if live_state and live_state.get("lastConnectedAt"):
            vehicle["lastSeenAt"] = live_state["lastConnectedAt"]
    live_connection_status = (
        (live_state or {}).get("connectionStatus")
        or (vehicle.get("connectionStatus") if vehicle else None)
    )
    last_seen_at = vehicle.get("lastSeenAt") if vehicle else None
    health = _compute_health_score(
        active_dtcs=active_dtcs,
        scheduled_service_first_row=scheduled_first,
        live_connection_status=live_connection_status,
        last_seen_at=last_seen_at,
        # Read off the projected row, so the score and the battery card the client
        # renders are derived from the same two values.
        battery_soh=(vehicle or {}).get("batterySoh"),
        fuel_type=(vehicle or {}).get("fuelType"),
    )

    # C1 (cycle 2): iOS declares HealthScoreBreakdown.score: Int and Deduction.amount: Int
    # (both non-optional).  The W3 shape (score: null inside a present breakdown) causes
    # DecodingError.valueNotFound and iOS rejects the whole response.  Fix: null the
    # entire breakdown and surface the reason in a new top-level key that Codable ignores.
    # decisions.md 2026-10-24 ~02:40Z.
    health_score_unavailable = None
    if snapshot_status == "error" and _is_bev((vehicle or {}).get("fuelType")):
        health_score_unavailable = {"reason": "Battery health unavailable"}
        health = None

    from datetime import datetime, timezone
    generated = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")

    return _respond(200, {
        "vehicleId": vehicle_id,
        "vehicle": vehicle,
        "driver": driver,
        "activeDtcs": active_dtcs,
        "healthScore": health["score"] if health else None,
        "healthScoreBreakdown": health,
        "healthScoreUnavailable": health_score_unavailable,
        "generatedAt": generated,
        "snapshotStatus": snapshot_status,
        "values": values,
        "snapshot": snapshot_meta,
    })
