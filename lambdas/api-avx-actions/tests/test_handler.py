"""
Unit tests for lambdas/api-avx-actions/handler.py

Mutation verification results (run 2026-09-19):
  (a) Widened check_owner_scope to accept any sub (removed sub == owner_id check):
      test_actions_scope_leak_owner_a_cannot_see_owner_b FAILED as expected.
      Mutation reverted. ✓ CAUGHT

  (b) Collapsed dismiss route to write `cancelled` instead of `dismissed`:
      test_dismiss_route_transitions_to_dismissed FAILED as expected.
      test_dismissed_is_distinct_from_cancelled_in_audit_trail FAILED as expected.
      Mutation reverted. ✓ CAUGHT

  (c) N/A (ios-owner reject is in findings handler).

  (d) N/A (SNS is in register-device handler).

Run:
    python3 -m pytest lambdas/api-avx-actions/ -v
"""
from __future__ import annotations

import json
import os
import sys
from unittest.mock import MagicMock, patch, call
from decimal import Decimal

import pytest

_LAMBDA_ROOT = os.path.join(os.path.dirname(__file__), "..")
_LAMBDAS_ROOT = os.path.dirname(_LAMBDA_ROOT)
for _p in (_LAMBDA_ROOT, _LAMBDAS_ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import handler as h


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _claims(sub="user-sub-123", groups=None, fleet_ids=None,
            dealer_ids=None, district_ids=None):
    c: dict = {"sub": sub}
    c["cognito:groups"] = (
        json.dumps(groups) if isinstance(groups, list) else (groups or "")
    )
    if fleet_ids is not None:
        c["custom:fleetIds"] = json.dumps(fleet_ids)
    if dealer_ids is not None:
        c["custom:dealerIds"] = json.dumps(dealer_ids)
    if district_ids is not None:
        c["custom:districtIds"] = json.dumps(district_ids)
    return c


def _event(method="GET", resource="/actions/owner/{owner_id}",
           path_params=None, claims=None, body=None, qs=None) -> dict:
    return {
        "httpMethod": method,
        "resource": resource,
        "pathParameters": path_params or {},
        "queryStringParameters": qs or {},
        "requestContext": {"authorizer": {"claims": claims or _claims()}},
        "body": json.dumps(body) if body else None,
    }


def _decode_body(resp: dict) -> dict:
    return json.loads(resp["body"])


def _sample_action(status="pending_execution", created_by=None, finding_id=None) -> dict:
    return {
        "action_id": "AVX-ACT-aabbccdd-0000-0000-0000-000000000001",
        "finding_id": finding_id or "FIND-00000000-0000-0000-0000-000000000001",
        "created_by": created_by or "owner-user-sub-123",
        "created_at": "2026-09-19T12:00:00Z",
        "updated_at": "2026-09-19T12:00:00Z",
        "target_system": "dms",
        "status": status,
        "action_kind": "book_combined_visit",
        "parameters": {},
        "evidence": [],
        "audit_trail": [],
    }


# ---------------------------------------------------------------------------
# Auth guard
# ---------------------------------------------------------------------------

class TestAuthGuard:
    def test_missing_claims_returns_401(self):
        ev = _event()
        ev["requestContext"]["authorizer"]["claims"] = None
        assert h.handler(ev, None)["statusCode"] == 401


# ---------------------------------------------------------------------------
# GET /actions/owner/{owner_id}
# ---------------------------------------------------------------------------

class TestActionsVehicleRoute:
    """C4: /actions/vehicle/{vin} route — mirrors findings scope surface."""

    def test_vehicle_route_service_advisor_allowed(self):
        """service-advisor can read actions by VIN."""
        with patch.object(h, "_list_actions_by_vin", return_value=([], None)):
            ev = _event(
                method="GET",
                resource="/actions/vehicle/{vin}",
                path_params={"vin": "VIN-001"},
                claims=_claims(groups=["service-advisor"]),
            )
            resp = h.handler(ev, None)
        assert resp["statusCode"] == 200
        body = _decode_body(resp)
        assert "actions" in body

    def test_vehicle_route_no_group_denied(self):
        ev = _event(
            method="GET",
            resource="/actions/vehicle/{vin}",
            path_params={"vin": "VIN-001"},
            claims=_claims(groups=[]),
        )
        resp = h.handler(ev, None)
        assert resp["statusCode"] == 403

    def test_vehicle_route_missing_vin_returns_400(self):
        ev = _event(
            method="GET",
            resource="/actions/vehicle/{vin}",
            path_params={},
            claims=_claims(groups=["service-advisor"]),
        )
        resp = h.handler(ev, None)
        assert resp["statusCode"] == 400

    def test_consumer_cannot_read_other_owner_vin_actions(self):
        """C4 + C3 mutation guard: consumer owner-A gets empty result for VIN owned by owner-B.

        The owner-filtered query (_list_actions_by_vin_for_owner) is called with
        owner_id=owner-A; it returns empty (owner-A has no actions for this VIN).
        The unfiltered query must NOT be called.
        """
        owner_a = "owner-A"
        with patch.object(h, "_list_actions_by_vin_for_owner", return_value=([], None)) as mock_filtered, \
             patch.object(h, "_list_actions_by_vin", return_value=(
                 [{"action_id": "ACT-B", "created_by": "owner-owner-B"}], None
             )) as mock_unfiltered:
            ev = _event(
                method="GET",
                resource="/actions/vehicle/{vin}",
                path_params={"vin": "VIN-001"},
                claims=_claims(sub=owner_a, groups=["consumer"]),
            )
            resp = h.handler(ev, None)

        mock_filtered.assert_called_once()
        mock_unfiltered.assert_not_called()
        assert resp["statusCode"] == 200
        assert _decode_body(resp)["actions"] == []

    def test_consumer_can_see_own_vin_actions(self):
        """consumer sees their own actions for their VIN."""
        owner_a = "owner-A"
        own_action = {"action_id": "ACT-A", "created_by": "owner-owner-A"}
        with patch.object(h, "_list_actions_by_vin_for_owner", return_value=([own_action], None)):
            ev = _event(
                method="GET",
                resource="/actions/vehicle/{vin}",
                path_params={"vin": "VIN-001"},
                claims=_claims(sub=owner_a, groups=["consumer"]),
            )
            resp = h.handler(ev, None)
        assert resp["statusCode"] == 200
        assert len(_decode_body(resp)["actions"]) == 1



    def test_owner_self_read_happy_path(self):
        with patch.object(h, "_list_actions_by_owner", return_value=([], None)):
            ev = _event(
                method="GET",
                resource="/actions/owner/{owner_id}",
                path_params={"owner_id": "user-abc"},
                claims=_claims(sub="user-abc", groups=["consumer"]),
            )
            resp = h.handler(ev, None)
        assert resp["statusCode"] == 200
        body = _decode_body(resp)
        assert body["actions"] == []

    # MUTATION-VERIFIED: widen check_owner_scope to accept any sub
    # Expected: this test fails. Reverted. ✓ CAUGHT
    def test_actions_scope_leak_owner_a_cannot_see_owner_b(self):
        """Owner A cannot list owner B's actions."""
        ev = _event(
            method="GET",
            resource="/actions/owner/{owner_id}",
            path_params={"owner_id": "owner-B"},
            claims=_claims(sub="owner-A", groups=["consumer"]),
        )
        resp = h.handler(ev, None)
        assert resp["statusCode"] == 403

    def test_w1_owner_fleet_fleet_operator_without_owner_fleet_denied(self):
        """W1: fleet-operator with any fleet_ids but no owner_fleet_id context → denied."""
        ev = _event(
            method="GET",
            resource="/actions/owner-fleet/{owner_id}",
            path_params={"owner_id": "any-owner"},
            claims=_claims(sub="op", groups=["fleet-operator"], fleet_ids=["F1"]),
        )
        resp = h.handler(ev, None)
        assert resp["statusCode"] == 403

    def test_platform_admin_can_read_any_owner(self):
        with patch.object(h, "_list_actions_by_owner", return_value=([], None)):
            ev = _event(
                method="GET",
                resource="/actions/owner/{owner_id}",
                path_params={"owner_id": "owner-B"},
                claims=_claims(sub="admin", groups=["platform-admin"]),
            )
            resp = h.handler(ev, None)
        assert resp["statusCode"] == 200


# ---------------------------------------------------------------------------
# GET /actions/ios-owner/{owner_id}
# ---------------------------------------------------------------------------

class TestActionsIosOwner:
    def test_ios_owner_self_read(self):
        with patch.object(h, "_list_actions_by_owner", return_value=([], None)):
            ev = _event(
                method="GET",
                resource="/actions/ios-owner/{owner_id}",
                path_params={"owner_id": "user-abc"},
                claims=_claims(sub="user-abc", groups=["consumer"]),
            )
            resp = h.handler(ev, None)
        assert resp["statusCode"] == 200

    def test_ios_owner_scope_leak_denied(self):
        ev = _event(
            method="GET",
            resource="/actions/ios-owner/{owner_id}",
            path_params={"owner_id": "owner-B"},
            claims=_claims(sub="owner-A", groups=["consumer"]),
        )
        resp = h.handler(ev, None)
        assert resp["statusCode"] == 403


# ---------------------------------------------------------------------------
# GET stub routes (fleet, dealership, district)
# ---------------------------------------------------------------------------

class TestActionStubRoutes:
    """T11.5: The three scope routes now return 501 (actions_scope_index_absent).

    _list_actions_by_scope was removed; the fan-out join cannot honour the
    paginated contract. Scope authorization STILL runs before the 501 — an
    unentitled caller must get 403, not 501 (T11.5 Accept 2).
    """

    # ── Entitled callers get 501 + reason code ────────────────────────────────

    def test_fleet_entitled_caller_gets_501(self):
        """Fleet-operator member gets 501 + reason code (scope GSIs absent)."""
        ev = _event(
            method="GET",
            resource="/actions/fleet/{fleet_id}",
            path_params={"fleet_id": "fleet-A"},
            claims=_claims(groups=["fleet-operator"], fleet_ids=["fleet-A"]),
        )
        resp = h.handler(ev, None)
        assert resp["statusCode"] == 501
        body = _decode_body(resp)
        assert body["error"] == "actions_scope_index_absent", (
            f"Expected reason code 'actions_scope_index_absent', got: {body.get('error')}"
        )

    def test_dealership_entitled_caller_gets_501(self):
        """Dealer member gets 501 + reason code."""
        ev = _event(
            method="GET",
            resource="/actions/dealership/{dealer_id}",
            path_params={"dealer_id": "dealer-1"},
            claims=_claims(groups=["dealer-admin"], dealer_ids=["dealer-1"]),
        )
        resp = h.handler(ev, None)
        assert resp["statusCode"] == 501
        body = _decode_body(resp)
        assert body["error"] == "actions_scope_index_absent"

    def test_district_entitled_caller_gets_501(self):
        """District-manager member gets 501 + reason code."""
        ev = _event(
            method="GET",
            resource="/actions/district/{district_id}",
            path_params={"district_id": "district-1"},
            claims=_claims(groups=["district-manager"], district_ids=["district-1"]),
        )
        resp = h.handler(ev, None)
        assert resp["statusCode"] == 501
        body = _decode_body(resp)
        assert body["error"] == "actions_scope_index_absent"

    def test_dealership_platform_admin_gets_501(self):
        """Platform-admin (dealership: bypasses membership) still gets 501."""
        ev = _event(
            method="GET",
            resource="/actions/dealership/{dealer_id}",
            path_params={"dealer_id": "dealer-1"},
            claims=_claims(groups=["platform-admin"]),
        )
        resp = h.handler(ev, None)
        assert resp["statusCode"] == 501
        body = _decode_body(resp)
        assert body["error"] == "actions_scope_index_absent"

    def test_district_platform_admin_gets_501(self):
        """Platform-admin (district: bypasses membership) still gets 501."""
        ev = _event(
            method="GET",
            resource="/actions/district/{district_id}",
            path_params={"district_id": "district-1"},
            claims=_claims(groups=["platform-admin"]),
        )
        resp = h.handler(ev, None)
        assert resp["statusCode"] == 501
        body = _decode_body(resp)
        assert body["error"] == "actions_scope_index_absent"

    # ── Unentitled callers still get 403 (scope auth runs BEFORE 501) ────────

    def test_fleet_stub_platform_admin_no_membership_denied(self):
        """Fleet asymmetry: platform-admin needs fleet membership → 403, not 501."""
        ev = _event(
            method="GET",
            resource="/actions/fleet/{fleet_id}",
            path_params={"fleet_id": "fleet-A"},
            claims=_claims(groups=["platform-admin"]),
        )
        resp = h.handler(ev, None)
        assert resp["statusCode"] == 403

    # ── Cross-scope enumeration guards (auth still runs before 501) ──────────

    def test_fleet_route_denies_district_a_manager_requesting_fleet_b(self):
        """Cross-scope: district manager cannot enumerate fleet actions → 403."""
        ev = _event(
            method="GET",
            resource="/actions/fleet/{fleet_id}",
            path_params={"fleet_id": "fleet-B"},
            claims=_claims(groups=["district-manager"], district_ids=["district-A"]),
        )
        resp = h.handler(ev, None)
        assert resp["statusCode"] == 403

    def test_fleet_route_denies_fleet_operator_wrong_fleet(self):
        """Cross-scope: fleet-operator requesting non-member fleet → 403."""
        ev = _event(
            method="GET",
            resource="/actions/fleet/{fleet_id}",
            path_params={"fleet_id": "fleet-B"},
            claims=_claims(groups=["fleet-operator"], fleet_ids=["fleet-A"]),
        )
        resp = h.handler(ev, None)
        assert resp["statusCode"] == 403

    def test_dealership_route_denies_dealer_another_dealership(self):
        """Cross-scope: dealer requesting different dealership → 403."""
        ev = _event(
            method="GET",
            resource="/actions/dealership/{dealer_id}",
            path_params={"dealer_id": "dealer-2"},
            claims=_claims(groups=["dealer-admin"], dealer_ids=["dealer-1"]),
        )
        resp = h.handler(ev, None)
        assert resp["statusCode"] == 403

    def test_district_route_denies_district_a_manager_requesting_district_b(self):
        """Cross-scope: district-A manager requesting district-B → 403."""
        ev = _event(
            method="GET",
            resource="/actions/district/{district_id}",
            path_params={"district_id": "district-B"},
            claims=_claims(groups=["district-manager"], district_ids=["district-A"]),
        )
        resp = h.handler(ev, None)
        assert resp["statusCode"] == 403


# ---------------------------------------------------------------------------
# T11.5 — 501 stubs with 403-before-501 ordering guard
# ---------------------------------------------------------------------------

class TestT11_5_ActionsScopeStubs:
    """T11.5: explicit 403-before-501 ordering proofs.

    The whole risk of T11.5 (per Accept 2) is that a stub must not become an
    information-disclosure oracle about which scopes exist. Scope authorization
    MUST run before the 501, so an unentitled caller gets 403, not 501.

    Mutations recorded in decisions.md § "2026-09-20 — T11.5":
      (a) Move 501 ahead of scope check — the 403-before-501 test MUST fail.
      (b) Stub a findings scope route too — MUST fail, proving tests distinguish surfaces.
    """

    # ── Accept 1: entitled callers get 501 + reason code ─────────────────────

    def test_fleet_entitled_gets_501_with_reason_code(self):
        """MUTATION GUARD (b): entitled fleet caller → 501 + 'actions_scope_index_absent'."""
        ev = _event(
            method="GET",
            resource="/actions/fleet/{fleet_id}",
            path_params={"fleet_id": "FLT-T11-5"},
            claims=_claims(groups=["fleet-operator"], fleet_ids=["FLT-T11-5"]),
        )
        resp = h.handler(ev, None)
        assert resp["statusCode"] == 501, (
            f"Entitled fleet caller should get 501, got {resp['statusCode']}"
        )
        body = _decode_body(resp)
        assert body["error"] == "actions_scope_index_absent", (
            f"Expected reason code 'actions_scope_index_absent', got: {body.get('error')}"
        )

    def test_dealership_entitled_gets_501_with_reason_code(self):
        """Entitled dealer → 501 + 'actions_scope_index_absent'."""
        ev = _event(
            method="GET",
            resource="/actions/dealership/{dealer_id}",
            path_params={"dealer_id": "DLR-T11-5"},
            claims=_claims(groups=["dealer-admin"], dealer_ids=["DLR-T11-5"]),
        )
        resp = h.handler(ev, None)
        assert resp["statusCode"] == 501
        body = _decode_body(resp)
        assert body["error"] == "actions_scope_index_absent"

    def test_district_entitled_gets_501_with_reason_code(self):
        """Entitled district-manager → 501 + 'actions_scope_index_absent'."""
        ev = _event(
            method="GET",
            resource="/actions/district/{district_id}",
            path_params={"district_id": "DIS-T11-5"},
            claims=_claims(groups=["district-manager"], district_ids=["DIS-T11-5"]),
        )
        resp = h.handler(ev, None)
        assert resp["statusCode"] == 501
        body = _decode_body(resp)
        assert body["error"] == "actions_scope_index_absent"

    # ── Accept 2: MUTATION GUARD (a) — 403 before 501 ────────────────────────
    # These three tests assert that an UNentitled caller gets 403, not 501.
    # If the 501 were moved ahead of the scope check, these tests would fail
    # (expecting 403, getting 501). That is the oracle regression mutation (a).

    def test_fleet_unentitled_gets_403_not_501(self):
        """MUTATION GUARD (a): unentitled fleet caller gets 403, NOT 501.

        If the 501 were placed before the scope check, this caller would see 501
        instead of 403, disclosing that the /actions/fleet scope exists. The test
        failing on a mutation that moves 501 before scope check is the required
        signal.
        """
        ev = _event(
            method="GET",
            resource="/actions/fleet/{fleet_id}",
            path_params={"fleet_id": "FLT-OTHER"},
            claims=_claims(groups=["fleet-operator"], fleet_ids=["FLT-MINE"]),
        )
        resp = h.handler(ev, None)
        assert resp["statusCode"] == 403, (
            f"Unentitled fleet caller must get 403 (scope check runs before 501), "
            f"got {resp['statusCode']} — if 501, the 501 is before scope check (oracle regression)"
        )

    def test_dealership_unentitled_gets_403_not_501(self):
        """MUTATION GUARD (a): unentitled dealer gets 403, NOT 501."""
        ev = _event(
            method="GET",
            resource="/actions/dealership/{dealer_id}",
            path_params={"dealer_id": "DLR-OTHER"},
            claims=_claims(groups=["dealer-admin"], dealer_ids=["DLR-MINE"]),
        )
        resp = h.handler(ev, None)
        assert resp["statusCode"] == 403, (
            f"Unentitled dealer must get 403 (scope check runs before 501), "
            f"got {resp['statusCode']}"
        )

    def test_district_unentitled_gets_403_not_501(self):
        """MUTATION GUARD (a): unentitled district-manager gets 403, NOT 501."""
        ev = _event(
            method="GET",
            resource="/actions/district/{district_id}",
            path_params={"district_id": "DIS-OTHER"},
            claims=_claims(groups=["district-manager"], district_ids=["DIS-MINE"]),
        )
        resp = h.handler(ev, None)
        assert resp["statusCode"] == 403, (
            f"Unentitled district-manager must get 403 (scope check runs before 501), "
            f"got {resp['statusCode']}"
        )


# ---------------------------------------------------------------------------
# PATCH /actions/{action_id} — state machine
# ---------------------------------------------------------------------------

class TestPatchActionStateMachine:
    def _patch_event(self, action_id, body, claims=None):
        return _event(
            method="PATCH",
            resource="/actions/{action_id}",
            path_params={"action_id": action_id},
            claims=claims or _claims(sub="user-sub-123"),
            body=body,
        )

    def test_valid_transition_pending_to_in_progress(self):
        action = _sample_action(status="pending_execution")
        updated = {**action, "status": "in_progress"}
        with patch.object(h, "_get_action", return_value=action), \
             patch.object(h, "_update_action_status", return_value=updated):
            resp = h.handler(
                self._patch_event("AVX-ACT-xxx", {"status": "in_progress"}),
                None,
            )
        assert resp["statusCode"] == 200
        assert _decode_body(resp)["status"] == "in_progress"

    def test_valid_transition_in_progress_to_completed(self):
        action = _sample_action(status="in_progress")
        updated = {**action, "status": "completed"}
        with patch.object(h, "_get_action", return_value=action), \
             patch.object(h, "_update_action_status", return_value=updated):
            resp = h.handler(
                self._patch_event("AVX-ACT-xxx", {"status": "completed"}),
                None,
            )
        assert resp["statusCode"] == 200

    def test_terminal_state_cannot_transition(self):
        """Completed action cannot transition to any other state."""
        for terminal in ("completed", "cancelled", "declined", "dismissed", "failed"):
            action = _sample_action(status=terminal)
            with patch.object(h, "_get_action", return_value=action):
                resp = h.handler(
                    self._patch_event("AVX-ACT-xxx", {"status": "in_progress"}),
                    None,
                )
            assert resp["statusCode"] == 400, f"Expected 400 for {terminal} → in_progress"

    def test_client_cannot_set_failed_status(self):
        """failed is executor-only; client PATCH to failed returns 403."""
        action = _sample_action(status="pending_execution")
        with patch.object(h, "_get_action", return_value=action):
            resp = h.handler(
                self._patch_event("AVX-ACT-xxx", {"status": "failed"}),
                None,
            )
        assert resp["statusCode"] == 403

    def test_client_cannot_set_dismissed_via_patch(self):
        """dismissed is only via the /dismiss route."""
        action = _sample_action(status="pending_execution")
        with patch.object(h, "_get_action", return_value=action):
            resp = h.handler(
                self._patch_event("AVX-ACT-xxx", {"status": "dismissed"}),
                None,
            )
        assert resp["statusCode"] == 400

    def test_unknown_status_returns_400(self):
        action = _sample_action(status="pending_execution")
        with patch.object(h, "_get_action", return_value=action):
            resp = h.handler(
                self._patch_event("AVX-ACT-xxx", {"status": "bogus-status"}),
                None,
            )
        assert resp["statusCode"] == 400

    def test_missing_action_returns_404(self):
        with patch.object(h, "_get_action", return_value=None):
            resp = h.handler(
                self._patch_event("AVX-ACT-missing", {"status": "in_progress"}),
                None,
            )
        assert resp["statusCode"] == 404

    def test_scope_check_denies_different_owner(self):
        """Owner A cannot transition owner B's action."""
        action = _sample_action(status="pending_execution", created_by="owner-owner-B")
        with patch.object(h, "_get_action", return_value=action):
            resp = h.handler(
                self._patch_event(
                    "AVX-ACT-xxx",
                    {"status": "in_progress"},
                    claims=_claims(sub="owner-A", groups=[]),
                ),
                None,
            )
        assert resp["statusCode"] == 403

    def test_w3_fleet_operator_cannot_patch_out_of_fleet_action(self):
        """W3: fleet-operator must hold the action's fleet_id to PATCH it."""
        action = {**_sample_action(status="pending_execution"), "fleet_id": "fleet-X"}
        with patch.object(h, "_get_action", return_value=action):
            resp = h.handler(
                self._patch_event(
                    "AVX-ACT-xxx",
                    {"status": "in_progress"},
                    claims=_claims(sub="op", groups=["fleet-operator"], fleet_ids=["fleet-Y"]),
                ),
                None,
            )
        assert resp["statusCode"] == 403

    def test_w3_fleet_operator_can_patch_in_fleet_action(self):
        """W3: fleet-operator holding the action's fleet_id may PATCH."""
        action = {**_sample_action(status="pending_execution"), "fleet_id": "fleet-X"}
        updated = {**action, "status": "in_progress"}
        with patch.object(h, "_get_action", return_value=action), \
             patch.object(h, "_update_action_status", return_value=updated):
            resp = h.handler(
                self._patch_event(
                    "AVX-ACT-xxx",
                    {"status": "in_progress"},
                    claims=_claims(sub="op", groups=["fleet-operator"], fleet_ids=["fleet-X"]),
                ),
                None,
            )
        assert resp["statusCode"] == 200

    @pytest.mark.untrusted_claims
    def test_fg29_untrusted_fleet_claim_cannot_patch_even_its_own_fleet(self):
        """Fix Group 29: the eighth gated site, and the only one outside `_lib`.

        This PATCH check read `custom:fleetIds` directly in this handler, so it was not
        in review cycle 3's list of seven (that list was scoped to `_lib/avx_scope.py`
        plus the findings handler). Found by running the "what ELSE reads this claim"
        grep across all of `lambdas/` and `agents/` — the lesson cycle 3 recorded, and
        this is its first application.

        An ungated read let a fleet-operator forge membership of the action's fleet and
        PATCH it. Untrusted now yields `[]`, so `action_fleet_id not in []` is True and
        the 403 fires. Note this is the SAME claim and env as
        `test_w3_fleet_operator_can_patch_in_fleet_action` above, which returns 200 —
        the only difference is the trust flag, which is what makes this a gate test
        rather than a scope test.
        """
        action = {**_sample_action(status="pending_execution"), "fleet_id": "fleet-X"}
        with patch.object(h, "_get_action", return_value=action), \
             patch.object(h, "_update_action_status") as mock_update:
            resp = h.handler(
                self._patch_event(
                    "AVX-ACT-xxx",
                    {"status": "in_progress"},
                    claims=_claims(sub="op", groups=["fleet-operator"], fleet_ids=["fleet-X"]),
                ),
                None,
            )
        assert resp["statusCode"] == 403, (
            f"an untrusted fleet claim must not authorize a PATCH; got {resp}"
        )
        mock_update.assert_not_called(), "the write must not happen"


# ---------------------------------------------------------------------------
# POST /actions/{action_id}/dismiss
# ---------------------------------------------------------------------------

class TestDismissRoute:
    def _dismiss_event(self, action_id, claims=None):
        return _event(
            method="POST",
            resource="/actions/{action_id}/dismiss",
            path_params={"action_id": action_id},
            claims=claims or _claims(sub="user-sub-123"),
        )

    def _make_feedback_table(self):
        feedback_table = MagicMock()
        feedback_table.put_item.return_value = {}
        return feedback_table

    # MUTATION-VERIFIED (b): collapse dismiss to write `cancelled` instead of `dismissed`
    # Expected: this test fails. Reverted. ✓ CAUGHT
    def test_dismiss_route_transitions_to_dismissed(self):
        """POST dismiss on a pending_execution action results in status=dismissed."""
        action = _sample_action(status="pending_execution")
        updated = {**action, "status": "dismissed", "audit_trail": [
            {"at": "2026-09-19T12:00:00Z", "from_status": "pending_execution",
             "to_status": "dismissed", "actor": "owner-user-sub-123"}
        ]}
        with patch.object(h, "_get_action", return_value=action), \
             patch.object(h, "_update_action_status", return_value=updated), \
             patch.object(h, "_load_finding_by_id", return_value={"finding_kind": "health.tires"}), \
             patch.object(h, "_write_feedback_row") as mock_feedback:
            resp = h.handler(self._dismiss_event("AVX-ACT-xxx"), None)

        assert resp["statusCode"] == 200
        body = _decode_body(resp)
        # MUST be dismissed, not cancelled
        assert body["status"] == "dismissed"
        # Feedback row written
        mock_feedback.assert_called_once()

    # MUTATION-VERIFIED (b): second assertion that audit_trail records dismissed not cancelled
    # Expected: this test fails when dismissed is replaced with cancelled. ✓ CAUGHT
    def test_dismissed_is_distinct_from_cancelled_in_audit_trail(self):
        """Audit trail must record to_status=dismissed, not to_status=cancelled."""
        action = _sample_action(status="pending_execution")
        # _update_action_status is called with to_status="dismissed"
        with patch.object(h, "_get_action", return_value=action), \
             patch.object(h, "_update_action_status") as mock_update, \
             patch.object(h, "_load_finding_by_id", return_value={"finding_kind": "health.tires"}), \
             patch.object(h, "_write_feedback_row"):
            mock_update.return_value = {**action, "status": "dismissed"}
            h.handler(self._dismiss_event("AVX-ACT-xxx"), None)

        # Verify the to_status passed to _update_action_status was "dismissed" not "cancelled"
        call_kwargs = mock_update.call_args
        assert call_kwargs.kwargs.get("to_status") == "dismissed" or \
               (len(call_kwargs.args) > 2 and call_kwargs.args[2] == "dismissed"), \
               f"Expected to_status='dismissed', got call: {call_kwargs}"

    def test_dismiss_already_dismissed_is_idempotent(self):
        action = _sample_action(status="dismissed")
        with patch.object(h, "_get_action", return_value=action):
            resp = h.handler(self._dismiss_event("AVX-ACT-xxx"), None)
        assert resp["statusCode"] == 200
        body = _decode_body(resp)
        assert body["status"] == "dismissed"

    def test_dismiss_writes_feedback_row(self):
        action = _sample_action(status="pending_execution",
                                finding_id="FIND-00000000-0000-0000-0000-000000000001")
        updated = {**action, "status": "dismissed"}
        finding = {"finding_kind": "health.tires"}
        with patch.object(h, "_get_action", return_value=action), \
             patch.object(h, "_update_action_status", return_value=updated), \
             patch.object(h, "_load_finding_by_id", return_value=finding), \
             patch.object(h, "_write_feedback_row") as mock_feedback:
            resp = h.handler(self._dismiss_event("AVX-ACT-xxx"), None)

        assert resp["statusCode"] == 200
        mock_feedback.assert_called_once()
        # finding_kind should be passed to feedback
        call_args = mock_feedback.call_args
        assert call_args.kwargs.get("finding_kind") == "health.tires" or \
               "health.tires" in str(call_args)

    def test_w6_dismiss_feedback_uses_action_owner_not_caller(self):
        """W6: when platform-admin dismisses, feedback owner_id = action's owner, not admin's sub.

        Mutation: use caller_sub instead of owner_part for feedback attribution.
        Expected: feedback is called with owner_id='admin-sub' (wrong) → test fails.
        ✓ CAUGHT when mutation applied; test passes with correct attribution.
        """
        action = _sample_action(
            status="pending_execution",
            created_by="owner-the-real-owner",  # actual owner
        )
        updated = {**action, "status": "dismissed"}
        finding = {"finding_kind": "health.battery"}

        with patch.object(h, "_get_action", return_value=action), \
             patch.object(h, "_update_action_status", return_value=updated), \
             patch.object(h, "_load_finding_by_id", return_value=finding), \
             patch.object(h, "_write_feedback_row") as mock_feedback:
            # platform-admin is the caller — NOT the action's owner
            resp = h.handler(
                self._dismiss_event(
                    "AVX-ACT-xxx",
                    claims=_claims(sub="admin-sub", groups=["platform-admin"]),
                ),
                None,
            )

        assert resp["statusCode"] == 200
        mock_feedback.assert_called_once()
        call_kwargs = mock_feedback.call_args.kwargs
        # feedback owner_id MUST be the action's owner ('the-real-owner'), NOT 'admin-sub'
        assert call_kwargs.get("owner_id") == "the-real-owner", \
            f"Expected owner_id='the-real-owner', got {call_kwargs}"

    def test_dismiss_returns_404_for_missing_action(self):
        with patch.object(h, "_get_action", return_value=None):
            resp = h.handler(self._dismiss_event("AVX-ACT-missing"), None)
        assert resp["statusCode"] == 404

    def test_dismiss_scope_check_denies_different_owner(self):
        action = _sample_action(created_by="owner-owner-B", status="pending_execution")
        with patch.object(h, "_get_action", return_value=action):
            resp = h.handler(
                self._dismiss_event(
                    "AVX-ACT-xxx",
                    claims=_claims(sub="owner-A", groups=[]),
                ),
                None,
            )
        assert resp["statusCode"] == 403


# ---------------------------------------------------------------------------
# State machine completeness
# ---------------------------------------------------------------------------

class TestStateMachineCompleteness:
    def test_all_statuses_defined(self):
        expected = {
            "pending_execution", "executed", "in_progress", "completed",
            "cancelled", "declined", "dismissed", "failed",
        }
        assert h._ALL_STATUSES == expected

    def test_terminals_defined(self):
        expected = {"completed", "cancelled", "declined", "dismissed", "failed"}
        assert h._TERMINAL_STATUSES == expected

    def test_failed_is_executor_only(self):
        assert "failed" in h._EXECUTOR_ONLY_TARGET_STATUSES

    def test_can_transition_from_terminal_is_false(self):
        for terminal in h._TERMINAL_STATUSES:
            assert not h._can_transition(terminal, "in_progress"), \
                f"Expected False for transition from terminal {terminal}"

    def test_can_transition_to_failed_via_patch_is_false(self):
        assert not h._can_transition("pending_execution", "failed")

    def test_can_transition_pending_to_in_progress(self):
        assert h._can_transition("pending_execution", "in_progress") is True

    def test_can_transition_in_progress_to_completed(self):
        assert h._can_transition("in_progress", "completed") is True

    def test_can_transition_pending_to_cancelled(self):
        assert h._can_transition("pending_execution", "cancelled") is True


# ---------------------------------------------------------------------------
# W9: State-machine transition coverage — all 8 statuses × valid targets
# ---------------------------------------------------------------------------

class TestStateMachineTransitionCoverage:
    """W9: Covers the 7 missing transitions from the review cycle 1 findings.

    Explicit cases:
      - pending_execution → executed   (allowed)
      - executed → completed           (allowed)
      - executed → in_progress         (allowed — spec only requires terminal monotonicity)
      - pending_execution → declined   (allowed)
      - in_progress → cancelled        (allowed)
      - in_progress → declined         (allowed)
      - pending_execution → cancelled  (allowed, already tested above; verified again)
      - executed → cancelled           (allowed)
    """

    def _patch_event(self, action_id, body, claims=None):
        return _event(
            method="PATCH",
            resource="/actions/{action_id}",
            path_params={"action_id": action_id},
            claims=claims or _claims(sub="user-sub-123"),
            body=body,
        )

    def test_pending_to_executed(self):
        """pending_execution → executed is a valid PATCH transition."""
        action = _sample_action(status="pending_execution")
        updated = {**action, "status": "executed"}
        with patch.object(h, "_get_action", return_value=action), \
             patch.object(h, "_update_action_status", return_value=updated):
            resp = h.handler(
                self._patch_event("ACT-1", {"status": "executed"}), None
            )
        assert resp["statusCode"] == 200
        assert _decode_body(resp)["status"] == "executed"

    def test_executed_to_completed(self):
        """executed → completed is a valid PATCH transition."""
        action = _sample_action(status="executed")
        updated = {**action, "status": "completed"}
        with patch.object(h, "_get_action", return_value=action), \
             patch.object(h, "_update_action_status", return_value=updated):
            resp = h.handler(
                self._patch_event("ACT-2", {"status": "completed"}), None
            )
        assert resp["statusCode"] == 200

    def test_executed_to_in_progress_is_allowed(self):
        """executed → in_progress: spec only forbids terminal→anything; non-terminal→non-terminal is allowed."""
        assert h._can_transition("executed", "in_progress") is True
        action = _sample_action(status="executed")
        updated = {**action, "status": "in_progress"}
        with patch.object(h, "_get_action", return_value=action), \
             patch.object(h, "_update_action_status", return_value=updated):
            resp = h.handler(
                self._patch_event("ACT-3", {"status": "in_progress"}), None
            )
        assert resp["statusCode"] == 200

    def test_pending_to_declined(self):
        """pending_execution → declined is allowed."""
        action = _sample_action(status="pending_execution")
        updated = {**action, "status": "declined"}
        with patch.object(h, "_get_action", return_value=action), \
             patch.object(h, "_update_action_status", return_value=updated):
            resp = h.handler(
                self._patch_event("ACT-4", {"status": "declined"}), None
            )
        assert resp["statusCode"] == 200

    def test_in_progress_to_cancelled(self):
        """in_progress → cancelled is allowed."""
        action = _sample_action(status="in_progress")
        updated = {**action, "status": "cancelled"}
        with patch.object(h, "_get_action", return_value=action), \
             patch.object(h, "_update_action_status", return_value=updated):
            resp = h.handler(
                self._patch_event("ACT-5", {"status": "cancelled"}), None
            )
        assert resp["statusCode"] == 200

    def test_in_progress_to_declined(self):
        """in_progress → declined is allowed."""
        action = _sample_action(status="in_progress")
        updated = {**action, "status": "declined"}
        with patch.object(h, "_get_action", return_value=action), \
             patch.object(h, "_update_action_status", return_value=updated):
            resp = h.handler(
                self._patch_event("ACT-6", {"status": "declined"}), None
            )
        assert resp["statusCode"] == 200

    def test_executed_to_cancelled(self):
        """executed → cancelled is allowed."""
        action = _sample_action(status="executed")
        updated = {**action, "status": "cancelled"}
        with patch.object(h, "_get_action", return_value=action), \
             patch.object(h, "_update_action_status", return_value=updated):
            resp = h.handler(
                self._patch_event("ACT-7", {"status": "cancelled"}), None
            )
        assert resp["statusCode"] == 200

    def test_can_transition_all_non_terminals_to_executed(self):
        """Non-terminal statuses can transition to executed."""
        non_terminals = h._ALL_STATUSES - h._TERMINAL_STATUSES
        for src in non_terminals:
            assert h._can_transition(src, "executed"), \
                f"Expected True for {src} → executed"

    def test_all_terminal_statuses_block_all_transitions(self):
        """Every terminal status blocks every transition attempt."""
        for terminal in h._TERMINAL_STATUSES:
            for target in h._ALL_STATUSES:
                assert not h._can_transition(terminal, target), \
                    f"Expected False for terminal {terminal} → {target}"



# ---------------------------------------------------------------------------
# W-C3-fleet: fleet-dimension scope guard on /actions/vehicle/{vin}
# ---------------------------------------------------------------------------

class TestFleetVehicleActionsScopeGuard:
    """W-C3-fleet fix (2026-09-19 fix cycle 2): fleet callers must only see actions
    for VINs in their own fleets.

    Mutation procedure (recorded 2026-09-19 fix cycle 2):
      Mutation A: replace `_list_actions_by_vin_for_fleet` call with unfiltered
        `_list_actions_by_vin` in handler's elif is_fleet_caller branch.
        Result: test_fleet_operator_cannot_enum_out_of_fleet_vin FAILS.
        Mutation reverted. ✓ CAUGHT

      Mutation B: in is_fleet_vehicle_caller, return (True, fleet_ids) even for
        platform-admin (remove the privileged-role short-circuit).
        Result: test_platform_admin_sees_all_vehicle_actions_unfiltered FAILS.
        Mutation reverted. ✓ CAUGHT
    """

    def test_fleet_operator_cannot_enum_out_of_fleet_vin(self):
        """T12.1: fleet-operator gets unconditional 503 for all VINs (actions handler).

        Updated from W-C3-fleet: the probe is gone. Fleet-caller paths return 503
        regardless of which VIN or fleet. See TestT12_1_ActionsAntiOracle.
        """
        event = _event(
            method="GET",
            resource="/actions/vehicle/{vin}",
            path_params={"vin": "VIN-101"},
            claims=_claims(sub="op-fleet-a", groups=["fleet-operator"], fleet_ids=["FLT-A"]),
        )
        resp = h.handler(event, None)
        assert resp["statusCode"] == 503
        body = json.loads(resp["body"])
        assert body["error"] == "fleet_scope_unavailable"

    def test_fleet_driver_cannot_enum_out_of_fleet_vin(self):
        """T12.1: fleet-driver gets unconditional 503 for all VINs (actions handler)."""
        event = _event(
            method="GET",
            resource="/actions/vehicle/{vin}",
            path_params={"vin": "VIN-102"},
            claims=_claims(sub="driver-fleet-a", groups=["fleet-driver"], fleet_ids=["FLT-A"]),
        )
        resp = h.handler(event, None)
        assert resp["statusCode"] == 503
        assert json.loads(resp["body"])["error"] == "fleet_scope_unavailable"

    def test_fleet_operator_in_matching_fleet_sees_vin_actions(self):
        """T12.1: fleet-operator still gets 503 even for VINs in their own fleet.

        Updated from W-C3-fleet: unconditional 503 until fleet_id projection lands.
        The coupling test (TestT12_1_ActionsAntiOracle) will signal when to re-enable.
        """
        event = _event(
            method="GET",
            resource="/actions/vehicle/{vin}",
            path_params={"vin": "VIN-103"},
            claims=_claims(sub="op-fleet-a", groups=["fleet-operator"], fleet_ids=["FLT-A"]),
        )
        resp = h.handler(event, None)
        assert resp["statusCode"] == 503
        assert json.loads(resp["body"])["error"] == "fleet_scope_unavailable"

    def test_fleet_operator_with_no_fleet_ids_is_denied(self):
        """Fleet-operator with empty custom:fleetIds is denied (fail-closed)."""
        with patch.object(h, "_list_actions_by_vin", return_value=([], None)) as mock_unfiltered, \
             patch.object(h, "_list_actions_by_vin_for_fleet", return_value=([], None)) as mock_fleet:
            event = _event(
                method="GET",
                resource="/actions/vehicle/{vin}",
                path_params={"vin": "VIN-104"},
                claims=_claims(sub="op-no-fleet", groups=["fleet-operator"], fleet_ids=[]),
            )
            resp = h.handler(event, None)

        mock_unfiltered.assert_not_called()
        mock_fleet.assert_not_called()
        assert resp["statusCode"] == 403

    def test_platform_admin_sees_all_vehicle_actions_unfiltered(self):
        """platform-admin bypasses fleet filtering entirely.

        MUTATION GUARD: if is_fleet_vehicle_caller returns (True, fleet_ids) for
        platform-admin, the admin would be routed to fleet-filtered query — this test
        FAILS because _list_actions_by_vin would not be called.
        """
        admin_actions = [
            {"action_id": "AVX-ACT-ADMIN", "scope": {"vin": "VIN-105", "fleet_id": "FLT-X"}},
        ]
        with patch.object(h, "_list_actions_by_vin", return_value=(admin_actions, None)) as mock_unfiltered, \
             patch.object(h, "_list_actions_by_vin_for_fleet", return_value=([], None)) as mock_fleet:
            event = _event(
                method="GET",
                resource="/actions/vehicle/{vin}",
                path_params={"vin": "VIN-105"},
                claims=_claims(sub="admin-sub", groups=["platform-admin"]),
            )
            resp = h.handler(event, None)

        mock_unfiltered.assert_called_once()
        mock_fleet.assert_not_called()
        assert resp["statusCode"] == 200
        assert len(json.loads(resp["body"])["actions"]) == 1



# ---------------------------------------------------------------------------
# T11.2 — Honest 503 for unscopeable VIN paths (actions handler)
# ---------------------------------------------------------------------------




# ---------------------------------------------------------------------------
# T12.1 — Anti-oracle: VIN with findings and VIN without produce identical responses
# (actions handler)
# ---------------------------------------------------------------------------

class TestT12_1_ActionsAntiOracle:
    """T12.1 anti-oracle for the actions handler: a VIN with Findings and a VIN
    without produce IDENTICAL responses for a fleet-caller.

    The original T11.2 probe leaked one bit of cross-tenant existence on the actions
    surface: 200 [] = this VIN has no findings; 503 = it has some. Any fleet-bearing
    caller could use this to enumerate VIN existence across tenants.

    T12.1 closes this by returning 503 unconditionally for the fleet-caller VIN path.

    Mutation (a) (recorded in decisions.md):
      Restore the probe: _probe returns None → 200 []; False → 503.
      Expected: test_fleet_vin_with_findings_and_without_are_identical FAILS. ✓ CAUGHT
    """

    def test_fleet_vin_with_findings_and_without_are_identical(self):
        """MUTATION GUARD (a): fleet-caller gets byte-identical responses for a
        VIN-with-findings and a VIN-without-findings.

        Both must return 503 + reason 'fleet_scope_unavailable'.
        If the probe were restored, VIN-without-findings returns 200 [] and
        VIN-with-findings returns 503 — this test FAILS.
        """
        fleet_claims = _claims(sub="driver-T12-actions", groups=["fleet-driver"], fleet_ids=["FLT-T12"])

        resp_empty = h.handler(_event(
            method="GET",
            resource="/actions/vehicle/{vin}",
            path_params={"vin": "VIN-ACTIONS-HAS-NO-FINDINGS"},
            claims=fleet_claims,
        ), None)

        resp_has = h.handler(_event(
            method="GET",
            resource="/actions/vehicle/{vin}",
            path_params={"vin": "VIN-ACTIONS-HAS-SOME-FINDINGS"},
            claims=fleet_claims,
        ), None)

        assert resp_empty["statusCode"] == 503, (
            f"VIN-with-no-findings returned {resp_empty['statusCode']}, expected 503"
        )
        assert resp_has["statusCode"] == 503, (
            f"VIN-with-findings returned {resp_has['statusCode']}, expected 503"
        )

        body_empty = json.loads(resp_empty["body"])
        body_has = json.loads(resp_has["body"])
        assert body_empty == body_has, (
            f"Actions responses differ: no-findings={body_empty!r}, "
            f"has-findings={body_has!r}. "
            "Any difference re-opens the cross-tenant existence oracle."
        )
        assert body_empty["error"] == "fleet_scope_unavailable"

    def test_fleet_caller_with_no_fleet_ids_gets_403_not_503(self):
        """Fleet-caller with no fleet_ids gets 403 BEFORE the 503.

        MUTATION GUARD (c): if the 503 were moved before the entitlement check,
        this test FAILS.
        """
        event = _event(
            method="GET",
            resource="/actions/vehicle/{vin}",
            path_params={"vin": "VIN-ANY"},
            claims=_claims(sub="driver-noflt", groups=["fleet-driver"], fleet_ids=[]),
        )
        resp = h.handler(event, None)
        assert resp["statusCode"] == 403, (
            f"Fleet caller with no fleet_ids must get 403 (entitlement failure), "
            f"not 503 (scope unavailable). Got: {resp['statusCode']}. "
            "The 503 must come AFTER the entitlement check."
        )

    def test_service_advisor_vin_actions_still_returns_data(self):
        """Service-advisor VIN path is unaffected by T12.1 and still returns data."""
        with patch.object(h, "_list_actions_by_vin", return_value=([], None)) as mock_svc:
            event = _event(
                method="GET",
                resource="/actions/vehicle/{vin}",
                path_params={"vin": "VIN-SVC-T12"},
                claims=_claims(sub="svc-t12-actions", groups=["service-advisor"]),
            )
            resp = h.handler(event, None)
        assert resp["statusCode"] == 200, (
            f"Service path must still return 200 — T12.1 must not disable it. "
            f"Got: {resp['statusCode']}"
        )
        mock_svc.assert_called_once()


# ---------------------------------------------------------------------------
# T14.5 — Action audit-entry schema validation (W-E)
#
# T11.1's Verify required response bodies to be validated against BOTH
# finding.schema.json AND action.schema.json.  The action.schema.json half
# was never written.  This class is the missing half for the actions handler.
#
# Tests exercise the REAL _update_action_status code path (not a mock of it)
# by patching _get_ddb at the DDB layer and inspecting what is passed to
# update_item.  This is the only way to catch the audit-entry shape the
# handler actually builds.
#
# Violations at time of authoring (T14.5 red phase):
#   (1) "actor" present — additionalProperties:false rejects it
#   (2) "by" absent  — required field missing
#
# from_status is also required and must be a valid status-enum string.
# For dismiss and PATCH, from_status comes from the existing action's status
# (a real string) so no None is sent on those routes.
#
# Mutation verification results (T14.5):
#   (a) revert by → actor in _update_action_status: CAUGHT — 2 tests fail
#   (b) set from_status=None in _update_action_status: CAUGHT — 2 tests fail
#   (c) well-formed entry validated: PASSES — anti-vacuity confirmed
# ---------------------------------------------------------------------------

import pathlib as _pl
import json as _json
import jsonschema as _jsonschema

_ACTIONS_SCHEMA_PATH = (
    _pl.Path(__file__).parent.parent.parent.parent
    / "agents" / "tier2" / "schemas" / "action.schema.json"
)
_ACTION_SCHEMA = _json.loads(_ACTIONS_SCHEMA_PATH.read_text())


def _make_schema_valid_audit_entry(from_status="pending_execution", to_status="in_progress",
                                   by="owner-user-sub-123") -> dict:
    """Build a schema-valid audit entry with 'by' (not 'actor')."""
    return {
        "at": "2026-09-19T12:00:00Z",
        "by": by,
        "from_status": from_status,
        "to_status": to_status,
    }


class TestT14_5_ActionAuditEntrySchemaValidation:
    """T14.5 — verify that audit entries produced by dismiss and PATCH use
    'by' (not 'actor') and carry valid from_status values.

    The tests call _update_action_status directly with a mocked DDB table
    and inspect what was passed to update_item.  The audit entry extracted
    from the ':entry' ExpressionAttributeValue is then validated against
    action.schema.json's $defs/audit_entry sub-schema.

    Running these tests BEFORE the actor→by fix produces:
      ValidationError: Additional properties are not allowed ('actor' was unexpected)
    """

    def _call_update_and_get_audit_entry(
        self,
        from_status: str = "pending_execution",
        to_status: str = "in_progress",
        actor: str = "owner-user-sub-123",
    ) -> dict:
        """Call _update_action_status with a mocked DDB and extract the audit entry
        it built.  The conditional check is bypassed by making the mock succeed."""
        import botocore.exceptions
        mock_table = MagicMock()
        mock_table.update_item.return_value = {"Attributes": {
            "action_id": "AVX-ACT-aabbccdd-0000-0000-0000-000000000001",
            "finding_id": "FIND-00000000-0000-0000-0000-000000000001",
            "created_by": actor if actor.startswith("owner-") else f"owner-{actor}",
            "created_at": "2026-09-19T12:00:00Z",
            "updated_at": "2026-09-19T12:05:00Z",
            "target_system": "dms",
            "status": to_status,
            "action_kind": "book_combined_visit",
            "parameters": {},
            "evidence": [],
            "audit_trail": [],
        }}
        mock_table.meta.client.exceptions.ConditionalCheckFailedException = (
            type("ConditionalCheckFailedException",
                 (botocore.exceptions.ClientError,), {})
        )

        with patch.object(h, "_get_ddb") as mock_ddb:
            mock_ddb.return_value.Table.return_value = mock_table
            h._update_action_status(
                action_id="AVX-ACT-aabbccdd-0000-0000-0000-000000000001",
                from_status=from_status,
                to_status=to_status,
                actor=actor,
            )

        # Extract the audit entry the handler built and passed to DDB
        call_kwargs = mock_table.update_item.call_args.kwargs
        expr_values = call_kwargs["ExpressionAttributeValues"]
        # ":entry" is a list with one dict — the audit entry
        entries = expr_values[":entry"]
        assert len(entries) == 1, f"Expected 1 audit entry, got {len(entries)}"
        return entries[0]

    def test_dismiss_audit_entry_validates_against_action_schema(self):
        """Dismiss route's audit entry must use 'by', not 'actor'.

        T14.5 red phase: FAILS before actor→by is fixed.
        The $defs/audit_entry sub-schema has additionalProperties:false and
        requires 'by'; an entry with 'actor' fails on both counts.
        """
        entry = self._call_update_and_get_audit_entry(
            from_status="pending_execution",
            to_status="dismissed",
        )
        audit_entry_schema = _ACTION_SCHEMA["$defs"]["audit_entry"]
        try:
            _jsonschema.validate(entry, audit_entry_schema)
        except _jsonschema.ValidationError as exc:
            raise AssertionError(
                f"dismiss audit entry fails schema validation: {exc.message}\n"
                f"  entry = {entry!r}\n"
                "  Fix: rename 'actor' key to 'by' in _update_action_status."
            ) from exc

    def test_patch_audit_entry_validates_against_action_schema(self):
        """PATCH route's audit entry must use 'by', not 'actor'.

        T14.5 red phase: FAILS before actor→by is fixed.
        """
        entry = self._call_update_and_get_audit_entry(
            from_status="pending_execution",
            to_status="in_progress",
        )
        audit_entry_schema = _ACTION_SCHEMA["$defs"]["audit_entry"]
        try:
            _jsonschema.validate(entry, audit_entry_schema)
        except _jsonschema.ValidationError as exc:
            raise AssertionError(
                f"PATCH audit entry fails schema validation: {exc.message}\n"
                f"  entry = {entry!r}\n"
                "  Fix: rename 'actor' key to 'by' in _update_action_status."
            ) from exc

    def test_well_formed_audit_entry_passes_schema(self):
        """Anti-vacuity: a schema-valid audit entry passes validation.

        Mutation guard (c): if this test fails for a reason other than 'actor',
        the validation is broken for an unrelated reason.
        """
        valid_entry = _make_schema_valid_audit_entry()
        audit_entry_schema = _ACTION_SCHEMA["$defs"]["audit_entry"]
        _jsonschema.validate(valid_entry, audit_entry_schema)  # must not raise



# ---------------------------------------------------------------------------
# T14.4 — FilterExpression shape pin for _list_actions_by_vin_for_fleet
#
# This helper is dead code: its calling route returns 503 unconditionally until
# the T11.6 follow-on (b) projection lands.  The test pins the FilterExpression
# shape so that:
#   (a) the pre-T14.4 false docstring/comment (which claimed scope.fleet_id was
#       used) cannot silently regress the code back to the nested shape, and
#   (b) mutation (a) — reverting the docstring/comment to something claiming
#       scope.fleet_id IS used — would expose the discrepancy between docs and
#       code, which is the exact defect T14.4 closes on the actions side.
#
# The actions handler was ALREADY using Attr("fleet_id") (top-level) before T14.4;
# T14.4 fixed its docstring and comment to match.  This test pins the code so
# that any future accidental revert to Attr("scope.fleet_id") fails loudly.
# ---------------------------------------------------------------------------

class TestT14_4_ActionsFilterExpressionShapePin:
    """T14.4: pin the FilterExpression shape of _list_actions_by_vin_for_fleet.

    Mutation (a) probe: this test MUST fail if the FilterExpression is reverted
    to Attr("scope.fleet_id").is_in(...) (the pre-T14.4 false docstring's claim).
    Recorded in decisions.md.
    """

    def _capture_query_kwargs(self, fn, *args, **kwargs):
        """Call fn(*args, **kwargs) with DynamoDB patched; return the captured
        kwargs dict that was passed to findings_table.query() (step 1)."""
        captured = {}

        class _MockTable:
            def query(self_, **kw):
                captured.update(kw)
                return {"Items": [], "Count": 0}

        with patch.object(h, "_get_ddb") as mock_ddb:
            mock_ddb.return_value.Table.return_value = _MockTable()
            fn(*args, **kwargs)

        return captured

    def test_actions_by_vin_for_fleet_uses_top_level_fleet_id(self):
        """_list_actions_by_vin_for_fleet uses Attr("fleet_id").is_in(caller_fleet_ids).

        T14.4: the docstring and inline comment previously claimed the filter used
        scope.fleet_id (nested), but the code already used Attr("fleet_id") (top-level).
        T14.4 corrected the docs; this test pins the code.

        After T14.4, both handlers agree:
          findings: _list_findings_by_vin_for_fleet → Attr("fleet_id").is_in(...)
          actions:  _list_actions_by_vin_for_fleet  → Attr("fleet_id").is_in(...)

        Mutation (a): change to Attr("scope.fleet_id").is_in(...) — this test MUST
        fail because the captured FilterExpression's Attr.name will be 'scope.fleet_id'
        instead of 'fleet_id', proving the handlers diverged.
        """
        captured = self._capture_query_kwargs(
            h._list_actions_by_vin_for_fleet, "VIN-TEST", ["FLEET-A", "FLEET-B"]
        )

        assert "FilterExpression" in captured, (
            "Expected a FilterExpression in query kwargs for step-1 findings query; "
            "none was passed. _list_actions_by_vin_for_fleet may have changed structure."
        )
        fe = captured["FilterExpression"]
        fe_expr = fe.get_expression()

        # Extract the Attr name from the expression values (IN expression: values[0] is Attr)
        attr_obj = fe_expr["values"][0]
        attr_name = getattr(attr_obj, "name", None)

        # Must NOT use the nested path (what the false pre-T14.4 docstring described)
        assert attr_name != "scope.fleet_id", (
            f"T14.4 shape regression: FilterExpression attribute name is 'scope.fleet_id' "
            f"(pre-T14.4 false docstring's claim). Must be top-level 'fleet_id'.\n"
            "The actions handler must agree with the findings handler: both use "
            "Attr(\"fleet_id\").is_in(caller_fleet_ids) (top-level, the shape the "
            "T11.6 follow-on (b) projection will write and the by_fleet GSI reads)."
        )

        # Must use the top-level attribute
        assert attr_name == "fleet_id", (
            f"T14.4 shape mismatch: FilterExpression attribute name is {attr_name!r}, "
            f"expected 'fleet_id' (top-level)."
        )



# ---------------------------------------------------------------------------
# T14.3 — Fix two defects on GET /actions/vehicle/{vin}
#
# Defect 1 (service/admin branch, W-C): _list_actions_by_vin's inner by_finding
# queries dropped LastEvaluatedKey, silently truncating actions for findings that
# span multiple DynamoDB pages.  Fix: follow LastEvaluatedKey to completion for
# each finding_id.  Invariant: next_token: None <=> all findings AND all their
# actions have been returned.
#
# Defect 2 (owner branch, W-F): _list_actions_by_vin_for_owner post-filtered on
# item.get("scope", {}).get("vin") == vin or item.get("vin") == vin, but no writer
# sets either field on Action rows.  200 [] was returned for every owner+VIN
# combination regardless of what actions exist.  Fix: 503 owner_scope_unavailable,
# same pattern as T12.1 fleet_scope_unavailable.
#
# Three mutations (recorded in decisions.md § "2026-09-20 — T14.3"):
#   (a) Service branch: restore single-page inner query (drop while loop) —
#       test_service_vin_next_token_none_only_when_all_actions_fetched FAILS.
#   (b) Owner branch: restore post-filter returning 200 [] —
#       test_owner_vin_returns_503_not_200_empty FAILS.
#   (c) Owner branch: move 503 before entitlement check —
#       test_owner_vin_no_entitlement_gets_403_not_503 FAILS.
# ---------------------------------------------------------------------------


class TestT14_3_ServiceBranchInnerPagination:
    """T14.3 (W-C): _list_actions_by_vin follows inner LastEvaluatedKey so a
    paginated by_finding response is never silently truncated.

    The function is tested at unit level by mocking _get_ddb().  The mock simulates
    a finding with two action pages: the first page returns LastEvaluatedKey, the
    second page returns the remaining items without LastEvaluatedKey.
    """

    def _make_ddb_mock(self, findings_items, actions_pages_by_finding):
        """Return a _get_ddb() mock.

        findings_items: list of finding row dicts returned by the by_vin GSI.
        actions_pages_by_finding: dict mapping finding_id → list of page dicts,
            where each page dict has 'Items' and optionally 'LastEvaluatedKey'.
        """
        mock_ddb = MagicMock()
        mock_findings_table = MagicMock()
        mock_actions_table = MagicMock()

        # Findings query always returns all items in one call (no pagination needed).
        mock_findings_table.query.return_value = {"Items": findings_items}

        # Actions query: pages through the list for each finding_id.
        call_counters: dict[str, int] = {}

        def _actions_query(**kwargs):
            key_cond = kwargs.get("KeyConditionExpression")
            # Extract the finding_id from the KeyConditionExpression expression values.
            fid = key_cond.get_expression()["values"][1]
            count = call_counters.get(fid, 0)
            call_counters[fid] = count + 1
            pages = actions_pages_by_finding.get(fid, [{"Items": []}])
            page_idx = min(count, len(pages) - 1)
            return pages[page_idx]

        mock_actions_table.query.side_effect = _actions_query

        def _table(name):
            if name == h._FINDINGS_TABLE:
                return mock_findings_table
            return mock_actions_table

        mock_ddb.Table.side_effect = _table
        return mock_ddb

    def test_service_advisor_inner_pages_fully_fetched(self):
        """MUTATION GUARD (a): when a finding's by_finding GSI spans two pages,
        both pages are returned and next_token is None (findings fully exhausted).

        Mutation: restore single-page inner query (drop `while` loop, use bare
        `a_resp = actions_table.query(...); items.extend(...)` without following
        LastEvaluatedKey).  Test FAILS because only 2 of 3 actions are returned.
        """
        finding = {"finding_id": "FID-T143-001", "vin": "VIN-T143"}
        action_p1 = {"action_id": "ACT-p1", "finding_id": "FID-T143-001"}
        action_p2 = {"action_id": "ACT-p2", "finding_id": "FID-T143-001"}
        action_p3 = {"action_id": "ACT-p3", "finding_id": "FID-T143-001"}

        mock_ddb = self._make_ddb_mock(
            findings_items=[finding],
            actions_pages_by_finding={
                "FID-T143-001": [
                    # Page 1: two items with LastEvaluatedKey
                    {"Items": [action_p1, action_p2], "LastEvaluatedKey": {"action_id": "ACT-p2"}},
                    # Page 2: one item, no LastEvaluatedKey (exhausted)
                    {"Items": [action_p3]},
                ],
            },
        )

        with patch.object(h, "_get_ddb", return_value=mock_ddb):
            items, out_token = h._list_actions_by_vin("VIN-T143", limit=50)

        assert len(items) == 3, (
            f"T14.3 (W-C): expected 3 actions (2 from page 1 + 1 from page 2), "
            f"got {len(items)}. Inner LastEvaluatedKey was dropped — "
            f"silent truncation: {[i['action_id'] for i in items]}"
        )
        assert {i["action_id"] for i in items} == {"ACT-p1", "ACT-p2", "ACT-p3"}, (
            "T14.3 (W-C): one or more actions from the second inner page are missing"
        )
        # Findings page exhausted → no more findings → out_token is None
        assert out_token is None, (
            f"T14.3 (W-C): findings page exhausted but next_token={out_token!r}; "
            f"expected None"
        )

    def test_service_vin_next_token_none_only_when_all_actions_fetched(self):
        """next_token is None iff both findings AND all their inner pages are exhausted.

        MUTATION GUARD (a): if the while loop is removed and LastEvaluatedKey is
        dropped from inner queries, 2 of 3 actions are returned but out_token is
        still None (findings page exhausted) — violating the invariant that
        next_token: None means complete.

        With the fix, all 3 actions are returned and out_token is None.
        """
        finding1 = {"finding_id": "FID-T143-002", "vin": "VIN-T143B"}
        finding2 = {"finding_id": "FID-T143-003", "vin": "VIN-T143B"}
        act_f1_p1 = {"action_id": "ACT-f1p1", "finding_id": "FID-T143-002"}
        act_f1_p2 = {"action_id": "ACT-f1p2", "finding_id": "FID-T143-002"}
        act_f2 = {"action_id": "ACT-f2", "finding_id": "FID-T143-003"}

        mock_ddb = self._make_ddb_mock(
            findings_items=[finding1, finding2],
            actions_pages_by_finding={
                "FID-T143-002": [
                    {"Items": [act_f1_p1], "LastEvaluatedKey": {"action_id": "ACT-f1p1"}},
                    {"Items": [act_f1_p2]},
                ],
                "FID-T143-003": [{"Items": [act_f2]}],
            },
        )

        with patch.object(h, "_get_ddb", return_value=mock_ddb):
            items, out_token = h._list_actions_by_vin("VIN-T143B", limit=50)

        assert len(items) == 3, (
            f"T14.3 (W-C): expected 3 actions total (2 from FID-002, 1 from FID-003), "
            f"got {len(items)}: {[i['action_id'] for i in items]}"
        )
        assert out_token is None, (
            f"T14.3 (W-C): findings exhausted, all inner pages followed; "
            f"expected out_token=None, got {out_token!r}"
        )

    def test_service_vin_findings_next_token_propagated(self):
        """When the findings page is NOT exhausted, out_token is non-None."""
        import base64
        findings_items = [{"finding_id": "FID-T143-004", "vin": "VIN-T143C"}]
        last_key = {"vin": "VIN-T143C", "finding_id": "FID-T143-004"}

        mock_ddb = MagicMock()
        mock_findings_table = MagicMock()
        mock_actions_table = MagicMock()
        mock_findings_table.query.return_value = {
            "Items": findings_items,
            "LastEvaluatedKey": last_key,
        }
        mock_actions_table.query.return_value = {
            "Items": [{"action_id": "ACT-f4", "finding_id": "FID-T143-004"}],
        }
        mock_ddb.Table.side_effect = lambda name: (
            mock_findings_table if name == h._FINDINGS_TABLE else mock_actions_table
        )

        with patch.object(h, "_get_ddb", return_value=mock_ddb):
            items, out_token = h._list_actions_by_vin("VIN-T143C", limit=1)

        assert out_token is not None, (
            "T14.3 (W-C): findings page has LastEvaluatedKey; out_token must be non-None"
        )
        # Decode and verify it encodes the findings LastEvaluatedKey
        decoded = json.loads(base64.b64decode(out_token).decode())
        assert decoded == last_key, (
            f"T14.3 (W-C): out_token decoded to {decoded!r}, expected {last_key!r}"
        )


class TestT14_3_OwnerVinBranch:
    """T14.3 (W-F): owner callers on GET /actions/vehicle/{vin} get 503, not 200 [].

    No Action writer sets `vin` or `scope.vin` on rows, so the previous post-filter
    always matched nothing.  The fix returns 503 owner_scope_unavailable — the
    same pattern as T12.1 fleet_scope_unavailable.

    MUTATION GUARD (c): entitlement check (is_owner_caller) MUST come before 503.
    An owner with no entitlement must get 403, not 503.
    """

    def test_owner_vin_returns_503_not_200_empty(self):
        """MUTATION GUARD (b): owner callers get 503, not 200 [].

        Mutation: restore post-filter returning 200 [].
        Expected: this test FAILS (statusCode == 200, not 503).
        """
        event = _event(
            method="GET",
            resource="/actions/vehicle/{vin}",
            path_params={"vin": "VIN-T143-owner"},
            claims=_claims(sub="owner-T143", groups=["vehicle-owner"]),
        )
        resp = h.handler(event, None)
        assert resp["statusCode"] == 503, (
            f"T14.3 (W-F): owner caller on /actions/vehicle/{{vin}} must return 503 "
            f"(owner_scope_unavailable), got {resp['statusCode']}. "
            "The previous 200 [] was wrong: no Action writer projects `vin`, so the "
            "post-filter always matched nothing — silent empty result, not an empty table."
        )
        body = json.loads(resp["body"])
        assert body["error"] == "owner_scope_unavailable", (
            f"T14.3 (W-F): error must be 'owner_scope_unavailable', got {body.get('error')!r}"
        )

    def test_owner_vin_error_code_is_owner_scope_unavailable(self):
        """503 body carries error='owner_scope_unavailable' (not fleet or generic)."""
        event = _event(
            method="GET",
            resource="/actions/vehicle/{vin}",
            path_params={"vin": "VIN-T143-owner2"},
            claims=_claims(sub="owner-T143b", groups=["consumer"]),
        )
        resp = h.handler(event, None)
        assert resp["statusCode"] == 503
        body = json.loads(resp["body"])
        assert body["error"] == "owner_scope_unavailable"
        # Must NOT say fleet_scope_unavailable — distinct error per branch
        assert body["error"] != "fleet_scope_unavailable", (
            "T14.3 (W-F): owner branch must use 'owner_scope_unavailable', "
            "not 'fleet_scope_unavailable'"
        )
        assert "detail" in body, "T14.3 (W-F): 503 body must include 'detail' key"

    def test_owner_vin_no_entitlement_gets_403_not_503(self):
        """MUTATION GUARD (c): unauthenticated / wrong-group caller gets 403 BEFORE 503.

        Mutation: move 503 before is_owner_caller entitlement gate.
        Expected: test FAILS (statusCode == 503, not 403).

        The service path (else branch) uses check_vehicle_scope and returns 403
        for a caller that has no VIN access and no owner/consumer group.
        """
        # A caller that is not vehicle-owner/consumer and fails check_vehicle_scope
        # goes to the else branch, which calls check_vehicle_scope.
        # check_vehicle_scope returns False for a sub that doesn't match the VIN.
        event = _event(
            method="GET",
            resource="/actions/vehicle/{vin}",
            path_params={"vin": "VIN-T143-denied"},
            claims=_claims(sub="other-user-T143", groups=["service-advisor"]),
        )
        # Patch check_vehicle_scope to deny this caller
        with patch.object(h, "check_vehicle_scope", return_value=False):
            resp = h.handler(event, None)
        assert resp["statusCode"] == 403, (
            f"T14.3 (W-F) mutation guard (c): a caller denied by check_vehicle_scope "
            f"must get 403, not 503. Got: {resp['statusCode']}. "
            "Moving the owner 503 before the entitlement check would cause this."
        )

    def test_owner_vin_503_comes_after_owner_group_check(self):
        """503 is reached only when the caller IS vehicle-owner/consumer.

        A non-owner caller (service-advisor) with vehicle scope gets 200 data,
        not 503.  This confirms the owner-group check gates the 503.
        """
        with patch.object(h, "_list_actions_by_vin", return_value=([], None)):
            event = _event(
                method="GET",
                resource="/actions/vehicle/{vin}",
                path_params={"vin": "VIN-T143-svc"},
                claims=_claims(sub="svc-T143", groups=["service-advisor"]),
            )
            resp = h.handler(event, None)
        assert resp["statusCode"] == 200, (
            f"T14.3 (W-F): service-advisor must still reach _list_actions_by_vin "
            f"and get 200, not 503. Got: {resp['statusCode']}"
        )



# ---------------------------------------------------------------------------
# Review Cycle 3, Critical C2 — the audit trail's APPEND is now asserted.
#
# Cycle 3 deleted `audit_trail = list_append(...)` from _update_action_status's
# UpdateExpression and the whole suite stayed green: findings 70 / actions 81 /
# schemas 151. That is a live risk rather than a cosmetic gap, because T14.7 made
# creation write `audit_trail: []` — so transitions are now the ONLY writer, and
# every Action's trail could stay empty for life with a passing suite.
#
# Why the existing tests did not catch it, which is the part worth remembering:
# they read `ExpressionAttributeValues[":entry"]`, and that value is still built
# and still passed to DynamoDB when the UpdateExpression no longer references it.
# They validated a value that was never applied — a proxy for the property, not
# the property. Same substitution as Cycle 1's Critical, third instance in this
# spec.
#
# These tests assert the UpdateExpression itself.
# ---------------------------------------------------------------------------


class TestCycle3C2_AuditTrailAppendIsAsserted:
    """The UpdateExpression must actually append the entry to audit_trail."""

    def _capture_update_kwargs(self, to_status: str = "dismissed") -> dict:
        mock_table = MagicMock()
        mock_table.update_item.return_value = {
            "Attributes": {
                "action_id": "AVX-ACT-aabbccdd-0000-0000-0000-000000000001",
                "status": to_status,
            }
        }
        with patch.object(h, "_get_ddb") as mock_ddb:
            mock_ddb.return_value.Table.return_value = mock_table
            h._update_action_status(
                action_id="AVX-ACT-aabbccdd-0000-0000-0000-000000000001",
                from_status="pending_execution",
                to_status=to_status,
                actor="owner-usr-4a5b6c7d",
            )
        return mock_table.update_item.call_args.kwargs

    def test_update_expression_appends_entry_to_audit_trail(self):
        """UpdateExpression must reference audit_trail, list_append and :entry.

        Mutation: delete the `audit_trail = list_append(...)` clause from
        _update_action_status's UpdateExpression. This test MUST fail. Before it
        existed, that deletion left 302 tests green across three directories.
        """
        kwargs = self._capture_update_kwargs()
        expr = kwargs["UpdateExpression"]

        assert "audit_trail" in expr, (
            "UpdateExpression does not mention audit_trail, so no transition is "
            "recorded. Because T14.7 made creation write an empty trail, transitions "
            f"are the only writer and every Action's trail stays empty.\nGot: {expr!r}"
        )
        assert "list_append" in expr, (
            "UpdateExpression does not use list_append, so the trail is overwritten "
            f"rather than appended and history is lost on each transition.\nGot: {expr!r}"
        )
        assert ":entry" in expr, (
            "UpdateExpression does not reference :entry. Note that "
            "ExpressionAttributeValues[':entry'] is still BUILT and PASSED even when "
            "the expression ignores it — which is exactly why asserting on the value "
            f"rather than the expression missed this.\nGot: {expr!r}"
        )

    def test_entry_value_is_actually_applied_not_merely_supplied(self):
        """Every :-placeholder supplied must be referenced by some expression.

        Guards the general form of C2: a value handed to DynamoDB that no
        expression mentions is dead payload, and a test reading it proves nothing
        about what was written.

        Scope note: placeholders may legitimately be referenced by the
        ConditionExpression rather than the UpdateExpression — `:old_status` is,
        and it is load-bearing there as the optimistic-concurrency guard
        (`attribute_exists(action_id) AND #st = :old_status`). Checking against the
        UpdateExpression alone would flag correct code, so both are considered.
        """
        kwargs = self._capture_update_kwargs()
        expr = kwargs["UpdateExpression"]
        cond = kwargs.get("ConditionExpression", "") or ""
        referenced_in = f"{expr} {cond}"
        supplied = set(kwargs.get("ExpressionAttributeValues", {}).keys())
        unreferenced = sorted(p for p in supplied if p not in referenced_in)
        assert not unreferenced, (
            f"ExpressionAttributeValues supplies {unreferenced} which neither the "
            f"UpdateExpression nor the ConditionExpression references — supplied but "
            f"never applied.\nUpdate:    {expr!r}\nCondition: {cond!r}"
        )

    def test_optimistic_concurrency_guard_is_present(self):
        """The ConditionExpression must pin the expected prior status.

        Noticed while scoping the test above. This is what makes the status machine
        monotonic under concurrent writers: without `#st = :old_status` two racing
        transitions can both succeed and the later one silently wins.
        """
        kwargs = self._capture_update_kwargs()
        cond = kwargs.get("ConditionExpression", "") or ""
        assert ":old_status" in cond and "attribute_exists(action_id)" in cond, (
            "ConditionExpression no longer pins the prior status and the row's "
            f"existence, so concurrent transitions can both commit.\nGot: {cond!r}"
        )

    def test_audit_trail_append_survives_every_transition_kind(self):
        """The append is not conditional on a particular to_status.

        dismiss, executed and cancelled must all append, so a future edit cannot
        quietly drop the trail for one transition kind while the dismiss-focused
        tests above stay green.
        """
        for to_status in ("dismissed", "executed", "cancelled"):
            expr = self._capture_update_kwargs(to_status)["UpdateExpression"]
            assert "list_append" in expr and ":entry" in expr, (
                f"transition to {to_status!r} does not append to audit_trail.\n"
                f"Got: {expr!r}"
            )



class TestCycle3W3_FleetFanOutIsBounded:
    """Pin the pagination of `_list_actions_by_vin_for_fleet` while it is dark.

    Review Cycle 3 found this was the FOURTH instance of the fan-out truncation
    T14.3 bounded in `_list_actions_by_vin`: the inner `by_finding` query discarded
    `LastEvaluatedKey`, so a finding with more actions than one page lost the
    remainder while the caller still saw `next_token: None`.

    The helper is currently unreachable — the fleet VIN route returns 503 until the
    top-level `fleet_id` projection lands. It is pinned anyway for the same reason
    T14.4 pinned the dark filters: the route's re-enable comment points straight
    here, and nothing else would notice a regression before it went live. Four
    instances of one defect is enough evidence that "unreachable" is temporary.
    """

    def test_inner_pages_are_followed_to_exhaustion(self):
        """Multi-page inner by_finding results are fully collected.

        Mutation: drop the `while True` / LastEvaluatedKey loop in
        `_list_actions_by_vin_for_fleet` (revert to a single query). This test MUST
        fail — before it existed, that helper had no pagination coverage at all.
        """
        findings_page = {
            "Items": [{"finding_id": "FIND-1"}],
            "Count": 1,
        }
        # Two inner pages for FIND-1, then exhaustion.
        inner_pages = [
            {"Items": [{"action_id": "A1"}], "LastEvaluatedKey": {"action_id": "A1"}},
            {"Items": [{"action_id": "A2"}]},
        ]

        calls = {"n": 0}

        class _Tbl:
            def query(self_, **kw):
                # First call is the findings-table step-1 query (it passes a
                # FilterExpression); subsequent calls are the inner by_finding pages.
                if "FilterExpression" in kw:
                    return findings_page
                i = calls["n"]
                calls["n"] += 1
                return inner_pages[min(i, len(inner_pages) - 1)]

        with patch.object(h, "_get_ddb") as mock_ddb:
            mock_ddb.return_value.Table.return_value = _Tbl()
            items, _token = h._list_actions_by_vin_for_fleet("VIN-TEST", ["FLEET-A"])

        ids = [i["action_id"] for i in items]
        assert ids == ["A1", "A2"], (
            "Inner by_finding pagination was truncated: expected both pages "
            f"['A1', 'A2'], got {ids}. A caller would silently receive a short list "
            "with next_token: None — the defect this spec has now hit four times."
        )



@pytest.mark.untrusted_claims
class TestMembershipClaimGatesEndToEndActions:
    """Fix Group 31 (review cycle 4, W2): the actions handler's own end-to-end gate proof.

    Fix Group 29 gave the FINDINGS handler three end-to-end untrusted tests and left this
    handler with predicate-level coverage only, while F29.5's Accept and the issue's
    Prevention section described protection broader than what existed.

    That asymmetry matters here specifically: **the eighth gated site was found in this
    handler**, reading `custom:fleetIds` directly rather than through
    `_lib/avx_scope.py`. A predicate test over the shared helper structurally cannot see
    a handler that bypasses the helper — which is exactly how that site survived a
    review that enumerated seven.

    Each test asserts the 403 AND that the caller never reaches the 501 stub, because a
    403-vs-501 distinction is what proves authorization ran before the route body rather
    than the route simply being unimplemented.
    """

    def _get(self, resource, path_params, claims):
        return h.handler(
            _event(method="GET", resource=resource, path_params=path_params, claims=claims),
            None,
        )

    def test_fleet_route_denies_forged_claim(self):
        resp = self._get(
            "/actions/fleet/{fleet_id}", {"fleet_id": "fleet-NOT-MINE"},
            _claims(groups=["fleet-operator"], fleet_ids=["fleet-A", "fleet-NOT-MINE"]),
        )
        assert resp["statusCode"] == 403, (
            f"a forged fleet claim must be denied before the stub; got {resp}"
        )

    def test_dealership_route_denies_forged_claim(self):
        resp = self._get(
            "/actions/dealership/{dealer_id}", {"dealer_id": "D-NOT-MINE"},
            _claims(groups=["dealer-admin"], dealer_ids=["D-NOT-MINE"]),
        )
        assert resp["statusCode"] == 403, f"forged dealer claim must be denied; got {resp}"

    def test_district_route_denies_forged_claim(self):
        resp = self._get(
            "/actions/district/{district_id}", {"district_id": "DIST-NOT-MINE"},
            _claims(groups=["district-manager"], district_ids=["DIST-NOT-MINE"]),
        )
        assert resp["statusCode"] == 403, f"forged district claim must be denied; got {resp}"

    def test_trusted_same_claims_reach_the_stub(self):
        """The positive control, without which the three above could pass for any reason.

        Same claims, same routes; the ONLY difference is the trust flags. Reaching the
        501 stub proves authorization passed, so the 403s above are attributable to the
        gate rather than to a malformed event or a missing route.
        """
        import os
        from unittest.mock import patch as _patch

        trusted = {
            "AVX_FLEET_IDS_CLAIM_TRUSTED": "true",
            "AVX_DEALER_IDS_CLAIM_TRUSTED": "true",
            "AVX_DISTRICT_IDS_CLAIM_TRUSTED": "true",
        }
        cases = (
            ("/actions/fleet/{fleet_id}", {"fleet_id": "fleet-A"},
             _claims(groups=["fleet-operator"], fleet_ids=["fleet-A"])),
            ("/actions/dealership/{dealer_id}", {"dealer_id": "D-1"},
             _claims(groups=["dealer-admin"], dealer_ids=["D-1"])),
            ("/actions/district/{district_id}", {"district_id": "DIST-1"},
             _claims(groups=["district-manager"], district_ids=["DIST-1"])),
        )
        with _patch.dict(os.environ, trusted):
            for resource, params, claims in cases:
                resp = self._get(resource, params, claims)
                assert resp["statusCode"] == 501, (
                    f"{resource} with a TRUSTED matching claim must pass authorization "
                    f"and reach the stub; got {resp}"
                )



# ---------------------------------------------------------------------------
# The sign-in email is never an owner id (issue 2026-09-25, CMS repo
# `issues/2026-09-25-ios-findings-owner-id-is-email/`)
# ---------------------------------------------------------------------------
#
# The iOS Alerts tab sent `GET /actions/owner/<sign-in email>` and got 403. The
# fix is client-side (send `sub`). These tests pin that the server does not
# accept an `email` claim equal to the path: `email` is self-writable in the CMS
# pool, so comparing it would let a user read another owner's Actions.

_EMAIL = "meridian.driver@example.com"
_EMAIL_CALLER_SUB = "086183b0-1081-7011-80a1-8230fcbff4c6"
_OWNER_READ_ROUTES = ["/actions/owner/{owner_id}", "/actions/ios-owner/{owner_id}"]


def _email_claims(email=_EMAIL):
    c = _claims(sub=_EMAIL_CALLER_SUB, groups=["vehicle-owner"])
    c["email"] = email
    c["email_verified"] = "true"
    c["custom:customerId"] = "CUST-004000DD"
    return c


class TestOwnerRoutesNeverAcceptEmail:
    @staticmethod
    def _read(resource, owner_id, claims):
        seen = []

        def _fake_list(oid, limit=50, next_token=None):
            seen.append(oid)
            return [], None

        with patch.object(h, "_list_actions_by_owner", side_effect=_fake_list):
            resp = h.handler(_event(method="GET", resource=resource,
                                    path_params={"owner_id": owner_id},
                                    claims=claims), None)
        return resp, seen

    @pytest.mark.parametrize("resource", _OWNER_READ_ROUTES)
    def test_own_email_as_owner_id_is_refused(self, resource):
        resp, seen = self._read(resource, _EMAIL, _email_claims())
        assert resp["statusCode"] == 403, resp["body"]
        assert seen == [], "no query may run for a refused owner id"

    @pytest.mark.parametrize("resource", _OWNER_READ_ROUTES)
    def test_rewritten_email_does_not_read_another_users_actions(self, resource):
        victim = "someone.else@example.com"
        resp, seen = self._read(resource, victim, _email_claims(email=victim))
        assert resp["statusCode"] == 403, resp["body"]
        assert seen == []

    @pytest.mark.parametrize("resource", _OWNER_READ_ROUTES)
    def test_another_users_sub_is_refused(self, resource):
        resp, seen = self._read(resource, "99999999-8888-7777-6666-555555555555", _email_claims())
        assert resp["statusCode"] == 403, resp["body"]
        assert seen == []

    @pytest.mark.parametrize("resource", _OWNER_READ_ROUTES)
    def test_trusted_customer_id_does_not_open_the_actions_routes(self, resource):
        """Actions are keyed `owner-{sub}`; the party of record does not apply here.
        The iOS client reads actions by `sub` only and relies on this asymmetry."""
        with patch.dict(os.environ, {"AVX_CUSTOMER_ID_CLAIM_TRUSTED": "true"}, clear=False):
            resp, seen = self._read(resource, "CUST-004000DD", _email_claims())
        assert resp["statusCode"] == 403, resp["body"]
        assert seen == []

    @pytest.mark.parametrize("resource", _OWNER_READ_ROUTES)
    def test_own_sub_is_accepted(self, resource):
        """Positive control: the identity the app now sends."""
        resp, seen = self._read(resource, _EMAIL_CALLER_SUB, _email_claims())
        assert resp["statusCode"] == 200, resp["body"]
        assert seen == [_EMAIL_CALLER_SUB]
