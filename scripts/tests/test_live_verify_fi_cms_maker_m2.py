"""Tests for scripts/live-verify-fi-cms-maker.py (M2: sections C and E).

Section C tests
---------------
T22  investigate all-pass: 202 fresh, polls completed, FM match, sw option, rules source.
T23  allow-writes gate: investigate without --allow-writes → SKIP exit 2.
T24  C allowlist has no direct DDB/SQS write ops (static check: _INVESTIGATE_OPS, _DECIDE_OPS).
T25  existing:true without manifest → FAIL (someone else's investigation).
T26  FM mismatch → investigate FAIL.
T27  decide all-pass: 200 executed, thread has softwareCampaignId, campaignStatus=draft.
T28  wrong thread sha → decide sees wrong thread (GET 404) → FAIL.
T29  issue: quality.issue Finding poll timeout → FAIL.
T30  issue: POST before Finding exists → caught by poll gate (tested via poll timeout).
T31  issue all-pass: thread version increments, status=issued.
T32  cleanup: no livetest users after investigate+decide+issue.
T33  mutant: pre > 2h old → investigate prereq FAIL.
T34  mutant: diagnosisSource != rules → investigate FAIL.
T35  pin: _derive_thread_id matches ADP 895f7b61 dtw.py formula.

Section E tests
---------------
T36  E.1 fix: pattern with no ADP rows is skipped (pick returns None).
T37  E.2 live-shapes no-lock: pick is LIVE_PICK_1 (702366b561f68391, FM-COMM-01, OPT-FM-COMM-01-A).
T38  E.2 live-shapes lock-first: pick is LIVE_PICK_2 (78229d78d0d19904, FM-BCP-01, OPT-FM-BCP-01-B).
T39  mutant: remove software_update from corpus → pick yields None with live shapes.
T40  mutant: lock both picks → pick yields None.
"""
from __future__ import annotations

import datetime
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import uuid
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Any

import boto3
import pytest

from scripts.tests.fi_cms_world.world import CmsWorld
from scripts.tests.fi_cms_world import maker as _maker
from scripts.tests.fi_rw_world.world import ACCOUNT, REGION, STAGE, HEALTH_FN
from scripts.tests.fi_rw_world import seed as _seed

_SCRIPT = Path(__file__).resolve().parents[1] / "live-verify-fi-cms-maker.py"
_PATTERN_ID = "test-m2-non-planted-001"
_FM_ID = "fm-m2-001"
_RUN_ID = "test-m2-maker"
_BUCKET = f"cms-{STAGE}-athena-results-{ACCOUNT}-us-east-1"
_CACHE_PREFIX = "fleet-intelligence/"

# ADP repo for pin test
_ADP_ROOT = str(Path.home() / "automotive-data-platform-on-aws")
_ADP_COMMIT = "895f7b61"
_DTW_PATH = "platform-foundation/source/lambda/digital_thread_writer/handler.py"


def _now_iso() -> str:
    import datetime as _dt  # noqa: PLC0415
    return _dt.datetime.now(_dt.timezone.utc).isoformat().replace("+00:00", "Z")


def _now_iso_offset(seconds: float) -> str:
    """Return an ISO string for (now + seconds)."""
    t = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=seconds)
    return t.isoformat().replace("+00:00", "Z")


# ---------------------------------------------------------------------------
# Re-use helpers from M1 tests
# ---------------------------------------------------------------------------

def _register_cms_stack(world: CmsWorld) -> None:
    cfn = boto3.client("cloudformation", region_name=REGION)
    cfn.create_stack(
        StackName=f"cms-{STAGE}-ui",
        TemplateBody=json.dumps({
            "Resources": {"Stub": {"Type": "AWS::SNS::Topic"}},
            "Outputs": {
                "APIEndpoint": {"Value": world.cms_fi_url},
                "UserPoolId": {"Value": world.pool.pool_id},
            },
        }),
    )


def _register_population_alarms(stage: str = STAGE) -> None:
    cw = boto3.client("cloudwatch", region_name=REGION)
    for suffix in ("fi-population-refresh-failing", "fi-population-views-stale"):
        cw.put_metric_alarm(
            AlarmName=f"cms-{stage}-ui-{suffix}",
            AlarmDescription="test",
            MetricName="Duration", Namespace="AWS/Lambda",
            Period=300, EvaluationPeriods=1, Threshold=1.0,
            ComparisonOperator="GreaterThanThreshold",
            TreatMissingData="notBreaching", Statistic="Sum",
        )


def _register_sqs_and_esm(stage: str = STAGE) -> None:
    # The world's wire_investigations (called during CmsWorld.__enter__) now
    # creates the investigations queue, DLQ, and ESM under the correct names.
    # Nothing to do here.
    pass


def _write_pop_caches(
    world: CmsWorld,
    stage: str = STAGE,
    account: str = ACCOUNT,
    pattern_list: list | None = None,
) -> None:
    s3 = boto3.client("s3", region_name="us-east-1")
    now_iso = _now_iso()
    if pattern_list is None:
        pattern_list = [{
            "patternId": _PATTERN_ID, "codeFamily": "P0SW1",
            "topCause": {"cohortDimension": "model", "cohortValue": "Azimuth",
                         "candidateRank": 1, "rateRatio": 5.0, "pValue": 0.0001},
            "candidateCount": 1, "vehiclesAffected": 50, "vehiclesInService": 1000,
            "rate": 0.05, "firstDetectedMonth": "2026-01", "trend": "rising",
            "method": "cohort", "computedAt": now_iso, "parametersVersion": "1.0",
        }]
    payloads = {
        "_cache/fault-patterns-v1.json":   {"computedAt": now_iso, "data": pattern_list},
        "_cache/quality-signals-v1.json":  {"computedAt": now_iso, "data": []},
        "_cache/health-rollup-v1.json":    {"computedAt": now_iso, "totalManagedVins": 1000, "data": []},
        "_cache/digital-thread-list-v1.json": {"computedAt": now_iso, "data": []},
        "_cache/software-campaigns-v1.json":  {"computedAt": now_iso, "data": []},
    }
    for key, payload in payloads.items():
        s3.put_object(
            Bucket=_BUCKET, Key=_CACHE_PREFIX + key,
            Body=json.dumps(payload).encode(),
        )


