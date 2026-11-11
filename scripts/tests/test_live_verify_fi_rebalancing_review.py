"""``live-verify-fi-rebalancing.py --part review`` (task 7.7, the 7-day review) against the shared
fake world in ``fi_rebal_cms_world.py``, built from the real code.

The scene (built once per module, reset after every test):

- move A (``loc-aus-downtown`` -> ``loc-aus-hub2``) approved through the screen's real approve
  proxy, AVX's real approve route and ``CmsExecutor``; CMS's real ``POST /moves`` wrote its record.
  The record's dates are then set 8 days back (effective ``today - 8``, so its review date is
  yesterday), the only edit to a real record; the manifest is the one ``--part cms`` writes.
- move B (``loc-atl-downtown`` -> ``loc-atl-hub1``), approved the same way, effective tomorrow:
  the move under 7 days old.
- CMS's real ``refresh-rebalance-cache`` published the outcomes view with A's row (fixture row,
  as Athena returns it: strings).
- a second run of the real rebalancing agent read the outcomes snapshot through its real
  ``read_outcomes`` / ``run_query`` (real SQL text, real workgroup) from :class:`ReviewAthena`, and
  its real writer refreshed A's Finding with ``rebalance.recent_outcomes``. The world's first run
  is moved two days back (it ran before the review date and read no outcomes).
- the Athena history holds decoys: the same SQL for another fleet inside the run, newer unrelated
  queries, and the right SQL a day earlier; ids page two at a time, as do result rows.

Every test drives ``main()`` with argv; the four rows R1-R4 are pinned with a passing control and a
world that breaks each.
"""
from __future__ import annotations

import copy
import datetime as dt
import json
import os
import re
import subprocess
import sys
import uuid
from types import SimpleNamespace
from typing import Any
from unittest import mock

import pytest

from scripts.tests import fi_rebal_cms_world as W

ROW_RE = re.compile(r"^(PASS|FAIL|SKIP)\s+(.+?): (.*)$")
AGENT_WG = f"cvx-{W.STAGE}-analytics"
RUN_ID = "20261020T120000Z-abc123"
COLS = ["to_location_id", "effective_date", "ratio_hours", "predicted_on_rent_hours_gain",
        "actual_on_rent_hours_gain", "materialised_on"]
UTC = dt.timezone.utc
R1, R2, R3, R4 = "R1 snapshot", "R2 history route", "R3 agent evidence", "R4 young moves"
WRITE_OPS = {"PutItem", "UpdateItem", "DeleteItem", "TransactWriteItems", "BatchWriteItem",
             "StartQueryExecution", "PutParameter", "Invoke"}


def rows(out: str) -> dict[str, tuple[str, str]]:
    found: dict[str, tuple[str, str]] = {}
    for line in out.splitlines():
        m = ROW_RE.match(line)
        if m:
            found[m.group(2)] = (m.group(1), m.group(3))
    return found


def vins_of(f: dict) -> list[str]:
    return list(f["recommended_action"]["parameters"]["vehicle_vins"])


def _sql(fleet: str) -> str:
    from agents.fi_rebalancing.sources import _load
    return _load("select_outcomes.sql", stage=W.STAGE, fleet_id=fleet)


class ReviewAthena:
    """Athena for --part review. ``StartQueryExecution`` (the agent's real ``run_query``) records
    an execution with its real SQL text, workgroup and submission time and answers
    :attr:`rows`; ``ListQueryExecutions`` (newest first, two ids a page, filtered by workgroup),
    ``BatchGetQueryExecution`` and ``GetQueryResults`` (two rows a page) serve the history."""

    PAGE = 2

    def __init__(self, rows_: list[dict]) -> None:
        self.rows = rows_
        self.ex: dict[str, dict] = {}
        self.res: dict[str, list[list[str | None]]] = {}

    def add(self, query: str, wg: str, at: dt.datetime, rows_: list[dict] | None = None,
            state: str = "SUCCEEDED") -> str:
        qid = str(uuid.uuid4())
        self.ex[qid] = {"QueryExecutionId": qid, "Query": query, "WorkGroup": wg,
                        "Status": {"State": state, "SubmissionDateTime": at}}
        self.res[qid] = [list(COLS)] + [[r.get(c) for c in COLS] for r in (rows_ or [])]
        return qid

    def start(self, p: dict) -> dict:
        return {"QueryExecutionId": self.add(p["QueryString"], p.get("WorkGroup") or "primary",
                                             dt.datetime.now(UTC), self.rows)}

    def get(self, p: dict) -> dict:
        return {"QueryExecution": copy.deepcopy(self.ex[p["QueryExecutionId"]])}

    def results(self, p: dict) -> dict:
        res = self.res[p["QueryExecutionId"]]
        start = int(p.get("NextToken") or 0)
        page = res[start:start + self.PAGE]
        out: dict = {"ResultSet": {
            "Rows": [{"Data": [{} if v is None else {"VarCharValue": v} for v in r]} for r in page],
            "ResultSetMetadata": {"ColumnInfo": [{"Name": c, "Type": "varchar"} for c in COLS]}}}
        if start + self.PAGE < len(res):
            out["NextToken"] = str(start + self.PAGE)
        return out

    def list(self, p: dict) -> dict:
        wg = p.get("WorkGroup") or "primary"
        ids = sorted((q for q, e in self.ex.items() if e["WorkGroup"] == wg),
                     key=lambda q: self.ex[q]["Status"]["SubmissionDateTime"], reverse=True)
        start = int(p.get("NextToken") or 0)
        out: dict = {"QueryExecutionIds": ids[start:start + self.PAGE]}
        if start + self.PAGE < len(ids):
            out["NextToken"] = str(start + self.PAGE)
        return out

    def batch(self, p: dict) -> dict:
        ids = p["QueryExecutionIds"]
        return {"QueryExecutions": [copy.deepcopy(self.ex[i]) for i in ids if i in self.ex],
                "UnprocessedQueryExecutionIds": [
                    {"QueryExecutionId": i, "ErrorCode": "INVALID_INPUT", "ErrorMessage": "gone"}
                    for i in ids if i not in self.ex]}


