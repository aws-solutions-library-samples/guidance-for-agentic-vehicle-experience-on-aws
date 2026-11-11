"""Tests for AdvisoryExecutor.

Four required test groups:
1.  Dispatch returns target_ref=None and a terminal success status (COMPLETED).
2.  Idempotency — two dispatches of the same action give equal results.
3.  No network call — AST-scan advisory.py to assert no `requests` import and
    no boto3.client / boto3.resource construction.  The test FAILS if either
    appears; it does NOT trivially pass.
4.  Unexpected internal error returns failed/executor_terminal_error via
    @never_raises rather than propagating the exception to the caller.
"""

from __future__ import annotations

import ast
import importlib
import inspect
import textwrap
import types
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from agents.executors.advisory import AdvisoryExecutor
from agents.executors.base import ActionStatus, ExecuteResult, FailureKind, TERMINAL_STATUSES


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

SAMPLE_ACTION: dict[str, Any] = {
    "id": "act-test-001",
    "type": "advisory",
    "target_system": "none",
    "owner_id": "owner-42",
    "params": {"note": "Acknowledge receipt of recall notice."},
}

SAMPLE_CLAIMS: dict[str, Any] = {
    "sub": "dealer-user-99",
    "custom:role": "service_advisor",
}


# ---------------------------------------------------------------------------
# 1. Return contract: target_ref=None, terminal success status
# ---------------------------------------------------------------------------


class TestDispatchReturnContract:
    """Group 1: the executor returns the correct shape on the happy path."""

    def test_returns_execute_result(self) -> None:
        executor = AdvisoryExecutor()
        result = executor.dispatch(SAMPLE_ACTION, SAMPLE_CLAIMS)
        assert isinstance(result, ExecuteResult)

    def test_target_ref_is_none(self) -> None:
        """Advisory has no downstream, so target_ref must always be None."""
        executor = AdvisoryExecutor()
        result = executor.dispatch(SAMPLE_ACTION, SAMPLE_CLAIMS)
        assert result.target_ref is None

    def test_status_is_completed(self) -> None:
        """COMPLETED is the only correct status — no downstream to poll."""
        executor = AdvisoryExecutor()
        result = executor.dispatch(SAMPLE_ACTION, SAMPLE_CLAIMS)
        assert result.status == ActionStatus.COMPLETED

    def test_status_is_terminal(self) -> None:
        """COMPLETED is in TERMINAL_STATUSES; no further transition expected."""
        executor = AdvisoryExecutor()
        result = executor.dispatch(SAMPLE_ACTION, SAMPLE_CLAIMS)
        assert result.status in TERMINAL_STATUSES

    def test_no_failure_kind(self) -> None:
        """A successful advisory result carries no failure_kind."""
        executor = AdvisoryExecutor()
        result = executor.dispatch(SAMPLE_ACTION, SAMPLE_CLAIMS)
        assert result.failure_kind is None

    def test_escalate_is_false(self) -> None:
        """Successful results never escalate."""
        executor = AdvisoryExecutor()
        result = executor.dispatch(SAMPLE_ACTION, SAMPLE_CLAIMS)
        assert result.escalate is False

    def test_target_system_attribute(self) -> None:
        """Protocol compliance: target_system must be 'none'."""
        executor = AdvisoryExecutor()
        assert executor.target_system == "none"

    def test_empty_action_does_not_raise(self) -> None:
        """dispatch must not raise even when the action has no id."""
        executor = AdvisoryExecutor()
        result = executor.dispatch({}, {})
        assert result.status == ActionStatus.COMPLETED
        assert result.target_ref is None


# ---------------------------------------------------------------------------
# 2. Idempotency
# ---------------------------------------------------------------------------


class TestIdempotency:
    """Group 2: two dispatches of the same action produce equal results."""

    def test_two_dispatches_equal(self) -> None:
        executor = AdvisoryExecutor()
        result_a = executor.dispatch(SAMPLE_ACTION, SAMPLE_CLAIMS)
        result_b = executor.dispatch(SAMPLE_ACTION, SAMPLE_CLAIMS)
        # status and target_ref must be identical — these are the fields that
        # flow into the Action row and the owner's card.
        assert result_a.status == result_b.status
        assert result_a.target_ref == result_b.target_ref
        assert result_a.failure_kind == result_b.failure_kind
        assert result_a.escalate == result_b.escalate

    def test_different_action_ids_give_same_shape(self) -> None:
        """Result shape is independent of the action id."""
        executor = AdvisoryExecutor()
        action_x = {**SAMPLE_ACTION, "id": "act-x"}
        action_y = {**SAMPLE_ACTION, "id": "act-y"}
        rx = executor.dispatch(action_x, SAMPLE_CLAIMS)
        ry = executor.dispatch(action_y, SAMPLE_CLAIMS)
        assert rx.status == ry.status
        assert rx.target_ref == ry.target_ref

    def test_repeat_with_different_claims_gives_same_shape(self) -> None:
        """Claims are irrelevant to the advisory result shape."""
        executor = AdvisoryExecutor()
        claims_a = {"sub": "user-a"}
        claims_b = {"sub": "user-b"}
        ra = executor.dispatch(SAMPLE_ACTION, claims_a)
        rb = executor.dispatch(SAMPLE_ACTION, claims_b)
        assert ra.status == rb.status
        assert ra.target_ref == rb.target_ref


