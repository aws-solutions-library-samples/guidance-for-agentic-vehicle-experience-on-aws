"""Tests for DmsExecutor.

Spec: `.kiro/specs/2026-09-18-avx-tier2-ios-cards-and-actions/spec.md`
      § "DMS executor", Addendum D5.

Five required tests (exact names per spec):
  test_4xx_terminal_maps_to_declined_not_failed
  test_5xx_exhausted_maps_to_failed_not_declined
  test_5xx_transient_then_2xx_succeeds
  test_failed_transition_records_retry_count_in_audit_trail
  test_failed_transition_fires_escalation_per_config

All AWS clients and the HTTP layer are stubbed; sleep is injected so no real
waits occur.
"""

from __future__ import annotations

import json
import time
from typing import Any
from unittest.mock import MagicMock, call, patch

import pytest
import responses as rsps  # type: ignore[import]

from agents.executors.base import (
    ActionStatus,
    FailureKind,
    RetryPolicy,
)
from agents.executors.dms import DmsExecutor

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_ENDPOINT = "http://dms-staging.test/api/dms/fleet/repair-orders"

#: Schema-shaped (action.schema.json `action_id` pattern) and carrying the two
#: parameters DMS requires. The earlier fixture (`action_id="act-001"`, no
#: parameters) is how the executor shipped a request DMS rejects on every call:
#: nothing here resembled a real Action, so nothing could disagree with DMS.
_ACTION_UUID = "1a2b3c4d-5e6f-4a7b-8c9d-0e1f2a3b4c5d"
_ACTION: dict[str, Any] = {
    "action_id": f"AVX-ACT-{_ACTION_UUID}",
    "action_kind": "book_combined_visit",
    "target_system": "dms",
    "parameters": {"vehicle_vin": "1MRDN0000000000015", "dealer_id": "dealer-seattle"},
    "owner_id": "owner-123",
}

_CLAIMS: dict[str, Any] = {
    "sub": "owner-123",
    "token": "Bearer.test.jwt",
}

#: A RetryPolicy with retry_count=3 and zero-ms backoff so tests stay fast.
_FAST_POLICY = RetryPolicy(retry_count=3, backoff_base_ms=0, backoff_cap_ms=0)


def _make_executor(
    *,
    enqueue_ios_owner: Any = None,
    events_client: Any = None,
    cloudwatch_client: Any = None,
) -> DmsExecutor:
    """Build a DmsExecutor with stubbed collaborators and a zero-sleep policy."""
    stub_ssm = MagicMock()
    # A stubbed SSM that always throws proves the executor tolerates an
    # unreachable Parameter Store; the injected policy below is what the test
    # actually exercises, so no test depends on ambient AWS state.
    stub_ssm.get_parameter.side_effect = Exception("stubbed SSM")

    executor = DmsExecutor(
        enqueue_ios_owner=enqueue_ios_owner or MagicMock(),
        events_client=events_client or MagicMock(),
        cloudwatch_client=cloudwatch_client or MagicMock(),
        ssm_client=stub_ssm,
        sleep_fn=lambda _: None,
        # Injected via the constructor seam rather than assigned onto the
        # instance afterwards: policy is now loaded lazily behind a property
        # (so that importing the registry performs no SSM I/O), and a property
        # cannot be shadowed by instance assignment.
        retry_policy=_FAST_POLICY,
    )
    return executor


# ---------------------------------------------------------------------------
# Original five spec-required tests
# ---------------------------------------------------------------------------


