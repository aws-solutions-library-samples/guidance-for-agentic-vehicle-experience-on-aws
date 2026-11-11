"""Red tests: AVX fi-rebalancing deterministic core and model-output validation.

Spec: `.kiro/specs/2026-10-06-fi-rebalancing-stage-e/spec.md` D8–D10.
Contract: `docs/fi-rebalancing-contract.md`.

All tests are xfail(strict=True) until the named task lands. Tests that cover
D8 steps 1–5, 7–8 and D9–D10 properties name task 4.1; tests that cover D8
step 6 validation (model output) name task 5.1.

Future modules (`agents.fi_rebalancing.*`) are imported inside test bodies so
pytest collection never errors.

Run:
    .venv/bin/python -m pytest agents/fi_rebalancing/tests -q
"""
from __future__ import annotations

import json
import pathlib
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Shared fixture data
# ---------------------------------------------------------------------------

_FLEET_ID = "fleet-meridian-01"
_METRO = "seattle"

_LOC_DEFICIT = "loc-sea-airport-01"
_LOC_SURPLUS = "loc-sea-downtown-02"

_VIN_A = "MRDVEH00000000021"
_VIN_B = "MRDVEH00000000022"
_VIN_C = "MRDVEH00000000023"

_ISO_WEEK = "2026-W42"

#: Path to the shared formula fixture (contract § 7, Stage B D7 pattern).
_FIXTURE_PATH = pathlib.Path(__file__).parent / "fixtures" / "rebalance_formulas.json"


def _load_fixtures() -> dict:
    return json.loads(_FIXTURE_PATH.read_text())


def _view_row(
    location_id: str = _LOC_DEFICIT,
    status: str = "deficit",
    turned_away_share: float = 0.25,
    forecast_week: str = _ISO_WEEK,
    metro: str = _METRO,
    fleet_id: str = _FLEET_ID,
) -> dict[str, Any]:
    return {
        "fleet_id": fleet_id,
        "location_id": location_id,
        "metro": metro,
        "status": status,
        "turned_away_share": turned_away_share,
        "forecast_week": forecast_week,
        "vehicles_short": 3,
    }


def _surplus_row(
    location_id: str = _LOC_SURPLUS,
    vehicles_spare: int = 5,
    metro: str = _METRO,
    fleet_id: str = _FLEET_ID,
) -> dict[str, Any]:
    return {
        "fleet_id": fleet_id,
        "location_id": location_id,
        "metro": metro,
        "status": "surplus",
        "vehicles_spare": vehicles_spare,
        "parking_spaces": 20,
        "assigned_count": 10,
    }


def _vehicle_record(
    vin: str = _VIN_A,
    location_id: str = _LOC_SURPLUS,
    is_grounded: bool = False,
    open_fi_items: list[dict] | None = None,
    hours_to_next_booking: float = 72.0,
    days_to_next_service: float = 30.0,
    in_service: bool = True,
) -> dict[str, Any]:
    """Each open item must carry both 'kind' and 'severity' (spec D8 steps 3–4).

    Critical 2 fix: severity is explicit so p0/attention tests are distinguishable
    from each other and from a severity-blind exclusion filter.
    """
    return {
        "vin": vin,
        "fleet_id": _FLEET_ID,
        "location_id": location_id,
        "is_grounded": is_grounded,
        "open_items_read_error": False,
        "open_fi_items": open_fi_items or [],
        "hours_to_next_booking": hours_to_next_booking,
        "days_to_next_service": days_to_next_service,
        "in_service": in_service,
        "days_idle_last_7": 2,
        "battery_soh_band": "good",
    }


# ---------------------------------------------------------------------------
# D8 step 1: Deficit selection
# ---------------------------------------------------------------------------


class TestDeficitSelection:
    """Deficit locations are selected from the view rows only (spec D8 step 1)."""

    def test_deficit_selection_uses_view_rows_only(self):
        """select_deficit_locations returns only rows whose status == 'deficit'."""
        from agents.fi_rebalancing.selection import select_deficit_locations  # type: ignore[import]

        deficit = _view_row(status="deficit")
        surplus = _view_row(location_id=_LOC_SURPLUS, status="surplus")
        balanced = _view_row(location_id="loc-sea-03", status="balanced")

        result = select_deficit_locations([deficit, surplus, balanced])

        assert len(result) == 1
        assert result[0]["location_id"] == _LOC_DEFICIT

    def test_deficit_selection_empty_view_returns_nothing(self):
        """Empty view → empty deficit list (no model call, no write)."""
        from agents.fi_rebalancing.selection import select_deficit_locations  # type: ignore[import]

        result = select_deficit_locations([])
        assert result == []


# ---------------------------------------------------------------------------
# D8 step 3: Eligibility
# ---------------------------------------------------------------------------


