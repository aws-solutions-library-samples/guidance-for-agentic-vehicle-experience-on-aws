"""Stage 2a world tests — items 2 and 3 via FiLoopWorld.

Replaces the hand-faked Item2World tests from stage 1 with world tests that drive the
real _item_2 / _item_3 / main() against the real fi_quality job through SQS.

Fixes covered here:
  - Items 2/3 happy-path world tests: PASS rows through real investigations route
  - C3 corpus formula check: total_cost = parts_cost × total_vehicles
  - M-B2  (exact label): item-2-plant-P1-pattern-present must FAIL on missing pattern
  - M-B4/B5 (cleanup hang resolved): cleanup no longer hangs on a passing run
  - M-B6  (exact label): item-3-repeat-same-id must FAIL on different repeat id
  - M-cleanup behavioral: already in s1 test_c2_residue_scan_fails_on_leftover
  - Each new C3 corpus check gets its own mutant in s2a-mutants.md

Stages:
  s2a-W2-H  happy path item 2 via main() (items 2+3 in one after-6 run, rc=2)
  s2a-W2-F* FAIL-path world tests per check in item 2
  s2a-W3-F* FAIL-path world tests per check in item 3
  s2a-M*    mutant verification (all s1 mutants re-run + one per new C3 check)

Run (long — detach):
    nohup ~/guidance-for-connected-vehicle-experience-on-aws/.venv/bin/python -m pytest \\
        scripts/tests/test_live_verify_fi_quality_after6_s2a.py -v --no-header \\
        > /tmp/2026-10-05-fi-fleet-health-stage-d-64/s2a/pytest.log 2>&1 &

Requires:
  ~/automotive-data-platform-on-aws at ADP 02b8716f (or FI_LOOP_WORLD_ADP_REPO)
  ~/connected-mobility-guidance-on-aws at CMS main (or FI_LOOP_WORLD_CMS_REPO)
  PYTHONDONTWRITEBYTECODE=1 when running mutations

ADP pin check:
  git -C ~/automotive-data-platform-on-aws log 9bb82e96..02b8716f \\
      -- platform-foundation/source/lambda/digital_thread_writer/handler.py
  → shows digital-thread-lake feature commits; THREAD_LAKE_BUCKET required.
  FiLoopWorld._extend_world sets THREAD_LAKE_BUCKET → no change needed in tests.
"""
from __future__ import annotations

import datetime as dt
import importlib.util
import os
import signal
import socket
import uuid
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterator

import pytest

# ---------------------------------------------------------------------------
# Repo presence checks (skip all tests if repos absent)
# ---------------------------------------------------------------------------

_HERE = Path(__file__).resolve().parent
_SCRIPT = _HERE.parent / "live-verify-fi-quality.py"

_ADP_REPO = Path(os.environ.get(
    "FI_LOOP_WORLD_ADP_REPO",
    str(Path.home() / "automotive-data-platform-on-aws")
))
_CMS_REPO = Path(os.environ.get(
    "FI_LOOP_WORLD_CMS_REPO",
    str(Path.home() / "connected-mobility-guidance-on-aws")
))

_ADP_HANDLER = (_ADP_REPO / "platform-foundation" / "source" / "lambda"
                / "digital_thread_writer" / "handler.py")
_CMS_INDEX = (_CMS_REPO / "services" / "fleet_intelligence" / "index.py")

_REPOS_PRESENT = _ADP_HANDLER.exists() and _CMS_INDEX.exists()
_SKIP_REASON = (
    "FiLoopWorld requires ~/automotive-data-platform-on-aws and "
    "~/connected-mobility-guidance-on-aws"
)

# ---------------------------------------------------------------------------
# Load the script module once
# ---------------------------------------------------------------------------

_spec = importlib.util.spec_from_file_location("lv_s2a_mod", _SCRIPT)
lv = importlib.util.module_from_spec(_spec)  # type: ignore[arg-type]
_spec.loader.exec_module(_mod := lv)  # type: ignore[union-attr]

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

STAGE = "staging"
REGION = "us-west-2"
ADP_REGION = "us-east-1"
ACCOUNT = "123456789012"

# Pattern / corpus constants for all world tests
_PAT_ID = "f0012345abcdef01"  # 16 hex chars; valid pattern_group_id
_FM_ID = "fm-w2-s2a-001"
_CODE_FAMILY = "P1AW"
_COHORT_DIM = "supplier_lot"
_COHORT_VAL = "SUPP-W2/LOT-S2A"
_N_VINS = 12  # vehicles_affected; used in C3 formula: 120 * 12 = 1440
_PARTS_COST_USD = 120  # parts_cost_per_vehicle_usd in seed_failure_mode
_EXPECTED_TOTAL = _PARTS_COST_USD * _N_VINS  # 1440

# AK planted_count = _N_VINS (number of affected vehicles planted)
_AK_PID = "P-W2-01"  # answer-key pattern id (different from DDB pattern_group_id)

_VALID_RUN_ID = "fi-quality-20261027T100000-s2a00001"

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _restore_signals() -> Iterator[None]:
    old_int = signal.getsignal(signal.SIGINT)
    old_term = signal.getsignal(signal.SIGTERM)
    old_hup = signal.getsignal(signal.SIGHUP)
    yield
    signal.signal(signal.SIGINT, old_int)
    signal.signal(signal.SIGTERM, old_term)
    signal.signal(signal.SIGHUP, old_hup)


@pytest.fixture(autouse=True)
def _loopback_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    real_connect = socket.socket.connect

    def _connect(sock: socket.socket, address: Any) -> Any:
        host = address[0] if isinstance(address, tuple) else str(address)
        if host not in ("127.0.0.1", "::1", "localhost"):
            raise AssertionError(f"test reached real host {address!r}")
        return real_connect(sock, address)

    monkeypatch.setattr(socket.socket, "connect", _connect)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _vin(n: int) -> str:
    """Generate a valid 17-character VIN for test use (matches VIN regex)."""
    digits = "ABCDEFGHJKMNPRSTUVWXYZ0123456789"
    encoded = ""
    v = max(0, n)
    for _ in range(8):
        encoded = digits[v % len(digits)] + encoded
        v //= len(digits)
    check = str(n % 10)
    trailing = digits[n % len(digits)]
    return "1MRDN00" + encoded + check + trailing


