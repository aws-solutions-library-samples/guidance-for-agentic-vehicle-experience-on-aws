"""VIN → standing facts against the CMS vehicle registry and the CMS drivers table.

Fix Group 15 / F11.2, re-pointed 2026-10-07 (issue
``2026-10-07-avx-finding-owner-is-purchaser-not-driver``).  Resolves the facts the
Tier 2 writer projects onto a persisted VIN-scoped AVX Finding (see
:mod:`agents.tier2.writer`): ``owner_id``, ``fleet_id`` and ``vehicle_id``.

``owner_id`` is the vehicle's assigned driver, not its purchaser
-----------------------------------------------------------------
User decision 2026-09-23 (``issues/2026-09-23-avx-owner-id-not-comparable-to-cognito-sub/``
§ "USER DECISION"): *"can we follow the same construct as the 'drivers' so a vin
is assigned a driver/user"*.  So ``owner_id`` is the Cognito ``sub`` of the CMS-pool
user who is the vehicle's assigned driver, reached through the CMS drivers table::

    VIN -> vehicles.vin-index -> vehicleId
        -> drivers rows with assignedVehicleId == vehicleId   (exactly one)
        -> that driver's cognitoEmail
        -> CMS pool users with email == cognitoEmail          (exactly one,
                                                               email_verified, enabled)
        -> that user's sub                                     == owner_id

The registry's ``sold_to`` (the purchaser, ``CUST-`` + 8 hex) is no longer read.
It stays on the vehicle row as commercial data.

Every consumer of ``owner_id`` compares it with a principal: the broker cards only a
Cognito ``sub``, ``check_owner_scope`` compares the caller's ``sub``, and
``agents/executors/chat.py`` refuses an Action whose owner disagrees with the
caller's ``sub``.  A ``sub`` is the one value all three can match.

Why email, and why every check on it
-------------------------------------
A driver row carries ``cognitoEmail`` and nothing else that names a principal (no
``sub``, verified 2026-10-07 across all 1,257 staging rows).  ``email`` is the one
attribute a pool client must be able to write (federated sign-in maps it), so it is
only safe as a join key under four conditions, each enforced here:

* **One pool.** The CMS pool (``CMS_USER_POOL_ID``), which owns the drivers table
  and is the pool iOS signs in to.  The same address exists in both pools the API
  trusts, with different subs, so "any pool" would be ambiguous.
* **Exactly one user** with that address.  Two matches (e.g. a native user and a
  federated user) project nothing; the resolver never picks one.
* **``email_verified == "true"``.**  A user who set an unverified address equal to a
  driver's ``cognitoEmail`` must not become that driver.
* **Exactly one driver row** assigned to the vehicle.  ``MRDN0000002000013`` has three; the
  vehicle's driver is then undefined, and no owner is projected.

The email is the write-time join only.  At request time consumers compare ``sub``,
never ``email`` (docs/identity-and-standing-contract.md § 5).

Why two registry calls and not one
----------------------------------
F11.2 Accept 5 specified ``dynamodb:GetItem`` only.  That is impossible against the
deployed registry (verified against ``cms-staging-storage-vehicles`` 2026-09-23):

* the base table's key schema is ``vehicleId`` (HASH) **only** — a VIN cannot be
  ``GetItem``-ed;
* the ``vin-index`` GSI is keyed on ``vin`` but projects ``KEYS_ONLY``.

So: Query the index for the key, then GetItem the base table for ``fleetId``.

A trap worth naming: before the 2026-11 CMS migration many registry rows had a
surrogate ``VEH-`` vehicleId, not the VIN, and a "simplification" that ``GetItem``s
by VIN appeared to work and failed silently on the demo fleet.  Since the migration
``vehicleId == vin`` on every row, but the two-step read stays: it is correct for
both shapes, and ``tests/test_runner_projects_owner_id.py`` pins it.

Why the drivers table is read once per run
------------------------------------------
``cms-{stage}-storage-drivers`` is keyed on ``driverId`` and has **no** index on
``assignedVehicleId`` (1,257 rows on staging).  A Scan per VIN would read the whole
table for every Finding.  :class:`AssignedDriverDirectory` scans it once, lazily, on
the first lookup of a run, and caches the email → ``sub`` answers for that run.
Build one per Lambda invocation; never hold one at module scope, or a reassignment
would not reach the projection until the container is recycled.

Consequence for the runner's write-time integrity re-read (F11.2 Accept 4): the
re-read still re-reads the vehicle row, but the owner half comes from the run's
snapshot.  A reassignment during a run converges on the next run, which is already
the staleness bound of a write-time projection (contract § 6).

Fail-closed posture
-------------------
Every failure yields ``None`` for the affected field: no index hit, no base row, a
blank value, an owner or fleet value equal to the VIN, a driver directory that cannot
load, an ambiguous assignment, an unverified or ambiguous pool user, or any client
error.  A ``vehicle_id`` equal to the VIN is NOT a failure: it is the registry's
invariant since the CMS migration (issue
``2026-11-07-standing-dropped-when-vehicleid-equals-vin``).
Nothing raises: a registry or Cognito fault degrades the projection, it does not
abort a nightly health run.

Neither the VIN, the email nor the ``sub`` is logged.  The VIN is reduced to a stable
SHA-256 fragment, matching ``runner.py``'s ``avx_write_failed`` path.
"""
from __future__ import annotations

