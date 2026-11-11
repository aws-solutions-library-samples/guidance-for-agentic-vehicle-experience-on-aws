"""
Selection orchestration and cursor store for the Tier 2 platform health agent.

Spec: .kiro/specs/2026-08-10-cvx-tier2-vin-selection-and-alerting/spec.md § D3
Spec: .kiro/specs/2026-10-05-fi-fleet-health-stage-d/spec.md § D7 (managed-first)

This module orchestrates VIN selection across runs by maintaining a per-component
cursor in the existing vsa-{stage}-health-findings DynamoDB table.  It sits between
the runner and the ADP source modules, seeing only VIN strings — no SQL, no ADP
column names.

Public interface
----------------

    resolve_batch(source, cursor_store, component, limit, run_stats=None) -> list[str]

        Read the stored cursor, call ``source.select_vins(cursor=..., limit=...)``,
        persist the new cursor, and return the VIN list.

        The optional *run_stats* dict is updated with ``managed_select_errors``
        (incremented once per run where the managed query fails with an exception)
        and, when the managed pool is larger than *limit*, ``managed_coverage``
        (the U7 7-day fallback record; see ``_managed_coverage``).
        Pass a shared dict to collect these; omit to ignore them.

        When *source* also exposes ``select_managed_vins(cursor, limit)`` (spec § D7),
        the batch is resolved in two phases:

        Phase M — managed VINs:
            Call ``source.select_managed_vins(cursor=managed_cursor, limit=limit)``
            to get up to *limit* managed VINs starting after *managed_cursor*.
            If the managed pool is exhausted, ``managed_cursor`` resets to ``None``
            so the next run starts from the beginning (rotation).

        Phase P — population VINs (remaining cap):
            remaining = limit − len(managed_vins)
            If remaining > 0: call ``source.select_vins(cursor=pop_cursor,
            limit=remaining)`` with the usual exhaustion / wrap-around semantics.

        Deduplication: population VINs that already appear in the managed batch
        are removed before combining.

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
attributes ``last_vin``, ``managed_last_vin`` (optional), ``updated_at``, and
``pool_exhausted_count``.

``last_vin`` is the population cursor — the last VIN returned by
``source.select_vins()`` in the most recent run.

``managed_last_vin`` (optional, absent on legacy items) is the managed cursor —
the last managed VIN returned by ``source.select_managed_vins()`` when the
managed pool is larger than the per-run cap (rotation; spec § D7).  Absent when
no managed rotation is in progress.

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

#: U7 (stage D spec): a component whose managed pool doesn't fit its nightly cap "falls back to
#: every managed vehicle within 7 days and says so in evidence". The run states the fallback in
#: its run record's ``managed_coverage`` map (see :func:`_managed_coverage`).
FALLBACK_CYCLE_NIGHTS = 7

_COVERAGE_MODE_FALLBACK = "7_day_fallback"
_COVERAGE_MODE_UNMEASURED = "unmeasured"


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


class SelectSourceWithManagedVins(Protocol):
    """Extended source protocol that supports managed-first selection (spec § D7).

    A source implementing this protocol exposes both ``select_vins`` (population
    sweep) and ``select_managed_vins`` (managed-priority pool).  Column names and
    SQL remain exclusively in the source modules; this protocol sees only VIN
    strings.
    """

    def select_vins(
        self, *, cursor: str | None, limit: int
    ) -> tuple[list[str], str | None]:
        ...

    def select_managed_vins(
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
    managed_last_vin: str | None = None,
) -> dict[str, Any]:
    """Build a cursor item for storage.

    The item intentionally carries NO ``ttl`` attribute — DynamoDB TTL reaps
    only items that carry the attribute, and losing the cursor silently resets
    all pagination progress (spec § D3).

    Parameters
    ----------
    component:
        Component name, e.g. ``"tires"``.
    last_vin:
        Population cursor — the last VIN returned by ``select_vins()`` in the
        most recent run.  ``None`` when the population sweep starts fresh.
    pool_exhausted_count:
        Number of completed population pool passes.
    managed_last_vin:
        Managed cursor — the last VIN returned by ``select_managed_vins()``
        when managed rotation is active (managed pool > per-run cap).  ``None``
        when absent or not in rotation (spec § D7).
    """
    item: dict[str, Any] = {
        "pk": f"CURSOR#{component}",
        "sk": "STATE",
        "last_vin": last_vin,
        "updated_at": _now_utc(),
        "pool_exhausted_count": pool_exhausted_count,
    }
    if managed_last_vin is not None:
        item["managed_last_vin"] = managed_last_vin
    return item


def _read_cursor_safe(
    cursor_store: CursorStore, component: str
) -> tuple[str | None, int, str | None]:
    """Read the stored cursor for *component* with fail-safe error handling.

    Returns ``(last_vin, pool_exhausted_count, managed_last_vin)``.  Any error
    — missing item, wrong types, absent keys — is caught, logged as a warning,
    and treated as ``(None, 0, None)``.  Never raises into the run.

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
            return None, 0, None

        last_vin = item.get("last_vin")
        # last_vin must be str or None; any other type is malformed
        if last_vin is not None and not isinstance(last_vin, str):
            _log.warning(
                "Malformed cursor item for component %r: last_vin has wrong type %r "
                "— treating as None and starting from the beginning",
                component,
                type(last_vin).__name__,
            )
            return None, 0, None

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
                return None, 0, None
        elif last_vin == "":
            _log.warning(
                "Malformed cursor item for component %r: last_vin is an empty string "
                "— treating as None and starting from the beginning",
                component,
            )
            return None, 0, None

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

        # managed_last_vin — optional; absent on legacy items (no managed-first
        # rotation in progress).  Any malformed value is silently treated as None
        # (fail-safe: restart managed rotation from the beginning).
        managed_last_vin = item.get("managed_last_vin")
        if managed_last_vin is not None:
            if not isinstance(managed_last_vin, str) or managed_last_vin == "":
                _log.warning(
                    "Malformed cursor item for component %r: managed_last_vin has "
                    "wrong type or is empty — treating as None",
                    component,
                )
                managed_last_vin = None
            else:
                try:
                    _sanitise_vin(managed_last_vin)
                except ValueError:
                    _log.warning(
                        "Malformed cursor item for component %r: managed_last_vin "
                        "contains no valid VIN characters — treating as None",
                        component,
                    )
                    managed_last_vin = None

        return last_vin, pool_exhausted_count, managed_last_vin

    except Exception as exc:  # noqa: BLE001
        _log.warning(
            "Error reading cursor for component %r: %s — "
            "starting from the beginning",
            component,
            exc,
        )
        return None, 0, None


