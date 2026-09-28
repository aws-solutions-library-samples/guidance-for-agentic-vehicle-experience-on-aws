#!/usr/bin/env python3
"""Create a demo admin-managed user in the meridian-fleet Cognito pool.

Usage:
  AWS_REGION=us-east-1 python3 scripts/seed-meridian-fleet-user.py [email] [password]

If password is omitted, a random temp password is generated.
Uses the 'meridian-fleet' user pool discovered from CloudFormation exports.
"""
from __future__ import annotations

import os
import secrets
import string
import sys

import boto3

REGION = os.environ.get("AWS_REGION", "us-east-1")
STAGE = os.environ.get("STAGE", "prod")
TENANT_ID = "meridian-fleet"


def _resolve_pool_id() -> str:
    cfn = boto3.client("cloudformation", region_name=REGION)
    export_name = f"vsa-{STAGE}-{TENANT_ID}-pool-id"
    for page in cfn.get_paginator("list_exports").paginate():
        for exp in page["Exports"]:
            if exp["Name"] == export_name:
                return exp["Value"]
    raise RuntimeError(f"Export {export_name} not found — is the API stack deployed?")


def _random_pw() -> str:
    alphabet = string.ascii_letters + string.digits + "!@#$%^&*"
    while True:
        pw = "".join(secrets.choice(alphabet) for _ in range(16))
        if (any(c.islower() for c in pw) and any(c.isupper() for c in pw)
                and any(c.isdigit() for c in pw) and any(c in "!@#$%^&*" for c in pw)):
            return pw


def main() -> int:
    email = sys.argv[1] if len(sys.argv) > 1 else "demo@meridian-fleet.example"
    password = sys.argv[2] if len(sys.argv) > 2 else _random_pw()

    pool_id = _resolve_pool_id()
    cognito = boto3.client("cognito-idp", region_name=REGION)

    try:
        cognito.admin_create_user(
            UserPoolId=pool_id,
            Username=email,
            UserAttributes=[
                {"Name": "email", "Value": email},
                {"Name": "email_verified", "Value": "true"},
                {"Name": "custom:tenantId", "Value": TENANT_ID},
                {"Name": "custom:role", "Value": "demo"},
            ],
            MessageAction="SUPPRESS",  # no welcome email
            TemporaryPassword=password,
        )
    except cognito.exceptions.UsernameExistsException:
        print(f"User {email} already exists; resetting password")
        cognito.admin_set_user_password(
            UserPoolId=pool_id, Username=email, Password=password, Permanent=True,
        )
    else:
        # Mark password permanent so the user can log in without forced change.
        cognito.admin_set_user_password(
            UserPoolId=pool_id, Username=email, Password=password, Permanent=True,
        )

    print(f"Pool:     {pool_id}")
    print(f"Email:    {email}")
    print(f"Password: {password}")
    print(f"Tenant:   {TENANT_ID}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
