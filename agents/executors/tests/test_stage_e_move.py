"""Red tests: CmsExecutor for move_vehicles (spec D11, contract § 8, task 4.3).

Every test is xfail(strict=True, reason="fi-rebalancing-stage-e task 4.3").

Task 4.3 wires CmsExecutor to POST to
``/api/v1/fleet-intelligence/moves`` and map the HTTP status to the executor
contract:
  - 409  → ActionStatus.DECLINED / FailureKind.TARGET_REFUSED
  - 5xx (after retries) → ActionStatus.FAILED / FailureKind.EXECUTOR_EXHAUSTED_RETRIES

These tests xfail today for two reasons:
  1. CmsExecutor does not accept RetryPolicy / sleep_fn in its constructor →
     TypeError in _make_cms_executor() → test fails → xfail.
  2. Even if constructed, dispatch makes no HTTP call for move_vehicles (it
     immediately refuses); the ``len(rsps.calls) >= 1`` guard catches this.

Run:
    .venv/bin/python -m pytest agents/executors/tests/test_stage_e_move.py -q
"""
from __future__ import annotations

import os
from contextlib import contextmanager
from unittest.mock import patch

import pytest
import responses as rsps  # type: ignore[import]

from agents.executors.base import ActionStatus, FailureKind

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_FLEET_ID = "fleet-meridian-01"
_FROM_LOC = "loc-sea-downtown-02"
_TO_LOC = "loc-sea-airport-01"
_VIN_A = "MRDVEH00000000021"
_VIN_B = "MRDVEH00000000022"
_CMS_BASE = "https://cms-staging.test"
_MOVES_URL = f"{_CMS_BASE}/api/v1/fleet-intelligence/moves"

_OPERATOR_TOKEN = "op-jwt-stage-e-0001"

# The executor forwards the approving operator's token; a claims dict without one is a
# local terminal error with nothing sent (architect, group4-design.md "Red-test changes").
_OPERATOR_CLAIMS = {
    "token": _OPERATOR_TOKEN,
    "sub": "op-sub-stage-e-0001",
    "cognito:groups": ["fleet-operator"],
    "custom:fleetIds": f"{_FLEET_ID},fleet-meridian-02",
}

_MOVE_ACTION = {
    "action_id": "AVX-ACT-e1000000-0000-4000-8000-000000000001",
    "action_kind": "move_vehicles",
    "target_system": "cms",
    "decided_by": {"kind": "operator", "id": _OPERATOR_CLAIMS["sub"]},
    "decision": "approved",
    "finding_id": "FIND-e1000000-0000-4000-8000-000000000001",
    "parameters": {
        "fleet_id": _FLEET_ID,
        "from_location_id": _FROM_LOC,
        "to_location_id": _TO_LOC,
        "vehicle_vins": [_VIN_A, _VIN_B],
        "forecast_week": "2026-W42",
        "transfer_cost_usd": 84.06,
        "predicted_on_rent_hours_gain": 35.0,
        "predicted_turnaways_avoided": 2,
    },
}


def _make_cms_executor():
    """Construct CmsExecutor as task 4.3 will wire it.

    Pre-4.3: CmsExecutor constructor takes no arguments → TypeError → xfail.
    Post-4.3: constructor accepts sleep_fn and retry_policy for the HTTP path.
    """
    from agents.executors.cms import CmsExecutor
    from agents.executors.base import RetryPolicy

    return CmsExecutor(
        sleep_fn=lambda _: None,
        retry_policy=RetryPolicy(retry_count=2, backoff_base_ms=0, backoff_cap_ms=0),
    )


