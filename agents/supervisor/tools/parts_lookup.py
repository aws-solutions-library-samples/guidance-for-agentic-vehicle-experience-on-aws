"""parts_lookup tool — KB-backed parts catalog retrieval (CVX-on-foundation v1).

Replaces the prior DDB scan path with bedrock-agent-runtime:Retrieve
filtered to source_category=parts_catalog.

Return shape preserves part-number, name, supersession.

See spec: .kiro/specs/2026-06-15-cvx-on-foundation-v1/spec.md § "Tool repointing"
and docs/tech.md § 3 for verified Retrieve request/response shape.
"""

from __future__ import annotations

import logging
from typing import Any

import boto3
from botocore.config import Config as BotocoreConfig
from botocore.exceptions import ClientError
from strands import tool

from agents.supervisor.tools.kb_filter import apply_source_category_filter

log = logging.getLogger(__name__)

_BOTOCORE_CFG = BotocoreConfig(read_timeout=8)


def make_parts_lookup_tool(session: Any = None, config: Any = None):
    """Build the parts_lookup tool.

    Args:
        session: SessionContext (unused in this tool; accepted for factory consistency).
        config: Config with adp_kb_id, adp_region.
    """
    adp_kb_id: str = getattr(config, "adp_kb_id", "") or ""
    adp_region: str = getattr(config, "adp_region", None) or "us-east-1"

    @tool
    def lookup_parts(
        category: str,
        vehicle_make: str = "",
        vehicle_model: str = "",
    ) -> dict[str, Any]:
        """Look up parts and pricing for a repair category and vehicle.

        Use this when:
        - You need to quote a repair cost to the driver
        - The driver asks "how much will this cost?"
        - You need to check parts availability before booking service

        Args:
            category: Repair category (e.g. "brakes", "engine", "electrical").
            vehicle_make: Filter to parts compatible with this make.
            vehicle_model: Filter to parts compatible with this model.

        Returns:
            Dict with found: bool, parts: list of matching parts with
            partNumber, name, supersession, and any pricing details from KB.
        """
        if not adp_kb_id:
            log.warning("lookup_parts: adp_kb_id not configured — returning unavailable")
            return {"found": False, "parts": [], "reason": "KB unavailable"}

        # Build query string including optional vehicle filters.
        query_parts = [f"parts catalog {category}"]
        if vehicle_make:
            query_parts.append(vehicle_make)
        if vehicle_model:
            query_parts.append(vehicle_model)
        query = " ".join(query_parts)

        try:
            client = boto3.client(
                "bedrock-agent-runtime",
                region_name=adp_region,
                config=_BOTOCORE_CFG,
            )
            # source_category filter gated OFF until the ADP KB carries that
            # metadata (interim — see tools/kb_filter.py and
            # issues/2026-06-18-cvx-kb-source-category-metadata-mismatch/).
            vsc: dict = {"numberOfResults": 5}
            apply_source_category_filter(
                vsc, {"equals": {"key": "source_category", "value": "parts_catalog"}}
            )
            resp = client.retrieve(
                knowledgeBaseId=adp_kb_id,
                retrievalQuery={"text": query},
                retrievalConfiguration={"vectorSearchConfiguration": vsc},
            )
        except (ClientError, Exception) as exc:  # noqa: BLE001
            log.warning("lookup_parts: KB Retrieve failed (%s)", exc)
            return {"found": False, "parts": [], "reason": "KB unavailable"}

        results = resp.get("retrievalResults") or []
        if not results:
            return {"found": False, "parts": []}

        parts = []
        for r in results:
            text = r.get("content", {}).get("text", "")
            parts.append({
                "partNumber": _extract_field(text, "partNumber") or _extract_field(text, "part_number") or "unknown",
                "name": _extract_field(text, "name") or _extract_field(text, "description") or text[:100],
                "supersession": _extract_field(text, "supersession") or _extract_field(text, "supersedes") or "",
                "source": r.get("location", {}).get("s3Location", {}).get("uri", "kb"),
                "score": r.get("score", 0),
            })
        return {"found": True, "parts": parts}

    return lookup_parts


def _extract_field(text: str, key: str) -> str:
    """Best-effort extraction of a field from KB text chunk."""
    import re
    pattern = rf'"{key}"\s*:\s*"([^"]+)"'
    m = re.search(pattern, text)
    if m:
        return m.group(1)
    # Also try `key: value` plain-text format.
    m2 = re.search(rf"{key}:\s*(.+?)(?:\n|$)", text, re.IGNORECASE)
    return m2.group(1).strip() if m2 else ""
