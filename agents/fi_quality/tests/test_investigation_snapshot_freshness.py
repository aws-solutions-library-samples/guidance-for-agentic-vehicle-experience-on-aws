"""Tests for G3F6F1.5 Part 2A: snapshot-freshness guard in run_investigation.

Coverage:
  A. Stale managed_vehicles/vehicle_fault_status_daily → status=failed,
     no Finding written, read_controls and read_co_occurring_codes never called.
  B. Fresh dates → investigation proceeds; both readers called.
"""
from __future__ import annotations

import datetime
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from agents.tier2.snapshot_freshness import SnapshotState

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_STAGE = "staging"
_TABLE = "vsa-staging-avx-investigations"
_WG    = "cvx-staging-analytics"
_S3    = "s3://cvx-staging-athena-results-1234567890-us-east-1/tier2/"

_INV_ID     = "inv-fresh-test-0001"
_PATTERN_ID = "pat-fresh-P0AA6-plant-CGA"
_VIN_1      = "MRDVEH00000000040"

_NOW_STR    = "2026-10-05T06:00:00Z"
_TODAY      = datetime.date(2026, 10, 5)
_STALE_DATE = datetime.date(2026, 10, 3)   # 2 days ago: cutoff=10-04, 10-03 < cutoff

_FRESH_DATES = {
    "managed_vehicles": SnapshotState(date=_TODAY, row_count=None, table_date=_TODAY),
    "vehicle_fault_status_daily": SnapshotState(date=_TODAY, row_count=1, table_date=_TODAY),
}
_STALE_DATES = {
    "managed_vehicles": SnapshotState(date=_STALE_DATE, row_count=None, table_date=_STALE_DATE),
    "vehicle_fault_status_daily": SnapshotState(date=_STALE_DATE, row_count=1, table_date=_STALE_DATE),
}

# Keys that run_investigation passes to _read_snap_dates.
_INV_SNAP_KEYS = frozenset({"managed_vehicles", "vehicle_fault_status_daily"})


def _snap_stub(dates: dict):
    """Return (stub, keys_seen) — stub records the positional keys arg and returns dates."""
    keys_seen: list[frozenset] = []

    def _fn(stage, keys, **kw):
        keys_seen.append(frozenset(keys))
        return dates

    return _fn, keys_seen

# ---------------------------------------------------------------------------
# Minimal doubles (reused from test_job_investigation pattern)
# ---------------------------------------------------------------------------

def _ddb_queued() -> dict:
    return {
        "investigation_id": {"S": _INV_ID},
        "pattern_id":       {"S": _PATTERN_ID},
        "status":           {"S": "queued"},
        "requested_by":     {"S": "eng-sub-fresh-001"},
        "requested_at":     {"S": "2026-10-05T05:00:00Z"},
    }


class _FakeDDB:
    def __init__(self, initial_item: dict) -> None:
        self._item = initial_item
        self.update_calls: list[dict] = []

    def get_item(self, *, TableName, Key, ConsistentRead=False) -> dict:
        return {"Item": self._item}

    def update_item(self, *, TableName, Key, UpdateExpression,
                    ExpressionAttributeNames=None, ExpressionAttributeValues=None,
                    ConditionExpression=None, **kw) -> dict:
        self.update_calls.append({"UpdateExpression": UpdateExpression,
                                  "ConditionExpression": ConditionExpression,
                                  "ExpressionAttributeValues": ExpressionAttributeValues})
        if self._item is None:
            self._item = {}
        names = ExpressionAttributeNames or {}
        vals  = ExpressionAttributeValues or {}
        ue    = UpdateExpression or ""
        status_key = names.get("#s", "status")
        for placeholder, value in [
            (":running",   "running"),
            (":completed", "completed"),
            (":failed",    "failed"),
            (":queued",    "queued"),
        ]:
            if placeholder in vals and placeholder in ue:
                self._item[status_key] = vals[placeholder]
        err_key = names.get("#em", "error_message")
        if ":msg" in vals:
            self._item[err_key] = vals[":msg"]
        if ":fid" in vals:
            self._item["finding_id"] = vals[":fid"]
        return {}

    @property
    def current_status(self) -> str:
        s = self._item.get("status") if self._item else None
        if isinstance(s, dict):
            return s.get("S", "")
        return str(s) if s else ""


