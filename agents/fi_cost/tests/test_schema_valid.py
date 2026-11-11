"""
Frozen Finding schema tests for fi_cost Tier 2 component (W5 fix).

FIX1_CALLS item 9: "a test validates every built Finding (anomaly and sell_timing)
against the frozen Finding schema the writer uses. `agent_version` is one semver
constant and the playbook's header carries the same version (a test pins they're
equal), so a playbook change bumps `agent_version`."

Spec: .kiro/specs/2026-10-02-fi-cost-stage-b/spec.md § D8, § D10
Schema: agents/tier2/schemas/finding.schema.json

Tests:
  S1 — AGENT_VERSION matches PLAYBOOK_VERSION (both equal the playbook's semver).
  S2 — AGENT_VERSION matches the schema's pattern ^[0-9]+\.[0-9]+\.[0-9]+$.
  S3 — A fully-filled anomaly Finding validates against the frozen schema.
  S4 — A fully-filled sell_timing Finding validates against the frozen schema.
  S5 — evidence entries carry all required fields (source, source_ref, computed_at).
"""
from __future__ import annotations

import json
import re
import uuid
from pathlib import Path
from typing import Any

import jsonschema
import pytest

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
_SCHEMA_PATH = (
    Path(__file__).parent.parent.parent / "tier2" / "schemas" / "finding.schema.json"
)
_FINDING_SCHEMA: dict[str, Any] = json.loads(_SCHEMA_PATH.read_text())

# Stable test hash (64 hex chars).
_TEST_HASH = "a" * 64
_TEST_FINDING_ID = f"FIND-{uuid.UUID('12345678-1234-5678-1234-567812345678')}"
_TEST_FINDING_KEY = "b" * 64
_NOW = "2026-10-05T12:00:00Z"
_EXPIRES = "2026-11-09T12:00:00Z"

# Shared evidence fixture for the anomaly Finding — shaped like the D8 bundle.
_EVIDENCE_ANOMALY = {
    "vin": "MRDSCHEMA001",
    "fleet_id": "fleet-meridian-schema",
    "year_month": "2026-09",
    "cost.month": "2026-09",
    "cost.cost_per_mile": 0.45,
    "cost.own_baseline": 0.30,
    "cost.cohort_baseline": 0.28,
    "cost.method": "own 6-month mean and fleet-model median",
    "cost.flag": "total",
    "severity": "soon",
    "energy.public_and_peak_kwh": 400.0,
    "energy.mean_public_peak_price_per_kwh": 0.38,
    "energy.depot_off_peak_price_per_kwh": 0.09,
    "energy.mean_price_per_kwh": 0.35,
    "energy.cohort_mean_price_per_kwh": 0.15,
    "energy.public_share": 0.75,
    "energy.cohort_public_share": 0.30,
    "energy.peak_share": 0.40,
    "energy.cohort_peak_share": 0.15,
    "energy.dc_fast_share": 0.60,
    "energy.cohort_dc_fast_share": 0.25,
    "delta.energy_usd": 300.0,
    "delta.maintenance_usd": 48.0,
    "delta.lease_usd": 20.0,
    "delta.insurance_usd": 10.0,
}

_MODEL_REPLY_ANOMALY = {
    "cause": "charging_rate",
    "action": "shift_to_depot_off_peak",
    "evidence_refs": ["energy.public_and_peak_kwh", "energy.mean_price_per_kwh"],
    "headline": "High public/peak charging costs above fleet median",
    "detail": (
        "Vehicle charges 75% at public DC-fast and depot-peak; "
        "shifting to depot off-peak saves an estimated $116/month."
    ),
    "confidence": "high",
}


