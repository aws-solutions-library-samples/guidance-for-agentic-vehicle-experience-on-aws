"""Tests for agents.fi_rebalancing.sources (spec G4-D1).

Covers:
- Athena reads: placeholder validation, 2-page paging (gaps), row parsing,
  SourceReadError on malformed row.
- DynamoDB: read_locations (2 pages), read_grounding (all outcomes),
  read_open_items (paging + filtering), VIN whose vehicleId differs.
- build_vehicle_facts: open-RO zero days, service-date days, inf when none,
  on-rent-now zero hours, upcoming booking hours, inf when no bookings.

No AWS calls; uses botocore.stub.Stubber with AWS env vars unset.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any
from unittest.mock import MagicMock, patch

import boto3
import pytest
from botocore.stub import Stubber

# Ensure no real AWS calls
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.pop("AWS_ACCESS_KEY_ID", None)
os.environ.pop("AWS_SECRET_ACCESS_KEY", None)

from agents.fi_rebalancing.sources import (
    SourceReadError,
    _fi,
    build_vehicle_facts,
    read_busy_windows,
    read_gaps,
    read_grounding,
    read_locations,
    read_open_items,
    read_vehicle_days,
)


# ---------------------------------------------------------------------------
# Helpers: Athena Stubber
# ---------------------------------------------------------------------------

_WG = "cvx-test-analytics"
_S3 = "s3://cvx-test-results/tier2/"
_STAGE = "test"
_FLEET = "demo-rental-carshare"


def _make_athena() -> tuple[Any, Stubber]:
    client = boto3.client("athena", region_name="us-east-1")
    return client, Stubber(client)


def _athena_start(stub: Stubber, qid: str = "q1") -> None:
    stub.add_response(
        "start_query_execution",
        {"QueryExecutionId": qid},
        expected_params=None,
    )


def _athena_succeeded(stub: Stubber, qid: str = "q1") -> None:
    stub.add_response(
        "get_query_execution",
        {"QueryExecution": {"Status": {"State": "SUCCEEDED"}, "QueryExecutionId": qid}},
        {"QueryExecutionId": qid},
    )


def _athena_results_page(stub: Stubber, qid: str, rows: list[list[str | None]],
                         cols: list[str], next_token: str | None = None) -> None:
    """Add a GetQueryResults page with a header row + data rows."""
    col_meta = [{"Name": c, "Type": "varchar"} for c in cols]
    header = {"Data": [{"VarCharValue": c} for c in cols]}
    data_rows = [{"Data": [{"VarCharValue": v} if v is not None else {} for v in row]}
                 for row in rows]
    resp: dict = {
        "ResultSet": {"Rows": [header] + data_rows, "ResultSetMetadata": {"ColumnInfo": col_meta}},
        "ResponseMetadata": {},
    }
    if next_token:
        resp["NextToken"] = next_token
    # adp_source.run_query always passes MaxResults=1000
    stub.add_response("get_query_results", resp,
                      {"QueryExecutionId": qid, "MaxResults": 1000})


def _athena_results_next_page(stub: Stubber, qid: str, rows: list[list[str | None]],
                               cols: list[str], token: str,
                               next_token: str | None = None) -> None:
    """Subsequent pages have data rows only (no header row — adp_source handles that)."""
    col_meta = [{"Name": c, "Type": "varchar"} for c in cols]
    data_rows = [{"Data": [{"VarCharValue": v} if v is not None else {} for v in row]}
                 for row in rows]
    resp: dict = {
        "ResultSet": {"Rows": data_rows, "ResultSetMetadata": {"ColumnInfo": col_meta}},
        "ResponseMetadata": {},
    }
    if next_token:
        resp["NextToken"] = next_token
    stub.add_response("get_query_results", resp,
                      {"QueryExecutionId": qid, "MaxResults": 1000, "NextToken": token})


# ---------------------------------------------------------------------------
# Placeholder validation
# ---------------------------------------------------------------------------

def test_load_invalid_stage_raises():
    from agents.fi_rebalancing.sources import _load
    with pytest.raises(ValueError, match="stage"):
        _load("select_gaps.sql", stage="INVALID_UPPER", fleet_id=_FLEET)


def test_load_invalid_fleet_id_raises():
    from agents.fi_rebalancing.sources import _load
    with pytest.raises(ValueError, match="fleet_id"):
        _load("select_gaps.sql", stage=_STAGE, fleet_id="has spaces here")


def test_load_invalid_today_raises():
    from agents.fi_rebalancing.sources import _load
    with pytest.raises(ValueError, match="today"):
        _load("select_events.sql", stage=_STAGE, today="not-a-date")


def test_load_invalid_now_raises():
    from agents.fi_rebalancing.sources import _load
    with pytest.raises(ValueError, match="now"):
        _load("select_busy_windows.sql", stage=_STAGE, fleet_id=_FLEET,
              now="2026-10-06T12:00:00")  # ISO format, not YYYY-MM-DD HH:MM:SS


# ---------------------------------------------------------------------------
# Athena read_gaps: 2-page paging
# ---------------------------------------------------------------------------

_GAPS_COLS = [
    "location_id", "forecast_hours", "supply_vehicles",
    "forecast_utilization_pct", "status", "vehicles_short", "vehicles_spare", "data_as_of"
]


def test_read_gaps_paging_returns_all_rows():
    client, stub = _make_athena()
    with stub:
        _athena_start(stub)
        _athena_succeeded(stub)
        # page 1: 1 row + next token
        _athena_results_page(stub, "q1",
                             [["loc-a", "80.0", "3", "0.85", "deficit", "2", "0", "2026-10-06"]],
                             _GAPS_COLS, next_token="tok1")
        # page 2: 1 row, no next token
        _athena_results_next_page(stub, "q1",
                                  [["loc-b", "40.0", "5", "0.40", "surplus", "0", "2", "2026-10-06"]],
                                  _GAPS_COLS, token="tok1")
        rows = read_gaps(stage=_STAGE, fleet_id=_FLEET,
                         athena_client=client, workgroup=_WG, results_s3_path=_S3)

    assert len(rows) == 2
    assert rows[0]["location_id"] == "loc-a"
    assert rows[0]["forecast_hours"] == pytest.approx(80.0)
    assert rows[0]["supply_vehicles"] == 3
    assert rows[1]["location_id"] == "loc-b"
    assert rows[1]["vehicles_spare"] == 2


def test_read_gaps_malformed_row_raises():
    """A row with a non-numeric forecast_hours raises SourceReadError."""
    client, stub = _make_athena()
    with stub:
        _athena_start(stub)
        _athena_succeeded(stub)
        _athena_results_page(stub, "q1",
                             [["loc-a", "not-a-number", "3", "0.85", "deficit", "2", "0", "2026-10-06"]],
                             _GAPS_COLS)
        with pytest.raises(SourceReadError, match="forecast_hours"):
            read_gaps(stage=_STAGE, fleet_id=_FLEET,
                      athena_client=client, workgroup=_WG, results_s3_path=_S3)


# ---------------------------------------------------------------------------
# Athena read_busy_windows: 2-page paging
# ---------------------------------------------------------------------------

_BUSY_COLS = ["vin", "location_id", "start_at", "end_at"]


def test_read_busy_windows_paging():
    client, stub = _make_athena()
    with stub:
        _athena_start(stub)
        _athena_succeeded(stub)
        _athena_results_page(stub, "q1",
                             [["VIN-1", "loc-a", "2026-10-06 10:00:00", "2026-10-06 14:00:00"]],
                             _BUSY_COLS, next_token="tok2")
        _athena_results_next_page(stub, "q1",
                                  [["VIN-2", "loc-a", "2026-10-06 18:00:00", "2026-10-07 08:00:00"]],
                                  _BUSY_COLS, token="tok2")
        rows = read_busy_windows(stage=_STAGE, fleet_id=_FLEET,
                                 now="2026-10-06 12:00:00",
                                 athena_client=client, workgroup=_WG, results_s3_path=_S3)

    assert len(rows) == 2
    assert rows[0]["vin"] == "VIN-1"
    assert rows[1]["vin"] == "VIN-2"
    # start_at/end_at must be naive UTC datetimes
    assert isinstance(rows[0]["start_at"], datetime)
    assert rows[0]["start_at"].tzinfo is None
    assert rows[0]["start_at"] == datetime(2026, 10, 6, 10, 0, 0)
    assert rows[0]["end_at"] == datetime(2026, 10, 6, 14, 0, 0)


def test_read_busy_windows_malformed_timestamp_raises():
    """A malformed start_at value raises SourceReadError."""
    client, stub = _make_athena()
    with stub:
        _athena_start(stub)
        _athena_succeeded(stub)
        _athena_results_page(stub, "q1",
                             [["VIN-1", "loc-a", "not-a-timestamp", "2026-10-06 14:00:00"]],
                             _BUSY_COLS)
        with pytest.raises(SourceReadError, match="start_at"):
            read_busy_windows(stage=_STAGE, fleet_id=_FLEET,
                              now="2026-10-06 12:00:00",
                              athena_client=client, workgroup=_WG, results_s3_path=_S3)


def test_read_busy_windows_offset_timestamp_converted_to_utc():
    """S-n3: a timestamp with a UTC offset is converted to UTC, not truncated."""
    client, stub = _make_athena()
    with stub:
        _athena_start(stub)
        _athena_succeeded(stub)
        # +02:00 offset: 12:00+02:00 == 10:00 UTC
        _athena_results_page(stub, "q1",
                             [["VIN-1", "loc-a",
                               "2026-10-06T12:00:00+02:00",
                               "2026-10-06T16:00:00+02:00"]],
                             _BUSY_COLS)
        rows = read_busy_windows(stage=_STAGE, fleet_id=_FLEET,
                                 now="2026-10-06 11:00:00",
                                 athena_client=client, workgroup=_WG, results_s3_path=_S3)

    assert len(rows) == 1
    row = rows[0]
    assert row["start_at"].tzinfo is None, "tzinfo must be stripped"
    assert row["end_at"].tzinfo is None, "tzinfo must be stripped"
    # Offset-aware 12:00+02:00 → UTC 10:00, not local 12:00
    assert row["start_at"] == datetime(2026, 10, 6, 10, 0, 0), (
        "start_at must be converted to UTC (10:00) not stripped in place (12:00)"
    )
    assert row["end_at"] == datetime(2026, 10, 6, 14, 0, 0)


# ---------------------------------------------------------------------------
# S-n3/S-b2: _fi raises SourceReadError on NaN / Infinity
# ---------------------------------------------------------------------------

def test_fi_nan_raises_source_read_error():
    """S-b2/S-n3: _fi must raise SourceReadError for 'NaN', not bare ValueError."""
    with pytest.raises(SourceReadError, match="non-finite"):
        _fi({"supply_vehicles": "NaN"}, "supply_vehicles")


def test_fi_infinity_raises_source_read_error():
    """S-b2/S-n3: _fi must raise SourceReadError for 'Infinity', not bare OverflowError."""
    with pytest.raises(SourceReadError, match="non-finite"):
        _fi({"supply_vehicles": "Infinity"}, "supply_vehicles")


def test_fi_non_integral_raises_and_integral_double_reads():
    """S-c3-6: '12.0' reads as 12; '2.7' raises rather than truncating to 2."""
    assert _fi({"supply_vehicles": "12.0"}, "supply_vehicles") == 12
    with pytest.raises(SourceReadError, match="non-integral"):
        _fi({"supply_vehicles": "2.7"}, "supply_vehicles")


# ---------------------------------------------------------------------------
# DynamoDB: read_locations (2 pages)
# ---------------------------------------------------------------------------

def _ddb_client() -> Any:
    return boto3.client("dynamodb", region_name="us-east-1")


def test_read_locations_two_pages():
    client = _ddb_client()
    stub = Stubber(client)
    table = "cms-test-storage-fleet-locations"
    item1 = {
        "locationId": {"S": "loc-a"}, "name": {"S": "Airport"},
        "metro": {"S": "seattle"}, "locationType": {"S": "airport"},
        "latitude": {"N": "47.44"}, "longitude": {"N": "-122.30"},
        "parkingSpaces": {"N": "20"}, "operatingHours": {"S": "06-22"},
    }
    item2 = {
        "locationId": {"S": "loc-b"}, "name": {"S": "Downtown"},
        "metro": {"S": "seattle"}, "locationType": {"S": "downtown"},
        "latitude": {"N": "47.60"}, "longitude": {"N": "-122.33"},
        "parkingSpaces": {"N": "15"}, "operatingHours": {"S": "07-21"},
    }
    with stub:
        # page 1
        stub.add_response("query", {
            "Items": [item1], "Count": 1, "LastEvaluatedKey": {"locationId": {"S": "loc-a"}}
        })
        # page 2
        stub.add_response("query", {
            "Items": [item2], "Count": 1
        })
        locs, incomplete, _ = read_locations(_FLEET, ddb_client=client, table=table)

    assert incomplete == []
    assert len(locs) == 2
    ids = {l["locationId"] for l in locs}
    assert ids == {"loc-a", "loc-b"}
    loc_a = next(l for l in locs if l["locationId"] == "loc-a")
    assert loc_a["latitude"] == pytest.approx(47.44)
    assert loc_a["locationType"] == "airport"
    assert loc_a["op_hours"] == pytest.approx(16.0)   # 22 - 6
    loc_b = next(l for l in locs if l["locationId"] == "loc-b")
    assert loc_b["op_hours"] == pytest.approx(14.0)   # 21 - 7


def test_read_locations_error_raises_source_read_error():
    client = _ddb_client()
    stub = Stubber(client)
    with stub:
        stub.add_client_error("query", service_error_code="InternalServerError")
        with pytest.raises(SourceReadError):
            read_locations(_FLEET, ddb_client=client, table="cms-test-storage-fleet-locations")


# ---------------------------------------------------------------------------
# DynamoDB: read_locations — incomplete location filtering
# ---------------------------------------------------------------------------

def _complete_item(loc_id: str = "loc-x") -> dict:
    """A fully-valid location item."""
    return {
        "locationId": {"S": loc_id}, "name": {"S": "Test"},
        "metro": {"S": "seattle"}, "locationType": {"S": "airport"},
        "latitude": {"N": "47.44"}, "longitude": {"N": "-122.30"},
        "parkingSpaces": {"N": "10"}, "operatingHours": {"S": "06-22"},
    }


def _locs_with_one_bad(bad_item: dict) -> tuple:
    """Run read_locations against one good item and one bad item."""
    client = _ddb_client()
    stub = Stubber(client)
    good = _complete_item("loc-good")
    with stub:
        stub.add_response("query", {"Items": [good, bad_item], "Count": 2})
        return read_locations(_FLEET, ddb_client=client,
                              table="cms-test-storage-fleet-locations")


def test_location_missing_metro_is_incomplete():
    bad = _complete_item("loc-bad")
    del bad["metro"]
    locs, incomplete, _ = _locs_with_one_bad(bad)
    assert "loc-good" in {l["locationId"] for l in locs}
    assert "loc-bad" in incomplete
    assert len(locs) == 1


def test_location_missing_location_type_is_incomplete():
    bad = _complete_item("loc-bad")
    del bad["locationType"]
    locs, incomplete, _ = _locs_with_one_bad(bad)
    assert "loc-bad" in incomplete
    assert len(locs) == 1


def test_location_missing_latitude_is_incomplete():
    bad = _complete_item("loc-bad")
    del bad["latitude"]
    locs, incomplete, _ = _locs_with_one_bad(bad)
    assert "loc-bad" in incomplete
    assert len(locs) == 1


def test_location_malformed_latitude_is_incomplete():
    bad = _complete_item("loc-bad")
    bad["latitude"] = {"N": "not-a-number"}
    locs, incomplete, _ = _locs_with_one_bad(bad)
    assert "loc-bad" in incomplete
    assert len(locs) == 1


def test_location_missing_longitude_is_incomplete():
    bad = _complete_item("loc-bad")
    del bad["longitude"]
    locs, incomplete, _ = _locs_with_one_bad(bad)
    assert "loc-bad" in incomplete
    assert len(locs) == 1


def test_location_missing_parking_spaces_is_incomplete():
    bad = _complete_item("loc-bad")
    del bad["parkingSpaces"]
    locs, incomplete, _ = _locs_with_one_bad(bad)
    assert "loc-bad" in incomplete
    assert len(locs) == 1


def test_location_missing_operating_hours_is_incomplete():
    bad = _complete_item("loc-bad")
    del bad["operatingHours"]
    locs, incomplete, _ = _locs_with_one_bad(bad)
    assert "loc-bad" in incomplete
    assert len(locs) == 1


def test_location_malformed_operating_hours_format():
    bad = _complete_item("loc-bad")
    bad["operatingHours"] = {"S": "9am-5pm"}   # wrong format
    locs, incomplete, _ = _locs_with_one_bad(bad)
    assert "loc-bad" in incomplete
    assert len(locs) == 1


def test_location_operating_hours_open_not_less_than_close():
    bad = _complete_item("loc-bad")
    bad["operatingHours"] = {"S": "22-06"}   # open >= close
    locs, incomplete, _ = _locs_with_one_bad(bad)
    assert "loc-bad" in incomplete
    assert len(locs) == 1


def test_read_locations_incomplete_metros_empty_when_all_complete():
    """S-n2: third value is an empty set when all locations are complete."""
    client = _ddb_client()
    stub = Stubber(client)
    item = _complete_item("loc-ok")
    with stub:
        stub.add_response("query", {"Items": [item], "Count": 1})
        _, incomplete, incomplete_metros = read_locations(
            _FLEET, ddb_client=client, table="cms-test-storage-fleet-locations"
        )
    assert incomplete == []
    assert incomplete_metros == set()


def test_read_locations_incomplete_metros_contains_metro_of_bad_location():
    """S-n2: L1 mutant — incomplete_metros must name the bad location's metro."""
    bad = _complete_item("loc-bad")
    bad["operatingHours"] = {"S": "22-06"}   # invalid range → incomplete
    # _complete_item uses metro='seattle'
    locs, incomplete, incomplete_metros = _locs_with_one_bad(bad)
    assert "loc-bad" in incomplete
    assert "seattle" in incomplete_metros, (
        "incomplete_metros must contain the metro of every incomplete location"
    )


