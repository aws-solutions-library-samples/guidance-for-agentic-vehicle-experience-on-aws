"""VIN → owner-of-record resolution against the CMS vehicle registry.

Fix Group 15 / F11.2.  Resolves the owner of record for a VIN so the Tier 2
writer can project a top-level ``owner_id`` attribute onto the persisted AVX
Finding DDB item (see :mod:`agents.tier2.writer`).

Why this module exists rather than reusing ``lambdas/shared/cms_client_real.py``
-------------------------------------------------------------------------------
F11.2 Accept 2 specifies reusing that module's ``_vehicles_table`` client.  That
is **not implementable in the Tier 2 Lambda**, for two independent reasons, both
verified 2026-09-23:

1. ``cms_client_real`` imports ``redis`` at module scope.  The Tier 2 bundle
   installs exactly three packages (``requirements-tier2-lambda.txt``:
   strands-agents, boto3, pydantic).  ``redis`` appears in that file only inside
   a comment listing packages the Lambda deliberately does NOT install.
2. It imports ``triage_classifier.types``, which exists only under
   ``infrastructure/build/`` — a path the CDK asset ``exclude`` list drops.

Importing it would raise ``ModuleNotFoundError`` before the first lookup.  The
alternative — adding redis and vendoring triage_classifier into a bundle already
at 101 MB against a 250 MB ceiling, to perform two DynamoDB calls — is the wrong
trade.  This module is deliberately boto3-only and does not duplicate that
client's responsibilities: it reads one attribute, and it shares the table-name
contract (``CMS_VEHICLES_TABLE``) rather than deriving a name of its own.

Why two calls and not one
-------------------------
F11.2 Accept 5 specifies ``dynamodb:GetItem`` only, "No ``dynamodb:Query``".
That is impossible against the deployed registry.  Verified against
``cms-staging-storage-vehicles`` 2026-09-23:

* the base table's key schema is ``vehicleId`` (HASH) **only** — there is no
  VIN key, so a VIN cannot be ``GetItem``-ed;
* the ``vin-index`` GSI is keyed on ``vin`` but its projection is ``KEYS_ONLY``,
  so it returns ``vin`` + ``vehicleId`` and cannot return ``sold_to``.

So resolution is necessarily: Query the index for the key, then GetItem the base
table for the attribute.  The IAM grants mirror that exactly — ``Query`` scoped
to the index ARN, ``GetItem`` scoped to the table ARN, nothing wider.

A trap worth naming: for some registry rows ``vehicleId == vin`` (the Ford
rows), while for the demo rows that actually carry owners it does not
(``VEH-CS-DEMO-0031`` vs ``CSPR0000000000031``).  A "simplification" that
``GetItem``s by VIN therefore appears to work and fails silently on precisely
the owner-bearing fleet.  ``test_owner_resolver.py`` pins this.

Fail-closed posture
-------------------
Every failure mode returns ``None``:  no index hit, no base row, absent
``sold_to``, empty ``sold_to``, a ``sold_to`` equal to the VIN, or any botocore
error.  ``None`` means "unresolvable", and the writer omits the projection
entirely rather than writing a falsy or wrong value.  Nothing raises: a
registry outage must degrade the projection, not abort a nightly health run.

The VIN is never logged in the clear.  It is reduced to a stable SHA-256
fragment, matching the existing convention in ``runner.py``'s
``avx_write_failed`` path (a per-process ``hash()`` would produce a different
fragment for the same VIN on every invocation).
"""
from __future__ import annotations

import hashlib
import logging
from typing import Any, NamedTuple, Optional

log = logging.getLogger(__name__)

# Deployed GSI name on cms-{stage}-storage-vehicles.  KEYS_ONLY projection of
# HASH `vin`.  Verified against the live staging table 2026-09-23.
VIN_INDEX_NAME = "vin-index"

# The registry attribute naming the party of record.  Values are CUST-XXXXXXXX.
# Provenance note per docs/identity-and-standing-contract.md § 4.1: `sold_to`
# literally means SOLD TO — the purchaser. CMS has no title field, so today the
# purchaser and the title holder are indistinguishable and this one value serves
# both. When CMS separates them, `owner_id` re-points and `customer_id` takes over
# the purchaser role.
SOLD_TO_ATTR = "sold_to"

# The registry attribute naming the fleet the vehicle belongs to.  Measured
# 2026-09-23: present on 155 of 155 staging vehicles — every vehicle is in a
# fleet, and only 99 also carry a party. So fleet standing has BROADER coverage
# than owner standing, which is why projecting it matters.
FLEET_ID_ATTR = "fleetId"

# Base-table partition key, and the value `custom:vehicleId` carries (VEH-*).
VEHICLE_ID_ATTR = "vehicleId"


