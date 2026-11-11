"""
test_avx_build_path.py — Guard that both deploy paths invoke build-avx-lambda.sh.

Critical C2 fix (Cycle 2, 2026-09-19): scripts/build-avx-lambda.sh was not invoked
by either .github/workflows/evals.yml ("Build Lambda assets" step) or the Makefile
`deploy-staging` target. On the next push to main, CDK synth would fail with
`Cannot find asset at .../infrastructure/build/api-avx-findings`.

This test catches regressions where someone removes the script from one of the
invocation sites (equivalent to: rm the line from evals.yml or Makefile → test FAILS).

Mutation procedure (verified 2026-09-19 fix cycle 2):
  1. Remove the `scripts/build-avx-lambda.sh` line from evals.yml → test FAILS.
  2. Remove the `bash scripts/build-avx-lambda.sh` line from Makefile → test FAILS.
  Mutation reverted. Tests green. ✓ CAUGHT
"""
import pathlib
import re
import os
import subprocess
import sys
import pytest

_REPO_ROOT = pathlib.Path(__file__).parent.parent


def _evals_yml_text() -> str:
    path = _REPO_ROOT / ".github" / "workflows" / "evals.yml"
    # The public mirror ships neither evals.yml nor .publish-exclude, so the evals.yml test
    # skips there. In the internal repo a missing evals.yml still fails.
    if not path.exists() and not (_REPO_ROOT / ".publish-exclude").exists():
        pytest.skip(".github/workflows/evals.yml is not in the public copy")
    return path.read_text()


def _makefile_text() -> str:
    return (_REPO_ROOT / "Makefile").read_text()


def test_evals_yml_build_step_includes_avx_lambda_script():
    """evals.yml 'Build Lambda assets' step must invoke build-avx-lambda.sh.

    Mutation: remove `scripts/build-avx-lambda.sh` from evals.yml → this test FAILS.
    """
    text = _evals_yml_text()
    # The script must appear under the Build Lambda assets step section.
    # We verify it appears after the Build Lambda assets marker and before the next step.
    build_step_pos = text.find("Build Lambda assets")
    assert build_step_pos != -1, "Could not find 'Build Lambda assets' step in evals.yml"
    next_step_pos = text.find("- name:", build_step_pos + 1)
    build_section = text[build_step_pos:next_step_pos if next_step_pos != -1 else len(text)]
    assert "build-avx-lambda.sh" in build_section, (
        "scripts/build-avx-lambda.sh is NOT in the 'Build Lambda assets' step of "
        ".github/workflows/evals.yml. The CI auto-deploy will fail at CDK synth with "
        "'Cannot find asset at .../infrastructure/build/api-avx-findings'. "
        "Add `scripts/build-avx-lambda.sh` to the step's `run:` block."
    )


def test_makefile_deploy_staging_invokes_build_avx_lambda():
    """Makefile deploy-staging target must invoke build-avx-lambda.sh before CDK synth.

    Mutation: remove `bash scripts/build-avx-lambda.sh` from Makefile → this test FAILS.
    """
    text = _makefile_text()
    # Locate the deploy-staging target
    target_pos = text.find("deploy-staging:")
    assert target_pos != -1, "deploy-staging target not found in Makefile"
    # Find the next target (line starting without a tab — i.e. new target definition)
    after_target = text[target_pos:]
    # Extract all lines until the next target definition (a line that starts at column 0
    # with a word followed by a colon, but not indented with a tab)
    target_block_match = re.search(
        r'^deploy-staging:.*?(?=^[a-zA-Z_][a-zA-Z_0-9\-]*:|\Z)',
        after_target,
        re.DOTALL | re.MULTILINE,
    )
    assert target_block_match is not None, "Could not extract deploy-staging block from Makefile"
    block = target_block_match.group(0)
    assert "build-avx-lambda.sh" in block, (
        "scripts/build-avx-lambda.sh is NOT invoked in the Makefile `deploy-staging` target "
        "before `_deploy-cdk`. Operator-manual deploys will fail at CDK synth when the "
        "infrastructure/build/api-avx-* directories are absent. "
        "Add `bash scripts/build-avx-lambda.sh` immediately before the `_deploy-cdk` invocation."
    )


def test_build_avx_lambda_sh_invoked_before_deploy_cdk_in_makefile():
    """In Makefile deploy-staging, build-avx-lambda.sh must appear before _deploy-cdk.

    Ensures that the script is not accidentally placed after synth/deploy (ordering
    matters because CDK synth reads the built directory).
    """
    text = _makefile_text()
    target_pos = text.find("deploy-staging:")
    assert target_pos != -1
    deploy_section = text[target_pos:]
    script_pos = deploy_section.find("build-avx-lambda.sh")
    cdk_pos = deploy_section.find("_deploy-cdk")
    assert script_pos != -1, "build-avx-lambda.sh not found in deploy-staging"
    assert cdk_pos != -1, "_deploy-cdk not found in deploy-staging"
    assert script_pos < cdk_pos, (
        "build-avx-lambda.sh must appear BEFORE _deploy-cdk in deploy-staging target. "
        "CDK synth reads the built asset directories; running the script after synth "
        "has no effect on the current deploy."
    )



