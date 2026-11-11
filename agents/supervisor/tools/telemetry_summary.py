"""telemetry_summary tool — aggregated vehicle telemetry via AgentCore Memory.

PySpark-gated: the upstream data source (`vehicle_telemetry_aggregated`) is
produced by the `PySpark via Glue` spec (P2 IDLE Architect). Until that spec
ships, Memory will always return zero records for this data_source. The tool
returns a structured gating message — never raises.

See `.kiro/specs/2026-06-15-cvx-on-foundation-v1/spec.md` § "PySpark gating"
and `docs/tech.md` § 6.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import boto3
from strands import tool

log = logging.getLogger(__name__)

_PYSPARK_GATING_RESPONSE: dict[str, Any] = {
    "available": False,
    "reason": "Pending PySpark via Glue spec ship",
    "tracking_row": "PySpark via Glue",
}


def make_telemetry_summary_tool(session: Any, config: Any) -> Any:
    """Factory returning a @tool for vehicle telemetry aggregates (PySpark-gated).

    Args:
        session: SessionContext with persona_id, vin, tenant_id.
        config: PersonaLoaderConfig with memory_id, memory_region.
    """
    memory_region = getattr(config, "memory_region", "us-west-2")

    @tool
    def get_telemetry_summary() -> dict[str, Any]:
        """Retrieve aggregated vehicle telemetry summary.

        Returns aggregated metrics (speed, fuel, battery, diagnostics).
        Note: availability depends on the PySpark via Glue data pipeline.
        """
        if not session.vin:
            return _PYSPARK_GATING_RESPONSE

        if not getattr(config, "tool_memory_reads", True):
            return _PYSPARK_GATING_RESPONSE

        namespace = f"cvx/{session.tenant_id}/{session.persona_id}"
        try:
            client = boto3.client("bedrock-agentcore", region_name=memory_region)
            resp = client.retrieve_memory_records(
                memoryId=config.memory_id,
                namespace=namespace,
                searchCriteria={
                    "searchQuery": f"telemetry aggregates for VIN {session.vin}",
                    "metadataFilters": [
                        {
                            "left": {"metadataKey": "vehicle_id"},
                            "operator": "EQUALS_TO",
                            "right": {"metadataValue": {"stringValue": session.vin}},
                        },
                        {
                            "left": {"metadataKey": "data_source"},
                            "operator": "EQUALS_TO",
                            "right": {"metadataValue": {"stringValue": "vehicle_telemetry_aggregated"}},
                        },
                    ],
                    "topK": 1,
                },
            )
        except Exception as exc:
            log.warning("telemetry_summary Memory read failed for %s: %s", session.vin, exc)
            return _PYSPARK_GATING_RESPONSE

        records = resp.get("memoryRecords") or []
        if not records:
            return _PYSPARK_GATING_RESPONSE

        payload = records[0].get("content", {})
        if isinstance(payload, dict) and "text" in payload:
            try:
                data = json.loads(payload["text"])
            except (ValueError, TypeError):
                data = payload
        else:
            data = payload

        return {"available": True, **data}

    return get_telemetry_summary
