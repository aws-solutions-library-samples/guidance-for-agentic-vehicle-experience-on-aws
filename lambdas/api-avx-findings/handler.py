"""
AVX Findings + Approve Lambda (G4.T1 + G4.T2)

Routes:
  GET  /findings/me                      — union of the caller's Findings; standing from
                                           trusted claims only (spec 2026-09-25-avx-own-vehicle-findings)
  GET  /findings/vehicle/{vin}           — real data, vehicle-scope auth; owner-group
                                           callers read their own by_owner / by_vehicle partition
  GET  /findings/owner/{owner_id}        — real data, owner self-read auth
  GET  /findings/owner-fleet/{owner_id}  — real data, owner-fleet auth
  GET  /findings/fleet/{fleet_id}        — real data via by_fleet GSI (T10.3)
  GET  /findings/dealership/{dealer_id}  — real data via by_dealership GSI (T10.3)
  GET  /findings/district/{district_id}  — real data via by_district GSI (T10.3)
  POST /findings/{finding_id}/approve    — create Action from Finding

Scope authorization is enforced per the frozen contract v2.2-final
x-scope-predicate blocks in docs/api/avx-findings-actions.openapi.yaml.

Environment variables (resolved at Lambda cold start):
  AVX_STAGE                   — stage name (default "dev")
  AVX_FINDINGS_TABLE          — DynamoDB table name for findings
  AVX_ACTIONS_TABLE           — DynamoDB table name for actions

  AVX_CUSTOMER_ID_CLAIM_TRUSTED, AVX_VEHICLE_ID_CLAIM_TRUSTED, AVX_FLEET_IDS_CLAIM_TRUSTED,
  AVX_DEALER_IDS_CLAIM_TRUSTED, AVX_DISTRICT_IDS_CLAIM_TRUSTED
                              — "true" to trust the matching custom:* claim; anything
                                else fails closed (_lib/avx_scope.claim_is_trusted)

Caller identity comes from JWT claims only; path parameters are NEVER trusted for
identity decisions. ``sub`` identifies the caller; ``custom:customerId``,
``custom:vehicleId``, ``custom:fleetIds``, ``custom:dealerIds`` and
``custom:districtIds`` count only when their trust flag is "true"
(docs/identity-and-standing-contract.md).
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import sys
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

import boto3
from boto3.dynamodb.conditions import Key

# ---------------------------------------------------------------------------
# Path setup — allow `from _lib.avx_scope import ...` when this file is the
# lambda root AND when tests import it with sys.path manipulation.
# ---------------------------------------------------------------------------
_LAMBDA_DIR = os.path.dirname(os.path.abspath(__file__))
_LAMBDAS_DIR = os.path.dirname(_LAMBDA_DIR)
if _LAMBDAS_DIR not in sys.path:
    sys.path.insert(0, _LAMBDAS_DIR)

from _lib.avx_scope import (  # noqa: E402
    FLEET_IDS_CLAIM_TRUSTED_ENV,
    VEHICLE_ID_CLAIM_TRUSTED_ENV,
    _extract_groups,
    _load_finding_by_id,
    check_dealer_scope,
    check_district_scope,
    check_fleet_scope,
    check_owner_fleet_scope,
    check_owner_scope,
    check_vehicle_scope,
    claim_is_trusted,
    is_fleet_vehicle_caller,
)

log = logging.getLogger(__name__)
log.setLevel(logging.INFO)

_STAGE = os.environ.get("AVX_STAGE", "dev")
_FINDINGS_TABLE = os.environ.get("AVX_FINDINGS_TABLE", f"vsa-{_STAGE}-avx-findings")
_ACTIONS_TABLE = os.environ.get("AVX_ACTIONS_TABLE", f"vsa-{_STAGE}-avx-actions")

_ddb = None


def _get_ddb():
    global _ddb
    if _ddb is None:
        _ddb = boto3.resource("dynamodb")
    return _ddb


# ---------------------------------------------------------------------------
# Response helpers
# ---------------------------------------------------------------------------

# The five top-level scope field keys that T10.2 denormalises from the nested
# scope object into a DDB top-level attribute (so the five sparse scope GSIs
# work).  These keys are NOT permitted at the root of a Finding by
# agents/tier2/schemas/finding.schema.json (root additionalProperties: false,
# 18 permitted keys — none of them a bare scope field name).  Strip them at
# the response boundary so every item in every response validates against the
# frozen contract.
#
# T11.1 — response boundary strip.  One helper; do NOT call inside query helpers
# (a future caller that needs the raw DDB item must still be able to get one).
_PROJECTED_SCOPE_ATTRS = frozenset({
    "vin",
    "owner_id",
    "fleet_id",
    "dealership_id",
    "district_id",
    # Added by Fix Group 30 (security review W1). Fix Group 25 added `vehicle_id` to
    # the writer's `_STANDING_ATTRS` and did NOT add it here, so every VIN-scoped
    # Finding with resolved standing returned a root `vehicle_id` and failed the
    # frozen contract's root `additionalProperties: false`.
    #
    # Invisible to CI because `_make_ddb_finding` fixtures never populated
    # `vehicle_id` — the one attribute the tests did not exercise was the one that
    # leaked. `test_strip_list_covers_every_writer_projected_attr` now derives the
    # expected set from the writer's source so the next attribute added there cannot
    # repeat this silently.
    "vehicle_id",
})

# F11.3 (Fix Group 15) — the claim carrying the caller's CMS customer identity,
# i.e. the namespace the projected top-level `owner_id` lives in (`CUST-XXXXXXXX`,
# from the registry's `sold_to`).
#
# NOT `sub`: `sub` is a Cognito UUID and cannot equal a `CUST-` value. Verified
# live 2026-09-23 — pool <cms-staging-user-pool-id> has 9 custom attributes, none a
# customer id, and 0 of 16 users carry a `CUST-` value anywhere.
#
# This attribute is NOT YET on the pool. Until it is added and backfilled
# (option (a) of issues/2026-09-23-avx-owner-id-not-comparable-to-cognito-sub/),
# `_caller_customer_id` returns None for every caller and the VIN-scoped approve
# path stays denied — deliberately, and fail-closed.
_CUSTOMER_ID_CLAIM = "custom:customerId"

# F11.3 / security review W1 — the claim is NOT trusted until an operator says so.
#
# A `custom:*` attribute is self-writable by default. Verified live on this very
# pool 2026-08-03 (see lambdas/api-assistant-chat/tests/test_persona_map.py
# ::test_unknown_role_does_not_escalate_to_fleet_driver): with the app client's
# `WriteAttributes` unset — Cognito's permissive default —
#   aws cognito-idp update-user-attributes --access-token <caller's own> \
#       --user-attributes Name=custom:role,Value=anything
# succeeded and the value appeared in the next ID token. That handed a consumer
# the owner-scoped persona.
#
# The identical shape here is strictly worse: `custom:customerId` is compared
# against the projected `owner_id` to authorize approving a Finding. If it were
# self-writable, ANY authenticated user could set their own `custom:customerId`
# to any `CUST-XXXXXXXX` and approve that owner's Findings — cross-tenant Action
# creation, which is exactly what W-SEC-1 closed and what this path must not
# reopen.
#
# `custom:customerId` must also be `mutable=True` so existing users can be
# backfilled, and mutable-plus-default-WriteAttributes IS the vulnerable state.
# So the requirement cannot be "make it immutable"; it has to be "keep it out of
# every app client's WriteAttributes".
#
# That requirement lives in the CMS repo, which CVX cannot assert against. So it
# is enforced here as a FAIL-CLOSED OPT-IN instead of a comment: the claim is
# ignored unless this env var is explicitly "true". Setting it is the operator's
# attestation that WriteAttributes excludes the attribute on every app client.
# Default-off means the dangerous state is unreachable by omission — a comment
# would have relied on the next reader noticing.
_CUSTOMER_ID_CLAIM_TRUSTED_ENV = "AVX_CUSTOMER_ID_CLAIM_TRUSTED"

# Per-claim trust gates for the other two standing claims.
#
# Review cycle 1 of Fix Group 25 (C2) caught a real inconsistency I introduced:
# `custom:customerId` was gated behind the env var above precisely BECAUSE a
# `custom:*` attribute is self-writable under the app client's default (unset)
# `WriteAttributes` — and then Fix Group 25 made `custom:vehicleId` and
# `custom:fleetIds` load-bearing for approve with no gate at all. Probed with the
# env unset (the deployed default): a forged vehicle or fleet claim APPROVED while
# a forged customer claim denied.
#
# docs/identity-and-standing-contract.md § 5.1 — which this session authored —
# states the requirement applies to EVERY custom:* claim used for authorization
# and that the exposure "exists NOW ... not when custom:customerId arrives". FG25
# is what made it exist. Documenting a requirement and then not applying it in the
# same change is the failure this comment exists to prevent repeating.
#
# One flag per claim, not one shared flag: each attests to a DIFFERENT CMS-side
# fact (that THIS attribute is excluded from every app client's WriteAttributes),
# and they will be verified and enabled at different times. A shared flag would let
# an operator attest to one and silently enable three.
_VEHICLE_ID_CLAIM_TRUSTED_ENV = VEHICLE_ID_CLAIM_TRUSTED_ENV
_FLEET_IDS_CLAIM_TRUSTED_ENV = FLEET_IDS_CLAIM_TRUSTED_ENV


def _caller_fleet_ids(claims: dict) -> list[str]:
    """Fleet ids from ``custom:fleetIds``, accepting BOTH JSON and CSV.

    Review cycle 1 (W3) caught that ``_lib.avx_scope._extract_claim_list`` parses
    JSON and, on failure, returns the whole string as a single element. The live
    claim is **CSV** — measured 2026-09-23:
    ``custom:fleetIds = 'flt-meridian-range-001,FLEET-DEMO-PUBLIC'`` — so a real
    multi-fleet operator got ``['flt-meridian-range-001,FLEET-DEMO-PUBLIC']``, one
    bogus element matching neither fleet. Fail-closed, so not an escalation, but the
    route Fix Group 25 just enabled did not work for the very case it exists for.
    Every test fixture used the JSON list form, which is why nothing noticed.

    Handled here rather than in ``_extract_claim_list`` on purpose: that helper is
    shared by the dealer and district scope paths, and widening a parser used by
    three authorization surfaces is a bigger change than this review cycle should
    make. Filed as a follow-on to converge them.
    """
    from _lib.avx_scope import _extract_claim_list

    parsed = _extract_claim_list(claims, "custom:fleetIds")
    out: list[str] = []
    for entry in parsed:
        # Split only the single-element whole-string fallback case; a genuine JSON
        # list whose members contain commas is not a shape Cognito produces here.
        out.extend(part.strip() for part in str(entry).split(",") if part.strip())
    return out


def _claim_is_trusted(env_var: str) -> bool:
    """Thin alias for `_lib.avx_scope.claim_is_trusted` — see that docstring.

    Fix Group 29 moved the implementation into `_lib/avx_scope.py` so this module and
    the scope helpers share ONE definition and one set of env-var names. Two copies of
    a security gate, in two files, is a drift hazard: cycle 3's C1 was precisely an
    inconsistency between what this module gated and what the scope helpers did not.
    """
    return claim_is_trusted(env_var)

# Groups whose members may hold standing over an individual VEHICLE, per
# docs/identity-and-standing-contract.md § 3.
#
# `fleet-driver` and `fleet-operator` are new here. Before Fix Group 25 the
# VIN-scope branch admitted only owner-ish groups, so a fleet driver looking at
# their own vehicle's Finding was denied even though `custom:vehicleId` names that
# exact vehicle — and a fleet operator was denied because the pre-existing
# fleet-operator check only fires on a FLEET-scoped Finding, while Agent 2 emits
# VIN-scoped Findings exclusively. Two standings that already had populated claims
# and could not be used.
#
# Membership here does NOT grant anything on its own: each standing route below
# still requires the caller's claim to match the vehicle's projected fact.
#
# ── W4: the outer gate is GROUP-keyed, the inner routes are CLAIM-keyed ────────
# Review cycle 1 asked why this differs from the pre-existing FLEET-scoped check
# 45 lines below, which requires `fleet-operator` specifically. It is deliberate,
# and the reason is what each factor actually attests to:
#
#   `cognito:groups` is admin-assigned and NOT self-writable (`_extract_groups`
#   reads only that claim). `custom:vehicleId` / `custom:fleetIds` ARE
#   self-writable under the default app-client `WriteAttributes` — which is why
#   they now sit behind `AVX_*_CLAIM_TRUSTED`. So the group check is the only
#   non-forgeable factor on this branch, and it is used as a coarse "is this a
#   vehicle-facing persona at all" filter. The BINDING to a specific vehicle or
#   fleet comes from the claim, per contract § 3, whose table keys standing on the
#   claim rather than on the group.
#
# Consequence: a caller in ANY one of these four groups obtains the owner and
# fleet_driver standings their claims support. fleet_manager is the exception
# since 2026-09-26 (spec 2026-09-25-avx-own-vehicle-findings, security review Cycle 1
# W1): it also needs the `fleet-operator` group, the same gate every read route
# applies, so an approve right is never wider than the read right.
# `test_vin_branch_fleet_standing_needs_the_fleet_operator_group` pins this.
#
# Requiring a matching group PER route was considered and rejected on live
# evidence (pool `<cms-staging-user-pool-id>`, read 2026-09-23): three of the four
# names above — `vehicle-owner`, `consumer`, `fleet-driver` — DO NOT EXIST as
# groups in that pool. Only `fleet-operator` does. Per-route group requirements
# would therefore make the owner and driver routes permanently unreachable, which
# is precisely the dead-route defect Fix Group 25 exists to fix.
#
# Live provisioning gap found while establishing the above, and NOT fixed here
# (it is CMS-side): the only users carrying standing claims are
# `meridian.driver@example.com` (`custom:vehicleId=VEH-MRDN-0015`) and
# `vehicle.owner@example.com` — and BOTH are in **no group at all**, so they fail
# this outer gate regardless of their claims. The driver and owner routes cannot
# fire for the only personas that have the claims for them. Tracked as a follow-on
# alongside the claim write-protection work.
_VEHICLE_STANDING_GROUPS = frozenset({
    "vehicle-owner",
    "consumer",
    "fleet-driver",
    "fleet-operator",
})

# Action kinds that act on the vehicle's CONDITION, per contract § 3.2.
#
# Every value in action.schema.json's action_kind enum is currently operational —
# verified 2026-09-23. So all three standings may approve all of them today, and
# the operational-vs-commercial split in § 3.2 has no live consequence yet.
#
# It WILL have one the moment a commercial kind is added (accept an offer, initiate
# a payment, transfer title), because `fleet_driver` must NOT carry commercial
# rights — a driver is not the vehicle's commercial party.
#
# `test_action_kind_enum_is_fully_classified` fails when the schema gains a kind
# absent from this set, which forces whoever adds it to classify it rather than
# inheriting driver-approvable by default. That is the whole purpose of the
# constant: it is a tripwire, not a lookup table, and nothing reads it at runtime
# yet.
_OPERATIONAL_ACTION_KINDS = frozenset({
    "book_combined_visit",
    "schedule_inspection",
    "update_charging_schedule",
    "file_warranty_pre",
    "surface_recall",
    "acknowledge_only",
})


def _strip_finding_for_response(item: dict) -> dict:
    """Remove projected top-level scope attributes added by T10.2 from a Finding item.

    T10.2 (writer.py _put_item / _update_item) writes the active scope field as a
    top-level DDB attribute (e.g. ``fleet_id="FLEET-123"`` alongside
    ``scope={"fleet_id": "FLEET-123"}``).  The denormalised attribute is required
    for the scope GSIs to work, but MUST NOT appear in API responses — the frozen
    contract (finding.schema.json) has root ``additionalProperties: false`` and
    lists 18 permitted keys, none of which is a bare scope field name.

    The nested ``scope`` object is untouched; callers read scope from ``item["scope"]``
    as before.
    """
    if not _PROJECTED_SCOPE_ATTRS.intersection(item):
        return item  # fast path: nothing to strip
    return {k: v for k, v in item.items() if k not in _PROJECTED_SCOPE_ATTRS}


class _DecimalEncoder(json.JSONEncoder):
    def default(self, o):
        if isinstance(o, Decimal):
            return int(o) if o % 1 == 0 else float(o)
        return super().default(o)


def _respond(status: int, body: dict | list) -> dict[str, Any]:
    return {
        "statusCode": status,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": os.environ.get("ALLOWED_ORIGIN", "*"),
            "Access-Control-Allow-Headers": "Content-Type,Authorization",
        },
        "body": json.dumps(body, cls=_DecimalEncoder),
    }


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


# ---------------------------------------------------------------------------
# Claims extraction
# ---------------------------------------------------------------------------

def _get_claims(event: dict) -> dict | None:
    try:
        claims = (event.get("requestContext", {})
                       .get("authorizer", {})
                       .get("claims"))
        if claims and claims.get("sub"):
            return claims
        return None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# DynamoDB helpers
# ---------------------------------------------------------------------------

def _list_findings_by_index(
    index_name: str,
    key_name: str,
    key_value: str,
    limit: int = 50,
    next_token: str | None = None,
) -> tuple[list[dict], str | None]:
    """Query findings table by a GSI. Returns (items, next_token).

    Two-slot invariant: both ``index_name`` and ``key_name`` MUST match the CDK
    declaration in ``infrastructure/lib/vsa-tier2-stack.ts`` exactly.  The
    parameters are adjacent positional strings, so a caller passing the index
    name into the key slot is silent at the call site but raises
    ``ValidationException: Query condition missed key schema element`` at DDB.

    Fix Group 10 T10.1 — the naming-convention collision between Python's
    ``<field>-index`` / ``<field>_index`` convention and the CDK's ``by_<field>``
    convention was the root cause of F1, F2, and F6.  Python moves; CDK is frozen.
    """
    table = _get_ddb().Table(_FINDINGS_TABLE)
    kwargs: dict[str, Any] = {
        "IndexName": index_name,
        "KeyConditionExpression": Key(key_name).eq(key_value),
        "ScanIndexForward": False,
        "Limit": limit,
    }
    if next_token:
        try:
            kwargs["ExclusiveStartKey"] = json.loads(
                __import__("base64").b64decode(next_token).decode()
            )
        except Exception:
            pass  # malformed cursor — start from beginning
    resp = table.query(**kwargs)
    items = resp.get("Items", [])
    last_key = resp.get("LastEvaluatedKey")
    out_token = None
    if last_key:
        out_token = __import__("base64").b64encode(
            json.dumps(last_key, cls=_DecimalEncoder).encode()
        ).decode()
    return items, out_token


def _list_findings_by_vin_for_owner(vin: str, owner_id: str, limit: int = 50, next_token: str | None = None):
    """Query findings table by VIN and filter to owner_id for vehicle-owner/consumer callers.

    C3 fix: vehicle-owner/consumer callers may only see findings where owner_id matches
    their sub claim. This enforces ownership at the query level so that owner-A cannot
    enumerate owner-B's VIN findings even with the same VIN string.

    T14.4: FilterExpression uses top-level owner_id only (the shape the T11.6 follow-on (b)
    projection will write). The previous dual-shape filter (scope.owner_id | owner_id) is
    removed: scope.owner_id was the pre-projection nested path; once the projection lands,
    only the top-level attribute exists. This path returns 503 until the projection is
    deployed, so this filter matches nothing today — intentional, proven by test.

    UNUSED since review Cycle 1 of spec 2026-09-25-avx-own-vehicle-findings (W1). Do
    not route callers here: it keys the query on the requested VIN's partition, and
    DynamoDB applies ``Limit`` before ``FilterExpression``, so a caller-chosen
    ``page_size`` makes ``next_token`` reveal whether another party's VIN has Findings.
    The owner path now uses ``_list_owner_findings_for_vin`` (the caller's own
    ``by_owner`` partition). Kept only because ``test_handler.py``'s T14.4 shape pins
    and skip guard reference it.
    """
    from boto3.dynamodb.conditions import Attr
    table = _get_ddb().Table(_FINDINGS_TABLE)
    kwargs: dict[str, Any] = {
        "IndexName": "by_vin",
        "KeyConditionExpression": Key("vin").eq(vin),
        "FilterExpression": Attr("owner_id").eq(owner_id),
        "ScanIndexForward": False,
        "Limit": limit,
    }
    if next_token:
        try:
            kwargs["ExclusiveStartKey"] = json.loads(
                __import__("base64").b64decode(next_token).decode()
            )
        except Exception:
            pass
    resp = table.query(**kwargs)
    items = resp.get("Items", [])
    last_key = resp.get("LastEvaluatedKey")
    out_token = None
    if last_key:
        out_token = __import__("base64").b64encode(
            json.dumps(last_key, cls=_DecimalEncoder).encode()
        ).decode()
    return items, out_token


def _list_caller_partition_for_vin(index_name: str, key_name: str, key_value: str, vin: str,
                                   limit: int = 50, next_token: str | None = None):
    """Query the CALLER's partition (``index_name`` on ``key_name``) filtered to ``vin``.

    Used by the vehicle route for owner-group callers. The key is the caller's own
    trusted identity, so every returned row, and every cursor, belongs to the caller.
    """
    from boto3.dynamodb.conditions import Attr
    table = _get_ddb().Table(_FINDINGS_TABLE)
    kwargs: dict[str, Any] = {
        "IndexName": index_name,
        "KeyConditionExpression": Key(key_name).eq(key_value),
        "FilterExpression": Attr("vin").eq(vin),
        "ScanIndexForward": False,
        "Limit": limit,
    }
    if next_token:
        try:
            kwargs["ExclusiveStartKey"] = json.loads(
                __import__("base64").b64decode(next_token).decode()
            )
        except Exception:
            pass
    resp = table.query(**kwargs)
    last_key = resp.get("LastEvaluatedKey")
    out_token = None
    if last_key:
        out_token = __import__("base64").b64encode(
            json.dumps(last_key, cls=_DecimalEncoder).encode()
        ).decode()
    return resp.get("Items", []), out_token


def _list_owner_findings_for_vin(owner_id: str, vin: str, limit: int = 50,
                                 next_token: str | None = None):
    """``by_owner`` on the caller's trusted customer id, filtered to ``vin``."""
    return _list_caller_partition_for_vin("by_owner", "owner_id", owner_id, vin, limit, next_token)