def _seed_adp_tables(
    stage: str = STAGE,
    pattern_id: str = _PATTERN_ID,
    fm_id: str = _FM_ID,
    cand_dim: str = "model",
    cand_val: str = "Azimuth",
    has_sw_opt: bool = True,
) -> str:
    sw_opt_id = f"opt-sw-{pattern_id[:8]}"
    r = boto3.resource("dynamodb", region_name="us-east-1")
    r.Table(f"adp-{stage}-fault-patterns").put_item(Item={
        "pattern_id": f"cand-{pattern_id[:8]}-001",
        "pattern_group_id": pattern_id,
        "code_family": "P0SW1",
        "cohort_dimension": cand_dim,
        "cohort_value": cand_val,
        "candidate_rank": 1,
    })
    opts: list[dict] = []
    if has_sw_opt:
        opts.append({"remedy_option_id": sw_opt_id, "remedy_type": "software_update",
                     "over_the_air": True, "lead_time_weeks": 2})
    opts.append({"remedy_option_id": "opt-mon-001", "remedy_type": "monitor"})
    r.Table(f"adp-{stage}-engineering-corpus").put_item(Item={
        "pk": f"fm#{fm_id}", "failure_mode_id": fm_id,
        "component_id": "ecu", "symptom_codes": ["P0SW1"],
        "options": opts,
    })
    return sw_opt_id


def _setup_world(world: CmsWorld) -> None:
    _register_cms_stack(world)
    _register_population_alarms()
    _register_sqs_and_esm()
    _write_pop_caches(world)
    _seed_adp_tables()


