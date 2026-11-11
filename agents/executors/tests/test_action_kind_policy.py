"""Red tests: the action-kind policy table — spec D4, contract § 4.

Expected interface (Group 2 creates agents/executors/policy.py):

    ACTION_KINDS: dict[str, PolicyRow]

    PolicyRow (dataclass or dict) fields:
        target_system: str
        executable: bool         # has a working executor in this deployment
        operator_required: bool  # only decided_by.kind == "operator" may decide it
        auto_approvable: bool    # an auto-approval rule may decide it
        rule_limit_param: str | None

    Import-time check in both directions: a kind in the schema enum missing from
    ACTION_KINDS, or a key in ACTION_KINDS missing from the enum, raises.

Kind names are pinned literally here, never imported from policy.py (spec D5).
The module is xfail(strict=True) until Group 2 lands policy.py; strict means a
forgotten mark fails CI once the code passes.
"""
from __future__ import annotations

import json
import pathlib

import pytest

pytestmark = pytest.mark.usefixtures()  # noqa: placeholder — xfail removed by Group 2

_SCHEMA = (
    pathlib.Path(__file__).resolve().parents[3]
    / "agents" / "tier2" / "schemas" / "action.schema.json"
)

#: The six Spec 1a kinds. They keep today's behaviour exactly (review Cycle 1 C2).
_SPEC_1A_KINDS = frozenset({
    "book_combined_visit",
    "schedule_inspection",
    "update_charging_schedule",
    "file_warranty_pre",
    "surface_recall",
    "acknowledge_only",
})

#: The two retail kinds: executor exists; the approve route's _RETAIL_KINDS runtime
#: gate still applies on top of the table, unchanged by Stage A.
_RETAIL_KINDS = frozenset({"lead_capture", "configurator_recommend"})

#: The seven FI kinds Stage A adds (spec D3).
_FI_KINDS = frozenset({
    "book_recall_service",
    "ground_vehicle",
    "move_vehicles",
    "exempt_recall",
    "complete_recall",
    "submit_warranty_claim",
    "apply_cost_action",
    # Stage D operator kinds (task 3.6)
    "book_fleet_service",
})

#: Engineering kinds (Stage D, task 3.6): require product-engineer, not operator.
_ENGINEERING_KINDS = frozenset({
    "record_engineering_decision",
    "issue_recall",
    "issue_software_campaign",
})

#: All 15 v2.5 kinds (unchanged for backward-compat reference).
_V25_KINDS = _SPEC_1A_KINDS | _RETAIL_KINDS | frozenset({
    "book_recall_service",
    "ground_vehicle",
    "move_vehicles",
    "exempt_recall",
    "complete_recall",
    "submit_warranty_claim",
    "apply_cost_action",
})

#: All kinds: v2.5 + Stage D (v2.7).
_ALL_KINDS = _V25_KINDS | frozenset({"book_fleet_service"}) | _ENGINEERING_KINDS

#: The four guardrailed kinds (G1–G4).
_GUARDED_KINDS = frozenset({
    "exempt_recall",
    "ground_vehicle",
    "submit_warranty_claim",
    "complete_recall",
})

#: Executable in Stage A: every pre-existing kind (unchanged) plus book_recall_service.
_EXECUTABLE_IN_STAGE_A = _SPEC_1A_KINDS | _RETAIL_KINDS | {"book_recall_service"}

#: Executable once cost Stage B Group 3 lands (spec 2026-10-02-fi-cost-stage-b D9, task 3.3).
_EXECUTABLE_FROM_STAGE_B = frozenset({"apply_cost_action"})

#: Executable once Stage C lands (spec D11, task 3.2): grounding + warranty claim.
_EXECUTABLE_FROM_STAGE_C = frozenset({"ground_vehicle", "submit_warranty_claim"})

#: Executable from Stage D (task 3.6): fleet-service booking + engineering kinds.
_EXECUTABLE_FROM_STAGE_D = frozenset({
    "book_fleet_service",
    "record_engineering_decision",
    "issue_recall",
    "issue_software_campaign",
})

#: Executable from Stage E (task 4.2): move_vehicles (FI rebalancing).
_EXECUTABLE_FROM_STAGE_E = frozenset({"move_vehicles"})

#: Only acknowledge_only may be auto-approved in Stage A (spec D9: token-free only).
_AUTO_APPROVABLE_IN_STAGE_A = frozenset({"acknowledge_only"})

#: Stage E Group 8 (U6, approved 2026-10-06): move_vehicles becomes the first limited
#: auto-approvable kind (a rule within its limit on transfer_cost_usd and the bounds).
#: Stage D rebooking (G8-AVX-1, approved 2026-11-02): book_fleet_service added as the
#: second auto-approvable kind (a rule gated on the rebook predicate + flag).
_AUTO_APPROVABLE_FROM_STAGE_E = frozenset({"move_vehicles", "book_fleet_service"})
_LIMITED_KINDS = {"move_vehicles": "transfer_cost_usd"}

#: Targets the contract fixes (§ 4 table). apply_cost_action's target is set by the
#: cost stage and is deliberately not pinned here.
_EXPECTED_TARGETS = {
    "book_recall_service": "dms",
    "submit_warranty_claim": "dms",
    "ground_vehicle": "cms",
    "move_vehicles": "cms",
    "exempt_recall": "cms",
    "complete_recall": "cms",
    "lead_capture": "retail",
    "configurator_recommend": "retail",
    # Stage D kinds (task 3.6)
    "book_fleet_service": "dms",
    "record_engineering_decision": "adp",
    "issue_recall": "adp",
    "issue_software_campaign": "adp",
}


