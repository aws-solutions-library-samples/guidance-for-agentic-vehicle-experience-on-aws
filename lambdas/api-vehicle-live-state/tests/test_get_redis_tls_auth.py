"""Tests for the optional TLS + AUTH toggles on the CVX Redis client (2026-09-16).

Ported from the DMS sibling test suite verbatim in intent — the two accelerators share
the same threat model and the same fail-open discipline, and both must satisfy the same
mutation-testing bar from ``~/.kiro/steering/testing.md`` § "Mutation testing at the
green boundary".

The class of defect these tests guard against
---------------------------------------------
A ``bool(os.environ.get("REDIS_TLS_ENABLED"))`` predicate reads any non-empty string as
True and silently enables TLS on a typo like ``"ture"``; a ``password=`` field with a
fallback to plaintext on secret-fetch failure connects unauthenticated when the operator
signaled they wanted AUTH. Both are unit-testable and neither would show up in a
stubbed-client happy-path suite. Each critical assertion below is paired inline with the
mutation the assertion is designed to catch.
"""

from __future__ import annotations

import os
import sys
from unittest.mock import MagicMock, patch

import pytest

# Allow direct import from the lambda root without a package install (CVX convention).
_LAMBDA_ROOT = os.path.join(os.path.dirname(__file__), "..")
if _LAMBDA_ROOT not in sys.path:
    sys.path.insert(0, _LAMBDA_ROOT)

# The handler module reads env vars at import time (`REDIS_ENDPOINT`, `CMS_VEHICLES_TABLE`).
# Set them BEFORE the import so the module doesn't crash on load. We patch REDIS_ENDPOINT
# per-test to point at a fake value; the module-level read is only used as a default the
# tests override via `patch.object`.
os.environ.setdefault("REDIS_ENDPOINT", "cache.example.internal")
os.environ.setdefault("REDIS_PORT", "6379")
os.environ.setdefault("CMS_VEHICLES_TABLE", "vsa-test-vehicles")

import handler  # noqa: E402 — must follow the env-var setup above


@pytest.fixture(autouse=True)
def _reset_module_state():
    """Clear the module-level client between tests + purge the two env vars.

    Without env-var purging, a test that sets ``REDIS_TLS_ENABLED=true`` and forgets to
    unset it leaves the next test's default-case assertion asserting against a TLS-on
    kwargs dict — the exact green-suite-lies-about-behaviour trap the steering flags.
    """
    handler._redis_client = None
    for var in ("REDIS_TLS_ENABLED", "REDIS_AUTH_TOKEN_SECRET_ARN"):
        os.environ.pop(var, None)
    yield
    handler._redis_client = None
    for var in ("REDIS_TLS_ENABLED", "REDIS_AUTH_TOKEN_SECRET_ARN"):
        os.environ.pop(var, None)


# ─────────────────────────── TLS toggle ───────────────────────────


class TestTlsToggleDefaults:
    """The default (env var absent or empty) MUST NOT change the constructor kwargs.

    Mutation guard: replacing ``_tls_enabled()`` with ``return True`` breaks
    ``test_default_does_not_pass_ssl``. Replacing with ``return False`` preserves it.
    """

    def test_default_does_not_pass_ssl(self):
        """Absent env var means the pre-2026-09-16 default: no ssl kwarg at all."""
        fake = MagicMock()
        with patch.object(handler.redis, "Redis", return_value=fake) as ctor:
            handler._get_redis()
        kwargs = ctor.call_args.kwargs
        assert "ssl" not in kwargs, (
            "TLS-off default must produce kwargs byte-identical to the pre-change "
            "constructor; presence of `ssl` (even ssl=False) is a behaviour change"
        )
        assert "ssl_cert_reqs" not in kwargs

    def test_empty_string_reads_as_false(self):
        os.environ["REDIS_TLS_ENABLED"] = ""
        fake = MagicMock()
        with patch.object(handler.redis, "Redis", return_value=fake) as ctor:
            handler._get_redis()
        assert "ssl" not in ctor.call_args.kwargs

    def test_false_string_reads_as_false(self):
        os.environ["REDIS_TLS_ENABLED"] = "false"
        fake = MagicMock()
        with patch.object(handler.redis, "Redis", return_value=fake) as ctor:
            handler._get_redis()
        assert "ssl" not in ctor.call_args.kwargs

    def test_zero_reads_as_false(self):
        """A ``bool(os.environ.get(...))`` implementation would read '0' as True."""
        os.environ["REDIS_TLS_ENABLED"] = "0"
        fake = MagicMock()
        with patch.object(handler.redis, "Redis", return_value=fake) as ctor:
            handler._get_redis()
        assert "ssl" not in ctor.call_args.kwargs

    def test_typo_reads_as_false(self):
        """``bool("ture")`` is True, which would silently enable TLS on a misspelling."""
        os.environ["REDIS_TLS_ENABLED"] = "ture"
        fake = MagicMock()
        with patch.object(handler.redis, "Redis", return_value=fake) as ctor:
            handler._get_redis()
        assert "ssl" not in ctor.call_args.kwargs, (
            "the predicate must be strict-string equality; a permissive bool(...) would "
            "enable TLS on any misspelling and there is no visible symptom until the "
            "connection fails"
        )


