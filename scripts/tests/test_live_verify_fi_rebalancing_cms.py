"""``live-verify-fi-rebalancing.py --part cms`` / ``--part cms-next`` (task 7.5) against the shared
fake world in ``fi_rebal_cms_world.py``, built from the real code: CMS's FI routes and the screen's
approve proxy at the deployed commit, AVX's approve route and ``CmsExecutor`` over HTTPS, the
real agent's move Findings and CMS's real refresh. Every test drives ``main()`` with argv.

Properties pinned here, each with a passing control and a world that breaks it:
isolation (reads and writes), the grounded-vehicle refusal and its sentence, the cleanup that
returns the test vehicle to service, the write windows and the flags that gate every write.
"""
from __future__ import annotations

import datetime as dt
import json
import re
from pathlib import Path
from typing import Any

import pytest

from scripts.tests import fi_rebal_cms_world as W

GROUNDED = "Vehicle {vin} is grounded and can't be moved."
ROW_RE = re.compile(r"^(PASS|FAIL|SKIP)\s+(.+?): (.*)$")


@pytest.fixture()
def world(tmp_path: Path):
    with W.CmsWorld(tmp_path) as w:
        yield w


def rows(out: str) -> dict[str, tuple[str, str]]:
    found: dict[str, tuple[str, str]] = {}
    for line in out.splitlines():
        m = ROW_RE.match(line)
        if m:
            found[m.group(2)] = (m.group(1), m.group(3))
    return found


def by_route(w: W.CmsWorld, method: str, suffix: str) -> list[dict]:
    return [r for r in w.cms.requests if r["method"] == method and (r.get("route") or "").endswith(suffix)]


def finding_at(w: W.CmsWorld, to_loc: str) -> dict:
    return next(f for f in w.move_findings() if f["location"]["to_location_id"] == to_loc)


def vins_of(f: dict) -> list[str]:
    return list(f["recommended_action"]["parameters"]["vehicle_vins"])


def manifest(w: W.CmsWorld) -> dict:
    files = sorted((w.tmp / "state").glob("*.json"))
    assert len(files) == 1, files
    return json.loads(files[0].read_text())


FULL = ("--allow-writes", "--approve-move")


# ---------------------------------------------------------------------------
# The world: what the agent recommended (the selection tests below rely on it)
# ---------------------------------------------------------------------------


def test_world_recommends_one_aus_move_and_two_atl_moves_with_excluded_cars(world):
    moves = {f["location"]["to_location_id"]: vins_of(f) for f in world.move_findings()}
    assert moves == {"loc-aus-hub2": [W.vin(11), W.vin(12)],
                     "loc-atl-airport": [W.ROSTER_VIN, W.vin(3)],
                     "loc-atl-hub1": [W.vin(1)]}, moves
    assert world.vehicle_id_of(W.vin(1)) == W.IOS_HELD


def test_world_fleet_records_decide_which_fleet_is_rental(world):
    """CMS 75007152b reads each fleet record's ``fleetType`` so the world's fleets fixture
    decides which kind a fleet is. Control: the rental fleet's utilization is a real figure.
    Its record retyped to the depot fleet's type: the view has basis "on_rent" but the kind
    is now "depot", so CMS returns unavailable (basis mismatch, S1) with fleetKind "depot"."""
    url = world.cms.url + "/api/v1/fleet-intelligence/utilization"
    token = world.user(W.RENTAL)
    code, body, _ = world.mod._get_json(url, token, {"days": "7", "fleetId": W.RENTAL})
    assert code == 200 and body.get("unavailable") is not True, body
    assert isinstance((body.get("summary") or {}).get("utilizationPct"), (int, float)), body
    world._res().Table(W.FLEETS).put_item(
        Item={"fleetId": W.RENTAL, "fleetType": W.FLEET_TYPES[W.DELIVERY]})
    code, body, _ = world.mod._get_json(url, token, {"days": "7", "fleetId": W.RENTAL})
    # S1 (CMS 75007152b): stale-kind view → unavailable with fleetKind so the page keeps its layout.
    assert code == 200 and body.get("unavailable") is True and body.get("fleetKind") == "depot", body
    assert body.get("reason") == world.lc.REASON_UNEXPECTED_SHAPE, body


# ---------------------------------------------------------------------------
# The full run
# ---------------------------------------------------------------------------


def test_full_run_passes_and_leaves_the_world_as_intended(world):
    a, b = finding_at(world, "loc-aus-hub2"), finding_at(world, "loc-atl-airport")
    rc, out, err = world.run_main(world.argv(*FULL))
    r = rows(out)
    assert rc == 0, (out, err)
    for name in ("C0 candidates", "C1 rental reads", "C2 depot reads", "C3 latency",
                 "C4 isolation reads", "C5 grounded move refused", "C5 isolation groundings",
                 "C5 return to service", "C6 move approved", "C7 isolation writes", "C8 residue"):
        assert r.get(name, ("missing",))[0] == "PASS", (name, r.get(name), out)
    # The move: AVX executed it, CMS recorded it and moved both cars.
    act = world.action(a["finding_id"])
    assert act["status"] == "executed" and act["decision"] == "approved", act
    mv = world.move(world.decision_id(a["finding_id"]))
    assert mv and mv["vins"] == vins_of(a) and mv["toLocationId"] == "loc-aus-hub2", mv
    for v in vins_of(a):
        assert world.vehicle(world.vehicle_id_of(v))["locationId"] == "loc-aus-hub2"
    # The probe: refused through the screen, nothing moved, the car back in service.
    pb = world.action(b["finding_id"])
    assert pb["status"] == "declined" and pb["decision"] == "overridden", pb
    assert pb["audit_trail"][-1].get("error_status") == 409, pb["audit_trail"]
    assert pb["parameters"]["vehicle_vins"] == [W.vin(3)], pb["parameters"]
    assert world.move(world.decision_id(b["finding_id"])) is None
    vid = world.vehicle_id_of(W.vin(3))
    assert world.vehicle(vid)["locationId"] == "loc-atl-downtown"
    g = world.groundings(vid)
    assert len(g) == 1 and g[0]["returnedAt"] and "openFleetId" not in g[0], g
    assert g[0]["actionId"].startswith("LIVETEST-75-") and g[0]["campaignId"] == "MRD-9999-975"
    # The grounding sentence came from CMS's own move route.
    direct = [x for x in by_route(world, "POST", "/moves") if x["status"] == 409]
    assert direct and json.loads(direct[0]["response"]) == {"error": GROUNDED.format(vin=W.vin(3))}
    # Temp users gone; the manifest records the move for the next-day part.
    assert not world.pool.users and len(world.pool.deleted) == 2
    m = manifest(world)
    assert m["move"]["action_id"] == world.decision_id(a["finding_id"])
    assert m["move"]["effective_date"] == mv["effectiveDate"]
    assert m["move"]["from"] == "loc-aus-downtown" and m["move"]["to"] == "loc-aus-hub2"
    assert m["grounding"]["returned"] is True
    assert "left by design" in r["C8 residue"][1] and world.decision_id(b["finding_id"]) in r["C8 residue"][1]


def test_the_approve_goes_through_the_screens_route_with_every_vin(world):
    a = finding_at(world, "loc-aus-hub2")
    rc, out, err = world.run_main(world.argv(*FULL))
    assert rc == 0, (out, err)
    sent = [x for x in by_route(world, "POST", "/actions/{findingId}/approve")
            if x["path"].endswith(f"/{a['finding_id']}/approve") and x["status"] == 200]
    assert json.loads(sent[0]["body"]) == {"parameters": {"vehicle_vins": vins_of(a)}}
    # AVX received the operator's token through CMS (check 1 there, check 2 in AVX).
    assert world.avx.sent("POST", "/findings/{finding_id}/approve")


def test_candidates_skip_ios_held_and_roster_cars(world):
    rc, out, err = world.run_main(world.argv())
    c0 = rows(out)["C0 candidates"]
    # ATL hub1 holds the iOS user's car, ATL airport the roster car: the move is AUS.
    assert c0[0] == "PASS" and finding_at(world, "loc-aus-hub2")["finding_id"] in c0[1], c0
    # The probe car is ATL airport's other car, never the roster car.
    assert f"vin={W.vin(3)}" in c0[1] and W.ROSTER_VIN not in c0[1].split("vin=")[1], c0


@pytest.mark.parametrize("kw", [{"recall_blocking": frozenset({W.vin(3)})}, {"recall_age_h": 25.0},
                                {"recall_missing": frozenset({W.vin(3)})}],
                         ids=["open-recall", "stale-recall-views", "not-in-cohort"])
