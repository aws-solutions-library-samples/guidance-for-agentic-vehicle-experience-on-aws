"""Drift test for the generated agent inventory.

Spec: .kiro/specs/2026-08-03-cvx-tier2-doctrine-and-inventory/spec.md
      § "Drift test" (7 tests) + § "Generator" (interface contract)

These tests pin the exact public interface of ``scripts/generate_agent_inventory.py``
so S1 and later specs cannot silently drift from the contract.  The generator does
not exist yet — all 7 tests fail with ImportError / ModuleNotFoundError on
``generate_agent_inventory`` until T2.1 ships it.

Design note (spec D6):
  ``pytest.ini`` sets ``testpaths = agents lambdas``.  The generator lives under
  ``scripts/``, which is outside that collection root.  This file imports it via an
  explicit ``sys.path.insert`` so the test is collected by default (it is under
  ``agents/supervisor/tests/``) while the generator remains in ``scripts/``.

Import discipline:
  The import of ``generate_agent_inventory`` is NOT wrapped in try/except and NOT
  passed to pytest.importorskip.  Instead it lives inside a session-scoped fixture
  so that:
  (a) pytest CAN collect all 7 test functions (they are defined regardless of whether
      the generator exists), AND
  (b) every test ERRORS with ModuleNotFoundError when the generator is absent —
      proving the tests are wired to the real target and not vacuously passing.
  Once T2.1 ships the generator, all 7 tests go green without any change here.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

# ---------------------------------------------------------------------------
# Resolve paths — safe to compute at import time (no generator needed)
# ---------------------------------------------------------------------------

# ``agents/supervisor/tests/<this>.py`` → repo root
REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPTS_DIR = REPO_ROOT / "scripts"
README_PATH = REPO_ROOT / "README.md"
DOCS_DIR = REPO_ROOT / "docs"
INVENTORY_MD = DOCS_DIR / "AGENT-INVENTORY.md"
INVENTORY_ADOC = DOCS_DIR / "AGENT-INVENTORY.adoc"

# ---------------------------------------------------------------------------
# Optional docs-package path (spec D7 / test_ig_snapshot_in_sync)
# ---------------------------------------------------------------------------

def _discover_cvx_docs() -> Path | None:
    """Resolve the CVX docs package via env var or sibling-path fallback.

    Mirrors the auto-discovery pattern of
    ``scripts/tests/test_seed_scripts_no_fake_connected.py``.
    Returns None when the package is absent (skip-tolerant per D7).
    """
    env = os.environ.get("CVX_DOCS_PATH")
    if env:
        p = Path(env).expanduser().resolve()
        return p if p.is_dir() else None
    # Sibling fallback: <repo-root>/../CVXDocsPackageCreate/src/...
    candidate = (
        REPO_ROOT.parent
        / "CVXDocsPackageCreate"
        / "src"
        / "AWSGuidanceforConnectedVehicleExperienceDocs"
        / "latest"
        / "implementationguide"
    )
    return candidate if candidate.is_dir() else None


CVX_DOCS_DIR = _discover_cvx_docs()

# ---------------------------------------------------------------------------
# Session fixture — imports the generator.
#
# Lives in a fixture (not at module level) so pytest can collect all 7 test
# functions regardless of whether the generator exists yet.  The import is NOT
# wrapped in try/except: when the module is absent, the ImportError propagates
# and each test that depends on this fixture is marked ERROR — the RED phase.
# Once T2.1 ships ``scripts/generate_agent_inventory.py``, the fixture resolves
# and all 7 tests go GREEN without any change here.
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def gen() -> ModuleType:
    """Return the ``generate_agent_inventory`` module.

    Hard-fails with ModuleNotFoundError when the generator has not been written yet.
    No try/except, no importorskip — intentional RED-phase behaviour.
    """
    sys.path.insert(0, str(SCRIPTS_DIR))
    import generate_agent_inventory as _gen  # noqa: E402
    return _gen


# ---------------------------------------------------------------------------
# Tests — all synchronous (no pytest-asyncio dependency)
# ---------------------------------------------------------------------------


def test_artifacts_match_registry(gen):
    """On-disk artifacts must match a fresh in-memory generation.

    Spec: ``test_artifacts_match_registry`` — both artifacts byte-match
    a fresh generation without modifying any tracked file.

    This test previously ran the generator as a subprocess, which rewrote
    ``README.md`` and ``docs/AGENT-INVENTORY.md`` on disk as a side effect.
    Because it was defined first, every subsequent test — including
    ``test_readme_region_matches`` whose job is to detect stale content —
    read files that this test had already healed, masking staleness.

    Fix (F1.3, review C3): generate entirely in memory via the module's own
    render functions, then compare against disk. No file is written. A stale
    artifact is now visible to ``test_readme_region_matches`` when run alone.

    Separately, ``--check`` mode (CLI contract test) is verified below in
    ``test_check_flag_exits_nonzero_on_drift``.
    """
    inv = gen.build_inventory(REPO_ROOT)

    # --- AGENT-INVENTORY.md ---
    expected_md = gen.render_markdown(inv)
    assert INVENTORY_MD.exists(), f"AGENT-INVENTORY.md not found at {INVENTORY_MD}"
    actual_md = INVENTORY_MD.read_text(encoding="utf-8")
    assert actual_md == expected_md, (
        "docs/AGENT-INVENTORY.md is stale. "
        "Run `python3 scripts/generate_agent_inventory.py` to regenerate."
    )

    # --- AGENT-INVENTORY.adoc ---
    expected_adoc = gen.render_asciidoc(inv)
    assert INVENTORY_ADOC.exists(), f"AGENT-INVENTORY.adoc not found at {INVENTORY_ADOC}"
    actual_adoc = INVENTORY_ADOC.read_text(encoding="utf-8")
    assert actual_adoc == expected_adoc, (
        "docs/AGENT-INVENTORY.adoc is stale. "
        "Run `python3 scripts/generate_agent_inventory.py` to regenerate."
    )

    # --- README.md region ---
    assert README_PATH.exists(), f"README.md not found at {README_PATH}"
    readme_text = README_PATH.read_text(encoding="utf-8")
    if gen.README_BEGIN_MARKER in readme_text and gen.README_END_MARKER in readme_text:
        expected_region = gen.render_readme_region(inv)
        start = readme_text.index(gen.README_BEGIN_MARKER) + len(gen.README_BEGIN_MARKER)
        end = readme_text.index(gen.README_END_MARKER)
        current_region = readme_text[start:end]
        assert current_region == expected_region, (
            "README.md fenced region is stale. "
            "Run `python3 scripts/generate_agent_inventory.py` to regenerate."
        )


def _scratch_repo(tmp_path: Path) -> Path:
    """A throwaway copy of exactly what the generator CLI reads.

    The CLI resolves its repo root from ``Path(__file__).parent.parent``, so
    running a COPY of the script inside this tree makes it read and diff the
    scratch files — no tracked file is touched, and no module API is
    substituted for the real entry point.
    """
    import shutil

    scratch = tmp_path / "repo"
    (scratch / "scripts").mkdir(parents=True)
    shutil.copy2(SCRIPTS_DIR / "generate_agent_inventory.py", scratch / "scripts")
    shutil.copytree(
        REPO_ROOT / "agents" / "supervisor",
        scratch / "agents" / "supervisor",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "tests"),
    )
    (scratch / "docs").mkdir()
    for name in ("AGENT-INVENTORY.md", "AGENT-INVENTORY.adoc"):
        shutil.copy2(DOCS_DIR / name, scratch / "docs" / name)
    shutil.copy2(README_PATH, scratch / "README.md")
    return scratch


def _run_check(root: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(root / "scripts" / "generate_agent_inventory.py"), "--check"],
        capture_output=True, text=True, cwd=root, timeout=120,
    )


def test_check_flag_exits_zero_when_fresh(tmp_path):
    """``--check`` on a faithful copy exits 0 — the positive control, so the
    drift test below cannot pass merely because the scratch copy is broken.
    ``make agent-inventory-check`` (Makefile) shells this exact flag."""
    result = _run_check(_scratch_repo(tmp_path))
    assert result.returncode == 0, (
        f"--check exited {result.returncode} on an unmodified copy.\n"
        f"stdout:\n{result.stdout[:2000]}\nstderr:\n{result.stderr[:2000]}"
    )


def test_check_flag_exits_nonzero_on_drift(tmp_path):
    """``--check`` must exit 1 and print a diff when an artifact is stale.

    Pins the CLI contract end to end: the real script, the real argparse flag,
    the real exit code. Review Cycle 2 C2W3: the previous version called
    ``_check_artifact`` directly and could ``pytest.skip`` — it asserted no exit
    code at all while claiming to. Mutation: make the ``--check`` branch
    ``sys.exit(0)`` unconditionally → FAILS.
    """
    scratch = _scratch_repo(tmp_path)
    (scratch / "docs" / "AGENT-INVENTORY.md").write_text("<!-- stale -->\n# STALE\n", encoding="utf-8")
    result = _run_check(scratch)
    assert result.returncode == 1, (
        f"--check exited {result.returncode} on a stale artifact; expected 1.\n"
        f"stderr:\n{result.stderr[:2000]}"
    )
    assert "AGENT-INVENTORY.md" in result.stdout, "expected a diff naming the stale file"


def test_no_orphan_exports(gen):
    """Every factory in ``__all__`` must appear in ≥1 of the 7 surfaces.

    Spec classification: orphan export → hard error.
    Pins interface: ``gen.build_inventory(repo_root)`` returns an object with
    ``.exported`` (list of factory names) and ``.surfaces`` (dict of surface
    name → list of factory names).
    """
    inv = gen.build_inventory(REPO_ROOT)

    # Collect all factories appearing in any surface.
    surfaced: set[str] = set()
    for _surface_name, factories in inv.surfaces.items():
        surfaced.update(factories)

    orphans = [f for f in inv.exported if f not in surfaced]
    assert not orphans, (
        f"Orphan exports detected (in __all__ but in no surface): {orphans}\n"
        "Each exported factory must appear in at least one of the 7 tool surfaces."
    )


def test_no_unregistered_factories(gen):
    """Every ``make_*_tool`` defined under ``tools/`` must be exported+registered
    OR appear in ``gen.INTERNAL_HELPER_FACTORIES`` with a rationale string.

    Spec classification: unregistered factory → hard error.
    Pins interface: ``gen.INTERNAL_HELPER_FACTORIES`` is a ``dict[str, str]``
    mapping factory name → rationale; ``inv.all_defined`` is the full set of
    defined ``make_*_tool`` names.
    """
    inv = gen.build_inventory(REPO_ROOT)

    unregistered = [
        f for f in inv.all_defined
        if f not in inv.exported and f not in gen.INTERNAL_HELPER_FACTORIES
    ]
    assert not unregistered, (
        f"Unregistered factories detected (defined but not exported and not "
        f"allowlisted): {unregistered}\n"
        "Either add to __all__ and a surface, or add to INTERNAL_HELPER_FACTORIES "
        "with a rationale string."
    )


def test_every_registered_tool_documented(gen):
    """Each registered factory must yield ≥1 ``@tool``-decorated function whose
    docstring first line is non-empty.

    Spec classification: undocumented tool → hard error.
    Pins interface: ``inv.tools_by_factory`` is a ``dict[str, list[tuple[str, str]]]``
    mapping factory name → list of (tool_name, description) pairs.
    """
    inv = gen.build_inventory(REPO_ROOT)

    undocumented: list[str] = []
    for factory_name in inv.exported:
        tools = inv.tools_by_factory.get(factory_name, [])
        if not tools:
            undocumented.append(f"{factory_name}: no @tool function found")
            continue
        for tool_name, description in tools:
            if not description or not description.strip():
                undocumented.append(
                    f"{factory_name}/{tool_name}: empty docstring first line"
                )

    assert not undocumented, (
        f"Undocumented @tool functions detected:\n  "
        + "\n  ".join(undocumented)
        + "\nEvery registered factory's @tool function must have a non-empty "
        "docstring first line."
    )


def test_readme_region_matches(gen):
    """The README.md fenced region must match generated content, and both
    markers must exist.

    Spec: ``README_BEGIN_MARKER`` / ``README_END_MARKER`` round-trip; absence of
    either marker is a hard error (so the region cannot be deleted and silently
    stop being checked).
    Pins interface: ``gen.README_BEGIN_MARKER``, ``gen.README_END_MARKER`` (strings);
    ``gen.render_readme_region(inv)`` returns the expected region body string.
    """
    assert README_PATH.exists(), f"README.md not found at {README_PATH}"
    readme_text = README_PATH.read_text(encoding="utf-8")

    assert gen.README_BEGIN_MARKER in readme_text, (
        f"Missing begin marker in README.md: {gen.README_BEGIN_MARKER!r}\n"
        "Add the marker pair to README.md and run "
        "`python3 scripts/generate_agent_inventory.py`."
    )
    assert gen.README_END_MARKER in readme_text, (
        f"Missing end marker in README.md: {gen.README_END_MARKER!r}\n"
        "Add the marker pair to README.md and run "
        "`python3 scripts/generate_agent_inventory.py`."
    )

    inv = gen.build_inventory(REPO_ROOT)
    expected_region = gen.render_readme_region(inv)

    # Extract the current region content from README.
    start_idx = readme_text.index(gen.README_BEGIN_MARKER) + len(gen.README_BEGIN_MARKER)
    end_idx = readme_text.index(gen.README_END_MARKER)
    current_region = readme_text[start_idx:end_idx]

    assert current_region == expected_region, (
        "README.md fenced region is stale.\n"
        "Run `python3 scripts/generate_agent_inventory.py` to regenerate."
    )


def test_generation_is_deterministic(gen):
    """Two consecutive generations must produce byte-identical artifacts.

    Spec: ``test_generation_is_deterministic`` — persona tool order is
    load-bearing (``tools/__init__.py:76`` — *"Order is intentional… Do NOT
    alphabetise"*), so the generator must preserve declaration order.
    Pins interface: ``gen.build_inventory``, ``gen.render_markdown``,
    ``gen.render_asciidoc``, ``gen.render_readme_region``.
    """
    inv1 = gen.build_inventory(REPO_ROOT)
    inv2 = gen.build_inventory(REPO_ROOT)

    md1 = gen.render_markdown(inv1)
    md2 = gen.render_markdown(inv2)
    assert md1 == md2, (
        "render_markdown is not deterministic: two consecutive calls produced "
        "different output.  Persona tool order must be preserved (no sorting)."
    )

    adoc1 = gen.render_asciidoc(inv1)
    adoc2 = gen.render_asciidoc(inv2)
    assert adoc1 == adoc2, (
        "render_asciidoc is not deterministic: two consecutive calls produced "
        "different output."
    )

    region1 = gen.render_readme_region(inv1)
    region2 = gen.render_readme_region(inv2)
    assert region1 == region2, (
        "render_readme_region is not deterministic: two consecutive calls produced "
        "different output."
    )


def test_ig_snapshot_in_sync(gen):
    """The CVX docs-package .adoc snapshot must match the CVX canonical artifact.

    Skips loudly (naming ``CVX_DOCS_PATH`` env var) when the package is absent
    (D7 — the CVX public-mirror clone will not have the docs package).

    Spec: ``test_ig_snapshot_in_sync`` — skip-tolerant per spec D7, mirroring
    the ``CMS_REPO_PATH`` pattern in
    ``scripts/tests/test_seed_scripts_no_fake_connected.py``.
    Pins interface: ``gen.build_inventory``, ``gen.render_asciidoc``.
    """
    if CVX_DOCS_DIR is None:
        pytest.skip(
            "CVX docs package not found.  Set CVX_DOCS_PATH to the "
            "implementationguide directory, or check out "
            "CVXDocsPackageCreate at "
            "../CVXDocsPackageCreate/src/"
            "AWSGuidanceforConnectedVehicleExperienceDocs/latest/implementationguide"
        )

    # The canonical artifact lives in the CVX repo.
    assert INVENTORY_ADOC.exists(), (
        f"CVX canonical artifact not found: {INVENTORY_ADOC}\n"
        "Run `python3 scripts/generate_agent_inventory.py` first."
    )

    canonical = INVENTORY_ADOC.read_text(encoding="utf-8")

    # The docs-package snapshot — look for AGENT-INVENTORY.adoc in the IG dir.
    snapshot_path = CVX_DOCS_DIR / "AGENT-INVENTORY.adoc"
    assert snapshot_path.exists(), (
        f"IG snapshot not found at {snapshot_path}.\n"
        "Copy docs/AGENT-INVENTORY.adoc to the docs package (T3.2)."
    )

    snapshot = snapshot_path.read_text(encoding="utf-8")
    assert canonical == snapshot, (
        "CVX docs-package AGENT-INVENTORY.adoc is out of sync with the "
        f"canonical CVX artifact at {INVENTORY_ADOC}.\n"
        "Re-run `python3 scripts/generate_agent_inventory.py` and copy the "
        "result to the docs package."
    )


def test_surface_factories_are_all_exported(gen):
    """Every factory referenced in any surface must appear in ``__all__``.

    Spec T3.0 symmetry check: the generator must hard-error when a persona list
    or kiosk allow-list references a factory that is absent from ``__all__``.
    Without this check the inventory silently undercounts — the summary table
    iterates ``inv.exported``, so a surfaced-but-not-exported factory is
    invisible to every downstream consumer of the inventory.

    Pins interface: ``gen.build_inventory`` validates this and exits non-zero
    if the invariant is violated.
    """
    inv = gen.build_inventory(REPO_ROOT)

    exported_set = set(inv.exported)
    violations: list[str] = []
    for surface_name, factory_list in inv.surfaces.items():
        for factory_name in factory_list:
            if (
                factory_name not in exported_set
                and factory_name not in gen.INTERNAL_HELPER_FACTORIES
            ):
                violations.append(f"{factory_name} (surface: {surface_name})")

    assert not violations, (
        "Surface factories not in __all__ detected:\n  "
        + "\n  ".join(violations)
        + "\nEvery factory referenced in a surface must appear in __all__ or "
        "INTERNAL_HELPER_FACTORIES so the inventory does not silently undercount."
    )


def test_surface_factories_are_all_exported__negative(gen, tmp_path):
    """Negative test: the symmetry validator actually fires, and only it.

    Constructs a synthetic repo fixture where a factory name (``make_ghost_tool``)
    is referenced in the ``fleet_driver`` persona list but:
    (a) is absent from ``__all__``, AND
    (b) is not defined anywhere under ``tools/`` (no corresponding ``.py`` file).

    Because (b) holds, ``all_defined`` will never contain ``make_ghost_tool``, so
    the **unregistered-factory validator** — which iterates ``all_defined`` — will
    NOT fire.  Only the **symmetry validator** (which iterates the surface lists
    and checks against ``__all__``) can detect the violation.

    This means: if the symmetry validator is deleted from ``build_inventory``, the
    test fails (SystemExit is never raised).  That isolation is the property T3.6
    requires.

    Spec T3.0 / T3.6: "construct the violation against a synthetic fixture …
    rather than mutating the real repo files."  Does NOT touch any file under
    ``agents/supervisor/tools/``, ``supervisor.py``, or ``pytest.ini`` — all
    writes are to ``tmp_path``.
    """
    import shutil

    # ------------------------------------------------------------------
    # Build a minimal synthetic repo under tmp_path.
    # ------------------------------------------------------------------
    tools_dir = tmp_path / "agents" / "supervisor" / "tools"
    tools_dir.mkdir(parents=True)

    # Copy all real tool module files (excl. __init__.py) unchanged.
    # This gives the unregistered-factory validator a realistic ``all_defined``
    # set — but ``make_ghost_tool`` will not appear in it, because it has no
    # corresponding .py file.
    real_tools_dir = REPO_ROOT / "agents" / "supervisor" / "tools"
    for src_file in real_tools_dir.glob("*.py"):
        if src_file.name != "__init__.py":
            shutil.copy2(src_file, tools_dir / src_file.name)

    # Synthesise an __init__.py that:
    #   • keeps __all__ exactly as in the real repo (no changes), AND
    #   • adds ``make_ghost_tool`` to the fleet_driver persona list only.
    #
    # ``make_ghost_tool`` is referenced as a bare name (matching the ast.Name
    # parse path in _parse_persona_tool_factories).  It is absent from __all__
    # and absent from every .py file under tools/ — so:
    #   – the orphan-export validator is silent (make_ghost_tool is not in __all__)
    #   – the unregistered-factory validator is silent (make_ghost_tool is not in
    #     all_defined, because no .py file defines it)
    #   – only the symmetry validator can detect it (surface reference without a
    #     corresponding __all__ entry).
    real_init = (real_tools_dir / "__init__.py").read_text(encoding="utf-8")

    # Inject the ghost factory as the first entry in fleet_driver's list.
    # The real list starts with "make_triage_tool," — insert before it.
    patched_init = real_init.replace(
        "    \"fleet_driver\": [\n        make_triage_tool,\n",
        "    \"fleet_driver\": [\n        make_ghost_tool,\n        make_triage_tool,\n",
    )
    assert "make_ghost_tool" in patched_init, (
        "Patch did not inject make_ghost_tool into fleet_driver list — "
        "the real __init__.py structure may have changed."
    )
    (tools_dir / "__init__.py").write_text(patched_init, encoding="utf-8")

    # Copy supervisor.py unchanged — kiosk surface references only real factories.
    real_supervisor = REPO_ROOT / "agents" / "supervisor" / "supervisor.py"
    supervisor_dst = tmp_path / "agents" / "supervisor" / "supervisor.py"
    shutil.copy2(real_supervisor, supervisor_dst)

    # ------------------------------------------------------------------
    # Invoke build_inventory against the synthetic root.
    # The symmetry validator must fire (make_ghost_tool in fleet_driver but not
    # in __all__).  The unregistered-factory validator must NOT fire (make_ghost_tool
    # is never in all_defined).  We can't observe which validator fired from outside,
    # but the fixture design guarantees only one can.
    # ------------------------------------------------------------------
    with pytest.raises(SystemExit) as exc_info:
        gen.build_inventory(tmp_path)

    assert exc_info.value.code != 0, (
        "build_inventory should have exited non-zero when a surface factory "
        "(make_ghost_tool) is absent from __all__, but it exited 0."
    )



# ---------------------------------------------------------------------------
# One factory returns exactly one tool
#
# Added 2026-09-23 by spec `2026-09-23-cvx-escalation-recommend-not-act` after a
# regression that 841 passing tests could not see.
#
# `make_escalate_tool` was changed to return a 2-tuple
# `(escalate_to_human, escalate_for_service)`. Two things broke silently:
#
#   1. `supervisor.py:381-384` registers tools as a FLAT list —
#      `[f(ctx, config) for f in factories]` — so the tuple landed where
#      Strands expects a tool. The surrounding try/except ("never fail a
#      session because a persona is misconfigured", :385) swallowed the error
#      and fell back to the legacy path, so escalation silently left the
#      persona-aware tool set.
#   2. `generate_agent_inventory.py` counts factory names in `__all__` and
#      renders them as "N registered tools" (:417, :493). One factory
#      returning two tools made the prose say 39 while the table listed 40.
#      Honest agent/tool counts are an explicit portfolio constraint.
#
# Fixed by splitting into `make_escalate_tool` + `make_service_escalate_tool`,
# restoring the 1:1 ratio every other module in the package already held
# (`dms_copilot.py` 3:3, `dms_warranty.py` 2:2).
#
# This guard exists because neither failure mode was visible: the registration
# error was swallowed, and the count drift only showed up in a docs-snapshot
# comparison that skips when the docs package is absent.
# ---------------------------------------------------------------------------


def _tool_factory_names(gen) -> list[str]:
    """Exported factory names, excluding known internal composition helpers."""
    inv = gen.build_inventory(REPO_ROOT)
    return list(inv.exported)


def test_every_factory_returns_exactly_one_tool(gen):
    """A factory returning a tuple/list breaks registration AND the tool count.

    Calls every exported factory with a stub session + config and asserts the
    return is a single tool object, not a container.
    """
    import importlib
    from unittest.mock import MagicMock

    from agents.supervisor.session import SessionContext

    ctx = SessionContext(
        tenant_id="t",
        session_id="s" * 33,
        jwt="not-a-real-jwt",
        persona_id="fleet_driver",
        vin="1FMCU0F70MUA12345",
    )
    config = MagicMock()
    config.api_base = "http://localhost"

    tools_pkg = importlib.import_module("agents.supervisor.tools")

    offenders: list[str] = []
    checked = 0
    for name in _tool_factory_names(gen):
        factory = getattr(tools_pkg, name, None)
        if factory is None or not callable(factory):
            continue
        try:
            # Factories are inconsistent in arity — mirror the defensive
            # dispatch in supervisor.py:381 rather than inventing a convention.
            takes_config = "config" in getattr(factory, "__code__").co_varnames
            result = factory(ctx, config) if takes_config else factory(ctx)
        except Exception:
            # A factory that raises on a stub context is out of scope here —
            # this guard is about the SHAPE of a successful return.
            continue
        checked += 1
        if isinstance(result, (tuple, list, set, dict)):
            offenders.append(
                f"{name} returned {type(result).__name__} "
                f"(len {len(result)}) — expected a single tool"
            )

    assert not offenders, (
        "Every tool factory must return exactly ONE tool. A container breaks "
        "registration silently (supervisor.py:381-385 swallows it) and makes "
        "the inventory's tool count understate the table:\n  "
        + "\n  ".join(offenders)
        + "\n\nSplit the factory instead, as make_escalate_tool /"
        " make_service_escalate_tool do."
    )

    # Anti-vacuity: if the loop checked nothing, every assertion above is free.
    assert checked >= 10, (
        f"Only {checked} factories were actually invoked — the guard is close "
        "to vacuous. Either the stub context stopped satisfying most factories "
        "or the factory discovery broke. Investigate rather than lowering this "
        "floor."
    )


def test_factory_count_matches_documented_tool_count(gen):
    """The prose count and the rendered table must agree.

    `len(inv.exported)` counts FACTORIES; the table renders one row per TOOL.
    Those are the same number only while the 1:1 ratio above holds, so this
    asserts the coupling directly rather than trusting it.
    """
    inv = gen.build_inventory(REPO_ROOT)
    adoc = gen.render_asciidoc(inv)

    table_rows = [
        ln for ln in adoc.splitlines() if ln.startswith("| `")
    ]
    assert len(table_rows) == len(inv.exported), (
        f"Documented tool table has {len(table_rows)} rows but "
        f"{len(inv.exported)} factories are exported. The prose count is "
        f"rendered from len(inv.exported), so these diverging means the "
        f"rendered count is wrong. Most likely a factory returns more than "
        f"one tool — see test_every_factory_returns_exactly_one_tool."
    )
