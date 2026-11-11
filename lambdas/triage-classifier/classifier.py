"""Deterministic VSA triage classifier.

Pure function: TriageContext -> TriageDecision. No I/O. The Lambda handler
does all fetching and writing around it.

Contract:
  - Same input → same output (deterministic).
  - Never downgrades a classification (property tested).
  - Recall and predictive inputs can only escalate, never downgrade.
  - Tenant rules add ForcedActions but cannot change the classification level.
  - LLM cannot mutate this output; the supervisor narrates it only.

See VSA_TRIAGE_CLASSIFIER.md §7 for the spec this implements.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml  # type: ignore

from .types import (
    ForcedAction,
    Level,
    ReasonFragment,
    TriageContext,
    TriageDecision,
)

CLASSIFIER_VERSION = "1.0.0"

_SEVERITY_TABLE_PATH = Path(__file__).parent.parent / "shared" / "dtc_severity.yaml"
_SEVERITY_TABLE: dict[str, dict[str, str]] = yaml.safe_load(_SEVERITY_TABLE_PATH.read_text()) or {}
_DEFAULT_DTC = {"severity": "moderate", "system": "unknown", "safe_to_drive": "yes"}

# (severity, safe_to_drive) -> Level
_DTC_TO_LEVEL: dict[tuple[str, str], Level] = {
    ("critical", "no"): Level.P0,
    ("critical", "limited"): Level.P1,
    ("critical", "yes"): Level.P1,
    ("high", "no"): Level.P0,
    ("high", "limited"): Level.P1,
    ("high", "yes"): Level.P1,
    ("moderate", "no"): Level.P1,
    ("moderate", "limited"): Level.P2,
    ("moderate", "yes"): Level.P2,
    ("low", "no"): Level.P2,
    ("low", "limited"): Level.P3,
    ("low", "yes"): Level.P3,
}


def _escalate_one_tier(level: Level) -> Level:
    return Level(max(int(Level.P0), int(level) - 1))


def _evaluate_tenant_rules(
    rules: tuple[Any, ...],  # TenantEscalationRule
    classification: Level,
    ctx: TriageContext,
) -> tuple[ForcedAction, ...]:
    """Minimal v1 expression evaluator — whitelist of known rule shapes.

    v1 supports only equality-and-AND expressions over a fixed set of variables.
    Full expression parser is a v2 deliverable.
    """
    actions: list[ForcedAction] = []
    eval_context = {
        "classification": classification.name,
        "tenantId": ctx.tenant_id,
        "segment": ctx.tenant_segment,
        **{k: v for k, v in ctx.extra.items()},
    }
    for rule in rules:
        if _matches(rule.when, eval_context):
            actions.append(ForcedAction(action=rule.action, reason=rule.reason, rule_id=rule.rule_id))
    return tuple(actions)


def _matches(expression: str, context: dict[str, Any]) -> bool:
    """Evaluate simple expressions of form:
      key == 'value'
      key == 'value' && other == 'value'
    Anything more complex raises (fail loud — tenant should use structured rules).
    """
    parts = [p.strip() for p in expression.split("&&")]
    for part in parts:
        if "==" not in part:
            return False
        lhs, rhs = part.split("==", 1)
        key = lhs.strip()
        # Map camelCase rule keys to snake_case context keys.
        key_snake = "".join("_" + c.lower() if c.isupper() else c for c in key).lstrip("_")
        actual = context.get(key) or context.get(key_snake)
        expected = rhs.strip().strip("'\"")
        if actual != expected:
            return False
    return True


def _classify_dtcs(
    active_dtcs: tuple[Any, ...],  # ActiveDtc
) -> list[tuple[Level, ReasonFragment]]:
    contributions: list[tuple[Level, ReasonFragment]] = []
    for dtc in active_dtcs:
        sev = _SEVERITY_TABLE.get(dtc.code, _DEFAULT_DTC)
        level = _DTC_TO_LEVEL.get(
            (sev["severity"], sev["safe_to_drive"]),
            Level.P2,
        )
        contributions.append(
            (
                level,
                ReasonFragment(
                    input="dtc",
                    value=dtc.code,
                    level_contribution=level,
                    weight="primary",  # set correctly after all contributions collected
                    extra={
                        "severity": sev["severity"],
                        "system": sev["system"],
                        "safeToDrive": sev["safe_to_drive"],
                    },
                ),
            )
        )
    return contributions


def _classify_telemetry(t: Any, thresholds: Any) -> list[tuple[Level, ReasonFragment]]:
    contributions: list[tuple[Level, ReasonFragment]] = []
    if t.stale:
        return contributions

    def _add(level: Level, signal: str, value: float, threshold: float) -> None:
        contributions.append(
            (
                level,
                ReasonFragment(
                    input="telemetry",
                    value=f"{signal}={value}",
                    level_contribution=level,
                    weight="primary",
                    extra={"threshold": threshold},
                ),
            )
        )

    if t.coolant_temp_c is not None:
        if t.coolant_temp_c >= thresholds.coolant_temp_p0_c:
            _add(Level.P0, "coolant_temp_c", t.coolant_temp_c, thresholds.coolant_temp_p0_c)
        elif t.coolant_temp_c >= thresholds.coolant_temp_p1_c:
            _add(Level.P1, "coolant_temp_c", t.coolant_temp_c, thresholds.coolant_temp_p1_c)
        elif t.coolant_temp_c >= thresholds.coolant_temp_p2_c:
            _add(Level.P2, "coolant_temp_c", t.coolant_temp_c, thresholds.coolant_temp_p2_c)

    for label, value in [
        ("tire_pressure_fl", t.tire_pressure_fl_psi),
        ("tire_pressure_fr", t.tire_pressure_fr_psi),
        ("tire_pressure_rl", t.tire_pressure_rl_psi),
        ("tire_pressure_rr", t.tire_pressure_rr_psi),
    ]:
        if value is None:
            continue
        delta = t.tire_spec_psi - value
        if delta >= thresholds.tire_pressure_p1_delta_psi:
            _add(Level.P1, label, value, t.tire_spec_psi - thresholds.tire_pressure_p1_delta_psi)
        elif delta >= thresholds.tire_pressure_p2_delta_psi:
            _add(Level.P2, label, value, t.tire_spec_psi - thresholds.tire_pressure_p2_delta_psi)

    if t.brake_pad_wear_mm_min is not None:
        if t.brake_pad_wear_mm_min <= thresholds.brake_pad_wear_p1_mm:
            _add(Level.P1, "brake_pad_wear_mm", t.brake_pad_wear_mm_min, thresholds.brake_pad_wear_p1_mm)
        elif t.brake_pad_wear_mm_min <= thresholds.brake_pad_wear_p2_mm:
            _add(Level.P2, "brake_pad_wear_mm", t.brake_pad_wear_mm_min, thresholds.brake_pad_wear_p2_mm)

    if t.battery_soc_pct is not None:
        if t.battery_soc_pct <= thresholds.battery_soc_p0_pct:
            _add(Level.P0, "battery_soc_pct", t.battery_soc_pct, thresholds.battery_soc_p0_pct)
        elif t.battery_soc_pct <= thresholds.battery_soc_p1_pct:
            _add(Level.P1, "battery_soc_pct", t.battery_soc_pct, thresholds.battery_soc_p1_pct)
        elif t.battery_soc_pct <= thresholds.battery_soc_p2_pct:
            _add(Level.P2, "battery_soc_pct", t.battery_soc_pct, thresholds.battery_soc_p2_pct)

    if t.oil_temp_c is not None:
        if t.oil_temp_c >= thresholds.oil_temp_p0_c:
            _add(Level.P0, "oil_temp_c", t.oil_temp_c, thresholds.oil_temp_p0_c)
        elif t.oil_temp_c >= thresholds.oil_temp_p1_c:
            _add(Level.P1, "oil_temp_c", t.oil_temp_c, thresholds.oil_temp_p1_c)
        elif t.oil_temp_c >= thresholds.oil_temp_p2_c:
            _add(Level.P2, "oil_temp_c", t.oil_temp_c, thresholds.oil_temp_p2_c)

    # ABS off while vehicle is moving → P0 (treat as secondary contribution to brake DTCs).
    if t.abs_status_ok is False and (t.speed_mph or 0) > 0:
        contributions.append(
            (
                Level.P0,
                ReasonFragment(
                    input="telemetry",
                    value="abs_status_ok=false",
                    level_contribution=Level.P0,
                    weight="primary",
                ),
            )
        )

    return contributions


def classify(ctx: TriageContext) -> TriageDecision:
    trace: list[ReasonFragment] = [
        ReasonFragment(
            input="baseline",
            value="default",
            level_contribution=Level.P3,
            weight="baseline",
        )
    ]

    contributions: list[tuple[Level, ReasonFragment]] = []
    contributions.extend(_classify_dtcs(ctx.active_dtcs))
    contributions.extend(_classify_telemetry(ctx.telemetry, ctx.tenant_triage.thresholds))

    if contributions:
        primary_level = Level(min(int(lvl) for lvl, _ in contributions))
        # Re-emit contributions with correct weights.
        for level, frag in contributions:
            weight = "primary" if level == primary_level else "secondary"
            trace.append(
                ReasonFragment(
                    input=frag.input,
                    value=frag.value,
                    level_contribution=frag.level_contribution,
                    weight=weight,
                    extra=frag.extra,
                )
            )
    else:
        primary_level = Level.P3

    # Recall escalator — escalates only when no primary DTC already implicates
    # the same system (avoids double-counting: DTC + recall for same system
    # doesn't bump the classification further).
    dtc_primary_systems = {
        c.extra.get("system")
        for lvl, c in contributions
        if c.input == "dtc" and lvl == primary_level and c.extra.get("system")
    }
    other_implicated_systems = {
        c.extra.get("system")
        for _, c in contributions
        if c.input != "dtc" and "system" in c.extra
    } - dtc_primary_systems
    for recall in ctx.open_recalls:
        overlaps_other = set(recall.affected_systems) & other_implicated_systems
        overlaps_primary_dtc = set(recall.affected_systems) & dtc_primary_systems
        # Escalate when recall overlaps a non-DTC contribution (telemetry only),
        # not when the primary DTC already implicated that system.
        if overlaps_other and not overlaps_primary_dtc:
            escalated = _escalate_one_tier(primary_level)
            if escalated < primary_level:
                trace.append(
                    ReasonFragment(
                        input="recall",
                        value=recall.nhtsa_id,
                        level_contribution=escalated,
                        weight="escalator",
                        extra={"affectedSystems": list(recall.affected_systems)},
                    )
                )
                primary_level = escalated
                break

    # Predictive escalator — tenant-gated.
    if ctx.tenant_triage.predictive_weight == "escalator-one-tier":
        high_risk = [p for p in ctx.predictive_signals if p.risk == "high"]
        if high_risk:
            escalated = _escalate_one_tier(primary_level)
            if escalated < primary_level:
                trace.append(
                    ReasonFragment(
                        input="predictive",
                        value=high_risk[0].id,
                        level_contribution=escalated,
                        weight="escalator",
                        extra={"risk": high_risk[0].risk, "system": high_risk[0].system},
                    )
                )
                primary_level = escalated
    elif ctx.tenant_triage.predictive_weight == "advisory":
        for p in ctx.predictive_signals:
            trace.append(
                ReasonFragment(
                    input="predictive",
                    value=p.id,
                    level_contribution=primary_level,
                    weight="advisory",
                    extra={"risk": p.risk, "system": p.system},
                )
            )

    forced = _evaluate_tenant_rules(ctx.tenant_triage.escalations, primary_level, ctx)

    return TriageDecision(
        session_id=ctx.session_id,
        tenant_id=ctx.tenant_id,
        vin=ctx.vin,
        classification=primary_level,
        reason=tuple(trace),
        forced_actions=forced,
        classifier_version=CLASSIFIER_VERSION,
        decided_at=datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        inputs_snapshot={
            "activeDtcs": [d.code for d in ctx.active_dtcs],
            "openRecalls": [r.nhtsa_id for r in ctx.open_recalls],
            "telemetry": {
                "coolant_temp_c": ctx.telemetry.coolant_temp_c,
                "tire_pressure_fl_psi": ctx.telemetry.tire_pressure_fl_psi,
                "brake_pad_wear_mm_min": ctx.telemetry.brake_pad_wear_mm_min,
                "battery_soc_pct": ctx.telemetry.battery_soc_pct,
                "speed_mph": ctx.telemetry.speed_mph,
                "abs_status_ok": ctx.telemetry.abs_status_ok,
                "stale": ctx.telemetry.stale,
            },
            "predictiveSignals": [p.id for p in ctx.predictive_signals],
        },
    )
