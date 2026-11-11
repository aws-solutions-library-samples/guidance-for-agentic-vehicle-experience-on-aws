"""Tests for CmsExecutor.apply_cost_action — spec D9, task 3.3.

Contract § 4, § 10, § 10a.

Five core behaviour tests:
  test_2xx_maps_to_completed
  test_4xx_maps_to_declined_target_refused
  test_5xx_exhausted_maps_to_failed_exhausted_retries
  test_missing_jwt_maps_to_terminal_error
  test_other_cms_kind_is_refused

Two mutation-check tests (task 3.3 Constraints):
  test_operator_required_is_true        — flip to False in a copy; a policy test fails
  test_auto_approvable_is_false         — flip to True in a copy; a policy test fails

These tests exercise the behaviour, not the mutation tooling itself.  The
actual mutation-check procedure (copy files to /tmp, run pytest with swapped
module, confirm named test fails) is documented in the hand-back.

All AWS clients and the HTTP layer are stubbed; sleep is injected so no real
waits occur.  The conftest ``_no_real_aws_clients`` fixture is autouse.
"""
from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
import responses as rsps  # type: ignore[import]

from agents.executors.base import (
    ActionStatus,
    FailureKind,
    RetryPolicy,
)
from agents.executors.cms import CmsExecutor

# ---------------------------------------------------------------------------
# Shared constants and fixtures
# ---------------------------------------------------------------------------

_CMS_BASE = "https://cms-staging.test"
_COST_ACTIONS_ENDPOINT = f"{_CMS_BASE}/api/v1/fleet-intelligence/cost-actions"

_VIN = "MRD0000000000001A"
_ACTION_UUID = "deadbeef-1234-4000-8000-cafebabe0001"
_FINDING_ID = "FND-deadbeef-1234-4000-8000-cafebabe0001"

#: A fully valid apply_cost_action with all required parameters.
_ACTION: dict[str, Any] = {
    "action_id": f"AVX-ACT-{_ACTION_UUID}",
    "action_kind": "apply_cost_action",
    "target_system": "cms",
    "finding_id": _FINDING_ID,
    "decided_by": {"kind": "operator", "id": "op-sub-0001"},
    "parameters": {
        "vehicle_vin": _VIN,
        "cost_action": "reassign_or_remarket",
        "month": "2026-08",
        "cause": "low_utilisation",
    },
}

#: Operator claims carrying a bearer token.
_CLAIMS: dict[str, Any] = {
    "sub": "op-sub-0001",
    "cognito:groups": ["fleet-operator"],
    "custom:fleetIds": "fleet-meridian-01",
    "token": "Bearer.test.operator.jwt",
}

#: A RetryPolicy with retry_count=3 and zero-ms backoff so tests stay fast.
_FAST_POLICY = RetryPolicy(retry_count=3, backoff_base_ms=0, backoff_cap_ms=0)


def _make_executor(*, retry_policy: RetryPolicy = _FAST_POLICY) -> CmsExecutor:
    """Build a CmsExecutor with stubbed SSM and a fast retry policy."""
    stub_ssm = MagicMock()
    stub_ssm.get_parameter.side_effect = Exception("stubbed SSM — policy injected directly")
    return CmsExecutor(
        ssm_client=stub_ssm,
        sleep_fn=lambda _: None,
        retry_policy=retry_policy,
    )


# ---------------------------------------------------------------------------
# Core behaviour tests
# ---------------------------------------------------------------------------


@rsps.activate
def test_2xx_maps_to_completed() -> None:
    """CMS 200 on apply_cost_action → COMPLETED.

    Spec D9: "Executed means 'the operator's decision is recorded where the
    fleet's own systems would act on it'".  The record is immediately complete;
    there is no separate completion step.
    """
    rsps.add(rsps.POST, _COST_ACTIONS_ENDPOINT,
             json={"record_id": "car-001", "status": "recorded"}, status=200)

    executor = _make_executor()
    with pytest.MonkeyPatch().context() as mp:
        mp.setenv("VSA_CMS_API_ENDPOINT", _CMS_BASE)
        result = executor.dispatch(_ACTION, _CLAIMS)

    assert result.status == ActionStatus.COMPLETED, (
        f"Expected status=completed for a 200 response, got {result.status!r}. "
        "A successful apply_cost_action is immediately complete (spec D9)."
    )
    assert result.failure_kind is None
    assert result.escalate is False


