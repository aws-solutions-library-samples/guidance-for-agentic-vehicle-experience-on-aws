"""Red tests — managed-first VIN selection for the health agent (task 1.4 item 7).

Covers spec D7: managed VINs in the priority pool come first, then the population sweep
continues with the remaining cap; the cursor never skips a population VIN; the cursor
advances only on population VINs.

Every test is xfail(strict=True, reason="FI stage D task 1.4"). All not-yet-built module
imports are inside test bodies so collection never errors. Use CSV custom:fleetIds in
fixtures.

Run:
    .venv/bin/python -m pytest agents/health/tests/test_managed_first_selection.py -q
"""
from __future__ import annotations

import pytest

# ---------------------------------------------------------------------------
# (W2: select_managed_first and advance_cursor helpers deleted from selection.py;
#  their tests are removed here.  Real-path tests via resolve_batch cover the
#  same properties without relying on removed helpers.)
# ---------------------------------------------------------------------------




# ============================================================================
# Integration test — real batch path (spec D7, task 3.1)
# ============================================================================

class _FakeManagedSource:
    """Fake source exposing both select_managed_vins and select_vins.

    Implements the SelectSourceWithManagedVins protocol without any I/O.

    Parameters
    ----------
    managed_vins:
        Ordered list of managed VINs to return (with cursor filtering applied
        internally, mimicking a real Athena keyset-paginated query).
    population_vins:
        Ordered list of population VINs to return.
    """

    def __init__(
        self,
        managed_vins: list[str],
        population_vins: list[str],
    ) -> None:
        self._managed = managed_vins
        self._population = population_vins
        # Track calls so mutation tests can assert which methods were invoked.
        self.managed_calls: list[dict] = []
        self.population_calls: list[dict] = []

    def select_managed_vins(
        self, *, cursor: str | None, limit: int
    ) -> tuple[list[str], str | None]:
        """Return up to *limit* managed VINs starting after *cursor*."""
        self.managed_calls.append({"cursor": cursor, "limit": limit})
        after = [v for v in self._managed if cursor is None or v > cursor]
        batch = after[:limit]
        return batch, (batch[-1] if batch else None)

    def select_vins(
        self, *, cursor: str | None, limit: int
    ) -> tuple[list[str], str | None]:
        """Return up to *limit* population VINs starting after *cursor*."""
        self.population_calls.append({"cursor": cursor, "limit": limit})
        after = [v for v in self._population if cursor is None or v > cursor]
        batch = after[:limit]
        return batch, (batch[-1] if batch else None)


class _FakeCursorStore:
    """In-memory cursor store for the integration test."""

    def __init__(self) -> None:
        self._items: dict[str, dict] = {}

    def get_cursor(self, component: str) -> dict | None:
        return self._items.get(component)

    def put_cursor(self, component: str, item: dict) -> None:
        self._items[component] = item


