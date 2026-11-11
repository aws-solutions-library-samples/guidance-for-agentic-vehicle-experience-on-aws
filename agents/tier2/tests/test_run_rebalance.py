"""Tests for run_rebalance / _run_rebalance_from_env / lambda_handler routing.

Covers (task 4.4):
* lambda_handler routes component='rebalance' to _run_rebalance_from_env.
* _run_rebalance_from_env builds the boto3 clients and reads the expected env names;
  boto3.client calls are replaced by recorders — no real AWS calls.
* refresh_fn passes refresh_on_noop=True; write_fn passes refresh_on_noop=False
  (assertions bound against the real avx_writer.write_finding signature).
* The run record (keyed agent-fi-rebalancing#rebalance) is written on both the success
  path and the abort (exception from run_rebalance) path, with the correct status.
* emit_metrics is called on both exits.
* The forwarded counters (artifacts_written, duplicates_skipped, model_calls,
  grounding_read_errors, rows_refused) bind correctly against write_run_record's
  parameter names.
"""
from __future__ import annotations

import inspect
from typing import Any
from unittest.mock import MagicMock

import pytest

from agents.tier2 import artifact_writer, runner
from agents.tier2.runner import FI_REBALANCING_AGENT_ID
from agents.tier2.run_records import write_run_record as _real_write_run_record

_REAL_WRR_SIG = inspect.signature(_real_write_run_record)

# ---------------------------------------------------------------------------
# Shared doubles
# ---------------------------------------------------------------------------


class _AvxWriter:
    """Minimal avx_writer double; records every write_finding call."""

    table_name = "vsa-staging-avx-findings"

    def __init__(self) -> None:
        self.dynamodb_client = self
        self.calls: list[dict[str, Any]] = []

    def query(self, **_kw: Any):
        return {"Items": []}

    def write_finding(
        self,
        finding: dict[str, Any],
        *,
        standing: Any = None,
        refresh_on_noop: bool = False,
        update_on_same_hash: bool = False,
    ) -> dict[str, Any]:
        self.calls.append({
            "finding": finding,
            "standing": standing,
            "refresh_on_noop": refresh_on_noop,
            "update_on_same_hash": update_on_same_hash,
        })
        return {"action": "put", "finding_id": finding.get("finding_id", "FIND-test")}


class _Cw:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def put_metric_data(self, **kw: Any) -> None:
        self.calls.append(kw)


def _stub_run_rebalance(monkeypatch, *, raises=None, stats=None):
    """Replace agents.fi_rebalancing.agent.run_rebalance with a stub."""
    from agents.fi_rebalancing import agent as rebal_agent

    default_stats = {
        "move_items_written": 2,
        "gap_items_written": 1,
        "items_unchanged": 3,
        "model_calls": 0,
        "grounding_read_errors": 4,
        "items_refused": 1,
    }

    def fake_run(**kw):
        if raises is not None:
            raise raises
        return stats if stats is not None else default_stats

    monkeypatch.setattr(rebal_agent, "run_rebalance", fake_run)


def _call_run_rebalance(monkeypatch, *, raises=None, stats=None, extra_kw=None):
    """Call runner.run_rebalance with minimal doubles; return (result_or_exc, cw, records)."""
    _stub_run_rebalance(monkeypatch, raises=raises, stats=stats)
    cw = _Cw()
    records: list = []

    kw = dict(
        athena_client=object(),
        workgroup="cvx-staging-analytics",
        results_s3_path="s3://bucket/tier2/fi-rebalancing/",
        ddb_client=object(),
        findings_table="vsa-staging-avx-findings",
        vehicles_table="cms-staging-storage-vehicles",
        groundings_table="cms-staging-storage-vehicle-groundings",
        fleet_locations_table="cms-staging-storage-fleet-locations",
        avx_writer=_AvxWriter(),
        cloudwatch_client=cw,
        run_record_writer=lambda **k: records.append(k) or True,
    )
    if extra_kw:
        kw.update(extra_kw)

    try:
        result = runner.run_rebalance("staging", **kw)
        return result, cw, records
    except Exception as exc:
        return exc, cw, records


