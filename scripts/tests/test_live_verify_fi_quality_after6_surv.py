"""Survivor-mutant tests — W6 (M11) and W7 (N04).

M11: item-8b-queue-empty check hardcoded True (always passes).
     Property pinned: 8b FAILs when the queue holds a message after the repeat POST.

N04: item 2 root-cause lookup also matches pattern_group_id (wrong arm restored).
     Property pinned: item 2's root-cause lookup matches pattern_id only, never
     pattern_group_id.

Each test PASSES on HEAD and FAILS on its own assertion with the mutant applied.
Verify (git archive + patch -p1):
    M11: patch .kiro/specs/2026-10-05-fi-fleet-health-stage-d/64-records/rev2/mutants/M11-item8b-queue-empty-loose.diff
    N04: patch .kiro/specs/2026-10-05-fi-fleet-health-stage-d/64-records/rev2/mutants/N04-w4-group-arm-back.diff

Run:
    env -u AWS_PROFILE -u AWS_SESSION_TOKEN \\
        AWS_ACCESS_KEY_ID=x AWS_SECRET_ACCESS_KEY=x AWS_DEFAULT_REGION=us-west-2 \\
        ~/guidance-for-connected-vehicle-experience-on-aws/.venv/bin/python \\
        -m pytest scripts/tests/test_live_verify_fi_quality_after6_surv.py -v
"""
from __future__ import annotations

import datetime as dt
import importlib.util
import signal
import socket
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterator

import pytest

# ---------------------------------------------------------------------------
# Load the script module
# ---------------------------------------------------------------------------

_HERE = Path(__file__).resolve().parent
_SCRIPT = _HERE.parent / "live-verify-fi-quality.py"
_spec = importlib.util.spec_from_file_location("live_verify_fi_quality_surv", _SCRIPT)
assert _spec is not None
lv = importlib.util.module_from_spec(_spec)  # type: ignore[arg-type]
_spec.loader.exec_module(lv)  # type: ignore[union-attr]

STAGE = "staging"
_VALID_RUN_ID = "fi-quality-20261027T110000-surv1234"

# ---------------------------------------------------------------------------
# Repo presence guard (FiLoopWorld requires both repos)
# ---------------------------------------------------------------------------

_ADP_REPO_ENV = "FI_LOOP_WORLD_ADP_REPO"
_CMS_REPO_ENV = "FI_LOOP_WORLD_CMS_REPO"
_DEFAULT_ADP = Path.home() / "automotive-data-platform-on-aws"
_DEFAULT_CMS = Path.home() / "connected-mobility-guidance-on-aws"

_ADP_PRESENT = (Path(sys.modules.get("os", __import__("os")).environ.get(
    _ADP_REPO_ENV, str(_DEFAULT_ADP))) / "platform-foundation").exists()
_CMS_PRESENT = (Path(sys.modules.get("os", __import__("os")).environ.get(
    _CMS_REPO_ENV, str(_DEFAULT_CMS))) / "services").exists()

_REPOS_PRESENT = _ADP_PRESENT and _CMS_PRESENT
_SKIP_REASON = (
    "FiLoopWorld requires ~/automotive-data-platform-on-aws and "
    "~/connected-mobility-guidance-on-aws (or set FI_LOOP_WORLD_ADP_REPO / "
    "FI_LOOP_WORLD_CMS_REPO)"
)

if _REPOS_PRESENT:
    from scripts.tests.fi_loop_world.world import (
        FiLoopWorld,
        INVESTIGATIONS_QUEUE,
        INVESTIGATIONS_TABLE,
    )

# ---------------------------------------------------------------------------
# Import helpers from s1 test (Item2World and data helpers — no lv dependency)
# ---------------------------------------------------------------------------

from scripts.tests.test_live_verify_fi_quality_after6_s1 import (  # noqa: E402
    Item2World,
    _make_completed_inv,
    _make_finding,
    _AK_ROW_ACTIVE,
    _PAT_ROW,
)

# ---------------------------------------------------------------------------
# Autouse fixtures
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
# Helpers for M11 (item 8b / FiLoopWorld)
# ---------------------------------------------------------------------------


