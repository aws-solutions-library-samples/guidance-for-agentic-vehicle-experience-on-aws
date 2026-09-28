"""service_history tool — vehicle service records via AgentCore Memory.

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

_MAX_RECORDS = 12


def make_service_history_tool(session: Any, config: Any) -> Any:
    """Factory returning a @tool that reads service records from AgentCore Memory.

    Args:
        session: SessionContext with persona_id, vin, tenant_id.
        config: PersonaLoaderConfig with memory_id, memory_region.
    """
    memory_region = getattr(config, "memory_region", "us-west-2")

    @tool
    def get_service_history() -> dict[str, Any]:
        """Retrieve the most recent service records for the vehicle in session.

        Returns up to 12 service records including date, mileage,
        work performed, and service center.
        """
        if not session.vin:
            return {"available": False, "reason": "No VIN in session context"}

        if not getattr(config, "tool_memory_reads", True):
            return {"available": False, "reason": "service_records not yet loaded"}

        namespace = f"cvx/{session.tenant_id}/{session.persona_id}"
        try:
            client = boto3.client("bedrock-agentcore", region_name=memory_region)
            resp = client.retrieve_memory_records(
                memoryId=config.memory_id,
                namespace=namespace,
                searchCriteria={
                    "searchQuery": f"service history for VIN {session.vin}",
                    "metadataFilters": [
                        {
                            "left": {"metadataKey": "vehicle_id"},
                            "operator": "EQUALS_TO",
                            "right": {"metadataValue": {"stringValue": session.vin}},
                        },
                        {
                            "left": {"metadataKey": "data_source"},
                            "operator": "EQUALS_TO",
                            "right": {"metadataValue": {"stringValue": "service_records"}},
                        },
                    ],
                    "topK": _MAX_RECORDS,
                },
            )
        except Exception as exc:
            log.warning("service_history Memory read failed for %s: %s", session.vin, exc)
            return {"available": False, "reason": "service_records not yet loaded"}

        records = resp.get("memoryRecords") or []
        if not records:
            return {"available": False, "reason": "service_records not yet loaded"}

        results = []
        for rec in records[:_MAX_RECORDS]:
            payload = rec.get("content", {})
            if isinstance(payload, dict) and "text" in payload:
                try:
                    results.append(json.loads(payload["text"]))
                except (ValueError, TypeError):
                    results.append(payload)
            else:
                results.append(payload)

        return {"available": True, "records": results, "count": len(results)}

    return get_service_history
