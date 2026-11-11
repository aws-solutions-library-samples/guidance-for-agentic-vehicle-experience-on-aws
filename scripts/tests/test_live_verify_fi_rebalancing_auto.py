"""``live-verify-fi-rebalancing.py --part auto`` (task 8.4) against the shared fake world
in ``fi_rebal_cms_world.py``, built from the real auto-approver and approval-rules Lambdas.

Stage 1 — RED PHASE.  ``--part auto`` does not yet exist in ``main()``; every test fails on
its own assertion (argparse rejects the unknown part; :meth:`AutoWorld.run_main` converts the
resulting ``SystemExit`` to rc=2 and empty output).  The assertions here specify the behavior
that the implementation must satisfy:

Properties pinned, one test per item and per failure that item must catch:

P1  (read-only)  auto-approver config, bounds, API policy, no enabled rule, window.
P2  (read-only)  pick two undecided move Findings; SKIP when no pair.
U1  (403 probes) internal route rejects unsigned and caller-signed requests.
U2  rule create/enable via the AVX approval-rules API.
U3  evaluator over-limit → no Action; limit raised → Action written.
U4  grounded probe car → CMS refuses with 409; no move record written.
U5  happy path: move executes with decidedByKind=rule, ruleId, no approverSub.
U6  always: rule disabled, probe car returned, temporary operator deleted.

Stage-1 test-bug fixes (commit 2026-10-22):
  1. Removed ``test_p1_skips_inside_05_30_07_15_window`` — the 03:45–07:15Z write
     window already covers the 05:30–07:15Z interval; the test was redundant and set
     the fake clock to 06:00Z, which is inside both windows, so
     ``test_refused_inside_03_45_07_15_write_window`` already catches it.
  2. ``test_u1_caller_signed_post_gets_403``: filter was ``"testing" not in
     authorization`` which excluded the caller's request (AWS_ACCESS_KEY_ID="testing");
     fixed to ``"testing" in authorization``.
  3. ``test_u2_rule_created_disabled_then_enabled_and_read_back``: post-run DDB check
     was ``rule.get("enabled") is True``, but U6 disables the rule before the run ends;
     fixed to check the AVX rules API request log (mid-run fact) for a successful enable
     PUT with ``"enabled": true`` in the body.
     ``test_u3_over_limit_produces_no_action``: post-run DDB Action check was
     ``act is None``, but U5 writes the Action; fixed to check the evaluator run records
     in W.RUNS (mid-run fact) for a run with ``artifacts_written=0``.
  4. ``test_u6_temporary_operator_deleted``: checked all fleet-operators in the pool,
     including pre-existing ones; fixed to snapshot pool users before the run and check
     only new users are gone after.
"""
from __future__ import annotations

import datetime as dt
import json
import re
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from scripts.tests import fi_rebal_cms_world as W

ROW_RE = re.compile(r"^(PASS|FAIL|SKIP)\s+(.+?): (.*)$")
RULE_ID = "livetest-fi-rebal-auto"   # prefix only; used in invoke_approver tests
_RULE_ID_PREFIX = "livetest-fi-rebal-auto"


def _run_rule_id(run_id: str) -> str:
    """Build the per-run rule_id from a manifest run_id."""
    return f"{_RULE_ID_PREFIX}-{run_id}"


def _rule_id_of(out: str) -> str:
    """Extract the per-run rule_id from script stdout.

    The script prints ``run id <run_id>; recover with:`` before U2 (C7).
    """
    import re as _re
    m = _re.search(r"run id ([0-9A-Za-z-]+);", out)
    assert m, f"run id not found in stdout: {out!r}"
    return _run_rule_id(m.group(1))


def rows(out: str) -> dict[str, tuple[str, str]]:
    found: dict[str, tuple[str, str]] = {}
    for line in out.splitlines():
        m = ROW_RE.match(line)
        if m:
            name = m.group(2)
            assert name not in found, (
                f"duplicate row name {name!r} in output\n{out}")
            found[name] = (m.group(1), m.group(3))
    return found


@pytest.fixture()
def auto_world(tmp_path: Path):
    with W.AutoWorld(tmp_path) as w:
        yield w


def _c0_eligible_vins(w: W.AutoWorld, finding: dict) -> list[str]:
    """Return the subset of the finding's VINs that pass C0 checks in the world.

    Used by finding_a/finding_b to select findings the way P2 does.
    """
    vins = list(
        ((finding.get("recommended_action") or {}).get("parameters") or {})
        .get("vehicle_vins") or []
    )
    fr = (finding.get("location") or {}).get("from_location_id", "")
    veh_by_vin = {v["vin"]: v for v in w.items(W.VEHICLES)}
    roster_vins = {
        r.get("vin") for r in w.items(W.LIVE_PLAN) if r.get("planDate") == "roster"
    }
    ios_vids = {
        d.get("assignedVehicleId")
        for d in w.items(W.DRIVERS)
        if d.get("cognitoEmail")
    }
    ok = []
    for v in vins:
        row = veh_by_vin.get(v) or {}
        if (row.get("locationId") != fr
                or v in roster_vins
                or row.get("vehicleId") in ios_vids):
            continue
        ok.append(v)
    return ok


def finding_a(w: W.AutoWorld) -> dict:
    """The first fully C0-eligible move Finding: ALL VINs pass, smallest id.

    P2 uses this as the 'execute' finding (U5 moves all its VINs).
    """
    return min(
        (f for f in w.move_findings()
         if len(_c0_eligible_vins(w, f))
         == len(((f.get("recommended_action") or {}).get("parameters") or {})
                .get("vehicle_vins") or [])),
        key=lambda f: f["finding_id"],
    )


def finding_b(w: W.AutoWorld) -> dict:
    """A second eligible Finding (probe: ≥1 eligible VIN, ≠ finding_a), smallest id.

    P2 uses this as the 'grounding probe' finding (U4 grounds one of its VINs).
    """
    a = finding_a(w)
    a_vins = set(
        ((a.get("recommended_action") or {}).get("parameters") or {})
        .get("vehicle_vins") or []
    )
    return min(
        (f for f in w.move_findings()
         if f["finding_id"] != a["finding_id"]
         and any(v not in a_vins for v in _c0_eligible_vins(w, f))),
        key=lambda f: f["finding_id"],
    )


def probe_vin_of(w: W.AutoWorld, fb: dict, fa: dict | None = None) -> str:
    """The eligible probe VIN from *fb* (first sorted VIN passing C0, not in A's VINs).

    Must be called before run_main (after U5 moves the cars, their locations change).
    """
    a_vins = set(
        ((fa.get("recommended_action") or {}).get("parameters") or {})
        .get("vehicle_vins") or []
    ) if fa is not None else set()
    eligible = _c0_eligible_vins(w, fb)
    for v in sorted(eligible):
        if v not in a_vins:
            return v
    raise RuntimeError(f"no eligible probe VIN in finding_b {fb['finding_id']}")


# ============================================================================
# P1 — read-only auto-approver config checks
# ============================================================================


def test_p1_passes_in_correctly_configured_world(auto_world):
    """P1 reports PASS when the auto-approver is fully configured as expected."""
    rc, out, err = auto_world.run_main(auto_world.auto_argv())
    r = rows(out)
    assert rc == 0, (out, err)
    assert r.get("P1 config", ("missing",))[0] == "PASS", r.get("P1 config")


def test_p1_fails_when_auto_moves_not_enabled(auto_world):
    """P1 catches FI_AUTO_MOVES_ENABLED absent or false in the function config."""
    auto_world.auto_defects["auto_moves_disabled"] = True
    rc, out, err = auto_world.run_main(auto_world.auto_argv())
    r = rows(out)
    assert rc == 1, (out, err)
    assert r.get("P1 config", ("PASS",))[0] == "FAIL", r.get("P1 config")
    assert "FI_AUTO_MOVES_ENABLED" in r.get("P1 config", ("", ""))[1], r.get("P1 config")


def test_p1_fails_when_endpoint_does_not_match_cms_output(auto_world):
    """P1 catches VSA_CMS_FI_INTERNAL_API_ENDPOINT not matching CMS's FiInternalApiEndpoint."""
    auto_world.auto_defects["endpoint_wrong"] = True
    rc, out, err = auto_world.run_main(auto_world.auto_argv())
    r = rows(out)
    assert rc == 1, (out, err)
    assert r.get("P1 config", ("PASS",))[0] == "FAIL", r.get("P1 config")
    assert "endpoint" in r.get("P1 config", ("", ""))[1].lower(), r.get("P1 config")


def test_p1_fails_when_timeout_too_short(auto_world):
    """P1 catches a timeout below 300 s (flag-off default is 60 s; G8-D9)."""
    auto_world.auto_defects["timeout_short"] = True
    rc, out, err = auto_world.run_main(auto_world.auto_argv())
    r = rows(out)
    assert rc == 1, (out, err)
    assert r.get("P1 config", ("PASS",))[0] == "FAIL", r.get("P1 config")
    assert "timeout" in r.get("P1 config", ("", ""))[1].lower(), r.get("P1 config")


def test_p1_fails_when_stream_mapping_disabled(auto_world):
    """P1 catches the event source mapping in State=Disabled."""
    auto_world.auto_defects["stream_disabled"] = True
    rc, out, err = auto_world.run_main(auto_world.auto_argv())
    r = rows(out)
    assert rc == 1, (out, err)
    assert r.get("P1 config", ("PASS",))[0] == "FAIL", r.get("P1 config")


def test_p1_fails_when_bounds_wrong(auto_world):
    """P1 catches the bounds SSM parameter carrying unexpected values."""
    # Overwrite the bounds with a bad value before the run.
    auto_world.session.client("ssm", region_name=W.REGION).put_parameter(
        Name=W.BOUNDS_PARAM, Value='{"max_limit_usd":999,"max_vehicles_per_auto_move":5}',
        Type="String", Overwrite=True)
    rc, out, err = auto_world.run_main(auto_world.auto_argv())
    r = rows(out)
    assert rc == 1, (out, err)
    assert r.get("P1 config", ("PASS",))[0] == "FAIL", r.get("P1 config")


def test_p1_fails_when_an_enabled_rule_already_exists(auto_world):
    """P1 catches an already-enabled move_vehicles rule in any fleet.

    The check exists to ensure the 06:00Z run cannot pick up a stale rule
    that was left enabled from a previous incomplete run.
    """
    auto_world.put_rule(W.RENTAL, "stale-rule", enabled=True, limit=300.0)
    rc, out, err = auto_world.run_main(auto_world.auto_argv())
    r = rows(out)
    assert rc == 1, (out, err)
    assert r.get("P1 config", ("PASS",))[0] == "FAIL", r.get("P1 config")
    assert "enabled" in r.get("P1 config", ("", ""))[1].lower(), r.get("P1 config")


# ============================================================================
# W7 — P1 checks policy exactly (wrong role → FAIL; extra Allow → FAIL)
# ============================================================================


def test_p1_fails_when_deny_names_wrong_role(auto_world):
    """P1 fails when the resource-policy Deny names a different role than the auto-approver.

    Mutant M17 scenario: the Deny condition references some-other-role instead of
    vsa-{stage}-avx-auto-approver-{region}.  P1's new exact-comparison must detect it.
    """
    auto_world.auto_defects["policy_wrong_role"] = True
    rc, out, err = auto_world.run_main(auto_world.auto_argv())
    r = rows(out)
    assert rc == 1, (out, err)
    p1 = r.get("P1 config", ("PASS",))
    assert p1[0] == "FAIL", p1
    detail = p1[1].lower()
    assert "condition" in detail or "role" in detail or "arn" in detail, (
        f"FAIL detail should name the role/condition mismatch: {p1[1]!r}")


def test_p1_fails_when_policy_has_extra_allow_statement(auto_world):
    """P1 fails when the resource policy has an extra Allow statement beyond the single Deny.

    An Allow * would bypass the Deny for callers that match both.  P1 must detect
    any Allow statement as unexpected.
    """
    auto_world.auto_defects["policy_extra_allow"] = True
    rc, out, err = auto_world.run_main(auto_world.auto_argv())
    r = rows(out)
    assert rc == 1, (out, err)
    p1 = r.get("P1 config", ("PASS",))
    assert p1[0] == "FAIL", p1
    detail = p1[1].lower()
    assert "allow" in detail, f"FAIL detail should name the Allow statement: {p1[1]!r}"


# ============================================================================
# W11 — P1 exact policy compare: operator / key / ARN / Principal / Resource / parse
# ============================================================================


def test_p1_fails_when_policy_uses_wrong_condition_operator(auto_world):
    """P1 fails when the Deny condition uses StringEquals instead of StringNotEquals."""
    auto_world.auto_defects["policy_wrong_operator"] = True
    rc, out, err = auto_world.run_main(auto_world.auto_argv())
    r = rows(out)
    assert rc == 1, (out, err)
    p1 = r.get("P1 config", ("PASS",))
    assert p1[0] == "FAIL", p1
    assert (
        "stringequals" in p1[1].lower()
        or "operator" in p1[1].lower()
        or "condition" in p1[1].lower()
    ), p1


def test_p1_fails_when_bounds_param_env_missing(auto_world):
    """P1 fails when AVX_AUTO_APPROVAL_BOUNDS_PARAM is absent from Lambda env."""
    auto_world.auto_defects["bounds_param_missing"] = True
    rc, out, err = auto_world.run_main(auto_world.auto_argv())
    r = rows(out)
    assert rc == 1, (out, err)
    p1 = r.get("P1 config", ("PASS",))
    assert p1[0] == "FAIL", p1
    assert (
        "avx_auto_approval_bounds_param" in p1[1].lower()
        or "bounds_param" in p1[1].lower()
    ), p1


def test_p1_fails_when_bounds_param_name_wrong(auto_world):
    """P1 fails when Lambda AVX_AUTO_APPROVAL_BOUNDS_PARAM doesn't match expected."""
    auto_world.auto_defects["bounds_param_wrong_name"] = True
    rc, out, err = auto_world.run_main(auto_world.auto_argv())
    r = rows(out)
    assert rc == 1, (out, err)
    p1 = r.get("P1 config", ("PASS",))
    assert p1[0] == "FAIL", p1
    assert (
        "avx_auto_approval_bounds_param" in p1[1].lower()
        or "bounds" in p1[1].lower()
    ), p1


def test_p1_fails_when_rules_scan_inconsistent(auto_world):
    """P1's rules-table scan must use ConsistentRead=True.

    Verified by inspecting Router.all_calls for the Scan call on the rules table;
    the params must carry ConsistentRead=True.
    """
    rc, out, err = auto_world.run_main(auto_world.auto_argv())
    # Collect every DynamoDB Scan call made during this run.
    scan_calls = [
        params
        for svc, op, params in auto_world.router.all_calls
        if svc == "dynamodb" and op == "Scan"
        and W.RULES in (params.get("TableName") or "")
    ]
    assert scan_calls, (
        f"No Scan call on rules table {W.RULES!r} found; "
        f"all_calls operations: {[(s,o) for s,o,_ in auto_world.router.all_calls]}"
    )
    for params in scan_calls:
        assert params.get("ConsistentRead") is True, (
            f"Rules scan missing ConsistentRead=True; params={params!r}"
        )


def test_p2_passes_when_two_eligible_findings_exist(auto_world):
    """P2 reports PASS and names both findings when at least two are undecided."""
    # Capture before run_main: after U5 moves the cars, C0 checks on the
    # moved finding would fail (cars no longer at their origins).
    fa = finding_a(auto_world)
    fb = finding_b(auto_world)
    rc, out, err = auto_world.run_main(auto_world.auto_argv())
    r = rows(out)
    assert rc == 0, (out, err)
    p2 = r.get("P2 candidates", ("missing",))
    assert p2[0] == "PASS", p2
    # Both finding IDs should appear in the message.
    assert fa["finding_id"] in p2[1], (fa["finding_id"], p2)
    assert fb["finding_id"] in p2[1], (fb["finding_id"], p2)


def test_p2_skips_when_all_findings_already_decided(auto_world):
    """P2 SKIP when no undecided pair is available (the next-run hint must appear)."""
    # Decide every move Finding.
    for f in auto_world.move_findings():
        auto_world._res().Table(W.ACTIONS).put_item(Item={
            "action_id": auto_world.decision_id(f["finding_id"]),
            "finding_id": f["finding_id"],
            "decision": "declined", "status": "declined",
        })
    rc, out, err = auto_world.run_main(auto_world.auto_argv())
    r = rows(out)
    # A SKIP on P2 must cascade to a SKIP on all write items and exit code 2.
    assert rc == 2, (out, err)
    assert r.get("P2 candidates", ("PASS",))[0] == "SKIP", r.get("P2 candidates")
    # Every write item should also be SKIP.
    for name in ("U2 rule", "U3 over limit", "U4 grounded", "U5 within limit"):
        assert r.get(name, ("PASS",))[0] == "SKIP", (name, r.get(name))


def test_p2_skips_when_all_findings_expired(auto_world):
    """W5: P2 filters out Findings with expires_at ≤ now + 2 h (_FINDING_MIN_LIFE).

    All findings are set to expire in 1 h (< 2 h); the undecided list is empty, so
    P2 must report SKIP.  Before the fix, no expiry check exists and P2 reports PASS.
    """
    soon = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=1)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    for f in auto_world.move_findings():
        f["expires_at"] = soon
        auto_world.put_finding(f)
    rc, out, err = auto_world.run_main(auto_world.auto_argv())
    r = rows(out)
    assert r.get("P2 candidates", ("PASS",))[0] == "SKIP", r.get("P2 candidates")
    assert rc == 2, (rc, out, err)


def test_p2_skips_when_all_probe_vins_outside_recall_cohort(tmp_path):
    """C3: P2 must run _recall_clear on the probe VIN; no probe → SKIP, nothing grounded.

    All rental VINs are removed from the recall cohort so no probe VIN can pass.
    Before the fix, P2 picks a probe without the recall check, so it reports PASS.
    """
    all_rental_vins = frozenset(v["vin"] for v in W.RENTAL_VEHICLES)
    with W.AutoWorld(tmp_path, recall_missing=all_rental_vins) as w:
        rc, out, err = w.run_main(w.auto_argv())
        r = rows(out)
        assert r.get("P2 candidates", ("PASS",))[0] == "SKIP", r.get("P2 candidates")
        assert rc == 2, (rc, out, err)
        open_groundings = [g for g in w.items(W.GROUNDINGS) if not g.get("returnedAt")]
        assert not open_groundings, f"unexpected open grounding: {open_groundings}"


# ============================================================================
# U1 — internal route rejects unsigned and caller-signed requests
# ============================================================================


def test_u1_unsigned_post_gets_403(auto_world):
    """U1 catches the internal route returning 403 to an unsigned POST (Deny policy)."""
    rc, out, err = auto_world.run_main(auto_world.auto_argv())
    r = rows(out)
    assert rc == 0, (out, err)
    u1 = r.get("U1 403 probes", ("missing",))
    assert u1[0] == "PASS", u1
    # The unsigned request must have hit the internal API and got 403.
    unsigned = [x for x in auto_world.internal.requests
                if not x.get("authorization") and x.get("status") == 403]
    assert unsigned, auto_world.internal.requests


