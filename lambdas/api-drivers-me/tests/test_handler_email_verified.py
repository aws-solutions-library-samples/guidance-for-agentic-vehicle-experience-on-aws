"""GET /drivers/me refuses an email Cognito has not verified, and a token with no email.

Issue ``issues/2026-09-24-drivers-me-trusts-unverified-client-writable-email/``. The route
returns a driver's record (licence, phone, assigned vehicle) for the row whose
``cognitoEmail`` equals the caller's ``email`` claim. A user who rewrote their email to a
driver's address without verifying it was admitted (demonstrated 2026-09-24). Each refusal
test has a must-succeed control with the same driver row, and asserts the drivers table
was never read.
"""
from __future__ import annotations

import importlib.util
import json
import logging
import pathlib
from typing import Any

import pytest

_HANDLER = pathlib.Path(__file__).resolve().parents[1] / "handler.py"
EMAIL = "meridian.driver@example.com"
DRIVER = {"driverId": "DRV-MRDN-0015", "cognitoEmail": EMAIL, "firstName": "M",
          "licenseNumber": "L-1"}


def _load():
    spec = importlib.util.spec_from_file_location("api_drivers_me_handler_ev", _HANDLER)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _Table:
    def __init__(self, rows: list[dict[str, Any]]):
        self.rows = rows
        self.scans: list[Any] = []

    def scan(self, **kw: Any) -> dict[str, Any]:
        cond = kw["FilterExpression"].get_expression()
        attr, value = cond["values"]
        assert attr.name == "cognitoEmail" and cond["operator"] == "="
        self.scans.append(value)
        return {"Items": [r for r in self.rows if r.get("cognitoEmail") == value]}

    def get_item(self, **kw: Any) -> dict[str, Any]:
        return {}


class _Resource:
    def __init__(self, table: _Table):
        self.table = table

    def Table(self, _name: str) -> _Table:  # noqa: N802 — boto3 resource API
        return self.table


@pytest.fixture
def h(monkeypatch):
    mod = _load()
    table = _Table([dict(DRIVER)])
    monkeypatch.setattr(mod, "_get_ddb", lambda: _Resource(table))
    mod._test_table = table  # type: ignore[attr-defined]
    return mod


def _event(**claims: Any) -> dict[str, Any]:
    return {"requestContext": {"authorizer": {"claims": claims}}}


@pytest.mark.parametrize("verified", ["true", True], ids=["api-gateway-string", "jwt-bool"])
def test_a_verified_email_reads_the_drivers_record(h, verified):
    """Control: the same row the refusals below protect is returned when verified."""
    resp = h.handler(_event(email=EMAIL, email_verified=verified, sub="s"), None)
    assert resp["statusCode"] == 200
    assert json.loads(resp["body"])["driver"]["driverId"] == "DRV-MRDN-0015"
    assert h._test_table.scans == [EMAIL]


@pytest.mark.parametrize("verified", ["false", False, None, "True", "1", 1, "yes", ""],
                         ids=["false", "bool-false", "missing", "True", "one", "int-1", "yes", "blank"])
def test_an_unverified_email_is_refused_before_any_read(h, verified):
    claims: dict[str, Any] = {"email": EMAIL, "sub": "s"}
    if verified is not None:
        claims["email_verified"] = verified
    resp = h.handler(_event(**claims), None)
    assert resp["statusCode"] == 403
    assert json.loads(resp["body"]) == {"error": "email not verified"}
    assert h._test_table.scans == [], "the drivers table must not be read for an unverified email"


def test_cognito_username_is_no_longer_a_fallback(h):
    """No email claim: 401, even when cognito:username equals a driver's address."""
    resp = h.handler(_event(**{"cognito:username": EMAIL, "email_verified": "true", "sub": "s"}), None)
    assert resp["statusCode"] == 401
    assert h._test_table.scans == []


@pytest.mark.parametrize("event", [
    _event(email="", email_verified="true"),
    _event(email="   ", email_verified="true"),
    _event(email_verified="true"),
    {"requestContext": {"authorizer": {}}},
    {"requestContext": {"authorizer": {"claims": "not-a-dict"}}},
    {},
], ids=["blank", "whitespace", "absent", "no-claims", "claims-not-a-dict", "no-context"])
def test_a_missing_email_is_refused(h, event):
    resp = h.handler(event, None)
    assert resp["statusCode"] == 401
    assert h._test_table.scans == []


def test_the_refusal_does_not_echo_or_log_the_unverified_email(h, caplog):
    caplog.set_level(logging.DEBUG)
    resp = h.handler(_event(email=EMAIL, email_verified="false", sub="s"), None)
    assert EMAIL not in resp["body"]
    assert EMAIL not in caplog.text
