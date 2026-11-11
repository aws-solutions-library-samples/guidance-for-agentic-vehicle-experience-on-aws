"""Per-session proactive P0 voice watcher (spec 2026-11-05-avx-proactive-p0-alert § V).

Pure-logic class only.  All I/O and asyncio live in bidi_app._run_proactive_p0_watcher,
which uses this class as its state machine.

Design summary
--------------
A ``ProactiveP0Watcher`` is created once per session.  After session establishment:

* ``baseline(dtcs)`` records the DTC set that was present at session start; those faults
  are silently ignored on every tick unless ``openReason == "p0_push"``.
* The watcher task calls ``new_faults(dtcs)`` each tick.  A fault is "new" when its
  ``(code, firstSeenAt)`` key appears in neither the baseline nor the alerted set.
* ``should_alert(classification, keys)`` is True only for P0 and only for keys not yet
  in the alerted set.
* ``mark_alerted(keys)`` records the alerted fault keys so the same occurrence is never
  surfaced twice.

The ``firstSeenAt`` field on each DTC entry is the DynamoDB row's ``timestamp`` value
(milliseconds), added to the entries returned by ``bidi_app._fetch_active_dtcs``.  Using
the row timestamp rather than the code alone means a code that is cleared and re-raised
(new row, different timestamp) is correctly detected as a new fault.

``openReason == "p0_push"``
----------------------------
When the iOS app was opened by tapping a P0 push notification it sends the
``X-Amzn-Bedrock-AgentCore-Runtime-Custom-OpenReason: p0_push`` header on the
WebSocket (read in ``bidi_app._establish_session``; only ``p0_push`` is kept; the
``session.start`` field is read only on the header-less path).  In this case the watcher
skips setting the baseline so all currently active P0 codes are treated as new on the
first tick and the classifier is asked immediately.  The test pins:

    baseline faults not alerted unless openReason == "p0_push"
"""

from __future__ import annotations

__all__ = ["ProactiveP0Watcher", "PROACTIVE_P0_POLL_SECONDS", "PROACTIVE_P0_FALLBACK_SECONDS"]

# Seconds between polls.  The watcher sleeps this long before each fetch.
PROACTIVE_P0_POLL_SECONDS: int = 10

# Seconds to wait for a model segment leading with the directive before the
# server sends ``platform.speak`` itself.
PROACTIVE_P0_FALLBACK_SECONDS: float = 6.0


class ProactiveP0Watcher:
    """Pure-logic state machine for the per-session proactive P0 watcher.

    No I/O, no asyncio: all side-effecting work is in bidi_app.

    Attributes mirror the four methods described in spec § V:
        ``baseline``       — record session-start faults (skip for p0_push)
        ``new_faults``     — faults not in baseline or alerted set
        ``should_alert``   — True for P0 and unalerted keys only
        ``mark_alerted``   — consume keys so they are not surfaced again
    """

    def __init__(self, *, open_reason: str | None = None) -> None:
        self._open_reason: str | None = open_reason
        # (code, firstSeenAt) tuples present at session start.  Empty when
        # open_reason == "p0_push" so those faults are treated as new.
        self._baseline: frozenset[tuple[str, int]] = frozenset()
        # (code, firstSeenAt) tuples for which the watcher has already sent an
        # alert.  Also prevents the same alert on repeated ticks.
        self._alerted: set[tuple[str, int]] = set()

    # ------------------------------------------------------------------ public

    @property
    def open_reason(self) -> str | None:
        """The ``openReason`` from the client's ``session.start`` message, if any."""
        return self._open_reason

    @staticmethod
    def fault_key(d: dict) -> tuple[str, int]:
        """Stable identity key for one DTC entry: ``(code, firstSeenAt)``.

        ``firstSeenAt`` defaults to 0 when absent so plain entries (e.g. those
        from test fixtures that omit it) still form a sensible key.
        """
        return (d.get("code", ""), int(d.get("firstSeenAt", 0) or 0))

    def baseline(self, active_dtcs: list[dict]) -> None:
        """Record the initial session-start DTC set.

        Called once, at session establishment, when ``open_reason != "p0_push"``.
        Faults in the baseline are silently ignored by ``new_faults``; they are
        already known and the driver is presumably already aware of them.

        Calling baseline on a ``p0_push`` session is not an error but has no effect
        (the baseline stays empty so all faults look new on the first tick).
        """
        if self._open_reason == "p0_push":
            return
        self._baseline = frozenset(
            self.fault_key(d) for d in (active_dtcs or []) if d.get("code")
        )

    def new_faults(self, active_dtcs: list[dict]) -> list[dict]:
        """Return faults not in the baseline or already alerted.

        For ``open_reason == "p0_push"`` the baseline is empty, so all currently
        active faults are returned on the first tick; the watcher classifies them
        immediately and the driver hears the P0 alert without delay.

        A fault whose key IS in ``_alerted`` is not returned even if it is still
        active — the driver has already been told.  A later occurrence with a
        different ``firstSeenAt`` (cleared and re-raised) produces a different key
        and is returned as new.
        """
        result = []
        for d in (active_dtcs or []):
            if not d.get("code"):
                continue
            key = self.fault_key(d)
            if key not in self._baseline and key not in self._alerted:
                result.append(d)
        return result

    def should_alert(
        self, classification: str | None, fault_keys: list[tuple[str, int]]
    ) -> bool:
        """True only when ``classification == "P0"`` and at least one key is unalerted.

        The P0 guard is the deterministic seam: no speech, card or gate for P1–P3
        from this path.  ``mark_alerted`` must be called before the next tick to
        prevent a second alert for the same occurrence.
        """
        if classification != "P0":
            return False
        return any(k not in self._alerted for k in fault_keys)

    def mark_alerted(self, keys: list[tuple[str, int]]) -> None:
        """Record that these fault-occurrence keys have been alerted.

        After this call ``new_faults`` will no longer return an entry with any of
        these keys, and ``should_alert`` will return False for them.
        """
        self._alerted.update(keys)