class TestEligibility:
    """Vehicles are excluded by eligibility rules (spec D8 step 3)."""

    def test_grounded_vehicle_excluded(self):
        """A vehicle with an open grounding is not in the eligible list."""
        from agents.fi_rebalancing.eligibility import filter_eligible  # type: ignore[import]

        vehicles = [_vehicle_record(is_grounded=True)]
        result = filter_eligible(vehicles)
        assert _VIN_A not in [v["vin"] for v in result], (
            "A grounded vehicle must not appear in the eligible list. "
            "Mutation: remove grounding check → grounded VIN appears → this fails."
        )

    def test_grounding_read_error_treated_as_grounded(self):
        """A read error on the grounding table counts as grounded (safe direction)."""
        from agents.fi_rebalancing.eligibility import filter_eligible  # type: ignore[import]

        vehicle = _vehicle_record(is_grounded=False)
        vehicle["grounding_read_error"] = True  # sentinel for a failed read

        result = filter_eligible([vehicle])
        assert _VIN_A not in [v["vin"] for v in result], (
            "A grounding read error must count as grounded. "
            "Mutation: treat read error as 'not grounded' → VIN passes → this fails."
        )

    def test_open_p0_finding_excludes_vehicle(self):
        """A vehicle with an open p0-severity FI item is excluded (spec D8 step 3).

        This test asserts **exclusion on presence**: any vehicle carrying at least one
        open FI item with severity ``p0`` must not appear in the eligible list.  It does
        NOT assert that the filter reads ``severity``; a severity-blind filter that
        excludes every vehicle with ``open_fi_items`` would also pass this test.
        See ``test_filter_reads_severity_not_presence`` for the guard that catches a
        severity-blind implementation.
        """
        from agents.fi_rebalancing.eligibility import filter_eligible  # type: ignore[import]

        vehicles = [
            _vehicle_record(
                open_fi_items=[{"kind": "fleet.recall.vehicle", "severity": "p0"}]
            )
        ]
        result = filter_eligible(vehicles)
        assert _VIN_A not in [v["vin"] for v in result], (
            "A vehicle with an open p0 item must be excluded (D8 step 3). "
            "Mutation: allow p0 items through → VIN appears → this fails."
        )

    def test_open_attention_finding_excludes_vehicle(self):
        """A vehicle with an open 'attention'-severity FI item is excluded (spec D8 step 3).

        This test asserts **exclusion on presence**: any vehicle carrying at least one
        open FI item with severity ``attention`` must not appear in the eligible list.
        It does NOT assert that the filter reads ``severity``; a severity-blind filter
        that excludes every vehicle with ``open_fi_items`` would also pass this test.
        See ``test_filter_reads_severity_not_presence`` for the guard that catches a
        severity-blind implementation (N1).
        """
        from agents.fi_rebalancing.eligibility import filter_eligible  # type: ignore[import]

        vehicles = [
            _vehicle_record(
                open_fi_items=[{"kind": "fleet.health.battery", "severity": "attention"}]
            )
        ]
        result = filter_eligible(vehicles)
        assert _VIN_A not in [v["vin"] for v in result], (
            "A vehicle with an open attention item must be excluded (D8 step 3). "
            "Mutation: allow attention items through → VIN appears → this fails."
        )

    def test_filter_reads_severity_not_presence(self):
        """The filter must read the ``severity`` field, not merely check item presence (N1).

        Mutation N1 (severity-blind on ``open_fi_items``): a filter that excludes any
        vehicle with non-empty ``open_fi_items`` — regardless of severity — would exclude
        VIN_A (routine item) and also correctly exclude VIN_B (p0 item), but this test
        has a third vehicle VIN_C with no items, which the routine vehicle must equal in
        eligibility.  The decisive assertion is that VIN_A (routine) IS eligible while
        VIN_B (p0) is NOT — a severity-blind filter cannot satisfy both at once when
        VIN_A has items and VIN_B has items.

        Specifically: N1 excludes VIN_A (routine item present) → VIN_A absent from result
        → ``_VIN_A in eligible_vins`` fails → xfail catches N1.
        """
        from agents.fi_rebalancing.eligibility import filter_eligible  # type: ignore[import]

        routine_vehicle = _vehicle_record(
            vin=_VIN_A,
            open_fi_items=[{"kind": "fleet.rebalance.gap", "severity": "routine"}],
        )
        p0_vehicle = _vehicle_record(
            vin=_VIN_B,
            open_fi_items=[{"kind": "fleet.recall.vehicle", "severity": "p0"}],
        )
        result = filter_eligible([routine_vehicle, p0_vehicle])
        eligible_vins = [v["vin"] for v in result]

        # Routine severity → vehicle stays eligible (D8 step 3 positive direction).
        assert _VIN_A in eligible_vins, (
            "A vehicle with only routine-severity items must remain eligible (D8 step 3). "
            "Mutation N1 (severity-blind): excludes VIN_A because it has items → absent → fails."
        )
        # p0 severity → vehicle excluded (negative direction; catches N1 if it's
        # severity-blind in the opposite sense of including everything).
        assert _VIN_B not in eligible_vins, (
            "A vehicle with a p0 item must be excluded (D8 step 3). "
            "Mutation N1: if it only looks at presence, it would include everything or nothing "
            "— the combination of VIN_A included + VIN_B excluded is the severity property."
        )

    def test_routine_only_vehicle_stays_eligible_and_item_in_evidence(self):
        """A vehicle with only routine-severity items stays eligible; items carried as evidence.

        Spec D8 steps 3–4: exclude only p0/attention; lower-severity items are evidence.
        The p0 vehicle in this test is included so the evidence assertion (D8 step 4) is
        paired with an exclusion check.  The severity property — that the filter reads the
        ``severity`` field, not just item presence — is guarded by
        ``test_filter_reads_severity_not_presence`` (N1); this test guards the evidence
        chain.
        """
        from agents.fi_rebalancing.eligibility import filter_eligible  # type: ignore[import]

        routine_vehicle = _vehicle_record(
            vin=_VIN_A,
            open_fi_items=[{"kind": "fleet.rebalance.gap", "severity": "routine"}],
        )
        p0_vehicle = _vehicle_record(
            vin=_VIN_B,
            open_fi_items=[{"kind": "fleet.recall.vehicle", "severity": "p0"}],
        )
        result = filter_eligible([routine_vehicle, p0_vehicle])
        eligible_vins = [v["vin"] for v in result]

        # Routine item → vehicle stays eligible (positive half).
        assert _VIN_A in eligible_vins, (
            "A vehicle with only routine-severity items must remain eligible (D8 step 3). "
            "Mutation: exclude any open item → VIN_A absent → this fails."
        )
        # p0 item → vehicle excluded (negative half that catches severity-blind filters).
        assert _VIN_B not in eligible_vins, (
            "A vehicle with a p0 item must be excluded (D8 step 3). "
            "Mutation M2 (checks wrong field, excludes nothing): VIN_B in result → fails."
        )
        # D8 step 4: the routine item must be carried in the eligible record.
        matching = next((v for v in result if v["vin"] == _VIN_A), None)
        assert matching is not None
        assert matching.get("open_fi_items"), (
            "The routine item must be present in the eligible record (D8 step 4 evidence). "
            "Mutation: strip open_fi_items before returning → item absent → this fails."
        )

    def test_mixed_severity_vehicle_excluded(self):
        """A vehicle with any p0 or attention item is excluded, even if it also has routine items.

        D8 step 3: exclude a vehicle with *any* open p0 or attention item.  A vehicle
        carrying both a routine gap and a p0 recall must be excluded — the routine item
        does not override the p0.

        Mutation N7 (all() instead of any()): a filter that only excludes a vehicle when
        ALL its open items are disqualifying would pass the routine item and leave
        VIN_C eligible → assert ``_VIN_C not in eligible_vins`` fails → N7 caught.
        A first-item-only check survives for the same reason (routine is first).
        """
        from agents.fi_rebalancing.eligibility import filter_eligible  # type: ignore[import]

        mixed_vehicle = _vehicle_record(
            vin=_VIN_C,
            open_fi_items=[
                {"kind": "fleet.rebalance.gap", "severity": "routine"},
                {"kind": "fleet.recall.vehicle", "severity": "p0"},
            ],
        )
        result = filter_eligible([mixed_vehicle])
        assert _VIN_C not in [v["vin"] for v in result], (
            "A vehicle with both routine and p0 items must be excluded (D8 step 3: any "
            "disqualifying item excludes). "
            "Mutation N7 (all() gate): routine item is not disqualifying, so all() is False "
            "→ vehicle stays eligible → VIN_C present → fails."
        )

    def test_booking_within_24h_excludes_vehicle(self):
        """A vehicle with a booking starting in the next 24 hours is excluded."""
        from agents.fi_rebalancing.eligibility import filter_eligible  # type: ignore[import]

        vehicles = [_vehicle_record(hours_to_next_booking=20.0)]
        result = filter_eligible(vehicles)
        assert _VIN_A not in [v["vin"] for v in result], (
            "A vehicle with a booking in < 24 h must be excluded. "
            "Mutation: remove 24-h booking check → VIN appears → this fails."
        )

    def test_service_within_3_days_excludes_vehicle(self):
        """A vehicle with a service visit in the next 3 days is excluded."""
        from agents.fi_rebalancing.eligibility import filter_eligible  # type: ignore[import]

        vehicles = [_vehicle_record(days_to_next_service=2.5)]
        result = filter_eligible(vehicles)
        assert _VIN_A not in [v["vin"] for v in result], (
            "A vehicle with a service visit in ≤ 3 days must be excluded. "
            "Mutation: remove 3-day service check → VIN appears → this fails."
        )

    def test_eligible_vehicle_passes_all_checks(self):
        """A vehicle that passes all eligibility rules appears in the eligible list."""
        from agents.fi_rebalancing.eligibility import filter_eligible  # type: ignore[import]

        vehicles = [_vehicle_record()]  # default: not grounded, no issues, booking far
        result = filter_eligible(vehicles)
        assert _VIN_A in [v["vin"] for v in result], (
            "A vehicle that passes all eligibility rules must be in the eligible list. "
            "Positive control: if this fails, eligibility is over-filtering."
        )


