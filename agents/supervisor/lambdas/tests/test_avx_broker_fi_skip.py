"""Tests for the FI item skip in avx_broker._process_finding (spec D13, task 1.8).

Spec: ``.kiro/specs/2026-10-05-fi-recall-warranty-stage-c/``

Design: one shared predicate case table, run against BOTH
  - ``agents/supervisor/lambdas/avx_broker._is_fi_finding``  (broker)
  - ``lambdas/api-avx-findings/handler._is_fi_finding``      (findings handler)
Neither bundle imports the other; the table lives here and both are imported
independently below.

Mutation record (see decisions.md for the broker-mut workspace):
  M1  Skip block removed from _process_finding entirely
      → ``test_fi_item_p0_recall_writes_no_card_and_no_push`` FAILED ✓ CAUGHT
  M2  ``or`` changed to ``and`` in _is_fi_finding
      → ``test_fi_predicate_prefix_alone_triggers_skip`` FAILED ✓ CAUGHT
  M3  Prefix check dropped (only ``recommended_action`` branch kept)
      → ``test_fi_predicate_prefix_alone_triggers_skip`` FAILED ✓ CAUGHT
  M4  Skip block moved to after owner resolution
      → ``test_fi_skip_runs_before_owner_resolution`` FAILED ✓ CAUGHT
"""
from __future__ import annotations

import json
import os
import sys
from unittest.mock import MagicMock, call, patch

import pytest

# ---------------------------------------------------------------------------
# Path setup — broker
# ---------------------------------------------------------------------------
_LAMBDAS_DIR = os.path.join(os.path.dirname(__file__), "..")
if _LAMBDAS_DIR not in sys.path:
    sys.path.insert(0, _LAMBDAS_DIR)

import avx_broker as broker

# ---------------------------------------------------------------------------
# Path setup — findings handler (independent import, no cross-bundle import)
# ---------------------------------------------------------------------------
_FINDINGS_LAMBDA_ROOT = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "..",
                 "lambdas", "api-avx-findings")
)
if _FINDINGS_LAMBDA_ROOT not in sys.path:
    sys.path.insert(0, _FINDINGS_LAMBDA_ROOT)

# Import under a distinct alias so it doesn't collide with the repo-root conftest's
# handler-eviction logic (which evicts sys.modules["handler"] before each test).
import handler as _findings_handler  # noqa: E402


# ---------------------------------------------------------------------------
# Shared predicate case table
#
# Each row: (finding_fields, expected_is_fi, label)
# Run against broker._is_fi_finding AND _findings_handler._is_fi_finding.
# ---------------------------------------------------------------------------
_FI_PREDICATE_CASES: list[tuple[dict, bool, str]] = [
    # --- FI by agent_id prefix alone (no recommended_action) ---------------
    (
        {"agent_id": "agent-fi-recall-warranty", "finding_kind": "fleet.recall.vehicle"},
        True,
        "agent-fi-recall-warranty prefix no recommended_action",
    ),
    (
        {"agent_id": "agent-fi-cost"},
        True,
        "agent-fi-cost prefix no recommended_action",
    ),
    (
        {"agent_id": "agent-fi-"},
        True,
        "bare agent-fi- prefix",
    ),
    # --- FI by recommended_action alone (non-FI agent_id) ------------------
    (
        {"agent_id": "agent-health", "recommended_action": {"kind": "ground_vehicle"}},
        True,
        "non-fi agent_id with recommended_action",
    ),
    (
        {"recommended_action": {"kind": "submit_warranty_claim"}},
        True,
        "no agent_id with recommended_action",
    ),
    (
        {"agent_id": "agent-health-battery", "recommended_action": {}},
        True,
        "health agent + empty recommended_action dict",
    ),
    # --- FI by both conditions ----------------------------------------------
    (
        {"agent_id": "agent-fi-recall-warranty", "recommended_action": {"kind": "ground_vehicle"}},
        True,
        "agent-fi- prefix AND recommended_action",
    ),
    # --- NOT FI — neither condition -----------------------------------------
    (
        {"agent_id": "agent-health", "finding_kind": "health.tires"},
        False,
        "health agent no recommended_action",
    ),
    (
        {"agent_id": "agent-health-battery"},
        False,
        "health battery agent no recommended_action",
    ),
    (
        {"finding_kind": "health.brakes"},
        False,
        "no agent_id no recommended_action",
    ),
    # --- Edge: agent_id is not a string -------------------------------------
    (
        {"agent_id": None},
        False,
        "None agent_id",
    ),
    (
        {"agent_id": 42},
        False,
        "integer agent_id",
    ),
    # --- Does not match on non-fi-prefixed agent_id -------------------------
    (
        {"agent_id": "agent-finance"},
        False,
        "agent-finance is not agent-fi- prefix",
    ),
    (
        {"agent_id": "agent_fi_health"},
        False,
        "underscore-separated name not agent-fi- prefix",
    ),
    (
        {"agent_id": "AGENT-FI-RECALL"},
        False,
        "uppercase prefix is not a match (case-sensitive)",
    ),
]


