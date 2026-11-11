"""scripts/lib/fail-on-skipped-tests.py must fail on every kind of skip pytest reports.

The reports are produced by running real pytest in a subprocess, not hand-written, so
the test exercises the JUnit shapes pytest actually emits. The case that matters most
is the module-level skip: lambdas/triage-classifier/tests/test_warmup.py skips that way
when the bundle is not built, and that skip is what the CI gate exists to catch.

Run: python3 -m pytest scripts/tests/test_fail_on_skipped_tests.py -v
"""
from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[1] / "lib" / "fail-on-skipped-tests.py"
_spec = importlib.util.spec_from_file_location("fail_on_skipped_tests", _SCRIPT)
gate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gate)

_SOURCES = {
    "passing": "def test_ok():\n    assert True\n",
    "marked_skip": (
        "import pytest\n\n"
        "def test_ok():\n    assert True\n\n"
        "@pytest.mark.skip(reason='x')\n"
        "def test_skipped():\n    pass\n"
    ),
    "module_skip": (
        "import pytest\n"
        "pytest.skip('bundle not built', allow_module_level=True)\n\n"
        "def test_never_collected():\n    pass\n"
    ),
    "runtime_skip": (
        "import pytest\n\n"
        "def test_ok():\n    assert True\n\n"
        "def test_skips_inside():\n    pytest.skip('no fixture data')\n"
    ),
}


def _junit(tmp_path: Path, kind: str) -> Path:
    """Run real pytest on a one-file suite and return its JUnit report."""
    suite = tmp_path / kind
    suite.mkdir()
    (suite / "pytest.ini").write_text("[pytest]\n")
    (suite / f"test_{kind}.py").write_text(_SOURCES[kind])
    report = suite / "junit.xml"
    subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
         "-c", str(suite / "pytest.ini"), "--rootdir", str(suite),
         f"--junitxml={report}", str(suite)],
        cwd=suite, check=False, capture_output=True, text=True,
    )
    assert report.is_file(), f"pytest wrote no report for {kind}"
    return report


def test_a_clean_report_passes(tmp_path, capsys):
    assert gate.main([str(_junit(tmp_path, "passing"))]) == 0
    assert "none skipped" in capsys.readouterr().out


@pytest.mark.parametrize("kind", ["marked_skip", "module_skip", "runtime_skip"])
def test_every_kind_of_skip_fails(tmp_path, capsys, kind):
    assert gate.main([str(_junit(tmp_path, kind))]) == 1
    assert "skipped" in capsys.readouterr().err


def test_one_skip_among_several_reports_fails(tmp_path):
    clean = _junit(tmp_path, "passing")
    skipped = _junit(tmp_path, "module_skip")
    assert gate.main([str(clean), str(skipped)]) == 1


def test_a_missing_report_is_an_error(tmp_path):
    assert gate.main([str(tmp_path / "absent.xml")]) == 2


def test_no_arguments_is_an_error():
    assert gate.main([]) == 2


def test_a_report_with_no_cases_fails(tmp_path):
    empty = tmp_path / "empty.xml"
    empty.write_text('<testsuites><testsuite name="pytest" tests="0"/></testsuites>')
    assert gate.main([str(empty)]) == 1
