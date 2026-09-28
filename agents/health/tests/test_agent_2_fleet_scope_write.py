"""
Spec: .kiro/specs/2026-09-18-avx-tier2-ios-cards-and-actions/tasks.md
      § Group 2 "Wire Agent 2 fleet_owner scope-write rule (Addendum D1)"

What is tested
--------------
Agent 2's write-path branching for fleet_owner vs consumer/vehicle_owner inputs.

Addendum D1:
  - ``consumer`` / ``vehicle_owner`` input → per-VIN Findings with ``scope.vin``.
    Evidence entries carry no ``vin`` field, or one matching the parent scope.
  - ``fleet_owner`` input → EXACTLY ONE Finding per ``(fleet_id, finding_kind)``
    with ``scope.fleet_id``, whose ``evidence[]`` carries per-VIN entries each
    with a ``vin`` attribution field.
  No N-per-VIN write for fleet_owner; no read-side aggregation.

Two contract tests:
  (a) test_agent_2_fleet_write_is_fleet_scoped:
      fleet_owner input with 3 VINs → exactly one Finding with scope.fleet_id,
      evidence[] length >= 3, each entry has a distinct vin.
  (b) test_agent_2_owner_write_is_per_vin:
      consumer input → per-VIN Findings with scope.vin.

Mutation verification (from the task spec):
  Revert to per-VIN emit for fleet_owner (three Findings, scope.vin);
  test (a) MUST fail (finds three rows, expected one).
  Which assertion catches it is stated in the decisions fragment.

Four rules from the spec (steering/testing.md § Mutation):
  1. Never read prose in a guard — compare structured data against exact expectations.
  2. Mutation values come from OUTSIDE the assertion's vocabulary.
  3. "Covered by construction" is a claim, not a measurement.
  4. Restore from a cp copy, NEVER git checkout --.
"""
from __future__ import annotations

import pytest

from agents.health.agent import (
    assess_fleet_health,
    assess_vehicle_health,
)
from agents.tier2.writer import compute_finding_key

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

_FLEET_ID = "FLEET-MERIDIAN-TEST-001"

# Three VINs with different health profiles.
_VIN_A = "VIN-FLEET-A-BATTERY-92"
_VIN_B = "VIN-FLEET-B-BATTERY-74"
_VIN_C = "VIN-FLEET-C-BATTERY-88"

_BATTERY_ROW_HEALTHY = {
    "vin": _VIN_A,
    "health_pct": "92.0",
    "age_days": "1095",
}
_BATTERY_ROW_DEGRADED = {
    "vin": _VIN_B,
    "health_pct": "74.0",
    "age_days": "2190",
}
_BATTERY_ROW_NORMAL = {
    "vin": _VIN_C,
    "health_pct": "88.0",
    "age_days": "730",
}

_THREE_VIN_DATA = [
    {"vin": _VIN_A, "battery_rows": [_BATTERY_ROW_HEALTHY]},
    {"vin": _VIN_B, "battery_rows": [_BATTERY_ROW_DEGRADED]},
    {"vin": _VIN_C, "battery_rows": [_BATTERY_ROW_NORMAL]},
]


# ---------------------------------------------------------------------------
# (a) test_agent_2_fleet_write_is_fleet_scoped
# ---------------------------------------------------------------------------


