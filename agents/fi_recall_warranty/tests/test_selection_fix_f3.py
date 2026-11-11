"""Tests for F3 fixes in agents/fi_recall_warranty/selection.py (G3-17, W1, W2, W6 paging half).

Coverage
--------
(a) check_grounded: returnedAt stored as DynamoDB NULL counts as open; returnedAt absent counts as
    open. FilterExpression string and :null value asserted via Stubber expected_params.
    Control: returnedAt set → is_grounded False.
(b) lookup_vehicle_cms_ids: ClientError on vin-index Query raises RegistryReadError → run_recall_loop
    still writes the ground item for a p0 row AND counts grounding_read_errors == 1.
    Control: no error → grounding_read_errors == 0.
(c) select_unremedied_recalls driven via real adp_source.run_query through an Athena Stubber:
    StartQueryExecution → GetQueryExecution SUCCEEDED → THREE GetQueryResults pages chained by
    NextToken. All rows from all three pages returned; header row stripped exactly once.
"""
from __future__ import annotations

import datetime
from typing import Any, Optional

import boto3
import pytest
from botocore.stub import Stubber


# ---------------------------------------------------------------------------
# Shared constants
# ---------------------------------------------------------------------------

_EXEC_ID = "test-exec-f3-001"
_WORKGROUP = "cvx-staging-analytics"
_RESULTS_PATH = "s3://test-bucket/results/"
_GROUNDING_TABLE = "cms-staging-storage-vehicle-groundings"
_VEHICLES_TABLE = "cms-staging-storage-vehicles"
_VEHICLE_ID = "MRDN0000000000015"
_VIN = "MRDVEH00000000001"
_FLEET_ID = "fleet-meridian-01"
_LOCATION_ID = "loc-001"
_TODAY = datetime.date(2026, 10, 7)
_COMPUTED_AT = "2026-10-07T05:00:00Z"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ddb() -> Any:
    return boto3.client(
        "dynamodb", region_name="us-west-2",
        aws_access_key_id="x", aws_secret_access_key="x",
    )


def _athena() -> Any:
    return boto3.client(
        "athena", region_name="us-east-1",
        aws_access_key_id="x", aws_secret_access_key="x",
    )


_FILTER_EXPECTED_PARAMS = {
    "TableName": _GROUNDING_TABLE,
    "KeyConditionExpression": "#v = :vid",
    "ExpressionAttributeNames": {"#v": "vehicleId"},
    "ExpressionAttributeValues": {
        ":vid": {"S": _VEHICLE_ID},
        ":null": {"S": "NULL"},
    },
    "FilterExpression": "attribute_not_exists(returnedAt) OR attribute_type(returnedAt, :null)",
}


def _exec_status(state: str) -> dict:
    return {
        "QueryExecution": {"QueryExecutionId": _EXEC_ID, "Status": {"State": state}},
        "ResponseMetadata": {},
    }


def _page1(cols: list[str], data: list[list], next_token: str | None = None) -> dict:
    """First page: header row + data rows."""
    header = {"Data": [{"VarCharValue": c} for c in cols]}
    rows = [header] + [{"Data": [{"VarCharValue": v} if v is not None else {} for v in r]} for r in data]
    resp: dict = {"ResultSet": {"Rows": rows, "ResultSetMetadata": {"ColumnInfo": []}}, "ResponseMetadata": {}}
    if next_token:
        resp["NextToken"] = next_token
    return resp


def _pagen(data: list[list], next_token: str | None = None) -> dict:
    """Subsequent pages: data rows only (no header)."""
    rows = [{"Data": [{"VarCharValue": v} if v is not None else {} for v in r]} for r in data]
    resp: dict = {"ResultSet": {"Rows": rows, "ResultSetMetadata": {"ColumnInfo": []}}, "ResponseMetadata": {}}
    if next_token:
        resp["NextToken"] = next_token
    return resp


# ---------------------------------------------------------------------------
# (a) check_grounded: NULL returnedAt, absent returnedAt, control (set)
# ---------------------------------------------------------------------------

