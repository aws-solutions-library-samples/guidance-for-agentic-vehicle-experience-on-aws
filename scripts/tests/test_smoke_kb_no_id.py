"""Guard test: smoke-test-kb-retrieve.py must exit non-zero when no KB id is set,
and must exit zero for --audit-set --dry-run even when no KB id is set.

WHY THIS EXISTS
---------------
Prior to this fix, `smoke-test-kb-retrieve.py` fell back to a hardcoded
staging KB id when `VSA_ADP_KB_ID` was unset and `--kb-id` was not passed.
On a public mirror — where that KB does not exist — the script appeared to
start, produced no output, and silently failed the IAM simulation step.
The hardcoded id is also an environment-specific literal that does not
belong in a published reference sample.

The correct behaviour is a loud, actionable error: print to stderr naming
both `VSA_ADP_KB_ID` and `--kb-id`, then exit non-zero.

`--audit-set <file> --dry-run` is an exception: it never calls AWS, so it
must exit 0 even when no KB id is set.

MUTATION GUIDE
--------------
To confirm the no-id guard actually guards the property (not just its
presence), reintroduce the old hardcoded fallback.  In
scripts/smoke-test-kb-retrieve.py, change line 85 from:

    DEFAULT_KB_ID = os.environ.get("VSA_ADP_KB_ID") or ""

back to:

    DEFAULT_KB_ID = os.environ.get("VSA_ADP_KB_ID") or "EXAMPLEKB01"  # ← mutation

With that line restored:
  - `test_default_is_empty_string` FAILS — DEFAULT_KB_ID equals "EXAMPLEKB01".
  - `test_exits_nonzero_when_no_kb_id_set` FAILS — the script no longer exits
    via the KB-id guard, so the Error: message is absent from stderr, causing
    the `assert "VSA_ADP_KB_ID" in result.stderr` assertion to fail.

Both tests must be green for the guard to hold.

Mutation S2 (--kb-id guard reads env only): change `if not args.kb_id:` to
`if not os.environ.get("VSA_ADP_KB_ID"):` in smoke-test-kb-retrieve.py.
`test_exits_zero_when_kb_id_provided_via_flag` FAILS — "KB id is required"
appears in stderr even when --kb-id is passed.

Mutation S4 (case change): change `Error:` to `error:` in the script.
`test_exits_nonzero_when_no_kb_id_set` FAILS — the first stderr line starts
with lowercase 'e', which demo-preflight.sh would not match.

Run:
    pytest scripts/tests/test_smoke_kb_no_id.py -v
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parent.parent / "smoke-test-kb-retrieve.py"


# ---------------------------------------------------------------------------
# Module-level constant guard
# ---------------------------------------------------------------------------

def _load_module():
    """Import the script as a module without executing its __main__ block."""
    spec = importlib.util.spec_from_file_location("smoke_test_kb", SCRIPT)
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def test_default_is_empty_string(monkeypatch):
    """MODULE-LEVEL DEFAULT_KB_ID must be empty when VSA_ADP_KB_ID is unset.

    Mutation: change `or ""` back to `or "EXAMPLEKB01"` (or any other literal).
    Expected: this test FAILS — DEFAULT_KB_ID equals "EXAMPLEKB01", not "".
    """
    monkeypatch.delenv("VSA_ADP_KB_ID", raising=False)
    mod = _load_module()
    assert mod.DEFAULT_KB_ID == "", (
        "DEFAULT_KB_ID must be empty when VSA_ADP_KB_ID is not set.  "
        "Remove the hardcoded fallback (e.g. 'or \"EXAMPLEKB01\"' or any other literal)."
    )


# ---------------------------------------------------------------------------
# Subprocess exit-code guard
# ---------------------------------------------------------------------------

def _run_no_id(extra_env: dict | None = None) -> subprocess.CompletedProcess:
    """Run the smoke script with VSA_ADP_KB_ID cleared and no --kb-id flag.

    Sets AWS_ENDPOINT_URL to a blocked local address so a regressed script
    that attempts a real AWS call fails fast (no network dependency).
    """
    env = {k: v for k, v in os.environ.items() if k != "VSA_ADP_KB_ID"}
    # Hermetic: if the guard regresses and AWS calls are attempted, they fail
    # immediately rather than hanging or hitting real endpoints.
    env["AWS_ENDPOINT_URL"] = "http://127.0.0.1:9"
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [sys.executable, str(SCRIPT)],
        env=env,
        capture_output=True,
        text=True,
    )


def test_exits_nonzero_when_no_kb_id_set():
    """Script must exit non-zero and name both VSA_ADP_KB_ID and --kb-id.

    Mutation: restore `or "EXAMPLEKB01"` in DEFAULT_KB_ID.
    Expected: this test FAILS — the script no longer exits early; it attempts
    AWS calls (which fail for a different reason or succeed if the KB still exists).
    """
    result = _run_no_id()
    assert result.returncode != 0, (
        "smoke-test-kb-retrieve.py exited 0 with no KB id set.  "
        "It must exit non-zero and print an actionable error to stderr."
    )
    assert "VSA_ADP_KB_ID" in result.stderr, (
        "stderr must name the VSA_ADP_KB_ID environment variable so the "
        "operator knows how to fix it."
    )
    assert "--kb-id" in result.stderr, (
        "stderr must name the --kb-id flag as an alternative."
    )
    # demo-preflight.sh greps for `^(✗|Error|Traceback)` — pin the capital 'E'
    # so a case change here (error: → Error:) is caught.
    # Mutation (S4): change `Error:` to `error:` in the script.  Expected: this
    # assertion FAILS — stderr.splitlines()[0] starts with lowercase 'e'.
    assert result.stderr.splitlines()[0].startswith("Error:"), (
        "First line of stderr must start with 'Error:' (capital E) so "
        "demo-preflight.sh's grep pattern matches it."
    )


def test_exits_zero_with_audit_set_dry_run_no_kb_id():
    """--audit-set --dry-run must exit 0 even when VSA_ADP_KB_ID is not set.

    `--dry-run` lists prompts without calling AWS, so it does not need a KB id.
    The KB-id guard must be bypassed for this path.

    Mutation: remove the early dry-run return in main() so the KB-id guard
    runs unconditionally.  Expected: this test FAILS — the script exits 1
    and stderr contains 'Error:'.
    """
    audit_set = Path(__file__).parent.parent.parent / "evals" / "kb_audit" / "meridian_audit_set.json"
    if not audit_set.exists():
        pytest.skip(f"audit set not found at {audit_set}; skipping dry-run path check")

    env = {k: v for k, v in os.environ.items() if k != "VSA_ADP_KB_ID"}
    env["AWS_ENDPOINT_URL"] = "http://127.0.0.1:9"  # hermetic
    result = subprocess.run(
        [
            sys.executable, str(SCRIPT),
            "--audit-set", str(audit_set),
            "--dry-run",
        ],
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"--audit-set --dry-run must exit 0 with no KB id set.\n"
        f"returncode: {result.returncode}\nstderr: {result.stderr}\nstdout: {result.stdout}"
    )


def test_exits_zero_when_kb_id_provided_via_flag():
    """--kb-id passes the guard; the script proceeds past it to AWS interaction.

    We run the LIVE single-query path (no --audit-set, no --dry-run) with
    VSA_ADP_KB_ID unset and --kb-id EXAMPLEKB01.  The hermetic blocked endpoint
    means the script will fail at STS or a connection attempt, but the KB-id
    guard must NOT fire — "KB id is required" must be absent from stderr.

    Mutation (S2): change `if not args.kb_id:` to
    `if not os.environ.get("VSA_ADP_KB_ID"):` so the guard ignores --kb-id.
    Expected: this test FAILS — "KB id is required" appears in stderr.
    """
    env = {k: v for k, v in os.environ.items() if k != "VSA_ADP_KB_ID"}
    # Hermetic: any AWS call fails fast at the connection rather than hanging.
    env["AWS_ENDPOINT_URL"] = "http://127.0.0.1:9"
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--kb-id", "EXAMPLEKB01"],
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
    )
    # The guard must NOT have fired.  (The script will exit non-zero for a
    # different reason — STS connection refused — which is expected and fine.)
    assert "KB id is required" not in result.stderr, (
        "The KB-id guard fired even though --kb-id was provided.  "
        "The guard must read args.kb_id, not os.environ.get('VSA_ADP_KB_ID')."
    )