@rsps.activate
def test_4xx_terminal_maps_to_declined_not_failed() -> None:
    """A genuine business refusal (422 Unprocessable) → declined / target_refused.

    The exemplar was 400 until review Cycle 1 of Fix Groups 35/36 (W3): DMS's
    create-RO route answers 400 only for request validation, which is our
    fault, not a dealer's answer, so 400 now maps to `executor_terminal_error`
    (see test_400_is_our_request_not_a_dealer_refusal); Cycle 2 (N1) did the
    same for DMS's 409. 422 keeps the refusal mapping. 404/401/403 are NOT exemplars here; they get their own
    test below (test_client_error_4xx_maps_to_terminal_error).
    """
    rsps.add(rsps.POST, _ENDPOINT, json={"error": "cannot accept"}, status=422)

    executor = _make_executor()
    with patch.dict("os.environ", {"VSA_DMS_API_ENDPOINT": "http://dms-staging.test"}):
        result = executor.dispatch(_ACTION, _CLAIMS)

    assert result.status == ActionStatus.DECLINED, (
        f"Expected status=declined for a 422 response, got {result.status!r}. "
        "422 is a genuine business refusal (declined / target_refused), "
        "not an executor failure (failed)."
    )
    assert result.failure_kind == FailureKind.TARGET_REFUSED
    assert result.retry_count == 0, "4xx must not be retried (retry_count must be 0)"
    assert result.escalate is False, (
        "A dealer refusal must not trigger escalation — the dealer answered; "
        "escalating would tell the owner we failed to reach a dealer who said no."
    )


@rsps.activate
def test_400_is_our_request_not_a_dealer_refusal() -> None:
    """W3: DMS's own roster rejection is a 400. Recording it as `declined` would
    tell the owner a dealer refused when no dealer ever saw the request."""
    rsps.add(rsps.POST, _ENDPOINT,
             json={"error": "dealer_id is not a valid DMS rooftop"}, status=400)

    executor = _make_executor()
    with patch.dict("os.environ", {"VSA_DMS_API_ENDPOINT": "http://dms-staging.test"}):
        result = executor.dispatch(_ACTION, _CLAIMS)

    assert result.status == ActionStatus.FAILED
    assert result.failure_kind == FailureKind.EXECUTOR_TERMINAL_ERROR
    assert result.last_error_status == 400
    assert result.retry_count == 0
    assert len(rsps.calls) == 1, "a validation 400 must not be retried"


@rsps.activate
def test_4xx_409_conflict_is_an_escalated_anomaly_not_a_refusal() -> None:
    """DMS's only 409 (e2085e1) means the avx_action_id already booked a different
    vehicle or dealer. Inverted from `..._maps_to_declined` in review Cycle 2 (N1):
    recording it as `declined` told the owner a dealer refused, and paged nobody,
    while a repair order may exist that CVX never recorded."""
    rsps.add(rsps.POST, _ENDPOINT,
             json={"error": "avx_action_id already booked a different repair order"},
             status=409)

    executor = _make_executor()
    with patch.dict("os.environ", {"VSA_DMS_API_ENDPOINT": "http://dms-staging.test"}):
        result = executor.dispatch(_ACTION, _CLAIMS)

    assert result.status == ActionStatus.FAILED
    assert result.failure_kind == FailureKind.EXECUTOR_TERMINAL_ERROR
    assert result.escalate is True
    assert result.last_error_status == 409
    assert len(rsps.calls) == 1


@rsps.activate
def test_client_error_4xx_maps_to_terminal_error() -> None:
    """401/403/404 are OUR fault — nothing reached a human.

    These must map to failed / executor_terminal_error (escalate=True), NOT to
    declined / target_refused. Telling the owner "the dealer refused" when in
    fact our token expired or the route doesn't exist is a false statement.

    Parametrised: 401, 403, 404.
    """
    for status_code in [401, 403, 404]:
        rsps.reset()
        rsps.add(rsps.POST, _ENDPOINT, json={"error": "auth or not found"}, status=status_code)

        executor = _make_executor()
        with patch.dict("os.environ", {"VSA_DMS_API_ENDPOINT": "http://dms-staging.test"}):
            result = executor.dispatch(_ACTION, _CLAIMS)

        assert result.status == ActionStatus.FAILED, (
            f"status {status_code} should map to failed (executor_terminal_error), "
            f"got {result.status!r}. Nothing reached a human; the owner must not "
            "be told the dealer refused."
        )
        assert result.failure_kind == FailureKind.EXECUTOR_TERMINAL_ERROR, (
            f"status {status_code} should have failure_kind=executor_terminal_error"
        )
        assert result.escalate is True, (
            f"status {status_code} must trigger escalation"
        )
        assert result.last_error_status == status_code


