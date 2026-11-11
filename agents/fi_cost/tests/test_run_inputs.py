"""The run loop's two extra reads (task 4.1): the data month and the sell-timing inputs.

* ``selection.find_latest_complete_month``: the newest ``fleet_cost_events`` month up to
  the calendar month before the run; ``None`` when nothing is published.
* ``sell_timing.fetch_sell_timing_inputs``: every managed vehicle's monthly maintenance
  and purchase price up to the data month, paged to the end.

Both SQL files also run under DuckDB against tables shaped like the ADP objects, so the
filters are checked by behaviour, not by string.
"""
from __future__ import annotations

import datetime
from pathlib import Path
from typing import Any

import duckdb
import pytest

from agents.fi_cost import selection, sell_timing

_SQL_DIR = Path(__file__).parent.parent / "sql"


class _Athena:
    """Records each query; answers with ``pages`` (header + rows, NextToken chained)."""

    def __init__(self, header: list[str], rows: list[list[str | None]], page: int = 2) -> None:
        self.sql: list[str] = []
        self.polls = 0
        self._pages = []
        chunks = [rows[i:i + page] for i in range(0, len(rows), page)] or [[]]
        for n, chunk in enumerate(chunks):
            data = [{"Data": [{"VarCharValue": v} if v is not None else {} for v in r]}
                    for r in chunk]
            if n == 0:
                data = [{"Data": [{"VarCharValue": h} for h in header]}] + data
            page_resp: dict[str, Any] = {"ResultSet": {"Rows": data}}
            if n + 1 < len(chunks):
                page_resp["NextToken"] = f"t{n + 1}"
            self._pages.append(page_resp)

    def start_query_execution(self, **kw):
        self.sql.append(kw["QueryString"])
        return {"QueryExecutionId": "q1"}

    def get_query_execution(self, **_kw):
        self.polls += 1
        return {"QueryExecution": {"Status": {"State": "SUCCEEDED"}}}

    def get_query_results(self, **kw):
        token = kw.get("NextToken")
        return self._pages[0 if token is None else int(token[1:])]


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    import agents.tier2.adp_source as adp_source

    monkeypatch.setattr(adp_source.time, "sleep", lambda _s: None)


NOW = datetime.datetime(2026, 10, 6, 4, 0, tzinfo=datetime.timezone.utc)


# ---------------------------------------------------------------------------
# Data month
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("now, expected", [
    (datetime.datetime(2026, 10, 6, tzinfo=datetime.timezone.utc), "2026-09"),
    (datetime.datetime(2026, 1, 2, tzinfo=datetime.timezone.utc), "2025-12"),
    (datetime.datetime(2026, 3, 1, 0, 30, tzinfo=datetime.timezone(datetime.timedelta(hours=5))), "2026-01"),
])
def test_previous_calendar_month(now, expected):
    assert selection.previous_calendar_month(now) == expected


def test_latest_month_is_bounded_by_the_month_before_the_run():
    athena = _Athena(["year_month"], [["2026-09"]])
    assert selection.find_latest_complete_month(athena, "wg", "s3://r/", now=NOW,
                                                stage="staging", max_polls=7) == "2026-09"
    (sql,) = athena.sql
    assert "adp_staging_fleet_cost.fleet_cost_events" in sql
    assert "year_month <= '2026-09'" in sql


def test_no_published_month_is_none():
    athena = _Athena(["year_month"], [[None]])
    assert selection.find_latest_complete_month(athena, "wg", "s3://r/", now=NOW) is None


def test_a_malformed_month_is_refused_before_it_reaches_another_query():
    athena = _Athena(["year_month"], [["2026-9'"]])
    with pytest.raises(ValueError):
        selection.find_latest_complete_month(athena, "wg", "s3://r/", now=NOW)


def test_the_poll_ceiling_reaches_the_query():
    import agents.tier2.adp_source as adp_source

    class _Slow(_Athena):
        def get_query_execution(self, **_kw):
            self.polls += 1
            return {"QueryExecution": {"Status": {"State": "RUNNING"}}}

    athena = _Slow(["year_month"], [["2026-09"]])
    with pytest.raises(adp_source.AthenaTimeoutError):
        selection.find_latest_complete_month(athena, "wg", "s3://r/", now=NOW, max_polls=3)
    assert athena.polls == 3


@pytest.mark.parametrize("stage", ["dev", "staging; DROP", ""])
def test_latest_month_refuses_an_unknown_stage(stage):
    with pytest.raises(ValueError):
        selection.find_latest_complete_month(_Athena(["year_month"], []), "wg", "s3://r/",
                                             now=NOW, stage=stage)


# ---------------------------------------------------------------------------
# Sell-timing inputs
# ---------------------------------------------------------------------------

_HEADER = ["vin", "fleet_id", "year_month", "maintenance_cost", "purchase_price_usd"]


def test_sell_timing_inputs_follow_next_token_to_the_end():
    rows = [[f"MRDST{n:012d}", "f1", "2026-09", "10.0", "40000.0"] for n in range(5)]
    athena = _Athena(_HEADER, rows, page=2)
    got = sell_timing.fetch_sell_timing_inputs(athena, "wg", "s3://r/", year_month="2026-09")
    assert len(got) == 5
    (sql,) = athena.sql
    assert "adp_staging_fleet_cost.vehicle_cost_monthly" in sql
    assert "adp_staging_vehicle_pricing.vehicle_pricing" in sql
    assert "m.year_month <= '2026-09'" in sql


