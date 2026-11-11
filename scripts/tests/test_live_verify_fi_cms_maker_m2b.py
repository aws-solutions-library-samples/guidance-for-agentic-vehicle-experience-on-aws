"""Architect tests for the maker script's M2 guards (live-verify-fi-cms-maker.py).

Each pins one property a reviewer would otherwise have to re-derive; the mutant each
catches is listed in the docstring and in
/tmp/2026-10-05-fi-fleet-health-stage-d-g78-mmut/m2b/results.json.

  B1 investigate refuses to POST while the investigations event source is not Enabled.
  B2 investigate refuses to POST while a message is in flight on the queue.
  B3 a second investigate run resumes the recorded investigation and never POSTs anew.
  B4 a second investigation row for the pattern FAILs the one-row check.
  B5 a second decide run reuses the recorded decision and never POSTs it again.
  B6 a thread whose affectedVinsSha256 differs from ADP's row FAILs.
  B7 an ADP row with no sha FAILs (fail closed), not a NOTE.
  B8 a second issue run reuses the recorded issue and never POSTs it again.
  B9 no decide version in the manifest FAILs the version check.
  B10 an issue Finding from another agent is not accepted (the poll times out, FAIL).
"""
from __future__ import annotations

import contextlib
import json
import tempfile
import types
from pathlib import Path
from typing import Any, Iterator

import boto3

from scripts.tests import test_live_verify_fi_cms_maker_m2 as m2
from scripts.tests.fi_cms_world.world import CmsWorld
from scripts.tests.fi_rw_world.world import REGION, STAGE

_maker = m2._maker
_seed = m2._seed


@contextlib.contextmanager
def _one_manifest_dir() -> Iterator[str]:
    """Make successive m2._full_run calls share one manifest dir (one run); yield the path."""
    fixed = tempfile.mkdtemp(prefix="fi-cms-m2b-")
    orig = m2.tempfile
    m2.tempfile = types.SimpleNamespace(mkdtemp=lambda **_kw: fixed)  # type: ignore[assignment]
    try:
        yield fixed
    finally:
        m2.tempfile = orig  # type: ignore[assignment]


def _world_with_pattern(w: CmsWorld) -> None:
    m2._setup_world(w)
    _maker.seed_pattern(w, pattern_id=m2._PATTERN_ID, failure_mode_id=m2._FM_ID,
                        vin_list=[_seed.vin(0), _seed.vin(1)])
    w.refresh_population()


def _count_calls(mod_obj: Any, name: str) -> list[int]:
    """Wrap ``mod_obj.<name>`` so each call is counted; return the counter."""
    calls = [0]
    orig = getattr(mod_obj, name)

    def _wrapped(*a: Any, **kw: Any) -> Any:
        calls[0] += 1
        return orig(*a, **kw)

    setattr(mod_obj, name, _wrapped)
    return calls


def _fails(rows: dict[str, str]) -> list[str]:
    return [k for k, v in rows.items() if v == "FAIL"]


def _inv_rows_for_pattern() -> list[dict]:
    t = boto3.resource("dynamodb", region_name=REGION).Table(_maker.INVESTIGATIONS_TABLE)
    return [it for it in t.scan(ConsistentRead=True).get("Items", [])
            if it.get("pattern_id") == m2._PATTERN_ID
            and not str(it.get("investigation_id", "")).startswith("lock#")]


def _patch_client(mod: Any, service: str, region: str | None, method: str,
                  make: Any) -> None:
    """After the script builds its ``service`` client (in ``region``), replace one
    method on that instance with ``make(orig_method)``. The fault reaches the script
    through the same client it calls, while the guarded allowlist stays installed."""
    orig_client = mod._client

    def _client(svc: str, reg: str, **kw: Any) -> Any:
        c = orig_client(svc, reg, **kw)
        if svc == service and (region is None or reg == region):
            setattr(c, method, make(getattr(c, method)))
        return c

    mod._client = _client


def test_b1_refuses_post_when_event_source_disabled() -> None:
    """Mutant: `if not resume_id and not _check_sqs_and_event_source(...)` → `if False`."""
    with CmsWorld(run_scheduled=False) as w:
        _world_with_pattern(w)
        mod = m2._load_mod(w)
        with _one_manifest_dir() as _:
            rc0, text0, _ = m2._full_run(w, mod, parts=["pre"])
            assert rc0 == 0, text0
            lam = boto3.client("lambda", region_name=REGION)
            for m in lam.list_event_source_mappings(
                    FunctionName=m2.HEALTH_FN)["EventSourceMappings"]:
                lam.update_event_source_mapping(UUID=m["UUID"], Enabled=False)
            posts = _count_calls(w.cms_fi._mod.maker, "handle_start_investigation")
            rc, text, rows = m2._full_run(w, mod, parts=["investigate"])
    assert posts[0] == 0, f"start route called {posts[0]} times\n{text}"
    assert any("refused before POST" in k for k in _fails(rows)), f"{_fails(rows)}\n{text}"
    assert rc == 1


# Restored (architect, battery c1 MA27): the world test for the real queue checks the
# world, not the script's refusal; this one guards the script's in-flight gate.
def test_b2_refuses_post_when_message_in_flight() -> None:
    """Mutant: queue check ignores ApproximateNumberOfMessagesNotVisible.

    moto can't hold a message in flight here (a receive on a queue with an event source
    tries to run the Lambda in docker), so the script's own SQS client reports one.
    """
    with CmsWorld(run_scheduled=False) as w:
        _world_with_pattern(w)
        mod = m2._load_mod(w)
        with _one_manifest_dir():
            rc0, text0, _ = m2._full_run(w, mod, parts=["pre"])
            assert rc0 == 0, text0

            def _make(orig: Any) -> Any:
                def _gqa(**kw: Any) -> dict:
                    out = orig(**kw)
                    if kw.get("QueueUrl", "").endswith(f"vsa-{STAGE}-avx-investigations"):
                        out["Attributes"]["ApproximateNumberOfMessages"] = "0"
                        out["Attributes"]["ApproximateNumberOfMessagesNotVisible"] = "1"
                    return out
                return _gqa

            _patch_client(mod, "sqs", None, "get_queue_attributes", _make)
            posts = _count_calls(w.cms_fi._mod.maker, "handle_start_investigation")
            rc, text, rows = m2._full_run(w, mod, parts=["investigate"])
    assert posts[0] == 0, f"start route called {posts[0]} times\n{text}"
    assert any("refused before POST" in k for k in _fails(rows)), f"{_fails(rows)}\n{text}"
    assert any("in_flight=1" in k for k in _fails(rows)), f"{_fails(rows)}\n{text}"




