"""CmsExecutor move_vehicles: request shape, token, redirects, endpoint and parameter guards.

Task 4.3 (FI rebalancing stage E), architect, 2026-10-10. Complements the red tests in
test_stage_e_move.py. The endpoint comes from the real ``_get_cms_endpoint`` here (env
``VSA_CMS_API_ENDPOINT``), not a patch, so its https check is exercised.
"""
from __future__ import annotations

import json
import os
from decimal import Decimal
from unittest.mock import patch

import pytest
import responses as rsps  # type: ignore[import]

from agents.executors.base import ActionStatus, FailureKind, RetryPolicy
from agents.executors.cms import CmsExecutor, _MOVE_REQUIRED

_BASE = "https://cms-staging.test"
_URL = f"{_BASE}/api/v1/fleet-intelligence/moves"
_TOKEN = "op-jwt-stage-e-more-0001"
_CLAIMS = {"token": _TOKEN, "sub": "op-sub-0002"}


def _action(**param_overrides):
    params = {
        "fleet_id": "demo-rental-carshare",
        "from_location_id": "loc-sea-dt",
        "to_location_id": "loc-sea-airport",
        "vehicle_vins": ["MRDSEAVIN00000001", "MRDSEAVIN00000002"],
        "forecast_week": "2026-W42",
        # DynamoDB hands the executor Decimals; requests can't serialise them.
        "transfer_cost_usd": Decimal("84.06"),
        "predicted_on_rent_hours_gain": Decimal("35.5"),
        "predicted_turnaways_avoided": Decimal("2"),
    }
    params.update(param_overrides)
    return {
        "action_id": "AVX-ACT-e3000000-0000-4000-8000-000000000003",
        "finding_id": "FIND-e3000000-0000-4000-8000-000000000003",
        "action_kind": "move_vehicles",
        "target_system": "cms",
        "parameters": params,
    }


def _executor():
    return CmsExecutor(sleep_fn=lambda _: None,
                       retry_policy=RetryPolicy(retry_count=2, backoff_base_ms=0, backoff_cap_ms=0))


@pytest.fixture(autouse=True)
def _endpoint():
    with patch.dict(os.environ, {"VSA_CMS_API_ENDPOINT": _BASE}):
        yield


@rsps.activate
def test_request_body_and_token_are_exact():
    rsps.add(rsps.POST, _URL, json={"moveId": "x"}, status=200)
    result = _executor().dispatch(_action(), _CLAIMS)
    assert result.status == ActionStatus.EXECUTED
    assert len(rsps.calls) == 1
    req = rsps.calls[0].request
    assert req.headers["Authorization"] == f"Bearer {_TOKEN}"
    assert json.loads(req.body) == {
        "actionId": "AVX-ACT-e3000000-0000-4000-8000-000000000003",
        "findingId": "FIND-e3000000-0000-4000-8000-000000000003",
        "fleetId": "demo-rental-carshare",
        "fromLocationId": "loc-sea-dt",
        "toLocationId": "loc-sea-airport",
        "vehicle_vins": ["MRDSEAVIN00000001", "MRDSEAVIN00000002"],
        "forecastWeek": "2026-W42",
        "transferCostUsd": 84.06,
        "predictedOnRentHoursGain": 35.5,
        "predictedTurnawaysAvoided": 2,
    }


@rsps.activate
def test_no_token_sends_nothing():
    rsps.add(rsps.POST, _URL, json={}, status=200)
    result = _executor().dispatch(_action(), {"sub": "op-sub-0002"})
    assert len(rsps.calls) == 0
    assert result.status == ActionStatus.FAILED
    assert result.failure_kind == FailureKind.EXECUTOR_TERMINAL_ERROR


@rsps.activate
def test_a_redirect_is_never_followed():
    elsewhere = "https://elsewhere.test/collect"
    rsps.add(rsps.POST, _URL, status=307, headers={"Location": elsewhere})
    rsps.add(rsps.POST, elsewhere, json={}, status=200)
    result = _executor().dispatch(_action(), _CLAIMS)
    assert [c.request.url for c in rsps.calls] == [_URL]
    assert result.status == ActionStatus.FAILED
    assert result.failure_kind == FailureKind.EXECUTOR_TERMINAL_ERROR


@rsps.activate
@pytest.mark.parametrize("endpoint", ["http://cms-staging.test", "https://user:pw@cms.test", ""])
def test_an_unsafe_endpoint_sends_nothing(endpoint):
    rsps.add(rsps.POST, _URL, json={}, status=200)
    with patch.dict(os.environ, {"VSA_CMS_API_ENDPOINT": endpoint}):
        result = _executor().dispatch(_action(), _CLAIMS)
    assert len(rsps.calls) == 0
    assert result.failure_kind == FailureKind.EXECUTOR_TERMINAL_ERROR


@rsps.activate
@pytest.mark.parametrize("missing", list(_MOVE_REQUIRED))
def test_a_missing_parameter_sends_nothing(missing):
    rsps.add(rsps.POST, _URL, json={}, status=200)
    action = _action()
    del action["parameters"][missing]
    result = _executor().dispatch(action, _CLAIMS)
    assert len(rsps.calls) == 0
    assert result.failure_kind == FailureKind.EXECUTOR_TERMINAL_ERROR


@rsps.activate
@pytest.mark.parametrize("vins", [[], ["MRDSEAVIN00000001", 7], "MRDSEAVIN00000001"])
def test_a_malformed_vin_list_sends_nothing(vins):
    rsps.add(rsps.POST, _URL, json={}, status=200)
    result = _executor().dispatch(_action(vehicle_vins=vins), _CLAIMS)
    assert len(rsps.calls) == 0
    assert result.failure_kind == FailureKind.EXECUTOR_TERMINAL_ERROR


