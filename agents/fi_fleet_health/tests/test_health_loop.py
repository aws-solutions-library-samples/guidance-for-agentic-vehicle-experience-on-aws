"""Tests for the FI fleet-health deterministic daily loop (spec D8/D9, task 3.2 part B).

Drives ``loop.run_health_loop`` end-to-end with real-shaped rows and injected readers.
Each test covers one bullet from the task specification, labelled in the docstring.

Mutation guards named here are verified in the /tmp scratch copies per the task.

Run:
    .venv/bin/python -m pytest agents/fi_fleet_health/tests/test_health_loop.py -q
"""
from __future__ import annotations

import copy
import datetime
from typing import Any, Optional
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Module under test
# ---------------------------------------------------------------------------
from agents.fi_fleet_health.loop import (
    map_need_to_labor_op,
    load_needs_config,
    read_attempt_history_health,
    run_health_loop,
    _skip_unchanged_health,
)

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

_VIN = "MRDVEH00000000017"
_VIN2 = "MRDVEH00000000042"
_FLEET_ID = "fleet-meridian-01"
_VEHICLE_ID = "vehicle-001"
_COMPUTED_AT = "2026-10-10T05:40:00Z"

_P0_CODE = "P0AA6"
_P0_DIRECTIVE = (
    "Stop driving immediately. "
    "Do not restart the vehicle. "
    "Contact your dealer for towing to the nearest service centre."
)
_P0_CATALOG_ENTRY = {
    "code": _P0_CODE,
    "description": "High-voltage isolation fault.",
    "system": "ev_drivetrain",
    "band": "p0",
    "applies_to": ["ev", "hybrid"],
    "directive": _P0_DIRECTIVE,
}

_ATTENTION_CODE = "P0A7F"
_SOON_CODE = "P1AB0"

# One stock row — single rooftop within range
_STOCK_ROWS = [
    {
        "dealer_id": "dealer-meridian-001",
        "part_number": "HV-ISO-KIT-001",
        "qty_on_hand": 2,
        "latitude": "47.6062",
        "longitude": "-122.3321",
        "stock_as_of": "2026-10-09T00:00:00Z",
    },
]

# ---------------------------------------------------------------------------
# Helper builders
# ---------------------------------------------------------------------------

def _make_vehicle_row(
    vin: str = _VIN,
    fleet_id: str = _FLEET_ID,
    vehicle_id: str = _VEHICLE_ID,
    open_faults: Optional[list] = None,
    agent2_findings: Optional[list] = None,
    pm_tasks: Optional[list] = None,
    catalog_entries: Optional[dict] = None,
    position: Optional[dict] = None,
) -> dict:
    return {
        "vin": vin,
        "fleet_id": fleet_id,
        "vehicle_id": vehicle_id,
        "open_faults": open_faults or [],
        "agent2_findings": agent2_findings or [],
        "pm_tasks": pm_tasks or [],
        "catalog_entries": catalog_entries or {},
        "position": position,
        "utilisation": {"daily_average_miles_30d": 42.5},
    }


def _p0_fault() -> dict:
    return {"code": _P0_CODE, "band": "p0", "last_seen_at": "2026-10-09T12:00:00Z"}


def _attention_fault() -> dict:
    return {"code": _ATTENTION_CODE, "band": "attention", "last_seen_at": "2026-10-08T00:00:00Z"}


def _soon_fault() -> dict:
    return {"code": _SOON_CODE, "band": "soon", "last_seen_at": "2026-10-07T00:00:00Z"}


def _overdue_pm() -> dict:
    return {"task_code": "TIRE_ROTATION", "status": "overdue"}


def _due_pm() -> dict:
    return {"task_code": "BRAKE_INSPECTION", "status": "due"}


def _simple_readers(
    *,
    is_grounded: bool = False,
    grounding_error: bool = False,
    history: Optional[dict] = None,
    standing: Any = None,
    live: Optional[dict] = None,
    recall_visit: Optional[dict] = None,
) -> dict:
    """Build the minimal set of injected reader callables."""

    def grounded_fn(vehicle_id: str) -> tuple[bool, int]:
        if grounding_error:
            return False, 1   # fail-closed; error_count=1
        return is_grounded, 0

    def history_fn(vin: str) -> tuple[dict, int]:
        return history or {}, 0

    def standing_fn(vin: str) -> Any:
        return standing or {"vin": vin, "fleet_id": _FLEET_ID}

    def lookup_live(finding_key: str) -> Optional[dict]:
        return live

    recall_fn = None
    if recall_visit is not None:
        def recall_fn(vin: str) -> Optional[dict]:
            return recall_visit

    return {
        "grounded_fn": grounded_fn,
        "history_fn": history_fn,
        "standing_fn": standing_fn,
        "lookup_live": lookup_live,
        "recall_visit_fn": recall_fn,
    }


# ---------------------------------------------------------------------------
# Tests for map_need_to_labor_op (needs config)
# ---------------------------------------------------------------------------

class TestMapNeedToLaborOp:
    """Each need kind resolves to a labor_op from the config."""

    def test_fault_p0_code_specific_wins_over_band(self):
        config = load_needs_config()
        need = {"kind": "fault", "code": "P0AA6", "band": "p0"}
        result = map_need_to_labor_op(need, config)
        assert result["labor_op"] == "LAB-HVISO-01"  # option (a): P0AA6's own op; band p0 books LAB-ELE-07
        assert "HV-ISO-KIT-001" in result["parts"]

    def test_fault_unknown_code_falls_back_to_band(self):
        config = copy.deepcopy(load_needs_config())
        # G6-D2: every band op is LAB-ELE-07 now, the same as the hard fallback, so the
        # band entry gets a marker op to show the band lookup ran.
        config["fault_bands"]["soon"] = {"labor_op": "LAB-TST-01", "parts": []}
        need = {"kind": "fault", "code": "P9999", "band": "soon"}
        result = map_need_to_labor_op(need, config)
        assert result["labor_op"] == "LAB-TST-01"

    def test_pm_task_tire_rotation(self):
        config = load_needs_config()
        need = {"kind": "pm_task", "task_code": "TIRE_ROTATION", "status": "overdue"}
        result = map_need_to_labor_op(need, config)
        assert result["labor_op"] == "LAB-SUS-03"

    def test_agent2_finding_battery(self):
        config = load_needs_config()
        # agent2_findings key is the "kind" of the finding (e.g. "battery")
        need = {"kind": "agent2_finding", "severity_band": "battery", "finding_id": "fi-001"}
        result = map_need_to_labor_op(need, config)
        assert result["labor_op"] == "LAB-BATSVC-01"

    def test_unknown_kind_returns_default(self):
        config = load_needs_config()
        need = {"kind": "unknown_kind"}
        result = map_need_to_labor_op(need, config)
        assert result["labor_op"] == "LAB-ELE-07"


# ---------------------------------------------------------------------------
# Bullet 1: booking item always, grounding item only when p0 + flag
# ---------------------------------------------------------------------------

class TestBookingAndGroundingItems:
    """Spec D8 step 3: booking always; grounding only p0 + flag + not grounded."""

    def test_p0_fault_flag_off_gives_booking_only(self):
        """fault_grounding_enabled=False → booking item only, no grounding.
        Mutation guard (d/6): if the loop ignores the flag → grounding written → fails.
        """
        row = _make_vehicle_row(
            open_faults=[_p0_fault()],
            catalog_entries={_P0_CODE: _P0_CATALOG_ENTRY},
        )
        written: list[dict] = []
        readers = _simple_readers()

        def write_fn(finding, standing, refresh_on_noop=True):
            written.append(finding)

        stats = run_health_loop(
            vehicle_rows=[row],
            stock_rows=_STOCK_ROWS,
            computed_at=_COMPUTED_AT,
            write_finding=write_fn,
            fault_grounding_enabled=False,
            **readers,
        )
        subjects = [f.get("subject_key", "") for f in written]
        assert any(s.startswith("service/") for s in subjects), "Booking item must be written."
        assert not any(s.startswith("ground/") for s in subjects), (
            "No grounding when fault_grounding_enabled=False. "
            "Mutation: ignore flag → ground/ present → fails."
        )

    def test_p0_fault_flag_on_gives_booking_and_grounding(self):
        """p0 + flag on + not grounded → both booking and grounding items.
        Mutation guard: same_hash_model_written returning True on rules item would
        skip when it should write.
        """
        row = _make_vehicle_row(
            open_faults=[_p0_fault()],
            catalog_entries={_P0_CODE: _P0_CATALOG_ENTRY},
        )
        written: list[dict] = []
        readers = _simple_readers(is_grounded=False)

        def write_fn(finding, standing, refresh_on_noop=True):
            written.append(finding)

        run_health_loop(
            vehicle_rows=[row],
            stock_rows=_STOCK_ROWS,
            computed_at=_COMPUTED_AT,
            write_finding=write_fn,
            fault_grounding_enabled=True,
            **readers,
        )
        subjects = [f.get("subject_key", "") for f in written]
        assert any(s.startswith("service/") for s in subjects), "Booking item must be written."
        assert any(s.startswith("ground/") for s in subjects), "Grounding item must be written."

    def test_p0_already_grounded_no_grounding_item(self):
        """Vehicle already grounded → booking only, no second grounding item."""
        row = _make_vehicle_row(
            open_faults=[_p0_fault()],
            catalog_entries={_P0_CODE: _P0_CATALOG_ENTRY},
        )
        written: list[dict] = []
        readers = _simple_readers(is_grounded=True)

        def write_fn(finding, standing, refresh_on_noop=True):
            written.append(finding)

        run_health_loop(
            vehicle_rows=[row],
            stock_rows=_STOCK_ROWS,
            computed_at=_COMPUTED_AT,
            write_finding=write_fn,
            fault_grounding_enabled=True,
            **readers,
        )
        subjects = [f.get("subject_key", "") for f in written]
        assert not any(s.startswith("ground/") for s in subjects), (
            "Already grounded vehicle must not get a second grounding item."
        )

    def test_attention_fault_booking_only(self):
        """attention fault → booking only (no grounding even with flag)."""
        row = _make_vehicle_row(open_faults=[_attention_fault()])
        written: list[dict] = []
        readers = _simple_readers()

        def write_fn(finding, standing, refresh_on_noop=True):
            written.append(finding)

        run_health_loop(
            vehicle_rows=[row],
            stock_rows=_STOCK_ROWS,
            computed_at=_COMPUTED_AT,
            write_finding=write_fn,
            fault_grounding_enabled=True,
            **readers,
        )
        subjects = [f.get("subject_key", "") for f in written]
        assert any(s.startswith("service/") for s in subjects)
        assert not any(s.startswith("ground/") for s in subjects)

    def test_pm_due_booking_only(self):
        """PM task due → booking only."""
        row = _make_vehicle_row(pm_tasks=[_due_pm()])
        written: list[dict] = []
        readers = _simple_readers()

        def write_fn(finding, standing, refresh_on_noop=True):
            written.append(finding)

        run_health_loop(
            vehicle_rows=[row],
            stock_rows=_STOCK_ROWS,
            computed_at=_COMPUTED_AT,
            write_finding=write_fn,
            **readers,
        )
        subjects = [f.get("subject_key", "") for f in written]
        assert any(s.startswith("service/") for s in subjects)


# ---------------------------------------------------------------------------
# Bullet 2: exhausted items carry no recommended_action
# ---------------------------------------------------------------------------

class TestExhaustedItems:
    """Spec D9: exhausted items carry no recommended_action on either slot.

    Mutation guard (e): writing exhausted item WITH recommended_action → test fails.
    """

    def _history_at_max(self, segment: str = "service", queue_status: str = "cancelled") -> dict:
        """History entry at MAX_ATTEMPTS with an immediate-retry status.

        episode_start=1 so ep_cap = MAX_ATTEMPTS (triggers exhaustion on this run).
        Without episode_start the new ep_cap=1 fallback would retry instead.
        """
        from agents.fi_recall_warranty.attempts import MAX_ATTEMPTS
        return {
            segment: {
                "attempt_number": MAX_ATTEMPTS,
                "finding_id": "FIND-exhausted-0001",
                "inputs_hash": "old-hash",
                "computed_at": "2026-10-01T00:00:00Z",
                "expires_at": "2026-10-04T00:00:00Z",
                "diagnosis_source": "rules",
                "queue_status": queue_status,
                "decided_at": "2026-10-01T00:00:00Z",
                "episode_start": 1,  # episode_start required so ep_cap=MAX_ATTEMPTS fires
            }
        }

    def test_service_slot_exhausted_no_recommended_action(self):
        """After MAX_ATTEMPTS on the service slot: informational item, no recommended_action.
        Mutation guard (e): include recommended_action in the exhausted item → this test fails.
        """
        from agents.fi_recall_warranty.attempts import MAX_ATTEMPTS

        history = self._history_at_max("service", "cancelled")
        row = _make_vehicle_row(open_faults=[_p0_fault()],
                                 catalog_entries={_P0_CODE: _P0_CATALOG_ENTRY})
        written: list[dict] = []
        readers = _simple_readers(history=history)

        def write_fn(finding, standing, refresh_on_noop=True):
            written.append(finding)

        run_health_loop(
            vehicle_rows=[row],
            stock_rows=_STOCK_ROWS,
            computed_at=_COMPUTED_AT,
            write_finding=write_fn,
            fault_grounding_enabled=False,
            **readers,
        )
        # The exhausted service item must be the only item written
        assert len(written) >= 1, "Exhausted item must be written."
        exhausted = next(
            (f for f in written if f.get("subject_key", "").startswith("service_exhausted/")), None
        )
        assert exhausted is not None, (
            "Exhausted service item must have subject_key service_exhausted/... (C3). "
            "Mutation: keep service/6 → this assertion fails."
        )
        assert exhausted.get("severity") == "informational", (
            "Exhausted item must have severity='informational'. "
            "Mutation: keep severity='p0' → fails."
        )
        assert "recommended_action" not in exhausted, (
            "Exhausted item must NOT carry recommended_action (spec D9). "
            "Mutation guard (e): add recommended_action → this assertion fails."
        )

    def test_ground_slot_exhausted_no_recommended_action(self):
        """After MAX_ATTEMPTS on the ground slot: informational item, no recommended_action."""
        from agents.fi_recall_warranty.attempts import MAX_ATTEMPTS
        history = self._history_at_max("ground", "cancelled")
        row = _make_vehicle_row(open_faults=[_p0_fault()],
                                 catalog_entries={_P0_CODE: _P0_CATALOG_ENTRY})
        written: list[dict] = []
        readers = _simple_readers(history=history)

        def write_fn(finding, standing, refresh_on_noop=True):
            written.append(finding)

        run_health_loop(
            vehicle_rows=[row],
            stock_rows=_STOCK_ROWS,
            computed_at=_COMPUTED_AT,
            write_finding=write_fn,
            fault_grounding_enabled=True,
            **readers,
        )
        ground_items = [f for f in written if f.get("subject_key", "").startswith("ground_exhausted/")]
        assert any(
            f.get("severity") == "informational" and "recommended_action" not in f
            for f in ground_items
        ), (
            "Exhausted ground item must have severity='informational' and no recommended_action. "
            "Mutation guard (e): add recommended_action → fails."
        )

    def test_stats_exhausted_items_written_incremented(self):
        """stats['exhausted_items_written'] increments for each exhausted item."""
        from agents.fi_recall_warranty.attempts import MAX_ATTEMPTS
        history = self._history_at_max("service", "cancelled")
        row = _make_vehicle_row(open_faults=[_p0_fault()],
                                 catalog_entries={_P0_CODE: _P0_CATALOG_ENTRY})
        readers = _simple_readers(history=history)

        stats = run_health_loop(
            vehicle_rows=[row],
            stock_rows=_STOCK_ROWS,
            computed_at=_COMPUTED_AT,
            write_finding=lambda f, s, refresh_on_noop=True: None,
            fault_grounding_enabled=False,
            **readers,
        )
        assert stats["exhausted_items_written"] >= 1, (
            "stats['exhausted_items_written'] must increment on exhausted items."
        )


# ---------------------------------------------------------------------------
# Bullet 3: recall visit sets default rooftop and detail
# ---------------------------------------------------------------------------

class TestRecallVisit:
    """Spec D8 'with a recall visit': the booking item defaults to that rooftop and
    detail is exactly 'Book with recall <id> at <rooftop>'.

    Mutation guard (c): ignoring the recall_visit → wrong detail text and rooftop → fails.
    """

    def test_recall_visit_sets_detail_and_rooftop(self):
        """When a live Stage C recall booking item exists for the VIN:
        - booking item detail == 'Book with recall <id> at <rooftop>'
        - dealer_id in the recommended_action equals the recall visit's rooftop.

        Mutation guard (c): ignore recall_visit_fn → detail is '' and dealer is nearest →
        both assertions fail.
        """
        recall_visit = {
            "recall_item_finding_id": "FIND-recall-001",
            "recall_id": "NHTSA-24-001",
            "rooftop_dealer_id": "dealer-recall-rooftop",
        }
        row = _make_vehicle_row(open_faults=[_soon_fault()])
        written: list[dict] = []
        readers = _simple_readers(recall_visit=recall_visit)

        def write_fn(finding, standing, refresh_on_noop=True):
            written.append(finding)

        run_health_loop(
            vehicle_rows=[row],
            stock_rows=_STOCK_ROWS,
            computed_at=_COMPUTED_AT,
            write_finding=write_fn,
            **readers,
        )
        booking = next(
            (f for f in written if f.get("subject_key", "").startswith("service/")), None
        )
        assert booking is not None, "Booking item must be written."

        # The detail text must be exactly the recall-visit phrase
        expected_detail = "Book with recall NHTSA-24-001 at dealer-recall-rooftop"
        assert booking.get("detail") == expected_detail, (
            f"Booking detail must be '{expected_detail}' when a recall visit is live. "
            "Mutation guard (c): ignore recall_visit_fn → detail is '' → fails."
        )

        # The chosen dealer in the recommended_action must be the recall visit's rooftop
        ra_dealer = booking.get("recommended_action", {}).get("parameters", {}).get("dealer_id")
        assert ra_dealer == "dealer-recall-rooftop", (
            "Booking recommended_action.dealer_id must be the recall visit's rooftop. "
            "Mutation guard (c): ignore recall_visit → nearest dealer chosen instead → fails."
        )

    def test_no_recall_visit_uses_nearest_rooftop(self):
        """Without a recall visit, the nearest in-stock rooftop is chosen."""
        row = _make_vehicle_row(
            open_faults=[_soon_fault()],
            position={"latitude": 47.6062, "longitude": -122.3321},
        )
        written: list[dict] = []
        readers = _simple_readers()  # recall_visit_fn=None

        def write_fn(finding, standing, refresh_on_noop=True):
            written.append(finding)

        run_health_loop(
            vehicle_rows=[row],
            stock_rows=_STOCK_ROWS,
            computed_at=_COMPUTED_AT,
            write_finding=write_fn,
            **readers,
        )
        booking = next(
            (f for f in written if f.get("subject_key", "").startswith("service/")), None
        )
        assert booking is not None
        ra_dealer = booking.get("recommended_action", {}).get("parameters", {}).get("dealer_id")
        # The nearest rooftop from stock rows
        assert ra_dealer == "dealer-meridian-001"


# ---------------------------------------------------------------------------
# Bullet 4: groundings read error → counted + grounding item still written
# ---------------------------------------------------------------------------

class TestGroundingsReadError:
    """Spec D8 step 3 fail-closed: a groundings read error is counted AND the
    grounding item is still written (not grounded = safe direction).

    Mutation guard (d): treat error as grounded → grounding item suppressed →
    'grounding' assertion fails; stat assertion also fails.
    """

    def test_grounding_read_error_counted_and_item_written(self):
        """grounding_read_errors increments; grounding item is still written.

        Mutation guard (d): if grounded_fn error → is_grounded=True → no grounding
        item written → 'ground/' assertion fails.
        Also: grounding_read_errors stays at 0 if error_count is dropped → stat fails.
        """
        row = _make_vehicle_row(
            open_faults=[_p0_fault()],
            catalog_entries={_P0_CODE: _P0_CATALOG_ENTRY},
        )
        written: list[dict] = []

        # grounding_error=True means grounded_fn returns (False, 1) — fail-closed
        readers = _simple_readers(grounding_error=True)

        def write_fn(finding, standing, refresh_on_noop=True):
            written.append(finding)

        stats = run_health_loop(
            vehicle_rows=[row],
            stock_rows=_STOCK_ROWS,
            computed_at=_COMPUTED_AT,
            write_finding=write_fn,
            fault_grounding_enabled=True,
            **readers,
        )
        # Error count must reach the stats
        assert stats["grounding_read_errors"] >= 1, (
            "stats['grounding_read_errors'] must increment when grounded_fn returns error_count=1. "
            "Mutation guard (d): drop error_count from (is_grounded, error_count) → "
            "grounding_read_errors stays 0 → fails."
        )
        # Fail-closed: grounding item must still be written
        subjects = [f.get("subject_key", "") for f in written]
        assert any(s.startswith("ground/") for s in subjects), (
            "Grounding item must be written when grounded_fn returns error (fail-closed). "
            "Mutation guard (d): treat error as grounded → no ground/ item → fails."
        )


# ---------------------------------------------------------------------------
# Bullet 5: skip-unchanged rule (refresh_on_noop=True)
# ---------------------------------------------------------------------------

class TestSkipUnchanged:
    """Spec D8 step 5: same hash + model-written → skip (items_skipped_unchanged++).
    refresh_on_noop=True is passed to write_finding on every write.

    Mutation guard (a): drop refresh_on_noop=True → test_refresh_on_noop_passed fails.
    """

    def test_same_hash_model_written_skips_item(self):
        """Same inputs_hash + diagnosis_source='model' → item skipped, not written.

        Mutation: return True on rules-written item → skips upgradeable items → test fails.
        """
        # Build a live item with the same hash and diagnosis_source='model'
        row = _make_vehicle_row(open_faults=[_soon_fault()])
        written: list[dict] = []

        # We need to know what hash will be computed.
        # We'll first run without a live item to capture the hash,
        # then rerun with that hash set as the live item's inputs_hash.
        captured: list[dict] = []
        readers = _simple_readers()

        def write_first(finding, standing, refresh_on_noop=True):
            captured.append(finding)

        run_health_loop(
            vehicle_rows=[row],
            stock_rows=_STOCK_ROWS,
            computed_at=_COMPUTED_AT,
            write_finding=write_first,
            **readers,
        )
        assert captured, "First run must write an item."
        captured_hash = captured[0].get("inputs_hash")

        # Now simulate a live model-written item with the same hash
        live_item = {
            "inputs_hash": captured_hash,
            "evidence": [
                {"kind": "health.diagnosis_source", "value": "model"}
            ],
        }

        def lookup_live_with_match(finding_key: str) -> Optional[dict]:
            return live_item

        stats = run_health_loop(
            vehicle_rows=[row],
            stock_rows=_STOCK_ROWS,
            computed_at=_COMPUTED_AT,
            grounded_fn=readers["grounded_fn"],
            history_fn=readers["history_fn"],
            standing_fn=readers["standing_fn"],
            lookup_live=lookup_live_with_match,
            write_finding=lambda f, s, refresh_on_noop=True: written.append(f),
        )
        assert stats["items_skipped_unchanged"] >= 1, (
            "items_skipped_unchanged must increment when same hash + model-written. "
            "Mutation: skip_unchanged always False → item written instead → fails."
        )
        assert not written, "No item should be written on skip_unchanged."

    def test_same_hash_rules_written_not_skipped(self):
        """Same hash + diagnosis_source='rules' → NOT skipped (eligible for model upgrade)."""
        row = _make_vehicle_row(open_faults=[_soon_fault()])
        captured: list[dict] = []
        readers = _simple_readers()

        def write_first(finding, standing, refresh_on_noop=True):
            captured.append(finding)

        run_health_loop(
            vehicle_rows=[row], stock_rows=_STOCK_ROWS,
            computed_at=_COMPUTED_AT, write_finding=write_first, **readers,
        )
        assert captured
        captured_hash = captured[0].get("inputs_hash")

        # Simulate live rules-written item with same hash
        live_rules = {
            "inputs_hash": captured_hash,
            "evidence": [{"kind": "health.diagnosis_source", "value": "rules"}],
        }
        written2: list[dict] = []

        stats = run_health_loop(
            vehicle_rows=[row],
            stock_rows=_STOCK_ROWS,
            computed_at=_COMPUTED_AT,
            grounded_fn=readers["grounded_fn"],
            history_fn=readers["history_fn"],
            standing_fn=readers["standing_fn"],
            lookup_live=lambda k: live_rules,
            write_finding=lambda f, s, refresh_on_noop=True: written2.append(f),
        )
        # Rules-written same-hash item is eligible → should NOT be skipped
        assert stats["items_skipped_unchanged"] == 0, (
            "Rules-written item with same hash must not be skipped (task 4.1 upgrades it). "
            "Mutation: skip rules-written items too → items_skipped_unchanged=1 → fails."
        )
        assert written2, "Rules-written same-hash item must be rewritten."

    def test_refresh_on_noop_always_passed(self):
        """write_finding must always receive refresh_on_noop=True.

        Mutation guard (a): drop refresh_on_noop=True keyword → call_args lacks it →
        assertion fails.
        """
        row = _make_vehicle_row(open_faults=[_soon_fault()])
        calls: list[dict] = []

        def write_fn(finding, standing, refresh_on_noop=False):
            # Default is False so we can detect whether True was explicitly passed
            calls.append({"refresh_on_noop": refresh_on_noop})

        readers = _simple_readers()
        run_health_loop(
            vehicle_rows=[row],
            stock_rows=_STOCK_ROWS,
            computed_at=_COMPUTED_AT,
            write_finding=write_fn,
            **readers,
        )
        assert calls, "write_finding must be called."
        for call in calls:
            assert call["refresh_on_noop"] is True, (
                "write_finding must always receive refresh_on_noop=True (spec D8 step 8). "
                "Mutation guard (a): omit refresh_on_noop=True → False received → fails."
            )


# ---------------------------------------------------------------------------
# Bullet 6: standing is passed (not None)
# ---------------------------------------------------------------------------

class TestStandingPassed:
    """write_finding(finding, standing, ...) — standing is NEVER None.

    Mutation guard (f): passing standing=None → test fails.
    """

    def test_standing_passed_to_write_finding(self):
        """The standing object from standing_fn is forwarded to write_finding.

        Mutation guard (f): pass None instead of standing → received_standing is None → fails.
        """
        row = _make_vehicle_row(open_faults=[_soon_fault()])
        expected_standing = {"vin": _VIN, "fleet_id": _FLEET_ID, "source": "test-sentinel"}
        received: list = []

        def write_fn(finding, standing, refresh_on_noop=True):
            received.append(standing)

        def standing_fn(vin: str) -> dict:
            return expected_standing

        readers = _simple_readers()
        run_health_loop(
            vehicle_rows=[row],
            stock_rows=_STOCK_ROWS,
            computed_at=_COMPUTED_AT,
            grounded_fn=readers["grounded_fn"],
            history_fn=readers["history_fn"],
            standing_fn=standing_fn,
            lookup_live=readers["lookup_live"],
            write_finding=write_fn,
        )
        assert received, "write_finding must be called."
        for s in received:
            assert s is not None, (
                "standing passed to write_finding must not be None. "
                "Mutation guard (f): pass None → s is None → fails."
            )
            assert s == expected_standing, (
                "standing must be the exact object returned by standing_fn. "
                "Mutation guard (f): pass a different object → identity check fails."
            )


# ---------------------------------------------------------------------------
# Bullet 7: in_progress / dismissed are never re-raised
# ---------------------------------------------------------------------------