# ---------------------------------------------------------------------------
# Public interface
# ---------------------------------------------------------------------------


def resolve_batch(
    source: SelectSource,
    cursor_store: CursorStore,
    component: str,
    limit: int,
    run_stats: dict[str, Any] | None = None,
) -> list[str]:
    """Resolve the next VIN batch for *component*, advancing the cursor.

    When *source* exposes ``select_managed_vins`` (spec § D7), the batch is
    resolved in two phases:

    Phase M — managed VINs:
        Fetch up to *limit* managed VINs starting after *managed_cursor*.
        The managed cursor resets to ``None`` when the managed pool is
        exhausted (rotation: next run starts from the beginning).

    Phase P — population VINs (remaining cap):
        remaining = limit − len(managed_vins)
        If remaining > 0, call ``select_vins`` with *pop_cursor* and
        *remaining* as the limit.  Population exhaustion / wrap-around
        semantics are identical to the non-managed path (see below).

    Deduplication: population VINs that already appear in the managed batch
    are removed before combining.

    When *source* does NOT expose ``select_managed_vins``, the existing
    algorithm (population-only sweep) is used unchanged.

    Population exhaustion algorithm (spec § D3):

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
    # S1: guard against limit=0 (operator setting a component cap to 0 to pause
    # it).  An empty-page check on managed_vins[-1] with limit=0 would raise
    # IndexError outside the managed-query try block and abort the run.
    if limit <= 0:
        return []

    # Step 1 — read stored cursors (fail-safe)
    initial_last_vin, pool_exhausted_count, managed_last_vin = _read_cursor_safe(
        cursor_store, component
    )

    # -------------------------------------------------------------------------
    # Managed-first path (spec § D7)
    # -------------------------------------------------------------------------
    has_managed = callable(getattr(source, "select_managed_vins", None))

    if has_managed:
        return _resolve_batch_managed_first(
            source=source,  # type: ignore[arg-type]
            cursor_store=cursor_store,
            component=component,
            limit=limit,
            initial_pop_cursor=initial_last_vin,
            initial_managed_cursor=managed_last_vin,
            pool_exhausted_count=pool_exhausted_count,
            run_stats=run_stats,
        )

    # -------------------------------------------------------------------------
    # Population-only path (original algorithm, unchanged)
    # -------------------------------------------------------------------------
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


def _resolve_batch_managed_first(
    source: "SelectSourceWithManagedVins",
    cursor_store: CursorStore,
    component: str,
    limit: int,
    initial_pop_cursor: str | None,
    initial_managed_cursor: str | None,
    pool_exhausted_count: int,
    run_stats: dict[str, Any] | None = None,
) -> list[str]:
    """Resolve a batch using managed-first selection (spec § D7).

    Phase M: fetch up to *limit* managed VINs starting after
    *initial_managed_cursor*.

    Managed cursor semantics (C1 fix):

    * **Full page** (``len == limit``): more managed VINs remain; advance
      the managed cursor so the next run continues from where this one left
      off (rotation).
    * **Short or empty page from a non-None cursor**: pool exhausted at the
      end of a rotation pass.  Wrap around: re-query from the start of the
      pool to fill the remaining managed slots in *this* run.  Reset the
      cursor to ``None`` so the *next* run begins a fresh rotation pass.
    * **Short or empty page from cursor=None**: pool is small (fits in one
      query).  No cursor needed; reset to ``None``.  Every run covers the
      whole managed pool.

    Managed query failure (W1 fix): if ``source.select_managed_vins`` raises,
    the error is logged and the run falls back to population-only for this
    run.  The managed cursor is NOT advanced, so the next run retries the
    managed query from the same position.

    Phase P: fill the remaining cap from the population sweep using
    *initial_pop_cursor*.  Population exhaustion / wrap-around follow the
    same three-case semantics as the population-only path.

    Deduplication: population VINs that appear in the managed batch are
    removed before combining.

    Cursor persistence: a single cursor item stores both ``last_vin``
    (population cursor) and, when managed rotation is active,
    ``managed_last_vin`` (managed cursor).
    """
    # ------------------------------------------------------------------
    # Phase M — managed VINs
    # ------------------------------------------------------------------
    # W1: a managed-query failure must never abort the whole health run.
    # Catch the exception, log it, and fall back to population-only for this run.
    _managed_select_failed = False
    _managed_wrap_failed = False
    try:
        managed_vins, _ = source.select_managed_vins(
            cursor=initial_managed_cursor, limit=limit
        )
    except Exception as _exc:  # noqa: BLE001
        _log.warning(
            "selection: managed query failed for component %r — "
            "falling back to population-only for this run: %s",
            component,
            _exc,
        )
        managed_vins = []
        _managed_select_failed = True
        # W3: count the failure in run_stats so the runner can alarm on it.
        if run_stats is not None:
            run_stats["managed_select_errors"] = (
                run_stats.get("managed_select_errors", 0) + 1
            )

    # C1: store the managed cursor ONLY when the page came back full
    # (len == limit), which means more managed VINs remain and rotation
    # is active.  A short or empty page means the pool is exhausted in
    # this pass.
    #
    # Crucially, when the initial cursor was non-None and the page is short
    # or empty (pool tail reached), we wrap around: re-query from the start
    # of the managed pool to fill the remaining managed slots in THIS run,
    # rather than handing those slots to the population sweep.  This ensures
    # every run sees all managed VINs when pool <= cap, and no run has zero
    # managed VINs when pool > cap (spec § D7, decisions item 1).
    new_managed_cursor: str | None
    if _managed_select_failed:
        # W3: preserve the cursor on failure so the next run retries from
        # the same position (as the docstring promises).  Don't reset to
        # None — a transient error should not restart battery's rotation.
        new_managed_cursor = initial_managed_cursor
    elif len(managed_vins) == limit:
        # Full page — rotation is active; advance the managed cursor.
        new_managed_cursor = managed_vins[-1]
    else:
        # Short or empty page — managed pool exhausted in this sweep pass.
        if initial_managed_cursor is not None:
            # We are partway through a rotation pass (cursor was non-None).
            # Wrap: fill the remaining managed slots from the beginning of
            # the pool so this run still provides a complete managed set.
            _remaining_managed_slots = limit - len(managed_vins)
            try:
                _wrap_vins, _ = source.select_managed_vins(
                    cursor=None, limit=_remaining_managed_slots
                )
            except Exception as _wrap_exc:  # noqa: BLE001
                _log.warning(
                    "selection: managed wrap-around query failed for component %r: %s",
                    component,
                    _wrap_exc,
                )
                _wrap_vins = []
                # A failed wrap leaves this run's managed slice short (the tail of the pool
                # only), so it counts as a managed-select failure: it reaches the run record
                # and the run's coverage is recorded as unmeasured, not left silent
                # (issue 2026-10-22-battery-fallback-record, review cycle 1 W2).
                _managed_wrap_failed = True
                if run_stats is not None:
                    run_stats["managed_select_errors"] = (
                        run_stats.get("managed_select_errors", 0) + 1
                    )
            _seen_managed = frozenset(managed_vins)
            for _v in _wrap_vins:
                if _v not in _seen_managed:
                    managed_vins.append(_v)
                    _seen_managed = _seen_managed | {_v}
        # W1: after wrap (or short page from cursor=None), set cursor based on
        # the combined set size rather than always resetting to None.
        # When pool == cap or pool == k×cap the last page is exactly full after
        # the wrap fill — keep the cursor so the next run does NOT re-take the
        # same batch (the k×cap defect this fixes).
        new_managed_cursor = managed_vins[-1] if len(managed_vins) == limit else None

    # U7 fallback record: a full managed page may mean the pool is larger than the cap.
    if run_stats is not None and not _managed_select_failed:
        coverage = _managed_coverage(
            source=source,
            component=component,
            limit=limit,
            managed_vins=managed_vins,
            initial_managed_cursor=initial_managed_cursor,
            wrap_failed=_managed_wrap_failed,
        )
        if coverage is not None:
            run_stats["managed_coverage"] = coverage

    # ------------------------------------------------------------------
    # Phase P — population VINs (remaining cap after managed)
    # ------------------------------------------------------------------
    remaining = limit - len(managed_vins)

    managed_set = frozenset(managed_vins)
    pop_vins: list[str] = []
    new_pop_cursor: str | None = initial_pop_cursor

    if remaining > 0:
        raw_pop, raw_next = source.select_vins(
            cursor=initial_pop_cursor, limit=remaining
        )

        if raw_pop:
            # Deduplicate population against managed
            pop_vins = [v for v in raw_pop if v not in managed_set]
            new_pop_cursor = raw_pop[-1]  # advance to last population VIN seen
        elif initial_pop_cursor is None:
            # Empty pool from the start — do not retry (exhaustion semantic 1)
            pop_vins = []
            new_pop_cursor = None
        else:
            # Population pool exhausted mid-sweep — wrap around (semantic 3)
            pool_exhausted_count += 1
            raw_pop_restart, _ = source.select_vins(cursor=None, limit=remaining)
            pop_vins = [v for v in raw_pop_restart if v not in managed_set]
            new_pop_cursor = raw_pop_restart[-1] if raw_pop_restart else None

    # ------------------------------------------------------------------
    # Persist combined cursor state and return batch
    # ------------------------------------------------------------------
    cursor_item = _build_cursor_item(
        component=component,
        last_vin=new_pop_cursor,
        pool_exhausted_count=pool_exhausted_count,
        managed_last_vin=new_managed_cursor,
    )
    cursor_store.put_cursor(component, cursor_item)

    return managed_vins + pop_vins


def _managed_coverage(
    *,
    source: Any,
    component: str,
    limit: int,
    managed_vins: list[str],
    initial_managed_cursor: str | None,
    wrap_failed: bool = False,
) -> dict[str, Any] | None:
    """Return the run's U7 fallback record, or ``None`` when the managed pool fits the cap.

    Called once per run after Phase M (not after a failed first managed query). A run whose
    wrap-around query failed took only the tail of a rotating pool: its slice is short, so it
    records ``unmeasured`` (and selection counts the failure in ``managed_select_errors``).
    Otherwise only a full managed page (``len == limit``) can mean the pool is larger than the
    cap, so a short page writes nothing and runs no count. A full page counts the pool with
    ``source.count_managed_vins(after=<this run's managed cursor>)``:

    * pool <= cap: every run covers the whole pool (the pool == cap wrap case), nothing to record.
    * pool > cap: the cursor rotates and each run takes ``cap`` managed vehicles in circular VIN
      order, so ``ceil(pool / cap)`` consecutive runs cover the pool. The record::

          {"mode": "7_day_fallback", "cycle_nights": 7, "cap": cap, "managed_pool": pool,
           "nights_to_cover": ceil(pool / cap), "cycle_covers": min(pool, 7 * cap),
           "slice_start": <1-based VIN-order position of this run's first managed vehicle>,
           "slice_size": <managed vehicles this run took>}

      ``cycle_covers < managed_pool`` means 7 nights don't cover the pool; it is recorded as it
      is, never rounded up to a claim.
    * No count method, a failed count or an inconsistent answer: ``{"mode": "unmeasured", ...}``
      without pool fields, so no reader can mistake it for the fallback. Never raises.
    """
    if limit <= 0:
        return None
    unmeasured: dict[str, Any] = {
        "mode": _COVERAGE_MODE_UNMEASURED,
        "cycle_nights": FALLBACK_CYCLE_NIGHTS,
        "cap": limit,
        "slice_size": len(managed_vins),
    }
    if wrap_failed:
        _log.warning(
            "selection: event=managed_coverage_unmeasured component=%r reason=wrap_failed "
            "slice_size=%d",
            component,
            len(managed_vins),
        )
        return unmeasured
    if len(managed_vins) != limit:
        return None
    count_fn = getattr(source, "count_managed_vins", None)
    if not callable(count_fn):
        _log.warning(
            "selection: event=managed_coverage_unmeasured component=%r reason=no_count_method",
            component,
        )
        return unmeasured
    try:
        pool, at_or_before = count_fn(after=initial_managed_cursor)
    except Exception as exc:  # noqa: BLE001
        _log.warning(
            "selection: event=managed_coverage_unmeasured component=%r reason=count_failed: %s",
            component,
            type(exc).__name__,
        )
        return unmeasured
    if (
        type(pool) is not int
        or type(at_or_before) is not int
        or pool < len(managed_vins)
        or not 0 <= at_or_before <= pool
    ):
        _log.warning(
            "selection: event=managed_coverage_unmeasured component=%r reason=inconsistent_count",
            component,
        )
        return unmeasured
    if pool <= limit:
        return None
    cycle_covers = min(pool, FALLBACK_CYCLE_NIGHTS * limit)
    record = {
        "mode": _COVERAGE_MODE_FALLBACK,
        "cycle_nights": FALLBACK_CYCLE_NIGHTS,
        "cap": limit,
        "managed_pool": pool,
        "nights_to_cover": -(-pool // limit),
        "cycle_covers": cycle_covers,
        "slice_start": at_or_before % pool + 1,
        "slice_size": len(managed_vins),
    }
    _log.info(
        "selection: event=managed_7_day_fallback component=%r managed_pool=%d cap=%d "
        "nights_to_cover=%d cycle_covers=%d slice_start=%d slice_size=%d",
        component, pool, limit, record["nights_to_cover"], cycle_covers,
        record["slice_start"], record["slice_size"],
    )
    if cycle_covers < pool:
        _log.warning(
            "selection: event=managed_cycle_short component=%r managed_pool=%d cycle_covers=%d",
            component, pool, cycle_covers,
        )
    return record
