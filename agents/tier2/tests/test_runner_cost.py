"""The ``cost`` component's run (spec 2026-10-02-fi-cost-stage-b task 4.1).

Covers ``agents.fi_cost.agent.run_cost_loop`` and ``agents.tier2.runner.run_cost``:

* the cap counts diagnoses after the unchanged-skip, never selected rows: 120
  selected, cap 50 → 50 calls and 70 deferred; the next run reaches the next 50;
* a vehicle whose fleet moved is re-diagnosed although its cost inputs did not change,
  so the old fleet's projection does not outlive the move;
* the wall clock defers vehicles instead of overrunning the Lambda;
* sell timing (D10) writes without a model call and skips by hash;
* one run record per run, keyed ``agent-fi-cost#cost``, on every exit;
* a cost Finding whose owner is a driver is not pushed, through the recall stage's
  explicit FI skip (PO steering 03d5df5e; ``avx_broker._is_fi_finding``).

Bedrock and every AWS client are doubles; the repo-root guard blocks real calls.
"""
from __future__ import annotations

import copy
import datetime
import os
import sys
from typing import Any

import pytest

from agents.fi_cost import agent as cost_agent
from agents.fi_cost import evidence as evidence_mod
from agents.fi_cost import selection as selection_mod
from agents.fi_cost import sell_timing as sell_timing_mod
from agents.fi_cost.finding_keys import (
    AGENT_ID,
    FINDING_KIND_ANOMALY,
    FINDING_KIND_SELL_TIMING,
    cost_finding_key,
)
from agents.tier2 import runner
from agents.tier2.owner_resolver import StandingFacts

NOW = datetime.datetime(2026, 10, 6, 4, 0, tzinfo=datetime.timezone.utc)
MONTH = "2026-09"
FLEET_A = "fleet-meridian-delivery-a"
FLEET_B = "fleet-meridian-delivery-b"


def _vin(n: int) -> str:
    return f"MRDCST{n:011d}"  # 17 characters, no I/O/Q


def _evidence(vin: str, *, cost_per_mile: float = 0.45) -> dict[str, Any]:
    """A bundle the charging_rate reply below validates against (test_idempotency's shape)."""
    return {
        "vin": vin,
        "fleet_id": FLEET_A,
        "year_month": MONTH,
        "cost.month": MONTH,
        "ratio_cohort": 1.7,
        "ratio_own": 1.4,
        "severity": "soon",
        "cost.cost_per_mile": cost_per_mile,
        "cost.own_baseline": 0.30,
        "cost.cohort_baseline": 0.28,
        "cost.flag": "total",
        "energy.public_and_peak_kwh": 400.0,
        "energy.mean_public_peak_price_per_kwh": 0.38,
        "energy.depot_off_peak_price_per_kwh": 0.09,
        "energy.mean_price_per_kwh": 0.35,
        "energy.cohort_mean_price_per_kwh": 0.15,
        "energy.public_share": 0.75,
        "energy.cohort_public_share": 0.30,
        "energy.peak_share": 0.40,
        "energy.cohort_peak_share": 0.15,
        "energy.dc_fast_share": 0.60,
        "energy.cohort_dc_fast_share": 0.25,
        "cost.maintenance": 200.0,
        "cost.lease": 600.0,
        "cost.insurance": 150.0,
        "cost.miles": 800.0,
        "cost.fleet_median_miles": 1000.0,
    }


class _Bedrock:
    """Converse returns a valid record_diagnosis reply; the guardrail never intervenes."""

    def __init__(self, *, fail: bool = False) -> None:
        self.converse_calls = 0
        self.guardrail_calls = 0
        self.fail = fail

    def converse(self, **_kw: Any) -> dict[str, Any]:
        self.converse_calls += 1
        if self.fail:
            raise RuntimeError("ThrottlingException")
        return {
            "output": {"message": {"content": [{"toolUse": {
                "toolUseId": "t1",
                "name": "record_diagnosis",
                "input": {
                    "cause": "charging_rate",
                    "action": "shift_to_depot_off_peak",
                    "evidence_refs": ["energy.public_and_peak_kwh"],
                    "headline": "High public charging",
                    "detail": "Shift charging to the depot off-peak window.",
                    "confidence": "high",
                },
            }}]}},
            "usage": {"inputTokens": 4000, "outputTokens": 400},
        }

    def apply_guardrail(self, **_kw: Any) -> dict[str, Any]:
        self.guardrail_calls += 1
        return {"action": "NONE"}


class _Store:
    """The AVX findings table at the two seams the loop uses (writer semantics, D2).

    Same hash and same standing → noop; a live row → update keeping ``finding_id``;
    otherwise put.  Standing is projected onto the stored item as the writer does.
    """

    def __init__(self) -> None:
        self.rows: dict[str, dict[str, Any]] = {}
        self.writes: list[tuple[dict[str, Any], Any]] = []

    def lookup_live(self, finding_key: str) -> dict[str, Any] | None:
        row = self.rows.get(finding_key)
        return copy.deepcopy(row) if row else None

    @staticmethod
    def _projection(standing: Any) -> dict[str, str]:
        if standing is None:
            return {}
        return {a: getattr(standing, a) for a in ("owner_id", "fleet_id", "vehicle_id")
                if getattr(standing, a, None)}

    def write(self, finding: dict[str, Any], standing: Any) -> dict[str, Any]:
        self.writes.append((copy.deepcopy(finding), standing))
        key = finding["finding_key"]
        live = self.rows.get(key)
        projection = self._projection(standing)
        if live is not None:
            persisted = {a: live[a] for a in ("owner_id", "fleet_id", "vehicle_id") if a in live}
            if live["inputs_hash"] == finding["inputs_hash"] and persisted == projection:
                return {"action": "noop", "finding_id": live["finding_id"]}
            row = {**finding, "finding_id": live["finding_id"], **projection}
            self.rows[key] = row
            return {"action": "update", "finding_id": row["finding_id"]}
        self.rows[key] = {**finding, **projection}
        return {"action": "put", "finding_id": finding["finding_id"]}


def _standing_for(fleet: str):
    def resolve(vin: str) -> StandingFacts:
        return StandingFacts(owner_id="CUST-0000AA01", fleet_id=fleet, vehicle_id=f"veh-{vin[-4:]}")
    return resolve


