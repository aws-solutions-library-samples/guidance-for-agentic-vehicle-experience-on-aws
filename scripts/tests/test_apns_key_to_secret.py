# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Tests for scripts/apns-key-to-secret.py (spec 2026-11-05-avx-proactive-p0-alert, T1.3a-2).

Uses botocore Stubber for exact request-param assertions.
The repo-root conftest.py blocks all real AWS calls and installs fake credentials.

Mutation checks — all four verified caught:
    M1 mode_check_removed
        Remove the ``mode != 0o600`` branch from ``_validate_key_file``.
        ``test_refusal_mode_0644`` no longer gets a RefusalError; main() falls
        through to the boto3 call path, which has no Stubber and hits conftest's
        RealAwsCallBlocked guard → test FAILS.

    M2 key_printed
        Add ``print(p8_content)`` (or ``print(arn, version_id, p8_content)``) in
        the success path.
        ``test_stdout_no_key_canary`` finds _P8_CANARY in captured stdout → FAILS.

    M3 owner_check_removed
        Remove the ``file_stat.st_uid != current_uid`` branch.
        ``test_refusal_wrong_owner`` no longer gets a RefusalError → FAILS.

    M4 secret_name_not_stage_scoped
        Change ``_secret_name`` to return ``"apns-auth-key"`` (drop the stage prefix).
        ``test_create_exact_params`` Stubber was programmed with
        ``Name="cvx/test/apns-auth-key"``; mis-named call raises StubAssertionError → FAILS.

Run::

    .venv/bin/python -m pytest scripts/tests/test_apns_key_to_secret.py -q
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import boto3
import pytest
from botocore.stub import Stubber

# ---------------------------------------------------------------------------
# Load the script module (hyphenated filename — can't use normal import)
# ---------------------------------------------------------------------------

_CVX_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _CVX_ROOT / "scripts" / "apns-key-to-secret.py"

_mod_name = "_apns_key_to_secret"
if _mod_name not in sys.modules:
    _spec = importlib.util.spec_from_file_location(_mod_name, _SCRIPT)
    _mod = importlib.util.module_from_spec(_spec)
    sys.modules[_mod_name] = _mod
    _spec.loader.exec_module(_mod)

m = sys.modules[_mod_name]

# ---------------------------------------------------------------------------
# Shared test constants
# ---------------------------------------------------------------------------

_STAGE = "test"
_REGION = "us-east-1"
_KEY_ID = "ABCDE12345"
_TEAM_ID = "TEAMID5678"
_BUNDLE_ID = "com.example.avx"

# _P8_CANARY is the sentinel string we check never leaks to stdout/stderr.
# It must not appear literally anywhere else in this file (substring rule).
_P8_CANARY = "P8_CANARY_UNIQUE_7f3e9b2a"
_PEM_CONTENT = (
    "-----BEGIN PRIVATE KEY-----\n"
    + _P8_CANARY
    + "\n-----END PRIVATE KEY-----\n"
)

_SECRET_NAME = f"cvx/{_STAGE}/apns-auth-key"
_SECRET_ARN = (
    f"arn:aws:secretsmanager:{_REGION}:123456789012:"
    f"secret:{_SECRET_NAME}-AbCdEf"
)
_VERSION_ID = "3eba63dc-21d5-4c2f-8c50-2a2cb5f748e4"  # UUID, min 32 chars

