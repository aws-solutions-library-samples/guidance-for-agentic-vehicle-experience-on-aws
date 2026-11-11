"""CDK custom-resource Lambda: SNS APNs SANDBOX platform application.

onEvent handler for a CDK ``custom_resources.Provider``.

Event contract (CloudFormation custom resource):
    RequestType: Create | Update | Delete
    ResourceProperties:
        SecretId        — Secrets Manager secret ARN or name containing the APNs
                          auth key as JSON ``{key_id, team_id, bundle_id, p8}``
        ApplicationName — SNS platform application name to create or manage
        KeyVersion      — incremented by the caller to force re-evaluation when
                          the secret is rotated

Response contract:
    PhysicalResourceId: <ApplicationName>  (stable across every request type)
    Data (Create / Update only):
        PlatformApplicationArn — ARN of the SNS application, or "" when the
                                  secret is absent

Behaviour:
    Create / Update:
        Secret absent (ResourceNotFoundException) →
            returns ``Data.PlatformApplicationArn = ""``; no SNS application
            is created or modified; the stack deploys cleanly without a key.
        Secret present →
            create (if new) or update (if existing) an APNS_SANDBOX application
            with token-based auth and return its ARN.
    Delete:
        Delete the SNS application if it exists.  The Secrets Manager secret
        is never touched; removing the key is the operator's job.

Key bytes (``p8`` signing-key content) are never logged or included in any
response field.  Other errors from AWS APIs re-raise immediately.

SNS attribute names for APNS token-based authentication (verified 2026-11-05):
    PlatformCredential    — signing key (.p8 file content)
    PlatformPrincipal     — signing key ID
    ApplePlatformTeamID   — Apple Developer account Team ID
    ApplePlatformBundleID — iOS application bundle identifier

Sources:
    https://docs.aws.amazon.com/sdk-for-python/v4/reference/clients/sns/operations/set_platform_application_attributes/
    https://docs.aws.amazon.com/sns/latest/api/API_CreatePlatformApplication.html
"""
from __future__ import annotations

import json
import logging

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


# ---------------------------------------------------------------------------
# Client factories — replaced in tests via patch.object
# ---------------------------------------------------------------------------

def _secretsmanager_client():
    return boto3.client("secretsmanager")


def _sns_client():
    return boto3.client("sns")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def on_event(event, _context):
    """CDK custom resource onEvent handler.

    CloudFormation invokes this on Create, Update, and Delete.
    Returns a dict with ``PhysicalResourceId`` and, for Create/Update, ``Data``.
    """
    request_type = event["RequestType"]
    props = event["ResourceProperties"]
    secret_id = props["SecretId"]
    application_name = props["ApplicationName"]

    # PhysicalResourceId is the application name — it never changes.
    physical_id = application_name

    if request_type == "Delete":
        _delete_application(application_name)
        return {"PhysicalResourceId": physical_id}

    # Create or Update — same upsert logic
    arn = _create_or_update_application(secret_id, application_name)
    return {
        "PhysicalResourceId": physical_id,
        "Data": {"PlatformApplicationArn": arn},
    }


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _create_or_update_application(secret_id: str, application_name: str) -> str:
    """Return the SNS platform application ARN, or "" if the secret is absent."""
    sm = _secretsmanager_client()
    try:
        resp = sm.get_secret_value(SecretId=secret_id)
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ResourceNotFoundException":
            logger.info("Secret %s absent; no SNS application created.", secret_id)
            return ""
        raise  # any other SM error is unexpected — propagate

    # Parse the secret — key bytes are never logged or returned
    secret_data = json.loads(resp["SecretString"])

    sns = _sns_client()
    existing_arn = _find_application(sns, application_name)

    attributes = {
        "PlatformCredential": secret_data["p8"],
        "PlatformPrincipal": secret_data["key_id"],
        "ApplePlatformTeamID": secret_data["team_id"],
        "ApplePlatformBundleID": secret_data["bundle_id"],
    }

    if existing_arn:
        logger.info("Updating SNS application %s.", application_name)
        sns.set_platform_application_attributes(
            PlatformApplicationArn=existing_arn,
            Attributes=attributes,
        )
        return existing_arn

    logger.info("Creating SNS application %s on APNS_SANDBOX.", application_name)
    create_resp = sns.create_platform_application(
        Name=application_name,
        Platform="APNS_SANDBOX",
        Attributes=attributes,
    )
    return create_resp["PlatformApplicationArn"]


def _delete_application(application_name: str) -> None:
    """Delete the SNS platform application if it exists.

    Never calls Secrets Manager — removing the key is the operator's job.
    """
    sns = _sns_client()
    existing_arn = _find_application(sns, application_name)
    if existing_arn:
        logger.info(
            "Deleting SNS application %s (%s).", application_name, existing_arn
        )
        sns.delete_platform_application(PlatformApplicationArn=existing_arn)
    else:
        logger.info(
            "SNS application %s not found; nothing to delete.", application_name
        )


def _find_application(sns_client, application_name: str) -> str | None:
    """Return the ARN of the named APNS_SANDBOX application, or None.

    Paginates ``list_platform_applications`` via NextToken.
    """
    kwargs: dict = {}
    while True:
        resp = sns_client.list_platform_applications(**kwargs)
        for app in resp.get("PlatformApplications", []):
            arn = app.get("PlatformApplicationArn", "")
            if arn.endswith("/" + application_name):
                return arn
        next_token = resp.get("NextToken")
        if not next_token:
            return None
        kwargs["NextToken"] = next_token