def test_location_op_hours_correct():
    """Complete location carries op_hours = close - open."""
    client = _ddb_client()
    stub = Stubber(client)
    item = _complete_item("loc-x")   # operatingHours = "06-22"
    with stub:
        stub.add_response("query", {"Items": [item], "Count": 1})
        locs, incomplete, _ = read_locations(_FLEET, ddb_client=client,
                                          table="cms-test-storage-fleet-locations")
    assert incomplete == []
    assert locs[0]["op_hours"] == pytest.approx(16.0)   # 22 - 6


def test_location_operating_hours_00_24_is_complete():
    """S-b4: operatingHours '00-24' (0 <= 0 < 24 <= 24) is valid and accepted."""
    client = _ddb_client()
    stub = Stubber(client)
    item = _complete_item("loc-fullday")
    item["operatingHours"] = {"S": "00-24"}
    with stub:
        stub.add_response("query", {"Items": [item], "Count": 1})
        locs, incomplete, _ = read_locations(_FLEET, ddb_client=client,
                                          table="cms-test-storage-fleet-locations")
    assert incomplete == []
    assert len(locs) == 1
    assert locs[0]["operatingHours"] == "00-24"
    assert locs[0]["op_hours"] == pytest.approx(24.0)


# ---------------------------------------------------------------------------
# DynamoDB: read_grounding (all outcomes)
# ---------------------------------------------------------------------------

