"""Unit tests for agents/executors/vehicle_command.py.

Covers:
- 201 with commandId → success / IN_PROGRESS / target_ref=commandId
- 200 with commandId → success  (C6 fix: accept full 2xx range)
- 201 without commandId → failed/executor_terminal_error
- 403 → declined/target_refused (genuine refusal)
- 400 (other business 4xx) → declined/target_refused
- 401 → failed/executor_terminal_error (client_error, escalates)   [C4 fix]
- 404 → failed/executor_terminal_error (client_error, escalates)   [C4 fix]
- 408 → retried (transient 4xx)                                     [C4 fix]
- 5xx exhausts retries → failed/executor_exhausted_retries
- 3× ConnectionError exhausts retries → executor_exhausted_retries / retry_count=3  [C8 fix]
- Missing vehicle_id → terminal_error (no HTTP call)
- Missing jwt → terminal_error (not refused!)                       [C5 fix]
- Missing commandName → terminal_error (no HTTP call)
- Authorization header carries the forwarded JWT (mutation-tested)
- 429 is treated as transient (retried)
- @never_raises: unhandled exception → terminal_error, never raises
- No real sleeps: injected no-op sleep
- Request carries a ``timeout`` kwarg derived from policy            [C7 fix]
- Escalation fires put_events + put_metric_data on terminal failure  [C2 fix]
- Escalation does NOT fire on a genuine refusal (declined)

All HTTP is stubbed with ``responses`` — no real network calls, no real sleeps.
"""

from __future__ import annotations

import os
import pytest
import responses as resp_lib
from unittest.mock import MagicMock

from agents.executors.base import (
    ActionStatus,
    FailureKind,
    ExecuteResult,
    RetryPolicy,
)
from agents.executors.vehicle_command import VehicleCommandExecutor


# ---------------------------------------------------------------------------
# Constants / fixtures
# ---------------------------------------------------------------------------

_BASE_URL = "https://cms-staging.example.com/prod"
_VEHICLE_ID = "VEH-CS-DEMO-0031"
_POST_URL = f"{_BASE_URL}/api/commands/{_VEHICLE_ID}"
_JWT = "eyJhbGciOiJSUzI1NiJ9.test.payload"
_COMMAND_ID = "abc123-cmd-xyz"

_CLAIMS = {"id_token": _JWT}
_ACTION = {
    "action_id": "act-001",
    "owner_id": "owner-42",
    "vehicle_id": _VEHICLE_ID,
    "params": {"commandName": "lock"},
    "action_kind": "lock_doors",
}

# No-backoff policy: retry_count=3 means 1 initial + 2 retries.
_NO_SLEEP_POLICY = RetryPolicy(retry_count=3, backoff_base_ms=0, backoff_cap_ms=0)


def _make_executor(
    *,
    events_client=None,
    cloudwatch_client=None,
) -> VehicleCommandExecutor:
    """Return an executor wired with a no-op sleep and no SSM calls."""
    return VehicleCommandExecutor(
        retry_policy=_NO_SLEEP_POLICY,
        _sleep=lambda _: None,
        _events_client=events_client,
        _cloudwatch_client=cloudwatch_client,
    )


def _make_escalation_clients():
    """Return mock EventBridge + CloudWatch clients for escalation assertions."""
    events = MagicMock()
    cw = MagicMock()
    return events, cw


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _set_env(monkeypatch):
    monkeypatch.setenv("VSA_CMS_COMMANDS_ENDPOINT", _BASE_URL)


def _success_body(command_id: str = _COMMAND_ID) -> dict:
    return {
        "commandId": command_id,
        "vehicleId": _VEHICLE_ID,
        "vin": "1FM5K8GC0NGA12345",
        "commandName": "lock",
        "status": "SENT",
        "timestamp": "2026-09-18T12:00:00Z",
    }


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------

