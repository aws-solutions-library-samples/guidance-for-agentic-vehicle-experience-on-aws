"""Tests for agents/fi_rebalancing/write.py and evidence builders (task 4.1d).

Spec: .kiro/specs/2026-10-06-fi-rebalancing-stage-e/group4-design.md G4-D7, G4-D8, G4-D9
Contract: docs/fi-rebalancing-contract.md §2, §3, §6

VINs deliberately differ from vehicleIds (fleet Finding scope is fleet_id, not VIN).
"""
from __future__ import annotations

import jsonschema
import pathlib
import json
from unittest.mock import MagicMock

import pytest

# ---------------------------------------------------------------------------
# Shared test data
# ---------------------------------------------------------------------------

_FLEET_ID = "fleet-meridian-test"
_AGENT_VERSION = "1.0.0"
_COMPUTED_AT = "2026-10-20T06:00:00Z"

# VINs are vehicle-level identifiers — distinct from vehicleId strings like "VEH-001"
_VIN_A = "MRDVEH00000000021"
_VIN_B = "MRDVEH00000000022"
_VIN_C = "MRDVEH00000000023"

_LOC_FROM = "loc-sea-downtown-02"
_LOC_TO = "loc-sea-airport-01"
_ISO_WEEK = "2026-W43"
_FIRST_DAY = "2026-10-20"

_FROM_LOCATION = {
    "locationId": _LOC_FROM,
    "name": "Seattle Downtown",
    "locationType": "downtown",
    "metro": "seattle",
    "latitude": 47.60,
    "longitude": -122.33,
    "parkingSpaces": 20,
    "operatingHours": "08-20",
    "op_hours": 12.0,
}

_TO_LOCATION = {
    "locationId": _LOC_TO,
    "name": "Seattle Airport",
    "locationType": "airport",
    "metro": "seattle",
    "latitude": 47.45,
    "longitude": -122.30,
    "parkingSpaces": 30,
    "operatingHours": "06-22",
    "op_hours": 16.0,
}

_PLAN = {
    "candidate_id": f"cand-{_LOC_FROM}-{_LOC_TO}",
    "from_location_id": _LOC_FROM,
    "to_location_id": _LOC_TO,
    "vehicle_vins": [_VIN_A, _VIN_B],
    "timing": "today",
    "detail": "Rules-based move: transfer vehicles to address fleet deficit.",
    "diagnosis_source": "rules",
    "transfer_cost_usd": 84.00,
    "revenue_gain_usd": 392.00,
    "source_loss_usd": 0.00,
    "revenue_impact_usd": 392.00,
    "net_benefit_usd": 308.00,
    "predicted_on_rent_hours_gain": 56.0,
    "predicted_turnaways_avoided": 4,
}

_CANDIDATE = {
    "candidate_id": f"cand-{_LOC_FROM}-{_LOC_TO}",
    "from_location_id": _LOC_FROM,
    "to_location_id": _LOC_TO,
    "metro": "seattle",
    "count": 2,
    "eligible_vins": [_VIN_A, _VIN_B, _VIN_C],
    "eligible_facts": {
        v: {"days_idle_last_7": 3, "days_to_next_service": float("inf"),
            "hours_to_next_booking": float("inf"), "open_lower_severity_items": ["routine"]}
        for v in (_VIN_A, _VIN_B, _VIN_C)
    },
    "vehicles_short": 3,
    "vehicles_spare": 5,
    "parking_room": 10,
    "distance_miles": 12.5,
    "transfer_cost_per_vehicle": 42.00,
    "per_day": [
        {
            "date": "2026-10-21",
            "forecast_hours": 48.0,
            "supply_vehicles": 4,
            "op_hours": 16.0,
            "source_forecast_hours": 36.0,
            "source_supply_vehicles": 8,
            "source_op_hours": 12.0,
        }
    ],
    "effective_date": "2026-10-21",
    "first_deficit_day": _FIRST_DAY,
    "forecast_week": _ISO_WEEK,
    "severity": "soon",
    "confidence": "medium",
    "max_utilization": 0.95,
    "rate": 3.50,
    "source_rate": 3.00,
    "mean_request_hours": 8.0,
    "net_benefit_usd": 308.00,
    "transfer_cost_usd": 84.00,
    "revenue_gain_usd": 392.00,
    "source_loss_usd": 0.00,
    "revenue_impact_usd": 392.00,
    "predicted_on_rent_hours_gain": 56.0,
    "predicted_turnaways_avoided": 4,
}

