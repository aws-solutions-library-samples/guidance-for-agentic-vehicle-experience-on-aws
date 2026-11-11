"""Unit tests for persona_map.infer_persona.

Verifies the claim→persona mapping table per spec § "Persona mapping".
"""

from __future__ import annotations

import sys
import os

# Allow direct import from the lambda root without a package install.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from persona_map import infer_persona, DEFAULT_PERSONA


# ---------------------------------------------------------------------------
# platform-admin / fleet-operator → fleet_driver
# ---------------------------------------------------------------------------


def test_group_platform_admin_returns_fleet_driver():
    claims = {"cognito:groups": "platform-admin"}
    assert infer_persona(claims) == "fleet_driver"


def test_group_fleet_operator_returns_fleet_driver():
    claims = {"cognito:groups": "fleet-operator"}
    assert infer_persona(claims) == "fleet_driver"


def test_multiple_groups_with_platform_admin_returns_fleet_driver():
    claims = {"cognito:groups": "fleet-viewer,platform-admin"}
    assert infer_persona(claims) == "fleet_driver"


# ---------------------------------------------------------------------------
# custom:role = service-advisor → service_advisor
# ---------------------------------------------------------------------------


def test_role_service_advisor_returns_service_advisor():
    claims = {"custom:role": "service-advisor"}
    assert infer_persona(claims) == "service_advisor"


def test_role_service_advisor_underscore_variant():
    claims = {"custom:role": "service_advisor"}
    assert infer_persona(claims) == "service_advisor"


# ---------------------------------------------------------------------------
# Unknown / missing → fleet_driver (default)
# ---------------------------------------------------------------------------


def test_unknown_group_returns_fleet_driver():
    claims = {"cognito:groups": "some-unknown-group"}
    assert infer_persona(claims) == DEFAULT_PERSONA


def test_unknown_role_does_not_escalate_to_fleet_driver():
    """An unrecognised `custom:role` must NOT grant the owner-scoped persona.

    This test previously asserted `== DEFAULT_PERSONA`, which encoded a live
    privilege escalation as intended behaviour. `custom:role` is a mutable user
    attribute and the pool's app clients leave `WriteAttributes` unset —
    Cognito's permissive default — so any authenticated user can set it on
    themselves with only their own access token. Verified live on the staging
    pool 2026-08-03: `update-user-attributes --access-token <own>
    --user-attributes Name=custom:role,Value=totally-made-up-role` succeeded and
    the value appeared in the next ID token, at which point `infer_persona`
    returned `fleet_driver` — handing a `consumer` the owner-scoped
    `customer_360`, `customer_interactions`, and `vehicle_identity` bindings.

    Only admin-assigned `cognito:groups` may widen the persona now. See
    security-review Cycle 7 S3 and the step-6 comment in persona_map.py.
    """
    claims = {"custom:role": "super-admin"}
    assert infer_persona(claims) != DEFAULT_PERSONA, (
        "A self-writable attribute must not confer owner scope."
    )
    assert infer_persona(claims) == "consumer"


def test_forged_role_matching_a_group_name_does_not_escalate():
    """Naming the role after a privileged GROUP must not borrow that group's persona."""
    for forged in ("platform-admin", "fleet-operator", "fleet-viewer", "kiosk"):
        assert infer_persona({"custom:role": forged}) == "consumer", (
            f"custom:role={forged!r} escalated via the group namespace"
        )


def test_genuine_unmapped_group_still_gets_default_persona():
    """The legitimate case the old fallback protected is preserved.

    Group membership requires admin credentials (`AdminAddUserToGroup`), so a
    real-but-unmapped group is a trustworthy signal and keeps the pre-kiosk
    surface for existing CMS users.
    """
    assert infer_persona({"cognito:groups": "cms-some-real-group"}) == DEFAULT_PERSONA