@rsps.activate
def test_408_request_timeout_is_retried() -> None:
    """408 Request Timeout is transient — it must be retried, not immediately declined.

    The spec treats it like a connection timeout: the target did not refuse,
    it ran out of time. After exhausting retries the result must be
    executor_exhausted_retries, not target_refused.
    """
    for _ in range(3):
        rsps.add(rsps.POST, _ENDPOINT, json={"error": "timeout"}, status=408)

    executor = _make_executor()
    with patch.dict("os.environ", {"VSA_DMS_API_ENDPOINT": "http://dms-staging.test"}):
        result = executor.dispatch(_ACTION, _CLAIMS)

    assert result.status == ActionStatus.FAILED, (
        f"408 exhausted retries should be status=failed, got {result.status!r}"
    )
    assert result.failure_kind == FailureKind.EXECUTOR_EXHAUSTED_RETRIES, (
        "408 should exhaust retries, not land on target_refused"
    )
    assert result.retry_count == 3, (
        f"Expected retry_count=3 (policy.retry_count), got {result.retry_count}"
    )
    assert len(rsps.calls) == 3, "408 must be retried 3 times"


@rsps.activate
def test_5xx_exhausted_maps_to_failed_not_declined() -> None:
    """Stubbed 503 on every attempt (policy retry_count=3) → executor fires 3
    attempts, returns status=failed / executor_exhausted_retries / retry_count=3.
    Must NOT be status=declined."""
    for _ in range(3):
        rsps.add(rsps.POST, _ENDPOINT, json={"error": "unavailable"}, status=503)

    executor = _make_executor()
    with patch.dict("os.environ", {"VSA_DMS_API_ENDPOINT": "http://dms-staging.test"}):
        result = executor.dispatch(_ACTION, _CLAIMS)

    # ---- The load-bearing assertions ----
    assert result.status == ActionStatus.FAILED, (
        f"Expected status=failed after transient exhaustion, got {result.status!r}. "
        "Addendum D5: 5xx exhaustion is a failed state, not a declined state."
    )
    assert result.failure_kind == FailureKind.EXECUTOR_EXHAUSTED_RETRIES
    assert result.retry_count == 3, (
        f"Expected retry_count=3 (= policy.retry_count), got {result.retry_count}"
    )
    assert result.escalate is True, "Exhausted failure must trigger escalation"
    # All 3 HTTP calls were made
    assert len(rsps.calls) == 3


@rsps.activate
def test_5xx_transient_then_2xx_succeeds() -> None:
    """One 503, then 200 → success on the second attempt (retry_count still 1,
    not counted against the policy budget since we succeeded)."""
    rsps.add(rsps.POST, _ENDPOINT, json={"error": "unavailable"}, status=503)
    rsps.add(rsps.POST, _ENDPOINT, json={"ro_id": "RO-9999"}, status=200)

    executor = _make_executor()
    with patch.dict("os.environ", {"VSA_DMS_API_ENDPOINT": "http://dms-staging.test"}):
        result = executor.dispatch(_ACTION, _CLAIMS)

    assert result.status == ActionStatus.IN_PROGRESS
    assert result.target_ref == "RO-9999"
    assert result.escalate is False
    assert len(rsps.calls) == 2, "Should have fired exactly 2 HTTP calls"


