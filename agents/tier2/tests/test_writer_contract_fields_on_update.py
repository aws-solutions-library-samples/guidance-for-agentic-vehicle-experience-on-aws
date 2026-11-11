"""An in-place update stores the run's ``confidence``, ``tokens_used`` and ``cost_usd``.

Issue ``issues/2026-11-09-tier2-writer-contract-fields-on-update/`` (prompt 356's sweep,
fixed in prompt 361). ``confidence`` is a Tier 2 artifact-contract field
(``~/.kiro/steering/agentic-tiers.md``); ``tokens_used`` and ``cost_usd`` say whether a
model or the deterministic fallback produced the text. ``_update_item`` rewrote the text
and, since 2026-11-06, ``agent_version``, but never these three, so a Finding first written
by the fallback and later rewritten by the model kept ``confidence``, ``tokens_used = 0``
and ``cost_usd = 0.0`` from its first write. Same defect class as
``issues/2026-11-06-tier2-writer-stale-agent-version/``.

Pinned on moto (a real UpdateItem, read back with GetItem), on the same routes and scopes
as ``test_writer_agent_version.py``: every route into the update path stores the second
run's three values; the same-hash no-op and the ``refresh_on_noop`` keep-alive, which
rewrite no text, keep the stored ones. The values are chosen to appear nowhere in the
writer, so a hardcoded value can't pass.

Mutants: ``issues/2026-11-09-tier2-writer-contract-fields-on-update/mutants.md``.
"""

from __future__ import annotations

import hashlib
from decimal import Decimal

import boto3
import pytest
from boto3.dynamodb.types import TypeDeserializer
from moto import mock_aws

from agents.tier2.owner_resolver import StandingFacts
from agents.tier2.writer import compute_finding_key, write_finding

_TABLE = "vsa-moto-avx-findings-contract-fields"
_REGION = "us-west-2"
_VIN = "MRDNCF00000000361"
_KIND = "test.contract_fields"

#: (confidence, tokens_used, cost_usd) of the first and the second run. The default order
#: is a fallback write rewritten by the model; ``_DIRECTIONS`` also runs the reverse, a model
#: write rewritten by the fallback, whose zeros must be stored (review p361 c1 W1).
_FIRST = ("low", 0, 0.0)
_SECOND = ("high", 18437, 0.0731)
_DIRECTIONS = {"fallback_then_model": (_FIRST, _SECOND), "model_then_fallback": (_SECOND, _FIRST)}

_H_FIRST = hashlib.sha256(b"contract-fields-first-write-grault").hexdigest()
_H_SECOND = hashlib.sha256(b"contract-fields-second-write-garply").hexdigest()

_FIRST_AT = "2026-11-09T10:00:00Z"
_SECOND_AT = "2026-11-09T11:00:00Z"
_FIRST_EXPIRES = "2026-11-23T10:00:00Z"
_SECOND_EXPIRES = "2026-11-24T11:00:00Z"

_DESER = TypeDeserializer()

_CASES = (
    ("agent-2-vehicle-health", "vin", _VIN),
    ("agent-fi-cost", "vin", _VIN),
    ("agent-fi-rebalancing", "fleet_id", "FLEET-CF-361"),
    ("agent-2-vehicle-health", "owner_id", "owner-cf-361"),
)
_VIN_CASES = tuple(c for c in _CASES if c[1] == "vin")
_VIN_IDS = [f"{a}-{s}" for a, s, _ in _VIN_CASES]


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


def _finding(case: tuple, *, finding_id: str, values: tuple, inputs_hash: str,
             computed_at: str, expires_at: str, **overrides) -> dict:
    agent_id, scope_field, scope_value = case
    confidence, tokens_used, cost_usd = values
    f = {
        "finding_id": finding_id,
        "finding_key": compute_finding_key(
            agent_id=agent_id, scope_field=scope_field, scope_value=scope_value,
            finding_kind=_KIND,
        ),
        "agent_id": agent_id,
        "agent_version": "4.1.3",
        "scope": {scope_field: scope_value},
        "severity": "routine",
        "computed_at": computed_at,
        "expires_at": expires_at,
        "confidence": confidence,
        "finding_kind": _KIND,
        "headline": "First headline.",
        "detail": "Detail text.",
        "evidence": [],
        "inputs_hash": inputs_hash,
        "tokens_used": tokens_used,
        "cost_usd": cost_usd,
        "audit_trail": [],
    }
    f.update(overrides)
    return f


def _read(client, finding_id: str) -> dict:
    item = client.get_item(TableName=_TABLE, Key={"finding_id": {"S": finding_id}})["Item"]
    return {k: _DESER.deserialize(v) for k, v in item.items()}


def _stored_values(stored: dict) -> tuple:
    return (stored["confidence"], stored["tokens_used"], stored["cost_usd"])


def _expected(values: tuple) -> tuple:
    confidence, tokens_used, cost_usd = values
    return (confidence, Decimal(tokens_used), Decimal(str(cost_usd)))


def _write_first(client, case: tuple, standing=None, values: tuple = _FIRST) -> str:
    first = _finding(case, finding_id="FIND-contract-fields-first", values=values,
                     inputs_hash=_H_FIRST, computed_at=_FIRST_AT, expires_at=_FIRST_EXPIRES)
    result = write_finding(first, table_name=_TABLE, dynamodb_client=client,
                           utc_now_fn=lambda: _FIRST_AT, standing=standing)
    assert result["action"] == "put"
    # Positive control: the first write stores the first run's values.
    assert _stored_values(_read(client, result["finding_id"])) == _expected(values)
    return result["finding_id"]


