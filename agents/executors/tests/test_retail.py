"""Tests for RetailExecutor.

Spec: `.kiro/specs/2026-09-18-avx-tier2-ios-cards-and-actions/spec.md`

Coverage contract
-----------------
1.  Happy path (201) — lead capture records target_ref=leadId.
1b. Happy path (200) — 200 with valid leadId is success, not declined.
2.  Tenant provenance — tenantId in the outbound body comes from claims, NOT
    from action parameters; an action-supplied tenantId must be ignored.
3.  Missing tenant claim — fails closed (terminal_error) rather than omitting
    the field or falling back to the action payload.
4.  Unsupported configurator action_kind — fails closed (failed/terminal_error),
    not success.
5.  Any other unsupported action_kind — fails closed (failed/terminal_error).
6.  4xx (non-429, non-client-error) response — returned as declined/target_refused.
6b. 401/404 — returned as terminal_error (not declined), and escalation fires.
7.  5xx exhausts retry budget — returned as failed/executor_exhausted_retries,
    escalation fires.
7b. 2xx response missing leadId — terminal_error (MissingLeadId).
8.  408 (transient 4xx) — retried.
9.  http.post call includes a timeout kwarg.
10. Optional fields forwarded when present; absent ones omitted.
11. target_system attribute matches expected value.

HTTP is always stubbed.  No real sleeps.  No real network calls.
"""

from __future__ import annotations

import json
import logging
from typing import Any
from unittest.mock import MagicMock, call, patch

import pytest

from agents.executors import escalation as escalation_module
from agents.executors.base import (
    ActionStatus,
    ExecuteResult,
    FailureKind,
    load_retry_policy,
    reset_policy_cache,
    RetryPolicy,
)
from agents.executors.retail import RetailExecutor, TARGET_SYSTEM


