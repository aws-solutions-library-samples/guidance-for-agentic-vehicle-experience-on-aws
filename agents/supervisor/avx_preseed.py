"""AVX drill-down pre-seed reader — the consumer half of ``ChatDrillDownExecutor``.

When an owner taps an AVX Finding card on iOS and drills into the assistant,
``agents/executors/chat.py`` has already written a **pre-seed marker** into
AgentCore Memory.  This module reads it back at session establishment so the
supervisor's first turn is grounded in the Finding under discussion, instead of
opening cold and asking "which alert do you mean?".

Until 2026-09-23 the marker had **zero readers** (Group 5 follow-on 2).  This
module closes that gap.

The marker contract
-------------------
Written by ``chat.py`` — this module is the documented reader of that contract,
so the two must be changed together:

* ``actor_id``   = the owner id
* ``session_id`` = ``f"session_pending:{finding_id}"``
* role           = ``TOOL``
* body (JSON)    = ``{marker_kind, finding_id, action_id, written_at,
  expires_at, evidence}``

``chat.py``'s own docstring states the reader **MUST NOT** hydrate from a marker
whose ``expires_at`` has passed.  :func:`resolve_pre_seed_fragment` enforces
that, and a test pins the boundary case.

Why this returns a system-prompt fragment
-----------------------------------------
The marker is **context, not content** — it must never appear in the transcript.
Two consequences, both deliberate:

1. It is appended to the bidi system prompt (alongside the make/model pin at
   ``bidi_app.py``), **not** passed as ``messages=``.  ``bidi_app.py``'s Nova
   Sonic patch notes that its history path "skips the 50KB/200KB truncation and
   user-first enforcement because our bidi_app never passes ``messages=``";
   using it would activate a path whose safeguards were removed on the
   assumption nobody would.
2. Evidence lines are rendered as a bounded, flattened summary rather than raw
   JSON, so a large ``evidence[]`` cannot blow out the prompt.

Failure policy — cold-open, never a session error
-------------------------------------------------
Every failure mode degrades to ``None``, meaning "no pre-seed", which is
byte-identical to the behaviour before this feature existed:

* header absent (older client, no drill-down pending) — caller never calls us
* marker missing / expired / malformed  → ``None`` + warning
* memory disabled or unreachable        → ``None`` + warning
* **marker belongs to another owner**   → ``None`` + warning

The last one is the non-obvious one.  A scope mismatch on ``GET /findings/{id}``
correctly returns 403, because that route's whole job is to return the finding.
Here the session's whole job is to *open*, so an authz miss must not take out
the chat surface — a stale card in someone's notification tray would otherwise
make the assistant unusable.  Cross-owner reads are prevented by deriving
``actor_id`` from the caller's own JWT ``sub`` claim and using it as the memory
scope, so another owner's marker is not in the scope being read.

.. warning::
   **That protection is only as strong as the ``sub`` it reads, and on the bidi
   runtime the JWT signature is NOT verified.**

   ``agentcore_app.py::_extract_actor_id`` documents this trust boundary at
   length and states explicitly that its reasoning "holds for the TEXT runtime
   only", because the text runtime's ``InvokeAgentRuntime`` is IAM-scoped to the
   single route Lambda, whereas the bidi runtime's Cognito pool auth-roles are
   much wider.  There is no PyJWT, no JWKS client and no signature check
   anywhere under ``agents/``, and ``bidi_app.py`` never validates the token
   against Cognito.  ``RequestContext`` exposes no authenticated caller
   principal either, so there is nothing verified to bind to instead.

   **Residual risk, stated plainly:** a caller able to invoke the bidi runtime
   (i.e. any signed-in app user) who also knows another owner's ``sub`` *and* a
   specific ``finding_id`` can read that Finding's evidence lines.

   Accepted for now on two grounds, not on the strength of the scope check:
   exploitation requires two independently non-enumerable UUIDs, and the payoff
   is bounded — at most :data:`_MAX_EVIDENCE_LINES` evidence strings, with no
   VIN and no PII in the marker payload.  It is **not** "structurally
   impossible", and this docstring must not be reworded to say so.

   The durable fix is verifying the JWT signature before trusting ``sub`` on
   this runtime — tracked as T18.6 in the spec's Fix Group 18, and flagged to
   the security reviewer rather than left for it to find.

This function never raises.
"""

