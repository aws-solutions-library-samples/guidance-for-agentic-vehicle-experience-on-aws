"""Vehicle eligibility filter (spec G4-D2).

Pure function; no AWS calls.  ``filter_eligible`` returns the subset of vehicles
that pass every eligibility rule, ranked by ``days_idle_last_7`` descending then VIN.
Records are returned unchanged (open_fi_items kept as evidence per D8 step 4).
"""
from __future__ import annotations

_DISQUALIFYING_SEVERITIES = {"p0", "attention"}


def _is_eligible(vehicle: dict) -> bool:
    """Return False if any single eligibility rule is violated (spec G4-D2)."""
    # Grounding (U7): only an explicit, successful "not grounded" passes. A read error, an
    # unresolved vehicleId (the reader sets grounding_read_error) or a missing or non-bool
    # flag counts as grounded, the safe direction for a move.
    if vehicle.get("grounding_read_error", False) is not False:
        return False
    if vehicle.get("is_grounded") is not False:
        return False

    # Open FI items: a failed read excludes; any p0 or attention item disqualifies.
    # W-n1: fail-closed on a missing key — no default, so absent → None → is not False
    # → ineligible, matching the grounding gate.  open_fi_items must be a list; a
    # missing key or a non-list value (e.g. None) also makes the vehicle ineligible.
    if vehicle.get("open_items_read_error") is not False:
        return False
    open_fi_items = vehicle.get("open_fi_items")
    if not isinstance(open_fi_items, list):
        return False
    for item in open_fi_items:
        if item.get("severity") in _DISQUALIFYING_SEVERITIES:
            return False

    # Booking within 24 h → ineligible.
    hours_to_booking = vehicle.get("hours_to_next_booking")
    if hours_to_booking is None or hours_to_booking < 24.0:
        return False

    # Service visit or open DMS repair order within 3 days (G4-D2). The reader folds an
    # open repair order into days_to_next_service = 0 and writes inf when nothing is
    # scheduled, so a missing value means the facts were not read: ineligible.
    days_to_service = vehicle.get("days_to_next_service")
    if days_to_service is None or days_to_service <= 3.0:
        return False

    # Must be in service (available_hours > 0 in the latest vehicle_days row).
    if vehicle.get("in_service") is not True:
        return False

    return True


def filter_eligible(vehicles: list[dict]) -> list[dict]:
    """Return eligible vehicles sorted by days_idle_last_7 desc, then VIN asc."""
    eligible = [v for v in vehicles if _is_eligible(v)]
    eligible.sort(key=lambda v: (-v.get("days_idle_last_7", 0), v.get("vin", "")))
    return eligible
