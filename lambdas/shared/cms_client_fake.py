"""In-process fixture data for VEH-0025 covering the three demo scenarios.

Scenarios are selected via env var VSA_SCENARIO:
  baseline  → no DTCs, all telemetry green                        (→ P3)
  p3-tire   → front-left tire at 32 PSI, within tolerance          (→ P3)
  p1-coolant → coolant 118C + DTC P0217 + open recall 26V123456    (→ P1)
  p0-brake  → DTC C1234 (critical, not safe) + ABS off at 58 mph   (→ P0)

Real CMS client will replace this once VSA can read from CMS us-east-1.
"""
from __future__ import annotations

import os

from triage_classifier.types import ActiveDtc, OpenRecall, TelemetrySnapshot

VEH_0025 = "1FMCU0F70MUA12345"

_GREEN = TelemetrySnapshot(
    coolant_temp_c=88.0,
    tire_pressure_fl_psi=34.0,
    tire_pressure_fr_psi=34.0,
    tire_pressure_rl_psi=34.0,
    tire_pressure_rr_psi=34.0,
    tire_spec_psi=34.0,
    brake_pad_wear_mm_min=6.0,
    battery_soc_pct=82.0,
    oil_pressure_kpa=350.0,
    oil_temp_c=105.0,
    speed_mph=58.0,
    abs_status_ok=True,
    last_update="2026-04-27T14:22:00Z",
    stale=False,
)


class FakeCmsClient:
    def __init__(self, scenario: str | None = None) -> None:
        self.scenario = scenario or os.environ.get("VSA_SCENARIO", "baseline")

    def get_telemetry(self, vin: str) -> TelemetrySnapshot:
        if vin != VEH_0025:
            return _GREEN
        if self.scenario == "p3-tire":
            return TelemetrySnapshot(**{**_GREEN.__dict__, "tire_pressure_fl_psi": 32.0})
        if self.scenario == "p1-coolant":
            return TelemetrySnapshot(**{**_GREEN.__dict__, "coolant_temp_c": 118.0})
        if self.scenario == "p0-brake":
            return TelemetrySnapshot(**{**_GREEN.__dict__, "abs_status_ok": False})
        return _GREEN

    def get_active_dtcs(self, vin: str) -> tuple[ActiveDtc, ...]:
        if vin != VEH_0025:
            return ()
        if self.scenario == "p1-coolant":
            return (ActiveDtc(code="P0217", first_seen_at="2026-04-27T14:22:05Z"),)
        if self.scenario == "p0-brake":
            return (ActiveDtc(code="C1234", first_seen_at="2026-04-27T14:22:10Z"),)
        return ()

    def get_open_recalls(self, vin: str) -> tuple[OpenRecall, ...]:
        if vin != VEH_0025:
            return ()
        if self.scenario == "p1-coolant":
            return (OpenRecall(nhtsa_id="26V123456", affected_systems=("powertrain",)),)
        return ()