def _ddb_grounding_setup(stub: Stubber, vin_items: list, grounding_items: list,
                          vin_table: str, grounding_table: str) -> None:
    """Stub vin-index query + groundings query."""
    stub.add_response("query", {"Items": vin_items, "Count": len(vin_items)})
    if len(vin_items) == 1:
        stub.add_response("query", {"Items": grounding_items, "Count": len(grounding_items)})


def test_grounding_open_grounding():
    """vin-index gives exactly one vehicleId; groundings returns an open record."""
    client = _ddb_client()
    stub = Stubber(client)
    with stub:
        _ddb_grounding_setup(stub,
            vin_items=[{"vehicleId": {"S": "vid-1"}}],
            grounding_items=[{"vehicleId": {"S": "vid-1"}}],  # open: no returnedAt
            vin_table="cms-test-storage-vehicles",
            grounding_table="cms-test-storage-vehicle-groundings",
        )
        is_grounded, err = read_grounding(
            "VIN-A", ddb_client=client,
            vehicles_table="cms-test-storage-vehicles",
            groundings_table="cms-test-storage-vehicle-groundings",
        )
    assert is_grounded is True
    assert err is False


def test_grounding_not_grounded():
    """vin-index gives one vehicleId; groundings returns empty (no open records)."""
    client = _ddb_client()
    stub = Stubber(client)
    with stub:
        _ddb_grounding_setup(stub,
            vin_items=[{"vehicleId": {"S": "vid-2"}}],
            grounding_items=[],
            vin_table="cms-test-storage-vehicles",
            grounding_table="cms-test-storage-vehicle-groundings",
        )
        is_grounded, err = read_grounding(
            "VIN-B", ddb_client=client,
            vehicles_table="cms-test-storage-vehicles",
            groundings_table="cms-test-storage-vehicle-groundings",
        )
    assert is_grounded is False
    assert err is False


