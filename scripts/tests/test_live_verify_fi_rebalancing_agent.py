"""``live-verify-fi-rebalancing.py --part agent`` against a fake staging built from the real code.

The world is ``scripts/tests/fi_rebal_world.py``: moto for DynamoDB/S3/SSM/Cognito, the real
``run_rebalance`` with the real writers behind the Lambda fake, so every finding and run record
the checks read has the shape production writes (review cycle 1 W7). Each check has a passing
control and failing cases that fail on the check's own assertion.
"""
from __future__ import annotations

import datetime as dt
import importlib.util
import json
import os
import signal
from decimal import Decimal
from pathlib import Path
from typing import Any

import boto3
import pytest
from moto import mock_aws

from scripts.tests import fi_rebal_world as W

_SCRIPT = Path(__file__).resolve().parents[1] / "live-verify-fi-rebalancing.py"


def _load() -> Any:
    spec = importlib.util.spec_from_file_location("lv_fi_rebal_agent", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _policy_value() -> str:
    from agents.fi_rebalancing.tests.policy_file import policy_entry
    v = policy_entry()["staging_default"]
    return v if isinstance(v, str) else json.dumps(v, separators=(",", ":"))


class World:
    """One fake staging: clients for the script and the state behind them."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, *, gaps_rows: list | None = None,
                 bedrock: "W.FakeBedrock | None" = None) -> None:
        self.mp = monkeypatch
        self.today = W._today()
        self.session = boto3.Session(aws_access_key_id="testing", aws_secret_access_key="testing",
                                     region_name=W.REGION)
        W.create_resources(self.session, self.today, _policy_value())
        self.cw = W.FakeCloudWatch()
        self.bedrock = bedrock
        self.lam = W.FakeLambda(self.session, self.cw, bedrock=bedrock)
        self.ath = W.FakeAthena(self.today, gaps_rows=gaps_rows)
        self.sns = W.FakeSns()
        self.logs = W.FakeLogs(self.session)
        self.ddb = self.session.client("dynamodb", region_name=W.REGION)
        self.s3 = self.session.client("s3", region_name=W.ADP_REGION)
        self.ssm = self.session.client("ssm", region_name=W.REGION)
        self.idp = W.FakeIdp()
        self._agent = W.agent_reads(monkeypatch, self.today, gaps_rows=gaps_rows)
        self._agent.__enter__()
        self.mod = _load()
        # The schedule guard: no Tier 2 schedule within 45 minutes (tests of the guard itself
        # are in test_live_verify_fi_rebalancing.py).
        monkeypatch.setattr(self.mod, "_check_scheduler_timing", lambda s, a: (True, "ok"))
        monkeypatch.setattr(self.mod, "_in_write_window", lambda: False)
        self.clock = W.FakeTime()
        monkeypatch.setattr(self.mod, "time", self.clock)
        self.state = self.mod._AgentState(stage=W.STAGE, fn_name=W.FN, fn_arn="arn:fn",
                                          sched=None, allow_writes=True)
        self.results: list = []

    def rows(self) -> dict[str, tuple[str, str]]:
        return {n: (s, d) for n, s, d in self.results}

    def g1(self, **kw: Any) -> tuple[str, str]:
        self.mod.check_g1_plants_vs_moves(
            self.state, ath=self.ath, s3=self.s3, ddb=self.ddb, lam=self.lam,
            lake_bucket=W.LAKE, wg="wg", use_run=kw.get("use_run"), results=self.results)
        return self.rows()["G1 plants vs moves"]

    def g2(self) -> tuple[str, str]:
        self.mod.check_g2_money_fields(self.state, ath=self.ath, ssm=self.ssm, ddb=self.ddb,
                                       wg="wg", results=self.results)
        return self.rows()["G2 money fields"]

    def g3(self) -> tuple[str, str]:
        self.mod.check_g3_idempotency(self.state, lam=self.lam, ddb=self.ddb,
                                      results=self.results)
        return self.rows()["G3 idempotency"]

    def scan(self) -> list[dict]:
        return self.mod._all_rebalancing_findings(self.ddb, W.STAGE)

    def put_finding(self, item: dict) -> None:
        from boto3.dynamodb.types import TypeSerializer
        ser = TypeSerializer()
        self.ddb.put_item(TableName=W.FINDINGS, Item={k: ser.serialize(v) for k, v in item.items()})


@pytest.fixture()
def world(monkeypatch: pytest.MonkeyPatch):
    for k in list(os.environ):
        if k.startswith("AWS_"):
            monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", W.REGION)
    with mock_aws():
        w = World(monkeypatch)
        yield w
        w._agent.__exit__(None, None, None)


# ---------------------------------------------------------------------------
# The world itself: the real runner wrote what the checks read
# ---------------------------------------------------------------------------


def test_world_run_writes_a_move_and_a_gap_with_the_real_writer(world):
    payload = world.mod._invoke_rebalance(world.lam, W.FN)
    assert payload["status"] == "completed" and payload["move_items_written"] == 1
    assert payload["gap_items_written"] >= 1
    rec = world.mod._run_record(world.ddb, W.STAGE, payload["started_at"], wait_s=0)
    assert rec is not None and rec["status"] == "completed"
    kinds = sorted(f["finding_kind"] for f in world.scan())
    assert "fleet.rebalance.move" in kinds and "fleet.rebalance.gap" in kinds


# ---------------------------------------------------------------------------
# G1
# ---------------------------------------------------------------------------


def test_g1_passes_on_the_real_run(world):
    status, detail = world.g1()
    assert status == "PASS", detail
    assert world.state.run is not None and world.state.run_items
    assert world.ath.queries[0].count("horizon_days = 7") == 2


def test_g1_old_items_of_an_earlier_run_do_not_score(world):
    """C2a: last week's live-looking rows into the plant location are not this run's output."""
    world.g1()                                  # a real run, then make its items old
    week_ago = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=7)).strftime(
        "%Y-%m-%dT%H:%M:%SZ")
    for f in world.scan():
        f["computed_at"] = week_ago
        world.put_finding(f)
    world.results.clear()
    run = world.state.run
    world.state.run = None
    status, detail = world.g1(use_run=run["started_at"])
    assert status == "FAIL" and "0/1 eligible plants" in detail, detail


def test_g1_fails_when_no_eligible_deficit_plant_remains(monkeypatch):
    """C2d: total == 0 is not a pass."""
    rows = [dict(g, status="ok") if g["location_id"] == "loc-def" else g
            for g in W.gaps(W._today())]
    with mock_aws():
        w = World(monkeypatch, gaps_rows=rows)
        status, detail = w.g1()
    assert status == "FAIL" and "no eligible deficit plant" in detail, detail


def test_g1_fails_when_the_key_has_no_no_spare_plant(world):
    key = W.plants(world.today)
    key["plants"] = [p for p in key["plants"] if "no-spare" not in p["note"]]
    world.s3.put_object(Bucket=W.LAKE, Key=W.PLANTS_KEY, Body=json.dumps(key).encode())
    status, detail = world.g1()
    assert status == "FAIL" and "0 no-spare plant" in detail, detail


def test_g1_fails_when_the_key_has_two_no_spare_plants(world):
    """Exactly one no-spare plant: with two, G1 can't tell which gap item answers D10
    (mutant N16)."""
    key = W.plants(world.today)
    (nsp,) = [p for p in key["plants"] if "no-spare" in p["note"]]
    key["plants"].append(dict(nsp, location_id="loc-def"))
    world.s3.put_object(Bucket=W.LAKE, Key=W.PLANTS_KEY, Body=json.dumps(key).encode())
    status, detail = world.g1()
    assert status == "FAIL" and "2 no-spare plant" in detail, detail


def _key_with(world, change) -> None:
    key = W.plants(world.today)
    change(key["plants"])
    world.s3.put_object(Bucket=W.LAKE, Key=W.PLANTS_KEY, Body=json.dumps(key).encode())


def test_g1_leaves_out_a_plant_that_starts_after_the_data_day(world):
    """Accept 1 "detectable that day" (W-E, mutant L5): a demand surge starting tomorrow at a
    deficit location with no move isn't scored, so G1 still passes on 1 of 1."""
    tomorrow = (world.today + dt.timedelta(days=1)).isoformat()
    _key_with(world, lambda ps: ps.append({"location_id": "loc-nsp", "start_date": tomorrow,
                                           "factor": 1.5, "kind": "demand_surge",
                                           "note": "starts tomorrow"}))
    status, detail = world.g1()
    assert status == "PASS" and "eligible=1;" in detail, detail


def test_g1_scores_a_plant_that_starts_on_the_data_day(world):
    """The boundary is inclusive: a surge starting on the data day is detectable that day."""
    today = world.today.isoformat()

    def start_today(ps):
        for p in ps:
            if p["location_id"] == "loc-def":
                p["start_date"] = today
    _key_with(world, start_today)
    status, detail = world.g1()
    assert status == "PASS" and "eligible=1;" in detail and "hit=1;" in detail, detail


def test_g1_fails_when_the_no_spare_plant_has_not_started(world):
    """W-E, mutant L6: a gap item at the no-spare location before its plant starts is not D10
    met."""
    tomorrow = (world.today + dt.timedelta(days=1)).isoformat()

    def start_tomorrow(ps):
        for p in ps:
            if "no-spare" in p["note"]:
                p["start_date"] = tomorrow
    _key_with(world, start_tomorrow)
    status, detail = world.g1()
    assert status == "FAIL" and "not active on" in detail, detail


def test_g1_fails_a_cross_metro_move(world):
    """W4: a move from another metro into a plant location fails G1."""
    world.g1()
    (move,) = [f for f in world.state.run_items if f["finding_kind"] == "fleet.rebalance.move"]
    move["location"]["from_location_id"] = "loc-nsp"      # phx -> sea
    world.put_finding(move)
    world.results.clear()
    run = world.state.run
    status, detail = world.g1(use_run=run["started_at"])
    assert status == "FAIL" and "not same-metro" in detail, detail


def test_g1_fails_when_the_no_spare_plant_has_no_gap_item(world):
    """Spec D10: the no-spare plant must get a gap item in the same run (mutant N12)."""
    assert world.g1()[0] == "PASS"
    nsp = [f for f in world.state.run_items if f["finding_kind"] == "fleet.rebalance.gap"
           and f["location"]["location_id"] == "loc-nsp"]
    assert nsp, "the real run wrote a gap item for the no-spare plant"
    for f in nsp:
        f["location"]["location_id"] = "loc-sur"
        world.put_finding(f)
    world.results.clear()
    status, detail = world.g1(use_run=world.state.run["started_at"])
    assert status == "FAIL" and "has no gap item" in detail, detail


def test_g1_use_run_must_name_a_completed_run(world):
    status, detail = world.g1(use_run="2026-01-01T00:00:00Z")
    assert status == "FAIL" and "no such run record" in detail, detail


def test_a_failed_invoke_stops_every_later_invoke(world):
    """W1: G3's re-run fails, so G5 doesn't touch the policy or invoke again."""
    assert world.g1()[0] == "PASS"
    world.ssm.put_parameter(Name=W.POLICY_PARAM, Value='{"bad": true}', Type="String",
                            Overwrite=True)
    status, detail = world.g3()
    assert status == "FAIL" and "PolicyUnavailable" in detail, detail
    assert world.state.invoke_failed is True
    n = len(world.lam.invocations)
    world.mod.check_g5_forced_failure(world.state, lam=world.lam, ssm=world.ssm, cw=world.cw,
                                      sns=world.sns, ddb=world.ddb, results=world.results)
    status, detail = world.rows()["G5 forced failure"]
    assert status == "SKIP" and "earlier invoke failed" in detail, detail
    assert len(world.lam.invocations) == n



# ---------------------------------------------------------------------------
# G2
# ---------------------------------------------------------------------------


def _g1_then_move(world) -> dict:
    assert world.g1()[0] == "PASS"
    (move,) = [f for f in world.state.run_items if f["finding_kind"] == "fleet.rebalance.move"]
    return move


def _set_evidence(move: dict, kind: str, **values: Any) -> None:
    for e in move["evidence"]:
        if e["kind"] == kind:
            e["value"].update(values)


def test_g2_passes_on_the_real_run(world):
    _g1_then_move(world)
    status, detail = world.g2()
    assert status == "PASS", detail


def test_g2_fails_one_cent_off(world):
    """S6: integer cents, so a one-cent miss is a miss."""
    from decimal import Decimal
    move = _g1_then_move(world)
    rv = next(e for e in move["evidence"] if e["kind"] == "rebalance.revenue")["value"]
    _set_evidence(move, "rebalance.revenue",
                  revenue_gain_usd=Decimal(str(rv["revenue_gain_usd"])) + Decimal("0.01"))
    world.put_finding(move)
    world.state.run_items = [move if f["finding_id"] == move["finding_id"] else f
                             for f in world.state.run_items]
    status, detail = world.g2()
    assert status == "FAIL" and "revenue_gain_usd" in detail, detail


def test_g2_fails_a_consistent_but_wrong_gain(world):
    """C3 / probe P6: closure identities hold, the gain doesn't match D9 recomputed."""
    from decimal import Decimal
    move = _g1_then_move(world)
    tv = next(e for e in move["evidence"] if e["kind"] == "rebalance.transfer")["value"]
    rv = next(e for e in move["evidence"] if e["kind"] == "rebalance.revenue")["value"]
    gain = Decimal("999999.00")
    impact = gain - Decimal(str(rv["source_loss_usd"]))
    net = impact - Decimal(str(tv["transfer_cost_usd"]))
    _set_evidence(move, "rebalance.revenue", revenue_gain_usd=gain, revenue_impact_usd=impact,
                  net_benefit_usd=net)
    move["recommended_action"]["estimated_value_usd"] = net
    world.state.run_items = [move]
    status, detail = world.g2()
    assert status == "FAIL" and "revenue_gain_usd" in detail, detail


def test_g2_fails_an_estimated_value_that_is_not_the_net(world):
    """Every evidence field matches the recomputation; only the action's estimated_value_usd
    differs from the net benefit by a dollar (mutant M4)."""
    from decimal import Decimal
    move = _g1_then_move(world)
    est = move["recommended_action"]["estimated_value_usd"]
    move["recommended_action"]["estimated_value_usd"] = Decimal(str(est)) + Decimal("1.00")
    world.state.run_items = [move]
    status, detail = world.g2()
    assert status == "FAIL" and "estimated_value_usd" in detail, detail
    assert "revenue_gain_usd" not in detail and "net_benefit_usd" not in detail, detail


def _kept_from_yesterday(world, move: dict, *, supply_then: int, finding_id: str | None = None,
                         snapshot_supply: int | None = None) -> dict:
    """*move* as if first written yesterday and only refreshed today (same inputs_hash: the
    writer moves computed_at/expires_at and keeps the evidence). Its evidence cites yesterday's
    forecast run and gaps snapshot, with destination supply *supply_then*, and its money is
    D9 on that evidence. ADP keeps yesterday's run; the world gets yesterday's snapshot with
    destination supply *snapshot_supply* (default: *supply_then*)."""
    import copy
    from agents.fi_rebalancing import candidates as C
    from agents.fi_rebalancing.formulas import DEFAULTS
    y = world.today - dt.timedelta(days=1)
    if not any(r["run_date"] == y.isoformat() for r in world.ath.forecast_rows):
        world.ath.forecast_rows += W.forecast(y)
    snap = W.gaps(y)
    for g in snap:
        if g["location_id"] == move["location"]["to_location_id"]:
            g["supply_vehicles"] = supply_then if snapshot_supply is None else snapshot_supply
    world.ath.snapshots[y.isoformat()] = snap
    m = copy.deepcopy(move)
    if finding_id:
        m["finding_id"] = finding_id
    days = sorted((e for e in m["evidence"] if e["kind"] == "rebalance.forecast"),
                  key=lambda e: e["value"]["date"])
    for i, e in enumerate(days):
        e["value"].update(date=(y + dt.timedelta(days=1 + i)).isoformat(), run_date=y.isoformat(),
                          data_as_of=y.isoformat(), supply_vehicles=supply_then)
    for e in m["evidence"]:
        if e["kind"] == "rebalance.supply":
            e["source_ref"] = f"adp.supply_demand_gaps_snapshot data_as_of={y.isoformat()}"
    tv = next(e for e in m["evidence"] if e["kind"] == "rebalance.transfer")["value"]
    cand = {"per_day": [{"forecast_hours": float(e["value"]["forecast_hours"]),
                         "supply_vehicles": supply_then, "op_hours": float(e["value"]["op_hours"]),
                         "source_forecast_hours": 20.0, "source_supply_vehicles": 10,
                         "source_op_hours": 10.0} for e in days],
            "rate": DEFAULTS["RATES"]["airport"], "source_rate": DEFAULTS["RATES"]["downtown"],
            "transfer_cost_per_vehicle": float(tv["transfer_cost_per_vehicle"]),
            "max_utilization": 0.95}
    money = C.money_for(cand, int(tv["vehicle_count"]))
    rv = next(e for e in m["evidence"] if e["kind"] == "rebalance.revenue")["value"]
    for k in ("revenue_gain_usd", "source_loss_usd", "revenue_impact_usd", "net_benefit_usd"):
        rv[k] = Decimal(str(money[k]))
    tv["transfer_cost_usd"] = Decimal(str(money["transfer_cost_usd"]))
    m["recommended_action"]["estimated_value_usd"] = Decimal(str(money["net_benefit_usd"]))
    return m


def test_g2_checks_a_kept_move_against_the_day_it_cites(world):
    """W-A: a move kept from yesterday (supply 2 then, 3 now) meets Accept 2 on its own
    evidence and yesterday's data."""
    move = _g1_then_move(world)
    world.state.run_items = [_kept_from_yesterday(world, move, supply_then=2)]
    status, detail = world.g2()
    y = (world.today - dt.timedelta(days=1)).isoformat()
    assert status == "PASS" and f"('{y}', '{y}')" in detail, detail


def test_g2_passes_a_run_with_a_rewritten_and_a_kept_move(world):
    """W-A: moves of one run may cite different forecast runs."""
    move = _g1_then_move(world)
    kept = _kept_from_yesterday(world, move, supply_then=3, finding_id="F-kept-from-yesterday")
    world.state.run_items = [move, kept]
    status, detail = world.g2()
    t, y = world.today.isoformat(), (world.today - dt.timedelta(days=1)).isoformat()
    assert status == "PASS" and f"('{t}', '{t}')" in detail and f"('{y}', '{y}')" in detail, detail


def test_g2_fails_a_kept_move_whose_cited_snapshot_disagrees(world):
    move = _g1_then_move(world)
    world.state.run_items = [_kept_from_yesterday(world, move, supply_then=2, snapshot_supply=3)]
    status, detail = world.g2()
    assert status == "FAIL" and "destination supply/op_hours 2.0/" in detail, detail


def test_g2_fails_destination_supply_that_disagrees_with_the_snapshot(world):
    """L7: one forecast day's supply_vehicles off by one vehicle."""
    move = _g1_then_move(world)
    day = sorted((e for e in move["evidence"] if e["kind"] == "rebalance.forecast"),
                 key=lambda e: e["value"]["date"])[2]
    day["value"]["supply_vehicles"] = day["value"]["supply_vehicles"] + 1
    world.state.run_items = [move]
    status, detail = world.g2()
    assert status == "FAIL" and "destination supply/op_hours" in detail, detail


def test_g2_fails_destination_op_hours_that_disagree_with_cms(world):
    move = _g1_then_move(world)
    day = sorted((e for e in move["evidence"] if e["kind"] == "rebalance.forecast"),
                 key=lambda e: e["value"]["date"])[0]
    day["value"]["op_hours"] = day["value"]["op_hours"] + 1
    world.state.run_items = [move]
    status, detail = world.g2()
    assert status == "FAIL" and "destination supply/op_hours" in detail, detail


def test_g2_fails_a_move_citing_two_forecast_runs(world):
    move = _g1_then_move(world)
    day = next(e for e in move["evidence"] if e["kind"] == "rebalance.forecast")
    day["value"]["run_date"] = (world.today - dt.timedelta(days=1)).isoformat()
    world.state.run_items = [move]
    status, detail = world.g2()
    assert status == "FAIL" and "(want one)" in detail, detail


def test_g2_fails_a_move_whose_supply_evidence_names_no_snapshot(world):
    move = _g1_then_move(world)
    for e in move["evidence"]:
        if e["kind"] == "rebalance.supply":
            e["source_ref"] = "adp.supply_demand_gaps_snapshot data_as_of=None"
    world.state.run_items = [move]
    status, detail = world.g2()
    assert status == "FAIL" and "want one gaps snapshot data_as_of" in detail, detail


def test_g2_fails_evidence_that_disagrees_with_adp(world):
    move = _g1_then_move(world)
    for e in move["evidence"]:
        if e["kind"] == "rebalance.forecast":
            e["value"]["forecast_hours"] = e["value"]["forecast_hours"] + 1
            break
    world.state.run_items = [move]
    status, detail = world.g2()
    assert status == "FAIL" and "forecast_hours" in detail and "!= ADP" in detail, detail


def test_g2_fails_a_missing_field(world):
    move = _g1_then_move(world)
    for e in move["evidence"]:
        if e["kind"] == "rebalance.transfer":
            del e["value"]["transfer_cost_per_vehicle"]
    world.state.run_items = [move]
    status, detail = world.g2()
    assert status == "FAIL" and "KeyError" in detail, detail


def test_g2_fails_a_run_with_no_move(world):
    assert world.g1()[0] == "PASS"
    world.state.run_items = [f for f in world.state.run_items
                             if f["finding_kind"] != "fleet.rebalance.move"]
    status, detail = world.g2()
    assert status == "FAIL" and "no move items" in detail, detail


# ---------------------------------------------------------------------------
# G3
# ---------------------------------------------------------------------------


def test_g3_passes_on_an_unchanged_rerun(world):
    assert world.g1()[0] == "PASS"
    status, detail = world.g3()
    assert status == "PASS", detail
    assert f"duplicates_skipped={len(world.state.run_items)}" in detail


def test_g3_fails_when_the_rerun_rewrites_items(world, monkeypatch):
    """C4b: the writer keeps finding_id on an in-place update, so ids alone can't show it."""
    assert world.g1()[0] == "PASS"
    from agents.fi_rebalancing import sources as src
    more = [dict(r, forecast_hours=r["forecast_hours"] + 5) for r in W.forecast(world.today)]
    monkeypatch.setattr(src, "read_forecast_days", lambda **kw: more)
    status, detail = world.g3()
    assert status == "FAIL" and "artifacts_written=" in detail and "(want 0)" in detail, detail
    assert "rewritten (inputs_hash changed)" in detail, detail


def test_g3_fails_when_the_record_lacks_a_counter(world, monkeypatch):
    """C4: an absent field is a FAIL, not a default."""
    assert world.g1()[0] == "PASS"
    from agents.tier2 import runner
    real = runner._rebalance_record_fields
    monkeypatch.setattr(runner, "_rebalance_record_fields",
                        lambda stats: {k: v for k, v in real(stats).items() if k != "model_calls"})
    status, detail = world.g3()
    assert status == "FAIL" and "run record has no model_calls" in detail, detail


def test_g3_fails_when_the_payload_lacks_tokens_used(world, monkeypatch):
    assert world.g1()[0] == "PASS"
    real = world.mod._invoke_rebalance

    def no_tokens(lam, fn):
        p = real(lam, fn)
        p.pop("tokens_used", None)
        return p
    monkeypatch.setattr(world.mod, "_invoke_rebalance", no_tokens)
    status, detail = world.g3()
    assert status == "FAIL" and "payload has no tokens_used" in detail, detail


def test_g3_fails_when_duplicates_skipped_is_short(world):
    """The re-run must skip exactly the items G1's run left."""
    assert world.g1()[0] == "PASS"
    world.state.run_items = world.state.run_items + [world.state.run_items[0]]
    status, detail = world.g3()
    assert status == "FAIL" and "duplicates_skipped=" in detail, detail


# ---------------------------------------------------------------------------
# G3 on the model path (prompt 40). Prompt 37's live re-check failed two ways that the
# rules-only world above could not reach: a skipped metro dropped its moves, so the re-run
# wrote "no move recommended" gap items for the deficits those moves cover (Austin), and a
# metro whose model output failed validation and fell back to rules never stored the
# fingerprint, so it called the model on every run (Atlanta). Each case below runs the real
# runner, agent and writers on moto with W.FakeBedrock, then the script's own G3 check.
# ---------------------------------------------------------------------------


@pytest.fixture()
def model_world(monkeypatch: pytest.MonkeyPatch):
    for k in list(os.environ):
        if k.startswith("AWS_"):
            monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", W.REGION)
    made: list[World] = []
    with mock_aws():
        def make(mode: str) -> World:
            w = World(monkeypatch, bedrock=W.FakeBedrock(mode))
            made.append(w)
            return w
        yield make
        for w in made:
            w._agent.__exit__(None, None, None)


def _live_gap_row(world, location_id: str) -> dict:
    rows = [f for f in world.scan()
            if f.get("finding_kind") == "fleet.rebalance.gap"
            and f.get("location", {}).get("location_id") == location_id
            and f.get("expires_at", "") > dt.datetime.now(dt.timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ")]
    assert len(rows) == 1, rows
    return rows[0]


def test_g3_passes_on_a_model_path_rerun(model_world):
    """Defect A: the skipped metro keeps its live move, so its target gets no gap item."""
    world = model_world("move")
    assert world.g1()[0] == "PASS"
    fresh = len(world.bedrock.converse_calls)
    assert fresh == 1, "the fresh run must take the model path for this case to mean anything"
    moves = [f for f in world.scan() if f.get("finding_kind") == "fleet.rebalance.move"]
    assert [m["detail"] for m in moves] == ["Move one spare vehicle to cover the shortfall."]
    status, detail = world.g3()
    assert status == "PASS", detail
    assert len(world.bedrock.converse_calls) == fresh, "the re-run called the model"
    assert not [f for f in world.scan() if f.get("finding_kind") == "fleet.rebalance.gap"
                and f.get("location", {}).get("location_id") == "loc-def"], (
        "a gap item says loc-def has no move while a live move covers it")


def test_g3_passes_when_the_metro_fell_back_to_rules(model_world):
    """Defect B: a rules-fallback outcome stores the fingerprint, so the re-run skips it."""
    world = model_world("invalid")
    world.g1()
    assert world.state.run is not None
    fresh = len(world.bedrock.converse_calls)
    assert fresh == 2, "two failed model attempts, then the rules fallback"
    moves = [f for f in world.scan() if f.get("finding_kind") == "fleet.rebalance.move"]
    assert len(moves) == 1 and moves[0]["detail"] != "Move one spare vehicle to cover the shortfall."
    status, detail = world.g3()
    assert status == "PASS", detail
    assert len(world.bedrock.converse_calls) == fresh, "the re-run called the model"


def test_g3_passes_when_the_model_held_the_deficit(model_world):
    """A no-move outcome (the model held the only deficit) stores the fingerprint on the
    gap item, so the re-run skips the model, and the hold stays on the live gap item."""
    world = model_world("hold")
    world.g1()
    assert world.state.run is not None
    fresh = len(world.bedrock.converse_calls)
    assert fresh == 1
    held = _live_gap_row(world, "loc-def")
    assert [e["value"]["reason"] for e in held["evidence"]
            if e["kind"] == "rebalance.hold"] == ["returns_expected"]
    status, detail = world.g3()
    assert status == "PASS", detail
    assert len(world.bedrock.converse_calls) == fresh, "the re-run called the model"
    after = _live_gap_row(world, "loc-def")
    assert after["finding_id"] == held["finding_id"]
    assert [e["value"]["reason"] for e in after["evidence"]
            if e["kind"] == "rebalance.hold"] == ["returns_expected"]


@pytest.mark.parametrize("mode,calls", [("move", 1), ("invalid", 2)])
def test_run_record_and_payload_count_the_model_tokens(model_world, mode, calls):
    """tokens_used was 0 in every payload while the model ran, and the run record had none."""
    world = model_world(mode)
    world.g1()
    run = world.state.run
    assert run is not None and len(world.bedrock.converse_calls) == calls
    want = calls * (W.FakeBedrock.USAGE["inputTokens"] + W.FakeBedrock.USAGE["outputTokens"])
    assert int(run["payload"]["tokens_used"]) == want, run["payload"]
    assert "tokens_used" in run["record"], run["record"]
    assert int(run["record"]["tokens_used"]) == want, run["record"]
    from agents.fi_rebalancing.llm import ModelStats
    cost = ModelStats(input_tokens=calls * W.FakeBedrock.USAGE["inputTokens"],
                      output_tokens=calls * W.FakeBedrock.USAGE["outputTokens"]).cost_usd()
    assert cost > 0
    assert "cost_usd" in run["record"], run["record"]
    assert float(run["record"]["cost_usd"]) == pytest.approx(cost), run["record"]



# ---------------------------------------------------------------------------
# G4
# ---------------------------------------------------------------------------


def _g4(world, api_url: str = "http://127.0.0.1:1/staging") -> dict:
    cleanup: list = []
    world.mod.check_g4_isolation(world.state, ddb=world.ddb, logs=world.logs, idp=world.idp,
                                 avx_api_endpoint=api_url, results=world.results,
                                 cleanup=cleanup)
    world.cleanup = cleanup
    return world.rows()


@pytest.fixture()
def api():
    servers = []

    def make(own_items=None, allowed=frozenset({W.DELIVERY})):
        s = W.FakeFindingsApi(own_items or [{"finding_id": "F-del-1", "agent_id": "agent-health",
                                             "scope": {"fleet_id": W.DELIVERY}},
                                            {"finding_id": "F-del-2", "agent_id": "agent-health",
                                             "scope": {"fleet_id": W.DELIVERY}}], set(allowed))
        servers.append(s)
        return s
    yield make
    for s in servers:
        s.close()


def test_g4_passes_and_sends_the_token_and_pages(world, api):
    assert world.g1()[0] == "PASS"
    srv = api()
    rows = _g4(world, srv.url)
    assert rows["G4a no owner_id"][0] == "PASS", rows
    assert rows["G4b fleet route isolation"][0] == "PASS", rows
    assert rows["G4c broker skip"][0] == "PASS", rows
    # The bytes sent: the bearer token of the temp user, both pages of the own route.
    (user,) = world.idp.deleted
    assert user.startswith("livetest-fi-rebal-g4-") and world.idp.users == {}
    assert {r["authorization"] for r in srv.requests} == {f"Bearer token-for-{user}"}
    paths = [(r["path"], r["query"].get("next_token")) for r in srv.requests]
    assert paths == [("/staging/findings/fleet/demo-rental-carshare", None),
                     ("/staging/findings/fleet/demo-lastmile-delivery", None),
                     ("/staging/findings/fleet/demo-lastmile-delivery", ["1"])]
    created = [kw for op, kw in world.idp.calls if op == "AdminCreateUser"][0]
    attrs = {a["Name"]: a["Value"] for a in created["UserAttributes"]}
    assert attrs["custom:fleetIds"] == W.DELIVERY and created["UserPoolId"] == world.idp.pool_id


def test_g4a_fails_on_owner_id(world, api):
    assert world.g1()[0] == "PASS"
    f = world.scan()[0]
    f["owner_id"] = "DRV-1"
    world.put_finding(f)
    rows = _g4(world, api().url)
    assert rows["G4a no owner_id"][0] == "FAIL", rows


def test_g4a_fails_with_no_findings(world, api):
    rows = _g4(world, api().url)
    assert rows["G4a no owner_id"] == ("FAIL", "no rebalancing Finding exists"), rows


def test_g4b_fails_when_the_rental_route_answers(world, api):
    assert world.g1()[0] == "PASS"
    rows = _g4(world, api(allowed={W.DELIVERY, W.RENTAL}).url)
    status, detail = rows["G4b fleet route isolation"]
    assert status == "FAIL" and "rental fleet route answered 200" in detail, detail


def test_g4b_fails_when_the_own_route_returns_a_rental_item(world, api):
    """W3: the positive control's body is read on every page."""
    assert world.g1()[0] == "PASS"
    leak = {"finding_id": "F-rent", "agent_id": "agent-fi-rebalancing",
            "scope": {"fleet_id": W.RENTAL}}
    rows = _g4(world, api(own_items=[{"finding_id": "F-del-1", "agent_id": "x",
                                      "scope": {"fleet_id": W.DELIVERY}}, leak]).url)
    status, detail = rows["G4b fleet route isolation"]
    assert status == "FAIL" and "F-rent" in detail, detail


def test_g4b_fails_when_the_own_route_is_refused(world, api):
    assert world.g1()[0] == "PASS"
    rows = _g4(world, api(allowed=set()).url)
    status, detail = rows["G4b fleet route isolation"]
    assert status == "FAIL" and "own fleet route answered 403" in detail, detail


def test_g4b_records_a_failed_deletion_with_the_command(world, api):
    assert world.g1()[0] == "PASS"
    world.idp.delete_fails = True
    _g4(world, api().url)
    (cmd,) = world.cleanup
    assert cmd.startswith("aws cognito-idp admin-delete-user") and "livetest-fi-rebal-g4-" in cmd


def test_g4b_skips_without_allow_writes_and_g4a_still_runs(world, api):
    """W3: G4a's result is not dropped when G4b can't run."""
    assert world.g1()[0] == "PASS"
    f = world.scan()[0]
    f["owner_id"] = "DRV-1"
    world.put_finding(f)
    world.state.allow_writes = False
    rows = _g4(world, api().url)
    assert rows["G4a no owner_id"][0] == "FAIL"
    assert rows["G4b fleet route isolation"][0] == "SKIP"
    assert world.idp.calls == []


def test_g4c_fails_without_a_skip_line_for_this_runs_items(world, api):
    assert world.g1()[0] == "PASS"
    world.logs.filter_log_events = lambda **kw: {"events": [
        {"message": "broker: skipping — fi_item finding_id=F-old kind=fleet.rebalance.move "
                    "agent_id=agent-fi-rebalancing"}]}
    rows = _g4(world, api().url)
    assert rows["G4c broker skip"][0] == "FAIL", rows


def test_g4c_pages_and_starts_at_the_run(world, api):
    assert world.g1()[0] == "PASS"
    world.logs.extra = ["START RequestId: r1", "END RequestId: r1", "REPORT RequestId: r1"]
    _g4(world, api().url)
    assert len(world.logs.calls) >= 2 and world.logs.calls[1].get("nextToken") == "2"
    start = world.mod._parse_ts(world.state.run["started_at"])
    assert world.logs.calls[0]["startTime"] == int(start.timestamp() * 1000) - 60_000


# ---------------------------------------------------------------------------
# G5
# ---------------------------------------------------------------------------


def _g5(world) -> tuple[str, str]:
    world.mod.check_g5_forced_failure(world.state, lam=world.lam, ssm=world.ssm, cw=world.cw,
                                      sns=world.sns, ddb=world.ddb, results=world.results)
    return world.rows()["G5 forced failure"]


def _param(world) -> str:
    return world.ssm.get_parameter(Name=W.POLICY_PARAM)["Parameter"]["Value"]


def test_g5_passes_and_restores_the_policy_byte_for_byte(world):
    before = _param(world)
    status, detail = _g5(world)
    assert status == "PASS", detail
    assert _param(world) == before
    (inv,) = world.lam.invocations
    assert inv["Payload"] == {"component": "rebalance"}
    runs = world.ddb.query(TableName=W.RUNS, KeyConditionExpression="agent_component = :k",
                           ExpressionAttributeValues={":k": {"S": "agent-fi-rebalancing#rebalance"}})
    assert [r["status"]["S"] for r in runs["Items"]] == ["aborted"]


def test_g5_backup_is_0600_and_holds_the_original(world, monkeypatch, tmp_path):
    import tempfile
    monkeypatch.setattr(tempfile, "mkdtemp", lambda prefix="": str(tmp_path))
    before = _param(world)
    _g5(world)
    js, raw = tmp_path / "g5-ssm-backup.json", tmp_path / "g5-ssm-backup.value"
    assert oct(js.stat().st_mode & 0o777) == "0o600" and oct(raw.stat().st_mode & 0o777) == "0o600"
    assert raw.read_text() == before and json.loads(js.read_text())["original_value"] == before


def test_g5_skips_when_the_alarm_is_already_in_alarm(world):
    world.cw.state = "ALARM"
    before = _param(world)
    status, detail = _g5(world)
    assert status == "SKIP" and "already in ALARM" in detail
    assert _param(world) == before and world.lam.invocations == []


def test_g5_fails_when_the_alarm_action_failed(world):
    world.cw.action_state = "Failed"
    status, detail = _g5(world)
    assert status == "FAIL" and "actionState='Failed'" in detail, detail


def test_g5_fails_when_the_alarm_never_fires(world):
    world.cw.fires = False
    status, detail = _g5(world)
    assert status == "FAIL" and "did not reach ALARM" in detail, detail


def test_g5_fails_a_stale_history_item(world):
    """C5b: an Action item from before the invoke doesn't count. The alarm does reach ALARM
    after the invoke, but its own Action item never appears; the only Succeeded item is a day
    old, so G5 must wait out its deadline and fail rather than take the old item (mutant M3)."""
    old = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=1)
    world.cw.records_action = False
    world.cw.history = [{"Timestamp": old, "HistoryItemType": "Action",
                         "HistoryData": json.dumps({"actionState": "Succeeded"})}]
    status, detail = _g5(world)
    assert status == "FAIL" and "actionState=None" in detail, detail
    assert "did not reach ALARM" not in detail, detail


def test_g5_fails_the_wrong_alarm_metric(world):
    """C5a: the alarm is read by name and must watch Errors on this function."""
    world.cw.metric = "Duration"
    status, detail = _g5(world)
    assert status == "FAIL" and "watches Duration" in detail, detail
    assert world.lam.invocations == []


def test_g5_fails_without_a_confirmed_subscriber(world):
    world.sns.subs = ["PendingConfirmation", "PendingConfirmation"]
    status, detail = _g5(world)
    assert status == "FAIL" and "no confirmed subscription" in detail, detail


def test_g5_reads_every_subscription_page(world):
    """S5: the confirmed subscriber is on the second page."""
    world.sns.subs = ["PendingConfirmation", f"{W.TOPIC_ARN}:abc"]
    assert _g5(world)[0] == "PASS"


def test_g5_refuses_to_override_an_already_malformed_policy(world):
    world.ssm.put_parameter(Name=W.POLICY_PARAM, Value='{"x": 1}', Type="String", Overwrite=True)
    status, detail = _g5(world)
    assert status == "SKIP" and "already malformed" in detail and world.lam.invocations == []
    assert _param(world) == '{"x": 1}'


def test_g5_fails_a_different_abort_reason(world, monkeypatch):
    """S1: the FunctionError must be PolicyUnavailable, not any failure."""
    from agents.tier2 import runner
    real = runner.run_rebalance

    def other(*a, **kw):
        raise RuntimeError("SourceReadError stand-in")
    monkeypatch.setattr(runner, "run_rebalance", other)
    status, detail = _g5(world)
    assert status == "FAIL" and "errorType='RuntimeError'" in detail, detail
    monkeypatch.setattr(runner, "run_rebalance", real)


class _FlakySsm:
    """The real (moto) SSM client, with put_parameter failing the first *fails* restores."""

    def __init__(self, real: Any, *, fails: int = 0, code: str = "TooManyUpdates",
                 read_back_error: bool = False, on_restore=None,
                 read_back_value: str | None = None) -> None:
        self.real, self.fails, self.code = real, fails, code
        self.read_back_error, self.on_restore, self.puts = read_back_error, on_restore, 0
        self.read_back_value = read_back_value   # another writer's value after the restore

    def put_parameter(self, **kw: Any) -> dict:
        from botocore.exceptions import ClientError
        self.puts += 1
        if kw["Value"] != '{"bad": true}':
            if self.on_restore:
                self.on_restore()
            if self.fails:
                self.fails -= 1
                raise ClientError({"Error": {"Code": self.code, "Message": "x"}}, "PutParameter")
        return self.real.put_parameter(**kw)

    def get_parameter(self, **kw: Any) -> dict:
        from botocore.exceptions import ClientError
        if self.read_back_error and self.puts >= 2:
            raise ClientError({"Error": {"Code": "AccessDeniedException", "Message": "x"}},
                              "GetParameter")
        if self.read_back_value is not None and self.puts >= 2:
            return {"Parameter": {"Name": kw["Name"], "Value": self.read_back_value}}
        return self.real.get_parameter(**kw)


def test_g5_sigterm_during_the_invoke_restores_the_policy_then_exits(world, monkeypatch):
    """W-B (C6's long window): a SIGTERM while the malformed policy is live, during the
    forced invoke (up to 960 s), still restores it byte for byte, then exits 143."""
    before = _param(world)
    seen: dict = {}

    def dying_invoke(**kw: Any) -> dict:
        seen["policy"] = _param(world)
        os.kill(os.getpid(), signal.SIGTERM)
        raise AssertionError("SIGTERM did not interrupt the invoke")
    world.mod._install_signal_handlers()
    monkeypatch.setattr(world.lam, "invoke", dying_invoke)
    try:
        with pytest.raises(SystemExit) as ei:
            _g5(world)
    finally:
        signal.signal(signal.SIGTERM, signal.SIG_DFL)
        signal.signal(signal.SIGHUP, signal.SIG_DFL)
    assert seen["policy"] == world.mod._MALFORMED_POLICY, "the signal came mid-window"
    assert ei.value.code == 143
    assert _param(world) == before


def test_g5_a_read_timeout_during_the_invoke_restores_the_policy(world, monkeypatch):
    """W-B: an exception that isn't a FunctionError (a client read timeout) mid-invoke."""
    from botocore.exceptions import ReadTimeoutError
    before = _param(world)

    def timing_out(**kw: Any) -> dict:
        raise ReadTimeoutError(endpoint_url="https://lambda.us-west-2.amazonaws.com")
    monkeypatch.setattr(world.lam, "invoke", timing_out)
    with pytest.raises(Exception) as ei:
        _g5(world)
    assert type(ei.value) is ReadTimeoutError, f"{type(ei.value).__name__}: {ei.value}"
    assert _param(world) == before


def _runs_table(world) -> list[dict]:
    return world.ddb.scan(TableName=W.RUNS, ConsistentRead=True)["Items"]


def _after_the_forced_invoke(world, monkeypatch, then) -> None:
    real = world.lam.invoke

    def invoke(**kw: Any) -> dict:
        out = real(**kw)
        then()
        return out
    monkeypatch.setattr(world.lam, "invoke", invoke)


def _aborted(world) -> dict:
    items = [i for i in _runs_table(world) if i["status"]["S"] == "aborted"]
    assert len(items) == 1, items
    return items[0]


def test_g5_fails_when_the_forced_run_is_recorded_completed(world, monkeypatch):
    """W-D (mutant L4): the runner must record the forced failure as an abort."""
    _after_the_forced_invoke(world, monkeypatch, lambda: world.ddb.put_item(
        TableName=W.RUNS, Item=dict(_aborted(world), status={"S": "completed"})))
    status, detail = _g5(world)
    assert status == "FAIL" and "want exactly one aborted" in detail, detail


def test_g5_fails_on_a_second_run_record_after_the_invoke(world, monkeypatch):
    def add_second() -> None:
        it = _aborted(world)
        later = world.mod._iso(world.mod._parse_ts(it["started_at"]["S"]) + dt.timedelta(seconds=1))
        world.ddb.put_item(TableName=W.RUNS, Item=dict(it, started_at={"S": later}))
    _after_the_forced_invoke(world, monkeypatch, add_second)
    status, detail = _g5(world)
    assert status == "FAIL" and "want exactly one aborted" in detail, detail


def test_g5_fails_when_the_forced_run_left_no_record(world, monkeypatch):
    def drop() -> None:
        it = _aborted(world)
        world.ddb.delete_item(TableName=W.RUNS, Key={"agent_component": it["agent_component"],
                                                     "started_at": it["started_at"]})
    _after_the_forced_invoke(world, monkeypatch, drop)
    status, detail = _g5(world)
    assert status == "FAIL" and "want exactly one aborted" in detail, detail


def test_g5_sigint_during_restore_finishes_the_restore_then_interrupts(world):
    """SIGINT (Ctrl-C) is held during the restore like SIGTERM (security c1 S-G6, SM6)."""
    before = _param(world)
    world.ssm = _FlakySsm(world.ssm, on_restore=lambda: os.kill(os.getpid(), signal.SIGINT))
    with pytest.raises(KeyboardInterrupt):
        _g5(world)
    assert world.ssm.real.get_parameter(Name=W.POLICY_PARAM)["Parameter"]["Value"] == before


def test_g5_restore_retries_too_many_updates(world):
    before = _param(world)
    world.ssm = _FlakySsm(world.ssm, fails=2)
    status, detail = _g5(world)
    assert status == "PASS" and "attempt 3" in detail, detail
    assert world.ssm.real.get_parameter(Name=W.POLICY_PARAM)["Parameter"]["Value"] == before


def test_g5_read_back_error_fails_closed_with_the_restore_command(world):
    world.ssm = _FlakySsm(world.ssm, read_back_error=True)
    status, detail = _g5(world)
    assert status == "FAIL" and "RESTORE FAILED" in detail and "aws ssm put-parameter" in detail
    assert "--overwrite --value file://" in detail and world.state.invoke_failed is True


def test_g5_read_back_that_differs_is_not_a_restore(world):
    """The restore is only done when the read-back equals the original byte for byte
    (mutant N14)."""
    world.ssm = _FlakySsm(world.ssm, read_back_value='{"someone": "else"}')
    status, detail = _g5(world)
    assert status == "FAIL" and "RESTORE FAILED (read-back differs" in detail, detail
    assert world.state.invoke_failed is True


def test_g5_non_retryable_restore_error_fails_closed(world):
    world.ssm = _FlakySsm(world.ssm, fails=1, code="AccessDeniedException")
    status, detail = _g5(world)
    assert status == "FAIL" and "RESTORE FAILED (AccessDeniedException" in detail, detail


def test_g5_sigterm_during_restore_finishes_the_restore_then_exits(world):
    """C6: SIGTERM becomes SystemExit only after the held restore block."""
    before = _param(world)
    world.mod._install_signal_handlers()
    world.ssm = _FlakySsm(world.ssm, on_restore=lambda: os.kill(os.getpid(), signal.SIGTERM))
    with pytest.raises(SystemExit) as ei:
        _g5(world)
    assert ei.value.code == 143
    assert world.ssm.real.get_parameter(Name=W.POLICY_PARAM)["Parameter"]["Value"] == before
    signal.signal(signal.SIGTERM, signal.SIG_DFL)
    signal.signal(signal.SIGHUP, signal.SIG_DFL)



# ---------------------------------------------------------------------------
# Invoke preconditions and process signals
# ---------------------------------------------------------------------------


def test_every_invoke_re_checks_the_schedules_and_the_window(world, monkeypatch):
    """W2: the guards run before each invoke, not once at start."""
    m = world.mod
    assert m._may_invoke(world.state) is None
    monkeypatch.setattr(m, "_check_scheduler_timing", lambda s, a: (False, "cron at 06:00Z"))
    reason = m._may_invoke(world.state)
    assert reason is not None and "scheduler guard: cron at 06:00Z" in reason, reason
    status, detail = world.g1()
    assert status == "SKIP" and "cron at 06:00Z" in detail and world.lam.invocations == []
    monkeypatch.setattr(m, "_check_scheduler_timing", lambda s, a: (True, "ok"))
    monkeypatch.setattr(m, "_in_write_window", lambda: True)
    assert "04:50-06:10" in m._may_invoke(world.state)


def test_main_turns_sigterm_and_sighup_into_one_systemexit(monkeypatch):
    """C6: installed at process start, before any item can hold a write."""
    mod = _load()
    old = {s: signal.getsignal(s) for s in (signal.SIGTERM, signal.SIGHUP)}
    try:
        assert mod.main(["--part", "agent", "--stage", "prod", "--workgroup", "wg"]) == 2
        for s in (signal.SIGTERM, signal.SIGHUP):
            h = signal.getsignal(s)
            assert callable(h) and h.__name__ == "_exit_once"
        with pytest.raises(SystemExit) as ei:
            signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)
        assert ei.value.code == 143
        signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)   # a second one is ignored
    finally:
        for s, h in old.items():
            signal.signal(s, h)
