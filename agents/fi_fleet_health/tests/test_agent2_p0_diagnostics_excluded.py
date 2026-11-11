"""Option B: a p0 diagnostics Finding never becomes an FI fleet-health booking need.

Spec ``2026-11-05-avx-proactive-p0-alert`` (decisions "Option B", user via prompt 299)
and spec ``2026-10-05-fi-fleet-health-stage-d`` (decisions, same date). The
diagnostics component now writes a ``p0`` Finding (text = the P0 directive) for a
(VIN, component) group holding a P0 code, so the driver's phone can be told. FI
fleet health must not read that Finding as a second booking need for the same
fault: it sees stop-driving faults only through the ADP fault path
(``select_fault_rows``, band ``p0``).

The Finding fixture is built by the real diagnostics writer (``build_fields`` from
``agents.health.cms_dtc_source``) and stored in the shape the runner writes, then
returned by a DynamoDB stub that does NOT apply the FilterExpression, so the
property is checked in the reader's own code, not in the stub.

Mutants (applied in a scratch copy, each caught; recorded in decisions.md):
  MB1  item check removed in ``select_agent2_findings``    -> test_p0_* fail
  MB2  ``:s_p0`` put back in the FilterExpression          -> test_query_never_asks_for_p0
  MB3  ``"p0"`` put back in ``_AGENT2_SEVERITY_BANDS``      -> test_p0_* fail
"""
from __future__ import annotations

import datetime
from typing import Any
from unittest.mock import MagicMock

from boto3.dynamodb.types import TypeSerializer

from agents.fi_fleet_health.selection import (
    _AGENT2_SEVERITY_BANDS,
    build_vehicle_rows,
    select_agent2_findings,
)
from agents.health.cms_dtc_source import build_fields
from agents.supervisor.dtc_response_catalog import is_p0_directive

_NOW = datetime.datetime(2026, 11, 6, 17, 0, 0, tzinfo=datetime.timezone.utc)
_LIVE = "2026-11-06T17:30:00Z"
_FLEET = "demo-lastmile-delivery"
_VIN_P0 = "MRDN0000002000012"
_VIN_SOON = "MRDN0000002000011"
_SER = TypeSerializer()


def _row(code: str, level: str, system: str) -> dict[str, Any]:
    return {"code": code, "level": level, "system": system, "description": "",
            "first_seen_at": "2026-11-06"}


def _stored(vin: str, component: str, dtcs: list[dict[str, Any]], fid: str) -> dict[str, Any]:
    """A Finding as the runner stores it (text in ``detail``), DynamoDB-typed."""
    f = build_fields(component, dtcs)
    item = {
        "finding_id": fid,
        "vin": vin,
        "fleet_id": _FLEET,
        "agent_id": "agent-2-vehicle-health",
        "finding_kind": f"health.{component}",
        "severity": f["severity_hint"],
        "headline": f["headline"],
        "detail": f["finding"],
        "evidence": f["evidence"],
        "expires_at": _LIVE,
    }
    return {k: _SER.serialize(v) for k, v in item.items()}


def _p0_item() -> dict[str, Any]:
    return _stored(_VIN_P0, "brakes", [_row("C1234", "P0", "brakes")], "fnd-p0")


def _soon_item() -> dict[str, Any]:
    return _stored(_VIN_SOON, "diagnostics", [_row("B1234", "P2", "body")], "fnd-soon")


def _stub_returning(*items: dict[str, Any]) -> tuple[MagicMock, list[dict[str, Any]]]:
    calls: list[dict[str, Any]] = []

    def fake_query(**kwargs):
        calls.append(kwargs)
        return {"Items": list(items)}

    ddb = MagicMock()
    ddb.query.side_effect = fake_query
    return ddb, calls


def test_fixture_is_a_real_p0_diagnostics_finding():
    """Anti-vacuity: the writer really grades the fixture p0 with the directive as text."""
    p0 = _p0_item()
    assert p0["severity"] == {"S": "p0"}
    assert is_p0_directive(p0["detail"]["S"])
    assert p0["evidence"]["L"][0]["M"]["kind"] == {"S": "cms.dtc.active"}
    soon = _soon_item()
    assert soon["severity"]["S"] in {"soon", "attention"}


def test_p0_diagnostics_finding_is_not_returned_even_if_the_query_returns_it():
    ddb, _ = _stub_returning(_p0_item(), _soon_item())

    result, errors = select_agent2_findings(
        [_FLEET], ddb_client=ddb, findings_table="vsa-staging-avx-findings", now=_NOW,
    )

    assert errors == 0
    assert _VIN_P0 not in result, "a p0 Agent 2 Finding must not reach FI fleet health"
    assert [f["finding_id"] for f in result[_VIN_SOON]] == ["fnd-soon"]


def test_p0_diagnostics_finding_adds_no_vehicle_row():
    """Through the assembler: a car whose only Agent 2 Finding is p0 gets no FI need from it."""
    ddb, _ = _stub_returning(_p0_item(), _soon_item())
    agent2_rows, _ = select_agent2_findings(
        [_FLEET], ddb_client=ddb, findings_table="vsa-staging-avx-findings", now=_NOW,
    )

    rows, counters = build_vehicle_rows(
        fault_rows=[], pm_rows=[], managed_vins=[], usage_rows=[], agent2_rows=agent2_rows,
    )

    assert [r["vin"] for r in rows] == [_VIN_SOON]
    assert counters["agent2_vin_count"] == 1
    assert all(f["severity"] != "p0" for r in rows for f in r["agent2_findings"])


def test_query_never_asks_for_p0():
    ddb, calls = _stub_returning(_soon_item())

    select_agent2_findings(
        [_FLEET, "demo-city-public"], ddb_client=ddb,
        findings_table="vsa-staging-avx-findings", now=_NOW,
    )

    assert len(calls) == 2
    for kw in calls:
        values = {v.get("S") for v in kw["ExpressionAttributeValues"].values()}
        assert "p0" not in values
        assert ":s_p0" not in kw["FilterExpression"]
        assert {"attention", "soon"} <= values


def test_severity_bands_are_exactly_soon_and_attention():
    assert _AGENT2_SEVERITY_BANDS == frozenset({"attention", "soon"})
