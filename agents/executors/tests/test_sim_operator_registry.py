"""11.1a RED tests — registry.dispatch() with the simulated fleet operator (D10 Decision 3).
11.1b RED tests — top-of-_check_guardrails guard over every ACTION_KINDS value (spec D10).

G2 (ground_vehicle) and G3 (submit_warranty_claim) admit the simulated decider only when:
  1. FI_SIM_OPERATOR_ENABLED == "true" in the running process.
  2. action.fleet_id is in DEMO_FLEET_IDS (the literal frozenset, never config).

book_fleet_service and book_recall_service with the simulated decider: same two conditions
(a new central re-check); else declined.

Every other kind with the simulated decider (G1, G4, G5, apply_cost_action, move_vehicles,
every other spec 1a kind) is declined.

A decider with the right kind and a wrong id, a wrong-case kind, or extra keys is not the
stand-in; those take the normal declined path.

Operator, rule, user and engineer behaviour is unchanged (existing passing tests are the
regression guard; the tests here cover only the new simulated-decider cases).

11.1b extends this: a top-of-_check_guardrails guard declines any decided_by whose
kind == DECIDER_KIND unless ALL four conditions hold (exactly the stand-in identity,
kind in SIMULATED_KINDS, process flag set, fleet in DEMO_FLEET_IDS). This catches
spec-1a and retail kinds that previously fell through to the executor unchanged.
"""
from __future__ import annotations

import os

import pytest

from agents.executors import registry
from agents.executors.base import ActionStatus, ExecuteResult, FailureKind
from agents.executors.policy import ACTION_KINDS as _ACTION_KINDS
from agents.executors.simulated_operator import (
    DECIDER_ID,
    DECIDER_KIND,
    DEMO_FLEET_IDS,
    ENABLED_ENV,
    SIMULATED_KINDS as _SIMULATED_KINDS,
)

_DEMO_FLEET = next(iter(sorted(DEMO_FLEET_IDS)))  # any one demo fleet
_NON_DEMO_FLEET = "live-fleet-production-01"
_OPERATOR_SUB = "a1b2c3d4-0000-4000-8000-000000000001"
_RULE_ID = f"{_DEMO_FLEET}/fi-rule-001"

#: Targets by kind (subset needed for these tests).
_TARGET = {
    "exempt_recall":         "cms",
    "ground_vehicle":        "cms",
    "submit_warranty_claim": "dms",
    "complete_recall":       "cms",
    "book_fleet_service":    "dms",
    "book_recall_service":   "dms",
    "apply_cost_action":     "cms",
    "move_vehicles":         "cms",
    "record_engineering_decision": "adp",
    "issue_recall":          "adp",
    "issue_software_campaign": "adp",
}


class _SpyExecutor:
    def __init__(self, ts: str) -> None:
        self.target_system = ts
        self.calls: list = []

    def dispatch(self, action, claims):
        self.calls.append(action)
        return ExecuteResult(status=ActionStatus.COMPLETED, target_ref="spy")


@pytest.fixture
def spies(monkeypatch):
    out: dict[str, _SpyExecutor] = {}
    for ts in ("cms", "dms", "adp"):
        spy = _SpyExecutor(ts)
        monkeypatch.setitem(registry.EXECUTORS, ts, spy)
        out[ts] = spy
    return out


def _sim_decider() -> dict:
    return {"kind": DECIDER_KIND, "id": DECIDER_ID}


def _action(kind: str, fleet_id: str = _DEMO_FLEET, extra_params: dict | None = None) -> dict:
    params: dict = {"vehicle_vin": "MRDVEH00000000015", **(extra_params or {})}
    # fleet_id lives at the top level (not inside parameters) on FI actions.
    return {
        "action_id": f"AVX-ACT-{abs(hash(kind)):08x}-0000-4000-8000-000000000001",
        "finding_id": "FIND-00000000-0000-4000-8000-000000000001",
        "action_kind": kind,
        "target_system": _TARGET[kind],
        "fleet_id": fleet_id,
        "parameters": params,
        "decided_by": _sim_decider(),
    }


def _with_decider(action: dict, decided_by) -> dict:
    a = dict(action)
    if decided_by is None:
        a.pop("decided_by", None)
    else:
        a["decided_by"] = decided_by
    return a


