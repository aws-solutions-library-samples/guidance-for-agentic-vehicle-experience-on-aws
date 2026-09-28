#!/usr/bin/env python3
"""
Attach ADP DynamoDB read permissions to the AgentCore execution role.

The CVX agent tools (vehicle_identity, service_network, parts_lookup)
query ADP DynamoDB tables at runtime. This script adds an inline policy
to the AgentCore execution role granting read access.

Usage:
    ADP_STAGE=dev python3 scripts/attach-adp-permissions.py

Requires: the AgentCore execution role ARN from .bedrock_agentcore.yaml
"""

import os
import json
import boto3

REGION = os.environ.get("AWS_REGION", "us-east-1")
ADP_STAGE = os.environ.get("ADP_STAGE", "dev")

# ADP table names
ADP_TABLES = [
    f"adp-{ADP_STAGE}-vehicle-identity",
    f"adp-{ADP_STAGE}-service-network",
    f"adp-{ADP_STAGE}-parts-catalog",
    f"adp-{ADP_STAGE}-vehicle-lifecycle",
    f"adp-{ADP_STAGE}-campaigns-offers",
    f"adp-{ADP_STAGE}-connected-services",
]


def main():
    iam = boto3.client("iam")
    sts = boto3.client("sts")
    account = sts.get_caller_identity()["Account"]

    # Find the AgentCore execution role
    role_name = os.environ.get("AGENTCORE_ROLE_NAME")
    if not role_name:
        # Try to find it from .bedrock_agentcore.yaml
        try:
            import yaml
            with open(".bedrock_agentcore.yaml") as f:
                config = yaml.safe_load(f)
            role_arn = list(config.get("agents", {}).values())[0]["aws"]["execution_role"]
            role_name = role_arn.split("/")[-1]
        except Exception:
            print("❌ Set AGENTCORE_ROLE_NAME or ensure .bedrock_agentcore.yaml exists")
            return

    print(f"Role: {role_name}")
    print(f"Tables: {len(ADP_TABLES)} ADP tables (stage={ADP_STAGE})")

    # Build policy
    resources = []
    for table in ADP_TABLES:
        resources.append(f"arn:aws:dynamodb:{REGION}:{account}:table/{table}")
        resources.append(f"arn:aws:dynamodb:{REGION}:{account}:table/{table}/index/*")

    policy = {
        "Version": "2012-10-17",
        "Statement": [{
            "Sid": "ADPDataProductsRead",
            "Effect": "Allow",
            "Action": [
                "dynamodb:GetItem",
                "dynamodb:Query",
                "dynamodb:Scan",
                "dynamodb:BatchGetItem",
            ],
            "Resource": resources,
        }],
    }

    # Also add Bedrock KB retrieve permission
    policy["Statement"].append({
        "Sid": "BedrockKBRetrieve",
        "Effect": "Allow",
        "Action": ["bedrock:Retrieve", "bedrock:RetrieveAndGenerate"],
        "Resource": f"arn:aws:bedrock:{REGION}:{account}:knowledge-base/*",
    })

    iam.put_role_policy(
        RoleName=role_name,
        PolicyName="cvx-adp-data-access",
        PolicyDocument=json.dumps(policy),
    )
    print(f"✅ Attached policy 'cvx-adp-data-access' to {role_name}")
    print(f"   Grants: DynamoDB read on {len(ADP_TABLES)} tables + Bedrock KB retrieve")


if __name__ == "__main__":
    main()