def test_b3_second_investigate_resumes_without_new_post() -> None:
    """Mutant: resume_id forced to '' (every run POSTs; the second hits our own lock)."""
    with CmsWorld(run_scheduled=False) as w:
        _world_with_pattern(w)
        posts = _count_calls(w.cms_fi._mod.maker, "handle_start_investigation")
        mod = m2._load_mod(w)
        with _one_manifest_dir() as _:
            rc1, text1, rows1 = m2._full_run(w, mod, parts=["pre", "investigate"])
            assert rc1 == 0 and not _fails(rows1), f"{_fails(rows1)}\n{text1}"
            first = posts[0]
            rc2, text2, rows2 = m2._full_run(w, mod, parts=["investigate"])
        rows_after = _inv_rows_for_pattern()
    # First run: POST + repeat-POST (records idempotency_checked). Second: check skipped.
    assert first == 2, f"first run made {first} start calls\n{text1}"
    assert posts[0] == 2, f"second run made {posts[0] - first} extra calls (expected 0)\n{text2}"
    assert not _fails(rows2), f"{_fails(rows2)}\n{text2}"
    assert rc2 == 0, text2
    assert len(rows_after) == 1, rows_after


def test_b4_duplicate_investigation_row_fails() -> None:
    """Mutant: one-row check asserts `inv_id in rows` instead of `rows == [inv_id]`."""
    with CmsWorld(run_scheduled=False) as w:
        _world_with_pattern(w)
        fi_maker = w.cms_fi._mod.maker
        orig = fi_maker.handle_start_investigation
        n = [0]

        def _dup_on_repeat(pattern_id: str, body: Any, event: Any) -> Any:
            n[0] += 1
            if n[0] == 2:  # the repeat POST: a second row appears for the pattern
                boto3.resource("dynamodb", region_name=REGION).Table(
                    _maker.INVESTIGATIONS_TABLE).put_item(Item={
                        "investigation_id": "11111111-2222-3333-4444-555555555555",
                        "pattern_id": pattern_id, "status": "queued"})
            return orig(pattern_id, body, event)

        fi_maker.handle_start_investigation = _dup_on_repeat
        mod = m2._load_mod(w)
        rc, text, rows = m2._full_run(w, mod, parts=["pre", "investigate"])
    assert any("one investigation row" in k for k in _fails(rows)), f"{_fails(rows)}\n{text}"
    assert rc == 1


def test_b5_second_decide_reuses_recorded_decision() -> None:
    """Mutant: the recorded decision_action is ignored (the decision is POSTed again)."""
    with CmsWorld(run_scheduled=False) as w:
        _world_with_pattern(w)
        decisions = _count_calls(w.cms_fi._mod.maker, "handle_decision")
        mod = m2._load_mod(w)
        with _one_manifest_dir() as _:
            rc1, text1, rows1 = m2._full_run(w, mod, parts=["pre", "investigate", "decide"])
            assert rc1 == 0 and not _fails(rows1), f"{_fails(rows1)}\n{text1}"
            rc2, text2, rows2 = m2._full_run(w, mod, parts=["decide"])
    assert decisions[0] == 1, f"decision route called {decisions[0]} times\n{text2}"
    assert not _fails(rows2), f"{_fails(rows2)}\n{text2}"
    assert rc2 == 0


def test_b6_thread_sha_mismatch_fails() -> None:
    """Mutant: the sha check compares a constant / is dropped."""
    with CmsWorld(run_scheduled=False) as w:
        _world_with_pattern(w)
        fi_maker = w.cms_fi._mod.maker
        orig = fi_maker.project_thread_record

        def _bad_sha(item: dict) -> dict:
            out = orig(item)
            out["affectedVinsSha256"] = "0" * 64
            return out

        fi_maker.project_thread_record = _bad_sha
        mod = m2._load_mod(w)
        rc, text, rows = m2._full_run(w, mod, parts=["pre", "investigate", "decide"])
    assert any("affectedVinsSha256 matches" in k for k in _fails(rows)), f"{_fails(rows)}\n{text}"
    assert rc == 1


def test_b7_missing_adp_sha_fails_closed() -> None:
    """Mutant: `bool(expected_sha) and` dropped (empty == empty passes).

    The script's ADP read returns the row without its sha and the thread carries none,
    so only the fail-closed guard can FAIL it. (Removing the sha from the table itself
    makes the ADP writer refuse the decision, which is not this property.)
    """
    with CmsWorld(run_scheduled=False) as w:
        _world_with_pattern(w)
        fi_maker = w.cms_fi._mod.maker
        orig = fi_maker.project_thread_record

        def _empty_sha(item: dict) -> dict:
            out = orig(item)
            out["affectedVinsSha256"] = None
            return out

        fi_maker.project_thread_record = _empty_sha
        mod = m2._load_mod(w)

        def _make(orig_gi: Any) -> Any:
            def _gi(**kw: Any) -> dict:
                out = orig_gi(**kw)
                if kw.get("TableName") == f"adp-{STAGE}-fault-patterns" and out.get("Item"):
                    out["Item"].pop("affected_vins_sha256", None)
                return out
            return _gi

        _patch_client(mod, "dynamodb", "us-east-1", "get_item", _make)
        rc, text, rows = m2._full_run(w, mod, parts=["pre", "investigate", "decide"])
    assert any("affectedVinsSha256 matches" in k for k in _fails(rows)), f"{_fails(rows)}\n{text}"
    assert rc == 1


def test_b8_second_issue_reuses_recorded_issue() -> None:
    """Mutant: the recorded issue_action is ignored (the issue is POSTed again)."""
    with CmsWorld(run_scheduled=False) as w:
        _world_with_pattern(w)
        issues = _count_calls(w.cms_fi._mod.maker, "handle_issue")
        mod = m2._load_mod(w)
        with _one_manifest_dir() as _:
            rc1, text1, rows1 = m2._full_run(
                w, mod, parts=["pre", "investigate", "decide", "issue"])
            assert rc1 == 0 and not _fails(rows1), f"{_fails(rows1)}\n{text1}"
            rc2, text2, rows2 = m2._full_run(w, mod, parts=["issue"])
    assert issues[0] == 1, f"issue route called {issues[0]} times\n{text2}"


def test_b9_missing_decide_version_fails() -> None:
    """Mutant: version check downgraded to a NOTE when there is no decide version."""
    with CmsWorld(run_scheduled=False) as w:
        _world_with_pattern(w)
        mod = m2._load_mod(w)
        orig_um = mod.update_manifest

        def _drop_version(manifest: Any, key: str, value: Any) -> None:
            if key == "thread_version_after_decide":
                return
            orig_um(manifest, key, value)

        mod.update_manifest = _drop_version
        rc, text, rows = m2._full_run(w, mod, parts=["pre", "investigate", "decide", "issue"])
    assert any("thread version" in k for k in _fails(rows)), f"{_fails(rows)}\n{text}"
    assert rc == 1


