"""The 7-day managed-coverage fallback record (stage D U7; issue 2026-10-22-battery-fallback-record).

U7: a component whose managed pool doesn't fit its nightly cap "falls back to every managed
vehicle within 7 days and says so in evidence". The evidence is the run record's
``managed_coverage`` map: written by selection when the managed page is full and a count shows the
pool is larger than the cap, forwarded by ``run()`` at every exit, stored by ``write_run_record``.

Run:
    .venv/bin/python -m pytest agents/tier2/tests/test_managed_fallback_record.py -q
"""
from __future__ import annotations

import math
from typing import Any, Optional

import duckdb
import pytest

# 17-character VINs in the VIN alphabet, ascending.
_VINS = [f"MRDFB{n:012d}" for n in range(1, 61)]


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _Source:
    """Managed + population source whose count answers from the same list it pages."""

    def __init__(self, managed: list[str], population: Optional[list[str]] = None, *,
                 count: Any = "list", select_raises: bool = False,
                 wrap_raises: bool = False) -> None:
        self.managed = sorted(managed)
        self.population = sorted(population or [])
        self._count = count
        self._select_raises = select_raises
        self._wrap_raises = wrap_raises
        self._managed_calls = 0
        self.count_calls: list[Optional[str]] = []

    def select_managed_vins(self, *, cursor, limit):
        self._managed_calls += 1
        if self._select_raises:
            raise RuntimeError("simulated managed query failure")
        if self._wrap_raises and self._managed_calls > 1 and cursor is None:
            raise RuntimeError("simulated wrap-around query failure")
        page = [v for v in self.managed if cursor is None or v > cursor][:limit]
        return page, (page[-1] if page else None)

    def select_vins(self, *, cursor, limit):
        page = [v for v in self.population if cursor is None or v > cursor][:limit]
        return page, (page[-1] if page else None)

    def count_managed_vins(self, *, after):
        self.count_calls.append(after)
        if self._count == "raise":
            raise RuntimeError("simulated count failure")
        if self._count != "list":
            return self._count
        pool = len(self.managed)
        at_or_before = 0 if after is None else sum(1 for v in self.managed if v <= after)
        return pool, at_or_before

    # FindingSource half, for run()
    def fetch_findings(self, vins):
        return [{"vin": v, "health_pct": 91.0, "age_days": 400} for v in vins]


class _NoCountSource(_Source):
    count_managed_vins = None  # type: ignore[assignment]


class _Cursor:
    def __init__(self, item: Optional[dict] = None) -> None:
        self.item = item
        self.puts: list[dict] = []

    def get_cursor(self, component):
        return self.item

    def put_cursor(self, component, item):
        self.puts.append(item)
        self.item = item


def _cursor_at(vin: Optional[str]) -> _Cursor:
    item = {"pk": "CURSOR#battery", "sk": "STATE", "last_vin": None, "pool_exhausted_count": 0}
    if vin is not None:
        item["managed_last_vin"] = vin
    return _Cursor(item)


def _resolve(source, *, limit: int, cursor: Optional[_Cursor] = None):
    from agents.tier2 import selection

    stats: dict[str, Any] = {}
    store = cursor or _Cursor()
    try:
        batch = selection.resolve_batch(source=source, cursor_store=store, component="battery",
                                        limit=limit, run_stats=stats)
    except Exception as exc:  # selection must never raise into the run
        raise AssertionError(f"resolve_batch raised {type(exc).__name__}: {exc}") from exc
    return batch, stats, store


def _expected(pool: int, cap: int, slice_start: int, slice_size: int) -> dict:
    return {
        "mode": "7_day_fallback",
        "cycle_nights": 7,
        "cap": cap,
        "managed_pool": pool,
        "nights_to_cover": math.ceil(pool / cap),
        "cycle_covers": min(pool, 7 * cap),
        "slice_start": slice_start,
        "slice_size": slice_size,
    }


# ---------------------------------------------------------------------------
# Selection writes the record
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("pool,cap", [(10, 3), (40, 7), (25, 3)])
def test_a_pool_larger_than_the_cap_records_the_7_day_fallback(pool, cap):
    """Exact record from the start of a rotation; (25, 3) is a cycle 7 nights can't cover."""
    src = _Source(_VINS[:pool])
    batch, stats, _ = _resolve(src, limit=cap)
    assert batch == _VINS[:cap]
    assert stats.get("managed_coverage") == _expected(pool, cap, 1, cap), stats
    assert src.count_calls == [None]


