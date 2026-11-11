"""live-verify-fi-recall-warranty through the shared GuardSuite."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from live_go_probe import GuardSuite  # noqa: E402


class TestFiRecallWarrantyGuard(GuardSuite):
    script = "live-verify-fi-recall-warranty.py"
    go_env = "FI_RECALL_WARRANTY_LIVE_GO"
    write_argvs = {
        "--allow-writes": ["--stage", "staging", "--allow-writes"],
        "--allow-managed-writes": ["--stage", "staging", "--allow-managed-writes"],
        "--allow-paging": ["--stage", "staging", "--allow-paging"],
    }
    read_argv = ["--stage", "staging"]
    refusal_rc = 1
