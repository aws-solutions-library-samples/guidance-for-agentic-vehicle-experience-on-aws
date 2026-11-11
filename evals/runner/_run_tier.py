"""Wrapper that runs a tier's pytest suite and emits a JSON report.

The JSON schema is shared with `evals/runner/reporter.py` (Task 5a) and
`evals/baselines/*.json` (Task 5c). Schema:

    {
      "tier": int,
      "schema_version": 1,
      "generated_at": ISO-8601 string,
      "cases": [{"id", "yaml_path", "outcome", "latency_ms",
                 "expected_tool_calls", "actual_tool_calls", "failure_reason"}, ...],
      "summary": {"total", "passed", "failed", "skipped",
                  "latency_p50_ms", "latency_p99_ms", "duration_ms"}
    }

Implementation note: this is intentionally simple. We invoke pytest as a
subprocess and parse the `-v` output for per-case PASSED/FAILED/SKIPPED.
We do NOT capture per-case latency or actual tool calls here — those are
populated as zero / empty. The reporter (5a) tolerates missing latency
data. Future iteration: switch to `pytest-junitxml` or a custom plugin to
extract richer per-case data.

Usage:
    python3 -m evals.runner._run_tier --tier 1 --output /tmp/eval-tier1.json
"""
from __future__ import annotations

import argparse
import datetime
import glob
import json
import os
import subprocess
import sys
import time
from pathlib import Path

TIER_TO_RUNNER = {
    1: "evals/runner/tier1_tools.py",
    2: "evals/runner/tier2_conversation.py",
    3: "evals/runner/tier3_e2e.py",
}

TIER_TO_CASES_GLOB: dict[int, str | list[str]] = {
    1: "evals/cases/tools/**/*.yaml",
    2: [
        "evals/cases/routing/**/*.yaml",
        "evals/cases/safety/**/*.yaml",
        "evals/cases/knowledge/**/*.yaml",
    ],
    3: "evals/cases/e2e/**/*.yaml",
}


def collect_case_files(tier: int) -> list[str]:
    """Discover and return all case YAML files for a given tier."""
    pat = TIER_TO_CASES_GLOB[tier]
    files: list[str] = []
    if isinstance(pat, str):
        files = glob.glob(pat, recursive=True)
    else:
        for p in pat:
            files.extend(glob.glob(p, recursive=True))
    return sorted(files)


def parse_pytest_output(stdout: str) -> list[dict]:
    """Parse pytest -v stdout for per-test outcomes.

    Matches lines like:
        evals/runner/tier1_tools.py::test_tool_unit[case-id] PASSED [  5%]
    """
    cases: list[dict] = []
    for line in stdout.splitlines():
        for outcome in ("PASSED", "FAILED", "SKIPPED"):
            marker = f" {outcome}"
            if marker not in line or "::" not in line:
                continue
            pre, _, _ = line.partition(marker)
            test_id = pre.split("::", 1)[1] if "::" in pre else pre
            test_id = test_id.strip()
            if "[" in test_id and test_id.endswith("]"):
                case_id = test_id[test_id.index("[") + 1 : -1]
            else:
                case_id = test_id
            cases.append(
                {
                    "id": case_id,
                    "yaml_path": "",
                    "outcome": outcome.lower(),
                    "latency_ms": 0.0,
                    "expected_tool_calls": [],
                    "actual_tool_calls": [],
                    "failure_reason": None if outcome == "PASSED" else "see pytest log",
                }
            )
            break
    return cases


def main() -> int:
    """Orchestrate tier-specific test runner (pytest wrapper)."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tier", type=int, choices=[1, 2, 3], required=True)
    parser.add_argument("--output", required=True, help="Path to write JSON report")
    args = parser.parse_args()

    runner_path = TIER_TO_RUNNER[args.tier]
    if not Path(runner_path).exists():
        print(f"Runner not found: {runner_path}", file=sys.stderr)
        return 2

    start = time.perf_counter()
    # Verified not injectable (see issues/2026-08-03-cvx-probe-critical-triage/
    # report.md): every argv element is static. `runner_path` is not user input —
    # it is a lookup into the module-level TIER_TO_RUNNER constant, keyed by
    # `args.tier`, which argparse constrains to `choices=[1, 2, 3]`, so the value
    # can only ever be one of three hard-coded repo-relative paths. `shell=False`
    # (the default) is in force, so no shell metacharacter interpretation occurs
    # and shlex quoting is inapplicable to this list form. The `env=` kwarg
    # extends os.environ rather than forwarding attacker-controlled values.
    #
    # The directive is repeated on two lines below because rules in this family
    # anchor to different nodes: `dangerous-subprocess-use-tainted-env-args`
    # reports on the argv list literal, while `dangerous-subprocess-use-audit`
    # reports on the `subprocess.run(` call itself. A `# nosemgrep` only applies
    # to the line it sits on or the line immediately after it — intervening
    # comment lines break it — so prose must never separate a directive from its
    # target. Verified empirically 2026-08-03.
    # nosemgrep: dangerous-subprocess-use-audit, dangerous-subprocess-use-tainted-env-args
    proc = subprocess.run(  # nosec B603 — static argv, shell=False
        # nosemgrep: dangerous-subprocess-use-audit, dangerous-subprocess-use-tainted-env-args
        [
            sys.executable,
            "-m",
            "pytest",
            runner_path,
            "-v",
            "--tb=short",
            "--no-header",
            "--color=no",  # Disable ANSI codes so the parser can match PASSED/FAILED/SKIPPED literally.
        ],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "NO_COLOR": "1"},
    )
    duration_ms = (time.perf_counter() - start) * 1000.0

    cases = parse_pytest_output(proc.stdout)

    summary = {
        "total": len(cases),
        "passed": sum(1 for c in cases if c["outcome"] == "passed"),
        "failed": sum(1 for c in cases if c["outcome"] == "failed"),
        "skipped": sum(1 for c in cases if c["outcome"] == "skipped"),
        "latency_p50_ms": 0.0,
        "latency_p99_ms": 0.0,
        "duration_ms": duration_ms,
    }

    report = {
        "tier": args.tier,
        "schema_version": 1,
        "generated_at": datetime.datetime.now(datetime.UTC)
        .isoformat()
        .replace("+00:00", "Z"),
        "cases": cases,
        "summary": summary,
    }

    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, indent=2))

    print(
        f"Tier {args.tier}: {summary['passed']}/{summary['total']} passed, "
        f"{summary['failed']} failed, {summary['skipped']} skipped, "
        f"{duration_ms:.0f}ms",
        file=sys.stderr,
    )

    # Exit code mirrors pytest: 0 on success, non-zero on any failure.
    # Note: a Tier 3 pytest run with no RUNTIME_ID emits skips and pytest
    # returns 5 (no tests collected) or 0 (skipped). We treat both as success
    # because the wrapper's job is to emit a valid report, not to enforce
    # business rules. The reporter (5a) decides regression status.
    if proc.returncode in (0, 5):
        return 0
    return proc.returncode


if __name__ == "__main__":
    raise SystemExit(main())
