"""
GET /vehicles/{vehicleId}/safety-events

Returns recent safety events (harsh braking, rapid acceleration, phone usage,
speeding, crashes, etc.) for a vehicle, sourced from the CMS safety-events
DynamoDB table via the `vehicleId-timestamp-index` GSI.

Query string:
    days   — lookback window in days (default 7, max 30)
    limit  — max events returned (default 50, max 200)

Response shape:
    {
      "vehicleId": "VEH-0047",
      "windowDays": 7,
      "events": [SafetyEvent, ...],
      "generatedAt": "2026-05-06T..."
    }

Where SafetyEvent is a cleaned, client-friendly projection:
    {
      "eventId":      "safety.phone_usage-1778008168961-VEH-0047",
      "eventType":    "phone_usage",
      "severity":     "MEDIUM",          # always string CRITICAL/HIGH/MEDIUM/LOW
      "description":  "Phone usage while driving",
      "timestamp":    1778008168961,     # ms since epoch, int
      "occurredAt":   "2026-05-05T...",  # ISO-8601 derived from timestamp
      "resolved":     false,
      "tripId":       "...",
      "location":     {"latitude": 47.64, "longitude": -122.29} | null
    }

Why a dedicated endpoint
------------------------
iOS's Alerts tab surfaces driver-relevant events (DTCs + safety events +
triage alerts) and used to only have the first and third. The CMS UI has
a `/api/v1/safety-events?driverId=...` endpoint but it's fleet-operator-
oriented (returns unnormalized severity, uses `message` instead of
`description`, no window filter). Rather than bend that endpoint, this
mirrors the pattern of the other iOS-facing Lambdas: narrow, VPCless,
returns exactly what the iOS view needs.

Severity normalisation
----------------------
The safety-events table contains a mix of rows with string severity
("LOW"/"MEDIUM"/"HIGH"/"CRITICAL") and numeric-as-string severity
("1"/"2"/"3"/"4", reverse-ranked: 4=CRITICAL, 1=LOW). This matches
the invariant noted in the session handoff for the event-catalog.
We normalise to the string form everywhere so iOS never has to.
"""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

import boto3
from boto3.dynamodb.conditions import Key

log = logging.getLogger()
log.setLevel(logging.INFO)

_SAFETY_TABLE = os.environ.get("CMS_SAFETY_EVENTS_TABLE", f"cms-{os.environ.get('CMS_STAGE', 'dev')}-storage-safety-events")
_GSI = os.environ.get("CMS_SAFETY_EVENTS_GSI", "vehicleId-timestamp-index")
_DEFAULT_DAYS = int(os.environ.get("DEFAULT_DAYS", "7"))
_MAX_DAYS = int(os.environ.get("MAX_DAYS", "30"))
_DEFAULT_LIMIT = int(os.environ.get("DEFAULT_LIMIT", "50"))
_MAX_LIMIT = int(os.environ.get("MAX_LIMIT", "200"))

# Numeric → string severity mapping. Matches the event-catalog convention
# documented in the session handoff: 4=CRITICAL, 1=LOW (reverse-ranked).
_NUMERIC_SEVERITY = {
    "4": "CRITICAL",
    "3": "HIGH",
    "2": "MEDIUM",
    "1": "LOW",
    "0": "LOW",
}
_ALLOWED_STRING_SEVERITIES = {"CRITICAL", "HIGH", "MEDIUM", "LOW"}

_ddb = None


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


def _parse_int(qs: dict, key: str, default: int, ceiling: int) -> int:
    raw = (qs or {}).get(key)
    if raw is None:
        return default
    try:
        v = int(raw)
    except (TypeError, ValueError):
        return default
    return max(1, min(ceiling, v))


def _normalise_severity(raw: Any) -> str:
    """Map whatever severity shape the DDB row carries to the canonical
    string form. Unknown values fall through to "LOW" — that matches
    iOS's fallback behaviour for ActiveDtc severity."""
    if raw is None:
        return "LOW"
    s = str(raw).strip()
    if not s:
        return "LOW"
    upper = s.upper()
    if upper in _ALLOWED_STRING_SEVERITIES:
        return upper
    if s in _NUMERIC_SEVERITY:
        return _NUMERIC_SEVERITY[s]
    # Sometimes severity is a float like "2.0" — truncate.
    try:
        bucket = str(int(float(s)))
        if bucket in _NUMERIC_SEVERITY:
            return _NUMERIC_SEVERITY[bucket]
    except (TypeError, ValueError):
        pass
    return "LOW"