import hashlib
import logging
import re
from typing import Any, NamedTuple, Optional

log = logging.getLogger(__name__)

# Deployed GSI name on cms-{stage}-storage-vehicles.  KEYS_ONLY projection of
# HASH `vin`.  Verified against the live staging table 2026-09-23.
VIN_INDEX_NAME = "vin-index"

# The registry attribute naming the fleet the vehicle belongs to.  Measured
# 2026-09-23: present on 155 of 155 staging vehicles.
FLEET_ID_ATTR = "fleetId"

# Base-table partition key, and the value `custom:vehicleId` carries (the VIN since
# the 2026-11 CMS migration; a surrogate VEH-* id before it).
VEHICLE_ID_ATTR = "vehicleId"

# cms-{stage}-storage-drivers attributes the directory reads, and nothing else
# (the rows also carry name, phone and licence data the runner has no use for).
DRIVER_ID_ATTR = "driverId"
DRIVER_VEHICLE_ATTR = "assignedVehicleId"
DRIVER_EMAIL_ATTR = "cognitoEmail"

# A Cognito `sub` is a lowercase UUID.  Same shape the broker requires before it
# will card a Finding (`avx_broker._COGNITO_SUB_RE`).
_SUB_RE = re.compile(r"\A[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z")

# An address safe to interpolate into a ListUsers filter: no quote, backslash or
# whitespace can appear, so the filter string cannot be broken out of.
_EMAIL_RE = re.compile(r"\A[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+\Z")

# ListUsers' documented maximum page size.
_LIST_USERS_LIMIT = 60


class StandingFacts(NamedTuple):
    """The vehicle-side facts a caller's claims are intersected against.

    * ``owner_id``   -> the assigned driver's Cognito ``sub`` (CMS pool); matched
      against the caller's ``sub``
    * ``fleet_id``   -> ``fleet_manager`` standing, matched against ``custom:fleetIds``
    * ``vehicle_id`` -> ``fleet_driver`` standing, matched against ``custom:vehicleId``

    Each field is **independently** ``None``-able, and ``None`` means "no standing
    provable by this route" — never "" and never a substitute value.

    ``vehicle_id`` is carried separately from the Finding's VIN on purpose: its
    presence is the proof that the CMS registry knows this car. Since the 2026-11
    CMS migration it equals the VIN (and so does ``custom:vehicleId``), so a claim
    compared against the VIN would match even for a VIN the registry has never
    heard of. Before the migration it was a surrogate VEH- id.
    """

    owner_id: Optional[str] = None
    fleet_id: Optional[str] = None
    vehicle_id: Optional[str] = None

    @property
    def any_resolved(self) -> bool:
        """True when at least one standing route is provable."""
        return any(f is not None for f in self)