def test_grounding_vin_index_zero_matches():
    """vin-index returns 0 items → (None, True) — registry unresolved."""
    client = _ddb_client()
    stub = Stubber(client)
    with stub:
        stub.add_response("query", {"Items": [], "Count": 0})
        is_grounded, err = read_grounding(
            "VIN-UNKNOWN", ddb_client=client,
            vehicles_table="cms-test-storage-vehicles",
            groundings_table="cms-test-storage-vehicle-groundings",
        )
    assert is_grounded is None
    assert err is True


def test_grounding_vin_index_two_matches():
    """vin-index returns 2 items (ambiguous) → (None, True)."""
    client = _ddb_client()
    stub = Stubber(client)
    with stub:
        stub.add_response("query", {
            "Items": [{"vehicleId": {"S": "vid-3"}}, {"vehicleId": {"S": "vid-4"}}],
            "Count": 2,
        })
        is_grounded, err = read_grounding(
            "VIN-AMB", ddb_client=client,
            vehicles_table="cms-test-storage-vehicles",
            groundings_table="cms-test-storage-vehicle-groundings",
        )
    assert is_grounded is None
    assert err is True


def test_grounding_vin_differs_from_vehicle_id():
    """VIN != vehicleId is normal; vin-index maps VIN to vehicleId."""
    client = _ddb_client()
    stub = Stubber(client)
    with stub:
        # vin-index: VIN "MRDVEH00000000021" → vehicleId "veh-uuid-abc"
        stub.add_response("query", {
            "Items": [{"vehicleId": {"S": "veh-uuid-abc"}}], "Count": 1
        })
        # groundings query: no open records
        stub.add_response("query", {"Items": [], "Count": 0})
        is_grounded, err = read_grounding(
            "MRDVEH00000000021", ddb_client=client,
            vehicles_table="cms-test-storage-vehicles",
            groundings_table="cms-test-storage-vehicle-groundings",
        )
    assert is_grounded is False
    assert err is False


