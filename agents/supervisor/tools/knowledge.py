"""knowledge tool — KB-backed reference retrieval with per-persona filters (CVX-on-foundation v1).

Replaces local-fixture fallback with bedrock-agent-runtime:Retrieve
filtered by source_category derived from PERSONA_KB_FILTERS for the
current persona.

Personas not in PERSONA_KB_FILTERS have no KB grounding and receive an
explicit {"available": false} response.

See spec: .kiro/specs/2026-06-15-cvx-on-foundation-v1/spec.md § "Tool repointing"
and docs/tech.md § 3 for verified Retrieve request/response shape.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import boto3
from botocore.config import Config as BotocoreConfig
from botocore.exceptions import ClientError
from strands import tool

from agents.supervisor.persona_definitions import PERSONA_KB_FILTERS
from agents.supervisor.tools.kb_filter import apply_source_category_filter

log = logging.getLogger(__name__)

_BOTOCORE_CFG = BotocoreConfig(read_timeout=8)
_REPO_ROOT = Path(__file__).resolve().parents[3]
_CORPORA_DIR = _REPO_ROOT / "corpora"


def make_knowledge_tool(session: Any = None, config: Any = None):
    """Build the knowledge retrieval tool.

    Args:
        session: SessionContext with persona_id.
        config: Config with adp_kb_id, adp_region.
    """
    adp_kb_id: str = getattr(config, "adp_kb_id", "") or ""
    adp_region: str = getattr(config, "adp_region", None) or "us-east-1"
    persona_id: str = getattr(session, "persona_id", "") or ""

    @tool
    def knowledge(query: str, max_results: int = 3) -> dict[str, Any]:
        """Search the vehicle technical knowledge base for relevant information.

        Use this tool when:
        - A DTC code needs interpretation (e.g. "what does P0420 mean?")
        - The driver asks about maintenance schedules or service intervals
        - You need to check if a recall applies to a vehicle
        - You need TSB information for a known issue
        - You need warranty coverage or SLA policy details

        Args:
            query: Natural language question or DTC code to look up.
            max_results: Number of relevant passages to return (1-5).

        Returns:
            A dict with found: bool, passages: list of {text, source, score}.
        """
        # Personas without KB grounding return structured unavailable.
        if persona_id not in PERSONA_KB_FILTERS:
            return {
                "available": False,
                "reason": "persona has no KB grounding",
            }

        if not adp_kb_id:
            log.warning("lookup_knowledge: adp_kb_id not configured — falling back to local")
            return _local_fallback(query, max_results)

        cats = PERSONA_KB_FILTERS[persona_id].get("source_category", [])
        kb_filter: dict
        if len(cats) == 1:
            kb_filter = {"equals": {"key": "source_category", "value": cats[0]}}
        else:
            kb_filter = {"in": {"key": "source_category", "value": cats}}

        try:
            client = boto3.client(
                "bedrock-agent-runtime",
                region_name=adp_region,
                config=_BOTOCORE_CFG,
            )
            # source_category filter is gated OFF until the ADP KB carries that
            # metadata (interim mitigation — see tools/kb_filter.py and
            # issues/2026-06-18-cvx-kb-source-category-metadata-mismatch/).
            vsc: dict = {"numberOfResults": max(1, min(max_results, 5))}
            apply_source_category_filter(vsc, kb_filter)
            resp = client.retrieve(
                knowledgeBaseId=adp_kb_id,
                retrievalQuery={"text": query},
                retrievalConfiguration={"vectorSearchConfiguration": vsc},
            )
        except (ClientError, Exception) as exc:  # noqa: BLE001
            log.warning("lookup_knowledge: KB Retrieve failed (%s) — falling back", exc)
            return _local_fallback(query, max_results)

        results = resp.get("retrievalResults") or []
        if not results:
            return _local_fallback(query, max_results)

        passages = [
            {
                "text": r.get("content", {}).get("text", ""),
                "source": r.get("location", {}).get("s3Location", {}).get("uri", "kb"),
                "score": r.get("score", 0),
            }
            for r in results
        ]
        return {"found": True, "passages": passages}

    return knowledge


def _local_fallback(query: str, max_results: int) -> dict[str, Any]:
    """Simple keyword search over local corpora/ markdown files."""
    if not _CORPORA_DIR.exists():
        return {"found": False, "passages": [], "answer": "Knowledge base not configured."}
    query_lower = query.lower()
    scored = []
    for md_file in _CORPORA_DIR.rglob("*.md"):
        content = md_file.read_text(errors="ignore")
        score = sum(1 for t in query_lower.split() if t in content.lower())
        if score > 0:
            idx = content.lower().find(query_lower.split()[0])
            excerpt = content[max(0, idx - 50): idx + 500].strip() if idx >= 0 else content[:500].strip()
            scored.append({"text": excerpt, "source": str(md_file.relative_to(_CORPORA_DIR)), "score": score})
    scored.sort(key=lambda x: x["score"], reverse=True)
    passages = scored[:max_results]
    return {
        "found": bool(passages),
        "passages": passages,
        "answer": passages[0]["text"][:500] if passages else "No relevant documents found.",
    }
