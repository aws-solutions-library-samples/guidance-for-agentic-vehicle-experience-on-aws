"""
Tests for the D2 in-place-update algorithm — ``agents/tier2/writer.py``.

Spec source:
    .kiro/specs/2026-09-18-avx-tier2-ios-cards-and-actions/tasks.md § Group 2
    → task "Implement `finding_key` stable-identity writer + in-place-update
      algorithm (Addendum D2)"
    decisions.md lines 107-165

Four required test cases (per task Verify):
    (a) same finding_key + drifted inputs across two writes = ONE row, ONE
        finding_id, audit_trail length 2.
    (b) same finding_key + identical inputs_hash within a single execution =
        no second write (retry-safety).
    (c) different finding_key = new row + new finding_id.
    (d) expired existing row + same finding_key = new row + new finding_id.

Mutation contract:
    Verified externally — see decisions-fragment-finding-key-writer.md.
    Swap the writer to unconditional PutItem keyed on inputs_hash; test (a)
    MUST fail (two rows instead of one, finding_id drifts).

Design rules applied (from task preamble):
    1. Never read prose in a guard — compare structured data.
    2. Mutation values come from OUTSIDE the assertion's own vocabulary.
    3. "Covered by construction" is a claim; measure and say what was measured.
    4. Restore from cp copy, NEVER git checkout.

All tests use a DDB Stubber (botocore.stub.Stubber); no network calls.
"""

from __future__ import annotations

import hashlib
import json
import uuid

import boto3
from botocore.stub import ANY, Stubber

from agents.tier2.writer import compute_finding_key, write_finding

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TABLE_NAME = "vsa-test-avx-findings"
AGENT_ID = "agent-2-vehicle-health"
SAMPLE_VIN = "1HGCM82633A004352"
FINDING_KIND = "health.battery"

# Two deterministic inputs hashes that are DIFFERENT from each other and from
# any string used in the assertions (Rule 2: values from outside the assertion's
# vocabulary).
_HASH_RUN_1 = hashlib.sha256(b"adp-rows-run-1-xyzzy").hexdigest()
_HASH_RUN_2 = hashlib.sha256(b"adp-rows-run-2-plugh").hexdigest()

# A third hash used only as a mutation injection (Rule 2: different from both).
_HASH_MUTATION_PROBE = hashlib.sha256(b"mutation-probe-distinct-3456789").hexdigest()

assert _HASH_RUN_1 != _HASH_RUN_2
assert _HASH_MUTATION_PROBE != _HASH_RUN_1
assert _HASH_MUTATION_PROBE != _HASH_RUN_2


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_ddb_client():
    client = boto3.client("dynamodb", region_name="us-west-2")
    return client, Stubber(client)


def _finding_key_for_vin(vin: str = SAMPLE_VIN) -> str:
    return compute_finding_key(
        agent_id=AGENT_ID,
        scope_field="vin",
        scope_value=vin,
        finding_kind=FINDING_KIND,
    )


def _make_finding(
    *,
    vin: str = SAMPLE_VIN,
    inputs_hash: str = _HASH_RUN_1,
    finding_id: str | None = None,
    computed_at: str = "2026-09-18T02:00:00Z",
    expires_at: str = "2026-09-26T02:00:00Z",
    audit_trail: list | None = None,
) -> dict:
    """Build a minimal schema-conforming Finding for tests."""
    fk = _finding_key_for_vin(vin)
    if finding_id is None:
        finding_id = f"FIND-{uuid.uuid4()}"
    if audit_trail is None:
        audit_trail = []
    return {
        "finding_id": finding_id,
        "finding_key": fk,
        "agent_id": AGENT_ID,
        "agent_version": "1.0.0",
        "scope": {"vin": vin},
        "severity": "routine",
        "computed_at": computed_at,
        "expires_at": expires_at,
        "confidence": "high",
        "finding_kind": FINDING_KIND,
        "headline": "Battery is healthy.",
        "detail": "State of health 92%.",
        "evidence": [
            {
                "kind": "adp.battery.soh",
                "source": "energy_usage",
                "source_ref": "athena-query-001",
                "value": {"state_of_health_pct": 92},
                "computed_at": computed_at,
                "provenance": "live",
            }
        ],
        "inputs_hash": inputs_hash,
        "tokens_used": 0,
        "cost_usd": 0.0,
        "audit_trail": audit_trail,
    }


