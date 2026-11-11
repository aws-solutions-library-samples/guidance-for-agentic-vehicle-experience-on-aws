"""Auto-approver mutants that survived prompt 100's sweep and prompt 108's re-run.

See ``agents/executors/tests/test_mutation_catches_p108.py`` for the framing. These
pin the evaluator's fail-closed and idempotency properties:

* A replayed decision (the conditional put fails) is a counted no-op: not a batch
  failure, not a second dispatch; any other put failure is a batch failure. The existing replay test counted dispatches only,
  so a mutant that re-raised the ConditionalCheckFailed, or that misread its error
  code, turned every replay into a failure the shard retries, and survived.
* The run record counts exactly the written and the duplicate outcomes.
* A malformed Finding image is reported as a batch failure, never decided.
* The policy table's ``auto_approvable`` double-check is enforced.
* A non-string VIN makes a move ineligible; it doesn't raise (a raise is a
  batch failure the shard retries forever).
* The Action update writes ``target_ref`` only when the result carries one: the
  field keys a string index, so a NULL makes DynamoDB refuse the whole update.
"""
from __future__ import annotations

import dataclasses
import os
import sys
from typing import Any, Optional
from unittest.mock import MagicMock, patch

import pytest

# ── sys.path setup (follows test_handler.py convention) ────────────────────
_LAMBDA_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
_LAMBDAS_ROOT = os.path.dirname(_LAMBDA_ROOT)
_REPO_ROOT = os.path.abspath(os.path.join(_LAMBDA_ROOT, "..", ".."))
for _p in (_LAMBDA_ROOT, _LAMBDAS_ROOT, _REPO_ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

_FLEET = "fleet-mrdn-01"
_RULES = [{"rule_id": "rule-001", "fleet_id": _FLEET, "action_kind": "acknowledge_only",
           "enabled": True, "limit": None}]


@pytest.fixture(autouse=True)
def _tables(monkeypatch):
    """The three table names the evaluator requires (it fails closed without them)."""
    monkeypatch.setenv("AVX_ACTIONS_TABLE", "vsa-test-avx-actions")
    monkeypatch.setenv("AVX_RULES_TABLE", "vsa-test-avx-approval-rules")
    monkeypatch.setenv("AVX_AGENT_RUNS_TABLE", "vsa-test-avx-agent-runs")
    monkeypatch.delenv("FI_AUTO_MOVES_ENABLED", raising=False)


def _image(finding_id: str, *, fleet_id: Optional[str] = _FLEET) -> dict[str, Any]:
    image: dict[str, Any] = {
        "finding_id": {"S": finding_id},
        "finding_kind": {"S": "health.tires"},
        "recommended_action": {"M": {
            "action_kind": {"S": "acknowledge_only"},
            "target_system": {"S": "none"},
            "parameters": {"M": {}},
        }},
        "expires_at": {"S": "2099-12-31T00:00:00Z"},
        "computed_at": {"S": "2026-10-24T02:00:00Z"},
    }
    if fleet_id is not None:
        image["fleet_id"] = {"S": fleet_id}
        image["scope"] = {"M": {"fleet_id": {"S": fleet_id}}}
    return image


def _record(seq: Optional[str], image: dict[str, Any]) -> dict[str, Any]:
    dynamodb: dict[str, Any] = {"NewImage": image}
    if seq is not None:
        dynamodb["SequenceNumber"] = seq
    return {"eventName": "INSERT", "dynamodb": dynamodb}


def _put_error(code: str) -> Exception:
    from botocore.exceptions import ClientError

    return ClientError({"Error": {"Code": code, "Message": "p108"}}, "PutItem")


def _run(records: list, *, existing: frozenset = frozenset(),
         failing: frozenset = frozenset(), target_ref: Optional[str] = None,
         updates: Optional[list] = None) -> tuple[dict, list, list, list]:
    """Run the handler over ``records`` with fake tables.

    ``existing`` holds action ids the actions table already has (their conditional
    put fails); a put for an id in ``failing`` fails with a throttle. Every dispatch
    returns ``completed`` with ``target_ref``; each ``update_item`` call's kwargs go to
    ``updates`` when given. Returns (handler result, ids put, ids dispatched, run
    records).
    """
    import handler as h  # noqa: PLC0415
    from agents.executors.base import ActionStatus, ExecuteResult  # noqa: PLC0415

    puts: list = []
    dispatched: list = []
    runs: list = []

    def _put(**kwargs: Any) -> dict:
        action_id = kwargs["Item"]["action_id"]
        if action_id in existing:
            raise _put_error("ConditionalCheckFailedException")
        if action_id in failing:
            raise _put_error("ProvisionedThroughputExceededException")
        puts.append(action_id)
        return {}

    def _dispatch(action: dict, _claims: dict, **_kw: Any) -> ExecuteResult:
        dispatched.append(action["action_id"])
        return ExecuteResult.success(target_ref=target_ref, status=ActionStatus.COMPLETED)

    table = MagicMock()
    table.put_item.side_effect = _put
    if updates is not None:
        table.update_item.side_effect = lambda **kw: updates.append(kw) or {}
    with patch.object(h, "_get_ddb", return_value=MagicMock(Table=lambda _name: table)), \
         patch.object(h, "_get_rules_for_fleet_kind", return_value=list(_RULES)), \
         patch.object(h, "_write_run_record", side_effect=lambda **kw: runs.append(kw)), \
         patch("agents.executors.registry.dispatch", side_effect=_dispatch):
        result = h.handler({"Records": records}, None)
    return result, puts, dispatched, runs


def test_replays_are_counted_duplicates_and_only_decisions_are_counted():
    """One new decision, two replays, one throttled put, one Finding with no fleet, one
    malformed record with no sequence number. Replays are counted no-ops (no failure, no
    dispatch); a put that fails for any other reason is a batch failure (retried, not
    swallowed as a duplicate); the run record counts 1 written and 2 duplicates; nothing
    else is counted or reported. Pins the CCF branch and its re-raise (handler ~430-433),
    the handler's counters (~536-538) and ``if seq:`` (~545)."""
    import handler as h  # noqa: PLC0415

    existing = frozenset({h.derive_action_id("FIND-p108-dup1", "", ""),
                          h.derive_action_id("FIND-p108-dup2", "", "")})
    failing = frozenset({h.derive_action_id("FIND-p108-throttled", "", "")})
    new_id = h.derive_action_id("FIND-p108-new1", "", "")
    records = [
        _record("seq-1", _image("FIND-p108-new1")),
        _record("seq-2", _image("FIND-p108-dup1")),
        _record("seq-3", _image("FIND-p108-dup2")),
        _record("seq-4", _image("FIND-p108-nofleet", fleet_id=None)),
        _record(None, {}),
        _record("seq-6", _image("FIND-p108-throttled")),
    ]
    result, puts, dispatched, runs = _run(records, existing=existing, failing=failing)

    assert result == {"batchItemFailures": [{"itemIdentifier": "seq-6"}]}, result
    assert puts == [new_id]
    assert dispatched == [new_id], "a replay must not dispatch"
    assert len(runs) == 1, runs
    counts = (runs[0].get("artifacts_written"), runs[0].get("duplicates_skipped"))
    assert counts == (1, 2), f"(written, duplicates) = {counts}"


@pytest.mark.parametrize("label, image", [
    ("empty_finding_id", _image("")),
    ("empty_new_image", {}),
])
def test_a_malformed_image_is_reported_and_never_decided(label, image):
    """An INSERT whose image is empty, or whose finding_id is blank, is a batch
    failure: nothing is written or dispatched (pins ~318 and ~333)."""
    result, puts, dispatched, _runs = _run([_record("seq-bad", image)])

    assert result == {"batchItemFailures": [{"itemIdentifier": "seq-bad"}]}, f"{label}: {result}"
    assert puts == [] and dispatched == [], f"{label}: puts={puts} dispatched={dispatched}"


def test_a_kind_the_policy_table_does_not_mark_auto_approvable_is_skipped():
    """The evaluator re-checks ``ACTION_KINDS[kind].auto_approvable`` (defence in
    depth). With the row switched off, an enabled rule decides nothing (pins ~350)."""
    from agents.executors import policy  # noqa: PLC0415

    off = dataclasses.replace(policy.ACTION_KINDS["acknowledge_only"], auto_approvable=False)
    with patch.dict(policy.ACTION_KINDS, {"acknowledge_only": off}):
        result, puts, dispatched, _runs = _run([_record("seq-1", _image("FIND-p108-off1"))])

    assert result == {"batchItemFailures": []}, result
    assert puts == [] and dispatched == [], f"puts={puts} dispatched={dispatched}"


@pytest.mark.parametrize("target_ref", [None, "", "TR-P108-1"])
def test_the_action_update_sets_target_ref_only_when_the_result_has_one(target_ref):
    """``target_ref`` keys the ``by_target_ref`` index (a string), so writing it as
    NULL or empty makes DynamoDB reject the whole update; the handler logs that and
    moves on, and the Action would stay ``pending_execution`` with no audit entry.
    Every auto-approved ``acknowledge_only`` has no target_ref. Pins
    ``if getattr(result, "target_ref", None):`` (~454)."""
    import handler as h  # noqa: PLC0415

    updates: list = []
    action_id = h.derive_action_id("FIND-p108-ref1", "", "")
    result, puts, dispatched, _runs = _run([_record("seq-1", _image("FIND-p108-ref1"))],
                                           target_ref=target_ref, updates=updates)

    assert result == {"batchItemFailures": []}, result
    assert dispatched == [action_id], dispatched
    assert len(updates) == 1, f"expected one Action update, got {len(updates)}"
    update = updates[0]
    assert update["Key"] == {"action_id": action_id}
    assert update["ExpressionAttributeValues"][":to"] == "completed", update
    if target_ref:
        assert ", #r = :ref" in update["UpdateExpression"], update["UpdateExpression"]
        assert update["ExpressionAttributeValues"][":ref"] == target_ref
    else:
        assert ":ref" not in update["UpdateExpression"], update["UpdateExpression"]
        assert ":ref" not in update["ExpressionAttributeValues"], update["ExpressionAttributeValues"]


def test_move_within_bounds_treats_a_non_string_vin_as_ineligible():
    """A non-string VIN makes the move ineligible; it must not raise, because a raise
    is a batch failure the shard retries (pins ~129)."""
    import handler as h  # noqa: PLC0415

    parameters = {
        "fleet_id": _FLEET,
        "from_location_id": "LAX-1",
        "to_location_id": "SFO-2",
        "forecast_week": "2026-W44",
        "vehicle_vins": [12345678901234567],
        "transfer_cost_usd": 120.0,
        "predicted_on_rent_hours_gain": 10.0,
        "predicted_turnaways_avoided": 2.0,
    }
    bounds = {"max_limit_usd": 10000.0, "max_vehicles_per_auto_move": 5}
    try:
        cost = h._move_within_bounds(_FLEET, parameters, bounds)
    except Exception as exc:  # noqa: BLE001 - a raise is the defect under test
        raise AssertionError(f"_move_within_bounds raised {type(exc).__name__}: {exc}") from exc
    assert cost is None, f"a non-string VIN must make the move ineligible; got {cost!r}"