def _list_vehicle_findings_for_vin(vehicle_id: str, vin: str, limit: int = 50,
                                   next_token: str | None = None):
    """``by_vehicle`` on the caller's trusted vehicle id, filtered to ``vin``."""
    return _list_caller_partition_for_vin("by_vehicle", "vehicle_id", vehicle_id, vin, limit,
                                          next_token)


def _list_findings_by_vin(vin: str, limit: int = 50, next_token: str | None = None):
    """W5: Query findings by VIN via GSI (service-advisor / fleet / platform-admin paths).

    Two-slot invariant: index_name="by_vin", key_name="vin"
    CDK declaration: vsa-tier2-stack.ts indexName='by_vin', partitionKey name='vin'
    (Fix Group 10 T10.1 — Python moves to match CDK, never the reverse)
    """
    return _list_findings_by_index("by_vin", "vin", vin, limit, next_token)


def _list_findings_by_vin_for_fleet(vin: str, caller_fleet_ids: list[str], limit: int = 50, next_token: str | None = None):
    """W-C3-fleet fix (2026-09-19 fix cycle 2): Query findings by VIN filtered to
    the caller's fleet memberships.

    UNUSED, and the wrong shape to re-enable (review Cycles 1-3 of spec
    2026-09-25-avx-own-vehicle-findings). It keys on the requested VIN's partition and
    filters on fleet_id; DynamoDB applies Limit before the filter, so a caller-chosen
    page_size turns next_token into a per-VIN existence oracle, and the filter alone does
    NOT stop a fleet-A caller learning about fleet-B VINs. The fleet-caller VIN route
    returns 503 (the writer does project top-level fleet_id now; the 503 stays until the
    route is rebuilt on by_fleet). Kept only for the T14.4 FilterExpression shape pin.
    """
    from boto3.dynamodb.conditions import Attr
    table = _get_ddb().Table(_FINDINGS_TABLE)
    kwargs: dict[str, Any] = {
        "IndexName": "by_vin",
        "KeyConditionExpression": Key("vin").eq(vin),
        "FilterExpression": Attr("fleet_id").is_in(caller_fleet_ids),
        "ScanIndexForward": False,
        "Limit": limit,
    }
    if next_token:
        try:
            kwargs["ExclusiveStartKey"] = json.loads(
                __import__("base64").b64decode(next_token).decode()
            )
        except Exception:
            pass
    resp = table.query(**kwargs)
    items = resp.get("Items", [])
    last_key = resp.get("LastEvaluatedKey")
    out_token = None
    if last_key:
        out_token = __import__("base64").b64encode(
            json.dumps(last_key, cls=_DecimalEncoder).encode()
        ).decode()
    return items, out_token


