"""fi-loop through the shared GuardSuite (live-go guard, brief §65)."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from live_go_probe import GuardSuite  # noqa: E402


class TestFiLoopGuard(GuardSuite):
    script = "live-verify-fi-loop.py"
    go_env = "FI_LOOP_LIVE_GO"
    # --part pre needs no --run-id, so the guard fires before any further check.
    write_argvs = {
        "--allow-writes": ["--stage", "staging", "--part", "pre", "--allow-writes"],
        "--allow-managed-writes": ["--stage", "staging", "--part", "pre", "--allow-managed-writes"],
    }
    read_argv = ["--stage", "staging", "--part", "pre"]
    refusal_rc = 1
