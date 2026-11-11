"""Tests for mutations that an overnight AVX executor sweep (prompt 100,
2026-10-24) found surviving against the then-current suite.

Each test here pins the *property* a surviving mutant violated — not the
mutation operator. Every test:

- Passes against unmutated code (verified in the sweep workdir).
- Fails if the specific mutation is applied to the target source (verified by
  re-running after rewriting the file; recorded in the sweep's `report.md`).

See `~/.kiro/steering/testing.md` § "Mutation testing at the green boundary"
and `agents/executors/tests/mutation_battery.py` for the broader framing. The
report, mutant inventory and per-mutant results from this run are archived at
`/tmp/2026-10-23-executor-sweep/results/` (not committed — ephemeral).

Mutation sweep sources
----------------------
Pinned AVX main at sweep time:
`2f7f23a33f2744e8972ca78503202a7cc0ecf67e` (5 ahead of origin/main
`00d83887`; the required pin `603531e3` is its ancestor).

Modules audited: `agents/executors/{base,cms,dms,adp,policy,registry,
target_reason,failure_reason}.py`, `lambdas/api-avx-findings/handler.py`,
`lambdas/avx-auto-approver/handler.py` — 2957 mutants in total, of which 457
survived. Overall mutation score 83% (CAUGHT / (CAUGHT + SURVIVED)); the
weakest surfaces were `agents.executors.cms` (55.8%), `agents.executors.dms`
(49.5%), and `agents.executors.adp` (48.0%). The approve-route handler
(`lambdas/api-avx-findings/handler.py`) scored 100%.

Covered mutants
---------------
Each test's docstring states the (file:lineno, kind, mutation, func) it pins.
"""
from __future__ import annotations

import time
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
import responses as rsps  # type: ignore[import]

from agents.executors.base import (
    ActionStatus,
    FailureKind,
    RetryPolicy,
)


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

_VIN = "MRDVEH00000000015"
_CAMPAIGN_ID = "MRD-2024-001"
_FAULT_CODE = "C0035"
_RO_ID = "ro-mut-7001"
_LINE_NO = "3"
_ACTION_UUID = "deadbeef-1234-4000-8000-abcdef000001"
_FINDING_ID = f"FND-{_ACTION_UUID}"

_CMS_ENDPOINT = "https://cms-staging.test"
_DMS_BASE = "http://dms-staging.test"

_OPERATOR_CLAIMS: dict[str, Any] = {
    "sub": "op-sub-mut-0001",
    "cognito:groups": ["fleet-operator"],
    "custom:fleetIds": "fleet-meridian-01",
    "token": "Bearer.mut.jwt",
}


def _make_cms_executor():
    """Build a `CmsExecutor` with stubbed collaborators and zero-ms backoff."""
    from agents.executors.cms import CmsExecutor

    return CmsExecutor(
        sleep_fn=lambda _: None,
        retry_policy=RetryPolicy(retry_count=3, backoff_base_ms=0, backoff_cap_ms=0),
    )


def _make_dms_executor():
    """Build a `DmsExecutor` with stubbed collaborators and zero-ms backoff."""
    from agents.executors.dms import DmsExecutor

    stub_ssm = MagicMock()
    stub_ssm.get_parameter.side_effect = Exception("stubbed SSM")
    return DmsExecutor(
        enqueue_ios_owner=MagicMock(),
        events_client=MagicMock(),
        cloudwatch_client=MagicMock(),
        ssm_client=stub_ssm,
        sleep_fn=lambda _: None,
        retry_policy=RetryPolicy(retry_count=3, backoff_base_ms=0, backoff_cap_ms=0),
    )


# ---------------------------------------------------------------------------
# Pick 1: `_grounding_body` — the `_both_present` sentinel must be TRUTHY
# ---------------------------------------------------------------------------
#   file: agents/executors/cms.py:231
#   kind: BOOL_CONST_FLIP
#   mutation: `True` -> `False`  (inside `return {"_both_present": True}`)
#
# `_dispatch_ground_vehicle` reads `body.get("_both_present")` and uses its
# truthiness to decide whether the parameters are ambiguous (contract § 4).
# Flipping the sentinel to `False` makes the ambiguity check falsy, so the
# function falls through to a real HTTP POST with both recall_id and
# fault_codes set — exactly the schema violation contract § 4 forbids.
#
# This test asserts the ambiguity path is taken and NO HTTP call is attempted.
# ---------------------------------------------------------------------------


