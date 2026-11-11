"""Seed constants for fh_world.

Two fleets (A = demo-rental-carshare, B = demo-lastmile-delivery).
Named VINs carry:
  VIN_P0_A       -- fleet A, p0 open fault (P0AA6)
  VIN_ATTN_B     -- fleet B, attention open fault (P0A7F)
  VIN_PM_OVERDUE -- fleet A, calendar PM overdue (TIRE_ROTATION) via operator completion
  VIN_PM_DUE     -- fleet A, calendar PM due (BRAKE_INSPECTION) via operator completion
  VIN_PM_CURRENT -- fleet B, calendar PM no_history (TIRE_ROTATION, no completions)
  VIN_BATTERY_A  -- fleet A, battery Agent-2 data (energy_usage row)
  VIN_TIRE_A     -- fleet A, needs_replacement=true (tire Agent-2 data)
  VIN_BRAKE_B    -- fleet B, open brake-service outcome (brake Agent-2 data)
  VIN_PM_SR      -- fleet A, TIRE_ROTATION calendar current via service_records
  VIN_PM_DMS     -- fleet B, BRAKE_INSPECTION calendar current via DMS line (LAB-BRK-01)
  VIN_PM_MILES   -- fleet A, TIRE_ROTATION mileage due (rounding: 14499 total miles)
  VIN_PM_HV      -- fleet B, HV_BATTERY_CHECK calendar no_history (fault DMS op only)
"""
from __future__ import annotations

import datetime as _dt

FLEET_A = "demo-rental-carshare"
FLEET_B = "demo-lastmile-delivery"
FLEETS = [FLEET_A, FLEET_B]

STAGE = "staging"

TODAY = _dt.date(2026, 10, 19)   # fixed world date; matches DESIGN commit date


def vin(i: int) -> str:
    v = f"FHWLD01S0PC{i:06d}"
    assert len(v) == 17
    return v


# Named VINs (indices 1-12 to avoid overlap with fi_rw_world)
VIN_P0_A       = vin(1)   # fleet A, p0 fault open
VIN_ATTN_B     = vin(2)   # fleet B, attention fault open
VIN_PM_OVERDUE = vin(3)   # fleet A, PM overdue
VIN_PM_DUE     = vin(4)   # fleet A, PM due
VIN_PM_CURRENT = vin(5)   # fleet B, PM no_history
VIN_BATTERY_A  = vin(6)   # fleet A, battery Agent-2 row
VIN_TIRE_A     = vin(7)   # fleet A, tire needs_replacement=true
VIN_BRAKE_B    = vin(8)   # fleet B, open brake outcome
# Item-4 specific VINs (PM coverage extension)
VIN_PM_SR      = vin(9)   # fleet A, TIRE_ROTATION calendar current via service_records
VIN_PM_DMS     = vin(10)  # fleet B, BRAKE_INSPECTION calendar current via DMS LAB-BRK-01
VIN_PM_MILES   = vin(11)  # fleet A, TIRE_ROTATION mileage due (rounding case)
VIN_PM_HV      = vin(12)  # fleet B, HV_BATTERY_CHECK calendar no_history (fault DMS op only)
# W3: scoped plan VIN — covered only by PLN-008 (scoped EV/Azimuth/2023); no single-vehicle plan.
# PLN-008 completion 2026-04-22 + 180 d = 2026-10-19 = TODAY → "due" (< boundary, not <=).
VIN_PM_SCOPED  = vin(13)  # fleet A, TIRE_ROTATION scoped plan (PLN-008)
# W5: second tire pool VIN so the tires pool has 2 managed VINs.
VIN_TIRE_B     = vin(14)  # fleet A, tire needs_replacement=true (second pool member)

# All managed VINs and their fleets (VIN -> fleet_id)
VIN_FLEET: dict[str, str] = {
    VIN_P0_A:       FLEET_A,
    VIN_ATTN_B:     FLEET_B,
    VIN_PM_OVERDUE: FLEET_A,
    VIN_PM_DUE:     FLEET_A,
    VIN_PM_CURRENT: FLEET_B,
    VIN_BATTERY_A:  FLEET_A,
    VIN_TIRE_A:     FLEET_A,
    VIN_BRAKE_B:    FLEET_B,
    VIN_PM_SR:      FLEET_A,
    VIN_PM_DMS:     FLEET_B,
    VIN_PM_MILES:   FLEET_A,
    VIN_PM_HV:      FLEET_B,
    VIN_PM_SCOPED:  FLEET_A,
    VIN_TIRE_B:     FLEET_A,
}

# vehicleId overrides: these VINs have vehicleId != vin (as staging did before
# the 2026-10 migration — e.g. MRDN0000000000015 had a surrogate VEH- id).
# Covers item-2 vin-index->vehicleId hop (VIN_P0_A) and item-4 vehicle_id->vin hop
# (VIN_PM_DMS, PLN-005 single-vehicle plan).  All other VINs keep vehicleId == vin.
VEHICLE_IDS: dict[str, str] = {
    VIN_P0_A:   "VEH-FHWLD-001",   # item-2 fleet-A VIN
    VIN_PM_DMS: "VEH-FHWLD-DMS",   # item-4 PLN-005 single-vehicle plan
}

# Fault codes used in planted rows (must be present in ADP's fault_codes.yaml)
CODE_P0       = "P0AA6"   # band=p0
CODE_ATTN     = "P0A7F"   # band=attention
PM_CODE_TIRE  = "TIRE_ROTATION"
PM_CODE_BRAKE = "BRAKE_INSPECTION"
PM_CODE_HV    = "HV_BATTERY_CHECK"

# Directive text for P0AA6 from ADP catalog (must match verbatim)
P0_DIRECTIVE = (
    "Stop driving safely now. Do not restart the vehicle. "
    "Contact Meridian Roadside Assistance."
)
