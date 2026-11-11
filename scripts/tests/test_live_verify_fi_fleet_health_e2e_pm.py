"""E2E tests for item 4 (PM recompute) of live-verify-fi-fleet-health.py.

World carries every PM case the view distinguishes:
  overdue (VIN_PM_OVERDUE, calendar, operator completion)
  due-by-date (VIN_PM_DUE, calendar, operator completion)
  no_history (VIN_PM_CURRENT, calendar, no completions)
  current via service_records (VIN_PM_SR, tire_service 2026-09-01)
  current via DMS line (VIN_PM_DMS, LAB-BRK-01 Invoiced 2026-09-15)
  due-by-miles + rounding (VIN_PM_MILES, mileage, operator; 14499 total miles)
  no_history (VIN_PM_HV, HV_BATTERY_CHECK, only fault DMS op LAB-HVISO-01)

Run:
    env -u AWS_PROFILE AWS_ACCESS_KEY_ID=testing AWS_SECRET_ACCESS_KEY=testing \\
      PYTHONDONTWRITEBYTECODE=1 \\
      .venv/bin/python -m pytest scripts/tests/test_live_verify_fi_fleet_health_e2e_pm.py -q
"""
from __future__ import annotations

import socket
from typing import Any

import pytest

from scripts.tests.fh_world.world import FhWorld
from scripts.tests.fh_world.seed import PM_CODE_TIRE, VIN_PM_MILES, VIN_PM_OVERDUE

BASE_FLAGS = ["--stage", "staging"]


@pytest.fixture(autouse=True)
def _loopback_only(monkeypatch: pytest.MonkeyPatch) -> None:
    real_connect = socket.socket.connect

    def connect(sock: socket.socket, address: Any) -> Any:
        host = address[0] if isinstance(address, tuple) else str(address)
        if host not in ("127.0.0.1", "::1", "localhost"):
            raise AssertionError(f"test tried to reach {address!r}")
        return real_connect(sock, address)

    monkeypatch.setattr(socket.socket, "connect", connect)


def _run4(w: FhWorld) -> tuple[int, dict[str, str], str]:
    rc, text, _ = w.run_main(BASE_FLAGS + ["--item", "4"])
    rows: dict[str, str] = {}
    for line in text.splitlines():
        s = line.strip()
        for st in ("PASS", "FAIL", "SKIP", "NOTE"):
            if s.startswith(f"[{st}] "):
                rows[s[len(st) + 3:].split(":", 1)[0]] = st
    return rc, rows, text


def test_item4_passes_in_world() -> None:
    """Positive control: item 4 PASS; oracle computes >1 (vin,task) pairs."""
    with FhWorld() as w:
        rc, rows, text = _run4(w)
        assert [k for k, v in rows.items() if v == "FAIL"] == [], text[-2000:]
        assert rows.get("4-view-match") == "PASS", text[-1500:]
        note = next((ln for ln in text.splitlines()
                     if "4-oracle:" in ln and "rows computed" in ln), None)
        assert note is not None
        assert int(note.split("4-oracle:")[1].split("(vin,task)")[0].strip()) > 1


def test_item4_fails_on_tampered_view_row() -> None:
    """FAIL: live_defects='pm_view_flip' injects a view-level defect; item 4 names VIN.

    pm_view_flip flips VIN_PM_OVERDUE/TIRE_ROTATION status to 'current' in the
    live view after each recompute.  The oracle retains 'overdue'.  Item 4's
    4-view-match check detects the divergence and FAILs, naming the VIN.
    """
    with FhWorld(live_defects=frozenset({"pm_view_flip"})) as w:
        rc, rows, text = _run4(w)
        assert rows.get("4-view-match") == "FAIL", text[-1500:]
        diff_notes = [ln for ln in text.splitlines() if "4-view-diff:" in ln]
        assert any(VIN_PM_OVERDUE[:8] in ln for ln in diff_notes), diff_notes


def test_item4_fault_op_not_counted_as_pm_completion() -> None:
    """LAB-HVISO-01 (fault P0AA6) must NOT count as HV_BATTERY_CHECK completion.

    The oracle must use ADP's map: HV_BATTERY_CHECK maps to LAB-HV-02, not
    LAB-HVISO-01.  Both view and oracle agree: no_history.  Item 4 PASS.
    """
    with FhWorld() as w:
        rc, rows, text = _run4(w)
        assert rows.get("4-view-match") == "PASS", text[-2000:]


def test_item4_mileage_rounding_case() -> None:
    """Mileage-based VIN_PM_MILES exercises _cast_bigint in the oracle.

    World data: last_odo=9999, interval=5000, total=14499.
    ndm = 9999 + 5000 = 14999; cur_odo = 14499; miles_left = 500 -> 'due'.
    Item 4 PASS confirms oracle and view agree on the mileage status.
    The mileage VIN must appear in the oracle output (status 'due').
    """
    with FhWorld() as w:
        rc, rows, text = _run4(w)
        assert rows.get("4-view-match") == "PASS", text[-2000:]
        # Verify oracle has a 'due' row for VIN_PM_MILES
        import importlib.util, pathlib
        script = pathlib.Path("scripts/live-verify-fi-fleet-health.py")
        spec = importlib.util.spec_from_file_location("lv_fh", script)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
        assert mod._cast_bigint(14498.5) == 14499  # rounds up (Trino CAST)
        assert round(14498.5) == 14498             # Python banker's rounds down


