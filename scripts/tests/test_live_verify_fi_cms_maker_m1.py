"""Tests for scripts/live-verify-fi-cms-maker.py (M1: pre/perf/scope).

Each test drives the real ``main()`` against ``CmsWorld``.

Tests
-----
T1  all-pass: pre exits 0; perf+scope exit 0 with --allow-writes.
T2  maker-check bypass: fleet-op let through → probe FAIL.
T3  GET leaks fleetId key → FAIL.
T4  GET leaks demo fleet id in string → FAIL.
T5  POST control doesn't reach validation (returns 202) → FAIL.
T6  p95 > 3 s → FAIL.
T7  temp users deleted on success.
T8  temp users deleted on raise.
T9  subprocess from temp cwd with PYTHONPATH stripped → exit 2.
T10 LIVE_TESTS gate: no LIVE_TESTS → exit 2, no boto3 client built.
T11 allow-writes gate: scope without --allow-writes → SKIP, exit 2.
T12 pre allowlist has no Cognito/write ops (static check).
T13 pick excludes a planted-match candidate.
T14 pick excludes a supplier_lot candidate.
T15 pick excludes a locked pattern.
T16 pick excludes an FM with no software_update option.
T17 nested forbidden key in response → FAIL.
T18 nested fleet id string value → FAIL.
T19 CS list route 404 → FAIL.
T20 CS list route returns unavailable → FAIL.
T21 pins: FORBIDDEN_TOKENS == CMS maker at 1ce93a75b; PLANTS == ADP 895f7b61.
"""
from __future__ import annotations

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

from scripts.tests.fi_cms_world.world import CmsWorld
from scripts.tests.fi_cms_world import cms_fi as _cms_fi_mod
from scripts.tests.fi_rw_world.world import ACCOUNT, REGION, STAGE, HEALTH_FN

_SCRIPT = Path(__file__).resolve().parents[1] / "live-verify-fi-cms-maker.py"
_PATTERN_ID = "test-m1-non-planted-001"
_FM_ID = "fm-m1-001"
_RUN_ID = "test-m1-maker"
_BUCKET = f"cms-{STAGE}-athena-results-{ACCOUNT}-us-east-1"
_CACHE_PREFIX = "fleet-intelligence/"

# ADP repo for pin tests
_ADP_ROOT = str(Path.home() / "automotive-data-platform-on-aws")
_ADP_COMMIT = "895f7b61"
_ADP_PLANTS_PATH = (
    "platform-foundation/source/data-products/"
    "fleet_fault_events/fleet_fault_events/plants.py"
)
_CMS_COMMIT = "1ce93a75b"
_CMS_MAKER_PATH = "services/fleet_intelligence/maker.py"


def _now_iso() -> str:
    import datetime as _dt  # noqa: PLC0415
    return _dt.datetime.now(_dt.timezone.utc).isoformat().replace("+00:00", "Z")


class _FakeTime:
    """Fake time module: monotonic() advances by per_call each call; sleep() is a no-op."""
    def __init__(self, per_call: float = 4.0) -> None:
        self._val = 0.0
        self._per_call = per_call

    def monotonic(self) -> float:
        self._val += self._per_call
        return self._val

    def sleep(self, n: float) -> None:
        pass


# ---------------------------------------------------------------------------
# World fixture helpers
# ---------------------------------------------------------------------------

def _register_cms_stack(world: CmsWorld) -> None:
    """Register cms-staging-ui CFN stack in moto."""
    import boto3  # noqa: PLC0415
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
    """Create both population alarms in OK state."""
    import boto3  # noqa: PLC0415
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
    """The world's wire_investigations creates the investigations queue, DLQ, and ESM.
    Nothing to do here — the world setup already handles this correctly.
    """
    pass


def _write_pop_caches(world: CmsWorld, stage: str = STAGE, account: str = ACCOUNT,
                     pattern_list: list | None = None,
                     campaigns_list: list | None = None) -> None:
    """Write minimal valid population cache objects to S3."""
    import boto3  # noqa: PLC0415
    s3 = boto3.client("s3", region_name="us-east-1")
    cache_keys = {
        "patterns": "_cache/fault-patterns-v1.json",
        "signals": "_cache/quality-signals-v1.json",
        "health_rollup": "_cache/health-rollup-v1.json",
        "threads": "_cache/digital-thread-list-v1.json",
        "campaigns": "_cache/software-campaigns-v1.json",
    }
    now_iso = _now_iso()
    if pattern_list is None:
        pattern_list = [
            {
                "patternId": _PATTERN_ID,
                "codeFamily": "P0SW1",
                "topCause": {
                    "cohortDimension": "model",   # not supplier_lot / planted
                    "cohortValue": "Azimuth",
                    "candidateRank": 1,
                    "rateRatio": 5.0,
                    "pValue": 0.0001,
                },
                "candidates": [{"patternGroupId": _PATTERN_ID, "cohortDimension": "model",
                                "cohortValue": "Azimuth", "candidateRank": 1}],
                "candidateCount": 1, "vehiclesAffected": 50, "vehiclesInService": 1000,
                "rate": 0.05, "firstDetectedMonth": "2026-01", "trend": "rising",
                "method": "cohort", "computedAt": now_iso, "parametersVersion": "1.0",
            }
        ]
    payloads = {
        "patterns": {"computedAt": now_iso, "data": pattern_list},
        "signals": {"computedAt": now_iso, "data": [
            {"codeFamily": "P0SW1", "cohortDimension": "model", "cohortValue": "Azimuth",
             "rate30d": 0.05, "rate90d": 0.04, "riseRatio": 1.25, "events30d": 100,
             "vehiclesAffected30d": 50, "firstSignalDate": "2026-01", "parametersVersion": "1.0"}
        ]},
        "health_rollup": {"computedAt": now_iso, "totalManagedVins": 1000, "data": [
            {"cohortValue": "Azimuth", "kindCounts": {"diagnostics": {"p0": 5}}}
        ]},
        "threads": {"computedAt": now_iso, "data": []},
        "campaigns": {"computedAt": now_iso,
                      "data": campaigns_list if campaigns_list is not None else []},
    }
    for name, key in cache_keys.items():
        s3.put_object(
            Bucket=_BUCKET,
            Key=_CACHE_PREFIX + key,
            Body=json.dumps(payloads[name]).encode(),
        )


def _seed_adp_tables(
    stage: str = STAGE,
    pattern_id: str = _PATTERN_ID,
    fm_id: str = _FM_ID,
    cand_dim: str = "model",
    cand_val: str = "Azimuth",
    has_sw_opt: bool = True,
) -> str:
    """Seed adp-{stage}-fault-patterns and adp-{stage}-engineering-corpus.

    Uses the new table structure:
    - fault-patterns: pattern_id (pk), pattern_group_id, code_family,
                      cohort_dimension, cohort_value, candidate_rank.
                      NO failure_mode_id.
    - engineering-corpus: pk=fm#<id>, failure_mode_id, component_id,
                          symptom_codes (list), options (list of maps).

    Returns sw_opt_id (the software_update remedy_option_id).
    """
    import boto3  # noqa: PLC0415
    sw_opt_id = f"opt-sw-{pattern_id[:8]}"
    r = boto3.resource("dynamodb", region_name="us-east-1")
    # fault-patterns row — NEW structure (no failure_mode_id)
    r.Table(f"adp-{stage}-fault-patterns").put_item(Item={
        "pattern_id":       f"cand-{pattern_id[:8]}-001",   # unique pk
        "pattern_group_id": pattern_id,                      # = CMS cache patternId
        "code_family":      "P0SW1",
        "cohort_dimension": cand_dim,
        "cohort_value":     cand_val,
        "candidate_rank":   1,
    })
    # corpus: symptom_codes must match "P0SW1"
    opts: list[dict] = []
    if has_sw_opt:
        opts.append({"remedy_option_id": sw_opt_id, "remedy_type": "software_update",
                     "over_the_air": True, "lead_time_weeks": 2})
    opts.append({"remedy_option_id": "opt-mon-001", "remedy_type": "monitor"})
    r.Table(f"adp-{stage}-engineering-corpus").put_item(Item={
        "pk":              f"fm#{fm_id}",
        "failure_mode_id": fm_id,
        "component_id":    "ecu",
        "symptom_codes":   ["P0SW1"],
        "options":         opts,
    })
    return sw_opt_id


def _setup_world(world: CmsWorld) -> None:
    """Seed all data needed for pre/perf/scope to pass."""
    _register_cms_stack(world)
    _register_population_alarms()
    _register_sqs_and_esm()
    _write_pop_caches(world)
    _seed_adp_tables()


# ---------------------------------------------------------------------------
# Module loading / main() runner
# ---------------------------------------------------------------------------

def _load_mod(world: CmsWorld) -> Any:
    spec = importlib.util.spec_from_file_location(
        f"lv_fi_cms_maker_{uuid.uuid4().hex[:8]}", _SCRIPT
    )
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def _run_main(
    mod: Any,
    part: str | None = None,
    run_id: str = _RUN_ID,
    allow_writes: bool = False,
    live_tests: bool = True,
) -> tuple[int, str]:
    """Set sys.argv, call main(), return (exit_code, stdout+stderr)."""
    env_backup = os.environ.copy()
    old_argv = sys.argv
    buf = io.StringIO()
    part = part or "pre"
    try:
        mdir = tempfile.mkdtemp(prefix="fi-cms-m1-test-")
        os.environ["FI_CMS_LIVE_MANIFEST_DIR"] = mdir
        if live_tests:
            os.environ["LIVE_TESTS"] = "1"
        else:
            os.environ.pop("LIVE_TESTS", None)
        argv = [str(_SCRIPT), "--stage", "staging", "--run-id", run_id,
                "--part", part]
        if allow_writes:
            argv.append("--allow-writes")
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
    """Parse [PASS/FAIL/SKIP/NOTE] rows from output."""
    rows: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        for st in ("PASS", "FAIL", "SKIP", "NOTE"):
            if line.startswith(f"[{st}]"):
                label = line[len(st) + 3:].strip().split(":")[0].strip()
                rows[label] = st
                break
    return rows


# ---------------------------------------------------------------------------
# T1: all-pass (pre + perf + scope)
# ---------------------------------------------------------------------------

