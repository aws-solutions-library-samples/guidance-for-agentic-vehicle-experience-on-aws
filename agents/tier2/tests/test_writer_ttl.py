"""
Tests for TTL attribute written by agents/tier2/writer.py (A1 retention spec).

Spec source:
    .kiro/specs/2026-10-27-avx-findings-retention/spec.md
    § Design B, A1 Interface in tasks.md

TTL rules (A1 Interface):
- attribute `ttl` (Number, epoch seconds) = expires_at + 7 days (604,800 s)
- Set on new row and moved in the same write whenever expires_at moves
  (both writer.py update paths incl. _refresh_expiry, and rebalancing writes)
- Only when no `recommended_action` AND agent_id not in {agent-fi-recall-warranty,
  agent-fi-quality}
- A malformed `expires_at` writes no `ttl` (fail safe: row is kept)

Tests that assert PRESENCE (RED before implementation):
  test_eligible_new_row_has_ttl
  test_eligible_ttl_is_number_type
  test_eligible_update_sets_ttl
  test_refresh_with_ttl_sets_ttl
Tests that assert ABSENCE (GREEN before implementation, guard mutations):
  test_fi_item_no_ttl
  test_recall_agent_no_ttl
  test_quality_agent_no_ttl
  test_malformed_expires_at_no_ttl
  test_fi_item_update_no_ttl
  test_recall_update_no_ttl
  test_refresh_without_ttl_no_ttl
"""
from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timezone
from typing import Any

import boto3
import pytest
from boto3.dynamodb.types import TypeDeserializer
from botocore.stub import ANY, Stubber
from moto import mock_aws

