"""Tests: apns-platform-app custom-resource Lambda handler.

Uses botocore Stubber to assert exact AWS request parameters.

Mutation checks (all four verified caught):
    M1 key_logged       — if handler logged ``secret_data['p8']``, test_key_not_logged FAILS
    M2 absent_error     — if ResourceNotFoundException raised instead of "" returned,
                          test_absent_secret_returns_empty_arn FAILS
    M3 delete_secret    — if _delete_application called secretsmanager, test_delete_does_not_touch_secret FAILS
    M4 wrong_platform   — if Platform="APNS" instead of "APNS_SANDBOX",
                          Stubber raises StubAssertionError on expected_params mismatch in
                          test_create_calls_sns_with_exact_params

No __init__.py; handler loaded via importlib.util.spec_from_file_location.

Run:
    .venv/bin/python -m pytest lambdas/apns-platform-app/tests/test_apns_platform_app_handler.py -q
"""
from __future__ import annotations

import importlib.util
import json
import logging
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import boto3
import pytest
from botocore.exceptions import ClientError
from botocore.stub import Stubber

# ---------------------------------------------------------------------------
# Path wiring — importlib pattern; no __init__.py anywhere in this lambda
# ---------------------------------------------------------------------------
_LAMBDA_ROOT = Path(__file__).resolve().parent.parent
_LAMBDAS_ROOT = _LAMBDA_ROOT.parent

