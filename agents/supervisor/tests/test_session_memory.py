"""Tests for agents/supervisor/session_memory.py — tasks T2.1 and T2.2.

Spec: `.kiro/specs/2026-09-22-cvx-text-session-memory/spec.md`

All tests inject a stub client; no network calls are made.

Mutation tests (run during development, outcomes recorded in the handback):
- T2.1 mutation: removed the broad ``except`` block → caller raised instead of
  returning normally → test ``test_record_turn_swallows_client_error`` FAILED
  as required.  Swallow restored.
- T2.2 mutation: reversed stub's outer-list ordering to already-oldest-first →
  ``test_load_recent_turns_ordering`` FAILED — turn-2 message appeared before
  turn-1 with the un-reversed output.  The ordering reversal was restored.
- F3.2 mutation: re-added ``actor=%s`` with the actor_id value to one WARNING
  log record → ``test_does_not_log_actor_id_value`` FAILED as required.
  Removed again.
- F3.3 mutation: collapsed both except branches back into one ``except Exception``
  with a single WARNING → ``test_contract_failure_logged_at_error`` FAILED —
  the contract-mismatch case was indistinguishable from a service failure.
  Separate branches restored.
- F3.5 mutation: removed the ``min(k, _MAX_HYDRATE_K)`` cap in _hydrate_k →
  ``test_hydrate_k_above_cap_is_clamped`` FAILED as required.  Cap restored.
"""

from __future__ import annotations

import logging
from typing import Any, List, Optional
from unittest.mock import MagicMock

import pytest

from agents.supervisor.session_memory import (
    DEFAULT_K,
    MemoryTurn,
    _MAX_HYDRATE_K,
    _hydrate_k,
    load_recent_turns,
    record_turn,
)


# ---------------------------------------------------------------------------
# Stub helpers
# ---------------------------------------------------------------------------


def _stub_client(
    get_last_k_turns_return: Optional[List[Any]] = None,
    create_event_side_effect: Optional[Exception] = None,
    get_last_k_turns_side_effect: Optional[Exception] = None,
) -> MagicMock:
    """Build a MemoryClient stub with configurable return values and side effects."""
    client = MagicMock()

    if create_event_side_effect is not None:
        client.create_event.side_effect = create_event_side_effect
    else:
        client.create_event.return_value = {
            "actorId": "test-actor",
            "sessionId": "test-session",
            "eventId": "0000001",
            "memoryId": "test-memory-id",
        }

    if get_last_k_turns_side_effect is not None:
        client.get_last_k_turns.side_effect = get_last_k_turns_side_effect
    else:
        client.get_last_k_turns.return_value = (
            get_last_k_turns_return if get_last_k_turns_return is not None else []
        )

    return client


# Service returns NEWEST-FIRST in the outer list.  Messages within a turn are
# CHRONOLOGICAL (USER first, then ASSISTANT).
# This is the correct live ordering documented in docs/tech.md.
_NEWEST_FIRST_TWO_TURNS = [
    # Index 0 = newest turn
    [
        {"role": "USER",      "content": {"text": "turn-2-user"}},
        {"role": "ASSISTANT", "content": {"text": "turn-2-assistant"}},
    ],
    # Index 1 = oldest turn
    [
        {"role": "USER",      "content": {"text": "turn-1-user"}},
        {"role": "ASSISTANT", "content": {"text": "turn-1-assistant"}},
    ],
]


# ---------------------------------------------------------------------------
# T2.1 — record_turn
# ---------------------------------------------------------------------------


class TestRecordTurn:
    """Tests for record_turn()."""

    def test_calls_create_event_with_correct_shape(self):
        """record_turn issues exactly one create_event call with the right args."""
        client = _stub_client()
        record_turn(client, "mem-id", "actor-abc", "sess-xyz", "hello", "hi there")

        client.create_event.assert_called_once_with(
            "mem-id",
            "actor-abc",
            "sess-xyz",
            [("hello", "USER"), ("hi there", "ASSISTANT")],
        )

    def test_message_order_is_text_then_role(self):
        """Tuple order is (text, role) — not (role, text).

        The AgentCore API raises ValueError on the wrong order; the test pins
        the order explicitly so a swap is caught before it reaches the service.
        """
        client = _stub_client()
        record_turn(client, "mem-id", "actor-abc", "sess-xyz", "user prompt", "asst reply")

        call_args = client.create_event.call_args
        messages = call_args[0][3]   # positional arg index 3

        first_text, first_role = messages[0]
        assert first_role == "USER", f"Expected role USER in first message, got {first_role!r}"
        assert first_text == "user prompt", f"Expected prompt text, got {first_text!r}"

        second_text, second_role = messages[1]
        assert second_role == "ASSISTANT", (
            f"Expected role ASSISTANT in second message, got {second_role!r}"
        )
        assert second_text == "asst reply", (
            f"Expected result text, got {second_text!r}"
        )

    def test_noop_when_memory_id_absent(self):
        """No-ops silently when memory_id is empty or None."""
        for bad_id in ("", None):
            client = _stub_client()
            record_turn(client, bad_id, "actor-abc", "sess-xyz", "p", "r")
            client.create_event.assert_not_called()

    def test_noop_when_actor_id_absent(self):
        """No-ops silently when actor_id is empty or None (spec D1)."""
        for bad_actor in ("", None):
            client = _stub_client()
            record_turn(client, "mem-id", bad_actor, "sess-xyz", "p", "r")
            client.create_event.assert_not_called()

    def test_swallows_client_error(self):
        """A create_event exception must NOT propagate to the caller (spec D4).

        Mutation test applied: removed the except block → caller raised.
        Confirmed the test FAILED.  Swallow restored.
        """
        client = _stub_client(create_event_side_effect=RuntimeError("network error"))
        # Must return None without raising.
        result = record_turn(client, "mem-id", "actor-abc", "sess-xyz", "p", "r")
        assert result is None

    def test_swallows_any_exception_type(self):
        """Swallows any exception — not only RuntimeError."""
        for exc in (ValueError("bad"), KeyError("k"), Exception("e")):
            client = _stub_client(create_event_side_effect=exc)
            record_turn(client, "mem-id", "actor-abc", "sess-xyz", "p", "r")  # must not raise

    def test_returns_none(self):
        """record_turn always returns None (no return value contract needed)."""
        client = _stub_client()
        result = record_turn(client, "mem-id", "actor-abc", "sess-xyz", "p", "r")
        assert result is None