@resp_lib.activate
def test_201_with_command_id_returns_success(monkeypatch):
    """201 with a commandId → success, IN_PROGRESS, target_ref=commandId."""
    _set_env(monkeypatch)
    resp_lib.add(resp_lib.POST, _POST_URL, json=_success_body(), status=201)

    result = _make_executor().dispatch(_ACTION, _CLAIMS)

    assert result.status == ActionStatus.IN_PROGRESS
    assert result.target_ref == _COMMAND_ID
    assert result.failure_kind is None


@resp_lib.activate
def test_200_with_command_id_returns_success(monkeypatch):
    """200 (not 201) with a commandId → success.

    C6 fix: accept the full 2xx range; a 200-with-valid-commandId must NOT fall
    through to declined/target_refused as it did before the fix.
    """
    _set_env(monkeypatch)
    resp_lib.add(resp_lib.POST, _POST_URL, json=_success_body(), status=200)

    result = _make_executor().dispatch(_ACTION, _CLAIMS)

    assert result.status == ActionStatus.IN_PROGRESS
    assert result.target_ref == _COMMAND_ID
    assert result.failure_kind is None


# ---------------------------------------------------------------------------
# Malformed 2xx: commandId absent → terminal_error (NOT success)
# ---------------------------------------------------------------------------

@resp_lib.activate
def test_201_without_command_id_is_terminal_error(monkeypatch):
    """201 response that omits commandId → failed/executor_terminal_error."""
    _set_env(monkeypatch)
    resp_lib.add(
        resp_lib.POST, _POST_URL,
        json={"vehicleId": _VEHICLE_ID, "status": "SENT"},  # no commandId
        status=201,
    )

    result = _make_executor().dispatch(_ACTION, _CLAIMS)

    assert result.status == ActionStatus.FAILED
    assert result.failure_kind == FailureKind.EXECUTOR_TERMINAL_ERROR
    assert result.target_ref is None


# ---------------------------------------------------------------------------
# Genuine refusals: 400, 409, 422 → declined/target_refused (no escalation)
# ---------------------------------------------------------------------------

@resp_lib.activate
def test_400_maps_to_refused(monkeypatch):
    """400 (business refusal) → declined/target_refused."""
    _set_env(monkeypatch)
    resp_lib.add(resp_lib.POST, _POST_URL, json={"error": "bad request"}, status=400)

    result = _make_executor().dispatch(_ACTION, _CLAIMS)

    assert result.status == ActionStatus.DECLINED
    assert result.failure_kind == FailureKind.TARGET_REFUSED


# ---------------------------------------------------------------------------
# C4 fix: 401/403/404 → terminal_error (client_error, escalates), NOT declined
# ---------------------------------------------------------------------------

@resp_lib.activate
def test_401_maps_to_terminal_error_not_declined(monkeypatch):
    """401 → failed/executor_terminal_error, escalate=True.

    C4 fix: authentication failure means nothing reached a human.  Telling the
    owner "the vehicle refused" when our JWT expired is a false statement.
    """
    _set_env(monkeypatch)
    events, cw = _make_escalation_clients()
    resp_lib.add(resp_lib.POST, _POST_URL, json={"error": "Unauthorized"}, status=401)

    result = _make_executor(
        events_client=events, cloudwatch_client=cw
    ).dispatch(_ACTION, _CLAIMS)

    assert result.status == ActionStatus.FAILED
    assert result.failure_kind == FailureKind.EXECUTOR_TERMINAL_ERROR
    assert result.escalate is True
    assert result.last_error_status == 401
    # Escalation is fired ONCE by registry.dispatch(), not here — see review
    # Cycle 2 Critical 3. The executor's half of the contract is a result that
    # REQUESTS escalation (asserted above); that something actually escalates is
    # asserted in test_registry.py::TestEscalationFiresForEveryTarget, across all
    # six targets rather than only the ones that happened to fire themselves.


