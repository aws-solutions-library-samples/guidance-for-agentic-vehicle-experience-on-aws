"""Stage 2b world tests — items 4 and 5 via FiLoopWorld.

Adds item 4 (record_engineering_decision for 4 LIVETEST- patterns) and
item 5 (issue_software_campaign on the resulting quality.issue Finding).

All tests are RED with the stage-2 placeholders and GREEN with the real
_item_4 / _item_5 implementation.

Checks per design §Item 4:
  C8   monitor with no thread record, or a campaign id set
  W8   software_update but thread has recall_campaign_id set
  M8   unknown option not recorded as declined
  M9   action.status != 'executed' for a valid approval
  VIN  sha256 of VIN object != pattern row's sha256

Checks per design §Item 5:
  M10  thread version status != 'issued' after issue_software_campaign
  S4   issued_row.software_campaign_id != draft_id

Cleanup tests:
  - raise after first write → cleanup undoes everything
  - SIGTERM after first write → cleanup undoes everything
  - delete that answers success but leaves the row → caught by residue alone

Mutant verification:
  - one per check
  - one per cleanup step (step 2 LIVETEST- patterns, step 4 thread/draft rows)

Run (detached, long):
    nohup ~/guidance-for-connected-vehicle-experience-on-aws/.venv/bin/python \\
        -m pytest scripts/tests/test_live_verify_fi_quality_after6_s2b.py -v \\
        > /tmp/2026-10-05-fi-fleet-health-stage-d-64/s2b/pytest.log 2>&1 &

Requires:
  ~/automotive-data-platform-on-aws at ADP 02b8716f (or FI_LOOP_WORLD_ADP_REPO)
  ~/connected-mobility-guidance-on-aws at CMS main (or FI_LOOP_WORLD_CMS_REPO)
  PYTHONDONTWRITEBYTECODE=1 when running mutations
"""
from __future__ import annotations

import datetime as dt
import hashlib
import importlib.util
import json
import os
import signal
import socket
import uuid
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterator

import pytest

# ---------------------------------------------------------------------------
# Repo presence checks
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
# Load the script module
# ---------------------------------------------------------------------------

_spec = importlib.util.spec_from_file_location("lv_s2b_mod", _SCRIPT)
lv = importlib.util.module_from_spec(_spec)  # type: ignore[arg-type]
_spec.loader.exec_module(lv)  # type: ignore[union-attr]

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

STAGE = "staging"
REGION = "us-west-2"
ADP_REGION = "us-east-1"
ACCOUNT = "123456789012"

_VALID_RUN_ID = "fi-quality-20261027T150000-s2b00001"

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


def _make_vins(seed: str, n: int) -> list[str]:
    """Return n sorted unique 17-char VINs using valid VIN chars [A-HJ-NPR-Z0-9]."""
    _CH = "ABCDEFGHJKMNPRSTUVWXYZ0123456789"
    out: set[str] = set()
    i = 0
    while len(out) < n:
        h = hashlib.sha256(f"{seed}-{i}".encode()).digest()
        vin = "".join(_CH[b % len(_CH)] for b in h[:17])
        out.add(vin)
        i += 1
    return sorted(out)


def _sha256_of_vins(vins: list[str]) -> str:
    return hashlib.sha256(json.dumps(sorted(vins)).encode()).hexdigest()


def _fresh_manifest45(run_id: str = _VALID_RUN_ID) -> dict:
    """Manifest with all item 4/5 fields initialised."""
    return {
        "run_id": run_id,
        "stage": STAGE,
        "investigations": [],
        "users": [],
        "item3_cap": None,
        "event_source_disabled": False,
        "items_passed": {},
        # item 4/5 fields
        "item4_patterns": [],
        "item4_findings": [],
        "item4_actions": [],
        "item4_thread_versions": [],
        "item4_draft_rows": [],
        "item4_vin_objects": [],
        "item4_s3_lake_objects": [],
        "item4_recall_s3_objects": [],
        "item4_vin_bucket": None,
        "item4_thread_lake_bucket": None,
        "item4_issued_recalls_bucket": None,
        "item5_findings": [],
        "item5_actions": [],
    }


def _patch_guards(monkeypatch: pytest.MonkeyPatch) -> None:
    """Patch write-window guard and pre-checks to always pass in world tests."""
    monkeypatch.setattr(lv, "_after6_write_window_guard", lambda *_a, **_k: None)
    monkeypatch.setattr(lv, "_after6_pre_checks", lambda *_a, **_k: None)
    monkeypatch.setattr(
        lv, "_utc_now",
        lambda: dt.datetime(2026, 10, 27, 10, 0, 0, tzinfo=dt.timezone.utc),
    )
    monkeypatch.setenv("LIVE_TESTS", "1")
    monkeypatch.setenv("FI_QUALITY_MANIFEST_DIR", "")


def _open_world(auto_pump: bool = False) -> Any:
    from scripts.tests.fi_loop_world.world import FiLoopWorld  # noqa: PLC0415
    return FiLoopWorld(auto_pump=auto_pump)


def _seed_base_world(w: Any) -> None:
    """Seed one normal pattern for items 2/3 (so world opens correctly)."""
    w.seed_pattern(
        pattern_id="f0012345abcdef01",
        vin_list=_make_vins("base-world", 8),
        failure_mode_id="fm-base-001",
        code_family="P0AB",
    )


def _seed_item4_livetest_patterns(
    w: Any, run_id: str, stage: str = STAGE
) -> list[dict]:
    """Seed the 4 LIVETEST- pattern and corpus rows for item 4 directly in the world's DDB.

    C3 fix: uses real corpus failure mode IDs (FM-BCP-01 etc.) instead of synthetic ones.
    Seeds adp-staging-fault-patterns + adp-staging-engineering-corpus.
    """
    import boto3  # noqa: PLC0415
    ddb = boto3.resource("dynamodb", region_name=ADP_REGION)
    pat_tbl = ddb.Table(f"adp-{stage}-fault-patterns")
    corpus_tbl = ddb.Table(f"adp-{stage}-engineering-corpus")

    # C3: real corpus fm_ids (match lv._C3_REAL_FM_IDS)
    _REAL_FM_IDS = {
        "sw": "FM-BCP-01", "hw": "FM-BCP-01", "mon": "FM-BCP-01", "unk": "FM-COMM-01",
    }
    # Real option IDs per remedy type from real corpus
    _REAL_OPTION_IDS = {
        "sw": "OPT-FM-BCP-01-B",  # software_update
        "hw": "OPT-FM-BCP-01-A",  # hardware_recall
        "mon": "OPT-FM-BCP-01-C",  # monitor
    }

    # Seed real corpus entries in the world
    _seeded: set = set()
    for _sfx, _fm in _REAL_FM_IDS.items():
        if _fm in _seeded:
            continue
        _seeded.add(_fm)
        if _fm == "FM-BCP-01":
            _opts = [
                {"remedy_option_id": "OPT-FM-BCP-01-A", "remedy_type": "hardware_recall",
                 "parts_cost_per_vehicle_usd": Decimal("250"),
                 "labor_hours_per_vehicle": Decimal("3"),
                 "lead_time_weeks": Decimal("8"), "over_the_air": False},
                {"remedy_option_id": "OPT-FM-BCP-01-B", "remedy_type": "software_update",
                 "over_the_air": True, "lead_time_weeks": Decimal("4"),
                 "parts_cost_per_vehicle_usd": Decimal("0"),
                 "labor_hours_per_vehicle": Decimal("0")},
                {"remedy_option_id": "OPT-FM-BCP-01-C", "remedy_type": "monitor"},
                {"remedy_option_id": "opt-existing-only", "remedy_type": "monitor"},
            ]
            _syms = ["P0BC0"]
        else:
            _opts = [
                {"remedy_option_id": "OPT-FM-COMM-01-A", "remedy_type": "hardware_recall"},
                {"remedy_option_id": "OPT-FM-COMM-01-B", "remedy_type": "software_update"},
                {"remedy_option_id": "opt-existing-only", "remedy_type": "monitor"},
            ]
            _syms = ["P0CM0"]
        corpus_tbl.put_item(Item={
            "pk": f"fm#{_fm}", "failure_mode_id": _fm,
            "component_name": f"Real {_fm}", "component_id": _fm.lower(),
            "mechanism": f"{_fm.lower()}_mechanism",
            "do_not_drive": False, "park_outside": False,
            "cause_dimension": "supplier_lot", "symptom_codes": _syms,
            "options": _opts,
        })

    _hex = run_id[-8:]
    specs_out = []
    for suffix, remedy_type in [
        ("sw",  "software_update"),
        ("hw",  "hardware_recall"),
        ("mon", "monitor"),
        ("unk", None),
    ]:
        pattern_id = f"LIVETEST-4-{run_id}-{suffix}"
        real_fm_id = _REAL_FM_IDS[suffix]
        option_id = _REAL_OPTION_IDS.get(suffix, f"opt-unk-{_hex}-NOTEXIST")
        n_vins = 4
        vins = _make_vins(f"{run_id}-{suffix}", n_vins)
        sha256 = _sha256_of_vins(vins)
        lot_val = f"LTST{_hex[:4].upper()}/LOT{suffix.upper()[:3]}01"
        code_family = "P0BC" if real_fm_id == "FM-BCP-01" else "P0LT"

        pat_tbl.put_item(Item={
            "pattern_id": pattern_id, "pattern_group_id": pattern_id,
            "candidate_rank": Decimal("1"), "code_family": code_family,
            "cohort_dimension": "supplier_lot", "cohort_value": lot_val,
            "rate_ratio": Decimal("3.0"), "p_value": Decimal("0.001"),
            "affected_vins": vins, "affected_vins_count": len(vins),
            "vehicles_affected": len(vins), "affected_vins_sha256": sha256,
            "livetest_run_id": run_id, "failure_mode_id": real_fm_id,
        })
        specs_out.append({
            "pattern_id": pattern_id, "fm_id": real_fm_id,
            "suffix": suffix, "remedy_type": remedy_type,
            "option_id": option_id, "vins": vins,
            "sha256": sha256, "lot_val": lot_val, "code_family": code_family,
        })

    return specs_out