def _list_findings_by_owner(owner_id: str, limit: int = 50, next_token: str | None = None):
    """W5: Query findings by owner_id via GSI (owner self-read path).

    Two-slot invariant: index_name="by_owner", key_name="owner_id"
    CDK declaration: vsa-tier2-stack.ts indexName='by_owner', partitionKey name='owner_id'
    (Fix Group 10 T10.1 — Python moves to match CDK, never the reverse)
    """
    return _list_findings_by_index("by_owner", "owner_id", owner_id, limit, next_token)


def _list_findings_by_fleet(fleet_id: str, limit: int = 50, next_token: str | None = None):
    """Query findings by fleet_id via GSI.

    Two-slot invariant: index_name="by_fleet", key_name="fleet_id"
    CDK declaration: vsa-tier2-stack.ts indexName='by_fleet', partitionKey name='fleet_id'
    (Fix Group 10 T10.1 + T10.3 — T10.2 denormalises fleet_id to top-level attribute,
    making this GSI effective; T10.3 wires this route.)
    """
    return _list_findings_by_index("by_fleet", "fleet_id", fleet_id, limit, next_token)


def _list_findings_by_dealership(dealership_id: str, limit: int = 50, next_token: str | None = None):
    """Query findings by dealership_id via GSI.

    Two-slot invariant: index_name="by_dealership", key_name="dealership_id"
    CDK declaration: vsa-tier2-stack.ts indexName='by_dealership', partitionKey name='dealership_id'
    (Fix Group 10 T10.3 — T10.2 denormalises dealership_id to top-level attribute.)
    """
    return _list_findings_by_index("by_dealership", "dealership_id", dealership_id, limit, next_token)


def _list_findings_by_district(district_id: str, limit: int = 50, next_token: str | None = None):
    """Query findings by district_id via GSI.

    Two-slot invariant: index_name="by_district", key_name="district_id"
    CDK declaration: vsa-tier2-stack.ts indexName='by_district', partitionKey name='district_id'
    (Fix Group 10 T10.3 — T10.2 denormalises district_id to top-level attribute.)
    """
    return _list_findings_by_index("by_district", "district_id", district_id, limit, next_token)


def _list_findings_by_vehicle(vehicle_id: str, limit: int = 50, next_token: str | None = None):
    """Query findings by the projected ``vehicle_id`` (VEH-*) via GSI.

    Two-slot invariant: index_name="by_vehicle", key_name="vehicle_id"
    CDK declaration: vsa-tier2-stack.ts indexName='by_vehicle', partitionKey name='vehicle_id'
    (spec 2026-09-25-avx-own-vehicle-findings T2.1; the writer projects vehicle_id since
    Fix Group 25).
    """
    return _list_findings_by_index("by_vehicle", "vehicle_id", vehicle_id, limit, next_token)


# ---------------------------------------------------------------------------
# Approve: create Action from Finding
# ---------------------------------------------------------------------------

_VALID_TARGET_SYSTEMS = {"dms", "vehicle-command", "retail", "none", "chat"}
# ios-owner is REJECTED on the approve route — it is the reverse-direction
# executor; its writer is Spec 2's dealer-side "propose to customer" surface.
# Rejecting it here closes the security W1 finding from contract v2.2 (Fix Group 2).
_FORBIDDEN_TARGET_SYSTEMS = {"ios-owner"}

_ACTION_KINDS = {
    "book_combined_visit",
    "schedule_inspection",
    "update_charging_schedule",
    "file_warranty_pre",
    "surface_recall",
    "acknowledge_only",
    # Both RetailExecutor kinds. Until 2026-09-20 neither was listed, so every
    # target_system="retail" approval was rejected 400 on action_kind and
    # RetailExecutor was unreachable from this route for BOTH its kinds — a dead
    # executor that read as wired. Added per spec
    # 2026-09-20-avx-acquire-configurator-target; `lead_capture` was added in the
    # same change because it was dead for the identical reason.
    #
    # Gated at runtime by _dispatch_enabled() below — see _RETAIL_KINDS.
    "lead_capture",
    "configurator_recommend",
}

