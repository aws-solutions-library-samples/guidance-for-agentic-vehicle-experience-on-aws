"""Tests for item 4 helper functions in live-verify-fi-fleet-health.py.

Fakes built from the real code/data shapes (source citations below).
A positive control per property; at least one bad world per property.

Source citations:
  _plan_vins_for:         reference.py _plan_targets; pm_vehicle_status.sql plan_candidates CTE
  _pm_last_done:          reference.py pm_vehicle_status sources 1-3; pm_vehicle_status.sql
                          sr_completions/dms_completions/op_completions CTEs
  _pm_status_and_due:     pm_vehicle_status.sql final SELECT CASE
  _recompute_pm_oracle:   reference.py pm_vehicle_status (no pandas)
  _compare_pm_results:    item 4 comparison spec (facts.md § "Item 4")
  pm_plans.applies_to:    CMS glue_merge_job.py get_json_object(new_image,'$.appliesTo.M')
                          i.e. the DynamoDB map CONTENTS, not the full attribute envelope
  repair_orders.lines:    dms_${stage}.repair_orders lines column is JSON VARCHAR
                          (pm_vehicle_status.sql latest_ros CTE; interface.md § 8)
  Athena all-string rows: _fetch helper converts rows to list[dict[str, str]]

Run (AWS env stripped):
    env -u AWS_PROFILE AWS_ACCESS_KEY_ID=testing AWS_SECRET_ACCESS_KEY=testing \\
      PYTHONDONTWRITEBYTECODE=1 \\
      .venv/bin/python -m pytest scripts/tests/test_live_verify_fi_fleet_health_item4.py -v
"""
from __future__ import annotations

import datetime as dt
import importlib.util
import json
from pathlib import Path

# ---------------------------------------------------------------------------
# Load module under test
# ---------------------------------------------------------------------------

_SCRIPT = Path(__file__).resolve().parents[1] / "live-verify-fi-fleet-health.py"
_spec = importlib.util.spec_from_file_location("live_verify_fi_fleet_health", _SCRIPT)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

_plan_vins_for = _mod._plan_vins_for
_pm_last_done = _mod._pm_last_done
_pm_status_and_due = _mod._pm_status_and_due
_recompute_pm_oracle = _mod._recompute_pm_oracle
_compare_pm_results = _mod._compare_pm_results
Results = _mod.Results


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------

# Valid VINs (17 chars, chars from [A-HJ-NPR-Z0-9]).
# Source: _VIN_RE in the script.
_V1 = "1FTEX1EM0MFA00001"
_V2 = "1FTEX1EM0MFA00002"
_V3 = "1FTEX1EM0MFA00003"
_FLT1 = "demo-rental-carshare"
_FLT2 = "demo-lastmile-delivery"

_DATA_AS_OF = dt.date(2026, 10, 13)
_DHD = 30   # due_horizon_days
_DHM = 500  # due_horizon_miles


def _make_plan(
    plan_id: str = "plan-001",
    fleet_id: str = _FLT1,
    task_code: str = "TIRE_ROTATION",
    basis: str = "calendar",
    interval_value: str = "90",
    start_date: str = "2026-01-01",
    vehicle_id: str | None = None,
    applies_to: dict | None = None,
) -> dict:
    """Build a pm_plans row in Athena shape (all strings; applies_to as DynamoDB map JSON).

    applies_to format: DynamoDB map CONTENTS (glue_merge_job.py extracts appliesTo.M),
    e.g. {"powertrains":{"L":[{"S":"ev"}]},"models":{"NULL":true}}.
    Source: pm_vehicle_status.sql plans CTE; reference.py _plan_targets docstring.
    """
    return {
        "plan_id": plan_id,
        "fleet_id": fleet_id,
        "vehicle_id": vehicle_id or "",
        "applies_to": json.dumps(applies_to) if applies_to else "",
        "task_code": task_code,
        "basis": basis,
        "interval_value": interval_value,
        "start_date": start_date,
    }


def _make_vehicle(
    vehicle_id: str = "VEH-001",
    vin: str = _V1,
    model: str = "Model-S",
    model_year: str = "2024",
    fuel_type: str = "ev",
) -> dict:
    """cms_ingest.vehicles row (Athena shape).

    Source: pm_vehicle_status.sql cms_vehicle CTE; reference.py _plan_targets veh_by_vin.
    """
    return {
        "vehicle_id": vehicle_id, "vin": vin, "model": model,
        "model_year": model_year, "fuel_type": fuel_type,
    }


def _make_sr(vin: str, service_type: str, service_date: str) -> dict:
    """service_records row (Athena shape; service_date as ISO date string)."""
    return {"vin": vin, "service_type": service_type, "service_date": service_date}


def _make_ro(
    ro_id: str = "RO-001",
    vin: str = _V1,
    status: str = "Ready",
    labor_op: str = "TIRE_ROTATION",
    updated_at: str = "2026-08-01T10:00:00+00:00",
    seq_no: str = "1",
) -> dict:
    """repair_orders row (Athena shape; lines as JSON string per interface.md § 8).

    Source: pm_vehicle_status.sql latest_ros CTE; dms_${stage}.repair_orders.
    """
    lines = json.dumps([{"labor_op": labor_op}])
    return {
        "ro_id": ro_id, "vehicle_vin": vin, "status": status,
        "lines": lines, "updated_at": updated_at, "_seq_no": seq_no,
    }


def _make_op_comp(vin: str, task_code: str, completed_on: str) -> dict:
    """pm_operator_completions row (Athena shape)."""
    return {"vin": vin, "task_code": task_code, "completed_on": completed_on}


def _make_usage(vin: str, usage_date: str, miles_driven: str) -> dict:
    """fleet_ops.daily_usage row (Athena shape)."""
    return {"vin": vin, "usage_date": usage_date, "miles_driven": miles_driven}