class TestRealBatchPath:
    """Integration tests through resolve_batch with managed-first selection.

    Each test uses _FakeManagedSource (exposing both methods) and
    _FakeCursorStore.  The tests fail if the managed-first branch is dropped
    from resolve_batch (mutation guard).

    C2 fix: rotation and coverage tests here drive the real path
    (_resolve_batch_managed_first via resolve_batch), not the standalone
    advance_cursor / select_managed_first helpers.  Several of the tests
    run the fake source across consecutive nights so the cursor-advancement
    property is exercised on the real code path.
    """

    def test_managed_vins_come_first_in_resolve_batch(self):
        """resolve_batch returns managed VINs before population VINs.

        Mutation: drop the managed-first branch (don't call select_managed_vins)
        → result contains only population VINs → M1 not in result → fails.
        """
        from agents.tier2.selection import resolve_batch

        managed = ["MRDMGD000000000000", "MRDMGD000000000001"]
        population = ["MRDPOP000000000000", "MRDPOP000000000001", "MRDPOP000000000002"]

        source = _FakeManagedSource(managed_vins=managed, population_vins=population)
        store = _FakeCursorStore()

        result = resolve_batch(
            source=source,
            cursor_store=store,
            component="tires",
            limit=3,
        )

        # Managed VINs must appear first.
        assert result[:2] == managed, (
            "Managed VINs must precede population VINs in the result. "
            "Mutation: drop managed-first branch → only population VINs → fails."
        )
        # Cap is 3: 2 managed + 1 population.
        assert len(result) == 3
        assert result[2] == population[0]

    def test_managed_vins_selected_before_population_vins(self):
        """select_managed_vins is called and its VINs appear in the batch.

        Mutation: skip calling select_managed_vins → managed_calls is empty
        and no managed VIN is in the result → fails.
        """
        from agents.tier2.selection import resolve_batch

        managed = ["MRDMGD000000000000"]
        population = ["MRDPOP000000000000", "MRDPOP000000000001"]

        source = _FakeManagedSource(managed_vins=managed, population_vins=population)
        store = _FakeCursorStore()

        result = resolve_batch(
            source=source,
            cursor_store=store,
            component="battery",
            limit=2,
        )

        assert len(source.managed_calls) >= 1, (
            "select_managed_vins must be called at least once. "
            "Mutation: drop the call → managed_calls empty → fails."
        )
        assert managed[0] in result, (
            f"Managed VIN {managed[0]} must be in the result. "
            "Mutation: drop managed-first branch → not in result → fails."
        )

    def test_population_cursor_advances_only_on_population_vins(self):
        """The cursor stored after a run reflects the last population VIN, not managed.

        Mutation: advance the population cursor over managed VINs → stored
        cursor equals a managed VIN → fails.
        """
        from agents.tier2.selection import resolve_batch

        managed = ["MRDMGD000000000000", "MRDMGD000000000001"]
        population = ["MRDPOP000000000000", "MRDPOP000000000001"]

        source = _FakeManagedSource(managed_vins=managed, population_vins=population)
        store = _FakeCursorStore()

        resolve_batch(
            source=source,
            cursor_store=store,
            component="brakes",
            limit=4,  # 2 managed + 2 population
        )

        stored = store.get_cursor("brakes")
        assert stored is not None
        # The population cursor (last_vin) must equal the last population VIN.
        assert stored.get("last_vin") == population[-1], (
            "last_vin (population cursor) must equal the last population VIN. "
            "Mutation: advance cursor over managed VINs → stored cursor is managed → fails."
        )
        # The stored population cursor must NOT be a managed VIN.
        assert stored.get("last_vin") not in set(managed), (
            "Population cursor must not point to a managed VIN. "
            "Mutation: advance population cursor over managed VINs → fails."
        )

    def test_managed_vins_deduplicated_from_population_in_resolve_batch(self):
        """A VIN in both managed and population is not duplicated in the batch.

        Mutation: skip deduplication → overlap VIN appears twice → fails.
        """
        from agents.tier2.selection import resolve_batch

        overlap = "MRDXXX000000000000"
        managed = [overlap, "MRDMGD000000000001"]
        population = [overlap, "MRDPOP000000000000"]  # overlap present

        source = _FakeManagedSource(managed_vins=managed, population_vins=population)
        store = _FakeCursorStore()

        result = resolve_batch(
            source=source,
            cursor_store=store,
            component="tires",
            limit=10,
        )

        assert result.count(overlap) == 1, (
            f"VIN {overlap} appears in both pools; must appear exactly once in result. "
            "Mutation: skip deduplication → appears twice → fails."
        )

    # ------------------------------------------------------------------
    # C1 + C2: rotation and coverage tests over multiple nights
    # ------------------------------------------------------------------

    def test_every_run_contains_all_managed_vins_when_pool_fits_cap(self):
        """C1 case (a): when managed pool < cap, every run covers every managed VIN.

        With 4 managed VINs and cap 6, the pool fits in a single query.  After
        the C1 fix, the cursor stays None (no rotation needed) and every
        consecutive run returns all 4 managed VINs.

        Mutation A (drop population wrap-around): unrelated path → does not
        survive THIS test but is covered by test_population_wrap_advances_cursor.

        Mutation D (never store a managed cursor — W16 defect): the cursor
        stored is None, same as the fix, so this mutant does not affect case (a).
        The W16 mutant is caught by test_all_managed_vins_in_every_ceil_pass_when_pool_gt_cap.
        """
        from agents.tier2.selection import resolve_batch

        managed = [f"MRDMGD{i:012d}" for i in range(4)]  # 4 managed VINs
        population = [f"MRDPOP{i:012d}" for i in range(6)]
        cap = 6  # 4 managed + 2 population

        source = _FakeManagedSource(managed_vins=managed, population_vins=population)
        store = _FakeCursorStore()

        for night in range(3):
            result = resolve_batch(
                source=source, cursor_store=store, component="tires", limit=cap
            )
            managed_in_result = [v for v in result if v in set(managed)]
            assert set(managed_in_result) == set(managed), (
                f"Night {night + 1}: all 4 managed VINs must appear in every run "
                f"when pool ({len(managed)}) < cap ({cap}). "
                "C1 fix: cursor stays None when page is short, so every run "
                "starts from the beginning of the managed pool."
            )

    def test_no_run_has_zero_managed_vins_when_pool_gt_cap(self):
        """C1 case (b): when managed pool > cap, no run has zero managed VINs.

        With 10 managed VINs and cap 3, the pool spans ceil(10/3)=4 runs.
        The C1 fix wraps the tail of the managed pool so the final pass of
        a rotation does not produce an empty managed set.

        Mutation D (never store managed cursor = W16 defect): every run takes
        the same first 3 VINs, never advancing, and this test still passes.
        Caught by test_all_managed_vins_in_every_ceil_pass_when_pool_gt_cap.
        """
        from agents.tier2.selection import resolve_batch

        managed = [f"MRDMGD{i:012d}" for i in range(10)]
        cap = 3
        runs = 8  # two full rotation passes

        source = _FakeManagedSource(managed_vins=managed, population_vins=[])
        store = _FakeCursorStore()

        for night in range(runs):
            result = resolve_batch(
                source=source, cursor_store=store, component="battery", limit=cap
            )
            managed_count = sum(1 for v in result if v in set(managed))
            assert managed_count > 0, (
                f"Night {night + 1}: managed count must never be 0 when pool "
                f"({len(managed)}) > cap ({cap}). "
                "C1 fix: wrap-around fills remaining managed slots from the start "
                "of the pool when the tail of a rotation pass is reached."
            )

    def test_all_managed_vins_in_every_ceil_pass_when_pool_gt_cap(self):
        """C1/C2 case (b): all managed VINs appear in any ceil(n/cap) consecutive runs.

        With 10 managed VINs and cap 3, any window of ceil(10/3)=4 consecutive
        runs must collectively cover all 10 managed VINs.

        Mutation A (drop population wrap-around): population path, does not affect
        managed rotation → SURVIVED.
        Mutation D (never store managed cursor = W16 defect): every run takes the
        same first 3 VINs; the union after 4 runs has only 3, not 10 → CAUGHT.
        """
        import math
        from agents.tier2.selection import resolve_batch

        managed = [f"MRDMGD{i:012d}" for i in range(10)]
        cap = 3
        n = len(managed)
        window = math.ceil(n / cap)  # 4 runs per full pass

        source = _FakeManagedSource(managed_vins=managed, population_vins=[])
        store = _FakeCursorStore()

        # Run enough nights to cover two full passes (so we can check any window).
        total_runs = window * 2
        per_run: list[list[str]] = []
        for _ in range(total_runs):
            result = resolve_batch(
                source=source, cursor_store=store, component="battery", limit=cap
            )
            per_run.append(result)

        # Every sliding window of `window` runs must cover all managed VINs.
        for start in range(total_runs - window + 1):
            window_vins: set[str] = set()
            for run_result in per_run[start : start + window]:
                window_vins.update(run_result)
            managed_in_window = window_vins & set(managed)
            assert managed_in_window == set(managed), (
                f"Window [{start}..{start + window - 1}]: managed VINs covered = "
                f"{len(managed_in_window)}/{n}; expected all {n}. "
                "Mutation D (never store cursor): same 3 VINs every run → "
                "only 3 in any window → CAUGHT."
            )

    def test_managed_cursor_advances_only_when_page_is_full(self):
        """C1: managed cursor is stored only on a full page (len == limit).

        With 4 managed VINs and cap 6, the page is always short (4 < 6), so
        the managed cursor must stay None — no rotation needed.

        Mutation D: store cursor unconditionally → stored cursor is non-None
        on the first run → next run would get 0 managed VINs → CAUGHT by
        test_every_run_contains_all_managed_vins_when_pool_fits_cap.
        This test catches the intermediate state directly.
        """
        from agents.tier2.selection import resolve_batch

        managed = [f"MRDMGD{i:012d}" for i in range(4)]
        cap = 6

        source = _FakeManagedSource(managed_vins=managed, population_vins=[])
        store = _FakeCursorStore()

        resolve_batch(source=source, cursor_store=store, component="tires", limit=cap)

        stored = store.get_cursor("tires")
        assert stored is not None
        assert stored.get("managed_last_vin") is None, (
            "managed_last_vin must be None when the managed page is short "
            f"(len={len(managed)} < cap={cap}): no rotation cursor needed. "
            "Mutation: store cursor unconditionally → managed_last_vin is non-None → CAUGHT."
        )

    def test_managed_cursor_stored_on_full_page(self):
        """C1: managed cursor IS stored when the page is exactly full.

        With 10 managed VINs and cap 3, the first run fills the cap → cursor
        stored.  Mutation: never store cursor → managed_last_vin is None → CAUGHT.
        """
        from agents.tier2.selection import resolve_batch

        managed = [f"MRDMGD{i:012d}" for i in range(10)]
        cap = 3

        source = _FakeManagedSource(managed_vins=managed, population_vins=[])
        store = _FakeCursorStore()

        result = resolve_batch(source=source, cursor_store=store, component="battery", limit=cap)

        stored = store.get_cursor("battery")
        assert stored is not None
        assert stored.get("managed_last_vin") == result[cap - 1], (
            f"managed_last_vin must equal the last managed VIN in a full page "
            f"(expected {result[cap - 1]!r}). "
            "Mutation: never store cursor → None → CAUGHT."
        )

    def test_population_cursor_advances_across_multiple_nights(self):
        """C2/Mutation A: population wrap-around advances the cursor.

        Run the source over 12 nights with 6 population VINs and cap 3.
        After 2 nights the population has been fully covered twice.  The
        cursor must advance so night 3 starts from the beginning again.

        Mutation A (drop population wrap-around): pool_exhausted_count stays
        0 and the population cursor freezes at the last VIN; night 3+ returns
        an empty population batch → CAUGHT.
        """
        from agents.tier2.selection import resolve_batch

        managed = ["MRDMGD000000000000"]  # 1 managed (fits every run)
        population = [f"MRDPOP{i:012d}" for i in range(6)]
        cap = 3  # 1 managed + 2 population

        source = _FakeManagedSource(managed_vins=managed, population_vins=population)
        store = _FakeCursorStore()

        for night in range(6):
            result = resolve_batch(
                source=source, cursor_store=store, component="tires", limit=cap
            )
            pop_in_result = [v for v in result if v in set(population)]
            assert len(pop_in_result) > 0 or len(population) == 0, (
                f"Night {night + 1}: population VINs must appear in the result when "
                f"there are {len(population)} in the pool (cap remaining = {cap - 1}). "
                "Mutation A (drop wrap-around): population freezes after exhaustion → CAUGHT."
            )

    def test_population_exhausted_count_increments_on_wrap(self):
        """Mutation B: pool_exhausted_count increments exactly once on wrap.

        After 3 nights with 4 population VINs and cap=3 (nightly allocation 2),
        the pool wraps on night 3.  pool_exhausted_count must equal 1.

        Mutation B (don't increment on wrap): count stays 0 after wrap → CAUGHT.
        """
        from agents.tier2.selection import resolve_batch

        managed = ["MRDMGD000000000000"]  # 1 managed
        population = [f"MRDPOP{i:012d}" for i in range(4)]
        cap = 3  # 1 managed + 2 population per night

        source = _FakeManagedSource(managed_vins=managed, population_vins=population)
        store = _FakeCursorStore()

        # Night 1: pop[0], pop[1] → cursor at pop[1], count=0
        resolve_batch(source=source, cursor_store=store, component="tires", limit=cap)
        # Night 2: pop[2], pop[3] → cursor at pop[3], count=0
        resolve_batch(source=source, cursor_store=store, component="tires", limit=cap)
        # Night 3: pop exhausted → wrap → count increments to 1
        resolve_batch(source=source, cursor_store=store, component="tires", limit=cap)

        stored = store.get_cursor("tires")
        assert stored is not None
        count = stored.get("pool_exhausted_count", 0)
        assert count == 1, (
            f"pool_exhausted_count must be 1 after one pool wrap; got {count}. "
            "Mutation B (don't increment on wrap): count stays 0 → CAUGHT."
        )

    def test_population_cursor_not_set_to_last_managed_vin(self):
        """Mutation C: population cursor must not be set to the last managed VIN.

        When managed VINs fill the cap (cap == len(managed)), no population
        VINs are consumed, so the population cursor (last_vin) must stay
        unchanged (None if no prior population pass).

        Mutation C: set last_vin to last managed VIN when managed fills cap →
        population cursor skips all population VINs up to that managed VIN
        on the next run → CAUGHT.
        """
        from agents.tier2.selection import resolve_batch

        managed = ["MRDMGD000000000000", "MRDMGD000000000001", "MRDMGD000000000002"]
        population = ["MRDPOP000000000000", "MRDPOP000000000001"]  # lexicographically after managed
        cap = 3  # exactly fills the managed pool

        source = _FakeManagedSource(managed_vins=managed, population_vins=population)
        store = _FakeCursorStore()

        resolve_batch(source=source, cursor_store=store, component="brakes", limit=cap)

        stored = store.get_cursor("brakes")
        assert stored is not None
        # Population cursor must be None (no population VINs consumed).
        assert stored.get("last_vin") is None, (
            "last_vin (population cursor) must be None when managed fills the cap "
            "and no population VINs were consumed. "
            "Mutation C: set last_vin = last managed VIN → population query skips "
            "all earlier population VINs → CAUGHT."
        )

    def test_managed_source_error_falls_back_to_population(self):
        """W1: a managed source error must not abort the run; population VINs returned.

        Mutation: let the error propagate → resolve_batch raises → CAUGHT.
        """
        from agents.tier2.selection import resolve_batch

        class _ErrorManagedSource:
            """Fake source whose select_managed_vins always raises."""

            def select_managed_vins(self, *, cursor, limit):
                raise RuntimeError("Athena AccessDenied on managed query")

            def select_vins(self, *, cursor, limit):
                population = [f"MRDPOP{i:012d}" for i in range(5)]
                after = [v for v in population if cursor is None or v > cursor]
                batch = after[:limit]
                return batch, (batch[-1] if batch else None)

        source = _ErrorManagedSource()
        store = _FakeCursorStore()

        # Must not raise; must return population VINs.
        result = resolve_batch(source=source, cursor_store=store, component="tires", limit=4)

        assert len(result) > 0, (
            "A managed-query error must fall back to population-only selection; "
            "the result must not be empty when population VINs exist. "
            "Mutation: let the error propagate → raises → CAUGHT."
        )
        assert all(v.startswith("MRDPOP") for v in result), (
            "All VINs in the fallback result must be population VINs (no managed). "
            "Mutation: let error propagate → no result at all → CAUGHT."
        )

    def test_managed_cursor_preserved_on_managed_query_failure(self):
        """W3: when the managed query fails, the stored cursor is NOT reset to None.

        The docstring promises 'the managed cursor is NOT advanced, so the next run
        retries the managed query from the same position.'  Before the W3 fix, the
        code unconditionally set new_managed_cursor = None on any failure, restarting
        battery's rotation after a transient Athena error.

        Mutation (W3 defect): set managed_last_vin = None on failure →
        stored cursor is None → next run starts from the beginning → CAUGHT.
        """
        from agents.tier2.selection import resolve_batch

        # Pre-load a non-None managed cursor so the store starts mid-rotation.
        pre_cursor = "MRDMGD000000000005"

        class _ErrorOnFirstManagedSource:
            """Raises on the FIRST select_managed_vins call; succeeds on population."""

            def __init__(self):
                self._managed_calls = 0

            def select_managed_vins(self, *, cursor, limit):
                self._managed_calls += 1
                raise RuntimeError("Athena transient error")

            def select_vins(self, *, cursor, limit):
                pop = [f"MRDPOP{i:012d}" for i in range(5)]
                after = [v for v in pop if cursor is None or v > cursor]
                batch = after[:limit]
                return batch, (batch[-1] if batch else None)

        source = _ErrorOnFirstManagedSource()
        store = _FakeCursorStore()
        # Seed the store with a pre-existing managed cursor.
        store.put_cursor("battery", {
            "pk": "CURSOR#battery", "sk": "STATE",
            "last_vin": None,
            "managed_last_vin": pre_cursor,
            "pool_exhausted_count": 0,
            "updated_at": "2026-10-08T00:00:00Z",
        })

        result = resolve_batch(source=source, cursor_store=store, component="battery", limit=4)

        stored = store.get_cursor("battery")
        assert stored is not None
        # W3 fix: managed cursor must be preserved on failure.
        assert stored.get("managed_last_vin") == pre_cursor, (
            f"managed_last_vin must equal the pre-existing cursor {pre_cursor!r} "
            "after a managed query failure (W3: preserve on failure). "
            "Mutation: reset to None on failure → managed_last_vin is None → CAUGHT."
        )

    def test_managed_select_errors_counted_in_run_stats(self):
        """W3: managed_select_errors is incremented in run_stats when the managed query fails.

        Mutation: don't increment run_stats → count stays 0 → CAUGHT.
        """
        from agents.tier2.selection import resolve_batch

        class _ErrorManagedSource2:
            def select_managed_vins(self, *, cursor, limit):
                raise RuntimeError("AccessDenied on managed query")

            def select_vins(self, *, cursor, limit):
                pop = [f"MRDPOP{i:012d}" for i in range(3)]
                after = [v for v in pop if cursor is None or v > cursor]
                batch = after[:limit]
                return batch, (batch[-1] if batch else None)

        source = _ErrorManagedSource2()
        store = _FakeCursorStore()
        run_stats: dict = {}

        resolve_batch(source=source, cursor_store=store, component="tires",
                      limit=3, run_stats=run_stats)

        assert run_stats.get("managed_select_errors", 0) == 1, (
            "managed_select_errors must be 1 after a single managed-query failure. "
            "Mutation: don't increment → count stays 0 → CAUGHT."
        )

    def test_wrap_query_failure_does_not_abort_run(self):
        """W3: when the wrap-around managed query fails, the run completes (no abort).

        The second select_managed_vins call (wrap) can fail for the same reasons
        as the first.  The try/except around it must catch the error, log it, and
        continue with whatever managed VINs were already collected.

        Mutation: remove the try/except → wrap failure propagates → run aborts → CAUGHT.
        """
        from agents.tier2.selection import resolve_batch

        call_count = [0]

        class _FailOnWrapSource:
            """First select_managed_vins returns partial page; second (wrap) raises."""

            def select_managed_vins(self, *, cursor, limit):
                call_count[0] += 1
                if call_count[0] == 1:
                    # Short page from a non-None cursor (pool tail reached).
                    # A cursor of None would also work, but non-None triggers the wrap.
                    partial = [f"MRDMGD{i:012d}" for i in range(2)]
                    return partial, (partial[-1] if partial else None)
                # Second call (wrap-around) raises.
                raise RuntimeError("Athena wrap query failed")

            def select_vins(self, *, cursor, limit):
                pop = [f"MRDPOP{i:012d}" for i in range(3)]
                after = [v for v in pop if cursor is None or v > cursor]
                batch = after[:limit]
                return batch, (batch[-1] if batch else None)

        source = _FailOnWrapSource()
        store = _FakeCursorStore()
        # Pre-seed a non-None managed cursor to trigger the wrap branch.
        store.put_cursor("tires", {
            "pk": "CURSOR#tires", "sk": "STATE",
            "last_vin": None,
            "managed_last_vin": "MRDMGD000000000010",  # non-None → wrap branch
            "pool_exhausted_count": 0,
            "updated_at": "2026-10-08T00:00:00Z",
        })

        # Must not raise — the run continues with whatever managed VINs were collected.
        result = resolve_batch(source=source, cursor_store=store, component="tires", limit=4)
        assert isinstance(result, list), (
            "resolve_batch must return a list even when the wrap query fails. "
            "Mutation: remove the try/except → wrap failure propagates → raises → CAUGHT."
        )

    def test_all_managed_vins_in_every_ceil_pass_n_equals_2x_cap(self):
        """W1: n = 2×cap — all managed VINs appear in any 2-run window.

        The k×cap defect: when pool = k×cap, the last full page ends on the last
        VIN.  The next run (after the wrap) re-takes the same first cap VINs because
        the cursor was unconditionally reset to None.  With the W1 fix, the cursor is
        set from the combined set, so the next run advances past the wrap batch.

        Mutation (W1 defect): always reset cursor to None after wrap →
        every other window misses a batch → CAUGHT.
        """
        import math
        from agents.tier2.selection import resolve_batch

        cap = 3
        n = cap * 2  # exactly 6 managed VINs, 2 full pages
        managed = [f"MRDMGD{i:012d}" for i in range(n)]

        source = _FakeManagedSource(managed_vins=managed, population_vins=[])
        store = _FakeCursorStore()

        window = math.ceil(n / cap)  # 2 runs per full pass
        per_run: list[list[str]] = []
        for _ in range(window * 3):  # run 3 full passes
            result = resolve_batch(
                source=source, cursor_store=store, component="battery", limit=cap
            )
            per_run.append(result)

        # Every sliding window of `window` runs must cover all managed VINs.
        for start in range(len(per_run) - window + 1):
            window_vins: set[str] = set()
            for run_result in per_run[start : start + window]:
                window_vins.update(run_result)
            managed_in_window = window_vins & set(managed)
            assert managed_in_window == set(managed), (
                f"Window [{start}..{start + window - 1}]: managed VINs covered = "
                f"{len(managed_in_window)}/{n}; expected all {n}. "
                "W1 mutant (reset cursor to None after wrap): same first batch every "
                "other window → only cap VINs in window → CAUGHT."
            )

    def test_all_managed_vins_in_every_ceil_pass_n_equals_cap(self):
        """W1: n == cap — single batch covers the entire pool; the next run repeats it.

        When pool == cap, every run should return all managed VINs (pool fits in cap).
        The W1 defect would cause the first run to return all, reset cursor to None,
        and the second run would correctly start from the beginning.

        This test is primarily a regression guard: pool == cap must not cause any run
        to miss managed VINs (the n = k×cap class).

        Mutation: set cursor to managed_vins[-1] when pool == cap → next run returns
        empty managed page → one night has zero managed VINs → CAUGHT by
        test_no_run_has_zero_managed_vins_when_pool_gt_cap analogue.
        """
        from agents.tier2.selection import resolve_batch

        cap = 5
        managed = [f"MRDMGD{i:012d}" for i in range(cap)]  # exactly cap VINs

        source = _FakeManagedSource(managed_vins=managed, population_vins=[])
        store = _FakeCursorStore()

        for night in range(4):
            result = resolve_batch(
                source=source, cursor_store=store, component="tires", limit=cap
            )
            assert set(result) == set(managed), (
                f"Night {night + 1}: when pool ({cap}) == cap ({cap}), all managed "
                "VINs must appear in every run. "
                "Mutation: set cursor to last VIN → next run sees empty page → 0 managed → CAUGHT."
            )

    def test_interleaved_population_cursor_never_jumps_managed(self):
        """W2 D3 semantic 2: population cursor is preserved after a population wrap.

        G3F3.3 (a2 W4): the previous version used cap=4 with 3 managed VINs, giving
        only 1 population VIN per night.  On a wrap night with one population VIN,
        first == last, so storing raw_pop_restart[0] (the first) instead of the last
        VIN survives — the two values are identical.  No wrap page with 2+ VINs was
        exercised, so D3 semantic 2 was never tested for a multi-VIN wrap page.

        This version uses cap=5 (3 managed + 2 population per night) with 5 population
        VINs, so the wrap page holds exactly 2 VINs.  Storing the first VIN as the
        cursor after wrap means the next night re-takes VIN 2 from the wrap page,
        which appears both on the wrap night and on the following night — a repeat
        within the new pass.

        Setup: managed VINs interleave with population VINs in sort order (so a cursor
        taken from a managed VIN would skip population VINs between them).  Run enough
        nights to complete at least 2 passes.  Assert that:
        1. Within each completed pass, every population VIN appears exactly once
           (no repeats — catches 'first-VIN cursor on wrap').
        2. The stored last_vin on the wrap night equals that night's last population
           VIN, not the first (D3 semantic 2 — catches 'clear cursor after wrap' too).
        3. No population VIN is skipped across all nights (catches 'cursor from managed
           VINs').

        A pass boundary is where pool_exhausted_count increments (the wrap night is
        the first night of the new pass, not the last of the old one).  The mutation
        causes a repeat in pass 2 (the second night of pass 2 re-takes a wrap-page
        VIN), so the no-repeat check on pass 2 catches it.
        """
        import math
        from agents.tier2.selection import resolve_batch

        # 3 managed VINs and 5 population VINs that interleave in sort order.
        # cap=5 → 2 population VINs per night.  With 5 population VINs the pool
        # exhausts on the third population-only night; the wrap page has 2 VINs,
        # exercising the first-vs-last cursor distinction.
        managed = ["MRDBBB000000000001", "MRDBBB000000000003", "MRDBBB000000000005"]
        population = [
            "MRDAAA000000000001",  # before all managed
            "MRDBBB000000000002",  # between managed[0] and managed[1]
            "MRDBBB000000000004",  # between managed[1] and managed[2]
            "MRDCCC000000000001",  # after all managed
            "MRDCCC000000000002",  # after all managed (2nd)
        ]
        cap = 5  # 3 managed + 2 population per night

        source = _FakeManagedSource(managed_vins=managed, population_vins=population)
        store = _FakeCursorStore()

        # Run enough nights for at least 2 complete passes (each pass covers all
        # population VINs once), plus a few extra to catch cursor drift.
        pop_per_night = cap - len(managed)  # = 2
        nights_per_pass = math.ceil(len(population) / pop_per_night)  # = 3
        total_nights = nights_per_pass * 2 + nights_per_pass + 2  # 3 passes + margin

        pop_seen_all: list[str] = []   # every population VIN returned, in order
        # pass_vins_list[i] = all population VINs returned during pass i
        # Pass boundary: pool_exhausted_count increments → the night is the FIRST of
        # the new pass (the wrap page).
        pass_vins_list: list[list[str]] = [[]]   # start with pass 0 (pre-first-wrap)
        last_exhaust_count = 0
        wrap_nights: list[int] = []

        for night in range(total_nights):
            result = resolve_batch(
                source=source, cursor_store=store, component="tires", limit=cap
            )
            pop_in_run = [v for v in result if v in set(population)]
            stored = store.get_cursor("tires") or {}

            stored_count = stored.get("pool_exhausted_count", 0)
            is_wrap_night = stored_count > last_exhaust_count

            if is_wrap_night:
                # Wrap night = first night of the new pass.  The OLD pass (nights
                # before this one) is now complete; check it for no-repeats.
                old_pass_idx = len(pass_vins_list) - 1
                old_pass = pass_vins_list[old_pass_idx]
                if old_pass:   # skip the pre-first-wrap pass (may be an incomplete scan)
                    seen_counts: dict[str, int] = {}
                    for pv in old_pass:
                        seen_counts[pv] = seen_counts.get(pv, 0) + 1
                    repeats = [pv for pv, n in seen_counts.items() if n > 1]
                    assert not repeats, (
                        f"Pass {old_pass_idx} (nights before wrap {len(wrap_nights) + 1}): "
                        f"population VINs {repeats!r} appeared more than once. "
                        "Assertion 1 (no repeats within a pass) FAILS. "
                        "Mutation (first-VIN cursor on wrap): wrap-page VIN 2 is "
                        "re-taken on the next night → repeat in pass "
                        f"{old_pass_idx + 1} → CAUGHT."
                    )

                # The stored population cursor on the wrap night must equal the LAST
                # population VIN returned on this night (D3 semantic 2).
                last_vin_stored = stored.get("last_vin")
                if pop_in_run:
                    assert last_vin_stored == pop_in_run[-1], (
                        f"Night {night + 1} (wrap night {len(wrap_nights) + 1}): "
                        f"stored last_vin {last_vin_stored!r} must equal last "
                        f"population VIN {pop_in_run[-1]!r}, not the first "
                        f"({pop_in_run[0]!r}). "
                        "Assertion 2 (last-VIN cursor) FAILS. "
                        "Mutation (first-VIN cursor): stored = pop_in_run[0] → "
                        "next run starts after first VIN → second VIN repeats → "
                        "CAUGHT (by assertion 1 in the following pass)."
                    )

                # Start a new pass with this night's VINs.
                pass_vins_list.append(list(pop_in_run))
                wrap_nights.append(night)
                last_exhaust_count = stored_count
            else:
                # Continuation night — add to current pass.
                pass_vins_list[-1].extend(pop_in_run)

            pop_seen_all.extend(pop_in_run)

        assert len(wrap_nights) >= 2, (
            f"Expected at least 2 population wraps in {total_nights} nights; "
            f"got {len(wrap_nights)}.  Check cap > len(managed)."
        )

        # Every population VIN must appear in the overall recorded history.
        for pv in population:
            assert pv in set(pop_seen_all), (
                f"Population VIN {pv!r} never appeared in {total_nights} nights. "
                "Assertion 3 (no skipped VINs) FAILS. "
                "Mutation (cursor from managed VINs): cursor jumps past interleaved "
                "population VINs → some never seen → CAUGHT."
            )


