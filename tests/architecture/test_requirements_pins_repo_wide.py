"""Repo-wide guard: no unpinned version specifier in any requirements file.

## Why this exists

`tests/architecture/test_bidi_runtime_deps_pinned.py` (added 2026-09-21 with
`issues/2026-09-21-bidi-voice-import-error-unpinned-sdk/`) pins exactly two
packages in exactly one file: `aws_sdk_bedrock_runtime` and `strands-agents` in
repo-root `requirements.txt`.

That guard would not have caught the same outage arriving through any other
package. The defect was never "this one package was unpinned" — it was "a range
on a dependency is an unreviewed upgrade on every container rebuild." A guard
that enumerates two package names asserts the *instance*, not the *property*,
which is the same shape as a test asserting a thing exists rather than asserting
what makes it safe.

This file asserts the property, repo-wide, and ratchets: every currently-unpinned
line is recorded in `_KNOWN_GAPS` so the suite lands green, and the ratchet test
fails if an entry is fixed without being removed from the list. New unpinned
lines fail immediately.

Policy source: `~/.kiro/steering/dependency-versions.md` — "All dependency
versions MUST be pinned to exact versions. No ranges, no floating specifiers."
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

# A requirement line opening with a name then a non-`==` specifier.
# Deliberately does not try to parse PEP 508 in full — it only needs to catch
# range specifiers on a leading package name.
_UNPINNED = re.compile(r"^(?P<name>[A-Za-z0-9_.\-]+)\s*(?P<op>>=|~=|<=|!=|>|<)")

# Lines that are pinned, or not requirements at all, are skipped.
_SKIP_PREFIXES = ("#", "-r", "-e", "--", "http://", "https://", "git+")

# Every unpinned specifier still present. Shrink this list as each is pinned.
# Do NOT add to it for new code — a new unpinned dependency should fail this
# suite, which is the entire point.
#
# Shrunk 2026-09-21 from 9 entries to 3:
#   * lambdas/triage-classifier/{pyyaml,boto3,redis} — pinned; it is on a real
#     deploy path (scripts/build-triage-classifier.sh installs from it).
#   * agents/supervisor/requirements.txt {boto3,requests,python-frontmatter} —
#     the file was DELETED (see test_deleted_supervisor_requirements_stays_deleted).
#
# The 2 remaining `requirements-dev.txt` entries are dev-only and never ship;
# the tenant-config-loader entry is the last shipped one outstanding.
_KNOWN_GAPS: set[tuple[str, str]] = {
    ("agents/supervisor/requirements-dev.txt", "pytest"),
    ("agents/supervisor/requirements-dev.txt", "pytest-asyncio"),
    ("lambdas/tenant-config-loader/requirements.txt", "boto3"),
}

# Deleted 2026-09-21. Declaring a runtime dependency here instead of in the root
# `requirements.txt` means it does not ship: agentcore auto-detects the root file
# and `.bedrock_agentcore.yaml` sets no override. That mistake took the runtime
# down once already (`redis` declared here, absent from root, tools package
# failed to import at module scope — issues/2026-09-10-agentcore-runtime-missing-redis/),
# and the file's continued existence muddied the 2026-09-11 bidi diagnosis.
_DELETED_SHADOW_REQUIREMENTS = "agents/supervisor/requirements.txt"


def _requirements_files() -> list[Path]:
    """Every tracked requirements*.txt, excluding build artifacts and venvs."""
    out: list[Path] = []
    for p in REPO_ROOT.rglob("requirements*.txt"):
        rel = p.relative_to(REPO_ROOT).as_posix()
        if any(
            seg in rel
            for seg in ("cdk.out/", ".venv", "node_modules/", "site-packages/", "build/")
        ):
            continue
        out.append(p)
    return sorted(out)


def _unpinned_in(path: Path) -> list[tuple[str, str]]:
    """Return (relpath, package) for each unpinned requirement line."""
    rel = path.relative_to(REPO_ROOT).as_posix()
    found: list[tuple[str, str]] = []
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith(_SKIP_PREFIXES):
            continue
        m = _UNPINNED.match(line)
        if m:
            found.append((rel, m.group("name")))
    return found


def _all_unpinned() -> set[tuple[str, str]]:
    found: set[tuple[str, str]] = set()
    for f in _requirements_files():
        found.update(_unpinned_in(f))
    return found


def test_requirements_files_are_discovered():
    """Anti-vacuity: the walker must actually find the known requirements files.

    Without this, a broken glob or an over-eager exclude would make every
    assertion below pass by examining nothing.
    """
    files = {p.relative_to(REPO_ROOT).as_posix() for p in _requirements_files()}
    assert "requirements.txt" in files, f"root requirements.txt not discovered: {files}"
    assert len(files) >= 8, f"expected >=8 requirements files, found {len(files)}: {files}"


def test_no_new_unpinned_requirements():
    """The property: no unpinned specifier outside the recorded ratchet."""
    new = _all_unpinned() - _KNOWN_GAPS
    assert not new, (
        "Unpinned dependency specifier(s) found that are not in _KNOWN_GAPS:\n"
        + "\n".join(f"  {f}: {pkg}" for f, pkg in sorted(new))
        + "\n\nPin to an exact version (==X.Y.Z) per "
        "~/.kiro/steering/dependency-versions.md. A range on a dependency is an "
        "unreviewed upgrade on every rebuild — that is what killed bidi voice "
        "for 9 days (issues/2026-09-21-bidi-voice-import-error-unpinned-sdk)."
    )


def test_known_gaps_are_still_real():
    """The ratchet: a fixed gap must be removed from _KNOWN_GAPS.

    Keeps the list from rotting into a permanent allowlist that silently
    tolerates regressions at entries already fixed.
    """
    stale = _KNOWN_GAPS - _all_unpinned()
    assert not stale, (
        "_KNOWN_GAPS lists entries that are no longer unpinned — delete them:\n"
        + "\n".join(f"  {f}: {pkg}" for f, pkg in sorted(stale))
    )


@pytest.mark.parametrize("package", ["requests", "python-frontmatter"])
def test_root_requirements_pins_the_2026_09_21_fixes(package: str):
    """Regression lock for the two pins added 2026-09-21.

    These are called out by name because they live in the same file as the
    outage and were the last two ranges in it. Reverting either to a range
    must fail loudly rather than merely re-entering _KNOWN_GAPS.
    """
    root = REPO_ROOT / "requirements.txt"
    pattern = re.compile(rf"^{re.escape(package)}==\S+$", re.MULTILINE)
    assert pattern.search(root.read_text()), (
        f"{package} must be pinned exactly (==) in requirements.txt; "
        "it was pinned 2026-09-21 and a range here is a regression."
    )


def test_deleted_supervisor_requirements_stays_deleted():
    """The shadow requirements file must not come back.

    Root `requirements.txt` is the only file the AgentCore runtime installs
    from — agentcore auto-detects it at source_path and
    `.bedrock_agentcore.yaml` sets no override. A sibling file next to the
    entrypoint reads like the natural place to declare a runtime dependency,
    and doing so means the dependency does not ship.

    That is not hypothetical: `redis` was declared only in the sibling, agentcore
    bundled root which lacked it, and the runtime could not initialize because
    the tools package imports redis at module scope
    (issues/2026-09-10-agentcore-runtime-missing-redis/). The file's continued
    existence then muddied the 2026-09-11 bidi-outage diagnosis.

    Deleted 2026-09-21. If a runtime dependency is needed, add it to root.
    """
    shadow = REPO_ROOT / _DELETED_SHADOW_REQUIREMENTS
    assert not shadow.exists(), (
        f"{_DELETED_SHADOW_REQUIREMENTS} was deleted 2026-09-21 and must stay "
        "deleted. Nothing installs from it — declaring a runtime dependency "
        "there means it does NOT ship, which took the runtime down once "
        "(issues/2026-09-10-agentcore-runtime-missing-redis/). Add runtime "
        "dependencies to the ROOT requirements.txt instead."
    )


def test_root_requirements_does_not_claim_a_sibling_is_authoritative():
    """The false claim, not just the file, was the causal mechanism.

    Root `requirements.txt` carried "The authoritative copy is
    agents/supervisor/requirements.txt; this is kept in sync manually." That
    sentence actively directed people to declare dependencies in the file that
    does not ship. Deleting the sibling without deleting the instruction would
    leave the trap pointing at a path that no longer exists.
    """
    text = (REPO_ROOT / "requirements.txt").read_text()
    lowered = text.lower()
    assert "authoritative copy is agents/supervisor" not in lowered, (
        "root requirements.txt must not claim a sibling file is authoritative — "
        "that claim caused issues/2026-09-10-agentcore-runtime-missing-redis/."
    )
    assert "kept in sync manually" not in lowered, (
        "root requirements.txt must not describe a manual two-file sync; there "
        "is only one runtime requirements file and it is this one."
    )