def test_the_cycle_constant_is_seven_nights():
    from agents.tier2 import selection

    assert selection.FALLBACK_CYCLE_NIGHTS == 7


def test_the_slice_starts_after_the_stored_cursor():
    """Cursor at the 6th managed VIN: this run's slice is managed vehicles 7..9."""
    src = _Source(_VINS[:10])
    batch, stats, _ = _resolve(src, limit=3, cursor=_cursor_at(_VINS[5]))
    assert batch == _VINS[6:9]
    assert stats.get("managed_coverage") == _expected(10, 3, 7, 3)
    assert src.count_calls == [_VINS[5]], "the count must be asked about the run's own cursor"


def test_a_slice_that_wraps_starts_at_its_first_vehicle():
    """Cursor at the 9th of 10: the slice is 10, 1, 2 (starts at 10)."""
    src = _Source(_VINS[:10])
    batch, stats, _ = _resolve(src, limit=3, cursor=_cursor_at(_VINS[8]))
    assert batch == [_VINS[9], _VINS[0], _VINS[1]]
    assert stats.get("managed_coverage") == _expected(10, 3, 10, 3)


def test_a_cursor_on_the_last_vehicle_starts_the_slice_at_one():
    src = _Source(_VINS[:10])
    batch, stats, _ = _resolve(src, limit=3, cursor=_cursor_at(_VINS[9]))
    assert batch == _VINS[:3]
    assert stats.get("managed_coverage") == _expected(10, 3, 1, 3)


def test_seven_consecutive_records_describe_seven_runs_that_cover_the_pool():
    """The records match what the runs really took: 7 x 6 >= 40, every vehicle seen."""
    src = _Source(_VINS[:40])
    store = _Cursor()
    seen: set[str] = set()
    starts = []
    for _ in range(7):
        batch, stats, store = _resolve(src, limit=6, cursor=store)
        cov = stats.get("managed_coverage")
        assert isinstance(cov, dict), stats
        # The slice the record names is the batch the run took.
        named = [src.managed[(cov["slice_start"] - 1 + i) % 40] for i in range(cov["slice_size"])]
        assert named == batch
        seen.update(batch)
        starts.append(cov["slice_start"])
    assert seen == set(_VINS[:40])
    assert starts == [1, 7, 13, 19, 25, 31, 37]


def test_a_pool_that_fits_writes_no_record_and_runs_no_count():
    src = _Source(_VINS[:2])
    _, stats, _ = _resolve(src, limit=3)
    assert "managed_coverage" not in stats
    assert src.count_calls == []


def test_a_pool_equal_to_the_cap_writes_no_record():
    """Full page, so the pool is counted; 3 <= 3 fits nightly."""
    src = _Source(_VINS[:3])
    _, stats, _ = _resolve(src, limit=3)
    assert "managed_coverage" not in stats
    assert src.count_calls == [None]


def test_a_failed_count_records_unmeasured_and_keeps_the_batch():
    src = _Source(_VINS[:10], count="raise")
    batch, stats, store = _resolve(src, limit=3)
    assert batch == _VINS[:3]
    assert stats.get("managed_coverage") == {"mode": "unmeasured", "cycle_nights": 7, "cap": 3,
                                         "slice_size": 3}
    assert store.puts[-1].get("managed_last_vin") == _VINS[2], "rotation must still advance"


@pytest.mark.parametrize("answer", [(2, 0), (10, 11), (-1, 0), ("10", 0), (10, None), None])
def test_an_inconsistent_count_records_unmeasured(answer):
    """A pool smaller than the page just taken, a position past the pool, or a malformed answer."""
    src = _Source(_VINS[:10], count=answer)
    _, stats, _ = _resolve(src, limit=3, cursor=_cursor_at(None))
    assert (stats.get("managed_coverage") or {}).get("mode") == "unmeasured", stats


def test_a_source_without_a_count_records_unmeasured_when_rotating():
    src = _NoCountSource(_VINS[:10])
    _, stats, _ = _resolve(src, limit=3)
    assert (stats.get("managed_coverage") or {}).get("mode") == "unmeasured", stats