def _build_item4_finding(
    *,
    pattern_id: str,
    fm_id: str,
    option_id: str,
    remedy_type: str | None,
    vins: list[str],
    code_family: str = "P0LT",
    cohort_dim: str = "supplier_lot",
    cohort_val: str = "LTST0001/LOT001",
    run_id: str = _VALID_RUN_ID,
) -> dict:
    """Build a population-scoped quality.investigation Finding for item 4.

    Uses build_investigation_finding from agents.fi_quality.write — verified
    against the deployed code (not assumed names).  The recommended_action is
    set by the function; recommended option_id comes from rc_options.

    Source: agents/fi_quality/write.py:build_investigation_finding (pinned 02b8716f).
    """
    from agents.fi_quality.write import build_investigation_finding  # noqa: PLC0415

    _hex = run_id[-8:]
    inv_id = str(uuid.uuid4())
    candidate_id = f"cand-lt-{_hex[:8]}"

    bundle = {
        "pattern": {
            "pattern_id": pattern_id,
            "pattern_group_id": pattern_id,
            "code_family": code_family,
            "cohort_dimension": cohort_dim,
            "cohort_value": cohort_val,
            "dimension": cohort_dim,
            "value": cohort_val,
            "ratio": Decimal("3"),
            "p_value": Decimal("0001") / Decimal("1000"),
            "vehicles_affected": len(vins),
        },
        "candidates": [
            {
                "candidate_id": candidate_id,
                "dimension": cohort_dim,
                "value": cohort_val,
                "ratio": Decimal("3"),
                "p_value": Decimal("0.001"),
                "rank": 1,
            }
        ],
        "failure_modes": [],
        "affected_vins": vins,
        "controls": [],
    }
    root_cause = {
        "candidate_id": candidate_id,
        "failure_mode_id": fm_id,
        "diagnosis_source": "rules",
        "confidence": "low",
        "evidence_refs": [],
    }
    if remedy_type is not None:
        rc_options = [{
            "remedy_option_id": option_id,
            "remedy_type": remedy_type,
            "recommended": True,
            "total_cost": Decimal("0"),
            "total_vehicles": len(vins),
            "parts_cost_per_vehicle": Decimal("0"),
            "tradeoffs": [],
        }]
    else:
        rc_options = [{
            "remedy_option_id": "opt-existing-only",
            "remedy_type": "monitor",
            "recommended": True,
            "total_cost": Decimal("0"),
            "total_vehicles": len(vins),
            "parts_cost_per_vehicle": Decimal("0"),
            "tradeoffs": [],
        }]

    return build_investigation_finding(
        investigation_id=inv_id,
        pattern_id=pattern_id,
        bundle=bundle,
        root_cause=root_cause,
        options=rc_options,
        computed_at="2026-10-27T10:00:00Z",
    )


def _write_avx_finding(w: Any, finding: dict) -> str:
    """Write the finding to the world's AVX findings table; return finding_id.

    Coerces floats to Decimal before writing (DDB rejects Python floats).
    Source: agents/tier2/writer.py:_coerce_floats.
    """
    from agents.tier2.writer import _coerce_floats  # noqa: PLC0415
    fid = finding.get("finding_id", "")
    assert fid, f"finding has no finding_id: {finding!r:.200}"
    safe_finding = _coerce_floats(finding)
    w.table(f"vsa-{STAGE}-avx-findings").put_item(Item=safe_finding)
    return fid


def _approve_via_world(
    w: Any,
    *,
    finding_id: str,
    option_id: str,
    sub: str = "eng-item4-001",
) -> dict:
    """POST /findings/{finding_id}/approve with record_engineering_decision.

    Returns the parsed response body. Raises AssertionError on non-200 status.
    Source: agents/executors/adp.py, lambdas/api-avx-findings/handler.py.
    """
    import json as _json  # noqa: PLC0415
    import urllib.error  # noqa: PLC0415
    import urllib.request  # noqa: PLC0415

    token = w._mint_engineer_token(sub)
    body = {
        "action_kind": "record_engineering_decision",
        "target_system": "adp",
        "parameters": {"option_id": option_id},
    }
    req = urllib.request.Request(
        f"{w.avx_url}findings/{finding_id}/approve",
        data=_json.dumps(body).encode(),
        method="POST",
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req) as resp:
            return _json.loads(resp.read())
    except urllib.error.HTTPError as e:
        err = e.read().decode()
        last_req = ""
        if hasattr(w, "avx_api") and w.avx_api.requests:
            approve_reqs = [r for r in w.avx_api.requests
                            if "/approve" in r.get("route", "")]
            if approve_reqs and approve_reqs[-1].get("error"):
                last_req = f"\nHandler error: {approve_reqs[-1]['error'][:500]}"
        return {"_http_error": e.code, "_body": err, "_handler": last_req}


def _get_thread_row(stage: str, thread_id: str) -> dict | None:
    """Get the thread v1 row from ADP DDB (lowest version)."""
    import boto3  # noqa: PLC0415
    ddb = boto3.resource("dynamodb", region_name=ADP_REGION)
    rows = ddb.Table(f"adp-{stage}-digital-thread").query(
        KeyConditionExpression="thread_id = :t",
        ExpressionAttributeValues={":t": thread_id},
        ConsistentRead=True,
    ).get("Items", [])
    if not rows:
        return None
    return min(rows, key=lambda r: int(r.get("version", 9999)))


def _get_thread_rows_all(stage: str, thread_id: str) -> list[dict]:
    """Get all thread rows for a thread_id."""
    import boto3  # noqa: PLC0415
    ddb = boto3.resource("dynamodb", region_name=ADP_REGION)
    return ddb.Table(f"adp-{stage}-digital-thread").query(
        KeyConditionExpression="thread_id = :t",
        ExpressionAttributeValues={":t": thread_id},
        ConsistentRead=True,
    ).get("Items", [])


def _find_quality_issue_finding(stage: str) -> dict | None:
    """Find the quality.issue Finding in AVX findings table (scans, not indexed)."""
    import boto3  # noqa: PLC0415
    ddb = boto3.resource("dynamodb", region_name=REGION)
    resp = ddb.Table(f"vsa-{stage}-avx-findings").scan(
        FilterExpression="finding_kind = :k",
        ExpressionAttributeValues={":k": "quality.issue"},
    )
    items = resp.get("Items", [])
    if not items:
        return None
    # Return the one with a software draft (action_kind == issue_software_campaign)
    for it in items:
        rec = it.get("recommended_action") or {}
        if rec.get("action_kind") == "issue_software_campaign":
            return it
    return items[0]


def _issue_software_via_world(
    w: Any,
    *,
    issue_finding_id: str,
    sub: str = "eng-item5-001",
) -> dict:
    """POST /findings/{id}/approve with issue_software_campaign.

    Returns the parsed response body. Raises AssertionError on non-200 status.
    Source: agents/executors/adp.py:_dispatch_issue, adp handler:_handle_issue_software_campaign.
    """
    import json as _json  # noqa: PLC0415
    import urllib.error  # noqa: PLC0415
    import urllib.request  # noqa: PLC0415

    token = w._mint_engineer_token(sub)
    body = {
        "action_kind": "issue_software_campaign",
        "target_system": "adp",
        "parameters": {},
    }
    req = urllib.request.Request(
        f"{w.avx_url}findings/{issue_finding_id}/approve",
        data=_json.dumps(body).encode(),
        method="POST",
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req) as resp:
            return _json.loads(resp.read())
    except urllib.error.HTTPError as e:
        err = e.read().decode()
        return {"_http_error": e.code, "_body": err}


# ===========================================================================
# W45-H: happy path for items 4 and 5 via _item_4 / _item_5 directly
# ===========================================================================

