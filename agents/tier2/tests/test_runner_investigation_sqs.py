"""W5 fix: runner._run_investigation_sqs uses the writer's returned finding_id.

When ArtifactWriter.write_finding returns an existing row (action='noop') because
the finding_key already exists, it returns that row's finding_id.  The runner
must record THAT id in the investigation row, not the locally-generated UUID.

Probe W4: run runner with a real ArtifactWriter on moto; first attempt fails
_set_completed → row stays queued; second attempt succeeds → row's finding_id
must equal the stored Finding's id, not the new UUID.

Mutation guard (test_runner_records_writers_finding_id):
    _write_finding returns None → run_investigation uses finding['finding_id']
    (locally generated UUID) → investigation row carries a UUID that does not
    match the stored Finding → GET links to a non-existent Finding → FAILS.
"""
from __future__ import annotations

import os
import uuid
from typing import Any
from unittest.mock import MagicMock, patch

import boto3
import pytest


# ---------------------------------------------------------------------------
# Module-level constants matching the fi_quality tests
# ---------------------------------------------------------------------------

_STAGE = "staging"
_TABLE_INV = "vsa-staging-avx-investigations"
_TABLE_FIND = "vsa-staging-avx-findings"
_PATTERN_ID = "pat-001-P0AA6-plant-CGA"
_VIN_1 = "MRDVEH00000000020"

# Minimal fault_patterns row returned by the Athena stub.
_PATTERN_ROW = {
    "pattern_id": _PATTERN_ID,
    "code_family": "P0AA6",
    "cohort_dimension": "assembly_plant",
    "cohort_value": "CGA",
    "candidate_rank": "1",
    "pattern_group_id": _PATTERN_ID,
    "vehicles_affected": "10",
    "vehicles_in_service": "200",
    "rate": "0.05",
    "rate_rest": "0.01",
    "rate_ratio": "5.0",
    "p_value": "0.001",
    "first_detected_month": "2026-07-01",
    "trend": "stable",
    "method": "poisson_bonferroni",
    "affected_vins_sample_csv": _VIN_1,
    "computed_at": "2026-10-05T01:00:00Z",
    "parameters_version": "1.0.0",
}

# Minimal build row.
_BUILD_ROW = {
    "vin": _VIN_1,
    "model": "Azimuth",
    "model_year": "2024",
    "build_month": "2024-03",
    "plant": "CGA",
    "component_id": "hv_battery_module",
    "supplier_id": "SUP-A",
    "supplier_lot": "L-2024-03-CGA",
    "part_number": "HV-001",
}

# Minimal failure mode.
_FM_ROW = {
    "failure_mode_id": "fm-battery",
    "component_id": "hv_battery_module",
    "symptom_codes": ["P0AA6"],
    "remedy_options": [
        {
            "remedy_option_id": "opt-001",
            "remedy_type": "hardware_recall",
            "recommended": True,
            "tradeoffs": "TRADEOFFS_TEXT",
        }
    ],
}


def _make_athena_stub(rows_per_query: list[list[dict]]) -> MagicMock:
    """Return an Athena MagicMock that serves rows in order per query."""
    stub = MagicMock()
    stub.start_query_execution.return_value = {"QueryExecutionId": "exec-001"}
    stub.get_query_execution.return_value = {
        "QueryExecution": {"Status": {"State": "SUCCEEDED"}}
    }
    call_idx = [0]

    def _get_results(**kwargs: Any) -> dict:
        idx = call_idx[0]
        rows = rows_per_query[idx % len(rows_per_query)]
        call_idx[0] += 1
        if not rows:
            return {"ResultSet": {"Rows": []}}
        header = {"Data": [{"VarCharValue": k} for k in rows[0].keys()]}
        data = [{"Data": [{"VarCharValue": str(v) if v is not None else ""}
                          for v in r.values()]} for r in rows]
        return {"ResultSet": {"Rows": [header] + data}}

    stub.get_query_results.side_effect = _get_results
    return stub


def _default_athena_stub() -> MagicMock:
    """Athena stub that returns pattern row, build row, empty controls,
    empty co-occurring codes, failure mode, no OTA, no recall, no service history."""
    return _make_athena_stub([
        [_PATTERN_ROW],      # read_pattern_group
        [_BUILD_ROW],        # read_affected_vins
        [],                  # read_controls
        [],                  # read_co_occurring_codes
        [],                  # read_failure_modes (empty → unknown path)
        [],                  # read_campaigns (OTA)
        [],                  # read_campaigns (recall)
        [],                  # read_service_history
    ])


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestRunnerWriteFindingReturnValue:
    """W5 fix (cycle 3 b): ``runner._run_investigation_sqs``'s ``_write_finding`` returns the
    writer's result, so ``run_investigation`` records the writer's authoritative ``finding_id``.

    Drives the REAL ``_run_investigation_sqs`` closure: ``boto3.client`` returns recorders,
    ``ArtifactWriter`` is a recorder whose ``write_finding`` returns a stored id, and
    ``run_investigation`` is replaced by a fake that calls the closure it is handed and keeps
    what the closure returns.  (An earlier version re-implemented the closure inside the test,
    imported ``ArtifactWriter`` from the wrong module, and was skipped unless ``CI`` was set,
    so it never ran and never touched the runner.)

    Mutation guard: the closure returns ``None`` -> ``seen == [None]`` -> fails here.
    """

    def test_closure_returns_writer_result_to_the_job(self, monkeypatch):
        from agents.tier2 import runner

        stored = {"action": "noop", "finding_id": "FIND-stored-existing-001"}
        seen: list = []

        class _Writer:
            def __init__(self, *a, **k):
                pass

            def write_finding(self, finding, standing=None, **kw):
                return stored

        def _fake_run_investigation(investigation_id, *, write_finding, **kw):
            seen.append(write_finding({"finding_id": "FIND-local-uuid"}))

        monkeypatch.setenv("ATHENA_RESULTS_BUCKET", "bucket-x")
        monkeypatch.setattr("agents.tier2.artifact_writer.ArtifactWriter", _Writer)
        monkeypatch.setattr("agents.fi_quality.job.run_investigation", _fake_run_investigation)
        monkeypatch.setattr(boto3, "client", lambda *a, **k: MagicMock())
        monkeypatch.setattr(runner, "_emit_run_record", lambda *a, **k: None)

        inv_id = str(uuid.uuid4())
        result = runner._run_investigation_sqs(
            [{"messageId": "m-1", "body": inv_id,
              "attributes": {"ApproximateReceiveCount": "1"}}],
            None,
        )

        assert result == {"batchItemFailures": []}
        assert seen == [stored], (
            "The runner's _write_finding must hand the writer's result back to the job "
            f"(got {seen!r}); returning None makes the job record a locally generated id."
        )
