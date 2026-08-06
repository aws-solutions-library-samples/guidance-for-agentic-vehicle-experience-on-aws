#!/usr/bin/env python3
"""
Wire deployed Connect stack outputs into the tenant config table.

After `cdk deploy cvx-{stage}-connect`, run this to update the tenant's
channels.connect config so the /escalate Lambda can find the instance
and contact flow.

Usage:
  VSA_STAGE=prod CMS_STAGE=dev python3 scripts/wire-connect-to-tenant.py
"""

import os
import json
import boto3

REGION = os.environ.get("AWS_REGION", "us-east-1")
VSA_STAGE = os.environ.get("VSA_STAGE", "prod")
CMS_STAGE = os.environ.get("CMS_STAGE", "dev")
TENANT_ID = os.environ.get("VSA_TENANT_ID", "michelin")
CONNECT_STACK = f"cvx-{VSA_STAGE}-connect"
TENANT_TABLE = f"vsa-{VSA_STAGE}-tenant-config"


def get_stack_outputs(cf, stack_name: str) -> dict:
    resp = cf.describe_stacks(StackName=stack_name)
    outputs = resp["Stacks"][0].get("Outputs", [])
    return {o["OutputKey"]: o["OutputValue"] for o in outputs}


def main():
    session = boto3.Session(region_name=REGION)
    cf = session.client("cloudformation")
    ddb = session.resource("dynamodb").Table(TENANT_TABLE)

    print(f"Reading outputs from stack: {CONNECT_STACK}")
    outputs = get_stack_outputs(cf, CONNECT_STACK)

    instance_arn = outputs.get("ConnectInstanceArn")
    instance_id = outputs.get("ConnectInstanceId")
    flow_arn = outputs.get("EscalationContactFlowArn")
    p0_queue = outputs.get("P0EmergencyQueueArn")
    p1_queue = outputs.get("P1UrgentQueueArn")
    p2_queue = outputs.get("P2RoutineQueueArn")

    if not instance_arn or not flow_arn:
        print(f"❌ Missing outputs from {CONNECT_STACK}. Is it deployed?")
        return

    # Extract contact flow ID from ARN
    # ARN format: arn:aws:connect:region:account:instance/id/contact-flow/flow-id
    contact_flow_id = flow_arn.split("/")[-1]

    print(f"  Instance: {instance_id}")
    print(f"  Flow ID:  {contact_flow_id}")
    print(f"  P0 Queue: {p0_queue}")

    # Read current tenant config
    pointer = ddb.get_item(Key={"tenantId": TENANT_ID, "version": "current"}).get("Item")
    if not pointer or "activeVersion" not in pointer:
        print(f"❌ No active config for tenant '{TENANT_ID}' in {TENANT_TABLE}")
        return

    version = pointer["activeVersion"]
    item = ddb.get_item(Key={"tenantId": TENANT_ID, "version": version}).get("Item")
    if not item:
        print(f"❌ Config version '{version}' not found")
        return

    # Update channels.connect
    channels = item.get("channels", {})
    channels["connect"] = {
        "instanceArn": instance_arn,
        "instanceId": instance_id,
        "contactFlowId": contact_flow_id,
    }
    item["channels"] = channels

    # Update escalation queue ARNs
    escalation = item.get("escalation", {})
    escalation["tier0QueueArn"] = p0_queue
    escalation["tier1QueueArn"] = p1_queue
    escalation["tier2QueueArn"] = p2_queue
    item["escalation"] = escalation

    # Write back
    ddb.put_item(Item=item)
    print(f"\n✅ Updated tenant '{TENANT_ID}' config with Connect details")
    print(f"   The /escalate endpoint will now route to the deployed Connect instance.")


if __name__ == "__main__":
    main()