def _minimal_athena_stub(pattern_row: dict) -> MagicMock:
    """Minimal Athena stub returning one pattern row for run_investigation."""
    stub = MagicMock()
    stub.start_query_execution.return_value = {"QueryExecutionId": "exec-fresh-001"}
    stub.get_query_execution.return_value = {
        "QueryExecution": {"Status": {"State": "SUCCEEDED"}}
    }

    call_i = [0]

    def _get_results(**kw) -> dict:
        idx = call_i[0]
        call_i[0] += 1
        if idx == 0:  # pattern group query
            header = {"Data": [{"VarCharValue": k} for k in pattern_row.keys()]}
            row    = {"Data": [{"VarCharValue": v or ""} for v in pattern_row.values()]}
            return {"ResultSet": {"Rows": [header, row]}}
        return {"ResultSet": {"Rows": []}}

    stub.get_query_results.side_effect = _get_results
    return stub


_PATTERN_ROW = {
    "pattern_id":              _PATTERN_ID,
    "code_family":             "P0AA6",
    "cohort_dimension":        "assembly_plant",
    "cohort_value":            "CGA",
    "candidate_rank":          "1",
    "pattern_group_id":        _PATTERN_ID,
    "vehicles_affected":       "3",
    "vehicles_in_service":     "100",
    "rate":                    "0.03",
    "rate_rest":               "0.01",
    "rate_ratio":              "3.0",
    "p_value":                 "0.01",
    "first_detected_month":    "2026-10-01",
    "trend":                   "stable",
    "method":                  "poisson_bonferroni",
    "affected_vins_sample_csv": _VIN_1,
    "computed_at":             "2026-10-05 01:00:00.000",
    "parameters_version":      "1.0.0",
}


# ---------------------------------------------------------------------------
# Test A: stale dates → status=failed, no Finding, readers not called
# ---------------------------------------------------------------------------