@rsps.activate
def test_ground_vehicle_rejects_both_recall_and_fault_codes_without_http_call():
    """Both recall_id and fault_codes present → AmbiguousGroundingForm terminal.

    Pins `cms.py:231` `_both_present` sentinel truthiness. If the sentinel is
    flipped to False, the ambiguity check at line 598 falls through and the
    executor would POST a malformed body. The `rsps.POST` passthrough below
    is DELIBERATELY not registered — any outbound call raises `ConnectionError`
    from `responses`, which the test's assertions catch as a non-schema failure.
    """
    executor = _make_cms_executor()
    action = {
        "action_id": f"AVX-ACT-{_ACTION_UUID}",
        "action_kind": "ground_vehicle",
        "target_system": "cms",
        "parameters": {
            "vehicle_vin": _VIN,
            "recall_id": _CAMPAIGN_ID,
            "fault_codes": [_FAULT_CODE],
        },
        "finding_id": _FINDING_ID,
    }

    with patch.dict("os.environ", {"VSA_CMS_API_ENDPOINT": _CMS_ENDPOINT}):
        result = executor.dispatch(action, _OPERATOR_CLAIMS)

    # The ambiguous-form path returns a terminal_error with this specific
    # error class. The MUTATION produces a dispatch attempt instead, which
    # either (a) ConnectionError's out because no responses mock is
    # registered or (b) succeeds with EXECUTED. Neither matches.
    assert result.status == ActionStatus.FAILED
    assert result.failure_kind == FailureKind.EXECUTOR_TERMINAL_ERROR
    assert result.last_error_class == "AmbiguousGroundingForm"
    # No HTTP call was attempted — `responses` logs every call.
    assert len(rsps.calls) == 0


# ---------------------------------------------------------------------------
# Pick 2: `_move_body` — invalid non-string required field returns None
# ---------------------------------------------------------------------------
#   file: agents/executors/cms.py:283 (BOOL_FLIP `or -> and`)
#   kind: BOOL_FLIP
#   mutation: `not isinstance(X,str) or not X.strip()` -> `and`
#
# A required field with an integer value should be refused by `_move_body`
# via the first clause (`not isinstance(X, str)`). Flipping `or` to `and`
# makes the guard require BOTH clauses: `not str AND not str.strip()` — the
# second clause raises `AttributeError` on an int (ints have no `.strip()`).
#
# The test asserts the function returns None cleanly for an integer field —
# a mutant that crashes with AttributeError fails pytest with a non-import
# cause, which is classified CAUGHT.
# ---------------------------------------------------------------------------


def test_move_body_rejects_integer_fleet_id_returns_none_not_raises():
    """Integer `fleet_id` must be refused via return-None, not crash.

    Pins `cms.py:283` `or` operator. Flipping to `and` turns the guard into
    `not str AND not strip()`, which raises `AttributeError` on an int.
    """
    from agents.executors.cms import _move_body

    action = {
        "action_id": f"AVX-ACT-{_ACTION_UUID}",
        "finding_id": _FINDING_ID,
        "parameters": {
            "fleet_id": 42,  # bad type — should be rejected
            "from_location_id": "LAX-1",
            "to_location_id": "SFO-2",
            "forecast_week": "2026-W01",
            "vehicle_vins": [_VIN],
            "transfer_cost_usd": 100.0,
            "predicted_on_rent_hours_gain": 10.0,
            "predicted_turnaways_avoided": 2.0,
        },
    }
    # Pass-through: no raise expected. Prompt 108: the call is wrapped so the `and`
    # mutant's AttributeError fails on this test's own assertion (unwrapped, the
    # junit-classified re-run read it as an incidental error, not a catch).
    try:
        result = _move_body(action)
    except Exception as exc:  # noqa: BLE001 - a raise is the defect under test
        raise AssertionError(
            f"_move_body must return None for an integer fleet_id, raised "
            f"{type(exc).__name__}: {exc}"
        ) from exc
    assert result is None


# ---------------------------------------------------------------------------
# Pick 3: `cost_action_parameter_error` — missing vehicle_vin is reported
# ---------------------------------------------------------------------------
#   file: agents/executors/cms.py:324
#   kind: EARLY_RETURN_REMOVE
#   mutation: `return "parameters.vehicle_vin is required ..."` -> `pass`
#
# Removing this early return lets the function fall through to later checks;
# a vin-less-but-otherwise-valid parameter set would return None (no error),
# which the AVX approve route would then accept — writing a cost action
# with no vin to CMS.
# ---------------------------------------------------------------------------