#: Kinds whose executor exists but which the frozen contract cannot yet represent.
#:
#: HISTORY: this gate was introduced while dispatch was unwired, to avoid persisting
#: `pending_execution` rows that nothing would execute. Fix Group 35 wired dispatch,
#: so that premise no longer holds. The gate stays for a different reason
#: (D-G10-3): the retail kinds are absent from the frozen `action_kind` enum, so
#: accepting one writes an Action that fails `urn:cvx:avx:action:v1`. Adding them
#: rotates all three contract pins that the Group 10 pre-flight checks, which is a
#: contract version bump, not a side effect of wiring dispatch.
#:
#: This matters because `ApiAcquireLeads` and `ApiAvxFindings` share one CloudFormation
#: stack (`vsa-{stage}-api`), so the idempotency fix cannot be deployed without also
#: deploying this route — the gate is what makes that deploy safe rather than a choice
#: between shipping a stall and shipping nothing.
#:
#: To lift the gate, first add both retail kinds to all three
#: ``action_kind`` enum sites — the gate is currently the only thing preventing
#: schema-invalid Actions from being written silently (flag true + retail kind →
#: HTTP 200, row written, ``action_kind`` fails ``urn:cvx:avx:action:v1``):
#:   1. ``agents/tier2/schemas/action.schema.json``              (line ~71, ``"enum"``)
#:   2. ``docs/api/avx-findings-actions.openapi.yaml``           (line ~1366, Action schema)
#:   3. ``docs/api/avx-findings-actions.openapi.yaml``           (line ~1550, ApproveRequest)
#:
#: Those three are the sites that make a written Action *valid*. There is a
#: fourth hand-copy of the same six kinds that affects *coverage* rather than
#: validity, and it is the one CI actually runs:
#:   4. ``tests/schemas/test_finding_action_schemas.py`` (~line 474,
#:      ``TestActionKindEnum`` parametrize) — add the retail kinds here too, or
#:      the schema suite will keep passing while silently never exercising them.
#:      Note that ``lambdas/api-avx-findings/tests/test_handler.py`` — where the
#:      gate's own tests live — is run by no CI job, so this parametrize is the
#:      only CI-visible assertion over the enum.
_RETAIL_KINDS = frozenset({"lead_capture", "configurator_recommend"})


def _dispatch_enabled() -> bool:
    """True when executor dispatch is wired into this deployable.

    Defaults to FALSE: a stage that has not been told dispatch exists must assume it
    does not, because the failure mode of guessing wrong is a silently stranded Action.
    """
    return os.environ.get("VSA_AVX_EXECUTOR_DISPATCH_ENABLED", "false").strip().lower() in (
        "1", "true", "yes",
    )


def _derive_action_id(finding_id: str, action_kind: str, created_by: str) -> str:
    """Derive a deterministic action_id for idempotency.

    sha256(f"{finding_id}|{action_kind}|{created_by}")[:32] used as the UUID hex,
    formatted as AVX-ACT-{uuid4-like} for schema compatibility.
    """
    h = hashlib.sha256(f"{finding_id}|{action_kind}|{created_by}".encode()).hexdigest()
    # Construct a UUID-like string from the first 32 hex chars
    # Format: xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
    s = h[:32]
    uid = f"{s[0:8]}-{s[8:12]}-{s[12:16]}-{s[16:20]}-{s[20:32]}"
    return f"AVX-ACT-{uid}"


# ---------------------------------------------------------------------------
# Executor dispatch (Fix Group 35)
# ---------------------------------------------------------------------------

#: `action.schema.json` `$defs/audit_entry.properties`, verbatim. That object is
#: `additionalProperties: false`, and `ExecuteResult.audit_entry()` also emits
#: `last_error_class` / `last_error_status`, so persisting it unprojected writes a
#: schema-invalid row on every failure (D-G10-6). Pinned to the schema file by
#: `TestDispatchAuditEntrySchemaPin`.
_AUDIT_ENTRY_KEYS = ("at", "by", "from_status", "to_status", "note", "failure_kind", "retry_count")

#: Executor result → the only prior statuses the result may be written over.
#: `in_progress` without a `target_ref` is DMS's own `dms.ro.status_changed`
#: (`Draft`), which DMS emits BEFORE returning 201 — so the event consumer can
#: project `pending_execution → in_progress` before this handler records the
#: `ro_id`. Without that arm the receipt would be lost to the race.
_DISPATCH_RESULT_CONDITION = (
    "#s = :pending OR (#s = :in_progress AND attribute_not_exists(#r))"
)


def _bearer_token(event: dict) -> str | None:
    """The caller's raw ID token, for executors that forward it downstream.

    The authorizer's `claims` carry no token, and `DmsExecutor` / `RetailExecutor` /
    `VehicleCommandExecutor` forward the CALLER's JWT rather than this Lambda's role
    (a role would be a confused deputy — `retail.py`). The header is the only place
    it exists. Never logged.
    """
    headers = event.get("headers") or {}
    if not isinstance(headers, dict):
        return None
    raw = next(
        (v for k, v in headers.items() if isinstance(k, str) and k.lower() == "authorization"),
        None,
    )
    if not isinstance(raw, str):
        return None
    raw = raw.strip()
    if raw[:7].lower() == "bearer ":
        raw = raw[7:].strip()
    return raw or None


def _dispatch_action(action: dict, claims: dict) -> Any:
    """Route through `registry.dispatch`, the one D5 escalation enforcement point.

    Imported lazily: only the approve route dispatches, and the read routes must not
    pay for (or be failed by) importing six executors and `requests`.
    """
    from agents.executors import registry  # noqa: PLC0415

    return registry.dispatch(action, claims)


def _persisted_audit_entry(result: Any, *, target_system: str, at: str) -> dict[str, Any]:
    """Project `result.audit_entry()` onto the schema's keys (D-G10-6).

    The two keys the schema forbids are not dropped. They are what an operator needs
    to tell "dealer said no" from "we could not reach the dealer", so they are folded
    into `note`, which the schema allows. They carry class names and status codes
    only, never exception messages.
    """
    raw = result.audit_entry(by=f"executor-{target_system}", at=at)
    entry = {k: raw[k] for k in _AUDIT_ENTRY_KEYS if k in raw}
    detail = [
        f"{k}={raw[k]}" for k in ("last_error_class", "last_error_status")
        if k in raw and raw[k] is not None
    ]
    if detail:
        entry["note"] = "; ".join(filter(None, [entry.get("note"), ", ".join(detail)]))
    return entry


def _record_dispatch_result(action: dict, result: Any) -> dict[str, Any]:
    """Advance the Action with the executor's result. Returns the stored row.

    Raises `ConditionalCheckFailedException` if the row left `pending_execution`
    for anything but the consumer's receipt-less `in_progress` (see
    `_DISPATCH_RESULT_CONDITION`), e.g. an owner cancelled mid-dispatch.
    """
    # Assigned here rather than passed in: the T11.3 IAM call-site guard
    # (test/vsa-api-stack-avx.test.ts) attributes `x.update_item(` by the nearest
    # preceding `x = ....Table(<CONST>)`, so a parameter would be read as whatever
    # table was last assigned above this function in the file.
    table = _get_ddb().Table(_ACTIONS_TABLE)
    at = _now_iso()
    entry = _persisted_audit_entry(result, target_system=action["target_system"], at=at)
    update = "SET #s = :to, #u = :now, #a = list_append(#a, :entry)"
    values: dict[str, Any] = {
        ":to": result.status,
        ":now": at,
        ":entry": [entry],
        ":pending": "pending_execution",
        ":in_progress": "in_progress",
    }
    if result.target_ref:
        update += ", #r = :ref"
        values[":ref"] = result.target_ref
    resp = table.update_item(
        Key={"action_id": action["action_id"]},
        UpdateExpression=update,
        ConditionExpression=_DISPATCH_RESULT_CONDITION,
        ExpressionAttributeNames={
            "#s": "status", "#u": "updated_at", "#a": "audit_trail", "#r": "target_ref",
        },
        ExpressionAttributeValues=values,
        ReturnValues="ALL_NEW",
    )
    return resp["Attributes"]


#: The callers DMS's create-RO route admits (W2). Cognito-issued groups, not a
#: `custom:*` claim, so no trust attestation is involved.
_DMS_BOOKING_GROUPS = frozenset({"fleet-operator", "platform-admin"})


def _approve_request_differs(existing: dict, target_system: str, parameters: dict) -> bool:
    """True if a re-approve names a different target or parameters than the stored
    Action. Compared as canonical JSON so DynamoDB's `Decimal`s equal body ints."""
    def canon(value: Any) -> str:
        return json.dumps(value or {}, sort_keys=True, cls=_DecimalEncoder)

    return (existing.get("target_system") != target_system
            or canon(existing.get("parameters")) != canon(parameters))


def _dms_parameters(finding: dict, parameters: dict) -> tuple[dict | None, str | None]:
    """Validate and complete `parameters` for `target_system=dms` (D-G10-2).

    Returns `(parameters, None)` or `(None, error)`. The VIN is taken from the
    Finding, never the body: approve authorization proved standing over THIS
    Finding's vehicle, so a body-supplied VIN would let a caller approve Finding A
    and book service for vehicle B.
    """
    vin = (finding.get("scope") or {}).get("vin")
    if not isinstance(vin, str) or not vin:
        return None, ("target_system 'dms' needs a VIN-scoped Finding: a repair "
                      "order is booked for one vehicle")
    supplied = parameters.get("vehicle_vin")
    if supplied is not None and supplied != vin:
        return None, "parameters.vehicle_vin must match the Finding's vehicle"
    dealer_id = parameters.get("dealer_id")
    if not isinstance(dealer_id, str) or not dealer_id.strip():
        return None, "parameters.dealer_id is required for target_system 'dms'"
    return {**parameters, "vehicle_vin": vin, "dealer_id": dealer_id.strip()}, None