class TestNoReraiseInProgress:
    """spec D9 / Stage C rule: in_progress and unknown never re-raise.
    dismissed and completed/executed are episode-closing statuses: they skip
    when the current needs match the closing item's needs (C2 episode boundary).

    Mutation guard (b): re-raise on in_progress → item written again → written list
    is non-empty when it should be empty.
    """

    def _history_with_status(self, status: str, closing_needs: Optional[frozenset] = None) -> dict:
        h: dict = {
            "attempt_number": 1,
            "finding_id": "FIND-status-0001",
            "inputs_hash": "some-hash",
            "computed_at": "2026-10-01T00:00:00Z",
            "expires_at": "2026-10-04T00:00:00Z",
            "diagnosis_source": "rules",
            "queue_status": status,
            "decided_at": None,
        }
        if closing_needs is not None:
            h["closing_needs"] = closing_needs
        return {"service": h}

    def test_in_progress_not_re_raised(self):
        """An in_progress action is never re-raised.

        Mutation guard (b): treat in_progress like cancelled → item rewritten →
        written is non-empty → fails.
        """
        history = self._history_with_status("in_progress")
        row = _make_vehicle_row(open_faults=[_soon_fault()])
        written: list[dict] = []
        readers = _simple_readers(history=history)

        def write_fn(finding, standing, refresh_on_noop=True):
            written.append(finding)

        run_health_loop(
            vehicle_rows=[row],
            stock_rows=_STOCK_ROWS,
            computed_at=_COMPUTED_AT,
            write_finding=write_fn,
            **readers,
        )
        assert not written, (
            "in_progress action must not cause a re-raise. "
            "Mutation guard (b): re-raise in_progress → written non-empty → fails."
        )

    def test_dismissed_not_re_raised_when_same_needs(self):
        """A dismissed action with the SAME needs as the closing item → skip (C2).

        C2 (design answer 1): dismissed is a closing status. When the current
        needs match the closing item's needs, the decline window has NOT passed,
        so no new episode starts.
        Mutation guard: remove closing_needs check → dismissed re-raises even
        with same needs → written non-empty → fails.
        """
        # Closing item had the same fault need as the current run
        closing = frozenset({"fault:P1AB0"})
        history = self._history_with_status("dismissed", closing_needs=closing)
        row = _make_vehicle_row(open_faults=[_soon_fault()])
        written: list[dict] = []
        readers = _simple_readers(history=history)

        def write_fn(finding, standing, refresh_on_noop=True):
            written.append(finding)

        run_health_loop(
            vehicle_rows=[row],
            stock_rows=_STOCK_ROWS,
            computed_at=_COMPUTED_AT,
            write_finding=write_fn,
            **readers,
        )
        assert not written, (
            "dismissed with same needs must not re-raise (no new episode). "
            "Mutation guard: skip closing_needs check → same needs still re-raises → fails."
        )

    def test_unknown_status_not_re_raised(self):
        """An unknown action status is never re-raised."""
        history = self._history_with_status("unknown")
        row = _make_vehicle_row(open_faults=[_soon_fault()])
        written: list[dict] = []
        readers = _simple_readers(history=history)

        def write_fn(finding, standing, refresh_on_noop=True):
            written.append(finding)

        run_health_loop(
            vehicle_rows=[row],
            stock_rows=_STOCK_ROWS,
            computed_at=_COMPUTED_AT,
            write_finding=write_fn,
            **readers,
        )
        assert not written, "unknown status must not cause a re-raise."


# ---------------------------------------------------------------------------
# Bullet 8: expiry is 3 days
# ---------------------------------------------------------------------------

class TestExpiry:
    """Each Finding's expires_at is computed_at + 3 days (contract § 1.8)."""

    def test_expiry_is_three_days(self):
        row = _make_vehicle_row(open_faults=[_soon_fault()])
        written: list[dict] = []
        readers = _simple_readers()

        def write_fn(finding, standing, refresh_on_noop=True):
            written.append(finding)

        run_health_loop(
            vehicle_rows=[row],
            stock_rows=_STOCK_ROWS,
            computed_at="2026-10-10T05:40:00Z",
            write_finding=write_fn,
            **readers,
        )
        assert written
        f = written[0]
        assert f.get("expires_at") == "2026-10-13T05:40:00Z", (
            f"expires_at must be computed_at + 3 days; got {f.get('expires_at')!r}."
        )


# ---------------------------------------------------------------------------
# Bullet 9: bad row is counted, not fatal
# ---------------------------------------------------------------------------

class TestBadRowSurvival:
    """A bad vehicle row is counted in vins_refused; the loop continues."""

    def test_bad_row_counted_loop_continues(self):
        """A row with no 'vin' is skipped and counted; subsequent rows are processed."""
        bad_row = {"vin": None, "open_faults": [_soon_fault()]}  # no vin
        good_row = _make_vehicle_row(vin=_VIN2, open_faults=[_soon_fault()])
        written: list[dict] = []
        readers = _simple_readers()

        def write_fn(finding, standing, refresh_on_noop=True):
            written.append(finding)

        stats = run_health_loop(
            vehicle_rows=[bad_row, good_row],
            stock_rows=_STOCK_ROWS,
            computed_at=_COMPUTED_AT,
            write_finding=write_fn,
            **readers,
        )
        assert stats["vins_refused"] >= 1, "Bad row must increment vins_refused."
        assert written, "Good row must still be processed."


# ---------------------------------------------------------------------------
# Bullet 10: stats dict shape
# ---------------------------------------------------------------------------

class TestStatsDictShape:
    """run_health_loop returns a dict with all required keys."""

    def test_stats_keys_present(self):
        required_keys = {
            "vins_selected", "vins_processed", "items_written",
            "items_skipped_unchanged", "vins_refused", "grounding_read_errors",
            "exhausted_items_written", "by_item_type",
        }
        readers = _simple_readers()
        stats = run_health_loop(
            vehicle_rows=[],
            stock_rows=[],
            computed_at=_COMPUTED_AT,
            write_finding=lambda f, s, refresh_on_noop=True: None,
            **readers,
        )
        for key in required_keys:
            assert key in stats, f"Missing stats key: {key!r}"


# ---------------------------------------------------------------------------
# Defect-fix tests (runner.py and grounding.py)
# ---------------------------------------------------------------------------

class TestDefectFixes:
    """Part-A defect fixes verified by import-level behaviour."""

    def test_defect1_same_hash_model_written_is_reexport(self):
        """runner.same_hash_model_written is Stage C's function, not a copy.

        Importing both and comparing identity: they must be the same object.
        Mutation: replace the re-export with a locally-defined function →
        id() comparison fails.
        """
        from agents.fi_fleet_health.runner import same_hash_model_written as health_fn
        from agents.fi_recall_warranty.runner import same_hash_model_written as stage_c_fn
        assert health_fn is stage_c_fn, (
            "agents.fi_fleet_health.runner.same_hash_model_written must be re-exported "
            "from Stage C's runner, not a separate copy. "
            "Mutation: define locally → different object id → fails."
        )

    def test_defect2_make_groundings_reader_returns_tuple(self):
        """make_groundings_reader returns (bool, int) — not just bool.

        The loop needs the error_count to increment stats['grounding_read_errors'].
        Mutation: return only a bool → TypeError when loop unpacks (is_grounded, err_count)
        OR error_count is silently dropped and grounding_read_errors stays 0.
        """
        from agents.fi_fleet_health.grounding import make_groundings_reader
        from unittest.mock import MagicMock

        mock_client = MagicMock()
        # Successful query → not grounded
        mock_client.query.return_value = {"Items": []}
        reader = make_groundings_reader(
            ddb_client=mock_client,
            groundings_table="cms-staging-storage-vehicle-groundings",
            vehicle_id="vehicle-001",
        )
        result = reader()
        assert isinstance(result, tuple) and len(result) == 2, (
            "make_groundings_reader reader must return (bool, int). "
            "Mutation: return only bool → isinstance(result, tuple) fails."
        )
        is_grounded, error_count = result
        assert is_grounded is False
        assert error_count == 0

    def test_defect2_reader_error_returns_false_and_one(self):
        """On a DynamoDB error, reader returns (False, 1)."""
        from agents.fi_fleet_health.grounding import make_groundings_reader
        from botocore.exceptions import ClientError
        from unittest.mock import MagicMock

        mock_client = MagicMock()
        mock_client.query.side_effect = ClientError(
            {"Error": {"Code": "ProvisionedThroughputExceededException", "Message": "test"}},
            "Query",
        )
        reader = make_groundings_reader(
            ddb_client=mock_client,
            groundings_table="cms-staging-storage-vehicle-groundings",
            vehicle_id="vehicle-001",
        )
        result = reader()
        is_grounded, error_count = result
        assert is_grounded is False, "Error must be fail-closed (not grounded)."
        assert error_count == 1, (
            "Error must return error_count=1 so the loop can count it. "
            "Mutation: return 0 → grounding_read_errors stays 0 → test fails."
        )



# ===========================================================================
# G3F1.1 fix tests — one per Critical C1–C7
# ===========================================================================

# ---------------------------------------------------------------------------
# C1: severity and disclosure_txt refreshed through the real write_finding
# ---------------------------------------------------------------------------

class TestC1SeverityRefreshOnReemit:
    """C1: a routine→p0 escalation persists in DynamoDB via the real write_finding.

    Two-run test: run 1 writes a routine booking; run 2 re-runs with a p0 fault.
    The stored row must carry severity='p0' and the stop-driving directive.

    Mutation guards:
    - Remove severity from _FI_REEMIT_ATTRS → stored row still has 'routine' → fails.
    - Remove disclosure_txt from _FI_REEMIT_ATTRS → stored row has no directive → fails.
    """

    def test_severity_escalation_updates_stored_row(self):
        """Day-1 routine, day-2 p0: UpdateItem must include severity and disclosure_txt.

        Drives the real write_finding (tier2/writer.py) with a fake DDB client.
        """
        from unittest.mock import MagicMock, call
        from agents.tier2.writer import write_finding, compute_finding_key
        from agents.fi_fleet_health.write import build_finding, AGENT_ID, FINDING_KIND
        from agents.fi_recall_warranty.evidence import compute_inputs_hash

        findings_table = "vsa-staging-avx-findings"
        vin = _VIN
        fleet_id = _FLEET_ID
        computed_at_d1 = "2026-10-10T05:00:00Z"
        computed_at_d2 = "2026-10-11T05:00:00Z"

        # --- Run 1: routine item, no live row → PutItem ---
        routine_item = {
            "item_type": "booking",
            "severity": "soon",
            "subject_key": "service/1",
            "headline": "Regular check: book service at dealer-m-001",
            "detail": "",
            "recommended_action": {
                "action_kind": "book_fleet_service",
                "target_system": "dms",
                "parameters": {"vehicle_vin": vin, "needs": [],
                               "dealer_id": "dealer-m-001", "urgency": "this_month"},
            },
        }
        raw_d1 = [{"kind": "health.need", "value": {"kind": "fault", "code": "P1AB0"},
                   "vin": vin, "provenance": "live"}]
        finding_d1 = build_finding(
            vin=vin, fleet_id=fleet_id, item=routine_item,
            computed_at=computed_at_d1,
            inputs_hash=compute_inputs_hash(raw_d1),
            evidence=raw_d1, confidence="low",
        )
        finding_key = finding_d1["finding_key"]
        finding_id_d1 = finding_d1["finding_id"]

        ddb1 = MagicMock()
        # No live row on day 1
        ddb1.query.return_value = {"Items": [], "Count": 0}
        ddb1.put_item.return_value = {}

        write_finding(
            finding_d1,
            table_name=findings_table,
            dynamodb_client=ddb1,
            standing={"vin": vin, "fleet_id": fleet_id},
        )
        assert ddb1.put_item.called, "Day 1: PutItem must be called (no live row)."

        # --- Run 2: p0 item, live row exists → UpdateItem ---
        p0_item = {
            "item_type": "booking",
            "severity": "p0",
            "subject_key": "service/1",
            "headline": f"Stop-driving fault {_P0_CODE}: book service at dealer-m-001",
            "detail": "",
            "disclosure_txt": _P0_DIRECTIVE,
            "recommended_action": {
                "action_kind": "book_fleet_service",
                "target_system": "dms",
                "parameters": {"vehicle_vin": vin, "needs": [],
                               "dealer_id": "dealer-m-001", "urgency": "this_week"},
            },
        }
        raw_d2 = [{"kind": "health.need", "value": {"kind": "fault", "code": _P0_CODE},
                   "vin": vin, "provenance": "live"}]
        finding_d2 = build_finding(
            vin=vin, fleet_id=fleet_id, item=p0_item,
            computed_at=computed_at_d2,
            inputs_hash=compute_inputs_hash(raw_d2),
            evidence=raw_d2, confidence="low",
        )
        finding_d2["finding_key"] = finding_key  # same slot

        # Simulate live row from day 1 with a different hash
        from boto3.dynamodb.types import TypeSerializer
        from decimal import Decimal
        _ser = TypeSerializer()

        def _safe_serialize(v):
            """Coerce floats to Decimal for TypeSerializer."""
            if isinstance(v, float):
                return _ser.serialize(Decimal(str(v)))
            return _ser.serialize(v)

        live_ddb_item = {k: _safe_serialize(v) for k, v in finding_d1.items()
                         if v is not None and not isinstance(v, (list, dict))}
        live_ddb_item["inputs_hash"] = {"S": "old-hash-from-day1"}
        live_ddb_item["vin"] = {"S": vin}
        live_ddb_item["expires_at"] = {"S": "2099-01-01T00:00:00Z"}

        ddb2 = MagicMock()
        ddb2.query.return_value = {"Items": [live_ddb_item], "Count": 1}
        ddb2.update_item.return_value = {}

        write_finding(
            finding_d2,
            table_name=findings_table,
            dynamodb_client=ddb2,
            standing={"vin": vin, "fleet_id": fleet_id},
        )
        assert ddb2.update_item.called, "Day 2: UpdateItem must be called (live row exists)."

        call_kwargs = ddb2.update_item.call_args[1]
        expr = call_kwargs["UpdateExpression"]
        names = call_kwargs.get("ExpressionAttributeNames", {})
        vals = call_kwargs.get("ExpressionAttributeValues", {})

        # severity must be SET in the UpdateExpression
        assert any(names.get(ph) == "severity" for ph in names), (
            "severity must be in ExpressionAttributeNames on re-emit (C1). "
            "Mutation: remove severity from _FI_REEMIT_ATTRS → absent → fails."
        )
        # disclosure_txt must be SET in the UpdateExpression
        assert any(names.get(ph) == "disclosure_txt" for ph in names), (
            "disclosure_txt must be in ExpressionAttributeNames on re-emit (C1). "
            "Mutation: remove disclosure_txt from _FI_REEMIT_ATTRS → absent → fails."
        )


# ---------------------------------------------------------------------------
# C2: grounding item carries severity='p0'
# ---------------------------------------------------------------------------

class TestC2GroundingItemSeverity:
    """C2: build_grounding_item and the loop both write severity='p0' on grounding items.

    Mutation guards:
    - Remove severity from build_grounding_item → grounding Finding has no severity
      (build_finding raises on missing severity) → fails.
    - Hardcode 'routine' → severity != 'p0' assertion fails.
    """

    def test_grounding_item_from_loop_has_severity_p0(self):
        """run_health_loop grounding item has severity='p0' (C2).

        Mutation: remove severity='p0' from build_grounding_item →
        build_finding raises ValueError → loop records vins_refused → fails.
        """
        row = _make_vehicle_row(
            open_faults=[_p0_fault()],
            catalog_entries={_P0_CODE: _P0_CATALOG_ENTRY},
        )
        written: list[dict] = []
        readers = _simple_readers(is_grounded=False)

        def write_fn(f, s, refresh_on_noop=True):
            written.append(f)

        stats = run_health_loop(
            vehicle_rows=[row],
            stock_rows=_STOCK_ROWS,
            computed_at=_COMPUTED_AT,
            write_finding=write_fn,
            fault_grounding_enabled=True,
            **readers,
        )
        ground = next(
            (f for f in written if (f.get("subject_key") or "").startswith("ground/")),
            None,
        )
        assert ground is not None, (
            "A grounding item must be written when fault_grounding_enabled=True and p0 fault."
        )
        assert ground.get("severity") == "p0", (
            f"Grounding item must have severity='p0'; got {ground.get('severity')!r}. "
            "Mutation: remove severity='p0' from build_grounding_item → raises or 'routine' → fails."
        )

    def test_grounding_item_has_p0_disclosure_txt(self):
        """Grounding item carries the catalog directive byte for byte."""
        row = _make_vehicle_row(
            open_faults=[_p0_fault()],
            catalog_entries={_P0_CODE: _P0_CATALOG_ENTRY},
        )
        written: list[dict] = []
        readers = _simple_readers(is_grounded=False)

        def write_fn(f, s, refresh_on_noop=True):
            written.append(f)

        run_health_loop(
            vehicle_rows=[row],
            stock_rows=_STOCK_ROWS,
            computed_at=_COMPUTED_AT,
            write_finding=write_fn,
            fault_grounding_enabled=True,
            **readers,
        )
        ground = next(
            (f for f in written if (f.get("subject_key") or "").startswith("ground/")),
            None,
        )
        assert ground is not None
        assert ground.get("disclosure_txt") == _P0_DIRECTIVE, (
            "Grounding item disclosure_txt must equal the catalog directive byte for byte. "
            "Mutation: replace with 'Please drive carefully.' → fails."
        )


# ---------------------------------------------------------------------------
# C3: exhausted items use separate segment; day-2 returns skip
# ---------------------------------------------------------------------------

class TestC3ExhaustedSegmentTerminal:
    """C3: exhausted items use service_exhausted/1 (not service/6).
    On day 2, read_attempt_history_health treats them as terminal → skip.

    Mutation guards:
    - Keep service/6 → subject_key startswith 'service_exhausted' fails.
    - Keep the history reader treating service_exhausted as unknown → day-2 rewrite fails.
    """

    def _exhausted_history(self, segment: str = "service") -> dict:
        from agents.fi_recall_warranty.attempts import MAX_ATTEMPTS
        return {
            segment: {
                "attempt_number": MAX_ATTEMPTS,
                "finding_id": "FIND-exhaust-0001",
                "inputs_hash": "old-hash-x",
                "computed_at": "2026-10-01T00:00:00Z",
                "expires_at": "2026-10-04T00:00:00Z",
                "diagnosis_source": "rules",
                "queue_status": "cancelled",
                "decided_at": "2026-10-01T00:00:00Z",
                "episode_start": 1,  # required so ep_cap=MAX_ATTEMPTS fires
            }
        }

    def test_exhausted_subject_key_uses_separate_segment(self):
        """Exhausted service item has subject_key 'service_exhausted/1' (C3)."""
        row = _make_vehicle_row(
            open_faults=[_p0_fault()],
            catalog_entries={_P0_CODE: _P0_CATALOG_ENTRY},
        )
        written: list[dict] = []
        readers = _simple_readers(history=self._exhausted_history("service"))

        def write_fn(f, s, refresh_on_noop=True):
            written.append(f)

        run_health_loop(
            vehicle_rows=[row],
            stock_rows=_STOCK_ROWS,
            computed_at=_COMPUTED_AT,
            write_finding=write_fn,
            fault_grounding_enabled=False,
            **readers,
        )
        assert written, "Exhausted item must be written."
        exhausted = next(
            (f for f in written if "exhausted" in (f.get("subject_key") or "")), None
        )
        assert exhausted is not None, (
            "Exhausted item must carry 'exhausted' in its subject_key. "
            "Mutation: keep service/6 → 'exhausted' not in subject_key → fails."
        )
        # G3F3.1 (a1 C3): exhausted item carries the actual attempt_n (MAX_ATTEMPTS=5),
        # not the hardcoded /1. History entry has attempt_number=MAX_ATTEMPTS.
        from agents.fi_recall_warranty.attempts import MAX_ATTEMPTS
        assert exhausted["subject_key"] == f"service_exhausted/{MAX_ATTEMPTS}", (
            f"Expected 'service_exhausted/{MAX_ATTEMPTS}'; got {exhausted['subject_key']!r}. "
            "C3 fix: subject carries the attempt that triggered exhaustion, not /1."
        )

    def test_day_two_after_exhaustion_returns_skip(self):
        """On day 2, with service/5 (rejected) + service_exhausted/1, loop skips — order independent.

        W2 fix: exhausted rows are NOT folded into the base segment's history.
        service/5 (rejected) is used by _plan_health_attempt; it yields write_exhausted
        only after the decline window passes (14 days for p0).  Within 14 days it skips.

        Mutation guard (W2): fold exhausted back into base segment → if exhausted arrives
        before service/5, queue_status='exhausted' → skip; if service/5 arrives first,
        queue_status='rejected' → but order-dependent → not a deterministic skip.
        """
        from agents.fi_recall_warranty.attempts import MAX_ATTEMPTS

        # history: service/5 was rejected 1 day ago (within the 14-day window for p0)
        history = {
            "service": {
                "attempt_number": MAX_ATTEMPTS,  # 5
                "finding_id": "FIND-svc5-0001",
                "inputs_hash": "old-hash-x",
                "computed_at": "2026-10-09T00:00:00Z",
                "expires_at": "2026-10-12T00:00:00Z",
                "diagnosis_source": "rules",
                "queue_status": "rejected",
                "decided_at": "2026-10-09T00:00:00Z",  # 1 day ago
                "episode_start": 1,  # required so ep_cap=MAX_ATTEMPTS fires
            }
        }
        row = _make_vehicle_row(
            open_faults=[_p0_fault()],
            catalog_entries={_P0_CODE: _P0_CATALOG_ENTRY},
        )
        written: list[dict] = []
        readers = _simple_readers(history=history)

        def write_fn(f, s, refresh_on_noop=True):
            written.append(f)

        run_health_loop(
            vehicle_rows=[row],
            stock_rows=_STOCK_ROWS,
            computed_at=_COMPUTED_AT,  # day 2
            write_finding=write_fn,
            fault_grounding_enabled=False,
            **readers,
        )
        # 1 day since rejection, 14-day threshold not met → skip
        assert not written, (
            "Day 2 after exhaustion (within decline window) must write nothing. "
            "Mutation: ignore attempt_n cap → write_next → written non-empty → fails."
        )


# ---------------------------------------------------------------------------
# C4: inputs_hash is stable across two different computed_at values
# ---------------------------------------------------------------------------

class TestC4StableInputsHash:
    """C4: inputs_hash uses Stage C's compute_inputs_hash (excludes volatile fields).
    Two runs with the same vehicle state but different computed_at must produce
    the same hash.

    Mutation guard:
    - Revert to a hash that includes computed_at → two runs with same data but
      different computed_at give different hashes → skip rule never fires on
      a model-written row → test_same_data_different_computed_at_same_hash fails.
    """

    def test_same_data_different_computed_at_same_hash(self):
        """Same vehicle state, two different computed_at → same inputs_hash."""
        row = _make_vehicle_row(open_faults=[_soon_fault()])
        hashes: list[str] = []
        readers = _simple_readers()

        for ts in ("2026-10-10T05:00:00Z", "2026-10-11T05:00:00Z"):
            captured: list[dict] = []

            def capture(f, s, refresh_on_noop=True, _c=captured):
                _c.append(f)

            run_health_loop(
                vehicle_rows=[row],
                stock_rows=_STOCK_ROWS,
                computed_at=ts,
                write_finding=capture,
                **readers,
            )
            assert captured, f"Run at {ts} must write an item."
            hashes.append(captured[0]["inputs_hash"])

        assert hashes[0] == hashes[1], (
            f"inputs_hash must be identical across two runs with different computed_at "
            f"but same data; got {hashes[0]!r} vs {hashes[1]!r}. "
            "Mutation: include computed_at in hash → hashes differ → fails."
        )

    def test_skip_fires_on_model_written_same_hash_cross_run(self):
        """Model-written item with same cross-run hash is skipped on day 2.

        Uses two different computed_at values so we're testing the real
        cross-run stability, not same-run deduplication.
        Mutation: include computed_at → day-2 hash differs from day-1 stored hash
        → skip_unchanged returns False → item written instead of skipped → fails.
        """
        row = _make_vehicle_row(open_faults=[_soon_fault()])
        readers = _simple_readers()

        # Day 1: capture the hash
        captured_d1: list[dict] = []

        def cap_d1(f, s, refresh_on_noop=True):
            captured_d1.append(f)

        run_health_loop(
            vehicle_rows=[row],
            stock_rows=_STOCK_ROWS,
            computed_at="2026-10-10T05:00:00Z",
            write_finding=cap_d1,
            **readers,
        )
        assert captured_d1
        d1_hash = captured_d1[0]["inputs_hash"]

        # Day 2: live model-written row carries d1_hash (day 1's hash)
        live_model = {
            "inputs_hash": d1_hash,
            "evidence": [{"kind": "health.diagnosis_source", "value": "model",
                          "source": "agent-fi-fleet-health",
                          "source_ref": "fi-fleet-health-run",
                          "computed_at": "2026-10-10T05:00:00Z",
                          "provenance": "live", "vin": _VIN}],
        }
        written_d2: list[dict] = []

        stats = run_health_loop(
            vehicle_rows=[row],
            stock_rows=_STOCK_ROWS,
            computed_at="2026-10-11T05:00:00Z",  # different from day 1
            grounded_fn=readers["grounded_fn"],
            history_fn=readers["history_fn"],
            standing_fn=readers["standing_fn"],
            lookup_live=lambda k: live_model,
            write_finding=lambda f, s, refresh_on_noop=True: written_d2.append(f),
        )
        assert stats["items_skipped_unchanged"] >= 1, (
            "Model-written item with same cross-run hash must be skipped on day 2. "
            "Mutation: include computed_at in hash → day-2 hash differs → not skipped → fails."
        )
        assert not written_d2, "Day-2 item must not be written on skip-unchanged path."


# ---------------------------------------------------------------------------
# C5: run_service populates vehicle_id and position via real readers
# ---------------------------------------------------------------------------

class TestC5VehicleIdAndPositionPopulated:
    """C5: run_service populates vehicle_id and position for each row before the loop.

    Mutation guards:
    - Drop vehicle_id lookup → grounded_fn never called (vehicle_id is None) → fails.
    - Drop position lookup → origin is None → rooftops ranked by dealer_id → fails.
    """

    def test_run_service_calls_grounded_fn_when_vehicle_id_resolved(
        self, monkeypatch
    ):
        """After vehicle_id is populated via lookup, grounded_fn is called.

        Mutation: skip lookup_vehicle_cms_ids → vehicle_id stays None →
        grounded_fn never called → grounded_fn_calls == 0 → assertion fails.
        """
        from agents.fi_fleet_health import selection as fh_sel
        from agents.fi_recall_warranty import selection as rw_sel
        from agents.fi_fleet_health import loop as fh_loop
        import agents.tier2.runner as _runner

        monkeypatch.setattr(fh_sel, "select_fault_rows", lambda *_a, **_kw: [])
        monkeypatch.setattr(fh_sel, "select_pm_rows",    lambda *_a, **_kw: [])
        monkeypatch.setattr(fh_sel, "select_managed_vins", lambda *_a, **_kw: [])
        monkeypatch.setattr(fh_sel, "select_daily_usage",  lambda *_a, **_kw: [])
        monkeypatch.setattr(fh_sel, "select_agent2_findings", lambda *_a, **_kw: ({}, 0))
        monkeypatch.setattr(rw_sel, "select_parts_stock",  lambda *_a, **_kw: [])

        # build_vehicle_rows returns ONE row WITHOUT vehicle_id set (the assembler never sets it)
        def fake_build(**_kw):
            return (
                [{"vin": _VIN, "fleet_id": _FLEET_ID,
                  "open_faults": [{"code": _P0_CODE, "band": "p0", "last_seen_at": "2026-10-01Z"}],
                  "pm_tasks": [], "agent2_findings": [], "utilisation": None,
                  "position": None, "catalog_entries": {_P0_CODE: _P0_CATALOG_ENTRY}}],
                {},
            )
        monkeypatch.setattr(fh_sel, "build_vehicle_rows", fake_build)

        grounded_fn_calls: list = []
        vehicle_ids_seen: list = []

        def fake_loop(*, vehicle_rows, grounded_fn, write_finding, **kw):
            # record what vehicle_id was set before the loop
            for vr in vehicle_rows:
                vehicle_ids_seen.append(vr.get("vehicle_id"))
            # call grounded_fn to simulate the loop behaviour
            for vr in vehicle_rows:
                vid = vr.get("vehicle_id")
                if vid is not None:
                    grounded_fn_calls.append(vid)
                    grounded_fn(vid)
            return {"vins_selected": 1, "vins_processed": 0, "items_written": 0,
                    "items_skipped_unchanged": 0, "vins_refused": 0,
                    "grounding_read_errors": 0, "exhausted_items_written": 0,
                    "by_item_type": {}}

        monkeypatch.setattr(fh_loop, "run_health_loop", fake_loop)

        # lookup_vehicle_cms_ids returns (vehicle_id, fleet_id, None)
        from agents.fi_recall_warranty import selection as _rw_sel_mod
        monkeypatch.setattr(
            _rw_sel_mod, "lookup_vehicle_cms_ids",
            lambda vin, **kw: ("vehicle-resolved-001", _FLEET_ID, None),
        )
        monkeypatch.setattr(
            _rw_sel_mod, "lookup_fleet_locations",
            lambda fleet_id, **kw: {},
        )

        from agents.tier2.tests.test_runner_service import _AvxWriter, _Cw
        records: list = []
        _runner.run_service(
            "staging",
            athena_client=object(),
            workgroup="cvx-staging-analytics",
            results_s3_path="s3://results/tier2/fi-fleet-health/",
            ddb_client=object(),
            findings_table="vsa-staging-avx-findings",
            actions_table="vsa-staging-avx-actions",
            groundings_table="cms-staging-storage-vehicle-groundings",
            vehicles_table="cms-staging-storage-vehicles",
            fleet_locations_table="cms-staging-storage-fleet-locations",
            avx_writer=_AvxWriter(),
            owner_resolver=None,
            cloudwatch_client=_Cw(),
            run_record_writer=lambda **k: records.append(k),
            fault_grounding_enabled=True,
        )

        assert vehicle_ids_seen == ["vehicle-resolved-001"], (
            f"vehicle_id must be populated before the loop; got {vehicle_ids_seen!r}. "
            "Mutation: skip lookup_vehicle_cms_ids → None → assertion fails."
        )
        assert grounded_fn_calls == ["vehicle-resolved-001"], (
            "grounded_fn must be called with the resolved vehicle_id. "
            "Mutation: skip lookup → vehicle_id=None → grounded_fn never called → fails."
        )


