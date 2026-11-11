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

# A requirement line counts as pinned only if it is exactly `name[extras]==version`,
# optionally followed by an environment marker. Every other requirement line is
# unpinned. Deliberately not a full PEP 508 parser.
#
# History (2026-10-01, issues/2026-10-01-async-tests-inert-and-triage-tests-not-in-ci/).
# This used to be a regex for `name` followed directly by a range operator. It missed
# `PyJWT[crypto]>=2.8` (extras before the operator), which hid the WebSocket
# authorizer's unpinned PyJWT
# (issues/2026-10-01-triage-and-authorizer-bundles-ship-unpinned-pyjwt/). Review
# cycle 2 then found it also passed a bare name (`boto3`), a wildcard (`boto3==1.*`)
# and a space before extras, so the check was inverted to accept only exact pins.
_REQ_NAME = re.compile(r"^(?P<name>[A-Za-z0-9_.\-]+)")
_PINNED = re.compile(
    r"^[A-Za-z0-9_.\-]+(?:\s*\[[^\]]*\])?\s*==\s*[^\s*;,=<>!~]+\s*(?:;.*)?$"
)
# Strip an inline comment (`#` after whitespace), per-line hash options, and a
# trailing line continuation before checking the requirement itself.
_INLINE_TAIL = re.compile(r"\s+(?:#|--hash[= ]).*$|\s*\\$")

# Lines that are pinned, or not requirements at all, are skipped.
_SKIP_PREFIXES = ("#", "-r", "-c", "-e", "-i", "--", "http://", "https://", "git+")

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
# Shrunk 2026-10-01 from 3 to 1: agents/supervisor/requirements-dev.txt
# {pytest, pytest-asyncio} pinned
# (issues/2026-10-01-async-tests-inert-and-triage-tests-not-in-ci/).
# The tenant-config-loader entry was the last one outstanding.
#
# The stricter pin check above surfaced the WebSocket authorizer's
# `PyJWT[crypto]>=2.8` on 2026-10-01; it was recorded here, then pinned the same day
# (issues/2026-10-01-triage-and-authorizer-bundles-ship-unpinned-pyjwt/).
_KNOWN_GAPS: set[tuple[str, str]] = {
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
    rel = path.relative_to(REPO_ROOT).as_posix() if path.is_relative_to(REPO_ROOT) else path.name
    found: list[tuple[str, str]] = []
    for raw in path.read_text().splitlines():
        line = _INLINE_TAIL.sub("", raw.strip()).strip()
        if not line or line.startswith(_SKIP_PREFIXES):
            continue
        m = _REQ_NAME.match(line)
        if m and not _PINNED.match(line):
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



def _pins(path: Path) -> dict[str, str]:
    """name (lowercased) -> exact version, for every `name==version` line."""
    out: dict[str, str] = {}
    for raw in path.read_text().splitlines():
        line = raw.split("#", 1)[0].strip()
        m = re.match(r"^([A-Za-z0-9_.\-]+)(?:\[[^\]]*\])?==(\S+)$", line)
        if m:
            out[m.group(1).lower()] = m.group(2)
    return out


def test_triage_test_deps_match_the_bundle():
    """The triage tests must run against the library versions the Lambda bundle ships.

    `requirements-dev.txt` (what the CI job `verify_triage_classifier` and developers
    install) repeats pins from `lambdas/triage-classifier/requirements.txt` (what
    `scripts/build-triage-classifier.sh` bundles). Added 2026-10-01 with that job
    (issues/2026-10-01-async-tests-inert-and-triage-tests-not-in-ci/).
    """
    bundle = _pins(REPO_ROOT / "lambdas" / "triage-classifier" / "requirements.txt")
    dev = _pins(REPO_ROOT / "requirements-dev.txt")
    shared = sorted(set(bundle) & set(dev))
    # Anti-vacuity: the packages the classifier tests import at module scope.
    assert {"pyyaml", "redis", "boto3"} <= set(shared), shared
    drift = {name: (bundle[name], dev[name]) for name in shared if bundle[name] != dev[name]}
    assert not drift, f"bundle vs requirements-dev.txt pins differ: {drift}"


@pytest.mark.parametrize(
    "line, pinned",
    [
        ("boto3==1.43.6", True),
        ("PyJWT[crypto]==2.12.1", True),
        ("moto[dynamodb]==5.2.3", True),
        ('async-timeout==4.0.3; python_version < "3.11"', True),
        ("pyyaml==6.0.2  # inline comment", True),
        ("pyyaml==6.0.2\t# tab before the comment", True),
        ("pyyaml==6.0.2 --hash=sha256:abc", True),
        ("pyyaml==6.0.2 \\", True),
        ("-c constraints.txt", True),
        ("-i https://pypi.org/simple", True),
        ("boto3  # no version, just a comment", False),
        ("boto3", False),
        ("boto3==1.*", False),
        ("boto3>=1.34", False),
        ("PyJWT[crypto]>=2.8", False),
        ("PyJWT [crypto]>=2.8", False),
        ("redis~=5.3", False),
        ("redis==5.3.1,<6", False),
    ],
)
def test_the_pin_check_distinguishes_pinned_from_unpinned(tmp_path, line, pinned):
    """Positive and negative controls for _unpinned_in (review cycle 2, S4)."""
    req = tmp_path / "requirements-probe.txt"
    req.write_text(line + "\n")
    assert (not _unpinned_in(req)) is pinned, line


_JWT_STACK = ("pyjwt", "cryptography")


def _lambda_requirements() -> list[Path]:
    return sorted(p for p in (REPO_ROOT / "lambdas").glob("*/requirements.txt") if p.is_file())


def test_every_lambda_jwt_stack_matches_the_root_pins():
    """One reviewed PyJWT / cryptography version across every Lambda bundle.

    The WebSocket authorizer verifies tokens with these libraries, and the triage and
    live-state bundles carry PyJWT through redis. Every Lambda requirements file that
    pins either one must pin the same version as the root requirements.txt (the
    AgentCore runtime), so a bump is one reviewed change rather than several that
    drift. Added 2026-10-01
    (issues/2026-10-01-triage-and-authorizer-bundles-ship-unpinned-pyjwt/).
    """
    root = _pins(REPO_ROOT / "requirements.txt")
    checked: list[str] = []
    drift: list[str] = []
    for req in _lambda_requirements():
        lam = _pins(req)
        for name in _JWT_STACK:
            if name in lam:
                rel = req.relative_to(REPO_ROOT).as_posix()
                checked.append(f"{rel}:{name}")
                if lam[name] != root[name]:
                    drift.append(f"{rel}: {name}=={lam[name]}, root pins {root[name]}")
    # Anti-vacuity: the files this rule exists for are among those checked.
    for must in ("lambdas/api-ws-authorizer/requirements.txt:pyjwt",
                 "lambdas/api-ws-authorizer/requirements.txt:cryptography",
                 "lambdas/triage-classifier/requirements.txt:pyjwt"):
        assert must in checked, f"{must} not checked; found {checked}"
    assert not drift, "\n".join(drift)


@pytest.mark.parametrize("bundle_file", [
    "lambdas/api-ws-authorizer/requirements.txt",
    "lambdas/triage-classifier/requirements.txt",
    "lambdas/api-vehicle-live-state/requirements.txt",
])
def test_bundles_that_load_pyjwt_pin_it(bundle_file):
    """These bundles import jwt directly or through redis, so PyJWT must be pinned there
    rather than left to whatever redis's `PyJWT>=2.9.0` resolves to on the build day."""
    assert "pyjwt" in _pins(REPO_ROOT / bundle_file), f"{bundle_file} does not pin PyJWT"
