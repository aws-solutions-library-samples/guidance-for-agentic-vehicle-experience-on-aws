"""Stage F2 loop tests (Fix Group G3-1).

Coverage (driving run_recall_loop directly)
-------------------------------------------
- parts_expected: written with own headline and key (not the book slot key).
- 5-attempt book history: writes book_exhausted/1; ground slot unaffected.
- 5-attempt ground history on p0 row: writes ground_exhausted/1; book/1 still written.
- no origin (vehicle_ids_fn → None) with three rooftops: no TypeError, item written,
  in_stock candidate 0 chosen over alphabetically-earlier out_of_stock dealer.
- in-stock rooftop beats nearer out-of-stock one (origin known).
- empty rooftop read: no book item written, book_items_without_rooftops incremented.

Each refusal/skip test has a must-succeed control leg.
"""
from __future__ import annotations

import pytest

import datetime
from typing import Any, Optional

from agents.fi_recall_warranty.recall import run_recall_loop
from agents.fi_recall_warranty.attempts import MAX_ATTEMPTS

_TODAY = datetime.date(2026, 10, 7)
_AT = "2026-10-07T05:00:00Z"
_VIN = "MRDTST0000F2A0001"
_FLEET = "fleet-f2"
_LOC = "loc-f2"
_CAM = "25V-F2A"
_PART = "PART-F2"


def _row(*, cam: str = _CAM, remedy_from: Optional[str] = None,
         recall_class: str = "safety", do_not_drive: bool = False,
         match_basis: str = "POPULATION") -> dict:
    return {
        "vin": _VIN, "campaign_id": cam, "recall_class": recall_class,
        "do_not_drive": do_not_drive, "match_basis": match_basis,
        "remedy_available_from": remedy_from, "remedy_part_numbers": [_PART],
        "summary": "Defect.", "consequence": "Risk.", "remedy": "Fix.",
        "received_date": "2026-01-01", "catalog_version": "1", "rule_version": "1",
        "models": None, "model_year_min": None, "model_year_max": None,
        "plants": None, "build_from": None, "build_to": None,
        "park_outside": False, "confirming_code": None,
        "confirming_seen_on": None, "confirming_source": None,
    }


def _stock_rows(*dealers: tuple) -> list:
    """Build stock rows. dealers: (dealer_id, qty) sorted by ascending lat (distance proxy)."""
    return [
        {"dealer_id": d, "part_number": _PART, "qty_on_hand": str(q),
         "latitude": str(37.78 + i * 0.05), "longitude": "-122.42",
         "stock_as_of": "2026-10-07"}
        for i, (d, q) in enumerate(dealers)
    ]


class _Fakes:
    def __init__(self, *, no_origin: bool = False, history: Optional[dict] = None):
        self.written: list[dict] = []
        self._no_origin = no_origin
        self._history = history or {}

    def vehicle_ids_fn(self, vin: str):
        return None if self._no_origin else (f"vid-{vin}", _FLEET, _LOC)

    def locations_fn(self, fleet_id: str) -> dict:
        return {_LOC: (37.7749, -122.4194)}

    def grounded_fn(self, vid: str):
        return False, 0

    def history_fn(self, vin: str):
        return self._history, 0

    def standing_fn(self, vin: str):
        return object()

    def lookup_live(self, key: str):
        return None

    def write_finding(self, f: dict, standing: Any, refresh_on_noop: bool = False):
        self.written.append(f)
        return {"action": "put"}


def _run(fakes: _Fakes, rows: list, stock_rows: Optional[list] = None) -> dict:
    return run_recall_loop(
        recall_rows=rows,
        stock_rows=stock_rows if stock_rows is not None else _stock_rows((_CAM + "-d", 5)),
        today=_TODAY, computed_at=_AT,
        vehicle_ids_fn=fakes.vehicle_ids_fn, locations_fn=fakes.locations_fn,
        grounded_fn=fakes.grounded_fn, history_fn=fakes.history_fn,
        standing_fn=fakes.standing_fn, lookup_live=fakes.lookup_live,
        write_finding=fakes.write_finding,
    )


def _book_hist(attempt: int = MAX_ATTEMPTS, qs: str = "failed") -> dict:
    return {(_CAM, "book"): {
        "attempt_number": attempt, "finding_id": "FIND-F2-BK",
        "inputs_hash": "b" * 64, "expires_at": "2026-10-10T00:00:00Z",
        "diagnosis_source": None, "queue_status": qs,
        "decided_at": "2026-10-06T12:00:00Z",
    }}


def _ground_hist(attempt: int = MAX_ATTEMPTS, qs: str = "failed") -> dict:
    return {(_CAM, "ground"): {
        "attempt_number": attempt, "finding_id": "FIND-F2-GR",
        "inputs_hash": "g" * 64, "expires_at": "2026-10-10T00:00:00Z",
        "diagnosis_source": None, "queue_status": qs,
        "decided_at": "2026-10-06T12:00:00Z",
    }}


