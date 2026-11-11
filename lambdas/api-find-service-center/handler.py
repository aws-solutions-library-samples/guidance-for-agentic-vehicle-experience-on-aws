"""POST /find-service-center

Native-app booking flow's service-center lookup. Mirrors the voice
agent's `find_service_center` tool (agents/supervisor/tools/service_network.py)
so the iOS native booking screen and Nova return the same centers
ranked the same way. Logic is intentionally duplicated rather than
shared today; a future refactor should pull the capability map and
haversine into lambdas/shared/.

Request (JSON):
    {
      "capability": "tire-inflation",        // required, fuzzy-mapped
      "latitude":  41.8781,                  // required for distance sort
      "longitude": -87.6298,
      "segment":   "fleet" | "oem" | "rental", // optional, default "fleet"
      "vehicleMake": "Ford",                  // optional filter
      "maxResults": 3                         // optional, 1..5, default 3
    }

Response:
    {
      "found": 3,
      "centers": [
        { centerId, name, type, address, phone, hours, rating,
          averageWaitDays, fleetDiscount, distanceMiles,
          nextAvailableSlots: ["Tuesday May 19 at 10:00 AM", ...] },
        ...
      ]
    }

Auth: Cognito authorizer (same as the rest of the API). The body's
fields aren't sensitive — anyone signed in to a tenant can list service
centers near a coordinate they pass.
"""
from __future__ import annotations

import json
import logging
import math
import os
import random
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

import boto3

log = logging.getLogger()
log.setLevel(logging.INFO)

_TABLE_NAME = os.environ.get("ADP_SERVICE_NETWORK_TABLE", "adp-dev-service-network")

# The ADP service network lives in a DIFFERENT REGION from this Lambda: the table is
# us-east-1, this function runs us-west-2. `boto3.resource("dynamodb")` with no region
# resolves to the Lambda's own region, so every Scan raised ResourceNotFoundException and
# the Service tab's dealer list came back empty — while the SAME lookup over voice worked,
# because `agents/supervisor/tools/service_network.py` does pin the region. That asymmetry
# is the cost of the duplication this module's own docstring notes: the agent tool got the
# cross-region fix and its REST twin never did.
#
# Default matches the voice tool exactly, so the two cannot drift apart again silently.
_ADP_REGION = os.environ.get("ADP_REGION", "us-east-1")
_ddb = None


def _get_table():
    global _ddb
    if _ddb is None:
        _ddb = boto3.resource("dynamodb", region_name=_ADP_REGION)
    return _ddb.Table(_TABLE_NAME)


# Same map the voice tool uses. Keep these lists in sync until the
# shared module exists.
_CAPABILITY_MAP = {
    "turbocharger": "engine", "turbo": "engine", "boost": "engine",
    "alternator": "engine", "starter": "engine", "fuel pump": "engine",
    "abs": "brakes", "wheel speed": "brakes", "brake pad": "brakes",
    "tire pressure": "tires", "tpms": "tires", "rotation": "tires",
    "tire-service": "tires", "tire service": "tires", "tire": "tires",
    "tire-pressure": "tires", "tire-pressure-check": "tires",
    "tire-inflation": "tires", "tire inflation": "tires",
    "inflate": "tires", "inflation": "tires",
    "tire fill": "tires", "tire-fill": "tires", "tires": "tires",
    "a/c": "hvac", "air conditioning": "hvac", "heater": "hvac",
    "battery": "electrical", "charging": "electrical", "voltage": "electrical",
    "shock": "suspension", "strut": "suspension", "alignment": "suspension",
}


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


def _haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Distance in miles between two lat/lon points."""
    R = 3959  # Earth radius in miles
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (math.sin(dlat / 2) ** 2
         + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2))
         * math.sin(dlon / 2) ** 2)
    return R * 2 * math.asin(math.sqrt(a))


def _resolve_capability(items: list[dict], cap: str) -> str:
    """Map LLM/UI-supplied capability to a value present on the centers'
    capabilities lists. Two-pass: exact-map first, then fuzzy substring
    match against the map keys."""
    cap = (cap or "").strip().lower()
    if cap in _CAPABILITY_MAP:
        return _CAPABILITY_MAP[cap]
    # If the raw value already matches some capability in the data, use it.
    if any(cap in item.get("capabilities", []) for item in items):
        return cap
    for key, val in _CAPABILITY_MAP.items():
        if key in cap or cap in key:
            return val
    # Space/hyphen tolerance, last resort before giving up. The data uses hyphenated tokens
    # ("oil-change", "ev-service"), but a caller — an LLM especially, and this endpoint is
    # reached by a voice tool — will naturally say "oil change". Without this the spaced form
    # falls through unchanged and the capability filter drops EVERY centre, which surfaces as
    # a 200 with `found: 0` and reads as "no dealers near you" rather than as a bad
    # parameter. Cost of being wrong here is nil: an unmatched token still returns [].
    hyphenated = cap.replace(" ", "-")
    if hyphenated != cap and any(hyphenated in item.get("capabilities", []) for item in items):
        return hyphenated
    return cap  # No mapping; downstream filter will return [] cleanly.


def _generate_slots() -> list[str]:
    """Three demo slots over the next 1-3 weekdays. Skips Sunday.
    Matches the voice tool's slot format so iOS rendering is identical."""
    today = datetime.now()
    slots: list[str] = []
    for day_offset in range(1, 4):
        day = today + timedelta(days=day_offset)
        if day.weekday() == 6:
            continue
        hour = random.choice([8, 9, 10, 11, 13, 14, 15])
        if hour < 12:
            slots.append(f"{day.strftime('%A %b %d')} at {hour}:00 AM")
        else:
            slots.append(f"{day.strftime('%A %b %d')} at {hour - 12}:00 PM")
    return slots


