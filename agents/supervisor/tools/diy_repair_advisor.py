"""DIY repair advisor — augmented with Bedrock KB dtc_guide snippets (CVX-on-foundation v1).

KEEPS the existing rule-based sub-agent corpus path (owner-manuals).
AUGMENTS: after rule-based lookup, calls bedrock-agent-runtime:Retrieve
with source_category=dtc_guide to append KB-derived DTC snippets to the
response. On KB Retrieve failure, falls back silently to rule-based only.

See spec: .kiro/specs/2026-06-15-cvx-on-foundation-v1/spec.md § "Tool repointing"
"""

from __future__ import annotations

import logging
from typing import Any

import boto3
from botocore.config import Config as BotocoreConfig
from botocore.exceptions import ClientError
from strands import tool

from agents.supervisor.session import SessionContext
from agents.supervisor.tools.kb_filter import apply_source_category_filter
from agents.supervisor.tools.sub_agent import (
    SubAgentConfig,
    make_sub_agent_tool,
)

log = logging.getLogger(__name__)

_BOTOCORE_CFG = BotocoreConfig(read_timeout=8)

_TIRE_TRIGGERS = (
    "tire", "tires", "tyre", "tyres", "psi", "pressure",
    "inflate", "deflate", "tpms", "flat",
)


def _tire_pressure_chat_message(
    *,
    query: str,
    dtc_code: str = "",
    vehicle_phrase: str = "",
    driver_first: str = "",
    **_kwargs,
) -> str | None:
    """Return a markdown chat message with F-150 tire pressures, or None."""
    q = (query or "").lower()
    if not any(t in q for t in _TIRE_TRIGGERS):
        return None
    title_vehicle = vehicle_phrase or "Ford F-150"
    return (
        f"**🛞 {title_vehicle} — Cold Tire Pressures**\n"
        "\n"
        "• Front: **35 PSI**\n"
        "• Rear: **35 PSI**\n"
        "• Spare (compact): 60 PSI\n"
        "\n"
        "_Always verify against the sticker on your driver's-side door jamb. "
        "Add 5 PSI rear when towing or hauling heavy loads._"
    )


def _kb_dtc_snippets(query: str, adp_kb_id: str, adp_region: str) -> list[dict[str, Any]]:
    """Retrieve DTC-guide snippets from KB; return empty list on any failure."""
    if not adp_kb_id:
        return []
    try:
        client = boto3.client(
            "bedrock-agent-runtime",
            region_name=adp_region,
            config=_BOTOCORE_CFG,
        )
        resp = client.retrieve(
            knowledgeBaseId=adp_kb_id,
            retrievalQuery={"text": query},
            retrievalConfiguration={
                "vectorSearchConfiguration": apply_source_category_filter(
                    {"numberOfResults": 3},
                    {"equals": {"key": "source_category", "value": "dtc_guide"}},
                )
            },
        )
        results = resp.get("retrievalResults") or []
        return [
            {
                "text": r.get("content", {}).get("text", ""),
                "source": r.get("location", {}).get("s3Location", {}).get("uri", "kb"),
                "score": r.get("score", 0),
            }
            for r in results
        ]
    except (ClientError, Exception) as exc:  # noqa: BLE001
        log.info("diy_repair_advisor: KB dtc_guide Retrieve failed (%s) — using rule-based only", exc)
        return []


def make_diy_repair_advisor_tool(ctx: SessionContext | None = None, config: Any = None):
    """Build the diy_repair_advisor sub-agent tool, augmented with KB dtc_guide snippets."""
    adp_kb_id: str = getattr(config, "adp_kb_id", "") or ""
    adp_region: str = getattr(config, "adp_region", None) or "us-east-1"

    # Build the underlying rule-based sub-agent tool.
    sub_agent_cfg = SubAgentConfig(
        name="diy_repair_advisor",
        description=(
            "Use this when the driver asks if they can fix something "
            "themselves, wants step-by-step repair guidance, or asks "
            "how to handle a vehicle warning that might be a quick fix "
            "(e.g. 'Can I top this off myself?', 'Is this something "
            "I can handle?', 'Walk me through it'). Returns owner-manual "
            "reference material — you should summarize the relevant "
            "section in 1-2 short sentences for the driver."
        ),
        corpus_subdir="owner-manuals",
        chat_message_fn=_tire_pressure_chat_message,
    )
    _rule_based_tool_fn = make_sub_agent_tool(sub_agent_cfg, ctx=ctx)

    @tool(name="diy_repair_advisor")
    def diy_repair_advisor(query: str, dtc_code: str = "") -> dict[str, Any]:
        """Advise the driver on DIY repair and DTC interpretation.

        Returns owner-manual reference material plus any relevant technical
        service bulletins from the vehicle knowledge base.

        Args:
            query: The driver's repair question or vehicle symptom description.
            dtc_code: Optional DTC fault code (e.g. "P0420").
        """
        # Step 1: rule-based corpus lookup (always runs).
        base_result: dict[str, Any] = _rule_based_tool_fn(query=query, dtc_code=dtc_code)

        # Step 2: augment with KB dtc_guide snippets (silent on failure).
        kb_query = f"{dtc_code} {query}".strip() if dtc_code else query
        kb_snippets = _kb_dtc_snippets(kb_query, adp_kb_id, adp_region)
        if kb_snippets:
            base_result["kb_dtc_snippets"] = kb_snippets

        return base_result

    return diy_repair_advisor
