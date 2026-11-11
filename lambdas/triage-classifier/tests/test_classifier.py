"""Unit + replay tests for the VSA triage classifier.

Invoke:  pytest -q lambdas/triage-classifier/tests

Mutations verified 2026-10-01 against classifier.py and dtc_severity.yaml (each fails
at least one test here; C2-C6 and C8 survived this file before that day's additions):
  C1  (critical, not safe to drive) graded P1.
  C2  (high, not safe to drive) graded P1.
  C3  The least severe contribution sets the level.
  C4  Coolant P0 boundary made exclusive (> 125).
  C5  Battery SoC at or below 5% graded P1.
  C6  Oil temperature at or above 150 C graded P1.
  C7  ABS-off-while-moving rule deleted.
  C8  ABS rule requires speed > 5.
  C9  P0217 regraded high / limited in the table.
  C10 One-tier escalation (shared by the recall and predictive escalators) downgrades.
      Caught through the predictive path; the recall path never escalates today
      (docs/VSA_TRIAGE_CLASSIFIER.md, "Known gap (2026-09-26)").
See issues/2026-10-01-async-tests-inert-and-triage-tests-not-in-ci/.
"""
from __future__ import annotations

import importlib.util
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


def _register_triage_classifier_package() -> None:
    """Import the classifier SOURCE under the name the bundle gives it.

    The source directory is `lambdas/triage-classifier` (hyphen) and
    `scripts/build-triage-classifier.sh` renames it to `triage_classifier`, so
    `sys.path` alone cannot make `import triage_classifier` work. Until
    2026-10-01 this file relied on that and failed collection with
    ModuleNotFoundError, so none of its tests (including P0217 -> P0) ran
    anywhere (issues/2026-10-01-async-tests-inert-and-triage-tests-not-in-ci/).
    Same helper as lambdas/shared/tests/test_cms_client_real_active_dtcs.py.

    If `triage_classifier` is already registered (another test file did the same
    thing earlier in the run), that copy is reused. test_warmup.py imports the built
    bundle without leaving it in sys.modules, so it cannot be picked up here.
    """
    if "triage_classifier" in sys.modules:
        return
    pkg_dir = ROOT / "lambdas" / "triage-classifier"
    spec = importlib.util.spec_from_file_location(
        "triage_classifier", pkg_dir / "__init__.py",
        submodule_search_locations=[str(pkg_dir)],
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["triage_classifier"] = module
    spec.loader.exec_module(module)


_register_triage_classifier_package()

from triage_classifier.classifier import classify  # noqa: E402
from triage_classifier.types import (  # noqa: E402
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
        tenant_id="meridian-fleet",
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
        ("P0700", Level.P0),   # high, not safe to drive — the only high+"no" row
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


def _severity_rows():
    import yaml

    table = yaml.safe_load((ROOT / "lambdas" / "shared" / "dtc_severity.yaml").read_text())
    return sorted(table.items())


_SEVERITIES = {"critical", "high", "moderate", "low"}
_SAFE_TO_DRIVE = {"no", "limited", "yes"}


@pytest.mark.parametrize("code,row", _severity_rows())
def test_every_severity_row_is_well_formed(code, row):
    """Every row uses the exact strings the classifier maps.

    YAML reads an unquoted `no` or `yes` as a boolean. A row written
    `safe_to_drive: no` then misses the classifier's (severity, safe_to_drive) map
    and falls back to P2 ("safe to drive"), while also dropping out of the P0 test
    below. Found 2026-10-01 in review (W1). The fallback itself is a production-code
    question: issues/2026-10-01-classifier-malformed-severity-row-grades-p2/.
    """
    assert isinstance(row.get("severity"), str) and row["severity"] in _SEVERITIES, (code, row)
    assert isinstance(row.get("safe_to_drive"), str) and row["safe_to_drive"] in _SAFE_TO_DRIVE, (
        f"{code}: safe_to_drive={row.get('safe_to_drive')!r}; quote it (\"no\", \"yes\")"
    )
    assert isinstance(row.get("system"), str) and row["system"], (code, row)
    from triage_classifier.classifier import _DTC_TO_LEVEL

    assert (row["severity"], row["safe_to_drive"]) in _DTC_TO_LEVEL, (code, row)


_NOT_SAFE_TO_DRIVE = [
    code for code, row in _severity_rows()
    if row["safe_to_drive"] == "no" and row["severity"] in ("critical", "high")
]


def test_not_safe_to_drive_rows_exist():
    """Anti-vacuity for the parametrized test below: the table has such rows."""
    assert len(_NOT_SAFE_TO_DRIVE) >= 5, _NOT_SAFE_TO_DRIVE


@pytest.mark.parametrize("code", _NOT_SAFE_TO_DRIVE)
def test_every_not_safe_to_drive_code_is_p0(code):
    """Any table row graded critical or high AND not safe to drive classifies P0.

    Table-driven so a new row is covered without editing this file. Added 2026-10-01:
    mapping (high, "no") to P1 passed every other test in this file, because no test
    used P0700, the one such row.
    """
    d = classify(_ctx(active_dtcs=(ActiveDtc(code=code, first_seen_at="t"),)))
    assert d.classification == Level.P0


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


# ---------- P0 boundaries ----------------------------------------------------
#
# Added 2026-10-01 after mutation testing
# (issues/2026-10-01-async-tests-inert-and-triage-tests-not-in-ci/). Each of these
# mutations passed every earlier test in this file: an exclusive coolant boundary,
# battery and oil-temperature P0 regraded to P1, the ABS rule requiring speed > 5, and
# the least severe contribution winning instead of the most severe.


def test_coolant_at_the_p0_threshold_is_p0():
    # docs/VSA_TRIAGE_CLASSIFIER.md: P0 is ">= coolantTempP0C (default 125)".
    at = TelemetrySnapshot(coolant_temp_c=125, abs_status_ok=True, speed_mph=58)
    below = TelemetrySnapshot(coolant_temp_c=124.9, abs_status_ok=True, speed_mph=58)
    assert classify(_ctx(telemetry=at)).classification == Level.P0
    assert classify(_ctx(telemetry=below)).classification == Level.P1


def test_battery_soc_at_or_below_p0_threshold_is_p0():
    # The doc's P0 row says "<= 5% on highway"; the classifier grades P0 at <= 5%
    # whatever the speed, which never under-grades the documented case.
    for soc in (5, 2):
        t = TelemetrySnapshot(battery_soc_pct=soc, abs_status_ok=True, speed_mph=58)
        assert classify(_ctx(telemetry=t)).classification == Level.P0, soc
    t = TelemetrySnapshot(battery_soc_pct=6, abs_status_ok=True, speed_mph=58)
    assert classify(_ctx(telemetry=t)).classification == Level.P1


def test_oil_temp_at_the_p0_threshold_is_p0():
    at = TelemetrySnapshot(oil_temp_c=150, abs_status_ok=True, speed_mph=58)
    below = TelemetrySnapshot(oil_temp_c=149, abs_status_ok=True, speed_mph=58)
    assert classify(_ctx(telemetry=at)).classification == Level.P0
    assert classify(_ctx(telemetry=below)).classification == Level.P1


def test_abs_off_at_any_speed_above_zero_is_p0():
    # speed_mph is a float; a sub-1 mph crawl is still moving (review W2).
    for speed in (0.1, 1, 58):
        moving = TelemetrySnapshot(abs_status_ok=False, speed_mph=speed)
        assert classify(_ctx(telemetry=moving)).classification == Level.P0, speed
    parked = TelemetrySnapshot(abs_status_ok=False, speed_mph=0)
    assert classify(_ctx(telemetry=parked)).classification == Level.P3


def test_the_most_severe_contribution_sets_the_level():
    # A P0 DTC alongside a P2 telemetry reading, and a P0 reading alongside a P2 DTC.
    low_tire = TelemetrySnapshot(tire_pressure_fl_psi=30, abs_status_ok=True, speed_mph=58)
    hot = TelemetrySnapshot(coolant_temp_c=126, abs_status_ok=True, speed_mph=58)
    p0_dtc = (ActiveDtc(code="P0217", first_seen_at="t"),)
    p2_dtc = (ActiveDtc(code="P0420", first_seen_at="t"),)
    assert classify(_ctx(active_dtcs=p0_dtc, telemetry=low_tire)).classification == Level.P0
    assert classify(_ctx(active_dtcs=p2_dtc, telemetry=hot)).classification == Level.P0


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
                rule_id="meridian-fleet-tier1-warm",
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