def test_all_pass() -> None:
    with CmsWorld(run_scheduled=False) as w:
        _setup_world(w)
        mod = _load_mod(w)
        # pre
        rc_pre, text_pre = _run_main(mod, part="pre")
        # perf
        rc_perf, text_perf = _run_main(mod, part="perf", allow_writes=True)
        # scope
        rc_scope, text_scope = _run_main(mod, part="scope", allow_writes=True)
    text = text_pre + text_perf + text_scope
    for part, rc in [("pre", rc_pre), ("perf", rc_perf), ("scope", rc_scope)]:
        assert rc == 0, f"{part}: exit {rc}; output:\n{text}"
    rows = _rows(text)
    fails = [k for k, v in rows.items() if v == "FAIL"]
    assert not fails, f"unexpected FAILs: {fails}\n{text}"
    # W8: scope must have successfully fetched and checked one real pattern detail
    assert any(
        "scope eng get patterns/" in k.lower() and v == "PASS"
        for k, v in rows.items()
    ), f"no 'scope eng GET patterns/<id>: 200' PASS row; rows={rows}\n{text}"


# ---------------------------------------------------------------------------
# T2: fleet-operator let through maker check → probe FAILs
# ---------------------------------------------------------------------------

def test_fleet_op_bypass_fails() -> None:
    """Patch authorize_maker_read to return True → fleet-op probe returns 200 → FAIL."""
    with CmsWorld(run_scheduled=False) as w:
        _setup_world(w)
        fi_mod = w.cms_fi._mod
        fi_maker = fi_mod.maker

        def _bypass(claims: Any) -> bool:
            return True

        fi_maker._auth.authorize_maker_read = _bypass
        mod = _load_mod(w)
        rc, text = _run_main(mod, part="scope", allow_writes=True)
    rows = _rows(text)
    assert any(
        ("fleet-op" in k.lower() or "fleet-operator" in k.lower() or "fleet_op" in k.lower()
         or "probe" in k.lower())
        and rows[k] == "FAIL"
        for k in rows
    ), f"expected a fleet-op probe FAIL; rows={rows}\n{text}"
    assert rc == 1, f"expected exit 1, got {rc}"


# ---------------------------------------------------------------------------
# T3: GET leaks a fleetId key → FAIL
# ---------------------------------------------------------------------------

def test_get_leaks_fleet_id_key_fails() -> None:
    """Pattern item with 'fleetId' key → scope scan FAIL."""
    with CmsWorld(run_scheduled=False) as w:
        _setup_world(w)
        fi_mod = w.cms_fi._mod
        fi_maker = fi_mod.maker
        _orig_proj = fi_maker.project_pattern_item

        def _leaky_proj(row: Any) -> Any:
            result = _orig_proj(row)
            result["fleetId"] = "demo-rental-carshare"
            return result

        fi_maker.project_pattern_item = _leaky_proj
        mod = _load_mod(w)
        rc, text = _run_main(mod, part="scope", allow_writes=True)
    rows = _rows(text)
    assert any("scan" in k.lower() and rows[k] == "FAIL" for k in rows), (
        f"expected scope scan FAIL for fleetId key leak; rows={rows}\n{text}"
    )
    assert rc == 1, f"expected exit 1, got {rc}"


# ---------------------------------------------------------------------------
# T4: GET leaks demo fleet id inside a string value → FAIL
# ---------------------------------------------------------------------------

def test_get_leaks_demo_fleet_id_in_string_fails() -> None:
    """Pattern item's codeFamily contains 'demo-rental-carshare' → FAIL."""
    with CmsWorld(run_scheduled=False) as w:
        _setup_world(w)
        fi_mod = w.cms_fi._mod
        fi_maker = fi_mod.maker
        _orig_proj = fi_maker.project_pattern_item

        def _leaky_val(row: Any) -> Any:
            result = _orig_proj(row)
            result["codeFamily"] = "demo-rental-carshare"
            return result

        fi_maker.project_pattern_item = _leaky_val
        mod = _load_mod(w)
        rc, text = _run_main(mod, part="scope", allow_writes=True)
    rows = _rows(text)
    assert any("scan" in k.lower() and rows[k] == "FAIL" for k in rows), (
        f"expected scope scan FAIL for demo fleet id in value; rows={rows}\n{text}"
    )
    assert rc == 1, f"expected exit 1, got {rc}"


# ---------------------------------------------------------------------------
# T5: POST control doesn't reach validation (returns 202) → FAIL
# ---------------------------------------------------------------------------

def test_post_ctrl_no_validation_fails() -> None:
    """Make handle_start_investigation return 202 even with unexpected body → ctrl FAIL."""
    with CmsWorld(run_scheduled=False) as w:
        _setup_world(w)
        fi_mod = w.cms_fi._mod
        fi_maker = fi_mod.maker

        def _always_202(pattern_id: str, body: Any, event: Any) -> Any:
            return fi_maker._resp(202, {
                "investigationId": "00000000-0000-0000-0000-000000000002",
                "patternId": pattern_id, "status": "queued",
                "requestedAt": _now_iso(), "existing": False,
            })

        fi_maker.handle_start_investigation = _always_202
        mod = _load_mod(w)
        rc, text = _run_main(mod, part="scope", allow_writes=True)
    rows = _rows(text)
    assert any(
        "ctrl" in k.lower() and "invest" in k.lower() and rows[k] == "FAIL"
        for k in rows
    ), f"expected ctrl POST investigations FAIL; rows={rows}\n{text}"
    assert rc == 1, f"expected exit 1, got {rc}"


# ---------------------------------------------------------------------------
# T6: p95 > 3 s → FAIL
# ---------------------------------------------------------------------------

def test_perf_p95_too_slow() -> None:
    """Use _FakeTime to make monotonic() advance 4 s per call; p95 > 3 s → FAIL."""
    with CmsWorld(run_scheduled=False) as w:
        _setup_world(w)
        mod = _load_mod(w)
        mod.time = _FakeTime(per_call=4.0)
        rc, text = _run_main(mod, part="perf", allow_writes=True)
    rows = _rows(text)
    assert any("p95" in k.lower() and "pattern" in k.lower() and rows[k] == "FAIL"
               for k in rows), (
        f"expected patterns p95 FAIL; rows={rows}\n{text}"
    )
    assert rc == 1, f"expected exit 1, got {rc}"


# ---------------------------------------------------------------------------
# T7: temp users deleted on success
# ---------------------------------------------------------------------------

def test_users_deleted_on_success() -> None:
    with CmsWorld(run_scheduled=False) as w:
        _setup_world(w)
        mod = _load_mod(w)
        rc, text = _run_main(mod, part="scope", allow_writes=True)
    assert rc == 0, f"exit {rc}\n{text}"
    prefix = f"livetest-{_RUN_ID}-"
    remaining = [
        u for u in w.pool.users.values()
        if u.get("attrs", {}).get("email", "").startswith(prefix)
    ]
    assert not remaining, f"livetest users still in pool: {remaining}"


# ---------------------------------------------------------------------------
# T8: temp users deleted when main raises
# ---------------------------------------------------------------------------

def test_users_deleted_on_raise() -> None:
    """Raise inside scope → finally cleanup still runs, users gone."""
    with CmsWorld(run_scheduled=False) as w:
        _setup_world(w)
        fi_mod = w.cms_fi._mod
        fi_maker = fi_mod.maker
        _calls = [0]
        _orig_hp = fi_maker.handle_patterns

        def _raise_third(qs: Any) -> Any:
            _calls[0] += 1
            if _calls[0] >= 3:
                raise RuntimeError("injected test error")
            return _orig_hp(qs)

        fi_maker.handle_patterns = _raise_third
        mod = _load_mod(w)
        try:
            _run_main(mod, part="scope", allow_writes=True)
        except Exception:
            pass
    prefix = f"livetest-{_RUN_ID}-"
    remaining = [
        u for u in w.pool.users.values()
        if u.get("attrs", {}).get("email", "").startswith(prefix)
    ]
    assert not remaining, f"livetest users not cleaned up on raise: {remaining}"


# ---------------------------------------------------------------------------
# T9: subprocess from temp cwd with PYTHONPATH stripped → exit 2
# ---------------------------------------------------------------------------

def test_subprocess_no_pythonpath() -> None:
    """Run script without PYTHONPATH and without LIVE_TESTS; expects exit 2."""
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    env.pop("LIVE_TESTS", None)
    with tempfile.TemporaryDirectory() as td:
        result = subprocess.run(
            [sys.executable, str(_SCRIPT), "--stage", "staging",
             "--run-id", "subprocess-test"],
            cwd=td, env=env, capture_output=True, text=True, timeout=60,
        )
    assert result.returncode not in (None,), "process killed"
    assert "ImportError" not in result.stderr, f"ImportError: {result.stderr[:400]}"
    assert "AttributeError" not in result.stderr, f"AttributeError: {result.stderr[:400]}"
    # Without LIVE_TESTS=1 the gate fires and returns 2.
    assert result.returncode == 2, (
        f"expected rc 2 (LIVE_TESTS gate), got {result.returncode}\n"
        f"stdout: {result.stdout[:400]}\nstderr: {result.stderr[:400]}"
    )


# ---------------------------------------------------------------------------
# T10: LIVE_TESTS gate: no LIVE_TESTS → exit 2, no boto3 client built
# ---------------------------------------------------------------------------

def test_live_tests_gate_no_client_built() -> None:
    """Without LIVE_TESTS=1, main() returns 2 and never calls _client."""
    with CmsWorld(run_scheduled=False) as w:
        _setup_world(w)
        mod = _load_mod(w)
        client_calls: list[str] = []
        _orig_client = mod._client

        def _spy_client(service: str, *a: Any, **kw: Any) -> Any:
            client_calls.append(service)
            return _orig_client(service, *a, **kw)

        mod._client = _spy_client
        rc, text = _run_main(mod, part="pre", live_tests=False)
    assert rc == 2, f"expected exit 2 (LIVE_TESTS gate), got {rc}; output:\n{text}"
    assert "SKIP" in text, f"expected SKIP in output; got:\n{text}"
    assert not client_calls, f"_client was called despite no LIVE_TESTS: {client_calls}"


# ---------------------------------------------------------------------------
# T11: allow-writes gate: scope without --allow-writes → SKIP, exit 2
# ---------------------------------------------------------------------------

def test_allow_writes_gate_skips_scope() -> None:
    """scope without --allow-writes → SKIP row, exit 2 (no FAIL)."""
    with CmsWorld(run_scheduled=False) as w:
        _setup_world(w)
        mod = _load_mod(w)
        # scope part, no allow_writes flag
        rc, text = _run_main(mod, part="scope", allow_writes=False)
    assert rc == 2, f"expected exit 2 (SKIP), got {rc}; output:\n{text}"
    rows = _rows(text)
    assert any(v == "SKIP" for v in rows.values()), (
        f"expected at least one SKIP row; rows={rows}\n{text}"
    )
    assert not any(v == "FAIL" for v in rows.values()), (
        f"unexpected FAIL rows; rows={rows}\n{text}"
    )


# ---------------------------------------------------------------------------
# T12: pre allowlist has no Cognito/write ops (static check)
# ---------------------------------------------------------------------------

