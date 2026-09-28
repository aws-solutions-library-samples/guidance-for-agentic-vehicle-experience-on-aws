"""ChatDrillDownExecutor — writes a pre-seed marker into AgentCore Memory.

target_system = 'chat'

Purpose
-------
When a Finding is approved for a chat drill-down, this executor writes a
structured event into AgentCore Memory scoped to the owner. The Tier 1
supervisor's session-init hook (a separate task; lives in agents/supervisor/)
reads this marker before the owner's first turn and hydrates the conversation
so the assistant already knows *why* it reached out.

This executor is **producer-only**. The consumer (session-init reader) is not
implemented here. The marker shape is documented in the docstring of
``_build_marker_payload`` so the reader has a stable contract to implement
against.

AgentCore Memory API (verified via inspect.signature on the installed package)
------------------------------------------------------------------------------
::

    MemoryClient.create_event(
        memory_id,
        actor_id,
        session_id,
        messages: List[Tuple[str, str]],
        event_timestamp=None,
        branch=None,
        metadata: Dict[str, {'stringValue': str}] | None = None,
    )

- ``messages`` is a list of ``(text, role)`` tuples; role ∈ {USER, ASSISTANT, TOOL}
- ``metadata`` allows at most 15 key–value pairs; each value is ``{'stringValue': '...'}``
- import path: ``from bedrock_agentcore.memory import MemoryClient``

The client is imported lazily so unit tests need not install the preview package;
inject a stub via the ``memory_client`` constructor kwarg.

Scoping
-------
- ``actor_id``  = the owner id (``action['owner_id']`` or ``claims['sub']``)
- ``session_id`` = ``f"session_pending-{finding_id}"``  (pending-session semantics)
  The separator is a HYPHEN, not a colon. AgentCore Memory constrains ``sessionId``
  to ``[a-zA-Z0-9][a-zA-Z0-9-_]*`` — a colon is rejected with ValidationException,
  so the original colon form could never be written at all. See
  ``issues/2026-09-23-chat-drilldown-marker-session-id-rejected-by-agentcore/``.
- ``memory_id``  = env var ``VSA_FOUNDATION_MEMORY_ID``
- region        = env var ``VSA_MEMORY_REGION`` (default ``us-west-2``)

TTL
---
AgentCore Memory has no per-event TTL. The marker therefore MUST carry an
explicit absolute expiry. ``_build_marker_payload`` writes an ``expires_at``
(ISO-8601 UTC) in both the JSON body and the event metadata. The session-init
reader is responsible for ignoring expired markers. The expiry window is
``MARKER_TTL_HOURS`` (default 72 h) after the moment of write.

Privacy
-------
Evidence entries may include VINs. The implementation:
- MUST NOT log any VIN, licence plate, or other owner PII.
- MUST NOT log the full marker payload.
- MAY log the finding id and the count of evidence entries.

Spec: `.kiro/specs/2026-09-18-avx-tier2-ios-cards-and-actions/spec.md`
"""

from __future__ import annotations

import json
import re
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Mapping, Optional, Tuple

from agents.executors.base import (
    ActionStatus,
    ExecuteResult,
    never_raises,
)

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TARGET_SYSTEM = "chat"

#: How long (in hours) a pre-seed marker remains valid after it is written.
#: The session-init reader MUST reject markers whose ``expires_at`` has passed.
MARKER_TTL_HOURS: int = 72

#: The key used as the session_id in AgentCore Memory for a pending drill-down.
#: Namespaced to avoid collision with live session ids, which are UUID v4.
_SESSION_PENDING_PREFIX = "session_pending"

#: Separator between the prefix and the finding id. MUST NOT be a colon:
#: AgentCore Memory rejects a sessionId containing one. Kept as a named constant so
#: the reader can assert the same value rather than duplicating a literal.
_SESSION_ID_SEPARATOR = "-"

#: AgentCore Memory's own documented constraint on ``sessionId``. Asserted at the
#: point of construction so a separator change that the SERVICE would reject fails a
#: unit test here instead of failing silently in production — which is what happened
#: for the whole life of Group 5, because every test stubbed the client.
_AGENTCORE_SESSION_ID_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9-_]*$")

#: Maximum metadata pairs AgentCore Memory accepts per event.
_METADATA_LIMIT = 15

#: Name under which the JSON marker body is stored as a TOOL message.
_MARKER_ROLE = "TOOL"


# ---------------------------------------------------------------------------
# Marker payload
# ---------------------------------------------------------------------------


