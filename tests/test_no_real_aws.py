"""Proof that the repo-root conftest blocks real AWS (user direction 2026-10-02).

Each test asserts one property of the guard in `conftest.py`:
- the guard is installed (checked at import, so a missing conftest fails collection
  instead of letting the probes below run with real credentials);
- every credential source is replaced or disabled;
- real boto3 client and resource calls, and HTTP to an *.amazonaws.com host, raise
  RealAwsCallBlocked;
- a test that SWALLOWS the blocked error still fails, through botocore or requests,
  when marked xfail, when the attempt is in a module-scoped fixture, and when it is
  at import time (the session fails);
- LIVE_TESTS=1 alone lifts nothing; only a `live_aws` test under LIVE_TESTS=1 gets
  the real environment back, and only while it runs;
- botocore's Stubber and loopback endpoints keep working.

The probe tables below are names no stage uses, so even a broken guard could not touch
a real table. Mutation results are in spec 2026-10-02-fi-action-queue-stage-a
decisions.md.
"""
from __future__ import annotations

import os
from pathlib import Path

import boto3
import botocore.endpoint
import pytest
import requests
import requests.adapters
from botocore.stub import Stubber

# Collection-time check (review FG4 W1): without the guard, nothing below may run.
if not (getattr(botocore.endpoint.Endpoint._send, "aws_guard", False)
        and getattr(requests.adapters.HTTPAdapter.send, "aws_guard", False)):
    raise RuntimeError(
        "the repo-root conftest AWS guard is not installed; refusing to run AWS probes"
    )

PROBE_TABLE = "fi-stage-a-guard-probe-no-such-table"
#: botocore probes go to a reserved .invalid host (review Cycle 6a): the botocore guard
#: blocks every non-loopback host, so the proof is as strong, and a broken guard sends
#: nothing anywhere (DNS fails).
PROBE_ENDPOINT = "https://guard-probe.invalid"
_ROOT_CONFTEST = Path(__file__).resolve().parents[1] / "conftest.py"


def _blocked(exc_info) -> bool:
    return type(exc_info.value).__name__ == "RealAwsCallBlocked"


def _probe_config():
    from botocore.config import Config

    return Config(retries={"max_attempts": 0}, connect_timeout=1)


def test_credentials_are_the_fake_ones():
    creds = boto3.Session().get_credentials()
    assert creds is not None and creds.access_key == "testing"
    assert os.environ["AWS_CONFIG_FILE"] == os.devnull
    assert os.environ["AWS_SHARED_CREDENTIALS_FILE"] == os.devnull
    assert os.environ["AWS_EC2_METADATA_DISABLED"] == "true"
    for var in ("AWS_PROFILE", "AWS_DEFAULT_PROFILE", "AWS_ROLE_ARN",
                "AWS_WEB_IDENTITY_TOKEN_FILE", "AWS_CONTAINER_CREDENTIALS_FULL_URI"):
        assert var not in os.environ, var


def test_placeholder_account_stops_the_sts_fallback():
    assert os.environ["VSA_ADP_ACCOUNT_ID"] == "123456789012"


@pytest.mark.provokes_aws_attempt
def test_real_boto3_call_is_blocked():
    client = boto3.client("dynamodb", region_name="us-west-2", endpoint_url=PROBE_ENDPOINT,
                          config=_probe_config())
    with pytest.raises(Exception) as exc_info:
        client.get_item(TableName=PROBE_TABLE, Key={"k": {"S": "probe"}})
    assert _blocked(exc_info)


@pytest.mark.provokes_aws_attempt
def test_real_boto3_resource_call_is_blocked():
    table = boto3.resource("dynamodb", region_name="us-west-2", endpoint_url=PROBE_ENDPOINT,
                           config=_probe_config()).Table(PROBE_TABLE)
    with pytest.raises(Exception) as exc_info:
        table.put_item(Item={"k": "probe"})
    assert _blocked(exc_info)


@pytest.mark.provokes_aws_attempt
@pytest.mark.parametrize("host", ["no-such-api.execute-api.us-west-2.amazonaws.com",
                                  "no-such-api.execute-api.cn-north-1.amazonaws.com.cn"])
def test_http_to_an_aws_host_is_blocked(host):
    """The requests guard matches AWS host suffixes, so this probe must name one; it is
    unsigned and the API id does not exist."""
    with pytest.raises(Exception) as exc_info:
        requests.get(f"https://{host}/probe", timeout=1)
    assert _blocked(exc_info)