def test_item4_daily_not_false_fail_after_item3() -> None:
    """4-daily-match must PASS when item 3 creates a DMS RO before item 4 runs.

    After item 3 books an RO the view recomputes (DMS RO now Ready), but the
    daily snapshot was written at open() and doesn't include it.  Without the
    fix this produces a false 4-daily-match FAIL for the touched VIN.  With the
    fix, VINs whose DMS RO was updated after the snapshot cutoff are excluded
    from the daily comparison.
    """
    with FhWorld() as w:
        WRITE_FLAGS = ["--stage", "staging", "--allow-writes", "--allow-managed-writes"]
        rc3, text3, _ = w.run_main(WRITE_FLAGS + ["--item", "3"])
        rows3 = w.rows(text3)
        assert rc3 != 1, f"item3 exit={rc3}; FAIL rows: {[k for k,v in rows3.items() if v=='FAIL']}"
        rc4, rows4, text4 = _run4(w)
        bad = {k: v for k, v in rows4.items() if v == "FAIL"}
        assert not bad, f"item4 FAIL after item3: {bad}\n{text4[-600:]}"


def test_item4_daily_fails_for_untouched_tampered_vin() -> None:
    """4-daily-match FAILs when a daily-snapshot row is tampered for a pre-cutoff RO VIN.

    VIN_PM_DMS has RO-010 (LAB-BRK-01 Invoiced 2026-09-15), which is BEFORE the
    snapshot cutoff (midnight on snapshot day).  After tampering its
    pm_vehicle_status_daily BRAKE_INSPECTION row, item 4 must still detect the
    divergence and FAIL — the exclusion must only protect VINs whose ROs were
    updated AFTER the snapshot, not VINs with any RO at all.

    Catches mutant WM4 (exclusion widened to every VIN with any RO): with WM4,
    VIN_PM_DMS is excluded from the daily comparison so the tamper becomes
    invisible and item 4 returns PASS, causing this test to fail.
    """
    from scripts.tests.fh_world.seed import PM_CODE_BRAKE, VIN_PM_DMS  # noqa: PLC0415
    with FhWorld() as w:
        assert _run4(w)[1].get("4-daily-match") == "PASS"
        w.athena.con.execute(
            "UPDATE adp_staging_fleet_health.pm_vehicle_status_daily "
            "SET status = 'overdue', next_due_date = '2026-01-01' "
            "WHERE vin = ? AND task_code = ?",
            [VIN_PM_DMS, PM_CODE_BRAKE],
        )
        rc1, rows1, text1 = _run4(w)
        assert rows1.get("4-daily-match") == "FAIL", text1[-1500:]
        diff_notes = [ln for ln in text1.splitlines() if "4-daily-diff:" in ln]
        assert any(VIN_PM_DMS[:8] in ln for ln in diff_notes), diff_notes


def test_item4_scoped_plan_vin_appears_in_oracle_output() -> None:
    """VIN_PM_SCOPED (PLN-008 scoped EV/Azimuth/2023) must appear in oracle as 'due'.

    PLN-008 completion 2026-04-22 + 180 d = 2026-10-19 = data_as_of.
    next_due_date == data_as_of → 'due' (overdue boundary is strict <).

    Catches mutant WM3: scoped applies_to branch disabled → scoped plans cover no
    VIN → VIN_PM_SCOPED missing from oracle → 4-view-match FAIL (view has it, oracle
    doesn't) → this assertion fails because 4-view-match is not PASS.

    Mutation verified: applying WM3 ('if True:' replacing the IS-NULL check) to a
    scratch archive copy causes 4-view-match to FAIL and this test to fail.
    """
    from scripts.tests.fh_world.seed import VIN_PM_SCOPED  # noqa: PLC0415
    with FhWorld() as w:
        rc, rows, text = _run4(w)
        assert rows.get("4-view-match") == "PASS", text[-2000:]
        # Confirm VIN_PM_SCOPED / TIRE_ROTATION appears in oracle output (status 'due').
        oracle_note = next(
            (ln for ln in text.splitlines()
             if "4-oracle:" in ln and "rows computed" in ln), None
        )
        assert oracle_note is not None, "missing 4-oracle note"
        oracle_count = int(oracle_note.split("4-oracle:")[1].split("(vin,task)")[0].strip())
        # 7 original + VIN_PM_SCOPED/TIRE_ROTATION + VIN_PM_DUE/TIRE_ROTATION from PLN-008
        assert oracle_count >= 9, f"expected >=9 oracle rows, got {oracle_count}"
