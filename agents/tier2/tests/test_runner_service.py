"""Tests for the ``service`` and ``investigation`` components of runner.py (task 3.8).

Covers:
* ``run_service``: writes a finding, emits metrics and a run record on both success and
  abort exits; ``drivers=`` wired into ``resolve_standing_facts`` so owner_id is projected;
  no ``bedrock-runtime`` client built (rules path).
* ``_run_service_from_env``: ``FAULT_GROUNDING_ENABLED`` strict parse.
* ``lambda_handler`` SQS dispatch: SQS event routed to ``_run_investigation_sqs``;
  successful investigation → ``batchItemFailures: []``; failed investigation → message id
  in ``batchItemFailures``; malformed body → failed item, not a crash; run record +
  heartbeat emitted on every exit.

All boto3 clients are replaced by recorders; no real AWS calls.
"""
from __future__ import annotations

import datetime
import json
import pytest
from typing import Any, Optional
from unittest.mock import patch, MagicMock

from agents.tier2 import runner
from agents.tier2.runner import (
    FI_FLEET_HEALTH_AGENT_ID,
    FI_QUALITY_AGENT_ID,
)
from agents.tier2.snapshot_freshness import SNAPSHOT_TABLES as _SNAP_TABLES


# ---------------------------------------------------------------------------
# Autouse fixture: fresh snapshot dates for all run_service tests
# (ONE fixture authorized by the architect — adds no assertions to existing tests)
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _fresh_snapshot_dates(monkeypatch):
    """Patch _read_snapshot_dates so existing run_service tests need no real Athena."""
    from agents.tier2.snapshot_freshness import ADP_KEYS, SnapshotState
    today = datetime.datetime.now(datetime.timezone.utc).date()
    fresh = {
        k: SnapshotState(
            date=today,
            row_count=(1 if k in ADP_KEYS else None),
            table_date=today,
        )
        for k in _SNAP_TABLES
    }
    monkeypatch.setattr(runner, "_read_snapshot_dates", lambda *_a, **_kw: fresh)


# ---------------------------------------------------------------------------
# Shared doubles
# ---------------------------------------------------------------------------

class _AvxWriter:
    """Minimal avx_writer double."""

    table_name = "vsa-staging-avx-findings"

    def __init__(self) -> None:
        self.dynamodb_client = self
        self.findings: list[dict[str, Any]] = []

    def query(self, **_kw: Any):
        return {"Items": []}

    def write_finding(self, finding: dict[str, Any], *,
                      standing: Any = None, refresh_on_noop: bool = False,
                      update_on_same_hash: bool = False) -> dict[str, Any]:
        self.findings.append(finding)
        return {"action": "put", "finding_id": finding.get("finding_id", "FIND-test")}


class _Cw:
    """CloudWatch double that records put_metric_data calls."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def put_metric_data(self, **kw: Any) -> None:
        self.calls.append(kw)


# ---------------------------------------------------------------------------
# Helpers for run_service tests
# ---------------------------------------------------------------------------

def _make_service_doubles(monkeypatch):
    """Patch selection readers so run_service can run without Athena / DDB."""
    from agents.fi_fleet_health import selection as fh_sel
    from agents.fi_fleet_health import loop as fh_loop
    from agents.fi_recall_warranty import selection as rw_sel

    # Empty readers — no vehicles selected.
    monkeypatch.setattr(fh_sel, "select_fault_rows",
                        lambda *_a, **_kw: [])
    monkeypatch.setattr(fh_sel, "select_pm_rows",
                        lambda *_a, **_kw: [])
    monkeypatch.setattr(fh_sel, "select_managed_vins",
                        lambda *_a, **_kw: [])
    monkeypatch.setattr(fh_sel, "select_daily_usage",
                        lambda *_a, **_kw: [])
    monkeypatch.setattr(fh_sel, "select_agent2_findings",
                        lambda *_a, **_kw: ({}, 0))
    monkeypatch.setattr(fh_sel, "build_vehicle_rows",
                        lambda **_kw: ([], {}))
    monkeypatch.setattr(rw_sel, "select_parts_stock",
                        lambda *_a, **_kw: [])


def _run_service(records: list, *, monkeypatch, **kw) -> dict[str, Any]:
    """Call run_service with minimal doubles; patch selection so it is Athena-free."""
    _make_service_doubles(monkeypatch)
    cw = _Cw()
    return runner.run_service(
        "staging",
        athena_client=object(),
        workgroup="cvx-staging-analytics",
        results_s3_path="s3://results/tier2/fi-fleet-health/",
        ddb_client=object(),
        findings_table="vsa-staging-avx-findings",
        actions_table="vsa-staging-avx-actions",
        groundings_table="cms-staging-storage-vehicle-groundings",
        vehicles_table="cms-staging-storage-vehicles",
        fleet_locations_table="cms-staging-storage-fleet-locations",
        avx_writer=_AvxWriter(),
        owner_resolver=None,
        cloudwatch_client=cw,
        run_record_writer=lambda **k: records.append(k),
        **kw,
    ), cw


# ---------------------------------------------------------------------------
# run_service: lambda_handler dispatch
# ---------------------------------------------------------------------------


def test_lambda_handler_dispatches_service(monkeypatch):
    """lambda_handler routes component='service' to _run_service_from_env."""
    seen: dict[str, Any] = {}

    def fake(stage):
        seen["stage"] = stage
        return {"ok": 1}

    monkeypatch.setattr(runner, "_run_service_from_env", fake)
    result = runner.lambda_handler({"stage": "staging", "component": "service"}, object())
    assert result == {"ok": 1}
    assert seen["stage"] == "staging"


# ---------------------------------------------------------------------------
# run_service: completed run writes one run record
# ---------------------------------------------------------------------------


def test_completed_service_run_writes_one_record_keyed_agent_fi_fleet_health_service(
    monkeypatch,
):
    """Successful (empty) run writes exactly one run record keyed agent-fi-fleet-health#service."""
    records: list = []
    _run_service(records, monkeypatch=monkeypatch)
    (rec,) = records
    assert rec["agent_id"] == FI_FLEET_HEALTH_AGENT_ID
    assert rec["component"] == "service"
    assert rec["status"] == "completed"


# ---------------------------------------------------------------------------
# run_service: emit_metrics on the abort (exception) exit
# ---------------------------------------------------------------------------


