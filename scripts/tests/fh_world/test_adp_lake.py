"""test_adp_lake.py — Acceptance test for S1.

Runs AVX's _FAULT_SQL, _PM_SQL, _MANAGED_VINS_SQL, _DAILY_USAGE_SQL and the
three Agent-2 sources' managed-VIN SQL through FakeAthena and asserts the
planted rows are returned.

AWS env is stripped; all I/O stays in DuckDB :memory:.
"""
from __future__ import annotations

import datetime
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

_AVX_ROOT = Path(__file__).resolve().parents[3]

# Strip AWS credentials so no real AWS call can succeed
for _k in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY",
           "AWS_SECURITY_TOKEN", "AWS_SESSION_TOKEN",
           "AWS_DEFAULT_REGION", "AWS_PROFILE"):
    os.environ.pop(_k, None)
os.environ.setdefault("AWS_ACCESS_KEY_ID", "test")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "test")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")

from scripts.tests.fi_rw_world.athena import FakeAthena
from scripts.tests.fi_rw_world.clock import WorldClock

from scripts.tests.fh_world.adp_lake import install_fleet_health_tables
from scripts.tests.fh_world.seed import (
    CODE_ATTN, CODE_P0,
    P0_DIRECTIVE,
    TODAY,
    VIN_ATTN_B, VIN_BATTERY_A, VIN_BRAKE_B,
    VIN_FLEET, VIN_P0_A,
    VIN_PM_DUE, VIN_PM_OVERDUE,
    VIN_TIRE_A,
    PM_CODE_BRAKE, PM_CODE_TIRE,
)

WORKGROUP = "cvx-staging-analytics"

_router: Any | None = None
_wired_client: Any | None = None


def _get_wired_client(athena: FakeAthena) -> Any:
    """Return a boto3 Athena client wired to FakeAthena via Router (created once)."""
    global _router, _wired_client
    if _wired_client is None:
        import boto3
        from scripts.tests.fi_rw_world.awsfake import Router
        _router = Router()
        athena.register(_router)
        _router.install()
        _wired_client = boto3.client(
            "athena", region_name="us-east-1",
            aws_access_key_id="test", aws_secret_access_key="test",
        )
    return _wired_client


@pytest.fixture(scope="module")
def athena() -> Iterator[FakeAthena]:
    clock = WorldClock(datetime.datetime(
        TODAY.year, TODAY.month, TODAY.day, 5, 40, 0,
        tzinfo=datetime.timezone.utc,
    ))
    a = FakeAthena(clock, {WORKGROUP, "dms-staging-fi-candidates"})
    install_fleet_health_tables(a, today=TODAY)
    # The Agent-2 sources check the managed_vehicles snapshot against "today" read
    # from the clock (max age 1 day). The lake is dated TODAY (fixed 2026-10-19), so
    # without the world clock the three managed-VIN tests failed once the real UTC
    # date passed 2026-10-20 (found 2026-10-21 in the a0302c3f full suite).
    clock.install([str(_AVX_ROOT)])
    try:
        yield a
    finally:
        clock.uninstall()


def _run(athena: FakeAthena, sql: str) -> list[dict]:
    """Run sql through FakeAthena via a Router-wired boto3 client."""
    from agents.tier2.adp_source import run_query
    return run_query(
        sql,
        athena_client=_get_wired_client(athena),
        workgroup=WORKGROUP,
        results_s3_path="s3://test-results/",
    )


# ---------------------------------------------------------------------------
# _FAULT_SQL
# ---------------------------------------------------------------------------

def test_fault_sql_returns_p0_vin(athena: FakeAthena) -> None:
    from agents.fi_fleet_health.selection import _FAULT_SQL
    sql = _FAULT_SQL.replace("${stage}", "staging")
    rows = _run(athena, sql)
    vins = {r["vin"] for r in rows}
    assert VIN_P0_A in vins, f"Expected {VIN_P0_A} in fault rows; got {vins}"


def test_fault_sql_returns_attention_vin(athena: FakeAthena) -> None:
    from agents.fi_fleet_health.selection import _FAULT_SQL
    sql = _FAULT_SQL.replace("${stage}", "staging")
    rows = _run(athena, sql)
    vins = {r["vin"] for r in rows}
    assert VIN_ATTN_B in vins, f"Expected {VIN_ATTN_B} in fault rows; got {vins}"


def test_fault_sql_p0_carries_directive(athena: FakeAthena) -> None:
    from agents.fi_fleet_health.selection import _FAULT_SQL
    sql = _FAULT_SQL.replace("${stage}", "staging")
    rows = _run(athena, sql)
    p0_rows = [r for r in rows if r["vin"] == VIN_P0_A]
    assert p0_rows, "No p0 fault rows returned"
    assert p0_rows[0]["directive_text"] == P0_DIRECTIVE, (
        f"directive_text mismatch: {p0_rows[0]['directive_text']!r}"
    )


def test_fault_sql_band_column(athena: FakeAthena) -> None:
    from agents.fi_fleet_health.selection import _FAULT_SQL
    sql = _FAULT_SQL.replace("${stage}", "staging")
    rows = _run(athena, sql)
    p0_rows = [r for r in rows if r["vin"] == VIN_P0_A]
    assert p0_rows[0]["band"] == "p0"


# ---------------------------------------------------------------------------
# _PM_SQL
# ---------------------------------------------------------------------------

