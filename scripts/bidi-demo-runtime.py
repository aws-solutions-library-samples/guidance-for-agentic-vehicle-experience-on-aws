#!/usr/bin/env python3
"""Manage vsa_supervisor_bidi_demo_staging — an isolated bidi runtime for recordings.

Background (issues/2026-10-30-field-video-f-plans/plans.md § 4): the LinkedIn
recording took the shared staging runtime through 12 versions (v78–v90) in a day.
This script creates and maintains a separate demo runtime that recording builds can
target, isolating code versions from the shared staging runtime while sharing the
same staging DynamoDB tables and Bedrock Memory.

The demo runtime copies vsa_supervisor_bidi_staging's role, network, protocol,
lifecycle, env map, request-header allowlist, runtime, and entryPoint.
Its artifact lives at vsa_supervisor_bidi_demo_staging/deployment.zip in the same
versioned S3 bucket as the staging runtime.

Required interpreter
--------------------
  .venv/bin/python
  (botocore >= 1.43.6 — older builds lack metadataConfiguration on UpdateAgentRuntime)

Test command
------------
  .venv/bin/python \\
      -m pytest scripts/tests/test_bidi_demo_runtime.py -q --no-header

Subcommands
-----------
  plan            (default) Read-only.  Print the CreateAgentRuntime request that
                  would be sent and every field that differs from staging.
  create          Copy staging's current artifact, create the demo runtime, wait READY,
                  then issue a follow-up update to apply metadataConfiguration (which
                  CreateAgentRuntime cannot carry).
  push --zip PATH Upload a local zip to the demo S3 key, update the runtime artifact,
                  wait READY; print the new version number and S3 VersionId.
  sync-config     Re-read staging's current config and update the demo runtime's
                  non-artifact fields.  Keeps the demo artifact unchanged.
  teardown        Delete the demo runtime, wait for it to disappear, purge every S3
                  object version under the demo key prefix, and delete the
                  service-created log group.

All write subcommands are dry-run unless --apply is passed.

Guards
------
  * Never calls update or delete on a runtime whose name is not exactly
    "vsa_supervisor_bidi_demo_staging".
  * Refuses if staging's VSA_JWT_VERIFY_MODE is not "enforce" — prevents accidentally
    copying a relaxed-auth config to the demo runtime.
  * Refuses regions other than us-west-2 (staging region).
  * Refuses if the demo runtime's S3 artifact key is not the expected demo key
    (prevents push/update from overwriting staging's S3 artifact).
  * Refuses at module load if botocore lacks metadataConfiguration on
    UpdateAgentRuntime (prevents silent field omission on old interpreters).

Usage
-----
  # Inspect what create would send (env values replaced with <redacted>):
  python3 scripts/bidi-demo-runtime.py
  python3 scripts/bidi-demo-runtime.py plan

  # Create the demo runtime (copies staging artifact + full config):
  python3 scripts/bidi-demo-runtime.py create --apply

  # Push locally-built artifact to demo:
  python3 scripts/bidi-demo-runtime.py push --zip deployment.zip --apply

  # Re-sync staging's config fields onto the demo runtime:
  python3 scripts/bidi-demo-runtime.py sync-config --apply

  # Tear down the demo runtime and its S3 objects:
  python3 scripts/bidi-demo-runtime.py teardown --apply
"""
from __future__ import annotations

import argparse
import copy
import datetime
import json
import sys
import time
from pathlib import Path
from typing import Any

import boto3
from botocore.exceptions import ClientError


# ---------------------------------------------------------------------------
# Botocore compatibility gate  (W4)
# ---------------------------------------------------------------------------

def _require_botocore_metadata() -> None:
    """Raise RuntimeError if botocore lacks metadataConfiguration on UpdateAgentRuntime.

    sync-config and push include metadataConfiguration (requireMMDSV2) in every
    UpdateAgentRuntime call.  Botocore 1.42.49 (Homebrew Python default as of
    2026-10-30) does not model this field, so requests built on that interpreter
    silently omit it.  This check fails loudly rather than silently degrading MMDS
    settings.

    Required interpreter:
        .venv/bin/python
    Required botocore: >= 1.43.6
    Test command:
        .venv/bin/python \\
            -m pytest scripts/tests/test_bidi_demo_runtime.py -q --no-header
    """
    import botocore
    import botocore.session
    try:
        svc = botocore.session.Session().get_service_model("bedrock-agentcore-control")
        members = svc.operation_model("UpdateAgentRuntime").input_shape.members
    except Exception:
        return  # Service not in this botocore build; will fail at first AWS call
    if "metadataConfiguration" not in members:
        raise RuntimeError(
            f"botocore {botocore.__version__} is missing metadataConfiguration on "
            "UpdateAgentRuntime.  sync-config and push would silently omit MMDS "
            "settings on this interpreter.\n"
            "Required: botocore >= 1.43.6.\n"
            "Use the repo's .venv/bin/python (run from the repo root)."
        )


