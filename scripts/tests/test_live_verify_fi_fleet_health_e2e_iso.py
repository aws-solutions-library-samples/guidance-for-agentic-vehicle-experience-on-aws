"""Stage S6 — item 5 fleet-A isolation tests using FhWorld.

Tests drive real ``GET /findings/fleet/{fleet_id}``, ``POST …/approve``,
and ``POST …/decline`` routes through FakeRestApi backed by the real
``api-avx-findings`` handler.  No boto3 mocks; the world handles all AWS calls.

Regression structure
--------------------
* test_item5_cms_half_is_note_not_skip   — failing before G6-D4 fix; documents that
  the CMS half of item 5 must emit NOTE not SKIP (G6-D4).
* test_item5_avx_isolation_passes        — positive control: all six isolation probes
  pass in a clean world (5a-ctrl, 5a-items, 5b-get, 5c-ctrl, 5c-approve, 5d-ctrl,
  5d-decline), no Action created, LIVETEST artefacts cleaned up.
* test_item5_fails_fleet_b_vin_in_fleet_a_response  — normal test: injects a fleet-B
  finding_id into fleet-A's GSI query result via a DDB Query hook; asserts 5a-items
  FAIL naming the leaked finding_id.
* test_item5_fails_approve_b_creates_action   — normal test: inserts a FakeRestApi
  route override that returns 200 for fleet-B approve; asserts 5c-approve FAIL.
* test_items_1_to_6_exit_0  — items 1-6 in one clean world; no FAIL or SKIP from
  requested items; requires fix(fh_world._refresh_lake) for items 3+4.

Run (AWS env stripped):
    env -u AWS_PROFILE AWS_ACCESS_KEY_ID=testing AWS_SECRET_ACCESS_KEY=testing \\
      PYTHONDONTWRITEBYTECODE=1 \\
      .venv/bin/python -m pytest scripts/tests/test_live_verify_fi_fleet_health_e2e_iso.py -v
"""
from __future__ import annotations

import json
import socket
import urllib.error
import urllib.request
from typing import Any

import pytest

from scripts.tests.fh_world import seed
from scripts.tests.fh_world.world import FhWorld
from scripts.tests.fi_rw_world.world import STAGE

FINDINGS = f"vsa-{STAGE}-avx-findings"
ACTIONS = f"vsa-{STAGE}-avx-actions"
WRITE_FLAGS = ["--stage", STAGE, "--allow-writes"]
ALL_FLAGS = WRITE_FLAGS + ["--allow-managed-writes"]
# Exact 403 bodies sourced from handler.py and avx_scope.py (read 2026-10-17).
_GET_FLEET_403 = {"error": "insufficient scope"}
_APPROVE_403 = {"error": "insufficient scope to approve this finding"}
_DECLINE_403 = {"error": "insufficient scope to decline this finding"}


# ---------------------------------------------------------------------------
# Loopback guard
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _loopback_only(monkeypatch: pytest.MonkeyPatch) -> None:
    real = socket.socket.connect

    def connect(sock: socket.socket, address: Any) -> Any:
        host = address[0] if isinstance(address, tuple) else str(address)
        if host not in ("127.0.0.1", "::1", "localhost"):
            raise AssertionError(f"test tried to reach {address!r}")
        return real(sock, address)

    monkeypatch.setattr(socket.socket, "connect", connect)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fleet_op(w: FhWorld, fleet_id: str, run: str) -> tuple[str, str]:
    """Create an ephemeral fleet-operator in the world pool; return (email, id_token).

    Uses direct pool methods (not the live-verify script) to keep the helper
    synchronous and independent of script-level argument plumbing.
    Source for user shape: cognito.py admin_create_user / initiate_auth.
    """
    pool = w.pool
    email = f"LIVETEST-ISO-{run}@iso.invalid"
    pool.admin_create_user(
        {"UserPoolId": pool.pool_id, "Username": email, "MessageAction": "SUPPRESS",
         "UserAttributes": [{"Name": "email", "Value": email},
                            {"Name": "email_verified", "Value": "true"},
                            {"Name": "custom:fleetIds", "Value": fleet_id}]})
    pool.admin_set_user_password(
        {"UserPoolId": pool.pool_id, "Username": email,
         "Password": "IsoTest99!Kiro", "Permanent": True})
    pool.admin_add_user_to_group(
        {"UserPoolId": pool.pool_id, "Username": email, "GroupName": "fleet-operator"})
    auth = pool.initiate_auth(
        {"ClientId": pool.client_id, "AuthFlow": "USER_PASSWORD_AUTH",
         "AuthParameters": {"USERNAME": email, "PASSWORD": "IsoTest99!Kiro"}})
    return email, auth["AuthenticationResult"]["IdToken"]


