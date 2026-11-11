"""End-to-end tests through main() for the after-6 / cleanup-only flow.
C1 fix (review cycle 2): LIVE_TESTS=1 set INSIDE the world context; world.open() pops it.
Item 9 split to task 6.5 (2026-10-28): --part after-6 runs items 2/3/4/5/8b only (rc 0
when all pass); --part after-6-check refuses with rc=1 before any AWS call.

Four tests on FiLoopWorld:

1. test_e2e_after6_pass_rows_and_residue
   --part after-6 --allow-writes: rc 0; PASS rows for items 2/3/4/5/8b by name;
   no SKIP rows for those items; ESM Enabled at the end; items 2/3 investigations cleaned.

2. test_e2e_after6_check_full_cleanup
   --part after-6 rc=0 (all five items pass); --part after-6-check refuses rc=1
   (item 9 is task 6.5, split 2026-10-28).

3. test_e2e_crash_then_cleanup_only
   Raise injected in item 4 after its first write → rc 1; then
   --cleanup-only --run-id <id> --allow-writes → rc 0; residue empty; ESM Enabled.

4. test_e2e_item3_cap_refused_near_utc_midnight
   item 3 SKIP when write window closed near midnight.

Run:
    env -u AWS_PROFILE -u AWS_SESSION_TOKEN \\
        AWS_ACCESS_KEY_ID=x AWS_SECRET_ACCESS_KEY=x AWS_DEFAULT_REGION=us-west-2 \\
        ~/guidance-for-connected-vehicle-experience-on-aws/.venv/bin/python \\
        -m pytest scripts/tests/test_live_verify_fi_quality_after6_e2e.py -v

Requires:
  ~/automotive-data-platform-on-aws at ADP staging state (or FI_LOOP_WORLD_ADP_REPO)
  ~/connected-mobility-guidance-on-aws at CMS main (or FI_LOOP_WORLD_CMS_REPO)
"""
from __future__ import annotations

import contextlib
import datetime as dt
import importlib.util
import io
import json
import os
import re
import signal
import socket
import sys
from pathlib import Path
from typing import Any, Iterator

import pytest

# ---------------------------------------------------------------------------
# Load the fi-quality script module
# ---------------------------------------------------------------------------

_HERE = Path(__file__).resolve().parent
_SCRIPT = _HERE.parent / "live-verify-fi-quality.py"
_spec_lv = importlib.util.spec_from_file_location("live_verify_fi_quality_e2e", _SCRIPT)
assert _spec_lv is not None
lv = importlib.util.module_from_spec(_spec_lv)  # type: ignore[arg-type]
_spec_lv.loader.exec_module(lv)  # type: ignore[union-attr]

STAGE = "staging"

# ---------------------------------------------------------------------------
# Repo presence guard (FiLoopWorld requires both repos)
# ---------------------------------------------------------------------------

_ADP_REPO_ENV = "FI_LOOP_WORLD_ADP_REPO"
_CMS_REPO_ENV = "FI_LOOP_WORLD_CMS_REPO"
_DEFAULT_ADP = Path.home() / "automotive-data-platform-on-aws"
_DEFAULT_CMS = Path.home() / "connected-mobility-guidance-on-aws"

_ADP_PRESENT = (
    Path(os.environ.get(_ADP_REPO_ENV, str(_DEFAULT_ADP))) / "platform-foundation"
).exists()
_CMS_PRESENT = (
    Path(os.environ.get(_CMS_REPO_ENV, str(_DEFAULT_CMS))) / "services"
).exists()

_REPOS_PRESENT = _ADP_PRESENT and _CMS_PRESENT
_SKIP_REASON = (
    "FiLoopWorld requires ~/automotive-data-platform-on-aws and "
    "~/connected-mobility-guidance-on-aws (or set FI_LOOP_WORLD_ADP_REPO / "
    "FI_LOOP_WORLD_CMS_REPO)"
)

if _REPOS_PRESENT:
    from scripts.tests.fi_loop_world.world import (
        FiLoopWorld,
        INVESTIGATIONS_DLQ,
        INVESTIGATIONS_QUEUE,
        INVESTIGATIONS_TABLE,
    )

