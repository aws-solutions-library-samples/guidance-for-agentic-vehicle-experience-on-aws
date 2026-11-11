"""Write-window gates in both live scripts, driven through main() (review g78 B C1, C W4).

Two properties, for every write part of both scripts:

  WW1 With the window shut, a write part creates no temp user and sends no write
      (no non-GET request) and exits 2 with a SKIP. Mutant caught: any part's
      start-of-part `require_write_window` call removed.
  WW2 On the happy path, every AWS-changing request has its own window check:
      the part makes exactly one check more than it makes writes (the start check).
      Mutant caught: any one per-write check removed (approve, RO POST, RO PATCH,
      return to service, investigation POST and repeat POST, decision, issue).

Counting is done on the script's own `require_write_window` and `Http.call`
(the class every script request goes through), so it can't be satisfied by a
check elsewhere.
"""
from __future__ import annotations

import contextlib
import tempfile
from typing import Any, Iterator

import pytest

from scripts.tests import test_live_verify_fi_cms_fleet_f2 as f2
from scripts.tests import test_live_verify_fi_cms_maker_m2 as m2
from scripts.tests.fi_cms_world.world import CmsWorld

_WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


@contextlib.contextmanager
def _record(mod: Any, *, refuse: bool) -> Iterator[dict[str, Any]]:
    """Count window checks, writes and temp users made by *mod* (one script module)."""
    rec: dict[str, Any] = {"events": [], "principals": 0}
    orig_rww = mod.require_write_window
    orig_principal = mod.principal
    http_cls = mod.Http
    orig_call = http_cls.call

    def _rww(*a: Any, **kw: Any) -> Any:
        rec["events"].append("check")
        return "window shut (test)" if refuse else orig_rww(*a, **kw)

    def _principal(*a: Any, **kw: Any) -> Any:
        rec["principals"] += 1
        return orig_principal(*a, **kw)

    def _call(self: Any, method: str, path: str, *a: Any, **kw: Any) -> Any:
        if method.upper() in _WRITE_METHODS:
            rec["events"].append(f"write {method.upper()} {path.split('?')[0]}")
        return orig_call(self, method, path, *a, **kw)

    mod.require_write_window = _rww
    mod.principal = _principal
    http_cls.call = _call
    try:
        yield rec
    finally:
        mod.require_write_window = orig_rww
        mod.principal = orig_principal
        http_cls.call = orig_call


def _writes(events: list[str]) -> list[str]:
    return [e for e in events if e.startswith("write ")]


# ---------------------------------------------------------------------------
# WW1: window shut → no temp user, no write, SKIP (every write part of both scripts)
# ---------------------------------------------------------------------------

_FLEET_PARTS = [
    ("perf", ["--allow-writes"]),
    ("isolation", ["--allow-writes"]),
    ("grounding-items", ["--allow-writes"]),
    ("ground", ["--allow-writes", "--allow-managed-writes"]),
    ("cleanup", ["--allow-writes"]),
]


@pytest.mark.parametrize("part,flags", _FLEET_PARTS, ids=[p for p, _ in _FLEET_PARTS])
def test_ww1_fleet_window_shut_no_user_no_write(part: str, flags: list[str]) -> None:
    with CmsWorld(run_scheduled=False) as w:
        f2._setup_world(w)
        mod = f2._load_mod(w)
        with _record(mod, refuse=True) as rec:
            rc, text = f2._run_main(mod, flags + ["--part", part],
                                    mdir=tempfile.mkdtemp(prefix="fi-cms-ww1-"))
    assert rec["principals"] == 0, f"{part}: {rec['principals']} temp user(s)\n{text}"
    assert _writes(rec["events"]) == [], f"{part}: writes {_writes(rec['events'])}\n{text}"
    assert "check" in rec["events"], f"{part}: never checked the window\n{text}"
    assert rc == 2, f"{part}: rc {rc}\n{text}"


