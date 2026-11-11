"""Fix Group G3-1 F6 (review cycle 1 routes C2, W5): the two Stage C routes' wire contract.

- Authorization is EXACTLY the operator's token (a service identity fails), on the CMS
  grounding route AND the DMS warranty-claim route (mutants Ma, Mb).
- The JSON body is EXACTLY the contract's key set and values: CMS G3-5
  {vin, campaignId, findingId, actionId}; DMS fleet-warranty.md § 3.2 {vehicle_vin, ro_id,
  line_no, dealer_id, labor_op, avx_action_id, + fault_code/notes when given} (Mc, Md).
- A required field missing -> terminal error and NO HTTP call (controls send exactly one).
"""
from __future__ import annotations

import json

import pytest
import responses as rsps  # type: ignore[import]
from unittest.mock import patch

from agents.executors.base import ActionStatus
from agents.executors.tests.test_stage_c_kinds import (
    _ACTION_UUID, _CAMPAIGN_ID, _CMS_ENDPOINT, _DMS_BASE, _DMS_PREFIX, _LINE_NO,
    _OPERATOR_CLAIMS, _RO_ID, _VIN, _make_cms_executor, _make_dms_executor,
)

_ACTION_ID = f"AVX-ACT-{_ACTION_UUID}"
_FINDING_ID = "FIND-f6000001-0000-0000-0000-000000000000"
_GROUNDINGS = f"{_CMS_ENDPOINT}/api/v1/fleet-intelligence/groundings"
_CLAIMS_URL = f"{_DMS_PREFIX}/warranty-claims"
_OPERATOR_AUTH = f"Bearer {_OPERATOR_CLAIMS['token']}"

_CLAIM_PARAMS = {
    "vehicle_vin": _VIN, "ro_id": _RO_ID, "line_no": _LINE_NO, "dealer_id": "dealer-seattle",
    "labor_op": "LO-EV-BRAKE-001", "fault_code": "C0035", "notes": "Brake caliper replaced.",
}


def _ground(params):
    return {"action_id": _ACTION_ID, "finding_id": _FINDING_ID, "action_kind": "ground_vehicle",
            "target_system": "cms", "parameters": params}


def _claim(params):
    return {"action_id": _ACTION_ID, "finding_id": _FINDING_ID,
            "action_kind": "submit_warranty_claim", "target_system": "dms", "parameters": params}


def _dispatch_ground(params):
    rsps.add(rsps.POST, _GROUNDINGS, json={}, status=201)
    with patch.dict("os.environ", {"VSA_CMS_API_ENDPOINT": _CMS_ENDPOINT}):
        return _make_cms_executor().dispatch(_ground(params), _OPERATOR_CLAIMS)


def _dispatch_claim(params):
    rsps.add(rsps.POST, _CLAIMS_URL, json={"claim_id": "clm-f6", "status": "Submitted"}, status=201)
    with patch.dict("os.environ", {"VSA_DMS_API_ENDPOINT": _DMS_BASE}):
        return _make_dms_executor().dispatch(_claim(params), _OPERATOR_CLAIMS)


class TestGroundingWire:
    @rsps.activate
    def test_exact_authorization_and_body(self):
        result = _dispatch_ground({"vehicle_vin": _VIN, "recall_id": _CAMPAIGN_ID})
        assert result.status == ActionStatus.EXECUTED
        assert len(rsps.calls) == 1
        req = rsps.calls[0].request
        assert req.headers["Authorization"] == _OPERATOR_AUTH
        assert json.loads(req.body) == {"vin": _VIN, "campaignId": _CAMPAIGN_ID,
                                        "findingId": _FINDING_ID, "actionId": _ACTION_ID}

    @rsps.activate
    @pytest.mark.parametrize("params", [{"recall_id": _CAMPAIGN_ID}, {"vehicle_vin": _VIN}],
                             ids=["no-vin", "no-recall-id"])
    def test_missing_required_field_sends_nothing(self, params):
        result = _dispatch_ground(params)
        assert result.status == ActionStatus.FAILED
        assert len(rsps.calls) == 0


class TestWarrantyClaimWire:
    @rsps.activate
    def test_exact_authorization_and_body(self):
        result = _dispatch_claim(dict(_CLAIM_PARAMS))
        assert result.status == ActionStatus.EXECUTED
        assert len(rsps.calls) == 1
        req = rsps.calls[0].request
        assert req.headers["Authorization"] == _OPERATOR_AUTH
        assert json.loads(req.body) == {**_CLAIM_PARAMS, "avx_action_id": _ACTION_UUID}

    @rsps.activate
    def test_optional_fields_absent_are_not_sent(self):
        params = {k: v for k, v in _CLAIM_PARAMS.items() if k not in ("fault_code", "notes")}
        _dispatch_claim(params)
        assert json.loads(rsps.calls[0].request.body) == {**params, "avx_action_id": _ACTION_UUID}

    @rsps.activate
    @pytest.mark.parametrize("missing", ["vehicle_vin", "ro_id", "line_no", "dealer_id", "labor_op"])
    def test_missing_required_field_is_terminal_with_no_http_call(self, missing):
        params = {k: v for k, v in _CLAIM_PARAMS.items() if k != missing}
        result = _dispatch_claim(params)
        assert result.status == ActionStatus.FAILED
        assert result.last_error_class == "MissingDispatchParameter"
        assert len(rsps.calls) == 0


_NO_TOKEN_CLAIMS = {k: v for k, v in _OPERATOR_CLAIMS.items() if k not in ("token", "id_token")}


class TestNoTokenSendsNothing:
    """Cycle 2 routes W2: no operator token -> terminal MissingJwt, nothing sent (never a
    service identity), on both Stage C routes."""

    @rsps.activate
    def test_grounding_without_token(self):
        rsps.add(rsps.POST, _GROUNDINGS, json={}, status=201)
        with patch.dict("os.environ", {"VSA_CMS_API_ENDPOINT": _CMS_ENDPOINT}):
            result = _make_cms_executor().dispatch(
                _ground({"vehicle_vin": _VIN, "recall_id": _CAMPAIGN_ID}), _NO_TOKEN_CLAIMS)
        assert result.status == ActionStatus.FAILED
        assert result.last_error_class == "MissingJwt"
        assert len(rsps.calls) == 0

    @rsps.activate
    def test_warranty_claim_without_token(self):
        rsps.add(rsps.POST, _CLAIMS_URL, json={"claim_id": "c"}, status=201)
        with patch.dict("os.environ", {"VSA_DMS_API_ENDPOINT": _DMS_BASE}):
            result = _make_dms_executor().dispatch(_claim(dict(_CLAIM_PARAMS)), _NO_TOKEN_CLAIMS)
        assert result.status == ActionStatus.FAILED
        assert result.last_error_class == "MissingJwt"
        assert len(rsps.calls) == 0
