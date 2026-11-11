"""conftest.py — repo root.

Under ``--import-mode=importlib`` (set globally in ``pytest.ini`` as of the
2026-09-18 Group 1 tripwire wire-up) Python's ``sys.modules`` cache is shared
across all test files, but each test module is loaded with its own namespace.

Multiple lambdas under ``lambdas/<name>/`` define a top-level ``handler``
module. So do a handful of tests OUTSIDE ``lambdas/`` — most notably
``agents/supervisor/tests/test_fleet_manager_persona.py`` — which prepend a
specific lambda root to ``sys.path`` and then ``import handler as h``.

When a lambdas-tree test runs first, it seats *its* ``handler`` in
``sys.modules["handler"]``. The subsequent agents-tree test then does
``sys.path.insert(0, <different lambda root>)`` and ``import handler`` — and
gets the *previous* handler from the cache, not a fresh import from the newly
prepended path. The failure mode: an ``AttributeError`` naming a symbol that
lives on the intended handler but not on the cached one.

The ``lambdas/conftest.py`` fix for this was written in Fix Group 4 and works
correctly for tests *inside* ``lambdas/`` (its ``pytest_runtest_setup`` hook
fires only on collection items under that directory). Review Cycle 2 W2
identified the residue: agents-tree tests that import a lambda ``handler`` are
not covered by that conftest and hit the exact bug it was written to prevent.
Reproduced: ``test_fleet_manager_persona_requestable`` passes in isolation,
fails order-dependent in the full suite.

This repo-root hook covers them. It does ONLY the eviction — no ``sys.path``
manipulation, because tests outside ``lambdas/`` manage their own path (see
``agents/supervisor/tests/test_fleet_manager_persona.py:16-21``). The eviction
is idempotent, so ``lambdas/conftest.py`` re-evicting on lambdas-tree tests is
a harmless no-op.
"""

from __future__ import annotations

import os
import sys

import pytest


# ── Fail closed against real AWS ──────────────────────────────────────────────
#
# User direction 2026-10-02 (spec 2026-10-02-fi-action-queue-stage-a): a unit test
# that reaches AWS must error out, never act with the developer's credentials. A
# review run of a mutated handler had made a live DynamoDB PutItem from a decline
# test using the shell's credentials (it failed only because the default-stage table
# did not exist).
#
# Installed ALWAYS, whatever LIVE_TESTS says (see "Installed when this conftest is
# imported" below). Two layers:
#   1. Credentials. The credential environment variables are replaced with fake
#      values, the config and credentials files point at /dev/null, and IMDS is off.
#      A request that escaped would be signed with junk, not with your identity
#      (except by a client a live test cached; see below).
#   2. Transport. botocore's Endpoint._send, and requests to *.amazonaws.com, raise
#      RealAwsCallBlocked. Loopback endpoints (a fake server a test starts itself) pass.
#      botocore's Stubber answers in the before-call hook, ahead of _send, so stubbed
#      clients keep working.
#
# Every blocked attempt is recorded against the test phase that made it, and
# pytest_runtest_makereport turns that phase into a FAILURE even when the code under
# test swallowed the exception and even when the test is marked xfail. Attempts made
# outside any test (at collection or import time) fail the session at the end.
#
# The only way through is a test marked `live_aws`, run with LIVE_TESTS=1. For that
# one test the real environment captured at startup is restored and the transport
# lets requests through; both are re-blocked when it finishes. LIVE_TESTS=1 alone
# lifts nothing, so a shell left with it exported cannot run unit tests as you.
# boto3's default session caches the credentials it resolved, so it is discarded when
# the live pass opens and again when it closes (review Cycle 6a W1): a live test signs
# with the restored credentials, and the next unit test with the fake ones. A client
# that code under test caches at module level keeps whatever it resolved; the transport
# block still stops it. Run live tests on their own (`-m live_aws`).
#
# Installed when this conftest is imported, before pytest_configure and before nested
# conftests load, so their import-time code sees the fake values too.
#
# Non-secret placeholders the code would otherwise fetch from AWS are set the same way
# as the fake credentials: VSA_ADP_ACCOUNT_ID, which Config.from_env() otherwise
# resolves with sts:GetCallerIdentity (about 230 agents/supervisor tests made that real
# call before this guard, found 2026-10-02).
#
# Proven by tests/test_no_real_aws.py (in CI).

