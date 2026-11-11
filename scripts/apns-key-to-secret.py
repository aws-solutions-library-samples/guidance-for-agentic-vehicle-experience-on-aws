#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""apns-key-to-secret.py — upload an APNs auth key (.p8) to Secrets Manager.

Usage::

    python3 scripts/apns-key-to-secret.py \\
        --stage staging \\
        --region us-west-2 \\
        --key-file /path/to/AuthKey_ABCDE12345.p8 \\
        --key-id ABCDE12345 \\
        --team-id TEAMID5678 \\
        --bundle-id com.example.avx \\
        [--dry-run]

The key file must be a regular file (not a symlink), have mode exactly 0600,
be owned by the current user, and contain a PEM private-key header.

Writes (or updates) Secrets Manager secret ``cvx/<stage>/apns-auth-key`` with
JSON ``{"key_id": ..., "team_id": ..., "bundle_id": ..., "p8": ...}``,
description ``APNs auth key for CVX stage <stage>``, and tags
``{"app": "cvx", "stage": <stage>}``.

Prints only the secret ARN and VersionId.  Key bytes (the p8 content) never
appear in any output, log, or exception message.

``--dry-run`` prints the metadata that would be written (without the key bytes)
and makes no AWS call.

Spec: ``.kiro/specs/2026-11-05-avx-proactive-p0-alert/`` task T1.3a-2.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import stat
import sys
from pathlib import Path

import boto3
from botocore.exceptions import ClientError


# ---------------------------------------------------------------------------
# Validation patterns
# ---------------------------------------------------------------------------

#: Key-ID and Team-ID: exactly 10 uppercase letters or digits.
_ID_RE = re.compile(r"^[A-Z0-9]{10}$")

#: Bundle identifier: at least two dot-separated labels (reverse-DNS shape).
#: Labels begin with a letter and contain letters, digits, and hyphens.
_BUNDLE_RE = re.compile(
    r"^[a-zA-Z][a-zA-Z0-9\-]*"
    r"(?:\.[a-zA-Z][a-zA-Z0-9\-]*)+$"
)

_PEM_HEADER = "-----BEGIN PRIVATE KEY-----"


# ---------------------------------------------------------------------------
# Refusal error
# ---------------------------------------------------------------------------


class RefusalError(ValueError):
    """Validation failure: main() prints the message to stderr and returns 1.

    Never set to str(key_bytes): key material must not appear in any exception
    message.
    """


def _refuse(message: str) -> None:
    """Raise RefusalError with *message* (makes no AWS call, does not print)."""
    raise RefusalError(message)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def _validate_ids(key_id: str, team_id: str) -> None:
    """Raise RefusalError if either ID doesn't match ^[A-Z0-9]{10}$."""
    if not _ID_RE.match(key_id):
        _refuse(
            f"--key-id must be exactly 10 uppercase letters/digits; "
            f"got {key_id!r}"
        )
    if not _ID_RE.match(team_id):
        _refuse(
            f"--team-id must be exactly 10 uppercase letters/digits; "
            f"got {team_id!r}"
        )


def _validate_bundle_id(bundle_id: str) -> None:
    """Raise RefusalError if bundle_id is not a reverse-DNS identifier."""
    if not _BUNDLE_RE.match(bundle_id):
        _refuse(
            f"--bundle-id must be a reverse-DNS identifier "
            f"(e.g. com.example.avx); got {bundle_id!r}"
        )


def _validate_key_file(path: str) -> str:
    """Validate the key file and return its text content.

    Refuses if:
    * the path is not a regular file (stat check; catches symlinks, dirs, …)
    * the file mode is not exactly 0600
    * the file is not owned by the current user
    * the file does not contain a PEM private-key header

    Key bytes are never included in any error message.
    """
    try:
        file_stat = os.lstat(path)  # lstat: does NOT follow symlinks
    except OSError as exc:
        _refuse(f"cannot stat {path!r}: {exc.strerror}")

    # Must be a regular file (S_ISREG is False for symlinks, directories, …)
    if not stat.S_ISREG(file_stat.st_mode):
        _refuse(
            f"{path!r} is not a regular file "
            f"(stat type: {stat.S_IFMT(file_stat.st_mode):#o})"
        )

    # Mode must be exactly 0600 — no group or world bits
    mode = stat.S_IMODE(file_stat.st_mode)
    if mode != 0o600:
        _refuse(
            f"{path!r} must have mode 0600 (found {oct(mode)}); "
            f"fix with: chmod 0600 {path}"
        )

    # Must be owned by the current user
    current_uid = os.getuid()
    if file_stat.st_uid != current_uid:
        _refuse(
            f"{path!r} must be owned by the current user "
            f"(uid {current_uid}); found uid {file_stat.st_uid}"
        )

    # Read content — key bytes never echoed to caller
    try:
        content = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        _refuse(f"cannot read {path!r}: {exc.strerror}")

    if _PEM_HEADER not in content:
        _refuse(
            f"{path!r} does not contain a PEM private key "
            f"(missing '{_PEM_HEADER}')"
        )

    return content


