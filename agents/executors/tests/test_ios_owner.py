"""Tests for IosOwnerExecutor.

Spec: `.kiro/specs/2026-09-18-avx-tier2-ios-cards-and-actions/spec.md`
      § "Executor dispatch abstraction", target_system = 'ios-owner'

Required test groups:

1.  Feed row written to the table resolved from AVX_ACTIONS_TABLE with the
    owner scope set.
2.  target_ref is the written row id.
3.  Notification path is triggered only indirectly — assert NO direct
    SNS / sns:Publish call is made; assert that a push did NOT happen.
4.  DynamoDB ClientError maps to failed/executor_terminal_error rather
    than raising.
5.  No-SNS boundary test: AST-scan ios_owner.py for SNS imports and
    `boto3.client('sns')` / `boto3.resource('sns')` calls; FAILS if any
    are present.  Test is non-vacuous: asserts the module was actually
    parsed and at least one import was found.

Mutation evidence must be pasted in the task hand-back.
"""

from __future__ import annotations

import ast
import logging
import os
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, call, patch

import pytest

from agents.executors.base import ActionStatus, ExecuteResult, FailureKind
from agents.executors.ios_owner import IosOwnerExecutor, _actions_table_name


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_ddb_stub() -> MagicMock:
    """Return a mock DynamoDB resource whose Table().put_item() succeeds."""
    stub = MagicMock()
    stub.Table.return_value = MagicMock()
    stub.Table.return_value.put_item.return_value = {"ResponseMetadata": {"HTTPStatusCode": 200}}
    return stub


def _make_executor(ddb: Any = None) -> IosOwnerExecutor:
    """Build an IosOwnerExecutor with an injected DDB stub."""
    return IosOwnerExecutor(ddb_resource=ddb or _make_ddb_stub())


SAMPLE_ACTION: dict[str, Any] = {
    "id": "act-owner-001",
    "owner_id": "owner-sub-abc",
    "category": "service",
    "severity": "informational",
    "headline": "Your tire inspection is ready to book.",
    "evidence": [{"type": "finding", "ref": "finding-001"}],
}

SAMPLE_CLAIMS: dict[str, Any] = {
    "sub": "dealer-user-99",
    "custom:role": "service_advisor",
}


# ---------------------------------------------------------------------------
# 1. Feed row written to the correct table with owner scope
# ---------------------------------------------------------------------------


