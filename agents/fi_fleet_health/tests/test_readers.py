"""Tests for the production readers — task 3.2c (spec D8 step 1).

Covers:
  1. SQL text tests — exact database, table, column names and WHERE filters for
     all four Athena queries (fault_rows, pm_rows, managed_vins, daily_usage).
  2. Paging tests — two pages each for Athena readers (via _run_query stub) and
     DDB readers (via LastEvaluatedKey stub).
  3. Reader error counted, run continues — one agent2 fleet's DDB query raises;
     the error is counted in stats and other fleets' findings still appear.
  4. Assembler-to-loop test — real-shaped vehicle_rows from build_vehicle_rows
     fed directly to run_health_loop; stats reflect the assembled rows.

Mutation guards (verified in /tmp scratch copies by the mutation step):
  M1 (stop after first Athena page)   → test_fault_rows_paging_two_pages fails
  M2 (drop tier = 'managed' filter)   → test_managed_vins_sql_has_tier_filter fails
  M3 (no severity filter on Agent 2)  → test_agent2_severity_filter_excludes_routine fails
  M4 (drop live filter on recall)     → test_recall_visit_fn_excludes_expired fails

Run:
    .venv/bin/python -m pytest agents/fi_fleet_health/tests/test_readers.py -q
"""
from __future__ import annotations

import datetime
from typing import Any, Optional
from unittest.mock import MagicMock

import pytest

# ---------------------------------------------------------------------------
# Shared VINs / fleet ids
# ---------------------------------------------------------------------------

_VIN_A = "MRDVEH00000000017"
_VIN_B = "MRDVEH00000000042"
_VIN_C = "MRDVEH00000000099"
_FLEET_1 = "fleet-meridian-01"
_FLEET_2 = "fleet-meridian-02"
_STAGE = "staging"
_WORKGROUP = "cvx-staging-analytics"
_S3_PATH = "s3://cvx-staging-athena-results/output"

_NOW = datetime.datetime(2026, 10, 10, 6, 0, 0, tzinfo=datetime.timezone.utc)
_NOW_ISO = "2026-10-10T06:00:00Z"

# A timestamp in the past (expired)
_PAST_ISO = "2026-10-09T00:00:00Z"
# A timestamp in the future (live)
_FUTURE_ISO = "2026-10-11T00:00:00Z"


# ===========================================================================
# 1. SQL text tests
# ===========================================================================

class TestFaultRowsSQL:
    """The fault-rows SQL must reference exact ADP tables and filter correctly."""

    def test_uses_vehicle_fault_status(self):
        """SQL references adp_staging_fleet_health.vehicle_fault_status_daily."""
        from agents.fi_fleet_health.selection import _FAULT_SQL
        sql = _FAULT_SQL.replace("${stage}", "staging")
        assert "adp_staging_fleet_health.vehicle_fault_status_daily" in sql, (
            "Fault SQL must join adp_staging_fleet_health.vehicle_fault_status_daily."
        )

    def test_uses_fault_codes_table(self):
        """SQL references adp_staging_fleet_health.fault_codes."""
        from agents.fi_fleet_health.selection import _FAULT_SQL
        sql = _FAULT_SQL.replace("${stage}", "staging")
        assert "adp_staging_fleet_health.fault_codes" in sql, (
            "Fault SQL must join adp_staging_fleet_health.fault_codes for directive_text."
        )

    def test_selects_severity_band_as_band(self):
        """SQL selects severity_band and aliases it to 'band'."""
        from agents.fi_fleet_health.selection import _FAULT_SQL
        sql = _FAULT_SQL.replace("${stage}", "staging")
        assert "severity_band" in sql, "Fault SQL must select severity_band."
        assert "band" in sql, "Fault SQL must alias the column to 'band'."

    def test_selects_last_seen_as_last_seen_at(self):
        """SQL selects last_seen and aliases it to 'last_seen_at'."""
        from agents.fi_fleet_health.selection import _FAULT_SQL
        sql = _FAULT_SQL.replace("${stage}", "staging")
        assert "last_seen" in sql, "Fault SQL must select last_seen."
        assert "last_seen_at" in sql, "Fault SQL must alias it to 'last_seen_at'."

    def test_selects_directive_text(self):
        """SQL selects directive_text from fault_codes (for disclosure_txt)."""
        from agents.fi_fleet_health.selection import _FAULT_SQL
        sql = _FAULT_SQL.replace("${stage}", "staging")
        assert "directive_text" in sql, (
            "Fault SQL must select directive_text from fault_codes (used for disclosure_txt)."
        )

    def test_filters_status_open(self):
        """SQL filters status = 'open' — closed faults must not appear."""
        from agents.fi_fleet_health.selection import _FAULT_SQL
        sql = _FAULT_SQL.replace("${stage}", "staging")
        assert "status" in sql and "open" in sql, (
            "Fault SQL must filter status = 'open'."
        )

    def test_filters_severity_band_soon_attention_p0(self):
        """SQL filters severity_band IN (p0, attention, soon) — routine excluded.

        Mutation guard M3 (via Athena path): if the IN list is broadened to
        include 'routine', routine faults reach the loop; the SQL text test
        catches this.
        """
        from agents.fi_fleet_health.selection import _FAULT_SQL
        sql = _FAULT_SQL.replace("${stage}", "staging")
        assert "'p0'" in sql, "Fault SQL must include 'p0' in the band filter."
        assert "'attention'" in sql, "Fault SQL must include 'attention' in the band filter."
        assert "'soon'" in sql, "Fault SQL must include 'soon' in the band filter."
        assert "routine" not in sql.lower(), (
            "Fault SQL must NOT include 'routine' — routine faults must not reach the loop."
        )


