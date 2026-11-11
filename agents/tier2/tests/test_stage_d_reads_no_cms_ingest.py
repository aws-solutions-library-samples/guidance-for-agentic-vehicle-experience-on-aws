"""AST scanner: stage-D AVX modules must not read cms_ingest, daily_usage,
or the fleet_health views (without _daily suffix).

G3F6.3 / G3F6F1.4 / G3F6C2-W4 hardened: uses re.finditer for every match
(catches duplicate occurrences), all checks are case-insensitive, scan scope
derived from ``git grep -l run_query -- agents/**/*.py`` (minus test files),
view patterns use word-boundary (catches bare constant forms such as
``_FAULT_VIEW = "vehicle_fault_status"``), and log-call arguments are skipped
by call site rather than by %s/%d content.

Evasion forms caught (verified against mutants in
/tmp/2026-10-05-fi-fleet-health-stage-d-f1c/ and cycle-2 W4):
  dupview   – bare view ref AFTER a _daily ref in the same string (finditer)
  fstrview  – f-string db variable ({fh_db}.vehicle_fault_status) → word-boundary
              pattern catches the literal fragment
  constusage– table-name constant (``_USAGE_TABLE = "daily_usage"``) →
              word-boundary match on the constant's value string
  upper     – ALL_CAPS reference (ADP_${stage}_CMS_INGEST.VIN_CMS_FLEET) →
              IGNORECASE
  othermod  – forbidden ref in fi_fleet_health/loop.py → git-grep scope
  viewconst – bare view-name constant (``_FAULT_VIEW = "vehicle_fault_status"``)
              → word-boundary (no leading dot required)
  scope     – forbidden ref in fi_recall_warranty/selection.py →
              git-grep scope includes that file

Docstrings and log-call arguments may name forbidden objects; the guard covers
query strings only (``_collect_sql_strings`` excludes all docstring nodes and
all string constants that are arguments of ``log.*``/``logger.*`` calls).
"""
from __future__ import annotations

import ast
import re
import subprocess
import textwrap
from pathlib import Path
from typing import Generator

import pytest

# ---------------------------------------------------------------------------
# Repo root
# ---------------------------------------------------------------------------

_REPO = Path(__file__).parents[3]

# ---------------------------------------------------------------------------
# Dynamic module discovery
# ---------------------------------------------------------------------------


def _iter_py_files() -> Generator[Path, None, None]:
    """Yield every production .py file under agents/ that references run_query.

    Primary: ``git grep -l run_query -- agents/**/*.py`` (fast, index-based).
    Fallback: walk agents/ and check file content, used for non-git trees such
    as ``git archive`` copies produced during mutation testing.
    """
    result = subprocess.run(
        ["git", "grep", "-l", "run_query", "--", "agents/**/*.py"],
        capture_output=True,
        text=True,
        cwd=str(_REPO),
    )
    if result.returncode == 0 and result.stdout.strip():
        for line in sorted(result.stdout.splitlines()):
            p = _REPO / line.strip()
            if (
                p.suffix == ".py"
                and not p.name.startswith("test_")
                and "tests" not in p.parts
                and "__pycache__" not in p.parts
            ):
                yield p
    else:
        # Fallback for non-git trees (git archive copies, CI sandboxes, etc.).
        agents = _REPO / "agents"
        for p in sorted(agents.rglob("*.py")):
            if (
                not p.name.startswith("test_")
                and "tests" not in p.parts
                and "__pycache__" not in p.parts
                and "run_query" in p.read_text(encoding="utf-8")
            ):
                yield p


#: Stage D's packages: every production module in them is scanned whether or not it calls
#: ``run_query`` itself (a SQL constant can live in a module that passes it on; review cycle 1
#: ``othermod`` put one in ``fi_fleet_health/loop.py``).
_STAGE_D_PACKAGES = ("fi_fleet_health", "fi_quality", "fi_recall_warranty")
_STAGE_D_EXTRA = ("tier2/runner.py", "tier2/selection.py", "tier2/snapshot_freshness.py")


