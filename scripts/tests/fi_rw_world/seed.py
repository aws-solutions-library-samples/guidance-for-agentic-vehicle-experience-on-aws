"""The world's data: a small, internally consistent staging.

Shapes follow the producers' contracts, not the script:
- ``recall_vehicle_status`` / ``recall_campaigns_received`` columns and types:
  ``glue get-table`` (``data/live_shapes.json``); semantics (population, CONFIRMED,
  remedied rules a and b, non-null ``managed_as_of``): ADP
  ``recall_campaigns/interface.md`` § 3.
- answer key parquet columns and the ``campaign_summary`` notes format: interface.md
  § 8c and the live object (columns ``vin, entity_kind, entity_id, expected_verdict,
  notes, planted_at``); the DMS seed file: § 8b and the live object.
- DMS rows: the real DMS handlers read them (roster and coordinates from DMS's own
  ``source.simulation.dealer_profiles``).

VINs are distinct from vehicleIds for one vehicle (``VEH-...``), as for 101 managed
vehicles in staging, and every field that usually coincides is varied somewhere.
"""
from __future__ import annotations

import datetime as _dt
from typing import Any

FLEETS = ["demo-rental-carshare", "demo-lastmile-delivery", "demo-field-service",
          "demo-city-public"]


def vin(i: int) -> str:
    v = f"MRDAZ01S0PC{i:06d}"
    assert len(v) == 17
    return v


# fleet of each VIN index; fleet A (most pending book items) is demo-rental-carshare.
VIN_FLEET: dict[int, str] = {**{i: FLEETS[0] for i in range(1, 11)},
                             11: FLEETS[1], 12: FLEETS[1], 13: FLEETS[1],
                             14: FLEETS[2], 15: FLEETS[2], 16: FLEETS[3], 17: FLEETS[3]}


def vehicle_id(i: int) -> str:
    return "VEH-WORLD-0010" if i == 10 else vin(i)


def campaigns(today: _dt.date) -> list[dict[str, Any]]:
    common = {"manufacturer": "Meridian", "models": ["Azimuth"], "model_year_min": 2023,
              "model_year_max": 2024, "signature_lookback_days": None,
              "remedy_flat_rate_hours": None, "catalog_version": "1.0.0"}
    return [
        {**common, "campaign_id": "MRD-2026-002", "component": "BRAKES",
         "summary": "Brake caliper bolts may loosen.",
         "consequence": "Reduced braking may increase the risk of a crash.",
         "remedy": "Dealers will inspect and torque the caliper bolts, free of charge.",
         "do_not_drive": False, "park_outside": False, "plants": ["CGA"],
         "build_from": _dt.date(2023, 1, 1), "build_to": _dt.date(2024, 6, 30),
         "fault_codes": ["C1A45"], "remedy_labor_op": "RECALL-002-BRK",
         "remedy_part_numbers": ["MRD-P-002"], "remedy_available_from": today - _dt.timedelta(days=30),
         "received_date": today - _dt.timedelta(days=20), "recall_class": "safety"},
        {**common, "campaign_id": "MRD-2026-004", "component": "HV BATTERY",
         "summary": "The high-voltage battery may overheat.",
         "consequence": "Do not drive the vehicle; a battery fire may occur while driving or parked.",
         "remedy": "Park outside away from structures. Dealers will replace the module, free of charge.",
         "do_not_drive": True, "park_outside": True, "plants": ["CGA"],
         "build_from": _dt.date(2023, 1, 1), "build_to": _dt.date(2023, 12, 31),
         "fault_codes": ["P0AB6"], "remedy_labor_op": "RECALL-004-HV",
         "remedy_part_numbers": ["MRD-P-004"], "remedy_available_from": today - _dt.timedelta(days=10),
         "received_date": today - _dt.timedelta(days=10), "recall_class": "safety"},
        {**common, "campaign_id": "MRD-2026-005", "component": "INFOTAINMENT",
         "summary": "The rear camera image may not display.",
         "consequence": "A missing rear image may reduce visibility when reversing.",
         "remedy": "Dealers will update the head-unit software, free of charge.",
         "do_not_drive": False, "park_outside": False, "plants": ["RNO"],
         "build_from": _dt.date(2024, 1, 1), "build_to": _dt.date(2024, 12, 31),
         "fault_codes": [], "remedy_labor_op": "RECALL-005-SW",
         "remedy_part_numbers": ["MRD-P-005"], "remedy_available_from": today + _dt.timedelta(days=100),
         "received_date": today - _dt.timedelta(days=5), "recall_class": "non_safety"},
    ]


# (campaign, vin index, match_basis, planted ADP remedy?)  — base of the view
VIEW_BASE: list[tuple[str, int, str, bool]] = (
    [("MRD-2026-004", i, "CONFIRMED" if i in (2, 3) else "POPULATION", i == 4)
     for i in (1, 2, 3, 4, 5, 6, 7, 8, 11, 14, 16)]
    + [("MRD-2026-002", i, "CONFIRMED" if i == 9 else "POPULATION", i == 6)
       for i in (5, 6, 7, 9, 12)]
    + [("MRD-2026-005", i, "POPULATION", False) for i in (1, 12, 15)]
)


def warranty_plants(today: _dt.date) -> list[dict[str, Any]]:
    """DMS seed file lines (interface.md § 8b). ``verdict_plant`` is the intended
    outcome; the world checks DMS's own ``validate_claim`` gives exactly that."""
    rows = [  # (vin index, labor_op, odometer_km or None, intended verdict, already claimed)
        (1, "EV-BATTERY-REPLACE", 42000, "approved", False),
        (2, "EV-MOTOR-REPLACE", 51000, "approved", False),
        (3, "EV-BATTERY-REPLACE", 30500, "approved", False),
        (5, "EV-MOTOR-REPLACE", 61000, "approved", False),
        (7, "EV-BATTERY-REPLACE", 44000, "approved", False),
        (8, "EV-BATTERY-REPLACE", None, "requires_review", False),  # live: no_odometer
        (11, "EV-BATTERY-REPLACE", 170000, "denied", False),
        (14, "EV-MOTOR-REPLACE", 171000, "denied", False),
        (16, "EV-BATTERY-REPLACE", 39000, "approved", True),
    ]
    out = []
    for n, (i, op, odo, verdict, claimed) in enumerate(rows, start=1):
        out.append({
            "vin": vin(i), "ro_id": f"RO-PLANT-WORLD{n:04d}", "dealer_id": "dealer-sacramento",
            "labor_op": op, "fault_code": "P1E00" if "BATTERY" in op else "P1A0F",
            "odometer_km": odo, "closed_date": (today - _dt.timedelta(days=3 + n)).isoformat(),
            "pay_type": "customer_pay", "verdict_plant": verdict, "already_claimed": claimed,
        })
    return out