@rsps.activate
@pytest.mark.parametrize("status,expected_kind", [
    (409, FailureKind.TARGET_REFUSED),   # grounded, moved already, parking full
    (400, FailureKind.TARGET_REFUSED),
    (401, FailureKind.EXECUTOR_TERMINAL_ERROR),
    (403, FailureKind.EXECUTOR_TERMINAL_ERROR),
])
def test_status_mapping(status, expected_kind):
    rsps.add(rsps.POST, _URL, json={}, status=status)
    result = _executor().dispatch(_action(), _CLAIMS)
    assert len(rsps.calls) == 1
    assert result.failure_kind == expected_kind


# ---------------------------------------------------------------------------
# W10: replay safety — 409 after an uncertain prior attempt is FAILED, not refused
# ---------------------------------------------------------------------------

@rsps.activate
def test_timeout_then_409_is_failed():
    """A timeout on attempt 1 makes the commit uncertain; a 409 on attempt 2 is FAILED.

    W10: if the first POST timed out, CMS may have committed the move but the
    response was lost.  The second attempt gets a 409 ("already exists").  This
    cannot be treated as a deliberate refusal — the move may already have happened.
    The executor returns FAILED / EXECUTOR_EXHAUSTED_RETRIES with a note naming
    the unknown outcome, so the operator is alerted to verify before retrying.

    Mutation F4-replay-removed: remove the uncertain_commit flag so a timed-out
    move's 409 becomes TARGET_REFUSED → this assertion fails (FAILED ≠ TARGET_REFUSED).
    Mutation F4-uncertain-timeout: do not set uncertain_commit on a transient
    exception → FAILED becomes TARGET_REFUSED → this assertion fails.
    """
    import requests as _requests
    rsps.add(rsps.POST, _URL, body=_requests.Timeout())
    rsps.add(rsps.POST, _URL, json={}, status=409)
    result = _executor().dispatch(_action(), _CLAIMS)
    assert len(rsps.calls) == 2, (
        f"executor must retry after a timeout; expected 2 calls, got {len(rsps.calls)}"
    )
    assert result.status == "failed", (
        f"timeout-then-409 must be FAILED (unknown outcome), got status={result.status!r}. "
        "Mutation F4-replay-removed: 409 maps to TARGET_REFUSED without the flag → fails."
    )
    assert result.failure_kind == FailureKind.EXECUTOR_EXHAUSTED_RETRIES, (
        f"failure_kind must be EXECUTOR_EXHAUSTED_RETRIES, got {result.failure_kind!r}. "
        "Mutation F4-uncertain-timeout: uncertain_commit not set on timeout → TARGET_REFUSED → fails."
    )
    assert result.note and "unknown" in result.note.lower(), (
        f"note must name the unknown outcome, got {result.note!r}. "
        "Mutation: note absent → this assertion fails."
    )


@rsps.activate
def test_503_then_409_is_failed():
    """A 5xx on attempt 1 makes the commit uncertain; a 409 on attempt 2 is FAILED.

    W10: a 503 (server error) on the first POST means CMS may have committed
    internally before the error.  The second attempt gets a 409.  Same logic as
    the timeout case: the result must be FAILED / EXECUTOR_EXHAUSTED_RETRIES,
    not TARGET_REFUSED.

    Mutation F4-uncertain-5xx: do not set uncertain_commit on a 5xx
    → FAILED becomes TARGET_REFUSED → this assertion fails.
    """
    rsps.add(rsps.POST, _URL, json={}, status=503)
    rsps.add(rsps.POST, _URL, json={}, status=409)
    result = _executor().dispatch(_action(), _CLAIMS)
    assert len(rsps.calls) == 2, (
        f"executor must retry after a 503; expected 2 calls, got {len(rsps.calls)}"
    )
    assert result.status == "failed", (
        f"503-then-409 must be FAILED (unknown outcome), got status={result.status!r}. "
        "Mutation F4-uncertain-5xx: uncertain_commit not set on 5xx → TARGET_REFUSED → fails."
    )
    assert result.failure_kind == FailureKind.EXECUTOR_EXHAUSTED_RETRIES, (
        f"failure_kind must be EXECUTOR_EXHAUSTED_RETRIES, got {result.failure_kind!r}."
    )
    assert result.note and "unknown" in result.note.lower(), (
        f"note must name the unknown outcome, got {result.note!r}."
    )


@rsps.activate
def test_409_on_first_attempt_is_refused():
    """A 409 with no prior attempt stays refused.

    This is the control case: no prior transient, so the commit is not uncertain.
    The 409 means CMS definitely refused (vehicles already moved, parking full, etc.),
    and the executor must return TARGET_REFUSED, not FAILED.

    Mutation F4-replay-over-eager: always return exhausted on 409 regardless of
    uncertain_commit → TARGET_REFUSED becomes EXECUTOR_EXHAUSTED_RETRIES → fails.
    """
    rsps.add(rsps.POST, _URL, json={}, status=409)
    result = _executor().dispatch(_action(), _CLAIMS)
    assert len(rsps.calls) == 1, (
        f"executor must not retry a 409 on the first attempt; got {len(rsps.calls)} calls"
    )
    assert result.failure_kind == FailureKind.TARGET_REFUSED, (
        f"409 with no prior attempt must be TARGET_REFUSED, got {result.failure_kind!r}. "
        "Mutation F4-replay-over-eager: always exhausted on 409 → EXECUTOR_EXHAUSTED_RETRIES → fails."
    )