def test_a_failed_managed_select_writes_no_coverage():
    src = _Source(_VINS[:10], population=_VINS[20:30], select_raises=True)
    _, stats, _ = _resolve(src, limit=3)
    assert stats.get("managed_select_errors") == 1
    assert "managed_coverage" not in stats
    assert src.count_calls == []


def test_a_failed_wrap_is_counted_and_recorded_unmeasured():
    """Review cycle 1 W2: cursor at the 9th of 10, the wrap query fails, the run takes only the
    10th. That night must not look like a pool that fits: an error is counted and the coverage
    is unmeasured with the short slice."""
    src = _Source(_VINS[:10], wrap_raises=True)
    batch, stats, store = _resolve(src, limit=3, cursor=_cursor_at(_VINS[8]))
    assert batch == [_VINS[9]]
    assert stats.get("managed_select_errors") == 1, stats
    assert stats.get("managed_coverage") == {"mode": "unmeasured", "cycle_nights": 7, "cap": 3,
                                             "slice_size": 1}, stats
    assert src.count_calls == []


def test_a_wrap_that_succeeds_counts_no_error():
    src = _Source(_VINS[:10])
    _, stats, _ = _resolve(src, limit=3, cursor=_cursor_at(_VINS[8]))
    assert stats.get("managed_select_errors", 0) == 0, stats
    assert (stats.get("managed_coverage") or {}).get("mode") == "7_day_fallback", stats


def test_run_carries_a_failed_wrap_to_the_run_record(monkeypatch):
    from agents.tier2 import runner

    monkeypatch.setattr(runner, "_MAX_VINS_BATTERY", 3)
    records: list[dict] = []
    try:
        runner.run("staging", source=_Source(_VINS[:10], wrap_raises=True), writer=_Writer(),
                   vins=None, component="battery", agent_fn=lambda vin, rows: _agent_fields(),
                   cursor_store=_cursor_at(_VINS[8]),
                   run_record_writer=lambda **kw: records.append(kw))
    except Exception as exc:
        raise AssertionError(f"run() raised {type(exc).__name__}: {exc}") from exc
    assert len(records) == 1, records
    assert records[0].get("managed_select_errors") == 1, records[0]
    assert (records[0].get("managed_coverage") or {}).get("mode") == "unmeasured", records[0]


# ---------------------------------------------------------------------------
# run() forwards it at every exit
# ---------------------------------------------------------------------------


def _agent_fields(tokens: int = 0) -> dict:
    out = {
        "finding": "Battery is healthy.",
        "severity_hint": "routine",
        "confidence": "high",
        "evidence": [{"source": "energy_usage", "value": {"soh": 91.0}, "provenance": "live"}],
    }
    if tokens:
        out["_tokens_used"] = tokens
    return out


class _Writer:
    def __init__(self) -> None:
        self.puts: list[dict] = []

    def put(self, artifact):
        self.puts.append(artifact)
        return True

    def put_raw(self, item):
        pass


class _EmptySource(_Source):
    def fetch_findings(self, vins):
        return []


class _NoVinRowsSource(_Source):
    def fetch_findings(self, vins):
        return [{"health_pct": 91.0} for _ in vins]


def _run(source, *, monkeypatch, max_tokens: int = 10_000_000, tokens: int = 0):
    from agents.tier2 import runner

    monkeypatch.setattr(runner, "_MAX_VINS_BATTERY", 3)
    records: list[dict] = []
    try:
        runner.run(
            "staging",
            source=source,
            writer=_Writer(),
            vins=None,
            component="battery",
            max_tokens=max_tokens,
            agent_fn=lambda vin, rows: _agent_fields(tokens),
            cursor_store=_Cursor(),
            run_record_writer=lambda **kw: records.append(kw),
        )
    except Exception as exc:
        raise AssertionError(f"run() raised {type(exc).__name__}: {exc}") from exc
    assert len(records) == 1, records
    return records[0]


