"""Bidi wiring for the AVX drill-down pre-seed (spec Fix Group 18, T18.3).

Two deliberate departures from ``test_bidi_app.py``, both to avoid shipping
vacuous tests:

1. **These tests are synchronous and drive ``_establish_session`` through
   ``asyncio.run``.**  ``pytest.ini`` sets ``asyncio_mode = auto``, but
   ``pytest-asyncio`` is **not installed** in this venv, so every
   ``@pytest.mark.asyncio`` test in ``test_bidi_app.py`` is SKIPPED — 13 of its
   14.  No CI job runs ``agents/supervisor/tests/`` either (``lint.yml`` covers
   ``tests/schemas/`` and ``agents/executors/`` only), so an async test added
   here would run nowhere.  ``asyncio.run`` needs no plugin and executes
   everywhere.
2. **They live in their own file** rather than extending ``test_bidi_app.py`` —
   zero collision surface while a concurrent session holds neighbouring files
   under ``agents/supervisor/``.

If ``pytest-asyncio`` is ever added and ``agents/supervisor/tests/`` is wired
into CI, these remain valid as-is; nothing here depends on the skip.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
from bedrock_agentcore.runtime.context import RequestContext

from agents.supervisor.avx_preseed import (
    MARKER_ROLE,
    SESSION_ID_SEPARATOR,
    SESSION_PENDING_PREFIX,
    append_pre_seed_to_prompt,
)
from agents.supervisor.bidi_app import AVX_FINDING_ID_QUERY_PARAMS, _establish_session

FINDING_ID = "FIND-11111111-2222-3333-4444-555555555555"
OWNER = "sub-aaaaaaaa-1111"
MEMORY_ID = "mem-abc123"
NOW = datetime(2026, 9, 23, 12, 0, 0, tzinfo=timezone.utc)

BASE_HEADERS = {
    "X-Amzn-Bedrock-AgentCore-Runtime-Custom-User-Token": "jwt.abc",
    "X-Amzn-Bedrock-AgentCore-Runtime-Custom-Tenant-Id": "meridian-fleet",
    "X-Amzn-Bedrock-AgentCore-Runtime-Custom-Vin": "1FMCU0F70MUA12345",
}


def _establish(headers: dict, first_message: dict | None = None):
    ctx = RequestContext(session_id="sid", request_headers=headers, request=None)
    ws = AsyncMock()
    if first_message is None:
        ws.receive_json = AsyncMock(
            side_effect=AssertionError("receive_json should not be called")
        )
    else:
        ws.receive_json = AsyncMock(return_value=first_message)
    return asyncio.run(_establish_session(ws, ctx, "sid"))


# ---------------------------------------------------------------------------
# Header extraction — all three casings, because AgentCore's camelCase
# normalization is not deterministic and a missed variant degrades silently.
# ---------------------------------------------------------------------------


def test_avx_finding_id_accepted_as_camelcase_collapsed():
    session = _establish(
        {**BASE_HEADERS, "X-Amzn-Bedrock-AgentCore-Runtime-Custom-AvxFindingId": FINDING_ID}
    )
    assert session.avx_finding_id == FINDING_ID


def test_avx_finding_id_accepted_as_hyphenated():
    session = _establish(
        {**BASE_HEADERS, "X-Amzn-Bedrock-AgentCore-Runtime-Custom-Avx-Finding-Id": FINDING_ID}
    )
    assert session.avx_finding_id == FINDING_ID


def test_avx_finding_id_accepted_as_underscored():
    session = _establish(
        {**BASE_HEADERS, "X-Amzn-Bedrock-AgentCore-Runtime-Custom-Avx_Finding_Id": FINDING_ID}
    )
    assert session.avx_finding_id == FINDING_ID


def test_all_three_declared_casings_are_lowercase_and_distinct():
    """The tuple is what the three tests above exercise; keep it honest.

    Asserted separately so shrinking the tuple fails here even if someone also
    deletes one of the three tests above.
    """
    assert len(AVX_FINDING_ID_QUERY_PARAMS) == 3
    assert len(set(AVX_FINDING_ID_QUERY_PARAMS)) == 3
    for key in AVX_FINDING_ID_QUERY_PARAMS:
        assert key == key.lower(), f"{key} is not lowercased; _normalize_headers lowercases keys"
        assert key.startswith("x-amzn-bedrock-agentcore-runtime-custom-")


def test_absent_header_leaves_avx_finding_id_none():
    session = _establish(dict(BASE_HEADERS))
    assert session.avx_finding_id is None


def test_session_start_fallback_accepts_avx_finding_id():
    """A client sending session.start instead of query params can still anchor."""
    session = _establish(
        {"X-Amzn-Bedrock-AgentCore-Runtime-Custom-User-Token": "jwt.abc"},
        first_message={
            "type": "session.start",
            "tenantId": "meridian-fleet",
            "vin": "1FMCU0F70MUA12345",
            "avxFindingId": FINDING_ID,
        },
    )
    assert session.avx_finding_id == FINDING_ID


def test_header_wins_over_session_start_value():
    session = _establish(
        {
            "X-Amzn-Bedrock-AgentCore-Runtime-Custom-User-Token": "jwt.abc",
            "X-Amzn-Bedrock-AgentCore-Runtime-Custom-AvxFindingId": FINDING_ID,
        },
        first_message={
            "type": "session.start",
            "tenantId": "meridian-fleet",
            "vin": "1FMCU0F70MUA12345",
            "avxFindingId": "FIND-should-lose",
        },
    )
    assert session.avx_finding_id == FINDING_ID


def test_a_bad_finding_id_header_does_not_prevent_session_establishment():
    """The anchor is optional context; a junk value must not fail the session."""
    session = _establish(
        {**BASE_HEADERS, "X-Amzn-Bedrock-AgentCore-Runtime-Custom-AvxFindingId": "!!! junk !!!"}
    )
    assert session.tenant_id == "meridian-fleet"
    assert session.vin == "1FMCU0F70MUA12345"


# ---------------------------------------------------------------------------
# Prompt composition — the append property, tested directly.
# ---------------------------------------------------------------------------


class _FakeMemoryClient:
    def __init__(self) -> None:
        self._store: dict[tuple[str, str], list[list[dict]]] = {}

    def seed_marker(self, actor_id: str, finding_id: str, expires_at: datetime) -> None:
        body = {
            "marker_kind": "drill_down_pre_seed",
            "finding_id": finding_id,
            "action_id": "ACT-1",
            "written_at": NOW.isoformat().replace("+00:00", "Z"),
            "expires_at": expires_at.replace(microsecond=0)
            .isoformat()
            .replace("+00:00", "Z"),
            "evidence": ["Left front tire at 28 PSI"],
        }
        key = (actor_id, f"{SESSION_PENDING_PREFIX}{SESSION_ID_SEPARATOR}{finding_id}")
        self._store.setdefault(key, []).insert(
            0, [{"role": MARKER_ROLE, "content": {"text": json.dumps(body)}}]
        )

    def get_last_k_turns(self, memory_id, actor_id, session_id, k=10):
        return self._store.get((actor_id, session_id), [])[:k]


def _jwt(sub: str) -> str:
    import base64

    payload = base64.b64encode(json.dumps({"sub": sub}).encode()).decode().rstrip("=")
    return f"h.{payload}.s"


BASE_PROMPT = "You are a vehicle assistant."


def test_valid_marker_appends_fragment_and_preserves_the_original_prompt():
    client = _FakeMemoryClient()
    client.seed_marker(OWNER, FINDING_ID, NOW + timedelta(hours=1))

    result = append_pre_seed_to_prompt(
        BASE_PROMPT,
        finding_id=FINDING_ID,
        jwt=_jwt(OWNER),
        memory_id=MEMORY_ID,
        client=client,
        now=NOW,
    )

    assert result.startswith(BASE_PROMPT), "the persona prompt must not be replaced"
    assert len(result) > len(BASE_PROMPT)
    assert FINDING_ID in result


@pytest.mark.parametrize(
    "label,finding_id,jwt,memory_id,seed",
    [
        ("absent header", None, _jwt(OWNER), MEMORY_ID, False),
        ("empty header", "", _jwt(OWNER), MEMORY_ID, False),
        ("no marker written", FINDING_ID, _jwt(OWNER), MEMORY_ID, False),
        ("another owner's marker", FINDING_ID, _jwt("sub-someone-else"), MEMORY_ID, True),
        ("memory disabled", FINDING_ID, _jwt(OWNER), "", True),
        ("unusable jwt", FINDING_ID, "not-a-jwt", MEMORY_ID, True),
        ("no jwt at all", FINDING_ID, None, MEMORY_ID, True),
    ],
)
def test_prompt_returned_unchanged_for_every_cold_open_case(
    label, finding_id, jwt, memory_id, seed
):
    client = _FakeMemoryClient()
    if seed:
        client.seed_marker(OWNER, FINDING_ID, NOW + timedelta(hours=1))

    result = append_pre_seed_to_prompt(
        BASE_PROMPT,
        finding_id=finding_id,
        jwt=jwt,
        memory_id=memory_id,
        client=client,
        now=NOW,
    )

    assert result == BASE_PROMPT, f"{label} should cold-open, leaving the prompt untouched"


def test_expired_marker_leaves_the_prompt_unchanged():
    client = _FakeMemoryClient()
    client.seed_marker(OWNER, FINDING_ID, NOW - timedelta(seconds=1))

    result = append_pre_seed_to_prompt(
        BASE_PROMPT,
        finding_id=FINDING_ID,
        jwt=_jwt(OWNER),
        memory_id=MEMORY_ID,
        client=client,
        now=NOW,
    )

    assert result == BASE_PROMPT


def test_append_never_raises_when_the_memory_client_explodes():
    class _Exploding:
        def get_last_k_turns(self, *_a, **_k):
            raise RuntimeError("memory down")

    result = append_pre_seed_to_prompt(
        BASE_PROMPT,
        finding_id=FINDING_ID,
        jwt=_jwt(OWNER),
        memory_id=MEMORY_ID,
        client=_Exploding(),
        now=NOW,
    )

    assert result == BASE_PROMPT


def test_bidi_app_does_not_pass_messages_to_bidi_agent():
    """Structural guard for constraint 1 of Fix Group 18.

    ``bidi_app.py``'s Nova Sonic patch states its history path "skips the
    50KB/200KB truncation and user-first enforcement because our bidi_app never
    passes ``messages=``".  Pre-seeding via message history would activate that
    path.  Asserted against the source because the construction site is inside
    an async WebSocket handler no test in this venv can execute.

    Hardened after review Cycle 1: the first version used a single
    ``re.search``, so a SECOND ``BidiAgent(...)`` construction elsewhere in the
    file would have been unguarded while the guard kept passing.  This version
    parses the AST, checks **every** construction site, and pins the count so a
    new one fails loudly rather than silently escaping.  It also rejects
    ``**kwargs`` splats and post-hoc ``agent.messages = ...`` assignment, which a
    substring test cannot see.
    """
    import ast
    import pathlib

    path = pathlib.Path(__file__).resolve().parents[1] / "bidi_app.py"
    tree = ast.parse(path.read_text())

    sites = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "BidiAgent"
    ]
    assert sites, "no BidiAgent(...) construction found — update this guard"
    assert len(sites) == 1, (
        f"expected exactly 1 BidiAgent construction, found {len(sites)}. A new "
        "one must be audited for messages= before this count is raised."
    )

    for site in sites:
        kwargs = {kw.arg for kw in site.keywords}
        assert "messages" not in kwargs, (
            "bidi_app now passes messages= to BidiAgent; the patched Nova Sonic "
            "history path drops truncation and user-first enforcement, so this "
            "needs the Strands pin bumped (PR #2188) before it is safe"
        )
        # `kw.arg is None` is a **kwargs splat, whose contents are invisible here.
        assert None not in {kw.arg for kw in site.keywords}, (
            "BidiAgent is constructed with a **kwargs splat, which could carry "
            "messages= past this guard — pass keywords explicitly"
        )

    # Post-hoc assignment would bypass the constructor entirely.
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Attribute) and target.attr == "messages":
                    raise AssertionError(
                        f"bidi_app.py:{node.lineno} assigns .messages post-hoc, "
                        "which bypasses the constructor guard"
                    )
