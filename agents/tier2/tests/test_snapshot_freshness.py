"""Tests for agents.tier2.snapshot_freshness (tests 1-6 helper; 7-9 run_service wire)."""
from __future__ import annotations

import datetime
import pytest

from agents.tier2.snapshot_freshness import (
    ADP_KEYS,
    SNAPSHOT_TABLES,
    SnapshotState,
    StaleSnapshotError,
    empty_keys,
    read_snapshot_dates,
    require_fresh,
)

TODAY = datetime.date(2026, 10, 10)
YESTERDAY = datetime.date(2026, 10, 9)
TWO_AGO = datetime.date(2026, 10, 8)
TOMORROW = datetime.date(2026, 10, 11)
KEYS = list(SNAPSHOT_TABLES)


def _state(
    d: datetime.date | None,
    *,
    row_count: int | None = 1,
    table_date: datetime.date | None = None,
) -> SnapshotState:
    return SnapshotState(date=d, row_count=row_count, table_date=table_date if table_date is not None else d)


def _all_states(d: datetime.date) -> dict[str, SnapshotState]:
    """All four keys with date=d; ADP keys row_count=1, managed_vehicles row_count=None."""
    out: dict[str, SnapshotState] = {}
    for k in KEYS:
        rc = 1 if k in ADP_KEYS else None
        out[k] = SnapshotState(date=d, row_count=rc, table_date=d)
    return out


def _stale_states(d: datetime.date) -> dict[str, SnapshotState]:
    """All four keys with stale date=d; ADP keys row_count=1."""
    return _all_states(d)


# ---------------------------------------------------------------------------
# Test 1: today and yesterday pass; returns the common date
# ---------------------------------------------------------------------------


def test_require_fresh_today_all_equal_returns_today():
    assert require_fresh(_all_states(TODAY), today=TODAY) == TODAY


def test_require_fresh_yesterday_all_equal_returns_yesterday():
    assert require_fresh(_all_states(YESTERDAY), today=TODAY) == YESTERDAY


# ---------------------------------------------------------------------------
# Test 2: date None → raises; ADP → "no marker", managed_vehicles → "empty"
# ---------------------------------------------------------------------------


def test_require_fresh_adp_none_raises_no_marker():
    states = _all_states(TODAY)
    states["vehicle_fault_status_daily"] = SnapshotState(date=None, row_count=None, table_date=None)
    with pytest.raises(StaleSnapshotError) as exc_info:
        require_fresh(states, today=TODAY)
    err = exc_info.value
    assert "no marker" in err.reason
    assert "vehicle_fault_status_daily" in err.reason


def test_require_fresh_managed_vehicles_none_raises_empty():
    states = _all_states(TODAY)
    states["managed_vehicles"] = SnapshotState(date=None, row_count=None, table_date=None)
    with pytest.raises(StaleSnapshotError) as exc_info:
        require_fresh(states, today=TODAY)
    err = exc_info.value
    assert "empty" in err.reason
    assert "managed_vehicles" in err.reason


# ---------------------------------------------------------------------------
# Test 3: stale → raises
# ---------------------------------------------------------------------------


def test_require_fresh_two_days_old_raises_stale():
    with pytest.raises(StaleSnapshotError) as exc_info:
        require_fresh(_all_states(TWO_AGO), today=TODAY)
    assert "stale" in exc_info.value.reason


# ---------------------------------------------------------------------------
# Test 4: mixed dates (same_date=True) → raises
# ---------------------------------------------------------------------------


def test_require_fresh_mixed_dates_raises():
    states = _all_states(TODAY)
    states["managed_vehicles"] = SnapshotState(date=YESTERDAY, row_count=None, table_date=YESTERDAY)
    with pytest.raises(StaleSnapshotError) as exc_info:
        require_fresh(states, today=TODAY)
    assert "mixed" in exc_info.value.reason


# ---------------------------------------------------------------------------
# Test 5: future date → raises
# ---------------------------------------------------------------------------


def test_require_fresh_future_date_raises():
    with pytest.raises(StaleSnapshotError):
        require_fresh(_all_states(TOMORROW), today=TODAY)


# ---------------------------------------------------------------------------
# Test 6: same_date=False
# ---------------------------------------------------------------------------


def test_require_fresh_same_date_false_mixed_passes_returns_earliest():
    states = {
        "managed_vehicles": SnapshotState(date=YESTERDAY, row_count=None, table_date=YESTERDAY),
        "vehicle_fault_status_daily": SnapshotState(date=TODAY, row_count=1, table_date=TODAY),
    }
    result = require_fresh(states, today=TODAY, same_date=False)
    assert result == YESTERDAY, (
        "Mutation: flipping same_date back to True makes mixed dates refuse → fails."
    )


