#!/usr/bin/env python3
"""Patch .bedrock_agentcore.yaml with CFN stack outputs.

Reads `AgentCoreExecutionRoleArn` and `AgentCoreS3Bucket` outputs from a
named CloudFormation stack/region, and writes them into the named agent's
`aws.execution_role` and `aws.s3_path` fields.

Idempotent: re-running with the same outputs produces no semantic change
to the file. Re-running after CDK output values change updates the yaml
in place.

Preserves all other agent entries (no diff to siblings).

Usage:
    python3 scripts/patch-agentcore-yaml.py \\
        --agent vsa_supervisor_bidi_staging \\
        --stack vsa-staging-core \\
        --region us-west-2

Exits 0 on success. Exits 2 on:
    - CFN stack not found / required outputs missing
    - Agent entry not found in yaml
    - YAML parse error
    - .bedrock_agentcore.yaml not found

Spec: .kiro/specs/2026-05-28-agentcore-staging-infra/
"""

from __future__ import annotations

import argparse
import importlib.util
import sys
from pathlib import Path

import boto3
import yaml
from botocore.exceptions import BotoCoreError, ClientError

REPO_ROOT = Path(__file__).resolve().parent.parent
YAML_PATH = REPO_ROOT / ".bedrock_agentcore.yaml"


_HEADER_PREFIX_WIRE = "X-Amzn-Bedrock-AgentCore-Runtime-Custom-"


def required_allowlist_entries(entrypoint: Path) -> list[str]:
    """Wire-name allowlist entries this entrypoint's code requires.

    Derived from the entrypoint by the verifier's own AST scan — single source of
    truth, so this cannot drift from what the runtime actually reads. Declared
    gaps in the verifier are excluded deliberately: they are headers the code
    reads that we have decided NOT to deliver yet, each with a recorded reason.

    Raises if a read group has no declared wire name, rather than guessing:
    `AvxFindingId` is not recoverable from the lowercased literal
    `avxfindingid`, and inventing a spelling would produce an allowlist entry
    that silently matches nothing.
    """
    spec = importlib.util.spec_from_file_location(
        "_verify_allowlist", REPO_ROOT / "scripts" / "verify_agentcore_header_allowlist.py"
    )
    v = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(v)

    read = v.derive_read_headers(entrypoint)
    entries: list[str] = []
    undeclared: list[str] = []
    for name in sorted(read):
        if name in v._KNOWN_GAPS:
            continue
        wire = v._WIRE_NAMES.get(name)
        if not wire:
            undeclared.append(name)
            continue
        entries.append(_HEADER_PREFIX_WIRE + wire)
    if undeclared:
        raise RuntimeError(
            f"no wire name declared for {undeclared}. Add them to _WIRE_NAMES in "
            "scripts/verify_agentcore_header_allowlist.py — the camelCase spelling "
            "cannot be derived from the lowercased literal, and guessing produces an "
            "allowlist entry that matches nothing."
        )
    return entries