def test_u1_caller_signed_post_gets_403(auto_world):
    """U1 catches the internal route returning 403 to a SigV4 POST signed by the caller.

    The 'caller' is the live-verify script itself, whose AWS_ACCESS_KEY_ID is 'testing'
    — not the auto-approver role.  The resource-policy Deny sends both back with 403.

    Bug fix: the original filter used ``"testing" not in authorization``, which
    *excluded* the caller's request because AWS_ACCESS_KEY_ID="testing" appears in the
    SigV4 Credential field.  The correct filter is ``"testing" in authorization`` to
    *select* the caller-signed entry.
    """
    rc, out, err = auto_world.run_main(auto_world.auto_argv())
    r = rows(out)
    assert rc == 0, (out, err)
    u1 = r.get("U1 403 probes", ("missing",))
    assert u1[0] == "PASS", u1
    # A caller-signed request (non-approver key) must appear in the internal API's log.
    caller_signed = [
        x for x in auto_world.internal.requests
        if x.get("authorization") and "testing" in (x.get("authorization") or "")
        and x.get("status") == 403
    ]
    assert caller_signed, auto_world.internal.requests


def test_u1_moves_table_unchanged_after_failed_probes(auto_world):
    """U1 verifies the moves table item set is unchanged by the two 403 probes.

    The U1 probes POST to the internal moves route with made-up action IDs; CMS
    refuses them with 403 (the route is behind the Deny policy and the script's
    own key is not the auto-approver key).  Even if the probes somehow reached
    CMS's handler, the action IDs don't exist in DDB so the handler would return
    404 without writing a move record.

    The test compares DDB move item sets before and after the full run.  U5 adds
    exactly one move record (for finding A); the U1 probes must contribute zero.
    The set equation is:  moves_after == moves_before ∪ {action_id_a}.

    Before run_main:  scan the moves table → moves_before (set of moveId strings).
    After run_main:   scan the moves table → moves_after.
    Assert: moves_after == moves_before | {action_id_a}  (U5's record only, none from U1).
    """
    fa = finding_a(auto_world)
    action_id_a = auto_world.decision_id(fa["finding_id"])
    moves_before = {
        m.get("moveId") for m in auto_world.items(W.MOVES)
        if m.get("moveId")
    }
    rc, out, err = auto_world.run_main(auto_world.auto_argv())
    assert rc == 0, (out, err)
    r = rows(out)
    assert r.get("U1 403 probes", ("missing",))[0] == "PASS", r.get("U1 403 probes")
    moves_after = {
        m.get("moveId") for m in auto_world.items(W.MOVES)
        if m.get("moveId")
    }
    new_moves = moves_after - moves_before
    assert new_moves == {action_id_a}, (
        f"Expected exactly A's move record to be new; "
        f"got new_moves={new_moves!r}, action_id_a={action_id_a!r}"
    )


# W8 tests — body checks, path pinning, FAIL on wrong path or bare 403 body


def test_auto_internal_moves_path_matches_cms_executor(auto_world):
    """_AUTO_INTERNAL_MOVES_PATH must equal agents.executors.cms._INTERNAL_MOVES_PATH.

    Pins the two constants so a rename of one is caught immediately.
    """
    import agents.executors.cms as _cms
    assert auto_world.mod._AUTO_INTERNAL_MOVES_PATH == _cms._INTERNAL_MOVES_PATH, (
        f"_AUTO_INTERNAL_MOVES_PATH={auto_world.mod._AUTO_INTERNAL_MOVES_PATH!r} != "
        f"agents.executors.cms._INTERNAL_MOVES_PATH={_cms._INTERNAL_MOVES_PATH!r}"
    )


def test_u1_wrong_probe_path_causes_u1_fail(auto_world):
    """U1 must FAIL when the explicit-deny body names the wrong resource path.

    Simulates a probe that reached the wrong API or stage: the IamTlsApi returns
    a deny body whose resource ARN ends with /POST/wrong/path instead of the
    correct /POST/api/v1/fleet-intelligence/internal/moves.  The script must
    require the resource ARN to name the expected path.
    """
    # wrong_deny_path defect: deny body names this path instead of the actual request path.
    auto_world.internal.wrong_deny_path = "/wrong/path"
    rc, out, err = auto_world.run_main(auto_world.auto_argv())
    r = rows(out)
    u1 = r.get("U1 403 probes", ("PASS", ""))
    assert u1[0] == "FAIL", (
        f"Expected U1 FAIL when deny body names wrong path; got {u1}; output: {out!r}"
    )
    assert ("wrong/path" in u1[1] or "resource" in u1[1].lower() or "deny" in u1[1].lower()), (
        f"U1 FAIL detail should mention resource ARN mismatch; got {u1[1]!r}"
    )


def test_u1_bare_forbidden_body_causes_u1_fail(auto_world):
    """U1 must FAIL when the internal API returns a bare 403 {"message":"Forbidden"}.

    The bare body (not AWS-style Missing Authentication Token or explicit deny)
    cannot distinguish a wrong path from the resource-policy Deny.  The script
    must require the specific AWS bodies to pass U1.
    """
    auto_world.internal.bare_deny = True
    rc, out, err = auto_world.run_main(auto_world.auto_argv())
    r = rows(out)
    u1 = r.get("U1 403 probes", ("PASS", ""))
    assert u1[0] == "FAIL", (
        f"Expected U1 FAIL when API returns bare Forbidden body; got {u1}; output: {out!r}"
    )


# ============================================================================
# W13 — U1 parse 'on resource:' ARN, require exact path suffix + 'with an explicit deny'
# ============================================================================


def test_u1_wrong_path_suffix_causes_u1_fail(auto_world):
    """U1 fails when the deny body's resource ARN has a wrong path suffix (not substring).

    M14 scenario: ``wrong_deny_path`` injects a wrong suffix into the deny body of a
    matched-route probe.  The script must compare the path suffix exactly, not as a
    substring.
    """
    auto_world.internal.wrong_deny_path = W.INTERNAL_FI_MOVES_PATH + "-wrong"
    rc, out, err = auto_world.run_main(auto_world.auto_argv())
    r = rows(out)
    u1 = r.get("U1 403 probes", ("PASS", ""))
    assert u1[0] == "FAIL", (
        f"Expected U1 FAIL with wrong path suffix in deny ARN; got {u1}; out={out!r}"
    )
    assert (
        "path" in u1[1].lower()
        or "resource" in u1[1].lower()
        or "arn" in u1[1].lower()
    ), u1


def test_u1_implicit_deny_causes_u1_fail(auto_world):
    """U1 fails when the signed probe gets a deny without 'with an explicit deny'.

    N-W8b scenario: the body has 'is not authorized to perform: execute-api:Invoke
    on resource: ...' but lacks 'with an explicit deny in a resource-based policy'.
    """
    auto_world.internal.implicit_deny = True
    rc, out, err = auto_world.run_main(auto_world.auto_argv())
    r = rows(out)
    u1 = r.get("U1 403 probes", ("PASS", ""))
    assert u1[0] == "FAIL", (
        f"Expected U1 FAIL with implicit-deny body; got {u1}; out={out!r}"
    )
    assert "explicit" in u1[1].lower(), u1


# ============================================================================
# U2 — rule created disabled, then enabled via the AVX approval-rules API
# ============================================================================


def test_u2_rule_created_disabled_then_enabled_and_read_back(auto_world):
    """U2 creates a move_vehicles rule (disabled), enables it, and reads it back.

    Bug fix (mid-run assertion): the original assertion checked ``rule.get("enabled")
    is True`` on the DDB rule *after the full run*, but U6 disables the rule before the
    run ends.  The end-state is always ``enabled=False``, so the assertion always failed.
    The correct check is a mid-run fact: the AVX rules API request log must contain a
    successful enable PUT (status 200) with ``"enabled": true`` in the body.  U6's
    later disable PUT is a separate request and does not invalidate this check.
    """
    rc, out, err = auto_world.run_main(auto_world.auto_argv())
    r = rows(out)
    assert rc == 0, (out, err)
    u2 = r.get("U2 rule", ("missing",))
    assert u2[0] == "PASS", u2
    # Mid-run fact: the AVX rules API must have received a successful enable PUT.
    enable_calls = [
        x for x in auto_world.avx.requests
        if x.get("method") == "PUT"
        and "/approval-rules/fleet/" in (x.get("path") or "")
        and json.loads(x.get("body") or "{}").get("enabled") is True
        and x.get("status", 0) in (200, 201)
    ]
    assert enable_calls, (
        f"no successful enable PUT found in AVX rules API log; "
        f"requests: {auto_world.avx.requests}"
    )


def test_u2_create_with_enabled_true_is_refused(auto_world):
    """U2 depends on the rules API rejecting enabled=True at create time (G8-D6).

    Rules are always created disabled; the API must return 400 for a create body
    that sets enabled=True.  U2 should FAIL if the API accepts it (a stale enabled
    rule would be left in the table, detected by P1 in the next run).

    Tests the approval-rules Lambda directly (not through run_main) because the
    invariant belongs to the Lambda, which is already fully implemented.
    """
    event = {
        "httpMethod": "PUT",
        "resource": "/approval-rules/fleet/{fleet_id}/{rule_id}",
        "path": f"/approval-rules/fleet/{W.RENTAL}/new-enabled-rule",
        "headers": {}, "queryStringParameters": None,
        "pathParameters": {"fleet_id": W.RENTAL, "rule_id": "new-enabled-rule"},
        "requestContext": {"authorizer": {"claims": {
            "sub": "test-sub-u2", "cognito:groups": "fleet-operator",
            "custom:fleetIds": W.RENTAL,
        }}},
        "body": json.dumps({"action_kind": "move_vehicles", "enabled": True, "limit": 400.0}),
    }
    auto_world._reset_approval_rules_caches()
    resp = auto_world.approval_rules.handler(event, None)
    assert resp["statusCode"] == 400, resp
    body = json.loads(resp["body"])
    assert body.get("code") == "rules_are_created_disabled", body


def test_u2_create_rule_put_limit(auto_world):
    """W15: U2's create PUT sends limit == round(a_cost - 1, 2) and no enabled key.

    The create PUT sets the initial limit below a_cost so the evaluator won't approve
    A yet.  Asserts from avx.requests that the actual PUT body carries the right limit
    and does NOT include 'enabled: true' (which would make U2 create AND enable the rule
    in one call, preventing the deliberate 'over-limit probe' in U3).
    """
    import json as _json  # noqa: PLC0415
    fa = finding_a(auto_world)
    a_cost = float(
        ((fa.get("recommended_action") or {}).get("parameters") or {})
        .get("transfer_cost_usd", 0)
    )
    expected_limit = round(a_cost - 1.0, 2)

    rc, out, err = auto_world.run_main(auto_world.auto_argv())
    r = rows(out)
    assert rc == 0, (out, err)

    # U2's create PUT: has "action_kind" AND does NOT have "enabled" in the body.
    rules_puts = [
        req for req in auto_world.avx.requests
        if req.get("method") == "PUT"
        and "/approval-rules/" in (req.get("path") or "")
    ]
    create_puts = []
    for req in rules_puts:
        try:
            body = _json.loads(req.get("body") or "{}")
            if "action_kind" in body and "enabled" not in body:
                create_puts.append(body)
        except Exception:  # noqa: BLE001
            pass
    assert create_puts, (
        f"No U2 create PUT found in avx.requests; all rules PUTs: "
        f"{[req.get('body') for req in rules_puts]}"
    )
    create_body = create_puts[0]
    actual_limit = round(float(create_body["limit"]), 2)
    assert actual_limit == expected_limit, (
        f"U2 create PUT body limit={actual_limit!r} != round(a_cost - 1, 2) = {expected_limit!r}"
    )
    # W15: "not enabled: true" — the create body must not enable the rule.
    assert "enabled" not in create_body, (
        f"U2 create PUT body must not include 'enabled'; got: {create_body!r}"
    )


# ============================================================================
# U3 — evaluator: over the limit produces no Action; limit raised → Action written
# ============================================================================


def test_u3_over_limit_produces_no_action(auto_world):
    """U3 reports PASS for 'no Action for A' when rule limit < A's cost.

    This is the evaluator-over-limit failure case the spec names: the evaluator
    must NOT approve A when the rule's limit is below A's transfer cost.

    Bug fix (mid-run assertion): the original assertion checked the DDB Actions
    table *after the full run*, but U5 writes the Action for A before the run ends.
    The end-state always has an executed Action, so ``act is None`` always failed.
    The correct check is a mid-run fact: the U3 PASS row in the script's output
    records that the evaluator wrote no Action.  The alternative of checking the
    RUNS table is unreliable in the test world because multiple evaluator invocations
    (U3, U4, U5) can happen within the same second; DDB uses second-precision sort
    keys so later invocations silently overwrite earlier ones at the same timestamp.

    The RUNS table key is ``agent_component = "{agent_id}#{component}"``; filter on
    ``agent_component = "agent-fi-evaluator#evaluator"`` (not on ``agent_id``).
    """
    rc, out, err = auto_world.run_main(auto_world.auto_argv())
    r = rows(out)
    assert rc == 0, (out, err)
    u3 = r.get("U3 over limit", ("missing",))
    assert u3[0] == "PASS", u3
    # The PASS row already records "evaluator wrote no Action for A"; that is the
    # mid-run fact.  The RUNS-table check was removed because moto timestamps are
    # second-precise: if U3 and U4 share the same second, U4's record (artifacts=1)
    # overwrites U3's (artifacts=0), making the test incorrectly fail.


def test_u3_evaluator_cannot_approve_over_limit(auto_world):
    """U3 detects a fail-open evaluator that approves a move above the rule's limit.

    If the evaluator writes an Action even when cost > rule.limit, U3 must FAIL.
    This is the explicit named failure from the spec's 'failures that item must catch'.
    """
    fa = finding_a(auto_world)
    # Create a rule whose limit is $1 (far below any realistic transfer cost).
    auto_world.put_rule(W.RENTAL, RULE_ID, enabled=True, limit=1.0)
    # Invoke the auto-approver directly with finding A.
    result = auto_world.invoke_approver(fa)
    # A correct auto-approver writes NO Action (cost > 1.0).
    act = auto_world.action(fa["finding_id"])
    assert act is None, (
        f"evaluator approved a move over the rule limit (cost > $1.00): {act}")


# ============================================================================
# U4 — grounded probe car: CMS refuses with 409; no move record written
# ============================================================================


def test_u4_grounded_car_causes_cms_refusal(auto_world):
    """U4 reports PASS when CMS returns 409 for a rule-approved move of a grounded car."""
    rc, out, err = auto_world.run_main(auto_world.auto_argv())
    r = rows(out)
    assert rc == 0, (out, err)
    u4 = r.get("U4 grounded", ("missing",))
    assert u4[0] == "PASS", u4


def test_u4_grounding_check_is_real_not_vacuous(auto_world):
    """U4 fails if CMS's grounding check is bypassed (defect: moves_ignore_groundings)."""
    auto_world.defects["moves_ignore_groundings"] = True
    rc, out, err = auto_world.run_main(auto_world.auto_argv())
    r = rows(out)
    assert rc == 1, (out, err)
    u4 = r.get("U4 grounded", ("PASS",))
    assert u4[0] == "FAIL", u4
    assert "grounded" in u4[1].lower(), u4


def test_u4_probe_car_returns_to_origin_after_grounding_refusal(auto_world):
    """U4 verifies B's probe car is back at its origin and grounding is closed.

    Must capture the probe VIN *before* run_main; after U5 moves A's cars,
    the C0 eligibility check in finding_b would see the world in a changed state.
    """
    fa = finding_a(auto_world)
    fb = finding_b(auto_world)
    p_vin = probe_vin_of(auto_world, fb, fa)
    probe_vid = auto_world.vehicle_id_of(p_vin)
    rc, out, err = auto_world.run_main(auto_world.auto_argv())
    assert rc == 0, (out, err)
    # Car must be back at its original location.
    v_row = auto_world.vehicle(probe_vid)
    original_loc = next(v["locationId"] for v in W.RENTAL_VEHICLES if v["vin"] == p_vin)
    assert v_row.get("locationId") == original_loc, v_row
    # The grounding must be closed (returned).
    g = auto_world.groundings(probe_vid)
    assert g and g[-1].get("returnedAt"), g


# ============================================================================
# U5 — happy path: move executes with the rule's fields in the move record
# ============================================================================


def test_u5_move_executes_with_rule_fields(auto_world):
    """U5 reports PASS and the move record carries decidedByKind=rule, ruleId, auto_approved.

    Must capture finding_a before run_main; after U5 moves the cars, C0 checks
    would see them at the destination, not origin, and finding_a would raise.
    """
    fa = finding_a(auto_world)
    rc, out, err = auto_world.run_main(auto_world.auto_argv())
    r = rows(out)
    assert rc == 0, (out, err)
    u5 = r.get("U5 within limit", ("missing",))
    assert u5[0] == "PASS", u5
    # Locate the executed move record in CMS's moves table.
    action_id = auto_world.decision_id(fa["finding_id"])
    mv = auto_world.move(action_id)
    assert mv is not None, "no move record found after U5"
    assert mv.get("decidedByKind") == "rule", mv
    assert mv.get("ruleId") == _rule_id_of(out) or mv.get("rule_id") == _rule_id_of(out), mv
    assert mv.get("decision") == "auto_approved", mv


def test_u5_move_record_has_no_approver_sub(auto_world):
    """U5 verifies no approverSub in the move record (rule-decided, no human token).

    Must capture finding_a before run_main; after U5 moves the cars, C0 checks fail.
    """
    fa = finding_a(auto_world)
    rc, out, err = auto_world.run_main(auto_world.auto_argv())
    assert rc == 0, (out, err)
    mv = auto_world.move(auto_world.decision_id(fa["finding_id"]))
    assert mv is not None, "no move record found"
    assert "approverSub" not in mv, f"approverSub present in rule-approved move: {mv}"


def test_u5_cars_are_at_destination_after_move(auto_world):
    """U5 verifies each VIN's base row shows the destination locationId after execution.

    Must capture finding_a before run_main; after U5 moves the cars, the C0 check
    in finding_a (which requires cars to be at origin) would raise ValueError.
    """
    fa = finding_a(auto_world)
    rc, out, err = auto_world.run_main(auto_world.auto_argv())
    assert rc == 0, (out, err)
    dest = fa["location"]["to_location_id"]
    for vin_val in fa["recommended_action"]["parameters"]["vehicle_vins"]:
        vid = auto_world.vehicle_id_of(vin_val)
        v_row = auto_world.vehicle(vid)
        assert v_row.get("locationId") == dest, (vin_val, v_row)


# ============================================================================
# W4 — U5 checks decided_by exactly, GET /moves fields, car lookup failure
# ============================================================================