# ---------------------------------------------------------------------------
# lambda_handler dispatch
# ---------------------------------------------------------------------------


def test_lambda_handler_dispatches_rebalance(monkeypatch):
    """lambda_handler routes component='rebalance' to _run_rebalance_from_env."""
    seen: dict[str, Any] = {}

    def fake_from_env(stage):
        seen["stage"] = stage
        return {"dispatched": True}

    monkeypatch.setattr(runner, "_run_rebalance_from_env", fake_from_env)
    result = runner.lambda_handler({"stage": "staging", "component": "rebalance"}, object())
    assert result == {"dispatched": True}
    assert seen["stage"] == "staging"


# ---------------------------------------------------------------------------
# _run_rebalance_from_env env-reading and client building
# ---------------------------------------------------------------------------


def test_run_rebalance_from_env_reads_env_and_builds_clients(monkeypatch):
    """_run_rebalance_from_env builds boto3 clients and passes the right env-derived names."""
    built: list[str] = []
    run_kw: dict[str, Any] = {}

    class _FakeBoto3:
        @staticmethod
        def client(service: str, **_kw: Any) -> object:
            built.append(service)
            return MagicMock(name=service)

    class _FakeWriter:
        def __init__(self, *, table_name: str, dynamodb_client: Any) -> None:
            run_kw["writer_table"] = table_name

        def write_finding(self, finding, *, standing=None, refresh_on_noop=False,
                          update_on_same_hash=False):
            return {}

    def fake_run_rebalance(stage, **kw):
        run_kw.update(kw)
        return {"status": "ok"}

    import sys
    monkeypatch.setitem(sys.modules, "boto3", _FakeBoto3)
    monkeypatch.setattr(artifact_writer, "ArtifactWriter", _FakeWriter)
    monkeypatch.setattr(runner, "run_rebalance", fake_run_rebalance)
    monkeypatch.setenv("ATHENA_RESULTS_BUCKET", "my-results-bucket")
    monkeypatch.setenv("AVX_FINDINGS_TABLE", "vsa-staging-avx-findings")
    monkeypatch.setenv("CMS_VEHICLES_TABLE", "cms-staging-storage-vehicles")
    monkeypatch.setenv("CMS_FLEET_LOCATIONS_TABLE", "cms-staging-storage-fleet-locations")
    monkeypatch.setenv("CMS_VEHICLE_GROUNDINGS_TABLE", "cms-staging-storage-vehicle-groundings")
    monkeypatch.setenv("ATHENA_WORKGROUP", "cvx-staging-analytics")

    result = runner._run_rebalance_from_env("staging")
    assert result == {"status": "ok"}

    # Three clients: dynamodb, athena, cloudwatch
    assert "dynamodb" in built
    assert "athena" in built
    assert "cloudwatch" in built

    # Key env names forwarded
    assert run_kw["findings_table"] == "vsa-staging-avx-findings"
    assert run_kw["vehicles_table"] == "cms-staging-storage-vehicles"
    assert run_kw["fleet_locations_table"] == "cms-staging-storage-fleet-locations"
    assert run_kw["groundings_table"] == "cms-staging-storage-vehicle-groundings"
    assert run_kw["workgroup"] == "cvx-staging-analytics"
    assert run_kw["results_s3_path"] == "s3://my-results-bucket/tier2/fi-rebalancing/"


# ---------------------------------------------------------------------------
# write_fn / refresh_fn keyword contract
# ---------------------------------------------------------------------------


