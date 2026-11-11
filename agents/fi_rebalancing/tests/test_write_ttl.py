"""
Tests for TTL attribute on rebalancing findings written via write.py (A1 retention spec).

Spec source:
    .kiro/specs/2026-10-27-avx-findings-retention/spec.md
    § Design B, A1 Interface (rebalancing's own writes)

Rules:
- Gap findings (agent-fi-rebalancing, no recommended_action) → ttl set when written.
- Move findings (have recommended_action) → no ttl.

RED tests (fail before implementation):
  TestGapFindingTTL.test_gap_finding_ddb_item_has_ttl
  TestGapFindingTTL.test_gap_finding_ttl_is_ddb_number
GREEN before implementation (mutation guards):
  TestMoveFindingNoTTL.test_move_finding_ddb_item_has_no_ttl
"""
from __future__ import annotations

from datetime import datetime

import boto3
import pytest
from boto3.dynamodb.types import TypeDeserializer
from botocore.stub import ANY, Stubber

from agents.tier2.writer import _to_ddb_item, write_finding
from agents.fi_rebalancing.write import build_gap_finding, build_move_finding

# ---------------------------------------------------------------------------
# Constants (kept separate from test_write.py to avoid import coupling)
# ---------------------------------------------------------------------------

TABLE = "vsa-test-avx-findings-ttl"
_des = TypeDeserializer()
GRACE = 604_800

_FLEET_ID = "fleet-ttl-test"
_AGENT_VERSION = "1.0.0"
_COMPUTED_AT = "2026-10-27T06:00:00Z"

_VIN_A = "MRDVEH00000000031"
_VIN_B = "MRDVEH00000000032"
_VIN_C = "MRDVEH00000000033"
_LOC_FROM = "loc-ttl-from-01"
_LOC_TO   = "loc-ttl-to-01"
_ISO_WEEK = "2026-W44"
_FIRST_DAY = "2026-10-28"

_FROM_LOCATION = {
    "locationId": _LOC_FROM, "name": "TTL From", "locationType": "downtown",
    "metro": "seattle", "latitude": 47.60, "longitude": -122.33,
    "parkingSpaces": 20, "operatingHours": "08-20", "op_hours": 12.0,
}
_TO_LOCATION = {
    "locationId": _LOC_TO, "name": "TTL To", "locationType": "airport",
    "metro": "seattle", "latitude": 47.45, "longitude": -122.30,
    "parkingSpaces": 30, "operatingHours": "06-22", "op_hours": 16.0,
}
_PLAN = {
    "candidate_id": f"cand-{_LOC_FROM}-{_LOC_TO}",
    "from_location_id": _LOC_FROM, "to_location_id": _LOC_TO,
    "vehicle_vins": [_VIN_A, _VIN_B],
    "timing": "today", "detail": "TTL test move.",
    "diagnosis_source": "rules",
    "transfer_cost_usd": 84.00, "net_benefit_usd": 308.00,
    "predicted_on_rent_hours_gain": 56.0, "predicted_turnaways_avoided": 4,
    "revenue_gain_usd": 392.00, "source_loss_usd": 0.00, "revenue_impact_usd": 392.00,
}
_CANDIDATE = {
    "candidate_id": f"cand-{_LOC_FROM}-{_LOC_TO}",
    "from_location_id": _LOC_FROM, "to_location_id": _LOC_TO,
    "vehicles_short": 3, "eligible_vins": [_VIN_A, _VIN_B, _VIN_C],
    "eligible_facts": {
        v: {"days_idle_last_7": 3, "days_to_next_service": float("inf"),
            "hours_to_next_booking": float("inf"), "open_lower_severity_items": ["routine"]}
        for v in (_VIN_A, _VIN_B, _VIN_C)
    },
    "vehicles_spare": 5, "parking_room": 10,
    "distance_miles": 12.5, "transfer_cost_per_vehicle": 42.00,
    "per_day": [{"date": "2026-10-28", "forecast_hours": 48.0,
                 "supply_vehicles": 4, "op_hours": 16.0}],
    "effective_date": "2026-10-28", "first_deficit_day": _FIRST_DAY,
    "forecast_week": _ISO_WEEK, "severity": "soon", "confidence": "medium",
    "max_utilization": 0.95, "rate": 3.50, "source_rate": 3.00,
    "mean_request_hours": 8.0, "net_benefit_usd": 308.00,
    "transfer_cost_usd": 84.00, "revenue_gain_usd": 392.00,
    "source_loss_usd": 0.00, "revenue_impact_usd": 392.00,
    "predicted_on_rent_hours_gain": 56.0, "predicted_turnaways_avoided": 4,
    "metro": "seattle",
}
_GAP_ITEM = {
    "location_id": _LOC_TO, "gap_kind": "deficit",
    "vehicles_short": 3, "forecast_week": _ISO_WEEK,
    "first_deficit_day": _FIRST_DAY, "severity": "soon", "confidence": "medium",
    "diagnosis_source": "rules",
    "per_day": [{"date": "2026-10-28", "forecast_hours": 48.0,
                 "supply_vehicles": 0, "op_hours": 16.0}],
}


def _expected_ttl(expires_at: str) -> int:
    dt = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
    return int(dt.timestamp()) + GRACE


def _make_gap_evidence():
    from agents.fi_rebalancing.evidence import gap_evidence
    return gap_evidence(_GAP_ITEM, computed_at=_COMPUTED_AT,
                        location=_TO_LOCATION, assigned_vins=[_VIN_C])


