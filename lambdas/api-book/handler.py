"""POST /book

Native-app booking endpoint. Persists a "scheduled" row into the CMS
service-history table, mirroring what the voice agent's book tool
writes when Nova books a service via voice. Same row shape so both
flows surface identically in the iOS Service tab and CMS UI.

Request (JSON):
    {
      "vehicleId":  "VEH-FORD-001",       // required, CMS primary key
      "vin":        "1FTFW1ED5MFB12345",   // required, audit field
      "driverId":   "DRV-FORD-001",       // optional, audit
      "tenantId":   "ford",                // required, audit
      "centerId":   "SC-ATL-101",          // required, which center
      "centerName": "Hennessy Mercedes",   // optional display label
      "centerAddress": "...",              // optional
      "slot":       "Tuesday May 19 at 10:00 AM",  // required, free-form
      "capability": "tire-inflation",      // required, original ask
      "reportedSymptom": "Tire pressure low",      // required, driver words
      "narrative":  "Tire pressure check at Hennessy Mercedes for Tuesday May 19 at 10:00 AM"  // required
    }

Response:
    {
      "requestNumber": "VSA-2026-05-18-A1B2",
      "status": "scheduled",
      "scheduledFor": "2026-05-19T15:00:00Z",   // best-effort parse of slot
      "serviceDate": "2026-05-18T20:14:32Z"     // when the row was written
    }

Auth: Cognito authorizer.
"""
from __future__ import annotations

import json
import logging
import os
import re
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import boto3
from botocore.exceptions import BotoCoreError, ClientError

log = logging.getLogger()
log.setLevel(logging.INFO)

_TABLE_NAME = os.environ.get(
    "CMS_SERVICE_HISTORY_TABLE",
    f"cms-{os.environ.get('CMS_STAGE', 'dev')}-storage-service-history",
)
_VEHICLES_TABLE = os.environ.get(
    "CMS_VEHICLES_TABLE",
    f"cms-{os.environ.get('CMS_STAGE', 'dev')}-storage-vehicles",
)

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


def _resp(status: int, body: dict) -> dict:
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body, cls=_DecimalEncoder),
    }


def _to_ddb_safe(value: Any) -> Any:
    """Convert floats to Decimal so DynamoDB accepts them."""
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, dict):
        return {k: _to_ddb_safe(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_to_ddb_safe(v) for v in value]
    return value


def _fetch_vehicle_enrichment(vehicle_id: str) -> dict[str, Any]:
    """Best-effort make/model/odometer lookup so the new history row
    matches the shape of seeded historical rows. Failures swallowed —
    the row is still useful without enrichment."""
    if not vehicle_id:
        return {}
    try:
        item = _get_ddb().Table(_VEHICLES_TABLE).get_item(
            Key={"vehicleId": vehicle_id}
        ).get("Item") or {}
        return {
            "vin": item.get("vin"),
            "make": item.get("make"),
            "model": item.get("model"),
            "mileageAtService": item.get("odometer") or item.get("mileage") or 0,
        }
    except Exception as e:  # noqa: BLE001
        log.warning("book: vehicle enrichment failed for %s: %s", vehicle_id, e)
        return {}


# Day name → weekday number, for parsing slots like "Tuesday May 19 at 10:00 AM"
_WEEKDAY = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
}


def _parse_slot(slot: str) -> datetime:
    """Best-effort parse of the human-readable slot string into a UTC
    datetime. Format: 'Tuesday May 19 at 10:00 AM' or '... 1:00 PM'.
    Falls back to (now + 7d) when parsing fails so the row still has a
    plausible scheduledFor value."""
    fallback = datetime.now(timezone.utc) + timedelta(days=7)
    if not slot:
        return fallback
    m = re.search(
        r"(?P<day>\w+)\s+(?P<month>\w+)\s+(?P<dom>\d{1,2})"
        r"\s+at\s+(?P<hour>\d{1,2}):(?P<minute>\d{2})\s+(?P<ampm>AM|PM)",
        slot, re.IGNORECASE,
    )
    if not m:
        return fallback
    try:
        # Pick the next future occurrence of (month, day-of-month).
        # Year inferred from now; if that date is already past, roll to next year.
        now = datetime.now(timezone.utc)
        candidate = datetime.strptime(
            f"{m['month']} {m['dom']} {now.year} "
            f"{m['hour']}:{m['minute']} {m['ampm']}",
            "%B %d %Y %I:%M %p",
        ).replace(tzinfo=timezone.utc)
        if candidate < now:
            candidate = candidate.replace(year=now.year + 1)
        return candidate
    except (ValueError, KeyError):
        return fallback


