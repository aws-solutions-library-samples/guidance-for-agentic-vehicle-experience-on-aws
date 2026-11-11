"""Moto-backed tests for agents/fi_sim_operator/ledger.py (C1 fix, task 11.3a).

Runs against real moto DynamoDB (mock_aws) — NOT against a local endpoint.
Run this file WITHOUT AWS_ENDPOINT_URL:

    .venv/bin/python -m pytest -p no:cacheprovider -q \
        agents/fi_sim_operator/tests/test_ledger_moto.py

These tests replace the fake-DDB reserve_cap tests for the cap expression
property.  The FakeDdb in test_ledger.py accepted the old invalid expression
because it evaluated the semantics; moto 5.2.3 rejects
``if_not_exists(#c, :zero) + :n <= :cap`` with ``Cannot parse condition`` as
the real service would.

Table shape mirrors the CDK ``AvxSimOperatorLedger`` construct
(infrastructure/lib/vsa-tier2-stack.ts line ~3572):
  pk (S, HASH) / sk (S, RANGE), PAY_PER_REQUEST, TTL attr 'ttl'.

Mutants covered:
  K1 — the old ConditionExpression (moto raises; reserve_cap would re-raise):
       patching ConditionExpression in update_item to the old form must cause the
       test_reserve_2_of_3_refused to fail.
  K2 — :max = cap (off by n): reserve 2 then another 2 of cap=3 would succeed;
       test_reserve_2_then_2_refused_count_stays_2 catches it.
"""
from __future__ import annotations

import os

import boto3
import pytest

# moto must intercept before any boto3 call; import here so the decorator works.
from moto import mock_aws

from agents.fi_sim_operator.ledger import (
    _cap_pk,
    _seen_pk,
    get_first_seen_times,
    get_today_cap_used,
    record_first_seen,
    reserve_cap,
)

_TABLE = "vsa-moto-test-avx-sim-operator"
_FLEET = "demo-fleet-moto"
_TODAY = "2026-11-07"
_TOMORROW = "2026-11-08"
_RUN_AT = "2026-11-07T07:10:00Z"
_FID1 = "FIND-MOTO-0001"
_FID2 = "FIND-MOTO-0002"
_CAP = 3


# ── Fixture: spin up a moto-backed DynamoDB table ────────────────────────────

@pytest.fixture(autouse=True)
def _aws_credentials(monkeypatch):
    """Provide dummy credentials so boto3 doesn't complain; moto intercepts all calls."""
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SECURITY_TOKEN", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")


