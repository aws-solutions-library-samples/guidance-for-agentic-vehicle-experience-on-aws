"""Item 1 on a 7-day cycle: PASS only on a sound fallback record (issue 2026-10-22-battery-fallback-record).

U7 lets a component whose managed pool doesn't fit its nightly cap cover every managed vehicle
within 7 days, if the run says so. The runner writes that as the run record's
``managed_coverage`` (agents/tier2/selection.py ``_managed_coverage``). These tests pin each rule
``_score_7_day_fallback`` applies, one bad record per rule, plus the boundary of the script's own
``7 x cap >= pool`` check. Run-record numbers arrive as ``Decimal`` from the DynamoDB
deserialiser, as live.

Run (AWS env stripped):
    env -u AWS_PROFILE AWS_ACCESS_KEY_ID=testing AWS_SECRET_ACCESS_KEY=testing \\
      PYTHONDONTWRITEBYTECODE=1 \\
      .venv/bin/python -m pytest scripts/tests/test_live_verify_fi_fleet_health_item1_fallback.py -q
"""
from __future__ import annotations

import importlib.util
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

_SCRIPT = Path(__file__).resolve().parents[1] / "live-verify-fi-fleet-health.py"
_spec = importlib.util.spec_from_file_location("live_verify_fi_fleet_health_fb", _SCRIPT)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

Results = _mod.Results
_score_u7_component = _mod._score_u7_component

_CAP = 600
_POOL = 4042  # live battery managed pool, 2026-10-22
_MLV = "MRD1AAAAAAA111111"


def _coverage(**over: Any) -> dict[str, Any]:
    """The record selection writes for 4,042 managed vehicles at cap 600, as Decimals."""
    cov: dict[str, Any] = {
        "mode": "7_day_fallback",
        "cycle_nights": Decimal(7),
        "cap": Decimal(_CAP),
        "managed_pool": Decimal(_POOL),
        "nights_to_cover": Decimal(7),
        "cycle_covers": Decimal(_POOL),
        "slice_start": Decimal(1801),
        "slice_size": Decimal(_CAP),
    }
    cov.update(over)
    return cov


def _run_rec(coverage: Any = "default") -> dict[str, Any]:
    rec: dict[str, Any] = {
        "agent_component": "agent-2-vehicle-health#battery",
        "started_at": "2026-10-23T02:00:05Z",
        "finished_at": "2026-10-23T02:01:10Z",
        "status": "completed",
        "artifacts_written": Decimal(600),
        "duplicates_skipped": Decimal(0),
        "managed_select_errors": Decimal(0),
        "agent_version": "0.1.0",
    }
    if coverage == "default":
        coverage = _coverage()
    if coverage is not None:
        rec["managed_coverage"] = coverage
    return rec


def _rotating_cursor() -> dict[str, Any]:
    return {"pk": "CURSOR#battery", "sk": "STATE", "last_vin": None,
            "pool_exhausted_count": Decimal(0), "managed_last_vin": _MLV}


def _score(run_rec: dict[str, Any], *, pool_size: int = _POOL, cap: int = _CAP) -> Results:
    res = Results()
    try:
        _score_u7_component(res, "battery", cap, run_rec, _rotating_cursor(), pool_size)
    except Exception as exc:  # a crash is not a verdict
        raise AssertionError(f"scoring raised {type(exc).__name__}: {exc}") from exc
    return res


def _row(res: Results, label: str = "1-battery-pool-fitted") -> tuple[str, str]:
    rows = [(s, d) for lb, s, d in res.rows if lb == label]
    assert len(rows) == 1, res.rows
    return rows[0]


# ---------------------------------------------------------------------------
# Positive control
# ---------------------------------------------------------------------------

def test_a_sound_record_passes_and_says_it_is_the_7_day_fallback():
    res = _score(_run_rec())
    status, detail = _row(res)
    assert status == "PASS", res.rows
    assert not res.failed, res.failed
    assert detail.startswith("7-day fallback:"), detail
    assert "managed pool 4042" in detail and "cap 600" in detail, detail
    assert "7 nights cover 4042 of the record's 4042 (capacity 4200)" in detail, detail
    assert "this run's slice 1801-2400" in detail, detail