_MAKER_PARTS = ["perf", "scope", "investigate", "decide", "issue"]


@pytest.mark.parametrize("part", _MAKER_PARTS)
def test_ww1_maker_window_shut_no_user_no_write(part: str) -> None:
    with CmsWorld(run_scheduled=False) as w:
        m2._setup_world(w)
        mod = m2._load_mod(w)
        with _record(mod, refuse=True) as rec:
            rc, text = m2._run_main(mod, part=part, allow_writes=True)
    assert rec["principals"] == 0, f"{part}: {rec['principals']} temp user(s)\n{text}"
    assert _writes(rec["events"]) == [], f"{part}: writes {_writes(rec['events'])}\n{text}"
    assert "check" in rec["events"], f"{part}: never checked the window\n{text}"
    assert rc == 2, f"{part}: rc {rc}\n{text}"


# ---------------------------------------------------------------------------
# WW2: one check per write, plus the start check (happy paths)
# ---------------------------------------------------------------------------

def test_ww2_fleet_ground_one_check_per_write() -> None:
    with CmsWorld(run_scheduled=False) as w:
        f2._setup_world(w)
        mod = f2._load_mod(w)
        mdir = tempfile.mkdtemp(prefix="fi-cms-ww2-")
        runs = []
        for refresh in (False, True):
            if refresh:
                w.refresh_health()
            with _record(mod, refuse=False) as rec:
                rc, text = f2._run_main(mod, ["--allow-writes", "--allow-managed-writes",
                                              "--part", "ground"],
                                        mdir=mdir, run_id=f2._RUN_ID_F2B)
            runs.append((rc, text, list(rec["events"])))
    (rc1, t1, e1), (rc2, t2, e2) = runs
    assert rc1 == 2 and rc2 == 0, f"rc {rc1}/{rc2}\n{t1}\n{t2}"
    w1, w2 = _writes(e1), _writes(e2)
    # Run 1: approve, S2's return to service (must be refused, but it is a write
    # route), RO POST, RO PATCH. Run 2: return to service.
    assert len(w1) == 4 and len(w2) == 1, f"writes {w1} / {w2}\nevents {e1}"
    for n, (ev, wr) in enumerate(((e1, w1), (e2, w2)), start=1):
        checks = ev.count("check")
        assert checks == len(wr) + 1, f"run {n}: {checks} checks for {len(wr)} writes: {ev}"
        # Each write is preceded by a check with no other write in between.
        last = None
        for e in ev:
            if e.startswith("write "):
                assert last == "check", f"run {n}: {e} not preceded by its own check: {ev}"
            last = e


def test_ww2_maker_one_check_per_write() -> None:
    with CmsWorld(run_scheduled=False) as w:
        m2._setup_world(w)
        m2._maker.seed_pattern(w, pattern_id=m2._PATTERN_ID, failure_mode_id=m2._FM_ID,
                               vin_list=[m2._seed.vin(0), m2._seed.vin(1)])
        w.refresh_population()
        mod = m2._load_mod(w)
        per_part: dict[str, list[str]] = {}
        orig = {p: getattr(mod, f"run_{p}") for p in ("investigate", "decide", "issue")}
        with _record(mod, refuse=False) as rec:
            for p, fn in orig.items():
                def _wrapped(*a: Any, _p: str = p, _fn: Any = fn, **kw: Any) -> Any:
                    start = len(rec["events"])
                    try:
                        return _fn(*a, **kw)
                    finally:
                        per_part[_p] = rec["events"][start:]
                setattr(mod, f"run_{p}", _wrapped)
            rc, text, rows = m2._full_run(w, mod, parts=["pre", "investigate", "decide", "issue"])
    assert rc == 0, f"rc {rc}\n{text}"
    expected_writes = {"investigate": 2, "decide": 1, "issue": 1}  # POST + repeat POST
    for p, n in expected_writes.items():
        ev = per_part.get(p, [])
        wr = _writes(ev)
        assert len(wr) == n, f"{p}: writes {wr}"
        assert ev.count("check") == n + 1, f"{p}: {ev.count('check')} checks for {n} writes: {ev}"
        last = None
        for e in ev:
            if e.startswith("write "):
                assert last == "check", f"{p}: {e} not preceded by its own check: {ev}"
            last = e