class StandingFacts(NamedTuple):
    """The vehicle-side facts a caller's claims are intersected against.

    One per standing in ``docs/identity-and-standing-contract.md`` § 3:

    * ``owner_id``   -> ``owner`` standing, matched against ``custom:customerId``
    * ``fleet_id``   -> ``fleet_manager`` standing, matched against ``custom:fleetIds``
    * ``vehicle_id`` -> ``fleet_driver`` standing, matched against ``custom:vehicleId``

    Each field is **independently** ``None``-able, and ``None`` means "no standing
    provable by this route" — never "" and never a substitute value. All three come
    from the same CMS vehicle row, so resolving them is one lookup, not three.

    ``vehicle_id`` is carried separately from the Finding's VIN on purpose:
    ``custom:vehicleId`` holds a ``VEH-*`` id (e.g. ``VEH-MRDN-0015``), not a VIN
    (``MRDN0000000000015``). Matching a claim against the VIN would never succeed.
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


def resolve_standing_facts(
    vin: str,
    *,
    ddb_client: Any,
    vehicles_table: str,
) -> StandingFacts:
    """Resolve *vin* to the standing facts for that vehicle.

    Replaces the single-purpose ``resolve_owner_id``. Same two DynamoDB calls —
    the extra facts are additional attributes on a row already being read, so
    broadening this costs nothing per Finding.

    Parameters
    ----------
    vin:
        Vehicle identification number as it appears in the registry's ``vin-index``.
    ddb_client:
        boto3 **low-level** DynamoDB client, holding ``Query`` on
        ``{vehicles_table}/index/vin-index`` and ``GetItem`` on ``{vehicles_table}``.
    vehicles_table:
        Fully resolved table name, injected.  Never derived here.

    Returns
    -------
    StandingFacts
        Any field may be ``None``. An all-``None`` result means no standing is
        provable for this vehicle, which MUST deny — see the contract § 6.

    Notes
    -----
    Does not raise. Every failure mode — missing index entry, missing row, missing
    attribute, blank value, or any botocore error — yields ``None`` for the
    affected field(s). A registry fault must degrade the projection, not abort a
    nightly health run.
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

    return StandingFacts(
        owner_id=_validate_party(
            row.get(SOLD_TO_ATTR), vin=vin, vehicles_table=vehicles_table
        ),
        fleet_id=_validate_opaque(
            row.get(FLEET_ID_ATTR), vin=vin, field=FLEET_ID_ATTR
        ),
        # The index already returned this; no second read, and it cannot be blank
        # (it is the base table's partition key).
        vehicle_id=_validate_opaque(vehicle_id, vin=vin, field=VEHICLE_ID_ATTR),
    )


def resolve_owner_id(
    vin: str,
    *,
    ddb_client: Any,
    vehicles_table: str,
) -> Optional[str]:
    """Owner-only view of :func:`resolve_standing_facts`.

    Retained because it is the narrow question the approve path's owner branch
    asks, and because removing it would churn call sites for no gain. New callers
    that need more than one standing should use ``resolve_standing_facts`` directly
    rather than calling this plus a second lookup.
    """
    return resolve_standing_facts(
        vin, ddb_client=ddb_client, vehicles_table=vehicles_table
    ).owner_id


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
        # Scan fallback — a Scan is not granted, and adding one would widen IAM
        # for a case the index already answers.
        return None

    vehicle_id = (items[0].get(VEHICLE_ID_ATTR) or {}).get("S")
    return vehicle_id or None


def _get_standing_row(
    vehicle_id: str,
    *,
    ddb_client: Any,
    vehicles_table: str,
) -> dict[str, str]:
    """Step 2 — GetItem the base table for the standing attributes only.

    ``ProjectionExpression`` is narrowed to exactly the two attributes needed
    (``vehicle_id`` came free from the index). The registry row carries owner
    PII — full name, email, phone, address on the ADP mirror — that the Tier 2
    runner has no business reading; not requesting it is the cheapest way to not
    hold it.

    Returns a plain ``{attr: str}`` dict; absent attributes are simply absent.
    """
    resp = ddb_client.get_item(
        TableName=vehicles_table,
        Key={VEHICLE_ID_ATTR: {"S": vehicle_id}},
        ProjectionExpression="#st, #fl",
        ExpressionAttributeNames={"#st": SOLD_TO_ATTR, "#fl": FLEET_ID_ATTR},
    )
    item = resp.get("Item") or {}
    out: dict[str, str] = {}
    for attr in (SOLD_TO_ATTR, FLEET_ID_ATTR):
        val = (item.get(attr) or {}).get("S")
        if val is not None:
            out[attr] = val
    return out


def _validate_opaque(
    raw: Optional[str],
    *,
    vin: str,
    field: str,
) -> Optional[str]:
    """Normalise a non-party identifier (fleet id, vehicle id) or return ``None``.

    Weaker than :func:`_validate_party` on purpose: these are opaque ids with no
    committed format (fleet ids are a mix of ``FLEET-*`` and slugs like
    ``oem1-staging-fleet``, measured 2026-09-23), so shape cannot be asserted.
    What IS asserted is the part that matters for authorization: non-empty, and
    not the VIN.
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
    if candidate == vin:
        log.warning(
            "owner_resolver: event=standing_unresolvable reason=%s_equals_vin vin_hash=%s",
            field,
            mask_vin(vin),
        )
        return None
    return candidate


def _validate_party(
    sold_to: Optional[str],
    *,
    vin: str,
    vehicles_table: str,
) -> Optional[str]:
    """Normalise a raw ``sold_to`` read into a projectable party id or ``None``.

    Three rejections, each a real failure rather than a hypothetical:

    * **absent / empty / whitespace** — 56 of 155 staging registry rows carry no
      ``sold_to`` at all.  ``""`` is not absent: an empty projected value would be
      compared against a caller identity, and an empty claim would match it.
    * **equal to the VIN** — a VIN is not a party identity.  Guards against a
      future resolver or registry change that mirrors the key into the field.

    Returning ``None`` for all of them keeps ``is None`` the single unresolvable
    signal the writer checks.

    Deliberately NOT asserted here: the ``CUST-[0-9A-F]{8}`` shape. It holds for
    99/99 staging rows, but this function's job is fail-closed normalisation, and
    rejecting an unexpected-but-real party id would silently drop a legitimate
    owner. The shape belongs in the contract's § 8 verification, where a violation
    is visible, not in a code path where it is invisible.
    """
    if sold_to is None:
        log.info(
            "owner_resolver: event=standing_unresolvable reason=no_sold_to vin_hash=%s table=%s",
            mask_vin(vin),
            vehicles_table,
        )
        return None
    return _validate_opaque(sold_to, vin=vin, field=SOLD_TO_ATTR)
