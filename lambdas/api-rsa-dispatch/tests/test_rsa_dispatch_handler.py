"""
Unit tests for lambdas/api-rsa-dispatch/handler.py

GET /rsa/dispatch/{dispatchId}

Module name: test_rsa_dispatch_handler  (not test_handler) so this suite does
not collide with api-escalate's tests.test_handler when both lambda directories
are collected in the same pytest run — e.g.
    pytest lambdas/api-rsa-dispatch lambdas/api-escalate
The tests/__init__.py + test_handler.py combination produces module name
tests.test_handler which aliases api-escalate's identically-named module (and
vice-versa). Moving the file to tests/test_rsa_dispatch_handler.py avoids that
silent collision entirely. (F10.5 fix — reviewer Suggestion "unique module name".)

Mutation verification results (Fix Group 10 — 2026-09-25):

  F10.1 mutations (authorized GET route):
    C1 - set authorizationType: NONE on the route → caught by CDK test (see below)
    C2 - delete addMethod entirely → caught by CDK test (see below)
    C3 - point integration at another Lambda → caught by CDK test (see below)

  F10.2 mutations (empty-row-tenant branch):
    M11  - untenanted row → distinct 404 body "dispatch has no owner"
           → test_row_with_empty_tenant_is_same_404_as_absent_row FAILED
    M11b - untenanted row readable by any tenant (row_tenant and row_tenant != caller)
           → test_row_with_empty_tenant_is_same_404_as_absent_row FAILED

  F10.3 / earlier mutations:
    (a) Remove tenant check → test_cross_tenant_read_returns_404 FAILED
    (b) Return full item instead of allowlist → test_internal_fields_are_subset_of_allowlist FAILED
    (c) Distinct 404 bodies → test_not_found_and_cross_tenant_have_same_response FAILED
    M1  - drop enRouteAt, arrivedAt, failedAt from _ALLOWED_ATTRS
          → test_full_row_response_key_set FAILED
    M9  - asOf hardcoded to 1970-01-01T00:00:00.000Z
          → test_as_of_is_recent FAILED
    M12 - drop claims.get("tenantId") fallback
          → test_tenant_id_claim_fallback FAILED
    M13 - simulated inferred from SIM- prefix when attribute absent
          → test_simulated_absent_real_client_with_sim_ref_returns_null FAILED

Run:
    .venv/bin/python3 -m pytest lambdas/api-rsa-dispatch/ -q
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Path wiring — load handler under unique module name so multiple lambda dirs
# can be collected together without sys.modules["handler"] collisions.
# ---------------------------------------------------------------------------
_LAMBDA_ROOT = Path(__file__).resolve().parent.parent
if str(_LAMBDA_ROOT) not in sys.path:
    sys.path.insert(0, str(_LAMBDA_ROOT))

os.environ.setdefault("RSA_DISPATCH_TABLE", "vsa-test-rsa-dispatch")
os.environ.setdefault("VSA_RSA_CLIENT", "fake")


def _load_handler():
    mod_name = "rsa_dispatch_handler"
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
# Expected key set (G3-D8 + asOf)
# ---------------------------------------------------------------------------
_EXPECTED_KEYS = frozenset({
    "dispatchId",
    "status",
    "etaMinutes",
    "providerRef",
    "simulated",
    "serviceType",
    "createdAt",
    "dispatchedAt",
    "enRouteAt",
    "arrivedAt",
    "failedAt",
    "updatedAt",
    "asOf",
})

# ---------------------------------------------------------------------------
# Test data constants
# ---------------------------------------------------------------------------
_DISPATCH_ID = "rsa-12345678-1234-1234-1234-123456789012"
_TENANT_A = "tenant-alpha"
_TENANT_B = "tenant-beta"

# Full row with all G3-D3 timestamps so the reader-side of the D3 contract can
# be asserted. (F10.3 / reviewer W1: _BASE_ROW previously lacked enRouteAt,
# arrivedAt, failedAt — those are exactly what the advancer and consumer write.)
_BASE_ROW: dict[str, Any] = {
    "dispatchId": _DISPATCH_ID,
    "tenantId": _TENANT_A,
    "status": "ARRIVED",
    "etaMinutes": 12,
    "providerRef": "SIM-ABC123",
    "simulated": True,
    "serviceType": "TOWING",
    "createdAt":    "2026-09-24T12:00:00.000Z",
    "dispatchedAt": "2026-09-24T12:00:05.000Z",
    "enRouteAt":    "2026-09-24T12:05:00.000Z",
    "arrivedAt":    "2026-09-24T12:12:30.000Z",
    "updatedAt":    "2026-09-24T12:12:30.000Z",
    # failedAt absent (not a failed row, but see test_full_row_response_key_set)
    # Internal fields — must never appear in response
    "vin": "1HGBH41JXMN000024",
    "lat": 37.7749,
    "lon": -122.4194,
    "callbackContact": "+15555550100",
    "driverId": "DRV-001",
    "driverName": "Alex Smith",
    "vehicleId": "VEH-001",
    "connectContactId": "cnt-001",
    "summary": "Brake failure on highway",
    "reason": "brake failure",
    "triageSessionId": "triage-001",
    "voiceSessionId": "voice-001",
    "failureReason": "provider_timeout",
}

# A fully-populated row with all six G3-D3 timestamps.
_FULL_ROW: dict[str, Any] = {
    "dispatchId": _DISPATCH_ID,
    "tenantId": _TENANT_A,
    "status": "FAILED",
    "etaMinutes": Decimal("8"),
    "providerRef": "SIM-XYZ",
    "simulated": True,
    "serviceType": "TOWING",
    "createdAt":    "2026-09-24T12:00:00.000Z",
    "dispatchedAt": "2026-09-24T12:00:05.000Z",
    "enRouteAt":    "2026-09-24T12:05:00.000Z",
    "arrivedAt":    "2026-09-24T12:12:30.000Z",
    "failedAt":     "2026-09-24T12:25:00.000Z",
    "updatedAt":    "2026-09-24T12:25:00.000Z",
}

# G3-D8's never-returned fields, owned by the test (F11.1). Kept independent of
# the handler's _ALLOWED_ATTRS so widening the handler cannot widen the test.
_NEVER_RETURNED = frozenset({
    "vin", "lat", "lon", "callbackContact", "driverId", "driverName",
    "vehicleId", "connectContactId", "summary", "reason", "triageSessionId",
    "voiceSessionId", "tenantId", "failureReason",
})
assert len(_NEVER_RETURNED) == 14
assert not (_NEVER_RETURNED & _EXPECTED_KEYS)

# F11.1: the fully-populated row also carries every never-returned field, so the
# exact-key-set test sees any leak, not only the subset test.
_FULL_ROW.update({k: _BASE_ROW[k] for k in _NEVER_RETURNED})
assert _NEVER_RETURNED <= set(_FULL_ROW)

# ---------------------------------------------------------------------------
# Helper builders
# ---------------------------------------------------------------------------


def _event(dispatch_id: str = _DISPATCH_ID, tenant_id: str = _TENANT_A) -> dict:
    return {
        "requestContext": {
            "authorizer": {
                "claims": {"custom:tenantId": tenant_id},
            }
        },
        "pathParameters": {"dispatchId": dispatch_id},
    }


def _decode(resp: dict) -> dict:
    return json.loads(resp["body"])


def _mock_table(item: dict | None):
    mock_tbl = MagicMock()
    mock_tbl.get_item.return_value = {"Item": item} if item is not None else {}
    mock_resource = MagicMock()
    mock_resource.Table.return_value = mock_tbl
    return mock_resource


# ---------------------------------------------------------------------------
# Auth guard
# ---------------------------------------------------------------------------


class TestAuthGuard:
    def test_missing_tenant_claim_returns_401(self):
        event = {
            "requestContext": {"authorizer": {"claims": {}}},
            "pathParameters": {"dispatchId": _DISPATCH_ID},
        }
        resp = h.handler(event, None)
        assert resp["statusCode"] == 401

    def test_null_claims_returns_401(self):
        event = {
            "requestContext": {"authorizer": {"claims": None}},
            "pathParameters": {"dispatchId": _DISPATCH_ID},
        }
        resp = h.handler(event, None)
        assert resp["statusCode"] == 401

    def test_missing_request_context_returns_401(self):
        event = {"pathParameters": {"dispatchId": _DISPATCH_ID}}
        resp = h.handler(event, None)
        assert resp["statusCode"] == 401

    def test_tenant_id_claim_fallback(self):
        """Claims carry only `tenantId` (no `custom:tenantId`) → caller can read
        their own row (G3-D9: extraction same as /escalate which accepts either key).

        Mutation M12: drop the `claims.get('tenantId')` fallback → this test MUST fail.
        """
        row = dict(_BASE_ROW)
        row["tenantId"] = _TENANT_A
        event = {
            "requestContext": {
                "authorizer": {
                    "claims": {"tenantId": _TENANT_A},  # no custom:tenantId
                }
            },
            "pathParameters": {"dispatchId": _DISPATCH_ID},
        }
        with patch.object(h, "_get_ddb", return_value=_mock_table(row)):
            resp = h.handler(event, None)
        assert resp["statusCode"] == 200, (
            "tenantId claim fallback must allow reading own row (G3-D9)"
        )


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------


class TestInputValidation:
    def test_missing_dispatch_id_returns_400(self):
        event = _event()
        event["pathParameters"] = {}
        resp = h.handler(event, None)
        assert resp["statusCode"] == 400

    def test_malformed_dispatch_id_returns_400(self):
        resp = h.handler(_event(dispatch_id="not-a-valid-id"), None)
        assert resp["statusCode"] == 400

    def test_empty_dispatch_id_returns_400(self):
        event = _event()
        event["pathParameters"]["dispatchId"] = ""
        resp = h.handler(event, None)
        assert resp["statusCode"] == 400

    def test_dispatch_id_format_without_rsa_prefix_returns_400(self):
        resp = h.handler(
            _event(dispatch_id="12345678-1234-1234-1234-123456789012"),
            None,
        )
        assert resp["statusCode"] == 400


# ---------------------------------------------------------------------------
# Not-found and cross-tenant isolation (G3-D9) — F10.2
# ---------------------------------------------------------------------------


class TestTenantIsolation:
    # MUTATION-VERIFIED (a): remove tenant check → FAILS
    def test_cross_tenant_read_returns_404(self):
        """Tenant-B caller cannot read tenant-A's row."""
        row = dict(_BASE_ROW)
        row["tenantId"] = _TENANT_A
        with patch.object(h, "_get_ddb", return_value=_mock_table(row)):
            resp = h.handler(_event(tenant_id=_TENANT_B), None)
        assert resp["statusCode"] == 404

    def test_absent_row_returns_404(self):
        with patch.object(h, "_get_ddb", return_value=_mock_table(None)):
            resp = h.handler(_event(), None)
        assert resp["statusCode"] == 404

    # MUTATION-VERIFIED (M11, M11b): F10.2 critical fix
    def test_row_with_empty_tenant_is_same_404_as_absent_row(self):
        """A row with tenantId='' returns 404 for a non-empty caller, and the
        response body is byte-identical to the absent-row 404 (G3-D9 existence oracle).

        Two mutations must both fail:
          M11  — untenanted row → distinct body "dispatch has no owner"
          M11b — untenanted row readable by any tenant with a non-empty claim
        """
        # Absent-row baseline
        with patch.object(h, "_get_ddb", return_value=_mock_table(None)):
            absent_resp = h.handler(_event(tenant_id=_TENANT_A), None)

        # Row with empty tenantId, non-empty caller (M11b target path)
        row_empty = dict(_BASE_ROW)
        row_empty["tenantId"] = ""
        with patch.object(h, "_get_ddb", return_value=_mock_table(row_empty)):
            empty_resp = h.handler(_event(tenant_id=_TENANT_A), None)

        assert empty_resp["statusCode"] == 404, (
            "Row with empty tenantId must return 404 for a non-empty caller"
        )
        assert _decode(empty_resp) == _decode(absent_resp), (
            "Empty-tenantId row and absent row must return identical 404 bodies (G3-D9)"
        )

    def test_row_with_absent_tenant_key_is_same_404_as_absent_row(self):
        """A row with no tenantId attribute at all is the same 404 as absent (G3-D9)."""
        with patch.object(h, "_get_ddb", return_value=_mock_table(None)):
            absent_resp = h.handler(_event(tenant_id=_TENANT_A), None)

        row_no_key = {k: v for k, v in _BASE_ROW.items() if k != "tenantId"}
        with patch.object(h, "_get_ddb", return_value=_mock_table(row_no_key)):
            no_key_resp = h.handler(_event(tenant_id=_TENANT_A), None)

        assert no_key_resp["statusCode"] == 404
        assert _decode(no_key_resp) == _decode(absent_resp), (
            "Row without tenantId key must return same 404 body as absent row (G3-D9)"
        )

    # MUTATION-VERIFIED (c): distinct 404 bodies → FAILS
    def test_not_found_and_cross_tenant_have_same_response(self):
        """Not-found and cross-tenant reads return identical 404 bodies (G3-D9)."""
        row = dict(_BASE_ROW)
        row["tenantId"] = _TENANT_A
        with patch.object(h, "_get_ddb", return_value=_mock_table(row)):
            cross_resp = h.handler(_event(tenant_id=_TENANT_B), None)
        with patch.object(h, "_get_ddb", return_value=_mock_table(None)):
            absent_resp = h.handler(_event(), None)

        assert cross_resp["statusCode"] == absent_resp["statusCode"]
        assert _decode(cross_resp) == _decode(absent_resp)

    def test_own_tenant_row_returns_200(self):
        with patch.object(h, "_get_ddb", return_value=_mock_table(_BASE_ROW)):
            resp = h.handler(_event(tenant_id=_TENANT_A), None)
        assert resp["statusCode"] == 200