def test_require_fresh_same_date_false_stale_still_raises():
    states = {
        "managed_vehicles": SnapshotState(date=TWO_AGO, row_count=None, table_date=TWO_AGO),
        "vehicle_fault_status_daily": SnapshotState(date=TODAY, row_count=1, table_date=TODAY),
    }
    with pytest.raises(StaleSnapshotError, match="stale snapshot"):
        require_fresh(states, today=TODAY, same_date=False)


def test_require_fresh_same_date_true_mixed_still_raises():
    states = {
        "managed_vehicles": SnapshotState(date=YESTERDAY, row_count=None, table_date=YESTERDAY),
        "vehicle_fault_status_daily": SnapshotState(date=TODAY, row_count=1, table_date=TODAY),
    }
    with pytest.raises(StaleSnapshotError, match="mixed"):
        require_fresh(states, today=TODAY)


# ---------------------------------------------------------------------------
# ADP marker-vs-table disagree rules
# ---------------------------------------------------------------------------


def test_require_fresh_adp_nonzero_table_date_differs_raises_disagree():
    """row_count > 0 but table_date != marker date → disagree."""
    states = _all_states(TODAY)
    states["vehicle_fault_status_daily"] = SnapshotState(
        date=TODAY, row_count=5, table_date=YESTERDAY
    )
    with pytest.raises(StaleSnapshotError) as exc_info:
        require_fresh(states, today=TODAY)
    assert "disagree" in exc_info.value.reason
    assert "vehicle_fault_status_daily" in exc_info.value.reason


def test_require_fresh_adp_nonzero_table_date_matches_no_error():
    """row_count > 0, table_date == date → ok."""
    require_fresh(_all_states(TODAY), today=TODAY)  # row_count=1, table_date=TODAY each


def test_require_fresh_adp_zero_table_date_after_marker_raises_disagree():
    """row_count == 0 but table has newer data → disagree."""
    states = _all_states(TODAY)
    states["vehicle_fault_status_daily"] = SnapshotState(
        date=TODAY, row_count=0, table_date=TOMORROW
    )
    with pytest.raises(StaleSnapshotError) as exc_info:
        require_fresh(states, today=TODAY)
    assert "disagree" in exc_info.value.reason


def test_require_fresh_adp_zero_table_date_before_marker_no_error():
    """row_count == 0, table_date older than marker → empty run is fresh, no error."""
    states = _all_states(TODAY)
    states["vehicle_fault_status_daily"] = SnapshotState(
        date=TODAY, row_count=0, table_date=YESTERDAY
    )
    require_fresh(states, today=TODAY)  # must not raise


def test_require_fresh_adp_zero_table_date_none_no_error():
    """row_count == 0, table_date=None (truly empty table) → no error."""
    states = _all_states(TODAY)
    states["vehicle_fault_status_daily"] = SnapshotState(
        date=TODAY, row_count=0, table_date=None
    )
    require_fresh(states, today=TODAY)  # must not raise


# ---------------------------------------------------------------------------
# empty_keys
# ---------------------------------------------------------------------------


def test_empty_keys_adp_zero_row_count_returned():
    states = _all_states(TODAY)
    states["vehicle_fault_status_daily"] = SnapshotState(
        date=TODAY, row_count=0, table_date=YESTERDAY
    )
    assert empty_keys(states) == frozenset({"vehicle_fault_status_daily"})


def test_empty_keys_managed_vehicles_excluded():
    """managed_vehicles is not an ADP key; ignored by empty_keys."""
    states = _all_states(TODAY)
    states["managed_vehicles"] = SnapshotState(date=TODAY, row_count=0, table_date=TODAY)
    assert "managed_vehicles" not in empty_keys(states)


def test_empty_keys_none_empty_returns_empty_frozenset():
    assert empty_keys(_all_states(TODAY)) == frozenset()


def test_empty_keys_all_adp_zero():
    states = _all_states(TODAY)
    for k in list(ADP_KEYS):
        states[k] = SnapshotState(date=TODAY, row_count=0, table_date=YESTERDAY)
    assert empty_keys(states) == ADP_KEYS


# ---------------------------------------------------------------------------
# read_snapshot_dates (helper tests, new row format)
# ---------------------------------------------------------------------------


def _stub_all_present(sql: str, **_kw) -> list[dict]:
    rows = []
    for k in KEYS:
        rows.append({
            "table_key": k,
            "marker_date": str(TODAY),
            "row_count": "100" if k in ADP_KEYS else None,
            "table_date": str(TODAY),
        })
    return rows


def _stub_missing_managed(sql: str, **_kw) -> list[dict]:
    return [r for r in _stub_all_present(sql) if r["table_key"] != "managed_vehicles"]


