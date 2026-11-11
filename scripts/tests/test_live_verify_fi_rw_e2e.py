"""End-to-end tests of scripts/live-verify-fi-recall-warranty.py against the shared fake.

Every test drives the script's real ``main()`` through items it names, against
``scripts/tests/fi_rw_world`` (moto, DuckDB Athena, and the real AVX and DMS handlers,
tier2 runner, DMS stand-ins and candidates job). Nothing here is a fake written from
the script's own assumptions: what the script reads is what the real producers wrote.

Run (about 15 minutes; one fresh world per test):
    PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest scripts/tests/test_live_verify_fi_rw_e2e.py -q

Needs the DMS repo at ``~/guidance-for-dealer-management-system-on-aws`` (or
``FI_RW_WORLD_DMS_REPO``); the script loads DMS's stand-in code from there too, so the
tests cannot run without it and fail rather than skip.
"""
from __future__ import annotations

import datetime as dt
import json
import socket
import uuid
from typing import Any

import pytest

from scripts.tests.fi_rw_world import World
from scripts.tests.fi_rw_world import world as world_mod

ALL_FLAGS = ["--stage", "staging", "--allow-writes", "--allow-managed-writes", "--allow-paging"]
HEALTH = world_mod.HEALTH_FN
FINDINGS = "vsa-staging-avx-findings"
ACTIONS = "vsa-staging-avx-actions"


@pytest.fixture(autouse=True)
def _loopback_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """No connection may leave the machine: AWS is moto (in-process), the APIs are on
    127.0.0.1. A non-loopback connect fails the test."""
    real_connect = socket.socket.connect

    def connect(sock: socket.socket, address: Any) -> Any:
        host = address[0] if isinstance(address, tuple) else str(address)
        if host not in ("127.0.0.1", "::1", "localhost"):
            raise AssertionError(f"test tried to reach {address!r}")
        return real_connect(sock, address)

    monkeypatch.setattr(socket.socket, "connect", connect)


def items(*ns: int) -> list[str]:
    out: list[str] = []
    for n in ns:
        out += ["--item", str(n)]
    return out


def run(w: World, *ns: int, flags: list[str] = ALL_FLAGS) -> tuple[int, dict[str, str], str]:
    rc, text, _ = w.run_main(flags + items(*ns))
    return rc, w.rows(text), text


def fails(rows: dict[str, str]) -> list[str]:
    return sorted(k for k, v in rows.items() if v == "FAIL")


def assert_passed(rows: dict[str, str], *labels: str) -> None:
    missing = [lb for lb in labels if rows.get(lb) != "PASS"]
    assert not missing, f"not PASS: {[(lb, rows.get(lb)) for lb in missing]}; FAILs={fails(rows)}"


def findings(w: World, fleet: str | None = None) -> list[dict[str, Any]]:
    out = w.scan(FINDINGS)
    return [f for f in out if fleet is None or f.get("fleet_id") == fleet]


# ---------------------------------------------------------------------------
# Gates and flags
# ---------------------------------------------------------------------------


def test_without_live_tests_exits_2_and_calls_nothing() -> None:
    with World() as w:
        rc, text, _ = w.run_main(["--all"], live=False)
        assert rc == 2 and "SKIPPED" in text
        assert w.main_calls() == [] and w.avx_api.requests == [] and w.dms_api.requests == []


def test_stage_prod_refused_before_any_aws_call() -> None:
    with World() as w:
        rc, text, _ = w.run_main(["--stage", "prod", "--all", "--allow-writes"])
        assert rc == 1 and "refusing 'prod'" in text
        assert w.main_calls() == []


def test_all_without_write_flags_runs_read_only_items_and_writes_nothing() -> None:
    with World() as w:
        rc, text, _ = w.run_main(["--stage", "staging", "--all"])
        rows = w.rows(text)
        assert rc == 2
        for n in (2, 3, 4, 5, 7, 8, 9, 11):
            assert rows.get(f"item-{n}") == "SKIP", n
        assert_passed(rows, "1-view-vs-answer-key", "6-groundings", "10-broker-fi-skip")
        assert w.main_writes() == []
        assert [r for r in w.avx_api.requests + w.dms_api.requests if r["method"] != "GET"] == []
        assert w.main_invocations() == []


def test_all_items_pass_with_every_flag_and_leave_no_residue() -> None:
    with World() as w:
        rc, text, _ = w.run_main(ALL_FLAGS + ["--all"])
        rows = w.rows(text)
        assert fails(rows) == [] and rc == 0, text[-3000:]
        assert_passed(rows, "residue-findings-livetest", "residue-actions-livetest",
                      "residue-cognito-livetest", "residue-lambda-env-snapshot",
                      "residue-dms-5b-ro", "residue-dms-fi-bookings", "residue-dms-fi-claims")
        # backups of the six tables, taken before the first write
        writes = [(s, o) for s, o, _ in w.main_calls()
                  if o in ("PutItem", "UpdateItem", "Invoke", "CreateBackup")]
        assert writes[:6] == [("dynamodb", "CreateBackup")] * 6
        assert not [f for f in w.scan(FINDINGS) if f["finding_id"].startswith("LIVETEST")]
        assert w.pool.users == {}
        assert w.function_env()["FI_COST_GUARDRAIL_ID"] == "gr-world-0001"


# ---------------------------------------------------------------------------
# Item 1 — views vs the answer key
# ---------------------------------------------------------------------------


def test_item1_matches_the_answer_key() -> None:
    with World() as w:
        rc, rows, _ = run(w, 1)
        assert rc == 2 and fails(rows) == []
        assert_passed(rows, "1-answer-key-non-empty", "1-answer-key-summaries",
                      "1-managed-as-of", "1-view-vs-answer-key")


def test_item1_fails_when_the_view_loses_a_confirmed_vin() -> None:
    with World() as w:
        w.view_base = [r for r in w.view_base if not (r[0] == "MRD-2026-004" and r[1] == 2)]
        _, rows, text = run(w, 1)
        assert rows.get("1-view-vs-answer-key") == "FAIL"
        assert "MRD-2026-004 confirmed: missing=['MRDAZ01S0PC000002']" in text


def test_item1_does_not_count_a_dms_remedy_as_an_extra_plant() -> None:
    with World() as w:
        vin = w.view_base[0][1]  # a POPULATION row of MRD-2026-004
        w.put("dms-staging-repair-orders", {
            "ro_id": "c0ffee00-0000-5000-8000-000000000001", "dealer_id": "dealer-sacramento",
            "vehicle_vin": world_mod.seed.vin(vin), "status": "Ready",
            "opened_at": "2026-10-01T09:00:00+00:00", "updated_at": "2026-10-02T12:00:00+00:00",
            "lines": [{"line_no": 1, "pay_type": "recall", "recall_campaign_id": "MRD-2026-004"}]})
        _, rows, text = run(w, 1)
        assert_passed(rows, "1-view-vs-answer-key")
        assert "1-dms-path-remedies: 1" in text


def test_item1_fails_without_campaign_summary_rows() -> None:
    with World() as w:
        import duckdb
        con = duckdb.connect()
        con.execute("CREATE TABLE ak (vin VARCHAR, entity_kind VARCHAR, entity_id VARCHAR, "
                    "expected_verdict VARCHAR, notes VARCHAR, planted_at VARCHAR)")
        con.execute("INSERT INTO ak VALUES ('MRDAZ01S0PC000002','recall_campaign','MRD-2026-004',"
                    "'CONFIRMED','confirming_code=P0AB6','2026-10-09')")
        import os
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "ak.parquet")
            con.execute(f"COPY ak TO '{path}' (FORMAT PARQUET)")
            w.internal("s3").put_object(Bucket=world_mod.LAKE_BUCKET,
                                        Key=world_mod.AK_PREFIX + "recall_plants_answer_key.parquet",
                                        Body=open(path, "rb").read())
        _, rows, _ = run(w, 1)
        assert rows.get("1-answer-key-summaries") == "FAIL"
        assert "1-view-vs-answer-key" not in rows



# ---------------------------------------------------------------------------
# Item 2 — one recall and one warranty run on demand
# ---------------------------------------------------------------------------


def _soften_one_disclosure_after_recall_run(w: World) -> None:
    """After the run, one recall Finding's disclosure differs from the catalog text
    (as if the agent had paraphrased it): the script must catch it."""
    def hook(fn: str, event: dict, result: Any) -> None:
        if fn == HEALTH and event.get("component") == "recall":
            f = next(f for f in w.scan(FINDINGS)
                     if f.get("finding_kind") == "fleet.recall.vehicle" and f.get("disclosure_txt"))
            w.put(FINDINGS, {**f, "disclosure_txt": f["disclosure_txt"] + " Exercise caution."})
    w.lam.after_run.append(hook)


