"""Red tests: the four guardrails in registry.dispatch() — spec D5, contract § 10.

Expected interface (Group 2):

    registry.dispatch(action, claims, *, fire_escalation=True,
                      service_record_resolver=None, ...)

    service_record_resolver: callable(ref: str, vin: str) -> dict | None
        Returns the DMS repair order {"vehicle_vin": ..., "status": ...} for ref;
        None when DMS has no such repair order (404); raises when the lookup itself
        fails (any other error, timeout). None as the resolver itself means "not
        configured". G4 refuses in all three cases (contract § 10a rows 6-7).

    A guardrail refusal returns ExecuteResult(status="declined",
    failure_kind="target_refused", escalate=False), and
    result.audit_entry(by=..., at=...) carries "guardrail": "G1".."G4" and the
    contract § 10a `failure_reason` sentence, with no `error_status`
    (security review Cycle 1 S2: the sentence, not the free-text note, is pinned).
    The executor for the Action's target is never called.

Guardrails, pinned by literal name (never imported from policy.py):

    G1  exempt_recall          refused unless decided_by.kind == "operator"
    G2  ground_vehicle         refused unless decided_by.kind == "operator"
    G3  submit_warranty_claim  refused unless decided_by.kind == "operator"
    G4  complete_recall        refused unless decided_by.kind == "operator" AND
                               service_record_ref
                               resolves to an RO whose vehicle_vin equals
                               parameters.vehicle_vin (set by the approve route from
                               the Finding's scope) and whose status is Ready,
                               Delivered or Invoiced

Each kind is routed to its real target (contract § 4): cms or dms. A spy replaces
that target's executor, so a deleted guardrail shows up as "the spy was called".
"""
from __future__ import annotations

import pytest

from agents.executors import registry
from agents.executors.base import ActionStatus, ExecuteResult, FailureKind

#: Applied per test, not per module: the "operator reaches the executor" tests pass
#: today (dispatch already routes them) and are regression guards. Group 2 removed
#: every RED mark.
import functools  # noqa: E402

def _noop(fn=None):  # type: ignore[no-untyped-def]
    """No-op decorator that replaces the removed @RED marker."""
    if fn is None:
        return _noop
    return fn

RED = _noop

VIN = "MRDVEH00000000015"
OTHER_VIN = "MRDVEH00000000016"
OPERATOR_SUB = "a1b2c3d4-0000-4000-8000-000000000001"

#: Literal guardrail table: kind -> (guardrail id, real target_system).
GUARDED = {
    "exempt_recall": ("G1", "cms"),
    "ground_vehicle": ("G2", "cms"),
    "submit_warranty_claim": ("G3", "dms"),
    "complete_recall": ("G4", "cms"),
}
OPERATOR_GUARDED = ["exempt_recall", "ground_vehicle", "submit_warranty_claim"]


class _SpyExecutor:
    """Stands in for a target's executor; records every dispatch."""

    def __init__(self, target_system: str):
        self.target_system = target_system
        self.calls: list[dict] = []

    def dispatch(self, action, claims):
        self.calls.append(action)
        return ExecuteResult(status=ActionStatus.COMPLETED, target_ref="spy-ref")


@pytest.fixture
def spies(monkeypatch):
    out = {}
    for target in ("cms", "dms"):
        spy = _SpyExecutor(target)
        monkeypatch.setitem(registry.EXECUTORS, target, spy)
        out[target] = spy
    return out


def _action(kind: str, decided_by=None, parameters=None) -> dict:
    action = {
        "action_id": f"AVX-ACT-00000000-0000-4000-8000-{abs(hash(kind)) % 10**12:012d}",
        "finding_id": "FIND-00000000-0000-4000-8000-000000000001",
        "action_kind": kind,
        "target_system": GUARDED[kind][1],
        # The approve route sets vehicle_vin from the Finding's own scope (as
        # _dms_parameters does), so G4 compares against a server-derived VIN.
        "parameters": {"vehicle_vin": VIN, **(parameters or {})},
    }
    if decided_by is not None:
        action["decided_by"] = decided_by
    return action


def _operator():
    return {"kind": "operator", "id": OPERATOR_SUB}


