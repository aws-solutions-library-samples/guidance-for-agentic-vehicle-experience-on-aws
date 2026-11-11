"""Regression guard: no hardcoded credential defaults in seed scripts.

Prior art:
  * cvx/issues/2026-07-28-unauthorized-prod-persona-seed/{report,summary}.md
  * cvx/issues/2026-08-03-cvx-demo-password-rotation/report.md

The bug: ``scripts/seed-persona-users.py`` read

    DEMO_PASSWORD = (os.environ.get("DEMO_PASSWORD") or "").strip() or "PLACEHOLDER-PW"

so any run that did not set ``DEMO_PASSWORD`` force-set the same well-known
password on every seeded persona in whichever Cognito pool was targeted —
including production, per the 2026-07-28 incident. The same literal was also
published in ``agents/supervisor/README.md``, ``docs/DEMO-RUNBOOK.md`` and the
``seed-staging`` Makefile target, making it a shared credential documented in
the repo.

Why a test rather than a comment: the 2026-07-31 fake-connected regression in
this same directory demonstrated that a source-comment guard does not survive.
A failing test does.

Why entropy scanners do not cover this: verified 2026-08-03 that ``gitleaks``
8.30.1 reports **zero** findings for that value across both the working tree
and all 127 commits of history. It is too short and too low-entropy to trip
generic secret detection. Detection therefore has to be structural — this test,
plus an explicit ``forbidden_patterns`` entry in ``.publish-secrets-scan.yml``
(the latter is tracked in the rotation issue and owned by the publish flow).

What this scans: every ``seed*.py`` under ``scripts/`` in this repo, for an
assignment to a password-ish name whose fallback is a string literal. The
allowed shapes are an env read with no literal fallback, or a fallback that
calls a generator::

    DEMO_PASSWORD = (os.environ.get("DEMO_PASSWORD") or "").strip()          # ok
    DEMO_PASSWORD = (os.environ.get("DEMO_PASSWORD") or "").strip() or _random_pw()   # ok
    DEMO_PASSWORD = (os.environ.get("DEMO_PASSWORD") or "").strip() or "PLACEHOLDER-PW"    # FAIL
    PASSWORD = "hunter2"                                                     # FAIL

Empty-string fallbacks (``or ""``) are permitted: they carry no credential and
are the standard "unset" sentinel used throughout these scripts.

Manual invocation::

    cd ~/guidance-for-connected-vehicle-experience-on-aws
    python3 -m pytest scripts/tests/test_seed_no_hardcoded_password.py -v
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPTS_DIR = _REPO_ROOT / "scripts"

# Assignment to a password-ish identifier whose value ends in a non-empty
# string literal. Captures both `X = "lit"` and `... or "lit"` forms.
#
# `(?!\s*["\']{2})` lets `or ""` through — an empty sentinel is not a credential.
_HARDCODED_PW_RE = re.compile(
    r"""^\s*                                  # start of line
        (?!\#)                                # not a comment
        (?P<name>[A-Za-z_][A-Za-z0-9_]*        # identifier containing...
            (?:PASSWORD|PASSWD|PWD|SECRET)     # ...a credential-ish token
            [A-Za-z0-9_]*)
        \s*(?::[^=]+)?=                        # optional annotation, then =
        (?P<value>.*)$                         # the whole RHS
    """,
    re.IGNORECASE | re.VERBOSE,
)

# A non-empty string literal appearing as the final `or` fallback, or as the
# entire RHS. Empty literals ("" / '') are explicitly excluded.
_LITERAL_FALLBACK_RE = re.compile(
    r"""(?:^|\bor\s+)                 # RHS start, or an `or` fallback
        (?P<lit>"[^"]+"|'[^']+')      # a NON-empty string literal
        \s*(?:\#.*)?$                 # to end of line, comments allowed
    """,
    re.VERBOSE,
)


def _seed_scripts() -> list[Path]:
    return sorted(_SCRIPTS_DIR.glob("seed*.py"))


def _offending_lines(path: Path) -> list[tuple[int, str]]:
    hits: list[tuple[int, str]] = []
    for lineno, raw in enumerate(path.read_text().splitlines(), start=1):
        m = _HARDCODED_PW_RE.match(raw)
        if not m:
            continue
        value = m.group("value")
        if _LITERAL_FALLBACK_RE.search(value):
            hits.append((lineno, raw.strip()))
    return hits


def test_seed_scripts_exist():
    """Guard the glob itself — a silent zero-file scan would pass vacuously."""
    scripts = _seed_scripts()
    assert scripts, f"no seed*.py found under {_SCRIPTS_DIR} — glob is wrong"
    names = {p.name for p in scripts}
    assert "seed-persona-users.py" in names, (
        f"seed-persona-users.py missing from scan set: {sorted(names)}"
    )


@pytest.mark.parametrize("script", _seed_scripts(), ids=lambda p: p.name)
def test_no_hardcoded_password_default(script: Path):
    hits = _offending_lines(script)
    assert not hits, (
        f"{script.relative_to(_REPO_ROOT)} assigns a credential from a hardcoded "
        f"string literal. Read it from the environment, or generate one — do not "
        f"bake in a shared default.\n"
        + "\n".join(f"  line {n}: {text}" for n, text in hits)
    )


def test_detector_catches_the_original_bug(tmp_path):
    """The regression this test exists for must actually trip it.

    Without this, a broken regex would let the suite pass vacuously — the same
    trap that made the first pass of the 2026-08-03 nosemgrep annotations look
    verified when they were not.
    """
    bad = tmp_path / "seed-bad.py"
    bad.write_text(
        'import os\n'
        'DEMO_PASSWORD = (os.environ.get("DEMO_PASSWORD") or "").strip() or "PLACEHOLDER-PW"\n'
    )
    hits = _offending_lines(bad)
    assert len(hits) == 1, f"detector missed the original bug shape: {hits}"
    assert "PLACEHOLDER-PW" in hits[0][1]


@pytest.mark.parametrize(
    "line",
    [
        'DEMO_PASSWORD = (os.environ.get("DEMO_PASSWORD") or "").strip()',
        'DEMO_PASSWORD = (os.environ.get("DEMO_PASSWORD") or "").strip() or _random_pw()',
        'PASSWORD = os.environ["PW"]',
        '# DEMO_PASSWORD = "PLACEHOLDER-PW"  historical note in a comment',
        'DEMO_PASSWORD = ""',
    ],
)
def test_detector_allows_safe_shapes(tmp_path, line):
    """No false positives on the forms we want people to use."""
    ok = tmp_path / "seed-ok.py"
    ok.write_text(f"import os\n{line}\n")
    assert not _offending_lines(ok), f"false positive on a safe line: {line!r}"
