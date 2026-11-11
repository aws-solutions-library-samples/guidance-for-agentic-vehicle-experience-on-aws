"""T1.2b: central decider re-check in registry._check_guardrails.

apply_cost_action and move_vehicles both require a valid decider before the
executor is reached:

  apply_cost_action: valid operator required; anything else → declined,
    failure_kind=target_refused, guardrail=None (no G6/G7 id, contract v2.8).

  move_vehicles: valid operator OR {kind:"rule", id:<non-empty string>} passes;
    anything else → declined as above.

All tests go through registry.dispatch with a _SpyExecutor replacing the cms
executor so a deleted check shows up as "the spy was called instead of declined".
"""
from __future__ import annotations

import pytest

from agents.executors import registry
from agents.executors.base import ActionStatus, ExecuteResult, FailureKind
from agents.executors.failure_reason import failure_reason

_CMS_TARGET = "cms"
_VIN = "MRDVEH00000000021"
_FLEET = "fleet-meridian-01"
_RULE_ID = f"{_FLEET}/small-moves"
_OP_ID = "op-sub-0001"

_BASE_COST_ACTION = {
    "action_id": "AVX-ACT-11110000-0000-4000-8000-000000000001",
    "finding_id": "FIND-11110000-0000-4000-8000-000000000001",
    "action_kind": "apply_cost_action",
    "target_system": _CMS_TARGET,
    "parameters": {
        "vehicle_vin": _VIN,
        "cost_action": "driver_coaching",
        "month": "2026-10",
        "cause": "efficiency",
    },
}

_BASE_MOVE = {
    "action_id": "AVX-ACT-22220000-0000-4000-8000-000000000001",
    "finding_id": "FIND-22220000-0000-4000-8000-000000000001",
    "action_kind": "move_vehicles",
    "target_system": _CMS_TARGET,
    "parameters": {
        "fleet_id": _FLEET,
        "from_location_id": "loc-sea-a",
        "to_location_id": "loc-sea-b",
        "vehicle_vins": [_VIN],
        "forecast_week": "2026-W42",
        "transfer_cost_usd": 50.0,
        "predicted_on_rent_hours_gain": 10.0,
        "predicted_turnaways_avoided": 1,
    },
}


class _SpyExecutor:
    target_system = _CMS_TARGET

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def dispatch(self, action: dict, claims: dict) -> ExecuteResult:
        self.calls.append(action)
        return ExecuteResult(status=ActionStatus.COMPLETED, target_ref="spy")


@pytest.fixture
def spy(monkeypatch: pytest.MonkeyPatch) -> _SpyExecutor:
    s = _SpyExecutor()
    monkeypatch.setitem(registry.EXECUTORS, _CMS_TARGET, s)
    return s


def _declined(result: ExecuteResult, spy: _SpyExecutor) -> None:
    """Assert result is the correct decider-check decline."""
    assert result.status == ActionStatus.DECLINED, (
        f"Expected DECLINED, got {result.status!r}. "
        "Central decider check must block before the executor is reached."
    )
    assert result.failure_kind == FailureKind.TARGET_REFUSED, (
        f"Expected TARGET_REFUSED, got {result.failure_kind!r}"
    )
    assert result.escalate is False
    assert result.guardrail is None, (
        f"Expected guardrail=None (no G6/G7 id, contract v2.8), got {result.guardrail!r}"
    )
    _expected_reason = failure_reason(
        target_system=_CMS_TARGET, failure_kind="target_refused"
    )
    assert result.failure_reason == _expected_reason, (
        f"Expected failure_reason={_expected_reason!r}, got {result.failure_reason!r}. "
        "The central re-check must use the target-refused sentence, not a guardrail sentence "
        "or None."
    )
    assert spy.calls == [], (
        "Executor must not be called when the central decider check fires"
    )


def _passes(result: ExecuteResult, spy: _SpyExecutor) -> None:
    """Assert the decider check passed through (spy was called)."""
    assert len(spy.calls) == 1, (
        f"Spy was not called — central decider check blocked a valid decider. "
        f"result.status={result.status!r}, result.failure_kind={result.failure_kind!r}"
    )


# ---------------------------------------------------------------------------
# apply_cost_action: operator required
# ---------------------------------------------------------------------------