def _approve(w: W.CmsWorld, finding: dict) -> str:
    token = w.user(W.RENTAL)
    code, body, _ = w.mod._post_json(
        f"{w.cms.url}/api/v1/fleet-intelligence/actions/{finding['finding_id']}/approve", token,
        {"parameters": {"vehicle_vins": vins_of(finding)}})
    assert code == 200 and body["decision"]["status"] == "executed", body
    return w.decision_id(finding["finding_id"])


def _refresh(w: W.CmsWorld, outcomes: list[dict]) -> None:
    saved = w.athena
    w.athena = w.refresh_athena
    try:
        w.refresh_athena.rows["move_outcomes"] = outcomes
        out = w.ix.handler({"fleetIntelligenceTask": "refresh-rebalance-cache"}, None)
        assert out.get("status") == "refreshed", out
    finally:
        w.athena = saved


def _run_agent(w: W.CmsWorld) -> dict:
    """A second run of the real agent; only ``read_outcomes`` reaches Athena (real SQL)."""
    from agents.fi_rebalancing import sources as src
    from agents.tier2 import runner
    from agents.tier2.artifact_writer import ArtifactWriter
    from agents.tier2.run_records import write_run_record

    real = src.read_outcomes
    ddb = w.session.client("dynamodb", region_name=W.REGION)
    res = w.session.resource("dynamodb", region_name=W.REGION)
    with W._agent_reads(w.today), mock.patch.object(src, "read_outcomes", real):
        return runner.run_rebalance(
            W.STAGE, athena_client=w.session.client("athena", region_name=W.ADP_REGION),
            workgroup=AGENT_WG, results_s3_path="s3://results/tier2/fi-rebalancing/",
            ddb_client=ddb, findings_table=W.FINDINGS, vehicles_table=W.VEHICLES,
            groundings_table=W.GROUNDINGS, fleet_locations_table=W.LOCS,
            avx_writer=ArtifactWriter(table_name=W.FINDINGS, dynamodb_client=ddb),
            run_record_writer=lambda **kw: write_run_record(ddb_client=res, table_name=W.RUNS, **kw),
            ssm_client=w.session.client("ssm", region_name=W.REGION))


def _ts(s: str) -> dt.datetime:
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))


