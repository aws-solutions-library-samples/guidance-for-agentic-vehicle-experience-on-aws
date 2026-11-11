"""p0-escalation through the shared GuardSuite.

Mutations verified (each CAUGHT):
  1. guard removed                     → test_block_off_without_go_refuses_every_write_flag
  2. guard narrowed to --allow-writes  → test_block_off_without_go_refuses_every_write_flag
                         (--allow-page entry; only --allow-writes guarded)
  3. guard moved after first client    → test_block_off_without_go_refuses_every_write_flag
  4. cleanup command missing GO_ENV    → test_a_check_failure_still_cleans_up_and_checks_residue
                         (runtime assertion on out in test_live_verify_p0_escalation.py)
  5. refusal returns 0                 → test_block_off_without_go_refuses_every_write_flag

Notes:
  - ``--allow-page`` alone reaches the guard because the guard fires before
    the ``if not args.allow_writes: return 2`` check.
  - The cleanup printed command (reached only after AWS calls) is now asserted
    at runtime via the world fixture in test_live_verify_p0_escalation.py
    (test_a_check_failure_still_cleans_up_and_checks_residue, assert on out).
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from live_go_probe import GuardSuite  # noqa: E402


class TestP0EscalationGuard(GuardSuite):
    script = "live-verify-p0-escalation.py"
    go_env = "P0_ESCALATION_LIVE_GO"
    # --allow-page alone reaches the guard (guard is before the allow_writes skip check).
    # --allow-writes alone also reaches it.
    write_argvs = {
        "--allow-writes": ["--allow-writes"],
        "--allow-page": ["--allow-page"],
    }
    read_argv = ["--pre-deploy"]
    refusal_rc = 1