def _build_marker_payload(
    finding_id: str,
    action_id: str,
    evidence: List[Any],
    *,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Build the JSON body written as the TOOL message in AgentCore Memory.

    Shape (stable contract for the session-init reader)::

        {
            "marker_kind":  "drill_down_pre_seed",
            "finding_id":   "<uuid>",
            "action_id":    "<uuid>",
            "written_at":   "<ISO-8601 UTC>",
            "expires_at":   "<ISO-8601 UTC>",   # reader MUST reject if past
            "evidence":     [<evidence entries from the Finding>],
        }

    ``expires_at`` is ``written_at + MARKER_TTL_HOURS`` hours.  It is REQUIRED
    and the session-init reader MUST NOT hydrate from a marker where
    ``expires_at <= now``.
    """
    ts = now or datetime.now(timezone.utc)
    written_at = ts.replace(microsecond=0).isoformat().replace("+00:00", "Z")
    expires_dt = (ts + timedelta(hours=MARKER_TTL_HOURS)).replace(microsecond=0)
    expires_at = expires_dt.isoformat().replace("+00:00", "Z")

    return {
        "marker_kind": "drill_down_pre_seed",
        "finding_id": finding_id,
        "action_id": action_id,
        "written_at": written_at,
        "expires_at": expires_at,
        "evidence": evidence,
    }


def _build_metadata(
    finding_id: str,
    action_id: str,
    expires_at: str,
) -> Dict[str, Dict[str, str]]:
    """Build the AgentCore Memory event metadata dict (≤15 pairs).

    Metadata is kept minimal and carries only routing / expiry keys —
    never VINs or other PII. The metadata limit is 15 pairs; this function
    produces at most 4.
    """
    meta = {
        "marker_kind":  {"stringValue": "drill_down_pre_seed"},
        "finding_id":   {"stringValue": finding_id},
        "action_id":    {"stringValue": action_id},
        "expires_at":   {"stringValue": expires_at},
    }
    # Guard against future growth: raise at call time so the error surfaces
    # on a real dict (not a literal that the compiler can fold away).
    if len(meta) > _METADATA_LIMIT:
        raise ValueError(
            f"metadata has {len(meta)} pairs but AgentCore Memory only allows "
            f"{_METADATA_LIMIT}; trim before calling create_event"
        )
    return meta


# ---------------------------------------------------------------------------
# Executor
# ---------------------------------------------------------------------------


class ChatDrillDownExecutor:
    """Writes a drill-down pre-seed marker into AgentCore Memory.

    The Tier 1 supervisor's session-init hook reads this marker (producer-only
    here; the consumer is a separate task in agents/supervisor/).

    Parameters
    ----------
    memory_client:
        Injected ``MemoryClient`` instance.  When ``None``, the constructor
        creates one lazily using ``VSA_FOUNDATION_MEMORY_ID`` /
        ``VSA_MEMORY_REGION`` from the environment.  Inject a stub in tests.
    """

    target_system: str = TARGET_SYSTEM

    def __init__(self, *, memory_client: Optional[Any] = None) -> None:
        self._memory_client = memory_client  # None → constructed lazily

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_memory_client(self) -> Any:
        """Return the injected client, or create one from env vars."""
        if self._memory_client is not None:
            return self._memory_client
        # Lazy import — bedrock_agentcore is a preview package.
        from bedrock_agentcore.memory import MemoryClient  # noqa: PLC0415

        region = os.environ.get("VSA_MEMORY_REGION", "us-west-2")
        self._memory_client = MemoryClient(region_name=region)
        return self._memory_client

    @staticmethod
    def _resolve_memory_id() -> Optional[str]:
        return os.environ.get("VSA_FOUNDATION_MEMORY_ID") or None

    # ------------------------------------------------------------------
    # Executor Protocol implementation
    # ------------------------------------------------------------------

    @never_raises
    def dispatch(self, action: Mapping[str, Any], claims: Mapping[str, Any]) -> ExecuteResult:
        """Write a pre-seed marker into AgentCore Memory for the owner.

        Fail-closed rules
        -----------------
        - Missing ``VSA_FOUNDATION_MEMORY_ID`` → terminal_error.
          A missing marker means the drill-down opens with no context and the
          owner sees an assistant that has forgotten why it messaged them.  We
          MUST NOT silently succeed.
        - Any exception from ``create_event`` is caught by ``@never_raises``
          and mapped to ``terminal_error``.

        Logging
        -------
        Only the finding id and the evidence count are logged.  VINs and the
        full payload are NEVER logged (privacy requirement).
        """
        memory_id = self._resolve_memory_id()
        if not memory_id:
            return ExecuteResult.terminal_error(
                last_error_class="MissingConfiguration",
                note=(
                    "VSA_FOUNDATION_MEMORY_ID is not set; cannot write pre-seed "
                    "marker — drill-down would open with no context for the owner"
                ),
            )

        # ---- extract fields from action / claims ----------------------
        finding = action.get("finding", {}) or {}
        finding_id: str = str(finding.get("id") or action.get("finding_id", "")).strip()
        if not finding_id:
            # Without this, an absent finding id yields session_id
            # "session_pending-" and dispatch reports COMPLETED: a marker written
            # into a bucket shared by every action missing a finding id, which no
            # reader can ever look up (readers resolve BY finding id). A silent
            # useless write is worse than a named failure. Found 2026-09-23 while
            # live-verifying T18.5 — the regex guard below does not catch it,
            # because "session_pending-" satisfies AgentCore's pattern.
            return ExecuteResult.terminal_error(
                last_error_class="MissingFindingId",
                note=(
                    "no finding id in action['finding']['id'] or "
                    "action['finding_id']; refusing to write a marker no reader "
                    "could resolve"
                ),
            )
        # Schema key is action_id; "id" is a legacy alias kept for compatibility.
        action_id: str = str(action.get("action_id") or action.get("id") or "")
        evidence: List[Any] = list(finding.get("evidence") or [])

        # ---- owner identity: CLAIMS are authoritative here ----------------
        #
        # This marker pre-seeds a drill-down conversation in the owner's OWN
        # AgentCore Memory session, so the only correct owner is the authenticated
        # caller. Preferring `action["owner_id"]` (as the first cut did) inverts
        # the trust direction: if the approve route ever accepts a caller-supplied
        # `owner_id` without cross-checking it, an attacker could write a marker
        # into a victim's session carrying attacker-chosen `evidence[]` — LLM
        # context injection delivered with the victim's own authority, read by the
        # supervisor before the victim's first turn.
        #
        # `claims["sub"]` therefore wins. An `action["owner_id"]` that disagrees is
        # rejected rather than silently ignored: a disagreement means the caller is
        # asking us to seed someone else's session, and the right answer to that is
        # an error, not a quiet substitution.
        #
        # Contrast `ios_owner.py`, where the action legitimately names a DIFFERENT
        # owner than the caller (a dealer proposing something to a customer). That
        # executor cannot use this rule; its authorization has to happen at the
        # approve route, which is why the requirement is stated there explicitly.
        caller_sub: str = str(claims.get("sub") or claims.get("owner_id") or "")
        claimed_owner: str = str(action.get("owner_id") or "")

        if caller_sub and claimed_owner and claimed_owner != caller_sub:
            return ExecuteResult.terminal_error(
                last_error_class="OwnerMismatch",
                note=(
                    "action.owner_id does not match the authenticated caller; "
                    "refusing to pre-seed another owner's chat session"
                ),
            )

        owner_id: str = caller_sub or claimed_owner

        if not owner_id:
            return ExecuteResult.terminal_error(
                last_error_class="MissingOwner",
                note="owner_id could not be resolved from claims or action",
            )

        # ---- build payload (no VINs in logs) --------------------------
        now = datetime.now(timezone.utc)
        payload = _build_marker_payload(finding_id, action_id, evidence, now=now)
        expires_at: str = payload["expires_at"]
        metadata = _build_metadata(finding_id, action_id, expires_at)

        # Log only safe fields — no VINs, no payload dump.
        log.info(
            "chat executor: writing pre-seed marker for finding=%s evidence_count=%d",
            finding_id,
            len(evidence),
        )

        session_id = f"{_SESSION_PENDING_PREFIX}{_SESSION_ID_SEPARATOR}{finding_id}"
        if not _AGENTCORE_SESSION_ID_RE.match(session_id):
            # Fail here, with the cause named, rather than at create_event where
            # @never_raises maps it to a bare status=failed and withholds the message.
            return ExecuteResult.terminal_error(
                last_error_class="InvalidSessionId",
                note=(
                    "constructed sessionId violates AgentCore's "
                    "[a-zA-Z0-9][a-zA-Z0-9-_]* constraint; check the separator "
                    "and the finding_id shape"
                ),
            )
        messages: List[Tuple[str, str]] = [(json.dumps(payload), _MARKER_ROLE)]

        client = self._get_memory_client()
        client.create_event(
            memory_id,
            owner_id,        # actor_id = owner
            session_id,
            messages,
            metadata=metadata,
        )

        log.info(
            "chat executor: marker written for finding=%s session=%s",
            finding_id,
            session_id,
        )

        return ExecuteResult.success(
            target_ref=session_id,
            status=ActionStatus.COMPLETED,
            note=f"pre-seed marker written; expires_at={expires_at}",
        )