# ---------------------------------------------------------------------------
# Shared case table — parametrized tests against BOTH predicates
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("fields,expected,label", _FI_PREDICATE_CASES, ids=[c[2] for c in _FI_PREDICATE_CASES])
def test_broker_fi_predicate(fields: dict, expected: bool, label: str) -> None:
    """broker._is_fi_finding must match the shared case table."""
    assert broker._is_fi_finding(fields) is expected, (
        f"broker._is_fi_finding({fields!r}) expected {expected} for case {label!r}"
    )


@pytest.mark.parametrize("fields,expected,label", _FI_PREDICATE_CASES, ids=[c[2] for c in _FI_PREDICATE_CASES])
def test_findings_handler_fi_predicate(fields: dict, expected: bool, label: str) -> None:
    """findings handler _is_fi_finding must match the same shared case table."""
    assert _findings_handler._is_fi_finding(fields) is expected, (
        f"findings handler._is_fi_finding({fields!r}) expected {expected} for case {label!r}"
    )


# ---------------------------------------------------------------------------
# Helper: minimal policy dict (bypass SSM)
# ---------------------------------------------------------------------------

def _policy(threshold: str = "p0",
            rate_limit: dict | None = None,
            cap: int = 15) -> dict:
    return {
        "apns_threshold": threshold,
        "rate_limit": rate_limit or {"day": 10, "hour": 3},
        "cardinality_cap": cap,
    }


# Cognito sub used across several tests
_SUB = "aabbccdd-1122-3344-5566-778899aabbcc"


def _recall_finding(sub: str = _SUB) -> dict:
    """A p0 fleet.recall.vehicle Finding with a Cognito-sub owner (FI by agent_id)."""
    return {
        "finding_id": "FIND-FI-0000-0000-0000-000000000001",
        "finding_kind": "fleet.recall.vehicle",
        "severity": "p0",
        "headline": "Safety recall open",
        "agent_id": "agent-fi-recall-warranty",
        "scope": {"owner_id": sub},
    }


def _fi_cost_finding(sub: str = _SUB) -> dict:
    """An agent-fi-cost Finding with a Cognito-sub owner (FI by prefix only).

    Carries scope.owner_id so this fixture exercises D13's named risk (a cost
    Finding whose owner_id is a driver's Cognito sub) — without it the test
    only catches the prefix condition but never the owner path.
    NOTE: no recommended_action
    """
    return {
        "finding_id": "FIND-FI-0000-0000-0000-000000000002",
        "finding_kind": "coverage.cost",
        "severity": "informational",
        "headline": "Fleet cost update",
        "agent_id": "agent-fi-cost",
        "scope": {"owner_id": sub},
        # NOTE: no recommended_action
    }


def _non_fi_with_recommended_action(sub: str = _SUB) -> dict:
    """A health Finding (non-fi agent_id) that carries recommended_action (FI by field)."""
    return {
        "finding_id": "FIND-FI-0000-0000-0000-000000000003",
        "finding_kind": "health.tires",
        "severity": "p0",
        "headline": "Tire pressure low",
        "agent_id": "agent-health",
        "recommended_action": {"kind": "ground_vehicle"},
        "scope": {"owner_id": sub},
    }


def _health_finding(sub: str = _SUB) -> dict:
    """A plain health Finding with no FI markers."""
    return {
        "finding_id": "FIND-HE-0000-0000-0000-000000000004",
        "finding_kind": "health.tires",
        "severity": "p0",
        "headline": "Tire pressure low",
        "agent_id": "agent-health",
        "scope": {"owner_id": sub},
    }


# ---------------------------------------------------------------------------
# Core skip test (catches Mutation 1: skip removed)
# ---------------------------------------------------------------------------

def test_fi_item_p0_recall_writes_no_card_and_no_push() -> None:
    """A p0 fleet.recall.vehicle FI Finding must be skipped before any DDB/SNS call.

    Mutation 1 catcher: if the skip block is removed, _process_finding falls
    through to owner-resolution and the gate logic.  Because the owner here is
    a Cognito sub (not CUST-*), it would pass the namespace check and reach
    the cardinality/consent/rate gates — none of which are mocked here — so
    the function would raise (or call real AWS, blocked by the repo guard).
    Instead it should return skip_reason="fi_item" immediately.
    """
    finding = _recall_finding(sub=_SUB)
    pol = _policy()

    # No mocking of DDB/SSM/SNS: if the FI check fires first, none of those
    # code paths are reached.  If the skip is removed (M1), the function will
    # try to call SSM or DDB and the repo-root guard will turn the test into a
    # FAILURE (RealAwsCallBlocked).
    result = broker._process_finding(finding, pol)

    assert result["action"] == "skipped"
    assert result["skip_reason"] == "fi_item"
    assert result["finding_id"] == finding["finding_id"]
    assert result["demotion_reason"] is None