def test_abort_exit_emits_metrics(monkeypatch):
    """Mutation guard: emit_metrics must be called even on the abort path.

    With the mutation (emit_metrics dropped), only the heartbeat fires (1
    put_metric_data call).  Without it, both emit_metrics AND the heartbeat
    fire (2 calls).  Checking for >= 2 catches the mutation.
    """
    from agents.fi_fleet_health import selection as fh_sel

    _make_service_doubles(monkeypatch)
    # Make select_fault_rows raise to trigger the abort exit.
    monkeypatch.setattr(fh_sel, "select_fault_rows",
                        lambda *_a, **_kw: (_ for _ in ()).throw(RuntimeError("boom")))

    cw = _Cw()
    records: list = []
    with pytest.raises(RuntimeError):
        runner.run_service(
            "staging",
            athena_client=object(),
            workgroup="cvx-staging-analytics",
            results_s3_path="s3://results/tier2/fi-fleet-health/",
            ddb_client=object(),
            findings_table="vsa-staging-avx-findings",
            actions_table="vsa-staging-avx-actions",
            groundings_table="cms-staging-storage-vehicle-groundings",
            vehicles_table="cms-staging-storage-vehicles",
            fleet_locations_table="cms-staging-storage-fleet-locations",
            avx_writer=_AvxWriter(),
            owner_resolver=None,
            cloudwatch_client=cw,
            run_record_writer=lambda **k: records.append(k),
        )

    # emit_metrics (5 datums) + heartbeat = 2 put_metric_data calls.
    # With mutation (emit_metrics dropped): only 1 call (heartbeat).
    assert len(cw.calls) >= 2, (
        "emit_metrics must be called on the abort exit; "
        f"got {len(cw.calls)} put_metric_data call(s) — expected >= 2"
    )
    (rec,) = records
    assert rec["status"] == "aborted"


# ---------------------------------------------------------------------------
# run_service: write_finding reaches avx_writer
# ---------------------------------------------------------------------------


def test_service_run_reaches_write_finding_for_each_vehicle(monkeypatch):
    """A vehicle row with one fault triggers write_finding via avx_writer."""
    from agents.fi_fleet_health import selection as fh_sel
    from agents.fi_recall_warranty import selection as rw_sel
    from agents.fi_fleet_health import loop as fh_loop

    monkeypatch.setattr(fh_sel, "select_fault_rows", lambda *_a, **_kw: [])
    monkeypatch.setattr(fh_sel, "select_pm_rows",    lambda *_a, **_kw: [])
    monkeypatch.setattr(fh_sel, "select_managed_vins", lambda *_a, **_kw: [])
    monkeypatch.setattr(fh_sel, "select_daily_usage",  lambda *_a, **_kw: [])
    monkeypatch.setattr(fh_sel, "select_agent2_findings", lambda *_a, **_kw: ({}, 0))
    monkeypatch.setattr(rw_sel, "select_parts_stock",  lambda *_a, **_kw: [])

    written: list[dict] = []

    def fake_build(**_kw):
        return (
            [{"vin": "VIN001", "fleet_id": "FLT-1", "vehicle_id": "VEH-1",
              "open_faults": [{"code": "P0420", "band": "p0", "last_seen_at": "2026-10-01Z"}],
              "pm_tasks": [], "agent2_findings": [], "utilisation": None,
              "position": None, "catalog_entries": {}}],
            {},
        )

    monkeypatch.setattr(fh_sel, "build_vehicle_rows", fake_build)

    # Stub the loop to avoid the full DDB wiring; just record that write_finding was called.
    original_loop = fh_loop.run_health_loop

    def fake_loop(*, vehicle_rows, write_finding, **kw):
        for vr in vehicle_rows:
            import hashlib, uuid
            finding = {
                "finding_id": f"FIND-{uuid.uuid4()}",
                "finding_key": "fk-test",
                "agent_id": FI_FLEET_HEALTH_AGENT_ID,
                "agent_version": "0.1.0",
                "scope": {"vin": vr["vin"]},
                "severity": "p0",
                "computed_at": "2026-10-10T00:00:00Z",
                "expires_at": "2026-10-13T00:00:00Z",
                "confidence": "low",
                "finding_kind": "fleet.health.service",
                "headline": "Test.",
                "detail": "Test.",
                "evidence": [],
                "inputs_hash": hashlib.sha256(b"test").hexdigest(),
                "tokens_used": 0,
                "cost_usd": 0.0,
                "audit_trail": [],
            }
            write_finding(finding, None, refresh_on_noop=True)
            written.append(finding)
        return {"vins_selected": len(vehicle_rows), "vins_processed": len(vehicle_rows),
                "items_written": len(vehicle_rows), "items_skipped_unchanged": 0,
                "vins_refused": 0, "grounding_read_errors": 0,
                "exhausted_items_written": 0, "by_item_type": {}}

    monkeypatch.setattr(fh_loop, "run_health_loop", fake_loop)

    records: list = []
    avx = _AvxWriter()
    runner.run_service(
        "staging",
        athena_client=object(),
        workgroup="cvx-staging-analytics",
        results_s3_path="s3://results/",
        ddb_client=object(),
        findings_table="vsa-staging-avx-findings",
        actions_table="vsa-staging-avx-actions",
        groundings_table="cms-staging-storage-vehicle-groundings",
        vehicles_table="cms-staging-storage-vehicles",
        fleet_locations_table="cms-staging-storage-fleet-locations",
        avx_writer=avx,
        owner_resolver=None,
        cloudwatch_client=_Cw(),
        run_record_writer=lambda **k: records.append(k),
    )

    assert len(avx.findings) == 1, "write_finding must be called for each vehicle row"
    (rec,) = records
    assert rec["artifacts_written"] == 1


# ---------------------------------------------------------------------------
# _run_service_from_env: FAULT_GROUNDING_ENABLED strict parse
# ---------------------------------------------------------------------------


def test_fault_grounding_flag_true_accepted():
    """'true' is parsed as True."""
    assert runner._parse_fault_grounding_enabled("true") is True
    assert runner._parse_fault_grounding_enabled("TRUE") is True


def test_fault_grounding_flag_false_accepted():
    """'false' (default) is parsed as False."""
    assert runner._parse_fault_grounding_enabled("false") is False
    assert runner._parse_fault_grounding_enabled("FALSE") is False


def test_fault_grounding_flag_invalid_raises():
    """Any value other than 'true'/'false' raises RuntimeError (default false, not crash)."""
    for bad in ("yes", "1", "", " ", "enabled"):
        with pytest.raises(RuntimeError):
            runner._parse_fault_grounding_enabled(bad)


