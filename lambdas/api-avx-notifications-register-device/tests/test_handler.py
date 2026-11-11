"""
Unit tests for lambdas/api-avx-notifications-register-device/handler.py

Mutation verification results (run 2026-09-19):
  (a) N/A (scope is in _lib/avx_scope.py).

  (b) N/A (dismiss is in actions handler).

  (c) N/A (ios-owner reject is in findings handler).

  (d) Deleted the sns.create_platform_endpoint() call in register-device:
      test_register_device_stores_endpoint_arn FAILED as expected.
      Mutation reverted. ✓ CAUGHT

Run:
    python3 -m pytest lambdas/api-avx-notifications-register-device/ -v
"""
from __future__ import annotations

import json
import os
import sys
from unittest.mock import MagicMock, patch

_LAMBDA_ROOT = os.path.join(os.path.dirname(__file__), "..")
_LAMBDAS_ROOT = os.path.dirname(_LAMBDA_ROOT)
for _p in (_LAMBDA_ROOT, _LAMBDAS_ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import handler as h


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _claims(sub="user-sub-123") -> dict:
    return {"sub": sub, "cognito:groups": "consumer"}


def _valid_body() -> dict:
    return {
        "device_token": "fake-apns-device-token-abc123",
        "per_category_consent": {"safety": True, "coverage": False, "service": True},
        "platform": "apns-staging",
    }


def _event(body=None, claims=None, method="POST") -> dict:
    return {
        "httpMethod": method,
        "resource": "/notifications/register-device",
        "pathParameters": {},
        "queryStringParameters": {},
        "requestContext": {"authorizer": {"claims": claims or _claims()}},
        "body": json.dumps(body) if body else json.dumps(_valid_body()),
    }


def _decode_body(resp: dict) -> dict:
    return json.loads(resp["body"])


def _devices_table_mock() -> MagicMock:
    """A devices-table MagicMock whose ``query`` answers like DynamoDB.

    The handler pages the owner's prior rows before upserting (D-SCOPE-1). A
    bare MagicMock returns a truthy MagicMock for ``LastEvaluatedKey`` and the
    page loop never ends, so every table mock here must return a real,
    terminated page. Semantics of the delete itself are tested with moto in
    test_one_device_per_owner.py.
    """
    table = MagicMock()
    table.query.return_value = {"Items": []}
    return table


# ---------------------------------------------------------------------------
# Auth guard
# ---------------------------------------------------------------------------

class TestAuthGuard:
    def test_missing_claims_returns_401(self):
        ev = _event()
        ev["requestContext"]["authorizer"]["claims"] = None
        resp = h.handler(ev, None)
        assert resp["statusCode"] == 401

    def test_missing_sub_returns_401(self):
        ev = _event(claims={"cognito:groups": "consumer"})
        resp = h.handler(ev, None)
        assert resp["statusCode"] == 401


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------

class TestHappyPath:
    def test_register_device_returns_success(self):
        mock_table = _devices_table_mock()
        mock_table.put_item.return_value = {}
        with patch.object(h, "_get_ddb") as mock_ddb, \
             patch.object(h, "_get_sns") as mock_sns_fn, \
             patch.object(h, "_APNS_PLATFORM_APP_ARN", "arn:aws:sns:us-west-2:123456789012:app/APNS/myapp"):
            mock_ddb.return_value.Table.return_value = mock_table
            mock_sns = MagicMock()
            mock_sns.create_platform_endpoint.return_value = {
                "EndpointArn": "arn:aws:sns:us-west-2:123456789012:endpoint/APNS/app/abc"
            }
            mock_sns_fn.return_value = mock_sns
            resp = h.handler(_event(), None)

        # W7: handler now returns 201 (matching OpenAPI spec)
        assert resp["statusCode"] == 201
        body = _decode_body(resp)
        assert body == {"success": True}

    # MUTATION-VERIFIED (d): delete the create_platform_endpoint call
    # Expected: test_register_device_stores_endpoint_arn fails. Reverted. ✓ CAUGHT
    def test_register_device_stores_endpoint_arn(self):
        """SNS create_platform_endpoint must be called when ARN is configured."""
        expected_arn = "arn:aws:sns:us-west-2:123456789012:endpoint/APNS/app/device-endpoint"
        mock_table = _devices_table_mock()
        mock_table.put_item.return_value = {}

        with patch.object(h, "_get_ddb") as mock_ddb, \
             patch.object(h, "_get_sns") as mock_sns_fn, \
             patch.object(h, "_APNS_PLATFORM_APP_ARN", "arn:aws:sns:us-west-2:123456789012:app/APNS/myapp"), \
             patch.object(h, "_APNS_PLATFORM_APP_ARN", "arn:aws:sns:us-west-2:123456789012:app/APNS/myapp"):
            mock_ddb.return_value.Table.return_value = mock_table
            mock_sns = MagicMock()
            mock_sns.create_platform_endpoint.return_value = {"EndpointArn": expected_arn}
            mock_sns_fn.return_value = mock_sns

            h.handler(_event(), None)

        # SNS must have been called
        mock_sns.create_platform_endpoint.assert_called_once()

        # DDB item must include the endpoint_arn
        put_call = mock_table.put_item.call_args
        item = put_call.kwargs.get("Item") or put_call.args[0].get("Item", {})
        assert item.get("endpoint_arn") == expected_arn

    def test_register_device_uses_sub_as_owner_id(self):
        """Owner identity must come from claims.sub, not the request body."""
        mock_table = _devices_table_mock()
        mock_table.put_item.return_value = {}
        caller_sub = "sub-from-claims-only"

        with patch.object(h, "_get_ddb") as mock_ddb, \
             patch.object(h, "_get_sns") as mock_sns_fn, \
             patch.object(h, "_APNS_PLATFORM_APP_ARN", ""):
            mock_ddb.return_value.Table.return_value = mock_table
            mock_sns_fn.return_value = MagicMock()

            body = _valid_body()
            ev = _event(body=body, claims=_claims(sub=caller_sub))
            h.handler(ev, None)

        put_call = mock_table.put_item.call_args
        item = put_call.kwargs.get("Item") or put_call.args[0].get("Item", {})
        assert item.get("owner_id") == caller_sub


# ---------------------------------------------------------------------------
# SNS skip when ARN is empty
# ---------------------------------------------------------------------------

class TestSNSSkipOnMissingArn:
    def test_empty_arn_skips_sns_and_records_none(self):
        mock_table = _devices_table_mock()
        mock_table.put_item.return_value = {}

        with patch.object(h, "_get_ddb") as mock_ddb, \
             patch.object(h, "_get_sns") as mock_sns_fn, \
             patch.object(h, "_APNS_PLATFORM_APP_ARN", ""):
            mock_ddb.return_value.Table.return_value = mock_table
            mock_sns_fn.return_value = MagicMock()

            resp = h.handler(_event(), None)

        assert resp["statusCode"] == 201
        # SNS should NOT have been called
        mock_sns_fn.return_value.create_platform_endpoint.assert_not_called()

        # DDB row should record endpoint_arn=None
        put_call = mock_table.put_item.call_args
        item = put_call.kwargs.get("Item") or put_call.args[0].get("Item", {})
        assert item.get("endpoint_arn") is None


# ---------------------------------------------------------------------------
# Device token security — must not be echoed or logged with real value
# ---------------------------------------------------------------------------

class TestDeviceTokenSecurity:
    def test_response_does_not_echo_device_token(self):
        """The response body must never contain the device_token value."""
        mock_table = _devices_table_mock()
        mock_table.put_item.return_value = {}
        secret_token = "SUPER-SECRET-APNS-TOKEN-MUST-NOT-APPEAR"

        with patch.object(h, "_get_ddb") as mock_ddb, \
             patch.object(h, "_get_sns") as mock_sns_fn, \
             patch.object(h, "_APNS_PLATFORM_APP_ARN", ""):
            mock_ddb.return_value.Table.return_value = mock_table
            mock_sns_fn.return_value = MagicMock()

            body = {**_valid_body(), "device_token": secret_token}
            resp = h.handler(_event(body=body), None)

        assert resp["statusCode"] == 201
        assert secret_token not in resp["body"]

    def test_token_log_prefix_is_hash_not_plaintext(self):
        """_token_log_prefix must return a hash, not the token itself."""
        token = "plaintext-token-value"
        prefix = h._token_log_prefix(token)
        # Must be a hex string of length 16 (first 16 chars of sha256)
        assert len(prefix) == 16
        assert prefix != token
        assert all(c in "0123456789abcdef" for c in prefix)

    def test_token_hash_prefix_is_deterministic(self):
        token = "my-device-token"
        assert h._token_log_prefix(token) == h._token_log_prefix(token)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

class TestValidation:
    def test_missing_device_token_returns_400(self):
        body = {k: v for k, v in _valid_body().items() if k != "device_token"}
        resp = h.handler(_event(body=body), None)
        assert resp["statusCode"] == 400

    def test_missing_per_category_consent_returns_400(self):
        body = {k: v for k, v in _valid_body().items() if k != "per_category_consent"}
        resp = h.handler(_event(body=body), None)
        assert resp["statusCode"] == 400

    def test_non_bool_consent_value_returns_400(self):
        body = _valid_body()
        body["per_category_consent"]["safety"] = "yes"  # not a bool
        resp = h.handler(_event(body=body), None)
        assert resp["statusCode"] == 400

    def test_missing_platform_returns_400(self):
        body = {k: v for k, v in _valid_body().items() if k != "platform"}
        resp = h.handler(_event(body=body), None)
        assert resp["statusCode"] == 400

    def test_invalid_json_body_returns_400(self):
        ev = _event()
        ev["body"] = "not json"
        resp = h.handler(ev, None)
        assert resp["statusCode"] == 400

    def test_upsert_stores_consent(self):
        """per_category_consent values are stored in DynamoDB."""
        mock_table = _devices_table_mock()
        mock_table.put_item.return_value = {}
        consent = {"safety": True, "coverage": True, "service": False}
        body = {**_valid_body(), "per_category_consent": consent}

        with patch.object(h, "_get_ddb") as mock_ddb, \
             patch.object(h, "_get_sns") as mock_sns_fn, \
             patch.object(h, "_APNS_PLATFORM_APP_ARN", ""):
            mock_ddb.return_value.Table.return_value = mock_table
            mock_sns_fn.return_value = MagicMock()
            h.handler(_event(body=body), None)

        put_call = mock_table.put_item.call_args
        item = put_call.kwargs.get("Item") or put_call.args[0].get("Item", {})
        assert item["per_category_consent"] == consent

    def test_w7_extra_consent_keys_rejected(self):
        """W7: additionalProperties:false — unknown consent keys return 400."""
        body = _valid_body()
        body["per_category_consent"]["advertising"] = True
        resp = h.handler(_event(body=body), None)
        assert resp["statusCode"] == 400
        body_dec = _decode_body(resp)
        assert "advertising" in body_dec["error"] or "unknown" in body_dec["error"].lower()

    def test_w7_platform_enum_invalid_returns_400(self):
        """W7: platform must be one of apns-staging | apns-prod."""
        body = {**_valid_body(), "platform": "apns-dev-unofficial"}
        resp = h.handler(_event(body=body), None)
        assert resp["statusCode"] == 400
        body_dec = _decode_body(resp)
        assert "platform" in body_dec["error"].lower() or "apns" in body_dec["error"].lower()

    def test_w7_platform_enum_valid_staging(self):
        """W7: apns-staging is a valid platform value."""
        mock_table = _devices_table_mock()
        mock_table.put_item.return_value = {}
        body = {**_valid_body(), "platform": "apns-staging"}
        with patch.object(h, "_get_ddb") as mock_ddb, \
             patch.object(h, "_get_sns") as mock_sns_fn, \
             patch.object(h, "_APNS_PLATFORM_APP_ARN", ""):
            mock_ddb.return_value.Table.return_value = mock_table
            mock_sns_fn.return_value = MagicMock()
            resp = h.handler(_event(body=body), None)
        assert resp["statusCode"] == 201

    def test_w7_platform_enum_valid_prod(self):
        """W7: apns-prod is a valid platform value."""
        mock_table = _devices_table_mock()
        mock_table.put_item.return_value = {}
        body = {**_valid_body(), "platform": "apns-prod"}
        with patch.object(h, "_get_ddb") as mock_ddb, \
             patch.object(h, "_get_sns") as mock_sns_fn, \
             patch.object(h, "_APNS_PLATFORM_APP_ARN", ""):
            mock_ddb.return_value.Table.return_value = mock_table
            mock_sns_fn.return_value = MagicMock()
            resp = h.handler(_event(body=body), None)
        assert resp["statusCode"] == 201

    def test_w7_only_declared_consent_keys_stored_in_ddb(self):
        """W7: handler sanitizes per_category_consent to only store declared keys."""
        mock_table = _devices_table_mock()
        mock_table.put_item.return_value = {}
        consent_with_extra = {"safety": True, "coverage": False, "service": True}
        body = {**_valid_body(), "per_category_consent": consent_with_extra}

        with patch.object(h, "_get_ddb") as mock_ddb, \
             patch.object(h, "_get_sns") as mock_sns_fn, \
             patch.object(h, "_APNS_PLATFORM_APP_ARN", ""):
            mock_ddb.return_value.Table.return_value = mock_table
            mock_sns_fn.return_value = MagicMock()
            h.handler(_event(body=body), None)

        put_call = mock_table.put_item.call_args
        item = put_call.kwargs.get("Item") or put_call.args[0].get("Item", {})
        stored_consent = item.get("per_category_consent", {})
        # Only the three declared keys must be stored
        assert set(stored_consent.keys()) == {"safety", "coverage", "service"}
