"""Item builders for the FI fleet-health agent (spec D9, contract §§ 1.4–1.7).

All headlines come from fixed templates; no model output touches headlines or
``disclosure_txt``.  The model may supply ``detail`` (booking items only), but
ALL other fields are server-set or come from the band table.

Public API
----------
build_disclosure_txt(fault_catalog_entry) -> Optional[str]
build_headline(item_type, severity, fault_code, need_description, rooftop_dealer_id) -> str
build_grounding_item(vin, attempt, fault_codes, last_sighting, catalog_entry, model_fn) -> dict
build_all_items(vin, attempt, open_faults, catalog_entries, agent2_findings,
                pm_tasks, already_grounded, fault_grounding_enabled,
                model_reply, candidate_dealer_ids) -> list[dict]
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Optional

from agents.fi_fleet_health.bands import classify_needs

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Fixed headline templates (contract § 1.4)
# ---------------------------------------------------------------------------
_HEADLINE_BOOKING_P0 = "Stop-driving fault {code}: book service at {rooftop}"
_HEADLINE_BOOKING_P0_NO_ROOFTOP = "Stop-driving fault {code}: choose a rooftop to book service"
_HEADLINE_BOOKING_ATTENTION = "Attention needed: book service at {rooftop}"
_HEADLINE_BOOKING_SOON = "Service needed soon: book service at {rooftop}"
_HEADLINE_BOOKING_ROUTINE = "PM due: book service at {rooftop}"
_HEADLINE_GROUNDING = "Stop-driving fault {code}: ground until the repair is recorded"
_HEADLINE_AFTER_5_ATTEMPTS = "Bookings keep failing — manual follow-up needed."

_BOOKING_HEADLINE_BY_SEVERITY: dict[str, str] = {
    "p0": _HEADLINE_BOOKING_P0,
    "attention": _HEADLINE_BOOKING_ATTENTION,
    "soon": _HEADLINE_BOOKING_SOON,
    "routine": _HEADLINE_BOOKING_ROUTINE,
}

_VALID_ITEM_TYPES = frozenset({"booking", "grounding"})


# ---------------------------------------------------------------------------
# build_disclosure_txt
# ---------------------------------------------------------------------------

def build_disclosure_txt(fault_catalog_entry: dict) -> Optional[str]:
    """Return the catalog directive byte for byte for p0 codes; None for others.

    Contract § 1.5: for a p0 item, ``disclosure_txt`` is the fault catalog's
    directive for the code, byte for byte.  Items below p0 carry no
    ``disclosure_txt``.

    Mutation guard: returning a paraphrase or a truncated string fails
    ``test_p0_booking_disclosure_txt_equals_catalog_directive`` and
    ``test_disclosure_txt_mutation_trim_fails``.
    """
    band = fault_catalog_entry.get("band", "")
    if band != "p0":
        return None
    return fault_catalog_entry.get("directive")


# ---------------------------------------------------------------------------
# build_headline
# ---------------------------------------------------------------------------

def build_headline(
    item_type: str,
    severity: str,
    fault_code: Optional[str],
    need_description: Optional[str],
    rooftop_dealer_id: Optional[str],
) -> str:
    """Build a fixed headline from the template table (contract § 1.4).

    Raises ValueError for an unknown item_type.

    Mutation guard: any implementation that lets model output supply the
    headline fails ``test_headline_booking_p0`` and siblings.
    """
    if item_type not in _VALID_ITEM_TYPES:
        raise ValueError(
            f"Unknown item_type {item_type!r}; expected one of {sorted(_VALID_ITEM_TYPES)}"
        )

    if item_type == "grounding":
        if fault_code is None:
            raise ValueError(
                "build_headline: fault_code must not be None for a grounding headline. "
                "Mutation guard: an Agent 2 p0 with no open fault code must be "
                "rejected by classify_needs before reaching the grounding builder."
            )
        return _HEADLINE_GROUNDING.format(code=fault_code)

    # booking
    template = _BOOKING_HEADLINE_BY_SEVERITY.get(severity)
    if template is None:
        raise ValueError(
            f"Unknown severity {severity!r} for booking headline; "
            f"expected one of {sorted(_BOOKING_HEADLINE_BY_SEVERITY)}"
        )
    # W6 (design answer 3): p0 with no rooftop gets a special headline so the
    # operator knows to choose one manually (contract § 1.4).
    if severity == "p0" and rooftop_dealer_id is None:
        if fault_code is None:
            raise ValueError(
                "build_headline: fault_code must not be None for a p0 booking headline. "
                "Mutation guard: W7 ensures Agent 2 p0 is capped at attention before "
                "reaching the p0 booking builder."
            )
        return _HEADLINE_BOOKING_P0_NO_ROOFTOP.format(code=fault_code)
    return template.format(code=fault_code, rooftop=rooftop_dealer_id)


# ---------------------------------------------------------------------------
# build_grounding_item
# ---------------------------------------------------------------------------

def build_grounding_item(
    vin: str,
    attempt: int,
    fault_codes: list,
    last_sighting: list,
    catalog_entry: dict,
    model_fn: Optional[Callable] = None,
) -> dict:
    """Build a grounding item.  The model_fn is NEVER called (spec D8 step 6).

    Contract § 1.5: grounding items receive fixed text and no model call.
    ``disclosure_txt`` is the catalog directive byte for byte.

    Parameters
    ----------
    vin:            Vehicle VIN.
    attempt:        Attempt number (≥ 1).
    fault_codes:    List of open p0 fault codes.
    last_sighting:  List of ``{"code": ..., "last_seen_at": ...}`` entries.
    catalog_entry:  Fault catalog entry for the primary p0 code (must have
                    ``band="p0"`` and ``directive``).
    model_fn:       Accepted but NEVER called — spec D8 step 6.

    Mutation guards:
    - Calling model_fn → ``test_grounding_item_gets_no_model_call`` fails.
    - Copying disclosure_txt from model output → byte-equality with directive fails.
    - Using first_sightings/first_seen instead of last_seen_at → test fails.
    """
    # model_fn is NEVER called
    primary_code = fault_codes[0] if fault_codes else catalog_entry.get("code", "")

    headline = _HEADLINE_GROUNDING.format(code=primary_code)
    disclosure = catalog_entry.get("directive")  # byte for byte

    item: dict[str, Any] = {
        "item_type": "grounding",
        "vin": vin,
        "attempt": attempt,
        "subject_key": f"ground/{attempt}",
        "severity": "p0",  # C2: grounding items are always p0 (only p0 triggers grounding)
        "headline": headline,
        "disclosure_txt": disclosure,
        "grounding_reason": {
            "fault_codes": list(fault_codes),
            "last_sighting": list(last_sighting),  # each entry has "code" + "last_seen_at"
        },
        "recommended_action": {
            "action_kind": "ground_vehicle",
            "target_system": "cms",
            "parameters": {
                "vehicle_vin": vin,
                "fault_codes": list(fault_codes),
            },
        },
    }
    return item


# ---------------------------------------------------------------------------
# build_all_items
# ---------------------------------------------------------------------------

def build_all_items(
    vin: str,
    attempt: int,
    open_faults: list,
    catalog_entries: dict,
    agent2_findings: list,
    pm_tasks: list,
    already_grounded: bool,
    fault_grounding_enabled: bool,
    model_reply: Optional[dict],
    candidate_dealer_ids: list,
) -> list:
    """Build the full item list for a vehicle run.

    The model_reply is IGNORED (spec D9: model cannot change severity, grounding,
    needs, disclosure_txt, or headlines).

    Returns a list of item dicts (booking + optionally grounding).

    Mutation guards:
    - Copy severity from model_reply → booking.severity changes → fails.
    - Copy disclosure_txt from model_reply → byte-equality with directive fails.
    - Copy headline from model_reply → mismatch with template fails.
    - Use model_reply add_needs → fabricated need appears → fails.
    """
    band_result = classify_needs(
        open_faults=open_faults,
        agent2_findings=agent2_findings,
        pm_tasks=pm_tasks,
        already_grounded=already_grounded,
        fault_grounding_enabled=fault_grounding_enabled,
        # model_hint is None — model_reply is not passed here
    )

    if not band_result.items:
        return []

    # Choose the candidate dealer (first in list by default; model may pick but
    # model_reply is ignored here — the caller (runner) uses model plan for dealer).
    chosen_dealer = candidate_dealer_ids[0] if candidate_dealer_ids else None

    # Determine the primary p0 code for headlines
    p0_code: Optional[str] = None
    p0_entry: Optional[dict] = None
    for fault in open_faults:
        if fault.get("band") == "p0":
            code = fault.get("code")
            if code and code in catalog_entries:
                p0_code = code
                p0_entry = catalog_entries[code]
                break
        # Handle entries passed without catalog_entries
        if fault.get("band") == "p0" and not p0_entry:
            p0_code = fault.get("code")

    items: list[dict] = []

    if "booking" in band_result.items:
        headline = build_headline(
            item_type="booking",
            severity=band_result.severity,
            fault_code=p0_code,
            need_description=None,
            rooftop_dealer_id=chosen_dealer,
        )
        # disclosure_txt only for p0 booking items (contract § 1.5)
        disclosure: Optional[str] = None
        if band_result.severity == "p0" and p0_entry:
            disclosure = build_disclosure_txt(p0_entry)

        booking: dict[str, Any] = {
            "item_type": "booking",
            "vin": vin,
            "attempt": attempt,
            "subject_key": f"service/{attempt}",
            "severity": band_result.severity,
            "headline": headline,
            "needs": list(band_result.needs),
            "recommended_action": {
                "action_kind": "book_fleet_service",
                "target_system": "dms",
                "parameters": {
                    "vehicle_vin": vin,
                    "needs": [],
                    "dealer_id": chosen_dealer,
                    "urgency": "this_week" if band_result.severity == "p0" else "this_month",
                },
            },
        }
        if disclosure is not None:
            booking["disclosure_txt"] = disclosure
        items.append(booking)

    if "grounding" in band_result.items and p0_code is not None and p0_entry is not None:
        # Build last_sighting from the p0 fault's last_seen_at
        last_sighting = []
        for fault in open_faults:
            if fault.get("band") == "p0" and fault.get("code") == p0_code:
                last_sighting.append({
                    "code": fault.get("code"),
                    "last_seen_at": fault.get("last_seen_at", ""),
                })

        grounding_item = build_grounding_item(
            vin=vin,
            attempt=attempt,
            fault_codes=[f.get("code") for f in open_faults if f.get("band") == "p0"],
            last_sighting=last_sighting,
            catalog_entry=p0_entry,
            model_fn=None,
        )
        items.append(grounding_item)

    return items