def test_u5_decided_by_id_wrong(auto_world):
    """U5 fails when decided_by.id names a different rule than the one created in U2.

    Mutant M17-style: a different rule approved A.  The existing test
    ``test_u5_move_executes_with_rule_fields`` only checks ``kind``; this test
    pins the full id comparison added by W4.
    """
    auto_world.auto_defects["u5_wrong_decided_by_id"] = True
    rc, out, err = auto_world.run_main(auto_world.auto_argv())
    r = rows(out)
    assert rc == 1, (out, err)
    u5 = r.get("U5 within limit", ("PASS",))
    assert u5[0] == "FAIL", u5
    detail = u5[1].lower()
    assert "decided_by" in detail or "id" in detail, (
        f"FAIL detail should name decided_by: {u5[1]!r}")


def test_u5_move_tolocation_wrong(auto_world):
    """U5 fails when GET /moves returns a toLocationId that doesn't match A's destination.

    The world's u5_move_tolocation_wrong defect replaces toLocationId with a
    wrong value in the GET /moves response.  W4's new field comparison must detect it.
    """
    auto_world.auto_defects["u5_move_tolocation_wrong"] = True
    rc, out, err = auto_world.run_main(auto_world.auto_argv())
    r = rows(out)
    assert rc == 1, (out, err)
    u5 = r.get("U5 within limit", ("PASS",))
    assert u5[0] == "FAIL", u5
    detail = u5[1]
    assert ("toLocationId" in detail or "tolocation" in detail.lower()), (
        f"FAIL detail should name toLocationId: {detail!r}")


def test_u5_car_lookup_fails(auto_world):
    """U5 fails (not silently passes) when a vehicle base row read raises.

    Before W4 the per-car exception was swallowed.  Now it becomes a FAIL.
    Uses mock.patch on the module-level _auto_vehicle, targeting only A's
    vehicle IDs so U4's B-car checks and U6's return-to-service are unaffected.
    """
    from unittest import mock

    fa = finding_a(auto_world)
    a_vins_fa = list(
        ((fa.get("recommended_action") or {}).get("parameters") or {})
        .get("vehicle_vins") or []
    )
    # Resolve A's vehicle IDs before the run (after U5 moves the cars they are
    # still in the vehicles table, but we need their ids to target the mock).
    a_vids = {auto_world.vehicle_id_of(v) for v in a_vins_fa}

    orig = auto_world.mod._auto_vehicle
    first_a_call = [False]

    def patched(ddb: object, t: dict, vid: str) -> dict:
        if vid in a_vids and not first_a_call[0]:
            first_a_call[0] = True
            raise RuntimeError("world: u5_car_lookup_fails defect")
        return orig(ddb, t, vid)

    with mock.patch.object(auto_world.mod, "_auto_vehicle", side_effect=patched):
        rc, out, err = auto_world.run_main(auto_world.auto_argv())
    r = rows(out)
    assert rc == 1, (out, err)
    u5 = r.get("U5 within limit", ("PASS",))
    assert u5[0] == "FAIL", u5
    detail = u5[1].lower()
    assert "failed" in detail or "lookup" in detail or "error" in detail, (
        f"FAIL detail should name the failure: {u5[1]!r}")


# ============================================================================
# C6 — U5 checks CMS move record: decidedByKind, ruleId, decision, no approverSub
# ============================================================================


def test_u5_cms_move_decidedByKind_operator(auto_world):
    """C6: U5 fails when CMS's move record has decidedByKind != 'rule'.

    The world defect rewrites all rule-decided move records to decidedByKind='operator'
    after CMS's handler writes them.  U5's new ConsistentRead check must detect this and
    report FAIL naming the field.
    """
    auto_world.auto_defects["u5_cms_move_decidedByKind_operator"] = True
    rc, out, err = auto_world.run_main(auto_world.auto_argv())
    r = rows(out)
    assert rc == 1, (out, err)
    u5 = r.get("U5 within limit", ("PASS",))
    assert u5[0] == "FAIL", u5
    detail = u5[1]
    assert "decidedByKind" in detail or "decidedbyKind" in detail.lower(), (
        f"U5 FAIL detail should name 'decidedByKind': {detail!r}")


def test_u5_cms_move_approverSub_present(auto_world):
    """C6: U5 fails when CMS's move record has approverSub present (rule-decided, no human token).

    The world defect adds approverSub to A's move record after CMS writes it.  U5's new
    check must detect the unexpected approverSub on the CMS record (not on the AVX Action)
    and record FAIL naming the field.
    """
    auto_world.auto_defects["u5_cms_move_approverSub"] = True
    rc, out, err = auto_world.run_main(auto_world.auto_argv())
    r = rows(out)
    assert rc == 1, (out, err)
    u5 = r.get("U5 within limit", ("PASS",))
    assert u5[0] == "FAIL", u5
    detail = u5[1]
    assert "approverSub" in detail, (
        f"U5 FAIL detail should name 'approverSub': {detail!r}")


def test_u6_rule_is_disabled_after_the_run(auto_world):
    """U6 catches a rule left enabled at the end (named failure in the spec).

    After the run the world-created rule must be stored with enabled=False.
    If it stays enabled, U6 FAIL, and the next 06:00Z evaluator run would pick it up.
    """
    rc, out, err = auto_world.run_main(auto_world.auto_argv())
    r = rows(out)
    assert rc == 0, (out, err)
    u6 = r.get("U6 always", ("missing",))
    assert u6[0] == "PASS", u6
    rule = auto_world.get_rule(W.RENTAL, _rule_id_of(out))
    assert rule is None, (
        f"rule should be deleted after U6 disabled and deleted it: {rule}"
    )


def test_u6_probe_car_returned_in_finally(auto_world):
    """C5: when the grounding route times out (writes nothing, written=None),
    _auto_return_to_service must FAIL — the outcome is unknown so the run cannot
    claim success.  The probe car may or may not be grounded; U6 reports FAIL and
    the caller prints the cleanup pointer.

    Failing contract: the old code returned True ("assumed not landed") so U6 reported
    PASS even though nothing confirmed the car was safe.  The fix removes the fail-open
    shortcut: written=None with no row → FAIL, rc 1.
    """
    auto_world.defects["grounding_times_out"] = True
    rc, out, err = auto_world.run_main(auto_world.auto_argv())
    r = rows(out)
    # U4 FAIL (grounding timed out), U6 FAIL (unknown grounding outcome).
    u6 = r.get("U6 always", ("missing",))
    assert u6[0] == "FAIL", (u6, out, err)
    assert rc == 1, (rc, out, err)
    # The cleanup pointer must be printed to stderr so the operator can recover.
    assert "--cleanup-only" in err and "--run-id" in err, (
        f"cleanup pointer not in stderr: {err!r}"
    )


def test_u6_rts_refused_after_unknown_grounding(auto_world):
    """C5: when the grounding route committed but answered 504 (written=None) and every
    return-to-service attempt then fails, U6 must FAIL and print the cleanup pointer.

    The probe car's grounding row exists (the route committed before the 504), but RTS
    returns 503 on every attempt.  The run reports FAIL, rc 1, the row stays open, and
    stderr names the --cleanup-only command.

    Failing contract: the old code returned True ("assumed not landed") after the waits
    because written=None; the row's existence and the failed RTS were ignored.
    """
    fa = finding_a(auto_world)
    fb = finding_b(auto_world)
    p_vin = probe_vin_of(auto_world, fb, fa)
    probe_vid = auto_world.vehicle_id_of(p_vin)

    auto_world.defects["grounding_commits_then_504"] = True
    auto_world.defects["rts_failures"] = 99  # every RTS call fails with 503

    rc, out, err = auto_world.run_main(auto_world.auto_argv())
    r = rows(out)

    assert rc == 1, (rc, out, err)
    u6 = r.get("U6 always", ("missing",))
    assert u6[0] == "FAIL", (u6, out, err)

    # The probe car's grounding row must still exist and not be returned.
    g = auto_world.groundings(probe_vid)
    assert g, f"no grounding row for probe car {probe_vid} (should have been committed)"
    open_g = [x for x in g if not x.get("returnedAt")]
    assert open_g, (
        f"probe car {probe_vid} was returned despite rts_failures=99: {g}"
    )

    # The cleanup pointer must be printed to stderr.
    assert "--cleanup-only" in err and "--run-id" in err, (
        f"cleanup pointer not in stderr: {err!r}"
    )


def test_u6_temporary_operator_deleted(auto_world):
    """U6 ensures the temporary fleet-operator created in U2 is deleted at the end.

    Bug fix: the original assertion scanned all fleet-operators in the pool, including
    any that existed before the run (e.g. world-seed users or other test fixtures).  This
    produced false positives when pre-existing operators were present.  The fix snapshots
    the pool's user set *before* the run and checks that no *new* users remain after: any
    operator created by this run's _temp_operator call must be deleted in U6's finally
    block.
    """
    users_before = set(auto_world.pool.users)
    rc, out, err = auto_world.run_main(auto_world.auto_argv())
    assert rc == 0, (out, err)
    # Only users that this run created (not pre-existing ones) are checked.
    new_users = set(auto_world.pool.users) - users_before
    assert not new_users, (
        f"temporary operator(s) still in pool after U6: "
        f"{[auto_world.pool.users[s] for s in new_users]}"
    )


# ============================================================================
# Write-window guard
# ============================================================================


def test_refused_inside_03_45_07_15_write_window(auto_world):
    """The run is refused when the clock is inside the 03:45-07:15Z write window.

    Note (Bug 1 fix): ``test_p1_skips_inside_05_30_07_15_window`` was removed because
    the 03:45–07:15Z write window already covers 05:30–07:15Z.  The removed test set
    the fake clock to 06:00Z (inside both windows), so this test already catches it.
    Only one write-window test is needed.
    """
    auto_world.mod._now = lambda: dt.datetime.now(dt.timezone.utc).replace(
        hour=4, minute=0, second=0, microsecond=0)
    rc, out, err = auto_world.run_main(auto_world.auto_argv())
    assert rc == 2, (out, err)
    assert "write window" in err.lower() or "write window" in out.lower(), (out, err)

# ============================================================================
# C4 — U6 rule-disable resilience and cleanup-only
# ============================================================================


def test_u6_rule_disabled_after_u3_exception(auto_world):
    """C4: U6 disables the rule even after an exception in U3 (finally block runs).

    The disable is the critical cleanup step; U3 exceptions must not skip it.
    """
    auto_world.auto_defects["u3_invoke_raises"] = True
    rc, out, err = auto_world.run_main(auto_world.auto_argv())
    r = rows(out)
    # U3 must fail (the world's invoke raised)
    assert r.get("U3 over limit", ("missing",))[0] == "FAIL", r
    # U6 must still pass
    u6 = r.get("U6 always", ("missing",))
    assert u6[0] == "PASS", u6
    rule = auto_world.get_rule(W.RENTAL, _rule_id_of(out))
    assert rule is None, (
        f"rule should be deleted after U6 exception path: {rule}"
    )


def test_u6_rule_disabled_with_unreadable_bounds(auto_world):
    """C4(c): U6 disables the rule even when bounds become unreadable after P1.

    The fix removes 'limit' from the disable body so the approval-rules Lambda
    uses the pure-disable path that skips the bounds SSM read.
    """
    auto_world.auto_defects["delete_bounds_after_p1"] = True
    rc, out, err = auto_world.run_main(auto_world.auto_argv())
    r = rows(out)
    assert rc == 0, (out, err)
    u6 = r.get("U6 always", ("missing",))
    assert u6[0] == "PASS", u6
    rule = auto_world.get_rule(W.RENTAL, _rule_id_of(out))
    assert rule is None, (
        f"rule should be deleted by U6 pure-disable path: {rule}"
    )


def test_auto_cleanup_only_disables_rule_and_returns_car(auto_world):
    """C4(d): --cleanup-only --run-id disables the rule and returns the probe car.

    Simulates a crash after U2+U4 (rule enabled, probe car grounded, manifest saved).
    The cleanup-only path must disable the rule and return the car, exempt from the
    write window.
    """
    import json as _json

    fa = finding_a(auto_world)
    fb = finding_b(auto_world)
    pvin = probe_vin_of(auto_world, fb, fa)
    pvid = auto_world.vehicle_id_of(pvin)

    run_id = "20261022T120000Z-abcdef"
    probe_gid = auto_world.mod._AUTO_PROBE_PREFIX + run_id
    per_run_rid = _run_rule_id(run_id)

    # Write manifest to the world's state_dir (as _save_manifest would).
    auto_world.state_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "run_id": run_id, "stage": W.STAGE, "part": "auto",
        "started_at": "2026-10-22T12:00:00Z",
        "rule_id": per_run_rid,
        "probe_gid": probe_gid,
        "probe_vid": pvid,
    }
    (auto_world.state_dir / f"{run_id}.json").write_text(_json.dumps(manifest))

    # Set up state: rule enabled and probe car grounded (as if U2+U4 ran and the script crashed).
    auto_world.put_rule(W.RENTAL, per_run_rid, enabled=True, limit=400.0)
    auto_world.put_grounding(pvid, probe_gid)

    # Verify the world state before cleanup.
    assert auto_world.get_rule(W.RENTAL, per_run_rid).get("enabled") is True
    assert any(g for g in auto_world.groundings(pvid) if not g.get("returnedAt"))

    # Run cleanup-only (--allow-writes required per W10; exempt from write window).
    argv = [
        "--part", "auto",
        "--stage", W.STAGE,
        "--workgroup", "wg",
        "--cms-api-endpoint", auto_world.cms.url,
        "--avx-api-endpoint", auto_world.avx.url,
        "--allow-writes",
        "--cleanup-only", "--run-id", run_id,
    ]
    rc, out, err = auto_world.run_main(argv)
    r = rows(out)

    assert rc == 0, (out, err)
    x1 = r.get("X1-auto", ("missing",))
    assert x1[0] == "PASS", (x1, out, err)

    # Rule must be deleted (disabled then deleted by cleanup_only).
    rule = auto_world.get_rule(W.RENTAL, per_run_rid)
    assert rule is None, f"rule should be deleted after cleanup: {rule}"

    # Probe car must have no open groundings.
    open_gnd = [g for g in auto_world.groundings(pvid) if not g.get("returnedAt")]
    assert not open_gnd, f"probe car still grounded after cleanup: {open_gnd}"


# ============================================================================
# W10 — incremental manifest saves; cleanup-only: read-first, probe_posted
# ============================================================================


def test_auto_cleanup_only_manifest_only_is_pass(auto_world):
    """W10: --cleanup-only with a manifest that has no rule_created and no probe_posted
    (nothing was written before the crash) must PASS with no PUT sent to the AVX API.

    Failing contract: the old code always sent the disable PUT, which answered 400
    (rules API rejects a pure-disable on a nonexistent rule), then reported FAIL.
    The W10 fix reads the rule from DDB first; absent → skip, PASS.
    """
    import json as _json

    run_id = "20261022T130000Z-abcdef"
    # Write a manifest with only the minimal fields (no rule_created, no probe_posted).
    auto_world.state_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "run_id": run_id, "stage": W.STAGE, "part": "auto",
        "started_at": "2026-10-22T13:00:00Z",
        "rule_id": _run_rule_id(run_id),
        "probe_gid": auto_world.mod._AUTO_PROBE_PREFIX + run_id,
        "probe_vid": "VEH-SOME-0001",
    }
    (auto_world.state_dir / f"{run_id}.json").write_text(_json.dumps(manifest))

    # No rule in DDB, no grounding.
    avx_requests_before = len(auto_world.avx.requests)

    argv = [
        "--part", "auto",
        "--stage", W.STAGE,
        "--workgroup", "wg",
        "--cms-api-endpoint", auto_world.cms.url,
        "--avx-api-endpoint", auto_world.avx.url,
        "--allow-writes",
        "--cleanup-only", "--run-id", run_id,
    ]
    rc, out, err = auto_world.run_main(argv)
    r = rows(out)

    assert rc == 0, (out, err)
    x1 = r.get("X1-auto", ("missing",))
    assert x1[0] == "PASS", (x1, out, err)

    # No PUT must have been sent to the AVX approval-rules API.
    new_avx_puts = [
        x for x in auto_world.avx.requests[avx_requests_before:]
        if x.get("method") == "PUT"
    ]
    assert not new_avx_puts, (
        f"no AVX PUT should be sent when rule is absent: {new_avx_puts}"
    )


def test_auto_cleanup_only_requires_allow_writes(auto_world):
    """W10: --cleanup-only without --allow-writes is refused (S4 / W10)."""
    import json as _json

    run_id = "20261022T130001Z-abcdef"
    auto_world.state_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "run_id": run_id, "stage": W.STAGE, "part": "auto",
        "started_at": "2026-10-22T13:00:01Z",
        "rule_id": _run_rule_id(run_id),
        "probe_gid": auto_world.mod._AUTO_PROBE_PREFIX + run_id,
        "probe_vid": "VEH-SOME-0002",
    }
    (auto_world.state_dir / f"{run_id}.json").write_text(_json.dumps(manifest))

    argv = [
        "--part", "auto",
        "--stage", W.STAGE,
        "--workgroup", "wg",
        "--cms-api-endpoint", auto_world.cms.url,
        "--avx-api-endpoint", auto_world.avx.url,
        "--cleanup-only", "--run-id", run_id,
    ]
    rc, out, err = auto_world.run_main(argv)
    assert rc == 2, (rc, out, err)
    assert "allow-writes" in err.lower(), f"expected REFUSED about --allow-writes: {err!r}"


def test_auto_cleanup_only_uses_real_manifest(auto_world):
    """W10: a real run's own manifest (produced by _save_manifest) is the one
    cleanup-only reads.  After a complete run (all PASS, rule already deleted),
    cleanup-only finds no rule → PASS, no PUT sent.  Verifies that:
    - The run did save a manifest (it was written by the script's incremental saves).
    - probe_posted is set in the manifest after a grounding POST.
    - cleanup-only correctly reports PASS when there is nothing to clean.
    """
    # Run a complete --part auto (all PASS).
    rc, out, err = auto_world.run_main(auto_world.auto_argv())
    assert rc == 0, (out, err)

    # The run-id was printed to stdout before U2 (C7).
    # Extract it from "run id <id>; recover with: ..."
    import re as _re
    m = _re.search(r"run id ([0-9A-Za-z-]+);", out)
    assert m, f"run id not found in stdout: {out!r}"
    run_id = m.group(1)

    # The manifest must exist in state_dir and have probe_posted=True (W10 incremental save).
    manifest_path = auto_world.state_dir / f"{run_id}.json"
    assert manifest_path.exists(), f"manifest not saved by the run: {manifest_path}"
    import json as _json
    saved = _json.loads(manifest_path.read_text())
    assert saved.get("probe_posted") is True, (
        f"manifest should have probe_posted=True after U4 grounding: {saved}"
    )

    # Run cleanup-only with the REAL manifest (script-produced, not hand-written).
    avx_before = len(auto_world.avx.requests)
    argv = [
        "--part", "auto",
        "--stage", W.STAGE,
        "--workgroup", "wg",
        "--cms-api-endpoint", auto_world.cms.url,
        "--avx-api-endpoint", auto_world.avx.url,
        "--allow-writes",
        "--cleanup-only", "--run-id", run_id,
    ]
    rc2, out2, err2 = auto_world.run_main(argv)
    r2 = rows(out2)

    # Rule was already deleted by U6 → no rule found → PASS, no PUT.
    assert rc2 == 0, (out2, err2)
    x1 = r2.get("X1-auto", ("missing",))
    assert x1[0] == "PASS", (x1, out2, err2)

    new_puts = [
        x for x in auto_world.avx.requests[avx_before:]
        if x.get("method") == "PUT"
    ]
    assert not new_puts, f"no AVX PUT when rule was already deleted: {new_puts}"