# Default inputs for oracle tests
def _base_inputs(
    vin_fleet: dict[str, str] | None = None,
    pm_plans: list[dict] | None = None,
    cms_vehicles: list[dict] | None = None,
    service_records: list[dict] | None = None,
    repair_orders: list[dict] | None = None,
    pm_op_completions: list[dict] | None = None,
    clm_map: dict[str, list[str]] | None = None,
    daily_usage: list[dict] | None = None,
) -> dict:
    """Minimal correct world: V1 in FLT1, calendar plan, tire_rotation SR."""
    return {
        "vin_fleet": vin_fleet or {_V1: _FLT1},
        "pm_plans": pm_plans or [
            _make_plan(fleet_id=_FLT1, task_code="TIRE_ROTATION", basis="calendar",
                       interval_value="90", start_date="2026-01-01",
                       applies_to={"powertrains": {"L": [{"S": "ev"}]},
                                   "models": {"NULL": True}}),
        ],
        "cms_vehicles": cms_vehicles or [_make_vehicle(vin=_V1)],
        "service_records": service_records or [
            _make_sr(_V1, "tire_service", "2026-09-01"),   # 42d ago, interval=90 → current
        ],
        "repair_orders": repair_orders or [],
        "pm_op_completions": pm_op_completions or [],
        "clm_map": clm_map or {"TIRE_ROTATION": ["TIRE_ROTATION"]},
        "daily_usage": daily_usage or [],
    }


def _run_oracle(inp: dict, data_as_of: dt.date = _DATA_AS_OF) -> list[dict]:
    return _recompute_pm_oracle(
        inp["pm_plans"], inp["cms_vehicles"], inp["service_records"],
        inp["repair_orders"], inp["vin_fleet"], inp["daily_usage"],
        inp["pm_op_completions"], inp["clm_map"],
        _DHD, _DHM, data_as_of,
    )


# ---------------------------------------------------------------------------
# 1. _plan_vins_for tests
# ---------------------------------------------------------------------------


class TestPlanVinsFor:
    """Source: reference.py _plan_targets; pm_vehicle_status.sql plan_candidates CTE."""

    def _vf(self, vin: str = _V1, fleet: str = _FLT1) -> dict[str, str]:
        return {vin: fleet}

    def _veh_maps(self, vid: str = "VEH-001", vin: str = _V1) -> tuple[dict, dict]:
        veh = {"vehicle_id": vid, "vin": vin, "model": "X", "model_year": "2024",
               "fuel_type": "ev"}
        return {vid: veh}, {vin: veh}

    def test_single_vehicle_plan_match(self):
        """Single-vehicle plan returns its VIN when VIN is managed in plan's fleet."""
        by_id, by_vin = self._veh_maps()
        plan = _make_plan(vehicle_id="VEH-001", fleet_id=_FLT1)
        assert _plan_vins_for(plan, self._vf(), by_id, by_vin) == [_V1]

    def test_single_vehicle_plan_fleet_mismatch(self):
        """Single-vehicle plan: VIN belongs to FLT2, plan is in FLT1 → no match."""
        by_id, by_vin = self._veh_maps()
        plan = _make_plan(vehicle_id="VEH-001", fleet_id=_FLT1)
        # VIN is in FLT2, not FLT1
        assert _plan_vins_for(plan, {_V1: _FLT2}, by_id, by_vin) == []

    def test_single_vehicle_plan_vehicle_id_not_in_map(self):
        """vehicle_id not in veh_by_id → no VINs returned."""
        plan = _make_plan(vehicle_id="VEH-UNKNOWN", fleet_id=_FLT1)
        assert _plan_vins_for(plan, self._vf(), {}, {}) == []

    def test_scoped_plan_powertrain_match(self):
        """Scoped plan with powertrain=ev matches an ev vehicle."""
        by_id, by_vin = self._veh_maps()
        plan = _make_plan(
            fleet_id=_FLT1,
            applies_to={"powertrains": {"L": [{"S": "ev"}]},
                        "models": {"NULL": True}},
        )
        assert _plan_vins_for(plan, self._vf(), by_id, by_vin) == [_V1]

    def test_scoped_plan_powertrain_no_match(self):
        """Scoped plan with powertrain=ev does NOT match an ice vehicle."""
        ice_veh = {"vehicle_id": "VEH-001", "vin": _V1, "model": "X",
                   "model_year": "2024", "fuel_type": "gasoline"}
        by_id, by_vin = {"VEH-001": ice_veh}, {_V1: ice_veh}
        plan = _make_plan(fleet_id=_FLT1,
                          applies_to={"powertrains": {"L": [{"S": "ev"}]}})
        assert _plan_vins_for(plan, self._vf(), by_id, by_vin) == []

    def test_scoped_plan_model_year_range(self):
        """Year range 2024-2025 matches a 2024 vehicle but not a 2023 one."""
        veh_2024 = {**_make_vehicle(vin=_V1), "model_year": "2024"}
        veh_2023 = {**_make_vehicle(vin=_V2), "model_year": "2023"}
        by_id = {"VEH-001": veh_2024, "VEH-002": veh_2023}
        by_vin = {_V1: veh_2024, _V2: veh_2023}
        vf = {_V1: _FLT1, _V2: _FLT1}
        plan = _make_plan(fleet_id=_FLT1,
                          applies_to={"modelYearMin": {"N": "2024"},
                                      "modelYearMax": {"N": "2025"}})
        result = _plan_vins_for(plan, vf, by_id, by_vin)
        assert _V1 in result
        assert _V2 not in result

    def test_scoped_plan_no_scope_fields_returns_empty(self):
        """A scoped plan with no restricting fields applies to nothing (CMS refuses it)."""
        plan = _make_plan(applies_to={})
        assert _plan_vins_for(plan, self._vf(), {}, {}) == []

    def test_plan_outside_own_fleet_never_matches_other_fleet_vin(self):
        """H1-D1: plan only applies to managed VINs in its own fleet.

        This is the 'completion from another fleet's plan' guard:
        a FLT2 plan must not return a FLT1 VIN, even when vehicle matches scope.
        Mutation: remove fleet_id equality check → FLT2 plan returns FLT1 VINs.
        """
        veh = _make_vehicle(vin=_V1)
        plan = _make_plan(
            fleet_id=_FLT2,  # plan belongs to FLT2
            applies_to={"powertrains": {"L": [{"S": "ev"}]}},
        )
        vf = {_V1: _FLT1}   # VIN is in FLT1
        assert _plan_vins_for(plan, vf, {}, {_V1: veh}) == []