_GAP_ITEM = {
    "location_id": _LOC_TO,
    "gap_kind": "deficit",
    "vehicles_short": 3,
    "forecast_week": _ISO_WEEK,
    "first_deficit_day": _FIRST_DAY,
    "severity": "soon",
    "confidence": "medium",
    "diagnosis_source": "rules",
    "per_day": [
        {
            "date": "2026-10-21",
            "forecast_hours": 48.0,
            "supply_vehicles": 0,
            "op_hours": 16.0,
        }
    ],
}

_SURPLUS_GAP_ITEM = {
    "location_id": _LOC_FROM,
    "gap_kind": "surplus",
    "vehicles_spare": 5,
    "forecast_week": _ISO_WEEK,
    "severity": "routine",
    "confidence": "medium",
}

# Load the schema once for reuse
_SCHEMA_PATH = (
    pathlib.Path(__file__).parents[3] / "agents" / "tier2" / "schemas" / "finding.schema.json"
)
_SCHEMA = json.loads(_SCHEMA_PATH.read_text())


def _validate(finding: dict) -> None:
    validator = jsonschema.Draft202012Validator(_SCHEMA)
    errors = list(validator.iter_errors(finding))
    assert not errors, [e.message for e in errors]


def _make_move_evidence() -> list:
    from agents.fi_rebalancing.evidence import move_evidence
    return move_evidence(
        _PLAN, _CANDIDATE,
        computed_at=_COMPUTED_AT,
        from_location=_FROM_LOCATION,
        to_location=_TO_LOCATION,
        outcomes=[],
        events=[],
    )


def _make_gap_evidence(assigned_vins: list | None = None) -> list:
    from agents.fi_rebalancing.evidence import gap_evidence
    vins = assigned_vins if assigned_vins is not None else [_VIN_C]
    return gap_evidence(
        _GAP_ITEM,
        computed_at=_COMPUTED_AT,
        location=_TO_LOCATION,
        assigned_vins=vins,
    )


# ---------------------------------------------------------------------------
# Schema validation
# ---------------------------------------------------------------------------

class TestSchemaValidation:
    """Both finding kinds must validate against the AVX Finding schema."""

    def test_move_finding_validates(self):
        """build_move_finding returns a schema-valid fleet.rebalance.move Finding."""
        from agents.fi_rebalancing.write import build_move_finding
        ev = _make_move_evidence()
        finding = build_move_finding(
            _PLAN, _CANDIDATE,
            fleet_id=_FLEET_ID,
            from_location=_FROM_LOCATION,
            to_location=_TO_LOCATION,
            evidence=ev,
            computed_at=_COMPUTED_AT,
            agent_version=_AGENT_VERSION,
        )
        _validate(finding)

    def test_gap_finding_validates(self):
        """build_gap_finding returns a schema-valid fleet.rebalance.gap Finding."""
        from agents.fi_rebalancing.write import build_gap_finding
        ev = _make_gap_evidence()
        finding = build_gap_finding(
            _GAP_ITEM,
            fleet_id=_FLEET_ID,
            location=_TO_LOCATION,
            evidence=ev,
            computed_at=_COMPUTED_AT,
            agent_version=_AGENT_VERSION,
        )
        _validate(finding)


class TestFleetScopeOnly:
    """Security W2 (Group 4 security cycle 1): both kinds carry only the fleet scope.

    A Finding with ``scope.vin`` would land on the findings table's ``by_vin`` index and in
    that vehicle's open items (which the eligibility gate reads). The schema accepts any
    string as ``scope.vin``, so only this test stops a changed scope field.
    """

    def _both(self):
        from agents.fi_rebalancing.write import build_gap_finding, build_move_finding
        move = build_move_finding(
            _PLAN, _CANDIDATE, fleet_id=_FLEET_ID, from_location=_FROM_LOCATION,
            to_location=_TO_LOCATION, evidence=_make_move_evidence(),
            computed_at=_COMPUTED_AT, agent_version=_AGENT_VERSION)
        gap = build_gap_finding(
            _GAP_ITEM, fleet_id=_FLEET_ID, location=_TO_LOCATION,
            evidence=_make_gap_evidence(), computed_at=_COMPUTED_AT,
            agent_version=_AGENT_VERSION)
        return move, gap

    def test_scope_is_the_fleet_and_nothing_else(self):
        from agents.tier2.writer import _extract_scope
        for finding in self._both():
            assert finding["scope"] == {"fleet_id": _FLEET_ID}, finding["scope"]
            assert _extract_scope(finding) == ("fleet_id", _FLEET_ID)
            assert "vin" not in finding