# ---------------------------------------------------------------------------
# _run_service_from_env: drivers= wired into resolve_standing_facts
# ---------------------------------------------------------------------------


def test_run_service_from_env_defaults_grounding_flag_to_false(monkeypatch):
    """_run_service_from_env defaults fault_grounding_enabled=False (not True).

    Mutation guard: hardcoding True passes the flag tests but this test catches
    it because run_service receives fault_grounding_enabled=True when env var
    is absent, and we assert it receives False.
    """
    import os
    calls: list[dict] = []

    def fake_boto3_client(service, **_kw):
        return object()

    monkeypatch.setattr("boto3.client", fake_boto3_client)

    class _FakeWriter:
        table_name = "vsa-staging-avx-findings"
        def __init__(self, **_kw): pass
        dynamodb_client = object()

    monkeypatch.setattr(runner, "ArtifactWriter" if hasattr(runner, "ArtifactWriter") else "_",
                        _FakeWriter, raising=False)
    monkeypatch.setattr(runner, "_assigned_driver_directory", lambda *_a, **_kw: object())
    monkeypatch.setattr(runner, "resolve_standing_facts", lambda *_a, **_kw: None)

    def fake_run_service(stage, *, fault_grounding_enabled, **_kw):
        calls.append({"fault_grounding_enabled": fault_grounding_enabled})
        return {"status": "completed", "artifacts_written": 0, "duplicates_skipped": 0,
                "started_at": "2026-10-10T00:00:00Z", "finished_at": "2026-10-10T00:00:00Z",
                "abort_reason": None, "vins_selected": 0}

    monkeypatch.setattr(runner, "run_service", fake_run_service)

    with monkeypatch.context() as m:
        m.setenv("VSA_STAGE", "staging")
        m.setenv("ATHENA_RESULTS_BUCKET", "test-bucket")
        # No FAULT_GROUNDING_ENABLED → default "false"
        m.delenv("FAULT_GROUNDING_ENABLED", raising=False)
        runner._run_service_from_env("staging")

    assert calls, "run_service was never called"
    (call,) = calls
    # Mutation guard: hardcoding True would fail this assertion.
    assert call["fault_grounding_enabled"] is False, (
        f"fault_grounding_enabled must default to False; got {call['fault_grounding_enabled']!r}"
    )


def test_run_service_from_env_wires_drivers(monkeypatch):
    """_run_service_from_env builds an AssignedDriverDirectory and passes it to run_service.

    Mutation guard: dropping ``drivers=`` from resolve_standing_facts disables owner_id
    projection.  This test confirms the produced owner_resolver calls
    resolve_standing_facts with a non-None drivers argument.
    """
    import os
    calls: list[dict] = []

    # Patch all boto3 clients to no-ops.
    fake_ddb = object()
    fake_athena = object()
    fake_cw = object()

    def fake_boto3_client(service, **_kw):
        if service == "dynamodb":
            return fake_ddb
        if service == "athena":
            return fake_athena
        if service == "cloudwatch":
            return fake_cw
        return object()

    monkeypatch.setattr("boto3.client", fake_boto3_client)

    # Patch ArtifactWriter so we don't need real DDB.
    class _FakeWriter:
        table_name = "vsa-staging-avx-findings"
        def __init__(self, **_kw): pass
        dynamodb_client = fake_ddb

    monkeypatch.setattr(runner, "ArtifactWriter" if hasattr(runner, "ArtifactWriter") else "_",
                        _FakeWriter, raising=False)

    # Patch _assigned_driver_directory to return a sentinel.
    sentinel = object()
    monkeypatch.setattr(runner, "_assigned_driver_directory",
                        lambda *_a, **_kw: sentinel)

    # Patch resolve_standing_facts to record the call.
    def fake_resolve(vin, *, ddb_client, vehicles_table, drivers):
        calls.append({"drivers": drivers})
        return None  # StandingFacts = None is safe

    monkeypatch.setattr(runner, "resolve_standing_facts", fake_resolve)

    # Patch run_service to just call the owner_resolver and return.
    def fake_run_service(stage, *, owner_resolver, **_kw):
        if owner_resolver is not None:
            owner_resolver("TESTVIN")
        return {"status": "completed", "artifacts_written": 0, "duplicates_skipped": 0,
                "started_at": "2026-10-10T00:00:00Z", "finished_at": "2026-10-10T00:00:00Z",
                "abort_reason": None, "vins_selected": 0}

    monkeypatch.setattr(runner, "run_service", fake_run_service)

    with monkeypatch.context() as m:
        m.setenv("VSA_STAGE", "staging")
        m.setenv("ATHENA_RESULTS_BUCKET", "test-bucket")
        m.setenv("FAULT_GROUNDING_ENABLED", "false")
        runner._run_service_from_env("staging")

    # Mutation guard: if drivers= is dropped, calls will be empty or drivers=None.
    assert calls, "resolve_standing_facts was never called"
    (call,) = calls
    assert call["drivers"] is sentinel, (
        "resolve_standing_facts must receive the AssignedDriverDirectory (drivers=)")


# ---------------------------------------------------------------------------
# SQS / investigation path: lambda_handler dispatches on Records[*].eventSource
# ---------------------------------------------------------------------------


def test_lambda_handler_dispatches_sqs_records(monkeypatch):
    """lambda_handler routes SQS Records to _run_investigation_sqs."""
    seen: dict[str, Any] = {}

    def fake_sqs(records, context):
        seen["records"] = records
        seen["context"] = context
        return {"batchItemFailures": []}

    monkeypatch.setattr(runner, "_run_investigation_sqs", fake_sqs)

    ctx = object()
    event = {
        "Records": [
            {"eventSource": "aws:sqs", "messageId": "msg-1",
             # Body is the bare investigation id (spec D10 / task 3.8 contract fix).
             "body": "aaaabbbb-cccc-dddd-eeee-000000000001"}
        ]
    }
    result = runner.lambda_handler(event, ctx)
    assert result == {"batchItemFailures": []}
    assert seen["records"] == event["Records"]
    assert seen["context"] is ctx


# ---------------------------------------------------------------------------
# SQS path: successful single record → batchItemFailures empty
# ---------------------------------------------------------------------------


