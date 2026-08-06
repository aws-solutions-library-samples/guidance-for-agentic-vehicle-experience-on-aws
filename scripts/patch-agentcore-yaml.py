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
import sys
from pathlib import Path

import boto3
import yaml
from botocore.exceptions import BotoCoreError, ClientError

REPO_ROOT = Path(__file__).resolve().parent.parent
YAML_PATH = REPO_ROOT / ".bedrock_agentcore.yaml"


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--agent", required=True, help="Agent key in .bedrock_agentcore.yaml")
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

    YAML_PATH.write_text(yaml.safe_dump(config, sort_keys=False))

    if before_role == role_arn and before_s3 == new_s3_path:
        print(f"= {args.agent}: execution_role and s3_path already up to date")
    else:
        print(f"✓ Patched {args.agent}: execution_role={role_arn}, s3_path={new_s3_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
