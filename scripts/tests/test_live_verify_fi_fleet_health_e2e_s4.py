"""Stage S4 — item 3 e2e tests for live-verify-fi-fleet-health.py.

Tests:
  test_item3_passes_end_to_end           — happy path in clean world
  test_item3_fails_when_labor_op_missing — labor_op absent from ADP's map
  test_item3_fails_when_consumer_drops_event — EventBus hook drops status event
  test_item3_fails_when_cancelled_not_reraised — service Lambda no-ops after cancel
  test_item3_leaves_no_cognito_residue   — LIVETEST Cognito users cleaned up

Run (AWS env stripped):
    env -u AWS_PROFILE AWS_ACCESS_KEY_ID=testing AWS_SECRET_ACCESS_KEY=testing \\
      PYTHONDONTWRITEBYTECODE=1 \\
      .venv/bin/python -m pytest scripts/tests/test_live_verify_fi_fleet_health_e2e_s4.py -q
"""
from __future__ import annotations

import io
import json
import socket
from typing import Any

import pytest
import pyarrow as pa
import pyarrow.parquet as pq

from scripts.tests.fh_world.world import FhWorld
from scripts.tests.fi_rw_world.world import ADP_REGION, ACCOUNT, HEALTH_FN, STAGE

FINDINGS = f"vsa-{STAGE}-avx-findings"
ACTIONS = f"vsa-{STAGE}-avx-actions"
LAKE_BUCKET = f"adp-{STAGE}-foundation-lake-{ACCOUNT}-{ADP_REGION}"
_CLOM_KEY = "config/fleet_health/code_labor_op_map/code_labor_op_map.parquet"
MANAGED_FLAGS = ["--stage", STAGE, "--allow-writes", "--allow-managed-writes"]


@pytest.fixture(autouse=True)
def _loopback_only(monkeypatch: pytest.MonkeyPatch) -> None:
    real_connect = socket.socket.connect

    def connect(sock: socket.socket, address: Any) -> Any:
        host = address[0] if isinstance(address, tuple) else str(address)
        if host not in ("127.0.0.1", "::1", "localhost"):
            raise AssertionError(f"test tried to reach {address!r}")
        return real_connect(sock, address)

    monkeypatch.setattr(socket.socket, "connect", connect)


def _run3(w: FhWorld) -> tuple[int, dict[str, str], str]:
    rc, text, _ = w.run_main(MANAGED_FLAGS + ["--item", "3"])
    return rc, w.rows(text), text


def _fails(rows: dict[str, str]) -> list[str]:
    return sorted(k for k, v in rows.items() if v == "FAIL")


# ---------------------------------------------------------------------------
# Test 1: happy path
# ---------------------------------------------------------------------------

def test_item3_passes_end_to_end() -> None:
    """Positive control: full item 3 round-trip in clean world — key checks PASS.

    3b-labor-op-map: the world's config parquet is augmented with needs_config.yaml
    entries (world fix D-G6), so the Finding's labor_ops are in the map.
    3c-action-completed / 3d-action-cancelled: require pump() to deliver EventBus
    events; script fix (line 1842) makes _item_3 use module-level `time` (ScriptTime).
    """
    with FhWorld() as w:
        rc, rows, text = _run3(w)
        assert not _fails(rows), f"unexpected FAILs: {_fails(rows)}; text={text[-2000:]}"
        for label in ("3a-planned-item", "3a-approve", "3a-ro-id",
                      "3c-action-in-progress", "3a-ro-lines", "3c-patch-ready",
                      "3c-action-completed", "3d-approve", "3d-ro-id",
                      "3d-ro-draft", "3d-patch-close", "3d-action-cancelled",
                      "3d-invoke", "3d-reraised", "3-residue-scan"):
            assert rows.get(label) in ("PASS", "NOTE"), \
                f"{label}={rows.get(label)!r}; text={text[-1500:]}"
        # Review cycle 3 W1: the full path's one residue check found both ROs it made.
        # (Each row prints once live as "[PASS] label: ..." and once in the summary.)
        assert text.count("] 3-residue-scan:") == 1, text[-2000:]
        assert "[PASS] 3-residue-scan: scan found 2 RO(s) for 2 action_id(s)" in text, text[-2000:]
        # rc=2 (skip) is expected: items 1,2,4,5,6,7 not requested → SKIP.
        # Ensure no item-3 row was itself skipped (pre-condition failure).
        item3_skips = [k for k, v in rows.items() if v == "SKIP" and k.startswith("3")]
        assert not item3_skips, f"item-3 rows skipped: {item3_skips}; text={text[-1000:]}"