# ---------------------------------------------------------------------------
# F3.2 — actor_id must never appear in log output from session_memory
# ---------------------------------------------------------------------------


class TestDoesNotLogActorId:
    """The actor_id value must never appear in any log record from session_memory.

    Mirrors the approach of ``test_does_not_log_sub_value`` in
    ``test_actor_id.py``, which pins the same policy for ``agentcore_app``.

    Both modules are now consistent: ids and counts only, no raw identity values.
    The route Lambda logs ``sub=`` in its own group if actor recovery is needed.

    Mutation that was applied and confirmed to FAIL this test:
      Re-added ``actor=%s, actor_id`` to the WARNING record in ``record_turn``.
      Test FAILED with "actor_id value must not be logged".  Removed again.
    """

    ACTOR_VALUE = "SENSITIVE-ACTOR-ID-12345"
    SESSION_VALUE = "sess-safe-to-log"

    def test_does_not_log_actor_id_value_on_success(self, caplog):
        """The actor_id value must not appear in any log record on success."""
        client = _stub_client()
        with caplog.at_level(logging.DEBUG, logger="agents.supervisor.session_memory"):
            record_turn(
                client, "mem-id", self.ACTOR_VALUE, self.SESSION_VALUE, "prompt", "result"
            )
        for record in caplog.records:
            assert self.ACTOR_VALUE not in record.getMessage(), (
                f"actor_id value must not be logged; found in: {record.getMessage()!r}"
            )

    def test_does_not_log_actor_id_value_on_write_failure(self, caplog):
        """The actor_id value must not appear even when create_event raises."""
        client = _stub_client(create_event_side_effect=RuntimeError("fail"))
        with caplog.at_level(logging.WARNING, logger="agents.supervisor.session_memory"):
            record_turn(
                client, "mem-id", self.ACTOR_VALUE, self.SESSION_VALUE, "prompt", "result"
            )
        for record in caplog.records:
            assert self.ACTOR_VALUE not in record.getMessage(), (
                f"actor_id value must not be logged on failure; found in: {record.getMessage()!r}"
            )

    def test_does_not_log_actor_id_value_on_hydrate_success(self, caplog):
        """The actor_id value must not appear in any log record from load_recent_turns."""
        turns = [[
            {"role": "USER",      "content": {"text": "hi"}},
            {"role": "ASSISTANT", "content": {"text": "hello"}},
        ]]
        client = _stub_client(get_last_k_turns_return=turns)
        with caplog.at_level(logging.DEBUG, logger="agents.supervisor.session_memory"):
            load_recent_turns(client, "mem-id", self.ACTOR_VALUE, self.SESSION_VALUE)
        for record in caplog.records:
            assert self.ACTOR_VALUE not in record.getMessage(), (
                f"actor_id value must not be logged on hydrate; found in: {record.getMessage()!r}"
            )

    def test_does_not_log_actor_id_value_on_hydrate_failure(self, caplog):
        """The actor_id value must not appear even when get_last_k_turns raises."""
        client = _stub_client(get_last_k_turns_side_effect=RuntimeError("fail"))
        with caplog.at_level(logging.WARNING, logger="agents.supervisor.session_memory"):
            load_recent_turns(client, "mem-id", self.ACTOR_VALUE, self.SESSION_VALUE)
        for record in caplog.records:
            assert self.ACTOR_VALUE not in record.getMessage(), (
                f"actor_id value must not be logged on hydrate failure; "
                f"found in: {record.getMessage()!r}"
            )

    def test_session_id_is_still_logged(self, caplog):
        """session_id is safe to log and must still appear for correlation."""
        client = _stub_client(create_event_side_effect=RuntimeError("fail"))
        with caplog.at_level(logging.WARNING, logger="agents.supervisor.session_memory"):
            record_turn(
                client, "mem-id", self.ACTOR_VALUE, self.SESSION_VALUE, "p", "r"
            )
        messages = [r.getMessage() for r in caplog.records]
        assert any(self.SESSION_VALUE in m for m in messages), (
            "session_id must be logged for failure correlation; "
            f"got records: {messages!r}"
        )


# ---------------------------------------------------------------------------
# F3.3 — contract failures vs service failures are distinguishable
# ---------------------------------------------------------------------------


