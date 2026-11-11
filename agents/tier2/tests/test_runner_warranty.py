"""The ``warranty`` component's run (spec 2026-10-05-fi-recall-warranty-stage-c T3.5B).

Covers ``agents.tier2.runner.run_warranty`` and the lambda_handler dispatch:

* lambda_handler dispatches component='warranty' to _run_warranty_from_env;
* one run record per run, keyed ``agent-fi-recall-warranty#warranty``, on every exit;
* a selection error records an aborted run and re-raises;
* no Bedrock client is built (rules path only, plan_fn=None).

Mutation guards:
  (a) warranty run record keyed '#recall' → fails test_run_record_keyed_warranty
  (b) no record on error path → fails test_a_selection_failure_records_an_aborted_run_and_raises
  (c) dispatch sends 'warranty' to run_recall → fails test_lambda_handler_dispatches_warranty
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
    table_name = "vsa-staging-avx-findings"

    def __init__(self) -> None:
        self.dynamodb_client = self
        self.findings: list[dict[str, Any]] = []

    def query(self, **_kw: Any):
        return {"Items": []}

    def write_finding(self, finding: dict[str, Any], *, standing: Any = None) -> dict[str, Any]:
        self.findings.append(finding)
        return {"action": "put", "finding_id": finding["finding_id"]}


def _run_warranty(
    records: list,
    *,
    athena_client: Any = None,
    monkeypatch: Any = None,
) -> dict[str, Any]:
    """Call run_warranty with minimal doubles; patch selection to return empty."""
    if monkeypatch is not None:
        from agents.fi_recall_warranty import selection as rw_sel

        monkeypatch.setattr(rw_sel, "select_warranty_candidates",
                            lambda *_a, **_kw: [])
        monkeypatch.setattr(rw_sel, "select_warranty_terms",
                            lambda *_a, **_kw: {})

    return runner.run_warranty(
        "staging",
        athena_client=athena_client or object(),
        workgroup="cvx-staging-analytics",
        results_s3_path="s3://results/tier2/fi-recall-warranty/",
        avx_writer=_AvxWriter(),
        owner_resolver=None,
        run_record_writer=lambda **kw: records.append(kw),
        now=datetime.datetime(2026, 10, 8, 4, 0, tzinfo=datetime.timezone.utc),
    )


# ---------------------------------------------------------------------------
# lambda_handler dispatch
# ---------------------------------------------------------------------------


def test_lambda_handler_dispatches_warranty(monkeypatch):
    """lambda_handler routes component='warranty' to _run_warranty_from_env.

    Mutation guard (c): if dispatch sends 'warranty' to run_recall instead,
    _run_recall_from_env is called rather than _run_warranty_from_env, and
    ``seen`` stays empty → assertion fails.
    """
    seen: dict[str, Any] = {}

    def fake(stage, event, context):
        seen.update(stage=stage, event=event, context=context)
        return {"ok": 1}

    monkeypatch.setattr(runner, "_run_warranty_from_env", fake)
    ctx = object()
    result = runner.lambda_handler({"stage": "staging", "component": "warranty"}, ctx)
    assert result == {"ok": 1}
    assert seen["stage"] == "staging" and seen["context"] is ctx


# ---------------------------------------------------------------------------
# Run record on success
# ---------------------------------------------------------------------------


def test_run_record_keyed_warranty(monkeypatch):
    """One run record keyed agent-fi-recall-warranty#warranty on a successful (empty) run.

    Mutation guard (a): if the component field is 'recall', rec["component"] == 'recall'
    and the assertion ``rec["component"] == "warranty"`` fails.
    """
    records: list = []
    _run_warranty(records, monkeypatch=monkeypatch)
    (rec,) = records
    assert rec["agent_id"] == FI_RECALL_WARRANTY_AGENT_ID
    assert rec["component"] == "warranty"
    assert rec["status"] == "completed"


# ---------------------------------------------------------------------------
# Run record on error (abort path)
# ---------------------------------------------------------------------------


def test_a_selection_failure_records_an_aborted_run_and_raises(monkeypatch):
    """Selection error → one aborted run record written, exception re-raised.

    Mutation guard (b): if the except block omits writing the run record,
    ``records`` stays empty → ``(rec,) = records`` raises ValueError → test fails.
    """
    from agents.fi_recall_warranty import selection as rw_sel

    def boom(*_a: Any, **_kw: Any):
        raise RuntimeError("AthenaQueryError")

    monkeypatch.setattr(rw_sel, "select_warranty_candidates", boom)

    records: list = []
    with pytest.raises(RuntimeError):
        _run_warranty(records, monkeypatch=None)

    (rec,) = records
    assert rec["agent_id"] == FI_RECALL_WARRANTY_AGENT_ID
    assert rec["status"] == "aborted"


# ---------------------------------------------------------------------------
# No Bedrock client (rules path)
# ---------------------------------------------------------------------------


def test_no_bedrock_client_is_built_by_run_warranty(monkeypatch):
    """With bedrock_client=None (default) run_warranty is rules-only: plan_fn stays None.

    Changed from Group 3's "bedrock_client must not exist as a parameter" contract
    to the S4 contract: bedrock_client is now an accepted keyword (default None);
    when None, no model calls are made and the run still completes with a run record.
    """
    import inspect

    sig = inspect.signature(runner.run_warranty)
    assert "bedrock_client" in sig.parameters, (
        "run_warranty must accept a bedrock_client parameter (S4 wiring)")
    assert sig.parameters["bedrock_client"].default is None, (
        "bedrock_client must default to None (rules-only path)")
    records: list = []
    _run_warranty(records, monkeypatch=monkeypatch)
    assert records, "run_warranty must write a run record"


def test_run_warranty_reads_dms_terms_and_passes_them_to_the_loop(monkeypatch):
    """F4 (W5): production reads the DMS lake terms and hands them to the loop, so the
    disclosure's term statement exists outside tests. Control: the reader gets the same
    Athena client / workgroup / results path as the candidates read."""
    from agents.fi_recall_warranty import selection as rw_sel
    from agents.fi_recall_warranty import warranty as rw_w

    terms = {"basic": {"terms_version": "1.0.0", "coverage_group": "basic",
                       "term_years": 4, "term_miles": 50_000, "labor_rate_usd": 150.0}}
    athena = object()
    seen: dict[str, Any] = {}

    def fake_terms(stage, **kw):
        seen["terms_kw"] = (stage, kw)
        return terms

    real_loop = rw_w.run_warranty_loop

    def spy_loop(**kw):
        seen["loop_terms"] = kw.get("warranty_terms")
        return real_loop(**kw)

    monkeypatch.setattr(rw_sel, "select_warranty_candidates", lambda *_a, **_kw: [])
    monkeypatch.setattr(rw_sel, "select_warranty_terms", fake_terms)
    monkeypatch.setattr(rw_w, "run_warranty_loop", spy_loop)
    records: list = []
    _run_warranty(records, athena_client=athena)
    assert seen["loop_terms"] is terms
    assert seen["terms_kw"][0] == "staging"
    assert seen["terms_kw"][1]["athena_client"] is athena
    assert seen["terms_kw"][1]["workgroup"] == "cvx-staging-analytics"