def test_item2_both_runs_complete_and_score() -> None:
    with World() as w:
        rc, rows, _ = run(w, 2)
        assert rc == 2 and fails(rows) == []
        assert_passed(rows, "2-recall-poll", "2-recall-completed", "2-recall-disclosure-txt",
                      "2-warranty-poll", "2-warranty-completed", "2-warranty-score")
        comps = [i["event"]["component"] for i in w.main_invocations()]
        assert comps == ["recall", "warranty"]
        assert [c[2]["InvocationType"] for c in w.main_calls()
                if c[1] == "Invoke"] == ["Event", "Event"]


def test_item2_an_aborted_run_fails_and_is_not_scored() -> None:
    with World() as w:
        w.athena.con.execute("DROP TABLE adp_staging_fleet_recall.recall_campaigns_received")
        w.athena.con.execute("CREATE TABLE adp_staging_fleet_recall.recall_campaigns_received "
                             "(campaign_id VARCHAR, summary VARCHAR, consequence VARCHAR, remedy VARCHAR)")
        _, rows, text = run(w, 2)
        assert rows.get("2-recall-completed") == "FAIL" and "status='aborted'" in text
        assert "2-recall-disclosure-txt" not in rows


def test_item2_finds_a_run_that_starts_before_the_invoke_returns() -> None:
    with World() as w:
        w.lam.start_async_inside_invoke = True
        _, rows, _ = run(w, 2)
        assert_passed(rows, "2-recall-completed", "2-warranty-completed")


def test_item2_a_disclosure_that_differs_from_the_catalog_fails() -> None:
    with World() as w:
        _soften_one_disclosure_after_recall_run(w)
        _, rows, text = run(w, 2)
        assert rows.get("2-recall-disclosure-txt") == "FAIL" and "mismatches=1" in text


def test_item2_a_flagged_denied_line_fails_the_warranty_score() -> None:
    with World() as w:
        denied = next(p for p in w.plants if p["verdict_plant"] == "denied")

        def hook(fn: str, event: dict, result: Any) -> None:
            if fn == HEALTH and event.get("component") == "warranty":
                base = next(f for f in w.scan(FINDINGS)
                            if f.get("finding_kind") == "fleet.warranty.claim_candidate")
                ev = [dict(e) for e in base["evidence"]]
                for e in ev:
                    if e["kind"] == "warranty.verdict":
                        e["value"] = {**e["value"], "verdict": "denied"}
                rec = json.loads(json.dumps(base["recommended_action"], default=str))
                rec["parameters"]["ro_id"] = denied["ro_id"]
                w.put(FINDINGS, {**base, "finding_id": "FIND-00000000-0000-4000-8000-00000000d3n1",
                                 "evidence": ev, "recommended_action": rec})
        w.lam.after_run.append(hook)
        _, rows, text = run(w, 2)
        assert rows.get("2-warranty-score") == "FAIL" and "denied_flagged=1" in text


def test_item2_refuses_a_blank_guardrail_without_invoking() -> None:
    with World() as w:
        w.set_function_env(HEALTH, FI_COST_GUARDRAIL_ID="")
        _, rows, _ = run(w, 2)
        assert rows.get("2-recall-guardrail") == "FAIL" and rows.get("2-warranty-guardrail") == "FAIL"
        assert w.main_invocations() == []


@pytest.mark.parametrize("hhmm", ["01:15", "02:51", "03:20", "04:14"])
def test_items_refuse_within_45_minutes_of_a_daily_schedule(hhmm: str) -> None:
    # 01:15 is 45 min before battery 02:00; 02:51 is 11 after brakes 02:40; 03:20 is
    # 40 before cost 04:00; 04:14 is 14 after it (cycle 3 C2's allowed starts).
    h, m = map(int, hhmm.split(":"))
    today = dt.datetime.now(dt.timezone.utc).date()
    with World(start=dt.datetime.combine(today, dt.time(h, m), dt.timezone.utc)) as w:
        _, rows, _ = run(w, 2)
        assert rows.get("2-recall-sched-timing") == "FAIL", rows
        assert w.main_invocations() == []


def test_items_run_46_minutes_or_more_from_every_daily_schedule() -> None:
    today = dt.datetime.now(dt.timezone.utc).date()
    with World(start=dt.datetime.combine(today, dt.time(6, 46), dt.timezone.utc)) as w:
        _, rows, _ = run(w, 2)
        assert "2-recall-sched-timing" not in rows and rows.get("2-recall-completed") == "PASS"


def test_main_refuses_writes_inside_the_scheduled_job_window() -> None:
    today = dt.datetime.now(dt.timezone.utc).date()
    with World(start=dt.datetime.combine(today, dt.time(5, 46), dt.timezone.utc)) as w:
        rc, text, _ = w.run_main(ALL_FLAGS + items(2))
        assert rc == 1 and "04:50–06:10" in text and w.main_calls() == []


def test_a_schedule_in_another_timezone_refuses() -> None:
    with World() as w:
        sch = w.internal("scheduler")
        full = sch.get_schedule(Name="vsa-staging-tier2-daily-cost-us-west-2")
        sch.delete_schedule(Name=full["Name"])
        sch.create_schedule(Name=full["Name"], ScheduleExpression=full["ScheduleExpression"],
                            ScheduleExpressionTimezone="America/New_York",
                            FlexibleTimeWindow={"Mode": "OFF"}, Target=full["Target"])
        _, rows, text = run(w, 2)
        assert rows.get("2-recall-sched-timing") == "FAIL" and "America/New_York" in text
        assert w.main_invocations() == []


def test_no_schedule_targeting_the_function_refuses() -> None:
    with World() as w:
        sch = w.internal("scheduler")
        for s in sch.list_schedules()["Schedules"]:
            sch.delete_schedule(Name=s["Name"], GroupName=s["GroupName"])
        _, rows, text = run(w, 2)
        assert rows.get("2-recall-sched-timing") == "FAIL" and "0 schedule(s) matched" in text


# ---------------------------------------------------------------------------
# Item 3 — re-run on unchanged inputs
# ---------------------------------------------------------------------------


def test_item3_rerun_writes_no_new_finding_and_calls_no_model() -> None:
    with World() as w:
        rc, rows, _ = run(w, 3)
        assert fails(rows) == [] and rc == 2
        assert_passed(rows, "3-recall-completed", "3-recall-idempotent", "3-warranty-completed",
                      "3-warranty-idempotent", "3-no-new-findings", "3-finding-counts")


def test_item3_fails_when_the_rerun_calls_the_model() -> None:
    # model unavailable: the scheduled runs wrote rules items, which a re-run re-plans
    with World(model_mode="unavailable") as w:
        _, rows, text = run(w, 3)
        assert rows.get("3-recall-idempotent") == "FAIL" and "model_calls=0," not in text.split(
            "3-recall-idempotent")[1][:80]


def test_item3_fails_when_the_rerun_writes_a_new_finding() -> None:
    with World() as w:
        def hook(fn: str, event: dict) -> None:
            if fn == HEALTH and event.get("component") == "recall":
                w.view_base = w.view_base + [("MRD-2026-004", 13, "POPULATION", False)]
        w.lam.before_run.append(hook)
        before = {f["finding_id"] for f in findings(w)}
        _, rows, text = run(w, 3)
        assert rows.get("3-no-new-findings") == "FAIL"
        new = {f["finding_id"] for f in findings(w)} - before
        assert new and f"{len(new)} new finding id(s)" in text, (len(new), text[-2500:])



def test_item3_refuses_a_blank_guardrail_without_invoking() -> None:
    with World() as w:
        w.set_function_env(HEALTH, FI_COST_GUARDRAIL_ID="")
        _, rows, _ = run(w, 3)
        assert rows.get("3-recall-guardrail") == "FAIL" and rows.get("3-warranty-guardrail") == "FAIL"
        assert w.main_invocations() == []


# ---------------------------------------------------------------------------
# Shared readers
# ---------------------------------------------------------------------------


def body(req: dict[str, Any]) -> Any:
    return json.loads(req["body"]) if req.get("body") else None


def approves(w: World) -> list[dict[str, Any]]:
    return w.requests("avx", "POST", "/findings/{finding_id}/approve")


def dms_ros(w: World, *, by_avx: bool = True) -> list[dict[str, Any]]:
    return [r for r in w.scan("dms-staging-repair-orders")
            if not by_avx or r.get("avx_action_id")]