def _declined(result, spy, *, expect_spy_empty: bool = True) -> None:
    assert result.status == ActionStatus.DECLINED
    assert result.failure_kind == FailureKind.TARGET_REFUSED
    assert result.escalate is False
    if expect_spy_empty and spy is not None:
        assert spy.calls == []


def _passes(result, spy) -> None:
    assert result.status != ActionStatus.DECLINED, (
        f"Expected the action to reach the executor but got DECLINED "
        f"(failure_kind={result.failure_kind!r}, failure_reason={result.failure_reason!r})"
    )
    assert len(spy.calls) == 1, f"Spy not called — result={result!r}"


# ---------------------------------------------------------------------------
# G2 ground_vehicle — simulated decider
# ---------------------------------------------------------------------------

class TestG2SimulatedDecider:
    """Simulated decider passes G2 only with the flag and a demo fleet."""

    def test_passes_when_enabled_and_demo_fleet(self, spies, monkeypatch):
        """RED: registry does not yet admit the simulated decider for G2."""
        monkeypatch.setenv(ENABLED_ENV, "true")
        result = registry.dispatch(_action("ground_vehicle", _DEMO_FLEET), {},
                                   fire_escalation=False)
        _passes(result, spies["cms"])

    def test_blocked_when_flag_not_set(self, spies, monkeypatch):
        """M1: dropping the process-flag check would make this pass."""
        monkeypatch.delenv(ENABLED_ENV, raising=False)
        result = registry.dispatch(_action("ground_vehicle", _DEMO_FLEET), {},
                                   fire_escalation=False)
        _declined(result, spies["cms"])

    def test_blocked_when_flag_is_false(self, spies, monkeypatch):
        """M1: flag must be exactly 'true', not a truthy string."""
        monkeypatch.setenv(ENABLED_ENV, "false")
        result = registry.dispatch(_action("ground_vehicle", _DEMO_FLEET), {},
                                   fire_escalation=False)
        _declined(result, spies["cms"])

    def test_blocked_when_fleet_not_in_demo_set(self, spies, monkeypatch):
        """M2: dropping the demo-fleet check would make this pass."""
        monkeypatch.setenv(ENABLED_ENV, "true")
        result = registry.dispatch(_action("ground_vehicle", _NON_DEMO_FLEET), {},
                                   fire_escalation=False)
        _declined(result, spies["cms"])

    def test_blocked_when_fleet_id_missing(self, spies, monkeypatch):
        """fleet_id absent from the action is not in DEMO_FLEET_IDS."""
        monkeypatch.setenv(ENABLED_ENV, "true")
        action = _action("ground_vehicle", _DEMO_FLEET)
        action.pop("fleet_id")
        result = registry.dispatch(action, {}, fire_escalation=False)
        _declined(result, spies["cms"])

    @pytest.mark.parametrize("fleet_id", sorted(DEMO_FLEET_IDS))
    def test_all_four_demo_fleets_pass(self, fleet_id, spies, monkeypatch):
        """Each of the four demo fleets passes when the flag is set."""
        monkeypatch.setenv(ENABLED_ENV, "true")
        result = registry.dispatch(_action("ground_vehicle", fleet_id), {},
                                   fire_escalation=False)
        _passes(result, spies["cms"])


# ---------------------------------------------------------------------------
# G3 submit_warranty_claim — simulated decider
# ---------------------------------------------------------------------------

class TestG3SimulatedDecider:
    """Simulated decider passes G3 only with the flag and a demo fleet."""

    def test_passes_when_enabled_and_demo_fleet(self, spies, monkeypatch):
        """RED: registry does not yet admit the simulated decider for G3."""
        monkeypatch.setenv(ENABLED_ENV, "true")
        result = registry.dispatch(_action("submit_warranty_claim"), {},
                                   fire_escalation=False)
        _passes(result, spies["dms"])

    def test_blocked_when_flag_not_set(self, spies, monkeypatch):
        monkeypatch.delenv(ENABLED_ENV, raising=False)
        result = registry.dispatch(_action("submit_warranty_claim"), {},
                                   fire_escalation=False)
        _declined(result, spies["dms"])

    def test_blocked_when_fleet_not_demo(self, spies, monkeypatch):
        monkeypatch.setenv(ENABLED_ENV, "true")
        result = registry.dispatch(_action("submit_warranty_claim", _NON_DEMO_FLEET), {},
                                   fire_escalation=False)
        _declined(result, spies["dms"])


# ---------------------------------------------------------------------------
# G1 exempt_recall — simulated decider always blocked
# ---------------------------------------------------------------------------

