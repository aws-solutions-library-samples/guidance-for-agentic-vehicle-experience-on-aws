"""fi-fleet-health through the shared GuardSuite (live-go guard, brief §65).

The printed verify_cmd for item 7 (--allow-paging) must begin with
FI_FLEET_HEALTH_LIVE_GO=<stage>, which is tested below in addition to the
shared suite checks.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from live_go_probe import GuardSuite, SCRIPTS_DIR  # noqa: E402


class TestFiFleetHealthGuard(GuardSuite):
    script = "live-verify-fi-fleet-health.py"
    go_env = "FI_FLEET_HEALTH_LIVE_GO"
    write_argvs = {
        "--allow-writes": ["--stage", "staging", "--allow-writes"],
        "--allow-managed-writes": ["--stage", "staging", "--allow-managed-writes"],
        "--allow-paging": ["--stage", "staging", "--allow-paging"],
    }
    read_argv = ["--stage", "staging"]
    refusal_rc = 1


# ---------------------------------------------------------------------------
# Printed-command test: verify_cmd for item 7 must lead with the go variable
# ---------------------------------------------------------------------------

def test_item7_verify_cmd_starts_with_live_go_env():
    """The verify_cmd string built inside _item_7_force begins with FI_FLEET_HEALTH_LIVE_GO=<stage>."""
    # Read the format string from the source (can't execute without AWS).
    # grep for the verify_cmd assignment and confirm the expected prefix.
    src = (SCRIPTS_DIR / "live-verify-fi-fleet-health.py").read_text()
    # The line we changed is:
    #   f"{_LIVE_GO_ENV}={stage} LIVE_TESTS=1 .venv/bin/python scripts/live-verify-fi-fleet-health.py "
    # Check the constant name is present in the format string, not the bare LIVE_TESTS prefix.
    assert "FI_FLEET_HEALTH_LIVE_GO" in src, (
        "FI_FLEET_HEALTH_LIVE_GO must appear in the script source"
    )
    # Find the verify_cmd assignment block and confirm ordering.
    idx = src.find('f"{_LIVE_GO_ENV}={stage} LIVE_TESTS=1 .venv/bin/python scripts/live-verify-fi-fleet-health.py')
    assert idx != -1, (
        "verify_cmd for item 7 must start with {_LIVE_GO_ENV}={stage} before LIVE_TESTS=1; "
        "found neither the expected format string in the source"
    )
    # The old bare LIVE_TESTS=1 prefix (without the go variable) must not remain.
    bare_idx = src.find('f"LIVE_TESTS=1 .venv/bin/python scripts/live-verify-fi-fleet-health.py')
    assert bare_idx == -1, (
        "old bare LIVE_TESTS=1 prefix for verify_cmd still present; must be replaced with "
        "FI_FLEET_HEALTH_LIVE_GO={stage} LIVE_TESTS=1"
    )