def test_no_probe_car_without_a_clear_fresh_recall_view(tmp_path, kw):
    with W.CmsWorld(tmp_path, **kw) as w:
        rc, out, err = w.run_main(w.argv(*FULL))
        r = rows(out)
        assert r["C0 candidates"][0] == "PASS" and "probe=none" in r["C0 candidates"][1], r
        assert r["C5 grounded move refused"][0] == "SKIP", r["C5 grounded move refused"]
        assert not w.items(W.GROUNDINGS)
        # The move waits for the probe, so one run shows both and approves one real move.
        assert r["C6 move approved"][0] == "SKIP" and "C5 did not pass" in r["C6 move approved"][1]
        assert w.action(finding_at(w, "loc-aus-hub2")["finding_id"]) is None


def test_a_grounded_car_disqualifies_its_move(world):
    vid = world.vehicle_id_of(W.vin(11))
    world._res().Table(W.GROUNDINGS).put_item(Item={
        "vehicleId": vid, "actionId": "AVX-ACT-world", "vin": W.vin(11), "fleetId": W.RENTAL,
        "openFleetId": W.RENTAL, "campaignId": "MRD-2026-001", "findingId": "F",
        "approverSub": "s", "groundedAt": "2026-10-21T00:00:00Z"})
    rc, out, err = world.run_main(world.argv(*FULL))
    r = rows(out)
    assert r["C0 candidates"][0] == "SKIP" and "grounded" in r["C0 candidates"][1], r
    assert not world.cms.sent("POST")


def test_a_full_destination_disqualifies_its_move(world):
    world._res().Table(W.LOCS).update_item(
        Key={"fleetId": W.RENTAL, "locationId": "loc-aus-hub2"},
        UpdateExpression="SET parkingSpaces = :p", ExpressionAttributeValues={":p": 3})
    rc, out, err = world.run_main(world.argv(*FULL))
    r = rows(out)
    assert r["C0 candidates"][0] == "SKIP" and "no room at loc-aus-hub2" in r["C0 candidates"][1], r
    assert not world.cms.sent("POST")


def test_a_decided_finding_is_not_a_candidate(world):
    a = finding_at(world, "loc-aus-hub2")
    world._res().Table(W.ACTIONS).put_item(Item={
        "action_id": world.decision_id(a["finding_id"]), "finding_id": a["finding_id"],
        "decision": "declined", "status": "declined"})
    rc, out, err = world.run_main(world.argv(*FULL))
    r = rows(out)
    assert r["C0 candidates"][0] == "SKIP", r
    assert not world.cms.sent("POST")


def test_no_live_move_skips_every_write_and_sends_nothing(world):
    for f in world.move_findings():
        f["expires_at"] = "2026-01-01T00:00:00Z"
        world.put_finding(f)
    rc, out, err = world.run_main(world.argv(*FULL))
    r = rows(out)
    assert rc == 2, out
    assert r["C0 candidates"][0] == "SKIP" and "no rental move" in r["C0 candidates"][1]
    for name in ("C5 grounded move refused", "C6 move approved", "C7 isolation writes"):
        assert r[name][0] == "SKIP", (name, r[name])
    assert not world.cms.sent("POST")


# ---------------------------------------------------------------------------
# Isolation, reads and writes
# ---------------------------------------------------------------------------


def test_isolation_reads_fail_when_cms_skips_its_fleet_check(world):
    world.defects["cms_scope_open"] = True
    rc, out, err = world.run_main(world.argv("--allow-writes"))
    r = rows(out)
    assert rc == 1 and r["C4 isolation reads"][0] == "FAIL", out
    assert "utilization" in r["C4 isolation reads"][1] and "200" in r["C4 isolation reads"][1]


def test_isolation_reads_need_the_cms_refusal_not_any_403(world):
    rc, out, err = world.run_main(world.argv("--allow-writes"))
    r = rows(out)
    assert r["C4 isolation reads"][0] == "PASS", out
    probes = [x for x in world.cms.requests if x["method"] == "GET" and x.get("status") == 403]
    assert len(probes) >= 6, probes
    assert all(json.loads(p["response"]) == {"error": "Not authorized for the requested fleet scope"}
               for p in probes)


@pytest.mark.parametrize("sentence", [
    "Unavailable until trip history arrives",
    "Fleet utilization applies to rental and car-share fleets.",
    "No data yet",
])
def test_c2_fails_when_demand_gives_old_sentence(world, monkeypatch, sentence):
    """C2 fails when demand/gaps gives any sentence other than the exact new demand sentence."""
    real = world.ix.rebalance_source.depot_response
    monkeypatch.setattr(world.ix.rebalance_source, "depot_response",
                        lambda route, fid: {**real(route, fid), "depot_message": sentence})
    rc, out, err = world.run_main(world.argv("--allow-writes"))
    r = rows(out)
    assert rc == 1 and r["C2 depot reads"][0] == "FAIL", r["C2 depot reads"]


def test_c2_fails_when_utilization_figure_is_null(world):
    """C2 fails when the depot utilization response carries a null utilizationPct."""
    real = world.mod._get_json
    def get(url: str, *a: Any, **k: Any) -> Any:
        code, body, secs = real(url, *a, **k)
        if "/utilization" in url and isinstance(body, dict) and body.get("basis") == "trips":
            body = {**body, "summary": {**(body.get("summary") or {}), "utilizationPct": None}}
        return code, body, secs
    world.mod._get_json = get
    rc, out, err = world.run_main(world.argv("--allow-writes"))
    assert rows(out)["C2 depot reads"][0] == "FAIL"


def test_c2_fails_when_utilization_basis_is_on_rent(world):
    """C2 fails when the depot utilization route returns basis 'on_rent' instead of 'trips'."""
    real = world.mod._get_json
    def get(url: str, *a: Any, **k: Any) -> Any:
        code, body, secs = real(url, *a, **k)
        if "/utilization" in url and isinstance(body, dict) and body.get("basis") == "trips":
            body = {**body, "basis": "on_rent"}
        return code, body, secs
    world.mod._get_json = get
    rc, out, err = world.run_main(world.argv("--allow-writes"))
    assert rows(out)["C2 depot reads"][0] == "FAIL"


def test_c2_fails_when_utilization_has_no_fleet_kind(world):
    """C2 fails when the depot utilization response is missing the fleetKind field."""
    real = world.mod._get_json
    def get(url: str, *a: Any, **k: Any) -> Any:
        code, body, secs = real(url, *a, **k)
        if "/utilization" in url and isinstance(body, dict) and body.get("basis") == "trips":
            body = {k2: v for k2, v in body.items() if k2 != "fleetKind"}
        return code, body, secs
    world.mod._get_json = get
    rc, out, err = world.run_main(world.argv("--allow-writes"))
    assert rows(out)["C2 depot reads"][0] == "FAIL"


def test_depot_reads_fail_when_demand_and_gaps_disagree(world):
    """C2 checks each non-utilization route independently; when gaps gives an old sentence,
    C2 fails even if demand has the correct sentence."""
    real = world.mod._get_json
    def get(url: str, *a: Any, **k: Any) -> Any:
        code, body, secs = real(url, *a, **k)
        if "/gaps" in url and isinstance(body, dict) and "depot_message" in body:
            body = {**body, "depot_message": "Fleet utilization applies to rental and car-share fleets."}
        return code, body, secs
    world.mod._get_json = get
    rc, out, err = world.run_main(world.argv("--allow-writes"))
    r = rows(out)
    assert rc == 1 and r["C2 depot reads"][0] == "FAIL", r["C2 depot reads"]


def test_c2_pass_row_names_both_depot_utilization_windows(world):
    """C2 PASS detail names both days windows; M2 (drop days=1 read) and M3 (detail → 'ok')
    must each cause this test to FAIL (W2/S1 coverage)."""
    rc, out, err = world.run_main(world.argv("--allow-writes"))
    c2 = rows(out)["C2 depot reads"]
    assert c2[0] == "PASS" and "7d 35.7%" in c2[1] and "1d 37.5%" in c2[1], c2


def test_isolation_reads_fail_on_a_gateway_403(world):
    world.defects["depot_gateway_403"] = True   # refused, but not by CMS's fleet check
    rc, out, err = world.run_main(world.argv("--allow-writes"))
    r = rows(out)
    assert rc == 1 and r["C4 isolation reads"][0] == "FAIL", out


def test_isolation_reads_fail_without_a_passing_control(world):
    world.defects["cms_refuses_all"] = True     # every probe "refused", nothing proven
    rc, out, err = world.run_main(world.argv("--allow-writes"))
    r = rows(out)
    assert r["C4 isolation reads"][0] == "FAIL" and "control" in r["C4 isolation reads"][1], r


