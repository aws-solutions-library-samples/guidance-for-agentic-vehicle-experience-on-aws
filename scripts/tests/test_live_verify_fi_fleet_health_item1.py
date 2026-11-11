"""Tests for _item_1 (U7 nightly managed-VIN coverage) in live-verify-fi-fleet-health.py.

Each bad world fails on the item's own assertion; a positive control (fitted pools) passes.

Run (AWS env stripped):
    env -u AWS_PROFILE AWS_ACCESS_KEY_ID=testing AWS_SECRET_ACCESS_KEY=testing \
      PYTHONDONTWRITEBYTECODE=1 \
      .venv/bin/python -m pytest scripts/tests/test_live_verify_fi_fleet_health_item1.py -q
"""
from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

import pytest

# ---------------------------------------------------------------------------
# Load module
# ---------------------------------------------------------------------------

_SCRIPT = Path(__file__).resolve().parents[1] / "live-verify-fi-fleet-health.py"
_spec = importlib.util.spec_from_file_location("live_verify_fi_fleet_health", _SCRIPT)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

import datetime as dt

Results              = _mod.Results
_score_u7_component  = _mod._score_u7_component
_run_in_night_window = _mod._run_in_night_window
_latest_health_run   = _mod._latest_health_run
_item_1              = _mod._item_1


# ---------------------------------------------------------------------------
# Fake data builders (shapes match the real writers)
# ---------------------------------------------------------------------------

def _make_run_record(
    component: str,
    started_at: str = "2026-10-17T02:00:06Z",
    finished_at: str = "2026-10-17T02:00:49Z",
    status: str = "completed",
    artifacts_written: int = 375,
    duplicates_skipped: int = 0,
    managed_select_errors: int = 0,
) -> dict[str, Any]:
    """Minimal run record matching run_records.py:50-115 (pk=agent_component, sk=started_at).

    Live battery example from facts.md § 'Live run records'.
    """
    # agent_component format: contract.py:82-84 + run_records.py:188
    agent_id = "agent-2-vehicle-health"
    return {
        "agent_component": f"{agent_id}#{component}",
        "started_at": started_at,
        "finished_at": finished_at,
        "status": status,
        "artifacts_written": artifacts_written,
        "duplicates_skipped": duplicates_skipped,
        "managed_select_errors": managed_select_errors,
        "agent_version": "0.1.0",
    }


def _make_cursor_item(
    component: str,
    managed_last_vin: str | None = None,
    last_vin: str | None = None,
    pool_exhausted_count: int = 0,
) -> dict[str, Any]:
    """Cursor item matching tier2/selection.py:191-200 (_build_cursor_item).

    managed_last_vin=None  → pool fits (short page from cursor=None path).
    managed_last_vin='VIN' → rotation active (full page path).
    """
    # _build_cursor_item omits managed_last_vin entirely when it is None:
    # selection.py:197: if managed_last_vin is not None: item["managed_last_vin"] = managed_last_vin
    item: dict[str, Any] = {
        "pk":                   f"CURSOR#{component}",
        "sk":                   "STATE",
        "last_vin":             last_vin,
        "pool_exhausted_count": pool_exhausted_count,
        "component":            component,
    }
    if managed_last_vin is not None:
        item["managed_last_vin"] = managed_last_vin
    return item


# ---------------------------------------------------------------------------
# Helper to call _score_u7_component and collect results
# ---------------------------------------------------------------------------

def _run_score(
    component: str = "battery",
    cap: int = 375,
    run_rec: Any = "default",
    cursor_item: Any = "default",
    pool_size: int | None = 300,
) -> Results:
    if run_rec == "default":
        run_rec = _make_run_record(component)
    if cursor_item == "default":
        cursor_item = _make_cursor_item(component)  # managed_last_vin=None → fitted
    res = Results()
    _score_u7_component(res, component, cap, run_rec, cursor_item, pool_size)
    return res


# ===========================================================================
# Night window helper
# ===========================================================================

def test_night_window_accepted():
    """02:00Z and 03:29Z are inside the window."""
    assert _run_in_night_window("2026-10-17T02:00:06Z")
    assert _run_in_night_window("2026-10-17T03:29:00Z")


def test_night_window_rejected():
    """01:59Z and 03:31Z are outside."""
    assert not _run_in_night_window("2026-10-17T01:59:59Z")
    assert not _run_in_night_window("2026-10-17T03:31:00Z")