def test_fi_cost_finding_skipped_by_prefix_alone() -> None:
    """An agent-fi-cost Finding without recommended_action is skipped (prefix arm).

    Also catches Mutation 2 (or→and) and Mutation 3 (prefix dropped):
    both mutations would require the recommended_action arm to fire, but this
    Finding has no recommended_action, so the skip would not happen and the
    function would fall through to the gate logic (raising or blocked AWS).
    """
    finding = _fi_cost_finding()
    pol = _policy()

    result = broker._process_finding(finding, pol)

    assert result["action"] == "skipped"
    assert result["skip_reason"] == "fi_item"


def test_non_fi_finding_with_recommended_action_skipped() -> None:
    """A non-fi-prefixed Finding that carries recommended_action is also skipped."""
    finding = _non_fi_with_recommended_action(sub=_SUB)
    pol = _policy()

    result = broker._process_finding(finding, pol)

    assert result["action"] == "skipped"
    assert result["skip_reason"] == "fi_item"


# ---------------------------------------------------------------------------
# Mutation 2 — or changed to and (also caught above, explicit test for clarity)
# ---------------------------------------------------------------------------

def test_fi_predicate_prefix_alone_triggers_skip() -> None:
    """_is_fi_finding returns True for agent-fi- prefix alone (no recommended_action).

    Mutation 2 catcher: if 'or' is changed to 'and', both conditions must be
    True simultaneously.  This finding has no recommended_action, so _and_
    would return False — the skip wouldn't fire.
    Mutation 3 catcher: if the prefix check is dropped, only the
    recommended_action arm remains.  This finding has no recommended_action,
    so the predicate would return False.
    """
    finding = {"agent_id": "agent-fi-recall-warranty"}
    assert broker._is_fi_finding(finding) is True


def test_fi_predicate_recommended_action_alone_triggers_skip() -> None:
    """_is_fi_finding returns True for recommended_action alone (non-fi agent_id)."""
    finding = {"agent_id": "agent-health", "recommended_action": {"kind": "ground_vehicle"}}
    assert broker._is_fi_finding(finding) is True


# ---------------------------------------------------------------------------
# Mutation 4 — skip moved after owner resolution
# ---------------------------------------------------------------------------

def test_fi_skip_runs_before_owner_resolution() -> None:
    """The FI skip must run BEFORE _resolve_owner_party_id is called.

    Mutation 4 catcher: if the skip block is moved to after the owner
    resolution line, _resolve_owner_party_id is called before the skip.
    The patch below tracks calls; assert_not_called() fails if the mutant
    runs _resolve_owner_party_id first.
    """
    finding = _recall_finding(sub=_SUB)
    pol = _policy()

    with patch.object(broker, "_resolve_owner_party_id") as mock_resolve:
        result = broker._process_finding(finding, pol)

    mock_resolve.assert_not_called()
    assert result["skip_reason"] == "fi_item"


# ---------------------------------------------------------------------------
# Health finding is NOT FI-skipped (behaves as before)
# ---------------------------------------------------------------------------

def test_health_finding_not_fi_skipped() -> None:
    """A plain health Finding (no agent-fi-, no recommended_action) is not FI-skipped.

    The broker must continue past the FI check.  We verify this by asserting
    the skip_reason is NOT "fi_item".  Since the finding's owner is a Cognito
    sub (UUID), the broker will pass the namespace guard and attempt the gate
    reads.  We mock them minimally so no AWS call is made.
    """
    finding = _health_finding(sub=_SUB)
    pol = _policy()

    # Mock the gate internals so the test doesn't need real DDB/SSM/SNS.
    with (
        patch.object(broker, "_card_exists", return_value=False),
        patch.object(broker, "_check_cardinality", return_value=True),
        patch.object(broker, "_check_consent", return_value=False),  # consent_off → skipped
        patch.object(broker, "_check_demotion", return_value=(False, "no_prior_dismissal")),
    ):
        result = broker._process_finding(finding, pol)

    # Must NOT be FI-skipped; the broker reached the gates (consent_off here)
    assert result["skip_reason"] != "fi_item"
    assert result["action"] == "skipped"
    assert result["skip_reason"] == "consent_off"