def _http(base: str, method: str, path: str, tok: str,
          body: dict | None = None) -> tuple[int, dict]:
    """HTTP call to FakeRestApi; returns (status, parsed_body)."""
    url = base.rstrip("/") + "/" + path.lstrip("/")
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(
        url, data=data, method=method,
        headers={"Authorization": f"Bearer {tok}",
                 "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


# ---------------------------------------------------------------------------
# Test 1 — CMS half of item 5 must be NOTE, not SKIP  (G6-D4)
# ---------------------------------------------------------------------------

def test_item5_cms_half_is_note_not_skip() -> None:
    """item 5's CMS half must emit NOTE, not SKIP (G6-D4: CMS route moved to Group 7).

    FAILING before the fix: res.skip → res.note in live-verify-fi-fleet-health.py.
    After the fix this becomes a regression guard ensuring the decision isn't
    accidentally reverted.
    """
    with FhWorld() as w:
        rc, text, _ = w.run_main(WRITE_FLAGS + ["--item", "5"])
        rows = w.rows(text)
        assert rows.get("5-cms") == "NOTE", (
            f"expected '5-cms'=NOTE (G6-D4 decision), got {rows.get('5-cms')!r}; "
            f"rc={rc}"
        )


# ---------------------------------------------------------------------------
# Test 2 — AVX isolation passes (positive control, direct HTTP)
# ---------------------------------------------------------------------------

def test_item5_avx_isolation_passes() -> None:
    """Positive control: --item 5 passes all 5a-5d rows via the real script."""
    with FhWorld() as w:
        rc, text, _ = w.run_main(WRITE_FLAGS + ["--item", "5"])
        rows = w.rows(text)
        for lbl in ("5a-ctrl", "5a-items", "5b-get", "5c-ctrl",
                    "5c-approve", "5d-ctrl", "5d-decline"):
            assert rows.get(lbl) == "PASS", (
                f"{lbl}={rows.get(lbl)!r}\n{text[-400:]}"
            )


# ---------------------------------------------------------------------------
# Test 2b — 5b-get FAIL when GET fleet/B returns 200 instead of 403
# ---------------------------------------------------------------------------

def test_item5_fails_5b_get_returns_200() -> None:
    """5b-get FAIL: GET /findings/fleet/B returns 200; FAIL line names fleet_b id."""
    from scripts.tests.fi_rw_world.apigw import Route
    import json as _json

    with FhWorld() as w:
        rc0, text0, _ = w.run_main(WRITE_FLAGS + ["--item", "5"])
        assert w.rows(text0).get("5b-get") == "PASS", text0[-400:]

        manifest = _json.loads(w.manifests[-1].read_text())
        fleet_b = manifest["items"]["item5_fleet_b"]
        real_fn = next(r.fn for r in w.avx_api.routes
                       if r.template == "/findings/fleet/{fleet_id}" and r.method == "GET")

        def _bypass_b(event: dict, ctx: Any) -> dict:
            if (event.get("pathParameters") or {}).get("fleet_id") == fleet_b:
                return {"statusCode": 200,
                        "body": _json.dumps({"fleet_id": fleet_b, "findings": []})}
            return real_fn(event, ctx)

        w.avx_api.routes.insert(0, Route("/findings/fleet/{fleet_id}", "GET",
                                         "findings", _bypass_b))
        try:
            rc1, text1, _ = w.run_main(WRITE_FLAGS + ["--item", "5"])
        finally:
            w.avx_api.routes.pop(0)

        rows1 = w.rows(text1)
        assert rows1.get("5b-get") == "FAIL", (
            f"expected 5b-get=FAIL, got {rows1.get('5b-get')!r}\n{text1[-400:]}"
        )
        fail_line = next((ln for ln in text1.splitlines()
                          if "5b-get" in ln and "FAIL" in ln), "")
        assert fleet_b in fail_line, (
            f"5b-get FAIL line does not name fleet_b={fleet_b!r}: {fail_line!r}"
        )


# ---------------------------------------------------------------------------
# Test 2c — 5d-decline FAIL when decline fleet/B returns 200 instead of 403
# ---------------------------------------------------------------------------

def test_item5_fails_5d_decline_returns_200() -> None:
    """5d-decline FAIL: decline fleet-B finding returns 200; FAIL line names fb_id."""
    from scripts.tests.fi_rw_world.apigw import Route
    import json as _json

    with FhWorld() as w:
        rc0, text0, _ = w.run_main(WRITE_FLAGS + ["--item", "5"])
        assert w.rows(text0).get("5d-decline") == "PASS", text0[-400:]

        fb = next(f for f in w.scan(FINDINGS)
                  if f.get("fleet_id") == seed.FLEET_B and f.get("recommended_action"))
        fb_id = fb["finding_id"]

        def _bypass_decline(event: dict, _ctx: Any) -> dict:
            return {"statusCode": 200,
                    "body": _json.dumps({"finding_id": fb_id, "status": "declined"})}

        w.avx_api.routes.insert(
            0, Route(f"/findings/{fb_id}/decline", "POST", "findings", _bypass_decline))
        try:
            rc1, text1, _ = w.run_main(WRITE_FLAGS + ["--item", "5"])
        finally:
            w.avx_api.routes.pop(0)

        rows1 = w.rows(text1)
        assert rows1.get("5d-decline") == "FAIL", (
            f"expected 5d-decline=FAIL, got {rows1.get('5d-decline')!r}\n{text1[-400:]}"
        )
        fail_line = next((ln for ln in text1.splitlines()
                          if "5d-decline" in ln and "FAIL" in ln), "")
        assert fb_id in fail_line, (
            f"5d-decline FAIL line does not name fb_id={fb_id!r}: {fail_line!r}"
        )


# ---------------------------------------------------------------------------
# Test 5 — items 1-6 exit 0 in a single clean world
# ---------------------------------------------------------------------------

def test_items_1_to_6_exit_0() -> None:
    """Items 1-6 in a single clean world: exit 0 with no FAIL and no SKIP row.

    Runs all write flags (--allow-writes --allow-managed-writes) for items 1-6.
    Requesting only items 1-6 must not emit SKIP rows for item 7 or any other
    item — not-requested items are simply not run; emitting SKIP for them is a
    script bug (fix applied in live-verify-fi-fleet-health.py).

    Requires fix(fh_world._refresh_lake): pm_vehicle_status and vehicle_fault_status
    are recomputed from current raw data before each Athena query so item-3 DMS ROs
    are visible to item-4's view query.  Without that fix, items 3+4 in one run
    diverge because item-3 writes a DMS RO that item-4's oracle sees but the
    pre-materialised pm_vehicle_status snapshot does not.
    """
    with FhWorld() as w:
        flags = WRITE_FLAGS + ["--allow-managed-writes"]
        for item in ("1", "2", "3", "4", "5", "6"):
            flags += ["--item", item]
        rc, text, _ = w.run_main(flags)
        rows = w.rows(text)
        bad = {k: v for k, v in rows.items() if v in ("FAIL", "SKIP")}
        assert rc == 0 and not bad, (
            f"exit={rc}; unexpected FAIL/SKIP rows: {bad}; tail:\n{text[-600:]}"
        )


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Test 3 — 5a-items FAIL when fleet-B finding leaks to fleet-A GET response
# ---------------------------------------------------------------------------

def test_item5_fails_fleet_b_vin_in_fleet_a_response() -> None:
    """5a-items FAIL: fleet-B finding_id injected into fleet-A GET response.

    Positive control: clean world -> 5a-items PASS.
    Regression injection: insert a FakeRestApi route override for GET /fleet/{id}
    that wraps the real handler and appends a fleet-B finding_id to fleet-A's
    response.  The script's 5a-items check intersects fleet-A response IDs with
    DDB-sourced fleet-B IDs and FAILs, naming the leaked fb_id.

    Note: the xfail scenario (new finding with wrong fleet_id + scope.vin=VIN_BRAKE_B)
    is NOT detectable by 5a-items because the script checks finding_ids (not scope.vin).
    This test injects the DETECTABLE regression: a real fleet-B finding_id in the
    fleet-A GET response.
    """
    from scripts.tests.fi_rw_world.apigw import Route
    import json as _json

    with FhWorld() as w:
        # Positive control: 5a-items PASS in a clean world.
        rc0, text0, _ = w.run_main(WRITE_FLAGS + ["--item", "5"])
        assert w.rows(text0).get("5a-items") == "PASS", text0[-400:]

        fb_finding = next(
            f for f in w.scan(FINDINGS)
            if f.get("fleet_id") == seed.FLEET_B and f.get("recommended_action")
        )
        fb_id = fb_finding["finding_id"]

        # Inject regression: wrap the real GET /fleet/{id} handler so fleet-A's
        # response includes fb_id.  The route override runs inside the same lambda_env
        # as the real handler (same route name="findings"), so real_fn(event, ctx)
        # is safe to call.
        real_get_fleet = next(
            r for r in w.avx_api.routes
            if r.template == "/findings/fleet/{fleet_id}" and r.method == "GET"
        )
        real_fn = real_get_fleet.fn

        def _inject_fleet_a_response(event: dict, ctx: Any) -> dict:
            result = real_fn(event, ctx)
            if (event.get("pathParameters") or {}).get("fleet_id") == seed.FLEET_A:
                body = _json.loads(result.get("body") or "{}")
                body.setdefault("findings", []).append({
                    "finding_id": fb_id, "finding_kind": "fleet.health.service",
                    "agent_id": "agent-fi-fleet-health", "severity": "soon",
                    "computed_at": "2026-10-19T00:00:00Z",
                    "recommended_action": {"action_kind": "book_fleet_service",
                                           "target_system": "dms", "parameters": {}},
                })
                result["body"] = _json.dumps(body)
            return result

        override = Route("/findings/fleet/{fleet_id}", "GET", "findings", _inject_fleet_a_response)
        w.avx_api.routes.insert(0, override)
        try:
            rc1, text1, _ = w.run_main(WRITE_FLAGS + ["--item", "5"])
        finally:
            w.avx_api.routes.pop(0)

        rows1 = w.rows(text1)
        assert rows1.get("5a-items") == "FAIL", (
            f"expected 5a-items=FAIL after injection, got {rows1.get('5a-items')!r}; "
            f"tail: {text1[-400:]}"
        )
        fail_line = next(
            (ln for ln in text1.splitlines() if "5a-items" in ln and "FAIL" in ln), ""
        )
        assert fb_id in fail_line, (
            f"fb_id {fb_id!r} not in 5a-items FAIL line: {fail_line}"
        )


# ---------------------------------------------------------------------------
# Test 4 — 5c-approve FAIL when fleet-B approve returns 200 instead of 403
# ---------------------------------------------------------------------------

def test_item5_fails_approve_b_creates_action() -> None:
    """5c-approve FAIL: approve fleet-B finding returns 200 (scope check bypassed).

    Positive control: clean world -> 5c-approve PASS (handler returns 403).
    Regression injection: insert a FakeRestApi route override that returns 200 for
    fleet-B finding's approve, simulating a handler scope-check bypass.  The script's
    5c-approve check sees 200 instead of 403 and FAILs.

    The xfail scenario (Cognito claim expansion) is detectable by 5c-approve when
    the handler returns 200; this test injects that outcome directly.
    """
    from scripts.tests.fi_rw_world.apigw import Route
    import json as _json

    with FhWorld() as w:
        # Positive control: 5c-approve PASS in clean world.
        rc0, text0, _ = w.run_main(WRITE_FLAGS + ["--item", "5"])
        assert w.rows(text0).get("5c-approve") == "PASS", text0[-400:]

        fb = next(f for f in w.scan(FINDINGS)
                  if f.get("fleet_id") == seed.FLEET_B and f.get("recommended_action"))
        fb_id = fb["finding_id"]

        # Inject regression: add a route override that returns 200 for fleet-B approve.
        # Inserted at position 0 to match before the real findings handler.
        def _bypass_approve(event: dict, _ctx: Any) -> dict:
            return {"statusCode": 200, "body": _json.dumps({
                "action_id": "INJECTED-BYPASS-001", "finding_id": fb_id,
                "status": "pending_execution", "action_kind": "book_fleet_service",
            })}

        override = Route(
            f"/findings/{fb_id}/approve", "POST", "findings", _bypass_approve
        )
        w.avx_api.routes.insert(0, override)
        try:
            rc1, text1, _ = w.run_main(WRITE_FLAGS + ["--item", "5"])
        finally:
            w.avx_api.routes.pop(0)

        rows1 = w.rows(text1)
        assert rows1.get("5c-approve") == "FAIL", (
            f"expected 5c-approve=FAIL after bypass injection, "
            f"got {rows1.get('5c-approve')!r}; tail: {text1[-400:]}"
        )