class TestCheckGroundedNullAndAbsent:
    """W1: open grounding must be detected for both DynamoDB NULL and absent returnedAt."""

    def test_null_returned_at_is_open_grounding(self):
        """A row with returnedAt stored as DynamoDB NULL must be treated as an open grounding."""
        from agents.fi_recall_warranty.selection import check_grounded

        client = _ddb()
        with Stubber(client) as stub:
            stub.add_response(
                method="query",
                service_response={
                    "Items": [
                        {
                            "vehicleId": {"S": _VEHICLE_ID},
                            "actionId": {"S": "act-001"},
                            "returnedAt": {"NULL": True},
                        }
                    ],
                    "Count": 1,
                },
                expected_params=_FILTER_EXPECTED_PARAMS,
            )
            is_grounded, err = check_grounded(
                _VEHICLE_ID,
                ddb_client=client,
                groundings_table=_GROUNDING_TABLE,
            )

        assert is_grounded is True, (
            "returnedAt NULL must be an open grounding. "
            "Mutation: dropping OR clause misses this shape."
        )
        assert err == 0

    def test_absent_returned_at_is_open_grounding(self):
        """A row with no returnedAt key must be treated as an open grounding."""
        from agents.fi_recall_warranty.selection import check_grounded

        client = _ddb()
        with Stubber(client) as stub:
            stub.add_response(
                method="query",
                service_response={
                    "Items": [
                        {
                            "vehicleId": {"S": _VEHICLE_ID},
                            "actionId": {"S": "act-002"},
                            # returnedAt key absent
                        }
                    ],
                    "Count": 1,
                },
                expected_params=_FILTER_EXPECTED_PARAMS,
            )
            is_grounded, err = check_grounded(
                _VEHICLE_ID,
                ddb_client=client,
                groundings_table=_GROUNDING_TABLE,
            )

        assert is_grounded is True, "Absent returnedAt must be an open grounding."
        assert err == 0

    def test_set_returned_at_is_not_open_grounding_control(self):
        """Control: DDB filter excluded the row (returnedAt set) → is_grounded False."""
        from agents.fi_recall_warranty.selection import check_grounded

        client = _ddb()
        with Stubber(client) as stub:
            # DDB returns empty list — filter excluded the returned row
            stub.add_response(
                method="query",
                service_response={"Items": [], "Count": 0},
                expected_params=_FILTER_EXPECTED_PARAMS,
            )
            is_grounded, err = check_grounded(
                _VEHICLE_ID,
                ddb_client=client,
                groundings_table=_GROUNDING_TABLE,
            )

        assert is_grounded is False, "Set returnedAt (filter excluded row) → not grounded (control)."
        assert err == 0


# ---------------------------------------------------------------------------
# (b) registry ClientError → grounding_read_errors counted; ground item written
# ---------------------------------------------------------------------------

class _LoopFakes:
    """Minimal fakes for run_recall_loop (vehicle_ids_fn injected separately)."""

    def __init__(self) -> None:
        self.written: list[dict] = []

    def locations_fn(self, fleet_id: str) -> dict:
        return {_LOCATION_ID: (37.77, -122.41)}

    def grounded_fn(self, vehicle_id: str) -> tuple[bool, int]:
        return False, 0

    def history_fn(self, vin: str) -> tuple[dict, int]:
        return {}, 0

    def standing_fn(self, vin: str) -> dict:
        return {"vin": vin}

    def lookup_live(self, key: str) -> Optional[dict]:
        return None

    def write_finding(self, finding: dict, standing: Any, refresh_on_noop: bool = False) -> dict:
        self.written.append(finding)
        return {"action": "put"}


def _p0_row() -> dict:
    return {
        "vin": _VIN,
        "campaign_id": "MRD-2024-001",
        "recall_class": "safety",
        "do_not_drive": True,
        "match_basis": "POPULATION",
        "remedy_available_from": None,
        "remedy_part_numbers": [],
        "summary": "Brake caliper may seize.",
        "consequence": "Reduced braking.",
        "remedy": "Replace caliper.",
        "received_date": "2026-01-01",
        "catalog_version": "1",
        "rule_version": "1",
        "models": None,
        "model_year_min": None,
        "model_year_max": None,
        "plants": None,
        "build_from": None,
        "build_to": None,
        "park_outside": False,
        "confirming_code": None,
        "confirming_seen_on": None,
        "confirming_source": None,
    }


def _run_loop(vehicle_ids_fn: Any, fakes: _LoopFakes) -> dict:
    from agents.fi_recall_warranty.recall import run_recall_loop
    return run_recall_loop(
        recall_rows=[_p0_row()],
        stock_rows=[],
        today=_TODAY,
        computed_at=_COMPUTED_AT,
        vehicle_ids_fn=vehicle_ids_fn,
        locations_fn=fakes.locations_fn,
        grounded_fn=fakes.grounded_fn,
        history_fn=fakes.history_fn,
        standing_fn=fakes.standing_fn,
        lookup_live=fakes.lookup_live,
        write_finding=fakes.write_finding,
    )


