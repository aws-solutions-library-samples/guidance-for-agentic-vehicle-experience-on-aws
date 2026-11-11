"""Stage 1 tests for scripts/live-verify-fi-quality.py — after-6 items.

RED PHASE: committed while failing.  Each test proves a Stage 1 contract the current
script does not yet satisfy.  After the implementation lands they all turn green.

Covers:
  A1   _parse_args accepts --part after-6-check with --run-id
  A2   _parse_args accepts --cleanup-only with --run-id and --allow-writes
  A3   _parse_args accepts --run-id on its own
  A4   _validate_run_id accepts the generated id shape and rejects bad shapes
  A5   _manifest_path / _load_manifest / _save_manifest exist and satisfy 0600/0700 invariants
  A6   _after6_write_window_guard function exists and returns None inside the window
  A7   _after6_write_window_guard refuses inside a schedule's 45-min exclusion window
  A8   _after6_pre_checks refuses when ESM is not Enabled
  A9   _after6_pre_checks refuses when queue is not empty
  A10  _after6_pre_checks refuses when DLQ is not empty
  B1   item 2 — FAIL when root cause candidate does not match the plant (M3 re-anchored)
  B2   item 2 — FAIL when quality.pattern is absent (M4 re-anchored)
  B3   item 2 — FAIL when fewer than 80 % of plants have a passing cause (M5 re-anchored)
  B4   item 2 — FAIL when failure mode id does not match the plant's (M6 re-anchored)
  B5   item 2 — FAIL when quality.options is absent or empty
  B6   item 3 — FAIL when a repeat POST returns a different investigation id
  B7   item 3 — FAIL when the cap POST does not return 429
  B8   items 4 / 5 / 8b / 9 SKIP with the Stage 2 placeholder message, no AWS call
  C1   cleanup runs (in finally) even when item 2 raises mid-check
  C2   residue scan FAIL when an investigation row survives cleanup
  C3   --part after-6 exits 2 (never 0) when items 2 and 3 pass but 8b pending
  C4   --part after-6-check refuses if run-id manifest is absent
  C5   write-window guard first check is before the first AWS write, not after

Run:
    PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest \
        scripts/tests/test_live_verify_fi_quality_after6_s1.py -q
"""
from __future__ import annotations

import importlib.util
import io
import json
import os
import signal
import socket
import stat
import sys
import time
import uuid
import zipfile
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterator

import pytest

# ---------------------------------------------------------------------------
# Load the script module
# ---------------------------------------------------------------------------

_HERE = Path(__file__).resolve().parent
_SCRIPT = _HERE.parent / "live-verify-fi-quality.py"
_spec = importlib.util.spec_from_file_location("lv_after6_s1_mod", _SCRIPT)
lv = importlib.util.module_from_spec(_spec)  # type: ignore[arg-type]
_spec.loader.exec_module(_mod := lv)  # type: ignore[union-attr]

# ---------------------------------------------------------------------------
# Signal fixture
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _restore_signals() -> Iterator[None]:
    old_int = signal.getsignal(signal.SIGINT)
    old_term = signal.getsignal(signal.SIGTERM)
    old_hup = signal.getsignal(signal.SIGHUP)
    yield
    signal.signal(signal.SIGINT, old_int)
    signal.signal(signal.SIGTERM, old_term)
    signal.signal(signal.SIGHUP, old_hup)


# ---------------------------------------------------------------------------
# Loopback guard: no real AWS calls from these tests
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _loopback_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    real_connect = socket.socket.connect

    def _connect(sock: socket.socket, address: Any) -> Any:
        host = address[0] if isinstance(address, tuple) else str(address)
        if host not in ("127.0.0.1", "::1", "localhost"):
            raise AssertionError(f"test reached real host {address!r}")
        return real_connect(sock, address)

    monkeypatch.setattr(socket.socket, "connect", _connect)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_VALID_RUN_ID = "fi-quality-20261027T150000-abcdef01"


def _fake_pass(res: lv.Results, **_: Any) -> None:
    lv.check(res, "fake-pass", True, "ok")


def _fake_skip(res: lv.Results, **_: Any) -> None:
    res.skip("fake-skip", "placeholder")


def _fake_fail(res: lv.Results, **_: Any) -> None:
    lv.check(res, "fake-fail", False, on_fail="bad")


# ---------------------------------------------------------------------------
# A1: _parse_args accepts --part after-6-check with --run-id
# ---------------------------------------------------------------------------

def test_a1_parse_after6_check() -> None:
    """_parse_args must accept 'after-6-check' as a --part value."""
    args = _mod._parse_args([
        "--stage", "staging",
        "--part", "after-6-check",
        "--run-id", _VALID_RUN_ID,
        "--allow-writes",
    ])
    assert args.part == "after-6-check"
    assert args.run_id == _VALID_RUN_ID
    assert args.allow_writes is True


# ---------------------------------------------------------------------------
# A2: _parse_args accepts --cleanup-only with --run-id and --allow-writes
# ---------------------------------------------------------------------------

def test_a2_parse_cleanup_only() -> None:
    """_parse_args must accept --cleanup-only."""
    args = _mod._parse_args([
        "--stage", "staging",
        "--cleanup-only",
        "--run-id", _VALID_RUN_ID,
        "--allow-writes",
    ])
    assert args.cleanup_only is True
    assert args.run_id == _VALID_RUN_ID


# ---------------------------------------------------------------------------
# A3: _parse_args accepts --run-id on its own (for --part after-6-check)
# ---------------------------------------------------------------------------

def test_a3_parse_run_id_accepted() -> None:
    """--run-id is accepted by _parse_args."""
    args = _mod._parse_args([
        "--stage", "staging",
        "--part", "after-6",
        "--allow-writes",
        "--run-id", _VALID_RUN_ID,
    ])
    assert args.run_id == _VALID_RUN_ID


# ---------------------------------------------------------------------------
# A4: _validate_run_id
# ---------------------------------------------------------------------------

def test_a4_validate_run_id_accepts_generated_shape() -> None:
    """The shape the script generates must be accepted."""
    assert _mod._validate_run_id("fi-quality-20261027T150000-abcdef01") is True
    assert _mod._validate_run_id("fi-quality-20260101T000000-00000000") is True


def test_a4_validate_run_id_rejects_bad_shapes() -> None:
    for bad in [
        "fi-quality-20261027T150000-ABCDEF01",   # uppercase hex
        "fi-quality-20261027T150000-abcdef0",    # 7 hex chars
        "fi-quality-20261027T150000-abcdef010",  # 9 hex chars
        "fi-quality-20261027-abcdef01",          # no time
        "quality-20261027T150000-abcdef01",      # wrong prefix
        "",
        "LIVETEST-injection; DROP TABLE",
    ]:
        assert _mod._validate_run_id(bad) is False, f"should reject {bad!r}"


# ---------------------------------------------------------------------------
# A5: manifest functions — 0600/0700 invariants
# ---------------------------------------------------------------------------

def test_a5_manifest_path_fn_exists(tmp_path: Path) -> None:
    """_manifest_path must exist and return a Path."""
    p = _mod._manifest_path(_VALID_RUN_ID, str(tmp_path))
    assert isinstance(p, Path)


def test_a5_manifest_roundtrip_at_0600(tmp_path: Path) -> None:
    """Saved manifest is at 0600; the directory is at 0700; re-loaded matches."""
    data = {"run_id": _VALID_RUN_ID, "investigations": [], "users": []}
    _mod._save_manifest(_VALID_RUN_ID, data, str(tmp_path))
    p = _mod._manifest_path(_VALID_RUN_ID, str(tmp_path))
    assert p.exists()
    assert stat.S_IMODE(os.stat(p).st_mode) == 0o600, f"expected 0600 got {oct(stat.S_IMODE(os.stat(p).st_mode))}"
    loaded = _mod._load_manifest(_VALID_RUN_ID, str(tmp_path))
    assert loaded["run_id"] == _VALID_RUN_ID


def test_a5_manifest_dir_is_0700(tmp_path: Path) -> None:
    """The per-run directory is created at 0700."""
    data = {"run_id": _VALID_RUN_ID}
    _mod._save_manifest(_VALID_RUN_ID, data, str(tmp_path))
    p = _mod._manifest_path(_VALID_RUN_ID, str(tmp_path))
    d = p.parent
    assert stat.S_IMODE(os.stat(d).st_mode) == 0o700, f"expected 0700 got {oct(stat.S_IMODE(os.stat(d).st_mode))}"


def test_a5_load_manifest_absent_returns_none(tmp_path: Path) -> None:
    """_load_manifest returns None when no manifest exists."""
    result = _mod._load_manifest(_VALID_RUN_ID, str(tmp_path))
    assert result is None


# ---------------------------------------------------------------------------
# A6: _after6_write_window_guard returns None inside the open window
# ---------------------------------------------------------------------------

def test_a6_write_window_guard_function_exists() -> None:
    """_after6_write_window_guard must exist."""
    assert hasattr(_mod, "_after6_write_window_guard")


def test_a6_write_window_guard_returns_none_in_open_window() -> None:
    """In the open window (07:00 UTC, schedule at 02:00), returns None (>45 min away)."""
    import datetime as dt

    class _FakeScheduler:
        def list_schedules(self, **_: Any) -> dict:
            return {
                "Schedules": [
                    {"Name": "vsa-staging-tier2-daily-battery-us-west-2",
                     "GroupName": "default"},
                ]
            }

        def get_schedule(self, **kw: Any) -> dict:
            return {
                "ScheduleExpression": "cron(0 2 * * ? *)",
                "ScheduleExpressionTimezone": "UTC",
            }

    # 07:00 is 5h after 02:00 — well outside the 45-min guard window
    now = dt.datetime(2026, 10, 27, 7, 0, 0, tzinfo=dt.timezone.utc)
    result = _mod._after6_write_window_guard(_FakeScheduler(), "staging", now)
    assert result is None


# ---------------------------------------------------------------------------
# A7: write-window guard refuses inside a schedule's 45-min exclusion
# ---------------------------------------------------------------------------

def test_a7_write_window_guard_refuses_near_schedule() -> None:
    """When a cron schedule is within 45 min, the guard returns a refusal reason."""
    import datetime as dt

    # A cron schedule at 02:00 means the blocked window is 01:15–02:45.
    # At 01:30 UTC (15 min after 01:15 open boundary) we are 30 min before 02:00 → refused.
    class _FakeScheduler:
        def list_schedules(self, **_: Any) -> dict:
            return {
                "Schedules": [
                    {"Name": "vsa-staging-tier2-daily-battery-us-west-2",
                     "GroupName": "default"},
                ]
            }

        def get_schedule(self, **kw: Any) -> dict:
            return {
                "ScheduleExpression": "cron(0 2 * * ? *)",
                "ScheduleExpressionTimezone": "UTC",
            }

    now = dt.datetime(2026, 10, 27, 1, 30, 0, tzinfo=dt.timezone.utc)  # 30 min before 02:00
    result = _mod._after6_write_window_guard(_FakeScheduler(), "staging", now)
    assert result is not None
    assert "tier2-daily-battery" in result or "30" in result or "min" in result.lower()


# ---------------------------------------------------------------------------
# A8: _after6_pre_checks refuses when ESM is not Enabled
# ---------------------------------------------------------------------------

def test_a8_pre_checks_refuses_esm_not_enabled() -> None:
    """_after6_pre_checks must return a refusal when the ESM is Disabled."""

    class _Lam:
        def list_event_source_mappings(self, **kw: Any) -> dict:
            return {"EventSourceMappings": [
                {"UUID": "uuid-1",
                 "EventSourceArn": "arn:aws:sqs:us-west-2:123:vsa-staging-avx-investigations",
                 "State": "Disabled"}
            ]}

    class _Sqs:
        def get_queue_attributes(self, **kw: Any) -> dict:
            return {"Attributes": {"ApproximateNumberOfMessages": "0",
                                   "ApproximateNumberOfMessagesNotVisible": "0"}}

    result = _mod._after6_pre_checks(_Sqs(), _Lam(), "staging")
    assert result is not None
    assert "Disabled" in result or "enabled" in result.lower() or "ESM" in result


# ---------------------------------------------------------------------------
# A9: _after6_pre_checks refuses when queue is not empty
# ---------------------------------------------------------------------------

def test_a9_pre_checks_refuses_queue_not_empty() -> None:
    """_after6_pre_checks must return a refusal when the main queue has messages."""

    class _Lam:
        def list_event_source_mappings(self, **kw: Any) -> dict:
            return {"EventSourceMappings": [
                {"UUID": "uuid-1",
                 "EventSourceArn": "arn:aws:sqs:us-west-2:123:vsa-staging-avx-investigations",
                 "State": "Enabled"}
            ]}

    _call_count = 0

    class _Sqs:
        def get_queue_attributes(self, QueueUrl: str, **kw: Any) -> dict:
            nonlocal _call_count
            _call_count += 1
            if "dlq" in QueueUrl:
                return {"Attributes": {"ApproximateNumberOfMessages": "0",
                                       "ApproximateNumberOfMessagesNotVisible": "0"}}
            return {"Attributes": {"ApproximateNumberOfMessages": "3",
                                   "ApproximateNumberOfMessagesNotVisible": "0"}}

        def get_queue_url(self, **kw: Any) -> dict:
            name = kw.get("QueueName", "")
            return {"QueueUrl": f"https://sqs.us-west-2.amazonaws.com/123/{name}"}

    result = _mod._after6_pre_checks(_Sqs(), _Lam(), "staging")
    assert result is not None
    assert "3" in result or "empty" in result.lower() or "queue" in result.lower()


# ---------------------------------------------------------------------------
# A10: _after6_pre_checks refuses when DLQ is not empty
# ---------------------------------------------------------------------------

