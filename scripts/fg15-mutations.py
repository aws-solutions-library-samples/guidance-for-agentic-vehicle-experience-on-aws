#!/usr/bin/env python3
"""Fix Group 15 mutation harness — F11.2 + F11.3.

Applies each named mutation to PRODUCTION source, runs the test that is supposed
to catch it, asserts the test FAILS, then restores the file byte-for-byte.

Run from the repo root:  python3 scripts/fg15-mutations.py

Exit 0 only if every mutation was caught and every file was restored. A mutation
that leaves the suite green is reported as NOT CAUGHT — that is a coverage gap,
not an acceptable state (see ~/.kiro/steering/testing.md § "Mutation testing at
the green boundary").
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

WRITER = "agents/tier2/writer.py"
RUNNER = "agents/tier2/runner.py"
RESOLVER = "agents/tier2/owner_resolver.py"
HANDLER = "lambdas/api-avx-findings/handler.py"
# Shared scope helpers — review cycle 2 (C1) deleted two `custom:role` arms here, so
# the harness now reaches beyond the FG15/25 surface into `_lib`.
SCOPE = "lambdas/_lib/avx_scope.py"
# The AVX actions handler — Fix Group 29's eighth gated site lives here, outside the
# review's list of seven, which was scoped to avx_scope.py plus the findings handler.
ACTIONS = "lambdas/api-avx-actions/handler.py"

OWNER_TESTS = "agents/tier2/tests/test_runner_projects_owner_id.py"
HANDLER_TESTS = "lambdas/api-avx-findings/tests/test_handler.py"
SCOPE_TESTS = "lambdas/_lib/tests/test_avx_scope.py"
ACTIONS_TESTS = "lambdas/api-avx-actions/tests/test_handler.py"


@dataclass
class Mutation:
    mid: str
    prop: str
    path: str
    old: str
    new: str
    tests: list[str] = field(default_factory=list)
    note: str = ""


MUTATIONS: list[Mutation] = [
    # ── F11.2 ───────────────────────────────────────────────────────────────
    Mutation(
        "M-FG11-1", "2 — projection missing", WRITER,
        old="    item_with_scope.update(_standing_projection(finding, standing))",
        new="    pass  # MUTANT: projection deleted",
        tests=[f"{OWNER_TESTS}::test_runner_projects_owner_id_for_vin_scoped_finding_when_resolvable"],
    ),
    Mutation(
        "M-FG11-2", "4 — integrity mismatch not detected", RUNNER,
        old="        reverify = owner_resolver(vin)\n        if reverify != resolved_standing:",
        new="        reverify = owner_resolver(vin)\n        if False:  # MUTANT: integrity assertion disabled",
        tests=[f"{OWNER_TESTS}::test_projected_owner_id_matches_vehicle_owner_lookup"],
    ),
    Mutation(
        "M-FG11-3a", "3 — empty string projected (writer guard)", WRITER,
        old="        if not candidate or candidate == scope_value:",
        new="        if candidate == scope_value:  # MUTANT: empty-string guard removed",
        tests=[f"{OWNER_TESTS}::test_writer_refuses_to_project_a_non_identity"],
    ),
    Mutation(
        "M-FG11-3b", "3 — resolver returns '' rather than None", RESOLVER,
        old='    candidate = raw.strip()\n    if not candidate:',
        new='    candidate = raw.strip()\n    if False:  # MUTANT: blank value passes through',
        tests=[f"{OWNER_TESTS}::test_resolver_returns_none_for_absent_or_blank_sold_to"],
    ),
    Mutation(
        "M-FG11-4a", "3 — VIN written as owner_id (writer guard)", WRITER,
        old="        if not candidate or candidate == scope_value:",
        new="        if not candidate:  # MUTANT: VIN-equality guard removed",
        tests=[f"{OWNER_TESTS}::test_writer_refuses_to_project_a_non_identity"],
    ),
    Mutation(
        "M-FG11-4b", "3 — VIN passes resolver validation", RESOLVER,
        old="    if candidate == vin:",
        new="    if False:  # MUTANT: resolver VIN-equality guard removed",
        tests=[f"{OWNER_TESTS}::test_resolver_rejects_a_sold_to_equal_to_the_vin"],
    ),
    Mutation(
        "M-FG11-5", "artifact-vs-item boundary breach", RUNNER,
        old='        "severity_hint": agent_fields.get("severity_hint", "routine"),\n        "ttl": ttl,\n    }',
        new='        "severity_hint": agent_fields.get("severity_hint", "routine"),\n        "ttl": ttl,\n        "owner_id": resolved_owner,  # MUTANT: leaked onto the artifact\n    }',
        tests=[f"{OWNER_TESTS}::test_projected_owner_id_lives_on_ddb_item_only"],
    ),
    Mutation(
        "M-FG11-5b", "update-path stale projection left in place", WRITER,
        old='        if to_remove:',
        new='        if False:  # MUTANT: stale projections retained',
        tests=[f"{OWNER_TESTS}::test_update_removes_stale_owner_projection_when_now_unresolvable"],
    ),
    Mutation(
        "M-FG20-W1", "integrity failure drops the whole Finding (pre-fix behaviour)", RUNNER,
        old="""                obs.record_owner_integrity_degraded()
                artifact, projected_standing = _build_artifact(
                    vin, component, vin_row_list, artifact_fields,
                    tokens_used=tokens, cost_usd=cost,
                    owner_resolver=None,
                )""",
        new="""                obs.record_owner_integrity_degraded()
                continue  # MUTANT: pre-fix behaviour, drops the Finding too""",
        tests=[f"{OWNER_TESTS}::test_integrity_failure_keeps_the_finding_and_drops_only_the_projection"],
    ),
    Mutation(
        "M-FG20-W2", "degradation not counted", RUNNER,
        old="                obs.record_owner_integrity_degraded()",
        new="                pass  # MUTANT: counter removed",
        tests=[f"{OWNER_TESTS}::test_integrity_failure_is_counted_in_the_run_summary"],
    ),
]