def test_isolation_write_probe_fails_when_the_approve_proxy_skips_check_one(world):
    world.defects["queue_scope_open"] = True
    a = finding_at(world, "loc-aus-hub2")
    rc, out, err = world.run_main(world.argv(*FULL))
    r = rows(out)
    assert rc == 1 and r["C7 isolation writes"][0] == "FAIL", out
    assert "fleet_not_in_scope" in r["C7 isolation writes"][1]
    # Even failing open, the probe changed nothing: same decision, same move record.
    assert world.action(a["finding_id"])["decided_by"]["kind"] == "operator"
    assert len([m for m in world.items(W.MOVES) if m.get("decidedByKind") != "seed"]) == 1


def test_isolation_write_probe_fails_when_the_move_route_skips_its_fleet_check(world):
    a = finding_at(world, "loc-aus-hub2")
    orig = world._cms_fi

    def fi(event: dict) -> dict:
        # The defect only once the move exists, so the move itself happens normally.
        moved = world.move(world.decision_id(a["finding_id"])) is not None
        world.defects["cms_scope_open"] = moved and event["httpMethod"] == "POST"
        return orig(event)
    world._cms_fi = fi
    world.cms.routes = [(m, t, p, fi if fn == orig else fn) for m, t, p, fn in world.cms.routes]
    rc, out, err = world.run_main(world.argv(*FULL))
    r = rows(out)
    assert rc == 1 and r["C7 isolation writes"][0] == "FAIL", out
    assert "/moves" in r["C7 isolation writes"][1]
    assert len([m for m in world.items(W.MOVES) if m.get("decidedByKind") != "seed"]) == 1


# ---------------------------------------------------------------------------
# The grounded-vehicle refusal
# ---------------------------------------------------------------------------


def test_grounded_probe_fails_and_stops_when_cms_moves_a_grounded_car(world):
    world.defects["moves_ignore_groundings"] = True
    a, b = finding_at(world, "loc-aus-hub2"), finding_at(world, "loc-atl-airport")
    rc, out, err = world.run_main(world.argv(*FULL))
    r = rows(out)
    assert rc == 1 and r["C5 grounded move refused"][0] == "FAIL", out
    assert W.vin(3) in r["C5 grounded move refused"][1]
    assert r["C6 move approved"][0] == "SKIP", r["C6 move approved"]
    assert world.action(a["finding_id"]) is None          # the real move was never approved
    assert r["C8 residue"][0] == "FAIL" and world.decision_id(b["finding_id"]) in r["C8 residue"][1]
    # Cleanup still returned the car to service.
    g = world.groundings(world.vehicle_id_of(W.vin(3)))
    assert g and all(x.get("returnedAt") for x in g)


def test_grounded_probe_needs_the_grounding_sentence_not_any_409(world):
    world.defects["moves_groundings_unreadable"] = True   # CMS answers its other 409
    rc, out, err = world.run_main(world.argv(*FULL))
    r = rows(out)
    assert rc == 1 and r["C5 grounded move refused"][0] == "FAIL", out
    assert "Groundings couldn't be read" in r["C5 grounded move refused"][1]
    assert r["C5 return to service"][0] == "PASS"


def test_grounded_probe_needs_avx_to_record_the_refusal(world):
    world.defects["avx_cms_unreachable"] = True   # AVX records a failure, not CMS's refusal
    rc, out, err = world.run_main(world.argv(*FULL))
    r = rows(out)
    assert r["C5 grounded move refused"][0] == "FAIL" and "status 'failed'" in r["C5 grounded move refused"][1], r
    assert r["C5 return to service"][0] == "PASS"


def test_grounded_probe_needs_cms_409_in_avx_audit(world):
    world.defects["executor_400"] = True   # CMS refused the executor for another reason
    rc, out, err = world.run_main(world.argv(*FULL))
    r = rows(out)
    assert r["C5 grounded move refused"][0] == "FAIL" and "error 400" in r["C5 grounded move refused"][1], r


def test_grounding_that_does_not_read_back_open_stops_before_the_approve(world):
    b = finding_at(world, "loc-atl-airport")
    orig = world._cms_fi

    def fi(event: dict) -> dict:
        resp = orig(event)
        if event["httpMethod"] == "POST" and event["resource"].endswith("/groundings"):
            # The route answered 201 but the row closes at once (a racing return to service).
            for g in world.groundings(world.vehicle_id_of(W.vin(3))):
                world._res().Table(W.GROUNDINGS).update_item(
                    Key={"vehicleId": g["vehicleId"], "actionId": g["actionId"]},
                    UpdateExpression="SET returnedAt = :t REMOVE openFleetId",
                    ExpressionAttributeValues={":t": "2026-10-21T00:00:00Z"})
        return resp
    world.cms.routes = [(m, t, p, fi if fn == orig else fn) for m, t, p, fn in world.cms.routes]
    rc, out, err = world.run_main(world.argv(*FULL))
    r = rows(out)
    assert r["C5 grounded move refused"][0] == "FAIL" and "open" in r["C5 grounded move refused"][1]
    assert world.action(b["finding_id"]) is None    # the probe approve was never sent


# ---------------------------------------------------------------------------
# Return to service: retried, failed, recovered
# ---------------------------------------------------------------------------


def test_return_to_service_is_retried(world):
    world.defects["rts_failures"] = 1
    rc, out, err = world.run_main(world.argv(*FULL))
    r = rows(out)
    assert rc == 0, out
    assert r["C5 return to service"][0] == "PASS"
    # the depot probe's 403, then the cleanup's 503 and its retried 200
    assert [x["status"] for x in by_route(world, "POST", "/return-to-service")] == [403, 503, 200]


def test_return_to_service_failure_fails_stops_writes_and_names_the_recovery(world):
    world.defects["rts_failures"] = 99
    a = finding_at(world, "loc-aus-hub2")
    rc, out, err = world.run_main(world.argv(*FULL))
    r = rows(out)
    assert rc == 1
    assert r["C5 return to service"][0] == "FAIL" and W.vin(3) in r["C5 return to service"][1]
    assert r["C6 move approved"][0] == "SKIP" and world.action(a["finding_id"]) is None
    run_id = manifest(world)["run_id"]
    assert f"--cleanup-only --run-id {run_id}" in err, err
    assert r["C8 residue"][0] == "FAIL" and W.vin(3) in r["C8 residue"][1]
    assert [g for g in world.groundings(world.vehicle_id_of(W.vin(3))) if not g.get("returnedAt")]


def test_return_to_service_is_judged_by_the_read_back_not_the_status(world):
    world.defects["rts_lies"] = True
    rc, out, err = world.run_main(world.argv(*FULL))
    r = rows(out)
    assert rc == 1 and r["C5 return to service"][0] == "FAIL", out
    assert r["C6 move approved"][0] == "SKIP"


def test_a_signal_during_return_to_service_lets_it_finish(world):
    import os
    import signal
    real = world.mod._post_json

    def post(url: str, *a: Any, **k: Any) -> Any:
        out = real(url, *a, **k)
        # The rental operator's return to service (200), not C5's depot probe of the same
        # route (403), which runs earlier while the car is grounded and outside the hold.
        if url.endswith("/return-to-service") and out[0] == 200:
            os.kill(os.getpid(), signal.SIGTERM)
        return out
    world.mod._post_json = post
    with pytest.raises(SystemExit) as ei:
        world.run_main(world.argv(*FULL))
    assert ei.value.code == 143
    assert manifest(world)["grounding"]["returned"] is True
    assert all(g.get("returnedAt") for g in world.groundings(world.vehicle_id_of(W.vin(3))))
    assert not world.pool.users


def test_cleanup_only_returns_the_test_car_to_service(world):
    world.defects["rts_failures"] = 99
    world.run_main(world.argv(*FULL))
    run_id = manifest(world)["run_id"]
    world.defects["rts_failures"] = 0
    rc, out, err = world.run_main(world.argv(*FULL, "--cleanup-only", "--run-id", run_id))
    r = rows(out)
    assert rc == 0 and r["X1 cleanup"][0] == "PASS", (out, err)
    assert all(g.get("returnedAt") for g in world.groundings(world.vehicle_id_of(W.vin(3))))
    assert manifest(world)["grounding"]["returned"] is True
    assert not world.pool.users


@pytest.mark.parametrize("exc", [RuntimeError("world: the approve call blew up"), SystemExit(143)])
def test_an_exception_after_the_grounding_still_returns_the_car(world, exc):
    b = finding_at(world, "loc-atl-airport")
    real = world.mod._post_json

    def post(url: str, *a: Any, **k: Any) -> Any:
        if f"/actions/{b['finding_id']}/approve" in url:
            raise exc
        return real(url, *a, **k)
    world.mod._post_json = post
    try:
        rc, out, err = world.run_main(world.argv(*FULL))
    except SystemExit as e:
        rc, out = e.code, ""
    if isinstance(exc, SystemExit):
        assert rc == 143
    else:
        assert rows(out)["C5 grounded move refused"][0] == "FAIL"
        assert rows(out)["C5 return to service"][0] == "PASS"
    g = world.groundings(world.vehicle_id_of(W.vin(3)))
    assert g and all(x.get("returnedAt") for x in g), g
    assert world.action(b["finding_id"]) is None
    assert not world.pool.users


