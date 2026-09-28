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
import re
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
        near_location: str = "",
    ) -> dict[str, Any]:
        """Find nearby service centers that can handle a specific repair.

        Use this when:
        - The driver needs to schedule service and you need to suggest a location
        - You need to check if a specific repair capability is available nearby
        - The driver asks "where can I get this fixed?"

        The tool uses the VEHICLE's last-known location from the session
        by default — do NOT pass latitude/longitude, you do not need to
        provide them.

        The one exception is `near_location`: if the customer TELLS you
        where they are ("I'm in Nashville", "I'll be in Las Vegas next
        week", "any dealers near Austin?"), pass that city through.
        The vehicle's position is often not where the customer is — they
        may be travelling, or shopping for a vehicle they do not own yet
        — and in that case the stated location is the better answer.

        Args:
            capability: Type of service needed (e.g. "brakes", "transmission",
                "oil-change", "recall-service", "diagnostics", "body-work",
                "tire-pressure", "tire-inflation").
            vehicle_make: Optional filter to centers that service this make
                (e.g. "Chevrolet", "Ford"). Pass an empty string to skip
                make filtering.
            max_results: Maximum centers to return (1-5, default 3).
            near_location: City the customer SAID they are in or near
                (e.g. "Nashville", "Las Vegas", "Austin, TX"). Pass only
                when the customer stated a location; leave empty to use
                the vehicle's own position. When supplied and recognised,
                results are ranked from that city instead.

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

            # A location the customer STATED overrides the vehicle's position.
            #
            # The vehicle is frequently not where the customer is: they are
            # travelling, or shopping for something they do not own yet. Before
            # this, "I'm in Nashville" could not reach the lookup at all — the
            # tool had no location parameter and its docstring told the model not
            # to pass one — so the customer was answered from the vehicle's
            # coordinates while believing they had been heard. That is worse than
            # refusing: the addresses were real, just for the wrong city.
            #
            # Resolved against the cities present in the table rather than a
            # geocoder or a hardcoded map. Three reasons: no network call inside
            # a latency-bound tool, no second copy of coordinates to drift from
            # the records, and a match can only ever name a city we actually have
            # dealers in — so a successful resolution cannot promise a city the
            # network does not serve.
            stated = (near_location or "").strip()
            stated_label: str | None = None
            stated_unmatched: str | None = None
            if stated:
                resolved = _resolve_stated_location(items, stated)
                if resolved:
                    latitude, longitude, stated_label = resolved
                    coord_source = "stated"
                    log.info(
                        "find_service_center: using stated location %r -> %s (%s,%s)",
                        stated, stated_label, latitude, longitude,
                    )
                else:
                    # Do NOT silently fall back and rank from the vehicle: the
                    # customer would be handed another city's dealers as though
                    # they were the answer to what they asked. Record it so the
                    # narration can say so.
                    stated_unmatched = stated
                    log.info(
                        "find_service_center: stated location %r matched no city "
                        "in the network; ranking from vehicle position instead",
                        stated,
                    )

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

            # An UNRECOGNISED capability is a vocabulary gap, not a dealer gap.
            #
            # The model invents capability strings from the conversation. Asked to
            # book a test ride it passed `book-appointment` (observed live
            # 2026-08-19) — a value in no record and in no synonym above — so
            # `matches` came back empty and the agent told the customer
            # "I wasn't able to find a Meridian of Nashville dealer in our
            # system". The dealer existed. The word did not. A confidently wrong
            # explanation is worse than a vague one, and chasing synonyms forever
            # is a losing game because the vocabulary is generated, not fixed.
            #
            # So distinguish the two cases by asking whether the capability exists
            # ANYWHERE in the network:
            #   - present on some records, none survive the other filters
            #     → a real gap ("no dealer near you does body-work"). Keep empty.
            #   - present on NO record at all
            #     → unrecognised vocabulary. Drop the capability filter; the
            #       segment, type and brand filters below still apply, so an OEM
            #       booking request still returns only authorised brand dealers.
            known_caps = {
                cap for item in items for cap in (item.get("capabilities") or [])
            }
            capability_recognised = search_cap in known_caps
            if not capability_recognised:
                log.warning(
                    "find_service_center: capability %r (from %r) matches no "
                    "record in the network — treating as unrecognised vocabulary "
                    "and NOT filtering on it, rather than reporting no dealers",
                    search_cap, capability,
                )
                matches = list(items)

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
                # Availability slots.
                #
                # Seeded per (centerId, date) rather than freshly random on every
                # call. Unseeded `random.choice` re-rolled the calendar on each
                # invocation, so a conversation could offer "Wednesday at 1 PM"
                # and then, one turn later when the customer accepted it, report
                # that Wednesday 1 PM was not an available slot — observed live
                # 2026-08-19. From the customer's side the agent contradicts
                # itself; from a demo audience's side the product looks broken.
                #
                # A deterministic seed makes a given dealer's slots stable for a
                # given day while still varying across dealers and across days,
                # which is all the demo needs. Real availability would come from
                # the dealer's calendar; this is mock data and says so.
                import random
                from datetime import datetime, timedelta
                today = datetime.now()
                slots = []
                for day_offset in range(1, 4):
                    day = today + timedelta(days=day_offset)
                    if day.weekday() == 6:  # Skip Sunday
                        continue
                    rng = random.Random(f"{c['centerId']}:{day.date().isoformat()}")
                    hour = rng.choice([8, 9, 10, 11, 13, 14, 15])
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
                    "capabilityRecognised": capability_recognised,
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
                narration = (
                    "I can't pinpoint your vehicle's location right now, "
                    "so I can't tell you which service center is closest. "
                    "Can you confirm your city or share your location?"
                )
                if stated_unmatched:
                    # They DID tell us where they are; we just don't serve it.
                    # Saying "confirm your city" here would ask again for
                    # something they already answered.
                    narration = (
                        f"I don't have an authorised dealer listed in "
                        f"{stated_unmatched}, and I can't pinpoint your vehicle's "
                        "location either. Would another city work, or shall I have "
                        "an advisor find your nearest option?"
                    )
                return {
                    "found": True,
                    "centers": results,
                    "locationKnown": False,
                    "statedLocation": stated_unmatched,
                    "statedLocationMatched": False if stated_unmatched else None,
                    "narration": narration,
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
            # When the customer named the city, say the distance is from THERE.
            # "about 1 mile from you" is only true if we ranked from where they
            # said they were; attributing a vehicle-relative distance to the
            # customer is the mismatch this parameter exists to remove.
            origin_phrase = f" of {stated_label}" if stated_label else ""
            narration = (
                f"I found {top['name']}{distance_phrase}{slot_phrase}. "
                f"Want me to book it?"
            )
            if stated_label:
                narration = (
                    f"I found {top['name']}, about "
                    f"{round(d) if d is not None else '?'} "
                    f"{'km' if unit == 'km' else 'miles'} from the centre"
                    f"{origin_phrase}{slot_phrase}. Want me to book it?"
                )
            elif stated_unmatched:
                # Ranked from the vehicle, and the customer's city is not served.
                # Both facts have to reach them or the answer is misleading.
                narration = (
                    f"I don't have an authorised dealer listed in "
                    f"{stated_unmatched}. The nearest to your vehicle is "
                    f"{top['name']}{distance_phrase}{slot_phrase}. "
                    "Want me to book it, or try another city?"
                )
            return {
                "found": True,
                "centers": results,
                "locationKnown": True,
                "locationSource": coord_source,
                "statedLocation": stated_label or stated_unmatched,
                "statedLocationMatched": (
                    True if stated_label else (False if stated_unmatched else None)
                ),
                "narration": narration,
            }

        except Exception as e:
            log.warning("Service network lookup failed: %s", e)
            return {"found": False, "centers": [], "error": str(e)}

    return find_service_center


def _resolve_stated_location(
    items: list[dict], stated: str
) -> tuple[float, float, str] | None:
    """Resolve a customer-stated city to coordinates from the network itself.

    Returns ``(latitude, longitude, label)`` — the centroid of every record in
    the matched city — or ``None`` when nothing matches.

    Matching is against the ``city`` values already in the table, so a match can
    only name a city the network actually serves. Handles the shapes a customer
    or the model actually produces: ``"Nashville"``, ``"Nashville, TN"``,
    ``"nashville tennessee"``, ``"I'm in Nashville"``.

    Deliberately conservative — a wrong match is worse than no match, because it
    hands the customer confident directions to another city. Substring matching
    requires the city name to be at least 4 characters so short names cannot be
    matched incidentally out of a longer sentence.

    Punctuation is stripped with a UNICODE-aware class, not ``[^a-z0-9]``: the
    network already contains ``München``, and an ASCII-only strip turned that
    into ``m nchen`` and reported the city as unserved while seven centers sat
    in it.
    """
    text = re.sub(r"[^\w\s,]", " ", (stated or "").lower(), flags=re.UNICODE)
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return None
    # Strip a leading "i'm in" / "near" / "around" style preamble.
    head = text.split(",", 1)[0].strip()

    by_city: dict[str, list[dict]] = {}
    for c in items:
        city = str(c.get("city") or "").strip()
        if not city:
            continue
        by_city.setdefault(city, []).append(c)

    def _centroid(recs: list[dict], label: str):
        pts = []
        for r in recs:
            try:
                pts.append((float(r["latitude"]), float(r["longitude"])))
            except (KeyError, TypeError, ValueError):
                continue
        if not pts:
            return None
        return (
            sum(p[0] for p in pts) / len(pts),
            sum(p[1] for p in pts) / len(pts),
            label,
        )

    # Pass 1: exact city match on the whole string or the pre-comma head.
    for city, recs in by_city.items():
        cl = city.lower()
        if cl == text or cl == head:
            return _centroid(recs, city)

    # Pass 2: the city name appears in what the customer said. Longest city
    # first, so "Las Vegas" wins over a hypothetical "Vegas" record.
    for city, recs in sorted(by_city.items(), key=lambda kv: -len(kv[0])):
        cl = city.lower()
        if len(cl) >= 4 and cl in text:
            return _centroid(recs, city)

    return None


def _haversine(lat1, lon1, lat2, lon2) -> float:
    """Distance in miles between two lat/lon points."""
    R = 3959  # Earth radius in miles
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat/2)**2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon/2)**2
    return R * 2 * math.asin(math.sqrt(a))
