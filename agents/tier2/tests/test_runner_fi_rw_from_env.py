"""Fix Group G3-1 F8 (review cycle 1 routes W4, S3, S6): the PRODUCTION wiring of the recall
and warranty components, through ``_run_recall_from_env`` / ``_run_warranty_from_env``.

boto3.client / boto3.resource are replaced by recorders; the component's selection and loop
are stubbed at their module seams, so what is under test is only the wiring:
- the standing reaching ``write_finding`` is the one ``resolve_standing_facts`` returned
  (cycle 1 mutant Mf set ``owner_resolver=None`` and survived), with refresh_on_noop=True;
- each run builds its own assigned-driver directory and passes it as ``drivers=`` (landing
  fix L2: main made ``drivers`` a required keyword while Group 3 was on its branch);
- a CloudWatch client is built and both the run metrics and the ``RunRecordsWritten``
  heartbeat are put (the staleness alarm of task 4.3 watches that heartbeat);
- the run record carries the component's counters (S3);
- no ``bedrock-runtime`` client is built (S6: the rules path calls no model).
"""
from __future__ import annotations

import inspect
from typing import Any
from unittest.mock import MagicMock

import boto3
import pytest

from agents.fi_recall_warranty import recall as rw_recall
from agents.fi_recall_warranty import selection as rw_sel
from agents.fi_recall_warranty import warranty as rw_w
from agents.tier2 import artifact_writer, owner_resolver, runner

_STANDING = object()
_VIN = "MRDTST000000F8001"
_VIN2 = "MRDTST000000F8002"
_REAL_RESOLVE_SIG = inspect.signature(owner_resolver.resolve_standing_facts)


class _Writer:
    instances: list = []

    def __init__(self, table_name: str, dynamodb_client: Any) -> None:
        self.table_name, self.dynamodb_client = table_name, dynamodb_client
        self.calls: list = []
        _Writer.instances.append(self)

    def write_finding(self, finding, *, standing=None, refresh_on_noop=False,
                      update_on_same_hash=False):
        self.calls.append((finding, standing, refresh_on_noop))
        return {"action": "put", "finding_id": finding["finding_id"]}


def _fake_loop(**kw):
    # As the real loops do: no resolver means no standing (never a TypeError here).
    standing = kw["standing_fn"](_VIN) if kw.get("standing_fn") else None
    if kw.get("standing_fn"):
        kw["standing_fn"](_VIN2)   # a second vehicle in the same run (L2c: one directory per run)
    kw["write_finding"]({"finding_id": "FIND-f8"}, standing, refresh_on_noop=True)
    return {"rows": 1, "items_written": 1, "items_skipped_unchanged": 0, "rows_refused": 2,
            "grounding_read_errors": 3, "book_items_without_rooftops": 4,
            "rows_without_terms": 5, "by_item_type": {"book": 1}}


@pytest.fixture()
def wired(monkeypatch):
    built: list = []
    clients: dict = {}
    resources: list = []

    def fake_client(service, *a, **kw):
        built.append(service)
        # A new client per call (the first per service is kept), so a second DynamoDB client
        # is a different object from the run's (L2d).
        m = MagicMock(name=service)
        clients.setdefault(service, m)
        return m

    def fake_resource(service, *a, **kw):
        r = MagicMock(name=f"resource-{service}")
        resources.append(r)
        return r

    resolved: list = []
    directories: list = []

    def fake_resolve(vin, **kw):
        # Bound against the REAL signature, so a call missing a required keyword (landing
        # fix L2: `drivers`) fails this test's assertion instead of passing a **kw stub.
        try:
            _REAL_RESOLVE_SIG.bind(vin, **kw)
            sig_ok: Any = True
        except TypeError as exc:
            sig_ok = repr(exc)
        resolved.append((vin, kw, sig_ok))
        return _STANDING

    def fake_directory(stage, *, ddb_client):
        d = object()
        directories.append((stage, ddb_client, d))
        return d

    monkeypatch.setattr(boto3, "client", fake_client)
    monkeypatch.setattr(boto3, "resource", fake_resource)
    monkeypatch.setattr(artifact_writer, "ArtifactWriter", _Writer)
    monkeypatch.setattr(runner, "resolve_standing_facts", fake_resolve)
    monkeypatch.setattr(runner, "_assigned_driver_directory", fake_directory)
    monkeypatch.setattr(rw_sel, "select_unremedied_recalls", lambda *a, **kw: ([{"vin": _VIN}], 6))
    monkeypatch.setattr(rw_sel, "select_parts_stock", lambda *a, **kw: [])
    monkeypatch.setattr(rw_sel, "select_warranty_candidates", lambda *a, **kw: [{"vin": _VIN}])
    monkeypatch.setattr(rw_sel, "select_warranty_terms", lambda *a, **kw: {})
    monkeypatch.setattr(rw_recall, "run_recall_loop", _fake_loop)
    monkeypatch.setattr(rw_w, "run_warranty_loop", _fake_loop)
    monkeypatch.setenv("ATHENA_RESULTS_BUCKET", "cvx-staging-athena-results-x")
    monkeypatch.setenv("AVX_AGENT_RUNS_TABLE", "vsa-staging-avx-agent-runs")
    monkeypatch.delenv("CMS_VEHICLES_TABLE", raising=False)
    _Writer.instances.clear()
    return {"built": built, "clients": clients, "resources": resources, "resolved": resolved,
            "directories": directories}


