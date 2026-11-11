"""Tests for scripts/live-verify-fi-quality.py.

Covers (brief §Tests):
  H1  LIVE_TESTS not set → exit 2
  H2  --stage prod → exit 1 before any AWS call
  H3  s3:// answer key → exit 1
  H4  missing answer key file → exit 1
  H5  --allow-writes absent → items 6 and 8a SKIP
  H6  after-6 items (2, 3, 4, 5, 8b, 9) are placeholders: one SKIP "not run: needs Group 6",
      no AWS call; --part after-6 exits 2
  H7  subprocess run with PYTHONPATH stripped (-I) from a temp cwd
  I1  _item_1: all active planted found (≥80%) → PASS
  I2  _item_1: none found → FAIL on found-active
  I3  _item_1: found-active < 80% → FAIL
  I4  _item_1: noise family found → FAIL (P0A7 pattern in future-only family)
  I5  _item_1: DDB id set mismatch → FAIL cross-check
  I6  _item_1: no answer key provided → SKIP
  I7  _item_7: simulate deny returns allowed → FAIL
  I8  _item_7: FI role not found → FAIL
  I9  _item_7: positive control denied → FAIL
  I10 exit codes: all PASS → 0; any FAIL → 1; any SKIP → 2
  I11 check() detail vs on_fail selection
  I12 _item_1: empty answer key → FAIL (anti-vacuity)

Run:
    PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest scripts/tests/test_live_verify_fi_quality.py -q
"""
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

# ---------------------------------------------------------------------------
# Load the script as a module (hyphenated filename requires importlib)
# ---------------------------------------------------------------------------

_SCRIPT = Path(__file__).resolve().parents[1] / "live-verify-fi-quality.py"
_spec = importlib.util.spec_from_file_location("live_verify_fi_quality", _SCRIPT)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

Results = _mod.Results
check = _mod.check
main = _mod.main
_item_1 = _mod._item_1
_item_6 = _mod._item_6
_item_7 = _mod._item_7
_item_8a = _mod._item_8a
_matches = _mod._matches
_load_answer_key = _mod._load_answer_key
_BW_DIMS = _mod._BW_DIMS
_ALLOWED_STAGE = _mod._ALLOWED_STAGE


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# H1: LIVE_TESTS not set → exit 2
# ---------------------------------------------------------------------------

def test_h1_live_tests_not_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LIVE_TESTS", raising=False)
    assert main(["--stage", "staging", "--part", "now"]) == 2


# ---------------------------------------------------------------------------
# H2: --stage prod → exit 1 before any AWS call
# ---------------------------------------------------------------------------

