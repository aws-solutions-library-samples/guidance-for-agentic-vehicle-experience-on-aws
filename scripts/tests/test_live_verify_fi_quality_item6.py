"""Tests for scripts/live-verify-fi-quality.py item 6.

Runs the REAL investigations and findings handlers in-process behind
``FakeRestApi`` / ``FakeUserPool`` on moto DynamoDB + SQS.

Covers:
  J1  three 403s per route (cs and fop on /investigations, /approve, /decline)
  J2  each eng control: exact status and error text
  J3  no Action row for the plant
  J4  cleanup confirm-by-read: queue empty, row/lock/plant absent,
      cap restored when cap was absent before
  J5  cap restored when cap existed (count=n) before
  J6  foreign queue message is not deleted and FAILs item
  J7  SKIP when the event source mapping is Enabled (not Disabled)
  J8  SKIP when the queue is non-empty at start
  J9  SKIP without --allow-writes

Run:
    PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest \
        scripts/tests/test_live_verify_fi_quality_item6.py -q
"""
from __future__ import annotations

import importlib
import importlib.util
import io
import json
import os
import sys
import uuid
import zipfile
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterator

import pytest
from moto import mock_aws

# ---------------------------------------------------------------------------
# Load the script as a module (hyphenated filename requires importlib)
# ---------------------------------------------------------------------------

_HERE = Path(__file__).resolve().parent
_SCRIPT = _HERE.parent / "live-verify-fi-quality.py"
_spec = importlib.util.spec_from_file_location("live_verify_fi_quality_item6_mod", _SCRIPT)
lv = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(lv)

Results = lv.Results

# ---------------------------------------------------------------------------
# Helpers shared with world.py (copied inline to avoid importing world.py,
# which drags in DMS repo discovery and other heavy deps)
# ---------------------------------------------------------------------------

_AVX_ROOT = Path(_HERE).resolve().parents[1]
STAGE, REGION, ACCOUNT = "staging", "us-west-2", "123456789012"
INV_TABLE = f"vsa-{STAGE}-avx-investigations"
FINDINGS_TABLE = f"vsa-{STAGE}-avx-findings"
ACTIONS_TABLE = f"vsa-{STAGE}-avx-actions"
QUEUE_NAME = f"vsa-{STAGE}-avx-investigations"
TIER2_FN = f"vsa-{STAGE}-tier2-health-agent-{REGION}"


def _load_file(path: Path, name: str) -> Any:
    """Load a Python file as a fresh module (same approach as world.py)."""
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    sys.modules[name] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def _zip_stub() -> bytes:
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w") as z:
        z.writestr("index.py", "def h(e, c):\n    raise RuntimeError('world-only')\n")
    return b.getvalue()


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------