def _build_anomaly_finding() -> dict[str, Any]:
    """Build a complete anomaly Finding with the fields Group 4 would add."""
    from agents.fi_cost.diagnose import run_cost_diagnosis, AGENT_VERSION

    finding = run_cost_diagnosis(
        evidence=_EVIDENCE_ANOMALY,
        model_reply=_MODEL_REPLY_ANOMALY,
    )
    # Fields added by Group 4 (process_vehicle / agent run loop)
    finding["finding_id"] = _TEST_FINDING_ID
    finding["finding_key"] = _TEST_FINDING_KEY
    finding["computed_at"] = _NOW
    finding["expires_at"] = _EXPIRES
    finding["inputs_hash"] = _TEST_HASH
    finding["tokens_used"] = 120
    finding["cost_usd"] = 0.00186  # 80 × 3/M + 40 × 15/M
    finding["audit_trail"] = []
    return finding


def _build_sell_timing_finding() -> dict[str, Any]:
    """Build a complete sell_timing Finding with the fields Group 4 would add."""
    from agents.fi_cost.sell_timing import build_sell_timing_finding

    finding = build_sell_timing_finding(
        vin="MRDSCHEMA002",
        fleet_id="fleet-meridian-schema",
        crossover_month="2026-12",
        purchase_price=45000.0,
        monthly_depreciation=375.0,
        year_month="2026-09",
    )
    # Group 4 fills inputs_hash
    finding["inputs_hash"] = _TEST_HASH
    return finding


# ---------------------------------------------------------------------------
# S1 — AGENT_VERSION == PLAYBOOK_VERSION
# ---------------------------------------------------------------------------


def test_agent_version_equals_playbook_version():
    """AGENT_VERSION must equal PLAYBOOK_VERSION (FIX1_CALLS item 9).

    A playbook change bumps the semver in the playbook header, which AGENT_VERSION
    reads from.  A test that pins they are equal ensures a playbook bump propagates
    to every Finding's agent_version field.

    Previously AGENT_VERSION was 'agent-fi-cost/playbook:1.0.0' which:
    (a) did not equal PLAYBOOK_VERSION ('1.0.0'), and
    (b) did not match the schema's ^[0-9]+\.[0-9]+\.[0-9]+$ pattern.
    """
    from agents.fi_cost.diagnose import AGENT_VERSION, PLAYBOOK_VERSION

    assert AGENT_VERSION == PLAYBOOK_VERSION, (
        f"AGENT_VERSION={AGENT_VERSION!r} must equal PLAYBOOK_VERSION={PLAYBOOK_VERSION!r}. "
        "A playbook change must bump agent_version (they are pinned equal)."
    )


# ---------------------------------------------------------------------------
# S2 — AGENT_VERSION matches schema pattern
# ---------------------------------------------------------------------------


def test_agent_version_is_semver():
    """AGENT_VERSION must match ^[0-9]+\.[0-9]+\.[0-9]+$ (frozen schema constraint).

    The frozen Finding schema's agent_version field requires a semver string.
    The old format 'agent-fi-cost/playbook:1.0.0' fails this pattern.
    """
    from agents.fi_cost.diagnose import AGENT_VERSION

    pattern = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
    assert pattern.match(AGENT_VERSION), (
        f"AGENT_VERSION={AGENT_VERSION!r} must match ^[0-9]+\\.[0-9]+\\.[0-9]+$ "
        "(frozen schema constraint). "
        "Old format 'agent-fi-cost/playbook:1.0.0' fails this pattern."
    )


# ---------------------------------------------------------------------------
# S3 — Anomaly Finding validates against frozen schema
# ---------------------------------------------------------------------------


def test_anomaly_finding_validates_against_frozen_schema():
    """A fully-built fleet.cost.anomaly Finding must validate against finding.schema.json.

    Catches schema violations introduced by C1/W5/W6 fixes:
    - evidence entries missing source, source_ref, computed_at
    - AGENT_VERSION not matching ^[0-9]+\.[0-9]+\.[0-9]+$
    - missing required top-level fields (tokens_used, cost_usd, etc.)
    """
    finding = _build_anomaly_finding()
    jsonschema.validate(instance=finding, schema=_FINDING_SCHEMA)