_require_botocore_metadata()


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

STAGING_NAME = "vsa_supervisor_bidi_staging"
DEMO_NAME = "vsa_supervisor_bidi_demo_staging"
STAGING_REGION = "us-west-2"

# Fields present on GetAgentRuntime that are copied into create/update requests.
# runtime + entryPoint live inside agentRuntimeArtifact and are handled separately.
# metadataConfiguration is NOT in CreateAgentRuntime input; included for updates only.
_COPY_ON_CREATE: frozenset[str] = frozenset({
    "roleArn",
    "networkConfiguration",
    "protocolConfiguration",
    "lifecycleConfiguration",
    "environmentVariables",
    "requestHeaderConfiguration",
    "description",
    "authorizerConfiguration",
    "filesystemConfigurations",
})
_COPY_ON_UPDATE: frozenset[str] = _COPY_ON_CREATE | frozenset({"metadataConfiguration"})

_POLL_INTERVAL = 10    # seconds between status polls
_POLL_TIMEOUT  = 600   # max seconds to wait for READY/*_FAILED


# ---------------------------------------------------------------------------
# Guard functions (called before any mutation)
# ---------------------------------------------------------------------------

def guard_target_name(name: str) -> None:
    """Raise ValueError if *name* is not exactly DEMO_NAME.

    Prevents accidentally updating or deleting the shared staging runtime.
    """
    if name != DEMO_NAME:
        raise ValueError(
            f"Operation refused: target runtime name is {name!r}; "
            f"only {DEMO_NAME!r} may be mutated by this script."
        )


def guard_jwt_enforce(env: dict[str, str]) -> None:
    """Raise ValueError if VSA_JWT_VERIFY_MODE is not 'enforce' in staging config.

    Prevents copying a relaxed-auth config to the demo runtime.
    """
    mode = env.get("VSA_JWT_VERIFY_MODE", "<not set>")
    if mode != "enforce":
        raise ValueError(
            f"Refused: staging VSA_JWT_VERIFY_MODE = {mode!r} — expected 'enforce'. "
            "Copying a non-enforcing config would relax auth on recording sessions."
        )


def guard_region(region: str) -> None:
    """Raise ValueError if region is not STAGING_REGION."""
    if region != STAGING_REGION:
        raise ValueError(
            f"Unknown region {region!r}: this script only supports "
            f"{STAGING_REGION!r} (staging region)."
        )


def guard_account(staging_arn: str, region: str) -> str:
    """Verify the ARN region matches *region* and return the account ID.

    ARN format: arn:partition:service:region:account:resource
    Raises ValueError if the ARN region doesn't match.
    """
    parts = staging_arn.split(":")
    if len(parts) < 5:
        raise ValueError(f"Cannot parse staging ARN: {staging_arn!r}")
    arn_region = parts[3]
    account = parts[4]
    if arn_region != region:
        raise ValueError(
            f"Staging runtime ARN region {arn_region!r} != requested region {region!r}."
        )
    return account


# ---------------------------------------------------------------------------
# Client factories (patched in tests)
# ---------------------------------------------------------------------------

def _make_control_client(region: str):
    return boto3.client("bedrock-agentcore-control", region_name=region)


def _make_s3_client(region: str):
    return boto3.client("s3", region_name=region)


def _make_logs_client(region: str):
    return boto3.client("logs", region_name=region)


# ---------------------------------------------------------------------------
# Low-level helpers
# ---------------------------------------------------------------------------

def find_runtime_by_name(control, name: str) -> dict | None:
    """Return the runtime summary dict from ListAgentRuntimes, or None if not found.

    Paginates automatically.  Returns the first runtime whose agentRuntimeName
    matches *name* exactly.
    """
    kwargs: dict = {}
    while True:
        resp = control.list_agent_runtimes(**kwargs)
        for rt in resp.get("agentRuntimes", []):
            if rt.get("agentRuntimeName") == name:
                return rt
        next_token = resp.get("nextToken")
        if not next_token:
            return None
        kwargs = {"nextToken": next_token}


def get_runtime(control, runtime_id: str) -> dict:
    """Return the full runtime config from GetAgentRuntime (ResponseMetadata stripped)."""
    resp = control.get_agent_runtime(agentRuntimeId=runtime_id)
    resp.pop("ResponseMetadata", None)
    return resp