# The exact SecretString JSON the script is expected to write
_EXPECTED_SECRET_JSON = json.dumps(
    {
        "key_id": _KEY_ID,
        "team_id": _TEAM_ID,
        "bundle_id": _BUNDLE_ID,
        "p8": _PEM_CONTENT,
    }
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_valid_key_file(tmp_path: Path) -> Path:
    """Return a 0600 file owned by the current user containing a PEM header."""
    key_file = tmp_path / "AuthKey_ABCDE12345.p8"
    key_file.write_text(_PEM_CONTENT, encoding="utf-8")
    key_file.chmod(0o600)
    return key_file


def _valid_args(key_file: Path) -> list[str]:
    return [
        "--stage", _STAGE,
        "--region", _REGION,
        "--key-file", str(key_file),
        "--key-id", _KEY_ID,
        "--team-id", _TEAM_ID,
        "--bundle-id", _BUNDLE_ID,
    ]


def _sm_client(region: str = _REGION):
    return boto3.client("secretsmanager", region_name=region)


# ---------------------------------------------------------------------------
# TestRefusals — all must return 1 with a clear stderr message, no AWS call
# ---------------------------------------------------------------------------


class TestRefusals:
    """Each path refuses before any boto3 client is created."""

    def test_refusal_mode_0644(self, tmp_path, capsys):
        """M1 target: mode check catches 0644 (group-readable)."""
        key_file = tmp_path / "key.p8"
        key_file.write_text(_PEM_CONTENT)
        key_file.chmod(0o644)

        rc = m.main(_valid_args(key_file))

        assert rc == 1
        err = capsys.readouterr().err
        assert "0600" in err

    def test_refusal_mode_0640(self, tmp_path, capsys):
        """0640 (group-read) is also rejected."""
        key_file = tmp_path / "key.p8"
        key_file.write_text(_PEM_CONTENT)
        key_file.chmod(0o640)

        rc = m.main(_valid_args(key_file))

        assert rc == 1
        err = capsys.readouterr().err
        assert "0600" in err

    def test_refusal_symlink(self, tmp_path, capsys):
        """A symlink is rejected even if its target is a valid 0600 file."""
        real_file = tmp_path / "real.p8"
        real_file.write_text(_PEM_CONTENT)
        real_file.chmod(0o600)
        link = tmp_path / "link.p8"
        link.symlink_to(real_file)

        rc = m.main(_valid_args(link))

        assert rc == 1
        err = capsys.readouterr().err
        assert "regular file" in err.lower() or "not a regular" in err.lower()

    def test_refusal_wrong_owner(self, tmp_path, capsys):
        """M3 target: owner check catches a file owned by a different uid."""
        key_file = _make_valid_key_file(tmp_path)

        # Patch os.getuid so the script sees a uid that doesn't match the file's
        with patch("os.getuid", return_value=os.lstat(str(key_file)).st_uid + 1000):
            rc = m.main(_valid_args(key_file))

        assert rc == 1
        err = capsys.readouterr().err
        assert "owned by" in err.lower() or "uid" in err.lower()

    def test_refusal_not_pem(self, tmp_path, capsys):
        """A 0600 file without the PEM header is rejected."""
        key_file = tmp_path / "key.p8"
        key_file.write_text("this is not a PEM key\n")
        key_file.chmod(0o600)

        rc = m.main(_valid_args(key_file))

        assert rc == 1
        err = capsys.readouterr().err
        assert "pem" in err.lower() or "BEGIN PRIVATE KEY" in err

    def test_refusal_bad_key_id_short(self, tmp_path, capsys):
        """key-id shorter than 10 chars is rejected."""
        key_file = _make_valid_key_file(tmp_path)
        args = _valid_args(key_file)
        args[args.index("--key-id") + 1] = "ABC123"  # only 6 chars

        rc = m.main(args)

        assert rc == 1
        err = capsys.readouterr().err
        assert "key-id" in err.lower() or "--key-id" in err

    def test_refusal_bad_key_id_lowercase(self, tmp_path, capsys):
        """key-id with lowercase letters is rejected."""
        key_file = _make_valid_key_file(tmp_path)
        args = _valid_args(key_file)
        args[args.index("--key-id") + 1] = "abcde12345"

        rc = m.main(args)

        assert rc == 1
        assert "key-id" in capsys.readouterr().err.lower()

    def test_refusal_bad_team_id(self, tmp_path, capsys):
        """team-id with special chars is rejected."""
        key_file = _make_valid_key_file(tmp_path)
        args = _valid_args(key_file)
        args[args.index("--team-id") + 1] = "TEAM-12345"  # hyphen not allowed

        rc = m.main(args)

        assert rc == 1
        assert "team-id" in capsys.readouterr().err.lower()

    def test_refusal_bad_bundle_id_no_dot(self, tmp_path, capsys):
        """bundle-id with no dot (not reverse-DNS) is rejected."""
        key_file = _make_valid_key_file(tmp_path)
        args = _valid_args(key_file)
        args[args.index("--bundle-id") + 1] = "comexampleavx"

        rc = m.main(args)

        assert rc == 1
        assert "bundle-id" in capsys.readouterr().err.lower()

    def test_refusal_bad_bundle_id_starts_with_dot(self, tmp_path, capsys):
        """.com.example (leading dot) is rejected."""
        key_file = _make_valid_key_file(tmp_path)
        args = _valid_args(key_file)
        args[args.index("--bundle-id") + 1] = ".com.example"

        rc = m.main(args)

        assert rc == 1

    def test_refusal_no_stdout_during_error(self, tmp_path, capsys):
        """Nothing goes to stdout on a refusal — only stderr carries the message."""
        key_file = tmp_path / "key.p8"
        key_file.write_text("no-pem")
        key_file.chmod(0o644)

        m.main(_valid_args(key_file))

        captured = capsys.readouterr()
        assert captured.out == ""


# ---------------------------------------------------------------------------
# TestCreateSecret — M4 target
# ---------------------------------------------------------------------------


class TestCreateSecret:
    """New secret path: create_secret called with exact params."""

    def test_create_exact_params(self, tmp_path, capsys):
        """M4: if secret name drops stage prefix, Stubber raises StubAssertionError."""
        key_file = _make_valid_key_file(tmp_path)

        sm = _sm_client()
        stubber = Stubber(sm)
        stubber.add_response(
            "create_secret",
            {
                "ARN": _SECRET_ARN,
                "Name": _SECRET_NAME,
                "VersionId": _VERSION_ID,
            },
            expected_params={
                "Name": _SECRET_NAME,                             # M4 catches if this changes
                "Description": f"APNs auth key for CVX stage {_STAGE}",
                "SecretString": _EXPECTED_SECRET_JSON,
                "Tags": [
                    {"Key": "app", "Value": "cvx"},
                    {"Key": "stage", "Value": _STAGE},
                ],
            },
        )

        with stubber:
            with patch.object(m, "_sm_client", return_value=sm):
                rc = m.main(_valid_args(key_file))

        assert rc == 0
        stubber.assert_no_pending_responses()

    def test_stdout_is_arn_then_version_id(self, tmp_path, capsys):
        """Successful create prints ARN on line 1, VersionId on line 2."""
        key_file = _make_valid_key_file(tmp_path)

        sm = _sm_client()
        stubber = Stubber(sm)
        stubber.add_response(
            "create_secret",
            {"ARN": _SECRET_ARN, "Name": _SECRET_NAME, "VersionId": _VERSION_ID},
            expected_params={
                "Name": _SECRET_NAME,
                "Description": f"APNs auth key for CVX stage {_STAGE}",
                "SecretString": _EXPECTED_SECRET_JSON,
                "Tags": [
                    {"Key": "app", "Value": "cvx"},
                    {"Key": "stage", "Value": _STAGE},
                ],
            },
        )

        with stubber:
            with patch.object(m, "_sm_client", return_value=sm):
                rc = m.main(_valid_args(key_file))

        assert rc == 0
        out_lines = capsys.readouterr().out.splitlines()
        assert out_lines[0] == _SECRET_ARN
        assert out_lines[1] == _VERSION_ID

    def test_stdout_no_key_canary(self, tmp_path, capsys):
        """M2 target: key bytes (the canary) must not appear in stdout or stderr."""
        key_file = _make_valid_key_file(tmp_path)

        sm = _sm_client()
        stubber = Stubber(sm)
        stubber.add_response(
            "create_secret",
            {"ARN": _SECRET_ARN, "Name": _SECRET_NAME, "VersionId": _VERSION_ID},
            expected_params={
                "Name": _SECRET_NAME,
                "Description": f"APNs auth key for CVX stage {_STAGE}",
                "SecretString": _EXPECTED_SECRET_JSON,
                "Tags": [
                    {"Key": "app", "Value": "cvx"},
                    {"Key": "stage", "Value": _STAGE},
                ],
            },
        )

        with stubber:
            with patch.object(m, "_sm_client", return_value=sm):
                m.main(_valid_args(key_file))

        captured = capsys.readouterr()
        combined = captured.out + captured.err
        assert _P8_CANARY not in combined, (
            "Key bytes (canary) must not appear in any output"
        )
        assert "BEGIN PRIVATE KEY" not in combined, (
            "PEM header must not appear in any output"
        )


# ---------------------------------------------------------------------------
# TestExistingSecret — create falls back to put_secret_value
# ---------------------------------------------------------------------------


class TestExistingSecret:
    """ResourceExistsException on create → falls back to put_secret_value."""

    def test_put_existing_uses_put_secret_value(self, tmp_path, capsys):
        """Exact put_secret_value params when the secret already exists."""
        key_file = _make_valid_key_file(tmp_path)

        sm = _sm_client()
        stubber = Stubber(sm)

        # create_secret → ResourceExistsException
        stubber.add_client_error(
            "create_secret",
            service_error_code="ResourceExistsException",
            service_message="A resource with the ID already exists.",
            expected_params={
                "Name": _SECRET_NAME,
                "Description": f"APNs auth key for CVX stage {_STAGE}",
                "SecretString": _EXPECTED_SECRET_JSON,
                "Tags": [
                    {"Key": "app", "Value": "cvx"},
                    {"Key": "stage", "Value": _STAGE},
                ],
            },
        )

        # put_secret_value response
        stubber.add_response(
            "put_secret_value",
            {"ARN": _SECRET_ARN, "Name": _SECRET_NAME, "VersionId": _VERSION_ID + "-v2"},
            expected_params={
                "SecretId": _SECRET_NAME,
                "SecretString": _EXPECTED_SECRET_JSON,
            },
        )

        with stubber:
            with patch.object(m, "_sm_client", return_value=sm):
                rc = m.main(_valid_args(key_file))

        assert rc == 0
        stubber.assert_no_pending_responses()

        out_lines = capsys.readouterr().out.splitlines()
        assert out_lines[0] == _SECRET_ARN
        assert out_lines[1] == _VERSION_ID + "-v2"

    def test_existing_stdout_no_key_canary(self, tmp_path, capsys):
        """Key canary must not appear in output even on put_secret_value path."""
        key_file = _make_valid_key_file(tmp_path)

        sm = _sm_client()
        stubber = Stubber(sm)
        stubber.add_client_error(
            "create_secret",
            service_error_code="ResourceExistsException",
            service_message="Already exists.",
            expected_params={
                "Name": _SECRET_NAME,
                "Description": f"APNs auth key for CVX stage {_STAGE}",
                "SecretString": _EXPECTED_SECRET_JSON,
                "Tags": [
                    {"Key": "app", "Value": "cvx"},
                    {"Key": "stage", "Value": _STAGE},
                ],
            },
        )
        stubber.add_response(
            "put_secret_value",
            {"ARN": _SECRET_ARN, "Name": _SECRET_NAME, "VersionId": _VERSION_ID},
            expected_params={
                "SecretId": _SECRET_NAME,
                "SecretString": _EXPECTED_SECRET_JSON,
            },
        )

        with stubber:
            with patch.object(m, "_sm_client", return_value=sm):
                m.main(_valid_args(key_file))

        combined = "".join(capsys.readouterr())
        assert _P8_CANARY not in combined


# ---------------------------------------------------------------------------
# TestDryRun
# ---------------------------------------------------------------------------


class TestDryRun:
    """--dry-run: print metadata without key bytes; make no AWS call."""

    def test_dry_run_exits_zero(self, tmp_path, capsys):
        key_file = _make_valid_key_file(tmp_path)

        rc = m.main([*_valid_args(key_file), "--dry-run"])

        assert rc == 0

    def test_dry_run_no_key_canary(self, tmp_path, capsys):
        """Key bytes must not appear in dry-run output."""
        key_file = _make_valid_key_file(tmp_path)

        m.main([*_valid_args(key_file), "--dry-run"])

        combined = "".join(capsys.readouterr())
        assert _P8_CANARY not in combined, (
            "Dry-run output must not contain key bytes"
        )
        assert "BEGIN PRIVATE KEY" not in combined

    def test_dry_run_shows_secret_name_and_ids(self, tmp_path, capsys):
        """Dry-run output includes the secret name, key_id, team_id, bundle_id."""
        key_file = _make_valid_key_file(tmp_path)

        m.main([*_valid_args(key_file), "--dry-run"])

        out = capsys.readouterr().out
        assert _SECRET_NAME in out
        assert _KEY_ID in out
        assert _TEAM_ID in out
        assert _BUNDLE_ID in out

    def test_dry_run_makes_no_aws_call(self, tmp_path):
        """No boto3 client is ever created in dry-run mode.

        If _sm_client were called, the mock would record a call and the
        assertion below would fail.
        """
        key_file = _make_valid_key_file(tmp_path)

        with patch.object(m, "_sm_client") as sm_factory:
            m.main([*_valid_args(key_file), "--dry-run"])

        sm_factory.assert_not_called()

    def test_dry_run_still_validates_file(self, tmp_path, capsys):
        """--dry-run does not bypass file validation."""
        bad_file = tmp_path / "key.p8"
        bad_file.write_text(_PEM_CONTENT)
        bad_file.chmod(0o644)  # wrong mode

        rc = m.main([*_valid_args(bad_file), "--dry-run"])

        assert rc == 1
        assert "0600" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# TestSubprocess — script runs as a file (subprocess runpy)
# ---------------------------------------------------------------------------


class TestSubprocess:
    """Run the script as a file in a subprocess to confirm it's importable and
    the __main__ guard is correct."""

    def test_script_exits_nonzero_with_no_args(self):
        """No args → argparse error → exit 2 (standard argparse behaviour).

        Verifies the script file is syntactically valid and can be executed.
        """
        result = subprocess.run(
            [sys.executable, str(_SCRIPT)],
            capture_output=True,
            text=True,
        )
        assert result.returncode != 0, (
            f"Expected non-zero exit when no args given; got {result.returncode}"
        )

    def test_dry_run_subprocess(self, tmp_path):
        """Full end-to-end subprocess run with --dry-run.

        No boto3 client is created; exit must be 0; key canary must not appear
        in stdout or stderr.
        """
        key_file = tmp_path / "key.p8"
        key_file.write_text(_PEM_CONTENT)
        key_file.chmod(0o600)

        result = subprocess.run(
            [
                sys.executable, str(_SCRIPT),
                "--stage", _STAGE,
                "--region", _REGION,
                "--key-file", str(key_file),
                "--key-id", _KEY_ID,
                "--team-id", _TEAM_ID,
                "--bundle-id", _BUNDLE_ID,
                "--dry-run",
            ],
            capture_output=True,
            text=True,
        )

        assert result.returncode == 0, (
            f"Dry-run subprocess failed: rc={result.returncode}\n"
            f"stdout={result.stdout}\nstderr={result.stderr}"
        )
        combined = result.stdout + result.stderr
        assert _P8_CANARY not in combined, (
            "Key canary must not appear in subprocess output"
        )
        assert _SECRET_NAME in result.stdout, (
            f"Expected secret name in dry-run output; got: {result.stdout!r}"
        )

    def test_subprocess_refuses_bad_mode(self, tmp_path):
        """Subprocess exits 1 on mode violation — no AWS call."""
        key_file = tmp_path / "key.p8"
        key_file.write_text(_PEM_CONTENT)
        key_file.chmod(0o644)

        result = subprocess.run(
            [
                sys.executable, str(_SCRIPT),
                "--stage", _STAGE,
                "--region", _REGION,
                "--key-file", str(key_file),
                "--key-id", _KEY_ID,
                "--team-id", _TEAM_ID,
                "--bundle-id", _BUNDLE_ID,
            ],
            capture_output=True,
            text=True,
        )

        assert result.returncode == 1
        assert "0600" in result.stderr
        assert _P8_CANARY not in result.stdout + result.stderr