from __future__ import annotations

import json
import logging
import re
import sys
from datetime import datetime, timezone
from typing import Any, List, Optional

from agents.supervisor.session_memory import MemoryTurn, load_recent_turns

log = logging.getLogger(__name__)

#: Session-id prefix used by ``chat.py`` when writing the marker.  Must match
#: ``agents.executors.chat._SESSION_PENDING_PREFIX``.
SESSION_PENDING_PREFIX = "session_pending"

#: Separator between prefix and finding id. MUST match the writer's
#: ``chat._SESSION_ID_SEPARATOR``, and MUST NOT be a colon: AgentCore Memory
#: constrains ``sessionId`` to ``[a-zA-Z0-9][a-zA-Z0-9-_]*`` and rejects a colon
#: outright. The original colon form meant the writer could never store a marker,
#: so this reader had nothing to read for the whole life of Group 5 — found by
#: T18.5 the first time either side ran against the real service. See
#: ``issues/2026-09-23-chat-drilldown-marker-session-id-rejected-by-agentcore/``.
SESSION_ID_SEPARATOR = "-"

#: AgentCore Memory's documented ``sessionId`` constraint, asserted on the value this
#: module CONSTRUCTS. Writer and reader sharing a constant makes them agree with each
#: other; only this makes either agree with the service.
AGENTCORE_SESSION_ID_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9-_]*$")

#: ``marker_kind`` discriminator.  Must match ``chat.py``'s payload builder.
MARKER_KIND = "drill_down_pre_seed"

#: Role the marker is written under.  Must match ``chat.py::_MARKER_ROLE``.
MARKER_ROLE = "TOOL"

#: Marker events are few (one per drill-down); a small window is plenty and
#: keeps the Memory read cheap on a latency-sensitive session-establish path.
_MARKER_LOOKBACK_TURNS = 5

#: Cap on evidence lines rendered into the prompt fragment.  A Finding with a
#: long ``evidence[]`` must not crowd out the persona prompt.
_MAX_EVIDENCE_LINES = 5

#: Cap on characters per rendered evidence line.
_MAX_EVIDENCE_CHARS = 200

#: Cap on the attacker-supplied ``finding_id`` header before it is concatenated
#: into an AgentCore Memory session_id (security review Cycle 1, S2).
_MAX_FINDING_ID_CHARS = 100


def actor_id_from_jwt(jwt: str | None) -> Optional[str]:
    """Return the JWT's ``sub`` claim, or ``None``.

    An **unverified** base64url decode of the payload segment — see the module
    docstring's trust-boundary warning before relying on it for anything wider
    than this module's bounded pre-seed read.

    Deliberately duplicated rather than imported from
    ``agentcore_app.py::_extract_actor_id``, for two reasons:

    1. That function is private, lives in a module a concurrent session was
       actively editing, and its own docstring says **"Do not lift this function
       into a shared module without re-evaluating the trust boundary for both
       runtimes."**  This copy carries that re-evaluation (module docstring).
    2. ``agentcore_app.py`` constructs its own ``BedrockAgentCoreApp()`` at
       import time.  Importing it from ``bidi_app.py`` — a second AgentCore
       entrypoint — would instantiate two apps in one process.

    ``None`` means "no usable identity", which callers must treat as
    memory-disabled.  It must never be replaced by a fallback such as
    ``session_id``, the tenant id, or a constant: any of those would collapse
    every caller into one shared memory scope.

    De-duplicating this with the peer session's copy — ideally by lifting it
    into ``session_memory.py`` **with** a per-runtime trust note — is a
    recommended follow-on, not something to do while that file is held.
    """
    import base64

    if not jwt or not isinstance(jwt, str):
        return None

    parts = jwt.split(".")
    if len(parts) != 3:
        log.debug(
            "actor_id_from_jwt: JWT has %d segment(s), expected 3 — memory disabled",
            len(parts),
        )
        return None

    try:
        payload_b64 = parts[1]
        payload_b64 += "=" * (4 - len(payload_b64) % 4)
        claims = json.loads(base64.b64decode(payload_b64))
    except Exception:  # noqa: BLE001 — any malformed token means no identity.
        log.debug("actor_id_from_jwt: failed to decode JWT payload — memory disabled")
        return None

    if not isinstance(claims, dict):
        log.debug("actor_id_from_jwt: JWT payload is not a JSON object — memory disabled")
        return None

    sub = claims.get("sub")
    if not isinstance(sub, str) or not sub.strip():
        log.debug("actor_id_from_jwt: no usable sub in JWT claims — memory disabled")
        return None
    return sub.strip()