def _policy():
    from agents.executors import policy  # noqa: PLC0415
    return policy


def _field(row, name):
    return getattr(row, name) if hasattr(row, name) else row[name]


def test_literal_kind_list_matches_the_schema():
    """The pinned list is the schema enum. Not xfail-dependent, but kept in this
    module so the two cannot drift; passes once, then strict xfail makes the
    module's own mark the thing Group 2 must remove."""
    enum = set(json.loads(_SCHEMA.read_text())["properties"]["action_kind"]["enum"])
    assert enum == _ALL_KINDS
    _policy()  # keeps this test red until policy.py exists


class TestPolicyCoverage:
    def test_every_v25_kind_has_a_row(self):
        assert set(_policy().ACTION_KINDS) >= _V25_KINDS

    def test_no_unknown_kinds_in_table(self):
        assert set(_policy().ACTION_KINDS) - _ALL_KINDS == set()

    @pytest.mark.parametrize("kind", sorted(_ALL_KINDS))
    def test_kind_has_required_fields(self, kind):
        row = _policy().ACTION_KINDS[kind]
        for name in ("target_system", "executable", "operator_required", "auto_approvable"):
            _field(row, name)


class TestTargets:
    @pytest.mark.parametrize("kind,target", sorted(_EXPECTED_TARGETS.items()))
    def test_target_matches_contract(self, kind, target):
        assert _field(_policy().ACTION_KINDS[kind], "target_system") == target


class TestExecutable:
    @pytest.mark.parametrize("kind", sorted(_EXECUTABLE_IN_STAGE_A | _EXECUTABLE_FROM_STAGE_B | _EXECUTABLE_FROM_STAGE_C | _EXECUTABLE_FROM_STAGE_D | _EXECUTABLE_FROM_STAGE_E))
    def test_executable_in_stage_a(self, kind):
        assert _field(_policy().ACTION_KINDS[kind], "executable") is True

    @pytest.mark.parametrize("kind", sorted(_ALL_KINDS - _EXECUTABLE_IN_STAGE_A - _EXECUTABLE_FROM_STAGE_B - _EXECUTABLE_FROM_STAGE_C - _EXECUTABLE_FROM_STAGE_D - _EXECUTABLE_FROM_STAGE_E))
    def test_not_executable_until_its_stage(self, kind):
        assert _field(_policy().ACTION_KINDS[kind], "executable") is False


class TestOperatorRequired:
    @pytest.mark.parametrize("kind", sorted(_FI_KINDS))
    def test_every_fi_kind_requires_an_operator(self, kind):
        """Includes complete_recall: G4 is a service-record check, but only fleet
        operators decide FI items at all (spec D4/D5).
        book_fleet_service is Stage D operator-only (contract § 4 table)."""
        assert _field(_policy().ACTION_KINDS[kind], "operator_required") is True

    @pytest.mark.parametrize("kind", sorted(_SPEC_1A_KINDS | _RETAIL_KINDS))
    def test_pre_existing_kinds_keep_their_approvers(self, kind):
        assert _field(_policy().ACTION_KINDS[kind], "operator_required") is False

    @pytest.mark.parametrize("kind", sorted(_ENGINEERING_KINDS))
    def test_engineering_kinds_are_not_operator_required(self, kind):
        """Engineering kinds require product-engineer (G5), not an operator.
        operator_required=False because decided_by.kind must be 'engineer' (spec D14)."""
        assert _field(_policy().ACTION_KINDS[kind], "operator_required") is False


class TestAutoApprovable:
    @pytest.mark.parametrize("kind", sorted(_GUARDED_KINDS))
    def test_guarded_kinds_are_never_auto_approvable(self, kind):
        assert _field(_policy().ACTION_KINDS[kind], "auto_approvable") is False

    @pytest.mark.parametrize("kind", sorted(_AUTO_APPROVABLE_IN_STAGE_A | _AUTO_APPROVABLE_FROM_STAGE_E))
    def test_stage_a_auto_approvable(self, kind):
        assert _field(_policy().ACTION_KINDS[kind], "auto_approvable") is True

    @pytest.mark.parametrize("kind", sorted(_ALL_KINDS - _AUTO_APPROVABLE_IN_STAGE_A - _AUTO_APPROVABLE_FROM_STAGE_E))
    def test_everything_else_is_not_auto_approvable(self, kind):
        assert _field(_policy().ACTION_KINDS[kind], "auto_approvable") is False

    @pytest.mark.parametrize("kind", sorted(_ALL_KINDS))
    def test_only_the_limited_kinds_name_a_rule_limit(self, kind):
        """A limited kind names the parameter its rule limits; no other kind names one."""
        assert _field(_policy().ACTION_KINDS[kind], "rule_limit_param") == _LIMITED_KINDS.get(kind)

    @pytest.mark.parametrize("kind", sorted(_AUTO_APPROVABLE_FROM_STAGE_E))
    def test_a_limited_kind_still_needs_an_operator_when_a_person_decides(self, kind):
        assert _field(_policy().ACTION_KINDS[kind], "operator_required") is True