# ---------------------------------------------------------------------------
# WW3: per-write check refusal stops progress; no write follows (B-1 g78-c2)
# ---------------------------------------------------------------------------

_GROUND_PER_WRITE_LABELS = ["S1", "S2", "RO POST", "RO PATCH", "S6"]


@pytest.mark.parametrize("k", range(5), ids=_GROUND_PER_WRITE_LABELS)
def test_ww3_per_write_refusal_stops_write(k: int) -> None:
    """WW3: refusing ground's per-write check k stops all writes after it (SKIP, rc 2).

    For each of the five per-write window checks of ground (S1, S2, RO POST,
    RO PATCH, S6), refuse only that check and assert no write follows, a SKIP
    row appears, and rc == 2.

    FC3 (removes ``return`` after every per-write ``res.skip``) must fail this
    test for every k because the write proceeds after the refused check.

    Global check index mapping across both ground invocations:
      0=run-1 start, 1=S1, 2=S2, 3=RO POST, 4=RO PATCH,
      5=run-2 start, 6=S6.
    k=0..3 are in run 1 only; k=4 (S6) requires a second invocation.
    """
    # For k=0..3 refuse check k+1 (within run 1); for k=4 refuse check 6 (S6 in run 2).
    refuse_at = k + 1 if k < 4 else 6
    need_second_run = (k == 4)

    with CmsWorld(run_scheduled=False) as w:
        f2._setup_world(w)
        mod = f2._load_mod(w)
        mdir = tempfile.mkdtemp(prefix="fi-cms-ww3-")

        check_count: list[int] = [0]
        all_events: list[str] = []
        orig_rww = mod.require_write_window
        orig_call = mod.Http.call

        def _rww(*a: Any, **kw: Any) -> Any:
            n = check_count[0]
            check_count[0] += 1
            if n == refuse_at:
                all_events.append("check:refused")
                return "window shut (ww3-test)"
            all_events.append("check")
            return orig_rww(*a, **kw)

        def _http_call(self: Any, method: str, path: str, *a: Any, **kw: Any) -> Any:
            if method.upper() in _WRITE_METHODS:
                all_events.append(f"write {method.upper()} {path.split('?')[0]}")
            return orig_call(self, method, path, *a, **kw)

        mod.require_write_window = _rww
        mod.Http.call = _http_call
        try:
            rc, text = f2._run_main(
                mod,
                ["--allow-writes", "--allow-managed-writes", "--part", "ground"],
                mdir=mdir, run_id=f2._RUN_ID_F2B,
            )
            if need_second_run:
                assert rc == 2, f"k=4 run 1 expected SKIP(2): rc={rc}\n{text}"
                w.refresh_health()
                rc, text = f2._run_main(
                    mod,
                    ["--allow-writes", "--allow-managed-writes", "--part", "ground"],
                    mdir=mdir, run_id=f2._RUN_ID_F2B,
                )
        finally:
            mod.require_write_window = orig_rww
            mod.Http.call = orig_call

    label = _GROUND_PER_WRITE_LABELS[k]
    assert "check:refused" in all_events, (
        f"WW3 {label}: refused check not reached; events: {all_events}")
    refuse_idx = all_events.index("check:refused")
    writes_after = [e for e in all_events[refuse_idx + 1:] if e.startswith("write ")]
    assert writes_after == [], (
        f"WW3 {label}: write(s) after refused check: {writes_after}\n"
        f"events: {all_events}\n{text}")
    assert "[SKIP]" in text, f"WW3 {label}: no SKIP row:\n{text}"
    assert rc == 2, f"WW3 {label}: rc {rc} (expected 2):\n{text}"
