"""Session memory helpers: write and hydrate conversation turns via AgentCore Memory.

Spec: `.kiro/specs/2026-09-22-cvx-text-session-memory/spec.md` — tasks T2.1, T2.2.

Public API
----------
``record_turn(client, memory_id, actor_id, session_id, prompt, result)``
    Persist one completed turn to AgentCore Memory.  Cannot raise — any
    exception is logged and swallowed (spec D4).  No-ops when memory is
    disabled or *actor_id* is absent.

    .. warning::
        ``messages`` argument order is ``(text, role)`` — this matches the
        AgentCore ``create_event`` API.  **``load_recent_turns`` returns
        ``(role, text)`` — the two are intentionally reversed.**  See the
        tuple-order note below.

``load_recent_turns(client, memory_id, actor_id, session_id, *, k=DEFAULT_K)``
    Return the last *k* conversation turns as :class:`MemoryTurn` named-tuples,
    oldest-first.  Each has ``.role`` and ``.text`` fields so the order cannot
    be confused at the call site.  Returns ``[]`` on any failure or when memory
    is disabled.  Never raises.

    .. warning::
        The return type carries ``(role, text)``, which is the **opposite order**
        from ``record_turn``'s ``(text, role)`` write-path.  Use attribute access
        (``turn.role``, ``turn.text``) rather than positional unpacking to make
        the difference impossible to miss.  If you must unpack positionally, note
        ``role, text = turn`` — role first.

Tuple-order asymmetry
---------------------
``record_turn`` **writes** ``(text, role)`` because that is the AgentCore
``create_event`` API contract.  ``load_recent_turns`` **returns** ``(role,
text)`` — wrapped in :class:`MemoryTurn` — because that is the conventional
order for prompt-building (role first, then content).  **The two are
intentionally different.**  T3.1 consumes both in the same function; using the
named-tuple fields prevents a mix-up from being silent.

Design notes
------------
*  Both functions accept an explicit ``actor_id`` parameter and no-op when it is
   absent.  They do NOT fall back to ``session_id`` or any other identifier —
   spec D1 forbids that pattern.  ``session.py:258-263`` shows the forbidden
   fallback; do not copy it.

*  ``MemoryClient`` is lazy-imported (preview package) following the same
   pattern as ``agents/executors/chat.py::_get_memory_client``.  Pass a stub
   via ``client`` in tests; never hit the network in unit tests.

*  No VINs, turn payloads, or ``actor_id`` values are logged.  Session ids and
   counts only (spec constraint; same rule as ``executors/chat.py``, which logs
   ``finding_id``/``session_id`` but omits ``owner_id``).  Log *whether* an
   actor was present (bool), never *which* actor.  The route Lambda already
   logs ``sub=`` in its own group (``lambdas/api-assistant-chat/handler.py:349``)
   if a specific actor must be recovered from logs.

*  **Isolation assumption, stated because nothing here enforces it** (security
   review Cycle 3 S2).  The memory key is ``(actor_id, session_id)`` and does NOT
   include ``is_kiosk``.  Separation between a principal's kiosk turns and their
   non-kiosk turns therefore rests entirely on kiosk tokens carrying a *different*
   Cognito ``sub`` than that principal's ordinary tokens — which is how kiosk
   identities are issued today (a device or shared kiosk-user identity), not
   something this module verifies.  If a kiosk session ever authenticates as the
   same ``sub`` as a signed-in owner, the two histories would share a namespace
   and ``is_kiosk`` would need to enter the key.  The anonymous-kiosk path is
   separately guarded upstream: ``session.py``'s kiosk branch returns ``None``
   before any LTM contact.

*  **Do not switch to a locals-including log formatter** without revisiting this
   module (security review Cycle 3 S3).  ``log.exception``/``log.warning`` here are
   safe under Python's default formatter, which does not emit frame locals — but
   the frames at those call sites hold hydrated turn text, so a formatter that
   included locals would begin shipping owner utterances (and VINs) to CloudWatch
   with no other code change.

*  ``load_recent_turns`` is structured so the future Tier 2 session-init reader
   (spec D7) can be added as a separate helper beside it.  The signature is
   deliberately self-contained (no global state) so both can be called from
   the same init path without coupling.

*  When ``load_recent_turns`` encounters a shape/contract failure
   (``KeyError``/``TypeError``/``IndexError`` reading ``msg["role"]`` or
   ``msg["content"]["text"]``), it logs at ERROR level with a message naming the
   contract as suspect.  A client/service failure (any other exception) is
   logged at WARNING.  Both return ``[]`` — spec D4 is unchanged.  A follow-up
   should promote the contract-failure case to a CloudWatch metric or EMF field
   so operators have an unambiguous signal without grepping logs.

AgentCore Memory API contract (verified live 2026-09-22 — see docs/tech.md)
----------------------------------------------------------------------------
``create_event(memory_id, actor_id, session_id, messages, ...)``
    ``messages`` is ``List[Tuple[str, str]]`` where each tuple is ``(text, role)``.
    Role must be one of ``USER|ASSISTANT|TOOL|OTHER`` (UPPERCASE).
    Wrong order raises ``ValueError`` — there is no silent corruption.

``get_last_k_turns(memory_id, actor_id, session_id, k=5, ...)``
    Returns ``List[List[Dict]]``.  **Ordering is MIXED**:
      - outer list (turns): NEWEST-FIRST
      - inner list (messages within a turn): CHRONOLOGICAL
    Hydration must reverse the outer list only; reversing a flattened list
    puts ASSISTANT before USER inside every turn.
    Text is at ``msg["content"]["text"]``, NOT flat ``msg["text"]``.
    Role is at ``msg["role"]``, value UPPERCASE.
    A session with zero events returns ``[]`` — no raise, not ``None``.
"""