# ===========================================================================
# Positive control: fitted pool
# ===========================================================================

def test_positive_control_fitted_pool_passes():
    """Positive control: pool_size<cap, managed_select_errors=0, cursor None → all PASS."""
    res = _run_score(pool_size=300, cap=375)
    assert not res.failed, f"unexpected fail(s): {res.failed}"
    statuses = {r[0]: r[1] for r in res.rows}
    assert statuses.get("1-battery-run") == "PASS"
    assert statuses.get("1-battery-select-errors") == "PASS"
    assert statuses.get("1-battery-pool-fitted") == "PASS"


# ===========================================================================
# Bad world: no run record last night
# ===========================================================================

def test_no_run_record_fails():
    """No completed run in the last 26 h → 1-{component}-run FAIL."""
    res = _run_score(run_rec=None)
    fails = {r[0] for r in res.failed}
    assert "1-battery-run" in fails, f"rows={res.rows}"


# ===========================================================================
# Bad world: failed run record (status='aborted')
# ===========================================================================

def test_aborted_run_record_fails():
    """_latest_health_run returns None when the newest record is aborted.

    Fake DDB: newest=aborted (in window), older=completed (last week, in window
    time-wise but outside the 26 h bound when _now is mocked to Oct-17 04:00).
    Without the 'completed' filter (mutant M3 removes it), the aborted record
    is returned and the assertion fails → mutant caught.

    Shapes from run_records.py:50-115.
    """
    import unittest.mock as mock  # noqa: PLC0415
    from boto3.dynamodb.types import TypeSerializer  # noqa: PLC0415

    ser = TypeSerializer()

    def _serialize(d: dict) -> dict:
        return {k: ser.serialize(v) for k, v in d.items() if v is not None}

    aborted       = _make_run_record("battery", status="aborted",
                                     started_at="2026-10-17T02:00:06Z")
    old_completed = _make_run_record("battery", status="completed",
                                     started_at="2026-10-10T02:05:00Z")   # 7 days earlier

    fake_ddb = mock.MagicMock()
    # query returns newest first (ScanIndexForward=False)
    fake_ddb.query.return_value = {
        "Items": [_serialize(aborted), _serialize(old_completed)],
        "Count": 2,
    }

    _mock_now = lambda: dt.datetime(2026, 10, 17, 4, 0, 0,
                                    tzinfo=dt.timezone.utc)  # 26 h cutoff = Oct-16 02:00
    with mock.patch.object(_mod, "_now", _mock_now):
        result = _latest_health_run(fake_ddb, "fake-table",
                                   "agent-2-vehicle-health#battery")
    assert result is None, (
        f"expected None (aborted newest + old completed both rejected); got {result}"
    )


# ===========================================================================
# Bad world: managed_select_errors > 0
# ===========================================================================

def test_select_errors_nonzero_fails():
    """managed_select_errors=1 → 1-{component}-select-errors FAIL."""
    rec = _make_run_record("tires", managed_select_errors=1,
                            started_at="2026-10-17T02:20:20Z")
    res = _run_score(component="tires", cap=156, run_rec=rec, pool_size=100)
    fails = {r[0] for r in res.failed}
    assert "1-tires-select-errors" in fails, f"rows={res.rows}"
    # Confirm PASS rows carry no failure wording (brief rule)
    for label, status, detail in res.rows:
        if status == "PASS":
            assert "FAIL" not in detail and "error" not in detail.lower()


# ===========================================================================
# Bad world: rotating cursor with pool > cap (no 7-day fallback)
# ===========================================================================

def test_rotating_cursor_pool_over_cap_fails():
    """managed_last_vin non-None and pool_size>cap → 1-{component}-pool-fitted FAIL."""
    cursor = _make_cursor_item("brakes", managed_last_vin="VIN1234567890ABCDE")
    res = _run_score(
        component="brakes", cap=2000,
        run_rec=_make_run_record("brakes", started_at="2026-10-17T02:40:30Z"),
        cursor_item=cursor,
        pool_size=2500,  # pool > cap
    )
    fails = {r[0] for r in res.failed}
    assert "1-brakes-pool-fitted" in fails, f"rows={res.rows}"
    # FAIL detail must mention fallback absence
    detail = next(d for l, s, d in res.rows if l == "1-brakes-pool-fitted" and s == "FAIL")
    assert "no 7-day-cycle fallback" in detail