@rsps.activate
def test_201_maps_to_completed() -> None:
    """CMS 201 Created on apply_cost_action → COMPLETED (same as 200)."""
    rsps.add(rsps.POST, _COST_ACTIONS_ENDPOINT,
             json={"record_id": "car-002"}, status=201)

    executor = _make_executor()
    with pytest.MonkeyPatch().context() as mp:
        mp.setenv("VSA_CMS_API_ENDPOINT", _CMS_BASE)
        result = executor.dispatch(_ACTION, _CLAIMS)

    assert result.status == ActionStatus.COMPLETED
    assert result.failure_kind is None


@rsps.activate
def test_403_is_a_terminal_client_error_that_escalates() -> None:
    """CMS 403 on apply_cost_action → failed (client_error), escalates, never retried.

    Stage A's single classification (base.classify_status): 401/403/404/405/415 are our
    token or our plumbing.  AVX already checked the operator's fleet standing before
    dispatch, so a CMS 403 means the two systems disagree, which an operator must see;
    recording it as "CMS refused" would close the item silently (decisions.md 2026-10-05
    "Group 3 review cycle 1" item 8).
    """
    rsps.add(rsps.POST, _COST_ACTIONS_ENDPOINT,
             json={"error": "vehicle_not_in_fleet"}, status=403)

    executor = _make_executor()
    with pytest.MonkeyPatch().context() as mp:
        mp.setenv("VSA_CMS_API_ENDPOINT", _CMS_BASE)
        result = executor.dispatch(_ACTION, _CLAIMS)

    assert result.status == ActionStatus.FAILED, f"CMS 403 must be failed, got {result.status!r}"
    assert result.failure_kind != FailureKind.TARGET_REFUSED
    assert result.last_error_status == 403
    assert result.retry_count == 0, "a client error is never retried"
    assert result.escalate is True, "a CMS 403 after AVX's own standing check must escalate"
    assert len(rsps.calls) == 1


@rsps.activate
def test_422_maps_to_declined_target_refused() -> None:
    """CMS 422 on apply_cost_action → declined / target_refused (spec D9)."""
    rsps.add(rsps.POST, _COST_ACTIONS_ENDPOINT,
             json={"error": "action_not_applicable"}, status=422)

    executor = _make_executor()
    with pytest.MonkeyPatch().context() as mp:
        mp.setenv("VSA_CMS_API_ENDPOINT", _CMS_BASE)
        result = executor.dispatch(_ACTION, _CLAIMS)

    assert result.status == ActionStatus.DECLINED
    assert result.failure_kind == FailureKind.TARGET_REFUSED
    assert result.last_error_status == 422


@rsps.activate
def test_400_maps_to_declined_target_refused() -> None:
    """CMS 400 on apply_cost_action → declined / target_refused (spec D9 override).

    Unlike DmsExecutor (where 400 is executor_terminal_error), spec D9 is
    explicit: 'CMS answering 4xx is declined with target_refused'.
    """
    rsps.add(rsps.POST, _COST_ACTIONS_ENDPOINT,
             json={"error": "invalid_month_format"}, status=400)

    executor = _make_executor()
    with pytest.MonkeyPatch().context() as mp:
        mp.setenv("VSA_CMS_API_ENDPOINT", _CMS_BASE)
        result = executor.dispatch(_ACTION, _CLAIMS)

    assert result.status == ActionStatus.DECLINED
    assert result.failure_kind == FailureKind.TARGET_REFUSED
    assert result.last_error_status == 400


