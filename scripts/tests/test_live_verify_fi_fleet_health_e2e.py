"""End-to-end tests for scripts/live-verify-fi-fleet-health.py using FhWorld.

Each test drives the script's real main() against the in-process FhWorld
(moto DDB, DuckDB Athena, real producers).  Nothing is a fake written from
the script's own assumptions: what the script reads is what the real Lambda
wrote.

Items tested: 1 (U7 nightly coverage), 2 (fleet routing / p0 disclosure),
6 (re-run idempotency, --allow-writes).

Modelled on test_live_verify_fi_rw_e2e.py (helpers, loopback fixture,
one fresh world per test, hooks on w.lam.after_run / before_run).

Run (AWS env stripped):
    env -u AWS_PROFILE AWS_ACCESS_KEY_ID=testing AWS_SECRET_ACCESS_KEY=testing \\
      PYTHONDONTWRITEBYTECODE=1 \\
      .venv/bin/python -m pytest scripts/tests/test_live_verify_fi_fleet_health_e2e.py -q
"""
from __future__ import annotations

import datetime as dt
import socket
from typing import Any

import pytest

from scripts.tests.fh_world.world import FhWorld
from scripts.tests.fh_world import seed
from scripts.tests.fi_rw_world.world import HEALTH_FN, REGION, STAGE

FINDINGS = f"vsa-{STAGE}-avx-findings"
# Flags sufficient for items 1 and 2 (read-only); item 6 adds --allow-writes.
BASE_FLAGS = ["--stage", STAGE]
WRITE_FLAGS = BASE_FLAGS + ["--allow-writes"]


# ---------------------------------------------------------------------------
# Loopback guard — no connection may leave the machine
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _loopback_only(monkeypatch: pytest.MonkeyPatch) -> None:
    real_connect = socket.socket.connect

    def connect(sock: socket.socket, address: Any) -> Any:
        host = address[0] if isinstance(address, tuple) else str(address)
        if host not in ("127.0.0.1", "::1", "localhost"):
            raise AssertionError(f"test tried to reach {address!r}")
        return real_connect(sock, address)

    monkeypatch.setattr(socket.socket, "connect", connect)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _items(*ns: int) -> list[str]:
    out: list[str] = []
    for n in ns:
        out += ["--item", str(n)]
    return out


def run(w: FhWorld, *ns: int,
        flags: list[str] = BASE_FLAGS) -> tuple[int, dict[str, str], str]:
    rc, text, _ = w.run_main(flags + _items(*ns))
    return rc, w.rows(text), text


def fails(rows: dict[str, str]) -> list[str]:
    return sorted(k for k, v in rows.items() if v == "FAIL")


def assert_passed(rows: dict[str, str], *labels: str) -> None:
    missing = [lb for lb in labels if rows.get(lb) != "PASS"]
    assert not missing, f"not PASS: {[(lb, rows.get(lb)) for lb in missing]}"


# ---------------------------------------------------------------------------
# Gate tests (no FhWorld needed)
# ---------------------------------------------------------------------------

def test_without_live_tests_exits_2_and_calls_nothing() -> None:
    with FhWorld() as w:
        rc, text, _ = w.run_main(["--item", "1"], live=False)
        assert rc == 2 and "SKIPPED" in text
        assert w.main_calls() == []


def test_stage_prod_refused_before_any_aws_call() -> None:
    with FhWorld() as w:
        rc, text, _ = w.run_main(["--stage", "prod", "--item", "1"])
        assert rc == 1 and "refusing 'prod'" in text
        assert w.main_calls() == []


# ---------------------------------------------------------------------------
# Item 1 — U7 nightly managed-VIN coverage
# ---------------------------------------------------------------------------

def test_item1_passes_in_world() -> None:
    """Positive control: battery/tires/brakes all ran and pool fits cap → all PASS."""
    with FhWorld() as w:
        rc, rows, text = run(w, 1)
        assert fails(rows) == [], text[-2000:]
        assert_passed(rows, "1-caps-read",
                      "1-battery-run", "1-battery-select-errors", "1-battery-pool-fitted",
                      "1-tires-run",   "1-tires-select-errors",   "1-tires-pool-fitted",
                      "1-brakes-run",  "1-brakes-select-errors",  "1-brakes-pool-fitted",
                      "1-battery-cap-raised")


def test_item1_fails_when_cap_below_600() -> None:
    """FAIL: battery cap env = 375 (pre-raise) → 1-battery-cap-raised FAIL, row names component.

    Positive control: item 1 passes in the same world before the cap is lowered.
    """
    with FhWorld() as w:
        # Positive: passes before modification
        _, rows_before, _ = run(w, 1)
        assert rows_before.get("1-battery-cap-raised") == "PASS", rows_before

        # Lower cap below required 600
        w.set_function_env(HEALTH_FN, TIER2_MAX_VINS_BATTERY="375")

        _, rows, text = run(w, 1)
        assert rows.get("1-battery-cap-raised") == "FAIL", (
            f"expected 1-battery-cap-raised FAIL when cap=375; rows={rows}")
        # Row name contains "battery" (component named in the failure)
        assert any("battery" in k for k in fails(rows))