# ---------------------------------------------------------------------------
# Test 2: labor_op missing from ADP map → 3b-labor-op-map FAIL naming the op
# ---------------------------------------------------------------------------

def test_item3_fails_when_labor_op_missing() -> None:
    """FAIL: overwrite config parquet with an empty map; 3b-labor-op-map FAIL, naming op.

    Positive control: clean world passes 3b-labor-op-map (world fix D-G6 included).
    Failing case: fresh world with empty map fails and names the missing labor_op.
    """
    # Positive control
    with FhWorld() as w:
        _, rows0, _ = _run3(w)
        assert rows0.get("3b-labor-op-map") == "PASS", \
            f"positive control: 3b-labor-op-map={rows0.get('3b-labor-op-map')!r} (world fix needed?)"

    # Failing case: replace config parquet with an empty map
    with FhWorld() as w:
        empty_tbl = pa.table(
            {"code": pa.array([], type=pa.string()),
             "labor_op": pa.array([], type=pa.string())}
        )
        buf = io.BytesIO()
        pq.write_table(empty_tbl, buf, compression="snappy")
        w.internal("s3").put_object(
            Bucket=LAKE_BUCKET, Key=_CLOM_KEY, Body=buf.getvalue())

        rc, rows, text = _run3(w)
        assert rows.get("3b-labor-op-map") == "FAIL", \
            f"expected 3b-labor-op-map FAIL after empty map; rows={rows}; text={text[-800:]}"
        # FAIL detail must name a labor_op
        fail_lines = [l for l in text.splitlines() if "3b-labor-op-map" in l and "FAIL" in l]
        assert fail_lines and any("LAB-" in l for l in fail_lines), \
            f"FAIL detail must name a LAB-* op; lines={fail_lines}"


# ---------------------------------------------------------------------------
# Test 3: consumer drops event → Action stuck, naming the step
# ---------------------------------------------------------------------------

def test_item3_fails_when_consumer_drops_event() -> None:
    """FAIL: EventBus hook drops every dms.ro.status_changed event.

    3c-action-completed depends on the status events; assert that label FAIL.
    Positive control: clean world passes 3c-action-completed.
    """
    # Positive control
    with FhWorld() as w:
        _, rows0, _ = _run3(w)
        assert rows0.get("3c-action-completed") in ("PASS", "NOTE"), \
            f"positive control: {rows0.get('3c-action-completed')!r}"

    # Failing case: drop all pending status events before each pump
    with FhWorld() as w:
        orig_pump = w.bus.pump

        def pump_dropping() -> None:
            w.bus._pending.clear()  # discard every queued status event

        w.bus.pump = pump_dropping  # type: ignore[method-assign]

        rc, rows, text = _run3(w)
        assert rows.get("3c-action-completed") == "FAIL", (
            f"expected 3c-action-completed FAIL after dropping events; rows={rows}; text={text[-1500:]}")


# ---------------------------------------------------------------------------
# Test 4: service invoke → no re-raise after cancel
# ---------------------------------------------------------------------------

