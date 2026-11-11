"""Unit tests for agents/supervisor/vehicle_scope.py — DS-1.

Pins the bidi scope contract (same rules, different surface from triage):

* Vehicle-bound (custom:vehicleId): vehicle_id and vin pinned from claim / row.
  Header values differing are dropped. Malformed claim, row not found,
  no VIN on row, lookup error → DENY or ERROR.
* Fleet-bound (custom:fleetIds): vehicleId header required; GetItem;
  fleetId must be in list; vin must match (case-insensitive) if both present.
  Empty fleet list → DENY; missing vehicleId header → DENY; fleet mismatch → DENY.
* Neither claim → UNCHANGED, header values pass through.
* custom:driverId is always pinned when present, regardless of branch.

Mutations verified (each must turn a test red — see build-notes.md):
  (1) Drop the pin: vehicle-bound accepts header vid/vin unchanged →
      test_vehicle_bound_pins_claim_vehicle FAILS
  (2) VIN case mismatch accepted: case-sensitive deny on mismatch →
      test_vehicle_bound_case_insensitive_vin_match FAILS
  (3) Accept on failed read: STATUS_ERROR → STATUS_PASS →
      test_vehicle_bound_lookup_error_is_error FAILS
  (4) Empty fleet list unrestricted →
      test_fleet_bound_empty_fleet_ids_denied FAILS
  (5) Only one row in scope → partial pass →
      (bidi fleet has only one row; see TestFleetBound)
  (6) Skip on malformed claim →
      test_vehicle_bound_malformed_claim_denied FAILS

Run:
    python3 -m pytest agents/supervisor/tests/test_supervisor_vehicle_scope.py -v
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from agents.supervisor import vehicle_scope as vs

# ------------------------------------------------------------------
# Constants
# ------------------------------------------------------------------

CLAIM_VID = "VEH-TEST-0015"        # surrogate id — distinct from the VIN (spec D1)
CLAIM_VIN = "MRDN0000000000015"
OTHER_VID = "VEH-MRDN-0099"
OTHER_VIN = "MRDN0000000000099"
FLEET_A = "FLEET-MERIDIAN-ONBOARD"
FLEET_B = "FLEET-MERIDIAN-OFFBOARD"

VEHICLE_CLAIMS = {
    "sub": "driver-sub",
    "custom:vehicleId": CLAIM_VID,
    "custom:driverId": "DRV-MRDN-0015",
}
FLEET_CLAIMS_A = {
    "sub": "fleet-sub",
    "custom:fleetIds": FLEET_A,
}
NO_CLAIMS = {
    "sub": "staff-sub",
}


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _row(vin: str = CLAIM_VIN, fleet_id: str = "") -> dict:
    return {"vin": vin, "fleetId": fleet_id}


def _lookup_ok(vin: str = CLAIM_VIN, fleet_id: str = FLEET_A):
    def fn(vid: str) -> dict | None:
        return _row(vin, fleet_id)
    return fn


def _lookup_not_found(vid: str) -> dict | None:
    return None


def _lookup_error(vid: str) -> dict | None:
    raise RuntimeError("table unavailable")


# ==================================================================
# Vehicle-bound (custom:vehicleId) tests
# ==================================================================

class TestVehicleBound:

    def test_vehicle_id_pinned_to_claim(self):
        """Mutation (1): drop the pin → this test would FAIL."""
        scope = vs.resolve(VEHICLE_CLAIMS, OTHER_VID, OTHER_VIN, None, _lookup_ok())
        assert scope.vehicle_id == CLAIM_VID

    def test_vin_pinned_from_row(self):
        scope = vs.resolve(VEHICLE_CLAIMS, None, None, None, _lookup_ok())
        assert scope.vin == CLAIM_VIN

    def test_vin_pinned_is_true(self):
        scope = vs.resolve(VEHICLE_CLAIMS, None, None, None, _lookup_ok())
        assert scope.vin_pinned is True

    def test_header_dropped_when_different_vid(self):
        scope = vs.resolve(VEHICLE_CLAIMS, OTHER_VID, None, None, _lookup_ok())
        assert scope.header_dropped is True

    def test_header_dropped_when_different_vin(self):
        scope = vs.resolve(VEHICLE_CLAIMS, None, OTHER_VIN, None, _lookup_ok())
        assert scope.header_dropped is True

    def test_header_not_dropped_when_matching(self):
        scope = vs.resolve(VEHICLE_CLAIMS, CLAIM_VID, CLAIM_VIN, None, _lookup_ok())
        assert scope.header_dropped is False

    def test_case_insensitive_vin_match_not_dropped(self):
        """Mutation (2): case-sensitive deny → this test would FAIL."""
        scope = vs.resolve(VEHICLE_CLAIMS, CLAIM_VID, CLAIM_VIN.lower(), None, _lookup_ok())
        assert scope.header_dropped is False
        assert scope.ok

    def test_malformed_claim_is_denied(self):
        """Mutation (6): skip → this test would FAIL."""
        bad = {**VEHICLE_CLAIMS, "custom:vehicleId": "VEH SPACE"}
        scope = vs.resolve(bad, None, None, None, _lookup_error)
        assert not scope.ok
        assert scope.status == vs.STATUS_DENY

    @pytest.mark.parametrize("bad", ["x" * 65, "VEH/../0015", "'; drop", " "])
    def test_other_malformed_shapes_denied(self, bad):
        scope = vs.resolve(
            {**VEHICLE_CLAIMS, "custom:vehicleId": bad},
            None, None, None, _lookup_error,
        )
        assert scope.status == vs.STATUS_DENY

    def test_lookup_error_returns_error_status(self):
        """Mutation (3): accept on error → this test would FAIL."""
        scope = vs.resolve(VEHICLE_CLAIMS, None, None, None, _lookup_error)
        assert not scope.ok
        assert scope.status == vs.STATUS_ERROR

    def test_row_not_found_returns_error(self):
        scope = vs.resolve(VEHICLE_CLAIMS, None, None, None, _lookup_not_found)
        assert not scope.ok
        assert scope.status == vs.STATUS_ERROR

    def test_no_vin_on_row_returns_error(self):
        scope = vs.resolve(
            VEHICLE_CLAIMS, None, None, None,
            lambda vid: {"vin": "", "fleetId": ""},
        )
        assert not scope.ok
        assert scope.status == vs.STATUS_ERROR

    def test_driver_id_pinned_from_claim(self):
        scope = vs.resolve(VEHICLE_CLAIMS, None, None, "DRV-OTHER", _lookup_ok())
        assert scope.driver_id == "DRV-MRDN-0015"

    def test_driver_id_from_header_when_no_claim(self):
        claims = {k: v for k, v in VEHICLE_CLAIMS.items() if k != "custom:driverId"}
        scope = vs.resolve(claims, None, None, "DRV-HEADER", _lookup_ok())
        assert scope.driver_id == "DRV-HEADER"

    def test_lookup_called_with_claim_vehicle_id(self):
        called: list[str] = []

        def fn(vid: str):
            called.append(vid)
            return _row()

        vs.resolve(VEHICLE_CLAIMS, None, None, None, fn)
        assert called == [CLAIM_VID]


# ==================================================================
# Fleet-bound (custom:fleetIds) tests
# ==================================================================

class TestFleetBound:

    def test_vehicle_in_caller_fleet_passes(self):
        scope = vs.resolve(
            FLEET_CLAIMS_A,
            CLAIM_VID, CLAIM_VIN, None,
            _lookup_ok(vin=CLAIM_VIN, fleet_id=FLEET_A),
        )
        assert scope.ok
        assert scope.status == vs.STATUS_PASS

    def test_vehicle_in_other_fleet_denied(self):
        scope = vs.resolve(
            FLEET_CLAIMS_A,
            CLAIM_VID, CLAIM_VIN, None,
            _lookup_ok(vin=CLAIM_VIN, fleet_id=FLEET_B),
        )
        assert not scope.ok
        assert scope.status == vs.STATUS_DENY

    def test_vin_mismatch_denied(self):
        scope = vs.resolve(
            FLEET_CLAIMS_A,
            CLAIM_VID, OTHER_VIN, None,
            _lookup_ok(vin=CLAIM_VIN, fleet_id=FLEET_A),
        )
        assert not scope.ok
        assert scope.status == vs.STATUS_DENY

    def test_fleet_vehicle_without_vin_of_record_is_refused(self):
        """The header VIN can't be checked, so it is not used."""
        scope = vs.resolve(
            FLEET_CLAIMS_A,
            CLAIM_VID, CLAIM_VIN, None,
            _lookup_ok(vin="", fleet_id=FLEET_A),
        )
        assert not scope.ok
        assert scope.vin is None

    def test_fleet_vehicle_without_vin_of_record_and_no_header_vin_is_refused(self):
        scope = vs.resolve(
            FLEET_CLAIMS_A,
            CLAIM_VID, None, None,
            _lookup_ok(vin="", fleet_id=FLEET_A),
        )
        assert not scope.ok

    def test_fleet_session_vin_is_the_vin_of_record(self):
        scope = vs.resolve(
            FLEET_CLAIMS_A,
            CLAIM_VID, None, None,
            _lookup_ok(vin=CLAIM_VIN, fleet_id=FLEET_A),
        )
        assert scope.ok and scope.vin == CLAIM_VIN and scope.vin_pinned

    def test_vin_case_insensitive_match(self):
        scope = vs.resolve(
            FLEET_CLAIMS_A,
            CLAIM_VID, CLAIM_VIN.lower(), None,
            _lookup_ok(vin=CLAIM_VIN, fleet_id=FLEET_A),
        )
        assert scope.ok

    def test_no_vehicle_id_header_denied(self):
        scope = vs.resolve(
            FLEET_CLAIMS_A, None, CLAIM_VIN, None, _lookup_ok(),
        )
        assert not scope.ok
        assert scope.status == vs.STATUS_DENY

    def test_empty_fleet_ids_claim_denied(self):
        """Mutation (4): empty list passes → this test would FAIL."""
        claims = {**FLEET_CLAIMS_A, "custom:fleetIds": ""}
        scope = vs.resolve(claims, CLAIM_VID, CLAIM_VIN, None, _lookup_ok())
        assert not scope.ok
        assert scope.status == vs.STATUS_DENY

    def test_csv_fleet_ids_parsed(self):
        claims = {**FLEET_CLAIMS_A, "custom:fleetIds": f"{FLEET_A},{FLEET_B}"}
        scope = vs.resolve(
            claims, CLAIM_VID, CLAIM_VIN, None,
            _lookup_ok(vin=CLAIM_VIN, fleet_id=FLEET_B),
        )
        assert scope.ok

    def test_lookup_error_returns_error(self):
        scope = vs.resolve(FLEET_CLAIMS_A, CLAIM_VID, CLAIM_VIN, None, _lookup_error)
        assert not scope.ok
        assert scope.status == vs.STATUS_ERROR

    def test_vehicle_not_found_denied(self):
        scope = vs.resolve(FLEET_CLAIMS_A, CLAIM_VID, CLAIM_VIN, None, _lookup_not_found)
        assert not scope.ok
        assert scope.status == vs.STATUS_DENY

    def test_driver_id_from_header_in_fleet_branch(self):
        scope = vs.resolve(
            FLEET_CLAIMS_A, CLAIM_VID, CLAIM_VIN, "DRV-FLEET-01",
            _lookup_ok(vin=CLAIM_VIN, fleet_id=FLEET_A),
        )
        assert scope.driver_id == "DRV-FLEET-01"