def pass_details(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("[PASS] "):
            label, _, detail = s[7:].partition(": ")
            out[label] = detail
    return out


def test_no_pass_row_carries_failure_wording() -> None:
    """A PASS row must not read like a failure ("did not", "timeout", "no ... found"):
    the operator reads these rows on the live run."""
    import re
    with World() as w:
        _, text, _ = w.run_main(ALL_FLAGS + ["--all"])
        bad = {k: v for k, v in pass_details(text).items()
               if re.search(r"did not|timeout|has no |not found|\bno \w+ (found|Finding)|"
                            r"not remedied|not in \w|missing$|=False \(", v)}
        assert bad == {}, bad


# ---------------------------------------------------------------------------
# Item 4 — book one recall service and follow it to remedied
# ---------------------------------------------------------------------------


def test_item4_books_one_recall_service_end_to_end() -> None:
    with World() as w:
        before = {r["ro_id"] for r in dms_ros(w)}
        rc, rows, text = run(w, 4)
        assert fails(rows) == [], text[-3000:]
        assert_passed(rows, "4-planned-item", "4-no-foreign-fi", "4-not-remedied-before",
                      "4-approve", "4-ro-id", "4-ro-avx-action-id", "4-is-fi-booking",
                      "4-not-close-early", "4-ro-ready", "4-ro-recall-cid",
                      "4-action-completed", "4-remedied", "residue-dms-fi-bookings")
        # the bytes the script sent: one approve, of the planned Finding, with its dealer
        (req,) = approves(w)
        fid = req["path"].split("/")[-2]
        finding = next(f for f in findings(w) if f["finding_id"] == fid)
        dealer = finding["recommended_action"]["parameters"]["dealer_id"]
        assert body(req) == {"action_kind": "book_recall_service", "target_system": "dms",
                             "parameters": {"dealer_id": dealer}}
        assert finding["fleet_id"] == "demo-rental-carshare"
        assert req["headers"].get("Authorization", "").startswith("Bearer ")
        # exactly one RO, carrying this approve's Action id and the campaign's recall line
        new = [r for r in dms_ros(w) if r["ro_id"] not in before]
        assert len(new) == 1 and new[0]["status"] in ("Ready", "Delivered", "Invoiced")
        cid = finding["subject_key"].split("/")[0]
        assert any(ln.get("pay_type") == "recall" and ln.get("recall_campaign_id") == cid
                   for ln in new[0]["lines"])
        assert w.main_invocations(world_mod.DEALER_PROGRESS_FN)


def test_item4_fails_when_dms_fleet_projection_is_stale() -> None:
    # live 2026-10-10: the hourly reconcile fails on kms:Decrypt, so every DMS fleet
    # route answers 404; the booking must fail and item 4 must say so.
    with World(live_defects=frozenset({"dms_marker_stale"})) as w:
        before = {r["ro_id"] for r in dms_ros(w)}
        rc, rows, _ = run(w, 4)
        assert rc == 1 and "FAIL" in (rows.get("4-ro-id"), rows.get("4-approve"))
        assert {r["ro_id"] for r in dms_ros(w)} == before


def test_item4_skips_a_finding_dms_would_close_early() -> None:
    with World() as w:
        _, rows, text = run(w, 4)
        assert_passed(rows, "4-not-close-early")
        early = text.count("_would_close_early(")
        assert early >= 1


# ---------------------------------------------------------------------------
# Item 5 — a booking closed before work: 5a (FI) re-plans, 5b (non-FI) cancels
# ---------------------------------------------------------------------------


def test_item5_closed_before_work_cancels_and_replans() -> None:
    with World() as w:
        rc, rows, text = run(w, 5)
        assert fails(rows) == [], text[-3000:]
        assert_passed(rows, "5a-approve", "5a-ro-id", "5a-ro-draft", "5a-patch-close",
                      "5a-audit-by-dms-event", "5a-audit-note", "5a-action-cancelled",
                      "5a-queue-cancelled", "5a-recall-run", "5a-book2-finding",
                      "5b-approve", "5b-ro-id", "5b-non-fi", "5b-ro-draft", "5b-patch-close",
                      "5b-action-cancelled", "residue-dms-5b-ro", "residue-dms-fi-bookings")
        patches = w.requests("dms", "PATCH", "/api/dms/dealers/{dealerId}/repair-orders/{roId}")
        assert [body(p) for p in patches] == [{"status": "Closed"}, {"status": "Closed"}]
        a5, b5 = approves(w)
        assert body(a5)["action_kind"] == "book_recall_service"
        assert body(b5)["action_kind"] == "book_fleet_service"
        assert body(b5)["target_system"] == "dms"
        # 5a's re-run found the closed RO and wrote attempt 2; 5b's RO is gone
        assert [i["event"]["component"] for i in w.main_invocations()] == ["recall"]


def test_item5a_refuses_a_blank_guardrail_before_its_rerun() -> None:
    with World() as w:
        w.set_function_env(HEALTH, FI_COST_GUARDRAIL_ID="")
        _, rows, _ = run(w, 5)
        assert rows.get("5a-guardrail") == "FAIL"
        assert w.main_invocations() == []


def test_item5a_fails_when_the_rerun_aborts() -> None:
    with World() as w:
        def hook(fn: str, event: dict) -> None:
            if fn == HEALTH and event.get("component") == "recall":
                w.athena.con.execute(
                    "DROP TABLE adp_staging_fleet_recall.recall_campaigns_received")
                w.athena.con.execute(
                    "CREATE TABLE adp_staging_fleet_recall.recall_campaigns_received "
                    "(campaign_id VARCHAR, summary VARCHAR, consequence VARCHAR, remedy VARCHAR)")
        w.lam.before_run.append(hook)
        _, rows, text = run(w, 5)
        assert rows.get("5a-recall-run") == "FAIL" and "aborted" in text
        assert rows.get("5a-book2-finding") != "PASS"


# ---------------------------------------------------------------------------
# Item 6 — every P0 VIN the run selected got a ground item, nothing else did
# ---------------------------------------------------------------------------


def test_item6_fails_when_a_p0_vin_lost_its_ground_item() -> None:
    with World() as w:
        ground = [f for f in findings(w) if "/ground/" in str(f.get("subject_key"))]
        assert ground
        w.table(FINDINGS).delete_item(Key={"finding_id": ground[0]["finding_id"]})
        _, rows, text = run(w, 6, flags=["--stage", "staging"])
        assert rows.get("6-groundings") == "FAIL" and "missing_ground=[" in text
        assert "missing_ground=[]" not in text


# ---------------------------------------------------------------------------
# Item 7 — file one warranty claim and follow it to paid
# ---------------------------------------------------------------------------


def test_item7_files_one_claim_end_to_end() -> None:
    with World() as w:
        rc, rows, text = run(w, 7)
        assert fails(rows) == [], text[-3000:]
        assert_passed(rows, "7-planned-item", "7-approve", "7-action-executed", "7-claim-found",
                      "7-claim-status", "7-claimed-amount", "7-submitted-at", "7-is-fi-claim",
                      "7-verdict-approved", "7-adjudication-paid", "residue-dms-fi-claims")
        (req,) = approves(w)
        assert body(req) == {"action_kind": "submit_warranty_claim", "target_system": "dms",
                             "parameters": {}}
        fid = req["path"].split("/")[-2]
        finding = next(f for f in findings(w) if f["finding_id"] == fid)
        assert finding["fleet_id"] == "demo-rental-carshare"
        claims = [c for c in w.scan("dms-staging-warranty-claims") if c.get("avx_action_id")]
        assert len(claims) == 1 and claims[0]["status"] == "Submitted"


def test_item7_fails_when_the_claim_route_double_serializes() -> None:
    # live DMS: create_fleet_warranty_claim serializes its TransactWriteItems twice
    with World(live_defects=frozenset({"dms_claim_double_serialized"})) as w:
        rc, rows, _ = run(w, 7)
        assert rc == 1
        assert "FAIL" in (rows.get("7-action-executed"), rows.get("7-claim-found"))
        assert not [c for c in w.scan("dms-staging-warranty-claims") if c.get("avx_action_id")]


# ---------------------------------------------------------------------------
# Item 8 — another fleet's operator can't see or act; DMS refuses another fleet
# ---------------------------------------------------------------------------

_B_ROUTES = ("/findings/{finding_id}/approve", "/findings/{finding_id}/decline")


def test_item8_scope_probes_pass_and_send_post_scope_bodies() -> None:
    with World() as w:
        rc, rows, text = run(w, 8)
        assert fails(rows) == [], text[-3000:]
        assert_passed(rows, "8-get-fleet-a-200", "8-ctrl-visible", "8-ctrl-approve-400",
                      "8-ctrl-decline-200", "8-get-fleet-b", "8-approve-b1", "8-decline-b1",
                      "8-b1-no-action-row", "8-approve-b2", "8-decline-b2", "8-b2-no-action-row",
                      "8-approve-b3", "8-decline-b3", "8-b3-no-action-row",
                      "8-dms-scope-probe", "8-dms-claim-scope-probe",
                      "residue-findings-livetest", "residue-actions-livetest",
                      "residue-cognito-livetest")
        ctrl, *b = approves(w)
        assert body(ctrl) == {"action_kind": "record_engineering_decision",
                              "target_system": "adp", "parameters": {}}
        assert ctrl["status"] == 400 and "population-scoped" in ctrl["response"]
        assert [body(r)["action_kind"] for r in b] == [
            "book_recall_service", "ground_vehicle", "submit_warranty_claim"]
        assert all(r["status"] == 403 for r in b)
        declines = w.requests("avx", "POST", "/findings/{finding_id}/decline")
        assert [r["status"] for r in declines] == [200, 403, 403, 403]
        # DMS: same-fleet controls answer the post-scope 400 bodies; another fleet 404
        ros = w.requests("dms", "POST", "/api/dms/fleet/repair-orders")
        claims = w.requests("dms", "POST", "/api/dms/fleet/warranty-claims")
        assert [r["status"] for r in ros] == [400, 404]
        assert [r["status"] for r in claims] == [400, 404]
        assert "avx_action_id must be a valid UUID" in ros[0]["response"]
        assert "RO not found" in claims[0]["response"]
        for r in ros + claims:
            sent = body(r)
            assert sent["dealer_id"] == "dealer-sacramento" and sent["avx_action_id"] == "not-a-uuid"
        assert body(ros[0])["vehicle_vin"] != body(ros[1])["vehicle_vin"]
        # no Action row for any B Finding, and only the control's decision row was written
        livetest_actions = [a for a in w.scan(ACTIONS)
                            if str(a.get("finding_id", "")).startswith("LIVETEST")]
        assert livetest_actions == []


def test_item8_fails_when_avx_lets_another_fleet_approve_or_decline() -> None:
    """AVX's approve AND decline routes share _check_approve_scope; opened up (it returns
    the standing that admits, None denies), every B approve, decline and no-action-row
    row FAILs (each checked, cycle 3 a8/o5). The controls still pass, so the B rows come
    from the probes, not from the controls-failed branch."""
    with World() as w:
        w.handlers["findings"]._check_approve_scope = lambda claims, finding: "operator"
        _, rows, _ = run(w, 8)
        assert_passed(rows, "8-get-fleet-a-200", "8-ctrl-approve-400", "8-ctrl-decline-200")
        for b in ("b1", "b2", "b3"):
            for lb in (f"8-approve-{b}", f"8-decline-{b}", f"8-{b}-no-action-row"):
                assert rows.get(lb) == "FAIL", (lb, rows.get(lb))
        # the Actions the opened route wrote are left behind, and the residue check says so
        leaked = [a for a in w.scan(ACTIONS) if str(a.get("finding_id", "")).startswith("LIVETEST")]
        assert leaked and rows.get("residue-actions-livetest") == "FAIL"


def test_item8_fails_when_avx_lets_another_fleet_read_its_queue() -> None:
    with World() as w:
        fh = w.handlers["findings"]
        real = fh.check_fleet_scope
        fh.check_fleet_scope = lambda claims, fleet_id: (
            True if "LIVETEST-FI-RW-" in str(fleet_id) else real(claims, fleet_id))
        _, rows, _ = run(w, 8)
        assert rows.get("8-get-fleet-b") == "FAIL"


def test_item8_fails_when_dms_lets_another_fleet_book() -> None:
    import sys
    with World() as w:
        fm = sys.modules["source.handlers.fleet_membership"]
        fm.check_fleet_membership = lambda **kw: {"allowed": True, "reason": None,
                                                  "status_code": 200}
        _, rows, _ = run(w, 8)
        assert rows.get("8-dms-scope-probe") == "FAIL"
        assert rows.get("8-dms-claim-scope-probe") == "FAIL"


def test_item8_dms_probes_fail_when_the_fleet_projection_is_stale() -> None:
    with World(live_defects=frozenset({"dms_marker_stale"})) as w:
        _, rows, text = run(w, 8)
        assert rows.get("8-dms-scope-probe") == "FAIL" and "positive control failed" in text
        assert rows.get("8-dms-claim-scope-probe") == "FAIL"
        # the wrong-fleet probe is not sent when the same-fleet control fails
        assert len(w.requests("dms", "POST", "/api/dms/fleet/repair-orders")) == 1
        assert len(w.requests("dms", "POST", "/api/dms/fleet/warranty-claims")) == 1


# ---------------------------------------------------------------------------
# Item 9 — forced failure per component; alarm; env restored exactly
# ---------------------------------------------------------------------------


def _env_updates(w: World) -> list[dict[str, str]]:
    return [dict(p["Environment"]["Variables"]) for s, o, p in w.main_calls()
            if s == "lambda" and o == "UpdateFunctionConfiguration"]


def test_item9_forces_both_failures_alarms_and_restores_the_whole_env() -> None:
    import glob
    import os
    import stat
    seen_files: list[tuple[str, int, Any]] = []
    run_dirs: list[str] = []
    with World() as w:
        # a canary value: whatever else the run prints (table names in backup ids, the
        # function name), no env VALUE may reach the terminal (cycle 3 W8)
        canary = "canary-" + uuid.uuid4().hex
        w.set_function_env(HEALTH, FI_RW_WORLD_CANARY=canary)
        original = w.function_env()

        def on_update(p: dict[str, Any]) -> None:
            if not seen_files:
                # the newest run dir holding item 9's backup (only item 9 writes it)
                d = max((os.path.dirname(b) + "/" for b in
                         glob.glob("/tmp/fi-rw-c-g55-*/item9-env-backup.json")),
                        key=os.path.getmtime)
                run_dirs.append(d)
                for name in ("item9-env-backup.json", "item9-env-restore.json"):
                    path = os.path.join(d, name)
                    seen_files.append((name, stat.S_IMODE(os.stat(path).st_mode),
                                       json.loads(open(path).read())))
        w.observe("lambda", "UpdateFunctionConfiguration", on_update)
        rc, rows, text = run(w, 9)
        assert fails(rows) == [], text[-3000:]
        assert_passed(rows, "9-alarm-actions-topic", "9-topic-subscriber",
                      "9-recall-invoke-error", "9-recall-alarm", "9-recall-alarm-action",
                      "9-alarm-back-to-ok", "9-warranty-invoke-error", "9-warranty-alarm",
                      "9-warranty-alarm-action", "9-env-restored", "residue-lambda-env-snapshot")
        # override: the whole map with only the guardrail blanked; restore: the whole map
        ups = _env_updates(w)
        blanked = {**original, "FI_COST_GUARDRAIL_ID": ""}
        assert ups[0] == blanked and ups[-1] == original and ups.count(original) >= 1
        assert w.function_env() == original
        # the sync invokes failed before any write, one per component
        sync = [i for i in w.main_invocations() if not i.get("ok")]
        assert [i["event"]["component"] for i in sync] == ["recall", "warranty"]
        # the printed restore command names a {Variables}-only 0600 file; no value printed
        (bk_name, bk_mode, bk), (rs_name, rs_mode, rs) = seen_files
        assert rs == {"Variables": original} and rs_mode == 0o600 and bk_mode == 0o600
        assert bk["Variables"] == original
        assert "item9-env-restore.json" in text and "item9-env-backup.json" in text
        restore_line = next(ln for ln in text.splitlines() if "restore cmd" in ln)
        assert "--environment file://" in restore_line and "item9-env-restore.json" in restore_line
        assert canary not in text and original["FI_COST_GUARDRAIL_ID"] not in text
        # the files are removed after a verified restore
        assert not os.path.exists(run_dirs[0] + "item9-env-restore.json")


def test_item9_restores_after_a_sigterm_during_the_restore() -> None:
    import os
    import signal
    with World() as w:
        original = w.function_env()
        sent: list[bool] = []

        def on_update(p: dict[str, Any]) -> None:
            if dict(p["Environment"]["Variables"]) == original and not sent:
                sent.append(True)
                os.kill(os.getpid(), signal.SIGTERM)
        w.observe("lambda", "UpdateFunctionConfiguration", on_update)
        rc, text, _ = w.run_main(ALL_FLAGS + items(9))
        assert sent and w.system_exit == 143 and rc == 143
        assert w.function_env() == original
        rows = w.rows(text)
        assert rows["9-env-restored"] == "PASS" and rows["residue-lambda-env-snapshot"] == "PASS"


def test_item9_a_sigterm_during_the_override_wait_still_restores() -> None:
    import os
    import signal
    with World() as w:
        original = w.function_env()
        sent: list[bool] = []
        blank: list[bool] = [False]

        # Track the applied env from the update itself: reading it from inside a lambda
        # observer would call the observed operation again and recurse.
        def on_update(p: dict[str, Any]) -> None:
            blank[0] = p["Environment"]["Variables"].get("FI_COST_GUARDRAIL_ID") == ""

        def on_get(p: dict[str, Any]) -> None:
            if not sent and blank[0]:
                sent.append(True)
                os.kill(os.getpid(), signal.SIGTERM)
        w.observe("lambda", "UpdateFunctionConfiguration", on_update)
        w.observe("lambda", "GetFunctionConfiguration", on_get)
        rc, text, _ = w.run_main(ALL_FLAGS + items(9))
        assert sent and w.system_exit == 143
        assert w.function_env() == original
        assert w.main_invocations() == [] or all(
            i["event"]["component"] == "recall" for i in w.main_invocations())


def test_item9_fails_when_the_alarm_does_not_notify_the_topic() -> None:
    with World() as w:
        w.alarm.redefine(w.internal("cloudwatch"), alarm_actions=[], ok_actions=[])
        _, rows, _ = run(w, 9)
        assert rows.get("9-alarm-actions-topic") == "FAIL"
        assert rows.get("9-recall-alarm-action") == "FAIL"
        assert w.function_env()["FI_COST_GUARDRAIL_ID"] == "gr-world-0001"


def test_item9_fails_when_the_alarm_action_failed() -> None:
    with World() as w:
        w.alarm.action_state = "Failed"
        _, rows, text = run(w, 9)
        assert rows.get("9-recall-alarm-action") == "FAIL" and "0 succeeded / 1 total" in text


def test_item9_refuses_a_blank_guardrail_and_never_overrides() -> None:
    with World() as w:
        w.set_function_env(HEALTH, FI_COST_GUARDRAIL_ID="")
        _, rows, _ = run(w, 9)
        assert rows.get("9-guardrail-pre") == "FAIL"
        assert _env_updates(w) == [] and w.main_invocations() == []


def test_item9_skips_when_the_alarm_is_already_in_alarm() -> None:
    with World() as w:
        w.internal("cloudwatch").set_alarm_state(AlarmName=world_mod.ALARM, StateValue="ALARM",
                                                 StateReason="test")
        _, rows, _ = run(w, 9)
        assert rows["9"] == "SKIP" and _env_updates(w) == []


def test_item9_rechecks_the_schedules_after_the_override_and_restores() -> None:
    """A schedule that lands within 45 minutes during the override wait (B2): no invoke,
    and the env is restored."""
    with World() as w:
        original = w.function_env()
        added: list[bool] = []
        sch = w.internal("scheduler")
        tmpl = sch.get_schedule(Name="vsa-staging-tier2-daily-cost-us-west-2")

        def on_update(p: dict[str, Any]) -> None:
            if not added and dict(p["Environment"]["Variables"]).get("FI_COST_GUARDRAIL_ID") == "":
                added.append(True)
                soon = w.clock.now() + dt.timedelta(minutes=30)
                sch.create_schedule(Name="vsa-staging-tier2-daily-soon-us-west-2",
                                    ScheduleExpression=f"cron({soon.minute} {soon.hour} * * ? *)",
                                    ScheduleExpressionTimezone="Etc/UTC",
                                    FlexibleTimeWindow={"Mode": "OFF"}, Target=tmpl["Target"])
        w.observe("lambda", "UpdateFunctionConfiguration", on_update)
        _, rows, _ = run(w, 9)
        assert rows.get("9-recall-sched-timing-pre-invoke") == "FAIL"
        assert w.main_invocations() == []
        assert w.function_env() == original and rows["9-env-restored"] == "PASS"


# ---------------------------------------------------------------------------
# Item 10 — the broker skipped FI items of both kinds
# ---------------------------------------------------------------------------


def test_item10_fails_when_only_recall_items_were_skipped() -> None:
    with World() as w:
        logs = w.internal("logs")
        logs.delete_log_group(logGroupName=world_mod.BROKER_LOG_GROUP)
        logs.create_log_group(logGroupName=world_mod.BROKER_LOG_GROUP)
        w.athena.con.execute("UPDATE adp_staging_fleet_recall.recall_campaigns_received "
                             "SET catalog_version = catalog_version || '-r2'")
        w.lam.run(HEALTH, {"stage": "staging", "component": "recall"})
        w.pump()
        _, rows, text = run(w, 10, flags=["--stage", "staging"])
        assert rows.get("10-broker-fi-skip") == "FAIL" and "warranty_seen=False" in text


# ---------------------------------------------------------------------------
# Item 11 — the fleet manager's chat quotes the recall safety text
# ---------------------------------------------------------------------------


def test_item11_chat_quotes_the_safety_text() -> None:
    with World() as w:
        rc, rows, text = run(w, 11)
        assert fails(rows) == [], text[-3000:]
        assert_passed(rows, "11-trust-flag", "11-chat-200", "11-eval", "residue-findings-livetest",
                      "residue-cognito-livetest")
        (req,) = w.requests("avx", "POST", "/assistant/chat")
        sent = body(req)
        assert sent.get("personaId") == "fleet_manager" and sent.get("sessionId")
        assert "LIVETEST-FI-RW-SAFETY-TEXT-1" in sent.get("prompt", sent.get("message", ""))
        (tool,) = w.tool_results
        assert tool["available"] and tool["safety_text"]


def test_item11_fails_when_the_reply_softens_the_safety_text() -> None:
    with World() as w:
        real = w._agent_runtime

        def softened(p: dict[str, Any]) -> dict[str, Any]:
            out = real(p)
            data = json.loads(out["response"].read())
            data["result"] = "Please exercise caution with this vehicle."
            out["response"] = world_mod.streaming(json.dumps(data).encode())
            return out
        w.router.add("bedrock-agentcore", "InvokeAgentRuntime", softened)
        _, rows, _ = run(w, 11)
        assert rows.get("11-eval") == "FAIL"


def test_item11_fails_when_the_text_runtime_does_not_trust_fleet_claims() -> None:
    with World() as w:
        w.router.add("bedrock-agentcore-control", "GetAgentRuntime",
                     lambda p: {"agentRuntimeId": p.get("agentRuntimeId"), "status": "READY",
                                "environmentVariables": {}})
        _, rows, _ = run(w, 11)
        assert rows.get("11-trust-flag") == "FAIL"
        assert w.requests("avx", "POST", "/assistant/chat") == []



# ---------------------------------------------------------------------------
# Properties the old file pinned only by reading the script's source (cycle 3 W13).
# Each test drives main() and breaks the property in the world.
# ---------------------------------------------------------------------------


def _dms_ro_key(p: dict[str, Any]) -> str:
    """ro_id from a GetItem's params, plain (resource) or typed (client)."""
    k = (p.get("Key") or {}).get("ro_id")
    return str(k.get("S") if isinstance(k, dict) else k or "")


def test_item4_fails_when_the_stand_in_never_moves_the_ro_to_ready() -> None:
    with World() as w:
        w.lam.handlers[world_mod.DEALER_PROGRESS_FN] = lambda e, c: {"moved": 0}
        _, rows, text = run(w, 4)
        assert rows.get("4-ro-ready") == "FAIL" and "did not reach Ready" in text


def test_item4_fails_when_the_ro_lacks_the_campaigns_recall_line() -> None:
    with World() as w:
        def hook(fn: str, event: dict, result: Any) -> None:
            if fn == world_mod.DEALER_PROGRESS_FN:
                for ro in dms_ros(w):
                    lines = [dict(ln) for ln in ro.get("lines") or []]
                    for ln in lines:
                        if ln.get("pay_type") == "recall":
                            ln["recall_campaign_id"] = "MRD-2099-999"
                    w.put("dms-staging-repair-orders", {**ro, "lines": lines})
        w.lam.after_run.append(hook)
        _, rows, _ = run(w, 4)
        assert rows.get("4-ro-recall-cid") == "FAIL"


@pytest.mark.parametrize("which", ["5a", "5b"])
def test_item5_fails_when_the_ro_is_not_draft_before_the_close(which: str) -> None:
    """The Accept says "closed from Draft": an RO already past Draft fails the row."""
    with World() as w:
        seen: list[str] = []

        def on_get(p: dict[str, Any]) -> None:
            if p.get("TableName") != "dms-staging-repair-orders":
                return
            ro_id = _dms_ro_key(p)
            ro = next((r for r in dms_ros(w) if r["ro_id"] == ro_id), None)
            if ro is None or ro_id in seen:
                return
            seen.append(ro_id)
            if len(seen) == (1 if which == "5a" else 2):
                w.put("dms-staging-repair-orders", {**ro, "status": "In Progress"})
        w.observe("dynamodb", "GetItem", on_get)
        _, rows, _ = run(w, 5)
        assert rows[f"{which}-ro-draft"] == "FAIL", rows


def test_item7_fails_closed_when_the_actions_scan_errors() -> None:
    with World() as w:
        def on_scan(p: dict[str, Any]) -> None:
            if p.get("TableName") == ACTIONS:
                raise world_mod.AwsError("InternalServerError", "scan failed", status=500)
        w.observe("dynamodb", "Scan", on_scan)
        _, rows, _ = run(w, 7)
        assert rows.get("7-actions-scan") == "FAIL" and "7-planned-item" not in rows
        assert approves(w) == []


def test_item7_writes_the_pay_date_to_the_manifest() -> None:
    with World() as w:
        _, rows, _ = run(w, 7)
        assert_passed(rows, "7-claim-found")
        (claim,) = [c for c in w.scan("dms-staging-warranty-claims") if c.get("avx_action_id")]
        sub = dt.datetime.fromisoformat(str(claim["submitted_at"]).replace("Z", "+00:00"))
        due = sub + dt.timedelta(days=14)
        run6 = due.replace(hour=6, minute=0, second=0, microsecond=0)
        if run6 < due:
            run6 += dt.timedelta(days=1)
        m = w.manifest()["items"]
        assert m["item7_pay_date"] == run6.date().isoformat()
        import os
        import stat
        # created 0600 from the start and kept 0600 across updates (security c1)
        assert stat.S_IMODE(os.stat(w.manifests[-1]).st_mode) == 0o600
        assert stat.S_IMODE(os.stat(w.manifests[-1].parent).st_mode) == 0o700
        assert "dms_commit" in w.manifest()["items"] and "dms_dirty" in w.manifest()["items"]


def test_item8_probes_all_fail_and_none_is_sent_when_the_fleet_a_control_fails() -> None:
    with World() as w:
        def _vals(x: Any) -> str:
            # boto3 condition objects (resource calls) carry their values in get_expression()
            if hasattr(x, "get_expression"):
                return " ".join(_vals(v) for v in x.get_expression()["values"])
            if isinstance(x, (list, tuple)):
                return " ".join(_vals(v) for v in x)
            return json.dumps(x, default=str)

        def on_query(p: dict[str, Any]) -> None:
            vals = _vals(p.get("KeyConditionExpression")) + _vals(
                p.get("ExpressionAttributeValues") or {})
            if p.get("TableName") == FINDINGS and "LIVETEST-FI-RW-" in vals and '-A"' in vals:
                raise world_mod.AwsError("InternalServerError", "control read failed", status=500)
        w.observe("dynamodb", "Query", on_query)
        _, rows, _ = run(w, 8)
        assert rows.get("8-get-fleet-a-200") == "FAIL"
        for lb in ("8-get-fleet-b", "8-approve-b1", "8-decline-b1", "8-approve-b2",
                   "8-approve-b3", "8-b1-no-action-row"):
            assert rows[lb] == "FAIL", lb
        assert approves(w) == [] and w.requests("avx", "POST", "/findings/{finding_id}/decline") == []


def test_item9_finds_the_alarm_topic_on_a_later_list_topics_page() -> None:
    with World() as w:
        sns = w.internal("sns")
        sns.delete_topic(TopicArn=w.topic_arn)
        for i in range(120):
            sns.create_topic(Name=f"aa-filler-{i:03d}")
        arn = sns.create_topic(Name=world_mod.TOPIC)["TopicArn"]
        assert arn == w.topic_arn
        sns.subscribe(TopicArn=arn, Protocol="email", Endpoint="operator@example.invalid")
        first = sns.list_topics()
        assert first.get("NextToken") and arn not in [t["TopicArn"] for t in first["Topics"]]
        _, rows, _ = run(w, 9)
        assert_passed(rows, "9-alarm-actions-topic", "9-topic-subscriber", "9-env-restored")


def _schedule_soon(w: World, name: str, minutes: int = 30) -> None:
    sch = w.internal("scheduler")
    tmpl = sch.get_schedule(Name="vsa-staging-tier2-daily-cost-us-west-2")
    soon = w.clock.now() + dt.timedelta(minutes=minutes)
    sch.create_schedule(Name=name, ScheduleExpression=f"cron({soon.minute} {soon.hour} * * ? *)",
                        ScheduleExpressionTimezone="Etc/UTC", FlexibleTimeWindow={"Mode": "OFF"},
                        Target=tmpl["Target"])


@pytest.mark.parametrize("when,label", [
    # alarm already back to OK: the poll loop ends without a re-check, the pre-override
    # B2 check before the second component refuses (cycle 3 o10)
    ("ok", "9-warranty-sched-timing"),
    # alarm still in ALARM: the back-to-OK wait re-checks on its next poll (security c1 SW1)
    ("alarm", "9-sched-timing-during-wait"),
])
def test_item9_rechecks_the_schedules_before_the_second_component(when: str, label: str) -> None:
    """A schedule that lands within 45 minutes after the recall phase reached ALARM:
    warranty is not invoked, and the env is restored."""
    with World() as w:
        original = w.function_env()
        recall_alarmed: list[bool] = []
        added: list[bool] = []

        def on_history(p: dict[str, Any]) -> None:
            recall_alarmed.append(True)  # read after the recall phase reached ALARM

        def on_describe(p: dict[str, Any]) -> None:
            if recall_alarmed and not added and w.alarm.state == when.upper():
                added.append(True)
                _schedule_soon(w, "vsa-staging-tier2-daily-soon-us-west-2")
        w.observe("cloudwatch", "DescribeAlarmHistory", on_history)
        w.observe("cloudwatch", "DescribeAlarms", on_describe)
        _, rows, _ = run(w, 9)
        assert added and rows.get(label) == "FAIL", rows
        assert [i["event"]["component"] for i in w.main_invocations()] == ["recall"]
        assert w.function_env() == original and rows["9-env-restored"] == "PASS"


def test_one_items_exception_does_not_stop_the_items_after_it() -> None:
    with World() as w:
        mod = w.load_script()

        def boom(res: Any, **kw: Any) -> None:
            raise RuntimeError("item 6 broke")
        mod._ITEM_FNS[6] = boom
        rc, text, _ = w.run_main(["--stage", "staging"] + items(6, 10), mod)
        rows = w.rows(text)
        assert rows.get("6-exception") == "FAIL" and "item 6 broke" in text
        assert rows["10-broker-fi-skip"] == "PASS" and rc == 1


def test_the_write_window_is_rechecked_before_each_write_item() -> None:
    with World() as w:
        mod = w.load_script()
        answers = iter([False, False, True])  # gate 3, before item 2, before item 3
        mod._in_write_window = lambda: next(answers, True)
        _, text, _ = w.run_main(ALL_FLAGS + items(2, 3), mod)
        rows = w.rows(text)
        assert rows.get("3-write-window") == "FAIL"
        assert [i["event"]["component"] for i in w.main_invocations()] == ["recall", "warranty"]


def test_a_sigterm_inside_an_item_still_tears_down_and_checks_residue() -> None:
    import os
    import signal
    with World() as w:
        sent: list[bool] = []

        def on_create_user(p: dict[str, Any]) -> None:
            planted = [f for f in w.scan(FINDINGS) if f["finding_id"].startswith("LIVETEST")]
            if not sent and planted:
                sent.append(True)
                os.kill(os.getpid(), signal.SIGTERM)
        w.observe("cognito-idp", "AdminCreateUser", on_create_user)
        rc, text, _ = w.run_main(ALL_FLAGS + items(8))
        rows = w.rows(text)
        assert sent and rc == 143
        assert_passed(rows, "residue-findings-livetest", "residue-actions-livetest",
                      "residue-cognito-livetest")
        assert not [f for f in w.scan(FINDINGS) if f["finding_id"].startswith("LIVETEST")]
        assert w.pool.users == {}


def test_item11_plants_the_safety_phrases_only_in_the_disclosure() -> None:
    """The marker and both safety phrases live only in the disclosure, so a reply that
    carries them shows the tool's safety_text reached the reply (call 63, W10)."""
    with World() as w:
        planted: list[dict[str, Any]] = []

        def on_put(p: dict[str, Any]) -> None:
            item = p.get("Item") or {}
            fid = item.get("finding_id")
            fid = fid.get("S") if isinstance(fid, dict) else fid
            if p.get("TableName") == FINDINGS and fid == "LIVETEST-FI-RW-SAFETY-TEXT-1":
                planted.append(item)
        w.observe("dynamodb", "PutItem", on_put)
        _, rows, _ = run(w, 11)
        assert_passed(rows, "11-eval")
        (f,) = planted
        disclosure = str(f["disclosure_txt"]).lower()
        for phrase in ("do not drive", "park outside", "livetest-d18"):
            assert phrase in disclosure
            assert phrase not in str(f.get("headline", "")).lower()
            assert phrase not in str(f.get("subject_key", "")).lower()



def test_item5a_follows_the_fleet_queue_across_pages() -> None:
    """The fleet queue route pages (limit 50 live; 3 here): the 5a item is found on a
    later page, so the queue check must follow next_token."""
    with World() as w:
        fh = w.handlers["findings"]
        real = fh._list_findings_by_fleet
        fh._list_findings_by_fleet = (
            lambda fid, limit=50, next_token=None: real(fid, 3, next_token))
        _, rows, _ = run(w, 5)
        assert_passed(rows, "5a-queue-cancelled")
        assert len(w.requests("avx", "GET", "/findings/fleet/{fleet_id}")) > 1


def test_item7_scans_the_actions_table_once_not_per_finding() -> None:
    with World() as w:
        _, rows, _ = run(w, 7)
        assert_passed(rows, "7-planned-item")
        warranty = [f for f in findings(w, "demo-rental-carshare")
                    if f.get("finding_kind") == "fleet.warranty.claim_candidate"]
        scans = [p for s, o, p in w.main_calls()
                 if s == "dynamodb" and o == "Scan" and p.get("TableName") == ACTIONS]
        # The pick's scan is the only unfiltered first-page scan of Actions (the residue
        # check filters on the LIVETEST prefix); one paged scan, not one per Finding
        # (review rebuild c1 rB4: a per-Finding scan that stops early passed `<`).
        starts = [p for p in scans if set(p) == {"TableName"}]
        assert len(warranty) >= 3 and len(starts) == 1, (len(starts), len(scans), len(warranty))



def test_item7_adjudicates_with_the_dms_trees_own_rule(tmp_path: Any) -> None:
    """Item 7 runs DMS's _adjudicate_fi_claims from --dms-repo, not a copy: a DMS tree
    whose rule never pays makes 7-adjudication-paid FAIL (brief D1)."""
    import shutil
    src = world_mod.DMS_ROOT / "source" / "simulation" / "fi_stand_ins.py"
    dst = tmp_path / "source" / "simulation" / "fi_stand_ins.py"
    dst.parent.mkdir(parents=True)
    shutil.copy(src, dst)
    with dst.open("a") as fh:
        fh.write("\n\ndef _adjudicate_fi_claims(claims, at=None, **kw):\n"
                 "    return {'paid': 0, 'rejected': 0}  # never pays\n")
    with World() as w:
        rc, text, _ = w.run_main(ALL_FLAGS + items(7) + ["--dms-repo", str(tmp_path)])
        rows = w.rows(text)
        assert_passed(rows, "7-claim-found")
        assert rows.get("7-adjudication-paid") == "FAIL" and rc == 1


def test_a_disabled_schedule_inside_the_window_does_not_refuse() -> None:
    """Other windows create new schedules DISABLED on this Lambda; a disabled schedule
    invokes nothing, so it must not refuse the run. The same schedule enabled refuses."""
    for state, refused in (("DISABLED", False), ("ENABLED", True)):
        with World() as w:
            sch = w.internal("scheduler")
            tmpl = sch.get_schedule(Name="vsa-staging-tier2-daily-cost-us-west-2")
            soon = w.clock.now() + dt.timedelta(minutes=10)
            sch.create_schedule(Name="vsa-staging-tier2-new-us-west-2", State=state,
                                ScheduleExpression=f"cron({soon.minute} {soon.hour} * * ? *)",
                                ScheduleExpressionTimezone="Etc/UTC",
                                FlexibleTimeWindow={"Mode": "OFF"}, Target=tmpl["Target"])
            _, rows, text = run(w, 2)
            if refused:
                assert rows.get("2-recall-sched-timing") == "FAIL", state
                assert w.main_invocations() == []
            else:
                assert "2-recall-sched-timing" not in rows, state
                assert_passed(rows, "2-recall-completed", "2-warranty-completed")



def test_item1_fails_when_the_view_mixes_two_cohort_snapshots() -> None:
    """managed_as_of is one cohort snapshot for every row; two values mean the view read a
    stale or half-rebuilt managed_vehicles table."""
    with World() as w:
        real = w.view_rows

        def two_snapshots() -> list[dict[str, Any]]:
            rows = real()
            rows[0] = {**rows[0], "managed_as_of": w.managed_as_of - dt.timedelta(days=1)}
            return rows
        w.view_rows = two_snapshots  # type: ignore[method-assign]
        _, rows, _ = run(w, 1)
        assert rows.get("1-managed-as-of") == "FAIL"


def test_item4_books_nothing_when_dms_would_close_every_candidates_ro_early(tmp_path: Any) -> None:
    """Item 4 skips a Finding whose deterministic RO DMS's stand-in would close early,
    BEFORE approving it: with a DMS tree that closes every RO early, nothing is approved."""
    import shutil
    shutil.copytree(world_mod.DMS_ROOT / "source", tmp_path / "source")
    with (tmp_path / "source" / "simulation" / "fi_stand_ins.py").open("a") as fh:
        fh.write("\n\ndef _would_close_early(ro_id):\n    return True  # every RO closes early\n")
    with World() as w:
        _, text, _ = w.run_main(ALL_FLAGS + items(4) + ["--dms-repo", str(tmp_path)])
        rows = w.rows(text)
        assert rows.get("4-planned-item") == "FAIL"
        assert approves(w) == []



# ---------------------------------------------------------------------------
# Rebuild review cycle 1 (review-g55-rebuild-cycle1.md): Warnings WA1-WA3, WB1, WB2 and
# the mutant candidates that survived (rA1, rA3, rA4, rB1-rB3).
# ---------------------------------------------------------------------------


def _ground_pairs(w: World) -> list[tuple[str, str]]:
    out = []
    for f in findings(w):
        sk = str(f.get("subject_key") or "")
        if "/ground/" in sk:
            out.append((sk.split("/")[0], str((f.get("scope") or {}).get("vin"))))
    return sorted(set(out))


def _ready_recall_ro(w: World, vin: str, cid: str, updated_at: str, n: int = 1) -> None:
    w.put("dms-staging-repair-orders", {
        "ro_id": f"c0ffee00-0000-5000-8000-{n:012d}", "dealer_id": "dealer-sacramento",
        "vehicle_vin": vin, "status": "Ready", "opened_at": updated_at,
        "updated_at": updated_at,
        "lines": [{"line_no": 1, "pay_type": "recall", "recall_campaign_id": cid}]})


def test_item6_passes_when_a_grounded_p0_pair_is_remedied_after_the_run() -> None:
    """The 05:40 dealer-progress stand-in (or item 4) can remedy a P0 pair after the
    05:00 run grounded it: that ground item is not a leak, and its VIN still counted a
    grounding read error (WA1)."""
    with World() as w:
        cid, vin = _ground_pairs(w)[0]
        _ready_recall_ro(w, vin, cid, w.today.isoformat() + "T05:45:00+00:00")
        _, rows, text = run(w, 6, flags=["--stage", "staging"])
        assert rows.get("6-groundings") == "PASS", text[-1500:]
        assert "remedied_since_run=1" in text


def test_item6_still_fails_on_a_ground_item_for_a_pair_remedied_before_the_run() -> None:
    with World() as w:
        cid, vin = _ground_pairs(w)[0]
        before = (w.today - dt.timedelta(days=1)).isoformat() + "T05:45:00+00:00"
        _ready_recall_ro(w, vin, cid, before)
        _, rows, text = run(w, 6, flags=["--stage", "staging"])
        assert rows.get("6-groundings") == "FAIL" and "non_p0_ground=[(" in text


def test_item6_ignores_an_earlier_runs_ground_item_on_another_pair() -> None:
    """Ground items live 3 days; one an earlier run wrote for a pair no longer selected
    is that run's, not the scored run's (WA1)."""
    with World() as w:
        g = next(f for f in findings(w) if "/ground/" in str(f.get("subject_key")))
        other = next(r for r in w.view_rows() if not r["remedied"]
                     and (r["campaign_id"], r["vin"]) not in _ground_pairs(w))
        old = (w.today - dt.timedelta(days=2)).isoformat() + "T05:00:00Z"
        w.put(FINDINGS, {**g, "finding_id": "FIND-00000000-0000-4000-8000-0000000e0001",
                         "subject_key": f"{other['campaign_id']}/ground/1",
                         "scope": {**g["scope"], "vin": other["vin"]}, "computed_at": old})
        _, rows, text = run(w, 6, flags=["--stage", "staging"])
        assert rows.get("6-groundings") == "PASS" and "earlier_runs_items=1" in text, text[-1500:]


def test_item9_restores_when_a_sigterm_lands_after_lambda_accepted_the_blank_value() -> None:
    """Lambda applies the override, then the update call is interrupted (WA2): the env is
    still restored, not reported 'no override applied'."""
    import os
    import signal
    with World() as w:
        original = w.function_env()
        state = {"inside": False, "sent": False}

        def on_update(p: dict[str, Any]) -> None:
            if state["inside"] or state["sent"]:
                return
            if p["Environment"]["Variables"].get("FI_COST_GUARDRAIL_ID") == "":
                state["inside"] = True
                w.set_function_env(HEALTH, FI_COST_GUARDRAIL_ID="")  # Lambda accepted it
                state["inside"] = False
                state["sent"] = True
                os.kill(os.getpid(), signal.SIGTERM)  # ... and the call never returns
        w.observe("lambda", "UpdateFunctionConfiguration", on_update)
        rc, text, _ = w.run_main(ALL_FLAGS + items(9))
        rows = w.rows(text)
        assert state["sent"] and rc == 143
        assert w.function_env() == original
        assert rows.get("9-env-restored") == "PASS" and "no override applied" not in text


def _decide_all_book_findings(w: World) -> None:
    from agents.supervisor.tools.fleet_recommendation import _derive_decision_action_id
    for f in findings(w):
        if "/book/1" in str(f.get("subject_key") or ""):
            w.put(ACTIONS, {"action_id": _derive_decision_action_id(f["finding_id"]),
                            "finding_id": f["finding_id"], "fleet_id": f.get("fleet_id"),
                            "action_kind": "book_recall_service", "status": "declined",
                            "created_at": w.clock.iso()})


@pytest.mark.parametrize("item", [4, 5])
def test_items_4_and_5a_skip_findings_that_already_have_their_decision(item: int) -> None:
    """A Finding with its decision Action is not pending: the plan must skip it before
    approving (the handler would answer 409). With every /book/1 Finding decided, nothing
    is approved (WA3)."""
    with World() as w:
        _decide_all_book_findings(w)
        _, rows, _ = run(w, item)
        label = "4-planned-item" if item == 4 else "5a-planned-item"
        assert rows.get(label) == "FAIL", rows
        assert [b for b in approves(w) if body(b)["action_kind"] == "book_recall_service"] == []


@pytest.mark.parametrize("item", [4, 5])
def test_items_4_and_5a_fail_closed_when_the_decision_read_errors(item: int) -> None:
    with World() as w:
        def on_get(p: dict[str, Any]) -> None:
            if p.get("TableName") == ACTIONS:
                raise world_mod.AwsError("InternalServerError", "read failed", status=500)
        w.observe("dynamodb", "GetItem", on_get)
        _, rows, _ = run(w, item)
        label = "4-actions-read" if item == 4 else "5a-actions-read"
        assert rows.get(label) == "FAIL", rows
        assert [b for b in approves(w) if body(b)["action_kind"] == "book_recall_service"] == []


def test_item1_counts_a_planted_remedy_that_dms_also_remedied() -> None:
    """The view prefers the DMS remedy (COALESCE(rdms, radp)): a planted ADP-path VIN that
    also has a Ready DMS recall RO shows the DMS ro_id and is still remedied (WB1)."""
    with World() as w:
        cid, i, _, _ = next(b for b in w.view_base if b[3])
        _ready_recall_ro(w, world_mod.seed.vin(i), cid, w.today.isoformat() + "T01:00:00+00:00")
        row = next(r for r in w.view_rows()
                   if r["campaign_id"] == cid and r["vin"] == world_mod.seed.vin(i))
        assert row["remedy_ref"].startswith("c0ffee00")  # the world view prefers DMS too
        _, rows, text = run(w, 1)
        assert_passed(rows, "1-view-vs-answer-key")
        assert "1-dms-path-remedies: 1" in text


def test_item5a_fails_when_its_finding_is_missing_from_every_queue_page() -> None:
    """WB2: the fail-closed half of the queue check."""
    with World() as w:
        from agents.supervisor.tools.fleet_recommendation import _derive_decision_action_id
        fh = w.handlers["findings"]
        real = fh._list_findings_by_fleet

        def without_cancelled(fid: str, limit: int = 50, next_token: Any = None) -> Any:
            got, tok = real(fid, 3, next_token)
            keep = [f for f in got if (w.table(ACTIONS).get_item(
                Key={"action_id": _derive_decision_action_id(f["finding_id"])}).get("Item")
                or {}).get("status") != "cancelled"]
            return keep, tok
        fh._list_findings_by_fleet = without_cancelled
        _, rows, text = run(w, 5)
        assert rows.get("5a-queue-cancelled") == "FAIL" and "not found in any page" in text
        assert len(w.requests("avx", "GET", "/findings/fleet/{fleet_id}")) > 1


def test_item11_sends_no_chat_when_the_trust_read_fails() -> None:
    with World() as w:
        def boom(p: dict[str, Any]) -> Any:
            raise world_mod.AwsError("AccessDeniedException", "denied", status=403)
        w.router.add("bedrock-agentcore-control", "ListAgentRuntimes", boom)
        _, rows, _ = run(w, 11)
        assert rows.get("11-trust-flag") == "FAIL"
        assert w.requests("avx", "POST", "/assistant/chat") == []


def test_item3_scores_nothing_when_no_rerun_completed() -> None:
    """Both re-runs refused: the id and count rows must not vouch for idempotency."""
    with World() as w:
        w.set_function_env(HEALTH, FI_COST_GUARDRAIL_ID="")
        _, rows, _ = run(w, 3)
        assert rows.get("3-no-new-findings") == "FAIL" and rows.get("3-finding-counts") == "FAIL"


def test_item9_counts_each_phases_alarm_action_after_its_own_invoke() -> None:
    """Recall's notification succeeds, warranty's fails: warranty must not count recall's."""
    with World() as w:
        def on_invoke(p: dict[str, Any]) -> None:
            if b'"warranty"' in (p.get("Payload") or b""):
                w.alarm.action_state = "Failed"
        w.observe("lambda", "Invoke", on_invoke)
        _, rows, _ = run(w, 9)
        assert rows.get("9-recall-alarm-action") == "PASS"
        assert rows.get("9-warranty-alarm-action") == "FAIL"


def test_the_manifest_records_the_dms_trees_commit(tmp_path: Any) -> None:
    import shutil
    import subprocess
    shutil.copytree(world_mod.DMS_ROOT / "source", tmp_path / "source")
    g = ["git", "-C", str(tmp_path), "-c", "user.email=t@example.invalid", "-c", "user.name=t"]
    subprocess.run(g + ["init", "-q"], check=True)
    subprocess.run(g + ["add", "-A"], check=True)
    subprocess.run(g + ["commit", "-q", "-m", "dms"], check=True)
    head = subprocess.run(g + ["rev-parse", "HEAD"], check=True, capture_output=True,
                          text=True).stdout.strip()
    with World() as w:
        w.run_main(["--stage", "staging", "--item", "1", "--dms-repo", str(tmp_path)])
        m = w.manifest()["items"]
        assert m["dms_commit"] == head and m["dms_dirty"] is False



def test_item2_does_not_take_a_record_that_started_in_the_invoke_second_before_it() -> None:
    """A run record already present at the invoke's second (a scheduled run, an earlier
    attempt) is excluded, so its result is never read as the invoked run's (rA2)."""
    with World() as w:
        done: list[bool] = []

        def on_get_cfg(p: dict[str, Any]) -> None:
            if not done:
                done.append(True)
                w.put("vsa-staging-avx-agent-runs", {
                    "agent_component": "agent-fi-recall-warranty#recall",
                    "started_at": w.clock.iso(), "finished_at": w.clock.iso(),
                    "status": "aborted", "artifacts_written": 0})
        w.observe("lambda", "GetFunctionConfiguration", on_get_cfg)
        _, rows, _ = run(w, 2)
        assert done and rows.get("2-recall-completed") == "PASS", rows



# ---------------------------------------------------------------------------
# Security review cycle 1 (security-review-g55-script-cycle1.md): SW1, SW2
# ---------------------------------------------------------------------------


def test_item9_stops_and_restores_when_a_schedule_is_enabled_during_the_alarm_wait() -> None:
    """SW1: the blank value stays applied through the alarm waits. A DISABLED schedule
    another session ENABLES meanwhile, due within 45 minutes, stops item 9 within one poll:
    no further invoke, and the env is restored."""
    with World() as w:
        original = w.function_env()
        sch = w.internal("scheduler")
        tmpl = sch.get_schedule(Name="vsa-staging-tier2-daily-cost-us-west-2")
        soon = w.clock.now() + dt.timedelta(minutes=40)
        kw = dict(Name="vsa-staging-tier2-new-us-west-2",
                  ScheduleExpression=f"cron({soon.minute} {soon.hour} * * ? *)",
                  ScheduleExpressionTimezone="Etc/UTC", FlexibleTimeWindow={"Mode": "OFF"},
                  Target=tmpl["Target"])
        sch.create_schedule(State="DISABLED", **kw)
        invoked: list[bool] = []
        flipped: list[bool] = []

        def on_invoke(p: dict[str, Any]) -> None:
            if b'"recall"' in (p.get("Payload") or b""):
                invoked.append(True)

        def on_describe(p: dict[str, Any]) -> None:
            if invoked and not flipped:
                flipped.append(True)
                sch.update_schedule(State="ENABLED", **kw)
        w.observe("lambda", "Invoke", on_invoke)
        w.observe("cloudwatch", "DescribeAlarms", on_describe)
        _, rows, _ = run(w, 9)
        assert flipped and rows.get("9-recall-sched-timing-during-wait") == "FAIL", rows
        assert [i["event"]["component"] for i in w.main_invocations()] == ["recall"]
        assert w.function_env() == original and rows.get("9-env-restored") == "PASS"


def test_item4_refuses_the_dealer_progress_invoke_inside_the_stand_in_window() -> None:
    """SW2: the dealer-progress stand-in runs on its own 05:40 cron; item 4 must not
    invoke it inside 04:50-06:10 even when the item started before the window."""
    with World() as w:
        mod = w.load_script()
        answers = iter([False, False, True])  # gate 3, before item 4, before the invoke
        mod._in_write_window = lambda: next(answers, True)
        _, text, _ = w.run_main(ALL_FLAGS + items(4), mod)
        rows = w.rows(text)
        assert rows.get("4-dp-window") == "FAIL"
        assert w.main_invocations(world_mod.DEALER_PROGRESS_FN) == []



def test_main_refuses_a_run_dir_that_already_exists(tmp_path: Any) -> None:
    """The run dir is never reused (security c1): a planted dir or symlink at the run's
    path would receive item 9's full env backup."""
    import os
    with World() as w:
        mod = w.load_script()
        rid = f"g55-planted-{os.getpid()}"
        mod._run_id = lambda: rid
        planted = f"/tmp/fi-rw-c-g55-{rid}"
        os.symlink(str(tmp_path), planted)
        try:
            with pytest.raises(FileExistsError):
                w.run_main(ALL_FLAGS + items(9), mod)
            assert list(tmp_path.iterdir()) == []
            assert [o for _s, o in w.main_writes()] == []
        finally:
            os.unlink(planted)
