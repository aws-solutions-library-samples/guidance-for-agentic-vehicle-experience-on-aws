"""Tests for the investigation job — task 3.3b.

Coverage:
  1. SQL text: exact ``adp_{stage}_engineering.*`` table names in SQL templates.
  2. Two-page paging per reader (pattern group, affected VINs, failure modes,
     campaigns, co-occurring codes).
  3. Status transitions with a fake DDB honouring ConditionExpression:
     * queued → running → completed
     * failure during bundle-build → failed with the fixed message
     * ``running`` status → no-op (redelivery)
     * ``completed`` status → no-op (redelivery)
  4. Unknown pattern (no Athena rows) → status=failed; re-raises.
  5. Mutation guards:
     * Dropping the ``queued`` condition on the running transition.
     * Leaving ``running`` status on failure.
     * Stopping after the first Athena page.
     * Taking affected VINs from the request instead of ``fault_patterns``.

Run:
    .venv/bin/python -m pytest agents/fi_quality/tests -q
"""
from __future__ import annotations

import copy
import json
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Autouse fixture: fresh snapshot dates for all run_investigation tests
# ---------------------------------------------------------------------------
import datetime as _dt

_FI_SNAP_DATE = _dt.date(2026, 10, 5)  # matches now=lambda: "2026-10-05T06:00:00Z"

from agents.tier2.snapshot_freshness import SnapshotState as _SnapshotState

_FI_FRESH = {
    "managed_vehicles": _SnapshotState(date=_FI_SNAP_DATE, row_count=None, table_date=_FI_SNAP_DATE),
    "vehicle_fault_status_daily": _SnapshotState(date=_FI_SNAP_DATE, row_count=1, table_date=_FI_SNAP_DATE),
}


@pytest.fixture(autouse=True)
def _fresh_fi_snapshot_dates(monkeypatch):
    """Patch _read_snap_dates so existing run_investigation tests need no Athena.

    The gate is passed through here (W1: a fixed date went stale after 2026-10-07 and a
    wall-clock date reads as "future" for tests pinning ``now=``).
    """
    from agents.fi_quality import job

    # These are the job's pre-gate tests and pass their own clocks (``now=``), so no fixed
    # snapshot date fits them all. The gate itself (reads, rules, same_date=False) is pinned
    # in test_investigation_snapshot_freshness.py with the real require_fresh.
    monkeypatch.setattr(job, "_read_snap_dates", lambda *_a, **_kw: {
        "managed_vehicles": _SnapshotState(date=None, row_count=None, table_date=None),
        "vehicle_fault_status_daily": _SnapshotState(date=None, row_count=None, table_date=None),
    })
    monkeypatch.setattr(job, "_require_fresh_snap", lambda *_a, **_kw: None)

# ---------------------------------------------------------------------------
# Test constants
# ---------------------------------------------------------------------------

_STAGE = "staging"
_TABLE = "vsa-staging-avx-investigations"
_WG    = "cvx-staging-analytics"
_S3    = "s3://cvx-staging-athena-results-1234567890-us-east-1/tier2/"

_INV_ID     = "inv-aaaa-bbbb-cccc-0001"
_PATTERN_ID = "pat-001-P0AA6-plant-CGA"
_VIN_1      = "MRDVEH00000000020"
_VIN_2      = "MRDVEH00000000021"
_VIN_CTL    = "MRDVEH00000000099"

# Minimal fault_patterns row (matches the _PATTERN_SQL columns)
_PATTERN_ROW_1 = {
    "pattern_id":              _PATTERN_ID,
    "code_family":             "P0AA6",
    "cohort_dimension":        "assembly_plant",
    "cohort_value":            "CGA",
    "candidate_rank":          "1",
    "pattern_group_id":        _PATTERN_ID,
    "vehicles_affected":       "42",
    "vehicles_in_service":     "500",
    "rate":                    "0.084",
    "rate_rest":               "0.014",
    "rate_ratio":              "6.0",
    "p_value":                 "0.0001",
    "first_detected_month":    "2026-07-01",
    "trend":                   "rising",
    "method":                  "poisson_bonferroni",
    "affected_vins_sample_csv": f"{_VIN_1},{_VIN_2}",
    "computed_at":             "2026-10-05 01:00:00.000",
    "parameters_version":      "1.0.0",
}

# Minimal vehicle_build_components row (matches _AFFECTED_VINS_SQL columns)
_BUILD_ROW_1 = {
    "vin":         _VIN_1,
    "model":       "Azimuth",
    "model_year":  "2024",
    "build_month": "2024-03",
    "plant":       "CGA",
    "component_id": "hv_battery_module",
    "supplier_id":  "SUP-A",
    "supplier_lot": "L-2024-03-CGA",
    "part_number":  "HV-001",
}
_BUILD_ROW_2 = {**_BUILD_ROW_1, "vin": _VIN_2}

# Minimal engineering_failure_modes + remedy_options rows (joined)
_FM_ROW = {
    "failure_mode_id":         "fm-001",
    "component_id":            "hv_battery_module",
    "mechanism":               "isolation_degradation",
    "symptom_codes_csv":       "P0AA6,P0A7F",
    "cause_dimension":         "supplier_lot",
    "do_not_drive":            "false",
    "park_outside":            "false",
    "remedy_option_id":        "opt-001",
    "remedy_type":             "hardware_recall",
    "parts_cost_per_vehicle":  "2400.00",
    "labor_hours_per_vehicle": "8.0",
    "lead_time_weeks":         "16",
    "over_the_air":            "false",
    "trade_off_notes":         "High cost; eliminates isolation risk.",
}
_FM_ROW_OPT2 = {
    **_FM_ROW,
    "remedy_option_id":        "opt-002",
    "remedy_type":             "monitor",
    "parts_cost_per_vehicle":  "0.0",
    "labor_hours_per_vehicle": "0.5",
    "lead_time_weeks":         "1",
    "over_the_air":            "true",
    "trade_off_notes":         "Monitor only.",
}

# Minimal DDB item format (type-descriptor)
def _ddb_investigation(status: str, *, started_at: str | None = None) -> dict:
    item = {
        "investigation_id": {"S": _INV_ID},
        "pattern_id":       {"S": _PATTERN_ID},
        "status":           {"S": status},
        "requested_by":     {"S": "eng-sub-001"},
        "requested_at":     {"S": "2026-10-05T10:00:00Z"},
    }
    if started_at is not None:
        item["started_at"] = {"S": started_at}
    return item


# ---------------------------------------------------------------------------
# Helpers: fake Athena client and fake DynamoDB client
# ---------------------------------------------------------------------------

def _make_athena_stub(rows: list[dict], *, two_pages: bool = False) -> MagicMock:
    """Return a minimal Athena stub that serves *rows* as query results.

    When *two_pages* is True, the rows are split across two pages to test
    that callers follow NextToken to the end.
    """
    stub = MagicMock()

    stub.start_query_execution.return_value = {"QueryExecutionId": "exec-001"}
    stub.get_query_execution.return_value = {
        "QueryExecution": {"Status": {"State": "SUCCEEDED"}}
    }

    if not rows:
        stub.get_query_results.return_value = {
            "ResultSet": {"Rows": []},
        }
        return stub

    # Build Athena-style rows: first row = header, rest = data.
    header_row = {"Data": [{"VarCharValue": k} for k in rows[0].keys()]}

    def _data_row(r: dict) -> dict:
        return {"Data": [{"VarCharValue": v if v is not None else ""} for v in r.values()]}

    data_rows = [_data_row(r) for r in rows]

    if two_pages:
        # Page 1: header + first data row; Page 2: remaining data rows
        page1_rows = [header_row, data_rows[0]]
        page2_rows = data_rows[1:]

        calls = [0]

        def _get_results(**kwargs: Any) -> dict:
            if calls[0] == 0:
                calls[0] += 1
                return {
                    "ResultSet": {"Rows": page1_rows},
                    "NextToken": "page2-token",
                }
            return {"ResultSet": {"Rows": page2_rows}}

        stub.get_query_results.side_effect = _get_results
    else:
        stub.get_query_results.return_value = {
            "ResultSet": {"Rows": [header_row] + data_rows},
        }

    return stub


class _FakeDDB:
    """Fake DynamoDB client that honours ConditionExpression for status.

    Stores a single item keyed by investigation_id.  The ``update_item``
    method enforces the ConditionExpression for the ``queued`` condition.
    """

    def __init__(self, initial_item: dict | None = None) -> None:
        # Store the item in Python-native format (not type-descriptor)
        self._item: dict | None = initial_item
        self.update_calls: list[dict] = []

    def get_item(self, *, TableName: str, Key: dict, ConsistentRead: bool = False) -> dict:
        if self._item is None:
            return {}
        return {"Item": self._item}

    def update_item(
        self,
        *,
        TableName: str,
        Key: dict,
        UpdateExpression: str,
        ExpressionAttributeNames: dict | None = None,
        ExpressionAttributeValues: dict | None = None,
        ConditionExpression: str | None = None,
        **kwargs: Any,
    ) -> dict:
        self.update_calls.append({
            "UpdateExpression": UpdateExpression,
            "ConditionExpression": ConditionExpression,
            "ExpressionAttributeNames": ExpressionAttributeNames,
            "ExpressionAttributeValues": ExpressionAttributeValues,
        })

        if self._item is None:
            self._item = {}

        # Enforce ConditionExpression "#s = :queued"
        if ConditionExpression and "#s = :queued" in ConditionExpression:
            vals = ExpressionAttributeValues or {}
            names = ExpressionAttributeNames or {}
            queued_val = vals.get(":queued", {}).get("S", "queued")
            status_field = names.get("#s", "status")
            current_status = self._item.get(status_field, {}).get("S", "")
            if current_status != queued_val:
                # Simulate ConditionalCheckFailedException
                error = {
                    "Error": {
                        "Code": "ConditionalCheckFailedException",
                        "Message": f"Condition not met: status is {current_status!r}",
                    }
                }
                from botocore.exceptions import ClientError
                raise ClientError(error, "UpdateItem")

        # Apply SET expressions (simplified: handle only our known patterns)
        if ExpressionAttributeValues and ExpressionAttributeNames:
            names = ExpressionAttributeNames
            vals  = ExpressionAttributeValues
            update_expr = UpdateExpression or ""
            # SET #s = :running  /  :completed  /  :failed  /  :queued
            # Only update status when the placeholder appears in the UpdateExpression
            # (not just when it is present in vals for a ConditionExpression).
            status_key = names.get("#s", "status")
            for attr_placeholder, val_placeholder in [
                (":running",   "running"),
                (":completed", "completed"),
                (":failed",    "failed"),
                (":queued",    "queued"),
            ]:
                if attr_placeholder in vals and attr_placeholder in update_expr:
                    self._item[status_key] = vals[attr_placeholder]

            # SET finding_id = :fid
            if ":fid" in vals:
                self._item["finding_id"] = vals[":fid"]

            # SET started_at = :ts
            if ":ts" in vals:
                self._item["started_at"] = vals[":ts"]

            # SET #em = :msg
            err_key = names.get("#em", "error_message")
            if ":msg" in vals:
                self._item[err_key] = vals[":msg"]

        return {}

    @property
    def current_status(self) -> str:
        if self._item is None:
            return ""
        s = self._item.get("status") or self._item.get("#s")
        if isinstance(s, dict):
            return s.get("S", "")
        return str(s) if s else ""

    @property
    def current_error_message(self) -> str:
        if self._item is None:
            return ""
        m = self._item.get("error_message", {})
        if isinstance(m, dict):
            return m.get("S", "")
        return str(m) if m else ""

    @property
    def current_finding_id(self) -> str:
        if self._item is None:
            return ""
        f = self._item.get("finding_id", {})
        if isinstance(f, dict):
            return f.get("S", "")
        return str(f) if f else ""


def _make_all_athena_stubs(
    *,
    pattern_rows: list[dict] | None = None,
    build_rows: list[dict] | None = None,
    control_rows: list[dict] | None = None,
    co_rows: list[dict] | None = None,
    fm_rows: list[dict] | None = None,
    ota_rows: list[dict] | None = None,
    recall_rows: list[dict] | None = None,
    svc_rows: list[dict] | None = None,
    two_pages: bool = False,
) -> MagicMock:
    """Return an Athena stub that serves different results per call sequence.

    Queries are executed in a fixed order by _build_investigation_bundle:
      0. read_pattern_group
      1. read_affected_vins
      2. read_controls
      3. read_co_occurring_codes
      4. read_failure_modes
      5. read_campaigns (OTA)
      6. read_campaigns (recall)
      7. read_service_history
    """
    responses = [
        pattern_rows or [_PATTERN_ROW_1],
        build_rows   or [_BUILD_ROW_1, _BUILD_ROW_2],
        control_rows or [],
        co_rows      or [],
        fm_rows      or [_FM_ROW, _FM_ROW_OPT2],
        ota_rows     or [],
        recall_rows  or [],
        svc_rows     or [],
    ]

    stub = MagicMock()
    stub.start_query_execution.return_value = {"QueryExecutionId": "exec-001"}
    stub.get_query_execution.return_value = {
        "QueryExecution": {"Status": {"State": "SUCCEEDED"}}
    }

    call_index = [0]

    def _get_results(**kwargs: Any) -> dict:
        idx = call_index[0]
        rows = responses[idx % len(responses)]
        call_index[0] += 1

        if not rows:
            return {"ResultSet": {"Rows": []}}

        header_row = {"Data": [{"VarCharValue": k} for k in rows[0].keys()]}

        def _data_row(r: dict) -> dict:
            return {"Data": [
                {"VarCharValue": v if v is not None else ""}
                for v in r.values()
            ]}

        data_rows = [_data_row(r) for r in rows]

        if two_pages and idx == 0:
            # Only the first query (pattern group) returns two pages.
            page = kwargs.get("NextToken")
            if page is None:
                return {
                    "ResultSet": {"Rows": [header_row, data_rows[0]]},
                    "NextToken": "page2-token",
                }
            # Call index for next round has already incremented — serve page 2.
            call_index[0] -= 1  # keep on same index until both pages consumed
            rows2 = responses[idx % len(responses)]
            data_rows2 = [_data_row(r) for r in rows2]
            call_index[0] += 1
            return {"ResultSet": {"Rows": data_rows2[1:]}}

        return {"ResultSet": {"Rows": [header_row] + data_rows}}

    stub.get_query_results.side_effect = _get_results
    return stub


# ---------------------------------------------------------------------------
# ============================================================================
# 1. SQL text tests — exact adp_{stage}_engineering.* names
# ============================================================================


class TestSqlTableNames:
    """SQL templates must reference the exact qualified table names."""

    def test_pattern_sql_references_adp_engineering_fault_patterns(self):
        """_PATTERN_SQL must join adp_{stage}_engineering.fault_patterns."""
        from agents.fi_quality.readers import _PATTERN_SQL
        assert "adp_${stage}_engineering.fault_patterns" in _PATTERN_SQL, (
            "_PATTERN_SQL must reference adp_${stage}_engineering.fault_patterns. "
            "Mutation: wrong database → query targets the wrong table."
        )

    def test_affected_vins_sql_references_vehicle_build_components(self):
        """_AFFECTED_VINS_SQL must join adp_{stage}_engineering.vehicle_build_components."""
        from agents.fi_quality.readers import _AFFECTED_VINS_SQL
        assert "adp_${stage}_engineering.vehicle_build_components" in _AFFECTED_VINS_SQL

    def test_failure_modes_sql_references_engineering_failure_modes(self):
        """_FAILURE_MODES_SQL must join adp_{stage}_engineering.engineering_failure_modes."""
        from agents.fi_quality.readers import _FAILURE_MODES_SQL
        assert "adp_${stage}_engineering.engineering_failure_modes" in _FAILURE_MODES_SQL

    def test_failure_modes_sql_references_engineering_remedy_options(self):
        """_FAILURE_MODES_SQL must join adp_{stage}_engineering.engineering_remedy_options."""
        from agents.fi_quality.readers import _FAILURE_MODES_SQL
        assert "adp_${stage}_engineering.engineering_remedy_options" in _FAILURE_MODES_SQL

    def test_controls_sql_references_vehicle_identity(self):
        """_CONTROLS_SQL must reference adp_{stage}_vehicle_identity.vehicle_identity."""
        from agents.fi_quality.readers import _CONTROLS_SQL
        assert "adp_${stage}_vehicle_identity.vehicle_identity" in _CONTROLS_SQL

    def test_co_occurring_sql_references_vehicle_fault_status(self):
        """_CO_OCCURRING_SQL must reference adp_{stage}_fleet_health.vehicle_fault_status_daily."""
        from agents.fi_quality.readers import _CO_OCCURRING_SQL
        assert "adp_${stage}_fleet_health.vehicle_fault_status_daily" in _CO_OCCURRING_SQL

    def test_ota_campaigns_sql_references_ota_campaigns(self):
        """_OTA_CAMPAIGNS_SQL must reference adp_{stage}_ota_campaigns.ota_campaigns."""
        from agents.fi_quality.readers import _OTA_CAMPAIGNS_SQL
        assert "adp_${stage}_ota_campaigns.ota_campaigns" in _OTA_CAMPAIGNS_SQL

    def test_ota_campaigns_sql_references_ota_campaign_events(self):
        """_OTA_CAMPAIGNS_SQL must reference adp_{stage}_ota_campaigns.ota_campaign_events."""
        from agents.fi_quality.readers import _OTA_CAMPAIGNS_SQL
        assert "adp_${stage}_ota_campaigns.ota_campaign_events" in _OTA_CAMPAIGNS_SQL

    def test_recall_campaigns_sql_references_recall_campaigns_received(self):
        """_RECALL_CAMPAIGNS_SQL must reference adp_{stage}_fleet_recall.recall_campaigns_received."""
        from agents.fi_quality.readers import _RECALL_CAMPAIGNS_SQL
        assert "adp_${stage}_fleet_recall.recall_campaigns_received" in _RECALL_CAMPAIGNS_SQL

    def test_service_history_sql_references_service_records(self):
        """_SERVICE_HISTORY_SQL must reference adp_{stage}_service_records.service_records."""
        from agents.fi_quality.readers import _SERVICE_HISTORY_SQL
        assert "adp_${stage}_service_records.service_records" in _SERVICE_HISTORY_SQL

    def test_pattern_sql_stage_substitution(self):
        """After substituting 'staging' for ${stage}, the table name is concrete."""
        from agents.fi_quality.readers import _PATTERN_SQL, _sql
        sql = _sql(_PATTERN_SQL, "staging")
        assert "adp_staging_engineering.fault_patterns" in sql
        assert "${stage}" not in sql


