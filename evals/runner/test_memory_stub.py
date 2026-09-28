"""Tests for MemoryStub — in-process AgentCore Memory mock (Group 6.A).

Covers: retrieve, list, create_event, batch_create, metadata-filter
AND-composite semantics, and namespace isolation.

See evals/runner/mocks.py for the MemoryStub implementation.
"""
from __future__ import annotations

import json

import pytest

from evals.runner.mocks import MemoryStub, seed_persona_memory


MEM_ID = "test-mem-001"
VIN = "1FMCU0J91KUB12345"
CUST = "cust-test-001"


@pytest.fixture
def stub() -> MemoryStub:
    s = MemoryStub()
    seed_persona_memory(
        stub=s,
        memory_id=MEM_ID,
        persona_id="fleet_driver",
        vehicle_id=VIN,
        customer_id=CUST,
        fixtures={
            "vehicle_identity": {"vin": VIN, "make": "Ford", "model": "Explorer", "year": 2023},
            "service_records": [
                {"service_date": "2024-01-15", "mileage": 32000, "work": "Oil change"},
                {"service_date": "2023-07-01", "mileage": 28000, "work": "Brake inspection"},
            ],
        },
        tenant_id="fleet",
    )
    return s


# ---------------------------------------------------------------------------
# Test 1: retrieve returns records matching a single metadata filter
# ---------------------------------------------------------------------------


def test_retrieve_single_filter(stub: MemoryStub):
    """retrieve_memory_records returns records matching a single EQUALS_TO filter."""
    resp = stub.retrieve_memory_records(
        memoryId=MEM_ID,
        searchCriteria={
            "searchQuery": "vehicle identity",
            "metadataFilters": [
                {
                    "left": {"metadataKey": "data_source"},
                    "operator": "EQUALS_TO",
                    "right": {"metadataValue": {"stringValue": "vehicle_identity"}},
                }
            ],
        },
    )
    records = resp["memoryRecords"]
    assert len(records) == 1
    data = json.loads(records[0]["content"]["text"])
    assert data["make"] == "Ford"


# ---------------------------------------------------------------------------
# Test 2: list_memory_records returns all records (no filter)
# ---------------------------------------------------------------------------


def test_list_returns_all_records(stub: MemoryStub):
    """list_memory_records with no filters returns all records for memory_id."""
    resp = stub.list_memory_records(memoryId=MEM_ID)
    records = resp["memoryRecords"]
    # 1 vehicle_identity + 2 service_records = 3
    assert len(records) == 3


# ---------------------------------------------------------------------------
# Test 3: create_event stores a record retrievable afterward
# ---------------------------------------------------------------------------


def test_create_event_stored_and_retrievable(stub: MemoryStub):
    """create_event persists content; subsequent retrieve returns it."""
    stub.create_event(
        memoryId=MEM_ID,
        actorId="vehicle_owner",
        sessionId="sess-001",
        payload=[
            {
                "conversational": {
                    "content": {"text": json.dumps({"vin": VIN, "event": "new_event"})},
                    "role": "ASSISTANT",
                }
            }
        ],
        metadata={
            "persona_id": {"stringValue": "vehicle_owner"},
            "vehicle_id": {"stringValue": VIN},
            "data_source": {"stringValue": "ota_campaigns"},
            "tenant_id": {"stringValue": "oem"},
        },
    )
    # Retrieve with filter to confirm it was stored
    resp = stub.retrieve_memory_records(
        memoryId=MEM_ID,
        searchCriteria={
            "searchQuery": "ota event",
            "metadataFilters": [
                {
                    "left": {"metadataKey": "data_source"},
                    "operator": "EQUALS_TO",
                    "right": {"metadataValue": {"stringValue": "ota_campaigns"}},
                }
            ],
        },
    )
    assert len(resp["memoryRecords"]) == 1
    data = json.loads(resp["memoryRecords"][0]["content"]["text"])
    assert data["event"] == "new_event"


# ---------------------------------------------------------------------------
# Test 4: batch_create_memory_records stores multiple records
# ---------------------------------------------------------------------------


def test_batch_create_multiple_records():
    """batch_create_memory_records stores all records and returns SUCCESS status."""
    s = MemoryStub()
    result = s.batch_create_memory_records(
        memoryId=MEM_ID,
        records=[
            {
                "namespaces": ["cvx/fleet/fleet_driver"],
                "content": {"text": json.dumps({"session_date": "2024-06-01", "kwh": 45.2})},
                "metadata": {
                    "data_source": {"stringValue": "charging_sessions"},
                    "vehicle_id": {"stringValue": VIN},
                },
            },
            {
                "namespaces": ["cvx/fleet/fleet_driver"],
                "content": {"text": json.dumps({"session_date": "2024-06-02", "kwh": 30.0})},
                "metadata": {
                    "data_source": {"stringValue": "charging_sessions"},
                    "vehicle_id": {"stringValue": VIN},
                },
            },
        ],
    )
    assert len(result["records"]) == 2
    assert all(r["status"] == "SUCCESS" for r in result["records"])
    # Confirm both are stored
    resp = s.retrieve_memory_records(
        memoryId=MEM_ID,
        searchCriteria={
            "searchQuery": "charging",
            "metadataFilters": [
                {
                    "left": {"metadataKey": "data_source"},
                    "operator": "EQUALS_TO",
                    "right": {"metadataValue": {"stringValue": "charging_sessions"}},
                }
            ],
        },
    )
    assert len(resp["memoryRecords"]) == 2


