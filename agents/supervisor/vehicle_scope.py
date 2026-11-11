"""Vehicle scope enforcement for the bidi runtime session boundary.

DS-1 (issues/2026-10-05-bidi-and-triage-accept-any-vehicle). Called from
`_establish_session` in bidi_app.py after JWT verification.

Mirrors the design of lambdas/api-assistant-chat/vehicle_scope.py but adapted
for the bidi surface:

* The bidi runtime takes the vehicle from headers / session.start. This module
  either pins those values to the caller's claim vehicle, or refuses the session
  when the claim vehicle cannot be verified.
* Unlike the chat scope (which proceeds with an unresolved VIN), the bidi scope
  REFUSES the session when the vehicles table is unreachable or has no VIN for
  the claim vehicle — an unverified VIN must not be classified or booked.

Rules:

| Caller claims                 | Allowed vehicle                                        |
|-------------------------------|--------------------------------------------------------|
| custom:vehicleId (valid)      | that vehicle only; VIN pinned from vehicles table row  |
| custom:vehicleId (malformed)  | none — refuse session (InvocationError)                |
| custom:fleetIds               | vehicleId header required; GetItem; refuse unless      |
|                               | row.fleetId is in fleet_ids AND row.vin == header vin  |
| neither                       | unchanged (tenant check only)                          |

custom:driverId is pinned to ctx.driver_id when present, regardless of vehicle
scope.

Note on claim trust in observe/off mode: when JWT verification is not in enforce
mode, claims come from an unverified token decode. The scope rule only narrows
(removes access it might have without the rule), so an unverified claim cannot
widen access. Recorded in the log line for traceability.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any, Callable, Optional

log = logging.getLogger(__name__)

VEHICLE_ID_CLAIM = "custom:vehicleId"
FLEET_IDS_CLAIM = "custom:fleetIds"
DRIVER_ID_CLAIM = "custom:driverId"

# Same pattern as api-assistant-chat/vehicle_scope.py
_VEHICLE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

# ------------------------------------------------------------------
# Status constants
# ------------------------------------------------------------------
STATUS_PASS = "pass"            # vehicle pinned successfully
STATUS_DENY = "deny"            # scope check failed — refuse session
STATUS_ERROR = "error"          # lookup failed — refuse session
STATUS_UNCHANGED = "unchanged"  # neither claim — pass through unmodified


@dataclass(frozen=True)
class BidiVehicleScope:
    """Result of a bidi vehicle scope enforcement.

    When ``status`` is DENY or ERROR, ``bidi_app.py`` must raise InvocationError
    with ``detail`` as the reason.

    When ``status`` is PASS, ``vehicle_id``, ``vin``, and ``driver_id`` are the
    values to use for the SessionContext (header values may have been replaced).

    When ``status`` is UNCHANGED (no vehicle claim), header values pass through
    unchanged. ``driver_id`` is still pinned from the claim when present.
    """
    status: str
    vehicle_id: Optional[str]
    vin: Optional[str]
    driver_id: Optional[str]
    vin_pinned: bool = False
    header_dropped: bool = False
    detail: str = ""
    claims_verified: bool = False

    @property
    def ok(self) -> bool:
        return self.status in (STATUS_PASS, STATUS_UNCHANGED)


# ------------------------------------------------------------------
# Internal helpers
# ------------------------------------------------------------------

def _extract_fleet_ids(claims: dict) -> list[str]:
    """Parse CSV or JSON fleet ids. Returns [] when absent or blank.

    Mirrors avx_scope._extract_claim_list + _trusted_claim_list: require at
    least one alphanumeric character to reject blank/separator values.
    """
    raw = claims.get(FLEET_IDS_CLAIM, "")
    if isinstance(raw, list):
        values: list[str] = [str(v) for v in raw]
    else:
        raw = str(raw or "").strip()  # a non-string, non-list value must not raise
        if not raw:
            return []
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                values = [str(v) for v in parsed]
            else:
                values = [str(parsed)]
        except (json.JSONDecodeError, TypeError):
            values = [v.strip() for v in raw.split(",")]
    return [v for v in values if any(ch.isalnum() for ch in v)]


def _vin_from_row(row: dict[str, Any]) -> str:
    """Extract VIN string from a GetItem response Item (typed or plain)."""
    vin = row.get("vin")
    if isinstance(vin, dict):
        return (vin.get("S") or "").strip()
    return (str(vin) if vin else "").strip()


def _fleet_id_from_row(row: dict[str, Any]) -> str:
    """Extract fleetId from a GetItem response Item (typed or plain)."""
    flt = row.get("fleetId")
    if isinstance(flt, dict):
        return (flt.get("S") or "").strip()
    return (str(flt) if flt else "").strip()


def _clean(value: object) -> Optional[str]:
    if value is None:
        return None
    s = str(value).strip()
    return s or None


# ------------------------------------------------------------------
# Public API
# ------------------------------------------------------------------

def resolve(
    claims: dict,
    header_vehicle_id: Optional[str],
    header_vin: Optional[str],
    header_driver_id: Optional[str],
    lookup_row: Callable[[str], Optional[dict]],
    claims_verified: bool = False,
) -> BidiVehicleScope:
    """Enforce vehicle scope for a bidi session establishment.

    Args:
        claims: JWT claims (verified or unverified-decode, per caller note).
        header_vehicle_id: vehicleId from headers / session.start (may be None).
        header_vin: vin from headers / session.start (may be None).
        header_driver_id: driverId from headers / session.start.
        lookup_row: ``fn(vehicle_id)`` → dict with ``vin`` / ``fleetId`` fields,
            or None if the row does not exist. RAISES on read errors (the caller
            maps that to STATUS_ERROR).
        claims_verified: True when the JWT outcome was ok=True (for logging).

    Returns a BidiVehicleScope. Never raises — exceptions from lookup_row are
    caught and turned into STATUS_ERROR.
    """
    claim_vid_raw = _clean(claims.get(VEHICLE_ID_CLAIM))
    fleet_claim_raw = claims.get(FLEET_IDS_CLAIM)
    claim_driver_id = _clean(claims.get(DRIVER_ID_CLAIM))

    # driver_id is always pinned from the claim when present, regardless of
    # which vehicle-scope branch applies.
    driver_id = claim_driver_id if claim_driver_id else _clean(header_driver_id)

    # ── Vehicle-bound caller ──────────────────────────────────────────────────

    # Presence of the claim binds the caller, even when its value is blank or
    # malformed: a bound caller never falls through to a less specific branch.
    if VEHICLE_ID_CLAIM in claims:
        if claim_vid_raw is None or not _VEHICLE_ID_RE.match(claim_vid_raw):
            log.warning(
                "bidi vehicle-scope: malformed vehicleId claim; refusing session "
                "(claims_verified=%s)",
                claims_verified,
            )
            return BidiVehicleScope(
                status=STATUS_DENY,
                vehicle_id=None,
                vin=None,
                driver_id=driver_id,
                vin_pinned=False,
                header_dropped=bool(header_vehicle_id or header_vin),
                detail="vehicleId claim is malformed",
                claims_verified=claims_verified,
            )

        try:
            row = lookup_row(claim_vid_raw)
        except Exception as exc:  # noqa: BLE001
            log.error(
                "bidi vehicle-scope: row lookup failed for claim vehicle=%s "
                "error=%s (claims_verified=%s)",
                claim_vid_raw, type(exc).__name__, claims_verified,
            )
            return BidiVehicleScope(
                status=STATUS_ERROR,
                vehicle_id=None,
                vin=None,
                driver_id=driver_id,
                vin_pinned=False,
                detail=f"vehicles table unavailable ({type(exc).__name__})",
                claims_verified=claims_verified,
            )

        if row is None:
            log.error(
                "bidi vehicle-scope: claim vehicle %s not found in vehicles table; "
                "refusing session (claims_verified=%s)",
                claim_vid_raw, claims_verified,
            )
            return BidiVehicleScope(
                status=STATUS_ERROR,
                vehicle_id=None,
                vin=None,
                driver_id=driver_id,
                vin_pinned=False,
                detail="claim vehicle not found",
                claims_verified=claims_verified,
            )

        vin_of_record = _vin_from_row(row)
        if not vin_of_record:
            log.error(
                "bidi vehicle-scope: claim vehicle %s has no VIN; "
                "refusing session (claims_verified=%s)",
                claim_vid_raw, claims_verified,
            )
            return BidiVehicleScope(
                status=STATUS_ERROR,
                vehicle_id=None,
                vin=None,
                driver_id=driver_id,
                vin_pinned=False,
                detail="claim vehicle has no VIN of record",
                claims_verified=claims_verified,
            )

        # Pin vehicle_id and vin from the claim. Any different header values are
        # dropped (logged). Case-insensitive VIN comparison like the chat scope.
        header_vid = _clean(header_vehicle_id)
        header_v = _clean(header_vin)
        dropped = bool(
            (header_vid is not None and header_vid != claim_vid_raw)
            or (header_v is not None and header_v.upper() != vin_of_record.upper())
        )
        if dropped:
            log.info(
                "bidi vehicle-scope: header vehicle values dropped; "
                "using claim vehicle=%s src=claim (claims_verified=%s)",
                claim_vid_raw, claims_verified,
            )

        return BidiVehicleScope(
            status=STATUS_PASS,
            vehicle_id=claim_vid_raw,
            vin=vin_of_record,
            driver_id=driver_id,
            vin_pinned=True,
            header_dropped=dropped,
            claims_verified=claims_verified,
        )

    # ── Fleet-bound caller ────────────────────────────────────────────────────

    if FLEET_IDS_CLAIM in claims:
        fleet_ids = _extract_fleet_ids(claims)

        if not fleet_ids:
            log.warning(
                "bidi vehicle-scope: fleetIds claim present but empty; "
                "refusing session (claims_verified=%s)",
                claims_verified,
            )
            return BidiVehicleScope(
                status=STATUS_DENY,
                vehicle_id=None,
                vin=None,
                driver_id=driver_id,
                vin_pinned=False,
                detail="fleet claim present but yields no valid fleet ids",
                claims_verified=claims_verified,
            )

        # Fleet-bound: require a vehicleId header; GetItem; check fleetId.
        vid = _clean(header_vehicle_id)
        if not vid:
            log.warning(
                "bidi vehicle-scope: fleet caller has no vehicleId header; "
                "refusing session (claims_verified=%s)",
                claims_verified,
            )
            return BidiVehicleScope(
                status=STATUS_DENY,
                vehicle_id=None,
                vin=None,
                driver_id=driver_id,
                vin_pinned=False,
                detail="fleet caller must supply a vehicleId",
                claims_verified=claims_verified,
            )

        try:
            row = lookup_row(vid)
        except Exception as exc:  # noqa: BLE001
            log.error(
                "bidi vehicle-scope: fleet lookup failed vehicle=%s error=%s "
                "(claims_verified=%s)",
                vid, type(exc).__name__, claims_verified,
            )
            return BidiVehicleScope(
                status=STATUS_ERROR,
                vehicle_id=None,
                vin=None,
                driver_id=driver_id,
                vin_pinned=False,
                detail=f"vehicles table unavailable ({type(exc).__name__})",
                claims_verified=claims_verified,
            )

        if row is None:
            log.warning(
                "bidi vehicle-scope: fleet vehicle %s not found; "
                "refusing session (claims_verified=%s)",
                vid, claims_verified,
            )
            return BidiVehicleScope(
                status=STATUS_DENY,
                vehicle_id=None,
                vin=None,
                driver_id=driver_id,
                vin_pinned=False,
                detail="vehicle not in caller scope",
                claims_verified=claims_verified,
            )

        row_fleet_id = _fleet_id_from_row(row)
        if row_fleet_id not in fleet_ids:
            log.warning(
                "bidi vehicle-scope: fleet vehicle fleetId not in caller fleet_ids; "
                "refusing session (claims_verified=%s)",
                claims_verified,
            )
            return BidiVehicleScope(
                status=STATUS_DENY,
                vehicle_id=None,
                vin=None,
                driver_id=driver_id,
                vin_pinned=False,
                detail="vehicle not in caller scope",
                claims_verified=claims_verified,
            )

        row_vin = _vin_from_row(row)
        header_v = _clean(header_vin)
        if not row_vin:
            log.error(
                "bidi vehicle-scope: fleet vehicle %s has no VIN of record; "
                "refusing session (claims_verified=%s)",
                vid, claims_verified,
            )
            return BidiVehicleScope(
                status=STATUS_ERROR,
                vehicle_id=None,
                vin=None,
                driver_id=driver_id,
                vin_pinned=False,
                detail="vehicle has no VIN of record",
                claims_verified=claims_verified,
            )
        if header_v and header_v.upper() != row_vin.upper():
            log.warning(
                "bidi vehicle-scope: fleet vehicle VIN mismatch; "
                "refusing session (claims_verified=%s)",
                claims_verified,
            )
            return BidiVehicleScope(
                status=STATUS_DENY,
                vehicle_id=None,
                vin=None,
                driver_id=driver_id,
                vin_pinned=False,
                detail="vehicle not in caller scope",
                claims_verified=claims_verified,
            )

        return BidiVehicleScope(
            status=STATUS_PASS,
            vehicle_id=vid,
            vin=row_vin,
            driver_id=driver_id,
            vin_pinned=True,
            claims_verified=claims_verified,
        )

    # ── Neither claim — pass through ──────────────────────────────────────────
    return BidiVehicleScope(
        status=STATUS_UNCHANGED,
        vehicle_id=_clean(header_vehicle_id),
        vin=_clean(header_vin),
        driver_id=driver_id,
        vin_pinned=False,
        claims_verified=claims_verified,
    )