def _rule():
    return {"kind": "rule", "id": "FLEET-MRD-A/ack-routine"}


def _claims():
    return {"sub": OPERATOR_SUB}


#: Contract § 10a rows 1-3, copied literally.
OPERATOR_REASON = {
    "exempt_recall": "Blocked: exempting a safety recall needs a fleet operator.",
    "ground_vehicle": "Blocked: grounding a vehicle needs a fleet operator.",
    "submit_warranty_claim": "Blocked: sending a warranty claim needs a fleet operator.",
}
G4_NOT_OPERATOR = "Blocked: completing a recall needs a fleet operator."
G4_NO_REF = "Blocked: completing a recall needs a service record."
G4_NOT_CHECKED = "Blocked: the service record could not be checked."
G4_NOT_FOUND = "Blocked: the service record could not be found."
G4_OTHER_VEHICLE = "Blocked: the service record is for a different vehicle."


def _assert_blocked(result, spies, kind, reason):
    guardrail, target = GUARDED[kind]
    assert result.status == ActionStatus.DECLINED
    assert result.failure_kind == FailureKind.TARGET_REFUSED
    assert result.escalate is False
    entry = result.audit_entry(by=f"executor-{target}", at="2026-10-02T00:00:00Z")
    assert entry["guardrail"] == guardrail
    assert entry["to_status"] == "declined"
    assert entry["failure_reason"] == reason
    assert "error_status" not in entry, "a guardrail never sends the Action (contract § 10a)"
    assert spies[target].calls == [], f"{guardrail}: the {target} executor must not run"


# ---------------------------------------------------------------------------
# G1–G3: operator required
# ---------------------------------------------------------------------------

class TestOperatorGuardrails:
    @RED
    @pytest.mark.parametrize("kind", OPERATOR_GUARDED)
    def test_rule_decided_is_blocked(self, kind, spies):
        result = registry.dispatch(_action(kind, _rule()), _claims(), fire_escalation=False)
        _assert_blocked(result, spies, kind, OPERATOR_REASON[kind])

    @RED
    @pytest.mark.parametrize("kind", OPERATOR_GUARDED)
    def test_user_decided_is_blocked(self, kind, spies):
        """A driver, owner or service advisor (kind "user") never satisfies G1–G3."""
        result = registry.dispatch(
            _action(kind, {"kind": "user", "id": OPERATOR_SUB}), _claims(),
            fire_escalation=False)
        _assert_blocked(result, spies, kind, OPERATOR_REASON[kind])

    @RED
    @pytest.mark.parametrize("kind", OPERATOR_GUARDED)
    @pytest.mark.parametrize("decided_by", [
        None,                                   # missing
        "operator",                             # not an object
        {"kind": "operator", "id": ""},         # empty id
        {"kind": "operator"},                   # no id
        {"kind": "Operator", "id": OPERATOR_SUB},  # wrong case
        {"id": OPERATOR_SUB},                   # no kind
    ], ids=["missing", "string", "empty-id", "no-id", "wrong-case", "no-kind"])
    def test_malformed_decided_by_is_blocked(self, kind, decided_by, spies):
        result = registry.dispatch(_action(kind, decided_by), _claims(), fire_escalation=False)
        _assert_blocked(result, spies, kind, OPERATOR_REASON[kind])

    @pytest.mark.parametrize("kind", OPERATOR_GUARDED)
    def test_operator_decided_reaches_the_executor(self, kind, spies):
        target = GUARDED[kind][1]
        result = registry.dispatch(_action(kind, _operator()), _claims(), fire_escalation=False)
        assert result.status != ActionStatus.DECLINED
        assert len(spies[target].calls) == 1


# ---------------------------------------------------------------------------
# G4: completed service record for the Finding's VIN
# ---------------------------------------------------------------------------

def _resolver(ro):
    calls = []

    def resolve(ref, vin):
        calls.append((ref, vin))
        return ro

    resolve.calls = calls
    return resolve


def _not_complete(status):
    word = status if status in {"Draft", "InProgress", "AwaitingParts", "Ready",
                                "Delivered", "Closed", "Invoiced"} else "unknown"
    return f"Blocked: the service record is not complete (status {word})."