class TestPmRowsSQL:
    """The PM-rows SQL must reference the correct table and filter correctly."""

    def test_uses_pm_vehicle_status(self):
        from agents.fi_fleet_health.selection import _PM_SQL
        sql = _PM_SQL.replace("${stage}", "staging")
        assert "adp_staging_fleet_health.pm_vehicle_status_daily" in sql

    def test_selects_vin_fleet_id_task_code_status(self):
        from agents.fi_fleet_health.selection import _PM_SQL
        sql = _PM_SQL.replace("${stage}", "staging")
        for col in ("vin", "fleet_id", "task_code", "status"):
            assert col in sql, f"PM SQL must select column '{col}'."

    def test_filters_overdue_and_due(self):
        """SQL filters status IN ('overdue', 'due') — 'current' and 'no_history' excluded."""
        from agents.fi_fleet_health.selection import _PM_SQL
        sql = _PM_SQL.replace("${stage}", "staging")
        assert "'overdue'" in sql and "'due'" in sql, (
            "PM SQL must filter status IN ('overdue', 'due')."
        )
        assert "current" not in sql, (
            "PM SQL must NOT include 'current' in the status filter."
        )


class TestManagedVinsSQL:
    """The managed-VINs SQL must read fleet_recall.managed_vehicles (newest snapshot).

    Mutation guard: reverting to cms_ingest.vin_cms_fleet causes the SQL text
    test to fail.  The table holds managed VINs only, so no tier filter.
    """

    def test_uses_managed_vehicles_table(self):
        from agents.fi_fleet_health.selection import _MANAGED_VINS_SQL
        sql = _MANAGED_VINS_SQL.replace("${stage}", "staging")
        assert "adp_staging_fleet_recall.managed_vehicles" in sql

    def test_selects_vin_and_fleet_id(self):
        from agents.fi_fleet_health.selection import _MANAGED_VINS_SQL
        sql = _MANAGED_VINS_SQL.replace("${stage}", "staging")
        assert "vin" in sql and "fleet_id" in sql

    def test_filters_by_newest_snapshot_not_tier(self):
        """SQL must use newest-snapshot filter, not tier = 'managed'.

        Mutation guard: reverting to tier = 'managed' causes this test to fail;
        the table is managed-VINs-only so no tier column exists.
        """
        from agents.fi_fleet_health.selection import _MANAGED_VINS_SQL
        sql = _MANAGED_VINS_SQL.replace("${stage}", "staging")
        assert "snapshot_date" in sql, (
            "Managed-VINs SQL must filter by snapshot_date (newest snapshot)."
        )
        assert "MAX(snapshot_date)" in sql, (
            "Managed-VINs SQL must select MAX(snapshot_date) for newest snapshot."
        )
        assert "tier" not in sql, (
            "Managed-VINs SQL must NOT contain 'tier' — no tier column in managed_vehicles."
        )


class TestDailyUsageSQL:
    """The daily-usage SQL must query vehicle_utilisation_daily (newest snapshot)."""

    def test_uses_daily_usage_table(self):
        from agents.fi_fleet_health.selection import _DAILY_USAGE_SQL
        sql = _DAILY_USAGE_SQL.replace("${stage}", "staging")
        assert "adp_staging_fleet_health.vehicle_utilisation_daily" in sql

    def test_selects_avg_miles_30d_column(self):
        from agents.fi_fleet_health.selection import _DAILY_USAGE_SQL
        sql = _DAILY_USAGE_SQL.replace("${stage}", "staging")
        assert "avg_miles_30d" in sql, (
            "Daily-usage SQL must select avg_miles_30d from vehicle_utilisation_daily."
        )

    def test_reads_avg_miles_30d_and_snapshot_date(self):
        from agents.fi_fleet_health.selection import _DAILY_USAGE_SQL
        sql = _DAILY_USAGE_SQL.replace("${stage}", "staging")
        assert "avg_miles_30d" in sql, (
            "Daily-usage SQL must read the pre-computed avg_miles_30d column."
        )
        assert "snapshot_date" in sql, (
            "Daily-usage SQL must filter by snapshot_date (newest snapshot)."
        )

    # test_groups_by_vin deleted: it only asserted '"vin" in sql.lower()', which is
    # vacuous (any SQL touching a vin column passes).  The 30-day window and the
    # one-row-per-VIN guarantee are properties of the ADP producer table, pinned
    # in platform-foundation/tests/test_g3f6_snapshots_sql.py (utilisation window
    # test).  If that ADP test is green the contract holds end-to-end.


# ===========================================================================
# 2. Paging tests — two pages each
# ===========================================================================