def test_b10_issue_finding_from_other_agent_not_accepted() -> None:
    """Mutant: the poll accepts any item on the finding_key (agent check dropped)."""
    with CmsWorld(run_scheduled=False) as w:
        _world_with_pattern(w)
        mod = m2._load_mod(w)
        with _one_manifest_dir() as _:
            rc1, text1, rows1 = m2._full_run(w, mod, parts=["pre", "investigate", "decide"])
            assert rc1 == 0 and not _fails(rows1), f"{_fails(rows1)}\n{text1}"
            ft = boto3.resource("dynamodb", region_name=REGION).Table(
                f"vsa-{STAGE}-avx-findings")
            changed = 0
            for it in ft.scan().get("Items", []):
                if it.get("finding_kind") == "quality.issue":
                    ft.update_item(Key={"finding_id": it["finding_id"]},
                                   UpdateExpression="SET agent_id = :a",
                                   ExpressionAttributeValues={":a": "agent-someone-else"})
                    changed += 1
            assert changed == 1, f"fixture: {changed} quality.issue findings"
            issues = _count_calls(w.cms_fi._mod.maker, "handle_issue")
            rc, text, rows = m2._full_run(w, mod, parts=["issue"])
    assert issues[0] == 0, f"issue POSTed {issues[0]} times\n{text}"
    assert any("quality.issue Finding appeared" in k for k in _fails(rows)), f"{_fails(rows)}\n{text}"


# ---------------------------------------------------------------------------
# B11 — guard: investigate refuses when pick changed after investigation recorded
# ---------------------------------------------------------------------------

_PATTERN_ID_B = "test-m2-non-planted-002"  # alphabetically after A → A picks first
_FM_ID_B = "fm-m2-002"


def test_b11_repick_after_investigate_never_posts_other_pattern() -> None:
    """Mutant: `if mdata.get("investigation_id") and ... != gid: return` dropped in investigate.

    Two patterns seeded, A before B alphabetically.  First run: pre picks A, investigate
    POSTs to A (original + idempotency repeat = 2 calls).  Direct manifest injection:
    pattern_pick overwritten to B (simulates what old pre would do when A is locked).
    Second investigate: guard must refuse (FAIL + rc 1) and never call
    handle_start_investigation at all, keeping the total at 2.

    After fix (1), pre refuses to overwrite — the direct injection below tests
    the investigate-level guard specifically.
    """
    with CmsWorld(run_scheduled=False) as w:
        m2._setup_world(w)
        _maker.seed_pattern(w, pattern_id=m2._PATTERN_ID, failure_mode_id=m2._FM_ID,
                            vin_list=[_seed.vin(0), _seed.vin(1)])
        _maker.seed_pattern(w, pattern_id=_PATTERN_ID_B, failure_mode_id=_FM_ID_B,
                            vin_list=[_seed.vin(2), _seed.vin(3)], code_family="P0SW2")
        w.refresh_population()
        # Write cache with both A and B so pre can pick A
        now_iso = m2._now_iso()
        m2._write_pop_caches(w, pattern_list=[
            {
                "patternId": m2._PATTERN_ID, "codeFamily": "P0SW1",
                "topCause": {"cohortDimension": "model", "cohortValue": "Azimuth",
                             "candidateRank": 1, "rateRatio": 5.0, "pValue": 0.0001},
                "candidateCount": 1, "vehiclesAffected": 50, "vehiclesInService": 1000,
                "rate": 0.05, "firstDetectedMonth": "2026-01", "trend": "rising",
                "method": "cohort", "computedAt": now_iso, "parametersVersion": "1.0",
            },
            {
                "patternId": _PATTERN_ID_B, "codeFamily": "P0SW2",
                "topCause": {"cohortDimension": "model", "cohortValue": "Azimuth",
                             "candidateRank": 1, "rateRatio": 4.0, "pValue": 0.0002},
                "candidateCount": 1, "vehiclesAffected": 30, "vehiclesInService": 800,
                "rate": 0.04, "firstDetectedMonth": "2026-02", "trend": "stable",
                "method": "cohort", "computedAt": now_iso, "parametersVersion": "1.0",
            },
        ])
        posts = _count_calls(w.cms_fi._mod.maker, "handle_start_investigation")
        mod = m2._load_mod(w)

        with _one_manifest_dir() as mdir:
            # Run 1: pre picks A, investigate POSTs to A (original + repeat = 2 calls)
            rc1, text1, rows1 = m2._full_run(w, mod, parts=["pre", "investigate"])
            assert rc1 == 0 and not _fails(rows1), f"{_fails(rows1)}\n{text1}"
            first = posts[0]  # 2: original POST + idempotency repeat-check

            # Directly overwrite pattern_pick with B in the manifest.
            # This simulates a repick; after fix (1) pre refuses to overwrite, so we
            # inject directly to keep this test focused on the investigate-level guard.
            mf = Path(mdir) / f"{m2._RUN_ID}.json"
            mfdata = json.loads(mf.read_text())
            mfdata["items"]["pattern_pick"] = {
                "pattern_id": _PATTERN_ID_B, "code_family": "P0SW2",
                "predicted_failure_mode_id": _FM_ID_B,
                "software_option_id": f"opt-sw-{_PATTERN_ID_B[:8]}",
                "recommended_option_id": f"opt-sw-{_PATTERN_ID_B[:8]}",
            }
            mf.write_text(json.dumps(mfdata))

            # Investigate with B pick but investigation for A already recorded
            rc3, text3, rows3 = m2._full_run(w, mod, parts=["investigate"])

    assert first == 2, f"first investigate made {first} start calls, expected 2\n{text1}"
    # Guard must stop second investigate before any handle_start_investigation call
    assert posts[0] == 2, (
        f"total handle_start_investigation calls {posts[0]}, expected 2 "
        f"(second investigate must be refused by pattern-mismatch guard)\n{text3}"
    )
    assert any("recorded investigation belongs to the pick" in k for k in _fails(rows3)), (
        f"expected FAIL about investigation/pick pattern mismatch\n{_fails(rows3)}\n{text3}"
    )
    assert rc3 == 1


# ---------------------------------------------------------------------------
# B12 — pre refuses to overwrite pattern_pick once investigation_id is recorded
# ---------------------------------------------------------------------------

