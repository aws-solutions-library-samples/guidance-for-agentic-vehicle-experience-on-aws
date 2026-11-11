"""Tests for agents/fi_recall_warranty/selection.py — task 3.1E.

Coverage
--------
1. select_unremedied_recalls: fake run_query records SQL; 'false' string → False bool.
2. select_unremedied_recalls: junk do_not_drive (not 'true'/'false') → row refused.
3. select_unremedied_recalls: three pages (fake run_query returns 3 rows).
4. check_grounded: botocore Stubber; open grounding → True.
5. check_grounded: DDB read error → (False, 1) — safe direction (D17).
6. lookup_vehicle_cms_ids: two vehicles for a VIN → None.
7. lookup_vehicle_cms_ids: exactly one vehicle returns (vehicleId, fleetId, locationId).
8. lookup_vehicle_cms_ids: zero vehicles → None.

No AWS calls — env may have dead creds; all DDB interactions use botocore.Stubber
or simple MagicMock objects.
"""
from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError
from botocore.stub import Stubber

import boto3


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_ddb_client() -> Any:
    """Return a real botocore DDB client that will be stubbed by the caller."""
    return boto3.client("dynamodb", region_name="us-west-2")


def _fake_run_query_factory(rows: list[dict]):
    """Return (fake_fn, sql_log) where fake_fn records SQL calls and returns rows."""
    sql_log: list[str] = []

    def _fake(sql: str, *, athena_client, workgroup, results_s3_path, **kwargs):
        sql_log.append(sql)
        return rows

    return _fake, sql_log


# ---------------------------------------------------------------------------
# 1. fake run_query records SQL and 'false' string is parsed to False
# ---------------------------------------------------------------------------

class TestSelectRecallsSqlAndBoolParsing:
    def test_false_string_parsed_to_false(self):
        """Athena returns 'false' as a string; selection must parse it to False bool.

        Mutation guard: if bool('false') is used instead of the strict comparison,
        do_not_drive would be True for every row with "false", and the band table
        would classify all such rows as p0/do-not-drive. This test fails on that
        mutation.
        """
        from agents.fi_recall_warranty.selection import select_unremedied_recalls

        fake_rows = [
            {
                "campaign_id": "MRD-2026-001",
                "vin": "MRDN0000000000001",
                "fleet_id": "fleet-meridian-01",
                "match_basis": "POPULATION",
                "confirming_code": None,
                "confirming_seen_on": None,
                "confirming_source": None,
                "rule_version": "1.0.0",
                "recall_class": "safety",
                "do_not_drive": "false",   # ← the string Athena returns
                "component": "Battery pack",
                "summary": "Battery may overheat.",
                "consequence": "Fire risk.",
                "remedy": "Replace battery module.",
                "remedy_available_from": None,
                "remedy_part_numbers": "[DMS-PT-04-001]",
                "remedy_labor_op": "LO-BAT-001",
            }
        ]
        fake, sql_log = _fake_run_query_factory(fake_rows)

        rows, refused = select_unremedied_recalls(
            "staging",
            athena_client=object(),
            workgroup="cvx-staging-analytics",
            results_s3_path="s3://bucket/prefix/",
            _run_query=fake,
        )

        assert len(sql_log) == 1, "run_query must be called exactly once"
        assert "${stage}" not in sql_log[0], "SQL must have stage substituted"
        assert "staging" in sql_log[0], "stage 'staging' must appear in SQL"
        assert refused == 0
        assert len(rows) == 1
        assert rows[0]["do_not_drive"] is False, (
            "Athena string 'false' must be parsed to Python False. "
            "Mutation: bool('false') returns True — every safety row would be p0/ground."
        )

    def test_true_string_parsed_to_true(self):
        """'true' string from Athena must become Python True."""
        from agents.fi_recall_warranty.selection import select_unremedied_recalls

        fake_rows = [
            {
                "campaign_id": "MRD-2026-002",
                "vin": "MRDN0000000000002",
                "fleet_id": "fleet-meridian-01",
                "match_basis": "CONFIRMED",
                "confirming_code": "B0001",
                "confirming_seen_on": "2026-03-15",
                "confirming_source": "service_records",
                "rule_version": "1.0.0",
                "recall_class": "safety",
                "do_not_drive": "true",   # ← do-not-drive notice
                "component": "Charging port",
                "summary": "Charging port may arc.",
                "consequence": "Fire risk.",
                "remedy": "Replace charging port.",
                "remedy_available_from": None,
                "remedy_part_numbers": "[]",
                "remedy_labor_op": "LO-CHG-001",
            }
        ]
        fake, _ = _fake_run_query_factory(fake_rows)
        rows, refused = select_unremedied_recalls(
            "staging",
            athena_client=object(),
            workgroup="cvx-staging-analytics",
            results_s3_path="s3://bucket/prefix/",
            _run_query=fake,
        )
        assert rows[0]["do_not_drive"] is True
        assert refused == 0


