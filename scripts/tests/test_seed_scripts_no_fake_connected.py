"""
Cross-repo regression guard for the 2026-05-28 CMS fake-connected-status bug
and its 2026-07-31 CVX-side recurrence.

Prior art:
  * cms/issues/2026-05-28-cms-vehicle-fake-connected-status/{report,summary}.md
  * cms/issues/2026-07-31-fake-connected-status-regression/{report,summary}.md
  * cvx/issues/2026-07-31-persona-seed-fake-connected-regression/{report,summary}.md

The bug: seed scripts wrote hardcoded ``"connectionStatus": "connected"`` into
CMS's vehicles DynamoDB table at deploy time despite no real IoT lifecycle
event ever having fired. The UI dutifully rendered the lie as a green
"Connected" badge with a fabricated "Last Connected" timestamp.

The producer-side fix in each incident was to remove the hardcoded write and
let Redis/Flink be the sole producer of ``connectionStatus``. But the May fix
lived as a source-comment in a single CMS file, and the CVX-side regression
in July showed the guard did not cross the repo boundary.

This test is that missing cross-repo guard. It scans every ``seed*.py`` in:

  * this repo (CVX)
  * the sibling CMS repo, auto-detected via ``CMS_REPO_PATH`` env var or
    ``../connected-mobility-guidance-on-aws`` relative to this file

for the forbidden anchored pattern::

    "connectionStatus"   :   "connected"   [,}]
    'connectionStatus'   :   'connected'   [,}]

The trailing ``[,}]`` anchor is intentional. It matches the literal
unconditional write pattern (``"connectionStatus": "connected",``) but skips
the CMS conditional form (``'connected' if is_connected else 'disconnected',``)
where ``is_connected`` is known to be ``False`` — that is not a lie, it is a
retained code-shape for future flexibility documented in
``cms/deployment/scripts/seed_engineering_fleets.py:177``.

Failure = at least one match. The test prints every offending file:line
so the fix location is obvious.

If the sibling CMS repo is not present (e.g. running in CI where only this
repo is checked out), the CMS scan is skipped with a note in the test output.
The CVX scan always runs.

Manual invocation::

    cd ~/guidance-for-connected-vehicle-experience-on-aws
    python3 -m pytest scripts/tests/test_seed_scripts_no_fake_connected.py -v
"""
from __future__ import annotations

import os
import re
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Anchored pattern: quoted ``connectionStatus`` key, colon, quoted ``connected``
# value, immediately followed by ``,`` or ``}`` (with optional whitespace).
# The anchor is what lets us skip conditional writes like
# ``'connectionStatus': 'connected' if is_connected else 'disconnected',``
# where the string ``'connected'`` is followed by ` if ...`, not `,` or `}`.
FORBIDDEN_PATTERN = re.compile(
    r"""["']connectionStatus["']\s*:\s*["']connected["']\s*[,}]"""
)

# Repo roots to scan.
CVX_REPO = Path(__file__).resolve().parents[2]  # scripts/tests/<this>.py -> repo root

# Best-effort discovery of the sibling CMS repo.
def _discover_cms_repo() -> Path | None:
    env = os.environ.get("CMS_REPO_PATH")
    if env:
        p = Path(env).expanduser().resolve()
        return p if p.is_dir() else None
    candidate = CVX_REPO.parent / "connected-mobility-guidance-on-aws"
    return candidate if candidate.is_dir() else None


CMS_REPO = _discover_cms_repo()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _find_seed_scripts(root: Path) -> list[Path]:
    """
    Return every ``seed*.py`` under ``root``, ignoring venv/build artifacts.

    We match the leaf filename ``seed*.py`` (matches ``seed-persona-users.py``,
    ``seed_engineering_fleets.py``, etc.). Case-sensitive to keep the scope
    unambiguous.
    """
    if not root.is_dir():
        return []

    excluded_parts = {
        ".venv", "venv", "node_modules", "cdk.out", "build", "dist",
        "__pycache__", ".git", ".mypy_cache", ".pytest_cache",
    }
    out: list[Path] = []
    for path in root.rglob("seed*.py"):
        if any(part in excluded_parts for part in path.parts):
            continue
        # `rglob("seed*.py")` matches leaf filenames; explicit sanity check.
        if not path.name.startswith("seed"):
            continue
        out.append(path)
    return out


def _scan_file(path: Path) -> list[tuple[int, str]]:
    """Return [(line_number, matched_line_text), ...] for offending lines."""
    hits: list[tuple[int, str]] = []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return hits
    for lineno, line in enumerate(text.splitlines(), start=1):
        # Skip anything inside a Python comment. Cheap heuristic: if a `#`
        # appears in the line before the pattern match, treat the pattern as
        # documentation, not code. This keeps our own docstring / comment
        # references from tripping the linter.
        m = FORBIDDEN_PATTERN.search(line)
        if not m:
            continue
        prefix = line[: m.start()]
        if "#" in prefix:
            continue
        hits.append((lineno, line.rstrip()))
    return hits


# ---------------------------------------------------------------------------
# Test
# ---------------------------------------------------------------------------

def test_no_hardcoded_connected_status_in_seed_scripts(capsys):
    """
    Every ``seed*.py`` in CVX (and CMS, when present) must not write a
    hardcoded ``"connectionStatus": "connected"`` literal. See module
    docstring for rationale and prior art.
    """
    scanned_roots: list[tuple[str, Path]] = [("CVX", CVX_REPO)]
    if CMS_REPO is not None:
        scanned_roots.append(("CMS", CMS_REPO))
    else:
        # Loud note for humans — not a test failure.
        print(
            "NOTE: sibling CMS repo not found "
            "(set CMS_REPO_PATH or check out at ../connected-mobility-guidance-on-aws). "
            "Only CVX is being scanned."
        )

    all_hits: list[str] = []
    scanned_file_count = 0

    for label, root in scanned_roots:
        seed_files = _find_seed_scripts(root)
        scanned_file_count += len(seed_files)
        print(f"[{label}] scanning {len(seed_files)} seed*.py under {root}")
        for f in seed_files:
            for lineno, text in _scan_file(f):
                # Report path relative to the scanned root for readability.
                try:
                    rel = f.relative_to(root)
                except ValueError:
                    rel = f
                all_hits.append(f"{label}: {rel}:{lineno}: {text.strip()}")

    if scanned_file_count == 0:
        pytest.fail(
            "Prevention lint found zero seed*.py files to scan across CVX "
            f"({CVX_REPO}) and CMS ({CMS_REPO}). The lint is not doing its "
            "job; check the repo layout."
        )

    assert not all_hits, (
        "Hardcoded `\"connectionStatus\": \"connected\"` write(s) found in "
        "seed script(s). This is a regression of the 2026-05-28 CMS "
        "fake-connected-status fix — seed code MUST NOT lie about live "
        "state; leave `connectionStatus` absent (or set to `disconnected`) "
        "and let Redis/Flink populate it from real telemetry events. See "
        "the module docstring for prior-art paths.\n\nOffending lines:\n  "
        + "\n  ".join(all_hits)
    )