def mask_vin(vin: str) -> str:
    """Return a stable, non-reversible 8-char fragment for logging.

    Stable across invocations (SHA-256, not Python's randomised ``hash()``), so
    the same VIN produces the same fragment in CloudWatch across Lambda
    containers and can be correlated without ever exposing the VIN.
    """
    return hashlib.sha256(vin.encode("utf-8")).hexdigest()[:8]



class AssignedDriverDirectory:
    """One run's view of "who is this vehicle's assigned driver, as a CMS principal".

    Lazy: nothing is read until the first :meth:`lookup`.  Then the drivers table is
    scanned once (paginated to completion), and each email → ``sub`` answer is cached
    for the life of this object.  A failed scan is remembered too, so every lookup in
    the run answers the same way (the runner's integrity re-read relies on that).

    Parameters
    ----------
    ddb_client:
        boto3 **low-level** DynamoDB client holding ``Scan`` on ``drivers_table``.
    drivers_table:
        Fully resolved table name (``cms-{stage}-storage-drivers``), injected.
    cognito_client:
        boto3 ``cognito-idp`` client holding ``ListUsers`` on ``user_pool_id``.
    user_pool_id:
        The CMS pool.  Empty or ``None`` disables owner resolution (every lookup
        answers ``None``), which is the safe state for a stage with no CMS pool.
    """

    def __init__(
        self,
        *,
        ddb_client: Any,
        drivers_table: str,
        cognito_client: Any,
        user_pool_id: Optional[str],
    ) -> None:
        self._ddb = ddb_client
        self._drivers_table = drivers_table
        self._cognito = cognito_client
        self._user_pool_id = (user_pool_id or "").strip() or None
        # vehicleId -> one entry per driver row naming it (its cognitoEmail or None)
        self._by_vehicle: Optional[dict[str, list[Optional[str]]]] = None
        self._load_failed = False
        self._sub_by_email: dict[str, tuple[Optional[str], str]] = {}

    def lookup(self, vehicle_id: str) -> tuple[Optional[str], str]:
        """Return ``(sub, reason)`` for the vehicle's assigned driver.

        ``sub`` is ``None`` unless every condition in the module docstring holds;
        ``reason`` is a short log token naming the outcome (``resolved`` on success).
        Never raises.
        """
        try:
            return self._lookup(vehicle_id)
        except Exception as exc:  # noqa: BLE001 — fail-closed by contract
            log.warning(
                "owner_resolver: event=driver_lookup_failed error=%s", type(exc).__name__
            )
            return None, "driver_lookup_failed"

    def _lookup(self, vehicle_id: str) -> tuple[Optional[str], str]:
        if not vehicle_id:
            return None, "no_vehicle_id"
        if self._user_pool_id is None or not self._drivers_table:
            return None, "driver_directory_unconfigured"
        index = self._index()
        if index is None:
            return None, "driver_directory_unavailable"
        rows = index.get(vehicle_id, [])
        if not rows:
            return None, "no_assigned_driver"
        if len(rows) > 1:
            return None, "multiple_assigned_drivers"
        email = rows[0]
        if email is None:
            return None, "driver_has_no_cognito_email"
        return self._sub_for_email(email)

    def _index(self) -> Optional[dict[str, list[Optional[str]]]]:
        if self._by_vehicle is not None:
            return self._by_vehicle
        if self._load_failed:
            return None
        by_vehicle: dict[str, list[Optional[str]]] = {}
        kwargs: dict[str, Any] = {
            "TableName": self._drivers_table,
            # Exactly the three attributes the join needs. The rows also carry
            # names, phone numbers and licence data; not requesting them is the
            # cheapest way not to hold them.
            "ProjectionExpression": "#d, #v, #e",
            "ExpressionAttributeNames": {
                "#d": DRIVER_ID_ATTR,
                "#v": DRIVER_VEHICLE_ATTR,
                "#e": DRIVER_EMAIL_ATTR,
            },
        }
        try:
            while True:
                page = self._ddb.scan(**kwargs)
                for item in page.get("Items") or []:
                    vehicle = _string_attr(item, DRIVER_VEHICLE_ATTR)
                    if vehicle is None:
                        continue
                    by_vehicle.setdefault(vehicle, []).append(
                        _string_attr(item, DRIVER_EMAIL_ATTR)
                    )
                last_key = page.get("LastEvaluatedKey")
                if not last_key:
                    break
                kwargs["ExclusiveStartKey"] = last_key
        except Exception as exc:  # noqa: BLE001 — fail-closed by contract
            self._load_failed = True
            log.warning(
                "owner_resolver: event=driver_directory_failed table=%s error=%s "
                "error_detail=%s",
                self._drivers_table,
                type(exc).__name__,
                str(exc)[:120],
            )
            return None
        self._by_vehicle = by_vehicle
        return by_vehicle

    def _sub_for_email(self, email: str) -> tuple[Optional[str], str]:
        key = email.lower()
        cached = self._sub_by_email.get(key)
        if cached is not None:
            return cached
        answer = self._query_pool(email)
        self._sub_by_email[key] = answer
        return answer

    def _query_pool(self, email: str) -> tuple[Optional[str], str]:
        if not _EMAIL_RE.fullmatch(email):
            return None, "driver_email_malformed"
        kwargs: dict[str, Any] = {
            "UserPoolId": self._user_pool_id,
            "Filter": f'email = "{email}"',
            "Limit": _LIST_USERS_LIMIT,
        }
        users: list[dict[str, Any]] = []
        try:
            while True:
                page = self._cognito.list_users(**kwargs)
                users.extend(page.get("Users") or [])
                token = page.get("PaginationToken")
                if not token:
                    break
                kwargs["PaginationToken"] = token
        except Exception as exc:  # noqa: BLE001 — fail-closed by contract
            log.warning(
                "owner_resolver: event=pool_lookup_failed error=%s error_detail=%s",
                type(exc).__name__,
                str(exc)[:120],
            )
            return None, "pool_lookup_failed"

        want = email.lower()
        matches = [u for u in users if (_user_attr(u, "email") or "").lower() == want]
        if not matches:
            return None, "no_pool_user"
        if len(matches) > 1:
            return None, "multiple_pool_users"
        user = matches[0]
        if user.get("Enabled") is not True:
            return None, "pool_user_disabled"
        if _user_attr(user, "email_verified") != "true":
            return None, "pool_user_email_unverified"
        sub = _user_attr(user, "sub")
        if sub is None or not _SUB_RE.fullmatch(sub):
            return None, "pool_user_sub_malformed"
        return sub, "resolved"