class TestStaleSnapshotAbortsBeforeReaders:
    """Stale dates must abort before read_controls and read_co_occurring_codes."""

    def test_stale_dates_set_status_failed(self, monkeypatch):
        """Stale snapshot → investigation status=failed (or queued if first attempt)."""
        from agents.fi_quality import job

        _stub, _ks = _snap_stub(_STALE_DATES)
        monkeypatch.setattr(job, "_read_snap_dates", _stub)

        ddb = _FakeDDB(_ddb_queued())
        findings: list = []

        try:
            job.run_investigation(
                _INV_ID,
                athena_client=_minimal_athena_stub(_PATTERN_ROW),
                workgroup=_WG,
                results_s3_path=_S3,
                stage=_STAGE,
                ddb_client=ddb,
                investigations_table=_TABLE,
                write_finding=lambda f: findings.append(f),
                now=lambda: _NOW_STR,
                approximate_receive_count=2,
                max_receive_count=2,
            )
        except Exception:
            pass

        assert ddb.current_status == "failed", (
            f"Stale snapshot must set status=failed; got {ddb.current_status!r}. "
            "Mutation: dropping the freshness check lets the investigation proceed "
            "with a stale snapshot → status=completed → fails."
        )
        assert _ks and _ks[0] == _INV_SNAP_KEYS, (
            f"_read_snap_dates must be called with {set(_INV_SNAP_KEYS)!r}; got {list(_ks)}."
        )

    def test_stale_dates_no_finding_written(self, monkeypatch):
        """Stale snapshot → no Finding must be written."""
        from agents.fi_quality import job

        _stub, _ks = _snap_stub(_STALE_DATES)
        monkeypatch.setattr(job, "_read_snap_dates", _stub)

        ddb = _FakeDDB(_ddb_queued())
        findings: list = []

        try:
            job.run_investigation(
                _INV_ID,
                athena_client=_minimal_athena_stub(_PATTERN_ROW),
                workgroup=_WG,
                results_s3_path=_S3,
                stage=_STAGE,
                ddb_client=ddb,
                investigations_table=_TABLE,
                write_finding=lambda f: findings.append(f),
                now=lambda: _NOW_STR,
                approximate_receive_count=2,
                max_receive_count=2,
            )
        except Exception:
            pass

        assert findings == [], (
            "No Finding must be written when the snapshot is stale. "
            "Mutation: dropping the check allows the investigation to complete "
            "and write a Finding based on stale ADP data → fails."
        )
        assert _ks and _ks[0] == _INV_SNAP_KEYS, (
            f"_read_snap_dates must be called with {set(_INV_SNAP_KEYS)!r}; got {list(_ks)}."
        )

    def test_stale_dates_read_controls_not_called(self, monkeypatch):
        """Stale snapshot → read_controls and read_co_occurring_codes must not be called.

        Patches via job.* (the names job.py imported at module load) rather than
        readers.* so the patched callables are actually reached by _build_investigation_bundle.
        Asserts both lists in both directions: empty when stale, non-empty when fresh.
        """
        from agents.fi_quality import job

        _stub, _ks = _snap_stub(_STALE_DATES)
        monkeypatch.setattr(job, "_read_snap_dates", _stub)

        controls_called: list = []
        co_called: list = []

        monkeypatch.setattr(
            job, "read_controls",
            lambda *a, **kw: controls_called.append(1) or [],
        )
        monkeypatch.setattr(
            job, "read_co_occurring_codes",
            lambda *a, **kw: co_called.append(1) or [],
        )

        ddb = _FakeDDB(_ddb_queued())

        try:
            job.run_investigation(
                _INV_ID,
                athena_client=_minimal_athena_stub(_PATTERN_ROW),
                workgroup=_WG,
                results_s3_path=_S3,
                stage=_STAGE,
                ddb_client=ddb,
                investigations_table=_TABLE,
                write_finding=lambda f: None,
                now=lambda: _NOW_STR,
                approximate_receive_count=2,
                max_receive_count=2,
            )
        except Exception:
            pass

        assert controls_called == [], (
            "read_controls must not be called when snapshot is stale. "
            "Mutation: moving the gate after _build_investigation_bundle lets "
            "read_controls run → controls_called non-empty → fails."
        )
        assert co_called == [], (
            "read_co_occurring_codes must not be called when snapshot is stale. "
            "Mutation: moving the gate after readers lets it run → fails."
        )
        assert _ks and _ks[0] == _INV_SNAP_KEYS, (
            f"_read_snap_dates must be called with {set(_INV_SNAP_KEYS)!r}; got {list(_ks)}."
        )


# ---------------------------------------------------------------------------
# Test B: fresh dates → both readers called
# ---------------------------------------------------------------------------

class TestFreshSnapshotCallsBothReaders:
    """With fresh dates, read_controls and read_co_occurring_codes are called."""

    def test_fresh_dates_both_readers_called(self, monkeypatch):
        """Fresh snapshot → both read_controls and read_co_occurring_codes are invoked."""
        from agents.fi_quality import job

        _stub, _ks = _snap_stub(_FRESH_DATES)
        monkeypatch.setattr(job, "_read_snap_dates", _stub)

        controls_called: list = []
        co_called: list = []

        # Patch via the job module's own namespace (the names were imported there).
        monkeypatch.setattr(
            job, "read_controls",
            lambda *a, **kw: controls_called.append(1) or [],
        )
        monkeypatch.setattr(
            job, "read_co_occurring_codes",
            lambda *a, **kw: co_called.append(1) or [],
        )
        # Stub all other readers to avoid real Athena calls.
        monkeypatch.setattr(job, "read_pattern_group",
                            lambda *a, **kw: [{
                                **_PATTERN_ROW,
                                "affected_vins_sample": [_VIN_1],
                                "cohort_dimension": "model",
                                "cohort_value": "Azimuth",
                            }])
        # Supply a build-facts row so model_year_int is set and read_controls is called.
        monkeypatch.setattr(job, "read_affected_vins",
                            lambda *a, **kw: [{"vin": _VIN_1, "model": "Azimuth",
                                               "model_year": "2024", "build_month": "2024-01",
                                               "plant": "CGA", "supplier_lot": "L001"}])
        monkeypatch.setattr(job, "read_failure_modes", lambda *a, **kw: [])
        monkeypatch.setattr(job, "read_campaigns",
                            lambda *a, **kw: {"recall": [], "ota": []})
        monkeypatch.setattr(job, "read_service_history", lambda *a, **kw: [])
        monkeypatch.setattr(job, "read_telemetry_aggregates", lambda *a, **kw: [])

        ddb = _FakeDDB(_ddb_queued())
        findings: list = []

        try:
            job.run_investigation(
                _INV_ID,
                athena_client=MagicMock(),
                workgroup=_WG,
                results_s3_path=_S3,
                stage=_STAGE,
                ddb_client=ddb,
                investigations_table=_TABLE,
                write_finding=lambda f: findings.append(f) or {"finding_id": "FIND-fresh-001"},
                now=lambda: _NOW_STR,
                approximate_receive_count=1,
                max_receive_count=2,
            )
        except Exception:
            pass

        assert controls_called, (
            "read_controls must be called when snapshot dates are fresh. "
            "Mutation: raising even on fresh dates blocks the investigation → fails."
        )
        # read_co_occurring_codes is called when affected_vins_sample is non-empty.
        assert co_called, (
            "read_co_occurring_codes must be called when snapshot dates are fresh. "
            "Mutation: raising even on fresh dates blocks the investigation → fails."
        )
        assert ddb.current_status == "completed", (
            f"Fresh snapshot investigation must complete; got {ddb.current_status!r}. "
            "Mutation: returning a list from read_campaigns raises AttributeError "
            "→ status=failed → fails."
        )
        assert len(findings) == 1, (
            f"Exactly 1 Finding must be written on a fresh run; got {len(findings)}."
        )
        assert _ks and _ks[0] == _INV_SNAP_KEYS, (
            f"_read_snap_dates must be called with {set(_INV_SNAP_KEYS)!r}; got {list(_ks)}."
        )


