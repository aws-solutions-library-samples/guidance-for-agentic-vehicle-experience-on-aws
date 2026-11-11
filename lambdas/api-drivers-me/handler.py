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
    1. Claims -> email, only when email_verified is true (else 403; no email -> 401).
       No cognito:username fallback (issue 2026-09-24-drivers-me-trusts-unverified-
       client-writable-email).
    2. Scan cms-{stage}-storage-drivers where cognitoEmail = email
    3. If driver found and has assignedVehicleId, GetItem on vehicles

404-equivalent (driver null) rather than 401 when no matching driver
exists — the iOS app should fall back to demo defaults in that case
rather than crash.
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

log = logging.getLogger()
log.setLevel(logging.INFO)

_DRIVERS_TABLE = os.environ.get("CMS_DRIVERS_TABLE", f"cms-{os.environ.get('CMS_STAGE', 'dev')}-storage-drivers")
_VEHICLES_TABLE = os.environ.get("CMS_VEHICLES_TABLE", f"cms-{os.environ.get('CMS_STAGE', 'dev')}-storage-vehicles")
# Stage C: vehicle snapshot table (pk: vehicleId, one item per car, written daily).
_SNAPSHOT_TABLE = os.environ.get(
    "CMS_VEHICLE_SNAPSHOT_TABLE",
    f"cms-{os.environ.get('CMS_STAGE', 'dev')}-storage-vehicle-snapshot",
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


def _verified_email(event: dict) -> tuple[str | None, str]:
    """The caller's email, but only when Cognito says it is verified.

    Returns ``(email, "ok")``, or ``(None, reason)`` with reason ``"no_email"`` or
    ``"email_unverified"``.

    Why both checks (issue ``2026-09-24-drivers-me-trusts-unverified-client-writable-email``):
    the pool clients must be able to write ``email`` (federated sign-in maps it), and on a
    pool without ``AttributesRequireVerificationBeforeUpdate = ["email"]`` an update takes
    effect at once with ``email_verified`` set to false. This route keys a driver's record on
    the email, so an unverified value is a claim to someone else's record. The
    ``cognito:username`` fallback is gone for the same reason: it is not an email, and it
    carries no verification at all.

    API Gateway's Cognito authorizer passes claims as strings (``"true"``); a raw JWT
    carries a JSON boolean. Exactly those two mean verified. Anything else, including a
    missing claim, ``"True"`` or ``"1"``, does not.
    """
    try:
        claims = (event.get("requestContext", {})
                       .get("authorizer", {})
                       .get("claims", {}))
    except Exception:  # noqa: BLE001
        return None, "no_email"
    if not isinstance(claims, dict):
        return None, "no_email"
    email = claims.get("email")
    if not isinstance(email, str) or not email.strip():
        return None, "no_email"
    verified = claims.get("email_verified")
    if not (verified is True or verified == "true"):
        return None, "email_unverified"
    return email.strip(), "ok"


_MAX_SCAN_PAGES = 50  # guard against unbounded pagination on very large tables


def _resolve_driver_by_email(email: str) -> dict | None:
    """Scan drivers for a row whose cognitoEmail matches, following pagination.

    The table has grown past 1 MB (7 k+ rows as of 2026-10-24); a single
    unpaginated scan silently misses rows beyond page 1.
    issues/2026-10-24-driver-lookup-single-page-scan/
    """
    table = _get_ddb().Table(_DRIVERS_TABLE)
    kw: dict = {"FilterExpression": Attr("cognitoEmail").eq(email)}
    for _ in range(_MAX_SCAN_PAGES):
        try:
            resp = table.scan(**kw)
        except Exception as e:  # noqa: BLE001
            log.warning("driver lookup failed for email=%s: %s", email, e)
            return None
        items = resp.get("Items") or []
        if items:
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
        lek = resp.get("LastEvaluatedKey")
        if not lek:
            return None
        kw["ExclusiveStartKey"] = lek
    log.warning("driver lookup exceeded page cap (%d) for email=%s", _MAX_SCAN_PAGES, email)
    return None


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

    iOS declares odometer and mileage as Int? — a fractional Decimal from the snapshot
    makes Swift's JSONDecoder raise DecodingError.dataCorrupted and the whole response
    is rejected, leaving currentDriver/currentVehicle nil.  (C1 fix, review cycle 1.)
    The exact reading is still available in values.odometer.value.

    S2 (cycle 2): return None for non-numeric types (str, bool, etc.) rather than
    raising TypeError → 500.  bool is excluded explicitly (isinstance(True, int) is True).
    """
    if val is None:
        return None
    if isinstance(val, bool) or not isinstance(val, (int, float, Decimal)):
        return None
    return int(math.floor(val))


def _is_bev(fuel_type: str | None) -> bool:
    """Whether this vehicle has a BEV traction pack.

    Mirrors api-vehicle-context._is_bev and iOS VehicleTabView.isElectric(_:).
    Non-EVs must NOT have batteryCharge substituted for fuelLevel (S3 / decisions.md
    2026-10-24 01:25Z): a non-EV row already carries a meaningful fuelLevel and there
    is no chargeLevel to substitute for it.
    """
    ft = (fuel_type or "").strip().lower()
    return ft in ("bev", "electric", "ev")


def handler(event, context):
    email, reason = _verified_email(event)
    if email is None:
        if reason == "email_unverified":
            # Refused, not "no driver": a 200 with nulls would tell iOS the user has no
            # vehicle. The email itself is not logged; it is the unverified value.
            log.warning("drivers/me: refused, email claim is not verified")
            return _respond(403, {"error": "email not verified"})
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

    # Stage C: overlay snapshot values on the vehicle (precedence: snapshot > row).
    # The snapshot pk is vehicleId. Rules (brief Stage C):
    #   - item present: set odometer, mileage, fuelLevel, batterySoh from snapshot
    #     (each may be null — a null snapshot value stays null, not the row value).
    #   - no item: leave row values unchanged, snapshotStatus "none".
    #   - read error: null odometer, mileage, batterySoh; null fuelLevel only for a BEV
    #     (non-EV keeps the row value — no snapshot substitute for fuel); still 200.
    snapshot_status = "none"
    values: "dict | None" = None
    snapshot_meta: "dict | None" = None
    if vehicle is not None:
        vid = vehicle.get("vehicleId") or assigned_id
        snapshot_item, snapshot_status = _load_snapshot(vid)
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
            # batteryCharge fills fuelLevel only for BEVs.  A non-EV row already carries
            # a meaningful fuelLevel; the snapshot has no fuel reading to substitute for it.
            # values.batteryCharge always carries the raw snapshot value.
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

    from datetime import datetime, timezone
    return _respond(200, {
        "driver": driver,
        "vehicle": vehicle,
        "email": email,
        "generatedAt": datetime.now(timezone.utc).isoformat(
            timespec="milliseconds"
        ).replace("+00:00", "Z"),
        "snapshotStatus": snapshot_status,
        "values": values,
        "snapshot": snapshot_meta,
    })
