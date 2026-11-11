"""fleet_recommendation tool — D12: "Why did you recommend this?"

Spec: `.kiro/specs/2026-10-02-fi-cost-stage-b/spec.md` § D12.
Contract: `docs/fi-action-queue-contract.md` § 3 (queue status), § 7 (browser allowlist).

Design invariants (enforced by test_fleet_recommendation.py):
- **No LLM in the path.** This is a fast DynamoDB read only.
- **No Athena call.** The Tier 2 agent already wrote the Finding; we read it.
- **Fleet standing check mirrors the queue's.** The tool reads the Finding's
  top-level ``fleet_id`` attribute (the storage-only denormalised field,
  contract § 7) and verifies the caller's JWT carries that fleet in
  ``custom:fleetIds`` with (platform-admin OR fleet-operator) group.
  Fleet asymmetry: even platform-admin needs fleet membership (same as
  ``avx_scope.check_fleet_scope``; not duplicated here — the JWT is decoded
  locally without signature re-verification).
- **Browser allowlist only.** The tool strips every server-side attribute
  listed in contract § 7 "Not returned": no ``scope``, no ``finding_key``,
  no ``inputs_hash``, no ``subject_key``, no ``tokens_used``, no
  ``cost_usd``, no ``decided_by.id``, no storage-only top-level vin/fleet_id.
- Table name and region come from environment variables:
    AVX_FINDINGS_TABLE  (default: vsa-{AVX_STAGE}-avx-findings)
    AVX_ACTIONS_TABLE   (default: vsa-{AVX_STAGE}-avx-actions)
    DYNAMODB_REGION     (default: us-west-2)
    AVX_FLEET_IDS_CLAIM_TRUSTED  ("true" to trust custom:fleetIds)
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
from datetime import datetime, timezone
from decimal import Decimal as _Decimal
from typing import Any

import boto3
from strands import tool

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Table name resolution (matches avx_broker.py / executors/ios_owner.py pattern)
# ---------------------------------------------------------------------------

_STAGE = os.environ.get("AVX_STAGE", "staging")
_DEFAULT_FINDINGS_TABLE = f"vsa-{_STAGE}-avx-findings"
_DEFAULT_ACTIONS_TABLE = f"vsa-{_STAGE}-avx-actions"
_DEFAULT_REGION = "us-west-2"

# ---------------------------------------------------------------------------
# Browser-allowlist field sets (contract § 7 "What CMS may return to a browser")
# ---------------------------------------------------------------------------

# Finding top-level fields the browser may see
_FINDING_ALLOWLIST: frozenset[str] = frozenset({
    "finding_id",
    "agent_id",
    "finding_kind",
    "severity",
    "confidence",
    "headline",
    "detail",
    "computed_at",
    "expires_at",
    "recommended_action",
    "location",
    "disclosure_txt",
    "agent_version",
    # subject_key is NOT in the allowlist per "Not returned" clause
})

# evidence[] entry fields the browser may see
_EVIDENCE_ENTRY_ALLOWLIST: frozenset[str] = frozenset({
    "kind",
    "source",
    "value",
    "computed_at",
    "provenance",
    "vin",
})

# Action top-level fields the browser may see
_ACTION_ALLOWLIST: frozenset[str] = frozenset({
    "action_id",
    "finding_id",
    "action_kind",
    "target_system",
    "status",
    "decision",
    # decided_by.kind only — decided_by.id is NOT returned
    "created_at",
    "updated_at",
    "target_ref",
})

# audit_trail[] entry fields the browser may see.
# `target_reason` (contract § 10b) is deliberately absent although § 7 lists it: it is
# another system's text, and this tool's output goes into a model's context. Pinned by
# agents/executors/tests/test_target_reason.py::test_fleet_manager_chat_tool_drops_target_reason.
_AUDIT_ENTRY_ALLOWLIST: frozenset[str] = frozenset({
    "at",
    "from_status",
    "to_status",
    "failure_kind",
    "failure_reason",
    "error_status",
    "guardrail",
})

# ---------------------------------------------------------------------------
# JWT payload decoder (no signature verification — already done by Cognito)
# ---------------------------------------------------------------------------


def _decode_jwt_payload(jwt: str) -> dict[str, Any]:
    """Decode the JWT payload section without verifying the signature.

    The caller's JWT was verified by the Cognito authorizer at session
    establishment.  We only need the claims — not the signature.

    Returns an empty dict on any decode error (fail closed).
    """
    try:
        parts = jwt.split(".")
        if len(parts) != 3:
            return {}
        payload_b64 = parts[1]
        # base64url may lack padding
        rem = len(payload_b64) % 4
        if rem:
            payload_b64 += "=" * (4 - rem)
        return json.loads(base64.urlsafe_b64decode(payload_b64))
    except Exception:  # noqa: BLE001
        return {}


def _extract_groups(claims: dict[str, Any]) -> frozenset[str]:
    """Extract the caller's Cognito group set from JWT claims."""
    raw = claims.get("cognito:groups", [])
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (ValueError, TypeError):
            raw = raw.split()
    return frozenset(raw)


