"""Tests for _item_2 (fleet-health items in correct fleet queues) in
live-verify-fi-fleet-health.py.

Findings built through the real writer functions (agents/fi_fleet_health/write.py)
and a catalog parquet with the real column shape.  Each bad world fails on the
item's own check; a positive control passes.

Pinned DDB/S3 ops for item 2:
  dynamodb.Query    (by_fleet GSI on avx-findings; vin-index on storage-vehicles)
  dynamodb.GetItem  (storage-vehicles by vehicleId)
  s3.GetObject      (ADP lake fault_codes.parquet)

Run (AWS env stripped):
    env -u AWS_PROFILE AWS_ACCESS_KEY_ID=testing AWS_SECRET_ACCESS_KEY=testing \\
      PYTHONDONTWRITEBYTECODE=1 \\
      .venv/bin/python -m pytest scripts/tests/test_live_verify_fi_fleet_health_item2.py -q
"""
from __future__ import annotations

import datetime as dt
import importlib.util
import os
import tempfile
import types
import unittest.mock as mock
from pathlib import Path
from typing import Any

import duckdb
import pytest

# ---------------------------------------------------------------------------
# Load module under test
# ---------------------------------------------------------------------------

_SCRIPT = Path(__file__).resolve().parents[1] / "live-verify-fi-fleet-health.py"
_spec = importlib.util.spec_from_file_location("live_verify_fi_fleet_health", _SCRIPT)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

Results            = _mod.Results
_item_2            = _mod._item_2
_needs_from_evidence = _mod._needs_from_evidence
_load_fault_catalog_s3 = _mod._load_fault_catalog_s3
_FH_RUN_PK         = _mod._FH_RUN_PK

# ---------------------------------------------------------------------------
# Pin _now for the entire module so run records dated 2026-10-17T05:40Z stay
# within the 26 h window regardless of when the test suite is executed.
# ---------------------------------------------------------------------------
_PINNED_NOW = dt.datetime(2026, 10, 17, 7, 0, tzinfo=dt.timezone.utc)


@pytest.fixture(autouse=True)
def _pin_now(monkeypatch):
    monkeypatch.setattr(_mod, "_now", lambda: _PINNED_NOW)


# ---------------------------------------------------------------------------
# Catalog fixture helpers
# ---------------------------------------------------------------------------

_P0_CODE = "P0AA6"
_SOON_CODE = "P0171"
_P0_DIRECTIVE = "Stop driving safely now. Do not restart the vehicle."


def _make_catalog_parquet(rows: list[dict]) -> bytes:
    """Serialise a list of {code, band, directive_text} dicts to parquet bytes.

    Column shape matches fault_codes.yaml → fault_codes.parquet loader output
    (tasks.md G51.5; catalog columns: code, band, directive_text).
    Uses duckdb (available in the CVX dev venv).
    """
    conn = duckdb.connect()
    conn.execute(
        "CREATE OR REPLACE TABLE _catalog(code VARCHAR, band VARCHAR, directive_text VARCHAR)"
    )
    for r in rows:
        code = r.get("code") or ""
        band = r.get("band") or ""
        dt   = r.get("directive_text")
        if dt is None:
            conn.execute(
                "INSERT INTO _catalog VALUES (?, ?, NULL)", [code, band]
            )
        else:
            conn.execute(
                "INSERT INTO _catalog VALUES (?, ?, ?)", [code, band, dt]
            )
    with tempfile.NamedTemporaryFile(suffix=".parquet", delete=False) as tmp:
        tmp_path = tmp.name
    try:
        conn.execute(f"COPY _catalog TO '{tmp_path}' (FORMAT PARQUET)")
        with open(tmp_path, "rb") as fh:
            return fh.read()
    finally:
        os.unlink(tmp_path)


_CATALOG_ROWS = [
    {"code": _P0_CODE,   "band": "p0",   "directive_text": _P0_DIRECTIVE},
    {"code": _SOON_CODE, "band": "soon",  "directive_text": None},
]


# ---------------------------------------------------------------------------
# Finding fixture helpers (real writer functions cited)
# ---------------------------------------------------------------------------