# ---------------------------------------------------------------------------
# Write windows and the flags that gate writes (each pinned in main())
# ---------------------------------------------------------------------------


def _at(w: W.CmsWorld, hhmm: str) -> None:
    h, m = map(int, hhmm.split(":"))
    t = dt.datetime.now(dt.timezone.utc).replace(hour=h, minute=m, second=0, microsecond=0)
    w.mod._now = lambda: t


@pytest.mark.parametrize("hhmm,open_", [("03:44", True), ("03:45", False), ("05:00", False),
                                         ("07:15", False), ("07:16", True)])
def test_write_window_edges(world, hhmm, open_):
    _at(world, hhmm)
    assert world.mod._cms_writes_allowed_now() is open_


def test_inside_the_window_nothing_is_written(world):
    _at(world, "05:00")
    rc, out, err = world.run_main(world.argv(*FULL))
    assert rc == 2 and "REFUSED" in err, (out, err)
    assert not world.pool.users and not world.pool.deleted
    assert not world.cms.requests


def test_the_window_is_rechecked_before_each_write_item(world):
    a = finding_at(world, "loc-aus-hub2")
    safe = world.mod._now()
    late = safe.replace(hour=5)
    world.mod._now = lambda: late if world.groundings(world.vehicle_id_of(W.vin(3))) else safe
    rc, out, err = world.run_main(world.argv(*FULL))
    r = rows(out)
    assert r["C6 move approved"][0] == "SKIP" and "window" in r["C6 move approved"][1]
    assert world.action(a["finding_id"]) is None


def test_without_allow_writes_no_user_and_no_request(world):
    rc, out, err = world.run_main(world.argv())
    r = rows(out)
    assert rc == 2
    assert r["C0 candidates"][0] == "PASS"
    for name in ("C1 rental reads", "C4 isolation reads", "C5 grounded move refused",
                 "C6 move approved"):
        assert r[name][0] == "SKIP", (name, r[name])
    assert not world.pool.users and not world.pool.deleted and not world.cms.requests


def test_allow_writes_without_approve_move_reads_only(world):
    rc, out, err = world.run_main(world.argv("--allow-writes"))
    r = rows(out)
    assert rc == 2
    for name in ("C1 rental reads", "C2 depot reads", "C3 latency", "C4 isolation reads"):
        assert r[name][0] == "PASS", (name, r[name], out)
    for name in ("C5 grounded move refused", "C6 move approved", "C7 isolation writes"):
        assert r[name][0] == "SKIP" and "--approve-move" in r[name][1], (name, r[name])
    assert not world.cms.sent("POST") and not world.pool.users


@pytest.mark.parametrize("bad", ["http://127.0.0.1:1/prod", "https://u:p@example.com/prod",
                                 "https:///prod", "https://example.com/prod?x=1"])
def test_endpoint_must_be_https_to_a_named_host(world, bad):
    argv = world.argv(*FULL)
    argv[argv.index("--cms-api-endpoint") + 1] = bad
    rc, out, err = world.run_main(argv)
    assert rc == 2 and "REFUSED" in err and not world.pool.users and not world.cms.requests


@pytest.mark.parametrize("extra", [("--cleanup-only", "--run-id", "../escape"),
                                   ("--cleanup-only", "--run-id", "x" * 80),
                                   ("--run-id", "../escape"), ("--cleanup-only",),
                                   ("--run-id", "20261021T170000Z-abc123")])
def test_bad_run_id_or_cleanup_without_one_is_refused_before_any_call(world, extra):
    # A readable manifest outside the state directory, so only the shape check can refuse.
    (world.tmp / "escape.json").write_text(json.dumps(
        {"run_id": "../escape", "stage": W.STAGE, "part": "cms"}))
    # And one inside it for the well-shaped id, so only "--run-id needs --cleanup-only" can.
    state = world.tmp / "state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "20261021T170000Z-abc123.json").write_text(json.dumps(
        {"run_id": "20261021T170000Z-abc123", "stage": W.STAGE, "part": "cms"}))
    rc, out, err = world.run_main(world.argv(*FULL, *extra))
    assert rc == 2 and "REFUSED" in err and not world.pool.users and not world.cms.requests


# ---------------------------------------------------------------------------
# Latency
# ---------------------------------------------------------------------------


class _Time:
    def __init__(self) -> None:
        import time as t
        self._t, self.offset = t, 0.0

    def time(self) -> float:
        return self._t.time() + self.offset

    def monotonic(self) -> float:
        return self.offset   # the fake clock alone, so a request's elapsed time is exact

    def perf_counter(self) -> float:
        return self._t.perf_counter() + self.offset

    def sleep(self, s: float) -> None:
        self.offset += s


@pytest.mark.parametrize("slow,secs,status", [(2, 3.5, "FAIL"), (1, 3.5, "PASS"),
                                              (20, 3.0, "FAIL"), (20, 2.9, "PASS")])
def test_route_p95_over_three_seconds_fails(world, slow, secs, status):
    world.fake_time = _Time()
    world.mod.time = world.fake_time
    # C1 reads /locations once before C3's 20 samples; `slow` of the 20 take `secs` longer.
    world.defects["delay"]["/api/v1/fleet-intelligence/locations"] = (
        [0.0] + [secs] * slow + [0.0] * (20 - slow))
    rc, out, err = world.run_main(world.argv("--allow-writes"))
    c3 = rows(out)["C3 latency"]
    assert c3[0] == status, c3
    if status == "FAIL":
        assert "locations" in c3[1]


# ---------------------------------------------------------------------------
# --part cms-next: the next day's bookings serve the moved cars at the destination
# ---------------------------------------------------------------------------


class BookingsAthena:
    """Athena for the next-day reads: bookings_runs, vehicle_days, booking_requests."""

    def __init__(self, settled: str, vehicle_days: list[dict], bookings: list[dict]) -> None:
        self.settled, self.vd, self.br = settled, vehicle_days, bookings
        self.queries: list[str] = []
        self._r: dict[str, list[list[str | None]]] = {}

    def start(self, p: dict) -> dict:
        sql = p["QueryString"]
        self.queries.append(sql)
        if ".bookings_runs" in sql:
            res = [["d"], [self.settled]]
        elif ".vehicle_days" in sql:
            res = [["vin", "location_id"]] + [[r["vin"], r["location_id"]] for r in self.vd]
        elif ".booking_requests" in sql:
            res = [["vin", "location_id", "outcome", "start_day"]] + [
                [r["vin"], r["location_id"], r["outcome"], r["start_day"]] for r in self.br]
        else:
            raise W.AwsError("InvalidRequestException", f"unexpected {sql[:60]}")
        qid = str(len(self.queries))
        self._r[qid] = res
        return {"QueryExecutionId": qid}

    def get(self, p: dict) -> dict:
        return {"QueryExecution": {"Status": {"State": "SUCCEEDED"}}}

    def results(self, p: dict) -> dict:
        rows_ = self._r[p["QueryExecutionId"]]
        return {"ResultSet": {"Rows": [{"Data": [{"VarCharValue": v} for v in r]} for r in rows_]}}


@pytest.fixture()
def moved(world):
    rc, out, err = world.run_main(world.argv(*FULL))
    assert rc == 0, (out, err)
    return world, manifest(world)


def _next(w: W.CmsWorld, m: dict) -> tuple[int, dict, str]:
    rc, out, err = w.run_main(["--part", "cms-next", "--stage", W.STAGE, "--workgroup", "wg",
                               "--run-id", m["run_id"]])
    return rc, rows(out), err


def _good(m: dict) -> tuple[list[dict], list[dict]]:
    eff, to = m["move"]["effective_date"], m["move"]["to"]
    vd = [{"vin": v, "location_id": to} for v in m["move"]["vins"]]
    br = [{"vin": m["move"]["vins"][0], "location_id": to, "outcome": "fulfilled", "start_day": eff}]
    return vd, br


def test_next_day_before_the_effective_day_is_settled_skips(moved):
    w, m = moved
    prev = (dt.date.fromisoformat(m["move"]["effective_date"]) - dt.timedelta(days=1)).isoformat()
    w.athena = BookingsAthena(prev, *_good(m))
    rc, r, err = _next(w, m)
    assert rc == 2 and r["N1 settled"][0] == "SKIP", r