def test_grounding_exception_returns_none_true():
    """Any exception during the read → (None, True)."""
    client = _ddb_client()
    stub = Stubber(client)
    with stub:
        stub.add_client_error("query", service_error_code="ProvisionedThroughputExceededException")
        is_grounded, err = read_grounding(
            "VIN-ERR", ddb_client=client,
            vehicles_table="cms-test-storage-vehicles",
            groundings_table="cms-test-storage-vehicle-groundings",
        )
    assert is_grounded is None
    assert err is True


# ---------------------------------------------------------------------------
# DynamoDB: read_open_items (paging)
# ---------------------------------------------------------------------------

def test_read_open_items_paging_and_filter():
    """Two pages; returns ALL live items regardless of severity (W8 fix).

    The DynamoDB filter is now ``expires_at > :now`` only; eligibility
    disqualifies p0/attention in Python.  The reader returns every live item
    with its severity so lower-severity items appear as evidence.
    """
    client = _ddb_client()
    stub = Stubber(client)
    fi_table = "vsa-test-health-findings"
    with stub:
        # page 1: p0 item + next token
        stub.add_response("query", {
            "Items": [{"finding_id": {"S": "FIND-001"}, "severity": {"S": "p0"},
                       "vin": {"S": "VIN-X"}, "expires_at": {"S": "2026-11-01 00:00:00"}}],
            "Count": 1,
            "LastEvaluatedKey": {"vin": {"S": "VIN-X"}, "finding_id": {"S": "FIND-001"}},
        })
        # page 2: routine item (lower severity) — returned by the new filter
        stub.add_response("query", {
            "Items": [{"finding_id": {"S": "FIND-002"}, "severity": {"S": "routine"},
                       "vin": {"S": "VIN-X"}, "expires_at": {"S": "2026-11-01 00:00:00"}}],
            "Count": 1,
        })
        items, err = read_open_items(
            "VIN-X", ddb_client=client, findings_table=fi_table,
            now_iso="2026-10-06T12:00:00Z",
        )
    assert err is False
    assert len(items) == 2
    severities = {it["severity"] for it in items}
    # W8: all live items returned, not just p0/attention
    assert severities == {"p0", "routine"}