# ---------------------------------------------------------------------------
# Session-scoped reset: prevent module-level escalation client singletons
# from leaking between test files when the full suite runs.
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_escalation_clients():
    """Wipe cached escalation clients before and after every test.

    escalation.py caches _events_client and _cloudwatch_client as module-level
    globals for Lambda cold-start efficiency. Tests that patch these via
    patch("agents.executors.escalation._get_events") restore the patched
    attribute on exit, but the underlying global may still hold a stale mock
    if it was set inside the patched scope. Calling reset_clients() on both
    sides of the test guarantees a clean slate regardless of ordering.
    """
    escalation_module.reset_clients()
    yield
    escalation_module.reset_clients()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_response(status_code: int, body: dict) -> MagicMock:
    """Build a fake ``requests.Response``."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = body
    return resp


def _lead_action(
    *,
    action_kind: str = "lead_capture",
    params: dict | None = None,
) -> dict:
    return {
        "action_id": "act-001",
        "action_kind": action_kind,
        "owner_id": "owner-xyz",
        "parameters": params or {},
    }


def _claims(tenant_id: str = "tenant-abc") -> dict:
    # `token` carries the caller's Cognito JWT. The Acquire API sits behind the
    # same Cognito authorizer as every other route (no M2M path), so the executor
    # requires it and forwards it — see test_request_forwards_caller_jwt below.
    return {
        "custom:tenantId": tenant_id,
        "sub": "user-xyz",
        "token": "eyJhbGciOiJSUzI1NiJ9.RETAIL_TEST.sig",
    }


def _no_op_sleep(s: float) -> None:
    pass


def _executor(http_stub: Any = None) -> RetailExecutor:
    """Return a RetailExecutor backed by an injected HTTP stub, no-op SSM,
    and no-op sleep (injectable, unlike the old time.sleep direct call)."""
    if http_stub is None:
        http_stub = MagicMock()
    return RetailExecutor(http_client=http_stub, ssm_client=None, sleep=_no_op_sleep)


def _fast_policy() -> RetryPolicy:
    """A 3-attempt policy with zero backoff — no real sleeps in tests."""
    return RetryPolicy(retry_count=3, backoff_base_ms=0, backoff_cap_ms=0)


# ---------------------------------------------------------------------------
# Test 1 — Happy path (201): lead capture records target_ref = leadId
# ---------------------------------------------------------------------------


def test_lead_capture_happy_path_records_lead_id(monkeypatch):
    """A 201 response sets target_ref to the leadId returned by the API."""
    session = MagicMock()
    session.post.return_value = _make_response(201, {"leadId": "LEAD-20260901-abcd1234", "capturedAt": "2026-09-01T12:00:00Z"})

    reset_policy_cache()
    monkeypatch.setenv("VSA_API_BASE", "https://api.example.com")
    with patch("agents.executors.retail.load_retry_policy", return_value=_fast_policy()):
        result = _executor(session).dispatch(_lead_action(), _claims())

    assert result.status == ActionStatus.COMPLETED
    assert result.target_ref == "LEAD-20260901-abcd1234"
    assert result.failure_kind is None


# ---------------------------------------------------------------------------
# Test 1b — Happy path (200): 200 with valid leadId must succeed, not declined
# ---------------------------------------------------------------------------


def test_lead_capture_200_with_lead_id_succeeds(monkeypatch):
    """A 200 response with a valid leadId must be recorded as success.

    This is the Critical 6 regression test. Before the fix, status_code == 201
    was the only gate; a 200 fell through to refused/target_refused and the
    leadId was discarded.
    """
    session = MagicMock()
    session.post.return_value = _make_response(200, {"leadId": "LEAD-200-OK", "capturedAt": "2026-09-01T12:00:00Z"})

    monkeypatch.setenv("VSA_API_BASE", "https://api.example.com")
    with patch("agents.executors.retail.load_retry_policy", return_value=_fast_policy()):
        result = _executor(session).dispatch(_lead_action(), _claims())

    assert result.status == ActionStatus.COMPLETED, (
        "A 200 with a valid leadId must be COMPLETED, not declined/target_refused"
    )
    assert result.target_ref == "LEAD-200-OK", "leadId from the 200 response must be recorded"
    assert result.failure_kind is None


# ---------------------------------------------------------------------------
# Test 2 — Tenant provenance: tenantId in body comes from claims, not params
# ---------------------------------------------------------------------------


def test_tenant_id_sourced_from_claims_not_action_params(monkeypatch):
    """The outbound POST body must contain the claims tenantId, not the action's."""
    session = MagicMock()
    session.post.return_value = _make_response(201, {"leadId": "LEAD-X", "capturedAt": "2026-09-01T12:00:00Z"})

    monkeypatch.setenv("VSA_API_BASE", "https://api.example.com")
    with patch("agents.executors.retail.load_retry_policy", return_value=_fast_policy()):
        _executor(session).dispatch(
            _lead_action(params={"tenantId": "attacker-tenant", "sessionId": "s1"}),
            _claims(tenant_id="real-tenant"),
        )

    # Inspect what was POSTed
    call_kwargs = session.post.call_args
    posted_body = call_kwargs[1]["json"] if call_kwargs[1] else call_kwargs[0][1]
    assert posted_body["tenantId"] == "real-tenant", (
        "tenantId in the outbound body MUST come from the caller's claims, "
        "not from action.parameters.tenantId"
    )
    assert posted_body["tenantId"] != "attacker-tenant", (
        "An action-supplied tenantId must never appear in the outbound body"
    )


# ---------------------------------------------------------------------------
# Test 3 — Claims without tenantId: fail closed
# ---------------------------------------------------------------------------


def test_missing_tenant_claim_fails_closed(monkeypatch):
    """When claims carry no tenantId, dispatch returns terminal_error immediately."""
    session = MagicMock()

    monkeypatch.setenv("VSA_API_BASE", "https://api.example.com")
    with patch("agents.executors.retail.load_retry_policy", return_value=_fast_policy()):
        result = _executor(session).dispatch(
            _lead_action(),
            # A JWT but no tenantId: isolates the tenant precondition. Passing
            # {} would trip the JWT check first and this test would pass for the
            # wrong reason.
            {"token": "eyJhbGciOiJSUzI1NiJ9.TENANT_TEST.sig"},
        )

    assert result.status == ActionStatus.FAILED
    assert result.failure_kind == FailureKind.EXECUTOR_TERMINAL_ERROR
    assert result.last_error_class == "MissingTenantClaim"
    # Must not have made any HTTP call
    session.post.assert_not_called()