def handler(event: dict, _context) -> dict:
    """API Gateway proxy integration entry point."""
    try:
        body = json.loads(event.get("body") or "{}")
    except json.JSONDecodeError:
        return _resp(400, {"error": "Body must be JSON"})

    required = ["vehicleId", "vin", "tenantId", "centerId",
                "slot", "capability", "reportedSymptom", "narrative"]
    missing = [k for k in required if not body.get(k)]
    if missing:
        return _resp(400, {"error": f"Missing required fields: {missing}"})

    vehicle_id = body["vehicleId"]
    vin = body["vin"]
    driver_id = body.get("driverId") or ""
    tenant_id = body["tenantId"]
    center_id = body["centerId"]
    center_name = body.get("centerName") or center_id
    center_address = body.get("centerAddress") or ""
    slot_text = body["slot"]
    capability = body["capability"]
    reported_symptom = body["reportedSymptom"]
    narrative = body["narrative"]

    now = datetime.now(timezone.utc)
    service_date_iso = now.isoformat()
    scheduled_for = _parse_slot(slot_text)

    # Request number format mirrors the voice agent so both flows look
    # identical to downstream consumers.
    date_slug = now.strftime("%Y-%m-%d")
    seq = uuid.uuid4().hex[:4].upper()
    request_number = f"VSA-{date_slug}-{seq}"

    enrichment = _fetch_vehicle_enrichment(vehicle_id)

    item: dict[str, Any] = {
        "vehicleId": vehicle_id,
        "serviceDate": service_date_iso,
        "status": "scheduled",
        "category": "UNSCHEDULED",
        "serviceType": "VSA_NATIVE_BOOKING",
        "description": f"Native-app booking: {reported_symptom[:200]}",
        "notes": narrative[:500],
        "scheduledFor": scheduled_for.isoformat(),
        # --- audit trail ---
        "requestNumber": request_number,
        "reportedSymptom": reported_symptom[:500],
        "narrative": narrative[:1000],
        "driverId": driver_id,
        "tenantId": tenant_id,
        "source": "ios-native-booking",
        "createdVia": "api-book-lambda",
        "capability": capability,
        # --- service center info from the find-service-center result ---
        "serviceCenterId": center_id,
        "serviceCenterName": center_name,
        "serviceCenterAddress": center_address,
        "slotChosen": slot_text,
        # --- enrichment (best-effort) ---
        "vin": enrichment.get("vin") or vin,
        "make": enrichment.get("make") or "",
        "model": enrichment.get("model") or "",
        "mileageAtService": enrichment.get("mileageAtService") or 0,
        # --- cost/labor: unknown at booking time ---
        "cost": 0,
        "laborHours": 0,
        "warrantyApplied": False,
        "warrantyCoverage": 0,
        "provider": center_name,
        "providerType": "Native booking",
        # --- composite-key uniqueness ---
        "serviceId": uuid.uuid4().hex[:8],
    }

    try:
        _get_ddb().Table(_TABLE_NAME).put_item(Item=_to_ddb_safe(item))
        log.info(
            "book: wrote scheduled row vehicleId=%s requestNumber=%s "
            "centerId=%s slot=%r",
            vehicle_id, request_number, center_id, slot_text,
        )
    except (BotoCoreError, ClientError) as e:
        log.exception(
            "book: failed to write to %s for vehicleId=%s: %s",
            _TABLE_NAME, vehicle_id, e,
        )
        return _resp(500, {"error": "Failed to persist booking"})

    return _resp(200, {
        "requestNumber": request_number,
        "status": "scheduled",
        "scheduledFor": scheduled_for.isoformat(),
        "serviceDate": service_date_iso,
        "centerName": center_name,
        "slot": slot_text,
    })