@contextmanager
def _patch_cms_endpoint():
    """Patch the CMS endpoint lookup so dispatch sends requests to _CMS_BASE."""
    with (
        patch.dict(os.environ, {"CMS_API_ENDPOINT": _CMS_BASE}, clear=False),
        patch(
            "agents.executors.cms.CmsExecutor._get_cms_endpoint",
            return_value=_CMS_BASE,
            create=True,
        ),
    ):
        yield


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestCmsExecutorMoveVehicles:
    """CmsExecutor dispatches move_vehicles via HTTP and maps CMS status codes."""

    @rsps.activate
    def test_cms_409_maps_to_refused(self):
        """CMS HTTP 409 → ActionStatus.DECLINED / FailureKind.TARGET_REFUSED.

        Guard: _make_cms_executor() raises TypeError (no constructor params yet) → xfail.
        Secondary guard: even if constructed, no HTTP call is made today →
        the ``len(rsps.calls) >= 1`` assertion fails → xfail.

        After 4.3: CmsExecutor POSTs to CMS; 409 → DECLINED / TARGET_REFUSED.

        Mutation: map 409 to FAILED → status != DECLINED → this fails.
        """
        rsps.add(rsps.POST, _MOVES_URL, json={"error": "Vehicle grounded"}, status=409)

        with _patch_cms_endpoint():
            executor = _make_cms_executor()
            result = executor.dispatch(_MOVE_ACTION, _OPERATOR_CLAIMS)

        assert len(rsps.calls) >= 1, (
            "CmsExecutor must make an HTTP call for move_vehicles. "
            "Guard: no call made today → assertion fails → xfail."
        )
        assert result.status == ActionStatus.DECLINED, (
            f"CMS 409 must map to DECLINED, got {result.status!r}. "
            "Mutation: return FAILED for 409 → wrong status → this fails."
        )
        assert result.failure_kind == FailureKind.TARGET_REFUSED, (
            f"CMS 409 must yield TARGET_REFUSED, got {result.failure_kind!r}. "
            "Mutation: return EXECUTOR_TERMINAL_ERROR → wrong kind → this fails."
        )

    @rsps.activate
    def test_cms_5xx_after_retries_maps_to_failed(self):
        """CMS 5xx repeated after retries → FAILED / EXECUTOR_EXHAUSTED_RETRIES.

        Guard: same two guards as test_cms_409_maps_to_refused.

        After 4.3: retries exhaust; → FAILED / EXECUTOR_EXHAUSTED_RETRIES.

        Mutation: map 5xx to DECLINED → status != FAILED → this fails.
        """
        for _ in range(3):  # initial + 2 retries
            rsps.add(rsps.POST, _MOVES_URL, json={"error": "unavailable"}, status=503)

        with _patch_cms_endpoint():
            executor = _make_cms_executor()
            result = executor.dispatch(_MOVE_ACTION, _OPERATOR_CLAIMS)

        assert len(rsps.calls) >= 1, (
            "CmsExecutor must attempt at least one HTTP call for move_vehicles. "
            "Guard: no call made today → assertion fails → xfail."
        )
        assert result.status == ActionStatus.FAILED, (
            f"CMS 5xx after retries must map to FAILED, got {result.status!r}. "
            "Mutation: map 5xx to DECLINED → wrong status → this fails."
        )
        assert result.failure_kind == FailureKind.EXECUTOR_EXHAUSTED_RETRIES, (
            f"Expected EXECUTOR_EXHAUSTED_RETRIES, got {result.failure_kind!r}. "
            "Mutation: return TARGET_REFUSED → wrong kind → this fails."
        )

    @rsps.activate
    def test_cms_200_maps_to_executed(self):
        """CMS 200 → ActionStatus.EXECUTED.

        Positive control: a CmsExecutor that always returns DECLINED would pass
        the 409 test but fail this one.
        """
        rsps.add(rsps.POST, _MOVES_URL, json={"move_id": "mv-001"}, status=200)

        with _patch_cms_endpoint():
            executor = _make_cms_executor()
            result = executor.dispatch(_MOVE_ACTION, _OPERATOR_CLAIMS)

        assert len(rsps.calls) >= 1, (
            "CmsExecutor must make an HTTP call for move_vehicles. "
            "Guard: no call made → assertion fails → xfail."
        )
        assert rsps.calls[0].request.headers["Authorization"] == f"Bearer {_OPERATOR_TOKEN}"
        assert result.status == ActionStatus.EXECUTED, (
            f"CMS 200 must map to EXECUTED, got {result.status!r}. "
            "Positive control: always-DECLINED executor fails here."
        )