def test_claims_with_only_sub_no_tenant_fails_closed(monkeypatch):
    """Claims with 'sub' but no tenantId also fail closed."""
    session = MagicMock()

    monkeypatch.setenv("VSA_API_BASE", "https://api.example.com")
    with patch("agents.executors.retail.load_retry_policy", return_value=_fast_policy()):
        result = _executor(session).dispatch(
            _lead_action(),
            # sub + JWT but no tenantId: isolates the tenant precondition.
            {"sub": "user-123", "token": "eyJhbGciOiJSUzI1NiJ9.SUB_ONLY.sig"},
        )

    assert result.status == ActionStatus.FAILED
    assert result.failure_kind == FailureKind.EXECUTOR_TERMINAL_ERROR
    session.post.assert_not_called()


# ---------------------------------------------------------------------------
# Test 4 — configurator_recommend WITHOUT savedConfig: fail closed
#
# Rewritten 2026-09-20 (spec 2026-09-20-avx-acquire-configurator-target). This test
# previously asserted configurator_recommend fails closed because it "has no backing
# endpoint". It now HAS one — the same POST /acquire/leads, carrying the approved
# configuration in savedConfig — and the test kept passing anyway, because the action
# it builds has no savedConfig and so still fails closed, for an entirely different
# reason than its name and docstring claimed. A vacuous pass: the assertions held
# while the behaviour under them changed completely.
#
# Split into two real property tests: refusal without a configuration (here) and
# dispatch with one (Test 4b). MUTATION-VERIFIED: dropping the MissingSavedConfig
# guard fails this test; removing configurator_recommend from _SUPPORTED_KINDS fails
# Test 4b.
# ---------------------------------------------------------------------------


def test_configurator_recommend_without_saved_config_fails_closed(monkeypatch):
    """No savedConfig means no configuration to register — refuse, do not submit.

    Submitting anyway would POST a lead indistinguishable from a bare lead_capture,
    silently losing the configuration the owner approved while reporting success.
    """
    session = MagicMock()

    monkeypatch.setenv("VSA_API_BASE", "https://api.example.com")
    result = _executor(session).dispatch(
        _lead_action(action_kind="configurator_recommend"),
        _claims(),
    )

    assert result.status == ActionStatus.FAILED, (
        "configurator_recommend without savedConfig must fail closed, not succeed"
    )
    assert result.failure_kind == FailureKind.EXECUTOR_TERMINAL_ERROR, (
        "must use EXECUTOR_TERMINAL_ERROR, not TARGET_REFUSED — this is our own "
        "missing input, not the target refusing the work"
    )
    assert result.last_error_class == "MissingSavedConfig", (
        f"expected MissingSavedConfig, got {result.last_error_class!r}. If this says "
        "UnsupportedActionKind, configurator_recommend has fallen out of "
        "_SUPPORTED_KINDS and this test is passing for the pre-2026-09-20 reason "
        "rather than the one it now documents."
    )
    session.post.assert_not_called()


# ---------------------------------------------------------------------------
# Test 4b — configurator_recommend WITH savedConfig: dispatches, records leadId
# ---------------------------------------------------------------------------


def test_configurator_recommend_with_saved_config_dispatches(monkeypatch):
    """The configuration is forwarded opaquely and target_ref is the returned leadId."""
    session = MagicMock()
    session.post.return_value = _make_response(201, {"leadId": "LEAD-20260920-cfg00001"})

    saved_config = {"preferredCategory": "suv", "preferredModelId": "model-x"}
    reset_policy_cache()
    monkeypatch.setenv("VSA_API_BASE", "https://api.example.com")
    with patch("agents.executors.retail.load_retry_policy", return_value=_fast_policy()):
        result = _executor(session).dispatch(
            _lead_action(
                action_kind="configurator_recommend",
                params={"savedConfig": saved_config, "sessionId": "sess-1"},
            ),
            _claims(),
        )

    assert result.status == ActionStatus.COMPLETED, (
        f"expected COMPLETED, got {result.status!r} (note: {result.note!r})"
    )
    assert result.target_ref == "LEAD-20260920-cfg00001", (
        "target_ref must be the leadId returned by the API — never synthesised"
    )
    session.post.assert_called_once()
    sent = session.post.call_args.kwargs["json"]
    assert sent["savedConfig"] == saved_config, (
        f"savedConfig must be forwarded unmodified; sent {sent.get('savedConfig')!r}. "
        "This executor must not read inside or reshape it."
    )
    assert sent["tenantId"] == "tenant-abc", (
        "tenantId must come from claims, never from the action payload"
    )


