"""Persona inference from CMS Cognito authorizer claims.

Maps the claims injected by API Gateway's Cognito authorizer into a
valid CVX persona slug.  Never raises — always returns a string.

Mapping table (spec § "Persona mapping"):
    group kiosk                                → consumer   ← evaluated first
    group platform-admin | fleet-operator      → fleet_driver
    custom:role = service-advisor              → service_advisor
    recognised group (fleet-viewer, etc.)      → fleet_driver
    unrecognised but non-empty group present   → fleet_driver  (DEFAULT_PERSONA)
    no groups, no role (claimless / anonymous) → consumer   ← fail-safe default

Security note on the final fallback (spec D3):
    ``fleet_driver`` is an owner-scoped persona — it grants access to
    vehicle-control, service-booking, and escalation tooling that a public
    kiosk visitor must never reach.  Returning it as the fallback for
    claimless or unrecognised input was a fail-open security defect: on a
    public kiosk (where the Cognito token is issued to the *device*, not to
    an enrolled fleet driver) every anonymous visitor silently received an
    owner-scoped tool surface.

    The fix: only return ``fleet_driver`` (DEFAULT_PERSONA) when the caller
    has presented at least one concrete group or role claim that the mapping
    table decided warrants that persona.  A caller with *no* claims at all
    gets the narrower ``consumer`` surface instead — the same persona the
    kiosk group resolves to — so claimless input never elevates privilege.

    ``DEFAULT_PERSONA`` is kept as ``"fleet_driver"`` so that external code
    that imports and inspects the constant (e.g. handler.py's
    ``default_persona`` env-var fallback) continues to work correctly for the
    non-kiosk CMS web UI path.  The constant describes the persona for
    authenticated CMS users whose claims matched nothing unexpected; it is
    NOT the anonymous fallback.

Valid persona slugs are those present in PERSONA_PRODUCT_BINDINGS.
"""

from __future__ import annotations

# The persona for authenticated CMS users whose group/role didn't match a
# more specific rule.  NOT used as the anonymous/claimless fallback — see
# security note in module docstring.
DEFAULT_PERSONA = "fleet_driver"

# Non-owner-scoped persona returned for claimless / anonymous callers.
# Using the same persona as the kiosk group keeps the anonymous surface
# identical to the kiosk surface, which is the narrowest non-error persona
# available and has no fleet, vehicle-control, or service-booking tools.
_ANONYMOUS_PERSONA = "consumer"

_ROLE_MAP: dict[str, str] = {
    "service-advisor": "service_advisor",
    "service_advisor": "service_advisor",
}

