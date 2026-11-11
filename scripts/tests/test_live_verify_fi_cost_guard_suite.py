"""live-verify-fi-cost through the shared GuardSuite."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from live_go_probe import GuardSuite  # noqa: E402


class TestFiCostGuard(GuardSuite):
    script = "live-verify-fi-cost.py"
    go_env = "FI_COST_LIVE_GO"
    write_argvs = {
        "--allow-writes": ["--stage", "staging", "--allow-writes"],
    }
    read_argv = ["--stage", "staging"]
    refusal_rc = 1