def test_b12_pre_refuses_overwrite_pick_after_investigation() -> None:
    """Mutant: guard in _pick_pattern before update_manifest("pattern_pick") dropped.

    Two patterns seeded (A and B).  First run: pre picks A, investigate records
    investigation_id.  Second pre: A is locked; without the guard it would pick B and
    overwrite pattern_pick — the guard must refuse (FAIL + rc 1).
    """
    with CmsWorld(run_scheduled=False) as w:
        m2._setup_world(w)
        _maker.seed_pattern(w, pattern_id=m2._PATTERN_ID, failure_mode_id=m2._FM_ID,
                            vin_list=[_seed.vin(0), _seed.vin(1)])
        _maker.seed_pattern(w, pattern_id=_PATTERN_ID_B, failure_mode_id=_FM_ID_B,
                            vin_list=[_seed.vin(2), _seed.vin(3)], code_family="P0SW2")
        w.refresh_population()
        now_iso = m2._now_iso()
        m2._write_pop_caches(w, pattern_list=[
            {"patternId": m2._PATTERN_ID, "codeFamily": "P0SW1",
             "topCause": {"cohortDimension": "model", "cohortValue": "Azimuth",
                          "candidateRank": 1, "rateRatio": 5.0, "pValue": 0.0001},
             "candidateCount": 1, "vehiclesAffected": 50, "vehiclesInService": 1000,
             "rate": 0.05, "firstDetectedMonth": "2026-01", "trend": "rising",
             "method": "cohort", "computedAt": now_iso, "parametersVersion": "1.0"},
            {"patternId": _PATTERN_ID_B, "codeFamily": "P0SW2",
             "topCause": {"cohortDimension": "model", "cohortValue": "Azimuth",
                          "candidateRank": 1, "rateRatio": 4.0, "pValue": 0.0002},
             "candidateCount": 1, "vehiclesAffected": 30, "vehiclesInService": 800,
             "rate": 0.04, "firstDetectedMonth": "2026-02", "trend": "stable",
             "method": "cohort", "computedAt": now_iso, "parametersVersion": "1.0"},
        ])
        mod = m2._load_mod(w)
        with _one_manifest_dir() as mdir:
            rc1, text1, rows1 = m2._full_run(w, mod, parts=["pre", "investigate"])
            assert rc1 == 0 and not _fails(rows1), f"{_fails(rows1)}\n{text1}"
            # Second pre: A locked → would pick B and overwrite; guard must refuse
            rc2, text2, rows2 = m2._full_run(w, mod, parts=["pre"])
            mf2 = Path(mdir) / f"{m2._RUN_ID}.json"
            pick_after = (json.loads(mf2.read_text())["items"].get("pattern_pick") or {})
    assert any("overwrite" in k for k in _fails(rows2)), f"{_fails(rows2)}\n{text2}"
    assert rc2 == 1
    assert pick_after.get("pattern_id") == m2._PATTERN_ID, (
        f"pattern_pick must be unchanged (still A) after refused pre; got {pick_after!r}"
    )


# ---------------------------------------------------------------------------
# B13 — investigate asserts polled investigation's pattern_id == pick
# ---------------------------------------------------------------------------

def test_b13_polled_investigation_pattern_id_checked() -> None:
    """Mutant: `final_body.get("pattern_id") == gid` check dropped.

    Patches pump_investigation to overwrite pattern_id in DDB after completion, so
    the GET poll response returns a different pattern_id than the pick.
    """
    with CmsWorld(run_scheduled=False) as w:
        _world_with_pattern(w)
        orig_pump = _maker.pump_investigation

        def _bad_pump(world: Any, inv_id: str) -> dict:
            out = orig_pump(world, inv_id)
            boto3.resource("dynamodb", region_name=REGION).Table(
                _maker.INVESTIGATIONS_TABLE
            ).update_item(
                Key={"investigation_id": inv_id},
                UpdateExpression="SET pattern_id = :p",
                ExpressionAttributeValues={":p": "wrong-pattern-id"},
            )
            return out

        _maker.pump_investigation = _bad_pump
        mod = m2._load_mod(w)
        try:
            rc, text, rows = m2._full_run(w, mod, parts=["pre", "investigate"])
        finally:
            _maker.pump_investigation = orig_pump
    assert any("polled investigation belongs to pick" in k for k in _fails(rows)), (
        f"{_fails(rows)}\n{text}"
    )
    assert rc == 1


# ---------------------------------------------------------------------------
# B14 — repeat POST skipped when investigation's requestedAt >= 7 days old
# ---------------------------------------------------------------------------

def test_b14_idempotency_skipped_when_requested_at_stale() -> None:
    """Mutant: requestedAt age check dropped (repeat POST always runs, creating a new investigation)."""
    with CmsWorld(run_scheduled=False) as w:
        _world_with_pattern(w)
        orig_pump = _maker.pump_investigation
        _stale_ts = m2._now_iso_offset(-7 * 24 * 3600 - 60)  # 7 days + 1 min ago

        def _stale_pump(world: Any, inv_id: str) -> dict:
            out = orig_pump(world, inv_id)
            boto3.resource("dynamodb", region_name=REGION).Table(
                _maker.INVESTIGATIONS_TABLE
            ).update_item(
                Key={"investigation_id": inv_id},
                UpdateExpression="SET requested_at = :r",
                ExpressionAttributeValues={":r": _stale_ts},
            )
            return out

        _maker.pump_investigation = _stale_pump
        posts = _count_calls(w.cms_fi._mod.maker, "handle_start_investigation")
        mod = m2._load_mod(w)
        try:
            rc, text, rows = m2._full_run(w, mod, parts=["pre", "investigate"])
        finally:
            _maker.pump_investigation = orig_pump
    # Only original POST ran; repeat skipped (stale requestedAt)
    assert posts[0] == 1, f"expected 1 start call (original only), got {posts[0]}\n{text}"
    assert not _fails(rows), f"{_fails(rows)}\n{text}"
    assert rc == 0, text


# ---------------------------------------------------------------------------
# B16 — pre < 2h requirement applies to first POST only (resume skips age check)
# ---------------------------------------------------------------------------

def test_b16_resume_investigate_skips_pre_freshness_check() -> None:
    """Mutant: pre < 2h check applied unconditionally (resume also requires fresh pre)."""
    with CmsWorld(run_scheduled=False) as w:
        _world_with_pattern(w)
        mod = m2._load_mod(w)
        with _one_manifest_dir() as mdir:
            rc1, text1, rows1 = m2._full_run(w, mod, parts=["pre", "investigate"])
            assert rc1 == 0 and not _fails(rows1), f"{_fails(rows1)}\n{text1}"
            # Make pre appear stale (3h ago) to simulate coming back for a resume
            mf = Path(mdir) / f"{m2._RUN_ID}.json"
            data = json.loads(mf.read_text())
            data["items"]["pre"]["completed_at"] = m2._now_iso_offset(-3 * 3600)
            mf.write_text(json.dumps(data))
            rc2, text2, rows2 = m2._full_run(w, mod, parts=["investigate"])
    assert not any(
        "pre < 2h old" in k and v == "FAIL" for k, v in rows2.items()
    ), f"resume should not fail the pre freshness check\n{_fails(rows2)}\n{text2}"
    assert rc2 == 0, text2