# ---------------------------------------------------------------------------
# Borrow seed constants and helpers from s2a
# ---------------------------------------------------------------------------

from scripts.tests.test_live_verify_fi_quality_after6_s2a import (  # noqa: E402
    _AK_PID,
    _COHORT_DIM,
    _COHORT_VAL,
    _CODE_FAMILY,
    _FM_ID,
    _N_VINS,
    _PAT_ID,
    _build_answer_key,
    _vins,
)

# ---------------------------------------------------------------------------
# Autouse fixtures
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


def _patch_guards(monkeypatch: pytest.MonkeyPatch) -> None:
    """Patch write-window guard, pre-checks, and clock on lv.

    LIVE_TESTS must be set inside the world context after world.open() because
    world.open() calls os.environ.pop('LIVE_TESTS', None).
    """
    monkeypatch.setattr(lv, "_after6_write_window_guard", lambda *_a, **_k: None)
    monkeypatch.setattr(lv, "_after6_pre_checks", lambda *_a, **_k: None)
    # No ``_utc_now`` pin: inside the world the script reads the world clock, as the
    # route does.  A pin to another date put item 3b's cap on a different day's
    # counter than the route reads, so the full cap never held (fix round 2).


def _open_world() -> Any:
    """Return an unopened FiLoopWorld. Use as `with _open_world() as w:`."""
    return FiLoopWorld(auto_pump=True)


_UNLOCKED_PAT_ID = "e2eunlockedpattern0001"


def _seed_world(w: Any) -> None:
    """Seed one fault pattern + corpus failure mode into the world.

    Also seeds FM-BCP-01 / FM-COMM-01 into the engineering-corpus table so
    item 4's corpus-real-fm-found check finds them (item 4 hardcodes these IDs).
    """
    w.seed_pattern(
        pattern_id=_PAT_ID,
        vin_list=_vins(_N_VINS),
        failure_mode_id=_FM_ID,
        cohort_dimension=_COHORT_DIM,
        cohort_value=_COHORT_VAL,
        code_family=_CODE_FAMILY,
    )
    # A second real pattern no run investigates: item 3b's cap probe needs a real
    # pattern with no lock (the route checks the lock before the cap).
    w.seed_pattern(
        pattern_id=_UNLOCKED_PAT_ID,
        vin_list=_vins(_N_VINS),
        failure_mode_id=_FM_ID,
        cohort_dimension=_COHORT_DIM,
        cohort_value=f"{_COHORT_VAL}-unplanted",
        code_family=_CODE_FAMILY,
    )
    _seed_corpus_for_item4()