def _extract_fleet_ids(claims: dict[str, Any]) -> frozenset[str]:
    """Extract custom:fleetIds from JWT claims.

    Returns an empty set when AVX_FLEET_IDS_CLAIM_TRUSTED != "true"
    (fail closed — same gate as lambdas/_lib/avx_scope.py).
    """
    if os.environ.get("AVX_FLEET_IDS_CLAIM_TRUSTED", "").strip().lower() != "true":
        return frozenset()
    raw = claims.get("custom:fleetIds", "")
    if not raw:
        return frozenset()
    try:
        ids = json.loads(raw)
        if not isinstance(ids, list):
            return frozenset()
    except (ValueError, TypeError):
        # CSV form (Stage A D13 CSV fleet claims)
        ids = [f.strip() for f in raw.split(",")]
    # Members must carry at least one alphanumeric character (avx_scope rule)
    return frozenset(f for f in ids if f and any(c.isalnum() for c in f))


def check_fleet_standing(jwt: str, fleet_id: str) -> bool:
    """Check fleet standing on *fleet_id* using the caller's JWT.

    Mirrors ``lambdas/_lib/avx_scope.check_fleet_scope`` without importing
    from the lambdas tree.  Fleet asymmetry (spec D12, contract § 4):
    even platform-admin needs fleet membership — unlike dealer/district scope.

    Returns False (fail closed) on any decode error.
    """
    claims = _decode_jwt_payload(jwt)
    groups = _extract_groups(claims)
    if "platform-admin" not in groups and "fleet-operator" not in groups:
        return False
    # Fleet asymmetry: even platform-admin needs membership in custom:fleetIds
    return fleet_id in _extract_fleet_ids(claims)


# ---------------------------------------------------------------------------
# Decision Action id derivation (contract § 4, "One decision per Finding")
# ---------------------------------------------------------------------------


def _derive_decision_action_id(finding_id: str) -> str:
    """Derive the deterministic decision Action id from a Finding id.

    Contract § 4:
        h = sha256("<finding_id>|fi-decision").hexdigest()[:32]
        action_id = "AVX-ACT-" + h[0:8] + "-" + h[8:12] + "-" + h[12:16]
                    + "-" + h[16:20] + "-" + h[20:32]

    Pinned by literal in test_derived_decision_action_id_literal.
    GetItem on this key is covered by the runtime role's existing grant on
    the actions table; a GSI Query on by_finding is not (vsa-api-stack.ts
    grants table-level only, no /index/* resource).
    """
    h = hashlib.sha256(f"{finding_id}|fi-decision".encode()).hexdigest()[:32]
    return f"AVX-ACT-{h[0:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:32]}"


# ---------------------------------------------------------------------------
# Decimal conversion (DynamoDB resource returns Decimal for numeric attributes)
# ---------------------------------------------------------------------------


