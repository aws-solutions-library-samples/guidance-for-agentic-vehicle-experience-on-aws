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
import math
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
_REPO_ROOT = os.path.dirname(_LAMBDAS_DIR)
for _p in (_LAMBDAS_DIR, _REPO_ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from agents.executors.policy import ACTION_KINDS as _POLICY_ACTION_KINDS_TABLE  # noqa: E402
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
    # Added by FI rebalancing stage E Group 6 G3 Fix Group 2 (security review W1).
    # The rebalance agent writes ``metro_inputs_hash`` on move Findings as a
    # top-level DDB attribute (spec D8 step 5 skip-metro gate, issue
    # ``2026-10-18-fi-rebalance-rerun-calls-model``). It is NOT in the frozen
    # Finding schema's 21 permitted root keys, so the response boundary MUST
    # strip it — same mechanism as the Fix Group 30 ``vehicle_id`` leak above.
    #
    # Precedent: writes land via agent-side paths rather than the shared
    # writer's ``_STANDING_ATTRS``, so the existing coverage test
    # ``test_strip_list_covers_every_writer_projected_attr`` is structurally
    # blind to this attribute. The coverage test was extended (FI stage E G6 G3
    # Fix Group 2 security review S3) to also parse the rebalance agent's own
    # ``_AGENT_PROJECTED_ATTRS`` registry to prevent the next agent-side
    # top-level DDB attribute from repeating this leak.
    "metro_inputs_hash",
    # Added by A1 retention spec (2026-10-27). writer._put_item,
    # writer._update_item, and writer._refresh_expiry set ``ttl`` (DynamoDB
    # TTL epoch-seconds) on every eligible row (no recommended_action, agent_id
    # not in the excluded set).  ``ttl`` is storage-only and must never appear
    # in an API response — the frozen contract's root ``additionalProperties:
    # false`` rejects it.  Registered in writer.py's ``_STORAGE_ONLY_ATTRS``
    # so the guard test ``test_strip_list_covers_every_writer_projected_attr``
    # catches any strip-list gap.
    "ttl",
    # FA2 retention (2026-10-27): sticky Boolean marker.  SET by any write where
    # the stored row or the incoming artifact carries recommended_action; never
    # REMOVEd.  Prevents a row that was once an FI item from acquiring ttl on a
    # subsequent informational re-emit.  Storage-only; must not appear in any
    # API response (root additionalProperties: false rejects it).
    # Registered in writer.py's _STORAGE_ONLY_ATTRS alongside ttl.
    "ttl_exempt",
})

# F11.3 (Fix Group 15) — the claim carrying the caller's CMS customer identity
# (`CUST-XXXXXXXX`, the registry's `sold_to`).
#
# SUPERSEDED 2026-10-07 for VIN Findings (issue
# 2026-10-07-avx-finding-owner-is-purchaser-not-driver): the projected top-level
# `owner_id` is now the assigned driver's Cognito `sub`, so this claim matches no
# VIN Finding. The owner arms that compare it stay as fail-closed code; the
# purchaser holds no owner standing on a VIN Finding.
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
# `meridian.driver@example.com` (`custom:vehicleId=MRDN0000000000015`) and
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
    # v2.5 FI kinds — all operational (fleet_driver may approve; none commercial).
    # Operator-required kinds (D5) still gate on operator standing at approve time;
    # classifying them here says only that they are not commercial-only.
    "book_recall_service",
    "ground_vehicle",
    "move_vehicles",
    "exempt_recall",
    "complete_recall",
    "submit_warranty_claim",
    "apply_cost_action",
    # Retail kinds
    "lead_capture",
    "configurator_recommend",
    # Stage D kinds (spec 2026-10-05-fi-fleet-health-stage-d, task 3.5).
    # book_fleet_service is fleet-level service booking (operational).
    # The three engineering kinds are population-scoped (no fleet queue);
    # fleet_driver approval is vacuously impossible for them — classifying
    # OPERATIONAL is safe and correct until a COMMERCIAL split is implemented.
    "book_fleet_service",
    "record_engineering_decision",
    "issue_recall",
    "issue_software_campaign",
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
    """Query findings by the projected ``vehicle_id`` (the VIN since the 2026-11 CMS migration) via GSI.

    Two-slot invariant: index_name="by_vehicle", key_name="vehicle_id"
    CDK declaration: vsa-tier2-stack.ts indexName='by_vehicle', partitionKey name='vehicle_id'
    (spec 2026-09-25-avx-own-vehicle-findings T2.1; the writer projects vehicle_id since
    Fix Group 25).
    """
    return _list_findings_by_index("by_vehicle", "vehicle_id", vehicle_id, limit, next_token)


# ---------------------------------------------------------------------------
# Approve: create Action from Finding
# ---------------------------------------------------------------------------