def _approve_finding(event: dict, claims: dict) -> dict[str, Any]:
    """POST /findings/{finding_id}/approve"""
    path_params = event.get("pathParameters") or {}
    finding_id = path_params.get("finding_id") or path_params.get("findingId")
    if not finding_id:
        return _respond(400, {"error": "finding_id required"})

    try:
        body = json.loads(event.get("body") or "{}")
    except json.JSONDecodeError:
        return _respond(400, {"error": "invalid JSON body"})

    target_system = body.get("target_system")
    action_kind = body.get("action_kind")
    parameters = body.get("parameters", {})

    # Validate target_system
    if not target_system:
        return _respond(400, {"error": "target_system required"})
    if target_system in _FORBIDDEN_TARGET_SYSTEMS:
        # ios-owner is rejected — security W1, contract v2.2-final
        return _respond(400, {
            "error": f"target_system '{target_system}' is not permitted on the approve route. "
                     "The ios-owner executor is the reverse-direction target written by Spec 2's "
                     "dealer surface, not by owner approval."
        })
    if target_system not in _VALID_TARGET_SYSTEMS:
        return _respond(400, {"error": f"unknown target_system: {target_system}"})
    if not action_kind or action_kind not in _ACTION_KINDS:
        return _respond(400, {"error": f"action_kind must be one of: {sorted(_ACTION_KINDS)}"})
    if action_kind in _RETAIL_KINDS and not _dispatch_enabled():
        # Fail closed. Accepting this would write an Action whose action_kind the
        # frozen contract does not contain — see _RETAIL_KINDS (D-G10-3).
        return _respond(400, {
            "error": f"action_kind '{action_kind}' is not executable in this "
                     "deployment: it is not in the Action contract's action_kind "
                     "enum, so the action would be recorded invalidly and never run."
        })

    # Load the Finding
    finding = _load_finding_by_id(finding_id)
    if not finding:
        return _respond(404, {"error": f"Finding {finding_id} not found"})

    # Scope check: caller must be the owner of the Finding's scope, OR
    # hold service-advisor / fleet-operator / platform-admin
    if not _check_approve_scope(claims, finding):
        return _respond(403, {"error": "insufficient scope to approve this finding"})

    # Target-specific parameters, validated AFTER the scope check so the 400s below
    # are not an oracle on a Finding the caller cannot see, and BEFORE any write so
    # an Action that cannot be dispatched is never recorded.
    if not isinstance(parameters, dict):
        return _respond(400, {"error": "parameters must be an object"})
    if target_system == "dms":
        # W2 (review Cycle 1): DMS's create-RO route admits fleet-operator or
        # platform-admin only (`repair_orders.py` `authorize(required_groups=
        # [FLEET_OPERATOR])` + the admin bypass). Anyone else would pass CVX's
        # approve check, be refused 403 by DMS, record `failed`, and page an
        # operator on every such approve. Refused here instead, before any write.
        if not (_extract_groups(claims) & _DMS_BOOKING_GROUPS):
            return _respond(403, {
                "error": "target_system 'dms' books through DMS's fleet repair-order "
                         "API, which accepts fleet-operator or platform-admin callers "
                         "only; owner booking through DMS is not available yet",
            })
        parameters, param_error = _dms_parameters(finding, parameters)
        if param_error:
            return _respond(400, {"error": param_error})

    # Derive idempotent action_id
    caller_sub = claims.get("sub", "")
    created_by = f"owner-{caller_sub}"
    action_id = _derive_action_id(finding_id, action_kind, created_by)
    now = _now_iso()

    # Build the Action row
    action = {
        "action_id": action_id,
        "finding_id": finding_id,
        "created_by": created_by,
        "created_at": now,
        "updated_at": now,
        "target_system": target_system,
        "status": "pending_execution",
        "action_kind": action_kind,
        "parameters": parameters,
        "evidence": finding.get("evidence", []),  # copy evidence from Finding
        # audit_trail starts empty: the trail records *transitions*, and a creation
        # is not a transition.  from_status is required and enum-constrained in
        # $defs/audit_entry, so no valid creation sentinel exists.  The creation
        # facts are already carried by created_at and created_by above.
        # Rejected alternatives (see decisions.md T14.7):
        #   (a) from_status=None — schema-invalid; (b) from_status=to_status — a
        #   fabricated self-transition that lies about history.
        "audit_trail": [],
    }

    # Idempotent PutItem: if action_id already exists, fetch and return existing row
    table = _get_ddb().Table(_ACTIONS_TABLE)
    try:
        table.put_item(
            Item=action,
            ConditionExpression="attribute_not_exists(action_id)",
        )
    except table.meta.client.exceptions.ConditionalCheckFailedException:
        # Already exists — return the existing row
        # ConsistentRead (review Cycle 2 S3): the conditional put just proved the
        # row exists; an eventually-consistent miss fell back to THIS request's
        # row and so defeated the W4 conflict check below.
        existing = table.get_item(Key={"action_id": action_id},
                                  ConsistentRead=True).get("Item", action)
        log.info("approve idempotency hit for action_id=%s", action_id)
        # W4 (review Cycle 1): the id is keyed on (finding, kind, caller), so a
        # re-approve naming a different target or parameters (another dealer) used
        # to get the old row back as if it had been honoured. The contract's 409.
        if _approve_request_differs(existing, target_system, parameters):
            return _respond(409, {
                "error": "an Action already exists for this Finding and action_kind "
                         "with a different target_system or parameters",
                "action_id": action_id,
            })
        return _respond(200, existing)
    except Exception as exc:
        log.error("DynamoDB PutItem failed for action_id=%s: %s", action_id, exc)
        return _respond(500, {"error": "failed to persist action"})

    # Dispatch (Fix Group 35). Only the request that won the put gets here, so a
    # re-approve cannot dispatch twice (D-G10-1). The executor needs the caller's
    # own token; the authorizer claims do not carry it.
    dispatch_claims = dict(claims)
    token = _bearer_token(event)
    if token:
        dispatch_claims["token"] = token
    try:
        result = _dispatch_action(action, dispatch_claims)
    except Exception as exc:  # noqa: BLE001
        # Executors never raise (@never_raises); this is the registry itself, or
        # the import. The row stays pending_execution, and a retry hits
        # idempotency rather than dispatching again. Class name only: an exception
        # message here can carry the forwarded token.
        log.error("dispatch raised for action_id=%s target_system=%s (%s); Action "
                  "left pending_execution", action_id, target_system, type(exc).__name__)
        return _respond(500, {"error": "action recorded but not dispatched",
                              "action_id": action_id})

    try:
        stored = _record_dispatch_result(action, result)
    except table.meta.client.exceptions.ConditionalCheckFailedException:
        # The row left pending_execution while the executor ran — an owner cancel
        # or dismiss. Their intent wins; the downstream work may still exist, so
        # its reference is logged for an operator to reconcile.
        log.warning("dispatch result not recorded for action_id=%s target_system=%s: "
                    "row changed during dispatch (result=%s target_ref=%s)",
                    action_id, target_system, result.status, result.target_ref)
        try:
            current = table.get_item(Key={"action_id": action_id},
                                     ConsistentRead=True).get("Item", action)
        except Exception as exc:  # noqa: BLE001
            log.error("re-read after lost dispatch race failed for action_id=%s (%s)",
                      action_id, type(exc).__name__)
            return _respond(500, {"error": "action changed during dispatch and could "
                                           "not be re-read", "action_id": action_id})
        return _respond(200, current)
    except Exception as exc:  # noqa: BLE001
        log.error("dispatch result write failed for action_id=%s target_system=%s "
                  "(%s): result=%s target_ref=%s", action_id, target_system,
                  type(exc).__name__, result.status, result.target_ref)
        return _respond(500, {"error": "action dispatched but its result was not recorded",
                              "action_id": action_id})

    log.info("approved finding_id=%s → action_id=%s target_system=%s status=%s",
             finding_id, action_id, target_system, stored.get("status"))
    return _respond(200, stored)


def _caller_customer_id(claims: dict) -> str | None:
    """Return the caller's customer identity, or ``None`` if none is available.

    **This is the (d) seam.** Fix Group 15 builds the owner projection and this
    read path now; the identity *mapping* is the one piece that needs a decision
    outside CVX, and it is isolated here so that decision is a configuration
    change rather than a code change.

    Why a separate claim and not ``claims["sub"]``
    ----------------------------------------------
    The projected top-level ``owner_id`` is the CMS registry's ``sold_to``, whose
    values are ``CUST-XXXXXXXX``. Cognito ``sub`` values are UUIDs. Comparing the
    two can never succeed — verified live 2026-09-23 against
    ``cms-staging-storage-vehicles`` and pool ``<cms-staging-user-pool-id>``, where 0 of
    16 users carry a ``CUST-`` value in any attribute. See
    ``issues/2026-09-23-avx-owner-id-not-comparable-to-cognito-sub/``.

    So this reads ``custom:customerId``, which is the attribute option (a) of
    that issue adds to the CMS pool. **Update 2026-09-26**: the attribute exists
    (since 2026-09-24) and the staging trust flag is on, so this now returns real
    identities; the paragraph below is kept as the record of why it was inert.
    Before that, the attribute did not exist, so this returned ``None`` for every
    caller and the VIN-scoped approve path stayed denied — the same outcome as
    before Fix Group 15, reached deliberately and fail-closed rather than by accident.

    This is inert, not stubbed: when the pool gains the attribute and users are
    backfilled, this function starts returning real identities and the path
    begins working with no change here. What it must never do is fall back to
    ``sub`` to "make something match" — that would compare two different
    namespaces and, depending on the data, could match the wrong person.

    ``scope.owner_id`` on an owner-scoped Finding is a different case and keeps
    using ``sub``: that value is written by executors that set it *from* ``sub``
    (see ``agents/executors/chat.py``), so the namespaces there already agree.

    Fail-closed on the claim's trustworthiness, not just its presence
    ----------------------------------------------------------------
    Returns ``None`` unless ``AVX_CUSTOMER_ID_CLAIM_TRUSTED`` is explicitly
    ``"true"``. See ``_CUSTOMER_ID_CLAIM_TRUSTED_ENV`` above for why: a
    ``custom:*`` attribute is self-writable under Cognito's default
    ``WriteAttributes``, and a self-writable claim compared against a projected
    owner id is cross-tenant approve. The env var is the operator's attestation
    that the attribute is excluded from every app client's ``WriteAttributes``.

    So provisioning the attribute is necessary but NOT sufficient to enable this
    path — which is deliberate, because provisioning it is the easy half.
    """
    if not _claim_is_trusted(_CUSTOMER_ID_CLAIM_TRUSTED_ENV):
        return None
    value = claims.get(_CUSTOMER_ID_CLAIM)
    if not isinstance(value, str):
        return None
    return value.strip() or None