def _run_main(argv: list[str]) -> tuple[int, str]:
    """Call lv.main(argv) with stdout captured. Returns (rc, stdout_text)."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = lv.main(argv)
    return rc, buf.getvalue()


def _parse_run_id(text: str) -> str:
    """Extract run_id from main() stdout (line: 'run_id: <id>  stage: ...')."""
    m = re.search(r"run_id:\s+(fi-quality-\S+)", text)
    assert m, f"run_id not found in output:\n{text[:600]}"
    return m.group(1)


def _read_manifest(manifest_dir: Path, run_id: str) -> dict:
    manifest_path = manifest_dir / run_id / "manifest.json"
    assert manifest_path.exists(), f"manifest not found: {manifest_path}"
    return json.loads(manifest_path.read_bytes())


# ---------------------------------------------------------------------------
# Test 1: --part after-6 → rc 2, all items PASS, ESM Enabled, item 9 still present
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _REPOS_PRESENT, reason=_SKIP_REASON)
def test_e2e_after6_pass_rows_and_residue(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """--part after-6 --allow-writes: rc 0; PASS rows for items 2/3/4/5/8b;
    no SKIP rows for those items; ESM Enabled; items 2/3 investigations cleaned.

    Item 9 split to task 6.5 (2026-10-28): item 9 is not in _AFTER6_ITEMS;
    rc must be 0 (not 2) when all five items pass with no FAILs and no SKIPs.

    C1 check: LIVE_TESTS set inside the world (after world.open() pops it).
    Pins N17 (6b off), N19 (attribute_exists condition), N16 (immediate readback),
    N23 (cap slot residue).
    """
    _patch_guards(monkeypatch)
    ak_path = _build_answer_key(tmp_path)
    manifest_dir = tmp_path / "manifests"
    manifest_dir.mkdir()

    with _open_world() as w:
        _seed_world(w)

        # Set LIVE_TESTS INSIDE the world context: world.open() pops it.
        os.environ["LIVE_TESTS"] = "1"
        os.environ["FI_QUALITY_MANIFEST_DIR"] = str(manifest_dir)

        rc, text = _run_main([
            "--stage", STAGE,
            "--part", "after-6",
            "--allow-writes",
            "--answer-key", ak_path,
        ])

        # ── rc 0: items 2/3/4/5/8b all PASS, no item 9 ──────────────────────
        assert rc == 0, (
            f"expected rc=0 (items 2/3/4/5/8b all PASS; item 9 is task 6.5), got rc={rc}.\n"
            f"output:\n{text}"
        )

        # ── PASS rows for each item; no SKIP rows for any of them ─────────────
        for item_prefix in ("item-2", "item-3", "item-4", "item-5", "item-8b"):
            pass_rows = [
                line
                for line in text.splitlines()
                if re.search(rf"\[PASS\].*{re.escape(item_prefix)}", line)
            ]
            skip_rows = [
                line
                for line in text.splitlines()
                if re.search(rf"\[SKIP\].*{re.escape(item_prefix)}", line)
            ]
            assert pass_rows, (
                f"no [PASS] rows for {item_prefix!r} in output.\n"
                f"output:\n{text}"
            )
            assert not skip_rows, (
                f"unexpected [SKIP] rows for {item_prefix!r}: {skip_rows}.\n"
                f"output:\n{text}"
            )

        assert any(re.search(r"\[PASS\].*after6-residue-scan", ln)
                   for ln in text.splitlines()), (
            f"no [PASS] after6-residue-scan row after --part after-6.\noutput:\n{text}")

        assert re.search(r"\[PASS\].*item-3-cap-429", text), (
            f"item 3b must get the cap's 429.\noutput:\n{text}")
        assert w.table(INVESTIGATIONS_TABLE).get_item(
            Key={"investigation_id": f"lock#{_UNLOCKED_PAT_ID}"}, ConsistentRead=True
        ).get("Item") is None, "item 3b's 429 POST must leave no lock on the probe pattern"

        # ── ESM must be Enabled after cleanup ─────────────────────────────────
        assert w._esm_state == "Enabled", (
            f"ESM must be Enabled after --part after-6 cleanup; "
            f"state={w._esm_state!r}"
        )

        # ── Items 2/3: investigation rows must be cleaned up ──────────────────
        # (item 9 split to task 6.5 — no item 9 residue to check here)
        run_id = _parse_run_id(text)
        manifest = _read_manifest(manifest_dir, run_id)
        inv_tbl = w.table(INVESTIGATIONS_TABLE)
        for rec in manifest.get("investigations", []):
            inv_id = rec.get("investigation_id")
            if not inv_id:
                continue
            leftover = inv_tbl.get_item(
                Key={"investigation_id": inv_id}, ConsistentRead=True
            ).get("Item")
            assert leftover is None, (
                f"investigation {inv_id!r} (item 2/3) should be cleaned up "
                f"after --part after-6"
            )


# ---------------------------------------------------------------------------
# Test 2: --part after-6 rc=0 (all five items pass); --part after-6-check refuses
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _REPOS_PRESENT, reason=_SKIP_REASON)
def test_e2e_after6_check_full_cleanup(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """--part after-6 exits 0 when all five items (2/3/4/5/8b) pass;
    --part after-6-check refuses with rc=1 (item 9 is task 6.5, split 2026-10-28).

    After split: after-6 no longer runs item 9, so rc=0 (not rc=2) when all
    items pass.  after-6-check is 6.5\'s entry point and refuses immediately.
    Pins N17 (6b off), split-2026-10-28 refusal.
    """
    _patch_guards(monkeypatch)
    ak_path = _build_answer_key(tmp_path)
    manifest_dir = tmp_path / "manifests"
    manifest_dir.mkdir()

    with _open_world() as w:
        _seed_world(w)

        # Set LIVE_TESTS INSIDE the world context.
        os.environ["LIVE_TESTS"] = "1"
        os.environ["FI_QUALITY_MANIFEST_DIR"] = str(manifest_dir)

        # ── Step A: run --part after-6 ────────────────────────────────────────
        rc_a6, text_a6 = _run_main([
            "--stage", STAGE,
            "--part", "after-6",
            "--allow-writes",
            "--answer-key", ak_path,
        ])

        # rc must be 0: all items (2/3/4/5/8b) passed; item 9 is task 6.5.
        assert rc_a6 == 0, (
            f"--part after-6 expected rc=0 (items 2/3/4/5/8b all PASS; "
            f"item 9 is task 6.5), got rc={rc_a6}.\n"
            f"output:\n{text_a6}"
        )

        run_id = _parse_run_id(text_a6)

        # ── Step B: run --part after-6-check → must refuse with rc=1 ─────────
        rc_check, text_check = _run_main([
            "--stage", STAGE,
            "--part", "after-6-check",
        ])

        assert rc_check == 1, (
            f"--part after-6-check expected rc=1 (task 6.5 refusal), "
            f"got rc={rc_check}.\n"
            f"output:\n{text_check}"
        )
        assert "6.5" in text_check, (
            f"--part after-6-check must mention '6.5' in its refusal; "
            f"got: {text_check!r}"
        )


# ---------------------------------------------------------------------------
# Test 3: crash in item 4 after first write → rc 1; cleanup-only → rc 0
# ---------------------------------------------------------------------------


def _seed_corpus_for_item4(region: str = "us-east-1", stage: str = "staging") -> None:
    """Seed FM-BCP-01 and FM-COMM-01 into the ADP engineering-corpus DDB table.

    Item 4 hardcodes these failure-mode IDs when reading the corpus. They must
    exist in the world's DDB for item 4 to pass the corpus-read check and
    proceed to its first DDB write. World extension: seeds test data needed by
    item 4's corpus-real-fm-found check (without this, item 4 returns early and
    the crash injection never fires).
    """
    import boto3
    from decimal import Decimal

    corpus_tbl = boto3.resource("dynamodb", region_name=region).Table(
        f"adp-{stage}-engineering-corpus"
    )
    corpus_tbl.put_item(Item={
        "pk": "fm#FM-BCP-01",
        "failure_mode_id": "FM-BCP-01",
        "component_name": "Brake Control Processor",
        "do_not_drive": False,
        "park_outside": False,
        "symptom_codes": ["P0BC0", "P0BC1"],
        "options": [
            {
                "remedy_option_id": "OPT-FM-BCP-01-A",
                "remedy_type": "hardware_recall",
                "parts_cost_per_vehicle_usd": Decimal("120"),
                "labor_hours_per_vehicle": Decimal("2"),
                "lead_time_weeks": Decimal("8"),
                "over_the_air": False,
                "recommended": True,
                "component_lots": "SUP-BCP/LOT-001",
            },
            {
                "remedy_option_id": "OPT-FM-BCP-01-B",
                "remedy_type": "software_update",
                "parts_cost_per_vehicle_usd": Decimal("0"),
                "labor_hours_per_vehicle": Decimal("0"),
                "lead_time_weeks": Decimal("2"),
                "over_the_air": True,
                "recommended": False,
                "component_lots": "SUP-BCP/LOT-001",
            },
            {
                "remedy_option_id": "OPT-FM-BCP-01-C",
                "remedy_type": "monitor",
                "parts_cost_per_vehicle_usd": Decimal("0"),
                "labor_hours_per_vehicle": Decimal("0"),
                "lead_time_weeks": Decimal("1"),
                "over_the_air": True,
                "recommended": False,
                "component_lots": "SUP-BCP/LOT-001",
            },
        ],
    })
    corpus_tbl.put_item(Item={
        "pk": "fm#FM-COMM-01",
        "failure_mode_id": "FM-COMM-01",
        "component_name": "Communication Module",
        "do_not_drive": False,
        "park_outside": False,
        "symptom_codes": ["P0CM0"],
        "options": [
            {
                "remedy_option_id": "OPT-FM-COMM-01-A",
                "remedy_type": "hardware_recall",
                "parts_cost_per_vehicle_usd": Decimal("80"),
                "labor_hours_per_vehicle": Decimal("1"),
                "lead_time_weeks": Decimal("6"),
                "over_the_air": False,
                "recommended": True,
                "component_lots": "SUP-COMM/LOT-001",
            },
        ],
    })


@pytest.mark.skipif(not _REPOS_PRESENT, reason=_SKIP_REASON)
def test_e2e_crash_then_cleanup_only(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Raise injected in item 4 after its second manifest write → rc 1; then
    --cleanup-only --run-id <id> --allow-writes → rc 0; residue empty; ESM Enabled.

    Item 9 split to task 6.5 (2026-10-28): item 9 never ran, so cleanup-only
    has nothing item-9-specific to undo and exits rc 0.
    """
    _patch_guards(monkeypatch)
    ak_path = _build_answer_key(tmp_path)
    manifest_dir = tmp_path / "manifests"
    manifest_dir.mkdir()

    # Inject a crash into item 4 AFTER its first write landed (architect, fix round 2):
    # the first PutItem of a LIVETEST-4 row into the ADP patterns table commits, then the
    # client call raises, as a timeout after the server applied the put would.
    import botocore.client as _bc

    landed: list[str] = []
    _orig_api = _bc.BaseClient._make_api_call

    def _api(self: Any, op: str, params: dict) -> Any:
        out = _orig_api(self, op, params)
        pid = (params.get("Item") or {}).get("pattern_id", "")
        if isinstance(pid, dict):  # low-level client shape {"S": ...}
            pid = pid.get("S", "")
        if (op == "PutItem" and not landed
                and params.get("TableName") == f"adp-{STAGE}-fault-patterns"
                and str(pid).startswith("LIVETEST-4-")):
            landed.append(str(pid))
            raise RuntimeError("injected crash after item 4's first pattern row landed")
        return out

    monkeypatch.setattr(_bc.BaseClient, "_make_api_call", _api)

    with _open_world() as w:
        _seed_world(w)
        # FM-BCP-01/FM-COMM-01 are now seeded by _seed_world via _seed_corpus_for_item4.

        os.environ["LIVE_TESTS"] = "1"
        os.environ["FI_QUALITY_MANIFEST_DIR"] = str(manifest_dir)

        # ── Step A: --part after-6 with crash in item 4 → rc 1 ───────────────
        rc_crash, text_crash = _run_main([
            "--stage", STAGE,
            "--part", "after-6",
            "--allow-writes",
            "--answer-key", ak_path,
        ])

        assert rc_crash == 1, (
            f"expected rc=1 (item-4 crash → FAIL row), got rc={rc_crash}.\n"
            f"output:\n{text_crash}"
        )

        # item-4-exception FAIL row must be present.
        fail_rows = [
            line
            for line in text_crash.splitlines()
            if re.search(r"\[FAIL\].*item-4-exception", line)
        ]
        assert fail_rows, (
            f"no [FAIL] item-4-exception row in output.\n"
            f"output:\n{text_crash}"
        )

        assert len(landed) == 1, f"the injected crash never fired (landed={landed})"
        run_id = _parse_run_id(text_crash)
        manifest = _read_manifest(manifest_dir, run_id)

        # At least the first item4 pattern must be recorded in the manifest
        # (recorded before crash, at save #2 — item4_patterns[0] appended).
        item4_patterns = manifest.get("item4_patterns", [])
        assert item4_patterns, (
            f"manifest should have at least item4_patterns[0] recorded before crash"
        )
        assert landed[0] in {p.get("pattern_id") for p in item4_patterns}, (
            f"the landed row {landed[0]!r} must be in the manifest before its put; "
            f"manifest patterns={item4_patterns!r}"
        )
        import boto3 as _b3

        _pt = _b3.resource("dynamodb", region_name="us-east-1").Table(
            f"adp-{STAGE}-fault-patterns")
        assert _pt.get_item(Key={"pattern_id": landed[0]}, ConsistentRead=True
                            ).get("Item") is None, (
            f"main()'s finally must delete the landed row {landed[0]!r}.\n"
            f"output:\n{text_crash}")

        # ── Step B: --cleanup-only → ESM Enabled, residue empty, rc=0 ────────
        # Item 9 split to task 6.5 (2026-10-28): item 9 never ran during --part after-6,
        # so cleanup-only has nothing item-9-specific to undo; rc must be 0.
        rc_cleanup, text_cleanup = _run_main([
            "--stage", STAGE,
            "--cleanup-only",
            "--run-id", run_id,
            "--allow-writes",
        ])

        assert rc_cleanup == 0, (
            f"--cleanup-only expected rc=0 (item 9 not run; split to task 6.5), "
            f"got rc={rc_cleanup}.\n"
            f"output:\n{text_cleanup}"
        )

        # ESM must be Enabled after cleanup-only.
        assert w._esm_state == "Enabled", (
            f"ESM must be Enabled after --cleanup-only; state={w._esm_state!r}"
        )

        # Residue scan in cleanup-only output must PASS.
        residue_pass = any(
            re.search(r"\[PASS\].*after6-residue-scan", line)
            for line in text_cleanup.splitlines()
        )
        assert residue_pass, (
            f"no [PASS] after6-residue-scan row after --cleanup-only.\n"
            f"output:\n{text_cleanup}"
        )

        # Verify item4_patterns are not in ADP DDB (crash at save #2 means
        # pat_tbl.put_item was never called; cleanup-only confirms idempotency).
        import boto3

        ddb_adp = boto3.resource("dynamodb", region_name="us-east-1")
        pat_tbl = ddb_adp.Table(f"adp-{STAGE}-fault-patterns")
        for pat_rec in item4_patterns:
            pid = pat_rec.get("pattern_id", "")
            if not pid:
                continue
            leftover = pat_tbl.get_item(
                Key={"pattern_id": pid}, ConsistentRead=True
            ).get("Item")
            assert leftover is None, (
                f"item4 pattern {pid!r} must be gone after --cleanup-only"
            )