@rsps.activate
def test_failed_transition_records_retry_count_in_audit_trail() -> None:
    """Audit-trail contract: on retry-budget exhaustion the audit entry carries
    failure_kind, retry_count, from_status=pending_execution, to_status=failed."""
    for _ in range(3):
        rsps.add(rsps.POST, _ENDPOINT, json={}, status=503)

    executor = _make_executor()
    with patch.dict("os.environ", {"VSA_DMS_API_ENDPOINT": "http://dms-staging.test"}):
        result = executor.dispatch(_ACTION, _CLAIMS)

    entry = result.audit_entry(by="dms-executor")

    assert entry["to_status"] == ActionStatus.FAILED
    assert entry["from_status"] == ActionStatus.PENDING_EXECUTION
    assert entry["failure_kind"] == FailureKind.EXECUTOR_EXHAUSTED_RETRIES
    assert entry["retry_count"] == 3
    assert "at" in entry
    assert "by" in entry


@rsps.activate
def test_failed_transition_requests_escalation() -> None:
    """On retry-budget exhaustion the result must REQUEST escalation.

    Review Cycle 2 Critical 3 moved the firing itself to registry.dispatch(),
    because escalation living inside each executor left it structurally
    unreliable: `chat` and `ios_owner` never called it, and neither did
    `@never_raises` — the catch-all that synthesises escalate=True for all six
    executors. So the paths most likely to fail were the ones that escalated to
    nobody.

    This test therefore asserts the executor's half of the contract. The
    behavioural assertions that used to live here — owner finding carries a
    non-empty owner_id, put_events fires with the right Detail, put_metric_data
    is dimensioned by target_system — moved to
    test_registry.py::TestEscalationFiresForEveryTarget, where they run against
    ALL SIX targets instead of only dms.
    """
    for _ in range(3):
        rsps.add(rsps.POST, _ENDPOINT, json={}, status=503)

    executor = _make_executor()
    action_with_owner = {**_ACTION, "owner_id": "owner-xyz"}

    with patch.dict("os.environ", {"VSA_DMS_API_ENDPOINT": "http://dms-staging.test"}):
        result = executor.dispatch(action_with_owner, _CLAIMS)

    assert result.status == ActionStatus.FAILED
    assert result.failure_kind == FailureKind.EXECUTOR_EXHAUSTED_RETRIES
    assert result.retry_count == 3
    assert result.escalate is True, (
        "a terminal failure must request escalation; registry.dispatch() fires it"
    )


# ---------------------------------------------------------------------------
# New tests: additional D5 / spec properties
# ---------------------------------------------------------------------------


@rsps.activate
def test_2xx_without_ro_id_is_terminal_error() -> None:
    """A 200 whose body lacks ro_id must be executor_terminal_error, not success.

    Without ro_id there is nothing to join against the dms.ro.status_changed
    event, so the card can never advance beyond in_progress. Returning
    success(target_ref=None) and then stalling is worse than failing loudly.
    """
    rsps.add(rsps.POST, _ENDPOINT, json={"status": "accepted"}, status=200)

    executor = _make_executor()
    with patch.dict("os.environ", {"VSA_DMS_API_ENDPOINT": "http://dms-staging.test"}):
        result = executor.dispatch(_ACTION, _CLAIMS)

    assert result.status == ActionStatus.FAILED, (
        f"A 200 without ro_id must be failed/executor_terminal_error; "
        f"got status={result.status!r}"
    )
    assert result.failure_kind == FailureKind.EXECUTOR_TERMINAL_ERROR
    assert result.last_error_class == "MissingRoId", (
        f"Expected last_error_class='MissingRoId', got {result.last_error_class!r}"
    )
    # last_error_status is intentionally absent: base.py's validation rejects
    # a 2xx code on any failure kind (a 2xx is not an error status), so we
    # use the error class name as the signal instead.
    assert result.last_error_status is None
    assert result.escalate is True