def test_recognised_role_still_maps_normally():
    """Closing the fallback must not break the explicit role mapping at step 3."""
    assert infer_persona({"custom:role": "service-advisor"}) == "service_advisor"
    assert infer_persona({"custom:role": "service_advisor"}) == "service_advisor"


def test_empty_claims_returns_anonymous_persona():
    # After the spec D3 fail-open fix: claimless input returns the narrow
    # anonymous persona ("consumer"), not the owner-scoped DEFAULT_PERSONA.
    # DEFAULT_PERSONA ("fleet_driver") is for authenticated CMS users whose
    # claims matched an unrecognised group/role — not for claimless input.
    # This test was updated in Group 3 to reflect the corrected contract;
    # the original assertion (== DEFAULT_PERSONA) encoded the security defect.
    assert infer_persona({}) != DEFAULT_PERSONA
    assert infer_persona({}) == "consumer"


def test_none_values_return_anonymous_persona():
    # Same rationale as test_empty_claims_returns_anonymous_persona above.
    # None values for cognito:groups and custom:role are treated as absent
    # claims, which gives the anonymous/kiosk-narrow "consumer" persona.
    claims = {"cognito:groups": None, "custom:role": None}
    result = infer_persona(claims)
    assert result != DEFAULT_PERSONA
    assert result == "consumer"


def test_fleet_viewer_group_returns_fleet_driver():
    claims = {"cognito:groups": "fleet-viewer"}
    assert infer_persona(claims) == "fleet_driver"


def test_default_is_fleet_driver():
    assert DEFAULT_PERSONA == "fleet_driver"


# ---------------------------------------------------------------------------
# Kiosk group → consumer (spec 2026-07-31-zone2-order-backend-and-kiosk D3)
# ---------------------------------------------------------------------------
# These tests are RED until Group 3 implements the kiosk rule in persona_map.py.


def test_kiosk_group_resolves_to_consumer():
    """Claim carrying the 'kiosk' Cognito group must map to the consumer persona."""
    claims = {"cognito:groups": "kiosk"}
    # FAILS RED: current code has no kiosk rule → returns fleet_driver
    assert infer_persona(claims) == "consumer"


def test_kiosk_group_ignores_client_supplied_persona():
    """A client-supplied persona override in claims must be ignored for kiosk.

    The claim decides persona server-side; a client-injected 'fleet_driver'
    custom:role must not escalate a kiosk caller to an owner-scoped persona.
    """
    claims = {
        "cognito:groups": "kiosk",
        "custom:role": "fleet_driver",  # client-supplied override attempt
    }
    # FAILS RED: current code has no kiosk rule → custom:role wins (fleet_driver)
    assert infer_persona(claims) == "consumer"


def test_kiosk_group_overrides_service_advisor_role():
    """Kiosk group must take priority over any custom:role, including service-advisor."""
    claims = {
        "cognito:groups": "kiosk",
        "custom:role": "service-advisor",
    }
    # FAILS RED: current code returns service_advisor via _ROLE_MAP lookup
    assert infer_persona(claims) == "consumer"


def test_claimless_input_does_not_resolve_to_fleet_driver():
    """Claimless (anonymous/unrecognised) input must NOT fall back to fleet_driver.

    fleet_driver is an owner-scoped persona; resolving anonymous input to it is
    the live fail-open defect described in spec D3. The correct default for
    unrecognised input is a non-owner-scoped persona (e.g. 'consumer').
    """
    # FAILS RED: current infer_persona({}) == "fleet_driver"
    result = infer_persona({})
    assert result != "fleet_driver", (
        f"Claimless input must not resolve to the owner-scoped 'fleet_driver' "
        f"persona; got {result!r}. Fix: change DEFAULT_PERSONA or the final "
        f"fallback in infer_persona() per spec D3."
    )


