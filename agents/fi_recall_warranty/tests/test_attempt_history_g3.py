"""Tests for attempts.py  — Task 3.1 G1 (attempt history).

Coverage
--------
1. fi_decision_id: pinned literal from test_decline_override.py.
2. read_attempt_history: two findings pages, all pages consumed.
3. read_attempt_history: each queue_status row from contract § 4 table.
4. read_attempt_history: foreign agent rows silently ignored (not in history).
5. plan_attempt: 5th attempt cancelled → write_exhausted.
6. plan_attempt: rejected safety — 13 days → skip; 14 days → write_next.
7. plan_attempt: stalled — 14 min → pending; 16 min → stalled (both → skip).

All DDB interactions use botocore.Stubber on a low-level client.
No real AWS calls; dead-cred env is fine.
"""
from __future__ import annotations

import datetime
from typing import Any

import boto3
import pytest
from boto3.dynamodb.types import TypeSerializer
from botocore.stub import Stubber

_SER = TypeSerializer()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ddb() -> Any:
    return boto3.client("dynamodb", region_name="us-west-2")


def _s(val: Any) -> Any:
    """Serialize a Python value to DDB AttributeValue format."""
    return _SER.serialize(val)


def _item(d: dict) -> dict:
    """Serialize every key of d to DDB AttributeValue."""
    return {k: _s(v) for k, v in d.items()}


def _finding(
    finding_id: str,
    vin: str,
    subject_key: str,
    inputs_hash: str = "a" * 64,
    expires_at: str = "2099-01-01T00:00:00Z",
    agent_id: str = "agent-fi-recall-warranty",
    finding_kind: str = "fleet.recall.vehicle",
    evidence: list | None = None,
) -> dict:
    """Return a minimal DDB-format Finding item."""
    if evidence is None:
        evidence = [
            {"vin": vin, "provenance": "live",
             "recall.diagnosis_source": "model", "diagnosis_source": "model"}
        ]
    return _item({
        "finding_id": finding_id,
        "vin": vin,
        "agent_id": agent_id,
        "finding_kind": finding_kind,
        "subject_key": subject_key,
        "inputs_hash": inputs_hash,
        "expires_at": expires_at,
        "evidence": evidence,
    })


def _action(
    action_id: str,
    finding_id: str,
    status: str,
    updated_at: str = "2026-10-01T00:00:00Z",
    decision: str | None = None,
    audit_trail: list | None = None,
) -> dict:
    """Return a minimal DDB-format Action item."""
    row: dict = {
        "action_id": action_id,
        "finding_id": finding_id,
        "status": status,
        "updated_at": updated_at,
        "audit_trail": audit_trail or [],
    }
    if decision is not None:
        row["decision"] = decision
    return _item(row)


def _declined_audit(
    at: str,
    *,
    failure_kind: str = "target_refused",
    guardrail: str | None = None,
) -> dict:
    entry: dict = {
        "at": at, "by": "system",
        "from_status": "pending_execution", "to_status": "declined",
        "failure_kind": failure_kind,
    }
    if guardrail:
        entry["guardrail"] = guardrail
    return entry


_VIN = "MRDN0000000000001"
_TBL_F = "vsa-staging-avx-findings"
_TBL_A = "vsa-staging-avx-actions"

# ---------------------------------------------------------------------------
# 1. fi_decision_id — pinned literal
# ---------------------------------------------------------------------------

class TestFiDecisionIdPinned:
    # Pinned pair from lambdas/api-avx-findings/tests/test_decline_override.py
    _PINNED_FID = "FIND-decline-00000000-0000-0000-0000-000000000001"
    _PINNED_DID = "AVX-ACT-2e9e1581-571a-8940-5628-be69b348a2bf"

    def test_pinned_literal_matches(self):
        """fi_decision_id must produce the exact pinned value from test_decline_override.py.

        M-a mutation: changing the hash formula (e.g., dropping '|fi-decision')
        produces a different hex string → this assertion fails immediately.
        """
        from agents.fi_recall_warranty.attempts import fi_decision_id

        got = fi_decision_id(self._PINNED_FID)
        assert got == self._PINNED_DID, (
            f"fi_decision_id formula drifted from test_decline_override.py pin.\n"
            f"  Expected: {self._PINNED_DID}\n"
            f"  Got:      {got}"
        )