# ---------------------------------------------------------------------------
# recommended_action shape (contract §3)
# ---------------------------------------------------------------------------

class TestRecommendedAction:
    """Move finding recommended_action matches contract §3 exactly."""

    def test_exact_recommended_action(self):
        """Fixture-exact check of every recommended_action field (contract §3)."""
        from agents.fi_rebalancing.write import build_move_finding
        ev = _make_move_evidence()
        finding = build_move_finding(
            _PLAN, _CANDIDATE,
            fleet_id=_FLEET_ID,
            from_location=_FROM_LOCATION,
            to_location=_TO_LOCATION,
            evidence=ev,
            computed_at=_COMPUTED_AT,
            agent_version=_AGENT_VERSION,
        )
        rec = finding["recommended_action"]
        assert rec["action_kind"] == "move_vehicles"
        assert rec["target_system"] == "cms"

        p = rec["parameters"]
        assert p["fleet_id"] == _FLEET_ID
        assert p["from_location_id"] == _LOC_FROM
        assert p["to_location_id"] == _LOC_TO
        assert p["vehicle_vins"] == [_VIN_A, _VIN_B]
        assert p["forecast_week"] == _ISO_WEEK
        # Money rounded to 2 decimals
        assert p["transfer_cost_usd"] == 84.00
        assert isinstance(p["transfer_cost_usd"], float)
        assert isinstance(p["predicted_on_rent_hours_gain"], float)
        # Turnaways must be int
        assert p["predicted_turnaways_avoided"] == 4
        assert isinstance(p["predicted_turnaways_avoided"], int)

        # estimated_value_usd = net_benefit, 2 decimals
        assert rec["estimated_value_usd"] == 308.00
        # basis string contains names and effective date
        basis = rec["estimated_value_basis"]
        assert "Seattle Airport" in basis or _LOC_TO in basis
        assert "Seattle Downtown" in basis or _LOC_FROM in basis
        assert "2026-10-21" in basis or _FIRST_DAY in basis

    def test_gap_finding_has_no_recommended_action(self):
        """Gap findings are informational only — no recommended_action (contract §2.2)."""
        from agents.fi_rebalancing.write import build_gap_finding
        ev = _make_gap_evidence()
        finding = build_gap_finding(
            _GAP_ITEM,
            fleet_id=_FLEET_ID,
            location=_TO_LOCATION,
            evidence=ev,
            computed_at=_COMPUTED_AT,
            agent_version=_AGENT_VERSION,
        )
        assert "recommended_action" not in finding


# ---------------------------------------------------------------------------
# Headline fallback (over 120 chars)
# ---------------------------------------------------------------------------

class TestHeadlineFallback:
    """Headline falls back to location ids when full names exceed 120 chars (G4-D7)."""

    def test_headline_fallback_to_ids(self):
        """Very long names trigger the id fallback so headline stays ≤ 120 chars."""
        from agents.fi_rebalancing.write import build_move_finding
        long_from = dict(_FROM_LOCATION, name="A" * 80)
        long_to = dict(_TO_LOCATION, name="B" * 80)
        ev = _make_move_evidence()
        finding = build_move_finding(
            _PLAN, _CANDIDATE,
            fleet_id=_FLEET_ID,
            from_location=long_from,
            to_location=long_to,
            evidence=ev,
            computed_at=_COMPUTED_AT,
            agent_version=_AGENT_VERSION,
        )
        hl = finding["headline"]
        assert len(hl) <= 120, f"headline too long ({len(hl)}): {hl!r}"
        # Fallback uses location ids
        assert _LOC_FROM in hl or _LOC_TO in hl

    def test_headline_uses_names_when_short_enough(self):
        """Short names are used directly without id fallback."""
        from agents.fi_rebalancing.write import build_move_finding
        ev = _make_move_evidence()
        finding = build_move_finding(
            _PLAN, _CANDIDATE,
            fleet_id=_FLEET_ID,
            from_location=_FROM_LOCATION,
            to_location=_TO_LOCATION,
            evidence=ev,
            computed_at=_COMPUTED_AT,
            agent_version=_AGENT_VERSION,
        )
        assert "Seattle Downtown" in finding["headline"]
        assert "Seattle Airport" in finding["headline"]