def _get_staging_full(control) -> dict:
    """Find staging by name and return its full config; raises RuntimeError if absent."""
    summary = find_runtime_by_name(control, STAGING_NAME)
    if summary is None:
        raise RuntimeError(f"Staging runtime {STAGING_NAME!r} not found in this account/region.")
    return get_runtime(control, summary["agentRuntimeId"])


def _get_demo_full(control) -> dict | None:
    """Find demo by name and return its full config, or None if it does not exist."""
    summary = find_runtime_by_name(control, DEMO_NAME)
    if summary is None:
        return None
    return get_runtime(control, summary["agentRuntimeId"])


# ---------------------------------------------------------------------------
# Request builders
# ---------------------------------------------------------------------------

def _staging_artifact_to_demo(staging: dict, version_id: str | None) -> dict:
    """Build the demo agentRuntimeArtifact from staging's codeConfiguration.

    Copies runtime and entryPoint unchanged; replaces the S3 prefix with the
    demo key (vsa_supervisor_bidi_demo_staging/deployment.zip) and optionally
    pins the supplied *version_id*.
    """
    src = staging["agentRuntimeArtifact"]["codeConfiguration"]
    s3 = src["code"]["s3"]
    demo_s3: dict[str, Any] = {
        "bucket": s3["bucket"],
        "prefix": f"{DEMO_NAME}/deployment.zip",
    }
    if version_id is not None:
        demo_s3["versionId"] = version_id
    return {
        "codeConfiguration": {
            "code": {"s3": demo_s3},
            "runtime": src["runtime"],
            "entryPoint": src["entryPoint"],
        }
    }


def build_create_request(staging: dict, version_id: str | None) -> dict:
    """Build a CreateAgentRuntime request dict from *staging*'s config.

    Sets agentRuntimeName to DEMO_NAME, S3 prefix to the demo key, and a
    demo-specific description (never copies staging's description verbatim).
    metadataConfiguration is intentionally omitted (not in CreateAgentRuntime
    input shape; botocore rejects unknown fields) — a follow-up
    update_agent_runtime applies it after the runtime is READY.
    """
    staging_version = staging.get("agentRuntimeVersion", "?")
    utc_now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    req: dict[str, Any] = {
        "agentRuntimeName": DEMO_NAME,
        "agentRuntimeArtifact": _staging_artifact_to_demo(staging, version_id),
        # Demo-specific description — not staging's version history.
        "description": (
            f"Recording runtime; config copied from {STAGING_NAME} "
            f"v{staging_version} at {utc_now}"
        ),
    }
    for field in _COPY_ON_CREATE:
        if field in staging and field != "description":   # demo description set above
            req[field] = staging[field]
    return req


def build_update_artifact_request(
    demo_id: str,
    demo_runtime: dict,
    new_version_id: str | None,
    staging_bucket: str,
) -> dict:
    """Build an UpdateAgentRuntime request that refreshes only the artifact VersionId.

    Keeps every other field from the demo runtime's current config, so push does
    not inadvertently reset config fields.

    Raises ValueError if the demo artifact's S3 prefix is not the expected demo
    key or if the bucket does not match staging's bucket (guard against console
    edits or a misconfigured agentcore deploy -a that could redirect the demo
    artifact to staging's S3 key).
    """
    src = demo_runtime["agentRuntimeArtifact"]["codeConfiguration"]
    s3 = src["code"]["s3"]

    expected_prefix = f"{DEMO_NAME}/deployment.zip"
    if s3["prefix"] != expected_prefix:
        raise ValueError(
            f"build_update_artifact_request refused: demo artifact prefix "
            f"{s3['prefix']!r} != expected {expected_prefix!r}. "
            "This guard prevents overwriting the staging S3 key."
        )
    if s3["bucket"] != staging_bucket:
        raise ValueError(
            f"build_update_artifact_request refused: demo artifact bucket "
            f"{s3['bucket']!r} != staging bucket {staging_bucket!r}."
        )

    new_s3: dict[str, Any] = {
        "bucket": s3["bucket"],
        "prefix": s3["prefix"],  # keep the demo prefix
    }
    if new_version_id is not None:
        new_s3["versionId"] = new_version_id
    req: dict[str, Any] = {
        "agentRuntimeId": demo_id,
        "agentRuntimeArtifact": {
            "codeConfiguration": {
                "code": {"s3": new_s3},
                "runtime": src["runtime"],
                "entryPoint": src["entryPoint"],
            }
        },
    }
    for field in _COPY_ON_UPDATE:
        if field in demo_runtime:
            req[field] = demo_runtime[field]
    return req