def test_read_open_items_error_returns_none_true():
    client = _ddb_client()
    stub = Stubber(client)
    with stub:
        stub.add_client_error("query", service_error_code="InternalServerError")
        items, err = read_open_items(
            "VIN-ERR", ddb_client=client,
            findings_table="vsa-test-health-findings",
            now_iso="2026-10-06T12:00:00Z",
        )
    assert items is None
    assert err is True


# ---------------------------------------------------------------------------
# build_vehicle_facts
# ---------------------------------------------------------------------------

_NOW_DT = datetime(2026, 10, 6, 12, 0, 0)
_TODAY = "2026-10-06"


def _make_vd(vin: str, available_hours: float = 8.0) -> dict:
    return {"vin": vin, "location_id": "loc-a", "available_hours": available_hours}


def _make_util(vin: str, available_hours: float, on_rent_hours: float) -> dict:
    return {"vin": vin, "snapshot_date": "2026-10-06",
            "available_hours": available_hours, "on_rent_hours": on_rent_hours}


def _make_busy(vin: str, start: str, end: str) -> dict:
    """Build a busy row with naive UTC datetimes (as returned by read_busy_windows)."""
    return {"vin": vin, "location_id": "loc-a",
            "start_at": datetime.strptime(start, "%Y-%m-%d %H:%M:%S"),
            "end_at": datetime.strptime(end, "%Y-%m-%d %H:%M:%S")}


def test_build_in_service_true():
    facts = build_vehicle_facts(
        "VIN-1", location_id="loc-a",
        vehicle_days_rows=[_make_vd("VIN-1", 8.0)],
        utilization_rows=[], busy_rows=[], open_ros_rows=[], service_rows=[],
        now_dt=_NOW_DT, today_str=_TODAY,
    )
    assert facts["in_service"] is True


def test_build_in_service_false_zero_hours():
    facts = build_vehicle_facts(
        "VIN-1", location_id="loc-a",
        vehicle_days_rows=[_make_vd("VIN-1", 0.0)],
        utilization_rows=[], busy_rows=[], open_ros_rows=[], service_rows=[],
        now_dt=_NOW_DT, today_str=_TODAY,
    )
    assert facts["in_service"] is False


def test_build_days_idle_last_7():
    util = [
        _make_util("VIN-1", 8.0, 0.0),   # idle
        _make_util("VIN-1", 8.0, 4.0),   # not idle
        _make_util("VIN-1", 0.0, 0.0),   # not idle (available=0)
        _make_util("VIN-1", 8.0, 0.0),   # idle
    ]
    facts = build_vehicle_facts(
        "VIN-1", location_id="loc-a",
        vehicle_days_rows=[_make_vd("VIN-1")],
        utilization_rows=util, busy_rows=[], open_ros_rows=[], service_rows=[],
        now_dt=_NOW_DT, today_str=_TODAY,
    )
    assert facts["days_idle_last_7"] == 2


