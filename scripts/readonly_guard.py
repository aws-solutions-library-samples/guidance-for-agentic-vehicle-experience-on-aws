# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Make a boto3 session provably read-only for a dry run.

Spec: ADP ``.kiro/specs/2026-09-30-cms-adp-convergence-stage1`` decisions.md 2026-10-02
"Group 2 review cycle 1 rulings" ruling 8.

``install(session)`` registers a botocore ``before-call`` handler on every client the
session creates. Any operation whose name does not start with a read prefix raises
``ReadOnlyViolation`` before the request is signed or sent, so a dry run cannot write even
if a code path forgets to check ``--apply``.

Usage::

    session = boto3.Session(region_name="us-west-2")
    if not args.apply:
        readonly_guard.install(session)
    ddb = session.client("dynamodb")
"""

from __future__ import annotations

READ_PREFIXES = (
    "Describe", "Get", "List", "Scan", "Query", "BatchGet", "TestAuthorization",
)


class ReadOnlyViolation(RuntimeError):
    """Raised when a dry run attempts a mutating AWS call."""


def _is_read(operation_name: str) -> bool:
    return operation_name.startswith(READ_PREFIXES)


def _handler(model, **_kwargs):
    name = model.name
    if not _is_read(name):
        raise ReadOnlyViolation(f"dry run refused mutating call {model.service_model.service_name}:{name}")


def install(session) -> None:
    """Install the guard on a ``boto3.Session`` (affects clients and resources it creates)."""
    session.events.register("before-call.*.*", _handler, unique_id="readonly-guard")


def install_on_client(client) -> None:
    """Install the guard on a single already-created client."""
    client.meta.events.register("before-call.*.*", _handler, unique_id="readonly-guard")