def test_a_slice_that_wraps_is_described_with_its_wrapped_end():
    status, detail = _row(_score(_run_rec(_coverage(slice_start=Decimal(4001)))))
    assert status == "PASS" and "this run's slice 4001-558" in detail, detail


@pytest.mark.parametrize("started_at", ["2026-10-23T15:05:00Z", "2026-10-23T02:00:05Z"])
def test_the_pass_row_names_the_slice_of_the_run_it_read_not_last_night(started_at):
    """The record describes the run item 1 read, which may be a run invoked by hand that day."""
    rec = _run_rec()
    rec.update(started_at=started_at)
    status, detail = _row(_score(rec))
    assert status == "PASS" and "this run's slice 1801-2400" in detail, detail
    assert "last night" not in detail, detail


def test_plain_ints_pass_too():
    cov = {k: (int(v) if isinstance(v, Decimal) else v) for k, v in _coverage().items()}
    assert _row(_score(_run_rec(cov)))[0] == "PASS"


# ---------------------------------------------------------------------------
# No record, or not a fallback record
# ---------------------------------------------------------------------------

def test_no_record_fails():
    status, detail = _row(_score(_run_rec(None)))
    assert status == "FAIL"
    assert "no 7-day-cycle fallback record" in detail and "managed_coverage absent" in detail


@pytest.mark.parametrize("bad", ["not-a-map", ["7_day_fallback"], 7])
def test_a_record_that_is_not_a_map_fails(bad):
    status, detail = _row(_score(_run_rec(bad)))
    assert status == "FAIL" and "managed_coverage absent" in detail


@pytest.mark.parametrize("mode", ["unmeasured", "nightly", None, "7_DAY_FALLBACK"])
def test_a_record_in_another_mode_fails(mode):
    cov = {"mode": mode, "cycle_nights": Decimal(7), "cap": Decimal(_CAP),
           "slice_size": Decimal(_CAP)}
    status, detail = _row(_score(_run_rec(cov)))
    assert status == "FAIL" and f"mode={mode!r}" in detail, detail


@pytest.mark.parametrize("field", ["cycle_nights", "cap", "managed_pool", "nights_to_cover",
                                   "cycle_covers", "slice_start", "slice_size"])
@pytest.mark.parametrize("bad", ["missing", "600", True, Decimal("600.5"), None])
def test_a_record_missing_a_field_or_carrying_a_non_integer_fails(field, bad):
    cov = _coverage()
    if bad == "missing":
        del cov[field]
    else:
        cov[field] = bad
    status, detail = _row(_score(_run_rec(cov)))
    assert status == "FAIL" and field in detail and "lacks" in detail, detail


# ---------------------------------------------------------------------------
# A record without full coverage
# ---------------------------------------------------------------------------

def test_a_record_whose_7_nights_dont_cover_its_pool_fails():
    """Pool 4,300 at cap 600: 7 nights cover 4,200. The record says so honestly; it fails."""
    cov = _coverage(managed_pool=Decimal(4300), nights_to_cover=Decimal(8),
                    cycle_covers=Decimal(4200))
    status, detail = _row(_score(_run_rec(cov), pool_size=4100))
    assert status == "FAIL" and "7 nights cover 4200 of the record's 4300" in detail, detail


@pytest.mark.parametrize("pool,ok", [(4199, True), (4200, True), (4201, False), (4300, False)])
def test_the_scripts_own_count_must_fit_seven_nights_of_the_live_cap(pool, ok):
    """The record is sound for 4,042; the script's own count decides at the 7 x 600 boundary."""
    status, detail = _row(_score(_run_rec(), pool_size=pool))
    assert (status == "PASS") is ok, detail
    if not ok:
        assert f"7 nights x cap 600 = 4200 < managed pool {pool}" in detail


def test_a_slice_smaller_than_the_cap_fails():
    status, detail = _row(_score(_run_rec(_coverage(slice_size=Decimal(599)))))
    assert status == "FAIL" and "slice_size=599 is not a full slice of 600" in detail


@pytest.mark.parametrize("start", [0, 4043, -1])
def test_a_slice_start_outside_the_pool_fails(start):
    status, detail = _row(_score(_run_rec(_coverage(slice_start=Decimal(start)))))
    assert status == "FAIL" and f"slice_start={start} outside 1..4042" in detail, detail