@pytest.fixture
def adp(monkeypatch):
    """Patch the four ADP reads; ``adp.selected`` / ``adp.sell_rows`` set the data."""

    class _Adp:
        month: str | None = MONTH
        selected: list[dict[str, Any]] = []
        evidence: dict[str, dict[str, Any]] = {}
        sell_rows: list[dict[str, Any]] = []
        calls: list[tuple[str, dict[str, Any]]] = []

    state = _Adp()
    state.selected, state.evidence, state.sell_rows, state.calls = [], {}, [], []

    def latest(athena_client, workgroup, results_s3_path, *, now, stage="staging", max_polls=None):
        state.calls.append(("latest", {"now": now, "max_polls": max_polls, "stage": stage}))
        return state.month

    def select(**kw):
        state.calls.append(("select", kw))
        return [dict(r) for r in state.selected]

    def gather(vins, year_month, athena_client, workgroup, results_s3_path, stage="staging",
               max_polls=None):
        state.calls.append(("gather", {"vins": list(vins), "year_month": year_month,
                                       "max_polls": max_polls}))
        return [copy.deepcopy(state.evidence[v]) for v in vins if v in state.evidence]

    def sell(athena_client, workgroup, results_s3_path, *, year_month, stage="staging",
             max_polls=None):
        state.calls.append(("sell", {"year_month": year_month, "max_polls": max_polls}))
        return [dict(r) for r in state.sell_rows]

    monkeypatch.setattr(selection_mod, "find_latest_complete_month", latest)
    monkeypatch.setattr(selection_mod, "select_anomalies", select)
    monkeypatch.setattr(evidence_mod, "gather_evidence", gather)
    monkeypatch.setattr(sell_timing_mod, "fetch_sell_timing_inputs", sell)
    return state


def _seed(adp, n: int) -> list[str]:
    vins = [_vin(i) for i in range(n)]
    adp.selected = [{"vin": v} for v in vins]
    adp.evidence = {v: _evidence(v) for v in vins}
    return vins


def _loop(store: _Store, bedrock: Any, *, cap: int = 50, remaining=lambda: 900.0,
          resolve=None) -> dict[str, Any]:
    return cost_agent.run_cost_loop(
        stage="staging",
        athena_client=object(),
        workgroup="cvx-staging-analytics",
        results_s3_path="s3://results/tier2/fi-cost/",
        bedrock_client=bedrock,
        write_finding=store.write,
        lookup_live=store.lookup_live,
        resolve_standing=resolve if resolve is not None else _standing_for(FLEET_A),
        max_diagnoses_per_run=cap,
        remaining_seconds=remaining,
        now=NOW,
    )


# ---------------------------------------------------------------------------
# The cap counts diagnoses after the skip (task 4.1, decisions 2026-10-05 item 6)
# ---------------------------------------------------------------------------


def test_cap_applies_after_the_unchanged_skip_and_later_runs_reach_the_rest(adp):
    vins = _seed(adp, 120)
    store, bedrock = _Store(), _Bedrock()

    first = _loop(store, bedrock)
    assert (first["diagnosed"], first["model_calls"], first["deferred"]) == (50, 50, 70)
    assert first["deferred_cap"] == 70 and first["duplicates_skipped"] == 0
    assert first["anomalies_written"] == 50
    # In excess order: the first 50 selected are the ones written.
    written = {f["scope"]["vin"] for f, _ in store.writes}
    assert written == set(vins[:50])

    second = _loop(store, _Bedrock())
    assert (second["duplicates_skipped"], second["diagnosed"], second["deferred"]) == (50, 50, 20)
    assert {f["scope"]["vin"] for f, _ in store.writes[50:]} == set(vins[50:100])

    third = _loop(store, _Bedrock())
    assert (third["duplicates_skipped"], third["diagnosed"], third["deferred"]) == (100, 20, 0)


def test_unchanged_vehicles_make_no_model_call_and_no_write(adp):
    _seed(adp, 5)
    store = _Store()
    _loop(store, _Bedrock())
    bedrock = _Bedrock()
    again = _loop(store, bedrock)
    assert bedrock.converse_calls == 0 and again["model_calls"] == 0
    assert again["duplicates_skipped"] == 5 and len(store.writes) == 5


def test_a_cap_of_zero_diagnoses_nothing(adp):
    _seed(adp, 3)
    stats = _loop(_Store(), _Bedrock(), cap=0)
    assert stats["diagnosed"] == 0 and stats["deferred"] == 3 and stats["model_calls"] == 0


@pytest.mark.parametrize("cap", [-1, 501, True, 1.5, "50"])
def test_an_out_of_range_cap_is_refused(adp, cap):
    _seed(adp, 1)
    with pytest.raises(ValueError):
        _loop(_Store(), _Bedrock(), cap=cap)


def test_a_retry_is_counted_as_a_second_model_call(adp):
    """model_calls is Converse calls made, not vehicles: a rejected first reply plus its
    retry is two calls for one diagnosis (D11's bound is two per diagnosis)."""
    _seed(adp, 1)

    class _OnceBad(_Bedrock):
        def converse(self, **kw):
            reply = super().converse(**kw)
            if self.converse_calls == 1:
                reply["output"]["message"]["content"][0]["toolUse"]["input"]["cause"] = "nonsense"
            return reply

    stats = _loop(_Store(), _OnceBad())
    assert stats["diagnosed"] == 1 and stats["model_calls"] == 2
    assert stats["tokens_used"] == 2 * 4400
    assert stats["cost_usd"] == pytest.approx(2 * (4000 * 3 + 400 * 15) / 1_000_000)


def test_a_model_outage_writes_rules_findings_and_the_run_completes(adp):
    _seed(adp, 2)
    store = _Store()
    stats = _loop(store, _Bedrock(fail=True))
    assert stats["rules_fallbacks"] == 2 and stats["anomalies_written"] == 2
    assert stats["write_failures"] == 0 and stats["model_calls"] == 4


# ---------------------------------------------------------------------------
# Standing: a fleet move with unchanged cost inputs still converges
# ---------------------------------------------------------------------------