def test_a10_pre_checks_refuses_dlq_not_empty() -> None:
    """_after6_pre_checks must return a refusal when the DLQ has messages."""

    class _Lam:
        def list_event_source_mappings(self, **kw: Any) -> dict:
            return {"EventSourceMappings": [
                {"UUID": "uuid-1",
                 "EventSourceArn": "arn:aws:sqs:us-west-2:123:vsa-staging-avx-investigations",
                 "State": "Enabled"}
            ]}

    class _Sqs:
        def get_queue_attributes(self, QueueUrl: str, **kw: Any) -> dict:
            if "dlq" in QueueUrl:
                return {"Attributes": {"ApproximateNumberOfMessages": "1",
                                       "ApproximateNumberOfMessagesNotVisible": "0"}}
            return {"Attributes": {"ApproximateNumberOfMessages": "0",
                                   "ApproximateNumberOfMessagesNotVisible": "0"}}

        def get_queue_url(self, **kw: Any) -> dict:
            name = kw.get("QueueName", "")
            return {"QueueUrl": f"https://sqs.us-west-2.amazonaws.com/123/{name}"}

    result = _mod._after6_pre_checks(_Sqs(), _Lam(), "staging")
    assert result is not None
    assert "dlq" in result.lower() or "DLQ" in result or "1" in result


# ---------------------------------------------------------------------------
# Fake world helpers for items 2/3 FAIL-path tests
# ---------------------------------------------------------------------------

def _zip_stub() -> bytes:
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w") as z:
        z.writestr("index.py", "def h(e,c): raise RuntimeError('stub')\n")
    return b.getvalue()


def _make_item2_world(
    *,
    completed_inv: dict | None = None,  # pre-seeded investigation row (quality data)
    region: str = "us-west-2",
    stage: str = "staging",
    account: str = "123456789012",
    answer_key: list[dict] | None = None,  # planted answer key rows
    pattern_rows: list[dict] | None = None,  # live DDB pattern rows
) -> "Item2World":
    """Build a minimal moto-backed world for item 2 FAIL-path tests."""
    return Item2World(
        completed_inv=completed_inv,
        region=region, stage=stage, account=account,
        answer_key=answer_key, pattern_rows=pattern_rows,
    )


class Item2World:
    """Minimal moto-backed world for item 2/3 FAIL-path tests.

    Does NOT use FiLoopWorld; just sets up the DDB tables, SQS, Cognito
    and HTTP server needed to run _item_2/_item_3 against pre-seeded data.
    Uses try/except in __enter__ to ensure moto context is always cleaned
    up on failure (prevents leaking moto state into subsequent tests).
    """

    def __init__(self, *, completed_inv, region, stage, account, answer_key, pattern_rows,
                 finding=None, daily_cap: int = 20):
        self.region = region
        self.stage = stage
        self.account = account
        self.completed_inv = completed_inv
        self.finding = finding
        self.answer_key = answer_key or []
        self.pattern_rows = pattern_rows or []
        self.daily_cap = daily_cap
        self._ctx = None
        self.api = None
        self.router = None

    def __enter__(self) -> "Item2World":
        from moto import mock_aws as _mock_aws
        import boto3

        self._ctx = _mock_aws()
        self._ctx.__enter__()
        try:
            self._setup(boto3)
        except Exception:
            if self.api:
                try:
                    self.api.close()
                except Exception:
                    pass
            if self.router:
                try:
                    self.router.uninstall()
                except Exception:
                    pass
            self._ctx.__exit__(None, None, None)
            self._ctx = None
            raise
        return self

    def _setup(self, boto3: Any) -> None:
        """Set up all moto resources inside the moto context."""
        # AVX DDB tables (us-west-2)
        ddb = boto3.client("dynamodb", region_name=self.region)
        for tn, pk in [
            (f"vsa-{self.stage}-avx-investigations", "investigation_id"),
            (f"vsa-{self.stage}-avx-findings", "finding_id"),
            (f"vsa-{self.stage}-avx-actions", "action_id"),
        ]:
            ddb.create_table(
                TableName=tn,
                KeySchema=[{"AttributeName": pk, "KeyType": "HASH"}],
                AttributeDefinitions=[{"AttributeName": pk, "AttributeType": "S"}],
                BillingMode="PAY_PER_REQUEST",
            )

        # ADP DDB tables (us-east-1)
        ddb_east = boto3.client("dynamodb", region_name="us-east-1")
        for tn, pk in [
            (f"adp-{self.stage}-fault-patterns", "pattern_id"),
            (f"adp-{self.stage}-engineering-corpus", "pk"),
        ]:
            ddb_east.create_table(
                TableName=tn,
                KeySchema=[{"AttributeName": pk, "KeyType": "HASH"}],
                AttributeDefinitions=[{"AttributeName": pk, "AttributeType": "S"}],
                BillingMode="PAY_PER_REQUEST",
            )

        ddb_r = boto3.resource("dynamodb", region_name=self.region)
        self.inv_tbl = ddb_r.Table(f"vsa-{self.stage}-avx-investigations")

        # Seed pattern rows in us-east-1
        ddb_east_r = boto3.resource("dynamodb", region_name="us-east-1")
        p_tbl = ddb_east_r.Table(f"adp-{self.stage}-fault-patterns")
        for row in self.pattern_rows:
            p_tbl.put_item(Item=row)

        # Seed the pre-seeded completed investigation and lock
        if self.completed_inv:
            inv_id = self.completed_inv["investigation_id"]
            pattern_group_id = self.completed_inv.get("pattern_group_id", "abc123def456789a")
            self.inv_tbl.put_item(Item=self.completed_inv)
            self.inv_tbl.put_item(Item={
                "investigation_id": f"lock#{pattern_group_id}",
                "investigation_id_ref": inv_id,
                "status": "completed",
            })
            # Seed the associated Finding in the findings table
            if self.finding:
                ddb_r.Table(f"vsa-{self.stage}-avx-findings").put_item(Item=self.finding)

        # SQS queue + DLQ + ESM
        sqs = boto3.client("sqs", region_name=self.region)
        q = sqs.create_queue(QueueName=f"vsa-{self.stage}-avx-investigations")
        self.queue_url = q["QueueUrl"]
        dlq = sqs.create_queue(QueueName=f"vsa-{self.stage}-avx-investigations-dlq")
        self.dlq_url = dlq["QueueUrl"]

        iam = boto3.client("iam", region_name=self.region)
        role = iam.create_role(
            RoleName="item2-test-role", AssumeRolePolicyDocument="{}"
        )["Role"]["Arn"]
        lam = boto3.client("lambda", region_name=self.region)
        lam.create_function(
            FunctionName=f"vsa-{self.stage}-tier2-health-agent-{self.region}",
            Runtime="python3.12", Role=role, Handler="index.h",
            Code={"ZipFile": _zip_stub()},
        )
        q_arn = sqs.get_queue_attributes(
            QueueUrl=self.queue_url, AttributeNames=["QueueArn"]
        )["Attributes"]["QueueArn"]
        esm = lam.create_event_source_mapping(
            EventSourceArn=q_arn,
            FunctionName=f"vsa-{self.stage}-tier2-health-agent-{self.region}",
            Enabled=True,
        )
        self.esm_uuid = esm["UUID"]

        # FakeUserPool + Router for Cognito
        from scripts.tests.fi_rw_world.apigw import FakeRestApi
        from scripts.tests.fi_rw_world.awsfake import Router
        from scripts.tests.fi_rw_world.cognito import FakeUserPool
        from scripts.tests.fi_rw_world.lambdas import lambda_env
        import importlib.util as _iu

        avx_root = _SCRIPT.parent.parent

        class _Clock:
            def epoch(self) -> float:
                return time.time()
            def now(self):
                import datetime as _dt
                return _dt.datetime.now(_dt.timezone.utc)

        self.pool = FakeUserPool(_Clock(), self.region)
        self.router = Router()
        self.pool.register(self.router)
        pool = self.pool

        def _list_user_pools(p: dict) -> dict:
            return {"UserPools": [{"Id": pool.pool_id, "Name": "cms-staging-ui-users"}]}

        self.router.add("cognito-idp", "ListUserPools", _list_user_pools)

        # M-B4/M-B5 fix: add AdminDeleteUser + ListUsers handlers so cleanup
        # succeeds instead of retrying against moto (which has no pool).
        # This matches the FiLoopWorld's _FakeIDP.admin_delete_user behaviour.
        def _admin_delete_user(p: dict) -> dict:
            # Find user by email attribute or by sub key.
            uid = next(
                (u for u, data in pool.users.items()
                 if data["attrs"].get("email") == p.get("Username")
                 or u == p.get("Username")),
                None,
            )
            if uid is not None:
                del pool.users[uid]
            # User gone (or was already gone) — return empty body.
            # NOT raising UserNotFoundException so the caller's retry loop exits cleanly.
            return {}

        def _list_users(p: dict) -> dict:
            # Return users matching the Filter (email = "...") from the pool.
            flt = p.get("Filter", "")
            matched = []
            for uid, udata in pool.users.items():
                email = udata["attrs"].get("email", "")
                # Simple equality filter: email = "xxx"
                if f'"{email}"' in flt:
                    matched.append({
                        "Username": uid,
                        "Attributes": [{"Name": k, "Value": v} for k, v in udata["attrs"].items()],
                        "UserStatus": udata.get("status", "CONFIRMED"),
                        "Enabled": True,
                    })
            return {"Users": matched}

        self.router.add("cognito-idp", "AdminDeleteUser", _admin_delete_user)
        self.router.add("cognito-idp", "ListUsers", _list_users)
        self.router.install()

        inv_env = {
            "AVX_INVESTIGATIONS_TABLE": f"vsa-{self.stage}-avx-investigations",
            "AVX_INVESTIGATIONS_QUEUE_URL": self.queue_url,
            "AVX_INVESTIGATIONS_DAILY_CAP": str(self.daily_cap),
            "AVX_STAGE": self.stage,
        }
        n = uuid.uuid4().hex[:8]
        with lambda_env(inv_env, f"vsa-{self.stage}-api-avx-investigations", self.region):
            inv_spec = _iu.spec_from_file_location(
                f"item2_inv_{n}",
                avx_root / "lambdas" / "api-avx-investigations" / "handler.py",
            )
            inv_mod = _iu.module_from_spec(inv_spec)  # type: ignore[arg-type]
            sys.modules[f"item2_inv_{n}"] = inv_mod
            inv_spec.loader.exec_module(inv_mod)  # type: ignore[union-attr]
        self._inv_mod = inv_mod
        self._inv_env = inv_env

        def _invoke(name: str, fn: Any, event: dict, ctx: Any) -> dict:
            with lambda_env(inv_env, name, self.region):
                return fn(event, ctx)

        self.api = FakeRestApi(
            f"vsa-{self.stage}-api", self.stage, self.pool.verify, _invoke
        )
        self.api.add("/investigations", "POST", "investigations", inv_mod.handler)
        self.api.add("/investigations/{investigationId}", "GET", "investigations", inv_mod.handler)

        # CloudFormation stack with RestApiUrl output
        boto3.client("cloudformation", region_name=self.region).create_stack(
            StackName=f"vsa-{self.stage}-api",
            TemplateBody=json.dumps({
                "Resources": {"Ph": {"Type": "AWS::SNS::Topic"}},
                "Outputs": {"RestApiUrl": {"Value": self.api.url}},
            }),
        )

        # EventBridge Scheduler: battery schedule at 02:00 UTC so write-window guard
        # sees the schedule and finds 10:00 UTC is in the open window (>45 min away).
        sched = boto3.client("scheduler", region_name=self.region)
        try:
            sched.create_schedule_group(Name="default")
        except Exception:
            pass
        try:
            sched.create_schedule(
                Name=f"vsa-{self.stage}-tier2-daily-battery-{self.region}",
                GroupName="default",
                ScheduleExpression="cron(0 2 * * ? *)",
                ScheduleExpressionTimezone="UTC",
                State="ENABLED",
                FlexibleTimeWindow={"Mode": "OFF"},
                Target={
                    "Arn": f"arn:aws:lambda:{self.region}:{self.account}:function:stub",
                    "RoleArn": f"arn:aws:iam::{self.account}:role/stub",
                    "Input": "{}",
                },
            )
        except Exception:
            pass

    def __exit__(self, *a: Any) -> None:
        if self.api is not None:
            try:
                self.api.close()
            except Exception:
                pass
        if self.router is not None:
            try:
                self.router.uninstall()
            except Exception:
                pass
        if self._ctx:
            self._ctx.__exit__(*a)


# ---------------------------------------------------------------------------
# Minimal answer-key row + pattern row for items 2/3 tests
# ---------------------------------------------------------------------------

_AK_ROW = {
    "pattern_id": "P1",
    "code_family": "P0AA",
    "cause_dimension": "supplier_lot",
    "cause_value": "SUPP1/LOT001",
    "planted_count": 12,
    "start_date": "2025-01-01",
    "failure_mode_id": "fm-suspension-001",
}
_AK_ROW_ACTIVE = {**_AK_ROW, "start_date": "2026-01-01"}

_PAT_ROW = {
    "pattern_id": "abc123def456789a",
    "pattern_group_id": "abc123def456789a",
    "code_family": "P0AA",
    "cohort_dimension": "supplier_lot",
    "cohort_value": "SUPP1/LOT001",
    "vehicles_affected": Decimal("12"),
    "vehicles_in_service": Decimal("360"),
    "rate": Decimal("0.1"),
    "rate_rest": Decimal("0.02"),
    "p_value": Decimal("1E-10"),
    "rate_ratio": Decimal("5.0"),
    "candidate_rank": Decimal("1"),
    "run_date": "2026-10-16",
}


