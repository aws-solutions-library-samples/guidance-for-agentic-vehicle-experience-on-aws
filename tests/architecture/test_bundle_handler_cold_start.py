"""Every bundled AVX handler imports from its bundle alone (Group 2 review, core C1).

`agents/executors/policy.py` reads `agents/tier2/schemas/action.schema.json` at import,
and both the findings and the actions handlers import it at module scope. Before the
fix, `scripts/build-avx-lambda.sh` shipped no schema to the findings bundle and no
`agents/` at all to the actions bundle, so every route on both Lambdas, iOS routes
included, would have failed at cold start.

Fix Group 3a (review-c8-core.md W1 + cold-start Suggestion):
  api-avx-approval-rules now gets bundle_lambda(use_lib=1) + bundle_policy (it imports
  _lib.avx_scope at module scope and agents.executors.policy lazily in
  _is_auto_approvable). avx-auto-approver gets bundle_lambda(use_lib=0) +
  bundle_executors + bundle_run_records (both agents.executors.registry and
  agents.tier2.run_records are lazy — import handler alone passes with the packages
  absent, so the cold-start test must import them explicitly).

These tests run the REAL build script into a temp directory (`AVX_BUILD_BASE`, with
`AVX_BUILD_SKIP_PIP=1` so no network is needed; the venv provides boto3/requests) and
import each handler — plus every module it imports lazily — in a subprocess with only
its bundle on `sys.path`. The negative cases run a copy of the script with one staging
step removed; `scripts/lib/` is symlinked into the fake root so that `check-bundle-
abi.py` (invoked by `bundle_lambda`) is reachable and the ABI check no longer causes
the script to exit before reaching the staging step under test.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
_SCRIPT = _REPO / "scripts" / "build-avx-lambda.sh"

# Map of bundle-name → modules to import to exercise all cold-start paths.
#
# Lazily imported modules (inside functions annotated # noqa: PLC0415) must be listed
# explicitly here. `import handler` alone passes even when those modules are absent from
# the bundle, so a test that only imports `handler` fails to catch a missing package and
# reports false coverage.
#
# api-avx-findings: handler imports nothing from agents at module scope; the approve
#   route lazily imports agents.executors.registry (which transitively imports policy).
# api-avx-actions: handler imports ACTION_KINDS from agents.executors.policy at module
#   scope — `import handler` catches missing policy.py directly. Listed explicitly for
#   clarity and symmetry.
# api-avx-approval-rules: handler imports _lib.avx_scope at module scope and
#   agents.executors.policy lazily in _is_auto_approvable.
# avx-auto-approver: handler has no agent imports at module scope; all three relevant
#   modules are lazy (registry at line ~213, policy at ~214, run_records at ~368).
_HANDLER_LAZY_IMPORTS: dict[str, tuple[str, ...]] = {
    "api-avx-findings": (
        "handler",
        "agents.executors.registry",  # approve route (lazy, dispatch path)
        "agents.executors.policy",    # policy.py reads action.schema.json at import
        "agents.executors.target_reason",  # imported by cms.py/dms.py; imports base (§ 10b)
        "agents.fi_quality.issue",    # lazily imported by _make_adp_hook._hook in registry.py
        "agents.tier2.writer",        # imported at module scope by agents.fi_quality.issue
    ),
    "api-avx-investigations": (
        "handler",                    # module-scope: _lib.avx_scope, agents.fi_quality.write
        "agents.fi_quality.write",    # module-scope: from agents.tier2.writer import compute_finding_key
        "agents.tier2.writer",        # imported at module scope by agents.fi_quality.write
    ),
    "api-avx-actions": (
        "handler",                    # module-scope `from agents.executors.policy import`
        "agents.executors.policy",    # explicit for symmetry with other handlers
        "agents.executors.failure_reason",  # module-scope (F1.1 C1: bundle_policy must stage it)
    ),
    "api-avx-approval-rules": (
        "handler",                    # module-scope `from _lib.avx_scope import`
        "agents.executors.policy",    # lazy inside _is_auto_approvable
    ),
    "avx-auto-approver": (
        "handler",
        "agents.executors.registry",  # lazy inside _process_record (line ~213)
        "agents.executors.policy",    # lazy inside _process_record (line ~214)
        "agents.executors.target_reason",  # imported by cms.py/dms.py; imports base (§ 10b)
        "agents.tier2.run_records",   # lazy inside _write_run_record (line ~368)
    ),
}

_HANDLERS = tuple(_HANDLER_LAZY_IMPORTS)


def _run_script(script: Path, build_base: Path) -> None:
    proc = subprocess.run(["bash", str(script)], capture_output=True, text=True, timeout=120,
                          env={**os.environ, "AVX_BUILD_BASE": str(build_base), "AVX_BUILD_SKIP_PIP": "1"})
    assert proc.returncode == 0, proc.stdout[-2000:] + proc.stderr[-2000:]


def _import_handler(bundle_dir: Path,
                    modules: tuple[str, ...] = ("handler",)) -> subprocess.CompletedProcess:
    """Import handler.py (and any extra modules) with the bundle first on sys.path and
    the checkout unreachable (`''` resolves to cwd = the repo root under pytest, so it
    is removed)."""
    stmts = "; ".join(f"import {m}" for m in modules)
    code = ("import sys; sys.path = [p for p in sys.path if p != ''];"
            f" sys.path.insert(0, {str(bundle_dir)!r}); {stmts}")
    return subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                          timeout=60, cwd=str(bundle_dir))


def _script_copy_without(tmp_path: Path, line_fragment: str) -> Path:
    """A copy of the build script, in a fake repo layout, with every line containing
    `line_fragment` removed.

    `scripts/lib/` is symlinked into the fake root so that `check-bundle-abi.py`
    (called by `bundle_lambda`) is reachable. Without this symlink the ABI check
    fails for the FIRST bundle and the script exits 1 before reaching any staging step
    under test — all three existing mutation cases passed vacuously because the old
    `return` escape treated "script exited before building the bundle" as a pass
    (review-c8-core.md W2).
    """
    fake_root = tmp_path / "repo"
    (fake_root / "scripts").mkdir(parents=True)
    for rel in ("agents", "lambdas"):
        os.symlink(_REPO / rel, fake_root / rel)
    os.symlink(_REPO / "scripts" / "lib", fake_root / "scripts" / "lib")
    text = _SCRIPT.read_text()
    lines = text.splitlines(keepends=True)
    kept = [ln for ln in lines if line_fragment not in ln]
    assert len(kept) < len(lines), f"mutation anchor {line_fragment!r} not found"
    copy = fake_root / "scripts" / "build-avx-lambda.sh"
    copy.write_text("".join(kept))
    return copy


@pytest.mark.parametrize("name,modules", _HANDLER_LAZY_IMPORTS.items())
def test_bundled_handler_imports_in_isolation(name: str, modules: tuple[str, ...],
                                              tmp_path: Path) -> None:
    _run_script(_SCRIPT, tmp_path / "build")
    proc = _import_handler(tmp_path / "build" / name, modules)
    assert proc.returncode == 0, proc.stderr[-2000:]


def test_actions_bundle_carries_the_policy_but_not_the_executors(tmp_path: Path) -> None:
    """The actions Lambda never dispatches: policy table only, no registry, no executors
    (the opt-in rule pinned by tests/test_avx_build_path.py)."""
    _run_script(_SCRIPT, tmp_path / "build")
    bundle = tmp_path / "build" / "api-avx-actions"
    assert (bundle / "agents" / "executors" / "policy.py").is_file()
    assert not (bundle / "agents" / "executors" / "registry.py").exists()
    assert not (bundle / "agents" / "executors" / "dms.py").exists()


@pytest.mark.parametrize("name,fragment", [
    # --- existing cases (api-avx-findings, api-avx-actions) ---
    ("api-avx-findings", 'cp "$ROOT/agents/tier2/schemas/action.schema.json" \\'),
    ("api-avx-actions",  'cp "$ROOT/agents/tier2/schemas/action.schema.json" "$OUT/'),
    ("api-avx-actions",  'bundle_policy "api-avx-actions"'),
    # --- F1.1 C1: failure_reason.py must be staged by bundle_policy ---
    # Remove the cp of failure_reason.py → bundle lacks it → handler module-scope import fails.
    ("api-avx-actions",  'cp "$ROOT/agents/executors/failure_reason.py"'),
    # --- new case (api-avx-findings hook staging) ---
    # findings-no-issue-hook: agents/fi_quality/ and agents/tier2/writer.py are not
    # staged; `import agents.fi_quality.issue` (lazily imported by _make_adp_hook._hook
    # in registry.py at hook call time, # noqa: PLC0415) fails.
    ("api-avx-findings", 'bundle_issue_hook "api-avx-findings"'),
    # --- new case (api-avx-investigations write hook staging) ---
    # investigations-no-write-hook: agents/fi_quality/write.py and agents/tier2/writer.py
    # are not staged; handler.py imports agents.fi_quality.write at module scope, so
    # the Lambda fails at cold start before serving any request.
    ("api-avx-investigations", 'bundle_write_hook "api-avx-investigations"'),
    # --- new cases (api-avx-approval-rules, avx-auto-approver) ---
    # approval-rules-no-policy: policy.py is not staged; _is_auto_approvable (lazy) and
    # the direct `import agents.executors.policy` in the cold-start check both fail.
    ("api-avx-approval-rules", 'bundle_policy "api-avx-approval-rules"'),
    # approver-no-executors: agents/executors/ is not staged; bundle_run_records still
    # runs (its call line is not removed), but registry.py is absent so
    # `import agents.executors.registry` fails.
    ("avx-auto-approver", 'bundle_executors "avx-auto-approver"'),
    # approver-no-run-records: bundle_executors stages executors + schema, but
    # run_records.py is not staged; `import agents.tier2.run_records` fails.
    ("avx-auto-approver", 'bundle_run_records "avx-auto-approver"'),
], ids=[
    "findings-no-schema",
    "actions-no-schema",
    "actions-no-policy",
    "actions-no-failure-reason",
    "findings-no-issue-hook",
    "investigations-no-write-hook",
    "approval-rules-no-policy",
    "approver-no-executors",
    "approver-no-run-records",
])
def test_a_missing_staging_step_breaks_cold_start(name: str, fragment: str,
                                                  tmp_path: Path) -> None:
    """Each removed staging step must make the bundled handler (or its lazily imported
    modules) fail to import.

    `scripts/lib/` is symlinked into the fake repo root (see `_script_copy_without`) so
    that check-bundle-abi.py is reachable. With the symlink, the ABI check passes for
    bundles that were built correctly, and only the specific removed step is absent from
    the bundle. Without the symlink the script exits 1 at the first bundle regardless of
    the mutation, and the old `return` escape masked every case (review-c8-core.md W2).

    The old `return` is replaced with an assertion: `handler.py` MUST be present after
    the mutated script runs. If it is absent that is a setup failure (scripts/lib/ not
    symlinked, or the mutation removed the bundle_lambda call instead of a staging step),
    not the failure we are testing.
    """
    script = _script_copy_without(tmp_path, fragment)
    build = tmp_path / "build"
    subprocess.run(["bash", str(script)], capture_output=True, text=True, timeout=120,
                   env={**os.environ, "AVX_BUILD_BASE": str(build), "AVX_BUILD_SKIP_PIP": "1"})
    bundle = build / name
    assert (bundle / "handler.py").is_file(), (
        f"Setup failure: {bundle}/handler.py absent after the mutated script. "
        f"scripts/lib/ must be symlinked in the fake root, and the mutation anchor "
        f"({fragment!r}) must not remove the bundle_lambda call that writes handler.py. "
        "Check _script_copy_without."
    )
    modules = _HANDLER_LAZY_IMPORTS[name]
    proc = _import_handler(bundle, modules)
    assert proc.returncode != 0, (
        f"{name}: import of {modules!r} succeeded with {fragment!r} removed. "
        "The mutation was not caught — either the removed fragment does not correspond "
        "to a staging step that the cold-start import path needs, or the step was "
        "duplicated elsewhere in the script."
    )
    shutil.rmtree(build, ignore_errors=True)