def build_update_config_request(
    demo_id: str, staging: dict, demo_artifact: dict
) -> dict:
    """Build an UpdateAgentRuntime request that syncs staging config onto the demo.

    Keeps the demo's current *demo_artifact* unchanged (so the demo code version
    is not affected by a config sync).  Sets a fresh demo-specific description
    (never staging's verbatim description).
    """
    staging_version = staging.get("agentRuntimeVersion", "?")
    utc_now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    req: dict[str, Any] = {
        "agentRuntimeId": demo_id,
        "agentRuntimeArtifact": demo_artifact,
        # Demo-specific description — not staging's version history.
        "description": (
            f"Recording runtime; config copied from {STAGING_NAME} "
            f"v{staging_version} at {utc_now}"
        ),
    }
    for field in _COPY_ON_UPDATE:
        if field in staging and field != "description":   # demo description set above
            req[field] = staging[field]
    return req


# ---------------------------------------------------------------------------
# S3 helpers
# ---------------------------------------------------------------------------

def copy_staging_artifact(s3, staging: dict) -> tuple[str, str, str | None]:
    """Copy staging's current deployment.zip to the demo S3 key.

    Returns (bucket, demo_prefix, version_id).  version_id is None when
    the bucket does not have versioning enabled.  Prints the source version
    (CopySourceVersionId) so the record shows which bytes the demo got.
    """
    art = staging["agentRuntimeArtifact"]["codeConfiguration"]["code"]["s3"]
    bucket = art["bucket"]
    staging_prefix = art["prefix"]
    demo_prefix = f"{DEMO_NAME}/deployment.zip"

    copy_source: dict = {"Bucket": bucket, "Key": staging_prefix}
    if "versionId" in art:
        copy_source["VersionId"] = art["versionId"]

    resp = s3.copy_object(Bucket=bucket, Key=demo_prefix, CopySource=copy_source)
    version_id: str | None = resp.get("VersionId")
    src_version_id: str | None = resp.get("CopySourceVersionId")
    if src_version_id:
        print(f"  copiedFromVersionId: {src_version_id}")
    return bucket, demo_prefix, version_id


def upload_artifact(
    s3, demo_runtime: dict, zip_path: Path, staging_bucket: str
) -> tuple[str, str, str | None]:
    """Upload a local zip file to the demo S3 key.

    Raises ValueError if the demo artifact's S3 prefix is not the expected demo
    key or if the bucket does not match *staging_bucket* (guard against console
    edits that could redirect the demo artifact to staging's S3 key).

    Returns (bucket, demo_prefix, version_id).
    """
    art = demo_runtime["agentRuntimeArtifact"]["codeConfiguration"]["code"]["s3"]
    bucket = art["bucket"]
    prefix = art["prefix"]

    expected_prefix = f"{DEMO_NAME}/deployment.zip"
    if prefix != expected_prefix:
        raise ValueError(
            f"upload_artifact refused: demo artifact prefix {prefix!r} != "
            f"expected {expected_prefix!r}. Uploading here would overwrite "
            "the staging artifact key."
        )
    if bucket != staging_bucket:
        raise ValueError(
            f"upload_artifact refused: demo artifact bucket {bucket!r} != "
            f"staging bucket {staging_bucket!r}."
        )

    resp = s3.put_object(Bucket=bucket, Key=prefix, Body=zip_path.read_bytes())
    version_id: str | None = resp.get("VersionId")
    return bucket, prefix, version_id


def delete_all_s3_versions(s3, bucket: str, prefix: str) -> int:
    """Delete all object versions and delete markers under *prefix*.

    Paginates list_object_versions automatically.  Raises RuntimeError if
    delete_objects reports per-key errors.  Returns the total count of
    objects/markers deleted.
    """
    total = 0
    list_kwargs: dict = {"Bucket": bucket, "Prefix": prefix}
    while True:
        resp = s3.list_object_versions(**list_kwargs)
        batch: list[dict] = []
        for ver in resp.get("Versions") or []:
            batch.append({"Key": ver["Key"], "VersionId": ver["VersionId"]})
        for dm in resp.get("DeleteMarkers") or []:
            batch.append({"Key": dm["Key"], "VersionId": dm["VersionId"]})

        if batch:
            del_resp = s3.delete_objects(
                Bucket=bucket,
                Delete={"Objects": batch, "Quiet": True},
            )
            errors = (del_resp or {}).get("Errors") or []
            if errors:
                raise RuntimeError(
                    f"s3.delete_objects reported {len(errors)} error(s) "
                    f"under s3://{bucket}/{prefix}: {errors[:3]}"
                )
            total += len(batch)

        if not resp.get("IsTruncated"):
            break
        list_kwargs["KeyMarker"] = resp["NextKeyMarker"]
        list_kwargs["VersionIdMarker"] = resp["NextVersionIdMarker"]

    return total