def _inject_tires_rotation_cursor(w: FhWorld) -> None:
    """Put a cursor item that records tires rotation (managed_last_vin != None).

    Simulates what happens after a nightly run with cap < pool (2 VINs).
    The verify script reads this cursor from vsa-staging-health-findings.
    """
    w.put("vsa-staging-health-findings", {
        "pk": "CURSOR#tires", "sk": "STATE",
        "last_vin": seed.VIN_TIRE_A, "managed_last_vin": seed.VIN_TIRE_A,
        "pool_exhausted_count": 0, "updated_at": "2026-10-19T02:20:00Z",
    })


def test_item1_fails_when_tires_pool_exceeds_cap() -> None:
    """FAIL: tires cap=1, pool=2, cursor rotates → 1-tires-pool-fitted FAIL."""
    with FhWorld() as w:
        _inject_tires_rotation_cursor(w)
        w.set_function_env(HEALTH_FN, TIER2_MAX_VINS_TIRES="1")
        _, rows, text = run(w, 1)
        assert rows.get("1-tires-pool-fitted") == "FAIL", (
            f"expected 1-tires-pool-fitted FAIL when cap=1 and pool=2; rows={rows}")
        assert "managed pool" in text, f"expected 'managed pool' in output; got:\n{text[-800:]}"


def test_item1_passes_tires_pool_wrap() -> None:
    """PASS: tires cap=2, pool=2, cursor rotates → pool==cap wrap → PASS.

    Catches WM5 (pool count always None): None yields pool-unknown FAIL.
    """
    with FhWorld() as w:
        _inject_tires_rotation_cursor(w)
        w.set_function_env(HEALTH_FN, TIER2_MAX_VINS_TIRES="2")
        _, rows, text = run(w, 1)
        assert rows.get("1-tires-pool-fitted") == "PASS", (
            f"expected 1-tires-pool-fitted PASS when cap=2=pool; rows={rows}\n{text[-800:]}")


def test_item1_fails_when_battery_run_exceeds_wall_budget() -> None:
    """FAIL: battery run finished_at set 601 s after started_at → duration > 600 s budget."""
    import boto3
    import datetime as _dt
    from boto3.dynamodb.conditions import Key as _Key
    with FhWorld() as w:
        runs = boto3.resource("dynamodb", region_name="us-west-2").Table(
            "vsa-staging-avx-agent-runs")
        items = runs.query(
            KeyConditionExpression=_Key("agent_component").eq("agent-2-vehicle-health#battery"),
            ScanIndexForward=False, Limit=1,
        ).get("Items", [])
        assert items, "no battery run record in world"
        rec = items[0]
        t_start = _dt.datetime.fromisoformat(
            rec["started_at"].replace("Z", "+00:00"))
        t_end_new = (t_start + _dt.timedelta(seconds=601)).isoformat(
            timespec="seconds").replace("+00:00", "Z")
        runs.update_item(
            Key={"agent_component": "agent-2-vehicle-health#battery",
                 "started_at": rec["started_at"]},
            UpdateExpression="SET finished_at = :fa",
            ExpressionAttributeValues={":fa": t_end_new},
        )
        _, rows, text = run(w, 1)
        assert rows.get("1-battery-wall-budget") == "FAIL", (
            f"expected 1-battery-wall-budget FAIL after 601 s; rows={rows}")


# ---------------------------------------------------------------------------
# Item 2 — fleet routing + p0 disclosure byte-for-byte
# ---------------------------------------------------------------------------

def test_item2_passes_in_world() -> None:
    """Positive control: service run wrote findings with correct fleet_id and disclosure → all PASS."""
    with FhWorld() as w:
        rc, rows, text = run(w, 2)
        assert fails(rows) == [], text[-2000:]
        assert rows.get("2-p0-item-present") == "PASS"


def test_item2_fails_when_p0_disclosure_differs_by_one_char() -> None:
    """FAIL: p0 disclosure_txt differs from catalog by one appended character.

    Positive control: same world before modification passes item 2.
    """
    with FhWorld() as w:
        # Positive before modification
        _, rows_before, _ = run(w, 2)
        assert fails(rows_before) == [], rows_before

        # Find p0 fleet-health finding with disclosure_txt
        p0 = next(
            f for f in w.scan(FINDINGS)
            if f.get("finding_kind") == "fleet.health.service"
            and f.get("severity") == "p0"
            and f.get("disclosure_txt")
        )
        orig_txt = p0["disclosure_txt"]
        # Append one character — disclosure must match byte-for-byte
        w.put(FINDINGS, {**p0, "disclosure_txt": orig_txt + "."})

        _, rows, text = run(w, 2)
        fail_rows = fails(rows)
        assert any("p0-disclosure" in k for k in fail_rows), (
            f"expected a p0-disclosure FAIL after 1-char mismatch; "
            f"fails={fail_rows}")