# ============================================================================
# SQL tests per source — verify managed query matches pool predicate and cursor
# ============================================================================

class TestTireSourceManagedSQL:
    """The tire source's managed-VIN query must match the pool predicate and have
    a cursor clause.

    W4 fix: assert the SAME pool predicate as _build_select_vins_sql, not just
    the tier filter.  Mutants E (drop needs_replacement) and G (drop cursor
    clause) must be caught.
    """

    def test_tire_managed_sql_uses_managed_vehicles_table(self):
        """_build_select_managed_vins_sql includes adp_{stage}_fleet_recall.managed_vehicles.

        Mutation: remove the JOIN or change the table name → assertion fails.
        """
        from agents.health.adp_tire_source import AdpTireSource

        source = AdpTireSource(
            stage="staging",
            athena_client=None,
            workgroup="test-wg",
            results_s3_path="s3://test-bucket/",
        )
        sql = source._build_select_managed_vins_sql(cursor=None, limit=10)
        assert "adp_staging_fleet_recall.managed_vehicles" in sql, (
            "Tire managed SQL must reference adp_{stage}_fleet_recall.managed_vehicles. "
            "Mutation: remove or rename the table → fails."
        )

    def test_tire_managed_sql_filters_tier_managed(self):
        """_build_select_managed_vins_sql uses newest-snapshot filter, not tier = 'managed'.

        Mutation: revert to tier = 'managed' → assertion fails.
        """
        from agents.health.adp_tire_source import AdpTireSource

        source = AdpTireSource(
            stage="staging",
            athena_client=None,
            workgroup="test-wg",
            results_s3_path="s3://test-bucket/",
        )
        sql = source._build_select_managed_vins_sql(cursor=None, limit=10)
        assert "snapshot_date" in sql, (
            "Tire managed SQL must filter by snapshot_date (newest snapshot). "
            "Mutation: revert to tier = 'managed' → fails."
        )
        assert "MAX(snapshot_date)" in sql, (
            "Tire managed SQL must select MAX(snapshot_date) for newest snapshot."
        )

    def test_tire_managed_sql_same_pool_predicate_as_population_sql(self):
        """Managed and population SQLs share the same pool predicate via _pool_predicate.

        W5 fix: both builders call _pool_predicate so they cannot drift.
        Mutant E: drop needs_replacement from the managed SQL → managed set grows to
        every VIN in vin_cms_fleet with any tire record → CAUGHT.
        """
        from agents.health.adp_tire_source import AdpTireSource

        source = AdpTireSource(
            stage="staging",
            athena_client=None,
            workgroup="test-wg",
            results_s3_path="s3://test-bucket/",
        )
        managed_sql = source._build_select_managed_vins_sql(cursor=None, limit=10)
        population_sql = source._build_select_vins_sql(cursor=None, limit=10)

        # Both SQLs must embed _pool_predicate's exact text (with their respective alias).
        assert source._pool_predicate("th.") in managed_sql, (
            f"Tire managed SQL must contain _pool_predicate('th.') = "
            f"{source._pool_predicate('th.')!r}. "
            "Mutant E: drop needs_replacement → managed set grows → CAUGHT."
        )
        assert source._pool_predicate("") in population_sql, (
            f"Tire population SQL must contain _pool_predicate('') = "
            f"{source._pool_predicate('')!r}. "
            "Both must use the same predicate so they cannot drift."
        )

    def test_tire_managed_sql_cursor_clause_when_cursor_given(self):
        """A non-None cursor produces vin > '<cursor>' in the managed SQL.

        W4 / Mutant G: drop the cursor clause from managed SQL → same first page
        returned on every run regardless of cursor; rotation has no effect → CAUGHT.
        """
        from agents.health.adp_tire_source import AdpTireSource

        source = AdpTireSource(
            stage="staging",
            athena_client=None,
            workgroup="test-wg",
            results_s3_path="s3://test-bucket/",
        )
        cursor_vin = "VIN000000000000001"
        sql = source._build_select_managed_vins_sql(cursor=cursor_vin, limit=10)
        assert f"vin > '{cursor_vin}'" in sql, (
            f"Tire managed SQL with cursor must contain \"vin > '{cursor_vin}'\". "
            "Mutant G: drop cursor clause → same first page every run → CAUGHT."
        )

    def test_tire_managed_sql_no_cursor_clause_when_cursor_none(self):
        """With cursor=None there is no vin > ... clause in the managed SQL."""
        from agents.health.adp_tire_source import AdpTireSource

        source = AdpTireSource(
            stage="staging",
            athena_client=None,
            workgroup="test-wg",
            results_s3_path="s3://test-bucket/",
        )
        sql = source._build_select_managed_vins_sql(cursor=None, limit=10)
        assert "vin >" not in sql, (
            "Tire managed SQL with cursor=None must not contain a vin > clause."
        )

    def test_tire_managed_sql_uses_stage_param(self):
        """Stage parameter is substituted correctly in the managed SQL."""
        from agents.health.adp_tire_source import AdpTireSource

        source = AdpTireSource(
            stage="prod",
            athena_client=None,
            workgroup="test-wg",
            results_s3_path="s3://test-bucket/",
        )
        sql = source._build_select_managed_vins_sql(cursor=None, limit=5)
        assert "adp_prod_fleet_recall.managed_vehicles" in sql
        assert "adp_prod_tire_health.tire_health" in sql