class TestFailureDistinguishability:
    """Shape/contract failures and client/service failures must log differently.

    Both return [] and neither raises (spec D4 unchanged).  The difference is:
    - Contract failure: KeyError/TypeError/IndexError reading the response shape
      → logged at ERROR, message names the contract and points at docs/tech.md.
    - Service failure: any other exception (throttle, network, auth)
      → logged at WARNING.

    A genuine zero-event session is silent (not a failure).

    Mutation that was applied and confirmed to FAIL ``test_contract_failure_logged_at_error``:
      Collapsed both except branches back into one ``except Exception`` with a
      single WARNING → the test checking for an ERROR record FAILED because only
      WARNING records existed.  Separate branches restored.
    """

    def _make_malformed_turn(self) -> List:
        """A turn whose message is missing the 'content' key — triggers KeyError."""
        return [[{"role": "USER"}]]   # no 'content' key

    def test_service_failure_returns_empty_list(self):
        """A service error (RuntimeError) returns []."""
        client = _stub_client(get_last_k_turns_side_effect=RuntimeError("throttle"))
        result = load_recent_turns(client, "mem-id", "actor-abc", "sess-xyz")
        assert result == []

    def test_service_failure_logged_at_warning(self, caplog):
        """A client/service error is logged at WARNING level."""
        client = _stub_client(get_last_k_turns_side_effect=RuntimeError("throttle"))
        with caplog.at_level(logging.DEBUG, logger="agents.supervisor.session_memory"):
            load_recent_turns(client, "mem-id", "actor-abc", "sess-xyz")
        warning_msgs = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
        assert warning_msgs, "Expected at least one WARNING record for service failure"

    def test_service_failure_not_logged_at_error(self, caplog):
        """A transient service error must NOT log at ERROR (reserved for contract breaks)."""
        client = _stub_client(get_last_k_turns_side_effect=RuntimeError("throttle"))
        with caplog.at_level(logging.DEBUG, logger="agents.supervisor.session_memory"):
            load_recent_turns(client, "mem-id", "actor-abc", "sess-xyz")
        error_msgs = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]
        assert not error_msgs, (
            f"Service failure must not log at ERROR; got: {error_msgs!r}"
        )

    def test_contract_failure_returns_empty_list(self):
        """A malformed response (missing 'content') still returns []."""
        client = _stub_client(get_last_k_turns_return=self._make_malformed_turn())
        result = load_recent_turns(client, "mem-id", "actor-abc", "sess-xyz")
        assert result == []

    def test_contract_failure_logged_at_error(self, caplog):
        """A KeyError reading msg['content']['text'] is logged at ERROR."""
        client = _stub_client(get_last_k_turns_return=self._make_malformed_turn())
        with caplog.at_level(logging.DEBUG, logger="agents.supervisor.session_memory"):
            load_recent_turns(client, "mem-id", "actor-abc", "sess-xyz")
        error_msgs = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]
        assert error_msgs, "Expected an ERROR record for contract mismatch"

    def test_contract_failure_not_logged_at_warning(self, caplog):
        """A contract failure must log at ERROR, not WARNING, so it is distinguishable."""
        client = _stub_client(get_last_k_turns_return=self._make_malformed_turn())
        with caplog.at_level(logging.DEBUG, logger="agents.supervisor.session_memory"):
            load_recent_turns(client, "mem-id", "actor-abc", "sess-xyz")
        warning_msgs = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
        assert not warning_msgs, (
            f"Contract failure must not log at WARNING (reserved for service failures); "
            f"got: {warning_msgs!r}"
        )

    def test_contract_failure_message_names_contract(self, caplog):
        """The ERROR message must name the contract / docs/tech.md as suspect."""
        client = _stub_client(get_last_k_turns_return=self._make_malformed_turn())
        with caplog.at_level(logging.ERROR, logger="agents.supervisor.session_memory"):
            load_recent_turns(client, "mem-id", "actor-abc", "sess-xyz")
        error_msgs = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]
        combined = " ".join(error_msgs)
        assert "contract" in combined.lower() or "docs/tech.md" in combined, (
            "Contract-failure ERROR message must name the contract or docs/tech.md; "
            f"got: {error_msgs!r}"
        )

    def test_zero_event_session_is_silent(self, caplog):
        """An empty session (genuine first turn) must not produce any log records."""
        client = _stub_client(get_last_k_turns_return=[])
        with caplog.at_level(logging.DEBUG, logger="agents.supervisor.session_memory"):
            result = load_recent_turns(client, "mem-id", "actor-abc", "sess-xyz")
        assert result == []
        failure_records = [
            r for r in caplog.records
            if r.levelno >= logging.WARNING
        ]
        assert not failure_records, (
            f"Zero-event session must not produce WARNING/ERROR records; "
            f"got: {[r.getMessage() for r in failure_records]!r}"
        )

    def test_type_error_on_role_access_is_contract_failure(self, caplog):
        """TypeError reading msg['role'] (e.g. msg is None) is a contract failure → ERROR."""
        # Inner turn contains None instead of a dict
        client = _stub_client(get_last_k_turns_return=[[None]])
        with caplog.at_level(logging.DEBUG, logger="agents.supervisor.session_memory"):
            load_recent_turns(client, "mem-id", "actor-abc", "sess-xyz")
        error_msgs = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]
        assert error_msgs, "TypeError on role access must log at ERROR (contract failure)"