def test_next_day_passes_when_the_cars_serve_the_destination(moved):
    w, m = moved
    w.athena = BookingsAthena(m["move"]["effective_date"], *_good(m))
    rc, r, err = _next(w, m)
    assert rc == 0, (r, err)
    assert all(r[n][0] == "PASS" for n in ("N1 settled", "N2 vehicle days", "N3 bookings")), r
    assert all(q.lstrip().startswith("SELECT ") for q in w.athena.queries)
    assert any(m["move"]["vins"][0] in q for q in w.athena.queries)


def test_next_day_fails_on_a_booking_at_the_origin(moved):
    w, m = moved
    vd, br = _good(m)
    br.append({"vin": m["move"]["vins"][1], "location_id": m["move"]["from"],
               "outcome": "fulfilled", "start_day": m["move"]["effective_date"]})
    w.athena = BookingsAthena(m["move"]["effective_date"], vd, br)
    rc, r, err = _next(w, m)
    assert rc == 1 and r["N3 bookings"][0] == "FAIL" and m["move"]["vins"][1] in r["N3 bookings"][1]


def test_next_day_fails_on_a_vehicle_day_at_the_origin(moved):
    w, m = moved
    vd, br = _good(m)
    vd[0] = {"vin": vd[0]["vin"], "location_id": m["move"]["from"]}
    w.athena = BookingsAthena(m["move"]["effective_date"], vd, br)
    rc, r, err = _next(w, m)
    assert rc == 1 and r["N2 vehicle days"][0] == "FAIL" and vd[0]["vin"] in r["N2 vehicle days"][1]


def test_next_day_fails_on_a_missing_vehicle_day(moved):
    w, m = moved
    vd, br = _good(m)
    w.athena = BookingsAthena(m["move"]["effective_date"], vd[:1], br)
    rc, r, err = _next(w, m)
    assert rc == 1 and r["N2 vehicle days"][0] == "FAIL"


def test_next_day_with_no_booking_served_skips(moved):
    w, m = moved
    vd, _ = _good(m)
    w.athena = BookingsAthena(m["move"]["effective_date"], vd, [])
    rc, r, err = _next(w, m)
    assert rc == 2 and r["N3 bookings"][0] == "SKIP", r


def test_next_day_refuses_a_tampered_manifest_before_any_query(moved):
    w, m = moved
    files = sorted((w.tmp / "state").glob("*.json"))
    m["move"]["vins"][0] = "X' OR '1'='1"
    files[0].write_text(json.dumps(m))
    w.athena = BookingsAthena(m["move"]["effective_date"], [], [])
    rc, r, err = _next(w, m)
    assert rc == 2 and "REFUSED" in err and not w.athena.queries


# ---------------------------------------------------------------------------
# Review cycle 1 (review-g75-cycle1.md): each check made to fail on its own
# ---------------------------------------------------------------------------


def _after_first_rental_read(w: W.CmsWorld, fn: Any) -> None:
    """Run *fn* once, just after C0 (the first rental read is C1's)."""
    orig, done = w._cms_fi, []

    def fi(event: dict) -> dict:
        if not done and event["httpMethod"] == "GET":
            done.append(True)
            fn()
        return orig(event)
    w._cms_fi = fi
    w.cms.routes = [(m, t, p, fi if f == orig else f) for m, t, p, f in w.cms.routes]


def test_c6_refuses_a_finding_decided_after_c0(world):
    a = finding_at(world, "loc-aus-hub2")
    _after_first_rental_read(world, lambda: world._res().Table(W.ACTIONS).put_item(Item={
        "action_id": world.decision_id(a["finding_id"]), "finding_id": a["finding_id"],
        "decision": "declined", "status": "declined"}))
    rc, out, err = world.run_main(world.argv(*FULL))
    r = rows(out)
    assert r["C6 move approved"][0] == "FAIL" and "decided before" in r["C6 move approved"][1], r
    assert not [x for x in by_route(world, "POST", "/actions/{findingId}/approve")
                if a["finding_id"] in x["path"]]


def test_c6_needs_avx_to_execute_the_move(world):
    a = finding_at(world, "loc-aus-hub2")
    world.defects["avx_cms_unreachable_for"] = a["finding_id"]
    rc, out, err = world.run_main(world.argv(*FULL))
    c6 = rows(out)["C6 move approved"]
    assert c6[0] == "FAIL" and "status 'failed'" in c6[1], c6


def test_c6_needs_every_car_at_the_destination(world):
    a = finding_at(world, "loc-aus-hub2")

    def undo(fid: str) -> None:   # a racing writer puts the cars back
        if fid == a["finding_id"]:
            for v in vins_of(a):
                world._res().Table(W.VEHICLES).update_item(
                    Key={"vehicleId": world.vehicle_id_of(v)},
                    UpdateExpression="SET locationId = :l", ExpressionAttributeValues={":l": "loc-aus-downtown"})
    world.defects["after_approve"] = undo
    rc, out, err = world.run_main(world.argv(*FULL))
    c6 = rows(out)["C6 move approved"]
    assert c6[0] == "FAIL" and "is at loc-aus-downtown, not loc-aus-hub2" in c6[1], c6


@pytest.mark.parametrize("field, bad", [("decidedByKind", "agent"), ("vins", "drop-one"),
                                        ("fromLocationId", "loc-aus-hub1"),
                                        ("toLocationId", "loc-aus-downtown"),
                                        ("effectiveDate", "2099-01-01")])
def test_c6_needs_the_move_record_as_cms_writes_it(world, field, bad):
    a = finding_at(world, "loc-aus-hub2")
    value = vins_of(a)[:1] if bad == "drop-one" else bad

    def skew(fid: str) -> None:
        if fid == a["finding_id"]:
            world._res().Table(W.MOVES).update_item(
                Key={"fleetId": W.RENTAL, "moveId": world.decision_id(fid)},
                UpdateExpression="SET #f = :v", ExpressionAttributeNames={"#f": field},
                ExpressionAttributeValues={":v": value})
    world.defects["after_approve"] = skew
    rc, out, err = world.run_main(world.argv(*FULL))
    c6 = rows(out)["C6 move approved"]
    assert c6[0] == "FAIL" and "move record differs" in c6[1], c6


@pytest.mark.parametrize("field, bad", [("decision", "overridden"),
                                        ("targetRef", "AVX-ACT-00000000-0000-0000-0000-000000000000")])
def test_c6_needs_the_approve_to_answer_approved_for_this_move(world, field, bad):
    a = finding_at(world, "loc-aus-hub2")
    real = world.mod._post_json

    def post(url: str, *a_: Any, **k: Any) -> Any:
        code, body, secs = real(url, *a_, **k)
        if url.endswith(f"/actions/{a['finding_id']}/approve") and isinstance(body, dict) \
                and isinstance(body.get("decision"), dict):
            body = {**body, "decision": {**body["decision"], field: bad}}
        return code, body, secs
    world.mod._post_json = post
    rc, out, err = world.run_main(world.argv(*FULL))
    c6 = rows(out)["C6 move approved"]
    assert c6[0] == "FAIL" and f"{field} {bad!r}" in c6[1], c6


def test_c6_needs_get_moves_to_list_the_move(world):
    world.defects["moves_list_drops"] = True
    rc, out, err = world.run_main(world.argv(*FULL))
    c6 = rows(out)["C6 move approved"]
    assert c6[0] == "FAIL" and "does not list" in c6[1], c6


def test_c5_refuses_a_car_grounded_after_c0_and_leaves_that_grounding(world):
    vid = world.vehicle_id_of(W.vin(3))
    real = {"vehicleId": vid, "actionId": "AVX-ACT-real", "vin": W.vin(3), "fleetId": W.RENTAL,
            "openFleetId": W.RENTAL, "campaignId": "MRD-2026-001", "findingId": "F",
            "approverSub": "s", "groundedAt": "2026-10-21T00:00:00Z"}
    _after_first_rental_read(world, lambda: world._res().Table(W.GROUNDINGS).put_item(Item=real))
    rc, out, err = world.run_main(world.argv(*FULL))
    r = rows(out)
    assert r["C5 grounded move refused"][0] == "FAIL" and "already grounded" in r["C5 grounded move refused"][1]
    g = world.groundings(vid)
    assert [x["actionId"] for x in g] == ["AVX-ACT-real"] and not g[0].get("returnedAt"), g