def _metric_names(cw: MagicMock) -> list:
    return [d["MetricName"] for c in cw.put_metric_data.call_args_list
            for d in c.kwargs.get("MetricData", [])]


@pytest.mark.parametrize("component,fn", [("recall", "_run_recall_from_env"),
                                          ("warranty", "_run_warranty_from_env")])
def test_from_env_wires_standing_metrics_record_and_no_model(wired, monkeypatch, component, fn):
    """S4 contract: _from_env builds exactly one bedrock-runtime client alongside other clients.

    Changed from Group 3's "no bedrock-runtime client" assertion to the S4 contract:
    _from_env builds exactly one bedrock-runtime client (with cost's Config) as part of
    the model wiring; the rest of the wiring (standing, metrics, run record) is unchanged.
    """
    monkeypatch.setenv("FI_COST_GUARDRAIL_ID", "test-guardrail-id")
    monkeypatch.setenv("FI_COST_GUARDRAIL_VERSION", "1")
    getattr(runner, fn)("staging", {}, None)

    (writer,) = _Writer.instances
    assert writer.calls == [({"finding_id": "FIND-f8"}, _STANDING, True)]
    # Landing fix L2: the run builds ONE assigned-driver directory from its own DynamoDB
    # client and every standing resolution receives exactly that directory.
    assert len(wired["directories"]) == 1, wired["directories"]
    ((stage, dir_client, directory),) = wired["directories"]
    assert stage == "staging"
    assert dir_client is wired["clients"]["dynamodb"] is writer.dynamodb_client
    kwargs = {"ddb_client": wired["clients"]["dynamodb"],
              "vehicles_table": "cms-staging-storage-vehicles", "drivers": directory}
    assert wired["resolved"] == [(_VIN, kwargs, True), (_VIN2, kwargs, True)]

    assert "bedrock-runtime" in wired["built"], wired["built"]
    assert wired["built"].count("bedrock-runtime") == 1, "exactly one bedrock-runtime client"
    assert "cloudwatch" in wired["built"], wired["built"]
    cw = wired["clients"]["cloudwatch"]
    names = _metric_names(cw)
    assert "RunRecordsWritten" in names, names
    assert "ArtifactsWritten" in names, names

    (res,) = wired["resources"]
    item = res.Table.return_value.put_item.call_args.kwargs["Item"]
    assert item["agent_component"] == f"agent-fi-recall-warranty#{component}"
    counters = {k: item.get(k) for k in ("vins_selected", "rows_refused", "grounding_read_errors",
                                         "book_items_without_rooftops", "rows_without_terms")}
    assert counters == {"vins_selected": 1,
                        "rows_refused": 2 + 6 if component == "recall" else 2,
                        "grounding_read_errors": 3, "book_items_without_rooftops": 4,
                        "rows_without_terms": 5}, counters


@pytest.mark.parametrize("component,fn,reader", [
    ("recall", "_run_recall_from_env", "select_unremedied_recalls"),
    ("warranty", "_run_warranty_from_env", "select_warranty_candidates")])
def test_aborted_run_puts_run_metrics_and_an_aborted_record(wired, monkeypatch, component, fn,
                                                            reader):
    """Cycle 2 routes S2: the abort exit emits the run metrics too, and records the abort."""
    monkeypatch.setenv("FI_COST_GUARDRAIL_ID", "test-gid")
    monkeypatch.setenv("FI_COST_GUARDRAIL_VERSION", "1")

    def boom(*a, **kw):
        raise RuntimeError("AthenaCursorError")

    monkeypatch.setattr(rw_sel, reader, boom)
    with pytest.raises(RuntimeError):
        getattr(runner, fn)("staging", {}, None)
    names = _metric_names(wired["clients"]["cloudwatch"])
    assert "ArtifactsWritten" in names, names
    (res,) = wired["resources"]
    item = res.Table.return_value.put_item.call_args.kwargs["Item"]
    assert (item["agent_component"], item["status"]) == (
        f"agent-fi-recall-warranty#{component}", "aborted")