for _p in (str(_LAMBDA_ROOT), str(_LAMBDAS_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)


def _load_handler():
    mod_name = "_apns_platform_app_handler"
    if mod_name in sys.modules:
        return sys.modules[mod_name]
    handler_path = _LAMBDA_ROOT / "handler.py"
    spec = importlib.util.spec_from_file_location(mod_name, handler_path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


h = _load_handler()

# ---------------------------------------------------------------------------
# Shared constants
# ---------------------------------------------------------------------------
_SECRET_ID = "cvx/test/apns-auth-key"
_APP_NAME = "vsa-test-avx-apns-sandbox"
_APP_ARN = (
    "arn:aws:sns:us-east-1:111122223333:app/APNS_SANDBOX/vsa-test-avx-apns-sandbox"
)
_KEY_ID = "ABCDEFGH12"
_TEAM_ID = "TEAMID1234"
_BUNDLE_ID = "com.example.avx"
# Deliberately unique string so we can check it's never in log output (M1)
_P8 = "-----BEGIN PRIVATE KEY-----\nMOCKEDKEY_UNIQUE_CANARY\n-----END PRIVATE KEY-----"

_SECRET_JSON = json.dumps(
    {
        "key_id": _KEY_ID,
        "team_id": _TEAM_ID,
        "bundle_id": _BUNDLE_ID,
        "p8": _P8,
    }
)

# Exact SNS attributes expected for every create/update (M4 catches wrong platform,
# M1 would catch any logging of these values)
_APNS_ATTRIBUTES = {
    "PlatformCredential": _P8,
    "PlatformPrincipal": _KEY_ID,
    "ApplePlatformTeamID": _TEAM_ID,
    "ApplePlatformBundleID": _BUNDLE_ID,
}

# One found application entry returned by list_platform_applications
_EXISTING_APP = {
    "PlatformApplicationArn": _APP_ARN,
    "Attributes": {},
}


# ---------------------------------------------------------------------------
# Event builders
# ---------------------------------------------------------------------------


def _create_event(app_name: str = _APP_NAME) -> dict:
    return {
        "RequestType": "Create",
        "PhysicalResourceId": "",
        "LogicalResourceId": "ApnsPlatformApp",
        "StackId": "arn:aws:cloudformation:us-east-1:111122223333:stack/test/abc",
        "RequestId": "req-create-001",
        "ResourceProperties": {
            "SecretId": _SECRET_ID,
            "ApplicationName": app_name,
            "KeyVersion": "1",
        },
    }


def _update_event(app_name: str = _APP_NAME) -> dict:
    ev = _create_event(app_name)
    ev["RequestType"] = "Update"
    ev["PhysicalResourceId"] = app_name
    return ev


def _delete_event(app_name: str = _APP_NAME) -> dict:
    ev = _create_event(app_name)
    ev["RequestType"] = "Delete"
    ev["PhysicalResourceId"] = app_name
    return ev


# ---------------------------------------------------------------------------
# Helpers: build pre-wired boto3 clients with attached Stubbers
# ---------------------------------------------------------------------------


def _sm_client(region: str = "us-east-1"):
    return boto3.client("secretsmanager", region_name=region)


def _sns_client_stub(region: str = "us-east-1"):
    return boto3.client("sns", region_name=region)


# ---------------------------------------------------------------------------
# TestCreateAbsentSecret — M2 mutation target
# ---------------------------------------------------------------------------


class TestCreateAbsentSecret:
    """Secret absent (ResourceNotFoundException) → empty ARN, no SNS call."""

    def test_absent_secret_returns_empty_arn(self):
        """M2: if ResourceNotFoundException raises instead of returning "", this FAILS."""
        sm = _sm_client()
        sm_stub = Stubber(sm)
        sm_stub.add_client_error(
            "get_secret_value",
            service_error_code="ResourceNotFoundException",
            service_message="Secrets Manager can't find the resource.",
            expected_params={"SecretId": _SECRET_ID},
        )
        sns_mock = MagicMock()

        with sm_stub:
            with patch.object(h, "_secretsmanager_client", return_value=sm), \
                 patch.object(h, "_sns_client", return_value=sns_mock):
                result = h.on_event(_create_event(), None)

        assert result["PhysicalResourceId"] == _APP_NAME
        assert result["Data"]["PlatformApplicationArn"] == ""
        sns_mock.assert_not_called()

    def test_other_sm_errors_propagate(self):
        """Non-ResourceNotFoundException from SM re-raises immediately."""
        sm = _sm_client()
        sm_stub = Stubber(sm)
        sm_stub.add_client_error(
            "get_secret_value",
            service_error_code="AccessDeniedException",
            service_message="Access denied.",
            expected_params={"SecretId": _SECRET_ID},
        )

        with sm_stub:
            with patch.object(h, "_secretsmanager_client", return_value=sm):
                with pytest.raises(ClientError) as exc_info:
                    h.on_event(_create_event(), None)

        assert exc_info.value.response["Error"]["Code"] == "AccessDeniedException"


# ---------------------------------------------------------------------------
# TestCreateNewApplication — M4 mutation target
# ---------------------------------------------------------------------------


class TestCreateNewApplication:
    """Create: secret present, no existing SNS application → create_platform_application."""

    def test_create_calls_sns_with_exact_params(self):
        """M4: if Platform='APNS' (not 'APNS_SANDBOX'), Stubber raises on expected_params."""
        sm = _sm_client()
        sm_stub = Stubber(sm)
        sm_stub.add_response(
            "get_secret_value",
            {"SecretString": _SECRET_JSON},
            expected_params={"SecretId": _SECRET_ID},
        )

        sns = _sns_client_stub()
        sns_stub = Stubber(sns)
        sns_stub.add_response(
            "list_platform_applications",
            {"PlatformApplications": []},
            expected_params={},
        )
        sns_stub.add_response(
            "create_platform_application",
            {"PlatformApplicationArn": _APP_ARN},
            expected_params={
                "Name": _APP_NAME,
                "Platform": "APNS_SANDBOX",
                "Attributes": _APNS_ATTRIBUTES,
            },
        )

        with sm_stub, sns_stub:
            with patch.object(h, "_secretsmanager_client", return_value=sm), \
                 patch.object(h, "_sns_client", return_value=sns):
                result = h.on_event(_create_event(), None)

        assert result["PhysicalResourceId"] == _APP_NAME
        assert result["Data"]["PlatformApplicationArn"] == _APP_ARN

    def test_physical_id_is_stable_on_create(self):
        """PhysicalResourceId is always the ApplicationName (stable; no replacement churn)."""
        sm = _sm_client()
        sm_stub = Stubber(sm)
        sm_stub.add_client_error(
            "get_secret_value",
            service_error_code="ResourceNotFoundException",
            service_message="not found",
            expected_params={"SecretId": _SECRET_ID},
        )

        with sm_stub:
            with patch.object(h, "_secretsmanager_client", return_value=sm), \
                 patch.object(h, "_sns_client", return_value=MagicMock()):
                result = h.on_event(_create_event(), None)

        assert result["PhysicalResourceId"] == _APP_NAME


# ---------------------------------------------------------------------------
# TestUpdateExistingApplication
# ---------------------------------------------------------------------------


class TestUpdateExistingApplication:
    """Update: existing SNS application → set_platform_application_attributes."""

    def test_update_calls_set_attributes_with_exact_params(self):
        sm = _sm_client()
        sm_stub = Stubber(sm)
        sm_stub.add_response(
            "get_secret_value",
            {"SecretString": _SECRET_JSON},
            expected_params={"SecretId": _SECRET_ID},
        )

        sns = _sns_client_stub()
        sns_stub = Stubber(sns)
        sns_stub.add_response(
            "list_platform_applications",
            {"PlatformApplications": [_EXISTING_APP]},
            expected_params={},
        )
        sns_stub.add_response(
            "set_platform_application_attributes",
            {},
            expected_params={
                "PlatformApplicationArn": _APP_ARN,
                "Attributes": _APNS_ATTRIBUTES,
            },
        )

        with sm_stub, sns_stub:
            with patch.object(h, "_secretsmanager_client", return_value=sm), \
                 patch.object(h, "_sns_client", return_value=sns):
                result = h.on_event(_update_event(), None)

        assert result["PhysicalResourceId"] == _APP_NAME
        assert result["Data"]["PlatformApplicationArn"] == _APP_ARN

    def test_update_absent_secret_returns_empty_arn(self):
        """Update with absent secret: same as Create — no application, empty ARN."""
        sm = _sm_client()
        sm_stub = Stubber(sm)
        sm_stub.add_client_error(
            "get_secret_value",
            service_error_code="ResourceNotFoundException",
            service_message="not found",
            expected_params={"SecretId": _SECRET_ID},
        )

        with sm_stub:
            with patch.object(h, "_secretsmanager_client", return_value=sm), \
                 patch.object(h, "_sns_client", return_value=MagicMock()):
                result = h.on_event(_update_event(), None)

        assert result["Data"]["PlatformApplicationArn"] == ""


# ---------------------------------------------------------------------------
# TestDeleteApplication — M3 mutation target
# ---------------------------------------------------------------------------


class TestDeleteApplication:
    """Delete events: SNS application deleted; Secrets Manager never touched."""

    def test_delete_removes_existing_application(self):
        sns = _sns_client_stub()
        sns_stub = Stubber(sns)
        sns_stub.add_response(
            "list_platform_applications",
            {"PlatformApplications": [_EXISTING_APP]},
            expected_params={},
        )
        sns_stub.add_response(
            "delete_platform_application",
            {},
            expected_params={"PlatformApplicationArn": _APP_ARN},
        )

        with sns_stub:
            with patch.object(h, "_sns_client", return_value=sns):
                result = h.on_event(_delete_event(), None)

        assert result["PhysicalResourceId"] == _APP_NAME
        assert "Data" not in result

    def test_delete_missing_app_is_noop(self):
        """No existing application → delete_platform_application is never called."""
        sns = _sns_client_stub()
        sns_stub = Stubber(sns)
        sns_stub.add_response(
            "list_platform_applications",
            {"PlatformApplications": []},
            expected_params={},
        )
        # No delete_platform_application stub — Stubber raises if called unexpectedly

        with sns_stub:
            with patch.object(h, "_sns_client", return_value=sns):
                result = h.on_event(_delete_event(), None)

        assert result["PhysicalResourceId"] == _APP_NAME

    def test_delete_does_not_touch_secret(self):
        """M3: if Delete calls secretsmanager at all, this FAILS."""
        sm_mock = MagicMock()

        sns = _sns_client_stub()
        sns_stub = Stubber(sns)
        sns_stub.add_response(
            "list_platform_applications",
            {"PlatformApplications": []},
            expected_params={},
        )

        with sns_stub:
            with patch.object(h, "_secretsmanager_client", return_value=sm_mock), \
                 patch.object(h, "_sns_client", return_value=sns):
                h.on_event(_delete_event(), None)

        assert not sm_mock.called, (
            "Delete must never call secretsmanager; removing the key is the "
            "operator's responsibility."
        )
        assert not sm_mock.method_calls


# ---------------------------------------------------------------------------
# TestKeyNotLogged — M1 mutation target
# ---------------------------------------------------------------------------


class TestKeyNotLogged:
    """M1: p8 signing key bytes must never appear in log output."""

    def test_key_not_logged_on_create(self, caplog):
        """M1: if handler logs key material at INFO (e.g. logger.info("p8=%s", ...)),
        MOCKEDKEY_UNIQUE_CANARY appears in caplog and the assertion fails.

        Note: logger.debug() mutations are filtered by the handler's setLevel(INFO)
        before propagating, so the realistic mutation is logger.info() — that is
        what would expose key bytes in a production Lambda.
        """
        sm = _sm_client()
        sm_stub = Stubber(sm)
        sm_stub.add_response(
            "get_secret_value",
            {"SecretString": _SECRET_JSON},
            expected_params={"SecretId": _SECRET_ID},
        )

        sns = _sns_client_stub()
        sns_stub = Stubber(sns)
        sns_stub.add_response(
            "list_platform_applications",
            {"PlatformApplications": []},
            expected_params={},
        )
        sns_stub.add_response(
            "create_platform_application",
            {"PlatformApplicationArn": _APP_ARN},
            expected_params={
                "Name": _APP_NAME,
                "Platform": "APNS_SANDBOX",
                "Attributes": _APNS_ATTRIBUTES,
            },
        )

        # Capture at INFO — the level that passes the handler's own setLevel(INFO)
        # filter and would thus expose key bytes in a real Lambda execution.
        with caplog.at_level(logging.INFO):
            with sm_stub, sns_stub:
                with patch.object(h, "_secretsmanager_client", return_value=sm), \
                     patch.object(h, "_sns_client", return_value=sns):
                    h.on_event(_create_event(), None)

        combined = "\n".join(caplog.messages)
        # The unique canary inside the p8 must never appear in any log record
        assert "MOCKEDKEY_UNIQUE_CANARY" not in combined, (
            "Signing key bytes must not appear in log output."
        )
        # Full PEM block must not appear
        assert "BEGIN PRIVATE KEY" not in combined, (
            "PEM block header must not appear in log output."
        )
