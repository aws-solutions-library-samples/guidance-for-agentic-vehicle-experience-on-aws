"""energy_summary tool — energy usage aggregates via AgentCore Memory.

PySpark-gated: the upstream data source (`energy_usage`) is produced by the
`PySpark via Glue` spec (P2 IDLE Architect). Until that spec ships, Memory
will always return zero records. The tool returns a structured gating
message — never raises.

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


def make_energy_summary_tool(session: Any, config: Any) -> Any:
    """Factory returning a @tool for energy usage aggregates (PySpark-gated).

    Args:
        session: SessionContext with persona_id, vin, tenant_id.
        config: PersonaLoaderConfig with memory_id, memory_region.
    """
    memory_region = getattr(config, "memory_region", "us-west-2")

    @tool
    def get_energy_summary() -> dict[str, Any]:
        """Retrieve aggregated energy usage summary for the vehicle or customer.

        Returns consumption metrics, efficiency trends, and cost estimates.
        Note: availability depends on the PySpark via Glue data pipeline.
        """
        if not getattr(config, "tool_memory_reads", True):
            return _PYSPARK_GATING_RESPONSE

        namespace = f"cvx/{session.tenant_id}/{session.persona_id}"
        vin = getattr(session, "vin", None)
        query = f"energy usage for VIN {vin}" if vin else "energy usage summary"

        try:
            client = boto3.client("bedrock-agentcore", region_name=memory_region)
            filters = [
                {
                    "left": {"metadataKey": "data_source"},
                    "operator": "EQUALS_TO",
                    "right": {"metadataValue": {"stringValue": "energy_usage"}},
                },
            ]
            if vin:
                filters.append({
                    "left": {"metadataKey": "vehicle_id"},
                    "operator": "EQUALS_TO",
                    "right": {"metadataValue": {"stringValue": vin}},
                })
            resp = client.retrieve_memory_records(
                memoryId=config.memory_id,
                namespace=namespace,
                searchCriteria={
                    "searchQuery": query,
                    "metadataFilters": filters,
                    "topK": 1,
                },
            )
        except Exception as exc:
            log.warning("energy_summary Memory read failed: %s", exc)
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

    return get_energy_summary