def _iter_stage_d_package_files() -> Generator[Path, None, None]:
    agents = _REPO / "agents"
    for pkg in _STAGE_D_PACKAGES:
        for p in sorted((agents / pkg).rglob("*.py")):
            if "tests" not in p.parts and "__pycache__" not in p.parts and not p.name.startswith("test_"):
                yield p
    for rel in _STAGE_D_EXTRA:
        p = agents / rel
        if p.is_file():
            yield p


def _iter_scope() -> list[Path]:
    return sorted({*_iter_py_files(), *_iter_stage_d_package_files()})


def _iter_sql_files() -> Generator[Path, None, None]:
    """Yield every .sql file in the stage-D runtime SQL directories."""
    sql_roots = [
        _REPO / "agents" / "fi_recall_warranty" / "sql",
    ]
    for root in sql_roots:
        if root.is_dir():
            for p in sorted(root.glob("*.sql")):
                yield p


# ---------------------------------------------------------------------------
# AST extraction helpers (Python files)
# ---------------------------------------------------------------------------


_EVIDENCE_SOURCE_LABELS = frozenset({"vehicle_fault_status", "pm_vehicle_status"})


def _collect_sql_strings(source_path: Path) -> list[str]:
    """Return every non-docstring, non-log-call string constant and f-string
    text part in *source_path*.

    Docstrings (the first ``Expr`` child of a module / class / function body
    that is a plain ``Constant(str)``) are excluded.

    Log-call strings — any ``Constant`` that appears inside the argument list
    of a call whose callee is ``log.*``, ``logger.*``, or ``logging.*`` — are
    excluded by call site.  This is stricter than the old %s/%d heuristic:
    printf-style SQL assigned to a variable is now scanned, while log format
    strings continue to be skipped.

    f-string ``JoinedStr`` nodes are walked; each ``Constant`` leaf inside
    them contributes the literal portion (the template text, not the
    interpolated values, which are only known at runtime).
    """
    source = source_path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(source_path))

    docstring_nodes: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr):
                val = body[0].value
                if isinstance(val, ast.Constant) and isinstance(val.value, str):
                    docstring_nodes.add(id(val))

    # Mark every Constant that is an argument of log.*() / logger.*() / logging.*().
    log_arg_nodes: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if (
                isinstance(func, ast.Attribute)
                and isinstance(func.value, ast.Name)
                and func.value.id in ("log", "logger", "logging")
            ):
                for arg in node.args:
                    for sub in ast.walk(arg):
                        if isinstance(sub, ast.Constant):
                            log_arg_nodes.add(id(sub))

    # Evidence ``source`` labels: the contract-fixed logical source names CMS groups on
    # (``{"source": "vehicle_fault_status"}`` in fi_fleet_health/bands.py; G3F6.4). Exempt
    # only that exact shape: a dict value under the key "source" equal to one of the two labels.
    label_nodes: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            for k, v in zip(node.keys, node.values):
                if (
                    isinstance(k, ast.Constant) and k.value == "source"
                    and isinstance(v, ast.Constant)
                    and v.value in _EVIDENCE_SOURCE_LABELS
                ):
                    label_nodes.add(id(v))

    strings: list[str] = []

    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if (
                id(node) not in docstring_nodes
                and id(node) not in log_arg_nodes
                and id(node) not in label_nodes
            ):
                strings.append(node.value)
        elif isinstance(node, ast.JoinedStr):
            for part in node.values:
                if isinstance(part, ast.Constant) and isinstance(part.value, str):
                    if id(part) not in log_arg_nodes:
                        strings.append(part.value)

    return strings


# ---------------------------------------------------------------------------
# SQL file extraction (raw text, split by line)
# ---------------------------------------------------------------------------


def _collect_sql_file_strings(sql_path: Path) -> list[str]:
    """Return each non-comment line of a .sql file as a string to scan."""
    lines = []
    for line in sql_path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("--"):
            lines.append(line)
    return lines


# ---------------------------------------------------------------------------
# Forbidden patterns (compiled once, IGNORECASE throughout)
# ---------------------------------------------------------------------------

