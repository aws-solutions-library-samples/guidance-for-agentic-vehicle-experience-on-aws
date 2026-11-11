"""A `vsa-<stage>-api` deploy must not silently turn Connect Profiles and Cases off.

Issue `issues/2026-11-03-deploy-drops-crm-config/`. `infrastructure/bin/vsa.ts` turns the
feature on only when it gets `-c connectCrmConfig` or env `CVX_CONNECT_CRM_CONFIG`, and every
supported deploy path (`make deploy-staging`, `make deploy`, `make synth`/`diff`, the runbook's
narrow `cdk diff`/`deploy`) runs through `scripts/with-stage-env.sh`. So the wrapper reads
`/cvx/<stage>/connect-crm-config`:

- found: exported as `CVX_CONNECT_CRM_CONFIG` (never printed);
- explicitly exported already: left alone, SSM not read;
- `ParameterNotFound`: left unset (the stage has no CRM config, the feature is off), with a
  warning naming the parameter;
- any other failure (access denied, no credentials, throttling, an empty or `None` value): the
  wrapper refuses and the command does not run, because deploying then would turn the feature off.

`make deploy-staging` also stages every `scripts/build-*.sh` bundle before `cdk deploy`.

The wrapper is driven with a fake `aws` first on PATH; nothing here calls AWS.
"""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
WRAPPER = REPO_ROOT / "scripts" / "with-stage-env.sh"
MAKEFILE = REPO_ROOT / "Makefile"

PARAM = "/cvx/staging/connect-crm-config"
# Shape-valid placeholder (all-ones account, as in test_stage_defaults_guard.py). The marker
# substring is what the "never printed" test looks for.
MARKER = "crm-marker-7f3e"
CONFIG = (
    '{"instanceArn": "arn:aws:connect:us-east-1:111122223333:instance/' + MARKER + '", '
    '"profilesDomain": "fake-profiles", "objectType": "CvxVehicle"}'
)

_FAKE_AWS = r"""#!/bin/bash
# Fake aws CLI: records argv, answers `ssm get-parameter --name <n>` from $FAKE_SSM_DIR.
printf '%s\n' "$*" >> "$FAKE_AWS_LOG"
# The wrapper also reads the APNs secret's metadata (`secretsmanager describe-secret`).
# Answer as Secrets Manager does for a stage with no APNs key (staging today): the
# wrapper exports CVX_APNS_KEY_VERSION=none and runs the command.
if [ "$1" = "secretsmanager" ]; then
  printf '\nAn error occurred (ResourceNotFoundException) when calling the DescribeSecret operation: Secrets Manager cannot find the specified secret.\n' >&2
  exit 254
fi
name=""
prev=""
for a in "$@"; do
  [ "$prev" = "--name" ] && name="$a"
  prev="$a"
done
key="$(printf '%s' "$name" | tr '/' '_')"
if [ -f "$FAKE_SSM_DIR/$key.err" ]; then
  cat "$FAKE_SSM_DIR/$key.err" >&2
  exit 254
fi
if [ -f "$FAKE_SSM_DIR/$key" ]; then
  cat "$FAKE_SSM_DIR/$key"
  printf '\n'
  exit 0
fi
printf '\nAn error occurred (ParameterNotFound) when calling the GetParameter operation: Parameter %s not found.\n' "$name" >&2
exit 254
"""

# Variables the wrapper or vsa.ts read; stripped so the operator's shell can't leak in.
_STRIP = re.compile(r"^(CVX_|VSA_|CMS_|VOICE_AUTH_|EXTRA_USER_POOL_IDS$|AWS_|CDK_)")