class TestRegistryClientError:
    def test_client_error_on_query_is_counted_and_loop_continues(self):
        """ClientError on vin-index Query → grounding_read_errors == 1, ground item written."""
        from agents.fi_recall_warranty.selection import lookup_vehicle_cms_ids

        client = _ddb()
        fakes = _LoopFakes()

        with Stubber(client) as stub:
            stub.add_client_error(
                method="query",
                service_error_code="ThrottlingException",
                service_message="Rate exceeded",
            )
            def vehicle_ids_fn(vin: str) -> Optional[tuple]:
                return lookup_vehicle_cms_ids(
                    vin, ddb_client=client, vehicles_table=_VEHICLES_TABLE,
                )
            try:
                stats = _run_loop(vehicle_ids_fn, fakes)
            except Exception:
                stats = {}  # propagation → loop did not absorb the error

        assert stats.get("grounding_read_errors") == 1, (
            "ClientError on registry Query must increment grounding_read_errors. "
            "Mutation: propagating the error aborts the run instead."
        )
        # ground item written: stats track it under by_item_type, and write_finding was called
        assert stats.get("by_item_type", {}).get("ground") == 1, (
            "D17: ground item must still be written even with a registry read error."
        )
        assert len(fakes.written) >= 1, "write_finding must have been called for the ground item."

    def test_no_error_zero_grounding_read_errors_control(self):
        """Control: successful registry read → grounding_read_errors == 0."""
        fakes = _LoopFakes()

        def vehicle_ids_fn(vin: str) -> Optional[tuple]:
            return ("MRDN0000000000015", _FLEET_ID, _LOCATION_ID)

        stats = _run_loop(vehicle_ids_fn, fakes)

        assert stats["grounding_read_errors"] == 0, "No error must leave grounding_read_errors at 0."


# ---------------------------------------------------------------------------
# (c) select_unremedied_recalls: three GetQueryResults pages via NextToken
# ---------------------------------------------------------------------------

# Columns the real SQL returns (subset sufficient for _parse_do_not_drive / _parse_part_numbers)
_COLS = [
    "campaign_id", "vin", "fleet_id", "match_basis",
    "confirming_code", "confirming_seen_on", "confirming_source", "rule_version",
    "recall_class", "do_not_drive", "park_outside", "received_date", "catalog_version",
    "models", "model_year_min", "model_year_max", "plants", "build_from", "build_to",
    "component", "summary", "consequence", "remedy",
    "remedy_available_from", "remedy_part_numbers", "remedy_labor_op",
]


def _recall_row_data(vin: str) -> list:
    """Minimal valid recall row (do_not_drive='false'; remedy_part_numbers='[]')."""
    return [
        "MRD-2024-001", vin, "fleet-meridian-01", "POPULATION",
        None, None, None, "1",
        "non_safety", "false", "false", "2026-01-01", "1",
        None, None, None, None, None, None,
        None, "summary text", "consequence text", "remedy text",
        None, "[]", None,
    ]


def _make_run_query_no_sleep():
    """Wrap adp_source.run_query with poll_interval=0 so tests don't actually sleep."""
    from agents.tier2.adp_source import run_query as _real

    def _run(sql, *, athena_client, workgroup, results_s3_path, **_kw):
        return _real(
            sql,
            athena_client=athena_client,
            workgroup=workgroup,
            results_s3_path=results_s3_path,
            poll_interval=0,
        )

    return _run


class TestSelectRecallsThreePages:
    def test_three_pages_all_rows_returned_header_stripped_once(self):
        """run_query follows NextToken across three pages; all 6 rows returned, header not repeated."""
        from agents.fi_recall_warranty.selection import select_unremedied_recalls

        client = _athena()
        with Stubber(client) as stub:
            stub.add_response(
                "start_query_execution",
                {"QueryExecutionId": _EXEC_ID, "ResponseMetadata": {}},
            )
            stub.add_response(
                "get_query_execution",
                _exec_status("SUCCEEDED"),
                expected_params={"QueryExecutionId": _EXEC_ID},
            )
            # Page 1: header + 2 rows, NextToken → page 2
            stub.add_response(
                "get_query_results",
                _page1(_COLS, [_recall_row_data("P1R1"), _recall_row_data("P1R2")], next_token="tok2"),
                expected_params={"QueryExecutionId": _EXEC_ID, "MaxResults": 1000},
            )
            # Page 2: 2 rows, NextToken → page 3
            stub.add_response(
                "get_query_results",
                _pagen([_recall_row_data("P2R1"), _recall_row_data("P2R2")], next_token="tok3"),
                expected_params={"QueryExecutionId": _EXEC_ID, "MaxResults": 1000, "NextToken": "tok2"},
            )
            # Page 3: 2 rows, no NextToken
            stub.add_response(
                "get_query_results",
                _pagen([_recall_row_data("P3R1"), _recall_row_data("P3R2")]),
                expected_params={"QueryExecutionId": _EXEC_ID, "MaxResults": 1000, "NextToken": "tok3"},
            )

            rows, refused = select_unremedied_recalls(
                "staging",
                athena_client=client,
                workgroup=_WORKGROUP,
                results_s3_path=_RESULTS_PATH,
                _run_query=_make_run_query_no_sleep(),
            )

            # Assert INSIDE the with block so this fires before stub.__exit__ calls
            # assert_no_pending_responses — the failing line is ours when mutant stops at page 1.
            vins = [r["vin"] for r in rows]
            assert vins == ["P1R1", "P1R2", "P2R1", "P2R2", "P3R1", "P3R2"], (
                "All 6 rows across three pages must be returned. "
                "Mutation: stopping after page 1 → only 2 rows returned."
            )
            assert refused == 0, "All rows have valid do_not_drive='false'; none should be refused."
            stub.assert_no_pending_responses()