def test_item3_fails_when_cancelled_not_reraised() -> None:
    """FAIL: intercept the service Lambda invoke so it returns success without running.

    3d-reraised FAIL naming the VIN: no new attempt>= fb_attempt+1 Finding found.
    Positive control: clean world produces a new Finding.
    """
    # Positive control
    with FhWorld() as w:
        _, rows0, _ = _run3(w)
        assert rows0.get("3d-reraised") in ("PASS", "NOTE"), \
            f"positive control: 3d-reraised={rows0.get('3d-reraised')!r}"

    # Failing case: after_run hook deletes any new service Findings the Lambda writes,
    # so the re-raise poll finds nothing.  The invoke itself returns StatusCode=200
    # (the Lambda ran but produced no persisted output), so 3d-invoke stays PASS.
    with FhWorld() as w:
        # Track finding_ids that existed before item 3's service invoke fires.
        findings_before_service_invoke: set[str] | None = None
        svc_item3_count: list[int] = [0]   # how many service calls happened via item 3

        # World setup has already run the service Lambda twice (prior_days=1 + today).
        # item 3's invoke is the next service call that runs via lam3.invoke (RequestResponse).
        # Count only those triggered AFTER world open (invocations_before_main tracks this).
        def _after_service(fn: str, event: dict, result: Any) -> None:
            nonlocal findings_before_service_invoke
            if fn != HEALTH_FN or event.get("component") != "service":
                return
            # Only intercept invocations that happen during run_main (item 3's call).
            if len(w.lam.invocations) <= w.invocations_before_main:
                return
            svc_item3_count[0] += 1
            if svc_item3_count[0] != 1:
                return
            # Delete any Findings the Lambda just wrote (by comparing to pre-invoke snapshot).
            if findings_before_service_invoke is not None:
                current_ids = {f["finding_id"] for f in w.scan(FINDINGS)}
                new_ids = current_ids - findings_before_service_invoke
                ddb = w.internal("dynamodb")
                for fid in new_ids:
                    ddb.delete_item(
                        TableName=FINDINGS,
                        Key={"finding_id": {"S": fid}},
                    )

        def _before_service(fn: str, event: dict) -> None:
            nonlocal findings_before_service_invoke
            if fn != HEALTH_FN or event.get("component") != "service":
                return
            if len(w.lam.invocations) <= w.invocations_before_main:
                return
            if svc_item3_count[0] == 0:
                findings_before_service_invoke = {
                    f["finding_id"] for f in w.scan(FINDINGS)
                }

        w.lam.before_run.append(_before_service)
        w.lam.after_run.append(_after_service)

        rc, rows, text = _run3(w)
        assert rows.get("3d-reraised") == "FAIL", (
            f"expected 3d-reraised FAIL; rows={rows}; text={text[-1500:]}")
        # The FAIL row names the VIN (FHWLD prefix)
        fail_lines = [l for l in text.splitlines() if "3d-reraised" in l and "FAIL" in l]
        assert fail_lines, "no FAIL line for 3d-reraised"
        assert any("FHWLD" in l or "vin=" in l for l in fail_lines), \
            f"FAIL detail must name the VIN; lines={fail_lines}"


# ---------------------------------------------------------------------------
# Test 5: no LIVETEST Cognito residue
# ---------------------------------------------------------------------------

def test_item3_leaves_no_cognito_residue() -> None:
    """Item 3 creates and cleans up ephemeral LIVETEST-<run>-* Cognito users.

    After a full run, no LIVETEST users remain in the pool.
    The script's own residue-cognito-livetest check must PASS.
    """
    with FhWorld() as w:
        rc, rows, text = _run3(w)

        # Script's built-in residue check — must be present and PASS
        assert rows.get("residue-cognito-livetest") == "PASS", \
            f"residue-cognito-livetest={rows.get('residue-cognito-livetest')!r}; text={text[-1500:]}"

        # Direct check: no LIVETEST users remain in the world pool
        idp = w.internal("cognito-idp")
        pool_id = idp.list_user_pools(MaxResults=10)["UserPools"][0]["Id"]
        users: list[dict] = []
        kw: dict[str, Any] = {"UserPoolId": pool_id}
        while True:
            page = idp.list_users(**kw)
            users.extend(page.get("Users", []))
            tok = page.get("PaginationToken")
            if not tok:
                break
            kw["PaginationToken"] = tok
        livetest = [
            u for u in users
            if any(a["Value"].startswith("LIVETEST-")
                   for a in (u.get("Attributes") or [])
                   if a["Name"] == "email")
        ]
        assert not livetest, \
            f"found {len(livetest)} LIVETEST Cognito user(s) after item 3"

        # Manifest records item 3 writes; item3a_planned is under the "items" key
        assert w.manifests, "no manifest written after item 3"
        manifest_data = json.loads(w.manifests[-1].read_text())
        assert "item3a_planned" in manifest_data["items"], \
            f"manifest missing item3a_planned; keys={list(manifest_data['items'])}"