# ── Stubber response builders ────────────────────────────────────────────────

def _stub_query_no_results(stubber: Stubber) -> None:
    """Stub a Query that returns no items (no live row for this finding_key)."""
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


def _stub_query_live_row(stubber: Stubber, row: dict) -> None:
    """Stub a Query that returns one live row (serialised to DDB wire format)."""
    from boto3.dynamodb.types import TypeSerializer
    _ser = TypeSerializer()

    def _to_ddb(v):
        from decimal import Decimal
        if isinstance(v, float):
            v = Decimal(str(v))
        return _ser.serialize(v)

    ddb_item = {k: _to_ddb(v) for k, v in row.items() if not isinstance(v, (list, dict))}
    # Serialise complex types
    for k, v in row.items():
        if isinstance(v, (list, dict)):
            from agents.tier2.writer import _to_ddb_item, _coerce_floats
            if isinstance(v, list):
                ddb_item[k] = _ser.serialize(_coerce_floats(v))
            else:
                ddb_item[k] = _ser.serialize(_coerce_floats(v))

    stubber.add_response(
        "query",
        {"Items": [ddb_item], "Count": 1},
        expected_params={
            "TableName": TABLE_NAME,
            "IndexName": "by_finding_key",
            "KeyConditionExpression": "finding_key = :fk",
            "FilterExpression": "expires_at > :now",
            "ExpressionAttributeValues": ANY,
        },
    )


def _stub_put_item(stubber: Stubber) -> None:
    """Stub an unconditional PutItem (new row)."""
    stubber.add_response(
        "put_item",
        {},
        expected_params={
            "TableName": TABLE_NAME,
            "Item": ANY,
        },
    )


def _stub_update_item(stubber: Stubber) -> None:
    """Stub an UpdateItem (in-place update)."""
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


# ---------------------------------------------------------------------------
# Test (a): same finding_key + drifted inputs → ONE row, ONE finding_id,
#           audit_trail length 2
# ---------------------------------------------------------------------------

