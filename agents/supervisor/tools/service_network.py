"""service_network tool — find nearest service centers by capability.

Queries the ADP service-network DynamoDB table to find service centers
that can handle a specific repair type for a given vehicle make.

Environment:
    ADP_SERVICE_NETWORK_TABLE — DynamoDB table name (default: adp-dev-service-network)
    ADP_STAGE — ADP deployment stage (default: dev)
"""

import os
import logging
import math
from typing import Any

import boto3
from strands import tool

from agents.supervisor.session import SessionContext

log = logging.getLogger(__name__)

_STAGE = os.environ.get("ADP_STAGE", "dev")
_TABLE = os.environ.get("ADP_SERVICE_NETWORK_TABLE", f"adp-{_STAGE}-service-network")


def make_service_network_tool(ctx: SessionContext | None = None):
    """Build the find_service_center tool.

    `ctx` is optional for backwards compatibility; when present we
    read the driver's location from `ctx.client_latitude` /
    `ctx.client_longitude` instead of trusting an LLM-provided
    lat/lng. Nova was observed passing those parameters inconsistently
    (omitting them, surrounding them with whitespace, or replacing
    them with arbitrary defaults) and the resulting "0 nearby"
    replies confused drivers who could see the candidate centers on
    their map. Pulling from ctx makes the answer deterministic.
    """
    @tool
    def find_service_center(
        capability: str,
        vehicle_make: str = "",
        max_results: int = 3,
    ) -> dict[str, Any]:
        """Find nearby service centers that can handle a specific repair.

        Use this when:
        - The driver needs to schedule service and you need to suggest a location
        - You need to check if a specific repair capability is available nearby
        - The driver asks "where can I get this fixed?"

        The tool automatically uses the driver's current location from
        the session — do NOT pass latitude/longitude, you do not need
        to provide them.

        Args:
            capability: Type of service needed (e.g. "brakes", "transmission",
                "oil-change", "recall-service", "diagnostics", "body-work",
                "tire-pressure", "tire-inflation").
            vehicle_make: Optional filter to centers that service this make
                (e.g. "Chevrolet", "Ford"). Pass an empty string to skip
                make filtering.
            max_results: Maximum centers to return (1-5, default 3).

        Returns:
            Dict with found: bool, centers: list of matching service centers
            with name, address, phone, hours, rating, wait time, and
            nextAvailableSlots (list of upcoming appointment times).
        """
        # Sanitize: strip whitespace, lowercase capability for matching.
        # Nova was observed passing values like " tire-inflation " (with
        # spaces) and " Ford " (with spaces), which broke exact-match
        # filters and produced spurious zero-result responses.
        capability = (capability or "").strip().lower()
        vehicle_make = (vehicle_make or "").strip()
        try:
            max_results = max(1, min(int(max_results), 5))
        except (TypeError, ValueError):
            max_results = 3

        # Pull location from session context — single source of truth,
        # same coords the iOS Vehicle map shows. The two valid values:
        #   - a non-zero float pair: bidi_app's pre-load resolved either
        #     the iOS-supplied Latitude/Longitude headers or the
        #     `/vehicles/{id}/live-state` endpoint (Redis-first, DDB
        #     fallback). This is the vehicle's last-known location.
        #   - None: bidi_app's pre-load tried both paths and both
        #     failed. The tool MUST NOT silently fall back to (0,0)
        #     here — that produced the "Tukwila WA returned for a
        #     vehicle nowhere near Tukwila" symptom (see
        #     `~/guidance-for-connected-vehicle-experience-on-aws/issues/2026-05-28-find-service-center-ignores-vehicle-location/`).
        # The fix: pass None through. The distance-sort branch below
        # is gated on `if latitude and longitude` so None correctly
        # disables the sort. When sort is disabled we return an
        # explicit "location unavailable" narration rather than a
        # DDB-scan-order pick.
        latitude: float | None = ctx.client_latitude if ctx else None
        longitude: float | None = ctx.client_longitude if ctx else None
        # Treat 0.0 as missing — defense-in-depth against any caller
        # that sets exactly zero. (`bidi_app._parse_coord` already does
        # this at the entry point, but a future caller might bypass.)
        if latitude == 0.0:
            latitude = None
        if longitude == 0.0:
            longitude = None
        coord_source = "ctx" if (latitude is not None and longitude is not None) else "missing"
        # Distance unit is tenant data, not a global. The internal haversine
        # returns miles; a tenant serving a metric market gets km. Hardcoding
        # miles quoted "2.2 miles" to a customer in a country that has never
        # used them.
        unit = (getattr(ctx, "distance_unit", None) or "mi") if ctx else "mi"
        unit = "km" if str(unit).lower() in ("km", "kilometre", "kilometer") else "mi"
        _MI_TO_KM = 1.60934
        log.info(
            "find_service_center: capability=%r make=%r segment=%s "
            "coords=(%s,%s) source=%s vehicle_id=%s",
            capability, vehicle_make,
            (ctx.tenant_segment if ctx else "fleet"),
            latitude, longitude, coord_source,
            (ctx.vehicle_id if ctx else None),
        )
        try:
            ddb = boto3.resource("dynamodb", region_name=os.environ.get("ADP_REGION", "us-east-1")).Table(_TABLE)
            resp = ddb.scan()
            items = resp.get("Items", [])

            # Map specific repair terms to broad capabilities
            capability_map = {
                "turbocharger": "engine", "turbo": "engine", "boost": "engine",
                "alternator": "engine", "starter": "engine", "fuel pump": "engine",
                "abs": "brakes", "wheel speed": "brakes", "brake pad": "brakes",
                "tire pressure": "tires", "tpms": "tires", "rotation": "tires",
                "tire-service": "tires", "tire service": "tires", "tire": "tires",
                # Phrasings drivers (and Nova) actually use for the same fix
                "tire-pressure": "tires", "tire-pressure-check": "tires",
                "tire-inflation": "tires", "tire inflation": "tires",
                "inflate": "tires", "inflation": "tires",
                "tire fill": "tires", "tire-fill": "tires",
                "tires": "tires",
                "a/c": "hvac", "air conditioning": "hvac", "heater": "hvac",
                "battery": "electrical", "charging": "electrical", "voltage": "electrical",
                "shock": "suspension", "strut": "suspension", "alignment": "suspension",
            }
            # Normalise whitespace/underscores to hyphens before mapping. The
            # model naturally writes "test ride" while the records use
            # "test-ride"; that single character silently returned zero dealers
            # for a test-ride request even though every dealer offers it.
            cap_raw = (capability or "").strip().lower()
            cap_norm = cap_raw.replace("_", "-").replace(" ", "-")
            capability_map.update({
                "test-ride": "test-ride", "testride": "test-ride",
                "test-drive": "test-ride", "demo-ride": "test-ride",
                "sales": "test-ride", "purchase": "test-ride",
                "scheduled-service": "scheduled-service",
                "service": "scheduled-service", "servicing": "scheduled-service",
            })
            capability = cap_norm
            search_cap = capability_map.get(cap_norm, cap_norm)
            # Fuzzy fallback: if no exact match, check substring matches
            if search_cap == capability and search_cap not in [c for item in items for c in item.get("capabilities", [])]:
                for key, val in capability_map.items():
                    if key in capability or capability in key:
                        search_cap = val
                        break

            # Filter by capability
            matches = [c for c in items if search_cap in c.get("capabilities", [])]

            # Persona/segment filter. The tenant config marks each tenant
            # as "fleet", "oem", or "rental"; the same network table
            # serves all three personas, but each persona expects a
            # different mix of centers:
            #
            #   fleet   — broadest mix: dealers, independents, fleet-
            #             service depots, quick-service. Drivers want
            #             the closest competent shop.
            #   oem     — only the brand's authorized dealerships
            #             (type=="dealer" AND brandsServiced contains
            #             the vehicle's make). OEM apps steer drivers
            #             to the dealer network.
            #   rental  — like fleet but biased toward national chains
            #             and quick-service (renters are mid-trip and
            #             want predictable, fast turnaround).
            #
            # Segment lives on ctx.tenant_segment (defaults to "fleet"
            # when ctx is None or lookup failed). vehicle_make is the
            # LLM-supplied filter; for OEM we additionally REQUIRE it
            # because "OEM dealer" only makes sense scoped to a brand.
            segment = (ctx.tenant_segment if ctx else "fleet")
            if segment == "oem":
                matches = [c for c in matches if c.get("type") == "dealer"]
                # If make was supplied, restrict to that brand's dealers.
                # If not supplied, leave all dealers — better than empty.
                # Prefer the make resolved from the vehicle record. The model
                # supplied the TENANT ID ("ford") as the make, which matched no
                # dealer's brandsServiced and returned zero results. The tenant
                # id is an internal slug with no necessary relationship to what
                # the vehicle actually is, so the record wins and the
                # LLM-supplied value is only a fallback.
                ctx_make = (getattr(ctx, "vehicle_make", None) or "") if ctx else ""
                effective_make = ctx_make or vehicle_make
                tenant_slug = (ctx.tenant_id or "").lower() if ctx else ""
                if (not ctx_make) and effective_make \
                        and effective_make.lower() == tenant_slug:
                    # Guard the case where no record make was available and the
                    # model echoed the slug: dropping the filter returns all
                    # authorised dealers, which beats returning none.
                    log.warning(
                        "find_service_center: ignoring make=%r (equals tenant id)",
                        effective_make,
                    )
                    effective_make = ""
                if effective_make:
                    vm_norm = effective_make.lower()
                    matches = [
                        c for c in matches
                        if any(vm_norm == b.lower() for b in c.get("brandsServiced", []))
                    ]
            elif segment == "rental":
                # Bias toward national chains + quick-service. Body shops
                # and one-off independents are de-prioritized but not
                # excluded outright (a renter with no other option still
                # gets served).
                preferred_types = {"quick-service", "tire-specialist", "dealer", "fleet-service"}
                preferred = [c for c in matches if c.get("type") in preferred_types]
                rest = [c for c in matches if c.get("type") not in preferred_types]
                matches = preferred + rest
                if vehicle_make:
                    vm_norm = vehicle_make.lower()
                    matches = [
                        c for c in matches
                        if any(vm_norm == b.lower() for b in c.get("brandsServiced", []))
                    ]
            else:  # fleet (default)
                # Broad mix; LLM-supplied vehicle_make is an optional
                # filter, not a requirement.
                if vehicle_make:
                    vm_norm = vehicle_make.lower()
                    matches = [
                        c for c in matches
                        if any(vm_norm == b.lower() for b in c.get("brandsServiced", []))
                    ]

            # Sort by distance if coordinates provided. When we know the
            # vehicle's brand, also boost authorized dealers that service
            # that brand — for a Ford with a check-engine fault, the
            # owner generally wants the Ford dealer over a generic
            # independent shop unless the dealer is materially farther
            # away. Two-key sort:
            #   (priority bucket, distance)
            # Bucket 0: brand-matched dealer  (best for warranty + spec)
            # Bucket 1: anything else         (rank by distance only)
            # Within each bucket the closest wins. We don't gate the
            # bucketing on max distance because the brand-match bonus
            # is only worth so much; if the closest brand dealer is
            # 200mi away, the user will say so and try the independent.
            if latitude and longitude:
                vm_norm = (vehicle_make or "").strip().lower()
                for c in matches:
                    c["_distance"] = _haversine(
                        latitude, longitude,
                        float(c.get("latitude", 0)), float(c.get("longitude", 0))
                    )
                    is_dealer = c.get("type") == "dealer"
                    brands = [b.lower() for b in c.get("brandsServiced", []) or []]
                    brand_dealer = is_dealer and bool(vm_norm) and vm_norm in brands
                    c["_brand_priority"] = 0 if brand_dealer else 1
                matches.sort(key=lambda c: (c["_brand_priority"], c["_distance"]))

            results = []
            for c in matches[:max_results]:
                # Generate random availability slots for the next few days
                import random
                from datetime import datetime, timedelta
                today = datetime.now()
                slots = []
                for day_offset in range(1, 4):
                    day = today + timedelta(days=day_offset)
                    if day.weekday() == 6:  # Skip Sunday
                        continue
                    hour = random.choice([8, 9, 10, 11, 13, 14, 15])
                    slots.append(f"{day.strftime('%A %b %d')} at {hour}:00 AM" if hour < 12
                                 else f"{day.strftime('%A %b %d')} at {hour - 12}:00 PM")
                results.append({
                    "centerId": c["centerId"],
                    "name": c["name"],
                    "type": c["type"],
                    "address": f"{c['address']}, {c['city']}, {c['state']} {c['zipCode']}",
                    "phone": c["phone"],
                    "hours": c.get("hours", {}),
                    "rating": float(c.get("rating", 0)),
                    "averageWaitDays": c.get("averageWaitDays", 0),
                    "fleetDiscount": c.get("fleetDiscount", False),
                    # `distanceMiles` retained for existing consumers; `distance`
                    # + `distanceUnit` are the unit-aware pair.
                    "distanceMiles": round(c.get("_distance", 0), 1) if "_distance" in c else None,
                    "distance": (
                        round(c["_distance"] * (_MI_TO_KM if unit == "km" else 1.0), 1)
                        if "_distance" in c else None
                    ),
                    "distanceUnit": unit,
                    "nextAvailableSlots": slots,
                })

            results_found = len(results) > 0
            if not results_found:
                return {
                    "found": False,
                    "centers": [],
                    "narration": "I couldn't find a service center for that nearby. Want me to escalate to a human agent?",
                }

            # If location was unknown we built `results` from the
            # filtered/segment-sorted list WITHOUT a distance sort
            # (the `if latitude and longitude` gate above is False
            # when either coord is None). The first entry is therefore
            # arbitrary in DDB-scan order — we MUST NOT recommend it
            # as the closest. Surface the location-unavailable state
            # explicitly so Nova asks the driver to share location
            # rather than narrating a wrong "found {arbitrary} about
            # {nonsense} miles from you."
            #
            # Why we don't just return found=False: the centers list
            # is still useful — the iOS reasoning drawer can show
            # "tool returned N centers, location unknown" and the
            # driver can decide whether to share location or pick
            # one manually. Returning empty centers would hide that.
            # See `~/guidance-for-connected-vehicle-experience-on-aws/issues/2026-05-28-find-service-center-ignores-vehicle-location/`.
            if latitude is None or longitude is None:
                log.warning(
                    "find_service_center: location unavailable (ctx coords None) — "
                    "returning %d centers without distance ranking; vehicle_id=%s",
                    len(results), (ctx.vehicle_id if ctx else None),
                )
                return {
                    "found": True,
                    "centers": results,
                    "locationKnown": False,
                    "narration": (
                        "I can't pinpoint your vehicle's location right now, "
                        "so I can't tell you which service center is closest. "
                        "Can you confirm your city or share your location?"
                    ),
                }

            # Build a narration string for the FIRST center — Nova
            # Sonic v2 reliably verbalizes string fields but goes
            # silent on complex dict returns. The system prompt
            # instructs Nova to speak this `narration` field.
            top = results[0]
            distance_phrase = ""
            d = top.get("distance")
            if d is not None:
                distance_phrase = (
                    f", about {round(d)} {'km' if unit == 'km' else 'miles'} from you"
                )
            slots = top.get("nextAvailableSlots") or []
            slot_phrase = ""
            if slots:
                # First slot only — keeps the narration short.
                slot_phrase = f". They can see you {slots[0]}"
            narration = (
                f"I found {top['name']}{distance_phrase}{slot_phrase}. "
                f"Want me to book it?"
            )
            return {
                "found": True,
                "centers": results,
                "locationKnown": True,
                "narration": narration,
            }

        except Exception as e:
            log.warning("Service network lookup failed: %s", e)
            return {"found": False, "centers": [], "error": str(e)}

    return find_service_center


def _haversine(lat1, lon1, lat2, lon2) -> float:
    """Distance in miles between two lat/lon points."""
    R = 3959  # Earth radius in miles
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat/2)**2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon/2)**2
    return R * 2 * math.asin(math.sqrt(a))
