#!/usr/bin/env python3
"""Offline check: the triage classifier grades each fault at least as severely as the platform.

Runs before every classifier bundle build (`scripts/build-triage-classifier.sh`), so a
table that would classify a fire as "safe to drive" cannot ship. No AWS access; needs
only pyyaml. The unit tests in `agents/supervisor/tests/test_dtc_severity_parity.py`
import the rules below and test them one code at a time.

Rules (issues/2026-09-26-classifier-grades-fire-and-crash-codes-p2/):
  1. Every code CVX's response catalog maps to a stop-driving template classifies P0.
  2. Every code in the CMS catalog snapshot classifies at least as severely as CMS grades it.
  3. The codes raised on their own evidence (EXTRA_P0, EXTRA_P1) stay raised.

Exit codes: 0 all rules hold · 1 a rule failed (each failure is printed).
"""
from __future__ import annotations

import importlib
import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "lambdas" / "triage-classifier"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agents.supervisor import dtc_response_catalog as cat  # noqa: E402

ORDER = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}

# Templates whose codes must classify P0. `pullover_safe_location` also ends "shut off
# the vehicle", but CVX binds it to codes CMS rates P1 (`P0299`, `C1201`) as well as to
# `P0A80` (P0), so it cannot imply P0 on its own.
STOP_DRIVING_TEMPLATES = frozenset({"exit_vehicle", "stay_call_911", "brake_emergency", "pullover_stop_engine"})

# The CMS event catalog's grade for each code it rates P0 or P1 (the more severe of its
# `severity_hint` and its numeric severity), read from `cms-staging-event-catalog` on
# 2026-09-26. A snapshot; `scripts/check-dtc-severity-drift.py` checks the live catalog.
CMS_CATALOG_GRADES = {
    "B0001": "P1", "B0001_FIRE": "P0", "B109B": "P1", "B1115": "P1", "B11D6": "P1",
    "B124D": "P1", "C004A": "P1", "C0091": "P1", "C0460": "P1", "C1001": "P1",
    "C1201": "P1", "C1234": "P0", "C1241": "P1", "C2007": "P1", "P0093": "P1",
    "P0118": "P1", "P0217": "P0", "P0219": "P1", "P0299": "P1", "P0300": "P1",
    "P0340": "P1", "P0606": "P1", "P0607": "P1", "P0700": "P0", "P0A80": "P0",
    "U0100": "P1", "U0101": "P1", "U0415": "P1", "U3000": "P0", "U3000_CRASH": "P0",
}

# Raised to P0 beyond the CMS catalog's grade. P0520: measured low oil pressure
# ("oilPressure < 15.0", CMS Flink processor); the CMS catalog says severity 2.
EXTRA_P0 = frozenset({"P0520"})

# Raised to P1 on the producer's own text where the CMS catalog's text is synthetic.
# B1419: "The restraints control module has detected a fault" (oem1-uds-dtc, CRITICAL).
EXTRA_P1 = frozenset({"B1419"})


def load_classifier():
    """The classifier source as a package under a private name (the directory is
    `triage-classifier`, not importable by name). Its table path resolves from the
    source tree, so this checks the table the build will ship."""
    name = "_triage_classifier_parity"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(
            name, SRC / "__init__.py", submodule_search_locations=[str(SRC)],
        )
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return importlib.import_module(f"{name}.classifier"), importlib.import_module(f"{name}.types")


def level_for(code: str) -> str:
    """The classification for a vehicle whose only input is this one active fault."""
    classifier, types = load_classifier()
    ctx = types.TriageContext(
        session_id="parity", tenant_id="t", tenant_segment="fleet", vin="VIN0PARITY0000001",
        active_dtcs=(types.ActiveDtc(code=code, first_seen_at="2026-09-26T00:00:00Z"),),
        telemetry=types.TelemetrySnapshot(), open_recalls=(), predictive_signals=(),
        tenant_triage=types.TenantTriageConfig(
            thresholds=types.TenantThresholds(), escalations=(), predictive_weight="none",
        ),
    )
    return classifier.classify(ctx).classification.name


def stop_driving_codes() -> list[str]:
    return sorted(c for c, m in cat.DTC_TO_TEMPLATE.items() if m.get("template") in STOP_DRIVING_TEMPLATES)


def floors() -> dict[str, str]:
    """The minimum grade for every code a rule names: the more severe where rules overlap."""
    out = dict(CMS_CATALOG_GRADES)
    for code in EXTRA_P1:
        out[code] = min(out.get(code, "P1"), "P1", key=ORDER.__getitem__)
    for code in EXTRA_P0:
        out[code] = "P0"
    return out


def failures() -> list[str]:
    out = []
    for code in stop_driving_codes():
        got = level_for(code)
        if got != "P0":
            out.append(f"{code}: CVX says {cat.DTC_TO_TEMPLATE[code]['template']!r}, classifier {got}")
    for code, grade in sorted(floors().items()):
        got = level_for(code)
        if ORDER[got] > ORDER[grade]:
            out.append(f"{code}: must be at least {grade}, classifier {got}")
    return out


def main() -> int:
    found = failures()
    for line in found:
        print(f"  PARITY {line}")
    if found:
        codes = {line.split(":", 1)[0] for line in found}
        print(f"FAIL: {len(codes)} code(s) under-graded; see lambdas/shared/dtc_severity.yaml")
        return 1
    print(f"OK: {len(stop_driving_codes())} stop-driving codes and {len(floors())} graded codes hold")
    return 0


if __name__ == "__main__":
    sys.exit(main())