class TestSameFindingKeyDriftedInputs:
    """Addendum D2 headline test.

    Two writes for the same open condition (same finding_key) with DIFFERENT
    inputs (drifted ADP data, different inputs_hash) must:
    - Produce exactly ONE logical row (UpdateItem on the second write, not PutItem).
    - Preserve the finding_id from the first write.
    - Append one audit_trail entry per UpdateItem call so trail length equals
      the number of in-place updates.
    """

    def test_second_write_is_update_not_put(self):
        """The second call to write_finding issues UpdateItem, not a second PutItem."""
        client, stubber = _make_ddb_client()

        # Run 1 finding — no live row exists yet.
        finding_1 = _make_finding(inputs_hash=_HASH_RUN_1, computed_at="2026-09-18T02:00:00Z")
        finding_id_1 = finding_1["finding_id"]

        # Run 2 finding — SAME finding_key, DIFFERENT inputs_hash (drifted data).
        finding_2 = _make_finding(
            inputs_hash=_HASH_RUN_2,
            computed_at="2026-09-19T02:00:00Z",
            expires_at="2026-09-27T02:00:00Z",
        )
        # Both have the same finding_key by construction (same vin/agent_id/finding_kind).
        assert finding_1["finding_key"] == finding_2["finding_key"], (
            "Both findings must share the same finding_key for this test to be valid. "
            f"Run 1: {finding_1['finding_key'][:12]}…, Run 2: {finding_2['finding_key'][:12]}…"
        )
        # They must differ in inputs_hash (drifted inputs).
        assert finding_1["inputs_hash"] != finding_2["inputs_hash"], (
            "Inputs hashes must differ to represent drifted ADP data. "
            "If they are the same this tests the retry-safety path, not the drift path."
        )

        with stubber:
            # Write 1: no live row → PutItem.
            _stub_query_no_results(stubber)
            _stub_put_item(stubber)
            result_1 = write_finding(
                finding_1,
                table_name=TABLE_NAME,
                dynamodb_client=client,
                utc_now_fn=lambda: "2026-09-18T02:01:00Z",
            )

            # Write 2: live row with DIFFERENT inputs_hash → UpdateItem.
            _stub_query_live_row(stubber, finding_1)
            _stub_update_item(stubber)
            result_2 = write_finding(
                finding_2,
                table_name=TABLE_NAME,
                dynamodb_client=client,
                utc_now_fn=lambda: "2026-09-19T02:01:00Z",
            )

        stubber.assert_no_pending_responses()

        # Assertions — Rule 1: compare structured data, not prose.
        assert result_1["action"] == "put", (
            f"First write must be 'put' (new row); got {result_1['action']!r}"
        )
        assert result_2["action"] == "update", (
            f"Second write with drifted inputs must be 'update' (in-place); "
            f"got {result_2['action']!r}.  "
            "If this is 'put', the writer is keying on inputs_hash instead of finding_key."
        )

        # ONE finding_id: the second write must preserve the first write's finding_id.
        assert result_2["finding_id"] == finding_id_1, (
            f"finding_id must be preserved across in-place updates.\n"
            f"  Write 1 minted: {finding_id_1}\n"
            f"  Write 2 returned: {result_2['finding_id']}\n"
            "If the IDs differ, the writer created a new row instead of updating."
        )

    def test_audit_trail_length_after_two_writes(self):
        """
        After the initial PutItem and one UpdateItem, the audit_trail has length 2.

        The first write stores the Finding with audit_trail=[].  The UpdateItem
        call appends one entry.  We verify the entry structure by inspecting the
        ExpressionAttributeValues passed to update_item.

        Rule 3 — "Covered by construction" is a claim, not a measurement.
        We capture the actual UpdateExpression arguments and assert the
        new_audit_entry list has length 1 (one entry appended per update).
        """
        client, stubber = _make_ddb_client()

        finding_1 = _make_finding(inputs_hash=_HASH_RUN_1)
        finding_2 = _make_finding(
            inputs_hash=_HASH_RUN_2,
            computed_at="2026-09-19T02:00:00Z",
            expires_at="2026-09-27T02:00:00Z",
        )

        captured_update_calls: list[dict] = []

        with stubber:
            # Write 1: PutItem.
            _stub_query_no_results(stubber)
            _stub_put_item(stubber)
            write_finding(
                finding_1,
                table_name=TABLE_NAME,
                dynamodb_client=client,
                utc_now_fn=lambda: "2026-09-18T02:01:00Z",
            )

            # Write 2: capture the UpdateItem call arguments.
            _stub_query_live_row(stubber, finding_1)

            # Override update_item to capture args before delegating to stubber.
            _real_update = client.update_item

            def _capturing_update(**kwargs):
                captured_update_calls.append(kwargs)
                return {}

            client.update_item = _capturing_update

            write_finding(
                finding_2,
                table_name=TABLE_NAME,
                dynamodb_client=client,
                utc_now_fn=lambda: "2026-09-19T02:01:00Z",
            )

        # We captured one update_item call.
        assert len(captured_update_calls) == 1, (
            f"Expected exactly 1 UpdateItem call; got {len(captured_update_calls)}"
        )

        expr_values = captured_update_calls[0]["ExpressionAttributeValues"]
        # ":new_audit_entry" must be a list of length 1.
        new_audit_entry_attr = expr_values[":new_audit_entry"]
        # DynamoDB wire format: {"L": [{"M": {...}}]}
        assert "L" in new_audit_entry_attr, (
            f"':new_audit_entry' must be a DynamoDB List; got keys: "
            f"{list(new_audit_entry_attr.keys())}"
        )
        entries_list = new_audit_entry_attr["L"]
        assert len(entries_list) == 1, (
            f"Each UpdateItem call appends exactly 1 audit entry; "
            f"got {len(entries_list)}"
        )

        # The entry must carry the required fields per finding_audit_entry schema.
        entry_map = entries_list[0]["M"]
        required_keys = {"at", "agent_version", "inputs_hash", "from_expires_at", "to_expires_at"}
        present_keys = set(entry_map.keys())
        missing = required_keys - present_keys
        assert not missing, (
            f"audit_trail entry is missing required keys: {sorted(missing)}. "
            f"Present: {sorted(present_keys)}"
        )

        # The appended inputs_hash must match run 2's hash.
        appended_inputs_hash = entry_map["inputs_hash"]["S"]
        assert appended_inputs_hash == _HASH_RUN_2, (
            f"audit_trail entry must carry run 2's inputs_hash.\n"
            f"  Expected: {_HASH_RUN_2[:12]}…\n"
            f"  Got:      {appended_inputs_hash[:12]}…"
        )

        # from_expires_at must match the original row's expires_at (run 1).
        from_expires = entry_map["from_expires_at"]["S"]
        assert from_expires == finding_1["expires_at"], (
            f"audit_trail entry from_expires_at must match run 1's expires_at.\n"
            f"  Expected: {finding_1['expires_at']}\n"
            f"  Got:      {from_expires}"
        )