def _build(w: W.CmsWorld) -> SimpleNamespace:
    today = w.today
    a = next(f for f in w.move_findings() if f["location"]["to_location_id"] == "loc-aus-hub2")
    b = next(f for f in w.move_findings() if f["location"]["to_location_id"] == "loc-atl-hub1")
    aid, bid = _approve(w, a), _approve(w, b)
    eff = (today - dt.timedelta(days=8)).isoformat()
    review_on = (today - dt.timedelta(days=1)).isoformat()
    moves = w._res().Table(W.MOVES)
    rec = w.move(aid)
    rec["effectiveDate"] = eff
    rec["createdAt"] = f"{(today - dt.timedelta(days=9)).isoformat()}T23:00:27.816448+00:00"
    moves.put_item(Item=rec)
    pred = str(rec["predictedOnRentHoursGain"])
    actual = "12.5"
    ratio = repr(12.5 / float(pred))
    w.mod._save_manifest({
        "run_id": RUN_ID, "stage": W.STAGE, "part": "cms", "started_at": rec["createdAt"],
        "move": {"finding_id": a["finding_id"], "action_id": aid, "vins": vins_of(a),
                 "from": "loc-aus-downtown", "to": "loc-aus-hub2", "approved": True,
                 "effective_date": eff, "created_at": rec["createdAt"]}})
    # The world's first run ran before the review date (it read no outcomes).
    runs = w._res().Table(W.RUNS)
    r1 = w.items(W.RUNS)[0]
    runs.delete_item(Key={"agent_component": r1["agent_component"], "started_at": r1["started_at"]})
    early = (today - dt.timedelta(days=2)).isoformat()
    r1 = {**r1, "started_at": f"{early}T06:00:45.000Z", "finished_at": f"{early}T06:02:14.000Z"}
    runs.put_item(Item=r1)
    view = {"move_id": aid, "fleet_id": W.RENTAL, "from_location_id": "loc-aus-downtown",
            "to_location_id": "loc-aus-hub2", "effective_date": eff, "review_available_on": review_on,
            "predicted_on_rent_hours_gain": pred, "predicted_turnaways_avoided": "7",
            "actual_on_rent_hours_gain": actual, "actual_turnaways_at_destination": "3",
            "source_actual_turnaways": "1", "ratio_hours": ratio, "method": "ml"}
    _refresh(w, [view])
    snap = [  # the newest snapshot: an older move into the same destination, A, another destination
        {"to_location_id": "loc-aus-hub2", "effective_date": (today - dt.timedelta(days=15)).isoformat(),
         "ratio_hours": "1.25", "predicted_on_rent_hours_gain": "40.0",
         "actual_on_rent_hours_gain": "50.0", "materialised_on": today.isoformat()},
        {"to_location_id": "loc-aus-hub2", "effective_date": eff, "ratio_hours": ratio,
         "predicted_on_rent_hours_gain": pred, "actual_on_rent_hours_gain": actual,
         "materialised_on": today.isoformat()},
        {"to_location_id": "loc-atl-airport", "effective_date": eff, "ratio_hours": "0.5",
         "predicted_on_rent_hours_gain": "10.0", "actual_on_rent_hours_gain": "5.0",
         "materialised_on": today.isoformat()},
    ]
    ra = ReviewAthena(snap)
    w.athena = ra
    w.router.add("athena", "ListQueryExecutions", ra.list)
    w.router.add("athena", "BatchGetQueryExecution", ra.batch)
    run2 = _run_agent(w)
    assert run2["status"] == "completed", run2
    (agent_qid,) = list(ra.ex)
    t = ra.ex[agent_qid]["Status"]["SubmissionDateTime"]
    ra.add(_sql(W.DELIVERY), AGENT_WG, t)                         # same run, another fleet
    ra.add(_sql(W.RENTAL), AGENT_WG, t - dt.timedelta(days=1))    # the right SQL, a day earlier
    fin = _ts(run2["finished_at"])
    for k in range(3):                                            # newer, unrelated
        ra.add(f"SELECT {k}", AGENT_WG, fin + dt.timedelta(seconds=10 + k))
    ra.add(_sql(W.RENTAL), AGENT_WG, fin + dt.timedelta(hours=23))  # the right SQL, next day's run
    a_after = next(f for f in w.move_findings() if f["finding_id"] == a["finding_id"])
    ents = [e for e in a_after["evidence"] if e["kind"] == "rebalance.recent_outcomes"]
    assert len(ents) == 1 and len(ents[0]["value"]) == 2, ents   # the world's premise
    return SimpleNamespace(w=w, today=today, a=a, b=b, aid=aid, bid=bid, eff=eff,
                           review_on=review_on, view=view, snap=snap, ra=ra, run2=run2,
                           agent_qid=agent_qid, run1=r1)


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    with W.CmsWorld(tmp_path_factory.mktemp("fi-rebal-review")) as w:
        yield _build(w)


_TABLES = (W.FINDINGS, W.MOVES, W.RUNS)


def _keys(table: str) -> tuple[str, ...]:
    return {W.FINDINGS: ("finding_id",), W.MOVES: ("fleetId", "moveId"),
            W.RUNS: ("agent_component", "started_at")}[table]


@pytest.fixture()
def s(built, monkeypatch):
    """The scene, reset after the test: tables, the Athena history, CMS's cache, defects."""
    w = built.w
    saved = {t: w.items(t) for t in _TABLES}
    ra_saved = (copy.deepcopy(built.ra.ex), copy.deepcopy(built.ra.res), copy.deepcopy(built.ra.rows))
    built.dirty = False
    w.mod._SESSION = None
    calls0 = len(w.router.all_calls)
    built.calls = lambda: w.router.all_calls[calls0:]
    built.created0, built.deleted0 = len(w.pool.users), len(w.pool.deleted)
    yield built
    for t in _TABLES:
        tbl = w._res().Table(t)
        for item in w.items(t):
            tbl.delete_item(Key={k: item[k] for k in _keys(t)})
        for item in saved[t]:
            tbl.put_item(Item=item)
    built.ra.ex, built.ra.res, built.ra.rows = ra_saved
    w.defects["moves_list_drops"] = False
    if built.dirty:
        _refresh(w, [built.view])
    for f in (w.state_dir).glob("*.json"):
        if f.name != f"{RUN_ID}.json":
            f.unlink()
    w.mod._SESSION = None