# ─── Test 1: parts_expected headline and key ─────────────────────────────────

class TestPartsExpected:

    def test_parts_expected_uses_own_key_segment(self):
        """parts_expected writes to parts_expected/<n>, not book/<n>."""
        tomorrow = str(_TODAY + datetime.timedelta(days=1))
        fakes = _Fakes()
        _run(fakes, [_row(remedy_from=tomorrow)])
        assert fakes.written, "control: must write at least one finding"
        keys = [f["subject_key"] for f in fakes.written]
        assert any("parts_expected" in k for k in keys), (
            f"parts_expected must use its own key segment; got {keys}"
        )
        assert not any(k.split("/")[1] == "book" for k in keys), (
            f"parts_expected must NOT write to the book action segment; got {keys}"
        )

    def test_parts_expected_headline_template(self):
        """parts_expected headline is 'Recall <id>: remedy parts expected <date>'."""
        from agents.fi_recall_warranty import writer
        tomorrow = "2026-10-08"
        h = writer.build_headline("parts_expected", _CAM, dealer_id=None, remedy_date=tomorrow)
        assert "remedy parts expected" in h, f"wrong template: {h!r}"
        assert tomorrow in h, f"remedy date must appear in headline: {h!r}"

    def test_book_when_remedy_available_today(self):
        """Control: remedy_from == today → book key (not parts_expected)."""
        fakes = _Fakes()
        _run(fakes, [_row(remedy_from=str(_TODAY))])
        keys = [f["subject_key"] for f in fakes.written]
        assert any(k.split("/")[1] == "book" for k in keys), (
            f"remedy available today must use book key; got {keys}"
        )


# ─── Test 2: exhausted item on slot's own key ─────────────────────────────────

class TestExhausted:

    def test_book_exhausted_key_and_attempt(self):
        """5 failed book attempts → book_exhausted/1, NOT book/6 or ground_exhausted."""
        fakes = _Fakes(history=_book_hist())
        _run(fakes, [_row()])
        assert fakes.written, "exhausted item must be written"
        keys = [f["subject_key"] for f in fakes.written]
        assert any("book_exhausted" in k for k in keys), (
            f"book exhaustion must write book_exhausted key; got {keys}. "
            "Mutation c: writing to 'book' key instead fails this assertion."
        )
        assert not any("ground_exhausted" in k for k in keys), (
            "book exhaustion must NOT produce a ground_exhausted key"
        )
        ex = [f for f in fakes.written if "book_exhausted" in f["subject_key"]]
        assert ex[0]["subject_key"].endswith("/1"), (
            f"exhausted attempt must be 1; got {ex[0]['subject_key']!r}"
        )

    def test_ground_exhausted_key_leaves_book_alone(self):
        """5 failed ground attempts on p0 row → ground_exhausted/1; book/1 also written."""
        fakes = _Fakes(history=_ground_hist())
        p0 = _row(do_not_drive=True, match_basis="CONFIRMED")
        _run(fakes, [p0])
        keys = [f["subject_key"] for f in fakes.written]
        assert any("ground_exhausted" in k for k in keys), (
            f"ground exhaustion must write ground_exhausted; got {keys}"
        )
        assert not any("book_exhausted" in k for k in keys), (
            "ground exhaustion must NOT write book_exhausted"
        )
        assert any(k.split("/")[1] == "book" for k in keys), (
            "book slot (unaffected) must still produce book/1"
        )

    def test_control_no_history_writes_normal_keys(self):
        """Control: no history → book/1 (not exhausted)."""
        fakes = _Fakes()
        _run(fakes, [_row()])
        keys = [f["subject_key"] for f in fakes.written]
        assert any(k.split("/")[1] == "book" for k in keys)
        assert not any("exhausted" in k for k in keys)


# ─── Test 3: no origin with three rooftops ────────────────────────────────────

class TestNoOriginThreeRooftops:

    def test_no_typeerror_and_item_written(self):
        """vehicle_ids_fn→None with 3 rooftops must not raise TypeError; item written."""
        stock = _stock_rows(("d-a", 0), ("d-b", 5), ("d-c", 0))
        fakes = _Fakes(no_origin=True)
        try:
            stats = _run(fakes, [_row()], stock_rows=stock)
        except TypeError as exc:  # cycle 2 agent N5: fail on this test's own line
            pytest.fail(f"no-origin run must not raise TypeError (C4): {exc}")
        assert stats["items_written"] >= 1
        assert fakes.written, (
            "must write item with no origin (no TypeError). "
            "Old code raised TypeError from min(None<None)."
        )

    def test_in_stock_candidate_chosen_when_origin_none(self):
        """With origin=None, in_stock candidate d-b wins over d-a (alphabetically first).

        Mutation d: if origin=None ignores in_stock, d-a (alphabetically first,
        out_of_stock) is picked instead of d-b (in_stock).
        """
        # d-a alphabetically first, out of stock; d-b in_stock
        stock = _stock_rows(("d-a", 0), ("d-b", 5))
        fakes = _Fakes(no_origin=True)
        _run(fakes, [_row()], stock_rows=stock)
        book_items = [f for f in fakes.written if f["subject_key"].split("/")[1] == "book"]
        assert book_items, "must write book item"
        dealer = book_items[0]["recommended_action"]["parameters"]["dealer_id"]
        assert dealer == "d-b", (
            f"in_stock d-b must be candidate 0; got {dealer!r}. "
            "Mutation d: sorting by dealer_id only picks d-a (out_of_stock)."
        )


