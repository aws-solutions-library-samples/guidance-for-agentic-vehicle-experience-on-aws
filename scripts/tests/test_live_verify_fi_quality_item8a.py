"""Item 8a of scripts/live-verify-fi-quality.py: re-running the patterns job adds nothing.

The fake job behaves like ADP ``engineering/jobs/fault_patterns.py`` ``lambda_handler`` as
deployed: it takes ``{"run_date": ...}``, re-puts every pattern item with a new
``computed_at``, overwrites ``fault_patterns.parquet`` and ``pattern_runs/<run_date>.json``,
and returns the handler's return shape (the 2026-10-16 live run's payload). Items and the
parquet are the live 2026-10-16 rows; schedules are their live ``get_schedule`` shapes.
"""
from __future__ import annotations

import copy
import datetime as dt
import importlib.util
import io
import json
from pathlib import Path
from typing import Any, Callable

import duckdb
import pytest
from botocore.exceptions import ClientError

_HERE = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("live_verify_fi_quality_8a", _HERE.parent / "live-verify-fi-quality.py")
lv = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(lv)

_DATA = _HERE / "fi_quality_data"
_ITEMS = json.loads((_DATA / "fault_patterns_items_2026-10-16.json").read_text())["items"]
_PARQUET = (_DATA / "fault_patterns_2026-10-16.parquet").read_bytes()
_ACCOUNT = "123456789012"
_BUCKET = f"adp-staging-foundation-lake-{_ACCOUNT}-us-east-1"
_SCHEDULES = {  # live list_schedules / get_schedule (2026-10-17): own groups, UTC
    "adp-staging-foundation-fault-daily-schedule": "cron(0 0 * * ? *)",
    "adp-staging-foundation-fault-patterns-schedule": "cron(0 1 * * ? *)",
}
#: The group each schedule is in (ADP fleet_health_stack.py:700, 710; live 2026-10-17). The
#: default group holds neither, so a read without the group is ResourceNotFound, as live.
_GROUPS = {
    "adp-staging-foundation-fault-daily-schedule": "adp-staging-foundation-fault-daily",
    "adp-staging-foundation-fault-patterns-schedule": "adp-staging-foundation-fault-patterns",
}
_T0 = dt.datetime(2026, 10, 16, 17, 58, 21, tzinfo=dt.timezone.utc)


def _restamp(raw: bytes) -> bytes:
    """The same rows with a new computed_at, as a re-run writes them."""
    con = duckdb.connect()
    src = io.BytesIO(raw)
    import tempfile, os  # noqa: E401
    with tempfile.TemporaryDirectory() as td:
        a, b = os.path.join(td, "a.parquet"), os.path.join(td, "b.parquet")
        open(a, "wb").write(src.read())
        con.execute(f"COPY (SELECT * REPLACE (TIMESTAMP '2026-10-16 23:00:00' AS computed_at) "
                    f"FROM read_parquet('{a}')) TO '{b}' (FORMAT PARQUET)")
        return open(b, "rb").read()