def argv(s: SimpleNamespace, *extra: str, run_id: str = RUN_ID, wg: str = AGENT_WG) -> list[str]:
    return ["--part", "review", "--stage", W.STAGE, "--workgroup", wg,
            "--cms-api-endpoint", s.w.cms.url, "--run-id", run_id, *extra]


def run(s: SimpleNamespace, *extra: str, **kw: Any) -> tuple[int, dict, str, str]:
    rc, out, err = s.w.run_main(argv(s, *extra, **kw))
    return rc, rows(out), out, err


def set_outcomes(s: SimpleNamespace, outcomes: list[dict]) -> None:
    s.dirty = True
    _refresh(s.w, outcomes)


def finding(s: SimpleNamespace, fid: str) -> dict:
    return next(f for f in s.w.items(W.FINDINGS) if f["finding_id"] == fid)


def put_finding(s: SimpleNamespace, item: dict) -> None:
    s.w._res().Table(W.FINDINGS).put_item(Item=item)


def outcome_entry(item: dict) -> dict:
    return next(e for e in item["evidence"] if e["kind"] == "rebalance.recent_outcomes")


def target_snap(s: SimpleNamespace, qid: str | None = None) -> list[str | None]:
    res = s.ra.res[qid or s.agent_qid]
    return next(r for r in res[1:] if r[0] == "loc-aus-hub2" and r[1] == s.eff)


# ---------------------------------------------------------------------------
# The whole part
# ---------------------------------------------------------------------------


def test_review_passes_and_writes_nothing_but_the_temp_operator(s):
    rc, r, out, err = run(s, "--allow-writes")
    assert rc == 0, (out, err)
    for name in (R1, R2, R3, R4):
        assert r.get(name, ("missing",))[0] == "PASS", (name, r.get(name), out)
    assert s.agent_qid in r[R1][1] and s.today.isoformat() in r[R1][1], r[R1]
    assert s.aid in r[R2][1], r[R2]
    assert s.a["finding_id"] in r[R3][1], r[R3]
    assert s.bid in r[R4][1] and f"Review on {(s.today + dt.timedelta(days=8)).isoformat()}" in r[R4][1], r[R4]
    # One temporary operator, deleted; nothing else written; no query started.
    creates = [c for c in s.calls() if c[:2] == ("cognito-idp", "AdminCreateUser")]
    assert len(creates) == 1 and creates[0][2]["Username"].startswith("livetest-fi-rebal-g4-"), creates
    assert len(s.w.pool.deleted) - s.deleted0 == 1, s.w.pool.deleted
    ops = {(svc, op) for svc, op, _ in s.calls()}
    writes = {(svc, op) for svc, op in ops if op in WRITE_OPS}
    assert not writes, writes
    assert ("athena", "ListQueryExecutions") in ops


def test_without_allow_writes_the_route_items_skip_and_no_user_is_made(s):
    rc, r, out, err = run(s)
    assert rc == 2, (out, err)
    assert r[R1][0] == "PASS" and r[R3][0] == "PASS", r
    assert r[R2][0] == "SKIP" and r[R4][0] == "SKIP", r
    assert not [c for c in s.calls() if c[0] == "cognito-idp" and c[1].startswith("Admin")]


# ---------------------------------------------------------------------------
# R1: the snapshot row, as the first agent run on or after the review date read it
# ---------------------------------------------------------------------------


def test_r1_skips_before_a_run_on_the_review_date(s):
    runs = s.w._res().Table(W.RUNS)
    r2 = next(x for x in s.w.items(W.RUNS) if x["started_at"] == s.run2["started_at"])
    runs.delete_item(Key={"agent_component": r2["agent_component"], "started_at": r2["started_at"]})
    rc, r, out, err = run(s)
    assert rc == 2 and r[R1][0] == "SKIP" and r[R3][0] == "SKIP", (r, err)
    assert s.review_on in r[R1][1], r[R1]


def test_r1_uses_the_first_run_on_the_review_date_not_the_newest(s):
    later = {**next(x for x in s.w.items(W.RUNS) if x["started_at"] == s.run2["started_at"])}
    nxt = _ts(s.run2["started_at"]) + dt.timedelta(days=1)
    later["started_at"] = nxt.isoformat().replace("+00:00", "Z")
    later["finished_at"] = (nxt + dt.timedelta(minutes=2)).isoformat().replace("+00:00", "Z")
    s.w._res().Table(W.RUNS).put_item(Item=later)
    rc, r, out, err = run(s)
    assert r[R1][0] == "PASS" and s.run2["started_at"] in r[R1][1], r[R1]


