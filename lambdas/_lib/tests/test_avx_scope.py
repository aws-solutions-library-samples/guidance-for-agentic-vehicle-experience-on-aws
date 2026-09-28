"""
Unit tests for lambdas/_lib/avx_scope.py

Tests cover:
  - check_vehicle_scope
  - check_owner_scope (mutation-verified — scope-leak detection)
  - check_owner_fleet_scope
  - check_fleet_scope (fleet asymmetry: platform-admin still needs membership)
  - check_dealer_scope (platform-admin bypasses membership)
  - check_district_scope (platform-admin bypasses membership)
  - _extract_groups (various claim formats)
  - _extract_claim_list

Run:
    python3 -m pytest lambdas/_lib/tests/ -v
"""
from __future__ import annotations

import json
import os
import sys

import pytest

# Allow direct import
_LIB_ROOT = os.path.join(os.path.dirname(__file__), "..")
_LAMBDAS_ROOT = os.path.dirname(_LIB_ROOT)
for _p in (_LIB_ROOT, _LAMBDAS_ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from avx_scope import (
    _extract_claim_list,
    _extract_groups,
    check_dealer_scope,
    check_district_scope,
    check_fleet_scope,
    check_owner_fleet_scope,
    check_owner_scope,
    check_vehicle_scope,
    claim_is_trusted,
    is_fleet_vehicle_caller,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _claims(
    sub="user-sub-123",
    groups=None,
    fleet_ids=None,
    dealer_ids=None,
    district_ids=None,
    custom_role=None,
) -> dict:
    c: dict = {"sub": sub}
    if groups is not None:
        if isinstance(groups, list):
            c["cognito:groups"] = json.dumps(groups)
        else:
            c["cognito:groups"] = groups
    else:
        c["cognito:groups"] = ""
    if fleet_ids is not None:
        c["custom:fleetIds"] = json.dumps(fleet_ids)
    if dealer_ids is not None:
        c["custom:dealerIds"] = json.dumps(dealer_ids)
    if district_ids is not None:
        c["custom:districtIds"] = json.dumps(district_ids)
    if custom_role is not None:
        c["custom:role"] = custom_role
    return c


# ---------------------------------------------------------------------------
# _extract_groups
# ---------------------------------------------------------------------------

class TestExtractGroups:
    def test_space_separated(self):
        c = {"cognito:groups": "fleet-operator service-advisor"}
        assert _extract_groups(c) == {"fleet-operator", "service-advisor"}

    def test_json_list(self):
        c = {"cognito:groups": '["fleet-operator","platform-admin"]'}
        assert _extract_groups(c) == {"fleet-operator", "platform-admin"}

    def test_python_list(self):
        c = {"cognito:groups": ["fleet-operator", "consumer"]}
        assert _extract_groups(c) == {"fleet-operator", "consumer"}

    def test_empty(self):
        c = {"cognito:groups": ""}
        assert _extract_groups(c) == set()

    def test_missing(self):
        assert _extract_groups({}) == set()

    def test_comma_separated(self):
        c = {"cognito:groups": "fleet-driver,consumer"}
        assert _extract_groups(c) == {"fleet-driver", "consumer"}


# ---------------------------------------------------------------------------
# _extract_claim_list
# ---------------------------------------------------------------------------

class TestExtractClaimList:
    def test_json_list(self):
        c = {"custom:fleetIds": '["fleet-A","fleet-B"]'}
        assert _extract_claim_list(c, "custom:fleetIds") == ["fleet-A", "fleet-B"]

    def test_python_list(self):
        c = {"custom:fleetIds": ["fleet-A"]}
        assert _extract_claim_list(c, "custom:fleetIds") == ["fleet-A"]

    def test_empty(self):
        assert _extract_claim_list({}, "custom:fleetIds") == []

    def test_single_value(self):
        c = {"custom:fleetIds": "fleet-singleton"}
        assert _extract_claim_list(c, "custom:fleetIds") == ["fleet-singleton"]


# ---------------------------------------------------------------------------
# check_vehicle_scope
# ---------------------------------------------------------------------------

class TestCheckVehicleScope:
    def test_platform_admin_allowed(self):
        c = _claims(groups=["platform-admin"])
        assert check_vehicle_scope(c, "VIN-001") is True

    def test_service_advisor_group(self):
        c = _claims(groups=["service-advisor"])
        assert check_vehicle_scope(c, "VIN-001") is True

    def test_technician_group(self):
        c = _claims(groups=["technician"])
        assert check_vehicle_scope(c, "VIN-001") is True

    def test_service_advisor_custom_role_is_now_DENIED(self):
        """Review cycle 2 (C1): the `custom:role` read arm is deleted.

        This test previously asserted `is True` — it was pinning a bypass that let a
        caller in NO Cognito group read every VIN by writing one self-writable
        attribute to their own user record. Inverted rather than deleted, so the
        grant cannot be reintroduced silently.

        The group path (`test_service_advisor_group`) is unaffected and covers every
        live service advisor; zero users carry `custom:role` in either pool.
        """
        c = _claims(custom_role="service-advisor")
        assert check_vehicle_scope(c, "VIN-001") is False

    def test_technician_custom_role_is_now_DENIED(self):
        """The same arm honoured `"technician"` too, which FG26 never measured."""
        c = _claims(custom_role="technician")
        assert check_vehicle_scope(c, "VIN-001") is False

    def test_fleet_driver_with_fleet_ids(self):
        c = _claims(groups=["fleet-driver"], fleet_ids=["fleet-A"])
        assert check_vehicle_scope(c, "VIN-001") is True

    def test_fleet_driver_no_fleet_ids(self):
        c = _claims(groups=["fleet-driver"])
        # No fleet IDs but consumer group allows self-read with finding context
        # Without finding and no fleet_ids, falls to consumer path which may allow
        assert check_vehicle_scope(c, "VIN-001") is False

    def test_no_group_denied(self):
        c = _claims()
        assert check_vehicle_scope(c, "VIN-001") is False

    def test_vehicle_owner_with_matching_finding(self):
        c = _claims(sub="owner-abc", groups=["vehicle-owner"])
        finding = {"scope": {"vin": "VIN-001"}}
        assert check_vehicle_scope(c, "VIN-001", finding) is True

    def test_vehicle_owner_no_finding(self):
        # C3 fix (2026-09-19): vehicle-owner WITHOUT a finding context now returns False.
        # The route handler enforces ownership at the query level (VIN+owner_id filter),
        # not via a pre-check pass-through. check_vehicle_scope returning False for this
        # case is intentional and correct — it is NOT a bug that needs working around.
        c = _claims(sub="owner-abc", groups=["vehicle-owner"])
        assert check_vehicle_scope(c, "VIN-001") is False


# ---------------------------------------------------------------------------
# check_owner_scope
# ---------------------------------------------------------------------------

class TestCheckOwnerScope:
    def test_sub_matches_owner_id(self):
        c = _claims(sub="user-123")
        assert check_owner_scope(c, "user-123") is True

    def test_sub_mismatch_denied(self):
        c = _claims(sub="user-123")
        assert check_owner_scope(c, "user-456") is False

    def test_platform_admin_bypass(self):
        c = _claims(sub="admin-999", groups=["platform-admin"])
        assert check_owner_scope(c, "user-456") is True

    def test_consumer_group_sub_match(self):
        c = _claims(sub="user-789", groups=["consumer"])
        assert check_owner_scope(c, "user-789") is True

    def test_consumer_group_sub_mismatch(self):
        # Consumer group alone does NOT bypass the sub check
        c = _claims(sub="user-789", groups=["consumer"])
        # Relies on sub matching, not group
        assert check_owner_scope(c, "user-999") is False

    # ── MUTATION-VERIFIED PROPERTY ──────────────────────────────────────────
    # Mutation: widen check_owner_scope to accept any sub (remove the sub check)
    # Expected result: test_owner_a_cannot_see_owner_b MUST fail
    def test_owner_a_cannot_see_owner_b(self):
        """Scope-leak: owner A must not see owner B's findings."""
        c_a = _claims(sub="owner-A", groups=["consumer"])
        assert check_owner_scope(c_a, "owner-B") is False

    def test_owner_a_can_see_own(self):
        c_a = _claims(sub="owner-A", groups=["consumer"])
        assert check_owner_scope(c_a, "owner-A") is True


# ---------------------------------------------------------------------------
# check_owner_fleet_scope
# ---------------------------------------------------------------------------

class TestCheckOwnerFleetScope:
    def test_sub_matches(self):
        c = _claims(sub="user-123")
        assert check_owner_fleet_scope(c, "user-123") is True

    def test_sub_mismatch(self):
        c = _claims(sub="user-123")
        assert check_owner_fleet_scope(c, "user-999") is False

    def test_platform_admin_bypass(self):
        c = _claims(sub="admin", groups=["platform-admin"])
        assert check_owner_fleet_scope(c, "user-999") is True

    def test_fleet_operator_with_membership(self):
        # W1 fix (2026-09-19): check_owner_fleet_scope now requires owner_fleet_id.
        # Without it, fleet-operator is denied (no unscoped pass-through).
        c = _claims(sub="op", groups=["fleet-operator"], fleet_ids=["F1"])
        assert check_owner_fleet_scope(c, "any-owner") is False  # no owner_fleet_id provided

    def test_fleet_operator_with_matching_owner_fleet_id(self):
        # fleet-operator with the owner's correct fleet_id is allowed.
        c = _claims(sub="op", groups=["fleet-operator"], fleet_ids=["F1"])
        assert check_owner_fleet_scope(c, "any-owner", owner_fleet_id="F1") is True

    def test_fleet_operator_with_wrong_owner_fleet_id(self):
        # fleet-operator with a different fleet_id is denied even with membership.
        c = _claims(sub="op", groups=["fleet-operator"], fleet_ids=["F1"])
        assert check_owner_fleet_scope(c, "any-owner", owner_fleet_id="F2") is False

    def test_fleet_operator_no_membership(self):
        c = _claims(sub="op", groups=["fleet-operator"])
        assert check_owner_fleet_scope(c, "any-owner") is False

    def test_scope_leak_owner_a_vs_b(self):
        """fleet-owner scope: owner A cannot see owner B without fleet membership."""
        c = _claims(sub="owner-A", groups=["consumer"])
        assert check_owner_fleet_scope(c, "owner-B") is False


# ---------------------------------------------------------------------------
# check_fleet_scope — fleet asymmetry test
# ---------------------------------------------------------------------------

class TestCheckFleetScope:
    """
    Fleet asymmetry: platform-admin is only an alternative for the GROUP
    requirement. The membership predicate (custom:fleetIds contains fleet_id)
    STILL applies even for platform-admin.

    This is the key difference from dealership/district scope.
    """

    def test_fleet_operator_with_membership(self):
        c = _claims(groups=["fleet-operator"], fleet_ids=["fleet-A"])
        assert check_fleet_scope(c, "fleet-A") is True

    def test_fleet_operator_without_membership(self):
        c = _claims(groups=["fleet-operator"], fleet_ids=["fleet-B"])
        assert check_fleet_scope(c, "fleet-A") is False

    def test_platform_admin_with_membership(self):
        """platform-admin WITH fleet membership — allowed."""
        c = _claims(groups=["platform-admin"], fleet_ids=["fleet-A"])
        assert check_fleet_scope(c, "fleet-A") is True

    def test_platform_admin_without_membership(self):
        """
        FLEET ASYMMETRY TEST: platform-admin WITHOUT fleet membership is DENIED.
        This is different from dealership/district where platform-admin bypasses
        the membership predicate entirely.
        """
        c = _claims(groups=["platform-admin"])
        assert check_fleet_scope(c, "fleet-A") is False

    def test_platform_admin_wrong_fleet(self):
        """platform-admin with membership for fleet-B cannot access fleet-A."""
        c = _claims(groups=["platform-admin"], fleet_ids=["fleet-B"])
        assert check_fleet_scope(c, "fleet-A") is False

    def test_no_group_denied(self):
        c = _claims(fleet_ids=["fleet-A"])
        assert check_fleet_scope(c, "fleet-A") is False

    def test_cross_fleet_enumeration_denied(self):
        """Fleet-operator in fleet-A cannot enumerate fleet-B's findings."""
        c = _claims(groups=["fleet-operator"], fleet_ids=["fleet-A"])
        assert check_fleet_scope(c, "fleet-B") is False


# ---------------------------------------------------------------------------
# check_dealer_scope
# ---------------------------------------------------------------------------

class TestCheckDealerScope:
    def test_dealer_with_membership(self):
        c = _claims(groups=["dealer-admin"], dealer_ids=["dealer-1"])
        assert check_dealer_scope(c, "dealer-1") is True

    def test_dealer_wrong_dealership(self):
        c = _claims(groups=["dealer-admin"], dealer_ids=["dealer-1"])
        assert check_dealer_scope(c, "dealer-2") is False

    def test_dealer_no_membership(self):
        c = _claims(groups=["dealer-admin"])
        assert check_dealer_scope(c, "dealer-1") is False

    def test_platform_admin_no_membership(self):
        """Dealership: platform-admin bypasses membership — different from fleet."""
        c = _claims(groups=["platform-admin"])
        assert check_dealer_scope(c, "dealer-1") is True

    def test_cross_dealer_denied(self):
        c = _claims(groups=["dealer-admin"], dealer_ids=["dealer-A"])
        assert check_dealer_scope(c, "dealer-B") is False

    def test_no_group_denied(self):
        c = _claims(dealer_ids=["dealer-1"])
        assert check_dealer_scope(c, "dealer-1") is False


# ---------------------------------------------------------------------------
# check_district_scope
# ---------------------------------------------------------------------------

class TestCheckDistrictScope:
    def test_district_manager_with_membership(self):
        c = _claims(groups=["district-manager"], district_ids=["district-1"])
        assert check_district_scope(c, "district-1") is True

    def test_district_manager_wrong_district(self):
        c = _claims(groups=["district-manager"], district_ids=["district-1"])
        assert check_district_scope(c, "district-2") is False

    def test_district_manager_no_membership(self):
        c = _claims(groups=["district-manager"])
        assert check_district_scope(c, "district-1") is False

    def test_platform_admin_no_membership(self):
        """District: platform-admin bypasses membership — different from fleet."""
        c = _claims(groups=["platform-admin"])
        assert check_district_scope(c, "district-1") is True

    def test_cross_district_denied(self):
        c = _claims(groups=["district-manager"], district_ids=["district-A"])
        assert check_district_scope(c, "district-B") is False

    def test_no_group_denied(self):
        c = _claims(district_ids=["district-1"])
        assert check_district_scope(c, "district-1") is False



# ---------------------------------------------------------------------------
# is_fleet_vehicle_caller (W-C3-fleet fix, 2026-09-19 fix cycle 2)
# ---------------------------------------------------------------------------

class TestIsFleetVehicleCaller:
    """Verify that is_fleet_vehicle_caller correctly identifies fleet callers.

    This helper gates the route handler's elif branch that applies a
    fleet-membership filter (Attr('scope.fleet_id').is_in(caller_fleet_ids)).
    """

    def test_fleet_operator_with_fleet_ids_is_fleet_caller(self):
        c = _claims(groups=["fleet-operator"], fleet_ids=["FLT-A"])
        is_fleet, fleet_ids = is_fleet_vehicle_caller(c)
        assert is_fleet is True
        assert fleet_ids == ["FLT-A"]

    def test_fleet_driver_with_fleet_ids_is_fleet_caller(self):
        c = _claims(groups=["fleet-driver"], fleet_ids=["FLT-B", "FLT-C"])
        is_fleet, fleet_ids = is_fleet_vehicle_caller(c)
        assert is_fleet is True
        assert fleet_ids == ["FLT-B", "FLT-C"]

    def test_fleet_operator_with_no_fleet_ids_is_fleet_scoped_with_zero_fleets(self):
        """Fail-closed: fleet-operator with empty fleetIds gets `(True, [])`.

        Behaviour CHANGED by Fix Group 29, deliberately. This previously returned
        `(False, [])`, which is the same answer given to privileged roles — and on a
        route that reads `is_fleet_caller` to decide whether to apply a restrictive
        FilterExpression, "not a fleet caller" means UNFILTERED. The old code relied on
        `check_vehicle_scope` denying separately.

        `(True, []) `is unambiguous: fleet-scoped, zero permitted fleets. Both consuming
        handlers turn it into a 403 via `if not caller_fleet_ids`
        (`api-avx-findings/handler.py:1040`, `api-avx-actions/handler.py:725`), which is
        the fail-closed answer stated once rather than depending on a second function.
        """
        c = _claims(groups=["fleet-operator"], fleet_ids=[])
        is_fleet, fleet_ids = is_fleet_vehicle_caller(c)
        assert is_fleet is True, (
            "a fleet-group caller must stay fleet-SCOPED even with no fleets; returning "
            "False here is the privileged answer and lets the route go unfiltered"
        )
        assert fleet_ids == []

    def test_platform_admin_is_not_fleet_caller(self):
        """platform-admin bypasses fleet filtering — must NOT be detected as fleet caller."""
        c = _claims(groups=["platform-admin"])
        is_fleet, fleet_ids = is_fleet_vehicle_caller(c)
        assert is_fleet is False
        assert fleet_ids == []

    def test_platform_admin_with_fleet_ids_is_not_fleet_caller(self):
        """platform-admin with fleetIds still bypasses fleet filtering."""
        c = _claims(groups=["platform-admin", "fleet-operator"], fleet_ids=["FLT-A"])
        is_fleet, fleet_ids = is_fleet_vehicle_caller(c)
        assert is_fleet is False
        assert fleet_ids == []

    def test_service_advisor_is_not_fleet_caller(self):
        """service-advisor sees all VINs — must NOT be detected as fleet caller."""
        c = _claims(groups=["service-advisor"])
        is_fleet, fleet_ids = is_fleet_vehicle_caller(c)
        assert is_fleet is False
        assert fleet_ids == []

    def test_technician_is_not_fleet_caller(self):
        c = _claims(groups=["technician"])
        is_fleet, fleet_ids = is_fleet_vehicle_caller(c)
        assert is_fleet is False
        assert fleet_ids == []

    def test_forged_custom_role_no_longer_escapes_fleet_filtering(self):
        """Review cycle 2 (C1) — the sharper half of the deleted claim arm.

        This test previously asserted `is_fleet is False` with the docstring
        "custom:role=service-advisor bypasses fleet filtering", i.e. it pinned the
        bypass as intended behaviour. But the claim was evaluated BEFORE the
        fleet-group branch, so a LEGITIMATE fleet-operator in fleet A could write
        `custom:role` on their own record and read Findings for VINs in fleet B —
        the exact cross-fleet leak the 2026-09-19 W-C3-fleet fix exists to prevent.

        Now a fleet caller stays a fleet caller regardless of the claim, and the
        FilterExpression is applied.
        """
        for forged in ("service-advisor", "technician"):
            c = _claims(groups=["fleet-driver"], fleet_ids=["FLT-A"], custom_role=forged)
            is_fleet, fleet_ids = is_fleet_vehicle_caller(c)
            assert is_fleet is True, f"custom:role={forged} still escapes fleet filtering"
            assert fleet_ids == ["FLT-A"]

    @pytest.mark.parametrize("privileged", ["service-advisor", "technician", "platform-admin"])
    def test_real_service_role_group_still_bypasses_fleet_filtering(self, privileged):
        """Unchanged: GROUP membership is admin-assigned and still privileged.

        The test that fails if the deletion were over-broad — and review cycle 3 (W1)
        proved the earlier version of it could NOT fail that way. It supplied one
        privileged group and NO fleet group, so with the privileged short-circuit
        deleted control simply fell through the fleet branch (which the caller did not
        match) to the same `return False, []`. The assertion was satisfied by the
        fall-through, not by the branch it names.

        The mutation it exists for — narrowing the privileged set to
        `{"platform-admin"}`, silently stripping `service-advisor` and `technician` —
        was invisible to all 318 tests across the three consumer packages. That
        matters concretely: `service-advisor` is the one privileged role with a live,
        group-provisioned member, and under that mutation such a caller who also holds
        a fleet group gets a 503 instead of unfiltered results.

        Fixed by giving the caller BOTH a privileged group and a fleet group, so the
        two branches produce different answers and only the privileged short-circuit
        can yield `(False, [])`.
        """
        c = _claims(groups=[privileged, "fleet-operator"], fleet_ids=["FLT-A"])
        is_fleet, fleet_ids = is_fleet_vehicle_caller(c)
        assert is_fleet is False, (
            f"{privileged} group must retain privileged (unfiltered) status even when "
            "the caller also holds a fleet group — otherwise it is routed into the "
            "fleet-filtered query"
        )
        assert fleet_ids == []

    def test_consumer_is_not_fleet_caller(self):
        """Consumer is handled by the is_owner_caller branch, not fleet."""
        c = _claims(groups=["consumer"])
        is_fleet, fleet_ids = is_fleet_vehicle_caller(c)
        assert is_fleet is False
        assert fleet_ids == []

    def test_empty_groups_is_not_fleet_caller(self):
        c = _claims(groups=[])
        is_fleet, fleet_ids = is_fleet_vehicle_caller(c)
        assert is_fleet is False
        assert fleet_ids == []



@pytest.mark.untrusted_claims
class TestMembershipClaimTrustGates:
    """Fix Group 29: every membership-claim read fails closed when untrusted.

    These run with `AVX_FLEET_IDS_CLAIM_TRUSTED`, `AVX_DEALER_IDS_CLAIM_TRUSTED` and
    `AVX_DISTRICT_IDS_CLAIM_TRUSTED` all CLEARED — the deployed default. Every other
    test in this module runs with them set, so without this class the gates would be
    invisible to the suite.

    Why the gates exist: `custom:*` attributes are self-writable under an app client's
    default `WriteAttributes`. Review cycle 3 measured three routes returning 200 with
    another organisation's Findings on a forged claim. See
    `issues/2026-09-23-avx-membership-claims-ungated-on-read-paths/`.

    Every assertion here pairs a claim that WOULD grant (and does, in the trusted tests
    above) with the untrusted env, so a passing test means the gate denied rather than
    the fixture being wrong.
    """

    def test_fleet_driver_vehicle_scope_denied(self):
        c = _claims(groups=["fleet-driver"], fleet_ids=["FLT-A"])
        assert check_vehicle_scope(c, "VIN-001") is False

    def test_fleet_operator_vehicle_scope_denied(self):
        c = _claims(groups=["fleet-operator"], fleet_ids=["FLT-A"])
        assert check_vehicle_scope(c, "VIN-001") is False

    def test_fleet_scope_denied(self):
        c = _claims(groups=["fleet-operator"], fleet_ids=["FLT-A"])
        assert check_fleet_scope(c, "FLT-A") is False

    def test_fleet_scope_denied_even_for_platform_admin(self):
        """The fleet asymmetry holds: platform-admin never bypassed the MEMBERSHIP
        predicate, only the group requirement. So an untrusted claim denies here too."""
        c = _claims(groups=["platform-admin"], fleet_ids=["FLT-A"])
        assert check_fleet_scope(c, "FLT-A") is False

    def test_dealer_scope_denied(self):
        c = _claims(groups=["dealer-admin"], dealer_ids=["D-1"])
        assert check_dealer_scope(c, "D-1") is False

    def test_district_scope_denied(self):
        c = _claims(groups=["district-manager"], district_ids=["DIST-1"])
        assert check_district_scope(c, "DIST-1") is False

    def test_owner_fleet_scope_fleet_grant_denied_but_owner_self_read_survives(self):
        """The gate must remove the FLEET grant without breaking owner self-read.

        `check_owner_fleet_scope` falls through to `caller_sub == owner_id`. Gating the
        fleet claim must not take that legitimate, non-forgeable path with it.
        """
        c = _claims(sub="owner-1", groups=["fleet-operator"], fleet_ids=["FLT-A"])
        assert check_owner_fleet_scope(c, "someone-else", owner_fleet_id="FLT-A") is False, (
            "an untrusted fleet claim must not grant access to another owner's findings"
        )
        assert check_owner_fleet_scope(c, "owner-1", owner_fleet_id="FLT-A") is True, (
            "owner self-read derives from `sub`, which is not self-writable — the gate "
            "must not deny it"
        )

    def test_untrusted_fleet_claim_stays_fleet_scoped_not_privileged(self):
        """The one site where `[]` alone would fail OPEN.

        `is_fleet_vehicle_caller` returns the set a restrictive FilterExpression is built
        from. `(False, [])` is the PRIVILEGED answer — it tells the route this caller is
        not fleet-scoped, and the route returns unfiltered results. So an untrusted claim
        must yield `(True, [])`: fleet-scoped, zero permitted fleets, which both
        consuming handlers turn into a 403.

        Mutating this to `(False, [])` is `M-FG29-8` and must fail here.
        """
        c = _claims(groups=["fleet-operator"], fleet_ids=["FLT-A"])
        is_fleet, fleet_ids = is_fleet_vehicle_caller(c)
        assert is_fleet is True, (
            "an untrusted fleet claim must leave the caller FLEET-SCOPED; returning "
            "False is the privileged answer and sends the route down the unfiltered path"
        )
        assert fleet_ids == [], "no fleet may be trusted from an untrusted claim"

    def test_privileged_groups_are_unaffected_by_the_gates(self):
        """Group membership is admin-assigned, so the gates must not touch it.

        The canary for an over-broad gate: if these flipped, the gates would have taken
        the non-forgeable path with them.
        """
        for group in ("platform-admin", "service-advisor", "technician"):
            c = _claims(groups=[group])
            assert check_vehicle_scope(c, "VIN-001") is True, (
                f"{group} reads all VINs by GROUP, not by claim — gate must not affect it"
            )
            is_fleet, fleet_ids = is_fleet_vehicle_caller(c)
            assert (is_fleet, fleet_ids) == (False, []), (
                f"{group} must remain privileged (unfiltered), not fleet-scoped"
            )

    def test_dealer_and_district_platform_admin_bypass_still_works(self):
        """platform-admin bypasses the dealer/district membership predicate entirely
        (the documented asymmetry vs fleet), and that bypass is group-based."""
        c = _claims(groups=["platform-admin"])
        assert check_dealer_scope(c, "D-1") is True
        assert check_district_scope(c, "DIST-1") is True


class TestTrustFlagParsing:
    """The flags open only on an exact `"true"`."""

    @pytest.mark.parametrize("value,expected", [
        ("true", True), ("TRUE", True), ("  true  ", True), ("True", True),
        ("1", False), ("yes", False), ("false", False), ("false ", False),
        ("", False), ("truthy", False),
    ])
    def test_only_exact_true_opts_in(self, monkeypatch, value, expected):
        monkeypatch.setenv("AVX_TEST_FLAG", value)
        assert claim_is_trusted("AVX_TEST_FLAG") is expected

    def test_absent_is_closed(self, monkeypatch):
        monkeypatch.delenv("AVX_TEST_FLAG", raising=False)
        assert claim_is_trusted("AVX_TEST_FLAG") is False



def test_trust_var_names_match_the_source():
    """`lambdas/conftest.py` hardcodes the trust var names; pin them here.

    That conftest cannot import from `_lib.avx_scope` — it is collected before any
    lambda root is on `sys.path` — so it carries three literal strings. A literal
    duplicated across files is exactly the drift that produced review cycle 3's C1
    (a gate spelled in one module and missing from another), so this asserts the two
    spellings agree.

    Mutation: rename either side without the other -> this fails.
    """
    import pathlib
    import re

    conftest = (pathlib.Path(__file__).parent.parent.parent / "conftest.py").read_text()
    block = re.search(r"_MEMBERSHIP_TRUST_VARS = \((.*?)\)", conftest, re.S)
    assert block, "could not find _MEMBERSHIP_TRUST_VARS in lambdas/conftest.py"
    in_conftest = set(re.findall(r'"([A-Z_]+)"', block.group(1)))

    from avx_scope import (
        DEALER_IDS_CLAIM_TRUSTED_ENV,
        DISTRICT_IDS_CLAIM_TRUSTED_ENV,
        FLEET_IDS_CLAIM_TRUSTED_ENV,
    )
    in_source = {
        FLEET_IDS_CLAIM_TRUSTED_ENV,
        DEALER_IDS_CLAIM_TRUSTED_ENV,
        DISTRICT_IDS_CLAIM_TRUSTED_ENV,
    }

    assert in_conftest == in_source, (
        f"lambdas/conftest.py trust vars {sorted(in_conftest)} disagree with "
        f"avx_scope.py {sorted(in_source)} — a rename desynced them, and every "
        "membership test would silently run in the WRONG trust state"
    )
    # Non-vacuity: both sides must be non-empty, or equality is trivially satisfied.
    assert len(in_source) == 3, "expected exactly three membership trust flags"



class TestBlankMembershipValues:
    """Review cycle 4 (Suggestion): a blank claim is not membership.

    `custom:fleetIds = ""` parses to `[""]`, which is truthy, so
    `if fleet_ids: return True` granted on it. Not a cross-tenant grant — no real id is
    the empty string — but it is the same emptiness-vs-identity confusion review cycle 1
    found on the driver route, and it made `is_fleet_vehicle_caller` return `(True, [""])`.

    Runs TRUSTED (no `untrusted_claims` marker) on purpose: the point is that a blank
    value is rejected even when the claim is trusted, which the gate alone would not do.
    """

    @pytest.mark.parametrize("blank", ["", "   ", ",", " , ", ";", "-", '[""]', '["  "]'])
    def test_blank_fleet_claim_is_not_membership(self, blank):
        c = _claims(groups=["fleet-operator"])
        c["custom:fleetIds"] = blank
        assert check_vehicle_scope(c, "VIN-001") is False, (
            f"blank fleet claim {blank!r} must not grant vehicle scope"
        )
        is_fleet, fleet_ids = is_fleet_vehicle_caller(c)
        assert is_fleet is True, "still fleet-scoped (not privileged)"
        assert fleet_ids == [], f"blank claim {blank!r} yielded {fleet_ids!r}"

    @pytest.mark.parametrize("blank", ["", "   ", ","])
    def test_blank_dealer_and_district_claims_are_not_membership(self, blank):
        c = _claims(groups=["dealer-admin"])
        c["custom:dealerIds"] = blank
        assert check_dealer_scope(c, "") is False, (
            "a blank dealer claim must not match a blank dealer_id either — emptiness "
            "is not identity on both sides"
        )
        c2 = _claims(groups=["district-manager"])
        c2["custom:districtIds"] = blank
        assert check_district_scope(c2, "") is False

    def test_real_values_still_pass_through_unchanged(self):
        """The canary: the blank filter must not drop legitimate members."""
        c = _claims(groups=["fleet-operator"], fleet_ids=["FLT-A", "FLT-B"])
        assert check_fleet_scope(c, "FLT-A") is True
        assert check_fleet_scope(c, "FLT-B") is True
        is_fleet, fleet_ids = is_fleet_vehicle_caller(c)
        assert (is_fleet, sorted(fleet_ids)) == (True, ["FLT-A", "FLT-B"])