class TestFleetOwnerWriteIsFleetScoped:
    """Fleet_owner input → exactly ONE fleet-scoped Finding per finding_kind.

    Addendum D1 headline: given a fleet_owner input with 3 VINs, Agent 2 emits
    exactly one Finding with scope.fleet_id, evidence[] length >= 3, and each
    entry has a distinct vin attribution field.
    """

    def test_agent_2_fleet_write_is_fleet_scoped(self):
        """Core D1 contract: 3 VINs → 1 fleet Finding, not 3 per-VIN Findings.

        This is the test that the mutation run MUST break.
        """
        result = assess_fleet_health(_FLEET_ID, _THREE_VIN_DATA)

        # There must be at least one finding_kind in the result.
        assert len(result) >= 1, (
            "assess_fleet_health returned an empty result for 3 VINs with battery data"
        )

        # The battery component should be present (all 3 VINs have battery data).
        assert "health.battery" in result, (
            f"Expected 'health.battery' in fleet findings, got keys: {list(result.keys())}"
        )

        fleet_battery = result["health.battery"]

        # CONTRACT ASSERTION 1: scope.fleet_id must be set (not scope.vin).
        scope = fleet_battery.get("scope", {})
        assert "fleet_id" in scope, (
            f"Fleet finding must have scope.fleet_id set, got scope={scope!r}. "
            "D1: fleet_owner input → ONE scope.fleet_id Finding, not per-VIN."
        )
        assert scope["fleet_id"] == _FLEET_ID, (
            f"scope.fleet_id must equal the input fleet_id={_FLEET_ID!r}, "
            f"got {scope['fleet_id']!r}"
        )
        assert "vin" not in scope, (
            f"Fleet finding must NOT have scope.vin set, got scope={scope!r}. "
            "scope is oneOf — vin and fleet_id cannot coexist."
        )

        # CONTRACT ASSERTION 2: evidence[] must have length >= 3 (one entry per VIN).
        evidence = fleet_battery.get("evidence", [])
        assert len(evidence) >= 3, (
            f"Fleet finding evidence must contain at least 3 entries (one per VIN). "
            f"Got {len(evidence)}: {evidence!r}"
        )

        # CONTRACT ASSERTION 3: each evidence entry must carry a distinct vin field.
        # This is the D1 schema requirement: fleet_id-scoped Finding → per-entry vin.
        evidence_vins = [e.get("vin") for e in evidence]
        assert all(v is not None for v in evidence_vins), (
            f"Every evidence entry in a fleet-scoped Finding must carry a 'vin' field. "
            f"Found None in: {evidence_vins!r}"
        )
        # Vins must be distinct (one entry per input VIN, no duplicates).
        assert len(set(evidence_vins)) == len(evidence_vins), (
            f"Evidence VINs must be distinct; duplicates found: {evidence_vins!r}"
        )
        # All 3 input VINs must appear.
        expected_vins = {_VIN_A, _VIN_B, _VIN_C}
        actual_vins = set(evidence_vins)
        assert expected_vins == actual_vins, (
            f"Evidence must include all 3 input VINs. "
            f"Expected: {expected_vins}, got: {actual_vins}"
        )

    def test_fleet_finding_key_uses_fleet_id_scope(self):
        """finding_key must be derived from scope_field='fleet_id' (D2 + D1 combined)."""
        result = assess_fleet_health(_FLEET_ID, _THREE_VIN_DATA)
        assert "health.battery" in result

        fleet_battery = result["health.battery"]
        actual_key = fleet_battery.get("finding_key", "")

        expected_key = compute_finding_key(
            agent_id="agent-2-vehicle-health",
            scope_field="fleet_id",
            scope_value=_FLEET_ID,
            finding_kind="health.battery",
        )
        assert actual_key == expected_key, (
            f"Fleet finding_key must be derived from scope_field='fleet_id', "
            f"not from 'vin'. Expected {expected_key!r}, got {actual_key!r}. "
            "A vin-derived key would differ — this catches a mis-derivation."
        )

    def test_fleet_finding_key_differs_from_vin_key(self):
        """Fleet finding_key must be distinguishable from any per-VIN key.

        Mutation guard: if the impl accidentally used scope_field='vin' for the
        fleet finding, the keys would hash to the same value as a per-VIN Finding.
        This test catches that by deriving both and asserting they differ.
        """
        result = assess_fleet_health(_FLEET_ID, _THREE_VIN_DATA)
        fleet_key = result["health.battery"]["finding_key"]

        # Each VIN key uses scope_field='vin' — must be different from fleet key.
        for vin in (_VIN_A, _VIN_B, _VIN_C):
            vin_key = compute_finding_key(
                agent_id="agent-2-vehicle-health",
                scope_field="vin",
                scope_value=vin,
                finding_kind="health.battery",
            )
            assert fleet_key != vin_key, (
                f"Fleet finding_key must differ from per-VIN key for VIN={vin!r}. "
                "If they collide, the fleet Finding would overwrite VIN Findings in DDB."
            )

    def test_fleet_returns_exactly_one_finding_per_component(self):
        """assess_fleet_health returns a flat dict: one finding per component.

        This confirms the function signature: it returns a dict keyed by
        finding_kind, NOT a list of N findings or a nested per-VIN structure.
        """
        result = assess_fleet_health(_FLEET_ID, _THREE_VIN_DATA)

        # Result must be a dict (not a list).
        assert isinstance(result, dict), (
            f"assess_fleet_health must return a dict, got {type(result)}"
        )

        # Each value must itself be a dict with scope.fleet_id.
        for finding_kind, finding in result.items():
            assert isinstance(finding, dict), (
                f"Each fleet finding must be a dict, got {type(finding)} for {finding_kind!r}"
            )
            scope = finding.get("scope", {})
            assert "fleet_id" in scope, (
                f"Finding for {finding_kind!r} must have scope.fleet_id, got {scope!r}"
            )


