"""Stream-B W4 fix: item 3b 202 → cleanup deletes its investigation, lock, Finding, slot.

Review cycle 3, task 6.4, fix round 3 — Stream B W4.

When the cap doesn't hold (item 3b gets 202), cleanup must:
  - delete the investigation (conditioned on item 3's principal sub),
  - delete its lock,
  - delete the job's Finding (looked up from the live investigation row), and
  - return the cap slot recorded by item 3b.

RED before fix: step 5 uses manifest["user_sub"] (item 2's sub) as the delete
condition; item 3b POSTs as its own user (different sub), so delete gets
ConditionalCheckFailed → investigation stays.

GREEN after fix: item 3b records its principal's sub in the investigations entry
(before POST); step 5 reads the live row to pick up finding_id; deletes the Finding
conditioned on subject_key; deletes investigation conditioned on item 3's sub;
step 3b returns the cap slot.

Mutation targets caught by this test:
  C3-35 (202 not recorded)       → item3b_entry is None → assert fires
  C3-39 (Finding delete on iid)  → ConditionalCheckFailed → finding stays → assert fires

Run (detached):
    nohup env -u AWS_PROFILE -u AWS_SESSION_TOKEN \\
        AWS_ACCESS_KEY_ID=x AWS_SECRET_ACCESS_KEY=x AWS_DEFAULT_REGION=us-west-2 \\
        ~/guidance-for-connected-vehicle-experience-on-aws/.venv/bin/python \\
        -m pytest scripts/tests/test_live_verify_fi_quality_after6_fx3b.py \\
        -v --no-header -p no:cacheprovider \\
        > /tmp/fi-health-d-64-fx3b/pytest.log 2>&1 &
"""
from __future__ import annotations

import datetime as dt
import importlib.util
import re
import signal
import socket
from pathlib import Path
from typing import Any, Iterator

import pytest

# ---------------------------------------------------------------------------
# Load the script module
# ---------------------------------------------------------------------------

_HERE = Path(__file__).resolve().parent
_SCRIPT = _HERE.parent / "live-verify-fi-quality.py"
_spec = importlib.util.spec_from_file_location("lv_fx3b_mod", _SCRIPT)
lv = importlib.util.module_from_spec(_spec)  # type: ignore[arg-type]
_spec.loader.exec_module(_mod := lv)  # type: ignore[union-attr]

# Import Item2World + helpers from s1 (moto-backed world, no FiLoopWorld repos needed).
_s1_spec = importlib.util.spec_from_file_location(
    "lv_s1_for_fx3b",
    _HERE / "test_live_verify_fi_quality_after6_s1.py",
)
_s1 = importlib.util.module_from_spec(_s1_spec)  # type: ignore[arg-type]
_s1_spec.loader.exec_module(_s1)  # type: ignore[union-attr]
Item2World = _s1.Item2World
_make_completed_inv = _s1._make_completed_inv
_PAT_ROW = _s1._PAT_ROW

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

STAGE   = "staging"
REGION  = "us-west-2"
ACCOUNT = "123456789012"

# Two distinct pattern ids so _pick_unlocked_real_pattern returns the right one.
# Alphabetically _COMPLETED_PAT_ID sorts before _PROBE_PAT_ID; the function tries
# them in order and skips the locked one.
_COMPLETED_PAT_ID = "abc123def456789a"   # item 3a repeat target (locked after item 2)
_PROBE_PAT_ID     = "f0099999abcdef99"   # item 3b cap probe (no lock → returned)

_VALID_RUN_ID = "fi-quality-20261028T120000-fx3b0001"

# ---------------------------------------------------------------------------
# Autouse fixtures (signal restore, loopback guard, manifest-dir leak guard)
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _restore_signals() -> Iterator[None]:
    old_int  = signal.getsignal(signal.SIGINT)
    old_term = signal.getsignal(signal.SIGTERM)
    old_hup  = signal.getsignal(signal.SIGHUP)
    yield
    signal.signal(signal.SIGINT,  old_int)
    signal.signal(signal.SIGTERM, old_term)
    signal.signal(signal.SIGHUP,  old_hup)