from __future__ import annotations

import logging
import os
from typing import Any, List, NamedTuple, Optional, Tuple

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Default number of turns to hydrate.  Configurable via VSA_MEMORY_HYDRATE_TURNS.
DEFAULT_K: int = 10

#: Upper bound on the number of turns to hydrate.  Prior turns are prepended to
#: the supervisor's prompt on every invocation, so unbounded K is a slow latency
#: regression (spec D3 / R4 prompt-token growth).  100 turns × ~200 tokens each
#: = ~20k tokens added to every prompt — the cap prevents runaway cost.
_MAX_HYDRATE_K: int = 100

#: Role constants — UPPERCASE as required by the AgentCore Memory API.
_ROLE_USER = "USER"
_ROLE_ASSISTANT = "ASSISTANT"


# ---------------------------------------------------------------------------
# Public return type for load_recent_turns (F3.6)
# ---------------------------------------------------------------------------


class MemoryTurn(NamedTuple):
    """A single hydrated conversation message.

    Fields
    ------
    role : str
        UPPERCASE role string — ``'USER'``, ``'ASSISTANT'``, ``'TOOL'``, or
        ``'OTHER'``.  Role comes **first** in the named-tuple layout.
    text : str
        The message content.

    .. note::
        ``load_recent_turns`` returns ``(role, text)`` **named-tuples**.
        ``record_turn`` **writes** ``(text, role)`` — the orders are reversed
        because the AgentCore write API takes ``(text, role)`` while the
        conventional prompt-building order is ``(role, content)``.  Use
        ``.role`` / ``.text`` attribute access at call sites to make the
        difference impossible to miss.  Positional unpacking still works
        (``NamedTuple`` is a ``tuple`` subclass) — just remember ``role`` is
        index 0.
    """

    role: str
    text: str


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _get_memory_client(region: str) -> Any:
    """Create a MemoryClient for *region*.

    Lazy-import: ``bedrock_agentcore`` is a preview package.  Tests inject a
    stub via the public-function ``client`` parameter; this function is only
    called when the ``client`` argument is ``None`` at runtime.
    """
    from bedrock_agentcore.memory import MemoryClient  # noqa: PLC0415

    return MemoryClient(region_name=region)


def _hydrate_k(k: Optional[int] = None) -> int:
    """Return the effective hydration turn limit, bounded by ``_MAX_HYDRATE_K``.

    Resolution order:
      1. Explicit ``k`` argument (if provided and positive).
      2. ``VSA_MEMORY_HYDRATE_TURNS`` environment variable.
      3. ``DEFAULT_K``.

    In all cases the result is clamped to ``[1, _MAX_HYDRATE_K]``.  This
    prevents unbounded prompt-token growth (spec D3 / R4): prior turns are
    prepended to every prompt and a very large ``k`` is a slow latency
    regression, not just a memory concern.

    ``K <= 0`` is an explicit misconfiguration (not a "load nothing" request —
    that is handled by disabling memory altogether).  It is treated as 1 rather
    than silently becoming 1 via ``max(1, ...)`` so the caller sees consistent
    behaviour regardless of path.
    """
    if k is not None:
        if not isinstance(k, int) or isinstance(k, bool):
            return DEFAULT_K
        if k <= 0:
            log.warning(
                "session_memory: _hydrate_k received non-positive k=%d; clamping to 1",
                k,
            )
            return 1
        return min(k, _MAX_HYDRATE_K)

    try:
        raw = int(os.environ.get("VSA_MEMORY_HYDRATE_TURNS", str(DEFAULT_K)))
    except (ValueError, TypeError):
        return DEFAULT_K

    if raw <= 0:
        log.warning(
            "session_memory: VSA_MEMORY_HYDRATE_TURNS=%d is non-positive; clamping to 1",
            raw,
        )
        return 1
    return min(raw, _MAX_HYDRATE_K)