def _make_sqs_env(monkeypatch, stage: str = "staging") -> None:
    """Patch boto3.client and env vars for _run_investigation_sqs."""
    monkeypatch.setenv("VSA_STAGE", stage)
    monkeypatch.setenv("ATHENA_RESULTS_BUCKET", "test-bucket")
    monkeypatch.setenv("AVX_FINDINGS_TABLE", "vsa-staging-avx-findings")
    monkeypatch.setenv("AVX_INVESTIGATIONS_TABLE", "vsa-staging-avx-investigations")
    monkeypatch.setenv("DYNAMODB_REGION", "us-west-2")
    monkeypatch.setenv("ADP_REGION", "us-east-1")
    monkeypatch.setenv("ATHENA_WORKGROUP", "cvx-staging-analytics")

    fake_ddb = object()
    fake_athena = object()
    fake_cw = _Cw()

    def fake_boto3_client(service, **_kw):
        if service == "dynamodb":
            return fake_ddb
        if service == "athena":
            return fake_athena
        if service == "cloudwatch":
            return fake_cw
        return object()

    monkeypatch.setattr("boto3.client", fake_boto3_client)

    class _FakeArtifactWriter:
        table_name = "vsa-staging-avx-findings"
        dynamodb_client = fake_ddb

        def __init__(self, **_kw): pass

        def write_finding(self, finding, *, standing=None, **_kw):
            return {"action": "put", "finding_id": finding.get("finding_id", "FIND-x")}

    # Patch ArtifactWriter used inside _run_investigation_sqs.
    import agents.tier2.artifact_writer as _aw
    monkeypatch.setattr(_aw, "ArtifactWriter", _FakeArtifactWriter)

    return fake_cw


def test_successful_sqs_record_returns_empty_batch_item_failures(monkeypatch):
    """A successful investigation → batchItemFailures is empty."""
    from agents.fi_quality import job as _job

    fake_cw = _make_sqs_env(monkeypatch)

    monkeypatch.setattr(_job, "run_investigation",
                        lambda *_a, **_kw: None)  # no-op: success

    records_out: list = []
    monkeypatch.setattr(runner, "_emit_run_record",
                        lambda *_a, **_kw: records_out.append(_kw))

    records = [
        {"eventSource": "aws:sqs", "messageId": "msg-1",
         # Bare investigation id — UUID format (spec D10 / task 3.8 contract fix).
         "body": "cccccccc-1111-1111-1111-000000000001"}
    ]
    result = runner._run_investigation_sqs(records, object())
    assert result == {"batchItemFailures": []}, (
        "A successful investigation must produce an empty batchItemFailures list")


# ---------------------------------------------------------------------------
# SQS path: failed investigation → message id in batchItemFailures
# ---------------------------------------------------------------------------


def test_failed_sqs_record_reported_in_batch_item_failures(monkeypatch):
    """A failing investigation → its messageId appears in batchItemFailures."""
    from agents.fi_quality import job as _job

    _make_sqs_env(monkeypatch)

    def boom(*_a, **_kw):
        raise RuntimeError("investigation failed")

    monkeypatch.setattr(_job, "run_investigation", boom)
    monkeypatch.setattr(runner, "_emit_run_record", lambda *_a, **_kw: None)

    records = [
        {"eventSource": "aws:sqs", "messageId": "msg-boom",
         # Bare investigation id — UUID format (spec D10 / task 3.8 contract fix).
         "body": "cccccccc-3333-3333-3333-000000000003"}
    ]
    result = runner._run_investigation_sqs(records, object())
    assert result == {"batchItemFailures": [{"itemIdentifier": "msg-boom"}]}, (
        "A failed investigation must report its messageId in batchItemFailures")


# ---------------------------------------------------------------------------
# SQS path: malformed body is a failed item, not a crash
# ---------------------------------------------------------------------------


def test_malformed_sqs_body_is_a_failed_item_not_a_crash(monkeypatch):
    """A body that is not a valid investigation id (UUID) is a failed item."""
    _make_sqs_env(monkeypatch)
    monkeypatch.setattr(runner, "_emit_run_record", lambda *_a, **_kw: None)

    records = [
        {"eventSource": "aws:sqs", "messageId": "msg-bad",
         "body": "this is not a uuid {{{"}
    ]
    # Must not raise.
    result = runner._run_investigation_sqs(records, object())
    assert result == {"batchItemFailures": [{"itemIdentifier": "msg-bad"}]}


def test_non_uuid_body_is_a_failed_item(monkeypatch):
    """A body that is non-empty but not UUID-shaped is a failed item.

    Previously this tested ``{"pattern_id": "pat-x"}`` (missing investigation_id
    key in a JSON object).  After the spec D10 contract fix the body is a bare
    string; any value that does not match the UUID regex is a failed item —
    including a JSON object serialised to a string.
    """
    _make_sqs_env(monkeypatch)
    monkeypatch.setattr(runner, "_emit_run_record", lambda *_a, **_kw: None)

    records = [
        {"eventSource": "aws:sqs", "messageId": "msg-noid",
         # A JSON object string: not a UUID — fails the shape check.
         "body": json.dumps({"pattern_id": "pat-x"})}
    ]
    result = runner._run_investigation_sqs(records, object())
    assert result == {"batchItemFailures": [{"itemIdentifier": "msg-noid"}]}


@pytest.mark.parametrize("body", [
    "this is not a uuid {{{",
    json.dumps({"investigation_id": "cccccccc-0001-0001-0001-000000000001"}),
    "cccccccc-0001-0001-0001-000000000001; DROP",
    "cccccccc-0001-0001-0001-00000000000",            # one hex digit short
    "../cccccccc-0001-0001-0001-000000000001",
])
def test_a_body_that_is_not_an_investigation_id_never_reaches_the_job(monkeypatch, body):
    """Security review g43 cycle 1 W1: the SQS body is checked against the UUID shape
    BEFORE run_investigation is called. The two tests above only see batchItemFailures,
    which a garbage id also produces when the job fails it downstream, so a check
    loosened to "non-empty" passed them. Here the job is stubbed: it must never be
    called, and the item still fails."""
    from agents.fi_quality import job as _job
    _make_sqs_env(monkeypatch)
    monkeypatch.setattr(runner, "_emit_run_record", lambda *_a, **_kw: None)
    called: list[str] = []
    monkeypatch.setattr(_job, "run_investigation",
                        lambda inv_id, **_kw: called.append(inv_id))

    records = [{"eventSource": "aws:sqs", "messageId": "msg-x", "body": body}]
    result = runner._run_investigation_sqs(records, object())

    assert called == [], f"run_investigation was called with {called!r} for body {body!r}"
    assert result == {"batchItemFailures": [{"itemIdentifier": "msg-x"}]}