def test_build_days_to_service_open_ro():
    """Open RO → days_to_next_service = 0.0."""
    facts = build_vehicle_facts(
        "VIN-1", location_id="loc-a",
        vehicle_days_rows=[_make_vd("VIN-1")],
        utilization_rows=[], busy_rows=[],
        open_ros_rows=[{"ro_id": "ro-1", "vehicle_vin": "VIN-1", "status": "In Progress"}],
        service_rows=[],
        now_dt=_NOW_DT, today_str=_TODAY,
    )
    assert facts["days_to_next_service"] == pytest.approx(0.0)


def test_build_days_to_service_from_date():
    """Service scheduled 5 days out → days_to_next_service = 5.0."""
    facts = build_vehicle_facts(
        "VIN-1", location_id="loc-a",
        vehicle_days_rows=[_make_vd("VIN-1")],
        utilization_rows=[], busy_rows=[], open_ros_rows=[],
        service_rows=[{"vin": "VIN-1", "service_date": "2026-10-11"}],
        now_dt=_NOW_DT, today_str=_TODAY,
    )
    assert facts["days_to_next_service"] == pytest.approx(5.0)


def test_build_days_to_service_inf_when_none():
    facts = build_vehicle_facts(
        "VIN-1", location_id="loc-a",
        vehicle_days_rows=[_make_vd("VIN-1")],
        utilization_rows=[], busy_rows=[], open_ros_rows=[], service_rows=[],
        now_dt=_NOW_DT, today_str=_TODAY,
    )
    import math
    assert math.isinf(facts["days_to_next_service"])


def test_build_hours_to_booking_on_rent_now():
    """start_at <= now < end_at → hours_to_next_booking = 0.0."""
    busy = [_make_busy("VIN-1", "2026-10-06 10:00:00", "2026-10-06 14:00:00")]
    facts = build_vehicle_facts(
        "VIN-1", location_id="loc-a",
        vehicle_days_rows=[_make_vd("VIN-1")],
        utilization_rows=[], busy_rows=busy, open_ros_rows=[], service_rows=[],
        now_dt=_NOW_DT, today_str=_TODAY,
    )
    assert facts["hours_to_next_booking"] == pytest.approx(0.0)


def test_build_hours_to_booking_upcoming():
    """start_at 6h ahead → hours_to_next_booking ≈ 6.0."""
    busy = [_make_busy("VIN-1", "2026-10-06 18:00:00", "2026-10-07 08:00:00")]
    facts = build_vehicle_facts(
        "VIN-1", location_id="loc-a",
        vehicle_days_rows=[_make_vd("VIN-1")],
        utilization_rows=[], busy_rows=busy, open_ros_rows=[], service_rows=[],
        now_dt=_NOW_DT, today_str=_TODAY,
    )
    assert facts["hours_to_next_booking"] == pytest.approx(6.0, rel=1e-4)


def test_build_hours_to_booking_inf_no_bookings():
    import math
    facts = build_vehicle_facts(
        "VIN-1", location_id="loc-a",
        vehicle_days_rows=[_make_vd("VIN-1")],
        utilization_rows=[], busy_rows=[], open_ros_rows=[], service_rows=[],
        now_dt=_NOW_DT, today_str=_TODAY,
    )
    assert math.isinf(facts["hours_to_next_booking"])


def test_build_vehicle_facts_tz_aware_now_dt():
    """A tz-aware now_dt is normalised to naive UTC; booking hours still correct."""
    now_aware = datetime(2026, 10, 6, 12, 0, 0, tzinfo=timezone.utc)
    # booking starts 6 h after now (18:00 UTC)
    busy = [_make_busy("VIN-1", "2026-10-06 18:00:00", "2026-10-07 08:00:00")]
    facts = build_vehicle_facts(
        "VIN-1", location_id="loc-a",
        vehicle_days_rows=[_make_vd("VIN-1")],
        utilization_rows=[], busy_rows=busy, open_ros_rows=[], service_rows=[],
        now_dt=now_aware, today_str=_TODAY,
    )
    assert facts["hours_to_next_booking"] == pytest.approx(6.0, rel=1e-4)



# ---------------------------------------------------------------------------
# Architect check (2026-10-10, mutants S1-S4): the grounding read is U7's input, so each
# failure direction is pinned with the groundings query's own response queued. Without it a
# mutant that makes an extra call meets a StubResponseError that the reader's own except
# turns into (None, True), the expected answer.
# ---------------------------------------------------------------------------