def test_stubber_still_answers():
    client = boto3.client("dynamodb", region_name="us-west-2")
    with Stubber(client) as stub:
        stub.add_response("list_tables", {"TableNames": ["vsa-stub-table"]}, {})
        assert client.list_tables()["TableNames"] == ["vsa-stub-table"]


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "[::1]"])
def test_loopback_endpoint_is_not_blocked(host):
    import botocore.exceptions
    from botocore.config import Config

    client = boto3.client("dynamodb", region_name="us-west-2",
                          endpoint_url=f"http://{host}:9",  # discard port: refused
                          config=Config(retries={"max_attempts": 0}, connect_timeout=1))
    with pytest.raises(botocore.exceptions.EndpointConnectionError):
        client.list_tables()


# ---------------------------------------------------------------------------
# Swallowed attempts still fail (pytester runs each case in a subprocess with a copy of
# the real conftest).
# ---------------------------------------------------------------------------

_SWALLOW_BOTO = """
import boto3
from botocore.config import Config
def _attempt():
    try:
        boto3.client("sts", region_name="us-west-2",
                     endpoint_url="https://guard-probe.invalid",
                     config=Config(retries={"max_attempts": 0}, connect_timeout=1),
                     ).get_caller_identity()
    except Exception:
        pass
"""


def _run(pytester, test_src: str, *, env: dict | None = None):
    pytester.makeconftest(_ROOT_CONFTEST.read_text())
    pytester.makepyfile(test_case=test_src)
    for key, value in (env or {}).items():
        pytester._monkeypatch.setenv(key, value)
    return pytester.runpytest_subprocess("-p", "no:cacheprovider")


def test_swallowed_botocore_attempt_fails_the_test(pytester):
    result = _run(pytester, _SWALLOW_BOTO + "\ndef test_it():\n    _attempt()\n")
    result.assert_outcomes(failed=1)
    result.stdout.fnmatch_lines(["*attempted real AWS during call*guard-probe.invalid*"])


def test_swallowed_requests_attempt_fails_the_test(pytester):
    result = _run(pytester, """
import requests
def test_it():
    try:
        requests.get("https://no-such-api.execute-api.us-west-2.amazonaws.com/x", timeout=1)
    except Exception:
        pass
""")
    result.assert_outcomes(failed=1)
    result.stdout.fnmatch_lines(["*attempted real AWS during call*execute-api*"])


def test_xfail_does_not_hide_an_attempt(pytester):
    result = _run(pytester, _SWALLOW_BOTO + """
import pytest
@pytest.mark.xfail(reason="would otherwise hide the attempt")
def test_it():
    _attempt()
    assert False
""")
    result.assert_outcomes(failed=1)
    # The exit status too: pytest's session counts a failed report as a failure only if
    # it no longer carries `wasxfail` (Fix Group 5 mutation G9 survived without this).
    assert result.ret == pytest.ExitCode.TESTS_FAILED


def test_module_scoped_fixture_attempt_fails(pytester):
    result = _run(pytester, _SWALLOW_BOTO + """
import pytest
@pytest.fixture(scope="module")
def warm():
    _attempt()
def test_it(warm):
    pass
""")
    outcomes = result.parseoutcomes()
    assert outcomes.get("passed", 0) == 0
    assert outcomes.get("failed", 0) + outcomes.get("errors", outcomes.get("error", 0)) >= 1
    result.stdout.fnmatch_lines(["*attempted real AWS during setup*"])


def test_teardown_attempt_fails_the_test(pytester):
    """Review Cycle 6a W2: a swallowed attempt in a fixture's teardown still fails."""
    result = _run(pytester, _SWALLOW_BOTO + """
import pytest
@pytest.fixture
def leaves_a_mess():
    yield
    _attempt()
def test_it(leaves_a_mess):
    pass
""")
    result.stdout.fnmatch_lines(["*attempted real AWS during teardown*"])
    assert result.ret == pytest.ExitCode.TESTS_FAILED


def test_every_dropped_credential_source_is_gone(pytester):
    """Each of the eight variables the guard drops, set in the environment, is absent
    inside a test (review Cycle 6a: single drop-list entries were unpinned)."""
    dropped = ("AWS_PROFILE", "AWS_DEFAULT_PROFILE", "AWS_ROLE_ARN", "AWS_ROLE_SESSION_NAME",
               "AWS_WEB_IDENTITY_TOKEN_FILE", "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI",
               "AWS_CONTAINER_CREDENTIALS_FULL_URI", "AWS_CONTAINER_AUTHORIZATION_TOKEN")
    result = _run(pytester, f"""
import os
def test_dropped():
    present = [v for v in {dropped!r} if v in os.environ]
    assert present == [], present
""", env={var: "sentinel-" + var.lower() for var in dropped})
    result.assert_outcomes(passed=1)


