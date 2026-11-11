"""Tests for the DMS certification-tier hot read (`_lib/dms_cert_cache`).

Spec: `.kiro/specs/2026-08-26-cvx-dms-personas-extension/spec.md`
Task: T1.4 (test skeleton) + T2.6 (implementation).

Covers:
  * Redis hit returns the cached tier.
  * Redis unreachable falls back to DDB.
  * Tier below Gold routes through the deterministic response.
  * Redis client convention matches ``api-vehicle-live-state/handler.py``
    (module-level lazy client, ``socket_connect_timeout=2``,
    ``decode_responses=True``, single constructor site).
  * Both Redis and DDB unavailable → ``("UNKNOWN", None)`` — fail-closed,
    never raises.
  * ``UNKNOWN`` is not an AI tier — callers treat it as below Gold.

No real Redis endpoint, no real DDB.  Every AWS/Redis call is patched.
Fakeredis is deliberately NOT used — it can't reproduce the socket-level
failure modes the fail-closed path handles.
"""
from __future__ import annotations

import importlib
import os
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Fixtures + helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_module_state(monkeypatch):
    """Reset the module-level Redis client between tests.

    The client is module-scoped by design (lazy singleton).  Tests need a
    clean slate each time so a patch on one test doesn't leak into the
    next.  We reload the module inside the test's namespace so every test
    sees ``_redis_client is None`` at start.
    """
    import agents.supervisor._lib.dms_cert_cache as mod
    # Force re-init on next _get_redis() call.
    mod._redis_client = None
    # Provide a REDIS_ENDPOINT so _get_redis proceeds to construct (the
    # module short-circuits to DDB when the env var is unset).
    monkeypatch.setenv("REDIS_ENDPOINT", "test-redis.example.internal")
    monkeypatch.setenv("REDIS_PORT", "6379")
    monkeypatch.setenv("VSA_DMS_STAGE", "staging")
    monkeypatch.setenv("VSA_DMS_REGION", "us-west-2")
    yield
    mod._redis_client = None


# ---------------------------------------------------------------------------
# Redis hot path
# ---------------------------------------------------------------------------


def test_cert_read_from_redis_hits_return_tier():
    """A Redis HGETALL hit short-circuits DDB and returns the cached tier."""
    import agents.supervisor._lib.dms_cert_cache as mod

    fake_client = MagicMock()
    fake_client.ping.return_value = True
    fake_client.hgetall.return_value = {
        "tier": "Gold",
        "computed_at": "2026-08-29T00:00:00Z",
    }
    with patch.object(mod.redis, "Redis", return_value=fake_client) as ctor, \
         patch.object(mod.boto3, "resource") as ddb:
        tier, computed_at = mod.check_dealer_certification("DLR-01")

    assert tier == "Gold"
    assert computed_at == "2026-08-29T00:00:00Z"
    # DDB fallback path MUST NOT have been reached.
    ddb.assert_not_called()
    ctor.assert_called_once()  # single lazy construction


# ---------------------------------------------------------------------------
# DDB fallback path
# ---------------------------------------------------------------------------


def test_cert_read_falls_back_to_ddb_on_redis_error():
    """A raise from Redis on ping/hgetall degrades to a DDB read."""
    import agents.supervisor._lib.dms_cert_cache as mod

    # Redis constructor succeeds but ping raises → _get_redis returns None.
    fake_client = MagicMock()
    fake_client.ping.side_effect = mod.redis.ConnectionError("boom")

    fake_table = MagicMock()
    fake_table.get_item.return_value = {
        "Item": {
            "dealer_id": "DLR-01",
            "interface_id": "OVERALL",
            "tier": "Silver",
            "computed_at": "2026-08-28T22:00:00Z",
        }
    }
    fake_ddb = MagicMock()
    fake_ddb.Table.return_value = fake_table

    with patch.object(mod.redis, "Redis", return_value=fake_client), \
         patch.object(mod.boto3, "resource", return_value=fake_ddb):
        tier, computed_at = mod.check_dealer_certification("DLR-01")

    assert tier == "Silver"
    assert computed_at == "2026-08-28T22:00:00Z"
    fake_table.get_item.assert_called_once_with(
        Key={"dealer_id": "DLR-01", "interface_id": "OVERALL"}
    )