class TestBrakeSourceManagedSQL:
    """The brake source's managed-VIN query must match the pool predicate and have
    a cursor clause.

    W4 fix: assert the SAME pool predicate as _build_select_vins_sql.
    Mutant F (drop outcome IN) and cursor clauses must be caught.
    """

    def test_brake_managed_sql_uses_managed_vehicles_table(self):
        """_build_select_managed_vins_sql includes adp_{stage}_fleet_recall.managed_vehicles."""
        from agents.health.adp_brake_source import AdpBrakeSource

        source = AdpBrakeSource(
            stage="staging",
            athena_client=None,
            workgroup="test-wg",
            results_s3_path="s3://test-bucket/",
        )
        sql = source._build_select_managed_vins_sql(cursor=None, limit=10)
        assert "adp_staging_fleet_recall.managed_vehicles" in sql, (
            "Brake managed SQL must reference adp_{stage}_fleet_recall.managed_vehicles. "
            "Mutation: remove or rename the table → fails."
        )

    def test_brake_managed_sql_filters_tier_managed(self):
        """_build_select_managed_vins_sql uses newest-snapshot filter, not tier = 'managed'."""
        from agents.health.adp_brake_source import AdpBrakeSource

        source = AdpBrakeSource(
            stage="staging",
            athena_client=None,
            workgroup="test-wg",
            results_s3_path="s3://test-bucket/",
        )
        sql = source._build_select_managed_vins_sql(cursor=None, limit=10)
        assert "snapshot_date" in sql, (
            "Brake managed SQL must filter by snapshot_date (newest snapshot). "
            "Mutation: revert to tier = 'managed' → fails."
        )
        assert "MAX(snapshot_date)" in sql, (
            "Brake managed SQL must select MAX(snapshot_date) for newest snapshot."
        )

    def test_brake_managed_sql_same_pool_predicate_as_population_sql(self):
        """Managed and population SQLs share the same pool predicate via _pool_predicate.

        W5 fix: both builders call _pool_predicate("sr.") / _pool_predicate("") so
        service_type = 'brake_service' AND outcome IN (...) cannot drift between them.
        Dropping service_type from the managed SQL survives before this test; after,
        it is caught because _pool_predicate("sr.") is no longer present.
        """
        from agents.health.adp_brake_source import AdpBrakeSource

        source = AdpBrakeSource(
            stage="staging",
            athena_client=None,
            workgroup="test-wg",
            results_s3_path="s3://test-bucket/",
        )
        managed_sql = source._build_select_managed_vins_sql(cursor=None, limit=10)
        population_sql = source._build_select_vins_sql(cursor=None, limit=10)

        # Both SQLs must embed _pool_predicate's exact text (with their respective alias).
        assert source._pool_predicate("sr.") in managed_sql, (
            f"Brake managed SQL must contain _pool_predicate('sr.') = "
            f"{source._pool_predicate('sr.')!r}. "
            "W5 mutant: drop service_type from managed SQL → managed set grows → CAUGHT."
        )
        assert source._pool_predicate("") in population_sql, (
            f"Brake population SQL must contain _pool_predicate('') = "
            f"{source._pool_predicate('')!r}. "
            "Both must use the same predicate so they cannot drift."
        )

    def test_brake_managed_sql_cursor_clause_when_cursor_given(self):
        """A non-None cursor produces vin > '<cursor>' in the brake managed SQL.

        W4: assert cursor clause in managed SQL mirrors population SQL.
        """
        from agents.health.adp_brake_source import AdpBrakeSource

        source = AdpBrakeSource(
            stage="staging",
            athena_client=None,
            workgroup="test-wg",
            results_s3_path="s3://test-bucket/",
        )
        cursor_vin = "VIN000000000000001"
        sql = source._build_select_managed_vins_sql(cursor=cursor_vin, limit=10)
        assert f"vin > '{cursor_vin}'" in sql, (
            f"Brake managed SQL with cursor must contain \"vin > '{cursor_vin}'\". "
            "Without it, every run returns the same first page regardless of rotation."
        )

    def test_brake_managed_sql_no_cursor_clause_when_cursor_none(self):
        """With cursor=None there is no vin > ... clause in the brake managed SQL."""
        from agents.health.adp_brake_source import AdpBrakeSource

        source = AdpBrakeSource(
            stage="staging",
            athena_client=None,
            workgroup="test-wg",
            results_s3_path="s3://test-bucket/",
        )
        sql = source._build_select_managed_vins_sql(cursor=None, limit=10)
        assert "vin >" not in sql, (
            "Brake managed SQL with cursor=None must not contain a vin > clause."
        )

    def test_brake_managed_sql_uses_stage_param(self):
        """Stage parameter is substituted correctly in the brake managed SQL."""
        from agents.health.adp_brake_source import AdpBrakeSource

        source = AdpBrakeSource(
            stage="prod",
            athena_client=None,
            workgroup="test-wg",
            results_s3_path="s3://test-bucket/",
        )
        sql = source._build_select_managed_vins_sql(cursor=None, limit=5)
        assert "adp_prod_fleet_recall.managed_vehicles" in sql
        assert "adp_prod_service_records.service_records" in sql