def _patch_guards_surv(monkeypatch: pytest.MonkeyPatch, utc_hour: int = 10) -> None:
    """Patch write-window guard, pre-checks, clock, and LIVE_TESTS for surv's lv."""
    monkeypatch.setattr(lv, "_after6_write_window_guard", lambda *_a, **_k: None)
    monkeypatch.setattr(lv, "_after6_pre_checks", lambda *_a, **_k: None)
    monkeypatch.setattr(
        lv, "_utc_now",
        lambda: dt.datetime(2026, 10, 27, utc_hour, 0, 0, tzinfo=dt.timezone.utc),
    )
    monkeypatch.setenv("LIVE_TESTS", "1")
    monkeypatch.setenv("FI_QUALITY_MANIFEST_DIR", "")


def _make_manifest_surv(run_id: str = _VALID_RUN_ID, **overrides: Any) -> dict:
    m: dict = {
        "run_id": run_id,
        "stage": STAGE,
        "started_at": "2026-10-27T11:00:00",
        "investigations": [],
        "item3_cap": None,
        "event_source_uuid": None,
        "event_source_disabled": False,
        "users": [],
        "items_passed": {},
        "item4_patterns": [],
        "item4_findings": [],
        "item4_actions": [],
        "item4_thread_versions": [],
        "item4_draft_rows": [],
        "item4_vin_objects": [],
        "item4_s3_lake_objects": [],
        "item4_recall_s3_objects": [],
        "item4_vin_bucket": None,
        "item4_thread_lake_bucket": None,
        "item4_issued_recalls_bucket": None,
        "item5_findings": [],
        "item5_actions": [],
    }
    m.update(overrides)
    return m


def _seed_base_world_surv(w: Any, *, run_id: str = _VALID_RUN_ID) -> tuple:
    """Seed one pattern + completed investigation for item 8b tests."""
    pat_group_id = "surv0012345abcdef"
    inv_id = "test-surv-inv-" + "a0b1c2d3"

    w.seed_pattern(
        pattern_id=pat_group_id,
        vin_list=["VIN00001234567890", "VIN00001234567891"],
        failure_mode_id="fm-surv-001",
        code_family="P0AB",
    )
    tbl = w.table(INVESTIGATIONS_TABLE)
    tbl.put_item(Item={
        "investigation_id": inv_id,
        "status": "completed",
        "pattern_group_id": pat_group_id,
        "pattern_id": pat_group_id,
        "requested_at": "2026-10-26T09:00:00Z",
        "requested_by": "sub-surv-001",
        "finding_id": f"FIND-{pat_group_id[:8]}",
    })
    tbl.put_item(Item={
        "investigation_id": f"lock#{pat_group_id}",
        "investigation_id_ref": inv_id,
    })
    return pat_group_id, inv_id


# ---------------------------------------------------------------------------
# Helpers for N04 (item 2 / Item2World) — patches surv's lv, not s1's
# ---------------------------------------------------------------------------