class TestFeedRowWritten:
    """Group 1: the row lands in the right table with the owner scope set."""

    def test_put_item_called_once(self) -> None:
        ddb = _make_ddb_stub()
        executor = IosOwnerExecutor(ddb_resource=ddb)
        executor.dispatch(SAMPLE_ACTION, SAMPLE_CLAIMS)
        ddb.Table.return_value.put_item.assert_called_once()

    def test_table_name_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The table used for put_item comes from AVX_ACTIONS_TABLE env var."""
        monkeypatch.setenv("AVX_ACTIONS_TABLE", "test-custom-actions-table")
        ddb = _make_ddb_stub()
        executor = IosOwnerExecutor(ddb_resource=ddb)
        executor.dispatch(SAMPLE_ACTION, SAMPLE_CLAIMS)
        ddb.Table.assert_called_once_with("test-custom-actions-table")

    def test_table_name_default_uses_vsa_stage(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Without AVX_ACTIONS_TABLE set, table name is vsa-{VSA_STAGE}-avx-actions."""
        monkeypatch.delenv("AVX_ACTIONS_TABLE", raising=False)
        monkeypatch.setenv("VSA_STAGE", "prod")
        # _actions_table_name is a module-level helper; test it directly too.
        name = _actions_table_name()
        assert name == "vsa-prod-avx-actions"

    def test_table_name_default_stage_is_staging(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """With neither env var set, default stage is 'staging'."""
        monkeypatch.delenv("AVX_ACTIONS_TABLE", raising=False)
        monkeypatch.delenv("VSA_STAGE", raising=False)
        name = _actions_table_name()
        assert name == "vsa-staging-avx-actions"

    def test_written_item_has_owner_scope(self) -> None:
        """The written row must carry scope.owner_id from action['owner_id']."""
        ddb = _make_ddb_stub()
        executor = IosOwnerExecutor(ddb_resource=ddb)
        executor.dispatch(SAMPLE_ACTION, SAMPLE_CLAIMS)
        _, kwargs = ddb.Table.return_value.put_item.call_args
        item = kwargs.get("Item") or ddb.Table.return_value.put_item.call_args[1].get("Item")
        if item is None:
            # positional call: put_item(Item=...)
            args, kwargs = ddb.Table.return_value.put_item.call_args
            item = kwargs.get("Item", args[0] if args else {})
        assert item["scope"]["owner_id"] == "owner-sub-abc"

    def test_written_item_has_owner_id(self) -> None:
        """The row's top-level owner_id field must match action['owner_id']."""
        ddb = _make_ddb_stub()
        executor = IosOwnerExecutor(ddb_resource=ddb)
        executor.dispatch(SAMPLE_ACTION, SAMPLE_CLAIMS)
        _, kwargs = ddb.Table.return_value.put_item.call_args
        item = kwargs.get("Item") or {}
        assert item.get("owner_id") == "owner-sub-abc"

    def test_written_item_has_status_in_progress(self) -> None:
        """The written row's status must be IN_PROGRESS."""
        ddb = _make_ddb_stub()
        executor = IosOwnerExecutor(ddb_resource=ddb)
        executor.dispatch(SAMPLE_ACTION, SAMPLE_CLAIMS)
        _, kwargs = ddb.Table.return_value.put_item.call_args
        item = kwargs.get("Item") or {}
        assert item.get("status") == ActionStatus.IN_PROGRESS

    def test_written_item_carries_category_and_severity(self) -> None:
        """Category and severity from the action are persisted in the row."""
        ddb = _make_ddb_stub()
        executor = IosOwnerExecutor(ddb_resource=ddb)
        executor.dispatch(SAMPLE_ACTION, SAMPLE_CLAIMS)
        _, kwargs = ddb.Table.return_value.put_item.call_args
        item = kwargs.get("Item") or {}
        assert item.get("category") == "service"
        assert item.get("severity") == "informational"


# ---------------------------------------------------------------------------
# 2. target_ref is the written row id
# ---------------------------------------------------------------------------


class TestTargetRef:
    """Group 2: target_ref in the ExecuteResult is the written row's action_id."""

    def test_target_ref_is_str(self) -> None:
        executor = _make_executor()
        result = executor.dispatch(SAMPLE_ACTION, SAMPLE_CLAIMS)
        assert isinstance(result.target_ref, str)

    def test_target_ref_matches_written_row_id(self) -> None:
        """target_ref must equal the action_id written into the DDB item."""
        ddb = _make_ddb_stub()
        executor = IosOwnerExecutor(ddb_resource=ddb)
        result = executor.dispatch(SAMPLE_ACTION, SAMPLE_CLAIMS)
        _, kwargs = ddb.Table.return_value.put_item.call_args
        item = kwargs.get("Item") or {}
        written_id = item.get("action_id")
        assert result.target_ref == written_id, (
            f"target_ref={result.target_ref!r} does not match written "
            f"action_id={written_id!r}"
        )

    def test_target_ref_is_not_none_on_success(self) -> None:
        executor = _make_executor()
        result = executor.dispatch(SAMPLE_ACTION, SAMPLE_CLAIMS)
        assert result.target_ref is not None

    def test_status_is_in_progress(self) -> None:
        executor = _make_executor()
        result = executor.dispatch(SAMPLE_ACTION, SAMPLE_CLAIMS)
        assert result.status == ActionStatus.IN_PROGRESS

    def test_target_system_attribute(self) -> None:
        assert IosOwnerExecutor.target_system == "ios-owner"
        assert IosOwnerExecutor().target_system == "ios-owner"


# ---------------------------------------------------------------------------
# 3. Notification is indirect only — no SNS call
# ---------------------------------------------------------------------------


class TestNoDirectSns:
    """Group 3: assert that dispatch makes NO direct SNS / sns:Publish call.

    The notification path is INDIRECT: the DDB write is picked up by a stream
    processor.  We assert the absence of the direct path, not the presence of
    the indirect one (the indirect path is outside this executor's scope).
    """

    def test_no_sns_client_constructed(self) -> None:
        """boto3.client must not be called with 'sns' during dispatch."""
        ddb = _make_ddb_stub()
        executor = IosOwnerExecutor(ddb_resource=ddb)
        with patch("boto3.client") as mock_client:
            executor.dispatch(SAMPLE_ACTION, SAMPLE_CLAIMS)
            for c in mock_client.call_args_list:
                args, kwargs = c
                service = args[0] if args else kwargs.get("service_name", "")
                assert service != "sns", (
                    "dispatch called boto3.client('sns') — this bypasses the broker "
                    "and violates the load-bearing constraint."
                )

    def test_no_sns_resource_constructed(self) -> None:
        """boto3.resource must not be called with 'sns' during dispatch."""
        ddb = _make_ddb_stub()
        executor = IosOwnerExecutor(ddb_resource=ddb)
        with patch("boto3.resource") as mock_resource:
            executor.dispatch(SAMPLE_ACTION, SAMPLE_CLAIMS)
            for c in mock_resource.call_args_list:
                args, kwargs = c
                service = args[0] if args else kwargs.get("service_name", "")
                assert service != "sns", (
                    "dispatch called boto3.resource('sns') — this bypasses the broker."
                )

    def test_no_push_call_on_success(self) -> None:
        """A successful write does not trigger any APNs / push side-effect."""
        ddb = _make_ddb_stub()
        executor = IosOwnerExecutor(ddb_resource=ddb)
        # The only side effect must be the one put_item call on the DDB table.
        result = executor.dispatch(SAMPLE_ACTION, SAMPLE_CLAIMS)
        assert result.status == ActionStatus.IN_PROGRESS
        # put_item was called exactly once
        assert ddb.Table.return_value.put_item.call_count == 1
        # No SNS publish anywhere on the stub
        assert not any(
            "publish" in str(c) for c in ddb.method_calls
        ), "Unexpected 'publish' call found on DDB stub — possible SNS confusion."


# ---------------------------------------------------------------------------
# 4. DynamoDB error maps to failed/executor_terminal_error
# ---------------------------------------------------------------------------


class TestDdbErrorHandling:
    """Group 4: DynamoDB errors are caught and returned as terminal_error."""

    def test_client_error_returns_failed(self) -> None:
        """A DDB ClientError must return FAILED, not raise."""
        from botocore.exceptions import ClientError  # type: ignore[import]

        error_response = {
            "Error": {"Code": "ProvisionedThroughputExceededException", "Message": "too fast"}
        }
        ddb = _make_ddb_stub()
        ddb.Table.return_value.put_item.side_effect = ClientError(
            error_response, "PutItem"
        )
        executor = IosOwnerExecutor(ddb_resource=ddb)
        result = executor.dispatch(SAMPLE_ACTION, SAMPLE_CLAIMS)
        assert result.status == ActionStatus.FAILED

    def test_client_error_has_terminal_error_kind(self) -> None:
        from botocore.exceptions import ClientError

        error_response = {"Error": {"Code": "ResourceNotFoundException", "Message": "no table"}}
        ddb = _make_ddb_stub()
        ddb.Table.return_value.put_item.side_effect = ClientError(error_response, "PutItem")
        executor = IosOwnerExecutor(ddb_resource=ddb)
        result = executor.dispatch(SAMPLE_ACTION, SAMPLE_CLAIMS)
        assert result.failure_kind == FailureKind.EXECUTOR_TERMINAL_ERROR

    def test_client_error_does_not_raise(self) -> None:
        from botocore.exceptions import ClientError

        error_response = {"Error": {"Code": "InternalServerError", "Message": "oops"}}
        ddb = _make_ddb_stub()
        ddb.Table.return_value.put_item.side_effect = ClientError(error_response, "PutItem")
        executor = IosOwnerExecutor(ddb_resource=ddb)
        # Must not raise — @never_raises guarantees this.
        result = executor.dispatch(SAMPLE_ACTION, SAMPLE_CLAIMS)
        assert isinstance(result, ExecuteResult)

    def test_generic_error_returns_failed(self) -> None:
        """Any exception from put_item (not just ClientError) must return FAILED."""
        ddb = _make_ddb_stub()
        ddb.Table.return_value.put_item.side_effect = RuntimeError("unexpected crash")
        executor = IosOwnerExecutor(ddb_resource=ddb)
        result = executor.dispatch(SAMPLE_ACTION, SAMPLE_CLAIMS)
        assert result.status == ActionStatus.FAILED
        assert result.failure_kind == FailureKind.EXECUTOR_TERMINAL_ERROR

    def test_error_result_records_exception_class(self) -> None:
        """The error result must carry the exception class name."""
        ddb = _make_ddb_stub()
        ddb.Table.return_value.put_item.side_effect = ValueError("bad value")
        executor = IosOwnerExecutor(ddb_resource=ddb)
        result = executor.dispatch(SAMPLE_ACTION, SAMPLE_CLAIMS)
        assert result.last_error_class == "ValueError"

    def test_error_result_target_ref_is_none(self) -> None:
        """On failure, target_ref must be None (no row was written)."""
        ddb = _make_ddb_stub()
        ddb.Table.return_value.put_item.side_effect = OSError("connection refused")
        executor = IosOwnerExecutor(ddb_resource=ddb)
        result = executor.dispatch(SAMPLE_ACTION, SAMPLE_CLAIMS)
        assert result.target_ref is None


# ---------------------------------------------------------------------------
# 5. No-SNS boundary test (AST scan)
# ---------------------------------------------------------------------------


def _ios_owner_source() -> str:
    """Return the source text of ios_owner.py."""
    module_path = Path(__file__).parent.parent / "ios_owner.py"
    return module_path.read_text(encoding="utf-8")


def _ast_import_names(source: str) -> list[str]:
    """Return all top-level module names from import statements."""
    tree = ast.parse(source)
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.append(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                names.append(node.module.split(".")[0])
    return names


def _ast_sns_client_calls(source: str) -> list[str]:
    """Return boto3.client('sns') or boto3.resource('sns') call sites in source."""
    tree = ast.parse(source)
    hits: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        # Match boto3.client(...) or boto3.resource(...)
        if not (
            isinstance(func, ast.Attribute)
            and isinstance(func.value, ast.Name)
            and func.value.id == "boto3"
            and func.attr in {"client", "resource"}
        ):
            continue
        # Check the first positional argument is the string 'sns'
        args = node.args
        if args and isinstance(args[0], ast.Constant) and args[0].value == "sns":
            hits.append(f"boto3.{func.attr}('sns') at line {node.lineno}")
    return hits


class TestNoSnsImport:
    """Group 5: AST boundary test — ios_owner.py must have no SNS surface.

    This test is non-vacuous: it asserts the source was actually parsed and
    that at least one import was found, preventing a false pass on an empty
    or unreadable file.

    Mutation evidence in the task hand-back proves the test fails when SNS
    is introduced.
    """

    def test_module_is_non_empty_and_has_imports(self) -> None:
        """Non-vacuity check: the module is parseable and has at least one import."""
        source = _ios_owner_source()
        assert len(source) > 0, "ios_owner.py is empty — nothing to scan."
        import_names = _ast_import_names(source)
        assert len(import_names) >= 1, (
            "ios_owner.py has no imports at all — the AST scan below would trivially "
            "pass by finding nothing.  Add at least one import (expected: os, boto3, etc.)."
        )

    def test_no_sns_import(self) -> None:
        """ios_owner.py must not import the `sns` module or service."""
        source = _ios_owner_source()
        import_names = _ast_import_names(source)
        # Explicit check: 'sns' must not appear as a standalone import name.
        assert "sns" not in import_names, (
            f"ios_owner.py imports 'sns' — this violates the load-bearing constraint "
            f"that the executor MUST NOT reference any SNS client.  "
            f"All imports found: {import_names}"
        )

    def test_no_boto3_sns_client_call(self) -> None:
        """ios_owner.py must not contain boto3.client('sns') or boto3.resource('sns')."""
        source = _ios_owner_source()
        hits = _ast_sns_client_calls(source)
        assert not hits, (
            "ios_owner.py constructs an SNS client/resource — this MUST NOT happen.  "
            "The broker owns channel policy; a direct SNS publish would bypass consent "
            "settings, quiet hours, and rate limits.  "
            f"Offending calls: {hits}"
        )

    def test_no_sns_string_literal_in_client_calls(self) -> None:
        """Belt-and-suspenders: scan for any boto3 call whose first arg is 'sns'."""
        source = _ios_owner_source()
        hits = _ast_sns_client_calls(source)
        assert not hits, (
            f"boto3.*('sns') call found in ios_owner.py: {hits}"
        )


# ---------------------------------------------------------------------------
# 6. Fail closed: empty / missing owner_id returns terminal_error (C3 fix)
# ---------------------------------------------------------------------------


class TestEmptyOwnerIdFailsClosed:
    """Group 6: an empty or missing owner_id must produce terminal_error, not success.

    Rationale: a row written with owner_id="" lands in DynamoDB but is invisible
    to every owner-scoped GSI query.  The owner is never told and the system
    records success — a silent failure.  Fail closed instead.

    Mutation evidence (pasted in hand-back):
      M-owner: revert to writing the row with empty owner_id and returning
      success → test_empty_owner_id_returns_terminal_error FAILS.
    """

    def test_empty_owner_id_returns_terminal_error(self) -> None:
        """action with owner_id='' must return FAILED/executor_terminal_error."""
        ddb = _make_ddb_stub()
        action_no_owner = {
            "action_id": "act-no-owner",
            "owner_id": "",          # explicitly empty
            "category": "service",
            "headline": "Test",
            "evidence": [],
        }
        executor = IosOwnerExecutor(ddb_resource=ddb)
        result = executor.dispatch(action_no_owner, SAMPLE_CLAIMS)

        assert result.status == ActionStatus.FAILED, (
            f"empty owner_id must return FAILED, got {result.status!r}. "
            "A row written to DDB with owner_id='' is invisible to every "
            "owner-scoped query — the owner is never told and the system "
            "records success, which is a silent failure."
        )
        assert result.failure_kind == FailureKind.EXECUTOR_TERMINAL_ERROR
        # The row must NOT have been written
        ddb.Table.return_value.put_item.assert_not_called()

    def test_absent_owner_id_returns_terminal_error(self) -> None:
        """action with no owner_id key at all must return FAILED/executor_terminal_error."""
        ddb = _make_ddb_stub()
        action_no_owner = {
            "action_id": "act-no-owner-key",
            # owner_id key is entirely absent
            "category": "service",
            "headline": "Test",
        }
        executor = IosOwnerExecutor(ddb_resource=ddb)
        result = executor.dispatch(action_no_owner, SAMPLE_CLAIMS)

        assert result.status == ActionStatus.FAILED
        assert result.failure_kind == FailureKind.EXECUTOR_TERMINAL_ERROR
        ddb.Table.return_value.put_item.assert_not_called()

    def test_none_owner_id_returns_terminal_error(self) -> None:
        """action with owner_id=None must return FAILED/executor_terminal_error."""
        ddb = _make_ddb_stub()
        action_none_owner = {
            "action_id": "act-none-owner",
            "owner_id": None,
        }
        executor = IosOwnerExecutor(ddb_resource=ddb)
        result = executor.dispatch(action_none_owner, SAMPLE_CLAIMS)

        assert result.status == ActionStatus.FAILED
        assert result.failure_kind == FailureKind.EXECUTOR_TERMINAL_ERROR
        ddb.Table.return_value.put_item.assert_not_called()

    def test_owner_id_from_claims_is_never_used(self) -> None:
        """Even if claims['sub'] is present, owner_id must come from action only.

        Design invariant: owner identity is never derived from claims for this
        executor — the caller must supply owner_id in the action document.
        A missing action.owner_id is terminal_error even if claims['sub'] exists.
        """
        ddb = _make_ddb_stub()
        action_no_owner = {"action_id": "act-claims-test", "owner_id": ""}
        claims_with_sub = {"sub": "fallback-sub-should-not-be-used"}
        executor = IosOwnerExecutor(ddb_resource=ddb)
        result = executor.dispatch(action_no_owner, claims_with_sub)

        # Must still fail: claims['sub'] must NOT be used as owner_id
        assert result.status == ActionStatus.FAILED, (
            "owner identity must come from action['owner_id'] only, not from "
            "claims['sub'] — using claims would allow a service account to write "
            "inbox cards scoped to itself rather than the target owner."
        )
        ddb.Table.return_value.put_item.assert_not_called()


# ---------------------------------------------------------------------------
# 7. Idempotent row_id: repeated dispatch does not create a second row
# ---------------------------------------------------------------------------


class TestIdempotentRowId:
    """Group 7: a repeated dispatch of the same action_id overwrites the same row.

    Rationale: @never_raises may re-dispatch on a transient exception, and the
    escalation path can also replay the same action.  Without a stable row id,
    each attempt creates a new inbox card — a duplicate notification to the owner.

    Two verification strategies:
      A. Assert that both dispatches write the SAME action_id into the DDB item.
      B. Explicit documentation test: when action_id is absent, a comment
         stating de-duplication is the caller's responsibility.

    Mutation evidence (pasted in hand-back):
      M-rowid: revert row_id to str(uuid.uuid4()) unconditionally →
      test_repeated_dispatch_writes_same_row_id FAILS because the two written
      ids are different UUIDs.
    """

    def test_repeated_dispatch_writes_same_row_id(self) -> None:
        """Two dispatches of the same action_id write to the same row id."""
        ddb_1 = _make_ddb_stub()
        ddb_2 = _make_ddb_stub()

        action = {**SAMPLE_ACTION, "action_id": "act-idem-001"}
        executor_1 = IosOwnerExecutor(ddb_resource=ddb_1)
        executor_2 = IosOwnerExecutor(ddb_resource=ddb_2)

        result_1 = executor_1.dispatch(action, SAMPLE_CLAIMS)
        result_2 = executor_2.dispatch(action, SAMPLE_CLAIMS)

        assert result_1.target_ref == result_2.target_ref, (
            "Two dispatches of the same action_id must produce the same row_id "
            f"(first={result_1.target_ref!r}, second={result_2.target_ref!r}). "
            "An unconditional uuid.uuid4() would make them differ — that is the "
            "mutation this test guards against."
        )

    def test_row_id_equals_action_id(self) -> None:
        """row_id written into the DDB item equals the action's action_id."""
        ddb = _make_ddb_stub()
        action = {**SAMPLE_ACTION, "action_id": "act-row-id-check"}
        executor = IosOwnerExecutor(ddb_resource=ddb)
        result = executor.dispatch(action, SAMPLE_CLAIMS)

        _, kwargs = ddb.Table.return_value.put_item.call_args
        item = kwargs.get("Item") or {}
        assert item.get("action_id") == "act-row-id-check", (
            "The DDB item's action_id must equal the input action_id so that a "
            "DynamoDB conditional expression can enforce 'attribute_not_exists' "
            "for true idempotency — a mismatch means a retry would always insert."
        )
        assert result.target_ref == "act-row-id-check"

    def test_different_action_ids_write_different_rows(self) -> None:
        """Two different action_ids produce different row ids (no collision)."""
        ddb_a = _make_ddb_stub()
        ddb_b = _make_ddb_stub()
        action_a = {**SAMPLE_ACTION, "action_id": "act-alpha"}
        action_b = {**SAMPLE_ACTION, "action_id": "act-beta"}

        r_a = IosOwnerExecutor(ddb_resource=ddb_a).dispatch(action_a, SAMPLE_CLAIMS)
        r_b = IosOwnerExecutor(ddb_resource=ddb_b).dispatch(action_b, SAMPLE_CLAIMS)

        assert r_a.target_ref != r_b.target_ref



# ---------------------------------------------------------------------------
# Review Cycle 5 Warning 2 — the owner_id log fix was correct in source and
# UNPINNED: reverting the argument left 33 tests passing.
#
# The history is the point. Cycle 1/2 removed owner_id from the INFO log. Cycle 3
# W9 said the DDB-failure log still emitted it. My Cycle 4 "fix" changed the
# FORMAT STRING from `owner_id=%s` to `row id=%s` and left the ARGUMENT as
# `owner_id` — so the PII was still logged and now mislabeled, and I "verified" it
# by grepping the format string. Cycle 5 then found the corrected version had no
# test either, and Cycle 4 had explicitly asked for this assertion.
#
# Twice proven by hand, twice not pinned. A hand-proof establishes the state once;
# only a test keeps it.
# ---------------------------------------------------------------------------

_PII_CANARY = "owner-SENTINEL-PII-9f3a"


def _executor_with_failing_put(exc: Exception):
    """An executor whose put_item raises, to drive the failure-log path."""
    ddb = MagicMock()
    ddb.Table.return_value.put_item.side_effect = exc
    return IosOwnerExecutor(ddb_resource=ddb)


def test_ddb_failure_log_does_not_emit_owner_id(caplog) -> None:
    """The failure path must not log the owner identifier.

    caplog is left UNSCOPED: the leak could be emitted by this module or by
    base.never_raises, and scoping to one logger is how an equivalent canary
    missed a real leak earlier in this package.
    """
    executor = _executor_with_failing_put(RuntimeError("ddb unavailable"))

    with caplog.at_level(logging.DEBUG):
        result = executor.dispatch(
            {"action_id": "ACT-log-1", "owner_id": _PII_CANARY, "action_kind": "k"},
            {},
        )

    assert result.status == ActionStatus.FAILED

    blob = "\n".join(
        [r.getMessage() for r in caplog.records]
        + [str(r.args) for r in caplog.records]
        + [r.exc_text or "" for r in caplog.records]
    )
    assert _PII_CANARY not in blob, (
        "owner_id reached a log record on the DynamoDB-failure path. It is a "
        f"durable customer identifier.\nLog was:\n{blob[:600]}"
    )


def test_ddb_failure_log_still_says_something_useful(caplog) -> None:
    """Removing the identifier must not turn the log into noise.

    Without this, 'fix' the leak by deleting the whole log line and the previous
    test passes while an operator loses the only signal that a write failed. The
    row id is the correlation handle someone already authorised to read the table
    actually needs.
    """
    executor = _executor_with_failing_put(RuntimeError("ddb unavailable"))

    with caplog.at_level(logging.DEBUG):
        executor.dispatch(
            {"action_id": "ACT-log-2", "owner_id": _PII_CANARY, "action_kind": "k"},
            {},
        )

    # Render each record the way a handler would, tolerating records whose args
    # do not match their format string (pytest's own records among them).
    rendered = []
    for r in caplog.records:
        try:
            rendered.append(r.getMessage())
        except (TypeError, ValueError):
            rendered.append(str(r.msg))
    blob = " ".join(rendered)
    assert "put_item failed" in blob, "the write failure was not logged at all"
    assert "ACT-log-2" in blob, (
        "the failure log carries no row id, so an operator cannot correlate it to "
        "the item that failed"
    )


def test_success_log_does_not_emit_owner_id(caplog) -> None:
    """The success path is the highest-frequency site; pin it too."""
    ddb = MagicMock()
    executor = IosOwnerExecutor(ddb_resource=ddb)

    with caplog.at_level(logging.DEBUG):
        executor.dispatch(
            {"action_id": "ACT-log-3", "owner_id": _PII_CANARY, "action_kind": "k"},
            {},
        )

    blob = "\n".join(
        [r.getMessage() for r in caplog.records] + [str(r.args) for r in caplog.records]
    )
    assert _PII_CANARY not in blob, f"owner_id leaked on the success path:\n{blob[:400]}"

    # Anti-vacuity partner: without this, deleting the success log line entirely
    # satisfies the assertion above while removing the operator's only record that
    # a card was written. Review Cycle 6 confirmed that mutation was MISSED.
    rendered = []
    for r in caplog.records:
        try:
            rendered.append(r.getMessage())
        except (TypeError, ValueError):
            rendered.append(str(r.msg))
    joined = " ".join(rendered)
    assert "writing inbox row" in joined, (
        "the success path logs nothing at all, so the no-PII assertion above is "
        "vacuous and an operator has no record that the card was written"
    )
    assert "ACT-log-3" in joined, "the success log carries no row id to correlate on"
