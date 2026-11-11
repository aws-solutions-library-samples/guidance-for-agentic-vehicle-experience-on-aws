"""M12-approve-scope-bypass-stage-c: _check_approve_scope gates stage-C action kinds.

Control: fleet-B operator approves fleet-B finding → 200, dispatch called once.
Test:    fleet-A operator (CSV custom:fleetIds) approves fleet-B finding → 403,
         dispatch never called, put_item / update_item never called.
"""
import json
import os
import sys
import pytest
from unittest.mock import MagicMock, patch

_LAMBDA_ROOT = os.path.join(os.path.dirname(__file__), "..")
_LAMBDAS_ROOT = os.path.dirname(_LAMBDA_ROOT)
_REPO_ROOT = os.path.dirname(_LAMBDAS_ROOT)
for _p in (_LAMBDA_ROOT, _LAMBDAS_ROOT, _REPO_ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

os.environ.setdefault("AVX_STAGE", "test")
os.environ.setdefault("AVX_FINDINGS_TABLE", "vsa-test-avx-findings")
os.environ.setdefault("AVX_ACTIONS_TABLE", "vsa-test-avx-actions")
os.environ.setdefault("AVX_FLEET_IDS_CLAIM_TRUSTED", "true")

import handler as h  # noqa: E402

_FLEET_B = "fleet-meridian-01"
_FLEET_A = "fleet-other-99"
_VIN = "MRDVEH00000000021"

_OP_B = {"sub": "op-b-001", "cognito:groups": ["fleet-operator"], "custom:fleetIds": _FLEET_B}
_OP_A = {"sub": "op-a-002", "cognito:groups": ["fleet-operator"], "custom:fleetIds": _FLEET_A}

_CASES = [
    ("ground_vehicle",       "cms", {"vehicle_vin": _VIN, "recall_id": "MRD-2024-001"},
     {"finding_kind": "fleet.recall.vehicle", "recommended_action": {
         "action_kind": "ground_vehicle", "target_system": "cms",
         "parameters": {"vehicle_vin": _VIN, "recall_id": "MRD-2024-001"}}}),
    ("book_recall_service",  "dms", {},
     {"finding_kind": "fleet.recall.vehicle", "recommended_action": {
         "action_kind": "book_recall_service", "target_system": "dms",
         "parameters": {"vehicle_vin": _VIN, "recall_id": "MRD-2024-001", "dealer_id": "d-sea"}}}),
    ("submit_warranty_claim","dms", {},
     {"finding_kind": "fleet.warranty.claim_candidate", "recommended_action": {
         "action_kind": "submit_warranty_claim", "target_system": "dms",
         "parameters": {"vehicle_vin": _VIN, "ro_id": "ro-1", "line_no": "1",
                        "dealer_id": "d-sea", "labor_op": "L1", "fault_code": "C0035",
                        "notes": "note."}}}),
]


def _finding(kind_extras: dict) -> dict:
    return {"finding_id": "AVX-FND-scope-sc-01", "agent_id": "agent-fi-rw",
            "severity": "p0", "fleet_id": _FLEET_B,
            "scope": {"vin": _VIN, "fleet_id": _FLEET_B}, **kind_extras}


def _mock_ddb():
    t = MagicMock()
    t.put_item.return_value = {}
    t.meta.client.exceptions.ConditionalCheckFailedException = Exception
    t.update_item.return_value = {"Attributes": {"action_id": "a1", "status": "executed"}}
    m = MagicMock()
    m.Table.return_value = t
    return m, t


def _event(action_kind, target_system, body_params, claims):
    return {
        "resource": "/findings/{finding_id}/approve",
        "httpMethod": "POST",
        "pathParameters": {"finding_id": "AVX-FND-scope-sc-01"},
        "requestContext": {"authorizer": {"claims": claims}},
        "body": json.dumps({"action_kind": action_kind, "target_system": target_system,
                            "parameters": body_params}),
    }


@pytest.mark.parametrize("action_kind,target,body,extras", _CASES)
def test_fleet_b_operator_gets_200(action_kind, target, body, extras):
    mock_ddb, _ = _mock_ddb()
    finding = _finding(extras)
    captured = []
    def cap(action, claims):
        captured.append(action); r = MagicMock(); r.status = "executed"; r.target_ref = None; return r
    with patch.object(h, "_load_finding_by_id", return_value=finding), \
         patch.object(h, "_get_ddb", return_value=mock_ddb), \
         patch.object(h, "_dispatch_action", side_effect=cap):
        resp = h.handler(_event(action_kind, target, body, _OP_B), None)
    assert resp["statusCode"] == 200, f"{action_kind}: expected 200, got {resp['statusCode']}: {resp.get('body','')[:200]}"
    assert len(captured) == 1, f"{action_kind}: dispatch must be called once"


@pytest.mark.parametrize("action_kind,target,body,extras", _CASES)
def test_fleet_a_operator_gets_403(action_kind, target, body, extras):
    mock_ddb, mock_table = _mock_ddb()
    finding = _finding(extras)
    with patch.object(h, "_load_finding_by_id", return_value=finding), \
         patch.object(h, "_get_ddb", return_value=mock_ddb), \
         patch.object(h, "_dispatch_action") as dispatch:
        resp = h.handler(_event(action_kind, target, body, _OP_A), None)
    assert resp["statusCode"] == 403, f"{action_kind}: expected 403, got {resp['statusCode']}: {resp.get('body','')[:200]}"
    dispatch.assert_not_called()
    mock_table.put_item.assert_not_called()
    mock_table.update_item.assert_not_called()
