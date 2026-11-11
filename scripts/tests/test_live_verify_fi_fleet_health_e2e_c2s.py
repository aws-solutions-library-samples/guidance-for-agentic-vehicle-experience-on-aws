"""Stage S6, lane C2 — C1 e2e: item 5 ctrl plant delete-failure tests (FhWorld).
Also W2 e2e: item 4 empty daily snapshot → FAIL 4-daily-rows.

Tests:

1. test_item5_delete_fails_exits_1_and_names_plant
2. test_item5_clean_run_ctrl_plant_deleted
3. test_item4_empty_daily_snapshot_fails  (W2)
4. test_item4_clean_world_daily_passes    (W2 positive control)

Run (AWS env stripped):
    env -u AWS_PROFILE AWS_ACCESS_KEY_ID=testing AWS_SECRET_ACCESS_KEY=testing \\
      PYTHONDONTWRITEBYTECODE=1 \\
      AVX_ADP_REPO=$HOME/adp-wt-fi-health-d-g6 \\
      .venv/bin/python -m pytest \\
        scripts/tests/test_live_verify_fi_fleet_health_e2e_c2s.py -v
"""
from __future__ import annotations

from typing import Any

import pytest

from scripts.tests.fh_world.world import FhWorld
from scripts.tests.fi_rw_world.awsfake import AwsError
from scripts.tests.fi_rw_world.world import STAGE

FINDINGS = f"vsa-{STAGE}-avx-findings"
WRITE_FLAGS = ["--stage", STAGE, "--allow-writes"]


# ---------------------------------------------------------------------------
# Loopback guard (no real network calls)
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _loopback_only(monkeypatch: pytest.MonkeyPatch) -> None:
    import socket
    real = socket.socket.connect

    def connect(sock: socket.socket, address: Any) -> Any:
        host = address[0] if isinstance(address, tuple) else str(address)
        if host not in ("127.0.0.1", "::1", "localhost"):
            raise AssertionError(f"test tried to reach {address!r}")
        return real(sock, address)

    monkeypatch.setattr(socket.socket, "connect", connect)


# ---------------------------------------------------------------------------
# Helper: inject a DeleteItem hook that fails for LIVETEST keys
# ---------------------------------------------------------------------------

def _inject_delete_fail(w: FhWorld, table_name: str) -> None:
    """Route DDB DeleteItem to raise ProvisionedThroughputExceededException for the table.

    Any other table passes through to moto.  This simulates a transient error that
    persists beyond the retry budget so the teardown must call mark_cleanup_failed().
    Source for the error shape: boto3 DynamoDB client error codes.
    """
    prev = w.router._handlers.get(("dynamodb", "DeleteItem"))

    def _fail_delete(api: dict[str, Any]) -> Any:
        if api.get("TableName") == table_name:
            raise AwsError(
                "ProvisionedThroughputExceededException",
                "Provisioned throughput exceeded",
                status=400,
            )
        return prev(api) if prev is not None else None

    w.router.add("dynamodb", "DeleteItem", _fail_delete)


def _remove_delete_fail(w: FhWorld) -> None:
    w.router._handlers.pop(("dynamodb", "DeleteItem"), None)


# ---------------------------------------------------------------------------
# Test 1 — delete failure → exit 1 + ctrl_id named
# ---------------------------------------------------------------------------