# ---------------------------------------------------------------------------
# D9 formulas
# ---------------------------------------------------------------------------


class TestFormulas:
    """D9 formulas produce deterministic values from fixed vectors (contract § 7)."""

    def test_transfer_cost_formula_matches_fixture(self):
        """Transfer cost computation matches the spec's '12-mile move ≈ $42' example."""
        from agents.fi_rebalancing.formulas import compute_transfer_cost  # type: ignore[import]

        fx = _load_fixtures()["transfer_cost"]
        result = compute_transfer_cost(**fx["inputs"])
        assert abs(result - fx["expected_usd"]) <= fx["tolerance"], (
            f"Expected transfer_cost ≈ {fx['expected_usd']}, got {result}. "
            "Mutation: wrong formula constant → value shifts outside tolerance → this fails."
        )

    def test_revenue_gain_per_day_limited_by_forecast_unserved(self):
        """Gain capped by forecast_unserved when unserved < moved-vehicle capacity."""
        from agents.fi_rebalancing.formulas import compute_revenue_gain_per_day  # type: ignore[import]

        fx = _load_fixtures()["revenue_gain_per_day"]
        result = compute_revenue_gain_per_day(**fx["inputs"])
        assert abs(result - fx["expected_usd"]) <= fx["tolerance"], (
            f"Expected revenue gain/day ≈ {fx['expected_usd']}, got {result}. "
            "Mutation: omit the min() cap → gain exceeds forecast_unserved → this fails."
        )

    def test_revenue_gain_per_day_capped_by_moved_vehicle_capacity(self):
        """Gain capped by moved-vehicle hours when capacity < forecast_unserved."""
        from agents.fi_rebalancing.formulas import compute_revenue_gain_per_day  # type: ignore[import]

        fx = _load_fixtures()["revenue_gain_per_day_capped_by_moved"]
        result = compute_revenue_gain_per_day(**fx["inputs"])
        assert abs(result - fx["expected_usd"]) <= fx["tolerance"], (
            f"Expected revenue gain/day ≈ {fx['expected_usd']}, got {result}. "
            "Mutation: omit the min() cap on the moved-vehicle side → gain too high → this fails."
        )

    @pytest.mark.parametrize("case", _load_fixtures()["confidence_bands"])
    def test_confidence_band_matches_fixture(self, case: dict):
        """Confidence band is determined by MAPE and optionally by recent outcome ratio."""
        from agents.fi_rebalancing.formulas import compute_confidence_band  # type: ignore[import]

        band = compute_confidence_band(
            forecast_mape=case["mape"],
            recent_outcomes_ratio=case["recent_outcomes_ratio"],
        )
        assert band == case["expected"], (
            f"mape={case['mape']}, recent_outcomes_ratio={case['recent_outcomes_ratio']}: "
            f"expected band={case['expected']!r}, got {band!r}. "
            f"Comment: {case.get('_comment', '')}. "
            "Mutation: change MAPE threshold → band shifts → this fails."
        )

    @pytest.mark.parametrize("case", _load_fixtures()["severity_bands"])
    def test_severity_band_matches_fixture(self, case: dict):
        """Severity band is determined by turned-away share; result is never 'p0'."""
        from agents.fi_rebalancing.formulas import compute_severity_band  # type: ignore[import]

        band = compute_severity_band(turned_away_share=case["turned_away_share"])
        assert band == case["expected"], (
            f"turned_away_share={case['turned_away_share']}: "
            f"expected severity={case['expected']!r}, got {band!r}. "
            f"Comment: {case.get('_comment', '')}. "
            "Mutation: wrong threshold → band shifts → this fails."
        )
        assert band != "p0", (
            "Severity for rebalancing Findings is never 'p0' (spec D10). "
            "Mutation: use p0 as the top band → band=='p0' → this fails."
        )

    def test_severity_never_p0_for_max_turnaway(self):
        """100% turned-away share produces 'attention', not 'p0'."""
        from agents.fi_rebalancing.formulas import compute_severity_band  # type: ignore[import]

        band = compute_severity_band(turned_away_share=1.0)
        assert band == "attention", (
            "Maximum turned-away share (100%) must yield 'attention', not 'p0'. "
            "Contract § 2: rebalancing findings are never p0. "
            "Mutation: add a p0 band at the top → band=='p0' → this fails."
        )


