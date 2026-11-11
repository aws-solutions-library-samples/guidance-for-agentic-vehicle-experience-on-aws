"""Tests for Memory-backed tabular tool factories (Group 3.C).

Strategy:
- Un-skip ONE test per tool for the happy path / PySpark-gating assertion.
- Memory-empty tests stay skipped — they need the Memory stub from Group 6.A.
- boto3 calls are patched via unittest.mock to avoid real AWS calls.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _make_session(
    persona_id="fleet_driver",
    vin="1FMCU0J91KUB12345",
    customer_id="cust-123",
    tenant_id="fleet",
):
    return SimpleNamespace(
        persona_id=persona_id,
        vin=vin,
        customer_id=customer_id,
        tenant_id=tenant_id,
    )


def _make_config(memory_id="mem-001", memory_region="us-west-2"):
    return SimpleNamespace(memory_id=memory_id, memory_region=memory_region)


def _memory_record(payload_dict: dict) -> dict:
    """Wrap a payload dict in the AgentCore Memory record shape."""
    return {"content": {"text": json.dumps(payload_dict)}}


def _memory_response(records: list) -> dict:
    return {"memoryRecords": records}


# ---------------------------------------------------------------------------
# vehicle_identity
# ---------------------------------------------------------------------------


def test_vehicle_identity_happy_path():
    from agents.supervisor.tools.vehicle_identity import make_vehicle_identity_tool

    session = _make_session()
    config = _make_config()

    record = _memory_record({"vin": "1FMCU0J91KUB12345", "make": "Ford", "model": "Explorer", "year": 2023})

    mock_client = MagicMock()
    mock_client.retrieve_memory_records.return_value = _memory_response([record])

    with patch("agents.supervisor.tools.vehicle_identity.boto3") as mock_boto3:
        mock_boto3.client.return_value = mock_client
        tool_fn = make_vehicle_identity_tool(session, config)
        result = tool_fn("1FMCU0J91KUB12345")

    assert result.get("make") == "Ford"
    assert result.get("found") is True


def test_vehicle_identity_memory_empty():
    """With no matching records in Memory, tool returns structured unavailable response."""
    from agents.supervisor.tools.vehicle_identity import make_vehicle_identity_tool

    session = _make_session()
    config = _make_config()

    mock_client = MagicMock()
    mock_client.retrieve_memory_records.return_value = _memory_response([])

    with patch("agents.supervisor.tools.vehicle_identity.boto3") as mock_boto3:
        mock_boto3.client.return_value = mock_client
        tool_fn = make_vehicle_identity_tool(session, config)
        result = tool_fn("1FMCU0J91KUB12345")

    assert result["available"] is False
    assert "reason" in result


# ---------------------------------------------------------------------------
# service_history
# ---------------------------------------------------------------------------


def test_service_history_happy_path():
    from agents.supervisor.tools.service_history import make_service_history_tool

    session = _make_session()
    config = _make_config()

    records = [
        _memory_record({"service_date": "2024-01-15", "mileage": 32000, "work": "Oil change"}),
        _memory_record({"service_date": "2023-07-01", "mileage": 28000, "work": "Brake inspection"}),
    ]

    mock_client = MagicMock()
    mock_client.retrieve_memory_records.return_value = _memory_response(records)

    with patch("agents.supervisor.tools.service_history.boto3") as mock_boto3:
        mock_boto3.client.return_value = mock_client
        tool_fn = make_service_history_tool(session, config)
        result = tool_fn()

    assert result["available"] is True
    assert result["count"] == 2
    assert result["records"][0]["work"] == "Oil change"


def test_service_history_memory_empty():
    """With no matching records, service_history returns structured unavailable response."""
    from agents.supervisor.tools.service_history import make_service_history_tool

    session = _make_session()
    config = _make_config()

    mock_client = MagicMock()
    mock_client.retrieve_memory_records.return_value = _memory_response([])

    with patch("agents.supervisor.tools.service_history.boto3") as mock_boto3:
        mock_boto3.client.return_value = mock_client
        tool_fn = make_service_history_tool(session, config)
        result = tool_fn()

    assert result["available"] is False
    assert "reason" in result


# ---------------------------------------------------------------------------
# charging_history
# ---------------------------------------------------------------------------


def test_charging_history_happy_path():
    from agents.supervisor.tools.charging_history import make_charging_history_tool

    session = _make_session()
    config = _make_config()

    records = [
        _memory_record({"session_date": "2024-06-01", "kwh": 45.2, "location": "Home"}),
    ]

    mock_client = MagicMock()
    mock_client.retrieve_memory_records.return_value = _memory_response(records)

    with patch("agents.supervisor.tools.charging_history.boto3") as mock_boto3:
        mock_boto3.client.return_value = mock_client
        tool_fn = make_charging_history_tool(session, config)
        result = tool_fn()

    assert result["available"] is True
    assert result["sessions"][0]["kwh"] == 45.2


def test_charging_history_memory_empty():
    """With no matching records, charging_history returns structured unavailable response."""
    from agents.supervisor.tools.charging_history import make_charging_history_tool

    session = _make_session()
    config = _make_config()

    mock_client = MagicMock()
    mock_client.retrieve_memory_records.return_value = _memory_response([])

    with patch("agents.supervisor.tools.charging_history.boto3") as mock_boto3:
        mock_boto3.client.return_value = mock_client
        tool_fn = make_charging_history_tool(session, config)
        result = tool_fn()

    assert result["available"] is False
    assert "reason" in result


# ---------------------------------------------------------------------------
# customer_profile
# ---------------------------------------------------------------------------


def test_customer_profile_happy_path():
    from agents.supervisor.tools.customer_profile import make_customer_profile_tool

    session = _make_session()
    config = _make_config()

    record = _memory_record({
        "customer_id": "cust-123",
        "name": "Jane Doe",
        "loyalty_tier": "gold",
        "vehicles_owned": 2,
    })

    mock_client = MagicMock()
    mock_client.retrieve_memory_records.return_value = _memory_response([record])

    with patch("agents.supervisor.tools.customer_profile.boto3") as mock_boto3:
        mock_boto3.client.return_value = mock_client
        tool_fn = make_customer_profile_tool(session, config)
        result = tool_fn()

    assert result["available"] is True
    assert result["loyalty_tier"] == "gold"


def test_customer_profile_memory_empty():
    """With no matching records, customer_profile returns structured unavailable response."""
    from agents.supervisor.tools.customer_profile import make_customer_profile_tool

    session = _make_session()
    config = _make_config()

    mock_client = MagicMock()
    mock_client.retrieve_memory_records.return_value = _memory_response([])

    with patch("agents.supervisor.tools.customer_profile.boto3") as mock_boto3:
        mock_boto3.client.return_value = mock_client
        tool_fn = make_customer_profile_tool(session, config)
        result = tool_fn()

    assert result["available"] is False
    assert "reason" in result


# ---------------------------------------------------------------------------
# interaction_history
# ---------------------------------------------------------------------------


def test_interaction_history_happy_path():
    from agents.supervisor.tools.interaction_history import make_interaction_history_tool

    session = _make_session()
    config = _make_config()

    records = [
        _memory_record({"interaction_date": "2024-05-10", "channel": "phone", "resolution": "resolved"}),
    ]

    mock_client = MagicMock()
    mock_client.retrieve_memory_records.return_value = _memory_response(records)

    with patch("agents.supervisor.tools.interaction_history.boto3") as mock_boto3:
        mock_boto3.client.return_value = mock_client
        tool_fn = make_interaction_history_tool(session, config)
        result = tool_fn()

    assert result["available"] is True
    assert result["interactions"][0]["channel"] == "phone"


def test_interaction_history_memory_empty():
    """With no matching records, interaction_history returns structured unavailable response."""
    from agents.supervisor.tools.interaction_history import make_interaction_history_tool

    session = _make_session()
    config = _make_config()

    mock_client = MagicMock()
    mock_client.retrieve_memory_records.return_value = _memory_response([])

    with patch("agents.supervisor.tools.interaction_history.boto3") as mock_boto3:
        mock_boto3.client.return_value = mock_client
        tool_fn = make_interaction_history_tool(session, config)
        result = tool_fn()

    assert result["available"] is False
    assert "reason" in result


# ---------------------------------------------------------------------------
# ota_history
# ---------------------------------------------------------------------------


def test_ota_history_happy_path():
    from agents.supervisor.tools.ota_history import make_ota_history_tool

    session = _make_session()
    config = _make_config()

    records = [
        _memory_record({"campaign_name": "FW-2024-001", "dispatch_date": "2024-03-15", "status": "installed"}),
    ]

    mock_client = MagicMock()
    mock_client.retrieve_memory_records.return_value = _memory_response(records)

    with patch("agents.supervisor.tools.ota_history.boto3") as mock_boto3:
        mock_boto3.client.return_value = mock_client
        tool_fn = make_ota_history_tool(session, config)
        result = tool_fn()

    assert result["available"] is True
    assert result["campaigns"][0]["status"] == "installed"


def test_ota_history_memory_empty():
    """With no matching records, ota_history returns structured unavailable response."""
    from agents.supervisor.tools.ota_history import make_ota_history_tool

    session = _make_session()
    config = _make_config()

    mock_client = MagicMock()
    mock_client.retrieve_memory_records.return_value = _memory_response([])

    with patch("agents.supervisor.tools.ota_history.boto3") as mock_boto3:
        mock_boto3.client.return_value = mock_client
        tool_fn = make_ota_history_tool(session, config)
        result = tool_fn()

    assert result["available"] is False
    assert "reason" in result


# ---------------------------------------------------------------------------
# telemetry_summary (PySpark-gated)
# ---------------------------------------------------------------------------


@pytest.mark.skip(reason="needs Group 6.A Memory stub — happy path requires real data")
def test_telemetry_summary_happy_path():
    pass


def test_telemetry_summary_returns_pyspark_gating_message_when_no_records():
    from agents.supervisor.tools.telemetry_summary import make_telemetry_summary_tool

    session = _make_session()
    config = _make_config()

    mock_client = MagicMock()
    mock_client.retrieve_memory_records.return_value = _memory_response([])

    with patch("agents.supervisor.tools.telemetry_summary.boto3") as mock_boto3:
        mock_boto3.client.return_value = mock_client
        tool_fn = make_telemetry_summary_tool(session, config)
        result = tool_fn()

    assert result["available"] is False
    assert result["tracking_row"] == "PySpark via Glue"
    assert "PySpark" in result["reason"]


# ---------------------------------------------------------------------------
# energy_summary (PySpark-gated)
# ---------------------------------------------------------------------------


@pytest.mark.skip(reason="needs Group 6.A Memory stub — happy path requires real data")
def test_energy_summary_happy_path():
    pass


def test_energy_summary_returns_pyspark_gating_message_when_no_records():
    from agents.supervisor.tools.energy_summary import make_energy_summary_tool

    session = _make_session()
    config = _make_config()

    mock_client = MagicMock()
    mock_client.retrieve_memory_records.return_value = _memory_response([])

    with patch("agents.supervisor.tools.energy_summary.boto3") as mock_boto3:
        mock_boto3.client.return_value = mock_client
        tool_fn = make_energy_summary_tool(session, config)
        result = tool_fn()

    assert result["available"] is False
    assert result["tracking_row"] == "PySpark via Glue"
    assert "PySpark" in result["reason"]


# ---------------------------------------------------------------------------
# Additional: exception from boto3 still returns graceful response
# ---------------------------------------------------------------------------


def test_vehicle_identity_boto3_exception_graceful():
    """When boto3 raises an exception (not just empty result), tool returns graceful unavailable."""
    from agents.supervisor.tools.vehicle_identity import make_vehicle_identity_tool

    session = _make_session()
    config = _make_config()

    mock_client = MagicMock()
    mock_client.retrieve_memory_records.side_effect = Exception("Connection refused")

    with patch("agents.supervisor.tools.vehicle_identity.boto3") as mock_boto3:
        mock_boto3.client.return_value = mock_client
        tool_fn = make_vehicle_identity_tool(session, config)
        result = tool_fn("1FMCU0J91KUB12345")

    assert result["available"] is False
    assert "reason" in result