# ---------------------------------------------------------------------------
# Test 5 — Any other unknown action_kind: fail closed
# ---------------------------------------------------------------------------


def test_unknown_action_kind_fails_closed(monkeypatch):
    """An unrecognised action_kind must return terminal_error, not silently succeed."""
    session = MagicMock()

    monkeypatch.setenv("VSA_API_BASE", "https://api.example.com")
    result = _executor(session).dispatch(
        _lead_action(action_kind="trade_in_valuation"),
        _claims(),
    )

    assert result.status == ActionStatus.FAILED
    assert result.failure_kind == FailureKind.EXECUTOR_TERMINAL_ERROR
    session.post.assert_not_called()


# ---------------------------------------------------------------------------
# Test 6 — Business-refusal 4xx response: declined/target_refused
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status_code", [400, 409, 422])
def test_business_refusal_4xx_returns_declined_target_refused(status_code, monkeypatch):
    """A genuine business-refusal 4xx from the API must map to declined/target_refused."""
    session = MagicMock()
    session.post.return_value = _make_response(status_code, {"error": "bad"})

    monkeypatch.setenv("VSA_API_BASE", "https://api.example.com")
    with patch("agents.executors.retail.load_retry_policy", return_value=_fast_policy()):
        result = _executor(session).dispatch(_lead_action(), _claims())

    assert result.status == ActionStatus.DECLINED
    assert result.failure_kind == FailureKind.TARGET_REFUSED
    assert result.last_error_status == status_code
    assert result.retry_count == 0, "Terminal 4xx must not be retried"
    assert result.escalate is False, "declined must not escalate"


# ---------------------------------------------------------------------------
# Test 6b — Client-error 4xx (401/404): terminal_error + escalation fires
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status_code", [401, 403, 404, 405, 415])
def test_client_error_4xx_returns_terminal_error_and_escalates(status_code, monkeypatch):
    """401/403/404/405/415 must map to terminal_error (not declined) and escalate.

    This is the Critical 4 regression test. These codes mean our auth or routing
    is broken — nothing reached a human at the dealer, so 'dealer refused' is
    a false statement. classify_status() maps them to 'client_error'.
    """
    session = MagicMock()
    session.post.return_value = _make_response(status_code, {"error": "unauthorized"})

    mock_events = MagicMock()
    mock_cw = MagicMock()

    monkeypatch.setenv("VSA_API_BASE", "https://api.example.com")
    with patch("agents.executors.retail.load_retry_policy", return_value=_fast_policy()), \
         patch("agents.executors.escalation._get_events", return_value=mock_events), \
         patch("agents.executors.escalation._get_cloudwatch", return_value=mock_cw), \
         patch("agents.executors.escalation.load_escalation_config",
               return_value={"owner_visible": {}, "operator_visible": {}}):
        result = _executor(session).dispatch(_lead_action(), _claims())

    assert result.status == ActionStatus.FAILED, (
        f"HTTP {status_code} must be FAILED (terminal_error), not DECLINED"
    )
    assert result.failure_kind == FailureKind.EXECUTOR_TERMINAL_ERROR, (
        f"HTTP {status_code} is a client error, not a dealer refusal"
    )
    assert result.last_error_status == status_code
    assert result.escalate is True, "terminal_error must escalate"
    # Escalation is fired ONCE by registry.dispatch() (review Cycle 2 Critical 3).
    # The executor's contract is a result that REQUESTS escalation, asserted
    # above. That something actually escalates is asserted in test_registry.py
    # ::TestEscalationFiresForEveryTarget, across all six targets.


# ---------------------------------------------------------------------------
# Test 7 — 5xx exhausts retry budget: failed/executor_exhausted_retries
#          + escalation fires
# ---------------------------------------------------------------------------