def test_item5_delete_fails_exits_1_and_names_plant() -> None:
    """Item 5: when the ctrl plant's DeleteItem always fails, run exits 1.

    RED before fix:
    - The finally block swallows the error (``except Exception: pass``).
    - The plant id is LIVETEST-FI-FH-{run}-5-ctrl; the marker LIVETEST-{run}
      is NOT a substring, so residue-findings-livetest returns PASS.
    - Exit code is 0 (not 1).  Assertion ``assert rc == 1`` fails.

    GREEN after fix:
    - Delete is retried 3 times; on the third failure mark_cleanup_failed() is
      called, so the run sets _cleanup_failed=True → exit 1.
    - Plant id is LIVETEST-{run}-FI-FH-5-ctrl (marker is a prefix substring),
      so residue-findings-livetest catches the left-over → exit 1 additionally.
    - The ctrl_id appears in the failure output.
    """
    with FhWorld() as w:
        findings_table = FINDINGS
        _inject_delete_fail(w, findings_table)
        try:
            rc, text, _ = w.run_main(WRITE_FLAGS + ["--item", "5"])
        finally:
            _remove_delete_fail(w)

        assert rc == 1, (
            f"expected exit 1 when ctrl plant delete always fails; got {rc}; "
            f"tail:\n{text[-600:]}"
        )
        # The ctrl_id or at minimum "LIVETEST" must appear in failure output so the
        # operator can identify the residue.
        assert "LIVETEST" in text, (
            f"expected ctrl_id / 'LIVETEST' in failure output; tail:\n{text[-400:]}"
        )


# ---------------------------------------------------------------------------
# Test 2 — clean run: ctrl plant deleted after item 5
# ---------------------------------------------------------------------------

def test_item5_clean_run_ctrl_plant_deleted() -> None:
    """Positive control: without the delete-failure hook item 5 exits 0 and the
    ctrl plant is absent from the findings table afterwards.

    This test is always GREEN (no injection).  It guards against regressions
    where a fix to the delete path accidentally leaves the plant behind even on
    a successful delete (e.g., the retry loop exits without deleting).
    """
    with FhWorld() as w:
        rc, text, _ = w.run_main(WRITE_FLAGS + ["--item", "5"])

        assert rc == 0, (
            f"expected exit 0 on a clean item-5 run; got {rc}; "
            f"tail:\n{text[-400:]}"
        )

        # Read back: no LIVETEST-*-5-ctrl finding should remain.
        remaining = [
            f["finding_id"] for f in w.scan(FINDINGS)
            if "LIVETEST" in f.get("finding_id", "") and "5-ctrl" in f.get("finding_id", "")
        ]
        assert not remaining, (
            f"ctrl plant still in DDB after clean run: {remaining}"
        )

# ---------------------------------------------------------------------------
# W2 — item 4: empty pm_vehicle_status_daily → FAIL 4-daily-rows
# ---------------------------------------------------------------------------

def test_item4_empty_daily_snapshot_fails() -> None:
    """Item 4 FAILs on 4-daily-rows when pm_vehicle_status_daily has no rows for sample.

    RED before fix: empty snap_rows → snap_data_as_of=None → NOTE 4-daily-skew,
    item 4 passes even though the daily snapshot the service agent reads is empty.
    GREEN after fix: `if not snap_rows:` fires before skew gate → FAIL 4-daily-rows.
    Source: review W2 [item 4, daily].
    """
    with FhWorld() as w:
        # Delete all rows from the daily snapshot table so it is empty for the sample.
        s = STAGE
        w.athena.con.execute(
            f"DELETE FROM adp_{s}_fleet_health.pm_vehicle_status_daily"
        )
        rc, text, _ = w.run_main(["--stage", s, "--item", "4"])

    assert rc == 1, (
        f"expected exit 1 when daily snapshot is empty; got {rc}; tail:\n{text[-400:]}"
    )
    assert "4-daily-rows" in text, (
        f"expected '4-daily-rows' FAIL row in output; tail:\n{text[-400:]}"
    )


def test_item4_clean_world_daily_passes() -> None:
    """Positive control: clean FhWorld (daily snapshot populated) passes 4-daily-match.

    Source: review W2 [item 4, daily] positive control.
    """
    with FhWorld() as w:
        s = STAGE
        rc, text, _ = w.run_main(["--stage", s, "--item", "4"])

    assert "4-daily-match" in text and "PASS" in text, (
        f"expected 4-daily-match PASS on clean world; got rc={rc}; tail:\n{text[-400:]}"
    )


# ---------------------------------------------------------------------------
# W1 — 5a-items: fleet-B VIN in fleet-A partition → FAIL naming finding_id
# ---------------------------------------------------------------------------