# ---------------------------------------------------------------------------
# 2. _pm_last_done tests
# ---------------------------------------------------------------------------


class TestPmLastDone:
    """Source: pm_vehicle_status.sql sr_completions/dms_completions/op_completions CTEs."""

    def test_service_records_source(self):
        """SR tire_service on 2026-07-01 → last_done=(date,'service_records')."""
        done, src = _pm_last_done(
            _V1, "TIRE_ROTATION",
            service_records=[_make_sr(_V1, "tire_service", "2026-07-01")],
            latest_ros={},
            op_done={},
            clm_map={"TIRE_ROTATION": ["TIRE_ROTATION"]},
        )
        assert done == dt.date(2026, 7, 1)
        assert src == "service_records"

    def test_dms_ro_source(self):
        """DMS RO via code_labor_op_map → last_done=(date,'dms_repair_order')."""
        ro = _make_ro(_V1, _V1, "Ready", "TIRE_ROTATION", "2026-08-01T00:00:00+00:00")
        done, src = _pm_last_done(
            _V1, "TIRE_ROTATION",
            service_records=[],
            latest_ros={"RO-001": ro},
            op_done={},
            clm_map={"TIRE_ROTATION": ["TIRE_ROTATION"]},
        )
        assert done == dt.date(2026, 8, 1)
        assert src == "dms_repair_order"

    def test_operator_source(self):
        """Operator completion → last_done=(date,'operator')."""
        done, src = _pm_last_done(
            _V1, "TIRE_ROTATION",
            service_records=[],
            latest_ros={},
            op_done={(_V1, "TIRE_ROTATION"): dt.date(2026, 9, 1)},
            clm_map={},
        )
        assert done == dt.date(2026, 9, 1)
        assert src == "operator"

    def test_tie_break_service_records_over_dms(self):
        """Same date: service_records wins over dms_repair_order.

        Source: pm_vehicle_status.sql best_completion CTE ORDER BY source CASE.
        Mutation: remove tie-break check → dms_repair_order may win on equal dates.
        """
        ro = _make_ro("RO-1", _V1, "Ready", "TIRE_ROTATION", "2026-08-01T12:00:00+00:00")
        done, src = _pm_last_done(
            _V1, "TIRE_ROTATION",
            service_records=[_make_sr(_V1, "tire_service", "2026-08-01")],
            latest_ros={"RO-1": ro},
            op_done={},
            clm_map={"TIRE_ROTATION": ["TIRE_ROTATION"]},
        )
        assert done == dt.date(2026, 8, 1)
        assert src == "service_records"  # tie-break: SR > DMS

    def test_cabin_filter_exempt_from_service_records(self):
        """CABIN_FILTER and OIL_CHANGE are never credited by service_records alone.

        Source: pm_vehicle_status.sql sr_completions WHERE task_code NOT IN (...).
        Mutation: remove the exempt check → SR date credited for CABIN_FILTER.
        """
        done, src = _pm_last_done(
            _V1, "CABIN_FILTER",
            service_records=[_make_sr(_V1, "cabin_filter_replace", "2026-07-01")],
            latest_ros={},
            op_done={},
            clm_map={},
        )
        # No SR credit; no DMS; no operator → no completion
        assert done is None
        assert src is None

    def test_no_completion_returns_none(self):
        """No matching source → (None, None)."""
        done, src = _pm_last_done(_V1, "TIRE_ROTATION", [], {}, {}, {})
        assert done is None and src is None


# ---------------------------------------------------------------------------
# 3. _pm_status_and_due tests (calendar and mileage)
# ---------------------------------------------------------------------------


class TestPmStatusAndDue:
    """Source: pm_vehicle_status.sql final SELECT CASE (calendar and mileage)."""

    def _cal(self, last_done: dt.date | None, interval: int = 90,
             start: dt.date = dt.date(2026, 1, 1)) -> tuple:
        return _pm_status_and_due(
            "calendar", last_done, interval, start, _V1, [], {}, _DHD, _DHM, _DATA_AS_OF
        )

    def test_calendar_current(self):
        """Done 30 days ago with 90d interval → next_due in 60 days → 'current'."""
        done = _DATA_AS_OF - dt.timedelta(days=30)
        status, ndd, ndm = self._cal(done, 90)
        assert status == "current"
        assert ndm is None
        assert ndd is not None

    def test_calendar_overdue(self):
        """Done 120 days ago with 90d interval → overdue."""
        done = _DATA_AS_OF - dt.timedelta(days=120)
        status, ndd, _ = self._cal(done, 90)
        assert status == "overdue"

    def test_calendar_due(self):
        """Done 70 days ago with 90d interval → next_due in 20 days → 'due' (<= 30d horizon)."""
        done = _DATA_AS_OF - dt.timedelta(days=70)
        status, ndd, _ = self._cal(done, 90)
        assert status == "due"
        assert ndd == done + dt.timedelta(days=90)

    def test_calendar_no_history(self):
        """No last_done → 'no_history'; next_due = start_date."""
        status, ndd, _ = self._cal(None, 90, start=dt.date(2026, 6, 1))
        assert status == "no_history"
        assert ndd == dt.date(2026, 6, 1)

    def test_mileage_overdue(self):
        """current_odo > next_due_miles → 'overdue'."""
        # 1000 miles driven, last_done_odo=0, interval=500 → next_due=500 → cur=1000 → overdue
        usage = [_make_usage(_V1, f"2026-0{m+1}-01", "200") for m in range(5)]
        status, ndd, ndm = _pm_status_and_due(
            "mileage", dt.date(2026, 1, 1), 500, dt.date(2026, 1, 1),
            _V1, usage, {_V1: 1000}, _DHD, _DHM, _DATA_AS_OF,
        )
        assert status == "overdue"
        assert ndd is None
        assert ndm is not None

    def test_mileage_no_history(self):
        """No last_done → 'no_history' regardless of driven mileage (spec D6)."""
        status, _, _ = _pm_status_and_due(
            "mileage", None, 500, dt.date(2026, 1, 1),
            _V1, [], {_V1: 10000}, _DHD, _DHM, _DATA_AS_OF,
        )
        assert status == "no_history"


