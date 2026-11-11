"""Tests for scripts/live-verify-fi-action-queue.py: the evaluator run-record poll (item 7a, 8b).

Review cycle 6 W1 / Fix Group 6 M6.1 (spec 2026-10-26-fi-sweep-avx-batch, issue
2026-10-27-fi-sweep-avx-ms-run-start-readers). Since T1.5 the auto-approver's run records carry
millisecond ``started_at`` (``lambdas/avx-auto-approver/handler.py`` ``_now_iso``), and item 7a
takes its bound as ``_iso(_now())``, a whole second with a ``Z``. DynamoDB compares strings and
``.`` sorts below ``Z``, so an evaluator run that starts in the bound's own second must still be
found. Run against moto, which applies the key condition; a MagicMock table would not.
"""
from __future__ import annotations

import ast
import importlib.util
from pathlib import Path
from typing import Any

import pytest

_SCRIPT = Path(__file__).resolve().parents[1] / "live-verify-fi-action-queue.py"
_spec = importlib.util.spec_from_file_location("live_verify_fi_action_queue", _SCRIPT)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

_PK = "agent-fi-evaluator#evaluator"
_BOUND = "2026-10-27T11:36:01Z"  # what _iso(_now()) gives just before the Finding is written


@pytest.fixture()
def runs_table(monkeypatch: pytest.MonkeyPatch) -> Any:
    from moto import mock_aws  # noqa: PLC0415
    import boto3  # noqa: PLC0415

    for k in ("AWS_PROFILE", "AWS_SESSION_TOKEN", "AWS_ENDPOINT_URL"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-west-2")
    with mock_aws():
        ddb = boto3.resource("dynamodb", region_name="us-west-2")
        yield ddb.create_table(
            TableName="vsa-staging-avx-agent-runs",
            KeySchema=[{"AttributeName": "agent_component", "KeyType": "HASH"},
                       {"AttributeName": "started_at", "KeyType": "RANGE"}],
            AttributeDefinitions=[{"AttributeName": "agent_component", "AttributeType": "S"},
                                  {"AttributeName": "started_at", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )


def _put(table: Any, started_at: str) -> None:
    table.put_item(Item={"agent_component": _PK, "started_at": started_at, "status": "completed"})


def test_an_evaluator_run_in_the_bounds_second_is_found(runs_table: Any) -> None:
    _put(runs_table, "2026-10-27T11:36:01.500Z")
    got = [i["started_at"] for i in _mod._evaluator_runs_since(runs_table, _BOUND)]
    assert got == ["2026-10-27T11:36:01.500Z"]


def test_an_evaluator_run_from_the_second_before_is_not(runs_table: Any) -> None:
    _put(runs_table, "2026-10-27T11:36:00.999Z")
    _put(runs_table, "2026-10-27T11:36:00Z")
    assert _mod._evaluator_runs_since(runs_table, _BOUND) == []


def test_main_reads_evaluator_runs_only_through_the_helper() -> None:
    """main() needs Cognito, the API and five tables, so items 7a and 8b can't run here. Pin the
    wiring instead: every ``Key("started_at").gte(...)`` in the script takes its bound through
    ``_started_at_lower_bound``, and main's ``evaluator_ran_after`` calls ``_evaluator_runs_since``."""
    tree = ast.parse(_SCRIPT.read_text(encoding="utf-8"))
    gte_args = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "gte" and isinstance(node.func.value, ast.Call)
                and getattr(node.func.value.func, "id", None) == "Key"
                and [getattr(a, "value", None) for a in node.func.value.args] == ["started_at"]):
            gte_args.append(node.args[0])
    assert len(gte_args) >= 2, "anti-vacuity: the helper's query and item 8a's diagnostics query"
    for arg in gte_args:
        assert isinstance(arg, ast.Call) and getattr(arg.func, "id", None) == "_started_at_lower_bound", \
            ast.unparse(arg)

    main = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "main")
    inner = next(n for n in ast.walk(main)
                 if isinstance(n, ast.FunctionDef) and n.name == "evaluator_ran_after")
    calls = [getattr(n.func, "id", None) for n in ast.walk(inner) if isinstance(n, ast.Call)]
    assert "_evaluator_runs_since" in calls, ast.unparse(inner)
