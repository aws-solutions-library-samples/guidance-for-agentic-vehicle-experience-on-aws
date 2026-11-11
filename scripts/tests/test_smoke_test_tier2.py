"""Pins for scripts/smoke-test-tier2.py: how long it waits for an invoke, how many times
it sends one, and which components it invokes.

Issue: issues/2026-10-18-tier2-smoke-test-retries-long-invokes/. On 2026-10-18 one smoke
run started five overlapping brakes runs: the Lambda client used botocore's default 60 s
read timeout and default retries (legacy mode: five attempts), a brakes run takes about
6.5 minutes, and each re-sent synchronous Invoke is a new run. The cursor check also
invoked every component whatever ``--component`` said.

The script's real ``main()`` runs here with its real botocore Lambda client (the one
built from ``_lambda_config()``). A ``before-send`` hook on that client stands in for
the HTTP layer, so retries and timeouts go through botocore's own retry handler: a
re-send shows up as a second send. The other clients (DynamoDB, Logs, CloudWatch, SNS,
STS) are small fakes. Nothing here reaches AWS.

Deliberate breaks (runner: records/run_mutants.py; the failing test ids for each break are in
records/mutants-<sha>.json, both under issues/2026-10-18-tier2-smoke-test-retries-long-invokes/).
Each fails at least one test on its own assertion:
  B0  the script as it was on main before this fix
  B1  drop ``config=_lambda_config()`` from the Lambda client
  B2  ``retries={"total_max_attempts": 2}``
  B3  ``retries={"max_attempts": 1}`` (counts retries, so two sends)
  B4  ``_LAMBDA_READ_TIMEOUT_S = 600``
  B5  the cursor check invokes ``COMPONENTS`` instead of the components it was given
  B6  the cursor check reads cursors for ``COMPONENTS``
  B7  ``main`` passes ``COMPONENTS`` to the cursor check
  B8  the cursor check runs under ``--no-invoke``
  B9  ``COMPONENTS`` derived from ``VALID_COMPONENTS - {"cost"}`` again
  B10 ``--component`` ignored (``components = COMPONENTS``)
  N1  the cursor check gets its own default Lambda client
  N2  the cursor check re-invokes after an exception
  N3  ``--no-invoke`` ignored under ``--no-infra-checks``
  N8  the log check runs under ``--no-invoke``
  R2  the cursor check moved after the artifact checks, plus N2
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import sys
from typing import Any

import boto3
import pytest
from botocore.awsrequest import AWSResponse
from botocore.exceptions import ReadTimeoutError
from urllib3.response import HTTPResponse

_REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
_SCRIPT = os.path.join(_REPO, "scripts", "smoke-test-tier2.py")

#: Lambda's maximum function timeout; the Tier 2 health agent is deployed with it.
_LAMBDA_MAX_TIMEOUT_S = 900

_HEALTH = ["battery", "brakes", "tires"]


def _load(name: str = "smoke_test_tier2_under_test") -> Any:
    spec = importlib.util.spec_from_file_location(name, _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(autouse=True)
def _offline_aws(monkeypatch):
    """Dummy credentials, no profile, no config file: a real client can be built, and
    nothing in the environment changes its retry policy unless a test sets it."""
    for var in (
        "AWS_PROFILE",
        "AWS_DEFAULT_PROFILE",
        "AWS_SESSION_TOKEN",
        "AWS_SECURITY_TOKEN",
        "AWS_RETRY_MODE",
        "AWS_MAX_ATTEMPTS",
        "AWS_ENDPOINT_URL",
        "AWS_ENDPOINT_URL_LAMBDA",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    monkeypatch.setenv("AWS_CONFIG_FILE", os.devnull)
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", os.devnull)


class _World:
    """Fake AWS behind the script. Lambda is a real botocore client with its HTTP send
    replaced; every send (first attempt or retry) is recorded in ``sends``."""

    def __init__(self, *, invoke_times_out: bool = False) -> None:
        self.invoke_times_out = invoke_times_out
        self.sends: list[str] = []          # component of every Invoke HTTP send
        self.cursor_reads: list[str] = []   # component of every cursor GetItem
        self.log_reads = 0                  # FilterLogEvents calls
        self.lambda_clients: list[Any] = []
        self._real_client = boto3.session.Session().client

    # ── boto3.client replacement ─────────────────────────────────────────────
    def client(self, service: str, **kwargs: Any) -> Any:
        if service == "lambda":
            real = self._real_client("lambda", **kwargs)
            real.meta.events.register("before-send.lambda.Invoke", self._on_send)
            self.lambda_clients.append(real)
            return real
        return {
            "dynamodb": _FakeDdb(self),
            "logs": _FakeLogs(self),
            "cloudwatch": _FakeCloudWatch(),
            "sns": _FakeSns(),
            "sts": _FakeSts(),
        }[service]

    def _on_send(self, request: Any, **_: Any) -> AWSResponse:
        component = json.loads(request.body)["component"]
        self.sends.append(component)
        if self.invoke_times_out:
            raise ReadTimeoutError(endpoint_url=request.url)
        body = json.dumps(
            {
                "status": "completed",
                "rows_read": 10,
                "artifacts_written": 1,
                "duplicates_skipped": 0,
                "tokens_used": 0,
                "abort_reason": None,
            }
        ).encode()
        raw = HTTPResponse(body=io.BytesIO(body), status=200, headers={}, preload_content=False)
        return AWSResponse(request.url, 200, {}, raw)

    def invokes_of(self, component: str) -> int:
        return self.sends.count(component)


class _FakeDdb:
    def __init__(self, world: _World) -> None:
        self._world = world

    def get_item(self, TableName: str, Key: dict) -> dict:
        component = Key["pk"]["S"].split("#", 1)[1]
        self._world.cursor_reads.append(component)
        # The cursor moves with each invoke of its component, as selection does.
        n = self._world.invokes_of(component)
        return {"Item": {"last_vin": {"S": f"CURSOR-{component}-{n}"}}}

    def scan(self, TableName: str, FilterExpression: str, ExpressionAttributeValues: dict, Limit: int) -> dict:
        prefix = ExpressionAttributeValues[":p"]["S"]
        inputs_hash = "a" * 64
        artifact = {
            "pk": {"S": "VEH#placeholder"},
            "sk": {"S": f"{prefix}{inputs_hash}"},
            "computed_at": {"S": "2026-10-23T00:00:00Z"},
            "confidence": {"N": "0.9"},
            "evidence": {"L": [{"M": {"source": {"S": "adp"}, "value": {"S": "1"}, "provenance": {"S": "p"}}}]},
            "agent_version": {"S": "test"},
            "inputs_hash": {"S": inputs_hash},
            "finding": {"S": "ok"},
            "severity_hint": {"S": "info"},
            "ttl": {"N": "1"},
        }
        return {"Items": [artifact]}


class _FakeLogs:
    def __init__(self, world: _World) -> None:
        self._world = world

    def filter_log_events(self, **_: Any) -> dict:
        self._world.log_reads += 1
        return {"events": []}


class _FakeCloudWatch:
    def describe_alarms(self, AlarmNames: list[str]) -> dict:
        return {"MetricAlarms": [{"AlarmName": n, "AlarmActions": ["arn:topic"]} for n in AlarmNames]}

    def get_metric_statistics(self, **_: Any) -> dict:
        return {"Datapoints": [{"Sum": 1.0}]}


class _FakeSns:
    def get_topic_attributes(self, TopicArn: str) -> dict:
        return {"Attributes": {}}


class _FakeSts:
    def get_caller_identity(self) -> dict:
        return {"Account": "123456789012"}


def _run(monkeypatch, world: _World, *flags: str) -> int:
    smoke = _load()
    monkeypatch.setattr(smoke.boto3, "client", world.client)
    monkeypatch.setattr(smoke.time, "sleep", lambda _s: None)
    monkeypatch.setattr(sys, "argv", ["smoke-test-tier2.py", "--stage", "staging", *flags])
    return smoke.main()


# ── timeout and retries ──────────────────────────────────────────────────────


def test_the_lambda_client_waits_past_the_lambda_timeout_with_one_attempt(monkeypatch):
    """Infra checks on, so the cursor check's invokes run too: they must go through the
    same configured client, since that path re-sent brakes five times on 2026-10-18."""
    world = _World()
    assert _run(monkeypatch, world, "--component", "battery") == 0
    assert world.sends == ["battery", "battery"]     # cursor check, then artifact check
    (client,) = world.lambda_clients                 # one client serves both paths
    config = client.meta.config
    assert config.read_timeout > _LAMBDA_MAX_TIMEOUT_S, config.read_timeout
    # total_max_attempts counts the first request; max_attempts counts retries.
    assert config.retries.get("total_max_attempts") == 1, config.retries
    assert "max_attempts" not in config.retries, config.retries


@pytest.mark.parametrize("infra_flags", [[], ["--no-infra-checks"]], ids=["infra-checks", "no-infra-checks"])
@pytest.mark.parametrize(
    "env",
    [{}, {"AWS_MAX_ATTEMPTS": "5"}, {"AWS_RETRY_MODE": "standard"}, {"AWS_RETRY_MODE": "adaptive"}],
    ids=["no-env", "AWS_MAX_ATTEMPTS=5", "standard-mode", "adaptive-mode"],
)
def test_a_timed_out_invoke_is_sent_once(monkeypatch, env, infra_flags):
    """A read timeout must end the run, not start another one: each re-send of a
    synchronous Invoke is a new Lambda execution. The operator's retry env must not
    change that either. With infra checks on, the first invoke is the cursor check's."""
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    world = _World(invoke_times_out=True)
    with pytest.raises(ReadTimeoutError):
        _run(monkeypatch, world, "--component", "brakes", *infra_flags)
    assert world.sends == ["brakes"]
    # With infra checks on, the timed-out invoke must be the cursor check's (it reads the
    # cursor first); a reorder that put the artifact invoke first would leave this empty.
    assert world.cursor_reads == (["brakes"] if not infra_flags else []), world.cursor_reads