# ---------------------------------------------------------------------------
# Test C: mixed dates in the 02:00-04:00 window must not block the investigation
# ---------------------------------------------------------------------------


class TestMixedDatesIngestWindow:
    """now=03:00Z, managed_vehicles=yesterday, fault_daily=today → completed."""

    def test_03z_mixed_dates_investigation_completes(self, monkeypatch):
        """C1 fix: same_date=False lets the investigation proceed in the ingest window.

        Mutation: flipping same_date back to True makes the call site refuse
        with 'mixed snapshot dates' → status=failed, 0 Findings → fails.
        """
        import datetime as _dt2
        from agents.fi_quality import job

        _NOW_MIXED = "2026-10-10T03:00:00Z"
        _D_TODAY = _dt2.date(2026, 10, 10)
        _D_YEST  = _dt2.date(2026, 10, 9)

        _mixed_dates = {
            "managed_vehicles": SnapshotState(
                date=_D_YEST, row_count=None, table_date=_D_YEST
            ),
            "vehicle_fault_status_daily": SnapshotState(
                date=_D_TODAY, row_count=1, table_date=_D_TODAY
            ),
        }
        _stub_m, _ks_m = _snap_stub(_mixed_dates)
        monkeypatch.setattr(job, "_read_snap_dates", _stub_m)

        monkeypatch.setattr(job, "read_controls", lambda *a, **kw: [])
        monkeypatch.setattr(job, "read_co_occurring_codes", lambda *a, **kw: [])
        monkeypatch.setattr(job, "read_pattern_group", lambda *a, **kw: [{
            **_PATTERN_ROW,
            "affected_vins_sample": [_VIN_1],
            "cohort_dimension": "model",
            "cohort_value": "Azimuth",
        }])
        monkeypatch.setattr(job, "read_affected_vins", lambda *a, **kw: [])
        monkeypatch.setattr(job, "read_failure_modes", lambda *a, **kw: [])
        monkeypatch.setattr(job, "read_campaigns",
                            lambda *a, **kw: {"recall": [], "ota": []})
        monkeypatch.setattr(job, "read_service_history", lambda *a, **kw: [])
        monkeypatch.setattr(job, "read_telemetry_aggregates", lambda *a, **kw: [])

        ddb = _FakeDDB(_ddb_queued())
        findings: list = []

        try:
            job.run_investigation(
                _INV_ID,
                athena_client=_minimal_athena_stub(_PATTERN_ROW),
                workgroup=_WG,
                results_s3_path=_S3,
                stage=_STAGE,
                ddb_client=ddb,
                investigations_table=_TABLE,
                write_finding=lambda f: findings.append(f) or {"finding_id": "FIND-mix-001"},
                now=lambda: _NOW_MIXED,
                approximate_receive_count=1,
                max_receive_count=2,
            )
        except Exception:
            pass

        assert ddb.current_status == "completed", (
            f"Mixed dates in 02:00-04:00 window must still complete; got "
            f"{ddb.current_status!r}. Mutation: same_date=True refuses 'mixed' → fails."
        )
        assert len(findings) == 1, (
            f"Expected 1 Finding written; got {len(findings)}."
        )
        assert _ks_m and _ks_m[0] == _INV_SNAP_KEYS, (
            f"_read_snap_dates must be called with {set(_INV_SNAP_KEYS)!r}; got {list(_ks_m)}."
        )