# ---------------------------------------------------------------------------
# Evidence vin and provenance
# ---------------------------------------------------------------------------

class TestEvidenceShape:
    """Every evidence entry has a vin and provenance='live' (schema allOf 1, G4-D7)."""

    def test_every_move_evidence_entry_has_vin_and_provenance_live(self):
        ev = _make_move_evidence()
        assert ev, "evidence must not be empty"
        for entry in ev:
            assert "vin" in entry, f"evidence entry missing vin: {entry['kind']!r}"
            assert entry["vin"], "vin must be non-empty"
            assert entry["provenance"] == "live", (
                f"expected provenance='live', got {entry['provenance']!r} on {entry['kind']!r}"
            )

    def test_every_gap_evidence_entry_has_vin_and_provenance_live(self):
        ev = _make_gap_evidence()
        assert ev, "evidence must not be empty"
        for entry in ev:
            assert "vin" in entry, f"gap evidence entry missing vin: {entry['kind']!r}"
            assert entry["vin"], "vin must be non-empty"
            assert entry["provenance"] == "live"

    def test_vehicle_source_ref_names_snapshot_date(self):
        """W7: rebalance.vehicle source_ref must name snapshot_date.

        Fails if source_ref is reverted to 'vehicle_days/latest' (mutant F3-W7a).
        """
        from agents.fi_rebalancing.evidence import move_evidence
        vd = [{"vin": _VIN_A, "snapshot_date": "2026-10-11", "available_hours": 10.0},
              {"vin": _VIN_B, "snapshot_date": "2026-10-11", "available_hours": 10.0}]
        ev = move_evidence(_PLAN, _CANDIDATE, computed_at=_COMPUTED_AT,
                           from_location=_FROM_LOCATION, to_location=_TO_LOCATION,
                           outcomes=[], events=[], vehicle_days=vd, today="2026-10-12")
        vehicle_entries = [e for e in ev if e["kind"] == "rebalance.vehicle"]
        assert vehicle_entries
        for e in vehicle_entries:
            assert "snapshot_date=2026-10-11" in e["source_ref"], (
                f"W7: vehicle source_ref must contain snapshot_date; got {e['source_ref']!r}"
            )

    def test_outcomes_source_ref_names_materialised_on(self):
        """W7: rebalance.recent_outcomes source_ref must name materialised_on.

        Fails if source_ref is reverted to 'move_outcomes_snapshot/latest' (mutant F3-W7b).
        """
        from agents.fi_rebalancing.evidence import move_evidence
        outcomes = [{"to_location_id": _LOC_TO, "effective_date": "2026-09-01",
                     "ratio_hours": 0.9, "materialised_on": "2026-10-11",
                     "predicted_on_rent_hours_gain": 10.0, "actual_on_rent_hours_gain": 9.0}]
        ev = move_evidence(_PLAN, _CANDIDATE, computed_at=_COMPUTED_AT,
                           from_location=_FROM_LOCATION, to_location=_TO_LOCATION,
                           outcomes=outcomes, events=[], today="2026-10-12")
        outcomes_entry = next(e for e in ev if e["kind"] == "rebalance.recent_outcomes")
        assert "materialised_on=2026-10-11" in outcomes_entry["source_ref"], (
            f"W7: outcomes source_ref must contain materialised_on; "
            f"got {outcomes_entry['source_ref']!r}"
        )

    def test_revenue_source_ref_names_forecast_run_date(self):
        """W7: rebalance.revenue source_ref must name the forecast run_date.

        Fails if source_ref is reverted to 'fleet_bookings_forecast/latest' (mutant F3-W7c).
        """
        from agents.fi_rebalancing.evidence import move_evidence
        cand_with_run_date = {**_CANDIDATE,
                              "per_day": [{"date": "2026-10-13", "forecast_hours": 45.0,
                                           "supply_vehicles": 3, "op_hours": 10.0,
                                           "method": "ml", "mape_28d": 0.10,
                                           "run_date": "2026-10-12", "data_as_of": "2026-10-12"}]}
        ev = move_evidence(_PLAN, cand_with_run_date, computed_at=_COMPUTED_AT,
                           from_location=_FROM_LOCATION, to_location=_TO_LOCATION,
                           outcomes=[], events=[], today="2026-10-12")
        revenue_entry = next(e for e in ev if e["kind"] == "rebalance.revenue")
        assert "run_date=2026-10-12" in revenue_entry["source_ref"], (
            f"W7: revenue source_ref must contain run_date; got {revenue_entry['source_ref']!r}"
        )