def test_a_fleet_move_with_unchanged_inputs_is_rewritten_with_the_new_fleet(adp):
    (vin,) = _seed(adp, 1)
    store = _Store()
    _loop(store, _Bedrock(), resolve=_standing_for(FLEET_A))
    key = cost_finding_key(vin, FINDING_KIND_ANOMALY, MONTH)
    first_id = store.rows[key]["finding_id"]

    bedrock = _Bedrock()
    stats = _loop(store, bedrock, resolve=_standing_for(FLEET_B))
    assert stats["duplicates_skipped"] == 0 and bedrock.converse_calls == 1
    assert store.rows[key]["fleet_id"] == FLEET_B
    assert store.rows[key]["finding_id"] == first_id   # updated in place, decision kept


def test_a_standing_change_between_the_two_reads_projects_nothing(adp):
    """F11.2 Accept 4 for cost: an owner that moves within one vehicle's write is not
    persisted; the item goes to no queue (fail closed) and the run counts it."""
    _seed(adp, 1)
    reads = iter([FLEET_A, FLEET_B])

    def unstable(vin):
        return StandingFacts(owner_id="CUST-0000AA01", fleet_id=next(reads), vehicle_id="veh-1")

    store = _Store()
    stats = _loop(store, _Bedrock(), resolve=unstable)
    assert stats["owner_integrity_degraded"] == 1
    (_, standing), = store.writes
    assert standing is None


# ---------------------------------------------------------------------------
# Wall clock and malformed data
# ---------------------------------------------------------------------------


def test_the_wall_clock_defers_vehicles_instead_of_starting_them(adp):
    _seed(adp, 4)
    budget = iter([900.0] * 4 + [500.0, 500.0, 100.0, 100.0] + [10.0] * 50)
    stats = _loop(_Store(), _Bedrock(), remaining=lambda: next(budget))
    assert stats["diagnosed"] == 2 and stats["deferred_wall_time"] == 2
    assert stats["deferred"] == 2


def test_no_wall_time_for_the_reads_aborts_the_run(adp):
    _seed(adp, 1)
    with pytest.raises(cost_agent.CostRunDeadlineExceeded):
        _loop(_Store(), _Bedrock(), remaining=lambda: cost_agent.FINAL_RESERVE_S)


def test_each_read_is_given_the_time_left_minus_the_final_reserve(adp):
    _seed(adp, 1)
    _loop(_Store(), _Bedrock(), remaining=lambda: 400.0)
    polls = {name: kw["max_polls"] for name, kw in adp.calls}
    assert polls == {name: int(400.0 - cost_agent.FINAL_RESERVE_S) for name in
                     ("latest", "select", "gather", "sell")}


def test_a_malformed_vin_is_counted_and_does_not_abort_the_run(adp):
    vins = _seed(adp, 2)
    adp.selected.insert(0, {"vin": "MRD'; DROP TABLE x"})
    stats = _loop(_Store(), _Bedrock())
    assert stats["invalid_vins"] == 1 and stats["anomalies_written"] == 2
    (_, gather_kw), = [c for c in adp.calls if c[0] == "gather"]
    assert gather_kw["vins"] == vins


def test_no_published_month_reads_nothing_else_and_says_so(adp):
    _seed(adp, 2)
    adp.month = None
    stats = _loop(_Store(), _Bedrock())
    assert stats["data_month"] is None and [c[0] for c in adp.calls] == ["latest"]


def test_a_vehicle_without_evidence_is_counted_not_diagnosed(adp):
    vins = _seed(adp, 2)
    del adp.evidence[vins[0]]
    stats = _loop(_Store(), _Bedrock())
    assert stats["evidence_missing"] == 1 and stats["diagnosed"] == 1


def test_one_failed_write_is_counted_and_the_others_are_written(adp):
    _seed(adp, 3)
    store = _Store()
    real = store.write

    def flaky(finding, standing):
        if finding["scope"]["vin"] == _vin(1):
            raise RuntimeError("ProvisionedThroughputExceededException")
        return real(finding, standing)

    store.write = flaky
    stats = _loop(store, _Bedrock())
    assert stats["write_failures"] == 1 and stats["anomalies_written"] == 2


def test_every_written_cost_finding_validates_and_carries_the_data_month(adp):
    _seed(adp, 2)
    store = _Store()
    _loop(store, _Bedrock())
    from agents.tier2.finding_transform import _validate_finding

    for finding, _ in store.writes:
        _validate_finding(finding)
        assert finding["subject_key"] == MONTH and finding["agent_id"] == AGENT_ID
        assert finding["finding_key"] == cost_finding_key(
            finding["scope"]["vin"], FINDING_KIND_ANOMALY, MONTH)
        assert finding["expires_at"] == "2026-11-10T04:00:00Z"   # 35 days (D8)


# ---------------------------------------------------------------------------
# Sell timing (D10)
# ---------------------------------------------------------------------------


def _rising_series(vin: str, price: float = 36000.0) -> list[dict[str, Any]]:
    """Maintenance rising past the 300/month straight-line depreciation within 3 months."""
    months = ["2026-05", "2026-06", "2026-07", "2026-08", MONTH]
    costs = [100.0, 150.0, 200.0, 250.0, 290.0]
    return [{"vin": vin, "fleet_id": FLEET_A, "year_month": m, "maintenance_cost": str(c),
             "purchase_price_usd": str(price)} for m, c in zip(months, costs)]


def test_sell_timing_is_written_without_a_model_call_then_skipped(adp):
    vin = _vin(900)
    adp.sell_rows = _rising_series(vin)
    store, bedrock = _Store(), _Bedrock()
    first = _loop(store, bedrock)
    assert first["sell_timing_candidates"] == 1 and first["sell_timing_written"] == 1
    assert bedrock.converse_calls == 0
    (finding, standing), = store.writes
    assert finding["finding_kind"] == FINDING_KIND_SELL_TIMING and finding["subject_key"] == MONTH
    assert standing.fleet_id == FLEET_A

    again = _loop(store, _Bedrock())
    assert again["sell_timing_skipped"] == 1 and len(store.writes) == 1


def test_a_vehicle_whose_series_stops_before_the_data_month_gets_no_sell_timing(adp):
    adp.sell_rows = [r for r in _rising_series(_vin(901)) if r["year_month"] != MONTH]
    stats = _loop(_Store(), _Bedrock())
    assert stats["sell_timing_candidates"] == 0


