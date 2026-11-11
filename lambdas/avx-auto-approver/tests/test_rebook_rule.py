"""Auto-approver rebook predicate (spec D7 / G8-AVX-1).

Moto-backed end-to-end: the handler reads the new Finding from the stream, verifies
the rebooking predicate against real moto tables (findings, actions, rules, groundings),
writes the Action, and calls registry.dispatch.

Structure mirrors test_move_rules.py: ``_World`` owns all moto state; each test
case mutates exactly one predicate condition.

Control: every condition true → one Action written, dispatch called once.
Negative: each condition failing alone → no Action, dispatch not called.
"""
from __future__ import annotations

import hashlib
import importlib.util
import os
import sys
from decimal import Decimal
from unittest.mock import patch

import boto3
import pytest
from moto import mock_aws

_HERE = os.path.dirname(os.path.abspath(__file__))
_LAMBDA_ROOT = os.path.dirname(_HERE)
_LAMBDAS_ROOT = os.path.dirname(_LAMBDA_ROOT)
_REPO_ROOT = os.path.dirname(_LAMBDAS_ROOT)
for _p in (_LAMBDAS_ROOT, _REPO_ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
_FLEET = "fleet-test-rebook-01"
_VIN = "1HGBH41JXMN109186"      # 17-char
_VIN2 = "2T1BURHE0JC043821"      # different VIN
_NEED_ID = "need-P0AA6-srv2"
_DEALER = "D-rebook-77"
_RULE_ID_STR = "rebook-daily"
_RULE_ID_FULL = f"{_FLEET}/{_RULE_ID_STR}"
_OP_ID = "op-sub-prev-00001"
_PREV_FINDING_ID = "FIND-prev-rebook-0000-0000-000000000001"
_NEW_FINDING_ID = "FIND-new-rebook-0000-0000-000000000002"
_GROUNDING_ID = "GRD-001"
_GROUNDING_ACTION = "ACT-grounding-001"
_GROUNDING_FINDING = "FIND-grounding-001"

_T = {
    "actions":   "vsa-test-rb-avx-actions",
    "rules":     "vsa-test-rb-avx-approval-rules",
    "runs":      "vsa-test-rb-avx-agent-runs",
    "findings":  "vsa-test-rb-avx-findings",
    "groundings":"vsa-test-rb-cms-groundings",
}
_LABOR_OP = "LOP-DIAG-P0AA6"    # labor_op required by server_set.py needs validation


def _load_handler():
    spec = importlib.util.spec_from_file_location(
        "auto_approver_handler_rb", os.path.join(_LAMBDA_ROOT, "handler.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _typed(value):
    from boto3.dynamodb.types import TypeSerializer
    return TypeSerializer().serialize(value)


def _prev_action_id(prev_fid: str) -> str:
    """Derive the action id for a prev finding (same formula as handler.derive_action_id)."""
    raw = hashlib.sha256(f"{prev_fid}|fi-decision".encode()).hexdigest()[:32]
    return "AVX-ACT-" + raw[0:8] + "-" + raw[8:12] + "-" + raw[12:16] + "-" + raw[16:20] + "-" + raw[20:32]


def _stream_record(*, fleet=_FLEET, vin=_VIN, finding_id=_NEW_FINDING_ID,
                   agent_id="agent-fi-fleet-health", subject_key="service/2",
                   need_ids=None, param_need_ids=None, dealer_id=_DEALER,
                   target_system="dms",
                   seq="seq-rb-1"):
    """Build a DynamoDB Streams INSERT record for the new rebook Finding.

    ``need_ids`` governs evidence health.need entries.  ``param_need_ids``
    governs recommended_action.parameters.needs; defaults to ``need_ids`` when
    absent (so the two match unless the caller wants to test the deferred-need
    case introduced by W1).
    """
    if need_ids is None:
        need_ids = [_NEED_ID]
    if param_need_ids is None:
        param_need_ids = need_ids
    evidence = [
        {"kind": "health.need", "value": {"need_id": nid, "code": "P0AA6"}}
        for nid in need_ids
    ]
    params: dict = {
        "vehicle_vin": vin,
        "urgency": "this_week",
        "needs": [{"kind": "fault", "code": "P0AA6", "need_id": nid,
                   "labor_op": _LABOR_OP}
                  for nid in param_need_ids],
    }
    if dealer_id is not None:
        params["dealer_id"] = dealer_id
    image = {
        "finding_id": finding_id,
        "agent_id": agent_id,
        "fleet_id": fleet,
        "vin": vin,
        "subject_key": subject_key,
        "scope": {"vin": vin},          # required by check_server_set_params (W2)
        "recommended_action": {
            "action_kind": "book_fleet_service",
            "target_system": target_system,
            "parameters": params,
        },
        "evidence": evidence,
        "expires_at": "2099-12-31T00:00:00Z",
        "computed_at": "2026-11-02T06:00:00Z",
    }
    return {
        "eventName": "INSERT",
        "dynamodb": {
            "SequenceNumber": seq,
            "NewImage": {k: _typed(v) for k, v in image.items()},
        },
    }


class _World:
    """Owns all moto state for a single test."""

    def __init__(self, mod, ddb):
        self.mod = mod
        self.ddb = ddb
        self.dispatched: list = []

    # --- helpers to seed tables ------------------------------------------------

    def rule(self, rule_id=_RULE_ID_STR, fleet=_FLEET, enabled=True, **over):
        row = {
            "fleet_id": fleet,
            "rule_id": rule_id,
            "action_kind": "book_fleet_service",
            "enabled": enabled,
            "created_by": "op",
            "updated_at": "2026-11-01T10:00:00Z",
        }
        row.update(over)
        self.ddb.Table(_T["rules"]).put_item(Item=row)

    def prev_finding(self, finding_id=_PREV_FINDING_ID, vin=_VIN,
                     fleet=_FLEET, subject_key="service/1",
                     computed_at="2026-11-01T06:00:00Z"):
        item = {
            "finding_id": finding_id,
            "agent_id": "agent-fi-fleet-health",
            "fleet_id": fleet,
            "vin": vin,
            "subject_key": subject_key,
        }
        if computed_at is not None:
            item["computed_at"] = computed_at
        self.ddb.Table(_T["findings"]).put_item(Item=item)

    def prev_action(self, finding_id=_PREV_FINDING_ID, vin=_VIN,
                    fleet=_FLEET, status="cancelled", need_ids=None,
                    decided_by=None):
        if need_ids is None:
            need_ids = [_NEED_ID]
        if decided_by is None:
            decided_by = {"kind": "operator", "id": _OP_ID}
        action_id = _prev_action_id(finding_id)
        self.ddb.Table(_T["actions"]).put_item(Item={
            "action_id": action_id,
            "finding_id": finding_id,
            "action_kind": "book_fleet_service",
            "target_system": "dms",
            "status": status,
            "fleet_id": fleet,
            "parameters": {
                "vehicle_vin": vin,
                "dealer_id": _DEALER,
                "needs": [{"kind": "fault", "code": "P0AA6", "need_id": nid}
                           for nid in need_ids],
            },
            "decided_by": decided_by,
            "created_at": "2026-11-01T10:00:00Z",
            "updated_at": "2026-11-01T10:00:00Z",
        })

    def grounding(self, vin=_VIN, fleet=_FLEET, reason="fault",
                  finding_id=_GROUNDING_FINDING, action_id=_GROUNDING_ACTION,
                  vehicle_id=_GROUNDING_ID):
        row: dict = {
            "vehicleId": vehicle_id,
            "actionId": action_id,
            "openFleetId": fleet,
            "vin": vin,
            "reason": reason,
            "groundedAt": "2026-11-01T08:00:00Z",
        }
        if finding_id:
            row["findingId"] = finding_id
        self.ddb.Table(_T["groundings"]).put_item(Item=row)

    # --- runner ----------------------------------------------------------------

    def run(self, *records):
        from agents.executors.base import ActionStatus, ExecuteResult

        def _fake_dispatch(action, claims, **kw):
            self.dispatched.append((action, claims))
            return ExecuteResult.success(
                target_ref=action["action_id"], status=ActionStatus.EXECUTED)

        with patch.object(self.mod, "_get_ddb", return_value=self.ddb), \
             patch("agents.executors.registry.dispatch", side_effect=_fake_dispatch):
            return self.mod.handler({"Records": list(records)}, None)

    def actions(self) -> list:
        return self.ddb.Table(_T["actions"]).scan(ConsistentRead=True)["Items"]

    def assert_approved(self, result) -> dict:
        """Assert a new Action was written with rule decider for the new finding; dispatch called once."""
        assert result["batchItemFailures"] == []
        new_action_id = _prev_action_id(_NEW_FINDING_ID)
        acts = [a for a in self.actions() if a.get("action_id") == new_action_id]
        assert len(acts) == 1, f"expected 1 new action for new finding, got {self.actions()}"
        a = acts[0]
        assert a["decided_by"] == {"kind": "rule", "id": _RULE_ID_FULL}
        assert a["action_kind"] == "book_fleet_service"
        assert len(self.dispatched) == 1
        return a

    def assert_not_approved(self, result, *, prev_action_present=True) -> None:
        """Assert no new Action written (beyond the pre-seeded prev one) and no dispatch."""
        assert result["batchItemFailures"] == []
        # The prev action may or may not be in the table; we just check dispatch.
        assert self.dispatched == [], "dispatch must not be called"
        # Only pre-seeded prev action (if any) in the table — no new action_id matching
        # the new finding.
        new_action_id = _prev_action_id(_NEW_FINDING_ID)
        items = self.actions()
        new_acts = [a for a in items if a.get("action_id") == new_action_id]
        assert new_acts == [], f"unexpected new action written: {new_acts}"


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------

def _create_tables(ddb) -> None:
    # actions: finding_id → action_id for prev_action; action_id is hash key
    ddb.create_table(
        TableName=_T["actions"],
        KeySchema=[{"AttributeName": "action_id", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "action_id", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )
    # rules: fleet_id (HASH) + rule_id (RANGE)
    ddb.create_table(
        TableName=_T["rules"],
        KeySchema=[
            {"AttributeName": "fleet_id", "KeyType": "HASH"},
            {"AttributeName": "rule_id", "KeyType": "RANGE"},
        ],
        AttributeDefinitions=[
            {"AttributeName": "fleet_id", "AttributeType": "S"},
            {"AttributeName": "rule_id", "AttributeType": "S"},
        ],
        BillingMode="PAY_PER_REQUEST",
    )
    # runs
    ddb.create_table(
        TableName=_T["runs"],
        KeySchema=[
            {"AttributeName": "agent_component", "KeyType": "HASH"},
            {"AttributeName": "started_at", "KeyType": "RANGE"},
        ],
        AttributeDefinitions=[
            {"AttributeName": "agent_component", "AttributeType": "S"},
            {"AttributeName": "started_at", "AttributeType": "S"},
        ],
        BillingMode="PAY_PER_REQUEST",
    )
    # findings: by_vin GSI (vin → hash key only, projection ALL)
    ddb.create_table(
        TableName=_T["findings"],
        KeySchema=[{"AttributeName": "finding_id", "KeyType": "HASH"}],
        AttributeDefinitions=[
            {"AttributeName": "finding_id", "AttributeType": "S"},
            {"AttributeName": "vin", "AttributeType": "S"},
        ],
        GlobalSecondaryIndexes=[{
            "IndexName": "by_vin",
            "KeySchema": [{"AttributeName": "vin", "KeyType": "HASH"}],
            "Projection": {"ProjectionType": "ALL"},
        }],
        BillingMode="PAY_PER_REQUEST",
    )
    # groundings (CMS): vehicleId (HASH) + actionId (RANGE);
    # open-groundings-index: openFleetId (HASH) + groundedAt (RANGE)
    ddb.create_table(
        TableName=_T["groundings"],
        KeySchema=[
            {"AttributeName": "vehicleId", "KeyType": "HASH"},
            {"AttributeName": "actionId", "KeyType": "RANGE"},
        ],
        AttributeDefinitions=[
            {"AttributeName": "vehicleId", "AttributeType": "S"},
            {"AttributeName": "actionId", "AttributeType": "S"},
            {"AttributeName": "openFleetId", "AttributeType": "S"},
            {"AttributeName": "groundedAt", "AttributeType": "S"},
        ],
        GlobalSecondaryIndexes=[{
            "IndexName": "open-groundings-index",
            "KeySchema": [
                {"AttributeName": "openFleetId", "KeyType": "HASH"},
                {"AttributeName": "groundedAt", "KeyType": "RANGE"},
            ],
            "Projection": {"ProjectionType": "ALL"},
        }],
        BillingMode="PAY_PER_REQUEST",
    )


#: The simulated fleet operator's parameter, as staging holds it (hold list naming
#: another car), so every control below passes against a non-empty hold list.
_HOLD_PARAM = "/cvx/test/fi/sim-operator"
_HELD_OTHER = "MRDN0000000000015"
_HOLD_DEFAULT = {
    "daily_cap": 20,
    "fleets": {"demo-city-public": {"enabled": True}},
    "hold_vins": [_HELD_OTHER],
    "hold_vehicle_ids": [_HELD_OTHER],
}


def _put_holds(value) -> None:
    """Write the hold-list parameter (a dict is JSON-encoded; a str is stored as is)."""
    import json as _json
    boto3.client("ssm", region_name="us-west-2").put_parameter(
        Name=_HOLD_PARAM, Type="String", Overwrite=True,
        Value=value if isinstance(value, str) else _json.dumps(value))


@pytest.fixture()
def w(monkeypatch):
    monkeypatch.setenv("AVX_ACTIONS_TABLE", _T["actions"])
    monkeypatch.setenv("AVX_RULES_TABLE", _T["rules"])
    monkeypatch.setenv("AVX_AGENT_RUNS_TABLE", _T["runs"])
    monkeypatch.setenv("AVX_FINDINGS_TABLE", _T["findings"])
    monkeypatch.setenv("CMS_GROUNDINGS_TABLE", _T["groundings"])
    monkeypatch.setenv("FI_AUTO_REBOOK_ENABLED", "true")
    monkeypatch.setenv("FI_SIM_OPERATOR_PARAM", _HOLD_PARAM)
    with mock_aws():
        ddb = boto3.resource("dynamodb", region_name="us-west-2")
        _create_tables(ddb)
        _put_holds(_HOLD_DEFAULT)
        world = _World(_load_handler(), ddb)
        # Seed the standard "happy path" state; individual tests mutate as needed.
        world.rule()
        world.prev_finding()
        world.prev_action()
        world.grounding()
        yield world


# ---------------------------------------------------------------------------
# Control: every condition true → approved
# ---------------------------------------------------------------------------

def test_control_all_conditions_true_action_written(w):
    """Must-succeed control: one Action with rule decider, dispatch called once."""
    result = w.run(_stream_record())
    w.assert_approved(result)


# ---------------------------------------------------------------------------
# Condition (a): agent_id, target_system, subject_key, n >= 2
# ---------------------------------------------------------------------------

def test_n_equals_1_not_approved(w):
    """subject_key service/1 → n=1 < 2 → no Action even with service/0 prev finding seeded."""
    # Seed a prev finding at service/0 so condition (c) would pass if n<2 were not checked
    w.ddb.Table(_T["findings"]).put_item(Item={
        "finding_id": "FIND-prev-s0-rebook-00000000001",
        "agent_id": "agent-fi-fleet-health",
        "fleet_id": _FLEET,
        "vin": _VIN,
        "subject_key": "service/0",
        "computed_at": "2026-10-31T06:00:00Z",
    })
    # Seed prev action for service/0 finding (cancelled, valid decider)
    s0_action_id = _prev_action_id("FIND-prev-s0-rebook-00000000001")
    w.ddb.Table(_T["actions"]).put_item(Item={
        "action_id": s0_action_id,
        "finding_id": "FIND-prev-s0-rebook-00000000001",
        "action_kind": "book_fleet_service",
        "target_system": "dms",
        "status": "cancelled",
        "fleet_id": _FLEET,
        "parameters": {
            "vehicle_vin": _VIN,
            "dealer_id": _DEALER,
            "needs": [{"kind": "fault", "code": "P0AA6", "need_id": _NEED_ID}],
        },
        "decided_by": {"kind": "operator", "id": _OP_ID},
        "created_at": "2026-10-31T10:00:00Z",
        "updated_at": "2026-10-31T10:00:00Z",
    })
    result = w.run(_stream_record(subject_key="service/1"))
    w.assert_not_approved(result)


def test_subject_key_service_0_not_approved(w):
    """subject_key service/0 → regex [1-9]\\d* does not match → no Action."""
    result = w.run(_stream_record(subject_key="service/0"))
    w.assert_not_approved(result)


def test_subject_key_leading_zero_not_approved(w):
    """subject_key service/02 → regex does not match leading zero → no Action."""
    result = w.run(_stream_record(subject_key="service/02"))
    w.assert_not_approved(result)


def test_wrong_agent_id_not_approved(w):
    """agent_id != agent-fi-fleet-health → no Action."""
    result = w.run(_stream_record(agent_id="agent-fi-recall-warranty"))
    w.assert_not_approved(result)


# ---------------------------------------------------------------------------
# Condition (b): VIN
# ---------------------------------------------------------------------------
# 17-char check covered by the gate; if VIN is wrong length the predicate returns False.

# ---------------------------------------------------------------------------
# Condition (c): the latest previous Finding (prompt 361)
# ---------------------------------------------------------------------------

def test_previous_finding_missing_not_approved(w):
    """No finding at service/1 for this VIN → no Action."""
    # Remove the pre-seeded prev finding
    w.ddb.Table(_T["findings"]).delete_item(
        Key={"finding_id": _PREV_FINDING_ID})
    result = w.run(_stream_record())
    w.assert_not_approved(result)


def test_latest_previous_finding_undecided_not_approved(w):
    """A later service/1 with no decision is the attempt the loop follows, so the older
    cancelled booking can't be rebooked through it (prompt 361: was 'two findings → ambiguous')."""
    w.prev_finding(finding_id="FIND-prev-rebook-dup-000000000001",
                   computed_at="2026-11-01T06:10:00Z")
    result = w.run(_stream_record())
    w.assert_not_approved(result)


# Prompt 359 / 361: after an undecided Finding expires, the fleet-health loop writes a new
# one with the same finding_key and a new finding_id, and the old one stays (ttl_exempt).
# 5 of the 7 pending demo-fleet p0 cars on 2026-11-08 carried 2-3 service/1 Findings.
_OLDER_SIBLINGS = (
    ("FIND-prev-rebook-old-0000-0000-000000000003", "2026-10-25T05:40:11Z"),
    ("FIND-prev-rebook-old-0000-0000-000000000004", "2026-10-29T05:40:11Z"),
)


def test_one_older_expired_sibling_does_not_block_the_rebook(w):
    """Red on main before prompt 361: an older undecided service/1 made (c) count two."""
    w.prev_finding(finding_id=_OLDER_SIBLINGS[0][0], computed_at=_OLDER_SIBLINGS[0][1])
    result = w.run(_stream_record())
    a = w.assert_approved(result)
    assert a["finding_id"] == _NEW_FINDING_ID


def test_two_older_expired_siblings_do_not_block_the_rebook(w):
    """Three service/1 Findings, the shape of the 11-08 demo-fleet cars: the latest decides."""
    for fid, at in _OLDER_SIBLINGS:
        w.prev_finding(finding_id=fid, computed_at=at)
    result = w.run(_stream_record())
    w.assert_approved(result)


def test_older_sibling_cancelled_latest_pending_not_approved(w):
    """The cancelled booking is on an OLDER service/1; the latest one is undecided, so the
    rule doesn't follow the cancelled one (no earliest-first or any-cancelled reading)."""
    w.ddb.Table(_T["findings"]).delete_item(Key={"finding_id": _PREV_FINDING_ID})
    w.ddb.Table(_T["actions"]).delete_item(Key={"action_id": _prev_action_id(_PREV_FINDING_ID)})
    old_fid = _OLDER_SIBLINGS[0][0]
    w.prev_finding(finding_id=old_fid, computed_at=_OLDER_SIBLINGS[0][1])
    w.prev_action(finding_id=old_fid)
    w.prev_finding(finding_id="FIND-prev-rebook-new-0000-0000-000000000005",
                   computed_at="2026-11-01T06:00:00Z")
    result = w.run(_stream_record())
    w.assert_not_approved(result)


def test_tie_on_the_latest_computed_at_not_approved(w):
    """Two service/1 Findings at the same latest computed_at, both with a cancelled operator
    booking: the loop keeps whichever it read first, so the rule fails closed."""
    twin = "FIND-prev-rebook-twin-0000-0000-000000000006"
    w.prev_finding(finding_id=twin, computed_at="2026-11-01T06:00:00Z")
    w.prev_action(finding_id=twin)
    result = w.run(_stream_record())
    w.assert_not_approved(result)


def test_previous_finding_without_computed_at_not_approved(w):
    """The only service/1 has no computed_at (malformed): fail closed."""
    w.ddb.Table(_T["findings"]).delete_item(Key={"finding_id": _PREV_FINDING_ID})
    w.prev_finding(computed_at=None)
    result = w.run(_stream_record())
    w.assert_not_approved(result)


def test_a_stampless_sibling_beside_the_booked_finding_not_approved(w):
    """Any service/1 candidate without a computed_at fails closed, even beside a stamped,
    cancelled one (review p361 c1 S1: the documented rule, not "skip the stampless one")."""
    w.prev_finding(finding_id="FIND-prev-rebook-nostamp-0000-000000000008", computed_at=None)
    result = w.run(_stream_record())
    w.assert_not_approved(result)


def test_latest_previous_finding_in_another_fleet_not_approved(w):
    """The latest service/1 for the VIN belongs to another fleet (its Action even names
    this fleet, inconsistent data): the rule doesn't fall back to this fleet's older one."""
    other = "FIND-prev-rebook-otherfleet-0000-000000000007"
    w.prev_finding(finding_id=other, fleet="fleet-other-99", computed_at="2026-11-01T07:00:00Z")
    w.prev_action(finding_id=other)
    result = w.run(_stream_record())
    w.assert_not_approved(result)


def test_previous_finding_in_another_fleet_not_approved(w):
    """Previous finding has different fleet_id → no match → no Action."""
    # Replace the existing prev finding with one in a different fleet
    w.ddb.Table(_T["findings"]).delete_item(Key={"finding_id": _PREV_FINDING_ID})
    w.prev_finding(fleet="fleet-other-99")
    result = w.run(_stream_record())
    w.assert_not_approved(result)


# ---------------------------------------------------------------------------
# Condition (d): previous action status, decider
# ---------------------------------------------------------------------------

def test_previous_action_missing_not_approved(w):
    """No action at the decision id of the previous finding → no Action."""
    w.ddb.Table(_T["actions"]).delete_item(
        Key={"action_id": _prev_action_id(_PREV_FINDING_ID)})
    result = w.run(_stream_record())
    w.assert_not_approved(result)


def test_previous_action_status_executed_not_approved(w):
    """Previous action status == 'executed' → not cancelled → no Action."""
    # Replace the prev action with status=executed
    w.ddb.Table(_T["actions"]).delete_item(
        Key={"action_id": _prev_action_id(_PREV_FINDING_ID)})
    w.prev_action(status="executed")
    result = w.run(_stream_record())
    w.assert_not_approved(result)


def test_previous_action_decided_by_simulated_not_approved(w):
    """Simulated fleet-operator decider on previous action → not a valid decider → no Action."""
    w.ddb.Table(_T["actions"]).delete_item(
        Key={"action_id": _prev_action_id(_PREV_FINDING_ID)})
    w.prev_action(decided_by={"kind": "simulated_fleet_operator",
                               "id": "simulated-fleet-operator"})
    result = w.run(_stream_record())
    w.assert_not_approved(result)


def test_previous_action_other_vin_not_approved(w):
    """Previous action has different vehicle_vin → no Action."""
    w.ddb.Table(_T["actions"]).delete_item(
        Key={"action_id": _prev_action_id(_PREV_FINDING_ID)})
    w.prev_action(vin=_VIN2)
    result = w.run(_stream_record())
    w.assert_not_approved(result)


def test_previous_action_decided_by_rule_passes(w):
    """Previous action decided by a valid rule decider → still qualifies → approved."""
    w.ddb.Table(_T["actions"]).delete_item(
        Key={"action_id": _prev_action_id(_PREV_FINDING_ID)})
    w.prev_action(decided_by={"kind": "rule", "id": f"{_FLEET}/auto-rebook"})
    result = w.run(_stream_record())
    w.assert_approved(result)


# ---------------------------------------------------------------------------
# Condition (e): need-ids equality
# ---------------------------------------------------------------------------

def test_needs_differ_not_approved(w):
    """New Finding needs != previous action needs → no Action."""
    # The stream record has need_id _NEED_ID; the prev action has a different id.
    w.ddb.Table(_T["actions"]).delete_item(
        Key={"action_id": _prev_action_id(_PREV_FINDING_ID)})
    w.prev_action(need_ids=["need-DIFFERENT-001"])
    result = w.run(_stream_record())
    w.assert_not_approved(result)


def test_needs_empty_on_new_finding_not_approved(w):
    """New Finding has no health.need evidence → empty need_ids → no Action."""
    result = w.run(_stream_record(need_ids=[]))
    w.assert_not_approved(result)


# ---------------------------------------------------------------------------
# Condition (f): open FI fault grounding
# ---------------------------------------------------------------------------

def test_only_recall_grounding_not_approved(w):
    """Grounding with reason='recall' (not 'fault') → no qualifying grounding → no Action."""
    # Replace with a non-fault grounding
    w.ddb.Table(_T["groundings"]).delete_item(
        Key={"vehicleId": _GROUNDING_ID, "actionId": _GROUNDING_ACTION})
    w.grounding(reason="recall")
    result = w.run(_stream_record())
    w.assert_not_approved(result)


def test_fault_grounding_for_another_vin_not_approved(w):
    """Fault grounding exists but for a different VIN → no match → no Action."""
    w.ddb.Table(_T["groundings"]).delete_item(
        Key={"vehicleId": _GROUNDING_ID, "actionId": _GROUNDING_ACTION})
    w.grounding(vin=_VIN2)
    result = w.run(_stream_record())
    w.assert_not_approved(result)


def test_grounding_missing_finding_id_not_approved(w):
    """Grounding row has no findingId → not an FI fault grounding → no Action."""
    w.ddb.Table(_T["groundings"]).delete_item(
        Key={"vehicleId": _GROUNDING_ID, "actionId": _GROUNDING_ACTION})
    w.grounding(finding_id=None)
    result = w.run(_stream_record())
    w.assert_not_approved(result)


# ---------------------------------------------------------------------------
# Condition (g): the car isn't on the simulated fleet operator's hold list (prompt 347)
# ---------------------------------------------------------------------------
# Mutants (records/11/mutants-p347-hold.md): H1 drop (g); H2 compare only the parameters'
# VIN; H3 absent parameter read as "no holds"; H4 accept a non-VIN-shaped held id;
# H5 read hold_vins only; H6 skip the list-of-strings check; H8 a null list read as empty.
# The Finding's ``vehicle_id`` is off-schema but real: the tier2 writer projects the registry's
# vehicle_id onto VIN-scoped Findings (``agents/tier2/writer.py`` ``_standing_projection``); the
# parameters' ``vehicle_id`` has no writer. Both can only add holds (review p347 c1 S1, c2 S1).

def _with_image(record: dict, **fields) -> dict:
    """The stream record with extra NewImage attributes (e.g. the Finding's vehicle_id)."""
    record["dynamodb"]["NewImage"].update({k: _typed(v) for k, v in fields.items()})
    return record


def test_held_by_vin_not_approved(w):
    """The car's VIN is in hold_vins → no Action, though every other condition holds."""
    _put_holds({**_HOLD_DEFAULT, "hold_vins": [_HELD_OTHER, _VIN]})
    w.assert_not_approved(w.run(_stream_record()))


def test_held_by_vehicle_id_only_not_approved(w):
    """The VIN is only in hold_vehicle_ids (VIN-form id), hold_vins empty → no Action (H5)."""
    _put_holds({**_HOLD_DEFAULT, "hold_vins": [], "hold_vehicle_ids": [_VIN]})
    w.assert_not_approved(w.run(_stream_record()))


def test_held_by_findings_vehicle_id_not_approved(w):
    """The Finding's vehicle_id is held though its VIN isn't → no Action (H2)."""
    _put_holds({**_HOLD_DEFAULT, "hold_vins": [], "hold_vehicle_ids": [_VIN2]})
    w.assert_not_approved(w.run(_with_image(_stream_record(), vehicle_id=_VIN2)))


def test_another_car_held_control_approved(w):
    """Control for the three above: only another car held → approved."""
    _put_holds({**_HOLD_DEFAULT, "hold_vins": [_VIN2], "hold_vehicle_ids": [_VIN2]})
    w.assert_approved(w.run(_with_image(_stream_record(), vehicle_id=_VIN)))


def test_no_hold_keys_control_approved(w):
    """Control: a parameter with neither list holds nothing → approved."""
    _put_holds({"daily_cap": 0, "fleets": {}})
    w.assert_approved(w.run(_stream_record()))


def test_hold_parameter_absent_not_approved(w):
    """The parameter doesn't exist → the hold list can't be read → no Action (H3)."""
    boto3.client("ssm", region_name="us-west-2").delete_parameter(Name=_HOLD_PARAM)
    w.assert_not_approved(w.run(_stream_record()))


def test_hold_param_env_unset_not_approved(w, monkeypatch):
    """FI_SIM_OPERATOR_PARAM unset → no Action."""
    monkeypatch.delenv("FI_SIM_OPERATOR_PARAM")
    w.assert_not_approved(w.run(_stream_record()))


@pytest.mark.parametrize("value", [
    "not json",
    "[]",
    '{"hold_vins": "1HGBH41JXMN109186"}',
    '{"hold_vins": [1]}',
    '{"hold_vehicle_ids": {"VEH-1": true}}',
    '{"hold_vins": null}',
    '{"hold_vehicle_ids": null}',
])
def test_hold_list_malformed_not_approved(w, value):
    """Not JSON, not an object, or a list that is null or isn't a list of strings → no Action
    (H6, H8)."""
    _put_holds(value)
    w.assert_not_approved(w.run(_stream_record()))


def test_held_legacy_vehicle_id_stops_every_rebook(w):
    """A held id that isn't VIN-shaped can't be resolved here → no Action for any car (H4)."""
    _put_holds({**_HOLD_DEFAULT, "hold_vehicle_ids": [_HELD_OTHER, "VEH-MRDN-0015"]})
    w.assert_not_approved(w.run(_stream_record()))


# ---------------------------------------------------------------------------
# Flag checks
# ---------------------------------------------------------------------------

def test_flag_unset_not_approved(w, monkeypatch):
    """FI_AUTO_REBOOK_ENABLED not set → not approved."""
    monkeypatch.delenv("FI_AUTO_REBOOK_ENABLED")
    result = w.run(_stream_record())
    w.assert_not_approved(result)


def test_flag_capital_true_not_approved(w, monkeypatch):
    """FI_AUTO_REBOOK_ENABLED='True' → must be exactly 'true' → not approved."""
    monkeypatch.setenv("FI_AUTO_REBOOK_ENABLED", "True")
    result = w.run(_stream_record())
    w.assert_not_approved(result)


# ---------------------------------------------------------------------------
# No enabled rule
# ---------------------------------------------------------------------------

def test_no_enabled_rule_not_approved(w):
    """No enabled rule for (fleet, book_fleet_service) → no Action."""
    w.ddb.Table(_T["rules"]).delete_item(
        Key={"fleet_id": _FLEET, "rule_id": _RULE_ID_STR})
    result = w.run(_stream_record())
    w.assert_not_approved(result)


# ---------------------------------------------------------------------------
# Read-error paths: each raising → no Action (warning logged, no raise)
# ---------------------------------------------------------------------------

def test_findings_table_read_error_not_approved(w, monkeypatch):
    """Exception from findings query → predicate returns False → no Action."""
    monkeypatch.setenv("AVX_FINDINGS_TABLE", "")  # _table_name raises RuntimeError
    result = w.run(_stream_record())
    w.assert_not_approved(result)


def _patch_table_method(w, raise_table: str, method: str, exc: Exception):
    """Return a context manager that makes ``w.ddb.Table(raise_table).<method>`` raise.

    Patches ``w.ddb.Table`` (the factory method), so the patch applies even though
    boto3 creates a new Table object on each ``.Table()`` call.
    """
    orig_table = w.ddb.Table

    class _Raising:
        def __init__(self, real):
            self._real = real

        def __getattr__(self, name):
            if name == method:
                def _raise(*args, **kwargs):
                    raise exc
                return _raise
            return getattr(self._real, name)

    def _patched(name):
        tbl = orig_table(name)
        if name == raise_table:
            return _Raising(tbl)
        return tbl

    return patch.object(w.ddb, "Table", side_effect=_patched)


def test_findings_query_raises_not_approved(w):
    """Actual DynamoDB query on findings raises → predicate returns False → no Action, no dispatch."""
    with _patch_table_method(w, _T["findings"], "query",
                             RuntimeError("injected findings query error")):
        result = w.run(_stream_record())
    w.assert_not_approved(result)


def test_action_lookup_read_error_not_approved(w):
    """Exception from action get_item → predicate returns False → no Action, no dispatch."""
    with _patch_table_method(w, _T["actions"], "get_item",
                             RuntimeError("injected action get_item error")):
        result = w.run(_stream_record())
    w.assert_not_approved(result)


def test_groundings_query_raises_not_approved(w):
    """Actual DynamoDB query on groundings raises → predicate returns False → no Action, no dispatch."""
    with _patch_table_method(w, _T["groundings"], "query",
                             RuntimeError("injected groundings query error")):
        result = w.run(_stream_record())
    w.assert_not_approved(result)


def test_groundings_table_not_set_not_approved(w, monkeypatch):
    """CMS_GROUNDINGS_TABLE not set → predicate returns False → no Action."""
    monkeypatch.setenv("CMS_GROUNDINGS_TABLE", "")
    result = w.run(_stream_record())
    w.assert_not_approved(result)


# ---------------------------------------------------------------------------
# R1: need-ids equality — superset and strict-subset must also be refused
# ---------------------------------------------------------------------------

_NEED_EXTRA = "need-EXTRA-R1-999"
_NEED_SECOND = "need-P0AA6-srv2-B"


def test_needs_superset_not_approved(w):
    """New Finding needs are a strict superset of the previous action's (prev ⊂ new) → no Action.

    Mutant R1 (handler.py:428): changed ``!=`` to a superset-accepting check.  The
    old test used disjoint sets only and could not catch this mutant.
    """
    # Prev action booked exactly one need; new Finding books that need PLUS one extra.
    w.ddb.Table(_T["actions"]).delete_item(
        Key={"action_id": _prev_action_id(_PREV_FINDING_ID)})
    w.prev_action(need_ids=[_NEED_ID])          # prev has {_NEED_ID}
    result = w.run(_stream_record(need_ids=[_NEED_ID, _NEED_EXTRA]))  # new has {_NEED_ID, _NEED_EXTRA}
    w.assert_not_approved(result)


def test_needs_strict_subset_not_approved(w):
    """New Finding needs are a strict subset of the previous action's (new ⊂ prev) → no Action."""
    # Prev action booked two needs; new Finding books only one of them.
    w.ddb.Table(_T["actions"]).delete_item(
        Key={"action_id": _prev_action_id(_PREV_FINDING_ID)})
    w.prev_action(need_ids=[_NEED_ID, _NEED_SECOND])   # prev has {_NEED_ID, _NEED_SECOND}
    result = w.run(_stream_record(need_ids=[_NEED_ID]))  # new has {_NEED_ID} only
    w.assert_not_approved(result)


# ---------------------------------------------------------------------------
# R3: previous decider — user, engineer, missing decided_by must all be refused
# ---------------------------------------------------------------------------

def test_previous_decided_by_user_not_approved(w):
    """Previous action decided_by kind='user' → not operator or rule → no Action.

    Mutant R3: ``else: pass`` instead of ``else: return False`` would let this through.
    """
    w.ddb.Table(_T["actions"]).delete_item(
        Key={"action_id": _prev_action_id(_PREV_FINDING_ID)})
    w.prev_action(decided_by={"kind": "user", "id": "user-sub-99999"})
    result = w.run(_stream_record())
    w.assert_not_approved(result)


def test_previous_decided_by_engineer_not_approved(w):
    """Previous action decided_by kind='engineer' → no Action."""
    w.ddb.Table(_T["actions"]).delete_item(
        Key={"action_id": _prev_action_id(_PREV_FINDING_ID)})
    w.prev_action(decided_by={"kind": "engineer", "id": "eng-sub-12345"})
    result = w.run(_stream_record())
    w.assert_not_approved(result)


def test_previous_decided_by_missing_not_approved(w):
    """Previous action has no decided_by at all → no Action."""
    # Write the action item directly without a decided_by key.
    action_id = _prev_action_id(_PREV_FINDING_ID)
    w.ddb.Table(_T["actions"]).delete_item(Key={"action_id": action_id})
    w.ddb.Table(_T["actions"]).put_item(Item={
        "action_id": action_id,
        "finding_id": _PREV_FINDING_ID,
        "action_kind": "book_fleet_service",
        "target_system": "dms",
        "status": "cancelled",
        "fleet_id": _FLEET,
        "parameters": {
            "vehicle_vin": _VIN,
            "dealer_id": _DEALER,
            "needs": [{"kind": "fault", "code": "P0AA6", "need_id": _NEED_ID}],
        },
        # No "decided_by" key.
        "created_at": "2026-11-01T10:00:00Z",
        "updated_at": "2026-11-01T10:00:00Z",
    })
    result = w.run(_stream_record())
    w.assert_not_approved(result)


def test_previous_operator_id_not_string_not_approved(w):
    """Previous operator decided_by has a non-string id (list) → _is_valid_operator → no Action.

    The old code did ``if not prev_db.get('id')`` which passes for any truthy value.
    The fix uses _is_valid_operator which requires a non-empty string.
    """
    w.ddb.Table(_T["actions"]).delete_item(
        Key={"action_id": _prev_action_id(_PREV_FINDING_ID)})
    w.prev_action(decided_by={"kind": "operator", "id": ["a", "list"]})
    result = w.run(_stream_record())
    w.assert_not_approved(result)


# ---------------------------------------------------------------------------
# W1: compare parameters.needs (not evidence) — deferred-need case
# ---------------------------------------------------------------------------

def test_deferred_need_not_approved(w):
    """Model deferred a need: evidence has [A, B], parameters.needs has only [A].

    Prev action booked [A, B].  With the OLD code (evidence-based new_need_ids)
    the predicate would compare {A, B} == {A, B} → True (incorrectly approved).
    With the fix (parameters.needs-based) it compares {A} == {A, B} → False.
    """
    # Prev action has both needs.
    w.ddb.Table(_T["actions"]).delete_item(
        Key={"action_id": _prev_action_id(_PREV_FINDING_ID)})
    w.prev_action(need_ids=[_NEED_ID, _NEED_SECOND])
    # New finding: evidence lists both needs (agent found both), but model deferred _NEED_SECOND
    # from this booking → parameters.needs has only _NEED_ID.
    result = w.run(_stream_record(
        need_ids=[_NEED_ID, _NEED_SECOND],   # evidence: both needs
        param_need_ids=[_NEED_ID],            # parameters.needs: only the first
    ))
    w.assert_not_approved(result)


# ---------------------------------------------------------------------------
# W2: server-set parameter check — dealer_id absent → no Action
# ---------------------------------------------------------------------------

def test_rebook_no_dealer_id_not_approved(w):
    """recommended_action.parameters has no dealer_id → check_server_set_params fails
    (dealer_id is required after fill) → no Action written, no dispatch.
    """
    result = w.run(_stream_record(dealer_id=None))
    w.assert_not_approved(result)


# ---------------------------------------------------------------------------
# W3: _MAX_FINDINGS_PAGES cap — hitting the cap with more pages → no Action
# ---------------------------------------------------------------------------

def test_findings_page_cap_not_approved(w):
    """Findings query hits _MAX_FINDINGS_PAGES with a LastEvaluatedKey still set → no Action.

    The prev_finding is returned on the 10th (last) page, which still reports a
    LastEvaluatedKey.  Without W3 fix the loop exits with ``prev_findings=[item]`` and
    condition (c) passes; the predicate proceeds and approves the rebook incorrectly.
    With the fix the while-else clause fires and returns False.
    """
    _fake_lk = {"finding_id": {"S": "dummy-last-key"}}
    orig_table = w.ddb.Table

    # Fetch the prev finding we'll deliver on the last page.
    prev_item = orig_table(_T["findings"]).get_item(
        Key={"finding_id": _PREV_FINDING_ID}
    )["Item"]

    call_count = [0]

    class _PaginatedFindings:
        def query(self, **kw):
            call_count[0] += 1
            # Deliver the qualifying item on the 10th page but report more pages.
            items = [prev_item] if call_count[0] == 10 else []
            return {"Items": items, "LastEvaluatedKey": _fake_lk}

        def __getattr__(self, name):
            return getattr(orig_table(_T["findings"]), name)

    def _patched(name):
        if name == _T["findings"]:
            return _PaginatedFindings()
        return orig_table(name)

    with patch.object(w.ddb, "Table", side_effect=_patched):
        result = w.run(_stream_record())
    w.assert_not_approved(result)


# ---------------------------------------------------------------------------
# W6: moves flag enabled, rebook flag unset → no book_fleet_service Action
# ---------------------------------------------------------------------------

def test_moves_flag_true_rebook_flag_unset_not_approved(w, monkeypatch):
    """FI_AUTO_MOVES_ENABLED='true', FI_AUTO_REBOOK_ENABLED unset → no Action.

    Mutant R4 (handler.py:534): the moves flag also enables rebooking.  Under that
    defect, the auto-approver writes a rule Action and then registry declines the
    dispatch (FI_AUTO_REBOOK_ENABLED unset), spending the Finding's one decision
    and removing the item from the operator queue.
    """
    monkeypatch.delenv("FI_AUTO_REBOOK_ENABLED")
    monkeypatch.setenv("FI_AUTO_MOVES_ENABLED", "true")
    result = w.run(_stream_record())
    w.assert_not_approved(result)
