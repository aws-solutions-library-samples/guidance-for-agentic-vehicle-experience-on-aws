"""
Tests for T10.2 scope denormalisation — ``agents/tier2/writer.py``.

Spec source:
    .kiro/specs/2026-09-18-avx-tier2-ios-cards-and-actions/tasks.md
    § Fix Group 10 → T10.2

The persisted DDB item carries a top-level attribute named for the active scope
field (e.g. ``vin``, ``fleet_id``) with the same value as the nested
``scope.<field>``.  This makes the five sparse scope GSIs queryable without a
schema change.

The denormalised attribute lives ONLY in the DDB item — NEVER in:
- the artifact / Finding dict returned to callers,
- ``inputs_hash`` (computed from ADP input rows before write_finding() runs),
- the frozen JSON schema.

Acceptance criteria tested here (T10.2 Accept 1–4):
1. On the PutItem path: top-level attribute present in Item= payload, nested
   scope unchanged.
2. On the UpdateItem path: top-level attribute set via UpdateExpression.
3. Artifact/item boundary: the dict returned to callers has no top-level scope
   field.
4. inputs_hash idempotency: hash is byte-identical whether the denormalised
   attribute would be present or not (it is NOT included in inputs_hash because
   inputs_hash is computed over ADP rows before write_finding() is called).

Mutation guards (T10.2 mutations):
(a) Drop denormalisation for dealership_id only → dealership_id case fails, others pass.
(b) Write attribute into returned artifact dict → artifact-boundary test fails.
(c) Include denormalised attribute in inputs_hash computation → idempotency test fails.

All tests use a DDB Stubber (botocore.stub.Stubber); no network calls.

Design rules applied (from existing test_finding_key_update_in_place.py):
1. Never read prose in a guard — compare structured data.
2. Mutation values come from OUTSIDE the assertion's own vocabulary.
3. "Covered by construction" is a claim; measure and say what was measured.
4. Restore from cp copy, NEVER git checkout.
"""

from __future__ import annotations

import hashlib
import uuid
from unittest.mock import patch

import boto3
import pytest
from boto3.dynamodb.types import TypeDeserializer
from botocore.stub import ANY, Stubber