def test_an_unreadable_price_drops_the_vehicle_and_is_counted(adp):
    rows = _rising_series(_vin(902))
    rows[2]["purchase_price_usd"] = "not-a-number"
    adp.sell_rows = rows
    stats = _loop(_Store(), _Bedrock())
    assert stats["sell_timing_candidates"] == 0 and stats["sell_timing_rejected_vins"] == 1


def test_plan_sell_timing_matches_the_ported_crossover():
    rows = _rising_series(_vin(903))
    candidates, rejected = sell_timing_mod.plan_sell_timing(rows, year_month=MONTH)
    series = [{"yearMonth": r["year_month"], "maintenanceCost": float(r["maintenance_cost"])}
              for r in rows]
    expected = sell_timing_mod.compute_crossover_month(36000.0, series, 3)
    assert rejected == 0 and candidates[0]["crossover_month"] == expected["crossover_month"]
    assert candidates[0]["monthly_depreciation"] == 300.0


# ---------------------------------------------------------------------------
# run_cost: run records (contract § 8) and the end-of-run error
# ---------------------------------------------------------------------------


class _Writer:
    """An avx_writer for run_cost: table_name/dynamodb_client for the live lookup."""

    table_name = "vsa-staging-avx-findings"

    def __init__(self) -> None:
        self.dynamodb_client = self
        self.findings: list[dict[str, Any]] = []

    def query(self, **_kw):
        return {"Items": []}

    def write_finding(self, finding, *, standing=None):
        self.findings.append(finding)
        return {"action": "put", "finding_id": finding["finding_id"]}


def _run_cost(records: list, *, writer=None, remaining=lambda: 900.0):
    return runner.run_cost(
        "staging",
        athena_client=object(),
        workgroup="cvx-staging-analytics",
        results_s3_path="s3://results/tier2/fi-cost/",
        bedrock_client=_Bedrock(),
        avx_writer=writer or _Writer(),
        max_diagnoses_per_run=50,
        remaining_seconds=remaining,
        owner_resolver=_standing_for(FLEET_A),
        run_record_writer=lambda **kw: records.append(kw),
        now=NOW,
    )


def test_a_completed_run_writes_one_record_keyed_agent_fi_cost_cost(adp):
    _seed(adp, 3)
    records: list = []
    summary = _run_cost(records)
    (rec,) = records
    assert (rec["agent_id"], rec["component"], rec["status"]) == ("agent-fi-cost", "cost", "completed")
    assert rec["artifacts_written"] == 3 and rec["model_calls"] == 3 and rec["deferred"] == 0
    assert rec["data_month"] == MONTH and rec["vins_selected"] == 3
    assert rec["agent_version"] == cost_agent.AGENT_VERSION
    assert summary["status"] == "completed" and summary["cost"]["diagnosed"] == 3


def test_an_athena_failure_records_an_aborted_run_and_raises(adp, monkeypatch, capsys):
    def boom(**_kw):
        raise RuntimeError("AthenaCursorError for MRDCST00000000001")

    monkeypatch.setattr(selection_mod, "select_anomalies", boom)
    records: list = []
    with pytest.raises(RuntimeError):
        _run_cost(records)
    (rec,) = records
    assert (rec["agent_id"], rec["status"]) == ("agent-fi-cost", "aborted")
    # The reason carries the exception's type, never its text: input-validation errors
    # name a VIN, and the run's JSON log line (RunObserver) prints the reason.
    logged = capsys.readouterr().out
    assert '"abort_reason": "RuntimeError"' in logged
    assert "MRDCST" not in repr(rec) and "MRDCST" not in logged


def test_failed_writes_record_an_aborted_run_then_raise(adp):
    _seed(adp, 2)

    class _Failing(_Writer):
        def write_finding(self, finding, *, standing=None):
            raise RuntimeError("AccessDeniedException")

    records: list = []
    with pytest.raises(runner.CostRunError):
        _run_cost(records, writer=_Failing())
    (rec,) = records
    assert rec["status"] == "aborted" and rec["write_failures"] == 2


def test_the_runner_agent_id_is_the_cost_agents_id():
    assert runner.FI_COST_AGENT_ID == AGENT_ID


def test_lambda_handler_dispatches_cost(monkeypatch):
    seen = {}

    def fake(stage, event, context):
        seen.update(stage=stage, event=event, context=context)
        return {"ok": 1}

    monkeypatch.setattr(runner, "_run_cost_from_env", fake)
    ctx = object()
    assert runner.lambda_handler({"stage": "staging", "component": "cost"}, ctx) == {"ok": 1}
    assert seen["stage"] == "staging" and seen["context"] is ctx


def test_the_cost_run_record_reaches_dynamodb_through_the_real_service_model():
    """cost_usd must be a Decimal for the DynamoDB resource; Stubber validates the call."""
    import boto3
    from botocore.stub import ANY, Stubber

    from agents.tier2.run_records import write_run_record

    ddb = boto3.resource("dynamodb", region_name="us-west-2",
                         aws_access_key_id="x", aws_secret_access_key="x")
    table = ddb.Table("vsa-staging-avx-agent-runs")
    with Stubber(table.meta.client) as stub:
        stub.add_response("put_item", {}, {"TableName": "vsa-staging-avx-agent-runs",
                                            "Item": ANY,
                                            "ConditionExpression": "attribute_not_exists(agent_component)"})
        assert write_run_record(
            agent_id="agent-fi-cost", component="cost", started_at="2026-10-06T04:00:00Z",
            finished_at="2026-10-06T04:05:00Z", status="completed", artifacts_written=3,
            duplicates_skipped=1, agent_version="1.0.0", ddb_client=ddb,
            table_name="vsa-staging-avx-agent-runs", data_month=MONTH, vins_selected=4,
            model_calls=3, deferred=0, tokens_used=13200, cost_usd=0.054, write_failures=0,
        ) is True
        stub.assert_no_pending_responses()


# ---------------------------------------------------------------------------
# max_diagnoses_per_run: one-run override, policy value, default
# ---------------------------------------------------------------------------


class _Ssm:
    def __init__(self, value=None, error=None):
        self.value, self.error, self.names = value, error, []

    def get_parameter(self, Name):  # noqa: N803 — boto3 keyword
        self.names.append(Name)
        if self.error:
            raise self.error
        return {"Parameter": {"Value": self.value}}