# ---------------------------------------------------------------------------
# Response projection (G3-D8) — F10.3
# ---------------------------------------------------------------------------


class TestAllowlistProjection:
    # MUTATION-VERIFIED (b): full item → FAILS
    def test_internal_fields_are_subset_of_allowlist(self):
        """The response keys must be a subset of the TEST-owned _EXPECTED_KEYS,
        and none of G3-D8's never-returned fields may appear.

        F11.1 (review Cycle 2, C): this previously compared against the handler's
        own `h._ALLOWED_ATTRS`, so widening the allowlist to include `vin` or
        `failureReason` widened the test's expectation with it and all tests
        stayed green. The comparison set now lives in the test.

        Mutations: add `vin`, `callbackContact` or `failureReason` to
        `_ALLOWED_ATTRS` → this test MUST fail.
        """
        with patch.object(h, "_get_ddb", return_value=_mock_table(_BASE_ROW)):
            resp = h.handler(_event(), None)
        assert resp["statusCode"] == 200
        body = _decode(resp)
        assert set(body) <= _EXPECTED_KEYS, (
            f"Response contains keys outside G3-D8: {set(body) - _EXPECTED_KEYS}"
        )
        leaked = set(body) & _NEVER_RETURNED
        assert not leaked, f"G3-D8 never-returned fields leaked: {leaked}"

    # MUTATION-VERIFIED (M1): drop enRouteAt/arrivedAt/failedAt from _ALLOWED_ATTRS → FAILS
    def test_full_row_response_key_set(self):
        """For a fully-populated row the response key set equals exactly _EXPECTED_KEYS.

        G3-D3: 3.1 reads what 3.2 writes. The three lifecycle timestamps
        (enRouteAt, arrivedAt, failedAt) are written by the advancer and consumer;
        without them in _ALLOWED_ATTRS the status endpoint could never show progress.

        Mutation M1: drop enRouteAt, arrivedAt, failedAt from _ALLOWED_ATTRS →
        those keys are absent from the body, and this assertion fails because
        set(body) ≠ _EXPECTED_KEYS.
        """
        with patch.object(h, "_get_ddb", return_value=_mock_table(_FULL_ROW)):
            resp = h.handler(_event(), None)
        assert resp["statusCode"] == 200
        body = _decode(resp)
        assert set(body) == _EXPECTED_KEYS, (
            f"Expected exactly {_EXPECTED_KEYS}, got {set(body)}. "
            f"Missing: {_EXPECTED_KEYS - set(body)}. "
            f"Extra: {set(body) - _EXPECTED_KEYS}."
        )

    def test_all_six_g3d3_timestamps_round_trip(self):
        """All six G3-D3 timestamps in _FULL_ROW round-trip through the response."""
        with patch.object(h, "_get_ddb", return_value=_mock_table(_FULL_ROW)):
            resp = h.handler(_event(), None)
        body = _decode(resp)
        assert body["createdAt"]    == "2026-09-24T12:00:00.000Z"
        assert body["dispatchedAt"] == "2026-09-24T12:00:05.000Z"
        assert body["enRouteAt"]    == "2026-09-24T12:05:00.000Z"
        assert body["arrivedAt"]    == "2026-09-24T12:12:30.000Z"
        assert body["failedAt"]     == "2026-09-24T12:25:00.000Z"
        assert body["updatedAt"]    == "2026-09-24T12:25:00.000Z"

    def test_optional_timestamps_absent_when_not_in_row(self):
        row = {
            "dispatchId": _DISPATCH_ID,
            "tenantId": _TENANT_A,
            "status": "REQUESTED",
            "createdAt": "2026-09-24T12:00:00.000Z",
        }
        with patch.object(h, "_get_ddb", return_value=_mock_table(row)):
            resp = h.handler(_event(), None)
        body = _decode(resp)
        assert resp["statusCode"] == 200
        assert "enRouteAt" not in body
        assert "arrivedAt" not in body
        assert "dispatchedAt" not in body
        assert "failedAt" not in body

    def test_decimal_eta_minutes_is_int(self):
        """Decimal etaMinutes is converted to int; not silently truncated for other types."""
        row = dict(_BASE_ROW)
        row["etaMinutes"] = Decimal("18")
        with patch.object(h, "_get_ddb", return_value=_mock_table(row)):
            resp = h.handler(_event(), None)
        body = _decode(resp)
        assert body["etaMinutes"] == 18
        assert isinstance(body["etaMinutes"], int)

    # MUTATION-VERIFIED (M9): asOf hardcoded to 1970 → FAILS (F10.3)
    def test_as_of_is_recent(self):
        """asOf is parsed with G3-D3 format and falls within 10 seconds of now.

        Mutation M9: hardcode asOf to '1970-01-01T00:00:00.000Z' →
        the parsed value is far in the past, and the ≤10s assertion fails.
        """
        before = datetime.now(tz=timezone.utc)

        row = {
            "dispatchId": _DISPATCH_ID,
            "tenantId": _TENANT_A,
            "status": "REQUESTED",
            "createdAt": "2026-09-24T12:00:00.000Z",
        }
        with patch.object(h, "_get_ddb", return_value=_mock_table(row)):
            resp = h.handler(_event(), None)
        after = datetime.now(tz=timezone.utc)

        body = _decode(resp)
        as_of_str = body["asOf"]
        # Parse with the exact G3-D3 format.
        as_of = datetime.strptime(as_of_str, "%Y-%m-%dT%H:%M:%S.%fZ").replace(
            tzinfo=timezone.utc
        )
        assert before - timedelta(seconds=1) <= as_of <= after + timedelta(seconds=10), (
            f"asOf {as_of_str!r} is not within 10 seconds of the test's clock "
            f"(before={before.isoformat()}, after={after.isoformat()})"
        )