# ---------------------------------------------------------------------------
# Test (b): same finding_key + identical inputs_hash → no second write (retry-safety)
# ---------------------------------------------------------------------------

class TestSameFindingKeySameInputsHash:
    """Retry-safety: D2 narrowed role for inputs_hash.

    If two attempts in the same execution share the same inputs_hash AND the
    same finding_key, the second is a no-op.  This prevents a retry from
    appending a duplicate audit_trail entry or re-issuing an UpdateItem.
    """

    def test_second_call_with_same_inputs_hash_is_noop(self):
        """Same finding_key + same inputs_hash → action='noop', no DDB write call."""
        client, stubber = _make_ddb_client()

        finding = _make_finding(inputs_hash=_HASH_RUN_1)

        with stubber:
            # First call: no live row → PutItem.
            _stub_query_no_results(stubber)
            _stub_put_item(stubber)
            result_1 = write_finding(
                finding,
                table_name=TABLE_NAME,
                dynamodb_client=client,
                utc_now_fn=lambda: "2026-09-18T02:01:00Z",
            )

            # Second call: live row with SAME inputs_hash → no-op.
            # The stub returns a live row carrying the same inputs_hash.
            _stub_query_live_row(stubber, finding)

            result_2 = write_finding(
                finding,
                table_name=TABLE_NAME,
                dynamodb_client=client,
                utc_now_fn=lambda: "2026-09-18T02:02:00Z",
            )

        # No pending responses: the second call consumed the Query stub but
        # did NOT consume a PutItem or UpdateItem stub — proving no write happened.
        stubber.assert_no_pending_responses()

        assert result_1["action"] == "put", (
            f"First write must be 'put'; got {result_1['action']!r}"
        )
        assert result_2["action"] == "noop", (
            f"Second write with identical inputs_hash must be 'noop'; "
            f"got {result_2['action']!r}. "
            "The retry-safety path should detect the matching inputs_hash and return early."
        )

        # finding_id must still be the original one (no new ID was minted).
        assert result_2["finding_id"] == finding["finding_id"], (
            f"noop must return the existing finding_id, not a new one.\n"
            f"  Expected: {finding['finding_id']}\n"
            f"  Got:      {result_2['finding_id']}"
        )


# ---------------------------------------------------------------------------
# Test (c): different finding_key → new row + new finding_id
# ---------------------------------------------------------------------------

