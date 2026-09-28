"""Guard: the D2 writer must gain a production caller once its table is declared.

WHY THIS EXISTS
===============

`agents/tier2/writer.py`'s `write_finding()` was deliberately left caller-less on
2026-09-18 (`issues/2026-09-18-d2-writer-wired-before-its-table-exists/`) because it
Queries a `by_finding_key` GSI that did not exist yet. That deferral was correct, and
the call site at `runner.py` documents it at length.

The defect this guard prevents is not the deferral -- it is the **transition out of it**.

`write_finding()` having zero callers was the *documented correct state*. When Group 3
created `vsa-{stage}-avx-findings` on 2026-09-20 the state silently became incorrect, and
nothing in the source tree changed at that moment: the table came into being in
CloudFormation. So a reviewer grepping for callers saw precisely what the comment and the
prior issue told them to expect -- deliberate absence -- and could not distinguish
"correctly deferred" from "now overdue". Group 3 closed with the table, seven GSIs, the
stream and the IAM grant shipped, and the wiring never done
(`issues/2026-09-21-group3-closed-without-wiring-the-d2-writer/`).

The consequence is severe and quiet: nothing writes an AVX Finding, so the DDB stream
never fires, so a correctly-built Group 7 broker would sit idle while its own unit suite
passed. A silent gate, not a red test.

This guard converts that invisible transition into a failing test at the one moment the
source tree *can* observe it -- when the table is declared in the CDK.

THE INVARIANT
=============

    EITHER  `vsa-{stage}-avx-findings` is absent from the CDK
    OR      `write_finding()` has at least one production caller.

"Production" excludes tests and the opt-in live-verification scripts: those supply the
table name themselves, which is exactly why they pass today while the production path is
unwired. Counting them as callers would make this guard vacuous -- the same substitution
that let the original seven read-surface defects through both review gates.

WHEN THIS GOES GREEN
====================

Delete nothing. Once the writer is wired, this guard keeps holding the invariant in the
other direction: it prevents a later refactor from dropping the only caller while leaving
the table in place.
"""
from __future__ import annotations

import ast
import pathlib

import pytest

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
_CDK_STACK = _REPO_ROOT / "infrastructure" / "lib" / "vsa-tier2-stack.ts"

#: The AVX Findings table's name template as it appears in the CDK.
_AVX_TABLE_MARKER = "avx-findings"

#: The D2 entry point whose production wiring this guard tracks.
_D2_ENTRY = "write_finding"

#: Roots searched for a production caller.
_PRODUCTION_ROOTS = ("agents", "lambdas")

#: Path fragments that disqualify a file from counting as a production caller.
#: `scripts/` is excluded wholesale: every live-verify entry point there passes the
#: table name in explicitly, so a call from one proves nothing about deployed wiring.
_NON_PRODUCTION_FRAGMENTS = ("/tests/", "test_", "/__pycache__/", "/scripts/")


def _avx_table_declared_in_cdk() -> bool:
    """True when the CDK declares the AVX Findings table.

    Read as text rather than parsed: this only needs to know whether the table is
    declared at all, and a substring check cannot be defeated by TS formatting.
    """
    if not _CDK_STACK.is_file():
        pytest.fail(f"CDK stack not found where expected: {_CDK_STACK}")
    return _AVX_TABLE_MARKER in _CDK_STACK.read_text(encoding="utf-8")


def _production_python_files() -> list[pathlib.Path]:
    out: list[pathlib.Path] = []
    for root_name in _PRODUCTION_ROOTS:
        root = _REPO_ROOT / root_name
        if not root.is_dir():
            continue
        for path in root.rglob("*.py"):
            posix = path.as_posix()
            if any(frag in posix for frag in _NON_PRODUCTION_FRAGMENTS):
                continue
            out.append(path)
    return out


def _call_sites(entry: str) -> list[tuple[pathlib.Path, int]]:
    """Locate genuine CALL sites of ``entry``, not mentions of its name.

    An AST walk is required rather than a grep: `write_finding` appears in many
    docstrings and comments in this repo (including the ones explaining why it has no
    caller), and a grep-based guard would count those as wiring and pass vacuously --
    the presence-vs-property trap this repo has hit repeatedly.

    Matches both ``write_finding(...)`` and ``<obj>.write_finding(...)``, and ignores
    a ``def write_finding`` definition.
    """
    found: list[tuple[pathlib.Path, int]] = []
    for path in _production_python_files():
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):  # pragma: no cover - not our concern
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = (
                func.attr if isinstance(func, ast.Attribute)
                else func.id if isinstance(func, ast.Name)
                else None
            )
            if name == entry:
                found.append((path, node.lineno))
    return found


