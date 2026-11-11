"""``--allow-writes`` outside the AWS block needs the live-run step's go.

Why: on 2026-10-29 a coder helper called ``lv.main([... '--part', 'item9', '--allow-writes'])`` from a plain
``python3 -c`` while debugging a test. Plain python never loads the repo ``conftest.py``, whose AWS block swaps in
fake credentials and refuses the transport, so the call ran the whole item 9 flow against staging with the user's
real credentials (issues/2026-10-30-unexplained-item9-shaped-run-2026-10-29/). The script now refuses
``--allow-writes`` unless the block is installed (pytest, or ``import conftest``) or ``FI_QUALITY_LIVE_GO`` equals
``--stage``, which only the live-run step sets.

Every probe uses a bad ``--run-id`` (or no manifest) so a call that gets past the guard stops at the next refusal
without building a client; ``boto3.client``/``resource`` raise if anything gets that far.

Run:
    .venv/bin/python -m pytest scripts/tests/test_live_verify_fi_quality_live_go.py -q -p no:cacheprovider
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parent.parent / "live-verify-fi-quality.py"
_spec = importlib.util.spec_from_file_location("lv_live_go", _SCRIPT)
lv = importlib.util.module_from_spec(_spec)  # type: ignore[arg-type]
_spec.loader.exec_module(lv)  # type: ignore[union-attr]

_GO = "FI_QUALITY_LIVE_GO"
_PAST_GUARD = "does not match the expected shape"  # the run-id refusal, which comes after the guard

_WRITE_ARGV = ["--stage", "staging", "--part", "item9", "--allow-writes", "--run-id", "bad-run-id"]
_READ_ARGV = ["--stage", "staging", "--part", "item9", "--run-id", "bad-run-id"]
_CLEANUP_ARGV = [
    "--stage", "staging", "--cleanup-only", "--allow-writes",
    "--run-id", "fi-quality-20261030T000000-deadbeef",
]


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    import boto3

    def _boom(*_a: object, **_k: object) -> None:
        raise AssertionError("a boto3 client or resource was built")

    monkeypatch.setattr(boto3, "client", _boom)
    monkeypatch.setattr(boto3, "resource", _boom)
    monkeypatch.setenv("LIVE_TESTS", "1")
    monkeypatch.delenv(_GO, raising=False)


def _block_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """A transport without the conftest marker: what plain ``python3`` has."""
    import botocore.endpoint as be

    def _send(self: object, request: object) -> None:
        raise AssertionError("a request reached the transport")

    monkeypatch.setattr(be.Endpoint, "_send", _send)


def _run(argv: list[str], capsys: pytest.CaptureFixture[str]) -> tuple[int, str]:
    rc = lv.main(argv)
    return rc, capsys.readouterr().err


def test_block_detection_follows_the_conftest_marker(monkeypatch: pytest.MonkeyPatch) -> None:
    assert lv._aws_block_active() is True  # pytest loads conftest, which installs the block
    _block_off(monkeypatch)
    assert lv._aws_block_active() is False


def test_block_off_without_go_refuses_allow_writes(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _block_off(monkeypatch)
    rc, err = _run(_WRITE_ARGV, capsys)
    assert rc == 1
    assert "refused: --allow-writes" in err and _GO in err
    assert _PAST_GUARD not in err


def test_refusal_points_a_debug_session_at_the_safe_path(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _block_off(monkeypatch)
    _, err = _run(_WRITE_ARGV, capsys)
    assert "conftest" in err and "helper must not" in err


def test_block_off_without_go_refuses_cleanup_only_before_the_manifest_is_read(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _block_off(monkeypatch)
    rc, err = _run(_CLEANUP_ARGV, capsys)
    assert rc == 1
    assert "refused: --allow-writes" in err
    assert "no manifest found" not in err


@pytest.mark.parametrize("value", ["", "production", "Staging", "staging ", "1"])
def test_block_off_with_a_wrong_go_refuses(
    value: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _block_off(monkeypatch)
    monkeypatch.setenv(_GO, value)
    rc, err = _run(_WRITE_ARGV, capsys)
    assert rc == 1
    assert "refused: --allow-writes" in err and _PAST_GUARD not in err


def test_block_off_with_the_go_passes_the_guard(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _block_off(monkeypatch)
    monkeypatch.setenv(_GO, "staging")
    rc, err = _run(_WRITE_ARGV, capsys)
    assert rc == 1
    assert _PAST_GUARD in err and "refused: --allow-writes" not in err


def test_block_on_needs_no_go(capsys: pytest.CaptureFixture[str]) -> None:
    rc, err = _run(_WRITE_ARGV, capsys)
    assert rc == 1
    assert _PAST_GUARD in err and "refused: --allow-writes" not in err


def test_a_read_only_run_is_not_guarded(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _block_off(monkeypatch)
    rc, err = _run(_READ_ARGV, capsys)
    assert rc == 1
    assert _PAST_GUARD in err and "refused: --allow-writes" not in err


def test_a_live_pass_is_not_a_block(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """During a ``live_aws`` pass the conftest keeps its marker but lets requests through."""
    import botocore.endpoint as be

    monkeypatch.setitem(be.Endpoint._send.__globals__["_LIVE_PASS"], "active", True)
    assert lv._aws_block_active() is False
    rc, err = _run(_WRITE_ARGV, capsys)
    assert rc == 1 and "refused: --allow-writes" in err and _PAST_GUARD not in err


_ENTRY_POINTS = {
    "part-now": ["--stage", "staging", "--part", "now", "--allow-writes", "--run-id", "bad-run-id"],
    "part-after-6": ["--stage", "staging", "--part", "after-6", "--allow-writes", "--run-id", "bad-run-id"],
    "items": ["--stage", "staging", "--items", "6,8a", "--allow-writes", "--run-id", "bad-run-id"],
}


@pytest.mark.parametrize("argv", list(_ENTRY_POINTS.values()), ids=list(_ENTRY_POINTS))
def test_block_off_without_go_refuses_every_write_entry_point(
    argv: list[str], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _block_off(monkeypatch)
    rc, err = _run(argv, capsys)
    assert rc == 1 and "refused: --allow-writes" in err and _PAST_GUARD not in err


@pytest.mark.parametrize("argv", list(_ENTRY_POINTS.values()), ids=list(_ENTRY_POINTS))
def test_block_off_with_the_go_passes_the_guard_on_every_entry_point(
    argv: list[str], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _block_off(monkeypatch)
    monkeypatch.setenv(_GO, "staging")
    rc, err = _run(argv, capsys)
    assert rc == 1 and _PAST_GUARD in err and "refused: --allow-writes" not in err


def test_the_printed_cleanup_command_carries_the_go(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """The command the operator pastes after a crashed run must work without hand-setting the variable."""
    monkeypatch.setenv("FI_QUALITY_MANIFEST_DIR", str(tmp_path))
    monkeypatch.setattr(lv, "_run_item9", lambda *a, **k: None)
    monkeypatch.setattr(lv, "_install_signal_handlers", lambda: None)
    run_id = "fi-quality-20261030T000000-deadbeef"
    lv.main(["--stage", "staging", "--part", "item9", "--allow-writes", "--run-id", run_id])
    cmd = [ln.strip() for ln in capsys.readouterr().out.splitlines() if "--cleanup-only" in ln]
    assert cmd == [
        f"{_GO}=staging LIVE_TESTS=1 python scripts/live-verify-fi-quality.py --stage staging "
        f"--cleanup-only --run-id {run_id} --allow-writes"
    ]


@pytest.mark.parametrize("block_off", [True, False], ids=["block-off-with-the-go", "block-on"])
def test_cleanup_only_gets_past_the_guard(
    block_off: bool, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """The printed recovery command is the only way to clean live residue; the guard must let it through."""
    monkeypatch.setenv("FI_QUALITY_MANIFEST_DIR", str(tmp_path))
    if block_off:
        _block_off(monkeypatch)
        monkeypatch.setenv(_GO, "staging")
    rc, err = _run(_CLEANUP_ARGV, capsys)
    assert rc == 1 and "no manifest found" in err and "refused: --allow-writes" not in err