@pytest.mark.parametrize("rts_open", [False, "rts"])
def test_return_to_service_refuses_when_another_grounding_opened(world, rts_open):
    # With CMS's return-to-service gate failing open ("rts"), the depot probe would close the
    # real grounding too, so it must not be sent while another grounding is open (S1).
    world.defects["groundings_scope_open"] = rts_open
    b = finding_at(world, "loc-atl-airport")
    vid = world.vehicle_id_of(W.vin(3))

    def other(fid: str) -> None:
        if fid == b["finding_id"]:
            world._res().Table(W.GROUNDINGS).put_item(Item={
                "vehicleId": vid, "actionId": "AVX-ACT-real", "vin": W.vin(3), "fleetId": W.RENTAL,
                "openFleetId": W.RENTAL, "campaignId": "MRD-2026-001", "findingId": "F",
                "approverSub": "s", "groundedAt": "2026-10-21T00:00:00Z"})
    world.defects["after_approve"] = other
    rc, out, err = world.run_main(world.argv(*FULL))
    r = rows(out)
    assert r["C5 return to service"][0] == "FAIL" and "another open grounding" in r["C5 return to service"][1]
    assert not [x for x in world.groundings(vid) if x["actionId"] == "AVX-ACT-real"][0].get("returnedAt")
    assert r["C6 move approved"][0] == "SKIP"
    assert r["C5 isolation groundings"][0] == "SKIP" and "AVX-ACT-real" in r["C5 isolation groundings"][1]
    assert not by_route(world, "POST", "/return-to-service")


def test_an_unknown_grounding_outcome_fails_and_cleanup_reads_the_row(world):
    world.defects["grounding_times_out"] = True
    rc, out, err = world.run_main(world.argv(*FULL))
    r = rows(out)
    assert rc == 1 and r["C5 return to service"][0] == "FAIL" and "unknown" in r["C5 return to service"][1]
    assert r["C6 move approved"][0] == "SKIP" and "--cleanup-only" in err
    m = manifest(world)
    assert m["grounding"]["returned"] is False
    # The write lands after the run: --cleanup-only reads the row and returns the car.
    vid = world.vehicle_id_of(W.vin(3))
    world._res().Table(W.GROUNDINGS).put_item(Item={
        "vehicleId": vid, "actionId": m["grounding"]["action_id"], "vin": W.vin(3),
        "fleetId": W.RENTAL, "openFleetId": W.RENTAL, "campaignId": "MRD-9999-975",
        "findingId": m["grounding"]["action_id"], "approverSub": "s",
        "groundedAt": "2026-10-21T00:00:00Z"})
    world.defects["grounding_times_out"] = False
    rc, out, err = world.run_main(world.argv(*FULL, "--cleanup-only", "--run-id", m["run_id"]))
    assert rc == 0 and rows(out)["X1 cleanup"][0] == "PASS", out
    assert all(g.get("returnedAt") for g in world.groundings(vid))


def test_cleanup_only_reads_the_row_not_the_manifest_flag(world):
    world.run_main(world.argv(*FULL))
    m = manifest(world)
    vid = m["grounding"]["vehicle_id"]
    world._res().Table(W.GROUNDINGS).update_item(   # re-opened after the run said returned
        Key={"vehicleId": vid, "actionId": m["grounding"]["action_id"]},
        UpdateExpression="SET openFleetId = :f REMOVE returnedAt", ExpressionAttributeValues={":f": W.RENTAL})
    rc, out, err = world.run_main(world.argv(*FULL, "--cleanup-only", "--run-id", m["run_id"]))
    assert rc == 0 and "returned at" in rows(out)["X1 cleanup"][1], out
    assert all(g.get("returnedAt") for g in world.groundings(vid))


def test_cleanup_only_refuses_a_manifest_that_fails_its_shape(world):
    world.defects["rts_failures"] = 99
    world.run_main(world.argv(*FULL))
    files = sorted((world.tmp / "state").glob("*.json"))
    m = json.loads(files[0].read_text())
    m["grounding"]["vehicle_id"] = "VEH-RNT-0003/../../x"
    files[0].write_text(json.dumps(m))
    world.defects["rts_failures"] = 0
    before = len(by_route(world, "POST", "/return-to-service"))
    rc, out, err = world.run_main(world.argv(*FULL, "--cleanup-only", "--run-id", m["run_id"]))
    assert rc == 1 and "shape" in rows(out)["X1 cleanup"][1]
    assert len(by_route(world, "POST", "/return-to-service")) == before   # nothing sent


@pytest.mark.parametrize("scope_open, said", [
    (True, "grounding as demo-lastmile-delivery answered 200"),
    ("rts", "return to service as demo-lastmile-delivery answered 200")])
def test_c5_isolation_on_the_groundings_routes(world, scope_open, said):
    world.defects["groundings_scope_open"] = scope_open
    a = finding_at(world, "loc-aus-hub2")
    rc, out, err = world.run_main(world.argv(*FULL))
    r = rows(out)
    assert r["C5 isolation groundings"][0] == "FAIL", r
    assert said in r["C5 isolation groundings"][1], r["C5 isolation groundings"]
    # One run, one move: a fail-open here stops the move (cycle 2 W1).
    assert r["C6 move approved"][0] == "SKIP" and world.action(a["finding_id"]) is None


def test_c1_fails_on_an_unavailable_rental_view(world):
    s3 = world.session.client("s3", region_name=W.ADP_REGION)
    s3.delete_object(Bucket=W.CMS_RESULTS, Key="fleet-intelligence/_cache/fi-rebalance-v1/manifest.json")
    rc, out, err = world.run_main(world.argv("--allow-writes"))
    c1 = rows(out)["C1 rental reads"]
    assert c1[0] == "FAIL" and "unavailable" in c1[1], c1


def test_c1_fails_on_a_stale_cache(world, monkeypatch):
    monkeypatch.setattr(world.ix.rebalance_source, "_cache_age_hours", lambda c: 30.0)
    rc, out, err = world.run_main(world.argv("--allow-writes"))
    c1 = rows(out)["C1 rental reads"]
    assert c1[0] == "FAIL" and "cache age 30.0" in c1[1], c1
    c2 = rows(out)["C2 depot reads"]
    assert c2[0] == "FAIL", c2  # W1: 26 h cache bound applies to the depot path too


def test_c2_fails_when_demand_has_non_null_rows(world, monkeypatch):
    """C2 fails when demand/gaps carries a non-null figure (e.g. rows: [] instead of null)."""
    real = world.ix.rebalance_source.depot_response
    monkeypatch.setattr(world.ix.rebalance_source, "depot_response",
                        lambda route, fid: {**real(route, fid), "rows": []})
    rc, out, err = world.run_main(world.argv("--allow-writes"))
    assert rows(out)["C2 depot reads"][0] == "FAIL"


def test_c7_needs_the_rental_controls(world):
    world.defects["controls_fail_after_move"] = True
    rc, out, err = world.run_main(world.argv(*FULL))
    c7 = rows(out)["C7 isolation writes"]
    assert c7[0] == "FAIL" and "control" in c7[1], c7


def test_c7_compares_the_action_after_the_probes(world):
    world.defects["probe_writes_action"] = True
    rc, out, err = world.run_main(world.argv(*FULL))
    c7 = rows(out)["C7 isolation writes"]
    assert c7[0] == "FAIL" and "changed" in c7[1], c7


def test_next_day_refuses_a_tampered_effective_date(moved):
    w, m = moved
    files = sorted((w.tmp / "state").glob("*.json"))
    m["move"]["effective_date"] = "2026-10-22' OR 1=1 --"
    files[0].write_text(json.dumps(m))
    w.athena = BookingsAthena("2026-10-22", [], [])
    rc, r, err = _next(w, m)
    assert rc == 2 and "REFUSED" in err and not w.athena.queries


def test_next_day_bookings_are_bounded_to_the_effective_day(moved):
    w, m = moved
    w.athena = BookingsAthena(m["move"]["effective_date"], *_good(m))
    _next(w, m)
    eff = dt.date.fromisoformat(m["move"]["effective_date"])
    q = next(x for x in w.athena.queries if ".booking_requests" in x)
    assert f"start_at >= TIMESTAMP '{eff} 00:00:00'" in q
    assert f"start_at < TIMESTAMP '{eff + dt.timedelta(days=1)} 00:00:00'" in q


def test_decision_id_equals_avx_helper():
    from agents.supervisor.tools.fleet_recommendation import _derive_decision_action_id
    import importlib.util
    spec = importlib.util.spec_from_file_location("lv_ids", W.REPO / "scripts" / "live-verify-fi-rebalancing.py")
    lv = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(lv)
    for fid in ("FIND-8ad427e2-6dca-440e-81d4-684705312ebb", "FIND-x", "f" * 128):
        assert lv._decision_id(fid) == _derive_decision_action_id(fid)


