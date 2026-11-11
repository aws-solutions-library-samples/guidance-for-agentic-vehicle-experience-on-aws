"""Handler side of 11.3b: items 3 (mode), 4 (run record), 5 (raise after it),
6 (decision reads), 7 (grounded codes), 8 (pending recall groundings).

The run record is written by the REAL ``agents.tier2.run_records.write_run_record``
into a fake DynamoDB resource that keeps every item (item 4: the first pass's
``extra=`` keyword raised a TypeError that was swallowed, so nothing was ever
written while every mocked test stayed green).

Mutants (records/11/mutants-11.3b-s2.md):
  H3  an unknown mode runs as approve
  H4  the run record's detail dropped / passed as ``extra=``
  H5  no raise after a unit error
  H6  decision reads for every finding again
  H7  a VIN counts as grounded without its codes (select side, test_select_11_3b.py)
  H8  pending recall groundings ignored
"""
from __future__ import annotations

import json
import os
import sys
from typing import Any
from unittest.mock import patch

import pytest
from botocore.exceptions import ClientError

_LAMBDA_ROOT = os.path.join(os.path.dirname(__file__), "..")
_REPO_ROOT = os.path.abspath(os.path.join(_LAMBDA_ROOT, "..", ".."))
for _p in (_LAMBDA_ROOT, os.path.dirname(_LAMBDA_ROOT), _REPO_ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

os.environ.setdefault("FI_SIM_OPERATOR_ENABLED", "true")
os.environ.setdefault("AVX_SIM_OPERATOR_TABLE", "vsa-test-avx-sim-operator")
os.environ.setdefault("AVX_ACTIONS_TABLE", "vsa-test-avx-actions")
os.environ.setdefault("AVX_FINDINGS_TABLE", "vsa-test-avx-findings")
os.environ.setdefault("AVX_AGENT_RUNS_TABLE", "vsa-test-avx-agent-runs")
os.environ.setdefault("FI_SIM_OPERATOR_PARAM", "/cvx/test/fi/sim-operator")
os.environ.setdefault("CMS_VEHICLES_TABLE", "cms-test-storage-vehicles")
os.environ.setdefault("CMS_GROUNDINGS_TABLE", "cms-test-storage-vehicle-groundings")

import handler as h  # noqa: E402

_FLEET = "demo-lastmile-delivery"
_CONFIG = {"fleets": {_FLEET: {"enabled": True}}, "daily_cap": 5,
           "hold_vins": [], "hold_vehicle_ids": []}


class _RunsTable:
    def __init__(self, store: list, fail: bool) -> None:
        self._store, self._fail = store, fail

    def put_item(self, Item: dict, ConditionExpression: str | None = None) -> dict:
        if self._fail:
            raise ClientError({"Error": {"Code": "AccessDeniedException", "Message": ""}}, "PutItem")
        json.dumps(Item)  # a float or other non-JSON value would break the real put too
        self._store.append(Item)
        return {}


class _RunsResource:
    def __init__(self, fail: bool = False) -> None:
        self.items: list[dict] = []
        self._fail = fail

    def Table(self, name: str) -> _RunsTable:  # noqa: N802
        assert name == "vsa-test-avx-agent-runs"
        return _RunsTable(self.items, self._fail)


@pytest.fixture
def runs(monkeypatch):
    res = _RunsResource()
    import boto3
    monkeypatch.setattr(boto3, "resource", lambda *a, **k: res)
    return res


def _patched(**over):
    """The handler's AWS-touching helpers, faked; ``over`` replaces any of them."""
    base = {
        "_load_ssm_config": lambda name: _CONFIG,
        "_scan_findings_for_fleet": lambda fleet, table: [],
        "_get_decided_ids": lambda ids, table: set(),
        "_read_grounded_codes": lambda fleet, table: ({}, 0),
    }
    base.update(over)
    return [patch.object(h, k, side_effect=v) for k, v in base.items()]


def _run(event: dict, **over) -> dict:
    ps = _patched(**over)
    for p in ps:
        p.start()
    try:
        with patch("agents.fi_sim_operator.ledger.get_first_seen_times", return_value={}), \
             patch("agents.fi_sim_operator.ledger.get_today_cap_used", return_value=0), \
             patch("agents.fi_sim_operator.ledger.record_first_seen", return_value=True):
            return h.handler(event, None)
    finally:
        for p in ps:
            p.stop()


# ── item 3: exact modes only ───────────────────────────────────────────────────

class TestModes:
    @pytest.mark.parametrize("mode", ["aprove", "APPROVE", " approve", "dry-run", "", None, 1])
    def test_unknown_mode_reads_and_writes_nothing(self, mode, runs):
        def _boom(*a, **k):
            raise AssertionError("read after an unknown mode")
        resp = _run({"mode": mode}, _load_ssm_config=_boom, _scan_findings_for_fleet=_boom)
        assert resp["status"] == "config_error"
        assert runs.items == []

    def test_no_mode_means_approve(self, runs):
        resp = _run({})
        assert resp["mode"] == "approve" and resp["status"] == "ok"


# ── item 4: the run record really writes ───────────────────────────────────────

class TestRunRecord:
    def test_completed_run_writes_its_detail(self, runs, capsys):
        # S3: dry_run uses the "dry_run" component, not "approve".
        resp = _run({"mode": "dry_run"})
        assert resp["status"] == "ok"
        assert len(runs.items) == 1
        item = runs.items[0]
        assert item["agent_component"] == "agent-fi-sim-operator#dry_run"
        assert item["status"] == "completed"
        assert item["tokens_used"] == 0
        assert item["sim_operator"]["mode"] == "dry_run"
        assert item["sim_operator"]["errors"] == 0
        assert set(item["sim_operator"]["fleets"]) == {_FLEET}
        # S3: dry_run must NOT emit the liveness metric (it would reset the staleness alarm).
        lines = [json.loads(x) for x in capsys.readouterr().out.splitlines() if "SimOperatorRunCompleted" in x]
        assert lines == []

    def test_approve_run_writes_approve_record_and_emits_liveness(self, runs, capsys):
        # S3: approve mode writes "#approve" and emits SimOperatorRunCompleted.
        resp = _run({})
        assert resp["status"] == "ok"
        assert len(runs.items) == 1
        assert runs.items[0]["agent_component"] == "agent-fi-sim-operator#approve"
        lines = [json.loads(x) for x in capsys.readouterr().out.splitlines() if "SimOperatorRunCompleted" in x]
        assert len(lines) == 1 and lines[0]["_aws"]["CloudWatchMetrics"][0]["Dimensions"] == [[]]

    def test_observe_run_writes_observe_record_and_no_liveness(self, runs, capsys):
        # Issue 2026-11-08-sim-operator-observe-run-label: an --observe-now run was written
        # as "#approve" and reset the staleness alarm the scheduled approve run feeds.
        resp = _run({"mode": "observe"})
        assert resp["status"] == "ok"
        assert len(runs.items) == 1
        assert runs.items[0]["agent_component"] == "agent-fi-sim-operator#observe"
        assert runs.items[0]["sim_operator"]["mode"] == "observe"
        lines = [x for x in capsys.readouterr().out.splitlines() if "SimOperatorRunCompleted" in x]
        assert lines == []

    @pytest.mark.parametrize("mode, emits", [("dry_run", False), ("observe", False), ("approve", True)])
    def test_component_is_the_mode_and_only_approve_emits_liveness(self, mode, emits, runs, capsys):
        # The off path (parameter absent) goes through the same _finish: same rule.
        def _absent(name):
            raise ClientError({"Error": {"Code": "ParameterNotFound", "Message": ""}}, "GetParameter")
        for over in ({}, {"_load_ssm_config": _absent}):
            runs.items.clear()
            _run({"mode": mode}, **over)
            assert [i["agent_component"] for i in runs.items] == [f"agent-fi-sim-operator#{mode}"]
            lines = [x for x in capsys.readouterr().out.splitlines() if "SimOperatorRunCompleted" in x]
            assert len(lines) == (1 if emits else 0)

    def test_absent_parameter_is_off_not_an_error(self, runs):
        def _absent(name):
            raise ClientError({"Error": {"Code": "ParameterNotFound", "Message": ""}}, "GetParameter")
        resp = _run({}, _load_ssm_config=_absent)
        assert resp["status"] == "off"
        assert runs.items[0]["sim_operator"]["reason"] == "parameter_absent"

    def test_run_record_failure_raises(self, monkeypatch):
        res = _RunsResource(fail=True)
        import boto3
        monkeypatch.setattr(boto3, "resource", lambda *a, **k: res)
        with pytest.raises(RuntimeError, match="1 error"):
            _run({})  # approve mode (not dry_run) so it writes and fails

    def test_run_record_failure_raises_dry_run(self, monkeypatch):
        # S4: a dry_run whose run record fails must also raise.
        res = _RunsResource(fail=True)
        import boto3
        monkeypatch.setattr(boto3, "resource", lambda *a, **k: res)
        with pytest.raises(RuntimeError, match="1 error"):
            _run({"mode": "dry_run"})


# ── item 5: raise after the run record ─────────────────────────────────────────

class TestRaiseAfterRecord:
    def test_grounding_read_error_is_recorded_then_raised(self, runs):
        with pytest.raises(RuntimeError, match="1 error"):
            _run({"mode": "dry_run"}, _read_grounded_codes=lambda f, t: ({}, 1))
        assert runs.items[0]["sim_operator"]["errors"] == 1
        assert runs.items[0]["sim_operator"]["fleets"][_FLEET]["grounding_read_errors"] == 1

    def test_unit_error_is_recorded_then_raised(self, runs):
        from agents.fi_sim_operator.run import FleetRunResult
        bad = FleetRunResult(fleet_id=_FLEET, mode="approve", units_error=2)
        with patch("agents.fi_sim_operator.run.run_fleet", return_value=bad):
            with pytest.raises(RuntimeError, match="2 error"):
                _run({})
        assert runs.items[0]["sim_operator"]["fleets"][_FLEET]["units_error"] == 2


# ── item 6: decision reads only for in-scope kinds ─────────────────────────────

class TestDecidedReads:
    def test_only_simulated_kinds_are_read(self, runs):
        findings = [
            {"finding_id": "F-cost", "recommended_action": {"action_kind": "apply_cost_action"}},
            {"finding_id": "F-move", "recommended_action": {"action_kind": "move_vehicles"}},
            {"finding_id": "F-none"},
            {"finding_id": "F-book", "recommended_action": {"action_kind": "book_fleet_service"}},
            {"finding_id": "F-gnd", "recommended_action": {"action_kind": "ground_vehicle"}},
        ]
        seen: list[list[str]] = []
        _run({"mode": "dry_run"},
             _scan_findings_for_fleet=lambda f, t: findings,
             _get_decided_ids=lambda ids, t: seen.append(list(ids)) or set())
        assert seen == [["F-book", "F-gnd"]]


# ── item 7: the groundings reader ──────────────────────────────────────────────

def _row(vin, codes, *, reason="fault", fid="FIND-1", aid="AVX-ACT-1"):
    item = {"vehicleId": {"S": vin}, "vin": {"S": vin}, "openFleetId": {"S": _FLEET},
            "reason": {"S": reason}, "faultCodes": {"L": [{"S": c} for c in codes]}}
    if fid:
        item["findingId"] = {"S": fid}
    if aid:
        item["actionId"] = {"S": aid}
    return item


class _GroundingsDdb:
    def __init__(self, pages: list[list[dict]], fail: bool = False) -> None:
        self.pages, self.fail, self.calls = pages, fail, []

    def query(self, **kw):
        self.calls.append(kw)
        if self.fail:
            raise ClientError({"Error": {"Code": "AccessDeniedException", "Message": ""}}, "Query")
        i = int(kw.get("ExclusiveStartKey", {}).get("i", {}).get("N", "0"))
        out = {"Items": self.pages[i]}
        if i + 1 < len(self.pages):
            out["LastEvaluatedKey"] = {"i": {"N": str(i + 1)}}
        return out


class TestGroundedCodesReader:
    def test_union_of_fi_fault_rows_across_pages(self, monkeypatch):
        vin, other = "MRDTEST0000000001", "MRDTEST0000000002"
        ddb = _GroundingsDdb([
            [_row(vin, ["P0A0D"]), _row(other, ["P0A7F"], reason="recall")],
            [_row(vin, ["P0A7F"]), _row(other, ["P0001"], fid=None)],
        ])
        monkeypatch.setattr(h, "_get_ddb", lambda: ddb)
        codes, errs = h._read_grounded_codes(_FLEET, "cms-test-storage-vehicle-groundings")
        assert (codes, errs) == ({vin: frozenset({"P0A0D", "P0A7F"})}, 0)
        assert ddb.calls[0]["IndexName"] == "open-groundings-index"
        assert ddb.calls[0]["ExpressionAttributeValues"] == {":f": {"S": _FLEET}}
        assert len(ddb.calls) == 2

    def test_read_error_means_nothing_grounded_and_one_error(self, monkeypatch):
        monkeypatch.setattr(h, "_get_ddb", lambda: _GroundingsDdb([], fail=True))
        assert h._read_grounded_codes(_FLEET, "t") == ({}, 1)

    def test_unset_table_is_an_error(self):
        assert h._read_grounded_codes(_FLEET, "") == ({}, 1)


# ── item 8: pending recall groundings ──────────────────────────────────────────

class TestRecallPending:
    def _g(self, fid, vin, rid, expires="2026-11-09T00:00:00Z"):
        return {"finding_id": fid, "scope": {"vin": vin}, "expires_at": expires,
                "recommended_action": {"action_kind": "ground_vehicle",
                                       "parameters": {"recall_id": rid, "vehicle_vin": vin}}}

    def test_undecided_unexpired_recall_groundings_only(self):
        now = "2026-11-06T07:10:00Z"
        fs = [
            self._g("F1", "V1", "MRD-1"),
            self._g("F2", "V2", "MRD-1"),                                 # decided
            self._g("F3", "V3", "MRD-1", expires="2026-11-05T00:00:00Z"),  # expired
            {"finding_id": "F4", "scope": {"vin": "V4"}, "expires_at": "2026-11-09T00:00:00Z",
             "recommended_action": {"action_kind": "ground_vehicle",
                                    "parameters": {"fault_codes": ["P0A0D"]}}},  # fault, not recall
        ]
        assert h._recall_pending_groundings(fs, {"F2"}, now) == {("V1", "MRD-1")}

    def test_handler_passes_them_to_the_classifier(self, runs):
        g = self._g("F1", "MRDTEST0000000001", "MRD-2026-004")
        got: list = []
        from agents.fi_sim_operator import select as sel
        real = sel.classify_finding

        def _spy(**kw):
            got.append(kw["recall_pending_groundings"])
            return real(**kw)
        with patch("agents.fi_sim_operator.select.classify_finding", side_effect=_spy):
            _run({"mode": "dry_run"}, _scan_findings_for_fleet=lambda f, t: [g])
        assert got and got[0] == {("MRDTEST0000000001", "MRD-2026-004")}


# ── W4: candidates_by_class counted before 20 h / cap filters ─────────────────

class TestCandidatesByClass:
    def test_dry_run_reports_candidates_by_class_before_plan_filters(self, runs):
        """W4: candidates_by_class comes from classified (pre-filter), not plan.units.

        With an empty SEEN ledger every candidate is 'too_new' so plan.units == [].
        planned_by_class is therefore {}.  But candidates_by_class must still be
        non-zero for each class present in classified.
        """
        from agents.fi_sim_operator.select import ClassifiedFinding
        finding = {"finding_id": "F-w4",
                   "recommended_action": {"action_kind": "book_fleet_service"}}
        cf = ClassifiedFinding(finding=finding, class_name="warranty_file", skip_reason=None)
        with patch("agents.fi_sim_operator.select.classify_finding", return_value=cf):
            resp = _run(
                {"mode": "dry_run"},
                _scan_findings_for_fleet=lambda f, t: [finding],
            )
        fleet = resp["fleets"][_FLEET]
        # pre-filter count is non-zero
        assert fleet["candidates_by_class"].get("warranty_file", 0) == 1
        assert "F-w4" in fleet["candidate_ids_by_class"].get("warranty_file", [])
        # plan filtered everything out (empty first_seen -> too_new)
        assert fleet["planned_by_class"] == {}

    def test_candidate_ids_capped_at_50_per_class(self, runs):
        """W4: finding_ids per class are capped at 50."""
        from agents.fi_sim_operator.select import ClassifiedFinding
        findings = [{"finding_id": f"F-{i}",
                     "recommended_action": {"action_kind": "book_fleet_service"}}
                    for i in range(60)]
        def _cf(finding, **_kw):
            return ClassifiedFinding(
                finding=finding, class_name="warranty_file", skip_reason=None)
        with patch("agents.fi_sim_operator.select.classify_finding", side_effect=_cf):
            resp = _run(
                {"mode": "dry_run"},
                _scan_findings_for_fleet=lambda f, t: findings,
            )
        fleet = resp["fleets"][_FLEET]
        assert fleet["candidates_by_class"]["warranty_file"] == 60
        assert len(fleet["candidate_ids_by_class"]["warranty_file"]) == 50


# ── W1: finer-grained counters appear in the run record ───────────────────────

class TestW1RunRecordCounters:
    def test_grounding_not_made_appears_in_run_record(self, runs):
        """W1: when a grounding is written but CMS refuses it, grounding_not_made
        must appear in the per-fleet run record entry."""
        from agents.fi_sim_operator.run import FleetRunResult
        result = FleetRunResult(
            fleet_id=_FLEET, mode="approve",
            units_approved=1,
            grounding_not_made=1,
            decisions_written=1,
        )
        with patch("agents.fi_sim_operator.run.run_fleet", return_value=result):
            resp = _run({})
        fleet = runs.items[0]["sim_operator"]["fleets"][_FLEET]
        assert fleet["grounding_not_made"] == 1
        assert fleet["executor_executed"] == 0
        assert fleet["executor_failed"] == 0
        assert fleet["executor_declined"] == 0

    def test_in_progress_counts_as_executor_executed(self, runs):
        """Q2 guard: in_progress (what DMS returns for a successful booking)
        must increment executor_executed.  Mutant Q2 omits in_progress from the
        condition, so a fleet of recall_book / fh_book singletons reports 0 executed."""
        from agents.fi_sim_operator.run import FleetRunResult, UnitResult
        fr = FleetRunResult(fleet_id=_FLEET, mode="approve")
        # Two units with in_progress grounding status
        fr.unit_results = [
            UnitResult(finding_id="F1", class_name="fh_book", mode="approve",
                       outcome="approved", grounding_status="in_progress"),
            UnitResult(finding_id="F2", class_name="recall_book", mode="approve",
                       outcome="approved", grounding_status="in_progress"),
        ]
        fr.units_approved = 2
        fr.decisions_written = 2
        fr.executor_executed = 2  # already tallied in run_fleet
        with patch("agents.fi_sim_operator.run.run_fleet", return_value=fr):
            resp = _run({})
        fleet = runs.items[0]["sim_operator"]["fleets"][_FLEET]
        assert fleet["executor_executed"] == 2, (
            "Q2 guard: in_progress must be counted as executor_executed. "
            "Mutant Q2 (no in_progress) returns 0."
        )


# ── W5: handler EMF namespace and metric name match the CDK source ─────────────

class TestMetricNameMatchesCdk:
    def test_handler_emf_namespace_and_name_match_cdk_source(self, capsys):
        """S2: _emit_run_completed()'s actual stdout namespace and metric name
        must match the CDK alarm's MetricStat (catches M7: namespace changed in
        _emit_run_completed to AVX/SimOperatorLive while _emit_emf still has
        AVX/SimOperator, making a source-grep test pass but breaking the alarm)."""
        import json as _json
        import os as _os

        # Capture the real metric record emitted by _emit_run_completed().
        h._emit_run_completed()
        out = capsys.readouterr().out.strip()
        record = _json.loads(out)
        cw = record["_aws"]["CloudWatchMetrics"][0]
        ns_from_handler = cw["Namespace"]
        metric_from_handler = cw["Metrics"][0]["Name"]

        # CDK staleness alarm must reference the same strings.
        cdk_src_path = _os.path.join(
            _os.path.dirname(__file__), "..", "..", "..",
            "infrastructure", "lib", "vsa-tier2-stack.ts",
        )
        with open(cdk_src_path) as fh:
            cdk_src = fh.read()
        assert f"'{ns_from_handler}'" in cdk_src, (
            f"_emit_run_completed namespace {ns_from_handler!r} not found in CDK source"
        )
        assert f"'{metric_from_handler}'" in cdk_src, (
            f"_emit_run_completed metric {metric_from_handler!r} not found in CDK source"
        )