class TestBatterySourceManagedSQL:
    """The battery source's managed-VIN query must have the cursor clause.

    Battery has no pool predicate (rotation-only), but the cursor clause is
    critical for rotation correctness.
    W4: assert cursor clause present.
    """

    def test_battery_managed_sql_uses_managed_vehicles_table(self):
        """_build_select_managed_vins_sql includes adp_{stage}_fleet_recall.managed_vehicles."""
        from agents.health.adp_battery_source import AdpBatterySource

        source = AdpBatterySource(
            stage="staging",
            athena_client=None,
            workgroup="test-wg",
            results_s3_path="s3://test-bucket/",
        )
        sql = source._build_select_managed_vins_sql(cursor=None, limit=10)
        assert "adp_staging_fleet_recall.managed_vehicles" in sql, (
            "Battery managed SQL must reference adp_{stage}_fleet_recall.managed_vehicles. "
            "Mutation: remove or rename the table → fails."
        )

    def test_battery_managed_sql_filters_tier_managed(self):
        """_build_select_managed_vins_sql uses newest-snapshot filter, not tier = 'managed'."""
        from agents.health.adp_battery_source import AdpBatterySource

        source = AdpBatterySource(
            stage="staging",
            athena_client=None,
            workgroup="test-wg",
            results_s3_path="s3://test-bucket/",
        )
        sql = source._build_select_managed_vins_sql(cursor=None, limit=10)
        assert "snapshot_date" in sql, (
            "Battery managed SQL must filter by snapshot_date (newest snapshot). "
            "Mutation: revert to tier = 'managed' → fails."
        )
        assert "MAX(snapshot_date)" in sql, (
            "Battery managed SQL must select MAX(snapshot_date) for newest snapshot."
        )

    def test_battery_managed_sql_cursor_clause_when_cursor_given(self):
        """A non-None cursor produces vin > '<cursor>' in the battery managed SQL.

        W4 / Mutant G (analogous): drop cursor clause → same first 600 VINs
        every run for battery (4,143 managed); rotation never advances → CAUGHT.
        """
        from agents.health.adp_battery_source import AdpBatterySource

        source = AdpBatterySource(
            stage="staging",
            athena_client=None,
            workgroup="test-wg",
            results_s3_path="s3://test-bucket/",
        )
        cursor_vin = "VIN000000000000001"
        sql = source._build_select_managed_vins_sql(cursor=cursor_vin, limit=10)
        assert f"vin > '{cursor_vin}'" in sql, (
            f"Battery managed SQL with cursor must contain \"vin > '{cursor_vin}'\". "
            "Without it, every run returns the same first page (no rotation advancement)."
        )

    def test_battery_managed_sql_no_cursor_clause_when_cursor_none(self):
        """With cursor=None there is no vin > ... clause in the battery managed SQL."""
        from agents.health.adp_battery_source import AdpBatterySource

        source = AdpBatterySource(
            stage="staging",
            athena_client=None,
            workgroup="test-wg",
            results_s3_path="s3://test-bucket/",
        )
        sql = source._build_select_managed_vins_sql(cursor=None, limit=10)
        assert "vin >" not in sql, (
            "Battery managed SQL with cursor=None must not contain a vin > clause."
        )

    def test_battery_managed_sql_uses_stage_param(self):
        """Stage parameter is substituted correctly in the battery managed SQL."""
        from agents.health.adp_battery_source import AdpBatterySource

        source = AdpBatterySource(
            stage="prod",
            athena_client=None,
            workgroup="test-wg",
            results_s3_path="s3://test-bucket/",
        )
        sql = source._build_select_managed_vins_sql(cursor=None, limit=5)
        assert "adp_prod_fleet_recall.managed_vehicles" in sql
        assert "adp_prod_energy_usage.energy_usage" in sql

    def test_limit_zero_returns_empty_without_indexerror(self):
        """S1: resolve_batch with limit=0 must return [] without raising IndexError.

        Before the S1 fix, a managed source that returns an empty list triggered
        managed_vins[-1] outside the try block, raising IndexError and aborting the run.

        Mutation: remove the limit<=0 guard → IndexError aborts the run → CAUGHT.
        """
        from agents.tier2.selection import resolve_batch

        class _AnyManagedSource:
            def select_managed_vins(self, *, cursor, limit):
                return [], None

            def select_vins(self, *, cursor, limit):
                return [], None

        source = _AnyManagedSource()
        store = _FakeCursorStore()

        # Must not raise; must return an empty list.
        result = resolve_batch(source=source, cursor_store=store, component="tires", limit=0)
        assert result == [], (
            "resolve_batch with limit=0 must return [] without raising IndexError. "
            "Mutation: remove limit<=0 guard → IndexError → CAUGHT."
        )