# ---------------------------------------------------------------------------
# 4. _recompute_pm_oracle tests (positive control + bad worlds)
# ---------------------------------------------------------------------------


class TestRecomputePmOracle:
    """Positive control + key bad worlds; validates oracle end-to-end."""

    def test_positive_control_calendar(self):
        """Correct world: scoped ev plan, SR completion 75d ago → 'current'.

        Positive control (brief): a correct world PASSes.
        """
        inp = _base_inputs()
        rows = _run_oracle(inp)
        assert len(rows) == 1
        r = rows[0]
        assert r["vin"] == _V1
        assert r["task_code"] == "TIRE_ROTATION"
        assert r["last_done_source"] == "service_records"
        assert r["last_done_date"] == "2026-09-01"
        assert r["status"] == "current"  # 42d since 2026-09-01, interval=90, DHD=30

    def test_positive_control_dms_completion(self):
        """DMS RO via code_labor_op_map completes BRAKE_INSPECTION."""
        inp = _base_inputs(
            pm_plans=[_make_plan(task_code="BRAKE_INSPECTION", basis="calendar",
                                 interval_value="180", start_date="2026-01-01",
                                 applies_to={"powertrains": {"L": [{"S": "ev"}]}})],
            service_records=[],
            repair_orders=[_make_ro("RO-1", _V1, "Ready", "BRAKE_INSPECTION",
                                    "2026-06-01T00:00:00+00:00")],
            clm_map={"BRAKE_INSPECTION": ["BRAKE_INSPECTION"]},
        )
        rows = _run_oracle(inp)
        assert rows[0]["last_done_source"] == "dms_repair_order"
        assert rows[0]["last_done_date"] == "2026-06-01"

    def test_single_vehicle_plan_wins_over_scoped(self):
        """Single-vehicle plan (precedence=1) wins over a scoped plan for same task.

        H1 precedence rule: pm_vehicle_status.sql plan_vins ROW_NUMBER ORDER BY
        precedence, plan_id.  Mutation: swap to precedence=2 for single-vehicle →
        scoped plan chosen, interval differs.
        """
        veh = _make_vehicle(vehicle_id="VEH-001", vin=_V1)
        sv_plan = _make_plan(
            plan_id="plan-sv", fleet_id=_FLT1, task_code="TIRE_ROTATION",
            basis="calendar", interval_value="365",   # single-vehicle: 365d
            vehicle_id="VEH-001",                      # single-vehicle plan
        )
        scoped_plan = _make_plan(
            plan_id="plan-sc", fleet_id=_FLT1, task_code="TIRE_ROTATION",
            basis="calendar", interval_value="90",    # scoped: 90d
            applies_to={"powertrains": {"L": [{"S": "ev"}]}},
        )
        inp = _base_inputs(
            pm_plans=[sv_plan, scoped_plan],
            cms_vehicles=[veh],
            service_records=[_make_sr(_V1, "tire_service", "2026-01-01")],
        )
        rows = _run_oracle(inp)
        assert len(rows) == 1
        assert rows[0]["interval_value"] == "365"  # single-vehicle plan wins

    def test_precedence_wrong_fails_compare(self):
        """Bad world: scoped plan chosen instead of single-vehicle → compare detects it.

        The oracle returns interval=365 (single-vehicle wins), but the 'view' row
        claims interval=90 (scoped wins). _compare_pm_results must FAIL.
        Mutation: drop precedence ordering → scoped may win → field mismatch.
        """
        # Oracle result: single-vehicle plan, interval=365
        oracle_rows = [{
            "vin": _V1, "fleet_id": _FLT1, "task_code": "TIRE_ROTATION",
            "basis": "calendar", "interval_value": "365",
            "last_done_date": "2026-01-01", "last_done_source": "service_records",
            "next_due_date": "2027-01-01", "next_due_miles": "",
            "status": "current", "data_as_of": str(_DATA_AS_OF),
        }]
        # View row: different interval → precedence was wrong
        wrong_view = [{**oracle_rows[0], "interval_value": "90",
                       "next_due_date": "2026-04-01", "status": "overdue"}]
        ok, _, issues = _compare_pm_results(oracle_rows, wrong_view)
        assert not ok
        assert any("status" in i for i in issues)

    def test_another_fleet_plan_does_not_count(self):
        """Completion from another fleet's plan must not count for our VIN.

        H1-D1: plans only apply within their own fleet.
        VIN _V1 is in _FLT1; a _FLT2 plan must not create a row for _V1.
        Mutation: remove fleet_id check in _plan_vins_for → _FLT2 plan creates row.
        """
        flt2_plan = _make_plan(
            plan_id="plan-flt2", fleet_id=_FLT2, task_code="TIRE_ROTATION",
            applies_to={"powertrains": {"L": [{"S": "ev"}]}},
        )
        inp = _base_inputs(
            pm_plans=[flt2_plan],
            service_records=[_make_sr(_V1, "tire_service", "2026-07-01")],
        )
        rows = _run_oracle(inp)
        # _FLT2 plan must not produce a row for _V1 (which is in _FLT1)
        assert rows == []