def _string_attr(item: dict[str, Any], name: str) -> Optional[str]:
    """A low-level DynamoDB ``S`` attribute, stripped, or ``None`` if absent/blank."""
    raw = (item.get(name) or {}).get("S")
    if not isinstance(raw, str):
        return None
    return raw.strip() or None


def _user_attr(user: dict[str, Any], name: str) -> Optional[str]:
    """One attribute value from a ListUsers ``User``, or ``None``."""
    for attr in user.get("Attributes") or []:
        if attr.get("Name") == name:
            value = attr.get("Value")
            return value if isinstance(value, str) else None
    return None


def resolve_standing_facts(
    vin: str,
    *,
    ddb_client: Any,
    vehicles_table: str,
    drivers: Optional[AssignedDriverDirectory],
) -> StandingFacts:
    """Resolve *vin* to the standing facts for that vehicle.

    Parameters
    ----------
    vin:
        Vehicle identification number as it appears in the registry's ``vin-index``.
    ddb_client:
        boto3 **low-level** DynamoDB client, holding ``Query`` on
        ``{vehicles_table}/index/vin-index`` and ``GetItem`` on ``{vehicles_table}``.
    vehicles_table:
        Fully resolved table name, injected.  Never derived here.
    drivers:
        The run's :class:`AssignedDriverDirectory`.  Required keyword, so no caller
        can omit it by accident; ``None`` means "no owner projection" and is the
        only way to ask for that.

    Returns
    -------
    StandingFacts
        Any field may be ``None``. An all-``None`` result means no standing is
        provable for this vehicle, which MUST deny — see the contract § 6.

    Notes
    -----
    Does not raise. Every failure mode yields ``None`` for the affected field(s).
    """
    if not vin:
        # Defensive: an empty VIN cannot key the index.  Not WARN-worthy — the
        # caller's scope extraction already guarantees a VIN for VIN-scoped Findings.
        return StandingFacts()

    try:
        vehicle_id = _query_vehicle_id(vin, ddb_client=ddb_client, vehicles_table=vehicles_table)
        if vehicle_id is None:
            log.info(
                "owner_resolver: event=standing_unresolvable reason=no_index_hit "
                "vin_hash=%s table=%s",
                mask_vin(vin),
                vehicles_table,
            )
            return StandingFacts()

        row = _get_standing_row(
            vehicle_id, ddb_client=ddb_client, vehicles_table=vehicles_table
        )
    except Exception as exc:  # noqa: BLE001 — fail-closed by contract
        # ResourceNotFoundException, AccessDeniedException,
        # ProvisionedThroughputExceededException, and anything else the client can
        # raise are all "unresolvable". WARN (not ERROR) because a nightly run
        # legitimately continues; the projection is simply omitted.
        log.warning(
            "owner_resolver: event=standing_lookup_failed vin_hash=%s table=%s "
            "error=%s error_detail=%s",
            mask_vin(vin),
            vehicles_table,
            type(exc).__name__,
            str(exc)[:120],
        )
        return StandingFacts()

    # The index already returned this; no second read, and it cannot be blank
    # (it is the base table's partition key).
    #
    # It MAY equal the VIN, and since CMS spec 2026-10-28-vehicleid-equals-vin it
    # always does: every registry row's vehicleId is its VIN, and custom:vehicleId
    # and the drivers table's assignedVehicleId carry the same value. Rejecting it
    # (the pre-migration rule, when a vehicleId equal to the VIN meant a bad row)
    # dropped fleet_driver standing and, because the driver lookup keys on this
    # value, the owner too (issue 2026-11-07-standing-dropped-when-vehicleid-equals-vin).
    # owner_id and fleet_id below still must not equal the VIN.
    checked_vehicle_id = _validate_opaque(
        vehicle_id, vin=vin, field=VEHICLE_ID_ATTR, may_equal_vin=True
    )
    return StandingFacts(
        owner_id=_resolve_driver_owner(checked_vehicle_id, vin=vin, drivers=drivers),
        fleet_id=_validate_opaque(
            row.get(FLEET_ID_ATTR), vin=vin, field=FLEET_ID_ATTR
        ),
        vehicle_id=checked_vehicle_id,
    )