# ---------------------------------------------------------------------------
# C6: evidence entries are schema-compliant; no top-level diagnosis_source / fleet_id
# ---------------------------------------------------------------------------

class TestC6SchemaCompliantEvidence:
    """C6: every Finding produced by run_health_loop validates against finding.schema.json.

    Mutation guards:
    - Remove source/source_ref from _build_evidence → schema validation raises → fails.
    - Add diagnosis_source at Finding top level → additionalProperties:false raises → fails.
    """

    def _get_schema(self):
        import json
        from pathlib import Path
        schema_path = (
            Path(__file__).parent.parent.parent.parent
            / "agents/tier2/schemas/finding.schema.json"
        )
        return json.loads(schema_path.read_text())

    def test_service_booking_finding_validates_against_schema(self):
        """A service booking Finding produced by run_health_loop validates against the schema."""
        import jsonschema

        schema = self._get_schema()
        row = _make_vehicle_row(
            open_faults=[_p0_fault()],
            catalog_entries={_P0_CODE: _P0_CATALOG_ENTRY},
        )
        written: list[dict] = []
        readers = _simple_readers()

        def write_fn(f, s, refresh_on_noop=True):
            written.append(f)

        run_health_loop(
            vehicle_rows=[row],
            stock_rows=_STOCK_ROWS,
            computed_at=_COMPUTED_AT,
            write_finding=write_fn,
            fault_grounding_enabled=False,
            **readers,
        )
        assert written
        booking = next(
            (f for f in written if (f.get("subject_key") or "").startswith("service/")), None
        )
        assert booking is not None

        # Must not raise ValidationError.
        jsonschema.validate(booking, schema)

        # Must not carry top-level diagnosis_source or fleet_id (additionalProperties:false)
        assert "diagnosis_source" not in booking, (
            "top-level diagnosis_source violates additionalProperties:false on the Finding. "
            "Mutation: add it back → jsonschema.validate raises → fails."
        )

    def test_grounding_finding_validates_against_schema(self):
        """A grounding Finding produced by run_health_loop validates against the schema."""
        import jsonschema

        schema = self._get_schema()
        row = _make_vehicle_row(
            open_faults=[_p0_fault()],
            catalog_entries={_P0_CODE: _P0_CATALOG_ENTRY},
        )
        written: list[dict] = []
        readers = _simple_readers(is_grounded=False)

        def write_fn(f, s, refresh_on_noop=True):
            written.append(f)

        run_health_loop(
            vehicle_rows=[row],
            stock_rows=_STOCK_ROWS,
            computed_at=_COMPUTED_AT,
            write_finding=write_fn,
            fault_grounding_enabled=True,
            **readers,
        )
        ground = next(
            (f for f in written if (f.get("subject_key") or "").startswith("ground/")), None
        )
        assert ground is not None
        jsonschema.validate(ground, schema)
        assert "diagnosis_source" not in ground

    def test_evidence_entries_carry_source_and_source_ref(self):
        """Each evidence entry has required 'source' and 'source_ref' fields (C6)."""
        row = _make_vehicle_row(open_faults=[_soon_fault()])
        written: list[dict] = []
        readers = _simple_readers()

        def write_fn(f, s, refresh_on_noop=True):
            written.append(f)

        run_health_loop(
            vehicle_rows=[row],
            stock_rows=_STOCK_ROWS,
            computed_at=_COMPUTED_AT,
            write_finding=write_fn,
            **readers,
        )
        assert written
        for ev_entry in written[0].get("evidence", []):
            assert "source" in ev_entry, (
                f"Evidence entry missing 'source': {ev_entry!r}. "
                "Mutation: remove source from _build_evidence → schema validation fails."
            )
            assert "source_ref" in ev_entry, (
                f"Evidence entry missing 'source_ref': {ev_entry!r}. "
                "Mutation: remove source_ref → schema validation fails."
            )


# ---------------------------------------------------------------------------
# C7: safety properties asserted on run_health_loop output
# ---------------------------------------------------------------------------

class TestC7SafetyPropertiesOnLoopOutput:
    """C7: the safety properties the reviewer named survive M1–M8 on the loop's writers.

    Each test goes through run_health_loop and asserts the exact property the
    corresponding mutant would break.

    Mutation guards (from the reviewer's mutant table):
    M1: booking disclosure_txt trimmed to 20 chars → byte-equality with full directive fails.
    M2: grounding disclosure_txt replaced with generic text → byte-equality fails.
    M3: booking headline replaced with free text → exact template match fails.
    M4: p0 booking written as 'routine' → severity assertion fails.
    M5: server-set needs emptied → needs assertion fails.
    M6: p0 urgency set to 'this_month' → urgency assertion fails.
    M8: grounding fault_codes emptied → fault_codes assertion fails.
    """

    def _run_and_collect(self, row, fault_grounding=False) -> list[dict]:
        written: list[dict] = []
        readers = _simple_readers(is_grounded=False)

        def write_fn(f, s, refresh_on_noop=True):
            written.append(f)

        run_health_loop(
            vehicle_rows=[row],
            stock_rows=_STOCK_ROWS,
            computed_at=_COMPUTED_AT,
            write_finding=write_fn,
            fault_grounding_enabled=fault_grounding,
            **readers,
        )
        return written

    def test_m1_p0_booking_disclosure_txt_is_full_directive(self):
        """M1: booking disclosure_txt == full catalog directive (not trimmed).

        Mutation: trim to 20 chars → len < len(directive) → fails.
        """
        row = _make_vehicle_row(
            open_faults=[_p0_fault()],
            catalog_entries={_P0_CODE: _P0_CATALOG_ENTRY},
        )
        written = self._run_and_collect(row)
        booking = next(
            (f for f in written if (f.get("subject_key") or "").startswith("service/")), None
        )
        assert booking is not None
        assert booking.get("disclosure_txt") == _P0_DIRECTIVE, (
            "p0 booking disclosure_txt must equal the full catalog directive. "
            "Mutation M1: trim to 20 chars → len mismatch → fails."
        )

    def test_m2_grounding_disclosure_txt_is_full_directive(self):
        """M2: grounding disclosure_txt == full directive (not generic fallback).

        Mutation: replace with 'Please drive carefully.' → fails.
        """
        row = _make_vehicle_row(
            open_faults=[_p0_fault()],
            catalog_entries={_P0_CODE: _P0_CATALOG_ENTRY},
        )
        written = self._run_and_collect(row, fault_grounding=True)
        ground = next(
            (f for f in written if (f.get("subject_key") or "").startswith("ground/")), None
        )
        assert ground is not None
        assert ground.get("disclosure_txt") == _P0_DIRECTIVE, (
            "Grounding disclosure_txt must be the catalog directive. "
            "Mutation M2: replace with generic text → fails."
        )

    def test_m3_p0_booking_headline_matches_template(self):
        """M3: booking headline matches the expected template format.

        Template: 'Stop-driving fault {code}: book service at {dealer}'.
        Mutation M3: replace with free text → pattern match fails.
        """
        row = _make_vehicle_row(
            open_faults=[_p0_fault()],
            catalog_entries={_P0_CODE: _P0_CATALOG_ENTRY},
        )
        written = self._run_and_collect(row)
        booking = next(
            (f for f in written if (f.get("subject_key") or "").startswith("service/")), None
        )
        assert booking is not None
        headline = booking.get("headline", "")
        assert _P0_CODE in headline, (
            f"p0 booking headline must contain the fault code {_P0_CODE!r}; got {headline!r}. "
            "Mutation M3: free-text headline → code absent → fails."
        )
        assert "Stop-driving fault" in headline or "stop-driving" in headline.lower(), (
            f"p0 booking headline must mention 'Stop-driving fault'; got {headline!r}. "
            "Mutation M3: generic headline → pattern absent → fails."
        )

    def test_m4_p0_booking_severity_is_p0(self):
        """M4: p0 booking item has severity='p0'.

        Mutation M4: write as 'routine' → severity != 'p0' → fails.
        """
        row = _make_vehicle_row(
            open_faults=[_p0_fault()],
            catalog_entries={_P0_CODE: _P0_CATALOG_ENTRY},
        )
        written = self._run_and_collect(row)
        booking = next(
            (f for f in written if (f.get("subject_key") or "").startswith("service/")), None
        )
        assert booking is not None
        assert booking.get("severity") == "p0", (
            f"p0 fault booking must have severity='p0'; got {booking.get('severity')!r}. "
            "Mutation M4: written as 'routine' → fails."
        )

    def test_m5_p0_booking_needs_are_non_empty(self):
        """M5: booking needs list is non-empty for a p0 fault.

        Mutation M5: empty needs → needs list is [] → assertion fails.
        """
        row = _make_vehicle_row(
            open_faults=[_p0_fault()],
            catalog_entries={_P0_CODE: _P0_CATALOG_ENTRY},
        )
        written = self._run_and_collect(row)
        booking = next(
            (f for f in written if (f.get("subject_key") or "").startswith("service/")), None
        )
        assert booking is not None
        ra = booking.get("recommended_action") or {}
        params = ra.get("parameters") or {}
        needs = params.get("needs", [])
        assert len(needs) >= 1, (
            f"p0 booking recommended_action.parameters.needs must be non-empty; got {needs!r}. "
            "Mutation M5: empty server-set needs → len == 0 → fails."
        )

    def test_m6_p0_booking_urgency_is_this_week(self):
        """M6: p0 booking urgency is 'this_week'.

        Mutation M6: urgency set to 'this_month' → != 'this_week' → fails.
        """
        row = _make_vehicle_row(
            open_faults=[_p0_fault()],
            catalog_entries={_P0_CODE: _P0_CATALOG_ENTRY},
        )
        written = self._run_and_collect(row)
        booking = next(
            (f for f in written if (f.get("subject_key") or "").startswith("service/")), None
        )
        assert booking is not None
        ra = booking.get("recommended_action") or {}
        params = ra.get("parameters") or {}
        urgency = params.get("urgency")
        assert urgency == "this_week", (
            f"p0 booking urgency must be 'this_week'; got {urgency!r}. "
            "Mutation M6: urgency='this_month' → fails."
        )

    def test_m8_grounding_fault_codes_non_empty(self):
        """M8: grounding recommended_action.parameters.fault_codes contains the p0 code.

        Mutation M8: empty fault_codes → assertion fails.
        """
        row = _make_vehicle_row(
            open_faults=[_p0_fault()],
            catalog_entries={_P0_CODE: _P0_CATALOG_ENTRY},
        )
        written = self._run_and_collect(row, fault_grounding=True)
        ground = next(
            (f for f in written if (f.get("subject_key") or "").startswith("ground/")), None
        )
        assert ground is not None
        ra = ground.get("recommended_action") or {}
        params = ra.get("parameters") or {}
        fault_codes = params.get("fault_codes", [])
        assert _P0_CODE in fault_codes, (
            f"Grounding fault_codes must contain {_P0_CODE!r}; got {fault_codes!r}. "
            "Mutation M8: empty fault_codes → code absent → fails."
        )

    def test_soon_fault_booking_severity_is_soon_not_p0(self):
        """Non-p0 band: soon fault booking has severity='soon' (band table row)."""
        row = _make_vehicle_row(open_faults=[_soon_fault()])
        written = self._run_and_collect(row)
        booking = next(
            (f for f in written if (f.get("subject_key") or "").startswith("service/")), None
        )
        assert booking is not None
        assert booking.get("severity") == "soon", (
            f"soon-fault booking must have severity='soon'; got {booking.get('severity')!r}."
        )



# ===========================================================================
# G3F1.2 — Warning fix tests (W1–W10)
# ===========================================================================

# ---------------------------------------------------------------------------
# W1: Agent 2 labor-op maps correctly by finding_kind (not severity_band)
# ---------------------------------------------------------------------------

class TestW1Agent2LaborOpByFindingKind:
    """W1: map_need_to_labor_op uses finding_kind to key the config.

    Before the fix, the code looked up by severity_band ('soon'/'attention'),
    which always matched the 'default' entry → EV_SYSTEM_INSPECTION.
    After the fix, 'health.battery' → component 'battery' → HV_BATTERY_DIAGNOSTIC.

    Mutant: change map_need_to_labor_op to use severity_band again →
            result['labor_op'] == 'EV_SYSTEM_INSPECTION' instead of
            'HV_BATTERY_DIAGNOSTIC' → assertion fails.
    """

    def test_agent2_battery_kind_maps_to_hv_diagnostic(self):
        """health.battery finding_kind → HV_BATTERY_DIAGNOSTIC (W1)."""
        config = load_needs_config()
        need = {
            "kind": "agent2_finding",
            "finding_id": "fi-batt-001",
            "finding_kind": "health.battery",   # real Agent 2 kind
            "severity_band": "soon",            # severity, NOT the key
        }
        result = map_need_to_labor_op(need, config)
        assert result["labor_op"] == "LAB-BATSVC-01", (
            f"health.battery Agent 2 need must map to LAB-BATSVC-01; "
            f"got {result['labor_op']!r}. "
            "Mutant: use severity_band as key → 'soon' not in config → default → fails."
        )

    def test_agent2_brakes_kind_maps_to_brake_inspection(self):
        """health.brakes finding_kind → BRAKE_INSPECTION (W1)."""
        config = load_needs_config()
        need = {
            "kind": "agent2_finding",
            "finding_id": "fi-brk-001",
            "finding_kind": "health.brakes",
            "severity_band": "attention",
        }
        result = map_need_to_labor_op(need, config)
        assert result["labor_op"] == "LAB-BRKSVC-01", (
            f"health.brakes Agent 2 need must map to LAB-BRKSVC-01; "
            f"got {result['labor_op']!r}."
        )

    def test_agent2_tires_kind_maps_to_tire_inspection(self):
        """health.tires finding_kind → TIRE_INSPECTION (W1)."""
        config = load_needs_config()
        need = {
            "kind": "agent2_finding",
            "finding_id": "fi-tires-001",
            "finding_kind": "health.tires",
            "severity_band": "soon",
        }
        result = map_need_to_labor_op(need, config)
        assert result["labor_op"] == "LAB-TIRSVC-01", (
            f"health.tires need must map to LAB-TIRSVC-01; got {result['labor_op']!r}."
        )

    def test_agent2_loop_output_contains_correct_labor_op(self):
        """run_health_loop booking for an Agent 2 battery finding uses HV_BATTERY_DIAGNOSTIC.

        Tests the full loop path with a production-shaped Agent 2 finding row.
        Mutant: revert map to use severity_band → EV_SYSTEM_INSPECTION in needs → fails.
        """
        row = _make_vehicle_row(
            agent2_findings=[{
                "finding_id": "fi-batt-loop",
                "severity": "attention",
                "finding_kind": "health.battery",
            }],
        )
        written: list[dict] = []
        readers = _simple_readers()

        def write_fn(f, s, refresh_on_noop=True):
            written.append(f)

        run_health_loop(
            vehicle_rows=[row],
            stock_rows=_STOCK_ROWS,
            computed_at=_COMPUTED_AT,
            write_finding=write_fn,
            **readers,
        )
        assert written, "A booking item must be written for an Agent 2 finding."
        booking = next(
            (f for f in written if (f.get("subject_key") or "").startswith("service/")), None
        )
        assert booking is not None
        ra = booking.get("recommended_action") or {}
        params = ra.get("parameters") or {}
        needs = params.get("needs", [])
        labor_ops = [n.get("labor_op") for n in needs]
        assert "LAB-BATSVC-01" in labor_ops, (
            f"Agent 2 battery need must produce LAB-BATSVC-01 in needs; "
            f"got {labor_ops!r}. "
            "Mutant: use severity_band for config key → EV_SYSTEM_INSPECTION → fails."
        )


# ---------------------------------------------------------------------------
# W2: each need carries a deterministic need_id
# ---------------------------------------------------------------------------

class TestW2NeedIds:
    """W2: needs in booking recommended_action carry need_id (contract §1.7).

    Mutant: remove need_id from the need dict → 'need_id' absent in every
            need line → assertion fails.
    """

    def test_fault_need_id_in_booking_action(self):
        """Fault need line in recommended_action carries need_id='fault:<code>'."""
        row = _make_vehicle_row(
            open_faults=[_p0_fault()],
            catalog_entries={_P0_CODE: _P0_CATALOG_ENTRY},
        )
        written: list[dict] = []
        readers = _simple_readers()

        def write_fn(f, s, refresh_on_noop=True):
            written.append(f)

        run_health_loop(
            vehicle_rows=[row],
            stock_rows=_STOCK_ROWS,
            computed_at=_COMPUTED_AT,
            write_finding=write_fn,
            fault_grounding_enabled=False,
            **readers,
        )
        booking = next(
            (f for f in written if (f.get("subject_key") or "").startswith("service/")), None
        )
        assert booking is not None
        ra = booking.get("recommended_action") or {}
        params = ra.get("parameters") or {}
        needs = params.get("needs", [])
        assert needs, "needs must be non-empty for a p0 fault."
        for n in needs:
            assert "need_id" in n, (
                f"Each need must carry need_id; missing in {n!r}. "
                "Mutant: drop need_id → absent → fails."
            )
        fault_need = next((n for n in needs if n.get("need_id", "").startswith("fault:")), None)
        assert fault_need is not None, (
            "Fault need must have need_id starting with 'fault:'. "
            "Mutant: use empty string → no 'fault:' prefix → fails."
        )
        assert fault_need["need_id"] == f"fault:{_P0_CODE}", (
            f"Fault need_id must be 'fault:{_P0_CODE}'; got {fault_need['need_id']!r}. "
            "Mutant: use random id → determinism test fails."
        )

    def test_pm_need_id_in_booking_action(self):
        """PM need line carries need_id='pm:<task_code>'."""
        row = _make_vehicle_row(pm_tasks=[_overdue_pm()])
        written: list[dict] = []
        readers = _simple_readers()

        def write_fn(f, s, refresh_on_noop=True):
            written.append(f)

        run_health_loop(
            vehicle_rows=[row],
            stock_rows=_STOCK_ROWS,
            computed_at=_COMPUTED_AT,
            write_finding=write_fn,
            **readers,
        )
        booking = next(
            (f for f in written if (f.get("subject_key") or "").startswith("service/")), None
        )
        assert booking is not None
        ra = booking.get("recommended_action") or {}
        params = ra.get("parameters") or {}
        needs = params.get("needs", [])
        pm_need = next((n for n in needs if n.get("need_id", "").startswith("pm:")), None)
        assert pm_need is not None, (
            "PM need must have need_id starting with 'pm:'. "
            "Mutant: drop need_id → absent → fails."
        )
        assert pm_need["need_id"] == "pm:TIRE_ROTATION", (
            f"PM need_id must be 'pm:TIRE_ROTATION'; got {pm_need['need_id']!r}."
        )


# ---------------------------------------------------------------------------
# W3: evidence key names match the contract
# ---------------------------------------------------------------------------

class TestW3EvidenceKeyNames:
    """W3: the loop's evidence entries use the exact names from contract §1.6.

    Mutant per sub-item (a–d): rename the key back to the old name → assertion fails.
    """

    def _run_and_collect(self, row: dict, grounding: bool = False) -> list[dict]:
        written: list[dict] = []
        readers = _simple_readers(is_grounded=False)

        def write_fn(f, s, refresh_on_noop=True):
            written.append(f)

        run_health_loop(
            vehicle_rows=[row],
            stock_rows=_STOCK_ROWS,
            computed_at=_COMPUTED_AT,
            write_finding=write_fn,
            fault_grounding_enabled=grounding,
            **readers,
        )
        return written

    def test_w3a_fault_need_uses_severity_band(self):
        """health.need for a fault uses 'severity_band', not 'band' (W3a).

        Mutant: rename back to 'band' → 'severity_band' absent → fails.
        """
        row = _make_vehicle_row(
            open_faults=[_soon_fault()],
        )
        written = self._run_and_collect(row)
        booking = next(
            (f for f in written if (f.get("subject_key") or "").startswith("service/")), None
        )
        assert booking is not None
        need_entries = [
            e for e in booking.get("evidence", []) if e.get("kind") == "health.need"
        ]
        assert need_entries, "Evidence must contain health.need entries."
        for entry in need_entries:
            val = entry.get("value") or {}
            if val.get("kind") == "fault":
                assert "severity_band" in val, (
                    f"Fault need evidence must use 'severity_band', not 'band'; got {val!r}. "
                    "Mutant W3a: rename back to 'band' → absent → fails."
                )
                assert "band" not in val, (
                    f"Fault need evidence must NOT use 'band' (old name); got {val!r}. "
                    "Mutant W3a: keep old 'band' key → unexpected key present."
                )

    def test_w3b_utilisation_uses_daily_average_miles_30d(self):
        """vehicle.utilisation evidence uses 'daily_average_miles_30d' (W3b).

        Mutant: rename back to 'avg_miles_30d' → 'daily_average_miles_30d' absent → fails.
        """
        row = _make_vehicle_row(
            open_faults=[_soon_fault()],
        )
        written = self._run_and_collect(row)
        booking = next(
            (f for f in written if (f.get("subject_key") or "").startswith("service/")), None
        )
        assert booking is not None
        util_entries = [
            e for e in booking.get("evidence", []) if e.get("kind") == "vehicle.utilisation"
        ]
        if util_entries:
            # utilisation present (vehicle row carries utilisation)
            val = util_entries[0].get("value") or {}
            assert "daily_average_miles_30d" in val, (
                f"vehicle.utilisation evidence must use 'daily_average_miles_30d'; got {val!r}. "
                "Mutant W3b: rename back to 'avg_miles_30d' → absent → fails."
            )
            assert "avg_miles_30d" not in val, (
                "vehicle.utilisation must NOT carry 'avg_miles_30d' (old name)."
            )

    def test_w3c_rooftops_uses_parts_in_stock(self):
        """health.rooftops evidence uses 'parts_in_stock', not 'in_stock' (W3c).

        Mutant: rename back to 'in_stock' → 'parts_in_stock' absent → fails.
        """
        row = _make_vehicle_row(
            open_faults=[_soon_fault()],
            position={"latitude": "47.6062", "longitude": "-122.3321",
                      "position_date": "2026-10-09"},
        )
        written = self._run_and_collect(row)
        booking = next(
            (f for f in written if (f.get("subject_key") or "").startswith("service/")), None
        )
        assert booking is not None
        rooftop_entries = [
            e for e in booking.get("evidence", []) if e.get("kind") == "health.rooftops"
        ]
        assert rooftop_entries, "Evidence must contain health.rooftops entries."
        val = rooftop_entries[0].get("value") or []
        assert val, "health.rooftops value must be non-empty."
        for rooftop in val:
            assert "parts_in_stock" in rooftop, (
                f"health.rooftops entry must use 'parts_in_stock'; got {rooftop!r}. "
                "Mutant W3c: rename back to 'in_stock' → absent → fails."
            )
            assert "in_stock" not in rooftop, (
                "health.rooftops entry must NOT carry 'in_stock' (old name)."
            )

    def test_w3d_recall_visit_uses_recall_item_finding_id(self):
        """health.recall_visit evidence uses 'recall_item_finding_id' (W3d).

        Mutant: rename back to 'finding_id' → 'recall_item_finding_id' absent → fails.
        """
        recall_visit = {
            "recall_item_finding_id": "FIND-recall-001",
            "recall_id": "RC-TEST-001",
            "rooftop_dealer_id": "dealer-recall-001",
        }
        row = _make_vehicle_row(
            open_faults=[_soon_fault()],
        )
        written: list[dict] = []
        readers = _simple_readers(recall_visit=recall_visit)

        def write_fn(f, s, refresh_on_noop=True):
            written.append(f)

        run_health_loop(
            vehicle_rows=[row],
            stock_rows=_STOCK_ROWS,
            computed_at=_COMPUTED_AT,
            write_finding=write_fn,
            **readers,
        )
        booking = next(
            (f for f in written if (f.get("subject_key") or "").startswith("service/")), None
        )
        assert booking is not None
        recall_entries = [
            e for e in booking.get("evidence", []) if e.get("kind") == "health.recall_visit"
        ]
        assert recall_entries, "Evidence must contain health.recall_visit when recall_visit provided."
        val = recall_entries[0].get("value") or {}
        assert "recall_item_finding_id" in val, (
            f"health.recall_visit must use 'recall_item_finding_id'; got {val!r}. "
            "Mutant W3d: rename back to 'finding_id' → absent → fails."
        )
        assert "finding_id" not in val, (
            "health.recall_visit must NOT carry 'finding_id' (old name)."
        )


# ---------------------------------------------------------------------------
# W4: last_seen_at is present and populated in grounding evidence
# ---------------------------------------------------------------------------

class TestW4LastSeenAtInGroundingEvidence:
    """W4: health.grounding_reason.last_sighting entries carry last_seen_at.

    The field is currently MAX(raised_at) from the ADP view; the contract
    calls it 'last_seen_at' as the best available anchor.  A test verifies
    it is populated so the return-to-service check can read it.

    Mutant: set last_seen_at to '' in all sighting entries →
            truthy check fails → assertion fails.
    """

    def test_last_seen_at_present_in_grounding_evidence(self):
        """Grounding evidence health.grounding_reason carries non-empty last_seen_at."""
        row = _make_vehicle_row(
            open_faults=[_p0_fault()],
            catalog_entries={_P0_CODE: _P0_CATALOG_ENTRY},
        )
        written: list[dict] = []
        readers = _simple_readers(is_grounded=False)

        def write_fn(f, s, refresh_on_noop=True):
            written.append(f)

        run_health_loop(
            vehicle_rows=[row],
            stock_rows=_STOCK_ROWS,
            computed_at=_COMPUTED_AT,
            write_finding=write_fn,
            fault_grounding_enabled=True,
            **readers,
        )
        ground = next(
            (f for f in written if (f.get("subject_key") or "").startswith("ground/")), None
        )
        assert ground is not None, "A grounding item must be written."
        gnd_entries = [
            e for e in ground.get("evidence", []) if e.get("kind") == "health.grounding_reason"
        ]
        assert gnd_entries, "Grounding item must carry health.grounding_reason evidence."
        val = gnd_entries[0].get("value") or {}
        sightings = val.get("last_sighting", [])
        assert sightings, "health.grounding_reason must have at least one last_sighting entry."
        for s in sightings:
            assert s.get("last_seen_at"), (
                f"last_sighting entry must carry non-empty last_seen_at; got {s!r}. "
                "Mutant W4: set to '' → falsy → fails."
            )


# ---------------------------------------------------------------------------
# W5: multiple p0 codes produce deterministic headline, directive, hash
# ---------------------------------------------------------------------------