# ─── Executor package staging (Step-5 prerequisite 2) ─────────────────────────
#
# `agents/executors/` was packaged into nothing. `bundle_lambda` copies only
# `lambdas/<name>/` plus `_lib/avx_scope.py`, so the 7 executors Group 5 shipped
# could never be imported by a deployed artifact — `handler.py`'s own comment says
# so: "agents/executors/ is packaged into nothing — zero references in
# infrastructure/lib/*.ts or the Makefile".
#
# These guard the staging that fixes it. The load-bearing one is
# `test_staged_executor_bundle_imports_in_isolation`: a repo-root import resolves
# everything from the dev venv and passes even when the bundle is incomplete, which
# is exactly how the missing `requests` dependency stayed invisible until the bundle
# was imported with ONLY its own directory on sys.path.

_BUNDLE = _REPO_ROOT / "infrastructure" / "build" / "api-avx-findings"


def _build_script_text() -> str:
    return (_REPO_ROOT / "scripts" / "build-avx-lambda.sh").read_text()


def test_build_script_stages_executors_into_the_approve_lambda():
    """The approve route dispatches, so its bundle must carry the executor package."""
    text = _build_script_text()
    assert "bundle_executors" in text, (
        "scripts/build-avx-lambda.sh no longer defines/calls bundle_executors. Without "
        "it agents/executors/ ships nowhere and the approve route's dispatch raises "
        "ModuleNotFoundError at cold start."
    )
    assert 'bundle_executors "api-avx-findings"' in text, (
        "api-avx-findings is the Lambda whose approve route dispatches; it must be the "
        "one receiving the executor package."
    )


def test_executor_staging_is_opt_in_not_blanket():
    """Only Lambdas that dispatch should carry the executor package.

    Two Lambdas now dispatch and therefore both receive bundle_executors:
      - api-avx-findings: the approve route calls registry.dispatch() on every
        operator decision (spec Group 5, dispatching executor abstraction).
      - avx-auto-approver: the evaluator calls registry.dispatch() on every rule
        match (spec D9, contract § 6 — "auto-approved" decision writes + dispatches).

    Asserted as exactly two (not "at least two") because the tempting simplification —
    staging executors for every AVX Lambda inside bundle_lambda — would add a `requests`
    dependency and executor import surface to read-only routes that never dispatch.
    If a third Lambda genuinely dispatches, update this test deliberately rather than
    widening staging by default.

    Mutation procedure (verified Fix Group 3a):
      1. Remove `bundle_executors "avx-auto-approver"` from the script →
         test fails (count becomes 1). ✓ CAUGHT
      2. Restore. Tests green.
    """
    text = _build_script_text()
    calls = [ln for ln in text.splitlines() if ln.strip().startswith("bundle_executors ")]
    # FI D10 (prompt 287): a third Lambda genuinely dispatches. avx-sim-operator, the
    # simulated fleet operator, dispatches its demo-fleet decisions through
    # registry.dispatch. Exact set, so a fourth still has to be added here on purpose.
    assert sorted(ln.strip() for ln in calls) == [
        'bundle_executors "api-avx-findings"',
        'bundle_executors "avx-auto-approver"',
        'bundle_executors "avx-sim-operator"',
    ], (
        f"bundle_executors calls changed: {calls}. api-avx-findings dispatches on its approve "
        "route; avx-auto-approver on every rule match (spec D9); avx-sim-operator on the "
        "stand-in's decisions (spec D10). Update this test deliberately rather than widening "
        "staging by default."
    )


