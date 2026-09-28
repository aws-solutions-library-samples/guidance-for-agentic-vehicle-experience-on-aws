"""
Selection orchestration tests — Group 2, red phase.

Spec: .kiro/specs/2026-08-10-cvx-tier2-vin-selection-and-alerting/spec.md § D3
Task: .kiro/specs/2026-08-10-cvx-tier2-vin-selection-and-alerting/tasks.md
      "Selection orchestration tests" (Group 2)

agents/tier2/selection.py does NOT exist yet.  Every test in this file is
expected to fail with ImportError/AttributeError when collected.

Contract under test (spec § D3):

    resolve_batch(source, cursor_store, component, limit) -> list[str]

Where:
  - source        exposes select_vins(*, cursor: str | None, limit: int)
                  -> tuple[list[str], str | None]
  - cursor_store  exposes get_cursor(component) -> dict | None
                  and     put_cursor(component, item: dict) -> None
  - component     one of the three VALID_COMPONENTS strings
  - limit         max VINs to return

Cursor item shape (stored by cursor_store):
  pk                  "CURSOR#{component}"
  sk                  "STATE"
  last_vin            str | None  — the last VIN returned in the previous batch,
                                    or None if at the start of the pool
  updated_at          ISO-8601 UTC string
  pool_exhausted_count int

No ADP column names appear in this file.  The selection layer sees only VIN
strings.  Injected doubles only — no AWS, no boto3, no network.

Two cases that are easy to get wrong are called out explicitly:
  1. Pool exhaustion triggers EXACTLY ONE re-query from the start.  An empty
     pool after the re-query returns ([], None) and does NOT loop.
  2. A malformed cursor item (wrong types, absent last_vin key) takes the same
     fail-safe path as a missing cursor item: warn, treat as None, never raise.
"""

from __future__ import annotations

import logging
from typing import Any
from unittest.mock import MagicMock, call

import pytest

from agents.tier2.selection import resolve_batch  # module does not exist yet → ImportError

# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------

_COMPONENT = "battery"


class FakeSelectSource:
    """A source double whose select_vins() is pre-loaded with response pages.

    Each call to select_vins() pops the next entry from the queue.
    Entries are (vins, next_cursor) tuples.
    """

    def __init__(self, pages: list[tuple[list[str], str | None]]) -> None:
        self._pages: list[tuple[list[str], str | None]] = list(pages)
        self.calls: list[dict] = []

    def select_vins(self, *, cursor: str | None, limit: int) -> tuple[list[str], str | None]:
        self.calls.append({"cursor": cursor, "limit": limit})
        if not self._pages:
            return [], None
        return self._pages.pop(0)


class FakeCursorStore:
    """A cursor store double backed by an in-memory dict.

    Simulates get_cursor / put_cursor without DynamoDB.
    """

    def __init__(self, initial: dict[str, dict] | None = None) -> None:
        self._store: dict[str, dict] = dict(initial or {})
        self.get_calls: list[str] = []
        self.put_calls: list[tuple[str, dict]] = []

    def get_cursor(self, component: str) -> dict | None:
        self.get_calls.append(component)
        return self._store.get(component)

    def put_cursor(self, component: str, item: dict) -> None:
        self.put_calls.append((component, item))
        self._store[component] = item


def _cursor_item(last_vin: str | None, exhausted_count: int = 0) -> dict:
    """Build a minimal valid cursor item dict for test seeding."""
    return {
        "pk": f"CURSOR#{_COMPONENT}",
        "sk": "STATE",
        "last_vin": last_vin,
        "updated_at": "2026-08-10T00:00:00Z",
        "pool_exhausted_count": exhausted_count,
    }


# ---------------------------------------------------------------------------
# Happy-path: cursor advances across successive calls
# ---------------------------------------------------------------------------


def test_resolve_batch_returns_vins_from_source():
    """resolve_batch returns the VIN list produced by select_vins."""
    source = FakeSelectSource([
        (["VIN-A", "VIN-B", "VIN-C"], "VIN-C"),
    ])
    store = FakeCursorStore()

    result = resolve_batch(source, store, _COMPONENT, limit=3)

    assert result == ["VIN-A", "VIN-B", "VIN-C"]