class TestApplyCostActionDeciderCheck:
    """apply_cost_action requires a valid operator; no executor for anything else."""

    @pytest.mark.parametrize("decided_by", [
        None,
        "operator",
        {"kind": "operator", "id": ""},
        {"kind": "operator"},
        {"kind": "Operator", "id": _OP_ID},
        {"id": _OP_ID},
        {"kind": "rule", "id": _RULE_ID},
        {"kind": "engineer", "id": "eng-001"},
        {"kind": "user", "id": _OP_ID},
        {},
    ], ids=[
        "none", "string", "empty-id", "no-id", "wrong-case", "no-kind",
        "rule", "engineer", "user", "empty-dict",
    ])
    def test_non_operator_is_declined(self, decided_by: object, spy: _SpyExecutor) -> None:
        action = {**_BASE_COST_ACTION, "decided_by": decided_by}
        result = registry.dispatch(action, {}, fire_escalation=False)
        _declined(result, spy)

    def test_valid_operator_reaches_executor(self, spy: _SpyExecutor) -> None:
        action = {**_BASE_COST_ACTION, "decided_by": {"kind": "operator", "id": _OP_ID}}
        result = registry.dispatch(action, {}, fire_escalation=False)
        _passes(result, spy)


# ---------------------------------------------------------------------------
# move_vehicles: operator OR well-formed rule decider passes
# ---------------------------------------------------------------------------

class TestMoveVehiclesDeciderCheck:
    """move_vehicles: operator or {kind:"rule", id:<non-empty>} passes; else declined."""

    def test_valid_operator_reaches_executor(self, spy: _SpyExecutor) -> None:
        action = {**_BASE_MOVE, "decided_by": {"kind": "operator", "id": _OP_ID}}
        result = registry.dispatch(action, {}, fire_escalation=False)
        _passes(result, spy)

    def test_valid_rule_decider_reaches_executor(self, spy: _SpyExecutor) -> None:
        """A well-formed rule decider passes — auto-approver's shape."""
        action = {**_BASE_MOVE, "decided_by": {"kind": "rule", "id": _RULE_ID}}
        result = registry.dispatch(action, {}, fire_escalation=False)
        _passes(result, spy)

    @pytest.mark.parametrize("decided_by", [
        None,
        "operator",
        {"kind": "operator", "id": ""},
        {"kind": "Operator", "id": _OP_ID},
        {"kind": "engineer", "id": "eng-001"},
        {"kind": "user", "id": _OP_ID},
        {"kind": "rule", "id": ""},
        {"kind": "rule", "id": None},
        {"kind": "rule", "id": 42},
        {"kind": "Rule", "id": _RULE_ID},
        {"kind": "rule"},
        {},
    ], ids=[
        "none", "string", "operator-empty-id", "operator-wrong-case",
        "engineer", "user",
        "rule-empty-id", "rule-null-id", "rule-int-id",
        "rule-wrong-case", "rule-no-id", "empty-dict",
    ])
    def test_invalid_decider_is_declined(self, decided_by: object, spy: _SpyExecutor) -> None:
        action = {**_BASE_MOVE, "decided_by": decided_by}
        result = registry.dispatch(action, {}, fire_escalation=False)
        _declined(result, spy)

    def test_missing_decided_by_key_is_declined(self, spy: _SpyExecutor) -> None:
        """No decided_by key at all → declined (not just None)."""
        action = {k: v for k, v in _BASE_MOVE.items() if k != "decided_by"}
        result = registry.dispatch(action, {}, fire_escalation=False)
        _declined(result, spy)


# ---------------------------------------------------------------------------
# G2.4: rule-decider id shape — <fleet_id>/<rule_id> with ^[^/\s]{1,128}$
# on each part (security S4).  RED against the current "any non-empty string"
# check; GREEN after _RULE_DECIDER_ID_RE is added to _is_valid_rule_decider.
# ---------------------------------------------------------------------------