def test_cost_action_parameter_error_names_the_missing_vin():
    """A parameters dict with no vehicle_vin must return a vin-mentioning error.

    Pins `cms.py:324` early return. If removed, this call falls through to
    `cost_action` / `cause` / `month` checks (all valid in this fixture) and
    returns None — a bad action silently accepted.
    """
    from agents.executors.cms import cost_action_parameter_error

    # Everything else is valid — only vin is missing. Prompt 108: the earlier fixture
    # used an invalid cost_action and cause, so with the vin return removed the
    # function still returned an error (the cost_action one) and only the
    # "vehicle_vin" check caught the mutant. With valid values the mutant returns
    # None and both assertions catch it.
    parameters = {
        "cost_action": "schedule_inspection",
        "cause": "repeat_repair",
        "month": "2026-10",
    }
    err = cost_action_parameter_error(parameters)
    assert err is not None, "missing vin must produce an error, not fall through"
    assert "vehicle_vin" in err, (
        f"error must name the missing field 'vehicle_vin'; got: {err!r}"
    )


# ---------------------------------------------------------------------------
# Pick 4: `_dispatch_ground_vehicle` — non-transient exception → no retry
# ---------------------------------------------------------------------------
#   file: agents/executors/cms.py:628
#   kind: IF_FORCE_TRUE
#   mutation: `if is_transient_exception(exc):` -> `if True:`
#
# Forcing the transient check to always-true means non-transient exceptions
# (e.g., ValueError raised by a misbehaving requests mock) are retried as
# though they were network transients, instead of returning terminal_error
# immediately. The test asserts a non-transient exception produces exactly
# ONE attempt and a terminal error.
# ---------------------------------------------------------------------------


@rsps.activate
def test_cms_ground_vehicle_non_transient_exception_terminal_error_once():
    """Non-transient exception on first attempt → terminal_error, no retries.

    Pins `cms.py:628` guard. If forced True, the mutant retries the
    non-transient — asserted by retry_count==0 and only one responses call.
    """
    # ValueError is NOT in TRANSIENT_EXCEPTION_NAMES; a transient would be
    # ConnectTimeout / ConnectionError / ReadTimeout.
    rsps.add(
        rsps.POST,
        f"{_CMS_ENDPOINT}/api/v1/fleet-intelligence/groundings",
        body=ValueError("non-transient failure from a bad server response"),
    )

    executor = _make_cms_executor()
    action = {
        "action_id": f"AVX-ACT-{_ACTION_UUID}",
        "action_kind": "ground_vehicle",
        "target_system": "cms",
        "parameters": {
            "vehicle_vin": _VIN,
            "recall_id": _CAMPAIGN_ID,
        },
        "finding_id": _FINDING_ID,
    }

    with patch.dict("os.environ", {"VSA_CMS_API_ENDPOINT": _CMS_ENDPOINT}):
        result = executor.dispatch(action, _OPERATOR_CLAIMS)

    assert result.status == ActionStatus.FAILED
    assert result.failure_kind == FailureKind.EXECUTOR_TERMINAL_ERROR
    assert result.last_error_class == "ValueError"
    assert result.retry_count == 0, (
        f"non-transient exception must not retry (got retry_count={result.retry_count})"
    )
    assert len(rsps.calls) == 1


# ---------------------------------------------------------------------------
# Pick 5: `_move_attempts` — non-transient exception → no retry (moves)
# ---------------------------------------------------------------------------
#   file: agents/executors/cms.py:762
#   kind: IF_FORCE_TRUE
#   mutation: `if is_transient_exception(exc):` -> `if True:`
#
# Same shape as pick 4, on the moves dispatch path (which has its own copy
# of the retry loop).
# ---------------------------------------------------------------------------