# ===========================================================================
# Edge: pool == cap, cursor non-None → all covered (PASS)
# ===========================================================================

def test_pool_equals_cap_with_nonnone_cursor_passes():
    """pool_size==cap and managed_last_vin non-None: wrap rotation, all VINs covered → PASS."""
    cursor = _make_cursor_item("battery", managed_last_vin="VIN1234567890ABCDE")
    res = _run_score(component="battery", cap=375, cursor_item=cursor, pool_size=375)
    fails = {r[0] for r in res.failed}
    assert "1-battery-pool-fitted" not in fails, f"rows={res.rows}"
    statuses = {r[0]: r[1] for r in res.rows}
    assert statuses.get("1-battery-pool-fitted") == "PASS"


# ===========================================================================
# _item_1 integration: injects fake DDB+Athena, all components PASS
# ===========================================================================

def test_caps_read_from_lambda_env():
    """Caps come from Lambda GetFunctionConfiguration, not code defaults.

    Lambda returns TIER2_MAX_VINS_BATTERY=600 (6.3's raised value); the
    pool-fitted detail must say cap=600, not cap=375 (runner.py:152 default).
    Before fix: detail says cap=375 → assertion fails.
    """
    import unittest.mock as mock  # noqa: PLC0415
    from boto3.dynamodb.types import TypeSerializer  # noqa: PLC0415

    ser = TypeSerializer()

    def _s(d: dict) -> dict:
        return {k: ser.serialize(v) for k, v in d.items() if v is not None}

    def _fake_client(service, region, *, allowed_ops, **_kw):
        m = mock.MagicMock()
        if service == "lambda":
            m.get_function_configuration.return_value = {
                "Environment": {"Variables": {
                    "TIER2_MAX_VINS_BATTERY": "600",
                    "TIER2_MAX_VINS_TIRES":   "156",
                    "TIER2_MAX_VINS_BRAKES":  "2000",
                }}
            }
        elif service == "dynamodb":
            def _fq(**kw):
                comp = kw["ExpressionAttributeValues"][":pk"]["S"].split("#")[1]
                return {"Items": [_s(_make_run_record(comp))], "Count": 1}
            def _fg(**kw):
                comp = kw["Key"]["pk"]["S"].replace("CURSOR#", "")
                return {"Item": _s(_make_cursor_item(comp))}
            m.query.side_effect   = _fq
            m.get_item.side_effect = _fg
        elif service == "athena":
            m.start_query_execution.return_value = {"QueryExecutionId": "q"}
            m.get_query_execution.return_value = {
                "QueryExecution": {"Status": {"State": "SUCCEEDED"}, "QueryExecutionId": "q"}
            }
            m.get_query_results.return_value = {
                "ResultSet": {"Rows": [
                    {"Data": [{"VarCharValue": "_col"}]},
                    # 150 fits every component's cap (600/156/2000). The earlier 300 sat above
                    # tires' 156 with no rotation, which item 1 now FAILs (issue
                    # 2026-10-22-battery-fallback-record, review cycle 1 W1).
                    {"Data": [{"VarCharValue": "150"}]},
                ]}
            }
        return m

    _mock_now = lambda: dt.datetime(2026, 10, 17, 4, 0, 0, tzinfo=dt.timezone.utc)
    res = Results()
    with mock.patch.object(_mod, "_client", side_effect=_fake_client), \
         mock.patch.object(_mod, "_now", _mock_now):
        _item_1(res, stage="staging", region="us-west-2")

    assert not res.failed, f"unexpected fail: {res.failed}"
    detail = next((r[2] for r in res.rows if r[0] == "1-battery-pool-fitted"), None)
    assert detail is not None, "no 1-battery-pool-fitted row found"
    assert "cap=600" in detail, (
        f"expected cap=600 (from Lambda env) in detail; "
        f"caps must not be hardcoded defaults — got: {detail!r}"
    )


