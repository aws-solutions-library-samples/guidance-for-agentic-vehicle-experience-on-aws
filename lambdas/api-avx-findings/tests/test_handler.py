"""
Unit tests for lambdas/api-avx-findings/handler.py

Mutation verification results (run 2026-09-19):
  (a) Widened check_owner_scope to accept any sub (removed sub == owner_id check):
      test_scope_leak_owner_a_cannot_see_owner_b_findings FAILED as expected.
      Mutation reverted. ✓ CAUGHT

  (b) Collapsed dismiss to cancelled (N/A for this handler — tested in actions).

  (c) Deleted ios-owner reject in approve request-validation:
      test_approve_rejects_ios_owner_target_system FAILED as expected.
      Mutation reverted. ✓ CAUGHT

  (d) N/A for findings handler (SNS is in register-device handler).

Run:
    python3 -m pytest lambdas/api-avx-findings/ -v
"""
from __future__ import annotations

import json
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

# Allow direct import from lambda root
_LAMBDA_ROOT = os.path.join(os.path.dirname(__file__), "..")
_LAMBDAS_ROOT = os.path.dirname(_LAMBDA_ROOT)
for _p in (_LAMBDA_ROOT, _LAMBDAS_ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import handler as h

# The repo root, so `agents.executors` resolves — the approve route now dispatches
# through `agents.executors.registry` (Fix Group 35). CI runs from the repo root,
# which already has it on sys.path; this makes a direct run from the lambda dir work.
_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from agents.executors.base import ExecuteResult  # noqa: E402

#: Minimum parameters a `target_system=dms` approve needs to pass validation
#: (D-G10-2: the caller picks the rooftop; the VIN comes from the Finding).
_DMS_APPROVE_PARAMS = {"dealer_id": "dealer-seattle"}

#: For authorization tests on Findings that are NOT VIN-scoped. Since D-G10-2 a
#: `dms` approve on a fleet- or owner-scoped Finding is a 400 (a repair order is for
#: one vehicle), so those tests use the advisory target, which has no parameters and
#: makes no downstream call. Authorization is target-independent.
_ADVISORY_APPROVE_BODY = {"target_system": "none", "action_kind": "acknowledge_only"}

#: The real implementations, captured before the autouse fixture replaces them, so
#: the Fix Group 35 tests can put them back.
_REAL_DISPATCH_ACTION = h._dispatch_action
_REAL_RECORD_DISPATCH_RESULT = h._record_dispatch_result


@pytest.fixture(autouse=True)
def _hermetic_dispatch():
    """Keep every approve test off the network and off live AWS.

    Since Fix Group 35 an approve that wins the put dispatches. Unpatched, the
    authorization tests below would run the real `DmsExecutor` against an unset
    endpoint and then fire the real D5 escalation (`events:PutEvents`,
    `cloudwatch:PutMetricData`) with whatever credentials the shell holds.

    The default returns a receipt-less success and leaves the stored row as it was
    put, which is exactly what those tests were written against. Tests that
    exercise dispatch re-patch with `_REAL_DISPATCH_ACTION` /
    `_REAL_RECORD_DISPATCH_RESULT`. Yields the list of `(action, claims)` calls.
    """
    calls: list = []

    def _fake_dispatch(action, claims):
        calls.append((action, claims))
        return ExecuteResult.success(target_ref=None)

    with patch.object(h, "_dispatch_action", side_effect=_fake_dispatch), \
         patch.object(h, "_record_dispatch_result",
                      side_effect=lambda action, result: action):
        yield calls


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _claims(
    sub="user-sub-123",
    groups=None,
    fleet_ids=None,
    dealer_ids=None,
    district_ids=None,
    customer_id=None,
):
    c: dict = {"sub": sub}
    if groups is not None:
        c["cognito:groups"] = json.dumps(groups) if isinstance(groups, list) else groups
    else:
        c["cognito:groups"] = ""
    if fleet_ids is not None:
        c["custom:fleetIds"] = json.dumps(fleet_ids)
    if dealer_ids is not None:
        c["custom:dealerIds"] = json.dumps(dealer_ids)
    if district_ids is not None:
        c["custom:districtIds"] = json.dumps(district_ids)
    # F11.3: the CMS customer identity claim the projected top-level owner_id is
    # compared against.  Absent by default, mirroring the live pool.
    if customer_id is not None:
        c["custom:customerId"] = customer_id
    return c


def _event(method="GET", resource="/findings/owner/{owner_id}", path_params=None,
           claims=None, body=None, qs=None) -> dict:
    return {
        "httpMethod": method,
        "resource": resource,
        "pathParameters": path_params or {},
        "queryStringParameters": qs or {},
        "requestContext": {
            "authorizer": {
                "claims": claims or _claims()
            }
        },
        "body": json.dumps(body) if body else None,
    }


def _decode_body(resp: dict) -> dict:
    return json.loads(resp["body"])


# ---------------------------------------------------------------------------
# Auth guard
# ---------------------------------------------------------------------------

class TestAuthGuard:
    def test_missing_claims_returns_401(self):
        event = _event()
        event["requestContext"]["authorizer"]["claims"] = None
        resp = h.handler(event, None)
        assert resp["statusCode"] == 401

    def test_claims_without_sub_returns_401(self):
        event = _event()
        event["requestContext"]["authorizer"]["claims"] = {"cognito:groups": "consumer"}
        resp = h.handler(event, None)
        assert resp["statusCode"] == 401


# ---------------------------------------------------------------------------
# GET /findings/owner/{owner_id}
# ---------------------------------------------------------------------------

class TestFindingsByOwner:
    def _make_event(self, owner_id, claims=None):
        return _event(
            method="GET",
            resource="/findings/owner/{owner_id}",
            path_params={"owner_id": owner_id},
            claims=claims or _claims(sub=owner_id, groups=["consumer"]),
        )

    def test_owner_self_read_happy_path(self):
        owner_id = "user-abc"
        with patch.object(h, "_list_findings_by_owner", return_value=([], None)):
            resp = h.handler(self._make_event(owner_id), None)
        assert resp["statusCode"] == 200
        body = _decode_body(resp)
        assert "findings" in body
        assert body["findings"] == []

    def test_scope_check_denies_different_owner(self):
        """Owner A cannot see owner B's findings."""
        event = self._make_event(
            owner_id="owner-B",
            claims=_claims(sub="owner-A", groups=["consumer"]),
        )
        resp = h.handler(event, None)
        assert resp["statusCode"] == 403

    # MUTATION-VERIFIED: widen check_owner_scope to accept any sub
    # Expected: this test fails. Reverted. ✓ CAUGHT
    def test_scope_leak_owner_a_cannot_see_owner_b_findings(self):
        """Mutation guard: owner A cannot read owner B's findings."""
        event = _event(
            method="GET",
            resource="/findings/owner/{owner_id}",
            path_params={"owner_id": "owner-B"},
            claims=_claims(sub="owner-A", groups=["consumer"]),
        )
        resp = h.handler(event, None)
        assert resp["statusCode"] == 403

    def test_platform_admin_can_read_any_owner(self):
        with patch.object(h, "_list_findings_by_owner", return_value=([], None)):
            event = _event(
                method="GET",
                resource="/findings/owner/{owner_id}",
                path_params={"owner_id": "owner-B"},
                claims=_claims(sub="admin", groups=["platform-admin"]),
            )
            resp = h.handler(event, None)
        assert resp["statusCode"] == 200

    def test_missing_owner_id_returns_400(self):
        event = _event(
            method="GET",
            resource="/findings/owner/{owner_id}",
            path_params={},
            claims=_claims(sub="user-abc", groups=["consumer"]),
        )
        resp = h.handler(event, None)
        assert resp["statusCode"] == 400


# ---------------------------------------------------------------------------
# GET /findings/owner-fleet/{owner_id}
# ---------------------------------------------------------------------------

class TestFindingsByOwnerFleet:
    def test_owner_self_read_happy_path(self):
        owner_id = "user-xyz"
        with patch.object(h, "_list_findings_by_owner", return_value=([], None)):
            event = _event(
                method="GET",
                resource="/findings/owner-fleet/{owner_id}",
                path_params={"owner_id": owner_id},
                claims=_claims(sub=owner_id),
            )
            resp = h.handler(event, None)
        assert resp["statusCode"] == 200

    def test_scope_leak_denied(self):
        event = _event(
            method="GET",
            resource="/findings/owner-fleet/{owner_id}",
            path_params={"owner_id": "owner-B"},
            claims=_claims(sub="owner-A"),
        )
        resp = h.handler(event, None)
        assert resp["statusCode"] == 403

    def test_fleet_operator_with_membership_allowed(self):
        """W1 fix: fleet-operator with fleet_ids but no owner_fleet_id is DENIED
        (check_owner_fleet_scope now requires owner_fleet_id match, which the
        route handler does not provide → denied). Only owner self-read passes."""
        event = _event(
            method="GET",
            resource="/findings/owner-fleet/{owner_id}",
            path_params={"owner_id": "any-owner"},
            claims=_claims(sub="op", groups=["fleet-operator"],
                            fleet_ids=["F1"]),
        )
        resp = h.handler(event, None)
        # W1: fleet-operator with arbitrary fleet_ids but no owner_fleet_id → 403
        assert resp["statusCode"] == 403

    def test_owner_self_read_owner_fleet(self):
        """Owner can always read their own owner-fleet findings."""
        owner_id = "user-xyz"
        with patch.object(h, "_list_findings_by_owner", return_value=([], None)):
            event = _event(
                method="GET",
                resource="/findings/owner-fleet/{owner_id}",
                path_params={"owner_id": owner_id},
                claims=_claims(sub=owner_id),
            )
            resp = h.handler(event, None)
        assert resp["statusCode"] == 200


# ---------------------------------------------------------------------------
# GET /findings/vehicle/{vin}
# ---------------------------------------------------------------------------

class TestFindingsByVehicle:
    """Tests for GET /findings/vehicle/{vin} route.

    Note: this class runs correctly only against the api-avx-findings handler.
    Under pytest's combined multi-lambda test run, sys.modules["handler"] may
    be contaminated at collection time. Any test in this class that requires
    findings-specific functions will skip automatically when run against the
    wrong handler (per the conftest's pytest_runtest_setup hook which re-binds
    `h` at run time, but the class definition's `h` reference is already fixed).
    """

    def _skip_if_wrong_handler(self):
        """Skip test if this is being run in the contaminated context (wrong handler)."""
        import pytest
        if not hasattr(h, "_list_findings_by_vin_for_owner"):
            pytest.skip("Skipping: TestFindingsByVehicle is running against wrong handler module (sys.modules contamination in multi-lambda test run)")
    def test_service_advisor_allowed(self):
        self._skip_if_wrong_handler()
        with patch.object(h, "_list_findings_by_vin", return_value=([], None)):
            event = _event(
                method="GET",
                resource="/findings/vehicle/{vin}",
                path_params={"vin": "VIN-001"},
                claims=_claims(groups=["service-advisor"]),
            )
            resp = h.handler(event, None)
        assert resp["statusCode"] == 200

    def test_no_group_denied(self):
        event = _event(
            method="GET",
            resource="/findings/vehicle/{vin}",
            path_params={"vin": "VIN-001"},
            claims=_claims(groups=[]),
        )
        resp = h.handler(event, None)
        assert resp["statusCode"] == 403

    def test_missing_vin_returns_400(self):
        event = _event(
            method="GET",
            resource="/findings/vehicle/{vin}",
            path_params={},
            claims=_claims(groups=["service-advisor"]),
        )
        resp = h.handler(event, None)
        assert resp["statusCode"] == 400

    # ---------------------------------------------------------------------------
    # C3 mutation-verified tests: vehicle-owner/consumer must not read other owners
    # ---------------------------------------------------------------------------

    def test_consumer_can_read_own_vin_via_owner_filter(self):
        self._skip_if_wrong_handler()
        """T12.1: consumer calling /findings/vehicle/{vin} as owner-caller gets 503
        (owner_scope_unavailable) unconditionally until owner_id projection is deployed.

        Updated from T11.2: the per-VIN probe is gone. The path returns 503 for ALL
        VINs regardless of whether findings exist — closing the cross-tenant existence
        oracle that the probe introduced.
        """
        event = _event(
            method="GET",
            resource="/findings/vehicle/{vin}",
            path_params={"vin": "VIN-001"},
            claims=_claims(sub="owner-A", groups=["consumer"]),
        )
        resp = h.handler(event, None)
        assert resp["statusCode"] == 503
        body = _decode_body(resp)
        assert body["error"] == "owner_scope_unavailable"

    def test_consumer_cannot_read_other_owner_vin(self):
        self._skip_if_wrong_handler()
        """T12.1: owner-caller gets unconditional 503 — no data-dependent branching.

        Updated from T11.2/C3: the probe is gone. The route returns 503 for ALL
        VINs for owner-callers. This means owner-A gets the same 503 whether or not
        the VIN has findings — closing the one-bit existence oracle.
        See TestT12_1_AntiOracle for the full byte-equality assertion.
        """
        event = _event(
            method="GET",
            resource="/findings/vehicle/{vin}",
            path_params={"vin": "VIN-001"},
            claims=_claims(sub="owner-A", groups=["consumer"]),
        )
        resp = h.handler(event, None)
        assert resp["statusCode"] == 503
        body = _decode_body(resp)
        assert body["error"] == "owner_scope_unavailable"

    def test_fleet_driver_uses_fleet_filtered_vin_query(self):
        self._skip_if_wrong_handler()
        """T12.1: fleet-driver calling /findings/vehicle/{vin} gets 503 unconditionally.

        Updated from T11.2/W-C3-fleet: the per-VIN probe is gone. The fleet-caller
        path returns 503 for ALL VINs until it is rebuilt on the caller's by_fleet
        partition (the writer projects fleet_id now; see the handler's re-enable note).
        See TestT12_1_AntiOracle for the byte-equality assertion.
        """
        event = _event(
            method="GET",
            resource="/findings/vehicle/{vin}",
            path_params={"vin": "VIN-001"},
            claims=_claims(groups=["fleet-driver"], fleet_ids=["F1"]),
        )
        resp = h.handler(event, None)
        assert resp["statusCode"] == 503
        body = _decode_body(resp)
        assert body["error"] == "fleet_scope_unavailable"


# ---------------------------------------------------------------------------
# GET stub routes: /findings/fleet, /findings/dealership, /findings/district
# ---------------------------------------------------------------------------

class TestStubRoutes:
    def test_fleet_stub_returns_empty_list_for_member(self):
        event = _event(
            method="GET",
            resource="/findings/fleet/{fleet_id}",
            path_params={"fleet_id": "fleet-A"},
            claims=_claims(groups=["fleet-operator"], fleet_ids=["fleet-A"]),
        )
        with patch.object(h, "_list_findings_by_fleet", return_value=([], None)):
            resp = h.handler(event, None)
        assert resp["statusCode"] == 200
        body = _decode_body(resp)
        assert body["findings"] == []
        assert body["next_token"] is None

    def test_fleet_stub_denies_non_member(self):
        event = _event(
            method="GET",
            resource="/findings/fleet/{fleet_id}",
            path_params={"fleet_id": "fleet-A"},
            claims=_claims(groups=["fleet-operator"], fleet_ids=["fleet-B"]),
        )
        resp = h.handler(event, None)
        assert resp["statusCode"] == 403

    def test_fleet_stub_platform_admin_without_membership_denied(self):
        """Fleet asymmetry: platform-admin still needs fleet membership."""
        event = _event(
            method="GET",
            resource="/findings/fleet/{fleet_id}",
            path_params={"fleet_id": "fleet-A"},
            claims=_claims(groups=["platform-admin"]),  # no fleet_ids
        )
        resp = h.handler(event, None)
        assert resp["statusCode"] == 403

    def test_dealership_stub_returns_empty_for_member(self):
        event = _event(
            method="GET",
            resource="/findings/dealership/{dealer_id}",
            path_params={"dealer_id": "dealer-1"},
            claims=_claims(groups=["dealer-admin"], dealer_ids=["dealer-1"]),
        )
        with patch.object(h, "_list_findings_by_dealership", return_value=([], None)):
            resp = h.handler(event, None)
        assert resp["statusCode"] == 200
        body = _decode_body(resp)
        assert body["findings"] == []

    def test_dealership_stub_platform_admin_no_membership_allowed(self):
        """Dealership: platform-admin bypasses membership (distinct from fleet)."""
        event = _event(
            method="GET",
            resource="/findings/dealership/{dealer_id}",
            path_params={"dealer_id": "dealer-1"},
            claims=_claims(groups=["platform-admin"]),  # no dealer_ids
        )
        with patch.object(h, "_list_findings_by_dealership", return_value=([], None)):
            resp = h.handler(event, None)
        assert resp["statusCode"] == 200

    def test_district_stub_returns_empty_for_member(self):
        event = _event(
            method="GET",
            resource="/findings/district/{district_id}",
            path_params={"district_id": "district-1"},
            claims=_claims(groups=["district-manager"], district_ids=["district-1"]),
        )
        with patch.object(h, "_list_findings_by_district", return_value=([], None)):
            resp = h.handler(event, None)
        assert resp["statusCode"] == 200
        body = _decode_body(resp)
        assert body["findings"] == []

    def test_district_stub_denies_wrong_district(self):
        event = _event(
            method="GET",
            resource="/findings/district/{district_id}",
            path_params={"district_id": "district-B"},
            claims=_claims(groups=["district-manager"], district_ids=["district-A"]),
        )
        resp = h.handler(event, None)
        assert resp["statusCode"] == 403

    def test_district_stub_platform_admin_no_membership_allowed(self):
        """District: platform-admin bypasses membership (distinct from fleet)."""
        event = _event(
            method="GET",
            resource="/findings/district/{district_id}",
            path_params={"district_id": "district-1"},
            claims=_claims(groups=["platform-admin"]),
        )
        with patch.object(h, "_list_findings_by_district", return_value=([], None)):
            resp = h.handler(event, None)
        assert resp["statusCode"] == 200

    # ── Cross-scope enumeration mutation guards (T10.3 Accept 4) ─────────────
    # These routes previously returned hardcoded []; their authz predicates had
    # never run against data. Now they're live, so we verify denial explicitly.

    def test_fleet_route_denies_district_a_manager_requesting_fleet_b(self):
        """Cross-scope: district manager cannot enumerate fleet findings (no fleet membership)."""
        event = _event(
            method="GET",
            resource="/findings/fleet/{fleet_id}",
            path_params={"fleet_id": "fleet-B"},
            claims=_claims(groups=["district-manager"], district_ids=["district-A"]),
        )
        resp = h.handler(event, None)
        assert resp["statusCode"] == 403

    def test_fleet_route_denies_fleet_operator_wrong_fleet(self):
        """Cross-scope: fleet-operator requesting findings for a non-member fleet is denied."""
        event = _event(
            method="GET",
            resource="/findings/fleet/{fleet_id}",
            path_params={"fleet_id": "fleet-B"},
            claims=_claims(groups=["fleet-operator"], fleet_ids=["fleet-A"]),
        )
        resp = h.handler(event, None)
        assert resp["statusCode"] == 403

    def test_dealership_route_denies_dealer_another_dealership(self):
        """Cross-scope: dealer requesting findings for a different dealer_id is denied."""
        event = _event(
            method="GET",
            resource="/findings/dealership/{dealer_id}",
            path_params={"dealer_id": "dealer-2"},
            claims=_claims(groups=["dealer-admin"], dealer_ids=["dealer-1"]),
        )
        resp = h.handler(event, None)
        assert resp["statusCode"] == 403

    def test_district_route_denies_district_a_manager_requesting_district_b(self):
        """Cross-scope: district-A manager cannot enumerate district-B findings."""
        event = _event(
            method="GET",
            resource="/findings/district/{district_id}",
            path_params={"district_id": "district-B"},
            claims=_claims(groups=["district-manager"], district_ids=["district-A"]),
        )
        resp = h.handler(event, None)
        assert resp["statusCode"] == 403

    def test_fleet_route_calls_gsi_helper(self):
        """Wiring guard: the fleet route calls _list_findings_by_fleet (not hardcoded []).

        T11.1: The sentinel item carries a projected top-level fleet_id attribute
        (as T10.2 would write to DDB).  The response body must strip it, so the
        expected findings contain only the finding_id key.
        """
        sentinel_finding = {"finding_id": "FIND-FLEET-001", "fleet_id": "fleet-A"}
        expected_stripped = {"finding_id": "FIND-FLEET-001"}  # fleet_id stripped
        with patch.object(h, "_list_findings_by_fleet", return_value=([sentinel_finding], None)) as mock_gsi:
            event = _event(
                method="GET",
                resource="/findings/fleet/{fleet_id}",
                path_params={"fleet_id": "fleet-A"},
                claims=_claims(groups=["fleet-operator"], fleet_ids=["fleet-A"]),
            )
            resp = h.handler(event, None)
        mock_gsi.assert_called_once_with("fleet-A", 50, None)
        body = _decode_body(resp)
        assert body["findings"] == [expected_stripped]

    def test_dealership_route_calls_gsi_helper(self):
        """Wiring guard: the dealership route calls _list_findings_by_dealership.

        T11.1: The sentinel item carries a projected top-level dealership_id attribute.
        The response body must strip it.
        """
        sentinel_finding = {"finding_id": "FIND-DEALER-001", "dealership_id": "dealer-1"}
        expected_stripped = {"finding_id": "FIND-DEALER-001"}  # dealership_id stripped
        with patch.object(h, "_list_findings_by_dealership", return_value=([sentinel_finding], None)) as mock_gsi:
            event = _event(
                method="GET",
                resource="/findings/dealership/{dealer_id}",
                path_params={"dealer_id": "dealer-1"},
                claims=_claims(groups=["dealer-admin"], dealer_ids=["dealer-1"]),
            )
            resp = h.handler(event, None)
        mock_gsi.assert_called_once_with("dealer-1", 50, None)
        body = _decode_body(resp)
        assert body["findings"] == [expected_stripped]

    def test_district_route_calls_gsi_helper(self):
        """Wiring guard: the district route calls _list_findings_by_district.

        T11.1: The sentinel item carries a projected top-level district_id attribute.
        The response body must strip it.
        """
        sentinel_finding = {"finding_id": "FIND-DIST-001", "district_id": "district-1"}
        expected_stripped = {"finding_id": "FIND-DIST-001"}  # district_id stripped
        with patch.object(h, "_list_findings_by_district", return_value=([sentinel_finding], None)) as mock_gsi:
            event = _event(
                method="GET",
                resource="/findings/district/{district_id}",
                path_params={"district_id": "district-1"},
                claims=_claims(groups=["district-manager"], district_ids=["district-1"]),
            )
            resp = h.handler(event, None)
        mock_gsi.assert_called_once_with("district-1", 50, None)
        body = _decode_body(resp)
        assert body["findings"] == [expected_stripped]


# ---------------------------------------------------------------------------
# POST /findings/{finding_id}/approve
# ---------------------------------------------------------------------------

_SAMPLE_FINDING = {
    "finding_id": "FIND-00000000-0000-0000-0000-000000000001",
    "finding_key": "a" * 64,
    "agent_id": "agent-2-vehicle-health",
    "agent_version": "1.0.0",
    # W-SEC-1 fix: scope includes owner_id so approve-route tests that use the
    # default consumer claims (sub="user-sub-123") can still approve this finding.
    # Tier 2 MUST emit owner_id in scope for consumer/vehicle-owner approval to
    # succeed — see decisions.md "Tier 2 owner_id emission" P2 follow-on.
    "scope": {"vin": "VIN-001", "owner_id": "user-sub-123"},
    "severity": "soon",
    "computed_at": "2026-09-18T02:00:00Z",
    "expires_at": "2026-09-25T02:00:00Z",
    "confidence": "high",
    "finding_kind": "health.tires",
    "headline": "Tire pressure low",
    "detail": "Front-left tire 24 PSI",
    "evidence": [{"kind": "tire_pressure_reading", "source": "adp", "source_ref": "ref1",
                  "value": {"psi": 24}, "computed_at": "2026-09-18T01:45:00Z", "provenance": "live"}],
    "inputs_hash": "b" * 64,
    "tokens_used": 100,
    "cost_usd": 0.001,
    "audit_trail": [],
}

_SAMPLE_ACTION = {
    "action_id": "AVX-ACT-aabbccdd-0000-0000-0000-000000000001",
    "finding_id": "FIND-00000000-0000-0000-0000-000000000001",
    "created_by": "owner-user-sub-123",
    "created_at": "2026-09-19T12:00:00Z",
    "updated_at": "2026-09-19T12:00:00Z",
    "target_system": "dms",
    "status": "pending_execution",
    "action_kind": "book_combined_visit",
    "parameters": {},
    "evidence": [],
    "audit_trail": [],
}


class TestApproveRoute:
    def _approve_event(self, finding_id, body, claims=None):
        return _event(
            method="POST",
            resource="/findings/{finding_id}/approve",
            path_params={"finding_id": finding_id},
            claims=claims or _claims(sub="user-sub-123", groups=["consumer"]),
            body=body,
        )

    def test_approve_happy_path(self, _hermetic_dispatch):
        mock_table = MagicMock()
        mock_table.put_item.return_value = {}

        with patch.object(h, "_load_finding_by_id", return_value=_SAMPLE_FINDING), \
             patch.object(h, "_get_ddb") as mock_ddb:
            mock_ddb.return_value.Table.return_value = mock_table
            event = self._approve_event(
                "FIND-00000000-0000-0000-0000-000000000001",
                {"target_system": "dms", "action_kind": "book_combined_visit", "parameters": _DMS_APPROVE_PARAMS},
                # platform-admin: DMS admits only fleet-operator / platform-admin (W2).
                claims=_claims(sub="user-sub-123", groups=["platform-admin"]),
            )
            resp = h.handler(event, None)

        assert resp["statusCode"] == 200
        body = _decode_body(resp)
        # The autouse fixture leaves the stored row as it was put; the real
        # result write is covered by TestFG35ApproveDispatch.
        assert body["status"] == "pending_execution"
        assert body["target_system"] == "dms"
        # Evidence copied from Finding
        assert body["evidence"] == _SAMPLE_FINDING["evidence"]
        # Fix Group 35: the put winner dispatches exactly once.
        assert len(_hermetic_dispatch) == 1

    def test_approve_creates_deterministic_action_id(self):
        """Same approve request returns same action_id (idempotency key)."""
        finding_id = "FIND-00000000-0000-0000-0000-000000000001"
        action_kind = "book_combined_visit"
        caller_sub = "user-sub-123"
        action_id_1 = h._derive_action_id(finding_id, action_kind, f"owner-{caller_sub}")
        action_id_2 = h._derive_action_id(finding_id, action_kind, f"owner-{caller_sub}")
        assert action_id_1 == action_id_2
        assert action_id_1.startswith("AVX-ACT-")

    def test_approve_idempotency_returns_existing_action(self):
        """Double-POST returns the same existing action."""
        from botocore.exceptions import ClientError

        mock_table = MagicMock()
        # Simulate ConditionalCheckFailed on put_item
        mock_table.put_item.side_effect = mock_table.meta.client.exceptions.ConditionalCheckFailedException(
            {"Error": {"Code": "ConditionalCheckFailedException", "Message": ""}}, "PutItem"
        )
        mock_table.meta.client.exceptions.ConditionalCheckFailedException = type(
            "ConditionalCheckFailedException", (ClientError,), {}
        )
        mock_table.put_item.side_effect = mock_table.meta.client.exceptions.ConditionalCheckFailedException(
            {"Error": {"Code": "ConditionalCheckFailedException"}}, "PutItem"
        )
        # The stored row carries what the handler writes for this same request
        # (VIN from the Finding, stripped dealer): since W4 a re-approve whose
        # parameters differ from the stored row is a 409, not a silent match.
        mock_table.get_item.return_value = {"Item": {
            **_SAMPLE_ACTION,
            "parameters": {**_DMS_APPROVE_PARAMS, "vehicle_vin": "VIN-001"},
        }}

        with patch.object(h, "_load_finding_by_id", return_value=_SAMPLE_FINDING), \
             patch.object(h, "_get_ddb") as mock_ddb:
            mock_ddb.return_value.Table.return_value = mock_table
            event = self._approve_event(
                "FIND-00000000-0000-0000-0000-000000000001",
                {"target_system": "dms", "action_kind": "book_combined_visit", "parameters": _DMS_APPROVE_PARAMS},
                claims=_claims(sub="user-sub-123", groups=["platform-admin"]),
            )
            resp = h.handler(event, None)

        assert resp["statusCode"] == 200
        body = _decode_body(resp)
        # Returns the existing action
        assert body["action_id"] == _SAMPLE_ACTION["action_id"]

    # MUTATION-VERIFIED: move ios-owner to _VALID_TARGET_SYSTEMS and empty _FORBIDDEN
    # (stronger mutation: ios-owner is actively allowed, not just unknown)
    # Expected: this test fails when mutation is applied. Reverted. ✓ CAUGHT
    def test_approve_rejects_ios_owner_target_system(self):
        """
        ios-owner is the reverse-direction executor (Spec 2's dealer side).
        Clients must not be able to inject cards into an owner's feed via approve.
        Security W1, contract v2.2-final.

        Mutation: _FORBIDDEN_TARGET_SYSTEMS = set() and ios-owner added to _VALID_TARGET_SYSTEMS
        → handler returns 200 with an Action row (bypass succeeds)
        → this test fails with AssertionError: 400 != 200
        → CAUGHT. Mutation reverted.
        """
        with patch.object(h, "_load_finding_by_id", return_value=_SAMPLE_FINDING):
            event = self._approve_event(
                "FIND-00000000-0000-0000-0000-000000000001",
                {"target_system": "ios-owner", "action_kind": "book_combined_visit"},
            )
            resp = h.handler(event, None)
        assert resp["statusCode"] == 400
        body = _decode_body(resp)
        # The forbidden-set rejection path includes the specific explanation
        assert "ios-owner" in body["error"]
        # Must NOT be the generic "unknown target_system" fallback —
        # the rejection must come from the explicit forbidden check
        assert "not permitted" in body["error"] or "ios-owner" in body["error"]

    def test_approve_ios_owner_error_is_explicit_not_generic(self):
        """The ios-owner rejection uses the explicit forbidden-check message, not
        the generic 'unknown target_system' fallback. This distinguishes the
        security boundary from mere absence in the valid set."""
        with patch.object(h, "_load_finding_by_id", return_value=_SAMPLE_FINDING):
            event = self._approve_event(
                "FIND-00000000-0000-0000-0000-000000000001",
                {"target_system": "ios-owner", "action_kind": "book_combined_visit"},
            )
            resp = h.handler(event, None)
        body = _decode_body(resp)
        # The message must describe WHY it's forbidden (channel-policy bypass),
        # not just say "unknown target_system"
        assert "not permitted" in body["error"] or "reverse-direction" in body["error"] or \
               "Spec 2" in body["error"] or "executor" in body["error"].lower()

    def test_approve_returns_404_for_missing_finding(self):
        with patch.object(h, "_load_finding_by_id", return_value=None):
            resp = h.handler(
                self._approve_event(
                    "FIND-00000000-0000-0000-0000-999999999999",
                    {"target_system": "dms", "action_kind": "book_combined_visit", "parameters": _DMS_APPROVE_PARAMS},
                ),
                None,
            )
        assert resp["statusCode"] == 404

    def test_approve_returns_403_for_wrong_owner(self):
        """A different owner cannot approve another owner's finding."""
        with patch.object(h, "_load_finding_by_id", return_value=_SAMPLE_FINDING):
            event = self._approve_event(
                "FIND-00000000-0000-0000-0000-000000000001",
                {"target_system": "dms", "action_kind": "book_combined_visit", "parameters": _DMS_APPROVE_PARAMS},
                claims=_claims(sub="different-owner", groups=[]),
            )
            resp = h.handler(event, None)
        assert resp["statusCode"] == 403

    def test_approve_denies_consumer_when_finding_has_no_owner_scope(self):
        """W-SEC-1: consumer cannot approve a VIN-only Finding that has no owner_id.

        Tier 2 currently emits scope: {"vin": vin} with no owner_id. Any consumer
        group member should be DENIED (403) for such findings — approval requires
        scope.owner_id == caller_sub. This is the write-path equivalent of the C3
        read-path fix.

        MUTATION GUARD (verified 2026-09-19 security fix cycle 1, Path B):
          Mutation: revert _check_approve_scope VIN-scope fallback from
            `return False` back to `return True`
          Expected: this test FAILS with AssertionError: 200 != 403
          Result: CONFIRMED — the defect is live at HEAD without the fix,
            and the test catches it. Mutation reverted. ✓ CAUGHT
        """
        finding_without_owner_id = {**_SAMPLE_FINDING, "scope": {"vin": "VIN-001"}}
        with patch.object(h, "_load_finding_by_id", return_value=finding_without_owner_id):
            event = self._approve_event(
                "FIND-00000000-0000-0000-0000-000000000001",
                {"target_system": "dms", "action_kind": "book_combined_visit", "parameters": _DMS_APPROVE_PARAMS},
                # Consumer group member whose sub does NOT appear as scope.owner_id
                # (because owner_id is absent from scope entirely)
                claims=_claims(sub="attacker-alice", groups=["consumer"]),
            )
            resp = h.handler(event, None)
        assert resp["statusCode"] == 403, (
            "consumer must not approve a VIN-only finding with no scope.owner_id "
            "(W-SEC-1: prevents cross-tenant Action creation)"
        )

    def test_approve_w2_fleet_operator_without_fleet_denied(self):
        """W2: fleet-operator with no matching fleet_id for the finding's fleet_id is denied."""
        fleet_finding = {**_SAMPLE_FINDING, "scope": {"fleet_id": "fleet-X"}}
        with patch.object(h, "_load_finding_by_id", return_value=fleet_finding):
            event = self._approve_event(
                "FIND-00000000-0000-0000-0000-000000000001",
                {"target_system": "dms", "action_kind": "book_combined_visit", "parameters": _DMS_APPROVE_PARAMS},
                # fleet-operator with fleet-Y, not fleet-X
                claims=_claims(sub="op", groups=["fleet-operator"], fleet_ids=["fleet-Y"]),
            )
            resp = h.handler(event, None)
        assert resp["statusCode"] == 403

    def test_approve_w2_fleet_operator_with_correct_fleet_allowed(self):
        """W2: fleet-operator holding the finding's fleet_id is allowed.

        Review cycle 2 (W1) added the `AVX_FLEET_IDS_CLAIM_TRUSTED` gate to this
        pre-existing arm, so the flag is now required — see the companion
        `test_approve_w1_fleet_scoped_arm_denied_without_the_trust_flag`.
        """
        fleet_finding = {**_SAMPLE_FINDING, "scope": {"fleet_id": "fleet-X"}}
        mock_table = MagicMock()
        mock_table.put_item.return_value = {}
        with patch.object(h, "_load_finding_by_id", return_value=fleet_finding), \
             patch.dict(os.environ, {"AVX_FLEET_IDS_CLAIM_TRUSTED": "true"}), \
             patch.object(h, "_get_ddb") as mock_ddb:
            mock_ddb.return_value.Table.return_value = mock_table
            event = self._approve_event(
                "FIND-00000000-0000-0000-0000-000000000001",
                _ADVISORY_APPROVE_BODY,
                claims=_claims(sub="op", groups=["fleet-operator"], fleet_ids=["fleet-X"]),
            )
            resp = h.handler(event, None)
        assert resp["statusCode"] == 200

    def test_approve_w1_fleet_scoped_arm_denied_without_the_trust_flag(self):
        """Review cycle 2 (W1): the FLEET-scoped arm read `custom:fleetIds` ungated.

        Fix Group 26 gated the VIN-branch fleet arm and left this one open, 63 lines
        up in the same function — so the identical forged claim was denied on one path
        and approved on the other, in the deployed default env.
        """
        fleet_finding = {**_SAMPLE_FINDING, "scope": {"fleet_id": "fleet-X"}}
        env = {k: v for k, v in os.environ.items()
               if k not in ("AVX_FLEET_IDS_CLAIM_TRUSTED", "AVX_VEHICLE_ID_CLAIM_TRUSTED",
                            "AVX_CUSTOMER_ID_CLAIM_TRUSTED")}
        with patch.object(h, "_load_finding_by_id", return_value=fleet_finding), \
             patch.dict(os.environ, env, clear=True), \
             patch.object(h, "_get_ddb") as mock_ddb:
            mock_ddb.return_value.Table.return_value = MagicMock()
            event = self._approve_event(
                "FIND-00000000-0000-0000-0000-000000000001",
                {"target_system": "dms", "action_kind": "book_combined_visit", "parameters": _DMS_APPROVE_PARAMS},
                claims=_claims(sub="op", groups=["fleet-operator"], fleet_ids=["fleet-X"]),
            )
            resp = h.handler(event, None)
        assert resp["statusCode"] == 403, (
            "an untrusted custom:fleetIds claim must not approve a FLEET-scoped Finding"
        )

    def test_approve_validates_action_kind(self):
        with patch.object(h, "_load_finding_by_id", return_value=_SAMPLE_FINDING):
            event = self._approve_event(
                "FIND-00000000-0000-0000-0000-000000000001",
                {"target_system": "dms", "action_kind": "invalid_kind"},
            )
            resp = h.handler(event, None)
        assert resp["statusCode"] == 400

    def test_approve_requires_target_system(self):
        with patch.object(h, "_load_finding_by_id", return_value=_SAMPLE_FINDING):
            event = self._approve_event(
                "FIND-00000000-0000-0000-0000-000000000001",
                {"action_kind": "book_combined_visit"},
            )
            resp = h.handler(event, None)
        assert resp["statusCode"] == 400

    def test_approve_unknown_target_system_returns_400(self):
        with patch.object(h, "_load_finding_by_id", return_value=_SAMPLE_FINDING):
            event = self._approve_event(
                "FIND-00000000-0000-0000-0000-000000000001",
                {"target_system": "bogus-system", "action_kind": "book_combined_visit"},
            )
            resp = h.handler(event, None)
        assert resp["statusCode"] == 400


# ---------------------------------------------------------------------------
# Route fallthrough
# ---------------------------------------------------------------------------

class TestUnknownRoute:
    def test_unknown_route_returns_404(self):
        event = _event(
            method="GET",
            resource="/findings/unknown-path",
            claims=_claims(groups=["platform-admin"]),
        )
        resp = h.handler(event, None)
        assert resp["statusCode"] == 404



# ---------------------------------------------------------------------------
# W-C3-fleet: fleet-dimension scope guard on /findings/vehicle/{vin}
# ---------------------------------------------------------------------------

class TestFleetVehicleScopeGuard:
    """W-C3-fleet fix (2026-09-19 fix cycle 2): fleet callers must only see findings
    for VINs in their own fleets.

    Mutation procedure (recorded 2026-09-19 fix cycle 2):
      Mutation A: replace `_list_findings_by_vin_for_fleet` call with unfiltered
        `_list_findings_by_vin` in handler's elif is_fleet_caller branch.
        Result: test_fleet_operator_cannot_enum_out_of_fleet_vin FAILS.
        test_fleet_operator_cannot_enum_out_of_fleet_vin_filtered_helper_called FAILS.
        Mutation reverted. ✓ CAUGHT

      Mutation B: in is_fleet_vehicle_caller, return (True, fleet_ids) even for
        platform-admin (remove the privileged-role short-circuit).
        Result: test_platform_admin_sees_all_vehicles_unfiltered FAILS.
        Mutation reverted. ✓ CAUGHT
    """

    def test_fleet_operator_cannot_enum_out_of_fleet_vin(self):
        """T12.1: fleet-operator gets unconditional 503 for all VINs.

        Updated from W-C3-fleet: the probe is gone and these paths return 503
        regardless of which fleet the caller belongs to. The entitlement check
        (no fleet_ids → 403) still runs first; any fleet-bearing caller gets 503.
        See TestT12_1_AntiOracle for the cross-tenant isolation assertion.
        """
        event = _event(
            method="GET",
            resource="/findings/vehicle/{vin}",
            path_params={"vin": "VIN-001"},
            claims=_claims(sub="op-fleet-a", groups=["fleet-operator"], fleet_ids=["FLT-A"]),
        )
        resp = h.handler(event, None)
        assert resp["statusCode"] == 503
        body = json.loads(resp["body"])
        assert body["error"] == "fleet_scope_unavailable"

    def test_fleet_driver_cannot_enum_out_of_fleet_vin(self):
        """T12.1: fleet-driver gets unconditional 503 for all VINs.

        Updated from W-C3-fleet: same as fleet-operator — 503 regardless of VIN.
        """
        event = _event(
            method="GET",
            resource="/findings/vehicle/{vin}",
            path_params={"vin": "VIN-002"},
            claims=_claims(sub="driver-fleet-a", groups=["fleet-driver"], fleet_ids=["FLT-A"]),
        )
        resp = h.handler(event, None)
        assert resp["statusCode"] == 503
        assert json.loads(resp["body"])["error"] == "fleet_scope_unavailable"

    def test_fleet_operator_in_matching_fleet_sees_vin_findings(self):
        """T12.1: fleet-operator in fleet-A still gets 503 for VINs until projection lands.

        Updated from W-C3-fleet: the filtered-query path is unavailable regardless
        of whether the caller's fleet matches the finding's fleet_id. The entitlement
        check (fleet_ids non-empty) passes, then 503 is returned unconditionally.
        The coupling test (TestT12_1_WriterProjectionCoupling) will signal when to
        re-enable this path.
        """
        event = _event(
            method="GET",
            resource="/findings/vehicle/{vin}",
            path_params={"vin": "VIN-003"},
            claims=_claims(sub="op-fleet-a", groups=["fleet-operator"], fleet_ids=["FLT-A"]),
        )
        resp = h.handler(event, None)
        # 503, not 200 — unconditional until projection lands
        assert resp["statusCode"] == 503
        assert json.loads(resp["body"])["error"] == "fleet_scope_unavailable"

    def test_fleet_operator_with_no_fleet_ids_is_denied(self):
        """Fleet-operator with empty custom:fleetIds is denied at pre-check (check_vehicle_scope).

        check_vehicle_scope returns False for fleet-operator with no fleetIds.
        is_fleet_vehicle_caller also returns (False, []) for them.
        They fall into the else/service-admin branch and fail check_vehicle_scope.
        """
        with patch.object(h, "_list_findings_by_vin", return_value=([], None)) as mock_unfiltered, \
             patch.object(h, "_list_findings_by_vin_for_fleet", return_value=([], None)) as mock_fleet:
            event = _event(
                method="GET",
                resource="/findings/vehicle/{vin}",
                path_params={"vin": "VIN-004"},
                claims=_claims(sub="op-no-fleet", groups=["fleet-operator"], fleet_ids=[]),
            )
            resp = h.handler(event, None)

        # Neither query should be called — pre-check denies
        mock_unfiltered.assert_not_called()
        mock_fleet.assert_not_called()
        assert resp["statusCode"] == 403

    def test_platform_admin_sees_all_vehicles_unfiltered(self):
        """platform-admin bypasses fleet filtering entirely.

        MUTATION GUARD: if is_fleet_vehicle_caller returns (True, fleet_ids) for
        platform-admin (removing the privileged-role short-circuit), the admin would
        be routed to the fleet-filtered query — this test FAILS because
        _list_findings_by_vin would not be called.
        """
        admin_findings = [
            {"finding_id": "FIND-X", "scope": {"vin": "VIN-005", "fleet_id": "FLT-C"}},
        ]
        with patch.object(h, "_list_findings_by_vin", return_value=(admin_findings, None)) as mock_unfiltered, \
             patch.object(h, "_list_findings_by_vin_for_fleet", return_value=([], None)) as mock_fleet:
            event = _event(
                method="GET",
                resource="/findings/vehicle/{vin}",
                path_params={"vin": "VIN-005"},
                claims=_claims(sub="admin-sub", groups=["platform-admin"]),
            )
            resp = h.handler(event, None)

        # Admin must use the unfiltered path, not the fleet-filtered path
        mock_unfiltered.assert_called_once()
        mock_fleet.assert_not_called()
        assert resp["statusCode"] == 200
        assert len(json.loads(resp["body"])["findings"]) == 1



# ---------------------------------------------------------------------------
# T11.1 — Response boundary schema validation
#
# Every Finding item in a handler response MUST validate against the frozen
# finding.schema.json contract (root additionalProperties:false, 18 permitted
# keys — none of them a bare scope field name).
#
# T10.2 denormalises the active scope field to a top-level DDB attribute so the
# five sparse GSIs work.  These projected attributes (vin, owner_id, fleet_id,
# dealership_id, district_id) must be stripped at the response boundary and must
# NOT appear in the JSON the caller receives.
#
# The test that matters: take each Finding in the response body and validate it
# against the schema FILE with jsonschema.validate().  A proxy assertion like
# ``"fleet_id" not in item`` is weaker (a future schema change that adds a
# different forbidden key would slip past); validating against the schema file
# itself catches the whole class.
#
# Mutation verification results (T11.1 mutations, 2026-09-20):
#
#   (a) Remove the strip (comment out _strip_finding_for_response in each
#       list-response return): schema-validation test for each scope field FAILED
#       as expected. Mutation reverted. ✓ CAUGHT
#
#   (b) Strip the nested ``scope`` object instead of the projected attribute
#       (change ``_PROJECTED_SCOPE_ATTRS`` to ``{"scope"}``): the test
#       ``test_strip_removes_projected_not_scope_object`` FAILED — scope was
#       missing from the response item, causing schema validation to fail on
#       the ``required`` check (scope is required). Mutation reverted. ✓ CAUGHT
#
#   (c) Apply the strip on only the fleet list route but not the owner list route
#       (remove the strip call from the owner-by-owner route only):
#       ``test_schema_valid_owner_id_scoped_finding`` FAILED as expected.
#       Mutation reverted. ✓ CAUGHT
# ---------------------------------------------------------------------------

import pathlib
import jsonschema

_SCHEMA_DIR = (
    pathlib.Path(__file__).parent.parent.parent.parent
    / "agents" / "tier2" / "schemas"
)
_FINDING_SCHEMA_PATH = _SCHEMA_DIR / "finding.schema.json"
_FINDING_SCHEMA = json.loads(_FINDING_SCHEMA_PATH.read_text())


def _make_ddb_finding(scope_field: str, scope_value: str) -> dict:
    """Build a minimal Finding dict shaped as T10.2 writes to DDB.

    The item has:
      - ``scope`` nested object with exactly one field set (contract invariant)
      - a matching top-level ``scope_field`` attribute (T10.2 denormalisation)
      - all required non-scope keys filled with valid values

    This is the shape a real DDB Query returns via handler query helpers.

    fleet_id-scoped findings require per-VIN evidence entries per Addendum D1
    (scope.fleet_id → each evidence entry must carry a 'vin' field, minItems:1).
    """
    # Build evidence that satisfies Addendum D1 for fleet_id-scoped findings.
    evidence_entry = {
        "kind": "adp.tires.prediction",
        "source": "adp-staging",
        "source_ref": "athena-query-K7QIFGDP27",
        "value": {"pressure_psi": 28.1},
        "computed_at": "2026-09-18T02:00:00Z",
        "provenance": "live",
    }
    if scope_field == "fleet_id":
        # Addendum D1: fleet_id-scoped findings must carry per-entry 'vin' attribution.
        evidence_entry = {**evidence_entry, "vin": "1HGBH41JXMN109186"}

    return {
        # T10.2 top-level denormalised attribute — MUST be stripped before response
        scope_field: scope_value,
        # Required finding fields (minimal but schema-valid)
        "finding_id": "FIND-a1b2c3d4-e5f6-7890-abcd-ef1234567890",
        "finding_key": "e3d71785c0a854b79f71bd239e2d15b63fa7ce38c8421cc1ead9529f4669bab0",
        "agent_id": "agent-2-vehicle-health",
        "agent_version": "1.0.0",
        "scope": {scope_field: scope_value},
        "severity": "attention",
        "computed_at": "2026-09-18T02:00:00Z",
        "expires_at": "2026-09-25T02:00:00Z",
        "confidence": "high",
        "finding_kind": "health.tires",
        "headline": "Left front tire pressure 18% below threshold",
        "detail": "Detail text.",
        "evidence": [evidence_entry],
        "inputs_hash": "049fab714fa0ed1be665a633770cc46f996f4161700a9c27b5c447b3ee72cc36",
        "tokens_used": 0,
        "cost_usd": 0.0,
        "audit_trail": [],
    }


def _assert_findings_valid(response_body: dict) -> None:
    """Assert every Finding in response_body['findings'] validates against the schema."""
    items = response_body["findings"]
    for idx, item in enumerate(items):
        try:
            jsonschema.validate(item, _FINDING_SCHEMA)
        except jsonschema.ValidationError as exc:
            raise AssertionError(
                f"Finding at index {idx} failed schema validation: {exc.message}"
            ) from exc


class TestT11_1_ResponseBoundarySchemaValidation:
    """T11.1 — verify that projected top-level scope attributes are stripped from
    every handler response and that each returned Finding validates against the
    frozen finding.schema.json contract.

    Each test supplies a DDB-shaped item (including the T10.2 top-level scope
    attribute) via the mocked query helper, calls the handler, and then validates
    every item in the response body against the schema FILE.
    """

    # ------------------------------------------------------------------
    # All five scope fields — one test each (list route)
    # ------------------------------------------------------------------

    def test_schema_valid_vin_scoped_finding(self):
        """VIN-scoped Finding: top-level 'vin' attribute stripped at response boundary."""
        ddb_item = _make_ddb_finding("vin", "1HGBH41JXMN109186")
        with patch.object(h, "_list_findings_by_vin", return_value=([ddb_item], None)):
            event = _event(
                method="GET",
                resource="/findings/vehicle/{vin}",
                path_params={"vin": "1HGBH41JXMN109186"},
                claims=_claims(sub="svc", groups=["service-advisor"]),
            )
            resp = h.handler(event, None)
        assert resp["statusCode"] == 200
        body = _decode_body(resp)
        _assert_findings_valid(body)

    def test_schema_valid_owner_id_scoped_finding(self):
        """owner_id-scoped Finding: top-level 'owner_id' attribute stripped."""
        ddb_item = _make_ddb_finding("owner_id", "user-abc-123")
        with patch.object(h, "_list_findings_by_owner", return_value=([ddb_item], None)):
            event = _event(
                method="GET",
                resource="/findings/owner/{owner_id}",
                path_params={"owner_id": "user-abc-123"},
                claims=_claims(sub="user-abc-123", groups=["consumer"]),
            )
            resp = h.handler(event, None)
        assert resp["statusCode"] == 200
        body = _decode_body(resp)
        _assert_findings_valid(body)

    def test_schema_valid_fleet_id_scoped_finding(self):
        """fleet_id-scoped Finding: top-level 'fleet_id' attribute stripped."""
        ddb_item = _make_ddb_finding("fleet_id", "FLT-001")
        with patch.object(h, "_list_findings_by_fleet", return_value=([ddb_item], None)):
            event = _event(
                method="GET",
                resource="/findings/fleet/{fleet_id}",
                path_params={"fleet_id": "FLT-001"},
                claims=_claims(sub="op-1", groups=["fleet-operator"], fleet_ids=["FLT-001"]),
            )
            resp = h.handler(event, None)
        assert resp["statusCode"] == 200
        body = _decode_body(resp)
        _assert_findings_valid(body)

    def test_schema_valid_dealership_id_scoped_finding(self):
        """dealership_id-scoped Finding: top-level 'dealership_id' attribute stripped."""
        ddb_item = _make_ddb_finding("dealership_id", "DLR-999")
        with patch.object(h, "_list_findings_by_dealership", return_value=([ddb_item], None)):
            event = _event(
                method="GET",
                resource="/findings/dealership/{dealer_id}",
                path_params={"dealer_id": "DLR-999"},
                claims=_claims(sub="mgr-1", groups=["dealer-admin"], dealer_ids=["DLR-999"]),
            )
            resp = h.handler(event, None)
        assert resp["statusCode"] == 200
        body = _decode_body(resp)
        _assert_findings_valid(body)

    def test_schema_valid_district_id_scoped_finding(self):
        """district_id-scoped Finding: top-level 'district_id' attribute stripped."""
        ddb_item = _make_ddb_finding("district_id", "DIST-007")
        with patch.object(h, "_list_findings_by_district", return_value=([ddb_item], None)):
            event = _event(
                method="GET",
                resource="/findings/district/{district_id}",
                path_params={"district_id": "DIST-007"},
                claims=_claims(sub="mgr-d7", groups=["district-manager"], district_ids=["DIST-007"]),
            )
            resp = h.handler(event, None)
        assert resp["statusCode"] == 200
        body = _decode_body(resp)
        _assert_findings_valid(body)

    # ------------------------------------------------------------------
    # owner-fleet route (also uses _list_findings_by_owner)
    # ------------------------------------------------------------------

    def test_schema_valid_owner_fleet_route(self):
        """owner-fleet route also strips projected scope attributes."""
        ddb_item = _make_ddb_finding("owner_id", "user-xyz")
        with patch.object(h, "_list_findings_by_owner", return_value=([ddb_item], None)):
            event = _event(
                method="GET",
                resource="/findings/owner-fleet/{owner_id}",
                path_params={"owner_id": "user-xyz"},
                claims=_claims(sub="user-xyz"),
            )
            resp = h.handler(event, None)
        assert resp["statusCode"] == 200
        body = _decode_body(resp)
        _assert_findings_valid(body)

    # ------------------------------------------------------------------
    # Vehicle route — owner-caller path (T12.1: unconditional 503)
    # ------------------------------------------------------------------

    def test_schema_valid_vehicle_route_owner_caller(self):
        """Vehicle route (owner-caller path) returns 503 unconditionally (T12.1).

        Schema strip validation for this path is deferred until the owner_id
        projection lands and the path is re-enabled. The service-advisor path
        below covers strip correctness for VIN-scoped findings.
        """
        event = _event(
            method="GET",
            resource="/findings/vehicle/{vin}",
            path_params={"vin": "1HGBH41JXMN109186"},
            claims=_claims(sub="owner-sub-123", groups=["vehicle-owner"]),
        )
        resp = h.handler(event, None)
        # T12.1: unconditional 503 — no data returned
        assert resp["statusCode"] == 503
        body = _decode_body(resp)
        assert body["error"] == "owner_scope_unavailable"

    # ------------------------------------------------------------------
    # Vehicle route — fleet-caller path (T12.1: unconditional 503)
    # ------------------------------------------------------------------

    def test_schema_valid_vehicle_route_fleet_caller(self):
        """Vehicle route (fleet-caller path) returns 503 unconditionally (T12.1).

        Schema strip validation for this path is deferred until the fleet_id
        projection lands and the path is re-enabled.
        """
        event = _event(
            method="GET",
            resource="/findings/vehicle/{vin}",
            path_params={"vin": "VIN-A1"},
            claims=_claims(sub="driver-1", groups=["fleet-driver"], fleet_ids=["FLT-A"]),
        )
        resp = h.handler(event, None)
        assert resp["statusCode"] == 503
        body = _decode_body(resp)
        assert body["error"] == "fleet_scope_unavailable"

    # ------------------------------------------------------------------
    # Mutation guard: nested scope object is untouched
    # ------------------------------------------------------------------

    def test_strip_removes_projected_not_scope_object(self):
        """The strip removes the top-level scope attribute but leaves scope intact.

        MUTATION GUARD: if _PROJECTED_SCOPE_ATTRS were changed to include 'scope'
        instead of (or in addition to) the bare field names, the nested scope
        object would be stripped and this test would fail because the item would
        no longer carry scope, failing the schema's required check.
        """
        ddb_item = _make_ddb_finding("fleet_id", "FLT-Z")
        with patch.object(h, "_list_findings_by_fleet", return_value=([ddb_item], None)):
            event = _event(
                method="GET",
                resource="/findings/fleet/{fleet_id}",
                path_params={"fleet_id": "FLT-Z"},
                claims=_claims(sub="op-z", groups=["fleet-operator"], fleet_ids=["FLT-Z"]),
            )
            resp = h.handler(event, None)
        assert resp["statusCode"] == 200
        body = _decode_body(resp)
        items = body["findings"]
        assert len(items) == 1
        item = items[0]
        # Projected top-level attribute MUST be absent
        assert "fleet_id" not in item, (
            "fleet_id projected attribute must be stripped from response"
        )
        # Nested scope object MUST be present and contain the value
        assert item.get("scope", {}).get("fleet_id") == "FLT-Z", (
            "scope.fleet_id must remain untouched in the response"
        )
        # Full schema validation confirms nothing else was accidentally stripped
        jsonschema.validate(item, _FINDING_SCHEMA)



# ---------------------------------------------------------------------------
# T11.2 — Honest 503 for unscopeable VIN paths
# ---------------------------------------------------------------------------




# ---------------------------------------------------------------------------
# T12.1 — Anti-oracle: VIN with findings and VIN without produce identical responses
# ---------------------------------------------------------------------------

class TestT12_1_AntiOracle:
    """T12.1 anti-oracle: a VIN with Findings and a VIN without produce IDENTICAL
    responses for the same caller (owner-caller and fleet-caller).

    The original T11.2 probe leaked one bit of cross-tenant existence:
      200 [] → this VIN has ZERO Findings
      503    → this VIN has at LEAST ONE Finding
    That distinction lets any authenticated caller enumerate tenant data on VINs
    they have no entitlement to.

    T12.1 closes this by returning 503 unconditionally — neither branch runs a
    by_vin probe query. The response is identical regardless of table contents.

    Mutation (a) (recorded in decisions.md):
      Restore the probe branching: _probe_vin returns None (no findings) → 200 [];
      _probe_vin returns False (findings exist, no attr) → 503.
      Expected: test_owner_vin_with_findings_and_without_are_identical FAILS because
      one returns 200, the other 503. ✓ CAUGHT
    """

    # ------------------------------------------------------------------
    # Owner-caller: VIN with findings == VIN without findings
    # ------------------------------------------------------------------

    def test_owner_vin_with_findings_and_without_are_identical(self):
        """MUTATION GUARD (a): owner-caller gets byte-identical responses for a
        VIN-with-findings and a VIN-without-findings.

        Both must return 503 + reason 'owner_scope_unavailable'.
        If the probe were restored (T11.2 regression), VIN-without-findings
        returns 200 [] and VIN-with-findings returns 503 — this test FAILS.
        """
        owner_claims = _claims(sub="owner-sub-T12", groups=["vehicle-owner"])

        # VIN that has no findings in the table (response must NOT leak this fact)
        resp_empty = h.handler(_event(
            method="GET",
            resource="/findings/vehicle/{vin}",
            path_params={"vin": "VIN-HAS-NO-FINDINGS"},
            claims=owner_claims,
        ), None)

        # VIN that has findings in the table (response must NOT leak this fact)
        resp_has = h.handler(_event(
            method="GET",
            resource="/findings/vehicle/{vin}",
            path_params={"vin": "VIN-HAS-SOME-FINDINGS"},
            claims=owner_claims,
        ), None)

        # Both must be 503
        assert resp_empty["statusCode"] == 503, (
            f"VIN-with-no-findings returned {resp_empty['statusCode']}, expected 503"
        )
        assert resp_has["statusCode"] == 503, (
            f"VIN-with-findings returned {resp_has['statusCode']}, expected 503"
        )

        # Full body equality — a differing message re-opens the leak at lower bandwidth
        body_empty = _decode_body(resp_empty)
        body_has = _decode_body(resp_has)
        assert body_empty == body_has, (
            f"Responses differ: VIN-with-no-findings={body_empty!r}, "
            f"VIN-with-findings={body_has!r}. "
            "Any difference re-opens the cross-tenant existence oracle."
        )

        # Reason code confirmed
        assert body_empty["error"] == "owner_scope_unavailable"

    # ------------------------------------------------------------------
    # Fleet-caller: VIN with findings == VIN without findings
    # ------------------------------------------------------------------

    def test_fleet_vin_with_findings_and_without_are_identical(self):
        """MUTATION GUARD (a): fleet-caller gets byte-identical responses for a
        VIN-with-findings and a VIN-without-findings.

        Both must return 503 + reason 'fleet_scope_unavailable'.
        """
        fleet_claims = _claims(sub="driver-T12", groups=["fleet-driver"], fleet_ids=["FLT-T12"])

        resp_empty = h.handler(_event(
            method="GET",
            resource="/findings/vehicle/{vin}",
            path_params={"vin": "VIN-FLEET-HAS-NO-FINDINGS"},
            claims=fleet_claims,
        ), None)

        resp_has = h.handler(_event(
            method="GET",
            resource="/findings/vehicle/{vin}",
            path_params={"vin": "VIN-FLEET-HAS-SOME-FINDINGS"},
            claims=fleet_claims,
        ), None)

        assert resp_empty["statusCode"] == 503, (
            f"Fleet VIN-with-no-findings returned {resp_empty['statusCode']}, expected 503"
        )
        assert resp_has["statusCode"] == 503, (
            f"Fleet VIN-with-findings returned {resp_has['statusCode']}, expected 503"
        )

        body_empty = _decode_body(resp_empty)
        body_has = _decode_body(resp_has)
        assert body_empty == body_has, (
            f"Fleet responses differ: no-findings={body_empty!r}, "
            f"has-findings={body_has!r}. "
            "Any difference re-opens the cross-tenant existence oracle."
        )
        assert body_empty["error"] == "fleet_scope_unavailable"

    # ------------------------------------------------------------------
    # Entitlement checks still run BEFORE the 503 (Accept 3)
    # ------------------------------------------------------------------

    def test_owner_caller_with_no_jwt_sub_gets_401(self):
        """JWT auth still runs before the 503. No sub → 401."""
        event = _event(
            method="GET",
            resource="/findings/vehicle/{vin}",
            path_params={"vin": "VIN-ANY"},
            claims={},  # no sub — triggers 401
        )
        event["requestContext"]["authorizer"]["claims"] = {}
        resp = h.handler(event, None)
        assert resp["statusCode"] == 401

    def test_fleet_caller_with_no_fleet_ids_gets_403_not_503(self):
        """Fleet-caller with no fleet_ids gets 403 BEFORE the 503.

        MUTATION GUARD (c): if the 503 were moved before the entitlement check,
        an unentitled caller (no fleetIds) would get 503 instead of 403.
        This test MUST fail if the entitlement check is reordered.
        """
        event = _event(
            method="GET",
            resource="/findings/vehicle/{vin}",
            path_params={"vin": "VIN-ANY"},
            claims=_claims(sub="driver-noflt", groups=["fleet-driver"], fleet_ids=[]),
        )
        resp = h.handler(event, None)
        assert resp["statusCode"] == 403, (
            f"Fleet caller with no fleet_ids must get 403 (entitlement failure), "
            f"not 503 (scope unavailable). Got: {resp['statusCode']}. "
            "The 503 must come AFTER the entitlement check — reordering creates "
            "an oracle disclosing that an entitlement check even exists."
        )

    # ------------------------------------------------------------------
    # Service/admin path still returns data (Accept 6)
    # ------------------------------------------------------------------

    def test_service_advisor_vin_path_still_returns_data(self):
        """Service-advisor VIN path is unaffected by T12.1 and still returns data."""
        vin_finding = {
            "finding_id": "FIND-SVC-T12",
            "scope": {"vin": "VIN-SVC-T12"},
        }
        with patch.object(h, "_list_findings_by_vin", return_value=([vin_finding], None)) as mock_svc:
            event = _event(
                method="GET",
                resource="/findings/vehicle/{vin}",
                path_params={"vin": "VIN-SVC-T12"},
                claims=_claims(sub="svc-t12", groups=["service-advisor"]),
            )
            resp = h.handler(event, None)
        assert resp["statusCode"] == 200, (
            f"Service path must still return 200 — T12.1 must not disable it. "
            f"Got: {resp['statusCode']}"
        )
        body = _decode_body(resp)
        assert len(body["findings"]) == 1
        mock_svc.assert_called_once()


# ---------------------------------------------------------------------------
# T12.1 — Coupling test: writer.py does not project owner_id/fleet_id for VIN-scope
# ---------------------------------------------------------------------------

class TestT12_1_WriterProjectionCoupling:
    """T12.1 coupling test — the self-heal signal.

    The owner-caller and fleet-caller VIN paths return 503 until writer.py starts
    projecting owner_id and fleet_id for VIN-scoped Findings.  This test asserts
    that the projection has NOT yet landed.  The moment it does, this test FAILS,
    which tells whoever enabled the projection exactly what to do next.

    When this test fails, its message reads:
      'PROJECTION LANDED: writer.py now emits top-level owner_id (and/or fleet_id) '
      'for VIN-scoped Findings. Re-enable the owner-caller and fleet-caller VIN paths '
      'in lambdas/api-avx-findings/handler.py (remove the unconditional 503 blocks) and '
      'lambdas/api-avx-actions/handler.py (same). Add tests that exercise those paths '
      'end-to-end. See decisions.md § "2026-09-20 — T12.1 cross-tenant oracle fix".'

    Mutation (b) (recorded in decisions.md):
      Add top-level owner_id to the denormalised item for a VIN-scoped finding
      (simulating the concurrent session's projection landing). This test MUST fail,
      proving it grips the writer's actual behaviour rather than asserting a constant.
    """

    def test_writer_does_not_project_owner_id_for_vin_scope(self):
        """SUPERSEDED 2026-09-23 by Fix Group 15 / F11.2.

        **This test's original property is now false, and this body no longer
        claims it.** F11.2 made ``writer._put_item`` project a top-level
        ``owner_id`` for VIN-scoped Findings *when a resolved owner is passed*.
        The original body called ``_put_item`` without the ``owner_id`` kwarg, so
        it exercised the one input under which the projection is correctly absent
        and stayed green through the very commit it existed to detect. Its
        "PROJECTION LANDED" message never printed.

        Repaired by asserting what is actually true now, in both directions:
          * no ``owner_id`` when none was resolved (the fail-closed default), and
          * ``owner_id`` present when one was — the landing this tripwire watched for.

        The re-enable decision has NOT been taken, and the 503 in ``handler.py``'s
        ``is_owner_caller`` branch is still correct. The blocker moved: it is no
        longer "the projection is missing" but "no caller claim lives in the
        projection's namespace". The projected value is the CMS registry's
        ``sold_to`` (``CUST-XXXXXXXX``); ``claims["sub"]`` is a Cognito UUID. That
        is now tracked by
        ``test_owner_scope_503_tripwire_tracks_the_identity_seam`` below.

        **The original step 2 was actively harmful and is corrected there.** It
        read: "call ``_list_findings_by_vin_for_owner`` with (vin, caller_sub,
        ...)". Doing that now filters top-level ``owner_id`` (``CUST-*``) against a
        UUID, matching nothing — so every owner would get a silent,
        complete-looking ``200 []``, strictly worse than a 503 that admits it
        cannot answer.
        """
        import sys
        import os
        import importlib
        from unittest.mock import MagicMock

        writer_path = os.path.normpath(
            os.path.join(os.path.dirname(__file__), "..", "..", "..", "agents", "tier2")
        )
        if writer_path not in sys.path:
            sys.path.append(writer_path)
        sys.modules.pop("writer", None)
        writer = importlib.import_module("writer")

        vin_scoped_finding = {
            "finding_id": "FIND-VIN-COUPLING-001",
            "finding_key": "abc123",
            "agent_id": "agent-2-vehicle-health",
            "agent_version": "1.0.0",
            "scope": {"vin": "VIN-COUPLING"},
            "severity": "attention",
            "computed_at": "2026-09-20T02:00:00Z",
            "expires_at": "2026-09-27T02:00:00Z",
            "confidence": "high",
            "finding_kind": "health.tires",
            "headline": "Test headline",
            "detail": "Test detail.",
            "evidence": [],
            "inputs_hash": "def456",
            "tokens_used": 0,
            "cost_usd": 0.0,
            "audit_trail": [],
        }

        from boto3.dynamodb.types import TypeDeserializer
        deserializer = TypeDeserializer()

        def _persisted(**extra):
            client = MagicMock()
            client.put_item.return_value = {}
            writer._put_item(
                table_name="test-findings-table",
                dynamodb_client=client,
                finding=vin_scoped_finding,
                **extra,
            )
            assert client.put_item.call_count == 1
            item = client.put_item.call_args.kwargs["Item"]
            return {k: deserializer.deserialize(v) for k, v in item.items()}

        # Direction 1 — no owner resolved: fail-closed, attribute absent.
        no_owner = _persisted()
        assert "owner_id" not in no_owner, (
            "with no resolved owner the projection MUST be omitted — not '', not None"
        )
        assert "fleet_id" not in no_owner, (
            "a VIN-scoped Finding must never carry a top-level fleet_id"
        )
        # T10.2 must keep working for VIN scope (preserved from the original test).
        assert no_owner.get("vin") == "VIN-COUPLING", (
            f"expected top-level 'vin' from T10.2, got keys: {list(no_owner.keys())}"
        )

        # Direction 2 — owner resolved: the projection lands. This is the
        # assertion the original test could not make, and the reason the
        # tripwire's exit condition had to move.
        # API broadened from owner_id= to standing= (Fix Group 25): the writer now
        # projects all three standing facts, not just the owner.
        from owner_resolver import StandingFacts  # same sys.path as `writer` above
        with_owner = _persisted(standing=StandingFacts(owner_id="CUST-004000DD"))
        assert with_owner.get("owner_id") == "CUST-004000DD", (
            "F11.2 projects a resolved owner onto the DDB item; if this fails the "
            "projection has been reverted and handler.py's 503 blocks are load-bearing "
            "again for their ORIGINAL reason"
        )
        assert "fleet_id" not in with_owner

    def test_owner_scope_503_tripwire_tracks_the_identity_seam(self):
        """An owner-group caller with NO trusted identity still gets the 503.

        **Rewritten 2026-09-26 (spec 2026-09-25-avx-own-vehicle-findings, review Cycle 2
        S11).** This was the tripwire that fired when the identity seam went live. It
        has fired: ``custom:customerId`` and ``custom:vehicleId`` are provisioned, and
        callers whose claims pass their ``AVX_*_CLAIM_TRUSTED`` gate now read their OWN
        partition (``by_owner`` / ``by_vehicle``, filtered on ``vin``; tests in
        ``test_findings_me.py``). The old re-enable steps named
        ``_list_findings_by_vin_for_owner``, which keys on the VIN's partition and has the
        ``Limit``-before-filter oracle (review Cycle 1 W1); they were not followed and must
        not be.

        What this guards now: with no trust flag set (this module's default), the claim
        set the live CMS pool can produce yields no trusted identity, and the handler
        returns 503 rather than any read. Reading a claim without its trust gate fails
        here.
        """
        # Every custom attribute the live CMS pool defines, plus sub, INCLUDING
        # custom:customerId (declared since 2026-09-24). Without it the customer-id
        # assertion below could not fail (review Cycle 3 W8).
        live_pool_claims = {
            "sub": "c8811380-c061-7085-0078-156412f0f674",
            "cognito:groups": json.dumps(["vehicle-owner"]),
            "custom:customerId": "CUST-004000DD",
            "custom:fleetIds": json.dumps([]),
            "custom:tenantId": "t-1",
            "custom:driverId": "d-1",
            "custom:role": "vehicle-owner",
            "custom:vehicleId": "VEH-MRDN-0015",
            "custom:provisionedVia": "seed",
            "custom:dealerIds": json.dumps([]),
            "custom:districtIds": json.dumps([]),
            "custom:subscriptionIds": "01M28TVET5BJ2PQTE4W5610TC7",
        }
        # Independent of the caller's shell: clear every identity trust flag here.
        env = {k: v for k, v in os.environ.items()
               if k not in ("AVX_FLEET_IDS_CLAIM_TRUSTED", "AVX_VEHICLE_ID_CLAIM_TRUSTED",
                            "AVX_CUSTOMER_ID_CLAIM_TRUSTED")}
        with patch.dict(os.environ, env, clear=True):
            assert h._caller_customer_id(live_pool_claims) is None, (
                "an untrusted custom:customerId produced a caller identity: the "
                "AVX_CUSTOMER_ID_CLAIM_TRUSTED gate is not being applied"
            )
            assert h._caller_vehicle_id(live_pool_claims) is None, (
                "an untrusted custom:vehicleId produced a caller identity: the "
                "AVX_VEHICLE_ID_CLAIM_TRUSTED gate is not being applied"
            )

        # And the handler returns the 503, not a read, for that caller.
        with patch.dict(os.environ, env, clear=True), \
             patch.object(h, "_load_finding_by_id", return_value=_FG15_VIN_FINDING_WITH_OWNER):
            resp = h.handler(
                _event(
                    method="GET",
                    resource="/findings/vehicle/{vin}",
                    path_params={"vin": "VIN-FG15-001"},
                    claims=live_pool_claims,
                ),
                None,
            )
        assert resp["statusCode"] == 503, (
            f"an owner caller with no trusted identity must get the 503; got {resp['statusCode']}"
        )


# ---------------------------------------------------------------------------
# T14.5 — Approve route Action audit-entry schema validation (W-E)
#
# T11.1's Verify required the approve response body to be validated against
# action.schema.json.  This is that validation for the findings handler's
# _approve_finding route.
#
# At time of authoring (T14.5 red phase) the approve route builds an Action
# with two audit-entry violations:
#   (1) "actor" present — additionalProperties:false rejects it
#   (2) "by" absent  — required field missing
#   (3) "from_status": None — required field must be a valid status string
#       (ESCALATION: from_status is required in $defs.audit_entry with no
#        valid initial-creation sentinel — see decisions.md T14.5 record)
#
# After the actor→by fix, this test STILL fails on from_status:None.
# That remaining failure is the escalation signal documented in decisions.md.
# ---------------------------------------------------------------------------

import pathlib as _fpl
import json as _fjson
import jsonschema as _fjsonschema

_FINDINGS_SCHEMA_DIR_T14_5 = (
    _fpl.Path(__file__).parent.parent.parent.parent
    / "agents" / "tier2" / "schemas"
)
_ACTION_SCHEMA_T14_5 = _fjson.loads(
    (_FINDINGS_SCHEMA_DIR_T14_5 / "action.schema.json").read_text()
)


class TestT14_5_ApproveBuildAuditEntrySchema:
    """T14.5 — verify that the Action built by _approve_finding has a valid
    audit_trail entry per action.schema.json $defs/audit_entry.

    The test calls the REAL _approve_finding code path (not a mock of
    _update_action_status) by patching _get_ddb at the DDB layer and
    inspecting what was passed to put_item.  This is the only way to catch
    the audit-entry shape the handler actually builds.

    After the actor→by fix: still fails on from_status:None (escalation).
    """

    def _call_approve_and_get_action(self) -> dict:
        """Call the approve route and return the Action dict passed to put_item."""
        import botocore.exceptions
        mock_table = MagicMock()
        mock_table.put_item.return_value = {}
        mock_table.meta.client.exceptions.ConditionalCheckFailedException = (
            type("ConditionalCheckFailedException",
                 (botocore.exceptions.ClientError,), {})
        )

        with patch.object(h, "_load_finding_by_id", return_value=_SAMPLE_FINDING), \
             patch.object(h, "_get_ddb") as mock_ddb:
            mock_ddb.return_value.Table.return_value = mock_table
            event = _event(
                method="POST",
                resource="/findings/{finding_id}/approve",
                path_params={"finding_id": "FIND-00000000-0000-0000-0000-000000000001"},
                claims=_claims(sub="user-sub-123", groups=["platform-admin"]),
                body={"target_system": "dms", "action_kind": "book_combined_visit", "parameters": _DMS_APPROVE_PARAMS},
            )
            resp = h.handler(event, None)

        assert resp["statusCode"] == 200, (
            f"Expected 200 from approve, got {resp['statusCode']}: {resp.get('body')}"
        )
        call_kwargs = mock_table.put_item.call_args.kwargs
        return call_kwargs["Item"]

    def test_approve_action_audit_entry_validates_against_schema(self):
        """Approve route emits audit_trail: [] at creation (T14.7).

        T14.5 escalation resolution: from_status is required in $defs.audit_entry
        with no valid initial-creation sentinel, so the trail is empty at creation.
        The trail records *transitions*; a creation is not a transition.

        MUTATION GUARDS (must be run in decisions.md):
          (a) Write from_status=None — schema validation MUST fail.
          (b) Write from_status=to_status (self-transition) — this test MUST fail,
              because schema validity alone cannot distinguish a fabricated self-
              transition from a real one, but len==0 can.
          (c) Drop trail append on a real transition — transition test MUST fail.

        Do NOT relax this assertion to len >= 0.  The correct property is
        exactly-empty at creation, not merely valid.
        """
        action = self._call_approve_and_get_action()
        audit_trail = action.get("audit_trail", [])
        assert len(audit_trail) == 0, (
            f"Expected empty audit_trail at creation (T14.7 decision: creation is not "
            f"a transition; trail records transitions only). Got {len(audit_trail)} "
            f"entries: {audit_trail!r}. "
            "If an entry was added with from_status=to_status, that is a fabricated "
            "self-transition — mutation (b) guard exists to catch exactly this."
        )


# ---------------------------------------------------------------------------
# T14.4 — FilterExpression shape pins for dead-code helpers
#
# All three helpers below (_list_findings_by_vin_for_owner,
# _list_findings_by_vin_for_fleet — in this file; _list_actions_by_vin_for_fleet
# in the actions test file) are dead code: their calling routes return 503
# unconditionally until the T11.6 follow-on (b) projection lands.
#
# The purpose of these tests is to pin the FilterExpression shape so that:
#   (a) the two-handler divergence (scope.fleet_id vs fleet_id) fails loudly
#       before re-enable rather than surfacing at runtime, and
#   (b) mutation (a) — reverting to the pre-T14.4 loosenings — is caught by
#       the suite rather than passing silently.
#
# These tests call the helpers directly (not through the route), patch DynamoDB,
# and capture the kwargs dict passed to table.query().  They do NOT route through
# handler() so they are not affected by the 503 blocks.
#
# Each test also proves the "matches nothing today" claim: a DynamoDB query with
# FilterExpression=Attr("fleet_id").is_in([...]) on a table whose items all carry
# scope.fleet_id (nested) rather than a top-level fleet_id attribute will return
# nothing — the test asserts the shape, and the shape's correctness-under-projection
# is proven by the comment, not by a live-data assertion.
#
# Mutation (a) deliverable: after the test suite passes, the caller must verify that
# reverting the FilterExpression on any of these three helpers causes the
# corresponding test to fail.  This is documented in decisions.md.
# ---------------------------------------------------------------------------

class TestT14_4_FilterExpressionShapePins:
    """T14.4: pin the FilterExpression shape of all three dead-code VIN helpers.

    Mutation (a) probe: each test MUST fail if the FilterExpression is reverted to
    the pre-T14.4 loosenings (scope.owner_id, scope.fleet_id, or the dual-shape OR).
    Recorded in decisions.md.
    """

    # ------------------------------------------------------------------
    # Helper: call a function and capture the kwargs passed to table.query
    # ------------------------------------------------------------------

    def _capture_query_kwargs(self, fn, *args, **kwargs):
        """Call fn(*args, **kwargs) with DynamoDB patched; return the captured
        kwargs dict that was passed to table.query()."""
        captured = {}

        class _MockTable:
            def query(self_, **kw):
                captured.update(kw)
                return {"Items": [], "Count": 0}

        with patch.object(h, "_get_ddb") as mock_ddb:
            mock_ddb.return_value.Table.return_value = _MockTable()
            fn(*args, **kwargs)

        return captured

    # ------------------------------------------------------------------
    # _list_findings_by_vin_for_owner
    # ------------------------------------------------------------------

    def test_findings_by_vin_for_owner_uses_top_level_owner_id(self):
        """_list_findings_by_vin_for_owner uses Attr("owner_id").eq(owner_id).

        T14.4: the pre-T14.4 filter was
            Attr("scope.owner_id").eq(owner_id) | Attr("owner_id").eq(owner_id)
        (dual-shape, wrong — scope.owner_id is the pre-projection nested path).
        After T14.4 it is:
            Attr("owner_id").eq(owner_id)
        (top-level only, matches when the T11.6 follow-on (b) projection writes it).

        Mutation (a): revert to the dual-shape OR — this test MUST fail because
        the captured FilterExpression will contain a CompoundConditionBase (OR)
        rather than an AttributeBase equality condition on the top-level key.
        """
        import pytest
        if not hasattr(h, "_list_findings_by_vin_for_owner"):
            pytest.skip("Skipping: not running against the findings handler")

        from boto3.dynamodb.conditions import Attr, ConditionBase

        captured = self._capture_query_kwargs(
            h._list_findings_by_vin_for_owner, "VIN-TEST", "OWNER-001"
        )

        assert "FilterExpression" in captured, (
            "Expected a FilterExpression in query kwargs; none was passed."
        )
        fe = captured["FilterExpression"]
        fe_expr = fe.get_expression()

        # Must be a simple equality (not an OR compound expression)
        assert fe_expr.get("operator") != "OR", (
            f"T14.4 shape mismatch: FilterExpression is an OR compound expression "
            f"(pre-T14.4 dual-shape). Must be a single Attr(\"owner_id\").eq(...). "
            f"operator={fe_expr.get('operator')!r}"
        )
        assert fe_expr.get("operator") == "=", (
            f"T14.4 shape mismatch: expected equality operator '=', got "
            f"{fe_expr.get('operator')!r}"
        )

        # Extract the Attr name from the expression values
        attr_obj = fe_expr["values"][0]
        attr_name = getattr(attr_obj, "name", None)

        # Must use the top-level attribute (not scope.owner_id)
        assert attr_name == "owner_id", (
            f"T14.4 shape mismatch: FilterExpression attribute name is {attr_name!r}, "
            f"expected 'owner_id' (top-level). Pre-T14.4 used 'scope.owner_id' (nested). "
            "Fix: use Attr(\"owner_id\").eq(owner_id) — the projection writes to "
            "top-level owner_id, not scope.owner_id."
        )

    def test_findings_by_vin_for_owner_matches_nothing_before_projection(self):
        """Anti-vacuity: the mock returns [] confirming the helper runs without error."""
        import pytest
        if not hasattr(h, "_list_findings_by_vin_for_owner"):
            pytest.skip("Skipping: not running against the findings handler")
        # Verify the helper is callable and the mock DynamoDB returns cleanly.
        captured = self._capture_query_kwargs(
            h._list_findings_by_vin_for_owner, "VIN-NO-PROJ", "OWNER-X"
        )
        assert "FilterExpression" in captured, (
            "Expected a FilterExpression in query kwargs; none was passed."
        )

    # ------------------------------------------------------------------
    # _list_findings_by_vin_for_fleet
    # ------------------------------------------------------------------

    def test_findings_by_vin_for_fleet_uses_top_level_fleet_id(self):
        """_list_findings_by_vin_for_fleet uses Attr("fleet_id").is_in(caller_fleet_ids).

        T14.4: the pre-T14.4 filter was
            Attr("scope.fleet_id").is_in(caller_fleet_ids)
        (nested, wrong — scope.fleet_id is the pre-projection nested path).
        After T14.4 it is:
            Attr("fleet_id").is_in(caller_fleet_ids)
        (top-level, matches when the T11.6 follow-on (b) projection writes it).
        This is the same shape as _list_actions_by_vin_for_fleet: the two handlers
        now agree.

        Mutation (a): revert to Attr("scope.fleet_id").is_in(...) — this test MUST
        fail because the captured FilterExpression's Attr.name will be 'scope.fleet_id'
        instead of 'fleet_id'.
        """
        import pytest
        if not hasattr(h, "_list_findings_by_vin_for_fleet"):
            pytest.skip("Skipping: not running against the findings handler")

        captured = self._capture_query_kwargs(
            h._list_findings_by_vin_for_fleet, "VIN-TEST", ["FLEET-A", "FLEET-B"]
        )

        assert "FilterExpression" in captured, (
            "Expected a FilterExpression in query kwargs; none was passed."
        )
        fe = captured["FilterExpression"]
        fe_expr = fe.get_expression()

        # Extract the Attr name from the expression values (IN expression: values[0] is Attr)
        attr_obj = fe_expr["values"][0]
        attr_name = getattr(attr_obj, "name", None)

        # Must NOT use the nested path
        assert attr_name != "scope.fleet_id", (
            f"T14.4 shape mismatch: FilterExpression attribute name is 'scope.fleet_id' "
            f"(pre-T14.4 nested path). Must be top-level 'fleet_id'.\n"
            "Fix: use Attr(\"fleet_id\").is_in(caller_fleet_ids) — the projection writes "
            "to top-level fleet_id, matching the by_fleet GSI's partition key."
        )

        # Must use the top-level attribute
        assert attr_name == "fleet_id", (
            f"T14.4 shape mismatch: FilterExpression attribute name is {attr_name!r}, "
            f"expected 'fleet_id' (top-level)."
        )



# ---------------------------------------------------------------------------
# Retail action_kinds are reachable — spec 2026-09-20-avx-acquire-configurator-target
#
# Until 2026-09-20 neither `lead_capture` nor `configurator_recommend` appeared in
# _ACTION_KINDS, so every target_system="retail" approval was rejected 400 on
# action_kind and RetailExecutor was unreachable from this route for BOTH its kinds
# — a dead executor that read as wired, in a route whose target allowlist DID list
# "retail".
#
# These assert the kinds get PAST validation rather than asserting they are present
# in a set: a membership assertion would pass against an allowlist that some later
# branch ignores. Reaching the Finding lookup is the property that matters.
#
# MUTATION-VERIFIED: removing either kind from _ACTION_KINDS fails the corresponding
# test with a 400 whose body names action_kind.
# ---------------------------------------------------------------------------

class TestRetailActionKindsReachable:
    def _approve_event(self, finding_id, body, claims=None):
        return TestApproveRoute._approve_event(self, finding_id, body, claims)

    def _assert_not_rejected_on_action_kind(self, action_kind):
        # Dispatch gate ON — these tests are about the allowlist, not the gate.
        # Without this, they would pass for the wrong reason once the gate landed.
        # _load_finding_by_id returns None → 404. A 404 proves validation was passed
        # and the handler progressed to the Finding lookup; a 400 naming action_kind
        # would mean the allowlist rejected it.
        with patch.object(h, "_load_finding_by_id", return_value=None), \
             patch.dict(os.environ, {"VSA_AVX_EXECUTOR_DISPATCH_ENABLED": "true"}, clear=False):
            event = self._approve_event(
                "FIND-00000000-0000-0000-0000-000000000001",
                {"target_system": "retail", "action_kind": action_kind},
            )
            resp = h.handler(event, None)
        body = _decode_body(resp)
        assert resp["statusCode"] != 400 or "action_kind" not in str(body.get("error", "")), (
            f"action_kind={action_kind!r} was rejected at validation "
            f"({resp['statusCode']}: {body.get('error')!r}). It is a RetailExecutor "
            "kind and must be reachable, or the executor is dead code."
        )
        return resp

    def test_lead_capture_is_an_accepted_action_kind(self):
        resp = self._assert_not_rejected_on_action_kind("lead_capture")
        assert resp["statusCode"] == 404, (
            f"expected 404 from the stubbed Finding lookup, got {resp['statusCode']} — "
            "the request stopped somewhere other than where this test intends."
        )

    def test_configurator_recommend_is_an_accepted_action_kind(self):
        resp = self._assert_not_rejected_on_action_kind("configurator_recommend")
        assert resp["statusCode"] == 404

    def test_an_unknown_action_kind_is_still_rejected(self):
        """Anti-vacuity: proves the allowlist still rejects, so the two tests above
        are not passing because validation was removed altogether."""
        with patch.object(h, "_load_finding_by_id", return_value=None):
            event = self._approve_event(
                "FIND-00000000-0000-0000-0000-000000000001",
                {"target_system": "retail", "action_kind": "trade_in_valuation"},
            )
            resp = h.handler(event, None)
        assert resp["statusCode"] == 400
        assert "action_kind" in str(_decode_body(resp).get("error", ""))



# ---------------------------------------------------------------------------
# Deploy-safety gate — _RETAIL_KINDS / VSA_AVX_EXECUTOR_DISPATCH_ENABLED
#
# Introduced while dispatch was unwired. Fix Group 35 wired dispatch; the gate now
# stands because the retail kinds are absent from the frozen action_kind enum, so
# accepting one writes a schema-invalid Action (D-G10-3). ApiAcquireLeads and
# ApiAvxFindings share one CloudFormation stack, so this gate is what makes the
# idempotency fix deployable without also shipping invalid rows.
#
# MUTATION-VERIFIED:
#   gate removed             -> test_retail_kinds_rejected_when_dispatch_unwired
#   gate made unconditional  -> TestRetailActionKindsReachable (both kinds)
#   default flipped to true  -> test_gate_defaults_closed
# ---------------------------------------------------------------------------

class TestDispatchGate:
    def _approve(self, action_kind, env=None):
        with patch.object(h, "_load_finding_by_id", return_value=None), \
             patch.dict(os.environ, env or {}, clear=False):
            return h.handler(
                TestApproveRoute._approve_event(
                    self,
                    "FIND-00000000-0000-0000-0000-000000000001",
                    {"target_system": "retail", "action_kind": action_kind},
                ),
                None,
            )

    @pytest.mark.parametrize("kind", ["lead_capture", "configurator_recommend"])
    def test_retail_kinds_rejected_when_dispatch_unwired(self, kind):
        """The whole point: with dispatch unwired, refuse rather than strand."""
        resp = self._approve(kind, {"VSA_AVX_EXECUTOR_DISPATCH_ENABLED": "false"})
        assert resp["statusCode"] == 400, (
            f"{kind} was accepted with dispatch unwired (got {resp['statusCode']}); "
            "the Action would persist as pending_execution and never execute."
        )
        assert "never run" in str(_decode_body(resp).get("error", "")), (
            "the rejection must say WHY — an operator reading a bare 400 cannot tell "
            "this from an unknown action_kind"
        )

    def test_gate_defaults_closed(self):
        """Absent env var must mean disabled. A stage not told dispatch exists has to
        assume it does not: guessing wrong strands an Action silently."""
        env = {k: v for k, v in os.environ.items()
               if k != "VSA_AVX_EXECUTOR_DISPATCH_ENABLED"}
        with patch.object(h, "_load_finding_by_id", return_value=None), \
             patch.dict(os.environ, env, clear=True):
            resp = h.handler(
                TestApproveRoute._approve_event(
                    self,
                    "FIND-00000000-0000-0000-0000-000000000001",
                    {"target_system": "retail", "action_kind": "lead_capture"},
                ),
                None,
            )
        assert resp["statusCode"] == 400, (
            "the gate defaulted OPEN with no env var set — a stage that never heard of "
            "this flag would accept un-executable actions"
        )

    def test_gate_does_not_affect_non_retail_kinds(self):
        """Anti-vacuity: the gate must not have become a blanket rejection."""
        resp = self._approve(
            "book_combined_visit", {"VSA_AVX_EXECUTOR_DISPATCH_ENABLED": "false"})
        assert resp["statusCode"] == 404, (
            f"a non-retail kind returned {resp['statusCode']} with the gate closed; "
            "expected 404 from the stubbed Finding lookup, i.e. past validation"
        )



# ---------------------------------------------------------------------------
# F4.1 — Gate-exit enum conformance (Cycle 4 Warning)
#
# The gate is the only barrier between "gate open + retail kind" and a
# schema-invalid Action written silently.  Whoever deletes the gate per
# the existing instruction must also update all three action_kind enum sites.
# This test asserts THAT PROPERTY, but note precisely HOW, because the
# distinction matters to anyone editing these tests: it reads
# action.schema.json directly and checks it against _RETAIL_KINDS whenever
# _dispatch_enabled() reports the gate open.  It does NOT drive the approve
# path and does NOT inspect a written Action.
#
# Consequence, stated here rather than only in decisions.md because this is
# where the misreading would happen: this test does NOT detect the
# enforcement block at _approve_finding being deleted while _RETAIL_KINDS and
# _dispatch_enabled() remain defined.  TestDispatchGate covers that direction
# (3 tests fail).  The two are COMPLEMENTARY, not redundant — verified both
# ways in review Cycle 5: enforcement-deleted is caught only by
# TestDispatchGate, and gate-opened-by-ambient-env is caught only by this
# test, since test_gate_defaults_closed patches the environment with
# clear=True.  Do not delete either believing the other covers it.
#
# MUTATION-VERIFIED (2026-09-21):
#   Add retail kinds to _RETAIL_KINDS gate-open path (env flag true) with
#   action.schema.json unchanged → test_gate_open_retail_action_is_schema_valid FAILS.
#   Add retail kinds to action.schema.json enum → test PASSES.
# ---------------------------------------------------------------------------

class TestF4_1_GateExitEnumConformance:
    """Gate-exit contract: opening the gate must coincide with adding retail kinds
    to all three action_kind enum sites.  This test reads action.schema.json so
    it fails if the gate is opened while the schema still lists only the original
    6 kinds — the condition that creates a silent schema-invalid write.

    Style follows TestT12_1_WriterProjectionCoupling: reads the artefact, not a
    hardcoded list, so the assertion tracks the real schema rather than a copy.

    Current state (gate CLOSED):
      _dispatch_enabled() returns False → retail kinds are rejected 400 before
      an Action is written → no schema-invalid write is possible.  The gap in the
      schema is protected by the gate.  This test PASSES.

    Mutation state (gate OPEN, schema unchanged):
      Opening the gate (VSA_AVX_EXECUTOR_DISPATCH_ENABLED=true) while action.schema.json
      still lacks the retail kinds creates a window where a retail-kind Action is
      written but schema-invalid.  This test FAILS, naming all three enum sites.
    """

    _REPO_ROOT = os.path.normpath(
        os.path.join(os.path.dirname(__file__), "..", "..", "..")
    )
    _SCHEMA_PATH = os.path.join(
        _REPO_ROOT,
        "agents", "tier2", "schemas", "action.schema.json",
    )

    def _load_action_schema_kinds(self) -> set[str]:
        """Read action_kind enum values from the live action.schema.json."""
        with open(self._SCHEMA_PATH, encoding="utf-8") as fh:
            schema = json.load(fh)
        return set(schema["properties"]["action_kind"]["enum"])

    def test_gate_open_retail_action_is_schema_valid(self):
        """Gate-open + schema-unchanged → test FAILS.  Gate-open + schema-updated → PASSES.

        When dispatch is enabled (gate open), every retail-kind Action must be a valid
        value in action.schema.json's action_kind enum, otherwise the written row is
        silently schema-invalid.

        This test PASSES today because the gate is CLOSED: no retail-kind Action
        is written regardless of the schema state, so the combination is safe.

        Mutation to verify this test grips the property:
          Open the gate (set VSA_AVX_EXECUTOR_DISPATCH_ENABLED=true globally or in the
          test env) while action.schema.json still lists only the original 6 kinds.
          This test MUST FAIL, naming all three enum sites where retail kinds must be added.
          Revert (close the gate or update the schema) to restore to PASS.
        """
        gate_open = h._dispatch_enabled()
        schema_kinds = self._load_action_schema_kinds()

        if not gate_open:
            # Gate is closed: retail kinds cannot reach the write path, so any
            # schema gap is currently protected.  Pass — but only because the gate holds.
            return

        # Gate is open: every retail kind must be in the schema enum, or a written
        # Action will have an invalid action_kind field.
        missing = sorted(h._RETAIL_KINDS - schema_kinds)
        assert not missing, (
            f"GATE-EXIT ENUM GAP: dispatch is enabled but the following retail "
            f"kind(s) are missing from action.schema.json action_kind enum:\n"
            f"  missing: {missing}\n"
            f"  current schema enum: {sorted(schema_kinds)}\n"
            "A retail-kind Action approved today is written with schema-invalid "
            "action_kind — silently invalid.  Before opening the gate (or deleting it) "
            "add both retail kinds to all three enum sites:\n"
            "  1. agents/tier2/schemas/action.schema.json        (~line 71, \"enum\")\n"
            "  2. docs/api/avx-findings-actions.openapi.yaml    (~line 1366, Action)\n"
            "  3. docs/api/avx-findings-actions.openapi.yaml    (~line 1550, ApproveRequest)"
        )


# ---------------------------------------------------------------------------
# F4.2 — _RETAIL_KINDS is pinned against retail.py:_SUPPORTED_KINDS
#
# handler.py:_RETAIL_KINDS hand-duplicates agents/executors/retail.py:_SUPPORTED_KINDS.
# A future third retail kind added to retail.py and missed in _RETAIL_KINDS would be
# ungated → silent stall.  This test pins the two sets equal.
#
# MUTATION-VERIFIED (2026-09-21):
#   Add a third kind to retail.py:_SUPPORTED_KINDS only →
#   test_retail_kinds_equals_supported_kinds FAILS with both paths in the message.
#   Revert → PASSES.
# ---------------------------------------------------------------------------

class TestF4_2_RetailKindsPinning:
    """_RETAIL_KINDS must equal retail.py:_SUPPORTED_KINDS at all times.

    Cycle 4 Suggestion 1 (taken): the two sets are hand-duplicated with no coupling
    test.  A third retail kind added to retail.py and missed in _RETAIL_KINDS would
    be ungated, producing a silent stall — the exact outcome the gate prevents.

    This test reads _SUPPORTED_KINDS from retail.py at import time rather than
    hardcoding the two current kinds, so it grips the property (equality) rather
    than the presence of the two known values.  A subset check would pass in
    exactly the direction that causes the stall (_RETAIL_KINDS ⊂ _SUPPORTED_KINDS
    with a new kind in _SUPPORTED_KINDS but not in _RETAIL_KINDS).
    """

    _RETAIL_PY_PATH = os.path.normpath(
        os.path.join(os.path.dirname(__file__), "..", "..", "..",
                     "agents", "executors", "retail.py")
    )

    def _load_supported_kinds(self) -> frozenset[str]:
        """Read _SUPPORTED_KINDS from retail.py without importing its dependencies."""
        import ast
        with open(self._RETAIL_PY_PATH, encoding="utf-8") as fh:
            source = fh.read()
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Assign)
                and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
                and node.targets[0].id == "_SUPPORTED_KINDS"
                and isinstance(node.value, ast.Call)
                and isinstance(node.value.func, ast.Name)
                and node.value.func.id == "frozenset"
            ):
                elts = node.value.args[0].elts
                return frozenset(
                    elt.value if isinstance(elt, ast.Constant) else elt.s  # py3.8+ / older
                    for elt in elts
                )
        raise AssertionError(
            f"Could not find _SUPPORTED_KINDS assignment in {self._RETAIL_PY_PATH}"
        )

    def test_retail_kinds_equals_supported_kinds(self):
        """_RETAIL_KINDS in handler.py must equal _SUPPORTED_KINDS in retail.py.

        Failure message names both file paths so the next author knows exactly
        where to look.  A subset check is intentionally NOT used: the stall
        occurs when _SUPPORTED_KINDS grows and _RETAIL_KINDS does not.
        """
        supported = self._load_supported_kinds()
        handler_retail = h._RETAIL_KINDS

        assert handler_retail == supported, (
            f"_RETAIL_KINDS mismatch between the two files:\n"
            f"  lambdas/api-avx-findings/handler.py  _RETAIL_KINDS   = {sorted(handler_retail)}\n"
            f"  agents/executors/retail.py           _SUPPORTED_KINDS = {sorted(supported)}\n"
            "Kinds in _SUPPORTED_KINDS but missing from _RETAIL_KINDS would be ungated, "
            "persisting Actions that nothing executes (silent stall).  "
            "Update _RETAIL_KINDS in handler.py to match, then re-run this test."
        )



