"""An in-place update stores the version of the run that rewrote the row.

Issue: ``issues/2026-11-06-tier2-writer-stale-agent-version/`` (backlog row
``Finding agent_version on update``). ``agent_version`` is one of the Tier 2
artifact contract's fields (``~/.kiro/steering/agentic-tiers.md``): readers use it
to tell which reasoning produced what they narrate. Before the fix,
``_update_item`` rewrote the text but never SET ``agent_version``, so the 84 cost
Findings rewritten by playbook 1.1.0 on 2026-10-28 still said 1.0.0; only the new
``audit_trail`` entry carried 1.1.0.

Properties pinned here, on moto (a real UpdateItem, read back with GetItem):

1. Every route into the update path stores the NEW version at top level, for a
   non-FI and an FI agent on VIN scope and for fleet- and owner-scoped Findings,
   keeps ``finding_id``, and the top-level value equals the new audit entry's.
   The routes are: a changed ``inputs_hash``; ``update_on_same_hash`` (the cost
   re-diagnosis route that exposed the defect); a same-hash severity change; and,
   for VIN scope, a same-hash standing drift.
2. The paths that rewrite no text keep the stored version: the same-hash no-op
   and the ``refresh_on_noop`` keep-alive (VIN scope; see ``_VIN_IDS``). The
   stored text was written by the stored version, so the pair stays true.

Version strings are chosen to appear nowhere in the writer or the agents
(``1.0.0`` / ``1.1.0`` do), so a hardcoded value cannot pass.

Mutants: ``issues/2026-11-06-tier2-writer-stale-agent-version/mutants/run_mutants.py``,
results beside it.
"""

from __future__ import annotations

import hashlib

import boto3
import pytest
from boto3.dynamodb.types import TypeDeserializer
from moto import mock_aws

from agents.tier2.owner_resolver import StandingFacts
from agents.tier2.writer import compute_finding_key, write_finding

_TABLE = "vsa-moto-avx-findings-agent-version"
_REGION = "us-west-2"
_VIN = "MRDNAV00000000304"
_KIND = "test.agent_version"

_OLD_VERSION = "3.7.0"
_NEW_VERSION = "3.8.2"

_H_FIRST = hashlib.sha256(b"agent-version-first-write-quux").hexdigest()
_H_SECOND = hashlib.sha256(b"agent-version-second-write-corge").hexdigest()

_FIRST_AT = "2026-11-06T10:00:00Z"
_SECOND_AT = "2026-11-06T11:00:00Z"
_FIRST_EXPIRES = "2026-11-20T10:00:00Z"
_SECOND_EXPIRES = "2026-11-21T11:00:00Z"

_DESER = TypeDeserializer()

#: (agent_id, scope_field, scope_value). One non-FI and one FI agent on VIN scope:
#: the FI branch of ``_update_item`` adds its own SET clauses. Plus a fleet-scoped
#: and an owner-scoped Finding: ``_update_item`` also branches on scope (standing
#: converges for VIN only; the REMOVE guards skip ``owner_id``). A fix scoped to
#: any one of those branches must fail one of these.
_CASES = (
    ("agent-2-vehicle-health", "vin", _VIN),
    ("agent-fi-cost", "vin", _VIN),
    ("agent-fi-rebalancing", "fleet_id", "FLEET-AV-304"),
    ("agent-2-vehicle-health", "owner_id", "owner-av-304"),
)
_VIN_CASES = tuple(c for c in _CASES if c[1] == "vin")


def _create_table(client) -> None:
    client.create_table(
        TableName=_TABLE,
        KeySchema=[{"AttributeName": "finding_id", "KeyType": "HASH"}],
        AttributeDefinitions=[
            {"AttributeName": "finding_id", "AttributeType": "S"},
            {"AttributeName": "finding_key", "AttributeType": "S"},
        ],
        GlobalSecondaryIndexes=[{
            "IndexName": "by_finding_key",
            "KeySchema": [{"AttributeName": "finding_key", "KeyType": "HASH"}],
            "Projection": {"ProjectionType": "ALL"},
        }],
        BillingMode="PAY_PER_REQUEST",
    )