# ---------------------------------------------------------------------------
# B17: A-3/W3 — investigate refuses when pre.fresh=False, pre.passed=False, or future ts
# ---------------------------------------------------------------------------


def test_b17_investigate_refuses_unfresh_or_failed_pre() -> None:
    """Mutant: `if age_s >= _FRESH_MAX_AGE_S: return` — doesn't check fresh or passed.

    Three cases each expect handle_start_investigation called 0 times and rc 1:
    (a) pre.fresh = False  (pre run without --fresh)
    (b) pre.passed = False  (alarm or check failure during pre)
    (c) pre.completed_at 10 min in the future  (clock skew → negative age)
    """
    with CmsWorld(run_scheduled=False) as w:
        _world_with_pattern(w)
        mod = m2._load_mod(w)
        posts = _count_calls(w.cms_fi._mod.maker, "handle_start_investigation")

        # (a) fresh=False -------------------------------------------------------
        with _one_manifest_dir() as mdir:
            m2._full_run(w, mod, parts=["pre"])
            mf = Path(mdir) / f"{m2._RUN_ID}.json"
            data = json.loads(mf.read_text())
            data["items"]["pre"]["fresh"] = False
            mf.write_text(json.dumps(data))
            rc_a, text_a, rows_a = m2._full_run(w, mod, parts=["investigate"])
        assert posts[0] == 0, (
            f"(a) handle_start_investigation was called {posts[0]} times; "
            f"fresh=False should have caused a refusal\n{text_a}"
        )
        assert rc_a == 1, f"(a) expected rc=1, got {rc_a}\n{text_a}"

        # (b) passed=False -------------------------------------------------------
        with _one_manifest_dir() as mdir:
            m2._full_run(w, mod, parts=["pre"])
            mf = Path(mdir) / f"{m2._RUN_ID}.json"
            data = json.loads(mf.read_text())
            data["items"]["pre"]["passed"] = False
            mf.write_text(json.dumps(data))
            rc_b, text_b, rows_b = m2._full_run(w, mod, parts=["investigate"])
        assert posts[0] == 0, (
            f"(b) handle_start_investigation was called {posts[0]} times; "
            f"passed=False should have caused a refusal\n{text_b}"
        )
        assert rc_b == 1, f"(b) expected rc=1, got {rc_b}\n{text_b}"

        # (c) future completed_at (clock skew) -----------------------------------
        with _one_manifest_dir() as mdir:
            m2._full_run(w, mod, parts=["pre"])
            mf = Path(mdir) / f"{m2._RUN_ID}.json"
            data = json.loads(mf.read_text())
            data["items"]["pre"]["completed_at"] = m2._now_iso_offset(+600)
            mf.write_text(json.dumps(data))
            rc_c, text_c, rows_c = m2._full_run(w, mod, parts=["investigate"])
        assert posts[0] == 0, (
            f"(c) handle_start_investigation was called {posts[0]} times; "
            f"future completed_at should have caused a refusal\n{text_c}"
        )
        assert rc_c == 1, f"(c) expected rc=1, got {rc_c}\n{text_c}"



# ---------------------------------------------------------------------------
# B18: A-3/W4 — decide/issue refuse when the preceding part had FAIL rows
# ---------------------------------------------------------------------------


def test_b18_no_write_after_failed_part() -> None:
    """Mutant: decide/issue prereq checks only for id presence, ignoring part.passed.

    (a) investigate.passed=False in manifest (+ investigation_id injected so the old
        inv_id prereq doesn't short-circuit) -> decide refuses; handle_decision 0 times.
    (b) decide.passed=False in manifest (+ thread_id injected; _poll_issue_finding
        patched to return a synthetic item so the old thread_id prereq doesn't stop it)
        -> issue refuses; handle_issue 0 times.
    Both cases fail on old code (decide/issue lack the passed check) and pass on new code.
    """
    # (a) investigate.passed=False -> decide refused --------------------------------
    with CmsWorld(run_scheduled=False) as w:
        _world_with_pattern(w)
        mod = m2._load_mod(w)
        decisions = _count_calls(w.cms_fi._mod.maker, "handle_decision")
        with _one_manifest_dir() as mdir:
            m2._full_run(w, mod, parts=["pre"])
            mf = Path(mdir) / f"{m2._RUN_ID}.json"
            data = json.loads(mf.read_text())
            # Inject investigate record with passed=False, plus investigation_id and
            # finding_id so the old `if not inv_id or not pick` prereq doesn't fire.
            data["items"]["investigate"] = {"completed_at": m2._now_iso(), "passed": False}
            data["items"]["investigation_id"] = "inv-fake-b18"
            data["items"]["investigation_pattern_id"] = (
                (data["items"].get("pattern_pick") or {}).get("pattern_id") or "fake-pid"
            )
            data["items"]["finding_id"] = "FIND-fake-b18"
            mf.write_text(json.dumps(data))
            rc_dec, text_dec, rows_dec = m2._full_run(w, mod, parts=["decide"])
    assert decisions[0] == 0, (
        "(a) handle_decision called " + str(decisions[0]) + " times; "
        "investigate.passed=False should have refused\n"
        "FAILs: " + str(_fails(rows_dec))
    )
    assert rc_dec == 1, "(a) expected rc=1, got " + str(rc_dec)

    # (b) decide.passed=False -> issue refused --------------------------------------
    with CmsWorld(run_scheduled=False) as w:
        _world_with_pattern(w)
        mod = m2._load_mod(w)
        issues = _count_calls(w.cms_fi._mod.maker, "handle_issue")

        # Patch _poll_issue_finding so the old `issue_finding not found` path doesn't
        # prevent handle_issue from being reached on OLD code.
        _FAKE_THREAD = "thread-fake-b18"

        def _fake_poll(ddb_w: Any, stage: str, thread_id: str, **kw: Any) -> Any:
            if thread_id == _FAKE_THREAD:
                # Return a minimal item that passes run_issue's finding_item checks.
                return {"finding_key": {"S": "fk-fake"}, "agent_id": {"S": "fake"},
                        "finding_kind": {"S": "quality.issue"}}
            return None

        mod._poll_issue_finding = _fake_poll

        with _one_manifest_dir() as mdir:
            m2._full_run(w, mod, parts=["pre"])
            mf = Path(mdir) / f"{m2._RUN_ID}.json"
            data = json.loads(mf.read_text())
            data["items"]["decide"] = {"completed_at": m2._now_iso(), "passed": False}
            data["items"]["thread_id"] = _FAKE_THREAD
            mf.write_text(json.dumps(data))
            rc_iss, text_iss, rows_iss = m2._full_run(w, mod, parts=["issue"])
    assert issues[0] == 0, (
        "(b) handle_issue called " + str(issues[0]) + " times; "
        "decide.passed=False should have refused\n"
        "FAILs: " + str(_fails(rows_iss))
    )
    assert rc_iss == 1, "(b) expected rc=1, got " + str(rc_iss)