def test_import_time_attempt_fails_the_session(pytester):
    result = _run(pytester, _SWALLOW_BOTO + "\n_attempt()\n\ndef test_it():\n    pass\n")
    assert result.ret != 0
    result.stdout.fnmatch_lines(["*real AWS attempted outside any test*", "*guard-probe.invalid*"])


# ---------------------------------------------------------------------------
# LIVE_TESTS=1 lifts the block only for a `live_aws` test, and only while it runs.
# ---------------------------------------------------------------------------

def test_live_flag_alone_lifts_nothing(pytester):
    result = _run(pytester, _SWALLOW_BOTO + "\ndef test_unit():\n    _attempt()\n",
                  env={"LIVE_TESTS": "1"})
    result.assert_outcomes(failed=1)


def test_live_marker_without_the_flag_is_still_blocked(pytester):
    result = _run(pytester, _SWALLOW_BOTO + """
import pytest
@pytest.mark.live_aws
def test_live():
    _attempt()
""")
    result.assert_outcomes(failed=1)


def test_live_test_gets_the_real_env_back_only_while_it_runs(pytester):
    result = _run(pytester, """
import os
import pytest

@pytest.mark.live_aws
def test_1_live():
    assert os.environ.get("AWS_PROFILE") == "sentinel-profile"
    assert os.environ.get("AWS_ACCESS_KEY_ID") != "testing"

def test_2_unit_after():
    assert "AWS_PROFILE" not in os.environ
    assert os.environ.get("AWS_ACCESS_KEY_ID") == "testing"

@pytest.mark.provokes_aws_attempt
def test_3_unit_after_is_still_blocked():
    # Fix Group 5 mutation G10: a live pass left open after the live test let later
    # unit tests through. The probe host is .invalid, so even a broken guard sends
    # nothing to AWS: it fails here on DNS instead of on the guard.
    import boto3
    from botocore.config import Config
    client = boto3.client("sts", region_name="us-west-2",
                          endpoint_url="https://guard-probe.invalid",
                          config=Config(retries={"max_attempts": 0}, connect_timeout=1))
    with pytest.raises(Exception) as exc_info:
        client.get_caller_identity()
    assert type(exc_info.value).__name__ == "RealAwsCallBlocked", type(exc_info.value)
""", env={"LIVE_TESTS": "1", "AWS_PROFILE": "sentinel-profile",
          "AWS_ACCESS_KEY_ID": "sentinel-key-id"})
    result.assert_outcomes(passed=3)


def test_live_credentials_do_not_cross_the_boundary(pytester):
    """Review Cycle 6a W1: boto3's default session caches the credentials it resolved, so
    os.environ is only a proxy. Check what a client would actually sign with, before,
    during and after a live test, and that the live pass really opens the transport."""
    result = _run(pytester, """
import boto3
import pytest
from botocore.config import Config

def _key():
    return boto3._get_default_session().get_credentials().access_key

def test_0_unit_before():
    assert _key() == "testing"

@pytest.mark.live_aws
def test_1_live():
    assert _key() == "sentinel-key-id"
    # The request reaches DNS for a .invalid host and fails there, not on the guard.
    client = boto3.client("sts", region_name="us-west-2",
                          endpoint_url="https://guard-probe.invalid",
                          config=Config(retries={"max_attempts": 0}, connect_timeout=1))
    with pytest.raises(Exception) as exc_info:
        client.get_caller_identity()
    assert type(exc_info.value).__name__ != "RealAwsCallBlocked", exc_info.value

def test_2_unit_after():
    assert _key() == "testing"
""", env={"LIVE_TESTS": "1", "AWS_ACCESS_KEY_ID": "sentinel-key-id"})
    result.assert_outcomes(passed=3)



def test_a_nested_conftest_is_guarded_at_import(pytester):
    """Review Cycle 5a S3 / 6a: a nested conftest named on the command line is imported
    before pytest_configure. The guard is installed when the root conftest is imported,
    so that conftest's import-time attempt is blocked and fails the session."""
    pytester.makeconftest(_ROOT_CONFTEST.read_text())
    sub = pytester.mkpydir("sub")
    (sub / "conftest.py").write_text(_SWALLOW_BOTO + "\n_attempt()\n")
    (sub / "test_case.py").write_text("def test_it():\n    pass\n")
    result = pytester.runpytest_subprocess("-p", "no:cacheprovider", "sub")
    assert result.ret != 0
    result.stdout.fnmatch_lines(["*real AWS attempted outside any test*", "*guard-probe.invalid*"])