# ---------------------------------------------------------------------------
# Gap location with no assigned VIN raises
# ---------------------------------------------------------------------------

class TestGapNoVin:
    """A gap location with no assigned VIN raises ValueError (G4-D7)."""

    def test_gap_evidence_no_vins_raises(self):
        from agents.fi_rebalancing.evidence import gap_evidence
        with pytest.raises(ValueError, match="no assigned vehicle"):
            gap_evidence(
                _GAP_ITEM,
                computed_at=_COMPUTED_AT,
                location=_TO_LOCATION,
                assigned_vins=[],
            )

    def test_gap_evidence_none_vins_raises(self):
        from agents.fi_rebalancing.evidence import gap_evidence
        with pytest.raises(ValueError):
            gap_evidence(
                _GAP_ITEM,
                computed_at=_COMPUTED_AT,
                location=_TO_LOCATION,
                assigned_vins=None,
            )


# ---------------------------------------------------------------------------
# inputs_hash stability (G4-D8)
# ---------------------------------------------------------------------------

class TestInputsHash:
    """inputs_hash suppresses noise; changes when identity-relevant fields change."""

    def _make_summary(self, plan_override: dict | None = None,
                      cand_override: dict | None = None) -> dict:
        from agents.fi_rebalancing.write import _move_summary
        plan = {**_PLAN, **(plan_override or {})}
        cand = {**_CANDIDATE, **(cand_override or {})}
        sk = f"{plan['from_location_id']}/{plan['to_location_id']}/{_ISO_WEEK}"
        return _move_summary(
            plan=plan, candidate=cand,
            subject_key=sk, agent_version=_AGENT_VERSION, run_date="2026-10-12",
        )

    def test_hash_stable_when_forecast_hours_shift_within_one_vehicle(self):
        """Forecast hours moving within a single vehicle don't change the hash (G4-D8 noise floor)."""
        from agents.fi_rebalancing.evidence import compute_inputs_hash
        # vehicles_short 2.3 and 2.7 both floor to 2
        s1 = self._make_summary(cand_override={"vehicles_short": 2.3})
        s2 = self._make_summary(cand_override={"vehicles_short": 2.7})
        assert compute_inputs_hash(s1) == compute_inputs_hash(s2)

    def test_hash_stable_when_net_inside_25_bucket(self):
        """Net benefit shifts within a $25 bucket don't change the hash (G4-D8 bucketing)."""
        from agents.fi_rebalancing.evidence import compute_inputs_hash
        # 300 and 324 both bucket to 300
        s1 = self._make_summary(plan_override={"net_benefit_usd": 300.0})
        s2 = self._make_summary(plan_override={"net_benefit_usd": 324.9})
        assert compute_inputs_hash(s1) == compute_inputs_hash(s2)

    def test_hash_changes_when_count_changes(self):
        """Adding a VIN to vehicle_vins changes the hash (count is identity-relevant)."""
        from agents.fi_rebalancing.evidence import compute_inputs_hash
        s1 = self._make_summary(plan_override={"vehicle_vins": [_VIN_A]})
        s2 = self._make_summary(plan_override={"vehicle_vins": [_VIN_A, _VIN_B]})
        assert compute_inputs_hash(s1) != compute_inputs_hash(s2)

    def test_hash_changes_when_vin_changes(self):
        """Replacing a VIN in vehicle_vins changes the hash."""
        from agents.fi_rebalancing.evidence import compute_inputs_hash
        s1 = self._make_summary(plan_override={"vehicle_vins": [_VIN_A]})
        s2 = self._make_summary(plan_override={"vehicle_vins": [_VIN_B]})
        assert compute_inputs_hash(s1) != compute_inputs_hash(s2)

    def test_hash_changes_when_severity_changes(self):
        """A severity shift changes the hash."""
        from agents.fi_rebalancing.evidence import compute_inputs_hash
        s1 = self._make_summary(cand_override={"severity": "soon"})
        s2 = self._make_summary(cand_override={"severity": "attention"})
        assert compute_inputs_hash(s1) != compute_inputs_hash(s2)

    def test_hash_stable_when_eligible_vins_change(self):
        """W6: eligible_vins not in hash — idle-pool turnover must not trigger rewrites.

        Fails if eligible_vins is re-added to the summary (mutant F3-W6b).
        """
        from agents.fi_rebalancing.evidence import compute_inputs_hash
        # Same vehicle_vins (the chosen VINs), different eligible_vins (the pool)
        s1 = self._make_summary(cand_override={"eligible_vins": [_VIN_A]})
        s2 = self._make_summary(cand_override={"eligible_vins": [_VIN_A, _VIN_B]})
        assert compute_inputs_hash(s1) == compute_inputs_hash(s2), (
            "eligible_vins must not be in the summary hash (W6: pool turns over daily)"
        )

    def test_hours_rounded_to_2_decimal_places_in_recommendation(self):
        """S1: predicted_on_rent_hours_gain rounded to 2 places in the recommendation."""
        from agents.fi_rebalancing.write import build_move_finding
        from agents.fi_rebalancing.evidence import move_evidence
        plan = dict(_PLAN, predicted_on_rent_hours_gain=84.1234)
        ev = move_evidence(plan, _CANDIDATE, computed_at=_COMPUTED_AT,
                           from_location=_FROM_LOCATION, to_location=_TO_LOCATION,
                           outcomes=[], events=[])
        finding = build_move_finding(
            plan, _CANDIDATE,
            fleet_id="fleet-1",
            from_location=_FROM_LOCATION,
            to_location=_TO_LOCATION,
            evidence=ev,
            computed_at=_COMPUTED_AT,
            agent_version=_AGENT_VERSION,
            run_date="2026-10-12",
        )
        hours = finding["recommended_action"]["parameters"]["predicted_on_rent_hours_gain"]
        assert hours == 84.12, (
            f"S1: predicted_on_rent_hours_gain must be rounded to 2 places; got {hours!r}"
        )

    def test_surplus_gap_hash_stable_when_first_deficit_day_differs(self):
        """W6c: surplus gap summary has no first_deficit_day; its hash must not depend on it.

        Fails if _gap_summary adds first_deficit_day_offset to a surplus gap (mutant F3-W6c).
        """
        from agents.fi_rebalancing.write import _gap_summary
        from agents.fi_rebalancing.evidence import compute_inputs_hash
        base = {
            "gap_kind": "surplus",
            "location_id": "loc-sur",
            "vehicles_short": 0,
            "vehicles_spare": 3,
            "severity": "routine",
            "confidence": "medium",
        }
        s1 = _gap_summary(gap_item={**base, "first_deficit_day": "2026-10-13"},
                          subject_key="loc-sur/2026-W42", agent_version="1.0.0",
                          run_date="2026-10-12")
        s2 = _gap_summary(gap_item={**base, "first_deficit_day": "2026-10-20"},
                          subject_key="loc-sur/2026-W42", agent_version="1.0.0",
                          run_date="2026-10-12")
        assert compute_inputs_hash(s1) == compute_inputs_hash(s2), (
            "W6c: surplus gap hash must not depend on first_deficit_day "
            "(surplus has no deficit day)"
        )

    def test_hash_changes_when_confidence_changes(self):
        """W-n7: confidence is identity-relevant for the move inputs_hash (H1).

        Fails if `'confidence': candidate.get('confidence')` is removed from
        _move_summary (mutant H1 sets it to None).
        """
        from agents.fi_rebalancing.evidence import compute_inputs_hash
        s1 = self._make_summary(cand_override={"confidence": "medium"})
        s2 = self._make_summary(cand_override={"confidence": "high"})
        assert compute_inputs_hash(s1) != compute_inputs_hash(s2), (
            "W-n7: confidence must change the move inputs_hash"
        )

    def test_gap_hash_changes_when_confidence_changes(self):
        """W-n7: confidence is identity-relevant for the gap inputs_hash (H2).

        Fails if `'confidence': gap_item.get('confidence')` is removed from
        _gap_summary (mutant H2 sets it to None).
        """
        from agents.fi_rebalancing.write import _gap_summary
        from agents.fi_rebalancing.evidence import compute_inputs_hash
        base = {
            "gap_kind": "deficit",
            "location_id": _LOC_TO,
            "vehicles_short": 3,
            "vehicles_spare": 0,
            "severity": "soon",
            "first_deficit_day": _FIRST_DAY,
        }
        s1 = _gap_summary(
            gap_item={**base, "confidence": "medium"},
            subject_key=f"{_LOC_TO}/{_ISO_WEEK}",
            agent_version=_AGENT_VERSION,
            run_date="2026-10-12",
        )
        s2 = _gap_summary(
            gap_item={**base, "confidence": "high"},
            subject_key=f"{_LOC_TO}/{_ISO_WEEK}",
            agent_version=_AGENT_VERSION,
            run_date="2026-10-12",
        )
        assert compute_inputs_hash(s1) != compute_inputs_hash(s2), (
            "W-n7: confidence must change the gap inputs_hash"
        )


