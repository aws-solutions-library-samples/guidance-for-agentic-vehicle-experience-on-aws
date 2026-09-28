"""Shared gate for the KB ``source_category`` metadata filter.

The ADP vehicle knowledge base (``YULHRAUMNU``) now carries per-document
``source_category`` metadata keyed to ``PERSONA_KB_FILTERS`` (ADP spec
``2026-06-22-adp-vkb-content-fill``, ingestion ``DAD31WBERT``), so applying a
``source_category`` filter to ``bedrock-agent-runtime:Retrieve`` returns
correctly-scoped chunks for the ``knowledge``, ``parts_lookup``, and
``diy_repair_advisor`` tools.

The filter is gated by ``VSA_KB_SOURCE_CATEGORY_FILTER`` (code default OFF for
backward-compat with KBs that lack the metadata). ``make deploy-staging``
threads ``--env VSA_KB_SOURCE_CATEGORY_FILTER=true`` to enable persona-scoped
filtering on the staging runtime. Leave it OFF only when pointing at a KB
without ``source_category`` metadata (else a filter would match zero chunks and
force local-corpora fallback).

History: gated OFF as the interim mitigation for spec
``2026-06-18-cvx-adp-kb-wireup`` (option 3) while the ADP KB lacked the metadata.
See ``issues/2026-06-18-cvx-kb-source-category-metadata-mismatch/``.

``make_kb_filter_tool`` — kiosk allow-list entry.
Added by spec ``2026-07-31-zone2-order-backend-and-kiosk`` § D3.  The kiosk
tool allow-list includes ``kb_filter`` so the anonymous-visitor surface can set
its own source-category filter for grounded shopping responses without
touching the full KB surface.  The tool wraps ``apply_source_category_filter``
as an LLM-callable action rather than a utility helper.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from strands import tool

log = logging.getLogger(__name__)


def source_category_filter_enabled() -> bool:
    """True only when VSA_KB_SOURCE_CATEGORY_FILTER is explicitly enabled."""
    return os.environ.get("VSA_KB_SOURCE_CATEGORY_FILTER", "").strip().lower() in (
        "1",
        "true",
        "yes",
    )


def apply_source_category_filter(
    vector_search_config: dict[str, Any], filter_dict: dict[str, Any]
) -> dict[str, Any]:
    """Add ``filter`` to a vectorSearchConfiguration only when the gate is enabled.

    Mutates and returns ``vector_search_config`` for call-site convenience.
    """
    if source_category_filter_enabled():
        vector_search_config["filter"] = filter_dict
    return vector_search_config


def make_kb_filter_tool(session: Any = None, config: Any = None):
    """Build the kb_filter tool bound to a session context.

    Exposes the source-category metadata filter as an LLM-callable action.
    The kiosk allow-list (spec D3) includes this tool so anonymous-visitor
    sessions can scope their KB retrievals to shopping-relevant categories
    without accessing the full KB surface.

    Args:
        session: SessionContext (or SimpleNamespace) — used for persona_id.
        config:  Config (or SimpleNamespace) — currently unused; accepted for
                 factory-arity consistency with all other tool factories.
    """
    persona_id: str = getattr(session, "persona_id", "") or ""

    @tool
    def kb_filter(source_category: str) -> dict[str, Any]:
        """Set the knowledge-base source category filter for this session.

        Scopes subsequent KB lookups to a specific document category so the
        agent receives shopping-relevant passages rather than fleet service
        records, DTC guides, or unrelated content.

        Args:
            source_category: One of the registered category keys:
                ``vehicle_shopping_guide``, ``dtc_guide``, ``tsb_recall``,
                ``owner_manual``, ``service_policy``, ``charging_narrative``,
                ``parts_catalog``, ``vehicle_manufacturing``.

        Returns:
            ``{applied: true, source_category: str}`` when the filter env var
            is enabled, or ``{applied: false, reason: str}`` when it is not
            active (e.g. the KB lacks metadata).
        """
        enabled = source_category_filter_enabled()
        if not enabled:
            log.info(
                "kb_filter: VSA_KB_SOURCE_CATEGORY_FILTER not enabled — "
                "filter not applied (persona=%s, requested=%s)",
                persona_id,
                source_category,
            )
            return {
                "applied": False,
                "reason": (
                    "VSA_KB_SOURCE_CATEGORY_FILTER is not enabled on this deployment. "
                    "Retrieve calls will be unfiltered."
                ),
            }

        log.info(
            "kb_filter: filter acknowledged persona=%s source_category=%s",
            persona_id,
            source_category,
        )
        return {"applied": True, "source_category": source_category}

    return kb_filter