from scripts.tests.fh_world import seed as _fh_seed_w1

_FLEET_A_W1 = _fh_seed_w1.FLEET_A
_FLEET_B_VIN_W1 = _fh_seed_w1.VIN_ATTN_B   # fleet-B VIN seeded in CMS vehicles


def test_item5_w1_b_vin_in_a_partition_fails() -> None:
    """W1 e2e: Finding planted in fleet-A partition with scope.vin = fleet-B VIN
    → 5a-items FAIL naming the finding_id.

    FhWorld seeds VIN_ATTN_B in cms-staging-storage-vehicles with fleetId=FLEET_B.
    We plant a Finding with fleet_id=FLEET_A but scope.vin=VIN_ATTN_B.
    After fix: _vin_fleet_cms resolves VIN_ATTN_B → FLEET_B ≠ FLEET_A → 5a-items FAIL.

    RED before fix: 5a-items uses finding_id intersection (always empty for different
    partitions in real DDB) → PASS even with a fleet-B VIN in the response.
    """
    with FhWorld() as w:
        # Plant the poisoned finding in fleet A's partition
        poison = {
            "finding_id": "FIND-W1-E2E-POISON",
            "finding_kind": "fleet.health.service",
            "agent_id": "agent-fi-fleet-health",
            "fleet_id": _FLEET_A_W1,
            "scope": {"vin": _FLEET_B_VIN_W1},
            "severity": "soon",
            "computed_at": "2026-10-01T00:00:00Z",
            "expires_at": "2999-12-31T00:00:00Z",
            "recommended_action": {
                "action_kind": "book_fleet_service",
                "target_system": "dms",
                "parameters": {"dealer_id": "DEALER-W1", "needs": []},
            },
        }
        w.put(FINDINGS, poison)
        rc, text, _ = w.run_main(WRITE_FLAGS + ["--item", "5"])

    assert rc == 1, (
        f"expected exit 1 when fleet-B VIN in fleet-A partition; got {rc}; "
        f"tail:\n{text[-400:]}"
    )
    assert "5a-items" in text and "FAIL" in text, (
        f"expected 5a-items FAIL in output; tail:\n{text[-400:]}"
    )
    assert "FIND-W1-E2E-POISON" in text, (
        f"expected finding_id named in 5a-items FAIL; tail:\n{text[-400:]}"
    )


def test_item5_w1_clean_world_passes() -> None:
    """W1 e2e positive control: clean FhWorld (no cross-partition VIN) → 5a-items PASS.

    All fleet-A findings have fleet-A VINs (from seed.VIN_FLEET).
    ctrl item is planted by item 5 and appears in the response.
    """
    with FhWorld() as w:
        rc, text, _ = w.run_main(WRITE_FLAGS + ["--item", "5"])

    assert "5a-items" in text, f"expected 5a-items row; tail:\n{text[-400:]}"
    # Either PASS directly, or no FAIL for 5a-items
    assert "5a-items" not in text or not (
        "5a-items" in text and "FAIL" in text and "FIND-W1-E2E-POISON" not in text
        and rc == 1 and text.count("FAIL") == 1 and text.count("5a-items") == 1
    ), f"unexpected 5a-items FAIL on clean world; tail:\n{text[-400:]}"
    # Simple positive: exit 0 means everything passed
    assert rc == 0, (
        f"expected exit 0 on clean world; got {rc}; tail:\n{text[-400:]}"
    )


# ---------------------------------------------------------------------------
# Review cycle 3 (P1, P2, S1): the two mechanisms, each pinned on its own
# ---------------------------------------------------------------------------

def _ctrl_ids(w: FhWorld) -> list[str]:
    return [f["finding_id"] for f in w.scan(FINDINGS)
            if "LIVETEST" in f.get("finding_id", "") and "5-ctrl" in f.get("finding_id", "")]


def _residue_line(text: str) -> str:
    lines = [ln for ln in text.splitlines() if "] residue-findings-livetest:" in ln]
    assert len(lines) == 1, f"expected one residue-findings-livetest row; got {lines}"
    return lines[0]