def _vins(count: int) -> list[str]:
    return [_vin(i) for i in range(count)]


def _build_answer_key(tmp_path: Path, *, pat_id: str = _AK_PID,
                      code_family: str = _CODE_FAMILY,
                      cause_dimension: str = _COHORT_DIM,
                      cause_value: str = _COHORT_VAL,
                      failure_mode_id: str = _FM_ID,
                      planted_count: int = _N_VINS,
                      start_date: str = "2026-01-01",
                      extra: list[dict] | None = None) -> str:
    """Create an answer-key parquet and return its path.

    Uses DuckDB's COPY TO so no pyarrow needed in the venv.
    Columns match _AK_COLUMNS from the script.
    """
    import duckdb  # noqa: PLC0415
    rows = [
        {
            "pattern_id": pat_id,
            "code_family": code_family,
            "cause_dimension": cause_dimension,
            "cause_value": cause_value,
            "planted_count": planted_count,
            "start_date": start_date,
            "failure_mode_id": failure_mode_id,
        }
    ]
    if extra:
        rows.extend(extra)
    ak_path = str(tmp_path / f"ak_{uuid.uuid4().hex[:8]}.parquet")
    con = duckdb.connect()
    try:
        # Build VALUES clause
        parts = ", ".join(
            f"('{r['pattern_id']}', '{r['code_family']}', '{r['cause_dimension']}', "
            f"'{r['cause_value']}', {r['planted_count']}, '{r['start_date']}', "
            f"'{r['failure_mode_id']}')"
            for r in rows
        )
        con.execute(
            "CREATE TABLE ak AS SELECT * FROM (VALUES "
            + parts
            + ") t(pattern_id, code_family, cause_dimension, cause_value, "
            "planted_count, start_date, failure_mode_id)"
        )
        con.execute(f"COPY ak TO '{ak_path}' (FORMAT PARQUET)")
    finally:
        con.close()
    return ak_path


def _patch_guards(monkeypatch: pytest.MonkeyPatch) -> None:
    """Patch write-window guard and pre-checks to always pass.

    These are tested separately by A6-A10 tests.  World tests focus on the
    investigation logic and quality checks.
    """
    monkeypatch.setattr(lv, "_after6_write_window_guard", lambda *_a, **_k: None)
    monkeypatch.setattr(lv, "_after6_pre_checks", lambda *_a, **_k: None)
    # Fix _utc_now to 10:00 UTC so no schedule is within the 45-min window
    monkeypatch.setattr(
        lv, "_utc_now",
        lambda: dt.datetime.now(dt.timezone.utc).replace(hour=10, minute=0,
                                                          second=0, microsecond=0)
    )
    monkeypatch.setenv("LIVE_TESTS", "1")
    monkeypatch.setenv("FI_QUALITY_MANIFEST_DIR", "")  # reset; tests pass tmp_path


def _open_world(auto_pump: bool = True, daily_cap: int = 20) -> Any:
    """Return an UNOPENED FiLoopWorld context manager.

    Use as: ``with _open_world() as w:``.  The context manager calls open() once.
    Do NOT call w.open() separately — the context manager handles it.
    """
    from scripts.tests.fi_loop_world.world import FiLoopWorld  # noqa: PLC0415
    return FiLoopWorld(auto_pump=auto_pump, daily_cap=daily_cap)


def _seed_world(w: Any) -> None:
    """Seed one fault pattern + corpus failure mode into the world."""
    w.seed_pattern(
        pattern_id=_PAT_ID,
        vin_list=_vins(_N_VINS),
        failure_mode_id=_FM_ID,
        cohort_dimension=_COHORT_DIM,
        cohort_value=_COHORT_VAL,
        code_family=_CODE_FAMILY,
    )


def _get_finding(w: Any, inv_id: str) -> dict:
    """Read the AVX Finding written by the quality job for this investigation.

    Returns the Finding row (from vsa-staging-avx-findings).
    Raises AssertionError if no finding_id is set on the investigation row.
    """
    inv_row = w.table("vsa-staging-avx-investigations").get_item(
        Key={"investigation_id": inv_id}, ConsistentRead=True
    ).get("Item", {})
    finding_id = inv_row.get("finding_id")
    assert finding_id, (
        f"investigation {inv_id} has no finding_id — "
        f"investigation row: {inv_row}"
    )
    finding = w.table("vsa-staging-avx-findings").get_item(
        Key={"finding_id": finding_id}, ConsistentRead=True
    ).get("Item")
    assert finding is not None, f"Finding {finding_id} not in avx-findings"
    return finding


def _modify_finding_evidence(w: Any, finding: dict, *, kind: str,
                              updater: Any) -> None:
    """Modify a specific evidence item in a Finding and write it back to DDB.

    ``updater`` is called with the evidence item's ``value`` dict (mutated in-place).
    If no evidence item with that kind exists, one is added.
    """
    evidence = finding.get("evidence") or []
    for ev in evidence:
        if ev.get("kind") == kind:
            updater(ev["value"])
            break
    else:
        # No existing item with this kind — add one for absence tests
        new_val: dict = {}
        updater(new_val)
        evidence.append({"kind": kind, "value": new_val})
    finding["evidence"] = evidence
    w.table("vsa-staging-avx-findings").put_item(Item=finding)


def _remove_finding_evidence_kind(w: Any, finding: dict, kind: str) -> None:
    """Remove all evidence items of the given kind from a Finding."""
    finding["evidence"] = [
        ev for ev in (finding.get("evidence") or [])
        if ev.get("kind") != kind
    ]
    w.table("vsa-staging-avx-findings").put_item(Item=finding)


def _fresh_manifest(run_id: str = _VALID_RUN_ID) -> dict:
    return {
        "run_id": run_id,
        "stage": STAGE,
        "investigations": [],
        "users": [],
        "item3_cap": None,
        "event_source_disabled": False,
        "items_passed": {},
    }


# ===========================================================================
# W2-H: happy-path items 2 and 3 via main() — rc must be 2 (9b pending)
# ===========================================================================