def _second(case: tuple, finding_id: str, values: tuple = _SECOND, **overrides) -> dict:
    f = _finding(case, finding_id=finding_id, values=values, inputs_hash=_H_FIRST,
                 computed_at=_SECOND_AT, expires_at=_SECOND_EXPIRES)
    f.update(overrides)
    return f


_DRIFTED = StandingFacts(owner_id="owner-after-move", fleet_id="FLEET-AFTER", vehicle_id=None)
_ORIGINAL = StandingFacts(owner_id="owner-before-move", fleet_id="FLEET-BEFORE", vehicle_id=None)

_UPDATE_ROUTES = {
    "changed_inputs_hash": (
        {"inputs_hash": _H_SECOND, "headline": "Second headline."}, {}, None, None,
    ),
    "update_on_same_hash": (
        {"headline": "Model-written headline."}, {"update_on_same_hash": True}, None, None,
    ),
    "same_hash_severity_change": ({"severity": "soon"}, {}, None, None),
}
_VIN_ONLY_ROUTES = {"same_hash_standing_drift": ({}, {}, _ORIGINAL, _DRIFTED)}

_UPDATE_PARAMS = [
    pytest.param(route, case, d, id=f"{d}-{route}-{case[0]}-{case[1]}")
    for d in sorted(_DIRECTIONS) for route in sorted(_UPDATE_ROUTES) for case in _CASES
] + [
    pytest.param(route, case, d, id=f"{d}-{route}-{case[0]}-{case[1]}")
    for d in sorted(_DIRECTIONS) for route in sorted(_VIN_ONLY_ROUTES) for case in _VIN_CASES
]


@pytest.mark.parametrize("route,case,direction", _UPDATE_PARAMS)
@mock_aws
def test_in_place_update_stores_the_runs_confidence_and_cost(
    route: str, case: tuple, direction: str,
) -> None:
    client = boto3.client("dynamodb", region_name=_REGION)
    _create_table(client)
    overrides, kwargs, first_standing, second_standing = (
        _UPDATE_ROUTES.get(route) or _VIN_ONLY_ROUTES[route]
    )
    first_values, second_values = _DIRECTIONS[direction]
    finding_id = _write_first(client, case, standing=first_standing, values=first_values)

    second = _second(case, "FIND-contract-fields-second-would-be-new", values=second_values,
                     **overrides)
    result = write_finding(second, table_name=_TABLE, dynamodb_client=client,
                           utc_now_fn=lambda: _SECOND_AT, standing=second_standing, **kwargs)

    assert result == {"action": "update", "finding_id": finding_id}, route
    stored = _read(client, finding_id)
    assert _stored_values(stored) == _expected(second_values), (
        f"{route}/{case}: an in-place update must store the rewriting run's confidence, "
        f"tokens_used and cost_usd; stored {_stored_values(stored)!r}"
    )
    # The update did rewrite the row (not a vacuous pass on an untouched item).
    assert stored["computed_at"] == _SECOND_AT


@pytest.mark.parametrize("case", _CASES, ids=[f"{a}-{s}" for a, s, _ in _CASES])
@mock_aws
def test_an_artifact_without_the_fields_updates_and_keeps_the_stored_values(case: tuple) -> None:
    """A malformed artifact missing the three (schema-required) fields still updates its
    text, and the stored values stay: neither a KeyError nor a REMOVE (as severity)."""
    client = boto3.client("dynamodb", region_name=_REGION)
    _create_table(client)
    finding_id = _write_first(client, case)
    second = _second(case, "FIND-contract-fields-partial", inputs_hash=_H_SECOND,
                     headline="Second headline.")
    for attr in ("confidence", "tokens_used", "cost_usd"):
        del second[attr]

    result = write_finding(second, table_name=_TABLE, dynamodb_client=client,
                           utc_now_fn=lambda: _SECOND_AT)

    assert result == {"action": "update", "finding_id": finding_id}
    stored = _read(client, finding_id)
    assert stored["headline"] == "Second headline."
    assert _stored_values(stored) == _expected(_FIRST)


@pytest.mark.parametrize("case", _VIN_CASES, ids=_VIN_IDS)
@mock_aws
def test_same_hash_noop_keeps_the_stored_values(case: tuple) -> None:
    client = boto3.client("dynamodb", region_name=_REGION)
    _create_table(client)
    finding_id = _write_first(client, case)

    result = write_finding(_second(case, "FIND-contract-fields-repeat"), table_name=_TABLE,
                           dynamodb_client=client, utc_now_fn=lambda: _SECOND_AT)

    assert result == {"action": "noop", "finding_id": finding_id}
    assert _stored_values(_read(client, finding_id)) == _expected(_FIRST)


@pytest.mark.parametrize("case", _VIN_CASES, ids=_VIN_IDS)
@mock_aws
def test_refresh_on_noop_keeps_the_stored_values(case: tuple) -> None:
    client = boto3.client("dynamodb", region_name=_REGION)
    _create_table(client)
    finding_id = _write_first(client, case)

    result = write_finding(_second(case, "FIND-contract-fields-keepalive"), table_name=_TABLE,
                           dynamodb_client=client, utc_now_fn=lambda: _SECOND_AT,
                           refresh_on_noop=True)

    assert result == {"action": "refresh", "finding_id": finding_id}
    stored = _read(client, finding_id)
    assert stored["expires_at"] == _SECOND_EXPIRES  # positive control: the refresh wrote
    assert _stored_values(stored) == _expected(_FIRST)