def test_missing_jwt_is_terminal_error_not_a_request() -> None:
    """A missing JWT must be caught locally and returned as terminal_error.

    The executor must NOT send 'Authorization: Bearer ' with an empty token and
    let the target produce a 401. A locally-detected missing credential is our
    fault, and it must escalate. No HTTP request should be made.
    """
    executor = _make_executor()
    # Claims with no token at all
    empty_claims: dict[str, Any] = {"sub": "owner-123"}

    with patch.dict("os.environ", {"VSA_DMS_API_ENDPOINT": "http://dms-staging.test"}):
        # No @responses.activate — if an HTTP call is made it would raise
        # ConnectionError, making the test vacuously "pass" in the wrong way.
        # Instead we use responses in passthrough mode to detect any real attempt.
        with rsps.RequestsMock(assert_all_requests_are_fired=False) as rsps_ctx:
            result = executor.dispatch(_ACTION, empty_claims)
            assert len(rsps_ctx.calls) == 0, (
                "No HTTP request should be made when the JWT is missing; "
                f"got {len(rsps_ctx.calls)} call(s)"
            )

    assert result.status == ActionStatus.FAILED, (
        f"A missing JWT must be failed/executor_terminal_error, got {result.status!r}"
    )
    assert result.failure_kind == FailureKind.EXECUTOR_TERMINAL_ERROR
    assert result.last_error_class == "MissingJwt"
    assert result.escalate is True


@rsps.activate
def test_per_attempt_timeout_used_not_literal() -> None:
    """The requests.post call must pass a timeout kwarg derived from policy,
    not the literal `10` that was there before this fix.

    If the timeout kwarg is absent, a stalled connection blocks indefinitely
    and the approve route dies at the API Gateway 29-second limit.
    """
    rsps.add(rsps.POST, _ENDPOINT, json={"ro_id": "RO-timeout-test"}, status=200)

    executor = _make_executor()
    with patch.dict("os.environ", {"VSA_DMS_API_ENDPOINT": "http://dms-staging.test"}):
        with patch("requests.post", wraps=None) as mock_post:
            # Set the return value to a fake response so we can inspect call kwargs.
            fake_resp = MagicMock()
            fake_resp.status_code = 200
            fake_resp.json.return_value = {"ro_id": "RO-timeout-test"}
            mock_post.return_value = fake_resp

            executor.dispatch(_ACTION, _CLAIMS)  # asserted via mock_post below

    assert mock_post.called, "requests.post was not called"
    call_kwargs = mock_post.call_args.kwargs
    assert "timeout" in call_kwargs, (
        "requests.post must be called with a `timeout` kwarg. "
        "Without it, a stalled connection blocks indefinitely and the approve "
        "route exceeds API Gateway's 29-second limit."
    )
    timeout_val = call_kwargs["timeout"]
    assert timeout_val is not None and timeout_val > 0, (
        f"timeout must be a positive number, got {timeout_val!r}"
    )
    # The value must be derived from policy, not a literal 10. For the
    # injected _FAST_POLICY (retry_count=3, zero backoff) the derived value
    # is policy.per_attempt_timeout_s = 25.0 / 3 ≈ 8.33, which is NOT 10.
    assert timeout_val != 10, (
        f"timeout={timeout_val!r} looks like the hardcoded literal 10; "
        "it must be derived from policy.per_attempt_timeout_s instead."
    )