_VALID_TARGET_SYSTEMS = (
    # W2 (FG2d review): only include target systems from EXECUTABLE kinds.
    # Non-executable FI kinds have target_system="cms", but no executable kind
    # uses cms today.  Admitting "cms" would let pre-existing kinds
    # (e.g. acknowledge_only) be approved with target_system="cms", writing an
    # Action that CmsExecutor records as declined and whose ios-owner exclusion
    # check never fires — landing an FI-flavoured Action in the caller's
    # by_created_by partition.  Only targets that have a working executor belong
    # on this route.  Contract § 4 table.
    {row.target_system for row in _POLICY_ACTION_KINDS_TABLE.values() if row.executable}
    | {"chat"}
)
# ios-owner is REJECTED on the approve route — it is the reverse-direction
# executor; its writer is Spec 2's dealer-side "propose to customer" surface.
# Rejecting it here closes the security W1 finding from contract v2.2 (Fix Group 2).
_FORBIDDEN_TARGET_SYSTEMS = {"ios-owner"}

# All v2.5 action kinds, derived from policy.py (spec D4).  Non-executable kinds
# are gated below.  The retail _RETAIL_KINDS runtime gate also applies on top.
_ACTION_KINDS = frozenset(_POLICY_ACTION_KINDS_TABLE.keys())

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

# The three population-scoped engineering action kinds (spec D11, contract v2.7 § 4).
# Used by the kind/scope coupling gate: these kinds are ONLY valid on population-scoped
# Findings; a population-scoped Finding ONLY accepts these three kinds.
_ENGINEERING_KINDS = frozenset({
    "record_engineering_decision",
    "issue_recall",
    "issue_software_campaign",
})


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
#: `last_error_class`, so persisting it unprojected writes a schema-invalid row on
#: every failure (D-G10-6). Pinned to the schema file by `TestDispatchAuditEntrySchemaPin`.
#: v2.5 adds `guardrail`, `failure_reason` and `error_status` (spec D5, D14);
#: `target_reason` is the target's own refusal sentence (contract § 10b).
_AUDIT_ENTRY_KEYS = (
    "at", "by", "from_status", "to_status",
    "note", "failure_kind", "retry_count",
    "guardrail", "failure_reason", "error_status",
    "target_reason",
)

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
    """Project `result.audit_entry()` onto the schema's keys (D-G10-6, D14).

    `error_status`, `failure_reason` and `guardrail` are now in `_AUDIT_ENTRY_KEYS`
    and arrive directly from `audit_entry()` — they are never folded into `note`.
    `last_error_class` is still folded into `note` so an operator can see the error
    class alongside the reason sentence.  `last_error_status` is now carried as the
    typed `error_status` field; it must NOT appear in `note` (spec D14, tests).
    """
    raw = result.audit_entry(by=f"executor-{target_system}", at=at)
    entry = {k: raw[k] for k in _AUDIT_ENTRY_KEYS if k in raw}
    # Fold ONLY last_error_class into note.  error_status is a dedicated field now;
    # do NOT re-introduce last_error_status here.
    if raw.get("last_error_class") is not None:
        entry["note"] = "; ".join(
            filter(None, [entry.get("note"), f"last_error_class={raw['last_error_class']}"])
        )
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


