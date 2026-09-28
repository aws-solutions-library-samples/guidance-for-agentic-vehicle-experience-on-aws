"""
AVX Actions Lambda (G4.T3)

Routes:
  GET   /actions/owner/{owner_id}          — owner self-read
  GET   /actions/ios-owner/{owner_id}      — ios-owner read (same scope as owner)
  GET   /actions/owner-fleet/{owner_id}    — owner-fleet scope
  GET   /actions/fleet/{fleet_id}          — fleet scope (live via findings→actions GSI join)
  GET   /actions/dealership/{dealer_id}    — dealership scope (live via findings→actions GSI join)
  GET   /actions/district/{district_id}    — district scope (live via findings→actions GSI join)
  PATCH /actions/{action_id}              — status transition
  POST  /actions/{action_id}/dismiss       — dismiss action (→ dismissed, not cancelled)

State machine (8 values):
  pending_execution | executed | in_progress | completed | cancelled | declined
  | dismissed | failed

Terminals (no transitions out):
  completed | cancelled | declined | dismissed | failed

`failed` is executor-only — client PATCH to `failed` returns 403.
`dismissed` is set ONLY by the dismiss route (or executor), not by PATCH.

Dismiss writes a row to vsa-{stage}-avx-feedback for the ranking projector.
`dismissed` is DISTINCT from `cancelled`:
  - dismissed: owner told the surface to stop showing this class of card
               (feedback signal written)
  - cancelled: owner cancelled an in-flight action they created
               (no feedback signal)

Environment variables:
  AVX_STAGE                   — stage name (default "dev")
  AVX_ACTIONS_TABLE           — DynamoDB table for actions
  AVX_FINDINGS_TABLE          — DynamoDB table for findings (needed for dismiss feedback)
  AVX_FEEDBACK_TABLE          — DynamoDB table for feedback
"""
from __future__ import annotations

import json
import logging
import os
import sys
import uuid
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from typing import Any

import boto3
from boto3.dynamodb.conditions import Key, Attr

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------
_LAMBDA_DIR = os.path.dirname(os.path.abspath(__file__))
_LAMBDAS_DIR = os.path.dirname(_LAMBDA_DIR)
if _LAMBDAS_DIR not in sys.path:
    sys.path.insert(0, _LAMBDAS_DIR)

from _lib.avx_scope import (  # noqa: E402
    _extract_groups,
    _load_finding_by_id,
    check_dealer_scope,
    check_district_scope,
    check_fleet_scope,
    check_owner_fleet_scope,
    check_owner_scope,
    check_vehicle_scope,
    is_fleet_vehicle_caller,
)

log = logging.getLogger(__name__)
log.setLevel(logging.INFO)

_STAGE = os.environ.get("AVX_STAGE", "dev")
_ACTIONS_TABLE = os.environ.get("AVX_ACTIONS_TABLE", f"vsa-{_STAGE}-avx-actions")
_FINDINGS_TABLE = os.environ.get("AVX_FINDINGS_TABLE", f"vsa-{_STAGE}-avx-findings")
_FEEDBACK_TABLE = os.environ.get("AVX_FEEDBACK_TABLE", f"vsa-{_STAGE}-avx-feedback")

_ddb = None


def _get_ddb():
    global _ddb
    if _ddb is None:
        _ddb = boto3.resource("dynamodb")
    return _ddb


# ---------------------------------------------------------------------------
# State machine
# ---------------------------------------------------------------------------

# All valid statuses (contract v2.2-final, Addenda D4/D5)
_ALL_STATUSES = frozenset({
    "pending_execution",
    "executed",
    "in_progress",
    "completed",
    "cancelled",
    "declined",
    "dismissed",
    "failed",
})

# Terminal states — no transitions out
_TERMINAL_STATUSES = frozenset({
    "completed",
    "cancelled",
    "declined",
    "dismissed",
    "failed",
})

# Executor-only states — client cannot PATCH to these
_EXECUTOR_ONLY_TARGET_STATUSES = frozenset({"failed"})

# Valid non-terminal → any non-terminal-or-executor-only transitions
# (client-accessible transitions)
_CLIENT_PATCH_ALLOWED_TARGETS = _ALL_STATUSES - _EXECUTOR_ONLY_TARGET_STATUSES - {"dismissed"}