# ---------------------------------------------------------------------------
# D8 step 5: inputs_hash
# ---------------------------------------------------------------------------


class TestInputsHash:
    """inputs_hash is computed before the model call and suppresses noise (spec D8 step 5)."""

    def test_inputs_hash_stable_when_forecast_changes_by_less_than_one_vehicle(self):
        """Rounding forecast shortfalls to whole vehicles keeps the hash unchanged."""
        from agents.fi_rebalancing.evidence import compute_inputs_hash  # type: ignore[import]

        evidence_a = {
            "forecast_shortfall": 2.3,  # rounds to 2
            "eligible_vins": [_VIN_A, _VIN_B],
        }
        evidence_b = {
            "forecast_shortfall": 2.7,  # also rounds to 2
            "eligible_vins": [_VIN_A, _VIN_B],
        }
        assert compute_inputs_hash(evidence_a) == compute_inputs_hash(evidence_b), (
            "Daily noise within one vehicle must not change inputs_hash. "
            "Mutation: hash raw floats without rounding → hashes differ → this fails."
        )

    def test_inputs_hash_changes_when_eligibility_changes(self):
        """Removing an eligible vehicle changes the hash (eligibility as yes/no)."""
        from agents.fi_rebalancing.evidence import compute_inputs_hash  # type: ignore[import]

        evidence_two = {"forecast_shortfall": 2.0, "eligible_vins": [_VIN_A, _VIN_B]}
        evidence_one = {"forecast_shortfall": 2.0, "eligible_vins": [_VIN_A]}

        assert compute_inputs_hash(evidence_two) != compute_inputs_hash(evidence_one), (
            "A change in eligible VINs must change inputs_hash. "
            "Mutation: ignore the VIN list when hashing → hashes match → this fails."
        )


# ---------------------------------------------------------------------------
# D10: one item per subject_key
# ---------------------------------------------------------------------------