def test_battery_cap_below_600_fails():
    """battery cap from Lambda env < 600 → 1-battery-cap-raised FAIL.

    Lambda returns TIER2_MAX_VINS_BATTERY=375 (pre-raise default).
    Before fix: no cap-raise check exists → row absent → assertion fails.
    After fix: 375 < 600 → 1-battery-cap-raised FAIL.
    """
    import unittest.mock as mock  # noqa: PLC0415
    from boto3.dynamodb.types import TypeSerializer  # noqa: PLC0415

    ser = TypeSerializer()

    def _s(d: dict) -> dict:
        return {k: ser.serialize(v) for k, v in d.items() if v is not None}

    def _fake_client(service, region, *, allowed_ops, **_kw):
        m = mock.MagicMock()
        if service == "lambda":
            m.get_function_configuration.return_value = {
                "Environment": {"Variables": {
                    "TIER2_MAX_VINS_BATTERY": "375",  # NOT raised
                    "TIER2_MAX_VINS_TIRES":   "156",
                    "TIER2_MAX_VINS_BRAKES":  "2000",
                }}
            }
        elif service == "dynamodb":
            def _fq(**kw):
                comp = kw["ExpressionAttributeValues"][":pk"]["S"].split("#")[1]
                return {"Items": [_s(_make_run_record(comp))], "Count": 1}
            def _fg(**kw):
                comp = kw["Key"]["pk"]["S"].replace("CURSOR#", "")
                return {"Item": _s(_make_cursor_item(comp))}
            m.query.side_effect    = _fq
            m.get_item.side_effect = _fg
        elif service == "athena":
            m.start_query_execution.return_value = {"QueryExecutionId": "q"}
            m.get_query_execution.return_value = {"QueryExecution": {
                "Status": {"State": "SUCCEEDED"}, "QueryExecutionId": "q",
            }}
            m.get_query_results.return_value = {"ResultSet": {"Rows": [
                {"Data": [{"VarCharValue": "_col"}]},
                {"Data": [{"VarCharValue": "100"}]},
            ]}}
        return m

    _mock_now = lambda: dt.datetime(2026, 10, 17, 4, 0, 0, tzinfo=dt.timezone.utc)
    res = Results()
    with mock.patch.object(_mod, "_client", side_effect=_fake_client), \
         mock.patch.object(_mod, "_now", _mock_now):
        _item_1(res, stage="staging", region="us-west-2")

    fails = {r[0] for r in res.failed}
    assert "1-battery-cap-raised" in fails, (
        f"expected 1-battery-cap-raised FAIL (cap=375 < 600); rows={res.rows}"
    )


def test_battery_run_over_budget_fails():
    """battery run finished_at − started_at >= 600 s → 1-battery-wall-budget FAIL.

    Battery run takes 660 s; budget is 600 s (tasks.md 6.3 carried from 4.3).
    Before fix: no wall-budget check → row absent → assertion fails.
    """
    import unittest.mock as mock  # noqa: PLC0415
    from boto3.dynamodb.types import TypeSerializer  # noqa: PLC0415

    ser = TypeSerializer()

    def _s(d: dict) -> dict:
        return {k: ser.serialize(v) for k, v in d.items() if v is not None}

    def _fake_client(service, region, *, allowed_ops, **_kw):
        m = mock.MagicMock()
        if service == "lambda":
            m.get_function_configuration.return_value = {
                "Environment": {"Variables": {
                    "TIER2_MAX_VINS_BATTERY": "600",
                    "TIER2_MAX_VINS_TIRES":   "156",
                    "TIER2_MAX_VINS_BRAKES":  "2000",
                }}
            }
        elif service == "dynamodb":
            def _fq(**kw):
                comp = kw["ExpressionAttributeValues"][":pk"]["S"].split("#")[1]
                rec = _make_run_record(
                    comp,
                    started_at="2026-10-17T02:00:00Z",
                    finished_at="2026-10-17T02:11:00Z",   # 660 s
                ) if comp == "battery" else _make_run_record(comp)
                return {"Items": [_s(rec)], "Count": 1}
            def _fg(**kw):
                comp = kw["Key"]["pk"]["S"].replace("CURSOR#", "")
                return {"Item": _s(_make_cursor_item(comp))}
            m.query.side_effect    = _fq
            m.get_item.side_effect = _fg
        elif service == "athena":
            m.start_query_execution.return_value = {"QueryExecutionId": "q"}
            m.get_query_execution.return_value = {"QueryExecution": {
                "Status": {"State": "SUCCEEDED"}, "QueryExecutionId": "q",
            }}
            m.get_query_results.return_value = {"ResultSet": {"Rows": [
                {"Data": [{"VarCharValue": "_col"}]},
                {"Data": [{"VarCharValue": "100"}]},
            ]}}
        return m

    _mock_now = lambda: dt.datetime(2026, 10, 17, 4, 0, 0, tzinfo=dt.timezone.utc)
    res = Results()
    with mock.patch.object(_mod, "_client", side_effect=_fake_client), \
         mock.patch.object(_mod, "_now", _mock_now):
        _item_1(res, stage="staging", region="us-west-2")

    fails = {r[0] for r in res.failed}
    assert "1-battery-wall-budget" in fails, (
        f"expected 1-battery-wall-budget FAIL (660 s > 600 s); rows={res.rows}"
    )