# ---------------------------------------------------------------------------
# simulated field (G3-D7)
# ---------------------------------------------------------------------------


class TestSimulatedField:
    def test_simulated_true_from_row_attribute(self):
        row = dict(_BASE_ROW)
        row["simulated"] = True
        with patch.object(h, "_get_ddb", return_value=_mock_table(row)):
            resp = h.handler(_event(), None)
        assert _decode(resp)["simulated"] is True

    def test_simulated_false_from_row_attribute(self):
        row = dict(_BASE_ROW)
        row["simulated"] = False
        with patch.object(h, "_get_ddb", return_value=_mock_table(row)):
            resp = h.handler(_event(), None)
        assert _decode(resp)["simulated"] is False

    def test_simulated_absent_with_fake_client_returns_true(self):
        row = {
            "dispatchId": _DISPATCH_ID,
            "tenantId": _TENANT_A,
            "status": "REQUESTED",
            "createdAt": "2026-09-24T12:00:00.000Z",
        }
        with patch.object(h, "_VSA_RSA_CLIENT", "fake"):
            with patch.object(h, "_get_ddb", return_value=_mock_table(row)):
                resp = h.handler(_event(), None)
        assert _decode(resp)["simulated"] is True

    def test_simulated_absent_with_real_client_returns_null(self):
        row = {
            "dispatchId": _DISPATCH_ID,
            "tenantId": _TENANT_A,
            "status": "REQUESTED",
            "createdAt": "2026-09-24T12:00:00.000Z",
        }
        with patch.object(h, "_VSA_RSA_CLIENT", "real"):
            with patch.object(h, "_get_ddb", return_value=_mock_table(row)):
                resp = h.handler(_event(), None)
        assert _decode(resp)["simulated"] is None

    # MUTATION-VERIFIED (M13): real-mode fallback inferred from SIM- prefix → FAILS (F10.5)
    def test_simulated_absent_real_client_with_sim_ref_returns_null(self):
        """Real-mode + absent simulated attribute + SIM- providerRef → null.

        The `simulated` field must come from the row attribute only; it is never
        inferred from the providerRef prefix (G3-D7 Accept 3: 'never inferred
        from the prefix').

        Mutation M13: return True when providerRef starts with 'SIM-' →
        this test must fail (body['simulated'] would be True instead of None).
        """
        row = {
            "dispatchId": _DISPATCH_ID,
            "tenantId": _TENANT_A,
            "status": "DISPATCHED",
            "createdAt": "2026-09-24T12:00:00.000Z",
            "providerRef": "SIM-ABC123",  # SIM- prefix present but no simulated attr
        }
        with patch.object(h, "_VSA_RSA_CLIENT", "real"):
            with patch.object(h, "_get_ddb", return_value=_mock_table(row)):
                resp = h.handler(_event(), None)
        assert _decode(resp)["simulated"] is None, (
            "simulated must not be inferred from the providerRef prefix (G3-D7)"
        )


