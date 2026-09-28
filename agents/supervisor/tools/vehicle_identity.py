"""vehicle_identity tool — VIN decode lookup via AgentCore Memory.

Replaces the prior DDB scan path. Reads from AgentCore Memory using
RetrieveMemoryRecords with metadata filters.

Tool name `lookup_vehicle_identity` is preserved for prompt compatibility.

See `.kiro/specs/2026-06-15-cvx-on-foundation-v1/spec.md` § "Tool repointing"
and `docs/tech.md` § 6 for the exact RetrieveMemoryRecords filter shape.
"""

from __future__ import annotations

import logging
from typing import Any

import boto3
from strands import tool

log = logging.getLogger(__name__)


def make_vehicle_identity_tool(session: Any, config: Any) -> Any:
    """Factory returning a @tool that reads vehicle identity from AgentCore Memory.

    Args:
        session: SessionContext with persona_id, vin, tenant_id.
        config: PersonaLoaderConfig with memory_id, adp_region (used as memory_region = us-west-2).
    """
    memory_region = getattr(config, "memory_region", "us-west-2")

    @tool
    def lookup_vehicle_identity(vin: str) -> dict[str, Any]:
        """Decode a VIN to get vehicle make, model, year, trim, and engine. It returns no recall data.

        Use this when:
        - You need to know what vehicle the driver has (make/model/year)
        - You need the make, model, and year to match recalls from `retrieve`
        - You need engine/transmission details for parts lookup

        Args:
            vin: The 17-character Vehicle Identification Number.
        """
        namespace = f"cvx/{session.tenant_id}/{session.persona_id}"
        if not getattr(config, "tool_memory_reads", True):
            return {"available": False, "reason": "vehicle_identity not yet loaded"}

        try:
            client = boto3.client("bedrock-agentcore", region_name=memory_region)
            resp = client.retrieve_memory_records(
                memoryId=config.memory_id,
                namespace=namespace,
                searchCriteria={
                    "searchQuery": f"vehicle identity for VIN {vin}",
                    "metadataFilters": [
                        {
                            "left": {"metadataKey": "persona_id"},
                            "operator": "EQUALS_TO",
                            "right": {"metadataValue": {"stringValue": session.persona_id}},
                        },
                        {
                            "left": {"metadataKey": "vehicle_id"},
                            "operator": "EQUALS_TO",
                            "right": {"metadataValue": {"stringValue": vin}},
                        },
                        {
                            "left": {"metadataKey": "data_source"},
                            "operator": "EQUALS_TO",
                            "right": {"metadataValue": {"stringValue": "vehicle_identity"}},
                        },
                    ],
                    "topK": 1,
                },
            )
        except Exception as exc:
            log.warning("vehicle_identity Memory read failed for %s: %s", vin, exc)
            return {"available": False, "reason": "vehicle_identity not yet loaded"}

        records = resp.get("memoryRecords") or []
        if not records:
            return {"available": False, "reason": "vehicle_identity not yet loaded"}

        payload = records[0].get("content", {})
        if isinstance(payload, dict) and "text" in payload:
            import json
            try:
                data = json.loads(payload["text"])
            except (ValueError, TypeError):
                data = payload
        else:
            data = payload

        data.setdefault("found", True)
        return data

    return lookup_vehicle_identity