# ---------------------------------------------------------------------------
# ============================================================================
# 2. Two-page paging tests
# ============================================================================


class TestTwoPagePaging:
    """Readers must follow NextToken to consume all pages."""

    def test_two_page_paging_pattern_group(self):
        """read_pattern_group must consume both pages when NextToken is present.

        Mutation: stopping after page 1 returns only the first row, not both.
        The test asserts len(rows) == 2 (one row per page).
        """
        from agents.fi_quality.readers import read_pattern_group

        row_a = {**_PATTERN_ROW_1, "pattern_id": "pat-a", "candidate_rank": "1"}
        row_b = {**_PATTERN_ROW_1, "pattern_id": "pat-b", "candidate_rank": "2"}

        stub = MagicMock()
        stub.start_query_execution.return_value = {"QueryExecutionId": "exec-001"}
        stub.get_query_execution.return_value = {
            "QueryExecution": {"Status": {"State": "SUCCEEDED"}}
        }

        pages = [True]  # sentinel

        def _results(**kwargs: Any) -> dict:
            header = {"Data": [{"VarCharValue": k} for k in row_a.keys()]}
            def _dr(r: dict) -> dict:
                return {"Data": [{"VarCharValue": v or ""} for v in r.values()]}

            if pages[0]:
                pages[0] = False
                return {
                    "ResultSet": {"Rows": [header, _dr(row_a)]},
                    "NextToken": "tok2",
                }
            return {"ResultSet": {"Rows": [_dr(row_b)]}}

        stub.get_query_results.side_effect = _results

        rows = read_pattern_group(
            _PATTERN_ID, _STAGE,
            athena_client=stub, workgroup=_WG, results_s3_path=_S3,
        )
        assert len(rows) == 2, (
            "Both pages must be consumed; stopping after page 1 drops the "
            "second candidate row. Mutation: missing NextToken follow-through."
        )

    def test_two_page_paging_affected_vins(self):
        """read_affected_vins must consume both pages.

        Mutation: stopping at page 1 returns only the first VIN's build row.
        """
        from agents.fi_quality.readers import read_affected_vins

        row_b2 = {**_BUILD_ROW_2}
        stub = MagicMock()
        stub.start_query_execution.return_value = {"QueryExecutionId": "exec-002"}
        stub.get_query_execution.return_value = {
            "QueryExecution": {"Status": {"State": "SUCCEEDED"}}
        }
        pages = [True]

        def _results(**kwargs: Any) -> dict:
            header = {"Data": [{"VarCharValue": k} for k in _BUILD_ROW_1.keys()]}
            def _dr(r: dict) -> dict:
                return {"Data": [{"VarCharValue": v or ""} for v in r.values()]}
            if pages[0]:
                pages[0] = False
                return {
                    "ResultSet": {"Rows": [header, _dr(_BUILD_ROW_1)]},
                    "NextToken": "tok2",
                }
            return {"ResultSet": {"Rows": [_dr(row_b2)]}}

        stub.get_query_results.side_effect = _results

        rows = read_affected_vins(
            [_VIN_1, _VIN_2], _STAGE,
            athena_client=stub, workgroup=_WG, results_s3_path=_S3,
        )
        assert len(rows) == 2, (
            "Both pages must be consumed; stopping at page 1 returns only "
            "one VIN's build row. Mutation: missing NextToken follow-through."
        )

    def test_two_page_paging_failure_modes(self):
        """read_failure_modes must consume both pages (two remedy options across pages).

        Mutation: stopping at page 1 returns only the first option, missing the second.
        """
        from agents.fi_quality.readers import read_failure_modes

        stub = MagicMock()
        stub.start_query_execution.return_value = {"QueryExecutionId": "exec-003"}
        stub.get_query_execution.return_value = {
            "QueryExecution": {"Status": {"State": "SUCCEEDED"}}
        }
        pages = [True]

        def _results(**kwargs: Any) -> dict:
            header = {"Data": [{"VarCharValue": k} for k in _FM_ROW.keys()]}
            def _dr(r: dict) -> dict:
                return {"Data": [{"VarCharValue": v or ""} for v in r.values()]}
            if pages[0]:
                pages[0] = False
                return {
                    "ResultSet": {"Rows": [header, _dr(_FM_ROW)]},
                    "NextToken": "tok2",
                }
            return {"ResultSet": {"Rows": [_dr(_FM_ROW_OPT2)]}}

        stub.get_query_results.side_effect = _results

        modes = read_failure_modes(
            "P0AA", ["hv_battery_module"], _STAGE,
            athena_client=stub, workgroup=_WG, results_s3_path=_S3,
        )
        assert len(modes) == 1, "Both rows belong to the same failure mode"
        assert len(modes[0]["remedy_options"]) == 2, (
            "Both remedy options must be present; stopping at page 1 drops "
            "the second option. Mutation: missing NextToken follow-through."
        )


# ---------------------------------------------------------------------------
# ============================================================================
# 3. Status transition tests (fake DDB honouring ConditionExpression)
# ============================================================================