# ---------------------------------------------------------------------------
# B19: W5 decision word must match option vs recommended; recommendedOptionId checked
# ---------------------------------------------------------------------------

def test_b19_decision_word_matches_option() -> None:
    """W5 two guards.

    (a) Mutant: patch _project_action to swap approved↔overridden → decide FAIL.
        Old check ``decision in ('approved', 'overridden')`` passes the swap;
        new check ``decision == ('approved' if sw==rec else 'overridden')`` catches it.

    (b) Mutant: patch project_investigation_result to return a mismatched
        recommendedOptionId → investigate FAIL.
        Old code has no such check; new check ``result_rec_opt_id == rec_opt_id`` catches it.
        (In the world, rec_opt_id == 'opt-mon-001', sw_opt_id == 'opt-sw-…', so they differ;
        the quality agent returns 'opt-mon-001' as the recommended option because it is first
        alphabetically in the schema enum — same source as opts[0].)
    """
    # (a) swap decision word → decide FAIL -----------------------------------------
    with CmsWorld(run_scheduled=False) as w:
        _world_with_pattern(w)
        fi_maker = w.cms_fi._mod.maker
        _orig_pa = fi_maker._project_action

        def _swap_decision(body: Any) -> dict:
            r = dict(_orig_pa(body))
            if r.get("decision") == "approved":
                r["decision"] = "overridden"
            elif r.get("decision") == "overridden":
                r["decision"] = "approved"
            return r

        fi_maker._project_action = _swap_decision
        mod = m2._load_mod(w)
        rc, text, rows = m2._full_run(w, mod, parts=["pre", "investigate", "decide"])

    decide_fails = [k for k in _fails(rows) if "decision" in k.lower()]
    assert decide_fails, (
        "(a) expected decide FAIL for swapped decision word; "
        f"all fails={_fails(rows)}\n{text}"
    )
    assert rc == 1, f"(a) expected rc=1; got {rc}\n{text}"

    # (b) wrong recommendedOptionId → investigate FAIL -----------------------------
    with CmsWorld(run_scheduled=False) as w:
        _world_with_pattern(w)
        fi_maker = w.cms_fi._mod.maker
        _orig_pir = fi_maker.project_investigation_result

        def _wrong_rec(finding: Any) -> dict:
            r = dict(_orig_pir(finding))
            r["recommendedOptionId"] = "wrong-not-in-pick"
            return r

        fi_maker.project_investigation_result = _wrong_rec
        mod = m2._load_mod(w)
        rc_inv, text_inv, rows_inv = m2._full_run(w, mod, parts=["pre", "investigate"])

    inv_fails = [k for k in _fails(rows_inv) if "recommended" in k.lower()]
    assert inv_fails, (
        "(b) expected investigate FAIL for wrong recommendedOptionId; "
        f"all fails={_fails(rows_inv)}\n{text_inv}"
    )
    assert rc_inv == 1, f"(b) expected rc=1; got {rc_inv}\n{text_inv}"


# ---------------------------------------------------------------------------
# B19 — decide refuses when pick.pattern_id != investigation_pattern_id
# ---------------------------------------------------------------------------


def test_b19_decide_refuses_on_pick_pattern_mismatch() -> None:
    """Mutant: mismatch check between pick.pattern_id and investigation_pattern_id dropped in decide.

    Two patterns seeded (A and B).  First run: pre picks A, investigate records
    investigation_pattern_id=A.  Direct manifest injection: pattern_pick overwritten
    to B (simulates the corruption Warning 1 describes — old pre would cause this).
    decide must refuse (FAIL + rc 1, handle_decision called 0 times).
    """
    with CmsWorld(run_scheduled=False) as w:
        m2._setup_world(w)
        _maker.seed_pattern(w, pattern_id=m2._PATTERN_ID, failure_mode_id=m2._FM_ID,
                            vin_list=[_seed.vin(0), _seed.vin(1)])
        _maker.seed_pattern(w, pattern_id=_PATTERN_ID_B, failure_mode_id=_FM_ID_B,
                            vin_list=[_seed.vin(2), _seed.vin(3)], code_family="P0SW2")
        w.refresh_population()
        now_iso = m2._now_iso()
        m2._write_pop_caches(w, pattern_list=[
            {"patternId": m2._PATTERN_ID, "codeFamily": "P0SW1",
             "topCause": {"cohortDimension": "model", "cohortValue": "Azimuth",
                          "candidateRank": 1, "rateRatio": 5.0, "pValue": 0.0001},
             "candidateCount": 1, "vehiclesAffected": 50, "vehiclesInService": 1000,
             "rate": 0.05, "firstDetectedMonth": "2026-01", "trend": "rising",
             "method": "cohort", "computedAt": now_iso, "parametersVersion": "1.0"},
            {"patternId": _PATTERN_ID_B, "codeFamily": "P0SW2",
             "topCause": {"cohortDimension": "model", "cohortValue": "Azimuth",
                          "candidateRank": 1, "rateRatio": 4.0, "pValue": 0.0002},
             "candidateCount": 1, "vehiclesAffected": 30, "vehiclesInService": 800,
             "rate": 0.04, "firstDetectedMonth": "2026-02", "trend": "stable",
             "method": "cohort", "computedAt": now_iso, "parametersVersion": "1.0"},
        ])
        decisions = _count_calls(w.cms_fi._mod.maker, "handle_decision")
        mod = m2._load_mod(w)

        with _one_manifest_dir() as mdir:
            # Run 1: pre picks A, investigate records investigation_pattern_id=A
            rc1, text1, rows1 = m2._full_run(w, mod, parts=["pre", "investigate"])
            assert rc1 == 0 and not _fails(rows1), f"{_fails(rows1)}\n{text1}"

            # Direct manifest injection: overwrite pattern_pick with B.
            # investigation_pattern_id stays as A (set by investigate).
            mf = Path(mdir) / f"{m2._RUN_ID}.json"
            mfdata = json.loads(mf.read_text())
            mfdata["items"]["pattern_pick"] = {
                "pattern_id": _PATTERN_ID_B,
                "code_family": "P0SW2",
                "predicted_failure_mode_id": _FM_ID_B,
                "software_option_id": f"opt-sw-{_PATTERN_ID_B[:8]}",
                "recommended_option_id": f"opt-sw-{_PATTERN_ID_B[:8]}",
            }
            mf.write_text(json.dumps(mfdata))

            rc2, text2, rows2 = m2._full_run(w, mod, parts=["decide"])

    assert decisions[0] == 0, (
        f"decide must not call handle_decision when pick mismatches investigation_pattern_id; "
        f"calls={decisions[0]}\n{text2}"
    )
    mismatch_fails = [k for k in _fails(rows2) if "mismatch" in k.lower() or "pattern" in k.lower()]
    assert mismatch_fails, (
        f"decide must FAIL naming the pattern_id mismatch; got fails={_fails(rows2)}\n{text2}"
    )
    assert rc2 == 1, f"expected rc=1, got {rc2}\n{text2}"