# ---------------------------------------------------------------------------
# 2. Junk do_not_drive → row refused and counted
# ---------------------------------------------------------------------------

class TestJunkDoNotDriveRefused:
    def test_junk_value_refused_and_counted(self):
        """A row whose do_not_drive is not exactly 'true' or 'false' must be refused.

        Mutation guard: if the parse raises but is silently swallowed without
        incrementing refused, the count stays 0 and this assertion fails.
        """
        from agents.fi_recall_warranty.selection import select_unremedied_recalls

        fake_rows = [
            {
                "campaign_id": "MRD-2026-001",
                "vin": "MRDN0000000000001",
                "fleet_id": "fleet-meridian-01",
                "match_basis": "POPULATION",
                "confirming_code": None,
                "confirming_seen_on": None,
                "confirming_source": None,
                "rule_version": "1.0.0",
                "recall_class": "safety",
                "do_not_drive": "yes",   # ← junk value
                "component": "Battery",
                "summary": "S",
                "consequence": "C",
                "remedy": "R",
                "remedy_available_from": None,
                "remedy_part_numbers": "[]",
                "remedy_labor_op": None,
            }
        ]
        fake, _ = _fake_run_query_factory(fake_rows)
        rows, refused = select_unremedied_recalls(
            "staging",
            athena_client=object(),
            workgroup="cvx-staging-analytics",
            results_s3_path="s3://bucket/prefix/",
            _run_query=fake,
        )
        assert rows == [], "junk do_not_drive row must be dropped from accepted"
        assert refused == 1, (
            "refused count must be 1. "
            "Mutation: swallowing ValueError without incrementing refused → 0, fails here."
        )

    def test_null_do_not_drive_also_refused(self):
        """A None do_not_drive must also be refused (not silently coerced)."""
        from agents.fi_recall_warranty.selection import select_unremedied_recalls

        fake_rows = [
            {
                "campaign_id": "MRD-2026-001", "vin": "VIN1", "fleet_id": "fl",
                "match_basis": "POPULATION",
                "confirming_code": None, "confirming_seen_on": None,
                "confirming_source": None, "rule_version": "1.0.0",
                "recall_class": "safety",
                "do_not_drive": None,   # ← None returned by Athena
                "component": "X", "summary": "S", "consequence": "C", "remedy": "R",
                "remedy_available_from": None, "remedy_part_numbers": None,
                "remedy_labor_op": None,
            }
        ]
        fake, _ = _fake_run_query_factory(fake_rows)
        rows, refused = select_unremedied_recalls(
            "staging",
            athena_client=object(),
            workgroup="cvx-staging-analytics",
            results_s3_path="s3://bucket/prefix/",
            _run_query=fake,
        )
        assert refused == 1
        assert rows == []


# ---------------------------------------------------------------------------
# 3. Three-row result (simulating three pages via fake run_query)
# ---------------------------------------------------------------------------

class TestThreePagesViaFakeRunQuery:
    def test_three_rows_returned(self):
        """Fake run_query returning 3 rows yields 3 accepted rows (all do_not_drive valid).

        The fake run_query simulates what adp_source.run_query produces after following
        three NextToken pages: a flat list of row dicts. The selection function must
        accept all three.
        """
        from agents.fi_recall_warranty.selection import select_unremedied_recalls

        def _row(vin: str, dnd: str) -> dict:
            return {
                "campaign_id": "MRD-2026-001", "vin": vin,
                "fleet_id": "fl-01", "match_basis": "POPULATION",
                "confirming_code": None, "confirming_seen_on": None,
                "confirming_source": None, "rule_version": "1.0.0",
                "recall_class": "non_safety", "do_not_drive": dnd,
                "component": "X", "summary": "S", "consequence": "C", "remedy": "R",
                "remedy_available_from": None, "remedy_part_numbers": "[]",
                "remedy_labor_op": None,
            }

        fake, sql_log = _fake_run_query_factory([
            _row("VIN-PAGE1", "false"),
            _row("VIN-PAGE2", "false"),
            _row("VIN-PAGE3", "true"),
        ])
        rows, refused = select_unremedied_recalls(
            "staging",
            athena_client=object(),
            workgroup="cvx-staging-analytics",
            results_s3_path="s3://bucket/prefix/",
            _run_query=fake,
        )
        assert len(rows) == 3, f"expected 3 rows, got {len(rows)}"
        assert refused == 0
        assert rows[0]["do_not_drive"] is False
        assert rows[2]["do_not_drive"] is True