class TestFindingShape:
    """Each run produces one item per subject_key; re-run on same hash skips write."""

    def test_one_item_per_subject_key_per_run(self):
        """build_finding_items deduplicates: exactly one item for identical signals, and
        the subject_key matches the contract's canonical form (contract § 2.1, spec D10).

        Warning M3 fix: ``len(items) == 1`` catches mutation M3 (``build_finding_items``
        returns ``[]``): 0 ≠ 1 → xfail (not xpass).  The exact key check ensures the
        format is ``<from>/<to>/<ISO week>``, not a hash or an opaque id.  The two-weeks
        case ensures different weeks produce separate items (no over-merging).
        """
        from agents.fi_rebalancing.write import build_finding_items  # type: ignore[import]

        _ISO_WEEK_2 = "2026-W43"

        # Two identical signals for the same (from, to, week) → exactly one item.
        signals_same = [
            {"from_location_id": _LOC_SURPLUS, "to_location_id": _LOC_DEFICIT,
             "forecast_week": _ISO_WEEK, "vehicle_vins": [_VIN_A]},
            {"from_location_id": _LOC_SURPLUS, "to_location_id": _LOC_DEFICIT,
             "forecast_week": _ISO_WEEK, "vehicle_vins": [_VIN_A]},
        ]
        items = build_finding_items(signals_same, fleet_id=_FLEET_ID)
        assert len(items) == 1, (
            f"Two identical signals must produce exactly 1 item, got {len(items)}. "
            "Mutation M3 (return []): 0 items → fails here."
        )
        expected_key = f"{_LOC_SURPLUS}/{_LOC_DEFICIT}/{_ISO_WEEK}"
        assert items[0]["subject_key"] == expected_key, (
            f"Expected subject_key={expected_key!r}, got {items[0].get('subject_key')!r}. "
            "Contract § 2.1: subject_key format is '<from>/<to>/<ISO week>'."
        )

        # Two signals for different weeks → two distinct items.
        signals_two_weeks = [
            {"from_location_id": _LOC_SURPLUS, "to_location_id": _LOC_DEFICIT,
             "forecast_week": _ISO_WEEK, "vehicle_vins": [_VIN_A]},
            {"from_location_id": _LOC_SURPLUS, "to_location_id": _LOC_DEFICIT,
             "forecast_week": _ISO_WEEK_2, "vehicle_vins": [_VIN_B]},
        ]
        items_two = build_finding_items(signals_two_weeks, fleet_id=_FLEET_ID)
        assert len(items_two) == 2, (
            f"Signals for two different weeks must produce 2 items, got {len(items_two)}. "
            "Mutation: merge all weeks into one item → 1 item → this fails."
        )
        keys = {i["subject_key"] for i in items_two}
        assert keys == {
            f"{_LOC_SURPLUS}/{_LOC_DEFICIT}/{_ISO_WEEK}",
            f"{_LOC_SURPLUS}/{_LOC_DEFICIT}/{_ISO_WEEK_2}",
        }, f"Subject keys mismatch: {keys!r}"

    def test_unchanged_inputs_hash_skips_model_call(self):
        """Skip-unchanged rule is biconditional: skips when hashes match, does not skip when they differ.

        Critical 1 fix: this test is biconditional.  The differing-hash case asserts
        ``skip is False``, which catches mutation M1 (``should_skip_metro → True``
        unconditionally): M1 returns True even when hashes differ → ``skip2 is False``
        fails → xfail (not xpass).
        The ``mock_model`` call is simulated by the test based on the skip decision, so
        ``assert_not_called()`` is load-bearing when skip is True.
        """
        from agents.fi_rebalancing.selection import should_skip_metro  # type: ignore[import]

        live_hash = "abc123deadbeef"
        mock_model = MagicMock()

        # Same hash → should skip; model must not be called.
        skip = should_skip_metro(live_item_hash=live_hash, candidate_hash=live_hash)
        assert skip is True, (
            "When hashes match, should_skip_metro must return True (no model call). "
            "Mutation: always False → skip==False → this fails."
        )
        if not skip:
            mock_model()  # caller would invoke the model
        mock_model.assert_not_called()

        # Different hash → must NOT skip; the caller would invoke the model.
        skip2 = should_skip_metro(live_item_hash=live_hash, candidate_hash="different_hash")
        assert skip2 is False, (
            "When hashes differ, should_skip_metro must return False (model call allowed). "
            "Mutation M1 (always True): skip2==True → this fails."
        )
        if not skip2:
            mock_model()
        mock_model.assert_called_once()

    def test_unchanged_inputs_hash_skips_write(self):
        """Write is suppressed when hashes match; write IS made when hashes differ.

        Critical 1 fix: biconditional.  The differing-hash case asserts the writer is
        called once, catching mutation M4 (``write_finding_if_changed`` never writes):
        M4 never calls the writer → ``mock_writer2.assert_called_once()`` fails → xfail.
        """
        from agents.fi_rebalancing.write import write_finding_if_changed  # type: ignore[import]

        mock_writer = MagicMock()
        write_finding_if_changed(
            live_item_hash="same_hash",
            new_hash="same_hash",
            finding={},
            writer=mock_writer,
        )
        mock_writer.assert_not_called()

        # Different hashes → writer IS called exactly once.
        mock_writer2 = MagicMock()
        write_finding_if_changed(
            live_item_hash="old_hash",
            new_hash="new_hash",
            finding={"finding_kind": "fleet.rebalance.move"},
            writer=mock_writer2,
        )
        mock_writer2.assert_called_once(), (
            "When hashes differ, write_finding_if_changed must call the writer once. "
            "Mutation M4 (never writes): writer never called → this fails."
        )

    def test_rules_fallback_produces_output_without_model_call(self):
        """The rules fallback produces move or gap findings with no model call."""
        from agents.fi_rebalancing.fallback import rules_fallback  # type: ignore[import]

        mock_model_call = MagicMock()
        candidates = [
            {
                "candidate_id": "cand-001",
                "from_location_id": _LOC_SURPLUS,
                "to_location_id": _LOC_DEFICIT,
                "eligible_vins": [_VIN_A, _VIN_B],
                "net_benefit_usd": 150.0,
                "count": 1,
                "vehicles_spare": 1,
                "vehicles_short": 1,
                "parking_room": 1,
                # Money fields required (W4: unchanged-count path must carry all keys).
                "transfer_cost_usd": 30.0,
                "revenue_gain_usd": 200.0,
                "source_loss_usd": 0.0,
                "revenue_impact_usd": 200.0,
                "predicted_on_rent_hours_gain": 60.0,
                "predicted_turnaways_avoided": 5,
            }
        ]
        result = rules_fallback(candidates=candidates, model_call=mock_model_call)

        mock_model_call.assert_not_called()
        assert len(result) >= 1, (
            "Rules fallback must produce at least one move item. "
            "Mutation: return empty list → assertion fails."
        )


