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

Driver is nil when no driver is assigned or we can't find one. The
vehicle record carries an `assignedDriverId` field in some schemas;
we also fall back to scanning drivers for assignedVehicleId = this
vehicleId (the pattern used by the CMS seed scripts).

healthScore is the single source of truth for both the iOS Home tab
and the CMS UI Vehicle Detail page — neither client recomputes it.
The breakdown's `deductions` list uses stable reason strings so the
CMS UI can render them verbatim in its tooltip / drill-down without
locale-specific reformatting in v1.
"""
from __future__ import annotations

import json
import logging
import os
from decimal import Decimal
from typing import Any

import boto3
from boto3.dynamodb.conditions import Attr

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
# Name of the api-vehicle-live-state Lambda. When set, the context
# handler invokes it to get a Redis-backed canonical connectionStatus
# (with the same 2-min live-window logic CMS UI + iOS already use)
# instead of relying on the lagging DDB `connectionStatus` field. When
# unset, falls back to the DDB field — a graceful degradation path
# for local development.
_LIVE_STATE_FN_NAME = os.environ.get("LIVE_STATE_FN_NAME", "")

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
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Headers": "Content-Type,Authorization",
        },
        "body": payload,
    }


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
        )
        if k in item
    }


def _load_driver_for_vehicle(vehicle_id: str) -> dict | None:
    """Find the driver assigned to this vehicle.

    Drivers carry an `assignedVehicleId` field populated by the seed. Scan
    with a filter — the drivers table is small (~75 rows) so this is
    cheap. First match wins.
    """
    table = _get_ddb().Table(_DRIVERS_TABLE)
    # NOTE: Limit is applied BEFORE FilterExpression in DDB scans, so a small
    # Limit causes false empty results when the matching rows happen to sit
    # past the first N scanned items. The drivers table is ~75 rows total so
    # we scan the whole thing (one page in practice).
    try:
        resp = table.scan(
            FilterExpression=Attr("assignedVehicleId").eq(vehicle_id),
            ProjectionExpression=(
                "driverId, firstName, lastName, email, phone, "
                "homeBase, safetyScore, licenseClass, licenseState, "
                "yearsExperience, totalMiles, totalTrips, "
                "lastTripDate, #st, assignedVehicleId"
            ),
            ExpressionAttributeNames={"#st": "status"},
        )
    except Exception as e:  # noqa: BLE001
        log.warning("driver lookup failed for vehicleId=%s: %s", vehicle_id, e)
        return None

    items = resp.get("Items") or []
    if not items:
        return None

    # Prefer the driver with the highest safety score (stable tiebreak for
    # vehicles shared across multiple drivers, which happens in the seed).
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


def _load_active_dtcs(vehicle_id: str) -> list[dict[str, Any]]:
    """Return all currently-active DTCs for the vehicle, deduped by code,
    newest-first. Returns [] on any failure.

    This is the data source the iOS Home tab's vehicle-health card and
    the Alerts tab's "Vehicle faults" section consume. Both rely on
    `session.activeDtcs` (derived from `vehicleContext.activeDtcs`)
    being populated; before this was added the context endpoint
    returned no DTCs at all and the health score was always 100.

    All severities are returned. The agent's `_fetch_active_dtcs`
    filters to CRITICAL+HIGH for prompt-priming purposes; here we
    surface everything so the iOS health-score deductions and the
    Alerts list match the actual fleet state.

    Mirrors the dedup + status filter logic in
    `agents/supervisor/bidi_app.py:_fetch_active_dtcs` so iOS and the
    voice agent agree on which rows are "active right now".

    Response shape matches iOS `ActiveDtc` model (Models.swift).
    """
    if not vehicle_id:
        return []
    from boto3.dynamodb.conditions import Key
    import time as _time
    try:
        # Status is the source of truth for "active right now". A 10-year
        # cutoff effectively scans all rows for the vehicle but keeps the
        # query bounded — same approach as the agent's helper. Limit=200
        # is generous; even the noisiest fleet vehicle in seed data has
        # ~30 history rows.
        cutoff_ms = int(_time.time() * 1000) - 87600 * 3600 * 1000
        resp = _get_ddb().Table(_DTC_HISTORY_TABLE).query(
            KeyConditionExpression=(
                Key("vehicleId").eq(vehicle_id)
                & Key("timestamp").gte(cutoff_ms)
            ),
            ScanIndexForward=False,  # newest first
            Limit=200,
        )
    except Exception as e:  # noqa: BLE001
        log.warning("dtc lookup failed for %s: %s", vehicle_id, e)
        return []

    seen_codes: set[str] = set()
    out: list[dict[str, Any]] = []
    for item in resp.get("Items", []):
        status = (item.get("status") or "").upper()
        if status in ("CLEARED", "RESOLVED"):
            continue
        if item.get("clearedDate"):
            # Some producers leave status=ACTIVE but set clearedDate
            # when the fault was resolved. Honor that as cleared.
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
) -> dict[str, Any]:
    """Compute the 0..100 vehicle health score and the deduction breakdown.

    Single source of truth for both iOS and CMS UI. Mirrors the formula
    that lived in iOS HomeTabView.computeHealthScore() pre-2026-05-19,
    so the visible score doesn't change for any vehicle when iOS swaps
    to the server value.

    Formula:
        score = 100
        for each active DTC:
            CRITICAL → -30   HIGH → -15   MEDIUM → -8   default → -4
        if scheduled service serviceDate < now: -10
        if connectionStatus != "connected": -5
        clamp to 0..100

    Returns:
        {
          "score": int,
          "deductions": [
            {"reason": "DTC P0299 HIGH",            "amount": 15},
            ...
            {"reason": "Scheduled service overdue", "amount": 10},
            {"reason": "Vehicle disconnected",      "amount": 5},
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

    # Clamp 0..100 to match the prior iOS clamp.
    score = max(0, min(100, score))

    from datetime import datetime, timezone
    computed_at = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    return {
        "score": score,
        "deductions": deductions,
        "computedAt": computed_at,
    }


def handler(event, context):
    path_params = event.get("pathParameters") or {}
    vehicle_id = path_params.get("vehicleId")
    if not vehicle_id:
        return _respond(400, {"error": "missing vehicleId path parameter"})

    vehicle = _load_vehicle(vehicle_id)
    if not vehicle:
        return _respond(404, {"error": f"vehicle {vehicle_id} not found"})

    driver = _load_driver_for_vehicle(vehicle_id)
    active_dtcs = _load_active_dtcs(vehicle_id)
    scheduled_first = _load_first_scheduled_service(vehicle_id)

    # Canonical connectivity signal via api-vehicle-live-state, which
    # reads the Redis meta hash (CMS UI + iOS Home tab use the same
    # backend). When unavailable, fall back to the DDB row's
    # `connectionStatus` field — same behaviour the helper had before
    # this Lambda gained the inter-Lambda invoke. Override the vehicle
    # dict's connectionStatus + lastSeenAt with the canonical values so
    # downstream consumers (iOS, CMS UI VehicleHealthScoreWidget) see
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
    )

    from datetime import datetime, timezone
    generated = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")

    return _respond(200, {
        "vehicleId": vehicle_id,
        "vehicle": vehicle,
        "driver": driver,
        "activeDtcs": active_dtcs,
        "healthScore": health["score"],
        "healthScoreBreakdown": health,
        "generatedAt": generated,
    })