def _patch_item2_guards_surv(monkeypatch: pytest.MonkeyPatch, world: Item2World) -> None:
    """Patch item 2's guards to use surv's lv module."""
    pool_id = world.pool.pool_id
    monkeypatch.setattr(lv, "_resolve_pool_id", lambda _idp: pool_id)
    monkeypatch.setattr(lv, "_after6_write_window_guard", lambda *_a, **_k: None)
    monkeypatch.setattr(lv, "_after6_pre_checks", lambda *_a, **_k: None)
    monkeypatch.setenv("LIVE_TESTS", "1")

    _orig_poll = lv._poll_investigation

    def _fast_poll(api_url: str, inv_id: str, token: str,
                   *, max_s: int = 3, interval_s: float = 1.0) -> Any:
        return _orig_poll(api_url, inv_id, token, max_s=max_s, interval_s=interval_s)

    monkeypatch.setattr(lv, "_poll_investigation", _fast_poll)
    monkeypatch.setattr(
        lv, "_utc_now",
        lambda: dt.datetime(2026, 10, 27, 10, 0, 0, tzinfo=dt.timezone.utc),
    )

    from contextlib import contextmanager as _cm

    @_cm
    def _fake_principal(idp: Any, pid: Any, cid: Any, label: str, groups: list,
                        *, created_users: list | None = None,
                        fleet_ids: Any = None) -> Any:
        sub = f"test-{label}-surv-n04"
        user = {
            "sub": sub,
            "attrs": {"email": f"livetest-fi-quality-{label}-surv@example.invalid"},
            "groups": set(groups),
            "password": None,
            "status": "CONFIRMED",
            "created": dt.datetime.now(dt.timezone.utc),
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
            world.pool.users.pop(sub, None)

    monkeypatch.setattr(lv, "principal", _fake_principal)


# ---------------------------------------------------------------------------
# W6 — M11: item-8b-queue-empty check always True
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _REPOS_PRESENT, reason=_SKIP_REASON)
def test_m11_8b_queue_empty_always_true(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """W6 / M11 survivor: item-8b-queue-empty must FAIL when the queue holds a message.

    M11 replaces `not msgs` with `True` in the post-POST receive check.  The pre-check
    uses get_queue_attributes (not receive_message), so we leave the queue empty at
    pre-check time and inject a phantom message only into receive_message, simulating a
    phantom duplicate that appeared after the repeat POST.

    HEAD: receive_message returns [phantom] → `not msgs` = False
          → item-8b-queue-empty FAIL → assertion passes.
    M11:  check is hardcoded True regardless of msgs
          → item-8b-queue-empty PASS → assertion fails.
    """
    import botocore.client

    _patch_guards_surv(monkeypatch)
    run_id = _VALID_RUN_ID
    monkeypatch.setenv("FI_QUALITY_MANIFEST_DIR", str(tmp_path))

    with FiLoopWorld(auto_pump=False) as w:
        pat_group_id, inv_id = _seed_base_world_surv(w, run_id=run_id)

        # Queue is empty → get_queue_attributes (pre-check) returns 0 → pre-check passes.
        # Patch receive_message to return a phantom message to simulate a post-POST
        # phantom duplicate (the queue-empty pre-check uses get_queue_attributes, not
        # receive_message, so this does not affect the pre-check).
        _orig_api_call = botocore.client.BaseClient._make_api_call

        def _inject_phantom(self: Any, operation_name: str, api_params: Any) -> Any:
            if (operation_name == "ReceiveMessage"
                    and INVESTIGATIONS_QUEUE in api_params.get("QueueUrl", "")
                    and "dlq" not in api_params.get("QueueUrl", "")):
                return {
                    "Messages": [{
                        "MessageId": "phantom-m11-msg",
                        "ReceiptHandle": "phantom-m11-receipt",
                        "Body": "phantom-investigation-id-m11",
                        "MD5OfBody": "d41d8cd98f00b204e9800998ecf8427e",
                    }],
                    "ResponseMetadata": {"RequestId": "phantom", "HTTPStatusCode": 200},
                }
            return _orig_api_call(self, operation_name, api_params)

        monkeypatch.setattr(botocore.client.BaseClient, "_make_api_call", _inject_phantom)

        manifest = _make_manifest_surv(run_id, investigations=[{
            "pattern_group_id": pat_group_id,
            "investigation_id": inv_id,
            "status": "completed",
        }])

        old_int = signal.getsignal(signal.SIGINT)
        old_term = signal.getsignal(signal.SIGTERM)
        try:
            res = lv.Results()
            lv._item_8b(
                res,
                stage=STAGE,
                run_id=run_id,
                manifest=manifest,
                manifest_dir=str(tmp_path),
                allow_writes=True,
            )
        finally:
            signal.signal(signal.SIGINT, old_int)
            signal.signal(signal.SIGTERM, old_term)

    fail_labels = {r[0] for r in res.rows if r[1] == "FAIL"}
    # M11 changes `not msgs` → True; this pins the exact label that goes missing:
    assert "item-8b-queue-empty" in fail_labels, (
        f"item-8b-queue-empty must FAIL when receive_message returns a message "
        f"(M11 hardcodes True, erasing this FAIL). rows={res.rows}"
    )


# ---------------------------------------------------------------------------
# W7 — N04: item 2 root-cause lookup matches pattern_group_id arm restored
# ---------------------------------------------------------------------------


def test_n04_item2_groupid_arm_restored(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """W7 / N04 survivor: item 2 root-cause lookup must match pattern_id only.

    N04 adds `or r.get("pattern_group_id") == rc_cand_id` to the lookup.

    Setup: one planted pattern whose quality.root_cause.candidate_id equals the live
    row's pattern_group_id but NOT its pattern_id.  HEAD correctly finds no live row
    (cause_ok=False, 0/1 pass rate → item-2-cause-fm-rate FAIL).  N04 incorrectly
    finds the live row via pattern_group_id (cause_ok=True, 1/1 pass rate → PASS).

    HEAD: `next(r for r in live_rows if r["pattern_id"] == "P1", None)` → None
          → cause_ok=False → rate=0% < 80% → item-2-cause-fm-rate FAIL
          → assertion passes.
    N04:  also checks `r["pattern_group_id"] == "P1"` → finds n04child live row
          → cause_ok=True → rate=100% ≥ 80% → item-2-cause-fm-rate PASS
          → assertion fails.
    """
    import duckdb

    monkeypatch.setenv("LIVE_TESTS", "1")
    monkeypatch.setattr(
        lv, "_utc_now",
        lambda: dt.datetime(2026, 10, 27, 10, 0, 0, tzinfo=dt.timezone.utc),
    )

    # Live pattern row: pattern_id != pattern_group_id.
    # rc_cand_id will be "P1" (the group); HEAD finds no row with pattern_id=="P1".
    n04_live_row = {
        **_PAT_ROW,  # base row (code_family, cohort, rate_ratio, p_value, etc.)
        "pattern_id": "n04child01234567",   # distinct from group id
        "pattern_group_id": "P1",           # group id == candidate_id → N04 arm fires
    }

    # Investigation: locked at "P1", finding has candidate_id="P1" (the group id).
    inv = _make_completed_inv()
    inv["pattern_group_id"] = "P1"
    finding = _make_finding(
        finding_id=inv["finding_id"],
        root_cause_candidate_id="P1",   # group id, not child pattern_id
    )

    with Item2World(
        completed_inv=inv,
        region="us-west-2",
        stage="staging",
        account="123456789012",
        answer_key=[_AK_ROW_ACTIVE],
        pattern_rows=[n04_live_row],
        finding=finding,
    ) as w:
        _patch_item2_guards_surv(monkeypatch, w)

        # Write AK parquet (one active plant, pattern_id="P1")
        ak_path = str(tmp_path / "ak_n04.parquet")
        con = duckdb.connect()
        con.execute(
            "CREATE TABLE ak AS SELECT ? AS pattern_id, ? AS code_family, "
            "? AS cause_dimension, ? AS cause_value, ? AS planted_count, "
            "? AS start_date, ? AS failure_mode_id",
            [
                _AK_ROW_ACTIVE["pattern_id"],        # "P1"
                _AK_ROW_ACTIVE["code_family"],       # "P0AA"
                _AK_ROW_ACTIVE["cause_dimension"],   # "supplier_lot"
                _AK_ROW_ACTIVE["cause_value"],       # "SUPP1/LOT001"
                _AK_ROW_ACTIVE["planted_count"],     # 12
                _AK_ROW_ACTIVE["start_date"],        # "2026-01-01"
                _AK_ROW_ACTIVE["failure_mode_id"],   # "fm-suspension-001"
            ],
        )
        con.execute(f"COPY ak TO '{ak_path}' (FORMAT PARQUET)")
        con.close()

        manifest: dict = {
            "run_id": _VALID_RUN_ID,
            "stage": "staging",
            "investigations": [],
            "users": [],
            "item3_cap": None,
            "event_source_disabled": False,
            "items_passed": {},
        }
        res = lv.Results()
        lv._item_2(
            res,
            stage="staging",
            run_id=_VALID_RUN_ID,
            manifest=manifest,
            manifest_dir=str(tmp_path),
            allow_writes=True,
            answer_key=ak_path,
        )

    fail_labels = {r[0] for r in res.rows if r[1] == "FAIL"}
    # N04 adds pattern_group_id arm; this pins the aggregate rate check that breaks:
    assert "item-2-cause-fm-rate" in fail_labels, (
        f"item-2-cause-fm-rate must FAIL when candidate_id matches pattern_group_id "
        f"but not pattern_id (N04 wrong arm makes cause_ok=True). rows={res.rows}"
    )


# ---------------------------------------------------------------------------
# W8 — N07: item 8b's post-POST receive short-polls (WaitTimeSeconds=0)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _REPOS_PRESENT, reason=_SKIP_REASON)
def test_n07_8b_receive_uses_long_poll(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """W8 / N07 survivor: item 8b's post-POST receive must long-poll (WaitTimeSeconds 20).

    N07 changes WaitTimeSeconds=_SQS_LONG_POLL_WAIT_S to WaitTimeSeconds=0.
    The world's router records all_calls with the original params before the handler
    strips WaitTimeSeconds for moto; we find the ReceiveMessage call on the
    investigations queue and assert the wait value.

    HEAD:  call carries WaitTimeSeconds=20  → assertion passes.
    N07:   call carries WaitTimeSeconds=0   → assertion fails.
    """
    _patch_guards_surv(monkeypatch)
    run_id = _VALID_RUN_ID
    monkeypatch.setenv("FI_QUALITY_MANIFEST_DIR", str(tmp_path))

    with FiLoopWorld(auto_pump=False) as w:
        pat_group_id, inv_id = _seed_base_world_surv(w, run_id=run_id)

        manifest = _make_manifest_surv(run_id, investigations=[{
            "pattern_group_id": pat_group_id,
            "investigation_id": inv_id,
            "status": "completed",
        }])

        old_int = signal.getsignal(signal.SIGINT)
        old_term = signal.getsignal(signal.SIGTERM)
        try:
            res = lv.Results()
            lv._item_8b(
                res,
                stage=STAGE,
                run_id=run_id,
                manifest=manifest,
                manifest_dir=str(tmp_path),
                allow_writes=True,
            )
        finally:
            signal.signal(signal.SIGINT, old_int)
            signal.signal(signal.SIGTERM, old_term)

    # Locate the post-POST receive call on the investigations queue (not DLQ).
    # The world's SQS handler fires the same ReceiveMessage internally with
    # WaitTimeSeconds=0 (it models the wait on the clock instead), so
    # all_calls has TWO entries: [script's external call, world's internal call].
    # The script's call is first; use index 0 to get it.
    recv_calls = [
        params
        for _svc, op, params in w.router.all_calls
        if op == "ReceiveMessage"
        and INVESTIGATIONS_QUEUE in params.get("QueueUrl", "")
        and "dlq" not in params.get("QueueUrl", "")
    ]
    assert recv_calls, (
        "expected at least one ReceiveMessage call on the investigations queue in _item_8b"
    )
    wait_s = recv_calls[0].get("WaitTimeSeconds", 0)
    # N07 changes this to 0; pin the exact constant so a future rename is caught too:
    assert wait_s == lv._SQS_LONG_POLL_WAIT_S, (
        f"item-8b post-POST receive must long-poll with "
        f"WaitTimeSeconds={lv._SQS_LONG_POLL_WAIT_S} "
        f"(N07 sets it to 0). got WaitTimeSeconds={wait_s!r}"
    )


# ---------------------------------------------------------------------------
# W11 — N11: item 8b's queue/DLQ-empty pre-check before repeat POST removed
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _REPOS_PRESENT, reason=_SKIP_REASON)
def test_n11_8b_pre_check_fires_on_nonempty_queue(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """W11 / N11 survivor: item 8b must FAIL when the queue is not empty before the repeat POST.

    N11 changes `if _pre_vis != 0 or _pre_inf != 0:` to `if False:`,
    suppressing the pre-check entirely.  We seed a foreign message in the
    investigations queue before calling _item_8b so get_queue_attributes
    returns depth=1 for the queue.

    HEAD:  _pre_vis=1 → condition fires → item-8b-queue-empty-pre FAIL → assertion passes.
    N11:   if False → block never entered → item-8b-queue-empty-pre absent → assertion fails.
    """
    _patch_guards_surv(monkeypatch)
    run_id = _VALID_RUN_ID
    monkeypatch.setenv("FI_QUALITY_MANIFEST_DIR", str(tmp_path))

    with FiLoopWorld(auto_pump=False) as w:
        pat_group_id, inv_id = _seed_base_world_surv(w, run_id=run_id)

        # Seed a foreign message in the investigations queue so the pre-check depth check
        # sees ApproximateNumberOfMessages=1 and fires.
        sqs_i = w.internal("sqs")
        q_url = sqs_i.get_queue_url(QueueName=INVESTIGATIONS_QUEUE)["QueueUrl"]
        sqs_i.send_message(QueueUrl=q_url, MessageBody="foreign-pre-check-n11")

        manifest = _make_manifest_surv(run_id, investigations=[{
            "pattern_group_id": pat_group_id,
            "investigation_id": inv_id,
            "status": "completed",
        }])

        old_int = signal.getsignal(signal.SIGINT)
        old_term = signal.getsignal(signal.SIGTERM)
        try:
            res = lv.Results()
            lv._item_8b(
                res,
                stage=STAGE,
                run_id=run_id,
                manifest=manifest,
                manifest_dir=str(tmp_path),
                allow_writes=True,
            )
        finally:
            signal.signal(signal.SIGINT, old_int)
            signal.signal(signal.SIGTERM, old_term)

    fail_labels = {r[0] for r in res.rows if r[1] == "FAIL"}
    # N11 changes the guard to `if False:`; this pins the pre-check label that goes missing:
    assert "item-8b-queue-empty-pre" in fail_labels, (
        f"item-8b-queue-empty-pre must FAIL when the investigations queue holds a "
        f"message before the repeat POST "
        f"(N11 changes the guard to `if False:`, erasing this FAIL). rows={res.rows}"
    )