def test_cert_read_ddb_miss_returns_unknown():
    """No item in DDB → fail-closed UNKNOWN.  The DDB miss is NOT cached."""
    import agents.supervisor._lib.dms_cert_cache as mod

    fake_client = MagicMock()
    fake_client.ping.return_value = True
    fake_client.hgetall.return_value = {}  # Redis miss

    fake_table = MagicMock()
    fake_table.get_item.return_value = {}  # DDB miss
    fake_ddb = MagicMock()
    fake_ddb.Table.return_value = fake_table

    with patch.object(mod.redis, "Redis", return_value=fake_client), \
         patch.object(mod.boto3, "resource", return_value=fake_ddb):
        tier, computed_at = mod.check_dealer_certification("DLR-NEW")

    assert tier == "UNKNOWN"
    assert computed_at is None
    # We MUST NOT have HSET a negative result — that would keep the gate
    # shut for the whole TTL when a new dealer is certified.
    fake_client.hset.assert_not_called()


# ---------------------------------------------------------------------------
# Below-Gold routes through the deterministic response
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("tier", ["Bronze", "Silver", "UNKNOWN"])
def test_below_gold_returns_deterministic_response(tier):
    """Bronze / Silver / UNKNOWN → deterministic_fn wins, ai_fn is not called."""
    from agents.supervisor._lib.dms_cert_cache import require_gold_or_deterministic

    ai_calls, det_calls = [], []

    def ai():
        ai_calls.append("ai")
        return "AI-NARRATION"

    def det():
        det_calls.append("det")
        return "DETERMINISTIC"

    result = require_gold_or_deterministic(tier, ai, det)
    assert result == "DETERMINISTIC"
    assert ai_calls == []
    assert det_calls == ["det"]


@pytest.mark.parametrize("tier", ["Gold", "Platinum"])
def test_gold_or_platinum_calls_ai(tier):
    """Gold / Platinum → ai_fn wins, deterministic_fn is not called."""
    from agents.supervisor._lib.dms_cert_cache import require_gold_or_deterministic

    ai_calls, det_calls = [], []

    def ai():
        ai_calls.append("ai")
        return "AI-NARRATION"

    def det():
        det_calls.append("det")
        return "DETERMINISTIC"

    result = require_gold_or_deterministic(tier, ai, det)
    assert result == "AI-NARRATION"
    assert ai_calls == ["ai"]
    assert det_calls == []


def test_below_gold_without_deterministic_fn_returns_shape():
    """No deterministic_fn → default shape ``{available:false, reason, tier}``."""
    from agents.supervisor._lib.dms_cert_cache import require_gold_or_deterministic

    def ai():
        raise AssertionError("ai_fn must not be called for tier=Silver")

    result = require_gold_or_deterministic("Silver", ai)
    assert result["available"] is False
    assert "Gold or Platinum" in result["reason"]
    assert result["tier"] == "Silver"


def test_unknown_tier_is_below_gold():
    """The fail-closed UNKNOWN sentinel is NOT an AI tier."""
    from agents.supervisor._lib.dms_cert_cache import is_ai_activated

    assert is_ai_activated("UNKNOWN") is False
    for good in ("Gold", "Platinum"):
        assert is_ai_activated(good) is True
    for bad in ("Bronze", "Silver", "unknown", "gold", ""):
        assert is_ai_activated(bad) is False


# ---------------------------------------------------------------------------
# Redis client convention — matches api-vehicle-live-state/handler.py
# ---------------------------------------------------------------------------


def test_redis_client_convention_matches_cvx_prior_art():
    """Constructor kwargs match ``api-vehicle-live-state/handler.py:70``."""
    import agents.supervisor._lib.dms_cert_cache as mod

    fake_client = MagicMock()
    fake_client.ping.return_value = True
    fake_client.hgetall.return_value = {"tier": "Gold", "computed_at": None}

    with patch.object(mod.redis, "Redis", return_value=fake_client) as ctor:
        mod.check_dealer_certification("DLR-01")
        mod.check_dealer_certification("DLR-02")

    # Module-level lazy: two calls, one construction.
    ctor.assert_called_once()
    kwargs = ctor.call_args.kwargs
    assert kwargs["socket_connect_timeout"] == 2, (
        "CVX Redis convention pins socket_connect_timeout=2 — see "
        "lambdas/api-vehicle-live-state/handler.py:70"
    )
    assert kwargs["decode_responses"] is True, (
        "CVX Redis convention pins decode_responses=True — see "
        "lambdas/api-vehicle-live-state/handler.py:70"
    )
    assert kwargs["port"] == 6379, "port must resolve from REDIS_PORT env"


def test_unreachable_redis_never_raises_from_check():
    """Combined Redis + DDB outage MUST NOT raise into the caller."""
    import agents.supervisor._lib.dms_cert_cache as mod

    fake_client = MagicMock()
    fake_client.ping.side_effect = mod.redis.ConnectionError("no route")

    fake_ddb = MagicMock()
    fake_table = MagicMock()
    fake_table.get_item.side_effect = mod.ClientError(
        error_response={
            "Error": {"Code": "InternalServerError", "Message": "down"}
        },
        operation_name="GetItem",
    )
    fake_ddb.Table.return_value = fake_table

    with patch.object(mod.redis, "Redis", return_value=fake_client), \
         patch.object(mod.boto3, "resource", return_value=fake_ddb):
        tier, computed_at = mod.check_dealer_certification("DLR-01")

    assert tier == "UNKNOWN"
    assert computed_at is None