@pytest.fixture
def ddb():
    """Real boto3 low-level DynamoDB client intercepted by moto."""
    with mock_aws():
        client = boto3.client("dynamodb", region_name="us-east-1")
        client.create_table(
            TableName=_TABLE,
            KeySchema=[
                {"AttributeName": "pk", "KeyType": "HASH"},
                {"AttributeName": "sk", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "pk", "AttributeType": "S"},
                {"AttributeName": "sk", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        yield client


# ── reserve_cap — the critical C1 fix ────────────────────────────────────────

class TestReserveCapMoto:
    """Mutant K1 (old expression) and K2 (:max = cap) must fail in this class."""

    def test_reserve_2_of_3_succeeds(self, ddb):
        """K1/K2 guard: first reservation of 2 in a cap-3 fleet succeeds."""
        ok = reserve_cap(ddb, _TABLE, _FLEET, _TODAY, n=2, cap=_CAP)
        assert ok is True, "reserve 2 of 3 must succeed (cap slot available)"

    def test_reserve_2_then_2_refused_count_stays_2(self, ddb):
        """K1/K2 guard: after reserving 2, a second request for 2 is refused.

        Mutant K2 (:max = cap instead of cap - n) would set :max=3 for n=2, so
        the second reserve sees count=2 <= 3 → allowed, making total=4 > 3.
        This test catches that.
        """
        reserve_cap(ddb, _TABLE, _FLEET, _TODAY, n=2, cap=_CAP)
        refused = reserve_cap(ddb, _TABLE, _FLEET, _TODAY, n=2, cap=_CAP)
        assert refused is False, (
            "K2: :max must be cap-n, not cap. "
            "If :max=cap, a second n=2 reserve with count=2 wrongly succeeds."
        )
        # Count must still be 2 (the refused call did not increment)
        count = get_today_cap_used(ddb, _TABLE, _FLEET, _TODAY)
        assert count == 2, f"Refused reserve must not change the count; got {count}"

    def test_reserve_1_after_2_succeeds_reaches_cap(self, ddb):
        """After reserving 2, a single-slot reserve succeeds and reaches cap=3."""
        reserve_cap(ddb, _TABLE, _FLEET, _TODAY, n=2, cap=_CAP)
        ok = reserve_cap(ddb, _TABLE, _FLEET, _TODAY, n=1, cap=_CAP)
        assert ok is True, "one more slot must fit after reserving 2 of 3"
        count = get_today_cap_used(ddb, _TABLE, _FLEET, _TODAY)
        assert count == 3, f"count must be exactly cap=3 after filling; got {count}"

    def test_fresh_day_starts_at_zero(self, ddb):
        """A different day's CAP item is independent; fresh day returns 0 used."""
        reserve_cap(ddb, _TABLE, _FLEET, _TODAY, n=2, cap=_CAP)
        count_tomorrow = get_today_cap_used(ddb, _TABLE, _FLEET, _TOMORROW)
        assert count_tomorrow == 0, "A different day must start at 0 slots used"

    def test_n_greater_than_cap_returns_false_no_write(self, ddb):
        """Requesting more slots than the cap is refused immediately (no DDB call needed)."""
        ok = reserve_cap(ddb, _TABLE, _FLEET, _TODAY, n=5, cap=_CAP)
        assert ok is False
        # Nothing was written
        count = get_today_cap_used(ddb, _TABLE, _FLEET, _TODAY)
        assert count == 0

    def test_n_less_than_1_raises(self, ddb):
        """n < 1 is a caller bug; reserve_cap raises ValueError."""
        with pytest.raises(ValueError, match="n must be >= 1"):
            reserve_cap(ddb, _TABLE, _FLEET, _TODAY, n=0, cap=_CAP)

    def test_cap_zero_always_refused(self, ddb):
        """daily_cap=0 refuses any n >= 1 (n > cap early-out)."""
        ok = reserve_cap(ddb, _TABLE, _FLEET, _TODAY, n=1, cap=0)
        assert ok is False


# ── record_first_seen — conditional put against moto ──────────────────────────

class TestRecordFirstSeenMoto:
    """Pin the conditional put semantics against a real DynamoDB engine."""

    def test_first_write_returns_true(self, ddb):
        ok = record_first_seen(ddb, _TABLE, _FLEET, _FID1, _RUN_AT)
        assert ok is True

    def test_second_write_returns_false_original_wins(self, ddb):
        """Idempotent: second write returns False; first timestamp is preserved."""
        record_first_seen(ddb, _TABLE, _FLEET, _FID1, _RUN_AT)
        ok = record_first_seen(ddb, _TABLE, _FLEET, _FID1, "2026-11-07T09:00:00Z")
        assert ok is False
        # Original timestamp is preserved
        times = get_first_seen_times(ddb, _TABLE, _FLEET, [_FID1])
        assert times[_FID1] == _RUN_AT

    def test_two_findings_stored_independently(self, ddb):
        r1 = record_first_seen(ddb, _TABLE, _FLEET, _FID1, _RUN_AT)
        r2 = record_first_seen(ddb, _TABLE, _FLEET, _FID2, "2026-11-06T07:10:00Z")
        assert r1 is True
        assert r2 is True
        times = get_first_seen_times(ddb, _TABLE, _FLEET, [_FID1, _FID2])
        assert times[_FID1] == _RUN_AT
        assert times[_FID2] == "2026-11-06T07:10:00Z"