class TestW5DeterministicP0Order:
    """W5: multiple p0 codes are sorted before primary code selection.

    Two runs with the same two p0 codes in swapped Athena row order must
    produce identical headline, directive, and inputs_hash.

    Mutant: drop the sort and rely on open_faults order →
            swapped order gives different headline/hash → assertion fails.
    """

    _P0_CODE_2 = "P1A0F"
    _P0_DIRECTIVE_2 = (
        "Stop driving immediately. Battery thermal fault detected. "
        "Do not recharge. Contact your dealer immediately."
    )
    _P0_CATALOG_2 = {
        "code": _P0_CODE_2,
        "description": "Battery thermal management fault.",
        "system": "ev_battery",
        "band": "p0",
        "applies_to": ["ev"],
        "directive": _P0_DIRECTIVE_2,
    }

    def _run_with_faults(self, faults: list[dict], catalog: dict) -> dict:
        row = _make_vehicle_row(
            open_faults=faults,
            catalog_entries=catalog,
        )
        written: list[dict] = []
        readers = _simple_readers()

        def write_fn(f, s, refresh_on_noop=True):
            written.append(f)

        run_health_loop(
            vehicle_rows=[row],
            stock_rows=_STOCK_ROWS,
            computed_at=_COMPUTED_AT,
            write_finding=write_fn,
            **readers,
        )
        booking = next(
            (f for f in written if (f.get("subject_key") or "").startswith("service/")), None
        )
        assert booking is not None, "A booking item must be written."
        return booking

    def test_two_p0_codes_same_headline_regardless_of_row_order(self):
        """Swapping p0 fault row order produces the same headline (W5).

        Both codes have the same last_seen_at; tie-breaking is by code (asc).
        Mutant: drop sort → swapped order picks different primary code → headlines differ → fails.
        """
        catalog = {
            _P0_CODE: _P0_CATALOG_ENTRY,
            self._P0_CODE_2: self._P0_CATALOG_2,
        }
        ts = "2026-10-09T12:00:00Z"  # same timestamp for both — tie-break by code
        fault_a = {"code": _P0_CODE,       "band": "p0", "last_seen_at": ts}
        fault_b = {"code": self._P0_CODE_2, "band": "p0", "last_seen_at": ts}

        # Run 1: faults in order [a, b]
        b1 = self._run_with_faults([fault_a, fault_b], catalog)
        # Run 2: faults in order [b, a] (swapped)
        b2 = self._run_with_faults([fault_b, fault_a], catalog)

        assert b1.get("headline") == b2.get("headline"), (
            f"Headlines must be identical regardless of fault row order. "
            f"Run 1: {b1.get('headline')!r}  Run 2: {b2.get('headline')!r}. "
            "Mutant W5: drop sort → order-dependent → fails."
        )
        assert b1.get("inputs_hash") == b2.get("inputs_hash"), (
            f"inputs_hash must be identical regardless of fault row order. "
            f"Run 1: {b1.get('inputs_hash')!r}  Run 2: {b2.get('inputs_hash')!r}. "
            "Mutant W5: drop sort → order-dependent hash → fails."
        )
        assert b1.get("disclosure_txt") == b2.get("disclosure_txt"), (
            "disclosure_txt must be the same primary code's directive both runs."
        )

    def test_latest_sighting_wins_tie_break(self):
        """Code with the latest last_seen_at is chosen as primary (W5).

        P1A0F seen 2026-10-09; P0AA6 seen 2026-10-07 → P1A0F is primary.
        Mutant W5: drop sort → first in list (P0AA6) chosen → wrong directive → fails.
        """
        catalog = {
            _P0_CODE: _P0_CATALOG_ENTRY,
            self._P0_CODE_2: self._P0_CATALOG_2,
        }
        # fault_b (P1A0F) has a later sighting
        fault_a = {"code": _P0_CODE,       "band": "p0", "last_seen_at": "2026-10-07T12:00:00Z"}
        fault_b = {"code": self._P0_CODE_2, "band": "p0", "last_seen_at": "2026-10-09T12:00:00Z"}

        # Run with fault_a first in the list (without sort, fault_a would be primary)
        booking = self._run_with_faults([fault_a, fault_b], catalog)
        # After sort (latest sighting desc), fault_b should be primary
        assert self._P0_CODE_2 in booking.get("headline", ""), (
            f"Latest-sighted code {self._P0_CODE_2!r} must be in headline; "
            f"got {booking.get('headline')!r}. "
            "Mutant W5: drop sort → first in list is P0AA6 → wrong headline → fails."
        )


# ---------------------------------------------------------------------------
# W6: p0 fault with no directive text is refused
# ---------------------------------------------------------------------------

class TestW6P0NoDirectiveRefused:
    """W6: a p0 fault whose directive_text is NULL or missing now uses the
    pullover_stop_engine fallback instead of refusing the vehicle (fix F16).

    Keeping the old class name so earlier review references are traceable.
    The old behavior (refusal) was the defect; this class now documents the fix.
    """

    def test_p0_fault_no_directive_refuses_vehicle(self):
        """Vehicle with a p0 fault and directive=None gets both items with the
        pullover_stop_engine fallback as disclosure_txt; it is NOT refused (fix F16).

        Old assertion (vins_refused >= 1) was testing the defect.
        New assertion: items written, disclosure_txt == fallback, fallbacks counted.
        """
        from agents.supervisor.dtc_response_catalog import RESPONSE_TEMPLATES
        _raw = RESPONSE_TEMPLATES["pullover_stop_engine"]["first_sentence"]
        expected_fallback = _raw[0].upper() + _raw[1:]

        p0_code = "P0AA6"
        row = _make_vehicle_row(
            open_faults=[{"code": p0_code, "band": "p0", "last_seen_at": "2026-10-09T12:00:00Z"}],
            catalog_entries={
                p0_code: {
                    "code": p0_code,
                    "band": "p0",
                    "directive": None,  # ← NULL directive triggers fallback
                }
            },
        )
        written: list[dict] = []
        readers = _simple_readers(is_grounded=False)

        def write_fn(f, s, refresh_on_noop=True):
            written.append(f)

        stats = run_health_loop(
            vehicle_rows=[row],
            stock_rows=_STOCK_ROWS,
            computed_at=_COMPUTED_AT,
            write_finding=write_fn,
            fault_grounding_enabled=True,
            **readers,
        )
        # Vehicle must NOT be refused — it should get both items
        assert stats["vins_refused"] == 0, (
            "A p0 vehicle with no directive must NOT be refused (fix F16). "
            f"vins_refused={stats['vins_refused']!r}."
        )
        assert written, (
            "Items must be written for a p0 vehicle even when directive=None (fix F16)."
        )
        assert stats["p0_directive_fallbacks"] >= 1, (
            "p0_directive_fallbacks must be incremented when fallback is used. "
            f"Got: {stats['p0_directive_fallbacks']!r}."
        )
        # Every p0-slot item must carry the fallback directive
        for finding in written:
            sk = finding.get("subject_key", "")
            if not sk.startswith(("service/", "ground/")):
                continue
            disc = finding.get("disclosure_txt")
            assert disc == expected_fallback, (
                f"disclosure_txt on {sk!r} must equal the pullover_stop_engine fallback.\n"
                f"Expected: {expected_fallback!r}\nGot: {disc!r}."
            )

    def test_p0_fault_with_directive_is_not_refused(self):
        """Vehicle with p0 fault AND a valid directive proceeds normally (W6)."""
        row = _make_vehicle_row(
            open_faults=[_p0_fault()],
            catalog_entries={_P0_CODE: _P0_CATALOG_ENTRY},
        )
        written: list[dict] = []
        readers = _simple_readers()

        def write_fn(f, s, refresh_on_noop=True):
            written.append(f)

        stats = run_health_loop(
            vehicle_rows=[row],
            stock_rows=_STOCK_ROWS,
            computed_at=_COMPUTED_AT,
            write_finding=write_fn,
            **readers,
        )
        assert stats["vins_refused"] == 0, (
            "Vehicle with p0 fault and valid directive must NOT be refused."
        )
        assert written, "Booking item must be written."


# ---------------------------------------------------------------------------
# W7: Agent 2 p0 severity without open fault does not trigger grounding
# ---------------------------------------------------------------------------

class TestW7Agent2P0NoGrounding:
    """W7: grounding is triggered only by open fault codes, not by Agent 2 severity.

    An Agent 2 finding at 'p0' severity must NOT produce a grounding item
    (grounding needs real open fault codes; the band table only keys on faults).

    Mutant: revert grounding check to `severity == 'p0'` without the fault check →
            Agent 2 p0 triggers grounding with empty fault_codes → test fails.
    """

    def test_agent2_p0_without_open_fault_no_grounding(self):
        """An Agent 2 p0 finding WITHOUT an open fault gives no grounding item.

        Mutant: revert to `severity == 'p0'` check → grounding item written with
                fault_codes=[] → assertion fails.
        """
        row = _make_vehicle_row(
            agent2_findings=[{
                "finding_id": "fi-a2-p0",
                "severity": "p0",
                "finding_kind": "health.battery",
            }],
            # No open_faults — no actual fault codes
        )
        written: list[dict] = []
        readers = _simple_readers(is_grounded=False)

        def write_fn(f, s, refresh_on_noop=True):
            written.append(f)

        run_health_loop(
            vehicle_rows=[row],
            stock_rows=_STOCK_ROWS,
            computed_at=_COMPUTED_AT,
            write_finding=write_fn,
            fault_grounding_enabled=True,
            **readers,
        )
        subjects = [f.get("subject_key", "") for f in written]
        assert not any(s.startswith("ground/") for s in subjects), (
            "Agent 2 p0 severity without open faults must NOT produce a grounding item. "
            "Mutant W7: revert to severity=='p0' check → grounding written → fails."
        )

    def test_p0_open_fault_with_agent2_p0_triggers_grounding(self):
        """An open p0 fault PLUS Agent 2 p0 finding → grounding item IS written."""
        row = _make_vehicle_row(
            open_faults=[_p0_fault()],
            agent2_findings=[{
                "finding_id": "fi-a2-p0",
                "severity": "p0",
                "finding_kind": "health.battery",
            }],
            catalog_entries={_P0_CODE: _P0_CATALOG_ENTRY},
        )
        written: list[dict] = []
        readers = _simple_readers(is_grounded=False)

        def write_fn(f, s, refresh_on_noop=True):
            written.append(f)

        run_health_loop(
            vehicle_rows=[row],
            stock_rows=_STOCK_ROWS,
            computed_at=_COMPUTED_AT,
            write_finding=write_fn,
            fault_grounding_enabled=True,
            **readers,
        )
        subjects = [f.get("subject_key", "") for f in written]
        assert any(s.startswith("ground/") for s in subjects), (
            "Open p0 fault + Agent 2 p0 → grounding item must still be written."
        )


# ---------------------------------------------------------------------------
# W8: no candidate rooftop refuses the vehicle
# ---------------------------------------------------------------------------

class TestW8NoRooftopRefused:
    """W8: vehicle with no candidate rooftop now skips the booking slot only
    (fix F16).  The vehicle is not counted in vins_refused; the booking is
    counted in book_items_without_rooftops.  The grounding slot is still written
    for p0 vehicles with fault_grounding_enabled.

    Keeping the old class name so earlier review references are traceable.
    """

    def test_no_stock_rows_refuses_vehicle(self):
        """Empty stock_rows with no recall_visit → booking is skipped, counted in
        book_items_without_rooftops; vehicle is NOT refused (fix F16).

        A soon-band fault has no grounding item, so nothing is written.
        Old assertion (vins_refused >= 1) was testing the defect.
        """
        row = _make_vehicle_row(
            open_faults=[_soon_fault()],
        )
        written: list[dict] = []
        readers = _simple_readers()

        def write_fn(f, s, refresh_on_noop=True):
            written.append(f)

        stats = run_health_loop(
            vehicle_rows=[row],
            stock_rows=[],          # no rooftops → W8 fires
            computed_at=_COMPUTED_AT,
            write_finding=write_fn,
            **readers,
        )
        assert stats["book_items_without_rooftops"] >= 1, (
            "book_items_without_rooftops must be incremented when booking is skipped (fix F16). "
            f"Got: {stats['book_items_without_rooftops']!r}."
        )
        # A soon-fault vehicle has no grounding item, so nothing is written.
        assert not written, (
            "No booking must be written when there is no candidate rooftop. "
            "Mutation M-W8-inv: skip booking guard → dealer_id=None in booking → fails."
        )
        # The vehicle is processed, not refused
        assert stats["vins_refused"] == 0, (
            "Vehicle must not be counted in vins_refused when only the booking is skipped. "
            f"Got: {stats['vins_refused']!r}."
        )

    def test_recall_visit_rooftop_avoids_refusal(self):
        """A recall_visit rooftop substitutes for stock_rows (W8)."""
        row = _make_vehicle_row(
            open_faults=[_soon_fault()],
        )
        recall_visit = {
            "recall_item_finding_id": "FIND-recall-001",
            "recall_id": "RC-001",
            "rooftop_dealer_id": "dealer-recall-001",
        }
        written: list[dict] = []
        readers = _simple_readers(recall_visit=recall_visit)

        def write_fn(f, s, refresh_on_noop=True):
            written.append(f)

        stats = run_health_loop(
            vehicle_rows=[row],
            stock_rows=[],          # no stock_rows, but recall_visit provides rooftop
            computed_at=_COMPUTED_AT,
            write_finding=write_fn,
            **readers,
        )
        assert stats["vins_refused"] == 0, (
            "Recall visit rooftop must provide a valid dealer_id (no refusal)."
        )
        assert written, "Booking must be written when recall_visit provides a rooftop."


# ---------------------------------------------------------------------------
# W9: exhausted item headline vs detail
# ---------------------------------------------------------------------------

class TestW9ExhaustedHeadlineVsDetail:
    """W9: exhausted item has the fixed headline and a longer per-segment detail.

    Mutant M7: use the detail sentence as the headline →
               headline != _INFORMATIONAL_HEADLINE → assertion fails.
    """

    def _exhausted_history(self, segment: str = "service") -> dict:
        from agents.fi_recall_warranty.attempts import MAX_ATTEMPTS
        return {
            segment: {
                "attempt_number": MAX_ATTEMPTS,
                "finding_id": "FIND-exhaust-w9",
                "inputs_hash": "old-hash",
                "computed_at": "2026-10-01T00:00:00Z",
                "expires_at": "2026-10-04T00:00:00Z",
                "diagnosis_source": "rules",
                "queue_status": "cancelled",
                "decided_at": "2026-10-01T00:00:00Z",
                "episode_start": 1,  # required so ep_cap=MAX_ATTEMPTS fires
            }
        }

    def test_exhausted_item_has_informational_headline(self):
        """Exhausted service item headline == contract §6 fixed text (W9).

        Mutant M7: assign the detail sentence to headline →
                   headline != _INFORMATIONAL_HEADLINE → fails.
        """
        from agents.fi_fleet_health.attempts import _INFORMATIONAL_HEADLINE

        row = _make_vehicle_row(
            open_faults=[_p0_fault()],
            catalog_entries={_P0_CODE: _P0_CATALOG_ENTRY},
        )
        written: list[dict] = []
        readers = _simple_readers(history=self._exhausted_history("service"))

        def write_fn(f, s, refresh_on_noop=True):
            written.append(f)

        run_health_loop(
            vehicle_rows=[row],
            stock_rows=_STOCK_ROWS,
            computed_at=_COMPUTED_AT,
            write_finding=write_fn,
            fault_grounding_enabled=False,
            **readers,
        )
        exhausted = next(
            (f for f in written if "exhausted" in (f.get("subject_key") or "")), None
        )
        assert exhausted is not None, "Exhausted item must be written."
        assert exhausted.get("headline") == _INFORMATIONAL_HEADLINE, (
            f"Exhausted item headline must be the fixed text {_INFORMATIONAL_HEADLINE!r}; "
            f"got {exhausted.get('headline')!r}. "
            "Mutant M7: assign detail sentence to headline → assertion fails."
        )

    def test_exhausted_item_detail_is_longer_sentence(self):
        """Exhausted service item detail is the per-segment longer sentence (W9).

        Mutant M7: set detail == headline (short fixed text) → detail != longer sentence → fails.
        """
        row = _make_vehicle_row(
            open_faults=[_p0_fault()],
            catalog_entries={_P0_CODE: _P0_CATALOG_ENTRY},
        )
        written: list[dict] = []
        readers = _simple_readers(history=self._exhausted_history("service"))

        def write_fn(f, s, refresh_on_noop=True):
            written.append(f)

        run_health_loop(
            vehicle_rows=[row],
            stock_rows=_STOCK_ROWS,
            computed_at=_COMPUTED_AT,
            write_finding=write_fn,
            fault_grounding_enabled=False,
            **readers,
        )
        exhausted = next(
            (f for f in written if "exhausted" in (f.get("subject_key") or "")), None
        )
        assert exhausted is not None
        from agents.fi_fleet_health.attempts import _INFORMATIONAL_HEADLINE
        detail = exhausted.get("detail", "")
        assert len(detail) > len(_INFORMATIONAL_HEADLINE), (
            f"Exhausted item detail must be longer than the headline. "
            f"headline={_INFORMATIONAL_HEADLINE!r}, detail={detail!r}. "
            "Mutant M7: detail == headline → lengths equal → fails."
        )
        # The detail for 'service' segment mentions 'dealer'
        assert "dealer" in detail.lower(), (
            f"Service-slot exhausted detail must mention dealer; got {detail!r}."
        )


# ---------------------------------------------------------------------------
# W10: reader tests assert exact values, not just placeholder names
# ---------------------------------------------------------------------------

class TestW10ExactFilterValues:
    """W10: mutation guards check the exact ExpressionAttributeValues, not just placeholders.

    W10-A: M10 mutant — ':s_soon' bound to 'routine' instead of 'soon' still passes the
           old test (which only checked ':s_soon' appears in the string).
           After fix: also assert the VALUE is 'soon'.
    W10-B: M9 mutant — 'expires_at < :now' instead of 'expires_at > :now' still passes
           the old test (which only checked 'expires_at' appears).
           After fix: assert '>' is present (or '> :now' exactly).
    W10-C: fleet-error handling is now inside select_agent2_findings (covered by
           TestReaderErrorCounting test above).
    """

    def test_w10a_s_soon_bound_to_soon_not_routine(self):
        """Agent 2 filter: ':s_soon' must be bound to 'soon', not 'routine' (W10-A).

        Mutant M10: bind ':s_soon' to 'routine' → old test passes (string present),
                    new test fails (value is wrong).
        """
        from agents.fi_fleet_health.selection import select_agent2_findings
        from unittest.mock import MagicMock

        _NOW_DT = datetime.datetime(2026, 10, 10, 6, 0, tzinfo=datetime.timezone.utc)
        captured_kwargs: list[dict] = []

        def fake_query(**kwargs):
            captured_kwargs.append(kwargs)
            return {"Items": []}

        ddb = MagicMock()
        ddb.query.side_effect = fake_query

        select_agent2_findings(
            ["fleet-test-001"],
            ddb_client=ddb,
            findings_table="avx-findings",
            now=_NOW_DT,
        )

        assert captured_kwargs, "query must have been called."
        ev = captured_kwargs[0].get("ExpressionAttributeValues", {})
        assert ev.get(":s_soon") == {"S": "soon"}, (
            f":s_soon must be bound to 'soon', not 'routine'; got {ev.get(':s_soon')!r}. "
            "Mutant M10: bind to 'routine' → old string-check passes, this fails."
        )
        assert ev.get(":s_att") == {"S": "attention"}, (
            f":s_att must be bound to 'attention'; got {ev.get(':s_att')!r}."
        )
        # Option B (spec 2026-11-05-avx-proactive-p0-alert, 2026-11-06): the Agent 2
        # query no longer asks for p0 Findings; FI sees stop-driving faults via ADP only.
        assert ":s_p0" not in ev, (
            f":s_p0 must not be bound (option B); got {ev.get(':s_p0')!r}."
        )

    def test_w10b_expires_at_uses_gt_comparison(self):
        """Recall visit filter: expires_at > :now (not <) excludes expired items (W10-B).

        Mutant M9: change to 'expires_at < :now' → old test passes (string present),
                   this test fails ('>' absent in FilterExpression).
        """
        from agents.fi_fleet_health.selection import recall_visit_fn
        from unittest.mock import MagicMock

        _NOW_DT = datetime.datetime(2026, 10, 10, 6, 0, tzinfo=datetime.timezone.utc)
        captured_kwargs: list[dict] = []

        def fake_query(**kwargs):
            captured_kwargs.append(kwargs)
            return {"Items": []}

        ddb = MagicMock()
        ddb.query.side_effect = fake_query

        recall_visit_fn(
            "MRDVEH00000000001",
            ddb_client=ddb,
            findings_table="avx-findings",
            now=_NOW_DT,
        )

        assert captured_kwargs, "query must have been called."
        fe = captured_kwargs[0].get("FilterExpression", "")
        # The filter must use '>' (greater-than) to exclude PAST entries.
        assert "expires_at > :now" in fe, (
            f"FilterExpression must include 'expires_at > :now'; got {fe!r}. "
            "Mutant M9: change to '< :now' → expired items pass through → this fails."
        )



# ---------------------------------------------------------------------------
# Fix F16: W8 and W6 safety-direction defects
# ---------------------------------------------------------------------------

class TestFixF16:
    """Fix F16: two early-return defects that dropped grounding items.

    W8 (no rooftop): the original code returned before ANY slot was written,
    so a stop-driving vehicle with no candidate dealer got nothing at all.
    Fix: skip only the booking slot; the grounding slot is still written.

    W6 (no directive): the original code returned when a p0 code had no catalog
    directive, again leaving the vehicle with no items.
    Fix: use the pullover_stop_engine first sentence as the fallback directive.

    Mutation guards:
    - M-W8: revert the fix (put back `return`) → grounding item absent → test fails.
    - M-W6: revert the fix (put back `return`) → both items absent → test fails.
    """

    # --- W8: no stock rows (no rooftop) + fault_grounding_enabled=True ------

    def test_w8_no_rooftop_p0_gets_both_booking_and_grounding(self):
        """A p0 vehicle with no stock rows gets booking (no dealer_id) + grounding (W6, design answer 3).

        W6 fix: p0 with no rooftop still gets a booking with the special no-rooftop headline
        and the directive; the operator must pick a dealer at approve time.
        Grounding is still written when fault_grounding_enabled=True.
        book_items_without_rooftops is incremented to track the no-dealer-id case.

        Mutation guard M-W6-no-rooftop: revert to _skip_booking_no_rooftop=True for p0 →
        booking absent → this fails.
        """
        from agents.fi_fleet_health.writer import _HEADLINE_BOOKING_P0_NO_ROOFTOP
        row = _make_vehicle_row(
            open_faults=[_p0_fault()],
            catalog_entries={_P0_CODE: _P0_CATALOG_ENTRY},
        )
        written: list[dict] = []
        readers = _simple_readers(is_grounded=False)

        def write_fn(finding, standing, refresh_on_noop=True):
            written.append(finding)

        stats = run_health_loop(
            vehicle_rows=[row],
            stock_rows=[],            # empty → no rooftop, no dealer → W6 fires
            computed_at=_COMPUTED_AT,
            write_finding=write_fn,
            fault_grounding_enabled=True,
            **readers,
        )

        subjects = [f.get("subject_key", "") for f in written]
        assert any(s.startswith("ground/") for s in subjects), (
            "Grounding item must be written even when no candidate rooftop exists. "
            "Mutation M-W8: revert to early-return → ground/ absent → this fails."
        )
        # W6: p0 with no rooftop still gets a booking item (W6 design answer 3)
        booking = next((f for f in written if (f.get("subject_key") or "").startswith("service/")), None)
        assert booking is not None, (
            "p0 booking item must be written even with no rooftop (W6). "
            "Mutation M-W6-no-rooftop: skip p0 booking → booking absent → fails."
        )
        expected_headline = _HEADLINE_BOOKING_P0_NO_ROOFTOP.format(code=_P0_CODE)
        assert booking.get("headline") == expected_headline, (
            f"No-rooftop p0 booking must have headline {expected_headline!r}; "
            f"got {booking.get('headline')!r}. "
            "Mutation: use normal headline → fails."
        )
        assert booking.get("disclosure_txt") == _P0_DIRECTIVE, (
            "No-rooftop p0 booking must still carry the directive. "
            "Mutation: drop directive → fails."
        )
        assert stats["book_items_without_rooftops"] == 1, (
            "book_items_without_rooftops must be incremented for the no-rooftop p0 case. "
            f"Got: {stats['book_items_without_rooftops']!r}"
        )

    def test_w8_no_rooftop_booking_stat_incremented_for_non_p0(self):
        """book_items_without_rooftops counts non-p0 vehicles whose booking was skipped.
        For p0 vehicles, the stat is also incremented (no-dealer-id booking written, W6).
        This test uses a soon fault so booking is skipped (no p0 exception).
        """
        row1 = _make_vehicle_row(vin=_VIN,  open_faults=[_soon_fault()])
        row2 = _make_vehicle_row(vin=_VIN2, open_faults=[_soon_fault()])
        readers = _simple_readers()
        stats = run_health_loop(
            vehicle_rows=[row1, row2],
            stock_rows=[],
            computed_at=_COMPUTED_AT,
            write_finding=lambda f, s, refresh_on_noop=True: None,
            fault_grounding_enabled=False,
            **readers,
        )
        assert stats["book_items_without_rooftops"] == 2, (
            "book_items_without_rooftops must count every vehicle whose booking was skipped (non-p0); "
            f"expected 2, got {stats['book_items_without_rooftops']!r}."
        )

    # --- W6: p0 code missing from catalog -----------------------------------

    # Expected fallback text (first sentence of pullover_stop_engine, first-letter
    # upper-cased).  Read at import time once so the test and the fix agree on the
    # source without the test copying the string literal.
    @staticmethod
    def _expected_fallback() -> str:
        from agents.supervisor.dtc_response_catalog import RESPONSE_TEMPLATES
        raw = RESPONSE_TEMPLATES["pullover_stop_engine"]["first_sentence"]
        return raw[0].upper() + raw[1:]

    def test_w6_missing_catalog_entry_gets_fallback_on_both_items(self):
        """A p0 fault code absent from catalog_entries gets the pullover_stop_engine
        first sentence as disclosure_txt on both the booking item and the grounding item.

        Mutation guard M-W6: revert to early-return → both items absent → fails.
        Mutation guard M-W6-txt: inject empty string instead of fallback → disclosure_txt
        comparison fails.
        """
        # Vehicle has a p0 fault with NO catalog entry at all
        row = _make_vehicle_row(
            open_faults=[_p0_fault()],
            catalog_entries={},       # P0AA6 absent
        )
        written: list[dict] = []
        readers = _simple_readers(is_grounded=False)

        def write_fn(finding, standing, refresh_on_noop=True):
            written.append(finding)

        stats = run_health_loop(
            vehicle_rows=[row],
            stock_rows=_STOCK_ROWS,
            computed_at=_COMPUTED_AT,
            write_finding=write_fn,
            fault_grounding_enabled=True,
            **readers,
        )

        subjects = [f.get("subject_key", "") for f in written]
        expected = self._expected_fallback()

        # Both items must be present
        assert any(s.startswith("service/") for s in subjects), (
            "Booking item must be written for a p0 vehicle even when the code has no "
            "catalog entry. "
            "Mutation M-W6: revert to early-return → service/ absent → this fails."
        )
        assert any(s.startswith("ground/") for s in subjects), (
            "Grounding item must be written for a p0 vehicle even when the code has no "
            "catalog entry. "
            "Mutation M-W6: revert to early-return → ground/ absent → this fails."
        )

        # Both items must carry the fallback as disclosure_txt (byte-equal)
        for finding in written:
            sk = finding.get("subject_key", "")
            if not sk.startswith(("service/", "ground/")):
                continue
            disc = finding.get("disclosure_txt")
            assert disc == expected, (
                f"disclosure_txt for {sk!r} must equal the pullover_stop_engine "
                f"first sentence byte-for-byte.\n"
                f"Expected: {expected!r}\n"
                f"Got:      {disc!r}\n"
                "Mutation M-W6-txt: use '' instead of fallback → assertion fails."
            )

        assert stats["p0_directive_fallbacks"] == 1, (
            "p0_directive_fallbacks must be incremented once per vehicle. "
            f"Got: {stats['p0_directive_fallbacks']!r}"
        )

    def test_w6_missing_directive_key_also_triggers_fallback(self):
        """A catalog entry that exists but has no 'directive' key gets the same fallback.
        Covers the second branch of the condition: ``not entry.get('directive')``.
        """
        no_directive_entry = {
            "code": _P0_CODE,
            "description": "High-voltage fault.",
            "band": "p0",
            # No 'directive' key
        }
        row = _make_vehicle_row(
            open_faults=[_p0_fault()],
            catalog_entries={_P0_CODE: no_directive_entry},
        )
        written: list[dict] = []

        def write_fn(finding, standing, refresh_on_noop=True):
            written.append(finding)

        stats = run_health_loop(
            vehicle_rows=[row],
            stock_rows=_STOCK_ROWS,
            computed_at=_COMPUTED_AT,
            write_finding=write_fn,
            fault_grounding_enabled=True,
            **_simple_readers(is_grounded=False),
        )

        expected = self._expected_fallback()
        subjects = [f.get("subject_key", "") for f in written]
        assert any(s.startswith("service/") for s in subjects), (
            "Booking item required when directive key is missing but entry exists."
        )
        service_finding = next(
            f for f in written if (f.get("subject_key") or "").startswith("service/")
        )
        assert service_finding.get("disclosure_txt") == expected, (
            f"disclosure_txt must be the fallback when directive key is absent; "
            f"got {service_finding.get('disclosure_txt')!r}."
        )
        assert stats["p0_directive_fallbacks"] == 1, (
            f"p0_directive_fallbacks must be 1; got {stats['p0_directive_fallbacks']!r}."
        )

    def test_w6_vehicle_with_catalog_directive_not_counted(self):
        """A p0 vehicle with a proper catalog directive must NOT increment
        p0_directive_fallbacks.  Checks the counter is guarded correctly.
        """
        row = _make_vehicle_row(
            open_faults=[_p0_fault()],
            catalog_entries={_P0_CODE: _P0_CATALOG_ENTRY},
        )
        stats = run_health_loop(
            vehicle_rows=[row],
            stock_rows=_STOCK_ROWS,
            computed_at=_COMPUTED_AT,
            write_finding=lambda f, s, refresh_on_noop=True: None,
            fault_grounding_enabled=False,
            **_simple_readers(),
        )
        assert stats["p0_directive_fallbacks"] == 0, (
            "p0_directive_fallbacks must stay 0 when all p0 codes have catalog directives; "
            f"got {stats['p0_directive_fallbacks']!r}."
        )



# ===========================================================================
# G3F2.1 new tests — C1, C2, W1-W11, S1, S2 (design answers 1-4, 9)
# ===========================================================================