@rsps.activate
def test_cms_move_vehicles_non_transient_exception_terminal_error_once():
    """Non-transient exception on moves → terminal_error, no retries.

    Pins `cms.py:762` guard. Mirrors pick 4 but exercises the moves path.
    """
    rsps.add(
        rsps.POST,
        f"{_CMS_ENDPOINT}/api/v1/fleet-intelligence/moves",
        body=ValueError("non-transient failure from a bad server response"),
    )

    executor = _make_cms_executor()
    action = {
        "action_id": f"AVX-ACT-{_ACTION_UUID}",
        "action_kind": "move_vehicles",
        "target_system": "cms",
        "finding_id": _FINDING_ID,
        "parameters": {
            "fleet_id": "fleet-meridian-01",
            "from_location_id": "LAX-1",
            "to_location_id": "SFO-2",
            "forecast_week": "2026-W01",
            "vehicle_vins": [_VIN],
            "transfer_cost_usd": 100.0,
            "predicted_on_rent_hours_gain": 10.0,
            "predicted_turnaways_avoided": 2.0,
        },
    }

    with patch.dict("os.environ", {"VSA_CMS_API_ENDPOINT": _CMS_ENDPOINT}):
        result = executor.dispatch(action, _OPERATOR_CLAIMS)

    assert result.status == ActionStatus.FAILED
    assert result.failure_kind == FailureKind.EXECUTOR_TERMINAL_ERROR
    assert result.last_error_class == "ValueError"
    assert result.retry_count == 0
    assert len(rsps.calls) == 1


# ---------------------------------------------------------------------------
# Pick 6: `_warranty_claim_body` — field-specific int-to-str coercion
# ---------------------------------------------------------------------------
#   file: agents/executors/dms.py:238
#   kind: IF_FORCE_TRUE
#   mutation:
#     `if field == "line_no" and isinstance(value, int) ...` -> `if True:`
#
# Only `line_no` is permitted as an int (DMS's own type) that gets coerced
# to str. Forcing the guard to True means any field with an int value gets
# coerced to a str — a non-line_no field receiving `42` would silently
# become `"42"` and pass as a valid string, instead of being refused.
# ---------------------------------------------------------------------------


def test_warranty_claim_body_rejects_integer_vin_without_coercion():
    """Only `line_no` may be an int; other fields with int values must fail.

    Pins `dms.py:238` field-equality guard. If forced True, an int vehicle_vin
    gets `str()`-coerced to a non-empty string and the body is returned.
    """
    from agents.executors.dms import _warranty_claim_body

    action = {
        "action_id": f"AVX-ACT-{_ACTION_UUID}",
        "action_kind": "submit_warranty_claim",
        "target_system": "dms",
        "finding_id": _FINDING_ID,
        "parameters": {
            # vehicle_vin is NOT line_no — passing an int here should be
            # refused, not coerced.
            "vehicle_vin": 42,
            "ro_id": _RO_ID,
            "line_no": _LINE_NO,
            "dealer_id": "dealer-seattle",
            "labor_op": "LO-EV-BRAKE-001",
            "fault_code": _FAULT_CODE,
            "notes": "fine print",
        },
    }
    assert _warranty_claim_body(action) is None, (
        "integer vehicle_vin must not be str-coerced into a valid body"
    )


# ---------------------------------------------------------------------------
# Pick 7: `_dispatch_warranty_claim` — non-transient exception → no retry
# ---------------------------------------------------------------------------
#   file: agents/executors/dms.py:681
#   kind: IF_FORCE_TRUE
#   mutation: `if is_transient_exception(exc):` -> `if True:`
#
# Same pattern as picks 4 and 5, on the warranty-claim dispatch path.
# ---------------------------------------------------------------------------


@rsps.activate
def test_dms_warranty_claim_non_transient_exception_terminal_error_once():
    """Non-transient exception on warranty claim → terminal_error, no retries.

    Pins `dms.py:681` guard. Mirrors picks 4-5 but exercises the DMS claim
    dispatch path.
    """
    rsps.add(
        rsps.POST,
        f"{_DMS_BASE}/api/dms/fleet/warranty-claims",
        body=ValueError("non-transient failure from a bad server response"),
    )

    executor = _make_dms_executor()
    action = {
        "action_id": f"AVX-ACT-{_ACTION_UUID}",
        "action_kind": "submit_warranty_claim",
        "target_system": "dms",
        "finding_id": _FINDING_ID,
        "parameters": {
            "vehicle_vin": _VIN,
            "ro_id": _RO_ID,
            "line_no": _LINE_NO,
            "dealer_id": "dealer-seattle",
            "labor_op": "LO-EV-BRAKE-001",
            "fault_code": _FAULT_CODE,
            "notes": "fine print",
        },
    }

    with patch.dict("os.environ", {"VSA_DMS_API_ENDPOINT": _DMS_BASE}):
        result = executor.dispatch(action, _OPERATOR_CLAIMS)

    assert result.status == ActionStatus.FAILED
    assert result.failure_kind == FailureKind.EXECUTOR_TERMINAL_ERROR
    assert result.last_error_class == "ValueError"
    assert result.retry_count == 0
    assert len(rsps.calls) == 1