# ---------------------------------------------------------------------------
# F11.3 (Fix Group 15) — approve reads the top-level owner_id projection
# ---------------------------------------------------------------------------
#
# Six control cases, three per owner group, per F11.3 Accept 3.  Plus two guards
# the six do not cover:
#
#   * the namespace guard — `sub` alone must NOT authorize, because the projected
#     value is a CUST- customer id and `sub` is a UUID.  Without this, a future
#     "fix" that falls back to `sub` to make something match would look correct
#     and would compare two different namespaces.
#   * an owner-scoped positive — proves the backward-compat branch is live code
#     (M-FG11-12 removes it and this is what fails).
#
# Fixture shapes match what F11.2 actually emits: VIN-scoped Findings carry
# `scope: {"vin": ...}` with NO nested owner_id (the schema's VIN branch forbids
# it) and the owner binding lives at the top level.  Per F11.3 Accept 4 there is
# deliberately no coverage of a nested `scope.owner_id` on a VIN Finding — that
# shape is unreachable in production and testing it would assert a fiction.

_FG15_OWNER_A = "CUST-004000DD"
_FG15_OWNER_B = "CUST-0040014E"

# A VIN-scoped Finding as F11.2 writes it: owner binding top-level, not nested.
_FG15_VIN_FINDING_WITH_OWNER = {
    **_SAMPLE_FINDING,
    "scope": {"vin": "VIN-FG15-001"},
    "owner_id": _FG15_OWNER_A,
}