def resolve_owner_id(
    vin: str,
    *,
    ddb_client: Any,
    vehicles_table: str,
    drivers: Optional[AssignedDriverDirectory],
) -> Optional[str]:
    """Owner-only view of :func:`resolve_standing_facts`."""
    return resolve_standing_facts(
        vin, ddb_client=ddb_client, vehicles_table=vehicles_table, drivers=drivers
    ).owner_id


def _resolve_driver_owner(
    vehicle_id: Optional[str],
    *,
    vin: str,
    drivers: Optional[AssignedDriverDirectory],
) -> Optional[str]:
    """The assigned driver's ``sub`` for *vehicle_id*, or ``None`` (logged)."""
    if drivers is None:
        reason, sub = "no_driver_directory", None
    elif vehicle_id is None:
        reason, sub = "no_vehicle_id", None
    else:
        try:
            sub, reason = drivers.lookup(vehicle_id)
        except Exception:  # noqa: BLE001 — an injected directory must not abort a run
            sub, reason = None, "driver_lookup_failed"
    if sub is None:
        log.info(
            "owner_resolver: event=standing_unresolvable reason=%s field=owner_id vin_hash=%s",
            reason,
            mask_vin(vin),
        )
        return None
    return _validate_opaque(sub, vin=vin, field="owner_id")