@rsps.activate
def test_5xx_exhausted_maps_to_failed_exhausted_retries() -> None:
    """CMS 503 repeated → failed / executor_exhausted_retries (spec D9).

    Spec D9: "5xx after retries is failed".
    """
    # Three 503s: attempt 0, 1, 2 (retry_count=3 means 3 attempts total).
    for _ in range(3):
        rsps.add(rsps.POST, _COST_ACTIONS_ENDPOINT,
                 json={"error": "service_unavailable"}, status=503)

    executor = _make_executor(
        retry_policy=RetryPolicy(retry_count=3, backoff_base_ms=0, backoff_cap_ms=0)
    )
    with pytest.MonkeyPatch().context() as mp:
        mp.setenv("VSA_CMS_API_ENDPOINT", _CMS_BASE)
        result = executor.dispatch(_ACTION, _CLAIMS)

    assert result.status == ActionStatus.FAILED, (
        f"Expected status=failed after 503 retries exhausted, got {result.status!r}. "
        "Spec D9: 5xx after retries is failed/executor_exhausted_retries."
    )
    assert result.failure_kind == FailureKind.EXECUTOR_EXHAUSTED_RETRIES
    assert result.escalate is True, "failed must escalate per contract Addendum D5"
    assert result.retry_count == 3
    assert len(rsps.calls) == 3, "executor must have retried on 5xx"


@rsps.activate
def test_5xx_then_2xx_succeeds() -> None:
    """Transient 5xx followed by 2xx → COMPLETED (retry recovered)."""
    rsps.add(rsps.POST, _COST_ACTIONS_ENDPOINT, json={}, status=503)
    rsps.add(rsps.POST, _COST_ACTIONS_ENDPOINT,
             json={"record_id": "car-003"}, status=200)

    executor = _make_executor()
    with pytest.MonkeyPatch().context() as mp:
        mp.setenv("VSA_CMS_API_ENDPOINT", _CMS_BASE)
        result = executor.dispatch(_ACTION, _CLAIMS)

    assert result.status == ActionStatus.COMPLETED
    assert len(rsps.calls) == 2, "executor must have retried after the 503"


def test_missing_jwt_maps_to_terminal_error() -> None:
    """A missing JWT is a local terminal error — do not let CMS produce the 401.

    Contract § 10, spec D9: a missing JWT means the operator was absent at our
    end; forwarding an empty header and recording a CMS 401 as 'declined' would
    misstate what happened (spec D9: 4xx means CMS refused, not we were broken).
    """
    claims_no_token: dict[str, Any] = {"sub": "op-sub-0001"}
    executor = _make_executor()
    result = executor.dispatch(_ACTION, claims_no_token)

    assert result.status == ActionStatus.FAILED
    assert result.failure_kind == FailureKind.EXECUTOR_TERMINAL_ERROR
    assert result.last_error_class == "MissingJwt"
    assert result.escalate is True


def test_other_cms_kind_is_refused() -> None:
    """An unimplemented CMS kind (e.g. exempt_recall) → declined / target_refused.

    Defence-in-depth: the approve route's policy.executable=False guards this
    first, but the executor refuses independently (Stage A behaviour preserved).
    The example kind was move_vehicles until FI stage E task 4.3 made that kind
    executable (2026-10-10); exempt_recall is still unimplemented, so the property
    this test guards is unchanged.
    """
    action_other_kind: dict[str, Any] = {
        **_ACTION,
        "action_kind": "exempt_recall",
    }
    executor = _make_executor()
    result = executor.dispatch(action_other_kind, _CLAIMS)

    assert result.status == ActionStatus.DECLINED
    assert result.failure_kind == FailureKind.TARGET_REFUSED
    assert result.escalate is False


def test_missing_vehicle_vin_is_terminal_error() -> None:
    """A missing vehicle_vin is a local terminal error (incomplete request from our side)."""
    action_no_vin: dict[str, Any] = {
        **_ACTION,
        "parameters": {
            "cost_action": "reassign_or_remarket",
            "month": "2026-08",
        },
    }
    executor = _make_executor()
    result = executor.dispatch(action_no_vin, _CLAIMS)

    assert result.status == ActionStatus.FAILED
    assert result.failure_kind == FailureKind.EXECUTOR_TERMINAL_ERROR
    assert result.last_error_class == "MissingDispatchParameter"


