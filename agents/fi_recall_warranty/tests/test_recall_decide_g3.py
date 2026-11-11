"""Tests for recall.decide_items (spec D9, task 3.1 G2a).

Eight scenarios:
  D1 — p0 row, not grounded           → [ground/1, book/1]
  D2 — p0 row, already grounded       → [book/1]
  D3 — row 3 (attention, [book])      → [book/1]
  D4 — remedy date tomorrow           → [parts_expected/1]
  D5 — remedy date today              → [book/1]
  D6 — history: cancelled attempt 2   → [book/3] (write_next)
  D7 — history: executed attempt 1    → []  (skip)
  D8 — history: 5th attempt failed    → [attempts_exhausted/6]

Mutation guards (named by failing assertion):
  (a) ground written even when grounded
      → test_p0_grounded_book_only: assert len == 1, item_type == 'book'
  (b) ground added when band lacks it
      → test_row3_book_only: assert len == 1, item_type == 'book'
  (c) writer accepts ground for row-3 band
      → test_ground_for_row3_band_raises (in test_writer_g3.py)
  (d) parts_expected uses >= instead of >
      → test_remedy_date_today_book: assert item_type == 'book'

Run:
    AWS_CONFIG_FILE=/dev/null AWS_SHARED_CREDENTIALS_FILE=/dev/null \\
        .venv/bin/python -m pytest agents/fi_recall_warranty/tests -q
"""
from __future__ import annotations

import datetime

import pytest

from agents.fi_recall_warranty.recall import decide_items

# ---------------------------------------------------------------------------
# Shared row templates
# ---------------------------------------------------------------------------

_TODAY = datetime.date(2025, 1, 15)
_TOMORROW = "2025-01-16"
_TODAY_STR = "2025-01-15"

# Row 1 (band): safety, do_not_drive=True → p0, [ground, book]
_ROW_P0 = {
    "campaign_id": "MRD-2024-001",
    "recall_class": "safety",
    "do_not_drive": True,
    "match_basis": "CONFIRMED",
    "remedy_available_from": None,
}

# Row 3 (band): safety, do_not_drive=False, POPULATION → attention, [book]
_ROW3 = {
    "campaign_id": "MRD-2024-002",
    "recall_class": "safety",
    "do_not_drive": False,
    "match_basis": "POPULATION",
    "remedy_available_from": None,
}

_NO_HISTORY: dict = {}


# ---------------------------------------------------------------------------
# D1 — p0 not grounded → ground + book
# ---------------------------------------------------------------------------

class TestP0NotGrounded:

    def test_returns_ground_and_book(self):
        """D1: p0 row, not grounded → [ground/1, book/1].

        Mutation (a): removing the 'not grounded' guard emits ground even when
        grounded — caught by test_p0_grounded_book_only.
        Mutation (b): appending ground even when not in band items emits ground
        for row-3 rows — caught by test_row3_book_only.
        """
        items = decide_items(_ROW_P0, grounded=False, history=_NO_HISTORY, today=_TODAY)
        types = [i["item_type"] for i in items]
        assert types == ["ground", "book"], (
            f"p0 not grounded must emit [ground, book]; got {types!r}"
        )
        assert items[0]["attempt"] == 1
        assert items[1]["attempt"] == 1


# ---------------------------------------------------------------------------
# D2 — p0 grounded → book only
# ---------------------------------------------------------------------------

class TestP0Grounded:

    def test_book_only(self):
        """D2: p0 row, already grounded → [book/1].

        Mutation (a): ground written even when grounded → len == 2, fails here.
        """
        items = decide_items(_ROW_P0, grounded=True, history=_NO_HISTORY, today=_TODAY)
        types = [i["item_type"] for i in items]
        assert len(items) == 1, (
            f"grounded vehicle must not get a ground item; got {types!r}"
        )
        assert items[0]["item_type"] == "book", (
            f"only a book item expected; got {items[0]['item_type']!r}"
        )


# ---------------------------------------------------------------------------
# D3 — row 3 → book only (band has no ground)
# ---------------------------------------------------------------------------

class TestRow3BookOnly:

    def test_book_only(self):
        """D3: row 3 (attention, [book]) → [book/1].

        Mutation (b): decide_items appends 'ground' for safety regardless of
        band.items → len == 2 with ground, fails here.
        """
        items = decide_items(_ROW3, grounded=False, history=_NO_HISTORY, today=_TODAY)
        types = [i["item_type"] for i in items]
        assert len(items) == 1, (
            f"row-3 band has no ground; expected [book], got {types!r}"
        )
        assert items[0]["item_type"] == "book"