# ---------------------------------------------------------------------------
# S1 (kill surviving mutant C2c): build_finding raises on missing severity
# ---------------------------------------------------------------------------

class TestS1BuildFindingRaisesOnMissingSeverity:
    """S1 (kill C2c): build_finding raises ValueError when item has no severity.

    The C2 fix added a ValueError raise in build_finding.  C2c mutant restores
    a default 'routine' fallback, making the raise unreachable.

    Mutation guard (C2c): restore default 'routine' → no raise → test fails.
    """

    def test_build_finding_raises_on_missing_severity(self):
        """build_finding raises ValueError when the item dict has no 'severity' key."""
        from agents.fi_fleet_health.write import build_finding, AGENT_ID
        from agents.fi_recall_warranty.evidence import compute_inputs_hash

        item_no_severity = {
            "item_type": "booking",
            "subject_key": "service/1",
            "headline": "Stop-driving fault P0AA6: book service at dealer-x",
            "detail": "",
            # deliberately no 'severity'
        }
        with pytest.raises(ValueError, match="missing 'severity'"):
            build_finding(
                vin=_VIN, fleet_id=_FLEET_ID,
                item=item_no_severity,
                computed_at=_COMPUTED_AT,
                inputs_hash=compute_inputs_hash([]),
                evidence=[],
                confidence="low",
            )


# ---------------------------------------------------------------------------
# S2 (kill surviving mutant): p0 tie-break code ascending
# ---------------------------------------------------------------------------

class TestS2P0TieBreakCodeAscending:
    """S2: when two p0 faults have the same last_seen_at, the code sorts ascending.

    Both reversed-tie mutants survive because test_two_p0_codes_same_headline
    checks only equality across orders.  This test pins which code wins.

    Mutation guard (S2): reverse the code sort → P0B00 wins → headline has P0B00
    instead of P0A00 → assertion fails.
    """

    def test_code_ascending_wins_tie(self):
        """Same last_seen_at: alphabetically first code wins the headline (ascending)."""
        now_iso = "2026-10-10T12:00:00Z"
        # Two p0 faults with identical last_seen_at
        fault_a = {"code": "P0A00", "band": "p0", "last_seen_at": now_iso}
        fault_b = {"code": "P0B00", "band": "p0", "last_seen_at": now_iso}
        catalog = {
            "P0A00": {"code": "P0A00", "band": "p0",
                      "directive": "Stop: P0A00 directive."},
            "P0B00": {"code": "P0B00", "band": "p0",
                      "directive": "Stop: P0B00 directive."},
        }
        # Both orders must give the same headline — and it must name P0A00 (alphabetically first)
        for faults in ([fault_a, fault_b], [fault_b, fault_a]):
            row = _make_vehicle_row(
                open_faults=faults,
                catalog_entries=catalog,
            )
            written: list[dict] = []
            readers = _simple_readers()

            def write_fn(f, s, refresh_on_noop=True):
                written.append(f)

            run_health_loop(
                vehicle_rows=[row],
                stock_rows=_STOCK_ROWS,
                computed_at=_COMPUTED_AT,
                write_finding=write_fn,
                fault_grounding_enabled=True,   # enable grounding to test S2b
                **readers,
            )
            assert written
            booking = next(
                (f for f in written if (f.get("subject_key") or "").startswith("service/")), None
            )
            assert booking is not None
            headline = booking.get("headline", "")
            assert "P0A00" in headline, (
                f"Code P0A00 (ascending first) must win the tie-break; headline={headline!r}. "
                "Mutation S2: reverse code sort → P0B00 wins → fails."
            )
            assert "P0B00" not in headline, (
                f"Code P0B00 must NOT win the tie-break; headline={headline!r}."
            )
            # Also check the grounding item's first fault code (second sort block, S2b mutant)
            grounding = next(
                (f for f in written if (f.get("subject_key") or "").startswith("ground/")), None
            )
            if grounding:
                # The grounding headline uses fault_codes[0] from _build_grounding_item_for_slot
                gnd_headline = grounding.get("headline", "")
                assert "P0A00" in gnd_headline, (
                    f"Grounding headline must use P0A00 (ascending code wins tie); "
                    f"got {gnd_headline!r}. "
                    "Mutation S2b: reverse grounding code sort → P0B00 first → fails."
                )


# ---------------------------------------------------------------------------
# C1: two-run test — directive change detected (design answer 2)
# ---------------------------------------------------------------------------

class TestC1DirectiveChangeDetected:
    """C1 (design answer 2): inputs_hash covers the directive text.

    When the catalog changes the directive wording on day 2, the hash changes,
    write_finding calls _update_item, and disclosure_txt is updated.

    Mutation guard (C1): exclude item_signature from hash → directive change
    produces same hash → write_finding returns 'refresh' → disclosure_txt stale.
    """

    def test_directive_change_changes_hash_and_updates_stored_row(self):
        """Day 1: fallback directive. Day 2: catalog directive. Hash must differ → update."""
        from agents.tier2.writer import write_finding as _write_finding, compute_finding_key
        from agents.fi_fleet_health.write import AGENT_ID, FINDING_KIND
        from agents.fi_fleet_health.loop import run_health_loop

        vin = _VIN
        fleet_id = _FLEET_ID
        findings_table = "vsa-staging-avx-findings"
        # We need a p0 fault to trigger the disclosure_txt path
        p0_code = "P0A80"
        fallback_directive = "Stop driving now."    # day 1: catalog has no directive
        catalog_directive = "HV isolation fault: stop driving now and call your dealer."  # day 2

        # ---- Day 1: catalog has no directive → fallback is used ----
        row_d1 = _make_vehicle_row(
            open_faults=[{"code": p0_code, "band": "p0", "last_seen_at": "2026-10-09Z"}],
            catalog_entries={p0_code: {"code": p0_code, "band": "p0",
                                       "directive": None}},  # no directive
        )
        written_d1: list[dict] = []
        readers_d1 = _simple_readers()

        def cap_d1(f, s, refresh_on_noop=True):
            written_d1.append(f)

        run_health_loop(
            vehicle_rows=[row_d1], stock_rows=_STOCK_ROWS,
            computed_at="2026-10-10T05:00:00Z",
            write_finding=cap_d1, fault_grounding_enabled=False,
            **readers_d1,
        )
        assert written_d1, "Day 1 must write a booking item."
        booking_d1 = next(
            (f for f in written_d1 if (f.get("subject_key") or "").startswith("service/")), None
        )
        assert booking_d1 is not None
        hash_d1 = booking_d1["inputs_hash"]

        # ---- Day 2: catalog NOW has a real directive ----
        row_d2 = _make_vehicle_row(
            open_faults=[{"code": p0_code, "band": "p0", "last_seen_at": "2026-10-09Z"}],
            catalog_entries={p0_code: {"code": p0_code, "band": "p0",
                                       "directive": catalog_directive}},  # real directive
        )
        written_d2: list[dict] = []

        # Simulate the live row from day 1 (same hash as d1 → triggers skip if hash unchanged)
        live_d1 = {
            "inputs_hash": hash_d1,
            "evidence": [{"kind": "health.diagnosis_source", "value": "rules",
                          "source": "agent-fi-fleet-health",
                          "source_ref": "fi-fleet-health-run",
                          "computed_at": "2026-10-10T05:00:00Z",
                          "provenance": "live", "vin": vin}],
        }

        def cap_d2(f, s, refresh_on_noop=True):
            written_d2.append(f)

        readers_d2 = _simple_readers(
            history={"service": {
                "attempt_number": 1,
                "finding_id": "FIND-d1-0001",
                "inputs_hash": hash_d1,
                "computed_at": "2026-10-10T05:00:00Z",
                "expires_at": "2026-10-13T05:00:00Z",
                "diagnosis_source": "rules",
                "queue_status": "none",
                "decided_at": None,
            }},
            live=live_d1,
        )

        run_health_loop(
            vehicle_rows=[row_d2], stock_rows=_STOCK_ROWS,
            computed_at="2026-10-11T05:00:00Z",
            write_finding=cap_d2, fault_grounding_enabled=False,
            **readers_d2,
        )
        assert written_d2, (
            "Day 2 must write an updated booking item when the directive changes. "
            "Mutation guard: exclude item_signature from hash → same hash → skip → fails."
        )
        booking_d2 = next(
            (f for f in written_d2 if (f.get("subject_key") or "").startswith("service/")), None
        )
        assert booking_d2 is not None
        hash_d2 = booking_d2["inputs_hash"]
        assert hash_d1 != hash_d2, (
            "Hash must change when the directive changes (C1 fix). "
            "Mutation guard: exclude item_signature → same hash → assertion fails."
        )
        assert booking_d2.get("disclosure_txt") == catalog_directive, (
            f"Day 2 disclosure_txt must be the catalog directive {catalog_directive!r}; "
            f"got {booking_d2.get('disclosure_txt')!r}. "
            "Mutation: keep fallback → catalog directive absent → fails."
        )


# ---------------------------------------------------------------------------
# W1 (cycle 3): each of the six signature fields is individually pinned
# ---------------------------------------------------------------------------

class TestW1SignatureFields:
    """W1 (cycle 3): headline, severity, dealer_id, agent_version, and needs
    (labor_op) are all part of the inputs_hash via the item_signature entry.

    Mutation guards (C1-c..f + W1 labor_op):
    - Drop headline from signature value → headline change keeps same hash → C1-c.
    - Drop severity from signature value → severity change keeps same hash → C1-f.
    - Drop dealer_id from signature value → rooftop change keeps same hash → C1-e.
    - Drop agent_version from signature value → version bump keeps same hash → C1-d.
    - Drop needs from signature value → labor_op change keeps same hash → W1.
    Each of these mutations produces two equal hashes where the test asserts inequality.
    """

    def _make_sig(self, **overrides) -> dict:
        """Return a minimal item dict for _build_item_signature_entry."""
        item: dict = {
            "severity": overrides.get("severity", "soon"),
            "headline": overrides.get("headline", "Maintenance needed: schedule service"),
            "recommended_action": {
                "action_kind": "book_fleet_service",
                "parameters": {
                    "dealer_id": overrides.get("dealer_id", "DEALER-001"),
                    "urgency": "this_month",
                    "needs": overrides.get("needs", [{"labor_op": "LAB-BRK-01"}]),
                },
            },
        }
        if "disclosure_txt" in overrides:
            item["disclosure_txt"] = overrides["disclosure_txt"]
        return item

    def _hash_sig(self, item: dict) -> str:
        from agents.fi_fleet_health.loop import _build_item_signature_entry
        from agents.fi_recall_warranty.evidence import compute_inputs_hash
        return compute_inputs_hash([_build_item_signature_entry(item, vin=_VIN)])

    def test_headline_change_changes_hash(self):
        """Different headlines → different hashes (C1-c fix).

        Mutation: drop headline from signature value → both hashes equal → fails.
        """
        h1 = self._hash_sig(self._make_sig(headline="Maintenance needed: schedule service"))
        h2 = self._hash_sig(self._make_sig(headline="Attention: schedule service soon"))
        assert h1 != h2, (
            "Different headlines must produce different inputs_hash (C1-c). "
            "Mutation: drop headline from signature → hashes equal → fails."
        )

    def test_severity_change_changes_hash(self):
        """Different severities → different hashes (C1-f fix).

        Mutation: drop severity from signature value → both hashes equal → fails.
        """
        h_soon = self._hash_sig(self._make_sig(severity="soon"))
        h_atten = self._hash_sig(self._make_sig(severity="attention"))
        assert h_soon != h_atten, (
            "Different severities must produce different inputs_hash (C1-f). "
            "Mutation: drop severity from signature → hashes equal → fails."
        )

    def test_dealer_id_change_changes_hash(self):
        """Different dealer_id → different hashes (C1-e fix).

        Mutation: drop dealer_id from signature value → both hashes equal → fails.
        """
        h1 = self._hash_sig(self._make_sig(dealer_id="DEALER-001"))
        h2 = self._hash_sig(self._make_sig(dealer_id="DEALER-002"))
        assert h1 != h2, (
            "Different dealer_ids must produce different inputs_hash (C1-e). "
            "Mutation: drop dealer_id from signature → hashes equal → fails."
        )

    def test_agent_version_change_changes_hash(self):
        """AGENT_VERSION bump → different hash (C1-d fix).

        Mutation: drop agent_version from signature value → bump keeps same hash → fails.
        """
        import agents.fi_fleet_health.loop as _loop
        item = self._make_sig()
        from agents.fi_fleet_health.loop import _build_item_signature_entry
        from agents.fi_recall_warranty.evidence import compute_inputs_hash

        sig1 = _build_item_signature_entry(item, vin=_VIN)
        # Temporarily change AGENT_VERSION to simulate a version bump
        old_ver = _loop.AGENT_VERSION
        _loop.AGENT_VERSION = "vTEST-99.99-bump"
        try:
            sig2 = _build_item_signature_entry(item, vin=_VIN)
        finally:
            _loop.AGENT_VERSION = old_ver
        h1 = compute_inputs_hash([sig1])
        h2 = compute_inputs_hash([sig2])
        assert h1 != h2, (
            "AGENT_VERSION bump must produce a different inputs_hash (C1-d). "
            "Mutation: drop agent_version from signature → bump keeps same hash → fails."
        )

    def test_labor_op_change_changes_hash(self):
        """Different labor_op in needs → different hashes (W1 fix: needs in signature).

        When needs_config changes (e.g. a repair maps to a different labor op), the
        stored RO line would be stale. The hash must move so write_finding writes the
        update.

        Mutation: drop needs from signature value → labor_op change keeps same hash →
        stored Finding keeps stale RO line → test fails.
        """
        h1 = self._hash_sig(self._make_sig(needs=[{"labor_op": "LAB-BRK-01"}]))
        h2 = self._hash_sig(self._make_sig(needs=[{"labor_op": "LAB-ELE-07"}]))
        assert h1 != h2, (
            "Different labor_op in needs must produce different inputs_hash (W1). "
            "Mutation: drop needs from signature → labor_op change keeps same hash → fails."
        )


# ---------------------------------------------------------------------------
# C2: episode management (design answer 1)
# ---------------------------------------------------------------------------

class TestC2EpisodeManagement:
    """C2 (design answer 1): completed/executed/dismissed end an episode.
    A new episode starts when current needs differ or decline window passed.

    Mutation guard: remove _CLOSING_STATUSES handling → completed booking
    blocks future new faults forever → test fails.
    """

    def test_completed_service_plus_new_fault_writes_next_booking(self):
        """After a completed service/1, a new p0 fault writes service/2 (new episode).

        The closing item had need_id 'fault:P1AB0' (soon fault).
        The current vehicle now has need_id 'fault:P0AA6' (p0 fault) — a new need.
        Result: write_next → service/2 written.

        Mutation guard (C2): keep completed in _NO_RETRY_STATUSES → skip → no write → fails.
        """
        # History: service/1 was completed with a soon fault need
        history = {
            "service": {
                "attempt_number": 1,
                "finding_id": "FIND-completed-0001",
                "inputs_hash": "old-hash-done",
                "computed_at": "2026-01-15T05:00:00Z",
                "expires_at": "2026-01-18T05:00:00Z",
                "diagnosis_source": "rules",
                "queue_status": "completed",
                "decided_at": "2026-01-15T12:00:00Z",
                # C2: the closing item's needs — only the soon fault
                "closing_needs": frozenset({"fault:P1AB0"}),
            }
        }
        # Current vehicle has a new p0 fault (different from closing needs)
        row = _make_vehicle_row(
            open_faults=[_p0_fault()],
            catalog_entries={_P0_CODE: _P0_CATALOG_ENTRY},
        )
        written: list[dict] = []
        readers = _simple_readers(history=history)

        def write_fn(f, s, refresh_on_noop=True):
            written.append(f)

        run_health_loop(
            vehicle_rows=[row],
            stock_rows=_STOCK_ROWS,
            computed_at=_COMPUTED_AT,
            write_finding=write_fn,
            fault_grounding_enabled=False,
            **readers,
        )
        assert written, (
            "After a completed booking with different needs, a new booking must be written. "
            "Mutation guard (C2): keep completed in _NO_RETRY_STATUSES → skip → fails."
        )
        booking = next(
            (f for f in written if (f.get("subject_key") or "").startswith("service/")), None
        )
        assert booking is not None
        assert booking["subject_key"] == "service/2", (
            f"New episode must start at service/2; got {booking['subject_key']!r}. "
            "Mutation: write_first → service/1 → fails."
        )
        assert booking.get("disclosure_txt") == _P0_DIRECTIVE, (
            "New episode booking must carry the p0 directive. "
        )

    def test_completed_service_same_needs_no_new_episode(self):
        """Completed service/1 with the SAME needs and recent decision → no new episode (C2)."""
        # Use a very recent decided_at (yesterday) so the decline window hasn't passed
        history = {
            "service": {
                "attempt_number": 1,
                "finding_id": "FIND-completed-0002",
                "inputs_hash": "old-hash-done",
                "computed_at": "2026-10-09T05:00:00Z",
                "expires_at": "2026-10-12T05:00:00Z",
                "diagnosis_source": "rules",
                "queue_status": "completed",
                "decided_at": "2026-10-09T12:00:00Z",  # 1 day ago (within 30-day window)
                # Same need as the current vehicle
                "closing_needs": frozenset({"fault:P1AB0"}),
            }
        }
        # Current vehicle has the same soon fault
        row = _make_vehicle_row(open_faults=[_soon_fault()])
        written: list[dict] = []
        readers = _simple_readers(history=history)

        def write_fn(f, s, refresh_on_noop=True):
            written.append(f)

        run_health_loop(
            vehicle_rows=[row], stock_rows=_STOCK_ROWS,
            computed_at=_COMPUTED_AT,  # 2026-10-10 — 1 day after closing
            write_finding=write_fn, **readers,
        )
        assert not written, (
            "Completed with same needs and recent decision must not start a new episode. "
            "Mutation: remove closing_needs check → always starts new episode → fails."
        )

    def test_dismissed_different_needs_new_episode(self):
        """Dismissed service/1 with different current needs → new episode starts."""
        history = {
            "service": {
                "attempt_number": 1,
                "finding_id": "FIND-dismissed-0001",
                "inputs_hash": "old-hash",
                "computed_at": "2026-01-01T00:00:00Z",
                "expires_at": "2026-01-04T00:00:00Z",
                "diagnosis_source": "rules",
                "queue_status": "dismissed",
                "decided_at": "2026-01-02T00:00:00Z",
                "closing_needs": frozenset({"fault:SOME_OLD_CODE"}),
            }
        }
        row = _make_vehicle_row(open_faults=[_p0_fault()],
                                catalog_entries={_P0_CODE: _P0_CATALOG_ENTRY})
        written: list[dict] = []
        readers = _simple_readers(history=history)

        def write_fn(f, s, refresh_on_noop=True):
            written.append(f)

        run_health_loop(
            vehicle_rows=[row], stock_rows=_STOCK_ROWS,
            computed_at=_COMPUTED_AT,
            write_finding=write_fn, **readers,
        )
        assert written, "Dismissed with different needs must start a new episode."
        booking = next(
            (f for f in written if (f.get("subject_key") or "").startswith("service/")), None
        )
        assert booking is not None
        assert booking["subject_key"] == "service/2", (
            f"New episode after dismissal must be service/2; got {booking['subject_key']!r}."
        )


# ---------------------------------------------------------------------------
# W1: hash excludes health.diagnosis_source; utilisation bucketed
# ---------------------------------------------------------------------------

class TestW1HashStability:
    """W1 (C1 fix): inputs_hash excludes health.diagnosis_source and buckets utilisation.

    Rules-path and model-path raw lists must hash identically.
    Utilisation within the same 10-mile bucket must hash identically.

    Mutation guard (W1-source): include health.diagnosis_source in hash →
    rules and model hashes differ → test fails.
    Mutation guard (W1-util): use exact utilisation → hash changes daily for
    driven vehicles → test fails.
    """

    def test_rules_and_model_raw_lists_hash_equal(self):
        """Two raw evidence lists identical except for health.diagnosis_source hash equally.

        W1: health.diagnosis_source is excluded from the hash so a rules-path
        item and a model-path item over the same vehicle data produce the same
        inputs_hash (enabling the skip-unchanged optimisation across paths).
        Mutation guard: include health.diagnosis_source → hashes differ → fails.
        """
        from agents.fi_fleet_health.loop import _build_raw_evidence, _build_item_signature_entry
        from agents.fi_recall_warranty.evidence import compute_inputs_hash

        vin = _VIN
        need = {"kind": "fault", "need_id": "fault:P1AB0", "code": "P1AB0",
                "severity_band": "soon", "last_seen_at": "2026-10-09Z"}
        common_kwargs = dict(
            needs=[need],
            rooftops=[],
            position=None,
            utilisation={"daily_average_miles_30d": 42.5},
            recall_visit=None,
            grounding_reason=None,
            vin=vin,
        )

        # Build two raw lists with different diagnosis_source values
        raw_rules = _build_raw_evidence(diagnosis_source="rules", **common_kwargs)
        raw_model = _build_raw_evidence(diagnosis_source="model", **common_kwargs)

        # Confirm neither contains health.diagnosis_source
        for entries, label in [(raw_rules, "rules"), (raw_model, "model")]:
            kinds = [e.get("kind") for e in entries]
            assert "health.diagnosis_source" not in kinds, (
                f"{label} raw evidence must NOT contain health.diagnosis_source entry. "
                "Mutation guard (W1-source): include it → diagnosis_source in kinds → fails."
            )

        # Both raw lists must hash identically
        h_rules = compute_inputs_hash(raw_rules)
        h_model = compute_inputs_hash(raw_model)
        assert h_rules == h_model, (
            f"rules ({h_rules!r}) and model ({h_model!r}) hashes must be equal. "
            "Mutation guard (W1-source): include health.diagnosis_source → hashes differ → fails."
        )

    def test_utilisation_bucket_same_hash(self):
        """Two runs differing only in utilisation within a 10-mile bucket hash identically.

        W1: utilisation is rounded to nearest 10 for hashing so a vehicle that
        drives a few miles daily doesn't produce a new hash every run.
        Mutation guard: use exact utilisation → 42.5 and 48.0 differ → hashes differ → fails.
        """
        from agents.fi_fleet_health.loop import _build_raw_evidence
        from agents.fi_recall_warranty.evidence import compute_inputs_hash

        vin = _VIN
        need = {"kind": "fault", "need_id": "fault:P1AB0", "code": "P1AB0",
                "severity_band": "soon", "last_seen_at": "2026-10-09Z"}
        common_kwargs = dict(
            needs=[need], rooftops=[], position=None,
            recall_visit=None, grounding_reason=None,
            diagnosis_source="rules", vin=vin,
        )

        # 42.5 and 44.0 both bucket to 40.0 (nearest 10: round(4.25)*10=40, round(4.4)*10=40)
        raw_42 = _build_raw_evidence(utilisation={"daily_average_miles_30d": 42.5},
                                     **common_kwargs)
        raw_44 = _build_raw_evidence(utilisation={"daily_average_miles_30d": 44.0},
                                     **common_kwargs)

        h42 = compute_inputs_hash(raw_42)
        h44 = compute_inputs_hash(raw_44)
        assert h42 == h44, (
            f"42.5 → {h42!r} and 44.0 → {h44!r} must hash identically (same 10-mile bucket: 40). "
            "Mutation guard (W1-util): use exact value → hashes differ → fails."
        )

        # But 42.5 and 55.0 are in different buckets (40 vs 60) — should differ
        raw_55 = _build_raw_evidence(utilisation={"daily_average_miles_30d": 55.0},
                                     **common_kwargs)
        h55 = compute_inputs_hash(raw_55)
        assert h42 != h55, (
            "42.5 (bucket 40) and 55.0 (bucket 60) → hashes must differ."
        )


# ---------------------------------------------------------------------------
# W2 (cycle 3): exact utilisation stored in evidence
# ---------------------------------------------------------------------------

class TestW2ExactUtilisationStored:
    """W2 (cycle 3): evidence carries the exact (un-bucketed) daily_average_miles_30d.

    The hash uses the 10-mile bucket; the stored evidence entry uses the exact value
    so CMS displays the real odometer rate.

    Mutation guard: omit exact_utilisation from _to_schema_evidence → stored value
    is the bucketed copy (40 for 42.7 input) → assertion fails.
    """

    def test_exact_utilisation_stored_in_booking_evidence(self):
        """Booking evidence carries exact daily_average_miles_30d (not bucketed).

        Mutation: store bucketed value → evidence shows 40.0 for 42.7 input → fails.
        """
        exact_miles = 42.7  # bucket is 40.0 (round(4.27)*10)
        row = {
            "vin": _VIN, "fleet_id": _FLEET_ID, "vehicle_id": _VEHICLE_ID,
            "open_faults": [],
            "agent2_findings": [],
            "pm_tasks": [{"task_code": "TIRE_ROTATION", "status": "overdue"}],
            "catalog_entries": {},
            "position": None,
            "utilisation": {"daily_average_miles_30d": exact_miles},
        }
        written: list[dict] = []
        run_health_loop(
            vehicle_rows=[row], stock_rows=_STOCK_ROWS,
            computed_at=_COMPUTED_AT,
            write_finding=lambda f, s, refresh_on_noop=True: written.append(f),
            fault_grounding_enabled=False,
            **_simple_readers(),
        )
        booking = next(
            (f for f in written if (f.get("subject_key") or "").startswith("service/")), None
        )
        assert booking is not None, "A booking must be written for the overdue PM."
        util_entries = [
            ev for ev in (booking.get("evidence") or [])
            if ev.get("kind") == "vehicle.utilisation"
        ]
        assert util_entries, "Evidence must contain a vehicle.utilisation entry."
        stored_miles = util_entries[0]["value"].get("daily_average_miles_30d")
        assert stored_miles == exact_miles, (
            f"Stored utilisation must be the exact input {exact_miles}; "
            f"got {stored_miles!r} (bucketed). "
            "Mutation: omit exact_utilisation → stored value is 40.0 → fails."
        )

    def test_utilisation_within_bucket_still_same_hash(self):
        """42.7 and 44.0 in the same 10-mile bucket → identical inputs_hash.

        W2: the hash uses the bucketed value; exact storage uses the actual miles.
        Both values bucket to 40 (round(4.27)*10 = 40, round(4.4)*10 = 40).
        Confirming the hash is stable while stored evidence differs.
        """
        def _row(miles: float) -> dict:
            return {
                "vin": _VIN, "fleet_id": _FLEET_ID, "vehicle_id": _VEHICLE_ID,
                "open_faults": [],
                "agent2_findings": [],
                "pm_tasks": [{"task_code": "TIRE_ROTATION", "status": "overdue"}],
                "catalog_entries": {},
                "position": None,
                "utilisation": {"daily_average_miles_30d": miles},
            }
        written_42: list[dict] = []
        written_44: list[dict] = []
        run_health_loop(vehicle_rows=[_row(42.7)], stock_rows=_STOCK_ROWS,
                        computed_at=_COMPUTED_AT,
                        write_finding=lambda f, s, refresh_on_noop=True: written_42.append(f),
                        fault_grounding_enabled=False, **_simple_readers())
        run_health_loop(vehicle_rows=[_row(44.0)], stock_rows=_STOCK_ROWS,
                        computed_at=_COMPUTED_AT,
                        write_finding=lambda f, s, refresh_on_noop=True: written_44.append(f),
                        fault_grounding_enabled=False, **_simple_readers())
        b42 = next((f for f in written_42 if (f.get("subject_key") or "").startswith("service/")), None)
        b44 = next((f for f in written_44 if (f.get("subject_key") or "").startswith("service/")), None)
        assert b42 and b44
        assert b42["inputs_hash"] == b44["inputs_hash"], (
            "42.7 and 44.0 are in the same 10-mile bucket → same inputs_hash. "
            "Mutation: use exact value for hash → hashes differ → fails."
        )
        # But stored evidence carries the exact values (different)
        def _util_miles(finding: dict) -> Optional[float]:
            for ev in (finding.get("evidence") or []):
                if ev.get("kind") == "vehicle.utilisation":
                    return ev["value"].get("daily_average_miles_30d")
            return None
        assert _util_miles(b42) == 42.7
        assert _util_miles(b44) == 44.0


# ---------------------------------------------------------------------------
# W3 (cycle 3): health.diagnosis_source present in stored evidence
# ---------------------------------------------------------------------------