def test_a_record_for_a_pool_that_fits_its_cap_fails():
    cov = _coverage(managed_pool=Decimal(600), nights_to_cover=Decimal(1),
                    cycle_covers=Decimal(600), slice_start=Decimal(1))
    status, detail = _row(_score(_run_rec(cov)))
    assert status == "FAIL" and "record pool=600 is not larger than its cap=600" in detail


# ---------------------------------------------------------------------------
# Wrong cycle arithmetic
# ---------------------------------------------------------------------------

def test_a_cycle_other_than_seven_nights_fails():
    """An 8-night record with covers 4,042 (8 x 600) would pass on its own numbers; U7 is 7."""
    cov = _coverage(cycle_nights=Decimal(8))
    status, detail = _row(_score(_run_rec(cov)))
    assert status == "FAIL" and "cycle_nights=8 (U7 is 7)" in detail, detail


@pytest.mark.parametrize("nights", [6, 8, 1])
def test_wrong_nights_to_cover_fails(nights):
    status, detail = _row(_score(_run_rec(_coverage(nights_to_cover=Decimal(nights)))))
    assert status == "FAIL" and f"nights_to_cover={nights} (ceil(4042/600) is 7)" in detail


@pytest.mark.parametrize("covers", [4200, 4041, 4043])
def test_wrong_cycle_covers_fails(covers):
    status, detail = _row(_score(_run_rec(_coverage(cycle_covers=Decimal(covers)))))
    assert status == "FAIL" and f"cycle_covers={covers}" in detail, detail


def test_a_record_cap_other_than_the_live_cap_fails():
    """Record written at cap 700 (arithmetic consistent for it); the Lambda now runs 600."""
    cov = _coverage(cap=Decimal(700), slice_size=Decimal(700), nights_to_cover=Decimal(6))
    status, detail = _row(_score(_run_rec(cov)))
    assert status == "FAIL" and "record cap=700 != live cap 600" in detail, detail


# ---------------------------------------------------------------------------
# Unchanged paths stay unchanged
# ---------------------------------------------------------------------------

def test_a_pool_that_fits_still_passes_without_a_record():
    res = Results()
    cursor = {"pk": "CURSOR#tires", "sk": "STATE", "last_vin": None,
              "pool_exhausted_count": Decimal(0)}
    _score_u7_component(res, "tires", 156, _run_rec(None), cursor, 4)
    assert _row(res, "1-tires-pool-fitted")[0] == "PASS"


def test_an_unknown_pool_still_fails_even_with_a_record():
    res = Results()
    _score_u7_component(res, "battery", _CAP, _run_rec(), _rotating_cursor(), None)
    status, detail = _row(res)
    assert status == "FAIL" and "pool_size unknown" in detail


# ---------------------------------------------------------------------------
# Review cycle 1 W1: the script's own count decides, whatever the cursor says
# ---------------------------------------------------------------------------

def _no_managed_cursor() -> dict[str, Any]:
    """A cursor with no managed position: what a night whose wrap query failed leaves."""
    return {"pk": "CURSOR#battery", "sk": "STATE", "last_vin": None,
            "pool_exhausted_count": Decimal(0)}


def _score_cursor(cursor: Any, run_rec: dict[str, Any], pool_size: Any) -> Results:
    res = Results()
    try:
        _score_u7_component(res, "battery", _CAP, run_rec, cursor, pool_size)
    except Exception as exc:
        raise AssertionError(f"scoring raised {type(exc).__name__}: {exc}") from exc
    return res


@pytest.mark.parametrize("cursor", ["no_managed_position", "no_cursor_item"])
def test_a_pool_larger_than_the_cap_fails_without_a_record_whatever_the_cursor(cursor):
    cur = _no_managed_cursor() if cursor == "no_managed_position" else None
    status, detail = _row(_score_cursor(cur, _run_rec(None), _POOL))
    assert status == "FAIL", detail
    assert "managed pool (4042) > cap (600)" in detail and "managed_coverage absent" in detail