@rsps.activate
def test_retry_loop_respects_deadline() -> None:
    """The retry loop must not exceed policy.deadline_s wall-clock seconds.

    Simulates a slow clock that advances past the deadline after the first
    attempt. The second attempt must NOT be made.
    """
    # Register enough stubs for 3 attempts in case the deadline is not enforced.
    for _ in range(3):
        rsps.add(rsps.POST, _ENDPOINT, json={"error": "slow"}, status=503)

    executor = _make_executor()

    # We advance time past the deadline after the first attempt by patching
    # time.monotonic so it returns a value beyond deadline_s on the second call.
    # First call (deadline init): t=0; second call (loop check): t > deadline_s.
    _policy_deadline = _FAST_POLICY.deadline_s
    call_count = 0

    def _fake_monotonic() -> float:
        nonlocal call_count
        call_count += 1
        # First two calls are legitimate (deadline init + first loop entry).
        if call_count <= 2:
            return 0.0
        # Any further check sees us past the deadline.
        return _policy_deadline + 1.0

    with patch.dict("os.environ", {"VSA_DMS_API_ENDPOINT": "http://dms-staging.test"}):
        with patch("agents.executors.dms.time.monotonic", side_effect=_fake_monotonic):
            result = executor.dispatch(_ACTION, _CLAIMS)

    # Only one HTTP call should have been made — the loop broke on the
    # deadline check after the first attempt.
    assert len(rsps.calls) == 1, (
        f"Expected 1 HTTP call (deadline enforced after first attempt), "
        f"got {len(rsps.calls)}"
    )
    assert result.status == ActionStatus.FAILED



# ---------------------------------------------------------------------------
# Review Cycle 3 Warning 3 — the InvalidJsonResponse 2xx branch was not
# mutation-pinned: re-adding `last_error_status=resp.status_code` there degraded
# the class to "ValueError" and the suite still reported 320 passed.
# vehicle_command's equivalent branch WAS pinned, so the two executors' identical
# branches had different test coverage — which is exactly how the narrower
# version of the Cycle 2 C2 fix survived.
# ---------------------------------------------------------------------------


@rsps.activate
def test_2xx_with_non_object_body_reports_invalid_json_response() -> None:
    """`[]` is valid JSON and not a dict; body.get() would raise AttributeError.

    The recorded class must name the upstream problem, not a Python type error —
    an operator reading the audit trail is diagnosing the downstream system.
    """
    rsps.add(rsps.POST, _ENDPOINT, json=[], status=200)

    with patch.dict("os.environ", {"VSA_DMS_API_ENDPOINT": "http://dms-staging.test"}):
        result = _make_executor().dispatch(_ACTION, _CLAIMS)

    assert result.status == ActionStatus.FAILED
    assert result.failure_kind == FailureKind.EXECUTOR_TERMINAL_ERROR
    assert result.last_error_class == "InvalidJsonResponse", (
        f"expected InvalidJsonResponse, got {result.last_error_class!r}. A bare "
        "'ValueError' here means the ExecuteResult construction raised inside the "
        "retry loop (a 2xx passed as last_error_status) and @never_raises "
        "degraded the specific failure class."
    )
    assert result.last_error_status is None, (
        "a 2xx must never be recorded as last_error_status on a failure result"
    )
    assert result.escalate is True


@rsps.activate
def test_2xx_with_unparseable_body_reports_invalid_json_response() -> None:
    """Not-JSON-at-all takes the same branch and must report the same class."""
    rsps.add(rsps.POST, _ENDPOINT, body="<html>gateway</html>", status=200)

    with patch.dict("os.environ", {"VSA_DMS_API_ENDPOINT": "http://dms-staging.test"}):
        result = _make_executor().dispatch(_ACTION, _CLAIMS)

    assert result.status == ActionStatus.FAILED
    assert result.last_error_class == "InvalidJsonResponse"
    assert result.last_error_status is None



# ---------------------------------------------------------------------------
# F35.2 — the request body DMS actually requires
# ---------------------------------------------------------------------------
#
# DMS `create_fleet_repair_order` 400s without `vehicle_vin` / `dealer_id`
# (repair_orders.py:1234-1239) and validates `avx_action_id` as a bare UUID
# (repair_orders.py:78). The executor sent only `{"avx_action_id": "AVX-ACT-…"}`,
# so every dispatch was refused and recorded as `declined`. These assertions pin
# the body against DMS's contract, not against the executor's own idea of it.

_DMS_UUID_RE = r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"