def test_the_world_would_see_a_retry(monkeypatch):
    """Anti-vacuity for the test above: a client built WITHOUT the script's config,
    through the same hook, sends a timed-out invoke more than once."""
    world = _World(invoke_times_out=True)
    monkeypatch.setattr("time.sleep", lambda _s: None)   # skip botocore's retry backoff
    client = world.client("lambda", region_name="us-west-2")
    with pytest.raises(ReadTimeoutError):
        client.invoke(FunctionName="f", Payload=json.dumps({"component": "brakes"}).encode())
    assert len(world.sends) > 1, world.sends


# ── which components are invoked ─────────────────────────────────────────────


@pytest.mark.parametrize("component", _HEALTH)
def test_component_limits_every_invoke(monkeypatch, component):
    world = _World()
    assert _run(monkeypatch, world, "--component", component) == 0
    # Once for the cursor check, once before the artifact check; nothing else.
    assert world.sends == [component, component], world.sends
    assert set(world.cursor_reads) == {component}, world.cursor_reads


def test_all_invokes_exactly_the_three_health_components(monkeypatch):
    world = _World()
    assert _run(monkeypatch, world) == 0
    assert sorted(world.sends) == sorted(_HEALTH * 2), world.sends
    assert set(world.cursor_reads) == set(_HEALTH), world.cursor_reads