class _World:
    def __init__(self) -> None:
        self.items = {i["pattern_id"]: copy.deepcopy(i) for i in _ITEMS}
        self.objects: dict[str, tuple[bytes, dt.datetime]] = {
            "curated/engineering/fault_patterns/fault_patterns.parquet": (_PARQUET, _T0),
            "curated/engineering/pattern_runs/2026-10-16.json": (b'{"run_date": "2026-10-16"}', _T0),
        }
        self.schedules = dict(_SCHEDULES)
        self.tz = "UTC"
        self.schedule_error = False
        self.invokes: list[dict] = []
        self.lambda_configs: list[Any] = []
        self.job: Callable[["_World", dict], tuple[dict | None, str | None]] = _good_job
        self.pre_job: dict[str, dict] | None = None  # the items as they were before the invoke

    # boto3 seams
    def client(self, service: str, **kw: Any) -> Any:
        w = self
        if service == "lambda":
            self.lambda_configs.append(kw.get("config"))

        class C:
            def get_caller_identity(self) -> dict:
                return {"Account": _ACCOUNT}

            def get_schedule(self, Name: str, GroupName: str = "default") -> dict:
                if w.schedule_error:
                    raise ClientError({"Error": {"Code": "AccessDeniedException", "Message": "x"}}, "GetSchedule")
                if _GROUPS.get(Name) != GroupName:
                    raise ClientError({"Error": {"Code": "ResourceNotFoundException",
                                                 "Message": f"Schedule {Name} does not exist."}}, "GetSchedule")
                return {"Name": Name, "GroupName": GroupName, "State": "ENABLED",
                        "ScheduleExpression": w.schedules[Name], "ScheduleExpressionTimezone": w.tz}

            def get_object(self, Bucket: str, Key: str) -> dict:
                assert Bucket == _BUCKET
                body, mtime = w.objects[Key]
                return {"Body": io.BytesIO(body), "LastModified": mtime}

            def invoke(self, **kw: Any) -> dict:
                w.invokes.append(kw)
                w.pre_job = copy.deepcopy(w.items)
                payload, err = w.job(w, json.loads(kw["Payload"]))
                out: dict[str, Any] = {"StatusCode": 200, "Payload": io.BytesIO(json.dumps(payload).encode())}
                if err:
                    out["FunctionError"] = err
                return out
        return C()

    def resource(self, service: str, **_: Any) -> Any:
        w = self

        class T:
            def scan(self, **kw: Any) -> dict:
                # Eventually consistent: after the invoke, a scan without ConsistentRead
                # sees the items as they were before the job wrote (review cycle 2 W3).
                src = w.items if (kw.get("ConsistentRead") or w.pre_job is None) else w.pre_job
                rows = sorted(src.values(), key=lambda r: r["pattern_id"])
                start = int(kw.get("ExclusiveStartKey", {}).get("i", 0))
                out: dict[str, Any] = {"Items": copy.deepcopy(rows[start:start + 50])}
                if start + 50 < len(rows):
                    out["LastEvaluatedKey"] = {"i": start + 50}
                return out

        class R:
            def Table(self, name: str) -> T:
                assert name == "adp-staging-fault-patterns"
                return T()
        return R()


def _good_job(w: _World, event: dict) -> tuple[dict | None, str | None]:
    rd = event["run_date"]
    for it in w.items.values():
        it["computed_at"] = "2026-10-16T23:00:00.000000"
    later = _T0 + dt.timedelta(hours=5)
    key = "curated/engineering/fault_patterns/fault_patterns.parquet"
    w.objects[key] = (_restamp(w.objects[key][0]), later)
    w.objects[f"curated/engineering/pattern_runs/{rd}.json"] = (b'{"run_date": "%s"}' % rd.encode(), later)
    return ({"status": "ok", "run_date": rd, "pattern_count": len(w.items), "items_written": len(w.items),
             "run_record_key": f"curated/engineering/pattern_runs/{rd}.json"}, None)


@pytest.fixture()
def w(monkeypatch: pytest.MonkeyPatch) -> _World:
    import boto3
    world = _World()
    monkeypatch.setattr(boto3, "client", world.client)
    monkeypatch.setattr(boto3, "resource", world.resource)
    monkeypatch.setattr(lv, "_utc_now", lambda: dt.datetime(2026, 10, 16, 21, 30, tzinfo=dt.timezone.utc))
    return world


def _run(w: _World) -> dict[str, str]:
    res = lv.Results()
    lv._run_items(["8a"], res, allow_writes=True, stage="staging", answer_key=None)
    return {label: status for label, status, _ in res.rows}


def test_k1_identical_rerun_passes(w: _World) -> None:
    st = _run(w)
    assert set(st.values()) == {"PASS"}, st
    assert [json.loads(i["Payload"]) for i in w.invokes] == [{"run_date": "2026-10-16"}]
    assert w.invokes[0]["InvocationType"] == "RequestResponse"
    assert w.invokes[0]["FunctionName"] == "adp-staging-foundation-fault-patterns-job"


def test_k2_rerun_adding_an_item_fails(w: _World) -> None:
    def job(world: _World, ev: dict) -> Any:
        out = _good_job(world, ev)
        extra = copy.deepcopy(next(iter(world.items.values())))
        extra["pattern_id"] = "ffffffffffffffff"
        world.items[extra["pattern_id"]] = extra
        return out
    w.job = job
    assert _run(w)["item-8a-same-ids"] == "FAIL"


def test_k3_rerun_changing_a_field_fails(w: _World) -> None:
    def job(world: _World, ev: dict) -> Any:
        out = _good_job(world, ev)
        next(iter(world.items.values()))["trend"] = "rising-again"
        return out
    w.job = job
    st = _run(w)
    assert st["item-8a-items-unchanged"] == "FAIL"
    assert st["item-8a-same-ids"] == "PASS"


