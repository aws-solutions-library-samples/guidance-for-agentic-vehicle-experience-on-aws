"""Pin a vehicle-bound caller's chat to the vehicle in its verified claims.

Why this exists (spec 2026-09-23-cvx-escalation-recommend-not-act, Fix Group 18):
the CMS web chat sends a VIN only when it is opened from a vehicle page, so a
driver who opened it from the dashboard reached the runtime with no vehicle at
all, and the model asked for a VIN instead of triaging a brake symptom
(issues/2026-09-28-text-runtime-no-vin-resolution-from-vehicleid/).

The caller's vehicle is already known and verified: the API Gateway Cognito
authorizer passes the ID token's `custom:vehicleId` claim, and no app client of
any pool the authorizer trusts can write that attribute (every one is limited
to `email` and `name`; `scripts/check-standing-claim-pools.sh` checks it). So
for a caller carrying the claim:

* the vehicle is the claim's, and its VIN is read from the CMS vehicles table;
* a client-sent `vin` or `vehicleId` that does not match is dropped;
* if the VIN cannot be read, no VIN is sent. The body's VIN is NOT used as a
  fallback, because it cannot be checked. The session is still pinned
  (`vehicleBound`), so the runtime's `triage` refuses for that turn rather than
  classify a VIN the model or driver supplies. The lookup runs again on the
  next turn.

A caller without the claim (fleet operator, service advisor, kiosk) keeps the
body values, as before. Scoping those callers is a separate gap (backlog row
`AVX vehicle-scope tenant-only`).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Callable, Optional

log = logging.getLogger()

VEHICLE_ID_CLAIM = "custom:vehicleId"

# A CMS vehicle key, e.g. VEH-MRDN-0015. The claim is admin-set, but it is still
# checked before it is used as a table key.
_VEHICLE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

SOURCE_CLAIM = "claim"                          # claim vehicle, VIN resolved
SOURCE_CLAIM_UNRESOLVED = "claim-unresolved"    # claim vehicle, VIN not found or lookup failed
SOURCE_CLAIM_INVALID = "claim-invalid"          # claim present but malformed; nothing used
SOURCE_BODY = "body"                            # no claim; client-sent values
SOURCE_NONE = "none"                            # no claim, no client values


@dataclass(frozen=True)
class VehicleScope:
    vin: Optional[str]
    vehicle_id: Optional[str]
    source: str
    # True when a client-sent vin or vehicleId was discarded.
    body_dropped: bool

    @property
    def bound(self) -> bool:
        """The caller carries the claim, so the session is pinned to it."""
        return self.source in (SOURCE_CLAIM, SOURCE_CLAIM_UNRESOLVED, SOURCE_CLAIM_INVALID)


def _clean(value: object) -> Optional[str]:
    """Coerce to a stripped string; None when absent or empty.

    `str(...)` first so a non-string body value (int, list) cannot raise and
    turn into an unhandled 5xx (same rule as `personaId` in handler.py).
    """
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def resolve(
    claims: dict,
    body_vin: object,
    body_vehicle_id: object,
    lookup_vin: Callable[[str], Optional[str]],
) -> VehicleScope:
    """Decide which vehicle this turn is about.

    `lookup_vin(vehicle_id)` returns the vehicle's VIN, or None when there is
    none. It may raise; a failure is treated as "VIN unknown".
    """
    body_vin_s = _clean(body_vin)
    body_vid_s = _clean(body_vehicle_id)
    claim_vid = _clean(claims.get(VEHICLE_ID_CLAIM))

    if claim_vid is None:
        source = SOURCE_BODY if (body_vin_s or body_vid_s) else SOURCE_NONE
        return VehicleScope(body_vin_s, body_vid_s, source, False)

    # From here the caller is bound to a vehicle. Client-sent values are only
    # ever compared against it, never used in place of it.
    if not _VEHICLE_ID_RE.match(claim_vid):
        log.warning("vehicle-scope: malformed %s claim; not used", VEHICLE_ID_CLAIM)
        return VehicleScope(
            None, None, SOURCE_CLAIM_INVALID, bool(body_vin_s or body_vid_s),
        )

    try:
        vin = _clean(lookup_vin(claim_vid))
    except Exception as exc:  # noqa: BLE001 — any failure means "VIN unknown"
        log.warning(
            "vehicle-scope: VIN lookup failed for the claim vehicle (%s)",
            type(exc).__name__,
        )
        vin = None

    dropped = bool(
        (body_vid_s is not None and body_vid_s != claim_vid)
        or (body_vin_s is not None and (vin is None or body_vin_s.upper() != vin.upper()))
    )
    source = SOURCE_CLAIM if vin else SOURCE_CLAIM_UNRESOLVED
    return VehicleScope(vin, claim_vid, source, dropped)
