"""Attempt rules for recall findings (spec D12 / contract § 4).

Constants are module-level so callers can override them in tests.
"""
from __future__ import annotations

import datetime
import hashlib
from typing import Any, Optional

from boto3.dynamodb.types import TypeDeserializer

_DESERIALIZER = TypeDeserializer()

# D12 configuration constants
REJECTED_SAFETY_DAYS: int = 14
REJECTED_OTHER_DAYS: int = 30
MAX_ATTEMPTS: int = 5

# Decisions that trigger an immediate retry (vehicle still unremedied)
_IMMEDIATE_RETRY = frozenset({"cancelled", "failed", "refused"})

# Decisions that never trigger a retry
_NO_RETRY = frozenset({"stalled", "blocked", "executed", "completed"})

_INFORMATIONAL_HEADLINE = "Bookings keep failing — manual follow-up needed."


def should_retry(
    terminal_decision: str,
    recall_class: str,
    days_since_decision: int,
    attempt_number: int = 0,
    *,
    _max_attempts: int = MAX_ATTEMPTS,
    _safety_days: int = REJECTED_SAFETY_DAYS,
    _other_days: int = REJECTED_OTHER_DAYS,
) -> bool:
    """Return True if a next attempt should be written for this vehicle/campaign/action.

    Parameters
    ----------
    terminal_decision:   One of: cancelled, failed, refused, rejected, stalled,
                         blocked, executed, completed.
    recall_class:        "safety" or "non_safety" — used for rejected window.
    days_since_decision: Days elapsed since the decision was made.
    attempt_number:      The current (just-resolved) attempt number; 0 = unknown.
    """
    # Cap check: if we've already reached MAX_ATTEMPTS, no more.
    if attempt_number >= _max_attempts:
        return False

    if terminal_decision in _IMMEDIATE_RETRY:
        return True

    if terminal_decision == "rejected":
        threshold = _safety_days if recall_class == "safety" else _other_days
        return days_since_decision >= threshold

    # stalled, blocked, executed, completed — and any unknown status
    return False


def build_next_subject_key(
    campaign_id: str,
    action: str,
    current_attempt: int,
) -> str:
    """Return the subject_key for the next attempt.

    Form: ``<campaign_id>/<action>/<n+1>``  (contract § 1.2).
    """
    return f"{campaign_id}/{action}/{current_attempt + 1}"


def build_max_attempts_item(
    vin: str,
    campaign_id: str,
    action: str,
) -> dict:
    """Return the informational item written after MAX_ATTEMPTS are exhausted.

    The item carries no ``recommended_action`` (D12).
    """
    return {
        "vin": vin,
        "campaign_id": campaign_id,
        "action": action,
        "headline": _INFORMATIONAL_HEADLINE,
        # No recommended_action — informational only.
    }


# ---------------------------------------------------------------------------
# fi_decision_id  (contract § 4 / handler._fi_decision_id mirror)
# ---------------------------------------------------------------------------

def fi_decision_id(finding_id: str) -> str:
    """Deterministic per-Finding decision id for FI queue items (contract § 4).

    sha256("<finding_id>|fi-decision").hexdigest()[:32], UUID-formatted as
    AVX-ACT-{uuid4-like}.  Pinned by literal in
    lambdas/api-avx-findings/tests/test_decline_override.py — changing this
    formula drifts that pin.  The test in this module cross-checks the pair.

    Pinned pair (from test_decline_override.py):
      finding_id  = "FIND-decline-00000000-0000-0000-0000-000000000001"
      decision_id = "AVX-ACT-2e9e1581-571a-8940-5628-be69b348a2bf"
    """
    h = hashlib.sha256(f"{finding_id}|fi-decision".encode()).hexdigest()[:32]
    return f"AVX-ACT-{h[0:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:32]}"


# ---------------------------------------------------------------------------
# Queue-status helpers
# ---------------------------------------------------------------------------

# Agent and kind constants (matches writer.py)
_AGENT_ID = "agent-fi-recall-warranty"
_FINDING_KIND = "fleet.recall.vehicle"
_STALL_MINUTES = 15


