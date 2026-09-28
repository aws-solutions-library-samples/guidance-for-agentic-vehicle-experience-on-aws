"""
AVX scope-authorization helpers.

Each check_*_scope function implements the x-scope-predicate mechanism from
docs/api/avx-findings-actions.openapi.yaml (contract v2.2-final). All functions:
  - Return True if the caller passes, False if the caller fails scope.
  - Derive caller identity ONLY from `claims['sub']` (NEVER from path parameters).
  - Extract group membership from `cognito:groups` claim (space- or comma-separated,
    or a list when the Cognito authorizer passes it as a JSON array string).

Fleet asymmetry (as specified in x-scope-predicate and confirmed in decisions.md):
  - Dealership scope: `platform-admin` bypasses the membership predicate entirely.
  - District scope: `platform-admin` bypasses the membership predicate entirely.
  - Fleet scope: `platform-admin` is ONLY an alternative for the GROUP requirement.
    The fleet membership predicate (custom:fleetIds) STILL applies for platform-admin.
    This asymmetry is encoded in the contract and must not be widened.

The `_load_finding_by_id` helper reads from the AVX findings table (DynamoDB)
using the environment-supplied table name.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any

import boto3
from boto3.dynamodb.conditions import Key

log = logging.getLogger(__name__)

# ── Membership-claim trust gates ──────────────────────────────────────────────
#
# `custom:fleetIds`, `custom:dealerIds` and `custom:districtIds` are Cognito custom
# attributes, and a user can write their own `custom:*` attributes under an app
# client's default (unset) `WriteAttributes`. That is not hypothetical here: a
# `custom:role` escalation was demonstrated live on the CMS pool 2026-08-03, and two
# `custom:role` authorization arms were deleted from this very file on 2026-09-23 for
# exactly that reason.
#
# Fix Groups 26-27 gated the APPROVE paths in `api-avx-findings/handler.py` and left
# every READ path here open. Review cycle 3 (C1) measured the consequence: three
# routes returned 200 with another organisation's Findings on a forged claim in the
# deployed default env — `/findings/fleet`, `/findings/dealership`,
# `/findings/district`. Fix Group 29 closes that. See
# `issues/2026-09-23-avx-membership-claims-ungated-on-read-paths/`.
#
# Default-OFF, so the dangerous state is unreachable by omission rather than by the
# next reader noticing a comment. These names are the single source of truth for both
# this module and `api-avx-findings/handler.py`, which imports them — a claim-name
# string duplicated across two files is a drift hazard.
#
# One flag per attribute, NOT one shared flag: each attests to a different CMS-side
# fact (that THIS attribute is excluded from every app client's `WriteAttributes`),
# and they will be verified and enabled at different times. A shared flag would let an
# operator attest to one and silently enable three.
#
# The real fix is upstream — exclude these attributes from `WriteAttributes` in CMS,
# at which point the claims become trustworthy and every flag here can go away. These
# gates are a CVX-side stand-in until that lands.
CUSTOMER_ID_CLAIM_TRUSTED_ENV = "AVX_CUSTOMER_ID_CLAIM_TRUSTED"
VEHICLE_ID_CLAIM_TRUSTED_ENV = "AVX_VEHICLE_ID_CLAIM_TRUSTED"
FLEET_IDS_CLAIM_TRUSTED_ENV = "AVX_FLEET_IDS_CLAIM_TRUSTED"
DEALER_IDS_CLAIM_TRUSTED_ENV = "AVX_DEALER_IDS_CLAIM_TRUSTED"
DISTRICT_IDS_CLAIM_TRUSTED_ENV = "AVX_DISTRICT_IDS_CLAIM_TRUSTED"


def claim_is_trusted(env_var: str) -> bool:
    """True only when *env_var* is exactly ``"true"`` (trimmed, case-insensitive).

    Truthiness is deliberately NOT accepted — ``"1"``, ``"yes"``, ``"false "`` all stay
    closed. A flag whose entire purpose is that enabling it is a deliberate act must
    not be trippable by a stray value.
    """
    return os.environ.get(env_var, "").strip().lower() == "true"


def _trusted_claim_list(claims: dict, claim_name: str, env_var: str) -> list[str]:
    """Membership values from *claim_name*, or ``[]`` when the claim is not trusted.

    Returning an empty list is the fail-closed answer at every GRANT site in this
    module, because each one asks "is X in this list". It is NOT the right answer
    everywhere — see `is_fleet_vehicle_caller`, where the claim narrows a query rather
    than granting access, and where an empty list must still mark the caller as
    fleet-scoped or the route falls through to UNFILTERED results.
    """
    if not claim_is_trusted(env_var):
        return []
    # Members must carry at least one alphanumeric character. Review cycle 4
    # (Suggestion) noted that `custom:fleetIds = ""` parses to `[""]`, which is TRUTHY —
    # so `if fleet_ids: return True` in `check_vehicle_scope` granted on a blank claim,
    # and `is_fleet_vehicle_caller` returned `(True, [""])`.
    #
    # Writing the test for that surfaced a second shape the Suggestion did not name:
    # `_extract_claim_list` does not split on commas (that is the known parser skew, and
    # deliberately unresolved here), so a claim of `","` yields the member `","`, which
    # survives a bare `.strip()` filter. Requiring an alphanumeric closes both.
    #
    # Neither is a cross-tenant grant — no real id is empty or a lone separator — but
    # "a blank claim counts as membership" is the same emptiness-vs-identity confusion
    # review cycle 1's W2 found on the driver route.
    #
    # Safe against legitimate ids: every id in this system carries alphanumerics
    # (`FLEET-MERIDIAN-ONBOARD`, `flt-meridian-range-001`, `D-1`, `DIST-1`). This
    # rejects only values that are entirely punctuation or whitespace.
    return [
        v for v in _extract_claim_list(claims, claim_name)
        if any(ch.isalnum() for ch in str(v))
    ]

_STAGE = os.environ.get("AVX_STAGE", "dev")
_FINDINGS_TABLE = os.environ.get(
    "AVX_FINDINGS_TABLE", f"vsa-{_STAGE}-avx-findings"
)

_ddb = None


def _get_ddb():
    global _ddb
    if _ddb is None:
        _ddb = boto3.resource("dynamodb")
    return _ddb


# ---------------------------------------------------------------------------
# Claims helpers
# ---------------------------------------------------------------------------

def _extract_groups(claims: dict) -> set[str]:
    """Return the set of Cognito groups the caller belongs to.

    The `cognito:groups` claim can arrive as:
    - A space-separated string: "fleet-operator service-advisor"
    - A JSON-encoded list string: '["fleet-operator","service-advisor"]'
    - A Python list (when tests inject it directly)
    """
    raw = claims.get("cognito:groups", "")
    if isinstance(raw, list):
        return set(raw)
    raw = raw.strip()
    if not raw:
        return set()
    if raw.startswith("["):
        try:
            return set(json.loads(raw))
        except json.JSONDecodeError:
            pass
    # Space- or comma-separated
    return set(raw.replace(",", " ").split())


def _extract_claim_list(claims: dict, key: str) -> list[str]:
    """Return a list from a JSON-encoded Cognito custom claim, or empty list."""
    raw = claims.get(key, "")
    if isinstance(raw, list):
        return raw
    raw = (raw or "").strip()
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, list):
            return [str(x) for x in parsed]
    except (json.JSONDecodeError, TypeError):
        pass
    # Fallback: treat as a single value
    return [raw]


# ---------------------------------------------------------------------------
# Scope predicates (one per x-scope-predicate mechanism)
# ---------------------------------------------------------------------------

def check_vehicle_scope(claims: dict, vin: str, finding: dict | None = None) -> bool:
    """
    Scope: caller must be one of:
      - `vehicle-owner` or `consumer` group — sub must match the vehicle's owner.
        When `finding` is provided, ownership is verified against finding.scope.vin.
        When `finding` is None (route-level pre-check), vehicle-owner/consumer are
        DENIED here; the route handler must filter results by (vin AND owner_id==sub)
        and enforce ownership at the query level, not via a pre-check pass-through.
      - `fleet-driver` group — fleet membership via custom:fleetIds (VIN must belong
        to a fleet the caller holds; enforced at query level by the route handler via
        `is_fleet_vehicle_caller` + `_list_*_by_vin_for_fleet` helpers).
      - `fleet-operator` group with fleet membership via custom:fleetIds.
        Same query-level fleet-dimension enforcement as fleet-driver.
      - `service-advisor` group — all VINs visible.
      - `technician` group — all VINs visible.
      - `platform-admin` group (cross-scope) — all VINs visible.

    The three privileged paths above are GROUP-keyed only. The `custom:role` claim
    arms that used to accompany them were deleted 2026-09-23 (review cycle 2, C1):
    `custom:role` is self-writable under an app client's default `WriteAttributes`,
    so the claim arm granted all-VIN visibility to a caller in no group, and in
    `is_fleet_vehicle_caller` let a legitimate fleet caller escape fleet-dimension
    filtering. Zero live holders in either pool, measured before removal.

    C3 fix (2026-09-19 fix cycle 1): removed the fail-open
    `if finding is None: return True` branch for vehicle-owner/consumer.
    Those callers are now DENIED at this scope check; the route handler
    filters the DynamoDB query by (vin == requested_vin AND owner_id == sub)
    to enforce ownership, guaranteeing that owner-A cannot enumerate
    owner-B's findings even if they share a VIN value.

    W-C3-fleet fix (2026-09-19 fix cycle 2): fleet-driver/fleet-operator with
    no fleetIds now return False (fail-closed). Fleet callers WITH fleetIds still
    pass this pre-check (return True), BUT the route handler additionally detects
    them via `is_fleet_vehicle_caller(claims)` and routes to a fleet-filtered
    query that adds `FilterExpression=Attr('scope.fleet_id').is_in(caller_fleet_ids)`.
    This means fleet-A operators cannot enumerate findings for VINs in fleet-B.
    """
    groups = _extract_groups(claims)
    caller_sub = claims.get("sub", "")

    if "platform-admin" in groups:
        return True

    # Service roles — can view any vehicle
    if groups & {"service-advisor", "technician"}:
        return True

    # The `custom:role` arm that used to sit here is DELETED (review cycle 2, C1).
    #
    # It granted all-VIN read visibility to a caller in NO Cognito group, via an
    # attribute the caller can write to their own user record under an app client's
    # default `WriteAttributes`. `custom:role` is the specific attribute proven
    # self-writable on the CMS pool 2026-08-03.
    #
    # Deleted for the same reason, and on the same evidence, as the approve-path arm
    # removed in Fix Group 26: measured 2026-09-23 across both pools the authorizer
    # accepts, ZERO users carry `custom:role` at all in
    # `<cms-staging-user-pool-id>` (0 of 16), and the only value present in
    # `<cvx-staging-fleet-pool-id>` is `demo` (1 of 7) — so neither
    # `"service-advisor"` nor `"technician"` has a live holder on either read or
    # write path. The group arms above cover every real service role.
    #
    # Fix Group 26 deleted the approve-path arm and left these two, which made the
    # asymmetry the defect: the same forged claim was denied for approve and honoured
    # for read. See `issues/2026-09-23-avx-custom-role-claim-unconditional-approve/`.

    # Fleet-driver: must hold fleet membership (custom:fleetIds populated by CMS)
    # VIN-to-fleet ownership enforced at query level by the route handler.
    # W-C3-fleet fix (2026-09-19 fix cycle 2): fleet callers are now detected
    # explicitly in the route handler (is_fleet_caller) and routed to the
    # fleet-filtered query helper, parallel to the is_owner_caller pattern.
    # Callers with no fleetIds fail-close here (no membership = deny).
    if "fleet-driver" in groups:
        fleet_ids = _trusted_claim_list(claims, "custom:fleetIds", FLEET_IDS_CLAIM_TRUSTED_ENV)
        if fleet_ids:
            return True
        return False  # fleet-driver with no TRUSTED fleet membership → deny

    # Fleet-operator: group + fleet membership
    # VIN-to-fleet ownership enforced at query level by the route handler.
    # Callers with no fleetIds fail-close here.
    if "fleet-operator" in groups:
        fleet_ids = _trusted_claim_list(claims, "custom:fleetIds", FLEET_IDS_CLAIM_TRUSTED_ENV)
        if fleet_ids:
            return True
        return False  # fleet-operator with no TRUSTED fleet membership → deny

    # Owner self-read with a specific finding context: caller sub must match
    # the finding's scope VIN (VIN matches) — proof they own this vehicle.
    if groups & {"vehicle-owner", "consumer"}:
        if finding is not None:
            scope = finding.get("scope", {})
            if scope.get("vin") == vin:
                return True
        # No finding context and no privileged group:
        # DENY at the pre-check level. The route handler must enforce ownership
        # at the query level (filter by vin AND owner_id == caller_sub).
        # Returning False here is NOT the final word — the route handler may still
        # serve results after filtering; but a blanket pre-pass would grant access
        # to any VIN regardless of ownership (C3 defect, 2026-09-19 fix cycle 1).
        return False

    return False


def is_fleet_vehicle_caller(claims: dict) -> tuple[bool, list[str]]:
    """Return (True, fleet_ids) if the caller is a fleet-driver or fleet-operator
    with at least one fleet in custom:fleetIds; (False, []) otherwise.

    Used by route handlers to detect fleet callers and route them to a
    fleet-filtered query (_list_*_by_vin_for_fleet) rather than the unfiltered
    _list_*_by_vin query. This closes the fleet-dimension C3 residual identified
    in review Cycle 2 (2026-09-19):

      fleet-operator-in-fleet-A calling GET /findings/vehicle/{VIN-in-fleet-B}
      previously got unfiltered results. Now routed to a query that applies
      FilterExpression=Attr('scope.fleet_id').is_in(caller_fleet_ids).

    Platform-admin and service roles DO NOT go through this path — they receive
    unfiltered results (all VINs visible per contract v2.2-final).
    """
    groups = _extract_groups(claims)
    # Privileged roles that bypass fleet-dimension filtering
    if groups & {"platform-admin", "service-advisor", "technician"}:
        return False, []

    # The `custom:role` arm that used to sit here is DELETED (review cycle 2, C1).
    #
    # This one was the sharper of the two, because it did not merely grant access to
    # an outsider — it let a LEGITIMATE fleet-operator escape the fleet-dimension
    # filter added 2026-09-19 for exactly that threat. The claim was checked BEFORE
    # the fleet-group branch below, so `custom:role = "technician"` returned
    # `(False, [])`, and a caller in fleet A read Findings for VINs in fleet B — the
    # precise cross-fleet leak the W-C3-fleet fix exists to prevent, reachable by
    # writing one self-writable attribute.
    #
    # Same evidence as the sibling deletion in `check_vehicle_scope` above: zero live
    # holders of `custom:role` in either pool, measured 2026-09-23. Note the Fix
    # Group 26 measurement covered `"service-advisor"` only; `"technician"` was
    # re-measured for this deletion rather than assumed, since this arm honours both.
    if groups & {"fleet-driver", "fleet-operator"}:
        # ⚠ This site is NOT like the six grant sites in this module, and gating it the
        # same way would fail OPEN. Here the claim NARROWS a query: the caller returns
        # `(True, fleet_ids)` and the route builds a restrictive FilterExpression from
        # it. `(False, [])` means "not a fleet caller", which is the answer given to
        # privileged roles and lets the route return UNFILTERED results.
        #
        # So an untrusted claim must still mark the caller as fleet-scoped, with zero
        # permitted fleets: `(True, [])`. The route handler turns that into a 403
        # ("insufficient scope: no fleet membership"), which is the fail-closed answer.
        #
        # This is the same trap the deleted `custom:role` arm fell into from the other
        # direction — it returned `(False, [])` for a fleet caller and thereby escaped
        # the filter. Verified by mutation: returning `(False, [])` here instead must
        # fail `test_untrusted_fleet_claim_stays_fleet_scoped_not_privileged`.
        fleet_ids = _trusted_claim_list(claims, "custom:fleetIds", FLEET_IDS_CLAIM_TRUSTED_ENV)
        if fleet_ids:
            return True, fleet_ids
        # Fleet role with no TRUSTED fleetIds: still fleet-scoped, zero fleets → 403.
        return True, []
    return False, []


def check_owner_scope(claims: dict, owner_id: str) -> bool:
    """
    Scope: caller.sub must equal owner_id, OR caller is platform-admin.
    Groups: consumer, vehicle-owner accepted.
    Fleet membership NOT required.
    """
    groups = _extract_groups(claims)
    caller_sub = claims.get("sub", "")

    if "platform-admin" in groups:
        return True

    # Self-read: claims.sub must equal the requested owner_id
    # NEVER trust the path owner_id alone — always derive from sub
    return caller_sub == owner_id


def check_owner_fleet_scope(claims: dict, owner_id: str, owner_fleet_id: str | None = None) -> bool:
    """
    Scope: caller.sub must equal owner_id (owner reading their own fleet findings),
    OR caller is fleet-operator with fleet membership verified against the owner's fleet,
    OR platform-admin.

    W1 fix (2026-09-19 fix cycle 1): fleet-operator must hold the specific fleet_id
    that the owner belongs to (via `owner_fleet_id` parameter or a lookup).
    When `owner_fleet_id` is not provided, fleet-operator is DENIED (no unscoped pass).
    Route handlers that resolve the owner's fleet_id must pass it here.
    """
    groups = _extract_groups(claims)
    caller_sub = claims.get("sub", "")

    if "platform-admin" in groups:
        return True

    # Fleet-operator with membership — must hold the owner's specific fleet.
    if "fleet-operator" in groups:
        fleet_ids = _trusted_claim_list(claims, "custom:fleetIds", FLEET_IDS_CLAIM_TRUSTED_ENV)
        if fleet_ids and owner_fleet_id and owner_fleet_id in fleet_ids:
            return True
        # No owner_fleet_id provided or operator does not hold owner's fleet → deny.
        # If the route handler cannot resolve the owner's fleet_id, it should deny.
        return caller_sub == owner_id  # fall through to owner self-read

    # Owner self-read: sub must match owner_id
    return caller_sub == owner_id


def check_fleet_scope(claims: dict, fleet_id: str) -> bool:
    """
    Scope: caller must hold `fleet-operator` or `platform-admin` Cognito group
    AND fleet membership for `fleet_id` verified via custom:fleetIds.

    Fleet asymmetry: platform-admin is ONLY an alternative for the GROUP
    requirement. The fleet membership predicate (custom:fleetIds contains fleet_id)
    STILL applies even for platform-admin. This is intentional — see spec
    x-scope-predicate and decisions.md. Do not widen.
    """
    groups = _extract_groups(claims)

    # Group requirement: fleet-operator OR platform-admin (either satisfies the group gate)
    has_group = bool(groups & {"fleet-operator", "platform-admin"})
    if not has_group:
        return False

    # Membership predicate: always required regardless of group
    # platform-admin does NOT bypass fleet membership (fleet asymmetry)
    fleet_ids = _trusted_claim_list(claims, "custom:fleetIds", FLEET_IDS_CLAIM_TRUSTED_ENV)
    return fleet_id in fleet_ids


def check_dealer_scope(claims: dict, dealer_id: str) -> bool:
    """
    Scope: caller must hold `dealer-admin` Cognito group AND a DMS dealership role
    scoped to dealer_id (via custom:dealerIds claim).
    `platform-admin` bypasses both the group AND the membership predicate.
    """
    groups = _extract_groups(claims)

    if "platform-admin" in groups:
        return True

    # `dealer-admin`, not `dealer`. Corrected 2026-09-20 — see
    # issues/2026-09-20-dealer-scope-requires-nonexistent-cognito-group/.
    # `dealer` was a group that existed NOWHERE: not in CMS's ui_stack.py (which
    # provisions the 8-role DMS taxonomy and has a contract test asserting it), not in
    # DMS's own auth (which uses `dealer-admin` plus per-dealership `dealer-<city>`
    # groups), and not in either pool the API Gateway authorizer trusts. So this
    # predicate returned False for every non-platform-admin caller and both
    # dealership-scoped routes were unreachable. Found by HTTP-surface live
    # verification; invisible to unit tests because each mints `groups=["dealer"]`
    # itself, and a test that supplies the group it tests for cannot discover that no
    # identity provider issues it.
    if "dealer-admin" not in groups:
        return False

    # Membership: custom:dealerIds must contain dealer_id
    dealer_ids = _trusted_claim_list(claims, "custom:dealerIds", DEALER_IDS_CLAIM_TRUSTED_ENV)
    return dealer_id in dealer_ids


def check_district_scope(claims: dict, district_id: str) -> bool:
    """
    Scope: caller must hold `district-manager` Cognito group AND district
    membership for district_id via the custom:districtIds claim.
    `platform-admin` bypasses both the group AND the membership predicate.
    """
    groups = _extract_groups(claims)

    if "platform-admin" in groups:
        return True

    if "district-manager" not in groups:
        return False

    # Membership: custom:districtIds must contain district_id
    district_ids = _trusted_claim_list(claims, "custom:districtIds", DISTRICT_IDS_CLAIM_TRUSTED_ENV)
    return district_id in district_ids


# ---------------------------------------------------------------------------
# Finding loader (shared by findings + actions handlers)
# ---------------------------------------------------------------------------

def _load_finding_by_id(finding_id: str, ddb_client=None) -> dict | None:
    """Read a Finding from the avx-findings table by primary key (finding_id).

    Returns the item dict, or None if not found.
    """
    if ddb_client is not None:
        # Low-level client path (used by tests with boto3 low-level stubs)
        resp = ddb_client.get_item(
            TableName=_FINDINGS_TABLE,
            Key={"finding_id": {"S": finding_id}},
        )
        item = resp.get("Item")
        if not item:
            return None
        return _deserialize_ddb_item(item)
    else:
        # High-level resource path (default production path)
        table = _get_ddb().Table(_FINDINGS_TABLE)
        resp = table.get_item(Key={"finding_id": finding_id})
        return resp.get("Item")


def _deserialize_ddb_item(item: dict) -> dict:
    """Convert DynamoDB low-level typed dict to plain Python dict."""
    from boto3.dynamodb.types import TypeDeserializer
    d = TypeDeserializer()
    return {k: d.deserialize(v) for k, v in item.items()}