def test_invalid_cost_action_is_terminal_error() -> None:
    """An unknown cost_action value is a local terminal error (not a CMS refusal)."""
    action_bad: dict[str, Any] = {
        **_ACTION,
        "parameters": {
            **_ACTION["parameters"],
            "cost_action": "detonate_engine",  # not in the closed list
        },
    }
    executor = _make_executor()
    result = executor.dispatch(action_bad, _CLAIMS)

    assert result.status == ActionStatus.FAILED
    assert result.failure_kind == FailureKind.EXECUTOR_TERMINAL_ERROR


# ---------------------------------------------------------------------------
# Mutation-check tests for operator_required and auto_approvable (task 3.3 Constraints)
#
# These tests assert the LIVE policy values.  The hand-back documents the
# mutation procedure: copy policy.py to /tmp/…-mut/, flip the field, run pytest
# with the copy swapped in, confirm the test below fails.
# ---------------------------------------------------------------------------


def test_operator_required_is_true_for_apply_cost_action() -> None:
    """apply_cost_action.operator_required is True (spec D9).

    Mutation check: set operator_required=False in a copy; this test fails.
    Only fleet operators may approve a cost action — no service identity or
    auto-approval rule may do so (spec D9: "no rule can approve a cost action").
    """
    from agents.executors.policy import ACTION_KINDS

    row = ACTION_KINDS["apply_cost_action"]
    assert row.operator_required is True, (
        "apply_cost_action.operator_required must be True (spec D9 / contract § 4). "
        "Only fleet operators may decide a cost action. "
        "Mutate to False to confirm this test catches the flip."
    )


def test_auto_approvable_is_false_for_apply_cost_action() -> None:
    """apply_cost_action.auto_approvable is False (spec D9).

    Mutation check: set auto_approvable=True in a copy; this test fails.
    Spec D9: "no rule can approve a cost action" — auto-approval is disabled.
    """
    from agents.executors.policy import ACTION_KINDS

    row = ACTION_KINDS["apply_cost_action"]
    assert row.auto_approvable is False, (
        "apply_cost_action.auto_approvable must be False (spec D9). "
        "No auto-approval rule may decide a cost action. "
        "Mutate to True to confirm this test catches the flip."
    )


def test_apply_cost_action_target_is_cms() -> None:
    """apply_cost_action.target_system is 'cms' (spec D9 / contract § 4, task 3.3)."""
    from agents.executors.policy import ACTION_KINDS

    row = ACTION_KINDS["apply_cost_action"]
    assert row.target_system == "cms", (
        "apply_cost_action.target_system must be 'cms' after Stage B lands (spec D9). "
        f"Got {row.target_system!r}."
    )


def test_apply_cost_action_is_executable() -> None:
    """apply_cost_action.executable is True after Stage B lands (task 3.3)."""
    from agents.executors.policy import ACTION_KINDS

    row = ACTION_KINDS["apply_cost_action"]
    assert row.executable is True, (
        "apply_cost_action.executable must be True in Stage B (task 3.3). "
        f"Got {row.executable!r}."
    )


# ---------------------------------------------------------------------------
# Request body construction
# ---------------------------------------------------------------------------


@rsps.activate
def test_request_body_carries_required_fields() -> None:
    """The HTTP body sent to CMS carries vehicle_vin, cost_action, month."""
    import json as _json

    rsps.add(rsps.POST, _COST_ACTIONS_ENDPOINT,
             json={"record_id": "car-010"}, status=200)

    executor = _make_executor()
    with pytest.MonkeyPatch().context() as mp:
        mp.setenv("VSA_CMS_API_ENDPOINT", _CMS_BASE)
        executor.dispatch(_ACTION, _CLAIMS)

    assert len(rsps.calls) == 1
    sent = _json.loads(rsps.calls[0].request.body)
    assert sent["vehicle_vin"] == _VIN
    assert sent["cost_action"] == "reassign_or_remarket"
    assert sent["month"] == "2026-08"
    assert sent["cause"] == "low_utilisation"
    assert sent["finding_id"] == _FINDING_ID