STANDING_MUTATIONS: list[Mutation] = [
    Mutation(
        "M-FG25-1", "fleet_driver route removed", HANDLER,
        old="""        if (
            # These two emptiness checks are JOINTLY load-bearing""",
        new="""        if False and (  # MUTANT: driver route disabled
            # These two emptiness checks are JOINTLY load-bearing""",
        tests=[f"{HANDLER_TESTS}::TestVehicleStandingSet::test_driver_of_this_vehicle_may_approve"],
    ),
    Mutation(
        "M-FG25-2", "fleet_manager route removed", HANDLER,
        old="        if projected_fleet_id and _claim_is_trusted(_FLEET_IDS_CLAIM_TRUSTED_ENV):",
        new="        if False:  # MUTANT: fleet route disabled",
        tests=[f"{HANDLER_TESTS}::TestVehicleStandingSet::test_operator_of_this_vehicles_fleet_may_approve"],
    ),
    Mutation(
        "M-FG25-3", "driver claim compared against the VIN, not vehicle_id", HANDLER,
        old="            and caller_vehicle_id.strip() == projected_vehicle_id",
        new="            and caller_vehicle_id.strip() in (projected_vehicle_id, vin)  # MUTANT",
        tests=[f"{HANDLER_TESTS}::TestVehicleStandingSet::test_driver_claim_does_not_match_against_the_vin"],
    ),
    # M-FG25-4 WITHDRAWN. It removed the `projected_vehicle_id` presence check,
    # which changes NO behaviour: the equality `claim == projected` cannot match a
    # non-empty claim when `projected is None`, so the presence check is defensive
    # rather than load-bearing. Demonstrated rather than assumed. A mutation that
    # alters nothing is not evidence of a coverage gap, and leaving it in the
    # harness as "NOT CAUGHT" would be a false finding.
    Mutation(
        "M-FG25-5", "substring match on vehicle_id", HANDLER,
        old="            and caller_vehicle_id.strip() == projected_vehicle_id",
        new="            and caller_vehicle_id.strip() in projected_vehicle_id  # MUTANT",
        tests=[f"{HANDLER_TESTS}::TestVehicleStandingSet"],
    ),
    Mutation(
        "M-FG25-6", "owner route falls back to a self-matching value (tautological grant)", HANDLER,
        old='        caller_identity = _caller_customer_id(claims)',
        new='        caller_identity = _caller_customer_id(claims) or projected_owner_id  # MUTANT: tautological',
        tests=[f"{HANDLER_TESTS}::TestVehicleStandingSet::test_driver_standing_does_not_confer_owner_standing"],
    ),
    Mutation(
        "M-FG25-7", "an action_kind left unclassified", HANDLER,
        old='    "surface_recall",\n    "acknowledge_only",\n})',
        new='    "acknowledge_only",\n})  # MUTANT: surface_recall unclassified',
        tests=[f"{HANDLER_TESTS}::test_action_kind_enum_is_fully_classified"],
    ),
    Mutation(
        "M-FG25-8", "writer stops projecting fleet_id and vehicle_id", WRITER,
        old='    for attr in ("owner_id", "fleet_id", "vehicle_id"):',
        new='    for attr in ("owner_id",):  # MUTANT: only owner projected',
        tests=[f"{OWNER_TESTS}::test_standing_projection_covers_all_three_facts"],
    ),
]