@pytest.mark.parametrize("exit_kind", ["no_rows", "no_vin_rows", "completed", "budget"])
def test_run_forwards_the_record_at_every_exit(exit_kind, monkeypatch):
    managed = _VINS[:10]
    kw: dict[str, Any] = {}
    if exit_kind == "no_rows":
        src = _EmptySource(managed)
    elif exit_kind == "no_vin_rows":
        src = _NoVinRowsSource(managed)
    elif exit_kind == "completed":
        src = _Source(managed)
    else:
        src = _Source(managed)
        kw = {"max_tokens": 100, "tokens": 1_000}
    rec = _run(src, monkeypatch=monkeypatch, **kw)
    if exit_kind == "budget":
        assert rec["status"] == "aborted", rec
    assert rec.get("managed_coverage") == _expected(10, 3, 1, 3), rec


@pytest.mark.parametrize("exit_kind", ["no_rows", "completed"])
def test_a_pool_that_fits_leaves_the_run_record_unchanged(exit_kind, monkeypatch):
    src = _EmptySource(_VINS[:2]) if exit_kind == "no_rows" else _Source(_VINS[:2])
    rec = _run(src, monkeypatch=monkeypatch)
    assert "managed_coverage" not in rec, rec


# ---------------------------------------------------------------------------
# write_run_record stores it
# ---------------------------------------------------------------------------


class _Table:
    def __init__(self) -> None:
        self.items: list[dict] = []

    def put_item(self, Item, **kwargs):
        self.items.append(Item)


class _Resource:
    def __init__(self) -> None:
        self.table = _Table()

    def Table(self, name):
        return self.table


def _write(**extra):
    from agents.tier2.run_records import write_run_record

    res = _Resource()
    ok = write_run_record(
        agent_id="agent-2-vehicle-health", component="battery",
        started_at="2026-10-23T02:00:05Z", finished_at="2026-10-23T02:01:10Z",
        status="completed", artifacts_written=600, duplicates_skipped=0,
        agent_version="0.1.0", ddb_client=res, table_name="vsa-staging-avx-agent-runs",
        **extra,
    )
    return ok, res.table.items


def test_write_run_record_stores_the_fallback_map_exactly():
    cov = _expected(4042, 600, 1801, 600)
    ok, items = _write(managed_coverage=dict(cov))
    assert ok and len(items) == 1
    assert items[0]["managed_coverage"] == cov
    assert all(type(v) is int for k, v in items[0]["managed_coverage"].items() if k != "mode")


def test_write_run_record_stores_an_unmeasured_map():
    cov = {"mode": "unmeasured", "cycle_nights": 7, "cap": 600, "slice_size": 600}
    ok, items = _write(managed_coverage=dict(cov))
    assert ok and items[0]["managed_coverage"] == cov


def test_write_run_record_omits_the_map_when_absent():
    ok, items = _write()
    assert ok and "managed_coverage" not in items[0]


def test_write_run_record_drops_unknown_keys_and_refuses_a_bad_mode():
    ok, items = _write(managed_coverage={**_expected(10, 3, 1, 3), "vins": ["X"]})
    assert ok and "vins" not in items[0]["managed_coverage"]
    ok, items = _write(managed_coverage={**_expected(10, 3, 1, 3), "mode": "fits"})
    assert ok and items[0]["managed_coverage"]["mode"] == "unmeasured", items[0]


@pytest.mark.parametrize("field", ["cycle_nights", "cap", "managed_pool", "nights_to_cover",
                                   "cycle_covers", "slice_start", "slice_size"])
@pytest.mark.parametrize("bad", ["missing", 3.0, True, "3"])
def test_a_partial_fallback_map_is_stored_as_unmeasured(field, bad):
    cov = _expected(10, 3, 1, 3)
    if bad == "missing":
        del cov[field]
    else:
        cov[field] = bad
    ok, items = _write(managed_coverage=cov)
    stored = items[0]["managed_coverage"]
    assert ok and stored["mode"] == "unmeasured", stored
    assert "managed_pool" not in stored and "cycle_covers" not in stored, stored


