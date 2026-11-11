"""End-to-end tests for scripts/live-verify-fi-cms-fleet.py (F1: pre/perf/isolation).

Each test drives the real ``main()`` against ``CmsWorld`` (moto + FakeRestApi +
FakeUserPool + real CMS FI handler).

Structure
---------
- ``world()`` fixture: opens CmsWorld, seeds health data, registers the
  ``cms-staging-ui`` CloudFormation stack and alarm in moto, seeds the
  grounding row needed by the POST /groundings control.
- ``load_mod()`` helper: loads the script via importlib and wires
  ``mod.time = ScriptTime(w.clock)`` so perf timing uses the world clock.
- ``run_main()`` helper: sets ``sys.argv``, ``LIVE_TESTS=1``, calls
  ``mod.main()``, and returns ``(rc, output)``.

Tests
-----
T1  all-pass: all three parts exit 0.
T2  scope-bypass: patch the /faults handler to skip scope check → probe FAILs.
T3  control-fail: patch /pm/plans GET to return 200 for fleet-B scope → control passes
    but probe does not 403 → FAIL.
T4  p95-exceed: handler advances clock 4 s per perf request → p95 > 3 s → FAIL.
T5  users-deleted-success: after all-pass, no livetest users in pool.
T6  users-deleted-on-raise: raise SystemExit(1) mid-isolation → users still deleted.
T7  subprocess: run script from temp cwd with PYTHONPATH stripped → exit 2.
T8  guard-pre-no-cognito-write: pre-part client refuses AdminCreateUser.
T9  guard-perf-allows-cognito: perf-part client allows AdminCreateUser.
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import datetime
import tempfile
import uuid
from pathlib import Path
from typing import Any

import pytest

from scripts.tests.fi_cms_world.world import CmsWorld, GROUNDINGS_TABLE, PM_PLANS_TABLE
from scripts.tests.fi_rw_world import seed
from scripts.tests.fi_rw_world.clock import ScriptTime
from scripts.tests.fi_rw_world.world import REGION

_SCRIPT = Path(__file__).resolve().parents[1] / "live-verify-fi-cms-fleet.py"
_FLEET_A = "demo-rental-carshare"
_FLEET_B = "demo-lastmile-delivery"
_FLEET_A_VIN = seed.vin(1)
_FLEET_A_VID = seed.vehicle_id(1)
_FLEET_B_VIN = seed.vin(11)
_FLEET_B_VID = seed.vehicle_id(11)
_RUN_ID = "test-f1-fleet"

# ---------------------------------------------------------------------------
# World fixture helpers
# ---------------------------------------------------------------------------


def _register_cms_stack(world: CmsWorld) -> None:
    """Create the cms-staging-ui CFN stack in moto with APIEndpoint and UserPoolId."""
    cfn = world.internal("cloudformation")
    cfn.create_stack(
        StackName="cms-staging-ui",
        TemplateBody=json.dumps({
            "Resources": {"Placeholder": {"Type": "AWS::SNS::Topic"}},
            "Outputs": {
                "APIEndpoint": {"Value": world.cms_fi_url},
                "UserPoolId": {"Value": world.pool.pool_id},
            },
        }),
    )


def _register_alarm(world: CmsWorld) -> None:
    """Create the health-views-stale alarm in moto CloudWatch (state OK)."""
    import boto3  # noqa: PLC0415
    cw = boto3.client("cloudwatch", region_name=REGION)
    cw.put_metric_alarm(
        AlarmName="cms-staging-ui-fi-health-views-stale",
        AlarmDescription="test",
        MetricName="Latency",
        Namespace="AWS/ApiGateway",
        Period=300,
        EvaluationPeriods=1,
        Threshold=1.0,
        ComparisonOperator="GreaterThanThreshold",
        TreatMissingData="notBreaching",
        Statistic="Average",
    )
    # moto creates alarms in OK state by default; confirm
    resp = cw.describe_alarms(AlarmNames=["cms-staging-ui-fi-health-views-stale"])
    assert resp["MetricAlarms"][0]["StateValue"] == "OK"


def _recreate_groundings_with_gsi(world: CmsWorld) -> None:
    """Recreate the groundings table with the open-groundings-index GSI.

    World._create_cms_tables creates it without the GSI (world.py:83).
    open_groundings_for_fleet (groundings.py:266) queries IndexName='open-groundings-index';
    faults/summary calls it, so the table needs the GSI for isolation controls to work.
    """
    from scripts.tests.fi_cms_world.world import GROUNDINGS_TABLE  # noqa: PLC0415
    ddb = world.internal("dynamodb")
    try:
        ddb.delete_table(TableName=GROUNDINGS_TABLE)
    except Exception:  # noqa: BLE001
        pass
    ddb.create_table(
        TableName=GROUNDINGS_TABLE,
        KeySchema=[
            {"AttributeName": "vehicleId", "KeyType": "HASH"},
            {"AttributeName": "actionId",  "KeyType": "RANGE"},
        ],
        AttributeDefinitions=[
            {"AttributeName": "vehicleId",  "AttributeType": "S"},
            {"AttributeName": "actionId",   "AttributeType": "S"},
            {"AttributeName": "openFleetId","AttributeType": "S"},
            {"AttributeName": "groundedAt", "AttributeType": "S"},
        ],
        GlobalSecondaryIndexes=[{
            "IndexName": "open-groundings-index",
            "KeySchema": [
                {"AttributeName": "openFleetId", "KeyType": "HASH"},
                {"AttributeName": "groundedAt",  "KeyType": "RANGE"},
            ],
            "Projection": {"ProjectionType": "ALL"},
        }],
        BillingMode="PAY_PER_REQUEST",
    )


def _seed_grounding_control(world: CmsWorld) -> None:
    """Seed one RETURNED recall-form grounding on the fleet-A vehicle, shaped like
    staging's rows. Isolation's runtime pick (`_pick_grounding_collision_row`) finds it and
    reuses its actionId with a mismatched findingId, so both POST /groundings requests
    collide with it and nothing is written.
    """
    world.put(GROUNDINGS_TABLE, {
        "vehicleId": _FLEET_A_VID,
        "actionId": "act-iso-ctrl-seed",
        "vin": _FLEET_A_VIN,
        "fleetId": _FLEET_A,
        "campaignId": "MRD-2026-002",
        "findingId": "find-iso-ctrl",
        "approverSub": "world-seed",
        "groundedAt": "2026-01-01T00:00:00Z",
        "returnedAt": "2026-01-02T00:00:00Z",   # closed → RTS finds no open grounding
    })


def _setup_world(world: CmsWorld) -> None:
    """Seed all data the script needs for all three parts to pass."""
    # Recreate groundings table with open-groundings-index GSI
    _recreate_groundings_with_gsi(world)
    # B-3 N1 fix: no pre-seeded groundings rows; isolation discovers them at runtime.
    # Use _seed_grounding_control(world) explicitly in tests that need a returned row.
    # Seed a fault for fleet-A vin(1) so GET /vehicles/{vin}/faults returns 200
    fault_date = datetime.date(2026, 10, 14)
    world.adp_faults.add_fault(seed.vin(1), "P0217", fault_date, fault_date)
    # health cache (faults + pm) via refresh
    world.seed_recall_cache()
    world.refresh_health()
    # CFN stack + alarm
    _register_cms_stack(world)
    _register_alarm(world)

# ---------------------------------------------------------------------------
# Module loading and main() runner
# ---------------------------------------------------------------------------


def _load_mod(world: CmsWorld) -> Any:
    """Load the script as a fresh module; wire mod.time to the world clock."""
    spec = importlib.util.spec_from_file_location(
        f"lv_fi_cms_fleet_{uuid.uuid4().hex[:8]}", _SCRIPT
    )
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    mod.time = ScriptTime(world.clock)
    return mod


def _run_main(
    mod: Any,
    argv: list[str],
    run_id: str = _RUN_ID,
    manifest_dir: str | None = None,
) -> tuple[int, str]:
    """Call mod.main() with a tmp manifest dir; return (exit_code, stdout)."""
    import io  # noqa: PLC0415
    import contextlib  # noqa: PLC0415

    env_backup = os.environ.copy()
    old_argv = sys.argv
    out = io.StringIO()
    try:
        os.environ["LIVE_TESTS"] = "1"
        mdir = manifest_dir or tempfile.mkdtemp(prefix="fi-cms-f1-test-")
        os.environ["FI_CMS_LIVE_MANIFEST_DIR"] = mdir
        sys.argv = [str(_SCRIPT), "--stage", "staging", "--run-id", run_id] + argv
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            try:
                rc = mod.main()
            except SystemExit as exc:
                rc = exc.code if isinstance(exc.code, int) else 1
    finally:
        sys.argv = old_argv
        os.environ.clear()
        os.environ.update(env_backup)
    return rc, out.getvalue()


def _rows(text: str) -> dict[str, str]:
    """Parse [PASS/FAIL/SKIP/NOTE] rows from output text."""
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
# T1: all three parts pass
# ---------------------------------------------------------------------------


def test_all_pass() -> None:
    with CmsWorld(run_scheduled=False) as w:
        _setup_world(w)
        _seed_grounding_control(w)  # B-3: provide returned row so groundings pair runs (no SKIP)
        mod = _load_mod(w)
        rc, text = _run_main(mod, ["--allow-writes"])
    assert rc == 0, f"exit {rc}; output:\n{text}"
    rows = _rows(text)
    fails = [k for k, v in rows.items() if v == "FAIL"]
    assert not fails, f"unexpected FAILs: {fails}\n{text}"


# ---------------------------------------------------------------------------
# T2: scope bypass on /faults probe → row FAILs
# ---------------------------------------------------------------------------


def test_scope_bypass_fails() -> None:
    """Patch both scope layers on /faults; probe returns 200 → FAIL."""
    with CmsWorld(run_scheduled=False) as w:
        _setup_world(w)
        fi_mod = w.cms_fi._mod
        _orig_authz = fi_mod._authorize_or_403

        # Layer 1: bypass _authorize_or_403 for /faults GET
        def _no_scope(event, method, resource, params, qs, body):
            if resource == "/api/v1/fleet-intelligence/faults" and method == "GET":
                return None
            return _orig_authz(event, method, resource, params, qs, body)

        fi_mod._authorize_or_403 = _no_scope

        # Layer 2: replace handler to skip health_source scope check
        def _bypass_faults(qs, claims):
            return {"statusCode": 200, "headers": {}, "body": '{"data":[],"total":0}'}

        fi_mod._handle_faults_get = _bypass_faults

        mod = _load_mod(w)
        rc, text = _run_main(mod, ["--allow-writes", "--part", "isolation"])
    rows = _rows(text)
    assert any("GET /faults" in k and "probe" in k and rows[k] == "FAIL" for k in rows), (
        f"expected FAIL for GET /faults probe; rows={rows}\n{text}"
    )
    assert rc == 1, f"exit code should be 1 but got {rc}"

# ---------------------------------------------------------------------------
# T3: control FAIL (control returns wrong status) → FAIL, not PASS
# ---------------------------------------------------------------------------


def test_control_fail_produces_fail() -> None:
    """Make the /pm/plans control return 500 → FAIL row, not PASS."""
    with CmsWorld(run_scheduled=False) as w:
        _setup_world(w)
        fi_mod = w.cms_fi._mod
        _orig = fi_mod._handle_pm_plans

        def _bad_plans(method, qs, body, claims):
            result = _orig(method, qs, body, claims)
            # Force control (fleet-A GET) to 500 to simulate a broken control
            if method == "GET" and "demo-rental-carshare" in qs.get("fleetId", ""):
                return {"statusCode": 500, "headers": {}, "body": '{"error":"injected"}'}
            return result

        fi_mod._handle_pm_plans = _bad_plans

        mod = _load_mod(w)
        rc, text = _run_main(mod, ["--allow-writes", "--part", "isolation"])
    rows = _rows(text)
    # The GET /pm/plans control should FAIL (expected 200, got 500)
    assert any("GET /pm/plans" in k and rows[k] == "FAIL" for k in rows), (
        f"expected GET /pm/plans FAIL; rows={rows}\n{text}"
    )
    assert rc == 1, f"expected exit 1, got {rc}"


# ---------------------------------------------------------------------------
# T4: p95 > 3 s → FAIL
# ---------------------------------------------------------------------------


def test_perf_p95_too_slow() -> None:
    """Advance clock 4 s per request; p95 > 3.0 s → perf FAIL."""
    with CmsWorld(run_scheduled=False) as w:
        _setup_world(w)
        fi_mod = w.cms_fi._mod
        _orig = fi_mod._handle_faults_get

        def _slow_faults(qs, claims):
            w.clock.advance(4.0)   # 4 s per call → p95 >> 3 s
            return _orig(qs, claims)

        fi_mod._handle_faults_get = _slow_faults

        mod = _load_mod(w)
        rc, text = _run_main(mod, ["--allow-writes", "--part", "perf"])
    rows = _rows(text)
    assert any("p95" in k and rows[k] == "FAIL" for k in rows), (
        f"expected p95 FAIL; rows={rows}\n{text}"
    )
    assert rc == 1, f"expected exit 1, got {rc}"


# ---------------------------------------------------------------------------
# T5: temp users deleted on success
# ---------------------------------------------------------------------------


def test_temp_users_deleted_on_success() -> None:
    with CmsWorld(run_scheduled=False) as w:
        _setup_world(w)
        _seed_grounding_control(w)  # c2: staging-shaped returned row so isolation's pair runs (no SKIP)
        mod = _load_mod(w)
        rc, text = _run_main(mod, ["--allow-writes"])
    assert rc == 0, f"exit {rc}\n{text}"
    # No livetest users with this run-id should remain in the pool
    prefix = f"livetest-{_RUN_ID}-"
    remaining = [
        u for u in w.pool.users.values()
        if u.get("attrs", {}).get("email", "").startswith(prefix)
    ]
    assert not remaining, f"livetest users still in pool: {remaining}"


# ---------------------------------------------------------------------------
# T6: temp users deleted when main raises mid-isolation
# ---------------------------------------------------------------------------


def test_temp_users_deleted_on_raise() -> None:
    """Raise inside isolation → finally cleanup still runs, users gone."""
    with CmsWorld(run_scheduled=False) as w:
        _setup_world(w)
        fi_mod = w.cms_fi._mod
        _call_count = [0]
        _orig = fi_mod._handle_faults_get

        def _raise_on_second(qs, claims):
            _call_count[0] += 1
            if _call_count[0] >= 2:
                raise RuntimeError("injected test error")
            return _orig(qs, claims)

        fi_mod._handle_faults_get = _raise_on_second
        mod = _load_mod(w)
        # isolation calls /faults at least once; second call raises → main exits non-zero
        try:
            rc, text = _run_main(mod, ["--allow-writes", "--part", "isolation"])
        except Exception:
            pass
    # Users should still be gone (principal context manager deletes on any exit)
    prefix = f"livetest-{_RUN_ID}-"
    remaining = [
        u for u in w.pool.users.values()
        if u.get("attrs", {}).get("email", "").startswith(prefix)
    ]
    assert not remaining, f"livetest users not cleaned up on raise: {remaining}"

# ---------------------------------------------------------------------------
# T7: subprocess from temp cwd with PYTHONPATH stripped
# ---------------------------------------------------------------------------


def test_subprocess_no_pythonpath() -> None:
    """Run script from a temp cwd with PYTHONPATH stripped; lazy imports fire."""
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    env.pop("LIVE_TESTS", None)
    with tempfile.TemporaryDirectory() as td:
        result = subprocess.run(
            [sys.executable, str(_SCRIPT), "--stage", "staging", "--run-id", "subprocess-test"],
            cwd=td,
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
    # LIVE_TESTS not set → exit 2 (SKIP), not ImportError/AttributeError
    assert result.returncode == 2, (
        f"expected rc=2, got {result.returncode}\n"
        f"stdout: {result.stdout[:400]}\nstderr: {result.stderr[:400]}"
    )


# ---------------------------------------------------------------------------
# T14: RTS control skips and FAILs when fleet-A vehicle has open grounding
# ---------------------------------------------------------------------------


def test_isolation_rts_skips_with_open_grounding() -> None:
    """RTS control FAILs naming the vehicle when fleet-A vehicle has open grounding."""
    with CmsWorld(run_scheduled=False) as w:
        _setup_world(w)
        # Seed an open grounding (no returnedAt) for vehicle(1)
        w.put(GROUNDINGS_TABLE, {
            "vehicleId": seed.vehicle_id(1),
            "actionId": "act-rts-open-test",
            "vin": seed.vin(1),
            "fleetId": _FLEET_A,
            "openFleetId": _FLEET_A,
            "findingId": "find-rts-open",
            "approverSub": "test",
            "groundedAt": "2026-01-01T00:00:00Z",
        })
        mod = _load_mod(w)
        # Force discovery to return vehicle(1) as fleet-A so the RTS check fires
        # (discovery skips it due to the open grounding; override to test the RTS guard)
        _orig_disc = mod._discover_fleet_vins
        def _force_fleet_a(ddb: Any, stage: str) -> tuple[str, str, str, str]:
            _, _, b_vid, b_vin = _orig_disc(ddb, stage)
            return seed.vehicle_id(1), seed.vin(1), b_vid, b_vin
        mod._discover_fleet_vins = _force_fleet_a
        rc, text = _run_main(mod, ["--allow-writes", "--part", "isolation"])
    rows = _rows(text)
    ctrl_key = next(
        (k for k in rows if "rts" in k.lower() and "control" in k.lower()), None
    )
    assert ctrl_key, f"no RTS control row; rows={rows}"
    assert rows[ctrl_key] == "FAIL", f"expected FAIL, got {rows[ctrl_key]!r}"
    fail_line = next(
        (ln for ln in text.splitlines()
         if "rts" in ln.lower() and "[FAIL]" in ln and "control" in ln.lower()),
        "",
    )
    assert "open grounding" in fail_line or seed.vehicle_id(1) in fail_line, (
        f"FAIL must mention vehicle or 'open grounding': {fail_line!r}"
    )
    assert rc == 1, f"expected rc=1, got {rc}"


# ---------------------------------------------------------------------------
# T12: VINs discovered not hardcoded + T13: no PutItem to groundings in isolation
# ---------------------------------------------------------------------------


def test_vins_discovered_not_hardcoded() -> None:
    """FLEET_A_VIN must not be a module constant; _discover_fleet_vins must exist."""
    spec = importlib.util.spec_from_file_location("lv_disc_chk", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    assert not hasattr(mod, "FLEET_A_VIN"), "FLEET_A_VIN must not be a module constant"
    assert hasattr(mod, "_discover_fleet_vins"), "_discover_fleet_vins must exist"


def test_isolation_groundings_no_putitem() -> None:
    """B-3 scenario (a): no returned grounding rows → SKIP pair, no POST /groundings sent.

    With no seed, _pick_grounding_collision_row returns None → isolation skips the
    pair entirely.  Groundings table unchanged; rc 0.  Dropping the returnedAt filter
    (FC1-like) can't change this path (no row to pick regardless).
    """
    with CmsWorld(run_scheduled=False) as w:
        _setup_world(w)
        fi_mod = w.cms_fi._mod
        gnd_create_calls: list[tuple] = []
        _orig_gnd = fi_mod._handle_groundings_create

        def _spy_gnd(body: Any, claims: Any) -> Any:
            gnd_create_calls.append((body, claims))
            return _orig_gnd(body, claims)

        fi_mod._handle_groundings_create = _spy_gnd
        before = len(w.scan(GROUNDINGS_TABLE))
        mod = _load_mod(w)
        rc, text = _run_main(mod, ["--allow-writes", "--part", "isolation"])
        after = len(w.scan(GROUNDINGS_TABLE))
    assert after == before, (
        f"groundings table changed during isolation\n{text}"
    )
    assert gnd_create_calls == [], (
        f"POST /groundings handler called {len(gnd_create_calls)} times; expected 0\n{text}"
    )
    rows = _rows(text)
    skip_keys = [k for k, v in rows.items() if v == "SKIP" and "groundings" in k.lower()]
    assert skip_keys, (
        f"expected a SKIP row for groundings pair; rows={rows}\n{text}"
    )
    assert rc == 2, f"exit {rc} (expected 2 for SKIP)\n{text}"


# ---------------------------------------------------------------------------
# T11: perf notes /faults/summary's own status code
# ---------------------------------------------------------------------------


def test_perf_faults_summary_notes_own_status() -> None:
    """perf notes /faults/summary's own HTTP status, not /faults's last status."""
    with CmsWorld(run_scheduled=False) as w:
        _setup_world(w)
        fi_mod = w.cms_fi._mod

        def _err_summary(qs: Any, claims: Any) -> dict[str, Any]:
            return {"statusCode": 503, "headers": {}, "body": '{"error":"injected"}'}

        fi_mod._handle_faults_summary_get = _err_summary
        mod = _load_mod(w)
        _, text = _run_main(mod, ["--allow-writes", "--part", "perf"])
    for line in text.splitlines():
        if "/faults/summary status" in line and "[NOTE]" in line:
            val = line.split("/faults/summary status", 1)[-1].lstrip(":").strip()
            assert val == "503", f"expected '503', got {val!r}; patch not reflected"
            return
    pytest.fail("/faults/summary status note not found")


# ---------------------------------------------------------------------------
# T10: pre records FAULT_GROUNDING_ENABLED as 'true'/'false'/'unreadable'
# ---------------------------------------------------------------------------


def test_pre_fault_grounding_distinct() -> None:
    """pre notes FAULT_GROUNDING_ENABLED as 'true', 'false', or 'unreadable'."""
    with CmsWorld(run_scheduled=False) as w:
        _setup_world(w)
        mod = _load_mod(w)
        rc, text = _run_main(mod, ["--part", "pre"])
    assert rc == 0, f"exit {rc}\n{text}"
    for line in text.splitlines():
        if "FAULT_GROUNDING_ENABLED" in line and "[NOTE]" in line:
            val = line.split("FAULT_GROUNDING_ENABLED", 1)[-1].lstrip(":").strip()
            assert val in ("true", "false", "unreadable"), (
                f"expected 'true'/'false'/'unreadable', got {val!r}"
            )
            return
    pytest.fail("no FAULT_GROUNDING_ENABLED note in pre output")


# ---------------------------------------------------------------------------
# T8: guard — pre-part client refuses Cognito write ops
# ---------------------------------------------------------------------------


def test_guard_pre_refuses_cognito_write() -> None:
    """Pre-part's cloudformation client must not allow AdminCreateUser."""
    spec = importlib.util.spec_from_file_location("lv_fleet_guard_t8", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(mod)  # type: ignore[union-attr]

    from scripts.fi_cms_live.harness import GuardViolation, _client  # noqa: PLC0415

    # Build a pre-part client pointing at a dead endpoint
    cfn = _client(
        "cognito-idp", "us-east-1",
        allowed_ops=mod._PRE_OPS,
        endpoint_url="http://127.0.0.1:1",
    )
    with pytest.raises(GuardViolation):
        cfn.admin_create_user(UserPoolId="us-east-1_X", Username="test@example.invalid",
                              MessageAction="SUPPRESS")


# ---------------------------------------------------------------------------
# T9: guard — perf-part client allows Cognito write ops (reaches endpoint)
# ---------------------------------------------------------------------------


def test_guard_perf_allows_cognito_write() -> None:
    """Perf-part client allows AdminCreateUser (guard passes, connection refused OK)."""
    spec = importlib.util.spec_from_file_location("lv_fleet_guard_t9", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(mod)  # type: ignore[union-attr]

    from scripts.fi_cms_live.harness import _client  # noqa: PLC0415
    from botocore.exceptions import EndpointConnectionError  # noqa: PLC0415

    idp = _client(
        "cognito-idp", "us-east-1",
        allowed_ops=mod._PERF_OPS,
        endpoint_url="http://127.0.0.1:1",
        # security c1 W1: a write client needs the run's prefix and pool (setup only;
        # the assertion below is unchanged: perf's allowlist admits AdminCreateUser).
        cognito_username_prefix="livetest-t9-",
        cognito_pool_id="us-east-1_X",
    )
    # Guard passes → request hits dead endpoint → EndpointConnectionError (not GuardViolation)
    with pytest.raises(EndpointConnectionError):
        idp.admin_create_user(UserPoolId="us-east-1_X", Username="livetest-t9-test@example.invalid",
                              MessageAction="SUPPRESS")


# ---------------------------------------------------------------------------
# T12: C2 — perf fails on non-200 response
# ---------------------------------------------------------------------------


def test_perf_fails_on_non_200() -> None:
    """perf returns 503 on every /faults request → FAIL (non-200 must not count as latency)."""
    with CmsWorld(run_scheduled=False) as w:
        _setup_world(w)
        fi_mod = w.cms_fi._mod

        def _err_faults(qs: Any, claims: Any) -> dict[str, Any]:
            return {"statusCode": 503, "headers": {}, "body": '{"error":"injected"}'}

        fi_mod._handle_faults_get = _err_faults
        mod = _load_mod(w)
        rc, text = _run_main(mod, ["--allow-writes", "--part", "perf"])
    rows = _rows(text)
    assert any("perf" in k.lower() and rows[k] == "FAIL" for k in rows), (
        f"expected perf FAIL on non-200; rows={rows}\n{text}"
    )
    assert rc == 1, f"expected exit 1, got {rc}"


# ---------------------------------------------------------------------------
# T13: C2 — perf fails on unavailableReason
# ---------------------------------------------------------------------------


def test_perf_fails_on_unavailable_reason() -> None:
    """perf returns 200 with unavailableReason → FAIL."""
    with CmsWorld(run_scheduled=False) as w:
        _setup_world(w)
        fi_mod = w.cms_fi._mod

        def _unavail_faults(qs: Any, claims: Any) -> dict[str, Any]:
            return {"statusCode": 200, "headers": {},
                    "body": json.dumps({"unavailableReason": "view_loading"})}

        fi_mod._handle_faults_get = _unavail_faults
        mod = _load_mod(w)
        rc, text = _run_main(mod, ["--allow-writes", "--part", "perf"])
    rows = _rows(text)
    assert any("perf" in k.lower() and rows[k] == "FAIL" for k in rows), (
        f"expected perf FAIL on unavailableReason; rows={rows}\n{text}"
    )
    assert rc == 1, f"expected exit 1, got {rc}"


def test_perf_non_integer_total_fails_not_crashes() -> None:
    """review c2 S11 (architect): a 200 whose `total` isn't an integer is a bad body: perf
    FAILs with a row naming it and the run exits 1, instead of raising out of main().
    Deliberate break: the integer check -> `if False:` (int("x") raises ValueError)."""
    with CmsWorld(run_scheduled=False) as w:
        _setup_world(w)
        fi_mod = w.cms_fi._mod

        def _str_total(qs: Any, claims: Any) -> dict[str, Any]:
            return {"statusCode": 200, "headers": {},
                    "body": json.dumps({"data": [], "total": "x"})}

        fi_mod._handle_faults_get = _str_total
        mod = _load_mod(w)
        try:
            rc, text = _run_main(mod, ["--allow-writes", "--part", "perf"])
        except Exception as exc:  # noqa: BLE001
            raise AssertionError(f"perf raised instead of FAILing: {type(exc).__name__}: {exc}") from exc
    rows = _rows(text)
    assert any("perf" in k.lower() and rows[k] == "FAIL" for k in rows), (
        f"expected perf FAIL on a non-integer total; rows={rows}\n{text}"
    )
    assert "is not an integer" in text, text
    assert rc == 1, f"expected exit 1, got {rc}\n{text}"


# ---------------------------------------------------------------------------
# B-4 W6: groundings control refused AFTER scope (409, not pre-scope 400)
# ---------------------------------------------------------------------------


def test_groundings_control_refused_after_scope() -> None:
    """POST /groundings control must be refused AFTER the scope check (409, not 400 from vin)."""
    with CmsWorld(run_scheduled=False) as w:
        _setup_world(w)
        _seed_grounding_control(w)
        fi_mod = w.cms_fi._mod
        # Simulate a scope regression: fleet-A writes always refused
        fi_mod._auth.authorize_fleet_write = lambda claims, fleet_id: (False, "mutant")
        mod = _load_mod(w)
        rc, text = _run_main(mod, ["--allow-writes", "--part", "isolation"])
    rows = _rows(text)
    ctrl_key = next(
        (k for k in rows if "groundings" in k.lower() and "control" in k.lower()), None
    )
    assert ctrl_key, f"no POST /groundings control row in output; rows={rows}\n{text}"
    # Pre-scope control (vin="!invalid") → 400 before scope → PASS despite the patch
    # Post-scope control (existing row, mismatched findingId) → 403 (scope refused) → FAIL
    assert rows[ctrl_key] == "FAIL", (
        f"control must reach authorize_fleet_write; got {rows[ctrl_key]!r}\n{text}"
    )
    assert rc == 1, f"expected rc=1, got {rc}"


# ---------------------------------------------------------------------------
# B-4 W7: write probes safe on scope leak — no table write if scope regresses
# ---------------------------------------------------------------------------


def _seed_grounding_probe_b(world: CmsWorld) -> None:
    """Seed a returned fleet-B grounding so the groundings probe uses a mismatched findingId.

    The probe sends actionId=act-b-probe-seed + findingId=find-b-probe-X (different from
    seeded find-b-probe-seed).  On a scope leak the conditional put fails → 409, no write.
    The row has returnedAt so fleet_b_vid has NO open grounding (RTS probe stays safe).
    """
    world.put(GROUNDINGS_TABLE, {
        "vehicleId": _FLEET_B_VID,
        "actionId": "act-b-probe-seed",
        "vin": _FLEET_B_VIN,
        "fleetId": _FLEET_B,
        "findingId": "find-b-probe-seed",
        "approverSub": "world-seed",
        "groundedAt": "2026-01-01T00:00:00Z",
        "returnedAt": "2026-01-02T00:00:00Z",   # closed → no open grounding
    })


def test_isolation_probes_never_write_on_scope_leak() -> None:
    """With scope open, write probes must not change pm-plans or groundings tables.

    B-3 N1 fix: the probe sends the fleet-A collision row's (actionId) with a
    mismatched findingId; conditional put fails on the existing row → 409, no write.
    """
    with CmsWorld(run_scheduled=False) as w:
        _setup_world(w)
        _seed_grounding_control(w)   # B-3: provide a fleet-A returned row to discover
        fi_mod = w.cms_fi._mod
        # Open the scope check for both write routes
        _orig_authz = fi_mod._authorize_or_403

        def _open_scope(event, method, resource, params, qs, body):
            return None  # bypass all scope gates

        fi_mod._authorize_or_403 = _open_scope
        fi_mod._auth.authorize_fleet_write = lambda claims, fleet_id: (True, None)

        plans_before = len(w.scan(PM_PLANS_TABLE))
        groundings_before = len(w.scan(GROUNDINGS_TABLE))
        mod = _load_mod(w)
        rc, text = _run_main(mod, ["--allow-writes", "--part", "isolation"])
        plans_after = len(w.scan(PM_PLANS_TABLE))
        groundings_after = len(w.scan(GROUNDINGS_TABLE))

    assert plans_after == plans_before, (
        f"pm-plans table grew by {plans_after - plans_before} on scope leak\n{text}"
    )
    assert groundings_after == groundings_before, (
        f"groundings table grew by {groundings_after - groundings_before} on scope leak\n{text}"
    )


# ---------------------------------------------------------------------------
# B-4 W5: per-part clients — pre's cognito-idp client has no write ops
# ---------------------------------------------------------------------------

_COGNITO_WRITE_OPS = frozenset({
    "cognito-idp.AdminCreateUser",
    "cognito-idp.AdminSetUserPassword",
    "cognito-idp.AdminDeleteUser",
})


def test_guard_main_pre_no_cognito_write_allowed() -> None:
    """main()'s cognito-idp client for --part pre must not include write ops."""
    with CmsWorld(run_scheduled=False) as w:
        _setup_world(w)
        mod = _load_mod(w)
        calls: list[tuple[str, frozenset]] = []
        _orig_client = mod._client

        def _spy(service: str, region: str,
                 allowed_ops: "frozenset[str]" = frozenset(), **kw: Any) -> Any:
            calls.append((service, frozenset(allowed_ops)))
            return _orig_client(service, region, allowed_ops=allowed_ops, **kw)

        mod._client = _spy
        rc, text = _run_main(mod, ["--part", "pre"])

    cognito_calls = [(svc, ops) for svc, ops in calls if svc == "cognito-idp"]
    assert cognito_calls, f"no cognito-idp client was built; calls={calls}"
    for _, ops in cognito_calls:
        found_writes = ops & _COGNITO_WRITE_OPS
        assert not found_writes, (
            f"pre's cognito-idp client allows write ops: {found_writes}"
        )
    assert rc == 0, f"expected rc=0, got {rc}\n{text}"


# ---------------------------------------------------------------------------
# Review g78 B W7 (third probe): the isolation RTS probe never targets a grounded
# fleet-B vehicle, so a scope leak can't return a real grounded vehicle to service.
# Mutant caught: `_discover_fleet_vins` taking the first fleet-B vehicle again.
# ---------------------------------------------------------------------------

def test_isolation_rts_probe_never_returns_a_grounded_fleet_b_vehicle() -> None:
    """Every fleet-B vehicle has an open grounding and the scope check is open: the
    script must refuse to pick an RTS probe target rather than return one to service."""
    with CmsWorld(run_scheduled=False) as w:
        _setup_world(w)
        fleet_b_vids = sorted({
            v["vehicleId"] for v in w.scan("cms-staging-storage-vehicles")
            if v.get("fleetId") == _FLEET_B and v.get("vehicleId")
        })
        assert len(fleet_b_vids) >= 2, f"fixture: fleet B has {fleet_b_vids}"
        for i, vid in enumerate(fleet_b_vids):
            w.put(GROUNDINGS_TABLE, {
                "vehicleId": vid, "actionId": f"act-b-open-{i}", "fleetId": _FLEET_B,
                "openFleetId": _FLEET_B, "findingId": f"find-b-open-{i}",
                "approverSub": "world-seed", "groundedAt": "2026-10-01T00:00:00Z",
                "reason": "fault",
            })
        fi_mod = w.cms_fi._mod
        fi_mod._authorize_or_403 = lambda event, method, resource, params, qs, body: None
        fi_mod._auth.authorize_fleet_write = lambda claims, fleet_id: (True, None)
        # Record every vehicle a return-to-service reaches (the real handler still runs).
        rts_targets: list[str] = []
        _orig_rts = fi_mod._handle_groundings_rts

        def _spy_rts(vehicle_id: str, body: Any, claims: Any) -> Any:
            rts_targets.append(vehicle_id)
            return _orig_rts(vehicle_id, body, claims)

        fi_mod._handle_groundings_rts = _spy_rts
        mod = _load_mod(w)
        rc, text = _run_main(mod, ["--allow-writes", "--part", "isolation"])
    grounded_hit = sorted(set(rts_targets) & set(fleet_b_vids))
    assert grounded_hit == [], (
        f"the RTS probe reached grounded fleet-B vehicle(s) {grounded_hit}\n{text}")
    assert rc != 0, f"isolation must not pass without a safe fleet-B target\n{text}"

# ---------------------------------------------------------------------------
# W-1: write-window guard skips write parts in maintenance window
# ---------------------------------------------------------------------------

def test_w1_write_window_skips_at_0500z() -> None:
    """perf refused in 04:00-06:15 maintenance window (05:00Z): SKIP row, rc=2.

    Mutant caught: removing require_write_window call from run_perf.
    Seen failing before fix: yes.
    """
    with CmsWorld(run_scheduled=False) as w:
        _setup_world(w)
        mod = _load_mod(w)
        mod._now = lambda: datetime.datetime(2026, 10, 22, 5, 0, 0,
                                             tzinfo=datetime.timezone.utc)
        rc, text = _run_main(mod, ["--allow-writes", "--part", "perf"])
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

    Drives main() --part perf with a patched idp factory that captures
    the cognito_username_prefix/pool_id kwargs; then calls AdminDeleteUser
    with a non-livetest username and asserts GuardViolation.
    Mutant caught: removing cognito_username_prefix from _client() call.
    Seen failing before fix: yes.
    """
    from scripts.fi_cms_live.harness import GuardViolation, _client  # noqa: PLC0415
    import pytest  # noqa: PLC0415
    with CmsWorld(run_scheduled=False) as w:
        _setup_world(w)
        mod = _load_mod(w)
        # Capture the cognito prefix/pool used when the idp client is built
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
        _run_main(mod, ["--allow-writes", "--part", "perf"])
    # The prefix must start with 'livetest-<run_id>-'
    assert captured, "no cognito-idp client was built"
    prefix = captured[0]["cognito_username_prefix"]
    pool = captured[0]["cognito_pool_id"]
    assert prefix.startswith("livetest-"), (
        f"expected cognito_username_prefix starting 'livetest-'; got {prefix!r}"
    )
    assert pool, "cognito_pool_id must be non-empty"
    # A real client built with this prefix must refuse a foreign username
    import boto3  # noqa: PLC0415
    guarded = _client("cognito-idp", "us-west-2",
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

    Seeds a 'pending' manifest entry for user/gnd-op and a matching user in
    the pool; cleanup must call AdminDeleteUser and leave the pool empty.
    Mutant caught: removing delete_manifest_users call from run_cleanup.
    Seen failing before fix: yes.
    """
    with CmsWorld(run_scheduled=False) as w:
        _setup_world(w)
        mod = _load_mod(w)
        # Pre-create a manifest with a recorded but not-yet-deleted user
        import json as _json  # noqa: PLC0415
        import tempfile, os  # noqa: PLC0415, E401
        mdir = tempfile.mkdtemp(prefix="fi-cms-w3-test-")
        os.environ["FI_CMS_LIVE_MANIFEST_DIR"] = mdir
        # Manually write a manifest with a pending user entry
        manifest_path = (
            __import__("pathlib").Path(mdir) / f"{_RUN_ID}.json"
        )
        username = f"livetest-{_RUN_ID}-gnd-op@example.invalid"
        manifest_path.write_text(_json.dumps({
            "run_id": _RUN_ID, "stage": "staging",
            "items": {
                "stage": "staging",
                "user/gnd-op": {"username": username, "status": "created"},
            },
        }))
        # Create the user in the pool (FakeUserPool uses email as username)
        idp_internal = w.internal("cognito-idp")
        idp_internal.admin_create_user(UserPoolId=w.pool.pool_id, Username=username,
                                       TemporaryPassword="Tmp!123456")
        count_before = len(idp_internal.list_users(UserPoolId=w.pool.pool_id)["Users"])
        # Run cleanup
        rc, text = _run_main(mod, ["--allow-writes", "--part", "cleanup"],
                             run_id=_RUN_ID, manifest_dir=mdir)
        count_after = len(idp_internal.list_users(UserPoolId=w.pool.pool_id)["Users"])
    assert count_after < count_before, (
        f"cleanup should have deleted the manifest user; before={count_before} after={count_after}\n{text}"
    )
    # rc may be 0 (clean) or 2 (skip if pool lookup fails) but not 1 (fail)
    assert rc != 1, f"cleanup should not FAIL; got rc={rc}\n{text}"


def test_pre_alone_passes_with_read_only_cognito() -> None:
    """`--part pre` on its own (no write part) must finish with no FAIL: its read-only
    Cognito client still has to run main()'s residue check (ListUsers). Found by the
    live read-only pre-check 2026-10-23 00:39Z, which FAILed with a GuardViolation."""
    with CmsWorld(run_scheduled=False) as w:
        _setup_world(w)
        mod = _load_mod(w)
        rc, text = _run_main(mod, ["--part", "pre"])
    rows = _rows(text)
    assert not [k for k, v in rows.items() if v == "FAIL"], f"FAIL rows: {rows}\n{text}"
    assert "GuardViolation" not in text and "not in allowlist" not in text, text
    assert rc == 0, f"rc {rc}\n{text}"

# ---------------------------------------------------------------------------
# B-3 N1 scenario tests for POST /groundings collision-row approach
# ---------------------------------------------------------------------------


def _seed_returned_grounding(
    world: CmsWorld,
    vehicle_id: str,
    vin: str,
    fleet_id: str,
    action_id: str,
    finding_id: str,
    *,
    recall_form: bool = True,
) -> None:
    """Seed a returned (closed) grounding row for use in B-3 tests.

    recall_form=True: adds campaignId (recall); False: adds reason=fault.
    returnedAt is always set so _pick_grounding_collision_row accepts it.
    """
    row: dict = {
        "vehicleId": vehicle_id,
        "actionId": action_id,
        "vin": vin,
        "fleetId": fleet_id,
        "findingId": finding_id,
        "approverSub": "test-seed",
        "groundedAt": "2026-01-01T00:00:00Z",
        "returnedAt": "2026-01-02T00:00:00Z",
    }
    if recall_form:
        row["campaignId"] = "MRD-2026-002"
    else:
        row["reason"] = "fault"
        row["faultCodes"] = ["P0420"]
    world.put(GROUNDINGS_TABLE, row)


def test_isolation_groundings_collision_recall_row() -> None:
    """B-3 scenario (b) recall form: control 409 PASS, probe 403 PASS, table unchanged.

    FC1 (fresh actionId in body) is caught: the body would contain a new actionId
    not matching any existing row, so CMS creates a new row (201); test fails because
    table grows and control is not 409.
    """
    with CmsWorld(run_scheduled=False) as w:
        _setup_world(w)
        _seed_returned_grounding(
            w, _FLEET_A_VID, _FLEET_A_VIN, _FLEET_A,
            action_id="act-b3-recall-ctrl", finding_id="find-b3-recall",
            recall_form=True,
        )
        before = w.scan(GROUNDINGS_TABLE)
        mod = _load_mod(w)
        rc, text = _run_main(mod, ["--allow-writes", "--part", "isolation"])
        after = w.scan(GROUNDINGS_TABLE)
    rows = _rows(text)
    ctrl_key = next(
        (k for k in rows if "POST /groundings" in k and "control" in k.lower()), None
    )
    probe_key = next(
        (k for k in rows if "POST /groundings" in k and "probe" in k.lower()), None
    )
    assert ctrl_key and rows[ctrl_key] == "PASS", (
        f"control must PASS; rows={rows}\n{text}"
    )
    assert probe_key and rows[probe_key] == "PASS", (
        f"probe must PASS; rows={rows}\n{text}"
    )
    assert sorted(r["actionId"] for r in after) == sorted(r["actionId"] for r in before), (
        f"table changed; before={before} after={after}\n{text}"
    )
    assert rc == 0, f"exit {rc}\n{text}"


def test_isolation_groundings_collision_fault_row() -> None:
    """B-3 scenario (b) fault form: control 409 PASS, probe 403 PASS, table unchanged."""
    with CmsWorld(run_scheduled=False) as w:
        _setup_world(w)
        _seed_returned_grounding(
            w, _FLEET_A_VID, _FLEET_A_VIN, _FLEET_A,
            action_id="act-b3-fault-ctrl", finding_id="find-b3-fault",
            recall_form=False,
        )
        before = w.scan(GROUNDINGS_TABLE)
        mod = _load_mod(w)
        rc, text = _run_main(mod, ["--allow-writes", "--part", "isolation"])
        after = w.scan(GROUNDINGS_TABLE)
    rows = _rows(text)
    ctrl_key = next(
        (k for k in rows if "POST /groundings" in k and "control" in k.lower()), None
    )
    assert ctrl_key and rows[ctrl_key] == "PASS", (
        f"fault-form control must PASS; rows={rows}\n{text}"
    )
    assert sorted(r["actionId"] for r in after) == sorted(r["actionId"] for r in before), (
        f"table changed (fault form); before={before} after={after}\n{text}"
    )
    assert rc == 0, f"exit {rc}\n{text}"


def test_isolation_groundings_scope_leak_probe_fails() -> None:
    """B-3 scenario (c): seeded row + scope fully open → probe gets 409 → FAIL row.

    Table is unchanged because conditional put fails on the existing row.
    FC1 (fresh actionId) is also caught here: control would get 201 → table grows.
    """
    with CmsWorld(run_scheduled=False) as w:
        _setup_world(w)
        _seed_returned_grounding(
            w, _FLEET_A_VID, _FLEET_A_VIN, _FLEET_A,
            action_id="act-b3-scope-ctrl", finding_id="find-b3-scope",
            recall_form=True,
        )
        fi_mod = w.cms_fi._mod
        fi_mod._authorize_or_403 = lambda event, method, resource, params, qs, body: None
        fi_mod._auth.authorize_fleet_write = lambda claims, fleet_id: (True, None)
        before = w.scan(GROUNDINGS_TABLE)
        mod = _load_mod(w)
        rc, text = _run_main(mod, ["--allow-writes", "--part", "isolation"])
        after = w.scan(GROUNDINGS_TABLE)
    rows = _rows(text)
    probe_key = next(
        (k for k in rows if "POST /groundings" in k and "probe" in k.lower()), None
    )
    assert probe_key and rows[probe_key] == "FAIL", (
        f"probe must FAIL (got 409 not 403 on scope leak); rows={rows}\n{text}"
    )
    assert sorted(r["actionId"] for r in after) == sorted(r["actionId"] for r in before), (
        f"table changed on scope leak; before={before} after={after}\n{text}"
    )
    assert rc == 1, f"expected rc=1, got {rc}"


def test_isolation_groundings_row_deleted_before_post() -> None:
    """B-3 scenario (d): collision row deleted between pick and first POST.

    Hook update_manifest to delete the row immediately after the manifest records it;
    _reread_ok then finds no row and FAIL-stops without sending any POST.
    Dropping the pre-POST re-read (dropping the _reread_ok guard) would catch this:
    the POST would create a new row (no existing actionId) → 201 → test fails.
    """
    with CmsWorld(run_scheduled=False) as w:
        _setup_world(w)
        _seed_returned_grounding(
            w, _FLEET_A_VID, _FLEET_A_VIN, _FLEET_A,
            action_id="act-b3-del-ctrl", finding_id="find-b3-del",
            recall_form=True,
        )
        fi_mod = w.cms_fi._mod
        gnd_create_calls: list[tuple] = []
        _orig_gnd = fi_mod._handle_groundings_create

        def _spy_gnd(body: Any, claims: Any) -> Any:
            gnd_create_calls.append((body, claims))
            return _orig_gnd(body, claims)

        fi_mod._handle_groundings_create = _spy_gnd
        mod = _load_mod(w)
        # Hook update_manifest: when isolation_grounding_collision is recorded, delete the row
        _orig_um = mod.update_manifest
        hooked = [False]

        def _hook_um(path: Any, key: str, value: Any) -> None:
            _orig_um(path, key, value)
            if key == "isolation_grounding_collision" and not hooked[0]:
                hooked[0] = True
                w.table(GROUNDINGS_TABLE).delete_item(
                    Key={"vehicleId": value["vehicle_id"],
                         "actionId": value["action_id"]},
                )

        mod.update_manifest = _hook_um
        rc, text = _run_main(mod, ["--allow-writes", "--part", "isolation"])
        after = w.scan(GROUNDINGS_TABLE)
    assert hooked[0], "hook did not fire — test setup invalid"
    assert gnd_create_calls == [], (
        f"POST /groundings handler reached {len(gnd_create_calls)} times; expected 0\n{text}"
    )
    fail_lines = [ln for ln in text.splitlines() if "[FAIL]" in ln and "pre-read" in ln]
    assert fail_lines, (
        f"expected a FAIL pre-read line; text:\n{text}"
    )
    assert after == [], f"expected empty table after row deleted; got {after}"
    assert rc == 1, f"expected rc=1, got {rc}"


def test_isolation_groundings_residue_query_runs_after_control_only() -> None:
    """Architect (c2): the collision row vanishes DURING the control POST (a concurrent
    delete after the pre-read), so CMS creates a row with this run's mismatch findingId.
    The probe's pre-read then refuses (findingId changed) and the probe is never sent.
    The post-POST residue query must still run and name the written row with a delete
    command: it runs once ANY POST was sent, not only when both were.
    Deliberate break: set `_posts_sent = True` only after the probe -> no residue row."""
    with CmsWorld(run_scheduled=False) as w:
        _setup_world(w)
        _seed_returned_grounding(
            w, _FLEET_A_VID, _FLEET_A_VIN, _FLEET_A,
            action_id="act-c2-race-ctrl", finding_id="find-c2-race",
            recall_form=True,
        )
        fi_mod = w.cms_fi._mod
        _orig_gnd = fi_mod._handle_groundings_create
        fired = [0]

        def _racing_gnd(body: Any, claims: Any) -> Any:
            if fired[0] == 0:
                fired[0] = 1
                w.table(GROUNDINGS_TABLE).delete_item(
                    Key={"vehicleId": _FLEET_A_VID, "actionId": "act-c2-race-ctrl"},
                )
            return _orig_gnd(body, claims)

        fi_mod._handle_groundings_create = _racing_gnd
        mod = _load_mod(w)
        rc, text = _run_main(mod, ["--allow-writes", "--part", "isolation"])
        after = w.scan(GROUNDINGS_TABLE)
    assert fired[0] == 1, f"race hook never fired\n{text}"
    written = [r for r in after if str(r.get("findingId", "")).endswith("-iso-mismatch")]
    assert len(written) == 1, f"setup: expected the race to write one row; got {after}\n{text}"
    rows = _rows(text)
    assert rows.get("isolation POST /groundings residue check") == "FAIL", (
        f"residue query must run after the control-only POST; rows={rows}\n{text}"
    )
    assert "aws dynamodb delete-item" in text, text
    assert rc == 1, f"expected rc=1, got {rc}\n{text}"


@pytest.mark.parametrize("race", [False, True])
def test_cleanup_checks_isolation_mismatch_row(race: bool) -> None:
    """Architect (c2, N1 step 6): cleanup reads the vehicle partition the isolation part
    recorded and FAILs on a row carrying this run's mismatch findingId (a write the
    isolation POSTs must never make); with no such row the check PASSes.
    Deliberate break: the cleanup check's condition -> `True` (race case passes)."""
    mdir = tempfile.mkdtemp(prefix="fi-cms-f1-isocu-")
    with CmsWorld(run_scheduled=False) as w:
        _setup_world(w)
        _seed_returned_grounding(
            w, _FLEET_A_VID, _FLEET_A_VIN, _FLEET_A,
            action_id="act-c2-cu-ctrl", finding_id="find-c2-cu", recall_form=True,
        )
        if race:
            fi_mod = w.cms_fi._mod
            _orig_gnd = fi_mod._handle_groundings_create
            fired = [0]

            def _racing_gnd(body: Any, claims: Any) -> Any:
                if fired[0] == 0:
                    fired[0] = 1
                    w.table(GROUNDINGS_TABLE).delete_item(
                        Key={"vehicleId": _FLEET_A_VID, "actionId": "act-c2-cu-ctrl"},
                    )
                return _orig_gnd(body, claims)

            fi_mod._handle_groundings_create = _racing_gnd
        mod = _load_mod(w)
        _run_main(mod, ["--allow-writes", "--part", "isolation"], manifest_dir=mdir)
        rc, text = _run_main(mod, ["--allow-writes", "--part", "cleanup"], manifest_dir=mdir)
    rows_full = [ln.strip() for ln in text.splitlines()
                 if ln.strip().startswith("[")
                 and "cleanup residue: isolation grounding mismatch row gone" in ln]
    want = "[FAIL]" if race else "[PASS]"
    assert len(rows_full) == 1 and rows_full[0].startswith(want), (
        f"expected one {want} isolation-residue row; got {rows_full}\n{text}"
    )
    if race:
        assert rc == 1, f"expected rc=1, got {rc}\n{text}"


def test_isolation_groundings_open_row_not_picked() -> None:
    """_pick_grounding_collision_row ignores open rows (no returnedAt).

    Dropping the returnedAt filter would return the open row; the subsequent
    POST would try to create a duplicate grounding.  Test the helper directly
    to avoid the discovery / fault-seed coupling of the full isolation flow.
    """
    with CmsWorld(run_scheduled=False) as w:
        _setup_world(w)
        # Seed an OPEN (no returnedAt) fleet-A row for vehicle(1)
        w.put(GROUNDINGS_TABLE, {
            "vehicleId": _FLEET_A_VID,
            "actionId": "act-b3-open-only",
            "vin": _FLEET_A_VIN,
            "fleetId": _FLEET_A,
            "findingId": "find-b3-open",
            "approverSub": "test-seed",
            "groundedAt": "2026-01-01T00:00:00Z",
            # no returnedAt — open row; must not be picked
        })
        mod = _load_mod(w)
        # Call _pick_grounding_collision_row directly via the world's low-level DDB client
        ddb_internal = w.internal("dynamodb")
        result = mod._pick_grounding_collision_row(ddb_internal, "staging", mod.FLEET_A)
    assert result is None, (
        f"expected None (no returned row); got {result!r} — "
        f"open row must not be picked (returnedAt filter required)"
    )


# ---------------------------------------------------------------------------
# B-4 WN3: perf records all four fleet IDs and their totals
# ---------------------------------------------------------------------------


def test_perf_all_four_fleets_requested_and_totals_recorded() -> None:
    """B-4 WN3: perf must request /faults for all four fleets and record fleet_totals.

    FC6 (all 20 requests go to FLEET_A) fails this: only one fleet appears.
    """
    with CmsWorld(run_scheduled=False) as w:
        _setup_world(w)
        fi_mod = w.cms_fi._mod
        requested_fleets: list[str] = []
        _orig_faults = fi_mod._handle_faults_get

        def _spy_faults(qs: Any, claims: Any) -> Any:
            requested_fleets.append(qs.get("fleetId", ""))
            return _orig_faults(qs, claims)

        fi_mod._handle_faults_get = _spy_faults
        mod = _load_mod(w)
        manifest_dir = tempfile.mkdtemp(prefix="fi-cms-b4-test-")
        rc, text = _run_main(mod, ["--allow-writes", "--part", "perf"],
                             manifest_dir=manifest_dir)
    # All four fleets must appear in the requests
    seen_fleets = set(requested_fleets)
    all_four = set(mod._ALL_FLEETS_LIST)
    assert all_four <= seen_fleets, (
        f"missing fleets in /faults requests: {all_four - seen_fleets}; "
        f"requested {sorted(seen_fleets)}\n{text}"
    )
    # manifest must record fleet_totals with four entries
    import glob as _glob  # noqa: PLC0415
    manifests = _glob.glob(f"{manifest_dir}/*.json")
    assert manifests, "no manifest file written"
    saved = json.loads(open(manifests[0]).read())["items"]
    perf = saved.get("perf", {})
    fleet_totals = perf.get("fleet_totals", {})
    assert set(fleet_totals.keys()) == all_four, (
        f"fleet_totals must have all four fleets; got {sorted(fleet_totals.keys())}"
    )
    # Each total must match what the handler returned
    for fid in all_four:
        assert isinstance(fleet_totals[fid], int), (
            f"fleet_totals[{fid!r}] must be int, got {type(fleet_totals[fid])}"
        )
    assert rc == 0, f"exit {rc}\n{text}"