def _make_finding(
    *,
    vin: str = "VIN0000000000P0001",
    fleet_id: str = "demo-rental-carshare",
    severity: str = "p0",
    disclosure_txt: str | None = _P0_DIRECTIVE,
    computed_at: str = "2026-10-17T05:45:00Z",  # after 05:40Z service run start
    p0_band: bool = True,
) -> dict:
    """Build a minimal fleet.health.service Finding.

    Uses agents/fi_fleet_health/write.py:build_finding (line 55) shape:
      finding_id, agent_id, finding_kind, scope, severity, computed_at,
      fleet_id (top-level, set by tier2/writer.py standing projection),
      evidence containing health.need entries (loop.py:_build_raw_evidence:638).
    """
    # Evidence need for a single fault (bands.py:classify_needs:128-145)
    fault_need_value = {
        "kind": "fault",
        "need_id": f"fault:{_P0_CODE}",
        "code": _P0_CODE,
        "severity_band": "p0" if p0_band else "soon",
        "source": "vehicle_fault_status",
    }
    evidence = [
        {
            "kind": "health.need",
            "value": fault_need_value,
            "vin": vin,
            "provenance": "live",
            "source": "adp_fleet_health",
            "source_ref": "vehicle_fault_status_daily",
            "computed_at": computed_at,
        }
    ]
    finding: dict = {
        "finding_id": f"FIND-{vin}-test",
        "agent_id": "agent-fi-fleet-health",
        "finding_kind": "fleet.health.service",
        "scope": {"vin": vin},
        "fleet_id": fleet_id,
        "severity": severity,
        "computed_at": computed_at,
        "expires_at": "2026-10-20T05:45:00Z",
        "confidence": "low",
        "evidence": evidence,
        "tokens_used": 0,
        "inputs_hash": "a" * 64,
        "subject_key": "service/1",
        "action_kind": "book_fleet_service",  # loop.py:1430 (book_fleet_service for faults+pm)
    }
    if disclosure_txt is not None:
        finding["disclosure_txt"] = disclosure_txt
    return finding


# ---------------------------------------------------------------------------
# World builder — fakes for DDB (low-level) + S3 + runs table
# ---------------------------------------------------------------------------

_STAGE = "staging"
_REGION = "us-west-2"
_ACCOUNT = "123456789012"
_FLEET = "demo-rental-carshare"
_VIN = "VIN0000000000P0001"
_VEHICLE_ID = "VEH-TEST-001"
_STARTED_AT = "2026-10-17T05:40:00Z"  # service schedule: vsa-tier2-stack.ts:2003


def _make_world(
    findings: list[dict] | None = None,
    cms_fleet: str | None = _FLEET,
    catalog_rows: list[dict] | None = None,
    run_started_at: str = _STARTED_AT,
    no_run: bool = False,
) -> dict[str, Any]:
    """Build fake DDB + S3 + run state for _item_2.

    Returns a dict with keys ``ddb``, ``s3``, ``args``.
    """
    if findings is None:
        findings = [_make_finding()]
    if catalog_rows is None:
        catalog_rows = _CATALOG_ROWS

    from boto3.dynamodb.types import TypeSerializer  # noqa: PLC0415

    ser = TypeSerializer()

    def _s(raw: dict) -> dict:
        return {k: ser.serialize(v) for k, v in raw.items() if v is not None}

    # --- DDB mock ---
    ddb = mock.MagicMock()

    # runs table → _latest_health_run uses low-level Query
    run_record = {
        "agent_component": "agent-fi-fleet-health#service",
        "started_at": run_started_at,
        "status": "completed",
    }

    def _fake_query(**kw):
        table = kw.get("TableName", "")
        index = kw.get("IndexName", "")

        # runs table
        if "avx-agent-runs" in table:
            if no_run:
                return {"Items": [], "Count": 0}
            return {"Items": [_s(run_record)], "Count": 1}

        # findings by_fleet GSI
        if "avx-findings" in table and index == "by_fleet":
            fleet_val = kw["ExpressionAttributeValues"][":flt"]["S"]
            fleet_findings = [f for f in findings if f.get("fleet_id") == fleet_val]
            return {"Items": [_s(f) for f in fleet_findings], "Count": len(fleet_findings)}

        # cms vehicles vin-index
        if "storage-vehicles" in table and index == "vin-index":
            if cms_fleet is None:
                return {"Items": [], "Count": 0}
            vehicle_row = {"vehicleId": _VEHICLE_ID}
            return {"Items": [_s(vehicle_row)], "Count": 1}

        return {"Items": [], "Count": 0}

    def _fake_get_item(**kw):
        table = kw.get("TableName", "")
        if "storage-vehicles" in table:
            if cms_fleet is None:
                return {"Item": {}}
            item = {"vehicleId": _VEHICLE_ID, "fleetId": cms_fleet}
            return {"Item": _s(item)}
        return {"Item": {}}

    ddb.query.side_effect = _fake_query
    ddb.get_item.side_effect = _fake_get_item

    # --- S3 mock ---
    s3 = mock.MagicMock()
    parquet_bytes = _make_catalog_parquet(catalog_rows)

    class _FakeBody:
        def read(self) -> bytes:
            return parquet_bytes

    s3.get_object.return_value = {"Body": _FakeBody()}

    # --- args ---
    args = types.SimpleNamespace(adp_lake_bucket=None)

    return {"ddb": ddb, "s3": s3, "args": args}