def reconcile_header_allowlist(agent_block: dict, agent: str, entrypoint: Path) -> list[str]:
    """Ensure the agent's requestHeaderAllowlist covers every header the code reads.

    Returns the list of entries ADDED (empty when already correct, so the caller
    can stay quiet — this runs on every deploy).

    This is the point of the whole exercise: `.bedrock_agentcore.yaml` is
    gitignored, so the allowlist cannot be committed, and AgentCore drops
    unlisted headers with NO error. Previously every developer and deploy path
    had to hand-add new entries, and forgetting produced a silent feature
    regression. Now the deploy maintains it.

    Additive only. Never removes an entry: an unrecognised one may be a sibling
    runtime's, hand-added for a local experiment, or ahead of this checkout.
    """
    rhc = agent_block.get("request_header_configuration")
    if rhc is None:
        # Text/HTTP runtime — the custom-header namespace is documented for
        # WebSocket runtimes only, so there is nothing to reconcile and adding
        # a block would imply a capability that does not exist.
        return []

    existing = list(rhc.get("requestHeaderAllowlist") or [])
    have = {e.lower() for e in existing}
    added = [e for e in required_allowlist_entries(entrypoint) if e.lower() not in have]
    if added:
        rhc["requestHeaderAllowlist"] = existing + added
    return added


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--agent", required=True, help="Agent key in .bedrock_agentcore.yaml")
    p.add_argument("--entrypoint", default=None,
                   help="Path (repo-relative) to the runtime entrypoint whose custom-header "
                        "constants drive the requestHeaderAllowlist reconcile. Omit to skip.")
    p.add_argument("--stack", required=True, help="CloudFormation stack name (e.g. vsa-staging-core)")
    p.add_argument("--region", required=True, help="CFN stack region (e.g. us-west-2)")
    args = p.parse_args()

    if not YAML_PATH.exists():
        print(f"✗ {YAML_PATH} does not exist", file=sys.stderr)
        return 2

    try:
        config = yaml.safe_load(YAML_PATH.read_text())
    except yaml.YAMLError as exc:
        print(f"✗ Failed to parse {YAML_PATH}: {exc}", file=sys.stderr)
        return 2

    agents = (config or {}).get("agents") or {}
    if args.agent not in agents:
        print(f"✗ Agent '{args.agent}' not found in {YAML_PATH} (keys: {sorted(agents)})", file=sys.stderr)
        return 2

    cfn = boto3.client("cloudformation", region_name=args.region)
    try:
        resp = cfn.describe_stacks(StackName=args.stack)
    except (BotoCoreError, ClientError) as exc:
        print(f"✗ describe_stacks failed for {args.stack} in {args.region}: {exc}", file=sys.stderr)
        return 2

    stacks = resp.get("Stacks") or []
    if not stacks:
        print(f"✗ Stack {args.stack} returned empty Stacks list", file=sys.stderr)
        return 2

    outputs = {o["OutputKey"]: o["OutputValue"] for o in stacks[0].get("Outputs") or []}
    role_arn = outputs.get("AgentCoreExecutionRoleArn")
    bucket = outputs.get("AgentCoreS3Bucket")
    missing = [k for k, v in (("AgentCoreExecutionRoleArn", role_arn), ("AgentCoreS3Bucket", bucket)) if not v]
    if missing:
        print(
            f"✗ Stack {args.stack} missing required outputs: {missing}. "
            f"Found outputs: {sorted(outputs)}",
            file=sys.stderr,
        )
        return 2

    aws_block = agents[args.agent]["aws"]
    new_s3_path = f"s3://{bucket}"
    before_role = aws_block.get("execution_role")
    before_s3 = aws_block.get("s3_path")
    aws_block["execution_role"] = role_arn
    aws_block["s3_path"] = new_s3_path

    # Reconcile the custom-header allowlist from the code the runtime actually
    # runs. See reconcile_header_allowlist's docstring for why this belongs in
    # the deploy path rather than in a README instruction.
    added: list[str] = []
    if args.entrypoint:
        entrypoint = REPO_ROOT / args.entrypoint
        if not entrypoint.exists():
            print(f"✗ --entrypoint not found: {entrypoint}", file=sys.stderr)
            return 2
        try:
            added = reconcile_header_allowlist(agents[args.agent], args.agent, entrypoint)
        except RuntimeError as exc:
            print(f"✗ header allowlist reconcile failed: {exc}", file=sys.stderr)
            return 2

    YAML_PATH.write_text(yaml.safe_dump(config, sort_keys=False))

    if added:
        print(f"✓ Added {len(added)} custom-header allowlist entry/entries to {args.agent}:")
        for entry in added:
            print(f"    + {entry}")
        print("  (AgentCore drops unlisted headers with no error; this file is")
        print("   gitignored, so the deploy maintains it rather than each developer.)")
    elif args.entrypoint:
        print(f"= {args.agent}: custom-header allowlist already covers the code's headers")

    if before_role == role_arn and before_s3 == new_s3_path:
        print(f"= {args.agent}: execution_role and s3_path already up to date")
    else:
        print(f"✓ Patched {args.agent}: execution_role={role_arn}, s3_path={new_s3_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