# Server-set rule extracted to agents/executors/server_set.py (task 11.3) so the
# simulated fleet operator can use the same rule without calling this Lambda.
# Tests that call h._check_server_set_params continue to work because the name
# is re-exported here as a module attribute.
from agents.executors.server_set import check_server_set_params as _check_server_set_params  # noqa: E402,F811



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
    # Accept 2 (spec D4): non-executable FI kinds return 400 before any write.
    if not _POLICY_ACTION_KINDS_TABLE[action_kind].executable:
        return _respond(400, {
            "error": f"action_kind '{action_kind}' is not executable in this deployment"
        })
    # Group 2 review (core W3): a v2.5 FI kind goes only to the target its policy row
    # names. Without this, book_recall_service with target `none` was recorded
    # `completed` with nothing sent to DMS. Spec 1a kinds keep today's targets (their
    # rows are not operator_required); tightening them is a separate decision.
    _row = _POLICY_ACTION_KINDS_TABLE[action_kind]
    if _row.operator_required and target_system != _row.target_system:
        return _respond(400, {
            "error": f"action_kind '{action_kind}' must target '{_row.target_system}'"
        })

    # Critical (FG3 review C1 / FG2d W2 regression): Stage B made apply_cost_action
    # executable with target_system="cms", which put "cms" into _VALID_TARGET_SYSTEMS
    # for ALL executable kinds. Without this gate, a non-cost kind (e.g.
    # acknowledge_only) could be approved to CmsExecutor, writing an Action that CMS
    # records as declined — landing it in the caller's by_created_by partition with a
    # target iOS cannot decode (the original W2 defect). This check re-narrows "cms"
    # to only the kinds whose policy row explicitly names it.
    # C2 (G3F1.5 review C2): Stage D added "adp" rows for the three engineering kinds;
    # that put "adp" into _VALID_TARGET_SYSTEMS for every kind. Without this gate, any
    # kind could be approved with target "adp". The generalised check refuses any
    # target that only FI policy rows name (i.e. its row's own target_system) unless
    # the kind's own row names it.
    # (test_fg2d_review.py::TestTargetSystemCmsRefused and test_g3f15_fixes.py::TestAdpTargetRefused)
    if target_system in ("cms", "adp") and _row.target_system != target_system:
        return _respond(400, {
            "error": f"unknown target_system: {target_system}"
        })

    # Load the Finding
    finding = _load_finding_by_id(finding_id)
    if not finding:
        return _respond(404, {"error": f"Finding {finding_id} not found"})

    # --- Population-scoped Findings (Stage D engineering kinds) ---
    # Contract § 2.7, § 3.7: only product-engineer may approve quality.investigation
    # and quality.issue Findings. scope.population_id is set; no scope.vin.
    # ALL 403s are decided BEFORE reading the decision state (contract § 4 "Order of
    # checks").
    _pop_scope = finding.get("scope", {})
    _is_pop_finding = set(_pop_scope) == {"population_id"}  # C1: exact single-key check

    if _is_pop_finding:
        if "product-engineer" not in _extract_groups(claims):
            return _respond(403, {"error": "insufficient scope to approve this finding"})
        standing = "engineer"
    else:
        # Scope check — report which standing matched (operator/user/None).
        # ALL 403s must be decided BEFORE reading decision state (security review
        # Cycle 1 W1; contract § 4 "Order of checks").
        standing = _check_approve_scope(claims, finding)
        if not standing:
            return _respond(403, {"error": "insufficient scope to approve this finding"})

    # W3 (FG2d review): operator_required kinds need operator standing regardless
    # of whether the Finding is an FI queue item.  In Stage A every
    # operator_required kind is an FI kind, so a Finding without recommended_action
    # never carries one in practice — but the policy check is kind-driven, not
    # rec-driven, and a caller that sends book_recall_service on a plain Finding
    # must also hold operator standing.
    if _POLICY_ACTION_KINDS_TABLE[action_kind].operator_required and standing != "operator":
        return _respond(403, {"error": "insufficient scope to approve this finding"})

    # Kind/scope coupling (spec D11 / contract v2.7): before any write.
    # Engineering kinds (record_engineering_decision, issue_recall, issue_software_campaign)
    # are ONLY valid on population-scoped Findings; a population-scoped Finding ONLY
    # accepts these three kinds.  Return 400 before any read of the decision state.
    if _is_pop_finding and action_kind not in _ENGINEERING_KINDS:
        return _respond(400, {
            "error": (
                f"action_kind '{action_kind}' is not valid for a population-scoped Finding; "
                f"population Findings only accept engineering kinds "
                f"({sorted(_ENGINEERING_KINDS)})"
            )
        })
    if action_kind in _ENGINEERING_KINDS and not _is_pop_finding:
        return _respond(400, {
            "error": (
                f"action_kind '{action_kind}' requires a population-scoped Finding "
                f"(scope must have population_id and no vin)"
            )
        })

    # FI queue item (Finding with recommended_action): operator-only, with extra rules.
    rec = finding.get("recommended_action")

    # Warning (FG3 review W2): apply_cost_action is only valid when the Finding
    # carries a matching cost recommendation. Without this, an operator could
    # approve apply_cost_action on a health/recall Finding with a body-supplied
    # vehicle_vin — recording a cost decision outside "one decision per Finding"
    # against a non-cost finding_id with any VIN the operator chose.
    # vehicle_vin is already server-set from scope below (the FI-item VIN path),
    # but that path only runs when rec is not None. This gate fires first.
    if action_kind == "apply_cost_action" and (
        rec is None or rec.get("action_kind") != "apply_cost_action"
    ):
        return _respond(400, {
            "error": "apply_cost_action requires a Finding with a matching "
                     "recommended_action (action_kind: apply_cost_action)"
        })

    # Stage C (G3-18, review cycle 1 routes C1): a kind with a per-kind server-set list
    # (ground_vehicle, book_recall_service, submit_warranty_claim) is approvable ONLY on a
    # Finding whose recommended_action has the same action_kind. Without one the body
    # would supply the server-set values (ground a different VIN, book a different recall,
    # claim a different repair). No body-only path; refused before any write.
    if _POLICY_ACTION_KINDS_TABLE[action_kind].server_set and (
        rec is None or rec.get("action_kind") != action_kind
    ):
        return _respond(400, {
            "error": f"{action_kind} requires a Finding with a matching "
                     f"recommended_action (action_kind: {action_kind})"
        })

    if rec is not None:
        # FI items: engineer on population-scoped items; operator on fleet-scoped items.
        # contract § 4 (fleet-scoped), contract § 2.7 (population-scoped).
        if _is_pop_finding:
            if standing != "engineer":
                return _respond(403, {"error": "insufficient scope to approve this finding"})
        elif standing != "operator":
            return _respond(403, {"error": "insufficient scope to approve this finding"})
        # action_kind and target_system must match the recommendation (contract § 4).
        if action_kind != rec.get("action_kind") or target_system != rec.get("target_system"):
            return _respond(400, {
                "error": "action_kind and target_system must match the Finding's recommended_action"
            })

    # Target-specific parameters, validated AFTER the scope check so the 400s below
    # are not an oracle on a Finding the caller cannot see, and BEFORE any write so
    # an Action that cannot be dispatched is never recorded.
    if not isinstance(parameters, dict):
        return _respond(400, {"error": "parameters must be an object"})
    _fi_decision: "str | None" = None  # set by _check_server_set_params for Stage C kinds
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
        # Stage C (spec D11): FI items with a per-kind server-set list use the
        # generic helper in place of the Stage A vehicle-only rule.
        if rec is not None and _POLICY_ACTION_KINDS_TABLE[action_kind].server_set:
            parameters, param_error, _fi_decision = _check_server_set_params(
                finding, action_kind, parameters)
            if param_error:
                return _respond(400, {"error": param_error})
        else:
            parameters, param_error = _dms_parameters(finding, parameters)
            if param_error:
                return _respond(400, {"error": param_error})

    # FI item: VIN-scoped with non-DMS target — set vehicle_vin from the Finding.
    # For DMS targets the block above already handles this.
    # Stage C: kinds with a per-kind server-set list (e.g. ground_vehicle) use the
    # generic helper; others fall through to the Stage A vehicle-only path.
    if rec is not None and target_system != "dms":
        if _POLICY_ACTION_KINDS_TABLE[action_kind].server_set:
            parameters, param_error, _fi_decision = _check_server_set_params(
                finding, action_kind, parameters)
            if param_error:
                return _respond(400, {"error": param_error})
            if action_kind == "move_vehicles":
                # The Action row is put with the boto3 resource API, which raises TypeError
                # on a Python float; a unit test with a mocked table never sees that.
                parameters = {k: (Decimal(str(v)) if isinstance(v, float) else v)
                              for k, v in parameters.items()}
        else:
            vin = (finding.get("scope") or {}).get("vin")
            if vin:
                supplied = parameters.get("vehicle_vin")
                if supplied is not None and supplied != vin:
                    return _respond(400, {"error": "parameters.vehicle_vin must match the Finding's vehicle"})
                parameters = {**parameters, "vehicle_vin": vin}

    # Cost action (spec D9; decisions.md 2026-10-05 "Group 3 review cycle 2"): `month` is
    # server-set from the recommendation (the Finding's data month; the body must omit it or
    # match), and the operator-settable values are checked with the executor's own validator
    # BEFORE the write, so an override missing `cause` can't record a decision that then fails
    # for good (contract § 4: a failed decision is final).
    if rec is not None and action_kind == "apply_cost_action":
        rec_month = (rec.get("parameters") or {}).get("month")
        supplied_month = parameters.get("month")
        if supplied_month is not None and supplied_month != rec_month:
            return _respond(400, {"error": "parameters.month must match the Finding's data month"})
        parameters = {**parameters, "month": rec_month}
        from agents.executors.cms import cost_action_parameter_error  # noqa: PLC0415 (lazy, as registry)
        param_error = cost_action_parameter_error(parameters)
        if param_error:
            return _respond(400, {"error": param_error})

    # FI item: location IDs in parameters must match the Finding's location (contract § 4).
    if rec is not None and not _fi_location_match(finding, parameters):
        return _respond(400, {"error": "parameters location IDs must match the Finding's location"})

    # Build decided_by from the token (D6: body-supplied approver is always ignored).
    caller_sub = claims.get("sub", "")
    decided_by = {"kind": standing, "id": caller_sub}

    # Derive action_id:
    # - FI item: per-Finding decision id (contract § 4, one decision per Finding).
    # - Non-FI: per-caller id (existing idempotency, unchanged).
    if rec is not None:
        action_id = _fi_decision_id(finding_id)
        # Stage C: decision computed by _check_server_set_params (per-kind server-set
        # rule); Stage A / Stage B fall back to a whole-body comparison.
        if _fi_decision is not None:
            decision = _fi_decision
        else:
            # Determine decision: overridden if the parameters AS SENT differ from the
            # recommendation (contract § 4: before AVX fills derived values such as
            # vehicle_vin, whatever the target).
            rec_params = rec.get("parameters", {})
            sent = body.get("parameters")
            body_params_canonical = json.dumps(
                sent if isinstance(sent, dict) else {}, sort_keys=True, cls=_DecimalEncoder)
            rec_params_canonical = json.dumps(rec_params, sort_keys=True, cls=_DecimalEncoder)
            decision = "approved" if body_params_canonical == rec_params_canonical else "overridden"
    else:
        created_by = f"owner-{caller_sub}"
        action_id = _derive_action_id(finding_id, action_kind, created_by)
        decision = None  # non-FI approves carry no decision

    now = _now_iso()
    created_by = f"owner-{caller_sub}"

    # Build the Action row
    action: dict[str, Any] = {
        "action_id": action_id,
        "finding_id": finding_id,
        "created_by": created_by,
        "created_at": now,
        "updated_at": now,
        "target_system": target_system,
        "status": "pending_execution",
        "action_kind": action_kind,
        "parameters": parameters,
        "evidence": finding.get("evidence", []),
        "audit_trail": [],
        "decided_by": decided_by,
    }
    if decision is not None:
        action["decision"] = decision
    # Storage-only fleet_id for the by_fleet GSI (spec D11, contract § 7).
    fi_fleet_id = finding.get("fleet_id")
    if fi_fleet_id:
        action["fleet_id"] = fi_fleet_id

    # Idempotent PutItem: if action_id already exists, fetch and return existing row
    table = _get_ddb().Table(_ACTIONS_TABLE)
    try:
        table.put_item(
            Item=action,
            ConditionExpression="attribute_not_exists(action_id)",
        )
    except table.meta.client.exceptions.ConditionalCheckFailedException:
        # Already exists — return the existing row
        existing = table.get_item(Key={"action_id": action_id},
                                  ConsistentRead=True).get("Item", action)
        log.info("approve idempotency hit for action_id=%s", action_id)
        if rec is not None:
            # FI item: one decision per Finding (contract § 4).
            # Same decision kind → 200 (idempotent); different → 409 already_decided.
            # W5 (FG2d review): contract § 4 says "Repeating the same decision
            # returns it (200). Any other decision gets 409 already_decided."
            # "Same decision" means the same decision word AND the same parameters.
            # The old check compared only the decision word, so a second override
            # with different parameters (priority: low after priority: urgent) got
            # 200 and silently returned the FIRST operator's Action to the second.
            if (existing.get("decision") == decision
                    and not _approve_request_differs(existing, target_system, parameters)):
                return _respond(200, _strip_action_for_response(existing))
            return _respond(409, {"error": "already_decided", "action_id": action_id})
        # Non-FI: W4 (review Cycle 1) — different target/params is a conflict.
        if _approve_request_differs(existing, target_system, parameters):
            return _respond(409, {
                "error": "an Action already exists for this Finding and action_kind "
                         "with a different target_system or parameters",
                "action_id": action_id,
            })
        return _respond(200, _strip_action_for_response(existing))
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

    # Defect-1 fix (task 3.7): for record_engineering_decision the executor
    # needs the loaded Finding to build the payload (it reads candidate_id,
    # failure_mode_id, etc. from the quality.root_cause evidence entry).
    # The persisted Action row (put_item above) intentionally does NOT carry
    # investigation_finding — it is not an Action schema field, and its full
    # Finding body would grow the row unboundedly.
    dispatch_action = action
    if action_kind == "record_engineering_decision":
        dispatch_action = {**action, "investigation_finding": finding}

    try:
        result = _dispatch_action(dispatch_action, dispatch_claims)
    except Exception as exc:  # noqa: BLE001
        log.error("dispatch raised for action_id=%s target_system=%s (%s); Action "
                  "left pending_execution", action_id, target_system, type(exc).__name__)
        return _respond(500, {"error": "action recorded but not dispatched",
                              "action_id": action_id})

    try:
        stored = _record_dispatch_result(action, result)
    except table.meta.client.exceptions.ConditionalCheckFailedException:
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
        return _respond(200, _strip_action_for_response(current))
    except Exception as exc:  # noqa: BLE001
        log.error("dispatch result write failed for action_id=%s target_system=%s "
                  "(%s): result=%s target_ref=%s", action_id, target_system,
                  type(exc).__name__, result.status, result.target_ref)
        return _respond(500, {"error": "action dispatched but its result was not recorded",
                              "action_id": action_id})

    log.info("approved finding_id=%s → action_id=%s target_system=%s status=%s",
             finding_id, action_id, target_system, stored.get("status"))
    return _respond(200, _strip_action_for_response(stored))