# ---------------------------------------------------------------------------
# T2.1 — Write a turn
# ---------------------------------------------------------------------------


def record_turn(
    client: Optional[Any],
    memory_id: Optional[str],
    actor_id: Optional[str],
    session_id: Optional[str],
    prompt: str,
    result: str,
) -> None:
    """Persist one completed conversation turn to AgentCore Memory.

    Issues one ``create_event`` with ``[(prompt, 'USER'), (result, 'ASSISTANT')]``.

    .. warning::
        The ``messages`` list uses ``(text, role)`` order — that is the
        AgentCore API contract.  ``load_recent_turns`` returns ``(role, text)``
        — the two are **intentionally reversed**.  See module docstring.

    **Cannot raise** — any exception is logged (ids and counts only, no payload)
    and swallowed.  The caller must never be affected by a memory failure (spec D4).

    No-ops silently when:
    - ``memory_id`` is falsy (memory disabled / not configured), or
    - ``actor_id`` is falsy (no authenticated identity — spec D1 forbids a
      fallback to ``session_id`` or any other identifier).

    Parameters
    ----------
    client:
        An injected ``MemoryClient`` instance, or ``None`` to create one lazily
        using ``VSA_MEMORY_REGION`` from the environment.
    memory_id:
        AgentCore Memory resource ID.  Empty string or ``None`` disables memory.
    actor_id:
        Authenticated user identity (Cognito ``sub``).  Must not be ``None``.
        Its **value** is never logged — only whether it was present.
    session_id:
        AgentCore session ID — stable across a conversation.
    prompt:
        The user's message text.
    result:
        The assistant's response text.
    """
    if not memory_id:
        return
    if not actor_id:
        return

    try:
        resolved_client = client or _get_memory_client(
            os.environ.get("VSA_MEMORY_REGION", "us-west-2")
        )
        messages: List[Tuple[str, str]] = [
            (prompt, _ROLE_USER),
            (result, _ROLE_ASSISTANT),
        ]
        resolved_client.create_event(memory_id, actor_id, session_id, messages)
        log.debug(
            "session_memory: recorded turn actor_present=True session=%s",
            session_id,
        )
    except Exception as exc:  # noqa: BLE001
        # Any exception is swallowed — a memory write failure must never fail
        # the turn.  Log session id and the exception CLASS NAME only; never the
        # actor_id value, the prompt, or the result — turns may contain VINs or
        # other owner data.  Class name (not str(exc) / args) distinguishes a
        # permanent config gap (AccessDeniedException) from a transient throttle
        # (ThrottlingException) at a glance without echoing request content.
        log.warning(
            "session_memory: failed to record turn session=%s exc_type=%s (swallowed)",
            session_id,
            type(exc).__name__,
        )


# ---------------------------------------------------------------------------
# T2.2 — Hydrate recent turns
# ---------------------------------------------------------------------------