def test_the_policy_value_is_read_from_the_stages_parameter():
    ssm = _Ssm("80")
    assert runner.resolve_max_diagnoses_per_run("staging", {}, ssm) == 80
    assert ssm.names == ["/vsa/staging/avx/fi-cost/max-diagnoses-per-run"]


def test_a_one_run_override_wins_and_a_bad_one_is_refused():
    assert runner.resolve_max_diagnoses_per_run("staging", {"max_diagnoses_per_run": 200}, _Ssm("80")) == 200
    for bad in (501, -1, "lots", True):
        with pytest.raises(ValueError):
            runner.resolve_max_diagnoses_per_run("staging", {"max_diagnoses_per_run": bad}, _Ssm("80"))


def test_an_absent_or_malformed_policy_value_uses_the_default():
    default = cost_agent.DEFAULT_MAX_DIAGNOSES_PER_RUN
    assert runner.resolve_max_diagnoses_per_run("staging", {}, _Ssm(error=RuntimeError("ParameterNotFound"))) == default
    assert runner.resolve_max_diagnoses_per_run("staging", {}, _Ssm("9999")) == default


def test_the_policy_defaults_file_carries_the_cap_at_its_default():
    import json

    path = os.path.join(os.path.dirname(__file__), "..", "..", "..",
                        "deployment", "config", "avx-policy-defaults.json")
    with open(path) as fh:
        params = json.load(fh)["parameters"]
    (entry,) = [p for p in params if p["_key_template"] == runner.FI_COST_MAX_DIAGNOSES_PARAM]
    assert entry["staging_default"] == cost_agent.DEFAULT_MAX_DIAGNOSES_PER_RUN == 50


# ---------------------------------------------------------------------------
# No push to a driver's phone (PO steering 03d5df5e; the recall stage's FI skip)
# ---------------------------------------------------------------------------

_DRIVER_SUB = "aabbccdd-1122-3344-5566-778899aabbcc"   # a Cognito sub, not a CUST- id


def _broker():
    lambdas_dir = os.path.abspath(os.path.join(
        os.path.dirname(__file__), "..", "..", "supervisor", "lambdas"))
    if lambdas_dir not in sys.path:
        sys.path.insert(0, lambdas_dir)
    import avx_broker

    return avx_broker


class _CapturingDdb:
    """Low-level DynamoDB double for the real writer: no live rows; records PutItem."""

    def __init__(self) -> None:
        self.items: list[dict[str, Any]] = []

    def query(self, **_kw):
        return {"Items": []}

    def put_item(self, **kw):
        self.items.append(kw["Item"])
        return {}


def test_a_cost_finding_owned_by_a_driver_is_not_pushed(adp):
    """Both cost kinds, as persisted by the real writer with a driver as owner, reach
    ``_process_finding`` and stop at the FI skip, before any owner gate.

    What this exercises is ``avx_broker._is_fi_finding`` (recall Stage C task 1.8), not
    the ``CUST-`` namespace check: the owner here is a driver's Cognito sub, which that
    check would let through.  Remove the skip and this test fails (decisions.md records
    the mutation).
    """
    from agents.tier2.artifact_writer import ArtifactWriter
    from agents.tier2.writer import _from_ddb_item

    _seed(adp, 1)
    adp.sell_rows = _rising_series(_vin(950))
    ddb = _CapturingDdb()
    writer = ArtifactWriter(table_name="vsa-staging-avx-findings", dynamodb_client=ddb)

    def driver_owned(vin):
        return StandingFacts(owner_id=_DRIVER_SUB, fleet_id=FLEET_A, vehicle_id="veh-1")

    runner.run_cost(
        "staging", athena_client=object(), workgroup="wg", results_s3_path="s3://r/",
        bedrock_client=_Bedrock(), avx_writer=writer, max_diagnoses_per_run=50,
        remaining_seconds=lambda: 900.0, owner_resolver=driver_owned,
        run_record_writer=lambda **_kw: None, now=NOW,
    )
    persisted = [_from_ddb_item(item) for item in ddb.items]
    assert {p["finding_kind"] for p in persisted} == {FINDING_KIND_ANOMALY, FINDING_KIND_SELL_TIMING}

    broker = _broker()
    policy = {"apns_threshold": "routine", "rate_limit": {"day": 10, "hour": 3}, "cardinality_cap": 15}
    for item in persisted:
        assert item["owner_id"] == _DRIVER_SUB          # the case the steering names
        decision = broker._process_finding(item, policy)
        assert (decision["action"], decision["skip_reason"]) == ("skipped", "fi_item"), item["finding_kind"]



# ---------------------------------------------------------------------------
# Fix Group G4-1 (Group 4 review cycle 1, run area)
# ---------------------------------------------------------------------------


def test_changed_evidence_with_unchanged_standing_is_rediagnosed(adp):
    """The hash half of 'unchanged' (cycle 1 W4, mutant R1)."""
    (vin,) = _seed(adp, 1)
    store = _Store()
    _loop(store, _Bedrock())
    adp.evidence[vin] = _evidence(vin, cost_per_mile=0.60)
    bedrock = _Bedrock()
    stats = _loop(store, bedrock)
    assert stats["duplicates_skipped"] == 0 and bedrock.converse_calls == 1
    key = cost_finding_key(vin, FINDING_KIND_ANOMALY, MONTH)
    assert store.rows[key]["inputs_hash"] == store.writes[-1][0]["inputs_hash"]


def test_the_run_record_carries_the_deferred_and_rules_fallback_counts(adp):
    """Cycle 1 W5 (mutant R2)."""
    _seed(adp, 3)
    records: list = []
    runner.run_cost(
        "staging", athena_client=object(), workgroup="wg", results_s3_path="s3://r/",
        bedrock_client=_Bedrock(fail=True), avx_writer=_Writer(), max_diagnoses_per_run=1,
        remaining_seconds=lambda: 900.0, owner_resolver=_standing_for(FLEET_A),
        run_record_writer=lambda **kw: records.append(kw), now=NOW,
    )
    (rec,) = records
    assert (rec["deferred"], rec["rules_fallbacks"], rec["model_calls"]) == (2, 1, 2)