class TestGapVinChoice:
    """W-b3: gap_evidence always picks sorted(assigned_vins)[0] regardless of input order."""

    def _check_all_entries(self, assigned_vins: list) -> None:
        from agents.fi_rebalancing.evidence import gap_evidence
        entries = gap_evidence(
            _GAP_ITEM,
            computed_at=_COMPUTED_AT,
            location=_TO_LOCATION,
            assigned_vins=assigned_vins,
        )
        for e in entries:
            assert e["vin"] == "V1", (
                f"W-b3: expected vin 'V1' (sorted first); got {e['vin']!r} "
                f"(input order {assigned_vins})"
            )
        supply = next(e for e in entries if e["kind"] == "rebalance.supply")
        assert supply["value"]["assigned_vins"] == ["V1", "V2", "V3"], (
            "W-b3: assigned_vins in supply evidence must be sorted"
        )

    def test_gap_vin_is_lowest_for_unsorted_input(self):
        """W-b3 unsorted: sorted → list mutant (MB-1) gives read order 'V3', not 'V1'."""
        self._check_all_entries(["V3", "V1", "V2"])

    def test_gap_vin_is_lowest_for_reversed_input(self):
        """W-b3 reversed input gives the same sorted-first VIN."""
        self._check_all_entries(["V2", "V1", "V3"])