def test_r1_fails_when_the_run_read_no_outcomes(s):
    del s.ra.ex[s.agent_qid]
    rc, r, out, err = run(s)
    assert rc == 1 and r[R1][0] == "FAIL" and r[R3][0] == "SKIP", r


def test_r1_fails_in_another_workgroup(s):
    rc, r, out, err = run(s, wg="cms-staging-analytics")
    assert rc == 1 and r[R1][0] == "FAIL", r


def test_r1_fails_when_the_agent_query_failed(s):
    s.ra.ex[s.agent_qid]["Status"]["State"] = "FAILED"
    rc, r, out, err = run(s)
    assert rc == 1 and r[R1][0] == "FAIL", r


def test_r1_fails_without_the_moves_row(s):
    res = s.ra.res[s.agent_qid]
    s.ra.res[s.agent_qid] = [x for x in res if not (x[0] == "loc-aus-hub2" and x[1] == s.eff)]
    rc, r, out, err = run(s)
    assert rc == 1 and r[R1][0] == "FAIL", r


@pytest.mark.parametrize("col", [2, 3, 4])
def test_r1_fails_on_a_missing_figure(s, col):
    target_snap(s)[col] = None
    rc, r, out, err = run(s)
    assert rc == 1 and r[R1][0] == "FAIL", r


def test_r1_fails_on_a_ratio_that_is_not_actual_over_predicted(s):
    row = target_snap(s)
    row[2] = repr(float(row[2]) * 1.01)
    rc, r, out, err = run(s)
    assert rc == 1 and r[R1][0] == "FAIL" and "ratio" in r[R1][1], r


def test_r1_counts_zero_gain_as_a_figure(s):
    row = target_snap(s)
    row[2], row[4] = "0.0", "0.0"
    rc, r, out, err = run(s)
    assert r[R1][0] == "PASS", r


def test_r1_fails_on_a_snapshot_from_before_the_review_date(s):
    old = (dt.date.fromisoformat(s.review_on) - dt.timedelta(days=1)).isoformat()
    for x in s.ra.res[s.agent_qid][1:]:
        x[5] = old
    rc, r, out, err = run(s)
    assert rc == 1 and r[R1][0] == "FAIL", r


def test_r1_refuses_a_named_run_before_the_review_date(s):
    rc, r, out, err = run(s, "--agent-run", s.run1["started_at"])
    assert rc == 1 and r[R1][0] == "FAIL" and s.review_on in r[R1][1], r


def test_r1_reads_the_named_run(s):
    rc, r, out, err = run(s, "--agent-run", s.run2["started_at"])
    assert r[R1][0] == "PASS" and s.run2["started_at"] in r[R1][1], r


def test_r1_fails_on_a_named_run_that_does_not_exist(s):
    rc, r, out, err = run(s, "--agent-run", f"{s.today.isoformat()}T06:00:45.000Z")
    assert rc == 1 and r[R1][0] == "FAIL", r


def test_r1_fails_on_a_named_run_that_did_not_complete(s):
    item = next(x for x in s.w.items(W.RUNS) if x["started_at"] == s.run2["started_at"])
    s.w._res().Table(W.RUNS).put_item(Item={**item, "status": "aborted"})
    rc, r, out, err = run(s, "--agent-run", s.run2["started_at"])
    assert rc == 1 and r[R1][0] == "FAIL" and "aborted" in r[R1][1], r


def test_r1_fails_on_two_reads_in_the_run(s):
    ex = s.ra.ex[s.agent_qid]
    s.ra.add(ex["Query"], AGENT_WG, ex["Status"]["SubmissionDateTime"], s.snap)
    rc, r, out, err = run(s)
    assert rc == 1 and r[R1][0] == "FAIL" and "2 select_outcomes" in r[R1][1], r


def test_r1_fails_when_the_history_cannot_be_read_whole(s, monkeypatch):
    real = s.ra.batch

    def batch(p: dict) -> dict:
        out = real(p)
        out["UnprocessedQueryExecutionIds"] = [{"QueryExecutionId": "x", "ErrorCode": "Throttling"}]
        return out

    monkeypatch.setitem(s.w.router._handlers, ("athena", "BatchGetQueryExecution"), batch)
    rc, r, out, err = run(s)
    assert rc == 1 and r[R1][0] == "FAIL" and "could not be read" in r[R1][1], r


# ---------------------------------------------------------------------------
# R2: CMS's Rebalance history route, as the car-share operator
# ---------------------------------------------------------------------------