def test_caps_read_failure_fails_item():
    """GetFunctionConfiguration failure → 1-caps-read FAIL, item aborts."""
    import unittest.mock as mock  # noqa: PLC0415

    def _fail_lam_client(service, region, *, allowed_ops, **_kw):
        m = mock.MagicMock()
        if service == "lambda":
            m.get_function_configuration.side_effect = RuntimeError("AccessDenied")
        return m

    _mock_now = lambda: dt.datetime(2026, 10, 17, 4, 0, 0, tzinfo=dt.timezone.utc)
    res = Results()
    with mock.patch.object(_mod, "_client", side_effect=_fail_lam_client), \
         mock.patch.object(_mod, "_now", _mock_now):
        _item_1(res, stage="staging", region="us-west-2")

    fails = {r[0] for r in res.failed}
    assert "1-caps-read" in fails, (
        f"expected 1-caps-read FAIL when GetFunctionConfiguration raises; rows={res.rows}"
    )


def test_item1_uses_per_component_pool_size():
    """pool_size in each component's detail uses the source's own pool predicate, not the
    whole managed_vehicles table.

    Athena mock dispatches by SQL content: energy_usage → 600, tire_health → 100,
    service_records → 50.  Before fix: one whole-table query (managed_vehicles only,
    no component join) gets the fallback "300" → battery detail shows pool_size=300,
    not pool_size=600; assertion fails.
    After fix: three per-component queries; each detail shows the correct pool_size.
    """
    import unittest.mock as mock  # noqa: PLC0415
    from boto3.dynamodb.types import TypeSerializer  # noqa: PLC0415

    ser = TypeSerializer()

    def _s(d: dict) -> dict:
        return {k: ser.serialize(v) for k, v in d.items() if v is not None}

    _query_sql: list[tuple[str, str]] = []

    def _fake_client(service, region, *, allowed_ops, **_kw):
        m = mock.MagicMock()
        if service == "lambda":
            m.get_function_configuration.return_value = {
                "Environment": {"Variables": {
                    "TIER2_MAX_VINS_BATTERY": "700",
                    "TIER2_MAX_VINS_TIRES":   "200",
                    "TIER2_MAX_VINS_BRAKES":  "2000",
                }}
            }
        elif service == "dynamodb":
            def _fq(**kw):
                comp = kw["ExpressionAttributeValues"][":pk"]["S"].split("#")[1]
                return {"Items": [_s(_make_run_record(comp))], "Count": 1}
            def _fg(**kw):
                comp = kw["Key"]["pk"]["S"].replace("CURSOR#", "")
                return {"Item": _s(_make_cursor_item(comp))}
            m.query.side_effect    = _fq
            m.get_item.side_effect = _fg
        elif service == "athena":
            ctr = [0]

            def _start(**kw):
                ctr[0] += 1
                eid = f"qid-{ctr[0]}"
                _query_sql.append((eid, kw.get("QueryString", "")))
                return {"QueryExecutionId": eid}

            def _status(**kw):
                return {"QueryExecution": {
                    "Status": {"State": "SUCCEEDED"},
                    "QueryExecutionId": kw["QueryExecutionId"],
                }}

            def _results(**kw):
                eid = kw["QueryExecutionId"]
                sql = next((q for (e, q) in _query_sql if e == eid), "")
                if "energy_usage" in sql:
                    val = "600"
                elif "tire_health" in sql:
                    val = "100"
                elif "service_records" in sql:
                    val = "50"
                else:
                    val = "300"   # whole-table or unexpected query
                return {"ResultSet": {"Rows": [
                    {"Data": [{"VarCharValue": "_col"}]},
                    {"Data": [{"VarCharValue": val}]},
                ]}}

            m.start_query_execution.side_effect  = _start
            m.get_query_execution.side_effect    = _status
            m.get_query_results.side_effect      = _results
        return m

    _mock_now = lambda: dt.datetime(2026, 10, 17, 4, 0, 0, tzinfo=dt.timezone.utc)
    res = Results()
    with mock.patch.object(_mod, "_client", side_effect=_fake_client), \
         mock.patch.object(_mod, "_now", _mock_now):
        _item_1(res, stage="staging", region="us-west-2")

    assert not res.failed, f"unexpected fail: {res.failed}"

    # each detail must name the component-specific pool, not the whole-table count
    def _detail(label: str) -> str:
        row = next((r for r in res.rows if r[0] == label), None)
        assert row is not None, f"no row {label!r}; rows={res.rows}"
        return row[2]

    assert "pool_size=600" in _detail("1-battery-pool-fitted"), (
        "battery pool must use energy_usage join (adp_battery_source.py:300)"
    )
    assert "pool_size=100" in _detail("1-tires-pool-fitted"), (
        "tires pool must use tire_health join (adp_tire_source.py:299)"
    )
    assert "pool_size=50" in _detail("1-brakes-pool-fitted"), (
        "brakes pool must use service_records join (adp_brake_source.py:543)"
    )


