"""Fix Group G3-1 F5 (G3-18; review cycle 1 routes C1, W1, W2, W6, S2), at the ROUTE.

Every refusal is a 400 with NOTHING written to the actions table, each paired with a
must-succeed control that writes. Fixtures hold the caller's fleet, so the refusals can't
come from the scope check.
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from .test_fg2d_review import _PLAIN_FINDING, _claims, _event, _make_fake_actions_table
from .test_fg2d_review import h


def _approve(finding, body_params=None, claims=None, extra_body=None):
    """The route with the stateful actions-table fake; dispatch stubbed to `executed` and
    recorded, so a 200 is the route's own answer. Returns (response, stored items)."""
    table = _make_fake_actions_table()
    table.update_item = lambda **kw: {"Attributes": {"status": "executed"}}
    rec = finding.get("recommended_action") or {}
    body = {"target_system": rec.get("target_system", "none"),
            "action_kind": rec.get("action_kind", "acknowledge_only"),
            "parameters": body_params if body_params is not None else {}}
    body.update(extra_body or {})
    event = _event("POST", "/findings/{finding_id}/approve",
                   path_params={"finding_id": finding["finding_id"]},
                   claims=claims, body=body)
    dispatched = []

    def _dispatch(action, _claims):
        dispatched.append(action)
        r = MagicMock(); r.status = "executed"; r.target_ref = None; r.failure_reason = None
        return r

    with patch.object(h, "_load_finding_by_id", return_value=finding), \
         patch.object(h, "_get_ddb") as mock_ddb, \
         patch.object(h, "_dispatch_action", side_effect=_dispatch):
        mock_ddb.return_value.Table.return_value = table
        resp = h.handler(event, None)
    if not table._store:
        assert dispatched == [], "nothing stored must mean nothing dispatched"
    return resp, dict(table._store)

_FLEET = "fleet-mrdn-01"
_VIN_A = "MRDVEH0000000F5A1"
_VIN_B = "MRDVEH0000000F5B2"
_OP = _claims(sub="op-sub-f5", groups="fleet-operator", fleet_ids=_FLEET)


def _fi(kind: str, target: str, params: dict, fid: str = "FIND-f5000001-0000-0000-0000-000000000000"):
    return {**{k: v for k, v in _PLAIN_FINDING.items() if k != "recommended_action"},
            "finding_id": fid, "agent_id": "agent-fi-recall-warranty",
            "finding_kind": "fleet.recall.vehicle", "fleet_id": _FLEET,
            "scope": {"vin": _VIN_A, "fleet_id": _FLEET},
            "recommended_action": {"action_kind": kind, "target_system": target,
                                   "parameters": params}}


_BOOK = {"vehicle_vin": _VIN_A, "recall_id": "25V-F5", "dealer_id": "dealer-seattle"}
_GROUND = {"vehicle_vin": _VIN_A, "recall_id": "25V-F5"}
_CLAIM = {"vehicle_vin": _VIN_A, "ro_id": "ro-f5", "line_no": "3", "dealer_id": "dealer-seattle",
          "labor_op": "LO-1"}

_KINDS = [
    ("book_recall_service", "dms", {"vehicle_vin": _VIN_B, "recall_id": "ANY", "dealer_id": "d"}),
    ("ground_vehicle", "cms", {"vehicle_vin": _VIN_B, "recall_id": "ANY"}),
    ("submit_warranty_claim", "dms", {"vehicle_vin": _VIN_B, "ro_id": "x", "line_no": "1",
                                      "dealer_id": "d", "labor_op": "L"}),
]


def _err(resp) -> str:
    return json.loads(resp["body"]).get("error", "")


class TestPlainFindingRefused:
    """C1: a Stage C kind on a Finding with no recommended_action takes nothing from the body."""

    @pytest.mark.parametrize("kind,target,body", _KINDS, ids=[k for k, _, _ in _KINDS])
    def test_plain_finding_stage_c_kind_is_400_and_writes_nothing(self, kind, target, body):
        finding = {**_PLAIN_FINDING, "scope": {"vin": _VIN_A}}
        resp, stored = _approve(finding, body_params=body,
                                extra_body={"action_kind": kind, "target_system": target},
                                claims=_OP)
        assert resp["statusCode"] == 400, resp.get("body", "")[:200]
        assert "requires a Finding with a matching recommended_action" in _err(resp)
        assert stored == {}

    def test_control_matching_recommendation_writes(self):
        resp, stored = _approve(_fi("book_recall_service", "dms", dict(_BOOK)), body_params={},
                                claims=_OP)
        assert resp["statusCode"] == 200, resp.get("body", "")[:200]
        assert len(stored) == 1
        (item,) = stored.values()
        assert item["parameters"]["vehicle_vin"] == _VIN_A
        assert item["parameters"]["recall_id"] == "25V-F5"


class TestKindMismatchRefused:
    def test_ground_recommendation_approved_as_booking_is_400(self):
        finding = _fi("ground_vehicle", "cms", dict(_GROUND))
        resp, stored = _approve(finding, body_params={"dealer_id": "dealer-seattle"},
                                extra_body={"action_kind": "book_recall_service",
                                            "target_system": "dms"}, claims=_OP)
        assert resp["statusCode"] == 400
        assert stored == {}


