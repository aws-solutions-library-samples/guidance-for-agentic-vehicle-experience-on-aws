"""
AVX Approval-Rules Lambda — spec D9, contract § 6.

Routes:
  GET    /approval-rules/fleet/{fleet_id}             — list a fleet's rules
  PUT    /approval-rules/fleet/{fleet_id}/{rule_id}   — create or update a rule
  DELETE /approval-rules/fleet/{fleet_id}/{rule_id}   — delete a rule

Scope (contract § 6):
  The caller needs ``fleet-operator`` or ``platform-admin`` group AND a trusted
  ``custom:fleetIds`` naming the fleet.  There is NO ``platform-admin`` bypass of
  the membership check, matching ``/findings/fleet/{fleet_id}``.
  ``custom:fleetIds`` is read only through ``_lib/avx_scope.py``'s trust gate.

Create semantics (contract § 6, spec D9):
  A new rule is always stored with ``enabled: False``.  A create body that already
  sets ``enabled: True`` returns 400 ``rules_are_created_disabled``, so enabling is
  always a second, deliberate PUT.  An update (existing row) honours the field.

Guarded kinds (spec D5 — literal, never imported from policy.py):
  ``exempt_recall``, ``ground_vehicle``, ``submit_warranty_claim``, ``complete_recall``

Non-auto-approvable kinds → 400 (read from the policy table).

Limits (stage E Group 8, U6; spec group8-notes G8-D6): a kind whose policy row has a
``rule_limit_param`` (``move_vehicles``: the move's ``transfer_cost_usd``) requires a
numeric ``limit`` from 0 to the configured ``max_limit_usd``, read from SSM per request
(``AVX_AUTO_APPROVAL_BOUNDS_PARAM``); unreadable bounds → 503, nothing written. A ``limit``
on any other kind → 400. An update that changes a rule's ``action_kind`` is a new rule:
it is stored disabled, and ``enabled: true`` in that body is refused.

Environment variables:
  AVX_APPROVAL_RULES_TABLE        — DynamoDB rules table (required; no fallback — fails closed)
  AVX_AUTO_APPROVAL_BOUNDS_PARAM  — SSM parameter with the limited kinds' bounds
  AVX_STAGE                       — stage name (informational)
"""
from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

import boto3
from boto3.dynamodb.conditions import Key