LIVE_TESTS = os.environ.get("LIVE_TESTS") == "1"

#: AWS documentation placeholder account; never a real account, publish-safe.
PLACEHOLDER_ACCOUNT_ID = "123456789012"

FAKE_ACCESS_KEY_ID = "testing"

_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}

_CREDENTIAL_SOURCES_TO_DROP = (
    "AWS_PROFILE",
    "AWS_DEFAULT_PROFILE",
    "AWS_ROLE_ARN",
    "AWS_ROLE_SESSION_NAME",
    "AWS_WEB_IDENTITY_TOKEN_FILE",
    "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI",
    "AWS_CONTAINER_CREDENTIALS_FULL_URI",
    "AWS_CONTAINER_AUTHORIZATION_TOKEN",
)
_FAKE_ENV = {
    "AWS_ACCESS_KEY_ID": FAKE_ACCESS_KEY_ID,
    "AWS_SECRET_ACCESS_KEY": "testing",
    "AWS_SESSION_TOKEN": "testing",
    "AWS_SECURITY_TOKEN": "testing",
    "AWS_CONFIG_FILE": os.devnull,
    "AWS_SHARED_CREDENTIALS_FILE": os.devnull,
    "AWS_EC2_METADATA_DISABLED": "true",
}
_GUARDED_VARS = tuple(_CREDENTIAL_SOURCES_TO_DROP) + tuple(_FAKE_ENV)

#: The real values at startup, restored only for a `live_aws` test under LIVE_TESTS=1.
_REAL_ENV: dict[str, str | None] = {}

#: True only while a `live_aws` test runs under LIVE_TESTS=1.
_LIVE_PASS = {"active": False}

#: Attempts made by the test phase now running ("setup", "call", "teardown").
BLOCKED_AWS_ATTEMPTS: list[str] = []
#: Attempts made outside any test phase (collection, import time).
_OUTSIDE_ATTEMPTS: list[str] = []
_IN_TEST_PHASE = {"active": False}


class RealAwsCallBlocked(RuntimeError):
    """A unit test tried to send a request to real AWS."""


def _record(attempt: str) -> None:
    (BLOCKED_AWS_ATTEMPTS if _IN_TEST_PHASE["active"] else _OUTSIDE_ATTEMPTS).append(attempt)


def _operation_of(request) -> str:
    """' [Service.Operation table=<name>]' for the blocked-call message. Names only:
    never the request body, which can carry test data."""
    import json

    target = request.headers.get("X-Amz-Target") or b""
    if isinstance(target, bytes):
        target = target.decode("utf-8", "replace")
    table = ""
    try:
        body = request.body
        if isinstance(body, bytes):
            body = body.decode("utf-8", "replace")
        if body and body.lstrip().startswith("{"):
            table = json.loads(body).get("TableName") or ""
    except Exception:  # noqa: BLE001 - message decoration only
        table = ""
    parts = [p for p in (target, f"table={table}" if table else "") if p]
    return f" [{' '.join(parts)}]" if parts else ""


def _apply_fake_env() -> None:
    for var in _CREDENTIAL_SOURCES_TO_DROP:
        os.environ.pop(var, None)
    os.environ.update(_FAKE_ENV)


def _restore_real_env() -> None:
    for var, value in _REAL_ENV.items():
        if value is None:
            os.environ.pop(var, None)
        else:
            os.environ[var] = value


def _reset_boto3_default_session() -> None:
    """Drop boto3's cached default session, so the next client resolves credentials from
    the environment as it is now."""
    try:
        import boto3
    except ImportError:  # pragma: no cover - boto3 is a dev dependency
        return
    boto3.DEFAULT_SESSION = None


_INSTALLED = {"done": False}