def test_5xx_exhausts_retries_returns_exhausted_and_escalates(monkeypatch):
    """Persistent 5xx must exhaust the retry budget, return exhausted, and escalate."""
    session = MagicMock()
    session.post.return_value = _make_response(503, {"error": "service unavailable"})

    mock_events = MagicMock()
    mock_cw = MagicMock()

    monkeypatch.setenv("VSA_API_BASE", "https://api.example.com")
    policy = RetryPolicy(retry_count=3, backoff_base_ms=0, backoff_cap_ms=0)
    with patch("agents.executors.retail.load_retry_policy", return_value=policy), \
         patch("agents.executors.escalation._get_events", return_value=mock_events), \
         patch("agents.executors.escalation._get_cloudwatch", return_value=mock_cw), \
         patch("agents.executors.escalation.load_escalation_config",
               return_value={"owner_visible": {}, "operator_visible": {}}):
        result = _executor(session).dispatch(_lead_action(), _claims())

    assert result.status == ActionStatus.FAILED
    assert result.failure_kind == FailureKind.EXECUTOR_EXHAUSTED_RETRIES
    assert result.retry_count == policy.retry_count
    assert result.last_error_status == 503
    assert result.escalate is True, "exhausted_retries must escalate"
    # Attempted exactly retry_count times
    assert session.post.call_count == policy.retry_count
    # Escalation is fired ONCE by registry.dispatch() (review Cycle 2 Critical 3).
    # The executor's contract is a result that REQUESTS escalation, asserted
    # above. That something actually escalates is asserted in test_registry.py
    # ::TestEscalationFiresForEveryTarget, across all six targets.


def test_connection_error_exhausts_retries(monkeypatch):
    """Network-layer transient exceptions (ConnectionError) exhaust the budget."""

    class ConnectionError(Exception):
        pass

    session = MagicMock()
    session.post.side_effect = ConnectionError("network gone")

    monkeypatch.setenv("VSA_API_BASE", "https://api.example.com")
    policy = RetryPolicy(retry_count=3, backoff_base_ms=0, backoff_cap_ms=0)
    with patch("agents.executors.retail.load_retry_policy", return_value=policy), \
         patch("agents.executors.retail.is_transient_exception", return_value=True), \
         patch("agents.executors.escalation.load_escalation_config",
               return_value={"owner_visible": {}, "operator_visible": {}}), \
         patch("agents.executors.escalation._get_events", return_value=MagicMock()), \
         patch("agents.executors.escalation._get_cloudwatch", return_value=MagicMock()):
        result = _executor(session).dispatch(_lead_action(), _claims())

    assert result.status == ActionStatus.FAILED
    assert result.failure_kind == FailureKind.EXECUTOR_EXHAUSTED_RETRIES
    assert result.retry_count == policy.retry_count
    assert result.escalate is True


# ---------------------------------------------------------------------------
# Test 7b — 2xx without leadId: terminal_error (MissingLeadId)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status_code", [200, 201, 202])
def test_2xx_without_lead_id_returns_terminal_error(status_code, monkeypatch):
    """A 2xx response that lacks leadId must be terminal_error, not completed.

    This is the Warning regression test. Without leadId the lead cannot be
    reconciled against the Acquire side; returning success with target_ref=None
    leaves an unreconcilable lead.
    """
    session = MagicMock()
    session.post.return_value = _make_response(status_code, {"capturedAt": "2026-09-01T12:00:00Z"})

    monkeypatch.setenv("VSA_API_BASE", "https://api.example.com")
    with patch("agents.executors.retail.load_retry_policy", return_value=_fast_policy()):
        result = _executor(session).dispatch(_lead_action(), _claims())

    assert result.status == ActionStatus.FAILED, (
        f"A {status_code} without leadId must be FAILED/terminal_error, not COMPLETED"
    )
    assert result.failure_kind == FailureKind.EXECUTOR_TERMINAL_ERROR
    assert result.last_error_class == "MissingLeadId"
    # last_error_status is intentionally not asserted: ExecuteResult validation
    # rejects a 2xx code on a failure kind (a 2xx-as-failure is incoherent by
    # design); the HTTP status is captured in the note instead.


# ---------------------------------------------------------------------------
# Test 8 — 408 is retried (transient 4xx), not refused
# ---------------------------------------------------------------------------