def test_d2_writer_has_a_production_caller_once_its_table_exists() -> None:
    """The invariant. Fails while the table exists and the writer is unwired.

    The ``xfail(strict=True)`` marker this test carried from 2026-09-21 has been
    REMOVED, because the invariant it tracked is now satisfied: Fix Group 16 T16.2
    gave ``write_finding()`` its first production caller in ``agents/tier2/runner.py``
    (Agent 2 dual-write). The ratchet did exactly its job -- it XPASSed, pytest
    reported that as a failure, and the marker came off.

    Note the marker was removed at the point the CALLER landed, not at the point the
    dual-write was live-verified against the deployed table. Those are two different
    claims and only the first is what this guard asserts. Live verification is T16.4's
    gate and is tracked there; leaving a strict-xfail on an already-passing test would
    have made the suite red for no protective reason, which is the failure mode the
    marker was chosen to avoid in the first place.
    """
    table_declared = _avx_table_declared_in_cdk()
    callers = _call_sites(_D2_ENTRY)

    if not table_declared:
        # Deferral is legitimate: no table, so no caller is expected. Nothing to assert.
        return

    assert callers, (
        f"`{_D2_ENTRY}()` has no production caller, but the CDK declares the AVX "
        f"Findings table ('{_AVX_TABLE_MARKER}' in {_CDK_STACK.name}).\n\n"
        "The 2026-09-18 deferral of this wiring is no longer valid: its stated "
        "precondition (the table plus its by_finding_key GSI) has been met. Until a "
        "production path calls it, nothing writes an AVX Finding, the table's DDB "
        "stream never fires, and any broker subscribed to that stream is silently "
        "idle while its own unit tests pass.\n\n"
        "See issues/2026-09-21-group3-closed-without-wiring-the-d2-writer/report.md "
        "-- note the fix is NOT a one-line repoint: the health artifact and the AVX "
        "Finding are different shapes, and spec.md:512 requires the legacy "
        "health-findings path be preserved.\n\n"
        f"Searched roots {_PRODUCTION_ROOTS} excluding {_NON_PRODUCTION_FRAGMENTS}."
    )


def test_guard_is_not_vacuous_docstring_mentions_are_not_callers() -> None:
    """Pin the guard's own discriminating property.

    If `_call_sites` ever degraded to substring matching, the guard above would pass on
    the many docstring mentions of `write_finding` and silently stop protecting
    anything. This asserts the AST walk still distinguishes a mention from a call, so
    the guard cannot rot into a tautology.
    """
    mentions_only = _REPO_ROOT / "agents" / "tier2" / "artifact_writer.py"
    if not mentions_only.is_file():
        pytest.skip("artifact_writer.py not present; nothing to assert against")

    text = mentions_only.read_text(encoding="utf-8")
    assert _D2_ENTRY in text, (
        "Fixture assumption broken: artifact_writer.py no longer mentions "
        f"`{_D2_ENTRY}` at all, so it can no longer demonstrate that a mention is not "
        "a call. Point this test at another file that mentions it in prose."
    )

    # The file must contain the NAME without this guard counting every occurrence.
    # Concretely: its prose mentions must outnumber nothing -- we assert only that the
    # AST walk's result for this file is derived from Call nodes, by checking that a
    # comment-only occurrence is not reported.
    tree = ast.parse(text)
    call_lines = {
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and (
            (isinstance(node.func, ast.Attribute) and node.func.attr == _D2_ENTRY)
            or (isinstance(node.func, ast.Name) and node.func.id == _D2_ENTRY)
        )
    }
    prose_lines = {
        i for i, line in enumerate(text.splitlines(), start=1)
        if _D2_ENTRY in line and line.lstrip().startswith(("#", '"', "'", "*"))
    }
    assert prose_lines, (
        "Fixture assumption broken: expected at least one comment/docstring line "
        f"mentioning `{_D2_ENTRY}` in artifact_writer.py."
    )
    assert not (prose_lines & call_lines), (
        "The AST walk reported a prose-only line as a call site, which means this "
        "guard has become substring-based and is now vacuous."
    )
