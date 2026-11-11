"""Who may read GET /vehicles/{vehicleId}/context.

Issue ``issues/2026-10-24-vehicle-context-driver-pii-unscoped/``. The route returns the
assigned driver's name, email, phone, home base and licence for whatever vehicle id is
in the path. Before this module it had no check that the caller may see that vehicle,
so any signed-in user of either trusted pool could walk the vehicle ids and read every
assigned driver's details.

A caller is admitted by any one of these standings (docs/identity-and-standing-contract.md
§ 3), and refused otherwise:

| Standing        | The caller needs                                              | Checked against                    |
|-----------------|---------------------------------------------------------------|------------------------------------|
| admin           | the ``platform-admin`` group                                  | nothing: every vehicle             |
| fleet_driver    | a vehicle-standing group AND a trusted ``custom:vehicleId``   | equals the path vehicle id exactly |
| fleet_manager   | the ``fleet-operator`` group AND trusted ``custom:fleetIds``  | holds the vehicle row's ``fleetId``|

The driver and fleet arms are the ``fleet_driver`` and ``fleet_manager`` standings of
``GET /findings/me`` (``api-avx-findings/handler.py`` ``_findings_me``): the same group
sets, the same claim-trust flags, and the same ``fleet-operator`` gate on fleet standing,
so a driver whose token carries ``custom:fleetIds`` still sees only the vehicle assigned
to them. The group is the factor a caller cannot write (``cognito:groups`` is
admin-assigned); the claim binds the caller to a vehicle or fleet. Each claim counts only
while its ``AVX_*_CLAIM_TRUSTED`` flag is exactly ``"true"`` (§ 5.1: the operator's
attestation that no app client of any trusted pool can write the attribute). The flags
are read on every call. Standing is a set (§ 3): a fleet operator who is also the driver
of a vehicle outside its fleets reads that vehicle through the driver arm.

The admin arm is NOT from ``/findings/me``, which has none, and § 3 names no admin
standing. It is the all-vehicle read the named findings routes give ``platform-admin``
(``_lib/avx_scope.py`` ``check_vehicle_scope``), kept here deliberately: it is the one
arm that shows any driver's details, to a group only an administrator can join, and it
needs neither trust flag.

Deliberately not admitted:

* ``service-advisor`` / ``technician``. The findings routes let them read every VIN's
  Findings. Findings hold no personal data; this route returns the driver's contact
  details and licence, so that all-vehicle read is not carried over. No caller of this
  route needs it (its callers are the iOS app, for the signed-in driver's own vehicle,
  and the supervisor's ``fetch_vehicle_facts``, which reads only make and position).
* ``custom:customerId`` (owner of record). The vehicle row's ``sold_to`` is commercial
  data AVX does not read (contract § 4.1), and owner standing does not imply the right
  to the driver's details (§ 3.1).
* ``email`` and the drivers table. ``/drivers/me`` finds a caller's driver record by
  verified email, but the contract (§ 5) forbids email as a standing key, so this route
  does not authorize through a drivers-table match.

Fail closed: no claims, or claims without ``sub``, is ``UNAUTHENTICATED`` (the handler
answers 401); claims without a standing are ``REFUSED`` (403). A fleet standing admits
nothing until the vehicle row has been read and its ``fleetId`` matched
(``fleet_admits``).

The claim parsing below repeats ``lambdas/_lib/avx_scope.py`` (``claim_is_trusted``,
``_extract_groups``, ``_trusted_claim_list``), the shared library every findings and
actions route uses, because this Lambda's asset is its own directory and cannot import
``_lib``. ``tests/test_context_scope.py`` runs both over the same inputs, and reads
``_VEHICLE_STANDING_GROUPS`` out of the findings handler, so the copies cannot drift apart
unnoticed. (``/findings/me`` parses ``custom:fleetIds`` with its own
``_caller_fleet_ids``, which strips JSON-list members, splits them on commas, and keeps a
member with no letter or digit, such as ``---``, that this module drops. For a CSV of real
fleet ids, which is what Cognito sends, the two agree. Its ``_caller_vehicle_id`` also has
no shape check; this module refuses a malformed ``custom:vehicleId``.)
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass

VEHICLE_ID_CLAIM = "custom:vehicleId"
FLEET_IDS_CLAIM = "custom:fleetIds"
VEHICLE_ID_CLAIM_TRUSTED_ENV = "AVX_VEHICLE_ID_CLAIM_TRUSTED"
FLEET_IDS_CLAIM_TRUSTED_ENV = "AVX_FLEET_IDS_CLAIM_TRUSTED"

ADMIN_GROUP = "platform-admin"
FLEET_MANAGER_GROUP = "fleet-operator"
#: Same set as ``_VEHICLE_STANDING_GROUPS`` in ``api-avx-findings/handler.py``.
VEHICLE_STANDING_GROUPS = frozenset({
    "vehicle-owner",
    "consumer",
    "fleet-driver",
    "fleet-operator",
})

#: Vehicle ids are the VIN since the 2026-11 CMS migration (``VEH-*`` before it). Same shape check as the
#: ``/triage`` and ``/assistant/chat`` vehicle-scope modules.
_VEHICLE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

ADMIN = "admin"
OWN_VEHICLE = "own_vehicle"
FLEET = "fleet"
REFUSED = "refused"
UNAUTHENTICATED = "unauthenticated"


@dataclass(frozen=True)
class Scope:
    """What the caller's claims allow for one vehicle id."""

    standing: str
    #: The caller's trusted fleets; set only when ``standing == FLEET``.
    fleet_ids: frozenset = frozenset()

    @property
    def admitted(self) -> bool:
        """True when the request may go on to read the vehicle row.

        For ``FLEET`` that is not yet a grant: ``fleet_admits`` decides once the row's
        ``fleetId`` is known.
        """
        return self.standing in (ADMIN, OWN_VEHICLE, FLEET)

    @property
    def needs_fleet_match(self) -> bool:
        return self.standing == FLEET