def _parse_iso8601_utc(raw: Any) -> Optional[datetime]:
    """Parse an ISO-8601 timestamp to an aware UTC datetime, or ``None``.

    ``chat.py`` writes ``...Z``; ``datetime.fromisoformat`` accepts ``Z`` only
    from Python 3.11, and this repo targets 3.12+, but the ``Z``→``+00:00``
    swap is kept explicit so the intent survives a version change.

    Comparing these values **as strings** would be a latent defect: ``Z`` and
    ``+00:00`` denote the same instant but sort differently, so a lexical
    comparison silently reverses for one of the two spellings.  Always compare
    the parsed datetimes.
    """
    if not isinstance(raw, str) or not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    # A naive timestamp is ambiguous; treat it as UTC rather than rejecting the
    # marker outright, since the writer's contract is UTC.
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _render_fragment(finding_id: str, evidence: List[Any]) -> str:
    """Render the system-prompt fragment for a validated marker.

    Bounded in both directions: at most ``_MAX_EVIDENCE_LINES`` entries, each
    truncated to ``_MAX_EVIDENCE_CHARS``.
    """
    lines: List[str] = []
    for entry in evidence[:_MAX_EVIDENCE_LINES]:
        if isinstance(entry, str):
            text = entry
        elif isinstance(entry, dict):
            # Prefer a human-readable field when the evidence entry is
            # structured; fall back to compact JSON so nothing is silently lost.
            text = str(
                entry.get("summary")
                or entry.get("description")
                or entry.get("detail")
                or json.dumps(entry, separators=(",", ":"), sort_keys=True)
            )
        else:
            text = str(entry)
        text = text.strip()
        if not text:
            continue
        if len(text) > _MAX_EVIDENCE_CHARS:
            text = text[: _MAX_EVIDENCE_CHARS - 1].rstrip() + "…"
        lines.append(f"- {text}")

    fragment = (
        "\n\nThe owner opened this conversation from a specific vehicle-health "
        f"alert (finding {finding_id}). Treat that alert as the subject of the "
        "first turn — do NOT ask which alert they mean."
    )
    if lines:
        fragment += "\n\nWhat the alert is based on:\n" + "\n".join(lines)
    fragment += (
        "\n\nThis context was provided by the application, not spoken by the "
        "owner. Do not read it back verbatim, and do not claim the owner said it."
    )
    return fragment


def _select_marker(turns: List[MemoryTurn]) -> Optional[dict]:
    """Return the newest valid marker body from ``turns``, or ``None``.

    ``load_recent_turns`` returns oldest-first, so iterate reversed to prefer
    the most recent marker if a finding was drilled into more than once.
    """
    for turn in reversed(turns):
        role = getattr(turn, "role", None)
        if not isinstance(role, str) or role.upper() != MARKER_ROLE:
            continue
        text = getattr(turn, "text", None)
        if not isinstance(text, str) or not text:
            continue
        try:
            body = json.loads(text)
        except (ValueError, TypeError):
            # A non-JSON TOOL message is not ours; keep looking.
            continue
        if not isinstance(body, dict):
            continue
        if body.get("marker_kind") != MARKER_KIND:
            continue
        return body
    return None