# ---------------------------------------------------------------------------
# Test D: empty fault marker → read_co_occurring_codes not called; completes
# ---------------------------------------------------------------------------


class TestEmptyFaultMarkerSkipsCoOccurring:
    """Empty vehicle_fault_status_daily marker (row_count=0) → co-occurring reader skipped."""

    def test_empty_fault_marker_skips_co_occurring_reader_and_completes(self, monkeypatch):
        """When vehicle_fault_status_daily has row_count=0, read_co_occurring_codes is not called.

        Mutation guard: removing the ``skip_co_occurring`` flag causes
        read_co_occurring_codes to be called as normal → co_called non-empty → fails.
        """
        from agents.fi_quality import job

        _empty_fault = {
            "managed_vehicles": SnapshotState(
                date=_TODAY, row_count=None, table_date=_TODAY
            ),
            "vehicle_fault_status_daily": SnapshotState(
                date=_TODAY, row_count=0, table_date=_TODAY
            ),
        }
        _stub_e, _ks_e = _snap_stub(_empty_fault)
        monkeypatch.setattr(job, "_read_snap_dates", _stub_e)

        co_called: list = []
        monkeypatch.setattr(
            job, "read_co_occurring_codes",
            lambda *a, **kw: co_called.append(1) or [],
        )
        monkeypatch.setattr(job, "read_controls", lambda *a, **kw: [])
        monkeypatch.setattr(job, "read_pattern_group", lambda *a, **kw: [{
            **_PATTERN_ROW,
            "affected_vins_sample": [_VIN_1],
            "cohort_dimension": "model",
            "cohort_value": "Azimuth",
        }])
        monkeypatch.setattr(job, "read_affected_vins", lambda *a, **kw: [])
        monkeypatch.setattr(job, "read_failure_modes", lambda *a, **kw: [])
        monkeypatch.setattr(
            job, "read_campaigns", lambda *a, **kw: {"recall": [], "ota": []}
        )
        monkeypatch.setattr(job, "read_service_history", lambda *a, **kw: [])
        monkeypatch.setattr(job, "read_telemetry_aggregates", lambda *a, **kw: [])

        ddb = _FakeDDB(_ddb_queued())
        findings: list = []

        try:
            job.run_investigation(
                _INV_ID,
                athena_client=MagicMock(),
                workgroup=_WG,
                results_s3_path=_S3,
                stage=_STAGE,
                ddb_client=ddb,
                investigations_table=_TABLE,
                write_finding=lambda f: findings.append(f) or {"finding_id": "FIND-empty-001"},
                now=lambda: _NOW_STR,
                approximate_receive_count=1,
                max_receive_count=2,
            )
        except Exception:
            pass

        assert co_called == [], (
            "read_co_occurring_codes must NOT be called when fault marker is empty. "
            "Mutation: removing skip_co_occurring flag → co_called non-empty → CAUGHT."
        )
        assert ddb.current_status == "completed", (
            f"Investigation must complete with empty fault marker; got "
            f"{ddb.current_status!r}. Mutation: raising on empty marker → failed."
        )
        assert len(findings) == 1, (
            f"Exactly 1 Finding must be written; got {len(findings)}."
        )
        assert _ks_e and _ks_e[0] == _INV_SNAP_KEYS, (
            f"_read_snap_dates must be called with {set(_INV_SNAP_KEYS)!r}; got {list(_ks_e)}."
        )