def test_the_map_validates_against_the_real_dynamodb_model(caplog):
    import logging

    import boto3
    from botocore.stub import Stubber

    from agents.tier2.run_records import _compute_run_ttl, write_run_record  # noqa: PLC0415

    cov = _expected(4042, 600, 1801, 600)
    resource = boto3.resource("dynamodb", region_name="us-west-2")
    item = {
        "agent_component": "agent-2-vehicle-health#battery",
        "started_at": "2026-10-23T02:00:05Z", "finished_at": "2026-10-23T02:01:10Z",
        "status": "completed", "artifacts_written": 600, "duplicates_skipped": 0,
        "agent_version": "0.1.0", "managed_coverage": cov,
        # T1.5: TTL field added to every run record.
        "ttl": _compute_run_ttl("2026-10-23T02:01:10Z"),
    }
    with Stubber(resource.meta.client) as stub, caplog.at_level(logging.WARNING):
        stub.add_response("put_item", {},
                          {"TableName": "vsa-staging-avx-agent-runs",
                           "Item": item,
                           "ConditionExpression": "attribute_not_exists(agent_component)"})
        ok = write_run_record(
            agent_id="agent-2-vehicle-health", component="battery",
            started_at="2026-10-23T02:00:05Z", finished_at="2026-10-23T02:01:10Z",
            status="completed", artifacts_written=600, duplicates_skipped=0,
            agent_version="0.1.0", ddb_client=resource,
            table_name="vsa-staging-avx-agent-runs", managed_coverage=dict(cov))
        stub.assert_no_pending_responses()
    assert ok is True
    assert not [r for r in caplog.records if "failed to write" in r.message]


# ---------------------------------------------------------------------------
# Each source counts its own managed pool (DuckDB over the real SQL)
# ---------------------------------------------------------------------------

_STAGE = "staging"
_OLD, _NEW = "2026-10-21", "2026-10-22"


def _duck() -> duckdb.DuckDBPyConnection:
    """Managed pool per source (newest snapshot): battery 5, tires 4, brakes 3.

    Each table also carries rows outside the pool (an old-snapshot-only VIN, a VIN not managed,
    a tire not needing replacement, a closed brake job), so a count that drops a predicate is wrong.
    """
    con = duckdb.connect(":memory:")
    fr, eu = f"adp_{_STAGE}_fleet_recall", f"adp_{_STAGE}_energy_usage"
    th, sr = f"adp_{_STAGE}_tire_health", f"adp_{_STAGE}_service_records"
    for s in (fr, eu, th, sr):
        con.execute(f"CREATE SCHEMA {s}")
    v = _VINS
    con.execute(f"CREATE TABLE {fr}.managed_vehicles (vin VARCHAR, fleet_id VARCHAR, snapshot_date DATE)")
    for x in v[:6]:
        con.execute(f"INSERT INTO {fr}.managed_vehicles VALUES ('{x}','f','{_NEW}')")
    con.execute(f"INSERT INTO {fr}.managed_vehicles VALUES ('{v[9]}','f','{_OLD}')")
    con.execute(f"CREATE TABLE {eu}.energy_usage (vin VARCHAR, state_of_health_pct DOUBLE, "
                "battery_age_days INTEGER, usage_date DATE)")
    for x in v[:5] + [v[9], v[10]]:  # v[9] old-snapshot only, v[10] not managed
        for d in (1, 2):  # two rows per VIN: DISTINCT matters
            con.execute(f"INSERT INTO {eu}.energy_usage VALUES ('{x}',90.0,{d},'{_NEW}')")
    con.execute(f"CREATE TABLE {th}.tire_health (vin VARCHAR, tire_position VARCHAR, "
                "needs_replacement BOOLEAN)")
    for x in v[:4]:
        con.execute(f"INSERT INTO {th}.tire_health VALUES ('{x}','FL',true),('{x}','FR',true)")
    con.execute(f"INSERT INTO {th}.tire_health VALUES ('{v[4]}','FL',false),('{v[10]}','FL',true)")
    con.execute(f"CREATE TABLE {sr}.service_records (vin VARCHAR, service_type VARCHAR, "
                "outcome VARCHAR)")
    for x in v[:3]:
        con.execute(f"INSERT INTO {sr}.service_records VALUES ('{x}','brake_service','parts_pending')")
    con.execute(f"INSERT INTO {sr}.service_records VALUES ('{v[3]}','brake_service','completed'),"
                f"('{v[10]}','brake_service','parts_pending')")
    return con


def _source(name: str):
    from agents.health.adp_battery_source import AdpBatterySource
    from agents.health.adp_brake_source import AdpBrakeSource
    from agents.health.adp_tire_source import AdpTireSource

    cls = {"battery": AdpBatterySource, "tires": AdpTireSource, "brakes": AdpBrakeSource}[name]
    return cls(_STAGE, athena_client=object(), workgroup="wg", results_s3_path="s3://r/",
               poll_interval=0)