def load_recent_turns(
    client: Optional[Any],
    memory_id: Optional[str],
    actor_id: Optional[str],
    session_id: Optional[str],
    *,
    k: Optional[int] = None,
) -> List[MemoryTurn]:
    """Return the last *k* conversation turns as :class:`MemoryTurn` named-tuples.

    .. warning::
        Returns ``(role, text)`` — the **opposite order** from ``record_turn``'s
        ``(text, role)`` write-path.  Use ``.role`` and ``.text`` attributes at
        the call site; do not rely on positional index without a comment.

    Turns are returned **oldest-first** so they can be prepended to the
    supervisor's message list in chronological order.  The service returns
    NEWEST-FIRST; this function reverses the outer list while preserving the
    chronological order of messages within each turn (NEWEST-FIRST outer /
    CHRONOLOGICAL inner — see docs/tech.md § AgentCore Memory contract).

    Returns ``[]`` on any failure or when memory is disabled.  Never raises.

    Failure logging distinguishes two cases (both return ``[]``, spec D4):

    * **Contract/shape failure** (``KeyError`` / ``TypeError`` / ``IndexError``
      reading ``msg["role"]`` or ``msg["content"]["text"]``): logged at ERROR
      level with a message naming the response contract as suspect and pointing
      at ``docs/tech.md`` § AgentCore Memory contract.  This case means
      hydration is permanently dead and amnesia will recur until the contract
      is corrected.
      TODO (follow-up): promote to a CloudWatch / EMF metric so operators have
      an unambiguous signal without grepping logs.

    * **Client/service failure** (all other exceptions): logged at WARNING.
      Throttles, transient network errors — self-healing, ignorable.

    A genuine zero-event first turn returns ``[]`` silently (not a failure).

    Parameters
    ----------
    client:
        An injected ``MemoryClient`` instance, or ``None`` to create one lazily.
    memory_id:
        AgentCore Memory resource ID.  Empty string or ``None`` → returns ``[]``.
    actor_id:
        Authenticated user identity.  ``None`` → returns ``[]`` (spec D1).
        Its **value** is never logged.
    session_id:
        AgentCore session ID.
    k:
        Number of turns to request.  Defaults to ``VSA_MEMORY_HYDRATE_TURNS``
        (env var, default ``DEFAULT_K=10``).  Capped at ``_MAX_HYDRATE_K``
        regardless of the source.

    Returns
    -------
    List of :class:`MemoryTurn` named-tuples — each has ``.role`` (UPPERCASE:
    ``'USER'``, ``'ASSISTANT'``, etc.) and ``.text``.  Empty list on any failure.
    """
    if not memory_id:
        return []
    if not actor_id:
        return []

    effective_k = _hydrate_k(k)

    try:
        resolved_client = client or _get_memory_client(
            os.environ.get("VSA_MEMORY_REGION", "us-west-2")
        )
        raw_turns = resolved_client.get_last_k_turns(
            memory_id, actor_id, session_id, k=effective_k
        )

        # raw_turns: List[List[Dict]] — outer is NEWEST-FIRST, inner is CHRONOLOGICAL.
        # We reverse the outer list to get OLDEST-FIRST, and iterate the inner
        # list in the original order to preserve USER-before-ASSISTANT within turns.
        result: List[MemoryTurn] = []
        for turn in reversed(raw_turns):
            for msg in turn:
                # KeyError / TypeError / IndexError here → contract failure (ERROR).
                # Caught in the except block below separately from service failures.
                role: str = msg["role"]                    # UPPERCASE
                text: str = msg["content"]["text"]         # nested, not flat
                result.append(MemoryTurn(role=role, text=text))

        log.debug(
            "session_memory: hydrated %d turns (%d messages) actor_present=True session=%s",
            len(raw_turns),
            len(result),
            session_id,
        )
        return result

    except (KeyError, TypeError, IndexError) as exc:
        # Shape/contract failure: msg["role"] or msg["content"]["text"] was absent
        # or of the wrong type.  This is NOT a transient service hiccup — it means
        # the API response no longer matches the contract in docs/tech.md §
        # "AgentCore Memory conversation-turn contract".  Hydration will be dead
        # until the contract is corrected; the amnesia bug will silently recur.
        # Logged at ERROR (not WARNING) so it is distinguishable from throttles.
        # exc_type is logged explicitly (not only via %r) so the class name is a
        # standalone searchable token — the same reason it is logged on the service
        # failure path below.
        # TODO: promote to a CloudWatch / EMF metric (Group 4/5 scope) so this
        # fires an alarm rather than requiring log triage.
        log.error(
            "session_memory: response contract mismatch reading turn messages "
            "(session=%s, exc_type=%s, error=%r) — check docs/tech.md § AgentCore Memory "
            "conversation-turn contract; hydration returning [] (swallowed)",
            session_id,
            type(exc).__name__,
            exc,
        )
        return []

    except Exception as exc:  # noqa: BLE001
        # Client/service failure: throttle, network error, auth failure, etc.
        # Transient and self-healing; log at WARNING so it is ignorable once
        # operators learn the pattern.  Distinguishable from contract failures
        # (above) by level and message.  Class name only — no str(exc), no args,
        # no traceback — service error messages can echo request content (VINs).
        log.warning(
            "session_memory: failed to load turns session=%s exc_type=%s (returning [])",
            session_id,
            type(exc).__name__,
        )
        return []
