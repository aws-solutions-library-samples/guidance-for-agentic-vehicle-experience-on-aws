"""`ExecuteResult` contract validation — the D5 rules asserted by construction.

Review Cycle 1 listed four D5-violating combinations `ExecuteResult` accepted.
They were fixed; these tests are why the fix can be trusted. Without them the
validation is unguarded code — a later "simplification" could delete any of these
rules and every other test in the package would still pass, because the rest of
the suite builds results through the `.success()` / `.refused()` / `.exhausted()`
/ `.terminal_error()` constructors, which are correct by construction and
therefore never exercise the rejection paths.

That is the gap the mutation battery found: disabling each rule in turn left the
whole suite green.
"""

from __future__ import annotations

import pytest

from agents.executors.base import (
    ActionStatus,
    ExecuteResult,
    FailureKind,
    RetryPolicy,
    classify_status,
    is_transient_status,
)


class TestFailedMustEscalate:
    """Addendum D5: "`failed` triggers escalation per config"."""

    def test_failed_without_escalate_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="escalate=True"):
            ExecuteResult(
                status=ActionStatus.FAILED,
                failure_kind=FailureKind.EXECUTOR_EXHAUSTED_RETRIES,
                retry_count=3,
                last_error_status=503,
                escalate=False,
            )

    def test_declined_with_escalate_is_rejected(self) -> None:
        """The other direction: a refusal must not escalate."""
        with pytest.raises(ValueError, match="escalate=True is only valid"):
            ExecuteResult(
                status=ActionStatus.DECLINED,
                failure_kind=FailureKind.TARGET_REFUSED,
                last_error_status=400,
                escalate=True,
            )

    @pytest.mark.parametrize(
        "ctor",
        [
            lambda: ExecuteResult.exhausted(retry_count=3, last_error_status=503),
            lambda: ExecuteResult.terminal_error(last_error_class="X"),
        ],
    )
    def test_failure_constructors_set_escalate(self, ctor) -> None:  # type: ignore[no-untyped-def]
        assert ctor().escalate is True

    def test_refused_constructor_does_not_escalate(self) -> None:
        assert ExecuteResult.refused(last_error_status=400).escalate is False


class TestSuccessCodeOnFailureStatus:
    """A 2xx recorded as a failure means the response was misread."""

    @pytest.mark.parametrize("code", [200, 201, 202, 204, 299])
    def test_2xx_on_failed_is_rejected(self, code: int) -> None:
        with pytest.raises(ValueError, match="success code"):
            ExecuteResult.terminal_error(last_error_class="MissingRef", last_error_status=code)

    @pytest.mark.parametrize("code", [200, 201])
    def test_2xx_on_declined_is_rejected(self, code: int) -> None:
        """This is the shape the `== 201` success gate actually produced."""
        with pytest.raises(ValueError, match="success code"):
            ExecuteResult.refused(last_error_status=code)


class TestRefusalCodeCoherence:
    """`target_refused` means a 4xx the target chose to send."""

    @pytest.mark.parametrize("code", [500, 502, 503, 504])
    def test_5xx_as_target_refused_is_rejected(self, code: int) -> None:
        with pytest.raises(ValueError, match="requires a 4xx"):
            ExecuteResult.refused(last_error_status=code)

    @pytest.mark.parametrize("code", [408, 425, 429])
    def test_transient_4xx_as_target_refused_is_rejected(self, code: int) -> None:
        with pytest.raises(ValueError, match="transient 4xx"):
            ExecuteResult.refused(last_error_status=code)

    @pytest.mark.parametrize("code", [400, 409, 422])
    def test_genuine_business_refusals_are_accepted(self, code: int) -> None:
        r = ExecuteResult.refused(last_error_status=code)
        assert r.status == ActionStatus.DECLINED
        assert r.retry_count == 0

    def test_retried_refusal_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="retry_count=0"):
            ExecuteResult(
                status=ActionStatus.DECLINED,
                failure_kind=FailureKind.TARGET_REFUSED,
                last_error_status=400,
                retry_count=2,
            )


class TestExhaustionCodeCoherence:
    """The budget can only be exhausted against something retryable."""

    @pytest.mark.parametrize("code", [400, 401, 403, 404, 409, 422])
    def test_terminal_4xx_as_exhausted_is_rejected(self, code: int) -> None:
        with pytest.raises(ValueError, match="never retried"):
            ExecuteResult.exhausted(retry_count=3, last_error_status=code)

    @pytest.mark.parametrize("code", [500, 503, 408, 425, 429, None])
    def test_retryable_codes_are_accepted_as_exhausted(self, code: int | None) -> None:
        r = ExecuteResult.exhausted(retry_count=3, last_error_status=code)
        assert r.status == ActionStatus.FAILED
        assert r.failure_kind == FailureKind.EXECUTOR_EXHAUSTED_RETRIES

    def test_terminal_error_accepts_client_error_codes(self) -> None:
        """`executor_terminal_error` is the kind that carries auth/plumbing 4xx."""
        for code in (401, 403, 404, 405, 415, None):
            r = ExecuteResult.terminal_error(last_error_class="ClientError", last_error_status=code)
            assert r.status == ActionStatus.FAILED
            assert r.escalate is True