def test_read_snapshot_dates_returns_snapshot_states():
    result = read_snapshot_dates(
        "staging", KEYS,
        athena_client=object(), workgroup="w", results_s3_path="s3://r/",
        _run_query=_stub_all_present,
    )
    for k in KEYS:
        s = result[k]
        assert isinstance(s, SnapshotState)
        assert s.date == TODAY
        assert s.table_date == TODAY
    for k in ADP_KEYS:
        assert result[k].row_count == 100
    assert result["managed_vehicles"].row_count is None


def test_read_snapshot_dates_issues_one_query_naming_all_tables():
    captured: list[str] = []

    def capturing(sql: str, **_kw):
        captured.append(sql)
        return _stub_all_present(sql)

    read_snapshot_dates(
        "staging", KEYS,
        athena_client=object(), workgroup="w", results_s3_path="s3://r/",
        _run_query=capturing,
    )
    assert len(captured) == 1, "must issue exactly one query"
    sql = captured[0]
    # ADP keys reference snapshot_runs; managed_vehicles references its own table
    assert "adp_staging_fleet_health.snapshot_runs" in sql
    non_adp = SNAPSHOT_TABLES["managed_vehicles"].replace("${stage}", "staging")
    assert non_adp in sql


def test_read_snapshot_dates_missing_key_raises():
    with pytest.raises(StaleSnapshotError) as exc_info:
        read_snapshot_dates(
            "staging", KEYS,
            athena_client=object(), workgroup="w", results_s3_path="s3://r/",
            _run_query=_stub_missing_managed,
        )
    assert "missing" in exc_info.value.reason


# Tests 7-9 added in step 3 after run_service is wired.


# ---------------------------------------------------------------------------
# Shared doubles for tests 7-9 (run_service wiring)
# ---------------------------------------------------------------------------

import datetime as _dt
from typing import Any
from agents.tier2 import runner


class _AvxWriter:
    table_name = "vsa-staging-avx-findings"

    def __init__(self) -> None:
        self.dynamodb_client = self
        self.findings: list[dict] = []

    def query(self, **_kw: Any):
        return {"Items": []}

    def write_finding(self, finding: dict, *, standing: Any = None,
                      refresh_on_noop: bool = False) -> dict:
        self.findings.append(finding)
        return {"action": "put", "finding_id": finding.get("finding_id", "FIND")}


class _Cw:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def put_metric_data(self, **kw: Any) -> None:
        self.calls.append(kw)


_NOW = _dt.datetime(2026, 10, 10, 12, 0, 0, tzinfo=_dt.timezone.utc)
_FRESH = _all_states(TODAY)


def _call_run_service(monkeypatch, records, *, read_snapshot_side_effect=None,
                      _assemble_probe=None, **kw):
    """Run run_service with minimal doubles; select_* readers return empty."""
    from agents.fi_fleet_health import selection as fh_sel
    from agents.fi_recall_warranty import selection as rw_sel

    monkeypatch.setattr(fh_sel, "select_fault_rows", lambda *_a, **_kw: [])
    monkeypatch.setattr(fh_sel, "select_pm_rows", lambda *_a, **_kw: [])
    monkeypatch.setattr(fh_sel, "select_managed_vins", lambda *_a, **_kw: [])
    monkeypatch.setattr(fh_sel, "select_daily_usage", lambda *_a, **_kw: [])
    monkeypatch.setattr(fh_sel, "select_agent2_findings", lambda *_a, **_kw: ({}, 0))
    def _assemble(**_kw):
        if _assemble_probe is not None:
            _assemble_probe.append(1)
        return [], {}

    monkeypatch.setattr(fh_sel, "build_vehicle_rows", _assemble)
    monkeypatch.setattr(rw_sel, "select_parts_stock", lambda *_a, **_kw: [])

    side = read_snapshot_side_effect or (lambda *_a, **_kw: _FRESH)
    monkeypatch.setattr(runner, "_read_snapshot_dates", side)

    avx = _AvxWriter()
    return runner.run_service(
        "staging",
        athena_client=object(), workgroup="cvx-staging-analytics",
        results_s3_path="s3://results/",
        ddb_client=object(),
        findings_table="vsa-staging-avx-findings",
        actions_table="vsa-staging-avx-actions",
        groundings_table="cms-staging-storage-vehicle-groundings",
        vehicles_table="cms-staging-storage-vehicles",
        fleet_locations_table="cms-staging-storage-fleet-locations",
        avx_writer=avx,
        cloudwatch_client=_Cw(),
        run_record_writer=lambda **k: records.append(k),
        now=_NOW,
        **kw,
    ), avx


# ---------------------------------------------------------------------------
# Test 7: stale dates → abort, no Finding, abort run record, select_* not called
# ---------------------------------------------------------------------------