def test_empty_groups_does_not_resolve_to_fleet_driver():
    """Empty cognito:groups with no other claims must not give fleet_driver."""
    claims = {"cognito:groups": ""}
    result = infer_persona(claims)
    # FAILS RED: current code returns fleet_driver for empty groups
    assert result != "fleet_driver", (
        f"Empty-groups claim must not resolve to owner-scoped 'fleet_driver'; "
        f"got {result!r}."
    )


# ---------------------------------------------------------------------------
# Fix Group 2 — case-insensitive group parsing (W3 security fix)
# ---------------------------------------------------------------------------


def test_kiosk_group_uppercase_resolves_to_consumer():
    """'KIOSK' (uppercase) must resolve to consumer, not leak to service_advisor."""
    claims = {"cognito:groups": "KIOSK", "custom:role": "service-advisor"}
    result = infer_persona(claims)
    assert result == "consumer", (
        f"Uppercase 'KIOSK' group must be normalised case-insensitively and "
        f"resolve to consumer; got {result!r}. "
        "Before fix: KIOSK was treated as unknown group, custom:role won → service_advisor."
    )


def test_kiosk_group_uppercase_list_resolves_to_consumer():
    """List-shaped ['KIOSK'] must also be case-insensitively matched."""
    claims = {"cognito:groups": ["KIOSK"], "custom:role": "service-advisor"}
    result = infer_persona(claims)
    assert result == "consumer", (
        f"List-shaped uppercase ['KIOSK'] must resolve to consumer; got {result!r}."
    )


def test_empty_groups_with_service_advisor_role_preserves_cms_ui_path():
    """cognito:groups='' with custom:role=service-advisor still gives service_advisor.

    The CMS web UI service-advisor path may carry custom:role without groups.
    The W3 security fix (case-insensitive groups) does not change role handling;
    it only ensures KIOSK is normalised. Role-without-groups must still work
    so the CMS web UI is not broken. This test is a regression guard.
    """
    claims = {"cognito:groups": "", "custom:role": "service-advisor"}
    result = infer_persona(claims)
    # Empty groups means no groups match at step 2/3; role check at step 4 still wins
    assert result == "service_advisor", (
        f"CMS web UI: empty groups + service-advisor role must give service_advisor; "
        f"got {result!r}. Do not break platform-admin / fleet-operator / service-advisor "
        f"for the CMS web UI."
    )


def test_role_without_any_groups_key_gives_service_advisor():
    """A claim with custom:role but NO cognito:groups key gives service_advisor.

    This is the existing CMS web UI path — service-advisor users may not have
    a cognito:groups claim at all. Role-without-groups must remain functional.
    Regression guard: the W3 fix must not break this path.
    """
    claims = {"custom:role": "service-advisor"}  # no cognito:groups at all
    result = infer_persona(claims)
    assert result == "service_advisor", (
        f"custom:role without groups must still give service_advisor (CMS web UI path); "
        f"got {result!r}."
    )


def test_service_advisor_role_with_valid_group_still_works():
    """service-advisor role WITH a recognised group claim still resolves correctly.

    Regression guard: fixing role-without-groups must not break the legitimate
    CMS web UI path where a user has both a group and the service-advisor role.
    """
    # A service-advisor also carrying fleet-operator group
    claims = {"cognito:groups": "fleet-operator", "custom:role": "service-advisor"}
    result = infer_persona(claims)
    # fleet-operator wins over service-advisor (step 3 before step 4 in priority)
    assert result == "fleet_driver"


def test_service_advisor_role_with_recognised_unordered_group():
    """service-advisor role + fleet-viewer group → service_advisor via step 4.

    fleet-viewer does not short-circuit at step 3 (only platform-admin /
    fleet-operator do). The role at step 4 wins, because groups is non-empty.
    """
    claims = {"cognito:groups": "fleet-viewer", "custom:role": "service-advisor"}
    result = infer_persona(claims)
    assert result == "service_advisor", (
        f"service-advisor role with fleet-viewer group (no early-exit) should "
        f"resolve to service_advisor via step 4; got {result!r}."
    )


