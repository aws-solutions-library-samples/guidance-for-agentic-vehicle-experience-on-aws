"""Resolve a vehicle's current coordinates for location-aware tools.

`find_service_center` deliberately does NOT resolve coordinates itself — it
reads `ctx.client_latitude` / `ctx.client_longitude` and returns an explicit
"location unavailable" narration when they are absent. That contract exists
because an earlier version fell back to (0, 0) and confidently recommended
service centres near Null Island; see
`issues/2026-05-28-find-service-center-ignores-vehicle-location/`.

The voice path satisfies that contract in `bidi_app._preload_vehicle_context`.
The text path (`agentcore_app`) never did, so every text-mode
`find_service_center` call had no coordinates and returned nothing — which is
why a test-ride request could not name a dealer.

Resolution order, matching the voice path:

1. Coordinates already on the session (client-supplied). Trusted as-is.
2. The live-state endpoint, which reads Redis first with a DynamoDB fallback —
   the same source the iOS Vehicle tab map and the CMS UI render, so the
   spoken/typed answer agrees with what the user is looking at.

Deliberately NOT the phone's GPS. The device and the vehicle are frequently in
different places (a demo simulator in one country, the vehicle in another), a
connected-vehicle platform is supposed to know where the *vehicle* is, and
client-supplied coordinates cannot be trusted for anything that matters.

NOTE: `bidi_app` still carries its own copy of this lookup. This module was
added rather than refactoring `bidi_app` because that module drives the voice
runtime shared with production and the change was made hours before a demo.
Migrating `bidi_app` to call this is tracked as follow-up work; until then treat
`bidi_app._fetch_live_vehicle_location` and this function as the same logic that
must change together.
"""

from __future__ import annotations

import logging
from typing import Any

import requests

log = logging.getLogger(__name__)

_TIMEOUT_SECONDS = 5


def _parse_coord(raw: Any) -> float | None:
    """Coerce a coordinate, rejecting the values that mean "unknown".

    Rejects exactly 0.0 as well as unparseable input. A literal zero is what
    clients emit when their location has not loaded yet, and treating it as
    valid sends distance ranking to Null Island in the Atlantic.
    """
    if raw is None or raw == "":
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if value == 0.0:
        return None
    return value


def fetch_live_vehicle_coords(
    vehicle_id: str,
    jwt: str,
    api_base: str,
) -> tuple[float | None, float | None]:
    """Return (latitude, longitude) from the live-state endpoint.

    Returns (None, None) on any failure. A location lookup must never break the
    turn: the caller leaves the session coordinates unset and the tool then
    says location is unavailable, which is the correct degraded behaviour.
    """
    if not (vehicle_id and jwt and api_base):
        return (None, None)
    try:
        resp = requests.get(
            f"{api_base}/vehicles/{vehicle_id}/live-state",
            headers={"Authorization": f"Bearer {jwt}"},
            timeout=_TIMEOUT_SECONDS,
        )
        if resp.status_code >= 400:
            log.warning(
                "vehicle_location: live-state returned %d for %s",
                resp.status_code,
                vehicle_id,
            )
            return (None, None)
        data = resp.json() or {}
        return (
            _parse_coord(data.get("latitude")),
            _parse_coord(data.get("longitude")),
        )
    except Exception as e:  # noqa: BLE001
        log.warning(
            "vehicle_location: live-state lookup failed for %s: %s",
            vehicle_id,
            e,
        )
        return (None, None)


def fetch_vehicle_facts(
    vehicle_id: str,
    jwt: str,
    api_base: str,
) -> dict[str, Any]:
    """Return {make, latitude, longitude} from the vehicle-context endpoint.

    One round trip for both facts. `/vehicles/{id}/context` projects `make`
    alongside `lastLatitude` / `lastLongitude`, so this avoids a second call
    purely to learn the brand.

    Returns {} on any failure.
    """
    if not (vehicle_id and jwt and api_base):
        return {}
    try:
        resp = requests.get(
            f"{api_base}/vehicles/{vehicle_id}/context",
            headers={"Authorization": f"Bearer {jwt}"},
            timeout=_TIMEOUT_SECONDS,
        )
        if resp.status_code >= 400:
            log.warning(
                "vehicle_location: context returned %d for %s",
                resp.status_code,
                vehicle_id,
            )
            return {}
        data = resp.json() or {}
        return {
            "make": (data.get("make") or "").strip() or None,
            "latitude": _parse_coord(data.get("lastLatitude")),
            "longitude": _parse_coord(data.get("lastLongitude")),
        }
    except Exception as e:  # noqa: BLE001
        log.warning(
            "vehicle_location: context lookup failed for %s: %s", vehicle_id, e
        )
        return {}


def resolve_session_coords(ctx, api_base: str) -> None:
    """Populate ctx.client_latitude / ctx.client_longitude in place.

    No-op when coordinates are already present or no vehicle is bound to the
    session. Never raises.
    """
    existing_lat = _parse_coord(getattr(ctx, "client_latitude", None))
    existing_lon = _parse_coord(getattr(ctx, "client_longitude", None))
    if existing_lat is not None and existing_lon is not None:
        ctx.client_latitude = existing_lat
        ctx.client_longitude = existing_lon
        return

    vehicle_id = getattr(ctx, "vehicle_id", None)
    if not vehicle_id:
        return
    jwt = getattr(ctx, "jwt", "") or ""

    # One call gives the brand and a usable position.
    facts = fetch_vehicle_facts(vehicle_id, jwt, api_base)

    # The brand must come from the vehicle record, never from the model. Asked
    # for a dealer, the model supplied the TENANT ID as the make ("ford"), which
    # matched no dealer's brandsServiced and silently returned zero results.
    # The tenant id is an internal slug and has no necessary relationship to
    # what the vehicle actually is.
    if facts.get("make") and not getattr(ctx, "vehicle_make", None):
        ctx.vehicle_make = facts["make"]

    lat, lon = facts.get("latitude"), facts.get("longitude")
    if lat is None or lon is None:
        # Fall back to live-state (Redis-first), which can also be fresher than
        # the stored row when the vehicle is actively connecting.
        lat, lon = fetch_live_vehicle_coords(vehicle_id, jwt, api_base)
    if lat is not None and lon is not None:
        ctx.client_latitude = lat
        ctx.client_longitude = lon
        log.info(
            "vehicle_location: resolved coords + make=%s for %s",
            getattr(ctx, "vehicle_make", None), vehicle_id,
        )