def test_run_service_stale_dates_aborts_before_reads(monkeypatch):
    """Stale snapshot → StaleSnapshotError raised before any select_* reader is called."""
    from agents.fi_fleet_health import selection as fh_sel
    from agents.fi_recall_warranty import selection as rw_sel

    called: list[str] = []

    def track(name: str):
        def f(*_a, **_kw):
            called.append(name)
            return []
        return f

    stale = _stale_states(TWO_AGO)

    monkeypatch.setattr(fh_sel, "select_fault_rows", track("fault_rows"))
    monkeypatch.setattr(fh_sel, "select_pm_rows", track("pm_rows"))
    monkeypatch.setattr(fh_sel, "select_managed_vins", track("managed_vins"))
    monkeypatch.setattr(fh_sel, "select_daily_usage", track("usage"))
    monkeypatch.setattr(fh_sel, "select_agent2_findings", lambda *_a, **_kw: ({}, 0))
    monkeypatch.setattr(fh_sel, "build_vehicle_rows", lambda **_kw: ([], {}))
    monkeypatch.setattr(rw_sel, "select_parts_stock", lambda *_a, **_kw: [])
    _snap_keys_seen: list[set] = []

    def _stale_side(stage, keys, **kw):
        _snap_keys_seen.append(set(keys))
        return stale

    monkeypatch.setattr(runner, "_read_snapshot_dates", _stale_side)

    avx = _AvxWriter()
    records: list = []

    with pytest.raises(StaleSnapshotError):
        runner.run_service(
            "staging",
            athena_client=object(), workgroup="cvx-staging-analytics",
            results_s3_path="s3://results/",
            ddb_client=object(),
            findings_table="vsa-staging-avx-findings",
            actions_table="vsa-staging-avx-actions",
            groundings_table="cms-staging-storage-vehicle-groundings",
            vehicles_table="cms-staging-storage-vehicles",
            fleet_locations_table="cms-staging-storage-fleet-locations",
            avx_writer=avx, cloudwatch_client=_Cw(),
            run_record_writer=lambda **k: records.append(k),
            now=_NOW,
        )

    assert not avx.findings, "no Finding must be written when snapshot is stale"
    assert not called, f"select_* readers must not be called; got {called}"
    (rec,) = records
    assert rec["status"] == "aborted"
    assert _snap_keys_seen and all(k == set(KEYS) for k in _snap_keys_seen), (
        f"_read_snapshot_dates must use all four SNAPSHOT_TABLES keys; got {_snap_keys_seen}. "
        "Mutation: drop a key → set differs → CAUGHT."
    )


# ---------------------------------------------------------------------------
# Test 8: dates change mid-run → StaleSnapshotError raised, no Finding written
# ---------------------------------------------------------------------------


def test_run_service_dates_change_mid_run_aborts(monkeypatch):
    """Second snapshot read returns different dates → mid-run shift detected."""
    call_n: list[int] = [0]
    _snap_keys_8: list[set] = []

    def shifting(stage, keys, **_kw):
        _snap_keys_8.append(set(keys))
        call_n[0] += 1
        if call_n[0] == 1:
            return _all_states(TODAY)
        return _all_states(YESTERDAY)  # shifted after four reads

    from agents.fi_fleet_health import selection as fh_sel

    records: list = []
    assembled: list[int] = []
    with pytest.raises(StaleSnapshotError):
        _call_run_service(monkeypatch, records,
                          read_snapshot_side_effect=shifting,
                          _assemble_probe=assembled)

    assert call_n[0] == 2, "the dates must be read before AND after the four reads"
    assert not assembled, "nothing may be assembled (so nothing written) after the dates moved"
    assert len(records) == 1 and records[0]["status"] == "aborted"
    assert _snap_keys_8 and all(k == set(KEYS) for k in _snap_keys_8), (
        f"_read_snapshot_dates must use all four SNAPSHOT_TABLES keys; got {_snap_keys_8}."
    )


# ---------------------------------------------------------------------------
# Test 9: fresh dates → summary carries snapshot_date == "2026-10-10"
# ---------------------------------------------------------------------------


def test_run_service_fresh_snapshot_date_in_summary(monkeypatch):
    """Successful run: summary['snapshot_date'] == '2026-10-10'."""
    records: list = []
    summary, _avx = _call_run_service(monkeypatch, records)
    assert summary["snapshot_date"] == "2026-10-10"


# ---------------------------------------------------------------------------
# Test 10: _read_snapshot_dates is called with all four SNAPSHOT_TABLES keys
# ---------------------------------------------------------------------------


def test_run_service_snapshot_keys_are_all_four_tables(monkeypatch):
    """Both _read_snapshot_dates calls must use exactly the four SNAPSHOT_TABLES keys."""
    captured: list[set] = []

    def _stub(stage, keys, **kw):
        captured.append(set(keys))
        return _FRESH

    records: list = []
    _call_run_service(monkeypatch, records, read_snapshot_side_effect=_stub)
    assert len(captured) == 2, (
        f"run_service must call _read_snapshot_dates exactly twice; called {len(captured)}."
    )
    for seen in captured:
        assert seen == set(KEYS), (
            f"_read_snapshot_dates must be called with all four SNAPSHOT_TABLES keys; "
            f"got {seen!r}. Mutation: drop a key from _snap_keys → CAUGHT."
        )