def test_408_is_retried_not_refused(monkeypatch):
    """408 Request Timeout is transient and must be retried.

    This is the Critical 4 regression test for the transient-4xx path.
    classify_status(408) -> 'transient', so it should not end up as refused.
    """
    session = MagicMock()
    # First call: 408; second: 201 with leadId
    session.post.side_effect = [
        _make_response(408, {"error": "request timeout"}),
        _make_response(201, {"leadId": "LEAD-AFTER-408", "capturedAt": "2026-09-01T12:00:00Z"}),
    ]

    monkeypatch.setenv("VSA_API_BASE", "https://api.example.com")
    policy = RetryPolicy(retry_count=3, backoff_base_ms=0, backoff_cap_ms=0)
    with patch("agents.executors.retail.load_retry_policy", return_value=policy):
        result = _executor(session).dispatch(_lead_action(), _claims())

    assert result.status == ActionStatus.COMPLETED, (
        "After a retried 408, a 201 success should yield COMPLETED"
    )
    assert result.target_ref == "LEAD-AFTER-408"
    assert session.post.call_count == 2, "408 should be retried once"


# ---------------------------------------------------------------------------
# Test 9 — http.post call includes a timeout kwarg
# ---------------------------------------------------------------------------


def test_post_call_includes_timeout_kwarg(monkeypatch):
    """Every http.post call MUST include a timeout kwarg.

    This is the Critical 7 regression test. Without a timeout, a hung
    connection blocks indefinitely, the retry logic never engages, and the
    approve route dies at the gateway with the Action still pending_execution.
    """
    session = MagicMock()
    session.post.return_value = _make_response(201, {"leadId": "LEAD-TIMEOUT-TEST"})

    monkeypatch.setenv("VSA_API_BASE", "https://api.example.com")
    with patch("agents.executors.retail.load_retry_policy", return_value=_fast_policy()):
        _executor(session).dispatch(_lead_action(), _claims())

    assert session.post.call_count >= 1
    # Every post call must carry a timeout kwarg
    for c in session.post.call_args_list:
        assert "timeout" in c.kwargs, (
            "http.post MUST be called with a timeout kwarg; "
            f"got kwargs={c.kwargs!r}"
        )
        assert c.kwargs["timeout"] is not None
        assert c.kwargs["timeout"] > 0, (
            f"timeout must be a positive number, got {c.kwargs['timeout']!r}"
        )


# ---------------------------------------------------------------------------
# Test 10 — 429 is treated as transient (retried, not refused)
# ---------------------------------------------------------------------------


def test_429_is_retried_not_refused(monkeypatch):
    """429 Too Many Requests is transient and must be retried."""
    session = MagicMock()
    # First call: 429; second: 201
    session.post.side_effect = [
        _make_response(429, {"error": "rate limited"}),
        _make_response(201, {"leadId": "LEAD-RETRY", "capturedAt": "2026-09-01T12:00:00Z"}),
    ]

    monkeypatch.setenv("VSA_API_BASE", "https://api.example.com")
    policy = RetryPolicy(retry_count=3, backoff_base_ms=0, backoff_cap_ms=0)
    with patch("agents.executors.retail.load_retry_policy", return_value=policy):
        result = _executor(session).dispatch(_lead_action(), _claims())

    assert result.status == ActionStatus.COMPLETED
    assert result.target_ref == "LEAD-RETRY"
    assert session.post.call_count == 2


# ---------------------------------------------------------------------------
# Test 11 — @never_raises: unexpected exception becomes terminal_error
# ---------------------------------------------------------------------------


def test_unexpected_exception_is_caught_by_never_raises(monkeypatch):
    """An unhandled exception in dispatch must be swallowed and returned as terminal_error."""
    session = MagicMock()
    session.post.side_effect = RuntimeError("unexpected internal error")

    monkeypatch.setenv("VSA_API_BASE", "https://api.example.com")
    policy = RetryPolicy(retry_count=1, backoff_base_ms=0, backoff_cap_ms=0)
    with patch("agents.executors.retail.load_retry_policy", return_value=policy), \
         patch("agents.executors.retail.is_transient_exception", return_value=False):
        result = _executor(session).dispatch(_lead_action(), _claims())

    assert result.status == ActionStatus.FAILED
    assert result.failure_kind == FailureKind.EXECUTOR_TERMINAL_ERROR


# ---------------------------------------------------------------------------
# Test 12 — Optional fields forwarded when present; absent ones omitted
# ---------------------------------------------------------------------------


