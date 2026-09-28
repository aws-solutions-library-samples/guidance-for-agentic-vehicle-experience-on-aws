"""Tests for the ADP Bedrock KB fields on Config (spec 2026-06-18-cvx-adp-kb-wireup).

Covers the env-var -> field mapping that closes the CVX-on-foundation v1
carry-forward gap: Config.from_env() must populate adp_kb_id / adp_region /
adp_account_id so the four KB-grounded tool factories (which read
getattr(config, "adp_kb_id", "")) see a real value in the deployed runtime.

Three cases:
  (a) all three env vars set        -> exact round-trip, NO STS call
  (b) account id unset              -> STS get-caller-identity fallback populates
  (c) all unset and STS unavailable -> safe-empty defaults, NO exception
"""

from __future__ import annotations

from unittest import mock

import pytest
from botocore.exceptions import NoCredentialsError

from agents.supervisor.config import Config

# 12-digit placeholder per test_persona_loader.py convention — never a real id.
_PLACEHOLDER_ACCOUNT = "123456789012"


def _clear_adp_env(monkeypatch) -> None:
    """Start each case from a known-clean ADP env so the host environment
    (which may carry VSA_ADP_* from a developer shell) cannot leak in."""
    for key in ("VSA_ADP_KB_ID", "VSA_ADP_REGION", "VSA_ADP_ACCOUNT_ID"):
        monkeypatch.delenv(key, raising=False)


def test_all_env_vars_set_round_trip_no_sts(monkeypatch):
    """(a) All three set -> exact field values, and STS is never consulted."""
    _clear_adp_env(monkeypatch)
    monkeypatch.setenv("VSA_ADP_KB_ID", "YULHRAUMNU")
    monkeypatch.setenv("VSA_ADP_REGION", "us-east-1")
    monkeypatch.setenv("VSA_ADP_ACCOUNT_ID", _PLACEHOLDER_ACCOUNT)

    with mock.patch("agents.supervisor.config.boto3.client") as mock_client:
        config = Config.from_env()

    assert config.adp_kb_id == "YULHRAUMNU"
    assert config.adp_region == "us-east-1"
    assert config.adp_account_id == _PLACEHOLDER_ACCOUNT
    # The account id was supplied explicitly, so the STS fallback must NOT fire.
    mock_client.assert_not_called()


def test_account_id_unset_uses_sts_fallback(monkeypatch):
    """(b) Account id unset -> resolved via STS get-caller-identity."""
    _clear_adp_env(monkeypatch)
    monkeypatch.setenv("VSA_ADP_KB_ID", "YULHRAUMNU")
    monkeypatch.setenv("VSA_ADP_REGION", "us-west-2")
    # VSA_ADP_ACCOUNT_ID intentionally left unset.

    sts = mock.MagicMock()
    sts.get_caller_identity.return_value = {"Account": _PLACEHOLDER_ACCOUNT}
    with mock.patch("agents.supervisor.config.boto3.client", return_value=sts) as mock_client:
        config = Config.from_env()

    assert config.adp_kb_id == "YULHRAUMNU"
    assert config.adp_region == "us-west-2"
    assert config.adp_account_id == _PLACEHOLDER_ACCOUNT
    mock_client.assert_called_once_with("sts")


def test_all_unset_sts_unavailable_safe_defaults(monkeypatch):
    """(c) Nothing set and STS raises a credential error -> no exception,
    safe-empty defaults.

    Protects unit-test contexts that construct Config.from_env() without AWS
    credentials available. Uses a real botocore error (NoCredentialsError, a
    BotoCoreError subclass) to match the narrowed except in from_env().
    """
    _clear_adp_env(monkeypatch)

    with mock.patch(
        "agents.supervisor.config.boto3.client",
        side_effect=NoCredentialsError(),
    ):
        config = Config.from_env()

    assert config.adp_kb_id == ""
    assert config.adp_region == "us-east-1"
    assert config.adp_account_id == ""


def test_unexpected_sts_exception_propagates(monkeypatch):
    """(d) An UNEXPECTED (non-botocore) error from STS is NOT swallowed.

    The narrowed `except (BotoCoreError, ClientError)` lets a programming or
    environment bug surface at root cause instead of silently defaulting
    adp_account_id="" and failing further downstream (security-review cycle 1
    hardening, spec 2026-06-18-cvx-adp-kb-wireup).
    """
    _clear_adp_env(monkeypatch)
    monkeypatch.setenv("VSA_ADP_KB_ID", "YULHRAUMNU")

    with mock.patch(
        "agents.supervisor.config.boto3.client",
        side_effect=RuntimeError("monkeypatched boto3 returned the wrong shape"),
    ):
        with pytest.raises(RuntimeError):
            Config.from_env()


def test_adp_account_id_excluded_from_repr(monkeypatch):
    """adp_account_id carries field(repr=False) so the resolved account id
    never lands in a log line via repr(config) / f"{config!r}"
    (security-review cycle 1 hardening, spec 2026-06-18-cvx-adp-kb-wireup)."""
    _clear_adp_env(monkeypatch)
    monkeypatch.setenv("VSA_ADP_KB_ID", "YULHRAUMNU")
    monkeypatch.setenv("VSA_ADP_ACCOUNT_ID", _PLACEHOLDER_ACCOUNT)

    with mock.patch("agents.supervisor.config.boto3.client"):
        config = Config.from_env()

    rendered = repr(config)
    assert _PLACEHOLDER_ACCOUNT not in rendered, "account id leaked into repr"
    assert "adp_account_id" not in rendered, "adp_account_id field shown in repr"
    # Sanity: a non-redacted ADP field still appears, so the field is populated
    # and only adp_account_id is suppressed.
    assert "adp_kb_id" in rendered
    # The value is still readable on the instance — repr-suppression only.
    assert config.adp_account_id == _PLACEHOLDER_ACCOUNT