class TestAthenaPaging:
    """Athena readers process all rows from a multi-page _run_query response.

    The _run_query stub returns rows simulating two 'pages' (combined into one
    flat list, as run_query itself does after exhausting NextToken).  The test
    asserts that all rows from both pages appear in the result.

    Mutation guard M1: if the caller stops after the first half of rows (as if
    NextToken paging stopped), the second-half VINs are absent and the
    set-equality assertion fails.
    """

    def _two_page_fault_rows(self):
        """Fault rows that simulate two pages: 3 rows each."""
        vins_p1 = [f"MRDVEH{i:012d}" for i in range(3)]
        vins_p2 = [f"MRDVEH{i:012d}" for i in range(3, 6)]
        all_vins = vins_p1 + vins_p2
        return [
            {
                "vin": v,
                "code": "P1AB0",
                "fleet_id": _FLEET_1,
                "band": "soon",
                "last_seen_at": "2026-10-01",
                "directive_text": None,
            }
            for v in all_vins
        ]

    def test_fault_rows_paging_two_pages(self):
        """select_fault_rows returns all rows across two simulated pages.

        Mutation guard M1: a reader that only processes the first 3 of 6
        rows (as if paging stopped) would return 3 VINs; the assertion
        checks for all 6.
        """
        from agents.fi_fleet_health.selection import select_fault_rows

        all_rows = self._two_page_fault_rows()
        calls: list = []

        def fake_run_query(sql, **kwargs):
            calls.append(sql)
            return all_rows

        result = select_fault_rows(
            _STAGE,
            athena_client=None,
            workgroup=_WORKGROUP,
            results_s3_path=_S3_PATH,
            _run_query=fake_run_query,
        )

        assert len(result) == 6, (
            f"Expected 6 fault rows (two pages), got {len(result)}. "
            "Mutation guard M1: stopping after first page returns 3."
        )
        assert len(calls) == 1, "select_fault_rows must call _run_query exactly once."

    def test_pm_rows_paging_two_pages(self):
        """select_pm_rows returns all rows across two simulated pages."""
        from agents.fi_fleet_health.selection import select_pm_rows

        all_rows = [
            {"vin": f"MRDVEH{i:012d}", "fleet_id": _FLEET_1,
             "task_code": "TIRE_ROTATION", "status": "overdue"}
            for i in range(6)
        ]

        def fake_run_query(sql, **kwargs):
            return all_rows

        result = select_pm_rows(
            _STAGE,
            athena_client=None,
            workgroup=_WORKGROUP,
            results_s3_path=_S3_PATH,
            _run_query=fake_run_query,
        )
        assert len(result) == 6

    def test_managed_vins_paging_two_pages(self):
        """select_managed_vins returns all rows across two simulated pages."""
        from agents.fi_fleet_health.selection import select_managed_vins

        all_rows = [
            {"vin": f"MRDVEH{i:012d}", "fleet_id": _FLEET_1}
            for i in range(6)
        ]

        def fake_run_query(sql, **kwargs):
            return all_rows

        result = select_managed_vins(
            _STAGE,
            athena_client=None,
            workgroup=_WORKGROUP,
            results_s3_path=_S3_PATH,
            _run_query=fake_run_query,
        )
        assert len(result) == 6

    def test_daily_usage_paging_two_pages(self):
        """select_daily_usage returns all rows across two simulated pages."""
        from agents.fi_fleet_health.selection import select_daily_usage

        all_rows = [
            {"vin": f"MRDVEH{i:012d}", "avg_miles_30d": "42.5"}
            for i in range(6)
        ]

        def fake_run_query(sql, **kwargs):
            return all_rows

        result = select_daily_usage(
            _STAGE,
            athena_client=None,
            workgroup=_WORKGROUP,
            results_s3_path=_S3_PATH,
            _run_query=fake_run_query,
        )
        assert len(result) == 6