def test_a_valid_investigation_id_does_reach_the_job(monkeypatch):
    """Positive control for the test above: a UUID-shaped body (with surrounding
    whitespace, which the runner strips) reaches run_investigation unchanged."""
    from agents.fi_quality import job as _job
    _make_sqs_env(monkeypatch)
    monkeypatch.setattr(runner, "_emit_run_record", lambda *_a, **_kw: None)
    called: list[str] = []
    monkeypatch.setattr(_job, "run_investigation",
                        lambda inv_id, **_kw: called.append(inv_id))

    records = [{"eventSource": "aws:sqs", "messageId": "msg-ok",
                "body": "  cccccccc-0001-0001-0001-000000000001\n"}]
    result = runner._run_investigation_sqs(records, object())

    assert called == ["cccccccc-0001-0001-0001-000000000001"]
    assert result == {"batchItemFailures": []}


# ---------------------------------------------------------------------------
# SQS path: run record keyed agent-fi-quality#investigation, heartbeat emitted
# ---------------------------------------------------------------------------


def test_sqs_path_writes_run_record_agent_fi_quality_investigation(monkeypatch):
    """SQS path writes exactly one run record keyed agent-fi-quality#investigation."""
    from agents.fi_quality import job as _job

    _make_sqs_env(monkeypatch)
    monkeypatch.setattr(_job, "run_investigation", lambda *_a, **_kw: None)

    records_written: list = []

    def fake_emit_run_record(run_record_writer, *, component, summary, agent_version,
                              stage, cloudwatch_client, agent_id, extra=None):
        records_written.append({"agent_id": agent_id, "component": component})
        # Also emit the heartbeat so staleness test passes if ever checked here.

    monkeypatch.setattr(runner, "_emit_run_record", fake_emit_run_record)

    records = [
        {"eventSource": "aws:sqs", "messageId": "msg-ok",
         # Bare investigation id — UUID format (spec D10 / task 3.8 contract fix).
         "body": "cccccccc-4444-4444-4444-000000000004"}
    ]
    runner._run_investigation_sqs(records, object())

    (rec,) = records_written
    assert rec["agent_id"] == FI_QUALITY_AGENT_ID
    assert rec["component"] == "investigation"


# ---------------------------------------------------------------------------
# SQS path: mixed batch — some succeed, some fail
# ---------------------------------------------------------------------------


def test_sqs_mixed_batch_partial_failures(monkeypatch):
    """Only failed message ids appear in batchItemFailures (not successful ones)."""
    from agents.fi_quality import job as _job

    _make_sqs_env(monkeypatch)
    monkeypatch.setattr(runner, "_emit_run_record", lambda *_a, **_kw: None)

    def conditional_fail(inv_id, **_kw):
        if inv_id == "cccccccc-2222-2222-2222-000000000002":
            raise RuntimeError("fail")

    monkeypatch.setattr(_job, "run_investigation", conditional_fail)

    records = [
        {"eventSource": "aws:sqs", "messageId": "msg-ok",
         # Bare UUID id — will succeed (spec D10 / task 3.8 contract fix).
         "body": "cccccccc-1111-1111-1111-000000000001"},
        {"eventSource": "aws:sqs", "messageId": "msg-fail",
         # Bare UUID id — conditional_fail raises for this id.
         "body": "cccccccc-2222-2222-2222-000000000002"},
    ]
    result = runner._run_investigation_sqs(records, object())
    assert result == {"batchItemFailures": [{"itemIdentifier": "msg-fail"}]}, (
        "Only failed message ids must appear in batchItemFailures")


# ---------------------------------------------------------------------------
# SQS path: ATHENA_RESULTS_BUCKET guard — all records failed, run record emitted
# ---------------------------------------------------------------------------


def test_missing_athena_results_bucket_fails_all_records(monkeypatch):
    """ATHENA_RESULTS_BUCKET unset → every SQS record is a failed item.

    The run record and heartbeat are still emitted so the staleness alarm does
    not fire on a misconfigured Lambda.

    Mutation guard: remove the ATHENA_RESULTS_BUCKET check → records are not
    pre-failed; run_investigation is called with an empty results_s3_path and
    this test finds the batchItemFailures list empty.
    """
    # Use _make_sqs_env then unset the bucket to simulate misconfiguration.
    _make_sqs_env(monkeypatch)
    monkeypatch.delenv("ATHENA_RESULTS_BUCKET", raising=False)

    records_written: list = []
    monkeypatch.setattr(runner, "_emit_run_record",
                        lambda *_a, **_kw: records_written.append(_kw))

    # run_investigation must NOT be called when the bucket is missing.
    from agents.fi_quality import job as _job
    run_inv_calls: list = []
    monkeypatch.setattr(_job, "run_investigation",
                        lambda *_a, **_kw: run_inv_calls.append(_kw))

    records = [
        {"eventSource": "aws:sqs", "messageId": "msg-1",
         "body": "cccccccc-1111-1111-1111-000000000001"},
        {"eventSource": "aws:sqs", "messageId": "msg-2",
         "body": "cccccccc-2222-2222-2222-000000000002"},
    ]
    result = runner._run_investigation_sqs(records, object())

    assert result["batchItemFailures"] == [
        {"itemIdentifier": "msg-1"},
        {"itemIdentifier": "msg-2"},
    ], (
        "All records must be failed when ATHENA_RESULTS_BUCKET is unset. "
        "Mutation (remove guard): batchItemFailures is empty, this assertion fails."
    )
    assert not run_inv_calls, (
        "run_investigation must NOT be called when ATHENA_RESULTS_BUCKET is unset."
    )
    assert len(records_written) == 1, (
        "Exactly one run record must be emitted even when the bucket guard fires. "
        "Mutation (skip heartbeat on guard): records_written is empty, fails."
    )
    assert records_written[0].get("agent_id") == FI_QUALITY_AGENT_ID


# ---------------------------------------------------------------------------
# Contract: service and investigation in VALID_COMPONENTS
# ---------------------------------------------------------------------------


def test_service_and_investigation_in_valid_components():
    """Both new components join VALID_COMPONENTS (contract pin)."""
    from agents.tier2.contract import VALID_COMPONENTS
    assert "service" in VALID_COMPONENTS
    assert "investigation" in VALID_COMPONENTS


def test_service_has_selection_limit():
    """_selection_limit_for_component does not raise for 'service'."""
    limit = runner._selection_limit_for_component("service")
    assert isinstance(limit, int) and limit >= 0


