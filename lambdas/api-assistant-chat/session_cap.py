"""Per-session turn ceiling — UX guard for kiosk and anonymous sessions.

Design intent (decisions.md Q9)
--------------------------------
The cap protects against ONE stuck or adversarial conversation, not
aggregate show volume. It deliberately keys on a **client-supplied**
session id, so a client can bypass it by rotating the id. That is
intentional: the real spend backstop is an API Gateway usage plan plus an
AWS Budgets action (Group 4). This module is a **UX guard**, not an
adversarial cost control.

Fail-open contract
------------------
If the counter store raises for any reason, the request is **allowed
through** and the error is logged loudly. A broken counter must not take
the booth down mid-show.

Storage
-------
Module-level dict of ``{session_id: (count, expires_monotonic)}``. Keeps
the hot path free of external service calls (no DynamoDB, no Redis).
Within one Lambda container the counter is durable across warm invocations.
Across container recycling the counter resets — acceptable, because a cold
start is effectively a new conversation context anyway.

Configuration (read at call time so tests can monkeypatch.setenv)
-----------------------------------------------------------------
``VSA_SESSION_TURN_LIMIT`` — max turns per session id.
    Default: 25. Design point is 60–80 visitors/hour at 2–3 min dwell,
    ~3–6 turns each; 25 gives curious explorers substantial headroom
    while bounding a stuck or script-driven session.

``VSA_SESSION_TTL_SECONDS`` — how long a session counter lives before
    expiry. Default: 14400 (4 hours — one show slot). Prevents a counter
    from the morning setup run from blocking afternoon visitors who happen
    to reuse the same generated session id.

Public API
----------
``check_and_increment(session_id)`` -> ``(allowed: bool, turns_used: int)``
    Returns ``(True, n)`` when the turn is within the ceiling.
    Returns ``(False, n)`` when the ceiling has been reached or exceeded.
    Always returns ``(True, 0)`` (fail-open) when an internal error occurs,
    and logs at WARNING level.
"""

from __future__ import annotations

import logging
import os
import time

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# In-process counter store
# ---------------------------------------------------------------------------

# Dict[session_id -> (count: int, expires_monotonic: float)]
_COUNTERS: dict[str, tuple[int, float]] = {}

# Sentinel to detect "env var explicitly set to empty" vs "not set"
_ENV_UNSET = object()


def _turn_limit() -> int:
    """Return the configured turn ceiling (read at call time)."""
    raw = os.environ.get("VSA_SESSION_TURN_LIMIT", "")
    if raw.strip():
        try:
            v = int(raw.strip())
            if v > 0:
                return v
        except ValueError:
            log.warning(
                "session_cap: invalid VSA_SESSION_TURN_LIMIT %r; using default 25", raw
            )
    return 25


def _ttl_seconds() -> float:
    """Return the configured counter TTL in seconds (read at call time)."""
    raw = os.environ.get("VSA_SESSION_TTL_SECONDS", "")
    if raw.strip():
        try:
            v = float(raw.strip())
            if v > 0:
                return v
        except ValueError:
            log.warning(
                "session_cap: invalid VSA_SESSION_TTL_SECONDS %r; using default 14400",
                raw,
            )
    return 14400.0  # 4 hours


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def check_and_increment(session_id: str) -> tuple[bool, int]:
    """Check the session turn counter and increment it.

    Returns ``(allowed, turns_used)`` where ``turns_used`` is the count
    AFTER incrementing (i.e. 1 on the first call for a session).

    Fail-open: on any internal error logs a WARNING and returns
    ``(True, 0)`` so the request is allowed through.

    Args:
        session_id: Client-supplied session identifier. Empty/None is
            treated as the special key ``"__anonymous__"`` and is allowed
            through (a missing session id is caught earlier in handler.py).
    """
    try:
        return _check_and_increment_inner(session_id or "__anonymous__")
    except Exception:  # noqa: BLE001
        log.warning(
            "session_cap: counter store error for session=%r — failing open",
            session_id,
            exc_info=True,
        )
        return True, 0


def _check_and_increment_inner(session_id: str) -> tuple[bool, int]:
    """Core implementation — not protected by fail-open catch."""
    limit = _turn_limit()
    ttl = _ttl_seconds()
    now = time.monotonic()

    existing = _COUNTERS.get(session_id)
    if existing is not None:
        count, expires = existing
        if now < expires:
            # Live entry
            new_count = count + 1
            _COUNTERS[session_id] = (new_count, expires)  # preserve original expiry
            allowed = new_count <= limit
            if not allowed:
                log.warning(
                    "session_cap: session=%r exceeded limit=%d (turns=%d)",
                    session_id,
                    limit,
                    new_count,
                )
            return allowed, new_count
        else:
            # Expired — treat as new session
            _COUNTERS.pop(session_id, None)

    # New (or just-expired) session
    new_count = 1
    _COUNTERS[session_id] = (new_count, now + ttl)
    return True, new_count


def reset_session(session_id: str) -> None:
    """Remove the counter for a session. Used in tests."""
    _COUNTERS.pop(session_id, None)


def clear_all() -> None:
    """Clear all counters. Used in tests to avoid cross-test pollution."""
    _COUNTERS.clear()


# ---------------------------------------------------------------------------
# Graceful canned response
# ---------------------------------------------------------------------------

SESSION_LIMIT_RESPONSE = (
    "You've reached the end of this session. "
    "Please start a new session to continue exploring Meridian vehicles."
)
