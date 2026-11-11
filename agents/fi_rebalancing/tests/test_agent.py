"""Tests for agents/fi_rebalancing/agent.py (spec G4-D10, task 4.1e).

Monkeypatches every sources.read_* to bind against the real signature first.
VINs differ from vehicleIds throughout (fleet Finding scope is fleet_id, not VIN).
"""
from __future__ import annotations

import inspect
from datetime import datetime, timezone

import pytest

from agents.fi_rebalancing import sources as _src
from agents.fi_rebalancing import write as _write
from agents.fi_rebalancing.agent import run_rebalance
from agents.fi_rebalancing.sources import SourceReadError
from agents.tier2.finding_transform import _validate_finding

# ---------------------------------------------------------------------------
# Shared static test data
# ---------------------------------------------------------------------------
_TODAY = "2026-10-12"
_RUN = "2026-10-12"
# effective + horizon window: run_date+1..+7
_EFF = [f"2026-10-{d:02d}" for d in range(13, 20)]
_NOW = datetime(2026, 10, 12, 14, 0, 0, tzinfo=timezone.utc)

_LOC_DEF = {
    "locationId": "loc-def", "name": "Airport Hub", "metro": "test-metro",
    "locationType": "airport", "latitude": 47.40, "longitude": -122.30,
    "parkingSpaces": 20, "operatingHours": "8-18", "op_hours": 10.0,
}
_LOC_SUR = {
    "locationId": "loc-sur", "name": "Downtown Hub", "metro": "test-metro",
    "locationType": "downtown", "latitude": 47.45, "longitude": -122.32,
    "parkingSpaces": 20, "operatingHours": "8-18", "op_hours": 10.0,
}
_LOCATIONS = [_LOC_DEF, _LOC_SUR]

_GAPS = [
    {
        "location_id": "loc-def", "status": "deficit", "vehicles_short": 1,
        "vehicles_spare": 0, "supply_vehicles": 3, "forecast_hours": 45.0,
        "forecast_utilization_pct": 0.95, "data_as_of": _TODAY,
    },
    {
        "location_id": "loc-sur", "status": "surplus", "vehicles_short": 0,
        "vehicles_spare": 2, "supply_vehicles": 10, "forecast_hours": 20.0,
        "forecast_utilization_pct": 0.20, "data_as_of": _TODAY,
    },
]

_FORECAST = (
    [{"location_id": "loc-def", "forecast_date": d, "forecast_hours": 45.0,
      "method": "ml", "mape_28d": 0.10, "run_date": _RUN, "data_as_of": _TODAY}
     for d in _EFF] +
    [{"location_id": "loc-sur", "forecast_date": d, "forecast_hours": 20.0,
      "method": "ml", "mape_28d": 0.10, "run_date": _RUN, "data_as_of": _TODAY}
     for d in _EFF]
)

# VINs ≠ vehicleIds: "VINELIG001" maps to vehicleId "VEH-001" in the vehicles table, etc.
# VINELIG001: eligible (not grounded, no open items, in_service, no upcoming booking/service)
# VINGRND001: grounded → ineligible (U7)
# VINGRERR01: grounding read error → ineligible (U7)
# VINDEF001:  sits at loc-def (provides gap evidence via assigned VINs)
_VEHICLE_DAYS = [
    {"vin": "VINELIG001", "location_id": "loc-sur", "available_hours": 10.0},
    {"vin": "VINGRND001", "location_id": "loc-sur", "available_hours": 10.0},
    {"vin": "VINGRERR01", "location_id": "loc-sur", "available_hours": 10.0},
    {"vin": "VINDEF001",  "location_id": "loc-def", "available_hours": 10.0},
]

_UTILIZATION = [
    {"vin": "VINELIG001", "snapshot_date": _TODAY, "available_hours": 10.0, "on_rent_hours": 0.0},
    {"vin": "VINGRND001", "snapshot_date": _TODAY, "available_hours": 10.0, "on_rent_hours": 0.0},
    {"vin": "VINGRERR01", "snapshot_date": _TODAY, "available_hours": 10.0, "on_rent_hours": 0.0},
]