# ---------------------------------------------------------------------------
# 2. read_attempt_history: two findings pages (all pages consumed)
# ---------------------------------------------------------------------------

class TestReadAttemptHistoryTwoPages:
    def test_second_page_findings_are_included(self):
        """Both pages of the by_vin query must be read; page-2 findings appear in history.

        M-b mutation: stopping after the first page omits findings from page 2
        → the (campaign_b, book) key is absent from the returned history.
        """
        from agents.fi_recall_warranty.attempts import fi_decision_id, read_attempt_history

        ddb = _ddb()
        fid_a = "FIND-page1-00000000-0000-0000-0000-000000000001"
        fid_b = "FIND-page2-00000000-0000-0000-0000-000000000001"
        act_a = fi_decision_id(fid_a)
        act_b = fi_decision_id(fid_b)

        page1_last_key = {"finding_id": {"S": fid_a}, "vin": {"S": _VIN}}

        with Stubber(ddb) as stub:
            # Page 1 — campaign_a
            stub.add_response(
                method="query",
                service_response={
                    "Items": [_finding(fid_a, _VIN, "MRD-2026-001/book/1")],
                    "LastEvaluatedKey": page1_last_key,
                },
                expected_params={
                    "TableName": _TBL_F,
                    "IndexName": "by_vin",
                    "KeyConditionExpression": "vin = :v",
                    "ExpressionAttributeValues": {":v": {"S": _VIN}},
                },
            )
            # Page 2 — campaign_b
            stub.add_response(
                method="query",
                service_response={
                    "Items": [_finding(fid_b, _VIN, "MRD-2026-002/book/1")],
                },
                expected_params={
                    "TableName": _TBL_F,
                    "IndexName": "by_vin",
                    "KeyConditionExpression": "vin = :v",
                    "ExpressionAttributeValues": {":v": {"S": _VIN}},
                    "ExclusiveStartKey": page1_last_key,
                },
            )
            # Actions for both findings — no item (no decision yet)
            stub.add_response(
                method="get_item",
                service_response={},
                expected_params={"TableName": _TBL_A, "Key": {"action_id": {"S": act_a}}},
            )
            stub.add_response(
                method="get_item",
                service_response={},
                expected_params={"TableName": _TBL_A, "Key": {"action_id": {"S": act_b}}},
            )
            history, skip = read_attempt_history(
                _VIN, ddb_client=ddb, findings_table=_TBL_F, actions_table=_TBL_A
            )

        assert ("MRD-2026-001", "book") in history, "Page-1 finding must appear in history"
        assert ("MRD-2026-002", "book") in history, (
            "Page-2 finding must appear in history. "
            "M-b: stopping after page 1 leaves ('MRD-2026-002', 'book') absent."
        )
        assert skip == 0


# ---------------------------------------------------------------------------
# 3. Each queue_status row of the contract § 4 table
# ---------------------------------------------------------------------------

_NOW = datetime.datetime(2026, 10, 10, 12, 0, 0, tzinfo=datetime.timezone.utc)
_DECIDED = "2026-10-01T00:00:00Z"  # 9 days before _NOW