def test_cms_guard_admits_temp_users_only(monkeypatch):
    import importlib.util
    import botocore.exceptions as bce
    spec = importlib.util.spec_from_file_location("lv_guard", W.REPO / "scripts" / "live-verify-fi-rebalancing.py")
    lv = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(lv)
    import boto3
    from botocore.config import Config
    sess = boto3.Session(botocore_session=lv._make_guarded_bcsession(True, "staging", "wg", "",
                                                                       cms_only=True),
                         aws_access_key_id="x", aws_secret_access_key="y", region_name="us-west-2")
    cfg = Config(retries={"total_max_attempts": 1}, connect_timeout=1)
    kw = {"endpoint_url": "http://127.0.0.1:1", "config": cfg}

    def raised(fn: Any) -> BaseException:
        try:
            fn()
        except BaseException as exc:  # noqa: BLE001
            return exc
        raise AssertionError("no exception")
    ssm, lam, idp, ddb = (sess.client(s, **kw) for s in ("ssm", "lambda", "cognito-idp", "dynamodb"))
    for call in (lambda: ssm.put_parameter(Name="/vsa/staging/avx/fi-rebalancing/policy", Value="{}",
                                           Type="String", Overwrite=True),
                 lambda: lam.invoke(FunctionName="", InvocationType="RequestResponse",
                                    Payload=b'{"component": "rebalance"}'),
                 lambda: ddb.put_item(TableName="t", Item={"k": {"S": "v"}}),
                 lambda: idp.admin_create_user(UserPoolId="p", Username="someone@example.com")):
        exc = raised(call)
        assert type(exc).__name__ == "GuardViolation", exc
    exc = raised(lambda: idp.admin_create_user(UserPoolId="p", Username="livetest-fi-rebal-g4-1@example.invalid"))
    assert isinstance(exc, bce.EndpointConnectionError), exc


def test_a_c0_failure_after_the_move_is_chosen_stops_every_write(world):
    a = finding_at(world, "loc-aus-hub2")

    def boom(*a_: Any, **k: Any) -> Any:
        raise RuntimeError("world: recall views unreadable")
    world.mod._recall_clear = boom      # raises in C0's probe loop, after run.move is set
    rc, out, err = world.run_main(world.argv(*FULL))
    r = rows(out)
    assert r["C0 candidates"][0] == "FAIL", r
    assert r["C6 move approved"][0] == "SKIP" and world.action(a["finding_id"]) is None


def test_the_cms_part_session_refuses_the_agent_parts_writes(world):
    world.run_main(world.argv("--allow-writes"))
    ssm = world.mod._client("ssm", W.REGION)
    try:
        ssm.put_parameter(Name=W.POLICY_PARAM, Value="{}", Type="String", Overwrite=True)
    except Exception as exc:  # noqa: BLE001
        assert type(exc).__name__ == "GuardViolation", exc
    else:
        raise AssertionError("the cms part's session let the policy write through")


# ---------------------------------------------------------------------------
# Battery 2's fix-mutant survivors (each test is the only one that sees its property)
# ---------------------------------------------------------------------------


def test_a_c0_failure_after_both_candidates_stops_every_write(world):
    # The manifest save at C0's end fails once (a full disk), after the move and the probe
    # are both chosen: with only the probe gate, C5 would ground the car and C6 would move.
    a, b = finding_at(world, "loc-aus-hub2"), finding_at(world, "loc-atl-airport")
    real, once = world.mod._save_manifest, {"done": False}

    def save(m: dict) -> None:
        if m.get("candidates") and not once["done"]:
            once["done"] = True
            raise OSError("world: no space left on device")
        real(m)
    world.mod._save_manifest = save
    rc, out, err = world.run_main(world.argv(*FULL))
    r = rows(out)
    assert r["C0 candidates"][0] == "FAIL", r
    assert r["C5 grounded move refused"][0] == "SKIP", r["C5 grounded move refused"]
    assert not world.groundings(world.vehicle_id_of(W.vin(3)))
    assert r["C6 move approved"][0] == "SKIP"
    assert world.action(a["finding_id"]) is None and world.action(b["finding_id"]) is None


def test_no_direct_move_post_unless_the_screen_showed_the_refusal(world):
    # AVX can't reach CMS for the probe's approve, and CMS's grounding check is broken: the
    # direct POST /moves would be the one request that moves the grounded car.
    b = finding_at(world, "loc-atl-airport")
    world.defects["avx_cms_unreachable_for"] = b["finding_id"]
    world.defects["moves_ignore_groundings"] = True
    rc, out, err = world.run_main(world.argv(*FULL))
    r = rows(out)
    assert rc == 1 and r["C5 grounded move refused"][0] == "FAIL", out
    assert not [x for x in by_route(world, "POST", "/moves") if W.vin(3) in (x["body"] or "")]
    assert world.move(world.decision_id(b["finding_id"])) is None
    assert world.vehicle(world.vehicle_id_of(W.vin(3)))["locationId"] == "loc-atl-downtown"


def test_c5_isolation_on_the_groundings_needs_the_rental_control(world):
    real, n = world.mod._post_json, {"groundings": 0}

    def post(url: str, *a: Any, **k: Any) -> Any:
        if url.endswith("/groundings"):
            n["groundings"] += 1
            if n["groundings"] == 2:     # the rental operator's repeat (the control)
                return 503, {"message": "world: unavailable"}, 0.0
        return real(url, *a, **k)
    world.mod._post_json = post
    rc, out, err = world.run_main(world.argv(*FULL))
    r = rows(out)
    assert r["C5 isolation groundings"][0] == "FAIL", r
    assert "control" in r["C5 isolation groundings"][1]


def test_c8_reads_the_grounding_itself_and_names_the_recovery_when_it_reopened(world):
    # C5 returned the car; the row is open again by C8 (CMS never reopens one, so this is the
    # independent final check C8 exists for). C8 alone sees it, says so, points to the
    # recovery and stops the manifest claiming the car was returned.
    a = finding_at(world, "loc-aus-hub2")
    vid, once = world.vehicle_id_of(W.vin(3)), {"done": False}

    def reopen(fid: str) -> None:
        if fid != a["finding_id"] or once["done"]:
            return
        once["done"] = True
        gid = manifest(world)["grounding"]["action_id"]
        world._res().Table(W.GROUNDINGS).update_item(
            Key={"vehicleId": vid, "actionId": gid},
            UpdateExpression="REMOVE returnedAt SET openFleetId = :f",
            ExpressionAttributeValues={":f": W.RENTAL})
    world.defects["after_approve"] = reopen
    rc, out, err = world.run_main(world.argv(*FULL))
    r = rows(out)
    assert r["C5 return to service"][0] == "PASS", r["C5 return to service"]
    assert rc == 1 and r["C8 residue"][0] == "FAIL" and W.vin(3) in r["C8 residue"][1], out
    assert f"--cleanup-only --run-id {manifest(world)['run_id']}" in err, err
    assert manifest(world)["grounding"]["returned"] is False


# ---------------------------------------------------------------------------
# Review cycle 2 (each fails on its own assertion without its fix)
# ---------------------------------------------------------------------------


def test_a_failed_read_check_stops_every_write(world, monkeypatch):
    # One run, one move: a FAIL in C1-C4 means fix and re-run, so nothing may be written first.
    real_dr = world.ix.rebalance_source.depot_response
    monkeypatch.setattr(world.ix.rebalance_source, "depot_response",
                        lambda route, fid: {**real_dr(route, fid),
                                           "depot_message": "No data yet"})  # C2 FAILs
    a = finding_at(world, "loc-aus-hub2")
    rc, out, err = world.run_main(world.argv(*FULL))
    r = rows(out)
    assert r["C2 depot reads"][0] == "FAIL", r
    c5 = r["C5 grounded move refused"]
    assert c5[0] == "SKIP" and "earlier item failed" in c5[1], c5
    iso = r.get("C5 isolation groundings")
    assert iso is not None and iso[0] == "SKIP", iso       # skip path names the row too (N1)
    assert not world.groundings(world.vehicle_id_of(W.vin(3)))
    assert r["C6 move approved"][0] == "SKIP" and world.action(a["finding_id"]) is None


def test_no_answer_from_the_grounding_route_is_an_unknown_outcome(world):
    # No answer (-1): the Lambda may still be writing, so a missing row is not "nothing written".
    real, n = world.mod._post_json, {"g": 0}

    def post(url: str, *a: Any, **k: Any) -> Any:
        if url.endswith("/groundings"):
            n["g"] += 1
            if n["g"] == 1:
                return -1, None, 0.0
        return real(url, *a, **k)
    world.mod._post_json = post
    rc, out, err = world.run_main(world.argv(*FULL))
    r = rows(out)
    rts = r["C5 return to service"]
    assert rc == 1 and rts[0] == "FAIL" and "unknown" in rts[1], rts
    assert manifest(world)["grounding"]["returned"] is False and "--cleanup-only" in err, err


def test_a_grounding_refused_before_the_approve_leaves_the_probe_undecided(world):
    b = finding_at(world, "loc-atl-airport")
    real, n = world.mod._post_json, {"g": 0}

    def post(url: str, *a: Any, **k: Any) -> Any:
        if url.endswith("/groundings"):
            n["g"] += 1
            if n["g"] == 1:
                return 400, {"error": "world: refused"}, 0.0
        return real(url, *a, **k)
    world.mod._post_json = post
    rc, out, err = world.run_main(world.argv(*FULL))
    r = rows(out)
    assert r["C5 grounded move refused"][0] == "FAIL", r
    iso = r.get("C5 isolation groundings")
    assert iso is not None and iso[0] == "SKIP", iso
    assert world.action(b["finding_id"]) is None
    c8 = r["C8 residue"]
    assert c8[0] == "PASS" and "left by design" not in c8[1], c8