# ---------------------------------------------------------------------------
# Polling helper
# ---------------------------------------------------------------------------

def wait_ready(control, runtime_id: str, timeout: int = _POLL_TIMEOUT) -> dict:
    """Poll GetAgentRuntime until status is READY or *_FAILED.

    Returns the final GetAgentRuntime response.  Raises RuntimeError if the
    runtime enters any *_FAILED state (CREATE_FAILED, UPDATE_FAILED,
    DELETE_FAILED), or TimeoutError if it does not reach READY within
    *timeout* seconds.
    """
    deadline = time.monotonic() + timeout
    while True:
        resp = get_runtime(control, runtime_id)
        status = resp.get("status", "UNKNOWN")
        print(f"  status: {status}", flush=True)
        if status == "READY":
            return resp
        if status.endswith("_FAILED"):
            reason = resp.get("failureReason") or "<no reason given>"
            raise RuntimeError(
                f"Runtime {runtime_id!r} entered {status!r} state: {reason}"
            )
        if time.monotonic() > deadline:
            raise TimeoutError(
                f"Runtime {runtime_id!r} did not reach READY within {timeout}s "
                f"(last status: {status!r})."
            )
        time.sleep(_POLL_INTERVAL)


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------

def _redact_env(req: dict) -> dict:
    """Return a deep copy of *req* with environmentVariables values replaced."""
    safe = copy.deepcopy(req)
    env = safe.get("environmentVariables")
    if env:
        safe["environmentVariables"] = {k: "<redacted>" for k in env}
    return safe


def _print_request_redacted(req: dict) -> None:
    """Print the request as JSON with env values replaced by <redacted>."""
    print(json.dumps(_redact_env(req), indent=2, default=str))


def _diff_from_staging(staging: dict, create_req: dict) -> list[str]:
    """Return human-readable lines for fields that differ from staging.

    Covers the create request fields plus fields that CreateAgentRuntime cannot
    carry (e.g. metadataConfiguration), which the follow-up update applies.
    """
    diffs: list[str] = []

    old_name = staging.get("agentRuntimeName", STAGING_NAME)
    new_name = create_req.get("agentRuntimeName", DEMO_NAME)
    if old_name != new_name:
        diffs.append(f"  agentRuntimeName: {old_name!r} → {new_name!r}")

    try:
        old_prefix = (
            staging["agentRuntimeArtifact"]["codeConfiguration"]["code"]["s3"]["prefix"]
        )
        new_prefix = (
            create_req["agentRuntimeArtifact"]["codeConfiguration"]["code"]["s3"]["prefix"]
        )
        if old_prefix != new_prefix:
            diffs.append(
                f"  agentRuntimeArtifact.codeConfiguration.code.s3.prefix:\n"
                f"    {old_prefix!r} → {new_prefix!r}"
            )
    except KeyError:
        pass

    # Description: create sets a demo-specific value, never staging's.
    old_desc = staging.get("description", "<not set>")
    new_desc = create_req.get("description", "<not set>")
    if old_desc != new_desc:
        diffs.append(
            f"  description:\n"
            f"    staging: {old_desc!r}\n"
            f"    demo:    {new_desc!r}"
        )

    # metadataConfiguration: not in CreateAgentRuntime; applied by follow-up update.
    if "metadataConfiguration" in staging:
        meta = staging["metadataConfiguration"]
        diffs.append(
            f"  metadataConfiguration: {meta!r}\n"
            "    (cannot be set on create — applied by follow-up update_agent_runtime after READY)"
        )

    return diffs


# ---------------------------------------------------------------------------
# Command implementations
# ---------------------------------------------------------------------------

