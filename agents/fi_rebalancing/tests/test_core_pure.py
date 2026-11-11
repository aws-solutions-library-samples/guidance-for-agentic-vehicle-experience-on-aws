"""Boundary tests for AVX fi-rebalancing pure-function core (task 4.1a).

Covers:
- eligibility boundary conditions (service exactly 3.0 days, booking exactly 24.0 h,
  missing ``in_service``, missing ``open_fi_items`` key, ranking by days_idle)
- great_circle_miles on two known points
- compute_inputs_hash list-order independence for *_vins keys
"""
from __future__ import annotations

import pytest

# ---------------------------------------------------------------------------
# Helpers (duplicated from core test to keep this file standalone)
# ---------------------------------------------------------------------------

_VIN_A = "MRDVEH00000000021"
_VIN_B = "MRDVEH00000000022"
_VIN_C = "MRDVEH00000000023"


def _vehicle(
    vin: str = _VIN_A,
    location_id: str = "loc-sea-01",
    is_grounded: bool = False,
    open_fi_items: list | None = None,
    hours_to_next_booking: float = 72.0,
    days_to_next_service: float = 30.0,
    in_service: bool = True,
    days_idle_last_7: int = 0,
) -> dict:
    rec = {
        "vin": vin,
        "fleet_id": "fleet-meridian-01",
        "location_id": location_id,
        "is_grounded": is_grounded,
        "open_items_read_error": False,
        "open_fi_items": open_fi_items if open_fi_items is not None else [],
        "hours_to_next_booking": hours_to_next_booking,
        "days_to_next_service": days_to_next_service,
        "in_service": in_service,
        "days_idle_last_7": days_idle_last_7,
    }
    return rec


# ---------------------------------------------------------------------------
# Eligibility boundaries
# ---------------------------------------------------------------------------