def test_item2_fails_when_finding_fleet_id_is_wrong() -> None:
    """FAIL: a finding's fleet_id does not match the VIN's CMS enrollment fleet.

    The VIN is enrolled in FLEET_A; we change the finding's fleet_id to FLEET_B.
    Positive control: same world before modification passes item 2.
    """
    with FhWorld() as w:
        _, rows_before, _ = run(w, 2)
        assert fails(rows_before) == [], rows_before

        # Pick any fleet-health finding and change its fleet_id to the wrong fleet
        fh = next(
            f for f in w.scan(FINDINGS)
            if f.get("finding_kind") == "fleet.health.service"
        )
        vin = (fh.get("scope") or {}).get("vin", "")
        # VIN is enrolled in FLEET_A; change finding to FLEET_B
        wrong_fleet = seed.FLEET_B if fh.get("fleet_id") == seed.FLEET_A else seed.FLEET_A
        w.put(FINDINGS, {**fh, "fleet_id": wrong_fleet})

        _, rows, text = run(w, 2)
        fail_rows = fails(rows)
        assert any("fleet-id" in k for k in fail_rows), (
            f"expected a fleet-id FAIL when finding fleet={wrong_fleet!r} "
            f"vs VIN's CMS fleet; fails={fail_rows}")


# ---------------------------------------------------------------------------
# Item 6 — re-run idempotency (--allow-writes)
# ---------------------------------------------------------------------------

def test_item6_passes_in_world() -> None:
    """Positive control: second service invoke adds no Finding ids, model off is a NOTE."""
    with FhWorld() as w:
        rc, rows, text = run(w, 6, flags=WRITE_FLAGS)
        assert rows.get("6-run-completed") == "PASS", text[-2000:]
        assert rows.get("6-new-findings") == "PASS", text[-2000:]
        # FI_SERVICE_MODEL_ENABLED is not 'true' in the world → NOTE
        assert rows.get("6-model-state") == "NOTE"


def test_item6_fails_when_rerun_writes_new_finding() -> None:
    """FAIL: before the second invoke, inject a new managed VIN with a p0 fault.

    The second invoke processes the new VIN and writes a new Finding id.
    6-new-findings FAILS; the positive control (first invoke) passed cleanly.

    Hook fires before each Lambda run.  Only the 2nd service invocation (item 6's
    second invoke) sees the new VIN; the first invoke stabilises on the original pool.
    """
    with FhWorld() as w:
        svc_count: list[int] = [0]
        today_iso = w.today.isoformat()  # daily tables use w.today (clock-advanced), not seed.TODAY
        new_vin = seed.vin(99)

        def _inject_before_second_service_call(fn: str, event: dict) -> None:
            if fn != HEALTH_FN or event.get("component") != "service":
                return
            svc_count[0] += 1
            if svc_count[0] != 2:
                return
            # Add new VIN to managed_vehicles (the source for _MANAGED_VINS_SQL)
            w.athena.con.execute(
                "INSERT INTO adp_staging_fleet_recall.managed_vehicles VALUES "
                f"('{new_vin}', '{seed.FLEET_A}', DATE '{today_iso}')"
            )
            # Add p0 fault row for new VIN to vehicle_fault_status_daily
            w.athena.con.execute(
                "INSERT INTO adp_staging_fleet_health.vehicle_fault_status_daily VALUES "
                f"('{new_vin}', '{seed.CODE_P0}', '{seed.FLEET_A}', "
                f"DATE '{today_iso}', DATE '{today_iso}', 1, 'p0', 'engine', "
                f"'engine-001', 'open', NULL, NULL, '1.0', "
                f"DATE '{today_iso}', DATE '{today_iso}')"
            )
            # CMS vehicle entry (owner resolution)
            w.put("cms-staging-storage-vehicles", {
                "vehicleId": new_vin, "vin": new_vin, "fleetId": seed.FLEET_A,
                "make": "Meridian", "model": "Azimuth", "year": 2023,
            })
            # DMS fleet membership
            w.put("dms-staging-fleet-membership", {
                "vehicle_vin": new_vin, "fleet_id": seed.FLEET_A,
                "synced_at": f"{today_iso}T00:00:00Z",
            })

        w.lam.before_run.append(_inject_before_second_service_call)

        _, rows, text = run(w, 6, flags=WRITE_FLAGS)
        assert rows.get("6-new-findings") == "FAIL", (
            f"expected 6-new-findings FAIL after injecting new VIN before 2nd invoke; "
            f"rows={rows}; text[-1000:]={text[-1000:]}")
        # FAIL line must name the new finding id (score_new_findings lists FIND-<uuid>s)
        fail_lines = [ln for ln in text.splitlines() if "6-new-findings" in ln and "FAIL" in ln]
        assert any("FIND-" in ln for ln in fail_lines), (
            f"6-new-findings FAIL line must name the new finding id (FIND-...); "
            f"fail_lines={fail_lines}")