# ---------------------------------------------------------------------------
# Test 11: no marker → run_service refuses (StaleSnapshotError)
# ---------------------------------------------------------------------------


def test_run_service_no_marker_refuses(monkeypatch):
    """ADP key with date=None (no marker written yet) → StaleSnapshotError, no Finding."""
    from agents.fi_fleet_health import selection as fh_sel
    from agents.fi_recall_warranty import selection as rw_sel

    no_marker = dict(_FRESH)
    no_marker["vehicle_fault_status_daily"] = SnapshotState(
        date=None, row_count=None, table_date=None
    )

    monkeypatch.setattr(fh_sel, "select_fault_rows",    lambda *_a, **_kw: [])
    monkeypatch.setattr(fh_sel, "select_pm_rows",       lambda *_a, **_kw: [])
    monkeypatch.setattr(fh_sel, "select_managed_vins",  lambda *_a, **_kw: [])
    monkeypatch.setattr(fh_sel, "select_daily_usage",   lambda *_a, **_kw: [])
    monkeypatch.setattr(fh_sel, "select_agent2_findings", lambda *_a, **_kw: ({}, 0))
    monkeypatch.setattr(fh_sel, "build_vehicle_rows",   lambda **_kw: ([], {}))
    monkeypatch.setattr(rw_sel, "select_parts_stock",   lambda *_a, **_kw: [])
    monkeypatch.setattr(runner, "_read_snapshot_dates", lambda *_a, **_kw: no_marker)

    avx = _AvxWriter()
    records: list = []
    with pytest.raises(StaleSnapshotError) as exc_info:
        runner.run_service(
            "staging",
            athena_client=object(), workgroup="cvx-staging-analytics",
            results_s3_path="s3://results/",
            ddb_client=object(),
            findings_table="vsa-staging-avx-findings",
            actions_table="vsa-staging-avx-actions",
            groundings_table="cms-staging-storage-vehicle-groundings",
            vehicles_table="cms-staging-storage-vehicles",
            fleet_locations_table="cms-staging-storage-fleet-locations",
            avx_writer=avx, cloudwatch_client=_Cw(),
            run_record_writer=lambda **k: records.append(k),
            now=_NOW,
        )
    assert "no marker" in exc_info.value.reason
    assert not avx.findings, "no Finding must be written on a no-marker refuse"
    assert records and records[0]["status"] == "aborted"


# ---------------------------------------------------------------------------
# Test 12: empty fault marker → fault_rows not read from Athena
# ---------------------------------------------------------------------------


def test_run_service_empty_fault_marker_fault_rows_not_used(monkeypatch):
    """Fresh empty fault marker (row_count=0) → select_fault_rows is NOT called.

    The table's MAX(snapshot_date) may still point at an older snapshot; calling
    select_fault_rows and assembling those rows would mix a stale vintage into
    the current run.

    Mutation guard: removing the ``_empty`` gate and always calling select_fault_rows
    means fault_rows_called is non-empty → assertion fails.
    """
    from agents.fi_fleet_health import selection as fh_sel
    from agents.fi_recall_warranty import selection as rw_sel

    # Empty fault marker: row_count=0, table_date=YESTERDAY (older snapshot remains).
    empty_fault_fresh = dict(_FRESH)
    empty_fault_fresh["vehicle_fault_status_daily"] = SnapshotState(
        date=TODAY, row_count=0, table_date=YESTERDAY
    )

    fault_rows_called: list[int] = []

    def _fake_fault_rows(*_a, **_kw):
        fault_rows_called.append(1)
        return [{"vin": "VIN-OLD", "code": "P0420", "snapshot_date": str(YESTERDAY)}]

    fault_rows_to_build: list = []

    def _probing_build(**kw):
        fault_rows_to_build.extend(kw.get("fault_rows", []))
        return [], {}

    monkeypatch.setattr(fh_sel, "select_fault_rows",    _fake_fault_rows)
    monkeypatch.setattr(fh_sel, "select_pm_rows",       lambda *_a, **_kw: [])
    monkeypatch.setattr(fh_sel, "select_managed_vins",  lambda *_a, **_kw: [])
    monkeypatch.setattr(fh_sel, "select_daily_usage",   lambda *_a, **_kw: [])
    monkeypatch.setattr(fh_sel, "select_agent2_findings", lambda *_a, **_kw: ({}, 0))
    monkeypatch.setattr(fh_sel, "build_vehicle_rows",   _probing_build)
    monkeypatch.setattr(rw_sel, "select_parts_stock",   lambda *_a, **_kw: [])
    monkeypatch.setattr(runner, "_read_snapshot_dates", lambda *_a, **_kw: empty_fault_fresh)

    avx = _AvxWriter()
    records: list = []
    summary = runner.run_service(
        "staging",
        athena_client=object(), workgroup="cvx-staging-analytics",
        results_s3_path="s3://results/",
        ddb_client=object(),
        findings_table="vsa-staging-avx-findings",
        actions_table="vsa-staging-avx-actions",
        groundings_table="cms-staging-storage-vehicle-groundings",
        vehicles_table="cms-staging-storage-vehicles",
        fleet_locations_table="cms-staging-storage-fleet-locations",
        avx_writer=avx, cloudwatch_client=_Cw(),
        run_record_writer=lambda **k: records.append(k),
        now=_NOW,
    )

    assert fault_rows_called == [], (
        "select_fault_rows must NOT be called when the marker says row_count==0. "
        "Mutation: removing the _empty gate calls select_fault_rows → CAUGHT."
    )
    assert fault_rows_to_build == [], (
        "build_vehicle_rows must receive fault_rows=[] when the marker is empty. "
        "Mutation: passing the old-snapshot rows → CAUGHT."
    )
    assert not avx.findings, "no Finding must be written when all rows are empty"
    assert summary.get("empty_snapshot_keys") == ["vehicle_fault_status_daily"], (
        "empty_snapshot_keys must be ['vehicle_fault_status_daily'] in the summary. "
        "Mutation: not recording it → assertion fails."
    )