def _run_item2(world: dict[str, Any]) -> Results:
    """Call _item_2 with mocked _client returning the world's DDB/S3 fakes."""
    ddb = world["ddb"]
    s3  = world["s3"]
    args = world["args"]

    call_order: list[str] = []

    def _fake_client(service, region, *, allowed_ops, **_kw):
        call_order.append(service)
        if service == "dynamodb":
            return ddb
        if service == "s3":
            return s3
        raise ValueError(f"unexpected service {service!r}")

    res = Results()
    with mock.patch.object(_mod, "_client", side_effect=_fake_client):
        _item_2(
            res,
            stage=_STAGE,
            region=_REGION,
            account=_ACCOUNT,
            args=args,
        )
    return res


# ===========================================================================
# Unit test: _needs_from_evidence
# ===========================================================================

def test_needs_from_evidence_splits_kinds():
    """_needs_from_evidence splits health.need entries by kind correctly."""
    evidence = [
        {"kind": "health.need", "value": {
            "kind": "fault", "code": "P0AA6", "severity_band": "p0"}},
        {"kind": "health.need", "value": {
            "kind": "agent2_finding", "finding_id": "FID-1",
            "severity": "attention", "severity_band": "attention",
            "finding_kind": "health.battery"}},
        {"kind": "health.need", "value": {
            "kind": "pm_task", "task_code": "TIRE_ROTATION", "status": "due",
            "severity_band": "routine"}},
        {"kind": "health.rooftops", "value": []},  # must be ignored
    ]
    faults, a2, pm = _needs_from_evidence(evidence)
    assert len(faults) == 1 and faults[0]["code"] == "P0AA6"
    assert len(a2) == 1 and a2[0]["severity"] == "attention"
    assert len(pm) == 1 and pm[0]["task_code"] == "TIRE_ROTATION"


# ===========================================================================
# Unit test: _load_fault_catalog_s3
# ===========================================================================

def test_load_fault_catalog_s3_parses_rows():
    """_load_fault_catalog_s3 returns {code: {code, band, directive_text}} from parquet."""
    parquet_bytes = _make_catalog_parquet(_CATALOG_ROWS)

    class _FakeBody:
        def read(self): return parquet_bytes

    s3 = mock.MagicMock()
    s3.get_object.return_value = {"Body": _FakeBody()}
    catalog = _load_fault_catalog_s3(s3, "fake-bucket")
    assert catalog[_P0_CODE]["band"] == "p0"
    assert catalog[_P0_CODE]["directive_text"] == _P0_DIRECTIVE
    assert catalog[_SOON_CODE]["band"] == "soon"


# ===========================================================================
# Positive control: correct finding passes all checks
# ===========================================================================

def test_positive_control_passes():
    """Positive control: one correct p0 finding with correct fleet/severity/disclosure → all PASS."""
    world = _make_world()
    res = _run_item2(world)
    assert not res.failed, f"unexpected failures: {res.failed}"
    # p0-item-present must pass
    statuses = {r[0]: r[1] for r in res.rows}
    assert statuses.get("2-p0-item-present") == "PASS"


# ===========================================================================
# Bad world: wrong fleet_id on the finding
# ===========================================================================