_DEFAULT_GROUNDING = {
    "VINELIG001": (False, False),   # not grounded, no error
    "VINGRND001": (True,  False),   # grounded
    "VINGRERR01": (None,  True),    # read error
    "VINDEF001":  (False, False),
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_run_kwargs(**overrides) -> dict:
    """Minimal keyword arguments for run_rebalance."""
    base: dict = dict(
        stage="staging",
        athena_client=object(),
        workgroup="test-wg",
        results_s3_path="s3://test/results",
        ddb_client=object(),
        findings_table="findings-tbl",
        vehicles_table="vehicles-tbl",
        groundings_table="groundings-tbl",
        fleet_locations_table="locations-tbl",
        now=_NOW,
    )
    base.update(overrides)
    return base


def _capture_fn():
    """Return (callable, calls_list); callable records each invocation."""
    calls: list[dict] = []

    def fn(finding: dict) -> None:
        calls.append(finding)

    return fn, calls


def _install_patches(
    monkeypatch,
    *,
    gaps=None,
    locations=None,
    forecast=None,
    grounding_map=None,
    open_items_map=None,
    open_ros_rows=None,
    live_hashes: dict | None = None,
    live_metro_hashes: dict | None = None,
    live_rows: dict | None = None,
):
    """Monkeypatch all sources.read_* and write.read_live_hash.

    Each fake first binds its arguments against the real function's signature.
    ``live_rows`` is ``{finding_key: row}`` for ``write.read_live_item``: the rows a
    reused metro's plans and holds are rebuilt from (D8 step 5, prompt 40).
    """
    gaps_data = gaps if gaps is not None else _GAPS
    locs_data = locations if locations is not None else _LOCATIONS
    fc_data = forecast if forecast is not None else _FORECAST
    grounding_map = grounding_map if grounding_map is not None else _DEFAULT_GROUNDING
    open_items_map = open_items_map or {}
    open_ros_data = open_ros_rows if open_ros_rows is not None else []
    live_hashes = live_hashes if live_hashes is not None else {}
    live_metro_hashes = live_metro_hashes if live_metro_hashes is not None else {}
    live_rows = live_rows if live_rows is not None else {}

    def _read_locations(fleet_id, *, ddb_client, table):
        inspect.signature(_src.read_locations).bind(
            fleet_id, ddb_client=ddb_client, table=table)
        return (locs_data, [], set())

    def _read_gaps(*, stage, fleet_id, athena_client, workgroup, results_s3_path):
        inspect.signature(_src.read_gaps).bind(
            stage=stage, fleet_id=fleet_id, athena_client=athena_client,
            workgroup=workgroup, results_s3_path=results_s3_path)
        return gaps_data

    def _read_forecast_days(*, stage, fleet_id, athena_client, workgroup, results_s3_path):
        inspect.signature(_src.read_forecast_days).bind(
            stage=stage, fleet_id=fleet_id, athena_client=athena_client,
            workgroup=workgroup, results_s3_path=results_s3_path)
        return fc_data

    def _read_outcomes(*, stage, fleet_id, athena_client, workgroup, results_s3_path):
        inspect.signature(_src.read_outcomes).bind(
            stage=stage, fleet_id=fleet_id, athena_client=athena_client,
            workgroup=workgroup, results_s3_path=results_s3_path)
        return []

    def _read_vehicle_days(*, stage, fleet_id, athena_client, workgroup, results_s3_path):
        inspect.signature(_src.read_vehicle_days).bind(
            stage=stage, fleet_id=fleet_id, athena_client=athena_client,
            workgroup=workgroup, results_s3_path=results_s3_path)
        return _VEHICLE_DAYS

    def _read_vehicle_utilization(*, stage, fleet_id, athena_client, workgroup, results_s3_path):
        inspect.signature(_src.read_vehicle_utilization).bind(
            stage=stage, fleet_id=fleet_id, athena_client=athena_client,
            workgroup=workgroup, results_s3_path=results_s3_path)
        return _UTILIZATION

    def _read_busy_windows(*, stage, fleet_id, now, athena_client, workgroup, results_s3_path):
        inspect.signature(_src.read_busy_windows).bind(
            stage=stage, fleet_id=fleet_id, now=now, athena_client=athena_client,
            workgroup=workgroup, results_s3_path=results_s3_path)
        return []

    def _read_request_lengths(*, stage, fleet_id, now, athena_client, workgroup, results_s3_path):
        inspect.signature(_src.read_request_lengths).bind(
            stage=stage, fleet_id=fleet_id, now=now, athena_client=athena_client,
            workgroup=workgroup, results_s3_path=results_s3_path)
        return [{"location_id": "loc-def", "mean_request_hours": 24.0}]

    def _read_events(*, stage, today, athena_client, workgroup, results_s3_path):
        inspect.signature(_src.read_events).bind(
            stage=stage, today=today, athena_client=athena_client,
            workgroup=workgroup, results_s3_path=results_s3_path)
        return []

    def _read_open_ros(*, stage, athena_client, workgroup, results_s3_path):
        inspect.signature(_src.read_open_ros).bind(
            stage=stage, athena_client=athena_client,
            workgroup=workgroup, results_s3_path=results_s3_path)
        return open_ros_data

    def _read_service_soon(*, stage, today, athena_client, workgroup, results_s3_path):
        inspect.signature(_src.read_service_soon).bind(
            stage=stage, today=today, athena_client=athena_client,
            workgroup=workgroup, results_s3_path=results_s3_path)
        return []

    def _read_grounding(vin, *, ddb_client, vehicles_table, groundings_table):
        inspect.signature(_src.read_grounding).bind(
            vin, ddb_client=ddb_client,
            vehicles_table=vehicles_table, groundings_table=groundings_table)
        return grounding_map.get(vin, (False, False))

    def _read_open_items(vin, *, ddb_client, findings_table, now_iso):
        inspect.signature(_src.read_open_items).bind(
            vin, ddb_client=ddb_client,
            findings_table=findings_table, now_iso=now_iso)
        return open_items_map.get(vin, ([], False))

    def _read_live_hash(finding_key, *, ddb_client, table_name, now_str):
        inspect.signature(_write.read_live_hash).bind(
            finding_key, ddb_client=ddb_client,
            table_name=table_name, now_str=now_str)
        return live_hashes.get(finding_key)

    def _read_live_item(finding_key, *, ddb_client, table_name, now_str):
        inspect.signature(_write.read_live_item).bind(
            finding_key, ddb_client=ddb_client,
            table_name=table_name, now_str=now_str)
        return live_rows.get(finding_key)

    def _set_metro_inputs_hash(*, finding_key, metro_inputs_hash,
                                ddb_client, table_name, now_str):
        """Mutate the shared ``live_metro_hashes`` dict in-place.

        Mirrors ``agents.fi_rebalancing.write.set_metro_inputs_hash``'s
        contract: the agent writes the attribute on every write path
        (PUT and UPDATE), so a subsequent run reads the current value.
        """
        inspect.signature(_write.set_metro_inputs_hash).bind(
            finding_key=finding_key, metro_inputs_hash=metro_inputs_hash,
            ddb_client=ddb_client, table_name=table_name, now_str=now_str)
        live_metro_hashes[finding_key] = metro_inputs_hash
        return True

    monkeypatch.setattr(_src, "read_locations", _read_locations)
    monkeypatch.setattr(_src, "read_gaps", _read_gaps)
    monkeypatch.setattr(_src, "read_forecast_days", _read_forecast_days)
    monkeypatch.setattr(_src, "read_outcomes", _read_outcomes)
    monkeypatch.setattr(_src, "read_vehicle_days", _read_vehicle_days)
    monkeypatch.setattr(_src, "read_vehicle_utilization", _read_vehicle_utilization)
    monkeypatch.setattr(_src, "read_busy_windows", _read_busy_windows)
    monkeypatch.setattr(_src, "read_request_lengths", _read_request_lengths)
    monkeypatch.setattr(_src, "read_events", _read_events)
    monkeypatch.setattr(_src, "read_open_ros", _read_open_ros)
    monkeypatch.setattr(_src, "read_service_soon", _read_service_soon)
    monkeypatch.setattr(_src, "read_grounding", _read_grounding)
    monkeypatch.setattr(_src, "read_open_items", _read_open_items)
    monkeypatch.setattr(_write, "read_live_hash", _read_live_hash)
    monkeypatch.setattr(_write, "read_live_item", _read_live_item)
    monkeypatch.setattr(_write, "set_metro_inputs_hash", _set_metro_inputs_hash)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestMoveItem:
    """One metro with deficit + surplus → one move finding."""

    def test_move_written_eligible_vins_only(self, monkeypatch):
        """Move finding is written; its vehicle_vins contains only the eligible VIN."""
        _install_patches(monkeypatch)
        write_fn, written = _capture_fn()
        refresh_fn, refreshed = _capture_fn()

        stats = run_rebalance(
            **_make_run_kwargs(write_fn=write_fn, refresh_fn=refresh_fn)
        )

        assert stats["moves_planned"] == 1
        assert stats["move_items_written"] == 1
        assert stats["model_calls"] == 0
        assert len(written) >= 1

        move_finding = next(f for f in written if f.get("finding_kind") == "fleet.rebalance.move")
        vins = move_finding["recommended_action"]["parameters"]["vehicle_vins"]
        assert "VINELIG001" in vins
        assert "VINGRND001" not in vins
        assert "VINGRERR01" not in vins

    def test_every_written_finding_validates(self, monkeypatch):
        """All findings written to write_fn are schema-valid, apart from the agent's
        registered DDB-only attributes.

        Prompt 40: every move and deficit gap item of a planned metro now carries
        ``metro_inputs_hash``, a top-level DynamoDB attribute that is not a schema property
        (``write._AGENT_PROJECTED_ATTRS``; the findings API strips it on read). Before, only
        model-path moves carried it, and this rules-only test never saw one. The test now
        validates each finding without exactly those attributes and requires that nothing
        else outside the schema is present.
        """
        _install_patches(monkeypatch)
        write_fn, written = _capture_fn()
        refresh_fn, _ = _capture_fn()

        run_rebalance(**_make_run_kwargs(write_fn=write_fn, refresh_fn=refresh_fn))

        assert written, "expected at least one finding to be written"
        assert any("metro_inputs_hash" in f for f in written)
        for finding in written:
            _validate_finding({k: v for k, v in finding.items()
                               if k not in _write._AGENT_PROJECTED_ATTRS})


class TestU7Grounding:
    """Grounded vehicle and grounding-read-error vehicle never appear in vehicle_vins (U7)."""

    def test_grounded_and_error_vehicles_excluded_end_to_end(self, monkeypatch):
        """VINGRND001 (grounded) and VINGRERR01 (read error) must not be in any move VINs."""
        _install_patches(monkeypatch)
        write_fn, written = _capture_fn()
        refresh_fn, _ = _capture_fn()

        stats = run_rebalance(
            **_make_run_kwargs(write_fn=write_fn, refresh_fn=refresh_fn)
        )

        all_vins_written: set[str] = set()
        for f in written:
            rec = f.get("recommended_action")
            if rec and "parameters" in rec:
                all_vins_written.update(rec["parameters"].get("vehicle_vins", []))

        assert "VINGRND001" not in all_vins_written
        assert "VINGRERR01" not in all_vins_written
        # grounding errors counted
        assert stats["grounding_read_errors"] >= 1


class TestUnchangedSecondRun:
    """Second run with same data calls only refresh_fn, not write_fn."""

    def test_second_run_refresh_only(self, monkeypatch):
        live_hashes: dict[str, str] = {}

        # First run: capture hashes from whatever write_fn receives
        _install_patches(monkeypatch, live_hashes=live_hashes)
        write_fn1, written1 = _capture_fn()
        refresh_fn1, refreshed1 = _capture_fn()
        run_rebalance(**_make_run_kwargs(write_fn=write_fn1, refresh_fn=refresh_fn1))
        assert written1, "first run must write at least one finding"

        # Populate live_hashes from first run's findings
        for f in written1:
            live_hashes[f["finding_key"]] = f["inputs_hash"]

        # Second run with same data and live hashes present
        write_fn2, written2 = _capture_fn()
        refresh_fn2, refreshed2 = _capture_fn()
        stats2 = run_rebalance(**_make_run_kwargs(write_fn=write_fn2, refresh_fn=refresh_fn2))

        assert written2 == [], "second run must not call write_fn"
        assert len(refreshed2) == len(written1), (
            "second run must call refresh_fn exactly once per previously-written finding"
        )
        assert stats2["items_unchanged"] == len(written1)

    def test_next_day_run_with_same_shortfall_refreshes_not_rewrites(self, monkeypatch):
        """W6: the next day's run with the same shortfall must call only refresh_fn.

        This test fails if `inputs_hash` contains the absolute `first_deficit_day`
        (which advances by one every day) rather than the offset from `run_date`.
        """
        live_hashes: dict[str, str] = {}

        # Day 1: run_date = _RUN = "2026-10-12"; forecast starts on 2026-10-13
        _install_patches(monkeypatch, live_hashes=live_hashes)
        write_fn1, written1 = _capture_fn()
        refresh_fn1, _ = _capture_fn()
        run_rebalance(**_make_run_kwargs(write_fn=write_fn1, refresh_fn=refresh_fn1))
        assert written1, "day-1 run must write at least one finding"
        for f in written1:
            live_hashes[f["finding_key"]] = f["inputs_hash"]

        # Day 2: advance run_date by one day; same deficit shortfall and VINs.
        # The forecast window shifts by one day (same offset) and run_date advances by 1.
        _DAY2 = "2026-10-13"
        _EFF2 = [f"2026-10-{d:02d}" for d in range(14, 21)]
        forecast_day2 = (
            [{"location_id": "loc-def", "forecast_date": d, "forecast_hours": 45.0,
              "method": "ml", "mape_28d": 0.10, "run_date": _DAY2, "data_as_of": _DAY2}
             for d in _EFF2] +
            [{"location_id": "loc-sur", "forecast_date": d, "forecast_hours": 20.0,
              "method": "ml", "mape_28d": 0.10, "run_date": _DAY2, "data_as_of": _DAY2}
             for d in _EFF2]
        )

        _NOW2 = datetime(2026, 10, 13, 14, 0, 0, tzinfo=timezone.utc)
        _install_patches(monkeypatch, forecast=forecast_day2, live_hashes=live_hashes)
        write_fn2, written2 = _capture_fn()
        refresh_fn2, refreshed2 = _capture_fn()
        run_rebalance(**_make_run_kwargs(write_fn=write_fn2, refresh_fn=refresh_fn2, now=_NOW2))

        assert written2 == [], (
            "day-2 run with unchanged shortfall must call only refresh_fn — "
            "if this fails, inputs_hash contains an absolute date instead of an offset"
        )
        assert len(refreshed2) == len(written1), (
            "day-2 run must refresh every finding written on day 1"
        )


class TestDeficitGapItem:
    """A deficit location with no surplus in the metro writes a gap item."""

    def test_deficit_no_surplus_writes_gap(self, monkeypatch):
        # Only a deficit location; no surplus anywhere
        gaps_no_surplus = [
            {
                "location_id": "loc-def", "status": "deficit", "vehicles_short": 2,
                "vehicles_spare": 0, "supply_vehicles": 3, "forecast_hours": 45.0,
                "forecast_utilization_pct": 0.95, "data_as_of": _TODAY,
            }
        ]
        # Only the deficit location in DDB; loc-sur removed
        locs = [_LOC_DEF]
        _install_patches(monkeypatch, gaps=gaps_no_surplus, locations=locs)
        write_fn, written = _capture_fn()
        refresh_fn, _ = _capture_fn()

        stats = run_rebalance(**_make_run_kwargs(write_fn=write_fn, refresh_fn=refresh_fn))

        assert stats["moves_planned"] == 0
        assert stats["gap_items_written"] >= 1
        gap_findings = [f for f in written if f.get("finding_kind") == "fleet.rebalance.gap"]
        assert gap_findings, "expected at least one gap finding"
        gf = gap_findings[0]
        assert gf["location"]["location_id"] == "loc-def"


class TestSurplusGapItem:
    """A surplus location in a metro with no deficit writes a surplus gap item."""

    def test_surplus_only_metro_writes_surplus_gap(self, monkeypatch):
        # Only a surplus; no deficit
        gaps_surplus_only = [
            {
                "location_id": "loc-sur", "status": "surplus", "vehicles_short": 0,
                "vehicles_spare": 3, "supply_vehicles": 10, "forecast_hours": 20.0,
                "forecast_utilization_pct": 0.20, "data_as_of": _TODAY,
            }
        ]
        locs = [_LOC_SUR]
        _install_patches(monkeypatch, gaps=gaps_surplus_only, locations=locs)
        write_fn, written = _capture_fn()
        refresh_fn, _ = _capture_fn()

        stats = run_rebalance(**_make_run_kwargs(write_fn=write_fn, refresh_fn=refresh_fn))

        assert stats["moves_planned"] == 0
        surplus_gaps = [
            f for f in written
            if f.get("finding_kind") == "fleet.rebalance.gap"
               and f["location"].get("location_id") == "loc-sur"
        ]
        assert surplus_gaps, "expected a surplus gap finding for loc-sur"
        assert surplus_gaps[0]["severity"] == "routine"

    def test_surplus_gap_not_written_when_metro_has_incomplete_location(self, monkeypatch):
        """S3: a surplus gap must not be written for a metro that has an incomplete location.

        An incomplete location means the metro's shortfall is unknown — writing
        "no location in this metro is short" when one location's data couldn't be
        read is untrue.

        Fails if the S3 gate is removed (mutant F3-S3).
        """
        # The surplus location is loc-sur; its metro is test-metro.
        # loc-inc is incomplete in the same metro.
        gaps_surplus_only = [
            {
                "location_id": "loc-sur", "status": "surplus", "vehicles_short": 0,
                "vehicles_spare": 3, "supply_vehicles": 10, "forecast_hours": 20.0,
                "forecast_utilization_pct": 0.20, "data_as_of": _TODAY,
            }
        ]
        loc_inc = {
            "locationId": "loc-inc", "name": "Incomplete Hub", "metro": "test-metro",
            "locationType": "downtown", "latitude": 47.50, "longitude": -122.35,
            "parkingSpaces": 10, "operatingHours": "8-18", "op_hours": 10.0,
        }

        def _read_locs_with_incomplete(fleet_id, *, ddb_client, table):
            # Return loc-sur as a complete location; loc-inc is in the same metro but incomplete.
            # metros_with_incomplete = {"test-metro"} because loc-inc belongs to it.
            import inspect
            from agents.fi_rebalancing import sources as _src
            inspect.signature(_src.read_locations).bind(
                fleet_id, ddb_client=ddb_client, table=table)
            return ([_LOC_SUR], ["loc-inc"], {"test-metro"})

        _install_patches(monkeypatch, gaps=gaps_surplus_only, locations=[_LOC_SUR])
        # Override read_locations to inject the incomplete location in the same metro.
        from agents.fi_rebalancing import sources as _src
        monkeypatch.setattr(_src, "read_locations", _read_locs_with_incomplete)

        write_fn, written = _capture_fn()
        refresh_fn, _ = _capture_fn()
        run_rebalance(**_make_run_kwargs(write_fn=write_fn, refresh_fn=refresh_fn))

        surplus_gaps = [
            f for f in written
            if f.get("finding_kind") == "fleet.rebalance.gap"
               and f["location"].get("location_id") == "loc-sur"
        ]
        assert surplus_gaps == [], (
            "S3: surplus gap must not be written when a location in the same metro is incomplete "
            f"(the metro's shortfall is unknown); got {surplus_gaps!r}"
        )


class TestSourceReadErrorPropagates:
    """SourceReadError from read_gaps propagates and write_fn is never called."""

    def test_read_gaps_error_aborts_run(self, monkeypatch):
        def _bad_gaps(*, stage, fleet_id, athena_client, workgroup, results_s3_path):
            raise SourceReadError("simulated read failure")

        _install_patches(monkeypatch)
        # Override with a failing read_gaps after the full install
        monkeypatch.setattr(_src, "read_gaps", _bad_gaps)

        write_fn, written = _capture_fn()
        refresh_fn, _ = _capture_fn()

        with pytest.raises(SourceReadError):
            run_rebalance(**_make_run_kwargs(write_fn=write_fn, refresh_fn=refresh_fn))

        assert written == [], "write_fn must not be called when a source read fails"



# ---------------------------------------------------------------------------
# Architect check (2026-10-10, mutants A1, A2, A4-A6, W4, W9). The U7 test above can't
# see a dropped grounding flag: with a 1-vehicle shortfall the clean vehicle sorts first
# and is taken anyway. Here the grounded and unreadable vehicles are the most idle, so
# either one would be chosen first if the run let it through.
#
# C5 (fix group G4-1 F3): extended with open-items read error and live p0, to catch
# mutants M2 and M3.  Both additional cases make the most-idle vehicle bad via
# open-items, not grounding, so they isolate that gate.
# ---------------------------------------------------------------------------

import sys  # noqa: E402

_THIS = sys.modules[__name__]


def _short3(monkeypatch):
    gaps = [dict(_GAPS[0], vehicles_short=3), dict(_GAPS[1], vehicles_spare=3)]
    monkeypatch.setattr(_THIS, "_UTILIZATION", [
        {"vin": v, "snapshot_date": d, "available_hours": 10.0, "on_rent_hours": 0.0}
        for v, idle_days in (("VINGRND001", 5), ("VINGRERR01", 5), ("VINELIG001", 1))
        for d in [f"2026-10-{12 - i:02d}" for i in range(idle_days)]
    ])
    return gaps


# Five parametrize cases for test_u7_the_most_idle_vehicle_is_still_never_moved:
# Each tuple is (bad_vin, grounding_extra, items_map, expected_other, open_ros_rows).
#   bad_vin:              VIN that must NOT appear in any move's vehicle_vins.
#   grounding_extra:      overrides on top of _DEFAULT_GROUNDING.
#   items_map:            open_items_map entries (all others default to ([], False)).
#   expected_other:       a clean VIN expected to be IN the move (control).
#   open_ros_rows:        rows returned by read_open_ros (already-mapped dicts).
_U7_CASES = [
    # grounded: VINGRND001 is most-idle (5 days) but grounded; VINGRERR01 is clean fallback.
    pytest.param(
        "VINGRND001",
        {"VINGRERR01": (False, False)},
        {},
        "VINGRERR01",
        [],
        id="grounded",
    ),
    # grounding_error: VINGRERR01 is most-idle (tied 5 days) but grounding read failed;
    # VINGRND001 is explicitly made clean.
    pytest.param(
        "VINGRERR01",
        {"VINGRND001": (False, False)},
        {},
        "VINGRND001",
        [],
        id="grounding_error",
    ),
    # open_items_error: VINGRND001 is most-idle but read_open_items failed; both VINGRND001
    # and VINGRERR01 get clean grounding so only the open-items gate matters.
    # Catches mutant M2: if open_items_read_error is always False, VINGRND001 becomes eligible
    # (most idle, 5 days) and appears in the move vins — test fails, M2 caught.
    pytest.param(
        "VINGRND001",
        {"VINGRND001": (False, False), "VINGRERR01": (False, False)},
        {"VINGRND001": (None, True)},
        "VINGRERR01",
        [],
        id="open_items_error",
    ),
    # open_items_error_empty_list: the same read error, but reported with an empty list.
    # The real reader returns (None, True), so the list guard in eligibility also catches the
    # case above; this one pins the merge's error flag on its own (M2 is otherwise masked by
    # that second guard; architect, Fix Group G4-2 mutant check).
    pytest.param(
        "VINGRND001",
        {"VINGRND001": (False, False), "VINGRERR01": (False, False)},
        {"VINGRND001": ([], True)},
        "VINGRERR01",
        [],
        id="open_items_error_empty_list",
    ),
    # live_p0: VINGRND001 is most-idle but has a live p0 finding; clean grounding for both.
    # Catches mutant M3: if open_fi_items is always [], the p0 is dropped and VINGRND001
    # becomes eligible — test fails, M3 caught.
    pytest.param(
        "VINGRND001",
        {"VINGRND001": (False, False), "VINGRERR01": (False, False)},
        {"VINGRND001": ([{"severity": "p0"}], False)},
        "VINGRERR01",
        [],
        id="live_p0",
    ),
    # open_ro: VINGRND001 is most-idle (5 days) but has an open repair order;
    # clean grounding and no open items for both eligible VINs.
    # Catches C-p2-1: if read_open_ros is always [], build_vehicle_facts never sets
    # days_to_next_service=0 from an open RO, and VINGRND001 is incorrectly eligible.
    # The DMS-shaped row (ro_id, vehicle_vin, status) is already mapped by read_open_ros;
    # here we return it directly from the mock (no SQL round-trip in unit tests).
    pytest.param(
        "VINGRND001",
        {"VINGRND001": (False, False), "VINGRERR01": (False, False)},
        {},
        "VINGRERR01",
        [{"ro_id": "RO-0001", "vehicle_vin": "VINGRND001", "status": "open"}],
        id="open_ro",
    ),
]


@pytest.mark.parametrize("bad_vin,grounding_extra,items_map,expected_other,open_ros_rows", _U7_CASES)
def test_u7_the_most_idle_vehicle_is_still_never_moved_when_grounded_or_unreadable(
        monkeypatch, bad_vin, grounding_extra, items_map, expected_other, open_ros_rows):
    gaps = _short3(monkeypatch)
    # Merge caller overrides on top of the default grounding map.
    grounding = {**_DEFAULT_GROUNDING, **grounding_extra}
    _install_patches(monkeypatch, gaps=gaps, grounding_map=grounding, open_items_map=items_map,
                     open_ros_rows=open_ros_rows)
    write_fn, written = _capture_fn()
    refresh_fn, _ = _capture_fn()
    run_rebalance(**_make_run_kwargs(write_fn=write_fn, refresh_fn=refresh_fn))
    moves = [f for f in written if f["finding_kind"] == "fleet.rebalance.move"]
    assert len(moves) == 1
    vins = moves[0]["recommended_action"]["parameters"]["vehicle_vins"]
    assert bad_vin not in vins, (
        f"{bad_vin!r} should be excluded by its eligibility condition but appeared in {vins!r}"
    )
    assert vins == [expected_other, "VINELIG001"], (
        f"expected ordered vins [{expected_other!r}, 'VINELIG001'] but got {vins!r}. "
        "W-n8 / mutant K1: filter_eligible must rank each source's vehicles together (one "
        "call per source, not one per vehicle, which keeps read order) so they come back in "
        "idle-day-descending order; "
        "expected_other (5 idle days) must rank before VINELIG001 (1 idle day)."
    )


def test_a_covered_deficit_and_its_metro_get_no_gap_items(monkeypatch):
    _install_patches(monkeypatch)
    write_fn, written = _capture_fn()
    refresh_fn, _ = _capture_fn()
    run_rebalance(**_make_run_kwargs(write_fn=write_fn, refresh_fn=refresh_fn))
    kinds = sorted(f["finding_kind"] for f in written)
    assert kinds == ["fleet.rebalance.move"]


def test_move_item_identity_and_expiry(monkeypatch):
    _install_patches(monkeypatch)
    write_fn, written = _capture_fn()
    refresh_fn, _ = _capture_fn()
    run_rebalance(**_make_run_kwargs(write_fn=write_fn, refresh_fn=refresh_fn))
    move = next(f for f in written if f["finding_kind"] == "fleet.rebalance.move")
    week = move["recommended_action"]["parameters"]["forecast_week"]
    assert move["subject_key"] == f"loc-sur/loc-def/{week}"
    assert move["computed_at"] == "2026-10-12T14:00:00Z"
    assert move["expires_at"] == "2026-10-14T14:00:00Z"  # D10: 2 days, refreshed daily


def test_vehicles_are_read_only_in_metros_with_a_deficit(monkeypatch):
    other_metro = dict(_LOC_SUR, locationId="loc-far", metro="other-metro")
    gaps = _GAPS + [dict(_GAPS[1], location_id="loc-far")]
    monkeypatch.setattr(_THIS, "_VEHICLE_DAYS", _VEHICLE_DAYS + [
        {"vin": "VINFAR0001", "location_id": "loc-far", "available_hours": 10.0}])
    read: list[str] = []
    grounding = dict(_DEFAULT_GROUNDING)
    _install_patches(monkeypatch, gaps=gaps, locations=_LOCATIONS + [other_metro],
                     grounding_map=grounding)
    real = _src.read_grounding

    def _counting(vin, **kw):
        read.append(vin)
        return real(vin, **kw)

    monkeypatch.setattr(_src, "read_grounding", _counting)
    write_fn, _ = _capture_fn()
    refresh_fn, _ = _capture_fn()
    stats = run_rebalance(**_make_run_kwargs(write_fn=write_fn, refresh_fn=refresh_fn))
    assert "VINFAR0001" not in read
    assert sorted(read) == ["VINELIG001", "VINGRERR01", "VINGRND001"]
    assert stats["vehicles_checked"] == 3



def test_forecast_evidence_names_method_and_data_dates_and_events_are_scoped(monkeypatch):
    """Tier 2 artifact contract: every forecast number carries its method and the data's own
    date, so a stale forecast is narrated as stale; events from other metros stay off."""
    _install_patches(monkeypatch)
    events = [
        {"event_id": "ev-here", "metro": "test-metro", "affected_location_ids": ["loc-def"],
         "category": "sports", "uplift_band": "high", "starts_on": "2026-10-14",
         "ends_on": "2026-10-14"},
        {"event_id": "ev-elsewhere", "metro": "other-metro", "affected_location_ids": ["loc-x"],
         "category": "convention", "uplift_band": "low", "starts_on": "2026-10-14",
         "ends_on": "2026-10-15"},
    ]

    def _events(*, stage, today, athena_client, workgroup, results_s3_path):
        return events

    monkeypatch.setattr(_src, "read_events", _events)
    write_fn, written = _capture_fn()
    refresh_fn, _ = _capture_fn()
    run_rebalance(**_make_run_kwargs(write_fn=write_fn, refresh_fn=refresh_fn))
    move = next(f for f in written if f["finding_kind"] == "fleet.rebalance.move")
    fc = [e for e in move["evidence"] if e["kind"] == "rebalance.forecast"]
    assert fc and all(e["value"]["method"] == "ml" and e["value"]["run_date"] == _RUN
                      and e["value"]["data_as_of"] == _TODAY for e in fc)
    assert all(f"run_date={_RUN}" in e["source_ref"] for e in fc)
    ev = next(e for e in move["evidence"] if e["kind"] == "rebalance.events")
    assert [x["event_id"] for x in ev["value"]] == ["ev-here"]


def test_gap_evidence_carries_its_forecast_days(monkeypatch):
    gaps = [_GAPS[0]]  # a deficit with no surplus anywhere
    _install_patches(monkeypatch, gaps=gaps)
    write_fn, written = _capture_fn()
    refresh_fn, _ = _capture_fn()
    run_rebalance(**_make_run_kwargs(write_fn=write_fn, refresh_fn=refresh_fn))
    gap = next(f for f in written if f["finding_kind"] == "fleet.rebalance.gap")
    fc = [e for e in gap["evidence"] if e["kind"] == "rebalance.forecast"]
    assert len(fc) == 7 and all(e["value"]["data_as_of"] == _TODAY for e in fc)



# ---------------------------------------------------------------------------
# C4: NaN mape_28d end-to-end — evidence must not raise through _to_ddb_item
# ---------------------------------------------------------------------------

def test_nan_mape_run_does_not_raise_and_writes_low_confidence_move(monkeypatch):
    """A run with NaN / NULL mape_28d must complete without error.

    Tests both the move path (loc-def → loc-sur) and the gap path
    (loc-solo, deficit-only metro) so that W-n2 / mutant G2 is caught
    regardless of which evidence builder omits _finite().

    ADP writes NULL for mape_28d when there is no 28-day backtest (mapped
    as None) and NaN when the model produces an invalid value.  Before C4
    the gap path's _finite() call was missing; NaN reached _to_ddb_item
    → TypeSerializer raised.

    write_fn and refresh_fn call _to_ddb_item on the WHOLE finding (not
    only evidence) so that NaN anywhere in the finding structure is caught.

    Mutations:
    - G2: remove _finite() from gap_evidence → NaN survives into
      _to_ddb_item → TypeError raised → write_fn re-raises → test fails.
    - K1 (W-n8): not targeted here; covered by the parametrized U7 test.
    """
    from agents.tier2.writer import _to_ddb_item  # noqa: PLC0415

    # loc-solo is in solo-metro with no surplus → a gap finding is written.
    # Some of its forecast days have mape_28d=None (ADP's SQL NULL form);
    # some have mape_28d=NaN.  Both must be sanitised by _finite().
    _LOC_SOLO = {
        "locationId": "loc-solo", "name": "Solo Hub", "metro": "solo-metro",
        "locationType": "airport", "latitude": 47.50, "longitude": -122.40,
        "parkingSpaces": 10, "operatingHours": "8-18", "op_hours": 10.0,
    }
    nan_gaps = [
        *_GAPS,  # loc-def deficit + loc-sur surplus in test-metro → move path
        {
            "location_id": "loc-solo", "status": "deficit", "vehicles_short": 1,
            "vehicles_spare": 0, "supply_vehicles": 2, "forecast_hours": 40.0,
            "forecast_utilization_pct": 0.95, "data_as_of": _TODAY,
        },
    ]
    # loc-def and loc-sur: NaN mape_28d (move path)
    # loc-solo: alternating None (ADP NULL) and NaN (invalid model value)
    nan_forecast = [
        {"location_id": loc, "forecast_date": d,
         "forecast_hours": 55.0, "method": "ml",
         "mape_28d": float("nan"), "run_date": _RUN, "data_as_of": _TODAY}
        for loc in ("loc-def", "loc-sur")
        for d in _EFF
    ] + [
        {"location_id": "loc-solo", "forecast_date": d,
         "forecast_hours": 40.0, "method": "ml",
         "mape_28d": None if i % 2 == 0 else float("nan"),
         "run_date": _RUN, "data_as_of": _TODAY}
        for i, d in enumerate(_EFF)
    ]

    # gap_evidence raises ValueError when no VIN is assigned at the location;
    # add one so the gap finding is actually written.
    monkeypatch.setattr(_THIS, "_VEHICLE_DAYS", [
        *_VEHICLE_DAYS,
        {"vin": "VINSOLO001", "location_id": "loc-solo", "available_hours": 10.0},
    ])

    written: list[dict] = []
    refreshed: list[dict] = []

    def write_fn(finding: dict) -> None:
        # W-n2: serialise the WHOLE finding, not only evidence, so that NaN
        # anywhere in the structure (including per-day forecast entries in gap
        # findings) is caught.  Re-raise as AssertionError for clear diagnostics.
        try:
            _to_ddb_item(finding)
        except Exception as exc:  # noqa: BLE001
            raise AssertionError(
                f"_to_ddb_item raised {type(exc).__name__} on whole finding "
                f"(C4 / W-n2: NaN not sanitised before serialisation): {exc}"
            ) from exc
        written.append(finding)

    def refresh_fn(finding: dict) -> None:
        try:
            _to_ddb_item(finding)
        except Exception as exc:  # noqa: BLE001
            raise AssertionError(
                f"_to_ddb_item raised {type(exc).__name__} in refresh_fn: {exc}"
            ) from exc
        refreshed.append(finding)

    _install_patches(monkeypatch, gaps=nan_gaps, forecast=nan_forecast,
                     locations=[_LOC_DEF, _LOC_SUR, _LOC_SOLO])
    # Must not raise
    run_rebalance(**_make_run_kwargs(write_fn=write_fn, refresh_fn=refresh_fn))

    # --- move path ---
    moves = [f for f in written if f.get("finding_kind") == "fleet.rebalance.move"]
    assert moves, "A move finding must be written even when all mape_28d are NaN"
    assert moves[0]["confidence"] == "low", (
        f"NaN MAPE must produce confidence='low' on move path; "
        f"got {moves[0].get('confidence')!r}"
    )

    # --- gap path (W-n2) ---
    # loc-solo has no surplus in solo-metro; a gap finding is written.
    gaps_written = [
        f for f in written
        if f.get("finding_kind") == "fleet.rebalance.gap"
        and f.get("location", {}).get("location_id") == "loc-solo"
    ]
    assert gaps_written, (
        "A gap finding for loc-solo must be written. "
        "W-n2 / mutant G2: removing _finite() from gap_evidence lets NaN survive "
        "into _to_ddb_item → TypeError raised before written.append() is reached."
    )
    gf = gaps_written[0]
    assert gf["confidence"] == "low", (
        f"NaN/None mape_28d must produce confidence='low' on gap path; got {gf.get('confidence')!r}"
    )
    gap_fc = [e for e in gf["evidence"] if e["kind"] == "rebalance.forecast"]
    assert gap_fc, "gap finding must carry rebalance.forecast evidence entries"
    assert all(e["value"]["mape_28d"] is None for e in gap_fc), (
        "mape_28d must be None in gap evidence entries (_finite(NaN)=None, _finite(None)=None); "
        "if _finite() is removed the value stays NaN and write_fn raises above."
    )



def test_empty_forecast_aborts_run(monkeypatch):
    """An empty forecast read raises SourceReadError (W5).

    Mutation: remove the empty-forecast check → run proceeds without a run_date,
    location_outlook raises ValueError on every deficit → no move written
    but also no SourceReadError. The pytest.raises(SourceReadError) then fails.
    """
    _install_patches(monkeypatch, forecast=[])
    write_fn, written = _capture_fn()
    refresh_fn, _ = _capture_fn()
    with pytest.raises(SourceReadError, match="forecast"):
        run_rebalance(**_make_run_kwargs(write_fn=write_fn, refresh_fn=refresh_fn))
    assert written == [], "write_fn must not be called when the run aborts on empty forecast"



def test_configured_min_net_benefit_is_passed_to_rules_fallback(monkeypatch):
    """run_rebalance passes its cfg min_net_benefit_usd to rules_fallback.

    A custom forecast is chosen so the candidate's net benefit ≈ 35 USD —
    above cfg's min (20.0) so build_candidates keeps it, but below the policy's
    min ($100 since 2026-10-16; $50 before) so rules_fallback with the un-forwarded default
    drops it.

    Correct behavior (cfg=20 forwarded): rules_fallback uses 20 → net=35 > 20
      → move written.
    Mutation F2-W4b (min_net_benefit_usd= removed from the call): rules_fallback
      uses the policy's min (100) → net=35 < 100 → move dropped → assert fails → CAUGHT.

    Forecast is constructed so loc-def net ≈ 35:
      unserved = 31 - 3*10*0.95 = 2.5 h/day
      gain_per_day = min(2.5, 9.5)*3.50 = 8.75
      total_gain = 8.75*7 = 61.25
      source loss ≈ 0 (surplus location is far from its capacity)
      transfer_cost = (2*~5/22 + 0.25)*30 + ~5*0.15 ≈ 21 (small: loc-def and loc-sur are close)
      net ≈ 61.25 - 21 ≈ 40  (in (20, 100))
    """
    low_demand_forecast = [
        {"location_id": loc, "forecast_date": d,
         "forecast_hours": 31.0, "method": "ml",
         "mape_28d": 0.10, "run_date": _RUN, "data_as_of": _TODAY}
        for loc in ("loc-def", "loc-sur")
        for d in _EFF
    ]
    _install_patches(monkeypatch, forecast=low_demand_forecast)
    write_fn, written = _capture_fn()
    refresh_fn, _ = _capture_fn()
    # With min_net=20 forwarded, the candidate (net≈40 > 20) should be written.
    run_rebalance(
        **_make_run_kwargs(
            write_fn=write_fn,
            refresh_fn=refresh_fn,
            config={"min_net_benefit_usd": 20.0},
        )
    )
    moves = [f for f in written if f.get("finding_kind") == "fleet.rebalance.move"]
    assert moves, (
        "With min_net_benefit_usd=20 and candidate net≈40, a move must be written. "
        "Mutation F2-W4b: fallback uses the policy's min (100) → net≈40 < 100 → dropped → fails."
    )



def test_zero_supply_deficit_increments_counter_and_logs(monkeypatch, caplog):
    """W-b2 (G4-2 F8): a deficit location with no assigned vehicle raises the counter by 1 and logs.

    Zero-supply deficit: no vehicle_day row at the deficit location, so assigned_vins is [],
    gap_evidence raises ValueError, the counter is incremented, and a WARNING is emitted
    that names the location id.  The counter must appear as gap_items_without_vehicle in
    the stats returned from run_rebalance.
    """
    import logging

    # One deficit location with zero supply (no VINs assigned there).
    # _VEHICLE_DAYS has only loc-sur and loc-def entries; we patch a new location "loc-zero"
    # that appears in gaps but has no assigned vehicles.
    loc_zero = {
        "locationId": "loc-zero", "name": "Zero Supply Hub", "metro": "test-metro",
        "locationType": "airport", "latitude": 47.50, "longitude": -122.40,
        "parkingSpaces": 5, "operatingHours": "8-18", "op_hours": 10.0,
    }
    gaps_with_zero = [
        *_GAPS,
        {
            "location_id": "loc-zero", "status": "deficit", "vehicles_short": 2,
            "vehicles_spare": 0, "supply_vehicles": 0, "forecast_hours": 50.0,
            "forecast_utilization_pct": 0.99, "data_as_of": _TODAY,
        },
    ]
    forecast_with_zero = list(_FORECAST) + [
        {"location_id": "loc-zero", "forecast_date": d, "forecast_hours": 50.0,
         "method": "ml", "mape_28d": 0.10, "run_date": _RUN, "data_as_of": _TODAY}
        for d in _EFF
    ]

    # Patch VINDEF001 to be at loc-def so loc-def still gets a gap item.
    # loc-zero has NO vehicle_day row → assigned_vins=[] → gap_evidence raises ValueError.
    _install_patches(
        monkeypatch,
        gaps=gaps_with_zero,
        forecast=forecast_with_zero,
        locations=[_LOC_DEF, _LOC_SUR, loc_zero],
    )

    write_fn, written = _capture_fn()
    refresh_fn, _ = _capture_fn()

    with caplog.at_level(logging.WARNING, logger="agents.fi_rebalancing.agent"):
        result = run_rebalance(**_make_run_kwargs(write_fn=write_fn, refresh_fn=refresh_fn))

    # Counter must be exactly 1 (loc-zero contributed one gap_items_without_vehicle).
    assert result["gap_items_without_vehicle"] == 1, (
        f"W-b2: gap_items_without_vehicle must be 1 for the zero-supply deficit location; "
        f"got {result.get('gap_items_without_vehicle')!r}"
    )

    # A WARNING naming the location id must have been emitted (W-b2 Decision 2).
    warning_texts = [r.message for r in caplog.records if r.levelno == logging.WARNING]
    assert any("loc-zero" in t for t in warning_texts), (
        f"W-b2: expected a warning mentioning 'loc-zero'; got warnings: {warning_texts!r}"
    )



def test_stale_forecast_drops_moves_and_says_so(monkeypatch):
    """W-c3-1 (G4-3): a forecast three days old drops every move candidate, and the stats say why.

    The money window is today+1..today+7; the forecast read keeps run_date+1..run_date+9, so with
    run_date = today-3 the window's last day has no row and build_candidates drops the candidate.
    The deficit's 7-day horizon (run_date+1..+7) is complete, so it becomes a gap item. Before
    the fix the only forecast counter the run reported was the gap loops' (0 here).
    """
    stale_run = "2026-10-09"
    days = [f"2026-10-{d:02d}" for d in range(10, 19)]  # run_date+1 .. run_date+9
    stale_forecast = (
        [{"location_id": "loc-def", "forecast_date": d, "forecast_hours": 45.0,
          "method": "ml", "mape_28d": 0.10, "run_date": stale_run, "data_as_of": stale_run}
         for d in days] +
        [{"location_id": "loc-sur", "forecast_date": d, "forecast_hours": 20.0,
          "method": "ml", "mape_28d": 0.10, "run_date": stale_run, "data_as_of": stale_run}
         for d in days]
    )
    _install_patches(monkeypatch, forecast=stale_forecast)
    write_fn, written = _capture_fn()
    refresh_fn, _ = _capture_fn()

    result = run_rebalance(**_make_run_kwargs(write_fn=write_fn, refresh_fn=refresh_fn))

    assert result.get("candidates_forecast_days_missing") == 1, (
        "W-c3-1: the one candidate (loc-sur -> loc-def) is dropped for a missing money-window "
        f"day and must be counted; got {result.get('candidates_forecast_days_missing')!r}"
    )
    assert result["move_items_written"] == 0
    kinds = [(f["finding_kind"], f["location"]) for f in written]
    assert kinds == [("fleet.rebalance.gap", {"location_id": "loc-def"})], kinds


def test_surplus_location_without_vehicle_is_counted_and_logged(monkeypatch, caplog):
    """S-c3-4 (G4-3): the surplus gap loop's "no assigned vehicle" path counts and logs too."""
    import logging

    loc_far = {
        "locationId": "loc-far", "name": "Far Lot", "metro": "other-metro",
        "locationType": "downtown", "latitude": 45.50, "longitude": -122.60,
        "parkingSpaces": 10, "operatingHours": "8-18", "op_hours": 10.0,
    }
    gaps = [*_GAPS, {
        "location_id": "loc-far", "status": "surplus", "vehicles_short": 0,
        "vehicles_spare": 3, "supply_vehicles": 6, "forecast_hours": 10.0,
        "forecast_utilization_pct": 0.15, "data_as_of": _TODAY,
    }]
    forecast = list(_FORECAST) + [
        {"location_id": "loc-far", "forecast_date": d, "forecast_hours": 10.0,
         "method": "ml", "mape_28d": 0.10, "run_date": _RUN, "data_as_of": _TODAY}
        for d in _EFF
    ]
    _install_patches(monkeypatch, gaps=gaps, forecast=forecast,
                     locations=[_LOC_DEF, _LOC_SUR, loc_far])
    write_fn, written = _capture_fn()
    refresh_fn, _ = _capture_fn()

    with caplog.at_level(logging.WARNING, logger="agents.fi_rebalancing.agent"):
        result = run_rebalance(**_make_run_kwargs(write_fn=write_fn, refresh_fn=refresh_fn))

    assert result["gap_items_without_vehicle"] == 1, result.get("gap_items_without_vehicle")
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("surplus location loc-far" in w for w in warnings), warnings
    assert all(f["location"] != {"location_id": "loc-far"} for f in written)