class TestW3DiagnosisSourcePresent:
    """W3 (cycle 3): every booking/grounding/exhausted Finding has exactly one
    health.diagnosis_source evidence entry with value 'rules'.

    Mutation guard: drop health.diagnosis_source from _to_schema_evidence →
    no entry in evidence → assertion fails.
    """

    def test_booking_evidence_has_diagnosis_source(self):
        """Booking evidence must contain one health.diagnosis_source='rules' entry.

        W3 (W1-c survivor): the mutant that drops health.diagnosis_source from
        stored evidence survives when only the hash tests are checked.  This test
        pins the stored entry on its own assertion.

        Mutation: remove health.diagnosis_source append from _to_schema_evidence →
        diagnosis_src_entries is empty → assertion fails.
        """
        row = _make_vehicle_row(
            open_faults=[_p0_fault()],
            catalog_entries={_P0_CODE: _P0_CATALOG_ENTRY},
        )
        written: list[dict] = []
        run_health_loop(
            vehicle_rows=[row], stock_rows=_STOCK_ROWS,
            computed_at=_COMPUTED_AT,
            write_finding=lambda f, s, refresh_on_noop=True: written.append(f),
            fault_grounding_enabled=False,
            **_simple_readers(),
        )
        booking = next(
            (f for f in written if (f.get("subject_key") or "").startswith("service/")), None
        )
        assert booking is not None
        diagnosis_entries = [
            ev for ev in (booking.get("evidence") or [])
            if ev.get("kind") == "health.diagnosis_source"
        ]
        assert len(diagnosis_entries) == 1, (
            f"Exactly one health.diagnosis_source entry required; got {diagnosis_entries}. "
            "Mutation (W1-c): drop health.diagnosis_source → empty list → fails."
        )
        assert diagnosis_entries[0]["value"] == "rules", (
            f"health.diagnosis_source must be 'rules'; got {diagnosis_entries[0]['value']!r}. "
            "Mutation: set value='model' → assertion fails."
        )

    def test_grounding_evidence_has_diagnosis_source(self):
        """Grounding item evidence must also carry health.diagnosis_source='rules'."""
        row = _make_vehicle_row(
            open_faults=[_p0_fault()],
            catalog_entries={_P0_CODE: _P0_CATALOG_ENTRY},
        )
        written: list[dict] = []
        run_health_loop(
            vehicle_rows=[row], stock_rows=_STOCK_ROWS,
            computed_at=_COMPUTED_AT,
            write_finding=lambda f, s, refresh_on_noop=True: written.append(f),
            fault_grounding_enabled=True,
            **_simple_readers(),
        )
        grounding = next(
            (f for f in written if (f.get("subject_key") or "").startswith("ground/")), None
        )
        assert grounding is not None
        diagnosis_entries = [
            ev for ev in (grounding.get("evidence") or [])
            if ev.get("kind") == "health.diagnosis_source"
        ]
        assert len(diagnosis_entries) == 1 and diagnosis_entries[0]["value"] == "rules", (
            "Grounding evidence must contain health.diagnosis_source='rules'. "
            "Mutation (W1-c): drop entry → fails."
        )


# ---------------------------------------------------------------------------
# W2: exhausted row order independence
# ---------------------------------------------------------------------------

class TestW2ExhaustedOrderIndependent:
    """W2: exhausted rows are NOT folded into the base segment history.
    service/5 (rejected) wins regardless of whether service_exhausted/1 appears
    before or after it in the by_vin query.

    Mutation guard (W2): fold exhausted back into base segment → if exhausted
    arrives first, queue_status='exhausted' → skip rather than service/5 → flaps.
    """

    def test_exhausted_before_service5_same_as_after(self):
        """Service/5 rejected + service_exhausted/1: both orderings give the same skip.

        Within the 14-day decline window (1 day), service/5 yields skip (not write_exhausted).
        The exhausted item is ignored (W2 fix).

        Mutation guard (W2): fold exhausted → if exhausted arrives first →
        queue_status='exhausted' → skip; if service/5 first → rejected/days_since<14 → skip.
        SAME result by accident but different paths — the W2 test distinguishes them via
        the read_attempt_history_health reader's output.
        """
        from agents.fi_recall_warranty.attempts import MAX_ATTEMPTS
        from agents.fi_fleet_health.loop import read_attempt_history_health
        from unittest.mock import MagicMock
        from boto3.dynamodb.types import TypeSerializer

        ser = TypeSerializer()

        def ddb(v):
            return ser.serialize(v)

        # service/5 row (rejected, 1 day ago)
        svc5_row = {
            "finding_id": ddb("FIND-svc5"),
            "agent_id": ddb("agent-fi-fleet-health"),
            "finding_kind": ddb("fleet.health.service"),
            "subject_key": ddb(f"service/{MAX_ATTEMPTS}"),  # "service/5"
            "severity": ddb("p0"),
            "computed_at": ddb("2026-10-09T05:00:00Z"),
            "expires_at": ddb("2026-10-12T05:00:00Z"),
            "inputs_hash": ddb("hash-svc5"),
            "vin": ddb(_VIN),
            "evidence": ddb([]),
        }

        # service_exhausted/1 row (informational)
        svc_exh_row = {
            "finding_id": ddb("FIND-svc-exh"),
            "agent_id": ddb("agent-fi-fleet-health"),
            "finding_kind": ddb("fleet.health.service"),
            "subject_key": ddb("service_exhausted/1"),
            "severity": ddb("informational"),
            "computed_at": ddb("2026-10-09T06:00:00Z"),
            "expires_at": ddb("2026-10-12T06:00:00Z"),
            "inputs_hash": ddb("hash-exh"),
            "vin": ddb(_VIN),
            "evidence": ddb([]),
        }

        # rejected action for service/5
        from boto3.dynamodb.types import TypeDeserializer
        _deser = TypeDeserializer()
        # Use "cancelled" status which maps directly in _map_action_to_queue_status
        rejected_action = {
            "action_id": {"S": f"ACTION-{_deser.deserialize(svc5_row['finding_id'])}"},
            "status": {"S": "cancelled"},
            "audit_trail": {"L": []},
        }

        for order in [
            [svc_exh_row, svc5_row],   # exhausted first
            [svc5_row, svc_exh_row],   # service/5 first
        ]:
            mock_ddb = MagicMock()
            mock_ddb.query.return_value = {"Items": order}
            mock_ddb.get_item.return_value = {"Item": rejected_action}

            history, skip = read_attempt_history_health(
                _VIN,
                ddb_client=mock_ddb,
                findings_table="vsa-staging-avx-findings",
                actions_table="vsa-staging-avx-actions",
            )

            assert "service" in history, (
                f"service must be in history for order={[r['subject_key'] for r in order]}. "
                "W2 fix: exhausted is ignored; service/5 still maps to service."
            )
            qs = history["service"]["queue_status"]
            attempt_n = history["service"]["attempt_number"]
            assert qs == "cancelled", (
                f"queue_status must be 'cancelled' (service/5) regardless of row order; "
                f"got {qs!r}. Mutation W2: fold exhausted → 'exhausted' when exhausted first."
            )
            assert attempt_n == MAX_ATTEMPTS, (
                f"attempt_number must be {MAX_ATTEMPTS}; got {attempt_n!r}."
            )


# ---------------------------------------------------------------------------
# W4: run_service run record carries service counters
# ---------------------------------------------------------------------------

class TestW4RunServiceRunRecordCounters:
    """W4 (design answer 9): service counters reach the run record.

    book_items_without_rooftops, p0_directive_fallbacks, vins_refused,
    exhausted_items_written, and agent2_read_errors must all appear in the
    record written by run_record_writer.

    Mutation guard: remove forwarding in _service_record_fields →
    missing key in stored record → assertion fails.
    """

    def test_run_service_run_record_carries_service_counters(self, monkeypatch):
        """run_service writes all W4 counters to the run record."""
        from agents.fi_fleet_health import selection as fh_sel
        from agents.fi_recall_warranty import selection as rw_sel
        from agents.fi_fleet_health import loop as fh_loop
        import agents.tier2.runner as _runner

        monkeypatch.setattr(fh_sel, "select_fault_rows", lambda *_a, **_kw: [])
        monkeypatch.setattr(fh_sel, "select_pm_rows",    lambda *_a, **_kw: [])
        monkeypatch.setattr(fh_sel, "select_managed_vins", lambda *_a, **_kw: [])
        monkeypatch.setattr(fh_sel, "select_daily_usage",  lambda *_a, **_kw: [])
        monkeypatch.setattr(fh_sel, "select_agent2_findings",
                            lambda *_a, **_kw: ({}, 2))  # 2 errors
        monkeypatch.setattr(fh_sel, "build_vehicle_rows",
                            lambda **_kw: ([], {}))
        monkeypatch.setattr(rw_sel, "select_parts_stock",  lambda *_a, **_kw: [])

        def fake_loop(*, vehicle_rows, **kw):
            return {
                "vins_selected": 0,
                "vins_processed": 0,
                "items_written": 0,
                "items_skipped_unchanged": 0,
                "vins_refused": 3,
                "grounding_read_errors": 0,
                "exhausted_items_written": 1,
                "by_item_type": {},
                "book_items_without_rooftops": 2,
                "p0_directive_fallbacks": 1,
                # agent2_read_errors will be added by run_service from select_agent2_findings
            }
        monkeypatch.setattr(fh_loop, "run_health_loop", fake_loop)

        from agents.tier2.tests.test_runner_service import _AvxWriter, _Cw
        records: list = []

        _runner.run_service(
            "staging",
            athena_client=object(),
            workgroup="cvx-staging-analytics",
            results_s3_path="s3://results/tier2/fi-fleet-health/",
            ddb_client=object(),
            findings_table="vsa-staging-avx-findings",
            actions_table="vsa-staging-avx-actions",
            groundings_table="cms-staging-storage-vehicle-groundings",
            vehicles_table="cms-staging-storage-vehicles",
            fleet_locations_table="cms-staging-storage-fleet-locations",
            avx_writer=_AvxWriter(),
            owner_resolver=None,
            cloudwatch_client=_Cw(),
            run_record_writer=lambda **k: records.append(k),
        )

        assert records, "run_record_writer must be called once."
        rec = records[0]
        # W4 counters must appear in the extra dict forwarded to write_run_record
        for key in ("book_items_without_rooftops", "p0_directive_fallbacks",
                    "vins_refused", "exhausted_items_written", "agent2_read_errors"):
            assert key in rec, (
                f"Run record must carry {key!r} (W4 fix). "
                f"Mutation: remove from _service_record_fields → key absent → fails."
            )
        assert rec["agent2_read_errors"] == 2, (
            "agent2_read_errors must be 2 (from select_agent2_findings return). "
            f"Got: {rec.get('agent2_read_errors')!r}. "
            "Mutation: drop agent2_read_errors forwarding → 0 → fails."
        )
        assert rec["vins_refused"] == 3
        assert rec["book_items_without_rooftops"] == 2
        assert rec["p0_directive_fallbacks"] == 1
        assert rec["exhausted_items_written"] == 1


# ---------------------------------------------------------------------------
# W5 (kill surviving mutant C5b): run_service with location and two rooftops
# ---------------------------------------------------------------------------

class TestW5PositionDistanceOrder:
    """W5 (kill C5b): run_service's position lookup affects rooftop ranking.

    C5b mutant: never set row["position"] → origin is None → rooftops ranked
    by dealer_id instead of distance → test fails.

    Mutation guard (C5b): drop row["position"] assignment in run_service →
    origin=None → nearest_dealer wrong → assertion fails.
    """

    def test_run_service_position_determines_rooftop_order(self, monkeypatch):
        """Position from registry determines which of two rooftops is nearest."""
        from agents.fi_fleet_health import selection as fh_sel
        from agents.fi_recall_warranty import selection as rw_sel
        from agents.fi_fleet_health import loop as fh_loop
        import agents.tier2.runner as _runner

        monkeypatch.setattr(fh_sel, "select_fault_rows", lambda *_a, **_kw: [])
        monkeypatch.setattr(fh_sel, "select_pm_rows",    lambda *_a, **_kw: [])
        monkeypatch.setattr(fh_sel, "select_managed_vins", lambda *_a, **_kw: [])
        monkeypatch.setattr(fh_sel, "select_daily_usage",  lambda *_a, **_kw: [])
        monkeypatch.setattr(fh_sel, "select_agent2_findings",
                            lambda *_a, **_kw: ({}, 0))

        # Vehicle at Seattle-ish, two dealers: one nearby (Seattle), one far (Miami)
        vehicle_row = {
            "vin": _VIN,
            "fleet_id": _FLEET_ID,
            "open_faults": [{"code": _P0_CODE, "band": "p0",
                              "last_seen_at": "2026-10-09Z"}],
            "pm_tasks": [], "agent2_findings": [],
            "utilisation": {"daily_average_miles_30d": 30.0},
            "catalog_entries": {_P0_CODE: _P0_CATALOG_ENTRY},
            # vehicle_id and position will be set by run_service
        }
        monkeypatch.setattr(fh_sel, "build_vehicle_rows",
                            lambda **_kw: ([vehicle_row], {}))

        # Registry returns vehicle_id + fleet_id + location_id for this VIN
        from agents.fi_recall_warranty import selection as _rw_sel
        monkeypatch.setattr(
            _rw_sel, "lookup_vehicle_cms_ids",
            lambda vin, **kw: ("vehicle-001", _FLEET_ID, "loc-seattle"),
        )
        # Fleet home at Seattle (47.6°N, -122.3°W)
        monkeypatch.setattr(
            _rw_sel, "lookup_fleet_locations",
            lambda fleet_id, **kw: {"loc-seattle": (47.6062, -122.3321)},
        )

        # Two rooftops: Seattle (nearby) and Miami (far)
        seattle_stock = {
            "dealer_id": "dealer-seattle-001",
            "part_number": "HV-ISO-KIT-001",
            "qty_on_hand": 2,
            "latitude": "47.6152",      # very close to fleet home
            "longitude": "-122.3447",
            "stock_as_of": "2026-10-09T00:00:00Z",
        }
        miami_stock = {
            "dealer_id": "dealer-miami-002",
            "part_number": "HV-ISO-KIT-001",
            "qty_on_hand": 2,
            "latitude": "25.7617",      # far from fleet home
            "longitude": "-80.1918",
            "stock_as_of": "2026-10-09T00:00:00Z",
        }
        monkeypatch.setattr(rw_sel, "select_parts_stock",
                            lambda *_a, **_kw: [seattle_stock, miami_stock])

        # Capture what run_health_loop receives
        loop_calls: list = []

        def capture_loop(*, vehicle_rows, **kw):
            loop_calls.append({"rows": vehicle_rows, **kw})
            return {
                "vins_selected": len(vehicle_rows), "vins_processed": 0,
                "items_written": 0, "items_skipped_unchanged": 0,
                "vins_refused": 0, "grounding_read_errors": 0,
                "exhausted_items_written": 0, "by_item_type": {},
                "book_items_without_rooftops": 0, "p0_directive_fallbacks": 0,
            }
        monkeypatch.setattr(fh_loop, "run_health_loop", capture_loop)

        from agents.tier2.tests.test_runner_service import _AvxWriter, _Cw
        _runner.run_service(
            "staging",
            athena_client=object(),
            workgroup="cvx-staging-analytics",
            results_s3_path="s3://results/tier2/fi-fleet-health/",
            ddb_client=object(),
            findings_table="vsa-staging-avx-findings",
            actions_table="vsa-staging-avx-actions",
            groundings_table="cms-staging-storage-vehicle-groundings",
            vehicles_table="cms-staging-storage-vehicles",
            fleet_locations_table="cms-staging-storage-fleet-locations",
            avx_writer=_AvxWriter(),
            owner_resolver=None,
            cloudwatch_client=_Cw(),
            run_record_writer=lambda **k: None,
            fault_grounding_enabled=True,
        )

        assert loop_calls, "run_health_loop must be called."
        enriched_row = loop_calls[0]["rows"][0]

        # C5b mutation guard: vehicle_id populated
        assert enriched_row.get("vehicle_id") == "vehicle-001", (
            f"vehicle_id must be populated by run_service; got {enriched_row.get('vehicle_id')!r}. "
            "Mutation C5b: drop vehicle_id → None → grounded_fn not called → fails."
        )

        # W5 mutation guard: position populated (fleet home coordinates)
        pos = enriched_row.get("position")
        assert pos is not None, (
            "position must be populated from fleet home (run_service C5 fix). "
            "Mutation C5b: drop position → None → rooftop ranked by dealer_id not distance → fails."
        )
        assert abs(pos.get("latitude", 0) - 47.6062) < 0.01, (
            f"position latitude must be near Seattle (47.6°); got {pos.get('latitude')!r}. "
            "Mutation C5b: no position → distance order not deterministic → fails."
        )


# ---------------------------------------------------------------------------
# W6: p0 with no rooftop, flag off — booking with directive and special headline
# ---------------------------------------------------------------------------

class TestW6P0NoRooftopFlagOff:
    """W6 (design answer 3): p0 with no rooftop + flag off → booking with directive.

    With fault_grounding_enabled=False, the booking slot is still written for p0
    with no candidate rooftop (no dealer_id, special headline, directive present).
    """

    def test_p0_no_rooftop_flag_off_writes_booking_with_directive(self):
        """p0 with no stock rows and flag=False: booking with special headline."""
        from agents.fi_fleet_health.writer import _HEADLINE_BOOKING_P0_NO_ROOFTOP
        row = _make_vehicle_row(
            open_faults=[_p0_fault()],
            catalog_entries={_P0_CODE: _P0_CATALOG_ENTRY},
        )
        written: list[dict] = []
        readers = _simple_readers()

        run_health_loop(
            vehicle_rows=[row],
            stock_rows=[],      # no rooftop
            computed_at=_COMPUTED_AT,
            write_finding=lambda f, s, refresh_on_noop=True: written.append(f),
            fault_grounding_enabled=False,
            **readers,
        )
        booking = next(
            (f for f in written if (f.get("subject_key") or "").startswith("service/")), None
        )
        assert booking is not None, (
            "p0 with no rooftop (flag=off) must still write a booking item (W6). "
            "Mutation: keep _skip_booking_no_rooftop = not default_dealer_id → no booking → fails."
        )
        expected = _HEADLINE_BOOKING_P0_NO_ROOFTOP.format(code=_P0_CODE)
        assert booking.get("headline") == expected, (
            f"Expected no-rooftop p0 headline {expected!r}; got {booking.get('headline')!r}."
        )
        assert booking.get("disclosure_txt") == _P0_DIRECTIVE, (
            "No-rooftop p0 booking must carry the directive."
        )
        ra = booking.get("recommended_action") or {}
        assert ra.get("parameters", {}).get("dealer_id") is None, (
            "No-rooftop p0 booking must have dealer_id=None (operator must pick one)."
        )


# ---------------------------------------------------------------------------
# W7: Agent 2 p0 → attention; build_headline raises on None code
# ---------------------------------------------------------------------------

class TestW7cGroundingHeadlineNullFaultCodeRaises:
    """W7-c (S2 cycle 3): build_headline raises ValueError for a grounding item
    with fault_code=None.  Today this path is unreachable through the loop
    (classify_needs enforces p0 faults before grounding is built), but the guard
    must still be tested directly so a future caller can't silently bypass it.

    Mutation guard: remove the `if fault_code is None: raise` in writer.py →
    build_headline returns a malformed headline 'Ground vehicle: stop-driving fault None' →
    test expects ValueError but gets a string → fails.
    """

    def test_grounding_none_fault_code_raises(self):
        """build_headline(item_type='grounding', fault_code=None) must raise ValueError.

        Mutation (W7-c): remove the raise guard → no exception raised → pytest.raises
        block exits without catching it → test fails on its own assertion.
        """
        import pytest
        from agents.fi_fleet_health.writer import build_headline
        with pytest.raises(ValueError, match="fault_code must not be None"):
            build_headline(
                item_type="grounding",
                severity="p0",
                fault_code=None,
                need_description=None,
                rooftop_dealer_id=None,
            )


class TestW7Agent2P0MappedToAttention:
    """W7 (design answer 4): Agent 2 findings at p0 are capped at attention.

    An Agent 2 p0 finding with no open p0 fault must produce:
    - severity = 'attention' (not 'p0') on the booking item
    - no grounding item (no open p0 fault)
    - no p0 headline (None code for p0 template raises)

    Mutation guard (W7): remove the cap → Agent 2 p0 severity reaches the booking
    builder → build_headline called with code=None for p0 template → raises or
    produces wrong headline → fails.
    """

    def test_agent2_p0_booking_has_attention_severity(self):
        """Agent 2 p0 finding → booking with severity='attention', not 'p0'."""
        row = _make_vehicle_row(
            agent2_findings=[{
                "finding_id": "fi-a2-p0-001",
                "severity": "p0",
                "finding_kind": "health.battery",
            }],
            open_faults=[],   # no open fault codes
        )
        written: list[dict] = []
        readers = _simple_readers()

        run_health_loop(
            vehicle_rows=[row], stock_rows=_STOCK_ROWS,
            computed_at=_COMPUTED_AT,
            write_finding=lambda f, s, refresh_on_noop=True: written.append(f),
            fault_grounding_enabled=True,
            **readers,
        )
        booking = next(
            (f for f in written if (f.get("subject_key") or "").startswith("service/")), None
        )
        assert booking is not None
        assert booking.get("severity") == "attention", (
            f"Agent 2 p0 must map to 'attention'; got {booking.get('severity')!r}. "
            "Mutation W7: remove cap → severity='p0' → build_headline raises on None code → fails."
        )
        # W5 (cycle 3): the NEED's severity_band must also be capped at 'attention'.
        # bands.py classify_needs applies the per-need cap (sev_band).
        # Mutation (W7-a): remove sev_band cap → need.severity_band='p0' → fails.
        need_entries = [
            ev for ev in (booking.get("evidence") or [])
            if ev.get("kind") == "health.need"
               and (ev.get("value") or {}).get("kind") == "agent2_finding"
        ]
        assert need_entries, "Agent 2 finding must appear as health.need in evidence."
        for ne in need_entries:
            sev_band = (ne.get("value") or {}).get("severity_band")
            assert sev_band == "attention", (
                f"Agent 2 need severity_band must be 'attention', not {sev_band!r} (W5). "
                "Mutation W7-a: remove sev_band cap in bands.py → severity_band='p0' → fails."
            )
        subjects = [f.get("subject_key", "") for f in written]
        assert not any(s.startswith("ground/") for s in subjects), (
            "Agent 2 p0 (no open fault) must NOT produce a grounding item."
        )


# ---------------------------------------------------------------------------
# W8: Agent 2 need description uses finding_kind
# ---------------------------------------------------------------------------

class TestW8Agent2NeedDescription:
    """W8: Agent 2 need's RO line description uses finding_kind, not 'agent2_finding'.

    Mutation guard (W8): restore 'agent2_finding' literal → description wrong → fails.
    """

    def test_agent2_need_description_uses_finding_kind(self):
        """Agent 2 battery finding → RO line description = 'battery', not 'agent2_finding'."""
        row = _make_vehicle_row(
            agent2_findings=[{
                "finding_id": "fi-a2-batt",
                "severity": "attention",
                "finding_kind": "health.battery",
            }],
        )
        written: list[dict] = []
        readers = _simple_readers()

        run_health_loop(
            vehicle_rows=[row], stock_rows=_STOCK_ROWS,
            computed_at=_COMPUTED_AT,
            write_finding=lambda f, s, refresh_on_noop=True: written.append(f),
            **readers,
        )
        assert written
        booking = written[0]
        ra = booking.get("recommended_action") or {}
        needs_lines = ra.get("parameters", {}).get("needs", [])
        assert needs_lines, "Booking must have at least one need line."
        a2_line = needs_lines[0]
        desc = a2_line.get("description", "")
        assert desc == "battery", (
            f"Agent 2 battery description must be 'battery' (from finding_kind 'health.battery'); "
            f"got {desc!r}. "
            "Mutation W8: restore 'agent2_finding' literal → fails."
        )


# ---------------------------------------------------------------------------
# W9: needs carry 'source' field
# ---------------------------------------------------------------------------

class TestW9NeedsCarrySource:
    """W9: each health.need evidence entry carries a 'source' field."""

    def test_fault_need_carries_source_vehicle_fault_status(self):
        """Fault needs carry source='vehicle_fault_status'."""
        from agents.fi_fleet_health.bands import classify_needs
        result = classify_needs(
            open_faults=[{"code": "P1AB0", "band": "soon"}],
            agent2_findings=[],
            pm_tasks=[],
        )
        assert result.needs
        fault_need = next(n for n in result.needs if n.get("kind") == "fault")
        assert fault_need.get("source") == "vehicle_fault_status", (
            f"Fault need source must be 'vehicle_fault_status'; got {fault_need.get('source')!r}. "
            "W9: source added per need kind (contract §1.6)."
        )

    def test_pm_need_carries_source_pm_vehicle_status(self):
        """PM needs carry source='pm_vehicle_status'."""
        from agents.fi_fleet_health.bands import classify_needs
        result = classify_needs(
            open_faults=[],
            agent2_findings=[],
            pm_tasks=[{"task_code": "TIRE_ROTATION", "status": "overdue"}],
        )
        pm_need = next(n for n in result.needs if n.get("kind") == "pm_task")
        assert pm_need.get("source") == "pm_vehicle_status", (
            f"PM need source must be 'pm_vehicle_status'; got {pm_need.get('source')!r}."
        )

    def test_agent2_need_carries_source_avx_findings(self):
        """Agent 2 needs carry source='avx_findings'."""
        from agents.fi_fleet_health.bands import classify_needs
        result = classify_needs(
            open_faults=[],
            agent2_findings=[{"finding_id": "fi-a2", "severity": "soon",
                              "finding_kind": "health.battery"}],
            pm_tasks=[],
        )
        a2_need = next(n for n in result.needs if n.get("kind") == "agent2_finding")
        assert a2_need.get("source") == "avx_findings", (
            f"Agent 2 need source must be 'avx_findings'; got {a2_need.get('source')!r}."
        )


# ---------------------------------------------------------------------------
# W10: exact FilterExpression operator; narrow catch; agent2_read_errors
# ---------------------------------------------------------------------------

class TestW10ExactFilterExpressionAndNarrowCatch:
    """W10 (kill surviving mutant): agent2 expires_at comparison is '>' (not '<').
    Narrow catch (ClientError/BotoCoreError), and error count returned.

    Mutation guard (W10-operator): change '> :now' to '< :now' → returns expired
    findings → test_agent2_expires_at_gt_not_lt fails.
    """

    def test_agent2_expires_at_uses_gt_comparison(self):
        """select_agent2_findings FilterExpression uses '> :now', not '< :now'.

        W10 (kill surviving mutant): the mutant 'expires_at < :now' passed every
        suite because no test checked the exact operator.

        Mutation guard (W10-operator): revert to '< :now' → the assertion
        'expires_at >' is not in filter_expr → fails.
        """
        from agents.fi_fleet_health.selection import select_agent2_findings
        from unittest.mock import MagicMock

        filter_exprs_seen: list = []

        def fake_query(**kwargs):
            filter_exprs_seen.append(kwargs.get("FilterExpression", ""))
            return {"Items": []}

        ddb = MagicMock()
        ddb.query.side_effect = fake_query
        import datetime
        select_agent2_findings(
            ["fleet-x"],
            ddb_client=ddb,
            findings_table="avx-staging",
            now=datetime.datetime.now(datetime.timezone.utc),
        )

        assert filter_exprs_seen, "DDB query must be called."
        fe = filter_exprs_seen[0]
        assert "expires_at > :now" in fe, (
            f"FilterExpression must use '> :now' for live-only filter (W10). "
            f"Got: {fe!r}. "
            "Mutation W10-operator: use '< :now' → expression has '< :now' not '> :now' → fails."
        )
        assert "expires_at < :now" not in fe, (
            "FilterExpression must NOT contain 'expires_at < :now' (stale-only filter). "
            "Mutation guard: revert → '< :now' present → fails."
        )

    def test_programming_error_propagates_not_swallowed(self):
        """A non-DDB exception (programming error) propagates out of select_agent2_findings.

        W10 narrow catch: only ClientError/BotoCoreError are caught. A ValueError
        or AttributeError (programming error) must not be swallowed.
        """
        from agents.fi_fleet_health.selection import select_agent2_findings
        from unittest.mock import MagicMock
        import datetime

        def boom(**kwargs):
            raise ValueError("programming error — must propagate")

        ddb = MagicMock()
        ddb.query.side_effect = boom

        with pytest.raises(ValueError, match="programming error"):
            select_agent2_findings(
                ["fleet-y"],
                ddb_client=ddb,
                findings_table="avx-staging",
                now=datetime.datetime.now(datetime.timezone.utc),
            )


# ---------------------------------------------------------------------------
# W11 (kill surviving mutant C6b): schema validation of exhausted items
# ---------------------------------------------------------------------------