def cmd_plan(control) -> int:
    """Read-only: print what create would send and every field differing from staging."""
    print(f"Reading staging runtime: {STAGING_NAME}")
    staging = _get_staging_full(control)

    # Warn about JWT mode without refusing (plan is informational).
    env = staging.get("environmentVariables") or {}
    mode = env.get("VSA_JWT_VERIFY_MODE", "<not set>")
    if mode != "enforce":
        print(
            f"⚠️  staging VSA_JWT_VERIFY_MODE = {mode!r} — "
            "create/sync-config would be refused until staging is in enforce mode.",
            file=sys.stderr,
        )

    create_req = build_create_request(staging, version_id=None)

    demo = _get_demo_full(control)
    if demo is None:
        print(f"\nDemo runtime {DEMO_NAME!r} does NOT exist — would create.")
    else:
        print(
            f"\nDemo runtime {DEMO_NAME!r} already EXISTS (id={demo['agentRuntimeId']}, "
            f"v{demo.get('agentRuntimeVersion')})."
        )
        print(f"  arn:  {demo.get('agentRuntimeArn')}")
        print("Use 'push' to update the artifact, or 'sync-config' to pull staging config.")

    print("\n--- CreateAgentRuntime request (DRY RUN, env values redacted) ---")
    _print_request_redacted(create_req)

    diffs = _diff_from_staging(staging, create_req)
    if diffs:
        print(f"\nFields that differ from {STAGING_NAME}:")
        print("\n".join(diffs))

    env_names = sorted((staging.get("environmentVariables") or {}).keys())
    print(f"\nEnvironment variables ({len(env_names)} keys, names only):")
    print("  " + ", ".join(env_names))

    print("\n(dry-run — no changes made)")
    return 0


def cmd_create(control, s3, apply: bool) -> int:
    """Copy staging's artifact and create the demo runtime.

    After the runtime reaches READY, issues a follow-up update_agent_runtime to
    apply metadataConfiguration (which CreateAgentRuntime cannot carry).
    """
    staging = _get_staging_full(control)
    guard_jwt_enforce(staging.get("environmentVariables") or {})

    existing = _get_demo_full(control)
    if existing is not None:
        print(
            f"✗ Demo runtime {DEMO_NAME!r} already exists "
            f"(id={existing['agentRuntimeId']}). "
            "Use 'push' to update the artifact, or run 'teardown' first.",
            file=sys.stderr,
        )
        return 1

    create_req_preview = build_create_request(staging, version_id=None)
    print("--- CreateAgentRuntime request (env values redacted) ---")
    _print_request_redacted(create_req_preview)

    diffs = _diff_from_staging(staging, create_req_preview)
    if diffs:
        print(f"\nFields that differ from {STAGING_NAME}:")
        print("\n".join(diffs))

    if not apply:
        print("\n(dry-run: pass --apply to execute)")
        return 0

    # Copy staging artifact first.
    art = staging["agentRuntimeArtifact"]["codeConfiguration"]["code"]["s3"]
    print(
        f"\n→ Copying artifact "
        f"s3://{art['bucket']}/{art['prefix']}"
        f" → {DEMO_NAME}/deployment.zip"
    )
    bucket, demo_prefix, version_id = copy_staging_artifact(s3, staging)
    print(f"  bucket:     {bucket}")
    print(f"  prefix:     {demo_prefix}")
    print(f"  versionId:  {version_id}")

    create_req = build_create_request(staging, version_id=version_id)
    print(f"\n→ Creating runtime {DEMO_NAME!r}...")
    resp = control.create_agent_runtime(**create_req)
    runtime_id = resp["agentRuntimeId"]
    print(f"  id:         {runtime_id}")
    print(f"  arn:        {resp['agentRuntimeArn']}")
    print(f"  status:     {resp['status']}")

    print(f"\n→ Waiting for READY (timeout {_POLL_TIMEOUT}s)...")
    wait_ready(control, runtime_id)
    print(f"\n✓ {DEMO_NAME!r} is READY")

    # Follow-up update: apply metadataConfiguration and other _COPY_ON_UPDATE fields
    # that CreateAgentRuntime cannot carry.
    if "metadataConfiguration" in staging:
        print(
            f"\n→ Applying post-create config update "
            f"(metadataConfiguration cannot be set on create)..."
        )
        post_update_req = build_update_config_request(
            runtime_id, staging, create_req["agentRuntimeArtifact"]
        )
        resp2 = control.update_agent_runtime(**post_update_req)
        new_version = resp2.get("agentRuntimeVersion")
        print(f"  version:    {new_version}")
        print(f"  status:     {resp2.get('status')}")
        print(f"\n→ Waiting for READY (timeout {_POLL_TIMEOUT}s)...")
        final = wait_ready(control, runtime_id)
        print(f"  ✓ Post-create config applied (v{new_version})")
    else:
        final = get_runtime(control, runtime_id)

    print(f"\n✓ {DEMO_NAME!r} fully configured")
    print(f"  arn: {final['agentRuntimeArn']}")
    return 0