class TestG4CompleteRecall:
    @RED
    def test_no_ref_is_blocked(self, spies):
        result = registry.dispatch(
            _action("complete_recall", _operator(), {}), _claims(),
            fire_escalation=False,
            service_record_resolver=_resolver({"vehicle_vin": VIN, "status": "Ready"}))
        _assert_blocked(result, spies, "complete_recall", G4_NO_REF)

    @RED
    def test_no_resolver_configured_is_blocked(self, spies):
        result = registry.dispatch(
            _action("complete_recall", _operator(), {"service_record_ref": "RO-1"}),
            _claims(), fire_escalation=False)
        _assert_blocked(result, spies, "complete_recall", G4_NOT_CHECKED)

    @RED
    def test_unresolvable_ref_is_blocked(self, spies):
        result = registry.dispatch(
            _action("complete_recall", _operator(), {"service_record_ref": "RO-1"}),
            _claims(), fire_escalation=False, service_record_resolver=_resolver(None))
        _assert_blocked(result, spies, "complete_recall", G4_NOT_FOUND)

    @RED
    def test_ro_for_another_vehicle_is_blocked(self, spies):
        result = registry.dispatch(
            _action("complete_recall", _operator(), {"service_record_ref": "RO-1"}),
            _claims(), fire_escalation=False,
            service_record_resolver=_resolver({"vehicle_vin": OTHER_VIN, "status": "Ready"}))
        _assert_blocked(result, spies, "complete_recall", G4_OTHER_VEHICLE)

    @RED
    @pytest.mark.parametrize("status", ["Closed", "InProgress", "Draft", "AwaitingParts", ""])
    def test_ro_not_completed_is_blocked(self, status, spies):
        result = registry.dispatch(
            _action("complete_recall", _operator(), {"service_record_ref": "RO-1"}),
            _claims(), fire_escalation=False,
            service_record_resolver=_resolver({"vehicle_vin": VIN, "status": status}))
        _assert_blocked(result, spies, "complete_recall", _not_complete(status))

    @RED
    @pytest.mark.parametrize("decided_by", [
        {"kind": "rule", "id": "FLEET-MRD-A/ack-routine"},
        {"kind": "user", "id": OPERATOR_SUB},
        None,
        {"kind": "operator", "id": ""},
    ], ids=["rule", "user", "missing", "empty-id"])
    def test_non_operator_with_a_good_record_is_still_blocked(self, decided_by, spies):
        """complete_recall is operator-only like every FI kind (contract § 10): a
        rule, a user or a malformed decider is blocked even with a completed RO."""
        result = registry.dispatch(
            _action("complete_recall", decided_by, {"service_record_ref": "RO-1"}),
            _claims(), fire_escalation=False,
            service_record_resolver=_resolver({"vehicle_vin": VIN, "status": "Ready"}))
        _assert_blocked(result, spies, "complete_recall", G4_NOT_OPERATOR)

    @RED
    def test_failed_lookup_is_blocked_as_not_checked(self, spies):
        """A DMS lookup that errors is not "not found" (contract § 10a row 6)."""
        def resolve(ref, vin):
            raise TimeoutError("SENTINEL-internal-dms.corp timed out")
        result = registry.dispatch(
            _action("complete_recall", _operator(), {"service_record_ref": "RO-1"}),
            _claims(), fire_escalation=False, service_record_resolver=resolve)
        _assert_blocked(result, spies, "complete_recall", G4_NOT_CHECKED)
        entry = result.audit_entry(by="executor-cms", at="2026-10-02T00:00:00Z")
        assert "SENTINEL" not in repr(entry)

    @RED
    def test_operator_check_comes_first_and_dms_is_not_read(self, spies):
        """G4 order (contract § 10a): a non-operator gets the operator sentence even with
        no ref, and AVX never reads DMS on that decider's behalf."""
        resolve = _resolver({"vehicle_vin": VIN, "status": "Ready"})
        for params in ({}, {"service_record_ref": "RO-1"}):
            result = registry.dispatch(
                _action("complete_recall", _rule(), params), _claims(),
                fire_escalation=False, service_record_resolver=resolve)
            _assert_blocked(result, spies, "complete_recall", G4_NOT_OPERATOR)
        assert resolve.calls == [], "DMS must not be read for a non-operator decider"

    @RED
    @pytest.mark.parametrize("status", ["Ready", "Delivered", "Invoiced"])
    def test_completed_ro_for_this_vin_reaches_the_executor(self, status, spies):
        resolve = _resolver({"vehicle_vin": VIN, "status": status})
        result = registry.dispatch(
            _action("complete_recall", _operator(), {"service_record_ref": "RO-1"}),
            _claims(), fire_escalation=False, service_record_resolver=resolve)
        assert result.status != ActionStatus.DECLINED
        assert len(spies["cms"].calls) == 1
        assert resolve.calls == [("RO-1", VIN)], "the resolver must be asked about the Finding's VIN"



