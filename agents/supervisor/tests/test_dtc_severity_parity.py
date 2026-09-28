"""The triage classifier must grade a fault at least as severely as the rest of the platform.

Regression guard for issues/2026-09-26-classifier-grades-fire-and-crash-codes-p2/. The
classifier (`lambdas/triage-classifier`) is the deterministic source of truth for P0-P3,
and it gives any code missing from `lambdas/shared/dtc_severity.yaml` the default
"moderate, safe to drive" (P2). `B0001_FIRE` and `U3000_CRASH` were missing, so a car
whose only fault was a fire or a crash classified P2: no P0 card, no roadside button,
no safety line. CVX's response catalog mapped both to "get out" and "call 911", and
CMS graded both P0, but nothing compared the three.

Every check here classifies a single active fault through the real `classify()`, so it
tests what the classifier does, not what the table says.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from agents.supervisor import dtc_response_catalog as cat

# The rules and their data live in the offline check the classifier build runs
# (`scripts/check-dtc-severity-parity.py`), so the build and these tests cannot drift.
_PATH = Path(__file__).resolve().parents[3] / "scripts" / "check-dtc-severity-parity.py"
_spec = importlib.util.spec_from_file_location("check_dtc_severity_parity", _PATH)
parity = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(parity)

_classifier, _ = parity.load_classifier()
_SRC = parity.SRC
_ORDER = parity.ORDER
level_for = parity.level_for
STOP_DRIVING_TEMPLATES = parity.STOP_DRIVING_TEMPLATES
CMS_CATALOG_GRADES = parity.CMS_CATALOG_GRADES
EXTRA_P0 = parity.EXTRA_P0
EXTRA_P1 = parity.EXTRA_P1


def test_the_classifier_reads_the_edited_table():
    """Anti-vacuity: the loaded table is the source file, and it has the new rows."""
    assert _classifier._SEVERITY_TABLE_PATH.resolve() == (_SRC.parent / "shared" / "dtc_severity.yaml").resolve()
    assert "B0001_FIRE" in _classifier._SEVERITY_TABLE


@pytest.mark.parametrize("code", ["B0001_FIRE", "U3000_CRASH"])
def test_fire_and_crash_are_p0(code):
    assert level_for(code) == "P0"


def test_the_stop_driving_set_is_every_template_but_one():
    assert STOP_DRIVING_TEMPLATES == set(cat.RESPONSE_TEMPLATES) - {"pullover_safe_location"}


@pytest.mark.parametrize(
    "code",
    sorted(c for c, m in cat.DTC_TO_TEMPLATE.items() if m.get("template") in STOP_DRIVING_TEMPLATES),
)
def test_a_stop_driving_directive_means_p0(code):
    assert level_for(code) == "P0", (
        f"{code} tells the driver {cat.DTC_TO_TEMPLATE[code]['template']!r} but classifies "
        f"{level_for(code)}; add it to lambdas/shared/dtc_severity.yaml as critical, not safe to drive"
    )


def test_the_stop_driving_scope_is_not_empty():
    codes = {c for c, m in cat.DTC_TO_TEMPLATE.items() if m.get("template") in STOP_DRIVING_TEMPLATES}
    assert {"B0001_FIRE", "U3000_CRASH", "C1234", "P0217"} <= codes


@pytest.mark.parametrize("code,grade", sorted(CMS_CATALOG_GRADES.items()))
def test_at_least_as_severe_as_the_cms_catalog(code, grade):
    got = level_for(code)
    assert _ORDER[got] <= _ORDER[grade], f"{code}: CMS grades {grade}, the classifier {got}"


@pytest.mark.parametrize("code", sorted(EXTRA_P0))
def test_codes_raised_to_p0_on_their_own_evidence(code):
    assert level_for(code) == "P0"


@pytest.mark.parametrize("code", sorted(EXTRA_P1))
def test_codes_raised_to_p1_on_the_producers_text(code):
    assert _ORDER[level_for(code)] <= _ORDER["P1"]


def test_an_unknown_code_still_gets_the_documented_default():
    """The P2 default is documented (VSA_TRIAGE_CLASSIFIER.md § 3); this fix adds rows,
    it does not change the default."""
    assert level_for("ZZZZ9") == "P2"


def test_the_offline_check_passes():
    assert parity.failures() == []


def test_the_build_runs_the_offline_check_before_bundling():
    build = (Path(__file__).resolve().parents[3] / "scripts" / "build-triage-classifier.sh").read_text()
    check = build.index("check-dtc-severity-parity.py")
    assert check < build.index('rm -rf "$OUT"'), (
        "the check must run before the old bundle is removed, or a failed check leaves an empty asset"
    )
    assert "set -euo pipefail" in build[:check], "a failing check must stop the build"


# ---------------------------------------------------------------------------
# The check must FAIL on a broken table, not just exist (review Cycle 2 Critical).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("code", [
    "B0001_FIRE",  # stop-driving template and CMS P0
    "P0520",       # EXTRA_P0 only
    "B1419",       # EXTRA_P1 only
    "C1001",       # CMS P1 only
])
def test_removing_a_row_fails_the_check(code, monkeypatch, capsys):
    monkeypatch.delitem(_classifier._SEVERITY_TABLE, code)
    found = parity.failures()
    assert any(line.startswith(f"{code}:") for line in found), found
    assert parity.main() == 1
    assert "FAIL:" in capsys.readouterr().out


def test_the_stop_driving_rule_stands_on_its_own(monkeypatch):
    """A stop-driving code no other rule names: only the stop-driving rule can catch it."""
    monkeypatch.setitem(cat.DTC_TO_TEMPLATE, "ZZ999", {"template": "exit_vehicle"})
    assert "ZZ999" not in parity.floors()
    assert any(line.startswith("ZZ999:") for line in parity.failures())


def test_a_clean_table_passes_main(capsys):
    assert parity.main() == 0
    assert capsys.readouterr().out.startswith("OK:")


def test_a_broken_table_fails_the_build_and_keeps_the_old_bundle(tmp_path):
    """Run the real build script on a copy whose table lacks `B0001_FIRE`. The check runs
    before anything is removed or downloaded, so this needs no network."""
    import shutil
    import subprocess
    import sys

    repo = Path(__file__).resolve().parents[3]
    root = tmp_path / "repo"
    for rel in ("scripts/build-triage-classifier.sh", "scripts/check-dtc-severity-parity.py",
                "agents/__init__.py", "agents/supervisor/__init__.py",
                "agents/supervisor/dtc_response_catalog.py"):
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(repo / rel, root / rel)
    shutil.copytree(repo / "lambdas" / "triage-classifier", root / "lambdas" / "triage-classifier",
                    ignore=shutil.ignore_patterns("tests", "__pycache__"))
    (root / "lambdas" / "shared").mkdir(parents=True)
    table = (repo / "lambdas" / "shared" / "dtc_severity.yaml").read_text()
    broken = "\n".join(l for l in table.splitlines() if not l.startswith("B0001_FIRE:")) + "\n"
    assert "\nB0001_FIRE:" in table and "\nB0001_FIRE:" not in broken
    (root / "lambdas" / "shared" / "dtc_severity.yaml").write_text(broken)
    # A wrapper, not a symlink: a symlinked venv interpreter loses its venv and runs on
    # the base Python, which may lack pyyaml (review Cycle 3 W1).
    (root / ".venv" / "bin").mkdir(parents=True)
    wrapper = root / ".venv" / "bin" / "python3"
    wrapper.write_text(f'#!/bin/sh\nexec "{sys.executable}" "$@"\n')
    wrapper.chmod(0o755)
    old = root / "infrastructure" / "build" / "triage-classifier"
    old.mkdir(parents=True)
    (old / "marker").write_text("previous bundle")

    r = subprocess.run(["bash", str(root / "scripts" / "build-triage-classifier.sh")],
                       capture_output=True, text=True, timeout=120)
    assert r.returncode != 0, r.stdout + r.stderr
    assert "FAIL:" in r.stdout and "B0001_FIRE" in r.stdout
    assert (old / "marker").read_text() == "previous bundle"


def test_overlapping_rules_take_the_more_severe_floor(monkeypatch):
    """Review Cycle 3 S: no two rules overlap today, so pin the rule directly."""
    monkeypatch.setitem(parity.CMS_CATALOG_GRADES, "B1419", "P0")  # CMS P0 beats EXTRA_P1's P1
    assert parity.floors()["B1419"] == "P0"
    monkeypatch.setitem(parity.CMS_CATALOG_GRADES, "P0520", "P2")  # EXTRA_P0 beats CMS P2
    assert parity.floors()["P0520"] == "P0"