@pytest.mark.parametrize("kw", [{"year_month": "2026-13"}, {"year_month": "2026-09' OR 1=1"},
                                {"year_month": "2026-09", "stage": "dev"}])
def test_sell_timing_inputs_validate_before_templating(kw):
    athena = _Athena(_HEADER, [])
    with pytest.raises(ValueError):
        sell_timing.fetch_sell_timing_inputs(athena, "wg", "s3://r/", **kw)
    assert athena.sql == []


def test_sell_timing_inputs_raise_rather_than_truncate(monkeypatch):
    import agents.tier2.adp_source as adp_source

    monkeypatch.setattr(sell_timing, "SELL_TIMING_MAX_ROWS", 3)
    rows = [[f"MRDST{n:012d}", "f1", "2026-09", "10.0", "40000.0"] for n in range(5)]
    with pytest.raises(adp_source.AthenaResultTooLargeError):
        sell_timing.fetch_sell_timing_inputs(_Athena(_HEADER, rows), "wg", "s3://r/",
                                             year_month="2026-09")


def test_conflicting_prices_or_duplicate_months_drop_the_vehicle():
    def row(vin, month, cost, price):
        return {"vin": vin, "fleet_id": "f1", "year_month": month,
                "maintenance_cost": cost, "purchase_price_usd": price}

    a, b = "MRDST000000000001", "MRDST000000000002"
    rows = [row(a, "2026-08", "300", "36000"), row(a, "2026-09", "400", "37000"),
            row(b, "2026-09", "400", "36000"), row(b, "2026-09", "400", "36000")]
    candidates, rejected = sell_timing.plan_sell_timing(rows, year_month="2026-09")
    assert candidates == [] and rejected == 2


def test_a_malformed_vin_gets_no_sell_timing_and_is_counted():
    """The VIN becomes the Finding's scope and narration (security review cycle 1).

    The good series (270, 290) is still below 300 in the data month and crosses at k = 1,
    so it qualifies under option (b)."""
    good = "MRDST000000000003"
    rows = [{"vin": v, "fleet_id": "f1", "year_month": m, "maintenance_cost": c,
             "purchase_price_usd": "36000"}
            for v in (good, "MRD'; DROP x") for m, c in (("2026-08", "270"), ("2026-09", "290"))]
    candidates, rejected = sell_timing.plan_sell_timing(rows, year_month="2026-09")
    assert [c["vin"] for c in candidates] == [good] and rejected == 1


# ---------------------------------------------------------------------------
# Behaviour under DuckDB
# ---------------------------------------------------------------------------


def _duck() -> duckdb.DuckDBPyConnection:
    conn = duckdb.connect()
    for schema in ("adp_test_fleet_cost", "adp_test_vehicle_pricing"):
        conn.execute(f"CREATE SCHEMA {schema}")
    conn.execute("CREATE TABLE adp_test_fleet_cost.fleet_cost_events (vin VARCHAR, year_month VARCHAR)")
    conn.execute("INSERT INTO adp_test_fleet_cost.fleet_cost_events VALUES "
                 "('V1','2026-08'),('V1','2026-09'),('V1','2026-10')")
    conn.execute("""CREATE TABLE adp_test_fleet_cost.vehicle_cost_monthly (
        vin VARCHAR, year_month VARCHAR, fleet_id VARCHAR, model VARCHAR, model_year INTEGER,
        miles DOUBLE, energy_cost DOUBLE, maintenance_cost DOUBLE, lease_cost DOUBLE,
        insurance_cost DOUBLE, total_cost DOUBLE, cost_per_mile DOUBLE)""")
    conn.execute("""INSERT INTO adp_test_fleet_cost.vehicle_cost_monthly
        (vin, year_month, fleet_id, maintenance_cost) VALUES
        ('V1','2026-08','f1',100), ('V1','2026-09','f1',150), ('V1','2026-10','f1',999),
        ('V2','2026-09','f1',50)""")
    conn.execute("""CREATE TABLE adp_test_vehicle_pricing.vehicle_pricing (
        vin VARCHAR, purchase_price_usd DOUBLE, pricing_snapshot_date DATE,
        provenance VARCHAR, dimension_version VARCHAR)""")
    conn.execute("INSERT INTO adp_test_vehicle_pricing.vehicle_pricing (vin, purchase_price_usd) "
                 "VALUES ('V1', 36000)")
    return conn


def test_latest_month_sql_reads_the_newest_partition_up_to_the_bound():
    sql = ((_SQL_DIR / "latest_month.sql").read_text()
           .replace("${stage}", "test").replace("${through_month}", "2026-09"))
    assert _duck().execute(sql).fetchall() == [("2026-09",)]


def test_sell_timing_sql_stops_at_the_data_month_and_drops_unpriced_vehicles():
    sql = ((_SQL_DIR / "sell_timing_inputs.sql").read_text()
           .replace("${stage}", "test").replace("${year_month}", "2026-09"))
    got = sorted(_duck().execute(sql).fetchall())
    assert got == [("V1", "f1", "2026-08", 100.0, 36000.0), ("V1", "f1", "2026-09", 150.0, 36000.0)]
