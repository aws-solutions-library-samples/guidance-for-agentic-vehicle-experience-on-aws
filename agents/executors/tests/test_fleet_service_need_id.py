"""A ``book_fleet_service`` booking sends each need's ``need_id`` to DMS (FI stage D task 9.1).

DMS's dealer stand-in recognises a fleet-health booking only by a line carrying
``need_id`` (DMS ``source/simulation/fi_stand_ins.py`` ``is_fi_fleet_health_booking``,
FI stage D task 9.2). If the executor stopped sending it, every fleet-health booking would
stay at ``Draft`` with no error on either side. The need ids are the deterministic
``fault:<code>``, ``pm:<task_code>`` and ``a2:<finding_kind>`` from ``fi_fleet_health/bands.py``.

Run:
    .venv/bin/python -m pytest agents/executors/tests/test_fleet_service_need_id.py -q
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import responses as rsps  # type: ignore[import]

_DMS_BASE = "http://dms-staging-g9.test"
_ACTION_UUID = "3f6b0c2e-8d41-4f7a-9b55-0e2c7d9a1f30"
_NEEDS = [
    {"need_id": "fault:P0AA6", "labor_op": "HV_ISOLATION_REPAIR", "description": "HV isolation"},
    {"need_id": "pm:tire_rotation", "labor_op": "TIRE_ROTATION"},
    {"need_id": "a2:health.battery", "labor_op": "HV_BATTERY_DIAGNOSTIC"},
]
_OPERATOR_CLAIMS = {
    "sub": "op-sub-g9-0001",
    "cognito:groups": ["fleet-operator"],
    "custom:fleetIds": "fleet-meridian-01",
    "token": "Bearer.fake.g9.jwt",
}


def _executor():
    from agents.executors.base import RetryPolicy
    from agents.executors.dms import DmsExecutor

    return DmsExecutor(
        enqueue_ios_owner=MagicMock(),
        events_client=MagicMock(),
        cloudwatch_client=MagicMock(),
        ssm_client=MagicMock(),
        sleep_fn=lambda _: None,
        retry_policy=RetryPolicy(retry_count=2, backoff_base_ms=0, backoff_cap_ms=0),
    )


@rsps.activate
def test_every_sent_line_carries_its_needs_need_id_in_order():
    rsps.add(rsps.POST, f"{_DMS_BASE}/api/dms/fleet/repair-orders",
             json={"ro_id": "ro-g9-0001"}, status=201)
    action = {
        "action_id": f"AVX-ACT-{_ACTION_UUID}",
        "action_kind": "book_fleet_service",
        "target_system": "dms",
        "parameters": {"vehicle_vin": "MRDCW01H8PC00V7RD", "dealer_id": "dealer-seattle",
                       "urgency": "this_week", "needs": [dict(n) for n in _NEEDS]},
    }
    with patch.dict("os.environ", {"VSA_DMS_API_ENDPOINT": _DMS_BASE}):
        _executor().dispatch(action, _OPERATOR_CLAIMS)

    assert len(rsps.calls) == 1, [c.request.url for c in rsps.calls]
    body = json.loads(rsps.calls[0].request.body)
    assert body.get("avx_action_id") == _ACTION_UUID
    assert [ln.get("need_id") for ln in body.get("lines", [])] == [n["need_id"] for n in _NEEDS]