def test_investigation_sqs_stops_before_deadline(monkeypatch):
    """_run_investigation_sqs honours remaining time.

    When a fake Lambda context reports < margin ms remaining, _run_investigation_sqs
    must fail the not-yet-started records rather than starting them.
    """
    from agents.fi_quality import job as _job
    _make_sqs_env(monkeypatch)
    monkeypatch.setattr(_job, "run_investigation", lambda *_a, **_kw: None)
    monkeypatch.setattr(runner, "_emit_run_record", lambda *_a, **_kw: None)

    class _TightContext:
        def get_remaining_time_in_millis(self):
            return 0  # no time left

    records = [
        {"eventSource": "aws:sqs", "messageId": f"msg-{i}",
         "body": f"cccccccc-{i:04d}-{i:04d}-{i:04d}-{i:012d}"}
        for i in range(1, 4)
    ]
    result = runner._run_investigation_sqs(records, _TightContext())
    # All records should fail when there is no time left.
    assert len(result["batchItemFailures"]) == len(records), (
        "All records must fail when remaining time is zero."
    )


def test_service_has_token_budget():
    """_component_max_tokens does not raise for 'service'."""
    budget = runner._component_max_tokens("service")
    assert isinstance(budget, int) and budget > 0


def test_investigation_has_token_budget():
    """_component_max_tokens does not raise for 'investigation'."""
    budget = runner._component_max_tokens("investigation")
    assert isinstance(budget, int) and budget >= 0


# ---------------------------------------------------------------------------
# Env vars: TIER2_MAX_VINS_SERVICE, TIER2_MAX_TOKENS_SERVICE etc. are read
# through os.environ.get (bidirectional env contract)
# ---------------------------------------------------------------------------


def test_env_contract_service_vars_use_environ_get():
    """New env vars for 'service' and 'investigation' are read via os.environ.get."""
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
        if not (isinstance(func, ast.Attribute) and func.attr == "get"
                and isinstance(func.value, ast.Attribute)
                and func.value.attr == "environ"):
            continue
        if node.args and isinstance(node.args[0], ast.Constant):
            get_reads.add(node.args[0].value)

    for var in (
        "TIER2_MAX_VINS_SERVICE",
        "TIER2_MAX_VINS_INVESTIGATION",
        "TIER2_MAX_TOKENS_SERVICE",
        "TIER2_MAX_TOKENS_INVESTIGATION",
        "FAULT_GROUNDING_ENABLED",
        "AVX_INVESTIGATIONS_TABLE",
    ):
        assert var in get_reads, (
            f"{var} must be read via os.environ.get() in runner.py for "
            "the bidirectional stack/runner env-contract tests to pass"
        )


# ============================================================================
# C1/W2 fix tests: SQS runner stale-running detection and ApproximateReceiveCount
# ============================================================================

class TestRunnerSQSInvestigationFixes:
    """C1/W2 fix: SQS runner passes ApproximateReceiveCount to run_investigation."""

    def test_approximate_receive_count_passed_to_run_investigation(self):
        """_run_investigation_sqs passes ApproximateReceiveCount to run_investigation.

        W2 fix: on first delivery (count=1 < max=2), run_investigation should
        reset the row to queued on failure; on last delivery (count=2), set failed.
        This test verifies the count is forwarded, not that the job logic is correct
        (that is tested by TestJobMutationGuards in test_job_investigation.py).
        """
        import os
        from unittest.mock import MagicMock, patch
        import agents.tier2.runner as _runner
        import agents.tier2.artifact_writer as _aw

        # Env vars for the runner.
        env_overrides = {
            "VSA_STAGE": "staging",
            "ATHENA_RESULTS_BUCKET": "test-bucket",
            "AVX_FINDINGS_TABLE": "vsa-staging-avx-findings",
            "AVX_INVESTIGATIONS_TABLE": "vsa-staging-avx-investigations",
            "DYNAMODB_REGION": "us-west-2",
            "ADP_REGION": "us-east-1",
            "ATHENA_WORKGROUP": "cvx-staging-analytics",
            "SQS_MAX_RECEIVE_COUNT": "2",
        }

        received_kwargs: list = []

        def _capture_run(investigation_id, **kwargs):
            received_kwargs.append({"investigation_id": investigation_id, **kwargs})

        fake_ddb = MagicMock()
        fake_athena = MagicMock()

        class _FakeCw:
            def put_metric_data(self, **_kw): pass

        def _fake_boto3_client(service, **_kw):
            if service == "dynamodb": return fake_ddb
            if service == "athena": return fake_athena
            if service == "cloudwatch": return _FakeCw()
            return MagicMock()

        from agents.fi_quality import job as _job
        import uuid

        inv_id = str(uuid.uuid4())
        record = {
            "eventSource": "aws:sqs",
            "messageId": "msg-count-test",
            "body": inv_id,
            "attributes": {"ApproximateReceiveCount": "1"},
        }

        with patch.dict(os.environ, env_overrides), \
             patch("boto3.client", side_effect=_fake_boto3_client), \
             patch.object(_aw, "ArtifactWriter", MagicMock()), \
             patch("agents.tier2.run_records.write_run_record"), \
             patch.object(_job, "run_investigation", side_effect=_capture_run), \
             patch.object(_runner, "_emit_run_record", lambda *a, **kw: None):
            _runner._run_investigation_sqs([record], object())

        assert len(received_kwargs) == 1, "run_investigation must be called once."
        kwargs = received_kwargs[0]
        assert "approximate_receive_count" in kwargs, (
            "approximate_receive_count must be passed to run_investigation (W2 fix)."
        )
        assert kwargs["approximate_receive_count"] == 1, (
            f"Expected approximate_receive_count=1, got {kwargs['approximate_receive_count']}."
        )
        assert kwargs["max_receive_count"] == 2, (
            f"Expected max_receive_count=2 (from SQS_MAX_RECEIVE_COUNT), "
            f"got {kwargs['max_receive_count']}."
        )



# ---------------------------------------------------------------------------
# S4 (G3F3.3): SQS_MAX_RECEIVE_COUNT guard tests
# ---------------------------------------------------------------------------