# ---------------------------------------------------------------------------
# DynamoDB error handling
# ---------------------------------------------------------------------------


class TestDynamoDbErrors:
    def test_ddb_client_error_returns_503(self):
        from botocore.exceptions import ClientError
        mock_tbl = MagicMock()
        mock_tbl.get_item.side_effect = ClientError(
            {"Error": {"Code": "InternalServerError", "Message": "simulated error"}},
            "GetItem",
        )
        mock_resource = MagicMock()
        mock_resource.Table.return_value = mock_tbl
        with patch.object(h, "_get_ddb", return_value=mock_resource):
            resp = h.handler(_event(), None)
        assert resp["statusCode"] == 503

    def test_ddb_botocore_error_returns_503(self):
        from botocore.exceptions import BotoCoreError
        mock_tbl = MagicMock()
        mock_tbl.get_item.side_effect = BotoCoreError()
        mock_resource = MagicMock()
        mock_resource.Table.return_value = mock_tbl
        with patch.object(h, "_get_ddb", return_value=mock_resource):
            resp = h.handler(_event(), None)
        assert resp["statusCode"] == 503


# ---------------------------------------------------------------------------
# Correct table name used
# ---------------------------------------------------------------------------


class TestTableTarget:
    def test_handler_reads_from_rsa_dispatch_table(self):
        tables_accessed: list[str] = []
        mock_tbl = MagicMock()
        mock_tbl.get_item.return_value = {}
        mock_resource = MagicMock()

        def _capture_table(name: str):
            tables_accessed.append(name)
            return mock_tbl

        mock_resource.Table.side_effect = _capture_table
        with patch.object(h, "_get_ddb", return_value=mock_resource):
            h.handler(_event(), None)
        assert tables_accessed == [h._RSA_DISPATCH_TABLE]