def _decline_finding(event: dict, claims: dict) -> dict[str, Any]:
    """POST /findings/{finding_id}/decline — contract § 5.

    Only for FI queue items (Findings with recommended_action).  Requires
    decided_by.kind == "operator".  One decision per Finding — idempotent on
    same decision, 409 already_decided on a different one.
    """
    path_params = event.get("pathParameters") or {}
    finding_id = path_params.get("finding_id") or path_params.get("findingId")
    if not finding_id:
        return _respond(400, {"error": "finding_id required"})

    try:
        body = json.loads(event.get("body") or "{}")
    except json.JSONDecodeError:
        return _respond(400, {"error": "invalid JSON body"})

    if not isinstance(body, dict):
        return _respond(400, {"error": "request body must be an object"})

    # Only "reason" is accepted — contract § 5 / security review Cycle 1 S1.
    unexpected = set(body) - {"reason"}
    if unexpected:
        return _respond(400, {"error": f"unexpected field(s): {sorted(unexpected)}"})

    reason = body.get("reason", "")
    if not isinstance(reason, str) or len(reason) > 500:
        return _respond(400, {"error": "reason must be a string of at most 500 characters"})

    # Load the Finding — BEFORE decision state so scope check comes first.
    finding = _load_finding_by_id(finding_id)
    if not finding:
        return _respond(404, {"error": f"Finding {finding_id} not found"})

    # Population-scoped Findings (spec D11, contract § 5):
    # Only product-engineer may decline; decided_by.kind == "engineer".
    # Fleet groups get 403.
    _pop_scope = finding.get("scope", {})
    _is_pop_finding = set(_pop_scope) == {"population_id"}  # C1: exact single-key check

    # Scope check: ALL 403s before reading decision state (security review Cycle 1 W1).
    if _is_pop_finding:
        # Fix #2: product-engineer only on population Findings (spec D11).
        if "product-engineer" not in _extract_groups(claims):
            return _respond(403, {"error": "insufficient scope to decline this finding"})
        standing = "engineer"
    else:
        standing = _check_approve_scope(claims, finding)
        if not standing:
            return _respond(403, {"error": "insufficient scope to decline this finding"})

    # Decline is only for FI queue items (contract § 5).
    rec = finding.get("recommended_action")
    if rec is None:
        return _respond(400, {"error": "decline is only for FI queue items (Findings with recommended_action)"})

    # Kind/scope coupling (spec D11 / contract v2.7): before any write.
    rec_kind = rec.get("action_kind", "")
    if _is_pop_finding and rec_kind not in _ENGINEERING_KINDS:
        return _respond(400, {
            "error": (
                f"action_kind '{rec_kind}' is not valid for a population-scoped Finding; "
                f"population Findings only accept engineering kinds "
                f"({sorted(_ENGINEERING_KINDS)})"
            )
        })
    if not _is_pop_finding and rec_kind in _ENGINEERING_KINDS:
        return _respond(400, {
            "error": (
                f"action_kind '{rec_kind}' requires a population-scoped Finding "
                f"(scope must have population_id and no vin)"
            )
        })

    # FI items: population → engineer standing; fleet → operator standing (spec D11, contract § 5).
    if _is_pop_finding:
        if standing != "engineer":
            return _respond(403, {"error": "insufficient scope to decline this finding"})
    else:
        if standing != "operator":
            return _respond(403, {"error": "insufficient scope to decline this finding"})

    caller_sub = claims.get("sub", "")
    decided_by_kind = "engineer" if _is_pop_finding else "operator"
    decided_by = {"kind": decided_by_kind, "id": caller_sub}
    action_id = _fi_decision_id(finding_id)
    now = _now_iso()

    # Build the declined Action (contract § 5 field values).
    action: dict[str, Any] = {
        "action_id": action_id,
        "finding_id": finding_id,
        "created_by": f"owner-{caller_sub}",
        "created_at": now,
        "updated_at": now,
        "target_system": rec["target_system"],
        "action_kind": rec["action_kind"],
        "status": "declined",
        "decision": "declined",
        "decided_by": decided_by,
        "parameters": {"decline_reason": reason} if reason else {},
        # Critical 1 (FG2d review): copy the Finding's evidence onto the decline
        # Action exactly as the approve route does.  action.schema.json requires
        # evidence; omitting it (FM20) fails schema validation and breaks CMS's
        # direct table reads (contract § 7).
        "evidence": finding.get("evidence", []),
        "audit_trail": [],
    }
    # Storage-only fleet_id for by_fleet GSI (spec D11, contract § 7).
    fi_fleet_id = finding.get("fleet_id")
    if fi_fleet_id:
        action["fleet_id"] = fi_fleet_id

    table = _get_ddb().Table(_ACTIONS_TABLE)
    try:
        table.put_item(
            Item=action,
            ConditionExpression="attribute_not_exists(action_id)",
        )
    except table.meta.client.exceptions.ConditionalCheckFailedException:
        existing = table.get_item(Key={"action_id": action_id},
                                  ConsistentRead=True).get("Item") or {}
        if existing.get("decision") == "declined":
            return _respond(200, _strip_action_for_response(existing))
        return _respond(409, {"error": "already_decided", "action_id": action_id})
    except Exception as exc:
        log.error("DynamoDB PutItem failed for decline action_id=%s: %s", action_id, exc)
        return _respond(500, {"error": "failed to persist decline"})

    log.info("declined finding_id=%s → action_id=%s", finding_id, action_id)
    return _respond(200, _strip_action_for_response(action))


