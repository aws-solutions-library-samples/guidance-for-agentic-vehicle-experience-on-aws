"""bidi_app._fetch_active_dtcs skips remotely cleared rows.

Issue 2026-10-05-active-dtcs-reads-remote-clears-as-active: this reader had no clear-marker check
at all, only the status, so a CLEARED_REMOTE row primed the voice prompt with a cleared fault.
"""
from __future__ import annotations

from decimal import Decimal

import agents.supervisor.bidi_app as bidi


class _Table:
    def __init__(self, items):
        self._items = items

    def query(self, **_kw):
        return {"Items": self._items}


class _Ddb:
    def __init__(self, items):
        self._t = _Table(items)

    def Table(self, _name):  # noqa: N802 - boto3 resource API
        return self._t


def test_remote_clears_and_markers_are_skipped(monkeypatch):
    rows = [
        {"vehicleId": "V", "code": "P0300", "severity": "HIGH", "status": "CLEARED_REMOTE",
         "clearedDate": "", "clearedAt": Decimal("1790799119132")},
        {"vehicleId": "V", "code": "P0171", "severity": "HIGH", "status": "ACTIVE",
         "clearedDate": "2026-10-05T00:00:00Z"},
        {"vehicleId": "V", "code": "C1234", "severity": "CRITICAL", "status": "ACTIVE",
         "clearedAt": Decimal("1790799119132")},
        {"vehicleId": "V", "code": "P0217", "severity": "CRITICAL", "status": "ACTIVE"},
    ]
    monkeypatch.setattr(bidi, "_get_cms_ddb", lambda: _Ddb(rows))
    assert [d["code"] for d in bidi._fetch_active_dtcs("V")] == ["P0217"]