def test_k4_parquet_row_change_fails(w: _World) -> None:
    def job(world: _World, ev: dict) -> Any:
        out = _good_job(world, ev)
        key = "curated/engineering/fault_patterns/fault_patterns.parquet"
        import tempfile, os  # noqa: E401
        with tempfile.TemporaryDirectory() as td:
            a, b = os.path.join(td, "a.parquet"), os.path.join(td, "b.parquet")
            open(a, "wb").write(world.objects[key][0])
            duckdb.connect().execute(f"COPY (SELECT * REPLACE (rate_ratio * 2 AS rate_ratio) FROM read_parquet('{a}')) "
                                     f"TO '{b}' (FORMAT PARQUET)")
            world.objects[key] = (open(b, "rb").read(), world.objects[key][1])
        return out
    w.job = job
    assert _run(w)["item-8a-parquet-unchanged"] == "FAIL"


def test_k5_job_that_writes_nothing_is_vacuous(w: _World) -> None:
    w.job = lambda world, ev: ({"status": "ok", "run_date": ev["run_date"]}, None)
    assert _run(w)["item-8a-job-wrote"] == "FAIL"


@pytest.mark.parametrize("payload,err", [
    ({"errorMessage": "boom", "errorType": "RuntimeError"}, "Unhandled"),
    ({"status": "error", "run_date": "2026-10-16"}, None),
    ({"status": "ok", "run_date": "2026-10-17"}, None),
])
def test_k6_bad_invoke_results_fail(w: _World, payload: dict, err: str | None) -> None:
    w.job = lambda world, ev: (payload, err)
    st = _run(w)
    assert st["item-8a-rerun-ok"] == "FAIL"
    assert "item-8a-same-ids" not in st


@pytest.mark.parametrize("hh,mm,skip", [
    (23, 30, True),   # 30 min before the 00:00 daily run (wraps midnight)
    (0, 44, True),    # 44 min after daily, 16 before patterns
    (1, 44, True),    # 44 min after patterns
    (1, 45, False),   # 45 min after patterns, 105 after daily
    (23, 14, False),  # 46 min before daily
])
def test_k7_schedule_guard(w: _World, monkeypatch: pytest.MonkeyPatch, hh: int, mm: int, skip: bool) -> None:
    monkeypatch.setattr(lv, "_utc_now", lambda: dt.datetime(2026, 10, 16, hh, mm, tzinfo=dt.timezone.utc))
    st = _run(w)
    assert (st == {"item-8a": "SKIP"}) is skip, st
    assert bool(w.invokes) is not skip


def test_k7b_non_utc_schedule_refuses(w: _World) -> None:
    w.tz = "America/New_York"
    assert _run(w) == {"item-8a": "SKIP"}
    assert not w.invokes


@pytest.mark.parametrize("expr", ["rate(1 day)", "cron(0/5 * * * ? *)", "cron(0 1 ? * MON *)"])
def test_k7c_unsupported_expression_fails_closed(w: _World, expr: str) -> None:
    w.schedules["adp-staging-foundation-fault-patterns-schedule"] = expr
    st = _run(w)
    # C4 fix: _schedule_guard now catches ValueError and returns a reason string,
    # so item-8a gets a SKIP (write window refused) rather than an exception FAIL.
    # Both SKIP and FAIL are "fail closed" — the Lambda was never invoked.
    assert (st.get("item-8a-exception") == "FAIL"
            or st.get("item-8a") == "SKIP"), (
        f"unsupported expression {expr!r} must be refused (SKIP or FAIL); got {st}"
    )
    assert not w.invokes


def test_k7d_schedule_read_error_fails_closed(w: _World) -> None:
    w.schedule_error = True
    assert _run(w).get("item-8a-exception") == "FAIL"
    assert not w.invokes


def test_k8_mixed_run_dates_refuse_before_invoking(w: _World) -> None:
    next(iter(w.items.values()))["run_date"] = "2026-10-15"
    st = _run(w)
    assert st["item-8a-one-run-date"] == "FAIL"
    assert not w.invokes


def test_k9_invoke_never_retries_and_outlasts_the_job(w: _World) -> None:
    _run(w)
    cfg = [c for c in w.lambda_configs if c is not None][0]
    assert cfg.read_timeout > 900  # the job's timeout
    assert cfg.retries == {"total_max_attempts": 1}


def test_k10_without_allow_writes_nothing_is_called(w: _World) -> None:
    res = lv.Results()
    lv._run_items(["8a"], res, allow_writes=False, stage="staging", answer_key=None)
    assert [s for _, s, _ in res.rows] == ["SKIP"]
    assert not w.invokes