# ---------------------------------------------------------------------------
# 5. _compare_pm_results bad worlds (brief § "Item 4" tests)
# ---------------------------------------------------------------------------


class TestComparePmResults:
    """Bad worlds from brief: view wrong by one day; extra row; missing row;
    precedence wrong (covered above); another-fleet completion (covered above).
    """

    def _oracle(self, status: str = "current", ndd: str = "2026-10-01",
                ldd: str = "2026-07-01") -> list[dict]:
        return [{
            "vin": _V1, "fleet_id": _FLT1, "task_code": "TIRE_ROTATION",
            "basis": "calendar", "interval_value": "90",
            "last_done_date": ldd, "last_done_source": "service_records",
            "next_due_date": ndd, "next_due_miles": "",
            "status": status, "data_as_of": str(_DATA_AS_OF),
        }]

    def test_positive_control_compare(self):
        """Identical oracle and view → ok=True.

        Positive control for _compare_pm_results.
        """
        rows = self._oracle()
        ok, _, issues = _compare_pm_results(rows, rows)
        assert ok
        assert issues == []

    def test_view_wrong_by_one_day(self):
        """Bad world: view next_due_date off by one day → FAIL.

        Mutation: remove next_due_date comparison → off-by-one date passes undetected.
        """
        oracle = self._oracle(ndd="2026-10-01")
        wrong_view = [{**oracle[0], "next_due_date": "2026-10-02"}]
        ok, _, issues = _compare_pm_results(oracle, wrong_view)
        assert not ok
        assert any("next_due_date" in i for i in issues)

    def test_extra_view_row(self):
        """Bad world: view has an extra (vin, task_code) row the oracle didn't compute."""
        oracle = self._oracle()
        extra_row = {**oracle[0], "task_code": "BRAKE_INSPECTION"}
        view = oracle + [extra_row]
        ok, _, issues = _compare_pm_results(oracle, view)
        assert not ok
        assert any("extra in view" in i for i in issues)

    def test_missing_view_row(self):
        """Bad world: oracle computed a row the view is missing."""
        oracle = self._oracle()
        ok, _, issues = _compare_pm_results(oracle, [])
        assert not ok
        assert any("missing from view" in i for i in issues)

    def test_view_wrong_status(self):
        """Bad world: view status is 'overdue' but oracle says 'current'."""
        oracle = self._oracle(status="current")
        wrong_view = [{**oracle[0], "status": "overdue"}]
        ok, _, issues = _compare_pm_results(oracle, wrong_view)
        assert not ok
        assert any("status" in i for i in issues)

    def test_view_wrong_source(self):
        """Bad world: view says dms_repair_order but oracle says service_records."""
        oracle = self._oracle()
        wrong_view = [{**oracle[0], "last_done_source": "dms_repair_order"}]
        ok, _, issues = _compare_pm_results(oracle, wrong_view)
        assert not ok
        assert any("last_done_source" in i for i in issues)



# ---------------------------------------------------------------------------
# 6. TestMileageFloatCast — C1: miles_driven is double; _cast_bigint rounds half-away-from-zero
# ---------------------------------------------------------------------------


class TestMileageFloatCast:
    """C1: miles_driven is a double; int() raises; CAST(double AS BIGINT) rounds half-away.

    Source: adp-wt-fi-health-d-g6 fleet_ops/daily_usage.sql:39 (double);
            pm_vehicle_status.sql final SELECT CAST(current_odo AS BIGINT),
            CAST(last_done_odo AS BIGINT) separately.
    """

    def _sched_plan(self, interval: str = "500") -> dict:
        return _make_plan(
            fleet_id=_FLT1, task_code="SCHEDULED_MAINTENANCE", basis="mileage",
            interval_value=interval, start_date="2026-01-01",
            applies_to={"powertrains": {"L": [{"S": "ev"}]}, "models": {"NULL": True}},
        )

    def test_fractional_string_does_not_raise(self):
        """miles_driven='42.7' (double-as-string) must not raise; int('42.7') raises ValueError."""
        inp = _base_inputs(
            pm_plans=[self._sched_plan()],
            service_records=[_make_sr(_V1, "scheduled_maintenance", "2026-09-01")],
            daily_usage=[_make_usage(_V1, "2026-06-01", "42.7")],
        )
        rows = _run_oracle(inp)
        assert len(rows) >= 1

    def test_half_boundary_rounds_away_from_zero(self):
        """CAST(double AS BIGINT) rounds 42.5 → 43; Python round() rounds to even (42).

        Bad world for rounding: using round() instead of half-away-from-zero gives
        next_due_miles='542' (round(42.5)+500), not '543' (_cast_bigint(42.5)+500).
        usage_date == last_done (SR 2026-09-01): d <= last_done → included in last_odo.
        """
        inp = _base_inputs(
            pm_plans=[self._sched_plan(interval="500")],
            service_records=[_make_sr(_V1, "scheduled_maintenance", "2026-09-01")],
            daily_usage=[_make_usage(_V1, "2026-09-01", "42.5")],
        )
        rows = _run_oracle(inp)
        assert len(rows) == 1
        # Trino: _cast_bigint(42.5) = 43; Python round(42.5) = 42 (banker's rounding)
        assert rows[0]["next_due_miles"] == "543"  # 43 + 500, not 42 + 500



# ---------------------------------------------------------------------------
# 7. TestItem4LfDenial — W1: LF denial / COLUMN_NOT_FOUND → FAIL naming table+grant
# ---------------------------------------------------------------------------