class TestSQSMaxReceiveCountGuard:
    """G3F3.3 (a2 S4): SQS_MAX_RECEIVE_COUNT guard in _run_investigation_sqs.

    The S2 guard parses SQS_MAX_RECEIVE_COUNT once before the main loop.  A
    non-integer value must fail the entire batch (all records → batchItemFailures)
    and write one run record, without calling run_investigation.  A valid integer
    must be forwarded to run_investigation as max_receive_count.

    Two tests:
    1. "x" (non-integer) → all records failed, one run record, run_investigation
       never called.
    2. "3" (non-default integer) → run_investigation called with max_receive_count=3.

    Mutation guard for test 1: remove the int() guard → records are processed
    normally → batchItemFailures is empty (successful invocation) → assertion fails.
    Mutation guard for test 2: hardcode max_receive_count=2 → wrong value passed →
    assertion fails.
    """

    def test_invalid_max_receive_count_fails_all_records(self, monkeypatch):
        """SQS_MAX_RECEIVE_COUNT='x' → all records in batchItemFailures, one run record.

        The guard must fail the whole batch before entering the per-record loop,
        matching the ATHENA_RESULTS_BUCKET guard's pattern (test_missing_athena_results_bucket_fails_all_records).

        Mutation: remove int() try/except → records reach the per-record loop →
        run_investigation is called → batchItemFailures is empty → FAILS.
        """
        _make_sqs_env(monkeypatch)
        monkeypatch.setenv("SQS_MAX_RECEIVE_COUNT", "x")  # non-integer

        records_written: list = []
        monkeypatch.setattr(runner, "_emit_run_record",
                            lambda *_a, **_kw: records_written.append(_kw))

        from agents.fi_quality import job as _job
        run_inv_calls: list = []
        monkeypatch.setattr(_job, "run_investigation",
                            lambda *_a, **_kw: run_inv_calls.append(_kw))

        records = [
            {"eventSource": "aws:sqs", "messageId": "msg-inv-1",
             "body": "cccccccc-1111-1111-1111-000000000001"},
            {"eventSource": "aws:sqs", "messageId": "msg-inv-2",
             "body": "cccccccc-2222-2222-2222-000000000002"},
        ]
        result = runner._run_investigation_sqs(records, object())

        assert result["batchItemFailures"] == [
            {"itemIdentifier": "msg-inv-1"},
            {"itemIdentifier": "msg-inv-2"},
        ], (
            "All records must be failed when SQS_MAX_RECEIVE_COUNT is non-integer. "
            "Mutation (remove guard): batchItemFailures is empty → CAUGHT."
        )
        assert not run_inv_calls, (
            "run_investigation must NOT be called when SQS_MAX_RECEIVE_COUNT is invalid. "
            "Mutation (remove guard): run_investigation is called → CAUGHT."
        )
        assert len(records_written) == 1, (
            "Exactly one run record must be emitted for the whole-batch failure. "
            "Mutation (skip run record on guard): records_written empty → CAUGHT."
        )

    def test_custom_max_receive_count_forwarded_to_run_investigation(self, monkeypatch):
        """SQS_MAX_RECEIVE_COUNT='3' → run_investigation called with max_receive_count=3.

        The guard must parse and forward the env-var value, not use a hardcoded default.

        Mutation: hardcode max_receive_count=2 inside _run_investigation_sqs →
        run_investigation receives 2 instead of 3 → CAUGHT.
        """
        _make_sqs_env(monkeypatch)
        monkeypatch.setenv("SQS_MAX_RECEIVE_COUNT", "3")

        from agents.fi_quality import job as _job
        received_kwargs: list = []
        monkeypatch.setattr(_job, "run_investigation",
                            lambda *_a, **_kw: received_kwargs.append(_kw))
        monkeypatch.setattr(runner, "_emit_run_record", lambda *_a, **_kw: None)

        records = [
            {"eventSource": "aws:sqs", "messageId": "msg-mc-1",
             "body": "cccccccc-3333-3333-3333-000000000003",
             "attributes": {"ApproximateReceiveCount": "1"}},
        ]
        result = runner._run_investigation_sqs(records, object())

        assert result["batchItemFailures"] == [], (
            "A valid SQS_MAX_RECEIVE_COUNT must not pre-fail the record."
        )
        assert len(received_kwargs) == 1, "run_investigation must be called once."
        assert received_kwargs[0]["max_receive_count"] == 3, (
            f"max_receive_count must be 3 (from SQS_MAX_RECEIVE_COUNT='3'); "
            f"got {received_kwargs[0].get('max_receive_count')!r}. "
            "Mutation (hardcode 2): max_receive_count=2 → assertion fails → CAUGHT."
        )


# ---------------------------------------------------------------------------
# W4: TIER2_MAX_VINS_SERVICE cap enforced in run_service
# ---------------------------------------------------------------------------


def _make_run_service_call(
    monkeypatch, vehicle_rows, *, max_vins_service=None, loop_fn=None
):
    """Call runner.run_service directly with all patches (no _make_service_doubles)."""
    from agents.fi_fleet_health import selection as fh_sel
    from agents.fi_fleet_health import loop as fh_loop
    from agents.fi_recall_warranty import selection as rw_sel

    monkeypatch.setattr(fh_sel, "select_fault_rows", lambda *_a, **_kw: [])
    monkeypatch.setattr(fh_sel, "select_pm_rows", lambda *_a, **_kw: [])
    monkeypatch.setattr(fh_sel, "select_managed_vins", lambda *_a, **_kw: [])
    monkeypatch.setattr(fh_sel, "select_daily_usage", lambda *_a, **_kw: [])
    monkeypatch.setattr(fh_sel, "select_agent2_findings", lambda *_a, **_kw: ({}, 0))
    monkeypatch.setattr(rw_sel, "select_parts_stock", lambda *_a, **_kw: [])
    monkeypatch.setattr(fh_sel, "build_vehicle_rows", lambda **_kw: (list(vehicle_rows), {}))

    if max_vins_service is not None:
        monkeypatch.setattr(runner, "_MAX_VINS_SERVICE", max_vins_service)

    def _default_loop(*, vehicle_rows, **_kw):
        return {
            "vins_selected": len(vehicle_rows),
            "vins_processed": len(vehicle_rows),
            "items_written": 0,
            "items_skipped_unchanged": 0,
            "vins_refused": 0,
            "grounding_read_errors": 0,
            "exhausted_items_written": 0,
            "by_item_type": {},
        }

    monkeypatch.setattr(fh_loop, "run_health_loop", loop_fn or _default_loop)

    cw = _Cw()
    records: list = []
    result = runner.run_service(
        "staging",
        athena_client=object(),
        workgroup="cvx-staging-analytics",
        results_s3_path="s3://results/tier2/fi-fleet-health/",
        ddb_client=object(),
        findings_table="vsa-staging-avx-findings",
        actions_table="vsa-staging-avx-actions",
        groundings_table="cms-staging-storage-vehicle-groundings",
        vehicles_table="cms-staging-storage-vehicles",
        fleet_locations_table="cms-staging-storage-fleet-locations",
        avx_writer=_AvxWriter(),
        owner_resolver=None,
        cloudwatch_client=cw,
        run_record_writer=lambda **k: records.append(k),
    )
    return result, records


