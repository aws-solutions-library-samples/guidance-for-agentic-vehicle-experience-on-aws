"""Tests for rooftops.rank_rooftops (task 3.1F, spec D8 step 2).

Mutation targets (listed in task hand-back):
  (a) in_stock when ANY part is held      → test_missing_one_part_not_in_stock
  (b) sort by distance only (ignore tier) → test_in_stock_beats_nearer_out_of_stock
  (c) stock_as_of included in hash        → N/A (tested in test_evidence_g3.py)
  (d) hash ignores the rooftop set        → N/A (tested in test_evidence_g3.py)
"""
from __future__ import annotations

import pytest

from agents.fi_recall_warranty.rooftops import rank_rooftops


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_ORIGIN = (37.7749, -122.4194)  # San Francisco

def _row(dealer_id: str, part: str, qty: int,
         lat: float = 37.8, lon: float = -122.0,
         stock_as_of: str = "2026-10-01") -> dict:
    return {
        "dealer_id": dealer_id,
        "part_number": part,
        "qty_on_hand": str(qty),
        "latitude": str(lat),
        "longitude": str(lon),
        "stock_as_of": stock_as_of,
    }


# ---------------------------------------------------------------------------
# Core ranking: in-stock beats nearer out-of-stock
# (mutation target b — sort by distance only)
# ---------------------------------------------------------------------------

class TestInStockBeat:
    def test_in_stock_beats_nearer_out_of_stock(self):
        """Mutation (b) target: a farther in-stock rooftop must rank above a nearer
        out-of-stock one.  If we sort by distance only, the nearer D-B appears first
        and this assertion fails."""
        remedy_parts = ["P001", "P002"]
        stock_rows = [
            # D-A: holds both parts, farther (lat 38.5)
            _row("D-A", "P001", 2, lat=38.5, lon=-122.4),
            _row("D-A", "P002", 1, lat=38.5, lon=-122.4),
            # D-B: holds only P001, closer (lat 37.78)
            _row("D-B", "P001", 3, lat=37.78, lon=-122.4),
        ]
        result = rank_rooftops(remedy_parts, stock_rows, _ORIGIN)
        assert result[0]["dealer_id"] == "D-A"
        assert result[0]["in_stock"] is True
        assert result[1]["dealer_id"] == "D-B"
        assert result[1]["in_stock"] is False


# ---------------------------------------------------------------------------
# Missing one part → not in stock
# (mutation target a — in_stock when ANY part held)
# ---------------------------------------------------------------------------

class TestMissingOnePart:
    def test_missing_one_part_not_in_stock(self):
        """Mutation (a) target: holding P001 but not P002 must NOT set in_stock=True."""
        remedy_parts = ["P001", "P002"]
        stock_rows = [
            _row("D-C", "P001", 5),
            # P002 absent — effectively qty 0
        ]
        result = rank_rooftops(remedy_parts, stock_rows, _ORIGIN)
        assert len(result) == 1
        # in_stock must be False because P002 is absent
        assert result[0]["in_stock"] is False, (
            "Expected in_stock=False when one remedy part is missing"
        )

    def test_zero_qty_part_not_in_stock(self):
        """qty=0 means the part is not actually held."""
        remedy_parts = ["P001"]
        stock_rows = [_row("D-D", "P001", 0)]
        result = rank_rooftops(remedy_parts, stock_rows, _ORIGIN)
        assert result[0]["in_stock"] is False


# ---------------------------------------------------------------------------
# No origin → distance None, stable by dealer_id
# ---------------------------------------------------------------------------

class TestNoOrigin:
    def test_no_origin_distance_none(self):
        remedy_parts = ["P001"]
        stock_rows = [
            _row("D-Z", "P001", 1),
            _row("D-A", "P001", 1),
        ]
        result = rank_rooftops(remedy_parts, stock_rows, origin=None)
        assert all(c["distance_miles"] is None for c in result)

    def test_no_origin_stable_by_dealer_id(self):
        """Without an origin the order is alphabetical by dealer_id."""
        remedy_parts = ["P001"]
        stock_rows = [
            _row("D-Z", "P001", 1, lat=37.9, lon=-122.4),
            _row("D-A", "P001", 1, lat=38.5, lon=-122.4),
        ]
        result = rank_rooftops(remedy_parts, stock_rows, origin=None)
        dealer_ids = [c["dealer_id"] for c in result]
        assert dealer_ids == sorted(dealer_ids)


# ---------------------------------------------------------------------------
# Unparseable rows are skipped
# ---------------------------------------------------------------------------

class TestSkipBadRows:
    def test_bad_coordinates_skipped(self):
        remedy_parts = ["P001"]
        stock_rows = [
            {"dealer_id": "D-BAD", "part_number": "P001", "qty_on_hand": "1",
             "latitude": "not-a-number", "longitude": "0.0"},
            _row("D-GOOD", "P001", 1),
        ]
        result = rank_rooftops(remedy_parts, stock_rows, _ORIGIN)
        ids = [c["dealer_id"] for c in result]
        assert "D-BAD" not in ids
        assert "D-GOOD" in ids

    def test_missing_dealer_id_skipped(self):
        remedy_parts = []
        stock_rows = [{"dealer_id": "", "part_number": "", "qty_on_hand": "1",
                       "latitude": "37.8", "longitude": "-122.0"}]
        result = rank_rooftops(remedy_parts, stock_rows, _ORIGIN)
        assert result == []


# ---------------------------------------------------------------------------
# Top three cap
# ---------------------------------------------------------------------------

class TestTopThreeCap:
    def test_returns_at_most_three(self):
        remedy_parts = ["P001"]
        stock_rows = [
            _row(f"D-{i:02d}", "P001", 1, lat=37.8 + i * 0.01, lon=-122.0)
            for i in range(6)
        ]
        result = rank_rooftops(remedy_parts, stock_rows, _ORIGIN)
        assert len(result) <= 3