# ---------------------------------------------------------------------------
# D8 step 6: model-output validation (task 5.1)
# ---------------------------------------------------------------------------


class TestModelOutputValidation:
    """Model output is validated before any write (spec D8 step 7)."""

    @pytest.mark.xfail(strict=True, reason="fi-rebalancing-stage-e task 5.1")
    def test_vin_outside_candidate_list_rejected(self):
        """A VIN not in the candidate's eligible list is rejected."""
        from agents.fi_rebalancing.validate import validate_model_output  # type: ignore[import]

        candidate_bundle = {
            "candidates": [
                {
                    "candidate_id": "cand-001",
                    "from_location_id": _LOC_SURPLUS,
                    "to_location_id": _LOC_DEFICIT,
                    "eligible_vins": [_VIN_A, _VIN_B],
                }
            ],
            "metro": _METRO,
        }
        model_output = {
            "moves": [
                {
                    "candidate_id": "cand-001",
                    "vehicle_vins": [_VIN_C],  # VIN_C is not in eligible_vins
                    "timing": "today",
                    "detail": "Move needed.",
                    "evidence_refs": [],
                }
            ],
            "holds": [],
        }
        ok, error = validate_model_output(model_output, candidate_bundle)
        assert not ok, (
            "A VIN outside the eligible list must fail validation. "
            "Mutation: remove VIN-in-bundle check → foreign VIN passes → this fails."
        )
        assert "VIN" in error or "vin" in error.lower(), (
            "Error message must name the offending VIN or the VIN constraint."
        )

    @pytest.mark.xfail(strict=True, reason="fi-rebalancing-stage-e task 5.1")
    def test_vehicle_in_two_moves_rejected(self):
        """A VIN that appears in two moves in the same metro is rejected."""
        from agents.fi_rebalancing.validate import validate_model_output  # type: ignore[import]

        candidate_bundle = {
            "candidates": [
                {
                    "candidate_id": "cand-001",
                    "from_location_id": _LOC_SURPLUS,
                    "to_location_id": _LOC_DEFICIT,
                    "eligible_vins": [_VIN_A, _VIN_B],
                },
                {
                    "candidate_id": "cand-002",
                    "from_location_id": _LOC_SURPLUS,
                    "to_location_id": "loc-sea-hub-03",
                    "eligible_vins": [_VIN_A, _VIN_C],
                },
            ],
            "metro": _METRO,
        }
        model_output = {
            "moves": [
                {
                    "candidate_id": "cand-001",
                    "vehicle_vins": [_VIN_A],
                    "timing": "today",
                    "detail": "First move.",
                    "evidence_refs": [],
                },
                {
                    "candidate_id": "cand-002",
                    "vehicle_vins": [_VIN_A],  # VIN_A already used in cand-001
                    "timing": "today",
                    "detail": "Second move.",
                    "evidence_refs": [],
                },
            ],
            "holds": [],
        }
        ok, error = validate_model_output(model_output, candidate_bundle)
        assert not ok, (
            "A VIN appearing in two moves must fail validation. "
            "Mutation: remove the VIN-uniqueness check → both moves pass → this fails."
        )

    @pytest.mark.xfail(strict=True, reason="fi-rebalancing-stage-e task 5.1")
    def test_source_above_85pct_utilization_after_move_rejected(self):
        """A move that leaves the source above 85% forecast utilization is rejected."""
        from agents.fi_rebalancing.validate import validate_model_output  # type: ignore[import]

        candidate_bundle = {
            "candidates": [
                {
                    "candidate_id": "cand-tight",
                    "from_location_id": _LOC_SURPLUS,
                    "to_location_id": _LOC_DEFICIT,
                    "eligible_vins": [_VIN_A, _VIN_B],
                    "source_forecast_utilization_after_move": 0.87,  # > 0.85 after removing 2
                }
            ],
            "metro": _METRO,
        }
        model_output = {
            "moves": [
                {
                    "candidate_id": "cand-tight",
                    "vehicle_vins": [_VIN_A, _VIN_B],
                    "timing": "within_3_days",
                    "detail": "Demand shift.",
                    "evidence_refs": [],
                }
            ],
            "holds": [],
        }
        ok, error = validate_model_output(model_output, candidate_bundle)
        assert not ok, (
            "A move leaving the source above 85% utilization must fail validation. "
            "Mutation: remove the 85% utilization cap → over-drawn source passes → this fails."
        )

    @pytest.mark.xfail(strict=True, reason="fi-rebalancing-stage-e task 5.1")
    def test_temporary_event_hold_without_announced_event_rejected(self):
        """A 'temporary_event' hold reason without an announced event in evidence is rejected."""
        from agents.fi_rebalancing.validate import validate_model_output  # type: ignore[import]

        candidate_bundle = {
            "candidates": [],  # no moves → gap candidate
            "gaps": [
                {
                    "gap_id": "gap-001",
                    "location_id": _LOC_DEFICIT,
                    "events": [],  # no announced events
                }
            ],
            "metro": _METRO,
        }
        model_output = {
            "moves": [],
            "holds": [
                {
                    "gap_id": "gap-001",
                    "reason": "temporary_event",  # reason requires an event
                    "detail": "There is a big game this week.",
                }
            ],
        }
        ok, error = validate_model_output(model_output, candidate_bundle)
        assert not ok, (
            "A 'temporary_event' hold without an announced event must fail validation. "
            "Mutation: remove the event-presence check → unsubstantiated hold passes → this fails."
        )

    @pytest.mark.xfail(strict=True, reason="fi-rebalancing-stage-e task 5.1")
    def test_valid_move_with_all_checks_passes(self):
        """Positive control: a model output that satisfies all constraints passes validation."""
        from agents.fi_rebalancing.validate import validate_model_output  # type: ignore[import]

        candidate_bundle = {
            "candidates": [
                {
                    "candidate_id": "cand-ok",
                    "from_location_id": _LOC_SURPLUS,
                    "to_location_id": _LOC_DEFICIT,
                    "eligible_vins": [_VIN_A, _VIN_B],
                    "source_forecast_utilization_after_move": 0.75,  # ≤ 0.85
                }
            ],
            "gaps": [],
            "metro": _METRO,
        }
        model_output = {
            "moves": [
                {
                    "candidate_id": "cand-ok",
                    "vehicle_vins": [_VIN_A],  # subset of eligible, single use
                    "timing": "today",
                    "detail": "Airport gap expected next week.",
                    "evidence_refs": ["rebalance.forecast"],
                }
            ],
            "holds": [],
        }
        ok, error = validate_model_output(model_output, candidate_bundle)
        assert ok, (
            f"A valid model output must pass validation. Got error: {error!r}. "
            "Positive control: if this fails, the validator is over-rejecting."
        )