def _history_for_action(action_item: dict | None) -> tuple[dict, int]:
    """Run read_attempt_history with one finding and one action (or None)."""
    from agents.fi_recall_warranty.attempts import fi_decision_id, read_attempt_history

    fid = "FIND-status-00000000-0000-0000-0000-000000000001"
    act_id = fi_decision_id(fid)
    ddb = _ddb()
    with Stubber(ddb) as stub:
        stub.add_response(
            method="query",
            service_response={"Items": [_finding(fid, _VIN, "MRD-2026-001/book/1")]},
            expected_params={
                "TableName": _TBL_F, "IndexName": "by_vin",
                "KeyConditionExpression": "vin = :v",
                "ExpressionAttributeValues": {":v": {"S": _VIN}},
            },
        )
        if action_item is None:
            stub.add_response(
                method="get_item", service_response={},
                expected_params={"TableName": _TBL_A, "Key": {"action_id": {"S": act_id}}},
            )
        else:
            stub.add_response(
                method="get_item",
                service_response={"Item": action_item},
                expected_params={"TableName": _TBL_A, "Key": {"action_id": {"S": act_id}}},
            )
        # Inject a frozen _now by monkeypatching datetime inside the module.
        import agents.fi_recall_warranty.attempts as mod
        orig = mod.datetime.datetime
        class _FrozenNow(orig):
            @classmethod
            def now(cls, tz=None):
                return _NOW
        mod.datetime.datetime = _FrozenNow
        try:
            h, s = read_attempt_history(
                _VIN, ddb_client=ddb, findings_table=_TBL_F, actions_table=_TBL_A
            )
        finally:
            mod.datetime.datetime = orig
    return h, s


class TestQueueStatusMapping:
    def test_none_when_no_action(self):
        h, _ = _history_for_action(None)
        assert h[("MRD-2026-001", "book")]["queue_status"] == "none"

    def test_cancelled(self):
        fid = "FIND-status-00000000-0000-0000-0000-000000000001"
        from agents.fi_recall_warranty.attempts import fi_decision_id
        act_id = fi_decision_id(fid)
        a = _action(act_id, fid, "cancelled",
                    audit_trail=[{
                        "at": _DECIDED, "by": "dms",
                        "from_status": "pending_execution", "to_status": "cancelled",
                    }])
        h, _ = _history_for_action(a)
        assert h[("MRD-2026-001", "book")]["queue_status"] == "cancelled"
        assert h[("MRD-2026-001", "book")]["decided_at"] == _DECIDED

    def test_failed(self):
        fid = "FIND-status-00000000-0000-0000-0000-000000000001"
        from agents.fi_recall_warranty.attempts import fi_decision_id
        act_id = fi_decision_id(fid)
        a = _action(act_id, fid, "failed",
                    audit_trail=[{
                        "at": _DECIDED, "by": "executor",
                        "from_status": "pending_execution", "to_status": "failed",
                        "failure_kind": "executor_terminal_error", "retry_count": 3,
                    }])
        h, _ = _history_for_action(a)
        assert h[("MRD-2026-001", "book")]["queue_status"] == "failed"

    def test_refused_declined_no_guardrail(self):
        """declined + failure_kind=target_refused + no guardrail → refused.

        M-b mutation: mapping both declined+target_refused and declined+decision=declined
        to 'rejected' would give 'rejected' here → assertion fails.
        """
        fid = "FIND-status-00000000-0000-0000-0000-000000000001"
        from agents.fi_recall_warranty.attempts import fi_decision_id
        act_id = fi_decision_id(fid)
        a = _action(act_id, fid, "declined",
                    audit_trail=[_declined_audit(_DECIDED)])
        h, _ = _history_for_action(a)
        assert h[("MRD-2026-001", "book")]["queue_status"] == "refused", (
            "declined + target_refused (no guardrail) must map to 'refused', not 'rejected'. "
            "M-b: collapsing refused/rejected to 'rejected' fails here."
        )

    def test_rejected_operator_declined(self):
        fid = "FIND-status-00000000-0000-0000-0000-000000000001"
        from agents.fi_recall_warranty.attempts import fi_decision_id
        act_id = fi_decision_id(fid)
        a = _action(act_id, fid, "declined", decision="declined",
                    audit_trail=[_declined_audit(_DECIDED)])
        h, _ = _history_for_action(a)
        assert h[("MRD-2026-001", "book")]["queue_status"] == "rejected"

    def test_blocked_guardrail(self):
        fid = "FIND-status-00000000-0000-0000-0000-000000000001"
        from agents.fi_recall_warranty.attempts import fi_decision_id
        act_id = fi_decision_id(fid)
        a = _action(act_id, fid, "declined",
                    audit_trail=[_declined_audit(_DECIDED, guardrail="G1")])
        h, _ = _history_for_action(a)
        assert h[("MRD-2026-001", "book")]["queue_status"] == "blocked"

    def test_pending_under_15_min(self):
        """pending_execution created 14 min ago → queue_status = pending."""
        fid = "FIND-status-00000000-0000-0000-0000-000000000001"
        from agents.fi_recall_warranty.attempts import fi_decision_id
        act_id = fi_decision_id(fid)
        updated = (_NOW - datetime.timedelta(minutes=14)).isoformat().replace("+00:00", "Z")
        a = _action(act_id, fid, "pending_execution", updated_at=updated)
        h, _ = _history_for_action(a)
        assert h[("MRD-2026-001", "book")]["queue_status"] == "pending"

    def test_stalled_over_15_min(self):
        """pending_execution created 16 min ago → queue_status = stalled.

        M-b mutation: mapping pending_execution > 15 min to 'pending' fails here.
        """
        fid = "FIND-status-00000000-0000-0000-0000-000000000001"
        from agents.fi_recall_warranty.attempts import fi_decision_id
        act_id = fi_decision_id(fid)
        updated = (_NOW - datetime.timedelta(minutes=16)).isoformat().replace("+00:00", "Z")
        a = _action(act_id, fid, "pending_execution", updated_at=updated)
        h, _ = _history_for_action(a)
        assert h[("MRD-2026-001", "book")]["queue_status"] == "stalled", (
            "pending_execution > 15 min must map to 'stalled'. "
            "M-b mutation: using 'pending' for all pending_execution fails here."
        )

    def test_executed(self):
        fid = "FIND-status-00000000-0000-0000-0000-000000000001"
        from agents.fi_recall_warranty.attempts import fi_decision_id
        act_id = fi_decision_id(fid)
        a = _action(act_id, fid, "executed",
                    audit_trail=[{
                        "at": _DECIDED, "by": "executor",
                        "from_status": "pending_execution", "to_status": "executed",
                    }])
        h, _ = _history_for_action(a)
        assert h[("MRD-2026-001", "book")]["queue_status"] == "executed"

    def test_completed(self):
        fid = "FIND-status-00000000-0000-0000-0000-000000000001"
        from agents.fi_recall_warranty.attempts import fi_decision_id
        act_id = fi_decision_id(fid)
        a = _action(act_id, fid, "completed",
                    audit_trail=[{
                        "at": _DECIDED, "by": "dms",
                        "from_status": "executed", "to_status": "completed",
                    }])
        h, _ = _history_for_action(a)
        assert h[("MRD-2026-001", "book")]["queue_status"] == "completed"