class TestG1SimulatedDeciderBlocked:
    """The simulated decider never satisfies G1 (exempt_recall)."""

    @pytest.mark.parametrize("flag", ["true", "false", ""])
    def test_always_blocked_regardless_of_flag(self, flag, spies, monkeypatch):
        if flag:
            monkeypatch.setenv(ENABLED_ENV, flag)
        else:
            monkeypatch.delenv(ENABLED_ENV, raising=False)
        result = registry.dispatch(_action("exempt_recall", _DEMO_FLEET), {},
                                   fire_escalation=False)
        _declined(result, spies["cms"])
        entry = result.audit_entry(by="executor-cms", at="2026-11-02T00:00:00Z")
        assert entry.get("guardrail") is None  # 11.1b: caught by top guard, not G1


# ---------------------------------------------------------------------------
# G4 complete_recall — simulated decider always blocked (not operator)
# ---------------------------------------------------------------------------

class TestG4SimulatedDeciderBlocked:
    def test_blocked_even_with_flag_and_demo_fleet(self, spies, monkeypatch):
        monkeypatch.setenv(ENABLED_ENV, "true")
        action = _action("complete_recall", _DEMO_FLEET,
                         {"service_record_ref": "RO-1", "vehicle_vin": "MRDVEH00000000015"})
        resolver = lambda ref, vin: {"vehicle_vin": "MRDVEH00000000015", "status": "Ready"}
        result = registry.dispatch(action, {}, fire_escalation=False,
                                   service_record_resolver=resolver)
        _declined(result, spies["cms"])
        entry = result.audit_entry(by="executor-cms", at="2026-11-02T00:00:00Z")
        assert entry.get("guardrail") is None  # 11.1b: caught by top guard, not G4


# ---------------------------------------------------------------------------
# G5 engineering kinds — simulated decider always blocked
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("kind", ["record_engineering_decision", "issue_recall",
                                   "issue_software_campaign"])
class TestG5SimulatedDeciderBlocked:
    def test_always_blocked(self, kind, spies, monkeypatch):
        monkeypatch.setenv(ENABLED_ENV, "true")
        result = registry.dispatch(_action(kind, _DEMO_FLEET), {}, fire_escalation=False)
        _declined(result, spies["adp"])
        entry = result.audit_entry(by="executor-adp", at="2026-11-02T00:00:00Z")
        assert entry.get("guardrail") is None  # 11.1b: caught by top guard, not G5


# ---------------------------------------------------------------------------
# apply_cost_action and move_vehicles — simulated decider always blocked (M3)
# ---------------------------------------------------------------------------

class TestCentralRecheckBlocksSimulated:
    """M3: admitting the simulated decider for apply_cost_action or move_vehicles."""

    def test_apply_cost_action_blocked(self, spies, monkeypatch):
        monkeypatch.setenv(ENABLED_ENV, "true")
        action = _action("apply_cost_action", _DEMO_FLEET, {
            "vehicle_vin": "MRDVEH00000000015",
            "cost_action": "driver_coaching",
            "month": "2026-11",
            "cause": "efficiency",
        })
        result = registry.dispatch(action, {}, fire_escalation=False)
        _declined(result, spies["cms"])
        # No named guardrail — central re-check uses no G id.
        entry = result.audit_entry(by="executor-cms", at="2026-11-02T00:00:00Z")
        assert entry.get("guardrail") is None

    def test_move_vehicles_blocked(self, spies, monkeypatch):
        monkeypatch.setenv(ENABLED_ENV, "true")
        action = _action("move_vehicles", _DEMO_FLEET, {
            "fleet_id": _DEMO_FLEET,
            "from_location_id": "loc-a",
            "to_location_id": "loc-b",
            "vehicle_vins": ["MRDVEH00000000015"],
            "forecast_week": "2026-W44",
            "transfer_cost_usd": 50.0,
            "predicted_on_rent_hours_gain": 10.0,
            "predicted_turnaways_avoided": 1,
        })
        result = registry.dispatch(action, {}, fire_escalation=False)
        _declined(result, spies["cms"])
        entry = result.audit_entry(by="executor-cms", at="2026-11-02T00:00:00Z")
        assert entry.get("guardrail") is None


# ---------------------------------------------------------------------------
# book_fleet_service and book_recall_service — simulated decider re-check
# ---------------------------------------------------------------------------

