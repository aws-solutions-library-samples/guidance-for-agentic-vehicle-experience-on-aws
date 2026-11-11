"""C1 regression: pair bookings get first-seen records so both pairs are planned.

Run WITHOUT AWS_ENDPOINT_URL (moto intercepts all boto3 calls):

    .venv/bin/python -m pytest -p no:cacheprovider -q \\
        lambdas/avx-sim-operator/tests/test_handler_pairs_moto.py

Mutants covered (in git archive HEAD copies under /tmp/fi-sim-op-f5a/):
  Q1 — record first-seen only for classified Findings again (no booking SEEN);
       both pair classes in planned_by_class must be absent and skip_counts
       must include 'too_new'. This test catches Q1 because it runs the real
       handler twice and asserts planned_by_class contains both pair classes.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import patch, MagicMock

import boto3
import pytest
from moto import mock_aws

_LAMBDA_ROOT = os.path.join(os.path.dirname(__file__), "..")
_REPO_ROOT = os.path.abspath(os.path.join(_LAMBDA_ROOT, "..", ".."))
for _p in (_LAMBDA_ROOT, os.path.dirname(_LAMBDA_ROOT), _REPO_ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

os.environ.setdefault("FI_SIM_OPERATOR_ENABLED", "true")
os.environ.setdefault("AVX_SIM_OPERATOR_TABLE", "vsa-moto-avx-sim-operator")
os.environ.setdefault("AVX_ACTIONS_TABLE", "vsa-moto-avx-actions")
os.environ.setdefault("AVX_FINDINGS_TABLE", "vsa-moto-avx-findings")
os.environ.setdefault("AVX_AGENT_RUNS_TABLE", "vsa-moto-avx-agent-runs")
os.environ.setdefault("FI_SIM_OPERATOR_PARAM", "/cvx/test/fi/sim-operator")
os.environ.setdefault("CMS_VEHICLES_TABLE", "")
os.environ.setdefault("CMS_GROUNDINGS_TABLE", "")

import handler as h  # noqa: E402

from agents.fi_sim_operator.select import FH_AGENT, RW_AGENT  # noqa: E402

_FLEET = "demo-lastmile-delivery"
_CONFIG = {"fleets": {_FLEET: {"enabled": True}}, "daily_cap": 20,
           "hold_vins": [], "hold_vehicle_ids": []}

_VIN_FH = "MRDTEST0000000FH1"
_VIN_RC = "MRDTEST0000000RC2"
_RECALL_ID = "MRD-2026-RECALL-01"
_FUTURE = "2027-01-01T00:00:00Z"


def _ts(base_iso: str, hours: float = 0.0) -> str:
    dt = datetime.fromisoformat(base_iso.replace("Z", "+00:00"))
    return (dt + timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _findings(t0: str) -> list[dict]:
    """Four findings for two pairs: FH pair and recall pair."""
    return [
        # FH grounding — classifies as fh_ground_book
        {
            "finding_id": "FIND-G1",
            "agent_id": FH_AGENT,
            "fleet_id": _FLEET,
            "scope": {"vin": _VIN_FH},
            "severity": "p0",
            "computed_at": t0,
            "expires_at": _FUTURE,
            "recommended_action": {
                "action_kind": "ground_vehicle",
                "target_system": "cms",
                "parameters": {
                    "vehicle_vin": _VIN_FH,
                    "fault_codes": ["P0A0D"],
                },
            },
            "evidence": [],
            "disclosure_txt": "grnd-disclosure",
        },
        # FH booking — p0 severity, ungrounded → classifies as p0_not_grounded (no class)
        {
            "finding_id": "FIND-B1",
            "agent_id": FH_AGENT,
            "fleet_id": _FLEET,
            "scope": {"vin": _VIN_FH},
            "severity": "p0",
            "computed_at": t0,
            "expires_at": _FUTURE,
            "recommended_action": {
                "action_kind": "book_fleet_service",
                "target_system": "dms",
                "parameters": {
                    "vehicle_vin": _VIN_FH,
                    "dealer_id": "DLR-001",
                    "needs": [{"need_id": "fault:P0A0D", "labor_op": "LBR-001"}],
                    "urgency": "this_week",
                },
            },
            "evidence": [],
            "disclosure_txt": "grnd-disclosure",
        },
        # Recall grounding — classifies as recall_ground_book
        {
            "finding_id": "FIND-RG",
            "agent_id": RW_AGENT,
            "fleet_id": _FLEET,
            "scope": {"vin": _VIN_RC},
            "severity": "p0",
            "computed_at": t0,
            "expires_at": _FUTURE,
            "recommended_action": {
                "action_kind": "ground_vehicle",
                "target_system": "cms",
                "parameters": {
                    "vehicle_vin": _VIN_RC,
                    "recall_id": _RECALL_ID,
                },
            },
            "evidence": [
                {"kind": "recall.campaign", "value": {
                    "campaign_id": _RECALL_ID,
                    "recall_class": "safety",
                }},
            ],
        },
        # Recall booking — pending grounding → classifies as paired (SKIP_PAIRED, no class)
        {
            "finding_id": "FIND-RB",
            "agent_id": RW_AGENT,
            "fleet_id": _FLEET,
            "scope": {"vin": _VIN_RC},
            "severity": "p0",
            "computed_at": t0,
            "expires_at": _FUTURE,
            "recommended_action": {
                "action_kind": "book_recall_service",
                "target_system": "dms",
                "parameters": {
                    "vehicle_vin": _VIN_RC,
                    "recall_id": _RECALL_ID,
                },
            },
            "evidence": [
                {"kind": "recall.campaign", "value": {
                    "campaign_id": _RECALL_ID,
                    "recall_class": "safety",
                }},
            ],
        },
    ]


class _MockDispatchResult:
    status = "in_progress"
    target_ref = None

    def audit_entry(self, *, by: str, at: str) -> dict:
        return {"from_status": "pending_execution", "to_status": "in_progress",
                "by": by, "at": at}


@pytest.fixture(autouse=True)
def _aws_creds(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SECURITY_TOKEN", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-west-2")


class _RunsTable:
    def __init__(self, store: list) -> None:
        self._store = store

    def put_item(self, Item: dict, ConditionExpression: str | None = None) -> dict:
        self._store.append(Item)
        return {}


class _RunsResource:
    def __init__(self) -> None:
        self.items: list[dict] = []

    def Table(self, name: str) -> _RunsTable:  # noqa: N802
        return _RunsTable(self.items)


def _create_tables(client) -> None:
    """Create the ledger and actions tables in moto."""
    for table_name, pk in [
        (os.environ["AVX_SIM_OPERATOR_TABLE"], "pk"),
        (os.environ["AVX_ACTIONS_TABLE"], "action_id"),
    ]:
        client.create_table(
            TableName=table_name,
            KeySchema=[
                {"AttributeName": pk, "KeyType": "HASH"},
                *([{"AttributeName": "sk", "KeyType": "RANGE"}]
                  if pk == "pk" else []),
            ],
            AttributeDefinitions=[
                {"AttributeName": pk, "AttributeType": "S"},
                *([{"AttributeName": "sk", "AttributeType": "S"}]
                  if pk == "pk" else []),
            ],
            BillingMode="PAY_PER_REQUEST",
        )


def _count_actions(client) -> int:
    resp = client.scan(TableName=os.environ["AVX_ACTIONS_TABLE"], Select="COUNT")
    return resp["Count"]


class TestPairsBothPlannedAfterObserve:
    """C1 regression: observe records first-seen for pair bookings; approve plans both.

    Q1 mutant guard: if first-seen is recorded only for classified (candidate) Findings,
    pair bookings have no SEEN record and the approve run skips them as too_new.
    This test fails on the Q1 mutant because planned_by_class will lack the pair classes.
    """

    # Cycle 3 (review-g11-avx-cycle3.md Critical, mutant X2): the schedule invokes `{}`
    # (approve), so the first sighting must be recorded by an approve run too, not only by
    # the by-hand observe run.
    @pytest.mark.parametrize("first_mode", ["observe", "approve"])
    def test_both_pairs_planned_at_t0_plus_21h(self, monkeypatch, first_mode):
        t0 = "2026-11-08T07:10:00Z"
        t1 = _ts(t0, hours=21.0)  # 21 h later — past the 20 h first-seen threshold

        runs = _RunsResource()
        monkeypatch.setattr(boto3, "resource", lambda *a, **k: runs)

        with mock_aws():
            ddb = boto3.client("dynamodb", region_name="us-west-2")
            _create_tables(ddb)
            # reset handler's cached client so it creates a fresh moto one
            h._ddb = None

            findings = _findings(t0)

            def _fake_server_set(finding, action_kind, body):
                params = (finding.get("recommended_action") or {}).get("parameters") or {}
                # ensure vehicle_vin is from scope
                vin = (finding.get("scope") or {}).get("vin", "")
                return {**params, "vehicle_vin": vin}, None, "approved"

            with (
                patch.object(h, "_load_ssm_config", return_value=_CONFIG),
                patch.object(h, "_scan_findings_for_fleet", return_value=findings),
                patch.object(h, "_get_decided_ids", return_value=set()),
                patch.object(h, "_read_grounded_codes", return_value=({}, 0)),
                patch("agents.executors.server_set.check_server_set_params",
                      side_effect=_fake_server_set),
                patch("agents.executors.registry.dispatch",
                      return_value=_MockDispatchResult()),
                patch.object(h, "_now_iso", return_value=t0),
                patch.object(h, "_today_utc", return_value=t0[:10]),
            ):
                # Observe at T0 — no units executed, but first-seen recorded for all 4
                obs = h.handler({"mode": first_mode}, None)

            assert obs["status"] == "ok", f"observe failed: {obs}"
            obs_fleet = obs["fleets"][_FLEET]
            # Both groundings are candidates; both bookings are unclassified but get SEEN
            assert obs_fleet.get("units_approved", 0) == 0
            assert _count_actions(ddb) == 0, "observe must write no Actions"

            # Now advance to T0+21h and approve
            with (
                patch.object(h, "_load_ssm_config", return_value=_CONFIG),
                patch.object(h, "_scan_findings_for_fleet", return_value=findings),
                patch.object(h, "_get_decided_ids", return_value=set()),
                patch.object(h, "_read_grounded_codes", return_value=({}, 0)),
                patch("agents.executors.server_set.check_server_set_params",
                      side_effect=_fake_server_set),
                patch("agents.executors.registry.dispatch",
                      return_value=_MockDispatchResult()),
                patch.object(h, "_now_iso", return_value=t1),
                patch.object(h, "_today_utc", return_value=t1[:10]),
            ):
                app = h.handler({"mode": "approve"}, None)

            assert app["status"] == "ok", f"approve failed: {app}"
            app_fleet = app["fleets"][_FLEET]
            planned = app_fleet.get("planned_by_class", {})
            assert "fh_ground_book" in planned, (
                "Q1 guard: fh_ground_book pair must be planned at T0+21h. "
                "If bookings have no SEEN record the pair skips as too_new."
            )
            assert "recall_ground_book" in planned, (
                "Q1 guard: recall_ground_book pair must be planned at T0+21h. "
                "If bookings have no SEEN record the pair skips as too_new."
            )
            # 2 pairs × 2 Actions each = 4 Actions written
            assert _count_actions(ddb) == 4, (
                f"Expected 4 Actions (2 pairs × 2); got {_count_actions(ddb)}"
            )
            # Q2 guard: in_progress (DMS success) must appear in executor_executed.
            # Mutant Q2 (in_progress not counted) returns 0 even when DMS accepted.
            # Cycle 3 W (mutant X3): both legs of both pairs are counted, exactly.
            assert app_fleet.get("executor_executed", 0) == 4, (
                "Q2/X3 guard: 4 in_progress dispatches (2 groundings + 2 bookings) must count as "
                f"executor_executed == 4; got {app_fleet.get('executor_executed')}"
            )

    def test_refused_bookings_are_counted(self, monkeypatch):
        """Cycle 3 W (X3): DMS refuses both pairs' bookings; the run counts them."""
        t0 = "2026-11-08T07:10:00Z"
        t1 = _ts(t0, hours=21.0)
        runs = _RunsResource()
        monkeypatch.setattr(boto3, "resource", lambda *a, **k: runs)

        class _Declined(_MockDispatchResult):
            status = "declined"

            def audit_entry(self, *, by: str, at: str) -> dict:
                return {"from_status": "pending_execution", "to_status": "declined", "by": by, "at": at}

        def _dispatch(action, claims, **kw):
            return _Declined() if str(action.get("action_kind", "")).startswith("book_") else _MockDispatchResult()

        def _fake_server_set(finding, action_kind, body):
            params = (finding.get("recommended_action") or {}).get("parameters") or {}
            return {**params, "vehicle_vin": (finding.get("scope") or {}).get("vin", "")}, None, "approved"

        with mock_aws():
            ddb = boto3.client("dynamodb", region_name="us-west-2")
            _create_tables(ddb)
            h._ddb = None
            findings = _findings(t0)
            for when, mode in ((t0, "approve"), (t1, "approve")):
                with (
                    patch.object(h, "_load_ssm_config", return_value=_CONFIG),
                    patch.object(h, "_scan_findings_for_fleet", return_value=findings),
                    patch.object(h, "_get_decided_ids", return_value=set()),
                    patch.object(h, "_read_grounded_codes", return_value=({}, 0)),
                    patch("agents.executors.server_set.check_server_set_params", side_effect=_fake_server_set),
                    patch("agents.executors.registry.dispatch", side_effect=_dispatch),
                    patch.object(h, "_now_iso", return_value=when),
                    patch.object(h, "_today_utc", return_value=when[:10]),
                ):
                    app = h.handler({"mode": mode}, None)
            fleet = app["fleets"][_FLEET]
            assert _count_actions(ddb) == 4
            assert fleet.get("decisions_written") == 4, fleet
            assert fleet.get("executor_executed") == 2, fleet
            assert fleet.get("executor_declined") == 2, fleet

    @pytest.mark.parametrize("first_mode", ["observe", "approve"])
    def test_booking_appearing_late_waits_one_more_run(self, monkeypatch, first_mode):
        """When a booking's first-seen is < 20 h old, its pair waits.

        Observe at T0: only grounding is in findings (no booking).
        Booking appears at T0+12h. Approve at T0+21h: booking first-seen is 9h
        old → pair still too_new.
        """
        t0 = "2026-11-08T07:10:00Z"
        t0_plus_12h = _ts(t0, hours=12.0)
        t0_plus_21h = _ts(t0, hours=21.0)

        runs = _RunsResource()
        monkeypatch.setattr(boto3, "resource", lambda *a, **k: runs)

        grounding_only = [f for f in _findings(t0) if f["finding_id"] == "FIND-G1"]
        # Recall pair (FIND-RG + FIND-RB) has different characteristics; focus on FH here.
        fh_pair = [f for f in _findings(t0)
                   if f["finding_id"] in ("FIND-G1", "FIND-B1")]

        def _fake_server_set(finding, action_kind, body):
            params = (finding.get("recommended_action") or {}).get("parameters") or {}
            vin = (finding.get("scope") or {}).get("vin", "")
            return {**params, "vehicle_vin": vin}, None, "approved"

        with mock_aws():
            ddb = boto3.client("dynamodb", region_name="us-west-2")
            _create_tables(ddb)
            h._ddb = None

            # Observe at T0 with grounding only — grounding gets SEEN, booking absent
            with (
                patch.object(h, "_load_ssm_config", return_value=_CONFIG),
                patch.object(h, "_scan_findings_for_fleet",
                             return_value=[grounding_only[0]]),
                patch.object(h, "_get_decided_ids", return_value=set()),
                patch.object(h, "_read_grounded_codes", return_value=({}, 0)),
                patch("agents.executors.server_set.check_server_set_params",
                      side_effect=_fake_server_set),
                patch("agents.executors.registry.dispatch",
                      return_value=_MockDispatchResult()),
                patch.object(h, "_now_iso", return_value=t0),
                patch.object(h, "_today_utc", return_value=t0[:10]),
            ):
                h.handler({"mode": first_mode}, None)

            # Booking appears at T0+12h — gets its first SEEN record here
            booking_late = {**fh_pair[1], "computed_at": t0_plus_12h}
            with (
                patch.object(h, "_load_ssm_config", return_value=_CONFIG),
                patch.object(h, "_scan_findings_for_fleet",
                             return_value=[fh_pair[0], booking_late]),
                patch.object(h, "_get_decided_ids", return_value=set()),
                patch.object(h, "_read_grounded_codes", return_value=({}, 0)),
                patch("agents.executors.server_set.check_server_set_params",
                      side_effect=_fake_server_set),
                patch("agents.executors.registry.dispatch",
                      return_value=_MockDispatchResult()),
                patch.object(h, "_now_iso", return_value=t0_plus_12h),
                patch.object(h, "_today_utc", return_value=t0_plus_12h[:10]),
            ):
                h.handler({"mode": first_mode}, None)

            # Approve at T0+21h: grounding first-seen is 21h old (OK) but booking
            # first-seen is only 9h old (<20h) → pair is still too_new
            with (
                patch.object(h, "_load_ssm_config", return_value=_CONFIG),
                patch.object(h, "_scan_findings_for_fleet",
                             return_value=[fh_pair[0], booking_late]),
                patch.object(h, "_get_decided_ids", return_value=set()),
                patch.object(h, "_read_grounded_codes", return_value=({}, 0)),
                patch("agents.executors.server_set.check_server_set_params",
                      side_effect=_fake_server_set),
                patch("agents.executors.registry.dispatch",
                      return_value=_MockDispatchResult()),
                patch.object(h, "_now_iso", return_value=t0_plus_21h),
                patch.object(h, "_today_utc", return_value=t0_plus_21h[:10]),
            ):
                app = h.handler({"mode": "approve"}, None)

            app_fleet = app["fleets"][_FLEET]
            planned = app_fleet.get("planned_by_class", {})
            assert "fh_ground_book" not in planned, (
                "Booking first-seen is only 9h old at T0+21h; pair must wait one more run."
            )
            assert _count_actions(ddb) == 0, (
                "No Actions should be written when the pair is still too_new."
            )