def claim_is_trusted(env_var: str) -> bool:
    """True only when *env_var* is exactly ``"true"`` (trimmed, case-insensitive)."""
    return os.environ.get(env_var, "").strip().lower() == "true"


def claims_from_event(event) -> dict | None:
    """``requestContext.authorizer.claims`` from an API Gateway event, or ``None``."""
    if not isinstance(event, dict):
        return None
    ctx = event.get("requestContext")
    authorizer = ctx.get("authorizer") if isinstance(ctx, dict) else None
    claims = authorizer.get("claims") if isinstance(authorizer, dict) else None
    return claims if isinstance(claims, dict) else None


def extract_groups(claims: dict) -> set:
    """The caller's Cognito groups: a list, a JSON-array string, or a space/comma list."""
    raw = claims.get("cognito:groups", "")
    if isinstance(raw, list):
        return set(raw)
    if not isinstance(raw, str):
        return set()
    raw = raw.strip()
    if not raw:
        return set()
    if raw.startswith("["):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, list):
            # Only string members are group names. (``_lib/avx_scope._extract_groups`` takes
            # any JSON value here, including an object's keys; this module is stricter.)
            return {g for g in parsed if isinstance(g, str)}
    return set(raw.replace(",", " ").split())


def trusted_fleet_ids(claims: dict) -> list:
    """``custom:fleetIds`` members (CSV or JSON), or ``[]`` when the claim isn't trusted.

    A member must carry at least one letter or digit, so a blank claim or a lone
    separator is not membership of anything.
    """
    if not claim_is_trusted(FLEET_IDS_CLAIM_TRUSTED_ENV):
        return []
    raw = claims.get(FLEET_IDS_CLAIM, "")
    if isinstance(raw, list):
        values = raw
    else:
        raw = (raw or "").strip() if isinstance(raw, str) else ""
        if not raw:
            return []
        values = None
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                values = [str(x) for x in parsed]
        except (json.JSONDecodeError, TypeError):
            pass
        if values is None:
            values = [v.strip() for v in raw.split(",")]
    return [v for v in values if any(ch.isalnum() for ch in str(v))]


def trusted_vehicle_id(claims: dict) -> str | None:
    """The caller's ``custom:vehicleId`` when trusted and well formed, else ``None``."""
    if not claim_is_trusted(VEHICLE_ID_CLAIM_TRUSTED_ENV):
        return None
    value = claims.get(VEHICLE_ID_CLAIM)
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not _VEHICLE_ID_RE.match(value):
        return None
    return value


def decide(claims: dict | None, vehicle_id: str) -> Scope:
    """The caller's standing over *vehicle_id*, from its claims alone (no table read)."""
    if not isinstance(claims, dict):
        return Scope(UNAUTHENTICATED)
    sub = claims.get("sub")
    if not isinstance(sub, str) or not sub.strip():
        return Scope(UNAUTHENTICATED)

    groups = extract_groups(claims)
    if ADMIN_GROUP in groups:
        return Scope(ADMIN)

    if groups & VEHICLE_STANDING_GROUPS:
        own = trusted_vehicle_id(claims)
        if own is not None and own == vehicle_id:
            return Scope(OWN_VEHICLE)

    if FLEET_MANAGER_GROUP in groups:
        fleets = trusted_fleet_ids(claims)
        if fleets:
            return Scope(FLEET, frozenset(str(f) for f in fleets))

    return Scope(REFUSED)


def fleet_admits(scope: Scope, vehicle: dict | None) -> bool:
    """For a ``FLEET`` scope: True only when the vehicle row's ``fleetId`` is one of the
    caller's fleets. A missing row, or a row without a ``fleetId``, is not admitted."""
    if scope.standing != FLEET or not isinstance(vehicle, dict):
        return False
    fleet_id = vehicle.get("fleetId")
    return isinstance(fleet_id, str) and bool(fleet_id) and fleet_id in scope.fleet_ids