def _ts_to_iso(ts_ms: int | None) -> str | None:
    if ts_ms is None:
        return None
    try:
        return datetime.fromtimestamp(ts_ms / 1000.0, tz=timezone.utc).isoformat()
    except (TypeError, ValueError, OverflowError):
        return None


def _coerce_location(item: dict) -> dict | None:
    """Pull lat/lng from either the nested `location` map (newer rows)
    or the top-level `lat`/`lng` columns (older rows). Returns None if
    neither yields numerics."""
    loc = item.get("location") or {}
    lat = loc.get("latitude") if isinstance(loc, dict) else None
    lng = loc.get("longitude") if isinstance(loc, dict) else None
    if lat is None:
        lat = item.get("lat")
    if lng is None:
        lng = item.get("lng")
    try:
        if lat is None or lng is None:
            return None
        return {"latitude": float(lat), "longitude": float(lng)}
    except (TypeError, ValueError):
        return None


def _project(item: dict) -> dict:
    """Narrow DDB row to the client-facing shape."""
    ts_raw = item.get("timestamp")
    ts_ms: int | None
    try:
        ts_ms = int(ts_raw) if ts_raw is not None else None
    except (TypeError, ValueError):
        ts_ms = None

    # description falls back through message → eventType (prettified)
    # so iOS never renders an empty row. Older rows use `message`,
    # newer rows use `description`; both observed in the table.
    description = (
        item.get("description")
        or item.get("message")
        or _pretty_event_type(item.get("eventType") or item.get("event_id"))
    )

    # createdAt may be absent on simulator-seeded rows — synthesize from
    # timestamp so iOS always has a display string.
    created_at = item.get("createdAt") or _ts_to_iso(ts_ms)

    return {
        "eventId": item.get("eventId"),
        "eventType": (item.get("eventType") or item.get("event_id") or "unknown").lower()
            .replace("safety.", ""),
        "severity": _normalise_severity(item.get("severity")),
        "description": description,
        "timestamp": ts_ms,
        "occurredAt": created_at,
        "resolved": bool(item.get("resolved") or False),
        "tripId": item.get("tripId"),
        "location": _coerce_location(item),
    }


def _pretty_event_type(raw: Any) -> str:
    if not raw:
        return "Safety event"
    s = str(raw).replace("safety.", "").replace("_", " ").strip()
    return s.capitalize() or "Safety event"


def handler(event, _context):
    path_params = event.get("pathParameters") or {}
    vehicle_id = path_params.get("vehicleId")
    if not vehicle_id:
        return _respond(400, {"error": "missing vehicleId path parameter"})

    qs = event.get("queryStringParameters") or {}
    days = _parse_int(qs, "days", _DEFAULT_DAYS, _MAX_DAYS)
    limit = _parse_int(qs, "limit", _DEFAULT_LIMIT, _MAX_LIMIT)

    now_ms = int(time.time() * 1000)
    cutoff_ms = now_ms - (days * 24 * 60 * 60 * 1000)

    table = _get_ddb().Table(_SAFETY_TABLE)
    try:
        resp = table.query(
            IndexName=_GSI,
            KeyConditionExpression=(
                Key("vehicleId").eq(vehicle_id)
                & Key("timestamp").gte(cutoff_ms)
            ),
            ScanIndexForward=False,  # newest first
            Limit=limit,
        )
    except Exception as e:  # noqa: BLE001
        log.exception("safety-events query failed for vehicleId=%s: %s", vehicle_id, e)
        return _respond(500, {"error": f"safety-events query failed: {e}"})

    items = resp.get("Items") or []
    events = [_project(it) for it in items]

    generated = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")

    return _respond(200, {
        "vehicleId": vehicle_id,
        "windowDays": days,
        "events": events,
        "generatedAt": generated,
    })
