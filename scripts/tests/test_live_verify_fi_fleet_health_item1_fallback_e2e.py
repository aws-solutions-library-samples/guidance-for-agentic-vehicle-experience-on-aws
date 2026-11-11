"""Item 1 against the real runner's 7-day fallback record (issue 2026-10-22-battery-fallback-record).

FhWorld runs the real Tier 2 runner for the nightly battery/tires/brakes runs. Here the tires
cap is 1 against a managed tire pool of 2, so the runner itself rotates and writes the run
record's ``managed_coverage``; the script must PASS on that record, and FAIL once the record is
removed or its arithmetic is broken. The runner reads its caps at import, so the world's runner
gets cap 1 by patching the module constant; the Lambda env (what the script reads) gets it too.

Run (AWS env stripped):
    env -u AWS_PROFILE AWS_ACCESS_KEY_ID=testing AWS_SECRET_ACCESS_KEY=testing \\
      PYTHONDONTWRITEBYTECODE=1 \\
      .venv/bin/python -m pytest scripts/tests/test_live_verify_fi_fleet_health_item1_fallback_e2e.py -q
"""
from __future__ import annotations

import socket
from typing import Any

import pytest

from scripts.tests.fh_world.world import FhWorld
from scripts.tests.fi_rw_world.world import HEALTH_FN, STAGE

RUNS = f"vsa-{STAGE}-avx-agent-runs"
TIRES_PK = "agent-2-vehicle-health#tires"


@pytest.fixture(autouse=True)
def _loopback_only(monkeypatch: pytest.MonkeyPatch) -> None:
    real_connect = socket.socket.connect

    def connect(sock: socket.socket, address: Any) -> Any:
        host = address[0] if isinstance(address, tuple) else str(address)
        if host not in ("127.0.0.1", "::1", "localhost"):
            raise AssertionError(f"test tried to reach {address!r}")
        return real_connect(sock, address)

    monkeypatch.setattr(socket.socket, "connect", connect)


@pytest.fixture()
def tires_cap_1(monkeypatch: pytest.MonkeyPatch):
    from agents.tier2 import runner

    monkeypatch.setattr(runner, "_MAX_VINS_TIRES", 1)
    with FhWorld() as w:
        w.set_function_env(HEALTH_FN, TIER2_MAX_VINS_TIRES="1")
        yield w


def _item1(w: FhWorld) -> tuple[dict[str, str], str]:
    _, text, _ = w.run_main(["--stage", STAGE, "--item", "1"])
    return w.rows(text), text


def _latest_tires_record() -> tuple[Any, dict[str, Any]]:
    import boto3
    from boto3.dynamodb.conditions import Key

    table = boto3.resource("dynamodb", region_name="us-west-2").Table(RUNS)
    items = table.query(KeyConditionExpression=Key("agent_component").eq(TIRES_PK),
                        ScanIndexForward=False, Limit=1)["Items"]
    assert items, "no tires run record in the world"
    return table, items[0]


def test_the_runners_own_record_passes_item_1(tires_cap_1: FhWorld) -> None:
    """Positive control: the real runner rotated tires and recorded it; item 1 passes."""
    _, rec = _latest_tires_record()
    cov = rec.get("managed_coverage")
    assert isinstance(cov, dict) and cov.get("mode") == "7_day_fallback", rec
    assert int(cov["cap"]) == 1 and int(cov["managed_pool"]) == 2, cov
    rows, text = _item1(tires_cap_1)
    assert rows.get("1-tires-pool-fitted") == "PASS", (rows, text[-1500:])
    assert [k for k, v in rows.items() if v == "FAIL"] == [], text[-2000:]
    assert "7-day fallback: managed pool 2 (run record 2), cap 1" in text, text[-1500:]


def test_removing_the_record_fails_item_1(tires_cap_1: FhWorld) -> None:
    table, rec = _latest_tires_record()
    table.update_item(Key={"agent_component": TIRES_PK, "started_at": rec["started_at"]},
                      UpdateExpression="REMOVE managed_coverage")
    rows, text = _item1(tires_cap_1)
    assert rows.get("1-tires-pool-fitted") == "FAIL", rows
    assert "managed_coverage absent" in text


def test_a_record_with_broken_arithmetic_fails_item_1(tires_cap_1: FhWorld) -> None:
    table, rec = _latest_tires_record()
    table.update_item(Key={"agent_component": TIRES_PK, "started_at": rec["started_at"]},
                      UpdateExpression="SET managed_coverage.nights_to_cover = :n",
                      ExpressionAttributeValues={":n": 5})
    rows, text = _item1(tires_cap_1)
    assert rows.get("1-tires-pool-fitted") == "FAIL", rows
    assert "nights_to_cover=5 (ceil(2/1) is 2)" in text, text[-1500:]


def test_a_record_cap_other_than_the_live_cap_fails_item_1(tires_cap_1: FhWorld) -> None:
    """The record must describe the cap the Lambda runs at; a record claiming cap 3 fails."""
    table, rec = _latest_tires_record()
    table.update_item(Key={"agent_component": TIRES_PK, "started_at": rec["started_at"]},
                      UpdateExpression="SET managed_coverage.cap = :c",
                      ExpressionAttributeValues={":c": 3})
    rows, text = _item1(tires_cap_1)
    assert rows.get("1-tires-pool-fitted") == "FAIL", rows
    assert "record cap=3 != live cap 1" in text, text[-1500:]


def test_a_run_invoked_by_hand_is_read_and_passes_on_its_own_record(tires_cap_1: FhWorld) -> None:
    """Note KK: after the deploy, one by-hand run with the schedule's input, then item 1.

    The world's clock stands at midday; the by-hand tires run is the newest record, so item 1
    reads it (not the 02:20Z run) and passes on the record that run wrote."""
    _, night = _latest_tires_record()
    tires_cap_1.lam.run(HEALTH_FN, {"stage": STAGE, "component": "tires"})
    tires_cap_1.pump()
    _, rec = _latest_tires_record()
    assert rec["started_at"] != night["started_at"], (night, rec)
    cov = rec.get("managed_coverage")
    assert isinstance(cov, dict) and cov.get("mode") == "7_day_fallback", rec
    rows, text = _item1(tires_cap_1)
    assert rows.get("1-tires-run") == "PASS", rows
    assert f"started_at={rec['started_at']} (outside the 02:00-03:30Z schedule window " \
           "(invoked by hand))" in text, text[-1500:]
    assert rows.get("1-tires-pool-fitted") == "PASS", (rows, text[-1500:])
    assert f"this run's slice {int(cov['slice_start'])}-" in text, text[-1500:]