@rsps.activate
def test_request_carries_authorization_header() -> None:
    """The operator's token is forwarded in the Authorization header (spec D9)."""
    rsps.add(rsps.POST, _COST_ACTIONS_ENDPOINT,
             json={"record_id": "car-011"}, status=200)

    executor = _make_executor()
    with pytest.MonkeyPatch().context() as mp:
        mp.setenv("VSA_CMS_API_ENDPOINT", _CMS_BASE)
        executor.dispatch(_ACTION, _CLAIMS)

    assert len(rsps.calls) == 1
    auth = rsps.calls[0].request.headers.get("Authorization", "")
    assert auth == "Bearer Bearer.test.operator.jwt"


@rsps.activate
def test_id_token_used_when_token_absent() -> None:
    """If 'token' is absent, the executor falls back to 'id_token' in claims."""
    rsps.add(rsps.POST, _COST_ACTIONS_ENDPOINT,
             json={"record_id": "car-012"}, status=200)

    claims_id_token = {"sub": "op-sub-0001", "id_token": "Bearer.id.token.jwt"}
    executor = _make_executor()
    with pytest.MonkeyPatch().context() as mp:
        mp.setenv("VSA_CMS_API_ENDPOINT", _CMS_BASE)
        result = executor.dispatch(_ACTION, claims_id_token)

    assert result.status == ActionStatus.COMPLETED
    auth = rsps.calls[0].request.headers.get("Authorization", "")
    assert auth == "Bearer Bearer.id.token.jwt"



# ---------------------------------------------------------------------------
# Transient 4xx retry (M3 catch) — review Cycle 1 Warning W3
# ---------------------------------------------------------------------------


@rsps.activate
def test_429_then_200_is_completed() -> None:
    """CMS 429 then 200 → COMPLETED with 2 HTTP calls.

    Mutation M3: `if False and disposition == "transient":` — the 429 falls to
    refused(429), which ExecuteResult rejects (429 in _NON_REFUSAL_4XX), and
    @never_raises converts that to failed.  This test catches M3.
    """
    rsps.add(rsps.POST, _COST_ACTIONS_ENDPOINT, json={"error": "rate_limited"}, status=429)
    rsps.add(rsps.POST, _COST_ACTIONS_ENDPOINT, json={"record_id": "car-020"}, status=200)

    executor = _make_executor(
        retry_policy=RetryPolicy(retry_count=3, backoff_base_ms=0, backoff_cap_ms=0)
    )
    with pytest.MonkeyPatch().context() as mp:
        mp.setenv("VSA_CMS_API_ENDPOINT", _CMS_BASE)
        result = executor.dispatch(_ACTION, _CLAIMS)

    assert result.status == ActionStatus.COMPLETED, (
        f"Expected completed after 429 then 200, got {result.status!r}. "
        "429 is transient (rate-limit, not a refusal) and must be retried."
    )
    assert len(rsps.calls) == 2, (
        f"Expected 2 HTTP calls (retry after 429), got {len(rsps.calls)}"
    )


@rsps.activate
def test_429_x3_is_exhausted() -> None:
    """Three consecutive 429s → failed / executor_exhausted_retries.

    Mutation M3 also caught here: without retry, only 1 attempt would be recorded.
    """
    for _ in range(3):
        rsps.add(rsps.POST, _COST_ACTIONS_ENDPOINT, json={"error": "rate_limited"}, status=429)

    executor = _make_executor(
        retry_policy=RetryPolicy(retry_count=3, backoff_base_ms=0, backoff_cap_ms=0)
    )
    with pytest.MonkeyPatch().context() as mp:
        mp.setenv("VSA_CMS_API_ENDPOINT", _CMS_BASE)
        result = executor.dispatch(_ACTION, _CLAIMS)

    assert result.status == ActionStatus.FAILED
    assert result.failure_kind == FailureKind.EXECUTOR_EXHAUSTED_RETRIES
    assert result.escalate is True
    assert len(rsps.calls) == 3, "executor must have retried on 429"