_GROUP_MAP: dict[str, str] = {
    # kiosk MUST be listed before the owner-scoped groups so the early-exit
    # check in infer_persona() handles priority correctly.
    "kiosk": "consumer",
    "platform-admin": "fleet_driver",
    "fleet-operator": "fleet_driver",
    "fleet-viewer": "fleet_driver",
    # -------------------------------------------------------------------------
    # DMS (Dealer Management System) Cognito groups — spec
    # `2026-08-26-cvx-dms-personas-extension` § Reachability (T6.3).
    #
    # These are admin-assigned groups (`AdminAddUserToGroup`) and therefore
    # trustworthy for persona elevation, following the same reasoning as the
    # CMS fleet groups at step 6 in infer_persona().  They are processed at
    # step 5 ("remaining recognised groups") in infer_persona() — after the
    # owner-scoped CMS groups (platform-admin / fleet-operator) and after
    # custom:role, so a dealer user who is ALSO in platform-admin still gets
    # fleet_driver, which is the correct CMS-wide precedence.
    #
    # Security note: a claimless caller can NEVER land on a DMS persona.
    # infer_persona() only returns a _GROUP_MAP value when `groups` is
    # non-empty (step 5 iterates `groups`).  The final fallback at step 6
    # returns DEFAULT_PERSONA / _ANONYMOUS_PERSONA based on `bool(groups)`,
    # not on _GROUP_MAP.  Adding entries here has NO effect on the
    # claimless/anonymous path.
    #
    # Mapping rationale:
    #
    # dealer-admin → service_advisor
    #   The dealer's highest-privilege user needs the broadest operational
    #   view: customer profiles, service records, KB lookups, bulletin
    #   searches, and scheduling.  service_advisor has exactly that surface
    #   (customer_360, customer_interactions, vehicle_identity, service_records
    #   + knowledge / parts_lookup / retrieve tools).  warranty_agent was
    #   considered but is a single-domain specialist (warranty verdicts only);
    #   a dealer admin needs the full service-lane picture, not just claims.
    #
    # service-advisor (DMS Cognito group) → service_advisor
    #   Same persona as `custom:role=service-advisor`.  The Cognito group is
    #   the DMS-provisioned equivalent for users whose IdP flow sets the group
    #   rather than the custom:role attribute.  Both should resolve identically
    #   to avoid a split experience depending on how the user was provisioned.
    #   NOTE: this does NOT change the `custom:role` path — that is handled at
    #   step 4 in infer_persona() and is unaffected by _GROUP_MAP.
    #
    # district-manager → dm_copilot
    #   The district manager's tool surface (visit_brief, portfolio_summary,
    #   action_plan) is purpose-built for this role.  No other existing persona
    #   covers district-level fleet analytics.
    #
    # bdc-rep → comms_triage
    #   Business Development Center reps handle inbound/outbound dealer
    #   communications.  comms_triage's classify_bulletin() tool is the correct
    #   match.  fleet_driver was considered but its vehicle-control and booking
    #   tools are irrelevant to a BDC rep and would widen the surface
    #   unnecessarily.
    #
    # parts-manager → parts_intel
    #   The parts_intel persona is the DMS v1.1 stub for parts-catalog queries.
    #   Even as a stub (returns "v1.1 deferred"), it is the right destination
    #   so the persona registry does not crash on a known `persona_id` and so
    #   the v1.1 implementation lands in the correct slot.
    #
    # f-and-i-manager → service_advisor
    #   Finance & Insurance managers deal primarily with customer data: profiles,
    #   credit history, deal structures.  Of the available personas, service_advisor
    #   has the closest tool surface (customer_360, customer_interactions,
    #   vehicle_identity).  A bespoke f-and-i persona is a v1.1 consideration;
    #   for v1 the service_advisor surface is significantly better than the
    #   default fleet_driver (which carries vehicle-control and booking tools
    #   irrelevant to F&I) or consumer (which is too narrow).
    #
    # dms-viewer → consumer
    #   Read-only viewer role — narrowest privilege in the DMS Cognito pool.
    #   consumer is the narrowest non-error persona available.  fleet_driver
    #   is wrong (owner-scoped, carries vehicle-control tools); DEFAULT_PERSONA
    #   would be fleet_driver which is equally wrong.  A read-only DMS viewer
    #   should see the minimal surface, which consumer provides.  This is
    #   intentionally conservative — widening to a richer persona requires a
    #   deliberate v1.1 decision, not an implicit default.
    # -------------------------------------------------------------------------
    "dealer-admin": "service_advisor",
    "service-advisor": "service_advisor",  # DMS Cognito group variant
    "district-manager": "dm_copilot",
    "bdc-rep": "comms_triage",
    "parts-manager": "parts_intel",
    "f-and-i-manager": "service_advisor",
    "dms-viewer": "consumer",
    # -------------------------------------------------------------------------
    # fleet-owner: small-business fleet owner (iOS-first) — spec
    # 2026-09-18-avx-tier2-ios-cards-and-actions § "Personas (Spec 1a scope)".
    # iOS card feed + chat drill-down surface. Use cases: #8 weekly attention
    # (per-VIN roll-up) and #11 compliance monitor (recall/inspection-due).
    # NOT fleet_manager (CMS-web/Spec 3) — distinct persona slug and tool surface.
    #
    # fleet-owner is an admin-assigned Cognito group (`AdminAddUserToGroup`),
    # so it is trustworthy for persona elevation at step 5 of infer_persona()
    # (same reasoning as the other DMS and fleet groups).  A claimless or
    # kiosk caller can NEVER land here (kiosk short-circuits at step 2;
    # claimless returns _ANONYMOUS_PERSONA at step 6 because bool(groups) is False).
    # -------------------------------------------------------------------------
    "fleet-owner": "fleet_owner",
}

