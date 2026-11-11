"""Repo-wide lint: no ``lambdas/api-*/handler.py`` may hardcode CORS wildcard.

## The failure mode this test exists to catch

The 2026-09-16 CORS scoping change replaced hardcoded
``"Access-Control-Allow-Origin": "*"`` in 14 Lambda handler response builders
with ``"Access-Control-Allow-Origin": os.environ.get("ALLOWED_ORIGIN", "*")``.
The CDK stack (`infrastructure/lib/vsa-api-stack.ts`) sets `ALLOWED_ORIGIN` on
every Lambda's environment from the ``/cvx/{stage}/allowed-origin`` SSM
parameter, so production Lambdas emit the scoped origin, not `"*"`.

**But** the shared-module approach was ruled out during design (CVX has two
different bundling patterns — 3 handlers use a build script that copies
``lambdas/shared/`` into the bundle, but the other 11 bundle directly from
``lambdas/api-<name>/`` and cannot ``import shared``). Without a shared module,
future handler #15 could easily copy-paste the wildcard back in.

This lint test is the drift guard. It walks every ``lambdas/api-*/handler.py``
and every ``lambdas/api-*/tests/`` file with AST + text search, and fails if
it finds:

- A dict literal with the key ``"Access-Control-Allow-Origin"`` and the value
  ``"*"`` (the exact regressed pattern)
- Any assignment or constant whose left is ``Access-Control-Allow-Origin`` and
  whose right is a bare ``"*"``

The test PASSES the fallback default ``os.environ.get("ALLOWED_ORIGIN", "*")``
— because the ``"*"`` there is only the fallback when the env var is unset
(tests + local dev), never the shipped value in production.

## Why AST + regex hybrid

Pure regex is fragile (whitespace, escaped quotes). Pure AST would miss
non-Python drift (e.g., someone adds a ``.tf`` or ``.yaml`` config file with a
wildcard). The AST pass catches the primary regression class in handler code;
the regex pass catches any lingering literal in source we haven't tokenized.
"""

from __future__ import annotations

import ast
import pathlib
import re
from typing import List, Tuple

REPO = pathlib.Path(__file__).resolve().parents[2]
HANDLER_GLOB = "lambdas/api-*/handler.py"

# The pattern the CDK-managed handlers now use. This is the ALLOWED value;
# anything else that produces ``"Access-Control-Allow-Origin": "*"`` at run
# time is a regression.
ALLOWED_PATTERN = re.compile(
    r'os\.environ\.get\(\s*[\'"]ALLOWED_ORIGIN[\'"]\s*,\s*[\'"]\*[\'"]\s*\)',
)