@pytest.fixture()
def world(tmp_path: Path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    fake = bindir / "aws"
    fake.write_text(_FAKE_AWS)
    fake.chmod(0o755)
    ssm = tmp_path / "ssm"
    ssm.mkdir()
    log = tmp_path / "aws.log"
    log.write_text("")
    out = tmp_path / "out.txt"

    def set_param(name: str, value: str) -> None:
        (ssm / name.replace("/", "_")).write_text(value)

    def set_error(name: str, stderr_text: str) -> None:
        (ssm / (name.replace("/", "_") + ".err")).write_text(stderr_text)

    def run(extra_env: dict[str, str] | None = None, stage: str = "staging",
            region: str = "us-west-2") -> subprocess.CompletedProcess[str]:
        env = {k: v for k, v in os.environ.items() if not _STRIP.match(k)}
        env.update(
            PATH=f"{bindir}:/usr/bin:/bin",
            FAKE_SSM_DIR=str(ssm),
            FAKE_AWS_LOG=str(log),
            OUT=str(out),
        )
        env.update(extra_env or {})
        # nosemgrep: dangerous-subprocess-use-audit
        return subprocess.run(
            ["bash", str(WRAPPER), stage, region, "--",
             "sh", "-c", 'printf %s "${CVX_CONNECT_CRM_CONFIG-__UNSET__}" > "$OUT"'],
            env=env, capture_output=True, text=True, timeout=60, check=False,
        )

    class W:
        pass

    w = W()
    w.set_param, w.set_error, w.run = set_param, set_error, run
    w.log, w.out = log, out
    return w


def _crm_calls(log: Path) -> list[str]:
    return [line for line in log.read_text().splitlines() if "connect-crm-config" in line]


class TestWrapperReadsTheCrmConfig:
    def test_config_in_ssm_reaches_the_command(self, world) -> None:
        world.set_param(PARAM, CONFIG)
        r = world.run()
        assert r.returncode == 0, r.stderr
        assert world.out.read_text() == CONFIG
        calls = _crm_calls(world.log)
        assert len(calls) == 1, calls
        assert f"--name {PARAM}" in calls[0] and "--region us-west-2" in calls[0]

    def test_explicit_export_wins_and_ssm_is_not_read(self, world) -> None:
        world.set_param(PARAM, CONFIG)
        r = world.run({"CVX_CONNECT_CRM_CONFIG": '{"preset": true}'})
        assert r.returncode == 0, r.stderr
        assert world.out.read_text() == '{"preset": true}'
        assert _crm_calls(world.log) == []

    def test_reads_the_stage_and_region_it_is_given(self, world) -> None:
        # Pins that the read follows the arguments: a hardcoded stage or region would hand
        # one stage's config (and the IAM it grants) to another.
        world.set_param(PARAM, CONFIG)
        world.set_param("/cvx/dev/connect-crm-config", '{"dev": true}')
        r = world.run(stage="dev", region="eu-west-1")
        assert r.returncode == 0, r.stderr
        assert world.out.read_text() == '{"dev": true}'
        calls = _crm_calls(world.log)
        assert len(calls) == 1, calls
        assert "--name /cvx/dev/connect-crm-config" in calls[0] and "--region eu-west-1" in calls[0]

    def test_whitespace_only_export_counts_as_unset(self, world) -> None:
        # bin/vsa.ts trims the value, so "   " would mean off without a word.
        world.set_param(PARAM, CONFIG)
        r = world.run({"CVX_CONNECT_CRM_CONFIG": "  \t "})
        assert r.returncode == 0, r.stderr
        assert world.out.read_text() == CONFIG
        assert len(_crm_calls(world.log)) == 1

    def test_absent_parameter_leaves_the_feature_off_and_says_so(self, world) -> None:
        r = world.run()
        assert r.returncode == 0, r.stderr
        assert world.out.read_text() == "__UNSET__"
        assert PARAM in r.stderr
        assert "off" in r.stderr.lower()

    @pytest.mark.parametrize("stderr_text", [
        "\nAn error occurred (AccessDeniedException) when calling the GetParameter operation: "
        "User: arn:aws:iam::111122223333:user/x is not authorized to perform: ssm:GetParameter\n",
        "\nUnable to locate credentials. You can configure credentials by running \"aws configure\".\n",
        "\nAn error occurred (ThrottlingException) when calling the GetParameter operation: Rate exceeded\n",
    ], ids=["access-denied", "no-credentials", "throttled"])
    def test_unreadable_parameter_refuses_and_the_command_does_not_run(self, world, stderr_text) -> None:
        world.set_error(PARAM, stderr_text)
        r = world.run()
        assert r.returncode != 0
        assert not world.out.exists(), "the deploy command ran without the CRM config"
        assert PARAM in r.stderr
        assert "CVX_CONNECT_CRM_CONFIG" in r.stderr

    @pytest.mark.parametrize("value", ["None", ""], ids=["None", "empty"])
    def test_empty_or_none_value_refuses(self, world, value) -> None:
        world.set_param(PARAM, value)
        r = world.run()
        assert r.returncode != 0
        assert not world.out.exists()
        assert PARAM in r.stderr

    def test_the_value_is_never_printed(self, world) -> None:
        world.set_param(PARAM, CONFIG)
        r = world.run()
        assert r.returncode == 0, r.stderr
        assert MARKER not in r.stdout and MARKER not in r.stderr

    def test_refusal_does_not_echo_the_aws_error_text(self, world) -> None:
        # The CLI's message carries the caller ARN (an account id); only the error code is shown.
        world.set_error(PARAM, "\nAn error occurred (AccessDeniedException) when calling the GetParameter "
                               "operation: User: arn:aws:iam::111122223333:user/" + MARKER + " is not authorized\n")
        r = world.run()
        assert r.returncode != 0
        assert "AccessDeniedException" in r.stderr
        assert MARKER not in r.stderr and "111122223333" not in r.stderr


def _recipe(target: str) -> list[str]:
    """The recipe lines of a Makefile target, tab prefix removed."""
    lines = MAKEFILE.read_text().splitlines()
    start = lines.index(f"{target}:")
    out = []
    for line in lines[start + 1:]:
        if line.startswith("\t"):
            out.append(line[1:])
        elif line.strip() == "":
            continue
        else:
            break
    return out


class TestDeployStagingStagesEveryBundle:
    def test_every_build_script_runs_before_cdk_deploy(self) -> None:
        scripts = sorted(p.name for p in (REPO_ROOT / "scripts").glob("build-*.sh"))
        assert len(scripts) >= 5, scripts
        recipe = _recipe("deploy-staging")
        deploy_at = next(i for i, line in enumerate(recipe) if line.startswith("$(MAKE) _deploy-cdk "))
        for name in scripts:
            # The exact command, so a commented-out (`@# bash ...`) or echoed line doesn't count.
            at = [i for i, line in enumerate(recipe) if line == f"bash scripts/{name}"]
            assert at, f"make deploy-staging never runs scripts/{name}"
            assert at[0] < deploy_at, f"scripts/{name} runs after cdk deploy"

    def test_cdk_deploy_runs_through_the_stage_wrapper(self) -> None:
        recipe = _recipe("_deploy-cdk")
        deploy = [i for i, line in enumerate(recipe) if "npm run deploy" in line]
        assert len(deploy) == 1, deploy
        # The wrapper call is continued onto the deploy line with a backslash.
        prev = recipe[deploy[0] - 1]
        assert prev.startswith("cd infrastructure && ../scripts/with-stage-env.sh $(STAGE) $(AWS_REGION) --"), prev
        assert prev.endswith("\\"), prev
