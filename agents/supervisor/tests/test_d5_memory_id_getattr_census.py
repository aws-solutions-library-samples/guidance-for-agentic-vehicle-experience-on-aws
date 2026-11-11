"""Executable guard for the spec-D5 prohibition on `getattr(config, "memory_id", …)`.

Added by Fix Group 18 review Cycle 2 (Warning 1).

``config.py:219-223`` states the rule plainly:

    These fields fix the AttributeError at 8 bare `config.memory_id` call-sites
    in tools/, where getattr was NEVER used for memory_id (unlike memory_region).
    Adding them here ends the silent failure without converting the call-sites to
    getattr — a getattr default would make the degradation uniform instead of
    ending it (spec D5).

**That rule had no executable form.** ``test_memory_config.py`` censuses the
``memory_region`` getattr sites and the *direct* ``config.memory_id`` reads, but
never the ``memory_id`` **getattr** prohibition — so the rule was documentation
only, and Fix Group 18 violated it at a fourth site without anything failing.
A rule with no executable form is not a control.

This is a **ratchet, not a clean sweep.** Three sites pre-date this guard and are
recorded below rather than fixed, because two of them are in
``agentcore_app.py``, which a concurrent session owned at the time of writing
(peer commit ``b0d6ee0``, same day). Editing a peer-held file to satisfy a guard
I am adding would be worse than recording the debt. The guard's job is to stop
the count growing and to force each removal to be deliberate.
"""

from __future__ import annotations

import ast
import pathlib
import tempfile

import pytest

SUPERVISOR = pathlib.Path(__file__).resolve().parents[1]

#: Sites that pre-date this guard, as ``(relative path, enclosing function)``.
#: Each is a real D5 violation. Shrink this set when a site is fixed — and
#: ``test_every_known_site_still_exists`` fails if you shrink it without fixing
#: the code, so the allowlist cannot rot into a lie.
#:
#: `session.py::session_manager_or_none`        — pre-existing.
#: `agentcore_app.py::invoke`                   — peer b0d6ee0; LIVE text path.
#: `agentcore_app.py::hydrate_session_history`  — peer b0d6ee0; LIVE text path.
#:
#: Note the asymmetry Cycle 2 surfaced: Fix Group 18's own fix landed on the one
#: site whose impact was already established as nil, while both live-path sites
#: remain. That is recorded here so it is not mistaken for a closed issue.
#:
#: **Keyed on enclosing FUNCTION, not line number** — review Cycle 3, Warning 1.
#: An earlier version used ``(path, lineno)``, which is unstable precisely where
#: it matters: two of the three sites live in ``agentcore_app.py``, which a
#: concurrent session edits continuously, and 2 of the last 3 commits to that
#: file inserted lines above an allowlisted site. Line-keying would have made
#: this guard assert a violation that does not exist ("New site introduced:
#: agentcore_app.py:304" for a site sitting right there in the allowlist), and a
#: permanently-red guard stops protecting its seam — this repo already carries
#: one such known-red (``test_agent_iam_has_no_connect_action``).
#:
#: Function names survive line drift. The residual weakening is honest and
#: narrower than the alternative: if a violation MOVES between functions within
#: an allowlisted file, or is deleted and a different one added in the same
#: function, the count is unchanged and this guard stays quiet. It is a ratchet
#: against accidental reintroduction, not a proof of absence.
_KNOWN_GETATTR_SITES: set[tuple[str, str]] = {
    ("session.py", "session_manager_or_none"),
    ("agentcore_app.py", "invoke"),
    ("agentcore_app.py", "hydrate_session_history"),
}

#: Files that are entrypoints/tests and not part of the census.
_SKIP_DIRS = {"tests", "__pycache__", "guardrails"}