@pytest.mark.skipif(not _REPOS_PRESENT, reason=_SKIP_REASON)
def test_w4h_sw_produces_pass_rows(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """item 4 sw: action PASS, thread-status PASS, no-recall-id PASS (W8), vin-sha256 PASS.

    RED with placeholder (_item_4 SKIPs); GREEN with real implementation.
    Design §Item 4 software_update checks.
    """
    _patch_guards(monkeypatch)
    run_id = _VALID_RUN_ID

    with _open_world() as w:
        specs = _seed_item4_livetest_patterns(w, run_id)
        sw_spec = next(s for s in specs if s["suffix"] == "sw")
        sw_finding = _build_item4_finding(
            pattern_id=sw_spec["pattern_id"],
            fm_id=sw_spec["fm_id"],
            option_id=sw_spec["option_id"],
            remedy_type=sw_spec["remedy_type"],
            vins=sw_spec["vins"],
            run_id=run_id,
        )
        _write_avx_finding(w, sw_finding)

        manifest = _fresh_manifest45(run_id)
        manifest["item4_patterns"].append({
            "pattern_id": sw_spec["pattern_id"],
            "fm_id": sw_spec["fm_id"],
            "vins": sw_spec["vins"],
            "sha256": sw_spec["sha256"],
            "livetest_run_id": run_id,
        })
        manifest["item4_findings"].append({
            "finding_id": sw_finding["finding_id"],
            "finding_kind": sw_finding.get("finding_kind"),
            "pattern_id": sw_spec["pattern_id"],
        })

        old_int = signal.getsignal(signal.SIGINT)
        old_term = signal.getsignal(signal.SIGTERM)
        try:
            res = lv.Results()
            lv._item_4(
                res,
                stage=STAGE,
                run_id=run_id,
                manifest=manifest,
                manifest_dir=str(tmp_path),
                allow_writes=True,
            )
        finally:
            signal.signal(signal.SIGINT, old_int)
            signal.signal(signal.SIGTERM, old_term)

    pass_labels = {r[0] for r in res.rows if r[1] == "PASS"}
    fail_labels = {r[0] for r in res.rows if r[1] == "FAIL"}

    # These labels must be PASS → will be RED with placeholder (SKIP → no PASS)
    assert "item-4-sw-action" in pass_labels, (
        f"item-4-sw-action PASS expected (RED: _item_4 is placeholder). rows={res.rows}"
    )
    assert "item-4-sw-thread-status" in pass_labels, (
        f"item-4-sw-thread-status PASS expected. rows={res.rows}"
    )
    assert "item-4-sw-no-recall-id" in pass_labels, (
        f"item-4-sw-no-recall-id PASS expected (W8 check). rows={res.rows}"
    )
    assert "item-4-sw-vin-sha256" in pass_labels, (
        f"item-4-sw-vin-sha256 PASS expected (VIN sha256 check). rows={res.rows}"
    )
    assert not fail_labels, f"Unexpected FAILs: {fail_labels}"


@pytest.mark.skipif(not _REPOS_PRESENT, reason=_SKIP_REASON)
def test_w4h_mon_produces_pass_rows(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """item 4 mon: action PASS, thread-present PASS, no-campaign-id PASS (C8).

    RED with placeholder; GREEN with real implementation.
    """
    _patch_guards(monkeypatch)
    run_id = _VALID_RUN_ID

    with _open_world() as w:
        specs = _seed_item4_livetest_patterns(w, run_id)
        mon_spec = next(s for s in specs if s["suffix"] == "mon")
        finding = _build_item4_finding(
            pattern_id=mon_spec["pattern_id"],
            fm_id=mon_spec["fm_id"],
            option_id=mon_spec["option_id"],
            remedy_type=mon_spec["remedy_type"],
            vins=mon_spec["vins"],
            run_id=run_id,
        )
        _write_avx_finding(w, finding)

        manifest = _fresh_manifest45(run_id)
        manifest["item4_patterns"].append({
            "pattern_id": mon_spec["pattern_id"],
            "fm_id": mon_spec["fm_id"],
            "vins": mon_spec["vins"],
            "sha256": mon_spec["sha256"],
            "livetest_run_id": run_id,
        })
        manifest["item4_findings"].append({
            "finding_id": finding["finding_id"],
            "finding_kind": finding.get("finding_kind"),
            "pattern_id": mon_spec["pattern_id"],
        })

        old_int = signal.getsignal(signal.SIGINT)
        old_term = signal.getsignal(signal.SIGTERM)
        try:
            res = lv.Results()
            lv._item_4(
                res,
                stage=STAGE,
                run_id=run_id,
                manifest=manifest,
                manifest_dir=str(tmp_path),
                allow_writes=True,
            )
        finally:
            signal.signal(signal.SIGINT, old_int)
            signal.signal(signal.SIGTERM, old_term)

    pass_labels = {r[0] for r in res.rows if r[1] == "PASS"}
    assert "item-4-mon-action" in pass_labels, (
        f"item-4-mon-action PASS expected. rows={res.rows}"
    )
    assert "item-4-mon-no-campaign-id" in pass_labels, (
        f"item-4-mon-no-campaign-id PASS expected (C8 check). rows={res.rows}"
    )


@pytest.mark.skipif(not _REPOS_PRESENT, reason=_SKIP_REASON)
def test_w4h_unk_produces_declined_pass(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """item 4 unk: action declined PASS (M8 check).

    RED with placeholder; GREEN with real implementation.
    """
    _patch_guards(monkeypatch)
    run_id = _VALID_RUN_ID

    with _open_world() as w:
        specs = _seed_item4_livetest_patterns(w, run_id)
        unk_spec = next(s for s in specs if s["suffix"] == "unk")
        finding = _build_item4_finding(
            pattern_id=unk_spec["pattern_id"],
            fm_id=unk_spec["fm_id"],
            option_id=unk_spec["option_id"],
            remedy_type=None,
            vins=unk_spec["vins"],
            run_id=run_id,
        )
        _write_avx_finding(w, finding)

        manifest = _fresh_manifest45(run_id)
        manifest["item4_patterns"].append({
            "pattern_id": unk_spec["pattern_id"],
            "fm_id": unk_spec["fm_id"],
            "vins": unk_spec["vins"],
            "sha256": unk_spec["sha256"],
            "livetest_run_id": run_id,
        })
        manifest["item4_findings"].append({
            "finding_id": finding["finding_id"],
            "finding_kind": finding.get("finding_kind"),
            "pattern_id": unk_spec["pattern_id"],
        })

        old_int = signal.getsignal(signal.SIGINT)
        old_term = signal.getsignal(signal.SIGTERM)
        try:
            res = lv.Results()
            lv._item_4(
                res,
                stage=STAGE,
                run_id=run_id,
                manifest=manifest,
                manifest_dir=str(tmp_path),
                allow_writes=True,
            )
        finally:
            signal.signal(signal.SIGINT, old_int)
            signal.signal(signal.SIGTERM, old_term)

    pass_labels = {r[0] for r in res.rows if r[1] == "PASS"}
    assert "item-4-unk-action" in pass_labels, (
        f"item-4-unk-action PASS expected (M8 check: declined). rows={res.rows}"
    )
    # RED: item-4-unk-no-thread-record is not emitted until the unk branch is fixed (6.5 stage 1).
    assert "item-4-unk-no-thread-record" in pass_labels, (
        f"item-4-unk-no-thread-record PASS expected (no thread row for unknown option). rows={res.rows}"
    )
    # RED: item-4-unk-no-lake-object is not emitted until the unk branch is fixed (6.5 stage 1).
    assert "item-4-unk-no-lake-object" in pass_labels, (
        f"item-4-unk-no-lake-object PASS expected (no lake object for unknown option). rows={res.rows}"
    )


@pytest.mark.skipif(not _REPOS_PRESENT, reason=_SKIP_REASON)
def test_w4f_unk_wrongly_writes_thread_row_fails(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """item 4 unk: if the writer wrongly writes a thread row, item-4-unk-no-thread-record FAILs.

    RED until unk branch fix (6.5 stage 1): the check doesn't run yet.
    GREEN after fix: the check queries DDB; a pre-seeded row causes FAIL naming the thread_id.
    """
    _patch_guards(monkeypatch)
    run_id = _VALID_RUN_ID

    with _open_world() as w:
        specs = _seed_item4_livetest_patterns(w, run_id)
        unk_spec = next(s for s in specs if s["suffix"] == "unk")
        finding = _build_item4_finding(
            pattern_id=unk_spec["pattern_id"],
            fm_id=unk_spec["fm_id"],
            option_id=unk_spec["option_id"],
            remedy_type=None,
            vins=unk_spec["vins"],
            run_id=run_id,
        )
        _write_avx_finding(w, finding)

        # Derive the thread_id that _item_4 will look for (same formula as ADP writer).
        import hashlib as _hh  # noqa: PLC0415
        _k = f"{unk_spec['pattern_id']}|{finding['finding_id']}"
        unk_thread_id = "thread-" + _hh.sha256(_k.encode()).hexdigest()[:16]

        # Wrongly pre-seed a thread row for the unk thread_id (simulates a writer bug).
        import boto3 as _b3  # noqa: PLC0415
        _ddb = _b3.resource("dynamodb", region_name="us-east-1")
        _ddb.Table(f"adp-{STAGE}-digital-thread").put_item(Item={
            "thread_id": unk_thread_id,
            "version": 1,
            "status": "decided",
        })

        manifest = _fresh_manifest45(run_id)
        manifest["item4_patterns"].append({
            "pattern_id": unk_spec["pattern_id"],
            "fm_id": unk_spec["fm_id"],
            "vins": unk_spec["vins"],
            "sha256": unk_spec["sha256"],
            "livetest_run_id": run_id,
        })
        manifest["item4_findings"].append({
            "finding_id": finding["finding_id"],
            "finding_kind": finding.get("finding_kind"),
            "pattern_id": unk_spec["pattern_id"],
        })

        old_int = signal.getsignal(signal.SIGINT)
        old_term = signal.getsignal(signal.SIGTERM)
        try:
            res = lv.Results()
            lv._item_4(
                res,
                stage=STAGE,
                run_id=run_id,
                manifest=manifest,
                manifest_dir=str(tmp_path),
                allow_writes=True,
            )
        finally:
            signal.signal(signal.SIGINT, old_int)
            signal.signal(signal.SIGTERM, old_term)

    fail_labels = {r[0] for r in res.rows if r[1] == "FAIL"}
    # RED: without the fix, item-4-unk-no-thread-record is not emitted at all.
    # GREEN: the check runs and finds the pre-seeded row → FAIL naming unk_thread_id.
    assert "item-4-unk-no-thread-record" in fail_labels, (
        f"item-4-unk-no-thread-record FAIL expected when thread row wrongly exists. rows={res.rows}"
    )
    # The FAIL detail must name the thread_id.
    fail_row = next((r for r in res.rows if r[0] == "item-4-unk-no-thread-record" and r[1] == "FAIL"), None)
    assert fail_row is not None
    assert unk_thread_id in fail_row[2], (
        f"FAIL detail must name the thread_id ({unk_thread_id!r}); got {fail_row[2]!r}"
    )


@pytest.mark.skipif(not _REPOS_PRESENT, reason=_SKIP_REASON)
def test_w5h_issue_software_campaign_produces_pass_rows(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """item 5: issue_software_campaign → action executed, thread issued, S4 draft id match.

    RED with placeholder (_item_5 SKIPs); GREEN with real implementation.
    Design §Item 5.
    """
    _patch_guards(monkeypatch)
    run_id = _VALID_RUN_ID

    with _open_world() as w:
        specs = _seed_item4_livetest_patterns(w, run_id)
        sw_spec = next(s for s in specs if s["suffix"] == "sw")
        sw_finding = _build_item4_finding(
            pattern_id=sw_spec["pattern_id"],
            fm_id=sw_spec["fm_id"],
            option_id=sw_spec["option_id"],
            remedy_type=sw_spec["remedy_type"],
            vins=sw_spec["vins"],
            run_id=run_id,
        )
        sw_finding_id = _write_avx_finding(w, sw_finding)

        # Approve sw decision (creates quality.issue Finding via hook)
        action = _approve_via_world(w, finding_id=sw_finding_id,
                                    option_id=sw_spec["option_id"])
        assert action.get("status") == "executed", (
            f"sw approve must execute first for item 5. action={action!r:.200}"
        )

        # Build manifest for item 5 (with sw results from item 4)
        manifest = _fresh_manifest45(run_id)
        # thread_id comes from action.target_ref
        thread_id = action.get("target_ref", "")
        manifest["item4_thread_versions"].append({
            "thread_id": thread_id,
            "version": 1,
            "action_id": action.get("action_id"),
            "finding_id": sw_finding_id,
        })

        old_int = signal.getsignal(signal.SIGINT)
        old_term = signal.getsignal(signal.SIGTERM)
        try:
            res = lv.Results()
            lv._item_5(
                res,
                stage=STAGE,
                run_id=run_id,
                manifest=manifest,
                manifest_dir=str(tmp_path),
                allow_writes=True,
            )
        finally:
            signal.signal(signal.SIGINT, old_int)
            signal.signal(signal.SIGTERM, old_term)

    pass_labels = {r[0] for r in res.rows if r[1] == "PASS"}
    fail_labels = {r[0] for r in res.rows if r[1] == "FAIL"}

    # These must be PASS → RED with placeholder
    assert "item-5-issue-action" in pass_labels, (
        f"item-5-issue-action PASS expected. rows={res.rows}"
    )
    assert "item-5-thread-issued" in pass_labels, (
        f"item-5-thread-issued PASS expected (M10 check). rows={res.rows}"
    )
    assert "item-5-draft-issued-id" in pass_labels, (
        f"item-5-draft-issued-id PASS expected (S4 check). rows={res.rows}"
    )
    assert not fail_labels, f"Unexpected FAILs: {fail_labels}"


# ===========================================================================
# W4-F*: FAIL-path item 4 world tests
# ===========================================================================

@pytest.mark.skipif(not _REPOS_PRESENT, reason=_SKIP_REASON)
def test_w4f_c8_monitor_no_thread(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """C8 FAIL: monitor option approved but thread record is absent.

    Pre-seeds the mon finding, approves it (creates thread), deletes the thread row,
    then calls _item_4 with the pre-seeded finding in the manifest so _item_4 reuses
    it and checks the now-absent thread.  Expects FAIL on item-4-mon-thread-present.
    """
    _patch_guards(monkeypatch)
    run_id = _VALID_RUN_ID

    with _open_world() as w:
        specs = _seed_item4_livetest_patterns(w, run_id)
        mon_spec = next(s for s in specs if s["suffix"] == "mon")
        finding = _build_item4_finding(
            pattern_id=mon_spec["pattern_id"],
            fm_id=mon_spec["fm_id"],
            option_id=mon_spec["option_id"],
            remedy_type="monitor",
            vins=mon_spec["vins"],
            run_id=run_id,
        )
        finding_id = _write_avx_finding(w, finding)

        # Approve first (creates thread row)
        action = _approve_via_world(w, finding_id=finding_id,
                                    option_id=mon_spec["option_id"])
        assert action.get("status") == "executed", f"pre-condition: {action!r:.100}"

        # Inject defect: delete the thread row
        import boto3 as _b3  # noqa: PLC0415
        ddb = _b3.resource("dynamodb", region_name=ADP_REGION)
        thread_id = action.get("target_ref", "")
        if thread_id:
            rows = ddb.Table(f"adp-{STAGE}-digital-thread").query(
                KeyConditionExpression="thread_id = :t",
                ExpressionAttributeValues={":t": thread_id},
            ).get("Items", [])
            for row in rows:
                ddb.Table(f"adp-{STAGE}-digital-thread").delete_item(
                    Key={"thread_id": row["thread_id"], "version": row["version"]}
                )

        # Pre-seed only the mon finding in the manifest (other suffixes not pre-seeded)
        manifest = _fresh_manifest45(run_id)
        manifest["item4_patterns"].append({
            "pattern_id": mon_spec["pattern_id"],
            "fm_id": mon_spec["fm_id"],
            "vins": mon_spec["vins"],
            "sha256": mon_spec["sha256"],
            "livetest_run_id": run_id,
        })
        manifest["item4_findings"].append({
            "finding_id": finding_id,
            "finding_kind": "quality.investigation",
            "pattern_id": mon_spec["pattern_id"],
        })

        old_int = signal.getsignal(signal.SIGINT)
        old_term = signal.getsignal(signal.SIGTERM)
        try:
            res = lv.Results()
            lv._item_4(
                res,
                stage=STAGE,
                run_id=run_id,
                manifest=manifest,
                manifest_dir=str(tmp_path),
                allow_writes=True,
            )
        finally:
            signal.signal(signal.SIGINT, old_int)
            signal.signal(signal.SIGTERM, old_term)

    fail_labels = {r[0] for r in res.rows if r[1] == "FAIL"}
    assert "item-4-mon-thread-present" in fail_labels, (
        f"Expected FAIL on item-4-mon-thread-present (C8 no-thread). rows={res.rows}"
    )


@pytest.mark.skipif(not _REPOS_PRESENT, reason=_SKIP_REASON)
def test_w4f_c8_monitor_has_campaign_id(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """C8 FAIL: monitor option but thread row has a campaign_id set.

    Pre-seeds, approves, injects defect (adds software_campaign_id to thread row),
    then calls _item_4 which reuses the pre-seeded finding and catches the defect.
    Expects FAIL on item-4-mon-no-campaign-id.
    """
    _patch_guards(monkeypatch)
    run_id = _VALID_RUN_ID

    with _open_world() as w:
        specs = _seed_item4_livetest_patterns(w, run_id)
        mon_spec = next(s for s in specs if s["suffix"] == "mon")
        finding = _build_item4_finding(
            pattern_id=mon_spec["pattern_id"],
            fm_id=mon_spec["fm_id"],
            option_id=mon_spec["option_id"],
            remedy_type="monitor",
            vins=mon_spec["vins"],
            run_id=run_id,
        )
        finding_id = _write_avx_finding(w, finding)

        action = _approve_via_world(w, finding_id=finding_id,
                                    option_id=mon_spec["option_id"])
        assert action.get("status") == "executed", f"pre-condition: {action!r:.100}"

        # Inject defect: add a software_campaign_id to the thread row
        import boto3 as _b3  # noqa: PLC0415
        ddb = _b3.resource("dynamodb", region_name=ADP_REGION)
        thread_id = action.get("target_ref", "")
        if thread_id:
            rows = ddb.Table(f"adp-{STAGE}-digital-thread").query(
                KeyConditionExpression="thread_id = :t",
                ExpressionAttributeValues={":t": thread_id},
            ).get("Items", [])
            if rows:
                row = rows[0]
                row["software_campaign_id"] = "MRD-SW-INJECTED-DEFECT"
                ddb.Table(f"adp-{STAGE}-digital-thread").put_item(Item=row)

        manifest = _fresh_manifest45(run_id)
        manifest["item4_patterns"].append({
            "pattern_id": mon_spec["pattern_id"],
            "fm_id": mon_spec["fm_id"],
            "vins": mon_spec["vins"],
            "sha256": mon_spec["sha256"],
            "livetest_run_id": run_id,
        })
        manifest["item4_findings"].append({
            "finding_id": finding_id,
            "finding_kind": "quality.investigation",
            "pattern_id": mon_spec["pattern_id"],
        })

        old_int = signal.getsignal(signal.SIGINT)
        old_term = signal.getsignal(signal.SIGTERM)
        try:
            res = lv.Results()
            lv._item_4(
                res,
                stage=STAGE,
                run_id=run_id,
                manifest=manifest,
                manifest_dir=str(tmp_path),
                allow_writes=True,
            )
        finally:
            signal.signal(signal.SIGINT, old_int)
            signal.signal(signal.SIGTERM, old_term)

    fail_labels = {r[0] for r in res.rows if r[1] == "FAIL"}
    assert "item-4-mon-no-campaign-id" in fail_labels, (
        f"Expected FAIL on item-4-mon-no-campaign-id (C8). rows={res.rows}"
    )


@pytest.mark.skipif(not _REPOS_PRESENT, reason=_SKIP_REASON)
def test_w4f_w8_software_has_recall_id(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """W8 FAIL: software_update thread row has recall_campaign_id set.

    Pre-seeds, approves, injects defect (adds recall_campaign_id to sw thread row),
    then calls _item_4 with pre-seeded finding. Expects FAIL on item-4-sw-no-recall-id.
    """
    _patch_guards(monkeypatch)
    run_id = _VALID_RUN_ID

    with _open_world() as w:
        specs = _seed_item4_livetest_patterns(w, run_id)
        sw_spec = next(s for s in specs if s["suffix"] == "sw")
        finding = _build_item4_finding(
            pattern_id=sw_spec["pattern_id"],
            fm_id=sw_spec["fm_id"],
            option_id=sw_spec["option_id"],
            remedy_type="software_update",
            vins=sw_spec["vins"],
            run_id=run_id,
        )
        finding_id = _write_avx_finding(w, finding)

        action = _approve_via_world(w, finding_id=finding_id,
                                    option_id=sw_spec["option_id"])
        assert action.get("status") == "executed", f"pre-condition: {action!r:.100}"

        # Inject defect: add recall_campaign_id to sw thread row
        import boto3 as _b3  # noqa: PLC0415
        ddb = _b3.resource("dynamodb", region_name=ADP_REGION)
        thread_id = action.get("target_ref", "")
        if thread_id:
            rows = ddb.Table(f"adp-{STAGE}-digital-thread").query(
                KeyConditionExpression="thread_id = :t",
                ExpressionAttributeValues={":t": thread_id},
            ).get("Items", [])
            if rows:
                row = rows[0]
                row["recall_campaign_id"] = "MRD-REC-INJECTED-DEFECT"
                ddb.Table(f"adp-{STAGE}-digital-thread").put_item(Item=row)

        manifest = _fresh_manifest45(run_id)
        manifest["item4_patterns"].append({
            "pattern_id": sw_spec["pattern_id"],
            "fm_id": sw_spec["fm_id"],
            "vins": sw_spec["vins"],
            "sha256": sw_spec["sha256"],
            "livetest_run_id": run_id,
        })
        manifest["item4_findings"].append({
            "finding_id": finding_id,
            "finding_kind": "quality.investigation",
            "pattern_id": sw_spec["pattern_id"],
        })

        old_int = signal.getsignal(signal.SIGINT)
        old_term = signal.getsignal(signal.SIGTERM)
        try:
            res = lv.Results()
            lv._item_4(
                res,
                stage=STAGE,
                run_id=run_id,
                manifest=manifest,
                manifest_dir=str(tmp_path),
                allow_writes=True,
            )
        finally:
            signal.signal(signal.SIGINT, old_int)
            signal.signal(signal.SIGTERM, old_term)

    fail_labels = {r[0] for r in res.rows if r[1] == "FAIL"}
    assert "item-4-sw-no-recall-id" in fail_labels, (
        f"Expected FAIL on item-4-sw-no-recall-id (W8). rows={res.rows}"
    )


@pytest.mark.skipif(not _REPOS_PRESENT, reason=_SKIP_REASON)
def test_w4f_m8_unknown_not_declined(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """M8 FAIL: unknown option approve but action.status is not 'declined'.

    Injects defect: makes the world's approve handler return 200/executed for unknown option
    by swapping the approve body's option_id to one that exists in corpus.
    Expects FAIL on item-4-unk-declined.
    """
    _patch_guards(monkeypatch)
    run_id = _VALID_RUN_ID

    with _open_world() as w:
        specs = _seed_item4_livetest_patterns(w, run_id)
        unk_spec = next(s for s in specs if s["suffix"] == "unk")
        finding = _build_item4_finding(
            pattern_id=unk_spec["pattern_id"],
            fm_id=unk_spec["fm_id"],
            option_id=unk_spec["option_id"],
            remedy_type=None,
            vins=unk_spec["vins"],
            run_id=run_id,
        )
        finding_id = _write_avx_finding(w, finding)

        # Defect: instead of approving with the unknown option_id, use an existing one
        # (the corpus has "opt-existing-only") → the approve succeeds with 'executed'
        action = _approve_via_world(w, finding_id=finding_id,
                                    option_id="opt-existing-only")
        # action.status should be 'executed' now (defect injected)
        # The script's check for "unk-declined" should FAIL

        manifest = _fresh_manifest45(run_id)
        manifest["item4_patterns"].append({
            "pattern_id": unk_spec["pattern_id"],
            "fm_id": unk_spec["fm_id"],
            "vins": unk_spec["vins"],
            "sha256": unk_spec["sha256"],
            "livetest_run_id": run_id,
        })
        manifest["item4_findings"].append({
            "finding_id": finding_id,
            "finding_kind": "quality.investigation",
            "pattern_id": unk_spec["pattern_id"],
        })

        # Patch the _item_6_http (which _item_4 uses for the approve POST) to return
        # the already-executed action when called for the unk finding
        orig_http = lv._item_6_http

        def _patched_http(url: str, method: str, path: str, token: str,
                          body: dict | None = None) -> tuple:
            if f"findings/{finding_id}/approve" in path:
                return 200, action  # return the executed action
            return orig_http(url, method, path, token, body)

        monkeypatch.setattr(lv, "_item_6_http", _patched_http)

        res = lv.Results()
        lv._item_4(
            res,
            stage=STAGE,
            run_id=run_id,
            manifest=manifest,
            manifest_dir=str(tmp_path),
            allow_writes=True,
        )

    fail_labels = {r[0] for r in res.rows if r[1] == "FAIL"}
    assert "item-4-unk-action" in fail_labels, (
        f"Expected FAIL on item-4-unk-action (M8 not declined). rows={res.rows}"
    )


@pytest.mark.skipif(not _REPOS_PRESENT, reason=_SKIP_REASON)
def test_w4f_m9_action_not_executed(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """M9 FAIL: action.status is 'pending_execution' instead of 'executed'.

    Injects defect by making _item_6_http return an action with status=pending.
    Expects FAIL on item-4-sw-action.
    """
    _patch_guards(monkeypatch)
    run_id = _VALID_RUN_ID

    with _open_world() as w:
        specs = _seed_item4_livetest_patterns(w, run_id)
        sw_spec = next(s for s in specs if s["suffix"] == "sw")
        finding = _build_item4_finding(
            pattern_id=sw_spec["pattern_id"],
            fm_id=sw_spec["fm_id"],
            option_id=sw_spec["option_id"],
            remedy_type="software_update",
            vins=sw_spec["vins"],
            run_id=run_id,
        )
        finding_id = _write_avx_finding(w, finding)

        # Inject: patched _item_6_http returns pending action
        orig_http = lv._item_6_http

        def _patched_http(url: str, method: str, path: str, token: str,
                          body: dict | None = None) -> tuple:
            if "/approve" in path and "findings" in path:
                # Return a pending action (defect: not executed)
                import uuid as _uuid  # noqa: PLC0415
                return 200, {
                    "action_id": f"action-{_uuid.uuid4().hex[:8]}",
                    "status": "pending_execution",
                    "target_ref": None,
                }
            return orig_http(url, method, path, token, body)

        monkeypatch.setattr(lv, "_item_6_http", _patched_http)

        manifest = _fresh_manifest45(run_id)
        manifest["item4_patterns"].append({
            "pattern_id": sw_spec["pattern_id"],
            "fm_id": sw_spec["fm_id"],
            "vins": sw_spec["vins"],
            "sha256": sw_spec["sha256"],
            "livetest_run_id": run_id,
        })
        manifest["item4_findings"].append({
            "finding_id": finding_id,
            "finding_kind": "quality.investigation",
            "pattern_id": sw_spec["pattern_id"],
        })
        res = lv.Results()
        lv._item_4(
            res,
            stage=STAGE,
            run_id=run_id,
            manifest=manifest,
            manifest_dir=str(tmp_path),
            allow_writes=True,
        )

    fail_labels = {r[0] for r in res.rows if r[1] == "FAIL"}
    assert "item-4-sw-action" in fail_labels, (
        f"Expected FAIL on item-4-sw-action (M9 not-executed). rows={res.rows}"
    )


@pytest.mark.skipif(not _REPOS_PRESENT, reason=_SKIP_REASON)
def test_w4f_vin_sha256_mismatch(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """VIN sha256 FAIL: VIN object in S3 has wrong content.

    Injects defect by overwriting the VIN S3 object with wrong VINs after approval.
    Expects FAIL on item-4-sw-vin-sha256.
    """
    _patch_guards(monkeypatch)
    run_id = _VALID_RUN_ID

    with _open_world() as w:
        specs = _seed_item4_livetest_patterns(w, run_id)
        sw_spec = next(s for s in specs if s["suffix"] == "sw")
        finding = _build_item4_finding(
            pattern_id=sw_spec["pattern_id"],
            fm_id=sw_spec["fm_id"],
            option_id=sw_spec["option_id"],
            remedy_type="software_update",
            vins=sw_spec["vins"],
            run_id=run_id,
        )
        finding_id = _write_avx_finding(w, finding)

        action = _approve_via_world(w, finding_id=finding_id,
                                    option_id=sw_spec["option_id"])
        assert action.get("status") == "executed", f"pre-condition: {action!r:.100}"

        # Inject defect: overwrite VIN S3 object with wrong VINs
        thread_row = _get_thread_row(STAGE, action.get("target_ref", ""))
        if thread_row:
            vin_key = thread_row.get("affected_vins_s3_key", "")
            vin_bucket = w.vin_bucket
            if vin_key and vin_bucket:
                import boto3 as _b3  # noqa: PLC0415
                wrong_vins = _make_vins("wrong-vins-inject", 4)
                _b3.client("s3", region_name=ADP_REGION).put_object(
                    Bucket=vin_bucket, Key=vin_key,
                    Body=json.dumps(wrong_vins).encode(),
                )

        manifest = _fresh_manifest45(run_id)
        manifest["item4_patterns"].append({
            "pattern_id": sw_spec["pattern_id"],
            "fm_id": sw_spec["fm_id"],
            "vins": sw_spec["vins"],
            "sha256": sw_spec["sha256"],
            "livetest_run_id": run_id,
        })
        manifest["item4_findings"].append({
            "finding_id": finding_id,
            "finding_kind": "quality.investigation",
            "pattern_id": sw_spec["pattern_id"],
        })
        res = lv.Results()
        lv._item_4(
            res,
            stage=STAGE,
            run_id=run_id,
            manifest=manifest,
            manifest_dir=str(tmp_path),
            allow_writes=True,
        )

    fail_labels = {r[0] for r in res.rows if r[1] == "FAIL"}
    assert "item-4-sw-vin-sha256" in fail_labels, (
        f"Expected FAIL on item-4-sw-vin-sha256 (VIN sha256 mismatch). rows={res.rows}"
    )


# ===========================================================================
# W5-F*: FAIL-path item 5 world tests
# ===========================================================================

@pytest.mark.skipif(not _REPOS_PRESENT, reason=_SKIP_REASON)
def test_w5f_m10_thread_not_issued(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """M10 FAIL: thread version status is not 'issued' after issue_software_campaign.

    Injects defect by deleting the v2 thread row after issue approval.
    Expects FAIL on item-5-thread-issued.
    """
    _patch_guards(monkeypatch)
    run_id = _VALID_RUN_ID

    with _open_world() as w:
        specs = _seed_item4_livetest_patterns(w, run_id)
        sw_spec = next(s for s in specs if s["suffix"] == "sw")
        sw_finding = _build_item4_finding(
            pattern_id=sw_spec["pattern_id"],
            fm_id=sw_spec["fm_id"],
            option_id=sw_spec["option_id"],
            remedy_type="software_update",
            vins=sw_spec["vins"],
            run_id=run_id,
        )
        sw_finding_id = _write_avx_finding(w, sw_finding)

        sw_action = _approve_via_world(w, finding_id=sw_finding_id,
                                       option_id=sw_spec["option_id"])
        assert sw_action.get("status") == "executed", f"pre-condition: {sw_action!r:.100}"
        thread_id = sw_action.get("target_ref", "")

        issue_finding = _find_quality_issue_finding(STAGE)
        assert issue_finding is not None, "quality.issue Finding must exist after sw approve"
        issue_finding_id = issue_finding["finding_id"]

        issue_action = _issue_software_via_world(w, issue_finding_id=issue_finding_id)
        assert issue_action.get("status") == "executed", (
            f"issue_software_campaign must execute. action={issue_action!r:.100}"
        )

        # Inject defect: change the v2 thread row's status to 'decided' instead of 'issued'
        import boto3 as _b3  # noqa: PLC0415
        ddb = _b3.resource("dynamodb", region_name=ADP_REGION)
        all_rows = _get_thread_rows_all(STAGE, thread_id)
        v2_rows = [r for r in all_rows if int(r.get("version", 0)) >= 2]
        for row in v2_rows:
            row["status"] = "decided"  # defect: should be 'issued'
            ddb.Table(f"adp-{STAGE}-digital-thread").put_item(Item=row)

        manifest = _fresh_manifest45(run_id)
        manifest["item4_thread_versions"].append({
            "thread_id": thread_id, "version": 1,
            "action_id": sw_action.get("action_id"),
            "finding_id": sw_finding_id,
        })
        res = lv.Results()
        lv._item_5(
            res,
            stage=STAGE,
            run_id=run_id,
            manifest=manifest,
            manifest_dir=str(tmp_path),
            allow_writes=True,
        )

    fail_labels = {r[0] for r in res.rows if r[1] == "FAIL"}
    assert "item-5-thread-issued" in fail_labels, (
        f"Expected FAIL on item-5-thread-issued (M10). rows={res.rows}"
    )


@pytest.mark.skipif(not _REPOS_PRESENT, reason=_SKIP_REASON)
def test_w5f_s4_issued_id_mismatch(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """S4 FAIL: issued_row.software_campaign_id != draft_id.

    Injects defect by overwriting the OTA issued row's software_campaign_id.
    Expects FAIL on item-5-draft-issued-id.
    """
    _patch_guards(monkeypatch)
    run_id = _VALID_RUN_ID

    with _open_world() as w:
        specs = _seed_item4_livetest_patterns(w, run_id)
        sw_spec = next(s for s in specs if s["suffix"] == "sw")
        sw_finding = _build_item4_finding(
            pattern_id=sw_spec["pattern_id"],
            fm_id=sw_spec["fm_id"],
            option_id=sw_spec["option_id"],
            remedy_type="software_update",
            vins=sw_spec["vins"],
            run_id=run_id,
        )
        sw_finding_id = _write_avx_finding(w, sw_finding)
        sw_action = _approve_via_world(w, finding_id=sw_finding_id,
                                       option_id=sw_spec["option_id"])
        assert sw_action.get("status") == "executed", f"pre-condition: {sw_action!r:.100}"
        thread_id = sw_action.get("target_ref", "")

        issue_finding = _find_quality_issue_finding(STAGE)
        assert issue_finding is not None, "quality.issue Finding must exist"
        issue_finding_id = issue_finding["finding_id"]

        issue_action = _issue_software_via_world(w, issue_finding_id=issue_finding_id)
        assert issue_action.get("status") == "executed", (
            f"issue_software_campaign must execute. action={issue_action!r:.100}"
        )

        # Inject defect: overwrite the ISSUED THREAD ROW's software_campaign_id with a wrong value
        import boto3 as _b3  # noqa: PLC0415
        ddb = _b3.resource("dynamodb", region_name=ADP_REGION)
        all_thread_rows_v = _get_thread_rows_all(STAGE, thread_id)
        issued_thread_rows = [r for r in all_thread_rows_v if str(r.get("status")) == "issued"]
        if issued_thread_rows:
            row = issued_thread_rows[0]
            row["software_campaign_id"] = "WRONG-CAMPAIGN-ID-DEFECT"
            ddb.Table(f"adp-{STAGE}-digital-thread").put_item(Item=row)

        manifest = _fresh_manifest45(run_id)
        manifest["item4_thread_versions"].append({
            "thread_id": thread_id, "version": 1,
            "action_id": sw_action.get("action_id"),
            "finding_id": sw_finding_id,
        })
        res = lv.Results()
        lv._item_5(
            res,
            stage=STAGE,
            run_id=run_id,
            manifest=manifest,
            manifest_dir=str(tmp_path),
            allow_writes=True,
        )

    fail_labels = {r[0] for r in res.rows if r[1] == "FAIL"}
    assert "item-5-draft-issued-id" in fail_labels, (
        f"Expected FAIL on item-5-draft-issued-id (S4). rows={res.rows}"
    )


# ===========================================================================
# Cleanup tests
# ===========================================================================

@pytest.mark.skipif(not _REPOS_PRESENT, reason=_SKIP_REASON)
def test_cleanup_raise_after_first_write_undoes_all(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A raise after the first item 4 write still runs cleanup and removes the row.

    Verifies that the try/finally in _item_4 (and the outer cleanup in _after6_cleanup)
    removes the LIVETEST- pattern row even when an exception is thrown after put_item.
    Design §Cleanup "raise": residue scan finds nothing after the raise.
    """
    _patch_guards(monkeypatch)
    run_id = _VALID_RUN_ID

    raised_exc: list[Exception] = []

    with _open_world() as w:
        specs = _seed_item4_livetest_patterns(w, run_id)
        sw_spec = next(s for s in specs if s["suffix"] == "sw")

        # Patch _item_6_http to raise after the first approve attempt
        orig_http = lv._item_6_http
        call_count = [0]

        def _raising_http(url: str, method: str, path: str, token: str,
                          body: dict | None = None) -> tuple:
            if "/approve" in path or "/investigations" in path:
                call_count[0] += 1
                if call_count[0] == 1:
                    exc = RuntimeError("injected raise after first write")
                    raised_exc.append(exc)
                    raise exc
            return orig_http(url, method, path, token, body)

        monkeypatch.setattr(lv, "_item_6_http", _raising_http)

        manifest = _fresh_manifest45(run_id)
        # Pre-seed pattern in manifest so cleanup knows to delete it
        finding = _build_item4_finding(
            pattern_id=sw_spec["pattern_id"],
            fm_id=sw_spec["fm_id"],
            option_id=sw_spec["option_id"],
            remedy_type="software_update",
            vins=sw_spec["vins"],
            run_id=run_id,
        )
        finding_id = _write_avx_finding(w, finding)
        manifest["item4_patterns"].append({
            "pattern_id": sw_spec["pattern_id"],
            "fm_id": sw_spec["fm_id"],
            "vins": sw_spec["vins"],
            "sha256": sw_spec["sha256"],
            "livetest_run_id": run_id,
        })
        manifest["item4_findings"].append({
            "finding_id": finding_id,
            "finding_kind": "quality.investigation",
            "pattern_id": sw_spec["pattern_id"],
        })

        old_int = signal.getsignal(signal.SIGINT)
        old_term = signal.getsignal(signal.SIGTERM)
        try:
            res_item = lv.Results()
            try:
                lv._item_4(
                    res_item,
                    stage=STAGE,
                    run_id=run_id,
                    manifest=manifest,
                    manifest_dir=str(tmp_path),
                    allow_writes=True,
                )
            except Exception:
                pass  # expected raise propagates; item4 catches it internally
        finally:
            signal.signal(signal.SIGINT, old_int)
            signal.signal(signal.SIGTERM, old_term)

        # Now run cleanup manually
        res_cleanup = lv.Results()
        import boto3  # noqa: PLC0415
        idp = boto3.client("cognito-idp", region_name=REGION)
        pool_id_val = None
        try:
            pool_id_val = lv._resolve_pool_id(idp)
        except Exception:
            pass
        lv._after6_cleanup(
            res_cleanup,
            manifest=manifest,
            manifest_dir=str(tmp_path),
            stage=STAGE,
            idp=idp,
            pool_id=pool_id_val,
        )

        # Residue scan should find nothing
        res_residue = lv.Results()
        lv._after6_residue_scan(
            res_residue,
            manifest=manifest,
            stage=STAGE,
        )

    residue_rows = [(r[0], r[1]) for r in res_residue.rows]
    fail_residues = [r for r in residue_rows if r[1] == "FAIL"]
    assert not fail_residues, (
        f"Cleanup after raise left residue: {fail_residues}. "
        f"cleanup rows: {[(r[0], r[1]) for r in res_cleanup.rows]}"
    )
    # Verify the raise was triggered
    assert raised_exc, "test failed to inject the raise (test setup bug)"


@pytest.mark.skipif(not _REPOS_PRESENT, reason=_SKIP_REASON)
def test_cleanup_sigterm_after_first_write_undoes_all(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A SIGTERM after the first item 4 write still runs cleanup.

    Verifies that the signal handler in _item_4 routes SIGTERM to cleanup.
    Design §Cleanup "SIGTERM after first write".
    """
    _patch_guards(monkeypatch)
    run_id = _VALID_RUN_ID
    sigterm_delivered = [False]

    with _open_world() as w:
        specs = _seed_item4_livetest_patterns(w, run_id)
        sw_spec = next(s for s in specs if s["suffix"] == "sw")

        orig_http = lv._item_6_http
        call_count = [0]

        def _sigterm_http(url: str, method: str, path: str, token: str,
                          body: dict | None = None) -> tuple:
            if "/approve" in path or "/investigations" in path:
                call_count[0] += 1
                if call_count[0] == 1:
                    # Send SIGTERM to ourselves after the first write
                    import os as _os  # noqa: PLC0415
                    sigterm_delivered[0] = True
                    _os.kill(_os.getpid(), signal.SIGTERM)
            return orig_http(url, method, path, token, body)

        monkeypatch.setattr(lv, "_item_6_http", _sigterm_http)

        # Install a safety SIGTERM handler to prevent process death in pytest
        old_term = signal.getsignal(signal.SIGTERM)

        terminated = [False]

        def _safe_term(sig: int, frame: Any) -> None:
            terminated[0] = True

        signal.signal(signal.SIGTERM, _safe_term)

        manifest = _fresh_manifest45(run_id)
        finding = _build_item4_finding(
            pattern_id=sw_spec["pattern_id"],
            fm_id=sw_spec["fm_id"],
            option_id=sw_spec["option_id"],
            remedy_type="software_update",
            vins=sw_spec["vins"],
            run_id=run_id,
        )
        finding_id = _write_avx_finding(w, finding)
        manifest["item4_patterns"].append({
            "pattern_id": sw_spec["pattern_id"],
            "fm_id": sw_spec["fm_id"],
            "vins": sw_spec["vins"],
            "sha256": sw_spec["sha256"],
            "livetest_run_id": run_id,
        })
        manifest["item4_findings"].append({
            "finding_id": finding_id,
            "finding_kind": "quality.investigation",
            "pattern_id": sw_spec["pattern_id"],
        })
        try:
            res_item = lv.Results()
            try:
                lv._item_4(
                    res_item,
                    stage=STAGE,
                    run_id=run_id,
                    manifest=manifest,
                    manifest_dir=str(tmp_path),
                    allow_writes=True,
                )
            except (SystemExit, KeyboardInterrupt):
                pass
        finally:
            signal.signal(signal.SIGTERM, old_term)

        # Run cleanup
        res_cleanup = lv.Results()
        import boto3  # noqa: PLC0415
        idp = boto3.client("cognito-idp", region_name=REGION)
        pool_id_val = None
        try:
            pool_id_val = lv._resolve_pool_id(idp)
        except Exception:
            pass
        lv._after6_cleanup(
            res_cleanup,
            manifest=manifest,
            manifest_dir=str(tmp_path),
            stage=STAGE,
            idp=idp,
            pool_id=pool_id_val,
        )

        # Residue scan
        res_residue = lv.Results()
        lv._after6_residue_scan(res_residue, manifest=manifest, stage=STAGE)

    fail_residues = [r for r in res_residue.rows if r[1] == "FAIL"]
    assert not fail_residues, (
        f"Cleanup after SIGTERM left residue: {fail_residues}"
    )
    assert sigterm_delivered[0], "test failed to deliver SIGTERM (test setup bug)"


@pytest.mark.skipif(not _REPOS_PRESENT, reason=_SKIP_REASON)
def test_cleanup_delete_success_but_row_remains_caught_by_residue(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Delete answers success but row remains → caught by residue scan alone, not cleanup FAIL.

    Design: "a delete that answers success but leaves the row is caught by the residue
    scan alone". This verifies the two-mechanism separation (cleanup PASS, residue FAIL).

    Approach: run cleanup (which deletes the row), then re-insert it to simulate a row
    that survived cleanup, then run the residue scan to verify it catches the leftover.
    """
    _patch_guards(monkeypatch)
    run_id = _VALID_RUN_ID

    with _open_world() as w:
        specs = _seed_item4_livetest_patterns(w, run_id)
        sw_spec = next(s for s in specs if s["suffix"] == "sw")

        manifest = _fresh_manifest45(run_id)
        manifest["item4_patterns"].append({
            "pattern_id": sw_spec["pattern_id"],
            "fm_id": sw_spec["fm_id"],
            "vins": sw_spec["vins"],
            "sha256": sw_spec["sha256"],
            "livetest_run_id": run_id,
        })

        # Run cleanup normally (actually deletes the pattern row)
        res_cleanup = lv.Results()
        import boto3  # noqa: PLC0415
        idp = boto3.client("cognito-idp", region_name=REGION)
        pool_id_val = None
        try:
            pool_id_val = lv._resolve_pool_id(idp)
        except Exception:
            pass
        lv._after6_cleanup(
            res_cleanup,
            manifest=manifest,
            manifest_dir=str(tmp_path),
            stage=STAGE,
            idp=idp,
            pool_id=pool_id_val,
        )

        # Simulate "delete succeeded but row is back" — re-insert the pattern row
        ddb = boto3.resource("dynamodb", region_name=ADP_REGION)
        ddb.Table(f"adp-{STAGE}-fault-patterns").put_item(Item={
            "pattern_id": sw_spec["pattern_id"],
            "pattern_group_id": sw_spec["pattern_id"],
            "code_family": "P0LT",
            "livetest_run_id": run_id,
            "affected_vins": sw_spec["vins"],
            "affected_vins_count": len(sw_spec["vins"]),
            "vehicles_affected": len(sw_spec["vins"]),
            "affected_vins_sha256": sw_spec["sha256"],
        })

        # Residue scan should catch the leftover
        res_residue = lv.Results()
        lv._after6_residue_scan(res_residue, manifest=manifest, stage=STAGE)

    cleanup_fails = [r for r in res_cleanup.rows if r[1] == "FAIL"]
    residue_fails = [r for r in res_residue.rows if r[1] == "FAIL"]

    # Cleanup should PASS (the delete actually worked)
    assert not cleanup_fails, (
        f"Cleanup should PASS (row was deleted). cleanup_rows={res_cleanup.rows}"
    )
    # Residue should FAIL (the row was re-inserted to simulate "came back after delete")
    assert residue_fails, (
        f"Residue scan should catch the leftover row. "
        f"residue_rows={res_residue.rows}"
    )


@pytest.mark.skipif(not _REPOS_PRESENT, reason=_SKIP_REASON)
def test_s21_unk_thread_row_found_in_residue(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Unk thread row planted for item4_unk_thread_ids → residue scan finds it → FAIL.

    S21 (unk-residue-off): the mutant sets 'if False:' instead of 'if _unk_rows:' in
    _after6_residue_scan, so the unk thread query result is ignored and the residue
    never reports the leftover row.  This test catches the mutant.

    Setup: seed a row in adp-staging-digital-thread with the unk_tid, put the tid in
    manifest['item4_unk_thread_ids'], run the residue scan, assert FAIL.
    """
    _patch_guards(monkeypatch)
    run_id = _VALID_RUN_ID

    with _open_world() as w:
        # Seed a thread row for the unk id in the ADP digital-thread table
        unk_tid = f"unk-s21-test-{run_id[-8:]}"
        ddb = __import__("boto3").resource("dynamodb", region_name=ADP_REGION)
        ddb.Table(f"adp-{STAGE}-digital-thread").put_item(Item={
            "thread_id": unk_tid,
            "version": 1,
            "status": "decided",
        })

        manifest = _fresh_manifest45(run_id)
        manifest["item4_unk_thread_ids"] = [unk_tid]

        res_residue = lv.Results()
        lv._after6_residue_scan(res_residue, manifest=manifest, stage=STAGE)

    fail_rows = [r for r in res_residue.rows if r[1] == "FAIL"]
    unk_fail = [r for r in fail_rows if f"unk-thread:{unk_tid[:8]}" in r[2]]
    assert unk_fail, (
        f"residue scan must FAIL when unk thread row is present; "
        f"fail_rows={fail_rows}, all_rows={res_residue.rows}"
    )



# ===========================================================================
# Mutant verification tests for s2b — simulations deleted (W12 fix: review-64-cycle1 C6)
# Real mutant tests belong in 64-records/fix1-mutants.md and use actual mutated script copies
# ===========================================================================


# ===========================================================================
# Part B: fix-round-1 — mutant-catching tests for M13, M14, M18
# ===========================================================================


@pytest.mark.skipif(not _REPOS_PRESENT, reason=_SKIP_REASON)
def test_w4h_sw_thread_not_decided_fails(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """M13 catch: item 4 must FAIL when thread status != 'decided'.

    Seeds a thread row with status='pending_execution' (not 'decided').
    Intercepts approve call to return a target_ref pointing at that thread.
    With the fix: item-4-sw-thread-status → FAIL.
    With M13 mutant (status check always True): PASS → test fails → mutant caught.
    """
    _patch_guards(monkeypatch)
    run_id = _VALID_RUN_ID

    with _open_world() as w:
        specs = _seed_item4_livetest_patterns(w, run_id)
        sw_spec = next(s for s in specs if s["suffix"] == "sw")
        finding = _build_item4_finding(
            pattern_id=sw_spec["pattern_id"],
            fm_id=sw_spec["fm_id"],
            option_id=sw_spec["option_id"],
            remedy_type="software_update",
            vins=sw_spec["vins"],
            run_id=run_id,
        )
        finding_id = _write_avx_finding(w, finding)

        # Pre-seed a thread row with status='pending_execution' (not 'decided')
        import boto3  # noqa: PLC0415
        ddb = boto3.resource("dynamodb", region_name=ADP_REGION)
        wrong_thread_id = f"wrong-thread-m13-{run_id[-8:]}"
        ddb.Table(f"adp-{STAGE}-digital-thread").put_item(Item={
            "thread_id": wrong_thread_id,
            "version": 1,
            "status": "pending_execution",  # NOT 'decided'
            "pattern_group_id": sw_spec["pattern_id"],
            "remedy_type": "software_update",
        })

        # Intercept _item_6_http: for approve, return action with target_ref=wrong_thread_id
        orig_http = lv._item_6_http

        def _patched_http(url: str, method: str, path: str, token: str,
                          body: dict | None = None) -> tuple:
            if method == "POST" and "/approve" in path and "findings" in path:
                return 200, {
                    "action_id": f"action-m13-{uuid.uuid4().hex[:8]}",
                    "status": "executed",
                    "target_ref": wrong_thread_id,
                }
            return orig_http(url, method, path, token, body)

        monkeypatch.setattr(lv, "_item_6_http", _patched_http)

        manifest = _fresh_manifest45(run_id)
        manifest["item4_patterns"].append({
            "pattern_id": sw_spec["pattern_id"],
            "fm_id": sw_spec["fm_id"],
            "vins": sw_spec["vins"],
            "sha256": sw_spec["sha256"],
            "livetest_run_id": run_id,
        })
        manifest["item4_findings"].append({
            "finding_id": finding_id,
            "finding_kind": "quality.investigation",
            "pattern_id": sw_spec["pattern_id"],
        })

        old_int = signal.getsignal(signal.SIGINT)
        old_term = signal.getsignal(signal.SIGTERM)
        try:
            res = lv.Results()
            lv._item_4(
                res,
                stage=STAGE,
                run_id=run_id,
                manifest=manifest,
                manifest_dir=str(tmp_path),
                allow_writes=True,
            )
        finally:
            signal.signal(signal.SIGINT, old_int)
            signal.signal(signal.SIGTERM, old_term)

    fail_labels = {r[0] for r in res.rows if r[1] == "FAIL"}
    assert "item-4-sw-thread-status" in fail_labels, (
        f"M13: item-4-sw-thread-status must FAIL when thread status is 'pending_execution'. "
        f"rows={res.rows}"
    )


@pytest.mark.skipif(not _REPOS_PRESENT, reason=_SKIP_REASON)
def test_w5_wrong_thread_id_in_finding_fails(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """M14 catch: item 5 must FAIL when quality.thread evidence has wrong thread_id.

    Uses the scan path (no item4_issue_finding_id in manifest) so M14 is exercised.
    Seeds a quality.issue Finding whose quality.thread.thread_id is NOT in sw_thread_ids.
    With the fix: scan can't match → FAIL on item-5-issue-finding-present.
    With M14 mutant (thread_id check dropped): scan matches → PASS → test fails.
    Test name contains 'w5' so mutant runner picks it up.
    """
    _patch_guards(monkeypatch)
    run_id = _VALID_RUN_ID

    with _open_world() as w:
        _seed_base_world(w)

        # The sw_thread_id that item 4 "created" — NOT the thread_id in the wrong Finding
        correct_sw_thread_id = f"correct-sw-thread-m14-{run_id[-8:]}"

        # Seed a quality.issue Finding with WRONG quality.thread thread_id in the scan path.
        wrong_thread_id = "wrong-thread-m14-definitely-different"
        issue_finding_wrong = {
            "finding_id": f"issue-finding-m14-wrong-{run_id[-8:]}",
            "finding_kind": "quality.issue",
            "investigation_id": "inv-m14-scan-test",
            "evidence": [
                {
                    "kind": "quality.thread",
                    "value": {"thread_id": wrong_thread_id},  # WRONG — not in sw_thread_ids
                },
                {
                    "kind": "quality.draft",
                    "value": {"draft_id": "MRD-SW-2026-M14WRONG"},
                },
            ],
            "recommended_action": {
                "action_kind": "issue_software_campaign",
                "parameters": {
                    "thread_id": wrong_thread_id,
                    "draft_id": "MRD-SW-2026-M14WRONG",
                    "software_campaign_id": "MRD-SW-2026-M14WRONG",
                },
            },
        }
        w.table(f"vsa-{STAGE}-avx-findings").put_item(Item=issue_finding_wrong)

        # Manifest: sw_thread_ids = [correct_sw_thread_id] — does NOT match wrong_thread_id
        # Do NOT set item4_issue_finding_id — force the scan path
        # No real quality.issue Finding with correct_sw_thread_id exists in the table
        manifest = _fresh_manifest45(run_id)
        manifest.setdefault("item4_thread_versions", []).append({
            "thread_id": correct_sw_thread_id, "version": 1,
            "action_id": "action-m14-fake",
        })
        # Do NOT set manifest["item4_issue_finding_id"]

        old_int = signal.getsignal(signal.SIGINT)
        old_term = signal.getsignal(signal.SIGTERM)
        try:
            res = lv.Results()
            lv._item_5(
                res,
                stage=STAGE,
                run_id=run_id,
                manifest=manifest,
                manifest_dir=str(tmp_path),
                allow_writes=True,
            )
        finally:
            signal.signal(signal.SIGINT, old_int)
            signal.signal(signal.SIGTERM, old_term)

    fail_labels = {r[0] for r in res.rows if r[1] == "FAIL"}
    # M14: should FAIL on issue-finding-present because the scan can't match the wrong thread_id
    # With M14 mutant (thread_id check dropped): the wrong finding IS matched → no FAIL here
    assert "item-5-issue-finding-present" in fail_labels, (
        f"M14: item-5-issue-finding-present must FAIL when scan's quality.thread "
        f"thread_id doesn't match sw_thread_ids. "
        f"rows={res.rows}"
    )


@pytest.mark.skipif(not _REPOS_PRESENT, reason=_SKIP_REASON)
def test_w4h_window_closes_between_approvals(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """M18 catch: item 4 must FAIL when write window closes between approvals.

    Monkeypatches _after6_write_window_guard to return a reason after the first call.
    With the fix (per-approve re-check): the second approval FAILs on window check.
    With M18 mutant (re-check always None): proceeds to second approval → no FAIL.
    Test name contains 'w4f' so mutant runner picks it up with 'w4h or w4f_m9'.
    """
    _patch_guards(monkeypatch)
    run_id = _VALID_RUN_ID

    call_count = [0]
    # First call (item-level guard) passes; subsequent calls (per-approve) fail closed
    def _window_guard_with_closure(*args: object, **kwargs: object) -> str | None:
        call_count[0] += 1
        if call_count[0] > 1:
            return "window closed mid-run (M18 test)"
        return None

    monkeypatch.setattr(lv, "_after6_write_window_guard", _window_guard_with_closure)

    with _open_world() as w:
        specs = _seed_item4_livetest_patterns(w, run_id)
        sw_spec = next(s for s in specs if s["suffix"] == "sw")
        finding = _build_item4_finding(
            pattern_id=sw_spec["pattern_id"],
            fm_id=sw_spec["fm_id"],
            option_id=sw_spec["option_id"],
            remedy_type="software_update",
            vins=sw_spec["vins"],
            run_id=run_id,
        )
        finding_id = _write_avx_finding(w, finding)

        manifest = _fresh_manifest45(run_id)
        manifest["item4_patterns"].append({
            "pattern_id": sw_spec["pattern_id"],
            "fm_id": sw_spec["fm_id"],
            "vins": sw_spec["vins"],
            "sha256": sw_spec["sha256"],
            "livetest_run_id": run_id,
        })
        manifest["item4_findings"].append({
            "finding_id": finding_id,
            "finding_kind": "quality.investigation",
            "pattern_id": sw_spec["pattern_id"],
        })

        old_int = signal.getsignal(signal.SIGINT)
        old_term = signal.getsignal(signal.SIGTERM)
        try:
            res = lv.Results()
            lv._item_4(
                res,
                stage=STAGE,
                run_id=run_id,
                manifest=manifest,
                manifest_dir=str(tmp_path),
                allow_writes=True,
            )
        finally:
            signal.signal(signal.SIGINT, old_int)
            signal.signal(signal.SIGTERM, old_term)

    fail_labels = {r[0] for r in res.rows if r[1] == "FAIL"}
    # With M18 mutant (no per-approve check): call_count > 1 never triggers window close
    # With the fix: second approval fails on window check
    assert any("window" in lbl.lower() for lbl in fail_labels), (
        f"M18: item-4 must FAIL when write window closes between approvals. "
        f"Expected a 'window' FAIL. rows={res.rows}"
    )


# ===========================================================================
# W6 fix-round-1-C: item 5 Finding lookup via GSI (not scan)
# ===========================================================================

@pytest.mark.skipif(not _REPOS_PRESENT, reason=_SKIP_REASON)
def test_w6c_finding_beyond_scan_page_found_via_gsi(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """item 5 must find the quality.issue Finding via GSI even if scan returns nothing.

    The live findings table is 214 MB; a one-page scan returns at most 1 MB.
    This test seeds the world with the Finding, then patches the table's scan
    to return empty Items (simulating the Finding sitting beyond page 1 of a scan).
    W6 fix: the code uses the by_finding_key GSI instead of scan.
    Mutant M30: GSI query removed / scan path restored → this test would pass (CAUGHT).
    RED: old code uses scan → sees nothing → item-5-issue-finding-present SKIP/FAIL.
    """
    _patch_guards(monkeypatch)
    run_id = _VALID_RUN_ID + "w6"

    with _open_world() as w:
        specs = _seed_item4_livetest_patterns(w, run_id)
        sw_spec = next(s for s in specs if s["suffix"] == "sw")
        sw_finding = _build_item4_finding(
            pattern_id=sw_spec["pattern_id"],
            fm_id=sw_spec["fm_id"],
            option_id=sw_spec["option_id"],
            remedy_type=sw_spec["remedy_type"],
            vins=sw_spec["vins"],
            run_id=run_id,
        )
        sw_finding_id = _write_avx_finding(w, sw_finding)
        action = _approve_via_world(w, finding_id=sw_finding_id,
                                    option_id=sw_spec["option_id"])
        assert action.get("status") == "executed", (
            f"sw approve must execute. action={action!r:.200}"
        )
        thread_id = action.get("target_ref", "")

        # Patch findings table scan to return empty Items (simulates > 1 MB offset).
        import boto3 as _b3  # noqa: PLC0415
        findings_tbl_real = _b3.resource("dynamodb", region_name=REGION).Table(
            f"vsa-{STAGE}-avx-findings"
        )

        def _empty_scan(**_kw: Any) -> dict:
            """Simulate the Finding sitting beyond the first scan page."""
            return {"Items": [], "Count": 0}

        monkeypatch.setattr(findings_tbl_real, "scan", _empty_scan)

        # Manifest has thread_id but NO item4_issue_finding_id (forces fallback path)
        manifest = _fresh_manifest45(run_id)
        manifest["item4_thread_versions"].append({
            "thread_id": thread_id,
            "version": 1,
            "action_id": action.get("action_id"),
            "finding_id": sw_finding_id,
        })

        old_int = signal.getsignal(signal.SIGINT)
        old_term = signal.getsignal(signal.SIGTERM)
        try:
            res = lv.Results()
            lv._item_5(
                res,
                stage=STAGE,
                run_id=run_id,
                manifest=manifest,
                manifest_dir=str(tmp_path),
                allow_writes=True,
            )
        finally:
            signal.signal(signal.SIGINT, old_int)
            signal.signal(signal.SIGTERM, old_term)

    # item-5-issue-finding-present must PASS (GSI found it despite empty scan)
    pass_labels = {r[0] for r in res.rows if r[1] == "PASS"}
    skip_labels = {r[0] for r in res.rows if r[1] == "SKIP"}
    assert "item-5-issue-finding-present" in pass_labels, (
        f"W6: item-5 must find quality.issue Finding via GSI even with empty scan. "
        f"PASS={pass_labels} SKIP={skip_labels} rows={res.rows}"
    )

# ---------------------------------------------------------------------------
# T35b: pin _derive_thread_id against ADP 02b8716f digital_thread_writer/handler.py
# (mirrors test_live_verify_fi_cms_maker_m1.py T35; verifies the quality script's
# copy matches the deployed ADP function — added in 6.5 stage 1).
# ---------------------------------------------------------------------------

_ADP_DTW_PATH = "platform-foundation/source/lambda/digital_thread_writer/handler.py"
_ADP_DTW_COMMIT = "02b8716f"


def test_pin_thread_id_against_adp_dtw() -> None:
    """_derive_thread_id in live-verify-fi-quality.py must match ADP 02b8716f dtw handler.

    T35b: executes ADP's _derive_thread_id and verifies our copy produces identical output.
    """
    import ast  # noqa: PLC0415
    import subprocess as _sp  # noqa: PLC0415
    import hashlib as _hl  # noqa: PLC0415

    proc = _sp.run(
        ["git", "-C", str(_ADP_REPO), "show",
         f"{_ADP_DTW_COMMIT}:{_ADP_DTW_PATH}"],
        capture_output=True, timeout=30, check=False,
    )
    if proc.returncode != 0:
        pytest.skip(f"ADP git show failed (repo absent?): {proc.stderr.decode()[:200]}")
    src = proc.stdout.decode("utf-8", errors="replace")
    tree = ast.parse(src)
    adp_func_node = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_derive_thread_id":
            adp_func_node = node
            break
    assert adp_func_node is not None, "_derive_thread_id not found in ADP dtw handler.py"
    adp_module_ast = ast.Module(body=[adp_func_node], type_ignores=[])
    ast.fix_missing_locations(adp_module_ast)
    adp_ns: dict = {"hashlib": _hl, "__name__": "__adp_pin__"}
    exec(  # noqa: S102
        compile(adp_module_ast, "<adp_dtw_pin_quality>", "exec"),
        adp_ns,
    )
    adp_fn = adp_ns["_derive_thread_id"]
    # Verify our script's copy matches on a test vector.
    _pat = "LIVETEST-4-fi-quality-20261028T223331-264a92a2-unk"
    _ifid = "00000000-0000-0000-0000-000000000099"
    adp_out = adp_fn(_pat, _ifid)
    our_out = lv._derive_thread_id(_pat, _ifid)
    assert our_out == adp_out, (
        f"_derive_thread_id mismatch:\n  script={our_out!r}\n  ADP={adp_out!r}"
    )