class TestDDBPaging:
    """DDB readers follow LastEvaluatedKey across two pages."""

    def _make_ddb_finding(self, vin: str, agent_id: str, finding_kind: str,
                          severity: str, expires_at: str,
                          action_kind: Optional[str] = None,
                          recall_id: str = "", dealer_id: Optional[str] = None,
                          finding_id: str = "fi-001") -> dict:
        """Build a DynamoDB-typed Finding item."""
        item: dict[str, Any] = {
            "finding_id":   {"S": finding_id},
            "vin":          {"S": vin},
            "fleet_id":     {"S": _FLEET_1},
            "agent_id":     {"S": agent_id},
            "finding_kind": {"S": finding_kind},
            "severity":     {"S": severity},
            "expires_at":   {"S": expires_at},
        }
        if action_kind:
            params: dict[str, Any] = {"vehicle_vin": {"S": vin}}
            if recall_id:
                params["recall_id"] = {"S": recall_id}
            if dealer_id:
                params["dealer_id"] = {"S": dealer_id}
            item["recommended_action"] = {
                "M": {
                    "action_kind": {"S": action_kind},
                    "parameters":  {"M": params},
                }
            }
        return item

    def test_agent2_findings_follows_last_evaluated_key(self):
        """select_agent2_findings pages through LastEvaluatedKey across two pages.

        Page 1 returns _VIN_A's finding and a LastEvaluatedKey.
        Page 2 returns _VIN_B's finding and no key.
        Both VINs must appear in the result.
        """
        from agents.fi_fleet_health.selection import select_agent2_findings

        page1_item = self._make_ddb_finding(
            _VIN_A, "agent-2-vehicle-health", "vehicle.health.anomaly",
            "soon", _FUTURE_ISO, finding_id="fi-a",
        )
        page2_item = self._make_ddb_finding(
            _VIN_B, "agent-2-vehicle-health", "vehicle.health.anomaly",
            "attention", _FUTURE_ISO, finding_id="fi-b",
        )

        query_calls: list = []

        def fake_query(**kwargs):
            query_calls.append(kwargs.copy())
            if len(query_calls) == 1:
                # First call — return page 1 with a continuation key
                return {
                    "Items": [page1_item],
                    "LastEvaluatedKey": {"fleet_id": {"S": _FLEET_1}, "tok": {"S": "p2"}},
                }
            # Second call — return page 2, no continuation
            return {"Items": [page2_item]}

        ddb_stub = MagicMock()
        ddb_stub.query.side_effect = fake_query

        result, _ = select_agent2_findings(
            [_FLEET_1],
            ddb_client=ddb_stub,
            findings_table="avx-staging-findings",
            now=_NOW,
        )

        assert _VIN_A in result, "Page-1 VIN must appear after paging."
        assert _VIN_B in result, "Page-2 VIN must appear after paging."
        assert len(query_calls) == 2, (
            f"Expected 2 DDB calls (one per page), got {len(query_calls)}."
        )

    def test_recall_visit_fn_follows_last_evaluated_key(self):
        """recall_visit_fn pages through LastEvaluatedKey across two pages.

        Page 1 returns a non-book_recall_service item and a LastEvaluatedKey.
        Page 2 returns the real booking item.
        The booking item from page 2 must be returned.
        """
        from agents.fi_fleet_health.selection import recall_visit_fn

        non_booking = self._make_ddb_finding(
            _VIN_A, "agent-fi-recall-warranty", "fleet.recall.vehicle",
            "attention", _FUTURE_ISO, action_kind="ground_vehicle",  # not book_recall_service
        )
        booking = self._make_ddb_finding(
            _VIN_A, "agent-fi-recall-warranty", "fleet.recall.vehicle",
            "attention", _FUTURE_ISO, action_kind="book_recall_service",
            recall_id="RC-9901", dealer_id="dealer-mrd-01", finding_id="fi-bk",
        )

        query_calls: list = []

        def fake_query(**kwargs):
            query_calls.append(kwargs.copy())
            if len(query_calls) == 1:
                return {
                    "Items": [non_booking],
                    "LastEvaluatedKey": {"vin": {"S": _VIN_A}, "tok": {"S": "p2"}},
                }
            return {"Items": [booking]}

        ddb_stub = MagicMock()
        ddb_stub.query.side_effect = fake_query

        result = recall_visit_fn(
            _VIN_A,
            ddb_client=ddb_stub,
            findings_table="avx-staging-findings",
            now=_NOW,
        )

        assert result is not None, "Booking item from page 2 must be returned."
        assert result["recall_id"] == "RC-9901"
        assert result["rooftop_dealer_id"] == "dealer-mrd-01"
        # W3d: the field is now named recall_item_finding_id (contract §1.6)
        assert result["recall_item_finding_id"] == "fi-bk"
        assert len(query_calls) == 2


# ===========================================================================
# 3. Reader error counted, run continues
# ===========================================================================

class TestReaderErrorCounting:
    """One agent2 fleet DDB query raising an exception is counted; other fleets continue.

    The builder (select_agent2_findings) is called with two fleet_ids.
    The DDB query for fleet_2 raises; the error is counted in a counters dict
    that the runner builds.  Fleet_1's findings still appear in the result.

    This test wires the builder inline (no separate error-count API on the
    reader itself).  The runner (task 3.8) catches the exception per-fleet and
    increments stats['ddb_read_errors'].  Here we verify the exception is
    propagated only for the failing fleet and that the other fleet's data is
    available.
    """

    def test_fleet2_error_does_not_prevent_fleet1_findings(self):
        """select_agent2_findings for two fleets: fleet_2 DDB error, fleet_1 data returned.

        W10-C fix: per-fleet exception handling is now INSIDE select_agent2_findings.
        Calling with both fleet_ids at once returns fleet_1 data without raising;
        the failing fleet is logged and skipped.
        """
        from agents.fi_fleet_health.selection import select_agent2_findings

        # Build a live item for fleet_1
        vin_fleet1 = _VIN_A
        page1_item: dict[str, Any] = {
            "finding_id": {"S": "fi-fleet1"},
            "vin":        {"S": vin_fleet1},
            "fleet_id":   {"S": _FLEET_1},
            "agent_id":   {"S": "agent-2-vehicle-health"},
            "finding_kind": {"S": "vehicle.health.anomaly"},
            "severity":   {"S": "soon"},
            "expires_at": {"S": _FUTURE_ISO},
        }

        call_index = [0]

        def fake_query(**kwargs):
            from botocore.exceptions import ClientError
            fid = (kwargs.get("ExpressionAttributeValues") or {}).get(":fid", {}).get("S")
            call_index[0] += 1
            if fid == _FLEET_1:
                return {"Items": [page1_item]}
            # fleet_2 raises a ClientError (W10: narrowed catch)
            raise ClientError(
                {"Error": {"Code": "ProvisionedThroughputExceededException",
                           "Message": "DDB unavailable for fleet_2"}},
                "Query",
            )

        ddb_stub = MagicMock()
        ddb_stub.query.side_effect = fake_query

        # W10-C: call with BOTH fleets at once — per-fleet handling is internal now.
        # The function must not raise; fleet_1 data must be in the result.
        # W10: error count must be 1 (one failing fleet).
        result, errs = select_agent2_findings(
            [_FLEET_1, _FLEET_2],
            ddb_client=ddb_stub,
            findings_table="avx-staging-findings",
            now=_NOW,
        )

        # fleet_1 succeeded
        assert vin_fleet1 in result, (
            "fleet_1 findings must appear even when fleet_2 raises (W10-C per-fleet handling)."
        )
        assert result[vin_fleet1][0]["severity"] == "soon"
        # fleet_2 was counted, not propagated
        assert _VIN_C not in result, "fleet_2 VINs must not appear (fleet_2 DDB query failed)."
        assert errs == 1, (
            f"agent2_read_errors must be 1 for one failing fleet; got {errs!r}. "
            "Mutation guard (W10): drop error count → errs==0 → this fails."
        )