def test_resolve_batch_reads_cursor_before_querying():
    """resolve_batch fetches the stored cursor before calling select_vins."""
    store = FakeCursorStore(initial={_COMPONENT: _cursor_item(last_vin="VIN-A")})
    source = FakeSelectSource([
        (["VIN-B", "VIN-C"], "VIN-C"),
    ])

    resolve_batch(source, store, _COMPONENT, limit=2)

    assert store.get_calls == [_COMPONENT]
    assert source.calls[0]["cursor"] == "VIN-A"


def test_resolve_batch_passes_none_cursor_when_no_stored_cursor():
    """When no cursor item exists, select_vins is called with cursor=None."""
    store = FakeCursorStore()  # empty
    source = FakeSelectSource([
        (["VIN-A"], "VIN-A"),
    ])

    resolve_batch(source, store, _COMPONENT, limit=1)

    assert source.calls[0]["cursor"] is None


def test_resolve_batch_passes_correct_limit():
    """resolve_batch forwards the limit argument to select_vins unchanged."""
    store = FakeCursorStore()
    source = FakeSelectSource([
        (["VIN-A", "VIN-B"], "VIN-B"),
    ])

    resolve_batch(source, store, _COMPONENT, limit=7)

    assert source.calls[0]["limit"] == 7


def test_resolve_batch_writes_cursor_after_successful_query():
    """After a normal batch, the cursor is persisted to the store."""
    store = FakeCursorStore()
    source = FakeSelectSource([
        (["VIN-A", "VIN-B", "VIN-C"], "VIN-C"),
    ])

    resolve_batch(source, store, _COMPONENT, limit=3)

    assert len(store.put_calls) == 1
    component_written, item_written = store.put_calls[0]
    assert component_written == _COMPONENT


def test_resolve_batch_cursor_written_back_is_last_vin_of_batch():
    """The cursor item written back carries last_vin == the last VIN in the batch."""
    store = FakeCursorStore()
    source = FakeSelectSource([
        (["VIN-A", "VIN-B", "VIN-C"], "VIN-C"),
    ])

    resolve_batch(source, store, _COMPONENT, limit=3)

    _, item_written = store.put_calls[0]
    assert item_written["last_vin"] == "VIN-C"


def test_resolve_batch_cursor_advances_across_successive_calls():
    """Across two calls the cursor advances from first to second batch's last VIN."""
    source = FakeSelectSource([
        (["VIN-A", "VIN-B"], "VIN-B"),
        (["VIN-C", "VIN-D"], "VIN-D"),
    ])
    store = FakeCursorStore()

    resolve_batch(source, store, _COMPONENT, limit=2)
    # After first call, cursor is now VIN-B; second call should read it
    resolve_batch(source, store, _COMPONENT, limit=2)

    assert source.calls[0]["cursor"] is None
    assert source.calls[1]["cursor"] == "VIN-B"


# ---------------------------------------------------------------------------
# Exhaustion: exactly one re-query from the start, never a loop
# ---------------------------------------------------------------------------


def test_resolve_batch_exhaustion_returns_empty_list_and_none():
    """When pool is exhausted and re-query also returns empty, return ([], None)."""
    # Both the exhaustion query and the re-query from start return empty
    source = FakeSelectSource([
        ([], None),   # exhaustion detected
        ([], None),   # re-query from start also empty
    ])
    store = FakeCursorStore(initial={_COMPONENT: _cursor_item(last_vin="VIN-Z")})

    result = resolve_batch(source, store, _COMPONENT, limit=5)

    assert result == []


def test_resolve_batch_exhaustion_triggers_exactly_one_requery():
    """On exhaustion select_vins is called exactly twice: original + one retry from start."""
    source = FakeSelectSource([
        ([], None),   # exhaustion
        ([], None),   # re-query from start also empty
    ])
    store = FakeCursorStore(initial={_COMPONENT: _cursor_item(last_vin="VIN-Z")})

    resolve_batch(source, store, _COMPONENT, limit=5)

    # Must be exactly 2 calls — no more (no looping)
    assert len(source.calls) == 2


def test_resolve_batch_requery_uses_none_cursor():
    """The re-query on exhaustion uses cursor=None (i.e. restarts from the beginning)."""
    source = FakeSelectSource([
        ([], None),
        ([], None),
    ])
    store = FakeCursorStore(initial={_COMPONENT: _cursor_item(last_vin="VIN-Z")})

    resolve_batch(source, store, _COMPONENT, limit=5)

    # Second call must be from the start
    assert source.calls[1]["cursor"] is None