def cmd_push(control, s3, zip_path: Path, apply: bool) -> int:
    """Upload a local zip and update the demo runtime's artifact."""
    if not zip_path.exists():
        print(f"✗ Zip not found: {zip_path}", file=sys.stderr)
        return 1

    demo = _get_demo_full(control)
    if demo is None:
        print(
            f"✗ Demo runtime {DEMO_NAME!r} does not exist. Run 'create' first.",
            file=sys.stderr,
        )
        return 1

    demo_id = demo["agentRuntimeId"]
    guard_target_name(demo["agentRuntimeName"])

    art = demo["agentRuntimeArtifact"]["codeConfiguration"]["code"]["s3"]
    bucket = art["bucket"]
    prefix = art["prefix"]
    size = zip_path.stat().st_size
    print(f"  demo id:    {demo_id}")
    print(f"  zip:        {zip_path}  ({size:,} bytes)")
    print(f"  bucket:     {bucket}")
    print(f"  key:        {prefix}")

    if not apply:
        print("\n(dry-run: pass --apply to execute)")
        return 0

    # Read staging's bucket for the S3 guard.
    staging = _get_staging_full(control)
    staging_bucket = (
        staging["agentRuntimeArtifact"]["codeConfiguration"]["code"]["s3"]["bucket"]
    )

    print(f"\n→ Uploading {zip_path.name}...")
    _, _, version_id = upload_artifact(s3, demo, zip_path, staging_bucket=staging_bucket)
    print(f"  versionId:  {version_id}")

    update_req = build_update_artifact_request(
        demo_id, demo, version_id, staging_bucket=staging_bucket
    )
    print(f"\n→ Updating runtime {DEMO_NAME!r}...")
    resp = control.update_agent_runtime(**update_req)
    new_version = resp.get("agentRuntimeVersion")
    print(f"  version:    {new_version}")
    print(f"  status:     {resp.get('status')}")

    print(f"\n→ Waiting for READY (timeout {_POLL_TIMEOUT}s)...")
    wait_ready(control, demo_id)
    print(f"\n✓ Push complete — versionId={version_id!r}, agentRuntimeVersion={new_version!r}")
    return 0


def cmd_sync_config(control, apply: bool) -> int:
    """Sync staging's config fields onto the demo runtime; keep demo artifact."""
    staging = _get_staging_full(control)
    guard_jwt_enforce(staging.get("environmentVariables") or {})

    demo = _get_demo_full(control)
    if demo is None:
        print(
            f"✗ Demo runtime {DEMO_NAME!r} does not exist. Run 'create' first.",
            file=sys.stderr,
        )
        return 1

    demo_id = demo["agentRuntimeId"]
    guard_target_name(demo["agentRuntimeName"])

    demo_artifact = demo["agentRuntimeArtifact"]
    update_req = build_update_config_request(demo_id, staging, demo_artifact)

    print(f"  staging:    {STAGING_NAME} v{staging.get('agentRuntimeVersion')}")
    print(f"  demo:       {demo_id} v{demo.get('agentRuntimeVersion')}")
    print("\n--- UpdateAgentRuntime request (config fields, env values redacted) ---")
    _print_request_redacted(update_req)

    if not apply:
        print("\n(dry-run: pass --apply to execute)")
        return 0

    print(f"\n→ Syncing config from {STAGING_NAME}...")
    resp = control.update_agent_runtime(**update_req)
    new_version = resp.get("agentRuntimeVersion")
    print(f"  version:    {new_version}")
    print(f"  status:     {resp.get('status')}")

    print(f"\n→ Waiting for READY (timeout {_POLL_TIMEOUT}s)...")
    wait_ready(control, demo_id)
    print(f"\n✓ Config synced from {STAGING_NAME}")
    return 0