def test_r2_fails_without_an_outcome_after_the_refresh(s):
    set_outcomes(s, [])
    rc, r, out, err = run(s, "--allow-writes")
    assert rc == 1 and r[R2][0] == "FAIL", r


def test_r2_skips_without_an_outcome_before_the_refresh_on_the_review_date(s, monkeypatch):
    set_outcomes(s, [])
    at = dt.datetime.fromisoformat(f"{s.review_on}T06:30:00+00:00")
    monkeypatch.setattr(s.w.mod, "_wall", lambda: at)
    rc, r, out, err = run(s, "--allow-writes")
    assert rc == 2 and r[R2][0] == "SKIP" and "06:40" in r[R2][1], r


@pytest.mark.parametrize("field", ["actual_on_rent_hours_gain", "predicted_on_rent_hours_gain"])
def test_r2_fails_when_the_route_differs_from_the_snapshot(s, field):
    """Ratio kept consistent, so only the comparison with R1's row can fail it (a ratio that
    differs alone is R2's own ratio check, pinned below)."""
    v = dict(s.view)
    v[field] = repr(float(v[field]) + 1.0)
    v["ratio_hours"] = repr(float(v["actual_on_rent_hours_gain"])
                            / float(v["predicted_on_rent_hours_gain"]))
    set_outcomes(s, [v])
    rc, r, out, err = run(s, "--allow-writes")
    assert rc == 1 and r[R1][0] == "PASS" and r[R2][0] == "FAIL", r


def test_r2_fails_on_a_ratio_that_is_not_actual_over_predicted(s, monkeypatch):
    v = dict(s.view)
    v["ratio_hours"] = repr(float(v["ratio_hours"]) * 1.01)
    set_outcomes(s, [v])
    # R1 not passing, so only R2's own ratio check can fail it.
    del s.ra.ex[s.agent_qid]
    rc, r, out, err = run(s, "--allow-writes")
    assert r[R2][0] == "FAIL" and "ratio" in r[R2][1], r


def test_r2_fails_when_outcomes_are_unavailable(s):
    real = s.w.ix.rebalance_source.load

    def load(kind: str, *a: Any, **k: Any) -> Any:
        if kind == "outcomes":
            raise s.w.lc.ViewUnavailable("world: outcomes view missing")
        return real(kind, *a, **k)

    with mock.patch.object(s.w.ix.rebalance_source, "load", load):
        rc, r, out, err = run(s, "--allow-writes")
    assert rc == 1 and r[R2][0] == "FAIL" and r[R4][0] == "FAIL", r
    assert "outcomesUnavailable" in r[R2][1] and "outcomesUnavailable" in r[R4][1], r


def test_r2_fails_when_the_route_does_not_list_the_move(s):
    s.w.defects["moves_list_drops"] = True
    rc, r, out, err = run(s, "--allow-writes")
    assert rc == 1 and r[R2][0] == "FAIL", r


@pytest.mark.parametrize("field,value", [("toLocationId", "loc-aus-downtown"),
                                         ("fromLocationId", "loc-atl-downtown"),
                                         ("effectiveDate", "2026-01-02"),
                                         ("vins", ["MRDAZ01S0WT000099"])])
def test_r2_fails_when_the_cms_record_is_another_move(s, field, value):
    rec = s.w.move(s.aid)
    rec[field] = value
    s.w._res().Table(W.MOVES).put_item(Item=rec)
    rc, r, out, err = run(s, "--allow-writes")
    assert rc == 1 and r[R2][0] == "FAIL" and "differs from the manifest" in r[R2][1], r


def test_r2_reads_every_page_of_the_route(s):
    """201 newer moves push the reviewed move to the route's second page (200 a page)."""
    moves = s.w._res().Table(W.MOVES)
    tomorrow = (s.today + dt.timedelta(days=1)).isoformat()
    with moves.batch_writer() as bw:
        for n in range(201):
            bw.put_item(Item={"fleetId": W.RENTAL, "moveId": f"AVX-ACT-pad-{n:04d}",
                              "decidedByKind": "operator", "fromLocationId": "loc-aus-downtown",
                              "toLocationId": "loc-aus-hub2", "vins": [W.vin(13)],
                              "effectiveDate": tomorrow,
                              "createdAt": f"{s.today.isoformat()}T00:00:{n % 60:02d}+00:00"})
    rc, r, out, err = run(s, "--allow-writes")
    assert r[R2][0] == "PASS", r


# ---------------------------------------------------------------------------
# R3: that run's evidence for the destination carries the outcome
# ---------------------------------------------------------------------------


def test_r3_fails_when_the_runs_move_lacks_the_outcome(s):
    item = finding(s, s.a["finding_id"])
    e = outcome_entry(item)
    e["value"] = [x for x in e["value"] if x["effective_date"] != s.eff]
    put_finding(s, item)
    rc, r, out, err = run(s)
    assert rc == 1 and r[R3][0] == "FAIL", r