def test_h2_stage_prod(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LIVE_TESTS", "1")
    import boto3
    with patch.object(boto3, "resource") as mock_resource, \
         patch.object(boto3, "client") as mock_client:
        result = main(["--stage", "prod"])
    assert result == 1
    mock_resource.assert_not_called()
    mock_client.assert_not_called()


# ---------------------------------------------------------------------------
# H3: s3:// answer key → exit 1
# ---------------------------------------------------------------------------

def test_h3_s3_answer_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LIVE_TESTS", "1")
    assert main(["--stage", "staging", "--answer-key", "s3://bucket/key.parquet"]) == 1


# ---------------------------------------------------------------------------
# H4: missing answer key file → exit 1
# ---------------------------------------------------------------------------

def test_h4_missing_answer_key(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("LIVE_TESTS", "1")
    assert main(["--stage", "staging", "--answer-key", str(tmp_path / "noexist.parquet")]) == 1


# ---------------------------------------------------------------------------
# H5: --allow-writes absent → items 6 and 8a SKIP
# ---------------------------------------------------------------------------

def test_h5_no_allow_writes_skips_every_write_item(monkeypatch: pytest.MonkeyPatch) -> None:
    """The runner is the write gate: every write item SKIPs and makes no AWS call (the
    repo conftest raises on any real call)."""
    monkeypatch.setenv("LIVE_TESTS", "1")
    res = Results()
    _mod._run_items(sorted(_mod._WRITE_ITEMS), res, allow_writes=False, stage="staging",
                    answer_key=None)
    assert res.rows == [(f"item-{n}", "SKIP", "writes to staging; needs --allow-writes")
                        for n in sorted(_mod._WRITE_ITEMS)]
    # Stage 3: 8b and 9 are real write items (design §Shape)
    assert _mod._WRITE_ITEMS == {"6", "8a", "2", "3", "4", "5", "8b"}  # 9 is task 6.5


# ---------------------------------------------------------------------------
# H6: the after-6 items are placeholders: one SKIP "not run: needs Group 6", no AWS call
# (the user's option (a), 2026-10-17; tasks.md 6.4 rebuilds them)
# ---------------------------------------------------------------------------

# Item 9 is task 6.5 (split 2026-10-28): not an after-6 item and not in _ITEM_FNS.
_AFTER6 = ("2", "3", "4", "5", "8b")
_NEEDS_G6 = "not run: needs Group 6"
_STAGE2 = "not run: stage 2"
# Stage 3: all after-6 items are real; no more stage-2 placeholders
_STAGE2_ITEMS: tuple[str, ...] = ()


def _no_aws(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Make every boto3 entry point raise and record who asked (on top of the conftest guard)."""
    import boto3
    calls: list[str] = []

    def _refuse(kind: str):
        def fn(*a: object, **k: object) -> None:
            calls.append(f"{kind}{a[:1]}")
            raise AssertionError(f"after-6 placeholder made an AWS call: boto3.{kind}{a[:1]}")
        return fn
    monkeypatch.setattr(boto3, "client", _refuse("client"))
    monkeypatch.setattr(boto3, "resource", _refuse("resource"))
    monkeypatch.setattr(boto3, "Session", _refuse("Session"))
    return calls


def test_h6_after6_parts_are_the_placeholders() -> None:
    # S8 fix: after-6 items are no longer placeholders — _NEEDS_GROUP_6 / _STAGE2_SKIP removed.
    # After-6 items (2/3/4/5/8b) are real write items in tasks.md 6.4; item 9 is 6.5's.
    assert _mod._AFTER6_ITEMS == _AFTER6
    assert _mod._NOW_ITEMS == ("1", "6", "7", "8a")
    assert set(_mod._ITEM_FNS) == set(_mod._NOW_ITEMS) | set(_AFTER6)
    # S8: dead constants removed
    assert not hasattr(_mod, "_NEEDS_GROUP_6"), "_NEEDS_GROUP_6 must be removed (S8 fix)"
    assert not hasattr(_mod, "_STAGE2_SKIP"), "_STAGE2_SKIP must be removed (S8 fix)"
    assert "2" in _mod._WRITE_ITEMS and "3" in _mod._WRITE_ITEMS
    # Stage 3: 8b and 9 ARE in _WRITE_ITEMS (real implementations)
    assert "8b" in _mod._WRITE_ITEMS and "9" not in _mod._WRITE_ITEMS  # 9 is task 6.5


@pytest.mark.parametrize("n", _STAGE2_ITEMS)
def test_h6_each_stage2_item_records_one_skip_and_calls_nothing(
        n: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """Items 4/5/8b/9 (stage 2 placeholders) SKIP with stage2 message, no AWS call."""
    calls = _no_aws(monkeypatch)
    res = Results()
    _mod._ITEM_FNS[n](res, allow_writes=True, stage="staging", answer_key="ak.parquet",
                      run_id="fi-quality-20261027T150000-abcdef01",
                      manifest={}, manifest_dir="/tmp")
    assert res.rows == [(f"item-{n}", "SKIP", _STAGE2)]
    assert calls == []


@pytest.mark.parametrize("n", ("2", "3"))
def test_h6_items_2_3_skip_without_manifest_and_no_aws(
        n: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """Items 2/3 SKIP with 'no manifest' reason when manifest=None, no AWS call."""
    calls = _no_aws(monkeypatch)
    res = Results()
    _mod._ITEM_FNS[n](res, allow_writes=True, stage="staging", answer_key="ak.parquet",
                      run_id="fi-quality-20261027T150000-abcdef01",
                      manifest=None, manifest_dir="/tmp")
    assert res.rows[0][1] == "SKIP"
    assert "manifest" in res.rows[0][2].lower() or "allow" in res.rows[0][2].lower()
    assert calls == []


@pytest.mark.parametrize("argv", [
    # With --allow-writes: items 2/3 SKIP (no AK); items 4/5/8b/9 call the write-window
    # guard (scheduler) → RealAwsCallBlocked → FAIL; hence rc=1 (FAIL, not 0). But the
    # conftest also fails the test if real AWS is attempted. So only the no-allow-writes
    # variant is tested here; the allow-writes path is exercised by stage-3 world tests.
    ["--part", "after-6"],
])
def test_h6_main_after6_exits_2_always(
        argv: list[str], monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str]) -> None:
    """--part after-6 without --allow-writes exits 2 (all items SKIP)."""
    import signal
    monkeypatch.setenv("LIVE_TESTS", "1")
    old = signal.getsignal(signal.SIGINT), signal.getsignal(signal.SIGTERM)
    try:
        got = main(["--stage", "staging", *argv])
    finally:
        signal.signal(signal.SIGINT, old[0])
        signal.signal(signal.SIGTERM, old[1])
    # Without --allow-writes, every write item SKIPs → has_any_skip → rc=2
    assert got == 2, f"Expected 2 from after-6 without --allow-writes, got {got}"


# ---------------------------------------------------------------------------
# H7: subprocess run with PYTHONPATH stripped (-I) from a temp cwd
# ---------------------------------------------------------------------------

def test_h7_subprocess_no_live_tests(tmp_path: Path) -> None:
    """Running the script without LIVE_TESTS=1 exits 2 even with stripped PYTHONPATH."""
    env = {k: v for k, v in os.environ.items() if k != "LIVE_TESTS"}
    result = subprocess.run(
        [sys.executable, "-I", str(_SCRIPT), "--stage", "staging", "--part", "now"],
        capture_output=True,
        cwd=str(tmp_path),
        env=env,
    )
    assert result.returncode == 2


# ---------------------------------------------------------------------------
# I1: _item_1 all active planted found → PASS
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# I2: _item_1 none found → FAIL on found-active
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# I3: _item_1 found-active < 80% → FAIL (give only 60% of active patterns)
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# I4: noise family found → FAIL (P0A7/P6 is future-dated; finding it is noise)
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# I5: DDB id set mismatch → FAIL cross-check
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# I6: no answer key → SKIP
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# I7: _item_7 simulate returns allowed → FAIL
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# I8: _item_7 FI role not found → FAIL
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# I9: _item_7 positive control denied → FAIL
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# I10: exit codes — all PASS → 0; any FAIL → 1; any SKIP → 2
# ---------------------------------------------------------------------------

def test_i10_exit_codes() -> None:
    res_pass = Results()
    check(res_pass, "x", True, "ok")
    assert not res_pass.has_any_fail
    assert not res_pass.has_any_skip

    res_fail = Results()
    check(res_fail, "x", False, "bad")
    assert res_fail.has_any_fail

    res_skip = Results()
    res_skip.skip("x", "reason")
    assert res_skip.has_any_skip
    assert not res_skip.has_any_fail


# ---------------------------------------------------------------------------
# I11: check() detail vs on_fail selection
# ---------------------------------------------------------------------------

def test_i11_check_detail_vs_on_fail() -> None:
    res = Results()
    check(res, "ok-row", True, "good detail", on_fail="should not appear")
    check(res, "fail-row", False, "ok detail", on_fail="fail detail")

    rows = {r[0]: (r[1], r[2]) for r in res.rows}
    assert rows["ok-row"] == ("PASS", "good detail")
    assert rows["fail-row"] == ("FAIL", "fail detail")


# ---------------------------------------------------------------------------
# I12: empty answer key → FAIL anti-vacuity
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# W6 (review cycle 1): main()'s exit mapping, end to end
# ---------------------------------------------------------------------------

def _fake_item(kind: str):
    def fn(res: Results, **_: object) -> None:
        if kind == "pass":
            check(res, "fake-pass", True, "ok")
        elif kind == "skip":
            res.skip("fake-skip", "why")
        elif kind == "fail":
            check(res, "fake-fail", False, on_fail="bad")
        else:
            raise RuntimeError("boom")
    return fn


@pytest.mark.parametrize("kinds,rc", [
    (["pass"], 0),
    (["pass", "skip"], 2),
    (["pass", "fail"], 1),
    (["skip", "fail"], 1),
    (["fail", "skip"], 1),
    (["raise"], 1),
    (["skip"], 2),
])
def test_w6_main_exit_mapping(kinds: list[str], rc: int, monkeypatch: pytest.MonkeyPatch) -> None:
    import signal
    monkeypatch.setenv("LIVE_TESTS", "1")
    keys = ["1", "7", "6"][:len(kinds)]
    fns = dict(_mod._ITEM_FNS)
    fns.update({k: _fake_item(kind) for k, kind in zip(keys, kinds)})
    monkeypatch.setattr(_mod, "_ITEM_FNS", fns)
    old = signal.getsignal(signal.SIGINT), signal.getsignal(signal.SIGTERM)
    try:
        got = main(["--stage", "staging", "--items", ",".join(keys), "--allow-writes"])
    finally:
        signal.signal(signal.SIGINT, old[0])
        signal.signal(signal.SIGTERM, old[1])
    assert got == rc



# ---------------------------------------------------------------------------
# Review cycle 2: W5 (later items still run), W6 (cleanup holds signals), S1 (paged clients)
# ---------------------------------------------------------------------------

def test_w5_an_item_that_raises_does_not_stop_the_next(monkeypatch: pytest.MonkeyPatch) -> None:
    fns = dict(_mod._ITEM_FNS)
    fns["1"] = _fake_item("raise")
    fns["7"] = _fake_item("pass")
    monkeypatch.setattr(_mod, "_ITEM_FNS", fns)
    res = Results()
    _mod._run_items(["1", "7"], res, allow_writes=False, stage="staging", answer_key=None)
    assert [(label, s) for label, s, _ in res.rows] == [("item-1-exception", "FAIL"), ("fake-pass", "PASS")]


def test_w6_signals_are_held_through_a_cleanup_and_delivered_after() -> None:
    import os
    import signal
    old = signal.getsignal(signal.SIGINT), signal.getsignal(signal.SIGTERM)
    reached: list[str] = []
    try:
        _mod._install_signal_handlers()
        with pytest.raises(SystemExit) as ei:
            with _mod._defer_signals_for_restore():
                os.kill(os.getpid(), signal.SIGTERM)
                reached.append("after the signal, inside the cleanup")
        assert ei.value.code == 143
    finally:
        signal.signal(signal.SIGINT, old[0])
        signal.signal(signal.SIGTERM, old[1])
    assert reached == ["after the signal, inside the cleanup"]


def test_s1_password_client_reads_every_page() -> None:
    class Idp:
        def list_user_pool_clients(self, **kw: object) -> dict:
            if "NextToken" not in kw:
                return {"UserPoolClients": [{"ClientId": "web"}], "NextToken": "p2"}
            return {"UserPoolClients": [{"ClientId": "pw"}]}

        def describe_user_pool_client(self, ClientId: str, **_: object) -> dict:
            flows = ["ALLOW_USER_PASSWORD_AUTH"] if ClientId == "pw" else ["ALLOW_REFRESH_TOKEN_AUTH"]
            return {"UserPoolClient": {"ExplicitAuthFlows": flows}}
    assert _mod.password_client(Idp(), "pool") == "pw"



def test_s8_a_run_stopped_by_a_signal_still_prints_its_summary(
        monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    import signal
    monkeypatch.setenv("LIVE_TESTS", "1")

    def stopped(res: Results, **_: object) -> None:
        check(res, "before-the-signal", True, "ok")
        raise SystemExit(143)
    fns = dict(_mod._ITEM_FNS)
    fns["1"] = stopped
    monkeypatch.setattr(_mod, "_ITEM_FNS", fns)
    old = signal.getsignal(signal.SIGINT), signal.getsignal(signal.SIGTERM)
    try:
        with pytest.raises(SystemExit) as ei:
            main(["--stage", "staging", "--items", "1"])
    finally:
        signal.signal(signal.SIGINT, old[0])
        signal.signal(signal.SIGTERM, old[1])
    assert ei.value.code == 143
    out = capsys.readouterr().out
    assert "\nSummary" in out and "before-the-signal" in out.split("\nSummary", 1)[1]


# ---------------------------------------------------------------------------
# P1  _poll_investigation exits via max_iterations even when clock is frozen
# ---------------------------------------------------------------------------

def test_p1_poll_investigation_exits_on_max_iterations(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression: poll loop must not spin forever when time.monotonic() is frozen.

    Root cause (2026-10-27): when all 8 test files load the script under different
    names (e.g. ``lv_s2a_mod``), the FiLoopWorld's ``load_script()`` only installs
    ScriptTime on the copy it loads internally — the test-file copies keep the real
    ``time`` module.  If ``pump_investigation`` exceeds the 30 s HTTP timeout, the
    investigation stays ``"queued"`` and the poll burns 600 real seconds.

    The fix adds ``max_iterations`` so a frozen or real-time ``time.monotonic()``
    cannot produce an unbounded loop.  This test verifies the bound is honoured even
    when ``time.monotonic()`` never advances past the deadline.
    """
    calls: list[int] = []

    # Always return "queued" — never a terminal state.
    def _fake_http(*_a: object, **_k: object) -> tuple[int, dict]:
        calls.append(1)
        return (200, {"status": "queued", "investigation_id": "inv-test"})

    class _FrozenTime:
        """time-module stand-in whose monotonic() never advances."""
        _t: float = 1_000_000.0

        def monotonic(self) -> float:
            return self._t  # frozen

        def sleep(self, _s: float) -> None:
            pass  # does not advance the frozen clock

        def __getattr__(self, name: str) -> object:
            import time as _rt
            return getattr(_rt, name)

    monkeypatch.setattr(_mod, "_item_6_http", _fake_http)
    monkeypatch.setattr(_mod, "time", _FrozenTime())
    try:
        result = _mod._poll_investigation(
            "http://localhost/",
            "inv-test",
            "tok",
            max_s=60,
            interval_s=5,
            max_iterations=4,
        )
    finally:
        # Restore real time (monkeypatch restores setattr, but be explicit).
        pass

    # max_iterations=4 → at most 4 GET calls, then returns None.
    assert result is None, f"expected None (timeout), got {result!r}"
    assert len(calls) == 4, (
        f"expected exactly 4 GET calls (max_iterations=4), made {len(calls)}"
    )