class TestItem4LfDenial:
    """W1: COLUMN_NOT_FOUND / not-authorized maps to FAIL naming table+grant (not SKIP).

    Source: review W1 [item 4]; decisions.md:2405-2409 (operator lacks SELECT on
    stack-created tables); group6-notes.md §3 (fault view COLUMN_NOT_FOUND or not authorized).
    """

    def _arp_deny(self, deny_table: str, err_msg: str):
        """Fake _athena_run_paged: succeed on vin_cms_fleet, deny on deny_table."""
        def _fake(client, sql, workgroup, timeout_s=180.0):
            if "vin_cms_fleet" in sql:
                return True, "", [[_V1, _FLT1]]
            if deny_table in sql:
                return False, err_msg, []
            return True, "", []
        return _fake

    def _run_item4(self, side_effect):
        from unittest import mock  # noqa: PLC0415
        res = _mod.Results()
        with (mock.patch.object(_mod, "_client", return_value=mock.MagicMock()),
              mock.patch.object(_mod, "_athena_run_paged", side_effect=side_effect)):
            _mod._item_4(res, stage="staging", account="111122223333",
                         run="r1", args=None)
        return res

    def test_column_not_found_emits_fail_with_grant_not_skip(self):
        """COLUMN_NOT_FOUND on pm_plans → FAIL label '4-lf-pm_plans', detail has 'grant'.

        Bad world: current code emits FAIL '4-pm_plans' with raw error (no grant hint).
        After fix: FAIL '4-lf-pm_plans' with 'grant' in detail; not SKIP.
        """
        err = ("COLUMN_NOT_FOUND: col 'applies_to' or requester is not authorized "
               "to SELECT on adp_staging_cms_ingest.pm_plans")
        res = self._run_item4(self._arp_deny("pm_plans", err))
        skips = [r for r in res.rows if r[1] == "SKIP" and "pm_plans" in r[2]]
        fails_lf = [r for r in res.rows
                    if r[1] == "FAIL" and "4-lf-pm_plans" == r[0]]
        assert not skips, f"LF denial must not SKIP; rows={res.rows}"
        assert fails_lf, f"expected FAIL '4-lf-pm_plans'; rows={res.rows}"
        assert "grant" in fails_lf[0][2].lower(), f"detail missing 'grant'; got {fails_lf[0][2]}"

    def test_not_authorized_also_fails_with_grant(self):
        """'is not authorized' on repair_orders → same FAIL with grant hint."""
        err = "is not authorized to perform: athena:GetQueryResults on dms_staging.repair_orders"
        res = self._run_item4(self._arp_deny("repair_orders", err))
        fails_lf = [r for r in res.rows
                    if r[1] == "FAIL" and "4-lf-repair_orders" == r[0]]
        assert fails_lf, f"expected FAIL '4-lf-repair_orders'; rows={res.rows}"
        assert "grant" in fails_lf[0][2].lower()



# ---------------------------------------------------------------------------
# Helper: minimal fake _athena_run_paged for _item_4 e2e tests
# ---------------------------------------------------------------------------


def _item4_fake_arp(
    snap_data_as_of: str = "2026-10-13",
    params_data_as_of: str = "2026-10-13",
    snap_status: str = "no_history",
    snap_next_due: str = "2026-12-30",
):
    """Fake _athena_run_paged for _item_4 end-to-end tests.

    All tables return success; snapshot returns 1 row with snap_data_as_of.
    Snap row has 9 columns (8 data + data_as_of) to match the post-fix SELECT.
    pm_vehicle_status_daily checked before pm_vehicle_status (substring order).
    Source: _item_4 SQL queries (table names as discriminators).
    """
    def _fake(client, sql, workgroup, timeout_s=180.0):  # noqa: ARG001
        if "vin_cms_fleet" in sql:
            return True, "", [[_V1, _FLT1]]
        if "pm_plans" in sql:
            return True, "", []           # no plans → oracle = 0 rows
        if "vehicles" in sql:
            return True, "", []
        if "service_records" in sql:
            return True, "", []
        if "repair_orders" in sql:
            return True, "", []
        if "pm_operator_completions" in sql:
            return True, "", []
        if "code_labor_op_map" in sql:
            return True, "", []
        if "daily_usage" in sql:
            return True, "", []
        if "parameters" in sql:
            return True, "", [["30", "500", params_data_as_of]]
        # pm_vehicle_status_daily BEFORE pm_vehicle_status (substring order)
        if "pm_vehicle_status_daily" in sql:
            # 9 cols: vin,task_code,basis,last_done_date,last_done_source,
            #         next_due_date,next_due_miles,status,data_as_of
            return True, "", [[_V1, "TIRE_ROTATION", "calendar", "", "",
                               snap_next_due, "", snap_status, snap_data_as_of]]
        if "pm_vehicle_status" in sql:
            return True, "", []           # empty view → oracle(0) vs view(0) → PASS
        return True, "", []
    return _fake


# ---------------------------------------------------------------------------
# 8. TestItem4DailySkew — W2: snapshot comparison gated on matching data_as_of
# ---------------------------------------------------------------------------


