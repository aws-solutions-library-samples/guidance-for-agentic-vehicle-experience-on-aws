"""The ``recall`` component's run (spec 2026-10-05-fi-recall-warranty-stage-c task 3.5).

Covers ``agents.tier2.runner.run_recall`` and the lambda_handler dispatch:

* lambda_handler dispatches component='recall' to _run_recall_from_env;
* one run record per run, keyed ``agent-fi-recall-warranty#recall``, on every exit;
* an Athena error records an aborted run and re-raises;
* no Bedrock client is built (rules path only, plan_fn=None);
* env-contract test: every new env var is read through os.environ.get so the
  bidirectional stack/runner contract test passes.

Bedrock and every AWS client are doubles; the repo-root guard blocks real calls.
"""
from __future__ import annotations

import datetime
from typing import Any

import pytest

from agents.tier2 import runner
from agents.tier2.runner import FI_RECALL_WARRANTY_AGENT_ID


# ---------------------------------------------------------------------------
# Minimal doubles
# ---------------------------------------------------------------------------

class _AvxWriter:
    """Minimal avx_writer double: table_name + dynamodb_client for the live lookup."""

    table_name = "vsa-staging-avx-findings"

    def __init__(self) -> None:
        self.dynamodb_client = self
        self.findings: list[dict[str, Any]] = []

    def query(self, **_kw: Any):
        return {"Items": []}

    def write_finding(self, finding: dict[str, Any], *, standing: Any = None) -> dict[str, Any]:
        self.findings.append(finding)
        return {"action": "put", "finding_id": finding["finding_id"]}


def _run_recall(
    records: list,
    *,
    athena_client: Any = None,
    monkeypatch: Any = None,
) -> dict[str, Any]:
    """Call run_recall with minimal doubles; patch selection reads to return empty."""
    if monkeypatch is not None:
        from agents.fi_recall_warranty import selection as rw_sel

        monkeypatch.setattr(rw_sel, "select_unremedied_recalls",
                            lambda *_a, **_kw: ([], 0))
        monkeypatch.setattr(rw_sel, "select_parts_stock",
                            lambda *_a, **_kw: [])

    return runner.run_recall(
        "staging",
        athena_client=athena_client or object(),
        workgroup="cvx-staging-analytics",
        results_s3_path="s3://results/tier2/fi-recall-warranty/",
        ddb_client=object(),
        vehicles_table="cms-staging-storage-vehicles",
        fleet_locations_table="cms-staging-storage-fleet-locations",
        groundings_table="cms-staging-storage-vehicle-groundings",
        actions_table="vsa-staging-avx-actions",
        avx_writer=_AvxWriter(),
        owner_resolver=None,
        run_record_writer=lambda **kw: records.append(kw),
        now=datetime.datetime(2026, 10, 8, 4, 0, tzinfo=datetime.timezone.utc),
    )


# ---------------------------------------------------------------------------
# lambda_handler dispatch
# ---------------------------------------------------------------------------


def test_lambda_handler_dispatches_recall(monkeypatch):
    """lambda_handler routes component='recall' to _run_recall_from_env."""
    seen: dict[str, Any] = {}

    def fake(stage, event, context):
        seen.update(stage=stage, event=event, context=context)
        return {"ok": 1}

    monkeypatch.setattr(runner, "_run_recall_from_env", fake)
    ctx = object()
    result = runner.lambda_handler({"stage": "staging", "component": "recall"}, ctx)
    assert result == {"ok": 1}
    assert seen["stage"] == "staging" and seen["context"] is ctx


# ---------------------------------------------------------------------------
# Run record on success
# ---------------------------------------------------------------------------


def test_a_completed_run_writes_one_record_keyed_agent_fi_recall_warranty_recall(monkeypatch):
    """One run record keyed agent-fi-recall-warranty#recall on a successful (empty) run."""
    records: list = []
    _run_recall(records, monkeypatch=monkeypatch)
    (rec,) = records
    assert rec["agent_id"] == FI_RECALL_WARRANTY_AGENT_ID
    assert rec["component"] == "recall"
    assert rec["status"] == "completed"


# ---------------------------------------------------------------------------
# Run record on error (abort path)
# ---------------------------------------------------------------------------


def test_an_athena_failure_records_an_aborted_run_and_raises(monkeypatch):
    """Athena error → one aborted run record written, exception re-raised."""
    from agents.fi_recall_warranty import selection as rw_sel

    def boom(*_a: Any, **_kw: Any):
        raise RuntimeError("AthenaCursorError")

    monkeypatch.setattr(rw_sel, "select_unremedied_recalls", boom)

    records: list = []
    with pytest.raises(RuntimeError):
        _run_recall(records, monkeypatch=None)

    (rec,) = records
    assert rec["agent_id"] == FI_RECALL_WARRANTY_AGENT_ID
    assert rec["status"] == "aborted"


# ---------------------------------------------------------------------------
# No Bedrock client (rules path)
# ---------------------------------------------------------------------------


def test_no_bedrock_client_is_built_by_run_recall(monkeypatch):
    """With bedrock_client=None (default) run_recall is rules-only: plan_fn stays None.

    Changed from Group 3's "bedrock_client must not exist as a parameter" contract
    to the S4 contract: bedrock_client is now an accepted keyword (default None);
    when None, no model calls are made and the run still completes with a run record.
    """
    import inspect

    sig = inspect.signature(runner.run_recall)
    assert "bedrock_client" in sig.parameters, (
        "run_recall must accept a bedrock_client parameter (S4 wiring)")
    assert sig.parameters["bedrock_client"].default is None, (
        "bedrock_client must default to None (rules-only path)")
    # Confirm the function runs without any Bedrock calls when client is absent.
    records: list = []
    _run_recall(records, monkeypatch=monkeypatch)
    assert records, "run_recall must write a run record"


# ---------------------------------------------------------------------------
# Env-contract: new vars use os.environ.get, not os.environ[...]
# ---------------------------------------------------------------------------


def test_env_contract_new_recall_vars_use_environ_get():
    """CMS_FLEET_LOCATIONS_TABLE, CMS_VEHICLE_GROUNDINGS_TABLE and AVX_ACTIONS_TABLE
    are read through os.environ.get() in runner.py so the bidirectional stack/runner
    contract tests pass (direction-A and direction-B both use the .get() AST scan).

    This test is the mutation guard for mutant (c): changing any read to
    os.environ["VAR"] removes it from the AST scan and causes
    test_env_contract_runner_reads_all_stack_vars to fail (stack sets var, runner
    doesn't "read" it per the parser → dead stack config).
    """
    import ast
    import pathlib

    runner_path = pathlib.Path(runner.__file__)
    source = runner_path.read_text()
    tree = ast.parse(source)

    get_reads: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Attribute) and func.attr == "get"):
            continue
        val = func.value
        if not (isinstance(val, ast.Attribute) and val.attr == "environ"):
            continue
        if node.args and isinstance(node.args[0], ast.Constant):
            get_reads.add(node.args[0].value)

    required = {"CMS_FLEET_LOCATIONS_TABLE", "CMS_VEHICLE_GROUNDINGS_TABLE", "AVX_ACTIONS_TABLE"}
    missing = required - get_reads
    assert not missing, (
        f"These recall env vars are not read via os.environ.get() in runner.py: {missing}.  "
        "Use os.environ.get('VAR', default) so the bidirectional env-contract test can find them."
    )