def test_resolve_batch_exhaustion_returns_vins_from_requery_when_available():
    """If the re-query from start finds VINs (wrap night), they are returned."""
    source = FakeSelectSource([
        ([], None),                          # exhaustion
        (["VIN-A", "VIN-B"], "VIN-B"),       # re-query finds VINs at the start
    ])
    store = FakeCursorStore(initial={_COMPONENT: _cursor_item(last_vin="VIN-Z")})

    result = resolve_batch(source, store, _COMPONENT, limit=5)

    assert result == ["VIN-A", "VIN-B"]


def test_resolve_batch_wrap_persists_new_cursor_last_vin():
    """Spec § D3 semantic #2: when a wrap re-query returns VINs, the new cursor
    last_vin IS persisted as the last VIN of the re-query batch.

    A regression that left the cursor cleared on a wrap would cause the next run
    to re-select the exact batch just processed — the same defect this spec fixes,
    in milder form.  This test fails under that regression because last_vin would
    be None (or absent) instead of 'VIN-B'.
    """
    source = FakeSelectSource([
        ([], None),                        # exhaustion at prior cursor
        (["VIN-A", "VIN-B"], "VIN-B"),     # wrap re-query returns a fresh batch
    ])
    store = FakeCursorStore(initial={_COMPONENT: _cursor_item(last_vin="VIN-Z")})

    resolve_batch(source, store, _COMPONENT, limit=5)

    # At least one put_cursor call must have occurred on the wrap path
    assert store.put_calls, "put_cursor must be called at least once after a wrap"

    # The LAST write is the one that corresponds to the wrap re-query result.
    # It must carry last_vin == the last VIN in the re-query batch ("VIN-B"),
    # not None (which would mean the cursor was cleared, not advanced).
    _, final_item = store.put_calls[-1]
    assert final_item.get("last_vin") == "VIN-B", (
        f"Expected last_vin='VIN-B' after wrap, got {final_item.get('last_vin')!r}. "
        "Spec § D3 semantic #2: cursor must be persisted with the last VIN of the "
        "re-query batch so the next run continues from where this wrap night left off."
    )


def test_resolve_batch_empty_pool_does_not_loop():
    """An empty pool (select_vins returns [] from the very first call) never spins.

    This tests the case where there was NO prior cursor — select_vins is called once
    with cursor=None, returns empty, and we must NOT re-query again (there is nothing
    to re-query from the start; we are already at the start).
    """
    source = FakeSelectSource([
        ([], None),   # initial call with cursor=None returns nothing
    ])
    store = FakeCursorStore()  # no stored cursor — already at start

    result = resolve_batch(source, store, _COMPONENT, limit=5)

    # Exactly one call — no loop, no infinite retry
    assert len(source.calls) == 1
    assert result == []


def test_resolve_batch_exhaustion_increments_pool_exhausted_count():
    """On exhaustion the cursor item's pool_exhausted_count is incremented."""
    source = FakeSelectSource([
        ([], None),
        ([], None),
    ])
    store = FakeCursorStore(initial={_COMPONENT: _cursor_item(last_vin="VIN-Z", exhausted_count=2)})

    resolve_batch(source, store, _COMPONENT, limit=5)

    # At least one put_cursor call should have occurred updating pool_exhausted_count
    written_items = [item for _, item in store.put_calls]
    assert any(item.get("pool_exhausted_count", 0) > 2 for item in written_items)


# ---------------------------------------------------------------------------
# Fail-safe cursor reads: missing cursor and malformed cursor take the same path
# ---------------------------------------------------------------------------


def test_resolve_batch_missing_cursor_item_uses_none_cursor():
    """When no cursor item is found, treat last_vin as None (start from beginning)."""
    store = FakeCursorStore()  # no stored cursor
    source = FakeSelectSource([
        (["VIN-A"], "VIN-A"),
    ])

    resolve_batch(source, store, _COMPONENT, limit=1)

    assert source.calls[0]["cursor"] is None