class TestTlsToggleEnabled:
    """When ``REDIS_TLS_ENABLED=true``, TLS kwargs MUST be present and MUST verify certs.

    Mutation guard: replacing ``ssl_cert_reqs="required"`` with ``"none"`` breaks
    ``test_tls_on_requires_cert_verification``. That mutation is the exact way a plausible
    "improvement" (allow self-signed certs during migration) becomes a MITM-open state.
    """

    @pytest.mark.parametrize("value", ["true", "TRUE", "True", "TrUe"])
    def test_case_insensitive_true(self, value):
        os.environ["REDIS_TLS_ENABLED"] = value
        fake = MagicMock()
        with patch.object(handler.redis, "Redis", return_value=fake) as ctor:
            handler._get_redis()
        assert ctor.call_args.kwargs["ssl"] is True

    def test_tls_on_requires_cert_verification(self):
        """The whole point of TLS is defeated by ``ssl_cert_reqs="none"``."""
        os.environ["REDIS_TLS_ENABLED"] = "true"
        fake = MagicMock()
        with patch.object(handler.redis, "Redis", return_value=fake) as ctor:
            handler._get_redis()
        assert ctor.call_args.kwargs["ssl_cert_reqs"] == "required", (
            "ssl_cert_reqs must be 'required'; any weaker setting turns TLS into "
            "wire-encryption theater — an active attacker can intercept and forge"
        )

    def test_tls_handshake_failure_fails_open(self):
        """A failed TLS handshake must return None, not fall back to plaintext."""
        os.environ["REDIS_TLS_ENABLED"] = "true"
        import ssl as ssl_mod

        with patch.object(
            handler.redis, "Redis", side_effect=ssl_mod.SSLError("cert expired")
        ):
            assert handler._get_redis() is None
        assert handler._redis_client is None, "no fallback to a plaintext connection"


# ─────────────────────────── AUTH toggle ───────────────────────────


class TestAuthToggleDefaults:
    def test_default_does_not_pass_password(self):
        fake = MagicMock()
        with patch.object(handler.redis, "Redis", return_value=fake) as ctor:
            handler._get_redis()
        assert "password" not in ctor.call_args.kwargs, (
            "AUTH-off default must not carry a `password` kwarg — even `password=None` "
            "would change the constructor signature and could affect redis-py's handling"
        )

    def test_empty_secret_arn_reads_as_unset(self):
        os.environ["REDIS_AUTH_TOKEN_SECRET_ARN"] = ""
        fake = MagicMock()
        with patch.object(handler.redis, "Redis", return_value=fake) as ctor:
            handler._get_redis()
        assert "password" not in ctor.call_args.kwargs

    def test_default_does_not_call_secrets_manager(self):
        """Absent AUTH env var must NOT construct a boto3 client for secretsmanager."""
        fake = MagicMock()
        with patch.object(handler.redis, "Redis", return_value=fake), \
             patch.object(handler.boto3, "client") as boto:
            handler._get_redis()
        boto.assert_not_called()