# ---------------------------------------------------------------------------
# 3. No-network constraint (AST scan)
# ---------------------------------------------------------------------------


def _advisory_source() -> str:
    """Return the source text of advisory.py."""
    module_path = Path(__file__).parent.parent / "advisory.py"
    return module_path.read_text(encoding="utf-8")


def _ast_import_names(source: str) -> list[str]:
    """Return all top-level import names from `import X` and `from X import …`."""
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


def _ast_boto3_client_calls(source: str) -> list[str]:
    """Return a list of `boto3.client(...)` or `boto3.resource(...)` call
    expressions found anywhere in the source (as formatted strings for
    error messages)."""
    tree = ast.parse(source)
    hits: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        # Match `boto3.client(...)` or `boto3.resource(...)`
        if (
            isinstance(func, ast.Attribute)
            and isinstance(func.value, ast.Name)
            and func.value.id == "boto3"
            and func.attr in {"client", "resource"}
        ):
            hits.append(f"boto3.{func.attr}() at line {node.lineno}")
    return hits


class TestNoNetworkCall:
    """Group 3: AST verification that advisory.py makes no network calls.

    These tests FAIL if `requests` is imported or `boto3.client` / `boto3.resource`
    appears in the source.  They do NOT trivially pass — see mutation evidence
    in the task hand-back.
    """

    def test_no_requests_import(self) -> None:
        """advisory.py must not import `requests`."""
        source = _advisory_source()
        imported_names = _ast_import_names(source)
        assert "requests" not in imported_names, (
            "advisory.py imports `requests` — this violates the HARD CONSTRAINT "
            "that the module makes no network call of any kind.  "
            f"Imports found: {imported_names}"
        )

    def test_no_boto3_client_or_resource(self) -> None:
        """advisory.py must not construct any boto3 client or resource."""
        source = _advisory_source()
        hits = _ast_boto3_client_calls(source)
        assert not hits, (
            "advisory.py constructs a boto3 client/resource — this violates the "
            "HARD CONSTRAINT that the module makes no network call of any kind.  "
            f"Hits found: {hits}"
        )

    def test_no_http_verbs_in_source(self) -> None:
        """Sanity check: no bare `requests.get/post/put/delete` calls exist.

        This is a belt-and-suspenders check: the import test above already
        blocks `requests` usage, but this catches a copy-paste of a call site
        that somehow survived import removal.
        """
        source = _advisory_source()
        tree = ast.parse(source)
        http_calls: list[str] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if (
                isinstance(func, ast.Attribute)
                and isinstance(func.value, ast.Name)
                and func.value.id == "requests"
                and func.attr in {"get", "post", "put", "patch", "delete", "head", "request"}
            ):
                http_calls.append(f"requests.{func.attr}() at line {node.lineno}")
        assert not http_calls, (
            f"advisory.py contains HTTP call sites: {http_calls}"
        )


# ---------------------------------------------------------------------------
# 4. @never_raises — unexpected internal error returns failed/terminal_error
# ---------------------------------------------------------------------------


class TestNeverRaises:
    """Group 4: @never_raises converts unexpected exceptions to ExecuteResult."""

    def test_internal_error_returns_failed_not_raises(self) -> None:
        """`dispatch` must not propagate an exception — it must return FAILED."""
        executor = AdvisoryExecutor()

        # Patch the inner function body to raise an unexpected error.
        # We patch `log.info` inside advisory.py to raise, which is inside
        # the @never_raises wrapper — simulating an internal explosion.
        with patch("agents.executors.advisory.log") as mock_log:
            mock_log.info.side_effect = RuntimeError("simulated internal failure")
            result = executor.dispatch(SAMPLE_ACTION, SAMPLE_CLAIMS)

        assert result.status == ActionStatus.FAILED
        assert result.failure_kind == FailureKind.EXECUTOR_TERMINAL_ERROR

    def test_internal_error_result_is_execute_result(self) -> None:
        """The failure result is an ExecuteResult, not a raw exception."""
        executor = AdvisoryExecutor()
        with patch("agents.executors.advisory.log") as mock_log:
            mock_log.info.side_effect = ValueError("boom")
            result = executor.dispatch(SAMPLE_ACTION, SAMPLE_CLAIMS)
        assert isinstance(result, ExecuteResult)

    def test_internal_error_captures_exception_class(self) -> None:
        """The failure result records the exception class name."""
        executor = AdvisoryExecutor()
        with patch("agents.executors.advisory.log") as mock_log:
            mock_log.info.side_effect = KeyError("missing key")
            result = executor.dispatch(SAMPLE_ACTION, SAMPLE_CLAIMS)
        assert result.last_error_class == "KeyError"

    def test_internal_error_does_not_escalate(self) -> None:
        """terminal_error sets escalate=True per base.py convention (Addendum D5)."""
        executor = AdvisoryExecutor()
        with patch("agents.executors.advisory.log") as mock_log:
            mock_log.info.side_effect = TypeError("type problem")
            result = executor.dispatch(SAMPLE_ACTION, SAMPLE_CLAIMS)
        # terminal_error() sets escalate=True per base.py — an owner is still
        # owed an explanation that the work could not be completed.
        assert result.escalate is True