class TestBookKindsSimulatedDecider:
    """The new central re-check for book_fleet_service and book_recall_service."""

    @pytest.mark.parametrize("kind", ["book_fleet_service", "book_recall_service"])
    def test_passes_when_enabled_and_demo_fleet(self, kind, spies, monkeypatch):
        """RED: registry does not yet admit the simulated decider for these kinds."""
        monkeypatch.setenv(ENABLED_ENV, "true")
        result = registry.dispatch(_action(kind, _DEMO_FLEET), {},
                                   fire_escalation=False)
        _passes(result, spies["dms"])

    @pytest.mark.parametrize("kind", ["book_fleet_service", "book_recall_service"])
    def test_blocked_when_flag_not_set(self, kind, spies, monkeypatch):
        """M1: dropping the process-flag check would make this pass."""
        monkeypatch.delenv(ENABLED_ENV, raising=False)
        result = registry.dispatch(_action(kind, _DEMO_FLEET), {},
                                   fire_escalation=False)
        _declined(result, spies["dms"])

    @pytest.mark.parametrize("kind", ["book_fleet_service", "book_recall_service"])
    def test_blocked_when_non_demo_fleet(self, kind, spies, monkeypatch):
        """M2: dropping the demo-fleet check would make this pass."""
        monkeypatch.setenv(ENABLED_ENV, "true")
        result = registry.dispatch(_action(kind, _NON_DEMO_FLEET), {},
                                   fire_escalation=False)
        _declined(result, spies["dms"])

    @pytest.mark.parametrize("kind", ["book_fleet_service", "book_recall_service"])
    def test_non_simulated_decider_is_not_affected(self, kind, spies, monkeypatch):
        """An operator decision on these kinds is unchanged (no extra check applied)."""
        monkeypatch.delenv(ENABLED_ENV, raising=False)
        action = _action(kind, _NON_DEMO_FLEET)
        action["decided_by"] = {"kind": "operator", "id": _OPERATOR_SUB}
        registry.dispatch(action, {}, fire_escalation=False)
        # Reaches the spy (operator always passes these kinds, no guardrail fires).
        assert len(spies["dms"].calls) == 1


# ---------------------------------------------------------------------------
# M6 — kind alone (without the id) is not the stand-in
# ---------------------------------------------------------------------------

class TestSimulatedDeciderIdentityChecks:
    """Only the exact {kind, id} pair is the stand-in (M6)."""

    @pytest.mark.parametrize("decided_by", [
        {"kind": DECIDER_KIND, "id": "wrong-id"},
        {"kind": DECIDER_KIND},                          # no id
        {"kind": DECIDER_KIND.upper(), "id": DECIDER_ID},        # wrong-case kind
        {"kind": DECIDER_KIND, "id": ""},               # empty id
    ], ids=["wrong-id", "no-id", "wrong-case", "empty-id"])
    def test_impersonator_is_blocked_on_g2(self, decided_by, spies, monkeypatch):
        """M6: kind alone (wrong id) must not pass as the stand-in.
        Note: extra keys are rejected by schema additionalProperties: false.
        An {kind: operator, id: DECIDER_ID} passes G2 as a valid operator (correct).
        """
        monkeypatch.setenv(ENABLED_ENV, "true")
        action = _action("ground_vehicle", _DEMO_FLEET)
        action["decided_by"] = decided_by
        result = registry.dispatch(action, {}, fire_escalation=False)
        _declined(result, spies["cms"])


# ===========================================================================
# 11.1b — top-of-_check_guardrails guard over every ACTION_KINDS value
# ===========================================================================

@pytest.fixture
def all_spies(monkeypatch):
    """Patch every registered target system with a spy.

    Needed by the parametrised ACTION_KINDS tests so that any kind's dispatch
    can reach (or be blocked from) its executor.
    """
    out: dict[str, _SpyExecutor] = {}
    for ts in ("cms", "dms", "adp", "none", "retail", "vehicle-command",
               "ios-owner", "chat"):
        spy = _SpyExecutor(ts)
        monkeypatch.setitem(registry.EXECUTORS, ts, spy)
        out[ts] = spy
    return out


