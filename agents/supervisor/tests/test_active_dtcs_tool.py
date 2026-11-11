"""get_active_dtcs: the session vehicle's active trouble codes.

issues/2026-09-29-agent-cannot-name-active-dtcs/. The model had no way to read a
vehicle's codes, so for MEDIUM/LOW-only vehicles it told drivers it could not see
any. These tests pin the "active" rule (same as the classifier's), the session-only
vehicle scope, and that a failed read never looks like "no codes".

Mutations tried (2026-09-29), each caught by the named test:
  (a) clearedDate/resetAt filter removed: test_cleared_rows_are_not_active FAILED.
  (b) failed read returned as an empty success: test_failed_read_is_not_no_codes FAILED.
  (c) partial+empty treated as "no codes": test_partial_read_with_no_codes_is_unavailable FAILED.
  (d) VIN lookup accepts the first of several vehicles: test_ambiguous_vin_is_unresolved FAILED.
  (e) tool given a `vin` argument: test_tool_takes_no_arguments FAILED.
  (f) pagination stops after page 1: test_follows_pages FAILED.

Live check against real DynamoDB: scripts/live-verify-active-dtcs-tool.py.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from boto3.dynamodb.conditions import ConditionBase

from agents.supervisor.tools import active_dtcs as mod


class _FakeTable:
    def __init__(self, pages=None, fail_on_page=None, index_items=None, index_fails=False):
        self.pages = pages or [[]]
        self.fail_on_page = fail_on_page
        self.index_items = index_items or []
        self.index_fails = index_fails
        self.calls: list[dict] = []

    def query(self, **kwargs):
        assert isinstance(kwargs["KeyConditionExpression"], ConditionBase)
        self.calls.append(kwargs)
        if kwargs.get("IndexName") == "vin-index":
            if self.index_fails:
                raise RuntimeError("AccessDenied")
            return {"Items": self.index_items}
        page = int(kwargs.get("ExclusiveStartKey", {}).get("page", 0))
        if self.fail_on_page is not None and page == self.fail_on_page:
            raise RuntimeError("ProvisionedThroughputExceeded")
        resp = {"Items": self.pages[page]}
        if page + 1 < len(self.pages):
            resp["LastEvaluatedKey"] = {"page": page + 1}
        return resp


class _FakeDdb:
    def __init__(self, dtc: _FakeTable, vehicles: _FakeTable | None = None):
        self.dtc, self.vehicles = dtc, vehicles or _FakeTable()

    def Table(self, name):  # noqa: N802 - boto3 name
        return self.dtc if name.endswith("dtc-history") else self.vehicles


@pytest.fixture
def run(monkeypatch):
    monkeypatch.setenv("CMS_STAGE", "staging")
    monkeypatch.delenv("CMS_DTC_HISTORY_TABLE", raising=False)
    monkeypatch.delenv("CMS_VEHICLES_TABLE", raising=False)

    def _run(ddb, vehicle_id="VEH-1", vin="VIN0000000000001"):
        monkeypatch.setattr(mod, "_ddb", lambda: ddb)
        session = SimpleNamespace(vehicle_id=vehicle_id, vin=vin, session_id="s-1")
        return mod.make_active_dtcs_tool(session)()

    return _run


def _row(code, **kw):
    return {"vehicleId": "VEH-1", "code": code, "status": kw.pop("status", "ACTIVE"), **kw}


# --- The active rule ------------------------------------------------------------

def test_returns_active_codes_with_severity_and_description(run):
    ddb = _FakeDdb(_FakeTable([[
        _row("P0420", severity="MEDIUM", description="Catalyst System Efficiency Below Threshold"),
        _row("B1234", severity="2", description="Occupant Classification System Mismatch"),
    ]]))
    out = run(ddb)
    assert out["available"] is True and out["readStatus"] == "complete"
    assert out["count"] == 2
    assert out["activeDtcs"][0] == {
        "code": "P0420", "severity": "MEDIUM", "description": "Catalyst System Efficiency Below Threshold",
    }
    assert out["activeDtcs"][1]["severity"] == "MEDIUM"  # numeric 2 normalised


def test_cleared_rows_are_not_active(run):
    ddb = _FakeDdb(_FakeTable([[
        _row("P0562", status="CLEARED"),
        _row("C1234", status="RESOLVED"),
        _row("P0171", clearedDate="2026-09-01"),
        _row("P0300", resetAt="2026-09-01"),
        _row("P0420"),
    ]]))
    assert [d["code"] for d in run(ddb)["activeDtcs"]] == ["P0420"]


def test_one_entry_per_code_newest_first(run):
    ddb = _FakeDdb(_FakeTable([[
        _row("P0420", description="newest"),
        _row("P0420", description="older"),
    ]]))
    out = run(ddb)
    assert out["count"] == 1 and out["activeDtcs"][0]["description"] == "newest"


def test_no_codes_is_available_and_empty(run):
    out = run(_FakeDdb(_FakeTable([[_row("P0562", status="CLEARED")]])))
    assert out == {"available": True, "vehicleId": "VEH-1", "activeDtcs": [], "count": 0, "readStatus": "complete"}


def test_follows_pages(run):
    ddb = _FakeDdb(_FakeTable([[_row("P0420")], [_row("B1234")]]))
    out = run(ddb)
    assert {d["code"] for d in out["activeDtcs"]} == {"P0420", "B1234"}
    assert out["readStatus"] == "complete"


def test_stops_after_max_pages_and_says_partial(run):
    pages = [[_row(f"P0{n:03d}")] for n in range(mod._MAX_PAGES + 2)]
    ddb = _FakeDdb(_FakeTable(pages))
    out = run(ddb)
    assert out["readStatus"] == "partial" and out["count"] == mod._MAX_PAGES
    assert len(ddb.dtc.calls) == mod._MAX_PAGES


def test_caps_codes(run):
    ddb = _FakeDdb(_FakeTable([[_row(f"P1{n:03d}") for n in range(25)]]))
    assert run(ddb)["count"] == mod._MAX_CODES


# --- Failure is not "no codes" -----------------------------------------------------

def test_failed_read_is_not_no_codes(run):
    out = run(_FakeDdb(_FakeTable(fail_on_page=0)))
    assert out["available"] is False
    assert "activeDtcs" not in out
    assert "right now" in out["reason"]


def test_partial_read_with_no_codes_is_unavailable(run):
    ddb = _FakeDdb(_FakeTable([[_row("P0562", status="CLEARED")], []], fail_on_page=1))
    assert run(ddb)["available"] is False


def test_partial_read_with_codes_is_reported_partial(run):
    ddb = _FakeDdb(_FakeTable([[_row("P0420")], []], fail_on_page=1))
    out = run(ddb)
    assert out["available"] is True and out["readStatus"] == "partial"


# --- Vehicle scope ---------------------------------------------------------------

def test_uses_session_vehicle_id_without_a_vin_lookup(run):
    ddb = _FakeDdb(_FakeTable([[_row("P0420")]]))
    run(ddb, vehicle_id="VEH-1")
    assert ddb.vehicles.calls == []


def test_resolves_vin_when_no_vehicle_id(run):
    ddb = _FakeDdb(_FakeTable([[_row("P0420")]]), _FakeTable(index_items=[{"vehicleId": "VEH-9", "vin": "V"}]))
    out = run(ddb, vehicle_id=None)
    assert out["vehicleId"] == "VEH-9"
    assert ddb.vehicles.calls[0]["IndexName"] == "vin-index"


def test_ambiguous_vin_is_unresolved(run):
    two = [{"vehicleId": "VEH-9"}, {"vehicleId": "VEH-10"}]
    ddb = _FakeDdb(_FakeTable([[_row("P0420")]]), _FakeTable(index_items=two))
    out = run(ddb, vehicle_id=None)
    assert out["available"] is False and ddb.dtc.calls == []


def test_vin_lookup_failure_says_right_now(run):
    ddb = _FakeDdb(_FakeTable([[_row("P0420")]]), _FakeTable(index_fails=True))
    out = run(ddb, vehicle_id=None)
    assert out["available"] is False and "right now" in out["reason"]


def test_no_vehicle_in_session(run):
    ddb = _FakeDdb(_FakeTable())
    out = run(ddb, vehicle_id=None, vin=None)
    assert out["available"] is False and "vehicleId" not in out
    assert ddb.dtc.calls == [] and ddb.vehicles.calls == []


def test_tool_takes_no_arguments():
    """The model cannot name a vehicle: the input schema has no properties."""
    t = mod.make_active_dtcs_tool(SimpleNamespace(vehicle_id="VEH-1", vin=None, session_id="s"))
    props = t.tool_spec["inputSchema"]["json"].get("properties", {})
    assert set(props) == set()


# --- Registration -----------------------------------------------------------------

@pytest.mark.parametrize("persona", ["fleet_driver", "vehicle_owner", "consumer", "service_advisor", "technician"])
def test_registered_for_vehicle_personas(persona):
    from agents.supervisor.tools import PERSONA_TOOL_FACTORIES

    assert mod.make_active_dtcs_tool in PERSONA_TOOL_FACTORIES[persona]



# --- Text runtime (build_supervisor) ------------------------------------------------
# The fleet_driver text path uses a fixed legacy tool list, not the registry. The
# first live run (2026-09-29) found the web chat still could not name codes because
# the tool was only in the registry. Mutation: removed from legacy_tools ->
# test_text_fleet_driver_path_has_the_tool FAILED.

def _build_text(persona: str, kiosk: bool = False):
    from unittest.mock import MagicMock, patch

    from agents.supervisor.session import SessionContext
    from agents.supervisor.supervisor import build_supervisor

    ctx = SessionContext(tenant_id="meridian-fleet", session_id="s-" + "0" * 32, jwt="j",
                         persona_id=persona, vin="MRDN0000000000015", is_kiosk=kiosk)
    cfg = SimpleNamespace(
        supervisor_model_id="m", aws_region="us-west-2", supervisor_max_tokens=10,
        memory_id="mem", memory_region="us-west-2", api_base="https://x.invalid",
        acquire_catalog_table="t", acquire_orders_table="t", acquire_leads_table="t",
        adp_kb_id="kb", adp_region="us-west-2", discover_guardrails_identifier="g",
        acquire_kb_source_category="c",
    )
    with patch("agents.supervisor.supervisor.BedrockModel", return_value=MagicMock()):
        return build_supervisor(ctx, cfg)


def test_text_fleet_driver_path_has_the_tool():
    assert "get_active_dtcs" in _build_text("fleet_driver").tool_names


def test_kiosk_never_gets_the_tool():
    assert "get_active_dtcs" not in _build_text("consumer", kiosk=True).tool_names


def test_remote_clears_and_cleared_at_rows_are_not_active(run):
    """Issue 2026-10-05-active-dtcs-reads-remote-clears-as-active."""
    from decimal import Decimal

    ddb = _FakeDdb(_FakeTable([[
        _row("P0300", status="CLEARED_REMOTE", clearedDate="", clearedAt=Decimal("1790799119132")),
        _row("P0171", clearedAt=Decimal("1790799119132")),
        _row("P0420", clearedAt="not-a-number"),
        _row("P0128"),
    ]]))
    assert [d["code"] for d in run(ddb)["activeDtcs"]] == ["P0420", "P0128"]