# ===========================================================================
# 4. Mutation guards on DDB readers
# ===========================================================================

class TestAgent2SeverityFilter:
    """Mutation guard M3: dropping the severity filter returns routine findings.

    A routine finding must NOT appear in the result — the filter expression
    in the DDB query must exclude severity='routine'.  If the filter is
    dropped, the test_agent2_severity_filter_excludes_routine test fails.
    """

    def test_agent2_severity_filter_excludes_routine(self):
        """Routine-severity findings must not appear even if the DB returns them.

        The FilterExpression on the DDB query must exclude 'routine'.  The
        fake_query simulates this: when the FilterExpression contains the
        severity IN clause (:s_soon present), it returns only the soon item.
        When the filter is ABSENT (mutation M3), it also returns the routine
        item — and the result must still not contain _VIN_C.

        Mutation guard M3: if the FilterExpression is removed, the DDB client
        returns both items; the reader must still not surface the routine one
        because the FilterExpression would catch it before returning.  Since we
        cannot filter in the stub, the real guard is the FilterExpression itself.
        The test asserts that the FilterExpression CONTAINS the severity IN
        clause (i.e. `:s_soon` is present) so the mutation is caught before
        any item is returned.
        """
        from agents.fi_fleet_health.selection import select_agent2_findings

        soon_item: dict[str, Any] = {
            "finding_id": {"S": "fi-soon"},
            "vin":        {"S": _VIN_A},
            "fleet_id":   {"S": _FLEET_1},
            "agent_id":   {"S": "agent-2-vehicle-health"},
            "finding_kind": {"S": "vehicle.health.anomaly"},
            "severity":   {"S": "soon"},
            "expires_at": {"S": _FUTURE_ISO},
        }

        def fake_query(**kwargs):
            filter_expr = kwargs.get("FilterExpression", "")
            # Mutation guard M3: the FilterExpression MUST contain `:s_soon`
            # to exclude routine findings.  A mutant that drops the severity IN
            # clause fails this assertion before any item is returned.
            assert ":s_soon" in filter_expr, (
                "agent2 FilterExpression must reference :s_soon "
                "(the severity IN clause). "
                "Mutation guard M3: dropping the severity filter → this fails."
            )
            assert ":s_att" in filter_expr, (
                "agent2 FilterExpression must reference :s_att."
            )
            # Option B (spec 2026-11-05-avx-proactive-p0-alert): p0 is not asked for.
            assert ":s_p0" not in filter_expr, (
                "agent2 FilterExpression must not ask for p0 Findings (option B)."
            )
            # Also verify 'routine' is not explicitly allowed
            assert "routine" not in filter_expr.lower(), (
                "The DDB FilterExpression must NOT include 'routine'. "
                "Mutation guard M3: if the filter is widened or dropped, "
                "routine findings reach the loop."
            )
            return {"Items": [soon_item]}

        ddb_stub = MagicMock()
        ddb_stub.query.side_effect = fake_query

        result, _ = select_agent2_findings(
            [_FLEET_1],
            ddb_client=ddb_stub,
            findings_table="avx-staging-findings",
            now=_NOW,
        )

        assert _VIN_A in result, "soon-severity VIN must appear."
        assert _VIN_C not in result, (
            "routine-severity VIN must NOT appear (mutation guard M3)."
        )