HANDLER_MUTATIONS: list[Mutation] = [
    Mutation(
        "M-FG11-8", "5 — revert to nested read", HANDLER,
        old='    projected_owner_id = finding.get("owner_id")',
        new='    projected_owner_id = finding.get("scope", {}).get("owner_id")  # MUTANT',
        tests=[f"{HANDLER_TESTS}::TestApproveOwnerBinding"],
    ),
    Mutation(
        "M-FG11-9", "6 — fail-open reintroduction", HANDLER,
        old="    # Fail closed: no owner binding matched.\n    return False",
        new="    # Fail closed: no owner binding matched.\n    return True  # MUTANT: fail-open",
        tests=[f"{HANDLER_TESTS}::TestApproveOwnerBinding"],
    ),
    Mutation(
        "M-FG11-10", "6 — empty-string sentinel", HANDLER,
        old="        if projected_owner_id and caller_identity and caller_identity == projected_owner_id:",
        new="        if caller_identity == projected_owner_id or (projected_owner_id is None and not caller_identity):  # MUTANT",
        tests=[f"{HANDLER_TESTS}::TestApproveOwnerBinding"],
    ),
    Mutation(
        "M-FG11-11", "substring match instead of equality", HANDLER,
        old="        if projected_owner_id and caller_identity and caller_identity == projected_owner_id:",
        new="        if projected_owner_id and caller_identity and (caller_identity in projected_owner_id or projected_owner_id in caller_identity):  # MUTANT",
        tests=[f"{HANDLER_TESTS}::TestApproveOwnerBinding"],
    ),
    Mutation(
        "M-FG11-12", "owner-scoped backward compat removed", HANDLER,
        old="    if scope_owner_id and caller_sub and caller_sub == scope_owner_id:\n        return True",
        new="    if False:  # MUTANT: owner-scoped fallback removed\n        return True",
        tests=[f"{HANDLER_TESTS}::TestApproveOwnerBinding"],
    ),
    Mutation(
        # Review cycle 1 W2. Replaces the withdrawn M-FG25-4, which mutated ONE of
        # these two checks and could not fail because the other covered it. The pair
        # is jointly load-bearing: remove both and a blank claim matches a blank
        # projection. All 126 handler tests were green against this mutant before
        # `test_blank_claim_never_matches_a_blank_projection` existed.
        "M-FG25-9", "BOTH driver-route emptiness guards removed", HANDLER,
        old="            projected_vehicle_id\n"
            "            and isinstance(caller_vehicle_id, str)\n"
            "            and caller_vehicle_id.strip()\n"
            "            and caller_vehicle_id.strip() == projected_vehicle_id",
        new="            isinstance(caller_vehicle_id, str)  # MUTANT: both emptiness guards gone\n"
            "            and caller_vehicle_id.strip() == (projected_vehicle_id or \"\").strip()",
        tests=[
            f"{HANDLER_TESTS}::TestVehicleStandingSet::test_blank_claim_never_matches_a_blank_projection",
        ],
    ),
    Mutation(
        # Review cycle 1 C3 / cycle 2 C1. Pins the deletion: restoring the
        # self-writable claim arm must fail. Without this the arm could be
        # reintroduced by anyone who reads the group check as "too narrow".
        "M-FG25-10", "custom:role claim arm restored (unconditional approve bypass)", HANDLER,
        old='    if groups & {"service-advisor"}:\n        return True',
        new='    if groups & {"service-advisor"} or claims.get("custom:role") == "service-advisor":  # MUTANT\n        return True',
        tests=[f"{HANDLER_TESTS}::TestServiceAdvisorClaimArmIsGone"],
    ),
    # ── Review cycle 2 W4: the reviewer ran these five by hand and found them all
    # caught. Registering them, because a mutation verified once in a review session
    # and never recorded protects nothing afterwards — the harness is the only thing
    # that re-runs. F26.3's own Verify field demanded the noop mutation explicitly.
    Mutation(
        "M-FG26-1", "vehicle-id trust gate removed (C2 regression)", HANDLER,
        old="            claims.get(\"custom:vehicleId\")\n"
            "            if _claim_is_trusted(_VEHICLE_ID_CLAIM_TRUSTED_ENV)\n"
            "            else None",
        new="            claims.get(\"custom:vehicleId\")  # MUTANT: gate removed",
        tests=[f"{HANDLER_TESTS}::TestStandingClaimTrustGates"],
    ),
    Mutation(
        "M-FG26-2", "fleet-ids trust gate removed on the VIN branch (C2 regression)", HANDLER,
        old="        if projected_fleet_id and _claim_is_trusted(_FLEET_IDS_CLAIM_TRUSTED_ENV):",
        new="        if projected_fleet_id:  # MUTANT: gate removed",
        tests=[f"{HANDLER_TESTS}::TestStandingClaimTrustGates"],
    ),
    Mutation(
        # Re-anchored to `_lib` by Fix Group 29, which moved the single definition there
        # so this module and the scope helpers cannot disagree. The anchor-freshness
        # guard caught the move, which is what it is for.
        "M-FG26-3", "trust flag accepts truthiness instead of exact 'true'", SCOPE,
        old='    return os.environ.get(env_var, "").strip().lower() == "true"',
        new='    return bool(os.environ.get(env_var, "").strip())  # MUTANT: truthiness',
        tests=[
            f"{HANDLER_TESTS}::TestStandingClaimTrustGates::test_only_exact_true_opts_in",
            f"{SCOPE_TESTS}::TestTrustFlagParsing::test_only_exact_true_opts_in",
        ],
    ),
    Mutation(
        "M-FG26-4", "CSV fleet claim reverted to JSON-only parsing (W3 regression)", HANDLER,
        old="        out.extend(part.strip() for part in str(entry).split(\",\") if part.strip())",
        new="        out.append(str(entry))  # MUTANT: no CSV split",
        tests=[f"{HANDLER_TESTS}::TestFleetClaimEncoding"],
    ),
    Mutation(
        "M-FG26-5", "W4 decision flipped to group-keyed on the VIN branch", HANDLER,
        old="        if projected_fleet_id and _claim_is_trusted(_FLEET_IDS_CLAIM_TRUSTED_ENV):",
        new='        if projected_fleet_id and "fleet-operator" in groups and _claim_is_trusted(_FLEET_IDS_CLAIM_TRUSTED_ENV):  # MUTANT: group-keyed',
        tests=[f"{HANDLER_TESTS}::TestVehicleStandingSet::test_vin_branch_standing_is_claim_keyed_not_group_keyed"],
    ),
    Mutation(
        "M-FG26-6", "FLEET-scoped arm's trust gate removed (cycle 2 W1 regression)", HANDLER,
        old='    if "fleet-operator" in groups and fleet_id and _claim_is_trusted(_FLEET_IDS_CLAIM_TRUSTED_ENV):',
        new='    if "fleet-operator" in groups and fleet_id:  # MUTANT: pre-existing arm left ungated',
        tests=[f"{HANDLER_TESTS}::TestApproveRoute::test_approve_w1_fleet_scoped_arm_denied_without_the_trust_flag"],
    ),
    Mutation(
        "M-FG26-7", "custom:role read arm restored in check_vehicle_scope (cycle 2 C1)", SCOPE,
        old="    # Service roles — can view any vehicle\n"
            "    if groups & {\"service-advisor\", \"technician\"}:\n"
            "        return True",
        new="    # Service roles — can view any vehicle\n"
            "    if groups & {\"service-advisor\", \"technician\"}:\n"
            "        return True\n"
            "    if claims.get(\"custom:role\", \"\") in (\"service-advisor\", \"technician\"):  # MUTANT\n"
            "        return True",
        tests=[f"{SCOPE_TESTS}::TestCheckVehicleScope"],
    ),
    Mutation(
        "M-FG26-8", "custom:role fleet-filter escape restored (cycle 2 C1, cross-fleet leak)", SCOPE,
        old='    if groups & {"platform-admin", "service-advisor", "technician"}:\n        return False, []',
        new='    if groups & {"platform-admin", "service-advisor", "technician"}:\n'
            '        return False, []\n'
            '    if claims.get("custom:role", "") in ("service-advisor", "technician"):  # MUTANT\n'
            '        return False, []',
        tests=[f"{SCOPE_TESTS}::TestIsFleetVehicleCaller"],
    ),
    # ── Review cycle 3 W1/W2: the reviewer's RM-B and RM-C4, registered rather than
    # left as review-session findings. RM-B's detection was previously an ACCIDENT of
    # M-FG26-8's anchor quoting the same line, and the diagnostic it produced
    # ("anchor occurs 0x") invites re-anchoring — a repair that returns the whole
    # system to green with two privileged roles silently stripped.
    Mutation(
        "M-FG28-1", "privileged group set narrowed, stripping service-advisor + technician", SCOPE,
        old='    if groups & {"platform-admin", "service-advisor", "technician"}:',
        new='    if groups & {"platform-admin"}:  # MUTANT: privileged set narrowed',
        tests=[f"{SCOPE_TESTS}::TestIsFleetVehicleCaller::test_real_service_role_group_still_bypasses_fleet_filtering"],
    ),
    Mutation(
        # Mutates the collector, which lives in the test module because it IS the
        # guard's implementation. The parametrized cases are the assertions about it.
        "M-FG28-2", "AST collector's ast.Attribute branch removed", HANDLER_TESTS,
        old="        elif isinstance(node, ast.Attribute):\n            referenced.add(node.attr)",
        new="        # MUTANT: ast.Attribute branch removed",
        tests=[f"{HANDLER_TESTS}::test_forbidden_read_collector_sees_every_import_form"],
    ),
    # ── Fix Group 29: one mutation per gated membership-claim read. Every other test
    # in these packages runs with the trust flags SET (per the new conftests), so a
    # green suite proves nothing about the gates on its own — these are what make the
    # gates load-bearing. M-FG29-8 is the important one: it is the single site where
    # the obvious gate (`[]`) fails OPEN.
    Mutation(
        "M-FG29-1", "trust gate removed from _trusted_claim_list (all 7 _lib sites at once)", SCOPE,
        old="    if not claim_is_trusted(env_var):\n        return []",
        new="    if False:  # MUTANT: gate removed\n        return []",
        tests=[
            f"{SCOPE_TESTS}::TestMembershipClaimTrustGates",
            # Review cycle 4 W2: the actions handler had no end-to-end untrusted
            # coverage, and the 8th gated site was found IN that handler reading the
            # claim directly. A predicate test over the shared helper structurally
            # cannot see a handler that bypasses the helper.
            f"{ACTIONS_TESTS}::TestMembershipClaimGatesEndToEndActions",
        ],
    ),
    Mutation(
        "M-FG29-2", "check_vehicle_scope fleet-driver read un-gated", SCOPE,
        old='        fleet_ids = _trusted_claim_list(claims, "custom:fleetIds", FLEET_IDS_CLAIM_TRUSTED_ENV)\n'
            '        if fleet_ids:\n'
            '            return True\n'
            '        return False  # fleet-driver with no TRUSTED fleet membership → deny',
        new='        fleet_ids = _extract_claim_list(claims, "custom:fleetIds")  # MUTANT\n'
            '        if fleet_ids:\n'
            '            return True\n'
            '        return False',
        tests=[f"{SCOPE_TESTS}::TestMembershipClaimTrustGates::test_fleet_driver_vehicle_scope_denied"],
    ),
    Mutation(
        "M-FG29-3", "check_fleet_scope read un-gated", SCOPE,
        old='    fleet_ids = _trusted_claim_list(claims, "custom:fleetIds", FLEET_IDS_CLAIM_TRUSTED_ENV)\n'
            '    return fleet_id in fleet_ids',
        new='    fleet_ids = _extract_claim_list(claims, "custom:fleetIds")  # MUTANT\n'
            '    return fleet_id in fleet_ids',
        tests=[f"{SCOPE_TESTS}::TestMembershipClaimTrustGates::test_fleet_scope_denied"],
    ),
    Mutation(
        "M-FG29-4", "check_dealer_scope read un-gated", SCOPE,
        old='    dealer_ids = _trusted_claim_list(claims, "custom:dealerIds", DEALER_IDS_CLAIM_TRUSTED_ENV)',
        new='    dealer_ids = _extract_claim_list(claims, "custom:dealerIds")  # MUTANT',
        tests=[f"{SCOPE_TESTS}::TestMembershipClaimTrustGates::test_dealer_scope_denied"],
    ),
    Mutation(
        "M-FG29-5", "check_district_scope read un-gated", SCOPE,
        old='    district_ids = _trusted_claim_list(claims, "custom:districtIds", DISTRICT_IDS_CLAIM_TRUSTED_ENV)',
        new='    district_ids = _extract_claim_list(claims, "custom:districtIds")  # MUTANT',
        tests=[f"{SCOPE_TESTS}::TestMembershipClaimTrustGates::test_district_scope_denied"],
    ),
    Mutation(
        "M-FG29-6", "check_owner_fleet_scope read un-gated", SCOPE,
        old='        fleet_ids = _trusted_claim_list(claims, "custom:fleetIds", FLEET_IDS_CLAIM_TRUSTED_ENV)\n'
            '        if fleet_ids and owner_fleet_id and owner_fleet_id in fleet_ids:',
        new='        fleet_ids = _extract_claim_list(claims, "custom:fleetIds")  # MUTANT\n'
            '        if fleet_ids and owner_fleet_id and owner_fleet_id in fleet_ids:',
        tests=[f"{SCOPE_TESTS}::TestMembershipClaimTrustGates::test_owner_fleet_scope_fleet_grant_denied_but_owner_self_read_survives"],
    ),
    Mutation(
        "M-FG29-7", "actions PATCH fleet check un-gated (the 8th site, found by grep)", ACTIONS,
        old='            caller_fleet_ids = _trusted_claim_list(\n'
            '                claims, "custom:fleetIds", FLEET_IDS_CLAIM_TRUSTED_ENV\n'
            '            )',
        new='            from _lib.avx_scope import _extract_claim_list  # MUTANT\n'
            '            caller_fleet_ids = _extract_claim_list(claims, "custom:fleetIds")',
        tests=[f"{ACTIONS_TESTS}::TestPatchActionStateMachine::test_fg29_untrusted_fleet_claim_cannot_patch_even_its_own_fleet"],
    ),
    Mutation(
        # THE one that matters most. `is_fleet_vehicle_caller` returns the set a
        # restrictive FilterExpression is built from, so `(False, [])` is the
        # PRIVILEGED answer — it tells the route the caller is not fleet-scoped and the
        # route returns UNFILTERED results. Gating this site the way the other seven
        # are gated fails OPEN.
        "M-FG29-8", "untrusted fleet claim returns the PRIVILEGED (False, []) — fails OPEN", SCOPE,
        old="        # Fleet role with no TRUSTED fleetIds: still fleet-scoped, zero fleets → 403.\n"
            "        return True, []",
        new="        return False, []  # MUTANT: privileged answer → route goes unfiltered",
        tests=[f"{SCOPE_TESTS}::TestMembershipClaimTrustGates::test_untrusted_fleet_claim_stays_fleet_scoped_not_privileged"],
    ),
    Mutation(
        # Security review W1, the actual shipped defect: FG25 added `vehicle_id` to the
        # writer's projection and not to the response-boundary strip list, so every
        # VIN-scoped Finding with resolved standing returned a root `vehicle_id` and
        # failed the frozen contract's root `additionalProperties: false`. Invisible to
        # CI because no fixture populated all three standing attrs at once — the one
        # attribute the tests did not exercise was the one that leaked.
        "M-FG30-1", "vehicle_id removed from the response-boundary strip list", HANDLER,
        old='    "vehicle_id",\n})',
        new='})  # MUTANT: vehicle_id no longer stripped from responses',
        tests=[
            f"{HANDLER_TESTS}::test_strip_list_covers_every_writer_projected_attr",
            f"{HANDLER_TESTS}::test_vin_scoped_response_with_all_three_standing_attrs_is_schema_valid",
        ],
    ),
    Mutation(
        # The forward-looking half: a NEW attribute added to the writer's projection with
        # no strip-list update is the same defect again. The guard parses the writer's
        # source precisely so this direction fails too, not only the one that shipped.
        "M-FG30-2", "a new writer-projected attr with no strip-list update", WRITER,
        old='_STANDING_ATTRS: tuple[str, ...] = ("owner_id", "fleet_id", "vehicle_id")',
        new='_STANDING_ATTRS: tuple[str, ...] = ("owner_id", "fleet_id", "vehicle_id", "dealer_group_id")  # MUTANT',
        tests=[f"{HANDLER_TESTS}::test_strip_list_covers_every_writer_projected_attr"],
    ),
    Mutation(
        # Review cycle 4 W1: the guard's ORIGINAL regex could not read a non-snake_case
        # attribute, and its `len >= 3` non-vacuity floor could not detect that, because
        # the set is exactly 3. So this mutation — identical in shape to M-FG30-2 but
        # camelCase — left the guard GREEN. The guard now parses with `ast`, which
        # removes the class rather than widening a character set.
        "M-FG31-1", "a camelCase writer-projected attr (the shape the regex could not see)", WRITER,
        old='_STANDING_ATTRS: tuple[str, ...] = ("owner_id", "fleet_id", "vehicle_id")',
        new='_STANDING_ATTRS: tuple[str, ...] = ("owner_id", "fleet_id", "vehicle_id", "dealerGroupId")  # MUTANT',
        tests=[f"{HANDLER_TESTS}::test_strip_list_covers_every_writer_projected_attr"],
    ),
    Mutation(
        # Review cycle 4 Suggestion: a blank or separator-only claim value was truthy, so
        # `if fleet_ids: return True` granted on it. Runs against the TRUSTED state, since
        # the gate alone does not catch this.
        "M-FG31-2", "blank/separator membership values no longer filtered", SCOPE,
        old="    return [\n"
            "        v for v in _extract_claim_list(claims, claim_name)\n"
            "        if any(ch.isalnum() for ch in str(v))\n"
            "    ]",
        new="    return _extract_claim_list(claims, claim_name)  # MUTANT: blanks pass through",
        tests=[f"{SCOPE_TESTS}::TestBlankMembershipValues"],
    ),
]