# ---------------------------------------------------------------------------
# D4 — remedy date tomorrow → parts_expected
# ---------------------------------------------------------------------------

class TestRemedyTomorrow:

    def test_parts_expected(self):
        """D4: remedy_available_from tomorrow → item_type='parts_expected'."""
        row = dict(_ROW_P0, remedy_available_from=_TOMORROW)
        items = decide_items(row, grounded=False, history=_NO_HISTORY, today=_TODAY)
        book_items = [i for i in items if i["item_type"] in ("book", "parts_expected")]
        assert len(book_items) == 1
        assert book_items[0]["item_type"] == "parts_expected", (
            f"remedy tomorrow must yield parts_expected; got {book_items[0]['item_type']!r}"
        )


# ---------------------------------------------------------------------------
# D5 — remedy date today → book (not parts_expected)
# ---------------------------------------------------------------------------

class TestRemedyToday:

    def test_book(self):
        """D5: remedy_available_from == today → item_type='book'.

        Mutation (d): using >= instead of > would return parts_expected here,
        failing this assertion.
        """
        row = dict(_ROW_P0, remedy_available_from=_TODAY_STR)
        items = decide_items(row, grounded=False, history=_NO_HISTORY, today=_TODAY)
        book_items = [i for i in items if i["item_type"] in ("book", "parts_expected")]
        assert len(book_items) == 1
        assert book_items[0]["item_type"] == "book", (
            f"remedy today must yield book (parts available); "
            f"got {book_items[0]['item_type']!r}. "
            "Mutation (d): >= would make this parts_expected."
        )


# ---------------------------------------------------------------------------
# D6 — cancelled attempt 2 → attempt 3 (write_next)
# ---------------------------------------------------------------------------

class TestHistoryCancelledAttempt2:

    def test_write_next_attempt_3(self):
        """D6: history shows attempt 2 cancelled → write_next → book/3."""
        history = {
            ("MRD-2024-001", "book"): {
                "attempt_number": 2,
                "finding_id": "FIND-abc",
                "inputs_hash": "x" * 64,
                "expires_at": "2025-01-20T00:00:00Z",
                "diagnosis_source": None,
                "queue_status": "cancelled",
                "decided_at": "2025-01-14T10:00:00Z",
            }
        }
        items = decide_items(_ROW_P0, grounded=True, history=history, today=_TODAY)
        book_items = [i for i in items if i["item_type"] in ("book", "parts_expected")]
        assert len(book_items) == 1
        assert book_items[0]["attempt"] == 3, (
            f"cancelled attempt 2 → next attempt is 3; got {book_items[0]['attempt']}"
        )


# ---------------------------------------------------------------------------
# D7 — history executed → nothing for that action
# ---------------------------------------------------------------------------

class TestHistoryExecuted:

    def test_skip(self):
        """D7: history shows executed → plan_attempt returns skip → no book item."""
        history = {
            ("MRD-2024-001", "book"): {
                "attempt_number": 1,
                "finding_id": "FIND-xyz",
                "inputs_hash": "y" * 64,
                "expires_at": "2025-01-20T00:00:00Z",
                "diagnosis_source": None,
                "queue_status": "executed",
                "decided_at": "2025-01-13T10:00:00Z",
            }
        }
        items = decide_items(_ROW_P0, grounded=True, history=history, today=_TODAY)
        book_items = [i for i in items if i["item_type"] in ("book", "parts_expected")]
        assert len(book_items) == 0, (
            f"executed history → no book item; got {book_items!r}"
        )


# ---------------------------------------------------------------------------
# D8 — 5th attempt failed → attempts_exhausted
# ---------------------------------------------------------------------------

class TestFifthAttemptFailed:

    def test_attempts_exhausted(self):
        """D8: 5th attempt failed (MAX_ATTEMPTS=5) → write_exhausted → book_exhausted/1."""
        history = {
            ("MRD-2024-001", "book"): {
                "attempt_number": 5,
                "finding_id": "FIND-max",
                "inputs_hash": "z" * 64,
                "expires_at": "2025-01-20T00:00:00Z",
                "diagnosis_source": None,
                "queue_status": "failed",
                "decided_at": "2025-01-14T10:00:00Z",
            }
        }
        items = decide_items(_ROW_P0, grounded=True, history=history, today=_TODAY)
        exhausted = [i for i in items if i["item_type"] == "book_exhausted"]
        assert len(exhausted) == 1, (
            f"5th failed must emit book_exhausted; got {[i['item_type'] for i in items]!r}"
        )
        assert exhausted[0]["attempt"] == 1, (
            f"exhausted attempt number must be 1 (singleton slot); got {exhausted[0]['attempt']}"
        )