def _last_audit_at(audit_trail: list, to_status: str) -> Optional[str]:
    """Return the ``at`` of the last audit entry with the given to_status."""
    for entry in reversed(audit_trail):
        if entry.get("to_status") == to_status:
            return entry.get("at")
    return None


def _map_action_to_queue_status(
    action: Optional[dict], now: datetime.datetime
) -> tuple[str, Optional[str]]:
    """Map an Action dict → (queue_status, decided_at).

    Returns ("none", None) when action is None (no decision recorded).
    """
    if action is None:
        return "none", None

    status = action.get("status", "")
    audit = action.get("audit_trail") or []

    if status == "pending_execution":
        updated = action.get("updated_at") or action.get("created_at", "")
        if updated:
            dt = datetime.datetime.fromisoformat(updated.replace("Z", "+00:00"))
            if (now - dt) > datetime.timedelta(minutes=_STALL_MINUTES):
                return "stalled", None
        return "pending", None

    if status == "cancelled":
        return "cancelled", _last_audit_at(audit, "cancelled")
    if status == "failed":
        return "failed", _last_audit_at(audit, "failed")
    if status in ("executed", "completed"):
        return status, _last_audit_at(audit, status)
    if status == "in_progress":
        # DMS accepted the booking (cycle 2 agent C2): never rewrite or re-raise it.
        return "in_progress", None
    if status == "dismissed":
        return "dismissed", None

    if status == "declined":
        decided_at = _last_audit_at(audit, "declined")
        # Order matters: guardrail > rejected (decision=declined) > refused
        if any(e.get("guardrail") for e in audit):
            return "blocked", decided_at
        if action.get("decision") == "declined":
            # Operator decline: audit_trail is [] (route writes no entries).
            # Fall back to updated_at / created_at (C2 fix).
            if decided_at is None:
                decided_at = action.get("updated_at") or action.get("created_at")
            return "rejected", decided_at
        return "refused", decided_at

    # An Action exists in a status this table doesn't know: never treat it as "no Action".
    return "unknown", None


# ---------------------------------------------------------------------------
# read_vin_findings / attempt_history_from_items / read_attempt_history
# ---------------------------------------------------------------------------

def read_vin_findings(
    vin: str,
    *,
    ddb_client: Any,
    findings_table: str,
) -> list[dict]:
    """Query the by_vin GSI for *vin* and return all deserialized items (all pages).

    One paged query serves both attempt-history and open-items consumers (D53).
    """
    items: list[dict] = []
    kwargs: dict[str, Any] = {
        "TableName": findings_table,
        "IndexName": "by_vin",
        "KeyConditionExpression": "vin = :v",
        "ExpressionAttributeValues": {":v": {"S": vin}},
    }
    while True:
        response = ddb_client.query(**kwargs)
        for raw in response.get("Items", []):
            items.append({k: _DESERIALIZER.deserialize(v) for k, v in raw.items()})
        last = response.get("LastEvaluatedKey")
        if not last:
            break
        kwargs["ExclusiveStartKey"] = last
    return items


