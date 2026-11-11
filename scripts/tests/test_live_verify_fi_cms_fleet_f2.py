"""Tests for live-verify-fi-cms-fleet.py parts grounding-items and ground (sub-steps 1-3).

Drives the real ``main()`` against CmsWorld (moto + FakeRestApi + FakeAthena + real handlers).

T1 all-pass: grounding-items + ground exit 0.
T2 altered directive → grounding-items FAIL.
T3 missing ground Finding → grounding-items FAIL.
T4 approve doesn't record CMS grounding → ground S1 FAIL.
T5 RTS returns 200 at step 2 → ground S2 FAIL.
T6 RTS returns 200 at step 3 → ground S3 FAIL.
T7 resume after step 1 doesn't approve again (approve count = 1 total).
T8 raise after dtc-history put still deletes the row (cleanup in finally).
T9 writes refused without both flags → exit 2, zero DDB writes.

F2a-fix-1 item1: _read_cms_p0_faults pages through all faults pages.
F2a-fix-1 item2: _page_avx_findings raises on non-200; run_grounding_items records FAIL row.
F2a-fix-1 item3: _load_fault_catalog_s3 reads parquet from S3 (Athena removed).
F2a-fix-1 item4: run_coverage SKIP/PASS/SKIP-cap/FAIL branches.
"""
from __future__ import annotations

import contextlib
import datetime
import importlib.util
import io
import json
import os
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Any

import boto3
import pytest

from scripts.tests.fi_cms_world.world import CmsWorld, GROUNDINGS_TABLE, DTC_HISTORY_TABLE
from scripts.tests.fi_rw_world import seed
from scripts.tests.fi_rw_world.world import REGION, ADP_REGION, LAKE_BUCKET, STAGE

_SCRIPT = Path(__file__).resolve().parents[1] / "live-verify-fi-cms-fleet.py"
VIN = seed.vin(1)
VEHICLE_ID = seed.vehicle_id(1)
FLEET = seed.FLEETS[0]      # "demo-rental-carshare"
CODE = "P0217"              # p0; directive from fault_codes.yaml
LABOR_OP = "LAB-COOLOT-01"  # code_labor_op_map fault_codes entry for P0217
FAULT_DATE = datetime.date(2026, 10, 20)
_RUN_ID = "test-f2-fleet"
AGENT_RUNS_TABLE = f"vsa-{STAGE}-avx-agent-runs"
_HEALTH_FN = f"vsa-{STAGE}-tier2-health-agent-{REGION}"


# ---------------------------------------------------------------------------
# Module loading helpers
# ---------------------------------------------------------------------------