# ─── Test 4: in_stock beats nearer out_of_stock (origin known) ────────────────

class TestInStockBeatsNearerOutOfStock:

    def test_in_stock_wins(self):
        """In_stock dealer at ~4mi beats out_of_stock at ~2mi.

        Mutation b: sorting by distance only picks the nearer out_of_stock dealer.
        """
        # d-near: 0 parts, ~2mi from SF (lat 37.79); d-far: 5 parts, ~4mi (lat 37.81)
        stock = [
            {"dealer_id": "d-near", "part_number": _PART, "qty_on_hand": "0",
             "latitude": "37.79", "longitude": "-122.42", "stock_as_of": "2026-10-07"},
            {"dealer_id": "d-far", "part_number": _PART, "qty_on_hand": "5",
             "latitude": "37.81", "longitude": "-122.42", "stock_as_of": "2026-10-07"},
        ]
        fakes = _Fakes()
        _run(fakes, [_row()], stock_rows=stock)
        book_items = [f for f in fakes.written if f["subject_key"].split("/")[1] == "book"]
        assert book_items, "must write book item"
        dealer = book_items[0]["recommended_action"]["parameters"]["dealer_id"]
        assert dealer == "d-far", (
            f"in_stock d-far must beat nearer out_of_stock d-near; got {dealer!r}. "
            "Mutation b: distance-only sort picks d-near."
        )
        assert "not in stock" not in book_items[0]["detail"], book_items[0]["detail"]

    def test_detail_says_not_in_stock_when_no_rooftop_holds_parts(self):
        """D9 / G3-16: when candidate 0 is not in stock, the fixed detail says so."""
        stock = [
            {"dealer_id": "d-zero", "part_number": _PART, "qty_on_hand": "0",
             "latitude": "37.79", "longitude": "-122.42", "stock_as_of": "2026-10-07"},
        ]
        fakes = _Fakes()
        _run(fakes, [_row()], stock_rows=stock)
        book_items = [f for f in fakes.written if f["subject_key"].split("/")[1] == "book"]
        assert book_items, "must write book item naming the rooftop"
        assert book_items[0]["recommended_action"]["parameters"]["dealer_id"] == "d-zero"
        assert "not in stock at d-zero" in book_items[0]["detail"], book_items[0]["detail"]


# ─── Test 5: empty rooftop read ───────────────────────────────────────────────

class TestEmptyRooftops:

    def test_no_book_item_when_empty_stock(self):
        """No stock rows → no book item written, book_items_without_rooftops = 1."""
        fakes = _Fakes()
        stats = _run(fakes, [_row()], stock_rows=[])
        book = [f for f in fakes.written if f["subject_key"].split("/")[1] in ("book", "parts_expected")]
        assert not book, f"empty stock must not write book item; got subjects={[f['subject_key'] for f in book]}"
        assert stats["book_items_without_rooftops"] >= 1, (
            "book_items_without_rooftops must be incremented when stock is empty"
        )

    def test_parts_expected_written_when_stock_empty(self):
        """Remedy not yet available and no DMS stock rows at all (the usual case before the
        parts ship): the parts-expected item names no rooftop and is still written."""
        fakes = _Fakes()
        tomorrow = (_TODAY + datetime.timedelta(days=1)).isoformat()
        stats = _run(fakes, [_row(remedy_from=tomorrow)], stock_rows=[])
        keys = [f["subject_key"] for f in fakes.written]
        assert f"{_CAM}/parts_expected/1" in keys, (
            f"parts_expected must be written with an empty stock read; got {keys}"
        )
        assert stats["book_items_without_rooftops"] == 0

    def test_control_nonempty_stock_writes_book_item(self):
        """Control: non-empty stock writes a book item and counter stays 0."""
        fakes = _Fakes()
        stats = _run(fakes, [_row()], stock_rows=_stock_rows(("d-ok", 5)))
        book = [f for f in fakes.written if f["subject_key"].split("/")[1] == "book"]
        assert book, "non-empty stock must produce a book item (control)"
        assert stats["book_items_without_rooftops"] == 0
