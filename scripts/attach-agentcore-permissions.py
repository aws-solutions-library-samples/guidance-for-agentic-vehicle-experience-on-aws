#!/usr/bin/env python3
"""Attach extra IAM permissions to the AgentCore execution role.

The `agentcore configure` / `agentcore deploy` toolkit creates an
execution role with baseline permissions (CloudWatch logs, ECR pull,
etc.), but does NOT know our agent needs:

  - bedrock:InvokeModel on the Claude Sonnet 4.6 US inference profile
    and its underlying foundation models in us-east-1, us-east-2, us-west-2.
  - bedrock:InvokeModelWithBidirectionalStream on Nova Sonic v2.
  - DynamoDB read/write against the CMS demo tables so `book()` can
    persist scheduled service bookings into
    `cms-prod-storage-service-history` (Option A per 2026-04-30 plan),
    and `triage()` / `book()` can enrich envelopes with real vehicle
    and driver data from `cms-prod-storage-vehicles` and
    `cms-prod-storage-drivers`.
  - kms:Decrypt / kms:GenerateDataKey on the CMS KMS key, since all
    three CMS tables use SSE-KMS.

This script reads .bedrock_agentcore.yaml to find the execution role
ARN the toolkit assigned, then attaches an inline policy with the
permissions above. Idempotent — re-running replaces the same-named
inline policy in place.

Run after `agentcore configure` and before the first invocation. Or
re-run any time the role drifts.

Usage:
    python scripts/attach-agentcore-permissions.py
    python scripts/attach-agentcore-permissions.py --role-name <explicit-role>
    python scripts/attach-agentcore-permissions.py --dry-run

We do NOT add VPC/egress perms — AgentCore runtime has outbound
internet by default, so calls to execute-api.us-east-1.amazonaws.com
for the triage tool work without extra IAM.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import boto3
import yaml

# Matches the inference profile in agents/supervisor/config.py default.
INFERENCE_PROFILE_ID = "us.anthropic.claude-sonnet-4-6"
FOUNDATION_MODEL_ID = "anthropic.claude-sonnet-4-6"
# The "us." prefix profile routes to these three regions.
INFERENCE_REGIONS = ["us-east-1", "us-east-2", "us-west-2"]
# Nova Sonic v2 foundation model — per Strands NOVA_SONIC_V2_MODEL_ID.
# Note the naming: the service is "Nova-2-Sonic" and this is v1 of it.
NOVA_SONIC_MODEL_ID = "amazon.nova-2-sonic-v1:0"
NOVA_SONIC_REGIONS = ["us-east-1", "us-west-2"]  # both regions where runtimes deploy
POLICY_NAME = "VsaAgentCoreSupervisorBedrockAccess"

# CMS DynamoDB tables the supervisor needs access to. book() writes
# scheduled service rows into service-history; triage() and book() read
# vehicles + drivers to enrich envelopes with real data.
#
# Region is inferred at policy-build time from the caller's profile
# (all three tables live in us-east-1 as of 2026-04-30). If CMS ever
# replicates to another region we'll want to change this to a list.
CMS_REGION = "us-east-1"
CMS_TABLES_WRITE = [
    "cms-prod-storage-service-history",  # book() PutItem target
]
CMS_TABLES_READ = [
    "cms-prod-storage-vehicles",         # VIN -> vehicleId + make/model/odometer
    "cms-prod-storage-drivers",          # driverId -> name/email/phone/safety
    "cms-prod-storage-service-history",  # iOS "scheduled service" list endpoint
]
# All CMS demo tables share this KMS CMK (see deployment/stacks/storage_stack.py).
# Confirmed 2026-04-30 across service-history / vehicles / drivers.
CMS_KMS_KEY_ARN = os.environ.get(
    "CMS_KMS_KEY_ARN",
    "arn:aws:kms:us-east-1:YOUR_ACCOUNT_ID:key/YOUR_KEY_ID",
)
REPO_ROOT = Path(__file__).resolve().parent.parent
AGENTCORE_CONFIG_PATH = REPO_ROOT / ".bedrock_agentcore.yaml"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--agent-name",
        default=None,
        help=(
            "Name of the agent in .bedrock_agentcore.yaml whose execution role "
            "to patch. If omitted, the 'default_agent' entry is used. Required "
            "when there are multiple agents (e.g. vsa_supervisor vs "
            "vsa_supervisor_bidi)."
        ),
    )
    parser.add_argument(
        "--role-name",
        help=(
            "IAM role name to attach policy to. Auto-discovered from "
            ".bedrock_agentcore.yaml if not provided."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the policy JSON and planned PutRolePolicy call; do not execute",
    )
    parser.add_argument(
        "--region",
        default="us-east-1",
        help="AWS region for IAM (IAM is global, but boto session needs one)",
    )
    args = parser.parse_args()

    role_name = args.role_name or _discover_role_name(agent_name=args.agent_name)
    if not role_name:
        print(
            "✗ Could not find execution role. Run `agentcore configure` first, "
            "or pass --role-name explicitly.",
            file=sys.stderr,
        )
        return 2

    account_id = _resolve_account_id(args.region)
    policy_doc = _build_policy(account_id)

    print(f"→ Role:       {role_name}")
    print(f"→ Account:    {account_id}")
    print(f"→ Policy:     {POLICY_NAME}")
    print(f"→ Claude:     {INFERENCE_PROFILE_ID}")
    print(f"→ Regions:    {INFERENCE_REGIONS}")
    print(f"→ Nova Sonic: {NOVA_SONIC_MODEL_ID} ({NOVA_SONIC_REGIONS})")
    print(f"→ CMS write:  {CMS_TABLES_WRITE}")
    print(f"→ CMS read:   {CMS_TABLES_READ}")
    print(f"→ CMS KMS:    {CMS_KMS_KEY_ARN}")
    print()

    if args.dry_run:
        print("--- Policy document (dry-run) ---")
        print(json.dumps(policy_doc, indent=2))
        return 0

    iam = boto3.client("iam", region_name=args.region)
    iam.put_role_policy(
        RoleName=role_name,
        PolicyName=POLICY_NAME,
        PolicyDocument=json.dumps(policy_doc),
    )
    print(f"✓ Attached {POLICY_NAME} to role {role_name}")
    return 0


def _discover_role_name(agent_name: str | None = None) -> str | None:
    """Parse .bedrock_agentcore.yaml to find the toolkit-assigned role.

    If agent_name is given, look up that specific entry. Otherwise use
    the default_agent key, and fall back to the first agent found.
    """
    if not AGENTCORE_CONFIG_PATH.exists():
        return None
    try:
        config = yaml.safe_load(AGENTCORE_CONFIG_PATH.read_text())
    except yaml.YAMLError as e:
        print(f"✗ Failed to parse {AGENTCORE_CONFIG_PATH}: {e}", file=sys.stderr)
        return None

    agents = (config or {}).get("agents") or {}
    if not agents:
        return None

    if agent_name is None:
        agent_name = (config or {}).get("default_agent")
    if agent_name and agent_name in agents:
        target = {agent_name: agents[agent_name]}
    else:
        target = agents

    for name, agent_cfg in target.items():
        aws_block = agent_cfg.get("aws") or {}
        role_arn = aws_block.get("execution_role")
        if role_arn:
            print(f"→ Found role for agent '{name}'")
            return role_arn.rsplit("/", 1)[-1]
    return None


def _resolve_account_id(region: str) -> str:
    sts = boto3.client("sts", region_name=region)
    return sts.get_caller_identity()["Account"]


def _build_policy(account_id: str) -> dict:
    """Build an IAM policy granting:
      1. InvokeModel on the Claude 4.6 inference profile and its underlying
         foundation model across the regions it spans.
      2. InvokeModelWithBidirectionalStream on Nova Sonic v2 (us-east-1)
         for the bidi_app.py voice runtime.
      3. DynamoDB PutItem on cms-prod-storage-service-history so book()
         can persist scheduled bookings.
      4. DynamoDB Query/Scan/GetItem on the CMS vehicles, drivers, and
         service-history tables so triage()/book() can enrich envelopes
         with real customer/vehicle data.
      5. kms:Decrypt + kms:GenerateDataKey on the CMS CMK so those DDB
         calls succeed against the SSE-KMS-encrypted tables.
    """
    foundation_model_arns = [
        f"arn:aws:bedrock:{region}::foundation-model/{FOUNDATION_MODEL_ID}"
        for region in INFERENCE_REGIONS
    ]
    inference_profile_arn = (
        f"arn:aws:bedrock:us-east-1:{account_id}:inference-profile/{INFERENCE_PROFILE_ID}"
    )
    nova_sonic_arns = [
        f"arn:aws:bedrock:{r}::foundation-model/{NOVA_SONIC_MODEL_ID}"
        for r in NOVA_SONIC_REGIONS
    ]

    def _ddb_arn(table: str) -> str:
        return f"arn:aws:dynamodb:{CMS_REGION}:{account_id}:table/{table}"

    cms_write_arns = [_ddb_arn(t) for t in CMS_TABLES_WRITE]
    cms_read_arns = [_ddb_arn(t) for t in CMS_TABLES_READ]
    # Read perms must also cover the table's GSIs (service-history has
    # ServiceTypeIndex and DealerIndex); permit every index under the table.
    cms_read_arns_with_indexes = cms_read_arns + [
        f"{arn}/index/*" for arn in cms_read_arns
    ]

    return {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "InvokeClaudeSonnet45ViaInferenceProfile",
                "Effect": "Allow",
                "Action": [
                    "bedrock:InvokeModel",
                    "bedrock:InvokeModelWithResponseStream",
                ],
                "Resource": [inference_profile_arn, *foundation_model_arns],
            },
            {
                "Sid": "InvokeNovaSonicBidirectionalStream",
                "Effect": "Allow",
                "Action": [
                    "bedrock:InvokeModelWithBidirectionalStream",
                ],
                "Resource": nova_sonic_arns,
            },
            {
                "Sid": "CmsServiceHistoryWrite",
                "Effect": "Allow",
                "Action": [
                    "dynamodb:PutItem",
                    "dynamodb:UpdateItem",
                ],
                "Resource": cms_write_arns,
            },
            {
                "Sid": "CmsRead",
                "Effect": "Allow",
                "Action": [
                    "dynamodb:GetItem",
                    "dynamodb:Query",
                    "dynamodb:Scan",
                    "dynamodb:BatchGetItem",
                ],
                "Resource": cms_read_arns_with_indexes,
            },
            {
                "Sid": "CmsKmsAccessForSseTables",
                "Effect": "Allow",
                "Action": [
                    "kms:Decrypt",
                    "kms:GenerateDataKey",
                    "kms:DescribeKey",
                ],
                "Resource": [CMS_KMS_KEY_ARN],
                "Condition": {
                    "StringEquals": {
                        "kms:ViaService": f"dynamodb.{CMS_REGION}.amazonaws.com"
                    }
                },
            },
        ],
    }


if __name__ == "__main__":
    sys.exit(main())