def attempt_history_from_items(
    items: list[dict],
    *,
    now: datetime.datetime,
    ddb_client: Any,
    actions_table: str,
) -> tuple[dict, int]:
    """Compute per-(campaign, action) history from pre-fetched items.

    Filters to agent-fi-recall-warranty recall findings, keeps the highest
    attempt per (campaign, action), resolves each Action via GetItem.

    Returns (history, skip_count) with the same shape as read_attempt_history.
    """
    best: dict[tuple, dict] = {}
    skip_count = 0
    for item in items:
        if item.get("agent_id") != _AGENT_ID:
            continue
        if item.get("finding_kind") != _FINDING_KIND:
            continue
        parts = (item.get("subject_key") or "").split("/")
        if len(parts) != 3:
            skip_count += 1
            continue
        campaign_id, action, n_str = parts
        try:
            attempt_n = int(n_str)
        except ValueError:
            skip_count += 1
            continue
        key = (campaign_id, action)
        existing = best.get(key)
        newer = (
            existing is None
            or attempt_n > existing["attempt_number"]
            or (attempt_n == existing["attempt_number"]
                and item.get("computed_at", "") > existing.get("computed_at", ""))
        )
        if newer:
            best[key] = {
                "attempt_number": attempt_n,
                "finding_id": item.get("finding_id", ""),
                "inputs_hash": item.get("inputs_hash", ""),
                "expires_at": item.get("expires_at", ""),
                "computed_at": item.get("computed_at", ""),
                "diagnosis_source": _extract_diagnosis_source(item),
            }

    history: dict[tuple, dict] = {}
    for key, entry in best.items():
        action_id = fi_decision_id(entry["finding_id"])
        resp = ddb_client.get_item(
            TableName=actions_table,
            Key={"action_id": {"S": action_id}},
        )
        raw_action = resp.get("Item")
        action_obj: Optional[dict] = None
        if raw_action:
            action_obj = {k: _DESERIALIZER.deserialize(v) for k, v in raw_action.items()}
        queue_status, decided_at = _map_action_to_queue_status(action_obj, now)
        history[key] = {**entry, "queue_status": queue_status, "decided_at": decided_at}

    return history, skip_count


def read_attempt_history(
    vin: str,
    *,
    ddb_client: Any,
    findings_table: str,
    actions_table: str,
) -> tuple[dict, int]:
    """Return per-(campaign, action) highest-attempt history for *vin*.

    Wrapper over read_vin_findings + attempt_history_from_items (D53).
    Signature and results unchanged — Group 3 tests stay green.

    Returns
    -------
    (history, skip_count)
        history    : dict[(campaign_id, action)] → {
                       "attempt_number", "finding_id", "inputs_hash",
                       "expires_at", "diagnosis_source", "queue_status",
                       "decided_at" }
        skip_count : number of findings with unparseable subject_key
    """
    now = datetime.datetime.now(datetime.timezone.utc)
    items = read_vin_findings(vin, ddb_client=ddb_client, findings_table=findings_table)
    return attempt_history_from_items(
        items, now=now, ddb_client=ddb_client, actions_table=actions_table
    )


def _extract_diagnosis_source(finding: dict) -> Optional[str]:
    """Return the diagnosis_source string from a Finding's evidence list."""
    for entry in finding.get("evidence") or []:
        # Schema format: {kind: "recall.diagnosis_source", value: "model"|"rules"}
        if entry.get("kind") == "recall.diagnosis_source":
            return entry.get("value")
        # Domain-key format (used in tests): {"recall.diagnosis_source": "value"}
        if "recall.diagnosis_source" in entry:
            return entry["recall.diagnosis_source"]
    return None


def open_items_from_items(
    items: list[dict],
    *,
    now_iso: str,
) -> list[dict]:
    """Return unexpired non-warranty-candidate open items from *items* (D53).

    Parameters
    ----------
    items    : deserialized Finding dicts for one VIN (from read_vin_findings).
    now_iso  : ISO-8601 UTC string; items with expires_at <= now_iso excluded.

    Each returned dict: {finding_id, finding_kind, headline, campaign_id}.
    campaign_id is the subject_key's first segment for fleet.recall.vehicle
    items, None otherwise.  fleet.warranty.claim_candidate items excluded.
    """
    result: list[dict] = []
    for item in items:
        kind = item.get("finding_kind", "")
        if kind == "fleet.warranty.claim_candidate":
            continue
        if (item.get("expires_at") or "") <= now_iso:
            continue
        cid: Optional[str] = None
        if kind == "fleet.recall.vehicle":
            parts = (item.get("subject_key") or "").split("/")
            if parts:
                cid = parts[0]
        result.append({
            "finding_id": item.get("finding_id", ""),
            "finding_kind": kind,
            "headline": item.get("headline", ""),
            "campaign_id": cid,
        })
    return result


