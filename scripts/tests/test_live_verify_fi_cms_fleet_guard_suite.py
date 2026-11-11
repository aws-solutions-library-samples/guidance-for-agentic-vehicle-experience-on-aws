"""fi-cms-fleet through the shared GuardSuite.

Mutations verified (each CAUGHT, not caught=pass):
  1. guard removed                     → test_block_off_without_go_refuses_every_write_flag
  2. guard narrowed to --allow-writes  → test_block_off_without_go_refuses_every_write_flag
                         (--allow-managed-writes entry)
  3. guard moved after first client    → test_block_off_without_go_refuses_every_write_flag
                         (sentinel fires first → not refused → assertion fails)
  4. (no printed command with write flag in this script)
  5. refusal returns 0                 → test_block_off_without_go_refuses_every_write_flag
                         (rc == 0 ≠ refusal_rc 1)
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from live_go_probe import GuardSuite  # noqa: E402


class TestFiCmsFleetGuard(GuardSuite):
    script = "live-verify-fi-cms-fleet.py"
    go_env = "FI_CMS_FLEET_LIVE_GO"
    write_argvs = {
        "--allow-writes": [
            "--stage", "staging", "--part", "pre", "--allow-writes",
        ],
        "--allow-managed-writes": [
            "--stage", "staging", "--part", "pre", "--allow-managed-writes",
        ],
    }
    read_argv = ["--stage", "staging", "--part", "pre"]
    refusal_rc = 1