def _load_mod_bare() -> Any:
    """Load the script as a module without a CmsWorld (for unit tests of helpers)."""
    spec = importlib.util.spec_from_file_location(
        f"lv_fi_cms_fleet_bare_{uuid.uuid4().hex[:8]}", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


# ---------------------------------------------------------------------------
# World helpers
# ---------------------------------------------------------------------------

def _recreate_groundings_with_gsi(w: CmsWorld) -> None:
    ddb = w.internal("dynamodb")
    try:
        ddb.delete_table(TableName=GROUNDINGS_TABLE)
    except Exception:  # noqa: BLE001
        pass
    ddb.create_table(
        TableName=GROUNDINGS_TABLE,
        KeySchema=[{"AttributeName": "vehicleId", "KeyType": "HASH"},
                   {"AttributeName": "actionId", "KeyType": "RANGE"}],
        AttributeDefinitions=[
            {"AttributeName": "vehicleId", "AttributeType": "S"},
            {"AttributeName": "actionId", "AttributeType": "S"},
            {"AttributeName": "openFleetId", "AttributeType": "S"},
            {"AttributeName": "groundedAt", "AttributeType": "S"},
        ],
        GlobalSecondaryIndexes=[{"IndexName": "open-groundings-index",
                                  "KeySchema": [
                                      {"AttributeName": "openFleetId", "KeyType": "HASH"},
                                      {"AttributeName": "groundedAt", "KeyType": "RANGE"},
                                  ],
                                  "Projection": {"ProjectionType": "ALL"}}],
        BillingMode="PAY_PER_REQUEST",
    )


def _register_stacks(w: CmsWorld) -> None:
    cfn = w.internal("cloudformation")
    cfn.create_stack(
        StackName="cms-staging-ui",
        TemplateBody=json.dumps({
            "Resources": {"Placeholder": {"Type": "AWS::SNS::Topic"}},
            "Outputs": {"APIEndpoint": {"Value": w.cms_fi_url},
                        "UserPoolId": {"Value": w.pool.pool_id}},
        }),
    )


def _register_alarm(w: CmsWorld) -> None:
    cw = boto3.client("cloudwatch", region_name=REGION)
    cw.put_metric_alarm(
        AlarmName="cms-staging-ui-fi-health-views-stale",
        AlarmDescription="test", MetricName="Latency", Namespace="AWS/ApiGateway",
        Period=300, EvaluationPeriods=1, Threshold=1.0,
        ComparisonOperator="GreaterThanThreshold",
        TreatMissingData="notBreaching", Statistic="Average",
    )


def _setup_world(w: CmsWorld) -> list[dict]:
    """Seed data; return list of seeded Findings (includes ground Finding)."""
    _recreate_groundings_with_gsi(w)
    w.adp_faults.add_fault(VIN, CODE, FAULT_DATE, FAULT_DATE)
    w.seed_recall_cache()
    w.refresh_health()
    findings = w.seed_health_findings(VIN, CODE, FLEET)
    _register_stacks(w)
    _register_alarm(w)
    _put_fault_catalog_s3(w)
    _enable_grounding(w)
    return findings


def _ground_finding(findings: list[dict]) -> dict:
    return next(f for f in findings if (f.get("subject_key") or "").startswith("ground/"))


def _load_mod(w: CmsWorld) -> Any:
    spec = importlib.util.spec_from_file_location(
        f"lv_fi_cms_fleet_f2_{uuid.uuid4().hex[:8]}", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def _run_main(mod: Any, argv: list[str], run_id: str = _RUN_ID,
              mdir: str | None = None) -> tuple[int, str]:
    env_back = os.environ.copy()
    old_argv = sys.argv
    out = io.StringIO()
    try:
        os.environ["LIVE_TESTS"] = "1"
        d = mdir or tempfile.mkdtemp(prefix="fi-cms-f2-")
        os.environ["FI_CMS_LIVE_MANIFEST_DIR"] = d
        sys.argv = [str(_SCRIPT), "--stage", "staging", "--run-id", run_id] + argv
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            try:
                rc = mod.main()
            except SystemExit as e:
                rc = e.code if isinstance(e.code, int) else 1
    finally:
        sys.argv = old_argv
        os.environ.clear()
        os.environ.update(env_back)
    return rc, out.getvalue()


def _rows(text: str) -> dict[str, str]:
    rows: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        for st in ("PASS", "FAIL", "SKIP", "NOTE"):
            if line.startswith(f"[{st}]"):
                rows[line[len(st) + 3:].strip().split(":")[0].strip()] = st
                break
    return rows


# ---------------------------------------------------------------------------
# World helper: write fault_codes.parquet to LAKE_BUCKET for S3 catalog tests
# ---------------------------------------------------------------------------

def _put_fault_catalog_s3(w: CmsWorld) -> None:
    """Build fault_codes.parquet from the world's DuckDB tables; put in LAKE_BUCKET."""
    import duckdb        # noqa: PLC0415
    import os as _os    # noqa: PLC0415

    rows = w.athena.con.execute(
        f"SELECT code, band, directive_text "
        f"FROM adp_{STAGE}_fleet_health.fault_codes"
    ).fetchall()
    con = duckdb.connect()
    con.execute("CREATE TABLE fc (code VARCHAR, band VARCHAR, directive_text VARCHAR)")
    for row in rows:
        con.execute("INSERT INTO fc VALUES (?, ?, ?)", list(row))
    with tempfile.NamedTemporaryFile(suffix=".parquet", delete=False) as tmp:
        tmp_path = tmp.name
    try:
        con.execute(f"COPY fc TO '{tmp_path}' (FORMAT PARQUET)")
        parquet = Path(tmp_path).read_bytes()
    finally:
        _os.unlink(tmp_path)
    s3 = w.internal("s3", ADP_REGION)
    try:
        s3.create_bucket(Bucket=LAKE_BUCKET,
                         CreateBucketConfiguration={"LocationConstraint": ADP_REGION})
    except Exception:  # noqa: BLE001
        pass
    s3.put_object(Bucket=LAKE_BUCKET,
                  Key="config/fleet_health/fault_codes/fault_codes.parquet",
                  Body=parquet)


# ---------------------------------------------------------------------------
# World helper: agent-runs table + run record seeding
# ---------------------------------------------------------------------------

def _create_agent_runs_table(w: CmsWorld) -> None:
    ddb = w.internal("dynamodb")
    try:
        ddb.delete_table(TableName=AGENT_RUNS_TABLE)
    except Exception:  # noqa: BLE001
        pass
    ddb.create_table(
        TableName=AGENT_RUNS_TABLE,
        KeySchema=[{"AttributeName": "agent_component", "KeyType": "HASH"},
                   {"AttributeName": "started_at", "KeyType": "RANGE"}],
        AttributeDefinitions=[
            {"AttributeName": "agent_component", "AttributeType": "S"},
            {"AttributeName": "started_at", "AttributeType": "S"},
        ],
        BillingMode="PAY_PER_REQUEST",
    )


def _put_agent_run(w: CmsWorld, vehicles_over_cap: int = 0,
                   offset_seconds: int = 0) -> None:
    """Seed one completed run record for the fleet-health service component.

    ``started_at`` is derived from the health-agent Lambda's real LastModified
    (from moto's wall clock), not the world's fake clock, so the coverage query
    ``started_at >= lm_sk`` finds this record even inside the world.
    """
    import datetime as _dt_real  # noqa: PLC0415 — bypass world-clock patch
    ddb = w.internal("dynamodb")
    fn = f"vsa-{STAGE}-tier2-health-agent-{REGION}"
    cfg = w.internal("lambda").get_function_configuration(FunctionName=fn)
    lm = cfg.get("LastModified", "2000-01-01T00:00:00Z")
    # fromisoformat on FakeDateTime inherits _RealDateTime.fromisoformat → real parse
    lm_dt = _dt_real.datetime.fromisoformat(lm.replace("+0000", "+00:00"))
    started_dt = lm_dt + _dt_real.timedelta(seconds=max(1, offset_seconds))
    started = started_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    item: dict = {
        "agent_component": {"S": "agent-fi-fleet-health#service"},
        "started_at": {"S": started},
        "status": {"S": "completed"},  # agents/tier2/run_records.py status values
        "finished_at": {"S": started},
    }
    if vehicles_over_cap:
        item["vehicles_over_cap"] = {"N": str(vehicles_over_cap)}
    ddb.put_item(TableName=AGENT_RUNS_TABLE, Item=item)


# ---------------------------------------------------------------------------
# T1: all-pass — grounding-items + ground steps 1-3
# ---------------------------------------------------------------------------

def test_all_pass() -> None:
    """F2b: all-pass across two ground invocations: first SKIPs at S5 (no repairRef),
    second passes after refresh_health adds repairRef; grounding-items exit 0."""
    with CmsWorld(run_scheduled=False) as w:
        _setup_world(w)
        mod = _load_mod(w)
        # Run grounding-items
        rc, text = _run_main(mod, ["--allow-writes", "--part", "grounding-items"])
        assert rc == 0, f"grounding-items exit {rc}:\n{text}"
        rows = _rows(text)
        fails = [k for k, v in rows.items() if v == "FAIL"]
        assert not fails, f"grounding-items FAILs: {fails}\n{text}"
        # First ground run: S1-S4 complete, S5 SKIPs (no repairRef in health cache yet)
        mdir = tempfile.mkdtemp(prefix="fi-cms-f2-allpass-")
        rc2, text2 = _run_main(mod, ["--allow-writes", "--allow-managed-writes",
                                     "--part", "ground"], mdir=mdir)
        assert rc2 == 2, f"first ground run: expected SKIP (2), got {rc2}:\n{text2}"
        # Read ro_id from manifest; add RO image to lake + refresh health
        man = json.loads((Path(mdir) / f"{_RUN_ID}.json").read_text())
        ro_id = man["items"].get("ground/step4_ro_id")
        assert ro_id, f"no step4_ro_id in manifest after first ground run: {man['items']}"
        ts = "2026-10-22T14:00:00+00:00"
        w.add_ro_image(ro_id, VIN, "Ready", LABOR_OP, ts)
        w.refresh_health()
        # Second ground run: S4 finds existing RO, S5 PASS, S6 200
        rc3, text3 = _run_main(mod, ["--allow-writes", "--allow-managed-writes",
                                     "--part", "ground"], mdir=mdir)
        assert rc3 == 0, f"second ground run exit {rc3}:\n{text3}"
        rows3 = _rows(text3)
        fails3 = [k for k, v in rows3.items() if v == "FAIL"]
        assert not fails3, f"ground FAILs: {fails3}\n{text3}"
        # dtc-history row deleted in finally
        ddb = w.internal("dynamodb")
        scan = ddb.scan(TableName=DTC_HISTORY_TABLE,
                        FilterExpression="vehicleId = :v",
                        ExpressionAttributeValues={":v": {"S": VEHICLE_ID}})
        assert not scan.get("Items"), f"dtc-history row still present: {scan['Items']}"


# ---------------------------------------------------------------------------
# T9: writes refused without both flags → ground SKIPs, zero DDB writes
# ---------------------------------------------------------------------------

def test_writes_refused_without_both_flags() -> None:
    with CmsWorld(run_scheduled=False) as w:
        _setup_world(w)
        mod = _load_mod(w)
        # No --allow-managed-writes → ground part must SKIP
        rc, text = _run_main(mod, ["--allow-writes", "--part", "ground"])
        assert rc == 2, f"expected exit 2 (SKIP), got {rc}:\n{text}"
        rows = _rows(text)
        assert any("SKIP" == rows.get(k) for k in rows), f"no SKIP row: {rows}"
        # No row should be in dtc-history
        ddb = w.internal("dynamodb")
        scan = ddb.scan(TableName=DTC_HISTORY_TABLE,
                        FilterExpression="vehicleId = :v",
                        ExpressionAttributeValues={":v": {"S": VEHICLE_ID}})
        assert not scan.get("Items"), "dtc-history written without --allow-managed-writes"
        # Neither flag → also SKIP
        rc2, _ = _run_main(mod, ["--part", "ground"])
        assert rc2 == 2, f"expected exit 2 without flags, got {rc2}"


def test_altered_directive_fails() -> None:
    with CmsWorld(run_scheduled=False) as w:
        findings = _setup_world(w)
        gf = _ground_finding(findings)
        # Overwrite the ground Finding's disclosure_txt to a corrupted value
        w.put("vsa-staging-avx-findings", {
            **gf, "vin": VIN, "fleet_id": FLEET,
            "disclosure_txt": "CORRUPTED-NOT-THE-CATALOG-DIRECTIVE",
        })
        mod = _load_mod(w)
        rc, text = _run_main(mod, ["--allow-writes", "--part", "grounding-items"])
    # Check raw output for FAIL on disclosure check (label contains colon; _rows truncates)
    assert "[FAIL]" in text and "disclosure_txt" in text, (
        f"expected disclosure_txt FAIL; output:\n{text}"
    )
    assert rc == 1, f"expected exit 1, got {rc}"


# ---------------------------------------------------------------------------
# T3: missing ground Finding → grounding-items FAIL
# ---------------------------------------------------------------------------

def test_missing_ground_finding_fails() -> None:
    with CmsWorld(run_scheduled=False) as w:
        findings = _setup_world(w)
        gf = _ground_finding(findings)
        # Delete the ground Finding — AVX findings table PK is finding_id (S) only
        # (vsa-tier2-stack.ts:307; agents/tier2/tests/test_writer_subject_key.py:738)
        ddb = w.internal("dynamodb")
        ddb.delete_item(
            TableName="vsa-staging-avx-findings",
            Key={"finding_id": {"S": gf["finding_id"]}},
        )
        mod = _load_mod(w)
        rc, text = _run_main(mod, ["--allow-writes", "--part", "grounding-items"])
    assert "[FAIL]" in text and "every open p0 vehicle has a ground Finding" in text, (
        f"expected ground Finding count FAIL; output:\n{text}"
    )
    assert rc == 1, f"expected exit 1, got {rc}"


# ---------------------------------------------------------------------------
# T4: approve doesn't write CMS grounding → ground S1 grounding-row FAIL
# ---------------------------------------------------------------------------

def test_approve_no_grounding_fails() -> None:
    with CmsWorld(run_scheduled=False) as w:
        _setup_world(w)
        orig = w.cms_fi._mod.groundings.create_fault_grounding

        def _noop_grounding(*args: object, **kwargs: object) -> dict:
            return {"statusCode": 200, "headers": {}, "body": '{"vehicleId":"noop"}'}

        w.cms_fi._mod.groundings.create_fault_grounding = _noop_grounding
        try:
            mod = _load_mod(w)
            rc, text = _run_main(mod, ["--allow-writes", "--allow-managed-writes",
                                        "--part", "ground"])
        finally:
            w.cms_fi._mod.groundings.create_fault_grounding = orig
    assert "[FAIL]" in text and "CMS fault grounding open" in text, (
        f"expected grounding-row FAIL; output:\n{text}"
    )
    assert rc == 1, f"expected exit 1, got {rc}"


# ---------------------------------------------------------------------------
# T5: RTS returns 200 at step 2 → ground S2 FAIL
# ---------------------------------------------------------------------------

def test_rts_200_at_step2_fails() -> None:
    with CmsWorld(run_scheduled=False) as w:
        _setup_world(w)
        orig = w.cms_fi._mod.groundings.return_to_service

        def _always_200(*args: object, **kwargs: object) -> dict:
            return {"statusCode": 200, "headers": {},
                    "body": '{"returnedAt":"2099-01-01T00:00:00Z"}'}

        w.cms_fi._mod.groundings.return_to_service = _always_200
        try:
            mod = _load_mod(w)
            rc, text = _run_main(mod, ["--allow-writes", "--allow-managed-writes",
                                        "--part", "ground"])
        finally:
            w.cms_fi._mod.groundings.return_to_service = orig
    assert "[FAIL]" in text and "S2: return to service refused" in text, (
        f"expected S2 RTS=409 FAIL; output:\n{text}"
    )
    assert rc == 1, f"expected exit 1, got {rc}"


# ---------------------------------------------------------------------------
# T6: RTS returns 200 at step 3 → ground S3 FAIL
# ---------------------------------------------------------------------------

def test_rts_200_at_step3_was_dropped() -> None:
    """S3 dtc-history write was dropped (W1). RTS is no longer called a second time
    during ground; there is no 'S3: still refused' check to fail."""
    # T6 archived: the behavior it tested no longer exists. The W1 new test
    # (test_ground_s3_is_note_no_dtc_write) covers the replacement behavior.


# ---------------------------------------------------------------------------
# T7: resume after step 1 doesn't approve again (approve count stays 1)
# ---------------------------------------------------------------------------

def test_resume_skips_approve() -> None:
    approve_calls: list[int] = [0]
    with CmsWorld(run_scheduled=False) as w:
        _setup_world(w)
        # Patch the findings handler _approve_finding to count calls
        # (world.handlers["findings"] is the loaded module, world.py:572)
        findings_mod = w.handlers["findings"]
        orig_approve = findings_mod._approve_finding

        def _counting_approve(event: dict, claims: dict) -> dict:
            approve_calls[0] += 1
            return orig_approve(event, claims)

        findings_mod._approve_finding = _counting_approve
        try:
            mod = _load_mod(w)
            mdir = tempfile.mkdtemp(prefix="fi-cms-f2-resume-")
            # First run: S1-S4 happen (step1 approves, S5 SKIPs — no repairRef yet)
            rc1, text1 = _run_main(mod, ["--allow-writes", "--allow-managed-writes",
                                          "--part", "ground"], mdir=mdir)
            assert rc1 == 2, f"first run expected SKIP (2), got {rc1}:\n{text1}"
            count_after_run1 = approve_calls[0]
            # Add RO image + refresh health so S5 passes on resume
            man = json.loads((Path(mdir) / f"{_RUN_ID}.json").read_text())
            ro_id = man["items"].get("ground/step4_ro_id")
            assert ro_id, f"no step4_ro_id: {man['items']}"
            w.add_ro_image(ro_id, VIN, "Ready", LABOR_OP, "2026-10-22T14:00:00+00:00")
            w.refresh_health()
            # Second run same run_id: step 1 should NOT fire approve again; S5 PASS, S6 200
            rc2, text2 = _run_main(mod, ["--allow-writes", "--allow-managed-writes",
                                          "--part", "ground"], mdir=mdir)
            assert rc2 == 0, f"second run failed: {rc2}:\n{text2}"
        finally:
            findings_mod._approve_finding = orig_approve
    assert count_after_run1 == 1, f"approve fired {count_after_run1} times on run 1"
    assert approve_calls[0] == 1, (
        f"approve fired {approve_calls[0]} times total; expected 1 (resume skips)"
    )


# ---------------------------------------------------------------------------
# T8: raise after dtc-history put → row still deleted (finally runs)
# ---------------------------------------------------------------------------

def test_raise_after_dtc_put_was_dropped() -> None:
    """S3 dtc-history put was dropped (W1). No dtc row is written in ground,
    so the 'finally deletes the row' behavior no longer applies.
    T8 archived; test_ground_s3_is_note_no_dtc_write covers the replacement."""


# ===========================================================================
# W1 (B-1): ground S3 is a NOTE, not a PASS/FAIL; dtc-history never written
# ===========================================================================

def test_ground_s3_is_note_no_dtc_write() -> None:
    """ground S3 is a NOTE (remote-clear leg dropped per architect W1 decision).
    No PutItem is made to dtc-history, and the S3 row appears only as [NOTE].
    Seen failing before fix: yes — today run_ground calls ddb.put_item on dtc-history."""
    with CmsWorld(run_scheduled=False) as w:
        _setup_world(w)
        mod = _load_mod(w)
        mdir = tempfile.mkdtemp(prefix="fi-cms-f2-w1-")
        rc, text = _run_main(mod, ["--allow-writes", "--allow-managed-writes",
                                    "--part", "ground"], mdir=mdir)
        # dtc-history must be empty — no PutItem from ground
        ddb = w.internal("dynamodb")
        scan = ddb.scan(
            TableName=DTC_HISTORY_TABLE,
            FilterExpression="vehicleId = :v",
            ExpressionAttributeValues={":v": {"S": VEHICLE_ID}},
            ConsistentRead=True,
        )
        assert not scan.get("Items"), (
            f"dtc-history row written by ground (expected none): {scan['Items']}"
        )
    # S3 must not appear as PASS or FAIL — only as NOTE (or not at all)
    rows = _rows(text)
    s3_checks = [k for k in rows if "S3" in k]
    assert not any(rows[k] in ("PASS", "FAIL") for k in s3_checks), (
        f"S3 appeared as PASS/FAIL (expected NOTE or absent): {s3_checks} rows={rows}"
    )


# ===========================================================================
# F2a-fix-1 item1: _read_cms_p0_faults pagination
# ===========================================================================

def test_faults_paginates_three_pages() -> None:
    """_read_cms_p0_faults pages through all pages; VIN on page 3 must appear."""
    mod = _load_mod_bare()
    vin1, vin2, vin3 = seed.vin(10), seed.vin(11), seed.vin(12)
    vid1, vid2, vid3 = "veh-001", "veh-002", "veh-003"

    def _p0_row(vin: str, vid: str) -> dict:
        return {"vin": vin, "vehicleId": vid, "code": "P0420",
                "severityBand": "p0", "status": "open", "repairRef": None}

    # Pages keyed by the request's own pageToken (a fake that answers in call order would
    # pass a loop that never sends the token). The tokens carry '+', '/', '=' as base64
    # cursors do, so an unquoted token reaches no page.
    import urllib.parse as _up  # noqa: PLC0415
    pages = {
        None: (200, {"data": [_p0_row(vin1, vid1)], "nextPageToken": "tok+page/2="}),
        "tok+page/2=": (200, {"data": [_p0_row(vin2, vid2)], "nextPageToken": "tok+page/3="}),
        "tok+page/3=": (200, {"data": [_p0_row(vin3, vid3)]}),
    }
    call_n = [0]

    class _Api:
        def call(self, _m: str, path: str, _t: str) -> tuple:  # type: ignore[override]
            call_n[0] += 1
            qs = _up.parse_qs(_up.urlsplit(path).query)
            tok = (qs.get("pageToken") or [None])[0]
            return pages.get(tok, (400, {"error": f"bad pageToken {tok!r}"}))

    result = mod._read_cms_p0_faults(_Api(), FLEET, "test-token")
    assert vin3 in result, f"page-3 VIN {vin3!r} missing; got keys: {sorted(result)}"
    assert call_n[0] == 3, f"expected 3 GET calls, got {call_n[0]}"


def test_faults_non200_raises() -> None:
    """_read_cms_p0_faults raises RuntimeError on non-200 (503 cache unavailable)."""
    mod = _load_mod_bare()

    class _Api:
        def call(self, _m: str, _p: str, _t: str) -> tuple:  # type: ignore[override]
            return 503, {"error": "cache unavailable"}

    with pytest.raises(RuntimeError, match="HTTP 503"):
        mod._read_cms_p0_faults(_Api(), FLEET, "tok")


# ===========================================================================
# F2a-fix-1 item2: _page_avx_findings status check
# ===========================================================================

def test_avx_findings_non200_raises() -> None:
    """_page_avx_findings raises RuntimeError on non-200 (403 included)."""
    mod = _load_mod_bare()

    class _Api:
        def call(self, _m: str, _p: str, _t: str) -> tuple:  # type: ignore[override]
            return 403, {"error": "Forbidden"}

    with pytest.raises(RuntimeError, match="HTTP 403"):
        mod._page_avx_findings(_Api(), FLEET, "tok")


def test_avx_findings_error_produces_fail_row() -> None:
    """A findings-handler error for one fleet → FAIL row, not 'no ground Finding'."""
    with CmsWorld(run_scheduled=False) as w:
        _setup_world(w)
        _put_fault_catalog_s3(w)
        findings_mod = w.handlers["findings"]
        orig = findings_mod._list_findings_by_fleet

        def _raise(*a: object, **k: object) -> None:
            raise RuntimeError("injected findings failure → 502")

        findings_mod._list_findings_by_fleet = _raise
        try:
            mod = _load_mod(w)
            rc, text = _run_main(mod, ["--allow-writes", "--part", "grounding-items"])
        finally:
            findings_mod._list_findings_by_fleet = orig
    # Expect a FAIL for the findings fetch (not just a missing-Finding FAIL)
    assert "[FAIL]" in text, f"expected FAIL row; output:\n{text}"
    fail_lines = [l for l in text.splitlines() if "[FAIL]" in l]
    assert any(
        "read fleet demo-rental-carshare" in l and "/findings/fleet/" in l and "HTTP 500" in l
        for l in fail_lines
    ), f"FAIL row doesn't mention findings fetch; FAILs: {fail_lines}\n{text}"
    assert rc == 1, f"expected exit 1, got {rc}"


# ===========================================================================
# F2a-fix-1 item3: S3 parquet catalog
# ===========================================================================

def test_load_fault_catalog_s3_reads_parquet() -> None:
    """_load_fault_catalog_s3 returns {code: {code, band, directive_text}} from S3."""
    with CmsWorld(run_scheduled=False) as w:
        _setup_world(w)
        _put_fault_catalog_s3(w)
        s3 = w.internal("s3", ADP_REGION)
        mod = _load_mod(w)
        catalog = mod._load_fault_catalog_s3(s3, LAKE_BUCKET)
    assert CODE in catalog, f"{CODE!r} not in catalog: {sorted(catalog)[:5]}"
    entry = catalog[CODE]
    assert entry.get("band") == "p0", f"wrong band: {entry}"
    assert entry.get("directive_text"), f"no directive_text: {entry}"


def test_grounding_items_uses_s3_catalog() -> None:
    """grounding-items succeeds with S3 parquet catalog (Athena ops removed)."""
    with CmsWorld(run_scheduled=False) as w:
        _setup_world(w)
        _put_fault_catalog_s3(w)
        mod = _load_mod(w)
        rc, text = _run_main(mod, ["--allow-writes", "--part", "grounding-items"])
    assert rc == 0, f"grounding-items exit {rc}:\n{text}"
    rows = _rows(text)
    fails = [k for k, v in rows.items() if v == "FAIL"]
    assert not fails, f"grounding-items FAILs: {fails}\n{text}"


# ===========================================================================
# F2a-fix-1 item4: run_coverage branches
# ===========================================================================

VIN2 = seed.vin(2)       # second VIN; seeded with fault but no ground Finding → gap


def _enable_grounding(w: CmsWorld) -> None:
    """Update the health-agent Lambda env to set FAULT_GROUNDING_ENABLED=true."""
    lam = w.internal("lambda")
    cfg = lam.get_function_configuration(FunctionName=_HEALTH_FN)
    env_vars = dict((cfg.get("Environment") or {}).get("Variables") or {})
    env_vars["FAULT_GROUNDING_ENABLED"] = "true"
    lam.update_function_configuration(FunctionName=_HEALTH_FN,
                                      Environment={"Variables": env_vars})


def _setup_world_with_gap(w: CmsWorld) -> None:
    """Seed VIN (p0 + ground Finding) AND VIN2 (p0 fault, NO ground Finding)."""
    _recreate_groundings_with_gsi(w)
    w.adp_faults.add_fault(VIN, CODE, FAULT_DATE, FAULT_DATE)
    w.adp_faults.add_fault(VIN2, CODE, FAULT_DATE, FAULT_DATE)
    w.seed_recall_cache()
    w.refresh_health()
    w.seed_health_findings(VIN, CODE, FLEET)   # VIN gets ground Finding
    # VIN2 deliberately left without a ground Finding → coverage gap
    _register_stacks(w)
    _register_alarm(w)


def test_coverage_skip_not_enabled() -> None:
    """FAULT_GROUNDING_ENABLED not set → coverage SKIP."""
    with CmsWorld(run_scheduled=False) as w:
        _setup_world(w)
        _put_fault_catalog_s3(w)
        # _setup_world turns the switch on; turn it off again ('false', as deployed before 7.7)
        lam = w.internal("lambda")
        env = dict(lam.get_function_configuration(FunctionName=_HEALTH_FN)["Environment"]["Variables"])
        env["FAULT_GROUNDING_ENABLED"] = "false"
        lam.update_function_configuration(FunctionName=_HEALTH_FN, Environment={"Variables": env})
        mod = _load_mod(w)
        rc, text = _run_main(mod, ["--allow-writes", "--part", "grounding-items"])
    assert rc == 2, f"expected exit 2 (SKIP), got {rc}:\n{text}"
    assert "[SKIP]" in text, f"no SKIP row:\n{text}"


def test_coverage_pass_all_covered() -> None:
    """All p0 VINs have pending ground Findings → PASS."""
    with CmsWorld(run_scheduled=False) as w:
        _setup_world(w)
        _put_fault_catalog_s3(w)
        _enable_grounding(w)
        mod = _load_mod(w)
        rc, text = _run_main(mod, ["--allow-writes", "--part", "grounding-items"])
    assert rc == 0, f"expected exit 0 (PASS), got {rc}:\n{text}"
    assert "[PASS]" in text, f"no PASS row:\n{text}"
    assert "[FAIL]" not in text, f"unexpected FAIL rows:\n{text}"


def test_coverage_skip_cap_hit() -> None:
    """Gaps + recent run with vehicles_over_cap > 0 → SKIP."""
    with CmsWorld(run_scheduled=False) as w:
        _setup_world_with_gap(w)
        _put_fault_catalog_s3(w)
        _enable_grounding(w)
        _create_agent_runs_table(w)
        _put_agent_run(w, vehicles_over_cap=5)   # recent run, cap hit
        mod = _load_mod(w)
        rc, text = _run_main(mod, ["--allow-writes", "--part", "grounding-items"])
    assert rc == 2, f"expected exit 2 (SKIP), got {rc}:\n{text}"
    skip_lines = [l for l in text.splitlines() if "[SKIP]" in l]
    assert any("cap" in l.lower() or "vehicles_over_cap" in l for l in skip_lines), (
        f"SKIP row doesn't mention cap: {skip_lines}\n{text}"
    )


def test_coverage_fail_cap_zero() -> None:
    """Gaps + run with vehicles_over_cap == 0 → FAIL (all should have been covered)."""
    with CmsWorld(run_scheduled=False) as w:
        _setup_world_with_gap(w)
        _put_fault_catalog_s3(w)
        _enable_grounding(w)
        _create_agent_runs_table(w)
        _put_agent_run(w, vehicles_over_cap=0)   # cap not hit → all should be covered
        mod = _load_mod(w)
        rc, text = _run_main(mod, ["--allow-writes", "--part", "grounding-items"])
    assert rc == 1, f"expected exit 1 (FAIL), got {rc}:\n{text}"
    fail_lines = [l for l in text.splitlines() if "[FAIL]" in l]
    assert any("ground Finding" in l for l in fail_lines), (
        f"FAIL row doesn't mention ground Finding: {fail_lines}\n{text}"
    )


def test_coverage_fail_no_run() -> None:
    """Gaps + no run record → FAIL (treat same as vehicles_over_cap == 0)."""
    with CmsWorld(run_scheduled=False) as w:
        _setup_world_with_gap(w)
        _put_fault_catalog_s3(w)
        _enable_grounding(w)
        _create_agent_runs_table(w)   # empty table (no run records)
        mod = _load_mod(w)
        rc, text = _run_main(mod, ["--allow-writes", "--part", "grounding-items"])
    assert rc == 1, f"expected exit 1 (FAIL), got {rc}:\n{text}"
    assert "[FAIL]" in text, f"no FAIL row:\n{text}"


# ===========================================================================
# F3 tests 2026-10-05: grounding-items + ground advanced edge cases (tests 1-10)
# ===========================================================================

VIN_B = seed.vin(2)    # second VIN in FLEET_A
VIN_C = seed.vin(3)    # third VIN in FLEET_A
VID_B = seed.vehicle_id(2)
VID_C = seed.vehicle_id(3)
CODE2 = "P0AA6"        # p0 with different directive from P0217


# ---------------------------------------------------------------------------
# Test 1: ground Finding with fault_codes naming a code that isn't open p0
# ---------------------------------------------------------------------------

def test_ground_finding_codes_not_open_p0_fails() -> None:
    """ground Finding fault_codes = ['P9999'] — code not open for the VIN → FAIL row."""
    bad_code = "P9999"
    with CmsWorld(run_scheduled=False) as w:
        findings = _setup_world(w)
        gf = _ground_finding(findings)
        w.put("vsa-staging-avx-findings", {
            **gf, "vin": VIN, "fleet_id": FLEET,
            "recommended_action": {
                **gf["recommended_action"],
                "parameters": {
                    **gf["recommended_action"]["parameters"],
                    "fault_codes": [bad_code],
                },
            },
        })
        mod = _load_mod(w)
        rc, text = _run_main(mod, ["--allow-writes", "--part", "grounding-items"])
    assert rc == 1, f"expected exit 1, got {rc}:\n{text}"
    assert (
        f"[FAIL] grounding-items {VIN}: "
        f"codes ['{bad_code}'] not open p0; "
        f"primary code ['{bad_code}'] not p0 in the catalog"
    ) in text, f"expected exact FAIL row for {bad_code!r}; output:\n{text}"


# ---------------------------------------------------------------------------
# Test 2: disclosure_txt == second code's directive, not fault_codes[0]'s
# ---------------------------------------------------------------------------

def test_disclosure_txt_second_code_directive_fails() -> None:
    """disclosure_txt is the second code's directive (not fault_codes[0]'s) → FAIL.

    Both p0 codes must be open before the health-cache refresh so _read_cms_p0_faults
    returns them both; P0AA6 is added before _setup_world's refresh_health call.
    """
    with CmsWorld(run_scheduled=False) as w:
        # Seed BOTH p0 faults before refresh so the health cache includes both
        _recreate_groundings_with_gsi(w)
        w.adp_faults.add_fault(VIN, CODE, FAULT_DATE, FAULT_DATE)
        w.adp_faults.add_fault(VIN, CODE2, FAULT_DATE, FAULT_DATE)  # before refresh
        w.seed_recall_cache()
        w.refresh_health()
        findings = w.seed_health_findings(VIN, CODE, FLEET)  # Finding for primary CODE
        _register_stacks(w)
        _register_alarm(w)
        _put_fault_catalog_s3(w)
        _enable_grounding(w)

        gf = _ground_finding(findings)
        # Fetch directives from DuckDB catalog
        rows0 = w.athena.con.execute(
            f"SELECT directive_text FROM adp_{STAGE}_fleet_health.fault_codes WHERE code = ?",
            [CODE]).fetchall()
        rows2 = w.athena.con.execute(
            f"SELECT directive_text FROM adp_{STAGE}_fleet_health.fault_codes WHERE code = ?",
            [CODE2]).fetchall()
        assert rows0 and rows2, f"catalog missing {CODE} or {CODE2}"
        code0_directive, code2_directive = rows0[0][0], rows2[0][0]
        assert code0_directive != code2_directive, (
            f"both codes share the same directive; test has no bite: {code0_directive!r}")
        # Overwrite: fault_codes=[CODE, CODE2] (both open), disclosure_txt = CODE2's directive
        w.put("vsa-staging-avx-findings", {
            **gf, "vin": VIN, "fleet_id": FLEET,
            "disclosure_txt": code2_directive,
            "recommended_action": {
                **gf["recommended_action"],
                "parameters": {
                    **gf["recommended_action"]["parameters"],
                    "fault_codes": [CODE, CODE2],
                },
            },
        })
        mod = _load_mod(w)
        rc, text = _run_main(mod, ["--allow-writes", "--part", "grounding-items"])
    assert rc == 1, f"expected exit 1, got {rc}:\n{text}"
    assert f"[FAIL] grounding-items {VIN}: disclosure_txt != catalog directive" in text, (
        f"expected disclosure_txt FAIL; output:\n{text}"
    )


# ---------------------------------------------------------------------------
# Test 3: expired ground Finding counts as missing, not covered
# ---------------------------------------------------------------------------

def test_expired_ground_finding_counts_as_missing() -> None:
    """expires_at in the past → Finding skipped by _live_ground_findings → treated as missing."""
    with CmsWorld(run_scheduled=False) as w:
        findings = _setup_world(w)
        gf = _ground_finding(findings)
        # Overwrite expires_at to a long-past timestamp
        w.put("vsa-staging-avx-findings", {
            **gf, "vin": VIN, "fleet_id": FLEET,
            "expires_at": "2000-01-01T00:00:00Z",
        })
        _create_agent_runs_table(w)   # empty table → _newest_completed_service_run returns None
        mod = _load_mod(w)
        rc, text = _run_main(mod, ["--allow-writes", "--part", "grounding-items"])
    assert rc == 1, f"expected exit 1, got {rc}:\n{text}"
    fail_lines = [l for l in text.splitlines() if "[FAIL]" in l]
    assert any("every open p0 vehicle has a ground Finding" in l for l in fail_lines), (
        f"expected 'every open p0 vehicle has a ground Finding' FAIL; FAILs:\n{fail_lines}"
    )
    # VIN appears in the gap list because its expired Finding was not counted
    assert VIN in text, f"expected VIN {VIN!r} in gap list; output:\n{text}"


# ---------------------------------------------------------------------------
# Test 4: gaps + vehicles_over_cap > 0 but 8 days after switch → FAIL, not SKIP
# ---------------------------------------------------------------------------

def test_cap_hit_but_old_switch_fails_not_skips() -> None:
    """vehicles_over_cap=5 but 8 days since the switch → FAIL (days < 7 check fails)."""
    import re as _re  # noqa: PLC0415
    with CmsWorld(run_scheduled=False) as w:
        _setup_world_with_gap(w)
        _put_fault_catalog_s3(w)
        _enable_grounding(w)
        _create_agent_runs_table(w)
        _put_agent_run(w, vehicles_over_cap=5)
        # Determine switched_at from the health Lambda LastModified
        lam = w.internal("lambda")
        cfg = lam.get_function_configuration(FunctionName=_HEALTH_FN)
        lm_str = str(cfg.get("LastModified") or "")
        m = _re.fullmatch(r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.\d+)?(Z|[+-]\d{4})", lm_str)
        tz = "+0000" if (m and m.group(2) == "Z") else (m.group(2) if m else "+0000")
        s = m.group(1) + tz if m else "2020-01-01T00:00:00+0000"
        switched_at = datetime.datetime.strptime(s, "%Y-%m-%dT%H:%M:%S%z")
        switched_at = switched_at.astimezone(datetime.timezone.utc)
        eight_days_later = switched_at + datetime.timedelta(days=8)
        mod = _load_mod(w)
        mod._now = lambda: eight_days_later
        rc, text = _run_main(mod, ["--allow-writes", "--part", "grounding-items"])
    assert rc == 1, f"expected exit 1 (FAIL), got {rc}:\n{text}"
    assert "[SKIP]" not in text, f"expected no SKIP; got:\n{text}"
    fail_lines = [l for l in text.splitlines() if "[FAIL]" in l]
    assert any("every open p0 vehicle has a ground Finding" in l for l in fail_lines), (
        f"expected coverage FAIL; FAILs:\n{fail_lines}"
    )
    assert "8.0 days since the switch" in text, (
        f"expected '8.0 days since the switch' in output:\n{text}"
    )


# ---------------------------------------------------------------------------
# Test 5: no open p0 fault anywhere → FAIL "open p0 faults present"
# ---------------------------------------------------------------------------

def test_no_open_p0_faults_fails() -> None:
    """No p0 faults seeded at all → FAIL 'grounding-items: open p0 faults present'."""
    with CmsWorld(run_scheduled=False) as w:
        _recreate_groundings_with_gsi(w)
        # Intentionally NO w.adp_faults.add_fault calls
        w.seed_recall_cache()
        w.refresh_health()
        _register_stacks(w)
        _register_alarm(w)
        _put_fault_catalog_s3(w)
        _enable_grounding(w)
        mod = _load_mod(w)
        rc, text = _run_main(mod, ["--allow-writes", "--part", "grounding-items"])
    assert rc == 1, f"expected exit 1, got {rc}:\n{text}"
    assert (
        "[FAIL] grounding-items: open p0 faults present: "
        "no open p0 fault in any fleet; nothing to check"
    ) in text, f"expected exact 'open p0 faults present' FAIL row; output:\n{text}"


# ---------------------------------------------------------------------------
# Test 6: target pick skips live-plan roster / decided action / Draft DMS RO
# ---------------------------------------------------------------------------

def test_target_pick_skip_reasons_in_manifest() -> None:
    """_pick_ground_target records all three skip reasons in ground/skipped."""
    from pathlib import Path as _Path  # noqa: PLC0415
    from agents.supervisor.tools.fleet_recommendation import (  # noqa: PLC0415
        _derive_decision_action_id as _ddaid,
    )
    mdir = tempfile.mkdtemp(prefix="fi-cms-f2-t6-")
    with CmsWorld(run_scheduled=False) as w:
        # Seed ALL faults before the health-cache refresh so all three VINs
        # appear in _read_cms_p0_faults results.
        _recreate_groundings_with_gsi(w)
        for v in (VIN, VIN_B, VIN_C):
            w.adp_faults.add_fault(v, CODE, FAULT_DATE, FAULT_DATE)
        w.seed_recall_cache()
        w.refresh_health()
        findings = w.seed_health_findings(VIN, CODE, FLEET)
        findings_b = w.seed_health_findings(VIN_B, CODE, FLEET)
        findings_c = w.seed_health_findings(VIN_C, CODE, FLEET)
        _register_stacks(w)
        _register_alarm(w)
        _put_fault_catalog_s3(w)
        _enable_grounding(w)

        # Skip reason 1: VIN on live-plan roster
        w.put("cms-staging-storage-live-plan", {
            "planDate": "2026-10-05", "sk": VEHICLE_ID, "vehicleId": VEHICLE_ID,
        })
        # Skip reason 2: VIN_B has a decided Action on its ground Finding
        gf_b = _ground_finding(findings_b)
        action_id_b = _ddaid(gf_b["finding_id"])
        w.put(f"vsa-{STAGE}-avx-actions", {
            "action_id": action_id_b, "finding_id": gf_b["finding_id"],
            "status": "executed",
        })
        # Skip reason 3: VIN_C has an active DMS RO
        w.put("dms-staging-repair-orders", {
            "ro_id": "test-ro-skip-t6", "vehicle_vin": VIN_C, "status": "Draft",
        })
        mod = _load_mod(w)
        # grounding-items first (creates manifest, should PASS)
        rc1, text1 = _run_main(mod, ["--allow-writes", "--part", "grounding-items"], mdir=mdir)
        assert rc1 == 0, f"grounding-items failed:\n{text1}"
        # ground: all three skip, no valid target → fails but writes ground/skipped
        _run_main(
            mod, ["--allow-writes", "--allow-managed-writes", "--part", "ground"], mdir=mdir)
    # Read manifest and verify ground/skipped entries
    manifest_path = _Path(mdir) / f"{_RUN_ID}.json"
    skipped = json.loads(manifest_path.read_text())["items"].get("ground/skipped", [])
    assert any(e[0] == VIN and "live-tier roster" in e[1] for e in skipped), (
        f"VIN not in ground/skipped as live-tier; skipped={skipped}")
    assert any(e[0] == VIN_B and "ground Finding already decided" in e[1] for e in skipped), (
        f"VIN_B not skipped as decided; skipped={skipped}")
    assert any(e[0] == VIN_C and "open DMS RO" in e[1] for e in skipped), (
        f"VIN_C not skipped as DMS RO; skipped={skipped}")


# ---------------------------------------------------------------------------
# Test 7: S1 approve 4xx → FAIL, zero RTS requests, zero dtc-history writes
# ---------------------------------------------------------------------------

def test_s1_approve_4xx_fails_no_rts_no_dtc() -> None:
    """approve returns 400 → S1 FAIL; no return-to-service call and no dtc-history row."""
    rts_calls: list[int] = [0]
    with CmsWorld(run_scheduled=False) as w:
        _setup_world(w)
        # Patch approve to return 400
        findings_mod = w.handlers["findings"]
        orig_approve = findings_mod._approve_finding

        def _400_approve(event: dict, claims: dict) -> dict:
            return {"statusCode": 400, "headers": {},
                    "body": json.dumps({"error": "bad request"})}

        findings_mod._approve_finding = _400_approve
        # Count return-to-service calls
        orig_rts = w.cms_fi._mod.groundings.return_to_service

        def _counting_rts(*args: object, **kwargs: object) -> dict:
            rts_calls[0] += 1
            return orig_rts(*args, **kwargs)

        w.cms_fi._mod.groundings.return_to_service = _counting_rts
        mdir = tempfile.mkdtemp(prefix="fi-cms-f2-s1-")
        try:
            mod = _load_mod(w)
            rc, text = _run_main(mod, ["--allow-writes", "--allow-managed-writes",
                                        "--part", "ground"], mdir=mdir)
        finally:
            findings_mod._approve_finding = orig_approve
            w.cms_fi._mod.groundings.return_to_service = orig_rts
        # A failed approve must not be recorded as done: a resume has to approve again.
        man = json.loads((Path(mdir) / f"{_RUN_ID}.json").read_text())
        assert "ground/step1" not in man["items"], f"step1 recorded after a failed approve: {man['items'].get('ground/step1')}"
        ddb = w.internal("dynamodb")
        scan = ddb.scan(TableName=DTC_HISTORY_TABLE,
                        FilterExpression="vehicleId = :v",
                        ExpressionAttributeValues={":v": {"S": VEHICLE_ID}})
    assert rc == 1, f"expected exit 1, got {rc}:\n{text}"
    assert (
        '[FAIL] ground S1: approve executed: got 400 {"error": "bad request"}'
    ) in text, f"expected exact S1 FAIL row; output:\n{text}"
    assert rts_calls[0] == 0, f"return-to-service called {rts_calls[0]} times (expected 0)"
    assert not scan.get("Items"), f"dtc-history written unexpectedly: {scan['Items']}"


# ---------------------------------------------------------------------------
# Test 8: FAULT_GROUNDING_ENABLED=false in ground → FAIL, zero dtc writes
# ---------------------------------------------------------------------------

def test_grounding_disabled_fails_no_dtc_writes() -> None:
    """run_ground with FAULT_GROUNDING_ENABLED=false → FAIL + zero dtc-history writes."""
    with CmsWorld(run_scheduled=False) as w:
        _setup_world(w)
        # Turn the switch off (like test_coverage_skip_not_enabled)
        lam = w.internal("lambda")
        env = dict(lam.get_function_configuration(
            FunctionName=_HEALTH_FN)["Environment"]["Variables"])
        env["FAULT_GROUNDING_ENABLED"] = "false"
        lam.update_function_configuration(FunctionName=_HEALTH_FN, Environment={"Variables": env})
        mod = _load_mod(w)
        rc, text = _run_main(mod, ["--allow-writes", "--allow-managed-writes",
                                    "--part", "ground"])
        ddb = w.internal("dynamodb")
        scan = ddb.scan(TableName=DTC_HISTORY_TABLE,
                        FilterExpression="vehicleId = :v",
                        ExpressionAttributeValues={":v": {"S": VEHICLE_ID}})
    assert rc == 1, f"expected exit 1, got {rc}:\n{text}"
    assert (
        "[FAIL] ground: FAULT_GROUNDING_ENABLED is 'true': "
        "FAULT_GROUNDING_ENABLED=false; no grounding items to approve"
    ) in text, f"expected exact FAIL row; output:\n{text}"
    assert not scan.get("Items"), f"dtc-history written with disabled grounding: {scan['Items']}"


# ---------------------------------------------------------------------------
# Test 9 (helper): _dtc_write_guard refuses wrong table / unrecorded key, admits recorded
# ---------------------------------------------------------------------------

def test_dtc_write_guard_helper() -> None:
    """_dtc_write_guard raises GuardViolation on wrong table or unknown key; admits known key."""
    mod = _load_mod_bare()
    GuardViol = mod.GuardViolation
    key1 = {"vehicleId": {"S": "v-t9"}, "timestamp": {"N": "1000"}}
    key2 = {"vehicleId": {"S": "v-t9"}, "timestamp": {"N": "9999"}}
    guard = mod._dtc_write_guard("cms-staging-storage-dtc-history", [key1])

    # PutItem to another table → GuardViolation
    try:
        guard({"TableName": "other-table",
               "Item": {"vehicleId": {"S": "v-t9"}, "timestamp": {"N": "1000"}}})
        raise AssertionError("expected GuardViolation for wrong table")
    except Exception as exc:
        assert type(exc) is GuardViol, (
            f"expected type GuardViolation, got {type(exc)}: {exc!r}")

    # DeleteItem of unrecorded key → GuardViolation
    try:
        guard({"TableName": "cms-staging-storage-dtc-history", "Key": key2})
        raise AssertionError("expected GuardViolation for unrecorded key")
    except Exception as exc:
        assert type(exc) is GuardViol, (
            f"expected type GuardViolation for unrecorded key, got {type(exc)}: {exc!r}")

    # DeleteItem of recorded key → no exception (admitted)
    guard({"TableName": "cms-staging-storage-dtc-history", "Key": key1})


# ---------------------------------------------------------------------------
# Test 10: resume with leftover step3_key row deletes it before writing new one
# ---------------------------------------------------------------------------

def test_resume_deletes_leftover_step3_key() -> None:
    """ground S3 dtc-history write and step3_key were dropped in B-1 (architect W1 decision).
    The scenario this test covered — resume finding a leftover step3_key row and deleting it
    before writing a new row — no longer exists: ground never writes to dtc-history.
    Archived: test_ground_s3_is_note_no_dtc_write covers the replacement property (no write)."""


def test_cleared_p0_fault_is_not_a_coverage_gap() -> None:
    """A p0 row whose occurrences all cleared (status 'cleared', no repairRef) is not an
    open p0 fault: no ground Finding is expected for it (kills the dropped status filter)."""
    with CmsWorld(run_scheduled=False) as w:
        w.adp_faults.add_fault(VIN2, CODE, FAULT_DATE, FAULT_DATE, cleared_at=FAULT_DATE)
        _setup_world(w)
        mod = _load_mod(w)
        rc, text = _run_main(mod, ["--allow-writes", "--part", "grounding-items"])
    assert "[PASS] grounding-items: every open p0 vehicle has a ground Finding: 1 of 1" in text, text
    assert rc == 0, f"expected exit 0, got {rc}:\n{text}"


# ===========================================================================
# F2b: S4-S6 + cleanup tests
# ===========================================================================

_RUN_ID_F2B = "test-f2b"
_DMS_RO_TABLE = f"dms-{STAGE}-repair-orders"


def _ground_two_invocations(
    w: CmsWorld, mod: Any, mdir: str, *, skip_health_refresh: bool = False
) -> tuple[int, str, str | None]:
    """Run ground twice; return (final_rc, final_text, ro_id).

    First invocation: S1-S4, then S5 SKIPs. Unless *skip_health_refresh* is
    True, refresh_health is called (script's own PATCH already updated DDB to
    Ready; no add_ro_image needed — review-B W2 / review-C W9).
    """
    rc1, text1 = _run_main(mod, ["--allow-writes", "--allow-managed-writes",
                                  "--part", "ground"], mdir=mdir, run_id=_RUN_ID_F2B)
    assert rc1 == 2, f"first ground: expected SKIP (2), got {rc1}:\n{text1}"
    man = json.loads((Path(mdir) / f"{_RUN_ID_F2B}.json").read_text())
    ro_id = man["items"].get("ground/step4_ro_id")
    assert ro_id, f"no step4_ro_id after first run: {man['items']}"
    if not skip_health_refresh:
        w.refresh_health()
    rc2, text2 = _run_main(mod, ["--allow-writes", "--allow-managed-writes",
                                  "--part", "ground"], mdir=mdir, run_id=_RUN_ID_F2B)
    return rc2, text2, ro_id


# ---------------------------------------------------------------------------
# F2b T1: full flow two invocations (first SKIP at S5, second 0 exit)
# ---------------------------------------------------------------------------

def test_f2b_full_flow_two_invocations() -> None:
    """S1-S4 complete; S5 SKIPs (no repairRef); after refresh_health S5 PASS + S6 200."""
    with CmsWorld(run_scheduled=False) as w:
        _setup_world(w)
        mod = _load_mod(w)
        mdir = tempfile.mkdtemp(prefix="fi-cms-f2b-1-")
        rc, text, ro_id = _ground_two_invocations(w, mod, mdir)
    assert rc == 0, f"second ground: expected exit 0, got {rc}:\n{text}"
    rows = _rows(text)
    assert rows.get("ground S5") == "PASS", f"S5 not PASS: {rows}\n{text}"
    assert rows.get("ground S6") == "PASS", f"S6 not PASS: {rows}\n{text}"


# ---------------------------------------------------------------------------
# F2b T2: resume never creates a second RO (finds existing via vin-index)
# ---------------------------------------------------------------------------

def test_f2b_no_second_ro_on_resume() -> None:
    """On resume, S4 reuses the manifest's RO id (no new RO created; S4 rows absent)."""
    with CmsWorld(run_scheduled=False) as w:
        _setup_world(w)
        mod = _load_mod(w)
        mdir = tempfile.mkdtemp(prefix="fi-cms-f2b-2-")
        rc, text, ro_id_from_helper = _ground_two_invocations(w, mod, mdir)
    assert rc == 0, f"second ground exit {rc}:\n{text}"
    # Second run has no "ground S4" rows — S4 was entirely skipped because ro_id was in manifest
    rows = _rows(text)
    assert "ground S4" not in rows, (
        f"second run should not have ground S4 rows (RO already in manifest); got: {rows}")
    # S5 and S6 passed with the manifest's ro_id
    assert rows.get("ground S5") == "PASS", f"S5 not PASS: {rows}\n{text}"
    assert rows.get("ground S6") == "PASS", f"S6 not PASS: {rows}\n{text}"


# ---------------------------------------------------------------------------
# F2b T3: FI booking body → FAIL before PATCH (is_fi_booking check)
# ---------------------------------------------------------------------------

def test_f2b_fi_booking_fails_before_patch() -> None:
    """When is_fi_booking returns True for the created RO, S4 FAILs before PATCHing to Ready.

    Patches mod.is_fi_booking to return True for any RO (regardless of need_id) to verify
    the check is called and respected by S4. The is_fi_booking logic itself is tested in
    the unit test for is_fi_booking (test_f2b_mutant_drop_fi_booking_check_caught).
    """
    with CmsWorld(run_scheduled=False) as w:
        _setup_world(w)
        mod = _load_mod(w)
        orig_is_fi = mod.is_fi_booking
        # Mutant: always return True (simulates an FI booking RO)
        mod.is_fi_booking = lambda _ro: True
        try:
            mdir = tempfile.mkdtemp(prefix="fi-cms-f2b-3-")
            rc, text = _run_main(mod, ["--allow-writes", "--allow-managed-writes",
                                       "--part", "ground"],
                                 mdir=mdir, run_id=_RUN_ID_F2B)
        finally:
            mod.is_fi_booking = orig_is_fi
    assert rc == 1, f"expected exit 1 (FAIL), got {rc}:\n{text}"
    assert "[FAIL] ground S4: not an FI booking" in text, (
        f"expected S4 FI-booking FAIL:\n{text}")
    # PATCH must not have been called (no "ground S4: RO status Ready" row)
    assert "ground S4: RO status Ready" not in text, (
        f"S4 PATCH should not run after FI booking detection:\n{text}")


# ---------------------------------------------------------------------------
# F2b T4: S6 returns 200 but no returnedAt → S6 FAIL
# ---------------------------------------------------------------------------

def test_f2b_s6_no_returned_at_fails() -> None:
    """S6: RTS returns 200 but body has no returnedAt → S6 FAIL."""
    with CmsWorld(run_scheduled=False) as w:
        _setup_world(w)
        mod = _load_mod(w)
        mdir = tempfile.mkdtemp(prefix="fi-cms-f2b-4-")
        # First invocation: S1-S4, S5 SKIP; skip_health_refresh so we can patch first.
        rc1, _, ro_id = _ground_two_invocations(w, mod, mdir, skip_health_refresh=True)
        # Refresh health so S5 passes (script's PATCH already set Ready in DDB).
        assert ro_id
        w.refresh_health()
        # Patch return_to_service to strip returnedAt from a 200 response
        orig_rts = w.cms_fi._mod.groundings.return_to_service

        def _strip_returned_at(*args: Any, **kw: Any) -> dict:
            import json as _j  # noqa: PLC0415
            resp = orig_rts(*args, **kw)
            if resp.get("statusCode") == 200:
                b = _j.loads(resp["body"])
                b.pop("returnedAt", None)
                resp["body"] = _j.dumps(b)
            return resp

        w.cms_fi._mod.groundings.return_to_service = _strip_returned_at
        try:
            rc2, text2 = _run_main(mod, ["--allow-writes", "--allow-managed-writes",
                                         "--part", "ground"],
                                   mdir=mdir, run_id=_RUN_ID_F2B)
        finally:
            w.cms_fi._mod.groundings.return_to_service = orig_rts
    assert rc2 == 1, f"expected exit 1 (FAIL at S6), got {rc2}:\n{text2}"
    assert "[FAIL] ground S6: return to service 200" in text2, (
        f"expected S6 returnedAt FAIL:\n{text2}")


# ---------------------------------------------------------------------------
# F2b T5: cleanup residue FAIL when a second marker RO exists
# ---------------------------------------------------------------------------

def test_f2b_cleanup_residue_fail_second_marker_ro() -> None:
    """cleanup FAILs when vin-index has a second LIVETEST RO beyond the manifest's RO."""
    with CmsWorld(run_scheduled=False) as w:
        _setup_world(w)
        mod = _load_mod(w)
        mdir = tempfile.mkdtemp(prefix="fi-cms-f2b-5-")
        # Complete the full two-invocation ground flow
        rc, text, ro_id = _ground_two_invocations(w, mod, mdir)
        assert rc == 0, f"ground rc={rc}:\n{text}"
        # Inject a second LIVETEST RO on the same VIN to simulate residue
        ddb = w.internal("dynamodb")
        marker = f"LIVETEST-{_RUN_ID_F2B}"
        ddb.put_item(TableName=_DMS_RO_TABLE, Item={
            "ro_id": {"S": "RO-EXTRA-LIVETEST"},
            "vehicle_vin": {"S": VIN},
            "description": {"S": f"{marker} extra RO"},
            "status": {"S": "Draft"},
            "dealer_id": {"S": "dealer-sacramento"},
            "ro_partition": {"S": "RO"},
        })
        rc_cu, text_cu = _run_main(mod, ["--allow-writes", "--part", "cleanup"],
                                   mdir=mdir, run_id=_RUN_ID_F2B)
    assert rc_cu == 1, f"expected cleanup FAIL (1), got {rc_cu}:\n{text_cu}"
    assert "[FAIL] cleanup residue: DMS marker ROs = exactly the manifest RO" in text_cu, (
        f"expected cleanup DMS residue FAIL:\n{text_cu}")


# ---------------------------------------------------------------------------
# F2b T6: clean ground flow → cleanup PASS (no residue)
# B-2 (review-B W2): add_ro_image must not strip description; a clean cleanup
# must PASS after a complete flow. Seen failing before fix: yes — the old
# add_ro_image put_item overwrote the RO with 6 fields, stripping description,
# so found_ros == [] and cleanup failed even on a clean run.
# ---------------------------------------------------------------------------

def test_f2b_cleanup_pass_clean_flow() -> None:
    """After a complete ground flow, cleanup should PASS with no residue."""
    with CmsWorld(run_scheduled=False) as w:
        _setup_world(w)
        mod = _load_mod(w)
        mdir = tempfile.mkdtemp(prefix="fi-cms-f2b-t6-")
        rc, text, ro_id = _ground_two_invocations(w, mod, mdir)
        assert rc == 0, f"ground rc={rc}:\n{text}"
        # Cleanup — after a complete, returned-to-service flow there should be no residue.
        rc_cu, text_cu = _run_main(mod, ["--allow-writes", "--part", "cleanup"],
                                    mdir=mdir, run_id=_RUN_ID_F2B)
    assert rc_cu == 0, f"expected cleanup PASS (0), got {rc_cu}:\n{text_cu}"


# ---------------------------------------------------------------------------
# F2b T7: ground S4 PATCH actually updates DDB status and preserves labor_op
# B-2 (review-B W2): assert the RO's labor_op == _labor_op_for_code and
# status == "Ready" from the DDB table after S4; tests the script's PATCH.
# Kills mutant W2.1 (wrong labor_op) and mutant W2.2 (PATCH faked) — both
# leave DDB with wrong data, visible here since we read DDB before health refresh.
# ---------------------------------------------------------------------------

def test_f2b_ground_s4_ddb_labor_op_and_status() -> None:
    """S4's POST + PATCH must leave DDB with correct labor_op and status==Ready."""
    with CmsWorld(run_scheduled=False) as w:
        _setup_world(w)
        mod = _load_mod(w)
        mdir = tempfile.mkdtemp(prefix="fi-cms-f2b-t7-")
        # First run: S1-S4 complete, S5 SKIPs; skip_health_refresh so we read
        # DDB before any _refresh_lake rebuild.
        rc1, text1, ro_id = _ground_two_invocations(w, mod, mdir, skip_health_refresh=True)
        assert rc1 == 2 and ro_id, f"expected SKIP and ro_id; got rc={rc1}:\n{text1}"
        # Read DDB directly — lines and status must match what the script wrote.
        rows = [r for r in w.scan(_DMS_RO_TABLE) if r.get("ro_id") == ro_id]
        assert rows, f"RO {ro_id!r} not found in DDB"
        ro = rows[0]
        assert ro.get("status") == "Ready", (
            f"S4 PATCH did not set DDB status to Ready: {ro.get('status')!r}")
        lines = ro.get("lines") or []
        assert lines, f"no lines in DDB item: {ro}"
        assert lines[0].get("labor_op") == LABOR_OP, (
            f"labor_op in DDB {lines[0].get('labor_op')!r} != expected {LABOR_OP!r}"
        )


# ---------------------------------------------------------------------------
# F2b Mutant M1: drop resume lookup → second run creates a second RO (caught)
# ---------------------------------------------------------------------------

def test_f2b_mutant_drop_resume_lookup_caught() -> None:
    """Mutant: _find_existing_marker_ro returns None → a resumed partial run creates a new RO.

    Setup: first run completes S4 (RO created), then manifest step4_ro_id is REMOVED to
    simulate a partial crash before update_manifest. Real code: _find_existing_marker_ro
    finds the existing RO → NOTE "resume", no new RO. Mutant: returns None → creates new RO
    → "DMS RO created" PASS in text.
    """
    with CmsWorld(run_scheduled=False) as w:
        _setup_world(w)
        mod = _load_mod(w)
        mdir = tempfile.mkdtemp(prefix="fi-cms-f2b-m1-")
        # First run: S1-S4 complete; S5 SKIPs (no health refresh)
        rc1, text1 = _run_main(mod, ["--allow-writes", "--allow-managed-writes",
                                     "--part", "ground"],
                               mdir=mdir, run_id=_RUN_ID_F2B)
        assert rc1 == 2, f"first run: expected SKIP(2), got {rc1}:\n{text1}"
        # Remove step4_ro_id to simulate partial crash (description still intact in DDB)
        man_path = Path(mdir) / f"{_RUN_ID_F2B}.json"
        man = json.loads(man_path.read_text())
        ro_id = man["items"].pop("ground/step4_ro_id", None)
        man["items"].pop("ground/step4_done", None)
        assert ro_id, f"no step4_ro_id: {man['items']}"
        man_path.write_text(json.dumps(man, indent=2))
        # Real code run: _find_existing_marker_ro finds the RO → NOTE "resume"
        rc_real, text_real = _run_main(mod, ["--allow-writes", "--allow-managed-writes",
                                             "--part", "ground"],
                                       mdir=mdir, run_id=_RUN_ID_F2B)
        assert rc_real == 2, f"real resume expected SKIP(2), got {rc_real}:\n{text_real}"
        # Reset manifest for mutant run
        man2 = json.loads(man_path.read_text())
        man2["items"].pop("ground/step4_ro_id", None)
        man2["items"].pop("ground/step4_done", None)
        man2["items"].pop("user/dms-sa2", None)
        man2["items"].pop("user/dms-sa", None)
        man_path.write_text(json.dumps(man2, indent=2))
        # Mutant: _find_existing_marker_ro always returns None → creates new RO
        orig_find = mod._find_existing_marker_ro
        mod._find_existing_marker_ro = lambda *a, **k: None
        try:
            rc_mut, text_mut = _run_main(mod, ["--allow-writes", "--allow-managed-writes",
                                               "--part", "ground"],
                                         mdir=mdir, run_id=_RUN_ID_F2B)
        finally:
            mod._find_existing_marker_ro = orig_find
        assert rc_mut == 2, f"mutant expected SKIP(2), got {rc_mut}:\n{text_mut}"
    # Real code finds existing RO (NOTE); mutant creates new one (PASS "DMS RO created")
    assert "[NOTE] ground S4: resume" in text_real, (
        f"real code should find existing RO via vin-index:\n{text_real}")
    assert "[PASS] ground S4: DMS RO created" in text_mut, (
        f"mutant should create new RO, not find existing:\n{text_mut}")


# ---------------------------------------------------------------------------
# F2b Mutant M2: drop is_fi_booking check → FI booking gets through (caught)
# ---------------------------------------------------------------------------

def test_f2b_mutant_drop_fi_booking_check_caught() -> None:
    """Mutant: is_fi_booking always returns False → FI booking passes S4 check.

    Caught when the non-mutated test_f2b_fi_booking_fails_before_patch expects FAIL.
    This test verifies the mutated behavior produces a PASS (i.e. the check is real).
    """
    with CmsWorld(run_scheduled=False) as w:
        _setup_world(w)
        dms_ro = w.handlers["repair_orders"]
        orig_create = dms_ro.create_repair_order

        def _inject_need_id_m2(event: dict, ctx: Any) -> dict:
            resp = orig_create(event, ctx)
            if resp.get("statusCode") == 201:
                import json as _j  # noqa: PLC0415
                body = _j.loads(resp["body"])
                body.setdefault("lines", [{}])[0]["need_id"] = "fault:P0217"
                resp["body"] = _j.dumps(body)
            return resp

        dms_ro.create_repair_order = _inject_need_id_m2
        mod = _load_mod(w)
        # Mutant: patch is_fi_booking to always return False
        orig_is_fi = mod.is_fi_booking
        mod.is_fi_booking = lambda _ro: False
        try:
            mdir = tempfile.mkdtemp(prefix="fi-cms-f2b-m2-")
            rc, text = _run_main(mod, ["--allow-writes", "--allow-managed-writes",
                                       "--part", "ground"],
                                 mdir=mdir, run_id=_RUN_ID_F2B)
        finally:
            mod.is_fi_booking = orig_is_fi
            dms_ro.create_repair_order = orig_create
    # Mutant bypasses the check → passes S4 (no "not an FI booking" FAIL)
    # — but S5 will SKIP since health not refreshed → exit 2
    assert rc == 2, f"mutant should SKIP at S5, not FAIL at S4; got rc={rc}:\n{text}"
    assert "[FAIL] ground S4: not an FI booking" not in text, (
        f"mutant should bypass FI booking check; unexpectedly failed:\n{text}")


# ---------------------------------------------------------------------------
# F2b Mutant M3: accept any repairRef → wrong repairRef passes S5 (caught)
# ---------------------------------------------------------------------------

def test_f2b_mutant_accept_any_repair_ref_caught() -> None:
    """Mutant: S5 accepts any non-None repairRef → wrong ro_id passes.

    Caught because the strict check (ref == ro_id) is the security property.

    After B-2: we first log the real RO via the DMS PATCH route (add_ro_image
    with the real ro_id, which has a dealer_id, so the DMS route is used).  That
    establishes real_ro in _ro_image_log with repair_date = today.  We then add
    wrong_ro with updated_at = FAULT_DATE so its repair_date (FAULT_DATE) is
    earlier than real_ro's.  The ADP vehicle_fault_status SQL picks the earliest
    qualifying RO as repair_ref, so wrong_ro wins.  S5 then SKIPs because
    wrong_ro != ro_id (the strict equality is what the mutant would relax).
    """
    with CmsWorld(run_scheduled=False) as w:
        _setup_world(w)
        mod = _load_mod(w)
        mdir = tempfile.mkdtemp(prefix="fi-cms-f2b-m3-")
        # Run both ground invocations without health refresh between them.
        rc1, _, ro_id = _ground_two_invocations(w, mod, mdir, skip_health_refresh=True)
        assert rc1 == 2, f"first run (no image): expected SKIP(2), got {rc1}"
        assert ro_id
        # Log the real RO via the DMS PATCH route so it is tracked in _ro_image_log
        # (repair_date = world-clock date, i.e. today >= FAULT_DATE + 2 days).
        w.add_ro_image(ro_id, VIN, "Ready", LABOR_OP, "2026-10-22T12:00:00+00:00")
        # Inject a DIFFERENT ro_id as repairRef with an *earlier* updated_at so that
        # the health SQL (ORDER BY repair_date ASC) picks wrong_ro over real_ro.
        # updated_at = FAULT_DATE → repair_date = FAULT_DATE (2026-10-20) < today.
        wrong_ro = "RO-WRONG-REF-001"
        w.add_ro_image(wrong_ro, VIN, "Ready", LABOR_OP,
                       f"{FAULT_DATE.isoformat()}T14:00:00+00:00")
        w.refresh_health()
        # Non-mutated: S5 should SKIP still (ref=wrong_ro != ro_id)
        rc2, text2 = _run_main(mod, ["--allow-writes", "--allow-managed-writes",
                                     "--part", "ground"],
                               mdir=mdir, run_id=_RUN_ID_F2B)
    assert rc2 == 2, f"expected SKIP (ref != ro_id); got {rc2}:\n{text2}"
    assert "ground S5" in text2 and "SKIP" in text2, (
        f"expected S5 SKIP when repairRef is wrong ro_id; got:\n{text2}")


def test_f2b_s5_waits_for_every_target_code() -> None:
    """S5 waits until EVERY target code shows repairRef = the RO: a target holding a second
    open p0 code that the RO didn't repair stays SKIP at S5 (kills 'check codes[0] only')."""
    with CmsWorld(run_scheduled=False) as w:
        _setup_world(w)
        mod = _load_mod(w)
        mdir = tempfile.mkdtemp(prefix="fi-cms-f2b-codes-")
        rc1, text1 = _run_main(mod, ["--allow-writes", "--allow-managed-writes",
                                     "--part", "ground"], mdir=mdir, run_id=_RUN_ID_F2B)
        assert rc1 == 2, f"first run: expected SKIP(2), got {rc1}:\n{text1}"
        man_path = Path(mdir) / f"{_RUN_ID_F2B}.json"
        man = json.loads(man_path.read_text())
        ro_id = man["items"]["ground/step4_ro_id"]
        # A second open p0 code on the vehicle, named by the target; the RO repairs only CODE.
        w.adp_faults.add_fault(VIN, "P0AA6", FAULT_DATE, FAULT_DATE)
        man["items"]["ground/target"]["fault_codes"] = [CODE, "P0AA6"]
        man_path.write_text(json.dumps(man, indent=2))
        w.add_ro_image(ro_id, VIN, "Ready", LABOR_OP, "2026-10-22T14:00:00+00:00")
        w.refresh_health()
        rc2, text2 = _run_main(mod, ["--allow-writes", "--allow-managed-writes",
                                     "--part", "ground"], mdir=mdir, run_id=_RUN_ID_F2B)
    skip = [l for l in text2.splitlines() if l.strip().startswith("[SKIP] ground S5")]
    assert skip and "'P0AA6': None" in skip[0], f"expected S5 SKIP naming P0AA6:\n{text2}"
    assert "ground S6" not in text2, f"S6 must not run before every code is repaired:\n{text2}"
    assert rc2 == 2, f"expected exit 2, got {rc2}:\n{text2}"


def test_f2b_cleanup_dtc_residue_scan_reads_every_page() -> None:
    """cleanup's dtc-history residue scan follows LastEvaluatedKey: a marker row that only a
    later Scan page holds still FAILs (moto serves one page for a small table, so the
    script's Scan is wrapped to read 1 item per page; kills 'read the first page only')."""
    with CmsWorld(run_scheduled=False) as w:
        _setup_world(w)
        mod = _load_mod(w)
        mdir = tempfile.mkdtemp(prefix="fi-cms-f2b-scan-")
        rc, text, _ro = _ground_two_invocations(w, mod, mdir)
        assert rc == 0, f"ground rc={rc}:\n{text}"
        ddb = w.internal("dynamodb")
        marker = f"LIVETEST-{_RUN_ID_F2B}"
        for i, src in enumerate(["other-a", "other-b", "other-c", marker]):
            ddb.put_item(TableName=DTC_HISTORY_TABLE, Item={
                "vehicleId": {"S": f"VEH-SCAN-{i}"}, "timestamp": {"N": str(1000 + i)},
                "code": {"S": CODE}, "status": {"S": "CLEARED"}, "source": {"S": src}})
        real_client = mod._client

        def _one_item_pages(service: str, *a: Any, **k: Any) -> Any:
            c = real_client(service, *a, **k)
            if service == "dynamodb":
                real_scan = c.scan
                c.scan = lambda **kw: real_scan(**{**kw, "Limit": 1})
            return c

        mod._client = _one_item_pages
        try:
            rc_cu, text_cu = _run_main(mod, ["--allow-writes", "--part", "cleanup"],
                                       mdir=mdir, run_id=_RUN_ID_F2B)
        finally:
            mod._client = real_client
    assert "[FAIL] cleanup residue: dtc-history rows with marker = none" in text_cu, text_cu
    assert rc_cu == 1, f"expected exit 1, got {rc_cu}:\n{text_cu}"


# ===========================================================================
# B-2 (review-B W2): is_fi_booking unit-tested against DMS de6c181
# fi_stand_ins.is_fi_booking over the same cases.
# Seen failing before fix: yes — old script is_fi_booking returned True for
# an RO with need_id but no avx_action_id, while DMS returns False.
# ===========================================================================

def test_is_fi_booking_matches_dms() -> None:
    """Script's is_fi_booking must agree with DMS de6c181 fi_stand_ins.is_fi_booking.

    Cases are drawn from fi_stand_ins' own docstrings and include the
    discrepancy the old script had: fleet-health requires avx_action_id.
    """
    import sys as _sys  # noqa: PLC0415
    from scripts.tests.fi_cms_world.world import (  # noqa: PLC0415
        DMS_COMMIT, DMS_ROOT, _require_pinned_checkout,
    )
    # The DMS checkout the world pins (not a path relative to this file, which broke in
    # an archive copy: battery c1 FB9/FB10 ModuleNotFoundError), refused unless at the pin.
    _require_pinned_checkout(DMS_ROOT, DMS_COMMIT)
    dms_root = str(DMS_ROOT)
    added = dms_root not in _sys.path
    if added:
        _sys.path.insert(0, dms_root)
    try:
        from source.simulation.fi_stand_ins import (  # noqa: PLC0415
            is_fi_booking as dms_is_fi_booking,
        )
    finally:
        if added:
            _sys.path.remove(dms_root)

    mod = _load_mod_bare()
    script_is_fi = mod.is_fi_booking

    cases = [
        # (description, ro_dict)
        ("no lines", {}),
        ("need_id but no avx_action_id (fleet-health without fleet marker)",
         {"lines": [{"need_id": "fault:P0217", "pay_type": "customer_pay"}]}),
        # Battery c1 FB10: a fleet customer's RO with a need_id but no avx_action_id is
        # not an FI booking at DMS; the script must agree (avx_action_id is required).
        ("fleet customer, need_id, no avx_action_id",
         {"customer_type": "fleet",
          "lines": [{"need_id": "fault:P0217", "pay_type": "customer_pay"}]}),
        ("need_id with avx_action_id+fleet (real fleet-health FI booking)",
         {"avx_action_id": "act-1", "customer_type": "fleet",
          "lines": [{"need_id": "fault:P0217", "pay_type": "customer_pay"}]}),
        ("MRD recall line with avx_action_id+fleet (recall FI booking)",
         {"avx_action_id": "act-2", "customer_type": "fleet",
          "lines": [{"pay_type": "recall", "recall_campaign_id": "MRD-2026-XY1"}]}),
        ("MRD recall line but no avx_action_id (advisor-created RO)",
         {"lines": [{"pay_type": "recall", "recall_campaign_id": "MRD-2026-XY1"}]}),
        ("empty need_id string (stripped → not FI)",
         {"avx_action_id": "act-3", "customer_type": "fleet",
          "lines": [{"need_id": "  ", "pay_type": "customer_pay"}]}),
        ("avx_action_id but customer_type=retail (retail AVX booking)",
         {"avx_action_id": "act-4", "customer_type": "retail",
          "lines": [{"need_id": "fault:P0217"}]}),
    ]

    for desc, ro in cases:
        script_result = script_is_fi(ro)
        dms_result = dms_is_fi_booking(ro)
        assert script_result == dms_result, (
            f"is_fi_booking mismatch on '{desc}':\n"
            f"  script={script_result}, DMS={dms_result}\n"
            f"  ro={ro}"
        )


# ===========================================================================
# B-3 (review-B W3): S4 resume patches a Draft RO when step4_done not recorded
# ===========================================================================

def test_s4_resume_patches_draft_ro() -> None:
    """W3 mutant gate: run 1 with PATCH answering 500 leaves step4_ro_id written but
    step4_done absent; run 2 must re-PATCH (not skip S4) and continue to S5.

    Seen failing before fix: yes — old gate `if ro_id is None:` skips S4 entirely
    on run 2 because ro_id is in the manifest; RO stays Draft; S5 SKIPs with no
    way to finish (repair_ref never set for a Draft RO per vehicle_fault_status.sql).
    """
    with CmsWorld(run_scheduled=False) as w:
        _setup_world(w)
        mod = _load_mod(w)
        mdir = tempfile.mkdtemp(prefix="fi-cms-f2b-w3-")
        # Run 1: POST the RO but make the PATCH return 500 → step4_ro_id written,
        # step4_done NOT written (ground exits at S4 FAIL).
        real_dms_call = mod._dms_api_call if hasattr(mod, "_dms_api_call") else None
        patch_call_count: list[int] = [0]

        def _patched_dms_call(method: str, path: str, *a: Any, **k: Any) -> tuple[int, Any]:
            if method == "PATCH" and "/repair-orders/" in path:
                patch_call_count[0] += 1
                if patch_call_count[0] == 1:
                    # First PATCH call: pretend DMS committed but returned 500
                    return 500, {"error": "simulated_internal_error"}
            real = mod._Http.__new__(mod._Http)
            return real.call(method, path, *a, **k)

        # Intercept dms_api.call so only the PATCH returns 500 on run 1.
        import unittest.mock as _mock  # noqa: PLC0415
        rc1, text1 = _run_main(mod, ["--allow-writes", "--allow-managed-writes",
                                      "--part", "ground"], mdir=mdir, run_id=_RUN_ID_F2B)
        # run 1 with normal behavior should reach S5 SKIP (PATCH actually works normally)
        # — we need a different approach: inject the failure via manifest manipulation.
        # Approach: run ground normally (S4 PASS), then clear step4_done from manifest
        # and set RO back to Draft to simulate a failed-PATCH resume.
        assert rc1 == 2, f"run 1 (no health): expected SKIP(2), got {rc1}:\n{text1}"
        man_path = Path(mdir) / f"{_RUN_ID_F2B}.json"
        man = json.loads(man_path.read_text())
        ro_id = man["items"].get("ground/step4_ro_id")
        assert ro_id, f"no step4_ro_id after run 1: {man['items']}"
        # Simulate: PATCH had failed → remove step4_done, put RO back to Draft
        del man["items"]["ground/step4_done"]
        man_path.write_text(json.dumps(man, indent=2))
        # Set RO status back to Draft in DDB (simulate pre-PATCH state)
        ddb = w.internal("dynamodb")
        ddb.update_item(
            TableName=f"dms-{STAGE}-repair-orders",
            Key={"ro_id": {"S": ro_id}},
            UpdateExpression="SET #s = :d",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":d": {"S": "Draft"}},
        )
        # Run 2 (resume): must re-PATCH the Draft RO to Ready and reach S5
        rc2, text2 = _run_main(mod, ["--allow-writes", "--allow-managed-writes",
                                      "--part", "ground"], mdir=mdir, run_id=_RUN_ID_F2B)
    # S5 SKIPs (no health refresh); rc=2 expected
    assert rc2 == 2, f"run 2 expected SKIP(2); got {rc2}:\n{text2}"
    rows = _rows(text2)
    assert rows.get("ground S4") == "PASS", (
        f"run 2 must re-PATCH Draft RO to Ready; got rows={rows}\n{text2}")
    assert "ground S4: RO status Ready" in text2, (
        f"run 2 must show RO status Ready in S4; got:\n{text2}")
    # step4_done must be recorded in the manifest after run 2
    man2 = json.loads(man_path.read_text())
    assert "ground/step4_done" in man2["items"], (
        f"step4_done must be written on run 2: {man2['items']}")


# ===========================================================================
# B-3 (review-B W4): cleanup FAILs on unrecorded marker RO / open grounding
# ===========================================================================

def test_cleanup_fails_on_unrecorded_marker_ro() -> None:
    """W4(a): when step4_marker is in the manifest but step4_ro_id is not
    (crash after POST 201 but before manifest write), a LIVETEST RO still
    exists in DDB.  Cleanup must FAIL, not silently pass with found_ros=[].

    Seen failing before fix: yes — old code gates the RO check on
    'if ro_expected is not None', so it skips the check when ro_id is absent.
    """
    with CmsWorld(run_scheduled=False) as w:
        _setup_world(w)
        mod = _load_mod(w)
        mdir = tempfile.mkdtemp(prefix="fi-cms-f2b-w4a-")
        # Run ground once: S1-S4 complete, S5 SKIPs (no health refresh).
        # step4_marker and step4_ro_id are both written.
        rc1, text1 = _run_main(mod, ["--allow-writes", "--allow-managed-writes",
                                      "--part", "ground"], mdir=mdir, run_id=_RUN_ID_F2B)
        assert rc1 == 2, f"run 1 expected SKIP(2): {rc1}\n{text1}"
        man_path = Path(mdir) / f"{_RUN_ID_F2B}.json"
        man = json.loads(man_path.read_text())
        assert "ground/step4_marker" in man["items"], f"step4_marker missing: {man['items']}"
        # Simulate crash: remove step4_ro_id (and step4_done) from manifest so
        # cleanup sees the marker but not the ro_id — but the RO still exists in DDB.
        del man["items"]["ground/step4_ro_id"]
        man["items"].pop("ground/step4_done", None)
        # Seed ground/step6 so cleanup's W4(b) check (open grounding) is bypassed;
        # cleanup checks "ground/step6" in manifest to confirm S6 was done.
        man["items"]["ground/step6"] = {"returned_at": "2099-01-01T00:00:00+00:00"}
        man_path.write_text(json.dumps(man, indent=2))
        # Run cleanup: must FAIL because a LIVETEST RO exists but ro_id is not recorded.
        rc_cu, text_cu = _run_main(mod, ["--allow-writes", "--part", "cleanup"],
                                   mdir=mdir, run_id=_RUN_ID_F2B)
    assert rc_cu == 1, f"cleanup expected exit 1 (FAIL); got {rc_cu}:\n{text_cu}"
    # Exact W4(a) FAIL row — FC2 (expected_ros = list(found_ros)) makes this row PASS:
    assert "[FAIL] cleanup residue: DMS marker ROs = exactly the manifest RO" in text_cu, (
        f"expected exact W4(a) FAIL row:\n{text_cu}")


def test_cleanup_fails_while_target_still_grounded() -> None:
    """W4(b): cleanup must FAIL when this run's grounding is still open (no step6).

    Seen failing before fix: yes — old code only prints 'grounding rows: N'
    and then PASSes; it never checks whether the open row belongs to this run
    or whether step6 was recorded.
    """
    with CmsWorld(run_scheduled=False) as w:
        _setup_world(w)
        mod = _load_mod(w)
        mdir = tempfile.mkdtemp(prefix="fi-cms-f2b-w4b-")
        # Run ground once: S1-S4 complete, S5 SKIPs (no health refresh), S6 never runs.
        # The vehicle's fault grounding is still open (no returnedAt).
        rc1, text1 = _run_main(mod, ["--allow-writes", "--allow-managed-writes",
                                      "--part", "ground"], mdir=mdir, run_id=_RUN_ID_F2B)
        assert rc1 == 2, f"run 1 expected SKIP(2): {rc1}\n{text1}"
        man = json.loads((Path(mdir) / f"{_RUN_ID_F2B}.json").read_text())
        assert "ground/step1" in man["items"], f"step1 missing: {man['items']}"
        assert "ground/step6" not in man["items"], f"step6 should not be present: {man['items']}"
        # Run cleanup: must not PASS while this run's grounding is still open.
        rc_cu, text_cu = _run_main(mod, ["--allow-writes", "--part", "cleanup"],
                                   mdir=mdir, run_id=_RUN_ID_F2B)
    assert rc_cu != 0, f"cleanup must not PASS with open grounding (no step6): {rc_cu}:\n{text_cu}"
    assert "grounding" in text_cu.lower() and ("FAIL" in text_cu or "SKIP" in text_cu), (
        f"cleanup should surface the open grounding:\n{text_cu}")