def test_a_series_ending_before_the_data_month_gets_no_sell_timing(adp):
    """Cycle 1 W6 (mutant R3): this series would cross within 3 months of the data month
    (2026-11 from an 2026-08 end), so only the 'still managed' check rejects it."""
    vin = _vin(904)
    months = ["2026-04", "2026-05", "2026-06", "2026-07", "2026-08"]
    costs = [100.0, 130.0, 160.0, 190.0, 220.0]
    adp.sell_rows = [{"vin": vin, "fleet_id": FLEET_A, "year_month": m, "maintenance_cost": str(c),
                      "purchase_price_usd": "36000"} for m, c in zip(months, costs)]
    stats = _loop(_Store(), _Bedrock())
    assert stats["sell_timing_candidates"] == 0


def test_a_sell_timing_fleet_move_is_rewritten_with_the_new_fleet(adp):
    """Cycle 1 W7 (mutant R4)."""
    vin = _vin(905)
    adp.sell_rows = _rising_series(vin)
    store = _Store()
    _loop(store, _Bedrock(), resolve=_standing_for(FLEET_A))
    stats = _loop(store, _Bedrock(), resolve=_standing_for(FLEET_B))
    key = cost_finding_key(vin, FINDING_KIND_SELL_TIMING, MONTH)
    assert stats["sell_timing_written"] == 1 and stats["sell_timing_skipped"] == 0
    assert store.rows[key]["fleet_id"] == FLEET_B


def test_the_cap_takes_vehicles_in_selection_order_not_vin_order(adp):
    """Cycle 1 W8 (mutant R5): selection order is excess order."""
    vins = _seed(adp, 3)
    adp.selected = list(reversed(adp.selected))
    store = _Store()
    _loop(store, _Bedrock(), cap=1)
    (finding, _), = store.writes
    assert finding["scope"]["vin"] == vins[-1]


def test_failed_diagnoses_count_against_the_cap(adp):
    """Cycle 1 W9 (mutant R6). Writes fail on alternate vehicles, so the write breaker
    never trips and only the cap can stop the run."""
    vins = _seed(adp, 8)
    store = _Store()
    real = store.write

    def every_other(finding, standing):
        if vins.index(finding["scope"]["vin"]) % 2 == 0:
            raise RuntimeError("AccessDeniedException")
        return real(finding, standing)

    store.write = every_other
    bedrock = _Bedrock()
    stats = _loop(store, bedrock, cap=4)
    assert stats["diagnosed"] == 4 and bedrock.converse_calls == 4
    assert stats["write_failures"] == 2 and stats["deferred_cap"] == 4


def test_a_write_path_that_always_fails_stops_diagnosing(adp):
    """A missing grant must not pay for a full cap of diagnoses that are never stored."""
    _seed(adp, 10)
    store = _Store()

    def failing(finding, standing):
        raise RuntimeError("AccessDeniedException")

    store.write = failing
    bedrock = _Bedrock()
    stats = _loop(store, bedrock, cap=50)
    n = cost_agent.MAX_CONSECUTIVE_WRITE_FAILURES
    assert stats["diagnosed"] == n and bedrock.converse_calls == n
    assert stats["deferred_write_failures"] == 10 - n and stats["deferred"] == 10 - n


def test_the_write_breaker_counts_consecutive_failures_only(adp):
    vins = _seed(adp, 7)
    store = _Store()
    real = store.write
    failing = {vins[0], vins[1], vins[3], vins[4], vins[5]}

    def sometimes(finding, standing):
        if finding["scope"]["vin"] in failing:
            raise RuntimeError("ThrottlingException")
        return real(finding, standing)

    store.write = sometimes
    stats = _loop(store, _Bedrock(), cap=50)
    # 0,1 fail, 2 succeeds (reset), 3,4,5 fail (trip), 6 deferred.
    assert stats["diagnosed"] == 6 and stats["deferred_write_failures"] == 1


class _Clock:
    def __init__(self, lambda_timeout_s: float = 900.0) -> None:
        self.t = 0.0
        self.timeout = lambda_timeout_s

    def remaining(self) -> float:
        return self.timeout - self.t


def test_an_athena_query_that_never_finishes_aborts_with_the_final_reserve_left(monkeypatch):
    """Cycle 1 W2: each poll's round trip is real time, not just the sleep."""
    import agents.tier2.adp_source as adp_source

    clock = _Clock()
    monkeypatch.setattr(adp_source.time, "sleep", lambda s: setattr(clock, "t", clock.t + s))

    class _Queued:
        def start_query_execution(self, **_kw):
            return {"QueryExecutionId": "q1"}

        def get_query_execution(self, **_kw):
            clock.t += 0.1   # us-west-2 -> us-east-1
            return {"QueryExecution": {"Status": {"State": "QUEUED"}}}

    with pytest.raises(cost_agent.CostRunDeadlineExceeded):
        cost_agent.run_cost_loop(
            stage="staging", athena_client=_Queued(), workgroup="wg", results_s3_path="s3://r/",
            bedrock_client=_Bedrock(), write_finding=lambda f, s: {}, lookup_live=lambda k: None,
            resolve_standing=None, max_diagnoses_per_run=50, remaining_seconds=clock.remaining,
            now=NOW,
        )
    # At most one sleep and one round trip past the check.
    assert clock.remaining() >= cost_agent.FINAL_RESERVE_S - 1.2, clock.remaining()


def test_result_paging_is_bounded_by_the_clock_too(monkeypatch):
    import agents.tier2.adp_source as adp_source

    clock = _Clock(lambda_timeout_s=cost_agent.FINAL_RESERVE_S + 10)
    monkeypatch.setattr(adp_source.time, "sleep", lambda s: None)

    class _SlowPages:
        def start_query_execution(self, **_kw):
            return {"QueryExecutionId": "q1"}

        def get_query_execution(self, **_kw):
            return {"QueryExecution": {"Status": {"State": "SUCCEEDED"}}}

        def get_query_results(self, **kw):
            clock.t += 3.0
            header = [{"Data": [{"VarCharValue": "year_month"}]}] if "NextToken" not in kw else []
            return {"ResultSet": {"Rows": header}, "NextToken": "more"}

    with pytest.raises(cost_agent.CostRunDeadlineExceeded):
        cost_agent.run_cost_loop(
            stage="staging", athena_client=_SlowPages(), workgroup="wg", results_s3_path="s3://r/",
            bedrock_client=_Bedrock(), write_finding=lambda f, s: {}, lookup_live=lambda k: None,
            resolve_standing=None, max_diagnoses_per_run=50, remaining_seconds=clock.remaining,
            now=NOW,
        )
    assert clock.remaining() >= cost_agent.FINAL_RESERVE_S - 3.0