# ---------------------------------------------------------------------------
# DMS Cognito group → persona mappings (spec 2026-08-26-cvx-dms-personas-extension T6.3)
# ---------------------------------------------------------------------------
# These tests cover the 7 DMS Cognito groups added to _GROUP_MAP.  The
# key invariants verified here:
#   1. Each group maps to the correct persona slug.
#   2. No DMS group can be used by a claimless/anonymous caller (impossible by
#      construction — step 6 requires bool(groups); step 5 only fires on non-
#      empty groups).  Tested via the existing security tests above.
#   3. The kiosk group still short-circuits all DMS groups (step 2 wins).
#   4. The platform-admin / fleet-operator early-exit still wins over DMS groups
#      when both are present (step 3 wins).
#   5. The existing 7 non-DMS personas are not changed.
# ---------------------------------------------------------------------------


def test_dms_dealer_admin_group_returns_service_advisor():
    """dealer-admin Cognito group → service_advisor.

    dealer-admin is the highest-privilege DMS user.  service_advisor is chosen
    because it has the broadest operational tool surface (customer_360,
    customer_interactions, vehicle_identity, service_records + knowledge /
    parts_lookup / retrieve), matching what a dealer admin needs.  warranty_agent
    was rejected: too narrow (warranty claims only, not the full service-lane
    picture).
    """
    assert infer_persona({"cognito:groups": "dealer-admin"}) == "service_advisor"


def test_dms_service_advisor_group_returns_service_advisor():
    """service-advisor Cognito GROUP → service_advisor.

    The DMS provisioning flow may assign a Cognito group named 'service-advisor'
    rather than setting the custom:role attribute, depending on the IdP
    integration.  Both paths must resolve to the same persona so behaviour is
    consistent regardless of how the user was provisioned.  NOTE: this mapping
    is in _GROUP_MAP (step 5); the custom:role path is handled at step 4 and is
    unaffected by this test.
    """
    assert infer_persona({"cognito:groups": "service-advisor"}) == "service_advisor"


def test_dms_district_manager_group_returns_dm_copilot():
    """district-manager Cognito group → dm_copilot.

    dm_copilot provides visit_brief / portfolio_summary / action_plan — the
    purpose-built surface for district-level fleet analytics.
    """
    assert infer_persona({"cognito:groups": "district-manager"}) == "dm_copilot"


def test_dms_bdc_rep_group_returns_comms_triage():
    """bdc-rep Cognito group → comms_triage.

    Business Development Center reps handle inbound/outbound dealer
    communications.  comms_triage's classify_bulletin() is the correct match.
    fleet_driver was considered but carries vehicle-control tools irrelevant to
    a BDC rep.
    """
    assert infer_persona({"cognito:groups": "bdc-rep"}) == "comms_triage"


def test_dms_parts_manager_group_returns_parts_intel():
    """parts-manager Cognito group → parts_intel.

    parts_intel is the DMS v1.1 stub persona for parts-catalog queries.  Even
    as a stub it is the correct destination so the registry does not crash on
    a known persona_id and the v1.1 implementation lands in the correct slot.
    """
    assert infer_persona({"cognito:groups": "parts-manager"}) == "parts_intel"


def test_dms_f_and_i_manager_group_returns_service_advisor():
    """f-and-i-manager Cognito group → service_advisor.

    Finance & Insurance managers need customer profile data (customer_360,
    customer_interactions).  service_advisor has the closest tool surface.
    A bespoke f-and-i persona is a v1.1 consideration.  fleet_driver was
    rejected: carries vehicle-control tools irrelevant to F&I.
    """
    assert infer_persona({"cognito:groups": "f-and-i-manager"}) == "service_advisor"


