"""Test skeletons for lambdas/api-acquire-leads/handler.py

These tests FAIL (red) because handler.py does not exist yet.
They MUST collect cleanly — no ImportError, no AttributeError on collection.

Run:
    python3 -m pytest lambdas/api-acquire-leads/ -v

Contract coverage
-----------------
* 200/201 happy path — POST /acquire/leads creates a lead (feature-flagged OFF by default)
* GET /acquire/leads/{leadId} returns a lead record
* 400 on malformed input (missing required fields, missing path param)
* 403 when the path/body tenantId does not match the caller's Cognito claim
* 404 on unknown leadId (GET path)
* Feature-flag OFF → POST returns 404 (endpoint not available) not 500

Loading pattern: importlib.util.spec_from_file_location (no __init__.py).
"""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Attempt to load the handler.
# ---------------------------------------------------------------------------
_HANDLER_PATH = Path(__file__).parent.parent / "handler.py"
_handler_mod = None

if _HANDLER_PATH.exists():
    _spec = importlib.util.spec_from_file_location(
        "api_acquire_leads_handler", _HANDLER_PATH
    )
    _handler_mod = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(_handler_mod)


def _skip_if_no_handler():
    if _handler_mod is None:
        pytest.fail(
            "lambdas/api-acquire-leads/handler.py does not exist yet. "
            "These tests are expected to fail (red) until Group 2 implements it."
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _post_event(
    body: dict | None = None,
    claim_tenant: str = "meridian-demo",
    claim_sub: str = "a1b2c3d4-0000-0000-0000-000000000001",
    claim_groups: str = "kiosk",
) -> dict:
    return {
        "httpMethod": "POST",
        "pathParameters": None,
        "requestContext": {
            "authorizer": {
                "claims": {
                    "sub": claim_sub,
                    "custom:tenantId": claim_tenant,
                    "cognito:groups": claim_groups,
                }
            }
        },
        "queryStringParameters": None,
        "body": json.dumps(body) if body is not None else None,
    }


def _get_event(
    lead_id: str = "LEAD-20260731-abc12345",
    claim_tenant: str = "meridian-demo",
    claim_sub: str = "a1b2c3d4-0000-0000-0000-000000000001",
    claim_groups: str = "kiosk",
) -> dict:
    return {
        "httpMethod": "GET",
        "pathParameters": {"leadId": lead_id},
        "requestContext": {
            "authorizer": {
                "claims": {
                    "sub": claim_sub,
                    "custom:tenantId": claim_tenant,
                    "cognito:groups": claim_groups,
                }
            }
        },
        "queryStringParameters": None,
        "body": None,
    }


_VALID_POST_BODY = {
    "tenantId": "meridian-demo",
    "sessionId": "session-abc123",
    "contactInfo": {"email": "visitor@example.com"},
}

_LEAD_ITEM = {
    "leadId": "LEAD-20260731-abc12345",
    "tenantId": "meridian-demo",
    "sessionId": "session-abc123",
    "capturedAt": "2026-07-31T11:00:00+00:00",
    "contactInfo": {"email": "visitor@example.com"},
}


def _make_ddb_write_ok():
    table = MagicMock()
    table.put_item.return_value = {}
    table.get_item.return_value = {"Item": _LEAD_ITEM}
    resource = MagicMock()
    resource.Table.return_value = table
    return resource


def _make_ddb_not_found():
    table = MagicMock()
    table.get_item.return_value = {}  # No "Item" key
    resource = MagicMock()
    resource.Table.return_value = table
    return resource


# ---------------------------------------------------------------------------
# POST — feature-flag OFF (default)
# ---------------------------------------------------------------------------

class TestLeadsFeatureFlagOff:
    def test_post_returns_404_when_flag_off(self):
        """VSA_ACQUIRE_LEAD_CAPTURE_ENABLED defaults to OFF.
        POST must return a 4xx (not 500) to signal endpoint unavailable."""
        _skip_if_no_handler()
        with patch.object(_handler_mod, "_get_ddb", return_value=_make_ddb_write_ok()), \
             patch.dict(os.environ, {"VSA_ACQUIRE_LEAD_CAPTURE_ENABLED": "false"}, clear=False):
            resp = _handler_mod.handler(_post_event(_VALID_POST_BODY), None)
        assert resp["statusCode"] in (403, 404, 503), (
            f"Expected 4xx when lead capture is disabled, got {resp['statusCode']}"
        )


# ---------------------------------------------------------------------------
# POST — feature-flag ON
# ---------------------------------------------------------------------------

class TestLeadsPostEnabled:
    def test_post_returns_201_when_flag_on(self):
        _skip_if_no_handler()
        with patch.object(_handler_mod, "_get_ddb", return_value=_make_ddb_write_ok()), \
             patch.dict(os.environ, {"VSA_ACQUIRE_LEAD_CAPTURE_ENABLED": "true"}, clear=False):
            resp = _handler_mod.handler(_post_event(_VALID_POST_BODY), None)
        assert resp["statusCode"] == 201

    def test_post_response_contains_lead_id(self):
        _skip_if_no_handler()
        with patch.object(_handler_mod, "_get_ddb", return_value=_make_ddb_write_ok()), \
             patch.dict(os.environ, {"VSA_ACQUIRE_LEAD_CAPTURE_ENABLED": "true"}, clear=False):
            body = json.loads(
                _handler_mod.handler(_post_event(_VALID_POST_BODY), None)["body"]
            )
        assert "leadId" in body

    def test_post_missing_tenant_id_returns_400(self):
        _skip_if_no_handler()
        bad_body = {k: v for k, v in _VALID_POST_BODY.items() if k != "tenantId"}
        with patch.object(_handler_mod, "_get_ddb", return_value=_make_ddb_write_ok()), \
             patch.dict(os.environ, {"VSA_ACQUIRE_LEAD_CAPTURE_ENABLED": "true"}, clear=False):
            resp = _handler_mod.handler(_post_event(bad_body), None)
        assert resp["statusCode"] == 400

    def test_post_invalid_json_returns_400(self):
        _skip_if_no_handler()
        evt = _post_event()
        evt["body"] = "not-json"
        with patch.object(_handler_mod, "_get_ddb", return_value=_make_ddb_write_ok()), \
             patch.dict(os.environ, {"VSA_ACQUIRE_LEAD_CAPTURE_ENABLED": "true"}, clear=False):
            resp = _handler_mod.handler(evt, None)
        assert resp["statusCode"] == 400


# ---------------------------------------------------------------------------
# POST 403 — tenant clamp
# ---------------------------------------------------------------------------

class TestLeadsTenantClamp:
    def test_body_tenant_mismatch_returns_403(self):
        """Body has tenantId=other-tenant but claim says meridian-demo → 403."""
        _skip_if_no_handler()
        mismatched = {**_VALID_POST_BODY, "tenantId": "other-tenant"}
        with patch.object(_handler_mod, "_get_ddb", return_value=_make_ddb_write_ok()), \
             patch.dict(os.environ, {"VSA_ACQUIRE_LEAD_CAPTURE_ENABLED": "true"}, clear=False):
            resp = _handler_mod.handler(
                _post_event(mismatched, claim_tenant="meridian-demo"), None
            )
        assert resp["statusCode"] == 403


# ---------------------------------------------------------------------------
# GET /acquire/leads/{leadId}
# ---------------------------------------------------------------------------

class TestLeadsGet:
    def test_get_returns_200(self):
        _skip_if_no_handler()
        with patch.object(_handler_mod, "_get_ddb", return_value=_make_ddb_write_ok()):
            resp = _handler_mod.handler(_get_event(), None)
        assert resp["statusCode"] == 200

    def test_get_returns_lead_id(self):
        _skip_if_no_handler()
        with patch.object(_handler_mod, "_get_ddb", return_value=_make_ddb_write_ok()):
            body = json.loads(_handler_mod.handler(_get_event(), None)["body"])
        assert "leadId" in body

    def test_get_missing_lead_id_param_returns_400(self):
        _skip_if_no_handler()
        bad_event = _get_event()
        bad_event["pathParameters"] = {}
        with patch.object(_handler_mod, "_get_ddb", return_value=_make_ddb_write_ok()):
            resp = _handler_mod.handler(bad_event, None)
        assert resp["statusCode"] == 400

    def test_get_unknown_lead_returns_404(self):
        _skip_if_no_handler()
        with patch.object(_handler_mod, "_get_ddb", return_value=_make_ddb_not_found()):
            resp = _handler_mod.handler(_get_event(lead_id="LEAD-unknown"), None)
        assert resp["statusCode"] == 404

    def test_get_lead_tenant_mismatch_returns_403(self):
        """Lead belongs to other-tenant; claim is meridian-demo → 403."""
        _skip_if_no_handler()
        other_item = {**_LEAD_ITEM, "tenantId": "other-tenant"}
        table = MagicMock()
        table.get_item.return_value = {"Item": other_item}
        resource = MagicMock()
        resource.Table.return_value = table
        with patch.object(_handler_mod, "_get_ddb", return_value=resource):
            resp = _handler_mod.handler(
                _get_event(claim_tenant="meridian-demo"), None
            )
        assert resp["statusCode"] == 403
