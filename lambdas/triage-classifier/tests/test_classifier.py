"""Unit + replay tests for the VSA triage classifier.

Invoke:  pytest -q lambdas/triage-classifier/tests
"""
from __future__ import annotations

import json
import random
import sys
import time
from pathlib import Path

# Make `lambdas/` importable as a source root.
HERE = Path(__file__).resolve()
ROOT = HERE.parents[3]
sys.path.insert(0, str(ROOT / "lambdas"))

import pytest

from triage_classifier.classifier import classify
from triage_classifier.types import (
    ActiveDtc,
    Level,
    OpenRecall,
    PredictiveSignal,
    TelemetrySnapshot,
    TenantEscalationRule,
    TenantThresholds,
    TenantTriageConfig,
    TriageContext,
)

CORPUS_PATH = ROOT / "corpora" / "triage-replay-v1.jsonl"

DEFAULT_TRIAGE = TenantTriageConfig(
    thresholds=TenantThresholds(),
    escalations=(),
    predictive_weight="none",
)


def _ctx(**kwargs) -> TriageContext:
    defaults = dict(
        session_id="sess-test",
        tenant_id="michelin",
        tenant_segment="fleet",
        vin="1FMCU0F70MUA12345",
        active_dtcs=(),
        telemetry=TelemetrySnapshot(
            coolant_temp_c=88,
            tire_pressure_fl_psi=34,
            tire_pressure_fr_psi=34,
            tire_pressure_rl_psi=34,
            tire_pressure_rr_psi=34,
            brake_pad_wear_mm_min=6,
            abs_status_ok=True,
            speed_mph=58,
        ),
        open_recalls=(),
        predictive_signals=(),
        tenant_triage=DEFAULT_TRIAGE,
        extra={},
    )
    defaults.update(kwargs)
    return TriageContext(**defaults)


# ---------- Sanity -----------------------------------------------------------


def test_baseline_is_p3():
    assert classify(_ctx()).classification == Level.P3


# ---------- DTC severity table ----------------------------------------------


@pytest.mark.parametrize(
    "code,expected",
    [
        ("P0217", Level.P0),   # critical, not safe to drive (flipped 2026-04-30 for VSA demo)
        ("C1234", Level.P0),   # critical, not safe to drive
        ("P0300", Level.P1),   # high, limited
        ("P0606", Level.P1),   # high, limited — PCM processor fault
        ("P0420", Level.P2),   # moderate
        ("B1000", Level.P3),   # low
        ("ZZZ999", Level.P2),  # unknown → default moderate → P2
    ],
)
def test_dtc_severity_mapping(code, expected):
    d = classify(_ctx(active_dtcs=(ActiveDtc(code=code, first_seen_at="t"),)))
    assert d.classification == expected


# ---------- Telemetry thresholds ---------------------------------------------


def test_coolant_thresholds():
    t = TelemetrySnapshot(coolant_temp_c=118, abs_status_ok=True, speed_mph=58)
    assert classify(_ctx(telemetry=t)).classification == Level.P1
    t = TelemetrySnapshot(coolant_temp_c=126, abs_status_ok=True, speed_mph=58)
    assert classify(_ctx(telemetry=t)).classification == Level.P0


def test_tire_pressure_thresholds():
    base = dict(coolant_temp_c=88, tire_pressure_fr_psi=34, tire_pressure_rl_psi=34,
                tire_pressure_rr_psi=34, brake_pad_wear_mm_min=6, abs_status_ok=True, speed_mph=58)
    assert classify(_ctx(telemetry=TelemetrySnapshot(**base, tire_pressure_fl_psi=32))).classification == Level.P3
    assert classify(_ctx(telemetry=TelemetrySnapshot(**base, tire_pressure_fl_psi=30))).classification == Level.P2
    assert classify(_ctx(telemetry=TelemetrySnapshot(**base, tire_pressure_fl_psi=26))).classification == Level.P1


def test_abs_off_while_moving_is_p0():
    t = TelemetrySnapshot(coolant_temp_c=88, abs_status_ok=False, speed_mph=58)
    assert classify(_ctx(telemetry=t)).classification == Level.P0


# ---------- Recall escalator -------------------------------------------------


def test_recall_does_not_double_count_same_system_as_primary_dtc():
    # DTC P0606 high+limited (powertrain) → P1.
    # Open recall in powertrain → does NOT escalate (already implicated by DTC).
    # (Previously used P0217 here; post-2026-04-30 P0217 is P0, so we use P0606
    # to keep this a P1 scenario exercising the recall-no-double-count path.)
    ctx = _ctx(
        active_dtcs=(ActiveDtc(code="P0606", first_seen_at="t"),),
        open_recalls=(OpenRecall(nhtsa_id="26V123456", affected_systems=("powertrain",)),),
    )
    assert classify(ctx).classification == Level.P1


def test_recall_does_not_escalate_when_no_overlap():
    ctx = _ctx(
        active_dtcs=(ActiveDtc(code="P0420", first_seen_at="t"),),
        open_recalls=(OpenRecall(nhtsa_id="nhtsa-x", affected_systems=("body",)),),
    )
    assert classify(ctx).classification == Level.P2


# ---------- Predictive escalator --------------------------------------------