def _can_transition(from_status: str, to_status: str) -> bool:
    """Return True if the transition is valid for a client PATCH."""
    if from_status in _TERMINAL_STATUSES:
        return False
    if to_status in _EXECUTOR_ONLY_TARGET_STATUSES:
        # failed is executor-only — 403 handled at call site
        return False
    if to_status == "dismissed":
        # dismissed is only via the /dismiss route, not PATCH
        return False
    if to_status not in _ALL_STATUSES:
        return False
    return True


# ---------------------------------------------------------------------------
# Response helpers
# ---------------------------------------------------------------------------

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

def _list_actions_by_owner(owner_id: str, limit: int = 50, next_token: str | None = None):
    """List actions where created_by = 'owner-{owner_id}' via GSI query.

    Uses the `by_created_by` GSI: index_name="by_created_by", key_name="created_by"
    CDK declaration: vsa-tier2-stack.ts indexName='by_created_by', partitionKey name='created_by'
    (Fix Group 10 T10.3 — scan fallback removed; a failed Query propagates as 5xx.)
    """
    table = _get_ddb().Table(_ACTIONS_TABLE)
    kwargs: dict[str, Any] = {
        "IndexName": "by_created_by",
        "KeyConditionExpression": Key("created_by").eq(f"owner-{owner_id}"),
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


def _list_actions_by_vin_for_owner(vin: str, owner_id: str, limit: int = 50, next_token: str | None = None):
    """C4: List actions for a VIN filtered to the calling owner's sub.

    T14.3 fix (W-F): Actions are NOT written with a `vin` or `scope.vin` field —
    `_approve_finding` builds 11 keys, neither among them.  The previous post-filter
    on `item.get("scope", {}).get("vin") == vin or item.get("vin") == vin` matched
    nothing and returned 200 [] unconditionally regardless of what actions exist.

    This function cannot be implemented correctly without either:
      (a) a `by_vin` GSI on the actions table, or
      (b) the writer projecting `vin` as a top-level attribute on Action rows
          (the mirror of the `owner_id` / `fleet_id` projection tracked in
          decisions.md § "Tier2 owner_id emission triage").

    Until (a) or (b) lands, the route returns 503 with reason
    `owner_scope_unavailable` — the same pattern as the fleet branch
    (`fleet_scope_unavailable` per T12.1 / decisions.md § "T12.1 cross-tenant
    oracle fix").  This is honest: 503 means "the data is there but this scoped
    query cannot be served yet", not "no actions exist for this VIN".

    Re-enable: delete this function body and replace with a real implementation
    once the writer projects top-level `vin` on Action rows (or a by_vin GSI
    is added to the actions table).  See decisions.md § "2026-09-20 — T14.3".
    """
    raise _OwnerVinScopeUnavailable()


class _OwnerVinScopeUnavailable(Exception):
    """Raised by _list_actions_by_vin_for_owner to signal 503 to the route handler."""


def _list_actions_by_vin(vin: str, limit: int = 50, next_token: str | None = None):
    """C4: List actions for a VIN for service/fleet/admin callers (no owner filter).

    The actions table has no by_vin GSI.  Two-step fan-out join:
      1. Query findings table by_vin GSI → at most `limit` finding_ids for this page.
         `next_token` is the findings-table ExclusiveStartKey: `next_token: None`
         means all findings for this VIN (and therefore all their actions) have
         been returned.  A non-None `next_token` means more findings pages remain.
      2. For each finding_id, fully page the by_finding GSI (following
         LastEvaluatedKey to completion) so every action for that finding is
         included.  A truncated inner page would produce fewer actions than exist
         while still returning `next_token: None` when the findings page is
         exhausted — violating the invariant that `next_token: None` means complete.

    T14.3 fix (W-C service/admin): the previous code dropped each inner query's
    LastEvaluatedKey, silently truncating actions when a finding had more than one
    DynamoDB result page.  Measured: callers received 6 of ≥15 actions with
    `next_token: None`.  Fix: follow LastEvaluatedKey within each finding's
    by_finding sub-pages until exhausted before moving to the next finding_id.

    (Fix Group 10 T10.3 — scan fallback removed; a failed Query propagates as 5xx.)
    """
    findings_table = _get_ddb().Table(_FINDINGS_TABLE)
    # Step 1: page of finding_ids for this VIN (bounded to `limit` findings).
    f_kwargs: dict[str, Any] = {
        "IndexName": "by_vin",
        "KeyConditionExpression": Key("vin").eq(vin),
        "ScanIndexForward": False,
        "Limit": limit,
    }
    if next_token:
        try:
            f_kwargs["ExclusiveStartKey"] = json.loads(
                __import__("base64").b64decode(next_token).decode()
            )
        except Exception:
            pass
    f_resp = findings_table.query(**f_kwargs)
    finding_ids = [item["finding_id"] for item in f_resp.get("Items", []) if "finding_id" in item]
    last_key = f_resp.get("LastEvaluatedKey")
    out_token = None
    if last_key:
        out_token = __import__("base64").b64encode(
            json.dumps(last_key, cls=_DecimalEncoder).encode()
        ).decode()
    if not finding_ids:
        return [], out_token
    # Step 2: for each finding_id, fully page the by_finding GSI so no actions
    # are silently truncated.  LastEvaluatedKey is followed until exhausted.
    actions_table = _get_ddb().Table(_ACTIONS_TABLE)
    items: list[dict] = []
    for fid in finding_ids:
        a_kwargs: dict[str, Any] = {
            "IndexName": "by_finding",
            "KeyConditionExpression": Key("finding_id").eq(fid),
            "ScanIndexForward": False,
        }
        while True:
            a_resp = actions_table.query(**a_kwargs)
            items.extend(a_resp.get("Items", []))
            inner_last = a_resp.get("LastEvaluatedKey")
            if not inner_last:
                break
            a_kwargs["ExclusiveStartKey"] = inner_last
    return items, out_token


def _list_actions_by_vin_for_fleet(vin: str, caller_fleet_ids: list[str], limit: int = 50, next_token: str | None = None):
    """W-C3-fleet fix (2026-09-19 fix cycle 2): List actions for a VIN filtered to
    the caller's fleet memberships.

    Queries findings by vin via the findings table's by_vin GSI (filtered to the
    caller's fleet_ids), then resolves actions for those finding_ids via by_finding.
    (Fix Group 10 T10.3 — scan fallback removed; T14.4: dual-shape comment removed;
    FilterExpression uses top-level fleet_id — the attribute T10.2 denormalises and
    the by_fleet GSI reads. scope.fleet_id is the pre-projection nested path and is
    NOT used here.)
    """
    from boto3.dynamodb.conditions import Attr
    findings_table = _get_ddb().Table(_FINDINGS_TABLE)
    # Step 1: find findings for this VIN, filtered to caller's fleet memberships.
    # fleet_id is top-level after T10.2; FilterExpression uses Attr("fleet_id") —
    # the authorisation filter the T11.6 follow-on (b) projection will satisfy.
    # There is no backward-compat nested-shape fallback: scope.fleet_id was the
    # pre-projection path, and the by_fleet GSI (the authoritative route for fleet
    # reads) was always keyed on the top-level attribute.
    f_kwargs: dict[str, Any] = {
        "IndexName": "by_vin",
        "KeyConditionExpression": Key("vin").eq(vin),
        "FilterExpression": Attr("fleet_id").is_in(caller_fleet_ids),
        "ScanIndexForward": False,
        "Limit": limit,
    }
    if next_token:
        try:
            f_kwargs["ExclusiveStartKey"] = json.loads(
                __import__("base64").b64decode(next_token).decode()
            )
        except Exception:
            pass
    f_resp = findings_table.query(**f_kwargs)
    finding_ids = [item["finding_id"] for item in f_resp.get("Items", []) if "finding_id" in item]
    last_key = f_resp.get("LastEvaluatedKey")
    out_token = None
    if last_key:
        out_token = __import__("base64").b64encode(
            json.dumps(last_key, cls=_DecimalEncoder).encode()
        ).decode()
    if not finding_ids:
        return [], out_token
    # Step 2: resolve actions for each finding_id via by_finding GSI.
    #
    # W3 (review Cycle 3): this was the FOURTH instance of the fan-out truncation
    # T14.3 bounded in `_list_actions_by_vin` — the inner query passed no Limit and
    # discarded LastEvaluatedKey, so a finding with more actions than one page lost
    # the remainder silently while the caller still saw `next_token: None`. Bounded
    # here with the same pattern as the sibling helper, deliberately identical so the
    # two cannot drift again.
    #
    # This helper is currently unreachable — the fleet VIN route returns 503 per
    # T12.1 until the top-level `fleet_id` projection lands (T11.6 follow-on (b)).
    # It is fixed anyway: the re-enable comment at the route points here, and
    # re-enabling a path that silently truncates is exactly the failure this spec
    # has now hit four times.
    actions_table = _get_ddb().Table(_ACTIONS_TABLE)
    items: list[dict] = []
    for fid in finding_ids:
        a_kwargs: dict[str, Any] = {
            "IndexName": "by_finding",
            "KeyConditionExpression": Key("finding_id").eq(fid),
            "ScanIndexForward": False,
        }
        while True:
            a_resp = actions_table.query(**a_kwargs)
            items.extend(a_resp.get("Items", []))
            inner_last = a_resp.get("LastEvaluatedKey")
            if not inner_last:
                break
            a_kwargs["ExclusiveStartKey"] = inner_last
    return items, out_token


# NOTE: _list_actions_by_scope was removed by T11.5 (Fix Group 11).
#
# The three scope routes (/actions/fleet, /actions/dealership, /actions/district)
# previously used a fan-out join: query findings by scope GSI → resolve actions via
# by_finding GSI. This join cannot honour the paginated contract the OpenAPI declares:
# `limit`/`next_token` measured findings while the payload was actions; the inner
# by_finding queries passed no Limit and discarded LastEvaluatedKey (silent truncation);
# an empty page could be returned with a continuation token; up to 51 sequential round
# trips at limit=50. Patching the join's pagination is not a fix — it is a
# better-disguised version of the same problem.
#
# The correct implementation is to project scope onto Action rows (mirroring T10.2
# for Findings) and add by_fleet/by_dealership/by_district GSIs to the actions table.
# This is tracked as a T11.6 follow-on in decisions.md.
#
# Until then, the three routes return 501 with reason code `actions_scope_index_absent`.
# Scope authorization still runs BEFORE the 501 (Accept 2): an unentitled caller
# gets 403, not 501 — the stub must not become an oracle disclosing which scopes exist.


def _get_action(action_id: str) -> dict | None:
    table = _get_ddb().Table(_ACTIONS_TABLE)
    resp = table.get_item(Key={"action_id": action_id})
    return resp.get("Item")


def _update_action_status(
    action_id: str,
    from_status: str,
    to_status: str,
    actor: str,
    audit_note: str | None = None,
) -> dict | None:
    """Atomically update action status and append to audit_trail. Returns updated item."""
    table = _get_ddb().Table(_ACTIONS_TABLE)
    now = _now_iso()
    audit_entry: dict[str, Any] = {
        "at": now,
        "by": actor,
        "from_status": from_status,
        "to_status": to_status,
    }
    if audit_note:
        audit_entry["note"] = audit_note

    try:
        resp = table.update_item(
            Key={"action_id": action_id},
            UpdateExpression=(
                "SET #st = :new_status, updated_at = :now, "
                "audit_trail = list_append(if_not_exists(audit_trail, :empty_list), :entry)"
            ),
            ConditionExpression="attribute_exists(action_id) AND #st = :old_status",
            ExpressionAttributeNames={"#st": "status"},
            ExpressionAttributeValues={
                ":new_status": to_status,
                ":old_status": from_status,
                ":now": now,
                ":entry": [audit_entry],
                ":empty_list": [],
            },
            ReturnValues="ALL_NEW",
        )
        return resp.get("Attributes")
    except table.meta.client.exceptions.ConditionalCheckFailedException:
        return None


# ---------------------------------------------------------------------------
# Dismiss handler
# ---------------------------------------------------------------------------

def _write_feedback_row(owner_id: str, finding_kind: str, action_id: str) -> None:
    """Write a feedback row to vsa-{stage}-avx-feedback for the ranking projector."""
    table = _get_ddb().Table(_FEEDBACK_TABLE)
    now = datetime.now(timezone.utc)
    ttl_epoch = int((now + timedelta(days=30)).timestamp())
    feedback_id = str(uuid.uuid4())
    table.put_item(Item={
        "owner_id": owner_id,
        "feedback_id": feedback_id,
        "action_id": action_id,
        "finding_kind": finding_kind,
        "dismissed_at": now.isoformat(timespec="seconds").replace("+00:00", "Z"),
        "ttl": ttl_epoch,
    })


def _dismiss_action(action_id: str, claims: dict) -> dict[str, Any]:
    """POST /actions/{action_id}/dismiss — transitions to 'dismissed' (not 'cancelled')."""
    action = _get_action(action_id)
    if not action:
        return _respond(404, {"error": f"Action {action_id} not found"})

    # Scope check: owner self-action
    caller_sub = claims.get("sub", "")
    groups = _extract_groups(claims)
    created_by = action.get("created_by", "")
    owner_part = created_by.removeprefix("owner-")

    if "platform-admin" not in groups and caller_sub != owner_part:
        return _respond(403, {"error": "insufficient scope to dismiss this action"})

    current_status = action.get("status", "")
    # dismiss → dismissed regardless of prior state (per Addendum D4)
    # (even if already dismissed — idempotent)
    if current_status == "dismissed":
        return _respond(200, action)

    actor = f"owner-{caller_sub}"
    updated = _update_action_status(
        action_id=action_id,
        from_status=current_status,
        to_status="dismissed",  # MUST be "dismissed", not "cancelled" — Addendum D4
        actor=actor,
        audit_note="owner dismissed via /dismiss route",
    )
    if updated is None:
        # Re-fetch (race condition or already changed)
        action = _get_action(action_id) or action
        return _respond(200, action)

    # Write feedback row for the ranking projector
    # Get finding_kind from the parent Finding (if available)
    finding_kind = "unknown"
    finding_id = action.get("finding_id")
    if finding_id:
        finding = _load_finding_by_id(finding_id)
        if finding:
            finding_kind = finding.get("finding_kind", "unknown")

    # W6 fix (2026-09-19): use the action's owner (from created_by), not the
    # caller's sub. When platform-admin dismisses on behalf, the feedback signal
    # must be attributed to the owner's sub (the one whose ranking model is updated),
    # not the admin's sub. The caller's identity is in the audit_trail actor field.
    feedback_owner_id = owner_part  # from action.created_by stripped of "owner-" prefix

    try:
        _write_feedback_row(
            owner_id=feedback_owner_id,
            finding_kind=finding_kind,
            action_id=action_id,
        )
    except Exception as exc:
        log.warning("feedback write failed for action_id=%s: %s", action_id, exc)
        # Non-fatal — dismiss still succeeds

    log.info("dismissed action_id=%s actor=%s", action_id, actor)
    return _respond(200, updated)


# ---------------------------------------------------------------------------
# PATCH status transition
# ---------------------------------------------------------------------------

def _patch_action(action_id: str, claims: dict, event: dict) -> dict[str, Any]:
    """PATCH /actions/{action_id} — state machine transition."""
    action = _get_action(action_id)
    if not action:
        return _respond(404, {"error": f"Action {action_id} not found"})

    # Scope check: owner or platform-admin may PATCH.
    # W3 fix (2026-09-19): service-advisor and fleet-operator are REMOVED from the
    # blanket bypass. They must hold the action's specific fleet/dealer scope.
    caller_sub = claims.get("sub", "")
    groups = _extract_groups(claims)
    created_by = action.get("created_by", "")
    owner_part = created_by.removeprefix("owner-")

    if "platform-admin" not in groups:
        # Check fleet-operator scope against the action's fleet_id (if present)
        action_fleet_id = action.get("fleet_id") or action.get("scope", {}).get("fleet_id")
        if "fleet-operator" in groups and action_fleet_id:
            # Fix Group 29: gated. `custom:fleetIds` is self-writable under an app
            # client's default `WriteAttributes`, so an ungated read let a
            # fleet-operator forge membership of the action's fleet and PATCH it.
            #
            # This site was NOT in review cycle 3's list of seven — that list was
            # scoped to `_lib/avx_scope.py` plus the findings handler, and this is a
            # third file reading the raw claim directly. Found by running the
            # "what ELSE reads this claim" grep across all of `lambdas/` and
            # `agents/`, which is the lesson cycle 3 recorded and this is the first
            # application of it.
            #
            # Fail-closed by construction: an untrusted claim yields `[]`, and
            # `action_fleet_id not in []` is True, so the 403 below fires.
            from _lib.avx_scope import (
                FLEET_IDS_CLAIM_TRUSTED_ENV,
                _trusted_claim_list,
            )
            caller_fleet_ids = _trusted_claim_list(
                claims, "custom:fleetIds", FLEET_IDS_CLAIM_TRUSTED_ENV
            )
            if action_fleet_id not in caller_fleet_ids:
                return _respond(403, {"error": "insufficient scope: fleet-operator not in action's fleet"})
        elif "service-advisor" in groups:
            # service-advisor: allow PATCH (broad service-lane access)
            pass
        elif caller_sub != owner_part:
            return _respond(403, {"error": "insufficient scope to update this action"})

    try:
        body = json.loads(event.get("body") or "{}")
    except json.JSONDecodeError:
        return _respond(400, {"error": "invalid JSON body"})

    to_status = body.get("status")
    if not to_status:
        return _respond(400, {"error": "status required"})

    if to_status not in _ALL_STATUSES:
        return _respond(400, {"error": f"unknown status: {to_status}. Must be one of: {sorted(_ALL_STATUSES)}"})

    # failed is executor-only
    if to_status in _EXECUTOR_ONLY_TARGET_STATUSES:
        return _respond(403, {"error": "status 'failed' is set only by the executor, not by clients"})

    # dismissed is only via the /dismiss route
    if to_status == "dismissed":
        return _respond(400, {"error": "use POST /actions/{action_id}/dismiss to dismiss an action"})

    current_status = action.get("status", "")

    # Check monotonicity: no transitions out of terminal states
    if current_status in _TERMINAL_STATUSES:
        return _respond(400, {
            "error": f"action is in terminal state '{current_status}' and cannot be transitioned"
        })

    if not _can_transition(current_status, to_status):
        return _respond(400, {
            "error": f"transition from '{current_status}' to '{to_status}' is not allowed"
        })

    actor = f"owner-{caller_sub}"
    updated = _update_action_status(
        action_id=action_id,
        from_status=current_status,
        to_status=to_status,
        actor=actor,
    )
    if updated is None:
        # Status changed concurrently or action no longer exists
        current = _get_action(action_id)
        if current:
            return _respond(409, {
                "error": "concurrent modification detected",
                "current_status": current.get("status"),
            })
        return _respond(404, {"error": f"Action {action_id} not found"})

    return _respond(200, updated)


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

    limit = min(int(qs.get("page_size", "50")), 200)
    next_token = qs.get("next_token")

    # ── POST /actions/{action_id}/dismiss ─────────────────────────────────────
    if method == "POST" and "dismiss" in resource:
        action_id = path_params.get("action_id")
        if not action_id:
            return _respond(400, {"error": "action_id required"})
        return _dismiss_action(action_id, claims)

    # ── PATCH /actions/{action_id} ────────────────────────────────────────────
    if method == "PATCH":
        action_id = path_params.get("action_id")
        if not action_id:
            return _respond(400, {"error": "action_id required"})
        return _patch_action(action_id, claims, event)

    # ── GET routes ───────────────────────────────────────────────────────────

    # C4: /actions/vehicle/{vin} — mirrors findings scope surface per G4.T3 accept.
    # vehicle-owner/consumer get owner-scoped results; service/fleet/admin get all.
    if method == "GET" and "vehicle" in resource:
        vin = path_params.get("vin")
        if not vin:
            return _respond(400, {"error": "vin required"})

        caller_groups = _extract_groups(claims)
        caller_sub = claims.get("sub", "")

        is_owner_caller = bool(
            caller_groups & {"vehicle-owner", "consumer"}
            and not (caller_groups & {"platform-admin", "service-advisor", "technician",
                                      "fleet-driver", "fleet-operator"})
        )

        # W-C3-fleet fix (2026-09-19 fix cycle 2): fleet callers are routed to a
        # fleet-filtered query so they only see actions for VINs in their fleets.
        is_fleet_caller, caller_fleet_ids = is_fleet_vehicle_caller(claims)

        if is_owner_caller:
            # Owner-scoped: only own actions against this VIN.
            # T14.3 fix (W-F): _list_actions_by_vin_for_owner raises
            # _OwnerVinScopeUnavailable because no writer projects `vin` onto Action
            # rows; the previous post-filter matched nothing and returned 200 [] silently.
            #
            # W4 (review Cycle 3) corrects what this comment used to claim. There is
            # NO 403 path for an owner caller here: `is_owner_caller` is a CLASSIFICATION
            # (which branch serves this caller), not an entitlement gate, and scoping is
            # done by `caller_sub` — an owner can only ever be scoped to themselves, so
            # there is nothing to deny. The previous wording ("an owner with no
            # entitlement gets 403, not 503") described a branch that does not exist.
            # The behaviour is correct and oracle-free: the 503 is unconditional across
            # VINs, so it leaks nothing about which VINs have actions. Callers who are
            # NOT owners fall through to the service/admin branch, where
            # `check_vehicle_scope` is the real 403 gate.
            try:
                items, out_token = _list_actions_by_vin_for_owner(vin, caller_sub, limit, next_token)
            except _OwnerVinScopeUnavailable:
                log.warning(
                    "owner_scope_unavailable vin=%s (actions): Action rows carry no `vin` "
                    "attribute — owner-scoped VIN query cannot be served until the writer "
                    "projects top-level `vin` on Action rows",
                    vin,
                )
                return _respond(503, {
                    "error": "owner_scope_unavailable",
                    "detail": (
                        "actions for this VIN cannot be scoped to an owner until the "
                        "Action writer projects a top-level `vin` attribute; "
                        "retry after the writer is updated"
                    ),
                })
            except Exception as exc:
                log.error("list actions by vin=%s owner=%s failed: %s", vin, caller_sub, exc)
                return _respond(500, {"error": "internal error"})
            return _respond(200, {
                "actions": items,
                "next_token": out_token,
                "computed_at": _now_iso(),
            })
        elif is_fleet_caller:
            # Fleet-scoped VIN query: fleet-driver/fleet-operator callers see only
            # actions whose fleet_id (top-level) is in their custom:fleetIds.
            # T14.4: fleet_id is top-level (same shape as the by_fleet GSI key and the
            # findings handler); scope.fleet_id is the pre-projection nested path and is
            # not used here.
            #
            # Fix Group 29 made this 403 load-bearing — do not delete it as redundant
            # with `check_vehicle_scope`. Since FG29, an UNTRUSTED `custom:fleetIds`
            # yields `(True, [])` from `is_fleet_vehicle_caller` — fleet-scoped with zero
            # permitted fleets — deliberately, because `(False, [])` is the PRIVILEGED
            # answer and would send this route down the unfiltered path. This 403 is what
            # converts that into a deny. `caller_fleet_ids` is now empty in two distinct
            # situations: genuinely no fleet membership, and any fleet caller while the
            # trust flag is off (the deployed default). Both must deny.
            if not caller_fleet_ids:
                return _respond(403, {"error": "insufficient scope: no fleet membership"})
            #
            # T12.1: unconditional 503 — no per-VIN probe.
            #
            # _list_actions_by_vin_for_fleet queries findings by VIN with a fleet_id
            # FilterExpression, then resolves actions via by_finding.  VIN-scoped Findings
            # carry only top-level `vin`; the filter matches nothing.  Probing per-VIN
            # leaks tenant existence: 200 [] = no findings for this VIN; 503 = some exist.
            # Both cases return 503 unconditionally until the projection lands.
            #
            # Re-enable: delete this return, then call
            #   _list_actions_by_vin_for_fleet(vin, caller_fleet_ids, limit, next_token)
            # which filters on top-level Attr("fleet_id").is_in(caller_fleet_ids).
            # Add tests that exercise it. See decisions.md § "2026-09-20 — T12.1
            # cross-tenant oracle fix" and § "T14.4 shape reconciliation".
            log.warning(
                "fleet_scope_unavailable vin=%s (actions): fleet_id projection not yet "
                "deployed — path unavailable until writer.py emits top-level fleet_id "
                "for VIN-scope",
                vin,
            )
            return _respond(503, {
                "error": "fleet_scope_unavailable",
                "detail": (
                    "actions for this VIN cannot be scoped to a fleet until the "
                    "fleet_id projection is deployed; retry after the projection is deployed"
                ),
            })
        else:
            if not check_vehicle_scope(claims, vin):
                return _respond(403, {"error": "insufficient scope"})
            try:
                items, out_token = _list_actions_by_vin(vin, limit, next_token)
            except Exception as exc:
                log.error("list actions by vin=%s failed: %s", vin, exc)
                return _respond(500, {"error": "internal error"})
            return _respond(200, {
                "actions": items,
                "next_token": out_token,
                "computed_at": _now_iso(),
            })

    # /actions/owner/{owner_id} and /actions/ios-owner/{owner_id} share same scope
    if method == "GET" and ("owner" in resource) and "fleet" not in resource:
        owner_id = path_params.get("owner_id")
        if not owner_id:
            return _respond(400, {"error": "owner_id required"})
        if not check_owner_scope(claims, owner_id):
            return _respond(403, {"error": "insufficient scope"})
        try:
            items, out_token = _list_actions_by_owner(owner_id, limit, next_token)
        except Exception as exc:
            log.error("list actions by owner_id=%s failed: %s", owner_id, exc)
            return _respond(500, {"error": "internal error"})
        return _respond(200, {
            "actions": items,
            "next_token": out_token,
            "computed_at": _now_iso(),
        })

    # /actions/owner-fleet/{owner_id}
    if method == "GET" and "owner-fleet" in resource:
        owner_id = path_params.get("owner_id")
        if not owner_id:
            return _respond(400, {"error": "owner_id required"})
        # W1 fix: pass owner_fleet_id=None — no fleet context at route level.
        # Fleet-operator with any fleet is denied; only owner self-read passes.
        if not check_owner_fleet_scope(claims, owner_id, owner_fleet_id=None):
            return _respond(403, {"error": "insufficient scope"})
        try:
            items, out_token = _list_actions_by_owner(owner_id, limit, next_token)
        except Exception as exc:
            log.error("list actions by owner-fleet owner_id=%s failed: %s", owner_id, exc)
            return _respond(500, {"error": "internal error"})
        return _respond(200, {
            "actions": items,
            "next_token": out_token,
            "computed_at": _now_iso(),
        })

    # /actions/fleet/{fleet_id} — T11.5: 501 stub (scope auth runs first)
    # The fan-out join (_list_actions_by_scope) is removed; see comment above that
    # function. Scope check MUST run before the 501 so an unentitled caller gets 403
    # rather than 501 — the stub must not disclose which scopes exist (Accept 2).
    if method == "GET" and "fleet" in resource:
        fleet_id = path_params.get("fleet_id")
        if not fleet_id:
            return _respond(400, {"error": "fleet_id required"})
        # Authorization runs BEFORE the 501 (T11.5 Accept 2).
        if not check_fleet_scope(claims, fleet_id):
            return _respond(403, {"error": "insufficient scope"})
        # T11.5: no scope GSIs on the actions table — cannot serve a scoped read.
        # Real fix tracked in decisions.md § "2026-09-20 — T11.6 follow-on (a)".
        log.info(
            "actions_scope_index_absent fleet_id=%s — scope GSIs not yet on actions table",
            fleet_id,
        )
        return _respond(501, {
            "error": "actions_scope_index_absent",
            "detail": (
                "actions-by-fleet queries require scope GSIs on the actions table "
                "which are not yet deployed; retry after the actions table is migrated"
            ),
        })

    # /actions/dealership/{dealer_id} — T11.5: 501 stub (scope auth runs first)
    if method == "GET" and "dealership" in resource:
        dealer_id = path_params.get("dealer_id")
        if not dealer_id:
            return _respond(400, {"error": "dealer_id required"})
        # Authorization runs BEFORE the 501 (T11.5 Accept 2).
        if not check_dealer_scope(claims, dealer_id):
            return _respond(403, {"error": "insufficient scope"})
        # T11.5: no scope GSIs on the actions table — cannot serve a scoped read.
        # Real fix tracked in decisions.md § "2026-09-20 — T11.6 follow-on (a)".
        log.info(
            "actions_scope_index_absent dealer_id=%s — scope GSIs not yet on actions table",
            dealer_id,
        )
        return _respond(501, {
            "error": "actions_scope_index_absent",
            "detail": (
                "actions-by-dealership queries require scope GSIs on the actions table "
                "which are not yet deployed; retry after the actions table is migrated"
            ),
        })

    # /actions/district/{district_id} — T11.5: 501 stub (scope auth runs first)
    if method == "GET" and "district" in resource:
        district_id = path_params.get("district_id")
        if not district_id:
            return _respond(400, {"error": "district_id required"})
        # Authorization runs BEFORE the 501 (T11.5 Accept 2).
        if not check_district_scope(claims, district_id):
            return _respond(403, {"error": "insufficient scope"})
        # T11.5: no scope GSIs on the actions table — cannot serve a scoped read.
        # Real fix tracked in decisions.md § "2026-09-20 — T11.6 follow-on (a)".
        log.info(
            "actions_scope_index_absent district_id=%s — scope GSIs not yet on actions table",
            district_id,
        )
        return _respond(501, {
            "error": "actions_scope_index_absent",
            "detail": (
                "actions-by-district queries require scope GSIs on the actions table "
                "which are not yet deployed; retry after the actions table is migrated"
            ),
        })

    return _respond(404, {"error": "route not found"})