def _is_party_of_record(claims: dict, owner_id: str) -> bool:
    """True iff the caller's trusted customer id IS `owner_id` (Fix Group 36).

    The owner read routes compared only `sub == owner_id`, but the `by_owner` GSI
    holds two namespaces (D-G10-4): owner-scoped Findings carry `scope.owner_id`
    written from `sub`, and VIN-scoped Findings carry the projected `sold_to`
    (`CUST-XXXXXXXX`). A Cognito `sub` is a UUID, so an owner could never read
    their own projected Findings. This is the second comparison, the same one
    `_check_approve_scope` already makes for VIN-scoped Findings. The shapes are
    disjoint, so neither comparison can match the other namespace's rows.

    Exact equality only, and `None` from an untrusted or absent claim never
    matches: `_caller_customer_id` is the fail-closed gate.
    """
    customer_id = _caller_customer_id(claims)
    return customer_id is not None and customer_id == owner_id


# ---------------------------------------------------------------------------
# GET /findings/me — standing derived server-side (spec 2026-09-25-avx-own-vehicle-findings)
# ---------------------------------------------------------------------------

#: Upper bound on GSI pages read per standing in one /findings/me call. Each page is
#: up to 200 items; a vehicle, owner or fleet with more open Findings than this is
#: truncated and logged rather than timing the Lambda out.
_ME_MAX_PAGES = 10


def _caller_vehicle_id(claims: dict) -> str | None:
    """The caller's trusted ``custom:vehicleId`` (VEH-*), or ``None``.

    Same fail-closed rule as the approve path (``_check_approve_scope``): the claim is
    read only when ``AVX_VEHICLE_ID_CLAIM_TRUSTED`` is ``"true"``, because the
    attribute is self-writable under Cognito's default ``WriteAttributes``. Blank
    values never count.
    """
    if not _claim_is_trusted(_VEHICLE_ID_CLAIM_TRUSTED_ENV):
        return None
    value = claims.get("custom:vehicleId")
    if not isinstance(value, str):
        return None
    return value.strip() or None


def _trusted_fleet_ids(claims: dict) -> list[str]:
    """``custom:fleetIds`` (JSON or CSV), only when ``AVX_FLEET_IDS_CLAIM_TRUSTED`` is "true".

    ``_caller_fleet_ids`` parses both shapes but is not trust-gated; this is the gate.
    """
    if not _claim_is_trusted(_FLEET_IDS_CLAIM_TRUSTED_ENV):
        return []
    return _caller_fleet_ids(claims)


def _read_all_pages(fetch, key: str, truncated: list[str]) -> list[dict]:
    """Call ``fetch(key, limit=200, next_token=...)`` until the last page.

    Bounded at ``_ME_MAX_PAGES``; when the cap is hit the key is appended to
    ``truncated`` so the response can say so instead of silently dropping rows.
    """
    items: list[dict] = []
    token = None
    for _ in range(_ME_MAX_PAGES):
        page, token = fetch(key, limit=200, next_token=token)
        items.extend(page or [])
        if not token:
            return items
    log.warning("findings_me: page cap %d reached for key=%s; result truncated", _ME_MAX_PAGES, key)
    truncated.append(key)
    return items


def _not_expired(finding: dict, now_iso: str) -> bool:
    """False only when ``expires_at`` is present and not in the future.

    ISO-8601 UTC "Z" strings compare correctly as strings.
    """
    expires_at = finding.get("expires_at")
    return not (isinstance(expires_at, str) and expires_at and expires_at <= now_iso)


def _findings_me(claims: dict) -> dict[str, Any]:
    """GET /findings/me: the union of Findings the caller has standing over.

    Standings (docs/identity-and-standing-contract.md § 3), each from a TRUSTED claim
    only — the request carries no identifier, and query parameters are ignored:
      * ``fleet_driver``: trusted ``custom:vehicleId`` plus a vehicle-standing group →
        ``by_vehicle``, kept only where the projected ``vehicle_id`` equals the claim.
        No CMS read: standing facts are projected at write time and intersected at read
        time (contract § 6, pinned by test_approve_scope_never_reads_the_cms_registry).
      * ``owner``: trusted ``custom:customerId`` plus a vehicle-standing group →
        ``by_owner``, kept only where ``owner_id`` equals it.
      * ``fleet_manager``: the ``fleet-operator`` group (the same group gate as
        ``/findings/fleet/{id}``; a ``fleet-driver`` reads only their vehicle) plus trusted
        ``custom:fleetIds`` (JSON or CSV) → ``by_fleet`` per fleet, kept only where
        ``fleet_id`` matches. The claim PARSER differs from ``/findings/fleet/{id}``,
        whose ``check_fleet_scope`` does not split CSV (review Cycle 2 S12; converging
        them is the follow-on named in ``_caller_fleet_ids``).
    Expired Findings are dropped. Findings are returned contract-shaped (projected
    scope attributes stripped, as on every other route); which standing admitted each
    one is reported beside them in ``admitted_by`` (finding_id → standings), so no
    Finding gains a root key the frozen schema forbids.
    No standing is ``200`` with empty ``findings`` and ``standings``, not an error.
    """
    groups = _extract_groups(claims)
    now_iso = _now_iso()
    admitted: dict[str, dict] = {}
    tags: dict[str, set[str]] = {}
    standings: set[str] = set()
    _truncated: list[str] = []

    def _admit(finding: dict, standing: str) -> None:
        fid = finding.get("finding_id")
        if not isinstance(fid, str) or not fid or not _not_expired(finding, now_iso):
            return
        admitted.setdefault(fid, finding)
        tags.setdefault(fid, set()).add(standing)

    if groups & _VEHICLE_STANDING_GROUPS:
        vehicle_id = _caller_vehicle_id(claims)
        if vehicle_id:
            standings.add("fleet_driver")
            for f in _read_all_pages(_list_findings_by_vehicle, vehicle_id, _truncated):
                if f.get("vehicle_id") == vehicle_id:
                    _admit(f, "fleet_driver")

        customer_id = _caller_customer_id(claims)
        if customer_id:
            standings.add("owner")
            for f in _read_all_pages(_list_findings_by_owner, customer_id, _truncated):
                if f.get("owner_id") == customer_id:
                    _admit(f, "owner")

    fleet_ids = _trusted_fleet_ids(claims) if "fleet-operator" in groups else []
    if fleet_ids:
        standings.add("fleet_manager")
        for fleet_id in sorted(set(fleet_ids)):
            for f in _read_all_pages(_list_findings_by_fleet, fleet_id, _truncated):
                if f.get("fleet_id") == fleet_id:
                    _admit(f, "fleet_manager")

    return {
        "findings": [_strip_finding_for_response(admitted[fid]) for fid in sorted(admitted)],
        "admitted_by": {fid: sorted(tags[fid]) for fid in sorted(admitted)},
        "standings": sorted(standings),
        "computed_at": now_iso,
        "truncated": bool(_truncated),
    }