class TestStatusEnumBoundary:
    @pytest.mark.parametrize("status", ["dismissed", "cancelled", "pending_execution", "nonsense"])
    def test_non_executor_writable_statuses_are_rejected(self, status: str) -> None:
        """An executor that could write `dismissed` could forge owner intent."""
        with pytest.raises(ValueError, match="not executor-writable"):
            ExecuteResult(status=status)

    def test_success_status_with_failure_kind_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="success status"):
            ExecuteResult(
                status=ActionStatus.COMPLETED,
                failure_kind=FailureKind.TARGET_REFUSED,
            )


class TestClassifyStatus:
    """One source of truth, so six executors cannot disagree about a 403."""

    @pytest.mark.parametrize("code", [500, 502, 503, 504, 408, 425, 429])
    def test_transient(self, code: int) -> None:
        assert classify_status(code) == "transient"
        assert is_transient_status(code) is True

    @pytest.mark.parametrize("code", [401, 403, 404, 405, 415])
    def test_client_error(self, code: int) -> None:
        assert classify_status(code) == "client_error"
        assert is_transient_status(code) is False

    @pytest.mark.parametrize("code", [400, 409, 422, 451])
    def test_refused(self, code: int) -> None:
        assert classify_status(code) == "refused"
        assert is_transient_status(code) is False

    @pytest.mark.parametrize("code", [200, 201, 204])
    def test_success_codes_are_a_caller_bug(self, code: int) -> None:
        with pytest.raises(ValueError, match="success code"):
            classify_status(code)


class TestAuditEntry:
    def test_failure_entry_carries_kind_and_retry_count(self) -> None:
        entry = ExecuteResult.exhausted(
            retry_count=3, last_error_class="HTTPError", last_error_status=503
        ).audit_entry(by="owner-1")
        assert entry["to_status"] == ActionStatus.FAILED
        assert entry["failure_kind"] == FailureKind.EXECUTOR_EXHAUSTED_RETRIES
        assert entry["retry_count"] == 3
        assert entry["last_error_class"] == "HTTPError"
        assert entry["last_error_status"] == 503
        assert entry["from_status"] == ActionStatus.PENDING_EXECUTION

    def test_success_entry_carries_no_failure_fields(self) -> None:
        entry = ExecuteResult.success(target_ref="RO-1").audit_entry(by="owner-1")
        assert "failure_kind" not in entry
        assert "retry_count" not in entry

    def test_connection_error_records_a_null_status_explicitly(self) -> None:
        """A timeout has no HTTP status; the operator must see that, not a gap."""
        entry = ExecuteResult.exhausted(
            retry_count=3, last_error_class="ConnectionError"
        ).audit_entry(by="owner-1")
        assert "last_error_status" in entry
        assert entry["last_error_status"] is None


class TestRetryPolicyBudget:
    def test_defaults_match_the_confirmed_staging_config(self) -> None:
        p = RetryPolicy()
        assert (p.retry_count, p.backoff_base_ms, p.backoff_cap_ms) == (3, 1000, 4000)

    def test_cap_below_base_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="truncate"):
            RetryPolicy(backoff_base_ms=4000, backoff_cap_ms=1000)

    def test_zero_attempts_is_rejected(self) -> None:
        with pytest.raises(ValueError, match=">= 1"):
            RetryPolicy(retry_count=0)

    def test_full_jitter_spans_the_window_and_never_exceeds_it(self) -> None:
        p = RetryPolicy()
        assert p.sleep_ms(0, rand=lambda: 0.0) == 0.0
        assert p.sleep_ms(0, rand=lambda: 1.0) == 1000
        # capped
        assert p.sleep_ms(9, rand=lambda: 1.0) == 4000



# ---------------------------------------------------------------------------
# Security review Cycle 1, Warning 5 — the malformed-policy log must not echo
# the parameter content. Same defect class as Cycle 1 Critical 1 (@never_raises
# logging the exception message), at a smaller site: a ValueError raised while
# coercing a malformed SSM value quotes the offending value back, and that value
# comes from Parameter Store, which is not a place to assume nothing sensitive
# ever lands.
# ---------------------------------------------------------------------------


class TestPolicyLoadDoesNotLogParameterContent:
    def test_malformed_policy_value_is_not_echoed(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        import logging as _logging

        from agents.executors import base

        canary = "SENSITIVE_PARAM_CONTENT_CANARY"

        class _Stub:
            def get_parameter(self, Name: str) -> dict:  # noqa: N803
                # Valid JSON object, but retry_count cannot be coerced to int —
                # the TypeError/ValueError message will contain the bad value.
                return {"Parameter": {"Value": '{"retry_count": "%s"}' % canary}}

        base.reset_policy_cache()
        with caplog.at_level(_logging.DEBUG):
            policy = base.load_retry_policy("dms", ssm_client=_Stub())
        base.reset_policy_cache()

        # Falls back safely rather than raising.
        assert policy.retry_count == base.DEFAULT_RETRY_POLICY.retry_count

        blob = "\n".join(
            [r.getMessage() for r in caplog.records]
            + [str(r.args) for r in caplog.records]
            + [r.exc_text or "" for r in caplog.records]
        )
        assert canary not in blob, (
            "the malformed-policy warning echoed the parameter value into the "
            f"log. Log only the exception class name.\n{blob[:400]}"
        )
        assert any("malformed" in r.getMessage() for r in caplog.records), (
            "the malformed value was swallowed with no warning at all — the "
            "operator's configuration was ignored and nothing said so"
        )