# ---------------------------------------------------------------------------
# Test 5: AND-composite metadata filters (multi-key)
# ---------------------------------------------------------------------------


def test_and_composite_metadata_filters(stub: MemoryStub):
    """Composite filters apply AND logic — only records matching ALL filters returned."""
    # Seed a second persona's vehicle_identity with same data_source but different persona
    seed_persona_memory(
        stub=stub,
        memory_id=MEM_ID,
        persona_id="vehicle_owner",
        vehicle_id="DIFFERENT_VIN",
        customer_id="cust-999",
        fixtures={"vehicle_identity": {"vin": "DIFFERENT_VIN", "make": "Honda"}},
        tenant_id="oem",
    )

    # Filter on both data_source AND vehicle_id — should return only fleet_driver's record
    resp = stub.retrieve_memory_records(
        memoryId=MEM_ID,
        searchCriteria={
            "searchQuery": "vehicle identity",
            "metadataFilters": [
                {
                    "left": {"metadataKey": "data_source"},
                    "operator": "EQUALS_TO",
                    "right": {"metadataValue": {"stringValue": "vehicle_identity"}},
                },
                {
                    "left": {"metadataKey": "vehicle_id"},
                    "operator": "EQUALS_TO",
                    "right": {"metadataValue": {"stringValue": VIN}},
                },
            ],
        },
    )
    records = resp["memoryRecords"]
    assert len(records) == 1
    data = json.loads(records[0]["content"]["text"])
    assert data["make"] == "Ford"


# ---------------------------------------------------------------------------
# Test 6: Namespace isolation — different memory_ids don't cross-contaminate
# ---------------------------------------------------------------------------


def test_namespace_isolation():
    """Records in different memory IDs are isolated from each other."""
    s = MemoryStub()
    s.batch_create_memory_records(
        memoryId="mem-A",
        records=[{
            "namespaces": ["cvx/fleet/fleet_driver"],
            "content": {"text": json.dumps({"vin": VIN})},
            "metadata": {"data_source": {"stringValue": "vehicle_identity"}},
        }],
    )
    s.batch_create_memory_records(
        memoryId="mem-B",
        records=[{
            "namespaces": ["cvx/fleet/fleet_driver"],
            "content": {"text": json.dumps({"vin": "OTHER_VIN"})},
            "metadata": {"data_source": {"stringValue": "vehicle_identity"}},
        }],
    )

    resp_a = s.retrieve_memory_records(
        memoryId="mem-A",
        searchCriteria={"searchQuery": "vehicle", "metadataFilters": []},
    )
    resp_b = s.retrieve_memory_records(
        memoryId="mem-B",
        searchCriteria={"searchQuery": "vehicle", "metadataFilters": []},
    )

    assert len(resp_a["memoryRecords"]) == 1
    assert len(resp_b["memoryRecords"]) == 1
    data_a = json.loads(resp_a["memoryRecords"][0]["content"]["text"])
    data_b = json.loads(resp_b["memoryRecords"][0]["content"]["text"])
    assert data_a["vin"] != data_b["vin"]


# ---------------------------------------------------------------------------
# Test 7: GREATER_THAN filter for numeric last_updated (TTL gating)
# ---------------------------------------------------------------------------


def test_greater_than_number_filter():
    """GREATER_THAN operator works on numberValue metadata (last_updated TTL check)."""
    import time

    s = MemoryStub()
    now = int(time.time() * 1000)

    s.batch_create_memory_records(
        memoryId=MEM_ID,
        records=[
            {
                "namespaces": ["cvx/fleet/fleet_driver"],
                "content": {"text": json.dumps({"product": "recent"})},
                "metadata": {
                    "last_updated": {"numberValue": float(now)},
                    "data_source": {"stringValue": "vehicle_identity"},
                },
            },
            {
                "namespaces": ["cvx/fleet/fleet_driver"],
                "content": {"text": json.dumps({"product": "old"})},
                "metadata": {
                    "last_updated": {"numberValue": float(now - 3_600_000)},  # 1h ago
                    "data_source": {"stringValue": "vehicle_identity"},
                },
            },
        ],
    )

    # Filter: last_updated > (now - 30min)
    threshold = float(now - 1_800_000)
    resp = s.retrieve_memory_records(
        memoryId=MEM_ID,
        searchCriteria={
            "searchQuery": "fresh records",
            "metadataFilters": [
                {
                    "left": {"metadataKey": "last_updated"},
                    "operator": "GREATER_THAN",
                    "right": {"metadataValue": {"numberValue": threshold}},
                }
            ],
        },
    )
    records = resp["memoryRecords"]
    assert len(records) == 1
    data = json.loads(records[0]["content"]["text"])
    assert data["product"] == "recent"


# ---------------------------------------------------------------------------
# Test 8: get_memory_record by ID
# ---------------------------------------------------------------------------


def test_get_memory_record_by_id():
    """get_memory_record retrieves a specific record by its ID."""
    s = MemoryStub()
    batch_result = s.batch_create_memory_records(
        memoryId=MEM_ID,
        records=[{
            "namespaces": ["cvx/oem/vehicle_owner"],
            "content": {"text": json.dumps({"customer_id": CUST})},
            "metadata": {"data_source": {"stringValue": "customer_360"}},
        }],
    )
    record_id = batch_result["records"][0]["recordId"]

    result = s.get_memory_record(memoryId=MEM_ID, memoryRecordId=record_id)
    assert result["memoryRecord"]["recordId"] == record_id
    data = json.loads(result["memoryRecord"]["content"]["text"])
    assert data["customer_id"] == CUST