@pytest.fixture(autouse=True)
def _loopback_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    real_connect = socket.socket.connect

    def _connect(sock: socket.socket, address: Any) -> Any:
        host = address[0] if isinstance(address, tuple) else str(address)
        if host not in ("127.0.0.1", "::1", "localhost"):
            raise AssertionError(f"test reached real host {address!r}")
        return real_connect(sock, address)

    monkeypatch.setattr(socket.socket, "connect", _connect)


@pytest.fixture(autouse=True)
def _manifest_dir_guard() -> Iterator[None]:
    """No manifest entry must land in the live default dir during any test here.

    Autouse per brief: all new files must add this guard (the leak came from
    _after6_c2.py pointing FI_QUALITY_MANIFEST_DIR at "" rather than tmp_path).
    """
    default_dir = Path(lv._MANIFEST_DEFAULT_DIR)
    before = {p.name for p in default_dir.iterdir()} if default_dir.exists() else set()
    yield
    after = {p.name for p in default_dir.iterdir()} if default_dir.exists() else set()
    leaked = after - before
    assert not leaked, (
        f"Leaked {len(leaked)} manifest entries into live default dir "
        f"({default_dir}): {sorted(leaked)}"
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _patch_guards_fx3b(monkeypatch: pytest.MonkeyPatch, world: Any) -> None:
    """Patch write-window guard, pre-checks, poll, and principal for fx3b tests.

    Each principal() call receives a distinct sub (counter-based) so item 2's
    and item 3's users have different subs — the key property W4 tests.
    Claims dict yields {"sub": sub} so the script can store requested_by on
    the investigations entry (W4 fix requires this).
    """
    pool_id = world.pool.pool_id
    monkeypatch.setattr(lv, "_resolve_pool_id", lambda _idp: pool_id)
    monkeypatch.setattr(lv, "_after6_write_window_guard", lambda *_a, **_k: None)
    monkeypatch.setattr(lv, "_after6_pre_checks",          lambda *_a, **_k: None)

    _orig_poll = lv._poll_investigation

    def _fast_poll(api_url: str, inv_id: str, token: str,
                   *, max_s: float = 3, interval_s: float = 1) -> Any:
        return _orig_poll(api_url, inv_id, token, max_s=max_s, interval_s=interval_s)

    monkeypatch.setattr(lv, "_poll_investigation", _fast_poll)

    # Disable the ESM in the moto world so moto doesn't auto-invoke the Lambda
    # function when send_message is called (moto requires docker for that path).
    # The manifest has event_source_disabled=False so cleanup step 1 is skipped.
    import boto3 as _b3_esm_disable
    _b3_esm_disable.client("lambda", region_name=REGION).update_event_source_mapping(
        UUID=world.esm_uuid, Enabled=False
    )

    from contextlib import contextmanager as _cm

    _call_n: list[int] = [0]

    @_cm  # type: ignore[arg-type]
    def _fake_principal(
        idp: Any, pid: Any, cid: Any, label: str, groups: list[str],
        *, created_users: list[str] | None = None, **_kw: Any,
    ) -> Any:
        _call_n[0] += 1
        sub = f"test-{label}-fx3b-{_call_n[0]:03d}"
        user: dict[str, Any] = {
            "sub": sub,
            "attrs": {
                "email": (
                    f"livetest-fi-quality-{label}-fx3b-{_call_n[0]:03d}"
                    "@example.invalid"
                )
            },
            "groups":   set(groups),
            "password": None,
            "status":   "CONFIRMED",
            "created":  dt.datetime.now(dt.timezone.utc),
        }
        if sub not in world.pool.users:
            world.pool.users[sub] = user
        token    = world.pool.mint(user)
        username = user["attrs"]["email"]
        if created_users is not None:
            created_users.append(username)
        try:
            yield token, {"sub": sub}, username
        finally:
            world.pool.users.pop(sub, None)

    monkeypatch.setattr(lv, "principal", _fake_principal)


# ===========================================================================
# W4: item 3b 202 → cleanup deletes investigation, lock, Finding, returns slot
# ===========================================================================


def test_w4_item3b_202_cleanup_deletes_inv_lock_finding_returns_slot(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """W4 fix: when item 3b gets 202 (cap doesn't hold), cleanup must fully undo it.

    Specifically tested (all four must be true after cleanup):
      1. investigation row deleted from DDB
      2. lock row deleted from DDB
      3. job's Finding deleted from DDB (found via live consistent read of the row)
      4. cap slot marked returned in manifest

    RED before fix:
      - step 5 deletes with requested_by = manifest["user_sub"] (item 2's sub)
      - item 3b posted as its own user → different sub → ConditionalCheckFailed
      - investigation stays in DDB → assertion (1) fires

    GREEN after fix:
      - item 3b records its principal's sub in the investigations entry
      - step 5 uses rec["requested_by"] → matches the DDB row → delete succeeds
      - step 5 reads the live row before deleting → finds finding_id set by job
      - step 5 deletes the Finding conditioned on subject_key
      - step 3b returns the cap slot recorded by item 3b

    Mutation coverage:
      C3-35 (202 not recorded)      → item3b_entry is None → assert fires
      C3-39 (Finding on wrong key)  → ConditionalCheckFailed → finding stays → assert fires
    """
    monkeypatch.setenv("LIVE_TESTS", "1")
    monkeypatch.setenv("FI_QUALITY_MANIFEST_DIR", str(tmp_path))
    monkeypatch.setattr(
        lv, "_utc_now",
        lambda: dt.datetime.now(dt.timezone.utc).replace(
            hour=10, minute=0, second=0, microsecond=0),
    )

    # item 2's sub — set in manifest["user_sub"] but distinct from item 3's sub.
    item2_sub = "test-item2-sub-fx3b"

    # Pre-seeded completed investigation for item 3a's repeat test.
    # requested_by must match item2_sub so step 5 can also delete it.
    completed_inv = _make_completed_inv(
        inv_id="inv-fx3b-completed-001",
        finding_id="LIVETEST-finding-fx3b-001",
    )
    completed_inv["pattern_group_id"] = _COMPLETED_PAT_ID
    completed_inv["requested_by"]     = item2_sub

    with Item2World(
        completed_inv=completed_inv,
        region=REGION, stage=STAGE, account=ACCOUNT,
        answer_key=[],
        daily_cap=21,       # 20 < 21 → probe POST succeeds (202), not 429
        pattern_rows=[
            {**_PAT_ROW, "pattern_id": _COMPLETED_PAT_ID,
             "pattern_group_id": _COMPLETED_PAT_ID},
            # Second unlocked pattern for _pick_unlocked_real_pattern to return
            {**_PAT_ROW, "pattern_id": _PROBE_PAT_ID,
             "pattern_group_id": _PROBE_PAT_ID},
        ],
    ) as w:
        _patch_guards_fx3b(monkeypatch, w)

        import boto3
        ddb_r        = boto3.resource("dynamodb", region_name=REGION)
        inv_tbl      = ddb_r.Table(f"vsa-{STAGE}-avx-investigations")
        findings_tbl = ddb_r.Table(f"vsa-{STAGE}-avx-findings")

        # Build initial manifest: one completed investigation for item 3a.
        manifest: dict = {
            "run_id": _VALID_RUN_ID,
            "stage": STAGE,
            "user_sub": item2_sub,
            "investigations": [
                {
                    "pattern_group_id": _COMPLETED_PAT_ID,
                    "investigation_id": "inv-fx3b-completed-001",
                    "status": "completed",
                }
            ],
            "users": [],
            "item3_cap": None,
            "item4_patterns": [], "item4_findings": [], "item4_actions": [],
            "item4_thread_versions": [], "item4_draft_rows": [], "item4_vin_objects": [],
            "item4_s3_lake_objects": [], "item4_recall_s3_objects": [],
            "item5_findings": [], "item5_actions": [],
            "event_source_disabled": False, "event_source_uuid": None,
            "items_passed": {}, "cap_slots_taken": [],
        }

        # ── Step A: run item 3 ───────────────────────────────────────────────
        # 3a: repeat POST → 200 (same id)
        # 3b: cap probe POST → 202 (count=20 < daily_cap=21 → route allows it)
        res3 = lv.Results()
        lv._item_3(
            res3,
            stage=STAGE,
            run_id=_VALID_RUN_ID,
            manifest=manifest,
            manifest_dir=str(tmp_path),
            allow_writes=True,
        )

        # Sanity: cap-429 check must FAIL (got 202, not 429)
        fail_labels = {r[0] for r in res3.rows if r[1] == "FAIL"}
        assert "item-3-cap-429" in fail_labels, (
            f"Pre-condition: expected item-3-cap-429 FAIL. Rows: {res3.rows}"
        )

        # item 3b must have recorded an investigation entry for cleanup to target.
        item3b_entry = next(
            (e for e in manifest.get("investigations", [])
             if e.get("plant_id") == "item3-cap-probe"),
            None,
        )
        assert item3b_entry is not None, (
            f"W4: item 3b 202 must be recorded in manifest['investigations']. "
            f"Rows: {res3.rows}; investigations: {manifest.get('investigations')}"
        )
        iid3b = item3b_entry["investigation_id"]
        assert iid3b, "item3b_entry must carry investigation_id"

        # Investigation row must exist in DDB (created by the route on 202).
        assert inv_tbl.get_item(
            Key={"investigation_id": iid3b}, ConsistentRead=True
        ).get("Item") is not None, (
            f"Pre-condition: investigation {iid3b!r} must exist in DDB"
        )

        # ── Step B: simulate job completing — write finding_id onto the row ──
        # (The real Tier 2 job consumes the SQS message, runs inference, and
        # updates the investigation row with finding_id + status="completed".)
        finding_id_3b = f"LIVETEST-finding-fx3b-{iid3b[:8]}"
        inv_tbl.update_item(
            Key={"investigation_id": iid3b},
            UpdateExpression="SET finding_id = :fid, #s = :done",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":fid": finding_id_3b, ":done": "completed"},
        )
        # Seed the Finding row (subject_key = investigation_id per write.py).
        findings_tbl.put_item(Item={
            "finding_id": finding_id_3b,
            "subject_key": iid3b,
        })

        # ── Step C: run cleanup ──────────────────────────────────────────────
        # idp/pool_id omitted: step 6 (user deletion) is skipped; not under test here.
        res_cl = lv.Results()
        lv._after6_cleanup(
            res_cl,
            manifest=manifest,
            manifest_dir=str(tmp_path),
            stage=STAGE,
            mode="after-6",
        )

        # ── W4 assertions ────────────────────────────────────────────────────

        # 1. Investigation row deleted.
        assert inv_tbl.get_item(
            Key={"investigation_id": iid3b}, ConsistentRead=True
        ).get("Item") is None, (
            f"W4: item 3b investigation {iid3b!r} must be deleted by cleanup. "
            f"Cleanup rows: {res_cl.rows}"
        )

        # 2. Lock row deleted.
        assert inv_tbl.get_item(
            Key={"investigation_id": f"lock#{_PROBE_PAT_ID}"}, ConsistentRead=True
        ).get("Item") is None, (
            f"W4: lock for probe pattern must be deleted by cleanup. "
            f"Cleanup rows: {res_cl.rows}"
        )

        # 3. Job Finding deleted.
        assert findings_tbl.get_item(
            Key={"finding_id": finding_id_3b}, ConsistentRead=True
        ).get("Item") is None, (
            f"W4: job Finding {finding_id_3b!r} must be deleted by cleanup. "
            f"Cleanup rows: {res_cl.rows}"
        )

        # 4. Cap slot returned.
        slots = manifest.get("cap_slots_taken", [])
        item3b_slot = next(
            (s for s in slots if s.get("investigation_id") == iid3b), None
        )
        assert item3b_slot is not None, (
            f"W4: cap slot for item 3b 202 must be recorded in manifest. "
            f"cap_slots_taken={slots}"
        )
        assert item3b_slot.get("cap_slot_returned"), (
            f"W4: cap slot must be marked returned by cleanup. "
            f"slot={item3b_slot}; cleanup rows={res_cl.rows}"
        )


# ===========================================================================
# W7: after-6-check FAILs when items_passed does not record an after-6 item
# ===========================================================================


def test_w7_after6_check_refuses_rc1_task65(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Item 9 split to task 6.5 (2026-10-28): --part after-6-check must return 1
    immediately before any AWS call, printing that item 9 is task 6.5.

    W7 (items_passed gate) no longer applies to 6.4: after-6-check is 6.5's entry point.

    RED before split: after-6-check loaded a manifest, checked items_passed, ran 9b.
    GREEN after split: after-6-check refuses (rc=1) with a "6.5" message; no AWS call.
    """
    import contextlib
    import io as _io

    monkeypatch.setenv("LIVE_TESTS", "1")
    monkeypatch.setenv("FI_QUALITY_MANIFEST_DIR", str(tmp_path))

    buf = _io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = lv.main([
            "--stage", STAGE,
            "--part", "after-6-check",
        ])
    output = buf.getvalue()

    assert rc == 1, (
        f"after-6-check must return 1 (task 6.5 refusal); got rc={rc}.\n"
        f"Output:\n{output}"
    )
    assert "6.5" in output, (
        f"after-6-check must mention '6.5' in its refusal message; got: {output!r}"
    )


def test_c4w2_step5_row_read_error_is_a_fail_row_and_cleanup_goes_on(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Cycle 4 W2 (architect): step 5's read of an investigation's live row raising once is
    that row's FAIL; the delete still runs and the cleanup reaches the residue scan."""
    import botocore.client as _bc

    monkeypatch.setenv("LIVE_TESTS", "1")
    monkeypatch.setenv("FI_QUALITY_MANIFEST_DIR", str(tmp_path))
    item2_sub = "test-item2-sub-c4w2"
    completed_inv = _make_completed_inv(
        inv_id="inv-c4w2-completed-001", finding_id="LIVETEST-finding-c4w2-001")
    completed_inv["pattern_group_id"] = _COMPLETED_PAT_ID
    completed_inv["requested_by"] = item2_sub

    with Item2World(
        completed_inv=completed_inv, region=REGION, stage=STAGE, account=ACCOUNT,
        answer_key=[], daily_cap=20,
        pattern_rows=[{**_PAT_ROW, "pattern_id": _COMPLETED_PAT_ID,
                       "pattern_group_id": _COMPLETED_PAT_ID}],
    ) as w:
        _patch_guards_fx3b(monkeypatch, w)
        import boto3
        inv_tbl = boto3.resource("dynamodb", region_name=REGION).Table(
            f"vsa-{STAGE}-avx-investigations")
        raised: list[str] = []
        _orig = _bc.BaseClient._make_api_call

        def _api(self: Any, op: str, params: dict) -> Any:
            key = (params.get("Key") or {}).get("investigation_id")
            if isinstance(key, dict):
                key = key.get("S")
            if (op == "GetItem" and not raised and key == "inv-c4w2-completed-001"
                    and params.get("ConsistentRead")):
                raised.append(key)
                raise RuntimeError("injected: ProvisionedThroughputExceeded on step 5's read")
            return _orig(self, op, params)

        monkeypatch.setattr(_bc.BaseClient, "_make_api_call", _api)
        manifest: dict = {
            "run_id": _VALID_RUN_ID, "stage": STAGE, "user_sub": item2_sub,
            "investigations": [{"pattern_group_id": _COMPLETED_PAT_ID,
                                "investigation_id": "inv-c4w2-completed-001",
                                "status": "completed", "requested_by": item2_sub}],
            "users": [], "item3_cap": None,
            "item4_patterns": [], "item4_findings": [], "item4_actions": [],
            "item4_thread_versions": [], "item4_draft_rows": [], "item4_vin_objects": [],
            "item4_s3_lake_objects": [], "item4_recall_s3_objects": [],
            "item5_findings": [], "item5_actions": [],
            "event_source_disabled": False, "event_source_uuid": None,
            "items_passed": {}, "cap_slots_taken": [],
        }
        res = lv.Results()
        try:
            lv._after6_cleanup(res, manifest=manifest, manifest_dir=str(tmp_path),
                               stage=STAGE, mode="after-6")
        except Exception as exc:  # noqa: BLE001
            raise AssertionError(
                f"one DynamoDB error in step 5 must not end the cleanup: "
                f"{type(exc).__name__}: {exc}") from exc
        row_after = inv_tbl.get_item(
            Key={"investigation_id": "inv-c4w2-completed-001"}, ConsistentRead=True).get("Item")

    assert raised, "the injected read error never fired"
    labels = {r[0]: r[1] for r in res.rows}
    assert labels.get("after6-cleanup-read-inv-inv-c4w2") == "FAIL", f"rows={res.rows}"
    assert "after6-residue-scan" in labels, f"cleanup must reach the residue scan; rows={res.rows}"
    assert row_after is None, "the row delete must still run after the read error"


# ===========================================================================
# Item 9 split (2026-10-28): --part after-6 does not run item 9; task 6.5
# ===========================================================================


def test_s1_after6_never_calls_item9(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """--part after-6 must not POST LIVETEST-9- (item 9 is task 6.5, split 2026-10-28).

    _AFTER6_ITEMS does not include "9".  Run main() with --part after-6 and confirm
    no LIVETEST-9- appears in the output.
    """
    import contextlib
    import io as _io

    monkeypatch.setenv("LIVE_TESTS", "1")
    monkeypatch.setenv("FI_QUALITY_MANIFEST_DIR", str(tmp_path))

    # Not a world test: the stale-user reaper (security cycle 1 W1) has no fake Cognito here.
    monkeypatch.setattr(lv, "_reap_stale_users_step", lambda *a, **k: None)

    # Fake all real after-6 items (2/3/4/5/8b) so no AWS calls are needed.
    fns = dict(lv._ITEM_FNS)
    for n in ("2", "3", "4", "5", "8b"):
        fns[n] = lambda res, **_k: res.pass_("item-" + n, "faked")  # noqa: B023
    monkeypatch.setattr(lv, "_ITEM_FNS", fns)

    old_int = signal.getsignal(signal.SIGINT)
    old_term = signal.getsignal(signal.SIGTERM)
    buf = _io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            lv.main(["--stage", STAGE, "--part", "after-6", "--allow-writes"])
    finally:
        signal.signal(signal.SIGINT, old_int)
        signal.signal(signal.SIGTERM, old_term)

    # Belt-and-suspenders: assert no LIVETEST-9- pattern in output
    assert "LIVETEST-9-" not in buf.getvalue(), (
        "--part after-6 must not POST LIVETEST-9-; item 9 is task 6.5.\n"
        f"stdout: {buf.getvalue()!r}"
    )


def test_s1_after6_check_refuses_before_any_aws_call(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """--part after-6-check must return 1 immediately; no AWS call must be made.

    Item 9 is task 6.5 (split 2026-10-28).  Patch all AWS-touching functions
    to detect if they are called; assert none are called when main() runs
    --part after-6-check.
    """
    import contextlib
    import io as _io

    monkeypatch.setenv("LIVE_TESTS", "1")
    monkeypatch.setenv("FI_QUALITY_MANIFEST_DIR", str(tmp_path))

    aws_called: list[str] = []

    def _spy_cleanup(*a: Any, **k: Any) -> None:
        aws_called.append("_after6_cleanup")

    monkeypatch.setattr(lv, "_after6_cleanup", _spy_cleanup)

    buf = _io.StringIO()
    old_int = signal.getsignal(signal.SIGINT)
    old_term = signal.getsignal(signal.SIGTERM)
    try:
        with contextlib.redirect_stdout(buf):
            rc = lv.main(["--stage", STAGE, "--part", "after-6-check"])
    finally:
        signal.signal(signal.SIGINT, old_int)
        signal.signal(signal.SIGTERM, old_term)

    output = buf.getvalue()
    assert rc == 1, (
        f"--part after-6-check must return 1 (task 6.5 refusal); got rc={rc}.\n"
        f"Output:\n{output}"
    )
    assert "6.5" in output, (
        f"--part after-6-check must print '6.5' in its refusal; got: {output!r}"
    )
    assert not aws_called, (
        f"--part after-6-check must not make any AWS call; called: {aws_called}"
    )


@pytest.mark.parametrize("argv", [
    ["--items", "9", "--allow-writes"],
    ["--items", "8b,9", "--allow-writes"],
    ["--part", "after-6-check", "--run-id", _VALID_RUN_ID, "--allow-writes"],
])
def test_split_item9_unreachable_from_main_and_no_aws_call(
        argv: list[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Split (architect, 2026-10-28): item 9 is task 6.5, so no ``main()`` path may run it,
    including ``--items 9``; each is refused before any AWS API call."""
    import contextlib
    import io as _io
    import botocore.client as _bc

    monkeypatch.setenv("LIVE_TESTS", "1")
    monkeypatch.setenv("FI_QUALITY_MANIFEST_DIR", str(tmp_path))
    api_calls: list[str] = []
    def _no_api(self: Any, op: str, params: dict) -> Any:
        api_calls.append(op)
        raise AssertionError(f"AWS call {op} made")

    monkeypatch.setattr(_bc.BaseClient, "_make_api_call", _no_api)
    buf = _io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        rc = lv.main(["--stage", STAGE, *argv])
    assert rc == 1, f"must be refused with rc 1; rc={rc}\n{buf.getvalue()}"
    assert not api_calls, f"AWS calls made: {api_calls}"


# A run id that passes the script's own shape gate (security cycle 2 W1: the module's
# _VALID_RUN_ID does not, so cleanup-only stopped at the run-id refusal).
_SEC1_RUN_ID = "fi-quality-20261028T120000-abcdef01"


@pytest.mark.parametrize("mode", ["after-6", "cleanup-only"])
def test_sec1_w1_after6_and_cleanup_only_reap_stale_users(
        mode: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Security review cycle 1 W1 (architect): both write paths reap this script's own
    left-over temp users once, and a reaper error is a FAIL row that stops nothing else."""
    import contextlib
    import io as _io

    monkeypatch.setenv("LIVE_TESTS", "1")
    monkeypatch.setenv("FI_QUALITY_MANIFEST_DIR", str(tmp_path))
    calls: list[str] = []
    ran: list[str] = []

    def _reaper(res: Any, idp: Any, pool_id: str) -> None:
        calls.append(pool_id)
        raise RuntimeError("injected reaper error")

    monkeypatch.setattr(lv, "_reap_stale_users", _reaper)
    monkeypatch.setattr(lv, "_resolve_pool_id", lambda idp: "us-west-2_POOLTST")
    monkeypatch.setattr(lv, "_after6_cleanup", lambda *a, **k: ran.append("cleanup"))
    def _items(*a: Any, **k: Any) -> None:
        ran.append("items")
        # A real after-6 run always makes temp users; the reaper runs only when it did.
        k["manifest"].setdefault("users", []).append("livetest-fi-quality-eng-1-abcdef@example.invalid")

    monkeypatch.setattr(lv, "_run_items", _items)
    if mode == "after-6":
        argv = ["--stage", STAGE, "--part", "after-6", "--allow-writes"]
    else:
        lv._save_manifest(_SEC1_RUN_ID, {"run_id": _SEC1_RUN_ID, "stage": STAGE},
                          str(tmp_path))
        monkeypatch.setattr(lv, "_validate_cleanup_manifest", lambda *a, **k: None)
        argv = ["--stage", STAGE, "--cleanup-only", "--run-id", _SEC1_RUN_ID, "--allow-writes"]
    buf = _io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        rc = lv.main(argv)
    out = buf.getvalue()
    assert calls == ["us-west-2_POOLTST"], f"the reaper must run once on {mode}; calls={calls}\n{out}"
    assert "cleanup" in ran, f"the run's own cleanup must still run; ran={ran}\n{out}"
    if mode == "after-6":
        assert ran.index("items") < ran.index("cleanup"), f"order: items, cleanup, reaper; ran={ran}"
    assert re.search(r"\[FAIL\] after6-reap-stale-users", out), out
    assert rc == 1, f"a reaper FAIL makes the run rc 1; rc={rc}"
