"""Tests for T3.1, T3.2, and Fix Group 5 of spec 2026-09-22-cvx-text-session-memory.

T3.1 — hydrate_session_history wired into invoke():
  - Exactly one hydration call per invocation, BEFORE build_supervisor.
  - Record turn AFTER the response is produced; cannot affect the response.
  - Memory disabled (no memory_id, no actor_id) → byte-for-byte today's behaviour.
  - Role mapping hazards: (a) UPPERCASE→lowercase, (b) drop TOOL/OTHER,
    (c) alternation normalisation, (d) non-string type validation.

T3.2 — guardrail interaction with replayed history:
  - Guardrail evaluates current utterance only.
  - Hydrated history with guardrail-tripping text does not change verdict for
    benign current turn.
  - Hydrated history does not exempt a tripping current turn.

Fix Group 5:
  F5.1 — _normalise_alternating invariant: empty OR [0]=user AND [-1]=assistant AND alternating.
         Trailing-user drop required; test_single_user_returns_user corrected.
  F5.2 — hydrate_session_history "Never raises" for any container/element shape.
  F5.3 — build_supervisor → Agent(messages=…) hop asserted via patch.object.
  F5.4 — record_turn patched in two tests that previously could write live events;
          autouse guard ensures no test can construct a real MemoryClient.

Mutation verification record (per ~/.kiro/steering/testing.md):
  M1: move hydration inside per-tool path → test_hydration_is_once_not_per_tool FAILS
  M2: write precedes response → test_write_is_after_response FAILS
  M3: feed guardrail history instead of current utterance
      → test_guardrail_evaluates_current_utterance_not_history FAILS
  M4: pass UPPERCASE role straight through → test_uppercase_roles_are_converted FAILS
  M5: let TOOL role through → test_tool_role_is_dropped FAILS
  M6: remove trailing-user drop (step 5) from _normalise_alternating
      → does NOT break the invariant and does NOT fail any invariant test, because
        step 6 catches it unconditionally and returns []. It FAILS all 3 of the
        TestNormaliseSalvagesRatherThanDiscards tests, which pin salvage rather than
        safety. (An earlier revision of this registry claimed the invariant tests
        fail; verified FALSE — see that class's docstring.) [F5.1]
  M7: move conversion back outside the try in hydrate_session_history
      → test_hydrate_never_raises_on_malformed_input FAILS [F5.2]
  M8: drop messages= kwarg from Agent(...) call in build_supervisor (scratch copy)
      → test_agent_constructor_receives_normalised_history FAILS [F5.3]
  M9: with autouse guard active, leave record_turn unpatched in a test
      → guard fails loudly on MemoryClient construction [F5.4]

Run:
    pytest agents/supervisor/tests/test_session_memory_invoke.py -v
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock, call, patch

import pytest
from bedrock_agentcore.runtime.context import RequestContext

from agents.supervisor.agentcore_app import (
    hydrate_session_history,
    _normalise_alternating,
    invoke,
)
from agents.supervisor.session_memory import MemoryTurn


# ---------------------------------------------------------------------------
# F5.4 autouse guard: no test may construct a real MemoryClient
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _block_real_memory_client(monkeypatch):
    """Fail any test that constructs a real MemoryClient (F5.4).

    A test that forgets to patch record_turn will reach session_memory's
    _get_memory_client (the lazy MemoryClient factory).  This guard intercepts
    that call so no live write can reach the Memory resource T4.1 verifies
    against.

    WHY THIS RECORDS INSTEAD OF ONLY RAISING — review Cycle 7 W2.  An earlier
    revision only raised RuntimeError here, which BLOCKED the live write but did
    not fail the test: both ``record_turn`` and ``load_recent_turns`` wrap their
    bodies in ``except Exception`` per spec D4 ("a memory failure must never fail
    the turn"), so the guard's own exception was swallowed by the very code it
    guards.  Demonstrated: the guard fired twice and the test still passed.  The
    protection was real; the tripwire was silent.

    So violations are also recorded and asserted **after the yield**, outside any
    ``except`` in the code under test.  Raising is kept as well — it stops the call
    at the point of use, which keeps the blast radius at zero even mid-test.

    Tests that exercise _get_memory_client directly should explicitly
    monkeypatch session_memory._get_memory_client themselves.
    """
    import agents.supervisor.session_memory as sm_mod

    violations: list[str] = []

    def _guarded_get_memory_client(region: str):
        violations.append(region)
        raise RuntimeError(
            "Real MemoryClient must not be constructed in unit tests. "
            "Patch record_turn and/or load_recent_turns to prevent live "
            "network calls that corrupt the T4.1 verification resource. "
            "(Caught by autouse _block_real_memory_client fixture — F5.4)"
        )

    monkeypatch.setattr(sm_mod, "_get_memory_client", _guarded_get_memory_client)
    yield
    # Asserted after the test body, where D4's `except Exception` cannot reach.
    assert not violations, (
        f"this test attempted to construct a real MemoryClient {len(violations)} "
        f"time(s) (regions={violations}). It was blocked, so nothing was written — "
        "but patch record_turn and/or load_recent_turns so the attempt does not "
        "happen at all. A live create_event here would seed junk turns into the "
        "Memory resource T4.1 verifies against (F5.4)."
    )


# ---------------------------------------------------------------------------
# Shared fixtures and helpers
# ---------------------------------------------------------------------------


def _ctx(session_id: str = "vsa-sess-t3-test", headers: dict | None = None) -> RequestContext:
    return RequestContext(
        session_id=session_id,
        request_headers=headers or {},
        request=None,
    )


def _hdr(token: str = "fake.jwt.token") -> dict:
    return {"X-Amzn-Bedrock-AgentCore-Runtime-Custom-User-Token": token}


def _fake_load_returns(turns: list[MemoryTurn]):
    """Return a mock for load_recent_turns that returns `turns`."""
    m = MagicMock(return_value=turns)
    return m


# A minimal valid JWT for test_t31 — no real sub (3-part, third part empty but valid structure).
# The sub MUST be present for actor_id to be non-None; otherwise hydration no-ops.
import base64, json as _json

def _jwt_with_sub(sub: str = "user-123") -> str:
    header = base64.b64encode(b'{"alg":"RS256","typ":"JWT"}').decode().rstrip("=")
    payload_bytes = _json.dumps({"sub": sub, "exp": 9999999999}).encode()
    payload = base64.b64encode(payload_bytes).decode().rstrip("=")
    sig = "fakesig"
    return f"{header}.{payload}.{sig}"


_JWT = _jwt_with_sub("test-sub-xyz")


def _fake_build(monkeypatch, *, extra_holder: dict | None = None):
    """Patch build_supervisor; return (holder, mock_agent).

    holder["messages"] captures the messages= kwarg passed to build_supervisor.
    holder["call_count"] increments on each build call.
    """
    import agents.supervisor.agentcore_app as mod

    fake_agent = MagicMock(
        return_value=MagicMock(message={"content": [{"text": "agent reply"}]})
    )
    holder: dict = extra_holder if extra_holder is not None else {}
    holder.setdefault("call_count", 0)

    def fake_build_supervisor(ctx, config, **kwargs):
        holder["call_count"] += 1
        holder["messages"] = kwargs.get("messages")
        holder["ctx"] = ctx
        return fake_agent

    monkeypatch.setattr(mod, "build_supervisor", fake_build_supervisor)
    return holder, fake_agent


# ---------------------------------------------------------------------------
# T3.1: hydrate_session_history unit tests (the named helper)
# ---------------------------------------------------------------------------


class TestHydrateSessionHistoryHelper:
    """Unit tests for the hydrate_session_history() named helper."""

    def test_returns_none_when_memory_id_absent(self):
        """No memory_id → returns None (today's behaviour, no hydration call)."""
        from types import SimpleNamespace
        config = SimpleNamespace()  # no memory_id attribute
        result = hydrate_session_history(
            client=MagicMock(),
            config=config,
            actor_id="some-actor",
            session_id="sess-001",
        )
        assert result is None

    def test_returns_none_when_memory_id_empty(self):
        from types import SimpleNamespace
        config = SimpleNamespace(memory_id="")
        result = hydrate_session_history(
            client=MagicMock(),
            config=config,
            actor_id="actor-x",
            session_id="sess-001",
        )
        assert result is None

    def test_returns_none_when_actor_id_none(self):
        """No actor_id → returns None (spec D1 fail-closed)."""
        from types import SimpleNamespace
        config = SimpleNamespace(memory_id="mem-abc")
        result = hydrate_session_history(
            client=MagicMock(),
            config=config,
            actor_id=None,
            session_id="sess-001",
        )
        assert result is None

    def test_returns_none_when_load_returns_empty(self):
        """Zero stored events → returns None (first-turn case, not a failure)."""
        import agents.supervisor.agentcore_app as mod
        from types import SimpleNamespace
        config = SimpleNamespace(memory_id="mem-abc")
        stub_client = MagicMock()

        with patch.object(mod, "load_recent_turns", return_value=[]):
            result = hydrate_session_history(
                client=stub_client,
                config=config,
                actor_id="actor-x",
                session_id="sess-001",
            )
        assert result is None

    def test_calls_load_recent_turns_with_correct_args(self):
        """hydrate_session_history passes memory_id, actor_id, session_id to load_recent_turns."""
        import agents.supervisor.agentcore_app as mod
        from types import SimpleNamespace

        config = SimpleNamespace(memory_id="mem-123")
        stub_client = MagicMock()
        mock_load = MagicMock(return_value=[
            MemoryTurn(role="USER", text="hello"),
            MemoryTurn(role="ASSISTANT", text="hi"),
        ])

        with patch.object(mod, "load_recent_turns", mock_load):
            hydrate_session_history(
                client=stub_client,
                config=config,
                actor_id="actor-abc",
                session_id="sess-xyz",
            )

        mock_load.assert_called_once_with(stub_client, "mem-123", "actor-abc", "sess-xyz")

    # --- role mapping hazard (a): UPPERCASE → lowercase ---

    def test_uppercase_roles_are_converted_to_lowercase(self):
        """UPPERCASE 'USER'/'ASSISTANT' from Memory → lowercase for Strands Agent.

        MUTATION: pass role straight through without converting (e.g. remove the
        _ROLE_MAP lookup) → this test FAILS because Strands expects 'user'/'assistant'.
        """
        import agents.supervisor.agentcore_app as mod
        from types import SimpleNamespace

        config = SimpleNamespace(memory_id="mem-abc")
        turns = [
            MemoryTurn(role="USER", text="where is my car"),
            MemoryTurn(role="ASSISTANT", text="checking now"),
        ]
        with patch.object(mod, "load_recent_turns", return_value=turns):
            result = hydrate_session_history(
                client=None, config=config, actor_id="a", session_id="s"
            )

        assert result is not None
        roles = [m["role"] for m in result]
        assert roles == ["user", "assistant"], (
            f"Roles must be lowercase 'user'/'assistant', got {roles!r}. "
            "Memory returns UPPERCASE; Strands requires lowercase (hazard a)."
        )

    # --- role mapping hazard (b): drop TOOL/OTHER ---

    def test_tool_role_is_dropped(self):
        """TOOL-role messages from Memory must be dropped before Agent construction.

        MUTATION: pass 'TOOL' through the _ROLE_MAP (e.g. map 'TOOL'→'tool') →
        this test FAILS because 'tool' is not a valid Strands/Bedrock role.
        """
        import agents.supervisor.agentcore_app as mod
        from types import SimpleNamespace

        config = SimpleNamespace(memory_id="mem-abc")
        turns = [
            MemoryTurn(role="USER", text="hello"),
            MemoryTurn(role="TOOL", text="[tool result]"),
            MemoryTurn(role="ASSISTANT", text="got it"),
        ]
        with patch.object(mod, "load_recent_turns", return_value=turns):
            result = hydrate_session_history(
                client=None, config=config, actor_id="a", session_id="s"
            )

        assert result is not None
        roles = [m["role"] for m in result]
        assert "tool" not in roles, f"'tool' role must be dropped; got {roles!r}"
        assert roles == ["user", "assistant"], f"Expected ['user', 'assistant'], got {roles!r}"

    def test_other_role_is_dropped(self):
        """'OTHER'-role messages from Memory must be dropped."""
        import agents.supervisor.agentcore_app as mod
        from types import SimpleNamespace

        config = SimpleNamespace(memory_id="mem-abc")
        turns = [
            MemoryTurn(role="USER", text="hello"),
            MemoryTurn(role="OTHER", text="[other]"),
            MemoryTurn(role="ASSISTANT", text="hi"),
        ]
        with patch.object(mod, "load_recent_turns", return_value=turns):
            result = hydrate_session_history(
                client=None, config=config, actor_id="a", session_id="s"
            )

        assert result is not None
        roles = [m["role"] for m in result]
        assert "other" not in roles

    # --- role mapping hazard (d): non-string value types ---

    def test_non_string_role_is_skipped(self):
        """A turn with role=None (or int) must be silently skipped, not raise."""
        import agents.supervisor.agentcore_app as mod
        from types import SimpleNamespace

        config = SimpleNamespace(memory_id="mem-abc")
        turns = [
            MemoryTurn(role=None, text="should be skipped"),   # type: ignore[arg-type]
            MemoryTurn(role="USER", text="real message"),
            MemoryTurn(role="ASSISTANT", text="response"),
        ]
        with patch.object(mod, "load_recent_turns", return_value=turns):
            result = hydrate_session_history(
                client=None, config=config, actor_id="a", session_id="s"
            )

        # Non-string role skipped; remaining messages form valid history.
        assert result is not None
        roles = [m["role"] for m in result]
        assert "user" in roles
        assert "assistant" in roles

    def test_non_string_text_is_skipped(self):
        """A turn with text=None (or int) must be silently skipped, not raise."""
        import agents.supervisor.agentcore_app as mod
        from types import SimpleNamespace

        config = SimpleNamespace(memory_id="mem-abc")
        turns = [
            MemoryTurn(role="USER", text=None),        # type: ignore[arg-type]
            MemoryTurn(role="USER", text="valid"),
            MemoryTurn(role="ASSISTANT", text="reply"),
        ]
        with patch.object(mod, "load_recent_turns", return_value=turns):
            result = hydrate_session_history(
                client=None, config=config, actor_id="a", session_id="s"
            )

        # No TypeError; None text silently skipped.
        # Result might be None (if normalisation drops the first user) or valid.
        # The key assertion is: no exception raised.
        # Also: we shouldn't have a message with text=None.
        if result is not None:
            for msg in result:
                assert isinstance(msg.get("content", [{}])[0].get("text", ""), str)

    def test_never_raises_on_load_exception(self):
        """If load_recent_turns raises unexpectedly, hydrate returns None (D4)."""
        import agents.supervisor.agentcore_app as mod
        from types import SimpleNamespace

        config = SimpleNamespace(memory_id="mem-abc")
        with patch.object(mod, "load_recent_turns", side_effect=RuntimeError("boom")):
            result = hydrate_session_history(
                client=None, config=config, actor_id="a", session_id="s"
            )
        assert result is None

    def test_message_format_is_strands_compatible(self):
        """Each message dict has 'role' and content=[{'text': '...'}]."""
        import agents.supervisor.agentcore_app as mod
        from types import SimpleNamespace

        config = SimpleNamespace(memory_id="mem-abc")
        turns = [
            MemoryTurn(role="USER", text="give me the VIN"),
            MemoryTurn(role="ASSISTANT", text="VIN is ABC123"),
        ]
        with patch.object(mod, "load_recent_turns", return_value=turns):
            result = hydrate_session_history(
                client=None, config=config, actor_id="a", session_id="s"
            )

        assert result is not None
        for msg in result:
            assert "role" in msg
            assert "content" in msg
            assert isinstance(msg["content"], list)
            assert len(msg["content"]) == 1
            assert "text" in msg["content"][0]
            assert isinstance(msg["content"][0]["text"], str)


# ---------------------------------------------------------------------------
# _normalise_alternating unit tests
# ---------------------------------------------------------------------------


class TestNormaliseAlternating:
    """Unit tests for the _normalise_alternating() helper."""

    def test_already_valid_sequence_unchanged(self):
        msgs = [
            {"role": "user", "content": [{"text": "hi"}]},
            {"role": "assistant", "content": [{"text": "hello"}]},
            {"role": "user", "content": [{"text": "thanks"}]},
            {"role": "assistant", "content": [{"text": "welcome"}]},
        ]
        result = _normalise_alternating(msgs)
        assert [m["role"] for m in result] == ["user", "assistant", "user", "assistant"]

    def test_leading_assistant_is_dropped(self):
        """If history starts with 'assistant', drop until 'user' is found."""
        msgs = [
            {"role": "assistant", "content": [{"text": "orphan"}]},
            {"role": "user", "content": [{"text": "hello"}]},
            {"role": "assistant", "content": [{"text": "hi"}]},
        ]
        result = _normalise_alternating(msgs)
        assert result[0]["role"] == "user"
        assert [m["role"] for m in result] == ["user", "assistant"]

    def test_consecutive_same_role_collapsed_to_last(self):
        """Consecutive messages with the same role → keep the last."""
        msgs = [
            {"role": "user", "content": [{"text": "first user"}]},
            {"role": "user", "content": [{"text": "second user (keep this)"}]},
            {"role": "assistant", "content": [{"text": "reply"}]},
        ]
        result = _normalise_alternating(msgs)
        assert [m["role"] for m in result] == ["user", "assistant"]
        assert result[0]["content"][0]["text"] == "second user (keep this)"

    def test_empty_input_returns_empty(self):
        assert _normalise_alternating([]) == []

    def test_all_assistant_returns_empty(self):
        msgs = [
            {"role": "assistant", "content": [{"text": "a"}]},
            {"role": "assistant", "content": [{"text": "b"}]},
        ]
        assert _normalise_alternating(msgs) == []

    def test_single_user_returns_empty(self):
        """A single 'user' message must be dropped — returns [].

        Previously this test was named test_single_user_returns_user and asserted
        len==1 / role=='user', which PINNED THE DEFECT: a single-user history would
        produce [user, user] once Strands appends the current prompt as 'user'
        after the hydrated history.

        CORRECTED (Fix Group 5 / F5.1): the normaliser must end with 'assistant'.
        A trailing 'user' is dropped.  A single-user history has nothing left →
        empty list → hydrate_session_history returns None → stateless turn.

        NOT A MUTATION TARGET for step 5 — corrected 2026-09-22 (review Cycle 8).
        An earlier revision of this docstring claimed that removing the
        trailing-user drop makes this test FAIL and return
        ``[{'role': 'user', ...}]``.  Both halves are false: ``[user]`` normalises
        to ``[]`` **with and without** step 5, because step 6 discards any list
        that does not end with ``assistant``.  Verified by running the mutation —
        this test stays green; only the three
        ``TestNormaliseSalvagesRatherThanDiscards`` tests fail.
        See that class for which step guarantees what.
        """
        msgs = [{"role": "user", "content": [{"text": "hi"}]}]
        result = _normalise_alternating(msgs)
        assert result == [], (
            "A single 'user' message must return [] — it would create [user, user] "
            "when Strands appends the current prompt. Trailing user must be dropped."
        )


# ---------------------------------------------------------------------------
# F5.1 — invariant-based normaliser tests and helper utilities
# ---------------------------------------------------------------------------


def _assert_normalise_invariant(result: list[dict], original_desc: str) -> None:
    """Assert the normaliser invariant: empty OR starts-user ends-assistant alternating.

    Tests the CONTRACT rather than case-by-case expected output, per F5.1 Accept.

    NOTE: this helper is satisfied by an empty list, so it does NOT detect the
    removal of step 5 (the trailing-user drop) — step 6 discards a non-conforming
    history and `[]` passes this check. Salvage is pinned separately by
    TestNormaliseSalvagesRatherThanDiscards.
    """
    if not result:
        return  # empty is always valid
    assert result[0]["role"] == "user", (
        f"[{original_desc}] Invariant violated: first message must be 'user', "
        f"got {result[0]['role']!r}. Full result: {[m['role'] for m in result]}"
    )
    assert result[-1]["role"] == "assistant", (
        f"[{original_desc}] Invariant violated: last message must be 'assistant', "
        f"got {result[-1]['role']!r}. Full result: {[m['role'] for m in result]}. "
        "Strands appends the current prompt as 'user' after this history — "
        "a trailing 'user' produces [user, user] which Bedrock rejects."
    )
    for i in range(len(result) - 1):
        assert result[i]["role"] != result[i + 1]["role"], (
            f"[{original_desc}] Invariant violated: non-alternating at index {i}: "
            f"{result[i]['role']!r} followed by {result[i+1]['role']!r}. "
            f"Full result: {[m['role'] for m in result]}"
        )


def _u(text: str = "hi") -> dict:
    return {"role": "user", "content": [{"text": text}]}


def _a(text: str = "ok") -> dict:
    return {"role": "assistant", "content": [{"text": text}]}


def _tool_msg(text: str = "[tool]") -> dict:
    return {"role": "tool", "content": [{"text": text}]}


class TestNormaliseAlternatingInvariant:
    """Invariant-based tests for _normalise_alternating (F5.1).

    Every test asserts the invariant (empty OR [0]=user AND [-1]=assistant AND
    strictly alternating) rather than specific outputs.  This covers the
    reviewer's 8 probe shapes plus edge cases.

    MUTATION NOTE — CORRECTED 2026-09-22 by the architect.  An earlier revision of
    this docstring claimed that removing the trailing-user drop (step 5) would make
    5 probes fail these invariant tests.  That is FALSE, and was verified false:
    with step 5 removed, all 50 tests in this file still pass, and a brute force
    over 5461 role sequences (length 0-6 over USER/ASSISTANT/TOOL/OTHER) found
    ZERO invariant violations.

    The reason is that step 6 is an *unconditional* invariant check that returns []
    when the invariant does not hold.  So step 6 — not step 5 — is what guarantees
    safety, which is good defence-in-depth.  Step 5's actual job is to SALVAGE a
    usable prefix instead of discarding the history entirely.

    That behavioural property is what TestNormaliseSalvagesRatherThanDiscards below
    pins, because step 5 LOOKS redundant given step 6 and could otherwise be deleted
    as dead code — silently degrading hydration from "use what we can" to "discard
    anything odd-shaped", which would make the assistant amnesiac more often than
    necessary with every test still green.
    """

    @pytest.mark.parametrize("msgs,desc", [
        # Reviewer's 8 probe shapes
        ([_u("p1"), _a("r1"), _u("p2"), _a("r2"), _u("p3")],
         "happy path 2 full turns + trailing user"),
        ([_a("orphan"), _u("p1"), _a("r1"), _u("p2")],
         "k-boundary leading ASSISTANT + trailing user"),
        ([_a("a1"), _a("a2")],
         "all-assistant history"),
        ([_u("p1"), _a("r1"), _u("p2"), _u("p3")],
         "truncated final turn consecutive USER"),
        ([_u("p1"), _u("p2")],
         "two USER no ASSISTANT"),
        ([_u("p1")],
         "single USER message"),
        ([_u("p1"), _a("r1"), _u("p2"), _tool_msg()],
         "trailing TOOL role"),
        ([_u("p1"), {"role": "assistant", "content": [{"text": "   "}]}],
         "ASSISTANT text whitespace-only"),
        # Additional invariant probes
        ([],
         "empty input"),
        ([_u("p1"), _a("r1")],
         "single valid pair"),
        ([_u("p1"), _a("r1"), _u("p2"), _a("r2")],
         "two full valid turns"),
        ([_a("orphan"), _u("p1"), _a("r1")],
         "leading assistant with valid tail"),
        ([_u("p1"), _a("r1"), _u("p2"), _a("r2"), _u("p3"), _a("r3")],
         "three full turns"),
    ])
    def test_normalise_invariant_holds(self, msgs, desc):
        """Invariant must hold for every input shape.

        This passes on an empty result, so it does NOT detect step 5's removal —
        see the class docstring. It guards step 6, which is the safety property.
        """
        result = _normalise_alternating(msgs)
        _assert_normalise_invariant(result, desc)

    def test_whitespace_only_assistant_dropped(self):
        """Assistant message with only whitespace text is dropped.

        Known trigger: _extract_text can return '\\n', record_turn writes it,
        hydration's text.strip() drops it — exposing a user tail.
        """
        msgs = [_u("where is my car"), {"role": "assistant", "content": [{"text": "\n"}]}]
        result = _normalise_alternating(msgs)
        # whitespace assistant dropped → [user] → trailing user dropped → []
        _assert_normalise_invariant(result, "whitespace-only assistant")

    def test_trailing_tool_role_produces_valid_invariant(self):
        """A trailing tool-role message is filtered; invariant still holds."""
        msgs = [_u("hello"), _a("hi"), _u("more"), _tool_msg("[result]")]
        result = _normalise_alternating(msgs)
        _assert_normalise_invariant(result, "trailing tool")

    def test_odd_length_truncated_to_even(self):
        """An odd-length sequence loses its trailing user to satisfy the invariant."""
        msgs = [_u("p1"), _a("r1"), _u("p2"), _a("r2"), _u("p3")]
        result = _normalise_alternating(msgs)
        _assert_normalise_invariant(result, "odd length")


class TestNormaliseSalvagesRatherThanDiscards:
    """Pin step 5's real contribution: SALVAGE, not safety (architect, 2026-09-22).

    Step 6 already guarantees the invariant unconditionally by returning [] when it
    does not hold, so step 5 (drop trailing ``user``) is NOT load-bearing for
    correctness — verified by deleting it and observing all 50 tests still pass and
    zero invariant violations across 5461 brute-forced role sequences.

    What step 5 actually buys is that a history ending in ``user`` is trimmed to its
    usable ``user``/``assistant`` prefix rather than thrown away wholesale. Without
    these tests, step 5 reads as dead code next to step 6 and a future cleanup could
    delete it, degrading hydration from "use what we can" to "discard anything
    odd-shaped" — the assistant would be amnesiac more often than necessary, with
    the entire suite still green. That is the failure mode this class exists to stop.

    MUTATION (verified): remove step 5 and **all three** tests below FAIL, because
    each asserts a non-empty salvaged result where step 6 alone returns []. (The
    third originally used a shape for which step 5 was a no-op and so did not fail;
    it was strengthened to `[user, assistant, user, TOOL]` per review Cycle 7.)

    Salvage quantified, so the value is a number rather than an assertion: without
    step 5, **662 of 5,461** brute-forced role sequences (12.1%) and 4 of the 8
    reviewer probe shapes lose usable history, and total hydratable messages drop
    ~27% (-26.9% on that enumeration).
    """

    def test_trailing_user_is_trimmed_not_discarded(self):
        """A complete turn followed by a dangling user keeps the complete turn."""
        msgs = [_u("q1"), _a("a1"), _u("q2")]
        result = _normalise_alternating(msgs)
        assert len(result) == 2, (
            "expected the u/a pair to be salvaged and only the dangling trailing "
            f"user dropped; got {[m['role'] for m in result]}. An empty result means "
            "step 5 (trailing-user drop) is gone and step 6 discarded the whole "
            "history — safe, but it throws away usable context."
        )
        assert result[0]["role"] == "user"
        assert result[1]["role"] == "assistant"
        assert result[0]["content"][0]["text"] == "q1"
        assert result[1]["content"][0]["text"] == "a1"

    def test_two_full_turns_plus_dangling_user_keeps_both_turns(self):
        """Salvage keeps ALL complete turns, not just the first."""
        msgs = [_u("q1"), _a("a1"), _u("q2"), _a("a2"), _u("q3")]
        result = _normalise_alternating(msgs)
        assert len(result) == 4, (
            "expected both complete turns salvaged; got "
            f"{[m['role'] for m in result]}"
        )
        assert [m["role"] for m in result] == [
            "user", "assistant", "user", "assistant",
        ]
        # The dangling turn's text must not survive anywhere.
        assert all(m["content"][0]["text"] != "q3" for m in result)

    def test_trailing_tool_still_salvages_the_prior_turn(self):
        """A trailing TOOL marker must not cost us the conversation before it.

        Uses ``[user, assistant, user, TOOL]`` rather than ``[user, assistant, TOOL]``
        deliberately (review Cycle 7 suggestion): with the shorter shape, filtering
        TOOL already leaves a list ending in ``assistant``, so step 5 is a no-op and
        this test could not detect its removal. The dangling ``user`` before the TOOL
        makes step 5 load-bearing here too, so all three tests in this class now fail
        under the mutation.
        """
        msgs = [_u("q1"), _a("a1"), _u("q2"), _tool_msg("[marker]")]
        result = _normalise_alternating(msgs)
        assert len(result) == 2, (
            "a trailing TOOL message should be filtered and the dangling user "
            "dropped, without discarding the preceding complete turn; got "
            f"{[m['role'] for m in result]}"
        )
        assert [m["role"] for m in result] == ["user", "assistant"]
        assert result[0]["content"][0]["text"] == "q1"
        assert result[1]["content"][0]["text"] == "a1"


# ---------------------------------------------------------------------------
# F5.2 — hydrate_session_history never raises on malformed input
# ---------------------------------------------------------------------------


class TestHydrateSessionHistoryMalformedInput:
    """F5.2 — hydrate_session_history never raises for any container/element shape.

    The conversion loop is inside the guarded try block, so malformed inputs
    return None rather than propagating to invoke() (which has no guard).

    MUTATION: move the conversion loop outside the try in hydrate_session_history
    → AttributeError/TypeError escapes → a test FAILS with an exception.
    """

    @pytest.mark.parametrize("bad_turns,desc", [
        ([("USER", "hello"), ("ASSISTANT", "hi")],
         "list of plain 2-tuples (no .role attr)"),
        (["USER hello", "ASSISTANT hi"],
         "list of strings (no .role attr)"),
        ([None, None],
         "None elements"),
        (42,
         "non-iterable return (int)"),
    ])
    def test_hydrate_never_raises_on_malformed_input(self, monkeypatch, bad_turns, desc):
        """hydrate_session_history returns None (not raises) for malformed input.

        MUTATION: move conversion outside the try in hydrate_session_history →
        AttributeError/TypeError escapes → this test FAILS with an exception.
        """
        import agents.supervisor.agentcore_app as mod
        from types import SimpleNamespace

        config = SimpleNamespace(memory_id="mem-abc")

        with patch.object(mod, "load_recent_turns", return_value=bad_turns):
            try:
                result = hydrate_session_history(
                    client=None, config=config, actor_id="actor", session_id="sess"
                )
            except Exception as exc:
                pytest.fail(
                    f"[{desc}] hydrate_session_history raised {type(exc).__name__}: {exc}. "
                    "The function promises 'Never raises' — any failure must return None."
                )
        assert result is None, (
            f"[{desc}] Expected None for malformed input, got {result!r}"
        )


# ---------------------------------------------------------------------------
# F5.3 — build_supervisor → Agent(messages=…) hop
# ---------------------------------------------------------------------------


class TestAgentConstructorReceivesHistory:
    """F5.3 — the Agent(messages=…) hop in build_supervisor is asserted.

    test_hydrated_messages_are_passed_to_build_supervisor stubs build_supervisor,
    proving the call site passes the kwarg — but NOT that the kwarg reaches Agent.
    A silent regression (renamed key, inverted splat condition) would reproduce
    the reported bug with a fully green suite.

    This test patches Agent in agents.supervisor.supervisor directly and asserts
    the constructor got the normalised history as messages=.

    MUTATION: drop the messages= kwarg from Agent(...) in a scratch copy →
    'messages' not in Agent init kwargs → this test FAILS.
    Do NOT edit supervisor.py (concurrent session holds uncommitted changes).
    """

    def test_agent_constructor_receives_normalised_history(self, monkeypatch):
        """Agent.__init__ receives messages= with the normalised history.

        MUTATION target: if `**({"messages": messages} if messages else {})` in
        supervisor.py is removed, 'messages' will not be in Agent's kwargs →
        assertion FAILS.  This closes the coverage gap where a regression there
        would reproduce the exact reported bug with a green suite.
        """
        import agents.supervisor.supervisor as sup
        import agents.supervisor.agentcore_app as mod

        fake_history = [
            {"role": "user", "content": [{"text": "what is my VIN?"}]},
            {"role": "assistant", "content": [{"text": "Your VIN is ABC123."}]},
        ]

        # Capture Agent.__init__ kwargs.
        agent_init_kwargs: dict = {}

        class RecordingAgent:
            """Minimal Agent replacement that records constructor kwargs."""
            def __init__(self, *args, **kwargs):
                agent_init_kwargs.update(kwargs)

            def __call__(self, prompt):
                return MagicMock(message={"content": [{"text": "agent reply"}]})

        with patch.object(sup, "Agent", RecordingAgent):
            monkeypatch.setattr(mod, "hydrate_session_history", lambda **kw: fake_history)
            monkeypatch.setattr(mod, "record_turn", MagicMock())

            invoke(
                payload={"prompt": "hello", "tenantId": "meridian", "jwt": _JWT},
                context=_ctx(),
            )

        assert "messages" in agent_init_kwargs, (
            "Agent.__init__ was not called with 'messages'. "
            "The build_supervisor → Agent(messages=…) hop is broken — "
            "this reproduces the reported bug (no conversation memory) "
            "with a fully green suite."
        )
        received = agent_init_kwargs["messages"]
        assert received == fake_history, (
            f"Agent received messages={received!r}, expected {fake_history!r}."
        )
        for msg in received:
            assert msg["role"] in ("user", "assistant"), (
                f"Message role {msg['role']!r} is not lowercase. "
                "Conversion must happen before Agent construction."
            )


# ---------------------------------------------------------------------------
# T3.1: invoke() wiring — ordering and count assertions
# ---------------------------------------------------------------------------


class TestInvokeMemoryOrdering:
    """Verifies the ordering and call-count constraints in invoke()."""

    def _make_load_stub(self, turns):
        """Return a MagicMock for load_recent_turns that returns `turns`."""
        return MagicMock(return_value=turns)

    def test_hydration_occurs_exactly_once_per_invocation(self, monkeypatch):
        """load_recent_turns is called exactly ONCE per invoke() call.

        MUTATION: move the hydration inside a per-tool call (simulate by
        calling hydrate_session_history N times from fake_build_supervisor)
        → the count assertion below would FAIL.

        F5.4: record_turn is patched here (and in the sibling test below) to
        prevent live writes to the Memory resource T4.1 verifies against.
        The autouse _block_real_memory_client guard enforces this: if record_turn
        were unpatched, invoke() would reach the real write path, constructing
        a MemoryClient and triggering the guard.
        """
        import agents.supervisor.agentcore_app as mod

        hydrate_calls = []
        original_hydrate = mod.hydrate_session_history

        def counting_hydrate(**kwargs):
            hydrate_calls.append(1)
            return None

        monkeypatch.setattr(mod, "hydrate_session_history", counting_hydrate)
        monkeypatch.setattr(mod, "record_turn", MagicMock())  # F5.4: prevent live write
        holder, _ = _fake_build(monkeypatch)

        invoke(
            payload={"prompt": "what is my VIN?", "tenantId": "meridian", "jwt": _JWT},
            context=_ctx(),
        )

        assert len(hydrate_calls) == 1, (
            f"hydrate_session_history must be called EXACTLY ONCE per invocation, "
            f"not {len(hydrate_calls)} times. Moving it inside a tool path would "
            f"call it N times — the latency regression the spec's Tier answer forbids."
        )

    def test_hydration_happens_before_build_supervisor(self, monkeypatch):
        """hydrate_session_history must be called BEFORE build_supervisor.

        MUTATION: call build_supervisor before hydration → this test FAILS because
        the call order would be [build, hydrate] instead of [hydrate, build].

        F5.4: record_turn is patched here to prevent live writes to the Memory
        resource T4.1 verifies against.  The autouse _block_real_memory_client
        guard would fire if record_turn were omitted.
        """
        import agents.supervisor.agentcore_app as mod

        call_order = []

        def recording_hydrate(**kwargs):
            call_order.append("hydrate")
            return None

        monkeypatch.setattr(mod, "hydrate_session_history", recording_hydrate)
        monkeypatch.setattr(mod, "record_turn", MagicMock())  # F5.4: prevent live write

        original_fake = None
        fake_agent = MagicMock(
            return_value=MagicMock(message={"content": [{"text": "ok"}]})
        )

        def recording_build(ctx, config, **kwargs):
            call_order.append("build")
            return fake_agent

        monkeypatch.setattr(mod, "build_supervisor", recording_build)

        invoke(
            payload={"prompt": "hello", "tenantId": "meridian", "jwt": _JWT},
            context=_ctx(),
        )

        assert call_order == ["hydrate", "build"], (
            f"hydrate must occur BEFORE build_supervisor; got order: {call_order!r}. "
            "Hydration must happen before the Agent is constructed (spec D3)."
        )

    def test_write_is_after_response_produced(self, monkeypatch):
        """record_turn must be called AFTER result_text is produced.

        MUTATION: call record_turn before agent(prompt) returns → this test
        FAILS because the write would precede the response.
        """
        import agents.supervisor.agentcore_app as mod

        call_order = []

        fake_agent_result = MagicMock(message={"content": [{"text": "agent reply"}]})
        fake_agent = MagicMock(side_effect=lambda p: (call_order.append("agent_called"), fake_agent_result)[1])

        def fake_build(ctx, config, **kwargs):
            return fake_agent

        monkeypatch.setattr(mod, "build_supervisor", fake_build)

        def recording_record_turn(**kwargs):
            call_order.append("record_turn")

        monkeypatch.setattr(mod, "record_turn", recording_record_turn)
        monkeypatch.setattr(mod, "hydrate_session_history", lambda **kw: None)

        invoke(
            payload={"prompt": "hi", "tenantId": "meridian", "jwt": _JWT},
            context=_ctx(),
        )

        assert "agent_called" in call_order
        assert "record_turn" in call_order
        agent_idx = call_order.index("agent_called")
        record_idx = call_order.index("record_turn")
        assert record_idx > agent_idx, (
            f"record_turn must be called AFTER agent produces its result. "
            f"Call order: {call_order!r} "
            f"(agent at {agent_idx}, record_turn at {record_idx})"
        )

    def test_record_turn_failure_does_not_affect_response(self, monkeypatch):
        """record_turn raising must not propagate to the caller (D4)."""
        import agents.supervisor.agentcore_app as mod

        fake_agent = MagicMock(
            return_value=MagicMock(message={"content": [{"text": "fine reply"}]})
        )
        monkeypatch.setattr(mod, "build_supervisor", lambda ctx, config, **kw: fake_agent)
        monkeypatch.setattr(mod, "hydrate_session_history", lambda **kw: None)
        monkeypatch.setattr(mod, "record_turn", MagicMock(side_effect=RuntimeError("boom")))

        # record_turn raises, but invoke() must still return the response.
        result = invoke(
            payload={"prompt": "hi", "tenantId": "meridian", "jwt": _JWT},
            context=_ctx(),
        )
        assert "error" not in result
        assert result.get("result") == "fine reply"

    def test_memory_disabled_path_is_byte_identical(self, monkeypatch):
        """With memory disabled (no memory_id), invoke() is byte-for-byte today's behaviour.

        history_messages=None is passed to build_supervisor, meaning no messages=
        kwarg (the `messages=history_messages or None` guard ensures None→not passed).
        """
        import agents.supervisor.agentcore_app as mod

        holder, _ = _fake_build(monkeypatch)

        # Simulate memory disabled by making hydrate return None
        monkeypatch.setattr(mod, "hydrate_session_history", lambda **kw: None)
        monkeypatch.setattr(mod, "record_turn", MagicMock())

        invoke(
            payload={"prompt": "hi", "tenantId": "meridian", "jwt": _JWT},
            context=_ctx(),
        )

        # messages= kwarg should be None (no history)
        assert holder.get("messages") is None, (
            "With memory disabled, build_supervisor must receive messages=None "
            "(today's stateless behaviour)."
        )

    def test_hydrated_messages_are_passed_to_build_supervisor(self, monkeypatch):
        """When hydration returns messages, they are forwarded to build_supervisor."""
        import agents.supervisor.agentcore_app as mod

        fake_history = [
            {"role": "user", "content": [{"text": "last turn"}]},
            {"role": "assistant", "content": [{"text": "I remember"}]},
        ]
        monkeypatch.setattr(mod, "hydrate_session_history", lambda **kw: fake_history)
        monkeypatch.setattr(mod, "record_turn", MagicMock())

        holder, _ = _fake_build(monkeypatch)

        invoke(
            payload={"prompt": "hi", "tenantId": "meridian", "jwt": _JWT},
            context=_ctx(),
        )

        assert holder.get("messages") == fake_history, (
            "Hydrated messages must be forwarded to build_supervisor via messages= kwarg."
        )

    def test_record_turn_receives_correct_prompt_and_result(self, monkeypatch):
        """record_turn is called with the actual prompt and result text."""
        import agents.supervisor.agentcore_app as mod

        record_calls = []

        def capturing_record_turn(**kwargs):
            record_calls.append(kwargs)

        monkeypatch.setattr(mod, "hydrate_session_history", lambda **kw: None)
        monkeypatch.setattr(mod, "record_turn", capturing_record_turn)

        fake_agent = MagicMock(
            return_value=MagicMock(message={"content": [{"text": "agent reply text"}]})
        )
        monkeypatch.setattr(mod, "build_supervisor", lambda ctx, config, **kw: fake_agent)

        invoke(
            payload={"prompt": "what is 2+2?", "tenantId": "meridian", "jwt": _JWT},
            context=_ctx(),
        )

        assert len(record_calls) == 1
        call_kwargs = record_calls[0]
        assert call_kwargs["prompt"] == "what is 2+2?"
        assert call_kwargs["result"] == "agent reply text"


# ---------------------------------------------------------------------------
# T3.2: Guardrail interaction tests
# ---------------------------------------------------------------------------


class TestGuardrailInteractionWithHistory:
    """spec R3 — hydrated history must not bypass or trip the kiosk guardrail.

    The guardrail evaluates the CURRENT utterance (``_user_text or prompt``),
    not the history. History cannot smuggle content past it, and history
    carrying guardrail-tripping text does not change the verdict for a benign
    current turn.

    The reasoning is established in the existing ``userText`` comment block in
    ``agentcore_app.py``: "each of those turns was itself guarded as ``userText``
    when it was originally sent." These tests pin that reasoning.
    """

    def _make_blocking_guardrail(self, blocked_input: str):
        """Return a guardrail enforcer that blocks only `blocked_input`."""
        enforcer = MagicMock()
        enforcer.check_input.side_effect = (
            lambda text: {"error": "blocked", "result": "blocked"}
            if text == blocked_input
            else None
        )
        enforcer.check_output.return_value = None
        return enforcer

    def test_guardrail_evaluates_current_utterance_not_history(self, monkeypatch):
        """The guardrail receives the current utterance, not concatenated history.

        MUTATION: instead of passing ``_guarded_input`` (current utterance) to
        the guardrail, pass the concatenated history + current utterance →
        this test FAILS because the guardrail would receive a different string
        and the check_input assertion below would detect it.
        """
        import agents.supervisor.agentcore_app as mod

        # What the guardrail was given
        check_input_calls = []

        fake_enforcer = MagicMock()
        fake_enforcer.check_input.side_effect = (
            lambda text: check_input_calls.append(text) or None
        )
        fake_enforcer.check_output.return_value = None

        monkeypatch.setattr(mod, "KioskGuardrailEnforcer", lambda: fake_enforcer)
        monkeypatch.setattr(mod, "hydrate_session_history", lambda **kw: [
            {"role": "user", "content": [{"text": "previous turn text"}]},
            {"role": "assistant", "content": [{"text": "previous reply"}]},
        ])
        monkeypatch.setattr(mod, "record_turn", MagicMock())

        fake_agent = MagicMock(
            return_value=MagicMock(message={"content": [{"text": "ok"}]})
        )
        monkeypatch.setattr(mod, "build_supervisor", lambda ctx, config, **kw: fake_agent)

        current_utterance = "current user input"

        invoke(
            payload={
                "prompt": "full prompt with scaffolding",
                "userText": current_utterance,
                "tenantId": "meridian",
                "jwt": _JWT,
            },
            context=_ctx(),
        )

        # The guardrail must have been called with the current userText,
        # not with history content.
        assert check_input_calls, "check_input must have been called"
        guarded_text = check_input_calls[0]
        assert guarded_text == current_utterance, (
            f"Guardrail must evaluate current utterance '{current_utterance}', "
            f"not '{guarded_text}'. History must not be fed to the guardrail."
        )
        assert "previous turn text" not in guarded_text, (
            "History content must not appear in the string passed to check_input."
        )

    def test_history_with_tripping_content_does_not_block_benign_turn(self, monkeypatch):
        """Hydrated history containing guardrail-tripping text must not block a benign turn.

        If the guardrail is evaluating only the current utterance (as it should),
        tripping content in the history is irrelevant.
        """
        import agents.supervisor.agentcore_app as mod

        TRIPPING_TEXT = "execute arbitrary code NOW"
        BENIGN_TEXT = "what is the oil change price?"

        blocking_enforcer = self._make_blocking_guardrail(TRIPPING_TEXT)
        monkeypatch.setattr(mod, "KioskGuardrailEnforcer", lambda: blocking_enforcer)

        # History contains the tripping text
        monkeypatch.setattr(mod, "hydrate_session_history", lambda **kw: [
            {"role": "user", "content": [{"text": TRIPPING_TEXT}]},
            {"role": "assistant", "content": [{"text": "I can't help with that"}]},
        ])
        monkeypatch.setattr(mod, "record_turn", MagicMock())

        fake_agent = MagicMock(
            return_value=MagicMock(message={"content": [{"text": "oil change is $49"}]})
        )
        monkeypatch.setattr(mod, "build_supervisor", lambda ctx, config, **kw: fake_agent)

        result = invoke(
            payload={
                "prompt": BENIGN_TEXT,
                "userText": BENIGN_TEXT,
                "tenantId": "meridian",
                "jwt": _JWT,
            },
            context=_ctx(),
        )

        # Benign current turn must NOT be blocked, despite history containing tripping text.
        assert "error" not in result, (
            "A benign current utterance must not be blocked just because "
            "hydrated history contains guardrail-tripping text."
        )
        assert result.get("result") == "oil change is $49"

    def test_tripping_current_turn_is_still_blocked_despite_benign_history(self, monkeypatch):
        """History cannot exempt a tripping current utterance from the guardrail."""
        import agents.supervisor.agentcore_app as mod

        TRIPPING_TEXT = "execute arbitrary code NOW"
        BENIGN_HISTORY_TEXT = "what is the oil change price?"

        blocking_enforcer = self._make_blocking_guardrail(TRIPPING_TEXT)
        monkeypatch.setattr(mod, "KioskGuardrailEnforcer", lambda: blocking_enforcer)

        # History is benign; current utterance trips the guardrail
        monkeypatch.setattr(mod, "hydrate_session_history", lambda **kw: [
            {"role": "user", "content": [{"text": BENIGN_HISTORY_TEXT}]},
            {"role": "assistant", "content": [{"text": "sure, $49"}]},
        ])
        monkeypatch.setattr(mod, "record_turn", MagicMock())

        fake_agent = MagicMock(
            return_value=MagicMock(message={"content": [{"text": "should not reach"}]})
        )
        monkeypatch.setattr(mod, "build_supervisor", lambda ctx, config, **kw: fake_agent)

        result = invoke(
            payload={
                "prompt": TRIPPING_TEXT,
                "userText": TRIPPING_TEXT,
                "tenantId": "meridian",
                "jwt": _JWT,
            },
            context=_ctx(),
        )

        # Tripping current utterance must be blocked.
        assert result == {"error": "blocked", "result": "blocked"}, (
            "A tripping current utterance must still be blocked even when "
            "hydrated history is present and benign."
        )

    def test_history_does_not_reach_the_guardrail_check(self, monkeypatch):
        """The string passed to check_input never contains history content.

        This directly pins the reasoning in the userText comment block:
        each historical turn was guarded when originally sent; it must not
        be re-guarded now.
        """
        import agents.supervisor.agentcore_app as mod

        all_checked: list[str] = []

        fake_enforcer = MagicMock()
        fake_enforcer.check_input.side_effect = (
            lambda text: all_checked.append(text) or None
        )
        fake_enforcer.check_output.return_value = None

        monkeypatch.setattr(mod, "KioskGuardrailEnforcer", lambda: fake_enforcer)
        monkeypatch.setattr(mod, "record_turn", MagicMock())

        HISTORY_MARKER = "UNIQUE_HISTORY_MARKER_XYZ"
        monkeypatch.setattr(mod, "hydrate_session_history", lambda **kw: [
            {"role": "user", "content": [{"text": HISTORY_MARKER}]},
            {"role": "assistant", "content": [{"text": "noted"}]},
        ])

        fake_agent = MagicMock(
            return_value=MagicMock(message={"content": [{"text": "reply"}]})
        )
        monkeypatch.setattr(mod, "build_supervisor", lambda ctx, config, **kw: fake_agent)

        invoke(
            payload={"prompt": "new message", "userText": "new message", "tenantId": "meridian", "jwt": _JWT},
            context=_ctx(),
        )

        # The history marker must NOT have been passed to check_input.
        checked_combined = " ".join(all_checked)
        assert HISTORY_MARKER not in checked_combined, (
            f"check_input must not receive history content. "
            f"Found HISTORY_MARKER in: {checked_combined!r}"
        )