# ---------------------------------------------------------------------------
# F3.5 — _hydrate_k is bounded; non-positive K is handled explicitly
# ---------------------------------------------------------------------------


class TestHydrateK:
    """_hydrate_k applies a cap and handles non-positive values explicitly.

    Spec D3: K is 'configurable AND bounded' because prior turns cost prompt
    tokens on every turn.  Unbounded K is a slow latency regression (R4).

    Mutation that was applied and confirmed to FAIL ``test_hydrate_k_above_cap_is_clamped``:
      Removed the ``min(k, _MAX_HYDRATE_K)`` cap from both paths in _hydrate_k.
      The test asserting ``result <= _MAX_HYDRATE_K`` FAILED when called with
      k=_MAX_HYDRATE_K + 1.  Cap restored.
    """

    def test_cap_constant_is_defined(self):
        """_MAX_HYDRATE_K must be a positive integer constant."""
        assert isinstance(_MAX_HYDRATE_K, int)
        assert _MAX_HYDRATE_K > 0

    def test_hydrate_k_above_cap_is_clamped(self):
        """An explicit k above _MAX_HYDRATE_K is clamped to _MAX_HYDRATE_K."""
        result = _hydrate_k(k=_MAX_HYDRATE_K + 1)
        assert result == _MAX_HYDRATE_K, (
            f"k={_MAX_HYDRATE_K + 1} should be clamped to {_MAX_HYDRATE_K}, got {result}"
        )

    def test_hydrate_k_at_cap_is_not_clamped(self):
        """An explicit k equal to _MAX_HYDRATE_K is returned as-is."""
        result = _hydrate_k(k=_MAX_HYDRATE_K)
        assert result == _MAX_HYDRATE_K

    def test_hydrate_k_below_cap_is_unchanged(self):
        """An explicit k below _MAX_HYDRATE_K is returned unchanged."""
        result = _hydrate_k(k=5)
        assert result == 5

    def test_env_above_cap_is_clamped(self, monkeypatch):
        """VSA_MEMORY_HYDRATE_TURNS above _MAX_HYDRATE_K is clamped."""
        monkeypatch.setenv("VSA_MEMORY_HYDRATE_TURNS", str(_MAX_HYDRATE_K + 999))
        result = _hydrate_k()
        assert result == _MAX_HYDRATE_K, (
            f"env value above cap should be clamped to {_MAX_HYDRATE_K}, got {result}"
        )

    def test_zero_k_explicit_returns_one(self):
        """k=0 is non-positive; must become 1, not silently zero."""
        result = _hydrate_k(k=0)
        assert result == 1

    def test_negative_k_explicit_returns_one(self):
        """k=-5 is non-positive; must become 1."""
        result = _hydrate_k(k=-5)
        assert result == 1

    def test_zero_env_returns_one(self, monkeypatch):
        """VSA_MEMORY_HYDRATE_TURNS=0 is non-positive; must become 1."""
        monkeypatch.setenv("VSA_MEMORY_HYDRATE_TURNS", "0")
        result = _hydrate_k()
        assert result == 1

    def test_negative_env_returns_one(self, monkeypatch):
        """VSA_MEMORY_HYDRATE_TURNS=-3 is non-positive; must become 1."""
        monkeypatch.setenv("VSA_MEMORY_HYDRATE_TURNS", "-3")
        result = _hydrate_k()
        assert result == 1

    def test_non_numeric_env_returns_default(self, monkeypatch):
        """A non-numeric env value falls back to DEFAULT_K."""
        monkeypatch.setenv("VSA_MEMORY_HYDRATE_TURNS", "notanumber")
        result = _hydrate_k()
        assert result == DEFAULT_K

    def test_default_k_is_within_cap(self):
        """DEFAULT_K must be <= _MAX_HYDRATE_K (sanity check on constants)."""
        assert DEFAULT_K <= _MAX_HYDRATE_K

    def test_load_recent_turns_passes_capped_k_to_service(self, monkeypatch):
        """load_recent_turns forwards the capped k to get_last_k_turns."""
        monkeypatch.setenv("VSA_MEMORY_HYDRATE_TURNS", str(_MAX_HYDRATE_K + 500))
        client = _stub_client(get_last_k_turns_return=[])
        load_recent_turns(client, "mem-id", "actor-abc", "sess-xyz")
        _, _, call_kwargs = client.get_last_k_turns.mock_calls[0]
        assert call_kwargs.get("k") == _MAX_HYDRATE_K, (
            f"get_last_k_turns should receive capped k={_MAX_HYDRATE_K}, "
            f"got k={call_kwargs.get('k')}"
        )

    def test_explicit_k_above_cap_forwarded_as_cap(self):
        """An explicit k above _MAX_HYDRATE_K is forwarded to the service as the cap."""
        client = _stub_client(get_last_k_turns_return=[])
        load_recent_turns(client, "mem-id", "actor-abc", "sess-xyz", k=_MAX_HYDRATE_K + 1000)
        _, _, call_kwargs = client.get_last_k_turns.mock_calls[0]
        assert call_kwargs.get("k") == _MAX_HYDRATE_K


# ---------------------------------------------------------------------------
# F3.6 — MemoryTurn named-tuple; tuple-order asymmetry is explicit
# ---------------------------------------------------------------------------