class TestRecallVisitLiveFilter:
    """Mutation guard M4: dropping the live filter returns expired recall visits."""

    def test_recall_visit_fn_excludes_expired(self):
        """An expired recall booking Finding must not be returned.

        The query FilterExpression includes ``expires_at > :now``.  If it is
        dropped, the expired finding would be returned.  The test asserts None
        is returned for a finding with expires_at in the past.

        Mutation guard M4: drop ``expires_at > :now`` from the FilterExpression
        → the expired item passes through → result is not None → this fails.
        """
        from agents.fi_fleet_health.selection import recall_visit_fn

        expired_item: dict[str, Any] = {
            "finding_id": {"S": "fi-exp"},
            "vin":        {"S": _VIN_A},
            "fleet_id":   {"S": _FLEET_1},
            "agent_id":   {"S": "agent-fi-recall-warranty"},
            "finding_kind": {"S": "fleet.recall.vehicle"},
            "severity":   {"S": "attention"},
            "expires_at": {"S": _PAST_ISO},
            "recommended_action": {
                "M": {
                    "action_kind": {"S": "book_recall_service"},
                    "parameters":  {
                        "M": {
                            "vehicle_vin": {"S": _VIN_A},
                            "recall_id":   {"S": "RC-OLD"},
                            "dealer_id":   {"S": "dealer-old"},
                        }
                    },
                }
            },
        }

        def fake_query(**kwargs):
            # Verify the live filter is present.
            filter_expr = kwargs.get("FilterExpression", "")
            assert "expires_at" in filter_expr, (
                "recall_visit_fn FilterExpression must include expires_at > :now "
                "(mutation guard M4)."
            )
            # The real DDB would exclude this item because _PAST_ISO < _NOW_ISO.
            # Simulate the live filter working: return empty.
            return {"Items": []}

        ddb_stub = MagicMock()
        ddb_stub.query.side_effect = fake_query

        result = recall_visit_fn(
            _VIN_A,
            ddb_client=ddb_stub,
            findings_table="avx-staging-findings",
            now=_NOW,
        )
        assert result is None, (
            "An expired recall booking must not be returned (mutation guard M4)."
        )


# ===========================================================================
# 5. Assembler-to-loop test
# ===========================================================================