def _install_aws_block() -> None:
    if _INSTALLED["done"]:
        return
    _INSTALLED["done"] = True
    _REAL_ENV.update({var: os.environ.get(var) for var in _GUARDED_VARS})
    _apply_fake_env()
    _reset_boto3_default_session()
    os.environ.setdefault("AWS_DEFAULT_REGION", "us-west-2")
    os.environ.setdefault("VSA_ADP_ACCOUNT_ID", PLACEHOLDER_ACCOUNT_ID)

    import botocore.endpoint

    _real_botocore_send = botocore.endpoint.Endpoint._send

    def _refuse_botocore(self, request):
        from urllib.parse import urlparse

        host = (urlparse(request.url).hostname or "").lower()
        if _LIVE_PASS["active"] or host in _LOOPBACK_HOSTS:
            return _real_botocore_send(self, request)
        attempt = f"{request.method} {request.url}{_operation_of(request)}"
        _record(attempt)
        raise RealAwsCallBlocked(
            f"unit test tried to call real AWS: {attempt}. Stub the client (botocore "
            "Stubber or a fake). An opt-in live test needs @pytest.mark.live_aws and "
            "LIVE_TESTS=1."
        )

    _refuse_botocore.aws_guard = True
    botocore.endpoint.Endpoint._send = _refuse_botocore

    import requests.adapters

    _real_send = requests.adapters.HTTPAdapter.send

    def _refuse_aws_hosts(self, request, *args, **kwargs):
        from urllib.parse import urlparse

        host = (urlparse(request.url).hostname or "").lower()
        if not _LIVE_PASS["active"] and (
            host.endswith(".amazonaws.com") or host.endswith(".amazonaws.com.cn")
        ):
            attempt = f"{request.method} {request.url}"
            _record(attempt)
            raise RealAwsCallBlocked(
                f"unit test tried to call a real AWS endpoint over HTTP: {attempt}"
            )
        return _real_send(self, request, *args, **kwargs)

    _refuse_aws_hosts.aws_guard = True
    requests.adapters.HTTPAdapter.send = _refuse_aws_hosts


# At import, not only in pytest_configure (review Cycle 5a S3 / 6a): nested conftests
# and plugins imported before pytest_configure must already see the guard.
_install_aws_block()


pytest_plugins = ["pytester"]


def pytest_configure(config) -> None:
    config.addinivalue_line(
        "markers",
        "provokes_aws_attempt: the test makes a blocked AWS call on purpose "
        "(tests/test_no_real_aws.py); the attempt does not fail it",
    )
    config.addinivalue_line(
        "markers",
        "live_aws: an opt-in live test. Only with LIVE_TESTS=1 are real credentials "
        "and the network restored, and only for the duration of that test",
    )
    _install_aws_block()


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_protocol(item, nextitem):  # noqa: ARG001
    live = LIVE_TESTS and item.get_closest_marker("live_aws") is not None
    if live:
        _restore_real_env()
        _reset_boto3_default_session()
        _LIVE_PASS["active"] = True
    try:
        yield
    finally:
        if live:
            _LIVE_PASS["active"] = False
            _apply_fake_env()
            _reset_boto3_default_session()


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    """Fail the phase that attempted AWS, even if swallowed and even if xfail."""
    outcome = yield
    report = outcome.get_result()
    attempts = list(BLOCKED_AWS_ATTEMPTS)
    BLOCKED_AWS_ATTEMPTS.clear()
    if not attempts or item.get_closest_marker("provokes_aws_attempt"):
        return
    report.outcome = "failed"
    if hasattr(report, "wasxfail"):
        del report.wasxfail
    message = (
        f"test attempted real AWS during {call.when} (blocked): " + "; ".join(attempts[:3])
    )
    # Keep the original failure (and the call site's traceback) when there was one
    # (review Cycle 6a): the fixer needs to see where the call came from.
    original = report.longrepr
    report.longrepr = f"{message}\n\n{original}" if original else message


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_setup(item):
    _IN_TEST_PHASE["active"] = True
    # Evict a possibly-stale ``handler`` module before every test item (cross-tree
    # sys.modules['handler'] pollution, see module docstring; idempotent with the
    # sibling hook at lambdas/conftest.py).
    sys.modules.pop("handler", None)
    yield


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_teardown(item, nextitem):  # noqa: ARG001
    yield
    _IN_TEST_PHASE["active"] = False


def pytest_sessionfinish(session, exitstatus):  # noqa: ARG001
    if _OUTSIDE_ATTEMPTS and session.exitstatus == 0:
        session.exitstatus = pytest.ExitCode.TESTS_FAILED


def pytest_terminal_summary(terminalreporter, exitstatus, config):  # noqa: ARG001
    if _OUTSIDE_ATTEMPTS:
        terminalreporter.section("real AWS attempted outside any test (blocked)")
        for attempt in _OUTSIDE_ATTEMPTS[:10]:
            terminalreporter.line(attempt)