def test_pre_allowlist_has_no_write_ops() -> None:
    """_PRE_OPS must not contain Cognito, PutItem, UpdateItem, DeleteItem, SendMessage."""
    spec = importlib.util.spec_from_file_location(
        f"lv_fi_cms_maker_t12_{uuid.uuid4().hex[:6]}", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    pre_ops = mod._PRE_OPS
    forbidden_in_pre = {
        "cognito-idp.AdminCreateUser", "cognito-idp.AdminDeleteUser",
        "cognito-idp.AdminSetUserPassword", "cognito-idp.AdminAddUserToGroup",
        "cognito-idp.InitiateAuth", "cognito-idp.ListUserPoolClients",
        "dynamodb.PutItem", "dynamodb.UpdateItem", "dynamodb.DeleteItem",
        "sqs.SendMessage",
    }
    leaks = forbidden_in_pre & pre_ops
    assert not leaks, f"_PRE_OPS contains write/Cognito ops: {leaks}"



# ---------------------------------------------------------------------------
# T12b: spy main()'s _client for --part pre — assert allowed_ops has no write ops
# ---------------------------------------------------------------------------

def test_pre_built_clients_have_no_write_ops() -> None:
    """Spy mod._client during main() --part pre; assert every allowed_ops arg is
    disjoint from Cognito/write ops.

    W10: T12 checks the constant but not what main() actually selects in its
    if/elif block. This test exercises that selection path.
    """
    _WRITE_OPS_FORBIDDEN = frozenset({
        "cognito-idp.AdminCreateUser", "cognito-idp.AdminDeleteUser",
        "cognito-idp.AdminSetUserPassword", "cognito-idp.AdminAddUserToGroup",
        "cognito-idp.InitiateAuth",
        "dynamodb.PutItem", "dynamodb.UpdateItem", "dynamodb.DeleteItem",
        "dynamodb.BatchWriteItem", "dynamodb.TransactWriteItems",
        "sqs.SendMessage",
        "s3.PutObject",
    })
    with CmsWorld(run_scheduled=False) as w:
        _setup_world(w)
        mod = _load_mod(w)

        captured_ops: list[frozenset[str]] = []
        _orig_client = mod._client

        def _spy_client(service: str, region: str, *, allowed_ops: frozenset[str],
                        **kw: Any) -> Any:
            captured_ops.append(allowed_ops)
            return _orig_client(service, region, allowed_ops=allowed_ops, **kw)

        mod._client = _spy_client
        # Stub run_pre so we don't need real AWS calls — we only care about
        # what ops _client was called with.
        mod.run_pre = lambda *a, **kw: None  # type: ignore[assignment]

        rc, _text = _run_main(mod, part="pre")

    assert captured_ops, "_client was never called for --part pre"
    leaks: frozenset[str] = frozenset()
    for ops in captured_ops:
        leaks |= ops & _WRITE_OPS_FORBIDDEN
    assert not leaks, (
        f"pre --part _client was called with write/Cognito ops: {sorted(leaks)}"
    )


# ---------------------------------------------------------------------------
# T12c: spy main()'s _client for --part cleanup — assert no Cognito write ops (MB19)
# ---------------------------------------------------------------------------

def test_cleanup_built_clients_have_no_cognito_write_ops() -> None:
    """Spy mod._client during main() --part cleanup; assert no Cognito create/modify ops.

    MB19 updated for W-3: cleanup MAY call AdminDeleteUser/AdminGetUser
    (for delete_manifest_users), but MUST NOT call AdminCreateUser,
    AdminSetUserPassword, or AdminAddUserToGroup.
    """
    _COGNITO_CREATE_OPS = frozenset({
        "cognito-idp.AdminCreateUser", "cognito-idp.AdminSetUserPassword",
        "cognito-idp.AdminAddUserToGroup",
    })
    with CmsWorld(run_scheduled=False) as w:
        _setup_world(w)
        mod = _load_mod(w)

        captured_ops: list[frozenset[str]] = []
        _orig_client = mod._client

        def _spy_client(service: str, region: str, *, allowed_ops: frozenset[str],
                        **kw: Any) -> Any:
            captured_ops.append(allowed_ops)
            return _orig_client(service, region, allowed_ops=allowed_ops, **kw)

        mod._client = _spy_client
        # Stub run_cleanup so no actual Cognito/DDB calls happen
        mod.run_cleanup = lambda *a, **kw: None  # type: ignore[assignment]

        # cleanup doesn't need --allow-writes, but does need a valid pool_id path
        rc, _text = _run_main(mod, part="cleanup")

    assert captured_ops, "_client was never called for --part cleanup"
    leaks: frozenset[str] = frozenset()
    for ops in captured_ops:
        leaks |= ops & _COGNITO_CREATE_OPS
    assert not leaks, (
        f"cleanup --part _client was called with Cognito create/modify ops: {sorted(leaks)}"
    )


# ---------------------------------------------------------------------------
# T13: pick excludes a planted-match candidate
# ---------------------------------------------------------------------------

def test_pick_excludes_planted_match() -> None:
    """Seed a pattern whose cohort matches a PLANTS entry → pick skips it, no pattern found."""
    with CmsWorld(run_scheduled=False) as w:
        _setup_world(w)
        import boto3  # noqa: PLC0415
        r = boto3.resource("dynamodb", region_name="us-east-1")
        # Overwrite the fault-patterns row with a row that matches a PLANTS entry:
        # P0AA build_window "2024-02:2024-05" → cohort_dimension=build_window_3m,
        # code_family=P0AA. But the cache uses codeFamily "P0SW1" for _PATTERN_ID,
        # so to test planting we need to write the cache with a planted codeFamily.
        planted_pid = "test-m1-planted-001"
        # Write new cache with the planted pattern ID
        _write_pop_caches(w, pattern_list=[{
            "patternId": planted_pid, "codeFamily": "P0AA",
            "topCause": {"cohortDimension": "build_window_4m", "cohortValue": "2024-02:2024-05",
                         "candidateRank": 1, "rateRatio": 4.0, "pValue": 0.0001},
            "candidateCount": 1, "vehiclesAffected": 49, "vehiclesInService": 1000,
            "rate": 0.049, "firstDetectedMonth": "2025-02", "trend": "stable",
            "method": "cohort", "computedAt": _now_iso(), "parametersVersion": "1.0",
        }])
        # Seed fault-patterns with planted candidate
        r.Table(f"adp-{STAGE}-fault-patterns").put_item(Item={
            "pattern_id": f"cand-planted-001",
            "pattern_group_id": planted_pid,
            "code_family": "P0AA",
            "cohort_dimension": "build_window_4m",
            "cohort_value": "2024-02:2024-05",   # matches P0AA build_window plant
            "candidate_rank": 1,
        })
        # Corpus: P0AA → some FM with software_update
        r.Table(f"adp-{STAGE}-engineering-corpus").put_item(Item={
            "pk": "fm#fm-p0aa-001", "failure_mode_id": "fm-p0aa-001",
            "component_id": "hv", "symptom_codes": ["P0AA"],
            "options": [{"remedy_option_id": "opt-sw-p0aa", "remedy_type": "software_update"}],
        })
        mod = _load_mod(w)
        rc, text = _run_main(mod, part="pre")
    rows = _rows(text)
    # Should have a FAIL on pattern pick (no qualifying pattern found)
    assert any(
        ("pattern pick" in k.lower() or "pick" in k.lower()) and rows[k] == "FAIL"
        for k in rows
    ), f"expected pattern pick FAIL for planted match; rows={rows}\n{text}"


# ---------------------------------------------------------------------------
# T14: pick excludes a supplier_lot candidate
# ---------------------------------------------------------------------------

def test_pick_excludes_lot_candidate() -> None:
    """Seed a fault-patterns row with cohort_dimension=supplier_lot → pick skips it."""
    with CmsWorld(run_scheduled=False) as w:
        _setup_world(w)
        import boto3  # noqa: PLC0415
        r = boto3.resource("dynamodb", region_name="us-east-1")
        # Overwrite the candidate row with supplier_lot dimension
        r.Table(f"adp-{STAGE}-fault-patterns").delete_item(
            Key={"pattern_id": f"cand-{_PATTERN_ID[:8]}-001"})
        r.Table(f"adp-{STAGE}-fault-patterns").put_item(Item={
            "pattern_id": f"cand-{_PATTERN_ID[:8]}-001",
            "pattern_group_id": _PATTERN_ID,
            "code_family": "P0SW1",
            "cohort_dimension": "supplier_lot",   # ← triggers exclusion
            "cohort_value": "SUP-X/LOT-001",
            "candidate_rank": 1,
        })
        mod = _load_mod(w)
        rc, text = _run_main(mod, part="pre")
    rows = _rows(text)
    assert any(
        ("pattern pick" in k.lower() or "pick" in k.lower()) and rows[k] == "FAIL"
        for k in rows
    ), f"expected pattern pick FAIL for supplier_lot candidate; rows={rows}\n{text}"


# ---------------------------------------------------------------------------
# T15: pick excludes a locked pattern
# ---------------------------------------------------------------------------

def test_pick_excludes_locked_pattern() -> None:
    """Put a lock row in investigations → pick skips that pattern."""
    with CmsWorld(run_scheduled=False) as w:
        _setup_world(w)
        import boto3  # noqa: PLC0415
        # Insert a lock row in investigations table
        ddb = boto3.client("dynamodb", region_name=REGION)
        ddb.put_item(
            TableName=f"vsa-{STAGE}-avx-investigations",
            Item={"investigation_id": {"S": f"lock#{_PATTERN_ID}"}},
        )
        mod = _load_mod(w)
        rc, text = _run_main(mod, part="pre")
    rows = _rows(text)
    assert any(
        ("pattern pick" in k.lower() or "pick" in k.lower()) and rows[k] == "FAIL"
        for k in rows
    ), f"expected pattern pick FAIL for locked pattern; rows={rows}\n{text}"


# ---------------------------------------------------------------------------
# T16: pick excludes FM with no software_update option
# ---------------------------------------------------------------------------

def test_pick_excludes_fm_no_software_update() -> None:
    """Corpus has no software_update remedy → pick skips."""
    with CmsWorld(run_scheduled=False) as w:
        _setup_world(w)
        import boto3  # noqa: PLC0415
        r = boto3.resource("dynamodb", region_name="us-east-1")
        # Overwrite corpus with no software_update option
        r.Table(f"adp-{STAGE}-engineering-corpus").put_item(Item={
            "pk": f"fm#{_FM_ID}", "failure_mode_id": _FM_ID,
            "component_id": "ecu", "symptom_codes": ["P0SW1"],
            "options": [
                {"remedy_option_id": "opt-mon-001", "remedy_type": "monitor"},
            ],
        })
        mod = _load_mod(w)
        rc, text = _run_main(mod, part="pre")
    rows = _rows(text)
    assert any(
        ("pattern pick" in k.lower() or "pick" in k.lower()) and rows[k] == "FAIL"
        for k in rows
    ), f"expected pattern pick FAIL for no software_update; rows={rows}\n{text}"


# ---------------------------------------------------------------------------
# T17: nested forbidden key in response → FAIL
# ---------------------------------------------------------------------------

def test_nested_forbidden_key_fails() -> None:
    """Inject 'fleetId' into topCause (nested dict) → recursive scan FAIL."""
    with CmsWorld(run_scheduled=False) as w:
        _setup_world(w)
        fi_mod = w.cms_fi._mod
        fi_maker = fi_mod.maker
        _orig_proj = fi_maker.project_pattern_item

        def _inject_nested(row: Any) -> Any:
            result = _orig_proj(row)
            # Inject forbidden key inside topCause (a nested dict)
            if isinstance(result.get("topCause"), dict):
                result["topCause"]["fleetId"] = "injected-nested"
            else:
                result["topCause"] = {"fleetId": "injected-nested"}
            return result

        fi_maker.project_pattern_item = _inject_nested
        mod = _load_mod(w)
        rc, text = _run_main(mod, part="scope", allow_writes=True)
    rows = _rows(text)
    assert any("scan" in k.lower() and rows[k] == "FAIL" for k in rows), (
        f"expected scope scan FAIL for nested fleetId key; rows={rows}\n{text}"
    )
    assert rc == 1, f"expected exit 1, got {rc}"


# ---------------------------------------------------------------------------
# T18: nested fleet id string value → FAIL
# ---------------------------------------------------------------------------

def test_nested_fleet_id_string_value_fails() -> None:
    """Inject 'demo-rental-carshare' as a string deep inside topCause → FAIL."""
    with CmsWorld(run_scheduled=False) as w:
        _setup_world(w)
        fi_mod = w.cms_fi._mod
        fi_maker = fi_mod.maker
        _orig_proj = fi_maker.project_pattern_item

        def _inject_deep_val(row: Any) -> Any:
            result = _orig_proj(row)
            if isinstance(result.get("topCause"), dict):
                result["topCause"]["cohortValue"] = "demo-rental-carshare"
            else:
                result["topCause"] = {"cohortValue": "demo-rental-carshare"}
            return result

        fi_maker.project_pattern_item = _inject_deep_val
        mod = _load_mod(w)
        rc, text = _run_main(mod, part="scope", allow_writes=True)
    rows = _rows(text)
    assert any("scan" in k.lower() and rows[k] == "FAIL" for k in rows), (
        f"expected scope scan FAIL for nested fleet id value; rows={rows}\n{text}"
    )
    assert rc == 1, f"expected exit 1, got {rc}"


# ---------------------------------------------------------------------------
# T19: CS list route returns non-200 → FAIL
# ---------------------------------------------------------------------------

def test_cs_list_non_200_fails() -> None:
    """Make patterns return 503 WITH data (not None) → s==200 guard catches it."""
    with CmsWorld(run_scheduled=False) as w:
        _setup_world(w)
        fi_mod = w.cms_fi._mod
        fi_maker = fi_mod.maker
        _orig_hp = fi_maker.handle_patterns

        def _return_503_with_data(qs: Any) -> Any:
            # Return 503 but include a non-None data field so only the status guard fires
            return fi_maker._resp(503, {
                "data": [{"patternId": _PATTERN_ID, "codeFamily": "P0SW1"}],
                "computedAt": _now_iso(),
            })

        fi_maker.handle_patterns = _return_503_with_data
        mod = _load_mod(w)
        rc, text = _run_main(mod, part="scope", allow_writes=True)
    rows = _rows(text)
    assert any(
        "cs" in k.lower() and "pattern" in k.lower() and rows[k] == "FAIL"
        for k in rows
    ), f"expected CS patterns GET FAIL for 503; rows={rows}\n{text}"
    assert rc == 1, f"expected exit 1, got {rc}"


# ---------------------------------------------------------------------------
# T20: CS list route returns 200 with unavailable → FAIL
# ---------------------------------------------------------------------------

def test_cs_list_unavailable_fails() -> None:
    """Make patterns return 200 with unavailableReason AND data=[] → unavailable guard fires."""
    with CmsWorld(run_scheduled=False) as w:
        _setup_world(w)
        fi_mod = w.cms_fi._mod
        fi_maker = fi_mod.maker
        _orig_hp = fi_maker.handle_patterns

        def _return_unavailable_with_empty_data(qs: Any) -> Any:
            # 200 with unavailableReason and data=[] (not None) so only the
            # unavailableReason guard fires (data is not None would pass)
            return fi_maker._resp(200, {
                "data": [],
                "unavailableReason": "partial-cache",
                "lastComputedAt": None,
            })

        fi_maker.handle_patterns = _return_unavailable_with_empty_data
        mod = _load_mod(w)
        rc, text = _run_main(mod, part="scope", allow_writes=True)
    rows = _rows(text)
    assert any(
        "cs" in k.lower() and "pattern" in k.lower() and rows[k] == "FAIL"
        for k in rows
    ), f"expected CS patterns GET FAIL for unavailable; rows={rows}\n{text}"
    assert rc == 1, f"expected exit 1, got {rc}"


# ---------------------------------------------------------------------------
# T21: pins — FORBIDDEN_TOKENS == CMS maker; PLANTS == ADP 895f7b61
# ---------------------------------------------------------------------------

def test_pin_forbidden_tokens_against_cms_maker() -> None:
    """FORBIDDEN_TOKENS in the live-verify script must equal CMS maker.py FORBIDDEN_TOKENS."""
    import subprocess as _sp  # noqa: PLC0415
    import ast  # noqa: PLC0415
    from scripts.tests.fi_cms_world import cms_fi  # noqa: PLC0415
    spec = importlib.util.spec_from_file_location(
        f"lv_maker_pin_{uuid.uuid4().hex[:6]}", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    script_tokens = mod._FORBIDDEN_TOKENS
    # Extract CMS maker FORBIDDEN_TOKENS via git show
    proc = _sp.run(
        ["git", "-C", str(cms_fi.CMS_REPO), "show",
         f"{cms_fi.CMS_COMMIT}:services/fleet_intelligence/maker.py"],
        capture_output=True, timeout=30, check=False,
    )
    assert proc.returncode == 0, f"git show CMS maker failed: {proc.stderr.decode()[:200]}"
    src = proc.stdout.decode("utf-8", errors="replace")
    tree = ast.parse(src)
    cms_tokens: frozenset[str] | None = None
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        for t in node.targets:
            if not (isinstance(t, ast.Name) and t.id == "FORBIDDEN_TOKENS"):
                continue
            val = node.value
            # FORBIDDEN_TOKENS = frozenset({...}) → Call(func=Name, args=[Set])
            if isinstance(val, ast.Call):
                fn = val.func
                if (isinstance(fn, ast.Name) and fn.id == "frozenset"
                        and val.args and isinstance(val.args[0], ast.Set)):
                    elts = val.args[0].elts
                    cms_tokens = frozenset(
                        elt.value if isinstance(elt, ast.Constant) else ""
                        for elt in elts
                    )
    assert cms_tokens is not None, "could not extract FORBIDDEN_TOKENS from CMS maker.py"
    assert script_tokens == cms_tokens, (
        f"FORBIDDEN_TOKENS mismatch.\n"
        f"  script has but CMS lacks: {script_tokens - cms_tokens}\n"
        f"  CMS has but script lacks: {cms_tokens - script_tokens}"
    )

    # --- Extended W7 pins ---
    # 2. Regex .pattern: _COORD_PAIR_RE, _FLEET_ID_RE, PATTERN_ID_RE, THREAD_ID_RE,
    #    INVESTIGATION_ID_RE — extract from CMS AST and compare with script
    _REGEX_NAMES = (
        "_COORD_PAIR_RE", "_FLEET_ID_RE", "PATTERN_ID_RE", "THREAD_ID_RE", "INVESTIGATION_ID_RE",
    )
    cms_regex_patterns: dict[str, str | None] = {n: None for n in _REGEX_NAMES}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        for t in node.targets:
            if not (isinstance(t, ast.Name) and t.id in cms_regex_patterns):
                continue
            val = node.value
            if isinstance(val, ast.Call) and val.args and isinstance(val.args[0], ast.Constant):
                cms_regex_patterns[t.id] = val.args[0].value
    for rname in _REGEX_NAMES:
        cms_pat = cms_regex_patterns[rname]
        assert cms_pat is not None, f"could not extract {rname}.pattern from CMS maker.py"
        script_pat = getattr(mod, rname).pattern
        assert script_pat == cms_pat, (
            f"{rname} .pattern mismatch:\n  script={script_pat!r}\n  CMS={cms_pat!r}"
        )

    # _FORBIDDEN_TOKEN_RE is derived from FORBIDDEN_TOKENS (same formula in both)
    # Can't compare .pattern directly (frozenset sort-order is non-deterministic) —
    # compare the set of alternatives instead.
    import re as _re  # noqa: PLC0415
    def _re_alts(pattern: str) -> frozenset[str]:
        """Extract frozenset of alternates from r"\\b(a|b|...)\\b"."""
        inner = pattern[3:-3]  # strip leading r"\b(" and trailing r")\b"
        return frozenset(inner.split("|"))
    cms_ftre_alts = _re_alts(_re.compile(
        r"\b(" + "|".join(_re.escape(t) for t in sorted(cms_tokens, key=len, reverse=True)) + r")\b"
    ).pattern)
    script_ftre_alts = _re_alts(mod._FORBIDDEN_TOKEN_RE.pattern)
    assert script_ftre_alts == cms_ftre_alts, (
        f"_FORBIDDEN_TOKEN_RE alternatives mismatch:\n"
        f"  script not in CMS: {script_ftre_alts - cms_ftre_alts}\n"
        f"  CMS not in script: {cms_ftre_alts - script_ftre_alts}"
    )

    # 3. String constants: FORBIDDEN_MSG, QUALITY_AGENT, POPULATION_ID, FINDING_KEY_INDEX
    _STR_NAMES = ("FORBIDDEN_MSG", "QUALITY_AGENT", "POPULATION_ID", "FINDING_KEY_INDEX")
    cms_str_vals: dict[str, str | None] = {n: None for n in _STR_NAMES}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        for t in node.targets:
            if not (isinstance(t, ast.Name) and t.id in cms_str_vals):
                continue
            val = node.value
            if isinstance(val, ast.Constant) and isinstance(val.value, str):
                cms_str_vals[t.id] = val.value
    for sname in _STR_NAMES:
        cms_val = cms_str_vals[sname]
        assert cms_val is not None, f"could not extract {sname} from CMS maker.py"
        script_val = getattr(mod, sname)
        assert script_val == cms_val, (
            f"{sname} mismatch:\n  script={script_val!r}\n  CMS={cms_val!r}"
        )

    # 4. compute_finding_key: extract the function from CMS source and call both on a test vector
    import types as _types  # noqa: PLC0415
    # Find the compute_finding_key FunctionDef in the CMS AST
    cms_cfk_func = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "compute_finding_key":
            cms_cfk_func = node
            break
    assert cms_cfk_func is not None, "could not find compute_finding_key in CMS maker.py"
    # Build a minimal module containing just the function
    cms_module_ast = ast.Module(body=[cms_cfk_func], type_ignores=[])
    ast.fix_missing_locations(cms_module_ast)
    cms_ns: dict = {"__name__": "__cms_pin__"}
    exec(  # noqa: S102
        compile(cms_module_ast, "<cms_maker_pin>", "exec"),
        cms_ns,
    )
    # Provide required imports to the namespace
    import hashlib as _hashlib  # noqa: PLC0415
    import json as _json  # noqa: PLC0415
    cms_ns["hashlib"] = _hashlib
    cms_ns["json"] = _json
    cms_cfk = cms_ns["compute_finding_key"]
    _vec = dict(
        agent_id="agent-fi-quality",
        scope_field="population_id",
        scope_value="meridian-motors",
        finding_kind="quality.investigation",
        subject_key="PAT-001",
    )
    assert mod.compute_finding_key(**_vec) == cms_cfk(**_vec), (
        "compute_finding_key output differs from CMS maker.py"
    )


# ---------------------------------------------------------------------------
# T35: pin _derive_thread_id against ADP 895f7b61 digital_thread_writer/handler.py
# ---------------------------------------------------------------------------

_ADP_DTW_HANDLER_PATH = "platform-foundation/source/lambda/digital_thread_writer/handler.py"


def test_pin_thread_id_against_adp() -> None:
    """exec ADP's _derive_thread_id and compare output with the script's copy.

    T35: unlike the prior approach (presence-check + inline formula comparison),
    this actually executes the ADP function on a test input and verifies the outputs
    are equal — catching any divergence in the actual computation.
    """
    import subprocess as _sp  # noqa: PLC0415
    import ast  # noqa: PLC0415
    proc = _sp.run(
        ["git", "-C", _ADP_ROOT, "show", f"{_ADP_COMMIT}:{_ADP_DTW_HANDLER_PATH}"],
        capture_output=True, timeout=30, check=False,
    )
    assert proc.returncode == 0, (
        f"git show ADP handler.py failed: {proc.stderr.decode()[:200]}"
    )
    src = proc.stdout.decode("utf-8", errors="replace")
    tree = ast.parse(src)
    # Find _derive_thread_id FunctionDef
    adp_func_node = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_derive_thread_id":
            adp_func_node = node
            break
    assert adp_func_node is not None, "could not find _derive_thread_id in ADP handler.py"
    # Build a minimal module and exec it
    adp_module_ast = ast.Module(body=[adp_func_node], type_ignores=[])
    ast.fix_missing_locations(adp_module_ast)
    import hashlib as _hashlib  # noqa: PLC0415
    adp_ns: dict = {"hashlib": _hashlib, "__name__": "__adp_pin__"}
    exec(  # noqa: S102
        compile(adp_module_ast, "<adp_dtw_pin>", "exec"),
        adp_ns,
    )
    adp_fn = adp_ns["_derive_thread_id"]
    # Load the script module
    spec = importlib.util.spec_from_file_location(
        f"lv_maker_t35_{uuid.uuid4().hex[:6]}", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    # Compare outputs on a test vector
    _pat = "PAT-FWTEST-001"
    _ifid = "00000000-0000-0000-0000-000000000042"
    adp_out = adp_fn(_pat, _ifid)
    script_out = mod._derive_thread_id(_pat, _ifid)
    assert script_out == adp_out, (
        f"_derive_thread_id output mismatch:\n  script={script_out!r}\n  ADP={adp_out!r}"
    )


def test_pin_plants_against_adp() -> None:
    """PLANTS in the live-verify script must match ADP 895f7b61 plants.py PATTERNS."""
    import subprocess as _sp  # noqa: PLC0415
    import ast  # noqa: PLC0415
    proc = _sp.run(
        ["git", "-C", _ADP_ROOT, "show", f"{_ADP_COMMIT}:{_ADP_PLANTS_PATH}"],
        capture_output=True, timeout=30, check=False,
    )
    assert proc.returncode == 0, (
        f"git show ADP plants.py failed: {proc.stderr.decode()[:200]}")
    src = proc.stdout.decode("utf-8", errors="replace")
    tree = ast.parse(src)
    # Extract PATTERNS: list of dicts — we only need code_family, cause_dimension, cause_value
    # which are all ast.literal_eval-compatible.  Other fields (start_date = dt.date(...),
    # rate float, planted_seed int, future_dated bool, etc.) are ignored.
    adp_entries: list[dict] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        for t in targets:
            if not (isinstance(t, ast.Name) and t.id == "PATTERNS"):
                continue
            val = node.value
            if not isinstance(val, ast.List):
                continue
            for elt in val.elts:
                if not isinstance(elt, ast.Dict):
                    continue
                entry: dict = {}
                for k_node, v_node in zip(elt.keys, elt.values):
                    if not isinstance(k_node, ast.Constant):
                        continue
                    key = k_node.value
                    if key in ("code_family", "cause_dimension"):
                        if isinstance(v_node, ast.Constant):
                            entry[key] = v_node.value
                    elif key == "cause_value":
                        try:
                            entry[key] = ast.literal_eval(v_node)
                        except Exception:  # noqa: BLE001
                            pass
                if "code_family" in entry and "cause_dimension" in entry and "cause_value" in entry:
                    adp_entries.append(entry)
    assert adp_entries, "could not extract PATTERNS from ADP plants.py"
    # Load our PLANTS
    spec = importlib.util.spec_from_file_location(
        f"lv_maker_plants_{uuid.uuid4().hex[:6]}", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    our_plants = mod.PLANTS

    def _key(p: dict) -> tuple:
        cv = p.get("cause_value", "")
        if isinstance(cv, list):
            cv = tuple(sorted(cv))
        return (p.get("code_family", ""), p.get("cause_dimension", ""), cv)

    adp_set = {_key(p) for p in adp_entries}
    our_set = {_key(p) for p in our_plants}
    assert our_set == adp_set, (
        f"PLANTS vs ADP PATTERNS mismatch.\n"
        f"  in script not in ADP: {our_set - adp_set}\n"
        f"  in ADP not in script: {adp_set - our_set}"
    )


# ---------------------------------------------------------------------------
# C2: fleet id inside a string list element → FAIL (strings in lists scanned)
# ---------------------------------------------------------------------------

def test_fleet_id_in_string_list_fails() -> None:
    """campaign.targetPlants=['flt-abc-1'] → scope scan FAIL (C2: strings in lists not checked)."""
    with CmsWorld(run_scheduled=False) as w:
        _setup_world(w)
        _write_pop_caches(w, campaigns_list=[{
            "campaignId": "camp-c2-001",
            "campaignName": "C2 probe",
            "status": "generated",
            "releaseVersion": "1.0",
            "targetPlants": ["flt-abc-1"],
            "targetModels": [],
            "targetModelYearMin": 2022,
            "targetModelYearMax": 2024,
            "targetComponentLots": [],
            "overTheAir": True,
            "leadTimeWeeks": 4,
            "affectedVinsCount": 50,
            "threadId": "thread-c2",
            "issuedAt": None,
            "createdAt": "2026-01-01T00:00:00Z",
        }])
        mod = _load_mod(w)
        rc, text = _run_main(mod, part="scope", allow_writes=True)
    rows = _rows(text)
    assert any("scan" in k.lower() and rows[k] == "FAIL" for k in rows), (
        f"expected scope scan FAIL for fleet id in targetPlants list; rows={rows}\n{text}"
    )
    assert rc == 1, f"expected exit 1, got {rc}"


# ---------------------------------------------------------------------------
# W2a: unlisted non-forbidden key at item top level → FAIL
# ---------------------------------------------------------------------------

def test_unlisted_key_fails() -> None:
    """Pattern item with unlisted key 'affectedVins' (not in FORBIDDEN_TOKENS) → FAIL (W2)."""
    with CmsWorld(run_scheduled=False) as w:
        _setup_world(w)
        fi_mod = w.cms_fi._mod
        fi_maker = fi_mod.maker
        _orig_proj = fi_maker.project_pattern_item

        def _extra_top(row: Any) -> Any:
            result = _orig_proj(row)
            result["affectedVins"] = ["VIN1"]   # not forbidden by name; unlisted
            return result

        fi_maker.project_pattern_item = _extra_top
        mod = _load_mod(w)
        rc, text = _run_main(mod, part="scope", allow_writes=True)
    rows = _rows(text)
    assert any(
        "scan" in k.lower() and rows[k] == "FAIL"
        for k in rows
    ), (f"expected scope scan FAIL for affectedVins at item top level; "
        f"rows={rows}\n{text}")
    assert "affectedVins" in text, "expected key name 'affectedVins' in scan FAIL output"
    assert rc == 1, f"expected exit 1, got {rc}"


# ---------------------------------------------------------------------------
# W2b: unlisted key at nested position (topCause dict) → FAIL once position allowlists exist
# ---------------------------------------------------------------------------

def test_unlisted_nested_key_fails() -> None:
    """Extra key inside topCause dict → FAIL once _TOP_CAUSE_KEYS enforced at that depth."""
    with CmsWorld(run_scheduled=False) as w:
        _setup_world(w)
        fi_mod = w.cms_fi._mod
        fi_maker = fi_mod.maker
        _orig_proj = fi_maker.project_pattern_item

        def _nested_extra(row: Any) -> Any:
            result = _orig_proj(row)
            if isinstance(result.get("topCause"), dict):
                result["topCause"]["extraNestedField"] = "x"
            return result

        fi_maker.project_pattern_item = _nested_extra
        mod = _load_mod(w)
        rc, text = _run_main(mod, part="scope", allow_writes=True)
    rows = _rows(text)
    assert any(
        "scan" in k.lower() and rows[k] == "FAIL"
        for k in rows
    ), (f"expected scope scan FAIL for nested topCause key; "
        f"rows={rows}\n{text}")
    assert "extraNestedField" in text, "expected key name 'extraNestedField' in scan FAIL output"
    assert rc == 1, f"expected exit 1, got {rc}"

# ---------------------------------------------------------------------------
# W9: perf requires 200 with data on every timed request
# ---------------------------------------------------------------------------

def test_perf_non_200_fails() -> None:
    """perf: handle_patterns returns 503 → non-200 response check FAILs, rc 1."""
    with CmsWorld(run_scheduled=False) as w:
        _setup_world(w)
        fi_mod = w.cms_fi._mod
        fi_maker = fi_mod.maker

        def _return_503(qs: Any) -> Any:
            return fi_maker._resp(503, {"error": "service unavailable"})

        fi_maker.handle_patterns = _return_503
        mod = _load_mod(w)
        rc, text = _run_main(mod, part="perf", allow_writes=True)
    rows = _rows(text)
    assert any(
        "perf" in k.lower() and "pattern" in k.lower() and rows[k] == "FAIL"
        for k in rows
    ), f"expected perf patterns FAIL for non-200; rows={rows}\n{text}"
    assert rc == 1, f"expected exit 1, got {rc}"

# ---------------------------------------------------------------------------
# W8: scope FAILs when patterns list is empty (no detail scan ran)
# ---------------------------------------------------------------------------

def test_scope_empty_patterns_fails() -> None:
    """Empty patterns cache (data=[]) → scope FAIL: detail GET never ran, no PASS row."""
    with CmsWorld(run_scheduled=False) as w:
        _setup_world(w)
        _write_pop_caches(w, pattern_list=[])
        mod = _load_mod(w)
        rc, text = _run_main(mod, part="scope", allow_writes=True)
    rows = _rows(text)
    assert any(
        "scope" in k.lower() and "pattern" in k.lower() and rows[k] == "FAIL"
        for k in rows
    ), f"expected scope FAIL for empty patterns; rows={rows}\n{text}"
    assert rc == 1, f"expected exit 1, got {rc}"

# ---------------------------------------------------------------------------
# W-1: write-window guard skips write parts in maintenance window
# ---------------------------------------------------------------------------

def test_w1_write_window_skips_at_0500z() -> None:
    """perf refused in 04:00-06:15 maintenance window (05:00Z): SKIP row, rc=2.

    Mutant caught: removing require_write_window call from run_perf.
    Seen failing before fix: yes.
    """
    import datetime as _dt
    with CmsWorld(run_scheduled=False) as w:
        _setup_world(w)
        mod = _load_mod(w)
        mod._now = lambda: _dt.datetime(2026, 10, 22, 5, 0, 0,
                                        tzinfo=_dt.timezone.utc)
        rc, text = _run_main(mod, part="perf", allow_writes=True)
    rows = _rows(text)
    assert any(rows.get(k) == "SKIP" for k in rows), (
        f"expected a SKIP row (maintenance window 05:00Z); rows={rows}\n{text}"
    )
    assert rc == 2, f"expected rc=2, got {rc}\n{text}"
    assert "04:00" in text or "maintenance" in text.lower() or "window" in text.lower(), (
        f"SKIP reason should mention window; text:\n{text}"
    )

# ---------------------------------------------------------------------------
# W-2: cognito-idp client built with username prefix and pool-id guard
# ---------------------------------------------------------------------------

def test_w2_cognito_foreign_username_refused() -> None:
    """Cognito Admin* call on a foreign username raises GuardViolation.

    Drives main() --part perf with a spy on _client; asserts the cognito-idp
    client is built with cognito_username_prefix='livetest-<run_id>-'.
    Mutant caught: removing cognito_username_prefix from _client() call.
    Seen failing before fix: yes.
    """
    import pytest  # noqa: PLC0415
    from scripts.fi_cms_live.harness import GuardViolation, _client as _h_client  # noqa: PLC0415
    with CmsWorld(run_scheduled=False) as w:
        _setup_world(w)
        mod = _load_mod(w)
        captured: list[dict] = []
        _orig_client = mod._client

        def _spy_client(svc: str, region: str, *, allowed_ops: frozenset, **kw: Any) -> Any:
            if svc == "cognito-idp":
                captured.append({
                    "cognito_username_prefix": kw.get("cognito_username_prefix", ""),
                    "cognito_pool_id": kw.get("cognito_pool_id", ""),
                })
            return _orig_client(svc, region, allowed_ops=allowed_ops, **kw)

        mod._client = _spy_client
        _run_main(mod, part="perf", allow_writes=True)
    assert captured, "no cognito-idp client was built"
    # In the maker, the first idp build has no prefix (pre-pool-discovery);
    # the write-part re-build (after CMS stack discovery) has the prefix.
    prefixed = [c for c in captured if c["cognito_username_prefix"].startswith("livetest-")]
    assert prefixed, (
        f"expected at least one cognito-idp build with 'livetest-' prefix; "
        f"got {[c['cognito_username_prefix'] for c in captured]!r}"
    )
    prefix = prefixed[0]["cognito_username_prefix"]
    pool = prefixed[0]["cognito_pool_id"]
    assert pool, "cognito_pool_id must be non-empty"
    guarded = _h_client("cognito-idp", "us-west-2",
                        allowed_ops=frozenset({"cognito-idp.AdminDeleteUser"}),
                        cognito_username_prefix=prefix,
                        cognito_pool_id=pool)
    with pytest.raises(GuardViolation):
        guarded.admin_delete_user(UserPoolId=pool, Username="real-user@example.com")

# ---------------------------------------------------------------------------
# W-3: cleanup calls delete_manifest_users before residue check
# ---------------------------------------------------------------------------

def test_w3_cleanup_deletes_manifest_users() -> None:
    """cleanup removes a manifest-recorded user that delete failed for earlier.

    Seen failing before fix: yes.
    """
    with CmsWorld(run_scheduled=False) as w:
        _setup_world(w)
        _write_pop_caches(w)
        mod = _load_mod(w)
        import json as _json  # noqa: PLC0415
        import tempfile  # noqa: PLC0415
        mdir = tempfile.mkdtemp(prefix="fi-cms-m1-w3-test-")
        manifest_path = (
            __import__("pathlib").Path(mdir) / f"{_RUN_ID}.json"
        )
        username = f"livetest-{_RUN_ID}-inv-eng@example.invalid"
        manifest_path.write_text(_json.dumps({
            "run_id": _RUN_ID, "stage": "staging",
            "items": {
                "stage": "staging",
                "user/inv-eng": {"username": username, "status": "created"},
            },
        }))
        idp_internal = w.internal("cognito-idp")
        idp_internal.admin_create_user(UserPoolId=w.pool.pool_id, Username=username,
                                       TemporaryPassword="Tmp!123456")
        # Count before: list_users returns sub UUIDs, not email — compare counts
        count_before_w3 = len(idp_internal.list_users(UserPoolId=w.pool.pool_id)["Users"])
        import os as _os  # noqa: PLC0415
        env_backup = _os.environ.copy()
        try:
            _os.environ["FI_CMS_LIVE_MANIFEST_DIR"] = mdir
            _os.environ["LIVE_TESTS"] = "1"
            import sys as _sys  # noqa: PLC0415
            old_argv = _sys.argv
            _sys.argv = [str(_SCRIPT), "--stage", "staging", "--run-id", _RUN_ID,
                         "--part", "cleanup", "--allow-writes"]
            import io as _io  # noqa: PLC0415
            import contextlib as _ctx  # noqa: PLC0415
            buf = _io.StringIO()
            try:
                with _ctx.redirect_stdout(buf), _ctx.redirect_stderr(buf):
                    rc_w3 = mod.main()
            except SystemExit as exc:
                rc_w3 = exc.code if isinstance(exc.code, int) else 1
            finally:
                _sys.argv = old_argv
        finally:
            _os.environ.clear()
            _os.environ.update(env_backup)
        text_w3 = buf.getvalue()
        count_after_w3 = len(idp_internal.list_users(UserPoolId=w.pool.pool_id)["Users"])
    assert count_after_w3 < count_before_w3, (
        f"cleanup should have deleted the manifest user; "
        f"before={count_before_w3} after={count_after_w3}\n{text_w3}"
    )
    assert rc_w3 != 1, f"cleanup should not FAIL; rc={rc_w3}\n{text_w3}"

# ---------------------------------------------------------------------------
# A-7: position allowlists — one test per new position family
# ---------------------------------------------------------------------------

# ── A-7 helper: load module without world (for direct _scan_any unit tests) ──

def _import_bare() -> Any:
    """Load the maker script without a CmsWorld (for _scan_any unit tests)."""
    spec = importlib.util.spec_from_file_location(
        f"lv_fi_cms_maker_bare_{uuid.uuid4().hex[:8]}", _SCRIPT
    )
    m = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(m)  # type: ignore[union-attr]
    return m


def _scan(mod: Any, obj: Any, path: str, top: Any = None):
    """Call mod._scan_any and return Results; captures stdout to avoid noise."""
    from scripts.fi_cms_live.harness import Results
    res = Results()
    buf = io.StringIO()
    with redirect_stdout(buf), redirect_stderr(buf):
        mod._scan_any(obj, path, res, top_allowlist=top)
    return res


# ── Family 1: facets (patterns list wrapper) ──────────────────────────────

def test_a7_facets_unlisted_key_fails() -> None:
    """Extra key inside facets dict → _scan_any FAIL (A-7 family: facets)."""
    mod = _import_bare()
    facets = {"models": [], "modelYears": [], "plants": [], "supplierIds": [],
              "unknownFacetKey": "x"}  # unlisted, non-forbidden
    res = _scan(mod, facets, "patterns facets", top=mod._FACETS_KEYS)
    assert res.failed, f"expected FAIL for unknownFacetKey; rows={res.rows}"
    assert any("unknownFacetKey" in r[2] for r in res.failed), f"key not in detail: {res.failed}"


# ── Family 2: kindCounts (rollup item) ────────────────────────────────────

def test_a7_kind_counts_unlisted_kind_fails() -> None:
    """Extra kind key inside kindCounts → _scan_any FAIL (A-7 family: kindCounts)."""
    mod = _import_bare()
    item = {"cohortValue": "Azimuth",
            "kindCounts": {"diagnostics": {"p0": 5}, "unknownKind": {"p0": 1}}}
    res = _scan(mod, item, "health-rollup", top=mod._ROLLUP_ITEM_KEYS)
    assert res.failed, f"expected FAIL for unknownKind; rows={res.rows}"
    assert any("unknownKind" in r[2] for r in res.failed), f"key not in detail: {res.failed}"


# ── Family 2b: per-kind severity dicts (inside kindCounts) ───────────────

def test_a7_kind_severity_unlisted_key_fails() -> None:
    """Extra severity key inside a per-kind dict → _scan_any FAIL (A-7 family: per-kind)."""
    mod = _import_bare()
    item = {"cohortValue": "Azimuth",
            "kindCounts": {"diagnostics": {"p0": 5, "unknownSeverity": 1}}}
    res = _scan(mod, item, "health-rollup", top=mod._ROLLUP_ITEM_KEYS)
    assert res.failed, f"expected FAIL for unknownSeverity; rows={res.rows}"
    assert any("unknownSeverity" in r[2] for r in res.failed), f"key not in detail: {res.failed}"


# ── Family 3: pattern detail's `investigation` inline dict ────────────────

def test_a7_investigation_inline_unlisted_key_fails() -> None:
    """Extra key inside pattern detail's `investigation` → _scan_any FAIL (A-7: investigation-inline)."""
    mod = _import_bare()
    now = "2024-01-01T00:00:00Z"
    pattern_detail = {
        "patternId": "pat-001", "codeFamily": "P0A0",
        "topCause": {"cohortDimension": "model", "cohortValue": "Azimuth",
                     "candidateRank": 1, "rateRatio": 3.5, "pValue": 0.001},
        "candidateCount": 1, "vehiclesAffected": 50, "vehiclesInService": 1000,
        "rate": 0.05, "firstDetectedMonth": "2026-01", "trend": "rising",
        "method": "cohort", "computedAt": now, "parametersVersion": "1.0",
        "candidates": [], "affectedVinsSample": [],
        "investigation": {
            "investigationId": "00000000-0000-0000-0000-000000000001",
            "status": "running", "requestedAt": now,
            "unknownInvField": "x",  # unlisted, non-forbidden
        },
        "investigationLookup": "ok",
    }
    res = _scan(mod, pattern_detail, "patterns/pat-001", top=mod._PATTERNS_ITEM_KEYS)
    assert res.failed, f"expected FAIL for unknownInvField; rows={res.rows}"
    assert any("unknownInvField" in r[2] for r in res.failed), f"key not in detail: {res.failed}"


# ── Family 4: investigation body ─────────────────────────────────────────

def test_a7_investigation_body_unlisted_key_fails() -> None:
    """Extra key in investigation body → _scan_any FAIL (A-7: investigation body)."""
    mod = _import_bare()
    body = {
        "investigationId": "00000000-0000-0000-0000-000000000001",
        "patternId": "pat-001", "status": "completed",
        "requestedAt": "2024-01-01T00:00:00Z",
        "startedAt": None, "completedAt": None,
        "findingId": None, "failureReason": None,
        "result": None, "resultLookup": None,
        "unknownBodyField": "x",  # unlisted, non-forbidden
    }
    res = _scan(mod, body, "investigations/test", top=mod._INVESTIGATION_BODY_KEYS)
    assert res.failed, f"expected FAIL for unknownBodyField; rows={res.rows}"
    assert any("unknownBodyField" in r[2] for r in res.failed), f"key not in detail: {res.failed}"


# ── Family 5: result (nested in investigation body) ───────────────────────

def test_a7_result_unlisted_key_fails() -> None:
    """Extra key in investigation result → _scan_any FAIL (A-7: result)."""
    mod = _import_bare()
    result_body = {
        "findingId": "FIND-001", "headline": "test", "severity": "p0",
        "confidence": "high", "computedAt": "2024-01-01T00:00:00Z", "expiresAt": None,
        "diagnosisSource": "model",
        "rootCause": None, "failureMode": None,
        "candidates": [], "options": [], "affectedVins": None, "controls": None,
        "recommendedOptionId": None,
        "unknownResultField": "x",  # unlisted, non-forbidden
    }
    res = _scan(mod, result_body, "investigations/test result", top=mod._RESULT_KEYS)
    assert res.failed, f"expected FAIL for unknownResultField; rows={res.rows}"
    assert any("unknownResultField" in r[2] for r in res.failed), f"key not in detail: {res.failed}"


# ── Family 5b: rootCause ──────────────────────────────────────────────────

def test_a7_root_cause_unlisted_key_fails() -> None:
    """Extra key inside rootCause → _scan_any FAIL (A-7: rootCause)."""
    mod = _import_bare()
    root = {
        "candidateId": "cand-001", "failureModeId": "FM-001",
        "reasoning": "test reasoning", "confidence": "high",
        "unknownRootField": "x",  # unlisted, non-forbidden
    }
    res = _scan(mod, root, "investigations/test rootCause", top=mod._ROOT_CAUSE_KEYS)
    assert res.failed, f"expected FAIL for unknownRootField; rows={res.rows}"
    assert any("unknownRootField" in r[2] for r in res.failed), f"key not in detail: {res.failed}"


# ── Family 5c: failureMode ────────────────────────────────────────────────

def test_a7_failure_mode_unlisted_key_fails() -> None:
    """Extra key inside failureMode → _scan_any FAIL (A-7: failureMode)."""
    mod = _import_bare()
    mode = {
        "failureModeId": "FM-001", "component": "OBC",
        "mechanism": "BMS undervolt", "symptomCodes": [],
        "unknownModeField": "x",  # unlisted, non-forbidden
    }
    res = _scan(mod, mode, "investigations/test failureMode", top=mod._FAILURE_MODE_KEYS)
    assert res.failed, f"expected FAIL for unknownModeField; rows={res.rows}"
    assert any("unknownModeField" in r[2] for r in res.failed), f"key not in detail: {res.failed}"


# ── Family 6: options[] items ─────────────────────────────────────────────

def test_a7_options_item_unlisted_key_fails() -> None:
    """Extra key in options[] item → _scan_any FAIL (A-7: options)."""
    mod = _import_bare()
    result_body = {
        "findingId": "FIND-001", "headline": "test", "severity": "p0",
        "confidence": "high", "computedAt": "2024-01-01T00:00:00Z", "expiresAt": None,
        "diagnosisSource": "model",
        "rootCause": None, "failureMode": None,
        "candidates": [],
        "options": [{
            "remedyOptionId": "OPT-001", "remedyType": "software_update",
            "partsCostPerVehicleUsd": None, "laborHoursPerVehicle": None,
            "totalVehicles": 100, "totalRemedyCostUsd": None, "leadTimeWeeks": None,
            "overTheAir": True, "recommended": True, "tradeoffs": None,
            "unknownOptionField": "x",  # unlisted, non-forbidden
        }],
        "affectedVins": None, "controls": None, "recommendedOptionId": "OPT-001",
    }
    res = _scan(mod, result_body, "investigations/test options", top=mod._RESULT_KEYS)
    assert res.failed, f"expected FAIL for unknownOptionField; rows={res.rows}"
    assert any("unknownOptionField" in r[2] for r in res.failed), f"key not in detail: {res.failed}"


# ── Family 7: thread record ───────────────────────────────────────────────

def test_a7_thread_record_unlisted_key_fails() -> None:
    """Extra key in thread record → _scan_any FAIL (A-7: thread record)."""
    mod = _import_bare()
    thread = {
        "threadId": "thr-001", "version": 1, "codeFamily": "P0A0",
        "causeDimension": "plant", "causeValue": "RNO",
        "detectionMethod": "statistical",
        "rateRatio": 3.5, "pValue": 0.001,
        "optionId": "OPT-001", "optionRemedyType": "software_update",
        "decidedAt": "2024-01-01T00:00:00Z",
        "affectedVinsCount": 1000, "affectedVinsSha256": "abc",
        "affectedVinsS3Key": "s3://...", "softwareCampaignId": "camp-001",
        "recallCampaignId": None, "status": "decided",
        "decisionOutcome": "approved", "campaignType": "software_update",
        "campaignStatus": "draft", "decidedByKind": "engineer", "notes": None,
        # THREAD_DETAIL_FIELDS
        "investigationFindingId": "FIND-001", "rootCauseCandidateId": "cand-001",
        "failureModeId": "FM-001", "agentVersion": "v1",
        "partsCostedPerVehicleUsd": None, "laborHoursPerVehicle": None,
        "leadTimeWeeks": None, "overTheAir": True, "patternId": "pat-001",
        "evidenceRefs": [], "diagnosisSource": "model",
        "unknownThreadField": "x",  # unlisted, non-forbidden
    }
    res = _scan(mod, thread, "threads/thr-001", top=mod._THREAD_RECORD_KEYS)
    assert res.failed, f"expected FAIL for unknownThreadField; rows={res.rows}"
    assert any("unknownThreadField" in r[2] for r in res.failed), f"key not in detail: {res.failed}"


# ── Family 8: action bodies (decision / issue) ────────────────────────────

def test_a7_action_body_unlisted_key_fails() -> None:
    """Extra key in action body → _scan_any FAIL (A-7: action bodies)."""
    mod = _import_bare()
    action = {
        "actionId": "act-001", "actionKind": "decision", "status": "executed",
        "decision": "approved", "failureReason": None,
        "unknownActionField": "x",  # unlisted, non-forbidden
    }
    res = _scan(mod, action, "investigations/test/decision", top=mod._ACTION_KEYS)
    assert res.failed, f"expected FAIL for unknownActionField; rows={res.rows}"
    assert any("unknownActionField" in r[2] for r in res.failed), f"key not in detail: {res.failed}"


# ── Family 8b: fail-closed on truly unknown nested position ──────────────

def test_a7_unknown_nested_position_fails_closed() -> None:
    """A nested dict at an unregistered key → 'no allowlist for key' FAIL (A-7: fail-closed)."""
    mod = _import_bare()
    from scripts.fi_cms_live.harness import Results
    obj = {
        "knownField": "value",
        "trulyUnknownNested": {"innerKey": "x"},  # no allowlist registered for this key
    }
    res = Results()
    buf = io.StringIO()
    with redirect_stdout(buf), redirect_stderr(buf):
        # Use a tight allowlist so the top-level check also allows the unknown key
        mod._scan_any(obj, "test/unknown", res,
                      top_allowlist=frozenset({"knownField", "trulyUnknownNested"}))
    fails = [r for r in res.rows if r[1] == "FAIL"]
    assert fails, f"expected 'no allowlist' FAIL for trulyUnknownNested; rows={res.rows}"
    assert any("no allowlist" in r[2] for r in fails), (
        f"expected 'no allowlist' in detail; fails={fails}")


def test_a7_unknown_list_of_dicts_fails_closed() -> None:
    """A list of dicts at an unregistered key → 'no allowlist' FAIL (battery c1 MA11)."""
    mod = _import_bare()
    from scripts.fi_cms_live.harness import Results
    obj = {"knownField": "value", "trulyUnknownList": [{"innerKey": "x"}]}
    res = Results()
    buf = io.StringIO()
    with redirect_stdout(buf), redirect_stderr(buf):
        mod._scan_any(obj, "test/unknown-list", res,
                      top_allowlist=frozenset({"knownField", "trulyUnknownList"}))
    fails = [r for r in res.rows if r[1] == "FAIL"]
    assert any("no allowlist for list-of-dicts" in r[2] for r in fails), (
        f"expected 'no allowlist for list-of-dicts' FAIL; rows={res.rows}")


# ---------------------------------------------------------------------------
# W3 (A-2): list-response wrappers are scanned; unknown wrapper keys fail closed
# ---------------------------------------------------------------------------

def test_wrapper_extra_key_fails() -> None:
    """Signals list response with extra wrapper key 'vins' → scope FAIL naming it (A-2 W3)."""
    with CmsWorld(run_scheduled=False) as w:
        _setup_world(w)
        fi_mod = w.cms_fi._mod
        fi_maker = fi_mod.maker
        _orig_handle_signals = fi_maker.handle_signals

        def _extra_wrapper(qs: Any) -> Any:
            result = _orig_handle_signals(qs)
            b = json.loads(result["body"])
            b["vins"] = ["VIN001"]
            result["body"] = json.dumps(b)
            return result

        fi_maker.handle_signals = _extra_wrapper
        mod = _load_mod(w)
        rc, text = _run_main(mod, part="scope", allow_writes=True)
    rows = _rows(text)
    assert any(
        "scan" in k.lower() and "wrapper" in k.lower() and rows[k] == "FAIL"
        for k in rows
    ), (f"expected scope scan FAIL naming wrapper key 'vins'; rows={rows}\n{text}")
    assert "vins" in text, f"expected key name 'vins' in FAIL output; got:\n{text}"
    assert rc == 1, f"expected exit 1, got {rc}"


# ---------------------------------------------------------------------------
# A-3 MC5: _ROUTE_ALLOWLISTS["patterns"] rejects detail keys in list items
# ---------------------------------------------------------------------------

def test_mc5_patterns_list_item_rejects_detail_key() -> None:
    """MC5: _ROUTE_ALLOWLISTS["patterns"] uses _PATTERNS_ITEM_KEYS (allows 'candidates').

    'candidates' is in _PATTERNS_ITEM_KEYS (detail level) but NOT in
    _PATTERNS_LIST_ITEM_KEYS (list level).  project_pattern_item is patched to
    include candidates:[] in list items.  With correct code the list-item scan
    uses _PATTERNS_LIST_ITEM_KEYS → FAIL naming 'candidates'.  With MC5 it uses
    _PATTERNS_ITEM_KEYS → no FAIL.
    """
    with CmsWorld(run_scheduled=False) as w:
        _setup_world(w)
        fi_mod = w.cms_fi._mod
        fi_maker = fi_mod.maker
        _orig_proj = fi_maker.project_pattern_item

        def _add_candidates(row: Any) -> Any:
            result = _orig_proj(row)
            result["candidates"] = []  # detail-level key, not in list allowlist
            return result

        fi_maker.project_pattern_item = _add_candidates
        mod = _load_mod(w)
        rc, text = _run_main(mod, part="scope", allow_writes=True)
    rows = _rows(text)
    assert any(
        "scan" in k.lower() and rows[k] == "FAIL"
        for k in rows
    ), (f"expected scope scan FAIL for 'candidates' in list item; "
        f"rows={rows}\n{text}")
    assert "candidates" in text, f"expected key name 'candidates' in FAIL output; got:\n{text}"
    assert rc == 1, f"expected exit 1, got {rc}"


# ---------------------------------------------------------------------------
# A-3 MC6: facets scan call is exercised via the live scope route
# ---------------------------------------------------------------------------

def test_mc6_facets_extra_key_fails_via_scope() -> None:
    """MC6: _scan_any call on facets_obj replaced with pass.

    handle_patterns is patched to return a facets dict with an extra key
    ('unknownFacetKey') not in _FACETS_KEYS.  With correct code
    _scan_any(facets_obj, ...) fires and FAILs naming that key.
    With MC6 (pass) the facets body goes unexamined and scope passes.
    """
    with CmsWorld(run_scheduled=False) as w:
        _setup_world(w)
        fi_mod = w.cms_fi._mod
        fi_maker = fi_mod.maker
        _orig_hp = fi_maker.handle_patterns

        def _facets_extra_key(qs: Any) -> Any:
            result = _orig_hp(qs)
            b = json.loads(result["body"])
            b["facets"] = {
                "models": [], "modelYears": [], "plants": [], "supplierIds": [],
                "unknownFacetKey": "x",  # not in _FACETS_KEYS
            }
            result["body"] = json.dumps(b)
            return result

        fi_maker.handle_patterns = _facets_extra_key
        mod = _load_mod(w)
        rc, text = _run_main(mod, part="scope", allow_writes=True)
    rows = _rows(text)
    assert any(
        "facets" in k.lower() and rows[k] == "FAIL"
        for k in rows
    ), (f"expected scope facets scan FAIL for 'unknownFacetKey'; "
        f"rows={rows}\n{text}")
    assert "unknownFacetKey" in text, (
        f"expected key name 'unknownFacetKey' in FAIL output; got:\n{text}")
    assert rc == 1, f"expected exit 1, got {rc}"


# ---------------------------------------------------------------------------
# A-3 MC7: perf check flags 200 with data=null
# ---------------------------------------------------------------------------

def test_mc7_perf_200_with_null_data_fails() -> None:
    """MC7: perf bad condition reduced to s != 200 (drops data-is-None check).

    handle_patterns returns 200 with data=null.  With correct code the check
    `b.get("data") is None` fires → FAIL.  With MC7 (only `s != 200`) the 200
    is accepted and no FAIL row is emitted.
    """
    with CmsWorld(run_scheduled=False) as w:
        _setup_world(w)
        fi_mod = w.cms_fi._mod
        fi_maker = fi_mod.maker

        def _return_200_null_data(qs: Any) -> Any:
            return fi_maker._resp(200, {"data": None, "computedAt": _now_iso()})

        fi_maker.handle_patterns = _return_200_null_data
        mod = _load_mod(w)
        rc, text = _run_main(mod, part="perf", allow_writes=True)
    rows = _rows(text)
    assert any(
        "perf" in k.lower() and "pattern" in k.lower() and rows[k] == "FAIL"
        for k in rows
    ), f"expected perf patterns FAIL for 200 with null data; rows={rows}\n{text}"
    assert rc == 1, f"expected exit 1, got {rc}"


# ---------------------------------------------------------------------------
# A-3 MC10: cognito prefix must include run_id (exact prefix)
# ---------------------------------------------------------------------------

def test_mc10_cognito_prefix_includes_run_id() -> None:
    """MC10: cognito_username_prefix stripped to 'livetest-' (run_id dropped).

    The write-part idp client must be built with the exact prefix
    'livetest-<run_id>-'.  The existing test_w2 asserts only startswith('livetest-'),
    which passes with MC10.  This test asserts the exact prefix and pool id.
    """
    with CmsWorld(run_scheduled=False) as w:
        _setup_world(w)
        mod = _load_mod(w)
        captured: list[dict] = []
        _orig_client = mod._client

        def _spy_client(svc: str, region: str, *, allowed_ops: frozenset, **kw: Any) -> Any:
            if svc == "cognito-idp":
                captured.append({
                    "cognito_username_prefix": kw.get("cognito_username_prefix", ""),
                    "cognito_pool_id": kw.get("cognito_pool_id", ""),
                })
            return _orig_client(svc, region, allowed_ops=allowed_ops, **kw)

        mod._client = _spy_client
        _run_main(mod, part="perf", allow_writes=True)

    # The write-part build must include the exact run_id prefix
    expected_prefix = f"livetest-{_RUN_ID}-"
    prefixed = [c for c in captured if c["cognito_username_prefix"] == expected_prefix]
    assert prefixed, (
        f"expected cognito-idp client built with exact prefix {expected_prefix!r}; "
        f"got prefixes={[c['cognito_username_prefix'] for c in captured]!r}"
    )
    pool = prefixed[0]["cognito_pool_id"]
    assert pool, f"cognito_pool_id must be non-empty; got {pool!r}"


# ---------------------------------------------------------------------------
# Security review c1 (M W2, architect): the ADP fault-patterns and corpus scans refuse
# to return a partial read, and the pattern pick refuses on that.
# ---------------------------------------------------------------------------

import pytest  # noqa: E402



def _fresh_mod() -> Any:
    spec = importlib.util.spec_from_file_location(
        f"lv_fi_cms_maker_secw2_{uuid.uuid4().hex[:6]}", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


class _EndlessScan:
    """A DynamoDB client whose Scan always says there's another page."""

    def __init__(self) -> None:
        self.calls = 0

    def scan(self, **kw: Any) -> dict:
        self.calls += 1
        if self.calls > 10_000:
            raise AssertionError("scan loop never stopped")
        return {"Items": [], "LastEvaluatedKey": {"pk": {"S": f"p{self.calls}"}}}


@pytest.mark.parametrize("fn", ["_scan_fp_table", "_scan_corpus_table"])
def test_sec_w2_adp_scan_refuses_partial_read(fn: str) -> None:
    """Deliberate break: drop the `else: raise` after the page loop -> returns partial data."""
    mod = _fresh_mod()
    ddb = _EndlessScan()
    try:
        getattr(mod, fn)(ddb, "staging")
    except Exception as exc:  # noqa: BLE001
        assert type(exc) is RuntimeError and "exceeded" in str(exc), f"{type(exc).__name__}: {exc}"
    else:
        raise AssertionError(f"{fn} returned after {ddb.calls} pages with more pending")
    assert ddb.calls == mod._MAX_PAGES, ddb.calls


def test_sec_w2_pattern_pick_refuses_when_a_scan_cannot_finish(tmp_path: Path) -> None:
    """The pick records a FAIL row and returns None (writes no pick) when an ADP scan
    can't finish. Deliberate break: drop the try/except -> the RuntimeError escapes."""
    mod = _fresh_mod()
    res = mod.Results()

    class _S3:
        def get_object(self, **kw: Any) -> dict:
            return {"Body": io.BytesIO(json.dumps({"data": [{"patternId": "a" * 16}]}).encode())}

    manifest = tmp_path / "m.json"
    manifest.write_text(json.dumps({"run_id": "r", "stage": "staging", "items": {}}))
    try:
        got = mod._pick_pattern(_S3(), None, _EndlessScan(), "b", "p/", "staging", res, manifest)
    except Exception as exc:  # noqa: BLE001
        raise AssertionError(f"pick raised {type(exc).__name__}: {exc}") from exc
    assert got is None
    assert any(lbl.startswith("pattern pick: ADP tables read in full") and st == "FAIL"
               for lbl, st, _detail in res.rows), res.rows
    assert "pattern_pick" not in json.loads(manifest.read_text())["items"]
