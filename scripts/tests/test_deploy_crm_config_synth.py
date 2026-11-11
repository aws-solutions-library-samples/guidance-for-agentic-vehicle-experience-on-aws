"""End to end: the CRM config the wrapper reads from SSM reaches the escalate Lambda.

Issue `issues/2026-11-03-deploy-drops-crm-config/`. `test_deploy_crm_config.py` pins that
`scripts/with-stage-env.sh` exports `CVX_CONNECT_CRM_CONFIG`; nothing there pins that
`infrastructure/bin/vsa.ts` reads that same name. If either side renamed it, the wrapper would
export a variable nobody reads and every deploy would drop the feature again with both suites
green. So this runs the real wrapper (with a fake `aws` answering only the CRM parameter) in front
of a real `cdk synth vsa-test-api` and reads the escalate function's env from the template.

`--exclusively` keeps asset bundling to the API stack (the Tier 2 stack's bundling needs Docker),
and `--ignore-errors` covers the API stack's VPC lookup, as in `test_stage_defaults_guard.py`;
the positive case is the control that the template is real.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from .test_stage_defaults_guard import INFRA_DIR, _base_env, _skip_if_no_tooling

REPO_ROOT = Path(__file__).resolve().parents[2]
WRAPPER = REPO_ROOT / "scripts" / "with-stage-env.sh"
ESCALATE_FN = "vsa-test-api-escalate"

# Shape-valid placeholders (all-ones account, as in test_stage_defaults_guard.py).
CONFIG = {
    "instanceArn": "arn:aws:connect:us-east-1:111122223333:instance/test-instance-0001",
    "profilesDomain": "test-profiles-domain",
    "objectType": "CvxVehicle",
    "casesDomainId": "test-cases-domain",
    "casesTemplateId": "test-cases-template",
    "fieldIds": {k: f"test-field-{k}" for k in
                 ("severity", "vin", "vehicleId", "faultCodes", "escalationKey", "triageSessionId")},
}

_FAKE_AWS = r"""#!/bin/bash
# Answers only `ssm get-parameter --name /cvx/test/connect-crm-config`, from $FAKE_CRM_FILE.
name=""; prev=""
for a in "$@"; do [ "$prev" = "--name" ] && name="$a"; prev="$a"; done
if [ "$name" = "/cvx/test/connect-crm-config" ] && [ -n "${FAKE_CRM_FILE:-}" ] && [ -f "$FAKE_CRM_FILE" ]; then
  cat "$FAKE_CRM_FILE"; printf '\n'; exit 0
fi
printf '\nAn error occurred (ParameterNotFound) when calling the GetParameter operation: Parameter %s not found.\n' "$name" >&2
exit 254
"""


def _escalate_env(tmp_path: Path, config: dict | None) -> dict[str, str]:
    _skip_if_no_tooling()
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "aws").write_text(_FAKE_AWS)
    (bindir / "aws").chmod(0o755)
    env = _base_env()
    env.pop("CVX_CONNECT_CRM_CONFIG", None)
    env["PATH"] = f"{bindir}:{env['PATH']}"
    if config is not None:
        crm_file = tmp_path / "crm.json"
        crm_file.write_text(json.dumps(config))
        env["FAKE_CRM_FILE"] = str(crm_file)
    # Static argv, shell=False.
    # nosemgrep: dangerous-subprocess-use-audit
    proc = subprocess.run(
        ["bash", str(WRAPPER), "test", env["AWS_REGION"], "--",
         "npx", "cdk", "synth", "vsa-test-api", "--json", "--exclusively", "--ignore-errors"],
        cwd=INFRA_DIR, env=env, capture_output=True, text=True, timeout=600, check=False,
    )
    if not proc.stdout.strip().startswith("{"):
        pytest.fail(f"no template from the wrapper + cdk synth.\nstderr:\n{proc.stderr[-2000:]}")
    resources = json.loads(proc.stdout).get("Resources", {})
    fns = [r for r in resources.values()
           if r.get("Type") == "AWS::Lambda::Function"
           and r.get("Properties", {}).get("FunctionName") == ESCALATE_FN]
    assert len(fns) == 1, f"expected one {ESCALATE_FN} function, found {len(fns)}"
    return fns[0]["Properties"].get("Environment", {}).get("Variables", {})


class TestCrmConfigReachesTheEscalateLambda:
    def test_config_in_ssm_lands_on_the_escalate_lambda(self, tmp_path: Path) -> None:
        env = _escalate_env(tmp_path, CONFIG)
        assert "CONNECT_CRM_CONFIG" in env, sorted(env)
        assert json.loads(env["CONNECT_CRM_CONFIG"])["instanceArn"] == CONFIG["instanceArn"]
        assert env.get("CONNECT_CRM_TENANTS") == "meridian"

    def test_no_parameter_means_no_crm_env(self, tmp_path: Path) -> None:
        env = _escalate_env(tmp_path, None)
        assert "CONNECT_CRM_CONFIG" not in env
        assert "CONNECT_CRM_TENANTS" not in env