# ---------------------------------------------------------------------------
# Secrets Manager helpers
# ---------------------------------------------------------------------------


def _secret_name(stage: str) -> str:
    """Return the stage-scoped secret name (must not be called without stage)."""
    return f"cvx/{stage}/apns-auth-key"


def _sm_client(region: str):
    """Return a Secrets Manager boto3 client.  Replaced in unit tests via patch."""
    return boto3.client("secretsmanager", region_name=region)


def _upsert_secret(
    sm,
    *,
    stage: str,
    key_id: str,
    team_id: str,
    bundle_id: str,
    p8: str,
) -> tuple[str, str]:
    """Create or update the secret.  Returns (ARN, VersionId).

    Key bytes are *only* in the SecretString request; they never appear in any
    log, printed message, or exception message in this function.
    """
    name = _secret_name(stage)
    # Build JSON payload — key bytes stay inside this string until it reaches SM
    secret_string = json.dumps(
        {"key_id": key_id, "team_id": team_id, "bundle_id": bundle_id, "p8": p8}
    )
    description = f"APNs auth key for CVX stage {stage}"
    tags = [{"Key": "app", "Value": "cvx"}, {"Key": "stage", "Value": stage}]

    try:
        resp = sm.create_secret(
            Name=name,
            Description=description,
            SecretString=secret_string,
            Tags=tags,
        )
        return resp["ARN"], resp["VersionId"]
    except ClientError as exc:
        code = exc.response["Error"]["Code"]
        if code not in ("ResourceExistsException", "InvalidRequestException"):
            # Not an "already exists" error — re-raise without including key bytes
            raise

    # Secret already exists — put a new version
    resp = sm.put_secret_value(
        SecretId=name,
        SecretString=secret_string,
    )
    return resp["ARN"], resp["VersionId"]


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:  # noqa: PLR0911
    """Parse args, validate, write (or dry-run).  Returns 0 on success, 1 on error."""
    ap = argparse.ArgumentParser(
        description="Upload an APNs auth (.p8) key to AWS Secrets Manager.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--stage", required=True, help="Deployment stage (e.g. staging)")
    ap.add_argument("--region", required=True, help="AWS region")
    ap.add_argument(
        "--key-file",
        required=True,
        dest="key_file",
        metavar="FILE",
        help="Path to the .p8 key file (must be mode 0600, owned by current user)",
    )
    ap.add_argument(
        "--key-id",
        required=True,
        dest="key_id",
        metavar="K",
        help="APNs key ID — exactly 10 uppercase alphanumeric chars",
    )
    ap.add_argument(
        "--team-id",
        required=True,
        dest="team_id",
        metavar="T",
        help="Apple Developer Team ID — exactly 10 uppercase alphanumeric chars",
    )
    ap.add_argument(
        "--bundle-id",
        required=True,
        dest="bundle_id",
        metavar="B",
        help="iOS application bundle identifier (reverse-DNS, e.g. com.example.avx)",
    )
    ap.add_argument(
        "--dry-run",
        action="store_true",
        dest="dry_run",
        help="Print what would be written (without key bytes); make no AWS call",
    )

    args = ap.parse_args(argv)

    try:
        # Validate IDs first — no I/O needed, fast
        _validate_ids(args.key_id, args.team_id)
        _validate_bundle_id(args.bundle_id)

        # Validate and read the key file — all checks complete before any AWS call
        p8_content = _validate_key_file(args.key_file)

        if args.dry_run:
            # Metadata only — key bytes intentionally absent
            print(
                f"dry-run: would write secret {_secret_name(args.stage)!r} "
                f"with key_id={args.key_id!r} "
                f"team_id={args.team_id!r} "
                f"bundle_id={args.bundle_id!r} "
                f"(key bytes not shown)"
            )
            return 0

        sm = _sm_client(args.region)
        arn, version_id = _upsert_secret(
            sm,
            stage=args.stage,
            key_id=args.key_id,
            team_id=args.team_id,
            bundle_id=args.bundle_id,
            p8=p8_content,
        )

        # Print only ARN and VersionId — key bytes never reach stdout
        print(arn)
        print(version_id)
        return 0

    except RefusalError as exc:
        # Print the (key-bytes-free) message and exit non-zero
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