# The unresolvable case: VIN-scoped, no projection at all.
_FG15_VIN_FINDING_NO_OWNER = {
    **_SAMPLE_FINDING,
    "scope": {"vin": "VIN-FG15-001"},
}

# _event() JSON-encodes the body itself, so this stays a dict.
# Advisory target since review Cycle 1 of Fix Groups 35/36 (W2): these classes test
# approve-scope authorization for owners, drivers and fleet operators, which is
# target-independent, and `dms` now also requires a DMS-admitted group
# (TestFG35ApproveDispatch.test_dms_refuses_callers_dms_would_refuse).
_FG15_APPROVE_BODY = {"action_kind": "acknowledge_only", "target_system": "none"}


# Security review W1: the customer-id claim is ignored unless the operator has
# attested that it is excluded from every app client's WriteAttributes. These
# tests exercise the COMPARISON logic, so they opt in explicitly. Tests for the
# fail-closed default live in TestCustomerIdClaimTrust below.
_TRUSTED_ENV = {"AVX_CUSTOMER_ID_CLAIM_TRUSTED": "true"}

# All three standing claims trusted. Every custom:* claim used for authorization is
# self-writable under default WriteAttributes, so each is gated independently and
# tests of the COMPARISON logic must opt into all of them (review cycle 1 C2).
_ALL_STANDING_TRUSTED = {
    "AVX_CUSTOMER_ID_CLAIM_TRUSTED": "true",
    "AVX_VEHICLE_ID_CLAIM_TRUSTED": "true",
    "AVX_FLEET_IDS_CLAIM_TRUSTED": "true",
}