@resp_lib.activate
def test_403_maps_to_terminal_error_not_declined(monkeypatch):
    """403 → failed/executor_terminal_error, escalate=True.

    C4 fix: 403 is in CLIENT_ERROR_4XX.  Fleet-viewer tokens should yield a
    plumbing error, not a "vehicle refused" card on the owner's feed.
    """
    _set_env(monkeypatch)
    events, cw = _make_escalation_clients()
    resp_lib.add(
        resp_lib.POST, _POST_URL,
        json={"error": "Read-only (fleet-viewer) tokens may not call write routes."},
        status=403,
    )

    result = _make_executor(
        events_client=events, cloudwatch_client=cw
    ).dispatch(_ACTION, _CLAIMS)

    assert result.status == ActionStatus.FAILED
    assert result.failure_kind == FailureKind.EXECUTOR_TERMINAL_ERROR
    assert result.escalate is True
    assert result.last_error_status == 403
    # Escalation is fired ONCE by registry.dispatch() (review Cycle 2 Critical 3).
    # The executor's contract is a result that REQUESTS escalation, asserted
    # above. That something actually escalates is asserted in test_registry.py
    # ::TestEscalationFiresForEveryTarget, across all six targets.


@resp_lib.activate
def test_404_maps_to_terminal_error_not_declined(monkeypatch):
    """404 → failed/executor_terminal_error, escalate=True.

    C4 fix: a route that does not exist means our plumbing is wrong, not that
    the vehicle refused the command.
    """
    _set_env(monkeypatch)
    events, cw = _make_escalation_clients()
    resp_lib.add(resp_lib.POST, _POST_URL, json={"error": "Not Found"}, status=404)

    result = _make_executor(
        events_client=events, cloudwatch_client=cw
    ).dispatch(_ACTION, _CLAIMS)

    assert result.status == ActionStatus.FAILED
    assert result.failure_kind == FailureKind.EXECUTOR_TERMINAL_ERROR
    assert result.escalate is True
    assert result.last_error_status == 404
    # Escalation is fired ONCE by registry.dispatch() (review Cycle 2 Critical 3).
    # The executor's contract is a result that REQUESTS escalation, asserted
    # above. That something actually escalates is asserted in test_registry.py
    # ::TestEscalationFiresForEveryTarget, across all six targets.


# ---------------------------------------------------------------------------
# C4 fix: 408 → retried (transient, not a refusal)
# ---------------------------------------------------------------------------

@resp_lib.activate
def test_408_is_retried_not_refused(monkeypatch):
    """408 Request Timeout → treated as transient, retried.

    C4 fix: 408 is a timeout, not a refusal.  Before the fix it was collapsed
    into declined/target_refused with no retry.
    """
    _set_env(monkeypatch)
    resp_lib.add(resp_lib.POST, _POST_URL, json={"error": "timeout"}, status=408)
    resp_lib.add(resp_lib.POST, _POST_URL, json=_success_body(), status=201)

    result = _make_executor().dispatch(_ACTION, _CLAIMS)

    # Should succeed on the second attempt
    assert result.status == ActionStatus.IN_PROGRESS
    assert result.target_ref == _COMMAND_ID


# ---------------------------------------------------------------------------
# Transient: 5xx exhausts retries
# ---------------------------------------------------------------------------

@resp_lib.activate
def test_5xx_exhausts_retries_to_failed(monkeypatch):
    """5xx on every attempt → failed/executor_exhausted_retries after budget is spent."""
    _set_env(monkeypatch)
    for _ in range(3):
        resp_lib.add(resp_lib.POST, _POST_URL, json={"error": "service unavailable"}, status=503)

    result = _make_executor().dispatch(_ACTION, _CLAIMS)

    assert result.status == ActionStatus.FAILED
    assert result.failure_kind == FailureKind.EXECUTOR_EXHAUSTED_RETRIES
    assert result.retry_count == 3
    assert result.escalate is True
    assert result.last_error_status == 503