# ---------------------------------------------------------------------------
# 4. Foreign agent rows ignored
# ---------------------------------------------------------------------------

class TestForeignAgentRowsIgnored:
    def test_different_agent_id_not_in_history(self):
        """Findings from another agent must be silently filtered (not in history, not skip_count).

        M-d mutation: counting foreign-agent rows causes them to land in history
        or increment skip_count → these assertions catch both.
        """
        from agents.fi_recall_warranty.attempts import read_attempt_history

        fid_foreign = "FIND-foreign-00000000-0000-0000-0000-000000000001"
        ddb = _ddb()
        with Stubber(ddb) as stub:
            stub.add_response(
                method="query",
                service_response={
                    "Items": [
                        _finding(fid_foreign, _VIN, "MRD-2026-001/book/1",
                                 agent_id="agent-2-vehicle-health"),
                    ],
                },
                expected_params={
                    "TableName": _TBL_F, "IndexName": "by_vin",
                    "KeyConditionExpression": "vin = :v",
                    "ExpressionAttributeValues": {":v": {"S": _VIN}},
                },
            )
            h, skip = read_attempt_history(
                _VIN, ddb_client=ddb, findings_table=_TBL_F, actions_table=_TBL_A
            )

        assert len(h) == 0, (
            "Foreign-agent findings must not appear in history. "
            "M-d: including them yields len(h) > 0."
        )
        assert skip == 0, (
            "Foreign-agent rows must not increment skip_count (they are not unparseable)."
        )


# ---------------------------------------------------------------------------
# 5. plan_attempt: 5th attempt cancelled → write_exhausted
# ---------------------------------------------------------------------------

