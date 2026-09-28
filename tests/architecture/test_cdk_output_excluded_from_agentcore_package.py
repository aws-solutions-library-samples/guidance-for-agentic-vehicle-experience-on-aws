"""`cdk.json`'s `output` must land where the AgentCore packager will not ship it.

This is the executable explanation of the `"output": "build/cdk.out"` key in
`infrastructure/cdk.json`. `cdk.json` is strict JSON and cannot carry a comment,
so without this test that key looks arbitrary and the next person to tidy it
would reintroduce a deploy-blocking failure.

The failure it prevents: `agentcore deploy` packages the repo root
(`source_path: .` in `.bedrock_agentcore.yaml`) and the toolkit's ignore list has
no `cdk.out` entry. `make deploy-staging` runs CDK *before* the runtime step, so
the CDK phase wrote ~494 MB into `infrastructure/cdk.out` and the runtime phase
then packaged it:

    ✓ Deployment package ready: 416.12 MB
    ❌ Launch failed: Package size (416.12 MB) exceeds 250MB limit

All 7 CloudFormation stacks deployed; only the runtimes failed. See
issues/2026-09-22-agentcore-package-sweeps-cdk-out/.

Why redirecting is the fix rather than a `.dockerignore`: the toolkit reads
**only** the template bundled inside the installed package
(`_get_ignore_patterns`), never a repository `.dockerignore`. That template
excludes `build/`, and `_matches_pattern` ignores a directory when ANY path
component matches — so `infrastructure/build/cdk.out` is excluded while
`infrastructure/cdk.out` is not. `.gitignore` already covers
`infrastructure/build/`, so nothing new leaks into git either.

Run:
    pytest tests/architecture/test_cdk_output_excluded_from_agentcore_package.py -v
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
_CDK_JSON = _REPO / "infrastructure" / "cdk.json"


def _packager_ignore_check():
    """Return (instance, patterns) from the real toolkit, or skip.

    Uses the toolkit's own class rather than a reimplementation: a local copy of
    the matching rules could agree with itself and disagree with what actually
    ships.
    """
    pkg = pytest.importorskip(
        "bedrock_agentcore_starter_toolkit.utils.runtime.package",
        reason="agentcore starter toolkit not installed",
    )
    candidates = [
        v for v in vars(pkg).values()
        if isinstance(v, type) and hasattr(v, "_should_ignore") and hasattr(v, "_get_ignore_patterns")
    ]
    assert candidates, "toolkit exposes no packager with the ignore helpers"
    inst = candidates[0].__new__(candidates[0])  # no __init__ needed for these two
    return inst, inst._get_ignore_patterns()


def _cdk_output() -> str:
    cfg = json.loads(_CDK_JSON.read_text())
    output = cfg.get("output")
    assert output, (
        "infrastructure/cdk.json has no 'output' key. Without it, CDK writes to "
        "infrastructure/cdk.out, which the AgentCore packager DOES ship — and the "
        "runtime deploy then fails the 250 MB limit. See this module's docstring."
    )
    return output


def test_cdk_output_is_redirected_at_all():
    assert _cdk_output() == "build/cdk.out"


def test_the_cdk_output_directory_is_ignored_by_the_agentcore_packager():
    """The property that matters: what CDK writes must not reach the package."""
    inst, patterns = _packager_ignore_check()
    # cdk.json's `output` is relative to the CDK app directory.
    rel = f"infrastructure/{_cdk_output()}"
    assert inst._should_ignore(rel, patterns, True), (
        f"{rel} is NOT ignored by the packager — the runtime deploy will ship the "
        "whole cloud assembly and fail the 250 MB limit."
    )


def test_the_default_location_would_not_be_ignored():
    """Negative control: proves the redirect is load-bearing, not decorative.

    If this ever starts passing, the toolkit has gained a `cdk.out` pattern and
    the `output` key could be dropped — but verify that deliberately rather than
    assuming it.
    """
    inst, patterns = _packager_ignore_check()
    assert not inst._should_ignore("infrastructure/cdk.out", patterns, True)


@pytest.mark.parametrize(
    "path,is_dir",
    [
        ("agents", True),
        ("agents/supervisor/bidi_app.py", False),
        ("agents/supervisor/config.py", False),
        ("requirements.txt", False),
    ],
)
def test_runtime_code_is_still_shipped(path, is_dir):
    """Positive control: the exclusion must not have caught the runtime itself.

    An over-broad ignore would produce a small package that cannot start, which
    is a worse failure than the oversized one being fixed.
    """
    inst, patterns = _packager_ignore_check()
    assert not inst._should_ignore(path, patterns, is_dir), f"{path} would not ship"


def test_the_redirect_target_is_gitignored():
    """The cloud assembly must not become a committable artifact."""
    ignored = (_REPO / ".gitignore").read_text()
    assert "infrastructure/build/" in ignored or "build/" in ignored.split()


def test_no_test_hardcodes_the_old_cdk_out_path():
    """Changing `output` must not silently orphan a consumer of the assembly.

    This is the second defect the redirect caused and the reason this test
    exists. `test_deterministic_seams.py` hardcoded
    `infrastructure/cdk.out/vsa-staging-core.template.json` and **skips** when the
    template is absent — so moving `output` turned
    `test_agent_iam_has_no_connect_action` and its siblings from red to *skipped*,
    with nothing reporting it. A deterministic-seam guard that skips is not
    guarding, and a skip is much harder to notice than a failure.

    The fix was to derive the directory from `cdk.json`. This test keeps it
    derived: any test that rebuilds the literal path is drifting away from the
    configured one.
    """
    offenders = []
    for path in (_REPO / "agents").rglob("test_*.py"):
        text = path.read_text()
        consults_cdk_json = "cdk.json" in text
        for lineno, line in enumerate(text.splitlines(), 1):
            if '"cdk.out"' not in line and "'cdk.out'" not in line:
                continue
            # Sanctioned: the default inside a derivation that DOES read
            # cdk.json, for a repo where `output` is unset. That line is not
            # drift — it is the fallback the derivation needs. Recognised by the
            # `or` default sitting in a file that reads cdk.json, so a plain
            # hardcoded path elsewhere in the same file is still caught.
            if consults_cdk_json and ("configured or" in line or "or \"cdk.out\"" in line):
                continue
            offenders.append(f"{path.relative_to(_REPO)}:{lineno}: {line.strip()}")
    assert not offenders, (
        "These reference the CDK output directory as a literal instead of reading "
        "`output` from infrastructure/cdk.json, so a change to that key will "
        "silently orphan them (skip, not fail):\n  " + "\n  ".join(offenders)
    )


def test_the_deterministic_seam_template_resolves_under_the_configured_output():
    """The specific consumer, pinned: its template path must follow cdk.json."""
    seams = pytest.importorskip(
        "agents.supervisor.tests.test_deterministic_seams",
        reason="supervisor tests not importable here",
    )
    resolved = Path(seams._STAGING_CORE_TEMPLATE)
    expected_dir = (_REPO / "infrastructure" / _cdk_output()).resolve()
    assert resolved.parent.resolve() == expected_dir, (
        f"deterministic-seams reads {resolved.parent}, but cdk.json writes to "
        f"{expected_dir}. The seam guards would skip rather than run."
    )