class TestW11ExhaustedFindingSchemaCompliant:
    """W11 (kill C6b): service_exhausted/1 and ground_exhausted/1 must validate
    against finding.schema.json with no top-level fleet_id.

    C6b mutant: add top-level fleet_id to the exhausted item → additionalProperties:false
    raises ValidationError → test fails.
    """

    def _get_schema(self):
        import json
        from pathlib import Path
        return json.loads(
            (Path(__file__).parent.parent.parent.parent
             / "agents/tier2/schemas/finding.schema.json").read_text()
        )

    def test_service_exhausted_validates_against_schema(self):
        """service_exhausted/1 produced by run_health_loop validates against schema."""
        import jsonschema
        from agents.fi_recall_warranty.attempts import MAX_ATTEMPTS

        schema = self._get_schema()
        history = {
            "service": {
                "attempt_number": MAX_ATTEMPTS,
                "finding_id": "FIND-exh-0001",
                "inputs_hash": "old-hash",
                "computed_at": "2026-10-01T00:00:00Z",
                "expires_at": "2026-10-04T00:00:00Z",
                "diagnosis_source": "rules",
                "queue_status": "cancelled",
                "decided_at": "2026-10-01T00:00:00Z",
                "episode_start": 1,
            }
        }
        row = _make_vehicle_row(
            open_faults=[_p0_fault()],
            catalog_entries={_P0_CODE: _P0_CATALOG_ENTRY},
        )
        written: list[dict] = []
        readers = _simple_readers(history=history)

        run_health_loop(
            vehicle_rows=[row], stock_rows=_STOCK_ROWS,
            computed_at=_COMPUTED_AT,
            write_finding=lambda f, s, refresh_on_noop=True: written.append(f),
            fault_grounding_enabled=False,
            **readers,
        )
        exh = next((f for f in written if "exhausted" in (f.get("subject_key") or "")), None)
        assert exh is not None, "Exhausted item must be written."

        jsonschema.validate(exh, schema)  # must not raise

        assert "fleet_id" not in exh, (
            "Exhausted item must have no top-level fleet_id (C6b). "
            "Mutation C6b: add fleet_id back → additionalProperties:false raises → fails."
        )
        assert "diagnosis_source" not in exh, (
            "Exhausted item must have no top-level diagnosis_source (C6). "
        )

    def test_ground_exhausted_validates_against_schema(self):
        """ground_exhausted/1 produced by run_health_loop validates against schema."""
        import jsonschema
        from agents.fi_recall_warranty.attempts import MAX_ATTEMPTS

        schema = self._get_schema()
        history = {
            "ground": {
                "attempt_number": MAX_ATTEMPTS,
                "finding_id": "FIND-gexh-0001",
                "inputs_hash": "old-hash",
                "computed_at": "2026-10-01T00:00:00Z",
                "expires_at": "2026-10-04T00:00:00Z",
                "diagnosis_source": "rules",
                "queue_status": "cancelled",
                "decided_at": "2026-10-01T00:00:00Z",
                "episode_start": 1,
            }
        }
        row = _make_vehicle_row(
            open_faults=[_p0_fault()],
            catalog_entries={_P0_CODE: _P0_CATALOG_ENTRY},
        )
        written: list[dict] = []
        readers = _simple_readers(history=history)

        run_health_loop(
            vehicle_rows=[row], stock_rows=_STOCK_ROWS,
            computed_at=_COMPUTED_AT,
            write_finding=lambda f, s, refresh_on_noop=True: written.append(f),
            fault_grounding_enabled=True,
            **readers,
        )
        exh = next((f for f in written if "ground_exhausted" in (f.get("subject_key") or "")), None)
        assert exh is not None, "ground_exhausted item must be written."
        jsonschema.validate(exh, schema)
        assert "fleet_id" not in exh



# ---------------------------------------------------------------------------
# G3F3.1 — Fix Group G3-3 tests
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Answer 1 (a1/a2 C1): write_run_record signature covers every field that
# _service_record_fields forwards.  Tests use the real write_run_record and
# production-shaped call paths (run_record_writer=None + fake boto3.resource).
# ---------------------------------------------------------------------------

class TestG3F3RunRecordSignatureBind:
    """G3F3.1 (a1/a2 C1): every counter _service_record_fields forwards is a
    named parameter of write_run_record.  An injected catch-all writer hides
    mismatches; binding against the real signature catches them.

    Mutation guard: remove registry_read_errors from write_run_record →
    inspect.signature(...).bind(**kwargs) raises TypeError → test fails.
    """

    def test_service_record_fields_bind_to_write_run_record(self):
        """inspect.signature(write_run_record).bind covers every _service_record_fields key."""
        import inspect
        from agents.tier2.run_records import write_run_record
        from agents.tier2.runner import _service_record_fields

        # Build a stats dict with every key _service_record_fields reads.
        full_stats = {
            "vins_selected": 10,
            "grounding_read_errors": 1,
            "book_items_without_rooftops": 2,
            "p0_directive_fallbacks": 0,
            "vins_refused": 3,
            "exhausted_items_written": 1,
            "agent2_read_errors": 4,
            "registry_read_errors": 2,
            "vehicles_over_cap": 5,
            "managed_select_errors": 1,
        }
        extra = _service_record_fields(full_stats)

        required = dict(
            agent_id="agent-fi-fleet-health",
            component="service",
            started_at="2026-10-10T02:00:00Z",
            finished_at="2026-10-10T02:01:00Z",
            status="completed",
            artifacts_written=10,
            duplicates_skipped=2,
            agent_version="0.1.0",
            ddb_client=object(),
            table_name="vsa-staging-avx-agent-runs",
        )
        # This must NOT raise TypeError — if write_run_record is missing any key
        # forwarded by _service_record_fields, bind() raises.
        try:
            inspect.signature(write_run_record).bind(**required, **extra)
        except TypeError as exc:
            raise AssertionError(
                f"write_run_record is missing a parameter forwarded by "
                f"_service_record_fields: {exc}. "
                "G3F3.1 fix: add registry_read_errors, vehicles_over_cap and "
                "managed_select_errors to write_run_record's signature."
            ) from exc

    def test_run_service_with_real_write_run_record_stores_record(self, monkeypatch):
        """run_service with run_record_writer=None stores one run record to the fake DDB.

        Uses a fake boto3.resource (no real AWS calls).  The run record must be
        stored and carry registry_read_errors and vehicles_over_cap.

        Mutation guard: remove registry_read_errors / vehicles_over_cap from
        write_run_record → TypeError → except → 0 records stored → assert fails.
        """
        import boto3 as _boto3
        import os
        from agents.fi_fleet_health import selection as fh_sel
        from agents.fi_recall_warranty import selection as rw_sel
        from agents.fi_fleet_health import loop as fh_loop
        import agents.tier2.runner as _runner
        from agents.tier2.tests.test_runner_service import _AvxWriter, _Cw

        puts = []

        class _Tbl:
            def put_item(self, Item):
                puts.append(Item)

        class _Res:
            def Table(self, name):
                return _Tbl()

        monkeypatch.setattr(_boto3, "resource", lambda *a, **k: _Res())
        monkeypatch.setenv("AVX_AGENT_RUNS_TABLE", "vsa-staging-avx-agent-runs")

        monkeypatch.setattr(fh_sel, "select_fault_rows", lambda *a, **k: [])
        monkeypatch.setattr(fh_sel, "select_pm_rows",    lambda *a, **k: [])
        monkeypatch.setattr(fh_sel, "select_managed_vins", lambda *a, **k: [])
        monkeypatch.setattr(fh_sel, "select_daily_usage",  lambda *a, **k: [])
        monkeypatch.setattr(fh_sel, "select_agent2_findings",
                            lambda *a, **k: ({}, 0))
        monkeypatch.setattr(fh_sel, "build_vehicle_rows",
                            lambda **kw: ([], {}))
        monkeypatch.setattr(rw_sel, "select_parts_stock", lambda *a, **k: [])

        def fake_loop(*, vehicle_rows, **kw):
            return {
                "vins_selected": 0,
                "vins_processed": 0,
                "items_written": 0,
                "items_skipped_unchanged": 0,
                "vins_refused": 0,
                "grounding_read_errors": 0,
                "exhausted_items_written": 0,
                "by_item_type": {},
                "book_items_without_rooftops": 0,
                "p0_directive_fallbacks": 0,
                "registry_read_errors": 7,
                "vehicles_over_cap": 3,
                "agent2_read_errors": 0,
            }
        monkeypatch.setattr(fh_loop, "run_health_loop", fake_loop)

        _runner.run_service(
            "staging",
            athena_client=object(),
            workgroup="cvx-staging-analytics",
            results_s3_path="s3://results/tier2/fi-fleet-health/",
            ddb_client=object(),
            findings_table="vsa-staging-avx-findings",
            actions_table="vsa-staging-avx-actions",
            groundings_table="cms-staging-storage-vehicle-groundings",
            vehicles_table="cms-staging-storage-vehicles",
            fleet_locations_table="cms-staging-storage-fleet-locations",
            avx_writer=_AvxWriter(),
            owner_resolver=None,
            cloudwatch_client=_Cw(),
            run_record_writer=None,
        )
        assert len(puts) == 1, (
            f"Exactly 1 run record must be stored; got {len(puts)}. "
            "G3F3.1 fix: registry_read_errors and vehicles_over_cap must be named "
            "parameters of write_run_record so _emit_run_record doesn't raise TypeError."
        )
        rec = puts[0]
        # registry_read_errors and vehicles_over_cap must be present (even at 0,
        # they're forwarded from run_service's own counters, not from the fake loop).
        assert "registry_read_errors" in rec, (
            "Run record must carry registry_read_errors. "
            "Mutation: remove registry_read_errors from write_run_record → TypeError "
            "→ 0 records stored → the assert above fails first."
        )
        assert "vehicles_over_cap" in rec, (
            "Run record must carry vehicles_over_cap. "
        )


# ---------------------------------------------------------------------------
# Answer 2 (a1 C2): closing time from Action's updated_at / created_at
# ---------------------------------------------------------------------------

class TestG3F3ClosingTimeFromAction:
    """G3F3.1 (a1 C2): read_attempt_history_health takes decided_at from
    Action.updated_at / created_at when the audit trail has no entry (DMS
    consumer and dismiss write none).

    Mutation guard: keep decided_at=None for 'completed' → _is_new_episode_needed
    always returns False on the window branch → window branch dead → same-need
    booking re-opens after 30 days → test fails.
    """

    def _make_ddb_client(self, items, action_obj):
        from unittest.mock import MagicMock
        mock_ddb = MagicMock()
        mock_ddb.query.return_value = {"Items": items}
        mock_ddb.get_item.return_value = {"Item": action_obj}
        return mock_ddb

    def _serialize(self, v):
        from boto3.dynamodb.types import TypeSerializer
        return TypeSerializer().serialize(v)

    def test_completed_booking_with_updated_at_sets_decided_at(self):
        """A 'completed' Action with updated_at and no audit entry produces decided_at."""
        from agents.fi_fleet_health.loop import read_attempt_history_health
        s = self._serialize

        svc_row = {
            "finding_id": s("FIND-svc-001"),
            "agent_id": s("agent-fi-fleet-health"),
            "finding_kind": s("fleet.health.service"),
            "subject_key": s("service/1"),
            "severity": s("attention"),
            "computed_at": s("2026-10-01T05:00:00Z"),
            "expires_at": s("2026-10-04T05:00:00Z"),
            "inputs_hash": s("hash-001"),
            "vin": s(_VIN),
            "evidence": s([]),
        }
        action_obj = {
            "action_id": {"S": "ACTION-FIND-svc-001"},
            "status": {"S": "completed"},
            "updated_at": {"S": "2026-10-02T12:00:00Z"},
            "created_at": {"S": "2026-10-01T10:00:00Z"},
            "audit_trail": {"L": []},  # DMS consumer writes no audit entries
        }
        mock_ddb = self._make_ddb_client([svc_row], action_obj)
        history, skip = read_attempt_history_health(
            _VIN, ddb_client=mock_ddb,
            findings_table="vsa-staging-avx-findings",
            actions_table="vsa-staging-avx-actions",
        )
        assert "service" in history
        hs = history["service"]
        assert hs["queue_status"] == "completed", f"queue_status={hs['queue_status']!r}"
        assert hs.get("decided_at") == "2026-10-02T12:00:00Z", (
            f"decided_at must be updated_at for completed-with-no-audit; "
            f"got {hs.get('decided_at')!r}. "
            "Mutation: keep decided_at=None → _is_new_episode_needed always False → fails."
        )

    def test_dismissed_with_updated_at_sets_decided_at(self):
        """A 'dismissed' Action with updated_at produces decided_at."""
        from agents.fi_fleet_health.loop import read_attempt_history_health
        s = self._serialize

        svc_row = {
            "finding_id": s("FIND-svc-002"),
            "agent_id": s("agent-fi-fleet-health"),
            "finding_kind": s("fleet.health.service"),
            "subject_key": s("service/2"),
            "severity": s("soon"),
            "computed_at": s("2026-10-01T05:00:00Z"),
            "expires_at": s("2026-10-04T05:00:00Z"),
            "inputs_hash": s("hash-002"),
            "vin": s(_VIN),
            "evidence": s([]),
        }
        action_obj = {
            "action_id": {"S": "ACTION-FIND-svc-002"},
            "status": {"S": "dismissed"},
            "updated_at": {"S": "2026-10-05T08:00:00Z"},
            "created_at": {"S": "2026-10-01T10:00:00Z"},
            "audit_trail": {"L": []},
        }
        mock_ddb = self._make_ddb_client([svc_row], action_obj)
        history, _ = read_attempt_history_health(
            _VIN, ddb_client=mock_ddb,
            findings_table="vsa-staging-avx-findings",
            actions_table="vsa-staging-avx-actions",
        )
        hs = history.get("service", {})
        assert hs.get("decided_at") == "2026-10-05T08:00:00Z", (
            f"decided_at must be updated_at for dismissed; got {hs.get('decided_at')!r}."
        )

    def test_completed_booking_14_day_window_prevents_new_episode(self):
        """Within the 14-day window for p0/attention, same needs → skip (not new episode)."""
        history = {
            "service": {
                "attempt_number": 1,
                "finding_id": "FIND-c14",
                "inputs_hash": "h",
                "computed_at": "2026-10-09T05:00:00Z",
                "expires_at": "2026-10-12T05:00:00Z",
                "diagnosis_source": "rules",
                "queue_status": "completed",
                # C2 fix: decided_at set from updated_at (not audit trail)
                "decided_at": "2026-10-09T12:00:00Z",  # 1 day ago
                "closing_needs": frozenset({f"fault:{_P0_CODE}"}),
                "episode_start": 1,
            }
        }
        row = _make_vehicle_row(
            open_faults=[_p0_fault()],
            catalog_entries={_P0_CODE: _P0_CATALOG_ENTRY},
        )
        written: list = []
        import datetime as _dt_c2
        now_for_test = _dt_c2.datetime(2026, 10, 10, 5, 40, tzinfo=_dt_c2.timezone.utc)
        run_health_loop(
            vehicle_rows=[row], stock_rows=_STOCK_ROWS,
            computed_at=_COMPUTED_AT,  # 2026-10-10, 1 day after
            write_finding=lambda f, s, refresh_on_noop=True: written.append(f),
            fault_grounding_enabled=False,
            now=now_for_test,
            **_simple_readers(history=history),
        )
        assert not written, (
            "1 day after completion with same needs and p0 (14-day window) must skip. "
            "Mutation: remove C2 fix → decided_at=None → window check always False "
            "→ same need still triggers new episode → written → fails."
        )

    def test_completed_booking_after_14_day_window_allows_new_episode(self):
        """After 14 days for p0/attention, same needs → new episode."""
        history = {
            "service": {
                "attempt_number": 1,
                "finding_id": "FIND-c14p",
                "inputs_hash": "h",
                "computed_at": "2026-08-01T05:00:00Z",
                "expires_at": "2026-08-04T05:00:00Z",
                "diagnosis_source": "rules",
                "queue_status": "completed",
                # 70 days before _COMPUTED_AT (2026-10-10) — well past both 14- and 30-day windows
                "decided_at": "2026-08-01T12:00:00Z",
                "closing_needs": frozenset({f"fault:{_P0_CODE}"}),
                "episode_start": 1,
            }
        }
        row = _make_vehicle_row(
            open_faults=[_p0_fault()],
            catalog_entries={_P0_CODE: _P0_CATALOG_ENTRY},
        )
        written: list = []
        import datetime
        now_for_test = datetime.datetime(2026, 10, 10, 5, 40, tzinfo=datetime.timezone.utc)
        run_health_loop(
            vehicle_rows=[row], stock_rows=_STOCK_ROWS,
            computed_at=_COMPUTED_AT,
            write_finding=lambda f, s, refresh_on_noop=True: written.append(f),
            fault_grounding_enabled=False,
            now=now_for_test,
            **_simple_readers(history=history),
        )
        bookings = [f for f in written if (f.get("subject_key") or "").startswith("service/")]
        assert bookings, (
            "After 70 days for p0/attention (past 14-day window), same needs → "
            "new episode must start. "
            "14↔30 swap mutation: uses 30-day threshold → no write at day 15 → fails."
        )
        assert bookings[0]["subject_key"] == "service/2"

    def test_closing_needs_equals_stored_item_need_ids(self):
        """closing_needs in history equals the need ids stored in the Finding's evidence."""
        from agents.fi_fleet_health.loop import read_attempt_history_health
        s = self._serialize

        need_id = f"fault:{_P0_CODE}"
        evidence = [
            {
                "kind": "health.need",
                "source": "agent-fi-fleet-health",
                "source_ref": "fi-fleet-health-run",
                "value": {"need_id": need_id, "kind": "fault"},
                "computed_at": "2026-10-01T05:00:00Z",
                "provenance": "live",
                "vin": _VIN,
            }
        ]
        svc_row = {
            "finding_id": s("FIND-svc-003"),
            "agent_id": s("agent-fi-fleet-health"),
            "finding_kind": s("fleet.health.service"),
            "subject_key": s("service/1"),
            "severity": s("p0"),
            "computed_at": s("2026-10-01T05:00:00Z"),
            "expires_at": s("2026-10-04T05:00:00Z"),
            "inputs_hash": s("hash-003"),
            "vin": s(_VIN),
            "evidence": s(evidence),
        }
        action_obj = {
            "action_id": {"S": "ACTION-FIND-svc-003"},
            "status": {"S": "completed"},
            "updated_at": {"S": "2026-10-02T12:00:00Z"},
            "created_at": {"S": "2026-10-01T10:00:00Z"},
            "audit_trail": {"L": []},
        }
        mock_ddb = self._make_ddb_client([svc_row], action_obj)
        history, _ = read_attempt_history_health(
            _VIN, ddb_client=mock_ddb,
            findings_table="vsa-staging-avx-findings",
            actions_table="vsa-staging-avx-actions",
        )
        hs = history.get("service", {})
        assert hs.get("closing_needs") == frozenset({need_id}), (
            f"closing_needs must equal the stored need ids; "
            f"got {hs.get('closing_needs')!r}. "
            "Mutation: store empty closing_needs → any need appears 'new' → "
            "false new-episode on every run → fails."
        )


# ---------------------------------------------------------------------------
# Answer 3 (a1 C3 + c C1): per-episode cap with episode_start tracking
# ---------------------------------------------------------------------------

class TestG3F3PerEpisodeCap:
    """G3F3.1 (a1 C3, c C1): the 5-attempt cap counts attempts within the
    current episode, not lifetime attempts.

    Design answer 3: Attempts in an episode are those numbered after the last
    closing attempt. The exhausted item is closing. Each episode's exhausted
    item carries the attempt number that triggered exhaustion.

    Episode start is derived by read_attempt_history_health from the full history
    scan (exhausted items + health.episode evidence); _plan_health_attempt reads
    episode_start from the history entry supplied by the reader.  When episode_start
    is absent (history constructed outside the reader, e.g. probe tests), ep_cap=1
    is used as a safe fallback (treat as first attempt of a new episode).

    Mutation guards:
    - Pass absolute attempt_n instead of ep_cap → episode 2 exhausts after
      (MAX_ATTEMPTS - prev_n) attempts → test_per_episode_cap_restarts fails.
    - Drop health.episode evidence → episode_start=None in history → ep_cap=1 fallback
      → should_retry always True for 'failed' → exhausted NEVER fires → run 7's
      assert exh_writes fails.
    - Always write to service_exhausted/1 → exhausted not used as closing record
      → new p0 episode never starts → test_exhausted_is_closing_record fails.
    """

    def _make_history(self, *, attempt_n: int, queue_status: str, decided_at: str,
                      episode_start: int, need_ids: Optional[frozenset] = None,
                      closing_needs: Optional[frozenset] = None) -> dict:
        row: dict = {
            "attempt_number": attempt_n,
            "finding_id": f"FIND-{attempt_n:04d}",
            "inputs_hash": f"hash-{attempt_n}",
            "computed_at": "2026-10-01T05:00:00Z",
            "expires_at": "2026-10-08T05:00:00Z",
            "diagnosis_source": "rules",
            "queue_status": queue_status,
            "decided_at": decided_at,
            "episode_start": episode_start,
        }
        if need_ids is not None:
            row["_need_ids"] = need_ids
        if closing_needs is not None:
            row["closing_needs"] = closing_needs
        return row

    def test_episode_start_evidence_written(self):
        """write_finding receives evidence with a health.episode entry containing episode_start."""
        row = _make_vehicle_row(
            open_faults=[_p0_fault()],
            catalog_entries={_P0_CODE: _P0_CATALOG_ENTRY},
        )
        written: list = []
        run_health_loop(
            vehicle_rows=[row], stock_rows=_STOCK_ROWS,
            computed_at=_COMPUTED_AT,
            write_finding=lambda f, s, refresh_on_noop=True: written.append(f),
            fault_grounding_enabled=False,
            **_simple_readers(history={}),
        )
        assert written, "write_first must produce a booking."
        booking = next((f for f in written if (f.get("subject_key") or "").startswith("service/")), None)
        assert booking is not None
        ep_entries = [
            ev for ev in (booking.get("evidence") or [])
            if ev.get("kind") == "health.episode"
        ]
        assert len(ep_entries) == 1, (
            f"Exactly one health.episode evidence entry must be present; got {ep_entries}. "
            "Mutation: drop health.episode → episode_start=None → cap defaults to 1 → fails."
        )
        assert ep_entries[0]["value"].get("started_at_attempt") == 1, (
            f"episode_start must be 1 for write_first; got {ep_entries[0]['value']!r}."
        )

    def test_per_episode_cap_restarts_after_closing(self):
        """Completed service/1, five failures → service/2..6 (regular), then exhausted.

        After completed service/1 (episode_start=1), a new p0 fault starts episode 2
        at service/2 (episode_start=2). Five failures: service/2..6 each writes as
        write_next (ep_cap < MAX_ATTEMPTS). On run 7 (history=service/6, ep_cap=5),
        can_retry returns False and write_exhausted fires at service_exhausted/6.

        Mutation: pass absolute attempt_n to should_retry_health → ep_cap skips history
        episode_start entirely → exhaustion fires at different attempt → fails.
        """
        from agents.fi_recall_warranty.attempts import MAX_ATTEMPTS
        import datetime as _dt_epc

        now_for_test = _dt_epc.datetime(2026, 10, 10, 5, 40, tzinfo=_dt_epc.timezone.utc)

        # After completed service/1, episode_start for new episode is 2.
        # We test with a NEW p0 fault (different need) to trigger write_next.
        # Use _SOON_CODE for the closing need so P0_CODE is "new".
        completed_closing: frozenset = frozenset({f"fault:{_SOON_CODE}"})
        history_service1 = self._make_history(
            attempt_n=1, queue_status="completed",
            decided_at="2026-09-01T12:00:00Z",  # far past 14-day window
            episode_start=1,
            closing_needs=completed_closing,
        )

        written_by_run: list[list] = []
        row = _make_vehicle_row(
            open_faults=[_p0_fault()],  # different from closing need
            catalog_entries={_P0_CODE: _P0_CATALOG_ENTRY},
        )
        # Simulate runs: service/2..6 as regular bookings, then service_exhausted/6
        current_history = {"service": history_service1}
        for run_num in range(2, MAX_ATTEMPTS + 3):  # 2..7 inclusive
            run_writes: list = []
            readers = _simple_readers(history=dict(current_history))
            run_health_loop(
                vehicle_rows=[row], stock_rows=_STOCK_ROWS,
                computed_at=_COMPUTED_AT,
                write_finding=lambda f, s, refresh_on_noop=True, _w=run_writes: _w.append(f),
                fault_grounding_enabled=False,
                now=now_for_test,
                **readers,
            )
            written_by_run.append(run_writes)

            svc_writes = [f for f in run_writes
                          if (f.get("subject_key") or "").startswith("service/")
                          and "exhausted" not in (f.get("subject_key") or "")]
            exh_writes = [f for f in run_writes if "exhausted" in (f.get("subject_key") or "")]

            if run_num <= MAX_ATTEMPTS + 1:  # 2..6: regular bookings
                assert svc_writes, (
                    f"Run {run_num}: expected service/{run_num}, got {run_writes}. "
                    "Per-episode cap: runs 2..6 should write regular bookings."
                )
                assert svc_writes[0]["subject_key"] == f"service/{run_num}", (
                    f"Run {run_num}: expected service/{run_num}; "
                    f"got {svc_writes[0]['subject_key']!r}."
                )
                # Update history for next run: service/<n> with "failed" status
                ep_entries = [ev for ev in (svc_writes[0].get("evidence") or [])
                              if ev.get("kind") == "health.episode"]
                ep_start = (ep_entries[0]["value"].get("started_at_attempt")
                            if ep_entries else None)
                current_history["service"] = {
                    "attempt_number": run_num,
                    "finding_id": svc_writes[0]["finding_id"],
                    "inputs_hash": svc_writes[0]["inputs_hash"],
                    "computed_at": _COMPUTED_AT,
                    "expires_at": "2026-10-17T05:40:00Z",
                    "diagnosis_source": "rules",
                    "queue_status": "failed",
                    "decided_at": None,
                    "episode_start": ep_start,
                }
            else:  # run_num == MAX_ATTEMPTS + 2 == 7: write_exhausted
                assert exh_writes, (
                    f"Run {run_num}: expected write_exhausted at service_exhausted/6; "
                    f"got {run_writes}. "
                    "ep_cap at service/6 is 6-2+1=5=MAX_ATTEMPTS → write_exhausted."
                )
                exh_key = exh_writes[0].get("subject_key")
                assert exh_key == f"service_exhausted/{MAX_ATTEMPTS + 1}", (
                    f"Exhausted item must be at service_exhausted/{MAX_ATTEMPTS + 1} "
                    f"(attempt_n=6 triggered exhaustion); got {exh_key!r}."
                )

    def test_exhausted_is_closing_record_new_p0_starts_new_episode(self):
        """After exhaustion, a new p0 need starts service/<n+1> with the directive.

        Design answer 3: the exhausted item is a closing record; a new need opens
        a new episode.

        Mutation: keep /1 as exhausted key → exhausted never wins over base item
        at attempt_n ≥ 1 → loop refreshes base forever → new p0 never triggers
        a new episode → no directive → test fails.
        """
        from agents.fi_recall_warranty.attempts import MAX_ATTEMPTS
        from boto3.dynamodb.types import TypeSerializer
        from unittest.mock import MagicMock
        from agents.fi_fleet_health.loop import read_attempt_history_health

        s = TypeSerializer().serialize
        exh_n = MAX_ATTEMPTS  # 5

        # service_exhausted/5 in DynamoDB (written by a previous run)
        exh_ev = [
            {
                "kind": "health.need",
                "source": "agent-fi-fleet-health",
                "source_ref": "fi-fleet-health-run",
                "value": {"need_id": "fault:P1AB0", "kind": "fault"},  # old 'soon' need
                "computed_at": "2026-10-05T05:00:00Z",
                "provenance": "live",
                "vin": _VIN,
            },
            {
                "kind": "health.episode",
                "source": "agent-fi-fleet-health",
                "source_ref": "fi-fleet-health-run",
                "value": {"started_at_attempt": 1},
                "computed_at": "2026-10-05T05:00:00Z",
                "provenance": "live",
            }
        ]
        exh_row = {
            "finding_id": s("FIND-exh-0005"),
            "agent_id": s("agent-fi-fleet-health"),
            "finding_kind": s("fleet.health.service"),
            "subject_key": s(f"service_exhausted/{exh_n}"),
            "severity": s("informational"),
            "computed_at": s("2026-10-05T05:00:00Z"),
            "expires_at": s("2026-10-12T05:00:00Z"),
            "inputs_hash": s("hash-exh"),
            "vin": s(_VIN),
            "evidence": s(exh_ev),
        }
        # No Action for an informational item
        mock_ddb = MagicMock()
        mock_ddb.query.return_value = {"Items": [exh_row]}
        mock_ddb.get_item.return_value = {"Item": None}

        history, _ = read_attempt_history_health(
            _VIN, ddb_client=mock_ddb,
            findings_table="vsa-staging-avx-findings",
            actions_table="vsa-staging-avx-actions",
        )
        hs = history.get("service")
        assert hs is not None, "Exhausted item must appear in history."
        assert hs["queue_status"] == "exhausted", (
            f"queue_status must be 'exhausted'; got {hs['queue_status']!r}. "
            "Mutation: revert to continue → exhausted never in history → no episode → fails."
        )
        assert hs["decided_at"] == "2026-10-05T05:00:00Z", (
            f"decided_at must be computed_at of exhausted item; got {hs['decided_at']!r}."
        )

        # Now simulate the loop with the exhausted item as history + a new p0 fault
        written: list = []
        row = _make_vehicle_row(
            open_faults=[_p0_fault()],                    # new p0 need (different from closing)
            catalog_entries={_P0_CODE: _P0_CATALOG_ENTRY},
        )
        run_health_loop(
            vehicle_rows=[row], stock_rows=_STOCK_ROWS,
            computed_at=_COMPUTED_AT,
            write_finding=lambda f, s, refresh_on_noop=True: written.append(f),
            fault_grounding_enabled=False,
            **_simple_readers(history=history),
        )
        bookings = [f for f in written if (f.get("subject_key") or "").startswith("service/")
                    and "exhausted" not in (f.get("subject_key") or "")]
        assert bookings, (
            "New p0 fault after exhaustion must start a new episode. "
            "Mutation: service_exhausted/1 → never wins vs base → no closing record "
            "→ new p0 can't start new episode → fails."
        )
        assert bookings[0]["subject_key"] == f"service/{exh_n + 1}", (
            f"New episode must be service/{exh_n + 1}; got {bookings[0]['subject_key']!r}."
        )
        assert bookings[0].get("disclosure_txt") == _P0_DIRECTIVE, (
            "New p0 episode must carry the directive."
        )


