"""interaction_history tool — customer interaction history via AgentCore Memory.

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

_MAX_INTERACTIONS = 50


def make_interaction_history_tool(session: Any, config: Any) -> Any:
    """Factory returning a @tool that reads customer interactions from AgentCore Memory.

    Args:
        session: SessionContext with persona_id, customer_id, tenant_id.
        config: PersonaLoaderConfig with memory_id, memory_region.
    """
    memory_region = getattr(config, "memory_region", "us-west-2")

    @tool
    def get_interaction_history() -> dict[str, Any]:
        """Retrieve the most recent customer interactions for the current session.

        Returns up to 50 interactions including channel, date, resolution, and notes.
        """
        customer_id = getattr(session, "customer_id", None)
        if not customer_id:
            return {"available": False, "reason": "No customer_id in session context"}

        namespace = f"cvx/{session.tenant_id}/{session.persona_id}"
        try:
            client = boto3.client("bedrock-agentcore", region_name=memory_region)
            resp = client.retrieve_memory_records(
                memoryId=config.memory_id,
                namespace=namespace,
                searchCriteria={
                    "searchQuery": f"customer interactions for {customer_id}",
                    "metadataFilters": [
                        {
                            "left": {"metadataKey": "customer_id"},
                            "operator": "EQUALS_TO",
                            "right": {"metadataValue": {"stringValue": customer_id}},
                        },
                        {
                            "left": {"metadataKey": "data_source"},
                            "operator": "EQUALS_TO",
                            "right": {"metadataValue": {"stringValue": "customer_interactions"}},
                        },
                    ],
                    "topK": _MAX_INTERACTIONS,
                },
            )
        except Exception as exc:
            log.warning("interaction_history Memory read failed for %s: %s", customer_id, exc)
            return {"available": False, "reason": "customer_interactions not yet loaded"}

        records = resp.get("memoryRecords") or []
        if not records:
            return {"available": False, "reason": "customer_interactions not yet loaded"}

        results = []
        for rec in records[:_MAX_INTERACTIONS]:
            payload = rec.get("content", {})
            if isinstance(payload, dict) and "text" in payload:
                try:
                    results.append(json.loads(payload["text"]))
                except (ValueError, TypeError):
                    results.append(payload)
            else:
                results.append(payload)

        return {"available": True, "interactions": results, "count": len(results)}

    return get_interaction_history
