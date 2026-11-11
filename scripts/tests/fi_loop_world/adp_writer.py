"""ADP writer piece — loads the real digital_thread_writer/handler.py from ADP 02b8716f.

The handler is loaded by path so the world uses exactly the ADP-side code at the
commit the brief names, regardless of what is on sys.path.  Module-level boto3 globals
in the handler (_dynamodb_resource, _s3_client) are reset to None after load so they
are rebuilt under moto on the first DynamoDB or S3 call inside the world.

Pin refreshed 2026-10-27 (stage 2a): deployed Lambda at 02b8716f per 64-facts.md § 1.
``git log 9bb82e96..02b8716f -- platform-foundation/source/lambda/digital_thread_writer/handler.py``
shows the digital-thread-lake feature commits; the handler now requires THREAD_LAKE_BUCKET
and puts a lake row per committed version.  FiLoopWorld._extend_world sets THREAD_LAKE_BUCKET
to the world's LAKE_BUCKET so S3 puts land in the world's moto bucket.

Original commit:
  ADP wip/fi-health-d-g9 9bb82e96
  platform-foundation/source/lambda/digital_thread_writer/handler.py

The tables and bucket this handler needs are created by ``FiLoopWorld``:
  - adp-staging-digital-thread        pk=thread_id, sk=version, GSI action_id-index
  - adp-staging-recall-campaigns-issued  pk=campaign_id
  - adp-staging-fault-patterns        pk=pattern_id
  - adp-staging-engineering-corpus    pk=pk
  - s3 bucket (ISSUED_RECALLS_BUCKET / VIN_BUCKET) = adp-staging-... (moto)

Invocation shape (from AdpExecutor._execute):
  boto3_lambda_client.invoke(
      FunctionName="arn:aws:lambda:us-east-1:123456789012:function:adp-staging-digital-thread-writer",
      InvocationType="RequestResponse",
      Payload=json.dumps(payload).encode(),
  )

The world registers this as a Lambda in the moto Lambda store so AdpExecutor's boto3
invoke call resolves to the real handler.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import uuid
from pathlib import Path
from typing import Any

_ADP_ROOT = Path(os.environ.get(
    "FI_LOOP_WORLD_ADP_REPO",
    str(Path.home() / "automotive-data-platform-on-aws")
))

#: Commit this world piece was authored against (ADP branch wip/fi-health-d-g9).
ADP_COMMIT = "02b8716f"

#: Path to the writer handler relative to the ADP repo root.
HANDLER_RELPATH = "platform-foundation/source/lambda/digital_thread_writer/handler.py"

FUNCTION_NAME = "adp-staging-digital-thread-writer"
FUNCTION_REGION = "us-east-1"
STAGE = "staging"

#: DynamoDB tables the writer touches (live describe-table reads 2026-10-21, scrubbed).
#: These are created by FiLoopWorld._create_adp_writer_tables().
TABLE_DEFS: list[dict] = [
    {
        "TableName": f"adp-{STAGE}-digital-thread",
        "KeySchema": [
            {"AttributeName": "thread_id", "KeyType": "HASH"},
            {"AttributeName": "version", "KeyType": "RANGE"},
        ],
        "AttributeDefinitions": [
            {"AttributeName": "thread_id", "AttributeType": "S"},
            {"AttributeName": "version", "AttributeType": "N"},
            {"AttributeName": "action_id", "AttributeType": "S"},
        ],
        "GlobalSecondaryIndexes": [{
            "IndexName": "action_id-index",
            "KeySchema": [{"AttributeName": "action_id", "KeyType": "HASH"}],
            "Projection": {"ProjectionType": "ALL"},
        }],
    },
    {
        "TableName": f"adp-{STAGE}-recall-campaigns-issued",
        "KeySchema": [{"AttributeName": "campaign_id", "KeyType": "HASH"}],
        "AttributeDefinitions": [{"AttributeName": "campaign_id", "AttributeType": "S"}],
    },
    {
        "TableName": f"adp-{STAGE}-ota-campaigns-issued",
        "KeySchema": [{"AttributeName": "campaign_id", "KeyType": "HASH"}],
        "AttributeDefinitions": [{"AttributeName": "campaign_id", "AttributeType": "S"}],
    },
    {
        "TableName": f"adp-{STAGE}-fault-patterns",
        "KeySchema": [{"AttributeName": "pattern_id", "KeyType": "HASH"}],
        "AttributeDefinitions": [{"AttributeName": "pattern_id", "AttributeType": "S"}],
    },
    {
        "TableName": f"adp-{STAGE}-engineering-corpus",
        "KeySchema": [{"AttributeName": "pk", "KeyType": "HASH"}],
        "AttributeDefinitions": [{"AttributeName": "pk", "AttributeType": "S"}],
    },
]


def load_handler() -> Any:
    """Load the ADP writer handler from ADP commit 02b8716f (refreshed 2026-10-27 stage 2a).

    Uses ``git show`` to extract the handler at commit ``ADP_COMMIT`` (02b8716f)
    so the world always runs the version the tests were authored against, regardless
    of the current state of the ADP working tree.

    W11 fix: fails loudly if git extraction fails.  The working tree is at
    9a08bca1, which lacks 9.1's issued path (486-line diff in handler.py) — a
    silent fallback would run wrong code against the world.

    Resets module-level boto3 globals so moto intercepts all AWS calls.
    Returns the loaded module.  Raises FileNotFoundError when the repo is absent.
    """
    import subprocess  # noqa: PLC0415
    import tempfile    # noqa: PLC0415

    if not _ADP_ROOT.exists():
        raise FileNotFoundError(
            f"ADP repo not found at {_ADP_ROOT}. "
            f"Set FI_LOOP_WORLD_ADP_REPO to the ADP repo root "
            f"(commit {ADP_COMMIT})."
        )

    # Extract the handler at ADP_COMMIT via git show.
    git_ref = f"{ADP_COMMIT}:{HANDLER_RELPATH}"
    try:
        proc = subprocess.run(
            ["git", "-C", str(_ADP_ROOT), "show", git_ref],
            capture_output=True, check=True,
        )
        handler_src = proc.stdout
    except subprocess.CalledProcessError as exc:
        # W11 fix: fail loudly — the working tree may be at a different commit
        # (9a08bca1 lacks the 9.1 issued-path, 486-line diff from 9bb82e96).
        # A fallback would silently run wrong code against the world.
        raise FileNotFoundError(
            f"git show {git_ref!r} failed in {_ADP_ROOT}: {exc.stderr.decode()!r}. "
            f"Ensure ADP commit {ADP_COMMIT} is present in the repo at "
            f"{_ADP_ROOT} (set FI_LOOP_WORLD_ADP_REPO to override the path)."
        ) from exc

    # Write to a temp file and load from there.
    with tempfile.NamedTemporaryFile(
        suffix=".py", prefix="adp_writer_handler_", delete=False
    ) as tf:
        tf.write(handler_src)
        tmp_path = tf.name

    name = f"adp_writer_{uuid.uuid4().hex[:8]}"
    # Ensure the ADP lambda package root is on sys.path for cross-module imports.
    pkg_root = str(_ADP_ROOT / "platform-foundation" / "source" / "lambda")
    if pkg_root not in sys.path:
        sys.path.append(pkg_root)
    spec = importlib.util.spec_from_file_location(name, tmp_path)
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    sys.modules[name] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    # Reset module-level boto3 clients so they are rebuilt under moto.
    for attr in ("_dynamodb_resource", "_s3_client"):
        if hasattr(mod, attr):
            setattr(mod, attr, None)
    return mod


def make_lambda_handler(mod: Any) -> Any:
    """Return a callable ``(event, ctx) -> dict`` wrapping the writer's handler.

    Re-initializes the module's boto3 clients AND module-level env-var caches
    in the current moto context on each invocation.

    Module-level env-var caches set at import time (e.g. ``_VIN_BUCKET``,
    ``_ISSUED_RECALLS_BUCKET``, ``_STAGE``) are re-read so the handler uses
    the lambda_env values rather than values that were in place at import time.
    """
    def _handler(event: dict[str, Any], ctx: Any = None) -> dict[str, Any]:
        import boto3 as _boto3
        import os as _os
        # Rebuild module-level boto3 clients in the current moto context.
        mod._dynamodb_resource = _boto3.resource("dynamodb", region_name="us-east-1")
        mod._s3_client = _boto3.client("s3", region_name="us-east-1")
        # Re-read env-var caches (set at import time; need current lambda_env values).
        for _attr, _env_key, _default in [
            ("_VIN_BUCKET", "VIN_BUCKET", ""),
            ("_ISSUED_RECALLS_BUCKET", "ISSUED_RECALLS_BUCKET", ""),
            ("_THREAD_LAKE_BUCKET", "THREAD_LAKE_BUCKET", ""),
            ("_STAGE", "STAGE", "staging"),
        ]:
            if hasattr(mod, _attr):
                setattr(mod, _attr, _os.environ.get(_env_key, _default))
        return mod.handle_event(event, ctx)

    return _handler


def seed_pattern(_, *, pattern_id: str, vin_list: list[str],
                  cohort_dimension: str,
                  cohort_value: str, code_family: str = "P0AB6") -> None:
    """Write a minimal fault-patterns row so the writer's ``get_affected_vins`` succeeds.

    F6a fix (brief §World): writes exactly the producer's attribute set — no
    ``failure_mode_id`` (the live ADP producer does not write it).  Includes
    ``pattern_group_id == pattern_id`` and ``candidate_rank: 1``.

    Uses the high-level Table resource so boto3 serializes Python types correctly
    (important for the sha256 check in ``affected_vins_from_item``).
    """
    import boto3
    import hashlib
    sorted_vins = sorted(set(vin_list))
    sha = hashlib.sha256(json.dumps(sorted_vins).encode()).hexdigest()
    table = boto3.resource("dynamodb", region_name=FUNCTION_REGION).Table(
        f"adp-{STAGE}-fault-patterns"
    )
    table.put_item(Item={
        "pattern_id": pattern_id,
        "pattern_group_id": pattern_id,   # F6a: producer sets pgid == pid for group-top
        "candidate_rank": 1,              # F6a: group-top row
        "code_family": code_family,
        "cohort_dimension": cohort_dimension,
        "cohort_value": cohort_value,
        "affected_vins": sorted_vins,
        "affected_vins_count": len(sorted_vins),
        "vehicles_affected": len(sorted_vins),
        "affected_vins_sha256": sha,
        "rate_ratio": "10.0",
        "p_value": "0.0001",
    })


def seed_failure_mode(_, *, failure_mode_id: str,
                       component_name: str, options: list[dict[str, Any]],
                       do_not_drive: bool = False,
                       park_outside: bool = False) -> None:
    """Write an engineering-corpus failure-mode row for the writer.

    C10 fix: include do_not_drive and park_outside at the failure-mode level (top level),
    matching failure_modes.yaml shape.  ADP writer 87d3025b handler.py:976 reads
    recall_class from failure_mode.get("do_not_drive"), not from the option.

    Uses the high-level Table resource.  Options are embedded in the row for
    simplicity; the writer's ``get_failure_mode_with_options`` reads from ``pk=fm#<id>``
    and the test patches it if needed.
    """
    import boto3
    table = boto3.resource("dynamodb", region_name=FUNCTION_REGION).Table(
        f"adp-{STAGE}-engineering-corpus"
    )
    table.put_item(Item={
        "pk": f"fm#{failure_mode_id}",
        "failure_mode_id": failure_mode_id,
        "component_name": component_name,
        "do_not_drive": do_not_drive,
        "park_outside": park_outside,
        "options": options,
    })