class TestApproveOwnerBinding:
    """F11.3 — the six control cases, plus the namespace and backward-compat guards."""

    def _approve_event(self, finding_id, claims, body=None):
        return _event(
            method="POST",
            resource="/findings/{finding_id}/approve",
            path_params={"finding_id": finding_id},
            claims=claims,
            body=body or _FG15_APPROVE_BODY,
        )

    def _approve(self, finding, claims, body=None):
        mock_table = MagicMock()
        mock_table.put_item.return_value = {}
        with patch.object(h, "_load_finding_by_id", return_value=finding), \
             patch.dict(os.environ, _TRUSTED_ENV), \
             patch.object(h, "_get_ddb") as mock_ddb:
            mock_table = MagicMock()
            mock_table.put_item.return_value = {}
            mock_ddb.return_value.Table.return_value = mock_table
            return h.handler(self._approve_event(finding["finding_id"], claims, body), None)

    # ── Positive: projection present, caller's customer id matches ──────────
    @pytest.mark.parametrize("group", ["vehicle-owner", "consumer"])
    def test_positive_matching_customer_id_returns_200(self, group):
        """M-FG11-8 target: reverting to the nested read MUST fail this."""
        resp = self._approve(
            _FG15_VIN_FINDING_WITH_OWNER,
            _claims(sub="cognito-uuid-1", groups=[group], customer_id=_FG15_OWNER_A),
        )
        assert resp["statusCode"] == 200, (
            f"{group} owning the finding must be able to approve it; got {resp}"
        )
        assert "action_id" in _decode_body(resp)

    # ── Negative 1: owner mismatch ──────────────────────────────────────────
    @pytest.mark.parametrize("group", ["vehicle-owner", "consumer"])
    def test_negative_owner_mismatch_returns_403(self, group):
        """M-FG11-9 and M-FG11-11 target."""
        resp = self._approve(
            _FG15_VIN_FINDING_WITH_OWNER,
            _claims(sub="cognito-uuid-2", groups=[group], customer_id=_FG15_OWNER_B),
        )
        assert resp["statusCode"] == 403, "a different owner must never approve"

    @pytest.mark.parametrize("group", ["vehicle-owner", "consumer"])
    def test_negative_substring_owner_is_not_a_match(self, group):
        """M-FG11-11 target specifically — equality, not containment.

        `CUST-004000DD-EXTRA` contains the real owner id as a prefix, so an
        implementation using `in` rather than `==` would authorize it.
        """
        resp = self._approve(
            _FG15_VIN_FINDING_WITH_OWNER,
            _claims(
                sub="cognito-uuid-3", groups=[group],
                customer_id=f"{_FG15_OWNER_A}-EXTRA",
            ),
        )
        assert resp["statusCode"] == 403, (
            "a superstring of the owner id must not authorize — equality only"
        )

    # ── Negative 2: unresolvable owner (no projection) ──────────────────────
    @pytest.mark.parametrize("group", ["vehicle-owner", "consumer"])
    def test_negative_unresolvable_owner_returns_403(self, group):
        """M-FG11-9 target.

        This is F11.2's unresolvable-VIN case. It MUST stay denied: "we could not
        determine the owner" is not "anyone may approve".

        NOT an M-FG11-10 target, despite what `tasks.md` originally specified.
        Re-derived by running that mutation and reading which tests fail: the
        empty-string sentinel is caught by
        `test_negative_empty_customer_id_claim_returns_403`, while this test stays
        green. Corrected in review cycle 2 after cycle 1's W3 fixed only the
        `decisions.md` copy of the same false claim.
        """
        resp = self._approve(
            _FG15_VIN_FINDING_NO_OWNER,
            _claims(sub="cognito-uuid-1", groups=[group], customer_id=_FG15_OWNER_A),
        )
        assert resp["statusCode"] == 403, (
            "a VIN Finding with no owner projection must not be approvable"
        )

    @pytest.mark.parametrize("group", ["vehicle-owner", "consumer"])
    def test_negative_empty_customer_id_claim_returns_403(self, group):
        """M-FG11-10 target — an empty claim must not match an absent projection."""
        resp = self._approve(
            _FG15_VIN_FINDING_NO_OWNER,
            _claims(sub="cognito-uuid-1", groups=[group], customer_id=""),
        )
        assert resp["statusCode"] == 403

    # ── The namespace guard ─────────────────────────────────────────────────
    @pytest.mark.parametrize("group", ["vehicle-owner", "consumer"])
    def test_sub_alone_does_not_authorize_a_vin_finding(self, group):
        """`sub` is a UUID; the projection is a CUST- id. They must not be compared.

        Guards the (d) seam: a future change that falls back to `sub` when
        `custom:customerId` is absent would make this pass, and would be
        comparing two unrelated identity namespaces.
        """
        resp = self._approve(
            _FG15_VIN_FINDING_WITH_OWNER,
            # No customer_id claim at all — exactly today's live pool.
            _claims(sub=_FG15_OWNER_A, groups=[group]),
        )
        assert resp["statusCode"] == 403, (
            "sub must not be compared against the projected customer id, even "
            "when its value happens to look like one"
        )

    def test_caller_customer_id_returns_none_without_the_claim(self):
        """The (d) seam is inert today, and explicitly so."""
        with patch.dict(os.environ, _TRUSTED_ENV):
            assert h._caller_customer_id(_claims(sub="cognito-uuid-1", groups=["consumer"])) is None
            assert h._caller_customer_id({"custom:customerId": "   "}) is None
            assert h._caller_customer_id({"custom:customerId": 12345}) is None
            assert h._caller_customer_id({"custom:customerId": _FG15_OWNER_A}) == _FG15_OWNER_A

    # ── Owner-scoped backward compatibility ─────────────────────────────────
    def test_owner_scoped_finding_still_approves_via_sub(self):
        """M-FG11-12 target: removing the fallback MUST fail this.

        An owner-scoped Finding's `scope.owner_id` is written *from* `sub` by the
        executors, so `sub` is the correct comparand there and this branch is not
        dead code.
        """
        owner_scoped = {
            **_SAMPLE_FINDING,
            "scope": {"owner_id": "cognito-uuid-9"},
        }
        owner_scoped.pop("owner_id", None)
        resp = self._approve(
            owner_scoped,
            _claims(sub="cognito-uuid-9", groups=["consumer"]),
            _ADVISORY_APPROVE_BODY,  # dms needs a VIN-scoped Finding (D-G10-2)
        )
        assert resp["statusCode"] == 200, (
            "owner-scoped Findings must keep working via sub (F11.3 Accept 2)"
        )

    def test_owner_scoped_finding_denies_a_different_sub(self):
        owner_scoped = {
            **_SAMPLE_FINDING,
            "scope": {"owner_id": "cognito-uuid-9"},
        }
        owner_scoped.pop("owner_id", None)
        resp = self._approve(
            owner_scoped,
            _claims(sub="cognito-uuid-OTHER", groups=["consumer"]),
        )
        assert resp["statusCode"] == 403

    # ── Privileged roles are unaffected ─────────────────────────────────────
    def test_platform_admin_still_bypasses(self):
        resp = self._approve(
            _FG15_VIN_FINDING_NO_OWNER,
            _claims(sub="admin-1", groups=["platform-admin"]),
        )
        assert resp["statusCode"] == 200

    def test_projected_owner_id_is_stripped_from_the_response(self):
        """T11.1's response-boundary strip must cover F11.2's new attribute.

        The projection is a storage-layer denormalisation; the frozen Finding
        contract forbids it at the root, so it must not escape in a response.
        """
        with patch.object(h, "_list_findings_by_owner",
                          return_value=([dict(_FG15_VIN_FINDING_WITH_OWNER)], None)):
            resp = h.handler(
                _event(
                    method="GET",
                    resource="/findings/owner/{owner_id}",
                    path_params={"owner_id": "cognito-uuid-1"},
                    claims=_claims(sub="cognito-uuid-1", groups=["consumer"]),
                ),
                None,
            )
        assert resp["statusCode"] == 200
        for item in _decode_body(resp)["findings"]:
            assert "owner_id" not in item, (
                "top-level owner_id must be stripped at the response boundary"
            )



