"""live-verify-fi-rebalancing through the shared GuardSuite."""
from __future__ import annotations

import pytest
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from live_go_probe import GuardSuite, load_script  # noqa: E402

from scripts.tests import fi_rebal_cms_world as W  # noqa: E402

_SCRIPTS = Path(__file__).resolve().parent.parent
_REBAL = load_script("live-verify-fi-rebalancing.py")


class TestFiRebalancingGuard(GuardSuite):
    script = "live-verify-fi-rebalancing.py"
    go_env = "FI_REBALANCING_LIVE_GO"
    write_argvs = {
        "--allow-writes": [
            "--stage", "staging",
            "--part", "agent",
            "--workgroup", "test-wg",
            "--allow-writes",
        ],
        "--approve-move": [
            "--stage", "staging",
            "--part", "cms",
            "--workgroup", "test-wg",
            "--cms-api-endpoint", "https://x.example.com/prod",
            "--approve-move",
        ],
        "--cleanup-only": [
            "--stage", "staging",
            "--part", "cms",
            "--workgroup", "test-wg",
            "--cms-api-endpoint", "https://x.example.com/prod",
            "--cleanup-only",
        ],
    }
    read_argv = [
        "--stage", "staging",
        "--part", "adp",
        "--workgroup", "test-wg",
    ]
    refusal_rc = 2


# ---------------------------------------------------------------------------
# Printed-command runtime tests (replace source-text checks; catch the mutant
# "variable moved into a trailing comment with the print broken").
# ---------------------------------------------------------------------------

def test_cleanup_pointer_cms_has_go_variable(capsys) -> None:
    """_cleanup_pointer printed command starts with FI_REBALANCING_LIVE_GO=staging."""
    run = _REBAL._CmsRun(
        stage="staging", run_id="test-run-1", base="https://x.example.com/prod",
        allow_writes=True, approve_move=True, manifest={},
    )
    _REBAL._cleanup_pointer(run, "test: something is still grounded")
    err = capsys.readouterr().err
    assert "FI_REBALANCING_LIVE_GO=staging LIVE_TESTS=1" in err, (
        "_cleanup_pointer must contain FI_REBALANCING_LIVE_GO=staging before LIVE_TESTS=1; "
        f"got: {err!r}"
    )


def test_auto_cleanup_pointer_has_go_variable(capsys) -> None:
    """_auto_cleanup_pointer printed command starts with FI_REBALANCING_LIVE_GO=staging."""
    _REBAL._auto_cleanup_pointer(
        run_id="test-run-1",
        stage="staging",
        cms_base="https://x.example.com/prod",
        avx_base="https://y.example.com/staging",
        workgroup="test-wg",
        what="test: something is still grounded",
    )
    err = capsys.readouterr().err
    assert "FI_REBALANCING_LIVE_GO=staging LIVE_TESTS=1" in err, (
        "_auto_cleanup_pointer must contain FI_REBALANCING_LIVE_GO=staging before LIVE_TESTS=1; "
        f"got: {err!r}"
    )


@pytest.fixture()
def _auto_world(tmp_path):
    with W.AutoWorld(tmp_path) as w:
        yield w


def test_part_auto_recovery_command_has_go_variable(_auto_world) -> None:
    """_part_auto prints the recovery command before U2; it must start with FI_REBALANCING_LIVE_GO=staging."""
    _, out, _ = _auto_world.run_main(_auto_world.auto_argv())
    assert "FI_REBALANCING_LIVE_GO=staging LIVE_TESTS=1" in out, (
        "_part_auto recovery command must contain FI_REBALANCING_LIVE_GO=staging before LIVE_TESTS=1; "
        f"got (first 500 chars): {out[:500]!r}"
    )
