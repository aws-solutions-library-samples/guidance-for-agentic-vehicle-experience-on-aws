"""Tests for agents/fi_sim_operator/ledger.py (task 11.3a).

Fakes only; no real DynamoDB. Tests cover:
  - record_first_seen: conditional put, idempotency
  - reserve_cap: conditional add, cap enforcement
  - get_first_seen_times: query result mapping
  - get_today_cap_used: returns 0 when absent

Mutant guards:
  L1 (reserve after writing): tested in test_run.py — the ledger functions
     themselves are tested for correct behavior here.
  L4 (overwrite person's decision): reserve_cap does NOT check whether an
     action exists — that check is the conditional put in run.py. These tests
     pin that reserve_cap is purely a cap counter, not a decision guard.
"""
from __future__ import annotations

import pytest

from agents.fi_sim_operator.ledger import (
    get_first_seen_times,
    get_today_cap_used,
    record_first_seen,
    reserve_cap,
    _SEEN_TTL_S,
    _seen_pk,
    _cap_pk,
)

_FLEET = "demo-lastmile-delivery"
_FID1 = "FIND-0000-0001"
_FID2 = "FIND-0000-0002"
_TABLE = "vsa-test-avx-sim-operator"
_TODAY = "2026-11-06"
_RUN_AT = "2026-11-06T07:10:00Z"


# ── Fake DynamoDB client ──────────────────────────────────────────────────────

class FakeDdb:
    """Minimal fake DynamoDB client (low-level) for ledger tests."""

    def __init__(self) -> None:
        self._store: dict[tuple, dict] = {}  # (pk, sk) → item

    def _key(self, item: dict, keydef=None) -> tuple:
        """Extract (pk, sk) from an item or key dict."""
        if keydef is not None:
            pk = keydef.get("pk", {}).get("S", "")
            sk = keydef.get("sk", {}).get("S", "")
            return (pk, sk)
        pk = item.get("pk", {}).get("S", "")
        sk = item.get("sk", {}).get("S", "")
        return (pk, sk)

    def put_item(self, TableName, Item, ConditionExpression=None, **kwargs):
        from botocore.exceptions import ClientError
        key = self._key(Item)
        if ConditionExpression == "attribute_not_exists(sk)":
            if key in self._store:
                error_response = {
                    "Error": {"Code": "ConditionalCheckFailedException", "Message": ""},
                }
                raise ClientError(error_response, "PutItem")
        self._store[key] = Item
        return {}

    def update_item(
        self, TableName, Key, UpdateExpression, ConditionExpression, **kwargs
    ):
        from botocore.exceptions import ClientError
        kk = self._key(None, keydef=Key)
        values = kwargs.get("ExpressionAttributeValues", {})
        n = int(values.get(":n", {}).get("N", "1"))
        # :max = cap - n; condition is attribute_not_exists(#c) OR #c <= :max
        # which is equivalent to current_count + n <= cap.
        max_count = int(values.get(":max", {}).get("N", "-1"))

        existing = self._store.get(kk, {})
        current_count = int(existing.get("count", {}).get("N", "0"))

        # Condition: attribute_not_exists(#c) OR #c <= :max
        # → current_count <= max_count  (0 when item absent matches either branch)
        if current_count > max_count:
                error_response = {
                    "Error": {"Code": "ConditionalCheckFailedException", "Message": ""},
                }
                raise ClientError(error_response, "UpdateItem")
        new_count = current_count + n
        updated = dict(existing)
        pk_val = Key.get("pk", {}).get("S", "")
        sk_val = Key.get("sk", {}).get("S", "")
        updated["pk"] = {"S": pk_val}
        updated["sk"] = {"S": sk_val}
        updated["count"] = {"N": str(new_count)}
        self._store[kk] = updated
        return {}

    def query(self, TableName, KeyConditionExpression, ExpressionAttributeValues, **kwargs):
        pk_val = ExpressionAttributeValues.get(":pk", {}).get("S", "")
        items = [
            item for (pk, sk), item in self._store.items()
            if pk == pk_val
        ]
        return {"Items": items}

    def get_item(self, TableName, Key, **kwargs):
        kk = self._key(None, keydef=Key)
        item = self._store.get(kk)
        return {"Item": item} if item else {}


# ── record_first_seen ─────────────────────────────────────────────────────────

class TestRecordFirstSeen:
    def test_first_write_returns_true(self):
        """First insert returns True (new item)."""
        ddb = FakeDdb()
        result = record_first_seen(ddb, _TABLE, _FLEET, _FID1, _RUN_AT)
        assert result is True

    def test_second_write_returns_false(self):
        """Second write for same finding returns False (already existed)."""
        ddb = FakeDdb()
        record_first_seen(ddb, _TABLE, _FLEET, _FID1, _RUN_AT)
        result = record_first_seen(ddb, _TABLE, _FLEET, _FID1, _RUN_AT)
        assert result is False

    def test_item_stored_with_first_seen_at(self):
        """Written item carries the first_seen_at timestamp."""
        ddb = FakeDdb()
        record_first_seen(ddb, _TABLE, _FLEET, _FID1, _RUN_AT)
        key = (_seen_pk(_FLEET), _FID1)
        item = ddb._store[key]
        assert item["first_seen_at"]["S"] == _RUN_AT

    def test_item_stored_with_ttl(self):
        """Written item has a TTL field set (10 days from now)."""
        import time
        ddb = FakeDdb()
        before = int(time.time())
        record_first_seen(ddb, _TABLE, _FLEET, _FID1, _RUN_AT)
        after = int(time.time())
        key = (_seen_pk(_FLEET), _FID1)
        item = ddb._store[key]
        ttl = int(item["ttl"]["N"])
        assert ttl >= before + _SEEN_TTL_S - 1
        assert ttl <= after + _SEEN_TTL_S + 1

    def test_different_finding_ids_are_independent(self):
        """Two different finding_ids are stored independently."""
        ddb = FakeDdb()
        r1 = record_first_seen(ddb, _TABLE, _FLEET, _FID1, _RUN_AT)
        r2 = record_first_seen(ddb, _TABLE, _FLEET, _FID2, _RUN_AT)
        assert r1 is True
        assert r2 is True
        assert len(ddb._store) == 2