def _scan(root: pathlib.Path) -> set[tuple[str, str]]:
    """Return every ``getattr(<name>, "memory_id", …)`` site as (path, function).

    Matches on the ARGUMENT, not on a receiver named ``config``: a call written
    ``getattr(cfg, "memory_id", None)`` or ``getattr(self.config, "memory_id")``
    is the same violation and must not slip through a receiver-name filter. This
    is the blind spot ``test_memory_config.py:33-34`` documents in its own
    scanner, recorded there as a known limitation.

    Keyed on the enclosing function so the result is stable under line drift in
    a concurrently-edited file (review Cycle 3, Warning 1). Module-level calls
    report ``"<module>"``.

    .. note:: **Acknowledged blind spots** (review Cycle 3, Suggestion 1).
       This detects the literal, direct form only. It does NOT match:

       * a variable attribute name — ``getattr(config, attr_name, None)``
       * string construction — ``getattr(config, "memory" + "_id")`` or an
         f-string
       * an aliased or module-qualified builtin — ``g = getattr`` /
         ``builtins.getattr(...)``
       * ``config.__dict__.get("memory_id")`` or ``vars(config).get(...)``

       That is adequate for a ratchet against **accidental reintroduction**,
       which is the threat model, and inadequate as a proof of absence. Stated
       explicitly because the sibling scanner this file cites documents its own
       limitation in exactly this shape, and meeting that bar is the standard
       next door.
    """
    found: set[tuple[str, str]] = set()
    for path in sorted(root.rglob("*.py")):
        if any(part in _SKIP_DIRS for part in path.relative_to(root).parts[:-1]):
            continue
        try:
            tree = ast.parse(path.read_text())
        except SyntaxError:  # pragma: no cover
            continue

        # Walk functions first so each call can be attributed to its enclosing
        # def; anything left over is module-level.
        owner: dict[int, str] = {}
        for fn in ast.walk(tree):
            if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for node in ast.walk(fn):
                    if isinstance(node, ast.Call):
                        # Inner functions win: the LAST (most deeply nested)
                        # assignment for a given call node is the closest def,
                        # because ast.walk yields outer defs before inner ones.
                        owner[id(node)] = fn.name

        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if not (isinstance(node.func, ast.Name) and node.func.id == "getattr"):
                continue
            if len(node.args) < 2:
                continue
            second = node.args[1]
            if isinstance(second, ast.Constant) and second.value == "memory_id":
                found.add(
                    (
                        str(path.relative_to(root)),
                        owner.get(id(node), "<module>"),
                    )
                )
    return found


def test_no_new_getattr_memory_id_sites():
    """The ratchet. A new D5 violation fails here.

    Mutation-verified: adding ``getattr(config, "memory_id", None)`` anywhere
    under ``agents/supervisor/`` (outside ``tests/``) fails this test.
    """
    found = _scan(SUPERVISOR)
    new = found - _KNOWN_GETATTR_SITES
    assert not new, (
        "New `getattr(config, \"memory_id\", ...)` site(s) introduced: "
        f"{sorted(new)}.\n"
        "spec D5 (config.py:219-223) forbids a getattr default for memory_id — "
        "the default makes the degradation uniform instead of ending it. Read "
        "`config.memory_id` bare so a misconfiguration surfaces.\n"
        "If this site is genuinely unavoidable, add it to _KNOWN_GETATTR_SITES "
        "with a one-line reason."
    )


def test_every_known_site_still_exists():
    """Anti-vacuity 1 — the allowlist cannot rot.

    If a recorded site is fixed or moves, this fails, forcing the allowlist to
    be shrunk deliberately rather than silently carrying entries that no longer
    describe the code. Without this, the ratchet above would keep passing
    against a stale set forever.
    """
    found = _scan(SUPERVISOR)
    stale = _KNOWN_GETATTR_SITES - found
    assert not stale, (
        f"_KNOWN_GETATTR_SITES lists site(s) that no longer exist: {sorted(stale)}. "
        "If they were fixed, remove them from the set (that is the ratchet "
        "tightening). If they only moved line, update the line number."
    )


def test_scanner_actually_detects_a_violation():
    """Anti-vacuity 2 — positive control.

    Without this, a scanner that silently matched nothing (a typo'd attribute
    name, a walk that never recurses) would make both tests above pass
    unconditionally. That is the failure mode that produced three false CAUGHTs
    in this group's first mutation battery.
    """
    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)
        (root / "victim.py").write_text(
            "def f(config):\n    return getattr(config, 'memory_id', None)\n"
        )
        found = _scan(root)
    assert found == {("victim.py", "f")}, f"scanner failed its positive control: {found}"


@pytest.mark.parametrize(
    "src,should_match",
    [
        ('getattr(config, "memory_id", None)', True),
        ("getattr(cfg, 'memory_id')", True),
        ('getattr(self.config, "memory_id", "")', True),
        ('getattr(config, "memory_region", "us-west-2")', False),
        ("config.memory_id", False),
        ('getattr(config, "memory_id_suffix", None)', False),
    ],
)
def test_scanner_discriminates_on_the_attribute_not_the_receiver(src, should_match):
    """The receiver is irrelevant; only the attribute name matters.

    `test_memory_config.py:33-34` records that its own scanner matches only a
    bare `config` Name receiver and therefore misses `self.config.memory_id`.
    This one must not inherit that limitation.
    """
    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)
        (root / "m.py").write_text(f"def f(config, cfg, self):\n    return {src}\n")
        found = _scan(root)
    assert bool(found) is should_match, f"{src!r} → {found}"
