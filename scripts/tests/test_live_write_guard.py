"""scripts/lib/live_write_guard.py: the shared rule behind every live-verify script's write flag.

Run:
    .venv/bin/python -m pytest scripts/tests/test_live_write_guard.py -q -p no:cacheprovider
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))
import live_write_guard as g  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
from live_go_probe import block_off  # noqa: E402

_GO = "SOME_SCRIPT_LIVE_GO"


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(_GO, raising=False)


def test_the_block_is_active_under_pytest() -> None:
    assert g.aws_block_active() is True


def test_a_transport_without_the_marker_is_not_a_block(monkeypatch: pytest.MonkeyPatch) -> None:
    block_off(monkeypatch)
    assert g.aws_block_active() is False


def test_a_live_pass_is_not_a_block(monkeypatch: pytest.MonkeyPatch) -> None:
    import botocore.endpoint as be

    monkeypatch.setitem(be.Endpoint._send.__globals__["_LIVE_PASS"], "active", True)
    assert g.aws_block_active() is False


def test_the_go_must_equal_the_stage(monkeypatch: pytest.MonkeyPatch) -> None:
    block_off(monkeypatch)
    assert g.writes_sanctioned("staging", _GO) is False
    for bad in ("", "production", "Staging", "staging ", "1"):
        monkeypatch.setenv(_GO, bad)
        assert g.writes_sanctioned("staging", _GO) is False, bad
    monkeypatch.setenv(_GO, "staging")
    assert g.writes_sanctioned("staging", _GO) is True
    assert g.writes_sanctioned("production", _GO) is False


def test_the_go_of_another_script_does_not_count(monkeypatch: pytest.MonkeyPatch) -> None:
    block_off(monkeypatch)
    monkeypatch.setenv("OTHER_SCRIPT_LIVE_GO", "staging")
    assert g.writes_sanctioned("staging", _GO) is False


def test_no_write_flag_means_no_refusal(monkeypatch: pytest.MonkeyPatch) -> None:
    block_off(monkeypatch)
    assert g.check_write_flags(stage="staging", go_env=_GO, flags={"--allow-writes": False}) is None
    assert g.check_write_flags(stage="staging", go_env=_GO, flags={}) is None


def test_any_one_write_flag_is_enough_to_refuse_and_all_given_are_named(monkeypatch: pytest.MonkeyPatch) -> None:
    block_off(monkeypatch)
    for given in ("--allow-writes", "--allow-managed-writes", "--allow-paging"):
        flags = {"--allow-writes": False, "--allow-managed-writes": False, "--allow-paging": False}
        flags[given] = True
        msg = g.check_write_flags(stage="staging", go_env=_GO, flags=flags)
        assert msg and given in msg and _GO + "=staging" in msg, given
    both = g.check_write_flags(
        stage="staging", go_env=_GO, flags={"--allow-writes": True, "--allow-paging": True}
    )
    assert both and "--allow-writes" in both and "--allow-paging" in both


def test_a_write_flag_passes_under_the_block_or_with_the_go(monkeypatch: pytest.MonkeyPatch) -> None:
    assert g.check_write_flags(stage="staging", go_env=_GO, flags={"--allow-writes": True}) is None  # block on
    block_off(monkeypatch)
    monkeypatch.setenv(_GO, "staging")
    assert g.check_write_flags(stage="staging", go_env=_GO, flags={"--allow-writes": True}) is None


def test_the_message_steers_a_debug_session_to_the_safe_path(monkeypatch: pytest.MonkeyPatch) -> None:
    block_off(monkeypatch)
    msg = g.check_write_flags(stage="staging", go_env=_GO, flags={"--allow-writes": True})
    assert msg and msg.startswith("refused: --allow-writes")
    assert "pytest" in msg and "import conftest" in msg and "helper must not" in msg
    assert f"{_GO}=staging" in msg