class TestEligibilityBoundaries:
    def test_service_exactly_3_days_excluded(self):
        """days_to_next_service == 3.0 must be excluded (spec G4-D2: > 3 required)."""
        from agents.fi_rebalancing.eligibility import filter_eligible

        result = filter_eligible([_vehicle(days_to_next_service=3.0)])
        assert _VIN_A not in [v["vin"] for v in result], (
            "Service exactly 3.0 days away excludes (requires > 3). "
            "Mutation: use < instead of <= → 3.0 passes → fails."
        )

    def test_service_just_above_3_days_eligible(self):
        """days_to_next_service == 3.001 must be eligible."""
        from agents.fi_rebalancing.eligibility import filter_eligible

        result = filter_eligible([_vehicle(days_to_next_service=3.001)])
        assert _VIN_A in [v["vin"] for v in result]

    def test_booking_exactly_24h_eligible(self):
        """hours_to_next_booking == 24.0 must be eligible (spec G4-D2: >= 24 required)."""
        from agents.fi_rebalancing.eligibility import filter_eligible

        result = filter_eligible([_vehicle(hours_to_next_booking=24.0)])
        assert _VIN_A in [v["vin"] for v in result], (
            "Booking exactly 24 h away is eligible (spec requires >= 24). "
            "Mutation: use > instead of >= → 24.0 excluded → fails."
        )

    def test_booking_just_under_24h_excluded(self):
        """hours_to_next_booking == 23.99 must be excluded."""
        from agents.fi_rebalancing.eligibility import filter_eligible

        result = filter_eligible([_vehicle(hours_to_next_booking=23.99)])
        assert _VIN_A not in [v["vin"] for v in result]

    def test_missing_in_service_excluded(self):
        """A vehicle dict without ``in_service`` key is ineligible (missing fact = ineligible)."""
        from agents.fi_rebalancing.eligibility import filter_eligible

        vehicle = _vehicle()
        del vehicle["in_service"]
        result = filter_eligible([vehicle])
        assert _VIN_A not in [v["vin"] for v in result], (
            "Missing in_service must be treated as ineligible. "
            "Mutation: default missing to True → VIN passes → fails."
        )

    def test_missing_open_fi_items_key_is_ok(self):
        """A vehicle dict with no ``open_fi_items`` key is ineligible (fail-closed, W-n1)."""
        from agents.fi_rebalancing.eligibility import filter_eligible

        vehicle = _vehicle()
        del vehicle["open_fi_items"]  # explicitly absent
        assert "open_fi_items" not in vehicle
        result = filter_eligible([vehicle])
        assert result == [], (
            "Missing open_fi_items key must be ineligible (fail-closed, W-n1). "
            "Mutation: restore the `or []` fallback → VIN passes → fails."
        )

    def test_missing_open_items_read_error_key_is_ineligible(self):
        """A record without open_items_read_error is ineligible (fail-closed, W-n1)."""
        from agents.fi_rebalancing.eligibility import filter_eligible

        vehicle = _vehicle()
        del vehicle["open_items_read_error"]
        assert "open_items_read_error" not in vehicle
        result = filter_eligible([vehicle])
        assert result == [], (
            "Missing open_items_read_error key must be ineligible (None is not False). "
            "Mutation: restore the `False` default → None passes → fails."
        )

    def test_non_list_open_fi_items_is_ineligible(self):
        """open_fi_items that is not a list makes the vehicle ineligible (W-n1).

        A tuple of clean items iterates fine, so a guard weakened to ``is None`` lets it through
        and this fails on its own assertion (S-c3-5; a string only failed by AttributeError).
        """
        from agents.fi_rebalancing.eligibility import filter_eligible

        for not_a_list in (({"severity": "routine"},), {}, "unexpected_string"):
            vehicle = _vehicle()
            vehicle["open_fi_items"] = not_a_list
            assert filter_eligible([vehicle]) == [], (
                f"Non-list open_fi_items {not_a_list!r} must be ineligible (fail-closed, W-n1)."
            )

    def test_ranking_by_days_idle_descending(self):
        """Eligible vehicles are ranked by days_idle_last_7 descending, then VIN asc."""
        from agents.fi_rebalancing.eligibility import filter_eligible

        va = _vehicle(vin=_VIN_A, days_idle_last_7=1)
        vb = _vehicle(vin=_VIN_B, days_idle_last_7=5)
        vc = _vehicle(vin=_VIN_C, days_idle_last_7=5)
        result = filter_eligible([va, vc, vb])
        vins = [v["vin"] for v in result]
        # VIN_B and VIN_C both have 5 idle days; tie broken by VIN asc.
        assert vins == [_VIN_B, _VIN_C, _VIN_A], (
            f"Expected [B, C, A] (5,5,1 idle days), got {vins}. "
            "Mutation: ascending idle sort → A first → fails."
        )


# ---------------------------------------------------------------------------
# great_circle_miles
# ---------------------------------------------------------------------------


class TestGreatCircleMiles:
    def test_jfk_to_lax_approx(self):
        """JFK (40.64, -73.78) to LAX (33.94, -118.41) ≈ 2,468 mi (known value)."""
        from agents.fi_rebalancing.formulas import great_circle_miles

        d = great_circle_miles(40.6413, -73.7781, 33.9425, -118.4081)
        assert 2450 <= d <= 2490, f"JFK→LAX expected ~2468 mi, got {d:.1f}"

    def test_same_point_is_zero(self):
        """Distance from a point to itself is zero."""
        from agents.fi_rebalancing.formulas import great_circle_miles

        assert great_circle_miles(47.6, -122.3, 47.6, -122.3) == pytest.approx(0.0, abs=1e-6)

    def test_road_factor_applied_externally(self):
        """great_circle_miles returns great-circle miles; caller applies road_factor × 1.35."""
        from agents.fi_rebalancing.formulas import great_circle_miles, DEFAULTS

        gc = great_circle_miles(47.6, -122.33, 47.61, -122.34)
        road = gc * DEFAULTS["road_factor"]
        assert road > gc  # road factor > 1 so road distance exceeds great-circle


# ---------------------------------------------------------------------------
# compute_inputs_hash — list-order independence for *_vins keys
# ---------------------------------------------------------------------------