from agents.tier2.writer import (
    _refresh_expiry,
    _to_ddb_item,
    compute_finding_key,
    write_finding,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TABLE = "vsa-test-avx-findings"
_des = TypeDeserializer()
GRACE = 604_800  # 7 days in seconds

# Expires-at chosen so epoch arithmetic is non-trivial
_EXPIRES_AT = "2026-11-03T10:00:00Z"


def _expected_ttl(expires_at: str) -> int:
    """Compute expected ttl = epoch(expires_at) + 604800."""
    dt = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
    return int(dt.timestamp()) + GRACE


# Deterministic hashes for inputs_hash (Rule 2: values from outside the assertion vocabulary)
_H1 = hashlib.sha256(b"ttl-test-run-A-xyzzy").hexdigest()
_H2 = hashlib.sha256(b"ttl-test-run-B-plugh").hexdigest()
_H3 = hashlib.sha256(b"ttl-test-run-C-quux").hexdigest()
assert _H1 != _H2
assert _H2 != _H3
assert _H1 != _H3

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ddb_client():
    return boto3.client("dynamodb", region_name="us-west-2")


def _make_finding(
    agent_id: str = "agent-2-vehicle-health",
    expires_at: str = _EXPIRES_AT,
    recommended_action: dict | None = None,
    inputs_hash: str = _H1,
    scope_field: str = "vin",
    scope_value: str = "1HGCM82633A004352",
) -> dict:
    """Build a minimal Finding dict for the given agent / scope."""
    fk = compute_finding_key(
        agent_id=agent_id,
        scope_field=scope_field,
        scope_value=scope_value,
        finding_kind="health.battery",
    )
    finding: dict = {
        "finding_id": f"FIND-{uuid.uuid4()}",
        "finding_key": fk,
        "agent_id": agent_id,
        "agent_version": "1.0.0",
        "scope": {scope_field: scope_value},
        "severity": "routine",
        "computed_at": "2026-10-27T10:00:00Z",
        "expires_at": expires_at,
        "confidence": "high",
        "finding_kind": "health.battery",
        "headline": "TTL test finding.",
        "detail": "Test detail.",
        "evidence": [],
        "inputs_hash": inputs_hash,
        "tokens_used": 0,
        "cost_usd": 0.0,
        "audit_trail": [],
    }
    if recommended_action is not None:
        finding["recommended_action"] = recommended_action
    return finding


def _stub_query_no_live(stubber: Stubber) -> None:
    """Stub the by_finding_key GSI query returning no live row (→ PutItem path)."""
    stubber.add_response(
        "query",
        {"Items": [], "Count": 0},
        expected_params={
            "TableName": TABLE,
            "IndexName": "by_finding_key",
            "KeyConditionExpression": "finding_key = :fk",
            "FilterExpression": "expires_at > :now",
            "ExpressionAttributeValues": ANY,
        },
    )


def _stub_query_live(stubber: Stubber, live: dict) -> None:
    """Stub the by_finding_key GSI query returning a live row (→ UpdateItem path)."""
    stubber.add_response(
        "query",
        {"Items": [_to_ddb_item(live)], "Count": 1},
        expected_params={
            "TableName": TABLE,
            "IndexName": "by_finding_key",
            "KeyConditionExpression": "finding_key = :fk",
            "FilterExpression": "expires_at > :now",
            "ExpressionAttributeValues": ANY,
        },
    )


def _capture_put_item_via_write_finding(finding: dict) -> dict:
    """Run write_finding on the new-row path; return the DDB Item dict."""
    client = _ddb_client()
    stubber = Stubber(client)
    captured: list[dict] = []
    orig = client.put_item

    def _intercept(**kw):
        captured.append(kw["Item"])
        return orig(**kw)

    client.put_item = _intercept  # type: ignore[method-assign]

    with stubber:
        _stub_query_no_live(stubber)
        stubber.add_response(
            "put_item", {},
            expected_params={"TableName": TABLE, "Item": ANY},
        )
        write_finding(
            finding,
            table_name=TABLE,
            dynamodb_client=client,
            utc_now_fn=lambda: "2026-10-27T10:01:00Z",
        )
    assert captured, "put_item was not called — writer.py may have taken a different path"
    return captured[0]


def _capture_update_item_via_write_finding(finding: dict) -> dict:
    """Run write_finding on the UpdateItem path; return the update_item kwargs."""
    # Build live row with _H2 so the new finding (_H1) triggers an update
    live = _make_finding(
        agent_id=finding["agent_id"],
        expires_at="2026-10-30T10:00:00Z",
        inputs_hash=_H2,
    )
    live["finding_key"] = finding["finding_key"]
    live["finding_id"] = finding["finding_id"]

    client = _ddb_client()
    stubber = Stubber(client)
    captured: list[dict] = []
    orig = client.update_item

    def _intercept(**kw):
        captured.append(kw)
        return orig(**kw)

    client.update_item = _intercept  # type: ignore[method-assign]

    with stubber:
        _stub_query_live(stubber, live)
        stubber.add_response(
            "update_item", {},
            expected_params={
                "TableName": TABLE,
                "Key": ANY,
                "UpdateExpression": ANY,
                "ExpressionAttributeNames": ANY,
                "ExpressionAttributeValues": ANY,
            },
        )
        write_finding(
            finding,
            table_name=TABLE,
            dynamodb_client=client,
            utc_now_fn=lambda: "2026-10-27T10:01:00Z",
        )
    assert captured, "update_item was not called"
    return captured[0]


# ---------------------------------------------------------------------------
# Tests: PutItem (new row) path
# ---------------------------------------------------------------------------


class TestPutItemTTL:
    """New rows must carry ttl iff eligible (no recommended_action, non-excluded agent)."""

    def test_eligible_new_row_has_ttl(self):
        """Health finding (agent-2-vehicle-health, no recommended_action) → ttl present."""
        finding = _make_finding(agent_id="agent-2-vehicle-health", expires_at=_EXPIRES_AT)
        item = _capture_put_item_via_write_finding(finding)
        assert "ttl" in item, (
            f"A1: eligible new row must carry ttl attribute; got keys {sorted(item.keys())}"
        )
        ttl_val = _des.deserialize(item["ttl"])
        expected = _expected_ttl(_EXPIRES_AT)
        assert ttl_val == expected, (
            f"A1: ttl must equal epoch(expires_at) + 604800; expected {expected}, got {ttl_val!r}"
        )

    def test_eligible_ttl_is_ddb_number(self):
        """ttl must be stored as a DynamoDB Number (not String or List)."""
        finding = _make_finding(agent_id="agent-2-vehicle-health", expires_at=_EXPIRES_AT)
        item = _capture_put_item_via_write_finding(finding)
        assert "ttl" in item
        assert "N" in item["ttl"], (
            f"A1: ttl must be DynamoDB Number type; got {item['ttl']!r}"
        )

    def test_fi_item_no_ttl(self):
        """FI item (has recommended_action) → no ttl.

        Mutation guard: if the recommended_action check is dropped, this fails.
        """
        finding = _make_finding(
            agent_id="agent-fi-cost",
            expires_at=_EXPIRES_AT,
            recommended_action={"action_kind": "approve"},
        )
        item = _capture_put_item_via_write_finding(finding)
        assert "ttl" not in item, (
            f"A1: FI item (has recommended_action) must NOT carry ttl; "
            f"got keys {sorted(item.keys())}"
        )

    def test_recall_agent_no_ttl(self):
        """agent-fi-recall-warranty → no ttl regardless of recommended_action.

        Mutation guard: if the agent exclusion check is dropped, this fails.
        """
        finding = _make_finding(agent_id="agent-fi-recall-warranty", expires_at=_EXPIRES_AT)
        item = _capture_put_item_via_write_finding(finding)
        assert "ttl" not in item, (
            f"A1: agent-fi-recall-warranty must NOT carry ttl; "
            f"got keys {sorted(item.keys())}"
        )

    def test_quality_agent_no_ttl(self):
        """agent-fi-quality → no ttl.

        Mutation guard: if the agent exclusion check is dropped, this fails.
        """
        finding = _make_finding(agent_id="agent-fi-quality", expires_at=_EXPIRES_AT)
        item = _capture_put_item_via_write_finding(finding)
        assert "ttl" not in item, (
            f"A1: agent-fi-quality must NOT carry ttl; got keys {sorted(item.keys())}"
        )

    def test_malformed_expires_at_no_ttl(self):
        """A malformed expires_at → no ttl (fail safe: row is kept).

        Mutation guard: if the try/except around the parse is removed, a ValueError
        would propagate rather than silently omitting ttl.
        """
        finding = _make_finding(
            agent_id="agent-2-vehicle-health", expires_at="not-a-valid-iso-date"
        )
        item = _capture_put_item_via_write_finding(finding)
        assert "ttl" not in item, (
            f"A1: malformed expires_at must produce no ttl (fail safe); "
            f"got keys {sorted(item.keys())}"
        )


# ---------------------------------------------------------------------------
# Tests: UpdateItem (in-place extension) path
# ---------------------------------------------------------------------------


class TestUpdateItemTTL:
    """Extension writes (UpdateItem) must move ttl whenever expires_at moves."""

    def test_eligible_update_sets_ttl(self):
        """Health finding extension → ttl in UpdateExpression and ExpressionAttributeValues."""
        finding = _make_finding(agent_id="agent-2-vehicle-health", expires_at=_EXPIRES_AT,
                                inputs_hash=_H1)
        kw = _capture_update_item_via_write_finding(finding)
        eav = kw.get("ExpressionAttributeValues", {})
        assert ":ttl" in eav, (
            f"A1: eligible extension must SET :ttl; got ExpressionAttributeValues keys "
            f"{sorted(eav.keys())}"
        )
        ttl_val = _des.deserialize(eav[":ttl"])
        expected = _expected_ttl(_EXPIRES_AT)
        assert ttl_val == expected, (
            f"A1: :ttl must equal epoch(expires_at) + 604800; expected {expected}, got {ttl_val!r}"
        )
        ue = kw.get("UpdateExpression", "")
        assert ":ttl" in ue, (
            f"A1: ttl must be referenced in UpdateExpression; got: {ue!r}"
        )

    def test_fi_item_update_no_ttl(self):
        """FI item extension → no ttl in UpdateExpression.

        Mutation guard: if the recommended_action check is dropped, this fails.
        """
        finding = _make_finding(
            agent_id="agent-fi-cost",
            expires_at=_EXPIRES_AT,
            recommended_action={"action_kind": "approve"},
            inputs_hash=_H1,
        )
        kw = _capture_update_item_via_write_finding(finding)
        eav = kw.get("ExpressionAttributeValues", {})
        assert ":ttl" not in eav, (
            f"A1: FI item extension must NOT set :ttl; got EAV keys {sorted(eav.keys())}"
        )

    def test_recall_agent_update_no_ttl(self):
        """agent-fi-recall-warranty extension → no ttl.

        Mutation guard: if the agent exclusion check is dropped, this fails.
        """
        finding = _make_finding(agent_id="agent-fi-recall-warranty", expires_at=_EXPIRES_AT,
                                inputs_hash=_H1)
        kw = _capture_update_item_via_write_finding(finding)
        eav = kw.get("ExpressionAttributeValues", {})
        assert ":ttl" not in eav


# ---------------------------------------------------------------------------
# Tests: _refresh_expiry (SET expires_at = :e, computed_at = :c extension path)
# ---------------------------------------------------------------------------


class TestRefreshExpiry:
    """_refresh_expiry with new_ttl → ttl in SET; without new_ttl → no ttl."""

    def _run_refresh(self, new_ttl=None) -> dict:
        """Call _refresh_expiry and capture the update_item kwargs."""
        client = _ddb_client()
        stubber = Stubber(client)
        captured: list[dict] = []
        orig = client.update_item

        def _intercept(**kw):
            captured.append(kw)
            return orig(**kw)

        client.update_item = _intercept  # type: ignore[method-assign]

        kwargs: dict = dict(
            table_name=TABLE,
            dynamodb_client=client,
            finding_id="FIND-00000000-0000-0000-0000-000000000001",
            expires_at=_EXPIRES_AT,
            computed_at="2026-10-27T10:01:00Z",
        )
        if new_ttl is not None:
            kwargs["new_ttl"] = new_ttl

        with stubber:
            # No expected_params: _refresh_expiry may add ExpressionAttributeNames
            # conditionally; let the interceptor capture the full call.
            stubber.add_response("update_item", {})
            result = _refresh_expiry(**kwargs)

        assert captured, "_refresh_expiry must issue an update_item call"
        return captured[0]

    def test_refresh_with_ttl_sets_ttl(self):
        """_refresh_expiry(new_ttl=X) adds ttl to the SET clause.

        RED before implementation: _refresh_expiry has no new_ttl parameter yet →
        TypeError or AssertionError.
        """
        expected = _expected_ttl(_EXPIRES_AT)
        kw = self._run_refresh(new_ttl=expected)
        eav = kw.get("ExpressionAttributeValues", {})
        assert ":ttl" in eav, (
            f"A1: _refresh_expiry(new_ttl=X) must SET :ttl; "
            f"got ExpressionAttributeValues keys {sorted(eav.keys())}"
        )
        assert _des.deserialize(eav[":ttl"]) == expected, (
            f"A1: refresh :ttl must equal new_ttl; expected {expected}, "
            f"got {_des.deserialize(eav[':ttl'])!r}"
        )

    def test_refresh_without_ttl_no_ttl(self):
        """_refresh_expiry() without new_ttl → no ttl in SET clause.

        Guards the backward-compatible default: callers that don't pass new_ttl
        (or pass None) must not accidentally set ttl on ineligible rows.
        """
        kw = self._run_refresh()  # no new_ttl
        eav = kw.get("ExpressionAttributeValues", {})
        assert ":ttl" not in eav, (
            f"A1: _refresh_expiry without new_ttl must NOT set ttl; "
            f"got ExpressionAttributeValues keys {sorted(eav.keys())}"
        )


# ---------------------------------------------------------------------------
# Helpers for refresh path (write_finding with refresh_on_noop=True)
# ---------------------------------------------------------------------------

_OLD_EXPIRES_AT = "2026-10-30T10:00:00Z"   # older than _EXPIRES_AT


def _capture_refresh_update_item(
    finding: dict,
    live_recommended_action: dict | None = None,
) -> dict:
    """Run write_finding on the refresh path; return the update_item kwargs.

    Builds a live row with the SAME inputs_hash so write_finding enters the
    noop / refresh branch.  live_recommended_action, if given, is set on the
    stored row (making it an FI item) but NOT on the incoming artifact.
    """
    live = _make_finding(
        agent_id=finding["agent_id"],
        expires_at=_OLD_EXPIRES_AT,             # older → refresh condition passes
        inputs_hash=finding["inputs_hash"],     # SAME → enters noop / refresh
    )
    live["finding_key"] = finding["finding_key"]
    live["finding_id"] = finding["finding_id"]
    if live_recommended_action is not None:
        live["recommended_action"] = live_recommended_action

    client = _ddb_client()
    stubber = Stubber(client)
    captured: list[dict] = []
    orig = client.update_item

    def _intercept(**kw):
        captured.append(kw)
        return orig(**kw)

    client.update_item = _intercept  # type: ignore[method-assign]

    with stubber:
        _stub_query_live(stubber, live)
        # _refresh_expiry calls update_item; no expected_params so stubber
        # accepts any call regardless of ExpressionAttributeNames presence.
        stubber.add_response("update_item", {})
        write_finding(
            finding,
            table_name=TABLE,
            dynamodb_client=client,
            utc_now_fn=lambda: "2026-10-27T10:01:00Z",
            refresh_on_noop=True,
        )

    assert captured, (
        "update_item was not called — write_finding may not have taken the refresh path"
    )
    return captured[0]


# ---------------------------------------------------------------------------
# Tests: REMOVE #ttl when _compute_ttl returns None (Critical 1)
# ---------------------------------------------------------------------------


class TestUpdateItemTTLRemove:
    """_update_item must REMOVE #ttl when the artifact is ineligible.

    A row that was informational (ttl set) and then gets recommended_action
    must have ttl stripped so DynamoDB's TTL scanner cannot delete a live
    FI queue item.

    These tests are RED before the fix:
      _update_item only sets ttl when eligible; it never removes it.
    """

    def test_fi_item_promoted_removes_ttl(self):
        """Row gaining recommended_action → REMOVE #ttl in UpdateExpression.

        Concrete scenario: a cost row was informational in run 1 (ttl set);
        run 2 emits recommended_action for the same finding_key.  The update
        must REMOVE #ttl so the FI queue item is never auto-deleted.

        Mutation guard (recorded in mutations-FA1.md): drop the REMOVE →
        this test fails because 'REMOVE' is absent or '#ttl' not after 'REMOVE'.
        """
        finding = _make_finding(
            agent_id="agent-fi-cost",
            expires_at=_EXPIRES_AT,
            recommended_action={"action_kind": "apply_cost_action"},
            inputs_hash=_H1,
        )
        kw = _capture_update_item_via_write_finding(finding)
        ue = kw.get("UpdateExpression", "")
        eav = kw.get("ExpressionAttributeValues", {})

        # Must NOT set :ttl
        assert ":ttl" not in eav, (
            f"A1/C1: FI item update must NOT set :ttl; got EAV keys {sorted(eav.keys())}"
        )
        # Must REMOVE #ttl
        remove_part = ue.split("REMOVE", 1)[-1] if "REMOVE" in ue else ""
        assert "#ttl" in remove_part, (
            f"A1/C1: FI item update must REMOVE #ttl; "
            f"got UpdateExpression: {ue!r}"
        )

    def test_malformed_expires_at_update_removes_ttl(self):
        """Malformed expires_at on update → REMOVE #ttl (fail-safe: kept not deleted).

        The Interface: 'a malformed expires_at REMOVEs ttl'.

        Mutation guard: drop the REMOVE → test fails.
        """
        finding = _make_finding(
            agent_id="agent-2-vehicle-health",
            expires_at="not-a-valid-iso",   # malformed
            inputs_hash=_H1,
        )
        kw = _capture_update_item_via_write_finding(finding)
        ue = kw.get("UpdateExpression", "")
        eav = kw.get("ExpressionAttributeValues", {})

        assert ":ttl" not in eav, (
            f"A1/C1: malformed expires_at update must NOT set :ttl; "
            f"got EAV keys {sorted(eav.keys())}"
        )
        remove_part = ue.split("REMOVE", 1)[-1] if "REMOVE" in ue else ""
        assert "#ttl" in remove_part, (
            f"A1/C1: malformed expires_at update must REMOVE #ttl; "
            f"got UpdateExpression: {ue!r}"
        )


# ---------------------------------------------------------------------------
# Tests: write_finding(refresh_on_noop=True) moves / removes ttl (Critical 2)
# ---------------------------------------------------------------------------


class TestWriteFindingRefreshTTL:
    """write_finding refresh path must move ttl for eligible rows and remove
    for ineligible ones.

    The existing TestRefreshExpiry tests _refresh_expiry directly with an
    explicit new_ttl; these tests go one level up (write_finding) so that
    the 'new_ttl=_compute_ttl(finding)' wire at line 406 is covered.

    Both tests are RED before the fix:
      line 406 uses _compute_ttl(finding) — the incoming artifact — not the
      stored row, so a FI live row would get :ttl SET; and no test caught
      it because the only write_finding+refresh test only checked :e.
    """

    def test_refresh_on_noop_eligible_live_row_moves_ttl(self):
        """Refresh of an eligible live row sets :ttl = epoch(new_expires_at) + GRACE.

        Mutation guard (recorded in mutations-FA1.md): delete
        `new_ttl=_compute_ttl(finding)` at the call site → test fails
        because :ttl is absent from the update_item call.
        """
        finding = _make_finding(
            agent_id="agent-2-vehicle-health",
            expires_at=_EXPIRES_AT,   # new (later) expires_at
            inputs_hash=_H1,
        )
        kw = _capture_refresh_update_item(finding, live_recommended_action=None)
        eav = kw.get("ExpressionAttributeValues", {})

        assert ":ttl" in eav, (
            f"A1/C2: refresh of eligible row must SET :ttl; "
            f"got EAV keys {sorted(eav.keys())}"
        )
        ttl_val = _des.deserialize(eav[":ttl"])
        expected = _expected_ttl(_EXPIRES_AT)
        assert ttl_val == expected, (
            f"A1/C2: refresh :ttl must equal epoch(expires_at) + 604800; "
            f"expected {expected}, got {ttl_val!r}"
        )

    def test_refresh_on_noop_fi_live_row_removes_ttl(self):
        """Refresh where the STORED row is an FI item must REMOVE #ttl, not SET it.

        Scenario: the live row carries recommended_action (made an FI item by
        the broker) but the incoming artifact has no recommended_action
        (informational re-emit with the same inputs_hash).  Using the incoming
        artifact's eligibility would wrongly SET ttl; the fix uses the stored
        row's recommended_action to determine eligibility.

        Mutation guard (recorded in mutations-FA1.md): use _compute_ttl(finding)
        instead of live-row eligibility → :ttl is SET instead of REMOVEd,
        so the assert ':ttl' not in eav fails.
        """
        finding = _make_finding(
            agent_id="agent-fi-cost",       # no recommended_action in the artifact
            expires_at=_EXPIRES_AT,
            inputs_hash=_H1,
        )
        kw = _capture_refresh_update_item(
            finding,
            live_recommended_action={"action_kind": "apply_cost_action"},
        )
        ue = kw.get("UpdateExpression", "")
        eav = kw.get("ExpressionAttributeValues", {})

        assert ":ttl" not in eav, (
            f"A1/C1+C2: refresh of FI live row must NOT set :ttl; "
            f"got EAV keys {sorted(eav.keys())}"
        )
        remove_part = ue.split("REMOVE", 1)[-1] if "REMOVE" in ue else ""
        assert "#ttl" in remove_part, (
            f"A1/C1+C2: refresh of FI live row must REMOVE #ttl; "
            f"got UpdateExpression: {ue!r}"
        )


# ---------------------------------------------------------------------------
# Helper: UpdateItem with arbitrary live-row extras (FA2 Critical)
# ---------------------------------------------------------------------------


def _capture_update_item_with_live_extras(
    finding: dict,
    live_extras: dict | None = None,
) -> dict:
    """Like _capture_update_item_via_write_finding but merges extra attrs into live row.

    Used by TestTtlExemptMarker to place recommended_action or ttl_exempt on the
    stored row while keeping the incoming artifact informational.
    """
    live = _make_finding(
        agent_id=finding["agent_id"],
        expires_at="2026-10-30T10:00:00Z",
        inputs_hash=_H2,  # different hash → update path
    )
    live["finding_key"] = finding["finding_key"]
    live["finding_id"] = finding["finding_id"]
    if live_extras:
        live.update(live_extras)

    client = _ddb_client()
    stubber = Stubber(client)
    captured: list[dict] = []
    orig = client.update_item

    def _intercept(**kw):
        captured.append(kw)
        return orig(**kw)

    client.update_item = _intercept  # type: ignore[method-assign]

    with stubber:
        _stub_query_live(stubber, live)
        stubber.add_response(
            "update_item", {},
            expected_params={
                "TableName": TABLE,
                "Key": ANY,
                "UpdateExpression": ANY,
                "ExpressionAttributeNames": ANY,
                "ExpressionAttributeValues": ANY,
            },
        )
        write_finding(
            finding,
            table_name=TABLE,
            dynamodb_client=client,
            utc_now_fn=lambda: "2026-10-27T10:01:00Z",
        )
    assert captured, "update_item was not called — writer.py may have taken a different path"
    return captured[0]


# ---------------------------------------------------------------------------
# Tests: sticky ttl_exempt marker (FA2 Critical)
# ---------------------------------------------------------------------------


class TestTtlExemptMarker:
    """FA2 Critical: a stored FI row re-emitted as informational must never acquire ttl.

    Spec rule: a row that carries or has carried ``recommended_action`` as far as the
    writer can see never gets ``ttl``.  A sticky ``ttl_exempt`` marker (SET by any write
    involving ``recommended_action``, never REMOVEd) preserves this invariant across
    subsequent informational re-emits where neither the stored row nor the artifact
    carries ``recommended_action``.

    Both tests below are RED before the fix:
        ``_update_item`` calls ``_compute_ttl(finding)`` with the incoming artifact only.
        An informational artifact has no ``recommended_action``, so ``_compute_ttl``
        returns a valid TTL and ``:ttl`` is SET in the UpdateExpression.
    """

    def test_stored_fi_row_informational_reemit_no_ttl(self):
        """Stored row has recommended_action; informational re-emit → REMOVE #ttl, no :ttl.

        Scenario: Day 1 a cost row became an FI item (recommended_action written).
        Day 2 the model returns no_action and the artifact drops recommended_action.
        _FI_REEMIT_ATTRS REMOVEs recommended_action from the stored row.  Without the
        fix, _update_item would SET :ttl because the artifact has no recommended_action.

        RED: ``_update_item`` uses ``_compute_ttl(finding)`` only → TTL computed → :ttl SET.

        Fix: pass live_row to _update_item; if the stored row carries recommended_action,
        merge it into the eligibility check → _compute_ttl returns None → REMOVE #ttl.

        Mutation guard (mutations-FA2.md § Critical):
            "decide from the artifact only" → :ttl SET → assert ":ttl" not in eav fails.
        """
        finding = _make_finding(
            agent_id="agent-fi-cost",
            expires_at=_EXPIRES_AT,
            # no recommended_action on the incoming artifact
            inputs_hash=_H1,
        )
        kw = _capture_update_item_with_live_extras(
            finding,
            live_extras={"recommended_action": {"action_kind": "apply_cost_action"}},
        )
        eav = kw.get("ExpressionAttributeValues", {})
        ue = kw.get("UpdateExpression", "")

        assert ":ttl" not in eav, (
            "FA2/C: stored FI row + informational re-emit must NOT set :ttl; "
            f"got EAV keys {sorted(eav.keys())}"
        )
        remove_part = ue.split("REMOVE", 1)[-1] if "REMOVE" in ue else ""
        assert "#ttl" in remove_part, (
            "FA2/C: stored FI row + informational re-emit must REMOVE #ttl; "
            f"got UpdateExpression: {ue!r}"
        )

    def test_stored_ttl_exempt_row_informational_reemit_no_ttl(self):
        """Stored row carries ttl_exempt=True (no RA now); artifact has no RA → no :ttl.

        Scenario: the stored row had its ttl_exempt SET by a previous write that
        involved recommended_action.  Now neither the stored row nor the artifact
        carries recommended_action.  The sticky marker must be honoured.

        RED: current code has no ttl_exempt concept; artifact has no recommended_action
        → ``_compute_ttl`` returns a TTL → :ttl is SET.

        Fix: ``_compute_ttl`` checks ``ttl_exempt`` on the combined dict; returns None.

        Mutation guard (mutations-FA2.md § Critical):
            "decide from the artifact only" → :ttl SET → assert ":ttl" not in eav fails.
        """
        finding = _make_finding(
            agent_id="agent-fi-cost",
            expires_at=_EXPIRES_AT,
            # no recommended_action on the incoming artifact
            inputs_hash=_H1,
        )
        kw = _capture_update_item_with_live_extras(
            finding,
            live_extras={"ttl_exempt": True},  # marker persisted from a prior FI write
        )
        eav = kw.get("ExpressionAttributeValues", {})
        ue = kw.get("UpdateExpression", "")

        assert ":ttl" not in eav, (
            "FA2/C: stored row with ttl_exempt + informational re-emit must NOT set :ttl; "
            f"got EAV keys {sorted(eav.keys())}"
        )
        remove_part = ue.split("REMOVE", 1)[-1] if "REMOVE" in ue else ""
        assert "#ttl" in remove_part, (
            "FA2/C: stored row with ttl_exempt + informational re-emit must REMOVE #ttl; "
            f"got UpdateExpression: {ue!r}"
        )


# ---------------------------------------------------------------------------
# Tests: ttl_exempt unit asserts (FA3 Critical unit pins)
# ---------------------------------------------------------------------------


class TestTtlExemptUnit:
    """FA3 Critical unit asserts — pin the five surviving writer mutations.

    Mutations caught (review-avx-cycle3.md § Critical):
    - M2: ``_put_item`` never SETs ``ttl_exempt``
    - M1: ``_update_item`` never SETs ``ttl_exempt`` (``if _should_set_exempt:`` → ``if False:``)
    - M4: marker SET only when the artifact carries ``recommended_action``
          (stored row's RA and stored ``ttl_exempt`` ignored in ``_should_set_exempt``)
    - M5: the refresh path drops ``"ttl_exempt": live_row.get("ttl_exempt")``
          from ``_live_for_ttl``

    M3 (neither PutItem nor UpdateItem SETs it) is caught by both M2 and M1 together
    and by the moto sequence test in ``TestMotoFiSequence``.
    """

    def test_fi_put_item_sets_ttl_exempt(self):
        """PutItem of an FI row (has recommended_action) stores ttl_exempt=True.

        Mutation guard (M2): ``_put_item`` never SETs ``ttl_exempt`` →
        ``"ttl_exempt" in item`` assertion fails.
        """
        finding = _make_finding(
            agent_id="agent-fi-cost",
            expires_at=_EXPIRES_AT,
            recommended_action={"action_kind": "approve"},
        )
        item = _capture_put_item_via_write_finding(finding)
        assert "ttl_exempt" in item, (
            "FA3/M2: FI PutItem must store ttl_exempt; got keys " + str(sorted(item.keys()))
        )
        assert item["ttl_exempt"] == {"BOOL": True}, (
            f"FA3/M2: ttl_exempt must be BOOL True; got {item['ttl_exempt']!r}"
        )

    def test_update_with_stored_fi_row_sets_ttl_exempt_val(self):
        """Stored row has recommended_action, artifact does not → update SETs :ttl_exempt_val.

        Mutation guards:
        - M1 (``if _should_set_exempt:`` → ``if False:``):
          no ``:ttl_exempt_val`` in EAV → assertion fails.
        - M4 (SET only when artifact carries RA):
          stored row's RA is ignored → ``_should_set_exempt=False`` → no ``:ttl_exempt_val``.
        """
        finding = _make_finding(
            agent_id="agent-fi-cost",
            expires_at=_EXPIRES_AT,
            # artifact has no recommended_action
            inputs_hash=_H1,
        )
        kw = _capture_update_item_with_live_extras(
            finding,
            live_extras={"recommended_action": {"action_kind": "apply_cost_action"}},
        )
        eav = kw.get("ExpressionAttributeValues", {})
        assert ":ttl_exempt_val" in eav, (
            "FA3/M1+M4: update of stored FI row must SET :ttl_exempt_val; "
            f"got EAV keys {sorted(eav.keys())}"
        )
        assert eav[":ttl_exempt_val"] == {"BOOL": True}, (
            f"FA3/M1+M4: :ttl_exempt_val must be BOOL True; got {eav[':ttl_exempt_val']!r}"
        )

    def test_refresh_of_ttl_exempt_row_removes_ttl(self):
        """Refresh of a row that holds ttl_exempt=True (no RA on either side) REMOVEs #ttl.

        Mutation guard (M5): the refresh path drops
        ``"ttl_exempt": live_row.get("ttl_exempt")`` from ``_live_for_ttl`` →
        ``_compute_ttl`` sees an eligible row → ``_new_ttl != None`` →
        ``remove_ttl=False`` → ``_refresh_expiry`` SETs ``#ttl`` instead of
        REMOVEing it → ``":ttl" not in eav`` assertion fails.
        """
        finding = _make_finding(
            agent_id="agent-fi-cost",
            expires_at=_EXPIRES_AT,
            # no recommended_action on the artifact
            inputs_hash=_H1,
        )
        # Build live row with ttl_exempt=True but no recommended_action.
        # Same inputs_hash → write_finding takes the noop/refresh branch.
        live = _make_finding(
            agent_id=finding["agent_id"],
            expires_at=_OLD_EXPIRES_AT,              # older → refresh condition passes
            inputs_hash=finding["inputs_hash"],       # SAME hash → refresh path
        )
        live["finding_key"] = finding["finding_key"]
        live["finding_id"] = finding["finding_id"]
        live["ttl_exempt"] = True                     # the sticky marker, no RA

        client = _ddb_client()
        stubber = Stubber(client)
        captured: list[dict] = []
        orig = client.update_item

        def _intercept(**kw):
            captured.append(kw)
            return orig(**kw)

        client.update_item = _intercept  # type: ignore[method-assign]

        with stubber:
            _stub_query_live(stubber, live)
            stubber.add_response("update_item", {})
            write_finding(
                finding,
                table_name=TABLE,
                dynamodb_client=client,
                utc_now_fn=lambda: "2026-10-27T10:01:00Z",
                refresh_on_noop=True,
            )

        assert captured, "update_item was not called — refresh path not taken"
        kw = captured[0]
        eav = kw.get("ExpressionAttributeValues", {})
        ue = kw.get("UpdateExpression", "")

        assert ":ttl" not in eav, (
            "FA3/M5: refresh of ttl_exempt row must NOT SET :ttl; "
            f"got EAV keys {sorted(eav.keys())}"
        )
        remove_part = ue.split("REMOVE", 1)[-1] if "REMOVE" in ue else ""
        assert "#ttl" in remove_part, (
            "FA3/M5: refresh of ttl_exempt row must REMOVE #ttl; "
            f"got UpdateExpression: {ue!r}"
        )

    def test_naive_expires_at_new_row_no_ttl(self):
        """A naive (no UTC offset) expires_at → no ttl on PutItem (fail safe).

        Mutation guard (S4a): removing ``if dt.tzinfo is None: return None`` in
        ``_compute_ttl`` causes the naive datetime to produce a local-tz epoch
        value instead of None → ttl IS written → ``"ttl" not in item`` fails.
        """
        finding = _make_finding(
            agent_id="agent-2-vehicle-health",
            expires_at="2026-11-03T10:00:00",  # naive: no 'Z' or '+00:00'
        )
        item = _capture_put_item_via_write_finding(finding)
        assert "ttl" not in item, (
            "FA3/S4a: naive (no UTC offset) expires_at must produce no ttl (fail safe); "
            f"got keys {sorted(item.keys())}"
        )

    def test_refresh_artifact_with_recommended_action_removes_ttl(self):
        """Refresh where the ARTIFACT has recommended_action (stored row does not) REMOVEs #ttl.

        Mutation guard (S4b): removing ``or finding.get("recommended_action")``
        from the ``_live_for_ttl`` dict in the refresh path causes the artifact's
        RA to be ignored → ``_compute_ttl`` sees no RA → computes a valid TTL →
        ``_refresh_expiry`` SETs ``#ttl`` → ``":ttl" not in eav`` fails.
        """
        finding = _make_finding(
            agent_id="agent-fi-cost",
            expires_at=_EXPIRES_AT,
            recommended_action={"action_kind": "approve"},  # artifact has RA
            inputs_hash=_H1,
        )
        # Stored row: same inputs_hash (→ refresh path), no recommended_action.
        live = _make_finding(
            agent_id=finding["agent_id"],
            expires_at=_OLD_EXPIRES_AT,
            inputs_hash=finding["inputs_hash"],   # SAME → refresh path
        )
        live["finding_key"] = finding["finding_key"]
        live["finding_id"] = finding["finding_id"]
        # No recommended_action and no ttl_exempt on the stored row.

        client = _ddb_client()
        stubber = Stubber(client)
        captured: list[dict] = []
        orig = client.update_item

        def _intercept(**kw):
            captured.append(kw)
            return orig(**kw)

        client.update_item = _intercept  # type: ignore[method-assign]

        with stubber:
            _stub_query_live(stubber, live)
            stubber.add_response("update_item", {})
            write_finding(
                finding,
                table_name=TABLE,
                dynamodb_client=client,
                utc_now_fn=lambda: "2026-10-27T10:01:00Z",
                refresh_on_noop=True,
            )

        assert captured, "update_item was not called"
        kw = captured[0]
        eav = kw.get("ExpressionAttributeValues", {})
        ue = kw.get("UpdateExpression", "")

        assert ":ttl" not in eav, (
            "FA3/S4b: refresh with artifact RA must NOT SET :ttl; "
            f"got EAV keys {sorted(eav.keys())}"
        )
        remove_part = ue.split("REMOVE", 1)[-1] if "REMOVE" in ue else ""
        assert "#ttl" in remove_part, (
            "FA3/S4b: refresh with artifact RA must REMOVE #ttl; "
            f"got UpdateExpression: {ue!r}"
        )


# ---------------------------------------------------------------------------
# Tests: moto sequence — FI put → re-emit × 2 → refresh (FA3 Critical moto)
# ---------------------------------------------------------------------------


class TestMotoFiSequence:
    """FA3 Critical: full moto sequence confirms no ttl and ttl_exempt=True at every step.

    Sequence (review-avx-cycle3.md § Critical, Method):
      1. FI put (artifact has ``recommended_action``)
      2. Informational re-emit (no RA, different inputs_hash) → UpdateItem
      3. Informational re-emit again (no RA, new inputs_hash) → UpdateItem
      4. Same-hash refresh (same hash as step 3, ``refresh_on_noop=True``)

    After EACH step:
      - ``ttl`` is absent from the stored item (no epoch-seconds attribute)
      - ``ttl_exempt`` is ``{"BOOL": true}``

    Mutations caught:
    - M2 (put never SETs marker): step 1 assertion fails — item has no ``ttl_exempt``
    - M3 (neither SETs it): step 1 assertion fails; step 2 additionally writes ``ttl``
    - M5 (refresh drops ``ttl_exempt`` from ``_live_for_ttl``): step 4 SETs ``#ttl``
      instead of REMOVEing it → step 4 assertion fails.
    - M1 and M4 are not caught by this sequence alone but are caught by
      ``TestTtlExemptUnit::test_update_with_stored_fi_row_sets_ttl_exempt_val``.
    """

    _TABLE = "vsa-moto-avx-findings-test"
    _REGION = "us-west-2"
    _SCOPE_VIN = "1HGCM82633A004352"
    _AGENT = "agent-fi-cost"
    _KIND = "cost.optimization"
    _NOW_FN = staticmethod(lambda: "2026-10-27T10:01:00Z")

    @staticmethod
    def _create_table(client: Any) -> None:
        client.create_table(
            TableName=TestMotoFiSequence._TABLE,
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

    @staticmethod
    def _make_seq_finding(
        finding_id: str,
        finding_key: str,
        inputs_hash: str,
        expires_at: str,
        ra: dict | None = None,
    ) -> dict:
        f = _make_finding(
            agent_id=TestMotoFiSequence._AGENT,
            expires_at=expires_at,
            recommended_action=ra,
            inputs_hash=inputs_hash,
            scope_field="vin",
            scope_value=TestMotoFiSequence._SCOPE_VIN,
        )
        f["finding_id"] = finding_id
        f["finding_key"] = finding_key
        f["finding_kind"] = TestMotoFiSequence._KIND
        return f

    @mock_aws
    def test_fi_put_informational_reemit_sequence_no_ttl(self):
        """Step-by-step DynamoDB verification of the four-step FA3 sequence."""
        client = boto3.client("dynamodb", region_name=self._REGION)
        self._create_table(client)

        finding_id = "FIND-SEQ-FA3-001"
        finding_key = compute_finding_key(
            agent_id=self._AGENT,
            scope_field="vin",
            scope_value=self._SCOPE_VIN,
            finding_kind=self._KIND,
        )

        def _read() -> dict:
            resp = client.get_item(
                TableName=self._TABLE,
                Key={"finding_id": {"S": finding_id}},
            )
            return resp.get("Item", {})

        # ── Step 1: FI put (recommended_action present) ──────────────────────
        result = write_finding(
            self._make_seq_finding(
                finding_id, finding_key, _H1, "2026-11-03T10:00:00Z",
                ra={"action_kind": "approve"},
            ),
            table_name=self._TABLE,
            dynamodb_client=client,
            utc_now_fn=self._NOW_FN,
        )
        assert result["action"] == "put", f"step1: expected put, got {result['action']!r}"
        item = _read()
        assert "ttl" not in item, (
            f"FA3/seq step1: no ttl expected after FI put; got {sorted(item.keys())}"
        )
        assert item.get("ttl_exempt") == {"BOOL": True}, (
            f"FA3/seq step1: ttl_exempt must be True after FI put; "
            f"got {item.get('ttl_exempt')!r}"
        )

        # ── Step 2: informational re-emit (no RA, different hash) ────────────
        result = write_finding(
            self._make_seq_finding(
                finding_id, finding_key, _H2, "2026-11-10T10:00:00Z",
            ),
            table_name=self._TABLE,
            dynamodb_client=client,
            utc_now_fn=self._NOW_FN,
        )
        assert result["action"] == "update", (
            f"step2: expected update, got {result['action']!r}"
        )
        item = _read()
        assert "ttl" not in item, (
            f"FA3/seq step2: no ttl expected after first informational re-emit; "
            f"got {sorted(item.keys())}"
        )
        assert item.get("ttl_exempt") == {"BOOL": True}, (
            f"FA3/seq step2: ttl_exempt must be True after first re-emit; "
            f"got {item.get('ttl_exempt')!r}"
        )

        # ── Step 3: second informational re-emit (new hash) ──────────────────
        result = write_finding(
            self._make_seq_finding(
                finding_id, finding_key, _H3, "2026-11-17T10:00:00Z",
            ),
            table_name=self._TABLE,
            dynamodb_client=client,
            utc_now_fn=self._NOW_FN,
        )
        assert result["action"] == "update", (
            f"step3: expected update, got {result['action']!r}"
        )
        item = _read()
        assert "ttl" not in item, (
            f"FA3/seq step3: no ttl expected after second informational re-emit; "
            f"got {sorted(item.keys())}"
        )
        assert item.get("ttl_exempt") == {"BOOL": True}, (
            f"FA3/seq step3: ttl_exempt must be True after second re-emit; "
            f"got {item.get('ttl_exempt')!r}"
        )

        # ── Step 4: same-hash refresh (same hash as step 3) ──────────────────
        result = write_finding(
            self._make_seq_finding(
                finding_id, finding_key, _H3, "2026-11-24T10:00:00Z",
            ),
            table_name=self._TABLE,
            dynamodb_client=client,
            utc_now_fn=self._NOW_FN,
            refresh_on_noop=True,
        )
        assert result["action"] in ("refresh", "noop"), (
            f"step4: expected refresh or noop, got {result['action']!r}"
        )
        item = _read()
        assert "ttl" not in item, (
            f"FA3/seq step4: no ttl expected after same-hash refresh; "
            f"got {sorted(item.keys())}"
        )
        assert item.get("ttl_exempt") == {"BOOL": True}, (
            f"FA3/seq step4: ttl_exempt must be True after refresh; "
            f"got {item.get('ttl_exempt')!r}"
        )