@pytest.mark.skipif(not _BUNDLE.is_dir(), reason="bundle not built; run scripts/build-avx-lambda.sh")
def test_staged_executor_bundle_imports_in_isolation():
    """Import the registry with the bundle dir FIRST on sys.path.

    What this proves: the staged package's structure and internal imports are intact —
    `agents/__init__.py` present, `agents.executors.*` resolvable, the registry's
    `_validate_registry()` passing, `dms` reaching a real `DmsExecutor`.

    What it does NOT prove, stated plainly because two earlier versions of this test
    claimed otherwise: **dependency completeness.** That is guarded by
    `test_module_scope_executor_imports_are_declared_in_requirements` below, which is
    the test that actually caught `requests` missing.

    Two failed attempts at "true" isolation, both recorded because each looked right:

    1. Plain subprocess with `PYTHONPATH=""` — not isolation at all. The interpreter is
       the dev venv's, so `import requests` resolved from its site-packages and this
       passed against a bundle with `requests` deliberately removed. Vacuous in exactly
       the way it was written to prevent.
    2. Adding `-S` to suppress site-packages — fails even on a CORRECT bundle, and the
       reason is by design: `bundle_lambda` deliberately deletes `urllib3`, `boto3`,
       `botocore`, `s3transfer`, `jmespath`, `dateutil` and `six` from the bundle
       because the Lambda runtime supplies them. `requests` imports `urllib3`, so a
       bundle that is correct for Lambda cannot import under full isolation. The bundle
       is intentionally not self-contained.

    So the venv interpreter is the closest available stand-in for the Lambda runtime's
    provided packages, and this test is scoped to what that can honestly establish.
    """
    code = (
        "import sys; sys.path.insert(0, '.');"
        "from agents.executors.registry import EXECUTORS, get_executor;"
        "print(','.join(sorted(EXECUTORS)));"
        "print(type(get_executor('dms')).__name__)"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        cwd=_BUNDLE, capture_output=True, text=True,
        env={**os.environ, "PYTHONPATH": ""},
    )
    assert proc.returncode == 0, (
        "the staged bundle cannot import its own executor registry — this is a cold-start "
        f"failure, not a test-only problem.\nstderr:\n{proc.stderr}"
    )
    targets = proc.stdout.splitlines()[0].split(",")
    assert "dms" in targets, f"dms target missing from the staged registry: {targets}"
    assert proc.stdout.splitlines()[1] == "DmsExecutor"


@pytest.mark.skipif(not _BUNDLE.is_dir(), reason="bundle not built; run scripts/build-avx-lambda.sh")
def test_staged_bundle_has_the_agents_package_marker():
    """`agents/__init__.py` must be present or `agents.executors` is not importable.

    Its own failure mode is the `_lib` problem this script was written for, and the
    `redis` incident before that: a package that exists in the tree but not on the
    deployed artifact's import path.
    """
    for required in ("agents/__init__.py", "agents/executors/__init__.py",
                     "agents/executors/registry.py", "agents/executors/dms.py"):
        assert (_BUNDLE / required).is_file(), f"{required} missing from the staged bundle"


@pytest.mark.skipif(not _BUNDLE.is_dir(), reason="bundle not built; run scripts/build-avx-lambda.sh")
def test_staged_bundle_excludes_executor_tests():
    """Runtime artifacts carry no test directories."""
    assert not (_BUNDLE / "agents" / "executors" / "tests").exists(), (
        "executor tests were staged into the Lambda bundle; bundle_executors excludes "
        "tests/ for the same reason bundle_lambda does."
    )


def _dispatching_lambdas() -> list[str]:
    """Every Lambda the build script stages the executor package into."""
    import re

    text = _build_script_text()
    names = re.findall(r'^bundle_executors "([^"]+)"', text, re.M)
    assert names, "no bundle_executors calls found in the build script"
    return names


@pytest.mark.parametrize("lambda_name", _dispatching_lambdas())
def test_module_scope_executor_imports_are_declared_in_requirements(lambda_name):
    """Every module-scope third-party import in the executor package is pinned here.

    Structural rather than a fixed list, so a NEW module-scope import in any executor
    fails this test instead of failing at cold start.

    Deliberately does NOT cover lazy in-method imports. `chat.py` imports
    `bedrock_agentcore.memory` inside a method, so the package imports fine without it
    and only `target_system=chat` dispatch needs it — see the requirements.txt note.
    That gap is real and is recorded there rather than hidden here.
    """
    import ast

    src = _REPO_ROOT / "agents" / "executors"
    # Every dispatching Lambda, not only the approve route (Group 2 review Cycle 9
    # Critical: the evaluator bundled the executors with no requirements.txt, so
    # `requests` was missing in the Lambda runtime and every stream record failed).
    req_path = _REPO_ROOT / "lambdas" / lambda_name / "requirements.txt"
    assert req_path.is_file(), f"{lambda_name} bundles agents/executors but has no requirements.txt"
    reqs = req_path.read_text()
    declared = {
        ln.split("==")[0].split("[")[0].strip().lower()
        for ln in reqs.splitlines()
        if ln.strip() and not ln.strip().startswith("#")
    }
    stdlib = set(sys.stdlib_module_names)
    local = {"agents", "_lib", "handler"}

    missing: dict[str, list[str]] = {}
    for path in sorted(src.glob("*.py")):
        tree = ast.parse(path.read_text())
        for node in tree.body:  # TOP-LEVEL only; lazy imports are out of scope by design
            mods: list[str] = []
            if isinstance(node, ast.Import):
                mods = [a.name.split(".")[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                mods = [node.module.split(".")[0]]
            for mod in mods:
                if mod in stdlib or mod in local:
                    continue
                if mod.lower() not in declared:
                    missing.setdefault(mod, []).append(path.name)

    assert not missing, (
        "module-scope imports in agents/executors/ are not declared in "
        f"lambdas/{lambda_name}/requirements.txt: {missing}. The dispatching Lambda "
        "bundles this package, so an undeclared module-scope import is a cold-start "
        "ModuleNotFoundError."
    )