# ===========================================================================
# G3F6F1.5 Part 2B: snapshot freshness in select_managed_vins
# ===========================================================================
#
# For each of the three sources (battery, brake, tire):
#   A. Stale managed_vehicles → select_managed_vins raises StaleSnapshotError;
#      through resolve_batch the run still completes on population VINs and
#      managed_select_errors == 1.
#   B. Fresh → unchanged behaviour (managed VINs returned, no raise).
#
# We control the freshness outcome by patching `_require_fresh_snap` in the
# source module directly (raise for stale, no-op for fresh), avoiding any
# dependency on the real clock.

import datetime as _dt

from agents.tier2.snapshot_freshness import SnapshotState, StaleSnapshotError

_MANAGED_VINS_B = [f"MRDMGD{i:012d}" for i in range(3)]
_POP_VINS_B     = [f"MRDPOP{i:012d}" for i in range(3)]
_ANY_DATES      = {"managed_vehicles": SnapshotState(
    date=_dt.date(2026, 10, 10), row_count=None, table_date=_dt.date(2026, 10, 10)
)}


def _make_fake_query_b(vins: list[str]):
    """Stub for run_query returning each vin as a row dict."""
    def _q(sql, **kw):
        return [{"vin": v} for v in vins]
    return _q


def _stale_require_fresh(dates, *, today):
    raise StaleSnapshotError("stale managed_vehicles (test stub)", dates=dates)


