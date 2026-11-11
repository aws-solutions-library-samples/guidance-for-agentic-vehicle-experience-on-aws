"""fi-quality through the shared GuardSuite (the per-script template the other nine scripts use)."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from live_go_probe import GuardSuite  # noqa: E402


class TestFiQualityGuard(GuardSuite):
    script = "live-verify-fi-quality.py"
    go_env = "FI_QUALITY_LIVE_GO"
    write_argvs = {"--allow-writes": ["--stage", "staging", "--part", "item9", "--allow-writes", "--run-id", "bad-run-id"]}
    read_argv = ["--stage", "staging", "--part", "item9", "--run-id", "bad-run-id"]
    refusal_rc = 1
