"""GET /drivers/me follows Scan pagination when looking up the caller's driver row.

Issue ``issues/2026-10-26-drivers-me-demo-driver-not-found/``. The drivers table holds
7,257 rows (about 3 Scan pages); the demo driver ``DRV-MRDN-0015`` sits on page 3, and a
single-page Scan returned ``driver: null``, which the iOS app shows as "no vehicle
assigned". The fake below returns one page per call and a ``LastEvaluatedKey`` until the
last page, the way DynamoDB does when a Scan reaches its 1 MB limit.
"""
from __future__ import annotations

import importlib.util
import json
import pathlib
from typing import Any

import pytest

_HANDLER = pathlib.Path(__file__).resolve().parents[1] / "handler.py"
EMAIL = "meridian.driver@example.com"
DRIVER = {"driverId": "DRV-MRDN-0015", "cognitoEmail": EMAIL, "firstName": "Marcus",
          "assignedVehicleId": "MRDN0000000000015"}


def _load():
    spec = importlib.util.spec_from_file_location("api_drivers_me_handler_paging", _HANDLER)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _PagedTable:
    """Serves ``pages`` one per scan call; the filter is applied per page, as DynamoDB does."""

    def __init__(self, pages: list[list[dict[str, Any]]], endless: bool = False):
        self.pages = pages
        self.endless = endless
        self.start_keys: list[Any] = []

    def scan(self, **kw: Any) -> dict[str, Any]:
        cond = kw["FilterExpression"].get_expression()
        _attr, value = cond["values"]
        start = kw.get("ExclusiveStartKey")
        self.start_keys.append(start)
        idx = 0 if start is None else start["page"] + 1
        page = self.pages[idx % len(self.pages)] if self.endless else self.pages[idx]
        resp: dict[str, Any] = {"Items": [r for r in page if r.get("cognitoEmail") == value]}
        if self.endless or idx + 1 < len(self.pages):
            resp["LastEvaluatedKey"] = {"page": idx}
        return resp

    def get_item(self, **kw: Any) -> dict[str, Any]:
        return {}


class _Resource:
    def __init__(self, table: _PagedTable):
        self.table = table

    def Table(self, _name: str) -> _PagedTable:  # noqa: N802 — boto3 resource API
        return self.table


def _filler(n: int, tag: str) -> list[dict[str, Any]]:
    return [{"driverId": f"DRV-{tag}-{i}", "cognitoEmail": f"{tag}{i}@example.com"} for i in range(n)]


def _handler_with(monkeypatch, table: _PagedTable):
    mod = _load()
    monkeypatch.setattr(mod, "_get_ddb", lambda: _Resource(table))
    return mod


def _call(mod) -> dict[str, Any]:
    event = {"requestContext": {"authorizer": {"claims": {
        "sub": "s", "email": EMAIL, "email_verified": "true"}}}}
    resp = mod.handler(event, None)
    assert resp["statusCode"] == 200
    return json.loads(resp["body"])


def test_driver_on_page_three_is_found(monkeypatch):
    table = _PagedTable([_filler(5, "a"), _filler(5, "b"), _filler(2, "c") + [dict(DRIVER)]])
    body = _call(_handler_with(monkeypatch, table))
    assert body["driver"]["driverId"] == "DRV-MRDN-0015"
    assert body["driver"]["assignedVehicleId"] == "MRDN0000000000015"
    assert table.start_keys == [None, {"page": 0}, {"page": 1}]


def test_driver_absent_from_every_page_reads_all_pages_then_returns_null(monkeypatch):
    table = _PagedTable([_filler(3, "a"), _filler(3, "b"), _filler(3, "c")])
    body = _call(_handler_with(monkeypatch, table))
    assert body["driver"] is None
    assert len(table.start_keys) == 3


def test_scan_stops_at_the_page_cap(monkeypatch):
    table = _PagedTable([_filler(2, "a")], endless=True)
    mod = _handler_with(monkeypatch, table)
    body = _call(mod)
    assert body["driver"] is None
    assert len(table.start_keys) == mod._MAX_SCAN_PAGES