def _fresh_require_fresh(dates, *, today):
    pass  # no-op: simulate fresh snapshot


def _resolve_b(*, source, run_stats):
    store = _FakeCursorStore()
    from agents.tier2.selection import resolve_batch
    return resolve_batch(
        source=source, cursor_store=store,
        component="tires", limit=3, run_stats=run_stats,
    )


class _PopOnlySource:
    """Wraps an inner source (for managed) + fixed population list."""

    def __init__(self, inner_src):
        self._inner = inner_src

    def select_managed_vins(self, *, cursor, limit):
        return self._inner.select_managed_vins(cursor=cursor, limit=limit)

    def select_vins(self, *, cursor, limit):
        after = [v for v in _POP_VINS_B if cursor is None or v > cursor]
        batch = after[:limit]
        return batch, (batch[-1] if batch else None)


class TestManagedFreshnessGuardBattery:
    """Battery: stale managed_vehicles → raises; through resolve_batch → population."""

    def _source(self, monkeypatch, require_fn):
        from agents.health import adp_battery_source as _mod
        from agents.health.adp_battery_source import AdpBatterySource
        monkeypatch.setattr(_mod, "_read_snap_dates", lambda *_a, **_kw: _ANY_DATES)
        monkeypatch.setattr(_mod, "_require_fresh_snap", require_fn)
        monkeypatch.setattr(_mod, "run_query", _make_fake_query_b(_MANAGED_VINS_B))
        return AdpBatterySource(
            "staging", athena_client=object(),
            workgroup="w", results_s3_path="s3://r/", poll_interval=0,
        )

    def test_stale_raises_stale_snapshot_error(self, monkeypatch):
        """Stale managed_vehicles → StaleSnapshotError raised from select_managed_vins."""
        src = self._source(monkeypatch, _stale_require_fresh)
        with pytest.raises(StaleSnapshotError):
            src.select_managed_vins(cursor=None, limit=5)

    def test_stale_through_resolve_batch_population_fallback(self, monkeypatch):
        """Stale → resolve_batch returns population VINs and managed_select_errors == 1."""
        src = self._source(monkeypatch, _stale_require_fresh)
        run_stats: dict = {}
        result = _resolve_b(source=_PopOnlySource(src), run_stats=run_stats)
        assert all(v.startswith("MRDPOP") for v in result), (
            "Fallback result must be population-only VINs. "
            "Mutation: error propagates → raises → CAUGHT."
        )
        assert run_stats.get("managed_select_errors", 0) == 1, (
            "managed_select_errors must equal 1 after stale managed query. "
            "Mutation: not counting → 0 → CAUGHT."
        )

    def test_fresh_managed_vins_returned(self, monkeypatch):
        """Fresh managed_vehicles → VINs returned unchanged."""
        src = self._source(monkeypatch, _fresh_require_fresh)
        vins, _ = src.select_managed_vins(cursor=None, limit=10)
        assert vins == _MANAGED_VINS_B, (
            "Fresh managed_vehicles must return VINs normally. "
            "Mutation: raising on fresh → empty result → CAUGHT."
        )

    def test_freshness_checked_only_once_per_instance(self, monkeypatch):
        """Freshness must be checked at most once per instance (not once per page)."""
        from agents.health import adp_battery_source as _mod
        call_count = [0]

        def _counting(dates, *, today):
            call_count[0] += 1

        from agents.health.adp_battery_source import AdpBatterySource
        monkeypatch.setattr(_mod, "_read_snap_dates", lambda *_a, **_kw: _ANY_DATES)
        monkeypatch.setattr(_mod, "_require_fresh_snap", _counting)
        monkeypatch.setattr(_mod, "run_query", _make_fake_query_b([]))
        src = AdpBatterySource(
            "staging", athena_client=object(),
            workgroup="w", results_s3_path="s3://r/", poll_interval=0,
        )
        src.select_managed_vins(cursor=None, limit=5)
        src.select_managed_vins(cursor=None, limit=5)
        assert call_count[0] == 1, (
            "Freshness must be checked exactly once per instance (cached). "
            "Mutation: checking on every call → count > 1 → CAUGHT."
        )

    def test_snap_dates_called_with_managed_vehicles_key(self, monkeypatch):
        """_read_snap_dates must be called with exactly ['managed_vehicles']."""
        from agents.health import adp_battery_source as _m
        from agents.health.adp_battery_source import AdpBatterySource
        ks: list[set] = []
        monkeypatch.setattr(
            _m, "_read_snap_dates",
            lambda stg, keys, **kw: ks.append(set(keys)) or _ANY_DATES,
        )
        monkeypatch.setattr(_m, "_require_fresh_snap", _fresh_require_fresh)
        monkeypatch.setattr(_m, "run_query", _make_fake_query_b(_MANAGED_VINS_B))
        AdpBatterySource(
            "staging", athena_client=object(), workgroup="w",
            results_s3_path="s3://r/", poll_interval=0,
        ).select_managed_vins(cursor=None, limit=5)
        assert ks and ks[0] == {"managed_vehicles"}, (
            f"_read_snap_dates must receive exactly ['managed_vehicles']; got {ks}. "
            "Mutation: pass extra/wrong keys → set differs → CAUGHT."
        )

    def test_real_require_fresh_yesterday_and_two_days_old(self, monkeypatch):
        """Without stubbing _require_fresh_snap: yesterday passes; two_ago → fallback."""
        import datetime as _d
        from agents.health import adp_battery_source as _m
        from agents.health.adp_battery_source import AdpBatterySource
        today = _d.datetime.now(_d.timezone.utc).date()
        monkeypatch.setattr(_m, "run_query", _make_fake_query_b(_MANAGED_VINS_B))

        # Case A: yesterday → managed VINs returned (real require_fresh not stubbed)
        monkeypatch.setattr(_m, "_read_snap_dates",
                            lambda *a, **kw: {"managed_vehicles": SnapshotState(
                                date=today - _d.timedelta(1), row_count=None,
                                table_date=today - _d.timedelta(1))})
        try:
            vins, _ = AdpBatterySource(
                "staging", athena_client=object(), workgroup="w",
                results_s3_path="s3://r/", poll_interval=0,
            ).select_managed_vins(cursor=None, limit=10)
        except StaleSnapshotError as _e:
            pytest.fail(f"yesterday must be fresh but got StaleSnapshotError: {_e}")
        assert vins == _MANAGED_VINS_B, (
            "Yesterday is fresh; managed VINs must be returned. "
            "Mutation (battery-bound today+30): gate refuses yesterday → StaleSnapshotError → CAUGHT."
        )

        # Case B: two days old → StaleSnapshotError → population fallback
        monkeypatch.setattr(_m, "_read_snap_dates",
                            lambda *a, **kw: {"managed_vehicles": SnapshotState(
                                date=today - _d.timedelta(2), row_count=None,
                                table_date=today - _d.timedelta(2))})
        rs: dict = {}
        result = _resolve_b(source=_PopOnlySource(
            AdpBatterySource("staging", athena_client=object(), workgroup="w",
                             results_s3_path="s3://r/", poll_interval=0)),
            run_stats=rs)
        assert all(v.startswith("MRDPOP") for v in result), (
            "Two-days-old snapshot is stale; must fall back to population VINs. "
            "Mutation: not raising on stale → managed VINs returned → CAUGHT."
        )
        assert rs.get("managed_select_errors", 0) == 1, (
            "managed_select_errors must be 1 after stale managed-query fallback. "
            "Mutation: not incrementing → 0 → CAUGHT."
        )


