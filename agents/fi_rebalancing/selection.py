"""Deficit-location selection and metro-skip logic (spec G4-D1, G4-D8 step 1).

Pure functions; no AWS calls.
"""
from __future__ import annotations


def select_deficit_locations(rows: list[dict]) -> list[dict]:
    """Return only rows whose ``status`` is ``'deficit'`` (spec D8 step 1)."""
    return [r for r in rows if r.get("status") == "deficit"]


def should_skip_metro(*, live_item_hash: str | None, candidate_hash: str) -> bool:
    """Return True iff hashes match — caller should skip the model call (spec G4-D8 step 5).

    A None live hash (no prior item) never matches, so a fresh write is always made.
    """
    if live_item_hash is None:
        return False
    return live_item_hash == candidate_hash
