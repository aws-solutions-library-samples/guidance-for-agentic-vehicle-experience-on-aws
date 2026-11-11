"""Rooftop ranking for the FI recall and warranty agent (spec D8 step 2, task 3.1F).

Public API
----------
rank_rooftops(remedy_parts, stock_rows, origin) -> list[dict]
    Return the top three candidate rooftops ranked for a recall remedy.
"""
from __future__ import annotations

import logging
import math
from typing import Optional

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Haversine distance
# ---------------------------------------------------------------------------
_EARTH_RADIUS_MILES = 3_958.8


def _haversine_miles(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in miles between two WGS-84 points."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
    return _EARTH_RADIUS_MILES * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


# ---------------------------------------------------------------------------
# Main ranking function
# ---------------------------------------------------------------------------

def rank_rooftops(
    remedy_parts: list[str],
    stock_rows: list[dict],
    origin: Optional[tuple[float, float]],
) -> list[dict]:
    """Rank rooftop candidates for a recall remedy.

    Parameters
    ----------
    remedy_parts:
        Part numbers required for the remedy (may be empty).
    stock_rows:
        Raw Athena rows with keys ``dealer_id``, ``part_number``, ``qty_on_hand``,
        ``latitude``, ``longitude``, and optionally ``stock_as_of``.
        Rows with unparseable coordinates or qty_on_hand are skipped.
    origin:
        ``(latitude, longitude)`` of the vehicle's CMS assigned location (G3-10).
        ``None`` → candidates unranked (stable by ``dealer_id``), ``distance_miles``
        is ``None`` on every returned entry.

    Returns
    -------
    list[dict]
        Up to three entries, each ``{dealer_id, distance_miles, in_stock,
        stock_as_of}``.  In-stock rooftops (holding every remedy part with
        qty > 0) rank before out-of-stock ones; within each tier, sorted by
        ascending distance when *origin* is known.
    """
    # --- Build per-dealer inventory ------------------------------------------
    dealer_parts: dict[str, dict[str, int]] = {}      # dealer_id -> {part -> qty}
    dealer_coords: dict[str, tuple[float, float]] = {}  # dealer_id -> (lat, lon)
    dealer_stock_as_of: dict[str, Optional[str]] = {}

    for row in stock_rows:
        d_id = row.get("dealer_id") or ""
        if not d_id:
            continue
        try:
            lat = float(row["latitude"])
            lon = float(row["longitude"])
        except (TypeError, ValueError, KeyError):
            log.debug("rooftops: skipping %s — unparseable coordinates", d_id)
            continue
        try:
            qty = int(float(row.get("qty_on_hand") or 0))
        except (TypeError, ValueError):
            log.debug("rooftops: skipping row for %s — unparseable qty", d_id)
            continue

        dealer_coords[d_id] = (lat, lon)
        part_no = row.get("part_number") or ""
        dealer_parts.setdefault(d_id, {})
        if part_no:
            dealer_parts[d_id][part_no] = max(dealer_parts[d_id].get(part_no, 0), qty)
        if row.get("stock_as_of"):
            dealer_stock_as_of[d_id] = row["stock_as_of"]
        elif d_id not in dealer_stock_as_of:
            dealer_stock_as_of[d_id] = None

    # --- Score each dealer ---------------------------------------------------
    candidates = []
    for d_id, coords in dealer_coords.items():
        inv = dealer_parts.get(d_id, {})
        if remedy_parts:
            in_stock = all(inv.get(p, 0) > 0 for p in remedy_parts)
        else:
            in_stock = True  # no parts required → always in stock

        if origin is not None:
            dist = _haversine_miles(origin[0], origin[1], coords[0], coords[1])
        else:
            dist = None

        candidates.append({
            "dealer_id": d_id,
            "distance_miles": dist,
            "in_stock": in_stock,
            "stock_as_of": dealer_stock_as_of.get(d_id),
        })

    # --- Sort: in-stock first, then by distance (None sorts last) -------------
    def _sort_key(c: dict) -> tuple:
        tier = 0 if c["in_stock"] else 1
        dist = c["distance_miles"] if c["distance_miles"] is not None else math.inf
        stable = c["dealer_id"]
        return (tier, dist, stable)

    if origin is None:
        # In-stock first, then dealer_id (no distance available).
        candidates.sort(key=lambda c: (0 if c["in_stock"] else 1, c["dealer_id"]))
    else:
        candidates.sort(key=_sort_key)

    return candidates[:3]