def test_predictive_escalator_only_when_tenant_opts_in():
    # P2 baseline from tire pressure, predictive high-risk, tenant weight=none → stays P2.
    t = TelemetrySnapshot(coolant_temp_c=88, tire_pressure_fl_psi=30, tire_pressure_fr_psi=34,
                          tire_pressure_rl_psi=34, tire_pressure_rr_psi=34,
                          brake_pad_wear_mm_min=6, abs_status_ok=True, speed_mph=58)
    preds = (PredictiveSignal(id="tl-7d", risk="high", system="chassis"),)
    assert classify(_ctx(telemetry=t, predictive_signals=preds)).classification == Level.P2
    # Tenant opts in → escalates to P1.
    tc = TenantTriageConfig(thresholds=TenantThresholds(), escalations=(), predictive_weight="escalator-one-tier")
    assert classify(_ctx(telemetry=t, predictive_signals=preds, tenant_triage=tc)).classification == Level.P1


# ---------- Tenant rules -----------------------------------------------------


def test_tenant_rule_adds_forced_action_without_changing_level():
    tc = TenantTriageConfig(
        thresholds=TenantThresholds(),
        escalations=(
            TenantEscalationRule(
                when="classification == 'P1' && fleet_sla == 'tier-1'",
                action="force-warm-transfer",
                reason="Tier-1 SLA",
                rule_id="michelin-tier1-warm",
            ),
        ),
        predictive_weight="none",
    )
    ctx = _ctx(
        active_dtcs=(ActiveDtc(code="P0606", first_seen_at="t"),),
        tenant_triage=tc,
        extra={"fleet_sla": "tier-1"},
    )
    d = classify(ctx)
    assert d.classification == Level.P1  # level unchanged
    assert any(f.action == "force-warm-transfer" for f in d.forced_actions)


# ---------- Property: no downgrade ever --------------------------------------


def test_recall_cannot_downgrade():
    random.seed(42)
    levels_seen = []
    for _ in range(200):
        dtcs = random.choice([
            (),
            (ActiveDtc(code="P0217", first_seen_at="t"),),
            (ActiveDtc(code="C1234", first_seen_at="t"),),
            (ActiveDtc(code="P0420", first_seen_at="t"),),
        ])
        recalls = random.choice([
            (),
            (OpenRecall(nhtsa_id="x", affected_systems=("powertrain",)),),
            (OpenRecall(nhtsa_id="y", affected_systems=("brakes",)),),
            (OpenRecall(nhtsa_id="z", affected_systems=("body",)),),
        ])
        without = classify(_ctx(active_dtcs=dtcs)).classification
        with_recall = classify(_ctx(active_dtcs=dtcs, open_recalls=recalls)).classification
        assert int(with_recall) <= int(without), f"Recall downgraded: {without} → {with_recall}"
        levels_seen.append(with_recall)
    assert {Level.P0, Level.P1, Level.P2, Level.P3} & set(levels_seen)


# ---------- Replay corpus (100%) ---------------------------------------------


def _build_context_from_corpus_row(row: dict) -> TriageContext:
    dtcs = tuple(ActiveDtc(**d) for d in row.get("active_dtcs", []))
    t = row.get("telemetry") or {}
    telem = TelemetrySnapshot(
        coolant_temp_c=t.get("coolant_temp_c"),
        tire_pressure_fl_psi=t.get("tire_pressure_fl_psi"),
        tire_pressure_fr_psi=t.get("tire_pressure_fr_psi"),
        tire_pressure_rl_psi=t.get("tire_pressure_rl_psi"),
        tire_pressure_rr_psi=t.get("tire_pressure_rr_psi"),
        tire_spec_psi=t.get("tire_spec_psi", 34),
        brake_pad_wear_mm_min=t.get("brake_pad_wear_mm_min"),
        battery_soc_pct=t.get("battery_soc_pct"),
        oil_temp_c=t.get("oil_temp_c"),
        abs_status_ok=t.get("abs_status_ok"),
        speed_mph=t.get("speed_mph"),
    )
    recalls = tuple(
        OpenRecall(nhtsa_id=r["nhtsa_id"], affected_systems=tuple(r["affected_systems"]))
        for r in row.get("open_recalls", [])
    )
    preds = tuple(PredictiveSignal(**p) for p in row.get("predictive_signals", []))
    tc = TenantTriageConfig(
        thresholds=TenantThresholds(),
        escalations=(),
        predictive_weight=row.get("predictive_weight", "none"),
    )
    return _ctx(active_dtcs=dtcs, telemetry=telem, open_recalls=recalls,
                predictive_signals=preds, tenant_triage=tc)


def test_replay_corpus_passes():
    rows = [json.loads(line) for line in CORPUS_PATH.read_text().splitlines() if line.strip()]
    assert rows, "corpus is empty"
    for row in rows:
        ctx = _build_context_from_corpus_row(row)
        d = classify(ctx)
        assert d.classification.name == row["expected"], f"{row['id']}: expected {row['expected']} got {d.classification.name} — {row['description']}"


# ---------- Latency budget ---------------------------------------------------


def test_classifier_p99_under_300ms():
    ctx = _ctx(
        active_dtcs=(ActiveDtc(code="P0217", first_seen_at="t"),),
        open_recalls=(OpenRecall(nhtsa_id="x", affected_systems=("powertrain",)),),
    )
    samples_ms = []
    for _ in range(200):
        start = time.perf_counter()
        classify(ctx)
        samples_ms.append((time.perf_counter() - start) * 1000)
    samples_ms.sort()
    p99 = samples_ms[int(len(samples_ms) * 0.99) - 1]
    assert p99 < 300, f"P99 {p99:.1f}ms exceeds 300ms budget"