def resolve_pre_seed_fragment(
    *,
    finding_id: Optional[str],
    actor_id: Optional[str],
    memory_id: Optional[str],
    client: Optional[Any] = None,
    now: Optional[datetime] = None,
) -> Optional[str]:
    """Resolve a drill-down pre-seed marker into a system-prompt fragment.

    Returns ``None`` — meaning "open cold" — for every failure mode.  Never
    raises.

    Parameters
    ----------
    finding_id:
        Value of the ``AvxFindingId`` custom header.  ``None``/empty → ``None``.
    actor_id:
        The **caller's own** identity, from their JWT ``sub`` claim.  This is
        the memory scope that gets read, which is what keeps one owner out of
        another's markers.  Never pass a value taken from the header or from
        the marker body — see the mutation contract in the spec's Fix Group 18
        T18.2.  Note the module docstring's warning: on the bidi runtime the
        ``sub`` is an unverified decode, so this is a meaningful but not
        airtight boundary.
    memory_id:
        AgentCore Memory resource id.  ``None``/empty → ``None`` (memory
        disabled; today's behaviour).
    client:
        Injectable Memory client.  ``None`` lets ``load_recent_turns`` create
        one lazily, matching the peer session-memory path.
    now:
        Injectable clock for expiry tests.  Defaults to ``datetime.now(utc)``.
    """
    if not finding_id or not isinstance(finding_id, str):
        return None
    finding_id = finding_id.strip()
    if not finding_id:
        return None
    # Security review Cycle 1 S2: bound the attacker-supplied string before it
    # becomes part of an AgentCore Memory session_id. Today an over-length value
    # would be rejected by boto3's shape validation, i.e. the guard is borrowed
    # from a dependency rather than owned here. A real finding_id is a UUID-shaped
    # token; 100 chars is generous.
    if len(finding_id) > _MAX_FINDING_ID_CHARS:
        log.warning(
            "avx_preseed: finding_id header is %d chars (max %d) — opening cold",
            len(finding_id),
            _MAX_FINDING_ID_CHARS,
        )
        return None
    if not actor_id:
        log.warning(
            "avx_preseed: no actor_id for finding=%s — memory scope unresolvable, "
            "opening cold",
            finding_id,
        )
        return None
    if not memory_id:
        log.warning(
            "avx_preseed: memory disabled (no memory_id) for finding=%s — opening cold",
            finding_id,
        )
        return None

    session_id = f"{SESSION_PENDING_PREFIX}{SESSION_ID_SEPARATOR}{finding_id}"
    if not AGENTCORE_SESSION_ID_RE.match(session_id):
        # Cold-open rather than raise, per this module's policy: a finding id whose
        # shape yields an unreadable sessionId is a bad anchor, not a session error.
        log.warning(
            "avx_preseed: constructed sessionId for finding=%s violates AgentCore's "
            "constraint — opening cold",
            finding_id,
        )
        return None

    # load_recent_turns is documented never to raise and to return [] on any
    # failure.  The belt-and-braces except is deliberate: this runs on the
    # session-establish path, and a future change to that contract must not be
    # able to take the chat surface down.  A bare `except Exception` here is the
    # cold-open policy, not laziness.
    try:
        turns = load_recent_turns(
            client,
            memory_id,
            actor_id,
            session_id,
            k=_MARKER_LOOKBACK_TURNS,
        )
    except Exception:  # noqa: BLE001 — cold-open policy; see above.
        # Log the exception TYPE only, never the exception itself and never a
        # traceback. Security review Cycle 1 W2: `exc_info=True` here could echo
        # request content into CloudWatch if `load_recent_turns`'s contract were
        # ever broken, and `actor_id` is passed to it. The peer's
        # `session_memory.py:415` carries an explicit comment against this exact
        # hazard ("service error messages can echo request content (VINs)") and
        # logs `type(exc).__name__` only; matching that discipline.
        log.warning(
            "avx_preseed: memory read raised for finding=%s exc_type=%s — opening cold",
            finding_id,
            type(sys.exc_info()[1]).__name__,
        )
        return None

    if not turns:
        # Either no marker was ever written for this finding, or it belongs to
        # a different owner and is therefore not in this actor's scope. Both
        # are indistinguishable from here, and both correctly cold-open.
        log.warning(
            "avx_preseed: no pre-seed marker in this owner's scope for finding=%s "
            "— opening cold (absent, expired-out, or another owner's)",
            finding_id,
        )
        return None

    body = _select_marker(turns)
    if body is None:
        log.warning(
            "avx_preseed: no %s marker among %d event(s) for finding=%s — opening cold",
            MARKER_KIND,
            len(turns),
            finding_id,
        )
        return None

    # The marker's own finding_id must agree with the header. A mismatch means
    # the memory scope returned something unexpected; prefer cold-open over
    # grounding the session in the wrong Finding.
    body_finding_id = body.get("finding_id")
    if body_finding_id != finding_id:
        log.warning(
            "avx_preseed: marker finding_id disagrees with header for finding=%s "
            "— opening cold",
            finding_id,
        )
        return None

    expires_at = _parse_iso8601_utc(body.get("expires_at"))
    if expires_at is None:
        # expires_at is REQUIRED by the writer's contract. A marker without a
        # parseable one is malformed, and honouring it would mean hydrating
        # from a marker with no expiry at all.
        log.warning(
            "avx_preseed: marker for finding=%s has missing/unparseable expires_at "
            "— opening cold",
            finding_id,
        )
        return None

    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    # `<=` not `<`: the writer's contract says the reader MUST NOT hydrate when
    # expires_at has been reached. Exactly-at-expiry is expired.
    if expires_at <= current:
        log.warning(
            "avx_preseed: marker for finding=%s expired at %s — opening cold",
            finding_id,
            body.get("expires_at"),
        )
        return None

    evidence = body.get("evidence")
    if not isinstance(evidence, list):
        evidence = []

    # Log only the finding id and evidence count — never the marker body, never
    # a VIN. Mirrors chat.py's logging discipline on the write side.
    log.info(
        "avx_preseed: hydrating finding=%s evidence_count=%d",
        finding_id,
        len(evidence),
    )
    return _render_fragment(finding_id, evidence)