class TestDifferentFindingKey:
    """Different finding_key means a different open condition → new row, new finding_id."""

    def test_different_vin_produces_different_finding_key_and_new_row(self):
        """Two VINs → two different finding_keys → two independent PutItem calls."""
        VIN_A = "1HGCM82633A004352"
        VIN_B = "2T1BURHE0JC062391"  # different VIN — different finding_key

        fk_a = compute_finding_key(
            agent_id=AGENT_ID,
            scope_field="vin",
            scope_value=VIN_A,
            finding_kind=FINDING_KIND,
        )
        fk_b = compute_finding_key(
            agent_id=AGENT_ID,
            scope_field="vin",
            scope_value=VIN_B,
            finding_kind=FINDING_KIND,
        )

        # Rule 3: measure the claim that different VINs produce different keys.
        assert fk_a != fk_b, (
            f"Different VINs must produce different finding_keys.\n"
            f"  VIN A ({VIN_A}): {fk_a[:12]}…\n"
            f"  VIN B ({VIN_B}): {fk_b[:12]}…\n"
            "If they are equal, compute_finding_key is not including scope_value."
        )

        client, stubber = _make_ddb_client()

        finding_a = _make_finding(vin=VIN_A, inputs_hash=_HASH_RUN_1)
        finding_b = _make_finding(vin=VIN_B, inputs_hash=_HASH_RUN_1)

        # The two finding_ids must be different (built with new uuids above).
        assert finding_a["finding_id"] != finding_b["finding_id"]

        with stubber:
            # VIN A: no live row → PutItem.
            _stub_query_no_results(stubber)
            _stub_put_item(stubber)
            result_a = write_finding(
                finding_a,
                table_name=TABLE_NAME,
                dynamodb_client=client,
                utc_now_fn=lambda: "2026-09-18T02:01:00Z",
            )

            # VIN B: no live row → PutItem (independent row).
            _stub_query_no_results(stubber)
            _stub_put_item(stubber)
            result_b = write_finding(
                finding_b,
                table_name=TABLE_NAME,
                dynamodb_client=client,
                utc_now_fn=lambda: "2026-09-18T02:01:00Z",
            )

        stubber.assert_no_pending_responses()

        assert result_a["action"] == "put", (
            f"VIN A write must be 'put'; got {result_a['action']!r}"
        )
        assert result_b["action"] == "put", (
            f"VIN B write must also be 'put' (new row for different finding_key); "
            f"got {result_b['action']!r}"
        )

        # Different finding_ids — two independent rows.
        assert result_a["finding_id"] != result_b["finding_id"], (
            f"Two different finding_keys must produce two different finding_ids.\n"
            f"  A: {result_a['finding_id']}\n"
            f"  B: {result_b['finding_id']}"
        )


# ---------------------------------------------------------------------------
# Test (d): EXPIRED existing row + same finding_key → new row + new finding_id
# ---------------------------------------------------------------------------

class TestExpiredRowSameFindingKey:
    """An expired row is treated as 'no row' — the condition has closed.

    When the GSI Query filters by ``expires_at > now()``, an expired row is
    excluded.  The writer therefore takes the PutItem path and mints a new
    finding_id, preserving audit continuity correctly: the previous condition
    is closed (expired) and the new one is a fresh opening.

    This is the D2 contract: "reopening produces a new finding_id (the previous
    open expired first, freeing the key)."
    """

    def test_expired_row_yields_new_finding_id(self):
        """Expired existing row + same finding_key → PutItem with new finding_id."""
        client, stubber = _make_ddb_client()

        # The existing row in DDB is expired — NOT returned by the GSI query
        # (FilterExpression: expires_at > :now excludes it).
        # We model this by having the Query return no items (the expired row
        # does not pass the filter).

        # New finding to write — same finding_key as the (now-expired) old row.
        new_finding = _make_finding(
            inputs_hash=_HASH_RUN_2,
            computed_at="2026-09-26T02:00:00Z",
            expires_at="2026-10-04T02:00:00Z",
        )
        old_finding_id = f"FIND-{uuid.uuid4()}"  # the expired row's finding_id

        # Verify the finding_keys match (same condition, just expired and re-opened).
        old_finding_key = compute_finding_key(
            agent_id=AGENT_ID,
            scope_field="vin",
            scope_value=SAMPLE_VIN,
            finding_kind=FINDING_KIND,
        )
        assert old_finding_key == new_finding["finding_key"], (
            "Both the old and new findings must share the same finding_key."
        )

        # The new finding must have a DIFFERENT finding_id than the old expired row.
        assert old_finding_id != new_finding["finding_id"], (
            "New finding must have a freshly-minted finding_id, not the expired row's."
        )

        with stubber:
            # Query returns NO results (expired row excluded by FilterExpression).
            _stub_query_no_results(stubber)
            # PutItem with the new finding_id.
            _stub_put_item(stubber)

            result = write_finding(
                new_finding,
                table_name=TABLE_NAME,
                dynamodb_client=client,
                utc_now_fn=lambda: "2026-09-26T02:01:00Z",
            )

        stubber.assert_no_pending_responses()

        assert result["action"] == "put", (
            f"Write after an expired row must be 'put' (new row, new finding_id); "
            f"got {result['action']!r}"
        )
        # New finding_id — NOT the expired row's ID.
        assert result["finding_id"] == new_finding["finding_id"], (
            f"New row must carry the new finding's own finding_id.\n"
            f"  Expected: {new_finding['finding_id']}\n"
            f"  Got:      {result['finding_id']}"
        )
        # Must be different from the old expired finding_id.
        assert result["finding_id"] != old_finding_id, (
            f"New finding_id must differ from the expired row's finding_id.\n"
            f"  Expired row: {old_finding_id}\n"
            f"  New row:     {result['finding_id']}"
        )