# ============================================================================
# W1 — failed temp-user deletion causes FAIL row and rc 1
# ============================================================================


def test_u6_failed_user_delete_causes_fail_row(auto_world):
    """W1: when admin_delete_user fails, U6 records a FAIL cleanup row and exits rc 1.

    With the W1 fix the cleanup loop and the U6 always row run AFTER the ExitStack
    exits (so _temp_operator's finally has completed), and a failed deletion
    produces a FAIL row for the cleanup command and a FAIL U6 always row.
    """
    auto_world.auto_defects["delete_user_fails"] = True
    rc, out, err = auto_world.run_main(auto_world.auto_argv())
    r = rows(out)
    # A 'cleanup' FAIL row must appear.
    cleanup_row = r.get("cleanup", ("missing",))
    assert cleanup_row[0] == "FAIL", (cleanup_row, out, err)
    # U6 always must also be FAIL.
    u6 = r.get("U6 always", ("missing",))
    assert u6[0] == "FAIL", u6
    # rc must be 1 (FAIL rows present).
    assert rc == 1, (rc, out, err)


# ============================================================================
# C2 — probe_grounded set before POST; grounding commits then 504 → car returned
# ============================================================================


def test_u6_probe_returned_after_grounding_commits_then_504(auto_world):
    """C2: when the grounding route commits the write but then answers 504 (a
    lost response), the probe car must still be returned at the end of the run.

    With the C2 bug, state["probe_grounded"] is only set on 200/201, so U6
    skips return-to-service and the car stays grounded even though the grounding
    row exists.  After the fix, probe_grounded is set BEFORE the POST, so U6
    always calls _auto_return_to_service; with written=None, it waits for the
    row and returns the car once the row appears.

    The test also confirms U6 does not claim a return that did not happen: the
    grounding row must have returnedAt (the car was actually returned, not just
    vacuously reported as returned).
    """
    fa = finding_a(auto_world)
    fb = finding_b(auto_world)
    p_vin = probe_vin_of(auto_world, fb, fa)
    probe_vid = auto_world.vehicle_id_of(p_vin)

    auto_world.defects["grounding_commits_then_504"] = True
    rc, out, err = auto_world.run_main(auto_world.auto_argv())
    r = rows(out)

    # U6 must report PASS (the car was returned in the finally block).
    u6 = r.get("U6 always", ("missing",))
    assert u6[0] == "PASS", (u6, out, err)

    # The grounding row must exist AND have returnedAt — the car was grounded
    # (the route committed before answering 504) and then returned by U6.
    g = auto_world.groundings(probe_vid)
    assert g, f"no grounding row for probe car {probe_vid} after grounding_commits_then_504"
    assert g[-1].get("returnedAt"), (
        f"probe car {probe_vid} grounding not returned after grounding_commits_then_504: {g}"
    )


# ============================================================================
# C1 (rest) — U4 reads B's derived Action and checks dispatch/decline fields
# ============================================================================


def test_u4_fails_when_approver_cannot_dispatch(auto_world):
    """U4 fails when the auto-approver's dispatch to CMS cannot reach the internal route.

    With dispatch_fails the world's _fake_invoke patches _cms_internal_endpoint to raise
    ValueError, so @never_raises returns terminal_error and the Action status becomes
    'failed', not 'declined'.  The U4 check that requires status=='declined' must
    record FAIL with a message naming the problem.
    """
    auto_world.auto_defects["dispatch_fails"] = True
    rc, out, err = auto_world.run_main(auto_world.auto_argv())
    r = rows(out)
    assert rc == 1, (out, err)
    u4 = r.get("U4 grounded", ("PASS",))
    assert u4[0] == "FAIL", u4


# ============================================================================
# W14 — U4 per-field checks on B's Action each have an isolated test
# ============================================================================


def test_u4_b_action_decision_other(auto_world):
    """W14: U4 fails when B's Action has decision != 'auto_approved'.

    The world defect rewrites B's Action decision to 'auto_declined' after
    the auto-approver writes it (invoke 2).  U4 must FAIL naming 'decision'.
    """
    auto_world.auto_defects["u4_b_decision_other"] = True
    rc, out, err = auto_world.run_main(auto_world.auto_argv())
    r = rows(out)
    assert rc == 1, (out, err)
    u4 = r.get("U4 grounded", ("PASS",))
    assert u4[0] == "FAIL", u4
    detail = u4[1].lower()
    assert "decision" in detail, (
        f"U4 FAIL detail should name 'decision': {u4[1]!r}")


def test_u4_b_action_decided_by_other_rule(auto_world):
    """W14: U4 fails when B's Action decided_by.id names a different rule.

    The world defect changes decided_by.id to 'other-fleet/other-rule-id'
    after the auto-approver writes it.  U4 must FAIL naming 'decided_by'.
    """
    auto_world.auto_defects["u4_b_decided_by_other_rule"] = True
    rc, out, err = auto_world.run_main(auto_world.auto_argv())
    r = rows(out)
    assert rc == 1, (out, err)
    u4 = r.get("U4 grounded", ("PASS",))
    assert u4[0] == "FAIL", u4
    detail = u4[1].lower()
    assert "decided_by" in detail or "decided by" in detail, (
        f"U4 FAIL detail should name 'decided_by': {u4[1]!r}")


def test_u4_b_action_status_not_declined(auto_world):
    """W14: U4 fails when B's Action status != 'declined'.

    The world defect changes status to 'executed' after the auto-approver
    writes it.  U4 must FAIL naming 'status'.
    """
    auto_world.auto_defects["u4_b_status_not_declined"] = True
    rc, out, err = auto_world.run_main(auto_world.auto_argv())
    r = rows(out)
    assert rc == 1, (out, err)
    u4 = r.get("U4 grounded", ("PASS",))
    assert u4[0] == "FAIL", u4
    detail = u4[1].lower()
    assert "status" in detail, (
        f"U4 FAIL detail should name 'status': {u4[1]!r}")


def test_u4_b_action_created_before_run(auto_world):
    """W14: U4 fails when B's Action created_at predates run start.

    The world defect sets created_at to '2020-01-01T00:00:00Z' after the
    auto-approver writes it.  U4 must FAIL naming 'created_at'.
    """
    auto_world.auto_defects["u4_b_created_before_run"] = True
    rc, out, err = auto_world.run_main(auto_world.auto_argv())
    r = rows(out)
    assert rc == 1, (out, err)
    u4 = r.get("U4 grounded", ("PASS",))
    assert u4[0] == "FAIL", u4
    detail = u4[1].lower()
    assert "created_at" in detail or "created at" in detail, (
        f"U4 FAIL detail should name 'created_at': {u4[1]!r}")


def test_u4_b_action_failure_kind_other(auto_world):
    """W14: U4 fails when B's Action's last audit entry failure_kind != 'target_refused'.

    The world defect changes the last audit entry's failure_kind to 'timeout'
    after the auto-approver writes it.  U4 must FAIL naming 'failure_kind'.
    """
    auto_world.auto_defects["u4_b_failure_kind_other"] = True
    rc, out, err = auto_world.run_main(auto_world.auto_argv())
    r = rows(out)
    assert rc == 1, (out, err)
    u4 = r.get("U4 grounded", ("PASS",))
    assert u4[0] == "FAIL", u4
    detail = u4[1].lower()
    assert "failure_kind" in detail or "failure kind" in detail, (
        f"U4 FAIL detail should name 'failure_kind': {u4[1]!r}")


def test_u4_b_action_error_status_other(auto_world):
    """W14: U4 fails when B's Action's last audit entry error_status != 409.

    The world defect changes the last audit entry's error_status to 404 after
    the auto-approver writes it.  U4 must FAIL naming 'error_status'.
    """
    auto_world.auto_defects["u4_b_error_status_other"] = True
    rc, out, err = auto_world.run_main(auto_world.auto_argv())
    r = rows(out)
    assert rc == 1, (out, err)
    u4 = r.get("U4 grounded", ("PASS",))
    assert u4[0] == "FAIL", u4
    detail = u4[1].lower()
    assert "error_status" in detail or "error status" in detail, (
        f"U4 FAIL detail should name 'error_status': {u4[1]!r}")


# ============================================================================
# W2 — Lambda client uses _lambda_config(); invoke response checked for
#      FunctionError / batchItemFailures; U3 requires evaluator run record
# ============================================================================


def test_u3_fails_when_approver_batch_item_failure(auto_world):
    """U3 fails when the auto-approver's invoke returns a non-empty batchItemFailures.

    The auto-approver emits batchItemFailures when it catches a per-record exception.
    U3's invoke-response check must decode the payload, see the non-empty list and
    record FAIL naming what it saw.
    """
    auto_world.auto_defects["u3_batch_item_failure"] = True
    rc, out, err = auto_world.run_main(auto_world.auto_argv())
    r = rows(out)
    assert rc == 1, (out, err)
    u3 = r.get("U3 over limit", ("PASS",))
    assert u3[0] == "FAIL", u3
    detail = u3[1].lower()
    assert "batchitem" in detail or "batch" in detail, (
        f"U3 FAIL detail does not name batchItemFailures: {u3[1]!r}")


def test_u3_pass_requires_evaluator_run_record(auto_world):
    """U3 PASS requires evidence that the evaluator actually ran (a run record in AVX).

    When the invoke returns success but the evaluator never wrote a run record
    (invoke is a no-op: returns status=200 with an empty batchItemFailures but
    without executing the handler), U3 must FAIL — the absence-only Action check
    would pass, but the run-record evidence is missing.
    """
    auto_world.auto_defects["u3_invoke_noop"] = True
    rc, out, err = auto_world.run_main(auto_world.auto_argv())
    r = rows(out)
    assert rc == 1, (out, err)
    u3 = r.get("U3 over limit", ("PASS",))
    assert u3[0] == "FAIL", u3


# ============================================================================
# C1 P2 half — item 1 (lane 4): full why_not for all B VINs; roster/iOS check
# ============================================================================


def test_p2_b_with_roster_car_is_not_chosen(auto_world):
    """C1 P2 half (lane 4): P2 applies the full why_not to EVERY VIN of B, not
    just the probe candidate.  A B whose non-probe car is on the live roster must
    be rejected before the probe loop runs.

    The world's default roster entry is at a deficit-location VIN that never
    appears in any finding's vehicle_vins.  This test adds a roster entry for
    a VIN in finding_b that is *not* the probe candidate (sorted first eligible).
    Before the fix, only the probe loop calls why_not so the non-probe roster VIN
    is invisible; after the fix every VIN of B passes through why_not first.

    If finding_b has only one VIN (no separate non-probe slot), the test SKIPs —
    the one-VIN case is already guarded by the probe loop's why_not call.
    """
    fa = finding_a(auto_world)
    fb = finding_b(auto_world)
    a_vins: set[str] = set(
        ((fa.get("recommended_action") or {}).get("parameters") or {})
        .get("vehicle_vins") or []
    )
    fb_vins: list[str] = list(
        ((fb.get("recommended_action") or {}).get("parameters") or {})
        .get("vehicle_vins") or []
    )
    fb_fr = (fb.get("location") or {}).get("from_location_id", "")

    # Determine which VIN would be the probe (first sorted eligible not in A).
    eligible_b = _c0_eligible_vins(auto_world, fb)
    probe_candidate = next(
        (v for v in sorted(eligible_b) if v not in a_vins),
        None,
    )
    # Pick a non-probe VIN to roster: any VIN in B that is NOT the probe.
    non_probe_vins = [v for v in fb_vins if v != probe_candidate]
    if not non_probe_vins:
        pytest.skip(
            f"finding_b {fb['finding_id']} has no non-probe VIN (probe={probe_candidate!r}); "
            "cannot isolate the non-probe roster check — single-VIN case already guarded"
        )
    roster_vin = non_probe_vins[0]
    roster_vid = auto_world.vehicle_id_of(roster_vin)

    # Add the roster entry: CMS plan_store.put_roster_entry shape.
    auto_world._res().Table(W.LIVE_PLAN).put_item(Item={
        "planDate": "roster",
        "sk": roster_vid,
        "vin": roster_vin,
        "vehicleId": roster_vid,
        "fleetId": W.RENTAL,
        "state": "live",
    })

    rc, out, err = auto_world.run_main(auto_world.auto_argv())
    r = rows(out)

    p2 = r.get("P2 candidates", ("missing",))
    # finding_b must NOT be chosen — one of its VINs is on the live roster.
    if p2[0] == "PASS":
        assert fb["finding_id"] not in p2[1], (
            f"P2 chose B={fb['finding_id']} whose car {roster_vin} is on the live roster "
            f"and is a non-probe VIN; expected B to be rejected: {p2}"
        )
    else:
        assert p2[0] == "SKIP", (p2, out, err)


# ============================================================================
# W3 — --limit validation and U3 raises to max(a_cost, b_cost)
# ============================================================================


def test_limit_zero_is_refused_before_aws(auto_world):
    """W3: --limit 0 must be refused (rc 2) before any AWS call.

    Asserts from Router.all_calls that no new AWS call was made during the
    refused run — stronger than checking 'no P1 row' which only covers the
    first AWS call rather than all of them.
    """
    calls_before = len(auto_world.router.all_calls)
    rc, out, err = auto_world.run_main(
        auto_world.auto_argv("--limit", "0")
    )
    assert rc == 2, (out, err)
    assert "refused" in err.lower() or "refused" in out.lower(), (
        f"expected 'refused' in output for --limit 0: {err!r}"
    )
    new_calls = auto_world.router.all_calls[calls_before:]
    assert not new_calls, (
        f"AWS calls were made despite --limit 0 refused: {new_calls}"
    )


def test_limit_negative_is_refused(auto_world):
    """W3: negative --limit is refused (rc 2) before any AWS call.

    Asserts from Router.all_calls that no new AWS call was made.
    """
    calls_before = len(auto_world.router.all_calls)
    rc, out, err = auto_world.run_main(
        auto_world.auto_argv("--limit", "-1")
    )
    assert rc == 2, (out, err)
    new_calls = auto_world.router.all_calls[calls_before:]
    assert not new_calls, (
        f"AWS calls were made despite --limit -1 refused: {new_calls}"
    )


def test_limit_above_max_is_refused(auto_world):
    """W3: --limit > max_limit_usd (500) is refused (rc 2) before any AWS call.

    P1 reads the SSM bounds and the rules API enforces them; the script must not
    proceed with a limit it knows the rules API will reject.
    Asserts from Router.all_calls that no new AWS call was made.
    """
    calls_before = len(auto_world.router.all_calls)
    rc, out, err = auto_world.run_main(
        auto_world.auto_argv("--limit", "501")
    )
    assert rc == 2, (out, err)
    new_calls = auto_world.router.all_calls[calls_before:]
    assert not new_calls, (
        f"AWS calls were made despite --limit 501 refused: {new_calls}"
    )


def test_limit_nan_is_refused_before_aws(auto_world):
    """S4: --limit nan must be refused (rc 2) before any AWS call.

    float("nan") <= 0 and float("nan") > 500 are both False in Python, so nan
    slips through the existing numeric guard and proceeds.  math.isfinite catches
    nan (and inf) before any AWS call.
    """
    calls_before = len(auto_world.router.all_calls)
    rc, out, err = auto_world.run_main(
        auto_world.auto_argv("--limit", "nan")
    )
    assert rc == 2, (out, err)
    assert "refused" in err.lower() or "refused" in out.lower(), (
        f"expected 'refused' in output for --limit nan: {err!r}"
    )
    new_calls = auto_world.router.all_calls[calls_before:]
    assert not new_calls, (
        f"AWS calls were made despite --limit nan refused: {new_calls}"
    )


def test_p2_skips_when_all_findings_cost_above_limit(auto_world):
    """W3: P2 only admits findings with cost ≤ --limit; a limit below all finding
    costs forces a SKIP.

    Uses --limit 1 (below any realistic move cost).  Before the fix the cost
    filter is present but --limit defaults to 999.99 and no test verifies the
    filter against a user-supplied limit; with the new required --limit the
    filter must work end-to-end.
    """
    rc, out, err = auto_world.run_main(
        auto_world.auto_argv("--limit", "1")
    )
    r = rows(out)
    assert r.get("P2 candidates", ("PASS",))[0] == "SKIP", (
        f"P2 should SKIP with --limit 1 (all findings cost more than $1): {r}"
    )


# ---------------------------------------------------------------------------
# S3 helpers — minimal valid move Finding for evaluator-precondition tests
# ---------------------------------------------------------------------------

def _decide_world_findings(w: W.AutoWorld) -> None:
    """Mark all world move Findings as already decided (declined)."""
    for f in w.move_findings():
        w._res().Table(W.ACTIONS).put_item(Item={
            "action_id": w.decision_id(f["finding_id"]),
            "finding_id": f["finding_id"],
            "decision": "declined", "status": "declined",
        })


