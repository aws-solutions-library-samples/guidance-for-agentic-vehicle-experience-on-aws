"""
GET /drivers/me

Resolves the signed-in Cognito user to their CMS driver record + assigned
vehicle. The iOS app calls this once after sign-in to populate "who am I
and what do I drive?" state.

Request:
    No body. Cognito authorizer populates requestContext.authorizer.claims
    with the user's email + sub.

Response:
    {
      "driver": { driverId, firstName, lastName, email, cognitoEmail, ... } | null,
      "vehicle": { vehicleId, vin, make, model, year, odometer, ... } | null,
      "generatedAt": "..."
    }

Lookup order:
    1. Claims -> email
    2. Scan cms-prod-storage-drivers where cognitoEmail = email
    3. If driver found and has assignedVehicleId, GetItem on vehicles

404-equivalent (driver null) rather than 401 when no matching driver
exists — the iOS app should fall back to demo defaults in that case
rather than crash.
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

_DRIVERS_TABLE = os.environ.get("CMS_DRIVERS_TABLE", f"cms-{os.environ.get('CMS_STAGE', 'dev')}-storage-drivers")
_VEHICLES_TABLE = os.environ.get("CMS_VEHICLES_TABLE", f"cms-{os.environ.get('CMS_STAGE', 'dev')}-storage-vehicles")

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


def _extract_email(event: dict) -> str | None:
    """Pull the authenticated user's email from the Cognito authorizer claims."""
    try:
        claims = (event.get("requestContext", {})
                       .get("authorizer", {})
                       .get("claims", {}))
        return claims.get("email") or claims.get("cognito:username")
    except Exception:  # noqa: BLE001
        return None


def _resolve_driver_by_email(email: str) -> dict | None:
    """Scan drivers for a row whose cognitoEmail matches. Table is ~75 rows so
    scan-with-filter is fine."""
    table = _get_ddb().Table(_DRIVERS_TABLE)
    try:
        resp = table.scan(
            FilterExpression=Attr("cognitoEmail").eq(email),
            # No Limit — Limit applies before FilterExpression, which produces
            # false-empty results if the matching row sits past the first N
            # scanned items.
        )
    except Exception as e:  # noqa: BLE001
        log.warning("driver lookup failed for email=%s: %s", email, e)
        return None
    items = resp.get("Items") or []
    if not items:
        return None
    # Curated subset for the response — omit internal fields.
    item = items[0]
    return {
        k: item.get(k)
        for k in (
            "driverId", "firstName", "lastName", "email", "cognitoEmail",
            "phone", "homeBase", "safetyScore", "licenseClass",
            "licenseState", "licenseNumber", "licenseExpiry", "hireDate",
            "yearsExperience", "totalMiles", "totalTrips", "incidentCount",
            "lastTripDate", "certifications", "status", "assignedVehicleId",
        )
        if k in item
    }


def _load_vehicle(vehicle_id: str) -> dict | None:
    """Same curated subset as api-vehicle-context so responses are
    consistent across both endpoints."""
    table = _get_ddb().Table(_VEHICLES_TABLE)
    resp = table.get_item(Key={"vehicleId": vehicle_id})
    item = resp.get("Item")
    if not item:
        return None
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
            # Added 2026-08-21. `api-vehicle-context` projected these on
            # 2026-08-19 for the EV battery card; this sibling was not updated,
            # so the two endpoints disagreed despite the docstring above
            # promising they match. iOS reads `batterySoh` from
            # `session.currentVehicle`, which is populated HERE and not by
            # vehicle-context — so the Home stat cell, the Vehicle tab signal row
            # and the battery card all rendered "—" on a vehicle whose row
            # carries `batterySoh: 94`.
            #
            # Third instance of this exact class in three weeks (see the
            # 2026-07-29 note above, and the tenant-config `acquire` omission).
            # The two projections are now pinned against each other by
            # `test_projection_parity.py` rather than by this comment — a
            # contract asserted only in prose is one that keeps drifting.
            "batterySoh", "batteryCapacityKwh",
        )
        if k in item
    }


def handler(event, context):
    email = _extract_email(event)
    if not email:
        log.warning("drivers/me: could not extract email from claims")
        return _respond(401, {"error": "no email in JWT claims"})

    driver = _resolve_driver_by_email(email)
    if not driver:
        log.info("drivers/me: no driver record for email=%s", email)
        # 200 with nulls — iOS falls back to demo defaults. 404 would crash
        # the existing demo@meridian-fleet.example user path.
        from datetime import datetime, timezone
        return _respond(200, {
            "driver": None,
            "vehicle": None,
            "email": email,
            "generatedAt": datetime.now(timezone.utc).isoformat(
                timespec="milliseconds"
            ).replace("+00:00", "Z"),
        })

    vehicle = None
    assigned_id = driver.get("assignedVehicleId")
    if assigned_id:
        vehicle = _load_vehicle(assigned_id)

    from datetime import datetime, timezone
    return _respond(200, {
        "driver": driver,
        "vehicle": vehicle,
        "email": email,
        "generatedAt": datetime.now(timezone.utc).isoformat(
            timespec="milliseconds"
        ).replace("+00:00", "Z"),
    })