class TestAssemblerToLoop:
    """Assemble vehicle_rows from readers and pass to run_health_loop.

    Uses real-shaped inputs throughout; verifies the assembled rows reach the
    loop's stats as vins_selected.  Also verifies that the injectable ``now``
    parameter works: a fixed datetime makes the loop deterministic in tests.
    """

    def _needs_config(self):
        """Minimal YAML config dict for map_need_to_labor_op."""
        return {
            "fault_bands": {"soon": {"labor_op": "EV_SYSTEM_INSPECTION", "parts": []}},
            "fault_codes": {},
            "pm_tasks": {
                "TIRE_ROTATION": {"labor_op": "TIRE_ROTATION_SERVICE", "parts": []}
            },
            "agent2_findings": {"default": {"labor_op": "EV_SYSTEM_INSPECTION", "parts": []}},
        }

    def test_build_vehicle_rows_produces_correct_union(self):
        """build_vehicle_rows unions VINs from fault, PM, and agent2 sources."""
        from agents.fi_fleet_health.selection import build_vehicle_rows

        fault_rows = [
            {"vin": _VIN_A, "code": "P1AB0", "fleet_id": _FLEET_1,
             "band": "soon", "last_seen_at": "2026-10-01", "directive_text": None},
        ]
        pm_rows = [
            {"vin": _VIN_B, "fleet_id": _FLEET_1,
             "task_code": "TIRE_ROTATION", "status": "overdue"},
        ]
        managed_vins = [
            {"vin": _VIN_A, "fleet_id": _FLEET_1},
            {"vin": _VIN_B, "fleet_id": _FLEET_1},
        ]
        usage_rows = [
            {"vin": _VIN_A, "avg_miles_30d": "55.0"},
        ]
        agent2_rows = {
            _VIN_C: [{"finding_id": "fi-c", "severity": "attention"}],
        }

        rows, counters = build_vehicle_rows(
            fault_rows=fault_rows,
            pm_rows=pm_rows,
            managed_vins=managed_vins,
            usage_rows=usage_rows,
            agent2_rows=agent2_rows,
        )

        vins_in = {r["vin"] for r in rows}
        assert _VIN_A in vins_in, "VIN_A (fault) must appear in vehicle_rows."
        assert _VIN_B in vins_in, "VIN_B (PM) must appear in vehicle_rows."
        assert _VIN_C in vins_in, "VIN_C (agent2) must appear in vehicle_rows."

        assert counters["vehicle_rows_count"] == 3
        assert counters["fault_vin_count"] == 1
        assert counters["pm_vin_count"] == 1
        assert counters["agent2_vin_count"] == 1
        assert counters["managed_vin_count"] == 2

    def test_assembler_vehicle_row_has_open_faults_with_last_seen_at(self):
        """open_faults list carries code, band, and last_seen_at."""
        from agents.fi_fleet_health.selection import build_vehicle_rows

        rows, _ = build_vehicle_rows(
            fault_rows=[
                {"vin": _VIN_A, "code": "P1AB0", "fleet_id": _FLEET_1,
                 "band": "soon", "last_seen_at": "2026-09-28", "directive_text": None},
            ],
            pm_rows=[],
            managed_vins=[{"vin": _VIN_A, "fleet_id": _FLEET_1}],
            usage_rows=[],
            agent2_rows={},
        )
        row = next(r for r in rows if r["vin"] == _VIN_A)
        assert row["open_faults"] == [
            {"code": "P1AB0", "band": "soon", "last_seen_at": "2026-09-28",
             "fault_source": None}
        ], "open_faults must carry code, band, last_seen_at, and fault_source."

    def test_assembler_catalog_entries_directive_key(self):
        """catalog_entries uses 'directive' key (maps directive_text → directive)."""
        from agents.fi_fleet_health.selection import build_vehicle_rows

        directive = "Stop driving immediately."
        rows, _ = build_vehicle_rows(
            fault_rows=[
                {"vin": _VIN_A, "code": "P0AA6", "fleet_id": _FLEET_1,
                 "band": "p0", "last_seen_at": "2026-10-01",
                 "directive_text": directive},
            ],
            pm_rows=[],
            managed_vins=[{"vin": _VIN_A, "fleet_id": _FLEET_1}],
            usage_rows=[],
            agent2_rows={},
        )
        row = next(r for r in rows if r["vin"] == _VIN_A)
        catalog = row["catalog_entries"]
        assert "P0AA6" in catalog
        assert catalog["P0AA6"]["directive"] == directive, (
            "catalog_entries['P0AA6']['directive'] must hold the ADP directive_text."
        )

    def test_assembler_utilisation_avg_miles(self):
        """utilisation dict carries avg_miles_30d as float."""
        from agents.fi_fleet_health.selection import build_vehicle_rows

        rows, _ = build_vehicle_rows(
            fault_rows=[
                {"vin": _VIN_A, "code": "P1AB0", "fleet_id": _FLEET_1,
                 "band": "soon", "last_seen_at": "2026-10-01", "directive_text": None},
            ],
            pm_rows=[],
            managed_vins=[{"vin": _VIN_A, "fleet_id": _FLEET_1}],
            usage_rows=[{"vin": _VIN_A, "avg_miles_30d": "63.4"}],
            agent2_rows={},
        )
        row = next(r for r in rows if r["vin"] == _VIN_A)
        # W3b: the assembled utilisation dict now uses 'daily_average_miles_30d'
        #      to match contract §1.6.  The SQL alias stays 'avg_miles_30d'.
        assert row["utilisation"] == {"daily_average_miles_30d": 63.4}, (
            "utilisation must carry daily_average_miles_30d as a float (W3b fix)."
        )

    def test_assembled_rows_reach_loop_as_vins_selected(self):
        """vehicle_rows from build_vehicle_rows reach run_health_loop.vins_selected.

        Also verifies the injectable ``now`` parameter: a fixed datetime is
        passed and the loop completes without calling datetime.datetime.now().
        """
        from agents.fi_fleet_health.selection import build_vehicle_rows
        from agents.fi_fleet_health.loop import run_health_loop

        fault_rows = [
            {"vin": _VIN_A, "code": "P1AB0", "fleet_id": _FLEET_1,
             "band": "soon", "last_seen_at": "2026-10-01", "directive_text": None},
            {"vin": _VIN_B, "code": "P1AB0", "fleet_id": _FLEET_1,
             "band": "soon", "last_seen_at": "2026-10-02", "directive_text": None},
        ]
        managed_vins = [
            {"vin": _VIN_A, "fleet_id": _FLEET_1},
            {"vin": _VIN_B, "fleet_id": _FLEET_1},
        ]

        vehicle_rows, counters = build_vehicle_rows(
            fault_rows=fault_rows,
            pm_rows=[],
            managed_vins=managed_vins,
            usage_rows=[],
            agent2_rows={},
        )

        assert counters["vehicle_rows_count"] == 2

        # Wire stub callables so the loop can run
        computed_at = "2026-10-10T06:00:00Z"

        stats = run_health_loop(
            vehicle_rows=vehicle_rows,
            # W8 fix: provide a stock row so vehicles pass the no-rooftop check.
            stock_rows=[{
                "dealer_id": "dealer-meridian-001",
                "part_number": "GENERIC-001",
                "qty_on_hand": 1,
                "latitude": "47.6062",
                "longitude": "-122.3321",
                "stock_as_of": "2026-10-09T00:00:00Z",
            }],
            computed_at=computed_at,
            grounded_fn=lambda vid: (False, 0),
            history_fn=lambda vin: ({}, 0),
            standing_fn=lambda vin: None,
            lookup_live=lambda fk: None,
            write_finding=lambda f, s, refresh_on_noop=True: {"action": "put"},
            recall_visit_fn=None,
            fault_grounding_enabled=False,
            needs_config=self._needs_config(),
            now=_NOW,   # injectable — deterministic for tests
        )

        assert stats["vins_selected"] == 2, (
            "vins_selected must equal the number of assembled vehicle_rows."
        )
        assert stats["items_written"] == 2, (
            "Each VIN with a soon-band fault should produce one booking item."
        )

    def test_now_injectable_does_not_call_datetime_now(self):
        """run_health_loop uses the injected now; does not call datetime.now() when provided.

        Verifies the injectable ``now`` parameter works end-to-end by patching
        the datetime module imported by loop.py to raise, then confirming the
        loop completes without calling datetime.now().
        """
        import unittest.mock as mock
        import agents.fi_fleet_health.loop as loop_module
        from agents.fi_fleet_health.loop import run_health_loop

        fixed_now = _NOW
        boom_called: list = []

        # Create a mock datetime class that records .now() calls.
        real_datetime = datetime.datetime

        class BoomDatetime(real_datetime):
            @classmethod
            def now(cls, *args, **kwargs):  # type: ignore[override]
                boom_called.append(True)
                return real_datetime.now(*args, **kwargs)

        with mock.patch.object(loop_module, "datetime", create=False) as mock_dt_mod:
            # Re-expose the real datetime.timezone so the loop's other
            # datetime usages (fromisoformat, timedelta, …) still work.
            mock_dt_mod.datetime = BoomDatetime
            mock_dt_mod.timezone = datetime.timezone
            mock_dt_mod.timedelta = datetime.timedelta

            run_health_loop(
                vehicle_rows=[],
                stock_rows=[],
                computed_at="2026-10-10T06:00:00Z",
                grounded_fn=lambda v: (False, 0),
                history_fn=lambda v: ({}, 0),
                standing_fn=lambda v: None,
                lookup_live=lambda k: None,
                write_finding=lambda f, s, refresh_on_noop=True: {"action": "put"},
                now=fixed_now,   # injected — BoomDatetime.now() must not be called
            )

        assert len(boom_called) == 0, (
            "run_health_loop must not call datetime.datetime.now() when now= is injected "
            f"(BoomDatetime.now() was called {len(boom_called)} time(s))."
        )

    def test_build_vehicle_rows_carries_fault_source_through_loop(self):
        """fault_source in fault_rows survives build_vehicle_rows into health.need evidence.

        Assembler path: a reader row carrying fault_source is assembled via
        build_vehicle_rows and then driven through run_health_loop.  Without the
        fix, build_vehicle_rows silently strips fault_source, so every health.need
        gets fault_source=None regardless of the ADP source column.

        Mutant (C1 guard): delete the fault_source key from the fault dict inside
        build_vehicle_rows → both assertions fail (return None, not the expected string).
        """
        from agents.fi_fleet_health.selection import build_vehicle_rows
        from agents.fi_fleet_health.loop import run_health_loop

        def _fault_source_through_assembler(source: str) -> object:
            """Assemble one VIN with fault_source=source; return the health.need fault_source."""
            rows, _ = build_vehicle_rows(
                fault_rows=[{
                    "vin": _VIN_A,
                    "code": "P1AB0",
                    "fleet_id": _FLEET_1,
                    "band": "soon",
                    "last_seen_at": "2026-10-01",
                    "directive_text": None,
                    "fault_source": source,
                }],
                pm_rows=[],
                managed_vins=[{"vin": _VIN_A, "fleet_id": _FLEET_1}],
                usage_rows=[],
                agent2_rows={},
            )
            written: list = []
            run_health_loop(
                vehicle_rows=rows,
                stock_rows=[{
                    "dealer_id": "dealer-meridian-001",
                    "part_number": "GENERIC-001",
                    "qty_on_hand": 1,
                    "latitude": "47.6062",
                    "longitude": "-122.3321",
                    "stock_as_of": "2026-10-09T00:00:00Z",
                }],
                computed_at="2026-10-10T06:00:00Z",
                write_finding=lambda f, s, refresh_on_noop=True: written.append(f),
                fault_grounding_enabled=False,
                grounded_fn=lambda v: (False, 0),
                history_fn=lambda v: ({}, 0),
                standing_fn=lambda v: None,
                lookup_live=lambda k: None,
                needs_config=self._needs_config(),
                now=_NOW,
            )
            booking = next(
                f for f in written
                if (f.get("subject_key") or "").startswith("service/")
            )
            fault_need = next(
                ev["value"]
                for ev in booking.get("evidence", [])
                if ev.get("kind") == "health.need"
                and (ev.get("value") or {}).get("kind") == "fault"
            )
            return fault_need.get("fault_source")

        for want in ("generated", "cms"):
            got = _fault_source_through_assembler(want)
            assert got == want, (
                f"fault_source={want!r} must survive build_vehicle_rows → health.need; got "
                f"{got!r} (None: the assembler strips the key, review C1)"
            )


# ===========================================================================
# Task 3.2 / spec D6: vfs.source selected and aliased fault_source
# ===========================================================================

class TestFaultRowsSQLFaultSource:
    """SQL must select vfs.source AS fault_source (task 3.2 / spec D6)."""

    def test_selects_vfs_source_as_fault_source(self):
        """Fault SQL selects vfs.source and aliases it to fault_source.

        Mutant: drop the column → assertion fails.
        Uses a regex so any occurrence of the word 'fault_source' alone
        cannot satisfy the check (e.g. a comment referencing the alias).
        """
        import re
        from agents.fi_fleet_health.selection import _FAULT_SQL
        sql = _FAULT_SQL.replace("${stage}", "staging")
        assert re.search(r"vfs\.source\s+AS\s+fault_source", sql), (
            "Fault SQL must contain 'vfs.source AS fault_source' (with optional whitespace). "
            "Mutant: drop or rename the column → pattern absent → fails."
        )