# ---------------------------------------------------------------------------
# Empty dealer_id / empty REDIS_ENDPOINT hardening
# ---------------------------------------------------------------------------


def test_empty_dealer_id_returns_unknown_without_calling_anything():
    """Blank dealer id short-circuits — no Redis, no DDB."""
    import agents.supervisor._lib.dms_cert_cache as mod

    with patch.object(mod.redis, "Redis") as ctor, \
         patch.object(mod.boto3, "resource") as ddb:
        tier, ca = mod.check_dealer_certification("")

    assert tier == "UNKNOWN"
    assert ca is None
    ctor.assert_not_called()
    ddb.assert_not_called()


def test_missing_redis_endpoint_skips_redis(monkeypatch):
    """No REDIS_ENDPOINT env var → skip Redis, go straight to DDB (common in tests)."""
    import agents.supervisor._lib.dms_cert_cache as mod
    monkeypatch.delenv("REDIS_ENDPOINT", raising=False)

    fake_table = MagicMock()
    fake_table.get_item.return_value = {
        "Item": {
            "dealer_id": "DLR-01",
            "interface_id": "OVERALL",
            "tier": "Platinum",
            "computed_at": "2026-08-29T02:00:00Z",
        }
    }
    fake_ddb = MagicMock()
    fake_ddb.Table.return_value = fake_table

    with patch.object(mod.redis, "Redis") as ctor, \
         patch.object(mod.boto3, "resource", return_value=fake_ddb):
        tier, computed_at = mod.check_dealer_certification("DLR-01")

    assert tier == "Platinum"
    assert computed_at == "2026-08-29T02:00:00Z"
    ctor.assert_not_called()  # never touched Redis




# ---------------------------------------------------------------------------
# Shared dealer-id resolution (consolidated helper — review Cycle 1 Warning 1)
# ---------------------------------------------------------------------------


class TestResolveDealerId:
    """Test the shared session-context helper used by all three DMS tool
    modules.  Consolidated here (rather than duplicated per-tool) so a
    behaviour change is a one-place edit."""

    def _ctx(self, **kwargs):
        from types import SimpleNamespace
        return SimpleNamespace(**kwargs)

    def test_explicit_dealer_id_wins(self):
        from agents.supervisor._lib.dms_cert_cache import resolve_dealer_id
        ctx = self._ctx(dealer_id="from-ctx")
        assert resolve_dealer_id(ctx, "from-tool-arg") == "from-tool-arg"

    def test_falls_back_to_dealer_id_attribute(self):
        from agents.supervisor._lib.dms_cert_cache import resolve_dealer_id
        ctx = self._ctx(dealer_id="DLR-42")
        assert resolve_dealer_id(ctx) == "DLR-42"

    def test_falls_back_to_camelCase_variant(self):
        from agents.supervisor._lib.dms_cert_cache import resolve_dealer_id
        ctx = self._ctx(dealerId="DLR-C")
        assert resolve_dealer_id(ctx) == "DLR-C"

    def test_falls_back_to_primary_dealer_id(self):
        from agents.supervisor._lib.dms_cert_cache import resolve_dealer_id
        ctx = self._ctx(primary_dealer_id="DLR-P")
        assert resolve_dealer_id(ctx) == "DLR-P"

    def test_falls_back_to_first_dealer_ids_list(self):
        from agents.supervisor._lib.dms_cert_cache import resolve_dealer_id
        ctx = self._ctx(dealer_ids=["DLR-1", "DLR-2"])
        assert resolve_dealer_id(ctx) == "DLR-1"

    def test_empty_context_returns_empty_string(self):
        from agents.supervisor._lib.dms_cert_cache import resolve_dealer_id
        ctx = self._ctx()
        assert resolve_dealer_id(ctx) == ""

    def test_ordering_dealer_id_over_dealerIds(self):
        """dealer_id (singular scalar) wins over dealer_ids (list)."""
        from agents.supervisor._lib.dms_cert_cache import resolve_dealer_id
        ctx = self._ctx(dealer_id="DLR-scalar", dealer_ids=["DLR-list-1"])
        assert resolve_dealer_id(ctx) == "DLR-scalar"

    def test_empty_list_falls_through(self):
        """An empty dealer_ids list must not be treated as a valid id."""
        from agents.supervisor._lib.dms_cert_cache import resolve_dealer_id
        ctx = self._ctx(dealer_ids=[])
        assert resolve_dealer_id(ctx) == ""