_VT = "cms-test-storage-vehicles"
_GT = "cms-test-storage-vehicle-groundings"


def _groundings_params(vehicle_id: str, start_key: dict | None = None) -> dict:
    params = {
        "TableName": _GT,
        "KeyConditionExpression": "#v = :vid",
        "ExpressionAttributeNames": {"#v": "vehicleId"},
        "ExpressionAttributeValues": {":vid": {"S": vehicle_id}, ":null": {"S": "NULL"}},
        "FilterExpression": "attribute_not_exists(returnedAt) OR attribute_type(returnedAt, :null)",
    }
    if start_key:
        params["ExclusiveStartKey"] = start_key
    return params


def test_grounding_two_matches_never_reads_groundings():
    client = _ddb_client()
    stub = Stubber(client)
    with stub:
        stub.add_response("query", {
            "Items": [{"vehicleId": {"S": "VEH-0003"}}, {"vehicleId": {"S": "VEH-0004"}}], "Count": 2})
        # A not-grounded answer the correct reader never asks for: a reader that takes the
        # first match would read it and report the vehicle movable.
        stub.add_response("query", {"Items": [], "Count": 0})
        result = read_grounding("MRDVEH00000000041", ddb_client=client,
                                vehicles_table=_VT, groundings_table=_GT)
    assert result == (None, True)


def test_grounding_groundings_table_error_is_unknown():
    client = _ddb_client()
    stub = Stubber(client)
    with stub:
        stub.add_response("query", {"Items": [{"vehicleId": {"S": "VEH-0005"}}], "Count": 1})
        stub.add_client_error("query", service_error_code="ProvisionedThroughputExceededException")
        result = read_grounding("MRDVEH00000000042", ddb_client=client,
                                vehicles_table=_VT, groundings_table=_GT)
    assert result == (None, True)


def test_grounding_open_row_on_second_page_is_grounded():
    client = _ddb_client()
    stub = Stubber(client)
    lek = {"vehicleId": {"S": "VEH-0006"}, "actionId": {"S": "GRD-1"}}
    with stub:
        stub.add_response("query", {"Items": [{"vehicleId": {"S": "VEH-0006"}}], "Count": 1})
        stub.add_response("query", {"Items": [], "Count": 0, "LastEvaluatedKey": lek},
                          expected_params=_groundings_params("VEH-0006"))
        stub.add_response("query", {"Items": [{"vehicleId": {"S": "VEH-0006"}}], "Count": 1},
                          expected_params=_groundings_params("VEH-0006", lek))
        result = read_grounding("MRDVEH00000000043", ddb_client=client,
                                vehicles_table=_VT, groundings_table=_GT)
        stub.assert_no_pending_responses()
    assert result == (True, False)


def test_grounding_query_counts_a_null_returned_at_as_open():
    """Stubber can't evaluate the filter, so the exact request is the pin: CMS writes
    returnedAt as DynamoDB NULL on an open grounding, and only attribute_type sees it."""
    client = _ddb_client()
    stub = Stubber(client)
    with stub:
        stub.add_response("query", {"Items": [{"vehicleId": {"S": "VEH-0007"}}], "Count": 1})
        stub.add_response("query", {"Items": [], "Count": 0},
                          expected_params=_groundings_params("VEH-0007"))
        result = read_grounding("MRDVEH00000000044", ddb_client=client,
                                vehicles_table=_VT, groundings_table=_GT)
    assert result == (False, False)


def test_read_open_items_refuses_a_now_the_table_cannot_compare():
    """'2026-10-06T01:00:00Z' > '2026-10-06 12:00:00' as strings: a row that expired at 01:00
    would read as live. The reader takes only the table's own format."""
    with pytest.raises(ValueError):
        read_open_items("MRDVEH00000000045", ddb_client=None,
                        findings_table="vsa-test-avx-findings", now_iso="2026-10-06 12:00:00")


def test_read_open_items_query_is_pinned():
    """Pin the DynamoDB filter to expires_at > :now only (W8: severity filter removed)."""
    client = _ddb_client()
    stub = Stubber(client)
    with stub:
        stub.add_response("query", {"Items": [], "Count": 0}, expected_params={
            "TableName": "vsa-test-avx-findings",
            "IndexName": "by_vin",
            "KeyConditionExpression": "#v = :vin",
            "ExpressionAttributeNames": {"#v": "vin"},
            "ExpressionAttributeValues": {
                ":vin": {"S": "MRDVEH00000000046"}, ":now": {"S": "2026-10-06T12:00:00Z"},
            },
            "FilterExpression": "expires_at > :now",
        })
        result = read_open_items("MRDVEH00000000046", ddb_client=client,
                                 findings_table="vsa-test-avx-findings",
                                 now_iso="2026-10-06T12:00:00Z")
    assert result == ([], False)