def test_write_fn_passes_refresh_on_noop_false_and_standing_none(monkeypatch):
    """write_fn calls avx_writer.write_finding with refresh_on_noop=False, standing=None."""
    from agents.fi_rebalancing import agent as rebal_agent
    from agents.tier2.artifact_writer import ArtifactWriter

    _REAL_WF_SIG = inspect.signature(ArtifactWriter.write_finding)

    captured: list[dict] = []

    class _RecordingWriter(_AvxWriter):
        def write_finding(self, finding, *, standing=None, refresh_on_noop=False,
                          update_on_same_hash=False):
            # Bind against the real signature — missing/wrong kwargs raise TypeError.
            _REAL_WF_SIG.bind(self, finding,
                               standing=standing,
                               refresh_on_noop=refresh_on_noop,
                               update_on_same_hash=update_on_same_hash)
            captured.append({"standing": standing, "refresh_on_noop": refresh_on_noop})
            return {}

    writer = _RecordingWriter()
    recorded_write_fns: list[Any] = []

    def fake_run(*, write_fn, refresh_fn, **kw):
        recorded_write_fns.append(("write", write_fn))
        recorded_write_fns.append(("refresh", refresh_fn))
        # Call each fn exactly once with a dummy finding
        dummy = {"finding_id": "FIND-wf-test"}
        write_fn(dummy)
        refresh_fn(dummy)
        return {"move_items_written": 0, "gap_items_written": 0,
                "items_unchanged": 0, "model_calls": 0,
                "grounding_read_errors": 0, "items_refused": 0}

    monkeypatch.setattr(rebal_agent, "run_rebalance", fake_run)
    cw = _Cw()
    runner.run_rebalance(
        "staging",
        athena_client=object(),
        workgroup="cvx-staging-analytics",
        results_s3_path="s3://bucket/tier2/fi-rebalancing/",
        ddb_client=object(),
        findings_table="vsa-staging-avx-findings",
        vehicles_table="cms-staging-storage-vehicles",
        groundings_table="cms-staging-storage-vehicle-groundings",
        fleet_locations_table="cms-staging-storage-fleet-locations",
        avx_writer=writer,
        cloudwatch_client=cw,
        run_record_writer=lambda **k: True,
    )

    assert len(captured) == 2, "expected two write_finding calls (write_fn + refresh_fn)"
    write_call = captured[0]
    refresh_call = captured[1]

    # write_fn: refresh_on_noop=False
    assert write_call["standing"] is None
    assert write_call["refresh_on_noop"] is False, (
        "write_fn must pass refresh_on_noop=False; "
        f"got {write_call['refresh_on_noop']!r}"
    )

    # refresh_fn: refresh_on_noop=True
    assert refresh_call["standing"] is None
    assert refresh_call["refresh_on_noop"] is True, (
        "refresh_fn must pass refresh_on_noop=True; "
        f"got {refresh_call['refresh_on_noop']!r}"
    )


# ---------------------------------------------------------------------------
# Run record written on success
# ---------------------------------------------------------------------------


def test_run_record_written_on_success(monkeypatch):
    """Successful run writes one run record keyed agent-fi-rebalancing#rebalance."""
    result, _cw, records = _call_run_rebalance(monkeypatch)
    (rec,) = records
    assert rec["agent_id"] == FI_REBALANCING_AGENT_ID
    assert rec["component"] == "rebalance"
    assert rec["status"] == "completed"


# ---------------------------------------------------------------------------
# Run record written on abort (exception from run_rebalance)
# ---------------------------------------------------------------------------


def test_run_record_written_on_exception_with_status_aborted(monkeypatch):
    """An exception from run_rebalance still writes a run record with status aborted."""
    exc, _cw, records = _call_run_rebalance(monkeypatch, raises=RuntimeError("boom"))
    assert isinstance(exc, RuntimeError)
    (rec,) = records
    assert rec["agent_id"] == FI_REBALANCING_AGENT_ID
    assert rec["component"] == "rebalance"
    assert rec["status"] == "aborted"