def _check_approve_scope(claims: dict, finding: dict) -> bool:
    """Caller must be the owner of the Finding's scope, OR hold a privileged role.

    W2 fix (2026-09-19 fix cycle 1): removed unscoped group-membership passes for
    fleet-operator and service-advisor. Now fleet-operator must hold membership in
    the finding's fleet; service-advisor checks custom:dealerIds if finding has a
    dealer scope. platform-admin bypasses all.

    W-SEC-1 fix (2026-09-19 security fix cycle 1, Path B): the VIN-scope fallback
    for vehicle-owner/consumer fails closed when no owner binding is present.

    F11.3 (Fix Group 15): the VIN-scope branch now reads the **top-level**
    ``owner_id`` that the Tier 2 writer projects onto the DDB item (F11.2),
    instead of ``scope.owner_id``. The nested read was unreachable-in-the-positive
    by construction: the Finding schema's VIN branch forbids ``scope.owner_id``,
    so that check could only ever deny. Owner-scoped Findings keep working via
    the nested value — see the ``scope_owner_id`` branch below, which is NOT
    dead code (removing it denies an owner their own owner-scoped Finding).

    The caller identity compared against the projection comes from
    :func:`_caller_customer_id` — see that docstring for why it is not ``sub``.
    """
    groups = _extract_groups(claims)
    caller_sub = claims.get("sub", "")

    if "platform-admin" in groups:
        return True

    # Service-advisor: accepts any finding (broad service-lane access by design).
    # Accepting the risk: service-advisor is a narrow role, not fleet-scoped.
    #
    # The `custom:role == "service-advisor"` arm that used to sit here is DELETED.
    # Review cycle 1 (C3) caught that it granted unconditional approve on any
    # Finding — including one with no standing facts projected at all and a VIN the
    # caller has no relation to — with NO group membership required. `custom:role`
    # is the specific attribute proven self-writable on the CMS pool 2026-08-03,
    # cited in `_CUSTOMER_ID_CLAIM_TRUSTED_ENV`'s comment as the reason a `custom:*`
    # claim cannot be trusted. It nullified every standing route below it.
    #
    # Deleted rather than gated behind a trust env var, because it is vestigial:
    # measured 2026-09-23 across BOTH pools the authorizer accepts, zero users
    # carry `custom:role = "service-advisor"` —
    #   `<cms-staging-user-pool-id>`  16 users, 0 carry `custom:role` at all;
    #                                 the `service-advisor` group has 1 member,
    #                                 provisioned by group membership.
    #   `<cvx-staging-fleet-pool-id>`  7 users, 1 carries `custom:role` (`demo`);
    #                                 `service.advisor@example.com` carries none.
    # So the group arm below covers every live service advisor and the claim arm
    # had no dependents. A trust flag would have preserved a path nothing uses.
    # See `issues/2026-09-23-avx-custom-role-claim-unconditional-approve/`.
    if groups & {"service-advisor"}:
        return True

    # Derive scope fields from the finding
    scope = finding.get("scope", {})
    vin = scope.get("vin")
    scope_owner_id = scope.get("owner_id")
    fleet_id = scope.get("fleet_id")

    # F11.2 projection, broadened per docs/identity-and-standing-contract.md § 6:
    # one top-level fact per standing route, all from the same CMS vehicle row.
    # Read here, NOT from scope (schema-forbidden there on the VIN branch).
    projected_owner_id = finding.get("owner_id")
    projected_fleet_id = finding.get("fleet_id")
    projected_vehicle_id = finding.get("vehicle_id")

    # Fleet-operator on a FLEET-scoped Finding. This is the pre-existing path for
    # Findings whose scope IS a fleet. VIN-scoped Findings are handled below, where
    # fleet standing is derived from the vehicle's projected fleet_id.
    #
    # Review cycle 2 (W1): this arm reads the same self-writable `custom:fleetIds`
    # as the VIN-branch arm below, and Fix Group 26 gated that one while leaving this
    # one open — so a forged fleet claim was denied 63 lines down and APPROVED here,
    # in all five env states including the deployed default. The W2 argument written
    # into this same function ("the handler is a separate trust boundary") applies
    # identically; it just was not applied here.
    #
    # ⚠ SCOPE OF THIS FLAG — corrected by review cycle 3 (C1). An earlier version of
    # this comment said "the same attribute", which reads as full coverage and is not
    # true. `AVX_FLEET_IDS_CLAIM_TRUSTED` governs exactly TWO of the NINE reads of
    # `custom:fleetIds` across this file and `_lib/avx_scope.py`: this arm and the
    # VIN-branch arm below. Both are APPROVE paths. Every READ path is still ungated,
    # and three of them return 200 with another org's data on a forged claim in the
    # deployed default env (`/findings/fleet`, `/findings/dealership`,
    # `/findings/district`).
    #
    # So this is deliberately a partial mitigation: the write side of standing is
    # gated, the read side is not. Gating the read side needs `_claim_is_trusted`
    # moved into `_lib` plus env wiring on BOTH consuming Lambdas' CDK stacks, which
    # is a larger change than a review cycle should make — and could not be verified
    # in the cycle that found it, because the stack's jest suite needs Docker.
    # Tracked with both probe tables at
    # `issues/2026-09-23-avx-membership-claims-ungated-on-read-paths/`.
    if "fleet-operator" in groups and fleet_id and _claim_is_trusted(_FLEET_IDS_CLAIM_TRUSTED_ENV):
        if fleet_id in _caller_fleet_ids(claims):
            return True

    # Owner-scoped Finding: the scope IS the owner, and that value is written
    # from `sub` by the executors, so `sub` is the right comparand here.
    # Backward compatibility (F11.3 Accept 2) — not dead code.
    if scope_owner_id and caller_sub and caller_sub == scope_owner_id:
        return True

    # ── VIN-scoped Finding: intersect the projected standing facts with claims ──
    #
    # Contract § 3: standing is a SET. A principal may hold any of these, and
    # holding one does NOT imply another. Each route is checked independently and
    # each requires BOTH sides present — an absent projection or an absent claim
    # means "no standing provable by this route", which must not match.
    #
    # Contract § 3.1: `fleet_driver` MUST NOT confer `owner`. The chain
    # sub -> custom:vehicleId -> vehicle -> sold_to reaches a party id, so
    # deriving owner standing from the driven vehicle is *possible* — and is
    # forbidden, because it would hand a fleet employee the commercial rights of
    # the company that owns the vehicle. That is why the three checks below read
    # three different claims and never fall back to each other.
    if vin and groups & _VEHICLE_STANDING_GROUPS:
        # owner standing — the caller IS the party of record
        caller_identity = _caller_customer_id(claims)
        if projected_owner_id and caller_identity and caller_identity == projected_owner_id:
            return True

        # fleet_driver standing — the caller is assigned to operate THIS vehicle.
        # Compared against the projected vehicle_id (VEH-*), NOT the VIN:
        # custom:vehicleId carries VEH-MRDN-0015 while the scope carries
        # MRDN0000000000015, so matching against the VIN could never succeed.
        #
        # Gated: custom:vehicleId is self-writable under default WriteAttributes,
        # so an ungated comparison lets any caller forge assignment to any vehicle.
        caller_vehicle_id = (
            claims.get("custom:vehicleId")
            if _claim_is_trusted(_VEHICLE_ID_CLAIM_TRUSTED_ENV)
            else None
        )
        if (
            # These two emptiness checks are JOINTLY load-bearing, and neither is
            # removable-safe — review cycle 1 (W2) proved it. Each is redundant only
            # BECAUSE the other exists; drop both and a blank claim ('   ') matches a
            # blank projection (`vehicle_id: ""`), granting approve. All 126 handler
            # tests stayed green against that mutant, because every fixture paired a
            # blank claim with a VALID projection. `M-FG25-9` and
            # `test_blank_claim_never_matches_a_blank_projection` now cover it.
            #
            # `vehicle_id: ""` is not writer-reachable (`_standing_projection` skips
            # empty values), but the handler is a separate trust boundary from the
            # writer: it reads whatever is in the table, which a backfill script, a
            # console edit, or a future writer could put there.
            projected_vehicle_id
            and isinstance(caller_vehicle_id, str)
            and caller_vehicle_id.strip()
            and caller_vehicle_id.strip() == projected_vehicle_id
        ):
            return True

        # fleet_manager standing — the caller manages the fleet this vehicle is in.
        # Gated for the same reason as the driver route above, AND on the
        # `fleet-operator` group (spec 2026-09-25-avx-own-vehicle-findings, security
        # review Cycle 1 W1): every read route needs that group for fleet-wide Findings,
        # so without it here a driver or owner carrying custom:fleetIds could approve a
        # Finding on another fleet vehicle that no read route returns to them.
        if ("fleet-operator" in groups and projected_fleet_id
                and _claim_is_trusted(_FLEET_IDS_CLAIM_TRUSTED_ENV)):
            if projected_fleet_id in _caller_fleet_ids(claims):
                return True

    # Fail closed: no owner binding matched.
    return False


# ---------------------------------------------------------------------------
# Route dispatch
# ---------------------------------------------------------------------------