# ---------------------------------------------------------------------------
# 4. check_grounded: open grounding returns True (botocore Stubber)
# ---------------------------------------------------------------------------

class TestCheckGrounded:
    def test_open_grounding_returns_true(self):
        """A row with no returnedAt → vehicle is grounded → (True, 0)."""
        from agents.fi_recall_warranty.selection import check_grounded

        ddb = _make_ddb_client()
        with Stubber(ddb) as stub:
            stub.add_response(
                method="query",
                service_response={
                    "Items": [
                        # One open grounding row (no returnedAt)
                        {
                            "vehicleId": {"S": "MRDN0000000000015"},
                            "actionId": {"S": "act-001"},
                            "groundedAt": {"S": "2026-10-01T05:00:00Z"},
                        }
                    ],
                    "Count": 1,
                },
                expected_params={
                    "TableName": "cms-staging-storage-vehicle-groundings",
                    "KeyConditionExpression": "#v = :vid",
                    "ExpressionAttributeNames": {"#v": "vehicleId"},
                    "ExpressionAttributeValues": {
                        ":vid": {"S": "MRDN0000000000015"},
                        ":null": {"S": "NULL"},
                    },
                    "FilterExpression": "attribute_not_exists(returnedAt) OR attribute_type(returnedAt, :null)",
                },
            )
            is_grounded, err = check_grounded(
                "MRDN0000000000015",
                ddb_client=ddb,
                groundings_table="cms-staging-storage-vehicle-groundings",
            )

        assert is_grounded is True, "open grounding row must yield is_grounded=True"
        assert err == 0

    def test_no_open_grounding_returns_false(self):
        """Empty Items (all groundings returned) → not grounded → (False, 0)."""
        from agents.fi_recall_warranty.selection import check_grounded

        ddb = _make_ddb_client()
        with Stubber(ddb) as stub:
            stub.add_response(
                method="query",
                service_response={"Items": [], "Count": 0},
                expected_params={
                    "TableName": "cms-staging-storage-vehicle-groundings",
                    "KeyConditionExpression": "#v = :vid",
                    "ExpressionAttributeNames": {"#v": "vehicleId"},
                    "ExpressionAttributeValues": {
                        ":vid": {"S": "VEH-MRDN-0016"},
                        ":null": {"S": "NULL"},
                    },
                    "FilterExpression": "attribute_not_exists(returnedAt) OR attribute_type(returnedAt, :null)",
                },
            )
            is_grounded, err = check_grounded(
                "VEH-MRDN-0016",
                ddb_client=ddb,
                groundings_table="cms-staging-storage-vehicle-groundings",
            )

        assert is_grounded is False
        assert err == 0


# ---------------------------------------------------------------------------
# 5. check_grounded: read error → (False, 1) — safe direction
# ---------------------------------------------------------------------------

class TestCheckGroundedReadError:
    def test_client_error_returns_not_grounded_and_error_count(self):
        """A DDB ClientError on the groundings query must return (False, 1).

        D17 safe direction: treating an error as grounded would suppress the
        grounding item; treating it as not grounded preserves it (CMS's route
        is idempotent).

        Mutation guard: if check_grounded returns (True, 1) on error, the caller
        would skip writing the grounding item → this assertion fails.
        """
        from agents.fi_recall_warranty.selection import check_grounded

        mock_ddb = MagicMock()
        mock_ddb.query.side_effect = ClientError(
            {"Error": {"Code": "AccessDeniedException",
                       "Message": "User is not authorized to access this resource"}},
            "Query",
        )

        is_grounded, err = check_grounded(
            "MRDN0000000000015",
            ddb_client=mock_ddb,
            groundings_table="cms-staging-storage-vehicle-groundings",
        )

        assert is_grounded is False, (
            "Read error must be treated as not grounded (D17 safe direction). "
            "Mutation: returning True on error → grounding item suppressed."
        )
        assert err == 1, "error_count must be 1 on ClientError"

    def test_generic_exception_also_safe(self):
        """Any non-ClientError exception also returns (False, 1)."""
        from agents.fi_recall_warranty.selection import check_grounded

        mock_ddb = MagicMock()
        mock_ddb.query.side_effect = RuntimeError("DDB unavailable")

        is_grounded, err = check_grounded(
            "MRDN0000000000015",
            ddb_client=mock_ddb,
            groundings_table="cms-staging-storage-vehicle-groundings",
        )
        assert is_grounded is False
        assert err == 1


