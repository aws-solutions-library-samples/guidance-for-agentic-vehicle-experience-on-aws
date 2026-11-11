"""Typed inputs and outputs for the VSA deterministic triage classifier.

Pure data only — no I/O, no network, no AWS SDK. Kept dependency-light so the
classifier module can import this without pulling boto3 into unit tests.

See VSA_TRIAGE_CLASSIFIER.md for semantics.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any


class Level(IntEnum):
    """Priority ordering: P0 is worst (numerically lowest)."""

    P0 = 0
    P1 = 1
    P2 = 2
    P3 = 3


@dataclass(frozen=True)
class ActiveDtc:
    code: str
    first_seen_at: str  # ISO 8601
    persistent: bool = True


@dataclass(frozen=True)
class TelemetrySnapshot:
    coolant_temp_c: float | None = None
    tire_pressure_fl_psi: float | None = None
    tire_pressure_fr_psi: float | None = None
    tire_pressure_rl_psi: float | None = None
    tire_pressure_rr_psi: float | None = None
    tire_spec_psi: float = 34.0
    brake_pad_wear_mm_min: float | None = None
    battery_soc_pct: float | None = None
    oil_pressure_kpa: float | None = None
    oil_temp_c: float | None = None
    speed_mph: float | None = None
    abs_status_ok: bool | None = None
    last_update: str | None = None  # ISO 8601; None = STALE
    stale: bool = False


@dataclass(frozen=True)
class OpenRecall:
    nhtsa_id: str
    affected_systems: tuple[str, ...]
    remedy_available: bool = True


@dataclass(frozen=True)
class PredictiveSignal:
    id: str
    risk: str  # "low" | "medium" | "high"
    system: str


@dataclass(frozen=True)
class TenantThresholds:
    coolant_temp_p2_c: float = 105.0
    coolant_temp_p1_c: float = 115.0
    coolant_temp_p0_c: float = 125.0
    tire_pressure_p2_delta_psi: float = 4.0
    tire_pressure_p1_delta_psi: float = 8.0
    brake_pad_wear_p2_mm: float = 3.0
    brake_pad_wear_p1_mm: float = 1.5
    battery_soc_p2_pct: float = 20.0
    battery_soc_p1_pct: float = 10.0
    battery_soc_p0_pct: float = 5.0
    oil_temp_p2_c: float = 130.0
    oil_temp_p1_c: float = 140.0
    oil_temp_p0_c: float = 150.0


@dataclass(frozen=True)
class TenantEscalationRule:
    when: str  # expression, evaluated against classifier context
    action: str  # "force-tier2" | "force-warm-transfer" | "dispatch-roadside"
    reason: str
    rule_id: str


@dataclass(frozen=True)
class TenantTriageConfig:
    thresholds: TenantThresholds
    escalations: tuple[TenantEscalationRule, ...]
    predictive_weight: str = "none"  # "none" | "advisory" | "escalator-one-tier"
    # downgrade_forbidden is a constant contract; not configurable.


@dataclass(frozen=True)
class TriageContext:
    session_id: str
    tenant_id: str
    tenant_segment: str
    vin: str
    active_dtcs: tuple[ActiveDtc, ...]
    telemetry: TelemetrySnapshot
    open_recalls: tuple[OpenRecall, ...]
    predictive_signals: tuple[PredictiveSignal, ...]
    tenant_triage: TenantTriageConfig
    # Free-form per-tenant context used by tenant escalation expressions.
    # e.g., {"fleet_sla": "tier-1", "region": "TX"}
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ReasonFragment:
    input: str  # category: "baseline" | "dtc" | "telemetry" | "recall" | "predictive"
    value: str  # specific value: DTC code, signal name, recall ID
    level_contribution: Level
    weight: str  # "baseline" | "primary" | "secondary" | "escalator"
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ForcedAction:
    action: str
    reason: str
    rule_id: str


@dataclass(frozen=True)
class TriageDecision:
    session_id: str
    tenant_id: str
    vin: str
    classification: Level
    reason: tuple[ReasonFragment, ...]
    forced_actions: tuple[ForcedAction, ...]
    classifier_version: str
    decided_at: str  # ISO 8601
    inputs_snapshot: dict[str, Any]