def handler(event: dict, context: Any) -> dict[str, Any]:
    claims = _get_claims(event)
    if claims is None:
        return _respond(401, {"error": "missing or invalid JWT claims"})

    resource = event.get("resource", "")
    method = event.get("httpMethod", "GET")
    path_params = event.get("pathParameters") or {}
    qs = event.get("queryStringParameters") or {}

    # ── GET /findings/me ─────────────────────────────────────────────────────
    # Exact match, before the substring dispatch below and before page_size is
    # parsed: it takes no identifiers and no paging parameters.
    if method == "GET" and resource == "/findings/me":
        try:
            return _respond(200, _findings_me(claims))
        except Exception as exc:  # noqa: BLE001 — never a 502; never a partial answer
            log.error("findings_me failed: %s", exc)
            return _respond(500, {"error": "internal error"})

    limit = min(int(qs.get("page_size", "50")), 200)
    next_token = qs.get("next_token")

    # ── POST /findings/{finding_id}/approve ──────────────────────────────────
    if method == "POST" and "approve" in resource:
        return _approve_finding(event, claims)

    # ── GET /findings/vehicle/{vin} ───────────────────────────────────────────
    if method == "GET" and "vehicle" in resource:
        vin = path_params.get("vin")
        if not vin:
            return _respond(400, {"error": "vin required"})

        from _lib.avx_scope import _extract_groups as _eg
        caller_groups = _eg(claims)
        caller_sub = claims.get("sub", "")

        # C3 fix: vehicle-owner/consumer callers bypass the scope pre-check
        # (which now returns False for them without a finding context) and
        # instead use a VIN+owner_id filtered GSI query — they only see their
        # own findings. Service roles and admin pass the pre-check and use the
        # unfiltered VIN query.
        is_owner_caller = bool(
            caller_groups & {"vehicle-owner", "consumer"}
            and not (caller_groups & {"platform-admin", "service-advisor", "technician",
                                      "fleet-driver", "fleet-operator"})
        )

        # W-C3-fleet fix (2026-09-19 fix cycle 2): fleet callers are routed to a
        # fleet-filtered query so they only see findings for VINs in their fleets.
        is_fleet_caller, caller_fleet_ids = is_fleet_vehicle_caller(claims)

        if is_owner_caller:
            # Spec 2026-09-25-avx-own-vehicle-findings T2.2: the identity seam is live
            # (custom:customerId and custom:vehicleId are provisioned and attested), so
            # an owner caller with a TRUSTED identity gets a read of its OWN partition
            # (by_owner / by_vehicle), filtered on the requested vin. Querying the VIN's
            # partition instead would leak (review Cycle 1 W1): DynamoDB applies Limit
            # before the filter, so with page_size=1 a non-null next_token revealed
            # that the VIN had Findings, and the token decoded to another party's
            # finding_id. Every key and cursor here stays inside the caller's own rows,
            # which keeps the T12.1 anti-oracle property. Without a trusted identity the
            # original 503 below still applies.
            owner_customer_id = _caller_customer_id(claims)
            owner_vehicle_id = None if owner_customer_id else _caller_vehicle_id(claims)
            if owner_customer_id or owner_vehicle_id:
                try:
                    if owner_customer_id:
                        items, out_token = _list_owner_findings_for_vin(
                            owner_customer_id, vin, limit, next_token)
                    else:
                        items, out_token = _list_vehicle_findings_for_vin(
                            owner_vehicle_id, vin, limit, next_token)
                except Exception as exc:
                    log.error("list findings by vin=%s (owner path) failed: %s", vin, exc)
                    return _respond(500, {"error": "internal error"})
                # Defence in depth, as on /findings/me: a row is returned only when its
                # projected facts agree with the query (the requested vin and the
                # caller's own identity), and expired Findings are hidden (spec: readers
                # filter on expires_at; diagnostics Findings expire 30 minutes after
                # their last write). A short page with a cursor is normal; clients
                # follow next_token.
                now_iso = _now_iso()
                scope_key, scope_value = (
                    ("owner_id", owner_customer_id) if owner_customer_id
                    else ("vehicle_id", owner_vehicle_id))
                kept = [item for item in items
                        if item.get("vin") == vin and item.get(scope_key) == scope_value
                        and _not_expired(item, now_iso)]
                return _respond(200, {
                    "findings": [_strip_finding_for_response(item) for item in kept],
                    "next_token": out_token,
                    "computed_at": now_iso,
                })

            # T12.1 / spec 2026-09-25-avx-own-vehicle-findings: 503 when the caller has
            # NO trusted identity — neither custom:customerId nor custom:vehicleId passes
            # its AVX_*_CLAIM_TRUSTED gate (flag off on this stage, or claim absent).
            # Unconditional for that caller, with no per-VIN probe, so the response
            # carries no signal about whether this VIN has Findings.
            #
            # Why 503 and not 403 (review Cycle 1 S4): `owner_scope_unavailable` is an
            # existing client-facing code, and the common cause is stage configuration
            # (trust flag off), not the caller. Changing it is a client contract change,
            # recorded in decisions.md as a follow-on. The history of this branch
            # (projection landing, identity seam) is in decisions.md § "2026-09-20 —
            # T12.1 cross-tenant oracle fix" and § "2026-09-23 — Fix Group 15 build".
            log.warning(
                "owner_scope_unavailable vin=%s: caller has no trusted customer or "
                "vehicle identity",
                vin,
            )
            return _respond(503, {
                "error": "owner_scope_unavailable",
                "detail": (
                    "findings for this VIN cannot be scoped to the caller: no trusted "
                    "customer or vehicle identity on this request"
                ),
            })
        elif is_fleet_caller:
            # Fleet-scoped VIN query: fleet-driver/fleet-operator callers see only
            # findings whose fleet_id (top-level) is in their custom:fleetIds.
            # T14.4: the filter uses top-level fleet_id, NOT scope.fleet_id — the
            # projection writes fleet_id as a top-level attribute (same key the by_fleet
            # GSI reads). Scope.fleet_id was the pre-projection nested path.
            # W-C3-fleet: if fleetIds is empty the pre-check in check_vehicle_scope also
            # denies. The 403 below is NOT redundant with it, and Fix Group 29 made that
            # load-bearing — read this before touching it.
            #
            # An earlier version of this comment said "is_fleet_vehicle_caller returns
            # False for those", which was true pre-FG29 and is now WRONG. Review cycle 4
            # (W3) flagged that its natural repair is deleting the guard below, which
            # would be a fail-open: since FG29, an UNTRUSTED `custom:fleetIds` yields
            # `(True, [])` — fleet-scoped with zero permitted fleets — deliberately,
            # because `(False, [])` is the PRIVILEGED answer and would send this route
            # down the unfiltered path. This 403 is what converts that into a deny.
            #
            # So `caller_fleet_ids` is empty in two distinct situations now: a caller
            # with genuinely no fleet membership, and any fleet caller while the trust
            # flag is off (the deployed default). Both must deny here.
            if not caller_fleet_ids:
                return _respond(403, {"error": "insufficient scope: no fleet membership"})
            #
            # T12.1: unconditional 503 — no per-VIN probe.
            #
            # The writer now projects top-level fleet_id on VIN-scoped Findings
            # (agents/tier2/writer.py _STANDING_ATTRS), so the original blocker is gone.
            # The 503 stays until this path is rebuilt, because the helper this note used
            # to name is the wrong shape.
            #
            # Re-enable (review Cycle 2 S11): do NOT call _list_findings_by_vin_for_fleet.
            # It keys on the VIN's partition (by_vin) and filters on fleet_id, and DynamoDB
            # applies Limit before the filter, so a caller-chosen page_size turns
            # next_token into a per-VIN existence oracle (review Cycle 1 W1 of spec
            # 2026-09-25-avx-own-vehicle-findings). Use the caller's own partition, as
            # the owner path above does: by_fleet per TRUSTED fleet (_trusted_fleet_ids),
            # filtered on vin, re-checked in Python, expired rows hidden. And gate it on
            # the `fleet-operator` group (review Cycle 3 S17): `fleet-driver` callers
            # also reach this branch, and a driver sees only the assigned vehicle
            # (spec D2; /findings/me W3). Add a live positive and negative test.
            log.warning(
                "fleet_scope_unavailable vin=%s: fleet-caller VIN path not yet rebuilt on "
                "the caller's by_fleet partition",
                vin,
            )
            return _respond(503, {
                "error": "fleet_scope_unavailable",
                "detail": (
                    "findings for this VIN cannot yet be scoped to a fleet on this route; "
                    "fleet operators read GET /findings/me or /findings/fleet/{fleet_id}"
                ),
            })
        else:
            # Service/platform-admin path: full pre-check, then unfiltered VIN query.
            if not check_vehicle_scope(claims, vin):
                return _respond(403, {"error": "insufficient scope"})
            try:
                # W5: use GSI-backed VIN query (unfiltered — service/admin sees all)
                items, out_token = _list_findings_by_vin(vin, limit, next_token)
            except Exception as exc:
                log.error("list findings by vin=%s (service path) failed: %s", vin, exc)
                return _respond(500, {"error": "internal error"})
            return _respond(200, {
                "findings": [_strip_finding_for_response(item) for item in items],
                "next_token": out_token,
                "computed_at": _now_iso(),
            })

    # ── GET /findings/owner/{owner_id} ───────────────────────────────────────
    if method == "GET" and "owner-fleet" not in resource and "owner" in resource:
        owner_id = path_params.get("owner_id")
        if not owner_id:
            return _respond(400, {"error": "owner_id required"})
        if not (check_owner_scope(claims, owner_id) or _is_party_of_record(claims, owner_id)):
            return _respond(403, {"error": "insufficient scope"})
        try:
            # W5: use GSI query instead of scan
            items, out_token = _list_findings_by_owner(owner_id, limit, next_token)
        except Exception as exc:
            log.error("list findings by owner_id=%s failed: %s", owner_id, exc)
            return _respond(500, {"error": "internal error"})
        return _respond(200, {
            "findings": [_strip_finding_for_response(item) for item in items],
            "next_token": out_token,
            "computed_at": _now_iso(),
        })

    # ── GET /findings/owner-fleet/{owner_id} ─────────────────────────────────
    if method == "GET" and "owner-fleet" in resource:
        owner_id = path_params.get("owner_id")
        if not owner_id:
            return _respond(400, {"error": "owner_id required"})
        # W1 fix: pass owner_fleet_id=None here — route has no fleet context,
        # so fleet-operator without owner's fleet is denied (fails closed).
        # Owner self-read (caller_sub == owner_id) still passes, and so does the
        # party of record for projected (VIN-scoped) Findings — Fix Group 36.
        if not (check_owner_fleet_scope(claims, owner_id, owner_fleet_id=None)
                or _is_party_of_record(claims, owner_id)):
            return _respond(403, {"error": "insufficient scope"})
        try:
            # W5: use GSI query instead of scan
            items, out_token = _list_findings_by_owner(owner_id, limit, next_token)
        except Exception as exc:
            log.error("list findings by owner-fleet owner_id=%s failed: %s", owner_id, exc)
            return _respond(500, {"error": "internal error"})
        return _respond(200, {
            "findings": [_strip_finding_for_response(item) for item in items],
            "next_token": out_token,
            "computed_at": _now_iso(),
        })

    # ── GET /findings/fleet/{fleet_id} ───────────────────────────────────────
    if method == "GET" and "fleet" in resource:
        fleet_id = path_params.get("fleet_id")
        if not fleet_id:
            return _respond(400, {"error": "fleet_id required"})
        if not check_fleet_scope(claims, fleet_id):
            return _respond(403, {"error": "insufficient scope"})
        # T10.3: wired to by_fleet GSI — T10.2 denormalises fleet_id to top-level
        # attribute, making this GSI effective. Scope auth is unchanged from Group 3+4.
        try:
            items, out_token = _list_findings_by_fleet(fleet_id, limit, next_token)
        except Exception as exc:
            log.error("list findings by fleet_id=%s failed: %s", fleet_id, exc)
            return _respond(500, {"error": "internal error"})
        return _respond(200, {
            "findings": [_strip_finding_for_response(item) for item in items],
            "next_token": out_token,
            "computed_at": _now_iso(),
        })

    # ── GET /findings/dealership/{dealer_id} ─────────────────────────────────
    if method == "GET" and "dealership" in resource:
        dealer_id = path_params.get("dealer_id")
        if not dealer_id:
            return _respond(400, {"error": "dealer_id required"})
        if not check_dealer_scope(claims, dealer_id):
            return _respond(403, {"error": "insufficient scope"})
        # T10.3: wired to by_dealership GSI — T10.2 denormalises dealership_id to
        # top-level attribute. Scope auth is unchanged from Group 3+4.
        try:
            items, out_token = _list_findings_by_dealership(dealer_id, limit, next_token)
        except Exception as exc:
            log.error("list findings by dealership_id=%s failed: %s", dealer_id, exc)
            return _respond(500, {"error": "internal error"})
        return _respond(200, {
            "findings": [_strip_finding_for_response(item) for item in items],
            "next_token": out_token,
            "computed_at": _now_iso(),
        })

    # ── GET /findings/district/{district_id} ─────────────────────────────────
    if method == "GET" and "district" in resource:
        district_id = path_params.get("district_id")
        if not district_id:
            return _respond(400, {"error": "district_id required"})
        if not check_district_scope(claims, district_id):
            return _respond(403, {"error": "insufficient scope"})
        # T10.3: wired to by_district GSI — T10.2 denormalises district_id to
        # top-level attribute. Scope auth is unchanged from Group 3+4.
        try:
            items, out_token = _list_findings_by_district(district_id, limit, next_token)
        except Exception as exc:
            log.error("list findings by district_id=%s failed: %s", district_id, exc)
            return _respond(500, {"error": "internal error"})
        return _respond(200, {
            "findings": [_strip_finding_for_response(item) for item in items],
            "next_token": out_token,
            "computed_at": _now_iso(),
        })

    return _respond(404, {"error": "route not found"})