# ---------------------------------------------------------------------------
# B20 — cleanup with pattern_pick=null completes normally and deletes users
# ---------------------------------------------------------------------------


def test_b20_cleanup_with_null_pick_completes() -> None:
    """Mutant: `mdata.get("pattern_pick", {})` instead of `or {}` in cleanup.

    Pre with no qualifying pattern leaves pattern_pick=null in the manifest.
    Before the fix, cleanup crashes with AttributeError on pick.get(…).
    After the fix, cleanup rc 0 and the residue check passes (no livetest users).
    """
    with CmsWorld(run_scheduled=False) as w:
        _world_with_pattern(w)
        mod = m2._load_mod(w)

        with _one_manifest_dir() as mdir:
            # Run pre normally (picks the pattern), then wipe the pick to null to
            # simulate a "pre with no qualifying pattern" state.
            rc_pre, _text_pre, _rows_pre = m2._full_run(w, mod, parts=["pre"])
            mf = Path(mdir) / f"{m2._RUN_ID}.json"
            mfdata = json.loads(mf.read_text())
            mfdata["items"]["pattern_pick"] = None  # explicit null
            mf.write_text(json.dumps(mfdata))

            rc_cl, text_cl, rows_cl = m2._full_run(w, mod, parts=["cleanup"])

    cleanup_fails = _fails(rows_cl)
    assert rc_cl == 0, (
        f"cleanup with null pattern_pick must succeed (rc 0); "
        f"got rc={rc_cl} fails={cleanup_fails}\n{text_cl}"
    )
    assert any("no livetest" in k.lower() and rows_cl[k] == "PASS"
               for k in rows_cl), (
        f"cleanup: no livetest Cognito users check must PASS; rows={rows_cl}\n{text_cl}"
    )


# ---------------------------------------------------------------------------
# B21 — MC2: pre.passed writer — investigate refuses failed pre (A-3)
# ---------------------------------------------------------------------------


def test_b21_investigate_refuses_failed_pre_by_code() -> None:
    """MC2: pre always records passed=True even on failure.

    The quality-signals pop cache is written with a stale computedAt so pre
    FAILS at the freshness check while _pick_pattern still succeeds (the
    patterns cache remains fresh).  With correct code pre records passed=False
    → investigate refuses at 'investigate prereq: pre --fresh and passed', and
    handle_start_investigation is never called.  With MC2 (passed=True always)
    investigate does NOT refuse and calls handle_start_investigation.
    """
    import boto3 as _boto3  # noqa: PLC0415

    with CmsWorld(run_scheduled=False) as w:
        _world_with_pattern(w)
        # Overwrite quality-signals cache with a stale timestamp so pre's
        # pop-cache freshness check FAILs on that cache.  The patterns cache
        # stays fresh so _pick_pattern can still pick a pattern and record
        # pattern_pick (allowing investigate to reach handle_start_investigation).
        _s3 = _boto3.client("s3", region_name="us-east-1")
        _stale_ts = "2020-01-01T00:00:00Z"
        _s3.put_object(
            Bucket=m2._BUCKET,
            Key=m2._CACHE_PREFIX + "_cache/quality-signals-v1.json",
            Body=json.dumps({"computedAt": _stale_ts, "data": []}).encode(),
        )
        mod = m2._load_mod(w)
        posts = _count_calls(w.cms_fi._mod.maker, "handle_start_investigation")

        with _one_manifest_dir():
            # Step 1: pre fails at quality-signals freshness; pattern_pick still recorded
            rc_pre, text_pre, _rows_pre = m2._full_run(w, mod, parts=["pre"])
            # Step 2: investigate — correct code refuses because pre.passed=False
            rc_inv, text_inv, rows_inv = m2._full_run(w, mod, parts=["investigate"])

    assert rc_pre == 1, (
        f"pre must fail (rc=1) when quality-signals cache is stale; "
        f"got rc={rc_pre}\n{text_pre}"
    )
    assert posts[0] == 0, (
        f"handle_start_investigation called {posts[0]} times; "
        f"investigate must refuse when pre.passed=False\n{text_inv}"
    )
    assert rc_inv == 1, (
        f"investigate must refuse (rc=1) when pre.passed=False; got {rc_inv}\n{text_inv}"
    )
    pre_fails = [k for k in _fails(rows_inv)
                 if "pre" in k.lower() and "pass" in k.lower()]
    assert pre_fails, (
        f"expected 'investigate prereq: pre --fresh and passed' FAIL; "
        f"all fails={_fails(rows_inv)}\n{text_inv}"
    )


# ---------------------------------------------------------------------------
# B22 — MC3: investigate.passed writer — decide refuses failed investigate (A-3)
# ---------------------------------------------------------------------------