class TestItem4DailySkew:
    """W2: oracle uses today's data_as_of; snapshot may be from an earlier date.

    Compare only when snapshot.data_as_of == parameters.data_as_of; NOTE otherwise.
    Source: review W2 [item 4]; pm_vehicle_status_daily snapshot_date vs parameters.data_as_of.
    """

    def _run(self, snap_data_as_of: str, params_data_as_of: str = "2026-10-13"):
        from unittest import mock  # noqa: PLC0415
        res = _mod.Results()
        arp = _item4_fake_arp(snap_data_as_of=snap_data_as_of,
                              params_data_as_of=params_data_as_of)
        with (mock.patch.object(_mod, "_client", return_value=mock.MagicMock()),
              mock.patch.object(_mod, "_athena_run_paged", side_effect=arp)):
            _mod._item_4(res, stage="staging", account="111122223333",
                         run="r1", args=None)
        return res

    def test_mismatched_data_as_of_emits_note_not_fail(self):
        """Snapshot data_as_of='2026-10-10' != params '2026-10-13' → NOTE 4-daily-skew.

        Bad world (before fix): comparison proceeds → FAIL 4-daily-match (oracle=0 vs snap=1).
        After fix: NOTE 4-daily-skew; 4-daily-match is not checked.
        """
        res = self._run(snap_data_as_of="2026-10-10", params_data_as_of="2026-10-13")
        has_skew = any("4-daily-skew" in r[0] and r[1] == "NOTE" for r in res.rows)
        has_match = any("4-daily-match" in r[0] for r in res.rows)
        assert has_skew, f"expected NOTE '4-daily-skew'; rows={res.rows}"
        assert not has_match, f"4-daily-match must not run on skewed snapshot; rows={res.rows}"

    def test_matching_data_as_of_proceeds_to_comparison(self):
        """Snapshot data_as_of == params.data_as_of → comparison runs (4-daily-match present).

        Positive control: matching dates still exercise the comparison path.
        """
        res = self._run(snap_data_as_of="2026-10-13", params_data_as_of="2026-10-13")
        has_match = any("4-daily-match" in r[0] for r in res.rows)
        has_skew = any("4-daily-skew" in r[0] for r in res.rows)
        assert has_match, f"expected 4-daily-match check; rows={res.rows}"
        assert not has_skew, f"no skew note expected when dates match; rows={res.rows}"

    def test_empty_daily_snapshot_fails_not_note(self):
        """Empty pm_vehicle_status_daily → FAIL 4-daily-rows, NOT NOTE 4-daily-skew.

        Bad world (before fix): snap_rows=[], snap_data_as_of=None, None != data_as_of
        triggers NOTE 4-daily-skew and returns without FAIL; item 4 passes.
        After fix: `if not snap_rows:` gate fires first → FAIL 4-daily-rows; no skew note.
        Source: review W2 [item 4].
        """
        from unittest import mock  # noqa: PLC0415

        def _empty_daily_arp(client, sql, workgroup, timeout_s=180.0):  # noqa: ARG001
            if "vin_cms_fleet" in sql:
                return True, "", [[_V1, _FLT1]]
            if "pm_plans" in sql:
                return True, "", []
            if "vehicles" in sql:
                return True, "", []
            if "service_records" in sql:
                return True, "", []
            if "repair_orders" in sql:
                return True, "", []
            if "pm_operator_completions" in sql:
                return True, "", []
            if "code_labor_op_map" in sql:
                return True, "", []
            if "daily_usage" in sql:
                return True, "", []
            if "parameters" in sql:
                return True, "", [["30", "500", "2026-10-13"]]
            if "pm_vehicle_status_daily" in sql:
                return True, "", []     # empty snapshot — the case under test
            if "pm_vehicle_status" in sql:
                return True, "", []
            return True, "", []

        res = _mod.Results()
        with (mock.patch.object(_mod, "_client", return_value=mock.MagicMock()),
              mock.patch.object(_mod, "_athena_run_paged", side_effect=_empty_daily_arp)):
            _mod._item_4(res, stage="staging", account="111122223333",
                         run="r1", args=None)

        fail_ids = [r[0] for r in res.rows if r[1] == "FAIL"]
        has_rows_fail = any("4-daily-rows" in fid for fid in fail_ids)
        has_skew = any("4-daily-skew" in r[0] for r in res.rows)
        assert has_rows_fail, (
            f"expected FAIL '4-daily-rows' for empty snapshot; rows={res.rows}"
        )
        assert not has_skew, (
            f"4-daily-skew must not fire when snapshot is empty; rows={res.rows}"
        )



# ---------------------------------------------------------------------------
# 9. TestComparePmResultsZeroRows — W3: zero oracle rows → FAIL; per-status counts
# ---------------------------------------------------------------------------


class TestComparePmResultsZeroRows:
    """W3: _compare_pm_results with zero oracle rows must FAIL (not vacuous PASS).

    Also: summary must include per-status counts so a sample with no 'due'/'overdue'
    rows is visible.
    Source: review W3 [item 4]; testing.md 'mutation testing at the green boundary'.
    """

    def test_empty_oracle_fails(self):
        """oracle=[] and view=[] → ok=False (not vacuous PASS).

        Bad world: current code returns ok=True on ([], []) (set-equality trivially holds).
        """
        ok, summary, issues = _compare_pm_results([], [])
        assert not ok, f"empty oracle must FAIL; got ok=True, summary={summary!r}"

    def test_empty_oracle_summary_says_zero(self):
        """Summary on empty oracle mentions '0 rows' or 'vacuous'."""
        _, summary, _ = _compare_pm_results([], [])
        assert "0" in summary or "vacuous" in summary.lower(), f"summary={summary!r}"

    def test_status_counts_in_summary(self):
        """Summary includes per-status counts: overdue/due/current/no_history."""
        oracle = self._oracle()
        _, summary, _ = _compare_pm_results(oracle, oracle)  # matching, oracle non-empty
        assert "overdue" in summary or "due" in summary or "current" in summary or \
               "no_history" in summary, f"summary missing status counts: {summary!r}"

    def _oracle(self):
        oracle = [
            {"vin": _V1, "task_code": "TIRE_ROTATION", "basis": "calendar",
             "last_done_date": "2026-09-01", "last_done_source": "service_records",
             "next_due_date": "2026-11-30", "next_due_miles": "", "status": "current"},
        ]
        view = [{**oracle[0]}]
        return oracle

    def test_per_status_counts_reported(self):
        """Summary for 2 oracle rows (one 'current', one 'overdue') shows both counts."""
        oracle = [
            {"vin": _V1, "task_code": "TIRE_ROTATION", "basis": "calendar",
             "last_done_date": "2026-09-01", "last_done_source": "service_records",
             "next_due_date": "2026-11-30", "next_due_miles": "", "status": "current"},
            {"vin": _V2, "task_code": "BRAKE_INSPECTION", "basis": "calendar",
             "last_done_date": "2025-01-01", "last_done_source": "service_records",
             "next_due_date": "2025-07-01", "next_due_miles": "", "status": "overdue"},
        ]
        _, summary, _ = _compare_pm_results(oracle, oracle)
        assert "current" in summary and "overdue" in summary, \
            f"summary missing per-status counts: {summary!r}"