# Any occurrence of cms_ingest (database or bare name).
_RE_CMS_INGEST = re.compile(r"cms_ingest", re.IGNORECASE)

# cms_ingest table names — word-boundary match to avoid env-var false positives
# (e.g. CMS_DTC_HISTORY_TABLE would only match dtc_history if preceded by a
# non-word char, but CMS_DTC_ has no word boundary before dtc_history).
_RE_CMS_TABLES = {
    name: re.compile(r"\b" + name + r"\b", re.IGNORECASE)
    for name in (
        "vin_cms_fleet",
        "pm_plans",
        "pm_operator_completions",
        "dtc_history",
        "record_coverage",
    )
}

# daily_usage table — word-boundary so function names (select_daily_usage)
# don't trigger (they are identifiers, not string constants, but be safe).
_RE_DAILY_USAGE = re.compile(r"\bdaily_usage\b", re.IGNORECASE)

# Bare fleet_health views (without _daily suffix).
# Word-boundary prefix catches both qualified names
# (fleet_health.vehicle_fault_status, {fh_db}.vehicle_fault_status) and bare
# string constants ("vehicle_fault_status") — no leading dot required.
# Negative lookahead (?!_daily) ensures _daily variants pass.
_RE_BARE_FAULT_STATUS = re.compile(r"\bvehicle_fault_status(?!_daily)", re.IGNORECASE)
_RE_BARE_PM_STATUS = re.compile(r"\bpm_vehicle_status(?!_daily)", re.IGNORECASE)


def _find_violations(strings: list[str]) -> dict[str, list[str]]:
    """Return a dict of violation-type → list of offending string snippets."""
    violations: dict[str, list[str]] = {
        "cms_ingest": [],
        "daily_usage": [],
        "bare_fault_status": [],
        "bare_pm_status": [],
    }
    for name in _RE_CMS_TABLES:
        violations[f"cms_table:{name}"] = []

    for s in strings:
        # Every check uses finditer so multiple occurrences in one string all fire.
        if list(_RE_CMS_INGEST.finditer(s)):
            violations["cms_ingest"].append(s[:120])
        if list(_RE_DAILY_USAGE.finditer(s)):
            violations["daily_usage"].append(s[:120])
        if list(_RE_BARE_FAULT_STATUS.finditer(s)):
            violations["bare_fault_status"].append(s[:120])
        if list(_RE_BARE_PM_STATUS.finditer(s)):
            violations["bare_pm_status"].append(s[:120])
        for name, pat in _RE_CMS_TABLES.items():
            if list(pat.finditer(s)):
                violations[f"cms_table:{name}"].append(s[:120])

    return violations


# ---------------------------------------------------------------------------
# Combined file scan: one test per file (Python + SQL)
# ---------------------------------------------------------------------------

_ALL_PY_FILES: list[Path] = _iter_scope()
_ALL_SQL_FILES: list[Path] = list(_iter_sql_files())

assert _ALL_PY_FILES, "No Python files found in the scan scope; check _REPO path"


@pytest.mark.parametrize("py_path", _ALL_PY_FILES, ids=lambda p: p.name)
def test_no_forbidden_sql_refs_in_py(py_path: Path) -> None:
    """No SQL string in *py_path* may reference cms_ingest, daily_usage, or the
    bare fleet_health views (without _daily), or any cms_ingest table name."""
    strings = _collect_sql_strings(py_path)
    violations = _find_violations(strings)
    all_violations = {k: v for k, v in violations.items() if v}
    assert not all_violations, (
        f"{py_path.name}: forbidden SQL references found:\n"
        + "\n".join(
            f"  [{vtype}]:\n"
            + "\n".join(textwrap.indent(repr(v), "    ") for v in vlist)
            for vtype, vlist in all_violations.items()
        )
    )