def test_wrong_fleet_id_fails():
    """Finding fleet_id != CMS enrollment fleet → fleet-id check FAIL."""
    # Finding has a managed fleet (so it is discovered via by_fleet query)
    # but CMS enrollment says the VIN belongs to a different fleet.
    finding = _make_finding(fleet_id="demo-rental-carshare")
    world = _make_world(findings=[finding], cms_fleet="demo-lastmile-delivery")
    res = _run_item2(world)
    fail_labels = {r[0] for r in res.failed}
    assert any("fleet-id" in l for l in fail_labels), f"rows={res.rows}"


# ===========================================================================
# Bad world: finding missing from by_fleet GSI
# ===========================================================================

def test_missing_from_by_fleet_gsi_fails():
    """Finding returned under a DIFFERENT fleet's query than its fleet_id → by-fleet-gsi FAIL.

    The by_fleet GSI check (brief (a)) verifies that querying by_fleet for the
    finding's OWN fleet_id returns it.  If the finding was indexed under a
    different fleet key, the GSI is corrupt → FAIL.
    """
    # finding.fleet_id = "demo-lastmile-delivery"
    # but the DDB fake returns it when queried for "demo-rental-carshare"
    # → gsi_members has ("demo-rental-carshare", fid)
    # → check ("demo-lastmile-delivery", fid) in gsi_members → False → FAIL
    finding = _make_finding(fleet_id="demo-lastmile-delivery")
    world = _make_world(findings=[], cms_fleet="demo-lastmile-delivery")

    orig_query = world["ddb"].query.side_effect

    def _return_under_wrong_fleet(**kw):
        idx = kw.get("IndexName", "")
        if idx == "by_fleet":
            fleet_val = kw["ExpressionAttributeValues"][":flt"]["S"]
            # return the finding only when querying demo-rental-carshare (wrong fleet)
            if fleet_val == "demo-rental-carshare":
                from boto3.dynamodb.types import TypeSerializer  # noqa: PLC0415
                ser = TypeSerializer()
                return {"Items": [{k: ser.serialize(v) for k, v in finding.items()
                                   if v is not None}], "Count": 1}
            return {"Items": [], "Count": 0}
        return orig_query(**kw)

    world["ddb"].query.side_effect = _return_under_wrong_fleet
    res = _run_item2(world)
    fail_labels = {r[0] for r in res.failed}
    assert any("by-fleet-gsi" in l for l in fail_labels), f"rows={res.rows}"


# ===========================================================================
# Bad world: severity one band off
# ===========================================================================

def test_severity_one_band_off_fails():
    """Finding severity=soon but needs give p0 → severity check FAIL."""
    finding = _make_finding(severity="soon", disclosure_txt=None)
    # p0 fault in evidence → classify_needs returns p0 → mismatch with soon
    world = _make_world(findings=[finding])
    res = _run_item2(world)
    fail_labels = {r[0] for r in res.failed}
    assert any("severity" in l for l in fail_labels), f"rows={res.rows}"


# ===========================================================================
# Bad world: disclosure_txt differs by trailing space (byte mismatch)
# ===========================================================================

def test_disclosure_trailing_space_fails():
    """disclosure_txt with trailing space != catalog directive → p0-disclosure FAIL.

    writer.py:build_disclosure_txt:64 returns directive byte for byte; a trailing
    space is a mutation that must fail (brief spec).
    """
    finding = _make_finding(disclosure_txt=_P0_DIRECTIVE + " ")  # trailing space
    world = _make_world(findings=[finding])
    res = _run_item2(world)
    fail_labels = {r[0] for r in res.failed}
    assert any("p0-disclosure" in l for l in fail_labels), f"rows={res.rows}"


# ===========================================================================
# Bad world: fault band not matching catalog band
# ===========================================================================

def test_fault_band_not_matching_catalog_fails():
    """Fault need band=soon but catalog says p0 → fault-band check FAIL."""
    # Build a finding whose evidence has P0AA6 with band=soon (wrong)
    finding = _make_finding(p0_band=False)  # severity_band=soon for P0AA6
    # Catalog still says P0AA6 → p0
    world = _make_world(findings=[finding], catalog_rows=_CATALOG_ROWS)
    res = _run_item2(world)
    fail_labels = {r[0] for r in res.failed}
    assert any("band" in l for l in fail_labels), f"rows={res.rows}"


# ===========================================================================
# No run → SKIP
# ===========================================================================

def test_no_service_run_skips():
    """No completed service run within last 26 h → item-2 SKIP."""
    world = _make_world(no_run=True)
    res = _run_item2(world)
    skip_labels = {r[0] for r in res.rows if r[1] == "SKIP"}
    assert "item-2" in skip_labels, f"rows={res.rows}"
    assert not res.failed