class TestRuleDeciderIdShape:
    """Rule decider id must be <fleet_id>/<rule_id>.

    Fleet part: ``^[A-Za-z0-9_-]{1,64}$`` — CMS enrolment shape
    (admin_add_vehicle/handler.py:67 on CMS main).
    Rule part: ``^[^/\\s]{1,128}$`` and ``str.isprintable()``.
    Checked with ``fullmatch`` on the whole id.

    The auto-approver writes ``f"{fleet_id}/{rule_id}"`` (handler.py:410)
    after validating ``rule_id`` against ``^[^/\\s]{1,128}$`` (handler.py:94).

    Tests added in G2.4 RED commit; extended in H3.1 RED commit with
    control-character and fleet-length cases, and in Fix Group 4 with
    rule-part control and format characters (isprintable()).
    """

    @pytest.mark.parametrize("rule_id", [
        "   ",                           # whitespace-only (bool=True but \s rejected)
        "\t/rule",                       # tab in fleet part
        "\n/rule",                       # newline in fleet part
        "fleet/\trule",                  # tab in rule part
        "fleet/rule name",               # space in rule part
        "fleet name/rule",               # space in fleet part
        "fleet-meridian-01",             # no slash at all
        "fleet/rule/extra",              # extra slash (two slashes)
        "/",                             # just a slash — both parts empty
        "/small-moves",                  # empty fleet part (leading slash)
        "fleet-meridian-01/",            # empty rule part (trailing slash)
        "fleet/" + "x" * 129,           # rule part 129 chars — over the 128 limit
        # H3.1 RED: control characters and fleet-length cases
        "fleet\x00/rule",               # NUL in fleet part (\x00, not \s)
        "fleet\x07/rule",               # BEL in fleet part (\x07, not \s)
        "fleet\x1b/rule",               # ESC in fleet part (\x1b, not \s)
        "fleet/rule\x7f",               # DEL in rule part (\x7f, not \s)
        "fleet\u200b/rule",             # zero-width space (\u200b, not matched by \s)
        "x" * 65 + "/rule",             # fleet part 65 chars — over CMS 64-char limit
        "fleet.name-01/rule-id",        # dot in fleet part (not in [A-Za-z0-9_-])
        "x" * 128 + "/rule",            # fleet part 128 chars — over CMS 64-char limit
        "fleet/rule\n",                 # trailing newline on rule (declined by isprintable(); was the fullmatch→match guard)
        # Fix Group 4 (review c4 W1): control and format characters in the RULE part
        "fleet/rule\x00",               # NUL in rule part
        "fleet/rule\x1b",               # ESC in rule part
        "fleet/rule\u200b",             # zero-width space in rule part
        "fleet/rule\x9b",               # C1 CSI in rule part (8-bit terminal escape)
        "fleet/\u202eelur",             # right-to-left override in rule part
        "fleet/rule\ufeff",             # BOM / zero-width no-break space in rule part
        "fleet/rule\xad",               # soft hyphen in rule part
    ], ids=[
        "whitespace-only",
        "tab-in-fleet",
        "newline-in-fleet",
        "tab-in-rule",
        "space-in-rule",
        "space-in-fleet",
        "no-slash",
        "extra-slash",
        "just-slash",
        "empty-fleet",
        "empty-rule",
        "rule-too-long",
        "nul-in-fleet",
        "bel-in-fleet",
        "esc-in-fleet",
        "del-in-rule",
        "zero-width-space-in-fleet",
        "fleet-too-long-65",
        "dot-in-fleet",
        "fleet-too-long-128",
        "trailing-newline-on-rule",
        "nul-in-rule",
        "esc-in-rule",
        "zero-width-space-in-rule",
        "c1-csi-in-rule",
        "rlo-in-rule",
        "bom-in-rule",
        "soft-hyphen-in-rule",
    ])
    def test_malformed_rule_id_shape_is_declined(
        self, rule_id: str, spy: _SpyExecutor
    ) -> None:
        """A rule decider id outside <fleet_id>/<rule_id> must be declined.

        RED until _RULE_DECIDER_ID_RE is in _is_valid_rule_decider.
        """
        action = {**_BASE_MOVE, "decided_by": {"kind": "rule", "id": rule_id}}
        result = registry.dispatch(action, {}, fire_escalation=False)
        _declined(result, spy)

    @pytest.mark.parametrize("rule_id", [
        f"{_FLEET}/small-moves",              # canonical auto-approver shape
        "f/r",                                # min-length both parts (1 char each)
        "fleet/" + "x" * 128,                # rule part exactly 128 chars — valid
        "a" * 64 + "/rule",                  # fleet part exactly 64 chars — valid (CMS limit)
        "fleet-01/rule-id",                  # hyphens and digits in fleet — valid
    ], ids=[
        "canonical",
        "min-length",
        "rule-max-length",
        "fleet-max-length-64",
        "hyphens-digits",
    ])
    def test_valid_rule_id_shape_passes(
        self, rule_id: str, spy: _SpyExecutor
    ) -> None:
        """A well-formed <fleet_id>/<rule_id> must reach the executor.

        These are GREEN both before and after the fix (positive regression guard).
        """
        action = {**_BASE_MOVE, "decided_by": {"kind": "rule", "id": rule_id}}
        result = registry.dispatch(action, {}, fire_escalation=False)
        _passes(result, spy)