@pytest.mark.parametrize("extra", [[], ["--no-infra-checks"], ["--component", "brakes"]],
                         ids=["default", "no-infra-checks", "component-brakes"])
def test_no_invoke_sends_nothing(monkeypatch, extra):
    """--no-invoke means no invoke; the cursor check, which needs one, is skipped, and so
    is the log check, which reads the invokes' output."""
    world = _World()
    assert _run(monkeypatch, world, "--no-invoke", *extra) == 0
    assert world.sends == []
    assert world.log_reads == 0


def test_the_log_check_runs_when_invoking(monkeypatch):
    """Anti-vacuity for the log assertion above."""
    world = _World()
    assert _run(monkeypatch, world, "--component", "battery", "--no-infra-checks") == 0
    assert world.log_reads == 1


def test_no_infra_checks_invokes_each_named_component_once(monkeypatch):
    world = _World()
    assert _run(monkeypatch, world, "--component", "tires", "--no-infra-checks") == 0
    assert world.sends == ["tires"]


def test_components_are_the_three_health_components_whatever_sys_path_holds(monkeypatch):
    """With the repo root importable (pytest, or PYTHONPATH=.) and without it (a plain
    operator shell), the list is the same three components."""
    from agents.tier2.contract import VALID_COMPONENTS

    assert set(_HEALTH) <= VALID_COMPONENTS
    assert _load("smoke_with_repo_root").COMPONENTS == _HEALTH

    monkeypatch.setitem(sys.modules, "agents", None)   # `import agents...` now raises
    monkeypatch.setitem(sys.modules, "agents.tier2", None)
    monkeypatch.setitem(sys.modules, "agents.tier2.contract", None)
    assert _load("smoke_without_repo_root").COMPONENTS == _HEALTH