#: Finding kinds whose Findings expire within a day — excluded from
#: :func:`bundle_for_row` so the recall model and ``inputs_hash`` both skip them.
#:
#: Add a kind here iff its ``ttl_seconds`` (as passed to
#: ``agents.tier2.finding_transform.health_artifact_to_finding`` or wherever the
#: writer sets ``expires_at``) is at most 24 hours (user decision, 2026-10-13;
#: issue "item 3 diagnosed: inputs_hash includes volatile sibling finding_ids").
#:
#: Keeping volatile siblings in the bundle would churn ``inputs_hash`` between
#: consecutive recall runs — ``cms_dtc_source`` rewrites ``health.diagnostics``
#: every five minutes with a 30-minute TTL (``DIAGNOSTICS_TTL_SECONDS``), so a
#: fault still active between a 05:00Z scheduled recall and an 11:00Z re-run
#: refreshes ~78 times, each refresh a new ``finding_id``.  Excluding them keeps
#: the model view narrower (siblings it never acts on) AND makes an unchanged
#: re-run re-check cost zero model calls, as spec Accept 3 requires.
_VOLATILE_FINDING_KINDS: frozenset[str] = frozenset({"health.diagnostics"})


def bundle_for_row(open_items: list[dict], campaign_id: str) -> list[dict]:
    """Drop this campaign's own recall items and volatile sibling kinds from the
    open-items bundle (D53; user decision 2026-10-13 option B).

    Dropped:

    * This campaign's own ``fleet.recall.vehicle`` entries (D53).
    * Any entry whose ``finding_kind`` is in :data:`_VOLATILE_FINDING_KINDS`
      (user, 2026-10-13).  Those Findings churn faster than the recall cadence;
      including them would (a) feed the model siblings it never acts on and
      (b) rewrite ``inputs_hash`` between runs, re-firing the model path on
      otherwise-unchanged vehicles (item 3).

    Other recall campaigns, stable health Findings, and other stable kinds are
    kept — a genuinely new recall campaign or warranty candidate still shifts
    the bundle and the hash, so the model path still fires when the vehicle's
    world really changed.
    """
    return [
        it for it in open_items
        if it["finding_kind"] not in _VOLATILE_FINDING_KINDS
        and not (it["finding_kind"] == "fleet.recall.vehicle"
                 and it["campaign_id"] == campaign_id)
    ]


# ---------------------------------------------------------------------------
# plan_attempt
# ---------------------------------------------------------------------------

def plan_attempt(
    history_entry: Optional[dict],
    recall_class: str,
    now: datetime.datetime,
) -> str:
    """Decide what to write for the next daily run.

    Parameters
    ----------
    history_entry : None or a dict from read_attempt_history's returned history.
    recall_class  : "safety" or "non_safety".
    now           : UTC datetime (for rejected-window calculation).

    Returns
    -------
    One of: "write_first", "write_next", "write_exhausted", "skip".
    """
    if history_entry is None:
        return "write_first"

    qs = history_entry["queue_status"]
    attempt_n = history_entry["attempt_number"]

    # Undecided or in-queue: rewrite the current attempt (C1 fix).
    if qs in ("none", "pending"):
        return "write_current"

    # Statuses that never trigger a retry.
    if qs in ("executed", "completed", "in_progress", "dismissed", "unknown", "stalled", "blocked"):
        return "skip"

    # For rejected: compute days since decision.
    days_since = 0
    if qs == "rejected":
        decided_at = history_entry.get("decided_at")
        if not decided_at:
            return "skip"
        decided_dt = datetime.datetime.fromisoformat(decided_at.replace("Z", "+00:00"))
        days_since = (now - decided_dt).days

    # Always pass attempt_number to should_retry (task requirement).
    if should_retry(qs, recall_class, days_since, attempt_n):
        return "write_next"

    # should_retry returned False. If the cap (MAX_ATTEMPTS) is the only reason,
    # write the exhausted item. Otherwise the status/timing was ineligible → skip.
    if attempt_n >= MAX_ATTEMPTS:
        threshold = (
            REJECTED_SAFETY_DAYS if recall_class == "safety" else REJECTED_OTHER_DAYS
        )
        if qs in _IMMEDIATE_RETRY or (qs == "rejected" and days_since >= threshold):
            return "write_exhausted"

    return "skip"