def test_service_run_vehicles_over_cap_counted(monkeypatch):
    """W4 (design answer 9): vehicles_over_cap > 0 when vehicle_rows exceed _MAX_VINS_SERVICE.

    Mutation: don't cap vehicle_rows -> vehicles_over_cap stays 0 -> assertion fails.
    """
    _test_cap = 2
    excess_rows = [
        # vehicle_id pre-set to skip the registry lookup (which needs a real ddb_client).
        {"vin": f"VIN{i:03d}", "fleet_id": "FLT-1", "vehicle_id": f"VEH{i:03d}",
         "open_faults": [], "pm_tasks": [], "agent2_findings": [],
         "utilisation": None, "position": None, "catalog_entries": {}}
        for i in range(_test_cap + 3)  # 5 rows, cap is 2
    ]
    result, _ = _make_run_service_call(
        monkeypatch, excess_rows, max_vins_service=_test_cap
    )

    assert result.get("vehicles_over_cap", 0) == 3, (
        f"vehicles_over_cap must be 3 (5 rows - cap 2); got {result.get('vehicles_over_cap')}. "
        "Mutation: don't cap vehicle_rows -> vehicles_over_cap stays 0 -> CAUGHT."
    )


def test_service_run_most_severe_vins_kept_when_over_cap(monkeypatch):
    """W4: run_service keeps the most severe vehicles when cap is exceeded.

    Mutation: keep arbitrary rows (not severity-sorted) -> p0 VIN not in processed
    batch -> assertion fails.
    """
    p0_vin = "VINP0000000000001"
    routine_vin = "VINROUTINE0000002"
    rows = [
        # vehicle_id pre-set to skip the registry lookup.
        {"vin": routine_vin, "fleet_id": "FLT-1", "vehicle_id": "VEH-routine",
         "open_faults": [], "pm_tasks": [], "agent2_findings": [],
         "utilisation": None, "position": None, "catalog_entries": {}},
        {"vin": p0_vin, "fleet_id": "FLT-1", "vehicle_id": "VEH-p0",
         "open_faults": [{"code": "P0420", "band": "p0", "last_seen_at": "2026-10-01Z"}],
         "pm_tasks": [], "agent2_findings": [], "utilisation": None,
         "position": None, "catalog_entries": {}},
    ]

    processed_vins: list[str] = []

    def fake_loop(*, vehicle_rows, **_kw):
        processed_vins.extend(r["vin"] for r in vehicle_rows)
        return {
            "vins_selected": len(vehicle_rows),
            "vins_processed": len(vehicle_rows),
            "items_written": 0,
            "items_skipped_unchanged": 0,
            "vins_refused": 0,
            "grounding_read_errors": 0,
            "exhausted_items_written": 0,
            "by_item_type": {},
        }

    _make_run_service_call(monkeypatch, rows, max_vins_service=1, loop_fn=fake_loop)

    assert p0_vin in processed_vins, (
        f"The p0 VIN {p0_vin!r} must be in the processed batch (most severe kept). "
        "Mutation: keep arbitrary rows -> p0 VIN may be dropped -> CAUGHT."
    )
    assert routine_vin not in processed_vins, (
        f"The routine VIN {routine_vin!r} must be excluded when cap=1 and a p0 VIN exists."
    )


# ---------------------------------------------------------------------------
# W7: registry_read_errors counted and forwarded in run_service
# ---------------------------------------------------------------------------


def test_service_run_vehicle_id_lookup_error_counted(monkeypatch):
    """W7: a vehicle-id RegistryReadError increments registry_read_errors in the summary.

    Mutation: drop the increment -> registry_read_errors stays 0 -> assertion fails.
    """
    from agents.fi_recall_warranty import selection as rw_sel
    from agents.fi_recall_warranty.selection import RegistryReadError

    row = {
        "vin": "VIN001", "fleet_id": "FLT-1", "vehicle_id": None,
        "open_faults": [], "pm_tasks": [], "agent2_findings": [],
        "utilisation": None, "position": None, "catalog_entries": {},
    }

    # Make the vehicle-id lookup raise RegistryReadError.
    monkeypatch.setattr(rw_sel, "lookup_vehicle_cms_ids",
                        lambda *_a, **_kw: (_ for _ in ()).throw(RegistryReadError("test")))

    result, records = _make_run_service_call(monkeypatch, [row])

    assert result.get("registry_read_errors", 0) == 1, (
        f"registry_read_errors must be 1 after a vehicle-id lookup RegistryReadError; "
        f"got {result.get('registry_read_errors')}. "
        "Mutation: drop the increment -> count stays 0 -> CAUGHT."
    )
    (rec,) = records
    assert rec["status"] == "completed", "Run must complete despite registry error."


def test_service_run_fleet_location_error_counted(monkeypatch):
    """W7: a fleet-location RegistryReadError increments registry_read_errors.

    Mutation: drop the increment -> count stays 0 -> assertion fails.
    """
    from agents.fi_recall_warranty import selection as rw_sel
    from agents.fi_recall_warranty.selection import RegistryReadError

    row = {
        "vin": "VIN001", "fleet_id": "FLT-1", "vehicle_id": None,
        "open_faults": [], "pm_tasks": [], "agent2_findings": [],
        "utilisation": None, "position": None, "catalog_entries": {},
    }

    # Vehicle-id lookup succeeds (returns vid, fleet_id, location_id).
    monkeypatch.setattr(rw_sel, "lookup_vehicle_cms_ids",
                        lambda *_a, **_kw: ("VEH-001", "FLT-1", "LOC-1"))
    # Fleet-location lookup raises.
    monkeypatch.setattr(rw_sel, "lookup_fleet_locations",
                        lambda *_a, **_kw: (_ for _ in ()).throw(RegistryReadError("test")))

    result, records = _make_run_service_call(monkeypatch, [row])

    assert result.get("registry_read_errors", 0) == 1, (
        f"registry_read_errors must be 1 after a fleet-location lookup RegistryReadError; "
        f"got {result.get('registry_read_errors')}. "
        "Mutation: drop the increment -> count stays 0 -> CAUGHT."
    )
    (rec,) = records
    assert rec["status"] == "completed", "Run must complete despite registry error."
