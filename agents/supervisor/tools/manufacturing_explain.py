"""manufacturing_explain tool — KB-grounded explanation of a pipeline stage.

Queries the ADP Bedrock Knowledge Base (``config.adp_kb_id``) filtered to the
``config.acquire_kb_source_category`` source category (default
``"vehicle_manufacturing"``) and returns the most relevant passages explaining
what happens during the requested manufacturing stage.

Pattern mirrors ``knowledge.py`` exactly: KB-grounded with a
``source_category`` filter applied through the ``apply_source_category_filter``
gate in ``kb_filter.py``. When ``adp_kb_id`` is empty the tool returns
``{available: false}`` so callers can degrade gracefully.

Stage query text is constructed deterministically from the stage identifier so
the same stage always maps to the same retrieval query.
"""

from __future__ import annotations

import logging
from typing import Any, Literal

import boto3
from botocore.config import Config as BotocoreConfig
from botocore.exceptions import ClientError
from strands import tool

from agents.supervisor.config import Config
from agents.supervisor.session import SessionContext
from agents.supervisor.tools.kb_filter import apply_source_category_filter

log = logging.getLogger(__name__)

_BOTOCORE_CFG = BotocoreConfig(read_timeout=8)

# Maps stable stage identifiers to human-readable retrieval queries.
# These are generic manufacturing concepts — no OEM-specific language.
_STAGE_QUERIES: dict[str, str] = {
    "orderPlaced": "what happens when a vehicle order is placed and confirmed",
    "paymentReceived": "what happens after payment is received for a vehicle order",
    "assignedToPlant": "how is a vehicle order assigned to a manufacturing plant",
    "subAssembly": "what happens during sub-assembly stage of vehicle manufacturing",
    "paint": "what happens during the paint stage of vehicle manufacturing",
    "finalAssembly": "what happens during final assembly of a vehicle",
    "qualityControl": "what quality control checks happen before a vehicle is shipped",
    "shipped": "what happens when a vehicle is shipped from the factory",
    "atDealer": "what happens when a vehicle arrives at the dealer for pre-delivery inspection",
    "readyForDelivery": "what steps happen before a vehicle is ready for customer delivery",
}

_DEFAULT_STAGE_QUERY = "explain this vehicle manufacturing stage to a customer"


def make_manufacturing_explain_tool(ctx: SessionContext, config: Config):
    """Build the manufacturing_explain tool bound to a session context."""

    adp_kb_id: str = getattr(config, "adp_kb_id", "") or ""
    adp_region: str = getattr(config, "adp_region", None) or "us-east-1"
    source_category: str = getattr(config, "acquire_kb_source_category", "vehicle_manufacturing") or "vehicle_manufacturing"

    @tool
    def manufacturing_explain(
        stage: Literal[
            "orderPlaced",
            "paymentReceived",
            "assignedToPlant",
            "subAssembly",
            "paint",
            "finalAssembly",
            "qualityControl",
            "shipped",
            "atDealer",
            "readyForDelivery",
        ],
    ) -> dict[str, Any]:
        """Explain what happens during a specific vehicle manufacturing or delivery stage.

        Use this tool when a customer asks what a particular pipeline stage
        means, why it takes a certain amount of time, or what is being done
        to their vehicle at that stage.

        Args:
            stage: The pipeline stage identifier. One of: orderPlaced,
                paymentReceived, assignedToPlant, subAssembly, paint,
                finalAssembly, qualityControl, shipped, atDealer,
                readyForDelivery.

        Returns:
            Dict with ``stage``, ``passages`` (list of grounded knowledge
            snippets with text/source/score), and ``answer`` (summary from
            top passage). Returns ``{available: false}`` when the knowledge
            base is not configured.
        """
        if not adp_kb_id:
            log.warning("manufacturing_explain: adp_kb_id not configured")
            return {"available": False, "reason": "KB not configured"}

        query = _STAGE_QUERIES.get(stage, _DEFAULT_STAGE_QUERY)
        kb_filter = {
            "equals": {
                "key": "source_category",
                "value": source_category,
            }
        }

        try:
            client = boto3.client(
                "bedrock-agent-runtime",
                region_name=adp_region,
                config=_BOTOCORE_CFG,
            )
            vsc: dict = {"numberOfResults": 3}
            apply_source_category_filter(vsc, kb_filter)
            resp = client.retrieve(
                knowledgeBaseId=adp_kb_id,
                retrievalQuery={"text": query},
                retrievalConfiguration={"vectorSearchConfiguration": vsc},
            )
        except (ClientError, Exception) as exc:  # noqa: BLE001
            log.warning("manufacturing_explain: KB Retrieve failed (%s)", exc)
            return {
                "stage": stage,
                "passages": [],
                "answer": f"Stage explanation currently unavailable. ({exc})",
            }

        results = resp.get("retrievalResults") or []
        passages = [
            {
                "text": r.get("content", {}).get("text", ""),
                "source": r.get("location", {}).get("s3Location", {}).get("uri", "kb"),
                "score": r.get("score", 0),
            }
            for r in results
        ]

        answer = passages[0]["text"][:500] if passages else (
            f"No specific information found for the {stage} stage."
        )

        return {
            "stage": stage,
            "passages": passages,
            "answer": answer,
        }

    return manufacturing_explain