@pytest.mark.parametrize("field", ["ratio_hours", "predicted_on_rent_hours_gain",
                                   "actual_on_rent_hours_gain"])
def test_r3_fails_on_other_figures(s, field):
    item = finding(s, s.a["finding_id"])
    x = next(x for x in outcome_entry(item)["value"] if x["effective_date"] == s.eff)
    x[field] = repr(float(x[field]) + 0.5)
    put_finding(s, item)
    rc, r, out, err = run(s)
    assert rc == 1 and r[R3][0] == "FAIL", r


def test_r3_fails_when_the_entry_cites_another_snapshot(s):
    item = finding(s, s.a["finding_id"])
    e = outcome_entry(item)
    e["source_ref"] = f"adp.move_outcomes_snapshot materialised_on={s.review_on}"
    put_finding(s, item)
    rc, r, out, err = run(s)
    assert rc == 1 and r[R3][0] == "FAIL", r


def test_r3_skips_when_the_run_wrote_no_move_into_the_destination(s):
    s.w._res().Table(W.FINDINGS).delete_item(Key={"finding_id": s.a["finding_id"]})
    rc, r, out, err = run(s)
    assert rc == 2 and r[R3][0] == "SKIP" and "--agent-run" in r[R3][1], r


def test_r3_fails_on_a_kept_move_with_evidence_from_before_the_review_date(s):
    """The real writer refreshes an unchanged move (same inputs_hash, which excludes
    recent_outcomes) without rewriting its evidence: the run's evidence then lacks the move."""
    item = finding(s, s.a["finding_id"])
    e = outcome_entry(item)
    e["value"] = []
    e["source_ref"] = "adp.move_outcomes_snapshot"
    e["computed_at"] = s.run1["started_at"][:19] + "Z"
    put_finding(s, item)
    rc, r, out, err = run(s)
    assert rc == 1 and r[R3][0] == "FAIL" and s.a["finding_id"] in r[R3][1], r
    assert "inputs_hash" in r[R3][1], r


def _kept_from_review_day(s: SimpleNamespace, materialised_on: str) -> None:
    item = finding(s, s.a["finding_id"])
    e = outcome_entry(item)
    e["computed_at"] = f"{s.review_on}T07:00:00Z"
    e["source_ref"] = f"adp.move_outcomes_snapshot materialised_on={materialised_on}"
    put_finding(s, item)


def test_r3_accepts_evidence_an_earlier_run_wrote_on_the_review_date(s):
    _kept_from_review_day(s, s.review_on)
    rc, r, out, err = run(s)
    assert r[R3][0] == "PASS", r


def test_r3_fails_on_kept_evidence_citing_an_older_snapshot(s):
    _kept_from_review_day(s, (s.today - dt.timedelta(days=5)).isoformat())
    rc, r, out, err = run(s)
    assert rc == 1 and r[R3][0] == "FAIL" and "kept recent_outcomes cite" in r[R3][1], r


def test_r3_fails_on_two_outcome_entries(s):
    item = finding(s, s.a["finding_id"])
    item["evidence"].append(copy.deepcopy(outcome_entry(item)))
    put_finding(s, item)
    rc, r, out, err = run(s)
    assert rc == 1 and r[R3][0] == "FAIL" and "2 recent_outcomes entries" in r[R3][1], r


def test_r3_ignores_a_move_from_another_run(s):
    item = finding(s, s.a["finding_id"])
    item["computed_at"] = s.run1["started_at"][:19] + "Z"
    put_finding(s, item)
    rc, r, out, err = run(s)
    assert rc == 2 and r[R3][0] == "SKIP", r


# ---------------------------------------------------------------------------
# R4: a move under 7 days old shows "Review on <date>"
# ---------------------------------------------------------------------------


def test_r4_fails_when_a_young_move_has_an_outcome(s):
    v = {**s.view, "move_id": s.bid, "to_location_id": "loc-atl-hub1",
         "from_location_id": "loc-atl-downtown"}
    set_outcomes(s, [s.view, v])
    rc, r, out, err = run(s, "--allow-writes")
    assert rc == 1 and r[R4][0] == "FAIL" and s.bid in r[R4][1], r
    assert "has an outcome" in r[R4][1], r


def test_r4_fails_on_a_wrong_review_date(s):
    with mock.patch.object(s.w.mv, "_REVIEW_OFFSET_DAYS", 8):
        rc, r, out, err = run(s, "--allow-writes")
    assert rc == 1 and r[R4][0] == "FAIL", r