# ---------------------------------------------------------------------------
# (b) test_agent_2_owner_write_is_per_vin
# ---------------------------------------------------------------------------


class TestOwnerWriteIsPerVin:
    """Consumer/vehicle_owner input → per-VIN Findings with scope.vin.

    This verifies the non-fleet path is unchanged: assess_vehicle_health
    still produces per-VIN component results with scope.vin as the natural
    scope (via runner._build_artifact).
    """

    def test_agent_2_owner_write_is_per_vin(self):
        """assess_vehicle_health for a single VIN produces VIN-local (not fleet) output.

        The 'scope' is implicitly per-VIN in assess_vehicle_health's output:
        the function takes a ``vin`` parameter and every returned component
        result is scoped to that VIN.  The runner (runner._build_artifact)
        then writes scope.vin when assembling the AVX Finding.

        This test verifies the output shape: assess_vehicle_health returns
        component dicts that carry the VIN in their evidence, NOT a
        scope.fleet_id.
        """
        vin = "VIN-CONSUMER-OWNER-001"
        result = assess_vehicle_health(
            vin,
            battery_rows=[{
                "vin": vin,
                "health_pct": "88.0",
                "age_days": "730",
            }],
        )

        # Result must include a battery component.
        assert "battery" in result, (
            f"assess_vehicle_health must return 'battery' component for battery input, "
            f"got: {list(result.keys())}"
        )
        battery = result["battery"]

        # The per-VIN result must NOT have scope.fleet_id.
        # (assess_vehicle_health returns the component judgment dict, not a full
        # AVX Finding — the scope is attached by runner._build_artifact.  We
        # verify here that the component result carries no fleet_id contamination.)
        assert "fleet_id" not in battery, (
            f"Per-VIN component result must not carry fleet_id, got: {battery!r}"
        )

        # The component result must carry the standard fields.
        assert "finding" in battery, "Per-VIN battery result must have 'finding' text"
        assert "severity_hint" in battery, "Per-VIN battery result must have 'severity_hint'"
        assert "confidence" in battery, "Per-VIN battery result must have 'confidence'"
        assert "evidence" in battery, "Per-VIN battery result must have 'evidence'"

        # Confirm this is genuinely a per-VIN result (distinct from fleet output).
        fleet_result = assess_fleet_health(
            "FLEET-OTHER-001",
            [{"vin": vin, "battery_rows": [{"vin": vin, "health_pct": "88.0", "age_days": "730"}]}],
        )
        # Fleet result has scope.fleet_id; per-VIN result has no such scope key.
        fleet_scope = fleet_result.get("health.battery", {}).get("scope", {})
        assert "fleet_id" in fleet_scope, (
            "Fleet result must have scope.fleet_id (sanity check for comparison)"
        )
        # The per-VIN result dict has no 'scope' key at all.
        assert "scope" not in battery, (
            f"Per-VIN battery component result must not have a 'scope' key, "
            f"got: {list(battery.keys())}"
        )

    def test_multiple_vins_produce_independent_per_vin_results(self):
        """Multiple assess_vehicle_health calls produce independent per-VIN results.

        This confirms there is no accidental fleet aggregation happening in the
        per-VIN path when called multiple times.
        """
        vin_1 = "VIN-CONSUMER-001"
        vin_2 = "VIN-CONSUMER-002"

        result_1 = assess_vehicle_health(
            vin_1,
            battery_rows=[{"vin": vin_1, "health_pct": "92.0", "age_days": "1095"}],
        )
        result_2 = assess_vehicle_health(
            vin_2,
            battery_rows=[{"vin": vin_2, "health_pct": "72.0", "age_days": "2190"}],
        )

        # Both must return battery component results independently.
        assert "battery" in result_1, "VIN 1 must have battery result"
        assert "battery" in result_2, "VIN 2 must have battery result"

        # Results must be independent (different severity for different SoH).
        # vin_1 at 92% SoH → routine; vin_2 at 72% SoH → soon
        sev_1 = result_1["battery"].get("severity_hint")
        sev_2 = result_2["battery"].get("severity_hint")
        assert sev_1 != sev_2, (
            f"Per-VIN results at different SoH levels should have different severity. "
            f"VIN1 (92% SoH): {sev_1!r}, VIN2 (72% SoH): {sev_2!r}"
        )
        # Specifically: 92% → routine, 72% → soon (below 80 threshold, above 70)
        assert sev_1 == "routine", f"92% SoH should be 'routine', got {sev_1!r}"
        assert sev_2 == "soon", f"72% SoH should be 'soon', got {sev_2!r}"
