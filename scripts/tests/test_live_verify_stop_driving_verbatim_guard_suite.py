"""stop-driving-verbatim through the shared GuardSuite.

Mutations verified (each CAUGHT):
  1. guard removed                     → test_block_off_without_go_refuses_every_write_flag
  2. guard checks wrong env var name   → test_block_off_with_the_go_passes_the_guard
                         (go_env set but wrong var checked → guard still refuses)
  3. guard moved after first client    → test_block_off_without_go_refuses_every_write_flag
  4. docstring command missing GO_ENV  → test_docstring_write_examples_carry_go_env (GuardSuite)
  5. refusal returns 0                 → test_block_off_without_go_refuses_every_write_flag

Notes:
  - The guard sits between the ``--allow-writes`` skip check (return 2) and the
    write-window check (_rw._in_write_window); it fires before any AWS call or clock
    check with scheduling relevance.
  - read_argv ["--stage", "staging"] exits 2 at the --allow-writes check before
    reaching the guard; that is correct — no write flag, no guard needed.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from live_go_probe import GuardSuite  # noqa: E402


class TestStopDrivingVerbatimGuard(GuardSuite):
    script = "live-verify-stop-driving-verbatim.py"
    go_env = "STOP_DRIVING_LIVE_GO"
    write_argvs = {
        "--allow-writes": [
            "--stage", "staging", "--allow-writes",
        ],
    }
    read_argv = ["--stage", "staging"]
    refusal_rc = 1