def test_one_diagnosis_fits_inside_its_reserve_at_the_client_timeouts():
    """Cycle 1 W3: every Bedrock call takes the read plus connect timeout (35 s) and the
    guardrail fails, so both attempts make both calls; the run record still fits."""
    clock = _Clock(lambda_timeout_s=cost_agent.FINAL_RESERVE_S + cost_agent.DIAGNOSIS_RESERVE_S)
    per_call = runner._FI_COST_BEDROCK_READ_TIMEOUT_S + runner._FI_COST_BEDROCK_CONNECT_TIMEOUT_S

    class _Slow:
        def converse(self, **_kw):
            clock.t += per_call
            return _Bedrock().converse()

        def apply_guardrail(self, **_kw):
            clock.t += per_call
            raise TimeoutError("Read timeout on endpoint URL")

    cost_agent.process_vehicle(
        evidence=_evidence(_vin(1)), live_finding=None, bedrock_client=_Slow(),
        write_finding=lambda f: None,
    )
    assert clock.t == 4 * per_call
    assert clock.remaining() >= cost_agent.FINAL_RESERVE_S, clock.remaining()


def test_an_aborted_record_carries_what_ran_before_the_abort(adp, monkeypatch):
    _seed(adp, 2)

    def boom(*_a, **_kw):
        raise RuntimeError("AthenaCursorError")

    monkeypatch.setattr(sell_timing_mod, "fetch_sell_timing_inputs", boom)
    records: list = []
    with pytest.raises(RuntimeError):
        _run_cost(records)
    (rec,) = records
    assert rec["status"] == "aborted" and rec["data_month"] == MONTH
    assert rec["vins_selected"] == 2 and rec["model_calls"] == 0


class _Context:
    def __init__(self, ms: int) -> None:
        self.ms = ms

    def get_remaining_time_in_millis(self) -> int:
        return self.ms


def _from_env(monkeypatch, context) -> dict[str, Any]:
    seen: dict[str, Any] = {}
    monkeypatch.setenv("FI_COST_GUARDRAIL_ID", "gr-test")
    monkeypatch.setenv("FI_COST_GUARDRAIL_VERSION", "DRAFT")
    monkeypatch.setenv("ATHENA_RESULTS_BUCKET", "cvx-staging-athena-results-x-us-east-1")
    monkeypatch.setattr(runner, "resolve_max_diagnoses_per_run", lambda *a: 50)
    monkeypatch.setattr(runner, "run_cost", lambda stage, **kw: seen.update(kw) or {})
    runner._run_cost_from_env("staging", {}, context)
    return seen


def test_the_cost_runs_clock_is_the_tier2_wall_budget_when_the_lambda_has_longer(monkeypatch):
    """Cycle 1 infra W5: a run that respects TIER2_MAX_WALL_SECONDS never pages the shared
    600 s run-duration alarm; the Lambda's own remaining time still wins when shorter."""
    long_run = _from_env(monkeypatch, _Context(900_000))["remaining_seconds"]()
    assert long_run <= runner._DEFAULT_MAX_WALL_SECONDS
    assert long_run > runner._DEFAULT_MAX_WALL_SECONDS - 5
    short_run = _from_env(monkeypatch, _Context(100_000))["remaining_seconds"]()
    assert short_run == pytest.approx(100.0)


def test_the_cost_run_refuses_to_start_without_its_guardrail(monkeypatch):
    monkeypatch.delenv("FI_COST_GUARDRAIL_ID", raising=False)
    monkeypatch.setattr(runner, "run_cost", lambda stage, **kw: pytest.fail("ran"))
    with pytest.raises(RuntimeError, match="FI_COST_GUARDRAIL_ID"):
        runner._run_cost_from_env("staging", {}, None)