def run_tests(targets: list[str]) -> tuple[str, str]:
    """Return (outcome, tail-of-output) where outcome is pass | fail | error.

    ``error`` is distinct from ``fail`` on purpose. A selector naming a test that
    no longer exists makes pytest exit non-zero, and a two-state pass/fail reading
    scores that as "the mutation was caught" — a vacuous catch that reports
    protection the suite does not have.

    Found live 2026-09-23: moving a class definition silently re-parented six
    methods of ``TestVehicleStandingSet``, so ``M-FG25-6``'s selector matched
    nothing and was reported caught while the whole suite stayed green. This is the
    same stale-reference failure the anchor-freshness guard covers for ``old=``
    anchors, one level over, in the ``tests=`` field.
    """
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", *targets, "-q", "--no-header", "-x"],
        cwd=ROOT, capture_output=True, text=True,
    )
    raw = proc.stdout + proc.stderr
    out = raw.strip().splitlines()
    tail = "\n".join(out[-3:])
    # pytest exits 4 on usage errors (unknown selector) and 5 when nothing was
    # collected. Match on both the code and the message, since -x plus a bad
    # selector can surface either.
    if proc.returncode in (4, 5) or "ERROR: not found:" in raw or "no tests ran" in raw:
        return "error", tail
    return ("pass" if proc.returncode == 0 else "fail"), tail