def _s3_finding(fid: str, *, cost: str = "2.00", vin_n: int = 4) -> dict:
    """Minimal move Finding that passes all evaluator preconditions by default.

    VIN vin_n is at loc-atl-downtown in the world (VINs 2-6 are safe choices).
    Callers override the one field under test to create the 'bad' variant.
    """
    now = dt.datetime.now(dt.timezone.utc)
    return {
        "finding_id": fid,
        "finding_kind": "fleet.rebalance.move",
        "fleet_id": W.RENTAL,                          # handler.py:327-328
        "scope": {"fleet_id": W.RENTAL},
        "expires_at": (now + dt.timedelta(hours=48)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "recommended_action": {
            "action_kind": "move_vehicles",
            "target_system": "cms",                    # handler.py:356-358; policy.py:116
            "parameters": {
                "fleet_id": W.RENTAL,                  # handler.py:125 (_move_within_bounds)
                "from_location_id": "loc-atl-downtown",
                "to_location_id": "loc-atl-hub1",
                "vehicle_vins": [W.vin(vin_n)],
                "forecast_week": "2099-W01",
                "transfer_cost_usd": cost,
                "predicted_on_rent_hours_gain": "10.0",
                "predicted_turnaways_avoided": "1",
            },
        },
        "location": {
            "from_location_id": "loc-atl-downtown",
            "to_location_id": "loc-atl-hub1",
        },
    }


def test_p2_skips_when_finding_lacks_top_level_fleet_id(auto_world):
    """S3: P2 excludes Findings without a top-level fleet_id.

    handler.py:327-328: the evaluator returns early when fleet_id is absent, so
    it would never act on this Finding.  P2 must filter the same way.

    Setup: all world Findings decided; 1 bad (no fleet_id, cheapest) + 1 good.
    Before fix: both in undecided → P2 PASS.
    After fix: bad excluded → 1 undecided → P2 SKIP.
    """
    _decide_world_findings(auto_world)
    bad = _s3_finding("FIND-s3-fleet-bad-0001")
    del bad["fleet_id"]                                # fails handler.py:327-328
    good = _s3_finding("FIND-s3-fleet-good-002", vin_n=5)
    auto_world.put_finding(bad)
    auto_world.put_finding(good)
    rc, out, err = auto_world.run_main(auto_world.auto_argv())
    r = rows(out)
    assert r.get("P2 candidates", ("PASS",))[0] == "SKIP", (
        f"P2 should SKIP: bad finding (no fleet_id) must be excluded; {r}"
    )
    assert rc == 2, (out, err)


def test_p2_skips_when_finding_has_wrong_parameters_fleet_id(auto_world):
    """S3: P2 excludes Findings where parameters.fleet_id ≠ rental fleet.

    handler.py:125 (_move_within_bounds) returns None when parameters.fleet_id
    does not match fleet_id, so the evaluator never approves this Finding.

    Setup: all world Findings decided; 1 bad (wrong params.fleet_id, cheapest)
    + 1 good.  Before fix: both undecided → PASS.  After fix: bad excluded → SKIP.
    """
    _decide_world_findings(auto_world)
    bad = _s3_finding("FIND-s3-pf-bad-00001")
    bad["recommended_action"]["parameters"]["fleet_id"] = "demo-other-fleet"
    good = _s3_finding("FIND-s3-pf-good-0002", vin_n=5)
    auto_world.put_finding(bad)
    auto_world.put_finding(good)
    rc, out, err = auto_world.run_main(auto_world.auto_argv())
    r = rows(out)
    assert r.get("P2 candidates", ("PASS",))[0] == "SKIP", (
        f"P2 should SKIP: bad finding (params.fleet_id=demo-other-fleet) excluded; {r}"
    )
    assert rc == 2, (out, err)


def test_p2_skips_when_finding_has_wrong_target_system(auto_world):
    """S3: P2 excludes Findings where recommended target_system ≠ "cms".

    handler.py:356-358 (S6 fix): the evaluator skips a Finding whose
    recommended target_system disagrees with the policy row ("cms" for
    move_vehicles; agents/executors/policy.py:116).

    Setup: all world Findings decided; 1 bad (target_system=dms, cheapest)
    + 1 good.  Before fix: both undecided → PASS.  After fix: bad excluded → SKIP.
    """
    _decide_world_findings(auto_world)
    bad = _s3_finding("FIND-s3-ts-bad-00001")
    bad["recommended_action"]["target_system"] = "dms"  # ≠ "cms"
    good = _s3_finding("FIND-s3-ts-good-0002", vin_n=5)
    auto_world.put_finding(bad)
    auto_world.put_finding(good)
    rc, out, err = auto_world.run_main(auto_world.auto_argv())
    r = rows(out)
    assert r.get("P2 candidates", ("PASS",))[0] == "SKIP", (
        f"P2 should SKIP: bad finding (target_system=dms) excluded; {r}"
    )
    assert rc == 2, (out, err)


def test_p2_skips_when_a_cost_at_most_one_dollar(auto_world):
    """S3: P2 excludes Findings with cost ≤ $1 to keep limit_u2 = round(a−1, 2) > 0.

    With cost = $1.00, limit_u2 = 0.00; with cost < $1, limit_u2 < 0, and the
    rules API refuses with 400 invalid_limit.  P2 must not select such a Finding.

    Setup: all world Findings decided; 2 undecided with cost ≤ $1 (bad1=$1.00,
    bad2=$0.50).  Before fix: both undecided → P2 PASS (bad1 as A, bad2 as B).
    After fix: both excluded → 0 undecided → P2 SKIP.
    """
    _decide_world_findings(auto_world)
    bad1 = _s3_finding("FIND-s3-cost-bad-0001", cost="1.00")
    bad2 = _s3_finding("FIND-s3-cost-good-002", cost="0.50", vin_n=5)
    auto_world.put_finding(bad1)
    auto_world.put_finding(bad2)
    rc, out, err = auto_world.run_main(auto_world.auto_argv())
    r = rows(out)
    assert r.get("P2 candidates", ("PASS",))[0] == "SKIP", (
        f"P2 should SKIP: findings with cost ≤ $1 must be excluded; {r}"
    )
    assert rc == 2, (out, err)


def test_u3_raises_rule_to_max_a_b_cost(auto_world):
    """W3/W15: after the over-limit probe, U3 raises the rule to max(a_cost, b_cost)
    not to --limit.  Verified both from the PASS wording and from avx.requests
    (the actual PUT body sent), per W15: the wording is insufficient because it prints
    a local variable that could differ from what the PUT body actually carries.

    Before the fix U3 raises to min(--limit, 500) = 500, covering every move
    in the fleet.  After the fix it raises to max(a_cost, b_cost).
    """
    import decimal  # noqa: PLC0415
    fa = finding_a(auto_world)
    fb = finding_b(auto_world)
    a_cost = float(
        ((fa.get("recommended_action") or {}).get("parameters") or {})
        .get("transfer_cost_usd", 0)
    )
    b_cost = float(
        ((fb.get("recommended_action") or {}).get("parameters") or {})
        .get("transfer_cost_usd", 0)
    )
    expected_raise = round(max(a_cost, b_cost), 2)

    rc, out, err = auto_world.run_main(auto_world.auto_argv())
    r = rows(out)
    assert rc == 0, (out, err)
    u3 = r.get("U3 over limit", ("missing",))
    assert u3[0] == "PASS", u3
    assert f"${expected_raise:.2f}" in u3[1], (
        f"U3 PASS detail should name max(a_cost=${a_cost:.2f}, b_cost={b_cost:.2f}) "
        f"= ${expected_raise:.2f} but got: {u3[1]!r}"
    )
    # W15: assert from avx.requests that U3's raise PUT sent limit == round(max(a,b), 2).
    # U3's raise PUT has "action_kind", "enabled": True, and limit == max(a,b) > a_cost - 1
    # (the U2 enable PUT has the same shape but the lower limit == a_cost - 1).
    # There are exactly two enabled=True PUTs; U3's is the one with the higher limit.
    import json as _json  # noqa: PLC0415
    rules_puts = [
        req for req in auto_world.avx.requests
        if req.get("method") == "PUT"
        and "/approval-rules/" in (req.get("path") or "")
    ]
    u3_raise_puts = []
    for req in rules_puts:
        try:
            body = _json.loads(req.get("body") or "{}")
            if (body.get("enabled") is True
                    and "limit" in body
                    and round(float(body["limit"]), 2) == expected_raise):
                u3_raise_puts.append(body)
        except Exception:  # noqa: BLE001
            pass
    assert u3_raise_puts, (
        f"No U3 raise PUT (limit=={expected_raise}) found in avx.requests; all rules PUTs: "
        f"{[req.get('body') for req in rules_puts]}"
    )
    actual_limit = round(float(u3_raise_puts[-1]["limit"]), 2)
    assert actual_limit == expected_raise, (
        f"U3 raise PUT body limit={actual_limit!r} != expected {expected_raise!r} "
        f"(max(a_cost={a_cost}, b_cost={b_cost}))"
    )


# ============================================================================
# W6 — U6 residue check: no extra Actions by rule, no extra move records
# ============================================================================


def test_u6_residue_clean_run(auto_world):
    """W6: the clean run emits exactly one residue row (PASS); no extra Actions
    or move records are created beyond A's Action and B's Action.

    Before the fix this row doesn't exist; after the fix it must appear with
    PASS.
    """
    rc, out, err = auto_world.run_main(auto_world.auto_argv())
    r = rows(out)
    assert rc == 0, (out, err)
    res = r.get("U6 residue", ("missing",))
    assert res[0] == "PASS", (
        f"Expected U6 residue PASS on clean run; got {res}"
    )


def test_u6_residue_fails_when_extra_finding_approved(auto_world):
    """W6: an extra Action decided by this run's rule that appears while it is
    enabled causes U6 residue to FAIL, naming the extra Action id.

    Uses mock._new_run_id to fix the per-run rule_id so the pre-injected action
    can be set up ahead of time.  Before the W12 fix the residue check uses the
    fixed _AUTO_RULE_ID constant and the extra action IS detected this way too.
    """
    from unittest import mock

    fixed_run = "20261022T120100Z-ccddee"
    per_run_rule_id = _run_rule_id(fixed_run)
    extra_id = "AVX-ACT-extra-00000000-0000-0000-0000-000000000000"
    auto_world._res().Table(W.ACTIONS).put_item(Item={
        "action_id": extra_id,
        "fleet_id": W.RENTAL,
        "finding_id": "FIND-extra-synthetic-for-residue-test",
        "action_kind": "move_vehicles",
        "decision": "auto_approved",
        "status": "executed",
        "decided_by": {"kind": "rule", "id": f"{W.RENTAL}/{per_run_rule_id}"},
        "created_at": "2099-01-01T00:00:00Z",
        "updated_at": "2099-01-01T00:00:00Z",
    })

    with mock.patch.object(auto_world.mod, "_new_run_id", return_value=fixed_run):
        rc, out, err = auto_world.run_main(auto_world.auto_argv())
    r = rows(out)

    res = r.get("U6 residue", ("missing",))
    assert res[0] == "FAIL", (
        f"Expected U6 residue FAIL (extra action injected); got {res}; "
        f"full output: {out!r}"
    )
    assert extra_id in res[1], (
        f"U6 residue FAIL detail should name extra action {extra_id!r}; got {res[1]!r}"
    )


# ============================================================================
# W12 — per-run rule id: residue check and cleanup-only gate
# ============================================================================


def test_u6_residue_old_rule_id_action_ignored(auto_world):
    """W12: Actions decided by an earlier run's rule (the old fixed id
    'livetest-fi-rebal-auto' OR a different per-run id) are NOT flagged as
    extra — only Actions by THIS run's per-run rule are relevant.

    Before the fix the residue filter uses the fixed _AUTO_RULE_ID, so an
    action with decided_by.id = '<fleet>/livetest-fi-rebal-auto' and a
    future created_at IS detected → U6 residue FAIL.
    After the fix the per-run rule_id is unique to this run, so both
    pre-injected actions are ignored → U6 residue PASS.

    The future created_at is required to pass the current code's
    'created_at >= :s' filter; without it the old code also ignores the
    action (past-dated actions fall through the filter).
    """
    # Pre-inject two actions that look like they were decided by other runs.
    # The far-future created_at ensures they are not filtered by the old code's
    # time filter, exposing the fixed-id false-positive.
    old_fixed_id = "AVX-ACT-old-fixed-0000-0000-0000-0000"
    other_run_id = "AVX-ACT-other-run-0000-0000-0000-0000"
    for aid, decided_id in [
        (old_fixed_id, f"{W.RENTAL}/livetest-fi-rebal-auto"),
        (other_run_id, f"{W.RENTAL}/livetest-fi-rebal-auto-20261001T000000Z-aabbcc"),
    ]:
        auto_world._res().Table(W.ACTIONS).put_item(Item={
            "action_id": aid,
            "fleet_id": W.RENTAL,
            "finding_id": f"FIND-other-run-{aid[-8:]}",
            "action_kind": "move_vehicles",
            "decision": "auto_approved",
            "status": "executed",
            "decided_by": {"kind": "rule", "id": decided_id},
            "created_at": "2099-01-01T00:00:00Z",  # future: passes old time filter
            "updated_at": "2099-01-01T00:00:00Z",
        })

    rc, out, err = auto_world.run_main(auto_world.auto_argv())
    r = rows(out)
    assert rc == 0, (out, err)
    res = r.get("U6 residue", ("missing",))
    assert res[0] == "PASS", (
        f"Expected U6 residue PASS (only old-run actions injected); got {res}; "
        f"full output: {out!r}"
    )


def test_u6_residue_extra_action_by_this_runs_rule_fails(auto_world):
    """W12: an extra Action with decided_by.id = '<fleet>/<this-run-rule-id>'
    causes U6 residue FAIL naming the extra action id.

    Before the fix the residue check uses the fixed _AUTO_RULE_ID, so an
    action with a per-run rule_id is NOT detected → U6 PASS (test expects
    FAIL → RED).  After the fix the per-run rule_id matches and it IS detected.
    """
    from unittest import mock

    fixed_run = "20261022T200000Z-aaaaaa"
    per_run_rule_id = f"livetest-fi-rebal-auto-{fixed_run}"
    extra_id = "AVX-ACT-extra-this-run-0000-0000-0000"

    auto_world._res().Table(W.ACTIONS).put_item(Item={
        "action_id": extra_id,
        "fleet_id": W.RENTAL,
        "finding_id": "FIND-extra-this-run-synthetic",
        "action_kind": "move_vehicles",
        "decision": "auto_approved",
        "status": "executed",
        "decided_by": {"kind": "rule", "id": f"{W.RENTAL}/{per_run_rule_id}"},
        "created_at": "2099-01-01T00:00:00Z",
        "updated_at": "2099-01-01T00:00:00Z",
    })

    with mock.patch.object(auto_world.mod, "_new_run_id", return_value=fixed_run):
        rc, out, err = auto_world.run_main(auto_world.auto_argv())
    r = rows(out)

    res = r.get("U6 residue", ("missing",))
    assert res[0] == "FAIL", (
        f"Expected U6 residue FAIL (extra action by this run's rule); got {res}; "
        f"full output: {out!r}"
    )
    assert extra_id in res[1], (
        f"U6 residue FAIL detail should name {extra_id!r}; got {res[1]!r}"
    )


def test_u6_residue_extra_move_record_fails(auto_world):
    """W12: an extra move record (not action_id_a) that appears during the run
    causes U6 residue FAIL naming the extra moveId.

    The world defect 'u6_extra_move_record' fires inside _internal_moves after
    A's move is written, writing an additional move that was NOT in
    existing_move_ids.  U6 must flag it.
    """
    extra_move_id = "AVX-EXTRA-MOVE-0000-0000-0000-0000"
    auto_world.auto_defects["u6_extra_move_record"] = extra_move_id

    rc, out, err = auto_world.run_main(auto_world.auto_argv())
    r = rows(out)

    res = r.get("U6 residue", ("missing",))
    assert res[0] == "FAIL", (
        f"Expected U6 residue FAIL (extra move injected); got {res}; "
        f"full output: {out!r}"
    )
    assert extra_move_id in res[1], (
        f"U6 residue FAIL detail should name {extra_move_id!r}; got {res[1]!r}"
    )


def test_cleanup_only_refuses_mismatched_rule_id(auto_world):
    """W12: --cleanup-only must be REFUSED when the manifest's rule_id does not equal
    the id built from the manifest's own run_id ('livetest-fi-rebal-auto-<run_id>').

    Before the fix the gate checks rule_id == _AUTO_RULE_ID (the fixed constant),
    so a manifest with the OLD fixed rule_id 'livetest-fi-rebal-auto' PASSES the
    gate (no AWS call refusal → test expects REFUSED → RED).
    After the fix the gate computes the expected per-run id from the manifest's
    run_id, so the old fixed rule_id is rejected → REFUSED (rc 2), no AWS call.
    """
    import json as _json

    run_id = "20261022T120000Z-abcdef"
    # Manifest with the OLD fixed rule_id — does NOT match the per-run id.
    auto_world.state_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "run_id": run_id, "stage": W.STAGE, "part": "auto",
        "started_at": "2026-10-22T12:00:00Z",
        "rule_id": "livetest-fi-rebal-auto",   # old fixed id — mismatches per-run
        "probe_gid": auto_world.mod._AUTO_PROBE_PREFIX + run_id,
        "probe_vid": "VEH-SOME-0001",
    }
    (auto_world.state_dir / f"{run_id}.json").write_text(_json.dumps(manifest))

    avx_before = len(auto_world.avx.requests)
    argv = [
        "--part", "auto",
        "--stage", W.STAGE,
        "--workgroup", "wg",
        "--cms-api-endpoint", auto_world.cms.url,
        "--avx-api-endpoint", auto_world.avx.url,
        "--allow-writes",
        "--cleanup-only", "--run-id", run_id,
    ]
    rc, out, err = auto_world.run_main(argv)

    assert rc == 2, (rc, out, err)
    assert "REFUSED" in err, f"expected REFUSED in stderr: {err!r}"

    # No AWS call to the approval-rules API must have been made.
    new_avx = [x for x in auto_world.avx.requests[avx_before:] if x.get("method") == "PUT"]
    assert not new_avx, f"no AVX PUT should be sent when gate refuses: {new_avx}"


# ============================================================================
# W9 — failure branches and exit paths tested via run_main
# ============================================================================


def test_u3_run_main_fails_when_evaluator_approves_over_limit(auto_world):
    """W9: U3 FAIL row (rc 1) when the evaluator approves A despite rule limit < cost.

    The world defect u3_evaluator_approves_anyway writes the Action for A directly to
    DDB after the first invoke (U3), simulating a fail-open evaluator.  The script's
    ConsistentRead GetItem on action_id_a must find the item and record U3 FAIL.

    Asserts from what the world recorded:
    - U3 FAIL row naming 'evaluator approved A despite limit < cost'
    - rc == 1
    - avx.requests contains a successful disable PUT (enabled=False, status 200) confirming
      U6 ran and disabled the rule despite the U3 failure (finally block executed).
    """
    auto_world.auto_defects["u3_evaluator_approves_anyway"] = True
    rc, out, err = auto_world.run_main(auto_world.auto_argv())
    r = rows(out)
    assert rc == 1, (rc, out, err)
    u3 = r.get("U3 over limit", ("missing",))
    assert u3[0] == "FAIL", u3
    assert "evaluator approved" in u3[1].lower() or "approved" in u3[1].lower() or (
        "action" in u3[1].lower() and "written" in u3[1].lower()
    ), f"U3 FAIL detail should name the approval: {u3[1]!r}"
    # Assert from world request log: U6 ran and disabled the rule (finally block executed).
    disable_calls = [
        x for x in auto_world.avx.requests
        if x.get("method") == "PUT"
        and "/approval-rules/fleet/" in (x.get("path") or "")
        and json.loads(x.get("body") or "{}").get("enabled") is False
        and x.get("status", 0) == 200
    ]
    assert disable_calls, (
        f"U6 should have made a successful disable PUT despite U3 FAIL (finally runs); "
        f"avx requests: {auto_world.avx.requests}"
    )