class TestPlanAttemptFifthCancelledExhausted:
    def test_5th_cancelled_gives_exhausted(self):
        """5th attempt with queue_status=cancelled must return write_exhausted.

        M-c mutation: not passing attempt_number to should_retry causes should_retry
        to use the default (0), which is < MAX_ATTEMPTS, so it returns True and the
        function returns 'write_next' instead of 'write_exhausted'.
        """
        from agents.fi_recall_warranty.attempts import MAX_ATTEMPTS, plan_attempt

        entry = {
            "attempt_number": MAX_ATTEMPTS,  # 5
            "finding_id": "FIND-0001",
            "inputs_hash": "a" * 64,
            "expires_at": "2099-01-01T00:00:00Z",
            "diagnosis_source": "model",
            "queue_status": "cancelled",
            "decided_at": "2026-10-01T00:00:00Z",
        }
        result = plan_attempt(entry, "safety", _NOW)
        assert result == "write_exhausted", (
            f"5th attempt cancelled must produce 'write_exhausted', got {result!r}. "
            "M-c: omitting attempt_number from should_retry uses default 0 → "
            "should_retry returns True → returns 'write_next' instead."
        )


# ---------------------------------------------------------------------------
# 6. plan_attempt: rejected safety — 13 days → skip; 14 days → write_next
# ---------------------------------------------------------------------------

class TestPlanAttemptRejectedSafety:
    _RECALL_CLASS = "safety"

    def _entry(self, days_ago: int) -> dict:
        decided_dt = _NOW - datetime.timedelta(days=days_ago)
        return {
            "attempt_number": 1,
            "finding_id": "FIND-0001",
            "inputs_hash": "a" * 64,
            "expires_at": "2099-01-01T00:00:00Z",
            "diagnosis_source": "model",
            "queue_status": "rejected",
            "decided_at": decided_dt.isoformat().replace("+00:00", "Z"),
        }

    def test_13_days_skip(self):
        """Rejected safety recall at 13 days must skip (threshold is 14).

        M-b mutation: mapping 'declined+target_refused' to 'rejected' would put
        non-operator-declined actions through this window; that is a separate
        concern.  This test pins the 14-day threshold for safety.
        """
        from agents.fi_recall_warranty.attempts import plan_attempt

        result = plan_attempt(self._entry(13), self._RECALL_CLASS, _NOW)
        assert result == "skip", (
            f"13 days after rejection (safety) must skip; got {result!r}. "
            "Threshold is 14 days for safety recalls."
        )

    def test_14_days_write_next(self):
        """Rejected safety recall at 14 days must write_next (threshold met)."""
        from agents.fi_recall_warranty.attempts import plan_attempt

        result = plan_attempt(self._entry(14), self._RECALL_CLASS, _NOW)
        assert result == "write_next", (
            f"14 days after rejection (safety) must write_next; got {result!r}."
        )


# ---------------------------------------------------------------------------
# 7. plan_attempt: stalled 14 vs 16 minutes — both should skip
#    (stalled status is handled by Stage A escalation, not here)
# ---------------------------------------------------------------------------

class TestPlanAttemptStalledStatuses:
    def _stalled_entry(self) -> dict:
        return {
            "attempt_number": 1,
            "finding_id": "FIND-0001",
            "inputs_hash": "a" * 64,
            "expires_at": "2099-01-01T00:00:00Z",
            "diagnosis_source": "model",
            "queue_status": "stalled",
            "decided_at": None,
        }

    def _pending_entry(self) -> dict:
        return {**self._stalled_entry(), "queue_status": "pending"}

    def test_stalled_skips(self):
        """Stalled queue status must skip (D12: Stage A escalation owns it)."""
        from agents.fi_recall_warranty.attempts import plan_attempt

        result = plan_attempt(self._stalled_entry(), "safety", _NOW)
        assert result == "skip", (
            f"stalled must produce 'skip'; got {result!r}."
        )

    def test_pending_rewrites_current(self):
        """Pending queue status (≤ 15 min) must rewrite current attempt (C1 fix)."""
        from agents.fi_recall_warranty.attempts import plan_attempt

        result = plan_attempt(self._pending_entry(), "safety", _NOW)
        assert result == "write_current"