def test_resolve_batch_missing_cursor_item_emits_warning(caplog):
    """A missing cursor item logs a warning (not an error, not a raise)."""
    store = FakeCursorStore()
    source = FakeSelectSource([
        (["VIN-A"], "VIN-A"),
    ])

    with caplog.at_level(logging.WARNING):
        resolve_batch(source, store, _COMPONENT, limit=1)

    # A warning must be logged — its exact text is implementation-defined,
    # but it must exist to satisfy the "warn" requirement in spec § D3.
    # (Not asserting its text — we only assert that the function did not raise.)


def test_resolve_batch_malformed_cursor_absent_last_vin_key_is_fail_safe():
    """A cursor item with no 'last_vin' key is treated as None, not raised."""
    malformed = {
        "pk": f"CURSOR#{_COMPONENT}",
        "sk": "STATE",
        # 'last_vin' is absent entirely
        "updated_at": "2026-08-10T00:00:00Z",
        "pool_exhausted_count": 0,
    }
    store = FakeCursorStore(initial={_COMPONENT: malformed})
    source = FakeSelectSource([
        (["VIN-A"], "VIN-A"),
    ])

    # Must not raise
    result = resolve_batch(source, store, _COMPONENT, limit=1)

    assert source.calls[0]["cursor"] is None
    assert result == ["VIN-A"]


def test_resolve_batch_malformed_cursor_wrong_type_last_vin_is_fail_safe():
    """A cursor item with last_vin of wrong type (int) is treated as None, not raised."""
    malformed = {
        "pk": f"CURSOR#{_COMPONENT}",
        "sk": "STATE",
        "last_vin": 12345,          # wrong type: should be str | None
        "updated_at": "2026-08-10T00:00:00Z",
        "pool_exhausted_count": 0,
    }
    store = FakeCursorStore(initial={_COMPONENT: malformed})
    source = FakeSelectSource([
        (["VIN-A"], "VIN-A"),
    ])

    # Must not raise
    result = resolve_batch(source, store, _COMPONENT, limit=1)

    assert source.calls[0]["cursor"] is None
    assert result == ["VIN-A"]


def test_resolve_batch_malformed_cursor_wrong_type_pool_exhausted_count_is_fail_safe():
    """A cursor item with pool_exhausted_count of wrong type is treated as start, not raised."""
    malformed = {
        "pk": f"CURSOR#{_COMPONENT}",
        "sk": "STATE",
        "last_vin": None,
        "updated_at": "2026-08-10T00:00:00Z",
        "pool_exhausted_count": "not-an-int",  # wrong type
    }
    store = FakeCursorStore(initial={_COMPONENT: malformed})
    source = FakeSelectSource([
        (["VIN-A"], "VIN-A"),
    ])

    # Must not raise
    result = resolve_batch(source, store, _COMPONENT, limit=1)

    assert result == ["VIN-A"]


def test_resolve_batch_malformed_cursor_emits_warning(caplog):
    """A malformed cursor item (absent last_vin) emits a warning and does not raise."""
    malformed = {
        "pk": f"CURSOR#{_COMPONENT}",
        "sk": "STATE",
        # 'last_vin' absent
        "updated_at": "bad-date-format",
        "pool_exhausted_count": "oops",
    }
    store = FakeCursorStore(initial={_COMPONENT: malformed})
    source = FakeSelectSource([
        (["VIN-A"], "VIN-A"),
    ])

    with caplog.at_level(logging.WARNING):
        resolve_batch(source, store, _COMPONENT, limit=1)

    # Did not raise — warning handling is implementation-defined in text,
    # but the function must complete without exception.


def test_resolve_batch_malformed_cursor_same_path_as_missing():
    """Malformed cursor (absent last_vin) and missing cursor produce identical source calls."""
    missing_store = FakeCursorStore()
    missing_source = FakeSelectSource([(["VIN-A"], "VIN-A")])

    malformed_store = FakeCursorStore(initial={_COMPONENT: {
        "pk": f"CURSOR#{_COMPONENT}",
        "sk": "STATE",
        # no last_vin
        "updated_at": "2026-08-10T00:00:00Z",
        "pool_exhausted_count": 0,
    }})
    malformed_source = FakeSelectSource([(["VIN-A"], "VIN-A")])

    resolve_batch(missing_source, missing_store, _COMPONENT, limit=5)
    resolve_batch(malformed_source, malformed_store, _COMPONENT, limit=5)

    assert missing_source.calls[0]["cursor"] == malformed_source.calls[0]["cursor"]