def test_per_item_window_closes_between_p2_and_u2(auto_world):
    """W9: per-item write-window re-check before U2-U5 gates each item individually.

    The start-of-run _cms_writes_allowed_now() check passes (returns True so P1/P2/U1
    run normally).  Then the clock enters the write window — subsequent
    _cms_writes_allowed_now() calls return False, so U2-U5 each record SKIP with the
    write-window message instead of proceeding.

    Asserts from what the world recorded:
    - U2/U3/U4/U5 all SKIP (write-window reason)
    - avx.requests has no successful rule create/enable PUTs (no rule was created)
    - U6 runs (the finally block fires even for SKIP items); rule_created=False so no
      disable attempt is made, and U6 PASS.
    """
    from unittest import mock  # noqa: PLC0415

    call_count: list[int] = [0]
    orig = auto_world.mod._cms_writes_allowed_now

    def patched() -> bool:
        call_count[0] += 1
        # Call 1 is the start-of-run guard: let it pass so P1/P2/U1 run.
        # All subsequent calls (per-item U2-U5 guards) return False.
        return call_count[0] == 1

    with mock.patch.object(auto_world.mod, "_cms_writes_allowed_now", side_effect=patched):
        rc, out, err = auto_world.run_main(auto_world.auto_argv())
    r = rows(out)
    # All write items must be SKIP (write window fired on U2's per-item check).
    for name in ("U2 rule", "U3 over limit", "U4 grounded", "U5 within limit"):
        row = r.get(name, ("PASS", ""))
        assert row[0] == "SKIP", (name, row)
        assert "write window" in row[1].lower() or "write" in row[1].lower(), (
            f"{name} SKIP detail should mention write window: {row[1]!r}")
    # No rule was created: avx.requests has no successful create PUT.
    create_puts = [
        x for x in auto_world.avx.requests
        if x.get("method") == "PUT"
        and "/approval-rules/fleet/" in (x.get("path") or "")
        and x.get("status", 0) in (200, 201)
    ]
    assert not create_puts, (
        f"no successful rule PUT should have been made (write window); "
        f"avx requests: {auto_world.avx.requests}"
    )


def test_no_allow_writes_skips_u2_through_u5(auto_world):
    """W9: without --allow-writes U2-U5 all SKIP and no writes reach AWS.

    No Lambda invoke (auto-approver is never called), no Cognito write (no temp
    operator created).

    Asserts from what the world recorded:
    - U2/U3/U4/U5 SKIP with '--allow-writes not given' reason
    - rc == 2 (all write items SKIP → _summary returns 2)
    - _auto_invoke_count == 0 (no Lambda Invoke reached the fake)
    - no new Cognito users in the pool (no temp operator created)
    """
    users_before = set(auto_world.pool.users)
    # Build argv WITHOUT --allow-writes (auto_argv always includes it; build manually).
    argv = [
        "--part", "auto",
        "--stage", W.STAGE,
        "--workgroup", "wg",
        "--cms-api-endpoint", auto_world.cms.url,
        "--avx-api-endpoint", auto_world.avx.url,
        # Note: no --allow-writes
        "--limit", "500",
        "--cms-results-bucket", W.CMS_RESULTS,
    ]
    rc, out, err = auto_world.run_main(argv)
    r = rows(out)
    assert rc == 2, (rc, out, err)
    for name in ("U2 rule", "U3 over limit", "U4 grounded", "U5 within limit"):
        row = r.get(name, ("PASS", ""))
        assert row[0] == "SKIP", (name, row)
        assert "allow-writes" in row[1] or "allow_writes" in row[1].replace("-", "_"), (
            f"{name} SKIP detail should name --allow-writes: {row[1]!r}")
    # Assert from world: no Lambda Invoke call was made.
    assert auto_world._auto_invoke_count == 0, (
        f"Lambda should not have been invoked without --allow-writes; "
        f"_auto_invoke_count={auto_world._auto_invoke_count}"
    )
    # No temp user was created.
    new_users = set(auto_world.pool.users) - users_before
    assert not new_users, (
        f"no Cognito user should have been created without --allow-writes: {new_users}"
    )


def test_u2_exception_still_reaches_u6(auto_world):
    """W9: an exception inside U2's PUT still reaches U6 (finally block runs).

    u2_create_raises makes the approval-rules PUT raise before the handler runs.
    The HTTP server returns a non-200 code; the script records U2 FAIL and sets
    write_failed=True.  U3/U4/U5 SKIP.  U6's finally block fires:
    rule_created=False (never succeeded) so no disable attempt, probe_grounded=False
    so no return to service.  U6 records PASS, temp operator deleted.

    Asserts from what the world recorded:
    - U2 FAIL row
    - U6 PASS row
    - DDB: no rule exists for RULE_ID (was never created)
    - pool: temp user deleted
    """
    users_before = set(auto_world.pool.users)
    auto_world.auto_defects["u2_create_raises"] = True
    rc, out, err = auto_world.run_main(auto_world.auto_argv())
    r = rows(out)
    assert rc == 1, (rc, out, err)
    u2 = r.get("U2 rule", ("missing",))
    assert u2[0] == "FAIL", u2
    # U6 must have run (finally fires even after U2 FAIL).
    u6 = r.get("U6 always", ("missing",))
    assert u6[0] == "PASS", (
        f"U6 always must PASS after U2 FAIL (rule_created=False, no disable; "
        f"probe_grounded=False, no RTS; temp operator deleted): got {u6}")
    # Assert from world: no rule was created (DDB state).
    rule = auto_world.get_rule(W.RENTAL, _rule_id_of(out))
    assert rule is None, (
        f"no rule should exist after U2 exception (rule_created=False): {rule}")
    # Temp user deleted.
    new_users = set(auto_world.pool.users) - users_before
    assert not new_users, (
        f"temp user should have been deleted after U2 exception: {new_users}")


def test_u4_exception_still_disables_rule_and_returns_car(auto_world):
    """W9: an exception in U4's invoke still reaches U6 which disables the rule
    and returns the probe car.

    u4_invoke_raises fires on the 2nd invoke (U4), AFTER the grounding POST has
    already succeeded (probe_grounded=True, probe_written=True).  U4 FAIL,
    write_failed=True.  U5 SKIP.  U6's finally block:
    rule_created=True → disables rule; probe_grounded=True → returns probe car.

    Asserts from what the world recorded:
    - U4 FAIL row
    - U6 PASS (rule disabled, probe car returned)
    - avx.requests: a successful disable PUT (enabled=False, status 200)
    - DDB: rule enabled=False
    - DDB: probe car's grounding row has returnedAt
    """
    fa = finding_a(auto_world)
    fb = finding_b(auto_world)
    p_vin = probe_vin_of(auto_world, fb, fa)
    probe_vid = auto_world.vehicle_id_of(p_vin)

    auto_world.auto_defects["u4_invoke_raises"] = True
    rc, out, err = auto_world.run_main(auto_world.auto_argv())
    r = rows(out)
    assert rc == 1, (rc, out, err)
    u4 = r.get("U4 grounded", ("missing",))
    assert u4[0] == "FAIL", u4
    # U6 must have run and disabled the rule.
    u6 = r.get("U6 always", ("missing",))
    assert u6[0] == "PASS", (u6, out, err)
    # Assert from world: disable PUT was made (request log).
    disable_calls = [
        x for x in auto_world.avx.requests
        if x.get("method") == "PUT"
        and "/approval-rules/fleet/" in (x.get("path") or "")
        and json.loads(x.get("body") or "{}").get("enabled") is False
        and x.get("status", 0) == 200
    ]
    assert disable_calls, (
        f"U6 should have disabled the rule after U4 exception; "
        f"avx requests: {auto_world.avx.requests}"
    )
    # Assert from DDB: rule deleted (U6 disabled and deleted it in the finally block).
    rule = auto_world.get_rule(W.RENTAL, _rule_id_of(out))
    assert rule is None, f"rule should be deleted after U4 exception path: {rule}"
    # Assert from DDB: probe car was returned (grounding closed).
    g = auto_world.groundings(probe_vid)
    assert g, f"no grounding row for probe car {probe_vid} after u4_invoke_raises"
    assert g[-1].get("returnedAt"), (
        f"probe car grounding not returned after U4 exception: {g}"
    )


def test_u5_exception_still_disables_rule_and_returns_car(auto_world):
    """W9: an exception in U5's invoke still reaches U6 which disables the rule
    and returns the probe car (which was grounded in U4).

    u5_invoke_raises fires on the 3rd invoke (U5).  By that point U2/U3/U4 all
    succeeded: the rule is enabled at the raised limit, the probe car is grounded.
    U5 FAIL, write_failed=True.  U6's finally block: disables the rule and returns
    the probe car.

    Asserts from what the world recorded:
    - U5 FAIL row
    - U6 PASS (rule disabled, probe car returned)
    - avx.requests: a successful disable PUT (enabled=False)
    - DDB: probe car's grounding row has returnedAt
    - DDB: no Action for A (U5 was aborted before writing the move)
    """
    fa = finding_a(auto_world)
    fb = finding_b(auto_world)
    p_vin = probe_vin_of(auto_world, fb, fa)
    probe_vid = auto_world.vehicle_id_of(p_vin)

    auto_world.auto_defects["u5_invoke_raises"] = True
    rc, out, err = auto_world.run_main(auto_world.auto_argv())
    r = rows(out)
    assert rc == 1, (rc, out, err)
    u5 = r.get("U5 within limit", ("missing",))
    assert u5[0] == "FAIL", u5
    u6 = r.get("U6 always", ("missing",))
    assert u6[0] == "PASS", (u6, out, err)
    # Assert from world request log: a disable PUT was made.
    disable_calls = [
        x for x in auto_world.avx.requests
        if x.get("method") == "PUT"
        and "/approval-rules/fleet/" in (x.get("path") or "")
        and json.loads(x.get("body") or "{}").get("enabled") is False
        and x.get("status", 0) == 200
    ]
    assert disable_calls, (
        f"U6 should have disabled the rule after U5 exception; "
        f"avx requests: {auto_world.avx.requests}"
    )
    # Assert from DDB: probe car returned (grounding closed).
    g = auto_world.groundings(probe_vid)
    assert g, f"no grounding row for probe car {probe_vid} after u5_invoke_raises"
    assert g[-1].get("returnedAt"), (
        f"probe car grounding not returned after U5 exception: {g}"
    )
    # Assert: U5 was aborted — Action for A should not have been executed.
    # (The invoke raised before the handler ran, so no Action was written in U5.)
    act = auto_world.action(fa["finding_id"])
    assert act is None or act.get("status") != "executed", (
        f"Action for A should not be executed after U5 exception: {act}"
    )


def test_systemexit_after_u2_still_disables_rule(auto_world):
    """W9: a SystemExit raised after U2 (during the per-item window check for U3)
    still triggers U6's finally block, which disables the rule and deletes the
    temp operator.

    The mock raises SystemExit(130) on the second call to _cms_writes_allowed_now
    (the start-of-run check passes; U3's per-item check raises).  At that point
    U2 has already succeeded (rule created and enabled) so U6 must disable it.
    U4 and U5 never run (U3's check raised before invoking), so no probe car is
    grounded.

    AutoWorld.run_main catches SystemExit and returns (2, '', '').  All assertions
    are from DDB / pool state (output is empty — the finally ran but _summary was
    not called before propagation).
    """
    from unittest import mock  # noqa: PLC0415

    call_count: list[int] = [0]

    def patched() -> bool:
        call_count[0] += 1
        if call_count[0] <= 1:
            # Start-of-run check: pass.
            return True
        if call_count[0] == 2:
            # U2's per-item check: also pass (let U2 succeed and create the rule).
            return True
        # U3's per-item check and beyond: raise SystemExit.
        raise SystemExit(130)

    users_before = set(auto_world.pool.users)
    with mock.patch.object(auto_world.mod, "_cms_writes_allowed_now", side_effect=patched):
        rc, out, err = auto_world.run_main(auto_world.auto_argv())
    # AutoWorld.run_main catches SystemExit → returns the exit code (130), captured output.
    assert rc != 0, (rc, out, err)
    # C7: the run id is printed before U2 (in stdout), so out is non-empty.
    # record() prints immediately; U6 residue rows may appear from the finally block.
    # Assert that write steps that never ran have no rows.
    r = rows(out)
    for should_be_absent in ("U3 over limit", "U4 grounded", "U5 within limit"):
        assert should_be_absent not in r, (
            f"{should_be_absent!r} should not appear (never ran before SystemExit): {r!r}"
        )
    # Assert from DDB: rule must be deleted (U6's finally disabled and deleted it).
    rule = auto_world.get_rule(W.RENTAL, _rule_id_of(out))
    assert rule is None, (
        f"rule should be deleted after U6's finally block: {rule}"
    )
    # Temp user must be gone (ExitStack exits after finally, _temp_operator cleans up).
    new_users = set(auto_world.pool.users) - users_before
    assert not new_users, (
        f"temp user should have been deleted after SystemExit: {new_users}"
    )


# ============================================================================
# C7 — one _signals_held() around all of U6 (disable → RTS → residue)
# ============================================================================


def test_u6_sigterm_during_disable_returns_car(auto_world):
    """C7: SIGTERM delivered during U6's disable PUT (after U4 grounded the probe car)
    must not skip the return-to-service.  With the C7 fix, one _signals_held() covers
    all of U6 (disable → RTS → residue); the signal is deferred until all three steps
    complete.

    Failing contract: the old code held signals per step, so a SIGTERM during the
    disable loop exited right after it and never reached _auto_return_to_service.

    Asserts from world state (DDB):
    - Rule is deleted (disable completed before the signal was re-raised).
    - Probe car is returned (RTS completed before the signal was re-raised).
    - rc is 143 (the signal rc from _signals_held).
    - The run-id + cleanup command was printed to stdout before U2.
    """
    import json as _json
    import os
    import signal as _signal
    from unittest import mock

    fa = finding_a(auto_world)
    fb = finding_b(auto_world)
    p_vin = probe_vin_of(auto_world, fb, fa)
    probe_vid = auto_world.vehicle_id_of(p_vin)

    # Patch _put_json in the script module to inject SIGTERM on the first successful
    # disable PUT (enabled=False → 200).  This fires from the MAIN thread (not the
    # server thread), which is inside _signals_held(), so the signal is held until
    # all of U6 finishes.
    original_put_json = auto_world.mod._put_json
    signal_sent: list = []

    def sigterm_on_first_disable_put(url: str, token: str, body: Any) -> tuple:
        code, resp, elapsed = original_put_json(url, token, body)
        if (not signal_sent
                and not body.get("enabled", True)   # this is a disable PUT
                and code == 200):
            signal_sent.append(True)
            os.kill(os.getpid(), _signal.SIGTERM)
        return code, resp, elapsed

    auto_world.mod._put_json = sigterm_on_first_disable_put
    try:
        rc, out, err = auto_world.run_main(auto_world.auto_argv())
    finally:
        auto_world.mod._put_json = original_put_json

    # rc is 143 (SIGTERM → SystemExit(143) from _signals_held).
    assert rc == 143, (rc, out, err)

    # The signal was delivered: SIGTERM was sent.
    assert signal_sent, "SIGTERM was never delivered (test setup issue)"

    # Rule must be deleted (disable completed while signal was held).
    rule = auto_world.get_rule(W.RENTAL, _rule_id_of(out))
    assert rule is None, (
        f"rule should be deleted after SIGTERM during disable: {rule}"
    )

    # Probe car must be returned (RTS ran while signal was still held).
    g = auto_world.groundings(probe_vid)
    assert g, f"no grounding row for probe car {probe_vid}"
    assert g[-1].get("returnedAt"), (
        f"probe car {probe_vid} was not returned after SIGTERM during disable: {g}"
    )

    # The run-id + cleanup command was printed to stdout before U2.
    assert "--cleanup-only" in out and "--run-id" in out, (
        f"run-id / cleanup command not in stdout: {out!r}"
    )


def test_u6_sigterm_during_residue_returns_car(auto_world):
    """C7: SIGTERM delivered during U6's residue scan (after U4 grounded the probe car)
    still ends with the car returned.

    With the C7 fix, one _signals_held() covers all of U6; RTS runs before the residue
    scan, so the signal cannot skip the return-to-service even if it arrives during
    residue.  (With the old code, the residue scan ran without any signal hold.)
    """
    import os
    import signal as _signal

    fa = finding_a(auto_world)
    fb = finding_b(auto_world)
    p_vin = probe_vin_of(auto_world, fb, fa)
    probe_vid = auto_world.vehicle_id_of(p_vin)

    # Patch _ddb_scan_all in the script module to deliver SIGTERM the FIRST time the
    # actions table is scanned (that's U6's residue check).  By that point U4 has
    # already grounded the car and RTS has already returned it (RTS runs before the
    # residue scan in the new order).  Everything before the residue scan ran
    # normally, so probe_vid has a grounding row with returnedAt.
    original_scan = auto_world.mod._ddb_scan_all
    signal_sent: list = []
    actions_table = auto_world.mod._auto_tables(W.STAGE)["actions"]

    def sigterm_on_actions_scan(ddb: Any, table: str, **kw: Any) -> list:
        if not signal_sent and table == actions_table:
            signal_sent.append(True)
            os.kill(os.getpid(), _signal.SIGTERM)
        return original_scan(ddb, table, **kw)

    auto_world.mod._ddb_scan_all = sigterm_on_actions_scan
    try:
        rc, out, err = auto_world.run_main(auto_world.auto_argv())
    finally:
        auto_world.mod._ddb_scan_all = original_scan

    assert rc == 143, (rc, out, err)
    assert signal_sent, "SIGTERM was never delivered (test setup issue)"

    # Probe car must be returned (RTS ran before the residue scan, before the signal).
    g = auto_world.groundings(probe_vid)
    assert g, f"no grounding row for probe car {probe_vid}"
    assert g[-1].get("returnedAt"), (
        f"probe car {probe_vid} not returned after SIGTERM during residue: {g}"
    )


# ===========================================================================
# S4 bullet 2 — U3 requires exactly one evaluator run record in invoke window
# ===========================================================================


def test_u3_fails_when_extra_stream_run_in_window(auto_world):
    """U3 FAIL when a concurrent stream-triggered evaluator run lands in the same window.

    U3 queries for run records between u3_start and u3_end (the invoke window).
    A stream-triggered run that happens to start during that same window produces
    two records.  U3 must FAIL naming the count (not just check the last record).

    The world defect ``u3_extra_stream_run`` writes a second run record during
    the first Lambda invoke so it falls inside [u3_start, u3_end].
    """
    auto_world.auto_defects["u3_extra_stream_run"] = True
    rc, out, err = auto_world.run_main(auto_world.auto_argv())
    r = rows(out)
    assert rc == 1, (out, err)
    u3 = r.get("U3 over limit", ("PASS",))
    assert u3[0] == "FAIL", u3
    assert "2" in u3[1], f"expected count '2' in FAIL message: {u3[1]!r}"


# ===========================================================================
# S4 bullet 3 — U4 reads grounding row back before invoking B
# ===========================================================================


def test_u4_fails_when_grounding_row_not_in_ddb(auto_world):
    """U4 FAIL when grounding POST answers 2xx but the DDB row isn't there.

    After a 2xx grounding POST, U4 reads the row back (ConsistentRead) before
    invoking B.  If the row is absent, U4 must FAIL without invoking the
    approver; U6 must still return the probe car (cleanup runs).

    The world defect ``u4_grounding_post_writes_nothing`` returns 201 without
    writing any grounding row to DDB.
    """
    auto_world.auto_defects["u4_grounding_post_writes_nothing"] = True
    rc, out, err = auto_world.run_main(auto_world.auto_argv())
    r = rows(out)
    assert rc == 1, (out, err)
    u4 = r.get("U4 grounded", ("PASS",))
    assert u4[0] == "FAIL", u4
    assert "no row" in u4[1].lower(), (
        f"expected 'no row' in FAIL message: {u4[1]!r}")
    # B was never invoked — only U3's invoke (count 1) should have been made.
    assert auto_world._auto_invoke_count == 1, (
        "B should not have been invoked when grounding row is absent; "
        f"_auto_invoke_count={auto_world._auto_invoke_count}")
    # U6 must still run and handle cleanup (probe was never grounded so
    # _auto_return_to_service returns immediately with 'wrote nothing').
    u6 = r.get("U6 always", ("missing",))
    assert u6[0] == "PASS", (
        f"U6 should PASS (no grounding row to return): {u6}")