def append_pre_seed_to_prompt(
    prompt: str,
    *,
    finding_id: Optional[str],
    jwt: Optional[str],
    memory_id: Optional[str],
    client: Optional[Any] = None,
    now: Optional[datetime] = None,
) -> str:
    """Return ``prompt`` with the pre-seed fragment appended, or unchanged.

    The single call site for the bidi runtime.  Exists as a named function
    rather than inline in ``bidi_app.py`` for one reason: ``bidi_app``'s session
    setup is reachable only through a Starlette WebSocket handler, and the
    repo's async bidi tests are **skipped** in this venv (``pytest.ini`` sets
    ``asyncio_mode = auto`` but ``pytest-asyncio`` is not installed, and no CI
    job runs ``agents/supervisor/tests/`` at all).  An append written inline
    there would be untestable in practice, so the composition lives here where
    a synchronous test can assert it — including the identity case.

    Returns ``prompt`` **unchanged** (same object) whenever there is no
    applicable marker, so "no pre-seed" is byte-identical to the behaviour
    before this feature existed.
    """
    if not finding_id:
        return prompt
    fragment = resolve_pre_seed_fragment(
        finding_id=finding_id,
        actor_id=actor_id_from_jwt(jwt),
        memory_id=memory_id,
        client=client,
        now=now,
    )
    if not fragment:
        return prompt
    return prompt + fragment