def test_r4_skips_without_a_young_move(s):
    rec = s.w.move(s.bid)
    rec["effectiveDate"] = (s.today - dt.timedelta(days=10)).isoformat()
    s.w._res().Table(W.MOVES).put_item(Item=rec)
    rc, r, out, err = run(s, "--allow-writes")
    assert rc == 2 and r[R4][0] == "SKIP", r


def test_r4_counts_a_move_on_its_seventh_day_as_young(s):
    rec = s.w.move(s.bid)
    rec["effectiveDate"] = (s.today - dt.timedelta(days=6)).isoformat()
    s.w._res().Table(W.MOVES).put_item(Item=rec)
    rc, r, out, err = run(s, "--allow-writes")
    assert r[R4][0] == "PASS" and s.bid in r[R4][1], r


def test_r4_does_not_count_a_move_on_its_review_date(s):
    """Effective 7 days ago: ADP's view admits it today, so its outcome is expected, not young."""
    eff = (s.today - dt.timedelta(days=7)).isoformat()
    rec = s.w.move(s.bid)
    rec["effectiveDate"] = eff
    s.w._res().Table(W.MOVES).put_item(Item=rec)
    v = {**s.view, "move_id": s.bid, "to_location_id": "loc-atl-hub1",
         "from_location_id": "loc-atl-downtown", "effective_date": eff,
         "review_available_on": s.today.isoformat()}
    set_outcomes(s, [s.view, v])
    rc, r, out, err = run(s, "--allow-writes")
    assert rc == 2 and r[R4][0] == "SKIP", r


# ---------------------------------------------------------------------------
# Refusals (before any AWS call) and cleanup
# ---------------------------------------------------------------------------


def _tampered(s: SimpleNamespace, change: Any) -> str:
    rid = "20261020T120000Z-def456"
    m = json.loads((s.w.state_dir / f"{RUN_ID}.json").read_text())
    m["run_id"] = rid
    change(m)
    s.w.mod._save_manifest(m)
    return rid


@pytest.mark.parametrize("change", [
    lambda m: m.update(part="auto"),
    lambda m: m.update(stage="prod"),
    lambda m: m["move"].update(approved="pending"),
    lambda m: m["move"]["vins"].__setitem__(0, "X' OR '1'='1"),
    lambda m: m["move"].update(to="loc'; --"),
    lambda m: m["move"].update(effective_date="2026-13-40"),
    lambda m: m["move"].update(action_id="not-an-action"),
], ids=["part", "stage", "unapproved", "vin", "location", "date", "action"])
def test_a_bad_manifest_is_refused_before_any_call(s, change):
    rid = _tampered(s, change)
    rc, r, out, err = run(s, "--allow-writes", run_id=rid)
    assert rc == 2 and "REFUSED" in err and not s.calls(), (err, s.calls())


@pytest.mark.parametrize("extra", [
    ["--run-id", "../../etc/passwd"],
    ["--agent-run", "yesterday"],
    ["--cms-api-endpoint", "http://example.com/prod"],
], ids=["run-id", "agent-run", "endpoint"])
def test_bad_arguments_are_refused_before_any_call(s, extra):
    a = argv(s, "--allow-writes")
    for i in range(0, len(extra), 2):
        if extra[i] in a:
            a[a.index(extra[i]) + 1] = extra[i + 1]
        else:
            a += extra[i:i + 2]
    rc, out, err = s.w.run_main(a)
    assert rc == 2 and "REFUSED" in err and not s.calls(), (err, s.calls())


def test_a_missing_manifest_is_refused(s):
    rc, r, out, err = run(s, run_id="20261020T120000Z-ffffff")
    assert rc == 2 and "REFUSED" in err and not s.calls()


def test_a_temp_operator_left_behind_fails_the_run(s, monkeypatch):
    def refuse(p: dict) -> dict:
        raise W.AwsError("InternalErrorException", "world: delete refused", 500)

    monkeypatch.setitem(s.w.router._handlers, ("cognito-idp", "AdminDeleteUser"), refuse)
    rc, r, out, err = run(s, "--allow-writes")
    assert rc == 1 and r["cleanup"][0] == "FAIL" and "admin-delete-user" in r["cleanup"][1], r
    assert r[R2][0] == "PASS", r


def test_the_script_runs_as_a_file_and_imports_what_review_needs(tmp_path):
    script = W.REPO / "scripts" / "live-verify-fi-rebalancing.py"
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    code = ("import runpy; runpy.run_path(%r, run_name='probe'); "
            "from agents.fi_rebalancing.sources import _load; "
            "print(len(_load('select_outcomes.sql', stage='staging', fleet_id='demo-rental-carshare')))"
            % str(script))
    p = subprocess.run([sys.executable, "-I", "-c", code], cwd=tmp_path, env=env,
                       capture_output=True, text=True, timeout=120)
    assert p.returncode == 0 and int(p.stdout.strip()) > 100, (p.stdout, p.stderr)