# ---------------------------------------------------------------------------
# Security review W1 — the customer-id claim is untrusted by default
# ---------------------------------------------------------------------------
#
# A `custom:*` Cognito attribute is self-writable under the app client's default
# (unset) WriteAttributes. Verified live on this pool 2026-08-03 against
# `custom:role` — see
# lambdas/api-assistant-chat/tests/test_persona_map.py
#   ::test_unknown_role_does_not_escalate_to_fleet_driver
# where `update-user-attributes --access-token <own>` succeeded and escalated a
# consumer's persona.
#
# The same shape here is worse: `custom:customerId` is compared against the
# projected `owner_id` to authorize approving a Finding, so a self-writable value
# means any authenticated caller can approve any owner's Findings — the exact
# cross-tenant Action creation W-SEC-1 closed.
#
# `custom:customerId` must be mutable=True (existing users need backfilling), and
# mutable + default WriteAttributes IS the vulnerable state. So the control cannot
# be immutability; it is "excluded from every app client's WriteAttributes" — a
# CMS-repo property CVX cannot assert against. Hence a fail-closed opt-in.


class TestCustomerIdClaimTrust:
    """The claim is ignored unless an operator explicitly attests it is safe."""

    _CLAIMS = {"custom:customerId": _FG15_OWNER_A, "sub": "cognito-uuid-1",
               "cognito:groups": json.dumps(["vehicle-owner"])}

    def test_claim_is_ignored_when_the_flag_is_absent(self):
        """Default-off. The dangerous state must be unreachable by omission."""
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("AVX_CUSTOMER_ID_CLAIM_TRUSTED", None)
            assert h._caller_customer_id(self._CLAIMS) is None

    @pytest.mark.parametrize("value", ["", "false", "False", "0", "no", "TRUE ", "yes", "1"])
    def test_only_the_exact_string_true_opts_in(self, value):
        """Anything other than "true" (case-insensitive, trimmed) stays closed.

        "1" and "yes" are deliberately NOT accepted: a truthiness check here would
        make a stray value an accidental opt-in, and this flag's whole purpose is
        that enabling it is a deliberate act.
        """
        with patch.dict(os.environ, {"AVX_CUSTOMER_ID_CLAIM_TRUSTED": value}):
            if value.strip().lower() == "true":
                assert h._caller_customer_id(self._CLAIMS) == _FG15_OWNER_A
            else:
                assert h._caller_customer_id(self._CLAIMS) is None, (
                    f"{value!r} must not be treated as an opt-in"
                )

    def test_flag_true_trusts_the_claim(self):
        with patch.dict(os.environ, {"AVX_CUSTOMER_ID_CLAIM_TRUSTED": "true"}):
            assert h._caller_customer_id(self._CLAIMS) == _FG15_OWNER_A

    @pytest.mark.parametrize("group", ["vehicle-owner", "consumer"])
    def test_approve_is_denied_when_the_claim_is_untrusted(self, group):
        """End-to-end: the whole VIN-scope approve path stays 403 without the flag.

        This is the assertion that actually matters — a caller presenting a
        perfectly matching `custom:customerId` must still be denied while the
        attribute's write-protection is unattested.
        """
        with patch.object(h, "_load_finding_by_id",
                          return_value=_FG15_VIN_FINDING_WITH_OWNER), \
             patch.dict(os.environ, {}, clear=False), \
             patch.object(h, "_get_ddb") as mock_ddb:
            os.environ.pop("AVX_CUSTOMER_ID_CLAIM_TRUSTED", None)
            mock_table = MagicMock()
            mock_table.put_item.return_value = {}
            mock_ddb.return_value.Table.return_value = mock_table
            resp = h.handler(
                _event(
                    method="POST",
                    resource="/findings/{finding_id}/approve",
                    path_params={"finding_id": _FG15_VIN_FINDING_WITH_OWNER["finding_id"]},
                    claims=_claims(sub="cognito-uuid-1", groups=[group],
                                   customer_id=_FG15_OWNER_A),
                    body=_FG15_APPROVE_BODY,
                ),
                None,
            )
        assert resp["statusCode"] == 403, (
            "a matching customer-id claim must NOT authorize while the attribute's "
            "WriteAttributes exclusion is unattested — it is self-writable by default"
        )

    def test_the_writeattributes_requirement_is_documented_at_the_seam(self):
        """The reason for the flag must survive next to the flag.

        A flag whose rationale lives only in a spec file gets set by someone who
        never read it. This pins the three load-bearing facts in-source: the
        attribute is self-writable by default, the 2026-08-03 precedent, and what
        setting the flag actually attests to.
        """
        import inspect
        src = inspect.getsource(h)
        for phrase in ("WriteAttributes", "2026-08-03", "self-writable"):
            assert phrase in src, (
                f"the W1 rationale lost {phrase!r} from handler.py — the flag "
                "becomes a switch with no stated precondition"
            )



