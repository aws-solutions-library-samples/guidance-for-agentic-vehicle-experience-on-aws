"""Tests for scripts/smoke-test-zone2-journey.py assertion helpers.

Covers:
  - assert_stages_in_order   (stage ordering and unknown-stage detection)
  - assert_all_stages_present (missing-stage detection)
  - assert_elapsed_within_budget (elapsed-time budget)
  - assert_catalog_shape
  - assert_reservation_response
  - assert_order_response_shape

Zero AWS calls.  All tests are deterministic and offline.

Style: matches scripts/tests/test_validate_acquire_catalog.py and
scripts/tests/test_seed_scripts_no_fake_connected.py (pytest, from __future__
import annotations, imports by sys.path manipulation, descriptive names).
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Import the smoke script module by file path (hyphens in filename prevent
# a normal package import — same pattern as test_seed_acquire_catalog.py).
# ---------------------------------------------------------------------------

_SCRIPT_PATH = Path(__file__).parent.parent / "smoke-test-zone2-journey.py"
_SPEC = importlib.util.spec_from_file_location("smoke_test_zone2_journey", _SCRIPT_PATH)
_MOD = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MOD)

# Re-export names under their original names for readability in tests.
assert_stages_in_order = _MOD.assert_stages_in_order
assert_all_stages_present = _MOD.assert_all_stages_present
assert_elapsed_within_budget = _MOD.assert_elapsed_within_budget
assert_catalog_shape = _MOD.assert_catalog_shape
assert_reservation_response = _MOD.assert_reservation_response
assert_order_response_shape = _MOD.assert_order_response_shape
STAGES = _MOD.STAGES
TERMINAL_STAGE = _MOD.TERMINAL_STAGE
run_self_check = _MOD.run_self_check


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _all_stage_entries() -> list[dict]:
    return [{"stage": s, "enteredAt": "2026-11-01T10:00:00+00:00"} for s in STAGES]


def _good_catalog() -> dict:
    return {
        "tenantId": "meridian",
        "categories": [],
        "models": [],
        "variants": [],
        "colors": [],
        "accessories": [],
        "interiors": [],
        "ivePackages": [],
    }


def _good_reservation() -> dict:
    return {
        "orderId": "ORD-20261101-abc123",
        "orderNumber": "ORD-20261101-abc123",
        "stage": "orderPlaced",
        "nextTransitionAt": "2026-11-01T10:00:10+00:00",
    }


def _good_order() -> dict:
    return {
        "orderId": "ORD-20261101-abc123",
        "tenantId": "meridian",
        "currentStage": "orderPlaced",
        "stages": [{"stage": "orderPlaced", "enteredAt": "2026-11-01T10:00:00+00:00"}],
        "modelId": "windrose",
    }


# ---------------------------------------------------------------------------
# assert_stages_in_order
# ---------------------------------------------------------------------------

class TestAssertStagesInOrder:
    def test_full_canonical_sequence_passes(self):
        found = assert_stages_in_order(_all_stage_entries())
        assert found == STAGES

    def test_partial_sequence_in_order_passes(self):
        """First 5 stages in canonical order should pass."""
        entries = _all_stage_entries()[:5]
        found = assert_stages_in_order(entries)
        assert found == STAGES[:5]

    def test_single_stage_passes(self):
        found = assert_stages_in_order([{"stage": "orderPlaced", "enteredAt": "x"}])
        assert found == ["orderPlaced"]

    def test_empty_list_raises(self):
        with pytest.raises(AssertionError, match="empty"):
            assert_stages_in_order([])

    def test_reversed_order_raises(self):
        reversed_entries = list(reversed(_all_stage_entries()))
        with pytest.raises(AssertionError, match="[Oo]rder"):
            assert_stages_in_order(reversed_entries)

    def test_out_of_order_middle_raises(self):
        """Swap stages 3 and 4 — should detect the ordering violation."""
        entries = _all_stage_entries()
        entries[3], entries[4] = entries[4], entries[3]
        with pytest.raises(AssertionError, match="[Oo]rder"):
            assert_stages_in_order(entries)

    def test_duplicate_stage_raises(self):
        """Repeated stage violates the non-decreasing index invariant."""
        entries = _all_stage_entries()[:3] + [_all_stage_entries()[2]] + _all_stage_entries()[3:]
        with pytest.raises(AssertionError, match="[Oo]rder"):
            assert_stages_in_order(entries)

    def test_unknown_stage_raises(self):
        entries = [{"stage": "someInventedStage", "enteredAt": "x"}]
        with pytest.raises(AssertionError, match="[Uu]nknown"):
            assert_stages_in_order(entries)

    def test_mixed_known_and_unknown_stage_raises(self):
        entries = _all_stage_entries()[:3] + [{"stage": "ghostStage", "enteredAt": "x"}]
        with pytest.raises(AssertionError, match="[Uu]nknown"):
            assert_stages_in_order(entries)

    def test_missing_stage_key_raises(self):
        entries = [{"enteredAt": "x"}]  # no 'stage' key
        with pytest.raises(AssertionError):
            assert_stages_in_order(entries)

    def test_terminal_stage_is_last(self):
        found = assert_stages_in_order(_all_stage_entries())
        assert found[-1] == TERMINAL_STAGE

    def test_returns_list_of_strings(self):
        found = assert_stages_in_order(_all_stage_entries()[:3])
        assert isinstance(found, list)
        assert all(isinstance(s, str) for s in found)


# ---------------------------------------------------------------------------
# assert_all_stages_present
# ---------------------------------------------------------------------------

class TestAssertAllStagesPresent:
    def test_all_stages_passes(self):
        assert_all_stages_present(list(STAGES))  # should not raise

    def test_missing_last_stage_raises(self):
        with pytest.raises(AssertionError, match="readyForDelivery"):
            assert_all_stages_present(list(STAGES[:-1]))

    def test_missing_first_stage_raises(self):
        with pytest.raises(AssertionError, match="orderPlaced"):
            assert_all_stages_present(list(STAGES[1:]))

    def test_missing_multiple_stages_raises(self):
        with pytest.raises(AssertionError, match="[Mm]issing"):
            assert_all_stages_present(list(STAGES[:3]))

    def test_empty_list_raises(self):
        with pytest.raises(AssertionError):
            assert_all_stages_present([])

    def test_error_message_names_missing_stages(self):
        """The error message must identify which stages are absent."""
        try:
            assert_all_stages_present(["orderPlaced"])
            pytest.fail("Should have raised")
        except AssertionError as e:
            msg = str(e)
            # Each stage after orderPlaced should be mentioned
            assert "paymentReceived" in msg


# ---------------------------------------------------------------------------
# assert_elapsed_within_budget
# ---------------------------------------------------------------------------

class TestAssertElapsedWithinBudget:
    def test_well_within_budget_passes(self):
        assert_elapsed_within_budget(10.0, 180.0)

    def test_at_exact_budget_passes(self):
        assert_elapsed_within_budget(180.0, 180.0)

    def test_just_over_budget_raises(self):
        with pytest.raises(AssertionError, match="[Bb]udget|exceeds"):
            assert_elapsed_within_budget(180.1, 180.0)

    def test_far_over_budget_raises(self):
        with pytest.raises(AssertionError):
            assert_elapsed_within_budget(9999.0, 180.0)

    def test_zero_elapsed_passes(self):
        assert_elapsed_within_budget(0.0, 180.0)

    def test_error_message_shows_elapsed_and_budget(self):
        try:
            assert_elapsed_within_budget(200.0, 100.0)
            pytest.fail("Should have raised")
        except AssertionError as e:
            msg = str(e)
            # Both values should appear for diagnostics
            assert "200" in msg or "200.0" in msg
            assert "100" in msg or "100.0" in msg


# ---------------------------------------------------------------------------
# assert_catalog_shape
# ---------------------------------------------------------------------------

class TestAssertCatalogShape:
    def test_good_catalog_passes(self):
        assert_catalog_shape(_good_catalog())

    def test_all_required_keys_present_with_data_passes(self):
        catalog = _good_catalog()
        catalog["models"] = [{"modelId": "windrose"}]
        catalog["ivePackages"] = [{"packageId": "IVE-01"}]
        assert_catalog_shape(catalog)

    def test_missing_interiors_raises(self):
        catalog = _good_catalog()
        del catalog["interiors"]
        with pytest.raises(AssertionError, match="interiors"):
            assert_catalog_shape(catalog)

    def test_missing_ive_packages_raises(self):
        catalog = _good_catalog()
        del catalog["ivePackages"]
        with pytest.raises(AssertionError, match="ivePackages"):
            assert_catalog_shape(catalog)

    def test_non_list_value_raises(self):
        catalog = _good_catalog()
        catalog["models"] = "not-a-list"
        with pytest.raises(AssertionError, match="models"):
            assert_catalog_shape(catalog)

    def test_missing_multiple_keys_raises(self):
        with pytest.raises(AssertionError):
            assert_catalog_shape({})

    def test_empty_arrays_are_valid(self):
        """decisions.md Q4: ivePackages and interiors must be present even empty."""
        catalog = _good_catalog()
        assert catalog["ivePackages"] == []
        assert catalog["interiors"] == []
        assert_catalog_shape(catalog)  # must not raise


# ---------------------------------------------------------------------------
# assert_reservation_response
# ---------------------------------------------------------------------------

class TestAssertReservationResponse:
    def test_good_reservation_passes(self):
        assert_reservation_response(_good_reservation())

    def test_missing_order_id_raises(self):
        resp = _good_reservation()
        del resp["orderId"]
        with pytest.raises(AssertionError, match="orderId"):
            assert_reservation_response(resp)

    def test_missing_order_number_raises(self):
        """decisions.md Q3: orderNumber is non-optional in Swift."""
        resp = _good_reservation()
        del resp["orderNumber"]
        with pytest.raises(AssertionError, match="orderNumber"):
            assert_reservation_response(resp)

    def test_missing_stage_raises(self):
        resp = _good_reservation()
        del resp["stage"]
        with pytest.raises(AssertionError, match="stage"):
            assert_reservation_response(resp)

    def test_wrong_initial_stage_raises(self):
        resp = _good_reservation()
        resp["stage"] = "paymentReceived"
        with pytest.raises(AssertionError, match="orderPlaced"):
            assert_reservation_response(resp)

    def test_missing_next_transition_raises(self):
        resp = _good_reservation()
        del resp["nextTransitionAt"]
        with pytest.raises(AssertionError, match="nextTransitionAt"):
            assert_reservation_response(resp)


# ---------------------------------------------------------------------------
# assert_order_response_shape
# ---------------------------------------------------------------------------

class TestAssertOrderResponseShape:
    def test_good_order_passes(self):
        assert_order_response_shape(_good_order())

    def test_missing_current_stage_raises(self):
        """decisions.md Q5: DDB 'stage' must be renamed to 'currentStage' on the wire."""
        order = _good_order()
        del order["currentStage"]
        with pytest.raises(AssertionError, match="currentStage"):
            assert_order_response_shape(order)

    def test_missing_stages_array_raises(self):
        """decisions.md Q5: DDB 'stageHistory' must be renamed to 'stages' on the wire."""
        order = _good_order()
        del order["stages"]
        with pytest.raises(AssertionError, match="stages"):
            assert_order_response_shape(order)

    def test_leaked_ddb_stage_field_raises(self):
        """DDB 'stage' must be stripped; 'currentStage' is the wire name."""
        order = _good_order()
        order["stage"] = "orderPlaced"  # leaked internal DDB attribute
        with pytest.raises(AssertionError, match="stage"):
            assert_order_response_shape(order)

    def test_leaked_stage_history_field_raises(self):
        order = _good_order()
        order["stageHistory"] = []  # leaked internal DDB attribute
        with pytest.raises(AssertionError, match="stageHistory"):
            assert_order_response_shape(order)

    def test_leaked_configuration_raises(self):
        order = _good_order()
        order["configuration"] = {"modelId": "windrose"}
        with pytest.raises(AssertionError, match="configuration"):
            assert_order_response_shape(order)

    def test_leaked_owner_sub_raises(self):
        order = _good_order()
        order["ownerSub"] = "sub-abc-123"
        with pytest.raises(AssertionError, match="ownerSub"):
            assert_order_response_shape(order)

    def test_leaked_payment_raises(self):
        order = _good_order()
        order["payment"] = {"status": "captured"}
        with pytest.raises(AssertionError, match="payment"):
            assert_order_response_shape(order)

    def test_leaked_next_transition_raises(self):
        order = _good_order()
        order["nextTransitionAt"] = "2026-11-01T10:00:00+00:00"
        with pytest.raises(AssertionError, match="nextTransitionAt"):
            assert_order_response_shape(order)


# ---------------------------------------------------------------------------
# Offline self-check end-to-end
# ---------------------------------------------------------------------------

class TestRunSelfCheck:
    def test_self_check_exits_zero(self, capsys):
        """run_self_check() must return 0 (all assertion helpers pass)."""
        rc = run_self_check()
        assert rc == 0, "run_self_check() returned non-zero — an assertion helper failed"

    def test_self_check_reports_passed(self, capsys):
        run_self_check()
        out = capsys.readouterr().out
        assert "PASSED" in out


# ---------------------------------------------------------------------------
# Constants sanity
# ---------------------------------------------------------------------------

class TestConstants:
    def test_stages_list_has_ten_entries(self):
        assert len(STAGES) == 10

    def test_terminal_stage_is_last(self):
        assert TERMINAL_STAGE == STAGES[-1]

    def test_terminal_stage_value(self):
        assert TERMINAL_STAGE == "readyForDelivery"

    def test_stages_are_unique(self):
        assert len(STAGES) == len(set(STAGES))

    def test_order_placed_is_first(self):
        assert STAGES[0] == "orderPlaced"


# ---------------------------------------------------------------------------
# Drift guard: the smoke script restates the canonical stage sequence rather
# than importing it (the script must stay runnable standalone from an operator
# laptop with no lambdas/ on sys.path).  That duplication is only safe if a
# test fails the moment the two lists diverge.  order_progressor is the source
# of truth — it is the code that actually advances the stage machine.
# ---------------------------------------------------------------------------

class TestCanonicalStageParity:
    @staticmethod
    def _load_progressor():
        progressor_path = (
            Path(__file__).parent.parent.parent
            / "lambdas" / "acquire" / "order_progressor" / "handler.py"
        )
        spec = importlib.util.spec_from_file_location(
            "_order_progressor_for_parity", progressor_path
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def test_stages_match_order_progressor(self):
        progressor = self._load_progressor()
        assert STAGES == progressor.STAGES, (
            "smoke-test-zone2-journey.py STAGES has drifted from "
            "lambdas/acquire/order_progressor/handler.py STAGES. "
            "order_progressor is the source of truth — update the smoke "
            "script to match, do not weaken this test."
        )

    def test_terminal_stage_matches_order_progressor(self):
        progressor = self._load_progressor()
        assert TERMINAL_STAGE == progressor.TERMINAL_STAGE