def _query_vehicle_id(
    vin: str,
    *,
    ddb_client: Any,
    vehicles_table: str,
) -> Optional[str]:
    """Step 1 — Query ``vin-index`` for the base-table partition key.

    ``Limit=1``: ``vin`` is the index's HASH key and is 1:1 with a vehicle, so a
    second page would indicate registry corruption rather than a page boundary.
    """
    resp = ddb_client.query(
        TableName=vehicles_table,
        IndexName=VIN_INDEX_NAME,
        KeyConditionExpression="#v = :vin",
        ExpressionAttributeNames={"#v": "vin"},
        ExpressionAttributeValues={":vin": {"S": vin}},
        Limit=1,
    )
    items = resp.get("Items") or []
    if not items:
        # An empty index response is authoritative: the GSI is keyed on vin, so
        # absence from the index means absence from the table.  Deliberately no
        # Scan fallback — a Scan is not granted on this table, and adding one
        # would widen IAM for a case the index already answers.
        return None

    vehicle_id = (items[0].get(VEHICLE_ID_ATTR) or {}).get("S")
    return vehicle_id or None


def _get_standing_row(
    vehicle_id: str,
    *,
    ddb_client: Any,
    vehicles_table: str,
) -> dict[str, str]:
    """Step 2 — GetItem the base table for ``fleetId`` only.

    ``ProjectionExpression`` is narrowed to the one attribute still read from the
    registry (``vehicle_id`` came free from the index; ``owner_id`` now comes from
    the drivers table).  The registry row carries owner PII the Tier 2 runner has
    no business reading; not requesting it is the cheapest way not to hold it.

    Returns a plain ``{attr: str}`` dict; absent attributes are simply absent.
    """
    resp = ddb_client.get_item(
        TableName=vehicles_table,
        Key={VEHICLE_ID_ATTR: {"S": vehicle_id}},
        ProjectionExpression="#fl",
        ExpressionAttributeNames={"#fl": FLEET_ID_ATTR},
    )
    item = resp.get("Item") or {}
    out: dict[str, str] = {}
    val = (item.get(FLEET_ID_ATTR) or {}).get("S")
    if val is not None:
        out[FLEET_ID_ATTR] = val
    return out


def _validate_opaque(
    raw: Optional[str],
    *,
    vin: str,
    field: str,
    may_equal_vin: bool = False,
) -> Optional[str]:
    """Normalise an identifier (fleet id, vehicle id, owner sub) or return ``None``.

    Shape is not asserted for fleet and vehicle ids: they have no committed format
    (fleet ids are a mix of ``FLEET-*`` and slugs like ``oem1-staging-fleet``,
    measured 2026-09-23).  What IS asserted is the part that matters for
    authorization: non-empty, and not the VIN.  (The owner ``sub`` is
    shape-checked by the directory before it gets here.)

    ``may_equal_vin`` lifts the VIN rule for the vehicle id only: a vehicle id
    equal to its VIN is the registry's invariant since the 2026-11 CMS
    migration. A VIN is still never a party (owner) or a fleet, so the default is
    ``False`` and only the ``vehicleId`` call passes ``True``.
    """
    if raw is None:
        return None
    candidate = raw.strip()
    if not candidate:
        log.warning(
            "owner_resolver: event=standing_unresolvable reason=empty_%s vin_hash=%s",
            field,
            mask_vin(vin),
        )
        return None
    if candidate == vin and not may_equal_vin:
        log.warning(
            "owner_resolver: event=standing_unresolvable reason=%s_equals_vin vin_hash=%s",
            field,
            mask_vin(vin),
        )
        return None
    return candidate