# Groups that resolve to an owner-scoped persona.  Kept as a named constant
# so handler.py can guard against client-supplied persona overrides that
# would re-escalate a kiosk caller.
_OWNER_SCOPED_GROUPS = frozenset({"platform-admin", "fleet-operator"})

# The kiosk group short-circuits ALL other claim processing.  A device that
# carries this group must never be escalated by a client-injected custom:role.
_KIOSK_GROUP = "kiosk"


def infer_persona(claims: dict) -> str:
    """Return the CVX persona slug for the given Cognito authorizer claims.

    Args:
        claims: Dict of Cognito authorizer claims as provided by API-GW
                (e.g. ``event["requestContext"]["authorizer"]["claims"]``).

    Returns:
        A valid CVX persona slug.  Never raises.

    Priority:
        1. ``kiosk`` group → ``consumer``  (cannot be overridden by custom:role)
        2. ``platform-admin`` / ``fleet-operator`` group → ``fleet_driver``
        3. ``custom:role = service-advisor`` → ``service_advisor``
        4. Remaining recognised groups → their mapped persona
        5. An unrecognised but genuine (admin-assigned) group → ``fleet_driver``
        6. No recognised group → ``consumer``  (fail-safe; see module docstring)

    ``custom:role`` only ever grants the persona it maps to explicitly at step 3.
    It can never widen the fallback at step 6, because it is a self-writable user
    attribute — see the comment at that step.
    """
    # --- 1. Parse group membership ---
    groups_raw = claims.get("cognito:groups") or ""
    if isinstance(groups_raw, str):
        groups = {g.strip().lower() for g in groups_raw.split(",") if g.strip()}
    else:
        groups = {str(g).strip().lower() for g in groups_raw if str(g).strip()}

    # --- 2. Kiosk group takes absolute priority ---
    # A kiosk device has a valid Cognito token (spec D2), so it will always
    # reach this branch.  Returning here before the custom:role check prevents
    # a client-injected role from escalating the kiosk surface.
    if _KIOSK_GROUP in groups:
        return "consumer"

    # --- 3. Owner-scoped groups ---
    for group in ("platform-admin", "fleet-operator"):
        if group in groups:
            return DEFAULT_PERSONA  # "fleet_driver"

    # --- 4. custom:role ---
    # Role is honoured when the caller presents a recognised role, even without
    # a groups claim. This preserves the existing CMS web UI path where
    # service-advisor users may carry custom:role without cognito:groups.
    # NOTE: kiosk devices are protected by step 2 (kiosk group short-circuits
    # before this step), so a kiosk caller cannot reach this branch.
    role = (claims.get("custom:role") or "").strip()
    if role in _ROLE_MAP:
        return _ROLE_MAP[role]

    # --- 5. Remaining recognised groups ---
    for group in groups:
        if group in _GROUP_MAP:
            return _GROUP_MAP[group]

    # --- 6. Final fallback ---
    # Only a GROUP may widen the persona here. `custom:role` deliberately does
    # NOT count toward this fallback.
    #
    # Why the asymmetry (security-review Cycle 7 S3, confirmed exploitable
    # 2026-08-03): `cognito:groups` is derived from real group membership, which
    # can only be changed with admin credentials (`AdminAddUserToGroup`). It is
    # therefore a trustworthy signal. `custom:role` is a mutable user attribute,
    # and the pool's app clients leave `WriteAttributes` unset — which is
    # Cognito's *permissive* default — so any authenticated user can set it on
    # themselves with nothing but their own access token:
    #
    #   aws cognito-idp update-user-attributes --access-token <own token> \
    #     --user-attributes Name=custom:role,Value=anything
    #
    # Verified live on the staging pool: the attribute wrote successfully and
    # appeared in the next ID token. With `or bool(role)` in this condition, that
    # made ANY non-empty role string — recognised or not — resolve to
    # DEFAULT_PERSONA (`fleet_driver`), an owner-scoped persona carrying
    # `customer_360`, `customer_interactions`, and `vehicle_identity`. A
    # `consumer` could self-promote to owner scope with one CLI call. Note the
    # escalation worked *better* with a garbage value than a real one, since a
    # recognised role returns its own narrow mapping at step 4.
    #
    # An unrecognised role now contributes nothing: a caller presenting only a
    # role it made up is treated as claimless and gets the narrow
    # _ANONYMOUS_PERSONA. A caller with a genuine (admin-assigned) group that
    # simply is not in _GROUP_MAP still gets DEFAULT_PERSONA, which preserves the
    # pre-kiosk surface for real CMS users — the case the previous comment was
    # protecting, without the forgeable half.
    #
    # Kiosk devices never reach here (step 2 short-circuits).
    has_trusted_claim = bool(groups)
    return DEFAULT_PERSONA if has_trusted_claim else _ANONYMOUS_PERSONA