# ---------------------------------------------------------------------------
# 6 & 8. lookup_vehicle_cms_ids: two vehicles → None; zero vehicles → None
# ---------------------------------------------------------------------------

class TestLookupVehicleCmsIds:
    def test_two_vehicles_for_vin_returns_none(self):
        """Two items from vin-index → registry ambiguity → None.

        Mutation guard: if the function takes the first item instead of refusing,
        it returns a non-None tuple and this assertion fails.
        """
        from agents.fi_recall_warranty.selection import lookup_vehicle_cms_ids

        mock_ddb = MagicMock()
        mock_ddb.query.return_value = {
            "Items": [
                {"vehicleId": {"S": "VEH-001"}},
                {"vehicleId": {"S": "VEH-002"}},   # duplicate VIN — ambiguous
            ]
        }

        result = lookup_vehicle_cms_ids(
            "MRDN0000000000001",
            ddb_client=mock_ddb,
            vehicles_table="cms-staging-storage-vehicles",
        )
        assert result is None, (
            "Two vehicles for a VIN must return None (ambiguous registry). "
            "Mutation: returning first item instead → non-None tuple, fails here."
        )

    def test_zero_vehicles_returns_none(self):
        """No items from vin-index → VIN not in registry → None."""
        from agents.fi_recall_warranty.selection import lookup_vehicle_cms_ids

        mock_ddb = MagicMock()
        mock_ddb.query.return_value = {"Items": []}

        result = lookup_vehicle_cms_ids(
            "MRDN0000000000099",
            ddb_client=mock_ddb,
            vehicles_table="cms-staging-storage-vehicles",
        )
        assert result is None

    def test_exactly_one_vehicle_returns_tuple(self):
        """One item → GetItem for fleet/location → returns (vehicleId, fleetId, locationId)."""
        from agents.fi_recall_warranty.selection import lookup_vehicle_cms_ids

        mock_ddb = MagicMock()
        mock_ddb.query.return_value = {
            "Items": [{"vehicleId": {"S": "MRDN0000000000015"}}]
        }
        mock_ddb.get_item.return_value = {
            "Item": {
                "fleetId": {"S": "fleet-meridian-01"},
                "locationId": {"S": "loc-sea-01"},
            }
        }

        result = lookup_vehicle_cms_ids(
            "MRDN0000000000015",
            ddb_client=mock_ddb,
            vehicles_table="cms-staging-storage-vehicles",
        )
        assert result is not None
        vehicle_id, fleet_id, location_id = result
        assert vehicle_id == "MRDN0000000000015"
        assert fleet_id == "fleet-meridian-01"
        assert location_id == "loc-sea-01"

    def test_missing_location_id_returns_none_field(self):
        """locationId absent from the vehicle row → None for location_id (not crash)."""
        from agents.fi_recall_warranty.selection import lookup_vehicle_cms_ids

        mock_ddb = MagicMock()
        mock_ddb.query.return_value = {
            "Items": [{"vehicleId": {"S": "VEH-MRDN-0020"}}]
        }
        mock_ddb.get_item.return_value = {
            "Item": {"fleetId": {"S": "fleet-meridian-02"}}
            # locationId absent
        }

        result = lookup_vehicle_cms_ids(
            "MRDN0000000000020",
            ddb_client=mock_ddb,
            vehicles_table="cms-staging-storage-vehicles",
        )
        assert result is not None
        _, _, location_id = result
        assert location_id is None



# ---------------------------------------------------------------------------
# Mutation (c) guard: SQL must filter out remedied rows
# ---------------------------------------------------------------------------

class TestRecallsSqlContent:
    def test_sql_filters_remedied_false(self):
        """The SQL template must include WHERE remedied = FALSE.

        Mutation guard: removing the WHERE clause means remedied rows are included,
        generating spurious Findings for vehicles that already had the recall fixed.
        This test checks the rendered SQL directly.

        Mutation: remove 'remedied = FALSE' from the SQL → SQL no longer contains
        that phrase → this assertion fails.
        """
        from agents.fi_recall_warranty.selection import select_unremedied_recalls

        fake, sql_log = _fake_run_query_factory([])
        select_unremedied_recalls(
            "staging",
            athena_client=object(),
            workgroup="cvx-staging-analytics",
            results_s3_path="s3://bucket/prefix/",
            _run_query=fake,
        )
        sql = sql_log[0]
        assert "remedied = FALSE" in sql or "remedied=FALSE" in sql or "remedied = false" in sql.lower(), (
            "SQL must filter WHERE remedied = FALSE. "
            "Mutation: removing the filter → remedied rows included → this fails."
        )