# ---------------------------------------------------------------------------
# Fix Group 25 — standing is a set: owner / fleet_driver / fleet_manager
# ---------------------------------------------------------------------------
#
# Per docs/identity-and-standing-contract.md § 3. Before this group the VIN-scope
# branch could only ask "are you the party of record?", so two standings whose
# claims were already populated could not be used:
#
#   * fleet_driver  — custom:vehicleId names the exact vehicle
#   * fleet_manager — custom:fleetIds contains the vehicle's fleet
#
# The pre-existing fleet-operator check does implement fleet standing, but only for
# FLEET-scoped Findings, and Agent 2 emits VIN-scoped exclusively — so it never
# fired. Same defect as the owner path, one row over.
#
# Note on the fixtures: custom:vehicleId carries a VEH-* id while the Finding's
# scope carries a VIN. They are different namespaces (contract § 2), which is why
# the writer projects vehicle_id separately and the check compares against that
# rather than against the VIN.

_FG25_VEHICLE_ID = "VEH-MRDN-0015"
_FG25_VIN = "MRDN0000000000015"
_FG25_FLEET_ID = "FLEET-MERIDIAN-ONBOARD"

# A VIN-scoped Finding carrying all three standing facts, as the writer now emits.
_FG25_FINDING_ALL_FACTS = {
    **_SAMPLE_FINDING,
    "scope": {"vin": _FG25_VIN},
    "owner_id": _FG15_OWNER_A,
    "fleet_id": _FG25_FLEET_ID,
    "vehicle_id": _FG25_VEHICLE_ID,
}


class TestVehicleStandingSet:
    """Three independent standing routes over one vehicle."""

    def _approve(self, finding, claims, env=None):
        with patch.object(h, "_load_finding_by_id", return_value=finding), \
             patch.dict(os.environ, env if env is not None else _ALL_STANDING_TRUSTED), \
             patch.object(h, "_get_ddb") as mock_ddb:
            mock_table = MagicMock()
            mock_table.put_item.return_value = {}
            mock_ddb.return_value.Table.return_value = mock_table
            return h.handler(
                _event(
                    method="POST",
                    resource="/findings/{finding_id}/approve",
                    path_params={"finding_id": finding["finding_id"]},
                    claims=claims,
                    body=_FG15_APPROVE_BODY,
                ),
                None,
            )

    # ── fleet_driver standing ───────────────────────────────────────────────
    def test_driver_of_this_vehicle_may_approve(self):
        """The primary AVX use case: a driver acting on their own vehicle's finding.

        Deliberately carries NO customer claim — this proves the driver route is
        independent of owner standing rather than passing through it.
        """
        claims = _claims(sub="cognito-uuid-driver", groups=["fleet-driver"])
        claims["custom:vehicleId"] = _FG25_VEHICLE_ID
        resp = self._approve(_FG25_FINDING_ALL_FACTS, claims)
        assert resp["statusCode"] == 200, (
            f"a driver assigned to this vehicle must be able to approve; got {resp}"
        )

    def test_driver_claim_that_is_a_substring_is_denied(self):
        """Equality, not containment. `VEH-MRDN-001` is a PREFIX of the projected
        `VEH-MRDN-0015`, so an implementation using `in` would authorize it.

        Added after mutation M-FG25-5 substituted `in` for `==` and no existing
        test noticed — every other fixture claim happened not to be a substring.
        Same class as M-FG11-11 on the owner route.
        """
        claims = _claims(sub="cognito-uuid-driver", groups=["fleet-driver"])
        claims["custom:vehicleId"] = _FG25_VEHICLE_ID[:-1]  # VEH-MRDN-001
        assert self._approve(_FG25_FINDING_ALL_FACTS, claims)["statusCode"] == 403, (
            "a substring of the projected vehicle id must not authorize"
        )

    def test_driver_claim_that_is_a_superstring_is_denied(self):
        """The other direction, in case the operands are compared the other way."""
        claims = _claims(sub="cognito-uuid-driver", groups=["fleet-driver"])
        claims["custom:vehicleId"] = _FG25_VEHICLE_ID + "-EXTRA"
        assert self._approve(_FG25_FINDING_ALL_FACTS, claims)["statusCode"] == 403

    def test_driver_of_a_different_vehicle_is_denied(self):
        claims = _claims(sub="cognito-uuid-driver", groups=["fleet-driver"])
        claims["custom:vehicleId"] = "VEH-SOMEONE-ELSE-9999"
        assert self._approve(_FG25_FINDING_ALL_FACTS, claims)["statusCode"] == 403

    def test_driver_claim_does_not_match_against_the_vin(self):
        """A claim holding the VIN must not satisfy the vehicle_id comparison.

        Guards the namespace boundary: if the check compared against the Finding's
        VIN instead of the projected vehicle_id, this would wrongly pass.
        """
        claims = _claims(sub="cognito-uuid-driver", groups=["fleet-driver"])
        claims["custom:vehicleId"] = _FG25_VIN
        assert self._approve(_FG25_FINDING_ALL_FACTS, claims)["statusCode"] == 403

    def test_driver_is_denied_when_vehicle_id_was_not_projected(self):
        finding = {k: v for k, v in _FG25_FINDING_ALL_FACTS.items() if k != "vehicle_id"}
        claims = _claims(sub="cognito-uuid-driver", groups=["fleet-driver"])
        claims["custom:vehicleId"] = _FG25_VEHICLE_ID
        assert self._approve(finding, claims)["statusCode"] == 403, (
            "an unresolved vehicle_id must deny — absent is not a wildcard"
        )

    @pytest.mark.parametrize("bad", ["", "   "])
    def test_blank_driver_claim_never_matches(self, bad):
        claims = _claims(sub="cognito-uuid-driver", groups=["fleet-driver"])
        claims["custom:vehicleId"] = bad
        assert self._approve(_FG25_FINDING_ALL_FACTS, claims)["statusCode"] == 403

    @pytest.mark.parametrize("blank_claim", ["", "   "])
    @pytest.mark.parametrize("blank_projection", ["", "   "])
    def test_blank_claim_never_matches_a_blank_projection(self, blank_claim, blank_projection):
        """W2: the guard the whole suite could not see.

        ``test_blank_driver_claim_never_matches`` above pairs a blank claim with a
        VALID projection, so it passes even with BOTH emptiness checks removed. This
        pairs blank against blank — the only shape that distinguishes them. With both
        guards gone, ``"   ".strip() == ""`` matches a ``vehicle_id: ""`` row and
        approves a caller with no relation to the vehicle.

        The blank projection is not writer-reachable, but the handler reads the table
        as a separate trust boundary — a backfill or console edit can put it there.
        """
        finding = {**_FG25_FINDING_ALL_FACTS, "vehicle_id": blank_projection}
        claims = _claims(sub="cognito-uuid-nobody", groups=["fleet-driver"])
        claims["custom:vehicleId"] = blank_claim
        assert self._approve(finding, claims)["statusCode"] == 403, (
            "a blank claim must never match a blank projection — emptiness is not identity"
        )

    @pytest.mark.parametrize("group", ["consumer", "vehicle-owner", "fleet-driver"])
    def test_vin_branch_fleet_standing_needs_the_fleet_operator_group(self, group):
        """Flipped 2026-09-26 (spec 2026-09-25-avx-own-vehicle-findings, security review
        Cycle 1 W1). This test used to pin the opposite: a ``consumer`` with a matching
        ``custom:fleetIds`` got fleet_manager standing on approve, while every read
        route requires ``fleet-operator`` for fleet-wide Findings. That let a caller
        approve a Finding no read route returns to it. fleet_manager standing on the VIN
        branch now needs the ``fleet-operator`` group; the positive is
        ``test_operator_of_this_vehicles_fleet_may_approve``.
        """
        claims = _claims(sub=f"cognito-uuid-{group}", groups=[group],
                         fleet_ids=[_FG25_FLEET_ID])
        assert self._approve(_FG25_FINDING_ALL_FACTS, claims)["statusCode"] == 403, (
            f"a {group} with a matching fleet claim must not get fleet_manager standing "
            "on approve without the fleet-operator group"
        )

    def test_group_outside_the_standing_set_is_denied_despite_every_claim(self):
        """The counterpart: the outer group gate is still load-bearing.

        Without this, the test above could be misread as "claims alone suffice".
        """
        claims = _claims(sub="cognito-uuid-tech", groups=["technician"],
                         fleet_ids=[_FG25_FLEET_ID])
        claims["custom:vehicleId"] = _FG25_VEHICLE_ID
        claims["custom:customerId"] = _FG15_OWNER_A
        assert self._approve(_FG25_FINDING_ALL_FACTS, claims)["statusCode"] == 403


    # ── fleet_manager standing ──────────────────────────────────────────────
    def test_operator_of_this_vehicles_fleet_may_approve(self):
        """Fleet standing on a VIN-scoped Finding — previously unreachable."""
        resp = self._approve(
            _FG25_FINDING_ALL_FACTS,
            _claims(sub="cognito-uuid-op", groups=["fleet-operator"],
                    fleet_ids=[_FG25_FLEET_ID, "FLEET-OTHER"]),
        )
        assert resp["statusCode"] == 200, (
            f"a fleet operator whose fleet contains this vehicle must approve; got {resp}"
        )

    def test_operator_of_a_different_fleet_is_denied(self):
        assert self._approve(
            _FG25_FINDING_ALL_FACTS,
            _claims(sub="cognito-uuid-op", groups=["fleet-operator"],
                    fleet_ids=["FLEET-NOT-THIS-ONE"]),
        )["statusCode"] == 403

    def test_operator_is_denied_when_fleet_id_was_not_projected(self):
        finding = {k: v for k, v in _FG25_FINDING_ALL_FACTS.items() if k != "fleet_id"}
        assert self._approve(
            finding,
            _claims(sub="cognito-uuid-op", groups=["fleet-operator"],
                    fleet_ids=[_FG25_FLEET_ID]),
        )["statusCode"] == 403

    # ── the non-implications (contract § 3.1) ───────────────────────────────
    def test_driver_standing_does_not_confer_owner_standing(self):
        """A driver must not inherit the owning party's rights.

        Probe: a Finding with an owner_id but NO vehicle_id and NO fleet_id. The
        caller holds a driver claim naming a vehicle, and no customer claim. If
        driver standing leaked into the owner route — e.g. by resolving the party
        from the driven vehicle, which the contract § 3.1 explicitly forbids
        because it is *possible* — this would pass.
        """
        owner_only = {k: v for k, v in _FG25_FINDING_ALL_FACTS.items()
                      if k not in ("vehicle_id", "fleet_id")}
        claims = _claims(sub="cognito-uuid-driver", groups=["fleet-driver"])
        claims["custom:vehicleId"] = _FG25_VEHICLE_ID
        assert self._approve(owner_only, claims)["statusCode"] == 403, (
            "driving a vehicle must not grant the owning party's standing"
        )

    def test_owner_standing_does_not_confer_driver_standing(self):
        """The converse: a party owning the vehicle is not its assigned operator.

        Probe: a Finding with vehicle_id but no owner_id. The caller holds a
        matching customer claim and no vehicle claim.
        """
        vehicle_only = {k: v for k, v in _FG25_FINDING_ALL_FACTS.items()
                        if k not in ("owner_id", "fleet_id")}
        assert self._approve(
            vehicle_only,
            _claims(sub="cognito-uuid-1", groups=["vehicle-owner"],
                    customer_id=_FG15_OWNER_A),
        )["statusCode"] == 403

    def test_no_projected_facts_at_all_denies_every_group(self):
        """Contract § 6: absence of all standing facts MUST deny."""
        bare = {k: v for k, v in _FG25_FINDING_ALL_FACTS.items()
                if k not in ("owner_id", "fleet_id", "vehicle_id")}
        for group in sorted(h._VEHICLE_STANDING_GROUPS):
            claims = _claims(sub="s", groups=[group], customer_id=_FG15_OWNER_A,
                             fleet_ids=[_FG25_FLEET_ID])
            claims["custom:vehicleId"] = _FG25_VEHICLE_ID
            assert self._approve(bare, claims)["statusCode"] == 403, (
                f"{group} approved a Finding with no standing facts projected"
            )


class TestServiceAdvisorClaimArmIsGone:
    """C3: `custom:role == "service-advisor"` no longer grants approve.

    It used to grant unconditionally, with no group membership, on any Finding —
    including one carrying no standing facts and a VIN the caller has no relation
    to. `custom:role` is the attribute proven self-writable on the CMS pool
    2026-08-03, so this nullified every standing route.

    Deleted rather than gated because it is vestigial: zero users across both
    pools carry the value (measured 2026-09-23). See
    `issues/2026-09-23-avx-custom-role-claim-unconditional-approve/`.
    """

    def _approve(self, finding, claims):
        with patch.object(h, "_load_finding_by_id", return_value=finding), \
             patch.dict(os.environ, _ALL_STANDING_TRUSTED), \
             patch.object(h, "_get_ddb") as mock_ddb:
            mock_table = MagicMock()
            mock_table.put_item.return_value = {}
            mock_ddb.return_value.Table.return_value = mock_table
            return h.handler(
                _event(
                    method="POST",
                    resource="/findings/{finding_id}/approve",
                    path_params={"finding_id": finding["finding_id"]},
                    claims=claims,
                    body=_FG15_APPROVE_BODY,
                ),
                None,
            )

    def _bare_finding(self):
        """A Finding with NO standing facts projected and an unrelated VIN."""
        return {
            **_SAMPLE_FINDING,
            "scope": {"vin": "UNRELATED00000001"},
        }

    def test_role_claim_alone_no_longer_approves(self):
        claims = _claims(sub="cognito-uuid-forger", groups=[])
        claims["custom:role"] = "service-advisor"
        assert self._approve(self._bare_finding(), claims)["statusCode"] == 403, (
            "a self-writable custom:role must not grant approve without group membership"
        )

    def test_role_claim_does_not_rescue_a_non_standing_group(self):
        """Belt-and-braces: the claim must not top up an insufficient group either."""
        claims = _claims(sub="cognito-uuid-forger", groups=["fleet-guest"])
        claims["custom:role"] = "service-advisor"
        assert self._approve(self._bare_finding(), claims)["statusCode"] == 403

    def test_real_service_advisor_group_still_approves(self):
        """The live path is unaffected — the one real advisor is group-provisioned.

        This is the test that would fail if the deletion had been over-broad.
        """
        claims = _claims(sub="cognito-uuid-advisor", groups=["service-advisor"])
        assert self._approve(self._bare_finding(), claims)["statusCode"] == 200

def _referenced_identifiers(source: str) -> set[str]:
    """Identifiers a module references in code: imports, attributes, names, str keys.

    Extracted from the guard below so the collection logic is directly testable.
    Review cycle 2 (W3): while the guard *runs*, it runs against a clean
    `handler.py`, so disabling any single collection branch left it green — the
    collector was unfalsifiable at exactly the spot cycle 1 had just repaired.
    `test_forbidden_read_collector_sees_every_import_form` now exercises each branch
    against synthetic source.
    """
    import ast

    referenced: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Name):
            referenced.add(node.id)
        elif isinstance(node, ast.Attribute):
            referenced.add(node.attr)
        elif isinstance(node, ast.alias):
            referenced.add(node.name.split(".")[0])
            if node.asname:
                referenced.add(node.asname)
        elif isinstance(node, ast.ImportFrom):
            # Review cycle 1: ImportFrom.module is a plain str and was never
            # collected, so `from owner_resolver import anything_new` evaded the
            # guard entirely — and handler.py already uses that import form.
            if node.module:
                referenced.add(node.module)
                referenced.update(node.module.split("."))
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            referenced.add(node.value)
    return referenced


@pytest.mark.parametrize("source,expected", [
    ("import owner_resolver", "owner_resolver"),
    ("import agents.tier2.owner_resolver", "agents"),
    ("import owner_resolver as orz", "orz"),
    ("from owner_resolver import resolve_owner_id", "resolve_owner_id"),
    # The cycle-1 escapes. Each requires the ImportFrom.module branch.
    ("from owner_resolver import a_brand_new_function", "owner_resolver"),
    ("from owner_resolver import StandingFacts", "owner_resolver"),
    ("from agents.tier2.owner_resolver import anything", "owner_resolver"),
    ("from .owner_resolver import anything", "owner_resolver"),
    # Non-import surfaces the guard also depends on.
    ("x = client.get_item(TableName=CMS_VEHICLES_TABLE)", "CMS_VEHICLES_TABLE"),
    ("row.get('sold_to')", "sold_to"),
    # Review cycle 3 (W2): reachable ONLY via the ast.Attribute branch. Without these
    # two, that branch could be deleted with this test and the real guard both green —
    # the same unfalsifiable state W3 was raised about, inside the fix for W3. The
    # cases above do not cover it: CMS_VEHICLES_TABLE is an ast.Name and 'sold_to'
    # there is a Constant, while the attribute names (`get_item`, `get`) are asserted
    # nowhere.
    ("vehicles.sold_to", "sold_to"),
    ("x = row.resolve_owner_id", "resolve_owner_id"),
])
def test_forbidden_read_collector_sees_every_import_form(source, expected):
    """W3: makes the collector falsifiable.

    Disabling the ImportFrom.module branch flips the four `owner_resolver` cases to
    failing; dropping the alias, Attribute, or Constant branch flips the others.
    Review cycle 3 verified each of the six branches individually — removing any one
    now fails at least one case. Before the last two cases were added, the
    `ast.Attribute` branch was the exception: it could be deleted with everything
    green, so the docstring's claim of full coverage was true of five branches, not
    six.

    Without this test, the cycle-1 ImportFrom fix could be reverted with the whole
    suite green.
    """
    assert expected in _referenced_identifiers(source), (
        f"collector missed {expected!r} in {source!r} — a forbidden read in this "
        "form would evade test_approve_scope_never_reads_the_cms_registry"
    )


def test_approve_scope_never_reads_the_cms_registry():
    """Structural pin on contract § 3.1 and § 6.

    Two invariants at once. **§ 6**: standing facts are projected at write time and
    intersected at read time — the authorizer must not put a cross-module CMS read
    on the authorization hot path. **§ 3.1**: the forbidden
    "derive the party from the driven vehicle" implementation NEEDS that read, so
    forbidding the read removes the most likely accidental route to it.

    **Scope of protection, stated honestly** (review cycle 1, W1). This is a denylist
    over an AST walk. It catches every plain and `from`-import form, aliased imports,
    attribute access, and string-literal keys. It does NOT catch deliberate
    obfuscation — `importlib.import_module("owner_" + "resolver")` and an attribute
    name assembled via `"".join(...)` both evade it, by construction. The threat model
    is accidental reintroduction by a future editor, not an adversary inside the repo.
    An earlier version of this docstring claimed it "forecloses the whole class", which
    overstated it.

    This is why the check exists structurally: a value-level test cannot
    distinguish "did not derive the party" from "derived it and it happened not to
    match", which is exactly why mutation M-FG25-6 originally slipped past.

    Checked against the **AST**, not the source text. A raw-text scan fails on the
    prose in `_CUSTOMER_ID_CLAIM`'s comment, which legitimately explains that the
    projected value's provenance is CMS `sold_to` — documenting a dependency is not
    having one, and a check that cannot tell them apart would force the comment to
    be deleted to stay green.
    """
    import pathlib

    referenced = _referenced_identifiers(pathlib.Path(h.__file__).read_text())

    for forbidden in ("owner_resolver", "resolve_standing_facts", "resolve_owner_id",
                      "CMS_VEHICLES_TABLE", "sold_to"):
        assert forbidden not in referenced, (
            f"handler.py references {forbidden!r} in code. The approve path must "
            "authorize from PROJECTED facts only — a CMS read here would both add a "
            "hot-path dependency (contract § 6) and enable deriving owner standing "
            "from the driven vehicle, which § 3.1 forbids."
        )

    # Non-vacuity: the AST walk must actually be finding identifiers, or the loop
    # above passes against an empty set and proves nothing.
    assert "_caller_customer_id" in referenced, (
        "the AST walk found no known identifier — it is not inspecting the module"
    )


def test_action_kind_enum_is_fully_classified():
    """A new action_kind must be classified operational-or-commercial deliberately.

    Contract § 3.2 gives `fleet_driver` operational approve but NOT commercial.
    Every kind in the schema is operational today, so the split has no live
    consequence — and it acquires one the moment a commercial kind is added.

    This fails when the schema gains a kind absent from
    `_OPERATIONAL_ACTION_KINDS`, forcing whoever adds it to decide rather than
    inheriting driver-approvable by default. Reads the schema rather than a copied
    list, so it cannot drift from the real enum.
    """
    import json
    import pathlib

    schema = json.loads(
        (pathlib.Path(__file__).resolve().parents[3]
         / "agents" / "tier2" / "schemas" / "action.schema.json").read_text()
    )
    enum = set(schema["properties"]["action_kind"]["enum"])
    assert enum, "could not read the action_kind enum from the schema"

    unclassified = enum - h._OPERATIONAL_ACTION_KINDS
    assert not unclassified, (
        f"action_kind(s) {sorted(unclassified)} are not classified. Per the "
        "identity-and-standing contract § 3.2, decide whether each is OPERATIONAL "
        "(a fleet_driver may approve it) or COMMERCIAL (they may not). If "
        "commercial, the VIN-scope fleet_driver route in _check_approve_scope must "
        "gate on action_kind before this ships."
    )

    stale = h._OPERATIONAL_ACTION_KINDS - enum
    assert not stale, (
        f"{sorted(stale)} classified but absent from the schema — remove them so "
        "the set cannot silently license a kind that no longer exists"
    )



# ---------------------------------------------------------------------------
# Fix Group 26 (review cycle 1 C2) — every standing claim is gated independently
# ---------------------------------------------------------------------------
#
# Fix Group 24 gated `custom:customerId` because a `custom:*` attribute is
# self-writable under the app client's default (unset) WriteAttributes. Fix Group
# 25 then made `custom:vehicleId` and `custom:fleetIds` load-bearing for approve
# with no gate — so with the env unset (the DEPLOYED DEFAULT) a forged vehicle or
# fleet claim approved while a forged customer claim denied.
#
# contract § 5.1, authored in the same session, states the requirement applies to
# every custom:* claim used for authorization and that the exposure exists NOW.
# FG25 is what made it exist.