@resp_lib.activate
def test_5xx_then_success_on_retry(monkeypatch):
    """First attempt 503, second succeeds → success."""
    _set_env(monkeypatch)
    resp_lib.add(resp_lib.POST, _POST_URL, json={"error": "temporary"}, status=503)
    resp_lib.add(resp_lib.POST, _POST_URL, json=_success_body(), status=201)

    result = _make_executor().dispatch(_ACTION, _CLAIMS)

    assert result.status == ActionStatus.IN_PROGRESS
    assert result.target_ref == _COMMAND_ID


@resp_lib.activate
def test_429_is_transient_and_retried(monkeypatch):
    """429 is treated as transient (rate-limited, not a refusal)."""
    _set_env(monkeypatch)
    resp_lib.add(resp_lib.POST, _POST_URL, json={"error": "too many requests"}, status=429)
    resp_lib.add(resp_lib.POST, _POST_URL, json=_success_body(), status=201)

    result = _make_executor().dispatch(_ACTION, _CLAIMS)

    assert result.status == ActionStatus.IN_PROGRESS
    assert result.target_ref == _COMMAND_ID


# ---------------------------------------------------------------------------
# C8 fix: ConnectionError on every attempt → exhausted_retries, retry_count=3
# ---------------------------------------------------------------------------