from agents.tier2.writer import (
    _extract_scope,
    _put_item,
    _update_item,
    compute_finding_key,
    write_finding,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TABLE_NAME = "vsa-test-avx-findings"
AGENT_ID = "agent-2-vehicle-health"
FINDING_KIND = "health.battery"

# Deterministic hashes used for inputs_hash — different from each other and
# from any string used in assertions (Rule 2: values from outside the
# assertion's own vocabulary).
_HASH_A = hashlib.sha256(b"t10-2-run-A-xyzzy").hexdigest()
_HASH_B = hashlib.sha256(b"t10-2-run-B-plugh").hexdigest()

assert _HASH_A != _HASH_B

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_deserializer = TypeDeserializer()


def _deserialise(ddb_value: dict) -> object:
    """Deserialise a single DynamoDB AttributeValue."""
    return _deserializer.deserialize(ddb_value)


def _make_ddb_client():
    client = boto3.client("dynamodb", region_name="us-west-2")
    return client, Stubber(client)


def _make_finding_for_scope(scope_field: str, scope_value: str, inputs_hash: str = _HASH_A) -> dict:
    """Build a minimal Finding for a given scope field."""
    finding_key = compute_finding_key(
        agent_id=AGENT_ID,
        scope_field=scope_field,
        scope_value=scope_value,
        finding_kind=FINDING_KIND,
    )
    finding_id = f"FIND-{uuid.uuid4()}"
    return {
        "finding_id": finding_id,
        "finding_key": finding_key,
        "agent_id": AGENT_ID,
        "agent_version": "1.0.0",
        "scope": {scope_field: scope_value},
        "severity": "routine",
        "computed_at": "2026-09-19T03:00:00Z",
        "expires_at": "2026-09-27T03:00:00Z",
        "confidence": "high",
        "finding_kind": FINDING_KIND,
        "headline": f"Test finding for scope {scope_field}.",
        "detail": "Denormalisation test detail.",
        "evidence": [],
        "inputs_hash": inputs_hash,
        "tokens_used": 0,
        "cost_usd": 0.0,
        "audit_trail": [],
    }


def _stub_query_no_results(stubber: Stubber) -> None:
    """Stub a Query that returns no items (new-row / PutItem path)."""
    stubber.add_response(
        "query",
        {"Items": [], "Count": 0},
        expected_params={
            "TableName": TABLE_NAME,
            "IndexName": "by_finding_key",
            "KeyConditionExpression": "finding_key = :fk",
            "FilterExpression": "expires_at > :now",
            "ExpressionAttributeValues": ANY,
        },
    )


def _stub_put_item_capture(stubber: Stubber, captured: list) -> None:
    """Stub PutItem; appends the received Item= dict to *captured*."""
    original_put = None  # assigned below

    def _intercept(*, TableName, Item, **kwargs):
        captured.append(Item)

    stubber.add_response(
        "put_item",
        {},
        expected_params={"TableName": TABLE_NAME, "Item": ANY},
    )
    return _intercept


# ---------------------------------------------------------------------------
# Scope field fixtures
# ---------------------------------------------------------------------------

SCOPE_CASES = [
    ("vin", "1HGCM82633A004352"),
    ("owner_id", "owner-uuid-0001"),
    ("fleet_id", "fleet-uuid-aaaa"),
    ("dealership_id", "dealer-uuid-bbbb"),
    ("district_id", "district-uuid-cccc"),
]


# ---------------------------------------------------------------------------
# T10.2 Accept 1 — PutItem path: top-level attribute present with correct value
# ---------------------------------------------------------------------------

class TestPutItemScopeAttribute:
    """Assert the PutItem Item= payload carries the top-level scope attribute.

    One test per scope field (all five).  Verifies:
    - The top-level attribute (e.g. ``Item["vin"]``) is present.
    - Its value matches the nested ``scope.<field>`` value (serialised as ``{"S": ...}``).
    - The nested ``scope`` map is untouched.
    - The original finding dict passed to write_finding() is NOT mutated.

    Mutation guard for (a): if denormalisation is dropped for ``dealership_id``
    only, ``test_put_item_projects_dealership_id`` MUST fail while the other
    four pass — proving per-field coverage rather than a single shared path.
    """

    @pytest.mark.parametrize("scope_field,scope_value", SCOPE_CASES)
    def test_put_item_projects_top_level_scope_attribute(self, scope_field, scope_value):
        """Top-level attribute present in PutItem Item= payload with correct value."""
        finding = _make_finding_for_scope(scope_field, scope_value)
        finding_before = dict(finding)  # snapshot for mutation guard

        client, stubber = _make_ddb_client()
        captured_items: list[dict] = []

        # Patch _put_item's DDB call to capture the Item= argument.
        original_put_item = client.put_item

        def _capturing_put(**kwargs):
            captured_items.append(kwargs["Item"])
            return original_put_item(**kwargs)

        client.put_item = _capturing_put  # type: ignore[method-assign]

        with stubber:
            _stub_query_no_results(stubber)
            stubber.add_response(
                "put_item",
                {},
                expected_params={"TableName": TABLE_NAME, "Item": ANY},
            )
            write_finding(
                finding,
                table_name=TABLE_NAME,
                dynamodb_client=client,
                utc_now_fn=lambda: "2026-09-19T03:01:00Z",
            )

        assert len(captured_items) == 1, "Exactly one put_item call expected."
        item = captured_items[0]

        # Assert top-level scope attribute is present with correct value.
        assert scope_field in item, (
            f"Expected top-level attribute '{scope_field}' in DDB Item, "
            f"got keys: {sorted(item.keys())}"
        )
        assert _deserialise(item[scope_field]) == scope_value, (
            f"Top-level '{scope_field}' value mismatch: "
            f"expected {scope_value!r}, got {_deserialise(item[scope_field])!r}"
        )

        # Assert nested scope dict is still present and unchanged.
        assert "scope" in item, "Nested 'scope' map must remain in the DDB item."
        nested_scope = _deserialise(item["scope"])
        assert nested_scope.get(scope_field) == scope_value, (
            f"Nested scope.{scope_field} must be preserved; got {nested_scope!r}"
        )

        # Assert original finding dict was NOT mutated.
        assert finding == finding_before, (
            "write_finding() must not mutate the caller's finding dict. "
            f"Before: {finding_before}, after: {finding}"
        )

    # Explicit per-field test names for the mutation guard — the parametrize
    # produces one node per scope_field; naming below makes mutation (a) easy
    # to verify by running only the dealership_id node.
    def test_put_item_projects_vin(self):
        """Explicit vin node for mutation (a) isolation."""
        self.test_put_item_projects_top_level_scope_attribute("vin", "1HGCM82633A004352")

    def test_put_item_projects_owner_id(self):
        self.test_put_item_projects_top_level_scope_attribute("owner_id", "owner-uuid-0001")

    def test_put_item_projects_fleet_id(self):
        self.test_put_item_projects_top_level_scope_attribute("fleet_id", "fleet-uuid-aaaa")

    def test_put_item_projects_dealership_id(self):
        """dealership_id case — required to FAIL when mutation (a) is applied."""
        self.test_put_item_projects_top_level_scope_attribute("dealership_id", "dealer-uuid-bbbb")

    def test_put_item_projects_district_id(self):
        self.test_put_item_projects_top_level_scope_attribute("district_id", "district-uuid-cccc")


# ---------------------------------------------------------------------------
# T10.2 Accept 1 — UpdateItem path: scope attribute set via UpdateExpression
# ---------------------------------------------------------------------------

class TestUpdateItemScopeAttribute:
    """Assert the UpdateItem call sets the top-level scope attribute.

    One test per scope field (all five).  The stub captures UpdateExpression
    and ExpressionAttributeValues; asserts:
    - ``ExpressionAttributeNames`` maps ``#scope_field`` to the scope field name.
    - ``ExpressionAttributeValues`` carries ``:scope_value`` with the correct value.
    - The UpdateExpression includes ``#scope_field = :scope_value``.
    - The nested scope is preserved in the finding dict passed in (no mutation).
    """

    @pytest.mark.parametrize("scope_field,scope_value", SCOPE_CASES)
    def test_update_item_sets_top_level_scope_attribute(self, scope_field, scope_value):
        """Top-level scope attribute set in UpdateExpression on the UpdateItem path."""
        finding_run1 = _make_finding_for_scope(scope_field, scope_value, inputs_hash=_HASH_A)
        finding_run2 = _make_finding_for_scope(scope_field, scope_value, inputs_hash=_HASH_B)
        finding_run2["finding_key"] = finding_run1["finding_key"]
        finding_run2["finding_id"] = finding_run1["finding_id"]

        client, stubber = _make_ddb_client()
        captured_update_kwargs: list[dict] = []

        original_update = client.update_item

        def _capturing_update(**kwargs):
            captured_update_kwargs.append(kwargs)
            return original_update(**kwargs)

        client.update_item = _capturing_update  # type: ignore[method-assign]

        # Serialise the live row for the Query stub.
        from boto3.dynamodb.types import TypeSerializer
        _ser = TypeSerializer()
        from agents.tier2.writer import _coerce_floats, _to_ddb_item

        live_row_ddb = _to_ddb_item(finding_run1)

        with stubber:
            # Query returns the live row (triggers UpdateItem path).
            stubber.add_response(
                "query",
                {"Items": [live_row_ddb], "Count": 1},
                expected_params={
                    "TableName": TABLE_NAME,
                    "IndexName": "by_finding_key",
                    "KeyConditionExpression": "finding_key = :fk",
                    "FilterExpression": "expires_at > :now",
                    "ExpressionAttributeValues": ANY,
                },
            )
            stubber.add_response(
                "update_item",
                {},
                expected_params={
                    "TableName": TABLE_NAME,
                    "Key": ANY,
                    "UpdateExpression": ANY,
                    "ExpressionAttributeNames": ANY,
                    "ExpressionAttributeValues": ANY,
                },
            )
            write_finding(
                finding_run2,
                table_name=TABLE_NAME,
                dynamodb_client=client,
                utc_now_fn=lambda: "2026-09-19T04:00:00Z",
            )

        assert len(captured_update_kwargs) == 1, "Exactly one update_item call expected."
        kw = captured_update_kwargs[0]

        # ExpressionAttributeNames must map #scope_field → scope_field.
        attr_names = kw.get("ExpressionAttributeNames", {})
        assert "#scope_field" in attr_names, (
            f"ExpressionAttributeNames must contain '#scope_field'; got {attr_names!r}"
        )
        assert attr_names["#scope_field"] == scope_field, (
            f"#scope_field must map to '{scope_field}'; got {attr_names['#scope_field']!r}"
        )

        # ExpressionAttributeValues must carry :scope_value with correct value.
        attr_values = kw.get("ExpressionAttributeValues", {})
        assert ":scope_value" in attr_values, (
            f"ExpressionAttributeValues must contain ':scope_value'; got keys {sorted(attr_values.keys())}"
        )
        assert _deserialise(attr_values[":scope_value"]) == scope_value, (
            f":scope_value must equal {scope_value!r}; "
            f"got {_deserialise(attr_values[':scope_value'])!r}"
        )

        # UpdateExpression must reference #scope_field = :scope_value.
        update_expr = kw.get("UpdateExpression", "")
        assert "#scope_field = :scope_value" in update_expr, (
            f"UpdateExpression must contain '#scope_field = :scope_value'; got: {update_expr!r}"
        )

    def test_update_item_sets_vin(self):
        self.test_update_item_sets_top_level_scope_attribute("vin", "1HGCM82633A004352")

    def test_update_item_sets_owner_id(self):
        self.test_update_item_sets_top_level_scope_attribute("owner_id", "owner-uuid-0001")

    def test_update_item_sets_fleet_id(self):
        self.test_update_item_sets_top_level_scope_attribute("fleet_id", "fleet-uuid-aaaa")

    def test_update_item_sets_dealership_id(self):
        """dealership_id case — required to FAIL when mutation (a) is applied."""
        self.test_update_item_sets_top_level_scope_attribute("dealership_id", "dealer-uuid-bbbb")

    def test_update_item_sets_district_id(self):
        self.test_update_item_sets_top_level_scope_attribute("district_id", "district-uuid-cccc")


# ---------------------------------------------------------------------------
# T10.2 Accept 3 — Artifact/item boundary: returned dict has NO extra scope attr
# ---------------------------------------------------------------------------

class TestArtifactItemBoundary:
    """The Finding dict returned by write_finding() must NOT carry the top-level
    scope attribute — only the DDB item does.

    write_finding() returns a ``{"action": ..., "finding_id": ...}`` dict, not
    the full Finding; this test confirms the *caller's finding dict* is not
    mutated to carry the projected attribute (the contract says the caller's
    dict must not gain the attribute).

    Mutation guard for (b): if the projected attribute is written into the
    returned artifact dict as well, this test MUST fail.
    """

    @pytest.mark.parametrize("scope_field,scope_value", SCOPE_CASES)
    def test_artifact_dict_does_not_gain_scope_attribute(self, scope_field, scope_value):
        """Caller's finding dict must not be mutated to carry the projected attribute."""
        finding = _make_finding_for_scope(scope_field, scope_value)

        # Capture the state of the finding dict BEFORE write_finding().
        # The projected attribute must NOT appear after the call.
        assert scope_field not in finding, (
            f"Test setup error: '{scope_field}' should not be a top-level key "
            "in a freshly constructed Finding."
        )

        client, stubber = _make_ddb_client()

        with stubber:
            _stub_query_no_results(stubber)
            stubber.add_response(
                "put_item",
                {},
                expected_params={"TableName": TABLE_NAME, "Item": ANY},
            )
            result = write_finding(
                finding,
                table_name=TABLE_NAME,
                dynamodb_client=client,
                utc_now_fn=lambda: "2026-09-19T03:01:00Z",
            )

        # The result dict is {"action": "put", "finding_id": ...} — it must not
        # carry the scope attribute.
        assert scope_field not in result, (
            f"write_finding() return value must not carry the top-level scope "
            f"attribute '{scope_field}'; got result keys: {sorted(result.keys())}"
        )

        # The caller's finding dict must not have been mutated.
        assert scope_field not in finding, (
            f"write_finding() mutated the caller's finding dict: "
            f"'{scope_field}' appeared after the call. The denormalised attribute "
            "must live ONLY in the DDB item, not in the passed-in dict."
        )

    def test_artifact_boundary_vin(self):
        self.test_artifact_dict_does_not_gain_scope_attribute("vin", "1HGCM82633A004352")

    def test_artifact_boundary_owner_id(self):
        self.test_artifact_dict_does_not_gain_scope_attribute("owner_id", "owner-uuid-0001")

    def test_artifact_boundary_fleet_id(self):
        self.test_artifact_dict_does_not_gain_scope_attribute("fleet_id", "fleet-uuid-aaaa")

    def test_artifact_boundary_dealership_id(self):
        self.test_artifact_dict_does_not_gain_scope_attribute("dealership_id", "dealer-uuid-bbbb")

    def test_artifact_boundary_district_id(self):
        self.test_artifact_dict_does_not_gain_scope_attribute("district_id", "district-uuid-cccc")


# ---------------------------------------------------------------------------
# T10.2 Accept 4 — D2 idempotency: inputs_hash unchanged by denormalisation
# ---------------------------------------------------------------------------

class TestInputsHashIdempotency:
    """inputs_hash must be byte-identical regardless of the denormalised attribute.

    inputs_hash is computed in runner.py._build_artifact over ADP input rows
    BEFORE write_finding() is called.  It is passed in as a field of the
    Finding dict.  write_finding() and _put_item() / _update_item() must not
    alter it.

    Mutation guard for (c): if inputs_hash were recomputed to include the
    denormalised attribute, two identical runs would hash differently (one
    before and one after the attribute is written) and D2 idempotency would
    break.  This test proves inputs_hash is untouched.
    """

    def test_inputs_hash_unchanged_after_write(self):
        """inputs_hash in the finding dict is identical before and after the write."""
        finding = _make_finding_for_scope("vin", "1HGCM82633A004352", inputs_hash=_HASH_A)
        hash_before = finding["inputs_hash"]

        client, stubber = _make_ddb_client()

        with stubber:
            _stub_query_no_results(stubber)
            stubber.add_response(
                "put_item",
                {},
                expected_params={"TableName": TABLE_NAME, "Item": ANY},
            )
            write_finding(
                finding,
                table_name=TABLE_NAME,
                dynamodb_client=client,
                utc_now_fn=lambda: "2026-09-19T03:01:00Z",
            )

        hash_after = finding["inputs_hash"]
        assert hash_after == hash_before, (
            "inputs_hash must not be altered by write_finding(). "
            f"Before: {hash_before!r}, after: {hash_after!r}"
        )

    def test_two_identical_findings_are_noop_on_second_write(self):
        """Identical inputs_hash on a live row → no-op (D2 retry-safety).

        This is the idempotency property: the same run hash arriving twice
        in the same execution is a no-op even after the denormalised attribute
        lands on the item.

        Mutation guard for (c): if the denormalised attribute is incorporated
        into the inputs_hash, the second write would compute a DIFFERENT hash
        (the item now has the extra attribute) and would NOT be a no-op —
        breaking D2 retry-safety.  This test MUST fail when mutation (c)
        is applied.
        """
        finding = _make_finding_for_scope("vin", "1HGCM82633A004352", inputs_hash=_HASH_A)

        from boto3.dynamodb.types import TypeSerializer
        from agents.tier2.writer import _coerce_floats, _to_ddb_item

        # The "live row" returned by the Query already has the same inputs_hash.
        live_row_ddb = _to_ddb_item(finding)

        client, stubber = _make_ddb_client()

        with stubber:
            # Query returns the live row with the SAME inputs_hash → noop path.
            stubber.add_response(
                "query",
                {"Items": [live_row_ddb], "Count": 1},
                expected_params={
                    "TableName": TABLE_NAME,
                    "IndexName": "by_finding_key",
                    "KeyConditionExpression": "finding_key = :fk",
                    "FilterExpression": "expires_at > :now",
                    "ExpressionAttributeValues": ANY,
                },
            )
            result = write_finding(
                finding,
                table_name=TABLE_NAME,
                dynamodb_client=client,
                utc_now_fn=lambda: "2026-09-19T03:01:00Z",
            )

        assert result["action"] == "noop", (
            "Identical inputs_hash on a live row must produce action='noop'. "
            f"Got: {result['action']!r}. D2 retry-safety is broken if this fails."
        )

    def test_inputs_hash_not_in_put_item_item_key_set(self):
        """The denormalised attribute does not influence inputs_hash.

        Verify by checking that the inputs_hash stored in the DDB item is the
        same string that was passed in — not recomputed to include the extra
        attribute.
        """
        finding = _make_finding_for_scope("fleet_id", "fleet-uuid-aaaa", inputs_hash=_HASH_B)
        expected_hash = finding["inputs_hash"]

        client, stubber = _make_ddb_client()
        captured_items: list[dict] = []

        original_put = client.put_item

        def _capturing_put(**kwargs):
            captured_items.append(kwargs["Item"])
            return original_put(**kwargs)

        client.put_item = _capturing_put  # type: ignore[method-assign]

        with stubber:
            _stub_query_no_results(stubber)
            stubber.add_response(
                "put_item",
                {},
                expected_params={"TableName": TABLE_NAME, "Item": ANY},
            )
            write_finding(
                finding,
                table_name=TABLE_NAME,
                dynamodb_client=client,
                utc_now_fn=lambda: "2026-09-19T03:01:00Z",
            )

        assert len(captured_items) == 1
        item = captured_items[0]
        stored_hash = _deserialise(item["inputs_hash"])
        assert stored_hash == expected_hash, (
            "inputs_hash stored in the DDB item must equal the value that was "
            f"passed in.  Expected {expected_hash!r}, got {stored_hash!r}. "
            "The denormalised attribute must NOT be incorporated into inputs_hash."
        )
