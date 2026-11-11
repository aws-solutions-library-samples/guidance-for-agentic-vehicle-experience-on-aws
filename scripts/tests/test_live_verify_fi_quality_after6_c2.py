"""C2 tests for scripts/live-verify-fi-quality.py — ESM event-source handling.

C2 (a) 8b timeout: event_source_disabled flag stays True when _wait_esm_state times out.
C2 (b) SIGTERM + ResourceInUse: cleanup step 1 retries ResourceInUseException on re-enable.
C2 (c) / N16: _wait_esm_state (polling) must be used, not an immediate readback.

Run:
    env -u AWS_PROFILE -u AWS_SESSION_TOKEN \\
        AWS_ACCESS_KEY_ID=x AWS_SECRET_ACCESS_KEY=x AWS_DEFAULT_REGION=us-west-2 \\
        PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest \\
        scripts/tests/test_live_verify_fi_quality_after6_c2.py -q
"""
from __future__ import annotations

import datetime as dt
import importlib.util as _ilu
import os
import signal
import socket
from pathlib import Path
from typing import Any, Iterator

import pytest

# ---------------------------------------------------------------------------
# Load the script under test
# ---------------------------------------------------------------------------

_HERE = Path(__file__).resolve().parent
_SCRIPT = _HERE.parent / "live-verify-fi-quality.py"
_spec = _ilu.spec_from_file_location("lv_after6_c2_mod", _SCRIPT)
assert _spec is not None
lv = _ilu.module_from_spec(_spec)  # type: ignore[arg-type]
_spec.loader.exec_module(lv)  # type: ignore[union-attr]

# ---------------------------------------------------------------------------
# Repo-presence guard (FiLoopWorld requires repos)
# ---------------------------------------------------------------------------

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

if _REPOS_PRESENT:
    from scripts.tests.fi_loop_world.world import (
        FiLoopWorld,
        INVESTIGATIONS_ESM_UUID,
        INVESTIGATIONS_TABLE,
    )