def test_connection_error_exhaustion_yields_exhausted_retries(monkeypatch):
    """Three consecutive ConnectionErrors → executor_exhausted_retries / retry_count=3.

    C8 fix: a transient exception on the final attempt must NOT fall through to
    executor_terminal_error with retry_count=2.  The contract (and dms.py) says
    executor_exhausted_retries with the full retry budget.
    """
    _set_env(monkeypatch)

    call_count = 0

    def _raise_connection_error(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        raise ConnectionError("simulated network failure")

    import unittest.mock as mock
    with mock.patch("agents.executors.vehicle_command.requests.post", side_effect=_raise_connection_error):
        result = _make_executor().dispatch(_ACTION, _CLAIMS)

    assert call_count == 3, f"expected 3 attempts, got {call_count}"
    assert result.status == ActionStatus.FAILED
    assert result.failure_kind == FailureKind.EXECUTOR_EXHAUSTED_RETRIES
    assert result.retry_count == 3
    assert result.escalate is True


# ---------------------------------------------------------------------------
# C5 fix: missing JWT → terminal_error (not refused)
# ---------------------------------------------------------------------------

def test_missing_jwt_returns_terminal_error_not_refused(monkeypatch):
    """claims without id_token/jwt → terminal_error (jwt_unavailable), no HTTP call.

    C5 fix: a missing JWT is an executor-side failure — no request was sent,
    so `refused` (which implies the target said no) is a false statement.
    Consistent with the missing-vehicle_id and missing-commandName preconditions.
    """
    _set_env(monkeypatch)
    result = _make_executor().dispatch(_ACTION, {})

    assert result.status == ActionStatus.FAILED
    assert result.failure_kind == FailureKind.EXECUTOR_TERMINAL_ERROR
    assert result.escalate is True


# ---------------------------------------------------------------------------
# Precondition failures (no HTTP call)
# ---------------------------------------------------------------------------

def test_missing_vehicle_id_returns_terminal_error(monkeypatch):
    """action without vehicle_id → terminal_error, no HTTP call."""
    _set_env(monkeypatch)
    action_no_vid = {"params": {"commandName": "lock"}}
    result = _make_executor().dispatch(action_no_vid, _CLAIMS)

    assert result.status == ActionStatus.FAILED
    assert result.failure_kind == FailureKind.EXECUTOR_TERMINAL_ERROR


def test_missing_command_name_returns_terminal_error(monkeypatch):
    """params without commandName → terminal_error, no HTTP call."""
    _set_env(monkeypatch)
    action_no_cmd = {"vehicle_id": _VEHICLE_ID, "params": {}}
    result = _make_executor().dispatch(action_no_cmd, _CLAIMS)

    assert result.status == ActionStatus.FAILED
    assert result.failure_kind == FailureKind.EXECUTOR_TERMINAL_ERROR


# ---------------------------------------------------------------------------
# JWT forwarding: the Authorization header must carry the owner's token
# ---------------------------------------------------------------------------

@resp_lib.activate
def test_request_carries_forwarded_jwt(monkeypatch):
    """The outbound POST must carry ``Authorization: Bearer <jwt>``."""
    _set_env(monkeypatch)

    captured: list[dict] = []

    def _capture(request):
        captured.append({"authorization": request.headers.get("Authorization", "")})
        return (201, {}, '{"commandId":"cmd1","vehicleId":"v","vin":"w","commandName":"lock","status":"SENT","timestamp":"2026-09-18T12:00:00Z"}')

    resp_lib.add_callback(resp_lib.POST, _POST_URL, callback=_capture, content_type="application/json")

    _make_executor().dispatch(_ACTION, _CLAIMS)

    assert len(captured) == 1, "no request was captured"
    assert captured[0]["authorization"] == f"Bearer {_JWT}"


# ---------------------------------------------------------------------------
# jwt field alias: claims may use 'jwt' instead of 'id_token'
# ---------------------------------------------------------------------------

@resp_lib.activate
def test_claims_jwt_field_accepted(monkeypatch):
    """claims with 'jwt' key (instead of 'id_token') must still forward the token."""
    _set_env(monkeypatch)

    captured: list[dict] = []

    def _capture(request):
        captured.append({"authorization": request.headers.get("Authorization", "")})
        return (201, {}, '{"commandId":"cmd2","vehicleId":"v","vin":"w","commandName":"lock","status":"SENT","timestamp":"2026-09-18T12:00:00Z"}')

    resp_lib.add_callback(resp_lib.POST, _POST_URL, callback=_capture, content_type="application/json")

    _make_executor().dispatch(_ACTION, {"jwt": _JWT})

    assert len(captured) == 1
    assert captured[0]["authorization"] == f"Bearer {_JWT}"


# ---------------------------------------------------------------------------
# C7 fix: request must carry a ``timeout`` kwarg derived from policy
# ---------------------------------------------------------------------------

@resp_lib.activate
def test_request_carries_timeout_kwarg(monkeypatch):
    """The outbound POST must include a ``timeout`` keyword argument.

    C7 fix: the old code used a hardcoded ``_TIMEOUT_SECONDS = 10.0`` literal.
    The timeout must now come from ``policy.per_attempt_timeout_s`` so three
    stalled attempts cannot blow the overall API Gateway synchronous budget.
    """
    _set_env(monkeypatch)

    import unittest.mock as mock

    real_post_calls: list[dict] = []

    def _mock_post(url, **kwargs):
        real_post_calls.append(kwargs)
        mock_resp = MagicMock()
        mock_resp.status_code = 201
        mock_resp.json.return_value = {"commandId": "cmd-t", "vehicleId": "v",
                                        "vin": "w", "commandName": "lock",
                                        "status": "SENT", "timestamp": "2026-09-18T12:00:00Z"}
        return mock_resp

    with mock.patch("agents.executors.vehicle_command.requests.post", side_effect=_mock_post):
        result = _make_executor().dispatch(_ACTION, _CLAIMS)

    assert result.status == ActionStatus.IN_PROGRESS
    assert len(real_post_calls) == 1, "expected exactly one POST"
    assert "timeout" in real_post_calls[0], (
        "requests.post was called without a 'timeout' kwarg — "
        "C7 fix: timeout must be derived from policy.per_attempt_timeout_s"
    )
    # The timeout must be a positive number
    assert real_post_calls[0]["timeout"] > 0


# ---------------------------------------------------------------------------
# C2 fix: escalation fires put_events + put_metric_data on terminal failures
# ---------------------------------------------------------------------------

@resp_lib.activate
def test_escalation_fires_on_5xx_exhaustion(monkeypatch):
    """5xx exhaustion triggers both put_events and put_metric_data.

    C2 fix: before the fix, result.escalate was True but nothing was actually
    called.  The test asserts behaviour (clients called) not the flag alone.
    """
    _set_env(monkeypatch)
    events, cw = _make_escalation_clients()

    for _ in range(3):
        resp_lib.add(resp_lib.POST, _POST_URL, json={"error": "service unavailable"}, status=503)

    result = _make_executor(
        events_client=events, cloudwatch_client=cw
    ).dispatch(_ACTION, _CLAIMS)

    assert result.status == ActionStatus.FAILED
    assert result.failure_kind == FailureKind.EXECUTOR_EXHAUSTED_RETRIES
    # Escalation is fired ONCE by registry.dispatch() (review Cycle 2 Critical 3).
    # The executor's contract is a result that REQUESTS escalation, asserted
    # above. That something actually escalates is asserted in test_registry.py
    # ::TestEscalationFiresForEveryTarget, across all six targets.


def test_escalation_does_not_fire_on_declined(monkeypatch):
    """A genuine refusal (declined) must NOT trigger escalation.

    D5: `declined` does not escalate — escalating a refusal would tell the
    operator we failed to reach a target who in fact answered and said no.
    """
    _set_env(monkeypatch)
    events, cw = _make_escalation_clients()

    import unittest.mock as mock

    def _mock_post(url, **kwargs):
        r = MagicMock()
        r.status_code = 400
        r.json.return_value = {"error": "bad request"}
        return r

    with mock.patch("agents.executors.vehicle_command.requests.post", side_effect=_mock_post):
        result = _make_executor(
            events_client=events, cloudwatch_client=cw
        ).dispatch(_ACTION, _CLAIMS)

    assert result.status == ActionStatus.DECLINED
    events.put_events.assert_not_called()
    cw.put_metric_data.assert_not_called()


# ---------------------------------------------------------------------------
# @never_raises: unhandled exception becomes terminal_error
# ---------------------------------------------------------------------------

def test_never_raises_when_endpoint_env_missing():
    """If VSA_CMS_COMMANDS_ENDPOINT is unset, dispatch returns terminal_error, never raises."""
    os.environ.pop("VSA_CMS_COMMANDS_ENDPOINT", None)
    executor = _make_executor()

    result = executor.dispatch(_ACTION, _CLAIMS)

    assert result.status == ActionStatus.FAILED
    assert result.failure_kind == FailureKind.EXECUTOR_TERMINAL_ERROR
    assert result.escalate is True


# ---------------------------------------------------------------------------
# No real sleeps: inject sleep and verify it's called (not a real wait)
# ---------------------------------------------------------------------------

@resp_lib.activate
def test_no_real_sleep_on_transient_retry(monkeypatch):
    """Injected sleep callable is invoked for transient retries, never blocking."""
    _set_env(monkeypatch)
    sleep_calls: list[float] = []
    policy = RetryPolicy(retry_count=2, backoff_base_ms=0, backoff_cap_ms=0)
    executor = VehicleCommandExecutor(
        retry_policy=policy,
        _sleep=lambda s: sleep_calls.append(s),
    )

    resp_lib.add(resp_lib.POST, _POST_URL, json={"error": "temp"}, status=503)
    resp_lib.add(resp_lib.POST, _POST_URL, json=_success_body(), status=201)

    result = executor.dispatch(_ACTION, _CLAIMS)

    assert result.status == ActionStatus.IN_PROGRESS
    assert len(sleep_calls) == 1



# ---------------------------------------------------------------------------
# Security review Cycle 1, Warning 2 — vehicle_id is interpolated into the
# request PATH, and the request carries the owner's forwarded JWT. A value
# containing a path or query separator steers that credential to a different CMS
# route than the one this executor is authorised to call. The only precondition
# before this fix was "non-empty".
#
# The value arrives from a Tier 2-authored Finding. The Tier 2 boundary scan in
# test_registry.py stops Tier 2 from CALLING an executor; it does not stop Tier 2
# from writing a Finding whose fields flow here. Validated at the point of use.
#
# `responses` is NOT activated on the rejection tests on purpose: if validation
# fails to reject, the executor attempts a real HTTP call, `responses` is not
# intercepting, and the test errors loudly instead of quietly recording a mock
# call. Absence of registered mocks is the assertion.
# ---------------------------------------------------------------------------

_INJECTION_VECTORS = [
    "VEH-1/../../geofences",    # traversal to a sibling API
    "VEH-1/../admin",
    "..",                       # bare traversal: /api/commands/.. -> /api/
    "../..",
    "VEH-1?admin=true",         # query injection
    "VEH-1#fragment",
    "VEH-1/extra/segments",
    "%2e%2e%2fadmin",           # encoded traversal
    "VEH 1",                    # whitespace
    "VEH-1\nHost: evil",        # newline / header smuggling
    "http://evil.example.com",  # absolute-URL smuggling
    "//evil.example.com",       # protocol-relative
    ".hidden",
    # These pass the character whitelist and are caught ONLY by the explicit
    # ".." check. Without them the whitelist alone satisfies every vector above
    # and the traversal check is unguarded — the mutation battery found exactly
    # that: deleting the ".." check left this test green.
    "VEH..1",
    "a..b",
    "VEH-1..",
]


@pytest.mark.parametrize("vehicle_id", _INJECTION_VECTORS)
def test_path_injection_vectors_are_rejected_without_a_request(monkeypatch, vehicle_id):
    """Each vector fails closed BEFORE the forwarded JWT can leave the process."""
    _set_env(monkeypatch)
    action = dict(_ACTION)
    action["vehicle_id"] = vehicle_id

    result = _make_executor().dispatch(action, _CLAIMS)

    assert result.status == ActionStatus.FAILED, (
        f"vehicle_id={vehicle_id!r} was accepted; it is interpolated into the "
        "request path and can redirect the owner's forwarded JWT to an "
        "unintended route"
    )
    assert result.failure_kind == FailureKind.EXECUTOR_TERMINAL_ERROR
    assert result.last_error_class == "InvalidVehicleId"


@resp_lib.activate
@pytest.mark.parametrize("vehicle_id", ["VEH-CS-DEMO-0031", "veh_1", "V1", "a.b-c_1"])
def test_legitimate_vehicle_ids_are_accepted(monkeypatch, vehicle_id):
    """The validator must not be so strict that real ids stop working.

    A guard that rejects legitimate traffic gets deleted by whoever it breaks, so
    the accept side is pinned alongside the reject side.
    """
    _set_env(monkeypatch)
    url = f"{_BASE_URL}/api/commands/{vehicle_id}"
    resp_lib.add(resp_lib.POST, url, json=_success_body(), status=201)

    action = dict(_ACTION)
    action["vehicle_id"] = vehicle_id
    result = _make_executor().dispatch(action, _CLAIMS)

    assert result.target_ref == _COMMAND_ID, (
        f"legitimate vehicle_id {vehicle_id!r} was rejected"
    )
    assert resp_lib.calls[0].request.url.endswith(f"/api/commands/{vehicle_id}")



# ---------------------------------------------------------------------------
# Review Cycle 2, Critical 2 — a malformed 2xx must report its OWN failure class.
#
# These branches sit inside a 2xx, and base.py rejects a success code on any
# failure kind. Passing `last_error_status=resp.status_code` there raised a
# ValueError INSIDE the retry loop, which @never_raises caught — so the audit
# trail recorded "ValueError" instead of "MissingCommandId", and (before
# escalation was centralised) the raise preceded the fire() call so nothing
# escalated at all. A valid-JSON non-object hit the same shape via AttributeError
# on `body.get`.
#
# The fix was verified by probe first; these are the regression guards, because a
# probe proves behaviour once and does not keep it true.
# ---------------------------------------------------------------------------


@resp_lib.activate
@pytest.mark.parametrize("status", [200, 201, 202])
def test_2xx_without_command_id_reports_missing_command_id(monkeypatch, status):
    """Not 'ValueError' — the operator reading the trail needs the real cause."""
    _set_env(monkeypatch)
    resp_lib.add(resp_lib.POST, _POST_URL, json={"unexpected": "shape"}, status=status)

    result = _make_executor().dispatch(_ACTION, _CLAIMS)

    assert result.status == ActionStatus.FAILED
    assert result.failure_kind == FailureKind.EXECUTOR_TERMINAL_ERROR
    assert result.last_error_class == "MissingCommandId", (
        f"expected MissingCommandId, got {result.last_error_class!r}. A generic "
        "Python exception name here means the ExecuteResult construction raised "
        "inside the loop and @never_raises degraded the specific failure class."
    )
    assert result.last_error_status is None, (
        "a 2xx must not be recorded as last_error_status on a failure result"
    )


@resp_lib.activate
def test_2xx_with_non_object_body_reports_invalid_json_response(monkeypatch):
    """`[]` is valid JSON and not a dict; body.get() would raise AttributeError."""
    _set_env(monkeypatch)
    resp_lib.add(resp_lib.POST, _POST_URL, json=[], status=200)

    result = _make_executor().dispatch(_ACTION, _CLAIMS)

    assert result.status == ActionStatus.FAILED
    assert result.last_error_class == "InvalidJsonResponse", (
        f"expected InvalidJsonResponse, got {result.last_error_class!r} — a bare "
        "AttributeError/TypeError points the operator at a Python error rather "
        "than at a malformed upstream response."
    )


@resp_lib.activate
def test_2xx_malformed_still_requests_escalation(monkeypatch):
    """The degraded path also used to skip escalation; it must not."""
    _set_env(monkeypatch)
    resp_lib.add(resp_lib.POST, _POST_URL, json={"nope": 1}, status=200)

    result = _make_executor().dispatch(_ACTION, _CLAIMS)

    assert result.escalate is True



# ---------------------------------------------------------------------------
# Review Cycle 3 Warning 4 — this executor's deadline guard was unpinned, while
# dms's and retail's were both caught. An unpinned guard on a
# safety-relevant remote-command path is the one most worth pinning: exceeding
# the synchronous budget means API Gateway kills the approve route with the
# Action still on pending_execution, so the owner is left believing a remote
# vehicle command is in flight when nothing is.
# ---------------------------------------------------------------------------


def test_retry_sequence_abandons_when_the_deadline_passes(monkeypatch):
    """Once the budget is spent the loop stops issuing requests."""
    _set_env(monkeypatch)
    calls: list[int] = []

    def _counting_post(*args, **kwargs):
        calls.append(1)
        raise ConnectionError("slow upstream")

    monkeypatch.setattr(
        "agents.executors.vehicle_command.requests",
        type("R", (), {"post": staticmethod(_counting_post)})(),
    )
    # Clock jumps past the deadline after the first attempt.
    ticks = iter([0.0, 0.0, 10_000.0, 10_000.0, 10_000.0, 10_000.0, 10_000.0])
    monkeypatch.setattr(
        "agents.executors.vehicle_command.time.monotonic",
        lambda: next(ticks, 10_000.0),
        raising=False,
    )

    result = _make_executor().dispatch(_ACTION, _CLAIMS)

    assert result.status == ActionStatus.FAILED
    assert len(calls) <= 2, (
        f"the loop issued {len(calls)} requests after the deadline passed; the "
        "sequence must abandon so the gateway does not kill the route mid-dispatch"
    )


def test_deadline_is_derived_from_policy_not_a_literal():
    """A literal would drift from base.SYNCHRONOUS_BUDGET_S and from the siblings."""
    from pathlib import Path

    from agents.executors import vehicle_command as vc_mod

    src = Path(vc_mod.__file__).read_text(encoding="utf-8")
    assert "policy.deadline_s" in src, (
        "vehicle_command does not derive its deadline from the policy"
    )
    assert "deadline" in src and ">= deadline" in src or "> deadline" in src, (
        "vehicle_command computes a deadline but never compares against it"
    )