# ===========================================================================
# Zero findings → SKIP
# ===========================================================================

def test_zero_findings_skips():
    """No fleet.health.service findings in any managed fleet → item-2 SKIP."""
    world = _make_world(findings=[])
    res = _run_item2(world)
    skip_labels = {r[0] for r in res.rows if r[1] == "SKIP"}
    assert "item-2" in skip_labels, f"rows={res.rows}"



# ===========================================================================
# _latest_service_run — service component runs at 05:40Z (not 02:00-03:30 window)
# ===========================================================================

def test_service_run_at_0540_found():
    """_latest_service_run returns a completed run starting at 05:40Z (service schedule).

    Service schedule: vsa-tier2-stack.ts:2003 (hour '5', minute '40').
    Unlike Agent-2 health runs (02:00-03:30), the service component fires at 05:40Z.
    """
    from boto3.dynamodb.types import TypeSerializer  # noqa: PLC0415

    ser = TypeSerializer()

    def _s(d: dict) -> dict:
        return {k: ser.serialize(v) for k, v in d.items()}

    run_rec = {
        "agent_component": _FH_RUN_PK,
        "started_at": "2026-10-17T05:40:00Z",
        "status": "completed",
    }
    ddb = mock.MagicMock()
    ddb.query.return_value = {"Items": [_s(run_rec)], "Count": 1}

    # _now returns 07:00Z same day; 05:40Z is well within the 26 h cutoff.
    _mock_now = lambda: dt.datetime(2026, 10, 17, 7, 0, tzinfo=dt.timezone.utc)
    with mock.patch.object(_mod, "_now", _mock_now):
        result = _mod._latest_service_run(ddb, "vsa-staging-avx-agent-runs")

    assert result is not None, "05:40Z run should be found by _latest_service_run"
    assert result["started_at"] == "2026-10-17T05:40:00Z"


def test_item2_e2e_0540_start_passes():
    """End-to-end: _item_2 finds and validates a service run that starts at 05:40Z."""
    world = _make_world(run_started_at="2026-10-17T05:40:00Z")
    res = _run_item2(world)
    assert not res.failed, f"unexpected failures with 05:40Z run: {res.failed}"
    statuses = {r[0]: r[1] for r in res.rows}
    assert statuses.get("2-p0-item-present") == "PASS"



# ===========================================================================
# Warning [item 2]: directive compared with first p0 code in evidence order
# ===========================================================================

_P0_CODE2       = "P0ZZZ"        # alphabetically AFTER P0AA6
_P0_DIRECTIVE2  = "Pull over immediately. Turn off the ignition."

_CATALOG_ROWS_TWO_P0 = [
    {"code": _P0_CODE2, "band": "p0",   "directive_text": _P0_DIRECTIVE2},  # newer
    {"code": _P0_CODE,  "band": "p0",   "directive_text": _P0_DIRECTIVE},   # older
    {"code": _SOON_CODE, "band": "soon", "directive_text": None},
]


def _make_finding_two_p0(vin: str = _VIN) -> dict:
    """Finding with two p0 fault needs: P0ZZZ (newer last_seen_at, 2nd in evidence)
    and P0AA6 (older last_seen_at, 1st in evidence).

    The booking builder picks the LATEST last_seen_at (loop.py:1409-1414), so P0ZZZ
    drives the disclosure.  disclosure_txt is set to P0ZZZ's directive.

    Fault need shape: selection.py:499,531 (code, band, last_seen_at).
    """

    def _fault_ev(code: str, band: str, last_seen_at: str) -> dict:
        return {
            "kind": "health.need",
            "value": {
                "kind": "fault",
                "need_id": f"fault:{code}",
                "code": code,
                "severity_band": band,
                "source": "vehicle_fault_status",
                "last_seen_at": last_seen_at,
            },
            "vin": vin,
            "provenance": "live",
            "computed_at": "2026-10-17T05:45:00Z",
        }

    evidence = [
        _fault_ev(_P0_CODE,  "p0", "2026-10-10T00:00:00Z"),  # older — 1st in evidence
        _fault_ev(_P0_CODE2, "p0", "2026-10-16T00:00:00Z"),  # newer — 2nd in evidence
    ]
    finding: dict = {
        "finding_id":   f"FIND-{vin}-2p0",
        "agent_id":     "agent-fi-fleet-health",
        "finding_kind": "fleet.health.service",
        "scope":        {"vin": vin},
        "fleet_id":     _FLEET,
        "severity":     "p0",
        "computed_at":  "2026-10-17T05:45:00Z",
        "expires_at":   "2026-10-20T05:45:00Z",
        "confidence":   "low",
        "evidence":     evidence,
        "tokens_used":  0,
        "inputs_hash":  "b" * 64,
        "subject_key":  "service/1",
        "action_kind":  "book_fleet_service",
        "disclosure_txt": _P0_DIRECTIVE2,   # P0ZZZ's directive (newer last_seen_at)
    }
    return finding