# ---------------------------------------------------------------------------
# Test 6: W7a — RO lines differ → 3a-ro-lines FAIL naming the changed op
# ---------------------------------------------------------------------------

def test_item3_fails_when_ro_lines_differ() -> None:
    """FAIL: dms_api._invoke hook swaps lines[0].labor_op on the RO the real
    DMS fleet repair-order handler wrote.

    The approve handler calls create_fleet_repair_order synchronously; the hook
    wraps w.dms_api._invoke, fires after the DMS handler returns (RO is in DDB),
    then replaces lines[0].labor_op with <orig>-MUTATED.  The script's
    3a-ro-lines get_item sees the mutated row and FAILs naming the changed op.
    Positive control: clean world passes 3a-ro-lines.
    Mutant WM8 guard: with score_fleet_service_lines replaced by (True, "..."),
    3a-ro-lines is PASS despite the mutation — the assert below FAILs, catching WM8.
    """
    from scripts.tests.fi_rw_world.world import STAGE as _STAGE

    DMS_ROS_TABLE = f"dms-{_STAGE}-repair-orders"

    # Positive control: clean world passes 3a-ro-lines
    with FhWorld() as w:
        _, rows0, _ = _run3(w)
        assert rows0.get("3a-ro-lines") == "PASS", (
            f"positive control: 3a-ro-lines={rows0.get('3a-ro-lines')!r}"
        )

    # Failing case: wrap dms_api._invoke to mutate lines after handler writes RO
    with FhWorld() as w:
        _mutated: set[str] = set()
        _orig_dms_invoke = w.dms_api._invoke

        def _patched_invoke(name: str, fn: Any, event: dict, ctx: Any) -> dict:
            result = _orig_dms_invoke(name, fn, event, ctx)
            # Only intercept the fleet RO create call; mutate at most once.
            if name != "dms:repair_orders.create_fleet_repair_order" or _mutated:
                return result
            try:
                body = json.loads(result.get("body") or "{}")
                ro_id = str(body.get("ro_id") or "")
                if not ro_id:
                    return result
                for ro in w.scan(DMS_ROS_TABLE):
                    if str(ro.get("ro_id")) != ro_id:
                        continue
                    lines = list(ro.get("lines") or [])
                    if not lines:
                        return result
                    orig_op = str(lines[0].get("labor_op") or "")
                    mut = [{**ln} for ln in lines]
                    mut[0] = {**mut[0], "labor_op": orig_op + "-MUTATED"}
                    w.put(DMS_ROS_TABLE, {**ro, "lines": mut})
                    _mutated.add(ro_id)
                    break
            except Exception:  # noqa: BLE001 — never break the world
                pass
            return result

        w.dms_api._invoke = _patched_invoke  # type: ignore[method-assign]

        rc, rows, text = _run3(w)

        assert rows.get("3a-ro-lines") == "FAIL", (
            f"expected 3a-ro-lines FAIL after line mutation; "
            f"mutated={_mutated}; rows={rows}; text={text[-2000:]}"
        )
        fail_lines = [ln for ln in text.splitlines()
                      if "3a-ro-lines" in ln and "FAIL" in ln]
        assert fail_lines, f"no FAIL line for 3a-ro-lines; text={text[-1000:]}"
        assert any("-MUTATED" in ln for ln in fail_lines), (
            f"FAIL detail must name the changed op; lines={fail_lines}"
        )
