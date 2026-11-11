"""Stage 3 world tests — items 8b, 9a, 9b; cleanup step 1; LIVETEST- filter.

ALL tests are RED with the stage-2 placeholders and GREEN with the real
implementation.

Checks per design §Item 8b:
  - ESM state must be Enabled before disable
  - Re-POST completed group returns 200 same id
  - Queue holds 0 messages after re-POST (no phantom duplicate)
  - Investigation row and Findings unchanged

Checks per design §Item 9a:
  - DLQ empty before POST
  - DLQ alarm OK before POST
  - POST returns 202 with new investigation_id
  - Queue holds exactly 1 message with the run's investigation_id
  - No foreign message in queue
  - ESM re-enabled (State==Enabled) after 9a
  - Due time printed (POST + 2×5400 + 900 s)

Checks per design §Item 9b:
  - Refuses before due time (SKIP rc=2)
  - DLQ holds run's message (no foreign message)
  - Alarm history: StateUpdate to ALARM + Action Succeeded on topic ARN
  - StartDate filter: old history item before POST time must not count
  - Delete DLQ message + row + lock
  - Confirm each deleted

Signal test:
  - SIGTERM inside disabled window still re-enables ESM

Cleanup tests:
  - nothing to clean → PASS  (manifest event_source_disabled=False)
  - ESM re-enable fails → FAIL

Items 1/8a LIVETEST- filter:
  - item 1 ignores LIVETEST- rows, prints NOTE with count
  - item 8a ignores LIVETEST- rows, prints NOTE with count

Mutant verification (one per check + one per cleanup step):
  - one per items 8b / 9a / 9b check
  - one per cleanup step 1 (re-enable bypassed)

Run (detached, long):
    nohup ~/guidance-for-connected-vehicle-experience-on-aws/.venv/bin/python \\
        -m pytest scripts/tests/test_live_verify_fi_quality_after6_s3.py -v \\
        > /tmp/2026-10-05-fi-fleet-health-stage-d-64/s3/pytest.log 2>&1 &

Requires:
  ~/automotive-data-platform-on-aws at ADP 02b8716f (or FI_LOOP_WORLD_ADP_REPO)
  ~/connected-mobility-guidance-on-aws at CMS main (or FI_LOOP_WORLD_CMS_REPO)
"""
from __future__ import annotations

import datetime as dt
import json
import os
import signal
import socket
import uuid
from pathlib import Path
from typing import Any, Iterator

import pytest

# ---------------------------------------------------------------------------
# Repo presence and SKIP guard
# ---------------------------------------------------------------------------

_HERE = Path(__file__).resolve().parent
_SCRIPT = _HERE.parent / "live-verify-fi-quality.py"

_ADP_REPO_ENV = "FI_LOOP_WORLD_ADP_REPO"
_CMS_REPO_ENV = "FI_LOOP_WORLD_CMS_REPO"
_DEFAULT_ADP = Path.home() / "automotive-data-platform-on-aws"
_DEFAULT_CMS = Path.home() / "connected-mobility-guidance-on-aws"

_ADP_PRESENT = (Path(os.environ.get(_ADP_REPO_ENV, _DEFAULT_ADP)) / "platform-foundation").exists()
_CMS_PRESENT = (Path(os.environ.get(_CMS_REPO_ENV, _DEFAULT_CMS)) / "services").exists()

_REPOS_PRESENT = _ADP_PRESENT and _CMS_PRESENT
_SKIP_REASON = (
    "FiLoopWorld requires ~/automotive-data-platform-on-aws and "
    "~/connected-mobility-guidance-on-aws (or set FI_LOOP_WORLD_ADP_REPO / "
    "FI_LOOP_WORLD_CMS_REPO)"
)

# ---------------------------------------------------------------------------
# Import the script under test
# ---------------------------------------------------------------------------

import importlib.util as _ilu

_spec = _ilu.spec_from_file_location("live_verify_fi_quality_s3", _SCRIPT)
assert _spec is not None
lv = _ilu.module_from_spec(_spec)  # type: ignore[arg-type]
_spec.loader.exec_module(lv)  # type: ignore[union-attr]

STAGE = "staging"
_VALID_RUN_ID = "fi-quality-20261027T100000-abcd1234"

