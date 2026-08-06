"""charging_history tool — EV charging sessions via AgentCore Memory.

See `.kiro/specs/2026-06-15-cvx-on-foundation-v1/spec.md` § "Tool repointing"
and `docs/tech.md` § 6 for the exact RetrieveMemoryRecords filter shape.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import boto3
from strands import tool

log = logging.getLogger(__name__)


def make_charging_history_tool(session: Any, config: Any) -> Any:
    """Factory returning a @tool that reads charging sessions from AgentCore Memory.

    Args:
        session: SessionContext with persona_id, vin, tenant_id.
        config: PersonaLoaderConfig with memory_id, memory_region.
    """
    memory_region = getattr(config, "memory_region", "us-west-2")

    @tool
    def get_charging_history() -> dict[str, Any]:
        """Retrieve recent EV charging sessions for the vehicle in session.

        Returns session date, energy dispensed (kWh), and charging location.
        """
        if not session.vin:
            return {"available": False, "reason": "No VIN in session context"}

        namespace = f"cvx/{session.tenant_id}/{session.persona_id}"
        try:
            client = boto3.client("bedrock-agentcore", region_name=memory_region)
            resp = client.retrieve_memory_records(
                memoryId=config.memory_id,
                namespace=namespace,
                searchCriteria={
                    "searchQuery": f"charging sessions for VIN {session.vin}",
                    "metadataFilters": [
                        {
                            "left": {"metadataKey": "vehicle_id"},
                            "operator": "EQUALS_TO",
                            "right": {"metadataValue": {"stringValue": session.vin}},
                        },
                        {
                            "left": {"metadataKey": "data_source"},
                            "operator": "EQUALS_TO",
                            "right": {"metadataValue": {"stringValue": "charging_sessions"}},
                        },
                    ],
                    "topK": 20,
                },
            )
        except Exception as exc:
            log.warning("charging_history Memory read failed for %s: %s", session.vin, exc)
            return {"available": False, "reason": "charging_sessions not yet loaded"}

        records = resp.get("memoryRecords") or []
        if not records:
            return {"available": False, "reason": "charging_sessions not yet loaded"}

        results = []
        for rec in records:
            payload = rec.get("content", {})
            if isinstance(payload, dict) and "text" in payload:
                try:
                    row = json.loads(payload["text"])
                except (ValueError, TypeError):
                    row = payload
            else:
                row = payload
            # Return subset: session_date, kwh, location per spec
            results.append({
                k: row[k]
                for k in ("session_date", "kwh", "location", "charge_duration_min")
                if k in row
            } or row)

        return {"available": True, "sessions": results, "count": len(results)}

    return get_charging_history