def _duck_run_query(con, seen: list[str]):
    def _rq(sql, **_kw):
        seen.append(sql)
        cur = con.execute(sql)
        cols = [d[0] for d in cur.description]
        return [{c: (None if val is None else str(val)) for c, val in zip(cols, row)}
                for row in cur.fetchall()]
    return _rq


@pytest.mark.parametrize("name,pool", [("battery", 5), ("tires", 4), ("brakes", 3)])
@pytest.mark.parametrize("after_idx,expected_before", [(None, 0), (1, 2), (2, 3)])
def test_each_source_counts_its_own_managed_pool(name, pool, after_idx, expected_before,
                                                 monkeypatch):
    import importlib

    mod = importlib.import_module({"battery": "agents.health.adp_battery_source",
                                   "tires": "agents.health.adp_tire_source",
                                   "brakes": "agents.health.adp_brake_source"}[name])
    seen: list[str] = []
    monkeypatch.setattr(mod, "run_query", _duck_run_query(_duck(), seen))
    after = None if after_idx is None else _VINS[after_idx]
    try:
        got = _source(name).count_managed_vins(after=after)
    except Exception as exc:
        raise AssertionError(f"count raised {type(exc).__name__}: {exc}") from exc
    assert got == (pool, expected_before)
    assert len(seen) == 1


@pytest.mark.parametrize("name", ["battery", "tires", "brakes"])
@pytest.mark.parametrize("source_polls", [60, 10])
def test_the_count_waits_less_than_the_runs_essential_queries(name, source_polls, monkeypatch):
    """Review cycle 1 S1: the count is optional; it may queue 30 s and poll at most 30 times."""
    import importlib

    mod = importlib.import_module({"battery": "agents.health.adp_battery_source",
                                   "tires": "agents.health.adp_tire_source",
                                   "brakes": "agents.health.adp_brake_source"}[name])
    seen: list[dict] = []
    monkeypatch.setattr(mod, "run_query", lambda sql, **kw: seen.append(kw) or
                        [{"managed_pool": "7", "at_or_before": "0"}])
    src = _source(name)
    src._max_polls = source_polls
    src.count_managed_vins(after=None)
    assert len(seen) == 1
    assert seen[0].get("max_queue_seconds") == 30.0, seen[0]
    assert seen[0].get("max_polls") == min(source_polls, 30), seen[0]


@pytest.mark.parametrize("name", ["battery", "tires", "brakes"])
def test_the_count_wraps_the_sources_own_managed_sql(name, monkeypatch):
    """Same pool predicate as the selection: the count embeds the selection's own SQL."""
    import importlib

    from agents.health import _managed_pool

    mod = importlib.import_module({"battery": "agents.health.adp_battery_source",
                                   "tires": "agents.health.adp_tire_source",
                                   "brakes": "agents.health.adp_brake_source"}[name])
    seen: list[str] = []
    monkeypatch.setattr(mod, "run_query", lambda sql, **kw: seen.append(sql) or
                        [{"managed_pool": "7", "at_or_before": "0"}])
    src = _source(name)
    src.count_managed_vins(after=None)
    inner = src._build_select_managed_vins_sql(cursor=None, limit=_managed_pool.COUNT_LIMIT)
    assert inner in seen[0]


def test_the_count_refuses_a_cursor_that_is_not_a_vin():
    from agents.health import _managed_pool

    with pytest.raises(ValueError, match="not a VIN"):
        _managed_pool.build_count_sql("SELECT DISTINCT vin FROM t", after="';DROP")


@pytest.mark.parametrize("rows", [[], [{"managed_pool": "x", "at_or_before": "0"}],
                                  [{"managed_pool": None, "at_or_before": "0"}],
                                  [{"managed_pool": "3"}],
                                  [{"managed_pool": "3", "at_or_before": "0"}] * 2])
def test_a_malformed_count_result_raises(rows):
    from agents.health import _managed_pool

    with pytest.raises(ValueError):
        _managed_pool.parse_count(rows)


def test_a_pool_at_the_count_limit_raises():
    from agents.health import _managed_pool

    with pytest.raises(ValueError, match="count limit"):
        _managed_pool.parse_count([{"managed_pool": str(_managed_pool.COUNT_LIMIT),
                                    "at_or_before": "0"}])