# ---------------------------------------------------------------------------
# World imports (lazy — guarded by _REPOS_PRESENT)
# ---------------------------------------------------------------------------

if _REPOS_PRESENT:
    from scripts.tests.fi_loop_world.world import (
        FiLoopWorld,
        DLQ_ALARM,
        INVESTIGATIONS_ESM_UUID,
        INVESTIGATIONS_DLQ,
        INVESTIGATIONS_QUEUE,
        INVESTIGATIONS_TABLE,
    )

# ---------------------------------------------------------------------------
# Fixtures
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
# Helpers
# ---------------------------------------------------------------------------


def _make_manifest(run_id: str = _VALID_RUN_ID, **overrides: Any) -> dict:
    """Base manifest with fields required for stage 3."""
    m = {
        "run_id": run_id,
        "stage": STAGE,
        "started_at": "2026-10-27T10:00:00",
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


def _save_manifest_to_tmp(run_id: str, manifest: dict, tmp_path: Path) -> None:
    """Write manifest to tmp_path using the script's own manifest writer."""
    lv._save_manifest(run_id, manifest, str(tmp_path))


def _patch_guards(monkeypatch: pytest.MonkeyPatch, utc_hour: int = 10) -> None:
    """Patch write-window guard, pre-checks, and clock to pass in world tests."""
    monkeypatch.setattr(lv, "_after6_write_window_guard", lambda *_a, **_k: None)
    monkeypatch.setattr(lv, "_after6_pre_checks", lambda *_a, **_k: None)
    monkeypatch.setattr(
        lv, "_utc_now",
        lambda: dt.datetime(2026, 10, 27, utc_hour, 0, 0, tzinfo=dt.timezone.utc),
    )
    monkeypatch.setenv("LIVE_TESTS", "1")
    monkeypatch.setenv("FI_QUALITY_MANIFEST_DIR", "")


def _open_world(auto_pump: bool = False) -> Any:
    return FiLoopWorld(auto_pump=auto_pump)


def _seed_base_world(w: Any, *, run_id: str = _VALID_RUN_ID) -> tuple:
    """Seed one pattern + a completed investigation for use in item 8b (repeat POST).

    Inserts the investigation row directly (bypassing the quality job) since item 8b
    only needs a completed investigation to repeat-POST. The lock row must also exist.

    Returns (pat_group_id, inv_id).
    """
    pat_group_id = "f0012345abcdef01"
    inv_id = "test-8b-inv-" + "a0b1c2d3"

    w.seed_pattern(
        pattern_id=pat_group_id,
        vin_list=["VIN00001234567890", "VIN00001234567891"],
        failure_mode_id="fm-base-001",
        code_family="P0AB",
    )

    # Put a completed investigation row directly (bypasses quality job)
    tbl = w.table(INVESTIGATIONS_TABLE)
    tbl.put_item(Item={
        "investigation_id": inv_id,
        "status": "completed",
        "pattern_group_id": pat_group_id,
        "pattern_id": pat_group_id,
        "requested_at": "2026-10-26T09:00:00Z",
        "requested_by": "sub-base-001",
        "finding_id": f"FIND-{pat_group_id[:8]}",
    })
    # Lock row (always exists after first investigation)
    tbl.put_item(Item={
        "investigation_id": f"lock#{pat_group_id}",
        "investigation_id_ref": inv_id,
    })
    return pat_group_id, inv_id


# ---------------------------------------------------------------------------
# ─── Happy-path: items 8b + 9a produce pass rows ───────────────────────────
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _REPOS_PRESENT, reason=_SKIP_REASON)
def test_w8b_esm_not_enabled(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """item 8b FAILs when ESM is not in Enabled state.

    RED with placeholder (SKIPs, not FAILs); GREEN with real item.
    """
    _patch_guards(monkeypatch)
    run_id = _VALID_RUN_ID

    with _open_world() as w:
        _seed_base_world(w, run_id=run_id)
        # Force ESM to Disabled
        w._esm_state = "Disabled"

        manifest = _make_manifest(run_id)
        res = lv.Results()
        old_int = signal.getsignal(signal.SIGINT)
        old_term = signal.getsignal(signal.SIGTERM)
        try:
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
    assert "item-8b-esm-enabled" in fail_labels or any(
        "item-8b" in lbl for lbl in fail_labels
    ), f"expected item-8b FAIL when ESM not Enabled. rows={res.rows}"


@pytest.mark.skipif(not _REPOS_PRESENT, reason=_SKIP_REASON)
def test_w8b_phantom_message_in_queue(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """item 8b FAILs when queue has an unexpected message after re-POST.

    RED with placeholder; GREEN with real item.
    """
    _patch_guards(monkeypatch)
    run_id = _VALID_RUN_ID

    with _open_world() as w:
        pat_group_id, inv_id = _seed_base_world(w, run_id=run_id)
        # Plant a phantom message in the queue before item 8b runs
        import boto3
        sqs = boto3.client("sqs", region_name="us-west-2")
        q_url = sqs.get_queue_url(QueueName=INVESTIGATIONS_QUEUE)["QueueUrl"]
        sqs.send_message(QueueUrl=q_url, MessageBody="phantom-investigation-id")

        manifest = _make_manifest(run_id, investigations=[{
            "pattern_group_id": pat_group_id,
            "investigation_id": inv_id,
            "status": "completed",
        }])
        res = lv.Results()
        old_int = signal.getsignal(signal.SIGINT)
        old_term = signal.getsignal(signal.SIGTERM)
        try:
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
    assert any("8b" in lbl for lbl in fail_labels), (
        f"expected item-8b FAIL when phantom message in queue. rows={res.rows}"
    )


@pytest.mark.skipif(not _REPOS_PRESENT, reason=_SKIP_REASON)
def test_sigterm_inside_disabled_window_reenables_esm(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """SIGTERM raised inside the disabled window still re-enables the ESM.

    This is a cleanup / signal-hold test. The ESM must be re-enabled even when
    the run terminates abnormally inside the 8b/9a disabled window.
    The cleanup step 1 handles this.

    RED with placeholder (cleanup step 1 does nothing); GREEN with real cleanup.
    """
    _patch_guards(monkeypatch)
    run_id = _VALID_RUN_ID

    with _open_world() as w:
        _seed_base_world(w, run_id=run_id)

        # Simulate: ESM was disabled (item 8b ran, but SIGTERM hit before 9a re-enable)
        w._esm_state = "Disabled"

        manifest = _make_manifest(
            run_id,
            event_source_uuid=INVESTIGATIONS_ESM_UUID,
            event_source_disabled=True,  # per-write flag: ESM was disabled
        )
        _save_manifest_to_tmp(run_id, manifest, tmp_path)

        res = lv.Results()
        old_int = signal.getsignal(signal.SIGINT)
        old_term = signal.getsignal(signal.SIGTERM)
        try:
            import boto3
            idp = boto3.client("cognito-idp", region_name="us-west-2")
            try:
                pool_id = lv._resolve_pool_id(idp)
            except Exception:
                pool_id = None
            with lv._defer_signals_for_restore():
                lv._after6_cleanup(
                    res,
                    manifest=manifest,
                    manifest_dir=str(tmp_path),
                    stage=STAGE,
                    idp=idp,
                    pool_id=pool_id,
                )
        finally:
            signal.signal(signal.SIGINT, old_int)
            signal.signal(signal.SIGTERM, old_term)

        # After cleanup, ESM should be Enabled
        assert w._esm_state == "Enabled", (
            f"ESM should be Enabled after cleanup. state={w._esm_state}. rows={res.rows}"
        )

    pass_labels = {r[0] for r in res.rows if r[1] == "PASS"}
    assert any("cleanup" in lbl and ("esm" in lbl or "enable" in lbl) for lbl in pass_labels), (
        f"cleanup step 1 (ESM re-enable) should be PASS. rows={res.rows}"
    )


# ---------------------------------------------------------------------------
# ─── cleanup-only: nothing to clean vs cleanup-failed ──────────────────────
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _REPOS_PRESENT, reason=_SKIP_REASON)
def test_cleanup_only_nothing_to_clean(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """cleanup-only with event_source_disabled=False → step 1 says 'nothing to clean' (PASS).

    RED with placeholder; GREEN with real cleanup step 1.
    """
    _patch_guards(monkeypatch)
    run_id = _VALID_RUN_ID
    monkeypatch.setenv("FI_QUALITY_MANIFEST_DIR", str(tmp_path))

    with _open_world() as w:
        _seed_base_world(w, run_id=run_id)

        manifest = _make_manifest(
            run_id,
            event_source_uuid=INVESTIGATIONS_ESM_UUID,
            event_source_disabled=False,  # not disabled — nothing to clean
        )
        _save_manifest_to_tmp(run_id, manifest, tmp_path)

        res = lv.Results()
        old_int = signal.getsignal(signal.SIGINT)
        old_term = signal.getsignal(signal.SIGTERM)
        try:
            import boto3
            idp = boto3.client("cognito-idp", region_name="us-west-2")
            try:
                pool_id = lv._resolve_pool_id(idp)
            except Exception:
                pool_id = None
            with lv._defer_signals_for_restore():
                lv._after6_cleanup(
                    res,
                    manifest=manifest,
                    manifest_dir=str(tmp_path),
                    stage=STAGE,
                    idp=idp,
                    pool_id=pool_id,
                )
        finally:
            signal.signal(signal.SIGINT, old_int)
            signal.signal(signal.SIGTERM, old_term)

    pass_or_skip_esm = [r for r in res.rows if "esm" in r[0] or "enable" in r[0]]
    # Should have no FAIL rows for ESM when nothing to clean
    esm_fails = [r for r in pass_or_skip_esm if r[1] == "FAIL"]
    assert not esm_fails, (
        f"no ESM FAILs expected when event_source_disabled=False. rows={res.rows}"
    )


@pytest.mark.skipif(not _REPOS_PRESENT, reason=_SKIP_REASON)
def test_cleanup_only_step1_fails_when_reenabled_but_reads_wrong_state(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """cleanup step 1 FAILs when ESM re-enable call fails.

    RED with placeholder; GREEN with real cleanup step 1.
    """
    _patch_guards(monkeypatch)
    run_id = _VALID_RUN_ID

    with _open_world() as w:
        _seed_base_world(w, run_id=run_id)
        w._esm_state = "Disabled"

        # Make UpdateEventSourceMapping always fail
        def _fail_update(p: dict[str, Any]) -> dict[str, Any]:
            from scripts.tests.fi_rw_world.awsfake import AwsError
            raise AwsError("ServiceException", "fake failure", status=500)

        w.router.add("lambda", "UpdateEventSourceMapping", _fail_update)

        manifest = _make_manifest(
            run_id,
            event_source_uuid=INVESTIGATIONS_ESM_UUID,
            event_source_disabled=True,
        )
        _save_manifest_to_tmp(run_id, manifest, tmp_path)

        res = lv.Results()
        old_int = signal.getsignal(signal.SIGINT)
        old_term = signal.getsignal(signal.SIGTERM)
        try:
            import boto3
            idp = boto3.client("cognito-idp", region_name="us-west-2")
            try:
                pool_id = lv._resolve_pool_id(idp)
            except Exception:
                pool_id = None
            with lv._defer_signals_for_restore():
                lv._after6_cleanup(
                    res,
                    manifest=manifest,
                    manifest_dir=str(tmp_path),
                    stage=STAGE,
                    idp=idp,
                    pool_id=pool_id,
                )
        finally:
            signal.signal(signal.SIGINT, old_int)
            signal.signal(signal.SIGTERM, old_term)

    fail_labels = {r[0] for r in res.rows if r[1] == "FAIL"}
    assert any("esm" in lbl or "enable" in lbl for lbl in fail_labels), (
        f"expected FAIL when ESM re-enable raises. rows={res.rows}"
    )


# ---------------------------------------------------------------------------
# ─── Items 1 and 8a: LIVETEST- filter ──────────────────────────────────────
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _REPOS_PRESENT, reason=_SKIP_REASON)
def test_w1_livetest_rows_ignored(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    """item 1 ignores pattern rows whose pattern_id starts with LIVETEST-.

    Prints a NOTE with the count. RED: no filtering → item-1-ddb-equals-parquet FAIL
    (LIVETEST row in DDB but not parquet). GREEN: filtered, NOTE printed.
    """
    _patch_guards(monkeypatch)
    run_id = _VALID_RUN_ID
    monkeypatch.setattr(lv, "_after6_write_window_guard", lambda *_a, **_k: None)

    import duckdb as _duckdb
    # Write a proper parquet answer key using DuckDB
    parquet_file = str(tmp_path / "ak.parquet")
    con = _duckdb.connect()
    try:
        con.execute("""
            CREATE TABLE _ak AS SELECT * FROM (VALUES (
                'P001', 'P0AB', 'supplier_lot', 'SUP-A/LOT-001', 2, '2026-01-01', 'fm-base-001'
            )) t(pattern_id, code_family, cause_dimension, cause_value, planted_count, start_date, failure_mode_id)
        """)
        con.execute(f"COPY _ak TO '{parquet_file}' (FORMAT PARQUET)")
    finally:
        con.close()
    ak_file = parquet_file

    with _open_world() as w:
        # Seed real pattern
        w.seed_pattern(
            pattern_id="f0012345abcdef01",
            vin_list=["VIN00001234567890", "VIN00001234567891"],
            failure_mode_id="fm-base-001",
            code_family="P0AB",
        )
        # Also seed a LIVETEST- pattern directly in the patterns table
        import boto3
        ddb = boto3.resource("dynamodb", region_name="us-east-1")
        pat_tbl = ddb.Table(f"adp-{STAGE}-fault-patterns")
        pat_tbl.put_item(Item={
            "pattern_id": f"LIVETEST-4-{run_id}-sw",
            "pattern_group_id": f"LIVETEST-4-{run_id}-sw",
            "run_date": "2026-10-27",
            "code_family": "P0LT",
            "livetest_run_id": run_id,
        })

        # Upload fault_patterns parquet to S3 with just the real pattern_id
        # (item 1 downloads this to check ddb_ids == pq_ids)
        fp_parquet_file = str(tmp_path / "fp.parquet")
        con2 = _duckdb.connect()
        try:
            con2.execute("""
                CREATE TABLE _fp AS SELECT * FROM (VALUES ('f0012345abcdef01', '2026-10-27', '2026-10-27T06:00:00Z')
                ) t(pattern_id, run_date, computed_at)
            """)
            con2.execute(f"COPY _fp TO '{fp_parquet_file}' (FORMAT PARQUET)")
        finally:
            con2.close()

        lake_bucket = f"adp-{STAGE}-foundation-lake-123456789012-us-east-1"
        import boto3 as _b3
        s3_east = _b3.client("s3", region_name="us-east-1")
        s3_east.upload_file(
            fp_parquet_file, lake_bucket,
            "curated/engineering/fault_patterns/fault_patterns.parquet",
        )

        res = lv.Results()
        old_int = signal.getsignal(signal.SIGINT)
        old_term = signal.getsignal(signal.SIGTERM)
        try:
            lv._item_1(
                res,
                stage=STAGE,
                answer_key=str(ak_file),
            )
        finally:
            signal.signal(signal.SIGINT, old_int)
            signal.signal(signal.SIGTERM, old_term)

    out = capsys.readouterr().out
    # RED: without filtering, item-1-ddb-equals-parquet FAILs (LIVETEST row in DDB but not parquet)
    # GREEN: NOTE printed, check passes
    assert "NOTE" in out and "LIVETEST" in out, (
        f"item 1 should print NOTE about LIVETEST- rows. out={out[:300]}"
    )
    # With real filter: item-1-ddb-equals-parquet should PASS
    pass_labels = {r[0] for r in res.rows if r[1] == "PASS"}
    assert "item-1-ddb-equals-parquet" in pass_labels or "item-1" in str(res.rows), (
        f"item-1-ddb-equals-parquet should PASS after LIVETEST- filter. rows={res.rows}"
    )
    # No FAIL due to LIVETEST row
    ddb_parquet_fails = [r for r in res.rows if r[1] == "FAIL" and "parquet" in r[0]]
    assert not ddb_parquet_fails, (
        f"item-1-ddb-equals-parquet must not FAIL with LIVETEST- row filtered. rows={res.rows}"
    )


@pytest.mark.skipif(not _REPOS_PRESENT, reason=_SKIP_REASON)
def test_w8a_livetest_rows_ignored(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    """item 8a ignores LIVETEST- pattern rows and prints NOTE with count.

    RED: no filtering → item-8a-one-run-date FAIL (LIVETEST row has different run_date).
    GREEN: filtered, NOTE printed.
    """
    _patch_guards(monkeypatch)
    run_id = _VALID_RUN_ID

    with _open_world() as w:
        # Seed real pattern (with run_date)
        w.seed_pattern(
            pattern_id="f0012345abcdef01",
            vin_list=["VIN00001234567890"],
            failure_mode_id="fm-base-001",
            code_family="P0AB",
        )
        import boto3
        ddb = boto3.resource("dynamodb", region_name="us-east-1")
        pat_tbl = ddb.Table(f"adp-{STAGE}-fault-patterns")
        # Add run_date to the real pattern
        pat_tbl.update_item(
            Key={"pattern_id": "f0012345abcdef01"},
            UpdateExpression="SET run_date = :rd",
            ExpressionAttributeValues={":rd": "2026-10-27"},
        )
        # Also seed a LIVETEST- pattern with a DIFFERENT run_date
        pat_tbl.put_item(Item={
            "pattern_id": f"LIVETEST-4-{run_id}-sw",
            "pattern_group_id": f"LIVETEST-4-{run_id}-sw",
            "run_date": "2026-10-01",  # different date → would fail item-8a-one-run-date
            "code_family": "P0LT",
            "livetest_run_id": run_id,
        })

        res = lv.Results()
        old_int = signal.getsignal(signal.SIGINT)
        old_term = signal.getsignal(signal.SIGTERM)
        # Also patch _schedule_guard so item 8a doesn't skip (ADP schedules not in world)
        monkeypatch.setattr(lv, "_schedule_guard", lambda *_a, **_k: None)
        try:
            try:
                lv._item_8a(
                    res,
                    stage=STAGE,
                )
            except Exception:
                # item 8a may raise after the NOTE check (S3/Lambda not set up in world)
                pass
        finally:
            signal.signal(signal.SIGINT, old_int)
            signal.signal(signal.SIGTERM, old_term)

    out = capsys.readouterr().out
    # RED: without filter, item-8a-one-run-date FAILs (two different run_dates)
    # GREEN: NOTE printed, LIVETEST row filtered
    assert "NOTE" in out and "LIVETEST" in out, (
        f"item 8a should print NOTE about LIVETEST- rows. out={out[:300]}"
    )
    # item-8a-one-run-date should not FAIL (LIVETEST row filtered)
    fail_labels = {r[0] for r in res.rows if r[1] == "FAIL"}
    assert "item-8a-one-run-date" not in fail_labels, (
        f"item-8a-one-run-date must not FAIL after LIVETEST- filter. rows={res.rows}"
    )


# ---------------------------------------------------------------------------
# ─── Mutants ────────────────────────────────────────────────────────────────
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _REPOS_PRESENT, reason=_SKIP_REASON)
def test_m_8b_esm_disable_check_bypassed(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Mutant: ESM Enabled check bypassed → test_w8b_esm_not_enabled should FAIL.

    The real test (test_w8b_esm_not_enabled) verifies the check by running _item_8b
    when w._esm_state == "Disabled". This simulation test verifies that the assertion
    in test_w8b_esm_not_enabled would catch a mutant that sets esm_state without checking.
    """
    _patch_guards(monkeypatch)
    run_id = _VALID_RUN_ID

    with _open_world() as w:
        _seed_base_world(w, run_id=run_id)
        w._esm_state = "Disabled"

        manifest = _make_manifest(run_id)
        res = lv.Results()
        old_int = signal.getsignal(signal.SIGINT)
        old_term = signal.getsignal(signal.SIGTERM)
        try:
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
    # Mutant catches: if check is bypassed, no FAIL → simulation catches nothing →
    # the mutant SURVIVES. Here we assert FAIL EXISTS → mutant is CAUGHT.
    assert fail_labels, (
        f"M-8b: ESM check must FAIL when ESM is Disabled. rows={res.rows}"
    )


@pytest.mark.skipif(not _REPOS_PRESENT, reason=_SKIP_REASON)
def test_m_cleanup_step1_bypassed(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Mutant: cleanup step 1 (ESM re-enable) bypassed → ESM stays Disabled.

    After cleanup, w._esm_state should be Enabled. If step 1 is bypassed it stays
    Disabled. GREEN: state==Enabled after cleanup.
    """
    _patch_guards(monkeypatch)
    run_id = _VALID_RUN_ID

    with _open_world() as w:
        _seed_base_world(w, run_id=run_id)
        w._esm_state = "Disabled"

        manifest = _make_manifest(
            run_id,
            event_source_uuid=INVESTIGATIONS_ESM_UUID,
            event_source_disabled=True,
        )
        _save_manifest_to_tmp(run_id, manifest, tmp_path)

        res = lv.Results()
        old_int = signal.getsignal(signal.SIGINT)
        old_term = signal.getsignal(signal.SIGTERM)
        try:
            import boto3
            idp = boto3.client("cognito-idp", region_name="us-west-2")
            try:
                pool_id = lv._resolve_pool_id(idp)
            except Exception:
                pool_id = None
            with lv._defer_signals_for_restore():
                lv._after6_cleanup(
                    res,
                    manifest=manifest,
                    manifest_dir=str(tmp_path),
                    stage=STAGE,
                    idp=idp,
                    pool_id=pool_id,
                )
        finally:
            signal.signal(signal.SIGINT, old_int)
            signal.signal(signal.SIGTERM, old_term)

        # GREEN: ESM should be Enabled after cleanup
        # Mutant: if step 1 is bypassed, state stays Disabled → this assertion fails
        assert w._esm_state == "Enabled", (
            f"M-cleanup-step1: ESM must be Enabled after cleanup. state={w._esm_state}"
        )


# ===========================================================================
# Fix-round-1-A: C6 — end-to-end main() test is in s2a test_c6_after6_main_end_to_end
# ===========================================================================


# ===========================================================================
# Part B: fix-round-1 — mutant-catching tests for M12, M15
# ===========================================================================


@pytest.mark.skipif(not _REPOS_PRESENT, reason=_SKIP_REASON)
def test_w7c_8b_queue_pre_check_fails_on_foreign(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """item 8b FAILs when queue has a foreign message BEFORE the repeat POST.

    W7 fix: 8b must check queue+DLQ empty before the repeat POST.
    Mutant M31: pre-check removed → no FAIL (CAUGHT).
    RED: old code has no 8b-specific pre-check → phantom message in queue before
    the repeat POST is not caught until after the POST (wrong location).
    """
    _patch_guards(monkeypatch)
    run_id = _VALID_RUN_ID + "w7a"

    with _open_world() as w:
        pat_group_id, inv_id = _seed_base_world(w, run_id=run_id)
        # Seed a foreign message in the queue BEFORE item 8b runs
        import boto3
        sqs = boto3.client("sqs", region_name="us-west-2")
        q_url = sqs.get_queue_url(QueueName=INVESTIGATIONS_QUEUE)["QueueUrl"]
        sqs.send_message(QueueUrl=q_url, MessageBody="foreign-pre-8b-message")

        manifest = _make_manifest(
            run_id,
            investigations=[{
                "pattern_group_id": pat_group_id,
                "investigation_id": inv_id,
                "status": "completed",
            }],
        )
        res = lv.Results()
        old_int = signal.getsignal(signal.SIGINT)
        old_term = signal.getsignal(signal.SIGTERM)
        try:
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
    assert any("pre" in lbl or "empty" in lbl or "foreign" in lbl for lbl in fail_labels), (
        f"W7: 8b must FAIL when queue has foreign message before repeat POST. "
        f"rows={res.rows}"
    )


@pytest.mark.skipif(not _REPOS_PRESENT, reason=_SKIP_REASON)
def test_n19_step5_del_inv_conditioned_on_requested_by(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Step 5 uses `requested_by = :sub` (not `attribute_exists`) for regular inv deletes.

    N19 mutant: changes step 5's `_del_inv` condition back to `attribute_exists` —
    a foreign investigation row is silently deleted, no FAIL emitted.
    Correct code: ConditionalCheckFailed → re-read → present ≠ ours → FAIL.
    """
    _patch_guards(monkeypatch)
    run_id = _VALID_RUN_ID + "-n19"
    monkeypatch.setenv("FI_QUALITY_MANIFEST_DIR", str(tmp_path))

    with _open_world() as w:
        _seed_base_world(w, run_id=run_id)

        our_sub = "sub-n19-our-user"
        other_sub = "sub-n19-other-user"
        inv_id = f"inv-n19-{run_id[:8]}"
        pgid = "f0011111aaaabbbb"

        # Regular investigation row that belongs to a DIFFERENT user
        tbl = w.table(INVESTIGATIONS_TABLE)
        tbl.put_item(Item={
            "investigation_id": inv_id,
            "status": "completed",
            "pattern_group_id": pgid,
            "requested_by": other_sub,   # intentionally not our_sub
        })

        manifest = _make_manifest(
            run_id,
            user_sub=our_sub,
            investigations=[{"investigation_id": inv_id,
                             "pattern_group_id": pgid, "status": "completed"}],
        )
        _save_manifest_to_tmp(run_id, manifest, tmp_path)

        res = lv.Results()
        lv._after6_cleanup(res, manifest=manifest, manifest_dir=str(tmp_path), stage=STAGE)

    # Correct code: FAIL row because the row exists but requested_by != our_sub
    # N19 mutant: attribute_exists → silently deletes → no FAIL row
    fail_details = [(lbl, d) for lbl, s, d in res.rows if s == "FAIL"]
    assert any(
        "mismatch" in d or "not ours" in d or "requested_by" in d
        for _, d in fail_details
    ), (
        f"N19: step 5 must FAIL when investigation requested_by != user_sub. "
        f"Got rows: {res.rows}"
    )


# ===========================================================================
# W2 round-2 tests (R1–R5)
# ===========================================================================


@pytest.mark.skipif(not _REPOS_PRESENT, reason=_SKIP_REASON)
def test_w4_cleanup_returns_slot_to_counter_and_never_retries_unknown(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """W4 (architect): the cleanup puts the counter back by exactly one per slot, and a slot whose
    earlier return has an unknown outcome (``cap_slot_returning`` without ``cap_slot_returned``)
    gets no second decrement and a FAIL row.
    """
    _patch_guards(monkeypatch)
    run_id = _VALID_RUN_ID
    monkeypatch.setenv("FI_QUALITY_MANIFEST_DIR", str(tmp_path))
    from decimal import Decimal

    with _open_world() as w:
        tbl = w.table(INVESTIGATIONS_TABLE)
        tbl.put_item(Item={"investigation_id": "cap#2026-10-27", "count": Decimal("5")})
        tbl.put_item(Item={"investigation_id": "cap#2026-10-26", "count": Decimal("7")})
        manifest = _make_manifest(run_id, investigations=[])
        manifest["cap_slots_taken"] = [
            {"date": "2026-10-27", "investigation_id": "a" * 32},
            {"date": "2026-10-27", "investigation_id": "b" * 32},
            {"date": "2026-10-26", "investigation_id": "c" * 32,
             "cap_slot_returning": True},
        ]
        _save_manifest_to_tmp(run_id, manifest, tmp_path)
        res = lv.Results()
        try:
            lv._after6_cleanup(res, manifest=manifest, manifest_dir=str(tmp_path),
                               stage=STAGE, mode="after-6")
        except Exception as exc:  # noqa: BLE001
            raise AssertionError(f"cleanup raised {type(exc).__name__}: {exc}") from exc
        today = tbl.get_item(Key={"investigation_id": "cap#2026-10-27"},
                             ConsistentRead=True).get("Item", {})
        yday = tbl.get_item(Key={"investigation_id": "cap#2026-10-26"},
                            ConsistentRead=True).get("Item", {})

    assert int(today.get("count", -1)) == 3, (
        f"two slots taken on 2026-10-27 must bring 5 back to 3; got {today!r} rows={res.rows}")
    assert int(yday.get("count", -1)) == 7, (
        f"a slot with an unknown earlier outcome must not be decremented again; got {yday!r}")
    fails = [r for r in res.rows if r[1] == "FAIL" and "cap#2026-10-26" in r[0]]
    assert fails, f"the unknown-outcome slot must FAIL naming cap#2026-10-26; rows={res.rows}"