# ---------------------------------------------------------------------------
# C1 structural: every UNION ALL arm casts row_count to VARCHAR at every call site
# ---------------------------------------------------------------------------

import re as _re


def _arm_types(sql: str) -> list[str]:
    """Return the cast type of row_count for each UNION ALL arm."""
    return _re.findall(r"CAST\((?:sr\.row_count|NULL) AS (\w+)\) AS row_count", sql)


def test_union_all_row_count_types_are_all_varchar():
    """Every arm in every call-site UNION ALL casts row_count to VARCHAR (C1).

    Structural check: Athena/Trino has no implicit VARCHAR/BIGINT coercion
    in a set operation, so all arms must agree on VARCHAR.
    Mutation: change any CAST(… AS VARCHAR) to CAST(… AS BIGINT) → list not all-VARCHAR → CAUGHT.
    """
    captured: list[str] = []

    def _capture(sql, **_kw):
        captured.append(sql)
        return []  # empty rows → StaleSnapshotError raised, but SQL was captured

    try:
        read_snapshot_dates(
            "staging", list(SNAPSHOT_TABLES),
            athena_client=None, workgroup="w", results_s3_path="s3://r/",
            _run_query=_capture,
        )
    except StaleSnapshotError:
        pass  # expected: no rows returned

    assert captured, "no SQL captured"
    types = _arm_types(captured[0])
    assert types, "no row_count CAST found in SQL"
    assert all(t == "VARCHAR" for t in types), (
        f"All UNION ALL arms must CAST row_count to VARCHAR; got {types}. "
        "Mutation: CAST(NULL AS BIGINT) → list contains BIGINT → CAUGHT."
    )


# ---------------------------------------------------------------------------
# W1 DuckDB: marker SQL executes against ADP-shaped tables; newest-marker,
# per-table selection, and missing-marker cases pinned.
# ---------------------------------------------------------------------------

import duckdb as _ddb


def _fresh_marker_db(*, marker_days: list[datetime.date], rows_today: bool = True) -> _ddb.DuckDBPyConnection:
    """Return a DuckDB connection with ADP-shaped tables for the three ADP keys."""
    con = _ddb.connect(":memory:")
    con.execute("CREATE SCHEMA adp_staging_fleet_health")
    con.execute("CREATE SCHEMA adp_staging_fleet_recall")
    con.execute(
        "CREATE TABLE adp_staging_fleet_health.snapshot_runs "
        "(table_key VARCHAR, snapshot_date DATE, row_count BIGINT)"
    )
    for tbl in ("vehicle_fault_status_daily", "pm_vehicle_status_daily", "vehicle_utilisation_daily"):
        con.execute(
            f"CREATE TABLE adp_staging_fleet_health.{tbl} (vin VARCHAR, snapshot_date DATE)"
        )
    con.execute(
        "CREATE TABLE adp_staging_fleet_recall.managed_vehicles (vin VARCHAR, snapshot_date DATE)"
    )
    for d in marker_days:
        for key in ("vehicle_fault_status_daily", "pm_vehicle_status_daily", "vehicle_utilisation_daily"):
            n = 3 if (rows_today or d != marker_days[-1]) else 0
            con.execute(
                "INSERT INTO adp_staging_fleet_health.snapshot_runs VALUES (?, ?, ?)",
                (key, d, n),
            )
            if n:
                con.execute(
                    f"INSERT INTO adp_staging_fleet_health.{key} VALUES (?, ?)",
                    ("V1", d),
                )
        con.execute(
            "INSERT INTO adp_staging_fleet_recall.managed_vehicles VALUES (?, ?)",
            ("V1", d),
        )
    return con


