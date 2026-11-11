#!/usr/bin/env python3
"""Exit non-zero if any JUnit XML report records a skipped test.

pytest exits 0 when tests are skipped, and a skipped guard reads as a passing one in
a summary line. CI jobs that gate a safety property run this after pytest so a skip
fails the job instead of hiding in the log.

Usage:
    python3 scripts/lib/fail-on-skipped-tests.py report.xml [report.xml ...]

Added 2026-10-01 (issues/2026-10-01-async-tests-inert-and-triage-tests-not-in-ci/).
"""
from __future__ import annotations

import sys
import xml.etree.ElementTree as ET
from pathlib import Path


def skipped_cases(report: Path) -> list[str]:
    """Return "classname::name" for every skipped testcase in one report.

    Counts <skipped> elements on testcases rather than trusting the suite-level
    `skipped` attribute, so the message can name what was skipped.
    """
    root = ET.parse(report).getroot()
    found: list[str] = []
    for case in root.iter("testcase"):
        if case.find("skipped") is not None:
            found.append(f"{case.get('classname', '')}::{case.get('name', '')}")
    return found


def main(argv: list[str]) -> int:
    if not argv:
        print("usage: fail-on-skipped-tests.py report.xml [report.xml ...]", file=sys.stderr)
        return 2
    total_cases = 0
    skipped: list[str] = []
    for arg in argv:
        report = Path(arg)
        if not report.is_file():
            print(f"ERROR: {report} not found; did pytest run with --junitxml?", file=sys.stderr)
            return 2
        total_cases += sum(1 for _ in ET.parse(report).getroot().iter("testcase"))
        skipped.extend(f"{report.name}: {c}" for c in skipped_cases(report))
    if total_cases == 0:
        print("ERROR: the reports contain no test cases", file=sys.stderr)
        return 1
    if skipped:
        print(f"ERROR: {len(skipped)} test(s) skipped; a skip here hides a guard:", file=sys.stderr)
        for line in skipped:
            print(f"  {line}", file=sys.stderr)
        return 1
    print(f"OK: {total_cases} test case(s), none skipped")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