# ---------------------------------------------------------------------------
# W1 (review FG2b) — absent VIN on both sides must be blocked, not passed
# ---------------------------------------------------------------------------

class TestG4AbsentVin:
    """review FG2b W1: None == None must not pass G4.

    `parameters.get("vehicle_vin")` returns None for fleet-scoped Findings.
    A resolver that returns an RO without `vehicle_vin` then gives None != None
    → False, so a Ready RO reached the executor unchecked.  Both VINs must be
    non-empty strings that are equal before G4 passes.
    """

    def test_absent_vin_on_both_sides_is_blocked(self, spies) -> None:
        """The mutant `if vin and ro.get("vehicle_vin") != vin` survives the old suite.

        New test: action has no vehicle_vin, RO has no vehicle_vin → must block.
        """
        action = {
            "action_id": "AVX-ACT-00000000-0000-4000-8000-000000000099",
            "finding_id": "FIND-00000000-0000-4000-8000-000000000001",
            "action_kind": "complete_recall",
            "target_system": "cms",
            "parameters": {"service_record_ref": "RO-77"},  # no vehicle_vin key
            "decided_by": _operator(),
        }
        result = registry.dispatch(
            action, _claims(), fire_escalation=False,
            service_record_resolver=_resolver({"status": "Ready"}),  # no vehicle_vin key
        )
        _assert_blocked(result, spies, "complete_recall", G4_OTHER_VEHICLE)

    def test_vin_present_only_in_parameters_is_blocked(self, spies) -> None:
        """VIN in parameters but absent from the RO → still a mismatch (block)."""
        action = {
            "action_id": "AVX-ACT-00000000-0000-4000-8000-000000000099",
            "finding_id": "FIND-00000000-0000-4000-8000-000000000001",
            "action_kind": "complete_recall",
            "target_system": "cms",
            "parameters": {"service_record_ref": "RO-77", "vehicle_vin": VIN},
            "decided_by": _operator(),
        }
        result = registry.dispatch(
            action, _claims(), fire_escalation=False,
            service_record_resolver=_resolver({"status": "Ready"}),  # no vehicle_vin
        )
        _assert_blocked(result, spies, "complete_recall", G4_OTHER_VEHICLE)

    def test_empty_string_vin_on_both_sides_is_blocked(self, spies) -> None:
        """Empty-string VINs on both sides must be blocked.

        review FG3b S1: mutant drops the ``and vin`` / ``and ro_vin`` truthiness
        checks in registry.py; then ``"" == ""`` is True and the guardrail passes.
        Both VINs must be non-empty strings for G4 to succeed.
        """
        action = {
            "action_id": "AVX-ACT-00000000-0000-4000-8000-000000000099",
            "finding_id": "FIND-00000000-0000-4000-8000-000000000001",
            "action_kind": "complete_recall",
            "target_system": "cms",
            "parameters": {"service_record_ref": "RO-77", "vehicle_vin": ""},
            "decided_by": _operator(),
        }
        result = registry.dispatch(
            action, _claims(), fire_escalation=False,
            service_record_resolver=_resolver({"vehicle_vin": "", "status": "Ready"}),
        )
        _assert_blocked(result, spies, "complete_recall", G4_OTHER_VEHICLE)