def _athena_like(con: _ddb.DuckDBPyConnection):
    def rq(sql, **_kw):
        cur = con.execute(sql)
        names = [d[0] for d in cur.description]
        return [{n: (None if v is None else str(v)) for n, v in zip(names, row)}
                for row in cur.fetchall()]
    return rq


_D   = datetime.date(2026, 10, 10)
_D1  = _D - datetime.timedelta(1)
_D2  = _D - datetime.timedelta(2)


def test_marker_duckdb_newest_of_three_is_selected():
    """Three marker rows (D-2, D-1, D); read_snapshot_dates returns the newest (D).

    Mutation N1 (ORDER BY ASC): oldest picked → date == D-2 → assertion fails → CAUGHT.
    """
    con = _fresh_marker_db(marker_days=[_D2, _D1, _D])
    states = read_snapshot_dates(
        "staging", list(ADP_KEYS),
        athena_client=None, workgroup="w", results_s3_path="s3://r/",
        _run_query=_athena_like(con),
    )
    for k in ADP_KEYS:
        assert states[k].date == _D, (
            f"{k}: expected newest marker date {_D}, got {states[k].date}. "
            "Mutation N1 (ASC) picks oldest date → CAUGHT."
        )


def test_marker_duckdb_per_table_key_selection():
    """Each key's marker is read independently (WHERE table_key = k).

    Mutation N2 (no table_key filter): all keys get the same marker row;
    if row_counts differ per key the wrong count reaches empty_keys → CAUGHT.
    """
    con = _fresh_marker_db(marker_days=[_D])
    # Override fault to row_count=0 to create a distinguishable state per key.
    con.execute(
        "UPDATE adp_staging_fleet_health.snapshot_runs SET row_count = 0 "
        "WHERE table_key = 'vehicle_fault_status_daily'"
    )
    states = read_snapshot_dates(
        "staging", list(ADP_KEYS),
        athena_client=None, workgroup="w", results_s3_path="s3://r/",
        _run_query=_athena_like(con),
    )
    assert states["vehicle_fault_status_daily"].row_count == 0, (
        "fault row_count=0 must be selected for its own key. "
        "Mutation N2 (no table_key filter): a non-zero key's row is returned → row_count != 0 → CAUGHT."
    )
    for k in ("pm_vehicle_status_daily", "vehicle_utilisation_daily"):
        assert states[k].row_count == 3, (
            f"{k}: expected row_count=3; mutation N2 may return fault's row_count=0 → CAUGHT."
        )


def test_marker_duckdb_missing_marker_raises_stale():
    """No snapshot_runs rows → read_snapshot_dates raises StaleSnapshotError('missing')."""
    con = _fresh_marker_db(marker_days=[])
    with pytest.raises(StaleSnapshotError) as exc:
        read_snapshot_dates(
            "staging", list(ADP_KEYS),
            athena_client=None, workgroup="w", results_s3_path="s3://r/",
            _run_query=_athena_like(con),
        )
    assert "missing" in exc.value.reason


# ---------------------------------------------------------------------------
# W2: empty-marker skip pinned for all three ADP keys (parametrised)
# ---------------------------------------------------------------------------

import pytest as _pt