class TestStandingClaimTrustGates:
    """Each standing claim must be independently gated, and default-off."""

    def _approve(self, finding, claims, env):
        with patch.object(h, "_load_finding_by_id", return_value=finding), \
             patch.dict(os.environ, {}, clear=False), \
             patch.object(h, "_get_ddb") as mock_ddb:
            for k in ("AVX_CUSTOMER_ID_CLAIM_TRUSTED", "AVX_VEHICLE_ID_CLAIM_TRUSTED",
                      "AVX_FLEET_IDS_CLAIM_TRUSTED"):
                os.environ.pop(k, None)
            os.environ.update(env)
            mock_table = MagicMock()
            mock_table.put_item.return_value = {}
            mock_ddb.return_value.Table.return_value = mock_table
            return h.handler(
                _event(
                    method="POST",
                    resource="/findings/{finding_id}/approve",
                    path_params={"finding_id": finding["finding_id"]},
                    claims=claims,
                    body=_FG15_APPROVE_BODY,
                ),
                None,
            )

    def _driver_claims(self):
        c = _claims(sub="cognito-uuid-driver", groups=["fleet-driver"])
        c["custom:vehicleId"] = _FG25_VEHICLE_ID
        return c

    def _operator_claims(self):
        return _claims(sub="cognito-uuid-op", groups=["fleet-operator"],
                       fleet_ids=[_FG25_FLEET_ID])

    # ── default-off: the deployed state ─────────────────────────────────────
    def test_driver_route_denied_with_no_flags(self):
        """The exact probe from review cycle 1 C2 — forged vehicle claim, env unset."""
        assert self._approve(_FG25_FINDING_ALL_FACTS, self._driver_claims(), {})[
            "statusCode"] == 403, (
            "an unattested custom:vehicleId must not grant driver standing — it is "
            "self-writable under default WriteAttributes"
        )

    def test_fleet_route_denied_with_no_flags(self):
        assert self._approve(_FG25_FINDING_ALL_FACTS, self._operator_claims(), {})[
            "statusCode"] == 403

    # ── the gates are independent, not a shared switch ──────────────────────
    def test_vehicle_flag_does_not_enable_the_fleet_route(self):
        assert self._approve(
            _FG25_FINDING_ALL_FACTS, self._operator_claims(),
            {"AVX_VEHICLE_ID_CLAIM_TRUSTED": "true"},
        )["statusCode"] == 403, "one attestation must not enable a different claim"

    def test_fleet_flag_does_not_enable_the_driver_route(self):
        assert self._approve(
            _FG25_FINDING_ALL_FACTS, self._driver_claims(),
            {"AVX_FLEET_IDS_CLAIM_TRUSTED": "true"},
        )["statusCode"] == 403

    def test_customer_flag_does_not_enable_either_vehicle_route(self):
        for claims in (self._driver_claims(), self._operator_claims()):
            assert self._approve(
                _FG25_FINDING_ALL_FACTS, claims,
                {"AVX_CUSTOMER_ID_CLAIM_TRUSTED": "true"},
            )["statusCode"] == 403

    # ── each flag enables exactly its own route ─────────────────────────────
    def test_vehicle_flag_enables_the_driver_route(self):
        assert self._approve(
            _FG25_FINDING_ALL_FACTS, self._driver_claims(),
            {"AVX_VEHICLE_ID_CLAIM_TRUSTED": "true"},
        )["statusCode"] == 200

    def test_fleet_flag_enables_the_fleet_route(self):
        assert self._approve(
            _FG25_FINDING_ALL_FACTS, self._operator_claims(),
            {"AVX_FLEET_IDS_CLAIM_TRUSTED": "true"},
        )["statusCode"] == 200

    @pytest.mark.parametrize("value", ["1", "yes", "TRUE ", "false", ""])
    def test_only_exact_true_opts_in(self, value):
        """Truthiness must not enable a claim — same rule as the customer gate."""
        expected = 200 if value.strip().lower() == "true" else 403
        assert self._approve(
            _FG25_FINDING_ALL_FACTS, self._driver_claims(),
            {"AVX_VEHICLE_ID_CLAIM_TRUSTED": value},
        )["statusCode"] == expected, f"{value!r} was treated as an opt-in"


# ---------------------------------------------------------------------------
# Fix Group 26 (review cycle 1 W3) — custom:fleetIds is CSV in production
# ---------------------------------------------------------------------------


class TestFleetClaimEncoding:
    """The live claim is CSV; `_extract_claim_list` alone returns it as one value."""

    def test_csv_multi_fleet_claim_is_parsed(self):
        """Measured 2026-09-23: custom:fleetIds = 'flt-a,FLEET-B' on a real user.

        `_extract_claim_list` JSON-parses and falls back to the whole string, so
        this yielded ['flt-a,FLEET-B'] — one element matching neither fleet. The
        route FG25 enabled did not work for the multi-fleet case it exists for, and
        every fixture used the JSON list form, which is why nothing noticed.
        """
        claims = {"custom:fleetIds": f"flt-meridian-range-001,{_FG25_FLEET_ID}"}
        assert h._caller_fleet_ids(claims) == ["flt-meridian-range-001", _FG25_FLEET_ID]

    def test_json_list_form_still_works(self):
        claims = {"custom:fleetIds": json.dumps(["flt-a", "flt-b"])}
        assert h._caller_fleet_ids(claims) == ["flt-a", "flt-b"]

    def test_single_value_works_in_both_forms(self):
        assert h._caller_fleet_ids({"custom:fleetIds": _FG25_FLEET_ID}) == [_FG25_FLEET_ID]
        assert h._caller_fleet_ids(
            {"custom:fleetIds": json.dumps([_FG25_FLEET_ID])}) == [_FG25_FLEET_ID]

    @pytest.mark.parametrize("raw", ["", "   ", ",", " , , "])
    def test_empty_and_separator_only_yield_nothing(self, raw):
        assert h._caller_fleet_ids({"custom:fleetIds": raw}) == []

    def test_whitespace_around_csv_members_is_stripped(self):
        claims = {"custom:fleetIds": f" flt-a , {_FG25_FLEET_ID} "}
        assert h._caller_fleet_ids(claims) == ["flt-a", _FG25_FLEET_ID]

    def test_csv_claim_authorizes_end_to_end(self):
        """The whole point: a real CSV multi-fleet operator can now approve."""
        claims = _claims(sub="cognito-uuid-op", groups=["fleet-operator"])
        claims["custom:fleetIds"] = f"flt-meridian-range-001,{_FG25_FLEET_ID}"
        with patch.object(h, "_load_finding_by_id", return_value=_FG25_FINDING_ALL_FACTS), \
             patch.dict(os.environ, _ALL_STANDING_TRUSTED), \
             patch.object(h, "_get_ddb") as mock_ddb:
            mock_table = MagicMock()
            mock_table.put_item.return_value = {}
            mock_ddb.return_value.Table.return_value = mock_table
            resp = h.handler(
                _event(
                    method="POST",
                    resource="/findings/{finding_id}/approve",
                    path_params={"finding_id": _FG25_FINDING_ALL_FACTS["finding_id"]},
                    claims=claims,
                    body=_FG15_APPROVE_BODY,
                ),
                None,
            )
        assert resp["statusCode"] == 200



@pytest.mark.untrusted_claims
class TestMembershipClaimGatesEndToEnd:
    """Fix Group 29: the three routes review cycle 3 measured returning 200 on a
    forged membership claim must now deny, in the DEPLOYED DEFAULT env.

    Cycle 3's measurement, as a legitimate `fleet-operator` of FLEET-A who edits their
    own user record to add FLEET-B:

        route                          forged claim         status   GSI queried
        GET /findings/fleet/{id}       custom:fleetIds      200      by_fleet
        GET /findings/dealership/{id}  custom:dealerIds     200      by_dealership
        GET /findings/district/{id}    custom:districtIds   200      by_district

    These assert 403 for the same shapes. The class is marked `untrusted_claims`, so it
    runs with every trust flag cleared — the state a deployed Lambda is in today.

    Each test also asserts the GSI helper was NEVER CALLED, not merely that the status
    was 403. A 403 alone would not prove the query was skipped, and the exposure was
    about reading another org's rows, not about the status code.
    """

    def _get(self, resource, path_params, claims, helper):
        event = _event(method="GET", resource=resource, path_params=path_params, claims=claims)
        with patch.object(h, helper, return_value=([], None)) as mock_helper:
            resp = h.handler(event, None)
        return resp, mock_helper

    def test_fleet_route_denies_and_never_queries(self):
        resp, mock_helper = self._get(
            "/findings/fleet/{fleet_id}", {"fleet_id": "fleet-NOT-MINE"},
            _claims(groups=["fleet-operator"], fleet_ids=["fleet-A", "fleet-NOT-MINE"]),
            "_list_findings_by_fleet",
        )
        assert resp["statusCode"] == 403, f"forged fleet claim must be denied; got {resp}"
        mock_helper.assert_not_called()

    def test_dealership_route_denies_and_never_queries(self):
        resp, mock_helper = self._get(
            "/findings/dealership/{dealer_id}", {"dealer_id": "D-NOT-MINE"},
            _claims(groups=["dealer-admin"], dealer_ids=["D-NOT-MINE"]),
            "_list_findings_by_dealership",
        )
        assert resp["statusCode"] == 403, f"forged dealer claim must be denied; got {resp}"
        mock_helper.assert_not_called()

    def test_district_route_denies_and_never_queries(self):
        resp, mock_helper = self._get(
            "/findings/district/{district_id}", {"district_id": "DIST-NOT-MINE"},
            _claims(groups=["district-manager"], district_ids=["DIST-NOT-MINE"]),
            "_list_findings_by_district",
        )
        assert resp["statusCode"] == 403, f"forged district claim must be denied; got {resp}"
        mock_helper.assert_not_called()

    def test_platform_admin_still_reaches_dealership_and_district(self):
        """The gates must not take the group-based platform-admin bypass with them.

        This is the over-broad-gate canary for this class: dealer/district scope lets
        platform-admin bypass the membership predicate entirely (the documented
        asymmetry vs fleet), and that bypass is not claim-derived.
        """
        for resource, params, helper in (
            ("/findings/dealership/{dealer_id}", {"dealer_id": "D-1"}, "_list_findings_by_dealership"),
            ("/findings/district/{district_id}", {"district_id": "DIST-1"}, "_list_findings_by_district"),
        ):
            resp, mock_helper = self._get(
                resource, params, _claims(groups=["platform-admin"]), helper
            )
            assert resp["statusCode"] == 200, (
                f"platform-admin must still reach {resource}; got {resp}"
            )
            mock_helper.assert_called_once()



def test_strip_list_covers_every_writer_projected_attr():
    """Fix Group 30 (security review W1): the two lists must not drift apart.

    `agents/tier2/writer.py` projects `_STANDING_ATTRS` onto the DDB item so the scope
    GSIs work; this handler strips `_PROJECTED_SCOPE_ATTRS` at the response boundary
    because the frozen contract has root `additionalProperties: false`. Fix Group 25
    added `vehicle_id` to the first list and not the second, so every VIN-scoped Finding
    with resolved standing returned a schema-invalid response.

    The writer cannot be imported here — it is a different deployment unit — so the
    expected set is parsed from its source. That is deliberate: a hand-maintained
    duplicate of the list is what broke, and asserting against the *other file's* source
    is what makes the next addition fail loudly instead of leaking.

    Mutation: remove any attr from `_PROJECTED_SCOPE_ATTRS`, or add one to the writer's
    `_STANDING_ATTRS`, -> this fails.
    """
    import ast
    import pathlib

    repo = pathlib.Path(h.__file__).resolve().parents[2]
    writer_src = (repo / "agents" / "tier2" / "writer.py").read_text()

    # Parsed with `ast`, NOT a regex. Review cycle 4 (W1) demonstrated that the
    # original `re.findall(r'"([a-z_]+)"', ...)` could not read a non-snake_case
    # name, and that the `len >= 3` non-vacuity floor could not detect the gap
    # because the set is EXACTLY 3 — so adding `"dealerGroupId"` to the writer
    # left this guard green. That is the shipped W1 defect one naming convention
    # over, inside the guard written to prevent it.
    #
    # `ast` removes the class rather than widening the character set: it reads
    # whatever literal the writer actually assigns, in any naming convention.
    writer_attrs: set[str] | None = None
    for node in ast.walk(ast.parse(writer_src)):
        targets = (
            [node.target] if isinstance(node, ast.AnnAssign)
            else node.targets if isinstance(node, ast.Assign)
            else []
        )
        if any(isinstance(t, ast.Name) and t.id == "_STANDING_ATTRS" for t in targets):
            writer_attrs = set(ast.literal_eval(node.value))
            break

    assert writer_attrs is not None, (
        "could not locate the _STANDING_ATTRS assignment in agents/tier2/writer.py — "
        "this guard cannot verify anything, so it fails rather than passing silently"
    )
    # Non-vacuity, now meaningful: a parse failure raises above rather than yielding a
    # short set, so this asserts the value is a non-empty collection of strings.
    assert writer_attrs and all(isinstance(a, str) and a for a in writer_attrs), (
        f"_STANDING_ATTRS parsed as {writer_attrs!r} — expected non-empty strings"
    )

    missing = writer_attrs - h._PROJECTED_SCOPE_ATTRS
    assert not missing, (
        f"writer.py projects {sorted(missing)} onto the DDB item but the response "
        f"boundary does not strip it. Root `additionalProperties: false` in "
        f"finding.schema.json means every response carrying it is contract-invalid."
    )


def test_vin_scoped_response_with_all_three_standing_attrs_is_schema_valid():
    """The end-to-end form of the above, against the real schema.

    The regression W1 describes is only reachable when all three standing attributes are
    present at once, which no pre-existing fixture produced. This builds that item and
    validates the stripped response against `finding.schema.json` itself rather than
    against a restatement of it.
    """
    import json
    import pathlib

    # Hard import, NOT `pytest.importorskip`. Review cycle 4 (Suggestion) caught that
    # the skip-form plus an undeclared dependency meant this check silently vanished on
    # every machine but the author's. `requirements-dev.txt`'s own header records the
    # same lesson about the pyflakes gate: "a skipped guard and a passing guard look
    # identical in a summary line." `jsonschema==4.26.0` is now declared there.
    import jsonschema

    repo = pathlib.Path(h.__file__).resolve().parents[2]
    schema = json.loads(
        (repo / "agents" / "tier2" / "schemas" / "finding.schema.json").read_text()
    )

    item = {
        **_SAMPLE_FINDING,
        "scope": {"vin": _FG25_VIN},
        "vin": _FG25_VIN,
        "owner_id": _FG15_OWNER_A,
        "fleet_id": _FG25_FLEET_ID,
        "vehicle_id": _FG25_VEHICLE_ID,
    }
    stripped = h._strip_finding_for_response(item)

    for attr in ("vin", "owner_id", "fleet_id", "vehicle_id"):
        assert attr not in stripped, f"{attr} survived the response-boundary strip"
    assert stripped["scope"]["vin"] == _FG25_VIN, "the nested scope must be untouched"

    jsonschema.validate(instance=stripped, schema=schema)



# ---------------------------------------------------------------------------
# Fix Group 35 — approve dispatches, and the executor's result is recorded
#
# Runs the REAL `_dispatch_action` / `_record_dispatch_result` against a moto
# DynamoDB table, so the condition expression is evaluated by DynamoDB semantics
# rather than trusted to a MagicMock. The DMS call is stubbed with `responses`;
# `mock_aws` also absorbs the D5 escalation's PutEvents / PutMetricData and the
# executor's SSM policy read, so nothing here reaches live AWS.
#
# MUTATION-VERIFIED (recorded in decisions-g10-unblock-fragment.md):
#   M35-1 drop token injection        -> test_dms_approve_end_to_end
#   M35-2 drop the result condition   -> test_cancel_during_dispatch_wins
#   M35-3 drop the in_progress arm    -> test_consumer_race_still_records_the_receipt
#   M35-3b drop attribute_not_exists  -> test_existing_receipt_is_not_overwritten
#   M35-4 persist audit_entry raw     -> test_refusal_is_recorded_schema_valid
#   M35-5 dispatch on idempotency hit -> test_reapprove_does_not_dispatch_again
#   M35-6 body VIN over Finding VIN   -> test_body_vin_must_match_the_finding
#   M35-7 log the Authorization header-> test_token_never_reaches_the_logs
# ---------------------------------------------------------------------------

_FG35_VIN = "1MRDN0000000000015"
_FG35_FINDING = {**_SAMPLE_FINDING, "scope": {"vin": _FG35_VIN}}
_FG35_DMS = "http://dms-staging.test"
_FG35_TOKEN = "eyJ.fg35-caller-token.sig"


def _fg35_action_schema_validator():
    import jsonschema

    schema_path = os.path.join(_REPO_ROOT, "agents", "tier2", "schemas", "action.schema.json")
    with open(schema_path) as fh:
        schema = json.load(fh)
    return jsonschema.Draft202012Validator(
        schema, format_checker=jsonschema.Draft202012Validator.FORMAT_CHECKER,
    )


def _fg35_assert_schema_valid(row: dict) -> None:
    doc = json.loads(json.dumps(row, cls=h._DecimalEncoder))
    errors = sorted(_fg35_action_schema_validator().iter_errors(doc), key=str)
    assert not errors, "stored Action fails action.schema.json:\n" + "\n".join(
        f"  {list(e.absolute_path)}: {e.message}" for e in errors
    )