class TestInputsHashVinsOrder:
    def test_eligible_vins_order_does_not_matter(self):
        """compute_inputs_hash must sort *_vins lists before hashing."""
        from agents.fi_rebalancing.evidence import compute_inputs_hash

        a = {"forecast_shortfall": 2.0, "eligible_vins": [_VIN_A, _VIN_B]}
        b = {"forecast_shortfall": 2.0, "eligible_vins": [_VIN_B, _VIN_A]}
        assert compute_inputs_hash(a) == compute_inputs_hash(b), (
            "VIN list order must not affect the hash (spec G4-D8: sort *_vins). "
            "Mutation: omit sort → hashes differ → fails."
        )

    def test_vehicle_vins_order_does_not_matter(self):
        """vehicle_vins key is also sorted before hashing."""
        from agents.fi_rebalancing.evidence import compute_inputs_hash

        a = {"forecast_shortfall": 1.0, "vehicle_vins": [_VIN_C, _VIN_A]}
        b = {"forecast_shortfall": 1.0, "vehicle_vins": [_VIN_A, _VIN_C]}
        assert compute_inputs_hash(a) == compute_inputs_hash(b)

    def test_different_vins_produce_different_hash(self):
        """Different VIN sets yield different hashes (sanity / anti-stubbing check)."""
        from agents.fi_rebalancing.evidence import compute_inputs_hash

        a = {"eligible_vins": [_VIN_A]}
        b = {"eligible_vins": [_VIN_B]}
        assert compute_inputs_hash(a) != compute_inputs_hash(b), (
            "Different VIN sets must produce different hashes. "
            "Mutation: always return constant hash → equal → fails."
        )



# ---------------------------------------------------------------------------
# U7: a grounded vehicle, or one whose grounding could not be read, is never a
# candidate. Each case pairs with the clean control above (test_eligible_vehicle_
# passes_all_checks) so an over-filtering implementation fails there.
# ---------------------------------------------------------------------------

def _clean(**overrides):
    record = {
        "vin": "MRDVEH00000000031",
        "is_grounded": False,
        "open_items_read_error": False,
        "open_fi_items": [],
        "hours_to_next_booking": 72.0,
        "days_to_next_service": float("inf"),
        "in_service": True,
        "days_idle_last_7": 1,
    }
    record.update(overrides)
    return record


def test_u7_clean_control_is_eligible():
    from agents.fi_rebalancing.eligibility import filter_eligible

    assert [v["vin"] for v in filter_eligible([_clean()])] == ["MRDVEH00000000031"]


@pytest.mark.parametrize(
    "overrides",
    [
        {"is_grounded": True},
        {"is_grounded": None},           # flag never set: unknown counts as grounded
        {"is_grounded": "false"},        # a string, not a bool: unknown
        {"grounding_read_error": True},  # Query failed
        {"grounding_read_error": "x"},   # any non-False error marker
        {"is_grounded": False, "grounding_read_error": True},
    ],
    ids=["grounded", "flag-missing", "flag-string", "read-error", "error-marker", "clean-flag-but-error"],
)
def test_u7_grounded_or_unknown_grounding_is_never_eligible(overrides):
    from agents.fi_rebalancing.eligibility import filter_eligible

    record = _clean(**overrides)
    if overrides.get("is_grounded", False) is None:
        del record["is_grounded"]
    assert filter_eligible([record]) == []


def test_open_items_read_error_excludes():
    from agents.fi_rebalancing.eligibility import filter_eligible

    assert filter_eligible([_clean(open_items_read_error=True)]) == []


def test_open_repair_order_folded_as_zero_days_excludes():
    from agents.fi_rebalancing.eligibility import filter_eligible

    assert filter_eligible([_clean(days_to_next_service=0.0)]) == []


@pytest.mark.parametrize("value", [None, "true", 1])
def test_in_service_must_be_true(value):
    from agents.fi_rebalancing.eligibility import filter_eligible

    assert filter_eligible([_clean(in_service=value)]) == []



@pytest.mark.parametrize("key", ["days_to_next_service", "hours_to_next_booking"])
def test_missing_schedule_fact_is_ineligible(key):
    """The reader writes inf when nothing is scheduled; an absent key means it never read."""
    from agents.fi_rebalancing.eligibility import filter_eligible

    record = _clean()
    del record[key]
    assert filter_eligible([record]) == []