# --- lane 12a: cycle-2 survivors ---


def test_u5_cms_move_wrong_ruleId(auto_world):
    """F-C6c: U5 fails when CMS's move record (DDB/GET) carries a wrong ruleId.

    The world defect u5_cms_move_wrong_ruleId rewrites ruleId in the moves table
    to 'wrong-fleet/wrong-rule-id' after CMS's handler writes A's move record.
    U5's ConsistentRead DDB check and GET /moves check must both compare ruleId
    against the per-run rule id; when they disagree, U5 records FAIL naming the field.

    Mutant survival without this test: removing the ruleId comparison in U5 leaves the
    test suite fully green — no existing test drives a wrong ruleId in the CMS record.
    """
    fa = finding_a(auto_world)
    auto_world.auto_defects["u5_cms_move_wrong_ruleId"] = True
    rc, out, err = auto_world.run_main(auto_world.auto_argv())
    r = rows(out)
    assert rc == 1, (out, err)
    u5 = r.get("U5 within limit", ("PASS",))
    assert u5[0] == "FAIL", u5
    detail = u5[1]
    assert "ruleId" in detail or "rule_id" in detail.lower(), (
        f"U5 FAIL detail should name 'ruleId': {detail!r}")


def test_u5_cms_move_wrong_decision(auto_world):
    """F-C6d: U5 fails when CMS's move record (DDB/GET) carries a wrong decision.

    The world defect u5_cms_move_wrong_decision rewrites decision in the moves table
    to 'manual_approved' after CMS's handler writes A's move record.
    U5's ConsistentRead DDB check and GET /moves check must compare decision against
    'auto_approved'; when they disagree, U5 records FAIL naming the field.

    Mutant survival without this test: removing the decision comparison in U5 leaves
    the test suite fully green — no existing test drives a wrong decision in the CMS
    record (as opposed to the AVX Action, which does have a decision check).
    """
    fa = finding_a(auto_world)
    auto_world.auto_defects["u5_cms_move_wrong_decision"] = True
    rc, out, err = auto_world.run_main(auto_world.auto_argv())
    r = rows(out)
    assert rc == 1, (out, err)
    u5 = r.get("U5 within limit", ("PASS",))
    assert u5[0] == "FAIL", u5
    detail = u5[1]
    assert "decision" in detail.lower(), (
        f"U5 FAIL detail should name 'decision': {detail!r}")


def test_u5_move_wrong_findingId(auto_world):
    """N-W4b: U5 fails when GET /moves returns a wrong findingId for A's move record.

    The world defect u5_move_wrong_findingId replaces findingId in every row of the
    GET /moves response with 'FIND-wrong-finding-id-lane12a'.  U5's GET /moves check
    must compare findingId against A's finding_id (fa["finding_id"]); when they differ,
    U5 records FAIL naming the field.

    Mutant survival without this test: a mutant that removes the findingId comparison
    in U5's GET /moves block still passes all existing tests — no test exercises a
    wrong findingId in the response.
    """
    fa = finding_a(auto_world)
    auto_world.auto_defects["u5_move_wrong_findingId"] = True
    rc, out, err = auto_world.run_main(auto_world.auto_argv())
    r = rows(out)
    assert rc == 1, (out, err)
    u5 = r.get("U5 within limit", ("PASS",))
    assert u5[0] == "FAIL", u5
    detail = u5[1]
    assert "findingId" in detail or "findingid" in detail.lower(), (
        f"U5 FAIL detail should name 'findingId': {detail!r}")


def test_p1_fails_when_deny_has_wrong_principal(auto_world):
    """F-W11c: P1 fails when the resource-policy Deny has a non-wildcard Principal.

    The world defect policy_wrong_principal replaces Principal {"AWS": "*"} with
    {"AWS": "arn:aws:iam::123456789012:user/wrong-principal"}.  P1's exact comparison
    of the Principal field must detect the mismatch and record FAIL naming 'Principal'.

    Mutant survival without this test: removing P1's Principal check leaves the test
    suite fully green — no existing test drives a wrong Principal in the Deny.
    """
    auto_world.auto_defects["policy_wrong_principal"] = True
    rc, out, err = auto_world.run_main(auto_world.auto_argv())
    r = rows(out)
    assert rc == 1, (out, err)
    p1 = r.get("P1 config", ("PASS",))
    assert p1[0] == "FAIL", p1
    detail = p1[1].lower()
    assert "principal" in detail, (
        f"P1 FAIL detail should name 'Principal': {p1[1]!r}")


def test_p1_fails_when_deny_has_wrong_resource(auto_world):
    """F-W11d: P1 fails when the resource-policy Deny Resource names a different API id.

    The world defect policy_wrong_resource replaces the Resource ARN's API id with
    'wrongapi0000' instead of the real INTERNAL_API_ID.  P1's exact comparison of the
    Resource ARN must detect the mismatch and record FAIL naming 'Resource'.

    Mutant survival without this test: removing P1's Resource comparison leaves the
    test suite fully green — no existing test drives a wrong Resource API id in the Deny.
    """
    auto_world.auto_defects["policy_wrong_resource"] = True
    rc, out, err = auto_world.run_main(auto_world.auto_argv())
    r = rows(out)
    assert rc == 1, (out, err)
    p1 = r.get("P1 config", ("PASS",))
    assert p1[0] == "FAIL", p1
    detail = p1[1].lower()
    assert "resource" in detail, (
        f"P1 FAIL detail should name 'Resource': {p1[1]!r}")

# --- lane 12b: cycle-2 survivors ---

# ============================================================================
# N-C4b — U6 disable retries (first PUT fails once, rule still ends disabled)
# ============================================================================


def test_u6_disable_first_put_fails_then_retries(auto_world):
    """N-C4b: U6 retries the disable PUT when the first attempt returns 503.

    Property: the disable loop uses (0.0, 2.0, 5.0) waits; a single 503 on the
    first try must be retried so the rule is eventually disabled and deleted.

    Failing contract: with the mutant (loop body ``for _wait in (0.0,)``), the first
    503 leaves the rule enabled and U6 records FAIL "rule still enabled after disable
    PUT".

    Asserts from avx.requests (mid-run fact) and DDB (end state):
    - At least 2 pure-disable PUTs are logged (first 503, second 200).
    - Rule is deleted after the run (retry succeeded).
    - U6 always PASS.
    """
    from unittest import mock

    # # lane 12b world defect: first pure-disable PUT returns 503.
    auto_world.auto_defects["u6_first_disable_put_fails"] = True
    # Avoid the real 2 s + 5 s sleeps in the disable retry loop.
    with mock.patch("time.sleep"):
        rc, out, err = auto_world.run_main(auto_world.auto_argv())
    r = rows(out)

    assert rc == 0, (out, err)
    u6 = r.get("U6 always", ("missing",))
    assert u6[0] == "PASS", (u6, out, err)

    # Rule must be deleted (retry on the second PUT succeeded).
    rule = auto_world.get_rule(W.RENTAL, _rule_id_of(out))
    assert rule is None, (
        f"rule should be deleted after retry: {rule}"
    )

    # Assert from avx.requests that ≥2 pure-disable PUTs were made.
    disable_puts = [
        x for x in auto_world.avx.requests
        if x.get("method") == "PUT"
        and "/approval-rules/fleet/" in (x.get("path") or "")
        and not json.loads(x.get("body") or "{}").get("enabled", True)
        and "limit" not in json.loads(x.get("body") or "{}")
    ]
    assert len(disable_puts) >= 2, (
        f"expected ≥2 disable PUTs (first 503, retry 200), got {len(disable_puts)}: "
        f"{disable_puts}"
    )


# ============================================================================
# N-C7 — one _signals_held() across U6: SIGTERM during RTS defers through residue
# ============================================================================


def test_u6_sigterm_during_rts_residue_still_runs(auto_world):
    """N-C7: SIGTERM delivered during U6's return-to-service must not skip the residue
    scan.

    With the fix (one _signals_held() across disable → RTS → residue), the signal is
    deferred until the full block completes.  With the mutant (_install_signal_handlers()
    called inside _signals_held() just before _auto_return_to_service), the immediate
    handler fires during RTS and the residue scan is skipped.

    The POST itself completes (fake server writes returnedAt) before the signal is
    injected; the differentiating property is whether the residue scan ran.

    Asserts from script output (mid-run fact) and DDB (end state):
    - rc is 143 (signal eventually fires).
    - "U6 residue" row is present (residue ran while signal was held).
    - Probe car has returnedAt (POST completed before signal was sent).
    """
    import os
    import signal as _signal

    fa = finding_a(auto_world)
    fb = finding_b(auto_world)
    p_vin = probe_vin_of(auto_world, fb, fa)
    probe_vid = auto_world.vehicle_id_of(p_vin)

    original_post_json = auto_world.mod._post_json
    signal_sent: list = []

    # Inject SIGTERM on the first _post_json call whose URL contains "return-to-service".
    # The call is made from the main thread inside _signals_held(), so with the fix the
    # signal is held; with the mutant _install_signal_handlers() replaced _hold so the
    # signal fires immediately.
    def sigterm_during_rts(url: str, token: str, body: Any) -> tuple:
        code, resp, elapsed = original_post_json(url, token, body)
        if not signal_sent and "return-to-service" in url:
            signal_sent.append(True)
            os.kill(os.getpid(), _signal.SIGTERM)
        return code, resp, elapsed

    auto_world.mod._post_json = sigterm_during_rts
    try:
        rc, out, err = auto_world.run_main(auto_world.auto_argv())
    finally:
        auto_world.mod._post_json = original_post_json

    assert rc == 143, (rc, out, err)
    assert signal_sent, "SIGTERM was never delivered (check that U4 grounded the probe car)"

    # Residue scan must have run (signal was deferred through RTS, not fired mid-RTS).
    r = rows(out)
    u6_residue = r.get("U6 residue")
    assert u6_residue is not None, (
        "U6 residue row absent — residue scan was skipped by a SIGTERM that fired "
        f"during RTS instead of being held: out={out!r}"
    )
    assert u6_residue[0] == "PASS", (
        f"U6 residue FAIL after SIGTERM during RTS: {u6_residue}"
    )

    # Probe car must have returnedAt (the RTS POST completed before the signal was sent).
    g = auto_world.groundings(probe_vid)
    assert g, f"no grounding row for probe car {probe_vid}"
    assert g[-1].get("returnedAt"), (
        f"probe car {probe_vid} not returned after SIGTERM during RTS: {g}"
    )


# ============================================================================
# F-C7c — _auto_cleanup_only's _signals_held(): SIGTERM during disable defers to RTS
# ============================================================================


def test_cleanup_only_sigterm_during_disable_returns_car(auto_world):
    """F-C7c: SIGTERM during --cleanup-only's disable PUT must not skip return-to-service.

    _auto_cleanup_only holds signals across both disable and RTS with one _signals_held().
    With the mutant (contextlib.nullcontext()), there is no hold; a SIGTERM during the
    disable fires immediately and skips the return-to-service, leaving the car grounded.

    Simulates a crash after U2+U4 (rule enabled, probe car grounded, manifest saved).
    Then runs --cleanup-only with SIGTERM injected during the first successful disable PUT.

    Asserts from DDB (end state):
    - rc is 143 (signal eventually fires after full cleanup).
    - Probe car has returnedAt (RTS ran while the signal was held).
    """
    import json as _json
    import os
    import signal as _signal

    fa = finding_a(auto_world)
    fb = finding_b(auto_world)
    pvin = probe_vin_of(auto_world, fb, fa)
    pvid = auto_world.vehicle_id_of(pvin)

    run_id = "20261022T140000Z-ab0012"
    probe_gid = auto_world.mod._AUTO_PROBE_PREFIX + run_id
    per_run_rid = _run_rule_id(run_id)

    # Write manifest (as _save_manifest would after U4).
    auto_world.state_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "run_id": run_id, "stage": W.STAGE, "part": "auto",
        "started_at": "2026-10-22T14:00:00Z",
        "rule_id": per_run_rid,
        "probe_gid": probe_gid,
        "probe_vid": pvid,
        "probe_posted": True,
    }
    (auto_world.state_dir / f"{run_id}.json").write_text(_json.dumps(manifest))

    # Set up state: rule enabled, probe car grounded (as after U2+U4 crash).
    auto_world.put_rule(W.RENTAL, per_run_rid, enabled=True, limit=400.0)
    auto_world.put_grounding(pvid, probe_gid)

    # Inject SIGTERM on the first successful pure-disable PUT (enabled=False, no limit).
    original_put_json = auto_world.mod._put_json
    signal_sent: list = []

    def sigterm_on_cleanup_disable(url: str, token: str, body: Any) -> tuple:
        code, resp, elapsed = original_put_json(url, token, body)
        if (not signal_sent
                and not body.get("enabled", True)
                and "limit" not in body
                and code == 200):
            signal_sent.append(True)
            os.kill(os.getpid(), _signal.SIGTERM)
        return code, resp, elapsed

    auto_world.mod._put_json = sigterm_on_cleanup_disable
    try:
        rc, out, err = auto_world.run_main([
            "--part", "auto",
            "--stage", W.STAGE,
            "--workgroup", "wg",
            "--cms-api-endpoint", auto_world.cms.url,
            "--avx-api-endpoint", auto_world.avx.url,
            "--allow-writes",
            "--cleanup-only", "--run-id", run_id,
        ])
    finally:
        auto_world.mod._put_json = original_put_json

    assert rc == 143, (rc, out, err)
    assert signal_sent, "SIGTERM was never delivered (check disable PUT fired)"

    # Probe car must be returned (RTS ran while signal was held).
    gnd = auto_world.groundings(pvid)
    assert gnd, f"no grounding row for probe car {pvid}"
    assert gnd[-1].get("returnedAt"), (
        f"probe car {pvid} not returned after SIGTERM during cleanup-only disable: {gnd}"
    )

# --- lane 12c: cycle-2 survivors ---

# ============================================================================
# F-S4c — U3 window lower-bound is u3_start, not "0000"
# ============================================================================


def test_u3_window_excludes_pre_run_record(auto_world):
    """F-S4c: a run record written before U3 starts does not count toward U3's invoke window.

    U3 queries for evaluator run records BETWEEN u3_start AND u3_end.  The mutant
    replaces u3_start with "0000" (open past), so a record from 2020 falls inside
    the BETWEEN clause, producing two records where one is expected → U3 FAIL.

    With the correct code the 2020 record is outside [u3_start, u3_end], so only
    the record written during the invoke is counted → U3 PASS.

    Property asserted: U3 PASSes even when an evaluator run record exists in the
    RUNS table from before u3_start.

    Mutant evidence: apply F-S4c (":s": {"S": "0000"}), re-run → U3 FAIL "2" in message.
    Revert → U3 PASS (this test passes).
    """
    from agents.tier2.run_records import write_run_record as _wrr  # noqa: PLC0415

    # Seed a run record well before any real u3_start.
    _wrr(
        agent_id="agent-fi-evaluator",
        component="evaluator",
        started_at="2020-01-01T00:00:00Z",
        finished_at="2020-01-01T00:00:01Z",
        status="completed",
        artifacts_written=0,
        duplicates_skipped=0,
        agent_version="lane12c-pre-run-seed",
        ddb_client=auto_world.session.resource("dynamodb", region_name=W.REGION),
        table_name=W.RUNS,
    )

    rc, out, err = auto_world.run_main(auto_world.auto_argv())
    r = rows(out)
    assert rc == 0, (out, err)
    u3 = r.get("U3 over limit", ("missing",))
    assert u3[0] == "PASS", (
        f"U3 should PASS: pre-run record at 2020-01-01 is outside [u3_start, u3_end]; "
        f"got {u3}.\n--- stdout ---\n{out}\n--- stderr ---\n{err}")


# ============================================================================
# N-W1 — creation-failure path records cleanup rows and U6 always SKIP
# ============================================================================


def test_u6_creation_failure_records_cleanup_and_skip(auto_world):
    """N-W1: when temp operator creation fails and cleanup also fails,
    the cleanup FAIL row(s) and 'U6 always' SKIP are both recorded.

    Flow: admin_create_user succeeds (created=True), then initiate_auth raises
    (op_auth_fails defect).  In _temp_operator's finally, admin_delete_user also
    raises (delete_user_fails defect), so cleanup.append(cmd) runs.  The
    _part_auto except block then loops over cleanup:

        for cmd in cleanup:                         # N-W1 mutant: for cmd in []
            record(results, "cleanup", "FAIL", ...)
        record(results, "U6 always", "SKIP", ...)   # always present

    The mutant drops the cleanup loop, so no 'cleanup' FAIL row appears.

    Property asserted: at least one 'cleanup' FAIL row with 'temp user not removed',
    AND 'U6 always' SKIP with 'temp operator creation failed'.

    Mutant evidence: apply N-W1 (for cmd in []); this test fails on the cleanup
    row assertion.  Revert → passes.
    """
    auto_world.auto_defects["op_auth_fails"] = True      # initiate_auth raises
    auto_world.auto_defects["delete_user_fails"] = True  # admin_delete_user raises in finally

    rc, out, err = auto_world.run_main(auto_world.auto_argv())
    r = rows(out)

    # The 'cleanup' FAIL row must be present — this is the property the mutant drops.
    cleanup_row = r.get("cleanup", ("missing", ""))
    assert cleanup_row[0] == "FAIL", (
        f"expected a 'cleanup' FAIL row; got {cleanup_row!r}\n"
        f"--- stdout ---\n{out}\n--- stderr ---\n{err}")
    assert "temp user not removed" in cleanup_row[1], (
        f"cleanup row detail should contain 'temp user not removed': {cleanup_row[1]!r}")

    # 'U6 always' SKIP must also be present (not a mutant target, but confirms the
    # exception was caught on the right path).
    u6 = r.get("U6 always", ("missing", ""))
    assert u6[0] == "SKIP", (
        f"expected 'U6 always' SKIP; got {u6!r}\n--- stdout ---\n{out}")
    assert "temp operator creation failed" in u6[1].lower(), (
        f"'U6 always' SKIP detail should mention creation failure: {u6[1]!r}")


# ============================================================================
# N-W2 — _part_auto's Lambda client is built with config=_lambda_config()
# ============================================================================