@pytest.mark.parametrize("sql_path", _ALL_SQL_FILES, ids=lambda p: p.name)
def test_no_forbidden_sql_refs_in_sql(sql_path: Path) -> None:
    """No line in *sql_path* may reference cms_ingest, daily_usage, or bare views."""
    strings = _collect_sql_file_strings(sql_path)
    violations = _find_violations(strings)
    all_violations = {k: v for k, v in violations.items() if v}
    assert not all_violations, (
        f"{sql_path.name}: forbidden SQL references found:\n"
        + "\n".join(
            f"  [{vtype}]:\n"
            + "\n".join(textwrap.indent(repr(v), "    ") for v in vlist)
            for vtype, vlist in all_violations.items()
        )
    )


# ---------------------------------------------------------------------------
# Anti-vacuity: scan scope must include at minimum the 9 stage-D files
# ---------------------------------------------------------------------------

_REQUIRED_PY_NAMES = {
    "selection.py",          # fi_fleet_health + fi_recall_warranty (both reference run_query)
    "readers.py",            # fi_quality
    "adp_battery_source.py",
    "adp_brake_source.py",
    "adp_tire_source.py",
    "runner.py",             # fi_recall_warranty (in scope via git grep)
    "snapshot_freshness.py", # tier2 (in scope via git grep — candidate 7)
    "loop.py",               # fi_fleet_health (package scope; cycle 1 othermod)
    "job.py",                # fi_quality (package scope)
}


def test_scan_scope_covers_required_files() -> None:
    """The scan scope must include all stage-D run_query-calling files (anti-vacuity)."""
    found_names = {p.name for p in _ALL_PY_FILES}
    missing = _REQUIRED_PY_NAMES - found_names
    assert not missing, (
        f"These required stage-D files are missing from the scan scope: {missing}\n"
        f"Found: {sorted(found_names)}"
    )


@pytest.mark.parametrize("py_path", _ALL_PY_FILES, ids=lambda p: p.name)
def test_each_file_contains_at_least_one_string(py_path: Path) -> None:
    """Anti-vacuity: each scanned file must yield at least 1 non-docstring string.

    A file that produces 0 strings was likely unparseable or trivially empty;
    the scanner would give a false pass.  __init__.py files are exempt (they
    are frequently empty or contain only version strings).
    """
    if py_path.name == "__init__.py":
        pytest.skip("__init__.py files may legitimately be empty")
    strings = _collect_sql_strings(py_path)
    assert strings, (
        f"{py_path.name}: 0 non-docstring strings collected; "
        "scanner may be broken or file is trivially empty"
    )


# ---------------------------------------------------------------------------
# Regression: the new _daily tables must still appear in their owning files
# ---------------------------------------------------------------------------

# For battery/brake/tire: {recall_db} is interpolated; the literal part is
# .managed_vehicles — the dot-prefix confirms it's a table reference, not just
# a variable name.
_NEW_TABLE_CHECKS: list[tuple[Path, str]] = [
    (_REPO / "agents/fi_fleet_health/selection.py",     "fleet_recall.managed_vehicles"),
    (_REPO / "agents/fi_fleet_health/selection.py",     "vehicle_fault_status_daily"),
    (_REPO / "agents/fi_fleet_health/selection.py",     "pm_vehicle_status_daily"),
    (_REPO / "agents/fi_fleet_health/selection.py",     "vehicle_utilisation_daily"),
    (_REPO / "agents/fi_quality/readers.py",            "fleet_recall.managed_vehicles"),
    (_REPO / "agents/fi_quality/readers.py",            "vehicle_fault_status_daily"),
    (_REPO / "agents/health/adp_battery_source.py",     ".managed_vehicles"),
    (_REPO / "agents/health/adp_brake_source.py",       ".managed_vehicles"),
    (_REPO / "agents/health/adp_tire_source.py",        ".managed_vehicles"),
]


@pytest.mark.parametrize("module_path,required", _NEW_TABLE_CHECKS,
                         ids=lambda x: str(x) if isinstance(x, str) else "")
def test_new_table_appears_in_sql_strings(module_path: Path, required: str) -> None:
    """Each new ADP-written table must appear in its owning module's SQL strings."""
    strings = _collect_sql_strings(module_path)
    hits = [s for s in strings if required in s]
    assert hits, (
        f"{module_path.name}: required table/pattern '{required}' not found in any "
        "SQL string. Either the table was not referenced or the AST scanner missed it."
    )
