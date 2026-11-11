"""T1.2a: allow_redirects=False and 3xx → UnexpectedRedirect terminal error.

Both POSTs (cost-action and ground_vehicle) must pass allow_redirects=False and
treat any 3xx as a terminal error matching the _move_attempts pattern (cms.py:~776).

One test per call site per redirect code (301, 302, 307, 308):
  - Exactly 1 POST is made (no follow)
  - result.status == FAILED
  - result.failure_kind == EXECUTOR_TERMINAL_ERROR
  - result.last_error_class == "UnexpectedRedirect"
  - result.last_error_status == <code>
"""
from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
import responses as rsps  # type: ignore[import]

from agents.executors.base import ActionStatus, FailureKind, RetryPolicy
from agents.executors.cms import CmsExecutor

_CMS_BASE = "https://cms-staging.test"
_COST_ACTIONS_URL = f"{_CMS_BASE}/api/v1/fleet-intelligence/cost-actions"
_GROUNDINGS_URL = f"{_CMS_BASE}/api/v1/fleet-intelligence/groundings"

_OPERATOR_TOKEN = "Bearer.test.op.jwt"

_COST_ACTION = {
    "action_id": "AVX-ACT-deadbeef-0000-4000-8000-000000000001",
    "action_kind": "apply_cost_action",
    "target_system": "cms",
    "finding_id": "FIND-deadbeef-0000-4000-8000-000000000001",
    "decided_by": {"kind": "operator", "id": "op-sub-0001"},
    "parameters": {
        "vehicle_vin": "MRD0000000000001A",
        "cost_action": "driver_coaching",
        "month": "2026-10",
        "cause": "efficiency",
    },
}

_GROUND_VEHICLE_ACTION = {
    "action_id": "AVX-ACT-deadbeef-0000-4000-8000-000000000002",
    "action_kind": "ground_vehicle",
    "target_system": "cms",
    "finding_id": "FIND-deadbeef-0000-4000-8000-000000000002",
    "decided_by": {"kind": "operator", "id": "op-sub-0001"},
    "parameters": {
        "vehicle_vin": "MRD0000000000001A",
        # recall_id required: _grounding_body returns None without it, and the
        # executor returns terminal_error before the HTTP call (no call made).
        "recall_id": "RECALL-TEST-REDIRECT-001",
    },
}

_CLAIMS = {"token": _OPERATOR_TOKEN}

_FAST_POLICY = RetryPolicy(retry_count=3, backoff_base_ms=0, backoff_cap_ms=0)


def _make_executor() -> CmsExecutor:
    stub_ssm = MagicMock()
    stub_ssm.get_parameter.side_effect = Exception("stubbed SSM")
    return CmsExecutor(
        ssm_client=stub_ssm,
        sleep_fn=lambda _: None,
        retry_policy=_FAST_POLICY,
    )


# ---------------------------------------------------------------------------
# Cost-action: 301, 302, 307, 308 → UnexpectedRedirect terminal, no follow
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("redirect_code", [301, 302, 307, 308])
@rsps.activate
def test_cost_action_redirect_is_terminal_no_follow(redirect_code: int) -> None:
    """A 3xx on apply_cost_action → terminal_error(UnexpectedRedirect), no retry.

    The 'no follow' property is guarded by responses: if allow_redirects=False is
    missing, requests would follow the redirect and try the Location URL (which is
    not registered), raising a ConnectionError and producing exhausted rather than
    terminal UnexpectedRedirect. Adding allow_redirects=False makes the 3xx land
    as the final response and the 3xx guard fires.
    """
    rsps.add(rsps.POST, _COST_ACTIONS_URL,
             status=redirect_code,
             headers={"Location": "https://cms-new.test/api/v1/fleet-intelligence/cost-actions"})

    executor = _make_executor()
    with pytest.MonkeyPatch().context() as mp:
        mp.setenv("VSA_CMS_API_ENDPOINT", _CMS_BASE)
        result = executor.dispatch(_COST_ACTION, _CLAIMS)

    assert len(rsps.calls) == 1, (
        f"Expected exactly 1 POST for a {redirect_code}; "
        f"got {len(rsps.calls)} (request was followed or retried)"
    )
    assert result.status == ActionStatus.FAILED, (
        f"Expected FAILED for 3xx redirect, got {result.status!r}"
    )
    assert result.failure_kind == FailureKind.EXECUTOR_TERMINAL_ERROR, (
        f"Expected EXECUTOR_TERMINAL_ERROR, got {result.failure_kind!r}"
    )
    assert result.last_error_class == "UnexpectedRedirect", (
        f"Expected last_error_class='UnexpectedRedirect', got {result.last_error_class!r}"
    )
    assert result.last_error_status == redirect_code, (
        f"Expected last_error_status={redirect_code}, got {result.last_error_status!r}"
    )
    assert result.escalate is True, "terminal_error must escalate"


# ---------------------------------------------------------------------------
# Ground-vehicle: 301, 302, 307, 308 → UnexpectedRedirect terminal, no follow
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("redirect_code", [301, 302, 307, 308])
@rsps.activate
def test_ground_vehicle_redirect_is_terminal_no_follow(redirect_code: int) -> None:
    """A 3xx on ground_vehicle → terminal_error(UnexpectedRedirect), no retry.

    Same property as the cost-action path: a redirect is never a legitimate
    CMS response to a POST — the URL is agreed at deploy time, and a 3xx means
    something is mis-wired at the network layer (or an attacker). The request
    carries the operator's token; following a redirect would send that token
    to an unintended destination.
    """
    rsps.add(rsps.POST, _GROUNDINGS_URL,
             status=redirect_code,
             headers={"Location": "https://cms-new.test/api/v1/fleet-intelligence/groundings"})

    executor = _make_executor()
    with pytest.MonkeyPatch().context() as mp:
        mp.setenv("VSA_CMS_API_ENDPOINT", _CMS_BASE)
        result = executor.dispatch(_GROUND_VEHICLE_ACTION, _CLAIMS)

    assert len(rsps.calls) == 1, (
        f"Expected exactly 1 POST for a {redirect_code}; "
        f"got {len(rsps.calls)} (request was followed or retried)"
    )
    assert result.status == ActionStatus.FAILED, (
        f"Expected FAILED for 3xx redirect on ground_vehicle, got {result.status!r}"
    )
    assert result.failure_kind == FailureKind.EXECUTOR_TERMINAL_ERROR, (
        f"Expected EXECUTOR_TERMINAL_ERROR, got {result.failure_kind!r}"
    )
    assert result.last_error_class == "UnexpectedRedirect", (
        f"Expected last_error_class='UnexpectedRedirect', got {result.last_error_class!r}"
    )
    assert result.last_error_status == redirect_code, (
        f"Expected last_error_status={redirect_code}, got {result.last_error_status!r}"
    )
    assert result.escalate is True, "terminal_error must escalate"
