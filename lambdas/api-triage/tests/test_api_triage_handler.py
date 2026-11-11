"""Unit tests for lambdas/api-triage/handler.py — tenant resolution and refusals.

issues/2026-09-29-web-chat-triage-401-claimless-callers/: most CMS web-chat
users carry no ``custom:tenantId`` claim, so ``/assistant/chat`` serves them as
``DEFAULT_TENANT_ID`` while this endpoint refused them with 401. These tests pin
the fallback AND the refusals that must survive it.

Mutations tried (2026-09-29), each caught by the named test:
  (a) Fallback applied even when a claim is present (default checked first):
      test_claim_wins_over_default FAILED. Reverted.
  (b) Mismatch check removed for default-sourced tenants:
      test_claimless_caller_cannot_pick_another_tenant FAILED. Reverted.
  (c) Hardcoded 'meridian-fleet' when DEFAULT_TENANT_ID is unset:
      test_claimless_without_default_is_401 FAILED. Reverted.
  (d) Whitespace-only claim treated as a claim (no strip):
      test_blank_claim_falls_back_to_default FAILED. Reverted.
  (e) `_log_safe` returning its input unchanged:
      test_client_values_cannot_forge_a_log_line FAILED. Reverted.

Run:
    python3 -m pytest lambdas/api-triage/ -v

Named test_api_triage_handler.py, not test_handler.py: under --import-mode=importlib
several lambdas/*/tests/test_handler.py files resolve to the same module name and
run each other's tests in a full-suite run.
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import sys
from unittest import mock

import pytest

_HANDLER_PATH = os.path.join(os.path.dirname(__file__), "..", "handler.py")
# handler.py imports its sibling triage_vehicle_scope, as it does at the root of
# the Lambda bundle.
_TRIAGE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _TRIAGE_DIR not in sys.path:
    sys.path.insert(0, _TRIAGE_DIR)


@pytest.fixture
def h(monkeypatch):
    """Load the handler fresh with a fake classifier client."""
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-west-2")
    monkeypatch.setenv("CLASSIFIER_FUNCTION_ARN", "arn:aws:lambda:us-west-2:123456789012:function:clf")
    monkeypatch.delenv("DEFAULT_TENANT_ID", raising=False)
    spec = importlib.util.spec_from_file_location("api_triage_handler", _HANDLER_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    fake = mock.MagicMock()
    fake.invoke.side_effect = lambda **kw: {
        "Payload": io.BytesIO(json.dumps({
            "classification": "P2",
            "sessionId": "s-1",
            "decidedAt": "2026-09-29T00:00:00Z",
            "latencyMs": 5,
        }).encode())
    }
    module._lambda = fake
    return module


def _event(claims: dict, body: dict | None = None) -> dict:
    return {
        "requestContext": {"authorizer": {"claims": claims}},
        "body": json.dumps(body if body is not None else {"vin": "MRDN0000000000015"}),
    }


def _classifier_tenant(h) -> str:
    """The tenantId the classifier was actually invoked with."""
    call = h._lambda.invoke.call_args
    return json.loads(call.kwargs["Payload"])["tenantId"]


# --- Fallback ---------------------------------------------------------------

def test_claimless_caller_gets_default_tenant(h, monkeypatch):
    monkeypatch.setenv("DEFAULT_TENANT_ID", "meridian-fleet")
    resp = h.handler(_event({"sub": "u1"}, {"vin": "V1", "tenantId": "meridian-fleet"}), None)
    assert resp["statusCode"] == 200
    assert _classifier_tenant(h) == "meridian-fleet"


def test_claimless_caller_without_body_tenant_gets_default(h, monkeypatch):
    monkeypatch.setenv("DEFAULT_TENANT_ID", "meridian-fleet")
    resp = h.handler(_event({"sub": "u1"}, {"vin": "V1"}), None)
    assert resp["statusCode"] == 200
    assert _classifier_tenant(h) == "meridian-fleet"


def test_blank_claim_falls_back_to_default(h, monkeypatch):
    monkeypatch.setenv("DEFAULT_TENANT_ID", "meridian-fleet")
    resp = h.handler(_event({"sub": "u1", "custom:tenantId": "   "}, {"vin": "V1"}), None)
    assert resp["statusCode"] == 200
    assert _classifier_tenant(h) == "meridian-fleet"


# --- Refusals that must survive the fallback ---------------------------------

def test_claimless_without_default_is_401(h):
    resp = h.handler(_event({"sub": "u1"}, {"vin": "V1", "tenantId": "meridian-fleet"}), None)
    assert resp["statusCode"] == 401
    h._lambda.invoke.assert_not_called()


def test_claimless_caller_cannot_pick_another_tenant(h, monkeypatch):
    monkeypatch.setenv("DEFAULT_TENANT_ID", "meridian-fleet")
    resp = h.handler(_event({"sub": "u1"}, {"vin": "V1", "tenantId": "michelin"}), None)
    assert resp["statusCode"] == 403
    h._lambda.invoke.assert_not_called()


def test_claim_wins_over_default(h, monkeypatch):
    monkeypatch.setenv("DEFAULT_TENANT_ID", "meridian-fleet")
    resp = h.handler(_event({"sub": "u1", "custom:tenantId": "meridian"}, {"vin": "V1"}), None)
    assert resp["statusCode"] == 200
    assert _classifier_tenant(h) == "meridian"


def test_claim_holder_cannot_use_default_tenant(h, monkeypatch):
    """A caller WITH a claim asking for the default tenant is still a mismatch."""
    monkeypatch.setenv("DEFAULT_TENANT_ID", "meridian-fleet")
    resp = h.handler(
        _event({"sub": "u1", "custom:tenantId": "meridian"}, {"vin": "V1", "tenantId": "meridian-fleet"}),
        None,
    )
    assert resp["statusCode"] == 403
    h._lambda.invoke.assert_not_called()


def test_claim_mismatch_is_403(h):
    resp = h.handler(
        _event({"sub": "u1", "custom:tenantId": "meridian"}, {"vin": "V1", "tenantId": "michelin"}),
        None,
    )
    assert resp["statusCode"] == 403
    h._lambda.invoke.assert_not_called()


def test_missing_vin_is_400(h, monkeypatch):
    monkeypatch.setenv("DEFAULT_TENANT_ID", "meridian-fleet")
    resp = h.handler(_event({"sub": "u1"}, {"tenantId": "meridian-fleet"}), None)
    assert resp["statusCode"] == 400
    h._lambda.invoke.assert_not_called()


# --- Logging ------------------------------------------------------------------

def test_refusal_is_logged_with_reason(h, caplog):
    with caplog.at_level("WARNING"):
        h.handler(_event({"sub": "u1"}, {"vin": "V1"}), None)
    assert any("refused 401" in r.getMessage() for r in caplog.records)


def test_client_values_cannot_forge_a_log_line(h, monkeypatch, caplog):
    """Security review 2026-09-29 W1: body tenantId and vin are logged sanitized."""
    monkeypatch.setenv("DEFAULT_TENANT_ID", "meridian-fleet")
    forged = "x\r\n\x00\ttriage: sub=victim tenant=michelin source=claim"
    with caplog.at_level("INFO"):
        h.handler(_event({"sub": "u1"}, {"vin": "V1", "tenantId": forged}), None)
        h.handler(_event({"sub": "u1"}, {"vin": forged}), None)
    msgs = [r.getMessage() for r in caplog.records if r.getMessage().startswith("triage:")]
    assert len(msgs) >= 2
    # Every character printable: catches \r, \x00 and tab, not only \n.
    assert all(m.isprintable() for m in msgs), msgs