def test_item1_all_components_pass_with_fakes():
    """_item_1 wires Lambda + DDB + Athena; all three components PASS."""
    import unittest.mock as mock  # noqa: PLC0415
    from boto3.dynamodb.types import TypeSerializer  # noqa: PLC0415

    ser = TypeSerializer()

    def _serialize(raw: dict) -> dict[str, Any]:
        return {k: ser.serialize(v) for k, v in raw.items() if v is not None}

    def _fake_client(service, region, *, allowed_ops, **_kw):
        m = mock.MagicMock()

        if service == "lambda":
            m.get_function_configuration.return_value = {
                "Environment": {"Variables": {
                    "TIER2_MAX_VINS_BATTERY": "600",  # raised cap per 6.3
                    "TIER2_MAX_VINS_TIRES":   "156",
                    "TIER2_MAX_VINS_BRAKES":  "2000",
                }}
            }

        elif service == "dynamodb":
            def _fake_query(**kw):
                comp = kw["ExpressionAttributeValues"][":pk"]["S"].split("#")[1]
                return {"Items": [_serialize(_make_run_record(comp))], "Count": 1}

            def _fake_get_item(**kw):
                comp = kw["Key"]["pk"]["S"].replace("CURSOR#", "")
                return {"Item": _serialize(_make_cursor_item(comp))}

            m.query.side_effect    = _fake_query
            m.get_item.side_effect = _fake_get_item

        elif service == "athena":
            m.start_query_execution.return_value = {"QueryExecutionId": "qid-fake"}
            m.get_query_execution.return_value = {
                "QueryExecution": {"Status": {"State": "SUCCEEDED"}, "QueryExecutionId": "qid-fake"}
            }
            m.get_query_results.return_value = {
                "ResultSet": {"Rows": [
                    {"Data": [{"VarCharValue": "_col0"}]},
                    # 150 fits every component's cap (600/156/2000). The earlier 300 sat above
                    # tires' 156 with no rotation, which item 1 now FAILs (issue
                    # 2026-10-22-battery-fallback-record, review cycle 1 W1).
                    {"Data": [{"VarCharValue": "150"}]},
                ]}
            }

        return m

    _mock_now = lambda: dt.datetime(2026, 10, 17, 4, 0, 0, tzinfo=dt.timezone.utc)
    res = Results()
    with mock.patch.object(_mod, "_client", side_effect=_fake_client), \
         mock.patch.object(_mod, "_now", _mock_now):
        _item_1(res, stage="staging", region="us-west-2")

    assert not res.failed, f"unexpected fail(s): {res.failed}"
    pass_labels = {r[0] for r in res.rows if r[1] == "PASS"}
    for comp in ("battery", "tires", "brakes"):
        assert f"1-{comp}-run" in pass_labels
        assert f"1-{comp}-select-errors" in pass_labels
        assert f"1-{comp}-pool-fitted" in pass_labels