def test_two_p0_codes_picks_by_last_seen_at():
    """p0 disclosure uses the fault with latest last_seen_at, not evidence order.

    P0AA6 is 1st in evidence (older last_seen_at); P0ZZZ is 2nd (newer).
    disclosure_txt = P0ZZZ's directive.
    Correct sort (last_seen_at DESC): picks P0ZZZ → comparison passes.
    Evidence order / code-alpha sort: picks P0AA6 → mismatch → p0-disclosure FAIL.
    """
    world = _make_world(
        findings=[_make_finding_two_p0()],
        catalog_rows=_CATALOG_ROWS_TWO_P0,
    )
    res = _run_item2(world)
    assert not res.failed, (
        f"two p0 codes: expected PASS for P0ZZZ (newer last_seen_at) but got failures: {res.failed}"
    )



# ===========================================================================
# Critical [item 2]: p0 directive check fails open
# ===========================================================================

def test_p0_booking_no_disclosure_txt_alongside_correct_fails():
    """p0 Finding missing disclosure_txt alongside a correct one → p0-disclosure FAIL.

    A p0 booking with no disclosure_txt must FAIL, not be silently skipped.
    The correct finding alongside it must not make the bad one pass.
    (review C2: 'p0 Finding with no disclosure_txt is skipped silently')
    """
    correct = _make_finding()  # has disclosure_txt
    # build a second p0 finding that is missing disclosure_txt
    bad = {k: v for k, v in correct.items() if k != "disclosure_txt"}
    bad["finding_id"] = "FIND-BAD-no-disclosure"
    bad["scope"] = {"vin": "VIN0000000000P0002"}

    world = _make_world(findings=[correct, bad])
    res = _run_item2(world)
    fail_labels = {r[0] for r in res.failed}
    assert any("p0-disclosure" in l for l in fail_labels), (
        f"expected p0-disclosure FAIL for finding with no disclosure_txt; rows={res.rows}"
    )


def test_p0_no_p0_fault_in_evidence_fails():
    """p0 Finding with disclosure_txt but no p0-banded fault in evidence → p0-disclosure FAIL.

    p0_checked must not increment without a real byte comparison.
    (review C2: 'p0_checked is incremented before anything is compared')
    """
    # Make a finding with severity=p0 but evidence fault is "soon" not "p0"
    finding = _make_finding(severity="p0", p0_band=False, disclosure_txt=_P0_DIRECTIVE)
    # p0_band=False gives severity_band="soon", so no p0 fault in open_faults
    world = _make_world(findings=[finding])
    res = _run_item2(world)
    fail_labels = {r[0] for r in res.failed}
    assert any("p0-disclosure" in l for l in fail_labels), (
        f"expected p0-disclosure FAIL when no p0 fault in evidence; rows={res.rows}"
    )


def test_item2_no_p0_findings_fails_p0_item_present():
    """Run wrote Findings but none have severity==p0 → 2-p0-item-present FAIL.

    Catches mutant m8-p0-present: `p0_checked > 0` → `True`.
    When every Finding is severity="attention" (not "p0"), the p0 branch is
    never entered and p0_checked stays 0.  The check must FAIL, not PASS.
    """
    # severity="attention" → action_kind branch not entered → p0_checked stays 0
    finding = _make_finding(severity="attention", disclosure_txt=None)
    world = _make_world(findings=[finding])
    res = _run_item2(world)
    statuses = {r[0]: r[1] for r in res.rows}
    assert statuses.get("2-p0-item-present") == "FAIL", (
        f"2-p0-item-present should be FAIL when no p0 findings exist; rows={res.rows}"
    )