def _make_move_evidence():
    from agents.fi_rebalancing.evidence import move_evidence
    return move_evidence(_PLAN, _CANDIDATE, computed_at=_COMPUTED_AT,
                         from_location=_FROM_LOCATION, to_location=_TO_LOCATION,
                         outcomes=[], events=[])


def _write_finding_capture_item(finding: dict) -> dict:
    """Write finding via write_finding on the new-row path; return DDB Item dict."""
    client = boto3.client("dynamodb", region_name="us-west-2")
    stubber = Stubber(client)
    captured: list[dict] = []
    orig = client.put_item

    def _intercept(**kw):
        captured.append(kw["Item"])
        return orig(**kw)

    client.put_item = _intercept  # type: ignore[method-assign]

    with stubber:
        # No live row → PutItem path
        stubber.add_response(
            "query", {"Items": [], "Count": 0},
            expected_params={
                "TableName": TABLE,
                "IndexName": "by_finding_key",
                "KeyConditionExpression": "finding_key = :fk",
                "FilterExpression": "expires_at > :now",
                "ExpressionAttributeValues": ANY,
            },
        )
        stubber.add_response(
            "put_item", {},
            expected_params={"TableName": TABLE, "Item": ANY},
        )
        write_finding(
            finding,
            table_name=TABLE,
            dynamodb_client=client,
            utc_now_fn=lambda: "2026-10-27T10:01:00Z",
        )
    assert captured, "put_item was not called"
    return captured[0]


# ---------------------------------------------------------------------------
# Gap findings (informational, agent-fi-rebalancing, no recommended_action)
# ---------------------------------------------------------------------------


class TestGapFindingTTL:
    """Gap findings must get ttl when written via writer.py's _put_item."""

    def test_gap_finding_ddb_item_has_ttl(self):
        """build_gap_finding + write_finding → DDB item carries ttl.

        RED before implementation: writer.py's _put_item does not set ttl yet.
        Mutation guard: if the agent-fi-rebalancing exclusion is accidentally
        added, this fails.
        """
        finding = build_gap_finding(
            _GAP_ITEM, fleet_id=_FLEET_ID, location=_TO_LOCATION,
            evidence=_make_gap_evidence(), computed_at=_COMPUTED_AT,
            agent_version=_AGENT_VERSION,
        )
        item = _write_finding_capture_item(finding)
        assert "ttl" in item, (
            f"A1 rebalancing: gap finding DDB item must carry ttl; "
            f"got keys {sorted(item.keys())}"
        )
        ttl_val = _des.deserialize(item["ttl"])
        expected = _expected_ttl(finding["expires_at"])
        assert ttl_val == expected, (
            f"A1 rebalancing: gap ttl must equal epoch(expires_at) + 604800; "
            f"expected {expected}, got {ttl_val!r}"
        )

    def test_gap_finding_ttl_is_ddb_number(self):
        """ttl attribute must be a DynamoDB Number type (not String)."""
        finding = build_gap_finding(
            _GAP_ITEM, fleet_id=_FLEET_ID, location=_TO_LOCATION,
            evidence=_make_gap_evidence(), computed_at=_COMPUTED_AT,
            agent_version=_AGENT_VERSION,
        )
        item = _write_finding_capture_item(finding)
        assert "ttl" in item
        assert "N" in item["ttl"], (
            f"A1 rebalancing: ttl must be DDB Number; got {item['ttl']!r}"
        )

    def test_gap_finding_dict_does_not_carry_ttl(self):
        """build_gap_finding return dict must NOT contain ttl (schema boundary).

        ttl is storage-only: it must not appear in the Finding schema dict
        (additionalProperties: false would reject it).
        """
        finding = build_gap_finding(
            _GAP_ITEM, fleet_id=_FLEET_ID, location=_TO_LOCATION,
            evidence=_make_gap_evidence(), computed_at=_COMPUTED_AT,
            agent_version=_AGENT_VERSION,
        )
        assert "ttl" not in finding, (
            f"A1 schema boundary: gap finding dict must NOT carry ttl; "
            f"ttl is storage-only (additionalProperties: false rejects it). "
            f"Got keys: {sorted(finding.keys())}"
        )


# ---------------------------------------------------------------------------
# Move findings (FI items — have recommended_action)
# ---------------------------------------------------------------------------


class TestMoveFindingNoTTL:
    """Move findings must NOT get ttl (they carry recommended_action)."""

    def test_move_finding_ddb_item_has_no_ttl(self):
        """build_move_finding + write_finding → DDB item has no ttl.

        Mutation guard: if the recommended_action check is dropped from
        _compute_ttl, this fails.
        """
        finding = build_move_finding(
            _PLAN, _CANDIDATE,
            fleet_id=_FLEET_ID,
            from_location=_FROM_LOCATION, to_location=_TO_LOCATION,
            evidence=_make_move_evidence(),
            computed_at=_COMPUTED_AT, agent_version=_AGENT_VERSION,
        )
        assert "recommended_action" in finding, "Test setup: move finding must have recommended_action"
        item = _write_finding_capture_item(finding)
        assert "ttl" not in item, (
            f"A1 rebalancing: move finding (FI item) must NOT carry ttl; "
            f"got keys {sorted(item.keys())}"
        )