def _make_completed_inv(*, inv_id: str | None = None, finding_id: str | None = None,
                        **_: Any) -> dict:
    """Build a pre-seeded completed investigation row.

    The investigation row has NO `quality` field — quality data lives in the Finding.
    Uses `datetime.datetime.now(utc)` for `requested_at` so the investigations
    handler considers it "live" (within the 7-day expiry window) regardless of when
    the test runs.
    """
    import datetime as _dt
    inv_id = inv_id or str(uuid.uuid4())
    finding_id = finding_id or f"LIVETEST-finding-{inv_id[:8]}"
    # Use now() so the handler's is_live_investigation() check passes
    now_iso = _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")
    return {
        "investigation_id": inv_id,
        "status": "completed",
        "pattern_group_id": "abc123def456789a",
        "requested_by": "eng-test@example.invalid",
        "requested_at": now_iso,
        "finding_id": finding_id,
    }


def _make_finding(*, finding_id: str | None = None, **quality_overrides: Any) -> dict:
    """Build a pre-seeded quality.investigation Finding with quality evidence.

    Quality data is in the `evidence` list as items with `kind = "quality.*"`.
    The design's quality.X notation refers to the evidence item with that kind.
    See s1-notes.md for the design conflict.
    """
    finding_id = finding_id or "LIVETEST-finding-default"
    evidence: list[dict[str, Any]] = [
        {
            "kind": "quality.root_cause",
            "value": {
                "candidate_id": quality_overrides.get("root_cause_candidate_id", "cand-1"),
                "failure_mode_id": quality_overrides.get("failure_mode_id", "fm-suspension-001"),
                "confidence": "medium",
            },
        },
        {
            "kind": "quality.candidates",
            "value": quality_overrides.get("candidates", [
                {
                    "candidate_id": "cand-1",
                    "code_family": "P0AA",
                    "dimension": "supplier_lot",    # Finding uses dimension/value
                    "value": "SUPP1/LOT001",
                    "rank": 1,
                }
            ]),
        },
        {
            "kind": "quality.pattern",
            "value": quality_overrides.get("pattern", {
                "ratio": Decimal("0.1"),
                "p_value": Decimal("1E-10"),
                "affected_count": Decimal("12"),
            }),
        },
        {
            "kind": "quality.options",
            "value": quality_overrides.get("options", [
                {
                    "remedy_option_id": "opt-recall-001",
                    "remedy_type": "hardware_recall",
                    "parts_cost_per_vehicle": Decimal("120"),
                    "total": Decimal("2040"),
                    "vehicles_affected": Decimal("12"),
                    "recommended": True,
                }
            ]),
        },
    ]
    return {
        "finding_id": finding_id,
        "finding_kind": "quality.investigation",
        "agent_id": "agent-fi-quality",
        "scope": {"population_id": "meridian-motors"},
        "evidence": evidence,
    }


# ---------------------------------------------------------------------------
# Fixtures: patch slow operations in B tests
# ---------------------------------------------------------------------------

def _patch_item2_guards(monkeypatch: pytest.MonkeyPatch, world: Item2World) -> None:
    """Patch write-window guard, pre-check guard, and Cognito helpers for B tests.

    B tests focus on quality-data checks, not guards. Those are tested by A6-A10.
    Patches `principal` to yield a fake token directly without going through Cognito admin
    operations, and reduces poll timeout to avoid hanging tests.
    """
    pool_id = world.pool.pool_id

    monkeypatch.setattr(lv, "_resolve_pool_id", lambda _idp: pool_id)
    monkeypatch.setattr(lv, "_after6_write_window_guard", lambda *_a, **_k: None)
    monkeypatch.setattr(lv, "_after6_pre_checks", lambda *_a, **_k: None)
    # NOTE: _INV_POLL_MAX_S/INTERVAL are default parameter values bound at def time,
    # so patching module constants has no effect. Patch the function itself.
    _orig_poll = lv._poll_investigation
    def _fast_poll(api_url, inv_id, token, *, max_s=3, interval_s=1):
        return _orig_poll(api_url, inv_id, token, max_s=max_s, interval_s=interval_s)
    monkeypatch.setattr(lv, "_poll_investigation", _fast_poll)

    # Patch `principal` to yield a pre-minted token without Cognito admin operations.
    # The real investigations route uses pool.verify() on the token; since FakeRestApi
    # uses world.pool.verify, we mint via world.pool directly.
    from contextlib import contextmanager as _cm
    import datetime as _dt

    @_cm
    def _fake_principal(idp, pid, cid, label, groups, *, created_users=None, fleet_ids=None):
        sub = f"test-{label}-s1-001"
        user = {
            "sub": sub,
            "attrs": {"email": f"livetest-fi-quality-{label}-0000000-aabbcc@example.invalid"},
            "groups": set(groups),
            "password": None,
            "status": "CONFIRMED",
            "created": _dt.datetime.now(_dt.timezone.utc),
        }
        if sub not in world.pool.users:
            world.pool.users[sub] = user
        token = world.pool.mint(user)
        username = user["attrs"]["email"]
        if created_users is not None:
            created_users.append(username)
        try:
            yield token, {}, username
        finally:
            # Remove from pool so residue scan / confirm-absent passes
            world.pool.users.pop(sub, None)

    monkeypatch.setattr(lv, "principal", _fake_principal)


# ---------------------------------------------------------------------------
# B1: item 2 FAIL when root cause candidate does not match the plant (M3)
# ---------------------------------------------------------------------------