@rsps.activate
def test_408_then_200_is_completed() -> None:
    """CMS 408 (Request Timeout) then 200 → COMPLETED (transient, retried)."""
    rsps.add(rsps.POST, _COST_ACTIONS_ENDPOINT, json={}, status=408)
    rsps.add(rsps.POST, _COST_ACTIONS_ENDPOINT, json={"record_id": "car-021"}, status=200)

    executor = _make_executor()
    with pytest.MonkeyPatch().context() as mp:
        mp.setenv("VSA_CMS_API_ENDPOINT", _CMS_BASE)
        result = executor.dispatch(_ACTION, _CLAIMS)

    assert result.status == ActionStatus.COMPLETED
    assert len(rsps.calls) == 2


# ---------------------------------------------------------------------------
# Connection error retry (M4 catch) — review Cycle 1 Warning W3
# ---------------------------------------------------------------------------


def test_connection_error_x3_is_exhausted() -> None:
    """Three consecutive ConnectionErrors → failed / executor_exhausted_retries.

    Mutation M4: `if False and is_transient_exception(exc):` — the first error
    goes to terminal_error immediately.  This test catches M4 (retry_count=3
    means 3 attempts; without retry only 1 would be made).
    """
    from unittest.mock import patch
    from requests.exceptions import ConnectionError as CxnError

    executor = _make_executor(
        retry_policy=RetryPolicy(retry_count=3, backoff_base_ms=0, backoff_cap_ms=0)
    )
    call_count = 0

    def _raise(*_a: object, **_kw: object) -> None:
        nonlocal call_count
        call_count += 1
        raise CxnError("stubbed ConnectionError")

    with pytest.MonkeyPatch().context() as mp:
        mp.setenv("VSA_CMS_API_ENDPOINT", _CMS_BASE)
        with patch("agents.executors.cms.requests.post", side_effect=_raise):
            result = executor.dispatch(_ACTION, _CLAIMS)

    assert result.status == ActionStatus.FAILED, (
        f"Expected failed after 3 ConnectionErrors, got {result.status!r}"
    )
    assert result.failure_kind == FailureKind.EXECUTOR_EXHAUSTED_RETRIES
    assert result.escalate is True
    assert call_count == 3, (
        f"Expected 3 connection attempts (retry on ConnectionError), got {call_count}"
    )


def test_connection_error_then_200_is_completed() -> None:
    """ConnectionError then 200 → COMPLETED (retry recovered the connection).

    Catches M4: without retry, the first ConnectionError would produce terminal_error.
    """
    import responses as rsps_mod  # noqa: PLC0415
    from unittest.mock import patch

    from requests.exceptions import ConnectionError as CxnError

    executor = _make_executor(
        retry_policy=RetryPolicy(retry_count=3, backoff_base_ms=0, backoff_cap_ms=0)
    )
    call_count = 0

    def _side_effect(*_a: object, **_kw: object) -> Any:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise CxnError("stubbed transient failure")
        # Mimic a successful `requests.Response`
        import requests as req  # noqa: PLC0415
        r = req.Response()
        r.status_code = 200
        r._content = b'{"record_id": "car-022"}'
        return r

    with pytest.MonkeyPatch().context() as mp:
        mp.setenv("VSA_CMS_API_ENDPOINT", _CMS_BASE)
        with patch("agents.executors.cms.requests.post", side_effect=_side_effect):
            result = executor.dispatch(_ACTION, _CLAIMS)

    assert result.status == ActionStatus.COMPLETED, (
        f"Expected completed after ConnectionError then 200, got {result.status!r}"
    )
    assert call_count == 2


# ---------------------------------------------------------------------------
# Client-error 4xx → terminal_error (review Cycle 1 Warning W3)
# ---------------------------------------------------------------------------


