"""The tires read is bounded to a 60-day window of ``event_date`` (prompt 361).

Issue ``issues/2026-11-09-tire-read-no-date-bound/`` (backlog row ``Vehicle page tire
data``). ADP's daily tire job (ADP spec ``2026-10-22-adp-tire-daily``) adds four rows per
car per day to ``tire_health``; the per-VIN read had no date filter, so it grew by 4 rows
per VIN a day while the tier2 stack sized it as 156 VINs x 60 days x 4 tires under a
50,000-row ceiling that raises ``AthenaResultTooLargeError`` when crossed.

Pinned here:
1. Every ``_build_sql`` shape (explicit VINs, ``max_vins`` subquery, unbounded) carries
   ``AND event_date >= DATE '<today - 59 days>'``: 60 dates, today (UTC) included.
2. The start follows the clock (no hardcoded date), and the literal is a date, not text.
3. The window, times the stack's tires VIN cap and 4 tires, fits the stack's row ceiling
   (read from ``vsa-tier2-stack.ts``, so either side changing alone fails here).
The live check (real Athena, partition pruning) is in the issue folder.

Mutants (``issues/2026-11-09-tire-read-no-date-bound/mutants.md``): clause removed; clause
only on the explicit-VIN branch; ``>`` for ``>=``; window 61 or 59; a hardcoded date.
"""

from __future__ import annotations

import datetime as dt
import pathlib
import re

import pytest

import agents.health.adp_tire_source as tire_mod
from agents.health.adp_tire_source import AdpTireSource, TIRE_READ_WINDOW_DAYS

_REPO = pathlib.Path(__file__).resolve().parents[3]
_STACK = _REPO / "infrastructure" / "lib" / "vsa-tier2-stack.ts"


def _source(**kw) -> AdpTireSource:
    return AdpTireSource("staging", athena_client=None, workgroup="wg",
                         results_s3_path="s3://b/p/", **kw)


def _shapes() -> dict[str, str]:
    return {
        "explicit_vins": _source(max_vins=10)._build_sql(vins=["VIN-001", "VIN-002"]),
        "max_vins": _source(max_vins=25)._build_sql(vins=None),
        "unbounded": _source(max_vins=None)._build_sql(vins=None),
    }


@pytest.fixture()
def today(monkeypatch):
    def pin(day: dt.date) -> None:
        monkeypatch.setattr(tire_mod, "_utc_today", lambda: day)
    return pin


def test_the_window_is_sixty_dates():
    assert TIRE_READ_WINDOW_DAYS == 60


@pytest.mark.parametrize("shape", ["explicit_vins", "max_vins", "unbounded"])
def test_every_sql_shape_is_bounded_to_the_window(today, shape):
    today(dt.date(2026, 11, 9))
    sql = _shapes()[shape]
    # 2026-11-09 minus 59 days: 60 event dates, 09-11 .. 11-09.
    assert "AND event_date >= DATE '2026-09-11'" in sql
    assert sql.count("event_date >=") == 1


def test_the_start_follows_the_clock(today):
    today(dt.date(2027, 1, 15))
    assert "AND event_date >= DATE '2026-11-17'" in _shapes()["unbounded"]
    today(dt.date(2026, 3, 1))
    assert "AND event_date >= DATE '2026-01-01'" in _shapes()["unbounded"]


def test_the_bound_sits_in_the_outer_where_not_the_vin_subquery(today):
    today(dt.date(2026, 11, 9))
    sql = _shapes()["max_vins"]
    outer, _, subquery = sql.partition("AND vin IN (")
    assert "event_date >=" in outer
    assert "event_date" not in subquery


def test_the_utc_clock_is_the_default():
    assert tire_mod._utc_today() == dt.datetime.now(dt.timezone.utc).date()


def test_the_window_fits_the_stacks_row_ceiling():
    src = _STACK.read_text()
    vins = int(re.search(r"TIER2_MAX_VINS_TIRES:\s*'(\d+)'", src).group(1))
    ceiling = int(re.search(r"TIER2_MAX_ROWS_PER_QUERY:\s*'(\d+)'", src).group(1))
    assert (vins, ceiling) == (156, 50000), "re-check the sizing note in vsa-tier2-stack.ts"
    assert vins * TIRE_READ_WINDOW_DAYS * 4 <= ceiling