# ---------------------------------------------------------------------------
# 10. TestItem4E2E — _item_4 end-to-end with fake Athena (all-string rows, fractional miles)
# ---------------------------------------------------------------------------


def _item4_e2e_arp():
    """Full fake _athena_run_paged for _item_4 happy path.

    Mileage plan for SCHEDULED_MAINTENANCE, interval=500.  Daily usage row with
    miles_driven='42.5' (fractional double-as-string; exercises C1 fix).

    Expected oracle row:
      vin=_V1, task=SCHEDULED_MAINTENANCE, basis=mileage, last_done=2026-09-01,
      source=service_records, next_due_miles=543 (_cast_bigint(42.5)+500=43+500),
      status=due (miles_left=500 == due_horizon_miles=500).

    View fake returns the matching row; snapshot fake has the same data_as_of.
    All Athena values are strings (Athena wire format).
    Source: _item_4 SQL table names as discriminators; _fetch uses zip(cols, row).
    """
    import json as _json  # noqa: PLC0415
    applies_to = _json.dumps({"powertrains": {"L": [{"S": "ev"}]},
                              "models": {"NULL": True}})
    # view row: 11 cols vin,fleet_id,task_code,basis,interval_value,
    #           last_done_date,last_done_source,next_due_date,next_due_miles,status,data_as_of
    view_row = [_V1, _FLT1, "SCHEDULED_MAINTENANCE", "mileage", "500",
                "2026-09-01", "service_records", "", "543", "due", "2026-10-13"]
    # snapshot row: 9 cols (8 data + data_as_of); same values as oracle
    snap_row = [_V1, "SCHEDULED_MAINTENANCE", "mileage", "2026-09-01",
                "service_records", "", "543", "due", "2026-10-13"]

    def _fake(client, sql, workgroup, timeout_s=180.0):  # noqa: ARG001
        if "vin_cms_fleet" in sql:
            return True, "", [[_V1, _FLT1]]
        if "pm_plans" in sql:
            # 8 cols: plan_id, fleet_id, vehicle_id, applies_to,
            #         task_code, basis, interval_value, start_date
            return True, "", [["plan-1", _FLT1, "", applies_to,
                               "SCHEDULED_MAINTENANCE", "mileage", "500", "2026-01-01"]]
        if "vehicles" in sql:
            # 5 cols: vehicle_id, vin, model, model_year, fuel_type
            return True, "", [["VEH-001", _V1, "ModelX", "2024", "electric"]]
        if "service_records" in sql:
            # 3 cols: vin, service_type, service_date
            return True, "", [[_V1, "scheduled_maintenance", "2026-09-01"]]
        if "repair_orders" in sql:
            return True, "", []
        if "pm_operator_completions" in sql:
            return True, "", []
        if "code_labor_op_map" in sql:
            return True, "", [["TIRE_ROTATION", "TIRE_ROTATION"]]
        if "daily_usage" in sql:
            # fractional miles_driven='42.5'; int() raises, float() works (C1)
            return True, "", [[_V1, "2026-09-01", "42.5"]]
        if "parameters" in sql:
            return True, "", [["30", "500", "2026-10-13"]]
        if "pm_vehicle_status_daily" in sql:
            return True, "", [snap_row]
        if "pm_vehicle_status" in sql:
            return True, "", [view_row]
        return True, "", []
    return _fake


class TestItem4E2E:
    """_item_4 end-to-end with fake Athena returning all-string rows (fractional miles)."""

    def test_item4_e2e_happy_path_passes(self):
        """Complete happy path: mileage plan, fractional miles, oracle matches view.

        Positive control (brief): a correct world PASSes.
        Exercises: C1 (_cast_bigint on '42.5'), W1 (no LF error), W2 (matching data_as_of),
        W3 (oracle non-empty → comparison not vacuous).
        """
        from unittest import mock  # noqa: PLC0415
        res = _mod.Results()
        with (mock.patch.object(_mod, "_client", return_value=mock.MagicMock()),
              mock.patch.object(_mod, "_athena_run_paged",
                                side_effect=_item4_e2e_arp())):
            _mod._item_4(res, stage="staging", account="111122223333",
                         run="e2e-test", args=None)

        fails = res.failed
        assert not fails, f"_item_4 should PASS; FAILs={fails}"
        # oracle must have 1 row with correct computed values
        view_pass = any(r[0] == "4-view-match" and r[1] == "PASS" for r in res.rows)
        assert view_pass, f"4-view-match must PASS; rows={res.rows}"
        daily_pass = any(r[0] == "4-daily-match" and r[1] == "PASS" for r in res.rows)
        assert daily_pass, f"4-daily-match must PASS; rows={res.rows}"
        # next_due_miles=543 confirms _cast_bigint(42.5)=43 (not round(42.5)=42)
        oracle_note = next((r for r in res.rows if r[0] == "4-oracle"), None)
        assert oracle_note and "1" in oracle_note[2], \
            f"oracle must have 1 row; note={oracle_note}"