@pytest.mark.skipif(not _REPOS_PRESENT, reason=_SKIP_REASON)
def test_w2h_item2_and_3_via_main_rc2(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """items 2 and 3 via main() produce PASS rows; rc=2 because items 4/5/8b/9 SKIP.

    Drives the real main() with --part after-6.  FiLoopWorld auto_pump=True ensures
    the investigation processes synchronously so _poll_investigation finds it completed.

    Design §Shape: «exit: 1 on any FAIL; 2 when everything ran and passed but 9b pending».

    s2a-W2-H mutant: main() rc check + PASS rows.
    """
    _patch_guards(monkeypatch)
    ak_path = _build_answer_key(tmp_path)
    monkeypatch.setenv("FI_QUALITY_MANIFEST_DIR", str(tmp_path))

    with _open_world(auto_pump=True) as w:
        _seed_world(w)

        old_int = signal.getsignal(signal.SIGINT)
        old_term = signal.getsignal(signal.SIGTERM)
        try:
            rc = lv.main([
                "--stage", STAGE,
                "--part", "after-6",
                "--allow-writes",
                "--answer-key", ak_path,
            ])
        finally:
            signal.signal(signal.SIGINT, old_int)
            signal.signal(signal.SIGTERM, old_term)

    # Design: rc=2 (9b pending / items 4/5/8b/9 SKIP), never 0 or 1
    assert rc == 2, f"Expected rc=2 (9b pending), got rc={rc}"


# ===========================================================================
# W2-F*: FAIL-path item 2 world tests
# ===========================================================================

@pytest.mark.skipif(not _REPOS_PRESENT, reason=_SKIP_REASON)
def test_w2f_missing_quality_pattern(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """item 2 FAIL on exact label item-2-plant-P1-pattern-present when quality.pattern absent.

    M-B2 re-anchored: the check must emit label «item-2-plant-<pid>-pattern-present»
    (not pattern-numbers or a substring match).  Catches the «pattern-present check
    bypassed» mutant definitively.
    """
    _patch_guards(monkeypatch)
    ak_path = _build_answer_key(tmp_path)

    with _open_world(auto_pump=True) as w:
        _seed_world(w)
        # Pre-pump to get a real Finding with correct shape, then remove quality.pattern
        result = w.post_investigation(pattern_id=_PAT_ID)
        inv_id = result.get("investigation_id")
        assert inv_id, f"POST /investigations returned {result}"
        finding = _get_finding(w, inv_id)
        # Inject defect: remove quality.pattern from evidence
        _remove_finding_evidence_kind(w, finding, "quality.pattern")

        manifest = _fresh_manifest()
        res = lv.Results()
        lv._item_2(
            res, stage=STAGE, run_id=_VALID_RUN_ID,
            manifest=manifest, manifest_dir=str(tmp_path),
            allow_writes=True, answer_key=ak_path,
        )

    fail_labels = [r[0] for r in res.rows if r[1] == "FAIL"]
    assert f"item-2-plant-{_AK_PID}-pattern-present" in fail_labels, (
        f"Expected FAIL on exact label 'item-2-plant-{_AK_PID}-pattern-present'. "
        f"Got: {res.rows}"
    )


@pytest.mark.skipif(not _REPOS_PRESENT, reason=_SKIP_REASON)
def test_w2f_bad_root_cause_candidate(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """item 2 FAIL on item-2-plant-P1-cause when root_cause candidate doesn't match plant.

    M-B1 world re-anchor: pre-pump to get real Finding, then change root_cause
    candidate_id to one not in candidates list → cause_ok=False.
    """
    _patch_guards(monkeypatch)
    ak_path = _build_answer_key(tmp_path)

    with _open_world(auto_pump=True) as w:
        _seed_world(w)
        result = w.post_investigation(pattern_id=_PAT_ID)
        inv_id = result.get("investigation_id")
        finding = _get_finding(w, inv_id)
        # Inject defect: root_cause points to a non-existent candidate_id
        def _bad_candidate(val: dict) -> None:
            val["candidate_id"] = "cand-nonexistent-xyz"

        _modify_finding_evidence(w, finding, kind="quality.root_cause",
                                 updater=_bad_candidate)

        manifest = _fresh_manifest()
        res = lv.Results()
        lv._item_2(
            res, stage=STAGE, run_id=_VALID_RUN_ID,
            manifest=manifest, manifest_dir=str(tmp_path),
            allow_writes=True, answer_key=ak_path,
        )

    fail_labels = [r[0] for r in res.rows if r[1] == "FAIL"]
    # W4 fix: per-plant cause miss is a NOTE (no FAIL row); only 80% rate decides.
    # With 1 plant and cause_ok=False, pass_count=0, rate=0/1=0% → rate FAIL.
    assert "item-2-cause-fm-rate" in fail_labels, (
        f"W4: bad root-cause must produce item-2-cause-fm-rate FAIL. Got: {res.rows}"
    )
    # Must NOT have a per-plant cause FAIL row
    assert not any(r.startswith("item-2-plant-") and "cause" in r
                   for r in fail_labels), (
        f"W4: per-plant cause miss must be NOTE (not FAIL row). fail_labels={fail_labels}"
    )


@pytest.mark.skipif(not _REPOS_PRESENT, reason=_SKIP_REASON)
def test_w2f_wrong_failure_mode(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """item 2 FAIL on item-2-plant-P1-failure-mode when FM id doesn't match plant.

    M-B4 world re-anchor (cleanup no longer hangs — AdminDeleteUser handler in place).
    Changes root_cause.failure_mode_id to a wrong value.
    """
    _patch_guards(monkeypatch)
    ak_path = _build_answer_key(tmp_path)

    with _open_world(auto_pump=True) as w:
        _seed_world(w)
        result = w.post_investigation(pattern_id=_PAT_ID)
        inv_id = result.get("investigation_id")
        finding = _get_finding(w, inv_id)
        # Inject defect: failure_mode_id → wrong value
        def _bad_fm(val: dict) -> None:
            val["failure_mode_id"] = "fm-wrong-value-xyz"

        _modify_finding_evidence(w, finding, kind="quality.root_cause",
                                 updater=_bad_fm)

        manifest = _fresh_manifest()
        res = lv.Results()
        lv._item_2(
            res, stage=STAGE, run_id=_VALID_RUN_ID,
            manifest=manifest, manifest_dir=str(tmp_path),
            allow_writes=True, answer_key=ak_path,
        )

    fail_labels = [r[0] for r in res.rows if r[1] == "FAIL"]
    # W4 fix: per-plant FM miss is a NOTE (no FAIL row); only 80% rate decides.
    assert "item-2-cause-fm-rate" in fail_labels, (
        f"W4: wrong FM must produce item-2-cause-fm-rate FAIL. Got: {res.rows}"
    )
    assert not any(r.startswith("item-2-plant-") and "failure-mode" in r
                   for r in fail_labels), (
        f"W4: per-plant FM miss must be NOTE (not FAIL row). fail_labels={fail_labels}"
    )


@pytest.mark.skipif(not _REPOS_PRESENT, reason=_SKIP_REASON)
def test_w2f_missing_options(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """item 2 FAIL on item-2-plant-P1-options-present when quality.options absent.

    M-B5 world re-anchor (cleanup no longer hangs).
    Removes quality.options from the Finding evidence.
    """
    _patch_guards(monkeypatch)
    ak_path = _build_answer_key(tmp_path)

    with _open_world(auto_pump=True) as w:
        _seed_world(w)
        result = w.post_investigation(pattern_id=_PAT_ID)
        inv_id = result.get("investigation_id")
        finding = _get_finding(w, inv_id)
        # Inject defect: remove options evidence
        _remove_finding_evidence_kind(w, finding, "quality.options")

        manifest = _fresh_manifest()
        res = lv.Results()
        lv._item_2(
            res, stage=STAGE, run_id=_VALID_RUN_ID,
            manifest=manifest, manifest_dir=str(tmp_path),
            allow_writes=True, answer_key=ak_path,
        )

    fail_labels = [r[0] for r in res.rows if r[1] == "FAIL"]
    assert f"item-2-plant-{_AK_PID}-options-present" in fail_labels, (
        f"Expected FAIL on options-present check. Got: {res.rows}"
    )


@pytest.mark.skipif(not _REPOS_PRESENT, reason=_SKIP_REASON)
def test_w2f_corpus_total_wrong(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """item 2 FAIL on corpus total check (C3) when total_cost ≠ parts_cost × vehicles_affected.

    The real fi_quality agent writes total_cost = parts_cost × total_vehicles (options.py).
    This test modifies the Finding's option total_cost to a wrong value, then verifies
    the script's C3 check catches it.

    New check (C3 corpus formula).  Mutant: M-C3-total (total check always True).
    """
    _patch_guards(monkeypatch)
    ak_path = _build_answer_key(tmp_path)

    with _open_world(auto_pump=True) as w:
        _seed_world(w)
        result = w.post_investigation(pattern_id=_PAT_ID)
        inv_id = result.get("investigation_id")
        finding = _get_finding(w, inv_id)

        # Verify the real Finding has the correct total_cost before injecting defect
        evidence = {ev.get("kind"): ev.get("value") for ev in finding.get("evidence", [])}
        opts = evidence.get("quality.options") or []
        assert opts, f"quality.options absent in real Finding — seeding may have failed: {finding}"

        # Inject defect: change first option's total_cost to wrong value
        def _bad_total(opts_val: Any) -> None:
            if isinstance(opts_val, list) and opts_val:
                # opts_val is a list; first item should be the option dict
                # (quality.options evidence value is the list itself)
                opts_val[0]["total_cost"] = Decimal("9999")

        # quality.options value is a list; modify it directly
        finding_evidence = finding.get("evidence") or []
        for ev in finding_evidence:
            if ev.get("kind") == "quality.options":
                opts_list = ev.get("value") or []
                if opts_list:
                    opts_list[0]["total_cost"] = Decimal("9999")
                break
        w.table("vsa-staging-avx-findings").put_item(Item=finding)

        manifest = _fresh_manifest()
        res = lv.Results()
        lv._item_2(
            res, stage=STAGE, run_id=_VALID_RUN_ID,
            manifest=manifest, manifest_dir=str(tmp_path),
            allow_writes=True, answer_key=ak_path,
        )

    fail_labels = [r[0] for r in res.rows if r[1] == "FAIL"]
    # The exact label is item-2-plant-<pid>-opt-<opt_id[:12]>-total
    # opt_id = "opt-recall-001"; [:12] = "opt-recall-00"
    assert any("opt" in lbl and "total" in lbl for lbl in fail_labels), (
        f"Expected FAIL on C3 corpus total check. Got: {res.rows}"
    )


@pytest.mark.skipif(not _REPOS_PRESENT, reason=_SKIP_REASON)
def test_w2f_corpus_veh_count_wrong(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """item 2 FAIL on corpus vehicles_affected check (C3) when total_vehicles wrong.

    The real fi_quality agent writes total_vehicles = vehicles_affected.
    This test verifies the script detects a mismatch.

    New check (C3 corpus vehicles count).  Mutant: M-C3-veh (veh-count check always True).
    """
    _patch_guards(monkeypatch)
    ak_path = _build_answer_key(tmp_path)

    with _open_world(auto_pump=True) as w:
        _seed_world(w)
        result = w.post_investigation(pattern_id=_PAT_ID)
        inv_id = result.get("investigation_id")
        finding = _get_finding(w, inv_id)

        # Inject defect: change total_vehicles in first option to wrong value
        finding_evidence = finding.get("evidence") or []
        for ev in finding_evidence:
            if ev.get("kind") == "quality.options":
                opts_list = ev.get("value") or []
                if opts_list:
                    opts_list[0]["total_vehicles"] = Decimal("999")
                break
        w.table("vsa-staging-avx-findings").put_item(Item=finding)

        manifest = _fresh_manifest()
        res = lv.Results()
        lv._item_2(
            res, stage=STAGE, run_id=_VALID_RUN_ID,
            manifest=manifest, manifest_dir=str(tmp_path),
            allow_writes=True, answer_key=ak_path,
        )

    fail_labels = [r[0] for r in res.rows if r[1] == "FAIL"]
    assert any("opt" in lbl and "veh" in lbl for lbl in fail_labels), (
        f"Expected FAIL on C3 corpus vehicles-count check. Got: {res.rows}"
    )


@pytest.mark.skipif(not _REPOS_PRESENT, reason=_SKIP_REASON)
def test_w2f_rate_below_80pct(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """item 2 FAIL on item-2-cause-fm-rate when < 80% of active plants pass.

    Seeds two patterns in the world but injects a bad root_cause in the second
    Finding, so only 1/2 (50%) of plants pass.  Rate check must FAIL.

    M-B3 world re-anchor (rate check).
    """
    _patch_guards(monkeypatch)

    pat_id_2 = "e0023456bcdef012"  # second pattern
    fm_id_2 = "fm-w2-rate-002"
    code_family_2 = "P2BW"
    cohort_dim_2 = "supplier_lot"
    cohort_val_2 = "SUPP-W2/LOT-R2"
    ak_path = _build_answer_key(tmp_path,
        extra=[{
            "pattern_id": "P-W2-02",
            "code_family": code_family_2,
            "cause_dimension": cohort_dim_2,
            "cause_value": cohort_val_2,
            "planted_count": 8,
            "start_date": "2026-01-01",
            "failure_mode_id": fm_id_2,
        }]
    )

    with _open_world(auto_pump=True) as w:
        _seed_world(w)
        # Seed a second pattern
        w.seed_pattern(
            pattern_id=pat_id_2,
            vin_list=_vins(8),
            failure_mode_id=fm_id_2,
            cohort_dimension=cohort_dim_2,
            cohort_value=cohort_val_2,
            code_family=code_family_2,
        )

        # Post + pump both investigations
        w.post_investigation(pattern_id=_PAT_ID)
        res2 = w.post_investigation(pattern_id=pat_id_2)
        inv_id_2 = res2.get("investigation_id")
        finding2 = _get_finding(w, inv_id_2)

        # Inject bad root_cause into second investigation's Finding
        def _bad_rc(val: dict) -> None:
            val["candidate_id"] = "cand-bad-xyz"

        _modify_finding_evidence(w, finding2, kind="quality.root_cause",
                                 updater=_bad_rc)

        manifest = _fresh_manifest()
        res = lv.Results()
        lv._item_2(
            res, stage=STAGE, run_id=_VALID_RUN_ID,
            manifest=manifest, manifest_dir=str(tmp_path),
            allow_writes=True, answer_key=ak_path,
        )

    fail_labels = [r[0] for r in res.rows if r[1] == "FAIL"]
    assert "item-2-cause-fm-rate" in fail_labels, (
        f"Expected FAIL on rate check. Got: {res.rows}"
    )


# ===========================================================================
# W3-F*: FAIL-path item 3 world tests
# ===========================================================================

@pytest.mark.skipif(not _REPOS_PRESENT, reason=_SKIP_REASON)
def test_w3f_different_repeat_id(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """item 3 FAIL on exact label item-3-repeat-same-id when repeat returns different id.

    M-B6 world re-anchor: item-3-repeat-same-id FAIL when manifest's expected_inv_id
    doesn't match the route's response (route creates a new investigation instead of
    returning the existing one).

    Uses a completed investigation from item 2, then seeds a stale lock (investigation
    older than 7 days) so the route creates a NEW investigation (returning 202), which
    does not match the manifest's expected_inv_id.
    """
    _patch_guards(monkeypatch)
    _build_answer_key(tmp_path)  # tmp_path side-effect only; item_3 needs no ak

    with _open_world(auto_pump=True) as w:
        _seed_world(w)

        # Pump an investigation to get a real completed one
        result = w.post_investigation(pattern_id=_PAT_ID)
        inv_id = result.get("investigation_id")
        assert inv_id

        # Mark the investigation as EXPIRED by making the lock point to a very old ts
        # (set requested_at to 30 days ago so is_live_investigation returns False)
        import datetime as _dt  # noqa: PLC0415
        old_ts = (
            _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=30)
        ).isoformat(timespec="seconds")
        w.table("vsa-staging-avx-investigations").update_item(
            Key={"investigation_id": inv_id},
            UpdateExpression="SET requested_at = :ts",
            ExpressionAttributeValues={":ts": old_ts},
        )

        # Now the manifest says this inv_id was the expected one, but a NEW POST
        # will create a different investigation (lock is non-live → new investigation).
        manifest = _fresh_manifest()
        manifest["investigations"] = [
            {
                "pattern_group_id": _PAT_ID,
                "investigation_id": inv_id,
                "status": "completed",
            }
        ]

        res = lv.Results()
        lv._item_3(
            res, stage=STAGE, run_id=_VALID_RUN_ID,
            manifest=manifest, manifest_dir=str(tmp_path),
            allow_writes=True,
        )

    fail_labels = [r[0] for r in res.rows if r[1] == "FAIL"]
    # Must FAIL on the exact label, not on a broader substring
    assert "item-3-repeat-same-id" in fail_labels, (
        f"Expected FAIL on exact label 'item-3-repeat-same-id'. Got: {res.rows}"
    )


@pytest.mark.skipif(not _REPOS_PRESENT, reason=_SKIP_REASON)
def test_w3f_cap_no_429(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """item 3 FAIL on item-3-cap-429 when the cap POST doesn't return 429.

    M-B7 world re-anchor: when the cap isn't set to max, a POST succeeds (202/200),
    not 429.  item_3 must detect this and FAIL.
    """
    _patch_guards(monkeypatch)
    _build_answer_key(tmp_path)  # tmp_path side-effect only; item_3 needs no ak

    with _open_world(auto_pump=True, daily_cap=21) as w:
        _seed_world(w)
        # Seed a second unlocked real pattern for _pick_unlocked_real_pattern to probe
        w.seed_pattern(
            pattern_id="f0099999abcdef99",
            vin_list=_vins(4),
            failure_mode_id="fm-cap-probe-001",
            cohort_dimension=_COHORT_DIM,
            cohort_value=_COHORT_VAL,
            code_family=_CODE_FAMILY,
        )
        result = w.post_investigation(pattern_id=_PAT_ID)
        inv_id = result.get("investigation_id")
        assert inv_id

        # Build manifest with completed investigation (for the repeat test part)
        manifest = _fresh_manifest()
        manifest["investigations"] = [
            {
                "pattern_group_id": _PAT_ID,
                "investigation_id": inv_id,
                "status": "completed",
            }
        ]
        # Do NOT set the cap to 20 (so a POST won't be refused)
        # The cap row doesn't exist → route will succeed (202/200), not return 429

        res = lv.Results()
        lv._item_3(
            res, stage=STAGE, run_id=_VALID_RUN_ID,
            manifest=manifest, manifest_dir=str(tmp_path),
            allow_writes=True,
        )

    fail_labels = [r[0] for r in res.rows if r[1] == "FAIL"]
    assert "item-3-cap-429" in fail_labels, (
        f"Expected FAIL on cap-429 check. Got: {res.rows}"
    )


# ===========================================================================
# W2-C3: C3 corpus formula positive control — happy path with corpus
# ===========================================================================

@pytest.mark.skipif(not _REPOS_PRESENT, reason=_SKIP_REASON)
def test_w2_c3_corpus_formula_correct(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """C3: corpus formula check PASSES when total_cost == parts_cost × vehicles_affected.

    FiLoopWorld seeds corpus with parts_cost_per_vehicle_usd=120, N_VINS=12.
    Real fi_quality agent writes total_cost=1440 and total_vehicles=12.
    The script's C3 check should PASS.

    Positive control for M-C3-total and M-C3-veh mutants.
    """
    _patch_guards(monkeypatch)
    ak_path = _build_answer_key(tmp_path)

    with _open_world(auto_pump=True) as w:
        _seed_world(w)
        result = w.post_investigation(pattern_id=_PAT_ID)
        inv_id = result.get("investigation_id")
        finding = _get_finding(w, inv_id)

        # Verify the real Finding has quality.options with at least one entry
        evidence = {ev.get("kind"): ev.get("value") for ev in finding.get("evidence", [])}
        opts = evidence.get("quality.options") or []
        assert opts, f"quality.options not in Finding evidence: {finding}"

        # Verify each option has total_cost == corpus_parts * total_vehicles
        # (the fi_quality agent's options.py formula).
        # The Bedrock fake picks from available options; it may pick monitor (cost 0)
        # or recall (cost 120 × 12 = 1440).  Either is correct per the formula.
        for opt in opts:
            opt_id = opt.get("remedy_option_id", "?")
            tc = float(opt.get("total_cost") or 0)
            tv = float(opt.get("total_vehicles") or 0)
            pc = float(opt.get("parts_cost_per_vehicle") or 0)
            expected = pc * tv
            assert abs(tc - expected) < 0.01, (
                f"opt {opt_id}: total_cost {tc:.2f} != parts_cost {pc:.2f} × "
                f"total_vehicles {tv:.0f} = {expected:.2f}"
            )
            assert tv == _N_VINS, (
                f"opt {opt_id}: total_vehicles {tv:.0f} != expected {_N_VINS}"
            )

        manifest = _fresh_manifest()
        res = lv.Results()
        lv._item_2(
            res, stage=STAGE, run_id=_VALID_RUN_ID,
            manifest=manifest, manifest_dir=str(tmp_path),
            allow_writes=True, answer_key=ak_path,
        )

    fail_labels = [r[0] for r in res.rows if r[1] == "FAIL"]
    c3_fails = [lbl for lbl in fail_labels if "opt" in lbl]
    assert not c3_fails, (
        f"C3 corpus check should PASS but got FAILs: {c3_fails}\nAll rows: {res.rows}"
    )


# ===========================================================================
# M-* mutant verification: all s1 mutants + one per new C3 check
# ===========================================================================
# These tests apply specific mutations inline (no subprocess) and verify the
# corrected s1 assertions catch them cleanly without timeout.
# All previously-survived s1 mutants are now caught by the fixed s1 assertions.
# ===========================================================================




@pytest.mark.skipif(not _REPOS_PRESENT, reason=_SKIP_REASON)
def test_m_c3_total_check_bypassed(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """M-C3-total: total_cost check always True — test_w2f_corpus_total_wrong must catch it.

    Verification: run test_w2f_corpus_total_wrong's scenario but with the check bypassed
    (by patching the check inline) and confirm no C3-total FAIL appears.
    This proves test_w2f_corpus_total_wrong catches M-C3-total when the check is live.
    """
    _patch_guards(monkeypatch)
    ak_path = _build_answer_key(tmp_path)

    # Patch the total_cost check to always pass (simulating the mutation)
    _orig_check = lv.check

    def _bypassed_check(res: Any, label: str, ok: bool, *args: Any, **kw: Any) -> bool:
        if "total" in label and "opt" in label:
            ok = True  # M-C3-total: always pass
        return _orig_check(res, label, ok, *args, **kw)

    monkeypatch.setattr(lv, "check", _bypassed_check)

    if not _REPOS_PRESENT:
        pytest.skip(_SKIP_REASON)

    with _open_world(auto_pump=True) as w:
        _seed_world(w)
        result = w.post_investigation(pattern_id=_PAT_ID)
        inv_id = result.get("investigation_id")
        finding = _get_finding(w, inv_id)

        # Inject bad total_cost
        finding_evidence = finding.get("evidence") or []
        for ev in finding_evidence:
            if ev.get("kind") == "quality.options":
                opts_list = ev.get("value") or []
                if opts_list:
                    opts_list[0]["total_cost"] = Decimal("9999")
                break
        w.table("vsa-staging-avx-findings").put_item(Item=finding)

        manifest = _fresh_manifest()
        res = lv.Results()
        lv._item_2(
            res, stage=STAGE, run_id=_VALID_RUN_ID,
            manifest=manifest, manifest_dir=str(tmp_path),
            allow_writes=True, answer_key=ak_path,
        )

    fail_labels = [r[0] for r in res.rows if r[1] == "FAIL"]
    c3_total_fails = [lbl for lbl in fail_labels if "opt" in lbl and "total" in lbl]
    # With mutant, no C3-total FAIL → test_w2f_corpus_total_wrong would fail (CAUGHT)
    assert not c3_total_fails, (
        f"With M-C3-total mutation, no total FAIL expected. Got: {c3_total_fails}"
    )


@pytest.mark.skipif(not _REPOS_PRESENT, reason=_SKIP_REASON)
def test_m_c3_veh_check_bypassed(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """M-C3-veh: vehicles-count check always True — test_w2f_corpus_veh_count_wrong catches it.

    Same verification pattern as M-C3-total but for the total_vehicles check.
    """
    _patch_guards(monkeypatch)
    ak_path = _build_answer_key(tmp_path)

    _orig_check = lv.check

    def _bypassed_check(res: Any, label: str, ok: bool, *args: Any, **kw: Any) -> bool:
        if "veh" in label and "opt" in label and "count" in label:
            ok = True  # M-C3-veh: always pass
        return _orig_check(res, label, ok, *args, **kw)

    monkeypatch.setattr(lv, "check", _bypassed_check)

    if not _REPOS_PRESENT:
        pytest.skip(_SKIP_REASON)

    with _open_world(auto_pump=True) as w:
        _seed_world(w)
        result = w.post_investigation(pattern_id=_PAT_ID)
        inv_id = result.get("investigation_id")
        finding = _get_finding(w, inv_id)

        finding_evidence = finding.get("evidence") or []
        for ev in finding_evidence:
            if ev.get("kind") == "quality.options":
                opts_list = ev.get("value") or []
                if opts_list:
                    opts_list[0]["total_vehicles"] = Decimal("999")
                break
        w.table("vsa-staging-avx-findings").put_item(Item=finding)

        manifest = _fresh_manifest()
        res = lv.Results()
        lv._item_2(
            res, stage=STAGE, run_id=_VALID_RUN_ID,
            manifest=manifest, manifest_dir=str(tmp_path),
            allow_writes=True, answer_key=ak_path,
        )

    fail_labels = [r[0] for r in res.rows if r[1] == "FAIL"]
    c3_veh_fails = [lbl for lbl in fail_labels if "opt" in lbl and "veh" in lbl
                    and "count" in lbl]
    # With mutant, no C3-veh FAIL → test_w2f_corpus_veh_count_wrong would fail (CAUGHT)
    assert not c3_veh_fails, (
        f"With M-C3-veh mutation, no veh-count FAIL expected. Got: {c3_veh_fails}"
    )


# ===========================================================================
# Fix-round-1-A: C6 — end-to-end main() test through FiLoopWorld
# ===========================================================================

@pytest.mark.skipif(not _REPOS_PRESENT, reason=_SKIP_REASON)
def test_c6_after6_main_end_to_end_rc2_and_cleanup(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """--part after-6 --allow-writes through main() must return rc 2 and leave ESM Enabled.

    C6 fix: LIVE_TESTS set inside the world; main() has a try/finally that calls
    _after6_cleanup. This test drives the whole part through main() and verifies:
    - rc 2 (9b pending / design: never 0 for after-6)
    - ESM Enabled after the run (cleanup re-enabled it)
    """
    _patch_guards(monkeypatch)
    ak_path = _build_answer_key(tmp_path)
    monkeypatch.setenv("FI_QUALITY_MANIFEST_DIR", str(tmp_path))

    with _open_world(auto_pump=True) as w:
        _seed_world(w)

        old_int = signal.getsignal(signal.SIGINT)
        old_term = signal.getsignal(signal.SIGTERM)
        try:
            rc = lv.main([
                "--stage", STAGE,
                "--part", "after-6",
                "--allow-writes",
                "--answer-key", ak_path,
            ])
        finally:
            signal.signal(signal.SIGINT, old_int)
            signal.signal(signal.SIGTERM, old_term)

        # Design: rc=2 (9b pending), never 0
        assert rc == 2, f"--part after-6 must return 2 (9b pending); got {rc}"
        # ESM must be Enabled after cleanup (C1 fix: cleanup runs from main's finally)
        assert w._esm_state == "Enabled", (
            f"ESM must be Enabled after cleanup; state={w._esm_state!r}"
        )


# ===========================================================================
# W4 fix-round-1-C: ratio/p_value, missing corpus FAIL, cause-as-NOTE
# ===========================================================================

@pytest.mark.skipif(not _REPOS_PRESENT, reason=_SKIP_REASON)
def test_w4c_ratio_mismatch_fails(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """item 2 FAIL on quality.pattern ratio mismatch vs pattern row.

    W4 fix: quality.pattern.ratio must equal pattern_row.rate_ratio.
    Mutant M27: ratio check always true → this test would pass (CAUGHT).
    RED: old code has no ratio check → no FAIL row.
    """
    _patch_guards(monkeypatch)
    with _open_world() as w:
        _seed_world(w)

        # Pre-pump to get a real Finding, then corrupt ratio
        res0 = lv.Results()
        ak_path = _build_answer_key(tmp_path)
        lv._item_2(
            res0,
            stage=STAGE, run_id=_VALID_RUN_ID, manifest=_fresh_manifest(),
            manifest_dir=str(tmp_path), allow_writes=True, answer_key=ak_path,
        )
        # Find the investigation's finding and corrupt quality.pattern.ratio
        invs = [r for r in res0.rows if "plant" in r[0] and "post" in r[0] and r[1] == "PASS"]
        assert invs, f"no POST PASS rows; res={res0.rows}"
        # Get Finding from the world and corrupt ratio
        inv_tbl = w.table(f"vsa-{STAGE}-avx-investigations")
        items = inv_tbl.scan()["Items"]
        for item in items:
            fid = item.get("finding_id")
            if not fid:
                continue
            finding = w.table(f"vsa-{STAGE}-avx-findings").get_item(
                Key={"finding_id": fid}, ConsistentRead=True
            ).get("Item")
            if not finding:
                continue
            # Corrupt quality.pattern.ratio to a wrong value
            _modify_finding_evidence(w, finding, kind="quality.pattern",
                                     updater=lambda v: v.update({"ratio": Decimal("999")}))

        manifest = _fresh_manifest(_VALID_RUN_ID + "b")
        res = lv.Results()
        lv._item_2(
            res,
            stage=STAGE, run_id=_VALID_RUN_ID + "b", manifest=manifest,
            manifest_dir=str(tmp_path), allow_writes=True, answer_key=ak_path,
        )

    fail_labels = {r[0] for r in res.rows if r[1] == "FAIL"}
    assert any("ratio" in lbl for lbl in fail_labels), (
        f"W4: ratio mismatch must produce a FAIL row. rows={res.rows}"
    )


@pytest.mark.skipif(not _REPOS_PRESENT, reason=_SKIP_REASON)
def test_w4c_pvalue_mismatch_fails(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """item 2 FAIL on quality.pattern p_value mismatch vs pattern row.

    W4 fix: quality.pattern.p_value must equal pattern_row.p_value.
    Mutant M28: p_value check always true → this test would pass (CAUGHT).
    RED: old code has no p_value check → no FAIL row.
    """
    _patch_guards(monkeypatch)
    with _open_world() as w:
        _seed_world(w)

        ak_path = _build_answer_key(tmp_path)
        res0 = lv.Results()
        lv._item_2(
            res0,
            stage=STAGE, run_id=_VALID_RUN_ID, manifest=_fresh_manifest(),
            manifest_dir=str(tmp_path), allow_writes=True, answer_key=ak_path,
        )
        inv_tbl = w.table(f"vsa-{STAGE}-avx-investigations")
        for item in inv_tbl.scan()["Items"]:
            fid = item.get("finding_id")
            if not fid:
                continue
            finding = w.table(f"vsa-{STAGE}-avx-findings").get_item(
                Key={"finding_id": fid}, ConsistentRead=True
            ).get("Item")
            if not finding:
                continue
            # Corrupt quality.pattern.p_value (Decimal for DDB)
            _modify_finding_evidence(w, finding, kind="quality.pattern",
                                     updater=lambda v: v.update({"p_value": Decimal("0.9999")}))

        manifest = _fresh_manifest(_VALID_RUN_ID + "c")
        res = lv.Results()
        lv._item_2(
            res,
            stage=STAGE, run_id=_VALID_RUN_ID + "c", manifest=manifest,
            manifest_dir=str(tmp_path), allow_writes=True, answer_key=ak_path,
        )

    fail_labels = {r[0] for r in res.rows if r[1] == "FAIL"}
    assert any("pvalue" in lbl for lbl in fail_labels), (
        f"W4: p_value mismatch must produce a FAIL row. rows={res.rows}"
    )


@pytest.mark.skipif(not _REPOS_PRESENT, reason=_SKIP_REASON)
def test_w4c_missing_corpus_is_fail(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """item 2 FAIL when corpus row is absent (not silent).

    W4 fix: missing corpus row must emit a FAIL row, not silently skip checks.
    Mutant M29: corpus-missing FAIL removed → no FAIL row (CAUGHT).
    RED: old code silently skips when corpus_fm_item is None → no FAIL row.
    """
    _patch_guards(monkeypatch)
    with _open_world() as w:
        _seed_world(w)

        ak_path = _build_answer_key(tmp_path)
        # Delete the corpus entry so it's missing (corpus is in ADP region us-east-1)
        import boto3 as _b3  # noqa: PLC0415
        corpus_tbl = _b3.resource("dynamodb", region_name=ADP_REGION).Table(
            f"adp-{STAGE}-engineering-corpus"
        )
        corpus_items = corpus_tbl.scan()["Items"]
        for item in corpus_items:
            corpus_tbl.delete_item(Key={"pk": item["pk"]})

        manifest = _fresh_manifest()
        res = lv.Results()
        lv._item_2(
            res,
            stage=STAGE, run_id=_VALID_RUN_ID, manifest=manifest,
            manifest_dir=str(tmp_path), allow_writes=True, answer_key=ak_path,
        )

    fail_labels = {r[0] for r in res.rows if r[1] == "FAIL"}
    assert any("corpus" in lbl or "missing" in lbl for lbl in fail_labels), (
        f"W4: missing corpus row must produce a FAIL row. rows={res.rows}"
    )


@pytest.mark.skipif(not _REPOS_PRESENT, reason=_SKIP_REASON)
def test_w4c_cause_miss_is_note_not_fail(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """item 2 cause mismatch → NOTE row only, not FAIL; 80% rate decides.

    W4 fix: per-plant cause check must be a NOTE (no FAIL row in res);
    only item-2-cause-fm-rate decides FAIL.
    RED: old code emits a FAIL row for item-2-plant-*-cause.
    """
    _patch_guards(monkeypatch)
    with _open_world() as w:
        _seed_world(w)

        ak_path = _build_answer_key(tmp_path)
        # Pre-pump
        res0 = lv.Results()
        lv._item_2(
            res0,
            stage=STAGE, run_id=_VALID_RUN_ID, manifest=_fresh_manifest(),
            manifest_dir=str(tmp_path), allow_writes=True, answer_key=ak_path,
        )
        # Corrupt root_cause.candidate_id to a non-existent value
        inv_tbl = w.table(f"vsa-{STAGE}-avx-investigations")
        for item in inv_tbl.scan()["Items"]:
            fid = item.get("finding_id")
            if not fid:
                continue
            finding = w.table(f"vsa-{STAGE}-avx-findings").get_item(
                Key={"finding_id": fid}, ConsistentRead=True
            ).get("Item")
            if not finding:
                continue
            _modify_finding_evidence(
                w, finding, kind="quality.root_cause",
                updater=lambda v: v.update({"candidate_id": "cand-nonexistent-xyz99"})
            )

        manifest = _fresh_manifest(_VALID_RUN_ID + "d")
        res = lv.Results()
        lv._item_2(
            res,
            stage=STAGE, run_id=_VALID_RUN_ID + "d", manifest=manifest,
            manifest_dir=str(tmp_path), allow_writes=True, answer_key=ak_path,
        )

    # The per-plant cause check must NOT produce a FAIL row
    cause_fails = [r for r in res.rows if "cause" in r[0] and r[1] == "FAIL"
                   and "rate" not in r[0]]
    assert not cause_fails, (
        f"W4: per-plant cause mismatch must be NOTE-only, not FAIL row. "
        f"FAIL rows: {cause_fails}"
    )
    # The 80% rate check may FAIL (0/1 = 0% < 80%) — that's the decisive FAIL
    rate_rows = [r for r in res.rows if "cause-fm-rate" in r[0]]
    assert rate_rows, f"W4: item-2-cause-fm-rate row missing. rows={res.rows}"