def _caller_customer_id(claims: dict) -> str | None:
    """Return the caller's customer identity, or ``None`` if none is available.

    **This is the (d) seam.** Fix Group 15 builds the owner projection and this
    read path now; the identity *mapping* is the one piece that needs a decision
    outside CVX, and it is isolated here so that decision is a configuration
    change rather than a code change.

    Why a separate claim and not ``claims["sub"]``
    ----------------------------------------------
    (Since 2026-10-07 the projected ``owner_id`` on a VIN Finding is the assigned
    driver's ``sub``, so this customer id matches none of them; see
    ``_CUSTOMER_ID_CLAIM``. The history below explains why it was ever compared.)

    The projected top-level ``owner_id`` was the CMS registry's ``sold_to``, whose
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
    held two namespaces (D-G10-4): owner-scoped Findings carry `scope.owner_id`
    written from `sub`, and VIN-scoped Findings carried the projected `sold_to`
    (`CUST-XXXXXXXX`) until 2026-10-07, when the projection became the assigned
    driver's `sub`. A Cognito `sub` is a UUID, so an owner could never read
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
    """The caller's trusted ``custom:vehicleId`` (the VIN since the 2026-11 CMS migration), or ``None``.

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
        # Spec D13 / contract § 12: FI queue items never reach this iOS route. The
        # shipped iOS decoder has no unknown fallback for their agent ids and kinds,
        # so one FI item would fail the whole response.
        if _is_fi_finding(finding):
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