@rsps.activate
def test_401_is_terminal_error() -> None:
    """CMS 401 → terminal_error / executor_terminal_error (not declined).

    base.CLIENT_ERROR_4XX: 401 means our token had expired or our auth is mis-wired,
    not that CMS refused the fleet operation. Recording it as `declined` would tell
    the owner 'CMS refused' and escalate to nobody.
    """
    rsps.add(rsps.POST, _COST_ACTIONS_ENDPOINT, json={"error": "unauthorized"}, status=401)

    executor = _make_executor()
    with pytest.MonkeyPatch().context() as mp:
        mp.setenv("VSA_CMS_API_ENDPOINT", _CMS_BASE)
        result = executor.dispatch(_ACTION, _CLAIMS)

    assert result.status == ActionStatus.FAILED, (
        f"Expected failed for CMS 401 (our fault), got {result.status!r}. "
        "401 must map to terminal_error so escalation fires."
    )
    assert result.failure_kind == FailureKind.EXECUTOR_TERMINAL_ERROR
    assert result.last_error_status == 401
    assert result.escalate is True, "terminal_error must escalate"


@rsps.activate
def test_404_is_terminal_error() -> None:
    """CMS 404 → terminal_error / executor_terminal_error (not declined).

    A 404 from CMS means the route does not exist (endpoint mis-wired or CMS not
    yet deployed), not that CMS refused the cost action on fleet-policy grounds.
    base.CLIENT_ERROR_4XX includes 404 for this reason.
    """
    rsps.add(rsps.POST, _COST_ACTIONS_ENDPOINT, json={"error": "not_found"}, status=404)

    executor = _make_executor()
    with pytest.MonkeyPatch().context() as mp:
        mp.setenv("VSA_CMS_API_ENDPOINT", _CMS_BASE)
        result = executor.dispatch(_ACTION, _CLAIMS)

    assert result.status == ActionStatus.FAILED
    assert result.failure_kind == FailureKind.EXECUTOR_TERMINAL_ERROR
    assert result.last_error_status == 404
    assert result.escalate is True


# ---------------------------------------------------------------------------
# Local validation — all 5 required fields (review Cycle 1 Warning W4)
# ---------------------------------------------------------------------------


def test_missing_cause_is_terminal_error() -> None:
    """A missing cause is a local terminal error (not a CMS refusal).

    CMS requires cause (task 1.6 red tests). A missing or unknown cause makes
    our request invalid locally; catching it here prevents a CMS 400 from being
    recorded as declined/target_refused.
    """
    action_no_cause: dict[str, Any] = {
        **_ACTION,
        "parameters": {
            "vehicle_vin": _VIN,
            "cost_action": "reassign_or_remarket",
            "month": "2026-08",
            # cause omitted
        },
    }
    executor = _make_executor()
    result = executor.dispatch(action_no_cause, _CLAIMS)

    assert result.status == ActionStatus.FAILED
    assert result.failure_kind == FailureKind.EXECUTOR_TERMINAL_ERROR
    assert result.last_error_class == "MissingDispatchParameter"


def test_unknown_cause_is_terminal_error() -> None:
    """A cause outside the six valid values is caught locally (not CMS-bound)."""
    action_bad_cause: dict[str, Any] = {
        **_ACTION,
        "parameters": {
            **_ACTION["parameters"],
            "cause": "magic",  # not in VALID_CAUSES
        },
    }
    executor = _make_executor()
    result = executor.dispatch(action_bad_cause, _CLAIMS)

    assert result.status == ActionStatus.FAILED
    assert result.failure_kind == FailureKind.EXECUTOR_TERMINAL_ERROR
    assert result.last_error_class == "MissingDispatchParameter"


def test_missing_finding_id_is_terminal_error() -> None:
    """A missing finding_id is a local terminal error.

    CMS requires finding_id for idempotency (decisions item 6). Without it,
    CMS would answer 400, which would be recorded as declined.
    """
    action_no_fid: dict[str, Any] = {k: v for k, v in _ACTION.items() if k != "finding_id"}
    executor = _make_executor()
    result = executor.dispatch(action_no_fid, _CLAIMS)

    assert result.status == ActionStatus.FAILED
    assert result.failure_kind == FailureKind.EXECUTOR_TERMINAL_ERROR
    assert result.last_error_class == "MissingDispatchParameter"