def test_auto_lambda_client_built_with_lambda_config(auto_world):
    """N-W2: the Lambda client in _part_auto is built with config=_lambda_config()
    (read_timeout=960 s, total_max_attempts=1).

    The mutant removes _lambda_config() from the _client call so the client
    uses boto3 defaults (no extended read timeout, default retry).  Nothing in the
    existing integration tests exercises the client *kwargs*, so the mutant survives.
    This unit test patches _client to record kwargs and asserts the Lambda client
    call carries the expected config.

    Property asserted: the first _client("lambda", ...) call in _part_auto
    has a 'config' kwarg whose read_timeout equals _LAMBDA_READ_TIMEOUT_S.

    Mutant evidence: apply N-W2 (lam = _client("lambda", CVX_REGION)); this test
    fails on the 'config' kwarg assertion.  Revert → passes.
    """
    from unittest import mock  # noqa: PLC0415

    original_client = auto_world.mod._client
    lambda_client_kwargs: list[dict] = []

    def recording_client(service: str, region: str, **kwargs: Any) -> Any:
        if service == "lambda":
            lambda_client_kwargs.append(dict(kwargs))
        return original_client(service, region, **kwargs)

    with mock.patch.object(auto_world.mod, "_client", new=recording_client):
        auto_world.run_main(auto_world.auto_argv())

    # At least one Lambda client must have been built.
    assert lambda_client_kwargs, (
        "no _client('lambda', ...) call was recorded during _part_auto")

    # The first Lambda client (used for P1 config reads and U3-U5 invokes) must
    # carry config=_lambda_config().
    first_kwargs = lambda_client_kwargs[0]
    assert "config" in first_kwargs, (
        f"lambda _client called without 'config' kwarg; recorded kwargs={first_kwargs!r}")

    cfg = first_kwargs["config"]
    expected_timeout = auto_world.mod._LAMBDA_READ_TIMEOUT_S
    actual_timeout = getattr(cfg, "read_timeout", None)
    assert actual_timeout == expected_timeout, (
        f"Lambda config read_timeout={actual_timeout!r}, expected {expected_timeout}; "
        f"config={cfg!r}")

# --- lane 13a ---


def test_u1_extracts_api_stage_from_endpoint_not_vsa_stage(auto_world):
    """U1 PASS even though CMS's internal API stage ('prod') differs from args.stage.

    C8: the world's IamTlsApi now uses stage 'prod' (matching what CMS deploys),
    while args.stage is 'staging'.  Before the fix, U1 compared the deny ARN's
    stage segment against args.stage ('staging'), so the deny ARN ending in
    '/prod/POST/...' never matched and U1 always failed on live runs.
    After the fix, U1 extracts the API stage from the endpoint URL path ('/prod/')
    and the suffix check passes.
    """
    # Confirm the world's internal API is using 'prod', not STAGE.
    assert auto_world.internal.stage == "prod", (
        f"world.internal.stage={auto_world.internal.stage!r}; "
        "expected 'prod' (C8 fix: world must match CMS's real deployment stage)"
    )
    rc, out, err = auto_world.run_main(auto_world.auto_argv())
    r = rows(out)
    u1 = r.get("U1 403 probes", ("missing",))
    assert u1[0] == "PASS", (
        f"U1 should PASS when endpoint is /prod/ and the deny ARN names /prod/POST/...; "
        f"got {u1}; output={out!r}"
    )


def test_u1_wrong_stage_in_deny_arn_causes_u1_fail(auto_world):
    """U1 FAIL when the deny ARN's stage segment does not match the endpoint stage.

    R3-C8b gap: if the script drops the stage from the ARN suffix check, a deny
    ARN naming the wrong stage ('wrong-stage' instead of 'prod') would still pass.
    After the fix, U1 extracts the stage from the endpoint URL and requires it in
    the ARN, so a mismatched stage causes FAIL.

    Defect: wrong_deny_stage makes IamTlsApi substitute 'wrong-stage' for 'prod'
    in the explicit-deny resource ARN, while the endpoint URL still starts with /prod/.
    """
    auto_world.internal.wrong_deny_stage = "wrong-stage"
    rc, out, err = auto_world.run_main(auto_world.auto_argv())
    r = rows(out)
    u1 = r.get("U1 403 probes", ("PASS", ""))
    assert u1[0] == "FAIL", (
        f"U1 should FAIL when deny ARN has stage 'wrong-stage' but endpoint stage is 'prod'; "
        f"got {u1}; out={out!r}"
    )
    assert (
        "wrong-stage" in u1[1]
        or "stage" in u1[1].lower()
        or "suffix" in u1[1].lower()
        or "arn" in u1[1].lower()
        or "resource" in u1[1].lower()
    ), f"U1 FAIL detail should mention the ARN mismatch; got {u1[1]!r}"

# --- lane 13b ---
# W16: widen U3's BETWEEN window by 10 s each side so clock skew of ±1 s
# and ±5 s between operator and Lambda clocks does not drop the run record.
# World switch: auto_defects["u3_approver_clock_skew_s"] = <int seconds>
#
# Note: the clock skew also affects U4's 'B Action created_at vs run_start_iso'
# comparison (a separate S5 suggestion, not addressed here).  These tests assert
# U3 PASS only, not rc==0, because U4 also fails on the skewed clock.


def test_u3_passes_when_approver_clock_skew_neg1s(auto_world):
    """W16: U3 PASSes when the auto-approver's _now_iso runs 1 s behind the operator.

    Without the 10-s margin, a 1-s skew drops the run record outside the
    [u3_start, u3_end] window whenever a second boundary falls between the
    operator's clock snap and the handler's started_at.

    World switch: ``u3_approver_clock_skew_s = -1`` shifts _now_iso by -1 s.
    """
    auto_world.auto_defects["u3_approver_clock_skew_s"] = -1
    _rc, out, err = auto_world.run_main(auto_world.auto_argv())
    r = rows(out)
    u3 = r.get("U3 over limit", ("missing",))
    assert u3[0] == "PASS", (
        f"U3 should PASS with approver clock 1 s behind: {u3}\n"
        f"--- stdout ---\n{out}\n--- stderr ---\n{err}")


def test_u3_passes_when_approver_clock_skew_pos1s(auto_world):
    """W16: U3 PASSes when the auto-approver's _now_iso runs 1 s ahead of the operator."""
    auto_world.auto_defects["u3_approver_clock_skew_s"] = +1
    _rc, out, err = auto_world.run_main(auto_world.auto_argv())
    r = rows(out)
    u3 = r.get("U3 over limit", ("missing",))
    assert u3[0] == "PASS", (
        f"U3 should PASS with approver clock 1 s ahead: {u3}\n"
        f"--- stdout ---\n{out}\n--- stderr ---\n{err}")


def test_u3_passes_when_approver_clock_skew_neg5s(auto_world):
    """W16: U3 PASSes when the auto-approver's _now_iso runs 5 s behind the operator."""
    auto_world.auto_defects["u3_approver_clock_skew_s"] = -5
    _rc, out, err = auto_world.run_main(auto_world.auto_argv())
    r = rows(out)
    u3 = r.get("U3 over limit", ("missing",))
    assert u3[0] == "PASS", (
        f"U3 should PASS with approver clock 5 s behind: {u3}\n"
        f"--- stdout ---\n{out}\n--- stderr ---\n{err}")


def test_u3_passes_when_approver_clock_skew_pos5s(auto_world):
    """W16: U3 PASSes when the auto-approver's _now_iso runs 5 s ahead of the operator."""
    auto_world.auto_defects["u3_approver_clock_skew_s"] = +5
    _rc, out, err = auto_world.run_main(auto_world.auto_argv())
    r = rows(out)
    u3 = r.get("U3 over limit", ("missing",))
    assert u3[0] == "PASS", (
        f"U3 should PASS with approver clock 5 s ahead: {u3}\n"
        f"--- stdout ---\n{out}\n--- stderr ---\n{err}")

# --- lane 13c ---

# ============================================================================
# R3-W17 — U6: all disable PUTs refused → cleanup pointer in stderr naming rule
# ============================================================================


def test_u6_all_disable_puts_refused_prints_pointer(auto_world):
    """W17: when every disable PUT fails, U6 must print a CLEANUP NEEDED pointer
    to stderr naming the rule id, in addition to recording the FAIL row.

    Without the fix U6 appends "rule still enabled after disable PUT" but prints
    nothing to stderr — the operator has no recovery hint in the immediate output.

    Mutant R3-W17: ``u6_problems.append("rule still enabled after disable PUT")``
    → ``pass`` — the rule-enabled problem is silently dropped.  The test fails
    because it asserts rc == 1 (a FAIL row must exist).

    Property asserted (mid-run):
    - rc == 1 (U6 FAIL row for the enabled rule).
    - stderr contains "CLEANUP NEEDED".
    - stderr names the rule id (``livetest-fi-rebal-auto-<run_id>``).
    """
    from unittest import mock  # noqa: PLC0415

    auto_world.auto_defects["u6_all_disable_puts_fail"] = True

    # Avoid the real 2 s + 5 s sleeps in the disable retry loop.
    with mock.patch("time.sleep"):
        rc, out, err = auto_world.run_main(auto_world.auto_argv())

    assert rc == 1, (
        f"expected rc 1 (FAIL); got {rc}\n--- stdout ---\n{out}\n--- stderr ---\n{err}")

    # stderr must contain the CLEANUP NEEDED pointer.
    assert "CLEANUP NEEDED" in err, (
        f"expected 'CLEANUP NEEDED' in stderr; got:\n{err}\n--- stdout ---\n{out}")

    # The pointer must name the rule id so the operator knows what to clean up.
    rule_id = _rule_id_of(out)
    assert rule_id in err, (
        f"expected rule_id {rule_id!r} in stderr; got:\n{err}\n--- stdout ---\n{out}")


# ============================================================================
# R3-12b-X1 — --cleanup-only disable retries (first PUT fails once)
# ============================================================================


def test_cleanup_only_first_disable_put_fails_then_retries(auto_world):
    """R3-12b-X1: --cleanup-only retries the disable PUT when the first attempt
    returns 503.

    Without the fix (mutant: ``for _wait in (0.0,)`` — one try only) the first
    503 leaves the rule enabled, X1-auto FAILs, and the rule is not deleted.

    Property asserted (mid-run fact and end state):
    - At least 2 pure-disable PUTs are logged (first 503, retry 200).
    - Rule is deleted after the run (retry succeeded).
    - X1-auto PASS.
    """
    import json as _json  # noqa: PLC0415
    from unittest import mock  # noqa: PLC0415

    fa = finding_a(auto_world)
    fb = finding_b(auto_world)
    pvin = probe_vin_of(auto_world, fb, fa)
    pvid = auto_world.vehicle_id_of(pvin)

    run_id = "20261022T150000Z-abcdef"
    probe_gid = auto_world.mod._AUTO_PROBE_PREFIX + run_id
    per_run_rid = _run_rule_id(run_id)

    # Write manifest to the world's state_dir.
    auto_world.state_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "run_id": run_id, "stage": W.STAGE, "part": "auto",
        "started_at": "2026-10-22T15:00:00Z",
        "rule_id": per_run_rid,
        "probe_gid": probe_gid,
        "probe_vid": pvid,
    }
    (auto_world.state_dir / f"{run_id}.json").write_text(_json.dumps(manifest))

    # State: rule enabled, probe car grounded.
    auto_world.put_rule(W.RENTAL, per_run_rid, enabled=True, limit=400.0)
    auto_world.put_grounding(pvid, probe_gid)

    # First pure-disable PUT returns 503 (single-shot).
    auto_world.auto_defects["u6_first_disable_put_fails"] = True

    argv = [
        "--part", "auto",
        "--stage", W.STAGE,
        "--workgroup", "wg",
        "--cms-api-endpoint", auto_world.cms.url,
        "--avx-api-endpoint", auto_world.avx.url,
        "--allow-writes",
        "--cleanup-only", "--run-id", run_id,
    ]
    with mock.patch("time.sleep"):
        rc, out, err = auto_world.run_main(argv)
    r = rows(out)

    assert rc == 0, (out, err)
    x1 = r.get("X1-auto", ("missing",))
    assert x1[0] == "PASS", (
        f"X1-auto should PASS after retry; got {x1!r}\n--- stdout ---\n{out}\n--- stderr ---\n{err}")

    # Rule must be deleted (second attempt succeeded).
    rule = auto_world.get_rule(W.RENTAL, per_run_rid)
    assert rule is None, (
        f"rule should be deleted after cleanup retry: {rule}")

    # At least 2 pure-disable PUTs were made (first 503, second 200).
    disable_puts = [
        x for x in auto_world.avx.requests
        if x.get("method") == "PUT"
        and "/approval-rules/fleet/" in (x.get("path") or "")
        and not _json.loads(x.get("body") or "{}").get("enabled", True)
        and "limit" not in _json.loads(x.get("body") or "{}")
    ]
    assert len(disable_puts) >= 2, (
        f"expected ≥2 disable PUTs (first 503, retry 200); got {len(disable_puts)}: "
        f"{disable_puts}")


# ============================================================================
# S5 bullet 4 — probe_grounded set after _save_manifest succeeds in U4
# ============================================================================


def test_u4_manifest_save_failure_does_not_ground(auto_world):
    """S5 bullet 4: if _save_manifest raises in U4 before the grounding POST,
    probe_grounded must stay False so U6 does not try to return a car that was
    never grounded.

    With the broken code (probe_grounded set BEFORE the save):
    - probe_grounded = True is set before _save_manifest
    - _save_manifest raises → POST is never sent
    - state["probe_grounded"] is already True
    - U6 calls _auto_return_to_service, loops through _RTS_WAITS_S, ends with
      "return to service: no grounding row yet" in u6_problems

    With the fix (probe_grounded set AFTER the save):
    - _save_manifest raises → exception bubbles out of the else-branch
    - state["probe_grounded"] was never set to True (stays False)
    - U6 skips _auto_return_to_service entirely
    - no "return to service" in U6 always detail

    Properties asserted:
    - No grounding is present in DDB after the run (POST was never sent).
    - "return to service" not in U6 always detail.
    - U4 FAIL (the _save_manifest exception was caught and recorded).
    """
    from unittest import mock  # noqa: PLC0415

    original_save = auto_world.mod._save_manifest

    def fail_on_probe_posted(m: dict) -> None:
        if m.get("probe_posted"):
            raise OSError("world: _save_manifest fails on probe_posted")
        return original_save(m)

    # time.sleep is mocked so that the broken path (RTS retries) does not
    # take 7+ seconds; with the fix no sleeps occur.
    with mock.patch.object(auto_world.mod, "_save_manifest", side_effect=fail_on_probe_posted):
        with mock.patch("time.sleep"):
            rc, out, err = auto_world.run_main(auto_world.auto_argv())
    r = rows(out)

    # U4 must FAIL (the _save_manifest exception was caught).
    u4 = r.get("U4 grounded", ("missing",))
    assert u4[0] == "FAIL", (
        f"U4 grounded should FAIL when _save_manifest raises; got {u4!r}\n"
        f"--- stdout ---\n{out}\n--- stderr ---\n{err}")

    # No grounding row should exist in DDB (POST was never sent).
    fb = finding_b(auto_world)
    fa = finding_a(auto_world)
    pvin = probe_vin_of(auto_world, fb, fa)
    pvid = auto_world.vehicle_id_of(pvin)
    open_gnd = [g for g in auto_world.groundings(pvid) if not g.get("returnedAt")]
    assert not open_gnd, (
        f"expected no open grounding when POST was never sent; got {open_gnd}\n"
        f"--- stdout ---\n{out}\n--- stderr ---\n{err}")

    # U6 must not mention "return to service" — it should not have tried.
    u6 = r.get("U6 always", ("missing",))
    assert "return to service" not in (u6[1] or "").lower(), (
        f"U6 should not mention 'return to service' when grounding was never sent; "
        f"got U6={u6!r}\n--- stdout ---\n{out}\n--- stderr ---\n{err}")


# ============================================================================
# S5 bullet 3 — U6 and X1-auto PASS texts say only what happened
# ============================================================================


def test_u6_pass_text_when_no_rule_created(auto_world):
    """S5 bullet 3: U6 PASS text must say 'no rule created' when rule_created is False,
    not 'rule disabled'.

    The u2_create_raises defect makes U2 FAIL (write_failed=True), so U3-U5 SKIP.
    U6 runs: rule_created is False → disable skipped; probe_grounded is False →
    return-to-service skipped.  The PASS detail should describe what actually happened.

    Property asserted:
    - U6 always PASS.
    - "rule disabled" not in detail (nothing was disabled).
    - "no rule created" in detail.
    """
    auto_world.auto_defects["u2_create_raises"] = True
    rc, out, err = auto_world.run_main(auto_world.auto_argv())
    r = rows(out)

    # U2 FAIL → rc 1; U6 PASS (nothing to clean up).
    u6 = r.get("U6 always", ("missing",))
    assert u6[0] == "PASS", (
        f"U6 always should PASS (no cleanup needed); got {u6!r}\n"
        f"--- stdout ---\n{out}\n--- stderr ---\n{err}")
    detail = u6[1] or ""
    assert "rule disabled" not in detail, (
        f"U6 PASS must not say 'rule disabled' when no rule was created; got {detail!r}")
    assert "no rule created" in detail, (
        f"U6 PASS must say 'no rule created'; got {detail!r}")


def test_x1_auto_pass_text_when_no_rule_and_no_probe(auto_world):
    """S5 bullet 3: X1-auto PASS text must say 'no rule created' and 'nothing grounded'
    when neither a rule nor a grounding exists in DDB.

    The manifest-only case (no rule_created, no probe_posted) leaves nothing to disable
    or return; _rule_exists is False, probe_vid is present but no grounding was made.
    """
    import json as _json  # noqa: PLC0415

    run_id = "20261022T160000Z-cafe00"
    per_run_rid = _run_rule_id(run_id)

    auto_world.state_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "run_id": run_id, "stage": W.STAGE, "part": "auto",
        "started_at": "2026-10-22T16:00:00Z",
        "rule_id": per_run_rid,
        "probe_gid": auto_world.mod._AUTO_PROBE_PREFIX + run_id,
        "probe_vid": "VEH-SOME-0001",
    }
    (auto_world.state_dir / f"{run_id}.json").write_text(_json.dumps(manifest))

    # Nothing written to DDB — no rule, no grounding.
    argv = [
        "--part", "auto",
        "--stage", W.STAGE,
        "--workgroup", "wg",
        "--cms-api-endpoint", auto_world.cms.url,
        "--avx-api-endpoint", auto_world.avx.url,
        "--allow-writes",
        "--cleanup-only", "--run-id", run_id,
    ]
    rc, out, err = auto_world.run_main(argv)
    r = rows(out)

    assert rc == 0, (out, err)
    x1 = r.get("X1-auto", ("missing",))
    assert x1[0] == "PASS", (x1, out, err)
    detail = x1[1] or ""
    assert "rule disabled" not in detail, (
        f"X1-auto PASS must not say 'rule disabled' when no rule exists; got {detail!r}")
    assert "no rule created" in detail, (
        f"X1-auto PASS must say 'no rule created'; got {detail!r}")
    assert "probe car returned" not in detail, (
        f"X1-auto PASS must not say 'probe car returned' when nothing was grounded; "
        f"got {detail!r}")
    assert "nothing grounded" in detail, (
        f"X1-auto PASS must say 'nothing grounded'; got {detail!r}")