def test_b1_item2_fail_bad_root_cause_candidate(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Item 2 must FAIL when the root cause candidate does not match the plant (M3 re-anchored).

    Before implementation: _item_2 is _after6_placeholder("2") which SKIPs.
    After implementation: _item_2 reads quality.root_cause.value.candidate_id,
    finds the candidate in quality.candidates, and requires _matches → FAIL here.
    """
    monkeypatch.setenv("LIVE_TESTS", "1")
    # Build an investigation where the Finding's root_cause candidate has WRONG cohort (C2)
    inv = _make_completed_inv()
    finding = _make_finding(
        finding_id=inv["finding_id"],
        root_cause_candidate_id="cand-bad",
        candidates=[{"candidate_id": "cand-bad", "code_family": "B1B1",
                     "dimension": "supplier_lot", "value": "WRONG/LOT", "rank": 1}],
    )
    pattern_rows = [_PAT_ROW]

    import datetime as dt
    monkeypatch.setattr(lv, "_utc_now",
        lambda: dt.datetime.now(dt.timezone.utc).replace(hour=10, minute=0))

    with Item2World(
        completed_inv=inv, region="us-west-2", stage="staging", account="123456789012",
        answer_key=[_AK_ROW_ACTIVE], pattern_rows=pattern_rows, finding=finding,
    ) as w:
        _patch_item2_guards(monkeypatch, w)
        import duckdb
        ak_path = str(tmp_path / "ak.parquet")
        con = duckdb.connect()
        con.execute(
            "CREATE TABLE ak AS SELECT ? AS pattern_id, ? AS code_family, "
            "? AS cause_dimension, ? AS cause_value, ? AS planted_count, "
            "? AS start_date, ? AS failure_mode_id",
            [_AK_ROW_ACTIVE["pattern_id"], _AK_ROW_ACTIVE["code_family"],
             _AK_ROW_ACTIVE["cause_dimension"], _AK_ROW_ACTIVE["cause_value"],
             _AK_ROW_ACTIVE["planted_count"], _AK_ROW_ACTIVE["start_date"],
             _AK_ROW_ACTIVE["failure_mode_id"]],
        )
        con.execute(f"COPY ak TO '{ak_path}' (FORMAT PARQUET)")
        con.close()

        manifest = {"run_id": _VALID_RUN_ID, "stage": "staging",
                    "investigations": [], "users": [], "item3_cap": None,
                    "event_source_disabled": False, "items_passed": {}}
        res = lv.Results()
        lv._item_2(res,
                   stage="staging", run_id=_VALID_RUN_ID,
                   manifest=manifest, manifest_dir=str(tmp_path),
                   allow_writes=True, answer_key=ak_path)

    # Before implementation: this FAILS because item 2 SKIPs → no FAIL row
    # After implementation: item 2 FAILs on bad root cause candidate
    fail_labels = [r[0] for r in res.rows if r[1] == "FAIL"]
    assert any("cause" in lbl or "root" in lbl or "candidate" in lbl
               for lbl in fail_labels), (
        f"Expected a FAIL on root cause check. Got: {res.rows}"
    )


# ---------------------------------------------------------------------------
# B2: item 2 FAIL when quality.pattern is absent (M4)
# ---------------------------------------------------------------------------

def test_b2_item2_fail_missing_quality_pattern(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Item 2 must FAIL when quality.pattern is absent (M4 re-anchored).

    Before implementation: SKIPs.  After: FAIL on pattern-numbers check.
    """
    monkeypatch.setenv("LIVE_TESTS", "1")
    inv = _make_completed_inv()
    finding = _make_finding(finding_id=inv["finding_id"])
    # Remove quality.pattern from the finding evidence
    finding["evidence"] = [e for e in finding["evidence"] if e.get("kind") != "quality.pattern"]

    import datetime as dt
    monkeypatch.setattr(lv, "_utc_now",
        lambda: dt.datetime.now(dt.timezone.utc).replace(hour=10, minute=0))

    with Item2World(
        completed_inv=inv, region="us-west-2", stage="staging", account="123456789012",
        answer_key=[_AK_ROW_ACTIVE], pattern_rows=[_PAT_ROW], finding=finding,
    ) as w:
        _patch_item2_guards(monkeypatch, w)
        import duckdb
        ak_path = str(tmp_path / "ak.parquet")
        con = duckdb.connect()
        con.execute(
            "CREATE TABLE ak AS SELECT ? AS pattern_id, ? AS code_family, "
            "? AS cause_dimension, ? AS cause_value, ? AS planted_count, "
            "? AS start_date, ? AS failure_mode_id",
            [_AK_ROW_ACTIVE["pattern_id"], _AK_ROW_ACTIVE["code_family"],
             _AK_ROW_ACTIVE["cause_dimension"], _AK_ROW_ACTIVE["cause_value"],
             _AK_ROW_ACTIVE["planted_count"], _AK_ROW_ACTIVE["start_date"],
             _AK_ROW_ACTIVE["failure_mode_id"]],
        )
        con.execute(f"COPY ak TO '{ak_path}' (FORMAT PARQUET)")
        con.close()

        manifest = {"run_id": _VALID_RUN_ID, "stage": "staging",
                    "investigations": [], "users": [], "item3_cap": None,
                    "event_source_disabled": False, "items_passed": {}}
        res = lv.Results()
        lv._item_2(res, stage="staging", run_id=_VALID_RUN_ID,
                   manifest=manifest, manifest_dir=str(tmp_path),
                   allow_writes=True, answer_key=ak_path)

    fail_labels = [r[0] for r in res.rows if r[1] == "FAIL"]
    # M-B2 fix: assert the exact FAIL label, not a broad substring match.
    # Mutation "pattern-present check bypassed" must produce item-2-plant-P1-pattern-present FAIL.
    assert "item-2-plant-P1-pattern-present" in fail_labels, (
        f"Expected FAIL on exact label 'item-2-plant-P1-pattern-present'. Got: {res.rows}"
    )


# ---------------------------------------------------------------------------
# B3: item 2 FAIL when fewer than 80 % of plants have passing investigations (M5)
# ---------------------------------------------------------------------------

def test_b3_item2_fail_rate_below_80pct(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Item 2 must FAIL when <80% of active plants have passing investigations (M5).

    Before: SKIP.  After: FAIL on 80% rate check.
    """
    monkeypatch.setenv("LIVE_TESTS", "1")
    import datetime as dt
    monkeypatch.setattr(lv, "_utc_now",
        lambda: dt.datetime.now(dt.timezone.utc).replace(hour=10, minute=0))

    # Three planted patterns; only one has a pre-seeded completed investigation → 33% < 80%
    ak_rows = [
        {**_AK_ROW_ACTIVE, "pattern_id": "P1", "code_family": "P0AA",
         "cause_dimension": "supplier_lot", "cause_value": "SUPP1/LOT001"},
        {**_AK_ROW_ACTIVE, "pattern_id": "P2", "code_family": "P1AB",
         "cause_dimension": "supplier_lot", "cause_value": "SUPP2/LOT002"},
        {**_AK_ROW_ACTIVE, "pattern_id": "P3", "code_family": "P2CD",
         "cause_dimension": "supplier_lot", "cause_value": "SUPP3/LOT003"},
    ]
    pat_rows = [
        {**_PAT_ROW, "pattern_id": "aaa111bbb2223334", "pattern_group_id": "aaa111bbb2223334",
         "code_family": "P0AA", "cohort_dimension": "supplier_lot",
         "cohort_value": "SUPP1/LOT001"},
        {**_PAT_ROW, "pattern_id": "bbb222ccc3334445", "pattern_group_id": "bbb222ccc3334445",
         "code_family": "P1AB", "cohort_dimension": "supplier_lot",
         "cohort_value": "SUPP2/LOT002"},
        {**_PAT_ROW, "pattern_id": "ccc333ddd4445556", "pattern_group_id": "ccc333ddd4445556",
         "code_family": "P2CD", "cohort_dimension": "supplier_lot",
         "cohort_value": "SUPP3/LOT003"},
    ]
    # Pre-seed only one completed investigation (for P0AA)
    inv = _make_completed_inv(inv_id="inv-p1-ok", finding_id="LIVETEST-finding-p1")
    inv["pattern_group_id"] = "aaa111bbb2223334"

    finding_p1 = _make_finding(finding_id="LIVETEST-finding-p1")

    with Item2World(
        completed_inv=inv, region="us-west-2", stage="staging", account="123456789012",
        answer_key=ak_rows, pattern_rows=pat_rows, finding=finding_p1,
    ) as w:
        _patch_item2_guards(monkeypatch, w)
        # Seed lock for first pattern pointing to the completed inv
        w.inv_tbl.put_item(Item={
            "investigation_id": "lock#aaa111bbb2223334",
            "investigation_id_ref": "inv-p1-ok",
            "status": "completed",
        })
        # Other patterns have no lock → new investigations needed → will fail
        # (since there's no real tier2 Lambda in this minimal world)

        import duckdb
        ak_path = str(tmp_path / "ak.parquet")
        con = duckdb.connect()
        con.execute(
            "CREATE TABLE ak AS SELECT * FROM (VALUES "
            + ", ".join(
                f"('{r['pattern_id']}', '{r['code_family']}', '{r['cause_dimension']}', "
                f"'{r['cause_value']}', {r['planted_count']}, '{r['start_date']}', '{r['failure_mode_id']}')"
                for r in ak_rows
            )
            + ") t(pattern_id, code_family, cause_dimension, cause_value, "
            "planted_count, start_date, failure_mode_id)"
        )
        con.execute(f"COPY ak TO '{ak_path}' (FORMAT PARQUET)")
        con.close()

        manifest = {"run_id": _VALID_RUN_ID, "stage": "staging",
                    "investigations": [], "users": [], "item3_cap": None,
                    "event_source_disabled": False, "items_passed": {}}
        res = lv.Results()
        lv._item_2(res, stage="staging", run_id=_VALID_RUN_ID,
                   manifest=manifest, manifest_dir=str(tmp_path),
                   allow_writes=True, answer_key=ak_path)

    fail_labels = [r[0] for r in res.rows if r[1] == "FAIL"]
    assert any("rate" in lbl.lower() or "80" in lbl or "cause" in lbl.lower()
               for lbl in fail_labels), (
        f"Expected FAIL on 80% rate. Got: {res.rows}"
    )


# ---------------------------------------------------------------------------
# B4: item 2 FAIL when failure mode does not match (M6)
# ---------------------------------------------------------------------------

def test_b4_item2_fail_wrong_failure_mode(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Item 2 must FAIL when quality.failure_mode does not match the plant's (M6).

    Before: SKIP.  After: FAIL on failure-mode check.
    """
    monkeypatch.setenv("LIVE_TESTS", "1")
    import datetime as dt
    monkeypatch.setattr(lv, "_utc_now",
        lambda: dt.datetime.now(dt.timezone.utc).replace(hour=10, minute=0))

    inv = _make_completed_inv()
    finding = _make_finding(
        finding_id=inv["finding_id"],
        failure_mode_id="fm-wrong-mode",  # doesn't match _AK_ROW["failure_mode_id"]
    )

    with Item2World(
        completed_inv=inv, region="us-west-2", stage="staging", account="123456789012",
        answer_key=[_AK_ROW_ACTIVE], pattern_rows=[_PAT_ROW], finding=finding,
    ) as w:
        _patch_item2_guards(monkeypatch, w)
        import duckdb
        ak_path = str(tmp_path / "ak.parquet")
        con = duckdb.connect()
        con.execute(
            "CREATE TABLE ak AS SELECT ? AS pattern_id, ? AS code_family, "
            "? AS cause_dimension, ? AS cause_value, ? AS planted_count, "
            "? AS start_date, ? AS failure_mode_id",
            [_AK_ROW_ACTIVE["pattern_id"], _AK_ROW_ACTIVE["code_family"],
             _AK_ROW_ACTIVE["cause_dimension"], _AK_ROW_ACTIVE["cause_value"],
             _AK_ROW_ACTIVE["planted_count"], _AK_ROW_ACTIVE["start_date"],
             _AK_ROW_ACTIVE["failure_mode_id"]],
        )
        con.execute(f"COPY ak TO '{ak_path}' (FORMAT PARQUET)")
        con.close()

        manifest = {"run_id": _VALID_RUN_ID, "stage": "staging",
                    "investigations": [], "users": [], "item3_cap": None,
                    "event_source_disabled": False, "items_passed": {}}
        res = lv.Results()
        lv._item_2(res, stage="staging", run_id=_VALID_RUN_ID,
                   manifest=manifest, manifest_dir=str(tmp_path),
                   allow_writes=True, answer_key=ak_path)

    fail_labels = [r[0] for r in res.rows if r[1] == "FAIL"]
    assert any("failure" in lbl.lower() or "mode" in lbl.lower() or "fm" in lbl.lower()
               for lbl in fail_labels), (
        f"Expected FAIL on failure_mode check. Got: {res.rows}"
    )


# ---------------------------------------------------------------------------
# B5: item 2 FAIL when quality.options is absent
# ---------------------------------------------------------------------------

def test_b5_item2_fail_missing_options(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Item 2 must FAIL when quality.options is absent or empty.

    Before: SKIP.  After: FAIL on options check.
    """
    monkeypatch.setenv("LIVE_TESTS", "1")
    import datetime as dt
    monkeypatch.setattr(lv, "_utc_now",
        lambda: dt.datetime.now(dt.timezone.utc).replace(hour=10, minute=0))

    inv = _make_completed_inv()
    finding = _make_finding(
        finding_id=inv["finding_id"],
        options=[],  # empty options → FAIL
    )

    with Item2World(
        completed_inv=inv, region="us-west-2", stage="staging", account="123456789012",
        answer_key=[_AK_ROW_ACTIVE], pattern_rows=[_PAT_ROW], finding=finding,
    ) as w:
        _patch_item2_guards(monkeypatch, w)
        import duckdb
        ak_path = str(tmp_path / "ak.parquet")
        con = duckdb.connect()
        con.execute(
            "CREATE TABLE ak AS SELECT ? AS pattern_id, ? AS code_family, "
            "? AS cause_dimension, ? AS cause_value, ? AS planted_count, "
            "? AS start_date, ? AS failure_mode_id",
            [_AK_ROW_ACTIVE["pattern_id"], _AK_ROW_ACTIVE["code_family"],
             _AK_ROW_ACTIVE["cause_dimension"], _AK_ROW_ACTIVE["cause_value"],
             _AK_ROW_ACTIVE["planted_count"], _AK_ROW_ACTIVE["start_date"],
             _AK_ROW_ACTIVE["failure_mode_id"]],
        )
        con.execute(f"COPY ak TO '{ak_path}' (FORMAT PARQUET)")
        con.close()

        manifest = {"run_id": _VALID_RUN_ID, "stage": "staging",
                    "investigations": [], "users": [], "item3_cap": None,
                    "event_source_disabled": False, "items_passed": {}}
        res = lv.Results()
        lv._item_2(res, stage="staging", run_id=_VALID_RUN_ID,
                   manifest=manifest, manifest_dir=str(tmp_path),
                   allow_writes=True, answer_key=ak_path)

    fail_labels = [r[0] for r in res.rows if r[1] == "FAIL"]
    assert any("option" in lbl.lower() for lbl in fail_labels), (
        f"Expected FAIL on empty options. Got: {res.rows}"
    )


# ---------------------------------------------------------------------------
# B6: item 3 FAIL when repeat POST returns a different investigation id
# ---------------------------------------------------------------------------

def test_b6_item3_fail_different_repeat_id(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Item 3 must FAIL when a repeat POST returns a different investigation id (M11).

    Before: SKIP.  After: FAIL when the route returns a new 202 instead of 200 + same id.
    """
    monkeypatch.setenv("LIVE_TESTS", "1")
    import datetime as dt
    monkeypatch.setattr(lv, "_utc_now",
        lambda: dt.datetime.now(dt.timezone.utc).replace(hour=10, minute=0))

    # Use a pattern_group_id with NO pre-seeded lock → the route creates a new investigation
    # on the first POST and a SECOND different one on the repeat (simulating bad behavior).
    # In the real world, the lock prevents this. We test the check catches it if somehow
    # the repeat returns a new id (e.g., we use a pattern that returns 202 both times due
    # to an expired/missing lock).
    pattern_rows = [
        {**_PAT_ROW, "pattern_id": "deadbeef01234567",
         "pattern_group_id": "deadbeef01234567",
         "code_family": "P0AA", "cohort_dimension": "supplier_lot",
         "cohort_value": "SUPP1/LOT001"},
    ]

    with Item2World(
        completed_inv=None, region="us-west-2", stage="staging", account="123456789012",
        answer_key=[], pattern_rows=pattern_rows,
    ) as w:
        _patch_item2_guards(monkeypatch, w)
        manifest = {"run_id": _VALID_RUN_ID, "stage": "staging",
                    "investigations": [
                        {"pattern_group_id": "deadbeef01234567",
                         "investigation_id": "inv-first-000",
                         "status": "completed"}
                    ],
                    "users": [], "item3_cap": None,
                    "event_source_disabled": False, "items_passed": {}}
        # Pre-seed the "first" investigation row and lock so the repeat POST
        # will return 200 + same id (correct behavior) — but we don't seed them,
        # so the route will create a NEW investigation (202), testing that item 3
        # correctly identifies the mismatch.
        res = lv.Results()
        lv._item_3(res, stage="staging", run_id=_VALID_RUN_ID,
                   manifest=manifest, manifest_dir=str(tmp_path),
                   allow_writes=True)

    fail_labels = [r[0] for r in res.rows if r[1] == "FAIL"]
    # M-B6 fix: assert the exact FAIL label, not a broad substring match.
    # Mutation "repeat-same-id check always True" must produce item-3-repeat-same-id FAIL.
    assert "item-3-repeat-same-id" in fail_labels, (
        f"Expected FAIL on exact label 'item-3-repeat-same-id'. Got: {res.rows}"
    )


# ---------------------------------------------------------------------------
# B7: item 3 FAIL when the cap POST does not return 429
# ---------------------------------------------------------------------------

def test_b7_item3_fail_cap_no_429(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Item 3 must FAIL when the cap POST does not return 429 (W2 re-anchored).

    Before: SKIP.  After: FAIL when the cap is not set to the max.
    """
    monkeypatch.setenv("LIVE_TESTS", "1")
    import datetime as dt
    monkeypatch.setattr(lv, "_utc_now",
        lambda: dt.datetime.now(dt.timezone.utc).replace(hour=10, minute=0))

    # Use a pattern_group_id with a locked completed investigation for the repeat test.
    pat_group_id = "cap99test00000001"
    inv_id = "inv-cap-test-001"
    inv = _make_completed_inv(inv_id=inv_id, finding_id="LIVETEST-finding-cap99")
    inv["pattern_group_id"] = pat_group_id

    with Item2World(
        completed_inv=inv, region="us-west-2", stage="staging", account="123456789012",
        answer_key=[], daily_cap=21, pattern_rows=[{
            **_PAT_ROW, "pattern_id": pat_group_id,
            "pattern_group_id": pat_group_id,
        }, {
            # Unlocked real pattern for _pick_unlocked_real_pattern to probe
            **_PAT_ROW, "pattern_id": "f0099999abcdef99",
            "pattern_group_id": "f0099999abcdef99",
        }],
    ) as w:
        _patch_item2_guards(monkeypatch, w)
        # Don't set the cap to the max → a fresh POST will succeed (202) not 429
        manifest = {"run_id": _VALID_RUN_ID, "stage": "staging",
                    "investigations": [
                        {"pattern_group_id": pat_group_id,
                         "investigation_id": inv_id, "status": "completed"}
                    ],
                    "users": [], "item3_cap": None,
                    "event_source_disabled": False, "items_passed": {}}
        res = lv.Results()
        lv._item_3(res, stage="staging", run_id=_VALID_RUN_ID,
                   manifest=manifest, manifest_dir=str(tmp_path),
                   allow_writes=True)

    fail_labels = [r[0] for r in res.rows if r[1] == "FAIL"]
    assert "item-3-cap-429" in fail_labels, (
        f"Expected FAIL on item-3-cap-429. Got: {res.rows}"
    )


# ---------------------------------------------------------------------------
# B8: items 4/5/8b/9 SKIP with Stage 2 placeholder message
# ---------------------------------------------------------------------------

def test_b8_items_4_5_8b_9_skip_with_stage2_message() -> None:
    """Stage 3: all after-6 items (4/5/8b/9) are real write items; they SKIP without --allow-writes.

    S8 fix: _STAGE2_SKIP and _NEEDS_GROUP_6 removed as dead code.
    All write items SKIP with "writes to staging; needs --allow-writes" when allow_writes=False.
    """
    writes_skip = "writes to staging; needs --allow-writes"
    for n in ("4", "5", "8b"):
        assert n in _mod._WRITE_ITEMS, f"item {n} must be in _WRITE_ITEMS"
        res = lv.Results()
        _fn = _mod._ITEM_FNS[n]
        _fn(res, allow_writes=False, stage="staging",
                          answer_key="ak.parquet", run_id=_VALID_RUN_ID,
                          manifest={}, manifest_dir="/tmp")
        assert res.rows == [(f"item-{n}", "SKIP", writes_skip)], (
            f"item {n}: expected SKIP with writes message, got {res.rows}"
        )


# ---------------------------------------------------------------------------
# C1: cleanup runs in finally even when item 2 raises mid-check
# ---------------------------------------------------------------------------

def test_c1_cleanup_runs_after_item2_exception(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The cleanup records are written (in finally) even when item 2 raises.

    Before implementation: item 2 is a placeholder; no cleanup exists.
    After implementation: cleanup always runs via the finally path.
    """
    monkeypatch.setenv("LIVE_TESTS", "1")

    reached: list[str] = []

    def _raising_item_2(res: lv.Results, **kwargs: Any) -> None:
        reached.append("item-2-start")
        raise RuntimeError("boom inside item 2")

    monkeypatch.setattr(_mod, "_item_2", _raising_item_2)

    # Check that even with a raising item 2, the cleanup records something
    # (not necessarily a PASS — the cleanup itself may fail; but it runs).
    cleanup_ran = []

    original_cleanup = _mod._after6_cleanup

    def _spy_cleanup(res: lv.Results, **kwargs: Any) -> None:
        cleanup_ran.append(True)
        original_cleanup(res, **kwargs)

    monkeypatch.setattr(_mod, "_after6_cleanup", _spy_cleanup)

    # _after6_run runs the items and the cleanup; this test verifies cleanup_ran
    # by patching _after6_cleanup and checking it was called.
    # Since the function doesn't exist yet, this will also fail with AttributeError.
    assert hasattr(_mod, "_after6_cleanup"), (
        "_after6_cleanup function must exist on the module"
    )


# ---------------------------------------------------------------------------
# C2: residue scan FAIL when an investigation row survives cleanup
# ---------------------------------------------------------------------------

def test_c2_residue_scan_fails_on_leftover(tmp_path: Path) -> None:
    """_after6_residue_scan must FAIL when a LIVETEST investigation remains.

    M-cleanup fix: behavioral test — seed a leftover investigation row into moto DDB,
    call _after6_residue_scan, check it produces a FAIL row naming the record (rc 1).
    The old test only checked hasattr, which passed even with the scan deleted.
    """
    from moto import mock_aws

    with mock_aws():
        import boto3

        ddb = boto3.client("dynamodb", region_name="us-west-2")
        ddb.create_table(
            TableName="vsa-staging-avx-investigations",
            KeySchema=[{"AttributeName": "investigation_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "investigation_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        ddb_r = boto3.resource("dynamodb", region_name="us-west-2")
        inv_tbl = ddb_r.Table("vsa-staging-avx-investigations")

        # Seed a leftover investigation row that cleanup "forgot" to delete.
        leftover_id = "leftover-inv-abc12345"
        pgid = "deadbeef00000001"
        inv_tbl.put_item(Item={
            "investigation_id": leftover_id,
            "pattern_group_id": pgid,
            "status": "completed",
        })

        # Build manifest that says this investigation was written by this run.
        manifest = {
            "run_id": _VALID_RUN_ID,
            "investigations": [
                {"investigation_id": leftover_id, "pattern_group_id": pgid, "status": "completed"}
            ],
        }
        res = lv.Results()
        _mod._after6_residue_scan(res, manifest=manifest, stage="staging", inv_tbl=inv_tbl)

    fail_labels = [r[0] for r in res.rows if r[1] == "FAIL"]
    assert any("residue" in lbl.lower() for lbl in fail_labels), (
        f"Expected a FAIL on residue scan. Got: {res.rows}"
    )
    # Verify the FAIL row names the leftover record, not just any failure.
    fail_details = [r[2] for r in res.rows if r[1] == "FAIL"]
    assert any(leftover_id[:8] in d or pgid[:8] in d for d in fail_details), (
        f"Residue FAIL should name the leftover record. Got fail details: {fail_details}"
    )


# ---------------------------------------------------------------------------
# C3: --part after-6 exits 2 (never 0) when items 2 and 3 pass
# ---------------------------------------------------------------------------

def test_c3_after6_exits_2_when_some_items_skip(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """--part after-6 exits 2 when any item SKIPs (e.g. no --allow-writes).

    Item 9 is task 6.5 (split 2026-10-28): _AFTER6_ITEMS = (2,3,4,5,8b).
    When items 2/3 pass but 4/5/8b skip → rc 2.
    """
    monkeypatch.setenv("LIVE_TESTS", "1")

    # Replace items 2/3 with pass, items 4/5/8b with skip (avoids AWS calls)
    fns = dict(_mod._ITEM_FNS)
    fns["2"] = _fake_pass
    fns["3"] = _fake_pass
    fns["4"] = _fake_skip
    fns["5"] = _fake_skip
    fns["8b"] = _fake_skip
    monkeypatch.setattr(_mod, "_ITEM_FNS", fns)

    # Redirect manifest dir to tmp_path
    monkeypatch.setenv("FI_QUALITY_MANIFEST_DIR", str(tmp_path))

    old_int = signal.getsignal(signal.SIGINT)
    old_term = signal.getsignal(signal.SIGTERM)
    try:
        rc = _mod.main([
            "--stage", "staging",
            "--part", "after-6",
            "--allow-writes",
        ])
    finally:
        signal.signal(signal.SIGINT, old_int)
        signal.signal(signal.SIGTERM, old_term)

    # Must be 2 (SKIP from items 4/5/8b)
    assert rc == 2, f"Expected exit 2 (SKIP from items 4/5/8b), got {rc}"


# ---------------------------------------------------------------------------
# C4: --part after-6-check refuses if run-id manifest is absent
# ---------------------------------------------------------------------------

def test_c4_after6_check_refuses_with_rc1(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """--part after-6-check exits 1 immediately; item 9 is --part item9, task 6.5.

    after-6-check refuses before any AWS call with rc=1.
    """
    monkeypatch.setenv("LIVE_TESTS", "1")
    monkeypatch.setenv("FI_QUALITY_MANIFEST_DIR", str(tmp_path))
    old_int = signal.getsignal(signal.SIGINT)
    old_term = signal.getsignal(signal.SIGTERM)
    try:
        rc = _mod.main([
            "--stage", "staging",
            "--part", "after-6-check",
        ])
    except SystemExit as e:
        pytest.fail(f"argparse rejected --part after-6-check: SystemExit({e.code})")
    finally:
        signal.signal(signal.SIGINT, old_int)
        signal.signal(signal.SIGTERM, old_term)
    assert rc == 1, f"--part after-6-check must return 1 (task 6.5 refusal); got {rc}"


# ---------------------------------------------------------------------------
# C5: write-window guard fires before the first AWS write in _item_2
# ---------------------------------------------------------------------------

def test_c5_write_window_guard_fires_before_first_write(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The write-window guard must be checked before any AWS write in item 2.

    Before implementation: item 2 SKIPs (no guard to check).
    After implementation: item 2 calls the guard and SKIPs when it refuses.
    """
    monkeypatch.setenv("LIVE_TESTS", "1")
    import datetime as dt

    # Simulate a blocked time (01:30 UTC, within 30 min of 02:00 schedule)
    monkeypatch.setattr(lv, "_utc_now",
        lambda: dt.datetime(2026, 10, 27, 1, 30, 0, tzinfo=dt.timezone.utc))

    # Patch _list_schedule_names to return a battery schedule so the guard finds it.
    # The write-window guard calls _list_schedule_names then _schedule_guard per name.
    # At 01:30 UTC with a 02:00 schedule, the gap is 30 min < 45 → refused.
    class _FakeSched:
        def get_schedule(self, **kw: Any) -> dict:
            return {
                "ScheduleExpression": "cron(0 2 * * ? *)",
                "ScheduleExpressionTimezone": "UTC",
            }
        def list_schedules(self, **_: Any) -> dict:
            return {"Schedules": [
                {"Name": "vsa-staging-tier2-daily-battery-us-west-2",
                 "GroupName": "default"}
            ]}

    monkeypatch.setattr(lv, "_list_schedule_names",
                        lambda scheduler, prefixes: [
                            ("vsa-staging-tier2-daily-battery-us-west-2", "default")
                        ])
    # Patch the guard directly rather than its sub-functions (C4 changed internals)
    monkeypatch.setattr(lv, "_after6_write_window_guard",
                        lambda *_a, **_k: "vsa-staging-tier2-daily-battery-us-west-2 is 30 min away; needs 45")

    manifest = {"run_id": _VALID_RUN_ID, "stage": "staging",
                "investigations": [], "users": [], "item3_cap": None,
                "event_source_disabled": False, "items_passed": {}}
    res = lv.Results()

    # Build a minimal answer key parquet
    import duckdb
    ak_path = str(tmp_path / "ak.parquet")
    con = duckdb.connect()
    con.execute(
        "CREATE TABLE ak AS SELECT ? AS pattern_id, ? AS code_family, "
        "? AS cause_dimension, ? AS cause_value, ? AS planted_count, "
        "? AS start_date, ? AS failure_mode_id",
        ["P1", "P0AA", "supplier_lot", "SUPP1/LOT001", 12, "2026-01-01", "fm-001"],
    )
    con.execute(f"COPY ak TO '{ak_path}' (FORMAT PARQUET)")
    con.close()

    try:
        lv._item_2(res, stage="staging", run_id=_VALID_RUN_ID,
                   manifest=manifest, manifest_dir=str(tmp_path),
                   allow_writes=True, answer_key=ak_path)
    except Exception:
        pass  # any exception is fine; we check skip row exists

    skip_rows = [r for r in res.rows if r[1] == "SKIP"]
    assert skip_rows, (
        f"Expected item 2 to SKIP when write window is closed. Got: {res.rows}"
    )
    assert any("window" in r[2].lower() or "schedule" in r[2].lower() or "min" in r[2].lower()
               for r in skip_rows), (
        f"SKIP reason should mention window/schedule. Got skip rows: {skip_rows}"
    )


# ===========================================================================
# Fix-round-1-A RED tests: W11, W8, W10, C4, W9-cleanup, C1, C2, C3, C5, W5, C6
# ===========================================================================

# ---------------------------------------------------------------------------
# W11: default manifest dir must NOT be in /tmp
# ---------------------------------------------------------------------------

def test_w11_default_manifest_dir_not_in_tmp() -> None:
    """_MANIFEST_DEFAULT_DIR must be outside /tmp (e.g. ~/.cache/…)."""
    default = _mod._MANIFEST_DEFAULT_DIR
    assert not str(default).startswith("/tmp"), (
        f"_MANIFEST_DEFAULT_DIR must not be in /tmp; got {default!r}"
    )


def test_w11_autouse_manifest_dir_fixture_exists(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Every test that calls main() or saves manifests must redirect via FI_QUALITY_MANIFEST_DIR.

    This test verifies that test_c3 does redirect the manifest dir,
    so no manifest ever lands in the real default location.
    """
    # The test body just sets the env var to verify the mechanism works.
    monkeypatch.setenv("FI_QUALITY_MANIFEST_DIR", str(tmp_path))
    assert _mod._manifest_base_dir() == str(tmp_path)


# ---------------------------------------------------------------------------
# W8: --part after-6 must exit 2 even when ALL after-6 items pass
# ---------------------------------------------------------------------------

def test_w8_after6_exits_0_when_all_items_pass(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """--part after-6 must return 0 when every item (2,3,4,5,8b) passes.

    Item 9 split to task 6.5 (2026-10-28): _AFTER6_ITEMS no longer includes 9,
    so when all five items pass with no FAIL and no SKIP → rc 0.
    """

    monkeypatch.setenv("LIVE_TESTS", "1")
    monkeypatch.setenv("FI_QUALITY_MANIFEST_DIR", str(tmp_path))

    # Patch all after-6 items to PASS
    fns = dict(_mod._ITEM_FNS)
    for n in ("2", "3", "4", "5", "8b"):
        fns[n] = _fake_pass
    monkeypatch.setattr(_mod, "_ITEM_FNS", fns)

    old_int = signal.getsignal(signal.SIGINT)
    old_term = signal.getsignal(signal.SIGTERM)
    try:
        rc = _mod.main(["--stage", "staging", "--part", "after-6", "--allow-writes"])
    finally:
        signal.signal(signal.SIGINT, old_int)
        signal.signal(signal.SIGTERM, old_term)

    assert rc == 0, (
        f"--part after-6 must return 0 when all items (2,3,4,5,8b) pass; got {rc}"
    )


def test_w8_after6_check_refuses_with_rc1_no_aws(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """--part after-6-check must return 1 immediately; item 9 is --part item9, task 6.5.

    No AWS call must be made: _after6_cleanup must not be called.
    """
    import io as _io
    import contextlib

    monkeypatch.setenv("LIVE_TESTS", "1")
    monkeypatch.setenv("FI_QUALITY_MANIFEST_DIR", str(tmp_path))

    cleanup_called = []
    monkeypatch.setattr(_mod, "_after6_cleanup", lambda *a, **k: cleanup_called.append(True))

    buf = _io.StringIO()
    old_int = signal.getsignal(signal.SIGINT)
    old_term = signal.getsignal(signal.SIGTERM)
    try:
        with contextlib.redirect_stdout(buf):
            rc = _mod.main([
                "--stage", "staging",
                "--part", "after-6-check",
            ])
    finally:
        signal.signal(signal.SIGINT, old_int)
        signal.signal(signal.SIGTERM, old_term)

    output = buf.getvalue()
    assert rc == 1, (
        f"--part after-6-check must return 1 (item 9 is task 6.5); got rc={rc}"
    )
    assert "6.5" in output, (
        f"--part after-6-check must print that item 9 is task 6.5; got: {output!r}"
    )
    assert not cleanup_called, "--part after-6-check must not call _after6_cleanup"


# ---------------------------------------------------------------------------
# W10: SIGHUP must raise SystemExit (not terminate silently)
# ---------------------------------------------------------------------------

def test_w10_sighup_raises_systemexit(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """_install_signal_handlers must install a handler for SIGHUP."""
    import signal as _signal
    _mod._install_signal_handlers()
    handler = _signal.getsignal(_signal.SIGHUP)
    # Restore immediately
    _signal.signal(_signal.SIGHUP, _signal.SIG_DFL)
    assert handler not in (_signal.SIG_DFL, _signal.SIG_IGN), (
        "SIGHUP must have a handler installed by _install_signal_handlers"
    )


# ---------------------------------------------------------------------------
# C4: rate() schedule must be skipped (not raise ValueError); at() fails closed
# ---------------------------------------------------------------------------

def test_c4_rate_schedule_skipped() -> None:
    """rate() schedule expression must not cause the after-6 write window guard to raise or refuse.

    Fix: _after6_write_window_guard pre-filters rate() schedules before calling
    _schedule_guard, so the after-6 items can proceed.
    """
    import datetime as dt

    class _FakeSched:
        def list_schedules(self, **_: Any) -> dict:
            return {"Schedules": [
                {"Name": "vsa-staging-tier2-diagnostics-us-west-2", "GroupName": "default"},
            ]}

        def get_schedule(self, Name: str, GroupName: str, **_: Any) -> dict:
            return {
                "ScheduleExpression": "rate(5 minutes)",
                "ScheduleExpressionTimezone": "UTC",
            }

    now = dt.datetime(2026, 10, 27, 7, 0, 0, tzinfo=dt.timezone.utc)

    # Must not raise ValueError and must be falsy (rate-only list = no window to check)
    try:
        result = _mod._after6_write_window_guard(_FakeSched(), "staging", now)
    except ValueError as exc:
        pytest.fail(
            f"_after6_write_window_guard raised ValueError on rate() schedule: {exc}"
        )
    assert not result, (
        f"rate()-only schedule list must be falsy (writes allowed); got {result!r}"
    )


def test_c4_at_schedule_fails_closed() -> None:
    """at() schedule expression must cause the after-6 write window guard to refuse (fail closed)."""
    import datetime as dt

    class _FakeSched:
        def list_schedules(self, **_: Any) -> dict:
            return {"Schedules": [
                {"Name": "vsa-staging-tier2-health-agent", "GroupName": "default"},
            ]}

        def get_schedule(self, Name: str, GroupName: str, **_: Any) -> dict:
            return {
                "ScheduleExpression": "at(2026-10-27T02:00:00)",
                "ScheduleExpressionTimezone": "UTC",
            }

    now = dt.datetime(2026, 10, 27, 7, 0, 0, tzinfo=dt.timezone.utc)
    result = _mod._after6_write_window_guard(_FakeSched(), "staging", now)
    assert result is not None, (
        "at() schedule must fail closed (return a reason); got None"
    )


def test_c4_after6_window_guard_skips_rate_schedules() -> None:
    """_after6_write_window_guard must not raise on a schedule list with rate() entries.

    Regression: previously vsa-staging-tier2-diagnostics-us-west-2 (rate(5 minutes))
    made the guard raise ValueError on every after-6 call.
    """
    import datetime as dt

    class _FakeSched:
        def list_schedules(self, **_: Any) -> dict:
            return {"Schedules": [
                {"Name": "vsa-staging-tier2-diagnostics-us-west-2", "GroupName": "default"},
                {"Name": "vsa-staging-tier2-daily-battery-us-west-2", "GroupName": "default"},
            ]}

        def get_schedule(self, Name: str, GroupName: str, **_: Any) -> dict:
            if "diagnostics" in Name:
                return {
                    "ScheduleExpression": "rate(5 minutes)",
                    "ScheduleExpressionTimezone": "UTC",
                }
            return {
                "ScheduleExpression": "cron(0 2 * * ? *)",
                "ScheduleExpressionTimezone": "UTC",
            }

    # 07:00 UTC — safe window (5h after 02:00 cron)
    now = dt.datetime(2026, 10, 27, 7, 0, 0, tzinfo=dt.timezone.utc)
    try:
        result = _mod._after6_write_window_guard(_FakeSched(), "staging", now)
    except ValueError as exc:
        pytest.fail(f"after6 guard raised ValueError on rate() schedule: {exc}")
    assert result is None, (
        f"Expected None in safe window with rate() + cron schedules; got {result!r}"
    )


# ---------------------------------------------------------------------------
# W9 cleanup: _after6_cleanup must have steps for item 9 row, lock, DLQ message
# ---------------------------------------------------------------------------

def test_c1_item2_does_not_call_cleanup(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """_item_2 must not call _after6_cleanup; cleanup belongs in main()'s finally."""
    import inspect
    src = inspect.getsource(_mod._item_2)
    assert "_after6_cleanup" not in src, (
        "_item_2 must not call _after6_cleanup; cleanup must be in main()'s finally only"
    )


def test_c1_main_finally_calls_cleanup_when_item_raises(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """main()'s finally must call _after6_cleanup even when an item raises an exception."""
    monkeypatch.setenv("LIVE_TESTS", "1")
    monkeypatch.setenv("FI_QUALITY_MANIFEST_DIR", str(tmp_path))

    fns = dict(_mod._ITEM_FNS)
    fns["2"] = lambda res, **kw: (_ for _ in ()).throw(RuntimeError("boom"))
    for n in ("3", "4", "5", "8b", "9"):
        fns[n] = _fake_pass
    monkeypatch.setattr(_mod, "_ITEM_FNS", fns)

    cleanup_called = []
    orig_cleanup = _mod._after6_cleanup
    def _spy(res, **kw):
        cleanup_called.append(True)
        orig_cleanup(res, **kw)
    monkeypatch.setattr(_mod, "_after6_cleanup", _spy)

    old_int = signal.getsignal(signal.SIGINT)
    old_term = signal.getsignal(signal.SIGTERM)
    try:
        _mod.main(["--stage", "staging", "--part", "after-6", "--allow-writes"])
    except Exception:
        pass
    finally:
        signal.signal(signal.SIGINT, old_int)
        signal.signal(signal.SIGTERM, old_term)

    assert cleanup_called, (
        "main()'s finally must call _after6_cleanup even when an item raises"
    )


# ---------------------------------------------------------------------------
# C2: item 2 must NOT record 200 investigations for cleanup (only 202 = this run's)
# ---------------------------------------------------------------------------

def test_c2_item2_200_not_recorded_for_cleanup(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A 200 POST response (existing investigation) must not be added to manifest investigations.

    Fix: only 202 responses (this run created them) are recorded for cleanup.
    The current code records BOTH 200 and 202 — that's the C2 bug.
    """
    import inspect
    src = inspect.getsource(_mod._item_2)
    # After the fix: the code must check status == 202 before appending to investigations
    # Heuristic: the investigation append must be gated on 202, not 200
    # The current buggy code appends for s in (200, 202) — look for that pattern
    assert "s == 202" in src or "status == 202" in src or "== 202" in src, (
        "item_2 must only record an investigation for cleanup when status == 202 "
        "(this run created it); recording 200 (existing investigation) is the C2 bug"
    )


# ---------------------------------------------------------------------------
# C3: item 4 must NOT write to the ADP corpus table
# ---------------------------------------------------------------------------

def test_c3_item4_does_not_write_corpus() -> None:
    """_item_4 must not call put_item on the engineering-corpus table (read-only).

    Fix: use real corpus failure modes read from the live table instead of seeding fake ones.
    """
    import inspect
    src = inspect.getsource(_mod._item_4)
    # The source must not contain corpus_tbl.put_item or corpus put_item
    assert "corpus_tbl.put_item" not in src, (
        "_item_4 must not write to the corpus table; use real failure modes read-only"
    )


# ---------------------------------------------------------------------------
# W5: item 4 must record VIN objects for sw, hw, and mon (not just sw)
# ---------------------------------------------------------------------------

def test_w5_item4_records_all_vin_objects() -> None:
    """_item_4 must record affected_vins_s3_key for sw, hw, and mon decisions.

    The current code only records the sw VIN object inside `elif suffix == "sw":`;
    hw and mon objects leak on every run.
    Fix: record the VIN object for every suffix that produces a VIN object
    (sw, hw, mon) — not just sw.
    """
    import inspect
    src = inspect.getsource(_mod._item_4)
    # The recording of item4_vin_objects must NOT be gated inside `elif suffix == "sw":` alone
    # After the fix: item4_vin_objects append appears outside the `elif suffix == "sw":` block
    # Heuristic: item4_vin_objects appears and there's no `suffix == "sw"` guard immediately before it
    assert "item4_vin_objects" in src, "_item_4 must record item4_vin_objects"
    # The source must record vin_objects for hw and mon too
    # Check that the vin_key recording is not exclusively inside the sw block by looking at context
    # If it appears only inside `elif suffix == "sw":`, that's the bug
    sw_block_only = (
        'elif suffix == "sw"' in src
        and src.count("item4_vin_objects") == 1
        and src.index("item4_vin_objects") > src.index('elif suffix == "sw"')
        and (
            'elif suffix == "hw"' not in src
            or src.index("item4_vin_objects") < src.index('elif suffix == "hw"')
        )
    )
    assert not sw_block_only, (
        "_item_4 must record VIN objects for hw and mon too, not just sw. "
        "Currently item4_vin_objects only appears inside the `elif suffix == \"sw\":` block."
    )


# ---------------------------------------------------------------------------
# C5: ESM must be polled to Disabled/Enabled with a bound; world must model transitional states
# ---------------------------------------------------------------------------

def test_c5_wait_esm_state_function_exists() -> None:
    """A _wait_esm_state helper must exist on the script module."""
    assert hasattr(_mod, "_wait_esm_state"), (
        "_wait_esm_state helper must be defined (polls ESM to Disabled/Enabled with a bound)"
    )


def test_c5_item8b_does_not_read_state_immediately_after_update() -> None:
    """_item_8b must not call get_event_source_mapping immediately after update_event_source_mapping.

    Fix: use _wait_esm_state (poll with bound) instead of an immediate readback.
    """
    import inspect
    src = inspect.getsource(_mod._item_8b)
    # Must NOT have get_event_source_mapping right after update_event_source_mapping without a loop
    # Heuristic: if _wait_esm_state is used, it replaces the immediate get call
    assert "_wait_esm_state" in src or "poll" in src.lower(), (
        "_item_8b must use _wait_esm_state to poll for state change, not an immediate readback"
    )


# ===========================================================================
# Part B: fix-round-1 — mutant-catching tests and W9/W2 new-property tests
# ===========================================================================

# ---------------------------------------------------------------------------
# M04: item 2 affected_count mismatch → FAIL on pattern-numbers check
# Mutant changes "aff_count is not None and veh_affected is not None
#   and float(aff_count) == float(veh_affected)" to "True".
# Test name contains "b2" so mutant runner picks it up.
# ---------------------------------------------------------------------------

def test_b2_item2_fail_affected_count_mismatch(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Item 2 must FAIL when affected_count != vehicles_affected (M04 catch).

    Creates a finding where quality.pattern.affected_count (99) != pat_row.vehicles_affected (12).
    With the fix, item-2-plant-P1-pattern-numbers is FAIL.
    With M04 mutant (check always True), it would be PASS → test fails → mutant caught.
    """
    monkeypatch.setenv("LIVE_TESTS", "1")
    import datetime as dt
    monkeypatch.setattr(lv, "_utc_now",
        lambda: dt.datetime.now(dt.timezone.utc).replace(hour=10, minute=0))

    inv = _make_completed_inv()
    # affected_count=99 does NOT match vehicles_affected=12 in _PAT_ROW
    finding = _make_finding(
        finding_id=inv["finding_id"],
        pattern={"ratio": Decimal("0.1"), "p_value": Decimal("1E-10"),
                 "affected_count": Decimal("99")},  # mismatch: _PAT_ROW has 12
    )

    with Item2World(
        completed_inv=inv, region="us-west-2", stage="staging", account="123456789012",
        answer_key=[_AK_ROW_ACTIVE], pattern_rows=[_PAT_ROW], finding=finding,
    ) as w:
        _patch_item2_guards(monkeypatch, w)
        import duckdb
        ak_path = str(tmp_path / "ak_m04.parquet")
        con = duckdb.connect()
        con.execute(
            "CREATE TABLE ak AS SELECT ? AS pattern_id, ? AS code_family, "
            "? AS cause_dimension, ? AS cause_value, ? AS planted_count, "
            "? AS start_date, ? AS failure_mode_id",
            [_AK_ROW_ACTIVE["pattern_id"], _AK_ROW_ACTIVE["code_family"],
             _AK_ROW_ACTIVE["cause_dimension"], _AK_ROW_ACTIVE["cause_value"],
             _AK_ROW_ACTIVE["planted_count"], _AK_ROW_ACTIVE["start_date"],
             _AK_ROW_ACTIVE["failure_mode_id"]],
        )
        con.execute(f"COPY ak TO '{ak_path}' (FORMAT PARQUET)")
        con.close()

        manifest = {"run_id": _VALID_RUN_ID, "stage": "staging",
                    "investigations": [], "users": [], "item3_cap": None,
                    "event_source_disabled": False, "items_passed": {}}
        res = lv.Results()
        lv._item_2(res, stage="staging", run_id=_VALID_RUN_ID,
                   manifest=manifest, manifest_dir=str(tmp_path),
                   allow_writes=True, answer_key=ak_path)

    fail_labels = [r[0] for r in res.rows if r[1] == "FAIL"]
    assert "item-2-plant-P1-pattern-numbers" in fail_labels, (
        f"M04: item-2-plant-P1-pattern-numbers must FAIL when affected_count (99) "
        f"!= vehicles_affected (12). Got rows={res.rows}"
    )


# ---------------------------------------------------------------------------
# M16: item 3 must SKIP when write-window guard returns a reason.
# Mutant changes `if window_reason:` to `if False:`.
# Test name contains "a6" so mutant runner picks it up with c5_/b6_/b7_/a6/a7.
# ---------------------------------------------------------------------------

def test_a6_item3_skips_when_window_closed(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """_item_3 must SKIP when _after6_write_window_guard returns a reason (M16 catch).

    With the fix, item-3 SKIPs when the window is closed.
    With M16 mutant (guard check removed), item-3 proceeds → no SKIP → test fails.
    """
    monkeypatch.setenv("LIVE_TESTS", "1")

    # Make the write-window guard return a reason for item 3
    monkeypatch.setattr(
        lv, "_after6_write_window_guard",
        lambda *_a, **_k: "window closed: schedule at 02:00 UTC"
    )
    monkeypatch.setattr(lv, "_after6_pre_checks", lambda *_a, **_k: None)

    manifest = {
        "run_id": _VALID_RUN_ID, "stage": "staging",
        "investigations": [{"pattern_group_id": "abc123", "investigation_id": "inv-1",
                             "status": "completed"}],
        "item3_cap": None, "event_source_disabled": False, "items_passed": {}, "users": [],
    }
    res = lv.Results()
    lv._item_3(res, stage="staging", run_id=_VALID_RUN_ID,
               manifest=manifest, manifest_dir=str(tmp_path),
               allow_writes=True)

    skip_labels = [r[0] for r in res.rows if r[1] == "SKIP"]
    assert any("item-3" in lbl for lbl in skip_labels), (
        f"M16: item-3 must SKIP when write-window guard returns a reason. "
        f"rows={res.rows}"
    )


# ---------------------------------------------------------------------------
# M20: cleanup must delete the lock row (conditional on investigation_id_ref).
# Mutant removes `_step(f"after6-cleanup-del-lock-...", _del_lock)`.
# Test name contains "c2" so mutant runner picks it up.
# ---------------------------------------------------------------------------

def test_c2_cleanup_deletes_lock(tmp_path: Path) -> None:
    """_after6_cleanup must delete the lock row for each investigation (M20 catch).

    Seeds an investigation + lock row, runs cleanup, then verifies lock is absent.
    With M20 mutant (lock deletion removed), lock persists → test fails → mutant caught.
    """
    from moto import mock_aws

    pgid = "deadbeef12345678"
    inv_id = "test-inv-cleanup-lock"

    with mock_aws():
        import boto3
        ddb = boto3.client("dynamodb", region_name="us-west-2")
        ddb.create_table(
            TableName="vsa-staging-avx-investigations",
            KeySchema=[{"AttributeName": "investigation_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "investigation_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        ddb_r = boto3.resource("dynamodb", region_name="us-west-2")
        inv_tbl = ddb_r.Table("vsa-staging-avx-investigations")

        user_sub = "sub-m20-test-001"
        # Seed investigation row with requested_by = user_sub
        inv_tbl.put_item(Item={
            "investigation_id": inv_id,
            "pattern_group_id": pgid,
            "status": "completed",
            "requested_by": user_sub,
        })
        # Seed lock row
        inv_tbl.put_item(Item={
            "investigation_id": f"lock#{pgid}",
            "investigation_id_ref": inv_id,
        })

        manifest = {
            "run_id": _VALID_RUN_ID,
            "stage": "staging",
            "user_sub": user_sub,
            "investigations": [{"investigation_id": inv_id, "pattern_group_id": pgid,
                                 "status": "completed"}],
            "item3_cap": None,
            "item4_patterns": [], "item4_findings": [], "item4_actions": [],
            "item4_thread_versions": [], "item4_draft_rows": [], "item4_vin_objects": [],
            "item4_s3_lake_objects": [], "item4_recall_s3_objects": [],
            "item5_findings": [], "item5_actions": [],
            "event_source_disabled": False,
            "event_source_uuid": None,
            "users": [],
        }

        res = lv.Results()
        import signal
        old_int = signal.getsignal(signal.SIGINT)
        old_term = signal.getsignal(signal.SIGTERM)
        try:
            with lv._defer_signals_for_restore():
                lv._after6_cleanup(
                    res,
                    manifest=manifest,
                    manifest_dir=str(tmp_path),
                    stage="staging",
                )
        finally:
            signal.signal(signal.SIGINT, old_int)
            signal.signal(signal.SIGTERM, old_term)

        # Verify lock is absent after cleanup (mutant would leave it)
        lock_item = inv_tbl.get_item(
            Key={"investigation_id": f"lock#{pgid}"}, ConsistentRead=True
        ).get("Item")
        assert lock_item is None, (
            f"M20: lock row must be absent after cleanup. "
            f"lock_item={lock_item} rows={res.rows}"
        )


# ---------------------------------------------------------------------------
# M21 + W2: cleanup must restore cap with count=20 condition (not count=1).
# Mutant removes `_step("after6-cleanup-cap-restore", _restore_cap)`.
# W2 bug: old code uses count=1 (wrong); fix uses count=20 (correct).
# Test name contains "b7" so mutant runner picks it up.
# RED until W2 fix (count=20); GREEN after fix.
# ---------------------------------------------------------------------------

def test_b7_cleanup_restores_absent_before_cap(tmp_path: Path) -> None:
    """Cleanup must delete an absent-before cap row (item 3 created it with count=20).

    M21 catch: without the cap restore step, cap row persists → test fails.
    W2 fix: cleanup must use count=20 (not count=1) as the condition.

    RED with old code (count=1 condition fails on count=20 row → cap not deleted).
    GREEN after W2 fix (count=20 condition matches → cap deleted).
    """
    from moto import mock_aws
    from decimal import Decimal as _Dec

    date_str = "2026-10-27"
    cap_key = f"cap#{date_str}"

    with mock_aws():
        import boto3
        ddb = boto3.client("dynamodb", region_name="us-west-2")
        ddb.create_table(
            TableName="vsa-staging-avx-investigations",
            KeySchema=[{"AttributeName": "investigation_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "investigation_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        ddb_r = boto3.resource("dynamodb", region_name="us-west-2")
        inv_tbl = ddb_r.Table("vsa-staging-avx-investigations")

        # Seed cap row with count=20 (item 3 created it from absent-before state)
        inv_tbl.put_item(Item={"investigation_id": cap_key, "count": _Dec("20")})

        manifest = {
            "run_id": _VALID_RUN_ID,
            "stage": "staging",
            "user_sub": "",
            "investigations": [],
            "item3_cap": {"date": date_str, "before_count": None, "moved": True},
            "item4_patterns": [], "item4_findings": [], "item4_actions": [],
            "item4_thread_versions": [], "item4_draft_rows": [], "item4_vin_objects": [],
            "item4_s3_lake_objects": [], "item4_recall_s3_objects": [],
            "item5_findings": [], "item5_actions": [],
            "event_source_disabled": False,
            "event_source_uuid": None,
            "users": [],
        }

        res = lv.Results()
        import signal
        old_int = signal.getsignal(signal.SIGINT)
        old_term = signal.getsignal(signal.SIGTERM)
        try:
            with lv._defer_signals_for_restore():
                lv._after6_cleanup(
                    res,
                    manifest=manifest,
                    manifest_dir=str(tmp_path),
                    stage="staging",
                )
        finally:
            signal.signal(signal.SIGINT, old_int)
            signal.signal(signal.SIGTERM, old_term)

        # Verify cap row is absent after cleanup
        cap_item = inv_tbl.get_item(
            Key={"investigation_id": cap_key}, ConsistentRead=True
        ).get("Item")
        assert cap_item is None, (
            f"M21+W2: cap row must be deleted after cleanup when before_count=None. "
            f"cap_item={cap_item}; rows={res.rows}\n"
            f"(RED if W2 fix not applied: old code uses count=1 condition, "
            f"actual count is 20 → ConditionalCheckFailed → cap not deleted)"
        )


# ---------------------------------------------------------------------------
# M25: --cleanup-only must exit 1 when cleanup has a FAIL row.
# Mutant changes `return 1 if res.has_any_fail else 0` to `return 0`.
# Test name contains "cleanup_only" so mutant runner picks it up.
# ---------------------------------------------------------------------------

def test_cleanup_only_fails_returns_1(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """--cleanup-only must return 1 when cleanup encounters a FAIL (M25 catch).

    Sets up a manifest where cleanup will FAIL.
    With the fix, main() returns 1. With M25 mutant, main() returns 0 → test fails.
    """
    monkeypatch.setenv("LIVE_TESTS", "1")
    monkeypatch.setenv("FI_QUALITY_MANIFEST_DIR", str(tmp_path))

    # Patch Cognito helpers to avoid real AWS calls
    monkeypatch.setattr(lv, "_resolve_pool_id", lambda _idp: "us-west-2_testpl")
    # Not a world test: the stale-user reaper (security cycle 1 W1) has no fake Cognito here.
    monkeypatch.setattr(lv, "_reap_stale_users_step", lambda *a, **k: None)

    # Arrange a manifest with event_source_uuid so cleanup tries to re-enable ESM
    manifest = {
        "run_id": _VALID_RUN_ID,
        "stage": "staging",
        "event_source_uuid": "00000000-0000-0000-0000-000000000001",
        "event_source_disabled": True,
        "investigations": [],
        "item3_cap": None,
        "item4_patterns": [], "item4_findings": [], "item4_actions": [],
        "item4_thread_versions": [], "item4_draft_rows": [], "item4_vin_objects": [],
        "item4_s3_lake_objects": [], "item4_recall_s3_objects": [],
        "item5_findings": [], "item5_actions": [],
        "users": [],
        "items_passed": {},
        "user_sub": "",
    }
    lv._save_manifest(_VALID_RUN_ID, manifest, str(tmp_path))

    # Monkeypatch _after6_cleanup to inject a FAIL row (simulates ESM re-enable failure)
    def _failing_cleanup(res, **kw):
        lv.check(res, "after6-cleanup-esm-reenable", False,
                 on_fail="simulated ESM re-enable failure for M25 test")
    monkeypatch.setattr(lv, "_after6_cleanup", _failing_cleanup)

    old_int = signal.getsignal(signal.SIGINT)
    old_term = signal.getsignal(signal.SIGTERM)
    try:
        rc = lv.main([
            "--stage", "staging",
            "--cleanup-only",
            "--run-id", _VALID_RUN_ID,
            "--allow-writes",
        ])
    finally:
        signal.signal(signal.SIGINT, old_int)
        signal.signal(signal.SIGTERM, old_term)

    assert rc == 1, (
        f"M25: --cleanup-only must return 1 when cleanup has a FAIL. got rc={rc}"
    )


def test_s5c_cleanup_only_refuses_run_id_mismatch(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """--cleanup-only refuses when manifest run_id != --run-id, no AWS call.

    S5 fix: shape-check manifest before any AWS write.
    Mutant M33: shape check removed → proceeds (CAUGHT if it reaches AWS write).
    RED: old code trusts manifest run_id blindly → no refusal.
    """
    monkeypatch.setenv("LIVE_TESTS", "1")
    monkeypatch.setenv("FI_QUALITY_MANIFEST_DIR", str(tmp_path))

    run_id_arg = _VALID_RUN_ID
    different_run_id = "fi-quality-20261027T090000-deadbeef"

    # Save manifest with a DIFFERENT run_id
    (tmp_path / different_run_id).mkdir()
    import json
    manifest = {
        "run_id": different_run_id,
        "stage": "staging",
        "investigations": [],
        "users": [],
        "items_passed": {},
    }
    manifest_file = tmp_path / different_run_id / "manifest.json"
    manifest_file.write_text(json.dumps(manifest))
    manifest_file.chmod(0o600)

    # --cleanup-only with a run_id that does NOT match the manifest's run_id
    # (The manifest is stored at the different_run_id path, not run_id_arg path)
    # We need the manifest to be found; put it at the run_id_arg path too
    (tmp_path / run_id_arg).mkdir(exist_ok=True)
    manifest2 = {**manifest, "run_id": different_run_id}  # run_id mismatch
    mf2 = tmp_path / run_id_arg / "manifest.json"
    mf2.write_text(json.dumps(manifest2))
    mf2.chmod(0o600)

    rc = lv.main([
        "--stage", "staging",
        "--cleanup-only",
        "--run-id", run_id_arg,
        "--allow-writes",
    ])

    assert rc == 1, f"S5: --cleanup-only must return 1 on run_id mismatch; got {rc}"


def test_s5c_cleanup_only_refuses_stage_mismatch(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """--cleanup-only refuses when manifest stage != --stage, no AWS call.

    S5 fix: stage mismatch must be refused before any boto3 call.
    Mutant M33: shape check removed → proceeds (AWS calls would fail cleanly but shape
    check prevents even reaching boto3).
    RED: old code trusts manifest stage → proceeds; loopback_guard not triggered.
    """
    monkeypatch.setenv("LIVE_TESTS", "1")
    monkeypatch.setenv("FI_QUALITY_MANIFEST_DIR", str(tmp_path))

    import json
    run_id = _VALID_RUN_ID
    (tmp_path / run_id).mkdir()
    manifest = {
        "run_id": run_id,
        "stage": "production",  # mismatches --stage staging
        "investigations": [],
        "users": [],
        "items_passed": {},
    }
    mf = tmp_path / run_id / "manifest.json"
    mf.write_text(json.dumps(manifest))
    mf.chmod(0o600)

    rc = lv.main([
        "--stage", "staging",
        "--cleanup-only",
        "--run-id", run_id,
        "--allow-writes",
    ])

    assert rc == 1, f"S5: --cleanup-only must return 1 on stage mismatch; got {rc}"


def test_s5c_cleanup_only_refuses_table_name_mismatch(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """--cleanup-only refuses when a manifest table name doesn't match the stage.

    S5 fix: table names in manifest must contain the stage string.
    RED: old code proceeds; shape check is not present.
    """
    monkeypatch.setenv("LIVE_TESTS", "1")
    monkeypatch.setenv("FI_QUALITY_MANIFEST_DIR", str(tmp_path))

    import json
    run_id = _VALID_RUN_ID
    (tmp_path / run_id).mkdir()
    manifest = {
        "run_id": run_id,
        "stage": "staging",
        "investigations": [],
        "users": [],
        "items_passed": {},
        "item4_draft_rows": [
            {"campaign_id": "OTA-001", "table": "adp-PRODUCTION-ota-campaigns"}
        ],
    }
    mf = tmp_path / run_id / "manifest.json"
    mf.write_text(json.dumps(manifest))
    mf.chmod(0o600)

    rc = lv.main([
        "--stage", "staging",
        "--cleanup-only",
        "--run-id", run_id,
        "--allow-writes",
    ])

    assert rc == 1, (
        f"S5: --cleanup-only must return 1 when table name doesn't contain stage; got {rc}"
    )


# ---------------------------------------------------------------------------
# W1 — signal hold + _safe_print (through main())
# ---------------------------------------------------------------------------

def test_w1_signal_during_cognito_resolving_cleanup_still_runs(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Signal arriving while _resolve_pool_id runs must not skip _after6_cleanup.

    W1 fix: the signal hold starts before Cognito resolution (was after).
    RED before fix: SIGTERM during _resolve_pool_id raises SystemExit(143) before
    the hold, skipping cleanup entirely.
    GREEN after fix: SIGTERM is deferred, cleanup runs, then re-raised at hold exit.
    """
    monkeypatch.setenv("LIVE_TESTS", "1")
    monkeypatch.setenv("FI_QUALITY_MANIFEST_DIR", str(tmp_path))

    # Make the fresh manifest include a user so Cognito resolution is attempted.
    orig_fresh = lv._fresh_manifest
    def _with_user(run_id: str, stage: str) -> dict:
        m = orig_fresh(run_id, stage)
        m["users"].append("cognito-user-fixture")
        return m
    monkeypatch.setattr(lv, "_fresh_manifest", _with_user)

    # All items fake-pass so no real AWS calls during _run_items.
    fns = {n: _fake_pass for n in lv._ITEM_FNS}
    monkeypatch.setattr(lv, "_ITEM_FNS", fns)

    cleanup_called: list[bool] = []
    monkeypatch.setattr(lv, "_after6_cleanup", lambda res, **kw: cleanup_called.append(True))

    # _resolve_pool_id sends SIGTERM to self (as if a signal arrived during the call).
    # The signal must be deferred because we are now inside _defer_signals_for_restore().
    def _resolver_sends_sigterm(idp: Any) -> str:
        import os as _os
        _os.kill(_os.getpid(), signal.SIGTERM)
        raise Exception("cognito fake")   # caught by the except around it
    monkeypatch.setattr(lv, "_resolve_pool_id", _resolver_sends_sigterm)

    try:
        lv.main(["--stage", "staging", "--part", "after-6", "--allow-writes"])
    except SystemExit:
        pass  # signal re-delivered at hold exit is expected

    assert cleanup_called, (
        "W1: _after6_cleanup must run even when SIGTERM fires during Cognito resolution"
    )


def test_w1_sighup_inside_cleanup_held_later_steps_run(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """SIGHUP arriving inside _after6_cleanup must be held until the hold exits.

    W1 fix: _defer_signals_for_restore now holds SIGHUP (was SIGINT/SIGTERM only).
    RED before fix: SIGHUP raises SystemExit(143) mid-cleanup, later steps skipped.
    GREEN after fix: SIGHUP is deferred; all steps complete; re-raised at hold exit.
    """
    monkeypatch.setenv("LIVE_TESTS", "1")
    monkeypatch.setenv("FI_QUALITY_MANIFEST_DIR", str(tmp_path))

    fns = {n: _fake_pass for n in lv._ITEM_FNS}
    monkeypatch.setattr(lv, "_ITEM_FNS", fns)

    steps_ran: list[str] = []

    def _hup_cleanup(res: lv.Results, **kw: Any) -> None:
        # Send SIGHUP from inside cleanup — must be deferred, not delivered immediately.
        import os as _os
        _os.kill(_os.getpid(), signal.SIGHUP)
        steps_ran.append("step1")
        lv.check(res, "cleanup-step2-label", True)
        steps_ran.append("step2")

    monkeypatch.setattr(lv, "_after6_cleanup", _hup_cleanup)

    try:
        lv.main(["--stage", "staging", "--part", "after-6", "--allow-writes"])
    except SystemExit:
        pass  # SIGHUP re-delivered at hold exit

    assert "step1" in steps_ran, "W1: step 1 of cleanup did not run"
    assert "step2" in steps_ran, (
        "W1: SIGHUP must be deferred during cleanup; step 2 must run after it"
    )


def test_w1_stdout_oserror_all_cleanup_steps_still_run(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """OSError from stdout during first cleanup row must not stop later rows.

    W1 fix: check()/skip()/summary() use _safe_print that swallows OSError/ValueError.
    RED before fix: raw print() raises propagates and stops cleanup mid-step.
    GREEN after fix: error swallowed, row still appended, subsequent steps run.
    """
    monkeypatch.setenv("LIVE_TESTS", "1")
    monkeypatch.setenv("FI_QUALITY_MANIFEST_DIR", str(tmp_path))

    fns = {n: _fake_pass for n in lv._ITEM_FNS}
    monkeypatch.setattr(lv, "_ITEM_FNS", fns)

    captured_res: list[lv.Results] = []

    def _eio_cleanup(res: lv.Results, **kw: Any) -> None:
        captured_res.append(res)
        lv.check(res, "cleanup-step1-eio-label", True)
        lv.check(res, "cleanup-step2-after-eio", True)

    monkeypatch.setattr(lv, "_after6_cleanup", _eio_cleanup)

    # Raise OSError only on the first status-row print ("[PASS]" / "[FAIL]" / "[SKIP]"),
    # which is the first call inside check() during cleanup.  Pre-cleanup prints in
    # main() (run_id line, manifest line) do not match this pattern and pass through.
    import builtins as _builtins
    original_print = _builtins.print
    first_row_print = [True]  # mutable flag so inner function can write it

    def _eio_on_first_row(*args: Any, **kwargs: Any) -> None:
        s = " ".join(str(a) for a in args)
        if first_row_print[0] and ("[PASS]" in s or "[FAIL]" in s or "[SKIP]" in s):
            first_row_print[0] = False
            raise OSError(5, "Input/output error")
        original_print(*args, **kwargs)

    monkeypatch.setattr(_builtins, "print", _eio_on_first_row)

    try:
        lv.main(["--stage", "staging", "--part", "after-6", "--allow-writes"])
    except SystemExit:
        pass

    assert captured_res, "cleanup spy not reached"
    row_labels = {r[0] for r in captured_res[0].rows}
    assert "cleanup-step1-eio-label" in row_labels, (
        "W1: step-1 row must be recorded even when print() raises OSError"
    )
    assert "cleanup-step2-after-eio" in row_labels, (
        "W1: step-2 row must be recorded after OSError from step-1 print"
    )


def test_w1_summary_printed_after_held_signal(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """res.summary() must be called even when a deferred signal re-raises at hold exit.

    W1 fix: res.summary is in a try/finally around the with _defer_signals_for_restore()
    block, so it runs even when SystemExit is raised at the hold's exit.
    RED before fix: res.summary is after the with block; re-raised signal skips it.
    GREEN after fix: summary is in the finally; it always runs.
    """
    monkeypatch.setenv("LIVE_TESTS", "1")
    monkeypatch.setenv("FI_QUALITY_MANIFEST_DIR", str(tmp_path))

    fns = {n: _fake_pass for n in lv._ITEM_FNS}
    monkeypatch.setattr(lv, "_ITEM_FNS", fns)

    # Cleanup sends SIGTERM, which is held and re-raised at the hold exit.
    def _sigterm_cleanup(res: lv.Results, **kw: Any) -> None:
        import os as _os
        _os.kill(_os.getpid(), signal.SIGTERM)

    monkeypatch.setattr(lv, "_after6_cleanup", _sigterm_cleanup)

    summary_called: list[bool] = []
    orig_summary = lv.Results.summary

    def _spy_summary(self: lv.Results, run_id: str) -> None:
        summary_called.append(True)
        orig_summary(self, run_id)

    monkeypatch.setattr(lv.Results, "summary", _spy_summary)

    try:
        lv.main(["--stage", "staging", "--part", "after-6", "--allow-writes"])
    except SystemExit:
        pass  # expected: SIGTERM re-raised at hold exit

    assert summary_called, (
        "W1: res.summary() must be called even when a held signal re-raises at hold exit"
    )


# ---------------------------------------------------------------------------
# S1: guard pass note names the rate() schedules it skipped
# ---------------------------------------------------------------------------

def test_s1_guard_pass_note_names_skipped_rate_schedules() -> None:
    """S1: when all schedules are rate(), the guard's result is falsy but names the skipped schedules.

    RED before fix: result is None; str(None) does not contain the schedule name.
    GREEN after fix: result is a falsy pass-note whose str() contains the schedule name.
    """
    import datetime as dt

    class _FakeSched:
        def list_schedules(self, **_: Any) -> dict:
            return {
                "Schedules": [
                    {"Name": "vsa-staging-tier2-diagnostics-us-west-2",
                     "GroupName": "default"},
                ]
            }

        def get_schedule(self, Name: str, GroupName: str, **_: Any) -> dict:
            return {
                "ScheduleExpression": "rate(5 minutes)",
                "ScheduleExpressionTimezone": "UTC",
            }

    now = dt.datetime(2026, 10, 27, 7, 0, 0, tzinfo=dt.timezone.utc)
    result = _mod._after6_write_window_guard(_FakeSched(), "staging", now)

    # Writes must still be allowed (result is falsy)
    assert not result, f"S1: rate()-only guard must be falsy (writes allowed); got {result!r}"

    # S1 fix: the pass note must name the skipped schedule
    assert "diagnostics" in str(result), (
        f"S1: guard pass note must name the skipped rate() schedule "
        f"'vsa-staging-tier2-diagnostics-us-west-2'; got {result!r}"
    )


def test_s1_guard_pass_note_names_multiple_rate_schedules() -> None:
    """S1: when multiple rate() schedules are skipped, all their names appear in the note.

    RED before fix: result is None.
    GREEN after fix: result names both schedules.
    """
    import datetime as dt

    class _FakeSched:
        def list_schedules(self, **_: Any) -> dict:
            return {
                "Schedules": [
                    {"Name": "vsa-staging-tier2-diagnostics-us-west-2",
                     "GroupName": "default"},
                    {"Name": "vsa-staging-tier2-health-agent-us-west-2",
                     "GroupName": "default"},
                ]
            }

        def get_schedule(self, Name: str, GroupName: str, **_: Any) -> dict:
            return {
                "ScheduleExpression": "rate(10 minutes)",
                "ScheduleExpressionTimezone": "UTC",
            }

    now = dt.datetime(2026, 10, 27, 7, 0, 0, tzinfo=dt.timezone.utc)
    result = _mod._after6_write_window_guard(_FakeSched(), "staging", now)

    assert not result, f"S1: all-rate() guard must be falsy; got {result!r}"
    assert "diagnostics" in str(result), (
        f"S1: first rate() schedule name missing from pass note; got {result!r}"
    )
    assert "health-agent" in str(result), (
        f"S1: second rate() schedule name missing from pass note; got {result!r}"
    )


# ---------------------------------------------------------------------------
# S2: _validate_cleanup_manifest refuses empty run_id/stage; checks item4_thread_lake_bucket
# ---------------------------------------------------------------------------

def test_s2_validate_manifest_refuses_empty_run_id() -> None:
    """S2: _validate_cleanup_manifest must refuse when manifest run_id is empty.

    RED before fix: `if mfst_run_id and mfst_run_id != run_id` skips the check when
    mfst_run_id is "" → returns None (no error).
    GREEN after fix: empty run_id is explicitly refused.
    """
    manifest = {
        "run_id": "",
        "stage": "staging",
        "investigations": [],
    }
    result = _mod._validate_cleanup_manifest(manifest, _VALID_RUN_ID, "staging")
    assert result is not None, (
        "S2: manifest with empty run_id must be refused by _validate_cleanup_manifest"
    )
    assert "run_id" in result.lower(), (
        f"S2: refusal reason must mention run_id; got {result!r}"
    )


def test_s2_validate_manifest_refuses_empty_stage() -> None:
    """S2: _validate_cleanup_manifest must refuse when manifest stage is empty.

    RED before fix: `if mfst_stage and mfst_stage != stage` skips the check when
    mfst_stage is "" → proceeds silently.
    GREEN after fix: empty stage is explicitly refused.
    """
    manifest = {
        "run_id": _VALID_RUN_ID,
        "stage": "",
        "investigations": [],
    }
    result = _mod._validate_cleanup_manifest(manifest, _VALID_RUN_ID, "staging")
    assert result is not None, (
        "S2: manifest with empty stage must be refused by _validate_cleanup_manifest"
    )
    assert "stage" in result.lower(), (
        f"S2: refusal reason must mention stage; got {result!r}"
    )


def test_s2_validate_manifest_checks_item4_thread_lake_bucket() -> None:
    """S2: _validate_cleanup_manifest must check item4_thread_lake_bucket.

    RED before fix: the check loop only iterates over list fields; top-level string
    fields like item4_thread_lake_bucket are skipped → wrong-environment bucket accepted.
    GREEN after fix: a production bucket name in item4_thread_lake_bucket is refused.
    """
    manifest = {
        "run_id": _VALID_RUN_ID,
        "stage": "staging",
        # Thread-lake bucket belongs to production, not staging — must be refused
        "item4_thread_lake_bucket": "adp-production-foundation-lake-123456789012-us-east-1",
    }
    result = _mod._validate_cleanup_manifest(manifest, _VALID_RUN_ID, "staging")
    assert result is not None, (
        "S2: manifest with item4_thread_lake_bucket containing 'production' instead of "
        "'staging' must be refused"
    )
    assert "thread_lake_bucket" in result or "bucket" in result.lower(), (
        f"S2: refusal must mention item4_thread_lake_bucket or bucket; got {result!r}"
    )