def test_the_smoke_test_never_invokes_the_cost_component():
    """scripts/smoke-test-tier2.py invokes each component it lists; a cost invoke is a
    model-billed run with no health artifact (Group 4 review cycle 1 suggestion)."""
    import importlib.util

    path = os.path.join(os.path.dirname(__file__), "..", "..", "..", "scripts", "smoke-test-tier2.py")
    spec = importlib.util.spec_from_file_location("smoke_test_tier2", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert "cost" not in module.COMPONENTS and "battery" in module.COMPONENTS



# ---------------------------------------------------------------------------
# Fix Group G4-2 (Group 4 review cycle 2)
# ---------------------------------------------------------------------------


def _production_bedrock_client(monkeypatch) -> Any:
    """The bedrock-runtime client _run_cost_from_env builds (constructed, never called)."""
    seen: dict[str, Any] = {}
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIDEXAMPLE")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "x" * 40)
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    monkeypatch.setenv("FI_COST_GUARDRAIL_ID", "gr-test")
    monkeypatch.setenv("FI_COST_GUARDRAIL_VERSION", "DRAFT")
    monkeypatch.setenv("ATHENA_RESULTS_BUCKET", "cvx-staging-athena-results-x-us-east-1")
    monkeypatch.setattr(runner, "resolve_max_diagnoses_per_run", lambda *a: 50)
    monkeypatch.setattr(runner, "run_cost", lambda stage, **kw: seen.update(kw) or {})
    runner._run_cost_from_env("staging", {}, None)
    return seen["bedrock_client"]


def test_the_cost_runs_bedrock_client_makes_one_attempt_per_call(monkeypatch):
    """Cycle 2 W1: botocore's max_attempts counts retries; total_max_attempts is what a
    request gets. One attempt per call: the agent's own retry is the retry policy."""
    retries = _production_bedrock_client(monkeypatch).meta.config.retries
    assert retries["total_max_attempts"] == 1, retries
    assert "max_attempts" not in retries, retries


def test_the_diagnosis_reserve_covers_the_clients_real_attempt_count(monkeypatch):
    client = _production_bedrock_client(monkeypatch)
    attempts = client.meta.config.retries["total_max_attempts"]
    assert client.meta.config.read_timeout == runner._FI_COST_BEDROCK_READ_TIMEOUT_S
    assert client.meta.config.connect_timeout == runner._FI_COST_BEDROCK_CONNECT_TIMEOUT_S
    per_attempt = runner._FI_COST_BEDROCK_READ_TIMEOUT_S + runner._FI_COST_BEDROCK_CONNECT_TIMEOUT_S
    per_call = attempts * per_attempt + (attempts - 1) * 1.0   # standard-mode backoff
    assert 4 * per_call < cost_agent.DIAGNOSIS_RESERVE_S


def test_an_abort_after_model_calls_records_the_calls_and_findings_already_done(adp, monkeypatch):
    """Cycle 2 W2: the aborted record carries the calls already billed and the Findings
    already written, not zeros."""
    vins = _seed(adp, 2)
    real = cost_agent.compute_inputs_hash

    def raising_on_second(evidence):
        if evidence.get("vin") == vins[1]:
            raise TypeError("unhashable evidence")   # outside the per-vehicle try
        return real(evidence)

    monkeypatch.setattr(cost_agent, "compute_inputs_hash", raising_on_second)
    records: list = []
    with pytest.raises(TypeError):
        _run_cost(records)
    (rec,) = records
    assert rec["status"] == "aborted"
    assert (rec["model_calls"], rec["tokens_used"], rec["artifacts_written"]) == (1, 4400, 1), rec


def test_a_model_reply_is_not_counted_as_a_rules_fallback(adp):
    """Cycle 2 W3: the rules_fallbacks tests all used a failing model."""
    _seed(adp, 2)
    records: list = []
    _run_cost(records)
    (rec,) = records
    assert (rec["rules_fallbacks"], rec["guardrail_interventions"], rec["model_calls"]) == (0, 0, 2)


def test_guardrail_interventions_are_counted_apart_from_model_failures(adp):
    _seed(adp, 1)

    class _Blocking(_Bedrock):
        def apply_guardrail(self, **_kw):
            self.guardrail_calls += 1
            return {"action": "GUARDRAIL_INTERVENED"}

    stats = _loop(_Store(), _Blocking())
    assert (stats["guardrail_interventions"], stats["rules_fallbacks"], stats["model_calls"]) == (2, 1, 2)


def test_sell_timing_defers_when_the_wall_clock_is_short(adp):
    """Cycle 2 W4."""
    adp.sell_rows = _rising_series(_vin(906))
    store = _Store()
    stats = _loop(store, _Bedrock(), remaining=lambda: cost_agent.FINAL_RESERVE_S + 1.0)
    assert stats["sell_timing_candidates"] == 1
    assert (stats["sell_timing_deferred"], stats["sell_timing_written"]) == (1, 0)
    assert store.writes == []



# ---------------------------------------------------------------------------
# Group 4 review cycle 3 (test gaps; not re-reviewed, mutation-backed)
# ---------------------------------------------------------------------------


class _BlockingBedrock(_Bedrock):
    def apply_guardrail(self, **_kw):
        self.guardrail_calls += 1
        return {"action": "GUARDRAIL_INTERVENED"}


def test_the_run_record_carries_the_guardrail_interventions(adp):
    """Cycle 3 run W1 (mutant N5: a hardwired 0 in the record)."""
    _seed(adp, 1)
    records: list = []
    runner.run_cost(
        "staging", athena_client=object(), workgroup="wg", results_s3_path="s3://r/",
        bedrock_client=_BlockingBedrock(), avx_writer=_Writer(), max_diagnoses_per_run=50,
        remaining_seconds=lambda: 900.0, owner_resolver=_standing_for(FLEET_A),
        run_record_writer=lambda **kw: records.append(kw), now=NOW,
    )
    (rec,) = records
    assert (rec["guardrail_interventions"], rec["rules_fallbacks"], rec["model_calls"]) == (2, 1, 2)


class _RunsTable:
    def __init__(self) -> None:
        self.items: list = []

    def put_item(self, Item, **kwargs):  # noqa: N803 — boto3 keyword
        self.items.append(Item)


class _RunsResource:
    def __init__(self) -> None:
        self.table = _RunsTable()

    def Table(self, name):  # noqa: N802 — boto3 method name
        return self.table


def test_the_stored_run_record_carries_every_cost_counter():
    """Cycle 3 run W1 (mutant N6: a counter dropped from the stored item). 5.3 items 3 and
    7 read model_calls and tokens_used from the stored record, not from kwargs."""
    from agents.tier2.run_records import write_run_record

    res = _RunsResource()
    assert write_run_record(
        agent_id="agent-fi-cost", component="cost", started_at="2026-10-06T04:00:00Z",
        finished_at="2026-10-06T04:05:00Z", status="completed", artifacts_written=3,
        duplicates_skipped=1, agent_version="1.0.0", ddb_client=res,
        table_name="vsa-staging-avx-agent-runs", data_month=MONTH, vins_selected=4,
        model_calls=3, deferred=2, tokens_used=13200, cost_usd=0.054, write_failures=1,
        rules_fallbacks=1, guardrail_interventions=2,
    ) is True
    (item,) = res.table.items
    expected = {"data_month": MONTH, "vins_selected": 4, "model_calls": 3, "deferred": 2,
                "tokens_used": 13200, "write_failures": 1, "rules_fallbacks": 1,
                "guardrail_interventions": 2}
    assert {k: item.get(k) for k in expected} == expected, item
    assert str(item["cost_usd"]) == "0.054"


def test_an_abort_after_a_sell_timing_write_records_that_finding(adp, monkeypatch):
    """Cycle 3 run W2 (mutant N7): sell timing runs first, so its writes count too."""
    vins = _seed(adp, 2)
    adp.sell_rows = _rising_series(_vin(907))
    real = cost_agent.compute_inputs_hash

    def raising_on_second(evidence):
        if evidence.get("vin") == vins[1]:
            raise TypeError("unhashable evidence")
        return real(evidence)

    monkeypatch.setattr(cost_agent, "compute_inputs_hash", raising_on_second)
    records: list = []
    with pytest.raises(TypeError):
        _run_cost(records)
    (rec,) = records
    assert rec["status"] == "aborted"
    assert (rec["artifacts_written"], rec["model_calls"]) == (2, 1), rec