def test_pm_sql_returns_overdue_vin(athena: FakeAthena) -> None:
    from agents.fi_fleet_health.selection import _PM_SQL
    sql = _PM_SQL.replace("${stage}", "staging")
    rows = _run(athena, sql)
    vins = {r["vin"] for r in rows}
    assert VIN_PM_OVERDUE in vins, f"Expected overdue VIN in PM rows; got {vins}"


def test_pm_sql_returns_due_vin(athena: FakeAthena) -> None:
    from agents.fi_fleet_health.selection import _PM_SQL
    sql = _PM_SQL.replace("${stage}", "staging")
    rows = _run(athena, sql)
    vins = {r["vin"] for r in rows}
    assert VIN_PM_DUE in vins, f"Expected due VIN in PM rows; got {vins}"


def test_pm_sql_has_task_code(athena: FakeAthena) -> None:
    from agents.fi_fleet_health.selection import _PM_SQL
    sql = _PM_SQL.replace("${stage}", "staging")
    rows = _run(athena, sql)
    codes = {r["task_code"] for r in rows}
    assert PM_CODE_TIRE in codes or PM_CODE_BRAKE in codes, (
        f"No expected PM task codes in {codes}"
    )


# ---------------------------------------------------------------------------
# _MANAGED_VINS_SQL
# ---------------------------------------------------------------------------

def test_managed_vins_sql_returns_all_managed(athena: FakeAthena) -> None:
    from agents.fi_fleet_health.selection import _MANAGED_VINS_SQL
    sql = _MANAGED_VINS_SQL.replace("${stage}", "staging")
    rows = _run(athena, sql)
    returned = {r["vin"] for r in rows}
    expected = set(VIN_FLEET.keys())
    assert expected == returned, (
        f"Managed VINs mismatch.\nMissing: {expected - returned}\n"
        f"Extra:   {returned - expected}"
    )


# ---------------------------------------------------------------------------
# _DAILY_USAGE_SQL
# ---------------------------------------------------------------------------

def test_daily_usage_sql_returns_rows(athena: FakeAthena) -> None:
    from agents.fi_fleet_health.selection import _DAILY_USAGE_SQL
    sql = _DAILY_USAGE_SQL.replace("${stage}", "staging")
    rows = _run(athena, sql)
    vins = {r["vin"] for r in rows}
    assert vins, "Expected at least one utilisation row"
    assert VIN_P0_A in vins or VIN_BATTERY_A in vins


# ---------------------------------------------------------------------------
# Agent-2 battery managed-VIN SQL
# ---------------------------------------------------------------------------

def test_battery_managed_vins_returns_battery_vin(athena: FakeAthena) -> None:
    from agents.health.adp_battery_source import AdpBatterySource
    client = _get_wired_client(athena)
    src = AdpBatterySource(stage="staging", max_vins=100, athena_client=client,
                           workgroup=WORKGROUP, results_s3_path="s3://test/")
    vins, _next = src.select_managed_vins(cursor=None, limit=100)
    assert VIN_BATTERY_A in vins, f"Expected {VIN_BATTERY_A} in battery pool; got {vins}"


# ---------------------------------------------------------------------------
# Agent-2 tire managed-VIN SQL
# ---------------------------------------------------------------------------

def test_tire_managed_vins_returns_tire_vin(athena: FakeAthena) -> None:
    from agents.health.adp_tire_source import AdpTireSource
    client = _get_wired_client(athena)
    src = AdpTireSource(stage="staging", max_vins=100, athena_client=client,
                        workgroup=WORKGROUP, results_s3_path="s3://test/")
    vins, _next = src.select_managed_vins(cursor=None, limit=100)
    assert VIN_TIRE_A in vins, f"Expected {VIN_TIRE_A} in tire pool; got {vins}"


# ---------------------------------------------------------------------------
# Agent-2 brake managed-VIN SQL
# ---------------------------------------------------------------------------

def test_brake_managed_vins_returns_brake_vin(athena: FakeAthena) -> None:
    from agents.health.adp_brake_source import AdpBrakeSource
    client = _get_wired_client(athena)
    src = AdpBrakeSource(stage="staging", max_vins=100, athena_client=client,
                         workgroup=WORKGROUP, results_s3_path="s3://test/")
    vins, _next = src.select_managed_vins(cursor=None, limit=100)
    assert VIN_BRAKE_B in vins, f"Expected {VIN_BRAKE_B} in brake pool; got {vins}"


# ---------------------------------------------------------------------------
# Config parquet bytes carry the planted p0 directive
# ---------------------------------------------------------------------------

def test_config_parquet_fault_codes_has_p0_directive(athena: FakeAthena) -> None:
    from scripts.tests.fh_world.adp_lake import config_parquet_bytes, _adp_root
    from io import BytesIO
    import pandas as pd

    blobs = config_parquet_bytes(_adp_root(), TODAY)
    key = "config/fleet_health/fault_codes/fault_codes.parquet"
    assert key in blobs, f"Missing key {key!r}"
    df = pd.read_parquet(BytesIO(blobs[key]))
    p0_rows = df[df["code"] == CODE_P0]
    assert not p0_rows.empty, f"Code {CODE_P0} not in fault_codes parquet"
    directive = p0_rows.iloc[0]["directive_text"]
    assert directive == P0_DIRECTIVE, (
        f"directive_text mismatch:\n  got: {directive!r}\n  want: {P0_DIRECTIVE!r}"
    )