@rsps.activate
def test_request_body_matches_dms_contract() -> None:
    import re as _re

    rsps.add(rsps.POST, _ENDPOINT, json={"ro_id": "RO-1"}, status=201)
    executor = _make_executor()
    with patch.dict("os.environ", {"VSA_DMS_API_ENDPOINT": "http://dms-staging.test"}):
        result = executor.dispatch(_ACTION, _CLAIMS)

    assert result.status == ActionStatus.IN_PROGRESS
    assert result.target_ref == "RO-1"
    assert len(rsps.calls) == 1
    sent = json.loads(rsps.calls[0].request.body)
    assert sent == {
        "vehicle_vin": "1MRDN0000000000015",
        "dealer_id": "dealer-seattle",
        "avx_action_id": _ACTION_UUID,
    }
    assert _re.match(_DMS_UUID_RE, sent["avx_action_id"]), (
        "avx_action_id must satisfy DMS's own regex, or DMS answers 400"
    )


@pytest.mark.parametrize("missing", ["vehicle_vin", "dealer_id"])
@rsps.activate
def test_missing_required_parameter_is_local_terminal_error(missing: str) -> None:
    params = dict(_ACTION["parameters"])
    del params[missing]
    action = {**_ACTION, "parameters": params}

    executor = _make_executor()
    with patch.dict("os.environ", {"VSA_DMS_API_ENDPOINT": "http://dms-staging.test"}):
        result = executor.dispatch(action, _CLAIMS)

    assert result.status == ActionStatus.FAILED
    assert result.failure_kind == FailureKind.EXECUTOR_TERMINAL_ERROR
    assert result.last_error_class == "MissingDispatchParameter"
    assert len(rsps.calls) == 0, "an incomplete request must not reach DMS"


@rsps.activate
def test_unexpressible_action_id_omits_correlation_but_still_books() -> None:
    rsps.add(rsps.POST, _ENDPOINT, json={"ro_id": "RO-2"}, status=201)
    action = {**_ACTION, "action_id": "not-a-uuid"}
    executor = _make_executor()
    with patch.dict("os.environ", {"VSA_DMS_API_ENDPOINT": "http://dms-staging.test"}):
        result = executor.dispatch(action, _CLAIMS)

    assert result.status == ActionStatus.IN_PROGRESS
    sent = json.loads(rsps.calls[0].request.body)
    assert "avx_action_id" not in sent


@rsps.activate
def test_no_retry_without_the_correlation_dms_deduplicates_on() -> None:
    """W1: retries are safe only because DMS deduplicates on avx_action_id. An
    Action whose id cannot be expressed to DMS gets one attempt, not a chance to
    book twice."""
    for _ in range(3):
        rsps.add(rsps.POST, _ENDPOINT, json={"error": "unavailable"}, status=503)
    action = {**_ACTION, "action_id": "not-a-uuid"}
    executor = _make_executor()
    with patch.dict("os.environ", {"VSA_DMS_API_ENDPOINT": "http://dms-staging.test"}):
        result = executor.dispatch(action, _CLAIMS)

    assert result.status == ActionStatus.FAILED
    assert len(rsps.calls) == 1


@rsps.activate
def test_retries_carry_the_same_correlation_id() -> None:
    """The dedup only works if every attempt names the same booking."""
    rsps.add(rsps.POST, _ENDPOINT, json={"error": "unavailable"}, status=503)
    rsps.add(rsps.POST, _ENDPOINT, json={"ro_id": "RO-R"}, status=200)
    executor = _make_executor()
    with patch.dict("os.environ", {"VSA_DMS_API_ENDPOINT": "http://dms-staging.test"}):
        result = executor.dispatch(_ACTION, _CLAIMS)

    assert result.target_ref == "RO-R"
    ids = {json.loads(c.request.body)["avx_action_id"] for c in rsps.calls}
    assert ids == {_ACTION_UUID}