class TestStageAShapedBookingRefused:
    """W1 / G3-7 corollary: a Stage A-shaped booking recommendation (no recall_id) is refused."""

    def test_rec_without_recall_id_is_400(self):
        finding = _fi("book_recall_service", "dms",
                      {"vehicle_vin": _VIN_A, "dealer_id": "LIVETEST-dealer-1"})
        resp, stored = _approve(finding, body_params={"dealer_id": "LIVETEST-dealer-1"},
                                claims=_OP)
        assert resp["statusCode"] == 400
        assert _err(resp) == "recommendation is missing parameters.recall_id"
        assert stored == {}


class TestBookingNeedsDealer:
    """W2: no rooftop after the fill -> 400 before any write (Stage A's rule)."""

    @pytest.mark.parametrize("body", [{}, {"dealer_id": None}], ids=["omitted", "null"])
    def test_rec_without_dealer_and_body_without_dealer_is_400(self, body):
        finding = _fi("book_recall_service", "dms", {"vehicle_vin": _VIN_A, "recall_id": "25V-F5"})
        resp, stored = _approve(finding, body_params=body, claims=_OP)
        assert resp["statusCode"] == 400
        assert _err(resp) == "parameters.dealer_id is required"
        assert stored == {}

    def test_control_body_supplies_dealer_is_overridden(self):
        finding = _fi("book_recall_service", "dms", {"vehicle_vin": _VIN_A, "recall_id": "25V-F5"})
        resp, stored = _approve(finding, body_params={"dealer_id": "d-x"}, claims=_OP)
        assert resp["statusCode"] == 200, resp.get("body", "")[:200]
        (item,) = stored.values()
        assert item["parameters"]["dealer_id"] == "d-x"
        assert item["decision"] == "overridden"


class TestFaultCodeAndPadding:
    """W6: the body can't add a fault_code the recommendation lacks. S2: no None padding."""

    def test_body_fault_code_when_rec_has_none_is_400(self):
        finding = _fi("submit_warranty_claim", "dms", dict(_CLAIM))
        resp, stored = _approve(finding, body_params={"fault_code": "P0001"}, claims=_OP)
        assert resp["statusCode"] == 400
        assert _err(resp) == "parameters.fault_code must match the Finding's recommended value"
        assert stored == {}

    def test_control_claim_without_fault_code_stores_exact_params(self):
        finding = _fi("submit_warranty_claim", "dms", dict(_CLAIM))
        resp, stored = _approve(finding, body_params={}, claims=_OP)
        assert resp["statusCode"] == 200, resp.get("body", "")[:200]
        (item,) = stored.values()
        assert item["parameters"] == _CLAIM, item["parameters"]
        assert item["decision"] == "approved"



class TestLiveVerifyRec10Shape:
    """W1: scripts/live-verify-fi-action-queue.py item 10 sends {dealer_id, recall_id} as both
    the recommendation and the body; the route must accept that shape (VIN from scope)."""

    def test_rec10_shape_is_accepted_and_dispatched(self):
        rec10 = {"dealer_id": "LIVETEST-dealer-r1", "recall_id": "LIVETEST-recall-r1"}
        resp, stored = _approve(_fi("book_recall_service", "dms", dict(rec10)),
                                body_params=dict(rec10), claims=_OP)
        assert resp["statusCode"] == 200, resp.get("body", "")[:200]
        (item,) = stored.values()
        assert item["parameters"] == {**rec10, "vehicle_vin": _VIN_A}


class TestGroundVehicleServerSetAtTheRoute:
    """Cycle 2 routes W1: ground_vehicle's server-set wiring, through the route (cms path)."""

    def test_body_recall_id_other_than_the_recommendation_is_400(self):
        finding = _fi("ground_vehicle", "cms", dict(_GROUND))
        resp, stored = _approve(finding, body_params={"recall_id": "OTHER-CAMPAIGN"}, claims=_OP)
        assert resp["statusCode"] == 400, resp.get("body", "")[:200]
        assert _err(resp) == "parameters.recall_id must match the Finding's recommended value"
        assert stored == {}

    def test_control_empty_body_takes_the_recommendation(self):
        finding = _fi("ground_vehicle", "cms", dict(_GROUND))
        resp, stored = _approve(finding, body_params={}, claims=_OP)
        assert resp["statusCode"] == 200, resp.get("body", "")[:200]
        (item,) = stored.values()
        assert item["parameters"] == _GROUND
        assert item["decision"] == "approved"


class TestBlankRecommendedDealer:
    """Cycle 2 routes S1: a blank dealer in the recommendation is refused after the fill."""

    def test_blank_rec_dealer_and_no_body_dealer_is_400(self):
        finding = _fi("book_recall_service", "dms", {**_BOOK, "dealer_id": "   "})
        resp, stored = _approve(finding, body_params={}, claims=_OP)
        assert resp["statusCode"] == 400
        assert _err(resp) == "parameters.dealer_id is required"
        assert stored == {}