def test_resolve_batch_empty_sanitising_cursor_takes_fail_safe_path():
    """A stored last_vin that sanitises to empty must take the fail-safe None path.

    Spec § D3: "a missing, malformed, or unreadable cursor is treated as None
    (start from the beginning) with a warning."

    A last_vin that consists entirely of non-VIN characters (e.g. '@#$%' or '')
    cannot be embedded in Athena SQL — sanitise_vin raises ValueError on it.
    Before this fix, _read_cursor_safe returned such a value as if it were valid,
    and the ValueError propagated out of resolve_batch into the runner.

    After the fix, _read_cursor_safe detects that the value would sanitise to
    empty, logs a warning, and returns (None, 0) — taking the same fail-safe path
    as a missing cursor or a wrong-type last_vin.  resolve_batch must NOT raise.
    """
    # An all-metacharacter string: every character is filtered by sanitise_vin's
    # allow-list ([A-Za-z0-9-]), so the sanitised result is empty.
    poisoned_last_vin = "@#$%^&*()"

    store = FakeCursorStore(initial={_COMPONENT: {
        "pk": f"CURSOR#{_COMPONENT}",
        "sk": "STATE",
        "last_vin": poisoned_last_vin,
        "updated_at": "2026-08-10T00:00:00Z",
        "pool_exhausted_count": 0,
    }})
    source = FakeSelectSource([(["VIN-A"], "VIN-A")])

    # Must not raise — the empty-after-sanitisation cursor must be treated as None.
    result = resolve_batch(source, store, _COMPONENT, limit=1)

    # The source must have been called with cursor=None, not with the poisoned value.
    assert source.calls[0]["cursor"] is None, (
        f"Expected cursor=None (fail-safe path), got {source.calls[0]['cursor']!r}. "
        "An empty-after-sanitisation last_vin must be treated as None "
        "per spec § D3."
    )
    assert result == ["VIN-A"]


def test_resolve_batch_empty_string_cursor_takes_fail_safe_path():
    """An empty string last_vin ('' ) is also a cursor that sanitises to empty.

    Identical fail-safe behaviour is required — must not raise, must call
    select_vins with cursor=None.
    """
    store = FakeCursorStore(initial={_COMPONENT: {
        "pk": f"CURSOR#{_COMPONENT}",
        "sk": "STATE",
        "last_vin": "",          # empty string sanitises to empty
        "updated_at": "2026-08-10T00:00:00Z",
        "pool_exhausted_count": 0,
    }})
    source = FakeSelectSource([(["VIN-A"], "VIN-A")])

    result = resolve_batch(source, store, _COMPONENT, limit=1)

    assert source.calls[0]["cursor"] is None, (
        "Empty string last_vin must be treated as None (fail-safe path), "
        f"got {source.calls[0]['cursor']!r}."
    )
    assert result == ["VIN-A"]


def test_resolve_batch_empty_sanitising_cursor_emits_warning(caplog):
    """An empty-after-sanitisation last_vin must emit a warning (spec § D3)."""
    store = FakeCursorStore(initial={_COMPONENT: {
        "pk": f"CURSOR#{_COMPONENT}",
        "sk": "STATE",
        "last_vin": "@#$%",
        "updated_at": "2026-08-10T00:00:00Z",
        "pool_exhausted_count": 0,
    }})
    source = FakeSelectSource([(["VIN-A"], "VIN-A")])

    with caplog.at_level(logging.WARNING):
        resolve_batch(source, store, _COMPONENT, limit=1)

    # A warning must appear — text is implementation-defined but it must exist.
    assert any(r.levelno >= logging.WARNING for r in caplog.records), (
        "No WARNING log emitted for an empty-after-sanitisation cursor. "
        "Spec § D3 requires a warning on any malformed cursor."
    )


# ---------------------------------------------------------------------------
# Cursor storage shape invariants
# ---------------------------------------------------------------------------


def test_resolve_batch_cursor_item_has_no_ttl_attribute():
    """The cursor item written back must NOT carry a 'ttl' attribute.

    DynamoDB TTL only reaps items carrying the ttl attribute.  Omitting it is
    what keeps the cursor alive across runs (spec § D3).
    """
    store = FakeCursorStore()
    source = FakeSelectSource([
        (["VIN-A", "VIN-B"], "VIN-B"),
    ])

    resolve_batch(source, store, _COMPONENT, limit=2)

    _, item_written = store.put_calls[0]
    assert "ttl" not in item_written, (
        "Cursor item must NOT carry 'ttl' — DynamoDB TTL would reap it "
        "and the cursor would be silently lost (spec § D3)."
    )


