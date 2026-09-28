"""retrieve tool — cross-account Bedrock KB Retrieve (CVX-on-foundation v1).

Replaces the local-markdown stub with bedrock-agent-runtime:Retrieve
against the ADP-owned knowledge base (adp-staging-vehicle-knowledge).

Fallback: on ClientError or empty result-set, falls back to local corpora/
fixtures for personas that don't strictly need cross-account KB
(Driver/Consumer/Retail/Vehicle Owner). Service Advisor + Technician
return {"available": false, "reason": "KB unavailable"}.

CloudWatch metric: cvx.retrieve.kb_unavailable emitted on fallback path.

See spec: .kiro/specs/2026-06-15-cvx-on-foundation-v1/spec.md § "Tool repointing"
and docs/tech.md § 3 for verified Retrieve request/response shape.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

import boto3
from botocore.config import Config as BotocoreConfig
from botocore.exceptions import ClientError
from strands import tool

from agents.supervisor.config import Config
from agents.supervisor.session import SessionContext
from agents.supervisor.tools.kb_filter import apply_source_category_filter

log = logging.getLogger(__name__)

# Personas that fall back to local corpora on KB failure (vs hard "unavailable").
_LOCAL_FALLBACK_PERSONAS = frozenset(
    {"fleet_driver", "consumer", "retail_advisor", "vehicle_owner"}
)

_REPO_ROOT = Path(__file__).resolve().parents[3]
_CORPORA_DIR = _REPO_ROOT / "corpora"

_BOTOCORE_CFG = BotocoreConfig(read_timeout=8)

_WORD_RE = re.compile(r"[a-zA-Z0-9]+")


def _local_fallback(query: str) -> list[dict[str, Any]]:
    """Simple keyword search over local corpora/ markdown files."""
    if not _CORPORA_DIR.exists():
        return []
    query_lower = query.lower()
    scored: list[tuple[int, str, str]] = []
    for md_file in _CORPORA_DIR.rglob("*.md"):
        content = md_file.read_text(errors="ignore")
        score = sum(1 for t in query_lower.split() if t in content.lower())
        if score > 0:
            idx = content.lower().find(query_lower.split()[0])
            excerpt = content[max(0, idx - 50): idx + 500].strip() if idx >= 0 else content[:500].strip()
            scored.append((score, excerpt, str(md_file.relative_to(_CORPORA_DIR))))
    scored.sort(key=lambda t: t[0], reverse=True)
    return [
        {"snippet": exc, "source": src, "score": sc}
        for sc, exc, src in scored[:5]
    ]


def _build_filter(source_category: str | list[str] | None) -> dict | None:
    """Build a KB retrieval filter for source_category."""
    if not source_category:
        return None
    if isinstance(source_category, list):
        if len(source_category) == 1:
            return {"equals": {"key": "source_category", "value": source_category[0]}}
        return {"in": {"key": "source_category", "value": source_category}}
    return {"equals": {"key": "source_category", "value": source_category}}


def _emit_kb_unavailable_metric() -> None:
    """Emit CloudWatch metric cvx.retrieve.kb_unavailable (best-effort)."""
    try:
        cw = boto3.client("cloudwatch")
        cw.put_metric_data(
            Namespace="cvx",
            MetricData=[{"MetricName": "retrieve.kb_unavailable", "Value": 1, "Unit": "Count"}],
        )
    except Exception:  # noqa: BLE001
        pass  # metric emission is best-effort; never raise


def make_retrieve_tool(ctx: SessionContext, config: Config):
    """Build the retrieve tool bound to session context.

    Calls bedrock-agent-runtime:Retrieve against config.adp_kb_id.
    Falls back to local corpora for non-reference personas.
    """
    adp_kb_id: str = getattr(config, "adp_kb_id", "") or ""
    adp_region: str = getattr(config, "adp_region", None) or "us-east-1"
    persona_id: str = getattr(ctx, "persona_id", "") or ""

    def _record_citations(hits: list[dict[str, Any]]) -> None:
        """Side-effect: stash the most recent retrieve hits on the session context.

        Replaces (last-call-wins) ``ctx.last_kb_citations`` with one entry per
        hit that has a ``source`` field. Used by ``agentcore_app.py`` after
        ``agent(prompt)`` returns to surface citations in the runtime response.
        Skips ``{"available": false}`` reference-persona-fallback markers.
        """
        recorded: list[dict] = []
        for h in hits:
            if not isinstance(h, dict):
                continue
            src = h.get("source")
            if not src:
                continue
            score_raw = h.get("score")
            score: float | None
            if isinstance(score_raw, bool):
                score = None  # bool subclasses int — reject to match tier3 _coerce_score
            elif isinstance(score_raw, (int, float)):
                score = float(score_raw)
            else:
                score = None
            recorded.append({"source": src, "score": score})
        # Always overwrite — even with [] — so a successful empty retrieve
        # doesn't leave stale citations from a prior turn on the response.
        try:
            ctx.last_kb_citations = recorded
        except Exception:  # noqa: BLE001
            # ctx may be a SimpleNamespace in tests without the field declared;
            # mutation is best-effort, never raise from a tool side-effect.
            try:
                setattr(ctx, "last_kb_citations", recorded)
            except Exception:  # noqa: BLE001
                pass

    @tool
    def retrieve(query: str, source_category: str | None = None) -> list[dict[str, Any]]:
        """Retrieve relevant documents from the ADP vehicle knowledge base.

        ALWAYS call this before citing a recall ID, a warranty term, a part
        number, TSB reference, or a policy rule. Do not invent those values.

        Args:
            query: Natural-language query (e.g. "DTC P0420 catalyst efficiency").
            source_category: Optional KB source category filter. One of:
                tsb_recall, owner_manual, service_policy, charging_narrative,
                dtc_guide, parts_catalog. Pass a comma-separated string for
                multiple categories (e.g. "tsb_recall,service_policy").
        """
        # Parse comma-separated string into list if needed.
        cats: str | list[str] | None = source_category
        if isinstance(source_category, str) and "," in source_category:
            cats = [s.strip() for s in source_category.split(",") if s.strip()]

        # Short-circuit: no KB configured → fall back immediately.
        if not adp_kb_id:
            log.warning("retrieve: adp_kb_id not configured — falling back to local corpora")
            _emit_kb_unavailable_metric()
            hits = _graceful_fallback(query, persona_id)
            _record_citations(hits)
            return hits

        retrieval_cfg: dict = {"vectorSearchConfiguration": {"numberOfResults": 5}}
        # source_category filter gated OFF until the ADP KB carries that metadata
        # (interim — see tools/kb_filter.py and
        # issues/2026-06-18-cvx-kb-source-category-metadata-mismatch/). Without
        # the gate, an agent-supplied source_category matches zero chunks on the
        # un-tagged KB and silently forces local-corpora fallback
        # (issues/2026-06-19-cvx-staging-kb-grounding-local-fallback/). Mirrors
        # parts_lookup.py / diy_repair_advisor.py.
        kb_filter = _build_filter(cats)
        if kb_filter:
            apply_source_category_filter(
                retrieval_cfg["vectorSearchConfiguration"], kb_filter
            )

        try:
            client = boto3.client(
                "bedrock-agent-runtime",
                region_name=adp_region,
                config=_BOTOCORE_CFG,
            )
            resp = client.retrieve(
                knowledgeBaseId=adp_kb_id,
                retrievalQuery={"text": query},
                retrievalConfiguration=retrieval_cfg,
            )
        except (ClientError, Exception) as exc:  # noqa: BLE001
            log.warning("retrieve: KB Retrieve failed (%s) — falling back", exc)
            _emit_kb_unavailable_metric()
            hits = _graceful_fallback(query, persona_id)
            _record_citations(hits)
            return hits

        results = resp.get("retrievalResults") or []
        if not results:
            log.info("retrieve: empty result-set from KB — falling back")
            _emit_kb_unavailable_metric()
            hits = _graceful_fallback(query, persona_id)
            _record_citations(hits)
            return hits

        hits = []
        for r in results:
            hits.append({
                "snippet": r.get("content", {}).get("text", ""),
                "source": r.get("location", {}).get("s3Location", {}).get("uri", "kb"),
                "score": r.get("score", 0),
            })
        _record_citations(hits)
        return hits

    return retrieve


def _graceful_fallback(query: str, persona_id: str) -> list[dict[str, Any]]:
    """Return local-corpus results for non-reference personas; unavailable for reference personas."""
    if persona_id in _LOCAL_FALLBACK_PERSONAS or not persona_id:
        return _local_fallback(query)
    return [{"available": False, "reason": "KB unavailable"}]