# ---------------------------------------------------------------------------
# CORS headers — all responses carry Access-Control-Allow-Origin (F9.4)
# ---------------------------------------------------------------------------


class TestCorsHeaders:
    def _assert_cors(self, resp: dict) -> None:
        headers = resp.get("headers", {})
        assert "Access-Control-Allow-Origin" in headers
        assert "Access-Control-Allow-Headers" in headers

    def test_200_response_has_cors_headers(self):
        item = {
            "dispatchId": _DISPATCH_ID,
            "status": "DISPATCHED",
            "createdAt": "2026-09-24T12:00:00.000Z",
            "tenantId": _TENANT_A,
            "simulated": True,
        }
        with patch.object(h, "_get_ddb", return_value=_mock_table(item)):
            resp = h.handler(_event(), None)
        assert resp["statusCode"] == 200
        self._assert_cors(resp)

    def test_401_response_has_cors_headers(self):
        event = _event()
        event["requestContext"]["authorizer"]["claims"] = {}
        resp = h.handler(event, None)
        assert resp["statusCode"] == 401
        self._assert_cors(resp)

    def test_404_response_has_cors_headers(self):
        mock_tbl = MagicMock()
        mock_tbl.get_item.return_value = {}
        mock_resource = MagicMock()
        mock_resource.Table.return_value = mock_tbl
        with patch.object(h, "_get_ddb", return_value=mock_resource):
            resp = h.handler(_event(), None)
        assert resp["statusCode"] == 404
        self._assert_cors(resp)