def test_run_record_and_summary_carry_the_runs_model_tokens(monkeypatch):
    """Prompt 40: the agent's tokens_used and cost_usd reach both the run record and the
    returned summary (it was 0 in the summary and absent from the record while the model ran)."""
    stats = {"move_items_written": 1, "gap_items_written": 0, "items_unchanged": 0,
             "model_calls": 3, "grounding_read_errors": 0, "items_refused": 0,
             "tokens_used": 451, "cost_usd": 0.0123}
    result, _cw, records = _call_run_rebalance(monkeypatch, stats=stats)
    (rec,) = records
    assert "tokens_used" in rec and "cost_usd" in rec, rec
    assert rec["tokens_used"] == 451 and rec["cost_usd"] == pytest.approx(0.0123)
    assert result["tokens_used"] == 451
    assert result["estimated_cost_usd"] == pytest.approx(0.0123)


def test_aborted_run_record_carries_no_token_or_cost_field(monkeypatch):
    """An aborted run's spend is unknown to the runner, so the record says nothing rather
    than claiming 0 tokens."""
    _exc, _cw, records = _call_run_rebalance(monkeypatch, raises=RuntimeError("boom"))
    (rec,) = records
    assert "tokens_used" not in rec and "cost_usd" not in rec, rec


# ---------------------------------------------------------------------------
# emit_metrics on both exits
# ---------------------------------------------------------------------------


def test_emit_metrics_on_success(monkeypatch):
    """Successful run calls emit_metrics (>= 1 put_metric_data call)."""
    _result, cw, _records = _call_run_rebalance(monkeypatch)
    assert len(cw.calls) >= 1, (
        f"emit_metrics must fire on the success path; got {len(cw.calls)} call(s)"
    )


def test_emit_metrics_on_abort(monkeypatch):
    """Abort path also calls emit_metrics (mutation guard: emit_metrics not dropped)."""
    _exc, cw, _records = _call_run_rebalance(monkeypatch, raises=RuntimeError("boom"))
    # emit_metrics puts several datums; heartbeat adds one more.  >= 2 means both fired.
    assert len(cw.calls) >= 2, (
        "emit_metrics must be called on the abort exit too; "
        f"got {len(cw.calls)} put_metric_data call(s)"
    )


# ---------------------------------------------------------------------------
# Forwarded counters bind against write_run_record's parameter names
# ---------------------------------------------------------------------------


def test_forwarded_counters_bind_to_write_run_record(monkeypatch):
    """The extra= dict passed to _emit_run_record names valid write_run_record parameters.

    Binds the forwarded counter names against the real write_run_record signature so a
    name change in either direction (runner or run_records) fails here immediately.
    """
    stats = {
        "move_items_written": 3,
        "gap_items_written": 2,
        "items_unchanged": 7,
        "model_calls": 0,
        "grounding_read_errors": 1,
        "items_refused": 4,
    }
    _result, _cw, records = _call_run_rebalance(monkeypatch, stats=stats)
    (rec,) = records

    # artifacts_written = move_items_written + gap_items_written
    assert rec["artifacts_written"] == 5  # 3 + 2

    # duplicates_skipped = items_unchanged
    assert rec["duplicates_skipped"] == 7

    # component-specific counters from _rebalance_record_fields
    extra_keys = {"model_calls", "grounding_read_errors", "rows_refused",
                  "vins_selected", "open_items_read_errors",
                  "locations_incomplete", "gap_items_without_vehicle",
                  "forecast_days_missing", "candidates_forecast_days_missing"}
    for key in extra_keys:
        assert key in rec, f"write_run_record call missing counter {key!r}"

    assert rec["model_calls"] == 0
    assert rec["grounding_read_errors"] == 1
    assert rec["rows_refused"] == 4  # items_refused forwarded as rows_refused
    assert rec["vins_selected"] == 0  # vehicles_checked; default_stats has no vehicles_checked
    assert rec["open_items_read_errors"] == 0  # open_items_read_errors; 0 in default_stats
    assert rec["locations_incomplete"] == 0
    assert rec["gap_items_without_vehicle"] == 0
    assert rec["forecast_days_missing"] == 0