def apply_mutation(m: Mutation) -> bool:
    p = ROOT / m.path
    s = p.read_text()
    if m.old not in s:
        print(f"  !! anchor NOT FOUND in {m.path} — mutation cannot be applied")
        return False
    if s.count(m.old) != 1:
        print(f"  !! anchor is ambiguous ({s.count(m.old)}x) in {m.path}")
        return False
    p.write_text(s.replace(m.old, m.new, 1))
    return True


def main(which: str) -> int:
    # Review cycle 2 Suggestion: reject an unrecognised arg instead of silently
    # running an empty pool. `fg15-mutations.py f11.4` (a typo) used to report
    # `caught: 0 / NOT caught: 0`, post-restore green, exit 0 — a clean bill of
    # health from a run that mutated nothing. It fails safe today only because the
    # repo-wide suite is broken; once the test_handler.py module collision is fixed
    # the pre-flight would pass and the vacuous success would be indistinguishable
    # from a real one.
    valid = {"f11.2", "f11.3", "standing", "all"}
    if which not in valid:
        print(f"unknown selection {which!r} — expected one of {sorted(valid)}")
        return 2

    pool = MUTATIONS if which in ("f11.2", "all") else []
    if which in ("f11.3", "all"):
        pool = pool + HANDLER_MUTATIONS
    if which in ("standing", "all"):
        pool = pool + STANDING_MUTATIONS

    # Belt-and-braces: a valid selection whose pool is empty is also a defect.
    if not pool:
        print(f"selection {which!r} produced an EMPTY pool — nothing would be tested")
        return 2

    # Pre-flight: the suite must be green before we start, or "caught" is meaningless.
    all_targets = sorted({t.split("::")[0] for m in pool for t in m.tests})
    outcome, tail = run_tests(all_targets)
    if outcome != "pass":
        print(f"PRE-FLIGHT {outcome.upper()} — suite must be green before mutating:")
        print(tail)
        return 2
    print(f"pre-flight green across {len(all_targets)} file(s)\n")

    caught, missed, unapplied, stale = [], [], [], []
    tmpdir = Path(tempfile.mkdtemp(prefix="fg15-mut-"))

    for m in pool:
        src = ROOT / m.path
        backup = tmpdir / f"{m.mid}-{src.name}"
        shutil.copy2(src, backup)
        print(f"{m.mid}  (property {m.prop})")
        try:
            if not apply_mutation(m):
                unapplied.append(m.mid)
                continue
            outcome, tail = run_tests(m.tests)
            if outcome == "pass":
                print(f"  NOT CAUGHT — {m.tests} stayed green")
                missed.append(m.mid)
            elif outcome == "error":
                print(f"  !! STALE SELECTOR — {m.tests} collected nothing; NOT a catch")
                print(f"     {tail.splitlines()[-1] if tail else ''}")
                stale.append(m.mid)
            else:
                print(f"  caught: {tail.splitlines()[-1] if tail else 'failed'}")
                caught.append(m.mid)
        finally:
            shutil.copy2(backup, src)
            assert src.read_bytes() == backup.read_bytes(), f"restore failed for {m.path}"

    print("\n" + "=" * 62)
    print(f"caught        : {len(caught)}  {caught}")
    print(f"NOT caught    : {len(missed)}  {missed}")
    print(f"unapplied     : {len(unapplied)}  {unapplied}")
    print(f"stale selector: {len(stale)}  {stale}")

    # Post-flight: everything restored, suite green again.
    outcome, tail = run_tests(all_targets)
    ok = outcome == "pass"
    print(f"post-restore suite green: {ok}")
    if not ok:
        print(tail)
    return 0 if (not missed and not unapplied and not stale and ok) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "all"))