class TestGapHeadlineAndDetail:
    """W-b4: exact deficit/surplus gap headlines and fixed detail; no VIN in either."""

    def _build(self, gap_item: dict, location: dict, assigned_vins: list) -> dict:
        from agents.fi_rebalancing.write import build_gap_finding
        from agents.fi_rebalancing.evidence import gap_evidence
        ev = gap_evidence(
            gap_item,
            computed_at=_COMPUTED_AT,
            location=location,
            assigned_vins=assigned_vins,
        )
        return build_gap_finding(
            gap_item,
            fleet_id=_FLEET_ID,
            location=location,
            evidence=ev,
            computed_at=_COMPUTED_AT,
            agent_version=_AGENT_VERSION,
            run_date="2026-10-12",
        )

    def test_deficit_gap_headline_and_detail_exact(self):
        """W-b4: deficit headline matches G4-D7; detail is fixed; no VIN present (MB-8)."""
        assigned_vins = [_VIN_A, _VIN_C, _VIN_B]
        finding = self._build(_GAP_ITEM, _TO_LOCATION, assigned_vins)
        assert finding["headline"] == (
            "Seattle Airport: short 3 vehicles from 2026-10-20, no move recommended"
        ), f"W-b4 deficit headline: {finding['headline']!r}"
        assert finding["detail"] == "Rebalancing gap item."
        for vin in assigned_vins:
            assert vin not in finding["headline"], (
                f"W-b4: assigned VIN {vin!r} must not appear in deficit headline"
            )
            assert vin not in finding["detail"], (
                f"W-b4: assigned VIN {vin!r} must not appear in deficit detail"
            )

    def test_surplus_gap_headline_and_detail_exact(self):
        """W-b4: surplus headline matches G4-D7; detail is fixed; no VIN present."""
        assigned_vins = [_VIN_A, _VIN_C, _VIN_B]
        finding = self._build(_SURPLUS_GAP_ITEM, _FROM_LOCATION, assigned_vins)
        assert finding["headline"] == (
            "Seattle Downtown: 5 spare vehicles, no location in the metro is short"
        ), f"W-b4 surplus headline: {finding['headline']!r}"
        assert finding["detail"] == "Rebalancing gap item."
        for vin in assigned_vins:
            assert vin not in finding["headline"], (
                f"W-b4: assigned VIN {vin!r} must not appear in surplus headline"
            )
            assert vin not in finding["detail"], (
                f"W-b4: assigned VIN {vin!r} must not appear in surplus detail"
            )