# ---------------------------------------------------------------------------
# Path setup — insert the lambdas/ root so _lib can be found
# ---------------------------------------------------------------------------
_LAMBDA_DIR = os.path.dirname(os.path.abspath(__file__))
_LAMBDAS_DIR = os.path.dirname(_LAMBDA_DIR)
_REPO_ROOT = os.path.dirname(_LAMBDAS_DIR)
for _p in (_LAMBDAS_DIR, _REPO_ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from _lib.avx_scope import check_fleet_scope  # noqa: E402

log = logging.getLogger(__name__)
log.setLevel(logging.INFO)

# No default fallback — the Lambda must be configured with this env var.
# In production, an unset var means the table call raises a TypeError (fail closed).
# In tests, _get_ddb() is patched so Table(None) returns the mock regardless.
_RULES_TABLE: str | None = os.environ.get("AVX_APPROVAL_RULES_TABLE") or None

# Guarded kinds — **literal per spec D5** ("pinned by name, not derived").
# Never import this set from policy.py: the D5 invariant says the guardrail tests
# must name kinds literally so that deleting a policy row doesn't silently unguard.
_GUARDED_KINDS: frozenset[str] = frozenset({
    "exempt_recall",          # G1: safety recall may never be auto-exempted
    "ground_vehicle",         # G2: grounding requires the operator
    "submit_warranty_claim",  # G3: sending a warranty claim requires the operator
    "complete_recall",        # G4: a recall completion needs a service record check
})

_ddb = None
_ssm = None


def _load_bounds() -> dict:
    """The limited kinds' bounds (policy.parse_auto_approval_bounds), or BoundsUnavailable."""
    global _ssm
    from agents.executors.policy import load_auto_approval_bounds  # noqa: PLC0415

    if _ssm is None:
        _ssm = boto3.client("ssm")
    return load_auto_approval_bounds(_ssm, os.environ.get("AVX_AUTO_APPROVAL_BOUNDS_PARAM", ""))


def _rule_limit_param(action_kind) -> "str | None":
    """The kind's rule_limit_param from the policy table (None for unknown kinds)."""
    from agents.executors.policy import ACTION_KINDS  # noqa: PLC0415

    row = ACTION_KINDS.get(action_kind) if isinstance(action_kind, str) else None
    return row.rule_limit_param if row is not None else None


def _get_ddb():
    global _ddb
    if _ddb is None:
        _ddb = boto3.resource("dynamodb")
    return _ddb


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
# Policy helper — auto_approvable gate
# ---------------------------------------------------------------------------

def _is_auto_approvable(action_kind: str) -> bool:
    """True iff this kind can be decided by an auto-approval rule.

    Reads ``agents.executors.policy.ACTION_KINDS``.  Guarded kinds are checked
    by name before this function is called (D5), so a failing import here is the
    only code path that returns False for a known-good kind.
    """
    try:
        from agents.executors.policy import ACTION_KINDS  # noqa: PLC0415
        row = ACTION_KINDS.get(action_kind)
        return row is not None and bool(row.auto_approvable)
    except Exception:
        log.exception(
            "Failed to load policy table for kind %r; refusing as not auto_approvable",
            action_kind,
        )
        return False


# ---------------------------------------------------------------------------
# Route handlers
# ---------------------------------------------------------------------------

def _handle_get_rules(fleet_id: str) -> dict:
    """GET /approval-rules/fleet/{fleet_id} — return the fleet's rules list."""
    table = _get_ddb().Table(_RULES_TABLE)
    resp = table.query(
        KeyConditionExpression=Key("fleet_id").eq(fleet_id),
    )
    return _respond(200, {"rules": resp.get("Items", [])})


def _handle_put_rule(
    fleet_id: str,
    rule_id: str,
    sub: str,
    body: dict[str, Any],
) -> dict:
    """PUT /approval-rules/fleet/{fleet_id}/{rule_id} — create or update a rule.

    Creation (no existing row):
      - ``enabled: True`` in the body → 400 ``rules_are_created_disabled``
      - otherwise stores ``enabled: False`` regardless of the body value

    Update (existing row):
      - ``enabled`` field is honoured; can be toggled to True

    Body fields accepted:
      ``action_kind`` — the kind this rule auto-approves
      ``enabled``     — boolean (honoured on update; ignored / forced False on create)
      ``limit``       — required for a limited kind (``move_vehicles``), validated against
                        the bounds; 400 ``limit_not_supported`` on any other kind (G8-D6)
    """
    action_kind = body.get("action_kind")
    limit = body.get("limit")
    enabled_in_body = body.get("enabled")

    # ── 0. Body shape (OpenAPI ApprovalRuleRequest: additionalProperties false) ──
    unexpected = set(body) - {"action_kind", "enabled", "limit"}
    if unexpected:
        return _respond(400, {"code": "unexpected_field",
                              "message": f"unexpected field(s): {sorted(unexpected)}"})
    if action_kind is not None and not isinstance(action_kind, str):
        return _respond(400, {"code": "invalid_action_kind",
                              "message": "action_kind must be a string"})
    if enabled_in_body is not None and not isinstance(enabled_in_body, bool):
        # bool("false") is True: a string must never enable a standing approval.
        return _respond(400, {"code": "invalid_enabled",
                              "message": "enabled must be a boolean"})

    # ── 1. Guarded-kind check (spec D5: literal names, never from policy.py) ──
    if action_kind in _GUARDED_KINDS:
        return _respond(400, {
            "code": "guarded_kind",
            "message": (
                f"Rules for '{action_kind}' are not permitted; "
                "this kind requires operator approval (contract § 10)."
            ),
        })

    # ── 2. Auto-approvable check (from policy table) ───────────────────────────
    # Skip when action_kind is absent (update-only body with no kind change).
    if action_kind is not None and not _is_auto_approvable(action_kind):
        return _respond(400, {
            "code": "kind_not_auto_approvable",
            "message": (
                f"'{action_kind}' is not auto-approvable in this deployment; "
                "no rule can be created for it (spec D9, contract § 6)."
            ),
        })

    if limit is not None and (isinstance(limit, bool) or not isinstance(limit, (int, float))):
        return _respond(400, {"code": "invalid_limit", "message": "limit must be a number"})

    # ── 3. Table access: detect create vs. update ──────────────────────────────
    # Warning 6 fix (review FG2-lambdas): use ConsistentRead so a stale cache
    # doesn't make us think a just-deleted rule still exists, and so that the
    # enabled=False gate on creation can't be bypassed by a stale read.
    table = _get_ddb().Table(_RULES_TABLE)
    existing_resp = table.get_item(
        Key={"fleet_id": fleet_id, "rule_id": rule_id},
        ConsistentRead=True,
    )
    existing = existing_resp.get("Item")

    if existing is None and action_kind is None:
        # OpenAPI ApprovalRuleRequest requires it; a rule with no kind is meaningless and
        # the evaluator could not match it. Before the limits, so a create body with a
        # limit and no kind is told what's missing.
        return _respond(400, {"code": "action_kind_required",
                              "message": "action_kind is required to create a rule"})

    # ── 4. Limits (G8-D6), on the rule as it will be stored ────────────────────
    kind_after = action_kind if action_kind is not None else (existing or {}).get("action_kind")
    kind_changes = existing is not None and action_kind is not None \
        and action_kind != existing.get("action_kind")
    limit_after = limit if limit is not None else (
        None if kind_changes else (existing or {}).get("limit"))
    if _rule_limit_param(kind_after) is None:
        if limit is not None:
            return _respond(400, {
                "code": "limit_not_supported",
                "message": f"'{kind_after}' has no numeric limit; omit 'limit' from the request.",
            })
        limit_after = None
    elif (existing is not None and enabled_in_body is False and limit is None
          and not kind_changes):
        # A pure disable never needs the bounds: turning a rule off must work even when
        # the parameter can't be read (the evaluator approves nothing then anyway).
        pass
    else:
        if limit_after is None:
            return _respond(400, {
                "code": "limit_required",
                "message": f"a rule for '{kind_after}' needs a numeric 'limit'.",
            })
        try:
            bounds = _load_bounds()
        except Exception:  # noqa: BLE001 — BoundsUnavailable or a client error: fail closed
            log.warning("approval-rules: auto-approval bounds unavailable; nothing written")
            return _respond(503, {
                "code": "auto_approval_bounds_unavailable",
                "message": "the auto-approval bounds couldn't be read; nothing was written.",
            })
        from agents.executors.policy import rule_limit_error  # noqa: PLC0415
        problem = rule_limit_error(limit_after, bounds)
        if problem is not None:
            return _respond(400, {"code": "invalid_limit", "message": problem})
        limit_after = Decimal(str(float(limit_after)))

    if existing is None:
        # ── Creation path ─────────────────────────────────────────────────────
        if action_kind is None:
            # OpenAPI ApprovalRuleRequest requires it; a rule with no kind is
            # meaningless and the evaluator could not match it.
            return _respond(400, {"code": "action_kind_required",
                                  "message": "action_kind is required to create a rule"})
        # D9 / contract § 6: "Creating a rule always stores enabled: false.
        # A create that sets enabled: true is refused 400."
        if enabled_in_body is True:
            return _respond(400, {
                "code": "rules_are_created_disabled",
                "message": (
                    "New rules must be created with enabled=False. "
                    "Enable them with a subsequent PUT once ready (contract § 6)."
                ),
            })

        now = _now_iso()
        item: dict[str, Any] = {
            "fleet_id": fleet_id,
            "rule_id": rule_id,
            "action_kind": action_kind,
            "enabled": False,
            "created_by": sub,
            "updated_at": now,
        }
        if limit_after is not None:
            item["limit"] = limit_after
        # Conditional: a concurrent create (or a create racing an enable) must not
        # overwrite a rule another request wrote between the read above and here.
        try:
            table.put_item(
                Item=item,
                ConditionExpression="attribute_not_exists(fleet_id) AND attribute_not_exists(rule_id)",
            )
        except table.meta.client.exceptions.ConditionalCheckFailedException:
            return _respond(409, {"code": "rule_changed_concurrently",
                                  "message": "the rule was created by another request; retry"})
        return _respond(200, {
            "fleet_id": fleet_id,
            "rule_id": rule_id,
            "action_kind": action_kind,
            "enabled": False,
            "created_by": sub,
            "updated_at": now,
            **({"limit": limit_after} if limit_after is not None else {}),
        })
    else:
        # ── Update path ───────────────────────────────────────────────────────
        updated: dict[str, Any] = dict(existing)
        if kind_changes:
            # G8-D6: a different kind is a different standing approval, so it starts
            # disabled like a create, and can't be enabled in the same request.
            if enabled_in_body is True:
                return _respond(400, {
                    "code": "rules_are_created_disabled",
                    "message": (
                        "Changing a rule's action_kind stores it disabled; enable it with "
                        "a subsequent PUT (contract § 6)."
                    ),
                })
            updated["enabled"] = False
        if action_kind is not None:
            updated["action_kind"] = action_kind
        if enabled_in_body is not None and not kind_changes:
            updated["enabled"] = bool(enabled_in_body)
        if limit_after is not None:
            updated["limit"] = limit_after
        else:
            updated.pop("limit", None)
        updated["updated_at"] = _now_iso()

        # Warning 6 fix (review FG2-lambdas): guard against resurrecting a deleted
        # rule.  A DELETE that races between our get_item and this put_item leaves
        # the rule absent; attribute_exists(rule_id) on the sort key refuses the
        # write and we return 409 instead of recreating an enabled rule.
        #
        # ABA fix (review Cycle 8b Suggestion): if a DELETE + fresh CREATE land
        # between our consistent read and this write, attribute_exists passes
        # (the new rule is present) but the old row's fields — possibly
        # enabled=True with the old created_by — overwrite the new disabled rule.
        # Conditioning on the read updated_at closes the window: the new rule has
        # a different updated_at so the condition expression fails and we return 409.
        _read_at = existing.get("updated_at")
        if _read_at is not None:
            _put_condition = "attribute_exists(rule_id) AND #u = :read_at"
            _put_expr_names: dict = {"#u": "updated_at"}
            _put_expr_values: dict = {":read_at": _read_at}
        else:
            # Fallback for rules that pre-date the updated_at field.
            _put_condition = "attribute_exists(rule_id)"
            _put_expr_names = {}
            _put_expr_values = {}

        try:
            put_kwargs: dict = {"Item": updated, "ConditionExpression": _put_condition}
            if _put_expr_names:
                put_kwargs["ExpressionAttributeNames"] = _put_expr_names
            if _put_expr_values:
                put_kwargs["ExpressionAttributeValues"] = _put_expr_values
            table.put_item(**put_kwargs)
        except table.meta.client.exceptions.ConditionalCheckFailedException:
            return _respond(409, {
                "code": "rule_changed_concurrently",
                "message": "the rule was deleted or changed concurrently; retry",
            })
        return _respond(200, {
            "fleet_id": fleet_id,
            "rule_id": rule_id,
            "action_kind": updated.get("action_kind"),
            "enabled": updated.get("enabled"),
            "created_by": updated.get("created_by"),
            "updated_at": updated.get("updated_at"),
            **({"limit": updated["limit"]} if "limit" in updated else {}),
        })


def _handle_delete_rule(fleet_id: str, rule_id: str) -> dict:
    """DELETE /approval-rules/fleet/{fleet_id}/{rule_id} — remove a rule."""
    table = _get_ddb().Table(_RULES_TABLE)
    table.delete_item(Key={"fleet_id": fleet_id, "rule_id": rule_id})
    return {
        "statusCode": 204,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": os.environ.get("ALLOWED_ORIGIN", "*"),
            "Access-Control-Allow-Headers": "Content-Type,Authorization",
        },
        "body": "",
    }


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def handler(event: dict, context: Any) -> dict:
    """Lambda entry point for ``/approval-rules/*`` routes."""
    method = event.get("httpMethod", "")
    resource = event.get("resource", "")
    log.info("approval-rules: method=%s resource=%s", method, resource)

    # ── Authentication ────────────────────────────────────────────────────────
    claims = _get_claims(event)
    if not claims:
        return _respond(401, {
            "error": "unauthorized",
            "message": "No valid claims in the request context.",
        })

    path_params = event.get("pathParameters") or {}
    fleet_id: str = path_params.get("fleet_id", "")
    rule_id: str = path_params.get("rule_id", "")

    # ── Scope check (contract § 6, spec D9) ───────────────────────────────────
    # Reads custom:fleetIds ONLY through the trust gate in _lib/avx_scope.py.
    # check_fleet_scope: (fleet-operator OR platform-admin) AND trusted membership.
    # No platform-admin bypass of fleet membership (fleet asymmetry).
    if not check_fleet_scope(claims, fleet_id):
        return _respond(403, {
            "code": "insufficient_scope",
            "message": (
                "fleet-operator or platform-admin with a trusted custom:fleetIds "
                "containing this fleet is required (contract § 6)."
            ),
        })

    # ── Route dispatch ────────────────────────────────────────────────────────
    if method == "GET" and resource == "/approval-rules/fleet/{fleet_id}":
        return _handle_get_rules(fleet_id)

    if method == "PUT" and resource == "/approval-rules/fleet/{fleet_id}/{rule_id}":
        raw_body = event.get("body") or "{}"
        try:
            body = json.loads(raw_body)
        except (json.JSONDecodeError, TypeError):
            body = {}
        sub = claims.get("sub", "")
        return _handle_put_rule(fleet_id, rule_id, sub, body)

    if method == "DELETE" and resource == "/approval-rules/fleet/{fleet_id}/{rule_id}":
        return _handle_delete_rule(fleet_id, rule_id)

    return _respond(404, {
        "error": "not_found",
        "message": f"No route for {method} {resource}.",
    })