class TestAuthTokenFetch:
    """When ``REDIS_AUTH_TOKEN_SECRET_ARN`` is set, the token MUST be fetched from Secrets
    Manager and passed as ``password=``. On any fetch failure the client MUST return None.

    Mutation guard: replacing the ``if token is None: return None`` early-return with a
    fallback to a plaintext connect breaks
    ``test_missing_secret_disables_cache_not_falls_back``.
    """

    def test_token_fetched_and_passed_as_password(self):
        os.environ["REDIS_AUTH_TOKEN_SECRET_ARN"] = (
            "arn:aws:secretsmanager:us-west-2:123:secret:redis-auth-abc"
        )
        fake_sm = MagicMock()
        fake_sm.get_secret_value.return_value = {"SecretString": "supersecret-token-42"}

        fake_redis = MagicMock()
        with patch.object(handler.redis, "Redis", return_value=fake_redis) as ctor, \
             patch.object(handler.boto3, "client", return_value=fake_sm) as boto_ctor:
            handler._get_redis()

        boto_ctor.assert_called_once_with("secretsmanager")
        fake_sm.get_secret_value.assert_called_once_with(
            SecretId="arn:aws:secretsmanager:us-west-2:123:secret:redis-auth-abc"
        )
        assert ctor.call_args.kwargs["password"] == "supersecret-token-42", (
            "token must be forwarded verbatim; any transformation risks mismatching what "
            "ElastiCache expects and turns AUTH into a silent no-cache"
        )

    def test_missing_secret_disables_cache_not_falls_back(self):
        """A fetch failure (IAM AccessDenied, etc.) MUST NOT result in an unauthenticated
        connect. This is the mutation-guard for the whole point of the AUTH toggle."""
        os.environ["REDIS_AUTH_TOKEN_SECRET_ARN"] = (
            "arn:aws:secretsmanager:us-west-2:123:secret:missing"
        )
        fake_sm = MagicMock()
        fake_sm.get_secret_value.side_effect = Exception("AccessDenied")

        with patch.object(handler.redis, "Redis") as ctor, \
             patch.object(handler.boto3, "client", return_value=fake_sm):
            got = handler._get_redis()

        assert got is None, "fetch failure must disable cache, not open a plaintext connection"
        ctor.assert_not_called()

    def test_binary_secret_disables_cache(self):
        """SecretsManager entries carrying `SecretBinary` (not `SecretString`) are not
        supported. Fail open rather than pass ``password=None`` to redis."""
        os.environ["REDIS_AUTH_TOKEN_SECRET_ARN"] = (
            "arn:aws:secretsmanager:us-west-2:123:secret:binary"
        )
        fake_sm = MagicMock()
        fake_sm.get_secret_value.return_value = {"SecretBinary": b"\x00\x01\x02"}

        with patch.object(handler.redis, "Redis") as ctor, \
             patch.object(handler.boto3, "client", return_value=fake_sm):
            got = handler._get_redis()

        assert got is None
        ctor.assert_not_called()

    def test_auth_token_never_appears_in_module_source(self):
        """A hardcoded default token in the source would ship to prod and become a real
        credential the moment the cluster starts requiring AUTH."""
        import pathlib

        src = pathlib.Path(handler.__file__).read_text()
        for line in src.splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            if "kwargs[" in stripped:
                continue
            if 'password="' in stripped or "password='" in stripped:
                pytest.fail(
                    f"Hardcoded password literal in handler.py: {stripped!r}. "
                    "A default password is a canary that becomes a real credential the "
                    "moment the cluster starts requiring AUTH."
                )


# ─────────────────────────── TLS + AUTH combined ───────────────────────────


class TestBothEnabled:
    def test_tls_and_auth_both_pass_through(self):
        os.environ["REDIS_TLS_ENABLED"] = "true"
        os.environ["REDIS_AUTH_TOKEN_SECRET_ARN"] = (
            "arn:aws:secretsmanager:us-west-2:123:secret:both"
        )
        fake_sm = MagicMock()
        fake_sm.get_secret_value.return_value = {"SecretString": "tls-and-auth"}
        fake_redis = MagicMock()

        with patch.object(handler.redis, "Redis", return_value=fake_redis) as ctor, \
             patch.object(handler.boto3, "client", return_value=fake_sm):
            handler._get_redis()

        kwargs = ctor.call_args.kwargs
        assert kwargs["ssl"] is True
        assert kwargs["ssl_cert_reqs"] == "required"
        assert kwargs["password"] == "tls-and-auth"
        # Load-bearing existing kwargs still present
        assert kwargs["socket_connect_timeout"] == 2
        assert kwargs["socket_timeout"] == 2
        assert kwargs["decode_responses"] is True


# ─────────────────────────── Backward compat with pre-change behavior ───────────────────


class TestBackwardCompat:
    def test_default_kwargs_are_byte_identical_to_pre_change(self):
        """The whole set of kwargs — with TLS+AUTH both off — must match the pre-2026-09-16
        constructor exactly. Any drift in this default path is a silent regression."""
        fake = MagicMock()
        with patch.object(handler.redis, "Redis", return_value=fake) as ctor:
            handler._get_redis()
        kwargs = ctor.call_args.kwargs
        assert set(kwargs.keys()) == {
            "host",
            "port",
            "socket_connect_timeout",
            "socket_timeout",
            "decode_responses",
        }, (
            "the default kwargs set MUST contain exactly the 5 pre-change kwargs; adding "
            "or removing keys silently changes what redis-py does even when their VALUES "
            "look 'safe'"
        )