def _check_approve_scope(claims: dict, finding: dict) -> "str | None":
    """Return the standing that admits the caller, or None if denied.

    Returns ``"operator"`` when the caller holds fleet-operator standing on the
    Finding's fleet (fleet-operator group + trusted custom:fleetIds naming it),
    or is a platform-admin.  Returns ``"user"`` for any other admitted standing
    (service-advisor, owner, fleet-driver).  Returns ``None`` for denied.

    Contract § 4: only ``"operator"`` may decide FI queue items.  Any truthy
    value is treated as allow/deny by callers that do not need the distinction
    (``if not _check_approve_scope(...)``).

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
        return "operator"

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
        return "user"

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
            return "operator"

    # Owner-scoped Finding: the scope IS the owner, and that value is written
    # from `sub` by the executors, so `sub` is the right comparand here.
    # Backward compatibility (F11.3 Accept 2) — not dead code.
    if scope_owner_id and caller_sub and caller_sub == scope_owner_id:
        return "user"

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
            return "user"

        # fleet_driver standing — the caller is assigned to operate THIS vehicle.
        # Compared against the projected vehicle_id, NEVER the VIN. Since the 2026-11
        # CMS vehicleId = VIN migration the claim equals the VIN, so comparing against
        # the VIN would admit the driver on any row, including one the registry never
        # vouched for. The projected vehicle_id (present only when the registry
        # resolved the car) is that proof; when it is absent this route denies
        # (pinned by tests/test_moved_car_standing.py).
        #
        # Gated: custom:vehicleId is self-writable under default WriteAttributes,
        # so an ungated comparison lets any caller forge assignment to any vehicle.
        caller_vehicle_id = (
            claims.get("custom:vehicleId")
            if _claim_is_trusted(_VEHICLE_ID_CLAIM_TRUSTED_ENV)
            else None
        )
        if (
            projected_vehicle_id
            and isinstance(caller_vehicle_id, str)
            and caller_vehicle_id.strip()
            and caller_vehicle_id.strip() == projected_vehicle_id
        ):
            return "user"

        # fleet_manager standing — the caller manages the fleet this vehicle is in.
        # Gated for the same reason as the driver route above, AND on the
        # `fleet-operator` group (spec 2026-09-25-avx-own-vehicle-findings, security
        # review Cycle 1 W1): every read route needs that group for fleet-wide Findings,
        # so without it here a driver or owner carrying custom:fleetIds could approve a
        # Finding on another fleet vehicle that no read route returns to them.
        if ("fleet-operator" in groups and projected_fleet_id
                and _claim_is_trusted(_FLEET_IDS_CLAIM_TRUSTED_ENV)):
            if projected_fleet_id in _caller_fleet_ids(claims):
                return "operator"

    # Fail closed: no owner binding matched.
    return None



# ---------------------------------------------------------------------------
# FI queue item helpers (spec D8, D13, contract §§ 4-5)
# ---------------------------------------------------------------------------

_PROJECTED_ACTION_ATTRS = frozenset({"fleet_id"})


def _strip_action_for_response(item: dict) -> dict:
    """Remove storage-only Action attributes (fleet_id) before returning to the client.

    Mirrors _strip_finding_for_response for the Action side (spec D11).
    """
    if not _PROJECTED_ACTION_ATTRS.intersection(item):
        return item
    return {k: v for k, v in item.items() if k not in _PROJECTED_ACTION_ATTRS}


def _fi_decision_id(finding_id: str) -> str:
    """Deterministic per-Finding decision id for FI queue items (contract § 4).

    sha256("<finding_id>|fi-decision").hexdigest()[:32], UUID-formatted as
    AVX-ACT-{uuid4-like}.  Pinned by literal in test_decline_override.py and the
    auto-approver's suite; changing this formula changes those pins.
    """
    h = hashlib.sha256(f"{finding_id}|fi-decision".encode()).hexdigest()[:32]
    return f"AVX-ACT-{h[0:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:32]}"


def _fi_location_match(finding: dict, parameters: dict) -> bool:
    """True if no location key in parameters contradicts the Finding's location.

    Contract § 4: "Location IDs in parameters must equal the Finding's location."
    The check runs parameters → Finding: if the body carries a location key it must
    (a) exist in the Finding's location dict, and (b) equal the Finding's value.
    Keys the body omits impose no constraint (a recommendation may not repeat
    location IDs for the single-location shape).

    Contract § 4: "A body ID cannot equal a location that is not there." A Finding
    with no ``location`` field (or a non-dict value) is treated as ``{}``, so
    any body location key fails the presence check (→ 400).  A body key whose
    Finding counterpart is absent also fails: ``None`` is not a valid location value,
    so ``body_key: null`` cannot "equal" an absent entry.

    W4 (FG2d review): the previous implementation ran Finding → parameters, which
    (a) returned 400 when a single-location recommendation did not repeat
        location_id in its parameters (approving exactly as recommended failed), and
    (b) returned 200 when the body added a location key the Finding did not have
        (body {location_id: A, to_location_id: Z} on Finding {location_id: A}
        passed silently).
    The corrected direction fixes both.

    W1 (FG3c review): the previous implementation treated a missing ``location``
    field as "no constraint" — any body location key was accepted.  Contract § 4
    requires rejection: a key cannot equal a value that does not exist.
    Additionally, ``None == location.get(key)`` was True when the body sent
    ``key: null`` and the Finding lacked that key — both were ``None``, so the
    check passed silently.  The new implementation uses explicit ``in`` membership
    rather than equality with ``get``, closing both gaps.
    """
    location = finding.get("location")
    if not isinstance(location, dict):
        location = {}  # no location field → every body location key is absent
    for key in ("location_id", "from_location_id", "to_location_id"):
        if key in parameters:
            # Body carries this key — it must be present in the Finding AND match.
            if key not in location or parameters[key] != location[key]:
                return False
    return True


def _drop_fi_findings(items: list[dict]) -> list[dict]:
    """The owner list routes' FI exclusion (issue 2026-10-07, owner_id = assigned driver).

    Since the Tier 2 writer projects the assigned driver's ``sub`` as ``owner_id``, a
    driver's ``/findings/owner/{sub}`` and ``/findings/owner-fleet/{sub}`` reach VIN
    Findings, and FI cost Findings carry the same projection. FI items are
    operator-only (spec D13 / contract § 12), as on ``/findings/me`` and the owner
    branch of ``/findings/vehicle/{vin}``. A short page with a cursor is normal;
    clients follow ``next_token``.

    Expired Findings are deliberately NOT dropped here: these routes have always
    returned them, they belong to the caller's own vehicle, and hiding them would
    change the contract for every owner-route caller.
    """
    return [item for item in items if not _is_fi_finding(item)]


def _is_fi_finding(finding: dict) -> bool:
    """True if this Finding must be hidden from iOS routes (spec D13, contract § 12).

    Hides Findings whose agent_id starts with "agent-fi-" OR that carry
    recommended_action.  Keying on both ensures that an FI decision on a
    pre-existing health kind (e.g. acknowledge_only) is also excluded when the
    Finding itself carries the FI mark.
    """
    agent_id = finding.get("agent_id", "")
    return (
        (isinstance(agent_id, str) and agent_id.startswith("agent-fi-"))
        or "recommended_action" in finding
    )


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

    # ── POST /findings/{finding_id}/decline (Stage A, contract § 5) ──────────
    if method == "POST" and resource == "/findings/{finding_id}/decline":
        return _decline_finding(event, claims)

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
                        and _not_expired(item, now_iso)
                        # Spec D13 / contract § 12, as on /findings/me (security
                        # review Cycle 3 authz S4).
                        and not _is_fi_finding(item)]
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
        items = _drop_fi_findings(items)
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
        items = _drop_fi_findings(items)
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