# ---------------------------------------------------------------------------
# compute_finding_key: basic property tests (Rule 3 — measure the claims)
# ---------------------------------------------------------------------------

class TestComputeFindingKey:
    """Unit tests for the key-derivation function itself."""

    def test_returns_64_char_hex(self):
        """finding_key must be a 64-character lowercase hex string."""
        key = compute_finding_key(
            agent_id=AGENT_ID,
            scope_field="vin",
            scope_value=SAMPLE_VIN,
            finding_kind=FINDING_KIND,
        )
        assert len(key) == 64, f"Expected 64 chars; got {len(key)}: {key!r}"
        assert key == key.lower(), "finding_key must be lowercase hex"
        assert all(c in "0123456789abcdef" for c in key), (
            f"finding_key must contain only hex chars; got: {key!r}"
        )

    def test_same_inputs_produce_same_key(self):
        """Identical inputs produce identical keys (determinism)."""
        key_1 = compute_finding_key(
            agent_id=AGENT_ID,
            scope_field="vin",
            scope_value=SAMPLE_VIN,
            finding_kind=FINDING_KIND,
        )
        key_2 = compute_finding_key(
            agent_id=AGENT_ID,
            scope_field="vin",
            scope_value=SAMPLE_VIN,
            finding_kind=FINDING_KIND,
        )
        assert key_1 == key_2

    def test_different_vin_produces_different_key(self):
        """Different VIN → different key (scope_value is a component)."""
        key_a = compute_finding_key(
            agent_id=AGENT_ID, scope_field="vin", scope_value="VIN000001", finding_kind=FINDING_KIND
        )
        key_b = compute_finding_key(
            agent_id=AGENT_ID, scope_field="vin", scope_value="VIN000002", finding_kind=FINDING_KIND
        )
        assert key_a != key_b

    def test_different_finding_kind_produces_different_key(self):
        """Different finding_kind → different key."""
        key_a = compute_finding_key(
            agent_id=AGENT_ID, scope_field="vin", scope_value=SAMPLE_VIN,
            finding_kind="health.battery",
        )
        key_b = compute_finding_key(
            agent_id=AGENT_ID, scope_field="vin", scope_value=SAMPLE_VIN,
            finding_kind="health.tires",
        )
        assert key_a != key_b

    def test_different_scope_field_produces_different_key(self):
        """Different scope_field → different key (same value, different field)."""
        key_a = compute_finding_key(
            agent_id=AGENT_ID, scope_field="vin", scope_value="ID-001",
            finding_kind=FINDING_KIND,
        )
        key_b = compute_finding_key(
            agent_id=AGENT_ID, scope_field="fleet_id", scope_value="ID-001",
            finding_kind=FINDING_KIND,
        )
        assert key_a != key_b
