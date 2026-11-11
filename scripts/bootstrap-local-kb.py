#!/usr/bin/env python3
"""
Bootstrap a local Bedrock Knowledge Base from the embedded corpora/ directory.

Creates:
1. S3 bucket for KB documents
2. Uploads corpora/ markdown files
3. Creates Bedrock Knowledge Base with S3 data source
4. Triggers initial sync

Usage:
    AWS_REGION=us-east-1 python3 scripts/bootstrap-local-kb.py

Outputs the KB_ID to set in .env as BEDROCK_KB_ID.
"""

import os
import sys
import time
import boto3
import json

REGION = os.environ.get("AWS_REGION", "us-east-1")
STAGE = os.environ.get("VSA_STAGE", "prod")
BUCKET_NAME = f"cvx-{STAGE}-knowledge-base-{boto3.client('sts').get_caller_identity()['Account']}"
KB_NAME = f"cvx-{STAGE}-vehicle-knowledge"
CORPORA_DIR = os.path.join(os.path.dirname(__file__), "..", "corpora")


def main():
    session = boto3.Session(region_name=REGION)
    s3 = session.client("s3")
    bedrock = session.client("bedrock-agent")
    iam = session.client("iam")

    print(f"🚀 Bootstrapping Knowledge Base")
    print(f"   Region: {REGION}")
    print(f"   Bucket: {BUCKET_NAME}")
    print(f"   KB Name: {KB_NAME}")
    print()

    # 1. Create S3 bucket
    print("1/5 Creating S3 bucket...")
    try:
        if REGION == "us-east-1":
            s3.create_bucket(Bucket=BUCKET_NAME)
        else:
            s3.create_bucket(Bucket=BUCKET_NAME,
                CreateBucketConfiguration={"LocationConstraint": REGION})
    except s3.exceptions.BucketAlreadyOwnedByYou:
        pass
    print(f"     ✅ {BUCKET_NAME}")

    # 2. Upload corpora
    print("2/5 Uploading corpora/ to S3...")
    count = 0
    for root, _, files in os.walk(CORPORA_DIR):
        for f in files:
            if not f.endswith(".md"):
                continue
            local_path = os.path.join(root, f)
            s3_key = os.path.relpath(local_path, CORPORA_DIR)
            s3.upload_file(local_path, BUCKET_NAME, s3_key,
                ExtraArgs={"ContentType": "text/markdown"})
            count += 1
    print(f"     ✅ {count} files uploaded")

    # 3. Create KB execution role
    print("3/5 Creating KB execution role...")
    role_name = f"cvx-{STAGE}-kb-role"
    role_arn = _ensure_kb_role(iam, role_name, BUCKET_NAME)
    print(f"     ✅ {role_name}")
    # Not a leftover debug sleep. IAM is eventually consistent: a role created
    # via CreateRole is not immediately assumable by Bedrock, and _ensure_kb()
    # below fails with a validation error on the role trust policy if it runs
    # too soon. There is no waiter for IAM propagation, so a fixed pause is the
    # documented mitigation. See issues/2026-08-03-cvx-probe-critical-triage/.
    # nosemgrep: arbitrary-sleep
    time.sleep(10)  # IAM propagation

    # 4. Create Knowledge Base
    print("4/5 Creating Bedrock Knowledge Base...")
    kb_id = _ensure_kb(bedrock, KB_NAME, role_arn, BUCKET_NAME)
    print(f"     ✅ KB ID: {kb_id}")

    # 5. Start ingestion
    print("5/5 Starting data sync...")
    ds_list = bedrock.list_data_sources(knowledgeBaseId=kb_id)
    if ds_list["dataSourceSummaries"]:
        ds_id = ds_list["dataSourceSummaries"][0]["dataSourceId"]
        bedrock.start_ingestion_job(knowledgeBaseId=kb_id, dataSourceId=ds_id)
        print(f"     ✅ Ingestion started (data source: {ds_id})")
    else:
        print("     ⚠️  No data source found — create one manually")

    print()
    print(f"✅ Done! Add this to your .env:")
    print(f"   BEDROCK_KB_ID={kb_id}")


def _ensure_kb_role(iam, role_name: str, bucket: str) -> str:
    trust = json.dumps({
        "Version": "2012-10-17",
        "Statement": [{"Effect": "Allow", "Principal": {"Service": "bedrock.amazonaws.com"},
                       "Action": "sts:AssumeRole"}]
    })
    try:
        resp = iam.get_role(RoleName=role_name)
        return resp["Role"]["Arn"]
    except iam.exceptions.NoSuchEntityException:
        resp = iam.create_role(RoleName=role_name, AssumeRolePolicyDocument=trust)
        arn = resp["Role"]["Arn"]
        iam.put_role_policy(RoleName=role_name, PolicyName="kb-access",
            PolicyDocument=json.dumps({
                "Version": "2012-10-17",
                "Statement": [
                    {"Effect": "Allow", "Action": ["s3:GetObject", "s3:ListBucket"],
                     "Resource": [f"arn:aws:s3:::{bucket}", f"arn:aws:s3:::{bucket}/*"]},
                    {"Effect": "Allow",
                     "Action": ["bedrock:InvokeModel", "bedrock:Retrieve"],
                     "Resource": "*"},
                ]
            }))
        return arn


def _ensure_kb(bedrock, name: str, role_arn: str, bucket: str) -> str:
    # Check if KB already exists
    kbs = bedrock.list_knowledge_bases()
    for kb in kbs.get("knowledgeBaseSummaries", []):
        if kb["name"] == name:
            return kb["knowledgeBaseId"]

    # Create new KB
    resp = bedrock.create_knowledge_base(
        name=name,
        description="Vehicle technical reference — DTCs, TSBs, recalls, maintenance, SLA",
        roleArn=role_arn,
        knowledgeBaseConfiguration={
            "type": "VECTOR",
            "vectorKnowledgeBaseConfiguration": {
                "embeddingModelArn": f"arn:aws:bedrock:{REGION}::foundation-model/amazon.titan-embed-text-v2:0",
            },
        },
        storageConfiguration={
            "type": "OPENSEARCH_SERVERLESS",
            "opensearchServerlessConfiguration": {
                "collectionArn": "auto",  # Let Bedrock create the collection
                "fieldMapping": {
                    "metadataField": "metadata",
                    "textField": "text",
                    "vectorField": "vector",
                },
                "vectorIndexName": f"cvx-{STAGE}-kb-index",
            },
        },
    )
    kb_id = resp["knowledgeBase"]["knowledgeBaseId"]

    # Create S3 data source
    bedrock.create_data_source(
        knowledgeBaseId=kb_id,
        name="corpora",
        description="Embedded vehicle technical reference documents",
        dataSourceConfiguration={
            "type": "S3",
            "s3Configuration": {"bucketArn": f"arn:aws:s3:::{bucket}"},
        },
    )
    return kb_id


if __name__ == "__main__":
    main()