def test_item5_failed_delete_names_plant_and_both_mechanisms_fire() -> None:
    """A DeleteItem that always fails: the stderr line names the plant, the cleanup flag
    is set, AND the findings residue check FAILs naming it (each asserted on its own)."""
    with FhWorld() as w:
        _inject_delete_fail(w, FINDINGS)
        try:
            rc, text, _ = w.run_main(WRITE_FLAGS + ["--item", "5"])
        finally:
            _remove_delete_fail(w)
        left = _ctrl_ids(w)
    assert rc == 1, text[-600:]
    assert len(left) == 1, f"the plant should still be in the table; got {left}"
    assert f"could not delete ctrl plant {left[0]!r} after 3 attempts" in text, text[-800:]
    line = _residue_line(text)
    assert "[FAIL]" in line and left[0] in line, line


def test_item5_plant_left_by_a_silent_delete_is_found_by_the_residue_check() -> None:
    """P1 'finds': DeleteItem answers success but leaves the plant (so item 5 sets no
    cleanup flag); only the residue check sees it, FAILs naming it, and the run exits 1.
    Positive control: the clean run's residue row PASSes with no plant left."""
    with FhWorld() as w:
        rc0, text0, _ = w.run_main(WRITE_FLAGS + ["--item", "5"])
        assert rc0 == 0, text0[-600:]
        assert "[PASS]" in _residue_line(text0) and not _ctrl_ids(w)

        prev = w.router._handlers.get(("dynamodb", "DeleteItem"))

        def _silent(api: dict[str, Any]) -> Any:
            key = ((api.get("Key") or {}).get("finding_id") or {}).get("S", "")
            if api.get("TableName") == FINDINGS and key.endswith("-FI-FH-5-ctrl"):
                return {}
            return prev(api) if prev is not None else None

        w.router.add("dynamodb", "DeleteItem", _silent)
        try:
            rc, text, _ = w.run_main(WRITE_FLAGS + ["--item", "5"])
        finally:
            _remove_delete_fail(w)
        left = _ctrl_ids(w)
    assert len(left) == 1, f"the silent delete should leave the plant; got {left}"
    assert "could not delete ctrl plant" not in text, "item 5 itself must not have seen it"
    line = _residue_line(text)
    assert "[FAIL]" in line and left[0] in line, line
    assert rc == 1, text[-600:]


def test_item5_put_raising_after_the_write_landed_still_deletes_the_plant() -> None:
    """PutItem lands the plant, then raises (a timeout after the request was served):
    item 5 SKIPs, deletes the plant anyway, and the residue check PASSes."""
    with FhWorld() as w:
        prev = w.router._handlers.get(("dynamodb", "PutItem"))
        busy = {"on": False}

        def _land_then_raise(api: dict[str, Any]) -> Any:
            if busy["on"]:
                return None  # our own landing write below: let moto serve it
            key = ((api.get("Item") or {}).get("finding_id") or {}).get("S", "")
            if api.get("TableName") == FINDINGS and key.endswith("-FI-FH-5-ctrl"):
                busy["on"] = True
                try:
                    w.internal("dynamodb").put_item(TableName=FINDINGS, Item=api["Item"])
                finally:
                    busy["on"] = False
                raise AwsError("InternalServerError", "timed out after write", status=500)
            return prev(api) if prev is not None else None

        w.router.add("dynamodb", "PutItem", _land_then_raise)
        try:
            rc, text, _ = w.run_main(WRITE_FLAGS + ["--item", "5"])
        finally:
            w.router._handlers.pop(("dynamodb", "PutItem"), None)
            if prev is not None:
                w.router.add("dynamodb", "PutItem", prev)
        left = _ctrl_ids(w)
    rows = w.rows(text)
    assert rows.get("item-5") == "SKIP", rows
    assert not left, f"the plant that landed must be deleted; still present: {left}"
    assert "[PASS]" in _residue_line(text), text[-600:]
    assert rc == 2, text[-600:]