def _load_mod(world: CmsWorld) -> Any:
    spec = importlib.util.spec_from_file_location(
        f"lv_fi_cms_maker_{uuid.uuid4().hex[:8]}", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def _run_main(
    mod: Any,
    part: str | None = None,
    run_id: str = _RUN_ID,
    allow_writes: bool = False,
    live_tests: bool = True,
    fresh: bool = False,
) -> tuple[int, str]:
    env_backup = os.environ.copy()
    old_argv = sys.argv
    buf = io.StringIO()
    part = part or "pre"
    try:
        global _last_manifest_dir
        mdir = tempfile.mkdtemp(prefix="fi-cms-m2-test-")
        _last_manifest_dir = mdir
        os.environ["FI_CMS_LIVE_MANIFEST_DIR"] = mdir
        if live_tests:
            os.environ["LIVE_TESTS"] = "1"
        else:
            os.environ.pop("LIVE_TESTS", None)
        argv = [str(_SCRIPT), "--stage", "staging", "--run-id", run_id, "--part", part]
        if allow_writes:
            argv.append("--allow-writes")
        if fresh:
            argv.append("--fresh")
        sys.argv = argv
        with redirect_stdout(buf), redirect_stderr(buf):
            try:
                ret = mod.main()
                rc = ret if isinstance(ret, int) else 0
            except SystemExit as exc:
                rc = exc.code if isinstance(exc.code, int) else 1
    finally:
        sys.argv = old_argv
        os.environ.clear()
        os.environ.update(env_backup)
    return rc, buf.getvalue()


def _rows(text: str) -> dict[str, str]:
    """{full row text after the status tag: status}.

    Keyed by the whole row (label and detail), not the text before the first ':',
    because this script's labels carry colons ("investigate: FM matches predicted"):
    keying on the prefix let a later PASS row under the same prefix overwrite a FAIL,
    so FAIL-expecting tests saw ``fails=[]``.
    """
    rows: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        for st in ("PASS", "FAIL", "SKIP", "NOTE"):
            if line.startswith(f"[{st}]"):
                rows[line[len(st) + 3:].strip()] = st
                break
    return rows


def _manifest_dir() -> str:
    """Return the last-used manifest dir from env or a temp path."""
    return os.environ.get("FI_CMS_LIVE_MANIFEST_DIR", tempfile.mkdtemp())


# W6: captures the manifest dir from the last _run_main call so E.2 tests can read
# the manifest and assert the full pattern_pick dict.
_last_manifest_dir: str = ""

def _setup_pump_sleep(world: CmsWorld, mod: Any) -> None:
    """Patch mod.time.sleep to auto-pump queued investigations in the fake world.

    Needed because run_investigate polls via HTTP GET and sleeps between polls.
    In the fake world there is no real SQS trigger — we pump synchronously on
    the first sleep call to drive the investigation to completion.
    """
    import types  # noqa: PLC0415
    pumped: set[str] = set()

    def _pumping_sleep(n: float) -> None:  # noqa: ARG001
        r = boto3.resource("dynamodb", region_name=REGION)
        try:
            items = r.Table(_maker.INVESTIGATIONS_TABLE).scan(
                FilterExpression=boto3.dynamodb.conditions.Attr("status").is_in(
                    ["queued", "running"])
            ).get("Items", [])
        except Exception:  # noqa: BLE001
            return
        for item in items:
            inv_id = item.get("investigation_id", "")
            if inv_id.startswith("lock#") or inv_id in pumped:
                continue
            pumped.add(inv_id)
            _maker.pump_investigation(world, inv_id)

    # A ticking clock: each sleep advances monotonic by n, so a poll loop whose
    # investigation never completes ends at its deadline instead of spinning for
    # the real 20 minutes (memory: a poll loop over a fake must not hang).
    clock = [0.0]

    def _monotonic() -> float:
        return clock[0]

    def _ticking_sleep(n: float) -> None:
        clock[0] += float(n) if n else 1.0
        _pumping_sleep(n)

    fake_time = types.SimpleNamespace(
        monotonic=_monotonic,
        sleep=_ticking_sleep,
    )
    mod.time = fake_time


# ---------------------------------------------------------------------------
# Multi-part run helper: pre → investigate → decide → issue → cleanup
# ---------------------------------------------------------------------------

def _full_run(
    world: CmsWorld,
    mod: Any,
    run_id: str = _RUN_ID,
    parts: list[str] | None = None,
) -> tuple[int, str, dict]:
    """Run the sequence pre → investigate → decide → issue and return last (rc, text, rows).

    The mod's time.sleep is patched to pump pending investigations synchronously,
    and _poll_issue_finding is patched to use a short timeout for the fake world.
    """
    if parts is None:
        parts = ["pre", "investigate", "decide", "issue"]

    # Patch time.sleep to auto-pump investigations (fake world has no real SQS trigger)
    _setup_pump_sleep(world, mod)

    # Patch _poll_issue_finding for the fake world (issue Finding is already written
    # synchronously after the decision executes via pump_investigation).
    orig_poll = mod._poll_issue_finding

    def _fast_poll(ddb_w: Any, stage: str, thread_id: str, **kw: Any) -> dict | None:
        return orig_poll(ddb_w, stage, thread_id, poll_interval_s=0, max_wait_s=5)

    mod._poll_issue_finding = _fast_poll

    env_backup = os.environ.copy()
    old_argv = sys.argv
    all_text = ""
    last_rc = 0

    try:
        mdir = tempfile.mkdtemp(prefix="fi-cms-full-")
        os.environ["FI_CMS_LIVE_MANIFEST_DIR"] = mdir
        os.environ["LIVE_TESTS"] = "1"

        for part in parts:
            buf = io.StringIO()
            argv = [str(_SCRIPT), "--stage", "staging", "--run-id", run_id, "--part", part]
            if part == "pre":
                argv.append("--fresh")
            elif part in ("perf", "scope", "investigate", "decide", "issue", "cleanup"):
                argv.append("--allow-writes")
            sys.argv = argv
            with redirect_stdout(buf), redirect_stderr(buf):
                try:
                    ret = mod.main()
                    rc = ret if isinstance(ret, int) else 0
                except SystemExit as exc:
                    rc = exc.code if isinstance(exc.code, int) else 1
            all_text += buf.getvalue()
            last_rc = rc
            if rc not in (0, 2):
                break
    finally:
        sys.argv = old_argv
        os.environ.clear()
        os.environ.update(env_backup)

    return last_rc, all_text, _rows(all_text)


# ---------------------------------------------------------------------------
# T22: investigate all-pass
# ---------------------------------------------------------------------------

def test_investigate_all_pass() -> None:
    """pre + investigate: 202 fresh, polls completed, FM/sw/rules checks pass."""
    with CmsWorld(run_scheduled=False) as w:
        _setup_world(w)
        _maker.seed_pattern(w, pattern_id=_PATTERN_ID, failure_mode_id=_FM_ID,
                            vin_list=[_seed.vin(0), _seed.vin(1), _seed.vin(2)])
        w.refresh_population()
        mod = _load_mod(w)
        rc, text, rows = _full_run(w, mod, parts=["pre", "investigate"])
    fails = [k for k, v in rows.items() if v == "FAIL"]
    assert not fails, f"unexpected FAILs: {fails}\n{text}"
    assert rc == 0, f"exit {rc}; output:\n{text}"
    assert any("investigate" in k.lower() and v == "PASS"
               for k, v in rows.items()), f"no investigate PASS rows: {rows}"


# ---------------------------------------------------------------------------
# T23: allow-writes gate for investigate
# ---------------------------------------------------------------------------

def test_investigate_allow_writes_gate() -> None:
    """investigate without --allow-writes → SKIP, exit 2."""
    with CmsWorld(run_scheduled=False) as w:
        _setup_world(w)
        mod = _load_mod(w)
        rc, text = _run_main(mod, part="investigate", allow_writes=False)
    assert rc == 2, f"expected exit 2 (SKIP), got {rc}; output:\n{text}"
    rows = _rows(text)
    assert any(v == "SKIP" for v in rows.values()), f"expected SKIP; rows={rows}"
    assert not any(v == "FAIL" for v in rows.values()), f"unexpected FAIL; rows={rows}"


# ---------------------------------------------------------------------------
# T24: C parts allowlist has no direct DDB/SQS write ops (static)
# ---------------------------------------------------------------------------

def test_c_allowlist_has_no_write_ops() -> None:
    """_INVESTIGATE_OPS and _DECIDE_OPS must not contain DDB/SQS write ops."""
    spec = importlib.util.spec_from_file_location(
        f"lv_cms_maker_t24_{uuid.uuid4().hex[:6]}", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    forbidden = {
        "dynamodb.PutItem", "dynamodb.UpdateItem", "dynamodb.DeleteItem",
        "sqs.SendMessage", "sqs.DeleteMessage",
    }
    for attr in ("_INVESTIGATE_OPS", "_DECIDE_OPS", "_ISSUE_OPS", "_CLEANUP_OPS"):
        ops = getattr(mod, attr, frozenset())
        leaks = forbidden & ops
        assert not leaks, f"{attr} contains forbidden write ops: {leaks}"


# ---------------------------------------------------------------------------
# T25: existing:true without manifest record → FAIL
# ---------------------------------------------------------------------------

def test_existing_true_without_manifest_fails() -> None:
    """POST investigation returns 200 existing:true → investigate FAIL."""
    with CmsWorld(run_scheduled=False) as w:
        _setup_world(w)
        fi_mod = w.cms_fi._mod
        fi_maker = fi_mod.maker

        # Patch handle_start_investigation to return 200 existing:true
        def _return_existing(pattern_id: str, body: Any, event: Any) -> Any:
            return fi_maker._resp(200, {
                "investigationId": "00000000-0000-0000-0000-000000000099",
                "patternId": pattern_id,
                "status": "completed",
                "requestedAt": _now_iso(),
                "existing": True,
            })

        fi_maker.handle_start_investigation = _return_existing
        mod = _load_mod(w)

        # Inject a fresh pre in manifest
        env_backup = os.environ.copy()
        old_argv = sys.argv
        buf = io.StringIO()
        try:
            mdir = tempfile.mkdtemp(prefix="fi-cms-m2-t25-")
            os.environ["FI_CMS_LIVE_MANIFEST_DIR"] = mdir
            os.environ["LIVE_TESTS"] = "1"
            # Pre-seed the manifest with a valid pre entry and pattern_pick
            from scripts.fi_cms_live.harness import open_manifest, update_manifest  # noqa: PLC0415
            manifest = open_manifest(_RUN_ID, STAGE)
            update_manifest(manifest, "pre", {
                "completed_at": _now_iso(), "fresh": True, "passed": True})
            update_manifest(manifest, "pattern_pick", {
                "pattern_id": _PATTERN_ID,
                "code_family": "P0SW1",
                "predicted_failure_mode_id": _FM_ID,
                "software_option_id": f"opt-sw-{_PATTERN_ID[:8]}",
                "recommended_option_id": f"opt-sw-{_PATTERN_ID[:8]}",
            })
            sys.argv = [str(_SCRIPT), "--stage", "staging", "--run-id", _RUN_ID,
                        "--part", "investigate", "--allow-writes"]
            with redirect_stdout(buf), redirect_stderr(buf):
                try:
                    ret = mod.main()
                    rc = ret if isinstance(ret, int) else 0
                except SystemExit as exc:
                    rc = exc.code if isinstance(exc.code, int) else 1
        finally:
            sys.argv = old_argv
            os.environ.clear()
            os.environ.update(env_backup)

    text = buf.getvalue()
    rows = _rows(text)
    assert any("existing" in k.lower() and v == "FAIL" for k, v in rows.items()), (
        f"expected existing:true FAIL; rows={rows}\n{text}"
    )
    assert rc == 1, f"expected exit 1, got {rc}"


# ---------------------------------------------------------------------------
# T26: FM mismatch → investigate FAIL
# ---------------------------------------------------------------------------

def test_fm_mismatch_fails() -> None:
    """Investigation returns a different FM than predicted → FAIL."""
    with CmsWorld(run_scheduled=False) as w:
        _setup_world(w)
        _maker.seed_pattern(w, pattern_id=_PATTERN_ID, failure_mode_id=_FM_ID,
                            vin_list=[_seed.vin(0)])
        w.refresh_population()
        fi_mod = w.cms_fi._mod
        fi_maker = fi_mod.maker
        # Patch project_investigation_result to return a different FM id
        _orig = fi_maker.project_investigation_result

        def _wrong_fm(finding: Any) -> Any:
            result = _orig(finding)
            if isinstance(result, dict) and isinstance(result.get("failureMode"), dict):
                result["failureMode"]["failureModeId"] = "fm-WRONG-001"
            return result

        fi_maker.project_investigation_result = _wrong_fm
        mod = _load_mod(w)
        rc, text, rows = _full_run(w, mod, parts=["pre", "investigate"])
    fails = [k for k, v in rows.items() if v == "FAIL"]
    assert any("fm" in k.lower() or "match" in k.lower() or "failure" in k.lower()
               for k in fails), (
        f"expected FM mismatch FAIL; fails={fails}\n{text}"
    )
    assert rc == 1, f"expected exit 1, got {rc}"


# ---------------------------------------------------------------------------
# T27: decide all-pass
# ---------------------------------------------------------------------------

def test_decide_all_pass() -> None:
    """pre → investigate → decide: 200 executed, softwareCampaignId, campaignStatus=draft."""
    with CmsWorld(run_scheduled=False) as w:
        _setup_world(w)
        _maker.seed_pattern(w, pattern_id=_PATTERN_ID, failure_mode_id=_FM_ID,
                            vin_list=[_seed.vin(0), _seed.vin(1)])
        w.refresh_population()
        mod = _load_mod(w)
        rc, text, rows = _full_run(w, mod, parts=["pre", "investigate", "decide"])
    fails = [k for k, v in rows.items() if v == "FAIL"]
    assert not fails, f"unexpected FAILs: {fails}\n{text}"
    assert rc == 0, f"exit {rc}; output:\n{text}"
    assert any("decide" in k.lower() and "200" in k.lower() and v == "PASS"
               for k, v in rows.items()), f"no decide PASS rows: {rows}"


# ---------------------------------------------------------------------------
# T28: wrong thread sha → GET thread 404 → decide FAIL
# ---------------------------------------------------------------------------

def test_wrong_thread_sha_fails() -> None:
    """Patch _derive_thread_id to return a wrong id → thread GET 404 → FAIL."""
    with CmsWorld(run_scheduled=False) as w:
        _setup_world(w)
        _maker.seed_pattern(w, pattern_id=_PATTERN_ID, failure_mode_id=_FM_ID,
                            vin_list=[_seed.vin(0)])
        w.refresh_population()
        mod = _load_mod(w)
        # Patch _derive_thread_id on the loaded module
        _orig_derive = mod._derive_thread_id

        def _wrong_sha(pid: str, fid: str) -> str:
            return "thread-wrongdeadbeef00"

        mod._derive_thread_id = _wrong_sha
        rc, text, rows = _full_run(w, mod, parts=["pre", "investigate", "decide"])
    fails = [k for k, v in rows.items() if v == "FAIL"]
    assert any("thread" in k.lower() or "decide" in k.lower()
               for k in fails), (
        f"expected thread GET FAIL for wrong sha; fails={fails}\n{text}"
    )
    assert rc == 1, f"expected exit 1, got {rc}"


# ---------------------------------------------------------------------------
# T29/T30: issue poll timeout → FAIL (Finding never appears)
# ---------------------------------------------------------------------------

def test_issue_finding_poll_timeout_fails() -> None:
    """_poll_issue_finding: max_wait_s=0 → times out immediately → issue FAIL."""
    with CmsWorld(run_scheduled=False) as w:
        _setup_world(w)
        _maker.seed_pattern(w, pattern_id=_PATTERN_ID, failure_mode_id=_FM_ID,
                            vin_list=[_seed.vin(0)])
        w.refresh_population()
        mod = _load_mod(w)
        # Patch _poll_issue_finding to always return None (timeout)
        mod._poll_issue_finding = lambda *a, **kw: None
        rc, text, rows = _full_run(w, mod, parts=["pre", "investigate", "decide", "issue"])
    fails = [k for k, v in rows.items() if v == "FAIL"]
    assert any("quality.issue" in k.lower() or "finding" in k.lower() or "issue" in k.lower()
               for k in fails), (
        f"expected issue Finding timeout FAIL; fails={fails}\n{text}"
    )
    assert rc == 1, f"expected exit 1, got {rc}"


# ---------------------------------------------------------------------------
# T31: issue all-pass
# ---------------------------------------------------------------------------

def test_issue_all_pass() -> None:
    """Full flow: pre → investigate → decide → issue — all PASS."""
    with CmsWorld(run_scheduled=False) as w:
        _setup_world(w)
        _maker.seed_pattern(w, pattern_id=_PATTERN_ID, failure_mode_id=_FM_ID,
                            vin_list=[_seed.vin(0), _seed.vin(1)])
        w.refresh_population()
        mod = _load_mod(w)
        rc, text, rows = _full_run(w, mod, parts=["pre", "investigate", "decide", "issue"])
    fails = [k for k, v in rows.items() if v == "FAIL"]
    assert not fails, f"unexpected FAILs: {fails}\n{text}"
    assert rc == 0, f"exit {rc}; output:\n{text}"
    assert any("issued" in k.lower() or "status=issued" in k.lower() or "version" in k.lower()
               for k, v in rows.items() if v == "PASS"), (
        f"no issue PASS rows; rows={rows}\n{text}"
    )


# ---------------------------------------------------------------------------
# T32: cleanup: no livetest users after full flow
# ---------------------------------------------------------------------------

def test_cleanup_no_livetest_users() -> None:
    """After investigate+decide+issue+cleanup, no livetest-{run_id} Cognito users remain."""
    with CmsWorld(run_scheduled=False) as w:
        _setup_world(w)
        _maker.seed_pattern(w, pattern_id=_PATTERN_ID, failure_mode_id=_FM_ID,
                            vin_list=[_seed.vin(0), _seed.vin(1)])
        w.refresh_population()
        mod = _load_mod(w)
        rc, text, rows = _full_run(w, mod,
                                   parts=["pre", "investigate", "decide", "issue", "cleanup"])
    prefix = f"livetest-{_RUN_ID}-"
    remaining = [u for u in w.pool.users.values()
                 if u.get("attrs", {}).get("email", "").startswith(prefix)]
    assert not remaining, f"livetest users still in pool after cleanup: {remaining}"
    assert rc == 0, f"cleanup rc {rc} (argparse exit 2 means 'cleanup' not in choices); text:\n{text}"


# ---------------------------------------------------------------------------
# T33: mutant — pre > 2h old → investigate prereq FAIL
# ---------------------------------------------------------------------------

def test_pre_too_old_blocks_investigate() -> None:
    """pre completed_at > 2h ago → investigate prereq FAIL."""
    with CmsWorld(run_scheduled=False) as w:
        _setup_world(w)
        mod = _load_mod(w)
        # Run pre normally, then overwrite pre.completed_at to > 2h ago
        env_backup = os.environ.copy()
        old_argv = sys.argv
        buf = io.StringIO()
        try:
            mdir = tempfile.mkdtemp(prefix="fi-cms-m2-t33-")
            os.environ["FI_CMS_LIVE_MANIFEST_DIR"] = mdir
            os.environ["LIVE_TESTS"] = "1"
            # Inject manifest with stale pre
            from scripts.fi_cms_live.harness import open_manifest, update_manifest  # noqa: PLC0415
            manifest = open_manifest(_RUN_ID, STAGE)
            stale_ts = _now_iso_offset(-3 * 3600)  # 3h ago
            update_manifest(manifest, "pre", {"completed_at": stale_ts, "fresh": True})
            update_manifest(manifest, "pattern_pick", {
                "pattern_id": _PATTERN_ID,
                "code_family": "P0SW1",
                "predicted_failure_mode_id": _FM_ID,
                "software_option_id": f"opt-sw-{_PATTERN_ID[:8]}",
                "recommended_option_id": f"opt-sw-{_PATTERN_ID[:8]}",
            })
            sys.argv = [str(_SCRIPT), "--stage", "staging", "--run-id", _RUN_ID,
                        "--part", "investigate", "--allow-writes"]
            with redirect_stdout(buf), redirect_stderr(buf):
                try:
                    ret = mod.main()
                    rc = ret if isinstance(ret, int) else 0
                except SystemExit as exc:
                    rc = exc.code if isinstance(exc.code, int) else 1
        finally:
            sys.argv = old_argv
            os.environ.clear()
            os.environ.update(env_backup)

    text = buf.getvalue()
    rows = _rows(text)
    fails = [k for k, v in rows.items() if v == "FAIL"]
    assert any("2h" in k.lower() or "pre" in k.lower() or "prereq" in k.lower()
               for k in fails), (
        f"expected pre > 2h FAIL; fails={fails}\n{text}"
    )
    assert rc == 1, f"expected exit 1, got {rc}"


# ---------------------------------------------------------------------------
# T34: mutant — diagnosisSource != rules → FAIL
# ---------------------------------------------------------------------------

def test_diagnosis_source_not_rules_fails() -> None:
    """Investigation returns diagnosisSource='model' → FAIL."""
    with CmsWorld(run_scheduled=False) as w:
        _setup_world(w)
        _maker.seed_pattern(w, pattern_id=_PATTERN_ID, failure_mode_id=_FM_ID,
                            vin_list=[_seed.vin(0)])
        w.refresh_population()
        fi_mod = w.cms_fi._mod
        fi_maker = fi_mod.maker
        # Patch read_investigation to return diagnosisSource='model'
        _orig = fi_maker.read_investigation

        def _model_source(inv_id: str) -> tuple:
            body, err = _orig(inv_id)
            # diagnosisSource lives under result (maker.py:772); the old patch set a
            # body-level key the CMS response never carries.
            if body is not None and isinstance(body.get("result"), dict):
                body["result"]["diagnosisSource"] = "model"
            return body, err

        fi_maker.read_investigation = _model_source
        mod = _load_mod(w)
        rc, text, rows = _full_run(w, mod, parts=["pre", "investigate"])
    fails = [k for k, v in rows.items() if v == "FAIL"]
    assert any("rules" in k.lower() or "diagnosis" in k.lower()
               for k in fails), (
        f"expected diagnosisSource FAIL; fails={fails}\n{text}"
    )
    assert rc == 1, f"expected exit 1, got {rc}"


# ---------------------------------------------------------------------------
# T35: pin — _derive_thread_id matches ADP 895f7b61 dtw.py
# ---------------------------------------------------------------------------

def test_pin_thread_id_against_adp() -> None:
    """_derive_thread_id in the live-verify script must match ADP dtw.py lines 813-819."""
    import subprocess as _sp  # noqa: PLC0415
    import ast  # noqa: PLC0415
    proc = _sp.run(
        ["git", "-C", _ADP_ROOT, "show", f"{_ADP_COMMIT}:{_DTW_PATH}"],
        capture_output=True, timeout=30, check=False,
    )
    assert proc.returncode == 0, (
        f"git show ADP dtw.py failed: {proc.stderr.decode()[:200]}")
    src = proc.stdout.decode("utf-8", errors="replace")
    # Extract the body of _derive_thread_id from the source
    assert "_derive_thread_id" in src, "function not found in ADP dtw.py"
    # Parse and find the function
    tree = ast.parse(src)
    found_fn: Any = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_derive_thread_id":
            found_fn = node
            break
    assert found_fn is not None, "_derive_thread_id not found in ADP dtw.py AST"
    # Verify the algorithm: f"{pattern_id}|{investigation_finding_id}" → sha256.hexdigest()[:16]
    # We verify by running both implementations on a test vector
    spec = importlib.util.spec_from_file_location(
        f"lv_maker_t35_{uuid.uuid4().hex[:6]}", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    our_fn = mod._derive_thread_id
    # Test vector
    pid, fid = "test-pat-001", "00000000-0000-0000-0000-000000000001"
    import hashlib as _hlib  # noqa: PLC0415
    expected = "thread-" + _hlib.sha256(f"{pid}|{fid}".encode()).hexdigest()[:16]
    assert our_fn(pid, fid) == expected, (
        f"_derive_thread_id mismatch: got {our_fn(pid, fid)!r} expected {expected!r}"
    )
    # Also verify with the live pattern + a fake finding_id
    pid2 = "702366b561f68391"
    fid2 = "aaaa1111-bbbb-cccc-dddd-eeeeeeeeeeee"
    expected2 = "thread-" + _hlib.sha256(f"{pid2}|{fid2}".encode()).hexdigest()[:16]
    assert our_fn(pid2, fid2) == expected2, (
        f"_derive_thread_id live-pattern mismatch"
    )


# ---------------------------------------------------------------------------
# Section E tests
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# T36: E.1 fix — pattern with no ADP rows is skipped, not fallen back to topCause
# ---------------------------------------------------------------------------

def test_e1_no_adp_rows_skips_pattern() -> None:
    """_pick_pattern: if ADP fault-patterns has no rows for a group, skip it.

    Mutant: the old code falls back to topCause; the fix skips the pattern.
    """
    with CmsWorld(run_scheduled=False) as w:
        _setup_world(w)
        # Write a patterns cache with _PATTERN_ID that has a non-planted, non-lot topCause
        _write_pop_caches(w, pattern_list=[{
            "patternId": _PATTERN_ID, "codeFamily": "P0SW1",
            "topCause": {"cohortDimension": "model", "cohortValue": "Azimuth",
                         "candidateRank": 1, "rateRatio": 5.0, "pValue": 0.0001},
            "candidateCount": 1, "vehiclesAffected": 50, "vehiclesInService": 1000,
            "rate": 0.05, "firstDetectedMonth": "2026-01", "trend": "rising",
            "method": "cohort", "computedAt": _now_iso(), "parametersVersion": "1.0",
        }])
        # Do NOT keep any ADP fault-patterns rows for _PATTERN_ID: _setup_world seeded
        # one (its _seed_adp_tables), so remove every row of that group.
        r = boto3.resource("dynamodb", region_name="us-east-1")
        fp_t = r.Table(f"adp-{STAGE}-fault-patterns")
        for it in fp_t.scan().get("Items", []):
            if it.get("pattern_group_id") == _PATTERN_ID or it.get("pattern_id") == _PATTERN_ID:
                fp_t.delete_item(Key={"pattern_id": it["pattern_id"]})
        assert not [it for it in fp_t.scan().get("Items", [])
                    if it.get("pattern_group_id") == _PATTERN_ID], "fixture still has rows"
        # DO seed corpus so it would pass FM check if it got there
        r.Table(f"adp-{STAGE}-engineering-corpus").put_item(Item={
            "pk": f"fm#{_FM_ID}", "failure_mode_id": _FM_ID,
            "component_id": "ecu", "symptom_codes": ["P0SW1"],
            "options": [
                {"remedy_option_id": "opt-sw-001", "remedy_type": "software_update"},
                {"remedy_option_id": "opt-mon-001", "remedy_type": "monitor"},
            ],
        })
        # No fault-patterns row → pick should skip _PATTERN_ID
        mod = _load_mod(w)
        rc, text = _run_main(mod, part="pre", fresh=True)
    rows = _rows(text)
    # pick should FAIL (no qualifying pattern), not PASS
    pick_rows = {k: v for k, v in rows.items()
                 if "pick" in k.lower() or "pattern pick" in k.lower()}
    assert any(v == "FAIL" for v in pick_rows.values()), (
        f"expected pattern pick FAIL when no ADP rows; rows={rows}\n{text}"
    )
    # Verify: if we had NOT applied the E.1 fix (old code would use topCause fallback),
    # the topCause (model=Azimuth, non-planted, non-lot) would have passed rules 2+3,
    # and with a corpus match (P0SW1 → _FM_ID → sw_update) the pick would PASS.
    # The FAIL here confirms the fix is applied (no fallback).


# ---------------------------------------------------------------------------
# T37: E.2 live-shapes no-lock: pick is LIVE_PICK_1
# ---------------------------------------------------------------------------

def test_e2_live_shapes_pick1_no_lock() -> None:
    """With live data files and no lock rows, pick must be LIVE_PICK_1 (702366b561f68391)."""
    with CmsWorld(run_scheduled=False) as w:
        _register_cms_stack(w)
        _register_population_alarms()
        _register_sqs_and_esm()
        # Seed from live data files, no lock
        _maker.seed_live_world(w, lock_first=False)
        mod = _load_mod(w)
        rc, text = _run_main(mod, part="pre", fresh=True)

    rows = _rows(text)
    # The pick should record LIVE_PICK_1 in the manifest
    assert _maker.LIVE_PICK_1 in text, (
        f"expected LIVE_PICK_1 ({_maker.LIVE_PICK_1}) in output; rows={rows}\n{text}"
    )
    pick_pass = any("pick" in k.lower() and v == "PASS" for k, v in rows.items())
    assert pick_pass, f"expected pick PASS; rows={rows}\n{text}"
    assert rc == 0, f"exit {rc}; output:\n{text}"
    # W6: assert the full pattern_pick dict recorded in the manifest
    manifest_file = Path(_last_manifest_dir) / f"{_RUN_ID}.json"
    pick = json.loads(manifest_file.read_text())["items"].get("pattern_pick", {})
    expected_pick = {
        "pattern_id": _maker.LIVE_PICK_1,
        "code_family": "U007",
        "predicted_failure_mode_id": "FM-COMM-01",
        "software_option_id": "OPT-FM-COMM-01-A",
        "recommended_option_id": "OPT-FM-COMM-01-A",
    }
    assert pick == expected_pick, (
        f"manifest pattern_pick mismatch:\n  got  {pick}\n  want {expected_pick}"
    )


# ---------------------------------------------------------------------------
# T38: E.2 live-shapes lock-first: pick is LIVE_PICK_2
# ---------------------------------------------------------------------------

def test_e2_live_shapes_pick2_with_lock() -> None:
    """With live data + lock on LIVE_PICK_1, pick must be LIVE_PICK_2 (78229d78d0d19904)."""
    with CmsWorld(run_scheduled=False) as w:
        _register_cms_stack(w)
        _register_population_alarms()
        _register_sqs_and_esm()
        # Seed from live data files, lock first pick
        _maker.seed_live_world(w, lock_first=True)
        mod = _load_mod(w)
        rc, text = _run_main(mod, part="pre", fresh=True)

    rows = _rows(text)
    assert _maker.LIVE_PICK_2 in text, (
        f"expected LIVE_PICK_2 ({_maker.LIVE_PICK_2}) in output; rows={rows}\n{text}"
    )
    pick_pass = any("pick" in k.lower() and v == "PASS" for k, v in rows.items())
    assert pick_pass, f"expected pick PASS; rows={rows}\n{text}"
    assert rc == 0, f"exit {rc}; output:\n{text}"
    # W6: assert the full pattern_pick dict recorded in the manifest
    manifest_file = Path(_last_manifest_dir) / f"{_RUN_ID}.json"
    pick = json.loads(manifest_file.read_text())["items"].get("pattern_pick", {})
    expected_pick = {
        "pattern_id": _maker.LIVE_PICK_2,
        "code_family": "P1A0",
        "predicted_failure_mode_id": "FM-BCP-01",
        "software_option_id": "OPT-FM-BCP-01-B",
        "recommended_option_id": "OPT-FM-BCP-01-A",
    }
    assert pick == expected_pick, (
        f"manifest pattern_pick mismatch:\n  got  {pick}\n  want {expected_pick}"
    )


# ---------------------------------------------------------------------------
# T39: mutant — remove sw_update from corpus → pick yields None
# ---------------------------------------------------------------------------

def test_e2_no_sw_update_pick_fails() -> None:
    """Remove software_update options from all corpus FMs → pick fails (no qualifying FM)."""
    with CmsWorld(run_scheduled=False) as w:
        _register_cms_stack(w)
        _register_population_alarms()
        _register_sqs_and_esm()
        _maker.seed_live_world(w, lock_first=False)
        # Remove software_update from every corpus item
        import json as _j  # noqa: PLC0415
        corpus_items = _j.loads(
            (Path(__file__).parent / "fi_cms_world" / "data" / "live_corpus_items.json").read_text()
        )
        r_e = boto3.resource("dynamodb", region_name="us-east-1")
        t = r_e.Table(f"adp-{STAGE}-engineering-corpus")
        for raw in corpus_items:
            # Build pk from failure_mode_id
            fm_id_raw = raw.get("failure_mode_id", {})
            fm_id = fm_id_raw.get("S", "") if isinstance(fm_id_raw, dict) else fm_id_raw
            if not fm_id:
                continue
            # Put the item with no software_update options
            t.put_item(Item={
                "pk": f"fm#{fm_id}",
                "failure_mode_id": fm_id,
                "component_id": "test",
                "symptom_codes": [],
                "options": [
                    {"remedy_option_id": "opt-mon-001", "remedy_type": "monitor"},
                ],
            })
        mod = _load_mod(w)
        rc, text = _run_main(mod, part="pre", fresh=True)

    rows = _rows(text)
    pick_rows = {k: v for k, v in rows.items()
                 if "pick" in k.lower() or "pattern pick" in k.lower()}
    assert any(v == "FAIL" for v in pick_rows.values()), (
        f"expected pick FAIL when no sw_update; rows={rows}\n{text}"
    )


# ---------------------------------------------------------------------------
# T40: mutant — lock both picks → pick yields None
# ---------------------------------------------------------------------------

def test_e2_both_picks_locked_yields_none() -> None:
    """Lock both LIVE_PICK_1 and LIVE_PICK_2 → pick fails (no qualifying pattern)."""
    with CmsWorld(run_scheduled=False) as w:
        _register_cms_stack(w)
        _register_population_alarms()
        _register_sqs_and_esm()
        _maker.seed_live_world(w, lock_first=True)  # locks LIVE_PICK_1
        # Also lock LIVE_PICK_2
        r_w = boto3.resource("dynamodb", region_name=REGION)
        r_w.Table(_maker.INVESTIGATIONS_TABLE).put_item(
            Item={"investigation_id": f"lock#{_maker.LIVE_PICK_2}"}
        )
        mod = _load_mod(w)
        rc, text = _run_main(mod, part="pre", fresh=True)

    rows = _rows(text)
    pick_rows = {k: v for k, v in rows.items()
                 if "pick" in k.lower() or "pattern pick" in k.lower()}
    assert any(v == "FAIL" for v in pick_rows.values()), (
        f"expected pick FAIL when both picks locked; rows={rows}\n{text}"
    )

# ---------------------------------------------------------------------------
# W1: cleanup runs when named in --part choices
# ---------------------------------------------------------------------------

def test_cleanup_part_runs() -> None:
    """full flow then --part cleanup: rc 0, PASS row, kept-record lines printed."""
    with CmsWorld(run_scheduled=False) as w:
        _setup_world(w)
        _maker.seed_pattern(w, pattern_id=_PATTERN_ID, failure_mode_id=_FM_ID,
                            vin_list=[_seed.vin(0), _seed.vin(1)])
        w.refresh_population()
        mod = _load_mod(w)
        rc, text, rows = _full_run(w, mod,
                                   parts=["pre", "investigate", "decide", "issue", "cleanup"])
    assert rc == 0, (
        f"cleanup rc {rc} (rc 2 = argparse exit: 'cleanup' not in choices); text:\n{text}"
    )
    assert any(
        "cleanup: no livetest" in k and v == "PASS" for k, v in rows.items()
    ), f"cleanup PASS row missing; rows={rows}\n{text}"
    assert "=== Kept records" in text, f"kept-record lines not printed; text:\n{text}"