def test_vins_selected_equals_vehicles_checked(monkeypatch):
    """W11: vins_selected in the run record equals vehicles_checked from the agent stats.

    Fails if vins_selected is hardcoded to 0 instead of forwarded (mutant F3-W11).
    """
    stats = {
        "move_items_written": 1,
        "gap_items_written": 0,
        "items_unchanged": 0,
        "model_calls": 0,
        "grounding_read_errors": 0,
        "items_refused": 0,
        "vehicles_checked": 42,        # the run checked 42 vehicles for eligibility
        "open_items_read_errors": 3,   # forwarded as open_items_read_errors (W-n5)
    }
    _result, _cw, records = _call_run_rebalance(monkeypatch, stats=stats)
    (rec,) = records
    assert rec["vins_selected"] == 42, (
        f"W11: vins_selected must equal vehicles_checked (42); got {rec.get('vins_selected')!r}"
    )
    assert rec["open_items_read_errors"] == 3, (
        f"W-n5: open_items_read_errors must equal open_items_read_errors (3); "
        f"got {rec.get('open_items_read_errors')!r}"
    )
    assert "registry_read_errors" not in rec, (
        "W-n5: rebalance must not write registry_read_errors (that is the service component's "
        "CMS reads counter); got registry_read_errors in run record"
    )

    # Every key in rec (except ddb_client/table_name, which _emit_run_record does not pass)
    # must be a valid write_run_record parameter.
    params = set(_REAL_WRR_SIG.parameters.keys())
    non_param_keys = {"ddb_client", "table_name"}
    for key in rec:
        if key in non_param_keys:
            continue
        assert key in params, (
            f"run record carries key {key!r} that is not a write_run_record parameter; "
            "either update write_run_record or fix the forwarding"
        )



def test_rebalance_counters_forwarded_by_name(monkeypatch):
    """W-n5 / W-b2 (G4-2 F8): each rebalance counter is forwarded under its own name.

    open_items_read_errors, locations_incomplete, gap_items_without_vehicle and
    forecast_days_missing are each asserted by name in the run record.  registry_read_errors
    must NOT appear (that field is the service component's CMS reads counter).
    """
    stats = {
        "move_items_written": 1,
        "gap_items_written": 2,
        "items_unchanged": 0,
        "model_calls": 0,
        "grounding_read_errors": 0,
        "items_refused": 0,
        "open_items_read_errors": 5,
        "locations_incomplete": 3,
        "gap_items_without_vehicle": 2,
        "forecast_days_missing": 1,
        "candidates_forecast_days_missing": 4,
    }
    _result, _cw, records = _call_run_rebalance(monkeypatch, stats=stats)
    (rec,) = records

    assert rec["open_items_read_errors"] == 5, (
        f"W-n5: open_items_read_errors must be 5; got {rec.get('open_items_read_errors')!r}"
    )
    assert rec["locations_incomplete"] == 3, (
        f"W-n5: locations_incomplete must be 3; got {rec.get('locations_incomplete')!r}"
    )
    assert rec["gap_items_without_vehicle"] == 2, (
        f"W-b2: gap_items_without_vehicle must be 2; got {rec.get('gap_items_without_vehicle')!r}"
    )
    assert rec["forecast_days_missing"] == 1, (
        f"W-n5: forecast_days_missing must be 1; got {rec.get('forecast_days_missing')!r}"
    )
    assert rec["candidates_forecast_days_missing"] == 4, (
        "W-c3-1: candidates_forecast_days_missing must be 4; "
        f"got {rec.get('candidates_forecast_days_missing')!r}"
    )
    assert "registry_read_errors" not in rec, (
        "W-n5: rebalance record must not carry registry_read_errors"
    )

    # Deleting the forwarded gap_items_without_vehicle line should be caught (mutant MB-2 / MB-3).
    params = set(_REAL_WRR_SIG.parameters.keys())
    for key in ("open_items_read_errors", "locations_incomplete",
                "gap_items_without_vehicle", "forecast_days_missing",
                "candidates_forecast_days_missing"):
        assert key in params, (
            f"write_run_record is missing parameter {key!r}; add it or fix the forwarding"
        )
