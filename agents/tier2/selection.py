"""
Selection orchestration and cursor store for the Tier 2 platform health agent.

Spec: .kiro/specs/2026-08-10-cvx-tier2-vin-selection-and-alerting/spec.md § D3

This module orchestrates VIN selection across runs by maintaining a per-component
cursor in the existing vsa-{stage}-health-findings DynamoDB table.  It sits between
the runner and the ADP source modules, seeing only VIN strings — no SQL, no ADP
column names.

Public interface
----------------

    resolve_batch(source, cursor_store, component, limit) -> list[str]

        Read the stored cursor, call ``source.select_vins(cursor=..., limit=...)``,
        persist the new cursor, and return the VIN list.

        Exhaustion semantics (spec § D3, three explicitly-specified cases):

        1. If ``select_vins`` was already called with ``cursor=None`` and returned
           empty, do NOT retry — one call, empty batch, done.  There is no "start"
           to return to; the pool is empty.

        2. If the post-wrap re-query (after exhaustion with a prior cursor) returns
           VINs, persist the new cursor as normal.  Leaving it cleared would cause
           the next run to re-select the same batch, reproducing the defect this
           spec exists to fix.

        3. ``pool_exhausted_count`` increments exactly once, when exhaustion is
           detected.  The re-query leg does not increment it again — the counter
           counts completed pool passes, not individual Athena calls.

Cursor storage
--------------
The cursor item is stored under ``pk="CURSOR#{component}", sk="STATE"`` with
attributes ``last_vin``, ``updated_at``, and ``pool_exhausted_count``.

NO ``ttl`` attribute is written — DynamoDB TTL reaps only items that carry the
attribute.  Omitting it is what keeps the cursor alive across runs (spec § D3).

The cursor is written via ``ArtifactWriter.put_raw`` (unconditional overwrite)
because it is mutable state, not an artifact.  It must not pass ``validate_artifact``.

Failure behaviour
-----------------
Every cursor read error is fail-safe: log a warning, treat the cursor as None
(start from the beginning), and never raise into the run.  A restarted sweep
writes duplicates that the conditional put absorbs.  Given the choice, re-doing
work beats stopping.

Isolation
---------
This module contains NO SQL and NO ADP column names.  The selection layer receives
only VIN strings from source.select_vins().  ADP column names belong exclusively
in the owning source modules under agents/health/.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Protocol

from agents.health._vin import sanitise_vin as _sanitise_vin

_log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Protocols — injected interfaces
# ---------------------------------------------------------------------------


class SelectSource(Protocol):
    """Any ADP source module that exposes a select_vins() method.

    The method returns (vins, next_cursor) where next_cursor is None when the
    pool is exhausted.  No ADP column names or SQL appear here — those live
    exclusively in the source modules under agents/health/.
    """

    def select_vins(
        self, *, cursor: str | None, limit: int
    ) -> tuple[list[str], str | None]:
        ...


class CursorStore(Protocol):
    """Persistent storage for per-component cursor state.

    The concrete implementation writes to the vsa-{stage}-health-findings
    DynamoDB table via ArtifactWriter.put_raw.  A fake double is injected
    in tests.
    """

    def get_cursor(self, component: str) -> dict[str, Any] | None:
        ...

    def put_cursor(self, component: str, item: dict[str, Any]) -> None:
        ...


# ---------------------------------------------------------------------------
# Cursor item helpers — no ADP column names, no SQL
# ---------------------------------------------------------------------------


def _now_utc() -> str:
    """Return the current UTC time as an ISO-8601 string."""
    return datetime.now(tz=timezone.utc).isoformat()


def _build_cursor_item(
    component: str,
    last_vin: str | None,
    pool_exhausted_count: int,
) -> dict[str, Any]:
    """Build a cursor item for storage.

    The item intentionally carries NO ``ttl`` attribute — DynamoDB TTL reaps
    only items that carry the attribute, and losing the cursor silently resets
    all pagination progress (spec § D3).
    """
    return {
        "pk": f"CURSOR#{component}",
        "sk": "STATE",
        "last_vin": last_vin,
        "updated_at": _now_utc(),
        "pool_exhausted_count": pool_exhausted_count,
    }


def _read_cursor_safe(
    cursor_store: CursorStore, component: str
) -> tuple[str | None, int]:
    """Read the stored cursor for *component* with fail-safe error handling.

    Returns ``(last_vin, pool_exhausted_count)``.  Any error — missing item,
    wrong types, absent keys — is caught, logged as a warning, and treated
    as ``(None, 0)``.  Never raises into the run.

    Spec § D3: "a missing, malformed, or unreadable cursor is treated as None
    (start from the beginning) with a warning."
    """
    try:
        item = cursor_store.get_cursor(component)
        if item is None:
            _log.warning(
                "No cursor item found for component %r — starting from the beginning",
                component,
            )
            return None, 0

        last_vin = item.get("last_vin")
        # last_vin must be str or None; any other type is malformed
        if last_vin is not None and not isinstance(last_vin, str):
            _log.warning(
                "Malformed cursor item for component %r: last_vin has wrong type %r "
                "— treating as None and starting from the beginning",
                component,
                type(last_vin).__name__,
            )
            return None, 0

        # A non-None last_vin that sanitises to empty cannot be embedded in
        # Athena SQL.  Spec § D3: "a missing, malformed, or unreadable cursor
        # is treated as None (start from the beginning) with a warning."
        # Returning it as-is would let sanitise_vin raise ValueError inside
        # the source's SQL builder, which propagates into the runner — fail-loud
        # rather than fail-safe.  Catch the empty-after-sanitisation case here
        # so every malformed cursor consistently takes the None path.
        if last_vin is not None and last_vin != "":
            try:
                _sanitise_vin(last_vin)
            except ValueError:
                _log.warning(
                    "Malformed cursor item for component %r: last_vin contains no "
                    "valid VIN characters after sanitisation — treating as None and "
                    "starting from the beginning",
                    component,
                )
                return None, 0
        elif last_vin == "":
            _log.warning(
                "Malformed cursor item for component %r: last_vin is an empty string "
                "— treating as None and starting from the beginning",
                component,
            )
            return None, 0

        pool_exhausted_count = item.get("pool_exhausted_count", 0)
        # pool_exhausted_count must be int; any other type is malformed
        if not isinstance(pool_exhausted_count, int):
            _log.warning(
                "Malformed cursor item for component %r: pool_exhausted_count has "
                "wrong type %r — treating as 0",
                component,
                type(pool_exhausted_count).__name__,
            )
            pool_exhausted_count = 0

        return last_vin, pool_exhausted_count

    except Exception as exc:  # noqa: BLE001
        _log.warning(
            "Error reading cursor for component %r: %s — "
            "starting from the beginning",
            component,
            exc,
        )
        return None, 0


# ---------------------------------------------------------------------------
# Public interface
# ---------------------------------------------------------------------------


def resolve_batch(
    source: SelectSource,
    cursor_store: CursorStore,
    component: str,
    limit: int,
) -> list[str]:
    """Resolve the next VIN batch for *component*, advancing the cursor.

    Algorithm (spec § D3):

    1. Read the stored cursor (fail-safe: missing/malformed → None).
    2. Call ``source.select_vins(cursor=stored_last_vin, limit=limit)``.
    3a. If VINs returned: persist cursor with last_vin = last VIN in batch.
        Return the batch.
    3b. If empty and the initial cursor was None (already at start):
        Exhaustion on an empty pool — do NOT retry.  Return [].
        This is exhaustion semantic (1): one call, empty batch, done.
    3c. If empty and the initial cursor was non-None:
        Pool exhausted — increment pool_exhausted_count (semantic 3: once only).
        Re-query once from the start (cursor=None).
        - If re-query returns VINs: persist the new cursor (semantic 2).
          Return the batch.
        - If re-query also returns empty: persist the reset cursor with
          last_vin=None.  Return [].

    Returns
    -------
    list[str]
        The resolved VIN batch.  Empty list when the pool is genuinely empty.
    """
    # Step 1 — read stored cursor (fail-safe)
    initial_last_vin, pool_exhausted_count = _read_cursor_safe(cursor_store, component)

    # Step 2 — query the source
    vins, next_cursor = source.select_vins(cursor=initial_last_vin, limit=limit)

    if vins:
        # Step 3a — normal batch: advance cursor to last VIN returned
        last_vin_in_batch = vins[-1]
        cursor_item = _build_cursor_item(
            component=component,
            last_vin=last_vin_in_batch,
            pool_exhausted_count=pool_exhausted_count,
        )
        cursor_store.put_cursor(component, cursor_item)
        return vins

    # Empty result — determine whether to retry
    if initial_last_vin is None:
        # Step 3b — already at the start, pool is empty.
        # Semantic (1): do not retry.  One call, empty batch, done.
        # Persist the reset state so the next run starts cleanly.
        cursor_item = _build_cursor_item(
            component=component,
            last_vin=None,
            pool_exhausted_count=pool_exhausted_count,
        )
        cursor_store.put_cursor(component, cursor_item)
        return []

    # Step 3c — exhaustion with a prior cursor.
    # Semantic (3): increment pool_exhausted_count exactly once here.
    pool_exhausted_count += 1

    # Re-query from the start — exactly one retry, never a loop.
    vins_from_start, next_cursor_from_start = source.select_vins(cursor=None, limit=limit)

    if vins_from_start:
        # Semantic (2): persist the new cursor so the next run continues
        # from where this wrap night left off.  Leaving it cleared would
        # cause the next run to re-select this same batch (the exact defect
        # this spec fixes).
        last_vin_in_batch = vins_from_start[-1]
        cursor_item = _build_cursor_item(
            component=component,
            last_vin=last_vin_in_batch,
            pool_exhausted_count=pool_exhausted_count,
        )
        cursor_store.put_cursor(component, cursor_item)
        return vins_from_start

    # Re-query also returned empty — persist reset state and return empty.
    cursor_item = _build_cursor_item(
        component=component,
        last_vin=None,
        pool_exhausted_count=pool_exhausted_count,
    )
    cursor_store.put_cursor(component, cursor_item)
    return []