def _finding(
    *,
    agent_id: str,
    scope_field: str,
    scope_value: str,
    finding_id: str,
    agent_version: str,
    inputs_hash: str,
    computed_at: str,
    expires_at: str,
    severity: str = "routine",
    headline: str = "First headline.",
) -> dict:
    return {
        "finding_id": finding_id,
        "finding_key": compute_finding_key(
            agent_id=agent_id, scope_field=scope_field, scope_value=scope_value,
            finding_kind=_KIND,
        ),
        "agent_id": agent_id,
        "agent_version": agent_version,
        "scope": {scope_field: scope_value},
        "severity": severity,
        "computed_at": computed_at,
        "expires_at": expires_at,
        "confidence": "medium",
        "finding_kind": _KIND,
        "headline": headline,
        "detail": "Detail text.",
        "evidence": [],
        "inputs_hash": inputs_hash,
        "tokens_used": 0,
        "cost_usd": 0.0,
        "audit_trail": [],
    }


def _read(client, finding_id: str) -> dict:
    item = client.get_item(TableName=_TABLE, Key={"finding_id": {"S": finding_id}})["Item"]
    return {k: _DESER.deserialize(v) for k, v in item.items()}


def _write_first(client, case: tuple, standing=None) -> str:
    agent_id, scope_field, scope_value = case
    first = _finding(
        agent_id=agent_id, scope_field=scope_field, scope_value=scope_value,
        finding_id="FIND-agent-version-first",
        agent_version=_OLD_VERSION, inputs_hash=_H_FIRST,
        computed_at=_FIRST_AT, expires_at=_FIRST_EXPIRES,
    )
    result = write_finding(
        first, table_name=_TABLE, dynamodb_client=client,
        utc_now_fn=lambda: _FIRST_AT, standing=standing,
    )
    assert result["action"] == "put"
    stored = _read(client, result["finding_id"])
    # Positive control: the first write stores the first version.
    assert stored["agent_version"] == _OLD_VERSION
    return result["finding_id"]


def _second(case: tuple, finding_id: str, **overrides) -> dict:
    agent_id, scope_field, scope_value = case
    f = _finding(
        agent_id=agent_id, scope_field=scope_field, scope_value=scope_value,
        finding_id=finding_id, agent_version=_NEW_VERSION, inputs_hash=_H_FIRST,
        computed_at=_SECOND_AT, expires_at=_SECOND_EXPIRES,
    )
    f.update(overrides)
    return f


# Each route: (second-write overrides, write_finding kwargs, standing on the first
# write, standing on the second write).
_DRIFTED = StandingFacts(owner_id="owner-after-move", fleet_id="FLEET-AFTER", vehicle_id=None)
_ORIGINAL = StandingFacts(owner_id="owner-before-move", fleet_id="FLEET-BEFORE", vehicle_id=None)

#: Routes that reach ``_update_item`` for any scope.
_UPDATE_ROUTES = {
    "changed_inputs_hash": (
        {"inputs_hash": _H_SECOND, "headline": "Second headline."}, {}, None, None,
    ),
    "update_on_same_hash": (
        {"headline": "Model-written headline."}, {"update_on_same_hash": True}, None, None,
    ),
    "same_hash_severity_change": (
        {"severity": "soon"}, {}, None, None,
    ),
}
#: Standing is projected for VIN-scoped Findings only, so this route exists only there.
_VIN_ONLY_ROUTES = {
    "same_hash_standing_drift": ({}, {}, _ORIGINAL, _DRIFTED),
}

_UPDATE_PARAMS = [
    pytest.param(route, case, id=f"{route}-{case[0]}-{case[1]}")
    for route in sorted(_UPDATE_ROUTES) for case in _CASES
] + [
    pytest.param(route, case, id=f"{route}-{case[0]}-{case[1]}")
    for route in sorted(_VIN_ONLY_ROUTES) for case in _VIN_CASES
]