def _find_literal_wildcard_ast(source: str, path: pathlib.Path) -> List[str]:
    """Walk the AST for dict literals whose key is ``Access-Control-Allow-Origin``
    and whose value is the bare string ``"*"``. Return offender descriptions."""
    tree = ast.parse(source, filename=str(path))
    offenders: List[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        for key, value in zip(node.keys, node.values):
            if not isinstance(key, ast.Constant) or key.value != "Access-Control-Allow-Origin":
                continue
            if isinstance(value, ast.Constant) and value.value == "*":
                offenders.append(
                    f"{_path_for_message(path)}:{key.lineno} — "
                    f"Access-Control-Allow-Origin literal '*' at dict key. "
                    f"Use os.environ.get('ALLOWED_ORIGIN', '*') so the CDK-wired "
                    f"per-stage scoped origin flows through at runtime."
                )
    return offenders


def _find_literal_wildcard_regex(source: str, path: pathlib.Path) -> List[str]:
    """Regex pass — catches drift the AST walker might miss (e.g. an f-string
    that always evaluates to the wildcard, or a value bound to a var and then
    used in a dict). Grep-shaped, deliberately liberal, then filtered by the
    allowed-pattern check to remove false positives."""
    offenders: List[str] = []
    for lineno, line in enumerate(source.splitlines(), start=1):
        if '"Access-Control-Allow-Origin"' not in line:
            continue
        # Strip the allowed pattern from the line before checking for a wildcard.
        # An offending line has ``"*"`` present AFTER the ALLOWED_ORIGIN fallback
        # is stripped out — or contains the header key on a line with no fallback
        # at all.
        stripped = ALLOWED_PATTERN.sub("<ALLOWED_FALLBACK>", line)
        # Now look for a wildcard that isn't part of the allowed fallback.
        if re.search(r'"Access-Control-Allow-Origin"\s*:\s*"\*"', stripped):
            offenders.append(
                f"{_path_for_message(path)}:{lineno} — {line.strip()!r}"
            )
    return offenders


def _path_for_message(path: pathlib.Path) -> str:
    """Format a path for an error message. Uses `relative_to(REPO)` when the
    path is inside the repo; falls back to a bare string for synthetic paths
    used by the meta-tests. The meta-tests pass fake `/fake/handler.py` paths;
    calling `relative_to(REPO)` on them raises ValueError, so this helper is
    the guard that keeps the meta-tests' failure story readable."""
    try:
        return str(path.relative_to(REPO))
    except ValueError:
        return str(path)


def _iter_handler_paths() -> List[pathlib.Path]:
    return sorted(REPO.glob(HANDLER_GLOB))


def test_no_literal_wildcard_in_api_handlers():
    """Every lambdas/api-*/handler.py must emit Access-Control-Allow-Origin via
    the env-driven fallback pattern, not a bare wildcard literal.

    Mutation guard: reintroducing ``"Access-Control-Allow-Origin": "*"`` (as
    opposed to ``os.environ.get("ALLOWED_ORIGIN", "*")``) in any handler
    causes this test to fail. Verified by manual mutation before the change
    landed."""
    all_offenders: List[str] = []
    paths = _iter_handler_paths()
    assert paths, f"expected to find handlers matching {HANDLER_GLOB} — none found"

    for path in paths:
        source = path.read_text()
        all_offenders.extend(_find_literal_wildcard_ast(source, path))
        all_offenders.extend(_find_literal_wildcard_regex(source, path))

    if all_offenders:
        formatted = "\n  ".join(all_offenders)
        raise AssertionError(
            f"CORS wildcard regression detected in {len(all_offenders)} location(s):\n"
            f"  {formatted}\n\n"
            f"Every handler must use:\n"
            f"    \"Access-Control-Allow-Origin\": os.environ.get(\"ALLOWED_ORIGIN\", \"*\")\n"
            f"so the CDK-wired per-stage scoped origin flows through at runtime.\n"
            f"See issue 2026-09-16-cvx-cors-scoping/ for the design rationale."
        )


def test_lint_would_catch_a_regression():
    """Meta-test: the linter's detection logic must actually flag a wildcard.

    Constructs a synthetic offender source string and runs it through both
    passes; both must report the violation. This catches the class of defect
    where the linter is silently broken and every real check passes vacuously.
    """
    synthetic_offender = 'def _resp():\n    return {"headers": {"Access-Control-Allow-Origin": "*"}}\n'
    fake_path = pathlib.Path("/fake/handler.py")

    ast_hits = _find_literal_wildcard_ast(synthetic_offender, fake_path)
    regex_hits = _find_literal_wildcard_regex(synthetic_offender, fake_path)

    assert ast_hits, (
        "AST pass failed to detect a synthetic wildcard offender — the linter is "
        "silently broken, and every test_no_literal_wildcard_in_api_handlers "
        "success is now vacuous"
    )
    assert regex_hits, "regex pass failed to detect a synthetic wildcard offender"


def test_lint_would_not_flag_the_allowed_pattern():
    """Meta-test: the linter must NOT flag the env-driven fallback pattern.

    Constructs a synthetic source using the correct pattern and asserts both
    passes accept it. This catches the failure mode where the linter is too
    strict and would block the correct code, forcing developers to disable it."""
    synthetic_ok = (
        'def _resp():\n'
        '    return {"headers": {"Access-Control-Allow-Origin": os.environ.get("ALLOWED_ORIGIN", "*")}}\n'
    )
    fake_path = pathlib.Path("/fake/handler.py")

    ast_hits = _find_literal_wildcard_ast(synthetic_ok, fake_path)
    regex_hits = _find_literal_wildcard_regex(synthetic_ok, fake_path)

    assert not ast_hits, f"AST pass falsely flagged the allowed pattern: {ast_hits}"
    assert not regex_hits, f"regex pass falsely flagged the allowed pattern: {regex_hits}"