# ---------------------------------------------------------------------------
# D9: net-benefit chain (Warning W4 fix — test vectors added to fixture)
# ---------------------------------------------------------------------------


class TestNetBenefitChain:
    """The complete net-benefit chain produces the ``estimated_value_usd`` the operator sees.

    Warning W4 fix: the fixture now covers source_loss_per_day, revenue_impact, net_benefit
    and the min_net_benefit_usd gate (spec D8 step 2, contract § 7 and § 11).
    These tests are tagged task 4.1 because the formulas live in
    ``agents/fi_rebalancing/formulas.py``.
    """

    def test_source_loss_per_day_matches_fixture(self):
        """Source loss = [unserved(F, S-m) - unserved(F, S)] × rate (spec D9, contract § 7).

        Zero-loss case: 10-vehicle, 72 h/day source losing 2.  Both before and after
        unserved hours are 0 (source within cap), so D9 yields $0.00/day.  A stub that
        uses the superseded formula (moved_vehicles × source_on_rent_hours_per_vehicle ×
        rate) fails with TypeError because the fixture no longer carries
        ``source_on_rent_hours_per_vehicle`` (N11 Critical 1).
        """
        from agents.fi_rebalancing.formulas import compute_source_loss_per_day  # type: ignore[import]

        fx = _load_fixtures()["source_loss_per_day"]
        result = compute_source_loss_per_day(**fx["inputs"])
        assert abs(result - fx["expected_usd"]) <= fx["tolerance"], (
            f"Expected source_loss_per_day ≈ {fx['expected_usd']}, got {result}. "
            "Mutation: wrong unserved formula → non-zero loss on within-cap source → fails."
        )

    def test_source_loss_per_day_nonzero_when_over_cap(self):
        """Source loss is non-zero when the source is pushed above its utilisation cap.

        Over-cap case: 10-vehicle, 120 h/day source (busy), 8 h op, losing 2.
        unserved(120, 10) = 44.0 h; unserved(120, 8) = 59.2 h; delta = 15.2 h × $3.00
        = $45.60/day (spec D9).  A stub that always returns zero source loss fails here,
        showing the formula responds to real demand surplus at the source.
        """
        from agents.fi_rebalancing.formulas import compute_source_loss_per_day  # type: ignore[import]

        fx = _load_fixtures()["source_loss_per_day_over_cap"]
        result = compute_source_loss_per_day(**fx["inputs"])
        assert abs(result - fx["expected_usd"]) <= fx["tolerance"], (
            f"Expected source_loss_per_day ≈ {fx['expected_usd']}, got {result}. "
            "Mutation: return 0 always → $0 ≠ $45.60 → fails."
        )

    def test_end_to_end_chain_composes(self):
        """The full D9 formula chain feeds each step's output into the next step's input.

        Warning W1 fix: the ``end_to_end_chain`` fixture vector is now read by a test that
        chains gain → loss → impact → cost → net and asserts both the final ``net_benefit``
        and ``passes_threshold``.  Each intermediate step is asserted too, so a corrupt
        fixture value (N8: gain set to 0, loss to 1e6) fails on that step's own assertion
        before the chain proceeds.

        Mutation N8: fixture corrupted (``revenue_gain_per_day.expected_usd = 0``, etc.) →
        ``gain == 28.00 ≠ 0`` → assertion on gain's expected value fails → caught.
        """
        from agents.fi_rebalancing.formulas import (  # type: ignore[import]
            compute_transfer_cost,
            compute_revenue_gain_per_day,
            compute_source_loss_per_day,
            compute_revenue_impact,
            compute_net_benefit,
            passes_net_benefit_threshold,
        )

        chain = _load_fixtures()["end_to_end_chain"]
        tol = chain["tolerance"]

        tc_inputs = {k: v for k, v in chain["transfer_cost"].items() if k != "expected_usd"}
        tc = compute_transfer_cost(**tc_inputs)
        assert abs(tc - chain["transfer_cost"]["expected_usd"]) <= tol, (
            f"transfer_cost chain step: expected {chain['transfer_cost']['expected_usd']}, got {tc}."
        )

        gain_inputs = {k: v for k, v in chain["revenue_gain_per_day"].items() if k != "expected_usd"}
        gain = compute_revenue_gain_per_day(**gain_inputs)
        assert abs(gain - chain["revenue_gain_per_day"]["expected_usd"]) <= tol, (
            f"revenue_gain_per_day chain step: expected {chain['revenue_gain_per_day']['expected_usd']}, got {gain}. "
            "Mutation N8: fixture sets expected to 0 → 28.00 ≠ 0 → fails."
        )

        loss_inputs = {k: v for k, v in chain["source_loss_per_day"].items() if k != "expected_usd"}
        loss = compute_source_loss_per_day(**loss_inputs)
        assert abs(loss - chain["source_loss_per_day"]["expected_usd"]) <= tol, (
            f"source_loss_per_day chain step: expected {chain['source_loss_per_day']['expected_usd']}, got {loss}. "
            "Mutation N8: fixture sets expected to 1e6 → fails."
        )

        impact = compute_revenue_impact(
            revenue_gain_per_day=gain,
            source_loss_per_day=loss,
            horizon_days=chain["revenue_impact"]["horizon_days"],
        )
        assert abs(impact - chain["revenue_impact"]["expected_usd"]) <= tol, (
            f"revenue_impact chain step: expected {chain['revenue_impact']['expected_usd']}, got {impact}."
        )

        net = compute_net_benefit(revenue_impact=impact, transfer_cost=tc)
        assert abs(net - chain["net_benefit"]["expected_usd"]) <= tol, (
            f"net_benefit chain step: expected {chain['net_benefit']['expected_usd']}, got {net}. "
            "Mutation N8: fixture sets expected to -999 → fails."
        )

        passes = passes_net_benefit_threshold(
            net_benefit=net,
            min_net_benefit_usd=chain["passes_threshold"]["min_net_benefit_usd"],
        )
        assert passes == chain["passes_threshold"]["should_pass"], (
            f"passes_threshold chain step: expected {chain['passes_threshold']['should_pass']}, got {passes}. "
            "Mutation N8: fixture sets should_pass to False → correct net 153.97 passes → True ≠ False → fails."
        )

    def test_revenue_impact_matches_fixture(self):
        """Revenue impact = (gain_per_day − loss_per_day) × 7 (contract § 7)."""
        from agents.fi_rebalancing.formulas import compute_revenue_impact  # type: ignore[import]

        fx = _load_fixtures()["revenue_impact"]
        result = compute_revenue_impact(**fx["inputs"])
        assert abs(result - fx["expected_usd"]) <= fx["tolerance"], (
            f"Expected revenue_impact ≈ {fx['expected_usd']}, got {result}. "
            "Mutation: omit the 7-day horizon → value ÷ 7 → fails."
        )

    def test_net_benefit_matches_fixture(self):
        """net_benefit = revenue_impact − transfer_cost (contract § 7)."""
        from agents.fi_rebalancing.formulas import compute_net_benefit  # type: ignore[import]

        fx = _load_fixtures()["net_benefit"]
        result = compute_net_benefit(**fx["inputs"])
        assert abs(result - fx["expected_usd"]) <= fx["tolerance"], (
            f"Expected net_benefit ≈ {fx['expected_usd']}, got {result}. "
            "Mutation: return revenue_impact without subtracting transfer_cost → too high → fails."
        )

    @pytest.mark.parametrize("case", _load_fixtures()["min_net_benefit_threshold"])
    def test_candidate_below_min_net_benefit_is_dropped(self, case: dict):
        """Candidates below min_net_benefit_usd are dropped (spec D8 step 2, contract § 11).

        Three cases: strictly above, exactly at (must pass), and strictly below threshold.
        Mutation: drop the threshold check → candidates below threshold pass → fails.
        """
        from agents.fi_rebalancing.formulas import passes_net_benefit_threshold  # type: ignore[import]

        passes = passes_net_benefit_threshold(
            net_benefit=case["net_benefit"],
            min_net_benefit_usd=case["min_net_benefit_usd"],
        )
        assert passes == case["should_pass"], (
            f"net_benefit={case['net_benefit']}, min={case['min_net_benefit_usd']}: "
            f"expected passes={case['should_pass']}, got {passes}. "
            f"Comment: {case.get('_comment', '')}. "
            "Mutation: remove threshold check → all candidates pass → boundary case fails."
        )