def test_dms_viewer_group_returns_consumer():
    """dms-viewer Cognito group → consumer.

    dms-viewer is the narrowest DMS privilege (read-only).  consumer is the
    narrowest non-error persona available.  fleet_driver is wrong (owner-scoped,
    carries vehicle-control tools).  This is deliberately conservative; widening
    requires an explicit v1.1 decision.
    """
    assert infer_persona({"cognito:groups": "dms-viewer"}) == "consumer"


# ---- Security invariants for DMS groups ----

def test_dms_groups_do_not_override_kiosk():
    """Kiosk group short-circuits all DMS groups — consumer wins regardless."""
    dms_groups = [
        "dealer-admin",
        "service-advisor",
        "district-manager",
        "bdc-rep",
        "parts-manager",
        "f-and-i-manager",
        "dms-viewer",
    ]
    for dms_group in dms_groups:
        result = infer_persona({"cognito:groups": f"kiosk,{dms_group}"})
        assert result == "consumer", (
            f"kiosk+{dms_group}: kiosk short-circuit must still win; got {result!r}"
        )


def test_platform_admin_overrides_dms_groups():
    """platform-admin in the same token wins over DMS groups (step 3 before step 5)."""
    # district-manager (→ dm_copilot) + platform-admin (→ fleet_driver): fleet_driver wins
    result = infer_persona({"cognito:groups": "platform-admin,district-manager"})
    assert result == "fleet_driver", (
        f"platform-admin must override district-manager; got {result!r}"
    )
    # bdc-rep (→ comms_triage) + fleet-operator (→ fleet_driver): fleet_driver wins
    result = infer_persona({"cognito:groups": "fleet-operator,bdc-rep"})
    assert result == "fleet_driver", (
        f"fleet-operator must override bdc-rep; got {result!r}"
    )


def test_dms_groups_are_not_reachable_claimlessly():
    """No DMS persona appears in the claimless/anonymous fallback path.

    The anonymous fallback is consumer (_ANONYMOUS_PERSONA), and the
    DEFAULT_PERSONA fallback (for unknown-but-genuine groups) is fleet_driver.
    Neither is a DMS persona.  This test is belt-and-suspenders: infer_persona's
    step 5 can only return a _GROUP_MAP value when groups is non-empty, and
    step 6 only returns DEFAULT_PERSONA or _ANONYMOUS_PERSONA regardless of
    _GROUP_MAP contents.  Verifies the invariant holds after our additions.
    """
    dms_persona_slugs = {
        "warranty_agent", "dm_copilot", "comms_triage",
        "parts_intel", "compliance_agent",
    }
    # Claimless
    assert infer_persona({}) not in dms_persona_slugs
    # Empty groups string
    assert infer_persona({"cognito:groups": ""}) not in dms_persona_slugs
    # None groups
    assert infer_persona({"cognito:groups": None}) not in dms_persona_slugs
    # Unknown group (genuine but unrecognised) — gets DEFAULT_PERSONA (fleet_driver)
    assert infer_persona({"cognito:groups": "some-unknown-group"}) not in dms_persona_slugs


def test_existing_non_dms_personas_unchanged():
    """Regression: the 7 pre-existing personas still map correctly after DMS additions.

    Any change to _GROUP_MAP that accidentally clobbers an existing entry would
    show up here before it reached production.
    """
    pre_existing = [
        ({"cognito:groups": "kiosk"},            "consumer"),
        ({"cognito:groups": "platform-admin"},   "fleet_driver"),
        ({"cognito:groups": "fleet-operator"},   "fleet_driver"),
        ({"cognito:groups": "fleet-viewer"},     "fleet_driver"),
        ({"custom:role": "service-advisor"},     "service_advisor"),
        ({"custom:role": "service_advisor"},     "service_advisor"),
        ({},                                     "consumer"),
    ]
    for claims, expected in pre_existing:
        result = infer_persona(claims)
        assert result == expected, (
            f"Pre-existing mapping {claims!r} → {expected!r} changed; got {result!r}. "
            "Do not change existing group/role mappings."
        )