def test_b22_decide_refuses_failed_investigate_by_code() -> None:
    """MC3: investigate always records passed=True even on failure.

    A duplicate investigation row is injected during investigate's repeat POST
    (step 10 — 'one investigation row for the pattern' FAIL) after investigation_id,
    investigation_pattern_id and finding_id are already recorded.  With correct code
    investigate records passed=False → decide refuses at
    'decide prereq: investigate passed'.  With MC3 (passed=True always) decide
    does NOT refuse and handle_decision is called.
    """
    with CmsWorld(run_scheduled=False) as w:
        _world_with_pattern(w)
        fi_maker = w.cms_fi._mod.maker
        orig_handle = fi_maker.handle_start_investigation
        n = [0]

        def _dup_on_repeat(pattern_id: str, body: Any, event: Any) -> Any:
            n[0] += 1
            if n[0] == 2:  # repeat POST: inject a second row for the pattern
                import boto3 as _b3  # noqa: PLC0415
                _b3.resource("dynamodb", region_name=m2.REGION).Table(
                    _maker.INVESTIGATIONS_TABLE).put_item(Item={
                        "investigation_id": "11111111-2222-3333-4444-555555555555",
                        "pattern_id": pattern_id, "status": "queued"})
            return orig_handle(pattern_id, body, event)

        fi_maker.handle_start_investigation = _dup_on_repeat
        mod = m2._load_mod(w)
        decisions = _count_calls(w.cms_fi._mod.maker, "handle_decision")

        with _one_manifest_dir():
            # Step 1: pre + investigate; investigate FAILs at dup-row check
            rc_inv, text_inv, rows_inv = m2._full_run(w, mod, parts=["pre", "investigate"])
            # Step 2: decide — correct code refuses because investigate.passed=False
            rc_dec, text_dec, rows_dec = m2._full_run(w, mod, parts=["decide"])

    assert any("one investigation row" in k for k in _fails(rows_inv)), (
        f"investigate must FAIL on dup row; fails={_fails(rows_inv)}\n{text_inv}"
    )
    assert rc_inv == 1, f"investigate must fail (rc=1); got {rc_inv}\n{text_inv}"
    assert decisions[0] == 0, (
        f"handle_decision called {decisions[0]} times; "
        f"decide must refuse when investigate.passed=False\n{text_dec}"
    )
    assert rc_dec == 1, (
        f"decide must refuse (rc=1) when investigate.passed=False; got {rc_dec}\n{text_dec}"
    )
    inv_fails = [k for k in _fails(rows_dec)
                 if "investigate" in k.lower() and "pass" in k.lower()]
    assert inv_fails, (
        f"expected 'decide prereq: investigate passed' FAIL; "
        f"all fails={_fails(rows_dec)}\n{text_dec}"
    )


# ---------------------------------------------------------------------------
# B23 — MC4: decide.passed writer — issue refuses failed decide (A-3)
# ---------------------------------------------------------------------------


def test_b23_issue_refuses_failed_decide_by_code() -> None:
    """MC4: decide always records passed=True even on failure.

    The ADP thread record is patched to return a wrong affectedVinsSha256 so
    decide FAILS at the SHA check while thread_id (and thread_version) are still
    recorded in the manifest afterwards.  With correct code decide records
    passed=False → issue refuses at 'issue prereq: decide passed', and
    handle_issue is never called.  With MC4 (passed=True always) issue does NOT
    refuse and calls handle_issue.
    """
    with CmsWorld(run_scheduled=False) as w:
        _world_with_pattern(w)
        fi_maker = w.cms_fi._mod.maker
        orig_proj_thread = fi_maker.project_thread_record

        def _bad_sha(item: dict) -> dict:
            out = orig_proj_thread(item)
            out["affectedVinsSha256"] = "0" * 64  # wrong SHA
            return out

        fi_maker.project_thread_record = _bad_sha
        mod = m2._load_mod(w)
        issues = _count_calls(w.cms_fi._mod.maker, "handle_issue")

        # Patch _poll_issue_finding so issue can potentially reach handle_issue
        # on OLD code (without the decide.passed guard) — needed so the mutant
        # actually calls handle_issue rather than stopping at a different prereq.
        _FAKE_THREAD_B23 = None  # filled after decide runs

        orig_poll = mod._poll_issue_finding

        def _fast_poll(ddb_w: Any, stage: str, thread_id: str, **kw: Any) -> dict | None:
            return orig_poll(ddb_w, stage, thread_id, poll_interval_s=0, max_wait_s=5)

        mod._poll_issue_finding = _fast_poll

        with _one_manifest_dir():
            # Step 1: pre + investigate + decide; decide FAILs at SHA check
            rc_dec, text_dec, rows_dec = m2._full_run(
                w, mod, parts=["pre", "investigate", "decide"])
            # Step 2: issue — correct code refuses because decide.passed=False
            rc_iss, text_iss, rows_iss = m2._full_run(w, mod, parts=["issue"])

    assert any("sha" in k.lower() for k in _fails(rows_dec)), (
        f"decide must FAIL at SHA check; fails={_fails(rows_dec)}\n{text_dec}"
    )
    assert rc_dec == 1, f"decide must fail (rc=1); got {rc_dec}\n{text_dec}"
    assert issues[0] == 0, (
        f"handle_issue called {issues[0]} times; "
        f"issue must refuse when decide.passed=False\n{text_iss}"
    )
    assert rc_iss == 1, (
        f"issue must refuse (rc=1) when decide.passed=False; got {rc_iss}\n{text_iss}"
    )
    dec_fails = [k for k in _fails(rows_iss)
                 if "decide" in k.lower() and "pass" in k.lower()]
    assert dec_fails, (
        f"expected 'issue prereq: decide passed' FAIL; "
        f"all fails={_fails(rows_iss)}\n{text_iss}"
    )


def test_b24_issue_refuses_on_pick_pattern_mismatch() -> None:
    """Architect (c2, review-A W1): `issue` refuses when the recorded pick no longer
    matches the investigated pattern. pre + investigate + decide pass on pattern A; the
    manifest's pattern_pick is then changed to another pattern id (the corruption review-A
    Warning 1 describes); issue must FAIL naming the mismatch and never call handle_issue.
    Deliberate break: the issue guard's condition -> `if False:` (handle_issue is called)."""
    with CmsWorld(run_scheduled=False) as w:
        _world_with_pattern(w)
        mod = m2._load_mod(w)
        issues = _count_calls(w.cms_fi._mod.maker, "handle_issue")
        orig_poll = mod._poll_issue_finding

        def _fast_poll(ddb_w: Any, stage: str, thread_id: str, **kw: Any) -> dict | None:
            return orig_poll(ddb_w, stage, thread_id, poll_interval_s=0, max_wait_s=5)

        mod._poll_issue_finding = _fast_poll
        with _one_manifest_dir() as mdir:
            rc1, text1, rows1 = m2._full_run(w, mod, parts=["pre", "investigate", "decide"])
            assert rc1 == 0 and not _fails(rows1), f"{_fails(rows1)}\n{text1}"
            mf = Path(mdir) / f"{m2._RUN_ID}.json"
            mfdata = json.loads(mf.read_text())
            mfdata["items"]["pattern_pick"] = dict(
                mfdata["items"]["pattern_pick"], pattern_id="b" * 16)
            mf.write_text(json.dumps(mfdata))
            rc2, text2, rows2 = m2._full_run(w, mod, parts=["issue"])
    assert issues[0] == 0, (
        f"issue must not call handle_issue on a pick mismatch; calls={issues[0]}\n{text2}"
    )
    assert any("issue prereq: pattern_pick matches investigation_pattern_id" in k
               for k in _fails(rows2)), (
        f"fails={_fails(rows2)}\n{text2}"
    )
    assert rc2 == 1, f"expected rc=1, got {rc2}\n{text2}"