class _W6:
    """Mini fixture context for item-6 tests."""

    def __init__(self) -> None:
        import boto3
        from scripts.tests.fi_rw_world.apigw import FakeRestApi
        from scripts.tests.fi_rw_world.awsfake import Router
        from scripts.tests.fi_rw_world.cognito import FakeUserPool
        from scripts.tests.fi_rw_world.lambdas import lambda_env

        self.lambda_env = lambda_env

        # -- Fake user pool (Cognito) -----------------------------------------
        class _Clock:
            def epoch(self) -> float:
                import time as _t
                return _t.time()

            def now(self) -> Any:
                import datetime as _dt
                return _dt.datetime.now(_dt.timezone.utc)

        self.pool = FakeUserPool(_Clock(), REGION)

        # -- Router: intercepts Cognito + lets moto serve DDB/SQS/Lambda ------
        self.router = Router()
        self.pool.register(self.router)
        # _resolve_pool_id calls list_user_pools → intercept so it finds our pool
        pool = self.pool  # capture for closure

        def _list_user_pools(p: dict) -> dict:
            return {"UserPools": [{"Id": pool.pool_id, "Name": "cms-staging-ui-users"}]}

        self.router.add("cognito-idp", "ListUserPools", _list_user_pools)
        self.router.install()

        # -- DynamoDB tables ---------------------------------------------------
        ddb = boto3.client("dynamodb", region_name=REGION)
        ddb.create_table(
            TableName=INV_TABLE,
            KeySchema=[{"AttributeName": "investigation_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "investigation_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        ddb.create_table(
            TableName=FINDINGS_TABLE,
            KeySchema=[{"AttributeName": "finding_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "finding_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        ddb.create_table(
            TableName=ACTIONS_TABLE,
            KeySchema=[{"AttributeName": "action_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "action_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        ddb_r = boto3.resource("dynamodb", region_name=REGION)
        self.inv_tbl = ddb_r.Table(INV_TABLE)
        self.findings_tbl = ddb_r.Table(FINDINGS_TABLE)
        self.actions_tbl = ddb_r.Table(ACTIONS_TABLE)

        # -- SQS queue ---------------------------------------------------------
        self.sqs = boto3.client("sqs", region_name=REGION)
        q = self.sqs.create_queue(QueueName=QUEUE_NAME)
        self.queue_url = q["QueueUrl"]
        self.queue_arn = self.sqs.get_queue_attributes(
            QueueUrl=self.queue_url, AttributeNames=["QueueArn"]
        )["Attributes"]["QueueArn"]

        # -- Lambda function + event source mapping (Disabled) -----------------
        iam = boto3.client("iam", region_name=REGION)
        role_arn = iam.create_role(
            RoleName="item6-test-role", AssumeRolePolicyDocument="{}"
        )["Role"]["Arn"]
        self.lam = boto3.client("lambda", region_name=REGION)
        self.lam.create_function(
            FunctionName=TIER2_FN, Runtime="python3.12", Role=role_arn,
            Handler="index.h", Code={"ZipFile": _zip_stub()},
        )
        esm = self.lam.create_event_source_mapping(
            EventSourceArn=self.queue_arn, FunctionName=TIER2_FN, Enabled=False
        )
        self.esm_uuid = esm["UUID"]

        # -- Load real handlers with their live environment --------------------
        # Load findings handler FIRST so _lib.avx_scope is loaded with AVX_FINDINGS_TABLE
        # set correctly (avx_scope._FINDINGS_TABLE is a module-level constant set at import
        # time; if investigations loads avx_scope first without AVX_FINDINGS_TABLE, it defaults
        # to "vsa-dev-avx-findings" and _load_finding_by_id silently targets the wrong table).
        n = uuid.uuid4().hex[:8]
        self._findings_env = {
            "AVX_FINDINGS_TABLE": FINDINGS_TABLE,
            "AVX_ACTIONS_TABLE": ACTIONS_TABLE,
            "AVX_STAGE": STAGE,
            "AVX_FLEET_IDS_CLAIM_TRUSTED": "true",
        }
        self._inv_env = {
            "AVX_INVESTIGATIONS_TABLE": INV_TABLE,
            "AVX_INVESTIGATIONS_QUEUE_URL": self.queue_url,
        }
        with lambda_env(self._findings_env, f"vsa-{STAGE}-api-avx-findings", REGION):
            self._findings_mod = _load_file(
                _AVX_ROOT / "lambdas" / "api-avx-findings" / "handler.py",
                f"item6_findings_{n}",
            )
        # Reset avx_scope._ddb so the next test's moto context gets a fresh client.
        # _lib.avx_scope is shared across tests (it's in sys.modules); its _ddb global
        # becomes stale when mock_aws is stopped and restarted between test fixtures.
        if "_lib.avx_scope" in sys.modules:
            sys.modules["_lib.avx_scope"]._ddb = None  # type: ignore[attr-defined]
        with lambda_env(self._inv_env, f"vsa-{STAGE}-api-avx-investigations", REGION):
            self._inv_mod = _load_file(
                _AVX_ROOT / "lambdas" / "api-avx-investigations" / "handler.py",
                f"item6_inv_{n}",
            )

        # -- FakeRestApi -------------------------------------------------------
        api_envs = {
            "investigations": self._inv_env,
            "findings": self._findings_env,
        }

        def _invoke(name: str, fn: Any, event: dict, ctx: Any) -> dict:
            env = api_envs.get(name, {})
            with lambda_env(env, name, REGION):
                return fn(event, ctx)

        self.api = FakeRestApi("vsa-staging-api", STAGE, self.pool.verify, _invoke)
        self.api.add("/investigations", "POST", "investigations",
                     self._inv_mod.handler)
        self.api.add("/findings/{finding_id}/approve", "POST", "findings",
                     self._findings_mod.handler)
        self.api.add("/findings/{finding_id}/decline", "POST", "findings",
                     self._findings_mod.handler)

        # -- CloudFormation stack with RestApiUrl output ----------------------
        boto3.client("cloudformation", region_name=REGION).create_stack(
            StackName=f"vsa-{STAGE}-api",
            TemplateBody=json.dumps({
                "Resources": {"Placeholder": {"Type": "AWS::SNS::Topic"}},
                "Outputs": {"RestApiUrl": {"Value": self.api.url}},
            }),
        )

    def close(self) -> None:
        self.api.close()
        self.router.uninstall()


@pytest.fixture(autouse=True)
def _no_sqs_waits(monkeypatch: pytest.MonkeyPatch) -> None:
    """moto honours WaitTimeSeconds; the live waits would make each test take minutes."""
    monkeypatch.setattr(lv, "_SQS_DRAIN_WAIT_S", 0)
    monkeypatch.setattr(lv, "_SQS_FINAL_WAIT_S", 0)
    monkeypatch.setattr(lv, "_QUEUE_EMPTY_POLL_S", 2)
    import datetime as _dt
    monkeypatch.setattr(lv, "_utc_now", lambda: _dt.datetime.now(_dt.timezone.utc).replace(hour=12, minute=0))


# ---------------------------------------------------------------------------
# The script's own DynamoDB tables read eventually consistently (review cycle 2 W3)
# ---------------------------------------------------------------------------

#: Per-test hooks on the script's tables (by table name).
_NOOP_DELETES: set[str] = set()          # delete_item does nothing
_RAISE_ON_GET: set[str] = set()          # get_item of these keys raises
_SIGTERM_ON_DELETE: set[str] = set()     # first delete_item sends SIGTERM after it ran


class _StaleTable:
    """The script's view of a moto table. A read without ``ConsistentRead`` sees the table
    as it was when the script opened it, which an eventually consistent read may return
    right after a write. Consistent reads and writes go to moto."""

    def __init__(self, real: Any) -> None:
        self._real = real
        self._name = real.name
        self._keys = [k["AttributeName"] for k in real.key_schema]
        self._snap = real.scan(ConsistentRead=True).get("Items", [])
        self._signalled = False

    def _key_of(self, item: dict) -> tuple:
        return tuple(item.get(k) for k in self._keys)

    def get_item(self, **kw: Any) -> dict:
        key = kw["Key"]
        if any(str(v) in _RAISE_ON_GET for v in key.values()):
            raise RuntimeError(f"get_item denied for {key}")
        if kw.get("ConsistentRead"):
            return self._real.get_item(**kw)
        want = tuple(key.get(k) for k in self._keys)
        hit = [i for i in self._snap if self._key_of(i) == want]
        return {"Item": hit[0]} if hit else {}

    def scan(self, **kw: Any) -> dict:
        if kw.get("ConsistentRead"):
            return self._real.scan(**kw)
        return {"Items": list(self._snap)}

    def delete_item(self, **kw: Any) -> Any:
        if self._name in _NOOP_DELETES:
            return {}
        out = self._real.delete_item(**kw)
        if self._name in _SIGTERM_ON_DELETE and not self._signalled:
            self._signalled = True
            import signal as _signal
            os.kill(os.getpid(), _signal.SIGTERM)
        return out

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)


@pytest.fixture(autouse=True)
def _script_reads_are_eventually_consistent(monkeypatch: pytest.MonkeyPatch) -> None:
    """Wrap only the tables the script itself opens (the route handlers' reads stay moto's)."""
    import boto3
    real_resource = boto3.resource
    _NOOP_DELETES.clear()
    _RAISE_ON_GET.clear()
    _SIGTERM_ON_DELETE.clear()

    def resource(service: str, *a: Any, **kw: Any) -> Any:
        r = real_resource(service, *a, **kw)
        if service == "dynamodb" and sys._getframe(1).f_globals.get("__name__") == lv.__name__:
            real_table = r.Table
            r.Table = lambda name: _StaleTable(real_table(name))  # type: ignore[method-assign]
        return r
    monkeypatch.setattr(boto3, "resource", resource)


@pytest.fixture()
def w6() -> Iterator[_W6]:
    """Fresh moto + Router + real handlers for item-6 tests (function scope)."""
    saved_env = dict(os.environ)
    for k in list(os.environ):
        if k.startswith("AWS_"):
            del os.environ[k]
    os.environ.update(
        AWS_ACCESS_KEY_ID="testing",
        AWS_SECRET_ACCESS_KEY="testing",
        AWS_SESSION_TOKEN="testing",
        AWS_DEFAULT_REGION=REGION,
        AWS_EC2_METADATA_DISABLED="true",
    )
    os.environ.pop("LIVE_TESTS", None)

    with mock_aws():
        ctx = _W6()
        try:
            yield ctx
        finally:
            ctx.close()

    os.environ.clear()
    os.environ.update(saved_env)


# ---------------------------------------------------------------------------
# Helpers for tests
# ---------------------------------------------------------------------------

def _run6(w: _W6, run_id: str = "TESTRUN001", allow_writes: bool = True) -> Results:
    """Call _item_6 directly (bypasses main/LIVE_TESTS guard)."""
    res = Results()
    lv._item_6(res, allow_writes=allow_writes, stage=STAGE, run_id=run_id)
    return res


def _rows(res: Results) -> dict[str, str]:
    return {label: status for label, status, _ in res.rows}


def _detail(res: Results) -> dict[str, str]:
    return {label: detail for label, _, detail in res.rows}


# ---------------------------------------------------------------------------
# J1: three 403s per route
# ---------------------------------------------------------------------------

def test_j1_three_403s_per_route(w6: _W6) -> None:
    """cs and fop get 403 on /investigations, /approve, /decline."""
    res = _run6(w6)
    r = _rows(res)
    assert r.get("item-6-inv-cs-403") == "PASS", r
    assert r.get("item-6-inv-fop-403") == "PASS", r
    assert r.get("item-6-approve-cs-403") == "PASS", r
    assert r.get("item-6-approve-fop-403") == "PASS", r
    assert r.get("item-6-decline-cs-403") == "PASS", r
    assert r.get("item-6-decline-fop-403") == "PASS", r


# ---------------------------------------------------------------------------
# J2: each eng control — exact status and error text
# ---------------------------------------------------------------------------

def test_j2_eng_inv_202_with_investigation_id(w6: _W6) -> None:
    """eng POST /investigations → 202 with an investigation_id string."""
    res = _run6(w6)
    r = _rows(res)
    assert r.get("item-6-inv-eng-202") == "PASS", r
    # The detail contains the investigation_id
    d = _detail(res)
    assert "id=" in (d.get("item-6-inv-eng-202") or "")


def test_j2_eng_approve_400_population_scoped(w6: _W6) -> None:
    """eng POST /approve → 400 with 'is not valid for a population-scoped Finding'."""
    res = _run6(w6)
    r = _rows(res)
    assert r.get("item-6-approve-eng-400") == "PASS", r
    d = _detail(res)
    assert "is not valid for a population-scoped Finding" in (d.get("item-6-approve-eng-400") or "")


def test_j2_eng_decline_400_fi_queue_only(w6: _W6) -> None:
    """eng POST /decline → 400 with 'decline is only for FI queue items'."""
    res = _run6(w6)
    r = _rows(res)
    assert r.get("item-6-decline-eng-400") == "PASS", r
    d = _detail(res)
    assert "decline is only for FI queue items" in (d.get("item-6-decline-eng-400") or "")


# ---------------------------------------------------------------------------
# J3: no Action row for the plant
# ---------------------------------------------------------------------------

def test_j3_no_action_row_for_plant(w6: _W6) -> None:
    """The approve and decline calls create no Action row (both return 400)."""
    res = _run6(w6)
    r = _rows(res)
    assert r.get("item-6-no-action-row") == "PASS", r


# ---------------------------------------------------------------------------
# J4: cleanup confirm-by-read — queue empty, rows absent, cap absent (was absent)
# ---------------------------------------------------------------------------

def test_j4_cleanup_confirms_all_clean_cap_was_absent(w6: _W6) -> None:
    """With no pre-existing cap item: cleanup removes all traces; confirms clean."""
    res = _run6(w6)
    r = _rows(res)
    assert r.get("item-6-confirm-queue-attrs") == "PASS", r
    assert r.get("item-6-confirm-queue-receive") == "PASS", r
    assert r.get("item-6-confirm-inv-absent") == "PASS", r
    assert r.get("item-6-confirm-lock-absent") == "PASS", r
    assert r.get("item-6-confirm-plant-absent") == "PASS", r
    assert r.get("item-6-confirm-cap-restored") == "PASS", r
    # Three user-absent confirms (one per principal)
    user_rows = [s for lbl, s, _ in res.rows if lbl == "item-6-confirm-user-absent"]
    assert len(user_rows) == 3
    assert all(s == "PASS" for s in user_rows)


# ---------------------------------------------------------------------------
# J5: cap restored when count=n before
# ---------------------------------------------------------------------------

def test_j5_cap_restored_when_count_existed(w6: _W6) -> None:
    """When cap exists (count=2) before the run, cleanup restores count=2."""
    import datetime as _dt

    cap_key = f"cap#{_dt.datetime.now(_dt.timezone.utc).strftime('%Y-%m-%d')}"
    w6.inv_tbl.put_item(Item={"investigation_id": cap_key, "count": Decimal("2")})

    res = _run6(w6)
    r = _rows(res)
    assert r.get("item-6-confirm-cap-restored") == "PASS", r

    # Verify the actual cap value was restored to 2 (not left at 3 after the POST).
    cap_item = w6.inv_tbl.get_item(Key={"investigation_id": cap_key}).get("Item")
    assert cap_item is not None
    assert cap_item.get("count") == Decimal("2")


# ---------------------------------------------------------------------------
# J6: foreign queue message is not deleted and FAILs
# ---------------------------------------------------------------------------

def test_j6_foreign_queue_message_not_deleted_fails(w6: _W6) -> None:
    """A foreign queue message must not be deleted; the item must FAIL."""
    # Inject a foreign message via the Router when the handler fires its own SendMessage.
    # Returning None from the hook lets moto handle the original call too.
    injected: list[bool] = [False]

    def _sqs_hook(api: dict) -> dict | None:
        if not injected[0]:
            injected[0] = True
            # Remove the hook temporarily so the foreign send does not recurse.
            prev = w6.router._handlers.pop(("sqs", "SendMessage"), None)
            try:
                w6.sqs.send_message(
                    QueueUrl=w6.queue_url, MessageBody="FOREIGN-MESSAGE-BODY"
                )
            finally:
                if prev is not None:
                    w6.router._handlers[("sqs", "SendMessage")] = prev
        return None  # let moto handle the original send too

    w6.router.add("sqs", "SendMessage", _sqs_hook)
    try:
        res = _run6(w6)
    finally:
        w6.router._handlers.pop(("sqs", "SendMessage"), None)

    r = _rows(res)
    assert r.get("item-6-cleanup-no-foreign-msg") == "FAIL", r
    assert res.has_any_fail


# ---------------------------------------------------------------------------
# J7: SKIP when event source is Enabled
# ---------------------------------------------------------------------------

def test_j7_skip_when_event_source_enabled(w6: _W6) -> None:
    """If the investigation event source mapping is Enabled, item must SKIP."""
    # Enable the mapping
    w6.lam.update_event_source_mapping(UUID=w6.esm_uuid, Enabled=True)
    res = _run6(w6)
    assert res.has_any_skip
    skip_rows = [(lbl, s, d) for lbl, s, d in res.rows if s == "SKIP"]
    assert len(skip_rows) == 1
    lbl, _, detail = skip_rows[0]
    assert lbl == "item-6"
    assert "Disabled" in detail or "not Disabled" in detail


# ---------------------------------------------------------------------------
# J8: SKIP when queue non-empty at start
# ---------------------------------------------------------------------------

def test_j8_skip_when_queue_nonempty_at_start(w6: _W6) -> None:
    """A stray visible message before the first write must cause a SKIP."""
    # Send a message so the queue is non-empty before item_6 runs
    w6.sqs.send_message(QueueUrl=w6.queue_url, MessageBody="stray-message")
    res = _run6(w6)
    assert res.has_any_skip
    skip_rows = [(lbl, s, d) for lbl, s, d in res.rows if s == "SKIP"]
    assert any("item-6" == lbl for lbl, _, _ in skip_rows)
    # The stray message must still be there (we must not have consumed it)
    msgs = w6.sqs.receive_message(
        QueueUrl=w6.queue_url, MaxNumberOfMessages=1, WaitTimeSeconds=0
    ).get("Messages") or []
    assert msgs, "SKIP must not consume the stray message"


# ---------------------------------------------------------------------------
# J9: SKIP without --allow-writes
# ---------------------------------------------------------------------------

def test_j9_skip_without_allow_writes(w6: _W6) -> None:
    """Without allow_writes the item must SKIP without touching any AWS resource."""
    res = Results()
    lv._item_6(res, allow_writes=False, stage=STAGE, run_id="TESTSKIP")
    r = _rows(res)
    assert r.get("item-6") == "SKIP"
    # No DynamoDB write happened (findings table should still be empty)
    items = w6.findings_tbl.scan().get("Items", [])
    assert not items



# ---------------------------------------------------------------------------
# J10-J14: architect additions (403 bodies, own message, cap only when changed)
# ---------------------------------------------------------------------------

def _wrap_http(monkeypatch: pytest.MonkeyPatch, hook: Any) -> None:
    real = lv._item_6_http

    def fake(api_url: str, method: str, path: str, token: str, body: Any = None) -> Any:
        return hook(real, api_url, method, path, token, body)
    monkeypatch.setattr(lv, "_item_6_http", fake)


def test_j10_api_gateway_403_is_not_the_routes_refusal(w6: _W6, monkeypatch: pytest.MonkeyPatch) -> None:
    """A 403 without the route's own body (API Gateway's {"message": ...}) fails the row."""
    calls = {"n": 0}

    def hook(real: Any, *a: Any) -> Any:
        calls["n"] += 1
        if calls["n"] == 1:  # cs POST /investigations
            return 403, {"message": "Forbidden"}
        return real(*a)
    _wrap_http(monkeypatch, hook)
    r = _rows(_run6(w6))
    assert r["item-6-inv-cs-403"] == "FAIL"
    assert r["item-6-inv-fop-403"] == "PASS"


def test_j11_own_message_consumed_elsewhere_fails(w6: _W6, monkeypatch: pytest.MonkeyPatch) -> None:
    """If the control's message is gone before cleanup (something consumed it), that FAILs."""
    def hook(real: Any, api_url: str, method: str, path: str, token: str, body: Any) -> Any:
        out = real(api_url, method, path, token, body)
        if path == "investigations" and out[0] == 202:
            w6.sqs.purge_queue(QueueUrl=w6.queue_url)
        return out
    _wrap_http(monkeypatch, hook)
    r = _rows(_run6(w6))
    assert r["item-6-cleanup-own-message"] == "FAIL"


def test_j12_failed_control_post_leaves_the_cap_alone(w6: _W6, monkeypatch: pytest.MonkeyPatch) -> None:
    """eng POST answering 503 writes nothing; cleanup must not 'restore' a cap it never moved."""
    import datetime as _dt
    cap_key = f"cap#{_dt.datetime.now(_dt.timezone.utc).strftime('%Y-%m-%d')}"
    w6.inv_tbl.put_item(Item={"investigation_id": cap_key, "count": Decimal("4")})

    def hook(real: Any, api_url: str, method: str, path: str, token: str, body: Any) -> Any:
        if path == "investigations" and "eng" in _token_label(w6, token):
            return 503, {"error": "unavailable"}
        return real(api_url, method, path, token, body)
    _wrap_http(monkeypatch, hook)
    res = _run6(w6)
    r = _rows(res)
    assert r["item-6-inv-eng-202"] == "FAIL"
    assert r["item-6-confirm-cap-restored"] == "PASS"
    assert "item-6-cleanup-cap-restore" not in r
    assert w6.inv_tbl.get_item(Key={"investigation_id": cap_key})["Item"]["count"] == Decimal("4")


def test_j13_cap_moved_by_someone_else_too_fails(w6: _W6, monkeypatch: pytest.MonkeyPatch) -> None:
    """Another request counted the same day during the run: restoring blindly would be wrong."""
    import datetime as _dt
    cap_key = f"cap#{_dt.datetime.now(_dt.timezone.utc).strftime('%Y-%m-%d')}"
    w6.inv_tbl.put_item(Item={"investigation_id": cap_key, "count": Decimal("2")})

    def hook(real: Any, api_url: str, method: str, path: str, token: str, body: Any) -> Any:
        out = real(api_url, method, path, token, body)
        if path == "investigations" and out[0] == 202:
            w6.inv_tbl.update_item(Key={"investigation_id": cap_key},
                                   UpdateExpression="SET #c = #c + :one",
                                   ExpressionAttributeNames={"#c": "count"},
                                   ExpressionAttributeValues={":one": Decimal("1")})
        return out
    _wrap_http(monkeypatch, hook)
    r = _rows(_run6(w6))
    assert r["item-6-cleanup-cap-restore"] == "FAIL"
    assert r["item-6-confirm-cap-restored"] == "FAIL"


def test_j14_plant_uses_iso_expiry_and_is_removed(w6: _W6, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    def hook(real: Any, api_url: str, method: str, path: str, token: str, body: Any) -> Any:
        if not seen:
            seen.update(w6.findings_tbl.get_item(Key={"finding_id": "LIVETEST-FI-QUALITY-TESTRUN001"})["Item"])
        return real(api_url, method, path, token, body)
    _wrap_http(monkeypatch, hook)
    r = _rows(_run6(w6))
    import datetime as _dt
    assert isinstance(seen["expires_at"], str)
    assert _dt.datetime.fromisoformat(seen["expires_at"]) > _dt.datetime.now(_dt.timezone.utc)
    assert seen["scope"] == {"population_id": "meridian-motors"} and "recommended_action" not in seen
    assert r["item-6-confirm-plant-absent"] == "PASS"


def _token_label(w: _W6, token: str) -> str:
    import base64
    payload = token.split(".")[1]
    payload += "=" * (-len(payload) % 4)
    claims = json.loads(base64.urlsafe_b64decode(payload))
    return claims.get("email", "")


# ---------------------------------------------------------------------------
# J15-J17: survivors of the first battery (N8, N12, N13)
# ---------------------------------------------------------------------------

def test_j15_an_action_row_for_the_plant_fails(w6: _W6, monkeypatch: pytest.MonkeyPatch) -> None:
    """If a control wrote an Action for the plant, the item must say so."""
    def hook(real: Any, api_url: str, method: str, path: str, token: str, body: Any) -> Any:
        out = real(api_url, method, path, token, body)
        if path.endswith("/decline"):
            w6.actions_tbl.put_item(Item={"action_id": "AVX-ACT-livetest-x",
                                          "finding_id": "LIVETEST-FI-QUALITY-TESTRUN001"})
        return out
    _wrap_http(monkeypatch, hook)
    assert _rows(_run6(w6))["item-6-no-action-row"] == "FAIL"


def test_j16_attributes_say_empty_but_a_message_remains_fails(w6: _W6, monkeypatch: pytest.MonkeyPatch) -> None:
    """Queue attributes are approximate: the final ReceiveMessage is the check that sees it."""
    import boto3
    real_client = boto3.client

    def client(service: str, *a: Any, **kw: Any) -> Any:
        c = real_client(service, *a, **kw)
        if service == "sqs":
            real_attrs = c.get_queue_attributes
            state = {"drained": False}
            real_delete = c.delete_message

            def delete_message(**dkw: Any) -> Any:
                out = real_delete(**dkw)
                if not state["drained"]:
                    state["drained"] = True
                    c.send_message(QueueUrl=dkw["QueueUrl"], MessageBody="late-arrival")
                return out

            def get_queue_attributes(**gkw: Any) -> Any:
                out = real_attrs(**gkw)
                if state["drained"]:
                    out["Attributes"] = {k: "0" for k in out["Attributes"]}
                return out
            c.delete_message = delete_message
            c.get_queue_attributes = get_queue_attributes
        return c
    monkeypatch.setattr(boto3, "client", client)
    r = _rows(_run6(w6))
    assert r["item-6-confirm-queue-attrs"] == "PASS"
    assert r["item-6-confirm-queue-receive"] == "FAIL"


def test_j17_a_user_left_behind_fails(w6: _W6, monkeypatch: pytest.MonkeyPatch) -> None:
    import boto3
    real_client = boto3.client

    def client(service: str, *a: Any, **kw: Any) -> Any:
        c = real_client(service, *a, **kw)
        if service == "cognito-idp":
            c.admin_delete_user = lambda **_: None  # the delete "succeeds" but nothing goes
        return c
    monkeypatch.setattr(boto3, "client", client)
    r = _rows(_run6(w6))
    assert r["item-6-confirm-user-absent"] == "FAIL"


# ---------------------------------------------------------------------------
# J18-J21: review cycle 1 W5, S2, S5, S6
# ---------------------------------------------------------------------------

def _today_cap() -> str:
    import datetime as _dt
    return f"cap#{_dt.datetime.now(_dt.timezone.utc).strftime('%Y-%m-%d')}"


def test_j18_plant_put_raising_leaves_the_real_cap_alone(w6: _W6, monkeypatch: pytest.MonkeyPatch) -> None:
    """W5: nothing was snapshotted, so a real counter of 1 must not be taken for ours."""
    w6.inv_tbl.put_item(Item={"investigation_id": _today_cap(), "count": Decimal("1")})
    import boto3
    real_resource = boto3.resource

    def resource(service: str, *a: Any, **kw: Any) -> Any:
        r = real_resource(service, *a, **kw)
        if service == "dynamodb":
            real_table = r.Table

            def Table(name: str) -> Any:
                t = real_table(name)
                if name == FINDINGS_TABLE:
                    def put_item(**_: Any) -> Any:
                        raise RuntimeError("plant put failed")
                    t.put_item = put_item
                return t
            r.Table = Table
        return r
    monkeypatch.setattr(boto3, "resource", resource)
    res = Results()
    with pytest.raises(RuntimeError, match="plant put failed"):
        lv._item_6(res, allow_writes=True, stage=STAGE, run_id="TESTRUN001")
    r = _rows(res)
    assert w6.inv_tbl.get_item(Key={"investigation_id": _today_cap()})["Item"]["count"] == Decimal("1")
    assert r["item-6-confirm-cap-restored"] == "PASS"
    assert "item-6-cleanup-cap-restore" not in r


def test_j19_lost_202_is_cleaned_up_through_the_lock(w6: _W6, monkeypatch: pytest.MonkeyPatch) -> None:
    """S5: the route succeeded but the client timed out: cleanup finds the row by the lock."""
    def hook(real: Any, api_url: str, method: str, path: str, token: str, body: Any) -> Any:
        out = real(api_url, method, path, token, body)
        if path == "investigations" and out[0] == 202:
            return 0, {"_error": "TimeoutError: timed out"}
        return out
    _wrap_http(monkeypatch, hook)
    r = _rows(_run6(w6))
    assert r["item-6-inv-eng-202"] == "FAIL"
    for row in ("item-6-cleanup-own-message", "item-6-confirm-queue-attrs", "item-6-confirm-queue-receive",
                "item-6-confirm-inv-absent", "item-6-confirm-lock-absent", "item-6-confirm-cap-restored"):
        assert r.get(row) == "PASS", (row, r)
    assert w6.inv_tbl.get_item(Key={"investigation_id": _today_cap()}).get("Item") is None


def test_j20_an_enabled_dlq_mapping_is_not_the_investigation_source(w6: _W6) -> None:
    """S2: only the main queue's mapping gates the item (suffix match, not substring)."""
    dlq = w6.sqs.create_queue(QueueName=f"vsa-{STAGE}-avx-investigations-dlq")["QueueUrl"]
    dlq_arn = w6.sqs.get_queue_attributes(QueueUrl=dlq, AttributeNames=["QueueArn"])["Attributes"]["QueueArn"]
    w6.lam.create_event_source_mapping(EventSourceArn=dlq_arn, FunctionName=TIER2_FN, Enabled=True)
    r = _rows(_run6(w6))
    assert "item-6" not in r, r          # not skipped
    assert r["item-6-inv-eng-202"] == "PASS"


@pytest.mark.parametrize("hh,mm", [(23, 55), (0, 5)])
def test_j21_refuses_near_midnight(w6: _W6, monkeypatch: pytest.MonkeyPatch, hh: int, mm: int) -> None:
    """S6: the cap key's day must not change between the snapshot and the POST."""
    import datetime as _dt
    monkeypatch.setattr(lv, "_utc_now", lambda: _dt.datetime(2026, 10, 17, hh, mm, tzinfo=_dt.timezone.utc))
    assert _rows(_run6(w6)) == {"item-6": "SKIP"}


def test_j22_request_timeout_is_a_status_not_an_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    """S5: a client timeout returns status 0 so the run reaches its cleanup."""
    import urllib.request

    def urlopen(*_a: Any, **kw: Any) -> Any:
        assert kw.get("timeout") == 30
        raise TimeoutError("timed out")
    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    status, body = lv._item_6_http("https://example.invalid/staging/", "POST", "investigations", "tok", {"a": 1})
    assert status == 0 and "TimeoutError" in body["_error"]



# ---------------------------------------------------------------------------
# J23-J30: review cycle 2 (W1, W2, W3, W6, W7, W8)
# ---------------------------------------------------------------------------

def test_j23_deletes_that_did_nothing_fail_the_confirms(w6: _W6) -> None:
    """W3: the confirm reads are consistent, so a row left behind is seen right after."""
    _NOOP_DELETES.update({INV_TABLE, FINDINGS_TABLE})
    r = _rows(_run6(w6))
    assert r.get("item-6-confirm-inv-absent") == "FAIL"
    assert r.get("item-6-confirm-lock-absent") == "FAIL"
    assert r.get("item-6-confirm-plant-absent") == "FAIL"


def _inject_foreign_on_send(w6: _W6) -> None:
    """A real request's message arrives with the control's (as J6)."""
    injected = [False]

    def _sqs_hook(api: dict) -> dict | None:
        if not injected[0]:
            injected[0] = True
            prev = w6.router._handlers.pop(("sqs", "SendMessage"), None)
            try:
                w6.sqs.send_message(QueueUrl=w6.queue_url, MessageBody="FOREIGN-MESSAGE-BODY")
            finally:
                if prev is not None:
                    w6.router._handlers[("sqs", "SendMessage")] = prev
        return None
    w6.router.add("sqs", "SendMessage", _sqs_hook)


def test_j24_a_foreign_message_is_received_once(w6: _W6, monkeypatch: pytest.MonkeyPatch) -> None:
    """W1: maxReceiveCount is 2, so the cleanup may receive a real request's message once."""
    import boto3
    monkeypatch.setattr(lv, "_SQS_FOREIGN_HOLD_S", 1)  # becomes visible again during the poll
    real_client = boto3.client
    seen: list[tuple[Any, list[str]]] = []

    def client(service: str, *a: Any, **kw: Any) -> Any:
        c = real_client(service, *a, **kw)
        if service == "sqs":
            real_receive = c.receive_message

            def receive_message(**rkw: Any) -> Any:
                out = real_receive(**rkw)
                seen.append((rkw.get("VisibilityTimeout"), [m["Body"] for m in out.get("Messages") or []]))
                return out
            c.receive_message = receive_message
        return c
    monkeypatch.setattr(boto3, "client", client)
    _inject_foreign_on_send(w6)
    try:
        res = _run6(w6)
    finally:
        w6.router._handlers.pop(("sqs", "SendMessage"), None)
    r, d = _rows(res), _detail(res)
    assert sum(bodies.count("FOREIGN-MESSAGE-BODY") for _, bodies in seen) == 1, seen
    drain = [vt for vt, _ in seen if vt != 5]
    assert drain and all(vt == lv._SQS_FOREIGN_HOLD_S for vt in drain), seen
    assert r.get("item-6-cleanup-no-foreign-msg") == "FAIL"
    assert r.get("item-6-confirm-queue-receive") == "FAIL" and d.get("item-6-confirm-queue-receive", "").startswith("not run")
    assert r.get("item-6-cleanup-own-message") == "PASS"


def test_j24b_the_foreign_hold_covers_the_cleanup() -> None:
    """W1: the hold outlasts every wait left in the cleanup (6 drain polls, the attribute
    poll, the final receive), so the drain can't receive a foreign message twice."""
    fresh = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(fresh)
    rest = 6 * fresh._SQS_DRAIN_WAIT_S + fresh._QUEUE_EMPTY_POLL_S + fresh._SQS_FINAL_WAIT_S
    assert fresh._SQS_FOREIGN_HOLD_S > rest, (fresh._SQS_FOREIGN_HOLD_S, rest)


def test_j25_a_failing_receive_still_removes_everything(w6: _W6, monkeypatch: pytest.MonkeyPatch) -> None:
    """W2: an error in the queue drain is that step's FAIL; the deletes after it still run."""
    import boto3
    real_client = boto3.client

    def client(service: str, *a: Any, **kw: Any) -> Any:
        c = real_client(service, *a, **kw)
        if service == "sqs":
            def receive_message(**_: Any) -> Any:
                raise RuntimeError("receive denied")
            c.receive_message = receive_message
        return c
    monkeypatch.setattr(boto3, "client", client)
    res = Results()
    try:
        lv._item_6(res, allow_writes=True, stage=STAGE, run_id="TESTRUN001")
    except Exception as exc:  # noqa: BLE001
        raise AssertionError(f"the cleanup raised instead of recording a FAIL: {exc!r}") from exc
    r = _rows(res)
    assert r.get("item-6-cleanup-drain") == "FAIL"
    assert w6.findings_tbl.get_item(Key={"finding_id": "LIVETEST-FI-QUALITY-TESTRUN001"}).get("Item") is None
    assert w6.inv_tbl.get_item(Key={"investigation_id": "lock#LIVETEST-TESTRUN001"}).get("Item") is None
    left = [i for i in w6.inv_tbl.scan()["Items"] if not str(i["investigation_id"]).startswith("cap#")]
    assert left == [], left


def test_j26_an_unreadable_lock_leaves_the_cap_and_says_so(w6: _W6) -> None:
    """W2: whether the control counted is unknown, so the cap is left alone with a FAIL; the
    row, lock and plant are still removed."""
    _RAISE_ON_GET.add("lock#LIVETEST-TESTRUN001")
    res = Results()
    try:
        lv._item_6(res, allow_writes=True, stage=STAGE, run_id="TESTRUN001")
    except Exception as exc:  # noqa: BLE001
        raise AssertionError(f"the cleanup raised instead of recording a FAIL: {exc!r}") from exc
    r = _rows(res)
    assert r.get("item-6-cleanup-lock-read") == "FAIL"
    assert r.get("item-6-cleanup-cap-restore") == "FAIL"
    assert r.get("item-6-confirm-cap-restored") == "FAIL"
    assert w6.inv_tbl.get_item(Key={"investigation_id": _today_cap()})["Item"]["count"] == Decimal("1")
    assert w6.findings_tbl.get_item(Key={"finding_id": "LIVETEST-FI-QUALITY-TESTRUN001"}).get("Item") is None
    assert w6.inv_tbl.get_item(Key={"investigation_id": "lock#LIVETEST-TESTRUN001"}).get("Item") is None
    left = [i for i in w6.inv_tbl.scan()["Items"] if not str(i["investigation_id"]).startswith("cap#")]
    assert left == [], left


def test_j27_a_signal_during_cleanup_waits_for_it(w6: _W6) -> None:
    """W6: SIGTERM during the first delete: every later delete runs, then the run stops."""
    import signal
    old = signal.getsignal(signal.SIGINT), signal.getsignal(signal.SIGTERM)
    _SIGTERM_ON_DELETE.add(INV_TABLE)
    lv._install_signal_handlers()
    try:
        with pytest.raises(SystemExit) as ei:
            _run6(w6)
    finally:
        signal.signal(signal.SIGINT, old[0])
        signal.signal(signal.SIGTERM, old[1])
    assert ei.value.code == 143
    assert w6.findings_tbl.get_item(Key={"finding_id": "LIVETEST-FI-QUALITY-TESTRUN001"}).get("Item") is None
    assert w6.inv_tbl.get_item(Key={"investigation_id": "lock#LIVETEST-TESTRUN001"}).get("Item") is None
    assert w6.inv_tbl.get_item(Key={"investigation_id": _today_cap()}).get("Item") is None


@pytest.mark.parametrize("route", ["approve", "decline"])
@pytest.mark.parametrize("who", ["cs", "fop"])
def test_j28_a_403_that_isnt_the_routes_refusal_fails(w6: _W6, monkeypatch: pytest.MonkeyPatch,
                                                     route: str, who: str) -> None:
    """W7: only the route's own scope refusal counts on approve and decline."""
    def hook(real: Any, api_url: str, method: str, path: str, token: str, body: Any) -> Any:
        if path.endswith(f"/{route}") and f"-{who}-" in _token_label(w6, token):
            return 403, {"message": "Forbidden"}
        return real(api_url, method, path, token, body)
    _wrap_http(monkeypatch, hook)
    r = _rows(_run6(w6))
    assert r.get(f"item-6-{route}-{who}-403") == "FAIL"
    others = [k for k in r if k.endswith("-403") and k != f"item-6-{route}-{who}-403"]
    assert others and all(r[k] == "PASS" for k in others), r


@pytest.mark.parametrize("route", ["approve", "decline"])
def test_j29_eng_needs_the_routes_own_400(w6: _W6, monkeypatch: pytest.MonkeyPatch, route: str) -> None:
    """W8: eng's control is the population-scope / FI-queue refusal, not any 400."""
    def hook(real: Any, api_url: str, method: str, path: str, token: str, body: Any) -> Any:
        if path.endswith(f"/{route}") and "-eng-" in _token_label(w6, token):
            return 400, {"error": "invalid JSON body"}
        return real(api_url, method, path, token, body)
    _wrap_http(monkeypatch, hook)
    r = _rows(_run6(w6))
    assert r.get(f"item-6-{route}-eng-400") == "FAIL"
    other = "decline" if route == "approve" else "approve"
    assert r.get(f"item-6-{other}-eng-400") == "PASS"


def test_j30_a_user_whose_setup_failed_is_still_confirmed_absent(w6: _W6, monkeypatch: pytest.MonkeyPatch) -> None:
    """S3: a user created before a later setup step raised is confirmed absent too."""
    import boto3
    real_client = boto3.client
    created: list[str] = []

    def client(service: str, *a: Any, **kw: Any) -> Any:
        c = real_client(service, *a, **kw)
        if service == "cognito-idp":
            real_create, real_add = c.admin_create_user, c.admin_add_user_to_group

            def admin_create_user(**ckw: Any) -> Any:
                created.append(ckw["Username"])
                return real_create(**ckw)

            def admin_add_user_to_group(**gkw: Any) -> Any:
                if gkw["GroupName"] == "fleet-operator":
                    raise RuntimeError("group missing")
                return real_add(**gkw)
            c.admin_create_user = admin_create_user
            c.admin_add_user_to_group = admin_add_user_to_group
        return c
    monkeypatch.setattr(boto3, "client", client)
    res = Results()
    with pytest.raises(RuntimeError, match="group missing"):
        lv._item_6(res, allow_writes=True, stage=STAGE, run_id="TESTRUN001")
    confirmed = [d for label, s, d in res.rows if label == "item-6-confirm-user-absent" and s == "PASS"]
    assert len(created) == 3 and all(any(u in d for d in confirmed) for u in created), (created, res.rows)



# ---------------------------------------------------------------------------
# J31-J35: security review cycle 1 (W1 deletes retried + stale users reaped, W2 no secrets
# in the output, S1 every consumer of the queue)
# ---------------------------------------------------------------------------

@pytest.fixture()
def _no_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(lv, "_USER_DELETE_BACKOFF_S", 0)


def test_j31_a_throttled_user_delete_is_retried(w6: _W6, monkeypatch: pytest.MonkeyPatch,
                                                _no_backoff: None) -> None:
    import boto3
    from botocore.exceptions import ClientError
    real_client = boto3.client
    calls: dict[str, int] = {}

    def client(service: str, *a: Any, **kw: Any) -> Any:
        c = real_client(service, *a, **kw)
        if service == "cognito-idp":
            real_delete = c.admin_delete_user

            def admin_delete_user(**dkw: Any) -> Any:
                calls[dkw["Username"]] = calls.get(dkw["Username"], 0) + 1
                if calls[dkw["Username"]] == 1:
                    raise ClientError({"Error": {"Code": "TooManyRequestsException", "Message": "slow"}},
                                      "AdminDeleteUser")
                return real_delete(**dkw)
            c.admin_delete_user = admin_delete_user
        return c
    monkeypatch.setattr(boto3, "client", client)
    res = _run6(w6)
    confirms = [s for label, s, _ in res.rows if label == "item-6-confirm-user-absent"]
    assert confirms == ["PASS"] * 3, res.rows
    assert len(calls) == 3 and all(n == 2 for n in calls.values()), calls
    assert not [u for u in w6.pool.users.values() if u["attrs"]["email"].startswith("livetest-fi-quality-")]


def _old_user(w6: _W6, email: str, age: Any) -> str:
    import datetime as _dt
    out = w6.pool.admin_create_user({"UserPoolId": w6.pool.pool_id, "Username": email,
                                     "UserAttributes": [{"Name": "email", "Value": email}]})
    sub = out["User"]["Username"]
    for u in w6.pool.users.values():
        if u["attrs"]["email"] == email:
            u["created"] = _dt.datetime.now(_dt.timezone.utc) - age
            sub = u["sub"]
    return sub


def test_j32_left_over_users_are_reaped_and_nothing_else(w6: _W6, _no_backoff: None) -> None:
    import datetime as _dt
    stale = "livetest-fi-quality-eng-1760000000-abc123@example.invalid"
    recent = "livetest-fi-quality-cs-1760000001-def456@example.invalid"
    other = "livetest-fi-quality-manual@example.com"
    _old_user(w6, stale, _dt.timedelta(hours=2))
    _old_user(w6, recent, _dt.timedelta(minutes=5))
    _old_user(w6, other, _dt.timedelta(days=3))
    res = _run6(w6)
    r, d = _rows(res), _detail(res)
    left = {u["attrs"]["email"] for u in w6.pool.users.values()}
    assert stale not in left
    assert recent in left and other in left
    assert r.get("item-6-reap-stale-users") == "PASS" and d.get("item-6-reap-stale-users", "").startswith("1 ")


def test_j33_a_reap_that_cannot_delete_fails(w6: _W6, monkeypatch: pytest.MonkeyPatch,
                                            _no_backoff: None) -> None:
    import boto3
    import datetime as _dt
    from botocore.exceptions import ClientError
    stale = "livetest-fi-quality-fop-1760000000-abc123@example.invalid"
    sub = _old_user(w6, stale, _dt.timedelta(hours=2))
    real_client = boto3.client

    def client(service: str, *a: Any, **kw: Any) -> Any:
        c = real_client(service, *a, **kw)
        if service == "cognito-idp":
            real_delete = c.admin_delete_user

            def admin_delete_user(**dkw: Any) -> Any:
                if dkw["Username"] in (stale, sub):
                    raise ClientError({"Error": {"Code": "InternalErrorException", "Message": "x"}},
                                      "AdminDeleteUser")
                return real_delete(**dkw)
            c.admin_delete_user = admin_delete_user
        return c
    monkeypatch.setattr(boto3, "client", client)
    r = _rows(_run6(w6))
    assert r.get("item-6-reap-stale-users") == "FAIL"


def test_j34_no_token_or_password_reaches_the_output(w6: _W6, monkeypatch: pytest.MonkeyPatch,
                                                    capsys: pytest.CaptureFixture[str]) -> None:
    """Security W2: the ephemeral users' passwords and id tokens are never printed."""
    import boto3
    real_client = boto3.client
    secrets_seen: list[str] = []

    def client(service: str, *a: Any, **kw: Any) -> Any:
        c = real_client(service, *a, **kw)
        if service == "cognito-idp":
            real_set, real_auth = c.admin_set_user_password, c.initiate_auth

            def admin_set_user_password(**pkw: Any) -> Any:
                secrets_seen.append(pkw["Password"])
                return real_set(**pkw)

            def initiate_auth(**akw: Any) -> Any:
                out = real_auth(**akw)
                secrets_seen.append(out["AuthenticationResult"]["IdToken"])
                return out
            c.admin_set_user_password = admin_set_user_password
            c.initiate_auth = initiate_auth
        return c
    monkeypatch.setattr(boto3, "client", client)
    res = _run6(w6)
    res.summary("TESTRUN001")
    cap = capsys.readouterr()
    assert len(secrets_seen) == 6, len(secrets_seen)
    leaked = [s[:12] for s in secrets_seen if s in cap.out or s in cap.err]
    assert leaked == [], leaked


def test_j35_another_enabled_consumer_of_the_queue_skips(w6: _W6) -> None:
    """Security S1: a second function's enabled mapping on the queue would process the control."""
    import boto3
    role = boto3.client("iam", region_name=REGION).create_role(
        RoleName="item6-other-role", AssumeRolePolicyDocument="{}")["Role"]["Arn"]
    w6.lam.create_function(FunctionName="some-other-consumer", Runtime="python3.12", Role=role,
                           Handler="index.h", Code={"ZipFile": _zip_stub()})
    w6.lam.create_event_source_mapping(EventSourceArn=w6.queue_arn, FunctionName="some-other-consumer",
                                       Enabled=True)
    res = _run6(w6)
    assert [(label, s) for label, s, _ in res.rows] == [("item-6", "SKIP")], res.rows
    assert not w6.findings_tbl.scan().get("Items")