# ==================================================================
# Neither-claim caller
# ==================================================================

class TestNeitherClaim:

    def test_unchanged_when_no_vehicle_claims(self):
        scope = vs.resolve(NO_CLAIMS, CLAIM_VID, CLAIM_VIN, "DRV-X", _lookup_error)
        assert scope.ok
        assert scope.status == vs.STATUS_UNCHANGED

    def test_header_values_pass_through(self):
        scope = vs.resolve(NO_CLAIMS, CLAIM_VID, CLAIM_VIN, None, _lookup_not_found)
        assert scope.vehicle_id == CLAIM_VID
        assert scope.vin == CLAIM_VIN

    def test_vin_not_pinned(self):
        scope = vs.resolve(NO_CLAIMS, CLAIM_VID, CLAIM_VIN, None, _lookup_not_found)
        assert scope.vin_pinned is False

    def test_no_lookup_in_unchanged_branch(self):
        called: list[str] = []

        def fn(vid: str):
            called.append(vid)
            return None

        vs.resolve(NO_CLAIMS, CLAIM_VID, CLAIM_VIN, None, fn)
        assert called == [], "lookup must not be called when there are no vehicle claims"


# ==================================================================
# BidiVehicleScope.ok property
# ==================================================================

class TestOkProperty:
    def test_pass_is_ok(self):
        assert vs.BidiVehicleScope(vs.STATUS_PASS, None, None, None).ok

    def test_unchanged_is_ok(self):
        assert vs.BidiVehicleScope(vs.STATUS_UNCHANGED, None, None, None).ok

    def test_deny_not_ok(self):
        assert not vs.BidiVehicleScope(vs.STATUS_DENY, None, None, None).ok

    def test_error_not_ok(self):
        assert not vs.BidiVehicleScope(vs.STATUS_ERROR, None, None, None).ok


@pytest.mark.parametrize("raw", [12345, 3.5, True, {"a": 1}])
def test_non_string_fleet_claim_does_not_raise(raw):
    """resolve() is documented never to raise; bidi calls it with no try."""
    scope = vs.resolve({"sub": "x", "custom:fleetIds": raw}, CLAIM_VID, CLAIM_VIN, None,
                       _lookup_ok(vin=CLAIM_VIN, fleet_id=FLEET_A))
    assert not scope.ok