@_pt.mark.parametrize("empty_key", sorted(ADP_KEYS))
def test_run_service_empty_marker_select_not_called(monkeypatch, empty_key):
    """For each ADP key, row_count=0 → its select_* is NOT called.

    Mutation N3/N4 (remove PM or utilisation empty guard): the skipped select IS called
    → assertion fails → CAUGHT.
    """
    from agents.fi_fleet_health import selection as fh_sel
    from agents.fi_recall_warranty import selection as rw_sel

    _KEY_TO_SELECT = {
        "vehicle_fault_status_daily": "select_fault_rows",
        "pm_vehicle_status_daily":    "select_pm_rows",
        "vehicle_utilisation_daily":  "select_daily_usage",
    }

    called: list[str] = []

    def _track(name):
        def f(*_a, **_kw):
            called.append(name)
            return []
        return f

    for key, attr in _KEY_TO_SELECT.items():
        monkeypatch.setattr(fh_sel, attr, _track(attr))

    monkeypatch.setattr(fh_sel, "select_managed_vins",    lambda *_a, **_kw: [])
    monkeypatch.setattr(fh_sel, "select_agent2_findings", lambda *_a, **_kw: ({}, 0))
    monkeypatch.setattr(fh_sel, "build_vehicle_rows",     lambda **_kw: ([], {}))
    monkeypatch.setattr(rw_sel, "select_parts_stock",     lambda *_a, **_kw: [])

    empty_states = dict(_all_states(TODAY))
    empty_states[empty_key] = SnapshotState(date=TODAY, row_count=0, table_date=YESTERDAY)
    monkeypatch.setattr(runner, "_read_snapshot_dates", lambda *_a, **_kw: empty_states)

    records: list = []
    runner.run_service(
        "staging",
        athena_client=object(), workgroup="cvx-staging-analytics",
        results_s3_path="s3://results/",
        ddb_client=object(),
        findings_table="vsa-staging-avx-findings",
        actions_table="vsa-staging-avx-actions",
        groundings_table="cms-staging-storage-vehicle-groundings",
        vehicles_table="cms-staging-storage-vehicles",
        fleet_locations_table="cms-staging-storage-fleet-locations",
        avx_writer=_AvxWriter(), cloudwatch_client=_Cw(),
        run_record_writer=lambda **k: records.append(k),
        now=_NOW,
    )

    skipped_attr = _KEY_TO_SELECT[empty_key]
    assert skipped_attr not in called, (
        f"{empty_key} row_count=0 → {skipped_attr} must NOT be called. "
        f"Mutation: remove the _empty guard → {skipped_attr} appears in {called} → CAUGHT."
    )
    (rec,) = records
    assert empty_key in rec.get("empty_snapshot_keys", []), (
        f"{empty_key} must appear in empty_snapshot_keys. "
        "Mutation: not recording → assertion fails → CAUGHT."
    )


# ---------------------------------------------------------------------------
# W3: run_service refuses when managed_vehicles date differs from ADP keys
# ---------------------------------------------------------------------------


def test_run_service_mixed_marker_dates_aborts(monkeypatch):
    """managed_vehicles dated D-1 while ADP keys are dated D → StaleSnapshotError('mixed').

    Mutation N5 (same_date=False at run_service call): mixed dates pass the gate
    → no exception raised → pytest.raises block never entered → CAUGHT.
    """
    from agents.fi_fleet_health import selection as fh_sel
    from agents.fi_recall_warranty import selection as rw_sel

    called: list[str] = []

    def _track(name):
        def f(*_a, **_kw):
            called.append(name)
            return []
        return f

    monkeypatch.setattr(fh_sel, "select_fault_rows",      _track("fault"))
    monkeypatch.setattr(fh_sel, "select_pm_rows",         _track("pm"))
    monkeypatch.setattr(fh_sel, "select_managed_vins",    _track("managed"))
    monkeypatch.setattr(fh_sel, "select_daily_usage",     _track("usage"))
    monkeypatch.setattr(fh_sel, "select_agent2_findings", lambda *_a, **_kw: ({}, 0))
    monkeypatch.setattr(fh_sel, "build_vehicle_rows",     lambda **_kw: ([], {}))
    monkeypatch.setattr(rw_sel, "select_parts_stock",     lambda *_a, **_kw: [])

    # ADP keys: D; managed_vehicles: D-1 — one day behind.
    mixed = dict(_all_states(TODAY))
    mixed["managed_vehicles"] = SnapshotState(date=YESTERDAY, row_count=None, table_date=YESTERDAY)

    _NOW_SERVICE = _dt.datetime(2026, 10, 10, 5, 40, 0, tzinfo=_dt.timezone.utc)
    monkeypatch.setattr(runner, "_read_snapshot_dates", lambda *_a, **_kw: mixed)

    records: list = []
    with pytest.raises(StaleSnapshotError) as exc:
        runner.run_service(
            "staging",
            athena_client=object(), workgroup="cvx-staging-analytics",
            results_s3_path="s3://results/",
            ddb_client=object(),
            findings_table="vsa-staging-avx-findings",
            actions_table="vsa-staging-avx-actions",
            groundings_table="cms-staging-storage-vehicle-groundings",
            vehicles_table="cms-staging-storage-vehicles",
            fleet_locations_table="cms-staging-storage-fleet-locations",
            avx_writer=_AvxWriter(), cloudwatch_client=_Cw(),
            run_record_writer=lambda **k: records.append(k),
            now=_NOW_SERVICE,
        )

    assert "mixed" in exc.value.reason, (
        f"Expected 'mixed' in reason; got {exc.value.reason!r}. "
        "Mutation N5 (same_date=False): no exception raised → pytest.raises fails → CAUGHT."
    )
    assert not called, (
        f"No select_* must run before the mixed-date refusal; called: {called}. "
        "Mutation N5: readers run before gate fires → CAUGHT."
    )
    (rec,) = records
    assert rec["status"] == "aborted"
