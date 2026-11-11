"""W1 smoke: fleet-operator gets plans for own fleet; 403 for other fleet.

FACTS (cms:services/fleet_intelligence/):
  - list_plans 200 body: {"data": [plans]} (pm_plans.py:_resp(200, ...))
  - 403 body: {"error": "Not authorized for the requested fleet scope"}
    (index.py:_err + pm_plans.py:_err, same format, same message string)
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request

from scripts.tests.fi_cms_world.world import CmsWorld
from scripts.tests.fi_rw_world import seed

FLEET_A = seed.FLEETS[0]   # "demo-rental-carshare"
FLEET_B = seed.FLEETS[1]   # "demo-lastmile-delivery"


def test_pm_plans_fleet_scope() -> None:
    with CmsWorld(run_scheduled=False) as w:
        w.seed_pm_plan(FLEET_A, "plan-001")
        tok = w.cms_fi_token(["fleet-operator"], [FLEET_A])
        base = w.cms_fi_url + "api/v1/fleet-intelligence/pm/plans"

        # fleet-operator of fleet A gets their own fleet's plans
        req_a = urllib.request.Request(
            f"{base}?fleetId={FLEET_A}",
            headers={"Authorization": f"Bearer {tok}"},
        )
        with urllib.request.urlopen(req_a) as resp:
            assert resp.status == 200, f"expected 200, got {resp.status}"
            body = json.loads(resp.read())
        plans = body.get("data") or []
        assert any(p.get("planId") == "plan-001" for p in plans), (
            f"plan-001 missing from data: {plans}"
        )

        # same token, fleet B → 403 with the fleet-scope error
        req_b = urllib.request.Request(
            f"{base}?fleetId={FLEET_B}",
            headers={"Authorization": f"Bearer {tok}"},
        )
        try:
            with urllib.request.urlopen(req_b):
                raise AssertionError("expected 403 for fleet B, got 200")
        except urllib.error.HTTPError as exc:
            assert exc.code == 403, f"expected 403, got {exc.code}"
            body_b = json.loads(exc.read())
            assert "Not authorized" in body_b.get("error", ""), (
                f"unexpected 403 body: {body_b}"
            )


# W7 — verify the CMS tables in the world have the same GSI key schema/projections
# as CMS storage_stack.py at 1ce93a75b.
def test_cms_tables_match_live_shapes() -> None:
    """Groundings table must have open-groundings-index (openFleetId/groundedAt, ALL);
    dtc-history must have active-code-index (vehicleId/activeCode, ALL)."""
    with CmsWorld(run_scheduled=False) as w:
        ddb = w.internal("dynamodb")

        # --- groundings table ---
        gt = ddb.describe_table(TableName="cms-staging-storage-vehicle-groundings")["Table"]
        gsi_names = {g["IndexName"] for g in gt.get("GlobalSecondaryIndexes", [])}
        assert "open-groundings-index" in gsi_names, (
            f"open-groundings-index missing; found: {gsi_names}"
        )
        ogi = next(g for g in gt["GlobalSecondaryIndexes"]
                   if g["IndexName"] == "open-groundings-index")
        ks = {k["KeyType"]: k["AttributeName"] for k in ogi["KeySchema"]}
        assert ks["HASH"] == "openFleetId", ks
        assert ks["RANGE"] == "groundedAt", ks
        assert ogi["Projection"]["ProjectionType"] == "ALL", ogi["Projection"]

        # --- dtc-history table ---
        dt = ddb.describe_table(TableName="cms-staging-storage-dtc-history")["Table"]
        dtc_gsi_names = {g["IndexName"] for g in dt.get("GlobalSecondaryIndexes", [])}
        assert "active-code-index" in dtc_gsi_names, (
            f"active-code-index missing; found: {dtc_gsi_names}"
        )
        aci = next(g for g in dt["GlobalSecondaryIndexes"]
                   if g["IndexName"] == "active-code-index")
        ks2 = {k["KeyType"]: k["AttributeName"] for k in aci["KeySchema"]}
        assert ks2["HASH"] == "vehicleId", ks2
        assert ks2["RANGE"] == "activeCode", ks2
        assert aci["Projection"]["ProjectionType"] == "ALL", aci["Projection"]


# W11 — CmsWorld.open must refuse a DMS checkout that isn't at de6c181
def test_dms_root_must_be_pinned_commit(tmp_path: "pytest.TempPathFactory", monkeypatch: "pytest.MonkeyPatch") -> None:  # noqa: F821
    """FI_RW_WORLD_DMS_REPO pointed at a clone checked out to a wrong commit raises."""
    import subprocess
    from pathlib import Path  # noqa: PLC0415
    # Build a minimal git repo with one commit (not de6c181).
    repo = tmp_path / "fake_dms"
    repo.mkdir()
    subprocess.run(["git", "-C", str(repo), "init", "-b", "main"], check=True,
                   capture_output=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@t.com"],
                   check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "T"], check=True,
                   capture_output=True)
    (repo / "README.md").write_text("fake\n")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "init"],
                   check=True, capture_output=True)
    monkeypatch.setattr("scripts.tests.fi_cms_world.world.DMS_ROOT", repo)
    try:
        with CmsWorld(run_scheduled=False):
            pass
        raise AssertionError("CmsWorld.open should have raised for wrong DMS commit")
    except RuntimeError as exc:
        assert "de6c181" in str(exc), str(exc)


# W-A — guard order: harness _client allowlist guard must run before the world's router
def test_guard_order_enforced_inside_cms_world() -> None:
    """Inside CmsWorld, _client's GuardViolation fires for ops outside the allowlist.

    GO1 (guard order off) must fail this test: when _GUARD_ORDER["on"] stays False,
    the world's router answers ListUsers first and GuardViolation is never raised.
    """
    import pytest  # noqa: PLC0415
    from scripts.fi_cms_live.harness import GuardViolation, _client  # noqa: PLC0415

    with CmsWorld(run_scheduled=False) as w:
        idp = _client(
            "cognito-idp",
            region=w.region,
            allowed_ops=frozenset({"cognito-idp.ListUserPools"}),
        )
        # ListUsers is NOT in the allowlist -> GuardViolation fires before the router answers
        with pytest.raises(GuardViolation) as exc_info:
            idp.list_users(UserPoolId=w.pool.pool_id)
        assert type(exc_info.value) is GuardViolation, (
            f"expected GuardViolation, got {type(exc_info.value)}"
        )
        assert "ListUsers" in str(exc_info.value), str(exc_info.value)
        # Allowed op: guard passes -> no GuardViolation (router or moto handles the rest)
        try:
            idp.list_user_pools(MaxResults=1)
        except GuardViolation:
            raise AssertionError(
                "GuardViolation raised for ListUserPools which is in the allowlist"
            )
        except Exception:
            pass  # guard passed; any non-GuardViolation error is acceptable


# W-G — dirty-tree half of _require_pinned_checkout
def test_require_pinned_checkout_dirty_tree_raises(
    tmp_path: "pytest.TempPathFactory",  # noqa: F821
    monkeypatch: "pytest.MonkeyPatch",  # noqa: F821
) -> None:
    """_require_pinned_checkout raises RuntimeError when the tree is dirty.

    WD1 (dirty-tree check disabled) must fail this test.
    """
    import subprocess as _subprocess  # noqa: PLC0415
    from scripts.tests.fi_cms_world.world import _require_pinned_checkout  # noqa: PLC0415

    DMS_COMMIT = "de6c181"
    fake_path = tmp_path / "fake_dms_dirty"
    fake_path.mkdir()

    _calls: list[list[str]] = []

    original_run = _subprocess.run

    def fake_run(args: list[str], **kw: object) -> object:  # type: ignore[override]
        _calls.append(list(args))
        if "rev-parse" in args:
            return type("R", (), {"stdout": f"{DMS_COMMIT}abcdef123456\n",
                                  "returncode": 0})()
        if "status" in args:
            return type("R", (), {"stdout": " M handlers/some_handler.py\n",
                                  "returncode": 0})()
        return original_run(args, **kw)

    monkeypatch.setattr("subprocess.run", fake_run)
    try:
        _require_pinned_checkout(fake_path, DMS_COMMIT)
        raise AssertionError("_require_pinned_checkout should raise for a dirty tree")
    except RuntimeError as exc:
        assert "not clean" in str(exc) or "dirty" in str(exc).lower() or fake_path.name in str(exc) or "handlers" in str(exc), str(exc)


# W-E — every DMS RO write via the real route must be captured in the image log
def test_dms_ro_real_patch_logs_three_images() -> None:
    """Ready → InProgress → Ready via the real PATCH route gives three lake images.

    WR1 (collapses _ro_image_log to one image per ro_id) must fail this test.
    The fix: _invoke_api auto-appends after a successful DMS RO create/PATCH.
    """
    import urllib.parse  # noqa: PLC0415
    import uuid  # noqa: PLC0415

    with CmsWorld(run_scheduled=False) as w:
        vin = seed.vin(0)
        ro_id = f"RO-WE-{uuid.uuid4().hex[:8]}"
        dealer_id = "dealer-test-we01"

        # Seed the RO directly in DDB — no route call → no image logged yet.
        w.put(w.DMS_RO_TABLE, {
            "ro_id": ro_id,
            "vehicle_vin": vin,
            "dealer_id": dealer_id,
            "status": "Draft",
            "lines": [{"labor_op": "LAB-TEST-01"}],
            "pay_types": [],
            "updated_at": "2026-10-01T09:00:00+00:00",
            "ro_partition": "RO",
        })
        before = [img for img in w._ro_image_log if img.get("ro_id") == ro_id]
        assert not before, f"expected 0 images before any PATCH, got {before}"

        # Mint a service-advisor token authorised for dealer_id.
        tok = w.dms_sa_token(dealer_id)
        ro_path = (
            f"/api/dms/dealers/{urllib.parse.quote(dealer_id, safe='')}"
            f"/repair-orders/{urllib.parse.quote(ro_id, safe='')}"
        )
        url = w.dms_url.rstrip("/") + "/" + ro_path.lstrip("/")

        # Three real-route PATCHes: Ready → InProgress → Ready.
        for new_status in ("Ready", "InProgress", "Ready"):
            req = urllib.request.Request(
                url, method="PATCH",
                data=json.dumps({"status": new_status}).encode(),
                headers={"Authorization": tok, "Content-Type": "application/json"},
            )
            try:
                with urllib.request.urlopen(req, timeout=10) as resp:
                    assert resp.status == 200, (
                        f"PATCH to {new_status!r}: expected 200, got {resp.status}")
            except urllib.error.HTTPError as exc:
                raise AssertionError(
                    f"PATCH to {new_status!r} failed {exc.code}: {exc.read()}"
                ) from exc

        # The three PATCHes must each have added one image (WR1 collapses to one).
        images = [img for img in w._ro_image_log if img.get("ro_id") == ro_id]
        assert len(images) == 3, (
            f"expected 3 images after 3 PATCHes, got {len(images)}: {images}")
        statuses = [img["status"] for img in images]
        assert statuses == ["Ready", "InProgress", "Ready"], (
            f"wrong status sequence: {statuses}")

        # Repair date (final Ready): the 3rd image's updated_at must equal the current DDB row.
        ddb_rows = [r for r in w.scan(w.DMS_RO_TABLE) if r.get("ro_id") == ro_id]
        assert ddb_rows, "RO missing from DDB after PATCHes"
        assert images[2]["updated_at"] == ddb_rows[0]["updated_at"], (
            f"final Ready updated_at mismatch: "
            f"image={images[2]['updated_at']!r} ddb={ddb_rows[0]['updated_at']!r}")

        # Lake: three rows, in order (WR1 keeps only the last → count fails).
        w._refresh_lake()
        rows = w.athena.con.execute(
            "SELECT _seq_no, status, updated_at FROM dms_staging.repair_orders "
            f"WHERE ro_id = '{ro_id}' ORDER BY CAST(_seq_no AS INTEGER)"
        ).fetchall()
        assert len(rows) == 3, (
            f"expected 3 lake rows for {ro_id}, got {len(rows)}: {rows}")
        lake_statuses = [r[1] for r in rows]
        assert lake_statuses == ["Ready", "InProgress", "Ready"], (
            f"lake status sequence: {lake_statuses}")