# ---------------------------------------------------------------------------
# Stage param: select_parts_stock and select_warranty_candidates
# ---------------------------------------------------------------------------

class TestStageParamPropagation:
    """Stage is substituted into every DMS table reference; invalid stage refused."""

    def test_parts_prod_stage_no_dms_staging_literal(self):
        """stage='prod' → no 'dms_staging' in parts SQL.

        Mutation (a): literal dms_staging restored in parts SQL → 'dms_staging'
        appears in SQL even when stage='prod' → this assertion fails.
        """
        from agents.fi_recall_warranty.selection import select_parts_stock

        fake, sql_log = _fake_run_query_factory([])
        select_parts_stock(
            "prod", athena_client=object(), workgroup="w",
            results_s3_path="s3://b/", _run_query=fake,
        )
        assert sql_log, "run_query must be called"
        for sql in sql_log:
            assert "dms_staging" not in sql, (
                "Mutation (a): literal dms_staging restored in parts SQL → "
                "dms_staging present when stage='prod'."
            )

    def test_warranty_prod_stage_no_dms_staging_literal(self):
        """stage='prod' → no 'dms_staging' in warranty SQL."""
        from agents.fi_recall_warranty.selection import select_warranty_candidates

        fake, sql_log = _fake_run_query_factory([])
        select_warranty_candidates(
            "prod", athena_client=object(), workgroup="w",
            results_s3_path="s3://b/", _run_query=fake,
        )
        assert sql_log
        for sql in sql_log:
            assert "dms_staging" not in sql, (
                "dms_staging must not appear in warranty SQL when stage='prod'."
            )

    def test_parts_invalid_stage_raises_before_query(self):
        """Invalid stage must raise ValueError before any query is executed."""
        from agents.fi_recall_warranty.selection import select_parts_stock

        fake, sql_log = _fake_run_query_factory([])
        with pytest.raises(ValueError, match="Invalid stage"):
            select_parts_stock(
                "bad_stage", athena_client=object(), workgroup="w",
                results_s3_path="s3://b/", _run_query=fake,
            )
        assert sql_log == [], "no query must run for an invalid stage"

    def test_warranty_invalid_stage_raises_before_query(self):
        """Invalid stage must raise ValueError before any query is executed."""
        from agents.fi_recall_warranty.selection import select_warranty_candidates

        fake, sql_log = _fake_run_query_factory([])
        with pytest.raises(ValueError, match="Invalid stage"):
            select_warranty_candidates(
                "not_valid", athena_client=object(), workgroup="w",
                results_s3_path="s3://b/", _run_query=fake,
            )
        assert sql_log == [], "no query must run for an invalid stage"


class TestWarrantySqlD8Filters:
    """Warranty SQL must include both D8 step 1 filters."""

    def test_verdict_filter_present_and_denied_excluded(self):
        """SQL must include approved/requires_review and must NOT include denied.

        Mutation (c): 'denied' added to the verdict list → SQL contains 'denied'
        → this assertion fails.
        """
        from agents.fi_recall_warranty.selection import select_warranty_candidates

        fake, sql_log = _fake_run_query_factory([])
        select_warranty_candidates(
            "staging", athena_client=object(), workgroup="w",
            results_s3_path="s3://b/", _run_query=fake,
        )
        sql = sql_log[0]
        assert "approved" in sql and "requires_review" in sql, (
            "Warranty SQL must filter verdict_status IN ('approved', 'requires_review')."
        )
        assert "denied" not in sql, (
            "Mutation (c): 'denied' added to verdict list → SQL contains 'denied' → fails."
        )

    def test_deadline_filter_present(self):
        """SQL must filter filing_deadline > current_date.

        Mutation (b): deadline filter removed → 'filing_deadline' and/or
        'current_date' missing from SQL → this assertion fails.
        """
        from agents.fi_recall_warranty.selection import select_warranty_candidates

        fake, sql_log = _fake_run_query_factory([])
        select_warranty_candidates(
            "staging", athena_client=object(), workgroup="w",
            results_s3_path="s3://b/", _run_query=fake,
        )
        sql = sql_log[0]
        assert "filing_deadline" in sql and "current_date" in sql, (
            "Mutation (b): deadline filter removed → 'filing_deadline' or "
            "'current_date' missing from SQL → fails."
        )