class TestManagedFreshnessGuardBrake:
    """Brake: stale managed_vehicles → raises; through resolve_batch → population."""

    def _source(self, monkeypatch, require_fn):
        from agents.health import adp_brake_source as _mod
        from agents.health.adp_brake_source import AdpBrakeSource
        monkeypatch.setattr(_mod, "_read_snap_dates", lambda *_a, **_kw: _ANY_DATES)
        monkeypatch.setattr(_mod, "_require_fresh_snap", require_fn)
        monkeypatch.setattr(_mod, "run_query", _make_fake_query_b(_MANAGED_VINS_B))
        return AdpBrakeSource(
            "staging", athena_client=object(),
            workgroup="w", results_s3_path="s3://r/", poll_interval=0,
        )

    def test_stale_raises_stale_snapshot_error(self, monkeypatch):
        src = self._source(monkeypatch, _stale_require_fresh)
        with pytest.raises(StaleSnapshotError):
            src.select_managed_vins(cursor=None, limit=5)

    def test_stale_through_resolve_batch_population_fallback(self, monkeypatch):
        src = self._source(monkeypatch, _stale_require_fresh)
        run_stats: dict = {}
        result = _resolve_b(source=_PopOnlySource(src), run_stats=run_stats)
        assert all(v.startswith("MRDPOP") for v in result)
        assert run_stats.get("managed_select_errors", 0) == 1

    def test_fresh_managed_vins_returned(self, monkeypatch):
        src = self._source(monkeypatch, _fresh_require_fresh)
        vins, _ = src.select_managed_vins(cursor=None, limit=10)
        assert vins == _MANAGED_VINS_B

    def test_snap_dates_called_with_managed_vehicles_key(self, monkeypatch):
        """_read_snap_dates must be called with exactly ['managed_vehicles']."""
        from agents.health import adp_brake_source as _m
        from agents.health.adp_brake_source import AdpBrakeSource
        ks: list[set] = []
        monkeypatch.setattr(
            _m, "_read_snap_dates",
            lambda stg, keys, **kw: ks.append(set(keys)) or _ANY_DATES,
        )
        monkeypatch.setattr(_m, "_require_fresh_snap", _fresh_require_fresh)
        monkeypatch.setattr(_m, "run_query", _make_fake_query_b(_MANAGED_VINS_B))
        AdpBrakeSource(
            "staging", athena_client=object(), workgroup="w",
            results_s3_path="s3://r/", poll_interval=0,
        ).select_managed_vins(cursor=None, limit=5)
        assert ks and ks[0] == {"managed_vehicles"}, (
            f"_read_snap_dates must receive exactly ['managed_vehicles']; got {ks}. "
            "Mutation: pass extra/wrong keys → set differs → CAUGHT."
        )

    def test_real_require_fresh_yesterday_and_two_days_old(self, monkeypatch):
        """Without stubbing _require_fresh_snap: yesterday passes; two_ago → fallback."""
        import datetime as _d
        from agents.health import adp_brake_source as _m
        from agents.health.adp_brake_source import AdpBrakeSource
        today = _d.datetime.now(_d.timezone.utc).date()
        monkeypatch.setattr(_m, "run_query", _make_fake_query_b(_MANAGED_VINS_B))

        monkeypatch.setattr(_m, "_read_snap_dates",
                            lambda *a, **kw: {"managed_vehicles": SnapshotState(
                                date=today - _d.timedelta(1), row_count=None,
                                table_date=today - _d.timedelta(1))})
        vins, _ = AdpBrakeSource(
            "staging", athena_client=object(), workgroup="w",
            results_s3_path="s3://r/", poll_interval=0,
        ).select_managed_vins(cursor=None, limit=10)
        assert vins == _MANAGED_VINS_B, (
            "Yesterday is fresh; managed VINs must be returned. "
            "Mutation: raise on yesterday → CAUGHT."
        )

        monkeypatch.setattr(_m, "_read_snap_dates",
                            lambda *a, **kw: {"managed_vehicles": SnapshotState(
                                date=today - _d.timedelta(2), row_count=None,
                                table_date=today - _d.timedelta(2))})
        rs: dict = {}
        result = _resolve_b(source=_PopOnlySource(
            AdpBrakeSource("staging", athena_client=object(), workgroup="w",
                           results_s3_path="s3://r/", poll_interval=0)),
            run_stats=rs)
        assert all(v.startswith("MRDPOP") for v in result), (
            "Two-days-old snapshot is stale; must fall back to population VINs. "
            "Mutation: not raising on stale → managed VINs returned → CAUGHT."
        )
        assert rs.get("managed_select_errors", 0) == 1, (
            "managed_select_errors must be 1 after stale fallback. "
            "Mutation: not incrementing → 0 → CAUGHT."
        )


class TestManagedFreshnessGuardTire:
    """Tire: stale managed_vehicles → raises; through resolve_batch → population."""

    def _source(self, monkeypatch, require_fn):
        from agents.health import adp_tire_source as _mod
        from agents.health.adp_tire_source import AdpTireSource
        monkeypatch.setattr(_mod, "_read_snap_dates", lambda *_a, **_kw: _ANY_DATES)
        monkeypatch.setattr(_mod, "_require_fresh_snap", require_fn)
        monkeypatch.setattr(_mod, "run_query", _make_fake_query_b(_MANAGED_VINS_B))
        return AdpTireSource(
            "staging", athena_client=object(),
            workgroup="w", results_s3_path="s3://r/", poll_interval=0,
        )

    def test_stale_raises_stale_snapshot_error(self, monkeypatch):
        src = self._source(monkeypatch, _stale_require_fresh)
        with pytest.raises(StaleSnapshotError):
            src.select_managed_vins(cursor=None, limit=5)

    def test_stale_through_resolve_batch_population_fallback(self, monkeypatch):
        src = self._source(monkeypatch, _stale_require_fresh)
        run_stats: dict = {}
        result = _resolve_b(source=_PopOnlySource(src), run_stats=run_stats)
        assert all(v.startswith("MRDPOP") for v in result)
        assert run_stats.get("managed_select_errors", 0) == 1

    def test_fresh_managed_vins_returned(self, monkeypatch):
        src = self._source(monkeypatch, _fresh_require_fresh)
        vins, _ = src.select_managed_vins(cursor=None, limit=10)
        assert vins == _MANAGED_VINS_B


    def test_snap_dates_called_with_managed_vehicles_key(self, monkeypatch):
        """_read_snap_dates must be called with exactly ['managed_vehicles']."""
        from agents.health import adp_tire_source as _m
        from agents.health.adp_tire_source import AdpTireSource
        ks: list[set] = []
        monkeypatch.setattr(
            _m, "_read_snap_dates",
            lambda stg, keys, **kw: ks.append(set(keys)) or _ANY_DATES,
        )
        monkeypatch.setattr(_m, "_require_fresh_snap", _fresh_require_fresh)
        monkeypatch.setattr(_m, "run_query", _make_fake_query_b(_MANAGED_VINS_B))
        AdpTireSource(
            "staging", athena_client=object(), workgroup="w",
            results_s3_path="s3://r/", poll_interval=0,
        ).select_managed_vins(cursor=None, limit=5)
        assert ks and ks[0] == {"managed_vehicles"}, (
            f"_read_snap_dates must receive exactly ['managed_vehicles']; got {ks}. "
            "Mutation: pass extra/wrong keys → set differs → CAUGHT."
        )

    def test_real_require_fresh_yesterday_and_two_days_old(self, monkeypatch):
        """Without stubbing _require_fresh_snap: yesterday passes; two_ago → fallback."""
        import datetime as _d
        from agents.health import adp_tire_source as _m
        from agents.health.adp_tire_source import AdpTireSource
        today = _d.datetime.now(_d.timezone.utc).date()
        monkeypatch.setattr(_m, "run_query", _make_fake_query_b(_MANAGED_VINS_B))

        monkeypatch.setattr(_m, "_read_snap_dates",
                            lambda *a, **kw: {"managed_vehicles": SnapshotState(
                                date=today - _d.timedelta(1), row_count=None,
                                table_date=today - _d.timedelta(1))})
        vins, _ = AdpTireSource(
            "staging", athena_client=object(), workgroup="w",
            results_s3_path="s3://r/", poll_interval=0,
        ).select_managed_vins(cursor=None, limit=10)
        assert vins == _MANAGED_VINS_B, (
            "Yesterday is fresh; managed VINs must be returned. "
            "Mutation: raise on yesterday → CAUGHT."
        )

        monkeypatch.setattr(_m, "_read_snap_dates",
                            lambda *a, **kw: {"managed_vehicles": SnapshotState(
                                date=today - _d.timedelta(2), row_count=None,
                                table_date=today - _d.timedelta(2))})
        rs: dict = {}
        result = _resolve_b(source=_PopOnlySource(
            AdpTireSource("staging", athena_client=object(), workgroup="w",
                          results_s3_path="s3://r/", poll_interval=0)),
            run_stats=rs)
        assert all(v.startswith("MRDPOP") for v in result), (
            "Two-days-old snapshot is stale; must fall back to population VINs. "
            "Mutation: not raising on stale → managed VINs returned → CAUGHT."
        )
        assert rs.get("managed_select_errors", 0) == 1, (
            "managed_select_errors must be 1 after stale fallback. "
            "Mutation: not incrementing → 0 → CAUGHT."
        )
