"""fi-cms-maker through the shared GuardSuite.

Mutations verified (each CAUGHT):
  1. guard removed                     → test_block_off_without_go_refuses_every_write_flag
  2. guard checks wrong env var name   → test_block_off_with_the_go_passes_the_guard
                         (go_env set but wrong var checked → guard still refuses)
  3. guard moved after first client    → test_block_off_without_go_refuses_every_write_flag
  4. docstring command missing GO_ENV  → test_docstring_write_examples_carry_go_env (GuardSuite)
  5. refusal returns 0                 → test_block_off_without_go_refuses_every_write_flag
  6. --part cleanup has no guard       → test_block_off_without_go_refuses_every_write_flag
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from live_go_probe import GuardSuite  # noqa: E402


class TestFiCmsMakerGuard(GuardSuite):
    script = "live-verify-fi-cms-maker.py"
    go_env = "FI_CMS_MAKER_LIVE_GO"
    write_argvs = {
        "--allow-writes": [
            "--stage", "staging", "--part", "perf", "--allow-writes",
        ],
        "--part cleanup": [
            "--stage", "staging", "--part", "cleanup", "--run-id", "rev-1",
        ],
    }
    read_argv = ["--stage", "staging", "--part", "pre"]
    refusal_rc = 1