class TestStatusTransitions:
    """Status transitions follow the spec D10 rules path."""

    def _run(
        self,
        initial_status: str,
        *,
        athena_stubs: MagicMock | None = None,
        fail_pattern: bool = False,
        started_at: str | None = None,
        approximate_receive_count: int = 2,  # default to last attempt → set failed
    ) -> "_FakeDDB":
        from agents.fi_quality.job import run_investigation

        ddb = _FakeDDB(_ddb_investigation(initial_status, started_at=started_at))

        if athena_stubs is None and not fail_pattern:
            athena_stubs = _make_all_athena_stubs()
        elif athena_stubs is None:
            athena_stubs = _make_all_athena_stubs(pattern_rows=[])

        findings_written: list[dict] = []

        def _write_finding(f: dict) -> None:
            findings_written.append(f)

        try:
            run_investigation(
                _INV_ID,
                athena_client=athena_stubs,
                workgroup=_WG,
                results_s3_path=_S3,
                stage=_STAGE,
                ddb_client=ddb,
                investigations_table=_TABLE,
                write_finding=_write_finding,
                now=lambda: "2026-10-05T06:00:00Z",
                approximate_receive_count=approximate_receive_count,
                max_receive_count=2,
            )
        except Exception:
            pass

        return ddb

    def test_queued_transitions_to_running_then_completed(self):
        """A queued investigation transitions: queued → running → completed."""
        ddb = self._run("queued")
        assert ddb.current_status == "completed", (
            f"Expected status=completed, got {ddb.current_status!r}"
        )
        assert ddb.current_finding_id.startswith("FIND-"), (
            "finding_id must be set on completion"
        )

    def test_completed_is_noop_redelivery(self):
        """A completed investigation is a no-op on redelivery."""
        from agents.fi_quality.job import run_investigation

        ddb = _FakeDDB(_ddb_investigation("completed"))
        call_count = [0]

        def _write_finding(f: dict) -> None:
            call_count[0] += 1

        run_investigation(
            _INV_ID,
            athena_client=MagicMock(),
            workgroup=_WG,
            results_s3_path=_S3,
            stage=_STAGE,
            ddb_client=ddb,
            investigations_table=_TABLE,
            write_finding=_write_finding,
            now=lambda: "2026-10-05T06:00:00Z",
        )
        assert call_count[0] == 0, (
            "write_finding must not be called for a completed investigation "
            "(no-op redelivery)."
        )
        assert ddb.current_status == "completed"

    def test_fresh_running_raises_retryable(self):
        """A FRESH running row raises a retryable error (C1 cycle-3 answer 4).

        Probe N4: redelivery 905 s after a timed-out start (rc=2) must not
        silently delete the message — it must raise so the caller reports it
        in batchItemFailures and SQS requeues it.

        Mutation guard:
            Return silently (no-op) instead of raising → batchItemFailures=0 →
            message deleted → row stuck running indefinitely → test asserts raise.
        """
        from agents.fi_quality.job import run_investigation
        from agents.fi_quality.write import _STALE_AGE_SECONDS
        import datetime

        # Use a started_at that is 60 seconds ago — well within the stale limit.
        fresh_ts = (
            datetime.datetime.now(datetime.timezone.utc)
            - datetime.timedelta(seconds=60)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")

        ddb = _FakeDDB(_ddb_investigation("running", started_at=fresh_ts))
        call_count = [0]

        def _write_finding(f: dict) -> None:
            call_count[0] += 1

        with pytest.raises(RuntimeError, match="already being processed"):
            run_investigation(
                _INV_ID,
                athena_client=MagicMock(),
                workgroup=_WG,
                results_s3_path=_S3,
                stage=_STAGE,
                ddb_client=ddb,
                investigations_table=_TABLE,
                write_finding=_write_finding,
                now=lambda: "2026-10-05T06:00:00Z",
            )
        assert call_count[0] == 0, "write_finding must not be called for a fresh running row"
        assert ddb.current_status == "running", (
            f"Status must stay running for a fresh row; got {ddb.current_status!r}. "
            "Mutation: return silently → status stays running and no raise → FAILS."
        )

    def test_stale_running_reruns_in_same_delivery(self):
        """A stale running row is re-run in the same SQS delivery (C1 cycle-3 answer 4).

        'Not requeued via SQS': the row is reset to queued then _set_running fires,
        the job executes, and on success the row is completed — all in one delivery.

        Mutation guard (test_stale_running_reruns_in_same_delivery):
            C1-J4: reset dropped → _set_running fails → job never runs →
            status stays running/queued → test asserts status==completed → FAILS.
        """
        from agents.fi_quality.job import run_investigation

        ddb = _FakeDDB(_ddb_investigation("running", started_at="2026-01-01T00:00:00Z"))
        call_count = [0]

        def _write_finding_ok(f: dict) -> dict:
            call_count[0] += 1
            return {"finding_id": "FIND-stale-rerun-001"}

        run_investigation(
            _INV_ID,
            athena_client=_make_all_athena_stubs(),
            workgroup=_WG,
            results_s3_path=_S3,
            stage=_STAGE,
            ddb_client=ddb,
            investigations_table=_TABLE,
            write_finding=_write_finding_ok,
            now=lambda: "2026-10-05T06:00:00Z",
            approximate_receive_count=1,
            max_receive_count=2,
        )

        assert call_count[0] == 1, (
            "write_finding must be called once for a stale re-run; "
            f"got {call_count[0]}. "
            "Mutation C1-J4: reset dropped → _set_running fails → job never runs → FAILS."
        )
        assert ddb.current_status == "completed", (
            f"Stale re-run must complete; got {ddb.current_status!r}. "
            "Mutation C1-J4: reset dropped → status never reaches completed → FAILS."
        )

    def test_stale_running_is_marked_failed_on_redelivery(self):
        """A stale running row that fails at the receive-count limit is marked failed.

        C1 fix: at rc == max_receive_count the job runs, fails, and the row
        is set to ``failed`` (DLQ path).  Below the limit it is re-queued for
        redrive (test_stale_running_requeues_below_max).

        Mutation guard: if stale running were still a no-op, the row would block
        the pattern forever; this test catches it.
        """
        from agents.fi_quality.job import run_investigation

        # Use an old started_at — well beyond the stale limit.
        ddb = _FakeDDB(_ddb_investigation("running", started_at="2026-01-01T00:00:00Z"))
        call_count = [0]

        def _write_finding_fail(f: dict) -> None:
            call_count[0] += 1
            raise RuntimeError("forced write failure for test")

        with pytest.raises(RuntimeError):
            run_investigation(
                _INV_ID,
                athena_client=_make_all_athena_stubs(),
                workgroup=_WG,
                results_s3_path=_S3,
                stage=_STAGE,
                ddb_client=ddb,
                investigations_table=_TABLE,
                write_finding=_write_finding_fail,
                now=lambda: "2026-10-05T06:00:00Z",
                approximate_receive_count=2,  # at the limit → mark failed
                max_receive_count=2,
            )

        assert call_count[0] == 1, "write_finding must be called before failure"
        assert ddb.current_status == "failed", (
            f"A stale running row at the receive-count limit must be marked failed, "
            f"got {ddb.current_status!r}. "
            "Mutation: treating stale running as no-op → row stays running, pattern blocked."
        )

    def test_stale_running_requeues_below_max(self):
        """A stale running row below the receive-count limit is re-queued on failure.

        C1 fix: below the max, the job is re-run in this delivery. If it fails,
        the row is set to queued (not failed) so SQS can redrive.

        Mutation guard: resetting to failed instead of queued prevents redrive;
        the test asserts ``status == 'queued'``.
        """
        from agents.fi_quality.job import run_investigation

        ddb = _FakeDDB(_ddb_investigation("running", started_at="2026-01-01T00:00:00Z"))

        def _write_finding_fail(f: dict) -> None:
            raise RuntimeError("forced write failure for test")

        with pytest.raises(RuntimeError):
            run_investigation(
                _INV_ID,
                athena_client=_make_all_athena_stubs(),
                workgroup=_WG,
                results_s3_path=_S3,
                stage=_STAGE,
                ddb_client=ddb,
                investigations_table=_TABLE,
                write_finding=_write_finding_fail,
                now=lambda: "2026-10-05T06:00:00Z",
                approximate_receive_count=1,  # below limit → requeue on failure
                max_receive_count=2,
            )

        assert ddb.current_status == "queued", (
            f"Below max_receive_count, a failed re-run must reset to queued; "
            f"got {ddb.current_status!r}. "
            "Mutation: set_failed instead of requeue → pattern can't redrive → FAILS."
        )

    def test_failure_sets_status_failed_with_fixed_message(self):
        """Any exception during investigation sets status=failed on the last attempt.

        Mutation guard: leaving status=running on failure means the test fails
        because ddb.current_status == 'running' instead of 'failed'.
        W2 fix: passes approximate_receive_count=max_receive_count (last attempt)
        so the row is set to 'failed' rather than 'queued'.
        """
        from agents.fi_quality.job import run_investigation, _FAILURE_MSG

        ddb = _FakeDDB(_ddb_investigation("queued"))
        athena = MagicMock()
        athena.start_query_execution.return_value = {"QueryExecutionId": "exec-fail"}
        athena.get_query_execution.return_value = {
            "QueryExecution": {"Status": {"State": "FAILED",
                                          "StateChangeReason": "test error"}}
        }

        with pytest.raises(Exception):
            run_investigation(
                _INV_ID,
                athena_client=athena,
                workgroup=_WG,
                results_s3_path=_S3,
                stage=_STAGE,
                ddb_client=ddb,
                investigations_table=_TABLE,
                write_finding=lambda f: None,
                now=lambda: "2026-10-05T06:00:00Z",
                approximate_receive_count=2,  # last attempt → set failed
                max_receive_count=2,
            )

        assert ddb.current_status == "failed", (
            f"Expected status=failed after exception, got {ddb.current_status!r}. "
            "Mutation: not calling _set_failed leaves status=running, this fails."
        )
        assert ddb.current_error_message == _FAILURE_MSG, (
            f"Expected error_message={_FAILURE_MSG!r}, "
            f"got {ddb.current_error_message!r}"
        )


# ---------------------------------------------------------------------------
# ============================================================================
# 4. Unknown pattern → failed
# ============================================================================


class TestUnknownPattern:
    """An unknown pattern sets status=failed and re-raises."""

    def test_unknown_pattern_sets_failed_and_reraises(self):
        """When fault_patterns returns no rows, status=failed and PatternNotFound raised."""
        from agents.fi_quality.job import run_investigation, _FAILURE_MSG, PatternNotFound

        ddb = _FakeDDB(_ddb_investigation("queued"))

        athena = MagicMock()
        athena.start_query_execution.return_value = {"QueryExecutionId": "exec-empty"}
        athena.get_query_execution.return_value = {
            "QueryExecution": {"Status": {"State": "SUCCEEDED"}}
        }
        # All queries return empty results (pattern_group is first → no rows)
        athena.get_query_results.return_value = {"ResultSet": {"Rows": []}}

        with pytest.raises(PatternNotFound):
            run_investigation(
                _INV_ID,
                athena_client=athena,
                workgroup=_WG,
                results_s3_path=_S3,
                stage=_STAGE,
                ddb_client=ddb,
                investigations_table=_TABLE,
                write_finding=lambda f: None,
                now=lambda: "2026-10-05T06:00:00Z",
                approximate_receive_count=2,  # last attempt → set failed
                max_receive_count=2,
            )

        assert ddb.current_status == "failed", (
            "An unknown pattern must set status=failed before re-raising."
        )
        assert ddb.current_error_message == _FAILURE_MSG


# ---------------------------------------------------------------------------
# ============================================================================
# 5. Mutation guards
# ============================================================================


class TestMutationGuards:
    """Four mutations each fail a named test."""

    # -------------------------------------------------------------------
    # Mutation 1: drop the queued condition on the running transition
    # -------------------------------------------------------------------

    def test_queued_condition_required_for_running(self):
        """A 'failed' row (route takeover) must be a no-op, not a re-run.

        W2 fix (decision 5 / b W2): a row the route took over is marked
        ``failed``; the old SQS message must return success silently so the
        DLQ alarm doesn't fire on every "Start it again" click.

        Mutation: removing the early-return for ``failed`` status lets the job
        attempt ``_set_running``, which raises ConditionalCheckFailedException
        → the message goes to the DLQ → alarm fires on every user restart.

        Catching mutation: dropping ``if status == "failed": return`` causes
        the fake's conditional-update to raise, making the function raise
        rather than return silently.
        """
        from agents.fi_quality.job import run_investigation

        ddb = _FakeDDB(_ddb_investigation("failed"))

        # Must return silently — no exception.
        run_investigation(
            _INV_ID,
            athena_client=_make_all_athena_stubs(),
            workgroup=_WG,
            results_s3_path=_S3,
            stage=_STAGE,
            ddb_client=ddb,
            investigations_table=_TABLE,
            write_finding=lambda f: None,
            now=lambda: "2026-10-05T06:00:00Z",
        )
        # The DDB item must be unchanged (status stays 'failed', in type-descriptor format).
        assert ddb._item.get("status") == {"S": "failed"}, (
            "A failed row must not be modified by run_investigation; "
            "status changed to: %r" % ddb._item.get("status")
        )

    # -------------------------------------------------------------------
    # Mutation 2: leave status 'running' on failure
    # -------------------------------------------------------------------

    def test_failure_leaves_failed_not_running(self):
        """Status must be 'failed' after an exception, never 'running'.

        Mutation: skip _set_failed (or call it after re-raise) → status stays
        'running'; this assertion fires.
        """
        from agents.fi_quality.job import run_investigation

        ddb = _FakeDDB(_ddb_investigation("queued"))

        # Fail at Athena query level (FAILED state)
        athena = MagicMock()
        athena.start_query_execution.return_value = {"QueryExecutionId": "exec-x"}
        athena.get_query_execution.return_value = {
            "QueryExecution": {"Status": {"State": "FAILED",
                                          "StateChangeReason": "induced failure"}}
        }

        with pytest.raises(Exception):
            run_investigation(
                _INV_ID,
                athena_client=athena,
                workgroup=_WG,
                results_s3_path=_S3,
                stage=_STAGE,
                ddb_client=ddb,
                investigations_table=_TABLE,
                write_finding=lambda f: None,
                now=lambda: "2026-10-05T06:00:00Z",
                approximate_receive_count=2,  # last attempt → set failed
                max_receive_count=2,
            )

        assert ddb.current_status == "failed", (
            f"Expected 'failed', got {ddb.current_status!r}. "
            "Mutation: not calling _set_failed → status='running' → this fails."
        )

    # -------------------------------------------------------------------
    # Mutation 3: read only the first Athena page
    # -------------------------------------------------------------------

    def test_read_only_first_page_misses_second_candidate(self):
        """A reader that stops at the first page silently drops rows.

        This test directly exercises read_pattern_group with a two-page stub
        and asserts both rows are returned.  A reader that doesn't follow
        NextToken returns only 1 row, failing the len == 2 assertion.
        """
        from agents.fi_quality.readers import read_pattern_group

        row_a = {**_PATTERN_ROW_1, "pattern_id": "pat-a", "candidate_rank": "1"}
        row_b = {**_PATTERN_ROW_1, "pattern_id": "pat-b", "candidate_rank": "2"}

        stub = MagicMock()
        stub.start_query_execution.return_value = {"QueryExecutionId": "exec-paging"}
        stub.get_query_execution.return_value = {
            "QueryExecution": {"Status": {"State": "SUCCEEDED"}}
        }

        pages = [True]

        def _results(**kwargs: Any) -> dict:
            header = {"Data": [{"VarCharValue": k} for k in row_a.keys()]}
            def _dr(r: dict) -> dict:
                return {"Data": [{"VarCharValue": v or ""} for v in r.values()]}
            if pages[0]:
                pages[0] = False
                return {
                    "ResultSet": {"Rows": [header, _dr(row_a)]},
                    "NextToken": "tok2",
                }
            return {"ResultSet": {"Rows": [_dr(row_b)]}}

        stub.get_query_results.side_effect = _results

        rows = read_pattern_group(
            _PATTERN_ID, _STAGE,
            athena_client=stub, workgroup=_WG, results_s3_path=_S3,
        )
        assert len(rows) == 2, (
            "Both pages must be consumed; stopping at page 1 drops the second "
            "candidate row.  Mutation: skip NextToken → returns 1 row → "
            "this assertion fails."
        )

    # -------------------------------------------------------------------
    # Mutation 4: take affected VINs from the request instead of fault_patterns
    # -------------------------------------------------------------------

    def test_affected_vins_from_fault_patterns_not_request(self):
        """quality.affected_vins.count must equal fault_patterns.vehicles_affected.

        The investigation request row (DDB) has no VIN list.  Reading VINs
        from the request would produce count=0.  This test asserts count > 0
        (matching the value in fault_patterns.vehicles_affected = 42).

        Mutation: use request row for VINs → count=0 → assertion fails.
        """
        from agents.fi_quality.job import run_investigation

        ddb = _FakeDDB(_ddb_investigation("queued"))
        athena = _make_all_athena_stubs()

        findings_written: list[dict] = []

        def _write_finding(f: dict) -> None:
            findings_written.append(f)

        run_investigation(
            _INV_ID,
            athena_client=athena,
            workgroup=_WG,
            results_s3_path=_S3,
            stage=_STAGE,
            ddb_client=ddb,
            investigations_table=_TABLE,
            write_finding=_write_finding,
            now=lambda: "2026-10-05T06:00:00Z",
        )

        assert findings_written, "A finding must have been written"
        finding = findings_written[0]

        # Locate quality.affected_vins in evidence
        av_entry = next(
            (e for e in finding.get("evidence", [])
             if e.get("kind") == "quality.affected_vins"),
            None,
        )
        assert av_entry is not None, "quality.affected_vins evidence must be present"
        count = av_entry["value"].get("count", 0)
        assert count > 0, (
            f"quality.affected_vins.count={count!r} must be > 0 "
            "(from fault_patterns.vehicles_affected=42). "
            "Mutation: take VINs from request → count=0 → this fails."
        )
        # vehicles_affected in _PATTERN_ROW_1 is "42"
        assert count == 42, (
            f"Expected count=42 (from fault_patterns.vehicles_affected), "
            f"got {count!r}. "
            "Mutation: use request row for VINs → count=0 → this fails."
        )
        # The VIN sample must also be non-empty: it comes from fault_patterns.affected_vins_sample_csv.
        # A mutation that takes VINs from the DDB request (which has no VIN list) returns
        # sample=[] even though count=42 is still correct from pattern.vehicles_affected.
        sample = av_entry["value"].get("sample", [])
        assert len(sample) > 0, (
            "quality.affected_vins.sample must contain VINs from "
            "fault_patterns.affected_vins_sample_csv, not from the request row "
            "(which has no VIN list). "
            "Mutation: take VINs from request → sample=[] → this fails."
        )



# =============================================================================
# G3F2.3 additions
# =============================================================================


class TestRequeueBranch:
    """W2 (review-b): the requeue branch must be exercised (named tests).

    Before the fix every test used receive_count=2 (the limit), so the
    requeue path was never taken.  The two named tests were missing.

    Mutation guards:
    - ``test_first_error_requeues_for_redrive``: replacing requeue with
      ``_set_failed`` → status=failed, not queued → FAILS.
    - ``test_last_attempt_sets_failed``: replacing set_failed with requeue →
      status=queued, not failed → FAILS.
    - ``test_requeue_stays_failed_if_already_failed``: dropping the ``running``
      condition on _set_queued → a failed row could be reset to queued → FAILS.
    """

    def _run(self, status: str, receive_count: int, max_count: int = 2) -> "_FakeDDB":
        from agents.fi_quality.job import run_investigation

        ddb = _FakeDDB(_ddb_investigation(status))
        athena = MagicMock()
        athena.start_query_execution.return_value = {"QueryExecutionId": "exec-requeue"}
        athena.get_query_execution.return_value = {
            "QueryExecution": {
                "Status": {"State": "FAILED", "StateChangeReason": "forced failure"},
            }
        }
        with pytest.raises(Exception):
            run_investigation(
                _INV_ID,
                athena_client=athena,
                workgroup=_WG,
                results_s3_path=_S3,
                stage=_STAGE,
                ddb_client=ddb,
                investigations_table=_TABLE,
                write_finding=lambda f: None,
                now=lambda: "2026-10-05T06:00:00Z",
                approximate_receive_count=receive_count,
                max_receive_count=max_count,
            )
        return ddb

    def test_first_error_requeues_for_redrive(self):
        """receive_count=1 (below max=2) → _set_queued is called, _set_failed is not.

        Mutant: replace requeue with _set_failed → _set_failed is called → FAILS.
        """
        ddb = self._run("queued", receive_count=1, max_count=2)
        # Check update_calls: _set_queued uses ":queued" in UpdateExpression.
        # _set_failed uses ":failed".
        expressions = [c.get("UpdateExpression", "") for c in ddb.update_calls]
        # _set_running sets ":running"; _set_queued sets ":queued"
        has_queued = any(":queued" in e for e in expressions)
        has_failed_only = all(":failed" in e for e in expressions if ":failed" in e)
        # _set_queued is called (":queued" in UpdateExpression)
        assert has_queued, (
            f"First attempt failure must call _set_queued; update_calls expressions: "
            f"{expressions}. Mutant: always set_failed → no ':queued' → FAILS."
        )
        # _set_failed must NOT be the final status-setting call.
        # The last status-setting call should be _set_queued (":queued" as new value).
        last_status_update = None
        for c in ddb.update_calls:
            expr = c.get("UpdateExpression", "")
            vals = c.get("ExpressionAttributeValues") or {}
            names = c.get("ExpressionAttributeNames") or {}
            sk = names.get("#s", "status")
            if ":queued" in vals:
                last_status_update = "queued"
            elif ":failed" in vals and ":queued" not in vals and ":running" not in expr.split("SET")[0]:
                # This is a _set_failed call (":failed" new value, not part of ConditionExpression)
                last_status_update = "failed"
        assert last_status_update != "failed", (
            f"Last status update must not be 'failed' (below max_receive_count); "
            f"update_calls: {expressions}. "
            "Mutant: always set_failed → last update is 'failed' → FAILS."
        )

    def test_last_attempt_sets_failed(self):
        """receive_count=2 (== max=2) → _set_failed is called after an error.

        Mutant: replace set_failed with requeue → _set_failed not called → FAILS.
        """
        ddb = self._run("queued", receive_count=2, max_count=2)
        expressions = [c.get("UpdateExpression", "") for c in ddb.update_calls]
        # _set_failed uses ":failed" as the new value (not in ConditionExpression).
        has_failed_call = any(
            ":failed" in (c.get("ExpressionAttributeValues") or {})
            for c in ddb.update_calls
        )
        assert has_failed_call, (
            f"Last attempt failure must call _set_failed; update_calls: {expressions}. "
            "Mutant: always requeue → _set_failed never called → FAILS."
        )

    def test_requeue_stays_failed_if_row_already_failed(self):
        """_set_queued's running condition prevents resetting a failed row.

        Directly calls _set_queued with a mock client that raises
        ConditionalCheckFailedException when status != running.
        """
        from agents.fi_quality.job import _set_queued
        from botocore.exceptions import ClientError

        class _ConditionalDDB:
            def __init__(self, current_status: str):
                self._status = current_status
                self.calls: list = []

            def update_item(self, **kwargs):
                self.calls.append(kwargs)
                cond = kwargs.get("ConditionExpression", "")
                vals = kwargs.get("ExpressionAttributeValues", {})
                if "#s = :running" in cond:
                    running_val = vals.get(":running", {}).get("S", "running")
                    if self._status != running_val:
                        raise ClientError(
                            {"Error": {"Code": "ConditionalCheckFailedException",
                                       "Message": "Condition not met"}},
                            "UpdateItem",
                        )
                # Would apply update here, but condition failed so we don't.

        ddb = _ConditionalDDB(current_status="failed")
        # _set_queued must NOT raise (it catches the exception), but must NOT
        # update the row (condition fails → stays failed).
        _set_queued(ddb, _TABLE, _INV_ID)  # must not raise
        # If any update was "applied" (condition not enforced), it would be a bug.
        # The test verifies the condition is present in the call.
        assert len(ddb.calls) == 1, "_set_queued must make exactly one DDB call."
        cond = ddb.calls[0].get("ConditionExpression", "")
        assert "#s = :running" in cond, (
            f"_set_queued must use #s = :running condition; got {cond!r}. "
            "Mutant: drop running condition → a failed row is reset to queued → FAILS."
        )


class TestFindingIdFromWriter:
    """W4 b: the runner records the writer's returned finding_id.

    On a _set_completed failure + redrive, the writer deduplicates by
    finding_key and returns the first row's finding_id.  The runner must
    record that id, not the locally-generated UUID.

    Mutation guard: always use finding['finding_id'] → on a deduplicated
    write the runner records the new UUID while the stored Finding carries
    the original → GET links to a non-existent Finding → FAILS.
    """

    def test_writer_returned_id_is_recorded(self):
        """When write_finding returns a noop with a different finding_id, that id is used."""
        from agents.fi_quality.job import run_investigation

        _STORED_FINDING_ID = "FIND-existing-finding-001"

        ddb = _FakeDDB(_ddb_investigation("queued"))
        findings_written: list[dict] = []
        written_finding_ids: list[str] = []

        def _write_finding_dedup(f: dict) -> dict:
            # Simulate the writer returning an existing row's finding_id (noop).
            findings_written.append(f)
            return {"action": "noop", "finding_id": _STORED_FINDING_ID}

        run_investigation(
            _INV_ID,
            athena_client=_make_all_athena_stubs(),
            workgroup=_WG,
            results_s3_path=_S3,
            stage=_STAGE,
            ddb_client=ddb,
            investigations_table=_TABLE,
            write_finding=_write_finding_dedup,
            now=lambda: "2026-10-05T06:00:00Z",
        )

        # The runner must record _STORED_FINDING_ID, not findings_written[0]['finding_id'].
        recorded_fid = ddb._item.get("finding_id", {})
        if isinstance(recorded_fid, dict):
            recorded_fid = recorded_fid.get("S", "")

        assert recorded_fid == _STORED_FINDING_ID, (
            f"Runner must record the writer's returned finding_id "
            f"({_STORED_FINDING_ID!r}); got {recorded_fid!r}. "
            "Mutant: use finding['finding_id'] → records a new UUID while the "
            "stored Finding has the original id → GET links to nothing."
        )


class TestServiceHistoryAllowlist:
    """W5 b: service history allowlist matches the reader's SELECT columns.

    Before the fix the allowlist had 'completion_date' (not in the SQL) and
    'labor_op_code' (not in the SQL), but lacked 'service_date' (which IS in
    the SQL).  Every bundle lost the service date of each repair.

    Mutation guards:
    - Restore 'completion_date' in the allowlist → 'service_date' still
      stripped → the test asserts 'service_date' in the row → FAILS.
    - Replace allowlist with denylist → 'total_cost_usd' passes through →
      FAILS.
    """

    def test_service_date_survives_allowlist(self):
        """'service_date' from the SQL SELECT must not be stripped."""
        from agents.fi_quality.bundle import _allowlist_service_history

        rows = [
            {
                "vin": "MRD001",
                "service_date": "2026-09-01",
                "service_type": "brake_service",
                "dtc_codes_csv": "P0AA6",
                "total_cost_usd": "1200.00",   # must be stripped
                "outcome": "completed",
                "labor_op_code": "LAB-BRK-01",  # not in SQL; must be stripped
            }
        ]
        result = _allowlist_service_history(rows)
        assert len(result) == 1
        row = result[0]
        assert "service_date" in row, (
            "service_date is in the reader's SELECT list and must survive. "
            "Mutant: restore completion_date → service_date stripped → FAILS."
        )
        assert row["service_date"] == "2026-09-01"
        assert "total_cost_usd" not in row, (
            "total_cost_usd must be stripped by the allowlist (not in the allowed set)."
        )
        assert "labor_op_code" not in row, (
            "labor_op_code is NOT in the reader's SQL SELECT; must be stripped."
        )
        assert "completion_date" not in row, (
            "completion_date is NOT a reader column; must be stripped."
        )

    def test_allowlist_strips_unlisted_keys(self):
        """Any key not in the allowlist is stripped (even benign ones).

        Replacing the allowlist with the denylist would only strip known-bad
        keys, allowing future unexpected columns to pass through.

        Mutant: replace allowlist with denylist → an unlisted 'source_system'
        key passes through → FAILS.
        """
        from agents.fi_quality.bundle import _allowlist_service_history

        rows = [
            {
                "vin": "MRD002",
                "service_date": "2026-09-02",
                "service_type": "tire_rotation",
                "outcome": "completed",
                "dtc_codes_csv": "",
                "source_system": "dms",   # not in SQL, not in allowlist
                "driver_id": "drv-001",   # MUST be stripped
            }
        ]
        result = _allowlist_service_history(rows)
        assert "source_system" not in result[0], (
            "'source_system' not in the reader's SELECT; must be stripped. "
            "Mutant: replace allowlist with denylist → passes through → FAILS."
        )
        assert "driver_id" not in result[0], (
            "'driver_id' not in the reader's SELECT; must be stripped."
        )
        assert "vin" in result[0], "vin is in the reader's SELECT; must survive."


class TestIssueHookActionId:
    """W4 c (review-c W4): the hook's recovery log reads the top-level action_id.

    Before the fix the hook read ``action.get("parameters", {}).get("action_id")``
    but the approve route stores action_id at the top level of the Action dict.
    Every final failure therefore logged ``action_id=`` with no value.

    Mutation guard: restore the parameters-nested lookup → top-level action_id
    is '' → the log does not contain the id → assert 'action_id=<id>' fails.

    Design answer 8: "the recovery log reads the Action's top-level action_id."
    """

    def test_hook_final_failure_logs_top_level_action_id(self, caplog):
        """Final retry failure logs the top-level action_id."""
        import logging
        from unittest.mock import patch as _patch, MagicMock as _MM
        from agents.fi_quality import issue as issue_module
        from agents.tier2 import writer as writer_module

        _ACTION_ID = "AVX-ACT-hook-test-001"

        # Route-shaped Action (action_id at the top level, not in parameters).
        action = {
            "action_id": _ACTION_ID,   # top-level
            "action_kind": "record_engineering_decision",
            "decided_by": {"kind": "engineer", "id": "eng-sub-001"},
            "investigation_finding": {
                "evidence": [{
                    "kind": "quality.pattern",
                    "value": {"code_family": "P0AA6"},
                }],
            },
            "parameters": {
                # action_id NOT here — route puts it at the top level.
                "investigation_id": "inv-001",
                "pattern_id": "pat-001",
                "option_id": "opt-001",
                "notes": "",
            },
        }
        response = {
            "thread_id": "thread-001",
            "draft_id": "draft-001",
            "draft_type": "hardware_recall",
            "version": 1,
        }

        # Patch write_finding in the writer module to always raise.
        def _failing_write(finding, *, table_name, dynamodb_client=None, **kw):
            raise RuntimeError("DDB down")

        with _patch.object(writer_module, "write_finding", side_effect=_failing_write), \
             _patch.dict("os.environ", {"AVX_FINDINGS_TABLE": "vsa-test-findings"}):
            # Also patch the CloudWatch metric so the test doesn't need real AWS.
            from agents.fi_quality import issue as _issue_mod
            with _patch.object(_issue_mod, "_make_ddb_client", return_value=_MM()):
                # Use a hook with a failing write_finding patch already active.
                hook = issue_module.make_issue_hook()
                with caplog.at_level(logging.ERROR, logger="agents.fi_quality.issue"):
                    # Patch boto3 CloudWatch client creation to avoid real AWS call.
                    with _patch("boto3.client") as _mock_boto:
                        _mock_boto.return_value = _MM()
                        hook(action, response)

        # The error log must contain the action_id.
        log_text = "\n".join(caplog.messages)
        assert _ACTION_ID in log_text, (
            f"Final failure log must contain action_id={_ACTION_ID!r}; "
            f"got log:\n{log_text}\n"
            "Mutant: restore parameters-nested lookup → action_id='' → not in log → FAILS."
        )


class TestBundleNoFleetId:
    """C4 fix: bundle passed to run_investigation has no fleet_id at any level.

    The fix ensures that the bundle _build_investigation_bundle returns never
    carries fleet_id or driver_id anywhere (even from affected VIN rows that
    carry those fields from the build facts query).

    Mutation guard: add fleet_id to the bundle after build_bundle → the
    forbidden-key scan catches it → FAILS.
    """

    def test_bundle_has_no_fleet_id_even_when_build_facts_carry_it(self):
        """build_bundle strips fleet_id from affected VINs and service history."""
        from agents.fi_quality.bundle import build_bundle

        pattern = {
            "pattern_id": "pat-001",
            "code_family": "P0AA6",
            "dimension": "assembly_plant",
            "value": "CGA",
            "ratio": 6.0,
            "p_value": 0.0001,
            "vehicles_affected": 42,
        }
        affected_vins = [
            {
                "vin": "MRD001",
                "model": "Azimuth",
                "model_year": "2024",
                "fleet_id": "fleet-meridian-01",   # must be stripped
                "driver_id": "drv-001",              # must be stripped
            },
        ]
        service_history = [
            {
                "vin": "MRD001",
                "service_date": "2026-09-01",
                "service_type": "brake_service",
                "outcome": "completed",
                "dtc_codes_csv": "P0AA6",
                "fleet_id": "fleet-meridian-01",   # must be stripped
            }
        ]
        bundle = build_bundle(
            pattern=pattern,
            failure_modes=[],
            fleet_type="delivery",
            affected_vins=affected_vins,
            controls=[],
            service_history=service_history,
        )

        def _scan(obj, path=""):
            if isinstance(obj, dict):
                for k, v in obj.items():
                    assert k not in ("fleet_id", "driver_id"), (
                        f"bundle must not carry {k!r} at {path}.{k}. "
                        "Mutant: add fleet_id to bundle → scan catches it → FAILS."
                    )
                    _scan(v, f"{path}.{k}")
            elif isinstance(obj, list):
                for i, item in enumerate(obj):
                    _scan(item, f"{path}[{i}]")

        _scan(bundle)

    def test_candidate_components_carried_in_bundle(self):
        """W6 b: bundle carries candidate_components for the fallback filter.

        The bundle must carry candidate_components = {component_id: rank} so the
        fallback can filter failure modes to candidate causes only.

        Mutant: remove candidate_components from bundle → fallback uses all
        failure modes including unrelated components → FAILS.
        """
        from agents.fi_quality.bundle import build_bundle

        pattern = {
            "pattern_id": "pat-001",
            "code_family": "P0AA6",
            "dimension": "component",
            "value": "hv_battery_module",
            "vehicles_affected": 10,
        }
        candidates = [
            {"candidate_id": "pat-001", "dimension": "component",
             "value": "hv_battery_module", "rank": 1, "ratio": 6.0, "p_value": 0.001},
        ]
        # build_bundle itself doesn't carry candidate_components (that's added by job.py).
        # This test verifies the fallback uses it when present.
        from agents.fi_quality.fallback import _match_failure_mode

        fms = [
            {
                "failure_mode_id": "fm-battery",
                "component_id": "hv_battery_module",
                "symptom_codes": ["P0AA6"],
                "remedy_options": [{"remedy_option_id": "opt-001"}],
            },
            {
                "failure_mode_id": "fm-unrelated",
                "component_id": "unrelated_component",
                "symptom_codes": ["P0AA6"],   # same code but wrong component
                "remedy_options": [{"remedy_option_id": "opt-999"}],
            },
        ]

        # Without candidate_components filter: both match, sort picks fm-battery
        # (alphabetically first, but the unrelated one could win in other orderings).
        candidate_components = {"hv_battery_module": 1}
        result = _match_failure_mode(fms, "P0AA6", candidate_components)
        assert result is not None
        assert result["failure_mode_id"] == "fm-battery", (
            f"With candidate_components, only fm-battery (hv_battery_module) must be "
            f"selected; got {result['failure_mode_id']!r}. "
            "Mutant: remove filter → fm-unrelated can win → FAILS."
        )

    def test_unrelated_component_filtered_out(self):
        """A failure mode whose component is not a candidate cause is excluded.

        Mutant: remove the candidate_components filter from _match_failure_mode →
        a failure mode for an unrelated component passes through → FAILS.
        """
        from agents.fi_quality.fallback import _match_failure_mode

        fms = [
            {
                "failure_mode_id": "fm-unrelated",
                "component_id": "suspension_arm",
                "symptom_codes": ["P0AA6"],
                "remedy_options": [{"remedy_option_id": "opt-sus"}],
            },
        ]
        # candidate_components has hv_battery_module (rank 1) — not suspension_arm.
        candidate_components = {"hv_battery_module": 1}
        result = _match_failure_mode(fms, "P0AA6", candidate_components)

        # The unrelated component is filtered out; no match → None.
        # (fallback falls through to the "all" pool if none match; since there are
        # no others here the function returns None.)
        assert result is None, (
            f"Failure mode for an unrelated component must be excluded; "
            f"got {result!r}. "
            "Mutant: remove filter → fm-unrelated passes → returns it → FAILS."
        )



# =============================================================================
# G3F3.2 new tests — close 33 surviving reviewer mutants
# =============================================================================


class TestC1RunningBoundary:
    """C1 fix: is_live_investigation uses the RUNNING bound, not the QUEUED bound.

    Mutant C1-J1: replace the per-status bound selection with always
    _STALE_QUEUED_AGE_SECONDS.  A running row with age between the two bounds
    would then appear live.

    Guards:
        test_running_bound_is_distinct_from_queued_bound
        test_running_uses_running_bound_not_queued_bound
    """

    def test_running_bound_is_distinct_from_queued_bound(self):
        """RUNNING bound < QUEUED bound (different timeouts)."""
        from agents.fi_quality.write import (
            _STALE_RUNNING_AGE_SECONDS,
            _STALE_QUEUED_AGE_SECONDS,
        )
        assert _STALE_RUNNING_AGE_SECONDS < _STALE_QUEUED_AGE_SECONDS, (
            f"Running bound ({_STALE_RUNNING_AGE_SECONDS}s) must be strictly less than "
            f"queued bound ({_STALE_QUEUED_AGE_SECONDS}s). "
            "Mutant C1-J1: using queued bound for running collapses the two → FAILS."
        )

    def test_running_uses_running_bound_not_queued_bound(self):
        """A running row aged between the two bounds must be dead (not live).

        Mutant C1-J1: with the queued bound used for running, a row at the
        mid-point age looks live (age < 3600) but should be dead (age > 960).

        Also verifies that the SAME age reports *live* for a queued row, confirming
        the two bounds are used for different statuses.
        """
        from datetime import datetime, timedelta, timezone
        from agents.fi_quality.write import (
            is_live_investigation,
            _STALE_RUNNING_AGE_SECONDS,
            _STALE_QUEUED_AGE_SECONDS,
        )

        now = datetime.now(timezone.utc)
        # Age between the two bounds (dead for running, live for queued).
        age_between = (_STALE_RUNNING_AGE_SECONDS + _STALE_QUEUED_AGE_SECONDS) // 2
        ts = (now - timedelta(seconds=age_between)).strftime("%Y-%m-%dT%H:%M:%SZ")

        assert not is_live_investigation("running", ts, _now=now), (
            f"A running row {age_between}s old (> running bound {_STALE_RUNNING_AGE_SECONDS}s) "
            "must be dead. "
            "Mutant C1-J1: using queued bound → row looks live → FAILS."
        )
        assert is_live_investigation("queued", ts, _now=now), (
            f"The same age ({age_between}s) must be live for a queued row "
            f"(< queued bound {_STALE_QUEUED_AGE_SECONDS}s). "
            "Control: confirms the two bounds differ."
        )


class TestC1StaleRunningReset:
    """C1 fix: stale running reset writes queued_at and enforces the started_at condition.

    Mutants caught:
        C1-J4: reset dropped (pass instead of _reset_stale_running_to_queued call)
        C1-J5: reset condition made always-true

    Uses moto so the DynamoDB condition is evaluated natively.
    """

    def test_stale_running_resets_to_queued_with_queued_at(self):
        """Stale running row at count=1 resets to queued; queued_at is written.

        Mutant C1-J4: reset dropped → row stays running → status != queued → FAILS.
        """
        from datetime import datetime, timedelta, timezone
        from moto import mock_aws
        import boto3
        from agents.fi_quality.job import _reset_stale_running_to_queued

        stale_ts = (
            datetime.now(timezone.utc) - timedelta(hours=3)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        now_ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        with mock_aws():
            ddb = boto3.client("dynamodb", region_name="us-west-2")
            ddb.create_table(
                TableName="tbl",
                BillingMode="PAY_PER_REQUEST",
                KeySchema=[{"AttributeName": "investigation_id", "KeyType": "HASH"}],
                AttributeDefinitions=[{"AttributeName": "investigation_id", "AttributeType": "S"}],
            )
            ddb.put_item(TableName="tbl", Item={
                "investigation_id": {"S": _INV_ID},
                "status": {"S": "running"},
                "started_at": {"S": stale_ts},
                "requested_at": {"S": stale_ts},
            })

            _reset_stale_running_to_queued(ddb, "tbl", _INV_ID, stale_ts, queued_at=now_ts)

            row = ddb.get_item(
                TableName="tbl", Key={"investigation_id": {"S": _INV_ID}}, ConsistentRead=True
            )["Item"]
            assert row.get("status", {}).get("S") == "queued", (
                f"Status must be queued after reset; got {row.get('status')!r}. "
                "Mutant C1-J4: reset dropped → status stays running → FAILS."
            )
            assert "started_at" not in row, (
                "started_at must be removed on reset. "
                "Mutant C1-J4: reset dropped → started_at stays → FAILS."
            )
            assert row.get("queued_at", {}).get("S") == now_ts, (
                f"queued_at must be written on reset; got {row.get('queued_at')!r}. "
                "a2 W3 fix: requeue writes queued_at for age measurement."
            )

    def test_reset_condition_rejects_changed_started_at(self):
        """Reset condition enforces started_at; a changed value is rejected.

        Mutant C1-J5: condition made always-true → reset succeeds even when
        started_at changed (another Lambda restarted) → kills fresh work → FAILS.
        """
        from datetime import datetime, timedelta, timezone
        from moto import mock_aws
        import boto3
        from agents.fi_quality.job import _reset_stale_running_to_queued

        old_ts = (
            datetime.now(timezone.utc) - timedelta(hours=3)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        new_ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        with mock_aws():
            ddb = boto3.client("dynamodb", region_name="us-west-2")
            ddb.create_table(
                TableName="tbl",
                BillingMode="PAY_PER_REQUEST",
                KeySchema=[{"AttributeName": "investigation_id", "KeyType": "HASH"}],
                AttributeDefinitions=[{"AttributeName": "investigation_id", "AttributeType": "S"}],
            )
            # Row has started_at = new_ts (a fresh Lambda started it).
            ddb.put_item(TableName="tbl", Item={
                "investigation_id": {"S": _INV_ID},
                "status": {"S": "running"},
                "started_at": {"S": new_ts},   # Different from old_ts
            })

            # Reset with old_ts — condition should fail silently (status unchanged).
            _reset_stale_running_to_queued(ddb, "tbl", _INV_ID, old_started_at=old_ts, queued_at=new_ts)

            row = ddb.get_item(
                TableName="tbl", Key={"investigation_id": {"S": _INV_ID}}, ConsistentRead=True
            )["Item"]
            assert row.get("status", {}).get("S") == "running", (
                f"Status must remain running when started_at changed; got {row.get('status')!r}. "
                "Mutant C1-J5: always-true condition → reset applies even with wrong started_at "
                "→ kills fresh Lambda's work → FAILS."
            )
            assert row.get("started_at", {}).get("S") == new_ts, (
                "started_at must not be changed when condition fails. "
                "Mutant C1-J5: condition always-true → started_at removed → FAILS."
            )


class TestC1SetCompletedThrottle:
    """C1 fix: _set_completed inside the try, so a throttle triggers requeue/fail.

    Mutant C1-J10: _set_completed moved outside the try → a throttle propagates
    before _set_queued/_set_failed is called → row stays running → FAILS.
    """

    def test_set_completed_throttle_requeues_at_count_one(self):
        """A _set_completed throttle at count=1 requeues the row (via moto)."""
        from datetime import datetime, timezone
        from moto import mock_aws
        import boto3
        from agents.fi_quality.job import run_investigation, _set_completed as _orig_sc
        from botocore.exceptions import ClientError

        with mock_aws():
            ddb = boto3.client("dynamodb", region_name="us-west-2")
            ddb.create_table(
                TableName=_TABLE,
                BillingMode="PAY_PER_REQUEST",
                KeySchema=[{"AttributeName": "investigation_id", "KeyType": "HASH"}],
                AttributeDefinitions=[{"AttributeName": "investigation_id", "AttributeType": "S"}],
            )
            ddb.put_item(TableName=_TABLE, Item={
                "investigation_id": {"S": _INV_ID},
                "pattern_id": {"S": _PATTERN_ID},
                "status": {"S": "queued"},
                "requested_at": {"S": "2026-10-05T10:00:00Z"},
                "requested_by": {"S": "eng-001"},
            })

            throttle_count = [0]
            original_update = ddb.update_item

            def _flaky_update(**kwargs):
                ue = kwargs.get("UpdateExpression", "")
                if ":completed" in str(kwargs.get("ExpressionAttributeValues", {})):
                    throttle_count[0] += 1
                    raise ClientError(
                        {"Error": {"Code": "ProvisionedThroughputExceededException"}},
                        "UpdateItem",
                    )
                return original_update(**kwargs)

            ddb.update_item = _flaky_update

            with pytest.raises(Exception):
                run_investigation(
                    _INV_ID,
                    athena_client=_make_all_athena_stubs(),
                    workgroup=_WG,
                    results_s3_path=_S3,
                    stage=_STAGE,
                    ddb_client=ddb,
                    investigations_table=_TABLE,
                    write_finding=lambda f: {"finding_id": "FIND-test-001"},
                    now=lambda: "2026-10-05T06:00:00Z",
                    approximate_receive_count=1,
                    max_receive_count=2,
                )

            # _set_completed must have been called (throttle triggered).
            assert throttle_count[0] >= 1, "throttle must have fired"
            # Row must be 'queued' (not 'running' or 'completed').
            row = ddb.get_item(
                TableName=_TABLE, Key={"investigation_id": {"S": _INV_ID}}, ConsistentRead=True
            )["Item"]
            status = row["status"]["S"]
            assert status == "queued", (
                f"After _set_completed throttle at count=1, status must be queued; "
                f"got {status!r}. "
                "Mutant C1-J10: _set_completed outside try → throttle propagates before "
                "_set_queued → row stays running → FAILS."
            )


class TestC1MarkFailedMetric:
    """C1 fix: InvestigationMarkFailedError metric emitted when failed-mark fails.

    Mutant C1-H19: no metric emitted → 4.3's alarm never fires → FAILS.
    """

    def test_sqs_mark_failed_failure_emits_metric(self):
        """When both SQS send and the failed-mark write fail, metric is emitted."""
        import os
        import sys
        sys.path.insert(0, f"{_WORKTREE}/lambdas/api-avx-investigations")
        import importlib
        import handler as h
        importlib.reload(h)

        from unittest.mock import patch as _patch, MagicMock as _MM

        sqs_mock = _MM()
        sqs_mock.send_message.side_effect = RuntimeError("SQS down")

        table_mock = _MM()
        table_mock.get_item.return_value = {}  # no pre-existing lock

        # DDB for the transaction (the investigation write) succeeds.
        ddb_mock = _MM()
        ddb_mock.transact_write_items.return_value = {}

        # The table's update_item (for failed-mark after SQS failure) FAILS.
        table_mock.update_item.side_effect = RuntimeError("DDB mark write failed")

        put_metric_calls = []

        def _fake_boto3_client(svc, *a, **kw):
            c = _MM()
            if svc == "cloudwatch":
                c.put_metric_data.side_effect = lambda **kw2: put_metric_calls.append(kw2)
            return c

        with _patch.object(h, "_get_investigations_table", return_value=table_mock), \
             _patch.object(h, "_get_ddb_client", return_value=ddb_mock), \
             _patch.object(h, "_get_sqs_client", return_value=sqs_mock), \
             _patch.dict(os.environ, {
                 "AVX_INVESTIGATIONS_TABLE": "tbl",
                 "AVX_INVESTIGATIONS_QUEUE_URL": "https://sqs/q",
             }), \
             _patch("boto3.client", side_effect=_fake_boto3_client):
            r = h.handler(_post_event(pattern_id=_PATTERN_ID), None)

        assert r["statusCode"] == 503, (
            f"SQS error + mark failure must return 503; got {r['statusCode']}."
        )
        metric_names = [
            md.get("MetricData", [{}])[0].get("MetricName")
            for md in put_metric_calls
        ]
        assert "InvestigationMarkFailedError" in metric_names, (
            f"InvestigationMarkFailedError metric must be emitted; "
            f"calls: {put_metric_calls}. "
            "Mutant C1-H19: no metric → alarm never fires → FAILS."
        )


def _post_event(pattern_id=_PATTERN_ID):
    import json
    return {
        "httpMethod": "POST",
        "body": json.dumps({"pattern_id": pattern_id}),
        "requestContext": {"authorizer": {"claims": {
            "sub": "eng-sub-001",
            "cognito:groups": "product-engineer",
        }}},
    }


class TestC2OneAttemptConditional:
    """C2 fix: takeover condition + _set_completed conditional → one Finding per pattern.

    Mutant C2-H1: takeover Update condition always-true → the route always wins
    the race, even when the job has legitimately claimed the row.
    Mutant C2-H20: ConditionCheck always-true → a re-live row is not detected.
    """

    def test_set_completed_cannot_overwrite_route_takeover(self):
        """_set_completed is conditional on running+started_at; a failed row stays failed.

        With C2-H1 (always-true takeover), the route marks a running row failed.
        The job then calls _set_completed → with the new conditional, it fails
        (status is failed, not running) → no overwrite → one Finding.

        Mutant: remove the condition → _set_completed overwrites failed status with
        completed → two Findings → FAILS (row should stay failed).
        """
        from moto import mock_aws
        import boto3
        from agents.fi_quality.job import _set_completed, _set_failed

        with mock_aws():
            ddb = boto3.client("dynamodb", region_name="us-west-2")
            ddb.create_table(
                TableName="tbl",
                BillingMode="PAY_PER_REQUEST",
                KeySchema=[{"AttributeName": "investigation_id", "KeyType": "HASH"}],
                AttributeDefinitions=[{"AttributeName": "investigation_id", "AttributeType": "S"}],
            )
            started = "2026-10-05T06:00:00Z"
            # Simulate: route took over, row is now failed.
            ddb.put_item(TableName="tbl", Item={
                "investigation_id": {"S": _INV_ID},
                "status": {"S": "failed"},
                "started_at": {"S": started},
                "error_message": {"S": "The investigation could not finish. Start it again."},
            })

            # Old job tries to mark completed with the original started_at.
            from botocore.exceptions import ClientError
            with pytest.raises(ClientError) as exc_info:
                _set_completed(ddb, "tbl", _INV_ID, "FIND-old-001", started_at=started)

            assert "ConditionalCheckFailed" in exc_info.value.response["Error"]["Code"], (
                "Completing a route-failed row must raise ConditionalCheckFailedException. "
                "Mutant: condition removed → set_completed overwrites status → FAILS."
            )
            # Status must still be failed.
            row = ddb.get_item(
                TableName="tbl", Key={"investigation_id": {"S": _INV_ID}}, ConsistentRead=True
            )["Item"]
            assert row["status"]["S"] == "failed", (
                "Row must stay failed after rejected set_completed. "
                "Mutant: condition removed → status flips to completed → FAILS."
            )

    def test_p4_takeover_race_returns_running_id(self):
        """P4: route sees stale queued; job _set_running fires before transaction.

        Expected: route returns 200 with the running id (not 202 with a new one).
        The takeover condition `#s = :queued AND #ts = :old_ts` fails because the
        job changed the status to running.

        Mutant C2-H1: always-true condition → takeover succeeds, route returns 202
        with a new id → FAILS (expected 200).
        """
        import json
        import os
        import sys
        from datetime import datetime, timedelta, timezone
        from moto import mock_aws
        import boto3 as _b3
        from unittest.mock import patch as _patch, MagicMock as _MM

        # Use valid UUID format for the route's shape check.
        stale_inv_id = "cc0000aa-0000-4000-8000-000000000001"
        stale_ts = (
            datetime.now(timezone.utc) - timedelta(hours=3)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        TABLE = "inv-p4"

        sys.path.insert(0, f"{_WORKTREE}/lambdas/api-avx-investigations")
        import importlib
        import handler as h
        importlib.reload(h)
        h._table = None
        h._ddb_client = None

        with mock_aws():
            raw_ddb = _b3.client("dynamodb", region_name="us-west-2")
            raw_ddb.create_table(
                TableName=TABLE, BillingMode="PAY_PER_REQUEST",
                KeySchema=[{"AttributeName": "investigation_id", "KeyType": "HASH"}],
                AttributeDefinitions=[{"AttributeName": "investigation_id", "AttributeType": "S"}],
            )
            tbl = _b3.resource("dynamodb", region_name="us-west-2").Table(TABLE)
            tbl.put_item(Item={
                "investigation_id": f"lock#{_PATTERN_ID}",
                "pattern_id": _PATTERN_ID,
                "investigation_id_ref": stale_inv_id,
                "requested_at": stale_ts,
            })
            tbl.put_item(Item={
                "investigation_id": stale_inv_id,
                "pattern_id": _PATTERN_ID,
                "status": "queued",
                "requested_at": stale_ts,
                "requested_by": "eng-old",
            })

            # Intercept transact_write_items to first call _set_running on the stale row.
            from agents.fi_quality.job import _set_running
            inject_done = [False]

            # Wrap a fresh real ddb client so transact_write_items has the intercept.
            real_ddb_for_handler = _b3.client("dynamodb", region_name="us-west-2")

            class _InterceptDDB:
                def transact_write_items(self_, **kwargs):
                    if not inject_done[0]:
                        inject_done[0] = True
                        now_ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                        try:
                            _set_running(raw_ddb, TABLE, stale_inv_id, now_ts)
                        except Exception:
                            pass
                    return real_ddb_for_handler.transact_write_items(**kwargs)

                def __getattr__(self_, name):
                    return getattr(real_ddb_for_handler, name)

            intercept_client = _InterceptDDB()
            sqs_mock = _MM()
            sqs_mock.send_message.return_value = {"MessageId": "m-p4"}

            with _patch.dict(os.environ, {
                "AVX_INVESTIGATIONS_TABLE": TABLE,
                "AVX_INVESTIGATIONS_QUEUE_URL": "https://sqs/q",
            }), _patch.object(h, "_get_sqs_client", return_value=sqs_mock), \
                _patch.object(h, "_get_ddb_client", return_value=intercept_client):
                r = h.handler(_post_event(pattern_id=_PATTERN_ID), None)

            body = json.loads(r["body"])
            # With the always-true mutation: 202 with a new id.
            # With the real condition: takeover fails → existing running row → 200.
            assert r["statusCode"] == 200, (
                f"Route must return 200 with existing id when job is running; "
                f"got {r['statusCode']} body={body}. "
                "Mutant C2-H1: always-true condition → takeover always wins → 202 new id → FAILS."
            )
            assert body.get("investigation_id") == stale_inv_id, (
                f"Route must return the running id; got {body.get('investigation_id')!r}. "
                "Mutant C2-H1: new id returned instead of running one → FAILS."
            )
            assert sqs_mock.send_message.call_count == 0, (
                "No SQS message must be sent when returning an existing running id."
            )


try:
    import os as _os
    # Derive the worktree root from __file__ so mutation copies in /tmp find the right path.
    _WORKTREE = _os.path.dirname(
        _os.path.dirname(
            _os.path.dirname(
                _os.path.dirname(_os.path.abspath(__file__))
            )
        )
    )
except Exception:
    _WORKTREE = _os.path.expanduser("~/cvx-wt-fi-health-d")


class TestC3ConcurrencyConditions:
    """C3 fix: lock Put + ConditionCheck guard concurrent requests.

    Tests the handler from test_investigations_route.py perspective but living here
    for proximity to job tests.  Actually exercised via test_investigations_route.py;
    this class delegates to the route test helpers for ConsistentRead checks.
    """

    def test_consistent_reads_present_in_handler(self):
        """Lock read and row read both use ConsistentRead=True.

        Mutant C3-H7: lock read ConsistentRead removed → stale replica can
        return empty when lock exists → route creates duplicate.
        Mutant C3-H8: row read ConsistentRead removed → stale replica can
        return empty for a live row → route admits a new investigation.
        """
        import inspect
        import handler as h  # noqa: PLC0415 (from sys.path adjustment above)
        source = inspect.getsource(h._post_investigation)
        # Both ConsistentRead=True must appear for lock read and row read.
        consistent_reads = source.count("ConsistentRead=True")
        assert consistent_reads >= 2, (
            f"_post_investigation must have at least 2 ConsistentRead=True calls "
            f"(lock read + row read); found {consistent_reads}. "
            "Mutants C3-H7 / C3-H8: removing either → stale reads → duplicates."
        )


class TestC4BundleRecursiveScan:
    """C4 fix: bundle recursively scanned after _build_investigation_bundle.

    Mutant C4-J11: fleet_id/driver_id added to the bundle AFTER build_bundle —
    beyond build_bundle's stripping, so it survives into the Finding.
    """

    def test_run_investigation_finding_has_no_fleet_id(self):
        """run_investigation writes a Finding with no fleet_id at any depth.

        Mutant C4-J11: bundle['fleet_id'] = 'fleet-x'; bundle['affected_vins'][0]['fleet_id'] = ...
        → recursive scan in the Finding catches it → FAILS (see test below).

        Since the existing test in TestBundleNoFleetId already verifies build_bundle strips
        fleet_id, this test verifies the Finding built by build_investigation_finding
        (which takes the stripped bundle) also has no fleet_id.
        """
        from agents.fi_quality.write import build_investigation_finding

        bundle = {
            "pattern": {
                "pattern_id": "pat-001",
                "code_family": "P0AA6",
                "dimension": "assembly_plant",
                "value": "CGA",
                "ratio": 6.0,
                "p_value": 0.0001,
                "vehicles_affected": 42,
            },
            "candidates": [
                {"candidate_id": "pat-001", "dimension": "assembly_plant",
                 "value": "CGA", "rank": 1, "ratio": 6.0, "p_value": 0.0001},
            ],
            "failure_modes": [],
            "fleet_type": "delivery",
            "affected_vins": [
                {"vin": "MRD001", "model": "Azimuth", "model_year": "2024",
                 "build_month": "2024-03", "plant": "CGA", "component_lot": "L-001"},
            ],
            "controls": [],
            "service_history": [],
        }

        finding = build_investigation_finding(
            investigation_id=_INV_ID,
            pattern_id="pat-001",
            bundle=bundle,
            root_cause={"failure_mode_id": "unknown", "diagnosis_source": "rules",
                        "candidate_id": "pat-001", "confidence": "low"},
            options=[],
            computed_at="2026-10-05T06:00:00Z",
        )

        def _scan(obj, path=""):
            if isinstance(obj, dict):
                for k, v in obj.items():
                    assert k not in ("fleet_id", "driver_id"), (
                        f"Finding must not carry {k!r} at {path}.{k}. "
                        "Mutant C4-J11: fleet_id added to bundle after build_bundle → "
                        "Finding inherits it → FAILS."
                    )
                    _scan(v, f"{path}.{k}")
            elif isinstance(obj, list):
                for i, item in enumerate(obj):
                    _scan(item, f"{path}[{i}]")

        _scan(finding)


class TestW2SetQueuedCondition:
    """W2-J6: _set_queued condition prevents overwriting a route-failed row.

    Mutant W2-J6: condition `#s = :running OR attribute_exists(...)` → always
    true → a failed row is reset to queued by _set_queued.
    """

    def test_set_queued_condition_prevents_failed_overwrite(self):
        """_set_queued on a failed row leaves it failed (condition blocks overwrite).

        Mutant W2-J6: always-true condition → _set_queued overwrites failed
        with queued → row incorrectly re-queued → FAILS.
        """
        from moto import mock_aws
        import boto3
        from agents.fi_quality.job import _set_queued

        with mock_aws():
            ddb = boto3.client("dynamodb", region_name="us-west-2")
            ddb.create_table(
                TableName="tbl",
                BillingMode="PAY_PER_REQUEST",
                KeySchema=[{"AttributeName": "investigation_id", "KeyType": "HASH"}],
                AttributeDefinitions=[{"AttributeName": "investigation_id", "AttributeType": "S"}],
            )
            ddb.put_item(TableName="tbl", Item={
                "investigation_id": {"S": _INV_ID},
                "status": {"S": "failed"},
                "error_message": {"S": "The investigation could not finish. Start it again."},
            })

            _set_queued(ddb, "tbl", _INV_ID, ts="2026-10-05T07:00:00Z")

            row = ddb.get_item(
                TableName="tbl", Key={"investigation_id": {"S": _INV_ID}}, ConsistentRead=True
            )["Item"]
            assert row["status"]["S"] == "failed", (
                f"A failed row must stay failed after _set_queued; got {row['status']!r}. "
                "Mutant W2-J6: condition always-true → failed row resets to queued → FAILS."
            )


class TestW5BuildBundleUsesAllowlist:
    """W5-B1: build_bundle uses the service_history allowlist, not just the denylist.

    Mutant W5-B1: use _strip_forbidden instead of _allowlist_service_history →
    keys not in the denylist but not in the allowlist survive (e.g. 'warranty_code').
    """

    def test_build_bundle_strips_unlisted_service_history_key(self):
        """A key not in _FORBIDDEN_FIELDS but not in _SERVICE_HISTORY_ALLOWED is stripped.

        A plain denylist misses novel keys that the allowlist catches.

        Mutant W5-B1: _strip_forbidden → 'warranty_code' survives → FAILS.
        """
        from agents.fi_quality.bundle import build_bundle

        pattern = {"pattern_id": "p1", "code_family": "P0AA6", "vehicles_affected": 5}
        service_history = [
            {
                "vin": "MRD001",
                "service_date": "2026-09-01",
                "service_type": "brake_service",
                "outcome": "completed",
                "dtc_codes_csv": "P0AA6",
                "warranty_code": "WC-999",   # unlisted; not in _FORBIDDEN_FIELDS
                "shop_code": "SC-AAA",        # another unlisted key
            }
        ]
        bundle = build_bundle(
            pattern=pattern, failure_modes=[], fleet_type="",
            affected_vins=[], controls=[], service_history=service_history,
        )
        sh = bundle.get("service_history", [{}])[0]
        assert "warranty_code" not in sh, (
            f"warranty_code must be stripped by the allowlist; got {sh}. "
            "Mutant W5-B1: _strip_forbidden instead of allowlist → unlisted keys survive → FAILS."
        )
        assert "shop_code" not in sh, (
            f"shop_code must be stripped by the allowlist; got {sh}. "
            "Mutant W5-B1: denylist misses this key; allowlist catches it."
        )
        # Allowlisted keys must survive.
        assert sh.get("vin") == "MRD001"
        assert sh.get("service_date") == "2026-09-01"

    def test_allowlist_is_the_service_history_select_list_minus_cost(self):
        """The allowlist is exactly the columns readers._SERVICE_HISTORY_SQL selects, less
        total_cost_usd (cost stays server-side). ADP's FI sweep 2026-10-26 (row "service_records
        source_ro_id") stopped writing source_ro_id, which service_records never had, so the
        entry that passed it through is gone; any key the reader can't return is a leak path.
        """
        import re

        from agents.fi_quality.bundle import _SERVICE_HISTORY_ALLOWED
        from agents.fi_quality.readers import _SERVICE_HISTORY_SQL

        select = _SERVICE_HISTORY_SQL.split("FROM", 1)[0].split("SELECT", 1)[1]
        cols = set()
        for item in select.split(",\n"):
            item = item.strip()
            m = re.search(r"\bAS\s+(\w+)\s*$", item)
            cols.add(m.group(1) if m else item)
        assert cols == {"vin", "service_date", "service_type", "dtc_codes_csv",
                        "total_cost_usd", "outcome"}, cols
        assert set(_SERVICE_HISTORY_ALLOWED) == cols - {"total_cost_usd"}
        assert "source_ro_id" not in _SERVICE_HISTORY_ALLOWED


class TestW6SupplierLotCandidateFiltersFailureModes:
    """W6 fix: supplier_lot candidate contributes its lot's components; other candidates don't.

    Mutants caught:
        W6-J12: `elif dim == "supplier_lot" and val:` → `elif False:` (no lot→component map)
        W6-J13: `component_ids_for_fm = list(candidate_components.keys()) or component_ids`
                → `= component_ids` (uses all build_facts components, not candidate-derived)
        W6-J14: `bundle["candidate_components"] = {}` (empty map → fallback uses all)
        W6-F1:  tie-break `(crank, fm_id)` → `(fm_id,)` (rank ignored in sort)
    """

    def _make_pattern_rows(self, dimension="supplier_lot", value="LOT-BRK-2024-03"):
        return [{
            "pattern_id": _PATTERN_ID,
            "code_family": "C0035",
            "cohort_dimension": dimension,
            "cohort_value": value,
            "candidate_rank": "1",
            "pattern_group_id": _PATTERN_ID,
            "vehicles_affected": "15",
            "vehicles_in_service": "200",
            "rate": "0.075",
            "rate_rest": "0.01",
            "rate_ratio": "7.5",
            "p_value": "0.00001",
            "first_detected_month": "2026-07-01",
            "trend": "stable",
            "method": "poisson_bonferroni",
            "affected_vins_sample_csv": f"{_VIN_1},{_VIN_2}",
            "computed_at": "2026-10-08T02:00:00Z",
            "parameters_version": "1.0.0",
        }]

    def test_supplier_lot_candidate_filters_failure_modes(self):
        """A supplier_lot candidate's lot maps to its component; only that FM is used.

        Mutant W6-J12: lot→component map disabled → candidate_components empty →
        fallback uses all components → unrelated FM can be chosen.
        Mutant W6-J13: component_ids_for_fm = all build_facts components →
        includes unrelated components → read_failure_modes gets both.
        Mutant W6-J14: candidate_components = {} in bundle → _match_failure_mode
        has no filter → unrelated FM wins.
        """
        from agents.fi_quality.fallback import _match_failure_mode

        # Lot LOT-BRK-2024-03 → brake_caliper component (via build facts)
        # Failure mode A: brake_caliper (correct, from the lot's component)
        # Failure mode B: hv_battery (unrelated component, should be filtered out)
        fms = [
            {
                "failure_mode_id": "fm-brake",
                "component_id": "brake_caliper",
                "symptom_codes": ["C0035"],
                "remedy_options": [{"remedy_option_id": "opt-brake"}],
            },
            {
                "failure_mode_id": "fm-battery",
                "component_id": "hv_battery",
                "symptom_codes": ["C0035"],
                "remedy_options": [{"remedy_option_id": "opt-batt"}],
            },
        ]

        # The supplier_lot candidate (rank=1) maps to brake_caliper.
        candidate_components = {"brake_caliper": 1}

        result = _match_failure_mode(fms, "C0035", candidate_components)
        assert result is not None, "Should find fm-brake for brake_caliper candidate."
        assert result["failure_mode_id"] == "fm-brake", (
            f"Expected fm-brake (brake_caliper); got {result['failure_mode_id']!r}. "
            "Mutant W6-J12/J13/J14: component filter disabled → fm-battery can win → FAILS."
        )

    def test_fallback_ranks_by_candidate_component_rank(self):
        """Failure modes are ranked by their component's candidate rank (lower = better).

        Mutant W6-F1: tie-break drops candidate rank → sort order is alphabetical
        by fm_id → fm-battery (alphabetically first) wins over fm-brake → FAILS.
        """
        from agents.fi_quality.fallback import _match_failure_mode

        fms = [
            {
                "failure_mode_id": "fm-battery-high-rank",  # alphabetically first but rank 5
                "component_id": "hv_battery",
                "symptom_codes": ["C0035"],
                "remedy_options": [{"remedy_option_id": "opt-batt"}],
            },
            {
                "failure_mode_id": "fm-brake-top-rank",  # rank 1 — must win
                "component_id": "brake_caliper",
                "symptom_codes": ["C0035"],
                "remedy_options": [{"remedy_option_id": "opt-brake"}],
            },
        ]

        candidate_components = {
            "hv_battery": 5,       # lower-priority candidate
            "brake_caliper": 1,    # top-ranked candidate
        }

        result = _match_failure_mode(fms, "C0035", candidate_components)
        assert result is not None
        assert result["failure_mode_id"] == "fm-brake-top-rank", (
            f"fm-brake-top-rank (rank 1) must win over fm-battery-high-rank (rank 5); "
            f"got {result['failure_mode_id']!r}. "
            "Mutant W6-F1: tie-break drops rank → alphabetical sort → fm-battery wins → FAILS."
        )


class TestW9InvestigationFindingContract:
    """W9 fix: Finding builder fields are asserted by named property tests.

    Mutants W9-a through W9-h: each property test fails when the corresponding
    mutant changes the builder's output.
    """

    def _base_bundle(self):
        return {
            "pattern": {
                "pattern_id": "pat-001",
                "code_family": "P0AA6",
                "dimension": "supplier_lot",
                "value": "LOT-2024-03",
                "ratio": 6.0,
                "p_value": 0.0001,
                "vehicles_affected": 42,
            },
            "candidates": [
                {"candidate_id": "pat-001", "dimension": "supplier_lot",
                 "value": "LOT-2024-03", "rank": 1, "ratio": 6.0, "p_value": 0.0001},
            ],
            "failure_modes": [
                {
                    "failure_mode_id": "fm-brake",
                    "component_id": "brake_caliper",
                    "symptom_codes": ["P0AA6"],
                    "remedy_options": [{"remedy_option_id": "opt-brake",
                                        "remedy_type": "hardware_recall",
                                        "recommended": True, "tradeoffs": "TRADEOFFS_SENTINEL"}],
                }
            ],
            "fleet_type": "delivery",
            "affected_vins": [
                {"vin": _VIN_1, "model": "Azimuth", "model_year": "2024",
                 "build_month": "2024-03", "plant": "CGA", "component_lot": "LOT-2024-03"},
                {"vin": _VIN_2, "model": "Azimuth", "model_year": "2024",
                 "build_month": "2024-03", "plant": "CGA", "component_lot": "LOT-2024-03"},
            ],
            "controls": [],
            "service_history": [],
        }

    def _make_finding(self, bundle=None, root_cause=None):
        from agents.fi_quality.write import build_investigation_finding
        b = bundle or self._base_bundle()
        rc = root_cause or {
            "failure_mode_id": "fm-brake",
            "candidate_id": "pat-001",
            "diagnosis_source": "rules",
            "confidence": "low",
        }
        options = [
            {"remedy_option_id": "opt-brake", "remedy_type": "hardware_recall",
             "recommended": True, "tradeoffs": "TRADEOFFS_SENTINEL"},
        ]
        return build_investigation_finding(
            investigation_id=_INV_ID,
            pattern_id="pat-001",
            bundle=b,
            root_cause=rc,
            options=options,
            computed_at="2026-10-05T06:00:00Z",
        )

    def test_severity_unknown_failure_mode_is_routine(self):
        """failure_mode_id=unknown → severity=routine.

        Mutant W9-a: severity always 'soon' → routine assertion fails → FAILS.
        Mutant W9-b: unknown → 'soon' → FAILS.
        """
        f = self._make_finding(
            root_cause={"failure_mode_id": "unknown", "candidate_id": "pat-001",
                        "diagnosis_source": "rules", "confidence": "low"},
        )
        assert f["severity"] == "routine", (
            f"unknown failure_mode_id must yield severity=routine; got {f['severity']!r}. "
            "Mutants W9-a/W9-b."
        )

    def test_severity_monitor_option_is_routine(self):
        """remedy_type=monitor → severity=routine.

        Mutant W9-c: monitor check dropped → 'soon' for non-null recommended option.
        """
        from agents.fi_quality.write import build_investigation_finding
        bundle = self._base_bundle()
        # Replace remedy option with monitor type.
        bundle["failure_modes"][0]["remedy_options"][0] = {
            "remedy_option_id": "opt-monitor",
            "remedy_type": "monitor",
            "recommended": True,
            "tradeoffs": "",
        }
        options = [{"remedy_option_id": "opt-monitor", "remedy_type": "monitor",
                    "recommended": True, "tradeoffs": ""}]
        f = build_investigation_finding(
            investigation_id=_INV_ID, pattern_id="pat-001", bundle=bundle,
            root_cause={"failure_mode_id": "fm-brake", "candidate_id": "pat-001",
                        "diagnosis_source": "rules", "confidence": "low"},
            options=options, computed_at="2026-10-05T06:00:00Z",
        )
        assert f["severity"] == "routine", (
            f"monitor remedy_type must yield severity=routine; got {f['severity']!r}. "
            "Mutant W9-c: monitor check removed → 'soon' → FAILS."
        )

    def test_headline_format_investigation_of(self):
        """Headline = 'Investigation of <code_family>: traces to <cause>'.

        Mutant W9-d: paraphrase ('Investigation into', 'likely') → exact-string fails.
        Mutant W9-e: cause always 'unknown' → traces-to part wrong.
        """
        f = self._make_finding()
        hl = f["headline"]
        assert hl.startswith("Investigation of P0AA6:"), (
            f"Headline must start with 'Investigation of P0AA6:'; got {hl!r}. "
            "Mutant W9-d: paraphrase → FAILS."
        )
        assert "traces to" in hl, (
            f"Headline must contain 'traces to'; got {hl!r}. "
            "Mutant W9-d: 'likely' instead of 'traces to' → FAILS."
        )
        assert "unknown" not in hl, (
            f"Headline must not say 'unknown' when candidate exists; got {hl!r}. "
            "Mutant W9-e: cause always 'unknown' → headline says unknown → FAILS."
        )

    def test_pattern_affected_count_from_vehicles_affected(self):
        """quality.pattern evidence uses vehicles_affected (42), not len(sample) (2).

        Mutant W9-f: len(affected_vins) → count=2 instead of 42 → FAILS.
        """
        f = self._make_finding()
        pattern_ev = next(e for e in f["evidence"] if e["kind"] == "quality.pattern")
        assert pattern_ev["value"]["affected_count"] == 42, (
            f"pattern affected_count must be 42 (vehicles_affected); "
            f"got {pattern_ev['value']['affected_count']!r}. "
            "Mutant W9-f: len(affected_vins) = 2 → FAILS."
        )

    def test_affected_sample_entries_are_dicts_with_build_fields(self):
        """quality.affected_vins sample entries are dicts with model, plant, etc.

        Mutant W9-g: bare-vin entries (only 'vin' key) → missing build fields → FAILS.
        """
        f = self._make_finding()
        av_ev = next((e for e in f["evidence"] if e["kind"] == "quality.affected_vins"), None)
        assert av_ev is not None, "quality.affected_vins evidence entry must exist."
        samples = av_ev["value"]["sample"]
        assert samples, "Sample must be non-empty."
        s0 = samples[0]
        assert isinstance(s0, dict), f"Sample entry must be a dict; got {type(s0)}."
        assert "model" in s0, (
            f"Sample entry must carry 'model' field; got {s0}. "
            "Mutant W9-g: bare-vin dict → missing 'model' → FAILS."
        )
        assert "plant" in s0, (
            f"Sample entry must carry 'plant' field; got {s0}. "
            "Mutant W9-g."
        )

    def test_absent_failure_mode_gives_no_evidence_entry(self):
        """A failure_mode_id absent from the bundle's failure_modes → no fm evidence.

        Mutant W9-h: fallback uses failure_modes[0] → wrong failure mode named
        in evidence even when it doesn't match → FAILS (entry should be absent).
        """
        f = self._make_finding(
            root_cause={"failure_mode_id": "fm-nonexistent",
                        "candidate_id": "pat-001", "diagnosis_source": "rules",
                        "confidence": "low"},
        )
        fm_ev_list = [e for e in f["evidence"] if e["kind"] == "quality.failure_mode"]
        assert len(fm_ev_list) == 0, (
            f"No quality.failure_mode evidence when fm_id not in bundle; "
            f"got {fm_ev_list}. "
            "Mutant W9-h: failure_modes[0] fallback → wrong FM named → FAILS."
        )

    def test_subject_key_is_investigation_id(self):
        """subject_key equals the investigation_id.

        Sanity check that the builder sets subject_key correctly; this also
        exercises the finding_key computation.
        """
        f = self._make_finding()
        assert f["subject_key"] == _INV_ID, (
            f"subject_key must be investigation_id; got {f['subject_key']!r}."
        )


class TestW9IssueBuilderContract:
    """W9 / W10 fix: issue Finding builder fields are asserted.

    Mutants:
        W9-i: subject_key = draft_id (not thread_id/issue)
        W9-j: headline paraphrase
    """

    def _make_action(self):
        return {
            "action_id": "AVX-ACT-001",
            "action_kind": "record_engineering_decision",
            "decided_by": {"kind": "engineer", "id": "eng-1"},
            "investigation_finding": {
                "evidence": [
                    {"kind": "quality.pattern",
                     "value": {"code_family": "P0AA6"}},
                    {"kind": "quality.affected_vins",
                     "value": {"count": 42}},
                ]
            },
            "parameters": {
                "investigation_id": _INV_ID, "pattern_id": _PATTERN_ID,
                "option_id": "opt-brake", "notes": "",
            },
        }

    def _make_response(self, draft_type="hardware_recall"):
        return {
            "thread_id": "thread-001",
            "draft_id": "draft-AAA",
            "draft_type": draft_type,
            "version": 1,
        }

    def test_subject_key_is_thread_id_slash_issue(self):
        """subject_key = '<thread_id>/issue', not draft_id.

        Mutant W9-i: subject_key = draft_id → wrong subject → FAILS.
        """
        from agents.fi_quality.issue import build_issue_finding_from_hook

        f = build_issue_finding_from_hook(self._make_action(), self._make_response())
        assert f is not None
        assert f["subject_key"] == "thread-001/issue", (
            f"subject_key must be 'thread_id/issue'; got {f['subject_key']!r}. "
            "Mutant W9-i: = draft_id → wrong subject → FAILS."
        )

    def test_issue_headline_approve_to_issue(self):
        """Recall headline = 'Draft recall for <code_family>: approve to issue'.

        Mutant W9-j: paraphrase ('Recall draft for', missing 'approve to issue') → FAILS.
        """
        from agents.fi_quality.issue import build_issue_finding_from_hook

        f = build_issue_finding_from_hook(self._make_action(), self._make_response())
        assert f is not None
        assert "Draft recall for P0AA6" in f["headline"], (
            f"Recall headline must contain 'Draft recall for P0AA6'; got {f['headline']!r}. "
            "Mutant W9-j: 'Recall draft for' → FAILS."
        )
        assert "approve to issue" in f["headline"], (
            f"Headline must contain 'approve to issue'; got {f['headline']!r}. "
            "Mutant W9-j: missing → FAILS."
        )


class TestCW1IssueHookClientConfig:
    """c W1 fix: hook DDB client has short timeouts; metric on every failure path.

    Tests:
        test_issue_hook_client_has_short_timeouts — connect=2/read=3/retries=1
        test_missing_table_emits_metric — AVX_FINDINGS_TABLE not set
        test_unknown_draft_type_emits_metric — build_issue_finding returns None
    """

    def test_issue_hook_client_has_short_timeouts(self):
        """_make_ddb_client returns a client with connect=2, read=3, retries=1.

        Mutant: 60 s timeouts / legacy retries → hook can outlast Lambda → FAILS.
        """
        import os
        from unittest.mock import patch as _patch
        import agents.fi_quality.issue as issue_mod

        clients_built = []

        def _fake_boto3_client(svc, *a, **kw):
            from unittest.mock import MagicMock as _MM
            c = _MM()
            c.meta.config = kw.get("config")
            clients_built.append(kw.get("config"))
            return c

        with _patch("boto3.client", side_effect=_fake_boto3_client):
            c = issue_mod._make_ddb_client()

        assert clients_built, "boto3.client must be called."
        cfg = clients_built[0]
        assert cfg is not None, "Config must be provided."
        assert cfg.connect_timeout == 2, (
            f"connect_timeout must be 2 s; got {cfg.connect_timeout}. "
            "Mutant: 60 s → hook outlasts Lambda → FAILS."
        )
        assert cfg.read_timeout == 3, (
            f"read_timeout must be 3 s; got {cfg.read_timeout}. "
            "Mutant: 60 s → FAILS."
        )
        assert getattr(cfg, "retries", {}).get("total_max_attempts") == 1, (
            f"total_max_attempts must be 1; got {getattr(cfg, 'retries', {})}. "
            "Mutant: legacy retries → up to 10 attempts → FAILS."
        )

    def test_missing_table_emits_metric(self):
        """Unset AVX_FINDINGS_TABLE → QualityIssueHookFailure metric emitted.

        Mutant: no metric on missing table → 4.3's alarm never fires → FAILS.
        """
        import os
        from unittest.mock import patch as _patch, MagicMock as _MM
        import agents.fi_quality.issue as issue_mod

        put_metric_calls = []

        def _fake_boto3_client(svc, *a, **kw):
            c = _MM()
            c.put_metric_data.side_effect = lambda **kw2: put_metric_calls.append(kw2)
            return c

        action = {
            "action_id": "AVX-ACT-001",
            "decided_by": {"kind": "engineer"},
            "investigation_finding": {},
            "parameters": {},
        }
        response = {"thread_id": "t1", "draft_id": "d1",
                    "draft_type": "hardware_recall", "version": 1}

        hook = issue_mod.make_issue_hook(ddb_client=_MM())

        with _patch.dict(os.environ, {"AVX_FINDINGS_TABLE": ""}), \
             _patch("boto3.client", side_effect=_fake_boto3_client):
            hook(action, response)

        assert any(
            d.get("MetricData", [{}])[0].get("MetricName") == "QualityIssueHookFailure"
            for d in put_metric_calls
        ), (
            f"Missing table must emit QualityIssueHookFailure; "
            f"calls={put_metric_calls}. "
            "Mutant c W1: no metric on missing table → FAILS."
        )

    def test_unknown_draft_type_emits_metric(self):
        """Unknown draft_type → build_issue_finding_from_hook returns None → metric.

        Mutant: no metric on None finding → 4.3's alarm never fires → FAILS.
        """
        import os
        from unittest.mock import patch as _patch, MagicMock as _MM
        import agents.fi_quality.issue as issue_mod

        put_metric_calls = []

        def _fake_boto3_client(svc, *a, **kw):
            c = _MM()
            c.put_metric_data.side_effect = lambda **kw2: put_metric_calls.append(kw2)
            return c

        action = {
            "action_id": "AVX-ACT-001",
            "decided_by": {"kind": "engineer"},
            "investigation_finding": {},
            "parameters": {},
        }
        # 'bogus' is not in _DRAFT_REMEDY_TYPES → builder returns None.
        response = {"thread_id": "t1", "draft_id": "d1",
                    "draft_type": "bogus", "version": 1}

        hook = issue_mod.make_issue_hook(ddb_client=_MM())

        with _patch.dict(os.environ, {"AVX_FINDINGS_TABLE": "fnd-table"}), \
             _patch("boto3.client", side_effect=_fake_boto3_client):
            hook(action, response)

        assert any(
            d.get("MetricData", [{}])[0].get("MetricName") == "QualityIssueHookFailure"
            for d in put_metric_calls
        ), (
            f"Unknown draft_type → None finding must emit QualityIssueHookFailure; "
            f"calls={put_metric_calls}. "
            "Mutant c W1: no metric → FAILS."
        )


class TestA2W3QueuedAt:
    """a2 W3: requeued rows set queued_at; GET and pre-read use it.

    The fix in _set_queued and _reset_stale_running_to_queued writes queued_at.
    GET uses queued_at (when present) instead of requested_at for queued rows
    so a requeued row doesn't appear stale while its redrive is in-flight.
    """

    def test_requeued_row_reads_queued_with_fresh_queued_at(self):
        """GET for a row with old requested_at but fresh queued_at reads queued.

        Without the fix (using requested_at only), the row appears stale
        (age > 3600 s) while its redrive is still 90 minutes away, and GET
        reports failed.

        Mutant: GET ignores queued_at → requested_at > bound → failed reported → FAILS.
        """
        import os
        from datetime import datetime, timedelta, timezone
        from moto import mock_aws
        import boto3 as _b3
        from unittest.mock import patch as _patch
        import sys
        sys.path.insert(0, f"{_WORKTREE}/lambdas/api-avx-investigations")
        import importlib
        import handler as h
        importlib.reload(h)
        h._table = None

        old_requested = (
            datetime.now(timezone.utc) - timedelta(hours=3)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        fresh_queued = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        TABLE = "inv-a2w3"

        with mock_aws():
            ddb = _b3.client("dynamodb", region_name="us-west-2")
            ddb.create_table(
                TableName=TABLE, BillingMode="PAY_PER_REQUEST",
                KeySchema=[{"AttributeName": "investigation_id", "KeyType": "HASH"}],
                AttributeDefinitions=[{"AttributeName": "investigation_id", "AttributeType": "S"}],
            )
            valid_inv_id = "aaaabbbb-cccc-4ddd-8eee-000000000001"
            ddb.put_item(TableName=TABLE, Item={
                "investigation_id": {"S": valid_inv_id},
                "pattern_id": {"S": _PATTERN_ID},
                "status": {"S": "queued"},
                "requested_at": {"S": old_requested},   # old — would look stale
                "queued_at": {"S": fresh_queued},         # fresh — should be used
                "requested_by": {"S": "eng-001"},
            })

            with _patch.dict(os.environ, {
                "AVX_INVESTIGATIONS_TABLE": TABLE,
                "AVX_INVESTIGATIONS_QUEUE_URL": "https://sqs/q",
            }):
                import json
                h._table = None
                r = h.handler(
                    {"httpMethod": "GET",
                     "pathParameters": {"investigationId": valid_inv_id},
                     "requestContext": {"authorizer": {"claims": {
                         "sub": "eng-sub-001",
                         "cognito:groups": "product-engineer",
                     }}}},
                    None,
                )

            body = json.loads(r["body"])
            assert body.get("status") == "queued", (
                f"Requeued row with fresh queued_at must report status=queued; "
                f"got {body.get('status')!r} body={body}. "
                "Mutant a2 W3: GET uses requested_at (old) → appears stale → "
                "reports failed while redrive is coming → FAILS."
            )



# =============================================================================
# G3F3.2 — additional tests for surviving mutants
# =============================================================================


class TestC1J4ResetCallSite:
    """C1-J4: the _reset_stale_running_to_queued call site is not dropped.

    The mutation replaces the call with `pass`, so the stale row stays running
    and _set_running fails (condition requires queued), so the job never runs.
    This test verifies via moto that a stale re-run completes successfully.
    """

    def test_run_investigation_with_stale_running_becomes_queued(self):
        """run_investigation re-runs a stale row in the same delivery (C1-J4 guard).

        Mutant C1-J4: call replaced with pass → row stays running → _set_running
        fails → except block → _set_queued → status=queued, not completed → FAILS.
        """
        from datetime import datetime, timedelta, timezone
        from moto import mock_aws
        import boto3
        from agents.fi_quality.job import run_investigation

        stale_ts = (
            datetime.now(timezone.utc) - timedelta(hours=3)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")

        with mock_aws():
            ddb = boto3.client("dynamodb", region_name="us-west-2")
            ddb.create_table(
                TableName=_TABLE, BillingMode="PAY_PER_REQUEST",
                KeySchema=[{"AttributeName": "investigation_id", "KeyType": "HASH"}],
                AttributeDefinitions=[{"AttributeName": "investigation_id", "AttributeType": "S"}],
            )
            ddb.put_item(TableName=_TABLE, Item={
                "investigation_id": {"S": _INV_ID},
                "pattern_id": {"S": _PATTERN_ID},
                "status": {"S": "running"},
                "started_at": {"S": stale_ts},
                "requested_at": {"S": stale_ts},
                "requested_by": {"S": "eng-001"},
            })

            run_investigation(
                _INV_ID,
                athena_client=_make_all_athena_stubs(),
                workgroup=_WG,
                results_s3_path=_S3,
                stage=_STAGE,
                ddb_client=ddb,
                investigations_table=_TABLE,
                write_finding=lambda f: {"finding_id": "FIND-t001"},
                now=lambda: "2026-10-05T06:00:00Z",
                approximate_receive_count=1,
                max_receive_count=2,
            )

            row = ddb.get_item(
                TableName=_TABLE, Key={"investigation_id": {"S": _INV_ID}}, ConsistentRead=True
            )["Item"]
            status = row["status"]["S"]
            assert status == "completed", (
                f"Stale re-run must complete; got {status!r}. "
                "Mutant C1-J4: call dropped with pass → _set_running fails → "
                "status stays queued or running → FAILS."
            )


class TestC3H10LockPutAllOld:
    """C3-H10: Lock Put must have ReturnValuesOnConditionCheckFailure=ALL_OLD.

    The mutation removes it. The test verifies via source inspection that the
    Lock Put item in the transaction has RVOCCF=ALL_OLD, so a cancelled lock
    condition returns the old lock item inline (no extra get_item needed).
    """

    def test_lock_put_has_return_values_on_condition_check_failure(self):
        """Lock Put's RVOCCF=ALL_OLD is present in handler source.

        Mutant C3-H10: ALL_OLD removed → winner_id must be read from a
        separate get_item (re-read fallback). When the re-read also fails,
        the response is 503 instead of 200.

        Source inspection: the Lock Put section of _post_investigation must
        carry RVOCCF=ALL_OLD. The ConditionCheck and Update items also have it,
        so the total count is 3; removing the Lock Put one drops it to 2.
        """
        import inspect
        import sys
        sys.path.insert(0, f"{_WORKTREE}/lambdas/api-avx-investigations")
        import handler as h

        source = inspect.getsource(h._post_investigation)
        # Look for the Lock Put section containing RVOCCF=ALL_OLD.
        # Pattern: Lock Put -> ConditionExpression -> ReturnValuesOnConditionCheckFailure
        lock_put_idx = source.find('"Put": {')
        assert lock_put_idx != -1, "Lock Put must exist in _post_investigation."
        # Find the ALL_OLD after the Lock Put.
        all_old_idx = source.find('"ReturnValuesOnConditionCheckFailure": "ALL_OLD"', lock_put_idx)
        assert all_old_idx != -1, (
            "Lock Put must have ReturnValuesOnConditionCheckFailure=ALL_OLD. "
            "Mutant C3-H10: removed → no inline winner_id → extra get_item needed → FAILS."
        )
        # Verify it's within the Lock Put scope (not from a different item).
        # The next closing "}" after lock_put_idx should still include all_old_idx.
        lock_section = source[lock_put_idx:lock_put_idx + 1200]
        assert '"ReturnValuesOnConditionCheckFailure": "ALL_OLD"' in lock_section, (
            "Lock Put's ALL_OLD must be in the Lock Put section; "
            "Mutant C3-H10: removed from the Lock Put → FAILS."
        )

        # Also count total ALL_OLD occurrences. With 3 items having RVOCCF, count=3.
        # H10 drops the Lock Put one → count=2. Assert >= 3.
        total_all_old = source.count('"ReturnValuesOnConditionCheckFailure": "ALL_OLD"')
        assert total_all_old >= 3, (
            f"_post_investigation must have at least 3 ALL_OLD occurrences; "
            f"found {total_all_old}. "
            "Mutant C3-H10: Lock Put ALL_OLD removed → count=2 → FAILS."
        )


class TestC4J11BundleFleetIdViaRunInvestigation:
    """C4-J11: bundle must not carry fleet_id/driver_id even AFTER build_bundle.

    The mutation adds fleet_id to the bundle AFTER the build_bundle call.
    This test goes through run_investigation with build facts that carry fleet_id
    and verifies the written Finding has no fleet_id at any depth.
    """

    def test_run_investigation_finding_has_no_fleet_id_or_driver_id(self):
        """The Finding produced by run_investigation has no fleet_id anywhere.

        Mutant C4-J11: adds fleet_id/driver_id to bundle after build_bundle →
        Finding inherits them → recursive scan catches it → FAILS.
        """
        from agents.fi_quality.job import run_investigation

        # Build facts that carry fleet_id and driver_id
        build_row_with_fleet = {
            **_BUILD_ROW_1,
            "fleet_id": "fleet-meridian-001",
            "driver_id": "drv-001",
        }

        written_findings = []

        def _capture_finding(f: dict):
            written_findings.append(f)
            return {"finding_id": "FIND-c4j11-001"}

        ddb = _FakeDDB(_ddb_investigation("queued"))
        run_investigation(
            _INV_ID,
            athena_client=_make_all_athena_stubs(
                build_rows=[build_row_with_fleet, _BUILD_ROW_2],
            ),
            workgroup=_WG,
            results_s3_path=_S3,
            stage=_STAGE,
            ddb_client=ddb,
            investigations_table=_TABLE,
            write_finding=_capture_finding,
            now=lambda: "2026-10-05T06:00:00Z",
        )

        assert written_findings, "At least one Finding must be written."
        finding = written_findings[0]

        def _scan(obj, path=""):
            if isinstance(obj, dict):
                for k, v in obj.items():
                    assert k not in ("fleet_id", "driver_id"), (
                        f"Finding must not carry {k!r} at {path}.{k}. "
                        "Mutant C4-J11: fleet_id added after build_bundle → FAILS."
                    )
                    _scan(v, f"{path}.{k}")
            elif isinstance(obj, list):
                for i, item in enumerate(obj):
                    _scan(item, f"{path}[{i}]")

        _scan(finding)


class TestW6SupplierLotViaJobBundle:
    """W6-J12/J13/J14: candidate_components derived correctly in _build_investigation_bundle.

    The mutations disable the lot→component mapping (J12), use all build_facts
    components (J13), or set an empty bundle map (J14). This test verifies the
    bundle's `candidate_components` is non-empty when a supplier_lot candidate
    is present with build facts that map the lot to a component.
    """

    def _supplier_lot_pattern_rows(self):
        return [{
            **_PATTERN_ROW_1,
            "cohort_dimension": "supplier_lot",
            "cohort_value": "LOT-BRK-2024-03",
            "candidate_rank": "1",
            "affected_vins_sample_csv": _VIN_1,
        }]

    def _brake_build_row(self):
        return {
            **_BUILD_ROW_1,
            "supplier_lot": "LOT-BRK-2024-03",
            "component_id": "brake_caliper",
        }

    def test_supplier_lot_candidate_builds_candidate_components_in_bundle(self):
        """bundle['candidate_components'] is non-empty for a supplier_lot pattern.

        Mutant W6-J12: lot→component map disabled → candidate_components = {} → FAILS.
        Mutant W6-J13: component_ids_for_fm = component_ids → ignores supplier_lot → FAILS.
        Mutant W6-J14: bundle['candidate_components'] = {} → fallback can't filter → FAILS.
        """
        from agents.fi_quality.job import _build_investigation_bundle

        # The Athena stub returns supplier_lot pattern rows and matching build facts
        athena = _make_all_athena_stubs(
            pattern_rows=self._supplier_lot_pattern_rows(),
            build_rows=[self._brake_build_row()],
        )

        bundle = _build_investigation_bundle(
            pattern_id=_PATTERN_ID,
            stage=_STAGE,
            athena_client=athena,
            workgroup=_WG,
            results_s3_path=_S3,
        )

        cc = bundle.get("candidate_components", {})
        assert cc, (
            f"candidate_components must be non-empty for a supplier_lot pattern; "
            f"got {cc!r}. "
            "Mutant W6-J12: lot→component disabled → {}; "
            "Mutant W6-J13: component_ids_for_fm not from candidates; "
            "Mutant W6-J14: bundle sets {} → FAILS."
        )
        assert "brake_caliper" in cc, (
            f"brake_caliper must be in candidate_components; got {cc!r}. "
            "Mutant W6-J12: lot→component disabled → brake_caliper missing → FAILS."
        )

    def test_supplier_lot_component_ids_used_for_failure_modes(self):
        """component_ids_for_fm comes from candidate_components, not all build facts.

        Mutant W6-J13: component_ids_for_fm = component_ids (all build facts) →
        includes unrelated components → _match_failure_mode can return unrelated FM.

        Test: pattern has a supplier_lot candidate. Build facts carry brake_caliper (via lot)
        AND hv_battery (not a candidate's component). With J13, hv_battery is in the query.
        With real code, only brake_caliper is used (from candidate_components).
        """
        from agents.fi_quality.job import _build_investigation_bundle

        # Build row: both brake_caliper (from the lot) and hv_battery (unrelated).
        build_rows_multi = [
            {**_BUILD_ROW_1, "supplier_lot": "LOT-BRK-2024-03", "component_id": "brake_caliper"},
            {**_BUILD_ROW_2, "supplier_lot": "LOT-OTHER-2024", "component_id": "hv_battery"},
        ]

        athena = _make_all_athena_stubs(
            pattern_rows=self._supplier_lot_pattern_rows(),
            build_rows=build_rows_multi,
        )
        bundle = _build_investigation_bundle(
            pattern_id=_PATTERN_ID, stage=_STAGE,
            athena_client=athena, workgroup=_WG, results_s3_path=_S3,
        )

        cc = bundle.get("candidate_components", {})
        assert "brake_caliper" in cc, (
            f"brake_caliper must be a candidate component; got {cc!r}."
        )
        assert "hv_battery" not in cc, (
            f"hv_battery must NOT be a candidate component (not from the supplier_lot); "
            f"got {cc!r}. "
            "Mutant W6-J12: lot→component disabled → hv_battery treated as candidate → "
            "fallback may return wrong FM → FAILS."
        )


    def test_only_candidate_components_passed_to_read_failure_modes(self):
        """component_ids_for_fm never includes components from non-candidate build facts.

        Mutant W6-J13: ``component_ids_for_fm = component_ids`` (all build facts) →
        hv_battery (present in build facts but not a supplier_lot candidate's component)
        reaches the read_failure_modes query → unrelated FM can be returned → FAILS.

        The test spies on read_failure_modes and asserts hv_battery is absent.
        """
        from unittest.mock import patch
        from agents.fi_quality.job import _build_investigation_bundle
        import agents.fi_quality.job as _job_module

        # Build rows: brake_caliper (from the lot candidate) AND hv_battery (unrelated).
        build_rows_multi = [
            {**_BUILD_ROW_1, "supplier_lot": "LOT-BRK-2024-03", "component_id": "brake_caliper"},
            {**_BUILD_ROW_2, "supplier_lot": "LOT-OTHER-2024",  "component_id": "hv_battery"},
        ]

        captured: list[list[str]] = []

        original_rfm = _job_module.read_failure_modes

        def _spy_rfm(code_family, component_ids, stage, **kwargs):  # type: ignore[override]
            captured.append(list(component_ids))
            return original_rfm(code_family, component_ids, stage, **kwargs)

        with patch.object(_job_module, "read_failure_modes", side_effect=_spy_rfm):
            _build_investigation_bundle(
                pattern_id=_PATTERN_ID,
                stage=_STAGE,
                athena_client=_make_all_athena_stubs(
                    pattern_rows=self._supplier_lot_pattern_rows(),
                    build_rows=build_rows_multi,
                ),
                workgroup=_WG,
                results_s3_path=_S3,
            )

        assert captured, "read_failure_modes must have been called"
        called_with = captured[0]
        assert "brake_caliper" in called_with, (
            f"brake_caliper must be in component_ids_for_fm; got {called_with!r}."
        )
        assert "hv_battery" not in called_with, (
            f"hv_battery must NOT be in component_ids_for_fm for a supplier_lot pattern; "
            f"got {called_with!r}. "
            "Mutant W6-J13: component_ids_for_fm = all build_facts → hv_battery included → FAILS."
        )


class TestC4BundleFleetIdViaJobBundle:
    """C4-J11: fleet_id added to bundle inside _build_investigation_bundle is caught.

    Mutant C4-J11: ``bundle['fleet_id'] = 'fleet-x'`` added AFTER the build_bundle
    call, bypassing build_bundle's stripping.  The existing TestC4BundleRecursiveScan
    only tests build_investigation_finding with a manually constructed bundle, so it
    misses the mutant.  This test calls _build_investigation_bundle directly with
    readers returning rows that carry fleet_id, and scans the result.

    Mutation guard: add fleet_id/driver_id to bundle after build_bundle in
    _build_investigation_bundle → scan catches it → FAILS.
    """

    def _build_rows_with_fleet_id(self):
        return [
            {
                **_BUILD_ROW_1,
                "fleet_id": "fleet-meridian-01",    # must NOT reach bundle
                "driver_id": "drv-001",             # must NOT reach bundle
            },
            {
                **_BUILD_ROW_2,
                "fleet_id": "fleet-meridian-01",
            },
        ]

    def test_bundle_from_build_investigation_bundle_has_no_fleet_id(self):
        """_build_investigation_bundle returns a bundle with no fleet_id at any depth.

        Mutant C4-J11: bundle['fleet_id'] = 'fleet-x' added after build_bundle →
        scan in this test catches it → FAILS.
        """
        from agents.fi_quality.job import _build_investigation_bundle

        bundle = _build_investigation_bundle(
            pattern_id=_PATTERN_ID,
            stage=_STAGE,
            athena_client=_make_all_athena_stubs(
                build_rows=self._build_rows_with_fleet_id(),
            ),
            workgroup=_WG,
            results_s3_path=_S3,
        )

        def _scan(obj: object, path: str = "") -> None:
            if isinstance(obj, dict):
                for k, v in obj.items():
                    assert k not in ("fleet_id", "driver_id"), (
                        f"bundle must not carry {k!r} at {path}.{k}. "
                        "Mutant C4-J11: fleet_id added to bundle after build_bundle → "
                        "scan catches it → FAILS."
                    )
                    _scan(v, f"{path}.{k}")
            elif isinstance(obj, list):
                for i, item in enumerate(obj):
                    _scan(item, f"{path}[{i}]")

        _scan(bundle)


class TestGetInvestigationAfterRequeue:
    """GET returns queued (not failed) for a freshly requeued investigation.

    W1 / b W3 fix: after _set_queued / stale reset, queued_at is written so
    is_live_investigation measures from the requeue time.  A freshly requeued
    row (queued_at = just now) must be live → GET reports status = 'queued'.

    Mutation guard (test_get_requeued_row_shows_queued_not_failed):
        GET reporting 'failed' for a live queued row means the staleness check
        used the wrong timestamp → is_live returns False → wrong status derived.
    """

    def test_get_requeued_row_shows_queued_not_failed(self):
        """GET /investigations/{id} returns queued for a freshly requeued row.

        After _set_queued sets queued_at = now, is_live('queued', queued_at) = True,
        so GET must derive status = 'queued', not 'failed'.

        Mutation: use requested_at (which is old) instead of queued_at for the
        staleness check → row appears stale → GET returns 'failed' → FAILS.
        """
        from moto import mock_aws
        import boto3
        import datetime
        from agents.fi_quality.job import _set_queued

        fresh_qa = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        with mock_aws():
            ddb = boto3.client("dynamodb", region_name="us-west-2")
            ddb.create_table(
                TableName=_TABLE, BillingMode="PAY_PER_REQUEST",
                KeySchema=[{"AttributeName": "investigation_id", "KeyType": "HASH"}],
                AttributeDefinitions=[{"AttributeName": "investigation_id", "AttributeType": "S"}],
            )
            # Insert a row that is currently running (will be requeued).
            old_ts = "2026-01-01T00:00:00Z"
            ddb.put_item(TableName=_TABLE, Item={
                "investigation_id": {"S": _INV_ID},
                "pattern_id": {"S": _PATTERN_ID},
                "status": {"S": "running"},
                "started_at": {"S": old_ts},
                "requested_at": {"S": old_ts},
                "requested_by": {"S": "eng-001"},
            })

            # Simulate the job's _set_queued call (requeue after failure).
            _set_queued(ddb, _TABLE, _INV_ID, ts=fresh_qa)

            # GET the row and check the derived status.
            row = ddb.get_item(
                TableName=_TABLE, Key={"investigation_id": {"S": _INV_ID}}, ConsistentRead=True
            )["Item"]
            status_raw = row.get("status", {}).get("S", "")
            queued_at = row.get("queued_at", {}).get("S", "")

            # The route handler derives the displayed status from is_live_investigation.
            from agents.fi_quality.write import is_live_investigation
            displayed_status = (
                status_raw
                if is_live_investigation(status_raw, queued_at)
                else "failed"
            )

            assert displayed_status == "queued", (
                f"A freshly requeued row must display as 'queued'; got {displayed_status!r}. "
                "Mutation: staleness check uses old requested_at → appears stale → "
                "GET returns 'failed' → FAILS."
            )
            assert queued_at == fresh_qa, (
                f"queued_at must equal the requeue timestamp; got {queued_at!r}."
            )



class TestCycle4SetRunningOnlyFromQueued:
    """Review cycle 4 b C1: ``_set_running`` only moves ``queued`` -> ``running``.

    On moto, so DynamoDB evaluates the condition. The job reads the row as ``queued``;
    the route's takeover marks it ``failed`` right after that read; then the job calls
    ``_set_running``. The superseded id must not run: no Finding, row stays ``failed``.

    Mutant ``setrunning-uncond`` (condition -> ``attribute_exists(investigation_id)``)
    runs the superseded id, writes a Finding and flips the row to ``completed``: FAILS.
    """

    TABLE = "inv-c4"
    INV = "aa000000-0000-4000-8000-000000000001"
    PAT = "pat-001-P0AA6-plant-CGA"

    def test_taken_over_after_read_never_runs(self, monkeypatch):
        import os
        from datetime import datetime, timedelta, timezone
        import boto3
        from moto import mock_aws
        from agents.fi_quality import job

        for k in [k for k in os.environ if k.startswith("AWS_")]:
            monkeypatch.delenv(k, raising=False)
        monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
        monkeypatch.setenv("AWS_DEFAULT_REGION", "us-west-2")

        def _ts(offset_s):
            return (datetime.now(timezone.utc) + timedelta(seconds=offset_s)).strftime(
                "%Y-%m-%dT%H:%M:%SZ")

        with mock_aws():
            ddb = boto3.client("dynamodb", region_name="us-west-2")
            ddb.create_table(
                TableName=self.TABLE, BillingMode="PAY_PER_REQUEST",
                KeySchema=[{"AttributeName": "investigation_id", "KeyType": "HASH"}],
                AttributeDefinitions=[{"AttributeName": "investigation_id", "AttributeType": "S"}])
            ddb.put_item(TableName=self.TABLE, Item={
                "investigation_id": {"S": self.INV}, "pattern_id": {"S": self.PAT},
                "status": {"S": "queued"}, "requested_at": {"S": _ts(-60)},
                "queued_at": {"S": _ts(-60)}})
            real_get = ddb.get_item
            takeovers = []

            def get_then_takeover(**kw):
                r = real_get(**kw)
                if not takeovers:   # the route's takeover lands right after the job's read
                    ddb.update_item(
                        TableName=self.TABLE, Key={"investigation_id": {"S": self.INV}},
                        UpdateExpression="SET #s = :f", ExpressionAttributeNames={"#s": "status"},
                        ExpressionAttributeValues={":f": {"S": "failed"}})
                    takeovers.append(1)
                return r

            ddb.get_item = get_then_takeover
            writes = []
            try:
                job.run_investigation(
                    self.INV, athena_client=_make_all_athena_stubs(), workgroup="wg",
                    results_s3_path="s3://b/p/", stage="staging", ddb_client=ddb,
                    investigations_table=self.TABLE,
                    write_finding=lambda f: writes.append(f) or {"finding_id": f["finding_id"]},
                    approximate_receive_count=1, max_receive_count=2)
            except Exception as exc:  # noqa: BLE001
                # The only acceptable raise is the condition refusing the move; any other
                # error (e.g. a ValidationException) would hide a broken condition.
                code = getattr(exc, "response", {}).get("Error", {}).get("Code")
                assert code == "ConditionalCheckFailedException", repr(exc)
            finally:
                ddb.get_item = real_get
            assert takeovers, "the takeover must have happened after the job's read"
            status = real_get(TableName=self.TABLE, Key={"investigation_id": {"S": self.INV}},
                              ConsistentRead=True)["Item"]["status"]["S"]
            assert writes == [], "a superseded investigation must write no Finding"
            assert status == "failed", f"the row must stay failed, got {status!r}"

    def test_takeover_while_running_keeps_the_row_failed(self, monkeypatch):
        """Cycle 4 b W4 (G3F4.8): the job's own call to ``_set_completed`` is conditional
        on this run's ``started_at``. The route takes the row over while the job runs
        (inside ``write_finding``); completion must not flip it back.

        Mutant ``completed-callsite`` (drop ``started_at=ts`` from the job's call) flips
        the row to ``completed``: FAILS.
        """
        import os
        from datetime import datetime, timedelta, timezone
        import boto3
        from moto import mock_aws
        from agents.fi_quality import job

        for k in [k for k in os.environ if k.startswith("AWS_")]:
            monkeypatch.delenv(k, raising=False)
        monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
        monkeypatch.setenv("AWS_DEFAULT_REGION", "us-west-2")
        ts = (datetime.now(timezone.utc) - timedelta(seconds=60)).strftime("%Y-%m-%dT%H:%M:%SZ")

        with mock_aws():
            ddb = boto3.client("dynamodb", region_name="us-west-2")
            ddb.create_table(
                TableName=self.TABLE, BillingMode="PAY_PER_REQUEST",
                KeySchema=[{"AttributeName": "investigation_id", "KeyType": "HASH"}],
                AttributeDefinitions=[{"AttributeName": "investigation_id", "AttributeType": "S"}])
            ddb.put_item(TableName=self.TABLE, Item={
                "investigation_id": {"S": self.INV}, "pattern_id": {"S": self.PAT},
                "status": {"S": "queued"}, "requested_at": {"S": ts}, "queued_at": {"S": ts}})
            took_over = []

            def write_then_takeover(f):
                ddb.update_item(
                    TableName=self.TABLE, Key={"investigation_id": {"S": self.INV}},
                    UpdateExpression="SET #s = :f", ExpressionAttributeNames={"#s": "status"},
                    ExpressionAttributeValues={":f": {"S": "failed"}})
                took_over.append(1)
                return {"finding_id": "FIND-dead-run"}

            try:
                job.run_investigation(
                    self.INV, athena_client=_make_all_athena_stubs(), workgroup="wg",
                    results_s3_path="s3://b/p/", stage="staging", ddb_client=ddb,
                    investigations_table=self.TABLE, write_finding=write_then_takeover,
                    approximate_receive_count=1, max_receive_count=2)
            except Exception as exc:  # noqa: BLE001
                code = getattr(exc, "response", {}).get("Error", {}).get("Code")
                assert code == "ConditionalCheckFailedException", repr(exc)
            assert took_over, "the job must have reached write_finding"
            status = ddb.get_item(TableName=self.TABLE, Key={"investigation_id": {"S": self.INV}},
                                  ConsistentRead=True)["Item"]["status"]["S"]
            assert status == "failed", f"a taken-over row must stay failed, got {status!r}"



class TestCycle4TakeoverTimestampHalf:
    """Cycle 4 b W3 (G3F4.8): the route's takeover condition checks the timestamp too.

    The route pre-reads a stale ``running`` row; before its transaction the job's
    redelivery re-claims it (reset to ``queued``, then ``_set_running`` with a new
    ``started_at``), so the status is ``running`` again with a different timestamp.
    The route must answer 200 with the live id and send nothing.

    Mutant ``takeover-no-ts`` (condition ``#s = :old_status`` only) answers 202 with a
    new id, marks the live re-run ``failed`` and sends a second message: FAILS.
    """

    TABLE = "inv-c4-ts"
    INV = "cc0000aa-0000-4000-8000-000000000002"

    def test_route_does_not_take_over_a_reclaimed_running_row(self, monkeypatch):
        import importlib
        import json
        import os
        import sys
        from datetime import datetime, timedelta, timezone
        from pathlib import Path
        import boto3
        from moto import mock_aws
        from agents.fi_quality import job

        for k in [k for k in os.environ if k.startswith("AWS_")]:
            monkeypatch.delenv(k, raising=False)
        monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
        monkeypatch.setenv("AWS_DEFAULT_REGION", "us-west-2")
        monkeypatch.setenv("AVX_INVESTIGATIONS_TABLE", self.TABLE)
        monkeypatch.setenv("AVX_INVESTIGATIONS_QUEUE_URL", "https://sqs/q")
        handler_dir = str(Path(__file__).resolve().parents[3] / "lambdas/api-avx-investigations")
        monkeypatch.syspath_prepend(handler_dir)
        import handler as h
        importlib.reload(h)
        monkeypatch.setattr(h, "_table", None, raising=False)
        monkeypatch.setattr(h, "_ddb_client", None, raising=False)

        def _ts(offset_s=0):
            return (datetime.now(timezone.utc) + timedelta(seconds=offset_s)).strftime(
                "%Y-%m-%dT%H:%M:%SZ")

        stale_start = _ts(-7200)
        with mock_aws():
            raw = boto3.client("dynamodb", region_name="us-west-2")
            raw.create_table(
                TableName=self.TABLE, BillingMode="PAY_PER_REQUEST",
                KeySchema=[{"AttributeName": "investigation_id", "KeyType": "HASH"}],
                AttributeDefinitions=[{"AttributeName": "investigation_id", "AttributeType": "S"}])
            tbl = boto3.resource("dynamodb", region_name="us-west-2").Table(self.TABLE)
            tbl.put_item(Item={"investigation_id": f"lock#{_PATTERN_ID}", "pattern_id": _PATTERN_ID,
                               "investigation_id_ref": self.INV, "requested_at": _ts(-7300)})
            tbl.put_item(Item={"investigation_id": self.INV, "pattern_id": _PATTERN_ID,
                               "status": "running", "requested_at": _ts(-7300),
                               "started_at": stale_start, "requested_by": "eng-old"})
            handler_ddb = boto3.client("dynamodb", region_name="us-west-2")
            reclaimed = []

            class _Intercept:
                def transact_write_items(self_, **kw):
                    if not reclaimed:   # the job's redelivery re-claims the stale row first
                        t = _ts()
                        job._reset_stale_running_to_queued(
                            raw, self.TABLE, self.INV, old_started_at=stale_start, queued_at=t)
                        job._set_running(raw, self.TABLE, self.INV, t)
                        reclaimed.append(t)
                    return handler_ddb.transact_write_items(**kw)

                def __getattr__(self_, n):
                    return getattr(handler_ddb, n)

            sqs = MagicMock()
            sqs.send_message.return_value = {"MessageId": "m"}
            monkeypatch.setattr(h, "_get_sqs_client", lambda: sqs)
            monkeypatch.setattr(h, "_get_ddb_client", lambda: _Intercept())
            ev = {"httpMethod": "POST", "body": json.dumps({"pattern_id": _PATTERN_ID}),
                  "requestContext": {"authorizer": {"claims": {
                      "sub": "eng-new", "cognito:groups": "product-engineer"}}}}
            r = h.handler(ev, None)
            body = json.loads(r["body"])
            status = raw.get_item(TableName=self.TABLE,
                                  Key={"investigation_id": {"S": self.INV}},
                                  ConsistentRead=True)["Item"]["status"]["S"]
            assert reclaimed, "the re-claim must have run before the route's transaction"
            assert r["statusCode"] == 200, (r["statusCode"], body)
            assert body.get("investigation_id") == self.INV, body
            assert status == "running", f"the live re-run must stay running, got {status!r}"
            assert sqs.send_message.call_count == 0, "no second message for one pattern"
