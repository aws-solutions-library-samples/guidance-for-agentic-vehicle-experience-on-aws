"""Registry re-check for book_fleet_service rule decisions (spec D7 / G8-AVX-1).

A rule-decided book_fleet_service passes the re-check only when:
  1. the decided_by has the valid rule shape (fleet_id/rule_id), AND
  2. FI_AUTO_REBOOK_ENABLED == "true" in the running process.

book_recall_service never admits a rule decider.
Operator and user deciders pass through unchanged.

Runs in agents/executors/tests/ (conftest guards AWS clients).
"""
from __future__ import annotations

import pytest

from agents.executors import registry
from agents.executors.base import ActionStatus, ExecuteResult, FailureKind

_FLEET = "fleet-meridian-01"
_RULE_ID = f"{_FLEET}/rebook-daily"
_VIN = "1HGBH41JXMN109186"
_OP_ID = "op-sub-g8-0001"

_BASE_BOOK = {
    "action_id": "AVX-ACT-g8g8g8g8-0000-4000-8000-000000000001",
    "finding_id": "FIND-g8g8g8g8-0000-4000-8000-000000000001",
    "action_kind": "book_fleet_service",
    "target_system": "dms",
    "fleet_id": _FLEET,
    "parameters": {
        "vehicle_vin": _VIN,
        "dealer_id": "D-001",
        "urgency": "this_week",
        "needs": [{"kind": "fault", "code": "P0AA6", "need_id": "need-1"}],
    },
}

_BASE_RECALL = {
    "action_id": "AVX-ACT-g8recall-0000-4000-8000-000000000002",
    "finding_id": "FIND-g8recall-0000-4000-8000-000000000002",
    "action_kind": "book_recall_service",
    "target_system": "dms",
    "fleet_id": _FLEET,
    "parameters": {"vehicle_vin": _VIN},
}


class _SpyExecutor:
    """Replaces the DMS executor; records calls and always returns executed."""

    def __init__(self):
        self.calls: list = []

    @property
    def target_system(self):
        return "dms"

    def dispatch(self, action, claims, **kw):
        self.calls.append((action, claims))
        return ExecuteResult.success(
            target_ref=action["action_id"], status=ActionStatus.EXECUTED
        )


@pytest.fixture()
def spy(monkeypatch):
    s = _SpyExecutor()
    monkeypatch.setitem(registry.EXECUTORS, "dms", s)
    return s


# ---------------------------------------------------------------------------
# book_fleet_service: rule decider
# ---------------------------------------------------------------------------

def test_rule_decided_bfs_passes_with_flag(spy, monkeypatch):
    """Valid rule shape + flag → passes re-check, executor called."""
    monkeypatch.setenv("FI_AUTO_REBOOK_ENABLED", "true")
    action = {**_BASE_BOOK, "decided_by": {"kind": "rule", "id": _RULE_ID}}
    result = registry.dispatch(action, {}, fire_escalation=False)
    assert spy.calls, "executor should be reached"
    assert result.status == ActionStatus.EXECUTED


def test_rule_decided_bfs_declined_without_flag(spy, monkeypatch):
    """Valid rule shape but flag absent → declined, executor not called."""
    monkeypatch.delenv("FI_AUTO_REBOOK_ENABLED", raising=False)
    action = {**_BASE_BOOK, "decided_by": {"kind": "rule", "id": _RULE_ID}}
    result = registry.dispatch(action, {}, fire_escalation=False)
    assert not spy.calls, "executor must not be reached when flag is off"
    assert result.failure_kind == FailureKind.TARGET_REFUSED


def test_rule_decided_bfs_declined_flag_wrong_case(spy, monkeypatch):
    """Flag == 'True' (capital T) → declined (must be exactly 'true')."""
    monkeypatch.setenv("FI_AUTO_REBOOK_ENABLED", "True")
    action = {**_BASE_BOOK, "decided_by": {"kind": "rule", "id": _RULE_ID}}
    result = registry.dispatch(action, {}, fire_escalation=False)
    assert not spy.calls
    assert result.failure_kind == FailureKind.TARGET_REFUSED


def test_rule_decided_bfs_declined_malformed_id(spy, monkeypatch):
    """Well-formed-looking id with a leading-zero fleet-id part → declined."""
    monkeypatch.setenv("FI_AUTO_REBOOK_ENABLED", "true")
    # The rule_decider regex requires fleet_id to be [A-Za-z0-9_-]{1,64}, but the
    # combined id is checked by _RULE_DECIDER_ID_RE. A slash-only id is invalid.
    action = {**_BASE_BOOK, "decided_by": {"kind": "rule", "id": "/"}}
    result = registry.dispatch(action, {}, fire_escalation=False)
    assert not spy.calls
    assert result.failure_kind == FailureKind.TARGET_REFUSED


def test_rule_decided_bfs_declined_empty_rule_part(spy, monkeypatch):
    """fleet_id present but rule_id empty → malformed → declined."""
    monkeypatch.setenv("FI_AUTO_REBOOK_ENABLED", "true")
    action = {**_BASE_BOOK, "decided_by": {"kind": "rule", "id": f"{_FLEET}/"}}
    result = registry.dispatch(action, {}, fire_escalation=False)
    assert not spy.calls
    assert result.failure_kind == FailureKind.TARGET_REFUSED


# ---------------------------------------------------------------------------
# book_fleet_service: operator and user deciders (unchanged)
# ---------------------------------------------------------------------------

def test_operator_decided_bfs_passes(spy, monkeypatch):
    """Operator decider → passes through regardless of flag."""
    monkeypatch.delenv("FI_AUTO_REBOOK_ENABLED", raising=False)
    action = {**_BASE_BOOK, "decided_by": {"kind": "operator", "id": _OP_ID}}
    result = registry.dispatch(action, {"token": "fake"}, fire_escalation=False)
    assert spy.calls
    assert result.status == ActionStatus.EXECUTED


# ---------------------------------------------------------------------------
# book_recall_service: rule decider never admitted
# ---------------------------------------------------------------------------

def test_rule_decided_recall_service_declined(spy, monkeypatch):
    """book_recall_service with a rule decider → declined (flag irrelevant)."""
    monkeypatch.setenv("FI_AUTO_REBOOK_ENABLED", "true")
    action = {**_BASE_RECALL, "decided_by": {"kind": "rule", "id": _RULE_ID}}
    result = registry.dispatch(action, {}, fire_escalation=False)
    assert not spy.calls
    assert result.failure_kind == FailureKind.TARGET_REFUSED


def test_operator_decided_recall_service_passes(spy, monkeypatch):
    """book_recall_service operator decider still passes (unchanged behaviour)."""
    monkeypatch.delenv("FI_AUTO_REBOOK_ENABLED", raising=False)
    action = {**_BASE_RECALL, "decided_by": {"kind": "operator", "id": _OP_ID}}
    result = registry.dispatch(action, {"token": "fake"}, fire_escalation=False)
    assert spy.calls
    assert result.status == ActionStatus.EXECUTED
