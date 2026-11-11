"""Tests for the AVX drill-down pre-seed reader (spec Fix Group 18, T18.1 + T18.2).

Every test here stubs the AgentCore Memory client.  Per
``~/.kiro/steering/spec-workflow.md`` § "Live-Verification Gate" a stub cannot
fail the way a service fails, so T18.5 owns the live gate and this group does
not close on these tests alone.

The marker shape asserted here is the contract written by
``agents/executors/chat.py``.  A change on either side must change both — the
fixtures below are built from that writer's documented payload, not invented.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from agents.supervisor import avx_preseed
from agents.supervisor.avx_preseed import (
    AGENTCORE_SESSION_ID_RE,
    MARKER_KIND,
    MARKER_ROLE,
    SESSION_ID_SEPARATOR,
    SESSION_PENDING_PREFIX,
    actor_id_from_jwt,
    resolve_pre_seed_fragment,
)

FINDING_ID = "FIND-11111111-2222-3333-4444-555555555555"
OWNER_A = "sub-aaaaaaaa-1111"
OWNER_B = "sub-bbbbbbbb-2222"
MEMORY_ID = "mem-abc123"

NOW = datetime(2026, 9, 23, 12, 0, 0, tzinfo=timezone.utc)


def _iso(dt: datetime) -> str:
    """Render as the writer does: second precision, trailing ``Z``."""
    return dt.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _marker_body(
    *,
    finding_id: str = FINDING_ID,
    expires_at: datetime | None = None,
    evidence: list | None = None,
    marker_kind: str = MARKER_KIND,
) -> dict:
    return {
        "marker_kind": marker_kind,
        "finding_id": finding_id,
        "action_id": "ACT-99999999",
        "written_at": _iso(NOW - timedelta(hours=1)),
        "expires_at": _iso(expires_at if expires_at else NOW + timedelta(hours=71)),
        "evidence": evidence if evidence is not None else ["Left front tire at 28 PSI"],
    }


class _FakeMemoryClient:
    """Minimal stand-in for MemoryClient.

    Stores events keyed by ``(actor_id, session_id)`` so a cross-owner read is
    exercised against real scoping behaviour rather than a mocked return value.
    ``get_last_k_turns`` mirrors the real contract: ``List[List[Dict]]``, outer
    list NEWEST-FIRST, inner messages ``{"role": ..., "content": {"text": ...}}``.
    """

    def __init__(self) -> None:
        self._store: dict[tuple[str, str], list[list[dict]]] = {}
        self.calls: list[tuple] = []

    def seed(self, actor_id: str, session_id: str, role: str, text: str) -> None:
        self._store.setdefault((actor_id, session_id), []).insert(
            0, [{"role": role, "content": {"text": text}}]
        )

    def seed_marker(self, actor_id: str, body: dict) -> None:
        session_id = f"{SESSION_PENDING_PREFIX}{SESSION_ID_SEPARATOR}{body['finding_id']}"
        self.seed(actor_id, session_id, MARKER_ROLE, json.dumps(body))

    def get_last_k_turns(self, memory_id, actor_id, session_id, k=10):
        self.calls.append((memory_id, actor_id, session_id, k))
        return self._store.get((actor_id, session_id), [])[:k]


@pytest.fixture()
def client() -> _FakeMemoryClient:
    return _FakeMemoryClient()


# ---------------------------------------------------------------------------
# T18.1 — happy path
# ---------------------------------------------------------------------------


def test_valid_marker_yields_fragment_naming_the_finding(client):
    client.seed_marker(OWNER_A, _marker_body())

    fragment = resolve_pre_seed_fragment(
        finding_id=FINDING_ID,
        actor_id=OWNER_A,
        memory_id=MEMORY_ID,
        client=client,
        now=NOW,
    )

    assert fragment is not None
    assert FINDING_ID in fragment
    assert "Left front tire at 28 PSI" in fragment


def test_reader_queries_the_pending_session_id_under_the_callers_own_actor(client):
    """The scope actually read is (caller's actor, session_pending:<finding>)."""
    client.seed_marker(OWNER_A, _marker_body())

    resolve_pre_seed_fragment(
        finding_id=FINDING_ID,
        actor_id=OWNER_A,
        memory_id=MEMORY_ID,
        client=client,
        now=NOW,
    )

    assert client.calls, "reader never called the memory client"
    memory_id, actor_id, session_id, _k = client.calls[0]
    assert memory_id == MEMORY_ID
    assert actor_id == OWNER_A
    assert session_id == f"{SESSION_PENDING_PREFIX}{SESSION_ID_SEPARATOR}{FINDING_ID}"


def test_fragment_states_the_context_was_not_spoken_by_the_owner(client):
    """Context, not content — the model must not attribute it to the owner."""
    client.seed_marker(OWNER_A, _marker_body())

    fragment = resolve_pre_seed_fragment(
        finding_id=FINDING_ID,
        actor_id=OWNER_A,
        memory_id=MEMORY_ID,
        client=client,
        now=NOW,
    )

    assert "not spoken by the owner" in fragment
    assert "do not claim the owner said it" in fragment.lower()


# ---------------------------------------------------------------------------
# T18.1 — expiry. Mutation (a) deletes the comparison; mutation (b) relaxes
# `<=` to `<`, which only the boundary test catches.
# ---------------------------------------------------------------------------


def test_expired_marker_is_rejected(client):
    client.seed_marker(
        OWNER_A, _marker_body(expires_at=NOW - timedelta(seconds=1))
    )

    assert (
        resolve_pre_seed_fragment(
            finding_id=FINDING_ID,
            actor_id=OWNER_A,
            memory_id=MEMORY_ID,
            client=client,
            now=NOW,
        )
        is None
    )


@pytest.mark.parametrize("spelling", ["Z", "offset"])
def test_marker_expiring_exactly_now_is_rejected(client, spelling):
    """`expires_at == now` is expired.

    Pins two mutations at once:

    * relaxing ``expires_at <= now`` to ``< now`` — passes
      ``test_expired_marker_is_rejected`` and fails only here;
    * comparing ``expires_at`` as an ISO **string** instead of a parsed
      datetime — ``"...Z"`` sorts ABOVE ``"...+00:00"`` (``ord('Z') > ord('+')``)
      so a string compare silently honours an already-expired ``Z``-spelled
      marker.  Parametrising the boundary over both spellings is what catches
      it; a future-dated marker satisfies a string compare either way.
    """
    body = _marker_body(expires_at=NOW)
    if spelling == "offset":
        body["expires_at"] = NOW.replace(microsecond=0).isoformat()  # ...+00:00
    client.seed_marker(OWNER_A, body)

    assert (
        resolve_pre_seed_fragment(
            finding_id=FINDING_ID,
            actor_id=OWNER_A,
            memory_id=MEMORY_ID,
            client=client,
            now=NOW,
        )
        is None
    )


def test_marker_one_second_from_expiry_is_still_honoured(client):
    """Positive control for the two tests above — the guard is not just 'always None'."""
    client.seed_marker(
        OWNER_A, _marker_body(expires_at=NOW + timedelta(seconds=1))
    )

    assert (
        resolve_pre_seed_fragment(
            finding_id=FINDING_ID,
            actor_id=OWNER_A,
            memory_id=MEMORY_ID,
            client=client,
            now=NOW,
        )
        is not None
    )


@pytest.mark.parametrize("bad", ["", "not-a-timestamp", None, 12345, "2026-13-45T99:99:99Z"])
def test_marker_with_missing_or_unparseable_expires_at_is_rejected(client, bad):
    body = _marker_body()
    body["expires_at"] = bad
    client.seed_marker(OWNER_A, body)

    assert (
        resolve_pre_seed_fragment(
            finding_id=FINDING_ID,
            actor_id=OWNER_A,
            memory_id=MEMORY_ID,
            client=client,
            now=NOW,
        )
        is None
    )


def test_both_expires_at_spellings_are_accepted_when_in_the_future(client):
    """Positive control for the two spellings — neither is rejected outright.

    This does NOT pin the string-comparison defect: a lexical compare honours a
    future-dated marker in both spellings too.  The discriminating case is the
    exact boundary, owned by
    ``test_marker_expiring_exactly_now_is_rejected[Z|offset]``.
    """
    expires = NOW + timedelta(hours=1)
    z_form = _iso(expires)
    offset_form = expires.replace(microsecond=0).isoformat()  # ...+00:00
    assert z_form != offset_form  # the two spellings really do differ

    for spelling in (z_form, offset_form):
        c = _FakeMemoryClient()
        body = _marker_body()
        body["expires_at"] = spelling
        c.seed_marker(OWNER_A, body)
        assert (
            resolve_pre_seed_fragment(
                finding_id=FINDING_ID,
                actor_id=OWNER_A,
                memory_id=MEMORY_ID,
                client=c,
                now=NOW,
            )
            is not None
        ), f"{spelling} was rejected — expiry is being compared as a string"


# ---------------------------------------------------------------------------
# T18.1 — marker selection. Mutation (c) drops the marker_kind filter.
# ---------------------------------------------------------------------------


def test_non_marker_tool_message_is_not_treated_as_a_marker(client):
    """Pins the `marker_kind` filter.

    The body is **otherwise entirely valid** — real ``finding_id``, real
    unexpired ``expires_at``, real ``evidence`` — so ``marker_kind`` is the only
    thing that disqualifies it.  An earlier version of this test omitted
    ``expires_at``, which meant the expiry guard rejected it and the test passed
    while the ``marker_kind`` filter was deleted: a false CAUGHT that the
    mutation battery surfaced.
    """
    session_id = f"{SESSION_PENDING_PREFIX}{SESSION_ID_SEPARATOR}{FINDING_ID}"
    body = _marker_body(marker_kind="some_other_marker")
    client.seed(OWNER_A, session_id, MARKER_ROLE, json.dumps(body))

    assert (
        resolve_pre_seed_fragment(
            finding_id=FINDING_ID,
            actor_id=OWNER_A,
            memory_id=MEMORY_ID,
            client=client,
            now=NOW,
        )
        is None
    )


@pytest.mark.parametrize("role", ["USER", "ASSISTANT", "OTHER"])
def test_valid_marker_body_under_a_non_tool_role_is_ignored(client, role):
    """Pins the role filter, which nothing else did.

    The body here is a fully valid marker; only the role is wrong.  The
    non-JSON test below cannot pin this, because its payload fails JSON parsing
    whether or not the role is checked.
    """
    session_id = f"{SESSION_PENDING_PREFIX}{SESSION_ID_SEPARATOR}{FINDING_ID}"
    client.seed(OWNER_A, session_id, role, json.dumps(_marker_body()))

    assert (
        resolve_pre_seed_fragment(
            finding_id=FINDING_ID,
            actor_id=OWNER_A,
            memory_id=MEMORY_ID,
            client=client,
            now=NOW,
        )
        is None
    )


def test_non_json_tool_message_does_not_raise_and_yields_none(client):
    session_id = f"{SESSION_PENDING_PREFIX}{SESSION_ID_SEPARATOR}{FINDING_ID}"
    client.seed(OWNER_A, session_id, MARKER_ROLE, "this is not json {{{")

    assert (
        resolve_pre_seed_fragment(
            finding_id=FINDING_ID,
            actor_id=OWNER_A,
            memory_id=MEMORY_ID,
            client=client,
            now=NOW,
        )
        is None
    )


def test_marker_whose_finding_id_disagrees_with_the_header_is_rejected(client):
    """Grounding the session in the wrong Finding is worse than cold-opening."""
    session_id = f"{SESSION_PENDING_PREFIX}{SESSION_ID_SEPARATOR}{FINDING_ID}"
    body = _marker_body(finding_id="FIND-a-totally-different-id")
    client.seed(OWNER_A, session_id, MARKER_ROLE, json.dumps(body))

    assert (
        resolve_pre_seed_fragment(
            finding_id=FINDING_ID,
            actor_id=OWNER_A,
            memory_id=MEMORY_ID,
            client=client,
            now=NOW,
        )
        is None
    )


def test_newest_marker_wins_when_a_finding_was_drilled_into_twice(client):
    old = _marker_body(evidence=["OLD evidence line"])
    new = _marker_body(evidence=["NEW evidence line"])
    client.seed_marker(OWNER_A, old)
    client.seed_marker(OWNER_A, new)  # inserted at position 0 = newest

    fragment = resolve_pre_seed_fragment(
        finding_id=FINDING_ID,
        actor_id=OWNER_A,
        memory_id=MEMORY_ID,
        client=client,
        now=NOW,
    )

    assert "NEW evidence line" in fragment
    assert "OLD evidence line" not in fragment


# ---------------------------------------------------------------------------
# T18.2 — scope. Mutation: take actor_id from the marker body or the header.
# ---------------------------------------------------------------------------


def test_another_owners_marker_is_not_readable(client):
    """Owner A's marker must not resolve for owner B.

    Pins the T18.2 mutation: sourcing `actor_id` from the marker body or the
    header instead of the caller's claims makes this pass.
    """
    client.seed_marker(OWNER_A, _marker_body())

    assert (
        resolve_pre_seed_fragment(
            finding_id=FINDING_ID,
            actor_id=OWNER_B,
            memory_id=MEMORY_ID,
            client=client,
            now=NOW,
        )
        is None
    )


def test_cross_owner_miss_is_indistinguishable_from_absent_and_never_raises(client):
    client.seed_marker(OWNER_A, _marker_body())

    for actor in (OWNER_B, "sub-unknown"):
        assert (
            resolve_pre_seed_fragment(
                finding_id=FINDING_ID,
                actor_id=actor,
                memory_id=MEMORY_ID,
                client=client,
                now=NOW,
            )
            is None
        )


# ---------------------------------------------------------------------------
# T18.1 — cold-open policy. Mutation (d) lets the exception propagate.
# ---------------------------------------------------------------------------


def test_memory_read_raising_yields_none_rather_than_propagating(monkeypatch, client):
    """Pins mutation (d).

    ``load_recent_turns`` is documented never to raise; this asserts the reader
    survives that contract being broken, because it runs on the
    session-establish path.
    """

    def _boom(*_args, **_kwargs):
        raise RuntimeError("memory service exploded")

    monkeypatch.setattr(avx_preseed, "load_recent_turns", _boom)

    assert (
        resolve_pre_seed_fragment(
            finding_id=FINDING_ID,
            actor_id=OWNER_A,
            memory_id=MEMORY_ID,
            client=client,
            now=NOW,
        )
        is None
    )


@pytest.mark.parametrize(
    "kwargs",
    [
        {"finding_id": None},
        {"finding_id": ""},
        {"finding_id": "   "},
        {"actor_id": None},
        {"actor_id": ""},
        {"memory_id": None},
        {"memory_id": ""},
    ],
)
def test_missing_inputs_cold_open(client, kwargs):
    base = {
        "finding_id": FINDING_ID,
        "actor_id": OWNER_A,
        "memory_id": MEMORY_ID,
        "client": client,
        "now": NOW,
    }
    base.update(kwargs)
    assert resolve_pre_seed_fragment(**base) is None


def test_absent_marker_cold_opens(client):
    assert (
        resolve_pre_seed_fragment(
            finding_id=FINDING_ID,
            actor_id=OWNER_A,
            memory_id=MEMORY_ID,
            client=client,
            now=NOW,
        )
        is None
    )


# ---------------------------------------------------------------------------
# Fragment bounding — a large evidence[] must not crowd out the persona prompt.
# ---------------------------------------------------------------------------


def test_evidence_lines_are_capped(client):
    client.seed_marker(
        OWNER_A,
        _marker_body(evidence=[f"evidence line {i}" for i in range(50)]),
    )

    fragment = resolve_pre_seed_fragment(
        finding_id=FINDING_ID,
        actor_id=OWNER_A,
        memory_id=MEMORY_ID,
        client=client,
        now=NOW,
    )

    rendered = [ln for ln in fragment.splitlines() if ln.startswith("- ")]
    assert len(rendered) == avx_preseed._MAX_EVIDENCE_LINES


def test_long_evidence_line_is_truncated(client):
    client.seed_marker(OWNER_A, _marker_body(evidence=["x" * 5000]))

    fragment = resolve_pre_seed_fragment(
        finding_id=FINDING_ID,
        actor_id=OWNER_A,
        memory_id=MEMORY_ID,
        client=client,
        now=NOW,
    )

    longest = max(
        (ln for ln in fragment.splitlines() if ln.startswith("- ")), key=len
    )
    # "- " prefix plus the cap.
    assert len(longest) <= avx_preseed._MAX_EVIDENCE_CHARS + 2


def test_structured_evidence_entries_render_without_raw_json_when_summarisable(client):
    client.seed_marker(
        OWNER_A,
        _marker_body(evidence=[{"summary": "Tread depth 3mm", "vin": "SHOULDNOTAPPEAR"}]),
    )

    fragment = resolve_pre_seed_fragment(
        finding_id=FINDING_ID,
        actor_id=OWNER_A,
        memory_id=MEMORY_ID,
        client=client,
        now=NOW,
    )

    assert "Tread depth 3mm" in fragment
    assert "SHOULDNOTAPPEAR" not in fragment


def test_evidence_not_a_list_is_tolerated(client):
    body = _marker_body()
    body["evidence"] = {"not": "a list"}
    client.seed_marker(OWNER_A, body)

    fragment = resolve_pre_seed_fragment(
        finding_id=FINDING_ID,
        actor_id=OWNER_A,
        memory_id=MEMORY_ID,
        client=client,
        now=NOW,
    )

    assert fragment is not None
    assert FINDING_ID in fragment


# ---------------------------------------------------------------------------
# actor_id_from_jwt
# ---------------------------------------------------------------------------


def _make_jwt(claims: dict) -> str:
    import base64

    payload = base64.b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"header.{payload}.signature"


def test_actor_id_from_jwt_reads_sub():
    assert actor_id_from_jwt(_make_jwt({"sub": OWNER_A})) == OWNER_A


@pytest.mark.parametrize(
    "bad",
    [None, "", "not-a-jwt", "only.two", "a.b.c.d", "header.!!!notbase64!!!.sig"],
)
def test_actor_id_from_jwt_returns_none_for_unusable_tokens(bad):
    assert actor_id_from_jwt(bad) is None


@pytest.mark.parametrize("claims", [{}, {"sub": ""}, {"sub": "   "}, {"sub": 123}])
def test_actor_id_from_jwt_returns_none_when_sub_is_unusable(claims):
    assert actor_id_from_jwt(_make_jwt(claims)) is None


def test_actor_id_from_jwt_never_falls_back_to_another_identifier():
    """A fallback would collapse every caller into one shared memory scope."""
    jwt = _make_jwt({"session_id": "sess-1", "tenant_id": "t", "email": "a@b.c"})
    assert actor_id_from_jwt(jwt) is None


def test_actor_id_from_jwt_payload_not_an_object_returns_none():
    import base64

    payload = base64.b64encode(json.dumps(["not", "a", "dict"]).encode()).decode().rstrip("=")
    assert actor_id_from_jwt(f"h.{payload}.s") is None



# ---------------------------------------------------------------------------
# Security review Cycle 1 — W2 (no exception detail in logs) and S2 (bound the
# attacker-supplied finding_id before it becomes a Memory session_id).
# ---------------------------------------------------------------------------


def test_memory_read_failure_logs_exception_type_only_never_the_exception(
    monkeypatch, caplog
):
    """W2: a broken `load_recent_turns` contract must not echo request content.

    `actor_id` is passed into that call, so a traceback or `str(exc)` in the log
    could carry it, or any request content the service echoes back. The peer's
    `session_memory.py:415` carries an explicit comment against this exact hazard
    and logs `type(exc).__name__` only.

    **Must monkeypatch `load_recent_turns` itself, not pass a raising client.**
    `load_recent_turns` is documented never to raise and absorbs client
    exceptions internally, so a raising client never reaches this module's
    belt-and-braces `except` — it takes the empty-result path instead. A first
    version of this test used a raising client and PASSED against the vulnerable
    `exc_info=True` code, because the `RuntimeError` it matched was
    `session_memory`'s own log line, not this module's. The mutation battery
    caught that; it is the exact "test asserts something adjacent to the
    property" shape this spec keeps hitting.
    """
    import logging as _logging

    secret_actor = "sub-SHOULD-NOT-APPEAR-IN-LOGS"

    def _boom(*_args, **_kwargs):
        raise RuntimeError(
            f"service echoed back actor={secret_actor} vin=1FMCU0F70MUA12345"
        )

    monkeypatch.setattr(avx_preseed, "load_recent_turns", _boom)

    with caplog.at_level(_logging.WARNING, logger="agents.supervisor.avx_preseed"):
        result = resolve_pre_seed_fragment(
            finding_id=FINDING_ID,
            actor_id=secret_actor,
            memory_id=MEMORY_ID,
            client=None,
            now=NOW,
        )

    assert result is None

    records = [r for r in caplog.records if "memory read raised" in r.getMessage()]
    assert records, "this module's cold-open warning was not emitted at all"

    # STRUCTURAL assertion, and the load-bearing one. `caplog.text` renders
    # through pytest's formatter, which does NOT necessarily expand `exc_info`.
    # `record.exc_info` is the property itself: attaching the exception object is
    # what puts it within reach of any downstream handler, whatever this
    # formatter happens to print.
    for record in records:
        assert record.exc_info is None, (
            "the log record carries an exception object; a CloudWatch-side "
            "formatter would expand it, and `actor_id` is passed to the call "
            "that raised. Log `type(exc).__name__` instead."
        )
        assert record.exc_text is None, "pre-rendered exception text is attached"

    # The exception TYPE must still be available for triage.
    assert any("RuntimeError" in r.getMessage() for r in records), (
        "the exception type should be logged so a real outage is diagnosable"
    )

    # Belt and braces on the rendered form too.
    blob = caplog.text
    assert secret_actor not in blob, "actor_id leaked into the rendered log"
    assert "1FMCU0F70MUA12345" not in blob, "a service-echoed VIN leaked into the rendered log"


def test_absurdly_long_finding_id_is_rejected_before_any_memory_call():
    """S2: bound the header before it becomes part of a Memory session_id.

    Asserts the client is never called, so the rejection happens before the
    network boundary rather than being delegated to boto3's shape validation.
    """

    class _Recording:
        def __init__(self) -> None:
            self.calls = 0

        def get_last_k_turns(self, *_a, **_k):
            self.calls += 1
            return []

    client = _Recording()
    result = resolve_pre_seed_fragment(
        finding_id="F" * (avx_preseed._MAX_FINDING_ID_CHARS + 1),
        actor_id=OWNER_A,
        memory_id=MEMORY_ID,
        client=client,
        now=NOW,
    )

    assert result is None
    assert client.calls == 0, "the over-length id reached the memory client"


def test_finding_id_at_the_length_cap_is_still_accepted():
    """Positive control for the cap — it must bound, not break normal ids."""
    at_cap = "F" * avx_preseed._MAX_FINDING_ID_CHARS
    client = _FakeMemoryClient()
    client.seed_marker(OWNER_A, _marker_body(finding_id=at_cap))

    assert (
        resolve_pre_seed_fragment(
            finding_id=at_cap,
            actor_id=OWNER_A,
            memory_id=MEMORY_ID,
            client=client,
            now=NOW,
        )
        is not None
    )
