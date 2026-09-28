"""Static-analysis gate for `agents/executors/`.

Review Cycle 4's Critical was a one-line `F821`: `registry.py` called `log.error`
in a module that defined no logger, so the escalation guard raised `NameError`
every time it ran and still destroyed the `ExecuteResult` it was written to
protect. It shipped because no test entered that branch, and it survived a
mutation battery because that battery's entry for the property was itself
vacuous.

A linter reports it in milliseconds. None was installed.

This test closes the CLASS rather than the instance, which is what the reviewer
asked for: undefined names, and a few other categories that are always bugs
rather than style, fail the suite. Style is deliberately out of scope — a gate
that argues about formatting gets disabled by whoever it annoys.

`pyflakes==3.2.0` is pinned in `requirements-dev.txt` per
`~/.kiro/steering/dependency-versions.md`. Install it with
`pip install -r requirements-dev.txt`.

`test_the_lint_gate_is_not_silently_skipped` FAILS rather than skips when pyflakes
is absent, because a skipped guard and a passing guard are indistinguishable in a
summary line — which is how a sibling guard in this package (an uncollected test
in `conftest.py`) hid for a whole cycle. Review Cycle 5 rightly noted that this
makes the suite red on a machine without the dependency, which is why the
dependency is now declared rather than merely mentioned.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

EXECUTORS_DIR = Path(__file__).resolve().parent.parent

#: Categories that are always defects, never style. Anything pyflakes reports
#: whose message matches one of these fails the gate.
#:
#: Deliberately EXCLUDES unused imports (F401), which are hygiene rather than
#: bugs — a gate that fails on those gets disabled by whoever it annoys, and then
#: it is not guarding F821 either. "local variable" is kept because it covers
#: referenced-before-assignment, which IS always a bug; it also catches merely
#: unused locals, and the three in this package were cleaned rather than
#: excluded, since one turned out to be dead scaffolding in a test.
_ALWAYS_A_BUG = (
    "undefined name",              # F821 — the Cycle 4 Critical
    "local variable",              # F821/F823 — referenced before assignment
    "redefinition of unused",      # F811 — a second def silently wins
    "duplicate argument",          # F831
    "f-string is missing placeholders",  # F541 — usually a lost variable
    "is assigned to but never used in an f-string",
    "invalid syntax",
    "'return' outside function",
    "use ==/!=",                   # F632's actual wording in pyflakes 3.x
)


def _pyflakes_available() -> bool:
    try:
        import pyflakes  # noqa: F401  (probe only)

        return True
    except ImportError:
        return False


def _run_pyflakes() -> list[str]:
    """Return pyflakes' findings, or raise if pyflakes itself failed to run.

    pyflakes exits 0 (clean) or 1 (findings). Anything else means it did not run —
    a broken install, a bad path, an incompatible interpreter. The first version
    of this function discarded the return code, so such a failure produced an
    empty finding list and the gate passed GREEN: a silent pass, in the one file
    whose whole argument is that silent passes are how defects ship.
    """
    proc = subprocess.run(
        [sys.executable, "-m", "pyflakes", str(EXECUTORS_DIR)],
        capture_output=True,
        text=True,
    )
    if proc.returncode not in (0, 1):
        raise RuntimeError(
            f"pyflakes exited {proc.returncode}, so it did not run and this gate "
            f"measured nothing.\nstdout: {proc.stdout[-500:]}\n"
            f"stderr: {proc.stderr[-500:]}"
        )
    return [line for line in (proc.stdout + proc.stderr).splitlines() if line.strip()]


@pytest.mark.skipif(
    not _pyflakes_available(),
    reason="pyflakes not installed — see requirements-dev.txt",
)
def test_no_always_wrong_lint_findings() -> None:
    """No undefined names or equivalent hard defects anywhere in the package."""
    findings = _run_pyflakes()
    hard = [
        line
        for line in findings
        if any(marker in line.lower() for marker in _ALWAYS_A_BUG)
    ]
    assert not hard, (
        "static analysis found defects that are always bugs, never style:\n  "
        + "\n  ".join(hard)
        + "\n\nThe Cycle 4 Critical was exactly one of these: an 'undefined name "
        "log' in registry.py's escalation guard, which made the guard raise "
        "NameError on every invocation while reading as protection."
    )


@pytest.mark.skipif(not _pyflakes_available(), reason="pyflakes not installed")
def test_every_module_using_a_logger_defines_one() -> None:
    """The specific shape of the Cycle 4 Critical, checked directly.

    Belt and braces alongside the pyflakes gate: this one survives pyflakes being
    uninstalled, and it names the defect in terms a reader of this package will
    recognise.
    """
    offenders: list[str] = []
    scanned = 0
    for path in sorted(EXECUTORS_DIR.glob("*.py")):
        text = path.read_text(encoding="utf-8")
        scanned += 1
        uses_logger = "log." in text
        defines_logger = "log = logging.getLogger" in text
        if uses_logger and not defines_logger:
            offenders.append(path.name)

    assert scanned >= 8, (
        f"only scanned {scanned} module(s); too few for this verdict to mean "
        "anything (expected base, registry, escalation and six executors)"
    )
    assert not offenders, (
        f"module(s) call log.* without defining a module logger: {offenders}. "
        "Every such call raises NameError at runtime — and if the call sits in an "
        "exception handler, the handler is worse than absent: it reads as "
        "protection and fails for every cause."
    )


@pytest.mark.skipif(not _pyflakes_available(), reason="pyflakes not installed")
def test_pyflakes_actually_detects_a_planted_defect(tmp_path: Path) -> None:
    """Positive control: prove the analyser runs and reports.

    Without this, `test_no_always_wrong_lint_findings` cannot tell "the package is
    clean" from "pyflakes analysed nothing and returned an empty list" — the same
    can't-distinguish-silence-from-success shape as every vacuous guard found in
    this package. The hole is not hypothetical: `python -m pyflakes` exits 1 when
    the module is absent, and 1 is the code that means "found problems", so the
    rc-guard in `_run_pyflakes` permits it.

    Plants the exact defect that was Cycle 4's Critical.
    """
    planted = tmp_path / "planted.py"
    planted.write_text("def f():\n    log.error('no logger here')\n", encoding="utf-8")

    proc = subprocess.run(
        [sys.executable, "-m", "pyflakes", str(planted)],
        capture_output=True,
        text=True,
    )
    combined = proc.stdout + proc.stderr

    assert "undefined name" in combined.lower(), (
        "pyflakes did not report a planted `undefined name` — the analyser is not "
        f"working, so the gate above is measuring nothing.\nOutput: {combined!r}"
    )
    assert any(
        marker in combined.lower() for marker in _ALWAYS_A_BUG
    ), "the planted defect is reported but matches no marker in _ALWAYS_A_BUG"


def test_the_lint_gate_is_not_silently_skipped() -> None:
    """If pyflakes is missing, say so loudly rather than passing quietly.

    A skipped gate and a passing gate look the same in a summary line. This test
    always runs and records which state the suite is in.
    """
    if not _pyflakes_available():
        pytest.fail(
            "pyflakes is not installed, so the lint gate above is SKIPPED and the "
            "F821 class is unguarded. Install pyflakes==3.2.0. This test fails "
            "rather than skips because a skipped guard is indistinguishable from a "
            "passing one in a summary line — which is how the Cycle 4 Critical's "
            "sibling (an uncollected test in conftest.py) hid for a whole cycle."
        )