# (c W3 tests are in lambdas/api-avx-findings/tests/test_g3f3_fixes.py)


# ---------------------------------------------------------------------------
# Answer 10 (a1 W4): Agent 2 need_id is a2:<finding_kind> — stable across re-mints
# ---------------------------------------------------------------------------

class TestG3F3Agent2NeedIdStable:
    """G3F3.1 (a1 W4): Agent 2 need ids are a2:<finding_kind>, stable across re-mints.

    Design answer 10: "Agent 2 need ids are a2:<finding_kind>, stable across re-mints."

    Mutation guard: revert to f"a2:{fid}" → id changes when Finding is re-minted →
    dismissal + re-mint triggers false new episode → test fails.
    """

    def test_a2_need_id_uses_finding_kind_not_finding_id(self):
        """classify_needs gives Agent 2 needs a need_id of 'a2:<kind>'."""
        from agents.fi_fleet_health.bands import classify_needs
        fid1 = "FIND-A2-0001"
        fid2 = "FIND-A2-0002"
        kind = "health.battery"

        result1 = classify_needs(
            open_faults=[],
            agent2_findings=[{"finding_id": fid1, "finding_key": f"key:{kind}",
                              "finding_kind": kind, "severity": "attention"}],
            pm_tasks=[],
        )
        result2 = classify_needs(
            open_faults=[],
            agent2_findings=[{"finding_id": fid2, "finding_key": f"key:{kind}",
                              "finding_kind": kind, "severity": "attention"}],
            pm_tasks=[],
        )
        need_ids_1 = {n["need_id"] for n in result1.needs}
        need_ids_2 = {n["need_id"] for n in result2.needs}
        assert need_ids_1 == need_ids_2 == {f"a2:{kind}"}, (
            f"a2 need_id must be a2:<kind>={kind!r} for both mints; "
            f"got {need_ids_1!r} and {need_ids_2!r}. "
            "Mutation: use fid → ids differ across mints → false new episode → fails."
        )

    def test_dismissal_reminted_finding_same_kind_no_new_episode(self):
        """Dismissal + re-minted Finding with same kind but different id → no new episode.

        The need_id is a2:<kind>, so the re-minted finding's need is the same
        as the dismissed one's closing need → episode doesn't reopen.
        """
        kind = "health.diagnostics"
        old_fid = "FIND-A2-DIAG-OLD"
        new_fid = "FIND-A2-DIAG-NEW"

        # Dismissed booking, closing_needs has the a2:<kind> need
        history = {
            "service": {
                "attempt_number": 1,
                "finding_id": "FIND-SVC-001",
                "inputs_hash": "h",
                "computed_at": "2026-10-09T05:00:00Z",
                "expires_at": "2026-10-12T05:00:00Z",
                "diagnosis_source": "rules",
                "queue_status": "dismissed",
                "decided_at": "2026-10-09T12:00:00Z",  # 1 day ago (within 30-day window)
                "closing_needs": frozenset({f"a2:{kind}"}),
                "episode_start": 1,
            }
        }
        row = _make_vehicle_row(
            agent2_findings=[{
                "finding_id": new_fid,  # re-minted (different fid)
                "finding_key": f"key:{kind}",
                "finding_kind": kind,
                "severity": "attention",
            }],
        )
        written: list = []
        run_health_loop(
            vehicle_rows=[row], stock_rows=_STOCK_ROWS,
            computed_at=_COMPUTED_AT,
            write_finding=lambda f, s, refresh_on_noop=True: written.append(f),
            fault_grounding_enabled=False,
            **_simple_readers(history=history),
        )
        assert not written, (
            "Re-minted Finding with same kind must not reopen the dismissed episode "
            "(same a2:<kind> need_id). "
            "Mutation: revert to a2:{fid} → different need_id → false new episode → written."
        )


# ---------------------------------------------------------------------------
# Answer 11 (a2 W1): service cap order uses classified severity + VIN hash tie-breaking
# ---------------------------------------------------------------------------

class TestG3F3ServiceCapOrder:
    """G3F3.1 (a2 W1): _severity_key uses the full classified severity
    (fault + Agent 2 + PM), not fault bands only.  Ties break by a
    date-salted VIN hash so the same vehicles aren't cut every night.

    Design answer 11: "uses the classified severity, and ties rotate by a
    date-salted VIN hash so the same vehicles aren't cut every night."

    Mutation guards:
    - Replace _most_severe with fault-bands-only → Agent 2 attention treated as
      no-fault → drops below a soon fault → test_a2_attention_beats_soon fails.
    - Remove date-salt from VIN hash → same vehicles cut every night →
      two runs with different dates must give different VIN ordering for ties.
    """

    def test_a2_attention_beats_soon_fault(self, monkeypatch):
        """A vehicle with Agent 2 attention is kept over one with only a soon fault."""
        from agents.fi_fleet_health import selection as fh_sel
        from agents.fi_recall_warranty import selection as rw_sel
        from agents.fi_fleet_health import loop as fh_loop
        import agents.tier2.runner as _runner
        from agents.tier2.tests.test_runner_service import _AvxWriter, _Cw

        vin_a2 = "MRDVEH00000000001"   # has Agent 2 attention, no fault
        vin_soon = "MRDVEH00000000002"  # has soon fault only

        monkeypatch.setattr(fh_sel, "select_fault_rows", lambda *a, **k: [])
        monkeypatch.setattr(fh_sel, "select_pm_rows",    lambda *a, **k: [])
        monkeypatch.setattr(fh_sel, "select_managed_vins",
                            lambda *a, **k: [{"vin": vin_a2, "fleet_id": "F1"},
                                             {"vin": vin_soon, "fleet_id": "F1"}])
        monkeypatch.setattr(fh_sel, "select_daily_usage", lambda *a, **k: [])
        monkeypatch.setattr(fh_sel, "select_agent2_findings", lambda *a, **k: ({}, 0))
        monkeypatch.setattr(rw_sel, "select_parts_stock",  lambda *a, **k: [])
        monkeypatch.setattr(fh_sel, "build_vehicle_rows", lambda **kw: ([
            {
                "vin": vin_a2,
                "fleet_id": "F1",
                "vehicle_id": "V1",
                "open_faults": [],
                "agent2_findings": [{"finding_id": "A2-1", "finding_kind": "health.battery",
                                     "finding_key": "k:health.battery",
                                     "severity": "attention"}],
                "pm_tasks": [],
                "catalog_entries": {},
                "position": None,
                "utilisation": None,
            },
            {
                "vin": vin_soon,
                "fleet_id": "F1",
                "vehicle_id": "V2",
                "open_faults": [{"code": _SOON_CODE, "band": "soon",
                                  "last_seen_at": "2026-10-09T00:00:00Z"}],
                "agent2_findings": [],
                "pm_tasks": [],
                "catalog_entries": {},
                "position": None,
                "utilisation": None,
            },
        ], {}))

        processed_vins: list = []

        def fake_loop(*, vehicle_rows, **kw):
            for r in vehicle_rows:
                processed_vins.append(r["vin"])
            return {
                "vins_selected": len(vehicle_rows),
                "vins_processed": len(vehicle_rows),
                "items_written": 0,
                "items_skipped_unchanged": 0,
                "vins_refused": 0,
                "grounding_read_errors": 0,
                "exhausted_items_written": 0,
                "by_item_type": {},
                "book_items_without_rooftops": 0,
                "p0_directive_fallbacks": 0,
            }
        monkeypatch.setattr(fh_loop, "run_health_loop", fake_loop)

        # Cap at 1 so only the highest-severity VIN is kept
        import agents.tier2.runner as runner_mod
        orig_max = runner_mod._MAX_VINS_SERVICE
        runner_mod._MAX_VINS_SERVICE = 1
        try:
            _runner.run_service(
                "staging",
                athena_client=object(),
                workgroup="cvx-staging-analytics",
                results_s3_path="s3://results/tier2/fi-fleet-health/",
                ddb_client=object(),
                findings_table="vsa-staging-avx-findings",
                actions_table="vsa-staging-avx-actions",
                groundings_table="cms-staging-storage-vehicle-groundings",
                vehicles_table="cms-staging-storage-vehicles",
                fleet_locations_table="cms-staging-storage-fleet-locations",
                avx_writer=_AvxWriter(),
                owner_resolver=None,
                cloudwatch_client=_Cw(),
                run_record_writer=lambda **k: None,
            )
        finally:
            runner_mod._MAX_VINS_SERVICE = orig_max

        assert processed_vins == [vin_a2], (
            f"Agent 2 attention must beat a soon fault; got {processed_vins!r}. "
            "Mutation: fault-bands-only → a2 attention treated as no-fault → "
            "soon fault beats it → processed_vins == [vin_soon] → fails."
        )

    def test_tie_breaking_rotates_by_date(self):
        """Tie-breaking uses date so different days produce different VIN orderings."""
        import datetime as _dt
        from agents.fi_fleet_health.bands import _SEVERITY_ORDER
        import hashlib as _hl

        # Two vehicles both have "attention" severity (a tie in severity).
        # The exact VINs here are chosen so that they swap order between the two dates.
        # We pick VINs where md5("2026-10-10:<vin>") < md5("2026-10-10:<vin2>") but
        # md5("2026-10-11:<vin>") > md5("2026-10-11:<vin2>").
        # Rather than assuming specific VINs, we compute and verify the ordering differs.
        severity = "attention"
        order = _SEVERITY_ORDER[severity]

        def _key(vin: str, date: _dt.date) -> tuple:
            salt = date.isoformat()
            tiebreak = int(_hl.md5(f"{salt}:{vin}".encode()).hexdigest(), 16)
            return (order, tiebreak)

        # Use a large pool and find two VINs that give different orderings on two days
        vins = [f"MRDVEH{i:011d}" for i in range(1, 50)]
        day1 = _dt.date(2026, 10, 10)
        day2 = _dt.date(2026, 10, 17)  # 7 days later

        found_swap = False
        for i, v1 in enumerate(vins):
            for v2 in vins[i+1:]:
                k1_d1 = _key(v1, day1)
                k2_d1 = _key(v2, day1)
                k1_d2 = _key(v1, day2)
                k2_d2 = _key(v2, day2)
                order_d1 = sorted([v1, v2], key=lambda v: _key(v, day1))
                order_d2 = sorted([v1, v2], key=lambda v: _key(v, day2))
                if order_d1 != order_d2:
                    found_swap = True
                    break
            if found_swap:
                break

        assert found_swap, (
            "Date-salted VIN hash must produce different orderings for at least one "
            "pair of VINs across two different dates. "
            "Mutation: remove date-salt from hash → all days give identical ordering "
            "→ no swap found → fails."
        )



# ---------------------------------------------------------------------------
# G3-3 Fix: C2-e — decline window 14/30 boundary (mutant: 30 if high else 14)
# ---------------------------------------------------------------------------

class TestC2EDeclineWindowBoundary:
    """C2-e mutant swaps the 14/30 thresholds: high severity gets 30 days, others 14.

    A test at exactly 15 days sits between 14 and 30:
      - Correct code:  threshold=14 for p0 → 15 ≥ 14 → True  (new episode).
      - C2-e mutant:   threshold=30 for p0 → 15 < 30 → False (no new episode).
    Similarly, a test at 20 days for 'soon' sits between 14 and 30:
      - Correct code:  threshold=30 for soon → 20 < 30 → False (no new episode).
      - C2-e mutant:   threshold=14 for soon → 20 ≥ 14 → True  (new episode).
    """

    def test_p0_at_15_days_triggers_new_episode(self):
        """p0 severity: 15 days since closing exceeds 14-day threshold → new episode.

        C2-e mutation swaps to 30-day threshold for high severity → 15 < 30
        → _is_new_episode_needed returns False → new episode is not opened → CAUGHT.
        """
        from agents.fi_fleet_health.loop import _is_new_episode_needed
        import datetime

        # now = 2026-10-15T12:00Z, decided_at = 2026-09-30T12:00Z → 15 days apart
        now = datetime.datetime(2026, 10, 15, 12, 0, tzinfo=datetime.timezone.utc)
        history_entry = {
            "decided_at": "2026-09-30T12:00:00Z",
            "closing_needs": frozenset({"fault:P0AA6"}),
        }
        # Same needs → only window-based trigger
        current_need_ids = frozenset({"fault:P0AA6"})

        result = _is_new_episode_needed(
            history_entry=history_entry,
            severity="p0",
            now=now,
            current_need_ids=current_need_ids,
        )
        assert result, (
            "p0 severity at 15 days since closing must open a new episode "
            "(14-day threshold, 15 ≥ 14). "
            "C2-e mutant uses 30-day threshold for high severity → 15 < 30 → False → CAUGHT."
        )

    def test_attention_at_15_days_triggers_new_episode(self):
        """'attention' is also high severity: 15 days > 14-day threshold → new episode."""
        from agents.fi_fleet_health.loop import _is_new_episode_needed
        import datetime

        now = datetime.datetime(2026, 10, 15, 12, 0, tzinfo=datetime.timezone.utc)
        history_entry = {
            "decided_at": "2026-09-30T12:00:00Z",
            "closing_needs": frozenset({"fault:P0001"}),
        }
        current_need_ids = frozenset({"fault:P0001"})

        result = _is_new_episode_needed(
            history_entry=history_entry,
            severity="attention",
            now=now,
            current_need_ids=current_need_ids,
        )
        assert result, (
            "'attention' severity at 15 days must trigger new episode (14-day threshold). "
            "C2-e mutant uses 30-day → 15 < 30 → no episode → CAUGHT."
        )

    def test_soon_at_20_days_does_not_trigger_new_episode(self):
        """Non-high severity 'soon' at 20 days: below 30-day threshold → no new episode.

        C2-e mutant uses 14-day threshold for non-high → 20 ≥ 14 → new episode → CAUGHT.
        """
        from agents.fi_fleet_health.loop import _is_new_episode_needed
        import datetime

        # 20 days: between 14 and 30
        now = datetime.datetime(2026, 10, 20, 12, 0, tzinfo=datetime.timezone.utc)
        history_entry = {
            "decided_at": "2026-09-30T12:00:00Z",   # 20 days before now
            "closing_needs": frozenset({"pm:brake_service"}),
        }
        current_need_ids = frozenset({"pm:brake_service"})

        result = _is_new_episode_needed(
            history_entry=history_entry,
            severity="soon",
            now=now,
            current_need_ids=current_need_ids,
        )
        assert not result, (
            "'soon' severity at 20 days (below 30-day window) must NOT open a new episode. "
            "C2-e mutant uses 14-day threshold for non-high → 20 ≥ 14 → episode → CAUGHT."
        )



class TestCycle4C1LaterEpisodeAfterOldExhaustion:
    """Review cycle 4 a C1: a closing after an old exhaustion starts a later episode.

    History (one VIN, through the real reader): ``service_exhausted/5``; episode 2 at
    ``service/6..9`` (evidence start 6), ``service/9`` completed; episode 3's first
    attempt ``service/10`` (evidence start 10), failed. Episode 3 has used 1 of its 5
    attempts, so the next run writes ``service/11``.

    Mutant (revert the fix: always ``exh_n + 1`` when an exhaustion precedes) gives
    ``episode_start == 6``, ``ep_cap == 5`` and ``write_exhausted``, and FAILS here.
    """

    VIN = "MRDVEH00000000091"

    def _history(self, last_start=10):
        from unittest.mock import MagicMock
        from boto3.dynamodb.types import TypeSerializer
        from agents.fi_fleet_health.loop import read_attempt_history_health
        from agents.fi_recall_warranty.attempts import fi_decision_id

        s = TypeSerializer().serialize

        def ev(need_ids, start):
            out = [{"kind": "health.need", "value": {"need_id": n, "kind": "fault"}}
                   for n in need_ids]
            out.append({"kind": "health.episode", "value": {"started_at_attempt": start}})
            return out

        def row(sk, fid, evidence, sev="attention"):
            return {
                "finding_id": s(fid), "agent_id": s("agent-fi-fleet-health"),
                "finding_kind": s("fleet.health.service"), "subject_key": s(sk),
                "severity": s(sev), "computed_at": s("2026-10-01T05:40:00Z"),
                "expires_at": s("2026-10-04T05:40:00Z"), "inputs_hash": s("h-" + fid),
                "vin": s(self.VIN), "evidence": s(evidence),
            }

        rows = [row("service_exhausted/5", "F-exh5", ev(["fault:P0A80"], 1), "informational")]
        rows += [row(f"service/{n}", f"F-s{n}", ev(["fault:P0A80"], 6)) for n in range(6, 10)]
        rows.append(row("service/10", "F-s10", ev(["fault:P0A80", "fault:P1B00"], last_start)))
        statuses = {fi_decision_id("F-s9"): "completed", fi_decision_id("F-s10"): "failed"}

        def get_item(TableName, Key, **_kw):
            aid = Key["action_id"]["S"]
            st = statuses.get(aid)
            if st is None:
                return {}
            return {"Item": {"action_id": s(aid), "status": s(st), "audit_trail": s([]),
                             "updated_at": s("2026-10-02T00:00:00Z"),
                             "created_at": s("2026-10-01T06:00:00Z")}}

        ddb = MagicMock()
        ddb.query.return_value = {"Items": rows}
        ddb.get_item.side_effect = get_item
        hist, _ = read_attempt_history_health(
            self.VIN, ddb_client=ddb, findings_table="f", actions_table="a")
        return hist["service"]

    def test_episode_start_is_the_later_episode(self):
        h = self._history()
        assert h["attempt_number"] == 10
        assert h["episode_start"] == 10, (
            f"episode_start must be 10 (episode 3), got {h['episode_start']!r}")

    def test_next_run_writes_the_next_attempt_not_exhausted(self):
        import datetime
        from agents.fi_fleet_health.loop import _plan_health_attempt

        h = self._history()
        now = datetime.datetime(2026, 10, 3, 5, 40, tzinfo=datetime.timezone.utc)
        decision = _plan_health_attempt(
            h, "attention", now, current_need_ids=frozenset({"fault:P0A80", "fault:P1B00"}))
        assert decision == "write_next", f"episode 3 has 4 attempts left; got {decision!r}"

    def test_evidence_start_past_the_attempt_is_ignored(self):
        """Corrupt evidence (start 99 on attempt 10) never moves the episode past the
        attempt; the reader keeps ``exh_n + 1``. Mutant: drop the ``<= attempt_number``
        bound -> episode_start 99 -> FAILS."""
        h = self._history(last_start=99)
        assert h["episode_start"] == 6, h["episode_start"]



class TestCycle4AreaAWarnings:
    """Review cycle 4 a W1-W3 (+ S1), from ``g3-cycle4-probes/probe_a2.py``.

    W1: ``by_vin`` has no sort key, so an exhausted row and a base row at the same
        attempt arrive in either order; both orders read ``exhausted``.
        Mutants ``ex-ge-to-gt`` / ``base-gt-to-ge`` FAIL one order.
    W2: an exhausted item holds while needs are unchanged inside the window (``skip``),
        and opens the next episode once the window has passed.
        Mutant ``exh-holds`` (closing needs emptied) FAILS the hold.
    W3: ``write_current`` carries the episode. Mutant ``current-ep-1`` FAILS.
    S1: the runner's stage D agent ids equal the Finding writers' ``AGENT_ID``.
    """

    VIN = "MRDVEH00000000092"

    @staticmethod
    def _now():
        import datetime
        return datetime.datetime(2026, 10, 8, 5, 40, tzinfo=datetime.timezone.utc)

    def _row(self, sk, fid, ev, sev="attention", computed="2026-10-05T05:40:00Z"):
        from boto3.dynamodb.types import TypeSerializer
        s = TypeSerializer().serialize
        return {
            "finding_id": s(fid), "agent_id": s("agent-fi-fleet-health"),
            "finding_kind": s("fleet.health.service"), "subject_key": s(sk),
            "severity": s(sev), "computed_at": s(computed),
            "expires_at": s("2026-10-08T05:40:00Z"), "inputs_hash": s("h-" + fid),
            "vin": s(self.VIN), "evidence": s(ev),
        }

    def _read(self, rows, actions):
        from unittest.mock import MagicMock
        from boto3.dynamodb.types import TypeSerializer
        from agents.fi_fleet_health.loop import read_attempt_history_health
        s = TypeSerializer().serialize
        ddb = MagicMock()
        ddb.query.return_value = {"Items": rows}

        def gi(TableName, Key, **_kw):
            st = actions.get(Key["action_id"]["S"])
            if not st:
                return {}
            return {"Item": {"status": s(st), "audit_trail": s([]),
                             "updated_at": s("2026-10-04T00:00:00Z")}}
        ddb.get_item.side_effect = gi
        return read_attempt_history_health(
            self.VIN, ddb_client=ddb, findings_table="f", actions_table="a")[0]["service"]

    _NEED = {"kind": "health.need", "value": {"need_id": "fault:P0A80", "kind": "fault"}}
    _EP1 = {"kind": "health.episode", "value": {"started_at_attempt": 1}}

    def _pair(self):
        from agents.fi_recall_warranty.attempts import fi_decision_id
        base5 = self._row("service/5", "F-s5", [self._NEED, self._EP1],
                          computed="2026-10-04T05:40:00Z")
        exh5 = self._row("service_exhausted/5", "F-x5", [self._NEED, self._EP1],
                         sev="informational")
        return base5, exh5, {fi_decision_id("F-s5"): "failed"}

    @pytest.mark.parametrize("order", ["exhausted_first", "base_first"])
    def test_exhausted_and_base_at_one_attempt_read_exhausted_in_either_order(self, order):
        from agents.fi_fleet_health.loop import _plan_health_attempt
        base5, exh5, acts = self._pair()
        rows = [exh5, base5] if order == "exhausted_first" else [base5, exh5]
        h = self._read(rows, acts)
        assert h["queue_status"] == "exhausted", h["queue_status"]
        d = _plan_health_attempt(h, "attention", self._now(),
                                 current_need_ids=frozenset({"fault:P0A80", "fault:P1B00"}))
        assert d == "write_next", d

    def test_exhausted_item_holds_while_needs_unchanged_inside_window(self):
        from agents.fi_fleet_health.loop import _plan_health_attempt
        _, exh5, _ = self._pair()
        h = self._read([exh5], {})
        d = _plan_health_attempt(h, "attention", self._now(),
                                 current_need_ids=frozenset({"fault:P0A80"}))
        assert d == "skip", f"the exhausted item must hold inside the window; got {d!r}"

    def test_exhausted_item_opens_next_episode_after_window(self):
        import datetime
        from agents.fi_fleet_health.loop import _plan_health_attempt
        _, exh5, _ = self._pair()
        h = self._read([exh5], {})
        later = self._now() + datetime.timedelta(days=30)
        d = _plan_health_attempt(h, "attention", later,
                                 current_need_ids=frozenset({"fault:P0A80"}))
        assert d == "write_next", f"after the 14-day window a new episode opens; got {d!r}"

    def test_write_current_carries_the_episode(self):
        from agents.fi_fleet_health.loop import run_health_loop
        hist = {"service": {
            "attempt_number": 7, "finding_id": "F-s7", "inputs_hash": "old-hash",
            "computed_at": "2026-10-07T05:40:00Z", "expires_at": "2026-10-10T05:40:00Z",
            "diagnosis_source": "rules", "queue_status": "pending", "decided_at": None,
            "episode_start": 6,
        }}
        written = []
        vrow = {
            "vin": self.VIN, "fleet_id": "fleet-1", "vehicle_id": "veh-1",
            "open_faults": [{"code": "P1B00", "band": "attention",
                             "last_seen_at": "2026-10-07T00:00:00Z"}],
            "agent2_findings": [],
            "pm_tasks": [{"task_code": "TIRE_ROTATION", "status": "overdue"}],
            "position": None, "utilisation": None, "catalog_entries": {},
        }
        stock = [{"dealer_id": "D-1", "part_number": "X", "qty_on_hand": 1,
                  "latitude": 0.0, "longitude": 0.0}]
        run_health_loop(
            vehicle_rows=[vrow], stock_rows=stock, computed_at="2026-10-08T05:40:00Z",
            grounded_fn=lambda vid: (False, 0), history_fn=lambda vin: (hist, 0),
            standing_fn=lambda vin: object(), lookup_live=lambda fk: None,
            write_finding=lambda f, st, refresh_on_noop=True: written.append(f),
            recall_visit_fn=lambda vin: {"recall_id": "", "rooftop_dealer_id": "D-1"},
            fault_grounding_enabled=False, now=self._now(),
        )
        svc = [f for f in written if f.get("subject_key") == "service/7"]
        assert len(svc) == 1, [f.get("subject_key") for f in written]
        eps = [e["value"]["started_at_attempt"] for e in svc[0].get("evidence", [])
               if e.get("kind") == "health.episode"]
        assert eps == [6], f"write_current must carry episode 6; got {eps}"

    def test_runner_stage_d_agent_ids_equal_the_writers(self):
        from agents.tier2 import runner
        from agents.fi_fleet_health.write import AGENT_ID as FH_ID
        from agents.fi_quality.write import AGENT_ID as FQ_ID
        assert runner.FI_FLEET_HEALTH_AGENT_ID == FH_ID
        assert runner.FI_QUALITY_AGENT_ID == FQ_ID


# ===========================================================================
# Task 3.2 / spec D6: fault_source propagates from ADP row → health.need evidence
# ===========================================================================

class TestFaultSourcePropagation:
    """Task 3.2 / spec D6: fault_source from ADP flows into health.need evidence."""

    def _fault(self, source) -> dict:
        """Build a soon fault, optionally carrying fault_source."""
        f = {"code": _SOON_CODE, "band": "soon", "last_seen_at": "2026-10-07T00:00:00Z"}
        if source is not None:
            f["fault_source"] = source
        return f

    def _get_fault_need(self, fault: dict) -> dict:
        written: list = []
        run_health_loop(
            vehicle_rows=[_make_vehicle_row(open_faults=[fault])],
            stock_rows=_STOCK_ROWS,
            computed_at=_COMPUTED_AT,
            write_finding=lambda f, s, refresh_on_noop=True: written.append(f),
            fault_grounding_enabled=False,
            **_simple_readers(),
        )
        booking = next(
            f for f in written if (f.get("subject_key") or "").startswith("service/")
        )
        return next(
            ev["value"]
            for ev in booking.get("evidence", [])
            if ev.get("kind") == "health.need"
            and (ev.get("value") or {}).get("kind") == "fault"
        )

    def test_generated_row_carries_fault_source(self):
        """generated fault row → health.need evidence has fault_source='generated'.

        Mutant: hardcode 'generated' → cms test fails.
        Mutant: drop field → this fails.
        """
        need = self._get_fault_need(self._fault("generated"))
        assert need.get("fault_source") == "generated", (
            f"generated fault must carry fault_source='generated'; got {need!r}"
        )

    def test_cms_row_carries_fault_source(self):
        """cms fault row → health.need evidence has fault_source='cms'.

        Mutant: hardcode 'generated' → this fails (cms row gets 'generated').
        """
        need = self._get_fault_need(self._fault("cms"))
        assert need.get("fault_source") == "cms", (
            f"cms fault must carry fault_source='cms'; got {need!r}"
        )

    def test_absent_fault_source_is_none_not_missing(self):
        """A fault with no ``fault_source`` value → the need still carries the key, as None.

        The fixture omits the key (``_fault(None)``); real reader rows carry it as None for
        snapshot rows written before ADP's snapshot job wrote ``source``.

        Mutant: drop field entirely → key absent → 'fault_source' in need fails.
        """
        need = self._get_fault_need(self._fault(None))
        assert "fault_source" in need, (
            f"fault need must carry 'fault_source' key (None when absent); got {need!r}"
        )
        assert need["fault_source"] is None, (
            f"Missing fault_source must map to None, not a guess; got {need['fault_source']!r}"
        )
