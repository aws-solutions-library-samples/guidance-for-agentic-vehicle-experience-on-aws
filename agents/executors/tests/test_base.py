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
        assert entry["error_status"] == 503
        assert entry["from_status"] == ActionStatus.PENDING_EXECUTION

    def test_success_entry_carries_no_failure_fields(self) -> None:
        entry = ExecuteResult.success(target_ref="RO-1").audit_entry(by="owner-1")
        assert "failure_kind" not in entry
        assert "retry_count" not in entry

    def test_connection_error_omits_error_status(self) -> None:
        """A timeout has no HTTP status; the entry omits error_status — never null
        (spec D14: 'writers omit an absent field; neither is ever null')."""
        entry = ExecuteResult.exhausted(
            retry_count=3, last_error_class="ConnectionError"
        ).audit_entry(by="owner-1")
        assert "error_status" not in entry, (
            "error_status must be omitted when absent, never written as null"
        )

    def test_audit_entry_validates_against_schema(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Each audit_entry() projection validates against $defs/audit_entry with jsonschema.

        review FG2b W8: the original test only checked key names and the
        failure_reason pattern regex — it never called jsonschema.  Three of the
        seven original fixtures were also schema-invalid: refused/exhausted/
        terminal_error *with an error_status* each lacked failure_reason, which
        violates the schema's allOf rule 5 (error_status → failure_reason required).

        Fix: build every case through registry.dispatch() so dispatch() sets
        failure_reason (D14: 'one builder').  Validate each audit_entry() result
        with jsonschema Draft 2020-12 against $defs/audit_entry.

        review FG3b S4: the prior version excluded last_error_class variants because
        ``additionalProperties: false`` rejects them; and its docstring claimed the
        field is "stripped before the audit trail is written", which is inaccurate —
        ``audit_entry()`` includes it when set, and the *caller* (DynamoDB writer /
        evaluator) drops it.  The test now projects entries onto the schema's known
        properties before jsonschema validation so the last_error_class cases can
        be exercised.

        review FG3b S3: after schema validation, assert that ``failure_reason``
        embeds the HTTP status and attempt count where the sentence vocabulary
        requires them — catching mutants that call the builder with ``error_status=None``
        or with the wrong ``retry_count``.
        """
        import json  # noqa: PLC0415
        import pathlib  # noqa: PLC0415
        import jsonschema  # noqa: PLC0415

        from agents.executors import registry, base as _base  # noqa: PLC0415

        schema_path = (
            pathlib.Path(__file__).resolve().parents[3]
            / "agents" / "tier2" / "schemas" / "action.schema.json"
        )
        full_schema = json.loads(schema_path.read_text())
        ae_schema = full_schema["$defs"]["audit_entry"]
        _schema_props = set(ae_schema.get("properties", {}).keys())

        TARGET = "dms"
        ACTION = {
            "action_id": "AVX-ACT-00000000-0000-4000-8000-000000000001",
            "finding_id": "FIND-00000000-0000-4000-8000-000000000001",
            "action_kind": "book_recall_service",
            "target_system": TARGET,
            "parameters": {},
        }
        CLAIMS = {"sub": "operator-1"}
        AT = "2026-10-02T00:00:00Z"

        # Include last_error_class variants: audit_entry() emits last_error_class
        # when set, but the schema's additionalProperties: false does not include it
        # (it is an internal field the caller projects away before persisting).
        # We validate by projecting onto _schema_props first.
        executor_results = [
            ExecuteResult.refused(),
            ExecuteResult.refused(last_error_status=400),
            ExecuteResult.refused(last_error_class="ValidationError"),
            ExecuteResult.exhausted(retry_count=3),
            ExecuteResult.exhausted(retry_count=3, last_error_status=503),
            ExecuteResult.exhausted(retry_count=3, last_error_class="ConnectionError"),
            ExecuteResult.terminal_error(),
            ExecuteResult.terminal_error(last_error_status=500),
            ExecuteResult.terminal_error(last_error_class="ValueError"),
            ExecuteResult.success(target_ref="RO-1"),
        ]

        validator = jsonschema.Draft202012Validator(ae_schema)

        for raw_result in executor_results:

            class _StubExecutor:
                target_system = TARGET

                @_base.never_raises
                def dispatch(self, action, claims):  # type: ignore[no-untyped-def]
                    return raw_result

            monkeypatch.setitem(registry.EXECUTORS, TARGET, _StubExecutor())
            result = registry.dispatch(ACTION, CLAIMS, fire_escalation=False)
            entry = result.audit_entry(by=f"executor-{TARGET}", at=AT)

            # Project onto schema-known keys before validation (last_error_class is
            # an internal field dropped by the caller; it must not appear in the
            # DynamoDB row but may appear in the in-memory entry).
            projected = {k: v for k, v in entry.items() if k in _schema_props}

            errors = sorted(validator.iter_errors(projected), key=lambda e: str(e.path))
            assert not errors, (
                f"audit_entry for to_status={result.status!r} (last_error_class="
                f"{getattr(raw_result, 'last_error_class', None)!r}) "
                f"failed $defs/audit_entry validation after projection: "
                + "; ".join(
                    f"[{'.'.join(str(p) for p in e.path)}] {e.message}"
                    for e in errors
                )
            )

            # review FG3b S3: pin the failure_reason sentence content so that a
            # builder called with error_status=None or retry_count=1 is caught.
            reason = entry.get("failure_reason")
            if reason is not None:
                error_status = getattr(raw_result, "last_error_status", None)
                retry_count = getattr(raw_result, "retry_count", 0)
                if error_status is not None:
                    assert f"(HTTP {error_status})" in reason, (
                        f"failure_reason must contain '(HTTP {error_status})' for "
                        f"error_status={error_status}; got {reason!r}"
                    )
                if retry_count and retry_count > 0:
                    attempt_word = "1 attempt" if retry_count == 1 else f"{retry_count} attempts"
                    assert attempt_word in reason, (
                        f"failure_reason must contain '{attempt_word}' for "
                        f"retry_count={retry_count}; got {reason!r}"
                    )


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