# ---------------------------------------------------------------------------
# Pick 8: `_dispatch_move_vehicles` — null body refuses to call CMS
# ---------------------------------------------------------------------------
#   file: agents/executors/cms.py:692
#   kind: NONE_FLIP
#   mutation: `if body is None:` -> `if body is "":`
#
# `_move_body` returns None when parameters are invalid; the dispatch wrapper
# refuses to call CMS. Flipping the sentinel to `""` makes the guard never
# fire (a dict body `is "" ` is always False), so an invalid parameter set
# would reach `body["actionId"]` and raise TypeError on None.
#
# The test asserts the terminal_error path (MissingDispatchParameter) is
# taken when `_move_body` returns None — invariant across all mutations.
# ---------------------------------------------------------------------------


def test_dispatch_move_vehicles_refuses_null_body_with_specific_terminal_error():
    """When `_move_body` returns None, the dispatch returns MissingDispatchParameter.

    Pins `cms.py:692` None sentinel. Mutating `None` to `""` makes the guard
    silently permit an invalid payload to reach the signed request builder.
    """
    executor = _make_cms_executor()
    # Deliberately malformed: missing most of the fields `_move_body` requires.
    action = {
        "action_id": f"AVX-ACT-{_ACTION_UUID}",
        "action_kind": "move_vehicles",
        "target_system": "cms",
        "finding_id": _FINDING_ID,
        "parameters": {},  # none of the required fields
    }

    with patch.dict("os.environ", {"VSA_CMS_API_ENDPOINT": _CMS_ENDPOINT}):
        result = executor.dispatch(action, _OPERATOR_CLAIMS)

    assert result.status == ActionStatus.FAILED
    assert result.failure_kind == FailureKind.EXECUTOR_TERMINAL_ERROR
    assert result.last_error_class == "MissingDispatchParameter"


# ---------------------------------------------------------------------------
# Pick 9: `_dispatch_move_vehicles_signed` — same guard, signed path
# ---------------------------------------------------------------------------
#   file: agents/executors/cms.py:720
#   kind: NONE_FLIP
#   mutation: `if body is None:` -> `if body is "":`
#
# The SigV4-signed path (rule-decided moves) carries its own copy of the
# guard. A regression on the signed path alone would otherwise fire only on
# the rule-approved slice of production traffic.
# ---------------------------------------------------------------------------


def test_dispatch_move_vehicles_signed_refuses_null_body_with_specific_terminal_error():
    """The SigV4-signed move path refuses null bodies with the same terminal.

    Pins `cms.py:720` None sentinel, which lives in the stage-E signed path
    (`decided_by.kind == "rule"`).
    """
    from agents.executors.cms import CmsExecutor

    # Prompt 108: credentials are injected as in the other signed-path tests, so a
    # mutant that gets past the body guard never reaches the real credential chain.
    executor = CmsExecutor(
        sleep_fn=lambda _: None,
        retry_policy=RetryPolicy(retry_count=3, backoff_base_ms=0, backoff_cap_ms=0),
        credentials_fn=MagicMock,
    )
    rule_action = {
        "action_id": f"AVX-ACT-{_ACTION_UUID}",
        "action_kind": "move_vehicles",
        "target_system": "cms",
        "finding_id": _FINDING_ID,
        "decided_by": {"kind": "rule", "id": "fleet-meridian-01/rule-001"},
        "parameters": {},  # malformed
    }

    with patch.dict(
        "os.environ",
        {
            "VSA_CMS_API_ENDPOINT": _CMS_ENDPOINT,
            # Prompt 108: the variable the signed path reads (this line used to name a
            # non-existent VSA_CMS_INTERNAL_API_ENDPOINT, so the endpoint was never
            # set and a mutant reached a MissingCmsInternalEndpoint error instead).
            "VSA_CMS_FI_INTERNAL_API_ENDPOINT":
                "https://yourapi000.execute-api.us-west-2.amazonaws.com/prod",
            "VSA_STAGE": "test",
        },
    ):
        # Rule-approved moves use `dispatch` with no caller token
        # (claims pass through the registry's rule branch); the dispatch
        # routes to `_dispatch_move_vehicles_signed`.
        result = executor.dispatch(rule_action, _OPERATOR_CLAIMS)

    assert result.status == ActionStatus.FAILED
    assert result.failure_kind == FailureKind.EXECUTOR_TERMINAL_ERROR
    assert result.last_error_class == "MissingDispatchParameter"