def _shape_center(c: dict, distance: float | None) -> dict:
    return {
        "centerId": c.get("centerId"),
        "name": c.get("name"),
        "type": c.get("type"),
        "address": (
            f"{c.get('address', '')}, {c.get('city', '')}, "
            f"{c.get('state', '')} {c.get('zipCode', '')}"
        ).strip(", "),
        "phone": c.get("phone"),
        "hours": c.get("hours", {}),
        "rating": float(c.get("rating", 0)) if c.get("rating") is not None else None,
        "averageWaitDays": int(c.get("averageWaitDays", 0))
        if c.get("averageWaitDays") is not None else None,
        "fleetDiscount": bool(c.get("fleetDiscount", False)),
        "distanceMiles": round(distance, 1) if distance is not None else None,
        "brandsServiced": c.get("brandsServiced", []),
        "nextAvailableSlots": _generate_slots(),
    }


def handler(event: dict, _context) -> dict:
    """API Gateway proxy integration entry point."""
    try:
        body = json.loads(event.get("body") or "{}")
    except json.JSONDecodeError:
        return _resp(400, {"error": "Body must be JSON"})

    capability = (body.get("capability") or "").strip()
    if not capability:
        return _resp(400, {"error": "capability is required"})

    try:
        latitude = float(body.get("latitude")) if body.get("latitude") is not None else None
        longitude = float(body.get("longitude")) if body.get("longitude") is not None else None
    except (TypeError, ValueError):
        return _resp(400, {"error": "latitude and longitude must be numeric"})

    vehicle_make = (body.get("vehicleMake") or "").strip()
    segment = (body.get("segment") or "fleet").strip().lower()
    if segment not in ("fleet", "oem", "rental"):
        segment = "fleet"
    try:
        max_results = max(1, min(int(body.get("maxResults") or 3), 5))
    except (TypeError, ValueError):
        max_results = 3

    log.info(
        "find-service-center: cap=%r make=%r segment=%s lat=%s lng=%s",
        capability, vehicle_make, segment, latitude, longitude,
    )

    table = _get_table()
    items = table.scan().get("Items", [])
    search_cap = _resolve_capability(items, capability)
    matches = [c for c in items if search_cap in c.get("capabilities", [])]

    # Persona filter — same logic as the voice tool's segment branch.
    if segment == "oem":
        matches = [c for c in matches if c.get("type") == "dealer"]
        if vehicle_make:
            vm = vehicle_make.lower()
            matches = [
                c for c in matches
                if any(vm == b.lower() for b in c.get("brandsServiced", []))
            ]
    elif segment == "rental":
        preferred_types = {"quick-service", "tire-specialist", "dealer", "fleet-service"}
        preferred = [c for c in matches if c.get("type") in preferred_types]
        rest = [c for c in matches if c.get("type") not in preferred_types]
        matches = preferred + rest
        if vehicle_make:
            vm = vehicle_make.lower()
            matches = [
                c for c in matches
                if any(vm == b.lower() for b in c.get("brandsServiced", []))
            ]
    else:  # fleet
        if vehicle_make:
            vm = vehicle_make.lower()
            matches = [
                c for c in matches
                if any(vm == b.lower() for b in c.get("brandsServiced", []))
            ]

    # Sort by distance if both coords are known.
    if latitude is not None and longitude is not None:
        for c in matches:
            try:
                c["_distance"] = _haversine(
                    latitude, longitude,
                    float(c.get("latitude", 0)), float(c.get("longitude", 0)),
                )
            except (TypeError, ValueError):
                c["_distance"] = float("inf")
        matches.sort(key=lambda c: c["_distance"])

    centers = [
        _shape_center(c, c.get("_distance") if "_distance" in c else None)
        for c in matches[:max_results]
    ]
    return _resp(200, {"found": len(centers), "centers": centers})