# Groups that constitute fleet authorization. Used by caller_has_fleet_group
# to gate the fleet_manager persona request path in handler.py.
# These are admin-controlled Cognito groups (AdminAddUserToGroup), not
# self-writable user attributes — see step-6 security note above.
_FLEET_AUTHORIZED_GROUPS = frozenset({"platform-admin", "fleet-operator", "fleet-viewer"})


def caller_has_fleet_group(claims: dict) -> bool:
    """Return True iff the caller's Cognito groups include a fleet-authorized group.

    Used by handler.py to gate the group-gated ``fleet_manager`` persona request
    path: a caller who requests ``fleet_manager`` is honored only when this
    returns True.

    Fleet-authorized groups: ``platform-admin``, ``fleet-operator``, ``fleet-viewer``.

    Reuses the same lower-cased comma-split parsing that ``infer_persona`` uses so
    the two functions agree on group membership.  The gate MUST read
    ``cognito:groups`` (admin-controlled, not self-writable) — see the step-6
    security note in ``infer_persona`` for why ``custom:role`` cannot be used here.

    Args:
        claims: Dict of Cognito authorizer claims as provided by API-GW.

    Returns:
        True iff the caller carries at least one fleet-authorized group.
        Never raises.
    """
    groups_raw = claims.get("cognito:groups") or ""
    if isinstance(groups_raw, str):
        groups = {g.strip().lower() for g in groups_raw.split(",") if g.strip()}
    else:
        groups = {str(g).strip().lower() for g in groups_raw if str(g).strip()}
    return bool(groups & _FLEET_AUTHORIZED_GROUPS)


def is_kiosk_claim(claims: dict) -> bool:
    """Return True iff the claims carry the kiosk Cognito group.

    Used by handler.py to set ``isKiosk=True`` in the AgentCore runtime
    payload so ``agentcore_app.py`` can pass it to the ``SessionContext``
    constructor (spec 2026-07-31-zone2-order-backend-and-kiosk § Critical fix).

    Distinct from the ``infer_persona``-returned "consumer" value: the
    ``_ANONYMOUS_PERSONA`` fallback for claimless input also yields
    "consumer", but that case is NOT a kiosk session — the device did not
    carry a kiosk group token.  Only the explicit kiosk group should set
    ``is_kiosk=True`` on the session context, so the allow-list gate is
    tight to provisioned kiosk devices, not to every anonymous caller.

    Never raises.
    """
    groups_raw = claims.get("cognito:groups") or ""
    if isinstance(groups_raw, str):
        groups = {g.strip().lower() for g in groups_raw.split(",") if g.strip()}
    else:
        groups = {str(g).strip().lower() for g in groups_raw if str(g).strip()}
    return _KIOSK_GROUP in groups