def test_optional_fields_forwarded_when_present(monkeypatch):
    """sessionId, contactInfo, modelId, discoverSessionId are passed through when supplied."""
    session = MagicMock()
    session.post.return_value = _make_response(201, {"leadId": "LEAD-OPT", "capturedAt": "2026-09-01T12:00:00Z"})

    params = {
        "sessionId": "sess-001",
        "contactInfo": {"name": "Test User", "email": "test@example.com"},
        "modelId": "model-xyz",
        "discoverSessionId": "disc-999",
        # This should be ignored — not a forwarded field
        "tenantId": "ignored-tenant",
    }

    monkeypatch.setenv("VSA_API_BASE", "https://api.example.com")
    with patch("agents.executors.retail.load_retry_policy", return_value=_fast_policy()):
        _executor(session).dispatch(_lead_action(params=params), _claims(tenant_id="real-tenant"))

    call_kwargs = session.post.call_args
    posted_body = call_kwargs[1]["json"] if call_kwargs[1] else call_kwargs[0][1]

    assert posted_body["sessionId"] == "sess-001"
    assert posted_body["contactInfo"] == {"name": "Test User", "email": "test@example.com"}
    assert posted_body["modelId"] == "model-xyz"
    assert posted_body["discoverSessionId"] == "disc-999"
    # tenantId must always come from claims
    assert posted_body["tenantId"] == "real-tenant"


def test_missing_optional_fields_not_in_body(monkeypatch):
    """Fields absent from action parameters must not appear in the outbound body."""
    session = MagicMock()
    session.post.return_value = _make_response(201, {"leadId": "LEAD-MIN", "capturedAt": "2026-09-01T12:00:00Z"})

    monkeypatch.setenv("VSA_API_BASE", "https://api.example.com")
    with patch("agents.executors.retail.load_retry_policy", return_value=_fast_policy()):
        _executor(session).dispatch(_lead_action(params={}), _claims())

    call_kwargs = session.post.call_args
    posted_body = call_kwargs[1]["json"] if call_kwargs[1] else call_kwargs[0][1]

    for optional in ("sessionId", "contactInfo", "modelId", "discoverSessionId"):
        assert optional not in posted_body, f"{optional} must not appear when not supplied"


# ---------------------------------------------------------------------------
# Test 13 — target_system attribute matches expected value
# ---------------------------------------------------------------------------


def test_target_system_attribute():
    """RetailExecutor.target_system must equal the module-level TARGET_SYSTEM."""
    executor = RetailExecutor()
    assert executor.target_system == TARGET_SYSTEM
    assert executor.target_system == "retail"



# ---------------------------------------------------------------------------
# Security review Cycle 1, Warning 1 — the caller's JWT must be forwarded.
#
# The first cut of this executor sent no Authorization header at all. The Acquire
# API is behind the same API Gateway Cognito authorizer as every other route and
# has no M2M credential path, so that was both a functional failure (every
# request 401s, exhausts the retry budget, and lands on a terminal failure) and a
# confused-deputy hazard if the route were instead reachable by the Lambda's own
# role — the executor would act on its own authority for a caller whose
# entitlement was never checked.
#
# Neither the general review nor the mutation battery caught this: every retail
# test stubbed the HTTP client and asserted on the BODY, and no test looked at
# the headers. The sibling executors each had a JWT-forwarding test; retail did
# not, so the absence was invisible.
# ---------------------------------------------------------------------------


def test_request_forwards_caller_jwt() -> None:
    """The outbound request must carry the caller's JWT as a Bearer token."""
    http = MagicMock()
    http.post.return_value = _make_response(201, {"leadId": "LEAD-1"})
    claims = _claims()

    RetailExecutor(http_client=http, sleep=lambda _s: None).dispatch(
        _lead_action(), claims
    )

    headers = http.post.call_args.kwargs.get("headers") or {}
    assert headers.get("Authorization") == f"Bearer {claims['token']}", (
        "the caller's JWT must be forwarded as 'Bearer <token>'; the Acquire API "
        f"has no M2M path. Got headers={headers!r}"
    )


def test_jwt_is_not_placed_in_the_url_or_body() -> None:
    """A credential in a URL lands in access logs; in a body it lands in DDB."""
    http = MagicMock()
    http.post.return_value = _make_response(201, {"leadId": "LEAD-1"})
    claims = _claims()

    RetailExecutor(http_client=http, sleep=lambda _s: None).dispatch(
        _lead_action(), claims
    )

    url = http.post.call_args.args[0] if http.post.call_args.args else ""
    body = http.post.call_args.kwargs.get("json") or {}
    assert claims["token"] not in str(url), "JWT leaked into the request URL"
    assert claims["token"] not in str(body), "JWT leaked into the request body"