STAGE = "staging"
_VALID_RUN_ID = "fi-quality-20261027T150000-c2test01"

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
    m: dict[str, Any] = {
        "run_id": run_id,
        "stage": STAGE,
        "started_at": "2026-10-27T15:00:00",
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


def _patch_guards(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, utc_hour: int = 10) -> None:
    """Patch write-window guard, pre-checks, clock, LIVE_TESTS, and sleep."""
    monkeypatch.setattr(lv, "_after6_write_window_guard", lambda *_a, **_k: None)
    monkeypatch.setattr(lv, "_after6_pre_checks", lambda *_a, **_k: None)
    monkeypatch.setattr(
        lv, "_utc_now",
        lambda: dt.datetime(2026, 10, 27, utc_hour, 0, 0, tzinfo=dt.timezone.utc),
    )
    monkeypatch.setenv("LIVE_TESTS", "1")
    monkeypatch.setenv("FI_QUALITY_MANIFEST_DIR", str(tmp_path))
    # Make ESM polling fast (no real sleep between get-state polls)
    monkeypatch.setattr(lv.time, "sleep", lambda _s: None)


def _open_world(auto_pump: bool = False) -> Any:
    return FiLoopWorld(auto_pump=auto_pump)


def _seed_base_world(w: Any, *, run_id: str = _VALID_RUN_ID) -> tuple:
    """Seed one pattern + a completed investigation row for item 8b tests."""
    pat_group_id = "f0012345abcdef01"
    inv_id = "test-c2-inv-a0b1c2d3"

    w.seed_pattern(
        pattern_id=pat_group_id,
        vin_list=["VIN00001234567890", "VIN00001234567891"],
        failure_mode_id="fm-c2-001",
        code_family="P0AB",
    )
    tbl = w.table(INVESTIGATIONS_TABLE)
    tbl.put_item(Item={
        "investigation_id": inv_id,
        "status": "completed",
        "pattern_group_id": pat_group_id,
        "pattern_id": pat_group_id,
        "requested_at": "2026-10-26T09:00:00Z",
        "requested_by": "sub-c2-001",
        "finding_id": f"FIND-{pat_group_id[:8]}",
    })
    tbl.put_item(Item={
        "investigation_id": f"lock#{pat_group_id}",
        "investigation_id_ref": inv_id,
    })
    return pat_group_id, inv_id


# ---------------------------------------------------------------------------
# C2(c) / N16 — polling required, immediate readback fails
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _REPOS_PRESENT, reason=_SKIP_REASON)
def test_c2_n16_immediate_readback_fails_esm_disabled_check(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """C2(c) / N16: _wait_esm_state (polling) must be used for item-8b-esm-disabled.

    The world holds 'Disabling' for 3 GetEventSourceMapping reads before advancing
    to 'Disabled'.  Correct code uses _wait_esm_state which polls past the transitional
    state → item-8b-esm-disabled is PASS.

    N16 mutant replaces _wait_esm_state with a direct get_event_source_mapping call that
    returns 'Disabling' on the first read → check fails → item-8b-esm-disabled is FAIL →
    this test fails → N16 is caught.

    Verify N16 in a git archive copy:
        git archive HEAD | tar -x -C /tmp/n16check
        patch -p1 -d /tmp/n16check < .kiro/specs/2026-10-05-fi-fleet-health-stage-d/64-records/rev2/mutants/N16-c5-8b-immediate-readback.diff
        env -u AWS_PROFILE ... .venv/bin/python -m pytest /tmp/n16check/scripts/tests/test_live_verify_fi_quality_after6_c2.py::test_c2_n16_immediate_readback_fails_esm_disabled_check -q
    """
    _patch_guards(monkeypatch, tmp_path)
    run_id = _VALID_RUN_ID

    with _open_world() as w:
        _seed_base_world(w, run_id=run_id)

        # No completed investigations in manifest: 8b will FAIL after the ESM check
        # (item-8b-repeat-target).  The ESM check itself is what this test pins.
        manifest = _make_manifest(run_id)
        res = lv.Results()
        old_int = signal.getsignal(signal.SIGINT)
        old_term = signal.getsignal(signal.SIGTERM)
        try:
            lv._item_8b(res, stage=STAGE, run_id=run_id, manifest=manifest,
                        manifest_dir=str(tmp_path), allow_writes=True)
        finally:
            signal.signal(signal.SIGINT, old_int)
            signal.signal(signal.SIGTERM, old_term)

    pass_labels = {r[0] for r in res.rows if r[1] == "PASS"}
    assert "item-8b-esm-disabled" in pass_labels, (
        "C2(c)/N16: item-8b-esm-disabled must be a PASS row.  "
        "N16 mutant (immediate get_event_source_mapping readback) sees 'Disabling' and "
        "produces FAIL instead.  rows=" + repr(res.rows)
    )


# ---------------------------------------------------------------------------
# C2(a) — 8b timeout: flag stays True
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _REPOS_PRESENT, reason=_SKIP_REASON)
def test_c2a_8b_timeout_flag_stays_true(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """C2(a): event_source_disabled stays True when _wait_esm_state times out.

    The disable request was already sent, so the ESM is (or will become) Disabled.
    Cleanup must re-enable it; reverting the flag to False would leave it permanently
    disabled if the script exits here.

    RED: current code reverts manifest['event_source_disabled'] = False on timeout.
    GREEN: the revert is removed; flag stays True so cleanup step 1 re-enables.
    """
    _patch_guards(monkeypatch, tmp_path)
    # Force _wait_esm_state to return a non-terminal state (timeout case)
    monkeypatch.setattr(lv, "_wait_esm_state", lambda *_a, **_kw: "Disabling")

    run_id = _VALID_RUN_ID
    with _open_world() as w:
        _seed_base_world(w, run_id=run_id)

        manifest = _make_manifest(run_id)
        res = lv.Results()
        old_int = signal.getsignal(signal.SIGINT)
        old_term = signal.getsignal(signal.SIGTERM)
        try:
            lv._item_8b(res, stage=STAGE, run_id=run_id, manifest=manifest,
                        manifest_dir=str(tmp_path), allow_writes=True)
        finally:
            signal.signal(signal.SIGINT, old_int)
            signal.signal(signal.SIGTERM, old_term)

    # The disable request was sent before _wait_esm_state was called; the flag must
    # stay True so that cleanup step 1 can re-enable the ESM.
    assert manifest.get("event_source_disabled") is True, (
        "C2(a): event_source_disabled was reverted to False after timeout; "
        "must stay True (disable request already sent → re-enable is idempotent).  "
        "rows=" + repr(res.rows)
    )


# ---------------------------------------------------------------------------
# C2(b) — SIGTERM during disable wait: cleanup retries ResourceInUseException
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _REPOS_PRESENT, reason=_SKIP_REASON)
def test_c2b_sigterm_cleanup_reenables_with_resource_in_use(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """C2(b): cleanup step 1 retries ResourceInUseException when re-enabling the ESM.

    Scenario: SIGTERM arrived while 8b was waiting for 'Disabled'; the ESM is stuck in
    'Disabling'.  cleanup step 1's _update_esm must retry until the state settles.

    Setup:
      - world._esm_state = "Disabling" (update was sent but not yet settled)
      - time.sleep monkeypatched: first call advances world to "Disabled" so the
        second _update_esm attempt can succeed
      - manifest event_source_disabled=True / event_source_uuid set

    RED: current code (pre-fix) calls update_event_source_mapping directly → raises
         ResourceInUseException → _reenable_esm() raises → FAIL row → ESM stays Disabling.
    GREEN: _update_esm retries → succeeds on 2nd attempt → ESM ends Enabled.
    """
    _patch_guards(monkeypatch, tmp_path)
    run_id = _VALID_RUN_ID

    with _open_world() as w:
        _seed_base_world(w, run_id=run_id)

        # Simulate: ESM is still in Disabling (SIGTERM hit during 8b's wait)
        w._esm_state = "Disabling"
        w._esm_transitional_reads_left = 3

        # On the first time.sleep call (inside _update_esm retry), advance world to
        # "Disabled" so the second update attempt is no longer blocked.
        _sleep_calls: list[float] = []

        def _advancing_sleep(s: float) -> None:
            _sleep_calls.append(s)
            if len(_sleep_calls) == 1:
                w._esm_state = "Disabled"  # settle so the retry succeeds
                w._esm_transitional_reads_left = 0

        monkeypatch.setattr(lv.time, "sleep", _advancing_sleep)

        manifest = _make_manifest(
            run_id,
            event_source_uuid=INVESTIGATIONS_ESM_UUID,
            event_source_disabled=True,
        )
        lv._save_manifest(run_id, manifest, str(tmp_path))

        res = lv.Results()
        import boto3
        idp = boto3.client("cognito-idp", region_name="us-west-2")
        try:
            pool_id = lv._resolve_pool_id(idp)
        except Exception:
            pool_id = None
        old_int = signal.getsignal(signal.SIGINT)
        old_term = signal.getsignal(signal.SIGTERM)
        try:
            with lv._defer_signals_for_restore():
                lv._after6_cleanup(res, manifest=manifest, manifest_dir=str(tmp_path),
                                   stage=STAGE, idp=idp, pool_id=pool_id)
        finally:
            signal.signal(signal.SIGINT, old_int)
            signal.signal(signal.SIGTERM, old_term)

        # After cleanup, the ESM must be Enabled (or Enabling if poll is in flight)
        assert w._esm_state in ("Enabled", "Enabling"), (
            "C2(b): ESM must be Enabled after cleanup retried ResourceInUseException.  "
            f"state={w._esm_state!r}  sleep_calls={_sleep_calls}  rows={res.rows}"
        )
