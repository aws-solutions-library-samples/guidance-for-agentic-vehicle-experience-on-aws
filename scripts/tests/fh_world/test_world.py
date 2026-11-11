"""test_world.py — S2 acceptance test for FhWorld.

One world per test (fresh context manager).  After open():
  - newest run record for agent-2-vehicle-health#{battery,tires,brakes}: completed, 0 errors
  - newest run record for agent-fi-fleet-health#service:                  completed, 0 errors
  - service run wrote AVX Findings with fleet_id set for both seeded fleets
  - fleet A's p0 Finding is reachable through the by_fleet GSI
"""
from __future__ import annotations

import pytest
import boto3
from boto3.dynamodb.conditions import Key

from scripts.tests.fh_world.world import FhWorld
from scripts.tests.fh_world.seed import FLEET_A, FLEET_B, CODE_P0


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _newest_run_record(table, agent_component: str) -> dict:
    """Return the run record with the latest started_at for agent_component."""
    res = table.query(
        KeyConditionExpression=Key("agent_component").eq(agent_component),
        ScanIndexForward=False,
        Limit=1,
    )
    items = res.get("Items", [])
    assert items, f"no run records for {agent_component}"
    return items[0]


def _findings_by_fleet(w: FhWorld, fleet_id: str) -> list[dict]:
    tbl = boto3.resource("dynamodb", region_name="us-west-2").Table("vsa-staging-avx-findings")
    res = tbl.query(
        IndexName="by_fleet",
        KeyConditionExpression=Key("fleet_id").eq(fleet_id),
    )
    return res.get("Items", [])


# ---------------------------------------------------------------------------
# tests
# ---------------------------------------------------------------------------

def test_agent2_run_records_completed() -> None:
    """battery, tires, brakes: newest run record is completed with 0 errors."""
    with FhWorld() as w:
        runs = boto3.resource("dynamodb", region_name="us-west-2").Table(
            "vsa-staging-avx-agent-runs")
        for comp in ("battery", "tires", "brakes"):
            rec = _newest_run_record(runs, f"agent-2-vehicle-health#{comp}")
            assert rec["status"] == "completed", (
                f"{comp} run status={rec['status']!r}; record={rec}")
            errors = int(rec.get("errors", 0))
            assert errors == 0, f"{comp} run had {errors} errors; record={rec}"


def test_service_run_record_completed() -> None:
    """service: newest run record is completed with 0 errors."""
    with FhWorld() as w:
        runs = boto3.resource("dynamodb", region_name="us-west-2").Table(
            "vsa-staging-avx-agent-runs")
        rec = _newest_run_record(runs, "agent-fi-fleet-health#service")
        assert rec["status"] == "completed", f"service status={rec['status']!r}; record={rec}"
        errors = int(rec.get("errors", 0))
        assert errors == 0, f"service run had {errors} errors; record={rec}"


def test_service_findings_have_fleet_id() -> None:
    """service wrote findings with fleet_id for both seeded fleets."""
    with FhWorld() as w:
        fleet_a_findings = _findings_by_fleet(w, FLEET_A)
        fleet_b_findings = _findings_by_fleet(w, FLEET_B)
        assert fleet_a_findings, f"no findings with fleet_id={FLEET_A!r} in avx-findings"
        assert fleet_b_findings, f"no findings with fleet_id={FLEET_B!r} in avx-findings"
        for f in fleet_a_findings + fleet_b_findings:
            assert f.get("fleet_id"), f"finding missing fleet_id: {f.get('finding_id')}"


def test_fleet_a_p0_finding_reachable_via_by_fleet_index() -> None:
    """Fleet A's p0 Finding (CODE_P0) is reachable through the by_fleet GSI."""
    with FhWorld() as w:
        findings = _findings_by_fleet(w, FLEET_A)
        assert findings, f"no findings with fleet_id={FLEET_A!r}"
        # At least one finding should reference the p0 fault code
        codes = {f.get("fault_code") or f.get("fault_codes") or "" for f in findings}
        has_p0 = any(CODE_P0 in str(c) for c in codes)
        # If not in fault_code, check the scope or recommended_action
        if not has_p0:
            texts = " ".join(str(f) for f in findings)
            has_p0 = CODE_P0 in texts
        assert has_p0, (
            f"fleet A findings do not reference {CODE_P0}; findings={findings}")