@pytest.mark.parametrize("bad_claims", [{}, {"custom:tenantId": "t"}, {"sub": "u"}])
def test_missing_jwt_fails_closed_without_sending_a_request(bad_claims: dict) -> None:
    """No credential means no request — fail closed, do not send an anonymous one.

    Sending it anyway would earn a 401 that, before the Cycle 1 classification
    fix, read as 'the target refused' on the owner's card.
    """
    http = MagicMock()
    result = RetailExecutor(http_client=http, sleep=lambda _s: None).dispatch(
        _lead_action(), bad_claims
    )

    assert result.status == ActionStatus.FAILED
    assert result.failure_kind == FailureKind.EXECUTOR_TERMINAL_ERROR
    http.post.assert_not_called()


def test_jwt_is_never_logged(caplog: pytest.LogCaptureFixture) -> None:
    """A forwarded credential must not reach a log record on any path."""
    jwt_canary = "eyJhbGciOiJSUzI1NiJ9.RETAIL_LOG_CANARY.sig"
    claims = dict(_claims())
    claims["token"] = jwt_canary
    http = MagicMock()
    http.post.side_effect = RuntimeError(f"upstream rejected {jwt_canary}")

    with caplog.at_level(logging.DEBUG):
        RetailExecutor(http_client=http, sleep=lambda _s: None).dispatch(
            _lead_action(), claims
        )

    blob = "\n".join(
        [r.getMessage() for r in caplog.records]
        + [str(r.args) for r in caplog.records]
        + [r.exc_text or "" for r in caplog.records]
    )
    assert jwt_canary not in blob, f"JWT leaked into a log record:\n{blob[:500]}"



# ---------------------------------------------------------------------------
# Review Cycle 2, Critical 4 — the retry SEQUENCE must respect an overall
# deadline, not just bound each attempt.
#
# This executor had a per-attempt timeout but no deadline at all, so a run of
# slow-but-not-timing-out attempts could still overrun the synchronous budget and
# let API Gateway kill the approve route with the Action still on
# `pending_execution` — the exact outcome @never_raises exists to prevent. dms and
# vehicle_command both enforced one; retail did not, and no test noticed.
# ---------------------------------------------------------------------------


def test_retry_sequence_abandons_when_the_deadline_passes(monkeypatch) -> None:
    """Once the budget is spent the sequence stops instead of overrunning."""
    http = MagicMock()
    http.post.return_value = _make_response(503, {"error": "unavailable"})

    # Clock jumps past the deadline right after the first attempt.
    clock = iter([0.0, 0.0, 10_000.0, 10_000.0, 10_000.0, 10_000.0])
    monkeypatch.setattr(
        "agents.executors.retail._monotonic", lambda: next(clock, 10_000.0)
    )

    with patch("agents.executors.retail.load_retry_policy", return_value=_fast_policy()):
        result = RetailExecutor(http_client=http, sleep=lambda _s: None).dispatch(
            _lead_action(), _claims()
        )

    assert result.status == ActionStatus.FAILED
    assert result.failure_kind == FailureKind.EXECUTOR_EXHAUSTED_RETRIES
    assert http.post.call_count < _fast_policy().retry_count or http.post.call_count <= 1, (
        f"the loop kept issuing requests past the deadline "
        f"({http.post.call_count} attempts); the sequence must abandon so the "
        "gateway does not kill the route mid-dispatch"
    )


def test_deadline_is_taken_from_policy_not_a_literal() -> None:
    """The budget must come from the policy so it stays consistent everywhere.

    Scans the whole module rather than a single function: the retry loop lives in
    `_dispatch_inner`, not `dispatch`, and a test pinned to one function name
    breaks on an internal refactor for a reason unrelated to the property.
    """
    from pathlib import Path

    from agents.executors import retail as retail_mod

    src = Path(retail_mod.__file__).read_text(encoding="utf-8")
    assert "policy.deadline_s" in src, (
        "retail does not derive its deadline from the policy; a literal would "
        "drift from base.SYNCHRONOUS_BUDGET_S and from the sibling executors"
    )
    assert "_monotonic() >= deadline" in src, (
        "retail computes a deadline but never compares against it"
    )