def _convert_decimals(obj: Any) -> Any:
    """Recursively convert Decimal values to float for JSON serialisation.

    DynamoDB resource returns Decimal for numeric attributes.  strands
    1.39.0's _wrap_tool_result calls json.dumps; Decimal is not JSON-
    serialisable and causes the result to fall back to str(result) — a
    Python repr with single-quoted Decimal literals.  Convert before return.
    """
    if isinstance(obj, _Decimal):
        return float(obj)
    if isinstance(obj, dict):
        return {k: _convert_decimals(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_convert_decimals(v) for v in obj]
    return obj


# ---------------------------------------------------------------------------
# Allowlist filters
# ---------------------------------------------------------------------------


def _filter_finding(finding: dict[str, Any]) -> dict[str, Any]:
    """Return only the browser-allowlisted fields from a Finding (contract § 7)."""
    result: dict[str, Any] = {
        k: v for k, v in finding.items() if k in _FINDING_ALLOWLIST
    }
    # scope.vin → top-level "vin" (contract § 7: "the value of scope.vin (as vin)")
    scope_vin = finding.get("scope", {}).get("vin")
    if scope_vin:
        result["vin"] = scope_vin
    # Filter evidence[] entries to the allowlist
    raw_evidence = finding.get("evidence", [])
    if raw_evidence:
        result["evidence"] = [
            {k: v for k, v in e.items() if k in _EVIDENCE_ENTRY_ALLOWLIST}
            for e in raw_evidence
        ]
    return result


def _filter_action(action: dict[str, Any]) -> dict[str, Any]:
    """Return only the browser-allowlisted fields from an Action (contract § 7)."""
    result: dict[str, Any] = {
        k: v for k, v in action.items() if k in _ACTION_ALLOWLIST
    }
    # decided_by: only the "kind" sub-field — never "id" (a Cognito sub)
    decided_by = action.get("decided_by")
    if isinstance(decided_by, dict) and "kind" in decided_by:
        result["decided_by"] = {"kind": decided_by["kind"]}
    # audit_trail[] entries: strip to allowlist
    raw_audit = action.get("audit_trail", [])
    if raw_audit:
        result["audit_trail"] = [
            {k: v for k, v in e.items() if k in _AUDIT_ENTRY_ALLOWLIST}
            for e in raw_audit
        ]
    # parameters: ONLY on decision Actions (those carrying a "decision" field)
    if "decision" in action and "parameters" in action:
        result["parameters"] = action["parameters"]
    return result


# ---------------------------------------------------------------------------
# Queue status (contract § 3)
# ---------------------------------------------------------------------------


def _utc_now_str() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def compute_queue_status(
    finding: dict[str, Any],
    action: dict[str, Any] | None,
    *,
    utc_now: str | None = None,
) -> str:
    """Derive the queue status per contract § 3.

    ``utc_now`` is injectable for deterministic tests.
    """
    now = utc_now if utc_now is not None else _utc_now_str()

    if action is None:
        expires_at = finding.get("expires_at", "")
        return "pending" if expires_at > now else "expired"

    status = action.get("status", "")
    decision = action.get("decision", "")
    audit_trail = action.get("audit_trail") or []
    last_audit: dict[str, Any] = audit_trail[-1] if audit_trail else {}

    if status == "declined":
        if decision == "declined":
            return "rejected"
        if last_audit.get("guardrail"):
            return "blocked"
        if last_audit.get("failure_kind") == "target_refused":
            return "refused"
    if status == "failed":
        return "failed"
    if status == "cancelled":
        return "cancelled"
    if status in ("completed", "executed"):
        return "executed"
    if status == "pending_execution":
        updated_at = action.get("updated_at", "")
        if updated_at:
            try:
                updated = datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
                current = datetime.fromisoformat(now.replace("Z", "+00:00"))
                if (current - updated).total_seconds() > 900:  # > 15 minutes
                    return "stalled"
            except ValueError:
                pass
        return decision if decision else "approved"
    if status == "in_progress":
        return decision if decision else "approved"

    # No action or unrecognised status — defer to finding expiry
    expires_at = finding.get("expires_at", "")
    return "pending" if expires_at > now else "expired"


# ---------------------------------------------------------------------------
# Safety text (read word for word by the assistant)
# ---------------------------------------------------------------------------

_RECALL_KIND = "fleet.recall.vehicle"
_FLEET_HEALTH_KIND = "fleet.health.service"


def _carries_safety_text(finding: dict[str, Any]) -> bool:
    """True when the Finding's ``disclosure_txt`` must reach the user word for word.

    - Recall items (stage C D18): any ``fleet.recall.vehicle`` Finding.
    - Stop-driving faults: a ``fleet.health.service`` Finding with ``severity == "p0"``,
      booking and grounding items alike (``docs/fi-fleet-health-contract.md`` § 1.5: the
      fault catalog's directive, byte for byte). Items below p0 carry no directive; one
      that somehow does is not narrated as a stop-driving order.

    Only a non-empty string counts. Pinned by test_fleet_recommendation_safety_text.py and
    test_fleet_recommendation_stop_driving.py.
    """
    text = finding.get("disclosure_txt")
    if not isinstance(text, str) or not text:
        return False
    kind = finding.get("finding_kind")
    if kind == _RECALL_KIND:
        return True
    return kind == _FLEET_HEALTH_KIND and finding.get("severity") == "p0"


# ---------------------------------------------------------------------------
# Tool factory
# ---------------------------------------------------------------------------


def make_fleet_recommendation_tool(session: Any, config: Any) -> Any:
    """Factory returning a @tool that reads an FI recommendation (cost, recall, warranty, or fleet health).

    Reads the Finding and its decision Action from AVX's DynamoDB tables,
    checks the caller's fleet standing as the queue does (contract § 4,
    spec D12), and returns only the contract § 7 browser-allowlisted fields.

    For recall Findings (finding_kind == "fleet.recall.vehicle") and stop-driving
    faults (finding_kind == "fleet.health.service" with severity "p0") the result
    also carries a top-level ``safety_text`` key whose value is the Finding's
    ``disclosure_txt`` verbatim — the assistant must read it word for word
    (``_carries_safety_text``).

    No model call. No Athena call. Fast DynamoDB read (p95 < 300 ms).

    Registered on: fleet_manager (spec D12 — "the fleet operator … the fleet
    assistant").  NOT registered on driver/consumer personas: the FI
    recommendation queue is an operator surface.

    Args:
        session: SessionContext with ``jwt`` attribute.
        config:  PersonaLoaderConfig (not used directly; accepted for
                 signature consistency with all other tool factories).
    """
    stage = os.environ.get("AVX_STAGE", "staging")
    findings_table = os.environ.get(
        "AVX_FINDINGS_TABLE", f"vsa-{stage}-avx-findings"
    )
    actions_table = os.environ.get(
        "AVX_ACTIONS_TABLE", f"vsa-{stage}-avx-actions"
    )
    region = os.environ.get("DYNAMODB_REGION", _DEFAULT_REGION)

    @tool
    def fleet_recommendation(finding_id: str) -> dict[str, Any]:
        """Read a fleet FI recommendation (cost, recall, warranty, or fleet health service) and its queue status.

        Answers "why did you recommend this?" for an item in the fleet's FI
        recommendation queue.  Returns the finding's headline, detail, evidence
        (summarised), recommended action, and current queue status.

        For recall items and stop-driving faults the result also includes
        ``safety_text`` — the verbatim safety text from the Finding (a recall's
        safety disclosure, or a stop-driving fault's directive).  Read it to the
        user word for word, first, before anything else.

        Use this when a fleet operator asks about a specific recommendation,
        such as "what's the finding behind that cost recommendation?", "why do
        you suggest we reassign vehicle X?", "why was this recall raised?", or
        "why does this vehicle need service?".
        The finding_id appears on the FI queue screen and in notifications.

        This is a fast read — no model call, no ADP query.

        Args:
            finding_id: The Finding ID from the fleet FI queue (format:
                        FIND-xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx).

        Returns a dict with ``available`` (bool) and, when available:
            ``finding``: headline, detail, evidence, severity, agent_id, ...
            ``queue_status``: one of pending / approved / rejected / executed /
                              blocked / refused / failed / stalled / cancelled / expired
            ``action``: the decision Action (if any), with audit trail
            ``safety_text``: recall items and stop-driving faults only — the
                             Finding's ``disclosure_txt`` verbatim; read this to the
                             user word for word, first, before anything else.
        """
        if not finding_id or not finding_id.strip():
            return {"available": False, "reason": "finding_id is required"}

        try:
            dynamodb = boto3.resource("dynamodb", region_name=region)
            findings_tbl = dynamodb.Table(findings_table)
            actions_tbl = dynamodb.Table(actions_table)
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "fleet_recommendation: DynamoDB client init failed: %s", exc
            )
            return {"available": False, "reason": "recommendation service unavailable"}

        # ── Read the Finding ────────────────────────────────────────────────
        try:
            resp = findings_tbl.get_item(Key={"finding_id": finding_id})
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "fleet_recommendation: GetItem failed finding_id=%s: %s",
                finding_id,
                exc,
            )
            return {"available": False, "reason": "recommendation service unavailable"}

        finding = resp.get("Item")
        if finding is None:
            return {"available": False, "reason": "finding not found"}

        # ── Fleet standing check (spec D12, contract § 4) ───────────────────
        # The Finding's storage-only top-level fleet_id is the gate.
        # A VIN-scoped Finding whose fleet did not resolve has no fleet_id —
        # fail closed (contract § 7 "A VIN-scoped Finding whose fleet did not
        # resolve has no top-level fleet_id, so it is in no fleet's queue").
        fleet_id = finding.get("fleet_id")
        if not fleet_id:
            return {"available": False, "reason": "no fleet access to this finding"}

        if not check_fleet_standing(session.jwt, fleet_id):
            return {"available": False, "reason": "no fleet access to this finding"}

        # ── Read the decision Action via GetItem ─────────────────────────────
        # The decision Action has a deterministic id derived from finding_id
        # (contract § 4, "One decision per Finding").  GetItem on action_id
        # is covered by the runtime role's existing table-level read grant.
        # A GSI Query on by_finding is not — the grant carries no /index/*
        # resource (vsa-api-stack.ts:1995-1996 vs :1775-1776 for Lambdas).
        # Also, Limit=1 on the GSI returns whichever Action the index
        # happens to serve first, which may not be the decision Action.
        decision_action_id = _derive_decision_action_id(finding_id)
        action: dict[str, Any] | None = None
        try:
            action_resp = actions_tbl.get_item(
                Key={"action_id": decision_action_id}
            )
            candidate = action_resp.get("Item")
            # Accept only Actions that carry "decision" (contract § 3, § 4).
            # An informational Finding may have an unrelated non-decision
            # Action in the table; ignore it.
            if candidate is not None and "decision" in candidate:
                action = candidate
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "fleet_recommendation: GetItem failed action_id=%s: %s",
                decision_action_id,
                exc,
            )
            # A failed read must not produce an invented "pending".
            return {"available": False, "reason": "recommendation service unavailable"}

        queue_status = compute_queue_status(finding, action)

        result: dict[str, Any] = {
            "available": True,
            "finding": _filter_finding(finding),
            "queue_status": queue_status,
        }
        if action is not None:
            result["action"] = _filter_action(action)

        result = _convert_decimals(result)

        # Safety text the assistant reads word for word — byte for byte, after
        # Decimal conversion (disclosure_txt is a string; no strip, no normalise).
        # It duplicates the already-allowlisted finding.disclosure_txt at the top level, so
        # it exposes no new field. Its source is server-side only: a recall's disclosure
        # (fi_recall_warranty writer) or the fault catalog's directive copied byte for byte
        # by fi_fleet_health/writer.build_disclosure_txt from ADP fault_codes; no API route
        # writes it. Whoever changes either writer changes what is narrated verbatim here.
        if _carries_safety_text(finding):
            result["safety_text"] = finding["disclosure_txt"]

        return result

    return fleet_recommendation