class TestMemoryTurnType:
    """load_recent_turns returns MemoryTurn named-tuples, not plain tuples.

    MemoryTurn has .role and .text fields so the ordering cannot be confused
    positionally at T3.1's call site.  It is still a tuple subclass so
    existing positional-unpacking code (``role, text = turn``) continues to work.

    The ordering asymmetry is documented and must be pinned:
      record_turn writes  (text, role) — AgentCore API contract
      load_recent_turns returns (role, text) — conventional prompt-building order
    """

    def test_returns_memory_turn_instances(self):
        """load_recent_turns yields MemoryTurn named-tuples, not plain tuples."""
        turns = [[
            {"role": "USER",      "content": {"text": "hi"}},
            {"role": "ASSISTANT", "content": {"text": "hello"}},
        ]]
        client = _stub_client(get_last_k_turns_return=turns)
        result = load_recent_turns(client, "mem-id", "actor-abc", "sess-xyz")
        for item in result:
            assert isinstance(item, MemoryTurn), (
                f"Expected MemoryTurn, got {type(item).__name__}"
            )

    def test_memory_turn_has_role_attribute(self):
        """MemoryTurn exposes .role for unambiguous field access."""
        turns = [[{"role": "USER", "content": {"text": "text"}}]]
        client = _stub_client(get_last_k_turns_return=turns)
        result = load_recent_turns(client, "mem-id", "actor-abc", "sess-xyz")
        assert result[0].role == "USER"

    def test_memory_turn_has_text_attribute(self):
        """MemoryTurn exposes .text for unambiguous field access."""
        turns = [[{"role": "USER", "content": {"text": "the content"}}]]
        client = _stub_client(get_last_k_turns_return=turns)
        result = load_recent_turns(client, "mem-id", "actor-abc", "sess-xyz")
        assert result[0].text == "the content"

    def test_memory_turn_supports_positional_unpacking(self):
        """NamedTuple is a tuple subclass; positional unpacking still works.

        ``role, text = turn`` continues to work so no downstream code breaks.
        Role is index 0 (first), text is index 1 (second).
        """
        turn = MemoryTurn(role="USER", text="hello")
        role, text = turn
        assert role == "USER"
        assert text == "hello"

    def test_memory_turn_role_is_index_zero(self):
        """role is at index 0 — confirms (role, text) positional order."""
        turn = MemoryTurn(role="ASSISTANT", text="reply")
        assert turn[0] == "ASSISTANT", "role must be index 0 in MemoryTurn"

    def test_memory_turn_text_is_index_one(self):
        """text is at index 1 — confirms (role, text) positional order."""
        turn = MemoryTurn(role="ASSISTANT", text="reply")
        assert turn[1] == "reply", "text must be index 1 in MemoryTurn"

    def test_write_order_is_text_then_role(self):
        """record_turn writes (text, role) — the OPPOSITE of load_recent_turns.

        This test pins the asymmetry explicitly so a T3.1 implementer sees both
        orderings in one place and cannot confuse them silently.
        """
        client = _stub_client()
        record_turn(client, "mem-id", "actor-abc", "sess-xyz", "prompt text", "asst reply")
        call_args = client.create_event.call_args
        messages = call_args[0][3]
        first_text, first_role = messages[0]
        # record_turn sends (text, role) — text first, role second.
        assert first_role == "USER", (
            "record_turn write order is (text, role) — role is index 1, not index 0"
        )
        assert first_text == "prompt text"

    def test_return_order_is_role_then_text(self):
        """load_recent_turns returns (role, text) — the OPPOSITE of record_turn.

        Use .role/.text attributes (or be explicit about index) at T3.1 call sites.
        """
        turns = [[{"role": "USER", "content": {"text": "the utterance"}}]]
        client = _stub_client(get_last_k_turns_return=turns)
        result = load_recent_turns(client, "mem-id", "actor-abc", "sess-xyz")
        assert result[0][0] == "USER", (
            "load_recent_turns return order is (role, text) — role is index 0"
        )
        assert result[0][1] == "the utterance"

    def test_empty_list_still_returned_on_no_history(self):
        """[] is returned unchanged for zero events (backward-compatible)."""
        client = _stub_client(get_last_k_turns_return=[])
        result = load_recent_turns(client, "mem-id", "actor-abc", "sess-xyz")
        assert result == []


# ---------------------------------------------------------------------------
# T2.2 — load_recent_turns (original tests preserved)
# ---------------------------------------------------------------------------