# ── reserve_cap ───────────────────────────────────────────────────────────────

class TestReserveCap:
    def test_first_reserve_returns_true(self):
        """First reserve when no cap item exists returns True."""
        ddb = FakeDdb()
        result = reserve_cap(ddb, _TABLE, _FLEET, _TODAY, n=1, cap=20)
        assert result is True

    def test_reserve_increments_count(self):
        """Reserved slots are accumulated in the count."""
        ddb = FakeDdb()
        reserve_cap(ddb, _TABLE, _FLEET, _TODAY, n=3, cap=20)
        key = (_cap_pk(_FLEET), _TODAY)
        item = ddb._store[key]
        assert int(item["count"]["N"]) == 3

    def test_reserve_within_cap_returns_true(self):
        """Reserving when count + n <= cap returns True."""
        ddb = FakeDdb()
        reserve_cap(ddb, _TABLE, _FLEET, _TODAY, n=10, cap=20)
        result = reserve_cap(ddb, _TABLE, _FLEET, _TODAY, n=10, cap=20)
        assert result is True

    def test_reserve_at_exact_cap_returns_true(self):
        """Reserving exactly the last available slot returns True (boundary)."""
        ddb = FakeDdb()
        reserve_cap(ddb, _TABLE, _FLEET, _TODAY, n=19, cap=20)
        result = reserve_cap(ddb, _TABLE, _FLEET, _TODAY, n=1, cap=20)
        assert result is True

    def test_reserve_exceeding_cap_returns_false(self):
        """Reserving when count + n > cap returns False.

        Mutation guard L1: if reserve_cap were called after writing the Action
        (instead of before), the check would fail after the Action is already written,
        causing the cap to be exceeded.
        """
        ddb = FakeDdb()
        reserve_cap(ddb, _TABLE, _FLEET, _TODAY, n=20, cap=20)
        result = reserve_cap(ddb, _TABLE, _FLEET, _TODAY, n=1, cap=20)
        assert result is False, (
            "reserve_cap must return False when count + n > cap. "
            "Mutation guard L1: this must be checked BEFORE any Action write."
        )

    def test_cap_zero_always_false(self):
        """daily_cap=0 → reserve returns False for any n > 0."""
        ddb = FakeDdb()
        result = reserve_cap(ddb, _TABLE, _FLEET, _TODAY, n=1, cap=0)
        assert result is False

    def test_pair_reserve_two_slots(self):
        """Pairs reserve 2 slots atomically."""
        ddb = FakeDdb()
        result = reserve_cap(ddb, _TABLE, _FLEET, _TODAY, n=2, cap=3)
        assert result is True
        key = (_cap_pk(_FLEET), _TODAY)
        assert int(ddb._store[key]["count"]["N"]) == 2

    def test_pair_reserve_fails_when_only_one_slot_left(self):
        """A pair (n=2) fails when only 1 slot remains (never split a pair).

        Mutation guard S5 (also tested in test_plan.py): a pair must never be
        split. If reserve_cap allowed partial reservations, the grounding would
        be written without the booking slot reserved.
        """
        ddb = FakeDdb()
        reserve_cap(ddb, _TABLE, _FLEET, _TODAY, n=19, cap=20)
        result = reserve_cap(ddb, _TABLE, _FLEET, _TODAY, n=2, cap=20)
        assert result is False, (
            "A pair needs 2 slots atomically. With only 1 left, the reserve must fail."
        )


# ── get_first_seen_times ──────────────────────────────────────────────────────

class TestGetFirstSeenTimes:
    def test_returns_known_finding_ids(self):
        """Returns a dict with first_seen_at for all finding_ids in the ledger."""
        ddb = FakeDdb()
        record_first_seen(ddb, _TABLE, _FLEET, _FID1, _RUN_AT)
        record_first_seen(ddb, _TABLE, _FLEET, _FID2, "2026-11-05T07:10:00Z")
        result = get_first_seen_times(ddb, _TABLE, _FLEET, [_FID1, _FID2])
        assert result[_FID1] == _RUN_AT
        assert result[_FID2] == "2026-11-05T07:10:00Z"

    def test_unknown_finding_id_absent_from_result(self):
        """A finding_id not in the ledger is absent from the returned dict."""
        ddb = FakeDdb()
        result = get_first_seen_times(ddb, _TABLE, _FLEET, ["FIND-UNKNOWN"])
        assert "FIND-UNKNOWN" not in result

    def test_empty_finding_ids_returns_empty_dict(self):
        """Empty input returns empty dict."""
        ddb = FakeDdb()
        result = get_first_seen_times(ddb, _TABLE, _FLEET, [])
        assert result == {}


# ── get_today_cap_used ────────────────────────────────────────────────────────

class TestGetTodayCapUsed:
    def test_returns_zero_when_no_cap_item(self):
        """No CAP item for today → returns 0."""
        ddb = FakeDdb()
        result = get_today_cap_used(ddb, _TABLE, _FLEET, _TODAY)
        assert result == 0

    def test_returns_current_count(self):
        """Returns the current count after some reserves."""
        ddb = FakeDdb()
        reserve_cap(ddb, _TABLE, _FLEET, _TODAY, n=5, cap=20)
        result = get_today_cap_used(ddb, _TABLE, _FLEET, _TODAY)
        assert result == 5