def test_c6_refuses_a_car_moved_to_another_fleet_after_c0(world):
    a, b = finding_at(world, "loc-aus-hub2"), finding_at(world, "loc-atl-airport")
    moved = world.vehicle_id_of(vins_of(a)[0])

    def refleet(fid: str) -> None:
        if fid == b["finding_id"]:       # C5's approve, before C6
            world._res().Table(W.VEHICLES).update_item(
                Key={"vehicleId": moved}, UpdateExpression="SET fleetId = :f",
                ExpressionAttributeValues={":f": W.DELIVERY})
    world.defects["after_approve"] = refleet
    rc, out, err = world.run_main(world.argv(*FULL))
    c6 = rows(out)["C6 move approved"]
    assert c6[0] == "FAIL" and "no longer a single car" in c6[1], c6
    assert world.action(a["finding_id"]) is None


def test_cleanup_only_fails_cleanly_when_the_grounding_row_is_unreadable(world):
    world.run_main(world.argv(*FULL))
    m = manifest(world)

    def boom(*a: Any, **k: Any) -> Any:
        raise RuntimeError("world: groundings table unreadable")
    world.mod._read_grounding = boom
    try:
        rc, out, err = world.run_main(world.argv(*FULL, "--cleanup-only", "--run-id", m["run_id"]))
    except Exception as exc:  # noqa: BLE001
        raise AssertionError(f"--cleanup-only raised {type(exc).__name__}: {exc}") from exc
    x1 = rows(out)["X1 cleanup"]
    assert rc == 1 and x1[0] == "FAIL" and "unreadable" in x1[1], x1


def test_c8_names_the_recovery_even_when_the_manifest_save_fails(world):
    a = finding_at(world, "loc-aus-hub2")
    vid, once = world.vehicle_id_of(W.vin(3)), {"done": False}

    def reopen(fid: str) -> None:
        if fid != a["finding_id"] or once["done"]:
            return
        once["done"] = True
        gid = manifest(world)["grounding"]["action_id"]
        world._res().Table(W.GROUNDINGS).update_item(
            Key={"vehicleId": vid, "actionId": gid},
            UpdateExpression="REMOVE returnedAt SET openFleetId = :f",
            ExpressionAttributeValues={":f": W.RENTAL})

        def no_space(m: dict) -> None:
            raise OSError(28, "world: no space left on device")
        world.mod._save_manifest = no_space
    world.defects["after_approve"] = reopen
    rc, out, err = world.run_main(world.argv(*FULL))
    assert rc == 1 and rows(out)["C8 residue"][0] == "FAIL", out
    assert "--cleanup-only --run-id" in err and "is still grounded" in err, err
    c8 = rows(out)["C8 residue"][1]
    assert "manifest save failed: OSError" in c8 and "residue read failed" not in c8, c8


@pytest.mark.parametrize("returned", ["already", "by_cleanup"])
def test_cleanup_only_names_a_failed_manifest_save(world, returned):
    world.defects["rts_failures"] = 99 if returned == "by_cleanup" else 0
    world.run_main(world.argv(*FULL))
    m = manifest(world)
    world.defects["rts_failures"] = 0

    def no_space(_m: dict) -> None:
        raise OSError(28, "world: no space left on device")
    world.mod._save_manifest = no_space
    try:
        rc, out, err = world.run_main(world.argv(*FULL, "--cleanup-only", "--run-id", m["run_id"]))
    except Exception as exc:  # noqa: BLE001
        raise AssertionError(f"--cleanup-only raised {type(exc).__name__}: {exc}") from exc
    x1 = rows(out)["X1 cleanup"]
    assert rc == 1 and x1[0] == "FAIL" and "manifest was not saved" in x1[1], x1
    assert all(g.get("returnedAt") for g in world.groundings(world.vehicle_id_of(W.vin(3))))


# ---------------------------------------------------------------------------
# Security review cycle 1 (Suggestions taken)
# ---------------------------------------------------------------------------


def _guarded(cms_only: bool = True):
    import importlib.util
    import boto3
    from botocore.config import Config
    spec = importlib.util.spec_from_file_location("lv_guard_sec", W.REPO / "scripts" / "live-verify-fi-rebalancing.py")
    lv = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(lv)
    sess = boto3.Session(botocore_session=lv._make_guarded_bcsession(True, "staging", "wg", "",
                                                                       cms_only=cms_only),
                         aws_access_key_id="x", aws_secret_access_key="y", region_name="us-west-2")
    cfg = Config(retries={"total_max_attempts": 1}, connect_timeout=1)
    return lv, sess.client("cognito-idp", endpoint_url="http://127.0.0.1:1", config=cfg)


def _outcome(fn: Any) -> BaseException:
    try:
        fn()
    except BaseException as exc:  # noqa: BLE001
        return exc
    raise AssertionError("no exception")


@pytest.mark.parametrize("cms_only", [True, False])
def test_guard_admits_the_temp_users_only_as_permanent_fleet_operators(cms_only):
    import botocore.exceptions as bce
    _lv, idp = _guarded(cms_only)
    user = "livetest-fi-rebal-g4-1@example.invalid"
    for call in (lambda: idp.admin_add_user_to_group(UserPoolId="p", Username=user, GroupName="platform-admin"),
                 lambda: idp.admin_add_user_to_group(UserPoolId="p", Username=user, GroupName="fleet-operators"),
                 lambda: idp.admin_set_user_password(UserPoolId="p", Username=user, Password="Pw-12345678!x",
                                                     Permanent=False),
                 lambda: idp.admin_set_user_password(UserPoolId="p", Username=user, Password="Pw-12345678!x")):
        exc = _outcome(call)
        assert type(exc).__name__ == "GuardViolation", exc
    for call in (lambda: idp.admin_add_user_to_group(UserPoolId="p", Username=user, GroupName="fleet-operator"),
                 lambda: idp.admin_set_user_password(UserPoolId="p", Username=user, Password="Pw-12345678!x",
                                                     Permanent=True)):
        exc = _outcome(call)
        assert isinstance(exc, bce.EndpointConnectionError), exc


def test_endpoint_with_a_fragment_is_refused(world):
    argv = world.argv(*FULL)
    argv[argv.index("--cms-api-endpoint") + 1] = world.cms.url + "#x"
    rc, out, err = world.run_main(argv)
    assert rc == 2 and "REFUSED" in err and not world.pool.users and not world.cms.requests


def test_the_cleanup_pointer_never_raises(world, monkeypatch):
    class Broken:
        def write(self, *_a: Any) -> int:
            raise RuntimeError("world: stderr gone")

        def flush(self) -> None:
            raise RuntimeError("world: stderr gone")
    run = world.mod._CmsRun(stage=W.STAGE, run_id="20261021T170000Z-abc123", base=world.cms.url,
                            allow_writes=True, approve_move=True, manifest={})
    monkeypatch.setattr(world.mod.sys, "stderr", Broken())
    try:
        world.mod._cleanup_pointer(run, "a test car is still grounded")
    except Exception as exc:  # noqa: BLE001
        raise AssertionError(f"the pointer raised {type(exc).__name__}: {exc}") from exc


def test_an_oversized_recall_object_is_refused_without_reading_it_all(world):
    class Body:
        def __init__(self) -> None:
            self.asked: list[Any] = []

        def read(self, n: Any = None) -> bytes:
            self.asked.append(n)
            return b"x" * ((n if n is not None else 10 ** 9))
    body = Body()

    class S3:
        def get_object(self, **_k: Any) -> dict:
            return {"Body": body}
    try:
        world.mod._recall_clear(S3(), "b", W.vin(3), world.mod._wall())
    except ValueError as exc:
        assert "bytes" in str(exc), exc
    else:
        raise AssertionError("an oversized recall manifest was accepted")
    assert body.asked and all(isinstance(n, int) and n <= world.mod._RECALL_MAX_BYTES + 1
                              for n in body.asked), body.asked


def test_the_cms_next_session_is_cms_only(moved):
    w, m = moved
    w.athena = BookingsAthena(m["move"]["effective_date"], *_good(m))
    seen = {}
    real = w.mod._init_session

    def spy(**kw: Any) -> Any:
        seen.update(kw)
        return real(**kw)
    w.mod._init_session = spy
    rc, r, err = _next(w, m)
    assert seen.get("allow_writes") is False and seen.get("cms_only") is True, seen