def test_bad_month_format_is_terminal_error() -> None:
    """A month that does not match YYYY-MM is a local terminal error.

    CMS requires month matching ``^\\d{4}-(0[1-9]|1[0-2])$`` (task 1.6 red tests).
    """
    action_bad_month: dict[str, Any] = {
        **_ACTION,
        "parameters": {
            **_ACTION["parameters"],
            "month": "2026-8",  # not zero-padded → fails CMS regex
        },
    }
    executor = _make_executor()
    result = executor.dispatch(action_bad_month, _CLAIMS)

    assert result.status == ActionStatus.FAILED
    assert result.failure_kind == FailureKind.EXECUTOR_TERMINAL_ERROR
    assert result.last_error_class == "MissingDispatchParameter"


def test_month_13_is_terminal_error() -> None:
    """month=2026-13 (invalid month number) is caught locally."""
    action_bad_month: dict[str, Any] = {
        **_ACTION,
        "parameters": {
            **_ACTION["parameters"],
            "month": "2026-13",
        },
    }
    executor = _make_executor()
    result = executor.dispatch(action_bad_month, _CLAIMS)

    assert result.status == ActionStatus.FAILED
    assert result.failure_kind == FailureKind.EXECUTOR_TERMINAL_ERROR


# ---------------------------------------------------------------------------
# Missing endpoint guard
# ---------------------------------------------------------------------------


def test_missing_cms_endpoint_is_terminal_error() -> None:
    """VSA_CMS_API_ENDPOINT unset → terminal_error with MissingCmsEndpoint.

    @never_raises catches the ValueError from _cms_cost_actions_endpoint and
    maps it to failed/executor_terminal_error.
    """
    executor = _make_executor()
    with pytest.MonkeyPatch().context() as mp:
        mp.delenv("VSA_CMS_API_ENDPOINT", raising=False)
        result = executor.dispatch(_ACTION, _CLAIMS)

    assert result.status == ActionStatus.FAILED
    assert result.failure_kind == FailureKind.EXECUTOR_TERMINAL_ERROR
    assert result.escalate is True


# ---------------------------------------------------------------------------
# Endpoint scheme guard (FI cost Stage B Group 4 security review cycle 1)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("endpoint", [
    "http://cms-staging.test",                 # plaintext: the token would travel in clear
    "cms-staging.test",                        # no scheme
    "https://",                                # no host
    "https://user:pass@cms-staging.test",      # credentials in the URL
    "ftp://cms-staging.test",
])
def test_a_non_https_cms_endpoint_sends_nothing_and_is_terminal(endpoint: str) -> None:
    """The request carries the operator's bearer token: anything but https:// to a named
    host must fail before a request is made, with the reason named and the value not."""
    from unittest.mock import patch

    executor = _make_executor()
    with pytest.MonkeyPatch().context() as mp, \
            patch("agents.executors.cms.requests.post") as post:
        mp.setenv("VSA_CMS_API_ENDPOINT", endpoint)
        result = executor.dispatch(_ACTION, _CLAIMS)
    post.assert_not_called()
    assert result.status == ActionStatus.FAILED
    assert result.failure_kind == FailureKind.EXECUTOR_TERMINAL_ERROR
    assert result.last_error_class == "ValueError"      # base.never_raises withholds the text
    assert "pass@" not in str(result) and "cms-staging.test" not in str(result)


def test_the_endpoint_guard_names_the_problem_not_the_value() -> None:
    from agents.executors.cms import _cms_cost_actions_endpoint

    with pytest.MonkeyPatch().context() as mp:
        mp.setenv("VSA_CMS_API_ENDPOINT", "http://user:secret@cms-staging.test")
        with pytest.raises(ValueError) as exc:
            _cms_cost_actions_endpoint()
    assert str(exc.value).startswith("InvalidCmsEndpoint")
    assert "secret" not in str(exc.value) and "cms-staging.test" not in str(exc.value)
    with pytest.MonkeyPatch().context() as mp:
        mp.setenv("VSA_CMS_API_ENDPOINT", "https://cms-staging.test/")
        assert _cms_cost_actions_endpoint() == _COST_ACTIONS_ENDPOINT