# ---------------------------------------------------------------------------
# S4 — Sell-timing Finding validates against frozen schema
# ---------------------------------------------------------------------------


def test_sell_timing_finding_validates_against_frozen_schema():
    """A fully-built fleet.cost.sell_timing Finding must validate against finding.schema.json.

    Catches schema violations:
    - agent_version 'fi-cost-stage-b/v1' not matching semver pattern
    - missing confidence field (required by schema)
    - evidence entries missing schema-required fields
    """
    finding = _build_sell_timing_finding()
    jsonschema.validate(instance=finding, schema=_FINDING_SCHEMA)


# ---------------------------------------------------------------------------
# S5 — Evidence entries have all required schema fields
# ---------------------------------------------------------------------------


def test_anomaly_finding_evidence_entries_have_schema_fields():
    """Every evidence entry in an anomaly Finding must have all frozen-schema-required fields.

    Frozen schema (evidence_entry $ref) requires:
      kind, source, source_ref, value, computed_at, provenance
    """
    finding = _build_anomaly_finding()
    required_fields = {"kind", "source", "source_ref", "value", "computed_at", "provenance"}
    for i, entry in enumerate(finding.get("evidence", [])):
        missing = required_fields - set(entry.keys())
        assert not missing, (
            f"Evidence entry {i} (kind={entry.get('kind')!r}) is missing fields: "
            f"{sorted(missing)}. "
            "The frozen schema requires all of: {sorted(required_fields)}"
        )


def test_sell_timing_finding_evidence_entries_have_schema_fields():
    """Every evidence entry in a sell_timing Finding must have all frozen-schema-required fields."""
    finding = _build_sell_timing_finding()
    required_fields = {"kind", "source", "source_ref", "value", "computed_at", "provenance"}
    for i, entry in enumerate(finding.get("evidence", [])):
        missing = required_fields - set(entry.keys())
        assert not missing, (
            f"sell_timing evidence entry {i} (kind={entry.get('kind')!r}) is missing: "
            f"{sorted(missing)}"
        )


def test_anomaly_finding_evidence_not_empty():
    """An anomaly Finding must have at least one evidence entry (cost.diagnosis_source).

    D8: Finding evidence must include cost.diagnosis_source.
    The current code always includes it.
    """
    finding = _build_anomaly_finding()
    assert len(finding.get("evidence", [])) >= 1, (
        "Anomaly Finding must have at least one evidence entry (cost.diagnosis_source)"
    )
    kinds = {e.get("kind") for e in finding["evidence"]}
    assert "cost.diagnosis_source" in kinds, (
        "Anomaly Finding must include a cost.diagnosis_source evidence entry (D8)"
    )


def test_sell_timing_finding_evidence_not_empty():
    """A sell_timing Finding must have evidence entries (at minimum lifecycle inputs)."""
    finding = _build_sell_timing_finding()
    assert len(finding.get("evidence", [])) >= 1, (
        "sell_timing Finding must have at least one evidence entry"
    )


def test_sell_timing_inputs_hash_is_real_and_input_sensitive():
    """W5 (review cycle 1): the sell-timing Finding carries a real inputs_hash over its inputs."""
    from agents.fi_cost.sell_timing import build_sell_timing_finding

    kw = dict(vin="MRDAZ01S0PC00GXY9", fleet_id="demo-lastmile-delivery", crossover_month="2026-11",
              purchase_price=52_000.0, monthly_depreciation=610.0, year_month="2026-09")
    a = build_sell_timing_finding(**kw)["inputs_hash"]
    b = build_sell_timing_finding(**kw)["inputs_hash"]
    c = build_sell_timing_finding(**{**kw, "crossover_month": "2026-12"})["inputs_hash"]
    assert len(a) == 64 and all(ch in "0123456789abcdef" for ch in a)
    assert a == b
    assert a != c