@pytest.mark.parametrize("cursor", ["no_managed_position", "no_cursor_item"])
def test_a_sound_record_passes_whatever_the_cursor(cursor):
    cur = _no_managed_cursor() if cursor == "no_managed_position" else None
    status, detail = _row(_score_cursor(cur, _run_rec(), _POOL))
    assert status == "PASS" and detail.startswith("7-day fallback:"), detail


@pytest.mark.parametrize("cursor", ["no_managed_position", "no_cursor_item"])
def test_an_unknown_pool_fails_whatever_the_cursor(cursor):
    cur = _no_managed_cursor() if cursor == "no_managed_position" else None
    status, detail = _row(_score_cursor(cur, _run_rec(None), None))
    assert status == "FAIL" and "pool_size unknown" in detail, detail


@pytest.mark.parametrize("pool", [599, 600])
def test_a_pool_that_fits_passes_with_no_managed_position(pool):
    status, detail = _row(_score_cursor(_no_managed_cursor(), _run_rec(None), pool))
    assert status == "PASS" and f"pool_size={pool} cap=600" in detail, detail


def test_the_pass_row_carries_no_failure_wording():
    status, detail = _row(_score(_run_rec()))
    low = detail.lower()
    assert status == "PASS"
    assert not any(w in low for w in ("fail", "error", "missing", "skipping", "not yet",
                                      "lacks", "unsound", "absent")), detail


# ---------------------------------------------------------------------------
# Note KK (2026-10-22): item 1 reads the newest battery run, scheduled or invoked by hand
# ---------------------------------------------------------------------------

def _query_ddb(*records: dict[str, Any]) -> Any:
    """Fake low-level DynamoDB whose Query answers newest first, honouring Limit."""
    from boto3.dynamodb.types import TypeSerializer

    ser = TypeSerializer()
    items = [{k: ser.serialize(v) for k, v in r.items() if v is not None} for r in records]

    class _Ddb:
        def query(self, **kw):
            assert kw.get("ScanIndexForward") is False, kw
            return {"Items": items[: kw.get("Limit", len(items))]}

    return _Ddb()


def _lookup(*records: dict[str, Any], now: str = "2026-10-23T16:00:00+00:00") -> Any:
    import datetime as dt
    from unittest import mock

    with mock.patch.object(_mod, "_now", lambda: dt.datetime.fromisoformat(now)):
        return _mod._latest_health_run(_query_ddb(*records), "runs",
                                       "agent-2-vehicle-health#battery")


def _rec(started_at: str, status: str = "completed") -> dict[str, Any]:
    rec = _run_rec()
    rec.update(started_at=started_at, finished_at=started_at, status=status)
    return rec


def test_a_run_invoked_by_hand_is_the_run_item_1_reads():
    """15:05Z by hand, after that night's 02:00Z run: the by-hand run is newest, and is read."""
    got = _lookup(_rec("2026-10-23T15:05:00Z"), _rec("2026-10-23T02:00:05Z"))
    assert got is not None and got["started_at"] == "2026-10-23T15:05:00Z", got


def test_the_scheduled_run_is_still_read_when_it_is_newest():
    got = _lookup(_rec("2026-10-23T02:00:05Z"), now="2026-10-23T09:00:00+00:00")
    assert got is not None and got["started_at"] == "2026-10-23T02:00:05Z", got


def test_a_newest_run_older_than_26_hours_is_not_read():
    assert _lookup(_rec("2026-10-22T13:59:00Z")) is None


def test_an_aborted_newest_run_is_not_replaced_by_an_older_completed_one():
    assert _lookup(_rec("2026-10-23T15:05:00Z", status="aborted"),
                   _rec("2026-10-23T02:00:05Z")) is None


@pytest.mark.parametrize("started_at,when", [
    ("2026-10-23T15:05:00Z", "outside the 02:00-03:30Z schedule window (invoked by hand)"),
    ("2026-10-23T02:00:05Z", "scheduled 02:00-03:30Z window"),
])
def test_the_run_row_says_whether_the_run_was_scheduled_or_by_hand(started_at, when):
    res = Results()
    _score_u7_component(res, "battery", _CAP, _rec(started_at), _rotating_cursor(), _POOL)
    status, detail = _row(res, "1-battery-run")
    assert status == "PASS" and detail == f"started_at={started_at} ({when})", detail