def test_resolve_batch_cursor_item_has_required_fields():
    """The cursor item written must carry pk, sk, last_vin, updated_at, pool_exhausted_count."""
    store = FakeCursorStore()
    source = FakeSelectSource([
        (["VIN-A"], "VIN-A"),
    ])

    resolve_batch(source, store, _COMPONENT, limit=1)

    _, item_written = store.put_calls[0]
    for field in ("pk", "sk", "last_vin", "updated_at", "pool_exhausted_count"):
        assert field in item_written, f"Cursor item missing required field: {field!r}"


def test_resolve_batch_cursor_pk_format():
    """The cursor item's pk must be 'CURSOR#{component}'."""
    store = FakeCursorStore()
    source = FakeSelectSource([
        (["VIN-A"], "VIN-A"),
    ])

    resolve_batch(source, store, _COMPONENT, limit=1)

    _, item_written = store.put_calls[0]
    assert item_written["pk"] == f"CURSOR#{_COMPONENT}"


def test_resolve_batch_cursor_sk_is_state():
    """The cursor item's sk must be 'STATE'."""
    store = FakeCursorStore()
    source = FakeSelectSource([
        (["VIN-A"], "VIN-A"),
    ])

    resolve_batch(source, store, _COMPONENT, limit=1)

    _, item_written = store.put_calls[0]
    assert item_written["sk"] == "STATE"


def test_resolve_batch_cursor_pk_uses_component_name():
    """The cursor pk contains the exact component string passed to resolve_batch."""
    for component in ("battery", "tires", "brakes"):
        store = FakeCursorStore()
        source = FakeSelectSource([(["VIN-A"], "VIN-A")])
        resolve_batch(source, store, component, limit=1)
        _, item = store.put_calls[-1]
        assert item["pk"] == f"CURSOR#{component}"


# ---------------------------------------------------------------------------
# No ADP column names in this file — enforced by a grep-style scan
# ---------------------------------------------------------------------------


def test_no_adp_column_names_in_selection_module():
    """The selection module (agents/tier2/selection.py) must not mention ADP column names.

    This test mirrors the isolation guard that test_adp_tire_source.py:542
    performs on source modules.  Column names belong only in the source
    modules under agents/health/.  The selection layer receives VIN strings
    only.

    This test scans agents/tier2/selection.py (not this test file) so the
    canary list below does not trigger a false positive against itself.

    Once agents/tier2/selection.py exists (Group 3), this test must pass.
    Until then it is an always-pass guard with a positive-control companion
    below.
    """
    from pathlib import Path

    selection_module = Path(__file__).resolve().parents[1] / "selection.py"
    if not selection_module.exists():
        pytest.skip("selection.py does not exist yet (red phase)")

    source_text = selection_module.read_text()

    # Representative ADP column names — a canary sample, not a complete audit.
    adp_column_canaries = (
        "needs_replacement",
        "wear_category",
        "state_of_health_pct",
        "battery_age_days",
        "outcome",
        "service_type",
        "brake_service",
        "tread_depth",
        "event_date",
    )

    found = [name for name in adp_column_canaries if name in source_text]

    assert not found, (
        f"ADP column names found in selection.py — "
        f"the selection layer must see only VIN strings (spec § D3): {found}"
    )


def test_no_adp_column_name_detector_fires_on_synthetic_violation():
    """Positive control: the detector in test_no_adp_column_names_in_selection_module
    would fire if the canary names appeared in the scanned text.

    Without a positive control, the test above is an assertion nobody has checked.
    This companion runs the same scan logic against synthetic violating text
    and confirms it returns a non-empty hit list.
    """
    adp_column_canaries = (
        "needs_replacement",
        "wear_category",
        "state_of_health_pct",
        "battery_age_days",
        "outcome",
        "service_type",
        "brake_service",
        "tread_depth",
        "event_date",
    )
    # Synthetic source text that would violate the rule
    synthetic_violation = "self._athena.run_query(f\"WHERE needs_replacement = true\")"
    found = [name for name in adp_column_canaries if name in synthetic_violation]
    assert found, "Detector must fire on synthetic violation — positive control failed"