def cmd_teardown(control, s3, logs, apply: bool) -> int:
    """Delete the demo runtime and all S3 object versions under the demo prefix.

    Always purges the demo S3 prefix even if the runtime does not exist (a failed
    create leaves the artifact behind).  Raises on S3 delete errors.  Polls until
    the runtime is gone or enters DELETE_FAILED.  Deletes the service-created log
    group /aws/bedrock-agentcore/runtimes/{id}-DEFAULT.
    """
    demo = _get_demo_full(control)

    if demo is not None:
        demo_id = demo["agentRuntimeId"]
        guard_target_name(demo["agentRuntimeName"])
        art = demo["agentRuntimeArtifact"]["codeConfiguration"]["code"]["s3"]
        bucket = art["bucket"]
    else:
        print(
            f"Demo runtime {DEMO_NAME!r} does not exist — "
            "checking S3 for leftover objects."
        )
        staging = _get_staging_full(control)
        bucket = staging["agentRuntimeArtifact"]["codeConfiguration"]["code"]["s3"]["bucket"]
        demo_id = None

    s3_prefix = f"{DEMO_NAME}/"   # covers deployment.zip and any companion files

    if demo_id:
        print(f"  demo id:    {demo_id}")
        print(f"  demo arn:   {demo.get('agentRuntimeArn')}")
    print(f"  s3 bucket:  {bucket}")
    print(f"  s3 prefix:  {s3_prefix}")

    if not apply:
        print(
            "\n(dry-run: would delete the runtime and all S3 object versions "
            f"under s3://{bucket}/{s3_prefix})\n"
            "Pass --apply to execute."
        )
        return 0

    # Step 1: Delete the runtime (if present) and wait for it to disappear.
    if demo_id:
        print(f"\n→ Deleting runtime {DEMO_NAME!r} (id={demo_id})...")
        control.delete_agent_runtime(agentRuntimeId=demo_id)
        print("  ✓ Delete request submitted")

        print(
            f"\n→ Waiting for {DEMO_NAME!r} to disappear from list "
            f"(timeout {_POLL_TIMEOUT}s)..."
        )
        deadline = time.monotonic() + _POLL_TIMEOUT
        while True:
            summary = find_runtime_by_name(control, DEMO_NAME)
            if summary is None:
                print("  ✓ Runtime no longer in list")
                break
            status = summary.get("status", "UNKNOWN")
            print(f"  status: {status}", flush=True)
            if status.endswith("_FAILED"):
                raise RuntimeError(
                    f"Runtime {demo_id!r} entered {status!r} during teardown — "
                    "manual cleanup may be required."
                )
            if time.monotonic() > deadline:
                raise TimeoutError(
                    f"Runtime {demo_id!r} still in list after {_POLL_TIMEOUT}s "
                    f"(last status: {status!r}) — teardown may be stuck."
                )
            time.sleep(_POLL_INTERVAL)

    # Step 2: Purge demo S3 prefix (always, even if runtime was absent).
    print(f"\n→ Deleting all S3 versions under s3://{bucket}/{s3_prefix}...")
    count = delete_all_s3_versions(s3, bucket, s3_prefix)
    print(f"  ✓ Deleted {count} object version(s)/delete-marker(s)")

    # Step 3: Delete service-created log group (only if runtime existed).
    if demo_id:
        log_group = f"/aws/bedrock-agentcore/runtimes/{demo_id}-DEFAULT"
        print(f"\n→ Deleting log group {log_group!r}...")
        try:
            logs.delete_log_group(logGroupName=log_group)
            print("  ✓ Log group deleted")
        except logs.exceptions.ResourceNotFoundException:
            print(f"  (log group {log_group!r} not found — already deleted or never created)")

    print(f"\n✓ Teardown complete.")
    return 0


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "subcommand",
        nargs="?",
        default="plan",
        choices=["plan", "create", "push", "sync-config", "teardown"],
        help="Subcommand to run (default: plan).",
    )
    p.add_argument(
        "--apply",
        action="store_true",
        help="Execute changes; without this flag all write commands are dry-run.",
    )
    p.add_argument(
        "--zip",
        type=Path,
        default=None,
        metavar="PATH",
        help="Path to a deployment.zip for the 'push' subcommand.",
    )
    p.add_argument(
        "--region",
        default=STAGING_REGION,
        help=f"AWS region (only {STAGING_REGION!r} accepted; default: {STAGING_REGION}).",
    )
    args = p.parse_args(argv)

    # Region guard: fail before touching any AWS service.
    try:
        guard_region(args.region)
    except ValueError as exc:
        print(f"✗ {exc}", file=sys.stderr)
        return 1

    control = _make_control_client(args.region)

    # Verify we're in the expected account by reading the staging runtime's ARN.
    staging_summary = find_runtime_by_name(control, STAGING_NAME)
    if staging_summary is not None:
        try:
            guard_account(staging_summary["agentRuntimeArn"], args.region)
        except ValueError as exc:
            print(f"✗ {exc}", file=sys.stderr)
            return 1

    try:
        if args.subcommand == "plan":
            return cmd_plan(control)

        s3 = _make_s3_client(args.region)

        if args.subcommand == "create":
            return cmd_create(control, s3, apply=args.apply)

        if args.subcommand == "push":
            if args.zip is None:
                print("✗ 'push' requires --zip PATH", file=sys.stderr)
                return 1
            return cmd_push(control, s3, zip_path=args.zip, apply=args.apply)

        if args.subcommand == "sync-config":
            return cmd_sync_config(control, apply=args.apply)

        if args.subcommand == "teardown":
            logs = _make_logs_client(args.region)
            return cmd_teardown(control, s3, logs, apply=args.apply)

        print(f"✗ Unknown subcommand: {args.subcommand!r}", file=sys.stderr)
        return 1

    except ValueError as exc:
        print(f"✗ {exc}", file=sys.stderr)
        return 1
    except ClientError as exc:
        print(f"✗ AWS error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