class TestLoadRecentTurns:
    """Tests for load_recent_turns()."""

    def test_returns_empty_list_for_zero_events(self):
        """A session with no events returns [] — no raise, no None (spec note)."""
        client = _stub_client(get_last_k_turns_return=[])
        result = load_recent_turns(client, "mem-id", "actor-abc", "sess-xyz")
        assert result == []

    def test_ordering_oldest_first(self):
        """Turns are returned OLDEST-FIRST regardless of the service's order.

        The service returns NEWEST-FIRST in the outer list.  load_recent_turns
        must reverse the outer list.

        Mutation test: reversed the stub to already-oldest-first (wrong for
        what the service returns), then checked that a reversal inside the
        function produced newest-first output → test FAILED.
        Specifically: without reversing the outer list, turn-1 messages appear
        before turn-2 messages, confirming the reversal is necessary.
        """
        client = _stub_client(get_last_k_turns_return=_NEWEST_FIRST_TWO_TURNS)
        result = load_recent_turns(client, "mem-id", "actor-abc", "sess-xyz")

        # Expected: turn-1 (oldest) first, then turn-2 (newest)
        expected = [
            MemoryTurn("USER",      "turn-1-user"),
            MemoryTurn("ASSISTANT", "turn-1-assistant"),
            MemoryTurn("USER",      "turn-2-user"),
            MemoryTurn("ASSISTANT", "turn-2-assistant"),
        ]
        assert result == expected, (
            f"Ordering wrong.\n"
            f"  Expected: {expected}\n"
            f"  Got:      {result}\n"
            "Turns must be oldest-first; messages within a turn must be "
            "USER-before-ASSISTANT (chronological)."
        )

    def test_ordering_inner_list_not_reversed(self):
        """Messages WITHIN a turn remain chronological (USER before ASSISTANT)."""
        client = _stub_client(get_last_k_turns_return=_NEWEST_FIRST_TWO_TURNS)
        result = load_recent_turns(client, "mem-id", "actor-abc", "sess-xyz")

        # The first message in the output must be USER (start of oldest turn).
        assert result[0].role == "USER", (
            f"First message should be USER, got {result[0].role!r}. "
            "Inner-list ordering is incorrect — do not reverse within a turn."
        )
        # The message immediately after that USER must be ASSISTANT.
        assert result[1].role == "ASSISTANT", (
            f"Second message should be ASSISTANT, got {result[1].role!r}. "
            "USER must precede ASSISTANT within the same turn."
        )

    def test_text_extracted_from_nested_content(self):
        """Text comes from msg['content']['text'] — NOT from flat msg['text']."""
        # A message with only 'content' and 'role' keys (as observed live).
        turns = [[
            {"role": "USER",      "content": {"text": "the actual user text"}},
            {"role": "ASSISTANT", "content": {"text": "the actual assistant text"}},
        ]]
        client = _stub_client(get_last_k_turns_return=turns)
        result = load_recent_turns(client, "mem-id", "actor-abc", "sess-xyz")

        assert result == [
            MemoryTurn("USER",      "the actual user text"),
            MemoryTurn("ASSISTANT", "the actual assistant text"),
        ]

    def test_role_preserved_uppercase(self):
        """Roles are returned as-is from the service — UPPERCASE."""
        turns = [[
            {"role": "USER",      "content": {"text": "hi"}},
            {"role": "ASSISTANT", "content": {"text": "hello"}},
        ]]
        client = _stub_client(get_last_k_turns_return=turns)
        result = load_recent_turns(client, "mem-id", "actor-abc", "sess-xyz")

        assert result[0].role == "USER"
        assert result[1].role == "ASSISTANT"

    def test_passes_k_to_service(self):
        """The effective k is forwarded to get_last_k_turns."""
        client = _stub_client(get_last_k_turns_return=[])
        load_recent_turns(client, "mem-id", "actor-abc", "sess-xyz", k=5)
        client.get_last_k_turns.assert_called_once_with(
            "mem-id", "actor-abc", "sess-xyz", k=5
        )

    def test_default_k_from_env(self, monkeypatch):
        """k defaults to VSA_MEMORY_HYDRATE_TURNS from the environment."""
        monkeypatch.setenv("VSA_MEMORY_HYDRATE_TURNS", "7")
        client = _stub_client(get_last_k_turns_return=[])
        load_recent_turns(client, "mem-id", "actor-abc", "sess-xyz")
        _, _, call_kwargs = client.get_last_k_turns.mock_calls[0]
        assert call_kwargs.get("k") == 7

    def test_default_k_fallback_when_env_absent(self, monkeypatch):
        """k falls back to DEFAULT_K when VSA_MEMORY_HYDRATE_TURNS is not set."""
        monkeypatch.delenv("VSA_MEMORY_HYDRATE_TURNS", raising=False)
        client = _stub_client(get_last_k_turns_return=[])
        load_recent_turns(client, "mem-id", "actor-abc", "sess-xyz")
        _, _, call_kwargs = client.get_last_k_turns.mock_calls[0]
        assert call_kwargs.get("k") == DEFAULT_K

    def test_returns_empty_list_when_memory_id_absent(self):
        """Returns [] when memory_id is empty or None."""
        for bad_id in ("", None):
            client = _stub_client()
            result = load_recent_turns(client, bad_id, "actor-abc", "sess-xyz")
            assert result == []
            client.get_last_k_turns.assert_not_called()

    def test_returns_empty_list_when_actor_id_absent(self):
        """Returns [] when actor_id is empty or None (spec D1)."""
        for bad_actor in ("", None):
            client = _stub_client()
            result = load_recent_turns(client, "mem-id", bad_actor, "sess-xyz")
            assert result == []
            client.get_last_k_turns.assert_not_called()

    def test_returns_empty_list_on_client_exception(self):
        """Returns [] when get_last_k_turns raises — never propagates.

        Companion to test_record_turn.test_swallows_client_error.
        """
        client = _stub_client(
            get_last_k_turns_side_effect=RuntimeError("memory unavailable")
        )
        result = load_recent_turns(client, "mem-id", "actor-abc", "sess-xyz")
        assert result == []

    def test_returns_empty_list_on_any_exception(self):
        """Returns [] for any exception type from the client."""
        for exc in (ValueError("v"), KeyError("k"), ConnectionError("c")):
            client = _stub_client(get_last_k_turns_side_effect=exc)
            result = load_recent_turns(client, "mem-id", "actor-abc", "sess-xyz")
            assert result == []

    def test_never_raises(self):
        """load_recent_turns must never propagate exceptions to its caller."""
        client = _stub_client(
            get_last_k_turns_side_effect=Exception("catastrophic failure")
        )
        # No pytest.raises — the call itself is the assertion.
        load_recent_turns(client, "mem-id", "actor-abc", "sess-xyz")

    def test_single_turn_single_message(self):
        """Handles a turn with only one message (e.g. TOOL message alone)."""
        turns = [[{"role": "TOOL", "content": {"text": "tool output"}}]]
        client = _stub_client(get_last_k_turns_return=turns)
        result = load_recent_turns(client, "mem-id", "actor-abc", "sess-xyz")
        assert result == [MemoryTurn("TOOL", "tool output")]



