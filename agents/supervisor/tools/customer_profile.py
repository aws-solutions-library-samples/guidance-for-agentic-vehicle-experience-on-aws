"""customer_profile tool — customer 360 view via AgentCore Memory.

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


def make_customer_profile_tool(session: Any, config: Any) -> Any:
    """Factory returning a @tool that reads customer 360 data from AgentCore Memory.

    Args:
        session: SessionContext with persona_id, customer_id, tenant_id.
        config: PersonaLoaderConfig with memory_id, memory_region.
    """
    memory_region = getattr(config, "memory_region", "us-west-2")

    @tool
    def get_customer_profile() -> dict[str, Any]:
        """Retrieve the customer 360 profile for the current session customer.

        Returns latest snapshot of customer demographics, preferences,
        vehicle ownership, and lifecycle stage.
        """
        customer_id = getattr(session, "customer_id", None)
        if not customer_id:
            return {"available": False, "reason": "No customer_id in session context"}

        if not getattr(config, "tool_memory_reads", True):
            return {"available": False, "reason": "customer_360 not yet loaded"}

        namespace = f"cvx/{session.tenant_id}/{session.persona_id}"
        try:
            client = boto3.client("bedrock-agentcore", region_name=memory_region)
            resp = client.retrieve_memory_records(
                memoryId=config.memory_id,
                namespace=namespace,
                searchCriteria={
                    "searchQuery": f"customer profile for {customer_id}",
                    "metadataFilters": [
                        {
                            "left": {"metadataKey": "customer_id"},
                            "operator": "EQUALS_TO",
                            "right": {"metadataValue": {"stringValue": customer_id}},
                        },
                        {
                            "left": {"metadataKey": "data_source"},
                            "operator": "EQUALS_TO",
                            "right": {"metadataValue": {"stringValue": "customer_360"}},
                        },
                    ],
                    "topK": 1,
                },
            )
        except Exception as exc:
            log.warning("customer_profile Memory read failed for %s: %s", customer_id, exc)
            return {"available": False, "reason": "customer_360 not yet loaded"}

        records = resp.get("memoryRecords") or []
        if not records:
            return {"available": False, "reason": "customer_360 not yet loaded"}

        payload = records[0].get("content", {})
        if isinstance(payload, dict) and "text" in payload:
            try:
                data = json.loads(payload["text"])
            except (ValueError, TypeError):
                data = payload
        else:
            data = payload

        return {"available": True, **data}

    return get_customer_profile