def _action_for_kind(kind: str, *, fleet_id: str = _DEMO_FLEET) -> dict:
    """Minimal valid-shaped action for *kind*, using the simulated decider."""
    params: dict = {"vehicle_vin": "MRDVEH00000000015"}
    if kind == "complete_recall":
        # G4 needs service_record_ref; the top guard fires before G4 anyway,
        # but the parametrised test also provides a resolver so the parameter
        # doesn't change the decline reason.
        params["service_record_ref"] = "RO-1"
    target = _ACTION_KINDS[kind].target_system
    return {
        "action_id": f"AVX-ACT-{abs(hash(kind)):08x}-0000-4000-8000-000000000001",
        "finding_id": "FIND-00000000-0000-4000-8000-000000000001",
        "action_kind": kind,
        "target_system": target,
        "fleet_id": fleet_id,
        "parameters": params,
        "decided_by": _sim_decider(),
    }


_NON_SIM_KINDS = sorted(k for k in _ACTION_KINDS if k not in _SIMULATED_KINDS)


class TestTopGuardAllKinds:
    """11.1b: parametrised over every ACTION_KINDS value.

    Non-SIMULATED_KINDS (spec-1a, retail, and the operator/engineer-only kinds) are
    declined at the top of _check_guardrails, even with the process flag set and the
    fleet in DEMO_FLEET_IDS.

    RED (before fix): spec-1a kinds (acknowledge_only, book_combined_visit,
    schedule_inspection, update_charging_schedule, file_warranty_pre, surface_recall)
    and retail kinds (lead_capture, configurator_recommend) fall through all existing
    per-kind checks and reach the executor instead of being declined.
    """

    @pytest.mark.parametrize("kind", _NON_SIM_KINDS)
    def test_non_simulated_kind_is_declined(self, kind, all_spies, monkeypatch):
        """Simulated decider on a non-SIMULATED_KINDS kind → declined.

        RED before the fix for: acknowledge_only, book_combined_visit,
        schedule_inspection, update_charging_schedule, file_warranty_pre,
        surface_recall, lead_capture, configurator_recommend.
        """
        monkeypatch.setenv(ENABLED_ENV, "true")
        action = _action_for_kind(kind)
        # A resolver is injected so complete_recall isn't blocked by
        # "resolver absent" before we can confirm the top-guard decline.
        resolver = lambda ref, vin: {"vehicle_vin": vin, "status": "Ready"}
        result = registry.dispatch(
            action, {}, fire_escalation=False, service_record_resolver=resolver,
        )
        # Assertion: DECLINED regardless of which mechanism fires
        _declined(result, spy=None, expect_spy_empty=False)

    @pytest.mark.parametrize("kind", sorted(_SIMULATED_KINDS))
    def test_simulated_kind_passes_when_enabled_and_demo_fleet(
            self, kind, all_spies, monkeypatch):
        """SIMULATED_KINDS reach the executor when flag is set and fleet is demo.

        Regression guard for 11.1a; included here so the parametrised suite
        covers all ACTION_KINDS.
        """
        monkeypatch.setenv(ENABLED_ENV, "true")
        action = _action_for_kind(kind)
        result = registry.dispatch(action, {}, fire_escalation=False)
        spy = all_spies[_ACTION_KINDS[kind].target_system]
        _passes(result, spy)


class TestTopGuardImpersonators:
    """11.1b: impersonators (right kind, wrong or missing id) on non-simulated kinds.

    Before the fix a wrong-id decider on acknowledge_only falls through all per-kind
    checks (none of them checks the id) and reaches the executor. After the fix the
    top guard intercepts any decided_by whose kind equals DECIDER_KIND and fails the
    is_simulated_decider() check → declined.

    RED (before fix): wrong-id, no-id, empty-id cases all pass through to the executor.
    """

    @pytest.mark.parametrize("decided_by", [
        {"kind": DECIDER_KIND, "id": "wrong-id"},
        {"kind": DECIDER_KIND},                    # no id key
        {"kind": DECIDER_KIND, "id": ""},          # empty id
    ], ids=["wrong-id", "no-id", "empty-id"])
    def test_impersonator_declined_on_acknowledge_only(
            self, decided_by, all_spies, monkeypatch):
        """An impersonator with DECIDER_KIND but wrong/missing id is declined.

        RED before the fix: these deciders have the wrong kind or missing id for
        is_simulated_decider(), but the existing checks never inspect the id for
        spec-1a kinds, so they fall through to the executor.
        """
        monkeypatch.setenv(ENABLED_ENV, "true")
        action = _action_for_kind("acknowledge_only")
        action["decided_by"] = decided_by
        result = registry.dispatch(action, {}, fire_escalation=False)
        _declined(result, spy=None, expect_spy_empty=False)