class TestFG35ApproveDispatch:

    @pytest.fixture
    def env(self):
        """moto DynamoDB + the real dispatch path + a stubbed DMS endpoint."""
        import boto3
        import responses as _responses
        from moto import mock_aws

        from agents.executors import base as _base
        from agents.executors import escalation as _escalation
        from agents.executors import registry as _registry

        def _drop_cached_clients():
            # Module-level client caches: one created outside mock_aws would reach
            # live AWS from inside it, and one created inside would outlive it.
            _base._ssm_client = None
            _escalation._events_client = None
            _escalation._cloudwatch_client = None
            _base.reset_policy_cache()

        dms_exec = _registry.EXECUTORS["dms"]
        saved_sleep = dms_exec._sleep
        with mock_aws(), _responses.RequestsMock(assert_all_requests_are_fired=False) as rm, \
             patch.dict(os.environ, {
                 "VSA_DMS_API_ENDPOINT": _FG35_DMS,
                 "AWS_DEFAULT_REGION": "us-west-2",
                 "AWS_REGION": "us-west-2",
             }):
            _drop_cached_clients()
            dms_exec._sleep = lambda _s: None
            ddb = boto3.resource("dynamodb", region_name="us-west-2")
            table = ddb.create_table(
                TableName=h._ACTIONS_TABLE,
                KeySchema=[{"AttributeName": "action_id", "KeyType": "HASH"}],
                AttributeDefinitions=[{"AttributeName": "action_id", "AttributeType": "S"}],
                BillingMode="PAY_PER_REQUEST",
            )
            with patch.object(h, "_get_ddb", return_value=ddb), \
                 patch.object(h, "_dispatch_action", side_effect=_REAL_DISPATCH_ACTION), \
                 patch.object(h, "_record_dispatch_result", side_effect=_REAL_RECORD_DISPATCH_RESULT), \
                 patch.object(h, "_load_finding_by_id", return_value=_FG35_FINDING):
                yield {"table": table, "rm": rm}
            dms_exec._sleep = saved_sleep
            _drop_cached_clients()

    @staticmethod
    def _approve(body=None, *, token=_FG35_TOKEN, claims=None):
        event = _event(
            method="POST",
            resource="/findings/{finding_id}/approve",
            path_params={"finding_id": _FG35_FINDING["finding_id"]},
            claims=claims or _claims(sub="admin-sub", groups=["platform-admin"]),
            body=body or {"target_system": "dms", "action_kind": "book_combined_visit",
                          "parameters": {"dealer_id": "dealer-seattle"}},
        )
        if token is not None:
            event["headers"] = {"authorization": f"Bearer {token}"}
        return h.handler(event, None)

    @staticmethod
    def _stored(env, action_id):
        return env["table"].get_item(Key={"action_id": action_id}).get("Item")

    # ── the Step 5 / Step 6 path ─────────────────────────────────────────────

    def test_dms_approve_end_to_end(self, env):
        env["rm"].add("POST", f"{_FG35_DMS}/api/dms/fleet/repair-orders",
                      json={"ro_id": "RO-FG35-1"}, status=201)
        resp = self._approve()
        assert resp["statusCode"] == 200, resp["body"]
        body = _decode_body(resp)

        # Step 5: DMS was called, with the caller's token and DMS's required body.
        calls = [c for c in env["rm"].calls if c.request.url.endswith("/repair-orders")]
        assert len(calls) == 1
        assert calls[0].request.headers["Authorization"] == f"Bearer {_FG35_TOKEN}"
        sent = json.loads(calls[0].request.body)
        assert sent["vehicle_vin"] == _FG35_VIN, "the VIN must come from the Finding"
        assert sent["dealer_id"] == "dealer-seattle"
        assert body["action_id"] == f"AVX-ACT-{sent['avx_action_id']}"

        # Step 5 / 6: receipt captured, status advanced, transition audited.
        assert body["target_ref"] == "RO-FG35-1"
        assert body["status"] == "in_progress"
        assert len(body["audit_trail"]) == 1
        entry = body["audit_trail"][0]
        assert (entry["from_status"], entry["to_status"], entry["by"]) == (
            "pending_execution", "in_progress", "executor-dms")
        assert entry["at"] == body["updated_at"]

        stored = self._stored(env, body["action_id"])
        assert stored["target_ref"] == "RO-FG35-1"
        assert stored["status"] == "in_progress"
        assert stored["parameters"]["vehicle_vin"] == _FG35_VIN
        _fg35_assert_schema_valid(stored)

    def test_refusal_is_recorded_schema_valid(self, env):
        """D-G10-6: a failure entry must validate. `audit_entry()` emits two keys the
        frozen schema forbids; they are folded into `note`, not written raw."""
        env["rm"].add("POST", f"{_FG35_DMS}/api/dms/fleet/repair-orders",
                      json={"error": "cannot accept"}, status=422)
        body = _decode_body(self._approve())
        assert body["status"] == "declined"
        entry = body["audit_trail"][0]
        assert entry["failure_kind"] == "target_refused"
        assert "last_error_status" not in entry and "last_error_class" not in entry
        assert "last_error_status=422" in entry["note"]
        _fg35_assert_schema_valid(self._stored(env, body["action_id"]))

    def test_dms_validation_400_is_not_recorded_as_a_dealer_refusal(self, env):
        """W3: DMS's roster rejection is our request, not a dealer's answer."""
        env["rm"].add("POST", f"{_FG35_DMS}/api/dms/fleet/repair-orders",
                      json={"error": "dealer_id is not a valid DMS rooftop"}, status=400)
        body = _decode_body(self._approve())
        assert body["status"] == "failed"
        entry = body["audit_trail"][0]
        assert entry["failure_kind"] == "executor_terminal_error"
        assert "last_error_status=400" in entry["note"]
        _fg35_assert_schema_valid(self._stored(env, body["action_id"]))

    def test_exhausted_retries_are_recorded_schema_valid(self, env):
        for _ in range(5):
            env["rm"].add("POST", f"{_FG35_DMS}/api/dms/fleet/repair-orders",
                          json={"error": "unavailable"}, status=503)
        body = _decode_body(self._approve())
        assert body["status"] == "failed"
        entry = body["audit_trail"][0]
        assert entry["failure_kind"] == "executor_exhausted_retries"
        assert entry["retry_count"] >= 1
        _fg35_assert_schema_valid(self._stored(env, body["action_id"]))

    def test_missing_token_is_recorded_as_executor_error_not_a_dms_call(self, env):
        body = _decode_body(self._approve(token=None))
        assert body["status"] == "failed"
        note = body["audit_trail"][0]["note"]
        assert "MissingJwt" in note, "the error class must be folded into note"
        assert "jwt_unavailable" in note, "the executor's own note must survive the fold"
        assert not [c for c in env["rm"].calls if c.request.url.endswith("/repair-orders")]

    # ── races and idempotency ────────────────────────────────────────────────

    def _dispatch_then(self, env, mutate_row, result):
        """A dispatch that changes the row mid-flight, as a concurrent writer would."""
        def _fake(action, claims):
            mutate_row(env["table"], action["action_id"])
            return result
        return patch.object(h, "_dispatch_action", side_effect=_fake)

    def test_cancel_during_dispatch_wins(self, env, caplog):
        import logging

        caplog.set_level(logging.WARNING)

        def cancel(table, action_id):
            table.update_item(Key={"action_id": action_id},
                              UpdateExpression="SET #s = :c",
                              ExpressionAttributeNames={"#s": "status"},
                              ExpressionAttributeValues={":c": "cancelled"})
        with self._dispatch_then(env, cancel, ExecuteResult.success(target_ref="RO-LATE")):
            body = _decode_body(self._approve())
        assert body["status"] == "cancelled", "the owner's cancel must not be overwritten"
        stored = self._stored(env, body["action_id"])
        assert stored["status"] == "cancelled"
        assert "target_ref" not in stored
        # W5: the log line is CVX's only record of a repair order booked for an
        # Action the owner cancelled mid-dispatch. It must name what to reconcile.
        warnings = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"
                    and "row changed during dispatch" in r.getMessage()]
        assert len(warnings) == 1, caplog.text
        assert body["action_id"] in warnings[0]
        assert "RO-LATE" in warnings[0] and "target_system=dms" in warnings[0]
        assert "result=in_progress" in warnings[0], "Accept 5 names the result status"

    def test_consumer_race_still_records_the_receipt(self, env):
        """DMS emits `Draft` before it returns 201; the consumer may project
        pending_execution → in_progress first. The receipt must still land."""
        def consumer(table, action_id):
            table.update_item(Key={"action_id": action_id},
                              UpdateExpression="SET #s = :p",
                              ExpressionAttributeNames={"#s": "status"},
                              ExpressionAttributeValues={":p": "in_progress"})
        with self._dispatch_then(env, consumer, ExecuteResult.success(target_ref="RO-RACE")):
            body = _decode_body(self._approve())
        stored = self._stored(env, body["action_id"])
        assert stored["target_ref"] == "RO-RACE"
        assert stored["status"] == "in_progress"
        assert len(stored["audit_trail"]) == 1
        assert stored["audit_trail"][0]["to_status"] == "in_progress"
        _fg35_assert_schema_valid(stored)

    def test_existing_receipt_is_not_overwritten(self, env):
        def already_recorded(table, action_id):
            table.update_item(Key={"action_id": action_id},
                              UpdateExpression="SET #s = :p, target_ref = :r",
                              ExpressionAttributeNames={"#s": "status"},
                              ExpressionAttributeValues={":p": "in_progress", ":r": "RO-FIRST"})
        with self._dispatch_then(env, already_recorded,
                                 ExecuteResult.success(target_ref="RO-SECOND")):
            body = _decode_body(self._approve())
        assert self._stored(env, body["action_id"])["target_ref"] == "RO-FIRST"

    def test_reapprove_does_not_dispatch_again(self, env):
        env["rm"].add("POST", f"{_FG35_DMS}/api/dms/fleet/repair-orders",
                      json={"ro_id": "RO-ONCE"}, status=201)
        first = _decode_body(self._approve())
        second = _decode_body(self._approve())
        assert first["action_id"] == second["action_id"]
        calls = [c for c in env["rm"].calls if c.request.url.endswith("/repair-orders")]
        assert len(calls) == 1, "a re-approve must not book a second repair order"
        assert second["target_ref"] == "RO-ONCE"

    def test_dispatch_raising_leaves_the_row_pending(self, env, caplog):
        with patch.object(h, "_dispatch_action",
                          side_effect=RuntimeError(f"boom {_FG35_TOKEN}")):
            resp = self._approve()
        assert resp["statusCode"] == 500
        action_id = _decode_body(resp)["action_id"]
        assert self._stored(env, action_id)["status"] == "pending_execution"
        assert _FG35_TOKEN not in caplog.text

    def test_result_write_error_is_a_500_naming_the_action(self, env):
        from botocore.exceptions import ClientError

        err = ClientError({"Error": {"Code": "ProvisionedThroughputExceededException"}},
                          "UpdateItem")
        with patch.object(h, "_record_dispatch_result", side_effect=err), \
             patch.object(h, "_dispatch_action",
                          return_value=ExecuteResult.success(target_ref="RO-X")):
            resp = self._approve()
        assert resp["statusCode"] == 500
        assert _decode_body(resp)["action_id"].startswith("AVX-ACT-")

    def test_token_never_reaches_the_logs(self, env, caplog):
        import logging

        caplog.set_level(logging.DEBUG)
        env["rm"].add("POST", f"{_FG35_DMS}/api/dms/fleet/repair-orders",
                      json={"error": "x"}, status=403)  # failed → escalation path too
        self._approve()
        assert _FG35_TOKEN not in caplog.text

    # ── D-G10-2: what a dms approve must carry ────────────────────────────────

    def test_body_vin_must_match_the_finding(self, env):
        resp = self._approve({"target_system": "dms", "action_kind": "book_combined_visit",
                              "parameters": {"dealer_id": "dealer-seattle",
                                             "vehicle_vin": "1MRDN0000000009999"}})
        assert resp["statusCode"] == 400
        assert not env["table"].scan()["Items"], "nothing may be written on a 400"

    def test_matching_body_vin_is_accepted(self, env):
        env["rm"].add("POST", f"{_FG35_DMS}/api/dms/fleet/repair-orders",
                      json={"ro_id": "RO-M"}, status=201)
        resp = self._approve({"target_system": "dms", "action_kind": "book_combined_visit",
                              "parameters": {"dealer_id": "dealer-seattle",
                                             "vehicle_vin": _FG35_VIN}})
        assert resp["statusCode"] == 200

    def test_dealer_id_is_required(self, env):
        resp = self._approve({"target_system": "dms", "action_kind": "book_combined_visit"})
        assert resp["statusCode"] == 400
        assert "dealer_id" in _decode_body(resp)["error"]
        assert not env["table"].scan()["Items"]

    def test_dms_needs_a_vin_scoped_finding(self, env):
        owner_scoped = {**_SAMPLE_FINDING, "scope": {"owner_id": "admin-sub"}}
        with patch.object(h, "_load_finding_by_id", return_value=owner_scoped):
            resp = self._approve()
        assert resp["statusCode"] == 400
        assert not env["table"].scan()["Items"]

    def test_parameters_must_be_an_object(self, env):
        resp = self._approve({"target_system": "none", "action_kind": "acknowledge_only",
                              "parameters": ["x"]})
        assert resp["statusCode"] == 400


    # ── review Cycle 1: W2 (DMS caller groups) and W4 (conflicting re-approve) ─

    @pytest.mark.parametrize("groups", [["vehicle-owner"], ["service-advisor"], ["consumer"]])
    def test_dms_refuses_callers_dms_would_refuse(self, env, groups):
        """W2: DMS admits fleet-operator / platform-admin only. Anyone else would pass
        CVX's approve check, be refused by DMS, record `failed` and page an
        operator. Refused before any write or dispatch instead."""
        claims = _claims(sub="u", groups=groups, customer_id="CUST-004000DD")
        with patch.dict(os.environ, {"AVX_CUSTOMER_ID_CLAIM_TRUSTED": "true"}), \
             patch.object(h, "_check_approve_scope", return_value=True):
            resp = self._approve(claims=claims)
        assert resp["statusCode"] == 403, resp["body"]
        assert "fleet-operator" in _decode_body(resp)["error"]
        assert not env["table"].scan()["Items"]
        assert not [c for c in env["rm"].calls if c.request.url.endswith("/repair-orders")]

    def test_fleet_operator_passes_the_dms_group_gate(self, env):
        env["rm"].add("POST", f"{_FG35_DMS}/api/dms/fleet/repair-orders",
                      json={"ro_id": "RO-FO"}, status=201)
        with patch.object(h, "_check_approve_scope", return_value=True):
            resp = self._approve(claims=_claims(sub="fo", groups=["fleet-operator"]))
        assert resp["statusCode"] == 200
        assert _decode_body(resp)["target_ref"] == "RO-FO"

    def test_reapprove_with_a_different_dealer_is_a_conflict(self, env):
        """W4: the id ignores parameters, so a second dealer used to get the first
        booking back as if it had been honoured."""
        env["rm"].add("POST", f"{_FG35_DMS}/api/dms/fleet/repair-orders",
                      json={"ro_id": "RO-A"}, status=201)
        self._approve()
        resp = self._approve({"target_system": "dms", "action_kind": "book_combined_visit",
                              "parameters": {"dealer_id": "dealer-boston"}})
        assert resp["statusCode"] == 409
        calls = [c for c in env["rm"].calls if c.request.url.endswith("/repair-orders")]
        assert len(calls) == 1

    def test_reapprove_with_a_padded_dealer_is_the_same_request(self, env):
        """S10: dealer_id is stored stripped, so whitespace is not a new request."""
        env["rm"].add("POST", f"{_FG35_DMS}/api/dms/fleet/repair-orders",
                      json={"ro_id": "RO-A"}, status=201)
        first = _decode_body(self._approve())
        resp = self._approve({"target_system": "dms", "action_kind": "book_combined_visit",
                              "parameters": {"dealer_id": "  dealer-seattle "}})
        assert resp["statusCode"] == 200
        assert first["parameters"]["dealer_id"] == "dealer-seattle"

    def test_reread_failure_after_a_lost_race_is_a_500(self, env):
        """S9: the re-read after the conditional write loses must not escape."""
        from botocore.exceptions import ClientError

        def cancel(table, action_id):
            table.update_item(Key={"action_id": action_id},
                              UpdateExpression="SET #s = :c",
                              ExpressionAttributeNames={"#s": "status"},
                              ExpressionAttributeValues={":c": "cancelled"})
            real_get = table.get_item

            def failing_get(**kw):
                raise ClientError({"Error": {"Code": "InternalServerError"}}, "GetItem")
            table.get_item = failing_get  # only the handler's re-read uses it now
            _fg35_restore.append((table, real_get))

        _fg35_restore: list = []
        with patch.object(h, "_get_ddb") as mock_ddb:
            mock_ddb.return_value.Table.return_value = env["table"]
            with self._dispatch_then(env, cancel, ExecuteResult.success(target_ref="RO-Z")):
                resp = self._approve()
        for table, real_get in _fg35_restore:
            table.get_item = real_get
        assert resp["statusCode"] == 500
        assert _decode_body(resp)["action_id"].startswith("AVX-ACT-")


    # ── review Cycle 2: S1 and S3 ────────────────────────────────────────────

    def test_reapprove_with_a_different_target_is_a_conflict(self, env):
        """S1: the target_system half of the W4 check."""
        env["rm"].add("POST", f"{_FG35_DMS}/api/dms/fleet/repair-orders",
                           json={"ro_id": "RO-T"}, status=201)
        body = {"target_system": "dms", "action_kind": "book_combined_visit",
                "parameters": {"dealer_id": "dealer-seattle"}}
        assert self._approve(body)["statusCode"] == 200
        # Same parameters as the stored row (VIN included), so ONLY the target
        # differs and the parameters half of the check cannot be what fires.
        other = {"target_system": "none", "action_kind": "book_combined_visit",
                 "parameters": {"dealer_id": "dealer-seattle", "vehicle_vin": _FG35_VIN}}
        assert self._approve(other)["statusCode"] == 409

    def test_idempotency_reread_is_strongly_consistent(self, env):
        """S3: an eventually-consistent miss fell back to the request's own row and
        so defeated the W4 conflict check."""
        env["rm"].add("POST", f"{_FG35_DMS}/api/dms/fleet/repair-orders",
                           json={"ro_id": "RO-C"}, status=201)
        self._approve()
        table = env["table"]
        seen = {}
        real_get = table.get_item

        def spy(**kw):
            seen.update(kw)
            return real_get(**kw)
        with patch.object(h, "_get_ddb") as mock_ddb:
            mock_ddb.return_value.Table.return_value = table
            table.get_item = spy
            try:
                self._approve()
            finally:
                table.get_item = real_get
        assert seen.get("ConsistentRead") is True


class TestFG35LostRaceRereadConsistency:
    """Review Cycle 3 S1: the lost-race re-read also uses ConsistentRead."""

    def test_lost_race_reread_is_strongly_consistent(self):
        from botocore.exceptions import ClientError

        table = MagicMock()
        table.put_item.return_value = {}
        ccf = type("ConditionalCheckFailedException", (ClientError,), {})
        table.meta.client.exceptions.ConditionalCheckFailedException = ccf
        table.get_item.return_value = {"Item": {"status": "cancelled"}}
        with patch.object(h, "_load_finding_by_id", return_value=_FG35_FINDING), \
             patch.object(h, "_get_ddb") as mock_ddb, \
             patch.object(h, "_dispatch_action",
                          return_value=ExecuteResult.success(target_ref="RO-1")), \
             patch.object(h, "_record_dispatch_result",
                          side_effect=ccf({"Error": {"Code": "ConditionalCheckFailedException"}},
                                          "UpdateItem")):
            mock_ddb.return_value.Table.return_value = table
            resp = TestFG35ApproveDispatch._approve()
        assert resp["statusCode"] == 200
        assert table.get_item.call_args.kwargs.get("ConsistentRead") is True


class TestFG35AuditEntrySchemaPin:
    """`_AUDIT_ENTRY_KEYS` is a hand copy of the schema; pin it to the file."""

    def test_keys_equal_the_schema_audit_entry_properties(self):
        path = os.path.join(_REPO_ROOT, "agents", "tier2", "schemas", "action.schema.json")
        with open(path) as fh:
            props = json.load(fh)["$defs"]["audit_entry"]["properties"]
        assert set(h._AUDIT_ENTRY_KEYS) == set(props)


class TestFG35BearerToken:
    @pytest.mark.parametrize("headers, expected", [
        ({"Authorization": "Bearer abc"}, "abc"),
        ({"authorization": "bearer abc"}, "abc"),
        ({"AUTHORIZATION": "abc"}, "abc"),
        ({"Authorization": "   "}, None),
        ({}, None),
        (None, None),
    ])
    def test_extraction(self, headers, expected):
        assert h._bearer_token({"headers": headers}) == expected



# ---------------------------------------------------------------------------
# Fix Group 36 — the owner read routes honour owner standing
#
# `issues/2026-09-24-avx-owner-read-route-compares-sub-to-party-id/`. Both routes
# compared only `sub == owner_id`, while projected (VIN-scoped) Findings carry the
# party of record `CUST-XXXXXXXX` in `owner_id`. D-G10-4: accept `sub` OR the
# trusted `custom:customerId`, mirroring `_check_approve_scope`.
#
# MUTATION-VERIFIED (decisions-g10-unblock-fragment.md):
#   M36-1 read the claim without the trust gate -> test_untrusted_claim_is_denied
#   M36-2 `in` instead of `==`                  -> test_prefix_of_the_owner_is_denied
#   M36-3 remove the arm (owner route)          -> test_party_of_record_reads_own_findings[owner]
#   M36-4 remove the arm (owner-fleet route)    -> test_party_of_record_reads_own_findings[owner-fleet]
# ---------------------------------------------------------------------------

_FG36_CUST = "CUST-004000DD"
_FG36_ROUTES = {
    "owner": "/findings/owner/{owner_id}",
    "owner-fleet": "/findings/owner-fleet/{owner_id}",
}


class TestFG36OwnerReadRoutesHonourPartyOfRecord:

    @staticmethod
    def _read(route, owner_id, claims, env):
        seen = []

        def _fake_list(oid, limit, next_token):
            seen.append(oid)
            return [], None

        with patch.object(h, "_list_findings_by_owner", side_effect=_fake_list), \
             patch.dict(os.environ, env, clear=False):
            resp = h.handler(_event(method="GET", resource=_FG36_ROUTES[route],
                                    path_params={"owner_id": owner_id},
                                    claims=claims), None)
        return resp, seen

    @pytest.mark.parametrize("route", sorted(_FG36_ROUTES))
    def test_party_of_record_reads_own_findings(self, route):
        resp, seen = self._read(
            route, _FG36_CUST,
            _claims(sub="11111111-2222-3333-4444-555555555555",
                    groups=["vehicle-owner"], customer_id=_FG36_CUST),
            {"AVX_CUSTOMER_ID_CLAIM_TRUSTED": "true"},
        )
        assert resp["statusCode"] == 200, resp["body"]
        assert seen == [_FG36_CUST], "the query must use the path owner_id"

    @pytest.mark.parametrize("route", sorted(_FG36_ROUTES))
    def test_untrusted_claim_is_denied(self, route):
        resp, seen = self._read(
            route, _FG36_CUST,
            _claims(sub="11111111-2222-3333-4444-555555555555",
                    groups=["vehicle-owner"], customer_id=_FG36_CUST),
            {"AVX_CUSTOMER_ID_CLAIM_TRUSTED": "false"},
        )
        assert resp["statusCode"] == 403
        assert seen == [], "no query may run for a denied caller"

    @pytest.mark.parametrize("route", sorted(_FG36_ROUTES))
    def test_other_party_is_denied(self, route):
        resp, _ = self._read(
            route, _FG36_CUST,
            _claims(sub="u", groups=["vehicle-owner"], customer_id="CUST-0000BEEF"),
            {"AVX_CUSTOMER_ID_CLAIM_TRUSTED": "true"},
        )
        assert resp["statusCode"] == 403

    @pytest.mark.parametrize("route", sorted(_FG36_ROUTES))
    def test_prefix_of_the_owner_is_denied(self, route):
        resp, _ = self._read(
            route, _FG36_CUST + "0",
            _claims(sub="u", groups=["vehicle-owner"], customer_id=_FG36_CUST),
            {"AVX_CUSTOMER_ID_CLAIM_TRUSTED": "true"},
        )
        assert resp["statusCode"] == 403

    @pytest.mark.parametrize("route", sorted(_FG36_ROUTES))
    def test_owner_that_is_a_prefix_of_the_claim_is_denied(self, route):
        """S2: the reverse direction (`owner_id in customer_id`)."""
        resp, _ = self._read(
            route, _FG36_CUST[:-1],
            _claims(sub="u", groups=["vehicle-owner"], customer_id=_FG36_CUST),
            {"AVX_CUSTOMER_ID_CLAIM_TRUSTED": "true"},
        )
        assert resp["statusCode"] == 403

    @pytest.mark.parametrize("route", sorted(_FG36_ROUTES))
    def test_no_claim_is_denied(self, route):
        resp, _ = self._read(
            route, _FG36_CUST, _claims(sub="u", groups=["vehicle-owner"]),
            {"AVX_CUSTOMER_ID_CLAIM_TRUSTED": "true"},
        )
        assert resp["statusCode"] == 403

    @pytest.mark.parametrize("route", sorted(_FG36_ROUTES))
    def test_sub_self_read_still_works(self, route):
        """Owner-scoped Findings are keyed by `sub`; that arm must survive."""
        sub = "11111111-2222-3333-4444-555555555555"
        resp, _ = self._read(route, sub, _claims(sub=sub, groups=["consumer"]), {})
        assert resp["statusCode"] == 200



# ---------------------------------------------------------------------------
# The sign-in email is never an owner id (issue 2026-09-25, CMS repo
# `issues/2026-09-25-ios-findings-owner-id-is-email/`)
# ---------------------------------------------------------------------------
#
# The iOS Alerts tab sent `GET /findings/owner/<sign-in email>` and got 403. The
# fix is client-side (send `sub` / `custom:customerId`). These tests pin the
# server half so the plausible wrong fix, accepting an `email` claim that equals
# the path, fails here. `email` is in the CMS app client's `WriteAttributes`, so
# a user can set it to another user's address; comparing it would be a
# cross-tenant read.

_EMAIL = "meridian.driver@example.com"
_EMAIL_CALLER_SUB = "086183b0-1081-7011-80a1-8230fcbff4c6"


def _email_claims(email=_EMAIL, **kw):
    c = _claims(sub=_EMAIL_CALLER_SUB, groups=["vehicle-owner"], customer_id="CUST-004000DD", **kw)
    c["email"] = email
    c["email_verified"] = "true"
    return c


class TestOwnerRoutesNeverAcceptEmail:
    @staticmethod
    def _read(route, owner_id, claims):
        seen = []

        def _fake_list(oid, limit, next_token):
            seen.append(oid)
            return [], None

        with patch.object(h, "_list_findings_by_owner", side_effect=_fake_list), \
             patch.dict(os.environ, {"AVX_CUSTOMER_ID_CLAIM_TRUSTED": "true"}, clear=False):
            resp = h.handler(_event(method="GET", resource=_FG36_ROUTES[route],
                                    path_params={"owner_id": owner_id},
                                    claims=claims), None)
        return resp, seen

    @pytest.mark.parametrize("route", sorted(_FG36_ROUTES))
    def test_own_email_as_owner_id_is_refused(self, route):
        """The exact 2026-09-25 request: the caller's own verified email."""
        resp, seen = self._read(route, _EMAIL, _email_claims())
        assert resp["statusCode"] == 403, resp["body"]
        assert seen == [], "no query may run for a refused owner id"

    @pytest.mark.parametrize("route", sorted(_FG36_ROUTES))
    def test_rewritten_email_does_not_read_another_users_feed(self, route):
        """A user who sets their email to someone else's address gains nothing."""
        victim = "someone.else@example.com"
        resp, seen = self._read(route, victim, _email_claims(email=victim))
        assert resp["statusCode"] == 403, resp["body"]
        assert seen == []

    @pytest.mark.parametrize("route", sorted(_FG36_ROUTES))
    def test_another_users_sub_is_refused(self, route):
        resp, seen = self._read(route, "99999999-8888-7777-6666-555555555555", _email_claims())
        assert resp["statusCode"] == 403, resp["body"]
        assert seen == []

    @pytest.mark.parametrize("route", sorted(_FG36_ROUTES))
    def test_the_identities_the_app_now_sends_are_accepted(self, route):
        """Positive control: `sub` and the trusted `custom:customerId` both read."""
        for owner_id in (_EMAIL_CALLER_SUB, "CUST-004000DD"):
            resp, seen = self._read(route, owner_id, _email_claims())
            assert resp["statusCode"] == 200, (owner_id, resp["body"])
            assert seen == [owner_id]