def test_e2e_item3_cap_refused_near_utc_midnight(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Architect, fix round 2: item 3b never moves the cap within 10 minutes of 00:00Z.

    The route keys the counter on its own UTC date, so near midnight the script's date and the
    route's can differ and a full cap on the wrong row lets a real investigation through.
    """
    _patch_guards(monkeypatch)
    ak_path = _build_answer_key(tmp_path)
    manifest_dir = tmp_path / "manifests"
    manifest_dir.mkdir()

    with _open_world() as w:
        _seed_world(w)
        now = w.clock.now()
        w.clock.set(now.replace(hour=23, minute=55, second=0, microsecond=0))
        os.environ["LIVE_TESTS"] = "1"
        os.environ["FI_QUALITY_MANIFEST_DIR"] = str(manifest_dir)
        rc, text = _run_main([
            "--stage", STAGE, "--part", "after-6", "--allow-writes", "--answer-key", ak_path,
        ])
        caps = {
            i["investigation_id"]: int(i.get("count", 0))
            for i in w.table(INVESTIGATIONS_TABLE).scan(ConsistentRead=True).get("Items", [])
            if str(i.get("investigation_id", "")).startswith("cap#")
        }
        lock = w.table(INVESTIGATIONS_TABLE).get_item(
            Key={"investigation_id": f"lock#{_UNLOCKED_PAT_ID}"}, ConsistentRead=True
        ).get("Item")

    assert re.search(r"\[SKIP\].*item-3-cap.*00:00Z", text), (
        f"item 3b must SKIP near 00:00Z.\noutput:\n{text}")
    assert not re.search(r"item-3-cap-(set|429|probe-pattern)", text), (
        f"item 3b must not pick a probe, set the cap or POST near 00:00Z.\noutput:\n{text}")
    assert lock is None, "no investigation may be created for the probe pattern near midnight"
    assert all(v < 20 for v in caps.values()), f"no cap row may be set full; caps={caps}"
    assert rc != 0, f"after-6 never exits 0; rc={rc}"
