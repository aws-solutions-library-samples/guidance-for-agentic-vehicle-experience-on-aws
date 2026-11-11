"""``scripts/seed-stage-parameters.sh`` refuses a CMS internal endpoint without its stage.

FI rebalancing stage E Group 8, review cycle 2 W1. The auto-approver's ``execute-api:Invoke``
grant takes the API id and the stage out of ``/cvx/<stage>/cms-fi-internal-api-endpoint`` at
deploy time (``vsa-tier2-stack.ts``). A value with no stage, a second path segment or
surrounding whitespace deploys a grant that matches nothing, so the seeder must refuse it.

The script runs for real, in dry-run mode, inside a throwaway git repository (it reads
``HEAD:infrastructure/bin/vsa.ts``), with a stub ``aws`` on ``PATH`` that answers only
``sts get-caller-identity`` and ``ssm get-parameter`` and records anything else.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess

import pytest

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
_SCRIPT = os.path.join(_REPO_ROOT, "scripts", "seed-stage-parameters.sh")
_NAME = "/cvx/staging/cms-fi-internal-api-endpoint"
_REFUSAL = f"{_NAME} — REFUSING"
_WOULD = f"{_NAME} (tier OPTIONAL) — would CREATE"

_STUB_AWS = """#!/bin/sh
echo "$*" >> "$AWS_STUB_LOG"
case "$1 $2" in
  "sts get-caller-identity") echo '{}'; exit 0 ;;
  "ssm get-parameter") exit 255 ;;
esac
exit 97
"""


@pytest.fixture(scope="module")
def sandbox(tmp_path_factory):
    root = tmp_path_factory.mktemp("seed")
    os.makedirs(root / "scripts")
    os.makedirs(root / "infrastructure" / "bin")
    os.makedirs(root / "bin")
    shutil.copy(_SCRIPT, root / "scripts" / "seed-stage-parameters.sh")
    (root / "infrastructure" / "bin" / "vsa.ts").write_text("// no STAGE_DEFAULTS\n")
    stub = root / "bin" / "aws"
    stub.write_text(_STUB_AWS)
    stub.chmod(0o755)
    git = ["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@example.com"]
    subprocess.run(git + ["init", "-q"], check=True)
    subprocess.run(git + ["add", "-A"], check=True)
    subprocess.run(git + ["commit", "-q", "-m", "seed sandbox"], check=True)
    return root


def _gaps(out: str) -> int:
    found = re.findall(r"gaps=(\d+)", out)
    assert len(found) == 1, out
    return int(found[0])


@pytest.fixture(scope="module")
def accepted_gaps(sandbox) -> int:
    """The run's gap count with CMS's real output seeded (every other field is a gap here)."""
    out, _, _ = _seed(sandbox, "https://yourapi000.execute-api.us-west-2.amazonaws.com/prod/")
    return _gaps(out)


def _seed(sandbox, value: str) -> tuple[str, str, list[str]]:
    log = sandbox / f"aws-{abs(hash(value))}.log"
    env = {
        "PATH": f"{sandbox / 'bin'}:{os.environ['PATH']}",
        "HOME": str(sandbox),
        "AWS_STUB_LOG": str(log),
        "VSA_CMS_FI_INTERNAL_API_ENDPOINT": value,
    }
    proc = subprocess.run(
        ["bash", str(sandbox / "scripts" / "seed-stage-parameters.sh"), "--stage", "staging"],
        cwd=sandbox, env=env, capture_output=True, text=True, timeout=120,
    )
    calls = log.read_text().splitlines() if log.exists() else []
    return proc.stdout, proc.stderr, calls


@pytest.mark.parametrize("value", [
    "https://yourapi000.execute-api.us-west-2.amazonaws.com/prod/",   # CMS's RestApi.url
    "https://yourapi000.execute-api.us-west-2.amazonaws.com/prod",
])
def test_cms_s_output_shape_is_accepted(sandbox, value):
    out, err, calls = _seed(sandbox, value)
    assert _WOULD in out, (out, err)
    assert _REFUSAL not in err
    assert [c for c in calls if not c.startswith(("sts get-caller-identity", "ssm get-parameter"))] == []


@pytest.mark.parametrize("value", [
    "https://yourapi000.execute-api.us-west-2.amazonaws.com",
    "https://yourapi000.execute-api.us-west-2.amazonaws.com/",
    "https://yourapi000.execute-api.us-west-2.amazonaws.com/prod/extra",
    "https://yourapi000.execute-api.us-west-2.amazonaws.com/prod ",
    " https://yourapi000.execute-api.us-west-2.amazonaws.com/prod/",
    "https://cms.example.com/prod/",
    # Review c3 S1: a doubled slash, a pasted CDK output line, a trailing quote.
    "https://yourapi000.execute-api.us-west-2.amazonaws.com/prod//",
    "cms-staging-ui.FiInternalApiEndpoint = https://yourapi000.execute-api.us-west-2.amazonaws.com/prod/",
    'https://yourapi000.execute-api.us-west-2.amazonaws.com/prod/"',
])
def test_a_value_the_grant_cannot_match_is_refused(sandbox, accepted_gaps, value):
    out, err, calls = _seed(sandbox, value)
    assert _REFUSAL in err, (out, err)
    # Review c3 S3: a refusal counts as a gap, so --apply exits non-zero.
    assert _gaps(out) == accepted_gaps + 1, out
    assert _NAME + " (tier OPTIONAL) — would" not in out
    assert not any(c.startswith(f"ssm get-parameter --name {_NAME}") for c in calls), calls


def test_this_test_runs_in_both_ci_files():
    """GitLab's ``verify_fi_tier2_pins`` (with git installed, JUnit checked for skips and kept
    as an artifact) and lint.yml."""
    import yaml

    if not os.path.exists(os.path.join(_REPO_ROOT, ".gitlab-ci.yml")):
        # The public mirror ships lint.yml but not .gitlab-ci.yml (.publish-exclude); GitLab's
        # fail-on-skipped-tests check keeps this from skipping in the internal pipeline.
        pytest.skip(".gitlab-ci.yml is not in this tree")
    path = "scripts/tests/test_seed_cms_fi_internal_endpoint.py"
    with open(os.path.join(_REPO_ROOT, ".gitlab-ci.yml")) as fh:
        job = yaml.safe_load(fh).get("verify_fi_tier2_pins")
    assert isinstance(job, dict), "GitLab job verify_fi_tier2_pins not found"
    assert any("apt-get install" in s and " git" in s for s in job.get("before_script", []))
    script = [s for s in job.get("script", []) if isinstance(s, str)]
    runs = [s for s in script if "pytest" in s and s.split()[-1] == path]
    assert len(runs) == 1, runs
    report = re.search(r"--junitxml=(\S+)", runs[0]).group(1)
    assert f"python3 scripts/lib/fail-on-skipped-tests.py {report}" in script
    assert report in job.get("artifacts", {}).get("reports", {}).get("junit", [])
    with open(os.path.join(_REPO_ROOT, ".github", "workflows", "lint.yml")) as fh:
        assert f"python3 -m pytest {path} -q" in [ln.strip() for ln in fh.read().splitlines()]