@pytest.mark.parametrize("route,case", _UPDATE_PARAMS)
@mock_aws
def test_in_place_update_stores_the_new_agent_version(route: str, case: tuple) -> None:
    client = boto3.client("dynamodb", region_name=_REGION)
    _create_table(client)
    overrides, kwargs, first_standing, second_standing = (
        _UPDATE_ROUTES.get(route) or _VIN_ONLY_ROUTES[route]
    )
    finding_id = _write_first(client, case, standing=first_standing)

    second = _second(case, "FIND-agent-version-second-would-be-new", **overrides)
    result = write_finding(
        second, table_name=_TABLE, dynamodb_client=client,
        utc_now_fn=lambda: _SECOND_AT, standing=second_standing, **kwargs,
    )

    assert result == {"action": "update", "finding_id": finding_id}, route
    stored = _read(client, finding_id)
    assert stored["agent_version"] == _NEW_VERSION, (
        f"{route}/{case}: an in-place update must store the version of the run that "
        f"rewrote the row; stored {stored['agent_version']!r}"
    )
    assert stored["audit_trail"][-1]["agent_version"] == stored["agent_version"]
    assert len(stored["audit_trail"]) == 1
    # The update did rewrite the row (not a vacuous pass on an untouched item).
    assert stored["computed_at"] == _SECOND_AT


#: The no-op and refresh paths are not reachable for fleet- or owner-scoped Findings
#: today: their denormalised scope attribute (``fleet_id`` / ``owner_id``) is read back as
#: persisted standing and never equals the empty desired projection, so a same-hash write
#: always updates (issue ``2026-11-06-tier2-writer-fleet-owner-same-hash-never-noop``, not
#: fixed here). These two pins therefore cover the VIN cases only; widen them to
#: ``_CASES`` when that issue is fixed.
_VIN_IDS = [f"{a}-{s}" for a, s, _ in _VIN_CASES]


@pytest.mark.parametrize("case", _VIN_CASES, ids=_VIN_IDS)
@mock_aws
def test_same_hash_noop_keeps_the_stored_version(case: tuple) -> None:
    client = boto3.client("dynamodb", region_name=_REGION)
    _create_table(client)
    finding_id = _write_first(client, case)

    repeat = _second(
        case, "FIND-agent-version-repeat", headline="Text the new version would have written.",
    )
    result = write_finding(
        repeat, table_name=_TABLE, dynamodb_client=client, utc_now_fn=lambda: _SECOND_AT,
    )

    assert result == {"action": "noop", "finding_id": finding_id}
    stored = _read(client, finding_id)
    # The stored text is still the first version's, so its version stays with it.
    assert stored["headline"] == "First headline."
    assert stored["agent_version"] == _OLD_VERSION
    assert stored["audit_trail"] == []


@pytest.mark.parametrize("case", _VIN_CASES, ids=_VIN_IDS)
@mock_aws
def test_refresh_on_noop_keeps_the_stored_version(case: tuple) -> None:
    client = boto3.client("dynamodb", region_name=_REGION)
    _create_table(client)
    finding_id = _write_first(client, case)

    keep_alive = _second(
        case, "FIND-agent-version-keepalive", headline="Text the new version would have written.",
    )
    result = write_finding(
        keep_alive, table_name=_TABLE, dynamodb_client=client,
        utc_now_fn=lambda: _SECOND_AT, refresh_on_noop=True,
    )

    assert result == {"action": "refresh", "finding_id": finding_id}
    stored = _read(client, finding_id)
    # Positive control: the refresh did write (expiry moved) ...
    assert stored["expires_at"] == _SECOND_EXPIRES
    # ... but rewrote no text, so the version that wrote that text stays.
    assert stored["headline"] == "First headline."
    assert stored["agent_version"] == _OLD_VERSION