# ---------------------------------------------------------------------------
# F4.2 — exception class name is logged; no payload / actor_id leaks
# ---------------------------------------------------------------------------


class TestExceptionClassLogging:
    """The exception CLASS NAME must appear in failure log records.

    F3.3 separated contract failures (ERROR) from service failures (WARNING),
    but every service failure still looked alike: an ``AccessDeniedException``
    (permanent — IAM gap, feature dead) was indistinguishable from a
    ``ThrottlingException`` (transient, self-healing).  F4.2 adds
    ``type(exc).__name__`` to both WARNING paths (record_turn and
    load_recent_turns) and to the contract-break ERROR.

    Rules enforced here:
    - The exception class name appears in the log record.
    - ``str(exc)`` / the exception message / args are NOT logged (a service
      error message can echo request content — VINs, session data).
    - actor_id value and any turn payload do not leak (independent of F3.2's
      existing tests, which are NOT weakened by this class).

    Mutations applied and confirmed FAIL during development:
    - Replaced ``type(exc).__name__`` with a literal ``""`` in the WARNING
      format string → ``test_service_failure_hydrate_logs_exc_class_name`` FAILED
      because ``AccessDeniedException`` was absent from every record.
    - Replaced ``type(exc).__name__`` with a literal ``""`` in the WARNING
      format string for record_turn → ``test_service_failure_record_logs_exc_class_name``
      FAILED for the same reason.
    - Added ``str(exc)`` (containing a VIN-shaped value) to the format string →
      ``test_no_exc_message_in_service_failure_warning`` FAILED.
    """

    # A class name that looks like a real AWS SDK exception.
    # Using a plain Exception subclass avoids any SDK import dependency
    # while producing a class name that exercises the property.
    class AccessDeniedException(Exception):
        """Simulates botocore / bedrock_agentcore AccessDeniedException."""

    class ThrottlingException(Exception):
        """Simulates botocore / bedrock_agentcore ThrottlingException."""

    # A VIN-shaped value we will assert NEVER appears in any log record.
    _FAKE_VIN = "1HGBH41JXMN109186"
    # actor_id value that must not appear.
    _ACTOR = "sub-sensitive-abc123"

    def _exc_with_vin(self) -> Exception:
        """Return an AccessDeniedException whose str() contains a VIN-shaped value.

        This tests that even when the service error message carries identifying
        content, we log only the class name — not str(exc).
        """
        return self.AccessDeniedException(
            f"User is not authorized to perform ListEvents on resource {self._FAKE_VIN}"
        )

    # ------------------------------------------------------------------
    # record_turn service-failure WARNING
    # ------------------------------------------------------------------

    def test_service_failure_record_logs_exc_class_name(self, caplog):
        """record_turn WARNING includes the exception class name."""
        client = _stub_client(
            create_event_side_effect=self.AccessDeniedException("access denied")
        )
        with caplog.at_level(logging.WARNING, logger="agents.supervisor.session_memory"):
            record_turn(client, "mem-id", self._ACTOR, "sess-xyz", "prompt", "result")
        warning_msgs = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
        assert warning_msgs, "Expected at least one WARNING record"
        combined = " ".join(warning_msgs)
        assert "AccessDeniedException" in combined, (
            f"Exception class name 'AccessDeniedException' must appear in WARNING; "
            f"got: {warning_msgs!r}"
        )

    def test_service_failure_record_distinguishes_exception_types(self, caplog):
        """Different exception class names produce different WARNING messages."""
        for exc_cls, expected_name in [
            (self.AccessDeniedException, "AccessDeniedException"),
            (self.ThrottlingException, "ThrottlingException"),
            (RuntimeError, "RuntimeError"),
        ]:
            client = _stub_client(create_event_side_effect=exc_cls("msg"))
            with caplog.at_level(logging.WARNING, logger="agents.supervisor.session_memory"):
                record_turn(client, "mem-id", self._ACTOR, "sess-xyz", "p", "r")
            msgs = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
            combined = " ".join(msgs)
            assert expected_name in combined, (
                f"Expected {expected_name!r} in WARNING for {exc_cls.__name__}; "
                f"got: {msgs!r}"
            )
            caplog.clear()

    def test_no_exc_message_in_record_turn_warning(self, caplog):
        """str(exc) content must NOT appear in the record_turn WARNING record.

        A service error message can echo request content.  This test uses an
        exception whose str() contains a VIN-shaped value to verify the policy.
        """
        client = _stub_client(create_event_side_effect=self._exc_with_vin())
        with caplog.at_level(logging.WARNING, logger="agents.supervisor.session_memory"):
            record_turn(client, "mem-id", self._ACTOR, "sess-xyz", "prompt", "result")
        for record in caplog.records:
            msg = record.getMessage()
            assert self._FAKE_VIN not in msg, (
                f"VIN-shaped exc message must not be logged in record_turn; "
                f"found in: {msg!r}"
            )

    def test_actor_id_not_in_record_turn_warning_with_new_format(self, caplog):
        """actor_id value must not appear in the WARNING even with the new exc_type field."""
        client = _stub_client(create_event_side_effect=self.AccessDeniedException("denied"))
        with caplog.at_level(logging.WARNING, logger="agents.supervisor.session_memory"):
            record_turn(client, "mem-id", self._ACTOR, "sess-xyz", "prompt", "result")
        for record in caplog.records:
            msg = record.getMessage()
            assert self._ACTOR not in msg, (
                f"actor_id value must not be logged; found in: {msg!r}"
            )

    # ------------------------------------------------------------------
    # load_recent_turns service-failure WARNING
    # ------------------------------------------------------------------

    def test_service_failure_hydrate_logs_exc_class_name(self, caplog):
        """load_recent_turns WARNING includes the exception class name."""
        client = _stub_client(
            get_last_k_turns_side_effect=self.AccessDeniedException("access denied")
        )
        with caplog.at_level(logging.WARNING, logger="agents.supervisor.session_memory"):
            load_recent_turns(client, "mem-id", self._ACTOR, "sess-xyz")
        warning_msgs = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
        assert warning_msgs, "Expected at least one WARNING record for service failure"
        combined = " ".join(warning_msgs)
        assert "AccessDeniedException" in combined, (
            f"Exception class name 'AccessDeniedException' must appear in WARNING; "
            f"got: {warning_msgs!r}"
        )

    def test_service_failure_hydrate_distinguishes_exception_types(self, caplog):
        """ThrottlingException and AccessDeniedException produce different class names."""
        for exc_cls, expected_name in [
            (self.AccessDeniedException, "AccessDeniedException"),
            (self.ThrottlingException, "ThrottlingException"),
        ]:
            client = _stub_client(get_last_k_turns_side_effect=exc_cls("msg"))
            with caplog.at_level(logging.WARNING, logger="agents.supervisor.session_memory"):
                load_recent_turns(client, "mem-id", self._ACTOR, "sess-xyz")
            msgs = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
            combined = " ".join(msgs)
            assert expected_name in combined, (
                f"Expected {expected_name!r} in WARNING for {exc_cls.__name__}; "
                f"got: {msgs!r}"
            )
            caplog.clear()

    def test_no_exc_message_in_service_failure_warning(self, caplog):
        """str(exc) content must NOT appear in the load_recent_turns WARNING.

        VIN-shaped content in the exc message must be suppressed.
        """
        client = _stub_client(get_last_k_turns_side_effect=self._exc_with_vin())
        with caplog.at_level(logging.WARNING, logger="agents.supervisor.session_memory"):
            load_recent_turns(client, "mem-id", self._ACTOR, "sess-xyz")
        for record in caplog.records:
            msg = record.getMessage()
            assert self._FAKE_VIN not in msg, (
                f"VIN-shaped exc message must not be logged; found in: {msg!r}"
            )

    def test_actor_id_not_in_hydrate_warning_with_new_format(self, caplog):
        """actor_id value must not appear in the hydrate WARNING."""
        client = _stub_client(
            get_last_k_turns_side_effect=self.AccessDeniedException("denied")
        )
        with caplog.at_level(logging.WARNING, logger="agents.supervisor.session_memory"):
            load_recent_turns(client, "mem-id", self._ACTOR, "sess-xyz")
        for record in caplog.records:
            msg = record.getMessage()
            assert self._ACTOR not in msg, (
                f"actor_id value must not appear in hydrate WARNING; found in: {msg!r}"
            )

    # ------------------------------------------------------------------
    # contract-break ERROR also carries the class name
    # ------------------------------------------------------------------

    def test_contract_failure_error_logs_exc_class_name(self, caplog):
        """The contract-break ERROR also includes the exception class name.

        The class name is a standalone searchable token in addition to %r.
        """
        # A malformed turn triggers KeyError — a known contract-break class.
        client = _stub_client(get_last_k_turns_return=[[{"role": "USER"}]])  # no 'content' key
        with caplog.at_level(logging.ERROR, logger="agents.supervisor.session_memory"):
            load_recent_turns(client, "mem-id", self._ACTOR, "sess-xyz")
        error_msgs = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]
        assert error_msgs, "Expected at least one ERROR record for contract failure"
        combined = " ".join(error_msgs)
        assert "KeyError" in combined, (
            f"Contract-break ERROR must name the exception class 'KeyError'; "
            f"got: {error_msgs!r}"
        )

    def test_contract_failure_error_does_not_log_actor_id(self, caplog):
        """actor_id must not appear in the contract-break ERROR record."""
        client = _stub_client(get_last_k_turns_return=[[{"role": "USER"}]])
        with caplog.at_level(logging.ERROR, logger="agents.supervisor.session_memory"):
            load_recent_turns(client, "mem-id", self._ACTOR, "sess-xyz")
        for record in caplog.records:
            msg = record.getMessage()
            assert self._ACTOR not in msg, (
                f"actor_id value must not appear in contract-break ERROR; found in: {msg!r}"
            )