class TestWriteFindingIfChanged:
    """write_finding_if_changed calls writer or refresher per hash comparison."""

    def test_same_hash_calls_refresher_not_writer(self):
        from agents.fi_rebalancing.write import write_finding_if_changed
        writer = MagicMock()
        refresher = MagicMock()
        write_finding_if_changed(
            live_item_hash="abc", new_hash="abc",
            finding={"finding_kind": "fleet.rebalance.move"},
            writer=writer, refresher=refresher,
        )
        writer.assert_not_called()
        refresher.assert_called_once()

    def test_different_hash_calls_writer_not_refresher(self):
        from agents.fi_rebalancing.write import write_finding_if_changed
        writer = MagicMock()
        refresher = MagicMock()
        write_finding_if_changed(
            live_item_hash="old", new_hash="new",
            finding={"finding_kind": "fleet.rebalance.move"},
            writer=writer, refresher=refresher,
        )
        writer.assert_called_once()
        refresher.assert_not_called()

    def test_no_live_hash_calls_writer(self):
        """None live_item_hash (no existing row) always triggers the writer."""
        from agents.fi_rebalancing.write import write_finding_if_changed
        writer = MagicMock()
        refresher = MagicMock()
        write_finding_if_changed(
            live_item_hash=None, new_hash="new",
            finding={},
            writer=writer, refresher=refresher,
        )
        writer.assert_called_once()
        refresher.assert_not_called()

    def test_same_hash_no_refresher_does_nothing(self):
        """Equal hashes with no refresher: writer is still not called."""
        from agents.fi_rebalancing.write import write_finding_if_changed
        writer = MagicMock()
        write_finding_if_changed(
            live_item_hash="same", new_hash="same",
            finding={},
            writer=writer,
        )
        writer.assert_not_called()



def test_vehicle_evidence_carries_the_eligibility_facts():
    from agents.fi_rebalancing.evidence import move_evidence

    plan = dict(_PLAN)
    entries = move_evidence(plan, _CANDIDATE, computed_at="2026-10-20T06:00:00Z",
                            from_location=_FROM_LOCATION, to_location=_TO_LOCATION,
                            outcomes=[], events=[])
    vehicle = [e for e in entries if e["kind"] == "rebalance.vehicle"]
    assert {e["vin"] for e in vehicle} == set(plan["vehicle_vins"])
    for e in vehicle:
        assert e["value"]["days_idle_last_7"] == 3
        # S4: key must name the 3-day window; None means nothing in today..today+3,
        # not "nothing ever scheduled".  An assertion (not a lookup) so mutant F3-S4
        # (which reverts the key to next_service_in_days) produces CAUGHT not INCIDENTAL.
        assert "next_service_in_days_next_3d" in e["value"], (
            "S4: evidence key must be 'next_service_in_days_next_3d' (names the window)"
        )
        assert e["value"]["next_service_in_days_next_3d"] is None
        assert e["value"]["open_lower_severity_items"] == ["routine"]


def test_a_plan_vin_without_eligibility_facts_is_refused():
    from agents.fi_rebalancing.evidence import move_evidence

    cand = {**_CANDIDATE, "eligible_facts": {}}
    with pytest.raises(ValueError):
        move_evidence(_PLAN, cand, computed_at="2026-10-20T06:00:00Z",
                      from_location=_FROM_LOCATION, to_location=_TO_LOCATION,
                      outcomes=[], events=[])
