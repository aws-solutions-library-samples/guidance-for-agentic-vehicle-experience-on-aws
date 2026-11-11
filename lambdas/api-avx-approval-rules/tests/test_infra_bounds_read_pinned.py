"""Pin the approval-rules Lambda's bounds read in vsa-api-stack.ts (stage E Group 8, G8-D5/D6).

The handler reads ``AVX_AUTO_APPROVAL_BOUNDS_PARAM`` to validate a ``move_vehicles`` rule's
limit. The stack must name exactly the bounds parameter in that env var, and the role must hold
``ssm:GetParameter`` on exactly that parameter and nothing wider: a wildcard would let the rules
API read any parameter, and a missing grant would turn every move-rule write into a 503.

Source-level, because jest (which synthesizes the template) runs in no CI job. It runs in
GitLab's ``verify_fi_tier2_pins`` and in lint.yml (the whole tests directory).
"""
from __future__ import annotations

import pathlib
import re

import pytest

_REPO = pathlib.Path(__file__).resolve().parents[3]
_STACK = _REPO / "infrastructure" / "lib" / "vsa-api-stack.ts"
_HANDLER = _REPO / "lambdas" / "api-avx-approval-rules" / "handler.py"
_PARAM = "/vsa/${props.stage}/avx/fi-rebalancing/auto-approval-bounds"


_GRANT_CALL = re.compile(
    r"apiAvxApprovalRules(?:\.role[?!]?)?\.(?:addToRolePolicy|addToPolicy|addToPrincipalPolicy)\(")


def _statements(src: str) -> list[str]:
    """The body of every grant call on the rules Lambda or its role, matched by parentheses."""
    out = []
    for m in _GRANT_CALL.finditer(src):
        depth, i = 1, m.end()
        while depth:
            depth += {"(": 1, ")": -1}.get(src[i], 0)
            i += 1
        out.append(src[m.end():i - 1])
    return out


def test_the_env_names_the_bounds_parameter():
    src = _STACK.read_text()
    assert f"AVX_AUTO_APPROVAL_BOUNDS_PARAM: `{_PARAM}`," in src
    assert src.count("AVX_AUTO_APPROVAL_BOUNDS_PARAM:") == 1
    assert 'os.environ.get("AVX_AUTO_APPROVAL_BOUNDS_PARAM"' in _HANDLER.read_text()


def test_one_statement_reads_exactly_that_parameter():
    stmts = _statements(_STACK.read_text())
    ssm = [s for s in stmts if "ssm:" in s]
    assert len(ssm) == 1, ssm
    (stmt,) = ssm
    assert "sid: 'AvxAutoApprovalBoundsRead'" in stmt
    assert re.search(r"actions:\s*\[\s*'ssm:GetParameter'\s*\]", stmt)
    resources = re.findall(r"`([^`]*)`", stmt.split("resources:", 1)[1])
    assert resources == [
        "arn:aws:ssm:${cdk.Stack.of(this).region}:${cdk.Stack.of(this).account}:parameter" + _PARAM
    ]
    assert "effect: iam.Effect.ALLOW" in stmt
    src = _STACK.read_text()
    for other in ("apiAvxApprovalRules.role?.attachInlinePolicy", "apiAvxApprovalRules.role!.attachInlinePolicy",
                  "apiAvxApprovalRules.role?.addManagedPolicy", "apiAvxApprovalRules.role!.addManagedPolicy"):
        assert other not in src


def test_this_pin_runs_in_gitlab_ci():
    """The public mirror ships neither ``.gitlab-ci.yml`` nor ``.publish-exclude``, so the
    test skips there. In the internal repo a missing ``.gitlab-ci.yml`` still fails."""
    import yaml

    if not (_REPO / ".gitlab-ci.yml").exists() and not (_REPO / ".publish-exclude").exists():
        pytest.skip(".gitlab-ci.yml is not in the public copy")
    path = "lambdas/api-avx-approval-rules/tests/test_infra_bounds_read_pinned.py"
    job = yaml.safe_load((_REPO / ".gitlab-ci.yml").read_text()).get("verify_fi_tier2_pins")
    assert isinstance(job, dict), "GitLab job verify_fi_tier2_pins not found"
    script = [x for x in job.get("script", []) if isinstance(x, str)]
    runs = [x for x in script if "pytest" in x and x.split()[-1] == path]
    assert len(runs) == 1, runs
    junit = re.search(r"--junitxml=(\S+)", runs[0]).group(1)
    assert f"python3 scripts/lib/fail-on-skipped-tests.py {junit}" in script
    assert junit in job.get("artifacts", {}).get("reports", {}).get("junit", [])
